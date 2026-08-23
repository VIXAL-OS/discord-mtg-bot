"""Regression pins for the Aug-14 cube Primeval Titan finding."""

import pytest

from mtg.engine import GameEngine
from mtg.triggers import _check_attack_triggers_sync
from rules.effect_templates import get_effect_library


TITAN_ORACLE = (
    "Trample\n"
    "Whenever Primeval Titan enters or attacks, you may search your library "
    "for up to two land cards, put them onto the battlefield tapped, then shuffle."
)


def _engine(game):
    engine = GameEngine(None)
    game._rules_engine = engine.rules
    engine.rules.engine_ref = engine
    return engine


def test_primeval_attack_template_is_one_bounded_search():
    actions, _ = get_effect_library().resolve_attack_trigger(
        trigger_card_name="Primeval Titan",
        trigger_oracle=TITAN_ORACLE,
        attacking_creature_name="Primeval Titan",
        attacking_creature_power=6,
        controller="Rick",
        opponent="Claude",
    )

    assert actions == [{
        "action": "search_library", "player": "Rick",
        "card_type": "Land", "to_zone": "battlefield",
        "count": 2, "tapped": True, "shuffle": True,
        "reason": "Primeval Titan attack trigger",
    }]


@pytest.mark.parametrize("land_count", [2, 1, 0])
def test_primeval_attack_searches_up_to_two_lands_and_never_queues_tier3(
        make_game, make_card, land_count):
    game = make_game()
    rick = game.players[0]
    titan = make_card(
        "Primeval Titan", mana_cost="{4}{G}{G}", cmc=6,
        type_line="Creature — Giant", oracle_text=TITAN_ORACLE,
        power="6", toughness="6",
    )
    lands = [
        make_card(
            f"Test Land {index}", type_line="Land", power=None,
            toughness=None,
        )
        for index in range(land_count)
    ]
    nonland = make_card(
        "Not a Land", type_line="Sorcery", power=None, toughness=None,
    )
    rick.battlefield.append(titan)
    rick.library.extend([*lands, nonland])

    messages, unhandled = _check_attack_triggers_sync(
        _engine(game), game, titan, rick)

    assert unhandled == []
    assert all(land in rick.battlefield for land in lands)
    assert all(land.tapped for land in lands)
    assert nonland in rick.library
    assert game.players[0].landfall_count_this_turn == land_count
    assert sum("searches library" in message for message in messages) == 1
    assert len([card for card in rick.battlefield
                if card.name.startswith("Test Land")]) == land_count
