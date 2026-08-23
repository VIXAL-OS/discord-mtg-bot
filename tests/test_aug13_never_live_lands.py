"""Behavioral headless coverage for the five never-live matrix lands."""

import pytest

from conftest import _make_card, _make_game


@pytest.mark.parametrize(("name", "type_line", "oracle", "expected"), [
    ("Badlands", "Land — Swamp Mountain", "", {"B": 1, "R": 1}),
    ("Tropical Island", "Land — Forest Island", "", {"G": 1, "U": 1}),
    ("Underground Sea", "Land — Island Swamp", "", {"B": 1, "U": 1}),
    ("Volcanic Island", "Land — Island Mountain", "", {"R": 1, "U": 1}),
])
def test_original_dual_produces_either_printed_basic_land_type(
        name, type_line, oracle, expected):
    game = _make_game()
    player = game.players[0]
    dual = _make_card(name, type_line=type_line, oracle_text=oracle,
                      power=None, toughness=None)
    player.battlefield.append(dual)

    assert player._get_mana_production(dual) == expected
    assert player.one_tap_mana_total() == 1


def test_reflecting_pool_mirrors_types_but_not_quantity():
    game = _make_game()
    player = game.players[0]
    pool = _make_card(
        "Reflecting Pool", type_line="Land",
        oracle_text=("{T}: Add one mana of any type that a land you control "
                     "could produce."), power=None, toughness=None)
    tropical = _make_card("Tropical Island", type_line="Land — Forest Island",
                          power=None, toughness=None)
    tomb = _make_card("Ancient Tomb", type_line="Land", power=None,
                      toughness=None)
    player.battlefield.extend([pool, tropical, tomb])

    assert player._get_mana_production(pool) == {"C": 1, "G": 1, "U": 1}
    # It mirrors mana TYPES, not Ancient Tomb's two-mana quantity.
    assert player._one_tap_output(player._get_mana_production(pool), pool) == 1


def test_reflecting_pool_alone_or_with_only_another_pool_makes_nothing():
    game = _make_game()
    player = game.players[0]
    first = _make_card("Reflecting Pool", type_line="Land", power=None,
                       toughness=None)
    second = _make_card("Reflecting Pool", type_line="Land", power=None,
                        toughness=None)
    player.battlefield.extend([first, second])

    assert player._get_mana_production(first) == {"C": 0}
    assert player._get_mana_production(second) == {"C": 0}
    assert player.one_tap_mana_total() == 0


def test_untyped_or_dual_still_uses_its_oracle_line():
    game = _make_game()
    player = game.players[0]
    river = _make_card(
        "Underground River", type_line="Land",
        oracle_text="{T}: Add {C}.\n{T}: Add {U} or {B}.",
        power=None, toughness=None)
    player.battlefield.append(river)

    assert player._get_mana_production(river) == {"U": 1, "B": 1, "C": 1}
    assert player.one_tap_mana_total() == 1
