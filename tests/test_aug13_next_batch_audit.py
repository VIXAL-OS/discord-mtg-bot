"""Focused pins for the Aug. 13 next-batch card-targeted findings.

These tests favor the production action/cast entry points.  Each regression
has an adverse control so a broad hard-coded rejection cannot satisfy it.
"""

import asyncio

from mtg.engine import GameEngine
from mtg.spells import _validate_cast
from rules.effect_templates import build_game_context, get_effect_library


def _run(coro):
    return asyncio.run(coro)


def _engine(game):
    engine = GameEngine(None)
    game._rules_engine = engine.rules
    engine.rules.engine_ref = engine
    return engine


def _land(make_card, name="Plains"):
    return make_card(
        name, type_line=f"Basic Land — {name}",
        oracle_text=f"{{T}}: Add {{{name[0] if name != 'Island' else 'U'}}}.",
        power=None, toughness=None)


def test_tooth_tutor_card2_uses_production_coercion_without_name_error(
        make_game, make_card):
    """The real AI executor imports/coerces structured ``tutor_card2``."""
    game = make_game()
    rick, _ = game.players
    game.active_player_index = 0
    tooth = make_card(
        "Tooth and Nail", type_line="Sorcery", mana_cost="{5}{G}{G}",
        oracle_text=("Choose one — Search your library for up to two creature "
                     "cards; or put up to two creature cards from your hand "
                     "onto the battlefield. Entwine {2}."),
        power=None, toughness=None)
    rick.hand.append(tooth)
    engine = _engine(game)
    seen = {}

    async def capture_cast(game_arg, player_arg, card_arg, **kwargs):
        seen["choices"] = list(card_arg._tutor_cards)
        return True, "captured", []

    engine.cast_spell_async = capture_cast
    result = _run(engine._execute_action(game, 0, {
        "type": "cast", "card": "Tooth and Nail",
        "tutor_card": "Craterhoof Behemoth",
        "tutor_card2": {"name": "Avenger of Zendikar"},
    }))
    assert result is not None
    assert seen["choices"] == ["Craterhoof Behemoth", "Avenger of Zendikar"]


KHALNI_ORACLE = (
    "Landfall — Whenever a land you control enters, you may put a quest "
    "counter on this enchantment.\nRemove three quest counters from this "
    "enchantment and sacrifice it: Search your library for up to two basic "
    "land cards, put them onto the battlefield tapped, then shuffle."
)


def _activate_khalni(make_game, make_card, basic_count):
    game = make_game()
    rick, _ = game.players
    game.active_player_index = 0
    khalni = make_card(
        "Khalni Heart Expedition", type_line="Enchantment",
        oracle_text=KHALNI_ORACLE, mana_cost="{1}{G}",
        power=None, toughness=None)
    khalni.counters["quest"] = 3
    rick.battlefield.append(khalni)
    for idx in range(basic_count):
        rick.library.append(_land(make_card, "Forest" if idx else "Island"))
    engine = _engine(game)
    message = _run(engine._execute_action(
        game, 0, {"type": "activate", "permanent": khalni.name,
                  "ability": 0}))
    moved = [c for c in rick.battlefield if c.is_land()]
    return rick, khalni, moved, message


def test_khalni_finds_two_basics_and_puts_each_in_tapped(make_game, make_card):
    rick, khalni, moved, message = _activate_khalni(
        make_game, make_card, basic_count=2)
    assert len(moved) == 2
    assert all(card.tapped for card in moved)
    assert khalni in rick.graveyard and khalni not in rick.battlefield
    assert message.count("puts ") == 2


def test_khalni_gracefully_finds_one_or_zero_basics(make_game, make_card):
    for available in (1, 0):
        rick, khalni, moved, message = _activate_khalni(
            make_game, make_card, basic_count=available)
        assert len(moved) == available
        assert khalni in rick.graveyard
        if available == 0:
            assert "No matching land" in message


DECIMATE_ORACLE = (
    "Destroy target artifact, target creature, target enchantment, and "
    "target land. (You can't cast this spell unless you have legal choices "
    "for all its targets.)"
)


def test_decimate_missing_target_class_is_rejected_before_payment(
        make_game, make_card):
    game = make_game()
    rick, opponent = game.players
    game.active_player_index = 0
    decimate = make_card(
        "Decimate", type_line="Sorcery", oracle_text=DECIMATE_ORACLE,
        mana_cost="{2}{R}{G}", power=None, toughness=None)
    rick.hand.append(decimate)
    for name in ("Mountain", "Forest", "Swamp", "Island"):
        rick.battlefield.append(_land(make_card, name))
    victim = make_card("Target Bear", type_line="Creature — Bear")
    opponent.battlefield.extend([victim, _land(make_card, "Plains")])
    engine = _engine(game)

    before_tapped = [land.tapped for land in rick.battlefield]
    success, reason, _ = _run(engine.cast_spell_async(
        game, rick, decimate, target=victim))
    assert not success
    assert "artifact, creature, enchantment, and land" in reason
    assert decimate in rick.hand
    assert [land.tapped for land in rick.battlefield] == before_tapped


def test_decimate_all_target_classes_emit_and_execute_all_class_action(
        make_game, make_card):
    game = make_game()
    rick, opponent = game.players
    engine = _engine(game)
    decimate = make_card(
        "Decimate", type_line="Sorcery", oracle_text=DECIMATE_ORACLE,
        mana_cost="{2}{R}{G}", power=None, toughness=None)
    victims = [
        make_card("Relic", type_line="Artifact", power=None, toughness=None),
        make_card("Bear", type_line="Creature — Bear"),
        make_card("Omen", type_line="Enchantment", power=None, toughness=None),
        _land(make_card, "Plains"),
    ]
    opponent.battlefield.extend(victims)
    decimate._decimate_target_ids = {
        "artifact": victims[0].id, "creature": victims[1].id,
        "enchantment": victims[2].id, "land": victims[3].id,
    }
    ctx = build_game_context(game, rick, opponent, card=decimate)
    actions, _ = get_effect_library().resolve_spell(
        decimate.name, decimate.oracle_text, rick.name, opponent.name, ctx)
    assert actions == [{"action": "decimate", "player": rick.name,
                        "source": "Decimate",
                        "target_ids": decimate._decimate_target_ids}]
    message = engine.rules._execute_action_on_state(game, actions[0])
    assert all(card not in opponent.battlefield for card in victims)
    assert all(card in opponent.graveyard for card in victims)
    assert message.count("destroyed") == 4


def test_aurelia_lock_blocks_only_noncreatures_and_expires(
        make_game, make_card):
    game = make_game()
    rick, opponent = game.players
    game.active_player_index = 1
    engine = _engine(game)
    fury = make_card(
        "Aurelia's Fury", type_line="Instant", mana_cost="{X}{R}{W}",
        oracle_text=("Aurelia's Fury deals X damage divided as you choose "
                     "among any number of targets. Tap each creature dealt "
                     "damage this way. Players dealt damage this way can't "
                     "cast noncreature spells this turn."),
        power=None, toughness=None)
    fury._x_value = 1
    ctx = build_game_context(
        game, rick, opponent, card=fury, explicit_target=opponent)
    actions, _ = get_effect_library().resolve_spell(
        fury.name, fury.oracle_text, rick.name, opponent.name, ctx)
    assert [action["action"] for action in actions] == [
        "deal_damage", "restrict_noncreature_casts"]
    messages = [engine.rules._execute_action_on_state(game, action)
                for action in actions]
    assert opponent.life == 39
    lock_message = messages[1]
    assert "can't cast noncreature spells" in lock_message

    bolt = make_card("Shock", type_line="Instant",
                     oracle_text="Shock deals 2 damage to any target.",
                     mana_cost="{R}", power=None, toughness=None)
    bear = make_card("Grizzly Bears", type_line="Creature — Bear",
                     mana_cost="{1}{G}")
    opponent.hand.extend([bolt, bear])
    opponent.battlefield.extend([
        make_card("Mountain", type_line="Basic Land — Mountain",
                  oracle_text="{T}: Add {R}.", power=None, toughness=None),
        make_card("Forest", type_line="Basic Land — Forest",
                  oracle_text="{T}: Add {G}.", power=None, toughness=None),
    ])

    rejection, _, _ = _validate_cast(engine, game, opponent, bolt, rick)
    assert rejection is not None
    assert "Aurelia's Fury" in rejection[1]
    creature_rejection, _, _ = _validate_cast(
        engine, game, opponent, bear, None)
    assert creature_rejection is None

    game.turn_number += 1
    expired_rejection, _, _ = _validate_cast(engine, game, opponent, bolt, rick)
    assert expired_rejection is None


def test_aurelia_creature_target_is_tapped_without_locking_a_player(
        make_game, make_card):
    game = make_game()
    rick, opponent = game.players
    engine = _engine(game)
    victim = make_card("Fury Victim", type_line="Creature — Bear")
    opponent.battlefield.append(victim)
    fury = make_card(
        "Aurelia's Fury", type_line="Instant", mana_cost="{X}{R}{W}",
        oracle_text=("Aurelia's Fury deals X damage divided as you choose "
                     "among any number of targets. Tap each creature dealt "
                     "damage this way. Players dealt damage this way can't "
                     "cast noncreature spells this turn."),
        power=None, toughness=None)
    fury._x_value = 1
    ctx = build_game_context(
        game, rick, opponent, card=fury, explicit_target=victim)
    actions, _ = get_effect_library().resolve_spell(
        fury.name, fury.oracle_text, rick.name, opponent.name, ctx)
    assert [action["action"] for action in actions] == ["deal_damage", "tap"]
    for action in actions:
        engine.rules._execute_action_on_state(game, action)
    assert victim.damage_marked == 1
    assert victim.tapped
    assert not hasattr(rick, '_noncreature_cast_lock_turn')
    assert not hasattr(opponent, '_noncreature_cast_lock_turn')
