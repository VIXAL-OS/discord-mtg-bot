"""Q-I durable stack / resolution identity pins."""

import asyncio
import json

import pytest

from mtg.engine import GameEngine
from mtg.models import Card, GameState, Player, StackEntry
from mtg.resolution import ResolutionCoordinator


def _card(name, card_id, **overrides):
    data = dict(
        name=name, id=card_id, type_line="Instant", oracle_text="",
        mana_cost="{U}", cmc=1,
    )
    data.update(overrides)
    return Card(**data)


def _game():
    game = GameState(
        thread_id=608,
        format="limited",
        players=[
            Player(name="Duplicate", user_id=9000 + index,
                   seat_id=index, life=20)
            for index in range(4)
        ],
        experimental_ffa=True,
    )
    # Unit tests do not need filesystem persistence; JSON round trips below
    # are the persistence assertion.
    game.is_autoplay = True
    return game


def test_resolution_round_trip_uses_exact_seat_card_and_stack_ids():
    game = _game()
    first_bear = _card(
        "Bear", "bear-a", type_line="Creature — Bear", power="2",
        toughness="2")
    second_bear = _card(
        "Bear", "bear-b", type_line="Creature — Bear", power="2",
        toughness="2")
    game.players[0].battlefield.append(first_bear)
    game.players[2].battlefield.append(second_bear)
    engine = object()
    coordinator = ResolutionCoordinator.for_game(engine, game)

    bottom_card = _card("Unsummon", "spell-bottom")
    bottom = StackEntry(
        bottom_card, game.players[1].name, 1, target=second_bear)
    game.stack.append(bottom)
    coordinator.register(bottom)

    top_card = _card("Counterspell", "spell-top")
    top = StackEntry(top_card, game.players[3].name, 3,
                     target=bottom_card)
    game.stack.append(top)
    coordinator.register(top)

    player_spell = _card("Target Player", "spell-player")
    player_entry = StackEntry(
        player_spell, game.players[0].name, 0, target=game.players[3])
    game.stack.append(player_entry)
    coordinator.register(player_entry)

    payload = game.to_dict()
    json.dumps(payload)
    restored = GameState.from_dict(payload)

    assert restored.stack[0].target is restored.players[2].battlefield[0]
    assert restored.stack[0].target.id == "bear-b"
    assert restored.stack[1].target is restored.stack[0].card
    assert restored.stack[2].target is restored.players[3]
    assert restored.stack[2].target is not restored.players[0]


def test_stale_exact_target_never_falls_back_to_duplicate_name():
    game = _game()
    gone = _card("Bear", "gone-bear", type_line="Creature — Bear")
    decoy = _card("Bear", "decoy-bear", type_line="Creature — Bear")
    game.players[1].battlefield.append(gone)
    game.players[2].battlefield.append(decoy)
    entry = StackEntry(
        _card("Removal", "removal"), game.players[0].name, 0,
        target=gone)
    game.stack.append(entry)
    ResolutionCoordinator.for_game(object(), game).register(entry)
    game.players[1].battlefield.remove(gone)

    restored = GameState.from_dict(game.to_dict())
    assert restored.stack[0].target is None
    assert restored.players[2].battlefield[0].name == "Bear"


def test_cast_snapshot_and_checkpoint_survive_runtime_destruction():
    game = _game()
    card = _card("Fire // Ice", "split-1")
    card.split_names = ["Fire", "Ice"]
    card.split_costs = ["{1}{R}", "{1}{U}"]
    card.cast_as_split_half = "Ice"
    card._cast_origin = "exile"
    card._x_value = 3
    card._kicked = True
    card._entwined = True
    entry = StackEntry(card, game.players[0].name, 0,
                       target=game.players[1])
    game.stack.append(entry)
    coordinator = ResolutionCoordinator.for_game(object(), game)
    job = coordinator.register(entry, additional_cost=2)
    entry.priority_id = "priority-608"
    job.priority_id = entry.priority_id
    coordinator.transition(entry, "priority_open")

    restored = GameState.from_dict(json.loads(json.dumps(game.to_dict())))
    restored_entry = restored.stack[0]
    restored_job = restored.resolution_jobs[entry.entry_id]
    assert restored_entry is not entry
    assert restored_entry.card is not card
    assert restored_entry.card.cast_as_split_half == "Ice"
    assert restored_entry.card._cast_origin == "exile"
    assert restored_entry.card._x_value == 3
    assert restored_entry.card._kicked is True
    assert restored_entry.card._entwined is True
    assert restored_entry.priority_id == "priority-608"
    assert restored_job.checkpoint == "priority_open"
    assert restored_job.additional_cost == 2


def test_setup_stack_rebinds_runtime_event_for_restored_job():
    async def exercise():
        game = _game()
        entry = StackEntry(
            _card("Opt", "opt"), game.players[0].name, 0,
            target=game.players[1])
        game.stack.append(entry)
        ResolutionCoordinator.for_game(object(), game).register(entry)
        restored = GameState.from_dict(game.to_dict())
        assert restored.stack[0].resolution_event is None

        engine = GameEngine(None)
        engine.setup_stack(restored, auto_pass_seconds=0)
        assert isinstance(restored.stack[0].resolution_event, asyncio.Event)
        assert restored._resolution_coordinator.recoverable_jobs()

    asyncio.run(exercise())


def test_resolution_checkpoint_cannot_move_backwards():
    game = _game()
    entry = StackEntry(
        _card("Opt", "opt-regression"), game.players[0].name, 0)
    game.stack.append(entry)
    coordinator = ResolutionCoordinator.for_game(object(), game)
    coordinator.register(entry)
    coordinator.transition(entry, "resolving")
    with pytest.raises(ValueError, match="checkpoint regression"):
        coordinator.transition(entry, "priority_open")


def test_trigger_entry_and_splice_ids_restore_without_runtime_objects():
    game = _game()
    splice = _card("Glacial Ray", "splice-card")
    game.players[0].hand.append(splice)
    spell = _card("Peer Through Depths", "arcane-spell")
    spell._spliced_cards = [splice]
    spell_entry = StackEntry(spell, game.players[0].name, 0)
    game.stack.append(spell_entry)
    coordinator = ResolutionCoordinator.for_game(object(), game)
    coordinator.register(spell_entry)

    trigger_entry = StackEntry(
        None, game.players[1].name, 1, is_spell=False,
        trigger_source="A source that left", trigger_text="Draw a card.")
    game.stack.append(trigger_entry)
    coordinator.register(trigger_entry)

    restored = GameState.from_dict(game.to_dict())
    assert restored.stack[0].card._spliced_cards == [restored.players[0].hand[0]]
    assert restored.stack[1].is_spell is False
    assert restored.stack[1].trigger_source == "A source that left"
    assert restored.stack[1].card.name == "A source that left"


def test_game_save_is_atomic_and_leaves_no_temporary_file(tmp_path):
    engine = GameEngine(None)
    engine.GAMES_DIR = str(tmp_path)
    game = _game()
    game.thread_id = 12345
    engine.save_game(game)
    saved = tmp_path / "12345.json"
    assert saved.exists()
    assert json.loads(saved.read_text(encoding="utf-8"))["thread_id"] == 12345
    assert list(tmp_path.glob("*.tmp")) == []
