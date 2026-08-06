"""Regressions from the Aug. 5 confirmation batch (160 completed games)."""

import asyncio

from mtg.constants import Phase
from mtg.engine import GameEngine


def _engine(game):
    engine = GameEngine(None)
    game._rules_engine = engine.rules
    engine.rules.engine_ref = engine
    return engine


def test_pending_drain_prefers_known_template_without_ai(
        make_game, make_card, capsys):
    game = make_game()
    rick, claude = game.players
    engine = _engine(game)
    hullbreaker = make_card(
        "Hullbreaker Horror", type_line="Creature - Kraken Horror",
        oracle_text=("Whenever you cast a spell, choose up to one - Return "
                     "target spell you don't control to its owner's hand. "
                     "Return target nonland permanent to its owner's hand."),
        power="7", toughness="8",
    )
    tithe = make_card(
        "Smothering Tithe", type_line="Enchantment",
        power=None, toughness=None, cmc=4,
    )
    rick.battlefield.append(hullbreaker)
    claude.battlefield.append(tithe)
    engine._queue_async_trigger(
        game, hullbreaker,
        "Whenever you cast a spell, return target nonland permanent "
        "to its owner's hand.",
        "cast_trigger", claude.name,  # deliberately stale/wrong metadata
        context="Rick cast Opt (via suspend)",
    )

    asyncio.run(engine.drain_pending_triggers(game))

    assert tithe not in claude.battlefield
    assert tithe in claude.hand
    output = capsys.readouterr().out
    assert "[DRAIN-CAST_TRIGGER-TEMPLATE]" in output
    assert "via Tier 3" not in output


def test_underworld_breach_sacrifices_from_nonactive_owner(
        make_game, make_card):
    from mtg.triggers import _check_end_step_triggers_sync

    game = make_game()
    rick, claude = game.players
    engine = _engine(game)
    breach = make_card(
        "Underworld Breach", type_line="Enchantment",
        oracle_text=("At the beginning of the end step, sacrifice this "
                     "enchantment."),
        power=None, toughness=None,
    )
    claude.battlefield.append(breach)

    messages, unhandled = _check_end_step_triggers_sync(engine, game)

    assert unhandled == []
    assert breach not in claude.battlefield
    assert breach in claude.graveyard
    assert breach not in rick.graveyard
    assert any("Underworld Breach" in msg for msg in messages)


def test_loyalty_text_is_not_a_live_draw_trigger(
        make_game, make_card, capsys):
    from mtg.triggers import fire_draw_triggers

    game = make_game()
    rick = game.players[0]
    _engine(game)
    teferi = make_card(
        "Teferi, Hero of Dominaria",
        type_line="Legendary Planeswalker - Teferi",
        oracle_text=(
            "\u22128: You get an emblem with \"Whenever you draw a card, "
            "exile target permanent an opponent controls.\""),
        power=None, toughness=None,
    )
    rick.battlefield.append(teferi)

    assert fire_draw_triggers(game, rick) == []
    assert "DRAW-TRIGGER" not in capsys.readouterr().out


def test_pathbreaker_and_kessig_attack_templates(
        make_game, make_card, lib):
    from rules.effect_templates import build_game_context

    game = make_game()
    rick, claude = game.players
    engine = _engine(game)
    ibex = make_card("Pathbreaker Ibex", power="3", toughness="3")
    giant = make_card("Hamletback Goliath", power="8", toughness="8")
    rick.battlefield.extend([ibex, giant])
    ctx = build_game_context(game, rick, claude, card=ibex)

    actions, _ = lib.resolve_attack_trigger(
        "Pathbreaker Ibex",
        "Whenever Pathbreaker Ibex attacks, creatures you control gain "
        "trample and get +X/+X until end of turn, where X is the greatest "
        "power among creatures you control.",
        ibex.name, 3, rick.name, claude.name, ctx,
    )
    assert actions[0]["power"] == 8
    assert actions[0]["toughness"] == 8
    assert actions[0]["keywords"] == ["Trample"]

    kessig = make_card("Kessig Naturalist", power="2", toughness="2")
    rick.battlefield.append(kessig)
    kctx = build_game_context(game, rick, claude, card=kessig)
    mana_actions, _ = lib.resolve_attack_trigger(
        "Kessig Naturalist",
        "Whenever Kessig Naturalist attacks, add {R} or {G}. Until end of "
        "turn, you don't lose this mana as steps and phases end.",
        kessig.name, 2, rick.name, claude.name, kctx,
    )
    engine.rules._execute_action_on_state(game, mana_actions[0])
    assert rick.mana_pool["G"] == 1
    engine.rules.on_phase_change(game, Phase.MAIN2)
    assert rick.mana_pool["G"] == 1
    engine.rules.on_end_step(game)
    assert rick.mana_pool["G"] == 0


def test_ardenn_attaches_equipment_at_beginning_of_combat(
        make_game, make_card, lib):
    from rules.effect_templates import build_game_context

    game = make_game()
    rick, claude = game.players
    engine = _engine(game)
    ardenn = make_card(
        "Ardenn, Intrepid Archaeologist",
        oracle_text=("At the beginning of combat on your turn, you may "
                     "attach any number of Auras and Equipment you control "
                     "to target permanent or player."),
        power="3", toughness="3",
    )
    sword = make_card(
        "Sword of the Animist", type_line="Artifact - Equipment",
        power=None, toughness=None,
    )
    target = make_card("Colossus", power="10", toughness="10")
    aura = make_card(
        "Ethereal Armor", type_line="Enchantment - Aura",
        oracle_text="Enchant creature",
        power=None, toughness=None,
    )
    rick.battlefield.extend([ardenn, sword, aura, target])
    ctx = build_game_context(game, rick, claude, card=ardenn)

    actions, _ = lib.resolve_etb(
        ardenn.name, ardenn.oracle_text, rick.name, claude.name,
        game_context=ctx, event_type="beginning_combat",
    )
    for action in actions:
        engine.rules._execute_action_on_state(game, action)

    assert sword.attached_to == target.id
    assert sword.id in target.attachments
    assert aura.attached_to == target.id
    assert aura.id in target.attachments


def test_wandermare_and_gadwick_cast_templates(
        make_game, make_card, lib):
    from rules.effect_templates import build_game_context

    game = make_game()
    rick, claude = game.players
    wandermare = make_card("Wandermare")
    gadwick = make_card("Gadwick, the Wizened")
    target = make_card(
        "Thought Vessel", type_line="Artifact",
        power=None, toughness=None, cmc=2,
    )
    rick.battlefield.extend([wandermare, gadwick])
    claude.battlefield.append(target)

    wctx = build_game_context(game, rick, claude, card=wandermare)
    actions, _ = lib.resolve_etb(
        wandermare.name,
        "Whenever you cast a creature spell that has an Adventure, put a "
        "+1/+1 counter on Wandermare.",
        rick.name, claude.name, wctx, event_type="cast_trigger",
    )
    assert actions == [{
        "action": "add_counters", "card": "Wandermare",
        "counter_type": "+1/+1", "amount": 1,
    }]

    gctx = build_game_context(game, rick, claude, card=gadwick)
    actions, _ = lib.resolve_etb(
        gadwick.name,
        "Whenever you cast a blue spell, tap target nonland permanent an "
        "opponent controls.",
        rick.name, claude.name, gctx, event_type="cast_trigger",
    )
    assert actions == [{"action": "tap", "card": "Thought Vessel"}]


def test_omarthis_manifests_real_library_cards_and_voice_has_dies_template(
        make_game, make_card, lib):
    from rules.effect_templates import build_game_context

    game = make_game()
    rick, claude = game.players
    engine = _engine(game)
    omarthis = make_card(
        "Omarthis, Ghostfire Initiate",
        oracle_text=("When Omarthis dies, manifest that many cards from the "
                     "top of your library, where X is the number of counters "
                     "on it."),
    )
    omarthis.counters["+1/+1"] = 2
    rick.graveyard.append(omarthis)
    original_cards = [
        make_card("Lightning Bolt", type_line="Instant",
                  power=None, toughness=None),
        make_card("Forest", type_line="Basic Land - Forest",
                  power=None, toughness=None),
    ]
    rick.library.extend(original_cards)
    ctx = build_game_context(
        game, rick, claude, card=omarthis, dying_creature=omarthis)

    actions, _ = lib.resolve_etb(
        omarthis.name, omarthis.oracle_text, rick.name, claude.name,
        ctx, event_type="dies",
    )
    assert actions == [{
        "action": "manifest_top", "player": rick.name, "count": 2,
        "source": "Omarthis, Ghostfire Initiate",
    }]
    engine.rules._execute_action_on_state(game, actions[0])
    manifests = [c for c in rick.battlefield
                 if getattr(c, "_manifested", False)]
    assert len(manifests) == 2
    assert rick.library == []
    assert all(c.name == "Manifest" and c.power == "2" for c in manifests)
    assert {c._manifest_original["name"] for c in manifests} == {
        "Lightning Bolt", "Forest",
    }

    voice = make_card(
        "Voice of Resurgence",
        oracle_text=("Whenever an opponent casts a spell during your turn "
                     "or when Voice of Resurgence dies, create an Elemental."),
    )
    voice_actions, _ = lib.resolve_etb(
        voice.name, voice.oracle_text, rick.name, claude.name,
        build_game_context(game, rick, claude, card=voice,
                           dying_creature=voice),
        event_type="dies",
    )
    assert voice_actions[0]["action"] == "create_token"
    assert voice_actions[0]["name"] == "Elemental"


def test_tovolar_upkeep_makes_night_and_transforms_human_werewolves(
        make_game, make_card):
    from mtg.triggers import _check_upkeep_triggers_sync

    game = make_game()
    rick = game.players[0]
    engine = _engine(game)
    tovolar = make_card(
        "Tovolar, Dire Overlord",
        type_line="Legendary Creature - Human Werewolf",
        oracle_text=(
            "Daybound\nAt the beginning of your upkeep, if you control "
            "three or more Wolves and/or Werewolves, it becomes night. "
            "Then transform any number of Human Werewolves you control."),
        has_transform=True,
        back_face_name="Tovolar, the Midnight Scourge",
        back_face_type_line="Legendary Creature - Werewolf",
        back_face_oracle_text="Nightbound",
        back_face_power="4", back_face_toughness="4",
        power="3", toughness="3",
    )
    classic = make_card(
        "Village Ironsmith", type_line="Creature - Human Werewolf",
        oracle_text=("At the beginning of each upkeep, if no spells were "
                     "cast last turn, transform Village Ironsmith."),
        has_transform=True,
        back_face_name="Ironfang",
        back_face_type_line="Creature - Werewolf",
        back_face_oracle_text=("At the beginning of each upkeep, if a player "
                               "cast two or more spells last turn, transform."),
        back_face_power="3", back_face_toughness="1",
        power="1", toughness="1",
    )
    wolf = make_card(
        "Wolf Token", type_line="Creature Token - Wolf",
        oracle_text="", power="2", toughness="2",
    )
    rick.battlefield.extend([tovolar, classic, wolf])

    messages, unhandled = _check_upkeep_triggers_sync(engine, game)

    assert unhandled == []
    assert game.day_night_active and not game.is_day
    assert tovolar.is_transformed
    assert classic.is_transformed
    assert any("Tovolar made it night" in msg for msg in messages)


def test_surly_badgersaur_fights_on_noncreature_nonland_discard(
        make_game, make_card):
    from mtg.triggers import fire_discard_triggers

    game = make_game()
    rick, claude = game.players
    _engine(game)
    surly = make_card(
        "Surly Badgersaur",
        oracle_text=("Whenever you discard a noncreature, nonland card, "
                     "Surly Badgersaur fights up to one target creature you "
                     "don't control."),
        power="4", toughness="4",
    )
    target = make_card("Bear Cub", power="2", toughness="2")
    discarded = make_card(
        "Faithless Looting", type_line="Sorcery",
        power=None, toughness=None,
    )
    rick.battlefield.append(surly)
    claude.battlefield.append(target)

    fire_discard_triggers(game, rick, discarded)

    assert target.damage_marked == 4
    assert surly.damage_marked == 2


def test_conspiracy_theorist_exiles_one_nonland_per_discard_event(
        make_game, make_card):
    from mtg.helpers import madness_discard_to_exile

    game = make_game()
    rick = game.players[0]
    theorist = make_card(
        "Conspiracy Theorist",
        oracle_text=("Whenever you discard one or more nonland cards, you "
                     "may exile one of them from your graveyard. If you do, "
                     "you may cast it this turn."),
    )
    first = make_card("Opt", type_line="Instant", power=None, toughness=None)
    second = make_card(
        "Consider", type_line="Instant", power=None, toughness=None)
    rick.battlefield.append(theorist)
    game._active_discard_event_id = 12

    first_msg = madness_discard_to_exile(game, rick, first)
    second_msg = madness_discard_to_exile(game, rick, second)

    assert first_msg and "Conspiracy Theorist" in first_msg
    assert first in rick.exile
    assert first.id in rick.playable_from_exile
    assert second_msg is None
    assert second not in rick.exile


def test_planeswalker_tail_is_deterministic_and_liliana_spares_noncreatures(
        make_game, make_card, lib):
    from rules.effect_templates import build_game_context

    game = make_game()
    rick, claude = game.players
    engine = _engine(game)
    for player, prefix in ((rick, "R"), (claude, "C")):
        player.battlefield.extend([
            make_card(f"{prefix} creature 1"),
            make_card(f"{prefix} creature 2"),
            make_card(f"{prefix} Tithe", type_line="Enchantment",
                      power=None, toughness=None),
        ])
    liliana = make_card(
        "Liliana, Dreadhorde General",
        type_line="Legendary Planeswalker - Liliana",
        power=None, toughness=None,
    )
    rick.battlefield.append(liliana)
    ctx = build_game_context(game, rick, claude, card=liliana)
    actions, _ = lib.resolve_pw_ability(
        liliana.name, "Each player sacrifices two creatures.",
        rick.name, claude.name, ctx,
    )
    assert len(actions) == 4
    assert all(action["type_filter"] == "creature" for action in actions)
    for action in actions:
        engine.rules._execute_action_on_state(game, action)

    assert any(c.name == "R Tithe" for c in rick.battlefield)
    assert any(c.name == "C Tithe" for c in claude.battlefield)
    assert not rick.creatures()
    assert not claude.creatures()

    garruk_plus, _ = lib.resolve_pw_ability(
        "Garruk Wildspeaker", "Untap two target lands.",
        rick.name, claude.name, {},
    )
    garruk_ult, _ = lib.resolve_pw_ability(
        "Garruk Wildspeaker",
        "Creatures you control get +3/+3 and gain trample until end of turn.",
        rick.name, claude.name, {},
    )
    assert garruk_plus == [{
        "action": "untap_lands", "player": rick.name, "count": 2,
    }]
    assert garruk_ult[0]["power"] == 3
    assert garruk_ult[0]["keywords"] == ["Trample"]

    rick.hand.extend([
        make_card("Mountain", type_line="Basic Land - Mountain",
                  power=None, toughness=None),
        make_card("Shock", type_line="Instant", power=None, toughness=None),
    ])
    jaya_ctx = build_game_context(game, rick, claude)
    jaya_actions, _ = lib.resolve_pw_ability(
        "Jaya Ballard",
        "Discard up to three cards. If you do, draw that many cards.",
        rick.name, claude.name, jaya_ctx,
    )
    assert [a["action"] for a in jaya_actions] == [
        "discard", "discard", "draw_cards",
    ]
    assert jaya_actions[-1]["amount"] == 2

    sword = make_card(
        "Sword of Fire and Ice", type_line="Artifact - Equipment",
        power=None, toughness=None, cmc=3,
    )
    rick.graveyard.append(sword)
    nahiri_actions, _ = lib.resolve_pw_ability(
        "Nahiri, the Lithomancer",
        "You may put an Equipment card from your hand or graveyard onto "
        "the battlefield.",
        rick.name, claude.name,
        build_game_context(game, rick, claude),
    )
    assert nahiri_actions == [{
        "action": "move_card", "card": sword.name,
        "from_zone": "graveyard", "to_zone": "battlefield",
        "player": rick.name,
    }]
