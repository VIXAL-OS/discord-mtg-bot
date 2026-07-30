"""Pins for the July 29, 2026 reviewer-wave fixes (batch 15315, wave 2).

Four Sonnet reviewers (uw_control/jund, tokens/baral, snow/graveyard,
sagas/layers — sampled by recency-of-attention from the never-recently-audited
complement) produced ~19 findings; the mechanisms below were each verified
against the code before fixing. The deferred remainder is recorded in
CLAUDE.md's July 29 register section.
"""
import asyncio
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _engine():
    from mtg.engine import GameEngine
    return GameEngine(None)


# ---------------------------------------------------------------------------
# Solitary Confinement — CR 611.2c: statics live and die with the permanent
# ---------------------------------------------------------------------------

_SOLCONF_ORACLE = ("At the beginning of your upkeep, sacrifice Solitary "
                   "Confinement unless you discard a card.\nSkip your draw "
                   "step.\nYou have shroud.\nPrevent all damage that would "
                   "be dealt to you.")


class TestSolitaryConfinementStatics:

    def test_skip_draw_follows_the_battlefield(self, game, make_card):
        from mtg.helpers import player_skips_draw_step
        rick = game.players[0]
        solconf = make_card("Solitary Confinement",
                            type_line="Enchantment", power="0", toughness="0",
                            oracle_text=_SOLCONF_ORACLE)
        assert player_skips_draw_step(rick) is None
        rick.battlefield.append(solconf)
        assert player_skips_draw_step(rick) == "Solitary Confinement"
        rick.battlefield.remove(solconf)   # exile / destroy / bounce — any exit
        assert player_skips_draw_step(rick) is None, \
            "CR 611.2c: the skip must end the moment the source leaves"

    def test_necropotence_skip_is_honored_too(self, game, make_card):
        from mtg.helpers import player_skips_draw_step
        rick = game.players[0]
        rick.battlefield.append(make_card(
            "Necropotence", type_line="Enchantment", power="0", toughness="0",
            oracle_text="Skip your draw step.\nWhenever you discard a card, "
                        "exile that card from your graveyard."))
        assert player_skips_draw_step(rick) == "Necropotence"

    def test_damage_prevention_follows_the_battlefield(self, game, make_card):
        from mtg.combat import apply_combat_damage_to_player
        engine = _engine()
        rick = game.players[0]
        solconf = make_card("Solitary Confinement",
                            type_line="Enchantment", power="0", toughness="0",
                            oracle_text=_SOLCONF_ORACLE)
        attacker = make_card("Bear")
        rick.battlefield.append(solconf)
        dealt = apply_combat_damage_to_player(engine.rules, game, rick, 3, attacker)
        assert dealt == 0, "damage to a Solitary Confinement controller is prevented"
        rick.battlefield.remove(solconf)
        dealt = apply_combat_damage_to_player(engine.rules, game, rick, 3, attacker)
        assert dealt == 3, \
            "batch 15315 prevented 6 damage the same turn the source was exiled"

    def test_the_sticky_flags_are_gone_from_the_upkeep_branch(self):
        src = (ROOT / "mtg/engine.py").read_text(encoding="utf-8")
        assert "_skip_draw = True" not in src, \
            "the sticky flag with no expiry is the bug this pin exists for"


# ---------------------------------------------------------------------------
# Fatal Push — the MV gate has a producer now
# ---------------------------------------------------------------------------

class TestFatalPushMvGate:

    def _lib(self):
        from rules.effect_templates import get_effect_library
        return get_effect_library()

    def test_build_game_context_produces_the_mv(self, game, make_card):
        """Both producer branches (string name AND Card object) — the first
        mutation sweep proved a string-only pin lets the object branch's
        producer be deleted unnoticed (the test passed for the wrong
        reason; July 26 lesson applied)."""
        from rules.effect_templates import build_game_context
        rick, claude = game.players
        solitude = make_card("Solitude", type_line="Creature — Elemental",
                             mana_cost="{3}{W}{W}", cmc=5,
                             power="3", toughness="2")
        rick.battlefield.append(solitude)
        ctx = build_game_context(game, claude, rick, explicit_target="Solitude")
        assert ctx.get('explicit_target_mv') == 5, \
            "string-target branch: the gate's only input had NO producer"
        ctx = build_game_context(game, claude, rick, explicit_target=solitude)
        assert ctx.get('explicit_target_mv') == 5, \
            "Card-object branch: same producer, separately written"

    def test_declared_high_mv_target_is_refused(self):
        actions = self._lib()._gen_fatal_push(
            "Claude", "Rick",
            {'explicit_target_name': 'Solitude', 'explicit_target_mv': 5})
        assert actions[0]['action'] == 'no_action', \
            "a cascade Fatal Push destroyed a mana-value-5 Solitude"

    def test_declared_legal_target_is_destroyed(self):
        actions = self._lib()._gen_fatal_push(
            "Claude", "Rick",
            {'explicit_target_name': 'Ragavan', 'explicit_target_mv': 1})
        assert actions == [{"action": "destroy", "card": "Ragavan"}]

    def test_auto_pick_only_considers_legal_targets(self):
        actions = self._lib()._gen_fatal_push(
            "Claude", "Rick",
            {'_opponent_creatures': [
                {'name': 'Titan', 'power': 6, 'cmc': 6},
                {'name': 'Bear', 'power': 2, 'cmc': 2},
            ]})
        assert actions == [{"action": "destroy", "card": "Bear"}], \
            "the fallback must pick the best LEGAL creature, not gate an illegal one"


# ---------------------------------------------------------------------------
# Meren — experience counters at the death choke-point
# ---------------------------------------------------------------------------

_MEREN_ORACLE = ("Whenever another creature you control dies, you get an "
                 "experience counter.\nAt the beginning of your end step, "
                 "choose target creature card in your graveyard. If that "
                 "card's mana value is less than or equal to the number of "
                 "experience counters you have, return it to the "
                 "battlefield. Otherwise, put it into your hand.")


class TestMerenExperienceCounters:

    def test_sacrifice_as_cost_deaths_grant_xp(self, game, make_card):
        """game_1531564156203827213: three sac-outlet deaths under a live
        Meren granted ZERO experience — the increment lived only in the SBA
        sweep's local death list, which sacrifice costs bypass entirely.
        queue_death is the one choke-point every death path reaches."""
        from mtg.triggers import queue_death
        claude = game.players[1]
        meren = make_card("Meren of Clan Nel Toth",
                          type_line="Legendary Creature — Human Shaman",
                          oracle_text=_MEREN_ORACLE)
        claude.battlefield.append(meren)
        victim = make_card("Wood Elves")
        queue_death(game, victim, claude)
        assert getattr(claude, '_experience_counters', 0) == 1

    def test_merens_own_death_grants_nothing(self, game, make_card):
        from mtg.triggers import queue_death
        claude = game.players[1]
        meren = make_card("Meren of Clan Nel Toth",
                          type_line="Legendary Creature — Human Shaman",
                          oracle_text=_MEREN_ORACLE)
        claude.battlefield.append(meren)
        queue_death(game, meren, claude)   # "another creature" — not herself
        assert getattr(claude, '_experience_counters', 0) == 0

    def test_the_sba_sweep_no_longer_double_increments(self, game, make_card):
        """The SBA path also routes through queue_deaths, so the increment
        must exist ONLY at the choke-point — the old sba.py block is gone."""
        src = (ROOT / "mtg/sba.py").read_text(encoding="utf-8")
        assert "_experience_counters = prev + 1" not in src


# ---------------------------------------------------------------------------
# Planeswalker targeting — qualified permanents + optional "up to" targets
# ---------------------------------------------------------------------------

class TestPlaneswalkerTargeting:

    def _system(self):
        from rules.planeswalker import PlaneswalkerManager
        return PlaneswalkerManager()

    def test_teferi_minus3_parses_its_target(self):
        """No pattern matched "target [qualifier] permanent" — Teferi's -3
        parsed needs_target=False, the declared target was dropped before
        activation, and Tier 3 hallucinated its own (loyalty burned 7→4 for
        zero effect in game_1531560953928355911)."""
        needs, desc = self._system()._parse_targeting(
            "Put target nonland permanent into its owner's library third "
            "from the top.")
        assert needs is True
        assert desc == "nonland permanent"

    def test_nonland_legality_excludes_lands(self, game, make_card):
        from rules.planeswalker import (AbilityType, PlaneswalkerAbility,
                                        get_legal_planeswalker_targets)
        rick = game.players[0]
        rick.battlefield.append(make_card("Forest", type_line="Basic Land — Forest",
                                          power="0", toughness="0"))
        bear = make_card("Bear")
        rick.battlefield.append(bear)
        ability = PlaneswalkerAbility(
            index=1, loyalty_cost=-3,
            ability_type=AbilityType.LOYALTY_MINUS,
            text="Put target nonland permanent into its owner's library "
                 "third from the top.",
            needs_target=True, target_description="nonland permanent")
        targets = get_legal_planeswalker_targets(game, rick, ability)
        names = [getattr(t, 'name', None) for t, _d in targets]
        assert "Bear" in names
        assert "Forest" not in names

    def test_up_to_gate_exists_in_activate(self):
        """Wrenn and Six's [+1] ("up to one target") was refused outright
        with an empty graveyard — CR 601.2c gates MANDATORY targets only,
        and the refusal didn't even consume the once-per-turn activation.
        The gate now falls through and activates with none chosen."""
        src = (ROOT / "rules/planeswalker.py").read_text(encoding="utf-8")
        assert "'up to' in (ability.text or '').lower()" in src
        assert "activating" in src.split(
            "'up to' in (ability.text or '').lower()", 1)[1][:1500]


# ---------------------------------------------------------------------------
# Counterspell restrictions — Mental Misstep's mana-value clause
# ---------------------------------------------------------------------------

class TestCounterRestrictionAllows:

    def test_exact_mana_value(self, make_card):
        from mtg.helpers import counter_restriction_allows
        misstep = "Counter target spell with mana value 1."
        assert counter_restriction_allows(
            misstep, make_card("Ragavan", cmc=1)) is True
        assert counter_restriction_allows(
            misstep, make_card("Intangible Virtue", cmc=2,
                               type_line="Enchantment")) is False, \
            "Mental Misstep countered a mana-value-2 spell in batch 15315"

    def test_or_less_variant(self, make_card):
        from mtg.helpers import counter_restriction_allows
        oracle = "Counter target spell with mana value 4 or less."
        assert counter_restriction_allows(oracle, make_card("Bear", cmc=2)) is True
        assert counter_restriction_allows(oracle, make_card("Titan", cmc=6)) is False

    def test_type_qualified_counters(self, make_card):
        from mtg.helpers import counter_restriction_allows
        negate = "Counter target noncreature spell."
        assert counter_restriction_allows(
            negate, make_card("Bear", type_line="Creature — Bear")) is False
        assert counter_restriction_allows(
            negate, make_card("Wrath", type_line="Sorcery")) is True
        essence = "Counter target creature spell."
        assert counter_restriction_allows(
            essence, make_card("Bear", type_line="Creature — Bear")) is True
        assert counter_restriction_allows(
            essence, make_card("Wrath", type_line="Sorcery")) is False

    def test_unrestricted_counters_are_untouched(self, make_card):
        from mtg.helpers import counter_restriction_allows
        assert counter_restriction_allows(
            "Counter target spell.", make_card("Titan", cmc=9)) is True


# ---------------------------------------------------------------------------
# Mentor of the Meek — the power gate embedded in the trigger condition
# ---------------------------------------------------------------------------

_MENTOR_ORACLE = ("Whenever another creature you control with power 2 or "
                  "less enters, you may pay {1}. If you do, draw a card.")


class TestMentorOfTheMeek:

    def _resolve(self, entering_power):
        from rules.effect_templates import get_effect_library
        lib = get_effect_library()
        actions, _desc = lib.resolve_trigger(
            trigger_card_name="Mentor of the Meek",
            trigger_oracle=_MENTOR_ORACLE,
            entering_creature_name="X", entering_creature_power=entering_power,
            entering_creature_toughness=1,
            controller="Claude", opponent="Rick", game_context={})
        return actions

    def test_five_power_heliod_does_not_draw(self):
        """Batch 15315: the `.*?` in the generic pattern swallowed "with
        power 2 or less" — a 5/5 Heliod drew a card."""
        actions = self._resolve(5)
        assert not any(a.get('action') == 'draw_cards'
                       for a in (actions or []))

    def test_small_creature_draws(self):
        actions = self._resolve(2)
        assert any(a.get('action') == 'draw_cards' for a in (actions or []))


# ---------------------------------------------------------------------------
# Ragavan — the combat-damage trigger finally does something
# ---------------------------------------------------------------------------

class TestRagavanCombatDamage:

    def test_template_fires_by_name(self):
        from rules.effect_templates import get_effect_library
        lib = get_effect_library()
        actions, _desc = lib.resolve_attack_trigger(
            trigger_card_name="Ragavan, Nimble Pilferer",
            trigger_oracle="Whenever Ragavan deals combat damage to a player, "
                           "create a Treasure token and exile the top card of "
                           "that player's library.",
            attacking_creature_name="Ragavan, Nimble Pilferer",
            attacking_creature_power=2,
            controller="Claude", opponent="Rick", game_context={})
        kinds = [a['action'] for a in actions]
        assert kinds == ['create_token', 'exile_top_of_library'], \
            "the trigger queued to Tier 3, whose combat-shape guard always refused"

    def test_exile_top_of_library_action(self, game, make_card):
        engine = _engine()
        rick = game.players[0]
        top = make_card("Snapcaster Mage")
        rick.library.insert(0, top)
        msg = engine.rules._execute_action_on_state(
            game, {"action": "exile_top_of_library", "player": "Rick", "count": 1})
        assert top in rick.exile
        assert top not in rick.library
        assert "Snapcaster Mage" in msg


# ---------------------------------------------------------------------------
# Split cards — affordable when either half is
# ---------------------------------------------------------------------------

class TestSplitCardAffordability:

    def test_either_half_payable(self, game, make_card):
        rick = game.players[0]
        for i in range(4):
            rick.battlefield.append(make_card(
                f"Island {i}", type_line="Basic Land — Island",
                power="0", toughness="0"))
        ok, reason = rick.can_pay_mana_cost("{3}{U} // {4}{U}{U}")
        assert ok is True, (
            "Commit // Memory was priced at the COMBINED 10-CMC cost and sat "
            f"dead in hand at the lethal moment ({reason})")

    def test_neither_half_payable(self, game, make_card):
        rick = game.players[0]
        rick.battlefield.append(make_card(
            "Island 0", type_line="Basic Land — Island",
            power="0", toughness="0"))
        ok, _reason = rick.can_pay_mana_cost("{3}{U} // {4}{U}{U}")
        assert ok is False


# ---------------------------------------------------------------------------
# Baral — "counters a spell" triggers exist now
# ---------------------------------------------------------------------------

class TestCountersASpellTriggers:

    def test_baral_loots_when_his_controller_counters(self, game, make_card):
        from mtg.triggers import fire_counters_a_spell_triggers
        claude = game.players[1]
        claude.battlefield.append(make_card(
            "Baral, Chief of Compliance",
            type_line="Legendary Creature — Human Wizard",
            oracle_text="Instant and sorcery spells you cast cost {1} less "
                        "to cast.\nWhenever a spell or ability you control "
                        "counters a spell, you may draw a card. If you do, "
                        "discard a card."))
        claude.library.append(make_card("Island", type_line="Basic Land — Island",
                                        power="0", toughness="0"))
        claude.hand.append(make_card("Expensive Thing", cmc=7))
        msgs = fire_counters_a_spell_triggers(game, "Claude")
        assert msgs and "draws a card" in msgs[0]
        assert len(claude.graveyard) == 1, "the loot's discard half must happen"

    def test_no_trigger_source_means_no_loot(self, game, make_card):
        from mtg.triggers import fire_counters_a_spell_triggers
        claude = game.players[1]
        claude.library.append(make_card("Island", type_line="Basic Land — Island",
                                        power="0", toughness="0"))
        assert fire_counters_a_spell_triggers(game, "Claude") == []


# ---------------------------------------------------------------------------
# Pattern of Rebirth — entry funnel + Wrenn +1 declared target (source pins)
# ---------------------------------------------------------------------------

class TestSmallReviewerFixes:

    def test_pattern_of_rebirth_uses_the_entry_funnel(self):
        src = (ROOT / "mtg/sba.py").read_text(encoding="utf-8")
        block = src.split('== "pattern of rebirth"', 1)[1][:2500]
        assert "_fire_noncast_battlefield_entry" in block, \
            "a reanimated Craterhoof pumped nothing — no ETB, no watchers, no emit"

    def test_wrenn_plus1_honors_a_declared_graveyard_land(self, game, make_card):
        from rules.effect_templates import get_effect_library
        lib = get_effect_library()
        actions = lib._gen_wrenn_plus1("Claude", "Rick", {
            'controller_graveyard': [
                make_card("Bloodstained Mire", type_line="Land",
                          power="0", toughness="0"),
                make_card("Wooded Foothills", type_line="Land",
                          power="0", toughness="0"),
            ],
            'explicit_target_name': 'Wooded Foothills',
        })
        assert actions[0]['card'] == 'Wooded Foothills'
