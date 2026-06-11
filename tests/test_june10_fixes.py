"""Repros for the June 10, 2026 audit fix sprint (30 verified findings).

Each test is the failing repro for one verified bug, committed alongside the
fix per the repro-test policy. References (C1..C8, V3..V31) match the audit
report's finding IDs.

Setup matches the suite convention: bare dataclasses + clientless
RulesEngine, no Discord/LLM/network. The Tier-3-only paths (C2 escalation,
C3 positional pairing at the executors) are integration-shaped and verified
by next-batch greps instead.
"""
import asyncio
from types import SimpleNamespace

import pytest

from mtg.actions import execute_action_on_state
from mtg.helpers import burst_dedup_key, clamp_noop_reason, command_zone_owner


def run(rules, game, action):
    return execute_action_on_state(rules, game, dict(action))


def _engine_shim(rules):
    """Minimal stand-in for GameEngine where trigger code only needs
    `.rules` (template execution + centralized life gain)."""
    return SimpleNamespace(rules=rules)


# ---------------------------------------------------------------------------
# C1 — mana over-tap: remaining_needs never decremented
# ---------------------------------------------------------------------------

class TestManaOverTap:
    def _mountain(self, make_card):
        return make_card("Mountain", type_line="Basic Land — Mountain",
                         oracle_text="{T}: Add {R}.", power="", toughness="")

    def test_one_mana_spell_taps_exactly_one_source(self, game, make_card):
        rick = game.players[0]
        for _ in range(8):
            rick.battlefield.append(self._mountain(make_card))
        assert rick.tap_sources_for_cost("{R}") is True
        tapped = sum(1 for c in rick.battlefield if c.tapped)
        # Pre-fix: all 8 Mountains tapped for a 1-mana Bolt.
        assert tapped == 1, f"1-mana spell tapped {tapped} sources"

    def test_two_colored_pips_tap_two(self, game, make_card):
        rick = game.players[0]
        for _ in range(6):
            rick.battlefield.append(self._mountain(make_card))
        assert rick.tap_sources_for_cost("{R}{R}") is True
        assert sum(1 for c in rick.battlefield if c.tapped) == 2


# ---------------------------------------------------------------------------
# V5 — Phyrexian mana double-payment (life AND mana)
# ---------------------------------------------------------------------------

class TestPhyrexianMana:
    def test_life_payment_taps_no_sources(self, game, make_card):
        rick = game.players[0]
        for _ in range(2):
            rick.battlefield.append(make_card(
                "Island", type_line="Basic Land — Island",
                oracle_text="{T}: Add {U}.", power="", toughness=""))
        ok = rick.tap_sources_for_cost("{U/P}", pay_phyrexian_with_life=True)
        assert ok is True
        assert rick.life == 38  # paid 2 life
        tapped = sum(1 for c in rick.battlefield if c.tapped)
        # Pre-fix: 2 life AND 2 Islands (CR 107.4f is either/or).
        assert tapped == 0, f"Phyrexian life payment also tapped {tapped} sources"


# ---------------------------------------------------------------------------
# V3 — partner color identity collapses when a commander leaves owner's zones
# ---------------------------------------------------------------------------

class TestCommanderIdentityCache:
    def test_identity_survives_commander_theft(self, game, make_card):
        rick, claude = game.players
        thrasios = make_card("Thrasios, Triton Hero", is_commander=True,
                             color_identity=["G", "U"], owner_index=0)
        tymna = make_card("Tymna the Weaver", is_commander=True,
                          color_identity=["W", "B"], owner_index=0)
        rick.command_zone.extend([thrasios, tymna])
        assert rick._get_commander_colors() == ["W", "U", "B", "G"]
        # Animate Dead steals Tymna onto the OPPONENT's battlefield: she is
        # now in none of Rick's zones. CR 903.4 — identity must not change.
        rick.command_zone.remove(tymna)
        claude.battlefield.append(tymna)
        assert rick._get_commander_colors() == ["W", "U", "B", "G"]


# ---------------------------------------------------------------------------
# C7 — dead commander goes to the OWNER's command zone (CR 903.9a)
# ---------------------------------------------------------------------------

class TestStolenCommanderDeath:
    def test_helper_resolves_owner(self, game, make_card):
        rick, claude = game.players
        tymna = make_card("Tymna the Weaver", is_commander=True, owner_index=0)
        assert command_zone_owner(game, tymna, claude) is rick
        # Missing/invalid owner_index falls back to the supplied player.
        nameless = make_card("Generic Commander", is_commander=True)
        nameless.owner_index = None
        assert command_zone_owner(game, nameless, claude) is claude

    def test_sba_death_returns_to_owner_zone(self, rules, game, make_card):
        rick, claude = game.players
        tymna = make_card("Tymna the Weaver", is_commander=True, owner_index=0,
                          power="2", toughness="2")
        tymna.damage_marked = 5  # lethal
        claude.battlefield.append(tymna)  # stolen — dies under thief's control
        rules.process_state_based_actions(game)
        assert tymna in rick.command_zone, "commander went to the controller's zone, not the owner's"
        assert tymna not in claude.command_zone
        assert tymna not in claude.battlefield


# ---------------------------------------------------------------------------
# V13 + C6 — single-target destroy honors undying; the return is a clean
# new object (no combat state, enters tapped, own ETB re-fires)
# ---------------------------------------------------------------------------

class TestDestroyUndying:
    def test_destroy_returns_undying_creature(self, rules, game, make_card):
        rick, claude = game.players
        messenger = make_card(
            "Geralf's Messenger", keywords=["Undying"],
            power="3", toughness="2",
            oracle_text=("Geralf's Messenger enters the battlefield tapped.\n"
                         "When Geralf's Messenger enters the battlefield, "
                         "target opponent loses 2 life.\n"
                         "Undying"))
        claude.battlefield.append(messenger)
        msg = run(rules, game, {"action": "destroy", "card": "Geralf's Messenger"})
        assert messenger in claude.battlefield, "undying creature stayed dead on single-target destroy"
        assert messenger not in claude.graveyard
        assert messenger.counters.get("+1/+1", 0) == 1
        assert "undying" in msg.lower()
        # C6: the return is a NEW object — enters tapped per its own text.
        assert messenger.tapped is True

    def test_destroy_with_counter_stays_dead(self, rules, game, make_card):
        claude = game.players[1]
        messenger = make_card("Geralf's Messenger", keywords=["Undying"],
                              power="3", toughness="2", oracle_text="Undying")
        messenger.counters["+1/+1"] = 1  # undying does not apply (CR 702.92a)
        claude.battlefield.append(messenger)
        run(rules, game, {"action": "destroy", "card": "Geralf's Messenger"})
        assert messenger in claude.graveyard

    def test_shield_counter_absorbs_destroy(self, rules, game, make_card):
        claude = game.players[1]
        warden = make_card("Sanctuary Warden", power="5", toughness="5")
        warden.counters["shield"] = 2
        claude.battlefield.append(warden)
        msg = run(rules, game, {"action": "destroy", "card": "Sanctuary Warden"})
        assert warden in claude.battlefield
        assert warden.counters["shield"] == 1
        assert "shield" in msg.lower()

    def test_finalize_clears_combat_state_and_refires_etb(self, rules, game, make_card):
        from mtg.sba import _finalize_death_save_return
        rick, claude = game.players
        messenger = make_card(
            "Geralf's Messenger", keywords=["Undying"],
            power="3", toughness="2",
            oracle_text=("Geralf's Messenger enters the battlefield tapped.\n"
                         "When Geralf's Messenger enters the battlefield, "
                         "target opponent loses 2 life.\nUndying"))
        messenger.attacking = True
        claude.battlefield.append(messenger)
        game.attackers = [messenger.id]
        game.blockers = {messenger.id: ["blk_1"]}
        rick_life_before = rick.life
        _finalize_death_save_return(rules, game, claude, messenger, "UNDYING")
        # CR 508.1a: the returned object was never declared as an attacker.
        assert messenger.attacking is False
        assert messenger.id not in game.attackers
        assert messenger.id not in game.blockers
        assert messenger.tapped is True  # enters tapped
        # The self-ETB drain re-fires on the new object (CR 603.6a).
        assert rick.life == rick_life_before - 2


# ---------------------------------------------------------------------------
# C5 — first-strike blocker must not deal damage in both steps (CR 510.5)
# ---------------------------------------------------------------------------

class TestFirstStrikeBlocker:
    def test_fs_blocker_deals_once(self, rules, game, make_card):
        rick, claude = game.players
        attacker = make_card("Elephant", power="3", toughness="3")
        attacker.attacking = True
        rick.battlefield.append(attacker)
        danitha = make_card("Danitha Capashen, Paragon", power="2", toughness="2",
                            keywords=["First strike", "Lifelink", "Vigilance"])
        claude.battlefield.append(danitha)
        game.attackers = [attacker.id]
        game.blockers = {attacker.id: [danitha.id]}
        game.active_player_index = 0
        rules.resolve_combat_damage(game)
        # Pre-fix: Danitha dealt 2 in the FS step AND 2 in the regular step —
        # the 3/3 died with 4 marked and lifelink credited twice.
        assert attacker in rick.battlefield, "3/3 died to a 2-power FS blocker (double-dip)"
        assert attacker.damage_marked == 2
        assert claude.life == 42, f"lifelink credited {claude.life - 40}, expected 2"


# ---------------------------------------------------------------------------
# V23 — pump_all_creatures player="all" + negative pump runs SBA
# ---------------------------------------------------------------------------

class TestSymmetricPump:
    def test_all_players_negative_pump_kills_both_sides(self, rules, game, make_card):
        rick, claude = game.players
        r_bird = make_card("Birds of Paradise", power="0", toughness="1")
        c_artist = make_card("Blood Artist", power="0", toughness="1")
        rick.battlefield.append(r_bird)
        claude.battlefield.append(c_artist)
        run(rules, game, {"action": "pump_all_creatures", "player": "all",
                          "power": -2, "toughness": -2,
                          "duration": "end_of_turn", "source": "Toxic Deluge"})
        # Pre-fix: no "player" key → find_player("") → silent no-op; 0/1s
        # survived a -4/-4 Deluge and blocked the same turn.
        assert r_bird not in rick.battlefield
        assert c_artist not in claude.battlefield

    def test_toxic_deluge_template_uses_x_and_all(self, lib):
        actions, _desc = lib.resolve_etb(
            "Toxic Deluge",
            "Pay X life. Each creature gets -X/-X until end of turn.",
            "Rick", "Claude", game_context={"x_value": 2})
        by_type = {a["action"]: a for a in actions}
        assert by_type["lose_life"]["amount"] == 2
        pump = by_type["pump_all_creatures"]
        assert pump["player"] == "all"
        assert pump["power"] == -2 and pump["toughness"] == -2


# ---------------------------------------------------------------------------
# V8 — non-cast battlefield entry initializes planeswalker loyalty
# ---------------------------------------------------------------------------

class TestFreeCastPlaneswalkerLoyalty:
    def test_move_card_sets_loyalty(self, rules, game, make_card):
        rick = game.players[0]
        teferi = make_card("Teferi, Time Raveler",
                           type_line="Legendary Planeswalker — Teferi",
                           loyalty="4", power="", toughness="")
        rick.library.append(teferi)
        run(rules, game, {"action": "move_card", "card": "Teferi, Time Raveler",
                          "from_zone": "library", "to_zone": "battlefield",
                          "player": "Rick"})
        assert teferi in rick.battlefield
        # Pre-fix: loyalty_counters stayed 0 and the PW died to SBA before
        # any player-visible line existed.
        assert teferi.loyalty_counters == 4


# ---------------------------------------------------------------------------
# V7 — reanimate honors "your graveyard" restriction
# ---------------------------------------------------------------------------

class TestReanimateOwnGraveyard:
    def test_own_graveyard_flag_restricts_pool(self, rules, game, make_card):
        rick, claude = game.players
        rick.graveyard.append(make_card("Big Opposing Titan", power="8", toughness="8", cmc=8))
        own = make_card("Small Own Creature", power="2", toughness="2", cmc=2)
        claude.graveyard.append(own)
        run(rules, game, {"action": "reanimate", "player": "Claude",
                          "own_graveyard": True})
        assert own in claude.battlefield, "own-graveyard reanimate skipped the controller's creature"
        assert game.players[0].graveyard[0].name == "Big Opposing Titan"

    def test_dread_return_template_emits_own_graveyard(self, lib):
        actions, _ = lib.resolve_etb(
            "Dread Return",
            "Return target creature card from your graveyard to the battlefield.",
            "Claude", "Rick",
            game_context={"best_own_graveyard_creature": "Sun Titan",
                          "best_graveyard_creature": "Opposing Bomb"})
        assert actions[0]["action"] == "reanimate"
        assert actions[0]["card"] == "Sun Titan"
        assert actions[0].get("own_graveyard") is True


# ---------------------------------------------------------------------------
# V16 — "nontoken creature dies" must not fire for token deaths
# ---------------------------------------------------------------------------

class TestNontokenDiesTrigger:
    def _setup(self, game, make_card):
        claude = game.players[1]
        reaper = make_card(
            "Midnight Reaper", power="3", toughness="2",
            oracle_text=("Whenever a nontoken creature you control dies, "
                         "Midnight Reaper deals 1 damage to you and you draw a card."))
        claude.battlefield.append(reaper)
        return claude, reaper

    def test_token_death_does_not_fire(self, rules, game, make_card):
        from mtg.triggers import _check_dies_triggers_sync
        claude, _ = self._setup(game, make_card)
        token = make_card("Faerie Rogue", power="1", toughness="1")
        token.is_token = True
        hand_before = len(claude.hand)
        msgs, _unh = _check_dies_triggers_sync(_engine_shim(rules), game, token, claude)
        assert len(claude.hand) == hand_before, "Reaper drew off a TOKEN death"
        assert not any("Midnight Reaper" in m for m in msgs)

    def test_nontoken_death_fires_draw_and_damage(self, rules, game, make_card):
        from mtg.triggers import _check_dies_triggers_sync
        claude, _ = self._setup(game, make_card)
        claude.library.append(make_card("Some Draw", type_line="Instant"))
        bear = make_card("Runeclaw Bear")
        life_before = claude.life
        hand_before = len(claude.hand)
        _check_dies_triggers_sync(_engine_shim(rules), game, bear, claude)
        assert len(claude.hand) == hand_before + 1
        # V16 second half: the "deals 1 damage to you" rider was dropped by
        # the generic pattern; the dedicated template now applies it.
        assert claude.life == life_before - 1


# ---------------------------------------------------------------------------
# V26 — "whenever you gain life" triggers (Vito / Heliod / Pridemate)
# ---------------------------------------------------------------------------

class TestGainLifeTriggers:
    def test_vito_drains_on_gain(self, rules, game, make_card):
        rick, claude = game.players
        vito = make_card("Vito, Thorn of the Dusk Rose", power="1", toughness="3",
                         oracle_text="Whenever you gain life, each opponent loses that much life.")
        claude.battlefield.append(vito)
        ok, amt, _ = rules._apply_life_gain(game, claude, 3, source_name="Lifelink")
        assert ok and amt == 3
        assert claude.life == 43
        assert rick.life == 37, "Vito never drained on a real gain"
        assert any("Vito" in m for m in game._pending_messages)

    def test_pridemate_counters_on_gain(self, rules, game, make_card):
        claude = game.players[1]
        pridemate = make_card("Ajani's Pridemate", power="2", toughness="2",
                              oracle_text="Whenever you gain life, put a +1/+1 counter on this creature.")
        claude.battlefield.append(pridemate)
        rules._apply_life_gain(game, claude, 2, source_name="test")
        assert pridemate.counters.get("+1/+1", 0) == 1

    def test_no_recursion_on_gain_inside_gain(self, rules, game, make_card):
        # Re-entrancy guard: a gain fired from inside a gain trigger must not
        # recurse forever (Vito + lifelink interplay).
        claude = game.players[1]
        vito = make_card("Vito, Thorn of the Dusk Rose", power="1", toughness="3",
                         oracle_text="Whenever you gain life, each opponent loses that much life.")
        claude.battlefield.append(vito)
        game._in_gain_life_triggers = True  # simulate nested context
        rules._apply_life_gain(game, claude, 2, source_name="nested")
        assert game.players[0].life == 40  # inner scan skipped
        game._in_gain_life_triggers = False


# ---------------------------------------------------------------------------
# V22 — dies-trigger drains route gains through the centralized path
# ---------------------------------------------------------------------------

class TestDiesDrainUsesCentralGain:
    def test_blood_artist_gain_feeds_gain_triggers(self, rules, game, make_card):
        # Routing through _apply_life_gain means a gain-trigger (Vito) now
        # sees Blood Artist's gain — proving the naked `+=` is gone.
        rick, claude = game.players
        artist = make_card("Blood Artist", power="0", toughness="1",
                           oracle_text="Whenever Blood Artist or another creature dies, "
                                       "target player loses 1 life and you gain 1 life.")
        vito = make_card("Vito, Thorn of the Dusk Rose", power="1", toughness="3",
                         oracle_text="Whenever you gain life, each opponent loses that much life.")
        claude.battlefield.extend([artist, vito])
        bear = make_card("Runeclaw Bear")
        from mtg.triggers import _check_dies_triggers_sync
        _check_dies_triggers_sync(_engine_shim(rules), game, bear, claude)
        assert claude.life == 41          # Blood Artist gain applied
        assert rick.life == 40 - 1 - 1    # 1 (Blood Artist) + 1 (Vito saw the gain)


# ---------------------------------------------------------------------------
# V18 — inline anthem fallback ignores activated/until-EOT lines
# ---------------------------------------------------------------------------

class TestAnthemFallbackStaticOnly:
    def test_castle_embereth_is_not_an_anthem(self, game, make_card):
        rick = game.players[0]
        castle = make_card(
            "Castle Embereth", type_line="Land — Castle", power="", toughness="",
            oracle_text=("Castle Embereth enters the battlefield tapped unless you control a Mountain.\n"
                         "{T}: Add {R}.\n"
                         "{1}{R}{R}, {T}: Creatures you control get +1/+0 until end of turn."))
        bear = make_card("Runeclaw Bear")
        rick.battlefield.extend([castle, bear])
        # Pre-fix: every creature read +1/+0 with ZERO activations.
        assert bear.get_effective_power(game) == 2

    def test_real_anthem_still_applies(self, game, make_card):
        rick = game.players[0]
        anthem = make_card("Glorious Anthem", type_line="Enchantment",
                           power="", toughness="",
                           oracle_text="Creatures you control get +1/+1.")
        bear = make_card("Runeclaw Bear")
        rick.battlefield.extend([anthem, bear])
        assert bear.get_effective_power(game) == 3


# ---------------------------------------------------------------------------
# V24 — Serra Ascendant's conditional +5/+5 and flying
# ---------------------------------------------------------------------------

class TestSerraAscendant:
    def _serra(self, make_card):
        return make_card(
            "Serra Ascendant", power="1", toughness="1", keywords=["Lifelink"],
            oracle_text=("Lifelink\n"
                         "As long as you have 30 or more life, this creature "
                         "gets +5/+5 and has flying."))

    def test_buff_active_at_30_plus(self, game, make_card):
        rick = game.players[0]  # commander game: 40 life
        serra = self._serra(make_card)
        rick.battlefield.append(serra)
        assert serra.get_effective_power(game) == 6
        assert serra.get_effective_toughness(game) == 6
        assert serra.has_keyword("Flying", game=game) is True
        assert serra.has_keyword("Lifelink", game=game) is True

    def test_buff_off_below_30(self, game, make_card):
        rick = game.players[0]
        rick.life = 20
        serra = self._serra(make_card)
        rick.battlefield.append(serra)
        assert serra.get_effective_power(game) == 1
        assert serra.has_keyword("Flying", game=game) is False
        # Lifelink is printed unconditionally — never gated.
        assert serra.has_keyword("Lifelink", game=game) is True


# ---------------------------------------------------------------------------
# Skullclamp class — mixed-sign equipment / aura bonuses
# ---------------------------------------------------------------------------

class TestMixedSignBonuses:
    def test_equipment_plus_minus(self, game, make_card):
        rick = game.players[0]
        clamp = make_card("Skullclamp", type_line="Artifact — Equipment",
                          power="", toughness="",
                          oracle_text="Equipped creature gets +1/-1.\nEquip {1}")
        bear = make_card("Runeclaw Bear")
        bear.attachments = [clamp.id]
        rick.battlefield.extend([clamp, bear])
        assert bear.get_effective_power(game) == 3
        assert bear.get_effective_toughness(game) == 1


# ---------------------------------------------------------------------------
# V19 / V31 helpers — dedup key, noop-reason clamp, owner resolution
# ---------------------------------------------------------------------------

class TestDisplayHelpers:
    def test_distinct_casts_get_distinct_keys(self):
        a = burst_dedup_key("✨ Claude cast **Talisman of Dominance**")
        b = burst_dedup_key("✨ Claude cast **Peregrine Drake**")
        assert a != b, "distinct casts collided — 3rd+ cast per turn would be suppressed"

    def test_draw_burst_still_collapses(self):
        a = burst_dedup_key("🃏 Guardian Project — Rick Deckard draws **Llanowar Elves**")
        b = burst_dedup_key("🃏 Guardian Project — Rick Deckard draws **Craterhoof Behemoth**")
        assert a == b

    def test_numeric_paren_stripped(self):
        a = burst_dedup_key("💀 Syr Konrad: deals 1 damage to Claude (life: 27)")
        b = burst_dedup_key("💀 Syr Konrad: deals 1 damage to Claude (life: 25)")
        assert a == b

    def test_clamp_noop_reason_passes_short(self):
        assert clamp_noop_reason("No creatures to destroy.") == "No creatures to destroy."

    def test_clamp_noop_reason_clamps_cot(self):
        cot = ("Land Tax checks if Rick Deckard controls more lands than Claude. "
               "Rick has 2 lands while Claude has 3 lands. Since Claude does not "
               "control fewer lands, the ability does not trigger. Therefore nothing happens.")
        assert clamp_noop_reason(cot) == "condition not met — no effect"


# ---------------------------------------------------------------------------
# Template library — Land Tax, Drakuseth, extort pattern, Midnight Reaper
# ---------------------------------------------------------------------------

class TestNewTemplates:
    def test_land_tax_fetches_three_basics(self, lib, game, make_card):
        rick, claude = game.players
        for _ in range(2):
            rick.battlefield.append(make_card("Plains", type_line="Basic Land — Plains",
                                              power="", toughness=""))
        for _ in range(4):
            claude.battlefield.append(make_card("Island", type_line="Basic Land — Island",
                                                power="", toughness=""))
        for _ in range(5):
            rick.library.append(make_card("Plains", type_line="Basic Land — Plains",
                                          power="", toughness=""))
        actions, _ = lib.resolve_etb(
            "Land Tax",
            "At the beginning of your upkeep, if an opponent controls more "
            "lands than you, you may search your library for up to three "
            "basic land cards, reveal them, and put them into your hand.",
            "Rick", "Claude",
            game_context={"_controller_player": rick, "_opponent_player": claude},
            event_type="upkeep")
        moves = [a for a in actions if a.get("action") == "move_card"]
        assert len(moves) == 3
        assert all(a["to_zone"] == "hand" for a in moves)

    def test_land_tax_noop_when_not_behind(self, lib, game, make_card):
        rick, claude = game.players
        for _ in range(3):
            rick.battlefield.append(make_card("Plains", type_line="Basic Land — Plains",
                                              power="", toughness=""))
        actions, _ = lib.resolve_etb(
            "Land Tax", "At the beginning of your upkeep, if an opponent controls more lands than you...",
            "Rick", "Claude",
            game_context={"_controller_player": rick, "_opponent_player": claude},
            event_type="upkeep")
        assert actions[0]["action"] == "no_action"

    def test_drakuseth_attack_template_full_damage(self, lib):
        actions, _ = lib.resolve_attack_trigger(
            "Drakuseth, Maw of Flames",
            "Whenever Drakuseth, Maw of Flames attacks, it deals 4 damage to "
            "any target and 3 damage to each of up to two other targets.",
            "Drakuseth, Maw of Flames", 7,
            "Rick", "Claude",
            game_context={"_opponent_creatures": [
                {"name": "Big Blocker", "power": 5},
                {"name": "Small Blocker", "power": 1},
            ]})
        assert actions, "Drakuseth attack template did not match"
        dmg = [a for a in actions if a["action"] == "deal_damage"]
        total = sum(a.get("amount", 0) for a in dmg)
        # 4 to the biggest creature + 3 to the second + 3 to the face = 10
        # (the generic pattern used to capture only the first number: 4).
        assert total == 10
        assert all(a.get("source") for a in dmg), "damage actions missing source (unknown-source class)"


# ---------------------------------------------------------------------------
# Action-vocabulary regression — create_token keywords reach has_defender
# ---------------------------------------------------------------------------

class TestTokenKeywords:
    def test_defender_token_cannot_attack(self, rules, game):
        run(rules, game, {"action": "create_token", "player": "Rick",
                          "name": "Wall", "power": 0, "toughness": 4,
                          "types": "Creature — Wall", "count": 1,
                          "keywords": ["Defender"]})
        wall = next(c for c in game.players[0].battlefield if c.name == "Wall")
        assert wall.has_keyword("Defender")
        assert wall.can_attack(game=game) is False


# ---------------------------------------------------------------------------
# Discard display — never hide exactly one name behind "+1 more"
# ---------------------------------------------------------------------------

class TestDiscardDisplay:
    def test_seven_card_hand_lists_all_names(self, rules, game, make_card):
        claude = game.players[1]
        for i in range(7):
            claude.hand.append(make_card(f"Card Number {i}", type_line="Instant"))
        msg = run(rules, game, {"action": "discard", "player": "Claude", "card": "all"})
        assert "more)" not in msg
        assert "Card Number 6" in msg

    def test_nine_card_hand_collapses(self, rules, game, make_card):
        claude = game.players[1]
        for i in range(9):
            claude.hand.append(make_card(f"Card Number {i}", type_line="Instant"))
        msg = run(rules, game, {"action": "discard", "player": "Claude", "card": "all"})
        assert "(+3 more)" in msg


# ---------------------------------------------------------------------------
# Coverage gap closed by script (2nd consecutive batch with zero events):
# CR 702.19c - trample + deathtouch assigns 1 per blocker, rest tramples
# ---------------------------------------------------------------------------

class TestTrampleDeathtouch:
    def test_cr_702_19c_multi_blocker_assignment(self, rules, game, make_card):
        rick, claude = game.players
        attacker = make_card("Glissa Stand-In", power="5", toughness="5",
                             keywords=["Trample", "Deathtouch"])
        attacker.attacking = True
        rick.battlefield.append(attacker)
        b1 = make_card("Blocker One", power="1", toughness="3")
        b2 = make_card("Blocker Two", power="1", toughness="3")
        claude.battlefield.extend([b1, b2])
        game.attackers = [attacker.id]
        game.blockers = {attacker.id: [b1.id, b2.id]}
        game.active_player_index = 0
        rules.resolve_combat_damage(game)
        # With deathtouch, ANY nonzero damage is lethal assignment (CR
        # 702.19c): 1 to each blocker, the remaining 3 tramples through.
        assert claude.life == 37, (
            f"trampler+deathtoucher should carry 3 to the player, "
            f"opponent at {claude.life}")
        # Both blockers die to deathtouch (SBA 704.5h).
        assert b1 not in claude.battlefield
        assert b2 not in claude.battlefield
        # Attacker took 1+1 retaliation, survives.
        assert attacker in rick.battlefield


# ---------------------------------------------------------------------------
# Coverage gaps from the June 10 audit, closed by script (the autoplay
# matrix went N-A on all of these for a second consecutive batch)
# ---------------------------------------------------------------------------

class TestTotemArmorDestroy:
    def test_umbra_destroyed_instead_of_creature(self, rules, game, make_card):
        claude = game.players[1]
        bear = make_card("Runeclaw Bear")
        umbra = make_card(
            "Bear Umbra", type_line="Enchantment - Aura",
            power="", toughness="",
            oracle_text=("Enchant creature. Enchanted creature gets +2/+2. "
                         "Umbra armor (If enchanted creature would be destroyed, "
                         "instead remove all damage from it and destroy this Aura.)"))
        umbra.attached_to = bear.id
        bear.attachments = [umbra.id]
        claude.battlefield.extend([bear, umbra])
        msg = run(rules, game, {"action": "destroy", "card": "Runeclaw Bear"})
        # CR 702.88e (umbra armor): the AURA dies, the creature survives.
        assert bear in claude.battlefield, "umbra armor did not save the creature"
        assert umbra not in claude.battlefield
        assert "umbra" in msg.lower() or "instead" in msg.lower()


class TestLivingDeathDiesTriggers:
    def test_sacrificed_creatures_queue_dies_triggers(self, rules, game, make_card):
        # May 30 F-LD1 branch the matrix never reached: the trigger source
        # itself is among the sacrificed. The fix queues _recently_died for
        # the dispatcher; this pins the queue contents + the zone swap.
        rick, claude = game.players
        artist = make_card("Blood Artist", power="0", toughness="1",
                           oracle_text="Whenever Blood Artist or another creature "
                                       "dies, target player loses 1 life and you gain 1 life.")
        bear = make_card("Runeclaw Bear")
        claude.battlefield.extend([artist, bear])
        returned = make_card("Sun Titan", power="6", toughness="6")
        claude.graveyard.append(returned)
        game._recently_died.clear()
        run(rules, game, {"action": "living_death"})
        # Sacrificed pair queued for the dies-trigger dispatcher (F-LD1).
        queued_names = {c.name for c, _p in game._recently_died}
        assert queued_names == {"Blood Artist", "Runeclaw Bear"}
        # Zone swap: prior-graveyard creature returns; sacrificed stay dead.
        assert returned in claude.battlefield
        assert artist in claude.graveyard
        assert bear in claude.graveyard


class TestMassFlicker:
    def test_flicker_resets_state_and_skips_stolen(self, rules, game, make_card):
        # Yorion-class mass flicker: damage/taps reset, and CR 110.1 -
        # "permanents you OWN" excludes stolen permanents.
        rick, claude = game.players
        own = make_card("Cloudblazer", power="2", toughness="2",
                        oracle_text="When Cloudblazer enters the battlefield, "
                                    "you gain 2 life and draw two cards.")
        own.owner_index = 1
        own.damage_marked = 1
        own.tapped = True
        stolen = make_card("Agent Victim", power="3", toughness="3")
        stolen.owner_index = 0  # owned by Rick, controlled by Claude
        stolen.damage_marked = 2
        claude.battlefield.extend([own, stolen])
        run(rules, game, {"action": "mass_flicker", "player": "Claude",
                          "count": 5, "require_ownership": True})
        # Owned permanent round-tripped with a clean slate.
        assert own in claude.battlefield
        assert own.damage_marked == 0
        assert own.tapped is False
        assert own.summoning_sick is True
        # Stolen permanent untouched (not flickered, damage intact).
        assert stolen.damage_marked == 2


# ---------------------------------------------------------------------------
# APNAP both-sides ordering (CR 603.3b) - the last open coverage gap from
# the June 10 audit, closed by extracting the drain-site sort into
# helpers.apnap_order_died (shared by mtg/triggers.py + both mtg/engine.py
# drain sites) and pinning it here.
# ---------------------------------------------------------------------------

class TestApnapBothSidesOrdering:
    def test_nap_deaths_scan_first(self, game, make_card):
        from mtg.helpers import apnap_order_died
        rick, claude = game.players
        game.active_player_index = 0  # Rick is AP
        ap_dead = (make_card("AP Victim"), rick)
        nap_dead = (make_card("NAP Victim"), claude)
        # Insertion order AP-first must invert: NAP scans (= resolves) first.
        ordered = apnap_order_died([ap_dead, nap_dead], game)
        assert ordered[0][1] is claude and ordered[1][1] is rick
        # Already NAP-first stays NAP-first.
        ordered2 = apnap_order_died([nap_dead, ap_dead], game)
        assert ordered2[0][1] is claude

    def test_stable_within_each_player(self, game, make_card):
        from mtg.helpers import apnap_order_died
        rick, claude = game.players
        game.active_player_index = 0
        n1 = (make_card("NAP First"), claude)
        n2 = (make_card("NAP Second"), claude)
        a1 = (make_card("AP First"), rick)
        a2 = (make_card("AP Second"), rick)
        ordered = apnap_order_died([a1, n1, a2, n2], game)
        names = [c.name for c, _p in ordered]
        # NAP block first, each block preserving insertion order (the
        # controller's chosen relative order, CR 603.3b).
        assert names == ["NAP First", "NAP Second", "AP First", "AP Second"]

    def test_active_player_one_flips_direction(self, game, make_card):
        from mtg.helpers import apnap_order_died
        rick, claude = game.players
        game.active_player_index = 1  # Claude is AP now
        pair_r = (make_card("Rick Dead"), rick)
        pair_c = (make_card("Claude Dead"), claude)
        ordered = apnap_order_died([pair_c, pair_r], game)
        assert ordered[0][1] is rick  # Rick is NAP -> first
