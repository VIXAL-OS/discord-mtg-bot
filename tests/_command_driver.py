"""Play a whole game through the real Discord COMMAND callbacks. NOT a test.

WHY THIS EXISTS. The project already has an AI driving human code paths —
Rick Deckard in `!autoplay` — but Rick enters at
`cog.engine._execute_action(...)`, one layer BELOW the command surface, and
autoplay never parses command text at all. So ten batches of Rick exercise
`play_land` / `cast_spell_async` and never touch `!play`'s name matching,
`!attack`'s `at` syntax, `!turn`'s handoff, or the embeds.

That ceiling is not theoretical. Every bug found by hand on Aug 23 lived above
Rick's entry point: `!turn` leaving the incoming human at UNTAP with no draw,
and an `!attack` tuple unpack that crashed EVERY 2-player attack. This driver
is the layer that would have caught both automatically.

WHAT IT IS: a decision POLICY plus a renderer. The policy answers semantic
questions ("which creatures attack?"); the driver turns the answer into
command TEXT and feeds it to the real callback. The rendering step is the
point, not overhead — it is the only thing that ever exercises the command
parsers.

WHAT IT IS NOT: Discord coverage. A fake context cannot test permissions, DM
delivery, thread membership, gateway reconnect or rate limits. Those stay with
the human pilot. Call this COMMAND-LAYER coverage and nothing more.
"""
import asyncio
import re
from types import SimpleNamespace

from mtg.cog import MTGGameCog
from mtg.constants import Phase
from mtg.engine import GameEngine
from mtg.models import Card, GameState, Player


# --------------------------------------------------------------------------
# A Discord context faithful enough to catch what Discord actually enforces
# --------------------------------------------------------------------------

DISCORD_MESSAGE_LIMIT = 2000


class RecordingCtx:
    """The slice of commands.Context the command bodies touch.

    It asserts the ONE Discord constraint a fake context can honestly check:
    a message over 2000 characters is rejected by the real API, and the
    register has truncation bugs on record.
    """

    def __init__(self, author, channel_id, transcript):
        self.author = author
        self.channel = SimpleNamespace(id=channel_id)
        self.guild = SimpleNamespace(id=1)
        self.sent = []
        self._transcript = transcript

    async def send(self, content=None, embed=None, **kwargs):
        if content is not None:
            text = str(content)
            if len(text) > DISCORD_MESSAGE_LIMIT:
                raise AssertionError(
                    "a %d-char message would be rejected by Discord (limit %d): %r"
                    % (len(text), DISCORD_MESSAGE_LIMIT, text[:120]))
            self.sent.append(text)
            self._transcript.append(text)
        elif embed is not None:
            self.sent.append("<embed>")
            self._transcript.append("<embed>")
        return None


# --------------------------------------------------------------------------
# Policies — the "AI" half, deliberately pluggable
# --------------------------------------------------------------------------

class HeuristicPolicy:
    """Deterministic, offline, good enough to produce a real game.

    CI must never reach an LLM (see the `rules` fixture note in conftest), so
    the default policy is arithmetic. The driver does not care where decisions
    come from — see AIPolicy for the batch-time alternative.
    """

    name = "heuristic"

    @staticmethod
    def lands_to_play(game, player):
        return [c for c in player.hand if c.is_land()]

    @staticmethod
    def spells_to_cast(game, player):
        """Cheapest first, and let the COMMAND reject what it will.

        Deliberately not pre-filtered to perfection: a rejected cast exercises
        the command's error path, which is exactly the kind of thing a
        scripted test never reaches.
        """
        # Non-land, not "creature": the docstring's own rule is to let the
        # command reject what it will, and a creature-only filter silently
        # excluded every artifact -- so !activate could never be reached by
        # ordinary play.
        castable = [c for c in player.hand if not c.is_land()]
        return sorted(castable, key=lambda c: getattr(c, "cmc", 0) or 0)

    @staticmethod
    def attackers(game, player):
        return [c for c in player.battlefield
                if c.is_creature(game=game) and not c.tapped
                and not c.summoning_sick]

    @staticmethod
    def activations(game, player):
        """Which abilities to activate, as (permanent name, ability arg).

        Empty by default so existing driven games are unchanged -- an
        activation policy is opt-in, because activating changes the shape of
        every downstream turn.
        """
        return []

    @staticmethod
    def defenders(game, player, attackers):
        """Who each attacker hits: the living opponent on the lowest life.

        Spreading damage would be better play; concentrating it is better
        TESTING, because it drives a seat to zero and exercises elimination
        and the shrinking turn rotation.
        """
        seat = game.players.index(player)
        living = [i for i in game.living_player_indices() if i != seat]
        if not living:
            return []
        target = min(living, key=lambda i: game.players[i].life)
        return [(card, target) for card in attackers]

    @staticmethod
    def blocks(game, defender, attacker_names):
        """Block the first attacker with the first free creature, once."""
        free = [c for c in defender.battlefield
                if c.is_creature(game=game) and not c.tapped]
        if not free or not attacker_names:
            return []
        return [(attacker_names[0], free[0].name)]


class AIPolicy:
    """Route the real `ClaudePlayer` decisions through the command surface.

    This is the batch-time policy: same driver, same rendering, but the
    semantic answers come from the model that already plays autoplay. It needs
    a configured client, so it is never the default and never runs in CI.
    """

    name = "ai"

    def __init__(self, claude_player):
        self.ai = claude_player

    def lands_to_play(self, game, player):
        return HeuristicPolicy.lands_to_play(game, player)

    def spells_to_cast(self, game, player):
        return HeuristicPolicy.spells_to_cast(game, player)

    def attackers(self, game, player):
        index = game.players.index(player)
        # asyncio.run, not get_event_loop().run_until_complete: the latter is
        # deprecated and warns today, and the driver is always called from
        # sync context (each command gets its own asyncio.run).
        chosen = asyncio.run(self.ai.decide_attackers(game, index))
        wanted = {str(n).lower() for n in (chosen or [])}
        return [c for c in player.battlefield
                if c.name.lower() in wanted and not c.tapped]

    def blocks(self, game, defender, attacker_names):
        return HeuristicPolicy.blocks(game, defender, attacker_names)

    def defenders(self, game, player, attackers):
        return HeuristicPolicy.defenders(game, player, attackers)

    def activations(self, game, player):
        return HeuristicPolicy.activations(game, player)


# --------------------------------------------------------------------------
# The driver
# --------------------------------------------------------------------------

class CommandDriver:
    """A two-human game played entirely through `MTGGameCog` callbacks."""

    THREAD_ID = 5150

    def __init__(self, tmp_path, monkeypatch, policy=None, deck=None,
                 names=("Alice", "Bob"), life=20):
        monkeypatch.setattr(GameEngine, "GAMES_DIR", str(tmp_path / "games"))
        self.policy = policy or HeuristicPolicy()
        self.transcript = []
        self.commands = []
        self.rejections = []

        players = [Player(name=n, user_id=2000 + i, life=life)
                   for i, n in enumerate(names)]
        game = GameState(thread_id=self.THREAD_ID, format="modern",
                         players=players)
        game.turn_number = 1
        game.active_player_index = 0
        for index, player in enumerate(players):
            for spec in (deck or self._default_deck()):
                player.library.append(self._card(spec, index, len(player.library)))
        # Opening hands. Without them both seats start empty and draw one a
        # turn, so !play is barely reached and !attack never is.
        for player in players:
            for _ in range(7):
                if player.library:
                    player.hand.append(player.library.pop(0))
        engine = GameEngine(None)
        engine.games[self.THREAD_ID] = game
        game._rules_engine = engine.rules
        # set_phase AFTER the engine is attached. The other way round, the
        # opening MAIN1 has no engine ref, so main-phase trigger dispatch is
        # skipped and every driven game prints a [PHASE-BUS] line -- a signal
        # the watch table says must be zero.
        game.set_phase(Phase.MAIN1, via="driver:start")

        cog = object.__new__(MTGGameCog)
        cog.engine = engine
        cog.display = SimpleNamespace(
            create_game_embed=lambda g: SimpleNamespace(fields=[]))
        cog._get_game = lambda _ctx: game
        cog._sanitize_action_bullets = lambda a: a

        self.game, self.engine, self.cog = game, engine, cog
        self.users = [SimpleNamespace(id=2000 + i, display_name=n,
                                      mention="<@%d>" % (2000 + i))
                      for i, n in enumerate(names)]

    # ---------------------------------------------------------------- setup
    @staticmethod
    def _default_deck():
        """Simple, deterministic, and INTERLEAVED.

        Order matters: a block of lands followed by a block of spells gives an
        opening hand of pure lands and a game where nothing is ever cast, so
        the interesting commands never fire. Two lands per creature keeps the
        curve payable without needing a shuffle (which would make the driver
        non-deterministic).
        """
        deck = []
        for _ in range(10):
            deck += ["Forest", "Forest", "Grizzly Bears"]
        return deck

    @staticmethod
    def _card(name, owner, seq):
        if name == "Forest":
            return Card(name="Forest", id="F_%d_%d" % (owner, seq),
                        type_line="Basic Land — Forest", mana_cost="")
        if name == "Mind Stone":
            # Real text, from data/card_data_cache.json.
            return Card(name="Mind Stone", id="A_%d_%d" % (owner, seq),
                        type_line="Artifact", mana_cost="{2}", cmc=2,
                        oracle_text="{T}: Add {C}.\n{1}, {T}, Sacrifice this "
                                    "artifact: Draw a card.")
        return Card(name=name, id="C_%d_%d" % (owner, seq),
                    type_line="Creature — Bear", mana_cost="{1}{G}", cmc=2,
                    power="2", toughness="2")

    # -------------------------------------------------------------- plumbing
    def run(self, command_attr, seat, *args, **kwargs):
        """Invoke a REAL command callback as `seat`, recording what happened."""
        ctx = RecordingCtx(self.users[seat], self.THREAD_ID, self.transcript)
        self.commands.append((command_attr, seat, args, kwargs))
        callback = getattr(MTGGameCog, command_attr).callback
        asyncio.run(callback(self.cog, ctx, *args, **kwargs))
        for line in ctx.sent:
            if line.startswith(("⚠️", "❌")) or "not your turn" in line.lower():
                self.rejections.append((command_attr, line))
        return ctx

    def issued(self, command_attr):
        return [c for c in self.commands if c[0] == command_attr]

    def said(self, needle):
        return [line for line in self.transcript if needle in line]

    # ------------------------------------------------------------- the loop
    def play_turns(self, count):
        """Play `count` whole turns, alternating seats, or stop if the game ends."""
        for _ in range(count):
            if self.game.ended:
                break
            seat = self.game.active_player_index
            if self.game.players[seat].eliminated:
                # Rotation should already skip the dead; if it has not, do not
                # drive them — every mutating command would refuse anyway.
                break
            self.take_turn(seat)
        return self

    def take_turn(self, seat):
        player = self.game.players[seat]

        # --- main phase 1: lands then spells, each through !play
        for land in self.policy.lands_to_play(self.game, player)[:1]:
            self.run("play_card", seat, card_name=land.name)
        for spell in self.policy.spells_to_cast(self.game, player)[:1]:
            self.run("play_card", seat, card_name=spell.name)

        # --- activated abilities, through the real !activate parser
        for name, ability in self.policy.activations(self.game, player)[:2]:
            self.run("activate_ability", seat,
                     args=("%s %s" % (name, ability)).strip())
            self._answer_pending(seat)

        # --- walk into combat with !pass, exactly as a player must
        self._pass_until(seat, Phase.DECLARE_ATTACKERS, limit=4)

        # --- attack
        if self.game.phase == Phase.DECLARE_ATTACKERS and not self.game.ended:
            chosen = self.policy.attackers(self.game, player)
            if chosen:
                text, defender_seats = self._render_attack(seat, player, chosen)
                if text is not None:
                    self.run("declare_attackers", seat, creatures=text)
                    # Only run the block protocol if the attack was ACCEPTED.
                    # A refused !attack leaves game.attackers empty, and
                    # submitting blocks anyway earns "No creature is attacking
                    # your seat" — noise that hides the real refusal.
                    if self.game.attackers:
                        self._resolve_blocks(seat, [c.name for c in chosen],
                                             defender_seats)

        # --- out of combat and end the turn
        self._pass_until(seat, Phase.MAIN2, limit=5)
        if not self.game.ended:
            self.run("end_turn", seat)

    def _answer_pending(self, seat):
        """Answer a target prompt if the last command raised one.

        `!target` cannot be driven on its own -- it only ever answers a
        pending choice -- so the loop has to reach it through an activation,
        which is exactly how a human gets there.
        """
        pending = getattr(self.game, "pending_action", None)
        if not pending:
            return
        options = pending.get("targets") or pending.get("options") or []
        if not options:
            return
        self.run("select_target", seat, 0)

    def _render_attack(self, seat, player, chosen):
        """Turn chosen attackers into command TEXT.

        Multiplayer REQUIRES `at <defender>` per group and refuses the whole
        command without it, so this is the only place the semicolon/`at`
        grammar is ever produced — and therefore the only thing that tests
        the parser for it. Two-player keeps the bare comma list, which is the
        form that command has always accepted.
        """
        if not self.game.is_multiplayer:
            return ", ".join(c.name for c in chosen), [1 - seat]

        assignments = self.policy.defenders(self.game, player, chosen)
        if not assignments:
            return None, []
        groups = {}
        for card, defender_seat in assignments:
            groups.setdefault(defender_seat, []).append(card.name)
        text = "; ".join(
            "%s at %s" % (", ".join(names), self.game.players[d].name)
            for d, names in groups.items())
        return text, list(groups)

    def _pass_until(self, seat, target, limit):
        for _ in range(limit):
            if self.game.ended or self.game.phase == target:
                return
            before = self.game.phase
            self.run("pass_priority", seat)
            if self.game.phase == before:
                return  # the command refused; do not spin

    def _resolve_blocks(self, attacker_seat, attacker_names, defender_seats):
        """Every attacked seat submits its OWN blocks.

        Multiplayer holds damage until each defender has finalized
        (`combat_defenders_done`), so a driver that submitted once would hang
        the combat — and a driver that submitted for an UNATTACKED seat would
        be told "No creature is attacking your seat".
        """
        for defender_seat in defender_seats:
            if self.game.ended:
                return
            if self.game.players[defender_seat].eliminated:
                continue
            defender = self.game.players[defender_seat]
            chosen = self.policy.blocks(self.game, defender, attacker_names)
            for attacker_name, blocker_name in chosen:
                self.run("declare_blocker", defender_seat,
                         block_str="%s with %s" % (attacker_name, blocker_name))
            if chosen:
                self.run("done_blocking", defender_seat)
            else:
                self.run("no_blockers", defender_seat)
