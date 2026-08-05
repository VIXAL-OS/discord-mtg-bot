"""Focused regressions for the Aug. 5 correctness-gap closure.

These are deterministic unit/integration pins.  They deliberately do not run
an autoplay game batch.
"""

import asyncio
import inspect

from mtg.constants import Phase
from mtg.engine import GameEngine
from mtg.legal_actions import castable_entries
from mtg.models import StackEntry
from mtg.triggers import _check_upkeep_triggers_sync
from rules.effect_templates import (
    EffectTemplateLibrary,
    build_game_context,
    get_effect_library,
)


def _engine(game=None):
    engine = GameEngine(None)
    if game is not None:
        game._rules_engine = engine.rules
        engine.rules.engine_ref = engine
    return engine


def _land(make_card, name, color):
    return make_card(
        name,
        type_line=f"Basic Land - {name.split()[0]}",
        oracle_text=f"{{T}}: Add {{{color}}}.",
        power=None,
        toughness=None,
    )


def test_typed_tutor_choice_is_honored_end_to_end(make_game, make_card):
    game = make_game()
    game.phase = Phase.MAIN1
    game.active_player_index = 0
    rick = game.players[0]
    rick.battlefield.extend([
        _land(make_card, "Swamp A", "B"),
        _land(make_card, "Swamp B", "B"),
    ])
    tutor = make_card(
        "Demonic Tutor", type_line="Sorcery", mana_cost="{1}{B}", cmc=2,
        oracle_text=("Search your library for a card, put that card into your "
                     "hand, then shuffle."), power=None, toughness=None)
    requested = make_card(
        "Laboratory Maniac", mana_cost="{2}{U}", cmc=3,
        type_line="Creature - Human Wizard")
    heuristic_pick = make_card(
        "Draco", mana_cost="{16}", cmc=16,
        type_line="Artifact Creature - Dragon")
    rick.hand.append(tutor)
    rick.library = [requested, heuristic_pick]

    asyncio.run(_engine(game)._execute_action(game, 0, {
        "type": "cast", "card": "Demonic Tutor",
        "tutor_card": "Laboratory Maniac",
    }))

    assert requested in rick.hand
    assert requested not in rick.library
    assert heuristic_pick in rick.library
    assert getattr(tutor, "_tutor_card", None) is None


def test_sacrifice_cost_is_not_advertised_without_fodder(
        make_game, make_card):
    game = make_game()
    rick = game.players[0]
    intent = make_card(
        "Diabolic Intent", type_line="Sorcery", mana_cost="{1}{B}", cmc=2,
        oracle_text=("As an additional cost to cast this spell, sacrifice a "
                     "creature. Search your library for a card, put that card "
                     "into your hand, then shuffle."),
        power=None, toughness=None)
    rick.hand.append(intent)
    rick.battlefield.extend([
        _land(make_card, "Swamp A", "B"),
        _land(make_card, "Swamp B", "B"),
    ])
    mana = {"W": 0, "U": 0, "B": 2, "R": 0, "G": 0, "C": 0}

    without_fodder = castable_entries(game, rick, mana, 0, 2)
    assert not any(entry["name"] == intent.name for entry in without_fodder)

    rick.battlefield.append(make_card("Doomed Traveler"))
    with_fodder = castable_entries(game, rick, mana, 0, 2)
    assert any(entry["name"] == intent.name for entry in with_fodder)


def test_primal_command_resolves_modes_one_and_two(
        make_game, make_card, rules):
    game = make_game()
    rick, claude = game.players
    ring = make_card("Sol Ring", type_line="Artifact", cmc=1,
                     power=None, toughness=None)
    claude.battlefield.append(ring)
    ctx = build_game_context(
        game, rick, claude,
        explicit_target=[claude.name, ring])
    ctx["_modes"] = [1, 2]

    actions, _ = get_effect_library().resolve_spell(
        "Primal Command", "Choose two.", rick.name, claude.name,
        game_context=ctx)

    assert [action["action"] for action in actions] == [
        "gain_life", "move_card"]
    for action in actions:
        rules._execute_action_on_state(game, action)
    assert claude.life == 47
    assert ring not in claude.battlefield
    assert claude.library[0] is ring


def test_primal_command_resolves_modes_three_and_four(
        make_game, make_card, rules):
    game = make_game()
    rick, claude = game.players
    dead = make_card("Dead Spell", type_line="Sorcery",
                     power=None, toughness=None)
    creature = make_card("Eternal Witness", cmc=3)
    claude.graveyard.append(dead)
    rick.library.append(creature)
    ctx = build_game_context(
        game, rick, claude, explicit_target=claude.name)
    ctx["_modes"] = [3, 4]

    actions, _ = get_effect_library().resolve_spell(
        "Primal Command", "Choose two.", rick.name, claude.name,
        game_context=ctx)

    assert [action["action"] for action in actions] == [
        "shuffle_graveyard_into_library", "search_library"]
    for action in actions:
        rules._execute_action_on_state(game, action)
    assert claude.graveyard == []
    assert dead in claude.library
    assert creature in rick.hand


def test_dragonmaster_outcast_upkeep_uses_named_template(
        make_game, make_card):
    game = make_game()
    game.active_player_index = 0
    rick = game.players[0]
    outcast = make_card(
        "Dragonmaster Outcast", type_line="Creature - Human Shaman",
        oracle_text=("At the beginning of your upkeep, if you control six or "
                     "more lands, create a 5/5 red Dragon creature token "
                     "with flying."), power="1", toughness="1")
    rick.battlefield.append(outcast)
    rick.battlefield.extend(
        _land(make_card, f"Mountain {index}", "R") for index in range(6))

    messages, unhandled = _check_upkeep_triggers_sync(_engine(game), game)

    dragons = [card for card in rick.battlefield
               if card.name == "Dragon" and getattr(card, "is_token", False)]
    assert unhandled == []
    assert len(dragons) == 1
    assert dragons[0].power == "5" and dragons[0].toughness == "5"
    assert dragons[0].has_keyword("Flying")
    assert any("Dragon" in message for message in messages)


def test_other_missing_conditional_upkeeps_are_event_scoped(
        make_game, make_card):
    game = make_game()
    rick, claude = game.players
    lib = get_effect_library()
    damia = make_card(
        "Damia, Sage of Stone",
        oracle_text=("At the beginning of your upkeep, if you have fewer than "
                     "seven cards in hand, draw cards equal to the difference."))
    rick.hand.extend(make_card(f"Card {i}", type_line="Sorcery",
                               power=None, toughness=None) for i in range(3))
    damia_actions, _ = lib.resolve_upkeep_trigger(
        damia.name, damia.oracle_text, rick.name, claude.name,
        build_game_context(game, rick, claude, card=damia))
    assert damia_actions == [
        {"action": "draw_cards", "player": rick.name, "amount": 4}]

    tyrant = make_card(
        "Hellkite Tyrant", type_line="Creature - Dragon",
        oracle_text=("At the beginning of your upkeep, if you control twenty "
                     "or more artifacts, you win the game."))
    rick.battlefield.extend(
        make_card(f"Artifact {i}", type_line="Artifact",
                  power=None, toughness=None) for i in range(20))
    tyrant_actions, _ = lib.resolve_upkeep_trigger(
        tyrant.name, tyrant.oracle_text, rick.name, claude.name,
        build_game_context(game, rick, claude, card=tyrant))
    assert tyrant_actions == [{
        "action": "win_game", "player": rick.name,
        "reason": "controls twenty or more artifacts at upkeep",
    }]


def test_dedicated_template_marker_has_no_live_source_occurrences():
    source = inspect.getsource(EffectTemplateLibrary)
    stale_marker = "needs a dedicated " + "template"
    assert stale_marker not in source
    for card_name in (
            "dragonmaster outcast", "damia, sage of stone",
            "hellkite tyrant"):
        assert card_name in get_effect_library()._upkeep_templates


def test_amass_grows_existing_army_instead_of_creating_another(
        make_game, make_card, rules):
    game = make_game()
    rick, claude = game.players
    army = make_card(
        "Zombie Army", type_line="Creature - Zombie Army",
        power="0", toughness="0")
    army.is_token = True
    army.counters["+1/+1"] = 2
    rick.battlefield.append(army)
    ctx = build_game_context(game, rick, claude)

    actions, _ = get_effect_library().resolve_spell(
        "Dreadhorde Invasion Test", "Amass 3.", rick.name, claude.name,
        game_context=ctx)
    assert actions == [{
        "action": "add_counters", "card": "Zombie Army",
        "counter_type": "+1/+1", "amount": 3,
    }]
    for action in actions:
        rules._execute_action_on_state(game, action)

    armies = [card for card in rick.battlefield
              if "army" in (card.type_line or "").lower().split()]
    assert armies == [army]
    assert army.counters["+1/+1"] == 5


def test_endrek_sacrifices_itself_when_the_seventh_thrull_enters(
        make_game, make_card, rules):
    game = make_game()
    rick = game.players[0]
    endrek = make_card(
        "Endrek Sahr, Master Breeder",
        type_line="Legendary Creature - Human Wizard")
    rick.battlefield.append(endrek)

    rules._execute_action_on_state(game, {
        "action": "create_token", "player": rick.name,
        "name": "Thrull", "power": 1, "toughness": 1,
        "types": "Creature - Thrull", "count": 7,
    })

    assert endrek not in rick.battlefield
    assert endrek in rick.graveyard
    assert sum(card.name == "Thrull" for card in rick.battlefield) == 7


def test_fierce_guardianship_full_cast_is_free_with_commander(
        make_game, make_card):
    game = make_game()
    game.phase = Phase.MAIN1
    rick, claude = game.players
    commander = make_card(
        "Thrasios, Triton Hero",
        type_line="Legendary Creature - Merfolk Wizard")
    commander.is_commander = True
    fierce = make_card(
        "Fierce Guardianship", type_line="Instant", mana_cost="{2}{U}",
        cmc=3, power=None, toughness=None,
        oracle_text=("If you control a commander, you may cast this spell "
                     "without paying its mana cost. Counter target "
                     "noncreature spell."))
    threat = make_card(
        "Expropriate", type_line="Sorcery", mana_cost="{7}{U}{U}",
        cmc=9, power=None, toughness=None)
    rick.battlefield.append(commander)
    rick.hand.append(fierce)
    threat_entry = StackEntry(
        card=threat, controller_name=claude.name, controller_index=1)
    game.stack.append(threat_entry)

    ok, message, _ = asyncio.run(
        _engine(game).cast_spell_async(game, rick, fierce, target=threat))

    assert ok, message
    assert fierce not in rick.hand
    assert threat_entry.countered
    assert commander in rick.battlefield


def test_autoplay_preserves_ghostly_flickers_two_target_names(
        make_game, make_card):
    from mtg.autoplay import _autoplay_execute_action

    game = make_game()
    game.phase = Phase.MAIN1
    rick = game.players[0]
    flicker = make_card(
        "Ghostly Flicker", type_line="Instant", mana_cost="{2}{U}", cmc=3,
        power=None, toughness=None,
        oracle_text=("Exile two target artifacts, creatures, and/or lands you "
                     "control, then return those cards to the battlefield "
                     "under your control."))
    creature = make_card("Mulldrifter")
    artifact = make_card(
        "Mana Rock", type_line="Artifact", power=None, toughness=None)
    rick.hand.append(flicker)
    rick.battlefield.extend([creature, artifact])
    rick.battlefield.extend(
        _land(make_card, f"Island {index}", "U") for index in range(3))

    class FakeCog:
        def __init__(self):
            self.engine = _engine(game)

        async def _autoplay_send(self, _thread, _message):
            return None

    result = asyncio.run(_autoplay_execute_action(
        FakeCog(), None, game, 0, {
            "type": "cast", "card": "Ghostly Flicker",
            "target": [creature.name, artifact.name],
        }))

    assert result and "Ghostly Flicker" in result
    assert creature in rick.battlefield
    assert artifact in rick.battlefield
    assert flicker in rick.graveyard
