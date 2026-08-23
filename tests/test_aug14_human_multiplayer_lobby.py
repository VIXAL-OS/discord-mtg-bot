import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from mtg.cog import MTGGameCog
from mtg.constants import Phase
from mtg.engine import GameEngine
from mtg.lobby import GameLobby, LobbyStore
from mtg.models import Card, GameState, Player


def _deck(name="Deck"):
    return {"name": name, "cards": [{"name": "Forest", "quantity": 60}]}


def _game_with_humans(count=3):
    return GameState(
        thread_id=991,
        format="commander",
        players=[
            Player(name=f"Player {index + 1}", user_id=index + 1,
                   seat_id=index, is_claude=False, life=40,
                   has_kept_hand=True)
            for index in range(count)
        ],
        turn_number=1,
        active_player_index=0,
        phase=Phase.DECLARE_ATTACKERS,
    )


def test_lobby_round_trip_keeps_stable_seats_and_decks(tmp_path):
    store = LobbyStore(tmp_path)
    lobby = GameLobby(
        thread_id=123,
        guild_id=456,
        owner_user_id=10,
        format_name="Commander",
        max_players=4,
    )
    host = lobby.add_user(10, "Same Name", _deck("Host"))
    departing = lobby.add_user(20, "Departing", _deck("Old"))
    third = lobby.add_user(30, "Same Name")
    host.ready = True
    lobby.remove_user(departing.user_id)
    replacement = lobby.add_user(40, "Replacement", _deck("New"))
    lobby.bind_deck(30, _deck("Third"))
    third.ready = True
    store.save(lobby)

    restored = LobbyStore(tmp_path).get(123)

    assert restored is not None
    assert [seat.seat_id for seat in restored.seats] == [0, 2, 3]
    assert replacement.seat_id == 3
    assert restored.seat_for_user(10).ready is True
    # Loading/changing a deck invalidates the prior readiness acknowledgement.
    assert restored.seat_for_user(30).ready is True
    restored.bind_deck(30, _deck("Changed"))
    assert restored.seat_for_user(30).ready is False
    assert restored.seat_for_user(30).deck_data["name"] == "Changed"


def test_lobby_rejects_duplicate_users_and_overfill():
    lobby = GameLobby(1, None, 1, "modern", 3)
    first = lobby.add_user(1, "One", _deck())
    assert lobby.add_user(1, "Renamed") is first
    lobby.add_user(2, "Two", _deck())
    lobby.add_user(3, "Three", _deck())
    with pytest.raises(ValueError, match="full"):
        lobby.add_user(4, "Four", _deck())


def test_game_create_command_opens_and_persists_lobby(tmp_path):
    thread = SimpleNamespace(id=888)
    channel = SimpleNamespace(create_thread=AsyncMock(return_value=thread))
    ctx = SimpleNamespace(
        channel=channel,
        guild=SimpleNamespace(id=999),
        author=SimpleNamespace(id=10, display_name="Host"),
        send=AsyncMock(),
    )
    cog = object.__new__(MTGGameCog)
    cog.lobbies = LobbyStore(tmp_path)
    cog.player_decks = {10: _deck("Host Deck")}
    cog._thread_send = AsyncMock()

    asyncio.run(MTGGameCog.start_game.callback(
        cog, ctx, opponent="create", format="commander", seats=3))

    lobby = cog.lobbies.get(888)
    assert lobby is not None
    assert lobby.owner_user_id == 10
    assert lobby.max_players == 3
    assert lobby.seat_for_user(10).deck_data["name"] == "Host Deck"
    assert (tmp_path / "888.json").exists()
    cog._thread_send.assert_awaited_once()


def test_player_and_combat_pregame_state_survive_save_round_trip():
    game = _game_with_humans()
    game.players[0].mulligans_taken = 2
    game.players[0].has_kept_hand = False
    game.waiting_for_human_blocks = True
    game.opening_hands_pending = True
    game.combat_defenders_done = [1]

    restored = GameState.from_dict(game.to_dict())

    assert restored.players[0].mulligans_taken == 2
    assert restored.players[0].has_kept_hand is False
    assert restored.waiting_for_human_blocks is True
    assert restored.opening_hands_pending is True
    assert restored.combat_defenders_done == [1]


def test_create_game_from_decks_preserves_lobby_seat_ids():
    engine = object.__new__(GameEngine)
    engine.rules = SimpleNamespace()
    engine.games = {}
    engine.save_game = lambda game: None

    async def load(player, deck_data, owner_index):
        card = Card(name=f"Forest {owner_index}", type_line="Basic Land — Forest")
        card.owner_index = owner_index
        player.library = [card]
        player.deck_name = deck_data["name"]

    engine._load_player_deck = load
    specs = [
        {"name": "A", "user_id": 10, "seat_id": 0, "deck_data": _deck("A")},
        {"name": "B", "user_id": 20, "seat_id": 2, "deck_data": _deck("B")},
        {"name": "C", "user_id": 30, "seat_id": 3, "deck_data": _deck("C")},
    ]

    game = asyncio.run(engine.create_game_from_decks(777, specs, "commander"))

    assert [player.seat_id for player in game.players] == [0, 2, 3]
    assert [player.user_id for player in game.players] == [10, 20, 30]
    assert [player.life for player in game.players] == [40, 40, 40]
    assert [player.library[0].owner_index for player in game.players] == [0, 1, 2]


def test_defender_resolution_prefers_user_id_and_stable_seat():
    game = _game_with_humans()
    assert MTGGameCog._resolve_defending_seat(game, "<@!2>")[0] == 1
    assert MTGGameCog._resolve_defending_seat(game, "seat 3")[0] == 2
    assert MTGGameCog._resolve_defending_seat(game, "Player 2")[0] == 1
    assert MTGGameCog._resolve_defending_seat(game, "seat 9")[0] is None


def test_multiplayer_attack_command_assigns_each_defender_once():
    game = _game_with_humans()
    a = Card(name="Ragavan", type_line="Creature — Monkey Pirate",
             power="2", toughness="1")
    b = Card(name="Goblin Guide", type_line="Creature — Goblin Scout",
             power="2", toughness="2")
    game.players[0].battlefield = [a, b]

    rules = SimpleNamespace(
        can_attack_with=lambda *_: (True, "OK"),
        pay_attack_tax=lambda *_: (True, ""),
        log_event=lambda *_: None,
    )
    engine = SimpleNamespace(
        rules=rules,
        tap_permanent=lambda card: setattr(card, "tapped", True),
        process_attack_triggers=lambda *_: [],
        check_state_based_actions=lambda *_: [],
        save_game=lambda *_: None,
    )
    cog = object.__new__(MTGGameCog)
    cog.engine = engine
    cog._get_game = lambda _ctx: game
    cog._snapshot_for_undo = lambda *_: None
    ctx = SimpleNamespace(
        author=SimpleNamespace(id=1),
        send=AsyncMock(),
    )

    asyncio.run(MTGGameCog.declare_attackers.callback(
        cog, ctx,
        creatures="Ragavan at <@2>; Goblin Guide at <@3>",
    ))

    assert game.attackers == [a.id, b.id]
    assert a.attacking_player == 1
    assert b.attacking_player == 2
    assert game.waiting_for_human_blocks is True
    assert game.combat_defenders_done == []


def test_each_attacked_defender_must_finalize_before_damage():
    game = _game_with_humans()
    first = Card(name="First", type_line="Creature", power="1", toughness="1")
    second = Card(name="Second", type_line="Creature", power="1", toughness="1")
    first.attacking = second.attacking = True
    first.attacking_player = 1
    second.attacking_player = 2
    game.players[0].battlefield = [first, second]
    game.attackers = [first.id, second.id]
    game.waiting_for_human_blocks = True

    engine = SimpleNamespace(
        save_game=lambda *_: None,
        _combat_priority_round=AsyncMock(),
    )
    cog = object.__new__(MTGGameCog)
    cog.engine = engine
    cog._resolve_combat = AsyncMock()
    ctx = SimpleNamespace(send=AsyncMock())

    asyncio.run(cog._finish_defender_blocks(ctx, game, 1))
    assert game.combat_defenders_done == [1]
    cog._resolve_combat.assert_not_awaited()

    asyncio.run(cog._finish_defender_blocks(ctx, game, 2))
    assert game.waiting_for_human_blocks is False
    cog._resolve_combat.assert_awaited_once_with(ctx, game)
