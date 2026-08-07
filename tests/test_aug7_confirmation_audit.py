"""Mutation-sensitive regressions from the Aug 7, 2026 confirmation-batch
audit (sha=caf8e6d corpus, 160 games).

Finding IDs (CO-*/A-*/B-*/C-*) reference the confirmation-audit ledger in
CLAUDE.md. Every pin exercises production behavior through the same
functions the live paths call (the pin-shape-reachability lessons): the
real replacement registrations, the real dies scan, the real equipment
bonus reader, the real cast-trigger matcher, real cache oracle text.
"""

import json
import re
from pathlib import Path

import pytest

from mtg.constants import Phase, Zone
from mtg.models import Card, GameState, Player
from mtg.rules_engine import RulesEngine

_CACHE = json.loads(
    (Path(__file__).resolve().parent.parent / "data" / "card_data_cache.json")
    .read_text(encoding="utf-8"))


def _cached_text(name: str) -> str:
    entry = _CACHE[name.lower()]
    return entry.get("oracle_text", "") or ""


def _cached_card(make_card, name: str, **over):
    entry = _CACHE[name.lower()]
    defaults = dict(
        type_line=entry.get("type_line", ""),
        oracle_text=entry.get("oracle_text", "") or "",
        power=entry.get("power") or "0",
        toughness=entry.get("toughness") or "0",
    )
    defaults.update(over)
    return make_card(entry.get("name", name), **defaults)


# ---------------------------------------------------------------------------
# B-1: Doubling Season / Parallel Lives controller conditions (were stubs)
# ---------------------------------------------------------------------------

class TestDoublingSeasonControllerGate:
    def _effects(self, factory_name, controller):
        from rules import replacement as rep
        return rep._NAMED_CARD_REPLACEMENTS[factory_name]("src1", controller)

    def _event(self, event_type, affected, amount):
        from rules.replacement import GameEvent
        return GameEvent(event_type=event_type, affected_player=affected,
                         amount=amount)

    def test_counters_double_only_for_the_controller(self):
        from rules.replacement import ReplacementEngine, EventType
        eng = ReplacementEngine()
        for eff in self._effects("doubling season", "Rick"):
            eng.add_effect(eff)
        own = eng.process_event_sync(
            self._event(EventType.COUNTER_PLACED, "Rick", 1))
        assert own.amount == 2, "own counters double"
        # The game-deciding bug: Rick's Season doubled counters on QWEN's
        # Predator Ooze (game_1535228613341872148).
        opp = eng.process_event_sync(
            self._event(EventType.COUNTER_PLACED, "Claude", 1))
        assert opp.amount == 1, "opponent counters must NOT double"

    def test_tokens_double_only_for_the_controller(self):
        from rules.replacement import ReplacementEngine, EventType
        eng = ReplacementEngine()
        for eff in self._effects("parallel lives", "Rick"):
            eng.add_effect(eff)
        own = eng.process_event_sync(
            self._event(EventType.TOKEN_CREATED, "Rick", 2))
        assert own.amount == 4
        opp = eng.process_event_sync(
            self._event(EventType.TOKEN_CREATED, "Claude", 2))
        assert opp.amount == 2

    def test_branching_evolution_and_hardened_scales_gated_too(self):
        from rules.replacement import ReplacementEngine, EventType
        eng = ReplacementEngine()
        for eff in self._effects("hardened scales", "Rick"):
            eng.add_effect(eff)
        assert eng.process_event_sync(
            self._event(EventType.COUNTER_PLACED, "Rick", 1)).amount == 2
        assert eng.process_event_sync(
            self._event(EventType.COUNTER_PLACED, "Claude", 1)).amount == 1


# ---------------------------------------------------------------------------
# C-1a: the combat-damage self-trigger phrase is negation-aware
# ---------------------------------------------------------------------------

class TestCombatDamagePhraseNegationAware:
    def test_solphim_replacement_text_is_rejected(self):
        from mtg.combat import COMBAT_DAMAGE_SELF_PHRASE
        solphim = _cached_text("Solphim, Mayhem Dominus").lower()
        assert "noncombat damage to an opponent" in solphim  # cache truth
        assert not COMBAT_DAMAGE_SELF_PHRASE.search(solphim), (
            "Solphim's REPLACEMENT wording must not classify as a combat "
            "self-trigger — Tier 3 fabricated 40 damage off this in "
            "game_1535236954705240105")

    def test_real_combat_triggers_still_pass(self):
        from mtg.combat import COMBAT_DAMAGE_SELF_PHRASE
        for name in ("Ohran Frostfang", "Stromkirk Occultist"):
            text = _cached_text(name).lower()
            assert COMBAT_DAMAGE_SELF_PHRASE.search(text), (
                f"{name}'s genuine combat-damage phrase must still match")


# ---------------------------------------------------------------------------
# C-1b + B-4: Solphim registered; source_controller threads through the
# noncombat player-damage funnel (the Tier-2 Skullcrack path)
# ---------------------------------------------------------------------------

class TestSolphimRegistrationAndSourceControllerThreading:
    def _game_with_solphim(self, make_game, make_card):
        game = make_game()
        rick, claude = game.players
        solphim = _cached_card(make_card, "Solphim, Mayhem Dominus")
        rick.battlefield.append(solphim)
        game.register_replacement_effects(solphim, rick.name)
        return game, rick, claude

    def test_noncombat_damage_to_opponent_is_doubled(self, make_game, make_card):
        game, rick, claude = self._game_with_solphim(make_game, make_card)
        rules = RulesEngine(None)
        # B-4 shape: a Tier-2 spell mid-resolution — not on any battlefield,
        # not on the stack — with the caster passed explicitly.
        dealt = rules._apply_noncombat_damage_to_player(
            game, claude, 3, "Skullcrack", source_controller=rick.name)
        assert dealt == 6, (
            "Solphim doubles noncombat damage from its controller's sources; "
            "without the B-4 source_controller threading the condition never "
            "fired for spells already off the stack")

    def test_combat_damage_is_not_doubled_by_solphim(self, make_game, make_card):
        game, rick, claude = self._game_with_solphim(make_game, make_card)
        rules = RulesEngine(None)
        atk = make_card("Grizzly Bears", power="3", toughness="3")
        rick.battlefield.append(atk)
        dealt = rules._apply_combat_damage_to_player(game, claude, 3, atk)
        assert dealt == 3, "Solphim is NONCOMBAT only"

    def test_own_side_damage_untouched(self, make_game, make_card):
        game, rick, claude = self._game_with_solphim(make_game, make_card)
        rules = RulesEngine(None)
        dealt = rules._apply_noncombat_damage_to_player(
            game, rick, 3, "Pyroclasm-ish", source_controller=rick.name)
        assert dealt == 3, "damage to Solphim's own controller is not doubled"


# ---------------------------------------------------------------------------
# A-1: dies-source snapshot at queue time for EVERY death
# ---------------------------------------------------------------------------

class TestDiesSourceSnapshot:
    def test_snapshot_populated_at_queue_and_excludes_late_entrants(
            self, make_game, make_card):
        from mtg.triggers import queue_death, _check_dies_triggers_sync
        game = make_game()
        rick, claude = game.players
        mystic = make_card("Elvish Mystic", power="1", toughness="1")
        rick.battlefield.append(mystic)
        # The death is queued while ONLY the Mystic is around.
        rick.battlefield.remove(mystic)
        queue_death(game, mystic, rick)
        snap = game._dies_source_ids_by_dead_id.get(mystic.id)
        assert snap is not None and mystic.id in snap
        # Zulaport enters AFTER the death (cast with the sac mana —
        # game_1535222967376674879). It must NOT fire for that death.
        zula = _cached_card(make_card, "Zulaport Cutthroat")
        rick.battlefield.append(zula)
        assert zula.id not in snap
        rules = RulesEngine(None)

        class _Engine:  # minimal engine shim for the scan's engine arg
            pass
        msgs, unhandled = _check_dies_triggers_sync(_Engine(), game, mystic, rick)
        assert not any("Zulaport" in m for m in msgs), (
            "a watcher that entered after the death fired retroactively "
            "(CR 603.3)")
        assert not any("Zulaport" in getattr(c, 'name', '')
                       for c, _t in unhandled)

    def test_batch_mates_ride_the_snapshot(self, make_game, make_card):
        from mtg.triggers import queue_deaths
        game = make_game()
        rick, claude = game.players
        a = make_card("Bear A")
        b = make_card("Bear B")
        # SBA sweeps remove every dying creature BEFORE queueing (the
        # batch-13 Meren fix) — the snapshot must still include batch-mates
        # so CR 603.10 simultaneous-death visibility survives the filter.
        queue_deaths(game, [(a, rick), (b, rick)])
        assert b.id in game._dies_source_ids_by_dead_id[a.id]
        assert a.id in game._dies_source_ids_by_dead_id[b.id]


# ---------------------------------------------------------------------------
# A-2a / A-4: equipment bonus reader — line discipline
# ---------------------------------------------------------------------------

class TestEquipmentBonusLineDiscipline:
    def _bonuses(self, make_game, make_card, equip_name, bearer_type=None):
        game = make_game()
        rick = game.players[0]
        eq = _cached_card(make_card, equip_name)
        bearer = make_card(
            "Bearer", type_line=bearer_type or "Creature — Human",
            power="2", toughness="2")
        bearer.attachments = [eq.id]
        rick.battlefield.extend([bearer, eq])
        return bearer._get_equipment_bonuses(game)

    def test_jitte_grants_nothing_statically(self, make_game, make_card):
        p, t, kws = self._bonuses(make_game, make_card, "Umezawa's Jitte")
        assert (p, t) == (0, 0), (
            "Jitte's +2/+2 lives on a COST-GATED MODAL bullet — the phantom "
            "permanent bonus decided combats in game_1535222945050271766")
        assert kws == []

    def test_loxodon_warhammer_unchanged(self, make_game, make_card):
        p, t, kws = self._bonuses(make_game, make_card, "Loxodon Warhammer")
        assert (p, t) == (3, 0)
        assert set(kws) == {"trample", "lifelink"}

    def test_skullclamp_mixed_sign_survives(self, make_game, make_card):
        p, t, kws = self._bonuses(make_game, make_card, "Skullclamp")
        assert (p, t) == (1, -1)

    def test_shadowspear_loses_its_phantom_grants(self, make_game, make_card):
        p, t, kws = self._bonuses(make_game, make_card, "Shadowspear")
        assert (p, t) == (1, 1)
        assert set(kws) == {"lifelink", "trample"}, (
            "the '{1}:' activated line about OPPONENTS' permanents granted "
            "the bearer hexproof + indestructible")

    def test_champions_helm_hexproof_is_legendary_gated(self, make_game, make_card):
        p, t, kws = self._bonuses(make_game, make_card, "Champion's Helm")
        assert (p, t) == (2, 2)
        assert "hexproof" not in kws, "non-legendary bearer gets no hexproof"
        p2, t2, kws2 = self._bonuses(
            make_game, make_card, "Champion's Helm",
            bearer_type="Legendary Creature — Human Knight")
        assert "hexproof" in kws2, "legendary bearer DOES get hexproof"

    def test_colossus_hammer_never_grants_flying(self, make_game, make_card):
        p, t, kws = self._bonuses(make_game, make_card, "Colossus Hammer")
        assert (p, t) == (10, 10)
        assert "flying" not in kws, "'loses flying' granted flying (inverted)"


# ---------------------------------------------------------------------------
# A-2b: Jitte's charge-counter trigger fires on combat damage, deduped/step
# ---------------------------------------------------------------------------

class TestJitteChargeCounters:
    def test_charge_counters_and_per_step_dedupe(self, make_game, make_card):
        from mtg.combat import fire_equipped_combat_damage_counters
        game = make_game()
        rick = game.players[0]
        jitte = _cached_card(make_card, "Umezawa's Jitte")
        bearer = make_card("Giver of Runes", power="1", toughness="2")
        bearer.attachments = [jitte.id]
        rick.battlefield.extend([bearer, jitte])
        game._equip_charge_fired_ids = set()
        fire_equipped_combat_damage_counters(game, bearer)
        assert jitte.counters.get("charge", 0) == 2, (
            "Jitte's trigger never fired in game_1535222945050271766 — four "
            "unblocked connects, zero counters")
        # Same step: a trample split is ONE damage event → no second fire.
        fire_equipped_combat_damage_counters(game, bearer)
        assert jitte.counters.get("charge", 0) == 2
        # New step (FS → regular): fires again.
        game._equip_charge_fired_ids = set()
        fire_equipped_combat_damage_counters(game, bearer)
        assert jitte.counters.get("charge", 0) == 4


# ---------------------------------------------------------------------------
# B-2: the "dealt damage by this creature this turn dies" premise gate
# ---------------------------------------------------------------------------

class TestPredatorOozePremiseGate:
    def _scan(self, game, dead, dead_owner):
        from mtg.triggers import _check_dies_triggers_sync

        class _Engine:
            pass
        return _check_dies_triggers_sync(_Engine(), game, dead, dead_owner)

    def test_premise_false_skips_the_trigger(self, make_game, make_card):
        game = make_game()
        rick, claude = game.players
        ooze = _cached_card(make_card, "Predator Ooze")
        claude.battlefield.append(ooze)
        dead = make_card("Rampaging Baloths", power="6", toughness="6")
        # Snapshot includes the Ooze (it was present) but the Ooze dealt no
        # damage to the dead creature — the fabricated-premise case.
        game._dies_source_ids_by_dead_id[dead.id] = {ooze.id, dead.id}
        msgs, unhandled = self._scan(game, dead, rick)
        assert not any("Predator Ooze" in getattr(c, 'name', '')
                       for c, _t in unhandled), (
            "Tier 3 fabricated the 'dealt damage by it' premise in "
            "game_1535228613341872148 — the gate must skip the queue")

    def test_premise_true_lets_the_trigger_through(self, make_game, make_card):
        game = make_game()
        rick, claude = game.players
        ooze = _cached_card(make_card, "Predator Ooze")
        claude.battlefield.append(ooze)
        dead = make_card("Rampaging Baloths", power="6", toughness="6")
        game._dies_source_ids_by_dead_id[dead.id] = {ooze.id, dead.id}
        game._creature_combat_damage_by_dealer = {ooze.id: {dead.id}}
        msgs, unhandled = self._scan(game, dead, rick)
        fired = (any("Predator Ooze" in m for m in msgs)
                 or any("Predator Ooze" in getattr(c, 'name', '')
                        for c, _t in unhandled))
        assert fired, "a TRUE premise must still fire/queue the trigger"

    def test_record_populated_by_the_combat_funnel(self, make_game, make_card):
        game = make_game()
        rick, claude = game.players
        rules = RulesEngine(None)
        atk = make_card("Attacker", power="3", toughness="3")
        blk = make_card("Blocker", power="2", toughness="4")
        # A Jitte on the attacker makes the funnel's equipment-trigger call
        # site load-bearing too (A-2b): damaging a BLOCKER must award the
        # charge counters — the shape the player-damage scan can never see.
        jitte = _cached_card(make_card, "Umezawa's Jitte")
        atk.attachments = [jitte.id]
        rick.battlefield.extend([atk, jitte])
        claude.battlefield.append(blk)
        game._equip_charge_fired_ids = set()
        rules._apply_combat_damage_to_creature(game, blk, 3, atk)
        assert blk.id in game._creature_combat_damage_by_dealer.get(atk.id, set())
        assert jitte.counters.get("charge", 0) == 2, (
            "the creature-damage funnel must fire equipment charge triggers")


# ---------------------------------------------------------------------------
# B-3: cast-trigger condition clause anchored at the trigger word
# ---------------------------------------------------------------------------

class TestAshZealotGraveyardGate:
    def _matches(self, make_card, cast_card):
        from mtg.triggers import _spell_matches_cast_trigger
        zealot_text = _cached_text("Ash Zealot")
        # The live sentence shape: keyword line + trigger, no period between
        # (this is exactly what reached _spell_matches_cast_trigger).
        sentence = zealot_text
        return _spell_matches_cast_trigger(sentence, cast_card)

    def test_hand_cast_does_not_fire(self, make_card):
        from mtg.triggers import _spell_matches_cast_trigger
        zealot_text = _cached_text("Ash Zealot")
        assert "First strike" in zealot_text  # the defeating keyword line
        card = make_card("Chandra, Torch of Defiance",
                         type_line="Legendary Planeswalker — Chandra")
        card._cast_from_graveyard = False
        assert _spell_matches_cast_trigger(None, zealot_text.lower(), card) is False, (
            "the comma inside 'First strike, haste' swallowed the condition "
            "clause and Ash Zealot fired on a command-zone cast "
            "(game_1535228623240568872)")

    def test_graveyard_cast_still_fires(self, make_card):
        from mtg.triggers import _spell_matches_cast_trigger
        zealot_text = _cached_text("Ash Zealot")
        card = make_card("Think Twice", type_line="Instant")
        card._cast_from_graveyard = True
        assert _spell_matches_cast_trigger(None, zealot_text.lower(), card) is True


# ---------------------------------------------------------------------------
# B-5: attack-action failures teach instead of "unknown reason"
# ---------------------------------------------------------------------------

class TestAttackActionFeedback:
    def test_error_branch_returns_teaching_message(self, make_game):
        from mtg.ai_turn import _get_action_error
        game = make_game()
        game._last_attack_action_failure = (
            game.turn_number, "no named creature could attack — teach")
        msg = _get_action_error(None, game, 1, {"type": "attack",
                                                "creatures": ["Bear"]})
        assert msg == "no named creature could attack — teach"
        assert game._last_attack_action_failure is None, "consumed on read"

    def test_fallback_message_without_stash(self, make_game):
        from mtg.ai_turn import _get_action_error
        game = make_game()
        msg = _get_action_error(None, game, 1, {"type": "attack"})
        assert "Declare Attackers" in msg
        assert "unknown reason" not in msg


# ---------------------------------------------------------------------------
# C-2 / A-3: combat state stripped on battlefield exit and phase-out
# ---------------------------------------------------------------------------

class TestCombatStateStrips:
    def test_move_card_battlefield_exit_strips_combat_state(
            self, make_game, make_card, rules):
        from mtg.actions import execute_action_on_state
        game = make_game()
        rick = game.players[0]
        atk = make_card("Sun Titan", power="6", toughness="6")
        rick.battlefield.append(atk)
        atk.attacking = True
        game.attackers.append(atk.id)
        execute_action_on_state(rules, game, {
            "action": "move_card", "card": "Sun Titan",
            "from_zone": "battlefield", "to_zone": "exile", "player": "Rick"})
        assert atk.attacking is False, (
            "Eerie Interlude leaked .attacking through the exile→return "
            "round trip (game_1535217860513890324)")
        assert atk.id not in game.attackers

    def test_phase_out_strips_combat_state(self, make_game, make_card, rules):
        from mtg.actions import execute_action_on_state
        game = make_game()
        rick = game.players[0]
        atk = make_card("Soulherder", power="1", toughness="1")
        rick.battlefield.append(atk)
        atk.attacking = True
        game.attackers.append(atk.id)
        execute_action_on_state(rules, game, {
            "action": "phase_out_all", "player": "Rick"})
        assert atk.attacking is False, (
            "Teferi's Protection left phased-out attackers in combat "
            "(CR 506.4, game_1535212572960227388)")
        assert atk.id not in game.attackers


# ---------------------------------------------------------------------------
# C-3: resolve-drop branches stash teaching reasons (consumption side)
# ---------------------------------------------------------------------------

class TestResolveDropReasons:
    def test_error_branch_consumes_the_stash(self, make_game):
        from mtg.ai_turn import _get_action_error
        game = make_game()
        game._last_resolve_drop_reason = (
            game.turn_number, "orphan `resolve` dropped — teach")
        msg = _get_action_error(None, game, 1,
                                {"type": "resolve", "description": "x"})
        assert "orphan `resolve` dropped — teach" in msg

    def test_both_drop_branches_stash_in_source(self):
        # The two branches live mid-way through a large autoplay handler that
        # needs a full cog to invoke; pin the stash presence structurally —
        # the consumption side is covered behaviorally above.
        src = (Path(__file__).resolve().parent.parent / "mtg" /
               "autoplay.py").read_text(encoding="utf-8")
        countered = src.split("prior cast was countered", 1)[1][:800]
        assert "_last_resolve_drop_reason" in countered
        orphan = src.split("action already fired this turn", 1)[1][:800]
        assert "_last_resolve_drop_reason" in orphan


# ---------------------------------------------------------------------------
# CO-1: Tale's End's "legendary spell" restriction
# ---------------------------------------------------------------------------

class TestTalesEndLegendaryRestriction:
    def test_non_legendary_spell_target_rejected(self, make_card):
        from mtg.helpers import counter_restriction_allows
        tales = _cached_text("Tale's End")
        counterspell = make_card("Counterspell", type_line="Instant")
        assert counter_restriction_allows(tales, counterspell) is False, (
            "the response path burned {0}{U} + the card at an illegal "
            "declared target (game_1535212567969144902, CR 601.2c)")

    def test_legendary_spell_target_allowed(self, make_card):
        from mtg.helpers import counter_restriction_allows
        tales = _cached_text("Tale's End")
        sfa = make_card("Search for Azcanta", type_line="Legendary Enchantment")
        assert counter_restriction_allows(tales, sfa) is True

    def test_disallow_unrestricted(self, make_card):
        from mtg.helpers import counter_restriction_allows
        disallow = _cached_text("Disallow")
        counterspell = make_card("Counterspell", type_line="Instant")
        assert counter_restriction_allows(disallow, counterspell) is True


# ---------------------------------------------------------------------------
# CO-2: ability-word-prefixed self-ETB triggers classify (with watcher guard)
# ---------------------------------------------------------------------------

class TestAbilityWordEtbClassification:
    def test_imprint_classifies_as_self_etb(self, make_card):
        from mtg.triggers import _is_self_etb_trigger_paragraph
        scepter = _cached_card(make_card, "Isochron Scepter")
        para = next(p for p in scepter.oracle_text.split("\n")
                    if p.lower().startswith("imprint"))
        assert _is_self_etb_trigger_paragraph(scepter, para) is True, (
            "the JSON imprint template was registered but UNREACHABLE — "
            "every Scepter resolved with no imprint "
            "(game_1535212567969144902)")

    def test_constellation_stays_with_its_watcher(self, make_card):
        from mtg.triggers import _is_self_etb_trigger_paragraph
        eidolon = _cached_card(make_card, "Eidolon of Blossoms")
        para = next(p for p in eidolon.oracle_text.split("\n")
                    if p.lower().startswith("constellation"))
        assert _is_self_etb_trigger_paragraph(eidolon, para) is False, (
            "stripping 'Constellation —' would double-fire Eidolon (the "
            "constellation watcher + JSON template already handle it)")


# ---------------------------------------------------------------------------
# CO-3: devour reachable from the cast funnel
# ---------------------------------------------------------------------------

class TestDevourReachable:
    def test_mycoloth_devours_tokens_on_entry(self, make_game, make_card):
        from mtg.spells import maybe_resolve_devour
        game = make_game()
        rick = game.players[0]
        myco = _cached_card(make_card, "Mycoloth")
        rick.battlefield.append(myco)
        for i in range(3):
            tok = make_card(f"Elf Warrior", power="1", toughness="1")
            tok.is_token = True
            rick.battlefield.append(tok)
        rules = RulesEngine(None)

        class _Engine:
            pass
        eng = _Engine()
        eng.rules = rules
        msgs = maybe_resolve_devour(eng, game, rick, myco)
        assert myco.counters.get("+1/+1", 0) == 6, (
            "devour was UNREACHABLE at entry — Mycoloth sat at 0 counters "
            "over 3 token fodder (game_1535222978873266206; the wave-5 pin "
            "called resolve_etb directly, the pin-shape trap)")
        assert not any(getattr(c, 'is_token', False)
                       for c in rick.battlefield), "tokens were devoured"

    def test_funnel_calls_the_function(self):
        src = (Path(__file__).resolve().parent.parent / "mtg" /
               "spells.py").read_text(encoding="utf-8")
        dispatch = src.split("async def _dispatch_resolution", 1)[1]
        assert "maybe_resolve_devour(" in dispatch, (
            "the cast funnel must invoke the devour parse")


# ---------------------------------------------------------------------------
# CO-4: the inline board-wipe-on-empty-board veto predicate
# ---------------------------------------------------------------------------

class TestInlineWipeVeto:
    def test_wipe_on_empty_board_detected(self, make_game, make_card):
        from mtg.helpers import board_wipe_on_empty_board
        game = make_game()
        doj = make_card("Day of Judgment", type_line="Sorcery",
                        oracle_text="Destroy all creatures.")
        assert board_wipe_on_empty_board(game, 1, doj) is True

    def test_wipe_with_creatures_allowed(self, make_game, make_card):
        from mtg.helpers import board_wipe_on_empty_board
        game = make_game()
        game.players[0].battlefield.append(make_card("Bear"))
        doj = make_card("Day of Judgment", type_line="Sorcery",
                        oracle_text="Destroy all creatures.")
        assert board_wipe_on_empty_board(game, 1, doj) is False

    def test_creature_spells_never_vetoed(self, make_game, make_card):
        from mtg.helpers import board_wipe_on_empty_board
        game = make_game()
        # A creature whose text mentions "each creature" must not be held.
        crea = make_card("Massacre Wurm",
                         type_line="Creature — Phyrexian Wurm",
                         oracle_text="When this enters, each creature your "
                                     "opponents control gets -2/-2.")
        assert board_wipe_on_empty_board(game, 1, crea) is False

    def test_inline_path_consults_the_helper(self):
        src = (Path(__file__).resolve().parent.parent / "mtg" /
               "claude_player.py").read_text(encoding="utf-8")
        assert "board_wipe_on_empty_board(" in src
