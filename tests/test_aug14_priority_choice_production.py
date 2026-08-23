"""Q-F: production multiplayer priority, reconnect, and choice UX."""

import asyncio
import json
from datetime import datetime, timedelta
from types import SimpleNamespace

from mtg.choices import (
    choice_views_for, create_choice, submit_choice, wait_for_choice,
)
from mtg.combat import make_replacement_callback
from mtg.cog import MTGGameCog
from mtg.engine import GameEngine
from mtg.models import Card, GameState, Player
from rules.priority import PriorityAction, PrioritySystem
from rules.replacement import (
    EventType, GameEvent, ReplacementEffect, ReplacementEngine,
)


def _game():
    return GameState(
        thread_id=8142,
        format="limited",
        players=[
            Player(name=name, user_id=2000 + index, seat_id=index, life=20)
            for index, name in enumerate(("A", "B", "C", "D"))
        ],
        active_player_index=0,
        turn_number=3,
        experimental_ffa=True,
    )


def _effect(effect_id, source, *, multiplier=2.0, controller="A"):
    return ReplacementEffect(
        id=effect_id,
        source_name=source,
        source_id=f"{effect_id}-source",
        controller=controller,
        replaces_event=EventType.DAMAGE,
        condition_text="damage would be dealt",
        replacement_type="multiply",
        multiply_amount=multiplier,
    )


def test_priority_round_trip_preserves_exact_apnap_window_and_presence():
    async def exercise():
        priority = PrioritySystem(
            ["A", "B", "C", "D"], auto_pass_seconds=60,
            reconnect_grace_seconds=90)
        await priority.player_action("A", PriorityAction.pass_priority())
        priority.mark_disconnected("B")
        payload = priority.to_dict()
        json.dumps(payload)
        priority._cancel_pass_timer()

        restored = PrioritySystem(
            ["A", "B", "C", "D"], auto_pass_seconds=60)
        restored.restore_state(payload)
        assert restored.priority_holder == "B"
        assert restored._passes_in_succession == ["A"]
        assert restored.get_state()["connected"]["B"] is False
        assert restored.get_state()["deadline"] is not None

    asyncio.run(exercise())


def test_expired_reconnect_deadline_passes_only_saved_holder():
    async def exercise():
        priority = PrioritySystem(["A", "B", "C", "D"], auto_pass_seconds=60)
        payload = priority.to_dict()
        payload["priority_holder"] = "B"
        payload["passes_in_succession"] = ["A"]
        payload["connected"]["B"] = False
        payload["connected"]["C"] = False
        payload["pass_deadline"] = (
            datetime.now() - timedelta(seconds=1)).isoformat()
        restored = PrioritySystem(["A", "B", "C", "D"], auto_pass_seconds=60)
        restored.restore_state(payload)
        await restored.resume()
        await asyncio.sleep(0.02)
        assert restored.priority_holder == "C"
        assert restored._passes_in_succession == ["A", "B"]
        assert (restored._pass_deadline - datetime.now()).total_seconds() > 100
        restored._cancel_pass_timer()

    asyncio.run(exercise())


def test_game_save_rehydrates_priority_state_without_runtime_objects():
    async def exercise():
        game = _game()
        engine = GameEngine(None)
        engine.setup_stack(game, auto_pass_seconds=0)
        game.combat_priority_window = "after attackers declared"
        game._priority_system.combat_window = True
        await game._priority_system.player_action(
            "seat:0", PriorityAction.pass_priority())
        payload = game.to_dict()
        json.dumps(payload)
        restored = GameState.from_dict(payload)
        assert restored._priority_system is None
        engine.setup_stack(restored, auto_pass_seconds=0)
        await asyncio.sleep(0)
        assert restored._priority_system.priority_holder == "seat:1"
        assert restored._priority_system._passes_in_succession == ["seat:0"]
        assert restored.combat_priority_window == "after attackers declared"
        assert restored._priority_system.combat_window is True

    asyncio.run(exercise())


def test_private_simultaneous_answers_stay_sealed_until_all_commit():
    async def exercise():
        game = _game()
        record = create_choice(
            game,
            choice_type="secret_vote",
            chooser_indices=[0, 1],
            options_by_player=["Sun", "Moon"],
            private=True,
            simultaneous=True,
            timeout_seconds=5,
        )
        json.dumps(game.to_dict())
        assert choice_views_for(game, 0)[0]["options"][0]["label"] == "Sun"
        assert choice_views_for(game, 2)[0]["options"] is None

        first = submit_choice(game, 0, 1, record["choice_id"])
        assert first["complete"] is False
        b_view = choice_views_for(game, 1)[0]
        assert "response" not in b_view
        assert b_view["responded"] is False

        second = submit_choice(game, 1, 0, record["choice_id"])
        assert second["complete"] is True
        result = await wait_for_choice(game, record["choice_id"])
        assert result == {0: "Moon", 1: "Sun"}
        assert record["choice_id"] not in game.pending_choices

    asyncio.run(exercise())


def test_replacement_prompt_is_private_and_returns_exact_effect():
    async def exercise():
        game = _game()
        direct_messages = []
        shared_messages = []

        class Channel:
            async def send(self, message):
                shared_messages.append(message)

        async def private_send(player_index, message):
            assert player_index == 1
            direct_messages.append(message)

        effects = [_effect("double", "Furnace"),
                   _effect("halve", "Gisela", multiplier=0.5)]
        callback = make_replacement_callback(
            None, game, Channel(), private_send=private_send,
            timeout_seconds=5)
        task = asyncio.create_task(callback("B", effects))
        await asyncio.sleep(0)
        record = next(iter(game.pending_choices.values()))
        assert "Furnace" in direct_messages[0]
        assert "Furnace" not in shared_messages[0]
        assert "Gisela" not in shared_messages[0]
        result = submit_choice(game, 1, 1, record["choice_id"])
        assert result["success"]
        assert await task is effects[1]

    asyncio.run(exercise())


def test_replacement_order_belongs_to_affected_object_controller():
    async def exercise():
        game = _game()
        permanent = Card(
            name="Target", id="affected", type_line="Creature",
            power="2", toughness="2")
        game.players[2].battlefield.append(permanent)
        event = GameEvent(
            event_type=EventType.DAMAGE,
            affected_object=permanent.id,
            source_controller="A",
            amount=3,
        )
        seen = []

        async def choose(chooser, effects):
            seen.append(chooser)
            return effects[0]

        effects = [_effect("one", "One"), _effect("two", "Two")]
        await ReplacementEngine()._choose_effect(event, effects, game, choose)
        assert seen == ["C"]

    asyncio.run(exercise())


def test_choice_timeout_uses_deterministic_legal_fallback():
    async def exercise():
        game = _game()
        record = create_choice(
            game,
            choice_type="private_mode",
            chooser_indices=[3],
            options_by_player=["First", "Second"],
            timeout_seconds=0.001,
        )
        assert await wait_for_choice(game, record["choice_id"]) == "First"
        assert record["timed_out"] is True

    asyncio.run(exercise())


def test_public_and_restored_choice_uses_serialized_value_without_runtime():
    async def exercise():
        game = _game()
        record = create_choice(
            game,
            choice_type="public_vote",
            chooser_indices=[0],
            options_by_player=[
                {"label": "Alpha", "value": "a"},
                {"label": "Beta", "value": "b"},
            ],
            private=False,
        )
        assert choice_views_for(game, 3)[0]["options"][1]["label"] == "Beta"
        restored = GameState.from_dict(game.to_dict())
        result = submit_choice(restored, 0, 1, record["choice_id"])
        assert result["success"] is True
        assert result["result"] == "b"

    asyncio.run(exercise())


def test_discord_pass_command_authorizes_priority_holder_not_active_player():
    async def exercise():
        game = _game()
        game.stack_enabled = True
        game.stack = [SimpleNamespace()]
        game._priority_system = PrioritySystem(
            ["A", "B", "C", "D"], auto_pass_seconds=0)
        game._priority_system.priority_holder = "B"
        saved = []
        sent = []
        cog = object.__new__(MTGGameCog)
        cog._get_game = lambda _ctx: game
        cog.engine = SimpleNamespace(save_game=lambda saved_game: saved.append(saved_game))
        ctx = SimpleNamespace(
            author=SimpleNamespace(id=game.players[1].user_id),
            send=lambda message: _append_async(sent, message),
        )

        await MTGGameCog.pass_priority.callback(cog, ctx)

        assert game._priority_system.priority_holder == "C"
        assert game._priority_system._passes_in_succession == ["B"]
        assert saved == [game]
        assert "passes" in sent[0]

    asyncio.run(exercise())


async def _append_async(items, value):
    items.append(value)
