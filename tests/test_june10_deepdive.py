"""Repros for the June 10 Tier-2 DEEP-DIVE findings (stress-deck cluster,
games #100-139 â€” snow/cascade + sagas/layers agents).

Finding IDs: A1-A10 = snow/cascade agent, B1-B10 = sagas/layers agent.
The B3 combat test was written TEST-FIRST (the agent could not pin the
mechanism); whatever it catches is the fix target.
"""
from types import SimpleNamespace

import pytest

from mtg.actions import execute_action_on_state


def run(rules, game, action):
    return execute_action_on_state(rules, game, dict(action))


def _engine_shim(rules):
    return SimpleNamespace(rules=rules)


BLOOD_ARTIST_2026 = ("Whenever this creature or another creature dies, "
                     "target player loses 1 life and you gain 1 life.")


# ---------------------------------------------------------------------------
# A1 â€” Marit Lage's Slumber: intervening-if condition (CR 603.4)
# ---------------------------------------------------------------------------

class TestMaritLageSlumber:
    ORACLE = ("At the beginning of your upkeep, if you control ten or more "
              "snow permanents, sacrifice Marit Lage's Slumber. If you do, "
              "create Marit Lage, a legendary 20/20 black Avatar creature "
              "token with flying and indestructible.")

    def _setup(self, game, make_card, snow_count):
        rick = game.players[0]
        slumber = make_card("Marit Lage's Slumber",
                            type_line="Snow Enchantment", power="", toughness="",
                            oracle_text=self.ORACLE)
        rick.battlefield.append(slumber)
        for _ in range(snow_count - 1):  # Slumber itself is snow
            rick.battlefield.append(make_card(
                "Snow-Covered Forest", type_line="Basic Snow Land â€” Forest",
                power="", toughness=""))
        return rick, slumber

    def test_below_threshold_no_token(self, lib, game, make_card):
        rick, _ = self._setup(game, make_card, snow_count=9)
        actions, _d = lib.resolve_etb(
            "Marit Lage's Slumber", self.ORACLE, "Rick", "Claude",
            game_context={"_controller_player": rick}, event_type="upkeep")
        assert actions and actions[0]["action"] == "no_action", \
            "Slumber fired below 10 snow permanents (won a game pre-fix)"

    def test_at_threshold_sacrifices_and_creates_real_marit_lage(self, lib, game, make_card):
        rick, _ = self._setup(game, make_card, snow_count=10)
        actions, _d = lib.resolve_etb(
            "Marit Lage's Slumber", self.ORACLE, "Rick", "Claude",
            game_context={"_controller_player": rick}, event_type="upkeep")
        kinds = [a["action"] for a in actions]
        assert "move_card" in kinds and "create_token" in kinds
        tok = next(a for a in actions if a["action"] == "create_token")
        assert tok["name"] == "Marit Lage"          # not "Black Avatar"
        assert "Legendary" in tok["types"]
        assert set(k.lower() for k in tok["keywords"]) == {"flying", "indestructible"}

    def test_generic_upkeep_pattern_refuses_intervening_if(self, lib, game, make_card):
        # A different conditional-upkeep card must NOT mint a token via the
        # generic pattern (the pre-fix path regexed across the condition).
        rick = game.players[0]
        actions, _d = lib.resolve_etb(
            "Hypothetical Slumber Variant",
            "At the beginning of your upkeep, if you control ten or more "
            "artifacts, create a 9/9 colorless Construct creature token.",
            "Rick", "Claude",
            game_context={"_controller_player": rick}, event_type="upkeep")
        assert actions is None or all(a["action"] == "no_action" for a in actions)


# ---------------------------------------------------------------------------
# A2 â€” get_legal_targets: artifact/enchantment/land restrictions (Berg Strider)
# ---------------------------------------------------------------------------

class TestLegalTargetsArtifacts:
    def test_artifact_restriction_finds_opponent_artifacts(self, game, make_card):
        from rules.spell_resolver import SpellResolver
        from rules.targeting import TargetTextParser
        rick, claude = game.players
        claude.battlefield.append(make_card("Arcane Signet", type_line="Artifact",
                                            power="", toughness=""))
        claude.battlefield.append(make_card("Stoneforge Mystic"))
        sr = SpellResolver(None)
        restriction = TargetTextParser.parse("target artifact")
        legal = sr.get_legal_targets(game, rick, restriction)
        names = [t[0].name for t in legal]
        assert "Arcane Signet" in names, \
            "ARTIFACT restriction fell through every scan branch (Berg Strider fizzle)"

    def test_land_restriction_finds_lands(self, game, make_card):
        from rules.spell_resolver import SpellResolver
        from rules.targeting import TargetTextParser
        rick, claude = game.players
        claude.battlefield.append(make_card("Island", type_line="Basic Land â€” Island",
                                            power="", toughness=""))
        sr = SpellResolver(None)
        restriction = TargetTextParser.parse("target land")
        legal = sr.get_legal_targets(game, rick, restriction)
        assert any(t[0].name == "Island" for t in legal)


# ---------------------------------------------------------------------------
# A7 â€” dual lands: one tap = one mana (underpayment direction)
# ---------------------------------------------------------------------------

class TestDualLandPayment:
    def _dual(self, make_card):
        return make_card("Underground River", type_line="Land",
                         oracle_text="{T}: Add {U} or {B}.", power="", toughness="")

    def test_five_mana_spell_needs_five_sources(self, game, make_card):
        rick = game.players[0]
        for _ in range(2):
            rick.battlefield.append(self._dual(make_card))
        for _ in range(4):
            rick.battlefield.append(make_card("Island", type_line="Basic Land â€” Island",
                                              oracle_text="{T}: Add {U}.",
                                              power="", toughness=""))
        assert rick.tap_sources_for_cost("{4}{U}") is True
        tapped = sum(1 for c in rick.battlefield if c.tapped)
        # Pre-fix: each dual credited 2 mana â†’ 3-4 sources "paid" a 5-MV
        # spell (CR 601.2g underpayment, 3 casts in the cascade game).
        assert tapped == 5, f"5-MV spell paid with only {tapped} sources"

    def test_cannot_pay_three_with_two_duals(self, game, make_card):
        rick = game.players[0]
        for _ in range(2):
            rick.battlefield.append(self._dual(make_card))
        # Two or-duals = 2 mana, not 4: a 3-MV cost must be unpayable.
        assert rick.tap_sources_for_cost("{2}{U}") is False


# ---------------------------------------------------------------------------
# B1 â€” 2026 "this creature" templating: Blood Artist fires again
# ---------------------------------------------------------------------------

class TestBloodArtist2026Wording:
    def test_drain_fires_on_other_creature_death(self, rules, game, make_card):
        rick, claude = game.players
        artist = make_card("Blood Artist", power="0", toughness="1",
                           oracle_text=BLOOD_ARTIST_2026)
        claude.battlefield.append(artist)
        bear = make_card("Runeclaw Bear")
        from mtg.triggers import _check_dies_triggers_sync
        msgs, _ = _check_dies_triggers_sync(_engine_shim(rules), game, bear, claude)
        assert rick.life == 39, "Blood Artist (2026 wording) never drained â€” dead-code branch"
        assert claude.life == 41

    def test_sees_its_own_death(self, rules, game, make_card):
        rick, claude = game.players
        artist = make_card("Blood Artist", power="0", toughness="1",
                           oracle_text=BLOOD_ARTIST_2026)
        # The dying card itself carries the trigger ("this creature or another").
        from mtg.triggers import _check_dies_triggers_sync
        _check_dies_triggers_sync(_engine_shim(rules), game, artist, claude)
        assert rick.life == 39


# ---------------------------------------------------------------------------
# B2 â€” "a creature an opponent controls dies" scope gate (Massacre Wurm)
# ---------------------------------------------------------------------------

class TestOpponentControlsDiesScope:
    WURM = ("Whenever a creature an opponent controls dies, that player "
            "loses 2 life.")

    def test_no_fire_on_own_creature_death(self, rules, game, make_card):
        rick, claude = game.players
        wurm = make_card("Massacre Wurm", power="6", toughness="5",
                         oracle_text=self.WURM)
        claude.battlefield.append(wurm)
        own_bear = make_card("Runeclaw Bear")
        from mtg.triggers import _check_dies_triggers_sync
        _check_dies_triggers_sync(_engine_shim(rules), game, own_bear, claude)
        # Pre-fix: fired on the controller's OWN death and the misfire's
        # life-loss ended a game from 1 â†’ 0.
        assert rick.life == 40 and claude.life == 40, \
            "Wurm fired on its controller's own creature dying"


# ---------------------------------------------------------------------------
# B-verdict â€” CR 903.9b: commander deaths fire dies triggers
# ---------------------------------------------------------------------------

class TestCommanderDeathFiresTriggers:
    def test_blood_artist_sees_commander_die(self, rules, game, make_card):
        rick, claude = game.players
        artist = make_card("Blood Artist", power="0", toughness="1",
                           oracle_text=BLOOD_ARTIST_2026)
        claude.battlefield.append(artist)
        cmdr = make_card("Tymna the Weaver", is_commander=True, owner_index=0,
                         power="2", toughness="2")
        cmdr.damage_marked = 5
        rick.battlefield.append(cmdr)
        rules.process_state_based_actions(game)
        assert cmdr in rick.command_zone  # zone choice still honored
        # CR 903.9b (2020+): the commander DIED on the way â€” it must be
        # queued for dies-trigger processing (the engine-side drain consumes
        # game._recently_died; the clientless rules layer just queues).
        assert any(c is cmdr for c, _p in game._recently_died), \
            "commander death suppressed dies triggers (pre-2020 rules)"


# ---------------------------------------------------------------------------
# B4 â€” command-zone round trip is a clean object
# ---------------------------------------------------------------------------

class TestResetBattlefieldState:
    def test_counters_and_binding_cleared(self, make_card):
        c = make_card("Sythis, Harvest's Hand", is_commander=True)
        c.counters["+1/+1"] = 2
        c._reanimated_by_aura_id = "aura_123"
        c.reset_battlefield_state()
        assert c.counters == {}, "counters survived the command-zone round trip"
        assert c._reanimated_by_aura_id is None, \
            "stale Animate Dead binding survived â€” re-cast commander was insta-sacrificed"


# ---------------------------------------------------------------------------
# B5d â€” Living Death must not return transformed back faces (CR 712.4a)
# ---------------------------------------------------------------------------

class TestLivingDeathTransformedFilter:
    def test_back_face_saga_not_returned(self, rules, game, make_card):
        claude = game.players[1]
        orochi = make_card("Kirin-Touched Orochi", power="1", toughness="1")
        orochi._transformed = True  # saga-table transform (front face lost)
        claude.graveyard.append(orochi)
        legit = make_card("Sun Titan", power="6", toughness="6")
        claude.graveyard.append(legit)
        run(rules, game, {"action": "living_death"})
        assert legit in claude.battlefield
        assert orochi in claude.graveyard, \
            "transformed saga returned as its creature back face (illegal per CR 712.4a)"


# ---------------------------------------------------------------------------
# B7 â€” Ethereal Armor's "for each enchantment you control"
# ---------------------------------------------------------------------------

class TestEtherealArmorForEach:
    def test_bonus_scales_with_enchantments(self, game, make_card):
        rick = game.players[0]
        bear = make_card("Runeclaw Bear")
        armor = make_card("Ethereal Armor", type_line="Enchantment â€” Aura",
                          power="", toughness="",
                          oracle_text=("Enchanted creature gets +1/+1 for each "
                                       "enchantment you control.\n"
                                       "Enchanted creature has first strike."))
        armor.attached_to = bear.id
        bear.attachments = [armor.id]
        other = make_card("Enchantress's Presence", type_line="Enchantment",
                          power="", toughness="")
        rick.battlefield.extend([bear, armor, other])
        # 2 base + 2 (Ethereal Armor counts itself + Presence)
        assert bear.get_effective_power(game) == 4, \
            "for-each multiplier discarded (flat +1/+1 applied)"


# ---------------------------------------------------------------------------
# B9 â€” constellation watchers exist now
# ---------------------------------------------------------------------------

class TestConstellationWatchers:
    def test_eidolon_draws_on_enchantment_entering(self, rules, game, make_card):
        claude = game.players[1]
        eidolon = make_card("Eidolon of Blossoms", power="2", toughness="2",
                            type_line="Enchantment Creature â€” Spirit",
                            oracle_text=("Constellation â€” Whenever Eidolon of "
                                         "Blossoms or another enchantment you "
                                         "control enters, draw a card."))
        claude.battlefield.append(eidolon)
        claude.library.append(make_card("Some Card", type_line="Instant"))
        entering = make_card("Banishing Light", type_line="Enchantment",
                             power="", toughness="")
        claude.battlefield.append(entering)
        from mtg.triggers import _check_enchantment_etb_watchers
        hand_before = len(claude.hand)
        msgs = _check_enchantment_etb_watchers(_engine_shim(rules), game, claude, entering)
        assert len(claude.hand) == hand_before + 1, "constellation draw never fired"
        assert any("Eidolon" in m for m in msgs)


# ---------------------------------------------------------------------------
# A3/A5/A8/B5c/B6/B10 â€” template-level repros
# ---------------------------------------------------------------------------

class TestDeepDiveTemplates:
    def test_leyline_tyrant_declines_with_no_mana(self, lib, game, make_card):
        claude = game.players[1]
        actions, _ = lib.resolve_dies_trigger(
            "Leyline Tyrant",
            "When this creature dies, you may pay any amount of {R}. When you do, "
            "it deals that much damage to any target.",
            "Leyline Tyrant", 4, 4, "Claude", "Rick",
            game_context={"_controller_player": claude})
        if actions is None:
            pytest.skip("dies-template entry point differs")
        assert actions[0]["action"] == "no_action", \
            "Tier-1.5 should decline the optional payment at zero mana"

    def test_leyline_tyrant_pays_real_red(self, lib, game, make_card):
        claude = game.players[1]
        for _ in range(2):
            claude.battlefield.append(make_card(
                "Mountain", type_line="Basic Land â€” Mountain",
                oracle_text="{T}: Add {R}.", power="", toughness=""))
        actions, _ = lib.resolve_dies_trigger(
            "Leyline Tyrant",
            "When this creature dies, you may pay any amount of {R}.",
            "Leyline Tyrant", 4, 4, "Claude", "Rick",
            game_context={"_controller_player": claude})
        if actions is None:
            pytest.skip("dies-template entry point differs")
        dmg = next(a for a in actions if a["action"] == "deal_damage")
        assert dmg["amount"] == 2
        assert all(c.tapped for c in claude.battlefield if c.is_land()), \
            "payment must tap the red sources"

    def test_twinflame_hallucinated_template_gone(self, lib):
        actions, _ = lib.resolve_etb(
            "Twinflame Tyrant",
            "Flying\nIf a source you control would deal damage to an opponent "
            "or a permanent an opponent controls, it deals double that damage instead.",
            "Claude", "Rick")
        if actions:
            assert all(a.get("action") != "deal_damage" for a in actions), \
                "the hallucinated 'deal 5 damage' Twinflame template is back"

    def test_karlach_template_grants_first_strike(self, lib, game):
        actions, _ = lib.resolve_attack_trigger(
            "Karlach, Fury of Avernus",
            "Whenever you attack, if it's the first combat phase of the turn, "
            "untap all attacking creatures. They gain first strike until end of "
            "turn. After this phase, there is an additional combat phase.",
            "Karlach, Fury of Avernus", 5, "Claude", "Rick",
            game_context={"_game": game})
        assert actions, "Karlach attack template missing"
        assert any(a.get("action") == "grant_keywords" for a in actions)

    def test_lightning_reaver_charge_counter(self, lib):
        actions, _ = lib.resolve_attack_trigger(
            "Lightning Reaver",
            "Whenever this creature deals combat damage to a player, put a "
            "charge counter on it.",
            "Lightning Reaver", 3, "Claude", "Rick", game_context={})
        assert actions
        cnt = next(a for a in actions if a.get("action") == "add_counters")
        assert cnt["counter_type"] == "charge"

    def test_orochi_token_requires_graveyard_exile(self, lib, game, make_card):
        rick, claude = game.players
        # Empty graveyards: no Spirit.
        actions, _ = lib.resolve_attack_trigger(
            "Kirin-Touched Orochi",
            "Whenever this creature attacks, you may exile target creature card "
            "from a graveyard. When you do, create a 1/1 colorless Spirit creature token.",
            "Kirin-Touched Orochi", 1, "Rick", "Claude",
            game_context={"_controller_player": rick, "_opponent_player": claude})
        assert actions[0]["action"] == "no_action", "free Spirit with no exile (reflexive cost)"
        # With a creature in a graveyard: exile + token.
        claude.graveyard.append(make_card("Verduran Enchantress", power="0", toughness="3"))
        actions2, _ = lib.resolve_attack_trigger(
            "Kirin-Touched Orochi",
            "Whenever this creature attacks, you may exile target creature card "
            "from a graveyard. When you do, create a 1/1 colorless Spirit creature token.",
            "Kirin-Touched Orochi", 1, "Rick", "Claude",
            game_context={"_controller_player": rick, "_opponent_player": claude})
        kinds = [a["action"] for a in actions2]
        assert "move_card" in kinds and "create_token" in kinds

    def test_meren_dies_event_is_a_noop(self, lib):
        actions, _ = lib.resolve_dies_trigger(
            "Meren of Clan Nel Toth",
            "At the beginning of your end step, choose target creature card in "
            "your graveyard...",
            "Runeclaw Bear", 2, 2, "Claude", "Rick", game_context={})
        if actions is None:
            pytest.skip("dies-template entry point differs")
        assert all(a["action"] == "no_action" for a in actions), \
            "Meren's end-step return fired from a DIES event (mid-opponent-turn returns)"

    def test_reanimate_charges_real_mv(self, lib, game, make_card):
        rick, claude = game.players
        sakura = make_card("Sakura-Tribe Elder", power="1", toughness="1", cmc=2)
        claude.graveyard.append(sakura)
        actions, _ = lib.resolve_etb(
            "Reanimate",
            "Put target creature card from a graveyard onto the battlefield "
            "under your control. You lose life equal to its mana value.",
            "Claude", "Rick",
            game_context={"_controller_player": claude, "_opponent_player": rick,
                          "explicit_target_name": "Sakura-Tribe Elder"})
        by = {a["action"]: a for a in actions}
        assert by["reanimate"]["card"] == "Sakura-Tribe Elder"
        assert by["lose_life"]["amount"] == 2, \
            "Reanimate charged the fallback 5 instead of the target's real MV"

    def test_abrupt_decay_declines_illegal_named_target(self, lib, game, make_card):
        rick, claude = game.players
        abyss = make_card("The Abyss", type_line="World Enchantment",
                          power="", toughness="", cmc=4)
        rick.battlefield.append(abyss)
        rick.battlefield.append(make_card("Runeclaw Bear", cmc=2))
        actions, _ = lib.resolve_etb(
            "Abrupt Decay",
            "This spell can't be countered. Destroy target nonland permanent "
            "with mana value 3 or less.",
            "Claude", "Rick",
            game_context={"_opponent_player": rick,
                          "explicit_target_name": "The Abyss",
                          "best_opponent_nonland_le3": "Runeclaw Bear"})
        assert actions[0]["action"] == "no_action", \
            "Decay silently retargeted off an illegal MV-4 named target"

    def test_beast_within_falls_back_to_lands(self, lib, game, make_card):
        rick, claude = game.players
        forest = make_card("Forest", type_line="Basic Land â€” Forest",
                           power="", toughness="", cmc=0)
        rick.battlefield.append(forest)
        actions, _ = lib.resolve_etb(
            "Beast Within",
            "Destroy target permanent. Its controller creates a 3/3 green Beast creature token.",
            "Claude", "Rick",
            game_context={"best_opponent_any_permanent": "Forest"})
        destroy = next(a for a in actions if a["action"] == "destroy")
        assert destroy["card"] == "Forest", \
            "land-only board: destroy half fizzled while the 3/3 was still granted"


# ---------------------------------------------------------------------------
# B3 â€” TEST-FIRST: FS+deathtouch BLOCKER must kill a vanilla attacker in the
# FS step and survive (the deep-dive saw it deal ZERO in both steps)
# ---------------------------------------------------------------------------

class TestFirstStrikeDeathtouchBlocker:
    def _combat(self, rules, game, make_card, debuffed):
        rick, claude = game.players
        attacker = make_card("Mother of Runes", power="1", toughness="1")
        attacker.attacking = True
        rick.battlefield.append(attacker)
        if debuffed:
            blocker = make_card("Glissa, the Traitor", power="3", toughness="3",
                                keywords=["First strike", "Deathtouch"])
            blocker.power_modifier = -2
            blocker.toughness_modifier = -2
        else:
            blocker = make_card("Glissa, the Traitor", power="1", toughness="1",
                                keywords=["First strike", "Deathtouch"])
        claude.battlefield.append(blocker)
        game.attackers = [attacker.id]
        game.blockers = {attacker.id: [blocker.id]}
        game.active_player_index = 0
        rules.resolve_combat_damage(game)
        return attacker, blocker, rick, claude

    def test_plain_1_1_fs_dt_blocker(self, rules, game, make_card):
        attacker, blocker, rick, claude = self._combat(rules, game, make_card, debuffed=False)
        assert attacker not in rick.battlefield, \
            "FS+DT blocker dealt no damage â€” vanilla attacker survived"
        assert blocker in claude.battlefield, \
            "blocker died to an attacker that should already be dead (CR 510.1)"

    def test_debuffed_3_3_fs_dt_blocker(self, rules, game, make_card):
        # The exact game shape: 3/3 at -2/-2 (Massacre Wurm) blocking a 1/1.
        attacker, blocker, rick, claude = self._combat(rules, game, make_card, debuffed=True)
        assert attacker not in rick.battlefield
        assert blocker in claude.battlefield
