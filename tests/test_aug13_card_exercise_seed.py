"""Headless pins for the transient autoplay card-exercise hook.

The exact twenty names are the Aug-13 1,211-card matrix complement which had
never appeared in a live autoplay corpus. These tests prove every one can be
placed into a real opening hand without network access or persistent deck/cache
mutation. Card-specific rules pins remain separate: seeding is evidence that a
live game can reach a card, not evidence that its Oracle text resolved correctly.
"""

import copy

import pytest

from conftest import _make_card, _make_game
from mtg.autoplay import (apply_autoplay_card_seeds,
                          extract_seed_card_flags)
from mtg.deck_loader import DeckLoader


NEVER_LIVE_MATRIX_CARDS = [
    "Apex of Power",
    "Badlands",
    "Bloodthirsty Adversary",
    "Cathar's Crusade",
    "Fireblade Artist",
    "Goblin Goliath",
    "Kozilek, the Great Distortion",
    "Last Chance",
    "Lurrus of the Dream-Den",
    "Moonveil Dragon",
    "Mox Tantalite",
    "Platoon Dispenser",
    "Quakebringer",
    "Reflecting Pool",
    "Star of Extinction",
    "Stormscale Scion",
    "Takenuma, Abandoned Mire",
    "Tropical Island",
    "Underground Sea",
    "Volcanic Island",
]


def _opening_game():
    game = _make_game()
    for player_index, player in enumerate(game.players):
        player.hand = [
            _make_card(
                f"Seat {player_index} filler {idx}",
                type_line="Basic Land — Forest", power=None, toughness=None,
            )
            for idx in range(7)
        ]
    return game


@pytest.mark.parametrize("card_name", NEVER_LIVE_MATRIX_CARDS)
def test_each_never_live_card_is_cache_seedable_and_game_local(card_name):
    loader = DeckLoader()
    cache_before = copy.deepcopy(loader.card_cache[card_name.lower()])
    game = _opening_game()

    results = apply_autoplay_card_seeds(game, loader, [card_name])

    assert results == [{
        "card": loader.card_cache[card_name.lower()].get("name", card_name),
        "player": "Rick",
        "status": "seeded-from-cache",
    }]
    canonical_name = results[0]["card"]
    seeded = next(c for c in game.players[0].hand
                  if c.name.lower() == canonical_name.lower())
    assert seeded._autoplay_seeded is True
    assert len(game.players[0].hand) == 7
    assert len(game._seed_replaced_cards) == 1
    assert loader.card_cache[card_name.lower()] == cache_before
    assert seeded.keywords is not loader.card_cache[card_name.lower()].get("keywords")

    # A second game is a new object graph: neither the injected card nor the
    # evidence lists survive. This is the operational answer to "does the
    # seeding go away after the seeded test?"
    next_game = _opening_game()
    assert all(c.name.lower() != canonical_name.lower()
               for p in next_game.players for c in p.hand)
    assert next_game._card_seed_results == []
    assert next_game._seed_replaced_cards == []


def test_parser_supports_quotes_repetition_and_seat_prefixes():
    args, seeds = extract_seed_card_flags([
        "commander", "cascade", "mythic",
        "--seed-card", "p1:Apex of Power",
        "--seed-card", "p2:Last Chance",
        "--claude",
    ])
    assert args == ["commander", "cascade", "mythic", "--claude"]
    assert seeds == ["p1:Apex of Power", "p2:Last Chance"]

    with pytest.raises(ValueError, match="requires a card name"):
        extract_seed_card_flags(["commander", "--seed-card"])


def test_existing_library_card_is_swapped_without_changing_inventory():
    loader = DeckLoader()
    game = _opening_game()
    player = game.players[1]
    target = _make_card("Last Chance", type_line="Sorcery", power=None,
                        toughness=None)
    player.library = [target, _make_card("Mountain", type_line="Basic Land")]
    inventory_before = sorted(c.id for c in player.hand + player.library)

    result = apply_autoplay_card_seeds(
        game, loader, ["p2:Last Chance"])[0]

    assert result["status"] == "seeded-from-library"
    assert target in player.hand
    assert sorted(c.id for c in player.hand + player.library) == inventory_before
    assert game._seed_replaced_cards == []


@pytest.mark.parametrize("zone_name", ["command_zone", "companion_zone"])
def test_legal_special_zone_is_not_bypassed(zone_name):
    loader = DeckLoader()
    game = _opening_game()
    player = game.players[0]
    target = _make_card("Lurrus of the Dream-Den")
    getattr(player, zone_name).append(target)
    hand_before = list(player.hand)

    result = apply_autoplay_card_seeds(
        game, loader, ["p1:Lurrus of the Dream-Den"])[0]

    assert result["status"] == f"legal-{zone_name.replace('_', '-')}"
    assert getattr(player, zone_name) == [target]
    assert player.hand == hand_before
    assert target._autoplay_seeded is False


def test_cache_miss_is_truthful_and_does_not_replace_a_card():
    loader = DeckLoader()
    game = _opening_game()
    before = list(game.players[0].hand)

    result = apply_autoplay_card_seeds(
        game, loader, ["Definitely Not a Real Card"])[0]

    assert result["status"] == "cache-miss"
    assert game.players[0].hand == before
    assert game._seed_replaced_cards == []
