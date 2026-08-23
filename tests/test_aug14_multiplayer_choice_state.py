"""Q-E: stable multiplayer targets and restart-safe pending choices."""

import asyncio
import json
from types import SimpleNamespace

from mtg.actions import execute_action_on_state
from mtg.autoplay import _autoplay_resolve_pending_action
from mtg.claude_player import ClaudePlayer
from mtg.engine import GameEngine
from mtg.helpers import (
    resolve_cast_target,
    resolve_target_choice,
    serialize_target_choice,
    serialize_target_choices,
)
from mtg.models import Card, GameState, Player
from rules.planeswalker import (
    AbilityType, PlaneswalkerAbility, get_legal_planeswalker_targets,
)


def _ffa():
    players = [
        Player(name=name, seat_id=index, user_id=1400 + index, life=20)
        for index, name in enumerate(("A", "B", "C", "D"))
    ]
    return GameState(
        thread_id=814, format="limited", players=players,
        active_player_index=0, turn_number=1, experimental_ffa=True)


def _card(name, card_id, *, oracle="", type_line="Creature — Bear"):
    return Card(
        name=name, id=card_id, type_line=type_line, oracle_text=oracle,
        power="2" if "Creature" in type_line else None,
        toughness="2" if "Creature" in type_line else None,
        summoning_sick=False)


def test_exact_player_id_beats_default_and_eliminated_seat_fails_closed():
    game = _ffa()
    bolt = _card(
        "Lightning Bolt", "bolt", oracle="Lightning Bolt deals 3 damage to any target.",
        type_line="Instant")

    assert resolve_cast_target(
        game, game.players[0], bolt, target_player_id=2) is game.players[2]
    assert game.default_opponent_for(game.players[0]) is not game.players[2]

    game.players[2].eliminated = True
    assert resolve_cast_target(
        game, game.players[0], bolt, "C", target_player_id=2) is None
    assert resolve_cast_target(
        game, game.players[0], bolt, target_player_id=1,
        target_card_id="anything") is None


def test_exact_card_id_disambiguates_duplicates_and_wrong_zone_is_stale():
    game = _ffa()
    first = _card("Bear", "bear-b")
    exact = _card("Bear", "bear-c")
    game.players[1].battlefield.append(first)
    game.players[2].battlefield.append(exact)
    removal = _card(
        "Murder", "murder", oracle="Destroy target creature.",
        type_line="Instant")

    assert resolve_cast_target(
        game, game.players[0], removal, target_card_id=exact.id) is exact
    game.players[2].battlefield.remove(exact)
    game.players[2].graveyard.append(exact)
    assert resolve_cast_target(
        game, game.players[0], removal, "Bear",
        target_card_id=exact.id) is None


def test_action_ids_mutate_only_exact_living_target(rules):
    game = _ffa()
    game.players[1].name = "Bot"
    game.players[2].name = "Bot"
    execute_action_on_state(rules, game, {
        "action": "deal_damage", "amount": 3, "target_player_id": 2,
    })
    assert [p.life for p in game.players] == [20, 20, 17, 20]

    first = _card("Bear", "bear-b")
    exact = _card("Bear", "bear-c")
    game.players[1].battlefield.append(first)
    game.players[2].battlefield.append(exact)
    execute_action_on_state(rules, game, {
        "action": "destroy", "card": "Bear", "target_card_id": exact.id,
    })
    assert first in game.players[1].battlefield
    assert exact in game.players[2].graveyard

    before = [p.life for p in game.players]
    game.players[3].eliminated = True
    assert execute_action_on_state(rules, game, {
        "action": "deal_damage", "amount": 9, "target_player_id": 3,
    }) is None
    assert [p.life for p in game.players] == before


def test_pending_choice_is_json_safe_and_rebinds_after_round_trip():
    game = _ffa()
    bear = _card("Bear", "persistent-bear")
    game.players[2].battlefield.append(bear)
    choices = serialize_target_choices(game, [
        (game.players[3], "D"), (bear, "C's Bear")])
    game.pending_action = {
        "type": "planeswalker_target", "card_id": "pw",
        "ability_index": 0, "player_idx": 0, "chooser_player_id": 0,
        "target_choices": choices,
    }

    payload = game.to_dict()
    json.dumps(payload)
    restored = GameState.from_dict(payload)
    assert resolve_target_choice(restored, choices[0]) is restored.players[3]
    rebound = resolve_target_choice(restored, choices[1])
    assert rebound is restored.players[2].battlefield[0]
    assert restored.visible_state(0)["pending_choice"]["card_id"] == "pw"
    assert restored.visible_state(1)["pending_choice"] is None
    json.dumps(restored.visible_state(0))


def test_persisted_choice_does_not_retarget_after_elimination_or_move():
    game = _ffa()
    bear = _card("Bear", "chosen-bear")
    twin = _card("Bear", "other-bear")
    game.players[2].battlefield.extend([bear, twin])
    player_ref = serialize_target_choice(game, game.players[2])
    card_ref = serialize_target_choice(game, bear)

    game.players[2].eliminated = True
    assert resolve_target_choice(game, player_ref) is None
    game.players[2].eliminated = False
    game.players[2].battlefield.remove(bear)
    game.players[2].graveyard.append(bear)
    assert resolve_target_choice(game, card_ref) is None
    assert twin in game.players[2].battlefield


def test_autoplay_pending_planeswalker_rebinds_exact_target_after_restore():
    game = _ffa()
    walker = _card("Test Walker", "walker", type_line="Legendary Planeswalker")
    target = _card("Bear", "target-bear")
    game.players[0].battlefield.append(walker)
    game.players[2].battlefield.append(target)
    game.pending_action = {
        "type": "planeswalker_target", "card_id": walker.id,
        "ability_index": 0, "player_idx": 0, "chooser_player_id": 0,
        "target_choices": [serialize_target_choice(game, target)],
    }
    game = GameState.from_dict(game.to_dict())

    seen = []

    class Manager:
        async def activate(self, game_arg, player, card, ability, targets):
            seen.extend(targets)
            return SimpleNamespace(messages=[])

    class Cog:
        engine = SimpleNamespace(planeswalker_manager=Manager())

        async def _autoplay_send(self, _thread, _message):
            return None

    asyncio.run(_autoplay_resolve_pending_action(Cog(), None, game))
    assert seen == [game.players[2].battlefield[0]]
    assert game.pending_action is None


def test_planeswalker_choice_inventory_excludes_eliminated_seats_and_objects():
    game = _ffa()
    live = _card("Live Bear", "live")
    gone = _card("Gone Bear", "gone")
    game.players[1].battlefield.append(live)
    game.players[2].battlefield.append(gone)
    game.players[2].eliminated = True
    ability = PlaneswalkerAbility(
        index=0, loyalty_cost=1, ability_type=AbilityType.LOYALTY_PLUS,
        text="CARD deals 1 damage to any target.", needs_target=True,
        target_description="any target")

    legal = get_legal_planeswalker_targets(game, game.players[0], ability)
    objects = [target for target, _label in legal]
    assert live in objects
    assert gone not in objects
    assert game.players[2] not in objects


def test_multiplayer_prompt_exposes_ids_without_changing_duel_state():
    game = _ffa()
    bear = _card("Bear", "prompt-bear")
    game.players[2].battlefield.append(bear)
    text = ClaudePlayer(None)._describe_game_state(game, 0)
    assert "player_id=2" in text
    assert "card_id=prompt-bear" in text

    duel = GameState(
        thread_id=815, format="limited",
        players=[Player(name="A", seat_id=0), Player(name="B", seat_id=1)])
    duel.players[1].battlefield.append(_card("Bear", "duel-bear"))
    duel_text = ClaudePlayer(None)._describe_game_state(duel, 0)
    assert "player_id=" not in duel_text
    assert "card_id=" not in duel_text


def test_engine_actor_forwards_exact_player_id_to_cast_target():
    game = _ffa()
    bolt = _card(
        "Lightning Bolt", "actor-bolt",
        oracle="Lightning Bolt deals 3 damage to any target.",
        type_line="Instant")
    game.players[0].hand.append(bolt)
    engine = GameEngine(None)
    seen = []

    async def capture(_game, _player, _card, *, target=None, **_kwargs):
        seen.append(target)
        return True, "cast", []

    engine.cast_spell_async = capture
    asyncio.run(engine._execute_action(game, 0, {
        "type": "cast", "card": "Lightning Bolt", "target_player_id": 2,
    }))
    assert seen == [game.players[2]]
