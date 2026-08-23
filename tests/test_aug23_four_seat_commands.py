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
