"""Behavioral pins for the cube-slice counter narration cleanup."""

import asyncio

from mtg.engine import GameEngine
from mtg.spells import _await_stack_window


class _ResolvingPriority:
    def __init__(self, game, callback):
        self.game = game
        self.callback = callback
        self.active_player = game.players[game.active_player_index].name
        self.priority_holder = self.active_player
        self._passes_in_succession = []
        self.auto_pass_seconds = 0.01

    async def player_action(self, _player_name, _action):
        entry = self.game.stack[-1]
        self.callback(entry)
        entry.resolution_event.set()
        return {"success": True}

    def remove_stack_entry_by_priority_id(self, _priority_id):
        return None


def _counter_during_window(game, engine, card, callback):
    game.stack_enabled = True
    game._priority_system = _ResolvingPriority(game, callback)
    final, _cast_triggers, _player_idx = asyncio.run(_await_stack_window(
        engine, game, game.players[0], card, None, []))
    assert final is not None
    return final[2]


def test_counter_effect_and_target_cleanup_announce_counter_exactly_once(
        make_game, make_card):
    game = make_game("modern")
    engine = GameEngine(None)
    victim = make_card("Murmuring Mystic", type_line="Creature — Bird Wizard")
    counter_messages = []

    def resolve_counter(_entry):
        counter_messages.append(engine.rules._execute_action_on_state(game, {
            "action": "counter_spell",
            "_source_card_name": "Arcane Denial",
            "_source_oracle": "Counter target spell.",
        }))

    cleanup_messages = _counter_during_window(
        game, engine, victim, resolve_counter)
    visible = [message for message in counter_messages + cleanup_messages if message]

    assert sum("countered" in message.lower() for message in visible) == 1
    assert victim in game.players[0].graveyard
    assert cleanup_messages == []


def test_unannounced_external_counter_keeps_cleanup_narration(
        make_game, make_card):
    game = make_game("modern")
    engine = GameEngine(None)
    victim = make_card("Divination", type_line="Sorcery")

    def mark_without_narrating(entry):
        entry.countered = True

    cleanup_messages = _counter_during_window(
        game, engine, victim, mark_without_narrating)

    assert len(cleanup_messages) == 1
    assert "countered" in cleanup_messages[0].lower()
    assert victim in game.players[0].graveyard


def test_counter_redirect_keeps_destination_without_repeating_counter(
        make_game, make_card):
    game = make_game("modern")
    engine = GameEngine(None)
    victim = make_card("Star of Extinction", type_line="Sorcery")
    counter_messages = []

    def resolve_remand(_entry):
        counter_messages.append(engine.rules._execute_action_on_state(game, {
            "action": "counter_spell",
            "countered_to": "hand",
            "_source_card_name": "Remand",
            "_source_oracle": "Counter target spell.",
        }))

    cleanup_messages = _counter_during_window(
        game, engine, victim, resolve_remand)
    visible = [message for message in counter_messages + cleanup_messages if message]

    assert sum("countered" in message.lower() for message in visible) == 1
    assert any("returns to its owner's hand" in message for message in visible)
    assert victim in game.players[0].hand
    assert victim not in game.players[0].graveyard
