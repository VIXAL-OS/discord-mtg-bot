"""Regressions from the Aug. 5 Terra recency-of-attention audit.

The 18-game sample mixed DeepSeek and Qwen and deliberately excluded the
recently audited confirmation corpus.  These tests reproduce only verified
engine/display defects; the Puppeteer Clique report was a log false positive.
"""

import asyncio

from mtg.constants import Phase
from mtg.engine import GameEngine


def _engine(game):
    engine = GameEngine(None)
    game._rules_engine = engine.rules
    engine.rules.engine_ref = engine
    return engine


def _land(make_card, name, symbol):
    return make_card(
        name,
        type_line="Basic Land",
        oracle_text=f"{{T}}: Add {{{symbol}}}.",
        power=None,
        toughness=None,
    )


def _rashmi(make_card):
    return make_card(
        "Rashmi, Eternities Crafter",
        type_line="Legendary Creature - Elf Druid",
        oracle_text=(
            "Whenever you cast your first spell each turn, reveal the top "
            "card of your library. You may cast that card without paying its "
            "mana cost if it's a nonland card with mana value less than that "
            "spell's mana value. If you don't cast it, put it into your hand."
        ),
        mana_cost="{2}{G}{U}",
        cmc=4,
        power="2",
        toughness="3",
    )


def _fog(make_card):
    return make_card(
        "Fog",
        type_line="Instant",
        oracle_text="Prevent all combat damage that would be dealt this turn.",
        mana_cost="{G}",
        cmc=1,
        power=None,
        toughness=None,
    )


def test_rashmi_lower_mv_card_is_cast_through_real_spell_pipeline(
        make_game, make_card):
    from mtg.triggers import _check_cast_triggers

    game = make_game()
    rick = game.players[0]
    engine = _engine(game)
    rick.battlefield.append(_rashmi(make_card))
    free_spell = _fog(make_card)
    draws = [
        make_card("Forest", type_line="Basic Land - Forest",
                  power=None, toughness=None),
        make_card("Island", type_line="Basic Land - Island",
                  power=None, toughness=None),
    ]
    rick.library = [free_spell, *draws]
    triggering_spell = make_card(
        "Opportunity", type_line="Instant", mana_cost="{4}{U}{U}", cmc=6,
        power=None, toughness=None,
        oracle_text="Target player draws four cards.",
    )
    # The production caller increments this before scanning cast triggers.
    rick.spells_cast_this_turn = 1

    messages = asyncio.run(
        _check_cast_triggers(engine, game, rick, triggering_spell))

    assert free_spell in rick.graveyard
    assert free_spell not in rick.hand
    assert free_spell not in rick.library
    assert rick.life == 40
    assert rick.library == draws
    assert rick.spells_cast_this_turn == 2
    assert any("without paying" in message.lower() for message in messages)


def test_opponent_gets_priority_over_rashmi_trigger_without_caster_stifle(
        make_game, make_card):
    from mtg.triggers import _check_cast_triggers

    game = make_game()
    rick, claude = game.players
    engine = _engine(game)
    game.stack_enabled = True
    rick.battlefield.append(_rashmi(make_card))
    top = _fog(make_card)
    rick.library = [top]
    rick.spells_cast_this_turn = 1
    claude.hand.append(make_card(
        "Tale's End", type_line="Instant", mana_cost="{1}{U}", cmc=2,
        power=None, toughness=None,
        oracle_text=(
            "Counter target activated ability, triggered ability, or "
            "legendary spell."),
    ))
    calls = []

    async def sink(_message):
        return None

    async def counter_trigger(game_arg, _send, description):
        calls.append(description)
        trigger = game_arg.stack[-1]
        assert not trigger.is_spell
        trigger.countered = True

    game._stack_send_func = sink
    engine._combat_priority_round = counter_trigger
    source = make_card(
        "Dig Through Time", type_line="Instant", mana_cost="{6}{U}{U}",
        cmc=8, power=None, toughness=None,
    )

    asyncio.run(_check_cast_triggers(engine, game, rick, source))

    assert len(calls) == 1
    assert top in rick.library
    assert top not in rick.hand
    assert top not in rick.graveyard
    assert not game.stack


def test_capricious_hellraiser_exiles_three_and_spell_copy_ceases(
        make_game, make_card, lib):
    from mtg.spells import resolve_pending_free_casts

    game = make_game()
    rick, claude = game.players
    engine = _engine(game)
    spell = _fog(make_card)
    creatures = [
        make_card("Grizzly Bears"),
        make_card("Runeclaw Bear"),
    ]
    rick.graveyard = [spell, *creatures]
    rick.library = [
        make_card("Forest", type_line="Basic Land - Forest",
                  power=None, toughness=None),
        make_card("Swamp", type_line="Basic Land - Swamp",
                  power=None, toughness=None),
    ]

    actions, _ = lib.resolve_etb(
        "Capricious Hellraiser",
        ("When Capricious Hellraiser enters the battlefield, exile three "
         "cards at random from your graveyard. Choose a noncreature, nonland "
         "card from among them and copy it. You may cast the copy without "
         "paying its mana cost."),
        rick.name, claude.name,
    )
    assert actions == [{
        "action": "capricious_hellraiser_etb", "player": rick.name}]
    engine.rules._execute_action_on_state(game, actions[0])
    pending_copy = game._free_cast_pending[0]["card"]

    messages = asyncio.run(resolve_pending_free_casts(engine, game))

    assert rick.graveyard == []
    assert {card.id for card in rick.exile} == {
        spell.id, *(card.id for card in creatures)}
    assert pending_copy not in rick.hand
    assert pending_copy not in rick.graveyard
    assert pending_copy not in rick.exile
    assert pending_copy not in rick.battlefield
    assert all(not card.name.startswith("Copy of ")
               for card in rick.battlefield)
    assert rick.life == 40
    assert any("Capricious Hellraiser" in message for message in messages)


def test_turn_narration_flush_clears_retained_mutable_actions(make_game):
    from mtg.autoplay import _mark_active_turn_narration_sent

    game = make_game()
    actions = ["old action one", "old action two"]
    game._active_turn_narration = {
        "turn": game.turn_number,
        "player": game.active_player.name,
        "actions": actions,
        "flushed": False,
    }

    _mark_active_turn_narration_sent(game)

    assert actions == []
    assert game._active_turn_narration["flushed"]
    actions.append("new response action")
    assert actions == ["new response action"]


def test_cauldron_grants_persist_only_to_all_declared_creatures(
        make_game, make_card):
    game = make_game()
    rick = game.players[0]
    engine = _engine(game)
    cauldron = make_card(
        "Cauldron of Souls",
        type_line="Artifact",
        oracle_text=(
            "{T}: Choose any number of target creatures. Each of those "
            "creatures gains persist until end of turn."),
        power=None,
        toughness=None,
    )
    worldslayer = make_card(
        "Worldslayer", type_line="Artifact - Equipment",
        power=None, toughness=None)
    creature = make_card("Kor Outfitter", power="2", toughness="2")
    rot_farm = make_card(
        "Golgari Rot Farm", type_line="Land",
        power=None, toughness=None)
    skullclamp = make_card(
        "Skullclamp", type_line="Artifact - Equipment",
        power=None, toughness=None)
    rick.battlefield.extend(
        [cauldron, worldslayer, creature, rot_farm, skullclamp])

    result = asyncio.run(engine._execute_action(game, 0, {
        "type": "activate",
        "permanent": "Cauldron of Souls",
        "ability": 0,
        "target": [
            "Worldslayer",
            "Kor Outfitter 2/2",
            "Golgari Rot Farm",
            "Skullclamp",
        ],
    }))

    assert result and "Kor Outfitter" in result
    assert cauldron.tapped
    assert "Persist" in creature.temp_keywords
    assert "Persist" not in worldslayer.temp_keywords
    assert "Persist" not in rot_farm.temp_keywords
    assert "Persist" not in skullclamp.temp_keywords


def test_grant_keywords_creature_filter_cannot_widen_to_permanents(
        make_game, make_card):
    game = make_game()
    rick = game.players[0]
    engine = _engine(game)
    creature = make_card("Kitchen Finks")
    land = make_card(
        "Golgari Rot Farm", type_line="Land",
        power=None, toughness=None)
    equipment = make_card(
        "Skullclamp", type_line="Artifact - Equipment",
        power=None, toughness=None)
    rick.battlefield.extend([creature, land, equipment])

    engine.rules._execute_action_on_state(game, {
        "action": "grant_keywords",
        "player": rick.name,
        "target": "all_own_permanents",
        "target_filter": "creature",
        "keywords": ["Persist"],
    })

    assert "Persist" in creature.temp_keywords
    assert "Persist" not in land.temp_keywords
    assert "Persist" not in equipment.temp_keywords


def test_sythis_compound_trigger_draws_and_gains_life_each_time(
        make_game, make_card):
    from mtg.triggers import _check_cast_triggers

    game = make_game()
    rick = game.players[0]
    engine = _engine(game)
    sythis = make_card(
        "Sythis, Harvest's Hand",
        type_line="Legendary Enchantment Creature - Nymph",
        oracle_text=(
            "Whenever you cast an enchantment spell, you gain 1 life and "
            "draw a card."),
        power="1", toughness="2",
    )
    rick.battlefield.append(sythis)
    draws = [make_card("Draw One"), make_card("Draw Two")]
    rick.library = list(draws)
    enchantments = [
        make_card("Rancor", type_line="Enchantment - Aura",
                  power=None, toughness=None),
        make_card("Utopia Sprawl", type_line="Enchantment - Aura",
                  power=None, toughness=None),
    ]

    for spell in enchantments:
        asyncio.run(_check_cast_triggers(engine, game, rick, spell))

    assert rick.life == 42
    assert rick.hand == draws
    assert rick.library == []


def test_searing_blood_player_damage_waits_for_exact_target_to_die(
        make_game, make_card, lib):
    from rules.effect_templates import build_game_context

    game = make_game()
    rick, claude = game.players
    engine = _engine(game)
    target = make_card("Monastery Swiftspear", power="1", toughness="1")
    claude.battlefield.append(target)
    ctx = build_game_context(
        game, rick, claude, explicit_target=target)
    actions, _ = lib.resolve_spell(
        "Searing Blood",
        ("Searing Blood deals 2 damage to target creature. When that "
         "creature dies this turn, Searing Blood deals 3 damage to the "
         "creature's controller."),
        rick.name, claude.name, game_context=ctx,
    )

    assert [action["action"] for action in actions] == [
        "deal_damage", "schedule_death_trigger"]
    for action in actions:
        engine.rules._execute_action_on_state(game, action)
    assert claude.life == 40
    assert game._death_watchers[0]["watch_target_id"] == target.id

    engine.check_state_based_actions(game)

    assert target in claude.graveyard
    assert claude.life == 37
    assert game._death_watchers == []


def test_invalid_autoplay_skullcrack_target_returns_without_casting(
        make_game, make_card):
    from mtg.autoplay import _autoplay_execute_action

    game = make_game("modern")
    game.phase = Phase.MAIN1
    rick, claude = game.players
    skullcrack = make_card(
        "Skullcrack",
        type_line="Instant",
        mana_cost="{1}{R}",
        cmc=2,
        power=None,
        toughness=None,
        oracle_text=(
            "Players can't gain life this turn. Damage can't be prevented "
            "this turn. Skullcrack deals 3 damage to target player."),
    )
    rick.hand.append(skullcrack)
    lands = [_land(make_card, f"Mountain {i}", "R") for i in range(2)]
    rick.battlefield.extend(lands)

    class FakeCog:
        def __init__(self):
            self.engine = _engine(game)

        async def _autoplay_send(self, _thread, _message):
            return None

    result = asyncio.run(_autoplay_execute_action(
        FakeCog(), None, game, 0, {
            "type": "cast",
            "card": "Skullcrack",
            "target": "Monastery Swiftspear",
        }))

    assert result is None
    assert skullcrack in rick.hand
    assert skullcrack not in rick.graveyard
    assert all(not land.tapped for land in lands)
    assert rick.spells_cast_this_turn == 0
    assert claude.life == 20
    assert "not a legal target" in game._last_cast_failure[2]


def test_full_split_name_from_hand_defaults_to_front_half_cost(
        make_game, make_card):
    game = make_game("modern")
    game.phase = Phase.MAIN1
    rick = game.players[0]
    engine = _engine(game)
    card = make_card(
        "Insult // Injury",
        type_line="Sorcery // Sorcery",
        oracle_text=(
            "Damage can't be prevented this turn. If a source you control "
            "would deal damage this turn, it deals double that damage instead."),
        mana_cost="{2}{R} // {2}{R}",
        cmc=6,
        power=None,
        toughness=None,
    )
    card.split_names = ["Insult", "Injury"]
    card.split_costs = ["{2}{R}", "{2}{R}"]
    card.split_types = ["Sorcery", "Sorcery"]
    card.split_texts = [
        card.oracle_text,
        ("Aftermath. Injury deals 2 damage to target creature and 2 damage "
         "to target player."),
    ]
    rick.hand.append(card)
    rick.battlefield.extend(
        _land(make_card, f"Mountain {i}", "R") for i in range(3))

    ok, message, _ = asyncio.run(
        engine.cast_spell_async(game, rick, card))

    assert ok, message
    assert card._mana_paid == 3
    assert card in rick.graveyard
    assert card.cast_as_split_half == -1


def test_korvold_sacrifice_result_has_console_telemetry(
        make_game, make_card, capsys):
    from mtg.actions import _fire_sacrifice_triggers

    game = make_game()
    rick = game.players[0]
    engine = _engine(game)
    korvold = make_card(
        "Korvold, Fae-Cursed King",
        oracle_text=(
            "Whenever you sacrifice a permanent, put a +1/+1 counter on "
            "Korvold and draw a card."),
        power="4", toughness="4",
    )
    food = make_card(
        "Food", type_line="Artifact - Food",
        power=None, toughness=None)
    rick.battlefield.append(korvold)
    rick.library.append(make_card("Drawn Card"))

    messages = _fire_sacrifice_triggers(
        engine.rules, game, rick, food)

    assert messages
    assert "[SAC-TRIGGER-RESULT] Korvold, Fae-Cursed King:" in (
        capsys.readouterr().out)
