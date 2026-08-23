"""Regression pins for the live Aug-14 Goblin Shortcutter cube finding."""

from conftest import _make_card
from mtg.engine import GameEngine
from mtg.models import GameState, Player
from mtg.rules_engine import RulesEngine
from rules.effect_templates import build_game_context, get_effect_library


SHORTCUTTER_ORACLE = (
    "When this creature enters, target creature can't block this turn."
)


def _game(player_count=2):
    players = [
        Player(name=chr(ord("A") + index), life=20,
               seat_id=index, user_id=81420 + index)
        for index in range(player_count)
    ]
    return GameState(
        thread_id=81490 + player_count,
        format="limited",
        players=players,
        active_player_index=0,
        experimental_ffa=player_count > 2,
    )


def _resolve_shortcutter(game, explicit_target=None):
    controller = game.players[0]
    source = next(card for card in controller.battlefield
                  if card.name == "Goblin Shortcutter")
    opponent = game.default_opponent_for(controller)
    actions, description = get_effect_library().resolve_etb(
        source.name, SHORTCUTTER_ORACLE, controller.name, opponent.name,
        build_game_context(game, controller, opponent, card=source,
                           explicit_target=explicit_target),
    )
    assert description.endswith("can't block this turn")
    assert len(actions) == 1
    return actions[0]


def test_shortcutter_restricts_one_duel_blocker_and_expires_at_cleanup():
    game = _game()
    rules = RulesEngine(None)
    source = _make_card(
        "Goblin Shortcutter", owner_index=0, power="2", toughness="1",
        oracle_text=SHORTCUTTER_ORACLE)
    chosen = _make_card("Hill Giant", owner_index=1, power="3", toughness="3")
    untouched = _make_card("Runeclaw Bear", owner_index=1,
                           power="2", toughness="2")
    game.players[0].battlefield.append(source)
    game.players[1].battlefield.extend([untouched, chosen])

    action = _resolve_shortcutter(game)
    message = rules._execute_action_on_state(game, action)

    assert action["target_card_id"] == chosen.id
    assert "can't block this turn" in message
    assert not chosen.can_block(game=game)
    assert untouched.can_block(game=game)
    assert source.can_block(game=game)

    GameEngine(None).clear_end_of_turn_effects(game)
    assert chosen.can_block(game=game)


def test_shortcutter_ffa_chooses_one_best_living_opponent_not_every_seat():
    game = _game(4)
    rules = RulesEngine(None)
    source = _make_card(
        "Goblin Shortcutter", owner_index=0, power="2", toughness="1",
        oracle_text=SHORTCUTTER_ORACLE)
    small = _make_card("Small Blocker", owner_index=1, power="1", toughness="4")
    biggest = _make_card("Big Blocker", owner_index=2, power="6", toughness="6")
    departed = _make_card("Departed Blocker", owner_index=3,
                          power="9", toughness="9")
    game.players[0].battlefield.append(source)
    game.players[1].battlefield.append(small)
    game.players[2].battlefield.append(biggest)
    game.players[3].battlefield.append(departed)
    game.players[3].eliminated = True

    action = _resolve_shortcutter(game)
    rules._execute_action_on_state(game, action)

    assert action["target_controller"] == game.players[2].name
    assert not biggest.can_block(game=game)
    assert small.can_block(game=game)
    assert departed.can_block(game=game)
    assert source.can_block(game=game)


def test_shortcutter_preserves_legal_explicit_target_and_avoids_hexproof():
    game = _game(3)
    rules = RulesEngine(None)
    source = _make_card(
        "Goblin Shortcutter", owner_index=0, power="2", toughness="1",
        oracle_text=SHORTCUTTER_ORACLE)
    explicit = _make_card("Explicit Blocker", owner_index=1,
                          power="1", toughness="5")
    hexproof = _make_card("Hexproof Giant", owner_index=2,
                          power="8", toughness="8", keywords=["Hexproof"])
    game.players[0].battlefield.append(source)
    game.players[1].battlefield.append(explicit)
    game.players[2].battlefield.append(hexproof)

    action = _resolve_shortcutter(game, explicit)
    rules._execute_action_on_state(game, action)

    assert action["target_card_id"] == explicit.id
    assert not explicit.can_block(game=game)
    assert hexproof.can_block(game=game)


def test_shortcutter_mandatory_etb_targets_itself_when_no_opponent_is_legal():
    game = _game()
    rules = RulesEngine(None)
    source = _make_card(
        "Goblin Shortcutter", owner_index=0, power="2", toughness="1",
        oracle_text=SHORTCUTTER_ORACLE)
    shrouded = _make_card("Shrouded Blocker", owner_index=1,
                          power="5", toughness="5", keywords=["Shroud"])
    game.players[0].battlefield.append(source)
    game.players[1].battlefield.append(shrouded)

    action = _resolve_shortcutter(game)
    rules._execute_action_on_state(game, action)

    assert action["target_card_id"] == source.id
    assert not source.can_block(game=game)
    assert shrouded.can_block(game=game)


def test_shortcutter_flag_round_trips_during_turn_and_zone_change_clears_it():
    card = _make_card("Goblin Shortcutter", owner_index=0,
                      power="2", toughness="1")
    card.cant_block_this_turn = True
    restored = type(card).from_dict(card.to_dict())
    assert restored.cant_block_this_turn is True

    restored.reset_battlefield_state()
    assert restored.cant_block_this_turn is False
