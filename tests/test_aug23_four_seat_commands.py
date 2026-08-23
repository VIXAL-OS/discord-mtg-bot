"""Four-seat human command harness — the Q-G/Q-H validation gap.

The cube FFA smoke is the project's discovery engine, but it drives only the
AI path. Q-G and Q-H are the HUMAN command surface, and until now nothing
drove `!join` / `!ready` / `!leave` / `!startgame` through real command
bodies as four DISTINCT Discord users. That is the part of the multiplayer
work with the least evidence behind it, and it needs no pilot to exercise —
only a mock context.

WHAT THIS COVERS, stated so nobody reads more into it than it earns: the
real command callbacks in `MTGGameCog`, run against a duck-typed cog that
carries only what those bodies touch (`lobbies`, `player_decks`). It does NOT
construct the engine, and it does NOT prove Discord permissions, DM delivery
or thread membership behave in production — those stay pilot-only.

The harness is deliberately kept in this file rather than a shared module
until a second file wants it; extraction is a rename away.
"""
import asyncio

import pytest

from mtg.cog import MTGGameCog
from mtg.lobby import GameLobby, LobbyStore


# --------------------------------------------------------------------------
# Mock Discord context
# --------------------------------------------------------------------------

class FakeAuthor:
    def __init__(self, user_id, display_name):
        self.id = user_id
        self.display_name = display_name
        self.mention = "<@%d>" % user_id


class FakeChannel:
    def __init__(self, channel_id):
        self.id = channel_id
        self.sent = []

    async def send(self, content=None, **kwargs):
        self.sent.append(content or "")
        return None


class FakeCtx:
    """The slice of commands.Context these command bodies actually touch."""

    def __init__(self, author, channel, guild_id=999):
        self.author = author
        self.channel = channel
        self.guild = type("G", (), {"id": guild_id})()
        self.sent = []

    async def send(self, content=None, **kwargs):
        self.sent.append(content or "")
        return None

    @property
    def last(self):
        return self.sent[-1] if self.sent else ""


class Table:
    """Four distinct Discord users sharing one thread."""

    NAMES = ["Alice", "Bob", "Carol", "Dave"]

    def __init__(self, tmp_path, seats=4, names=None):
        self.channel = FakeChannel(channel_id=4242)
        self.cog = type("CogStub", (), {})()
        self.cog.lobbies = LobbyStore(tmp_path / "lobbies")
        self.cog.player_decks = {}
        names = names or self.NAMES
        # Always four people even in a 3-seat lobby: the overflow test needs
        # somebody left over to be refused. Distinct IDs even when display
        # names collide — that is the point of the duplicate-name pin.
        self.users = [FakeAuthor(1000 + i, n) for i, n in enumerate(names)]
        self.lobby = GameLobby(
            thread_id=self.channel.id, guild_id=999,
            owner_user_id=self.users[0].id,
            format_name="commander", max_players=seats)
        self.lobby.add_user(self.users[0].id, self.users[0].display_name, None)
        self.cog.lobbies.save(self.lobby)

    def ctx(self, index):
        return FakeCtx(self.users[index], self.channel)

    def run(self, command_attr, index, *args, **kwargs):
        """Invoke a REAL command callback as user `index`."""
        ctx = self.ctx(index)
        callback = getattr(MTGGameCog, command_attr).callback
        asyncio.run(callback(self.cog, ctx, *args, **kwargs))
        return ctx

    def give_deck(self, index, name="Test Deck"):
        """Load a deck the way `!mydeck` does.

        `!mydeck` writes player_decks AND calls _bind_deck_to_lobby, which is
        what puts the deck on an already-seated player's seat. Writing
        player_decks alone is invisible to a seat that joined earlier, since
        add_user captures the deck at join time.
        """
        deck = {"name": name, "cards": [{"name": "Forest", "quantity": 60}]}
        self.cog.player_decks[self.users[index].id] = deck
        lobby = self.current()
        if lobby is not None and lobby.seat_for_user(self.users[index].id):
            lobby.bind_deck(self.users[index].id, deck)
            self.cog.lobbies.save(lobby)

    def current(self):
        return self.cog.lobbies.get(self.channel.id)


# --------------------------------------------------------------------------
# Q-G — seats are owned by Discord user id
# --------------------------------------------------------------------------

class TestFourSeatsAreStableAndUserOwned:

    def test_four_distinct_users_take_four_seats(self, tmp_path):
        table = Table(tmp_path)
        for i in (1, 2, 3):
            table.run("join_lobby", i)

        seats = table.current().seats
        assert len(seats) == 4
        assert sorted(s.user_id for s in seats) == [1000, 1001, 1002, 1003]
        assert sorted(s.seat_id for s in seats) == [0, 1, 2, 3]

    def test_duplicate_display_names_do_not_collide(self, tmp_path):
        """Two people really can share a display name in Discord. Seats are
        keyed by user id, so this must produce two seats, not one."""
        table = Table(tmp_path, names=["Alex", "Alex", "Alex", "Alex"])
        for i in (1, 2, 3):
            table.run("join_lobby", i)

        seats = table.current().seats
        assert len(seats) == 4, "display name must not be identity"
        assert len({s.user_id for s in seats}) == 4

    def test_joining_twice_is_refused_not_duplicated(self, tmp_path):
        table = Table(tmp_path)
        table.run("join_lobby", 1)
        ctx = table.run("join_lobby", 1)

        assert len(table.current().seats) == 2
        assert "already seated" in ctx.last.lower()

    def test_leaving_does_not_renumber_the_remaining_seats(self, tmp_path):
        """The stable-seat contract: seat ids are identity, not position.
        Renumbering would silently re-point saved games and combat state."""
        table = Table(tmp_path)
        for i in (1, 2, 3):
            table.run("join_lobby", i)
        before = {s.user_id: s.seat_id for s in table.current().seats}

        table.run("leave_lobby", 1)

        after = {s.user_id: s.seat_id for s in table.current().seats}
        assert table.users[1].id not in after
        for user_id, seat_id in after.items():
            assert before[user_id] == seat_id, (
                "seat %d moved when another player left" % seat_id)

    def test_a_full_lobby_refuses_a_fifth_player(self, tmp_path):
        table = Table(tmp_path, seats=3)
        table.run("join_lobby", 1)
        table.run("join_lobby", 2)
        ctx = table.run("join_lobby", 3)

        assert len(table.current().seats) == 3
        assert ctx.last, "a refusal must say something to the player"


class TestReadinessGating:

    def test_ready_requires_a_deck(self, tmp_path):
        table = Table(tmp_path)
        table.run("join_lobby", 1)
        ctx = table.run("ready_lobby", 1)

        seat = next(s for s in table.current().seats
                    if s.user_id == table.users[1].id)
        assert not seat.ready, "readiness without a deck is not readiness"
        assert ctx.last

    def test_ready_succeeds_once_a_deck_is_loaded(self, tmp_path):
        table = Table(tmp_path)
        table.run("join_lobby", 1)
        table.give_deck(1)
        table.run("ready_lobby", 1)

        seat = next(s for s in table.current().seats
                    if s.user_id == table.users[1].id)
        assert seat.ready

    def test_ready_off_reverses_it(self, tmp_path):
        table = Table(tmp_path)
        table.run("join_lobby", 1)
        table.give_deck(1)
        table.run("ready_lobby", 1)
        table.run("ready_lobby", 1, "off")

        seat = next(s for s in table.current().seats
                    if s.user_id == table.users[1].id)
        assert not seat.ready

    def test_the_start_gate_holds_until_every_seat_is_ready(self, tmp_path):
        """ADVERSE CONTROL — the gate that stops a game beginning without
        every seat's deck.

        Asserted on `can_start` rather than by driving `!startgame`: actually
        starting builds decks through the engine, which this harness
        deliberately does not construct. The gate is the logic; construction
        is the cube smoke's job."""
        table = Table(tmp_path)
        for i in (1, 2, 3):
            table.run("join_lobby", i)
        for i in (0, 1, 2):
            table.give_deck(i)
            table.run("ready_lobby", i)

        assert not table.current().can_start, \
            "three of four ready is not ready"

        table.give_deck(3)
        table.run("ready_lobby", 3)

        assert table.current().can_start


class TestLobbyOwnership:

    def test_only_the_host_can_cancel(self, tmp_path):
        table = Table(tmp_path)
        table.run("join_lobby", 1)

        ctx = table.run("cancel_lobby", 1)

        assert table.current() is not None, \
            "a non-host cancelled the host's lobby"
        assert ctx.last

    def test_the_host_can_cancel(self, tmp_path):
        table = Table(tmp_path)
        table.run("join_lobby", 1)

        table.run("cancel_lobby", 0)

        assert table.current() is None

    def test_the_lobby_view_lists_every_seat(self, tmp_path):
        table = Table(tmp_path)
        for i in (1, 2, 3):
            table.run("join_lobby", i)

        ctx = table.run("show_lobby", 0)

        body = " ".join(ctx.sent)
        for user in table.users:
            assert user.display_name in body, \
                "%s missing from the lobby view" % user.display_name


class TestDurability:

    def test_a_lobby_survives_a_store_reload(self, tmp_path):
        """Restart safety: LobbyStore is the only record that a game is being
        assembled, so a bot restart mid-gathering must not lose the seats."""
        table = Table(tmp_path)
        for i in (1, 2, 3):
            table.run("join_lobby", i)
        table.give_deck(2)
        table.run("ready_lobby", 2)

        reloaded = LobbyStore(tmp_path / "lobbies").get(table.channel.id)

        assert reloaded is not None
        assert len(reloaded.seats) == 4
        assert sorted(s.seat_id for s in reloaded.seats) == [0, 1, 2, 3]
        ready = [s for s in reloaded.seats if s.ready]
        assert [s.user_id for s in ready] == [table.users[2].id]

    def test_changing_a_deck_invalidates_readiness(self, tmp_path):
        """Otherwise a seat could start with a deck nobody validated."""
        table = Table(tmp_path)
        table.run("join_lobby", 1)
        table.give_deck(1)
        table.run("ready_lobby", 1)

        seat = next(s for s in table.current().seats
                    if s.user_id == table.users[1].id)
        assert seat.ready
        table.current().bind_deck(table.users[1].id,
                                  {"name": "Other", "cards": []})

        seat = next(s for s in table.current().seats
                    if s.user_id == table.users[1].id)
        assert not seat.ready, \
            "a new deck must be re-readied, not inherit the old ready flag"


# --------------------------------------------------------------------------
# Q-H — the turn handoff, which needs a real engine rather than a lobby
# --------------------------------------------------------------------------

class Seated:
    """A started multiplayer game driven through the real command callbacks.

    The lobby harness above deliberately stops short of building an engine.
    This one goes one step further because the handoff bug lives in what the
    command does with `engine.end_turn()`'s result, which a lobby cannot show.
    GAMES_DIR is redirected at a tmp dir: a real GameEngine loads and writes
    the live save directory otherwise, which a test must never touch.
    """

    def __init__(self, tmp_path, monkeypatch, names=("Alice", "Bob", "Carol"),
                 claude_seats=()):
        from types import SimpleNamespace

        from mtg.constants import Phase
        from mtg.engine import GameEngine
        from mtg.models import Card, GameState, Player

        monkeypatch.setattr(GameEngine, "GAMES_DIR", str(tmp_path / "games"))

        players = []
        for i, name in enumerate(names):
            if i in claude_seats:
                players.append(Player(name=name, user_id=None, is_claude=True,
                                      life=40))
            else:
                players.append(Player(name=name, user_id=1000 + i, life=40))
        game = GameState(thread_id=77, format="commander", players=players)
        game.turn_number = 1
        game.active_player_index = 0
        game.set_phase(Phase.MAIN1, via="test")
        for p in players:
            for k in range(6):
                p.library.append(Card(
                    name="Forest%d" % k, id="%s_l%d" % (p.name, k),
                    type_line="Basic Land — Forest"))

        engine = GameEngine(None)
        engine.games[77] = game
        game._rules_engine = engine.rules

        cog = object.__new__(MTGGameCog)
        cog.engine = engine
        cog.display = SimpleNamespace(create_game_embed=lambda g: "<embed>")
        cog._get_game = lambda _ctx: game

        self.cog, self.game, self.engine = cog, game, engine
        self.users = [FakeAuthor(1000 + i, n) for i, n in enumerate(names)]

    def end_turn_as(self, index):
        ctx = FakeCtx(self.users[index], FakeChannel(77))
        asyncio.run(MTGGameCog.end_turn.callback(self.cog, ctx))
        return ctx


class TestTurnHandoff:
    """end_turn() leaves the next seat at UNTAP; somebody has to advance it.

    The 2-player path advances the incoming human INSIDE the Claude branch, so
    human-vs-Claude always worked. A human handing to another human — every
    multiplayer lobby, and any 2-player human game — hit neither branch, so the
    incoming seat skipped its untap step, its upkeep triggers and its DRAW STEP
    until it manually passed three times, with nothing saying so.

    The same file already had the correct shape: `!discard`'s turn-continuation
    path carries an `else: # Human's turn — draw and announce` doing exactly
    these three advances. `!turn` simply lacked it, which is why this is an
    omission rather than a design choice.
    """

    def test_a_human_handoff_reaches_main1_and_draws(self, tmp_path, monkeypatch):
        from mtg.constants import Phase

        table = Seated(tmp_path, monkeypatch)
        bob = table.game.players[1]
        before_hand, before_lib = len(bob.hand), len(bob.library)

        ctx = table.end_turn_as(0)

        assert table.game.active_player.name == "Bob"
        assert table.game.phase == Phase.MAIN1, (
            "the incoming seat was left at %s" % table.game.phase.name)
        assert len(bob.hand) == before_hand + 1, "Bob skipped his draw step"
        assert len(bob.library) == before_lib - 1
        joined = "\n".join(ctx.sent)
        assert "Draw Step" in joined, "the draw must be visible, not silent"
        assert "Main Phase 1" in joined

    def test_the_untouched_seats_do_not_draw(self, tmp_path, monkeypatch):
        """Adverse control: only the incoming seat advances."""
        table = Seated(tmp_path, monkeypatch)
        carol = table.game.players[2]
        before = len(carol.hand)
        table.end_turn_as(0)
        assert len(carol.hand) == before

    def test_the_claude_branch_does_not_double_advance(self, tmp_path,
                                                       monkeypatch):
        """The new branch is an `elif`, so a Claude seat must still take the
        old path exactly once — advancing twice would overshoot into combat."""
        from types import SimpleNamespace

        from mtg.constants import Phase

        table = Seated(tmp_path, monkeypatch, claude_seats=(1,))

        async def _no_actions(_game):
            return []

        table.engine.execute_claude_turn = _no_actions
        table.engine.claude_ai = SimpleNamespace(last_error=None)
        table.cog._sanitize_action_bullets = lambda a: a

        table.end_turn_as(0)

        # Claude took its turn and handed back to the next human, who is at
        # MAIN1 — not past it.
        assert table.game.active_player.name == "Carol"
        assert table.game.phase == Phase.MAIN1

    def test_an_ended_game_is_not_advanced(self, tmp_path, monkeypatch):
        """`elif not game.ended` — a finished game must not draw anybody a
        card on the way out (CR 104.2a)."""
        from mtg.constants import Phase

        table = Seated(tmp_path, monkeypatch)
        bob = table.game.players[1]

        real_end_turn = table.engine.end_turn

        def _ending_end_turn(game):
            out = real_end_turn(game)
            game.ended = True
            return out

        table.engine.end_turn = _ending_end_turn
        before = len(bob.hand)
        table.end_turn_as(0)

        assert len(bob.hand) == before
        assert table.game.phase == Phase.UNTAP


# --------------------------------------------------------------------------
# Q-H — commander damage where players actually look
# --------------------------------------------------------------------------

class TestCommanderDamageUX:
    """The embed is what !state and every turn handoff SEND to Discord.

    It carried life, poison, hand size and battlefield but not the second loss
    condition, so the only place a tally appeared was the text board and a
    one-off line when a commander connected. The June 11 audit had already
    found players learning the 21 rule from their own death message; this is
    the persistent half of that fix, and it matters most at four seats, where
    a player tracks up to three different commanders at once.
    """

    @staticmethod
    def _game(fmt="commander"):
        from mtg.models import GameState, Player
        players = [Player(name=n, user_id=1000 + i, life=40)
                   for i, n in enumerate(["Alice", "Bob", "Carol", "Dave"])]
        return GameState(thread_id=5, format=fmt, players=players)

    def test_the_embed_shows_every_commanders_tally(self):
        from mtg.display import GameDisplay

        game = self._game()
        game.players[0].commander_damage = {"Atraxa": 7, "Korvold": 12}
        embed = GameDisplay.create_game_embed(game)

        alice = embed.fields[0].value
        assert "Commander damage" in alice, "the embed omitted the tally"
        assert "Atraxa 7/21" in alice
        assert "Korvold 12/21" in alice, (
            "each commander is tracked separately (CR 903.10a)")

    def test_the_threshold_is_shown_not_just_the_number(self):
        """/21 is the point: a bare number says nothing about how close the
        player is to losing."""
        from mtg.display import GameDisplay

        game = self._game()
        game.players[1].commander_damage = {"Atraxa": 20}
        assert GameDisplay.commander_damage_summary(
            game, game.players[1]) == "Atraxa 20/21"

    def test_a_player_with_no_commander_damage_gets_no_line(self):
        from mtg.display import GameDisplay

        game = self._game()
        game.players[0].commander_damage = {"Atraxa": 5}
        embed = GameDisplay.create_game_embed(game)
        assert "Commander damage" in embed.fields[0].value
        assert "Commander damage" not in embed.fields[1].value

    def test_non_commander_formats_show_nothing(self):
        """Brawl and Oathbreaker keep command zones WITHOUT the 21-damage loss
        condition (Aug 14), so the tally must not appear there either."""
        from mtg.display import GameDisplay

        for fmt in ("modern", "brawl", "oathbreaker"):
            game = self._game(fmt)
            game.players[0].commander_damage = {"Atraxa": 9}
            assert GameDisplay.commander_damage_summary(
                game, game.players[0]) is None, fmt
            assert "Commander damage" not in GameDisplay.create_game_embed(
                game).fields[0].value, fmt

    def test_the_text_board_and_the_embed_cannot_drift(self):
        """Both renderers go through one helper. A mutant that reverts either
        to its own formatting shows up as a mismatch here."""
        from mtg.display import GameDisplay

        game = self._game()
        game.players[2].commander_damage = {"Korvold": 14}
        summary = GameDisplay.commander_damage_summary(game, game.players[2])

        assert summary in GameDisplay.format_board_state(game)
        assert summary in GameDisplay.create_game_embed(game).fields[2].value

    def test_legacy_integer_keys_still_render(self):
        """Saves written before the per-commander keying (Aug 14) hold seat
        indices. They must degrade to the player name, not crash or print a
        bare index."""
        from mtg.display import GameDisplay

        game = self._game()
        game.players[0].commander_damage = {1: 6}
        assert GameDisplay.commander_damage_summary(
            game, game.players[0]) == "Bob 6/21"


# --------------------------------------------------------------------------
# Q-H — !attack can name a planeswalker (CR 508.1a)
# --------------------------------------------------------------------------

class TestAttackingAPlaneswalker:
    """`at <defender>` already existed in multiplayer; it just could not name
    a planeswalker, and 2-player had no `at` at all — which is where it
    matters most, since the walker-heavy decks are played two-handed.

    The engine-side routing lives in
    tests/test_aug23_qh_planeswalker_defenders.py; these are the command pins.
    """

    @staticmethod
    def _walker(table, seat, name="Jace, the Mind Sculptor"):
        from mtg.models import Card
        walker = Card(name=name, id="pw_%d" % seat,
                      type_line="Legendary Planeswalker — Jace")
        walker.loyalty_counters = 4
        table.game.players[seat].battlefield.append(walker)
        return walker

    @staticmethod
    def _bear(table, seat, name="Grizzly Bears"):
        from mtg.constants import Phase
        from mtg.models import Card
        bear = Card(name=name, id="bear_%d" % seat, type_line="Creature — Bear",
                    power="2", toughness="2", summoning_sick=False)
        table.game.players[seat].battlefield.append(bear)
        # !attack is only legal in the declare-attackers step; the harness
        # starts at MAIN1 because the handoff tests need it there.
        table.game.set_phase(Phase.DECLARE_ATTACKERS, via="test")
        return bear

    def test_attacking_at_a_planeswalker_records_the_assignment(
            self, tmp_path, monkeypatch):
        table = Seated(tmp_path, monkeypatch)
        bear = self._bear(table, 0)
        walker = self._walker(table, 1)

        ctx = FakeCtx(table.users[0], FakeChannel(77))
        asyncio.run(MTGGameCog.declare_attackers.callback(
            table.cog, ctx, creatures="Grizzly Bears at Jace, the Mind Sculptor"))

        assert bear.attacking is True
        assert bear.attacking_planeswalker == walker.id
        # The walker's CONTROLLER stays the defending seat, which is what keeps
        # blocking and attack taxes working.
        assert bear.attacking_player == 1
        assert any("at Jace" in m for m in ctx.sent), ctx.sent

    def test_attacking_at_a_seat_leaves_no_walker_assignment(
            self, tmp_path, monkeypatch):
        """Adverse control: the ordinary seat attack must be unchanged."""
        table = Seated(tmp_path, monkeypatch)
        bear = self._bear(table, 0)
        self._walker(table, 1)

        ctx = FakeCtx(table.users[0], FakeChannel(77))
        asyncio.run(MTGGameCog.declare_attackers.callback(
            table.cog, ctx, creatures="Grizzly Bears at Bob"))

        assert bear.attacking is True
        assert bear.attacking_planeswalker is None
        assert bear.attacking_player == 1

    def test_a_seat_name_wins_over_a_walker_of_the_same_name(
            self, tmp_path, monkeypatch):
        """Seats resolve first, so a player cannot be shadowed by a card."""
        table = Seated(tmp_path, monkeypatch, names=("Alice", "Jace", "Carol"))
        bear = self._bear(table, 0)
        self._walker(table, 2, name="Jace")

        ctx = FakeCtx(table.users[0], FakeChannel(77))
        asyncio.run(MTGGameCog.declare_attackers.callback(
            table.cog, ctx, creatures="Grizzly Bears at Jace"))

        assert bear.attacking_player == 1, "the SEAT named Jace"
        assert bear.attacking_planeswalker is None

    def test_an_ambiguous_walker_name_is_refused_not_guessed(
            self, tmp_path, monkeypatch):
        table = Seated(tmp_path, monkeypatch)
        bear = self._bear(table, 0)
        self._walker(table, 1)
        self._walker(table, 2)

        ctx = FakeCtx(table.users[0], FakeChannel(77))
        asyncio.run(MTGGameCog.declare_attackers.callback(
            table.cog, ctx, creatures="Grizzly Bears at Jace, the Mind Sculptor"))

        assert bear.attacking is False
        assert any("More than one opponent" in m for m in ctx.sent), ctx.sent

    def test_your_own_walker_is_not_a_legal_target(self, tmp_path, monkeypatch):
        """You attack a planeswalker an OPPONENT controls (CR 508.1a)."""
        table = Seated(tmp_path, monkeypatch)
        bear = self._bear(table, 0)
        self._walker(table, 0, name="My Own Jace")

        ctx = FakeCtx(table.users[0], FakeChannel(77))
        asyncio.run(MTGGameCog.declare_attackers.callback(
            table.cog, ctx, creatures="Grizzly Bears at My Own Jace"))

        assert bear.attacking is False
        assert bear.attacking_planeswalker is None

    def test_two_player_at_names_a_walker(self, tmp_path, monkeypatch):
        """`at` is NEW in 2-player, and it is the only way to attack a walker
        in the format the walker-heavy decks are actually played in."""
        table = Seated(tmp_path, monkeypatch, names=("Alice", "Bob"))
        bear = self._bear(table, 0)
        walker = self._walker(table, 1)

        ctx = FakeCtx(table.users[0], FakeChannel(77))
        asyncio.run(MTGGameCog.declare_attackers.callback(
            table.cog, ctx, creatures="Grizzly Bears at Jace, the Mind Sculptor"))

        assert bear.attacking is True
        assert bear.attacking_planeswalker == walker.id
        assert bear.attacking_player == 1

    def test_two_player_without_at_still_hits_the_face(self, tmp_path,
                                                       monkeypatch):
        """Adverse control: `at` is optional here, so the bare form is
        unchanged."""
        table = Seated(tmp_path, monkeypatch, names=("Alice", "Bob"))
        bear = self._bear(table, 0)
        self._walker(table, 1)

        ctx = FakeCtx(table.users[0], FakeChannel(77))
        asyncio.run(MTGGameCog.declare_attackers.callback(
            table.cog, ctx, creatures="Grizzly Bears"))

        assert bear.attacking is True
        assert bear.attacking_planeswalker is None
        assert bear.attacking_player == 1

    def test_two_player_refuses_your_own_walker(self, tmp_path, monkeypatch):
        """THE DECISIVE PATH for the resolver's own-seat skip.

        The multiplayer branch has a separate `defender_idx == player_idx`
        guard; 2-player has none, so here the skip inside
        _resolve_attack_target is the only thing between a player and
        attacking their own planeswalker (CR 508.1a).
        """
        table = Seated(tmp_path, monkeypatch, names=("Alice", "Bob"))
        bear = self._bear(table, 0)
        self._walker(table, 0, name="My Own Jace")

        ctx = FakeCtx(table.users[0], FakeChannel(77))
        asyncio.run(MTGGameCog.declare_attackers.callback(
            table.cog, ctx, creatures="Grizzly Bears at My Own Jace"))

        assert bear.attacking is False
        assert bear.attacking_planeswalker is None
        assert bear.attacking_player is None
