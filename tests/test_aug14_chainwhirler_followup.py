"""Regression pins for the live Aug-14 Goblin Chainwhirler cube finding."""

from conftest import _make_card
from mtg.models import GameState, Player
from mtg.rules_engine import RulesEngine
from rules.effect_templates import get_effect_library


CHAINWHIRLER_ORACLE = (
    "First strike\n"
    "When Goblin Chainwhirler enters, it deals 1 damage to each opponent "
    "and each creature and planeswalker they control."
)


def _game(player_count):
    players = [
        Player(name=chr(ord("A") + index), life=20,
               seat_id=index, user_id=81400 + index)
        for index in range(player_count)
    ]
    return GameState(
        thread_id=81450 + player_count,
        format="limited",
        players=players,
        active_player_index=0,
        experimental_ffa=player_count > 2,
    )


def _chainwhirler_actions(game):
    controller = game.players[0]
    opponent = game.players[1]
    actions, description = get_effect_library().resolve_etb(
        "Goblin Chainwhirler", CHAINWHIRLER_ORACLE,
        controller.name, opponent.name, {"_game": game},
    )
    assert description.startswith("When Goblin Chainwhirler enters")
    assert actions == [{
        "action": "damage_each_opponent_board",
        "amount": 1,
        "controller": controller.name,
        "source": "Goblin Chainwhirler",
    }]
    return actions


def test_chainwhirler_damages_duel_opponent_creatures_and_planeswalker():
    game = _game(2)
    rules = RulesEngine(None)
    controller, opponent = game.players
    source = _make_card(
        "Goblin Chainwhirler", owner_index=0,
        mana_cost="{R}{R}{R}", power="3", toughness="3",
        oracle_text=CHAINWHIRLER_ORACLE,
    )
    own_one_one = _make_card(
        "Own Soldier", owner_index=0, power="1", toughness="1")
    # Two duplicate-name 1/1s prove the bulk action uses stable IDs rather
    # than repeatedly damaging the first matching object.
    tokens = [
        _make_card("Soldier", owner_index=1, power="1", toughness="1")
        for _ in range(2)
    ]
    for token in tokens:
        token.is_token = True
    hexproof_bear = _make_card(
        "Hexproof Bear", owner_index=1, power="2", toughness="2",
        oracle_text="Hexproof")
    walker = _make_card(
        "Test Walker", owner_index=1,
        type_line="Legendary Planeswalker — Test", power=None,
        toughness=None)
    walker.loyalty_counters = 2
    controller.battlefield.extend([source, own_one_one])
    opponent.battlefield.extend([*tokens, hexproof_bear, walker])

    message = rules._execute_action_on_state(
        game, _chainwhirler_actions(game)[0])

    assert opponent.life == 19
    assert own_one_one.damage_marked == 0
    assert [token.damage_marked for token in tokens] == [1, 1]
    assert hexproof_bear.damage_marked == 1, (
        "the printed each-effect does not target and must ignore hexproof")
    assert walker.loyalty_counters == 1
    assert message.count("1 damage to **Soldier**") == 2
    assert "Goblin Chainwhirler" in message

    rules.process_state_based_actions(game)
    assert all(token not in opponent.battlefield for token in tokens)
    assert all(token not in opponent.graveyard for token in tokens), (
        "tokens cease to exist after reaching the graveyard")
    assert hexproof_bear in opponent.battlefield


def test_chainwhirler_fans_out_to_living_opponents_only():
    game = _game(4)
    rules = RulesEngine(None)
    controller = game.players[0]
    source = _make_card(
        "Goblin Chainwhirler", owner_index=0,
        mana_cost="{R}{R}{R}", power="3", toughness="3",
        oracle_text=CHAINWHIRLER_ORACLE,
    )
    controller.battlefield.append(source)
    living_creatures = []
    for index in (1, 2):
        creature = _make_card(
            f"Victim {index}", owner_index=index,
            power="1", toughness="1")
        game.players[index].battlefield.append(creature)
        living_creatures.append(creature)
    game.players[3].eliminated = True
    departed_creature = _make_card(
        "Departed Victim", owner_index=3, power="1", toughness="1")
    game.players[3].battlefield.append(departed_creature)

    rules._execute_action_on_state(game, _chainwhirler_actions(game)[0])

    assert [player.life for player in game.players] == [20, 19, 19, 20]
    assert [card.damage_marked for card in living_creatures] == [1, 1]
    assert departed_creature.damage_marked == 0
