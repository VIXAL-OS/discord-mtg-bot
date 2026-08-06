"""Mutation-sensitive regressions from the Aug. 6 confirmation-batch audit."""

import asyncio

from mtg.constants import Phase
from mtg.engine import GameEngine
from mtg.models import GameState, Player


def _engine(game):
    engine = GameEngine(None)
    game._rules_engine = engine.rules
    engine.rules.engine_ref = engine
    return engine


def _land(make_card, name="Mountain", symbol="R", type_line=None):
    return make_card(
        name,
        type_line=type_line or f"Basic Land - {name}",
        oracle_text=f"{{T}}: Add {{{symbol}}}.",
        power=None,
        toughness=None,
    )


def test_autoplay_boolean_adventure_payload_falls_back_to_front_face(
        make_game, make_card):
    from mtg.autoplay import _autoplay_execute_action

    game = make_game("modern")
    game.phase = Phase.MAIN1
    rick = game.players[0]
    bear = make_card(
        "Runeclaw Bear", mana_cost="{1}{G}", cmc=2,
        oracle_text="", power="2", toughness="2")
    rick.hand.append(bear)
    rick.battlefield.extend([
        _land(make_card, "Forest", "G"),
        _land(make_card, "Forest Two", "G", "Basic Land - Forest"),
    ])

    class FakeCog:
        def __init__(self):
            self.engine = _engine(game)

        async def _autoplay_send(self, _thread, _message):
            return None

    result = asyncio.run(_autoplay_execute_action(
        FakeCog(), None, game, 0,
        {"type": "cast", "card": bear.name, "adventure": True}))

    assert result
    assert bear in rick.battlefield


def test_inventors_fair_restriction_rejects_before_any_cost_mutation(
        make_game, make_card):
    game = make_game()
    rick = game.players[0]
    engine = _engine(game)
    fair = make_card(
        "Inventors' Fair", type_line="Legendary Land",
        oracle_text=(
            "{T}: Add {C}.\n"
            "{4}, {T}, Sacrifice Inventors' Fair: Search your library for "
            "an artifact card, reveal it, put it into your hand, then shuffle. "
            "Activate only if you control three or more artifacts."),
        power=None, toughness=None)
    artifacts = [
        make_card(f"Clue {i}", type_line="Artifact - Clue",
                  power=None, toughness=None)
        for i in range(2)
    ]
    lands = [_land(make_card, f"Wastes {i}", "C", "Basic Land")
             for i in range(5)]
    rick.battlefield.extend([fair, *artifacts, *lands])
    before_library = list(rick.library)

    result = asyncio.run(engine._execute_action(game, 0, {
        "type": "activate", "permanent": fair.name, "ability": 1}))

    assert result is None
    assert fair in rick.battlefield and not fair.tapped
    assert all(not land.tapped for land in lands)
    assert rick.library == before_library

    artifacts.append(make_card(
        "Phased Clue", type_line="Artifact - Clue",
        power=None, toughness=None))
    artifacts[-1]._phased_out = True
    rick.battlefield.append(artifacts[-1])
    from mtg.helpers import activated_ability_restriction_failure
    assert activated_ability_restriction_failure(
        game, rick, fair.oracle_text).endswith("controls only 2")


def test_chandra_window_defers_damage_persists_and_resolves_once(
        make_game, make_card):
    from mtg.helpers import is_castable_from_exile

    game = make_game()
    rick, claude = game.players
    third = Player(name="Third", user_id=12345, life=40)
    game.players.append(third)
    engine = _engine(game)
    spell = make_card(
        "Goblin Hero", mana_cost="{R}", cmc=1,
        oracle_text="", power="1", toughness="1")
    rick.library = [spell]

    msg = engine.rules._execute_action_on_state(game, {
        "action": "exile_top_play_or_damage", "player": rick.name,
        "damage": 2, "source": "Chandra, Torch of Defiance"})

    assert "may cast" in msg.lower()
    assert claude.life == 40
    assert spell.id in game.conditional_exile_casts
    assert spell.id not in rick.playable_from_exile
    assert is_castable_from_exile(game, rick, spell)
    restored = GameState.from_dict(game.to_dict())
    assert restored.conditional_exile_casts[spell.id]["damage"] == 2

    messages = engine.resolve_expired_conditional_exile_casts(game)
    assert claude.life == 38
    assert third.life == 38
    assert spell.id not in game.conditional_exile_casts
    assert len(messages) == 2
    assert engine.resolve_expired_conditional_exile_casts(game) == []
    assert claude.life == 38


def test_chandra_exact_exile_cast_consumes_record_but_land_never_castable(
        make_game, make_card):
    from mtg.helpers import is_castable_from_exile

    game = make_game()
    rick, claude = game.players
    engine = _engine(game)
    creature = make_card(
        "Mons's Goblin Raiders", mana_cost="{R}", cmc=1,
        oracle_text="", power="1", toughness="1")
    duplicate = make_card(
        "Mons's Goblin Raiders", mana_cost="{R}", cmc=1,
        oracle_text="", power="1", toughness="1")
    rick.library = [creature]
    rick.battlefield.append(_land(make_card))
    engine.rules._execute_action_on_state(game, {
        "action": "exile_top_play_or_damage", "player": rick.name})
    rick.exile.append(duplicate)
    game.conditional_exile_casts[duplicate.id] = {
        "card_id": duplicate.id, "controller_index": 0,
        "expires_turn": game.turn_number,
        "source_name": "Chandra, Torch of Defiance", "damage": 2,
    }
    rick.exile.remove(creature)
    rick.hand.append(creature)

    ok, message, _ = asyncio.run(engine.cast_spell_async(
        game, rick, creature, from_exile=True))

    assert ok, message
    assert creature.id not in game.conditional_exile_casts
    assert duplicate.id in game.conditional_exile_casts
    game.conditional_exile_casts.pop(duplicate.id)
    assert engine.resolve_expired_conditional_exile_casts(game) == []
    assert claude.life == 40

    land_game = make_game()
    land_rick = land_game.players[0]
    land_engine = _engine(land_game)
    exiled_land = _land(make_card, "Swamp", "B")
    land_rick.library = [exiled_land]
    land_engine.rules._execute_action_on_state(land_game, {
        "action": "exile_top_play_or_damage", "player": land_rick.name})
    assert not is_castable_from_exile(land_game, land_rick, exiled_land)
    assert exiled_land.id not in land_rick.playable_from_exile


def test_blood_on_the_snow_counts_consumed_snow_and_resolves_in_order(
        make_game, make_card, lib):
    game = make_game()
    rick, claude = game.players
    engine = _engine(game)

    snow_ring = make_card(
        "Snow-Covered Sol Ring", type_line="Snow Artifact",
        oracle_text="{T}: Add {C}{C}.", power=None, toughness=None)
    rick.battlefield.append(snow_ring)
    assert rick.tap_sources_for_cost("{1}", game=game)
    assert rick._last_payment["snow_spent"] == 1
    assert rick.mana_pool["C"] == 1

    own_four = make_card("Own Four", cmc=4, power="4", toughness="4")
    too_large = make_card("Too Large", cmc=5, power="5", toughness="5")
    opposing = make_card("Opposing Bear", cmc=2, power="2", toughness="2")
    rick.battlefield.append(own_four)
    rick.graveyard.append(too_large)
    claude.battlefield.append(opposing)
    actions, _ = lib.resolve_spell(
        "Blood on the Snow",
        ("Choose one - Destroy all creatures; or destroy all planeswalkers. "
         "Then return a creature or planeswalker card with mana value X or "
         "less from your graveyard, where X is snow mana spent to cast this."),
        rick.name, claude.name,
        game_context={
            "_modes": [1], "snow_mana_spent": 4,
            "controller_creature_count": 1,
            "opponent_creature_count": 1,
            "controller_planeswalker_count": 0,
            "opponent_planeswalker_count": 0,
        })
    assert [a["action"] for a in actions] == [
        "destroy_all_creatures", "reanimate"]
    assert actions[1]["max_cmc"] == 4
    invalid, _ = lib.resolve_spell(
        "Blood on the Snow", "Choose one.", rick.name, claude.name,
        game_context={"_modes": ["not-a-mode"], "snow_mana_spent": 4})
    assert invalid[0]["action"] == "no_action"
    assert "invalid mode" in invalid[0]["reason"]
    for action in actions:
        engine.rules._execute_action_on_state(game, action)
    assert own_four in rick.battlefield
    assert too_large in rick.graveyard
    assert opposing in claude.graveyard


def test_reality_shift_manifests_real_card_and_zone_change_restores_it(
        make_game, make_card, lib):
    from rules.effect_templates import build_game_context

    game = make_game()
    rick, claude = game.players
    engine = _engine(game)
    target = make_card("Target Bear")
    original = make_card(
        "Lightning Bolt", type_line="Instant", mana_cost="{R}",
        power=None, toughness=None)
    claude.battlefield.append(target)
    claude.library.append(original)
    ctx = build_game_context(game, rick, claude, explicit_target=target)
    actions, _ = lib.resolve_spell(
        "Reality Shift",
        "Exile target creature. Its controller manifests the top card.",
        rick.name, claude.name, game_context=ctx)
    assert actions[1]["action"] == "manifest_top"
    for action in actions:
        engine.rules._execute_action_on_state(game, action)
    assert original in claude.battlefield and original.name == "Manifest"

    engine.rules._execute_action_on_state(game, {
        "action": "move_card", "card": "Manifest",
        "from_zone": "battlefield", "to_zone": "hand",
        "player": claude.name})
    assert original in claude.hand
    assert original.name == "Lightning Bolt"
    assert not original._manifested


def test_sba_death_restores_phantasmal_image_printed_identity(
        make_game, make_card):
    from mtg.actions import _snapshot_copy_source

    game = make_game()
    rick = game.players[0]
    engine = _engine(game)
    image = make_card(
        "Phantasmal Image", mana_cost="{1}{U}", cmc=2,
        oracle_text="You may have Phantasmal Image enter as a copy.",
        power="0", toughness="0")
    _snapshot_copy_source(image)
    image.name = "Woe Strider"
    image.power = "3"
    image.toughness = "2"
    image.damage_marked = 2
    image._is_copy = True
    rick.battlefield.append(image)

    engine.check_state_based_actions(game)

    assert image in rick.graveyard
    assert image.name == "Phantasmal Image"
    assert image._pre_copy_snapshot is None


def test_phased_combatants_neither_deal_nor_receive_damage(
        make_game, make_card):
    from mtg.combat import resolve_combat_damage

    game = make_game()
    rick, claude = game.players
    engine = _engine(game)
    attacker = make_card("Attacker", power="4", toughness="4")
    blocker = make_card("Blocker", power="2", toughness="2")
    rick.battlefield.append(attacker)
    claude.battlefield.append(blocker)
    game.active_player_index = 0
    game.attackers = [attacker.id]
    game.blockers = {attacker.id: [blocker.id]}

    blocker._phased_out = True
    resolve_combat_damage(engine.rules, game)
    assert claude.life == 40
    assert attacker.damage_marked == 0
    assert blocker.damage_marked == 0

    attacker._phased_out = True
    game.blockers = {}
    resolve_combat_damage(engine.rules, game)
    assert claude.life == 40


def test_brawl_does_not_track_or_enforce_commander_damage(
        make_game, make_card):
    brawl = make_game("brawl")
    rick, claude = brawl.players
    engine = _engine(brawl)
    commander = make_card("Brawl Commander", power="3", toughness="3")
    commander.is_commander = True
    engine.rules._apply_combat_damage_to_player(
        brawl, claude, 3, commander)
    assert claude.commander_damage == {}
    claude.commander_damage[commander.name] = 21
    engine.check_state_based_actions(brawl)
    assert not brawl.ended

    commander_game = make_game("commander")
    c_rick, c_claude = commander_game.players
    c_engine = _engine(commander_game)
    c_commander = make_card("Real Commander", power="3", toughness="3")
    c_commander.is_commander = True
    c_engine.rules._apply_combat_damage_to_player(
        commander_game, c_claude, 3, c_commander)
    assert c_claude.commander_damage[c_commander.name] == 3


def test_open_vaults_initializes_simultaneous_entries_auras_and_sagas(
        make_game, make_card):
    game = make_game()
    rick, claude = game.players
    engine = _engine(game)
    victim = make_card("Victim", power="2", toughness="2")
    claude.battlefield.append(victim)
    pacifism = make_card(
        "Pacifism", type_line="Enchantment - Aura",
        oracle_text="Enchant creature\nEnchanted creature can't attack or block.",
        power=None, toughness=None)
    saga = make_card(
        "Test Saga", type_line="Enchantment - Saga",
        oracle_text="I - Draw a card.\nII - Gain 2 life.",
        power=None, toughness=None)
    rock = make_card(
        "Mind Stone", type_line="Artifact",
        power=None, toughness=None)
    rock.tapped = True
    rock.damage_marked = 7
    rock.counters["charge"] = 3
    rick.graveyard.extend([pacifism, saga])
    claude.graveyard.append(rock)

    engine.rules._execute_action_on_state(
        game, {"action": "open_the_vaults"})

    assert pacifism in rick.battlefield
    assert pacifism.attached_to == victim.id
    assert saga.counters["lore"] == 1
    assert rock in claude.battlefield
    assert not rock.tapped and rock.damage_marked == 0
    assert rock.counters == {}


def test_storefront_snapcaster_growing_rites_and_zone_display(
        make_game, make_card, lib):
    from rules.effect_templates import build_game_context

    game = make_game("commander")
    rick, claude = game.players
    engine = _engine(game)
    storefront = make_card(
        "Obscura Storefront", type_line="Land",
        power=None, toughness=None)
    mountain = _land(make_card, "Mountain", "R")
    island = _land(
        make_card, "Snow-Covered Island", "U",
        "Basic Snow Land - Island")
    rick.battlefield.append(storefront)
    rick.library = [mountain, island]
    life_before = rick.life
    actions, _ = lib.resolve_etb(
        storefront.name, "When this enters, sacrifice it.", rick.name,
        claude.name, build_game_context(game, rick, claude, card=storefront))
    assert actions[0]["action"] == "sacrifice_permanent"
    engine.rules._execute_action_on_state(game, actions[0])
    assert storefront in rick.graveyard
    assert island in rick.battlefield and island.tapped
    assert mountain in rick.library
    assert rick.life == life_before + 1

    snap_actions, _ = lib.resolve_etb(
        "Snapcaster Mage", "Target instant or sorcery gains flashback.",
        rick.name, claude.name, {})
    assert snap_actions[0]["silent_on_no_result"] is True

    rites = make_card(
        "Growing Rites of Itlimoc // Itlimoc, Cradle of the Sun",
        type_line="Legendary Enchantment // Legendary Land",
        has_transform=True, power=None, toughness=None)
    rites_actions, _ = lib.resolve_etb(
        rites.name, "Look at the top four cards.", rick.name, claude.name,
        build_game_context(game, rick, claude, card=rites))
    assert rites_actions and rites_actions[0]["action"] == "select_from_top"

    commander = make_card("Display Commander")
    commander.is_commander = True
    commander.owner_index = 0
    rick.battlefield.append(commander)
    msg = engine.rules._execute_action_on_state(game, {
        "action": "move_card", "card": commander.name,
        "from_zone": "battlefield", "to_zone": "hand",
        "player": rick.name})
    assert "command zone" in msg
    assert "command_zone" not in msg
