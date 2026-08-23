"""Headless contract for the bounded eight-seat cube bracket slice."""

import asyncio
import copy
import json
from types import MethodType, SimpleNamespace

import pytest

from conftest import _make_card
from cube_draft import (
    CubeDraftCog,
    DraftSeat,
    build_all_drafted_decks,
    first_round_pairings,
    format_cube_standings,
    new_bracket_state,
    record_bracket_result,
)


def _seats():
    seats = []
    for seat_index in range(8):
        pool = [
            _make_card(
                f"Seat {seat_index} Spell {card_index}",
                mana_cost="{G}", type_line="Creature — Elf",
                power="2", toughness="2",
            )
            for card_index in range(45)
        ]
        seats.append(DraftSeat(
            seat_index=seat_index, name=f"Seat {seat_index + 1}", pool=pool))
    return seats


def test_all_eight_pools_build_exact_forty_card_finalized_decks():
    seats = _seats()

    build_all_drafted_decks(seats)

    assert [len(seat.deck) for seat in seats] == [40] * 8
    assert all(seat.deck_finalized for seat in seats)
    assert all(len(seat.deck) + len(seat.sideboard) >= 45 for seat in seats)


def test_first_round_has_four_matches_and_every_seat_exactly_once():
    seats = list(reversed(_seats()))

    pairings = first_round_pairings(seats)

    assert [[left.seat_index, right.seat_index]
            for left, right in pairings] == [[0, 1], [2, 3], [4, 5], [6, 7]]
    assert sorted(seat.seat_index for pair in pairings for seat in pair) == list(range(8))


def test_pairing_rejects_wrong_pod_size_and_duplicate_seats():
    with pytest.raises(ValueError, match="requires 8 seats"):
        first_round_pairings(_seats()[:7])

    duplicate = _seats()
    duplicate[-1].seat_index = 0
    with pytest.raises(ValueError, match="must be unique"):
        first_round_pairings(duplicate)


def test_results_distinguish_wins_draws_and_no_contests_and_sort_points():
    bracket = new_bracket_state(123, "Test Cube", "test", _seats())
    record_bracket_result(
        bracket, 1, {"outcome": "win_p1", "winner_seat": 0, "turns": 8})
    record_bracket_result(
        bracket, 2, {"outcome": "timeout", "winner_seat": None, "turns": 60})
    record_bracket_result(
        bracket, 3, {"outcome": "crash", "winner_seat": None,
                     "turns": 2, "error": "boom"})
    record_bracket_result(
        bracket, 4, {"outcome": "win_p2", "winner_seat": 7, "turns": 12})

    rows = {row["seat"]: row for row in bracket["standings"]}
    assert (rows[0]["wins"], rows[1]["losses"], rows[0]["points"]) == (1, 1, 3)
    assert (rows[2]["draws"], rows[3]["draws"], rows[2]["points"]) == (1, 1, 1)
    assert rows[4]["no_contests"] == rows[5]["no_contests"] == 1
    assert rows[4]["played"] == rows[5]["played"] == 0
    assert (rows[7]["wins"], rows[6]["losses"], rows[7]["points"]) == (1, 1, 3)
    assert bracket["status"] == "complete"
    assert [row["seat"] for row in bracket["standings"][:2]] == [0, 7]
    assert "Cube First-Round Standings" in format_cube_standings(bracket)

    with pytest.raises(ValueError, match="already recorded"):
        record_bracket_result(
            bracket, 1, {"outcome": "win_p2", "winner_seat": 1})

    fresh = new_bracket_state(999, "Test Cube", "test", _seats())
    with pytest.raises(ValueError, match="requires winner seat 0"):
        record_bracket_result(
            fresh, 1, {"outcome": "win_p1", "winner_seat": 7})
    assert fresh["pairings"][0]["status"] == "scheduled"
    assert fresh["pairings"][0]["result"] is None
    with pytest.raises(ValueError, match="unsupported cube bracket outcome"):
        record_bracket_result(
            fresh, 1, {"outcome": "typo", "winner_seat": None})
    assert fresh["pairings"][0]["status"] == "scheduled"


def test_first_round_orchestrator_runs_exactly_four_disjoint_two_seat_matches():
    cog = CubeDraftCog.__new__(CubeDraftCog)
    calls = []
    saves = []

    async def fake_match(self, thread, left, right, match_number, max_turns=60):
        calls.append((match_number, left.seat_index, right.seat_index, max_turns))
        return {"outcome": "win_p1", "winner": left.name,
                "winner_seat": left.seat_index, "turns": match_number}

    def fake_save(self, bracket):
        saves.append(copy.deepcopy(bracket))
        return "unused.json"

    cog._run_bracket_match = MethodType(fake_match, cog)
    cog._save_bracket = MethodType(fake_save, cog)
    thread = SimpleNamespace(id=9876)

    bracket, results = asyncio.run(cog._run_cube_first_round(
        thread, "Test Cube", "test", _seats(), max_turns=17))

    assert calls == [
        (1, 0, 1, 17), (2, 2, 3, 17),
        (3, 4, 5, 17), (4, 6, 7, 17),
    ]
    assert len(results) == 4
    # One crash-safe initial schedule, then a checkpoint after every result.
    assert len(saves) == 5
    assert saves[0]["status"] == "scheduled"
    assert saves[-1]["status"] == bracket["status"] == "complete"


def test_match_runner_creates_an_ordinary_two_player_game_and_maps_winner():
    from mtg.engine import GameEngine

    cog = CubeDraftCog.__new__(CubeDraftCog)
    cog.engine = GameEngine(None)
    observed = []

    async def no_mulligan(_hand, _count):
        return False

    async def fake_human_turn(_thread, game, player_index):
        observed.append((len(game.players), player_index, game.format,
                         [len(player.hand) + len(player.library)
                          for player in game.players]))
        game.ended = True
        game.winner = 0
        return ["decisive"]

    async def fake_pending(_thread, _game):
        return None

    async def discard_send(_self, _thread, content=None, embed=None, final=False):
        return None

    cog.engine.claude_ai.decide_mulligan = no_mulligan
    delete_calls = []
    original_delete = cog.engine.delete_game

    def tracked_delete(thread_id, *, preserve_logging=False):
        delete_calls.append((thread_id, preserve_logging))
        original_delete(thread_id, preserve_logging=preserve_logging)

    cog.engine.delete_game = tracked_delete
    cog.game_cog = SimpleNamespace(
        _autoplay_human_turn=fake_human_turn,
        _autoplay_resolve_pending_action=fake_pending,
        display=SimpleNamespace(create_game_embed=lambda _game: "board"),
    )
    cog._autodraft_send = MethodType(discard_send, cog)
    seats = _seats()
    build_all_drafted_decks(seats)
    thread = SimpleNamespace(id=654321)

    result = asyncio.run(cog._run_bracket_match(
        thread, seats[0], seats[1], 1, max_turns=3))

    assert len(observed) == 1
    assert observed[0][0] == 2
    assert observed[0][1] in (0, 1)
    assert observed[0][2:] == ("limited", [40, 40])
    assert result["outcome"] == "win_p1"
    assert result["winner_seat"] == 0
    assert result["winner"] == seats[0].name
    assert thread.id not in cog.engine.games
    assert delete_calls == [(thread.id, True)]


def test_live_bracket_match_posts_card_board_embed_before_ending_turn():
    from mtg.engine import GameEngine

    cog = CubeDraftCog.__new__(CubeDraftCog)
    cog.engine = GameEngine(None)
    sent = []
    board_games = []

    async def no_mulligan(_hand, _count):
        return False

    async def quiet_turn(_thread, _game, _player_index):
        return []

    async def fake_pending(_thread, _game):
        return None

    def create_board(game):
        board_games.append(game)
        return {"cards": [card.name for card in game.players[0].battlefield]}

    async def capture_send(_self, _thread, content=None, embed=None, final=False):
        sent.append((content, embed, final))

    cog.engine.claude_ai.decide_mulligan = no_mulligan
    cog.game_cog = SimpleNamespace(
        _autoplay_human_turn=quiet_turn,
        _autoplay_resolve_pending_action=fake_pending,
        display=SimpleNamespace(create_game_embed=create_board),
    )
    cog._autodraft_send = MethodType(capture_send, cog)
    seats = _seats()
    build_all_drafted_decks(seats)

    result = asyncio.run(cog._run_bracket_match(
        SimpleNamespace(id=7654321), seats[0], seats[1], 1, max_turns=1))

    embeds = [embed for _content, embed, _final in sent if embed is not None]
    assert result["outcome"] == "timeout"
    assert len(board_games) == 1
    assert embeds == [{"cards": []}]


def test_bracket_round_trips_on_disk_and_is_not_loaded_as_a_draft(tmp_path):
    writer = CubeDraftCog.__new__(CubeDraftCog)
    writer.DRAFTS_DIR = str(tmp_path)
    writer.brackets = {}
    bracket = new_bracket_state(456, "Persistent Cube", "test", _seats())

    filepath = writer._save_bracket(bracket)

    with open(filepath, encoding="utf-8") as handle:
        disk = json.load(handle)
    assert disk == bracket
    assert open(filepath, "rb").read().endswith(b"\n")

    reader = CubeDraftCog.__new__(CubeDraftCog)
    reader.DRAFTS_DIR = str(tmp_path)
    reader.brackets = {}
    reader.drafts = {}
    reader._load_all_brackets()
    reader._load_all_drafts()
    assert reader.brackets == {456: bracket}
    assert reader.drafts == {}
