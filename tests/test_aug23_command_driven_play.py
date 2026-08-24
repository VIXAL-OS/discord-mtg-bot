"""A whole 2-player game played through the real Discord command callbacks.

WHY THIS LAYER EXISTS. Rick Deckard already drives human code paths in
`!autoplay` — but he enters at `cog.engine._execute_action(...)`, BELOW the
command surface, and autoplay never parses command text. So Rick exercises
`play_land` / `cast_spell_async` and never `!play`'s name matching, `!attack`'s
syntax, `!block`, or `!turn`'s handoff.

That ceiling has cost real bugs. Everything found by hand on Aug 23 lived above
Rick's entry point, and the very first run of this driver found another one
that ten batches of Rick had never touched: **`!block` could not be used at
all.** `!attack` finished in DECLARE_ATTACKERS and then told the defender to
`!block`, which `can_block_with()` refuses outside DECLARE_BLOCKERS — so every
attack in a human game went through unblocked. `!block` is the only caller of
that check, and nothing in the suite had ever invoked it.

SCOPE, so nobody reads more into it: this is COMMAND-LAYER coverage. A fake
context cannot test Discord permissions, DM delivery, thread membership,
gateway reconnect or rate limits. Those remain the human pilot's job. It does
check the one constraint a fake context can honestly enforce — the 2000-char
message limit — because the register has truncation bugs on record.
"""
from mtg.constants import Phase

from _command_driver import CommandDriver, HeuristicPolicy


# --------------------------------------------------------------------------
# The driver has to be able to actually play
# --------------------------------------------------------------------------

def test_a_full_game_plays_through_the_command_layer(tmp_path, monkeypatch):
    """BASELINE. A driver that silently does nothing would make every
    assertion below vacuous, so this pins that the real commands ran and the
    game genuinely advanced."""
    driver = CommandDriver(tmp_path, monkeypatch).play_turns(12)

    assert driver.game.turn_number >= 10, "the game barely advanced"
    for command in ("play_card", "pass_priority", "end_turn",
                    "declare_attackers", "declare_blocker", "done_blocking"):
        assert driver.issued(command), "%s was never exercised" % command

    # Real cards actually resolved through !play, not just commands issued.
    assert driver.said("Played **Forest**")
    assert driver.said("Cast **Grizzly Bears**")


def test_only_expected_rejections_occur(tmp_path, monkeypatch):
    """A driven game should produce mana rejections and nothing else.

    The policy deliberately tries a cast before checking affordability,
    because a rejected cast exercises the command's error path. Any OTHER
    rejection shape is a finding.
    """
    driver = CommandDriver(tmp_path, monkeypatch).play_turns(12)

    unexpected = [line for _cmd, line in driver.rejections
                  if "Not enough mana" not in line]
    assert unexpected == [], "unexpected command rejections: %s" % unexpected


# --------------------------------------------------------------------------
# The bug this driver found on its first run
# --------------------------------------------------------------------------

def test_the_block_instruction_can_actually_be_followed(tmp_path, monkeypatch):
    """THE FINDING. `!attack` tells the defender to `!block`; `!block` refuses
    outside DECLARE_BLOCKERS. Blocking was impossible in human-vs-human play
    and every attack resolved unblocked."""
    driver = CommandDriver(tmp_path, monkeypatch).play_turns(12)

    refusals = [line for _cmd, line in driver.rejections
                if "declare blockers during" in line]
    assert refusals == [], (
        "the bot told the defender to block and then refused the block: %s"
        % refusals[:2])
    assert driver.said("blocks"), "no block was ever recorded"


def test_attacking_hands_the_defender_the_blockers_step(tmp_path, monkeypatch):
    """The narrow mechanism, pinned directly so a regression is unambiguous."""
    driver = CommandDriver(tmp_path, monkeypatch)
    # Advance to a turn where the attacker has a live creature.
    driver.play_turns(6)

    seat = driver.game.active_player_index
    driver._pass_until(seat, Phase.DECLARE_ATTACKERS, limit=4)
    attackers = HeuristicPolicy.attackers(driver.game,
                                          driver.game.players[seat])
    if not attackers:
        import pytest
        pytest.skip("fixture produced no legal attacker this turn")

    driver.run("declare_attackers", seat,
               creatures=", ".join(c.name for c in attackers))
    assert driver.game.phase == Phase.DECLARE_BLOCKERS, (
        "after handing off to a human defender the game must BE in the "
        "blockers step, or the prompt it just sent is unusable")


def test_a_declared_block_reaches_combat_damage(tmp_path, monkeypatch):
    """End-to-end: the block is not just accepted, it changes the outcome.

    2/2 blocks 2/2, so both die and no damage reaches the player — which is
    visibly different from the unblocked game the bug produced.
    """
    driver = CommandDriver(tmp_path, monkeypatch).play_turns(12)

    assert driver.said("Blocks confirmed"), "blocks never reached damage"
    # With every attack blocked by an equal body, no player damage lands.
    assert [p.life for p in driver.game.players] == [20, 20]


# --------------------------------------------------------------------------
# The two bugs found by hand today — this layer would have caught both
# --------------------------------------------------------------------------

def test_the_turn_handoff_is_exercised_by_real_play(tmp_path, monkeypatch):
    """Aug 23: `!turn` left the incoming human at UNTAP with no draw. Rick
    never saw it because he does not use `!turn`."""
    driver = CommandDriver(tmp_path, monkeypatch).play_turns(4)

    assert len(driver.said("Draw Step")) >= 3, (
        "each handoff must advance the incoming seat through its draw")
    assert driver.game.phase != Phase.UNTAP, (
        "a seat was left stranded at UNTAP")


def test_attack_does_not_crash_in_a_two_player_game(tmp_path, monkeypatch):
    """Aug 23: a tuple unpack in 2-player-only code made `!attack` raise
    ValueError for every 2-player game. No multiplayer test could reach it;
    this driver plays exactly that format."""
    driver = CommandDriver(tmp_path, monkeypatch).play_turns(12)
    assert driver.issued("declare_attackers"), "attacks never happened"
    assert not driver.said("too many values to unpack")


# --------------------------------------------------------------------------
# The one Discord constraint a fake context can honestly enforce
# --------------------------------------------------------------------------

def test_no_message_exceeds_the_discord_limit(tmp_path, monkeypatch):
    """RecordingCtx raises on a >2000-char send, so reaching the end of a
    game is the assertion. The register has truncation bugs on record, and
    this is the cheap half of that class."""
    driver = CommandDriver(tmp_path, monkeypatch).play_turns(12)
    assert driver.transcript, "nothing was sent at all"
    assert max(len(line) for line in driver.transcript) <= 2000


def test_blocking_outside_the_blockers_step_is_still_refused(tmp_path,
                                                             monkeypatch):
    """ADVERSE CONTROL for the fix above (CR 509.1).

    Moving the hand-off into DECLARE_BLOCKERS must not become "blocks are
    legal whenever". Without this pin, deleting the phase gate from
    can_block_with() changes nothing any test can see, because the driver
    only ever blocks during the step.
    """
    driver = CommandDriver(tmp_path, monkeypatch).play_turns(6)

    seat = driver.game.active_player_index
    driver._pass_until(seat, Phase.DECLARE_ATTACKERS, limit=4)
    attackers = HeuristicPolicy.attackers(driver.game,
                                          driver.game.players[seat])
    if not attackers:
        import pytest
        pytest.skip("fixture produced no legal attacker this turn")

    driver.run("declare_attackers", seat,
               creatures=", ".join(c.name for c in attackers))
    assert driver.game.phase == Phase.DECLARE_BLOCKERS

    # Now step OUT of the blockers step and try to block anyway.
    defender_seat = 1 - seat
    defender = driver.game.players[defender_seat]
    blocker = next((c for c in defender.battlefield
                    if c.is_creature(game=driver.game) and not c.tapped), None)
    if blocker is None:
        import pytest
        pytest.skip("fixture produced no legal blocker this turn")

    driver.game.set_phase(Phase.MAIN1, via="test:out-of-step")
    ctx = driver.run("declare_blocker", defender_seat,
                     block_str="%s with %s" % (attackers[0].name, blocker.name))

    assert any("declare blockers during" in line for line in ctx.sent), (
        "a block outside the blockers step must be refused: %s" % ctx.sent)


def test_the_harness_rejects_an_oversized_message(tmp_path, monkeypatch):
    """The length guard, pinned directly.

    The game-level assertion above is a standing net: nothing in this fixture
    produces a message near the limit, so disabling the guard would change
    nothing there. This pins the guard itself, so the net cannot rot into
    decoration.
    """
    import asyncio

    from _command_driver import DISCORD_MESSAGE_LIMIT, RecordingCtx

    transcript = []
    ctx = RecordingCtx(author=None, channel_id=1, transcript=transcript)

    asyncio.run(ctx.send("x" * DISCORD_MESSAGE_LIMIT))  # exactly at the limit
    assert len(transcript) == 1

    try:
        asyncio.run(ctx.send("x" * (DISCORD_MESSAGE_LIMIT + 1)))
    except AssertionError as exc:
        assert "would be rejected by Discord" in str(exc)
    else:
        raise AssertionError("an over-limit message was accepted")


# --------------------------------------------------------------------------
# The AI seam — proven with a stub, because CI must never reach an LLM
# --------------------------------------------------------------------------

class _StubAI:
    """Stands in for ClaudePlayer: same async decide_attackers contract."""

    def __init__(self, choose):
        self.choose = choose
        self.calls = []

    async def decide_attackers(self, game, index):
        self.calls.append(index)
        return self.choose(game, index)


def test_the_ai_seam_drives_the_command_layer(tmp_path, monkeypatch):
    """The whole point of the harness: swap where DECISIONS come from without
    touching how they reach the game.

    Pinned with a stub rather than the real model — CI must never reach an
    LLM (see the `rules` fixture note in conftest) — but the stub honours the
    same async contract `ClaudePlayer.decide_attackers` does, so wiring the
    real one at batch time is a constructor argument and nothing else.

    Without this, AIPolicy would be exactly the declared-with-no-consumer
    shape this project keeps rediscovering as dead code.
    """
    from _command_driver import AIPolicy

    # An AI that refuses to ever attack.
    pacifist = _StubAI(lambda game, index: [])
    driver = CommandDriver(tmp_path, monkeypatch,
                           policy=AIPolicy(pacifist)).play_turns(10)

    assert pacifist.calls, "the AI was never consulted"
    assert not driver.issued("declare_attackers"), (
        "the AI said not to attack and the driver attacked anyway")
    # It still played a real game through the commands.
    assert driver.issued("play_card")
    assert driver.game.turn_number >= 8


def test_the_ai_choice_reaches_the_attack_command(tmp_path, monkeypatch):
    """Adverse control for the pin above: an AI that DOES attack must produce
    a real `!attack`, so 'no attacks' cannot pass for both answers."""
    from _command_driver import AIPolicy

    aggressive = _StubAI(
        lambda game, index: [c.name for c in game.players[index].battlefield
                             if c.is_creature(game=game)])
    driver = CommandDriver(tmp_path, monkeypatch,
                           policy=AIPolicy(aggressive)).play_turns(10)

    assert aggressive.calls
    assert driver.issued("declare_attackers"), (
        "the AI chose attackers and none were declared")
    assert driver.said("attacks with")
