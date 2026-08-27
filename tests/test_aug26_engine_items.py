"""The three recorded-then-fixed taxonomy-audit items (Aug 26, 2026).

1. Sign in Blood family ordering — all three print "draw ... AND lose ...";
   the template emitted lose-first, which at exactly-N life ended the game
   before the draws (the tail SBA wave detects the zero mid-list).
2. Claustrophobia-class continuous "doesn't untap during its controller's
   untap step" — enforced nowhere; the aura tapped once and the creature
   politely untapped next turn. Now a STEP-scoped static at the shared
   untap seam (helpers.untap_permanent), game-threaded because the Aura
   sits on ITS controller's battlefield (usually the opponent's).
3. Tormenting Voice's printed additional-discard cost — never charged (a
   pure "draw 2 for {1}{R}"). Now the sacrifice-cost machinery's sibling:
   shared predicate (advertisement + the CR 601.2g cast gate) + payment in
   _pay_costs after mana, routed through the madness choke point.

Oracle constants are bulk-verified verbatim.
"""

import pytest

from mtg.engine import GameEngine
from mtg.helpers import untap_permanent
from mtg.legal_actions import (
    additional_discard_requirement, can_pay_additional_discard,
)
from mtg.spells import _pay_costs, _validate_cast
from rules.effect_templates import build_game_context, get_effect_library


CLAUSTROPHOBIA = ("Enchant creature\nWhen this Aura enters, tap enchanted "
                  "creature.\nEnchanted creature doesn't untap during its "
                  "controller's untap step.")
TORMENTING_VOICE = ("As an additional cost to cast this spell, discard a "
                    "card.\nDraw two cards.")
CATHARTIC_REUNION = ("As an additional cost to cast this spell, discard two "
                     "cards.\nDraw three cards.")
SONIC_BURST = ("As an additional cost to cast this spell, discard a card at "
               "random.\nSonic Burst deals 4 damage to any target.")
LIGHTNING_AXE = ("As an additional cost to cast this spell, discard a card "
                 "or pay {5}.\nLightning Axe deals 5 damage to target "
                 "creature.")
MAGMATIC_INSIGHT = ("As an additional cost to cast this spell, discard a "
                    "land card.\nDraw two cards.")
FIRESTORM = ("As an additional cost to cast this spell, discard X cards.\n"
             "Firestorm deals X damage to each of X targets.")


class TestSignInBloodOrdering:
    @pytest.mark.parametrize("name,amt", [
        ("Sign in Blood", 2), ("Night's Whisper", 2), ("Ambition's Cost", 3),
    ])
    def test_draw_precedes_life_loss(self, game, make_card, name, amt):
        lib = get_effect_library()
        rick, claude = game.players
        ctx = build_game_context(game, rick, claude)
        actions, _ = lib.resolve_spell(name, "", "Rick", "Claude",
                                       game_context=ctx)
        assert actions and len(actions) == 2
        assert actions[0]["action"] == "draw_cards" and actions[0]["amount"] == amt
        assert actions[1]["action"] == "lose_life" and actions[1]["amount"] == amt, (
            "printed order is draw THEN lose — lose-first at exactly-N life "
            "ends the game before the draws")


class TestContinuousNoUntap:
    def _enchanted(self, game, make_card, aura_text=CLAUSTROPHOBIA,
                   cross_controller=True):
        rick, claude = game.players
        bear = make_card("Grizzly Bears", type_line="Creature — Bear",
                         power="2", toughness="2")
        bear.tapped = True
        bear.summoning_sick = False
        rick.battlefield.append(bear)
        aura = make_card("Claustrophobia", type_line="Enchantment — Aura",
                         oracle_text=aura_text, power=None, toughness=None)
        aura.attached_to = bear.id
        bear.attachments = [aura.id]
        # The decisive case: the Aura sits on ITS controller's battlefield —
        # the OPPONENT's — so resolving the attachment needs game scope.
        (claude if cross_controller else rick).battlefield.append(aura)
        return bear, aura

    def test_stays_tapped_through_the_untap_step(self, game, rules, make_card):
        bear, _ = self._enchanted(game, make_card)
        game.active_player_index = 0
        rules.on_untap_step(game)
        assert bear.tapped, (
            "Claustrophobia's whole card: the creature must not untap "
            "during its controller's untap step")

    def test_untap_all_does_not_become_the_bypass(self, game, make_card):
        ge = GameEngine(None)
        bear, _ = self._enchanted(game, make_card)
        game.active_player_index = 0
        ge.rules.on_untap_step(game)
        ge.untap_all(game.players[0], game=game)
        assert bear.tapped, (
            "untap_all runs right after on_untap_step — without game it "
            "would untap what the static just kept tapped")

    def test_effect_untap_still_works(self, game, rules, make_card):
        bear, _ = self._enchanted(game, make_card)
        rules._execute_action_on_state(game, {
            "action": "untap", "card": "Grizzly Bears"})
        assert not bear.tapped, (
            "the static is STEP-scoped — Twiddle/Seedborn-class effect "
            "untaps are not the untap step (CR 502 vs an effect)")

    def test_sentence_scope_guards_unrelated_riders(self, game, rules, make_card):
        # A rider sentence CONTAINING "doesn't untap during" that does NOT
        # begin with enchanted/equipped must not freeze the bearer. (The
        # first fixture here used "don't untap" — plural — which never
        # reaches the phrase match at all, so the scope guard was untested;
        # the mutation sweep caught it. This sentence starts with "That".)
        bear, _ = self._enchanted(
            game, make_card,
            aura_text=("Enchant creature\nEnchanted creature gets +2/+2.\n"
                       "That creature doesn't untap during its controller's "
                       "next untap step."))
        game.active_player_index = 0
        rules.on_untap_step(game)
        assert not bear.tapped

    def test_untaps_normally_once_the_aura_leaves(self, game, rules, make_card):
        bear, aura = self._enchanted(game, make_card)
        game.players[1].battlefield.remove(aura)
        bear.attachments = []
        game.active_player_index = 0
        rules.on_untap_step(game)
        assert not bear.tapped

    def test_helper_direct_contract(self, game, make_card):
        bear, _ = self._enchanted(game, make_card)
        assert untap_permanent(bear, game=game, during_untap_step=True) is False
        assert bear.tapped
        assert untap_permanent(bear, game=game) is True, (
            "without during_untap_step the static must not apply")


class TestAdditionalDiscardCost:
    def test_requirement_parses_the_plain_family_only(self, make_card):
        def req(text):
            return additional_discard_requirement(
                make_card("Probe", type_line="Sorcery", oracle_text=text,
                          power=None, toughness=None))
        assert req(TORMENTING_VOICE) == 1
        assert req(CATHARTIC_REUNION) == 2
        # The declined tail — each keeps the historical behavior rather
        # than mischarging: random choice, modal cost, typed, X.
        assert req(SONIC_BURST) is None
        assert req(LIGHTNING_AXE) is None
        assert req(MAGMATIC_INSIGHT) is None
        assert req(FIRESTORM) is None

    def test_can_pay_excludes_the_spell_itself(self, game, make_card):
        rick = game.players[0]
        voice = make_card("Tormenting Voice", type_line="Sorcery",
                          oracle_text=TORMENTING_VOICE, mana_cost="{1}{R}",
                          power=None, toughness=None)
        rick.hand = [voice]
        assert not can_pay_additional_discard(voice, rick), (
            "CR 601.2g: the spell cannot pay its own discard cost")
        rick.hand.append(make_card("Mountain", type_line="Basic Land — Mountain",
                                   power=None, toughness=None))
        assert can_pay_additional_discard(voice, rick)

    def test_cast_gate_refuses_without_fodder(self, game, make_card):
        engine = GameEngine(None)
        rick = game.players[0]
        voice = make_card("Tormenting Voice", type_line="Sorcery",
                          oracle_text=TORMENTING_VOICE, mana_cost="{1}{R}",
                          power=None, toughness=None)
        rick.hand = [voice]
        for _ in range(2):
            mtn = make_card("Mountain", type_line="Basic Land — Mountain",
                            oracle_text="{T}: Add {R}.", power=None,
                            toughness=None)
            mtn.summoning_sick = False
            rick.battlefield.append(mtn)
        game.active_player_index = 0
        rejection, _, _ = _validate_cast(engine, game, rick, voice, None)
        assert rejection is not None and "discard" in rejection[1]
        # With fodder the discard gate stands down (later gates may still
        # apply — assert specifically that THIS refusal is gone).
        rick.hand.append(make_card("Filler", type_line="Sorcery",
                                   power=None, toughness=None))
        rejection2, _, _ = _validate_cast(engine, game, rick, voice, None)
        assert rejection2 is None or "discard" not in rejection2[1]

    def test_payment_discards_after_mana(self, game, make_card):
        engine = GameEngine(None)
        rick = game.players[0]
        voice = make_card("Tormenting Voice", type_line="Sorcery",
                          oracle_text=TORMENTING_VOICE, mana_cost="{1}{R}",
                          power=None, toughness=None)
        fodder = make_card("Expensive Filler", type_line="Sorcery", cmc=6,
                           power=None, toughness=None)
        rick.hand = [voice, fodder]
        costs = {
            "effective_mana_cost": "", "effective_cmc": 0,
            "total_cost": 0, "x_value_chosen": 0,
            "total_alt_reduction": 0, "cost_increase": 0,
            "pay_mana": False,
        }
        assert _pay_costs(engine, game, rick, voice, costs, 0) is None
        assert fodder not in rick.hand, "the printed cost is finally charged"
        assert fodder in rick.graveyard
        assert voice in rick.hand, "the spell never pays its own cost"
        assert any("discard" in m.lower()
                   for m in costs.get("additional_cost_messages", []))

    def test_offer_filter_matches_the_gate(self, game, make_card):
        # The shared-predicate property (the Aug-5 sacrifice lesson):
        # advertisement must never offer what the gate refuses.
        from mtg.legal_actions import castable_entries
        rick = game.players[0]
        voice = make_card("Tormenting Voice", type_line="Sorcery",
                          oracle_text=TORMENTING_VOICE, mana_cost="{1}{R}",
                          power=None, toughness=None)
        rick.hand = [voice]
        for _ in range(2):
            mtn = make_card("Mountain", type_line="Basic Land — Mountain",
                            oracle_text="{T}: Add {R}.", power=None,
                            toughness=None)
            mtn.summoning_sick = False
            rick.battlefield.append(mtn)
        pool = {'W': 0, 'U': 0, 'B': 0, 'R': 2, 'G': 0, 'C': 0}
        entries = castable_entries(game, rick, pool, 0, 2)
        assert not any("Tormenting Voice" in e["label"] for e in entries), (
            "no other card in hand — the CR 601.2g gate would refuse this")
        rick.hand.append(make_card("Filler", type_line="Sorcery",
                                   power=None, toughness=None))
        entries = castable_entries(game, rick, pool, 0, 2)
        assert any("Tormenting Voice" in e["label"] for e in entries)
