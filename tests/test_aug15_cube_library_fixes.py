"""Regressions from the Aug-15 strict cube FFA smoke (sha 4e77257)."""

import asyncio
from types import SimpleNamespace

from mtg.engine import GameEngine
from rules.effect_templates import (
    build_game_context,
    has_residual_clause_beyond_library_look,
)


RISEN_REEF_ORACLE = (
    "Whenever Risen Reef or another Elemental enters the battlefield under "
    "your control, look at the top card of your library. If it's a land "
    "card, you may put it onto the battlefield tapped. If you don't put the "
    "card onto the battlefield, put it into your hand."
)


def _resolve_template(lib, rules, game, player, opponent, card):
    ctx = build_game_context(game, player, opponent, card=card)
    actions, description = lib.resolve_spell(
        card.name, card.oracle_text, player.name, opponent.name, ctx)
    assert actions is not None, description
    messages = [rules._execute_action_on_state(game, action)
                for action in actions if action.get("action") != "no_action"]
    return actions, messages


def test_compound_library_look_is_not_a_pure_reorder():
    assert has_residual_clause_beyond_library_look(RISEN_REEF_ORACLE)
    assert not has_residual_clause_beyond_library_look(
        "Look at the top three cards of your library, then put them back in "
        "any order.")


def test_judge_shortcut_calls_resolver_for_compound_but_not_pure_reorder(
        make_game, rules):
    from mtg.judge import resolve_effect

    game = make_game()
    calls = []

    class Messages:
        def create(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(
                content=[SimpleNamespace(
                    type="text",
                    text=('{' + '"explanation":"test",'
                          '"actions":[{"action":"no_action",'
                          '"reason":"test"}]}'),
                )],
                usage=None,
            )

    rules.client = SimpleNamespace(messages=Messages())
    asyncio.run(resolve_effect(
        rules, game, RISEN_REEF_ORACLE,
        source_card="Synthetic Compound", controller="Rick"))
    assert len(calls) == 1, "compound state mutation was short-circuited"

    messages, actions = asyncio.run(resolve_effect(
        rules, game,
        "Look at the top three cards of your library, then put them back in "
        "any order.",
        source_card="Synthetic Reorder", controller="Rick"))
    assert len(calls) == 1, "pure reorder should retain the cheap shortcut"
    assert not actions
    assert any("library reordering" in message for message in messages)


def test_risen_reef_self_entry_moves_exactly_one_top_land_tapped(
        make_game, make_card):
    game = make_game()
    rick, _claude = game.players
    engine = GameEngine(None)
    engine.rules.engine_ref = engine
    reef = make_card(
        "Risen Reef", type_line="Creature — Elemental",
        oracle_text=RISEN_REEF_ORACLE, power="1", toughness="1")
    top_land = make_card("Top Forest", type_line="Basic Land — Forest")
    control = make_card("Control Spell", type_line="Sorcery", cmc=3,
                        power=None, toughness=None)
    rick.library = [reef, top_land, control]

    engine.rules._execute_action_on_state(game, {
        "action": "move_card", "card": reef.name,
        "from_zone": "library", "to_zone": "battlefield",
        "player": rick.name,
    })

    assert reef in rick.battlefield
    assert top_land in rick.battlefield
    assert top_land.tapped
    assert rick.library == [control]


def test_risen_reef_later_elemental_and_opponent_control_gate(
        make_game, make_card):
    game = make_game()
    rick, claude = game.players
    engine = GameEngine(None)
    reef = make_card(
        "Risen Reef", type_line="Creature — Elemental",
        oracle_text=RISEN_REEF_ORACLE, power="1", toughness="1")
    own_elemental = make_card("Air Elemental", type_line="Creature — Elemental")
    opponent_elemental = make_card(
        "Fire Elemental", type_line="Creature — Elemental")
    first = make_card("First Prize", type_line="Instant", cmc=4,
                      power=None, toughness=None)
    control = make_card("Control", type_line="Instant", cmc=1,
                        power=None, toughness=None)
    rick.battlefield.extend([reef, own_elemental])
    rick.library = [first, control]

    engine._check_creature_etb_triggers_sync(game, rick, own_elemental)
    assert first in rick.hand
    assert rick.library == [control]

    claude.battlefield.append(opponent_elemental)
    engine._check_creature_etb_triggers_sync(game, claude, opponent_elemental)
    assert rick.library == [control]
    assert control not in rick.hand


def test_fact_or_fiction_preserves_five_objects_and_both_destinations(
        make_game, make_card, rules, lib):
    game = make_game()
    rick, claude = game.players
    source = make_card(
        "Fact or Fiction", type_line="Instant", cmc=4,
        oracle_text=("Reveal the top five cards of your library. An opponent "
                     "separates those cards into two piles. Put one pile into "
                     "your hand and the other into your graveyard."),
        power=None, toughness=None)
    revealed = [
        make_card("Duplicate", type_line="Instant", cmc=1,
                  power=None, toughness=None),
        make_card("Duplicate", type_line="Sorcery", cmc=6,
                  power=None, toughness=None),
        make_card("Forest", type_line="Basic Land — Forest"),
        make_card("Bear", cmc=2),
        make_card("Dragon", type_line="Creature — Dragon", cmc=7),
    ]
    tail = make_card("Tail", type_line="Instant", cmc=1,
                     power=None, toughness=None)
    rick.library = [*revealed, tail]

    actions, _ = _resolve_template(
        lib, rules, game, rick, claude, source)

    assert [a["action"] for a in actions] == ["fact_or_fiction"]
    assert rick.library == [tail]
    assert rick.hand and rick.graveyard
    assert {id(card) for card in rick.hand + rick.graveyard} == {
        id(card) for card in revealed}


def test_dig_through_time_selects_two_and_bottoms_the_other_five(
        make_game, make_card, rules, lib):
    game = make_game()
    rick, claude = game.players
    source = make_card(
        "Dig Through Time", type_line="Instant", cmc=8,
        oracle_text=("Delve. Look at the top seven cards of your library. "
                     "Put two of them into your hand and the rest on the "
                     "bottom of your library in any order."),
        power=None, toughness=None)
    top = [make_card(f"Choice {i}", type_line="Sorcery", cmc=i,
                     power=None, toughness=None) for i in range(1, 8)]
    tail = make_card("Original Tail", type_line="Instant", cmc=1,
                     power=None, toughness=None)
    rick.library = [*top, tail]

    actions, _ = _resolve_template(
        lib, rules, game, rick, claude, source)

    assert [a["action"] for a in actions] == ["select_cards_from_top"]
    assert len(rick.hand) == 2
    assert rick.library[0] is tail
    assert len(rick.library) == 6
    assert not rick.graveyard
    assert {id(card) for card in rick.hand + rick.library[1:]} == {
        id(card) for card in top}


def test_dig_short_library_moves_up_to_two_without_losing_cards(
        make_game, make_card, rules, lib):
    game = make_game()
    rick, claude = game.players
    source = make_card("Dig Through Time", type_line="Instant", cmc=8,
                       oracle_text="Look at the top seven cards.",
                       power=None, toughness=None)
    only = make_card("Only Card", type_line="Sorcery", cmc=2,
                     power=None, toughness=None)
    rick.library = [only]

    _resolve_template(lib, rules, game, rick, claude, source)

    assert rick.hand == [only]
    assert not rick.library


def test_long_term_plans_shuffles_then_places_choice_third_from_top(
        make_game, make_card, rules, lib):
    game = make_game()
    rick, claude = game.players
    source = make_card(
        "Long-Term Plans", type_line="Instant", cmc=3,
        oracle_text=("Search your library for a card, shuffle your library, "
                     "then put that card third from the top."),
        power=None, toughness=None)
    target = make_card("Wanted", type_line="Sorcery", cmc=9,
                       power=None, toughness=None)
    others = [make_card(f"Other {i}", type_line="Instant", cmc=i,
                        power=None, toughness=None) for i in range(5)]
    rick.library = [target, *others]
    ctx = build_game_context(game, rick, claude, card=source)
    actions, _ = lib.resolve_spell(
        source.name, source.oracle_text, rick.name, claude.name, ctx)
    actions[0]["card_name"] = target.name

    rules._execute_action_on_state(game, actions[0])

    assert rick.library[2] is target
    assert target not in rick.hand
    assert {id(card) for card in rick.library} == {
        id(target), *(id(card) for card in others)}


def test_halimar_depths_reorders_real_library_and_empty_is_safe(
        make_game, make_card):
    game = make_game()
    rick, _claude = game.players
    engine = GameEngine(None)
    for i in range(4):
        rick.battlefield.append(
            make_card(f"Land {i}", type_line="Basic Land — Island"))
    depths = make_card(
        "Halimar Depths", type_line="Land",
        oracle_text=("Halimar Depths enters tapped. When Halimar Depths "
                     "enters, look at the top three cards of your library, "
                     "then put them back in any order."))
    top_land = make_card("Top Land", type_line="Basic Land — Island")
    high = make_card("High Spell", type_line="Sorcery", cmc=6,
                     power=None, toughness=None)
    low = make_card("Low Spell", type_line="Instant", cmc=2,
                    power=None, toughness=None)
    rick.library = [top_land, high, low]

    messages = engine._handle_land_etb(game, rick, depths)

    assert rick.library == [low, high, top_land]
    assert any("reorders the top 3" in message for message in messages)

    rick.library.clear()
    messages = engine._handle_land_etb(game, rick, depths)
    assert any("library is empty" in message for message in messages)
