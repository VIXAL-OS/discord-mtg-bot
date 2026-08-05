"""Regressions from the Aug. 5 recency-gap audit."""

import asyncio

from mtg.engine import GameEngine
from mtg.helpers import compute_cost_increase
from mtg.models import FormatValidator
from mtg.spells import _advance_sagas, cast_spell_async
from mtg.triggers import (
    _check_day_night_and_werewolf_transforms,
    _check_upkeep_triggers_sync,
    _spell_matches_cast_trigger,
)
from rules.effect_templates import build_game_context, get_effect_library
from rules.targeting_helpers import _validate_player_target_for_action


def _land(make_card, name="Swamp", type_line="Basic Land - Swamp"):
    return make_card(
        name, type_line=type_line, oracle_text="", power=None, toughness=None)


def test_bontus_monument_drains_and_gains(make_game, make_card):
    game = make_game()
    rick, claude = game.players
    monument = make_card(
        "Bontu's Monument", type_line="Legendary Artifact",
        oracle_text=("Black creature spells you cast cost {1} less to cast. "
                     "Whenever you cast a creature spell, each opponent loses "
                     "1 life and you gain 1 life."),
        power=None, toughness=None)
    rick.battlefield.append(monument)
    creature = make_card("Grizzly Bears", type_line="Creature - Bear")

    asyncio.run(GameEngine(None)._check_cast_triggers(game, rick, creature))

    assert rick.life == 41
    assert claude.life == 39
    assert claude.life_lost_this_turn == 1


def test_bontus_monument_ignores_devotion_god_that_is_not_a_creature(
        make_game, make_card):
    game = make_game()
    rick = game.players[0]
    erebos = make_card(
        "Erebos, God of the Dead", mana_cost="{3}{B}",
        type_line="Legendary Enchantment Creature - God",
        oracle_text=("Indestructible\nAs long as your devotion to black is less "
                     "than five, Erebos isn't a creature."))
    rick.battlefield.append(erebos)
    sentence = ("whenever you cast a creature spell, each opponent loses 1 "
                "life and you gain 1 life")

    assert not _spell_matches_cast_trigger(
        None, sentence, erebos, caster=rick, game=game)

    rick.battlefield.extend([
        make_card("Phyrexian Arena", mana_cost="{1}{B}{B}",
                  type_line="Enchantment", power=None, toughness=None),
        make_card("Dauthi Voidwalker", mana_cost="{B}{B}",
                  type_line="Creature - Dauthi Rogue"),
    ])
    assert _spell_matches_cast_trigger(
        None, sentence, erebos, caster=rick, game=game)


def test_abominable_treefolk_counts_snow_permanents(make_game, make_card):
    game = make_game()
    rick = game.players[0]
    treefolk = make_card(
        "Abominable Treefolk", type_line="Snow Creature - Treefolk",
        oracle_text=("Abominable Treefolk's power and toughness are each equal "
                     "to the number of snow permanents you control."),
        power="*", toughness="*")
    snow_land = _land(
        make_card, "Snow-Covered Forest", "Basic Snow Land - Forest")
    ordinary_land = _land(make_card, "Forest", "Basic Land - Forest")
    rick.battlefield.extend([treefolk, snow_land, ordinary_land])

    assert treefolk.get_effective_power(game) == 2
    assert treefolk.get_effective_toughness(game) == 2


def test_life_from_the_loam_rejects_a_player_target_before_cast(
        make_game, make_card):
    game = make_game()
    rick, claude = game.players
    loam = make_card(
        "Life from the Loam", type_line="Sorcery", mana_cost="{1}{G}", cmc=2,
        oracle_text=("Return up to three target land cards from your graveyard "
                     "to your hand. Dredge 3."),
        power=None, toughness=None)

    legal, reason = _validate_player_target_for_action(
        game, claude, loam, rick.name)

    assert not legal
    assert reason


def test_kemba_creates_one_cat_per_attached_equipment(make_game, make_card):
    game = make_game()
    rick, claude = game.players
    kemba = make_card(
        "Kemba, Kha Regent", type_line="Legendary Creature - Cat Cleric",
        oracle_text=("At the beginning of your upkeep, create a 2/2 white Cat "
                     "creature token for each Equipment attached to Kemba, Kha "
                     "Regent."))
    rick.battlefield.append(kemba)
    lib = get_effect_library()

    empty_actions, _ = lib.resolve_upkeep_trigger(
        kemba.name, kemba.oracle_text, rick.name, claude.name,
        build_game_context(game, rick, claude, card=kemba))
    assert empty_actions == []

    for name in ("Bonesplitter", "Lightning Greaves"):
        equipment = make_card(
            name, type_line="Artifact - Equipment", power=None, toughness=None)
        equipment.attached_to = kemba.id
        kemba.attachments.append(equipment.id)
        rick.battlefield.append(equipment)

    actions, _ = lib.resolve_upkeep_trigger(
        kemba.name, kemba.oracle_text, rick.name, claude.name,
        build_game_context(game, rick, claude, card=kemba))
    assert len(actions) == 1
    assert actions[0]["action"] == "create_token"
    assert actions[0]["count"] == 2


def test_classic_werewolf_transforms_only_once_per_upkeep(make_game, make_card):
    game = make_game()
    game._spells_cast_last_turn = 0
    rick = game.players[0]
    mayor = make_card(
        "Mayor of Avabruck", type_line="Creature - Human Advisor Werewolf",
        oracle_text=("At the beginning of each upkeep, if no spells were cast "
                     "last turn, transform Mayor of Avabruck."),
        has_transform=True, back_face_name="Howlpack Alpha",
        back_face_type_line="Creature - Werewolf",
        back_face_oracle_text=("At the beginning of each upkeep, if a player "
                               "cast two or more spells last turn, transform "
                               "Howlpack Alpha."),
        back_face_power="3", back_face_toughness="3")
    rick.battlefield.append(mayor)
    engine = GameEngine(None)

    messages = _check_day_night_and_werewolf_transforms(engine, game)
    assert len(messages) == 1
    assert mayor.name == "Howlpack Alpha"
    assert mayor.is_transformed

    upkeep_messages, unhandled = _check_upkeep_triggers_sync(engine, game)
    assert mayor.name == "Howlpack Alpha"
    assert mayor.is_transformed
    assert upkeep_messages == []
    assert unhandled == []


def test_command_zone_origin_does_not_trigger_ash_zealot(
        make_game, make_card):
    game = make_game("oathbreaker")
    rick, claude = game.players
    ash = make_card(
        "Ash Zealot", type_line="Creature - Human Warrior",
        oracle_text=("Whenever a player casts a spell from a graveyard, Ash "
                     "Zealot deals 3 damage to that player."))
    claude.battlefield.append(ash)
    oathbreaker = make_card(
        "Test Oathbreaker", type_line="Legendary Planeswalker - Test",
        power=None, toughness=None, is_commander=True)
    rick.battlefield.extend([
        oathbreaker,
        _land(make_card, "Mountain", "Basic Land - Mountain"),
    ])
    signature = make_card(
        "Command Test", type_line="Sorcery", mana_cost="{R}", cmc=1,
        oracle_text="", power=None, toughness=None,
        is_signature_spell=True)
    signature._cast_from_graveyard = True  # stale legacy stamp from an old cast
    signature._cast_from_command_zone = True
    rick.hand.append(signature)  # executors temporarily move command cards here

    ok, cast_message, _ = asyncio.run(
        cast_spell_async(GameEngine(None), game, rick, signature))

    assert ok, cast_message
    assert signature._cast_origin == "command_zone"
    assert rick.life == 20


def test_oathbreaker_signature_spell_is_not_a_second_commander(
        make_card):
    oathbreaker = make_card(
        "Jace, Cunning Castaway", mana_cost="{1}{U}{U}",
        type_line="Legendary Planeswalker - Jace", power=None, toughness=None,
        is_commander=True)
    signature = make_card(
        "Opt", mana_cost="{U}", type_line="Instant", power=None,
        toughness=None, is_signature_spell=True)
    islands = [
        _land(make_card, "Island", "Basic Land - Island") for _ in range(58)
    ]

    valid, issues = FormatValidator.validate_deck(
        [oathbreaker, signature, *islands], "oathbreaker",
        commander=[oathbreaker, signature])

    assert valid, issues


def test_elspeth_tax_starts_at_chapter_two_and_expires(
        make_game, make_card):
    game = make_game()
    rick, claude = game.players
    game.turn_number = 7
    saga = make_card(
        "Elspeth Conquers Death", type_line="Enchantment - Saga",
        oracle_text=("I - Exile target permanent an opponent controls with "
                     "mana value 3 or greater.\n"
                     "II - Noncreature spells your opponents cast cost {2} "
                     "more to cast until your next turn.\n"
                     "III - Return target creature or planeswalker card from "
                     "your graveyard to the battlefield."),
        power=None, toughness=None)
    saga.counters["lore"] = 1
    rick.battlefield.append(saga)
    noncreature = make_card(
        "Open the Vaults", type_line="Sorcery", mana_cost="{4}{W}{W}",
        power=None, toughness=None)
    creature = make_card(
        "Sun Titan", type_line="Creature - Giant", mana_cost="{4}{W}{W}")

    assert compute_cost_increase(game, claude, noncreature)[0] == 0

    _advance_sagas(GameEngine(None), game, rick)

    assert compute_cost_increase(game, rick, noncreature)[0] == 0
    assert compute_cost_increase(game, claude, noncreature)[0] == 2
    assert compute_cost_increase(game, claude, creature)[0] == 0
    game.turn_number = 9
    assert compute_cost_increase(game, claude, noncreature)[0] == 0
    assert game._temporary_cost_increases == []


def test_spell_queller_empty_stack_is_handled_silently(make_game, make_card):
    game = make_game()
    rick = game.players[0]
    rick.battlefield.extend([
        _land(make_card, "Plains", "Basic Land - Plains"),
        _land(make_card, "Island", "Basic Land - Island"),
        _land(make_card, "Island", "Basic Land - Island"),
    ])
    queller = make_card(
        "Spell Queller", mana_cost="{1}{W}{U}", cmc=3,
        type_line="Creature - Spirit", power="2", toughness="3",
        oracle_text=("Flash\nFlying\nWhen Spell Queller enters the battlefield, "
                     "exile target spell with mana value 4 or less until Spell "
                     "Queller leaves the battlefield."))
    rick.hand.append(queller)

    ok, _, effects = asyncio.run(
        cast_spell_async(GameEngine(None), game, rick, queller))

    assert ok
    assert effects == []
    assert queller in rick.battlefield
