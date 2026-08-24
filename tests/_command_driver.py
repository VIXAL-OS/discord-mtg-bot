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
        castable = [c for c in player.hand
                    if not c.is_land() and c.is_creature()]
        return sorted(castable, key=lambda c: getattr(c, "cmc", 0) or 0)

    @staticmethod
    def attackers(game, player):
        return [c for c in player.battlefield
                if c.is_creature(game=game) and not c.tapped
                and not c.summoning_sick]

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
        game.set_phase(Phase.MAIN1, via="driver:start")

        engine = GameEngine(None)
        engine.games[self.THREAD_ID] = game
        game._rules_engine = engine.rules

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
            self.take_turn(self.game.active_player_index)
        return self

    def take_turn(self, seat):
        player = self.game.players[seat]

        # --- main phase 1: lands then spells, each through !play
        for land in self.policy.lands_to_play(self.game, player)[:1]:
            self.run("play_card", seat, card_name=land.name)
        for spell in self.policy.spells_to_cast(self.game, player)[:1]:
            self.run("play_card", seat, card_name=spell.name)

        # --- walk into combat with !pass, exactly as a player must
        self._pass_until(seat, Phase.DECLARE_ATTACKERS, limit=4)

        # --- attack
        if self.game.phase == Phase.DECLARE_ATTACKERS and not self.game.ended:
            chosen = self.policy.attackers(self.game, player)
            if chosen:
                self.run("declare_attackers", seat,
                         creatures=", ".join(c.name for c in chosen))
                self._resolve_blocks(seat, [c.name for c in chosen])

        # --- out of combat and end the turn
        self._pass_until(seat, Phase.MAIN2, limit=5)
        if not self.game.ended:
            self.run("end_turn", seat)

    def _pass_until(self, seat, target, limit):
        for _ in range(limit):
            if self.game.ended or self.game.phase == target:
                return
            before = self.game.phase
            self.run("pass_priority", seat)
            if self.game.phase == before:
                return  # the command refused; do not spin

    def _resolve_blocks(self, attacker_seat, attacker_names):
        defender_seat = 1 - attacker_seat
        defender = self.game.players[defender_seat]
        if self.game.ended:
            return
        chosen = self.policy.blocks(self.game, defender, attacker_names)
        for attacker_name, blocker_name in chosen:
            self.run("declare_blocker", defender_seat,
                     block_str="%s with %s" % (attacker_name, blocker_name))
        if chosen:
            self.run("done_blocking", defender_seat)
        else:
            self.run("no_blockers", defender_seat)
