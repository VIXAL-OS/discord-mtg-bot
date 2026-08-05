"""Regressions from games 153-160 and the adjacent DeepSeek cube audit."""

import asyncio

from mtg.engine import GameEngine


def _engine(game):
    engine = GameEngine(None)
    game._rules_engine = engine.rules
    engine.rules.engine_ref = engine
    return engine


def test_primeval_titan_attack_trigger_is_not_combat_shaped():
    from mtg.judge import is_combat_shaped_resolve

    titan = ("Whenever this creature enters or attacks, you may search your "
             "library for up to two land cards, put them onto the battlefield "
             "tapped, then shuffle.")
    assert not is_combat_shaped_resolve(titan)
    assert is_combat_shaped_resolve("Attack for lethal.")


def test_solitary_confinement_is_not_scanned_twice(make_game, make_card):
    from mtg.triggers import _check_upkeep_triggers_sync

    game = make_game()
    rick = game.players[0]
    confinement = make_card(
        "Solitary Confinement", type_line="Enchantment", power=None, toughness=None,
        oracle_text=("At the beginning of your upkeep, sacrifice Solitary "
                     "Confinement unless you discard a card."),
    )
    rick.battlefield.append(confinement)

    messages, unhandled = _check_upkeep_triggers_sync(_engine(game), game)

    assert messages == []
    assert unhandled == []
    assert confinement in rick.battlefield


def test_containment_construct_exiles_discard_and_grants_permission(
        make_game, make_card):
    from mtg.helpers import madness_discard_to_exile

    game = make_game()
    rick = game.players[0]
    construct = make_card(
        "Containment Construct", type_line="Artifact Creature - Construct",
        oracle_text=("Whenever you discard a card, you may exile that card "
                     "from your graveyard. If you do, you may play that card "
                     "this turn."),
    )
    discarded = make_card("Island", type_line="Basic Land - Island",
                          power=None, toughness=None)
    rick.battlefield.append(construct)

    message = madness_discard_to_exile(game, rick, discarded)

    assert message and "Containment Construct" in message
    assert discarded in rick.exile
    assert discarded.id in rick.playable_from_exile
    assert discarded not in rick.graveyard


def test_reanimate_rejects_an_explicit_target_outside_graveyards(
        make_game, make_card, lib):
    from rules.effect_templates import build_game_context

    game = make_game()
    rick, claude = game.players
    neheb = make_card("Neheb, Dreadhorde Champion", cmc=4)
    claude.graveyard.append(neheb)
    ctx = build_game_context(
        game, rick, claude, explicit_target="Kroxa, Titan of Death's Hunger")

    actions, _ = lib.resolve_spell(
        "Reanimate",
        "Put target creature card from a graveyard onto the battlefield.",
        rick.name, claude.name, game_context=ctx,
    )

    assert not any(a.get("action") == "reanimate" for a in actions)
    assert actions[0]["action"] == "no_action"


def test_reanimate_unresolved_declared_target_spends_nothing(
        make_game, make_card):
    game = make_game()
    rick = game.players[0]
    spell = make_card(
        "Reanimate", type_line="Sorcery", power=None, toughness=None,
        mana_cost="{B}", cmc=1,
        oracle_text=("Put target creature card from a graveyard onto the "
                     "battlefield under your control. You lose life equal "
                     "to its mana value."),
    )
    neheb = make_card("Neheb, Dreadhorde Champion", cmc=4)
    rick.hand.append(spell)
    rick.graveyard.append(neheb)
    rick.mana_pool["B"] = 1

    result = asyncio.run(_engine(game)._execute_action(game, 0, {
        "type": "cast", "card": "Reanimate",
        "target": "Kroxa, Titan of Death's Hunger",
    }))

    assert result is None
    assert spell in rick.hand
    assert neheb in rick.graveyard
    assert rick.mana_pool["B"] == 1


def test_tortured_existence_validates_before_cost_and_resolves(
        make_game, make_card):
    game = make_game()
    rick = game.players[0]
    tortured = make_card(
        "Tortured Existence", type_line="Enchantment", power=None, toughness=None,
        oracle_text=("{B}, Discard a creature card: Return target creature "
                     "card from your graveyard to your hand."),
    )
    fodder = make_card("Basking Rootwalla", cmc=1)
    target = make_card("Neheb, Dreadhorde Champion", cmc=4)
    rick.battlefield.append(tortured)
    rick.hand.append(fodder)
    rick.mana_pool["B"] = 1
    engine = _engine(game)

    rejected = asyncio.run(engine._execute_action(game, 0, {
        "type": "activate", "permanent": "Tortured Existence", "ability": 0,
    }))
    assert rejected is None
    assert rick.mana_pool["B"] == 1
    assert fodder in rick.hand

    rick.graveyard.append(target)
    result = asyncio.run(engine._execute_action(game, 0, {
        "type": "activate", "permanent": "Tortured Existence", "ability": 0,
        "target": target.name, "discard": fodder.name,
    }))
    assert result and target.name in result
    assert rick.mana_pool["B"] == 0
    assert target in rick.hand
    assert fodder in rick.graveyard


def test_restoration_chapter_two_never_returns_an_instant_after_failed_pair(
        make_game, make_card):
    from mtg.spells import _resolve_restoration_chapter_two

    game = make_game()
    rick = game.players[0]
    saga = make_card("The Restoration of Eiganjo", type_line="Enchantment - Saga",
                     power=None, toughness=None)
    teferi = make_card("Teferi, Hero of Dominaria",
                       type_line="Legendary Planeswalker - Teferi",
                       cmc=5, power=None, toughness=None)
    denial = make_card("Arcane Denial", type_line="Instant", cmc=2,
                       power=None, toughness=None)
    rick.hand.append(teferi)
    rick.graveyard.append(denial)

    messages = _resolve_restoration_chapter_two(
        _engine(game), game, rick, saga)

    assert messages == []
    assert teferi in rick.hand
    assert denial in rick.graveyard
    assert denial not in rick.battlefield


def test_restoration_chapter_two_discards_then_returns_legal_permanent(
        make_game, make_card):
    from mtg.spells import _resolve_restoration_chapter_two

    game = make_game()
    rick = game.players[0]
    saga = make_card("The Restoration of Eiganjo", type_line="Enchantment - Saga",
                     power=None, toughness=None)
    fodder = make_card("Divination", type_line="Sorcery", cmc=3,
                       power=None, toughness=None)
    target = make_card("Arcane Signet", type_line="Artifact", cmc=2,
                       power=None, toughness=None)
    rick.hand.append(fodder)
    rick.graveyard.append(target)

    _resolve_restoration_chapter_two(_engine(game), game, rick, saga)

    assert fodder in rick.graveyard
    assert target in rick.battlefield
    assert target.tapped


def test_sphinx_draws_still_fire_shabraz_without_recursing(
        make_game, make_card):
    game = make_game()
    rick, claude = game.players
    shabraz = make_card(
        "Shabraz, the Skyshark",
        type_line="Legendary Creature - Shark Bird",
        oracle_text=("Partner with Brallin, Skyshark Rider\nFlying\n"
                     "Whenever you draw a card, put a +1/+1 counter on "
                     "Shabraz and you gain 1 life."),
        power="3", toughness="3",
    )
    sphinx = make_card(
        "Consecrated Sphinx", type_line="Creature - Sphinx",
        oracle_text=("Flying\nWhenever an opponent draws a card, you may "
                     "draw two cards."), power="4", toughness="6",
    )
    rick.battlefield.extend([shabraz, sphinx])
    rick.library.extend([make_card("R1"), make_card("R2"), make_card("R3")])
    claude.library.append(make_card("C1"))
    start_life = rick.life

    _engine(game).draw_cards(claude, 1, game=game)

    assert len(rick.hand) == 2
    assert shabraz.counters.get("+1/+1") == 2
    assert rick.life == start_life + 2


def test_failed_adventure_does_not_poison_later_creature_cost(
        make_game, make_card):
    game = make_game()
    rick = game.players[0]
    fae = make_card(
        "Fae of Wishes", type_line="Creature - Faerie Wizard",
        mana_cost="{1}{U} // {3}{U}", cmc=2,
        oracle_text="Flying", power="1", toughness="4",
        adventure_name="Granted", adventure_cost="{3}{U}",
        adventure_text=("You may reveal a noncreature card you own from "
                        "outside the game and put it into your hand."),
        adventure_type="Sorcery - Adventure",
    )
    rick.hand.append(fae)
    rick.mana_pool.update({"U": 1, "R": 1})

    asyncio.run(_engine(game)._execute_action(game, 0, {
        "type": "cast", "card": "Fae of Wishes", "adventure": "Granted",
    }))

    assert fae in rick.hand
    assert not fae.cast_as_adventure
    assert rick.can_pay_mana_cost("{1}{U}", spending_card=fae)[0]


def test_gadwick_cast_trigger_requires_a_blue_spell(make_game, make_card):
    from mtg.triggers import _spell_matches_cast_trigger

    game = make_game()
    engine = _engine(game)
    trigger = "whenever you cast a blue spell, tap target nonland permanent"
    jaya = make_card("Jaya Ballard", type_line="Legendary Planeswalker - Jaya",
                     mana_cost="{2}{R}{R}{R}", power=None, toughness=None)
    gearhulk = make_card("Combustible Gearhulk",
                         type_line="Artifact Creature - Construct",
                         mana_cost="{4}{R}{R}")
    chart = make_card("Chart a Course", type_line="Sorcery",
                      mana_cost="{1}{U}", power=None, toughness=None)

    assert not _spell_matches_cast_trigger(engine, trigger, jaya)
    assert not _spell_matches_cast_trigger(engine, trigger, gearhulk)
    assert _spell_matches_cast_trigger(engine, trigger, chart)


def test_sarkhan_mana_cannot_cast_jaya_but_can_cast_a_dragon(
        make_game, make_card):
    from rules.planeswalker import (AbilityType, PlaneswalkerAbility,
                                    PlaneswalkerManager)

    game = make_game()
    rick = game.players[0]
    sarkhan = make_card(
        "Sarkhan, Fireblood", type_line="Legendary Planeswalker - Sarkhan",
        mana_cost="{1}{R}{R}", power=None, toughness=None,
    )
    ability = PlaneswalkerAbility(
        index=0, loyalty_cost=1, ability_type=AbilityType.LOYALTY_PLUS,
        text=("Add two mana in any combination of colors. Spend this mana "
              "only to cast Dragon spells."),
    )
    asyncio.run(PlaneswalkerManager()._execute_ability(
        game, rick, sarkhan, ability, []))

    assert sum(rick.mana_pool.values()) == 0
    assert sum(b["amount"] for b in rick.restricted_mana_pool) == 2
    for i in range(3):
        rick.battlefield.append(make_card(
            f"Mountain {i}", type_line="Basic Land - Mountain",
            oracle_text="{T}: Add {R}.", power=None, toughness=None,
        ))
    jaya = make_card("Jaya Ballard", type_line="Legendary Planeswalker - Jaya",
                     mana_cost="{2}{R}{R}{R}", power=None, toughness=None)
    dragon = make_card("Glorybringer", type_line="Creature - Dragon",
                       mana_cost="{2}{R}{R}{R}", power="4", toughness="4")

    assert not rick.can_pay_mana_cost(jaya.mana_cost, spending_card=jaya)[0]
    assert rick.can_pay_mana_cost(dragon.mana_cost, spending_card=dragon)[0]
    assert not rick.tap_sources_for_cost(
        jaya.mana_cost, game=game, spending_card=jaya)
    assert sum(b["amount"] for b in rick.restricted_mana_pool) == 2
    assert not any(land.tapped for land in rick.battlefield)

    assert rick.tap_sources_for_cost(
        dragon.mana_cost, game=game, spending_card=dragon)
    assert rick.restricted_mana_pool == []
    assert all(land.tapped for land in rick.battlefield)

    restored = type(rick).from_dict(rick.to_dict())
    assert restored.restricted_mana_pool == []
