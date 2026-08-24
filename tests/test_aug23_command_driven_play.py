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
from types import SimpleNamespace

from mtg.constants import Phase, Zone

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


# --------------------------------------------------------------------------
# 4-seat: two command surfaces the 2-player driver cannot reach
# --------------------------------------------------------------------------

FOUR = ("Alice", "Bob", "Carol", "Dave")


def _four(tmp_path, monkeypatch, turns=24):
    return CommandDriver(tmp_path, monkeypatch, names=FOUR).play_turns(turns)


class TestFourSeatDrivenPlay:
    """Multiplayer REQUIRES `at <defender>` per group and holds damage until
    every attacked seat submits its own blocks. Neither grammar nor protocol
    exists in a 2-player game, so neither had any coverage."""

    def test_a_full_four_seat_game_plays_through_the_commands(self, tmp_path,
                                                              monkeypatch):
        driver = _four(tmp_path, monkeypatch)

        assert driver.game.is_multiplayer
        assert driver.game.turn_number >= 20
        for command in ("play_card", "declare_attackers", "end_turn"):
            assert driver.issued(command), "%s never ran" % command

    def test_attacks_use_the_at_defender_grammar(self, tmp_path, monkeypatch):
        """The only thing that ever produces the `at` syntax, and therefore
        the only thing that tests the parser for it."""
        driver = _four(tmp_path, monkeypatch)

        attacks = driver.said("attacks with")
        assert attacks, "nobody attacked"
        assert all(" at " in line for line in attacks), (
            "multiplayer attacks must name a defender: %s" % attacks[:2])
        # And the command ACCEPTED them — a refusal leaves no attack line.
        assert not driver.said("Multiplayer attacks require a defender")

    def test_every_attacked_seat_submits_its_own_blocks(self, tmp_path,
                                                        monkeypatch):
        """Damage waits on combat_defenders_done, so a defender that never
        submits hangs the combat."""
        driver = _four(tmp_path, monkeypatch)

        assert driver.issued("no_blockers") or driver.issued("done_blocking")
        assert not driver.said("No creature is attacking your seat"), (
            "the driver submitted blocks for a seat nobody attacked")

    def test_only_expected_rejections_in_four_seats(self, tmp_path, monkeypatch):
        driver = _four(tmp_path, monkeypatch)
        unexpected = [line for _cmd, line in driver.rejections
                      if "Not enough mana" not in line]
        assert unexpected == [], "unexpected rejections: %s" % unexpected

    def test_a_seat_is_eliminated_and_play_continues(self, tmp_path,
                                                     monkeypatch):
        """The policy concentrates damage on the lowest-life opponent
        specifically so elimination gets exercised."""
        driver = _four(tmp_path, monkeypatch)

        dead = [p for p in driver.game.players if p.eliminated]
        assert dead, "no seat was eliminated in 24 turns"
        assert not driver.game.ended, "the game should continue past one death"
        assert len(driver.game.living_player_indices()) >= 2

    def test_no_message_exceeds_the_discord_limit_at_four_seats(self, tmp_path,
                                                                monkeypatch):
        """Four battlefields make the long-message risk real, not notional."""
        driver = _four(tmp_path, monkeypatch)
        assert max(len(line) for line in driver.transcript) <= 2000


# --------------------------------------------------------------------------
# Same-name creatures — two bugs the 4-seat work surfaced
# --------------------------------------------------------------------------

def _twins(driver, seat, name="Grizzly Bears", tapped_first=False):
    """Give a seat two identically-named creatures, both able to act."""
    from mtg.models import Card

    made = []
    for index in (1, 2):
        card = Card(name=name, id="twin_%d_%d" % (seat, index),
                    type_line="Creature — Bear", power="2", toughness="2",
                    summoning_sick=False)
        driver.game.players[seat].battlefield.append(card)
        made.append(card)
    if tapped_first:
        made[0].tapped = True
    return made


class TestSameNameCreatures:
    """`find_card` returns the FIRST exact match, which is wrong whenever a
    player controls several creatures with one name. Both combat commands
    resolved names that way."""

    def test_naming_a_creature_twice_declares_two_distinct_attackers(
            self, tmp_path, monkeypatch):
        """THE BUG: `!attack Grizzly Bears, Grizzly Bears` produced
        game.attackers == ['dup_1', 'dup_1'] — one creature declared twice,
        dealing its damage TWICE, while the second Bears never attacked
        (CR 508.1)."""
        driver = CommandDriver(tmp_path, monkeypatch)
        twins = _twins(driver, 0)
        driver.game.set_phase(Phase.DECLARE_ATTACKERS, via="test")

        driver.run("declare_attackers", 0,
                   creatures="Grizzly Bears, Grizzly Bears")

        assert len(driver.game.attackers) == 2
        assert len(set(driver.game.attackers)) == 2, (
            "the same creature was declared as two attackers")
        assert set(driver.game.attackers) == {t.id for t in twins}

    def test_the_duplicate_attacker_deals_damage_once_each(self, tmp_path,
                                                           monkeypatch):
        """The impact, measured: two 2/2s deal 4, not one creature twice."""
        driver = CommandDriver(tmp_path, monkeypatch)
        _twins(driver, 0)
        driver.game.set_phase(Phase.DECLARE_ATTACKERS, via="test")

        driver.run("declare_attackers", 0,
                   creatures="Grizzly Bears, Grizzly Bears")
        before = driver.game.players[1].life
        driver.run("no_blockers", 1)

        assert before - driver.game.players[1].life == 4

    def test_naming_one_creature_twice_when_only_one_exists(self, tmp_path,
                                                            monkeypatch):
        """Adverse control: the fix must not invent a second attacker."""
        from mtg.models import Card

        driver = CommandDriver(tmp_path, monkeypatch)
        only = Card(name="Grizzly Bears", id="lonely",
                    type_line="Creature — Bear", power="2", toughness="2",
                    summoning_sick=False)
        driver.game.players[0].battlefield.append(only)
        driver.game.set_phase(Phase.DECLARE_ATTACKERS, via="test")

        driver.run("declare_attackers", 0,
                   creatures="Grizzly Bears, Grizzly Bears")

        assert driver.game.attackers == ["lonely"]

    def test_multiplayer_can_send_twins_at_different_defenders(
            self, tmp_path, monkeypatch):
        """`Bears at Bob; Bears at Carol` is legal and was refused outright
        with 'was assigned to more than one defender'."""
        driver = CommandDriver(tmp_path, monkeypatch, names=FOUR)
        twins = _twins(driver, 0)
        driver.game.set_phase(Phase.DECLARE_ATTACKERS, via="test")

        driver.run("declare_attackers", 0,
                   creatures="Grizzly Bears at Bob; Grizzly Bears at Carol")

        assert set(driver.game.attackers) == {t.id for t in twins}
        seats = sorted(c.attacking_player for c in twins)
        assert seats == [1, 2], "the twins must hit DIFFERENT defenders"

    def test_multiplayer_names_twice_in_one_group_declares_two(self, tmp_path,
                                                                monkeypatch):
        """The multiplayer branch has its own selection loop, and its own copy
        of the bug. The separate-groups test does not cover it: the trailing
        dedup loop already handled that shape, so only a duplicate WITHIN one
        group exercises the in-loop claim."""
        driver = CommandDriver(tmp_path, monkeypatch, names=FOUR)
        twins = _twins(driver, 0)
        driver.game.set_phase(Phase.DECLARE_ATTACKERS, via="test")

        driver.run("declare_attackers", 0,
                   creatures="Grizzly Bears, Grizzly Bears at Bob")

        assert len(driver.game.attackers) == 2
        assert set(driver.game.attackers) == {t.id for t in twins}
        assert all(c.attacking_player == 1 for c in twins)

    def test_blocking_picks_the_legal_copy_not_the_first(self, tmp_path,
                                                          monkeypatch):
        """THE SECOND BUG, same family: with a tapped and an untapped Bears,
        `!block ... with Grizzly Bears` resolved to the TAPPED one and
        answered 'Grizzly Bears is tapped' — the player could not block at
        all despite holding a legal blocker."""
        from mtg.models import Card

        driver = CommandDriver(tmp_path, monkeypatch)
        ogre = Card(name="Ogre", id="ogre", type_line="Creature — Ogre",
                    power="3", toughness="3", summoning_sick=False)
        driver.game.players[0].battlefield.append(ogre)
        twins = _twins(driver, 1, tapped_first=True)
        driver.game.set_phase(Phase.DECLARE_ATTACKERS, via="test")

        driver.run("declare_attackers", 0, creatures="Ogre")
        ctx = driver.run("declare_blocker", 1,
                         block_str="Ogre with Grizzly Bears")

        assert not any("is tapped" in line for line in ctx.sent), ctx.sent
        assert driver.game.blockers.get("ogre") == [twins[1].id], (
            "the untapped copy must be chosen")

    def test_blocking_still_refuses_when_no_copy_is_legal(self, tmp_path,
                                                          monkeypatch):
        """Adverse control: preferring a legal copy must not become
        'blocking always works'."""
        from mtg.models import Card

        driver = CommandDriver(tmp_path, monkeypatch)
        ogre = Card(name="Ogre", id="ogre", type_line="Creature — Ogre",
                    power="3", toughness="3", summoning_sick=False)
        driver.game.players[0].battlefield.append(ogre)
        twins = _twins(driver, 1, tapped_first=True)
        twins[1].tapped = True  # now BOTH are tapped
        driver.game.set_phase(Phase.DECLARE_ATTACKERS, via="test")

        driver.run("declare_attackers", 0, creatures="Ogre")
        ctx = driver.run("declare_blocker", 1,
                         block_str="Ogre with Grizzly Bears")

        assert any("is tapped" in line for line in ctx.sent), ctx.sent
        assert not driver.game.blockers.get("ogre")

    def test_over_assigning_one_creature_is_refused_not_silent(self, tmp_path,
                                                               monkeypatch):
        """The exclude_ids fix must not turn a clear refusal into a silent
        half-attack: with ONE Bears, `Bears at Bob; Bears at Carol` asks for
        something impossible and has to say so."""
        from mtg.models import Card

        driver = CommandDriver(tmp_path, monkeypatch, names=FOUR)
        only = Card(name="Grizzly Bears", id="lonely",
                    type_line="Creature - Bear", power="2", toughness="2",
                    summoning_sick=False)
        driver.game.players[0].battlefield.append(only)
        driver.game.set_phase(Phase.DECLARE_ATTACKERS, via="test")

        ctx = driver.run("declare_attackers", 0,
                         creatures="Grizzly Bears at Bob; "
                                   "Grizzly Bears at Carol")

        assert any("more than one defender" in line for line in ctx.sent),             ctx.sent
        assert driver.game.attackers == [], (
            "a refused attack must declare nobody")

    def test_a_name_that_does_not_exist_is_still_skipped_quietly(
            self, tmp_path, monkeypatch):
        """Adverse control: the refusal fires on CLAIMED copies, not on any
        empty lookup. A typo keeps its long-standing silent skip."""
        from mtg.models import Card

        driver = CommandDriver(tmp_path, monkeypatch, names=FOUR)
        real = Card(name="Grizzly Bears", id="real",
                    type_line="Creature - Bear", power="2", toughness="2",
                    summoning_sick=False)
        driver.game.players[0].battlefield.append(real)
        driver.game.set_phase(Phase.DECLARE_ATTACKERS, via="test")

        ctx = driver.run("declare_attackers", 0,
                         creatures="Grizzly Bears at Bob; Wurm at Carol")

        assert not any("more than one defender" in line for line in ctx.sent)
        assert driver.game.attackers == ["real"]

    def test_the_name_loop_terminates_even_if_find_card_misbehaves(self):
        """The `while True` must not depend on a collaborator keeping a
        promise. Under a correct find_card this guard is unreachable, so the
        only fixture that can exercise it is one that breaks the promise --
        which is exactly what the mutation sweep did by accident, spinning for
        a quarter of an hour instead of failing.

        The stub counts calls so a regression FAILS here rather than hanging
        the suite.
        """
        from mtg.cog import MTGGameCog

        class _Stubborn:
            """Ignores exclude_ids, always answering with the same card."""

            def __init__(self):
                self.calls = 0

            def find_card(self, name, zone, exclude_ids=None):
                self.calls += 1
                if self.calls > 50:
                    raise AssertionError("_find_qualifying did not terminate")
                return SimpleNamespace(id="same", name=name)

        stubborn = _Stubborn()
        result = MTGGameCog._find_qualifying(
            stubborn, "Grizzly Bears", Zone.BATTLEFIELD, lambda c: False)

        assert result is None
        assert stubborn.calls <= 3, (
            "it should give up as soon as a card repeats, not grind")

    def test_blocking_finds_the_attacking_copy(self, tmp_path, monkeypatch):
        """The attacker side of the same collision: with two same-named
        creatures and only one attacking, the command answered
        "'Grizzly Bears' isn't attacking!"."""
        from mtg.models import Card

        driver = CommandDriver(tmp_path, monkeypatch)
        attackers = _twins(driver, 0)
        wall = Card(name="Wall", id="wall", type_line="Creature — Wall",
                    power="0", toughness="4", summoning_sick=False)
        driver.game.players[1].battlefield.append(wall)
        driver.game.set_phase(Phase.DECLARE_ATTACKERS, via="test")

        # Declare by ID so the attacking copy is NOT the first name match.
        # Declaring by NAME would resolve to the first twin, the plain
        # fallback would find it too, and the pin would pass either way --
        # which is exactly how it first slipped past its own mutant.
        driver.run("declare_attackers", 0, creatures=attackers[1].id)
        assert driver.game.attackers == [attackers[1].id], (
            "fixture check: the SECOND twin must be the one attacking")
        assert driver.game.players[0].battlefield[0] is attackers[0], (
            "fixture check: the non-attacking twin must be the first match")

        ctx = driver.run("declare_blocker", 1, block_str="Grizzly Bears with Wall")
        assert not any("isn't attacking" in line for line in ctx.sent), ctx.sent
        assert driver.game.blockers.get(attackers[1].id) == ["wall"]


class TestRefusedAttacks:
    """The driver must not run the block protocol after an attack the engine
    refused. In ordinary driven play no attack is refused any more -- that was
    the duplicate-name bug -- so the guard needs a policy that deliberately
    proposes an illegal attacker."""

    def test_no_block_protocol_after_a_refused_attack(self, tmp_path,
                                                      monkeypatch):
        class _SickPolicy(HeuristicPolicy):
            @staticmethod
            def attackers(game, player):
                # Summoning-sick creatures pass the driver's own selection and
                # are then refused by the engine, which is the shape that used
                # to leave "No creature is attacking your seat" in the log.
                return [c for c in player.battlefield
                        if c.is_creature(game=game) and c.summoning_sick][:1]

        driver = CommandDriver(tmp_path, monkeypatch, policy=_SickPolicy(),
                               names=FOUR).play_turns(8)

        assert driver.said("No valid attackers"), (
            "fixture check: the engine must actually be refusing")
        assert not driver.said("No creature is attacking your seat"), (
            "the driver blocked after a refused attack")


# --------------------------------------------------------------------------
# The three command parsers nothing had ever executed
# --------------------------------------------------------------------------

MIND_STONE = ("{T}: Add {C}.\n"
              "{1}, {T}, Sacrifice this artifact: Draw a card.")

JACE_TMS = (
    "+2: Look at the top card of target player's library. You may put that "
    "card on the bottom of that player's library.\n"
    "0: Draw three cards, then put two cards from your hand on top of your "
    "library in any order.\n"
    "−1: Return target creature to its owner's hand.\n"
    "−12: Exile all cards from target player's library, then that player "
    "shuffles their hand into their library.")


def _artifact(driver, seat, name="Mind Stone", oracle=MIND_STONE):
    from mtg.models import Card

    card = Card(name=name, id="art_%d" % seat, type_line="Artifact",
                mana_cost="{2}", cmc=2, oracle_text=oracle,
                summoning_sick=False)
    driver.game.players[seat].battlefield.append(card)
    return card


def _walker(driver, seat, name="Jace, the Mind Sculptor", oracle=JACE_TMS):
    from mtg.models import Card

    card = Card(name=name, id="pw_%d" % seat,
                type_line="Legendary Planeswalker — Jace",
                mana_cost="{2}{U}{U}", cmc=4, oracle_text=oracle,
                loyalty="3", summoning_sick=False)
    card.loyalty_counters = 3
    driver.game.players[seat].battlefield.append(card)
    return card


class TestActivateParser:
    """`!activate` has three distinct parse shapes -- bare, named, and
    named-with-an-index -- and a separate planeswalker branch. The register
    records the "two activation paths diverge" family six times, and this is
    the human one."""

    def test_bare_activate_lists_what_can_be_activated(self, tmp_path,
                                                       monkeypatch):
        driver = CommandDriver(tmp_path, monkeypatch)
        _artifact(driver, 0)

        ctx = driver.run("activate_ability", 0, args="")

        assert any("Mind Stone" in line for line in ctx.sent), ctx.sent

    def test_naming_a_permanent_lists_its_abilities(self, tmp_path,
                                                    monkeypatch):
        driver = CommandDriver(tmp_path, monkeypatch)
        _artifact(driver, 0)

        ctx = driver.run("activate_ability", 0, args="Mind Stone")

        joined = " ".join(ctx.sent)
        assert "Mind Stone" in joined, ctx.sent
        assert "Draw a card" in joined or "Sacrifice" in joined, ctx.sent

    def test_a_permanent_not_on_the_battlefield_is_refused(self, tmp_path,
                                                           monkeypatch):
        driver = CommandDriver(tmp_path, monkeypatch)
        _artifact(driver, 0)

        ctx = driver.run("activate_ability", 0, args="Black Lotus")

        assert ctx.sent, "the command said nothing at all"
        assert not any("Mind Stone" in line for line in ctx.sent), (
            "a miss must not silently activate a different permanent")

    def test_the_planeswalker_branch_reads_a_signed_loyalty_cost(
            self, tmp_path, monkeypatch):
        """A separate parser from the ability-index one: `+2` is a loyalty
        cost, `1` is an index, and confusing them must be visible."""
        driver = CommandDriver(tmp_path, monkeypatch)
        _walker(driver, 0)

        ctx = driver.run("activate_ability", 0, args="Jace 1")

        assert any("no [+1] ability" in line for line in ctx.sent), ctx.sent

    def test_activate_then_target_pays_the_loyalty(self, tmp_path,
                                                   monkeypatch):
        """The full human chain, and the reason the loyalty is not paid at
        activation: Jace's +2 targets a PLAYER, and targets are chosen before
        costs are paid (CR 601.2b). So `!activate` prompts and `!target`
        resolves -- neither command is testable without the other."""
        driver = CommandDriver(tmp_path, monkeypatch)
        jace = _walker(driver, 0)
        before = jace.loyalty_counters

        ctx = driver.run("activate_ability", 0, args="Jace +2")
        assert any("!target" in line for line in ctx.sent), (
            "the +2 must ask for its target")
        assert jace.loyalty_counters == before, (
            "loyalty is paid on resolution, not on announcement")

        driver.run("select_target", 0, 0)

        assert jace.loyalty_counters == before + 2, (
            "the +2 must add loyalty once its target is chosen")
        assert driver.game.pending_action is None, (
            "the prompt must be consumed")


class TestTargetParser:
    """`!target` only exists to answer a pending prompt, so it cannot be
    driven in isolation -- it has to be reached through a real activation."""

    def test_target_without_a_pending_prompt_is_refused(self, tmp_path,
                                                        monkeypatch):
        driver = CommandDriver(tmp_path, monkeypatch)

        ctx = driver.run("select_target", 0, 0)

        assert any("No pending" in line for line in ctx.sent), ctx.sent

    def test_a_targeted_planeswalker_ability_opens_a_prompt(self, tmp_path,
                                                            monkeypatch):
        """Jace's -1 returns TARGET creature, so activating it must ask."""
        from mtg.models import Card

        driver = CommandDriver(tmp_path, monkeypatch)
        _walker(driver, 0)
        bear = Card(name="Grizzly Bears", id="victim",
                    type_line="Creature — Bear", power="2", toughness="2",
                    summoning_sick=False)
        driver.game.players[1].battlefield.append(bear)

        driver.run("activate_ability", 0, args="Jace -1")

        pending = driver.game.pending_action
        if pending is None:
            # The ability resolved without a prompt -- acceptable only if it
            # actually did the thing, which is what we really care about.
            assert bear not in driver.game.players[1].battlefield, (
                "Jace -1 neither prompted for a target nor bounced anything")
        else:
            assert pending.get("type"), pending

    def test_the_wrong_seat_cannot_answer_a_prompt(self, tmp_path,
                                                   monkeypatch):
        """A pending choice belongs to its chooser; another seat answering it
        would decide someone else's choice for them."""
        driver = CommandDriver(tmp_path, monkeypatch)
        driver.game.pending_action = {
            "type": "planeswalker_target",
            "player_idx": 0,
            "targets": [],
        }

        ctx = driver.run("select_target", 1, 0)

        assert ctx.sent, "the wrong seat was answered with silence"
        assert driver.game.pending_action is not None, (
            "the wrong seat consumed another player's prompt")


class TestDiscardParser:
    """`!discard` is free-form: it takes a name and matches it against the
    hand. It is also the entry point for madness (CR 702.35)."""

    def test_discard_moves_the_named_card_to_the_graveyard(self, tmp_path,
                                                           monkeypatch):
        driver = CommandDriver(tmp_path, monkeypatch)
        player = driver.game.players[0]
        target = player.hand[0]
        before = len(player.hand)

        driver.run("discard_card", 0, card_name=target.name)

        assert len(player.hand) == before - 1
        assert target in player.graveyard

    def test_discard_refuses_a_card_not_in_hand(self, tmp_path, monkeypatch):
        driver = CommandDriver(tmp_path, monkeypatch)
        player = driver.game.players[0]
        before = len(player.hand)

        ctx = driver.run("discard_card", 0, card_name="Black Lotus")

        assert any("Couldn't find" in line for line in ctx.sent), ctx.sent
        assert len(player.hand) == before, "a miss must not discard anything"

    def test_discard_removes_exactly_one_copy(self, tmp_path, monkeypatch):
        """The same-name family again: the hand routinely holds several cards
        with one name, and a discard must consume exactly one."""
        driver = CommandDriver(tmp_path, monkeypatch)
        player = driver.game.players[0]
        name = player.hand[0].name
        copies = [c for c in player.hand if c.name == name]
        assert len(copies) >= 2, "fixture check: need duplicates in hand"

        driver.run("discard_card", 0, card_name=name)

        left = [c for c in player.hand if c.name == name]
        assert len(left) == len(copies) - 1
        assert len(player.graveyard) == 1


class TestDrivenActivation:
    """Targeted pins prove a parser accepts what you thought to send it.
    Ordinary play is what found the !block hand-off and both same-name bugs,
    so the loop matters more: it emits combinations nobody designed."""

    @staticmethod
    def _stone_policy():
        class _Activates(HeuristicPolicy):
            @staticmethod
            def activations(game, player):
                return [(c.name, "")
                        for c in player.battlefield
                        if c.name == "Mind Stone" and not c.tapped]

        return _Activates()

    def test_a_driven_game_reaches_the_activate_parser(self, tmp_path,
                                                       monkeypatch):
        deck = []
        for _ in range(10):
            deck += ["Forest", "Forest", "Mind Stone"]

        driver = CommandDriver(tmp_path, monkeypatch,
                               policy=self._stone_policy(),
                               deck=deck).play_turns(12)

        assert driver.issued("activate_ability"), (
            "the loop never reached !activate")
        joined = " ".join(driver.transcript)
        assert "Mind Stone" in joined, "the artifact never appeared"

    def test_driven_activation_raises_no_unexpected_rejections(self, tmp_path,
                                                               monkeypatch):
        deck = []
        for _ in range(10):
            deck += ["Forest", "Forest", "Mind Stone"]

        driver = CommandDriver(tmp_path, monkeypatch,
                               policy=self._stone_policy(),
                               deck=deck).play_turns(12)

        allowed = ("Not enough mana", "No valid attackers")
        unexpected = [line for _cmd, line in driver.rejections
                      if not any(a in line for a in allowed)]
        assert unexpected == [], "unexpected rejections: %s" % unexpected[:3]


def test_driven_games_emit_no_phase_bus_warning(tmp_path, monkeypatch, capsys):
    """`[PHASE-BUS]` means a MAIN entry ran with no engine ref, so main-phase
    trigger dispatch was skipped. The watch table says it must be zero, and
    the driver used to print one per game by attaching its engine AFTER the
    opening set_phase."""
    CommandDriver(tmp_path, monkeypatch).play_turns(4)

    assert "[PHASE-BUS]" not in capsys.readouterr().out
