"""July 23, 2026 audit (batch game_15296*) — pins for the reviewer-wave fixes.

Nine findings from the four-reviewer semantic wave over the July 23 verification
batch, each source-verified against the code + oracle (data/card_data_cache.json)
before fixing:

  #1  Aminatou -1 ignored the declared target and could flicker a
      controlled-but-not-owned permanent (rules/effect_templates.py template +
      mtg/actions.py flicker handler `require_own`).
  #2  flicker did not clear +1/+1 counters on re-entry (CR 400.7).
  #5  Erebos "Pay 2 life: Draw a card" double-charged life via Tier 3
      (mtg/engine.py inline plain-draw parser).
  #6  a "sacrifice a creature" edict matched a devotion-gated god on its
      printed type_line instead of is_creature(game).
  #8  Force of Negation's "exile instead of graveyard" clause was unimplemented.
  #10 a mandatory sacrifice edict couldn't choose a commander when it was the
      only legal creature.
  #11 Volcanic Geyser (X damage to any target) dropped its declared creature
      target at Tier 3 and hit the opponent's face.
  #12 Dark Prophecy's dies trigger dropped its "you lose 1 life" clause.
  #13 Moldervine Reclamation's dies trigger dropped its "draw a card" clause.
"""
import re

import pytest

from rules.effect_templates import get_effect_library, build_game_context


# --------------------------------------------------------------------------- #
# #1 — Aminatou -1: honor the declared target + require_own ownership guard
# --------------------------------------------------------------------------- #
class TestAminatouMinusOneTargeting:
    def test_template_honors_explicit_target_and_sets_require_own(self, lib):
        tmpl = lib._pw_ability_templates[("aminatou", "exile another target permanent")]
        # AI declared Animate Dead; the heuristic would have picked Korvold.
        ctx = {"explicit_target_name": "Animate Dead", "best_own_etb_creature": "Korvold"}
        actions = tmpl.action_generator("Claude", "Rick", ctx)
        assert actions[0]["action"] == "flicker"
        assert actions[0]["target"] == "Animate Dead", "declared target must win over the heuristic"
        assert actions[0].get("require_own") is True

    def test_template_no_action_without_any_target(self, lib):
        tmpl = lib._pw_ability_templates[("aminatou", "exile another target permanent")]
        actions = tmpl.action_generator("Claude", "Rick", {})
        assert actions[0]["action"] == "no_action"

    def test_flicker_require_own_reselects_owned_over_stolen(self, rules, make_game, make_card):
        game = make_game()
        claude = game.players[1]  # index 1
        owned = make_card("Owned Bear", oracle_text="", entered_this_turn=False)
        owned.owner_index = 1
        stolen = make_card("Reanimated Korvold", oracle_text="", entered_this_turn=False)
        stolen.owner_index = 0  # Rick still owns it (CR 208) though Claude controls it
        claude.battlefield.extend([owned, stolen])
        rules._execute_action_on_state(game, {
            "action": "flicker", "player": "Claude",
            "target": "Reanimated Korvold", "require_own": True})
        assert stolen.entered_this_turn is False, "a not-owned permanent must not be flickered"
        assert owned.entered_this_turn is True, "an owned permanent is reselected instead"

    def test_flicker_require_own_fizzles_when_none_owned(self, rules, make_game, make_card):
        game = make_game()
        claude = game.players[1]
        stolen = make_card("Reanimated Korvold", oracle_text="", entered_this_turn=False)
        stolen.owner_index = 0
        claude.battlefield.append(stolen)
        rules._execute_action_on_state(game, {
            "action": "flicker", "player": "Claude",
            "target": "Reanimated Korvold", "require_own": True})
        assert stolen.entered_this_turn is False, "no owned target → fizzle, never steal"


# --------------------------------------------------------------------------- #
# #2 — flicker clears +1/+1 counters (CR 400.7 new object)
# --------------------------------------------------------------------------- #
class TestFlickerClearsCounters:
    def test_counters_reset_on_flicker(self, rules, make_game, make_card):
        game = make_game()
        claude = game.players[1]
        bear = make_card("Grizzly Bears", oracle_text="")
        bear.owner_index = 1
        bear.counters = {"+1/+1": 3}
        claude.battlefield.append(bear)
        rules._execute_action_on_state(game, {
            "action": "flicker", "player": "Claude", "target": "Grizzly Bears"})
        assert bear.counters == {}, "a re-entered permanent is a new object with no counters"


# --------------------------------------------------------------------------- #
# #6 / #10 — sacrifice edict: devotion-aware creature filter + commander last resort
# --------------------------------------------------------------------------- #
class TestSacrificeEdict:
    def _erebos(self, make_card):
        return make_card(
            "Erebos, God of the Dead",
            type_line="Legendary Enchantment Creature — God",
            oracle_text="As long as your devotion to black is less than five, "
                        "Erebos isn't a creature.",
            mana_cost="{1}{B}{B}")

    def test_devotion_god_not_sacrificed_to_creature_edict(self, rules, make_game, make_card):
        game = make_game()
        claude = game.players[1]
        erebos = self._erebos(make_card)  # {B}{B} = devotion 2 < 5 → not a creature
        erebos.owner_index = 1
        claude.battlefield.append(erebos)
        msg = rules._execute_action_on_state(game, {
            "action": "sacrifice_permanent", "player": "Claude",
            "type_filter": "creature", "reason": "Dictate of Erebos"})
        assert "no permanent to sacrifice" in (msg or "").lower()
        assert erebos in claude.battlefield

    def test_real_creature_still_sacrificed(self, rules, make_game, make_card):
        game = make_game()
        claude = game.players[1]
        bear = make_card("Bear", type_line="Creature — Bear")
        bear.owner_index = 1
        claude.battlefield.append(bear)
        rules._execute_action_on_state(game, {
            "action": "sacrifice_permanent", "player": "Claude",
            "type_filter": "creature", "reason": "Grave Pact"})
        assert bear not in claude.battlefield

    def test_commander_sacrificed_as_last_resort(self, rules, make_game, make_card):
        game = make_game()  # commander format
        claude = game.players[1]
        gisela = make_card("Gisela, Blade of Goldnight",
                           type_line="Legendary Creature — Angel", oracle_text="")
        gisela.is_commander = True
        gisela.owner_index = 1
        claude.battlefield.append(gisela)
        rules._execute_action_on_state(game, {
            "action": "sacrifice_permanent", "player": "Claude",
            "type_filter": "creature", "reason": "Grave Pact"})
        assert gisela not in claude.battlefield, "a mandatory edict must be able to choose the commander"

    def test_noncommander_preferred_over_commander(self, rules, make_game, make_card):
        game = make_game()
        claude = game.players[1]
        gisela = make_card("Gisela, Blade of Goldnight",
                           type_line="Legendary Creature — Angel", oracle_text="")
        gisela.is_commander = True
        gisela.owner_index = 1
        bear = make_card("Bear", type_line="Creature — Bear")
        bear.owner_index = 1
        claude.battlefield.extend([gisela, bear])
        rules._execute_action_on_state(game, {
            "action": "sacrifice_permanent", "player": "Claude",
            "type_filter": "creature", "reason": "Grave Pact"})
        assert bear not in claude.battlefield
        assert gisela in claude.battlefield, "commander is preserved when a non-commander exists"


# --------------------------------------------------------------------------- #
# #8 — Force of Negation: exile the countered spell instead of graveyard
# --------------------------------------------------------------------------- #
class TestForceOfNegationExile:
    def test_template_sets_countered_to_exile(self, lib):
        actions, _ = lib.resolve_spell(
            "Force of Negation",
            "Counter target noncreature spell. If that spell is countered this "
            "way, exile it instead of putting it into its owner's graveyard.",
            "Claude", "Rick", {"stack_top_is_creature": False})
        assert actions is not None
        assert actions[0]["action"] == "counter_spell"
        assert actions[0].get("countered_to") == "exile"

    def test_template_fizzles_on_creature_spell(self, lib):
        actions, _ = lib.resolve_spell(
            "Force of Negation", "Counter target noncreature spell.",
            "Claude", "Rick", {"stack_top_is_creature": True})
        assert actions[0]["action"] == "no_action"


# --------------------------------------------------------------------------- #
# #11 — Volcanic Geyser: honor the declared creature target with X damage
# --------------------------------------------------------------------------- #
class TestVolcanicGeyser:
    def test_hits_declared_creature_with_x(self, lib):
        actions, _ = lib.resolve_spell(
            "Volcanic Geyser", "Volcanic Geyser deals X damage to any target.",
            "Claude", "Rick",
            {"explicit_target_name": "Savra, Queen of the Golgari",
             "explicit_target_is_creature": True, "x_value": 2})
        assert actions is not None
        dmg = next(a for a in actions if a["action"] == "deal_damage")
        assert dmg["amount"] == 2
        assert dmg["target_card"] == "Savra, Queen of the Golgari"
        assert "target_player" not in dmg, "must hit the declared creature, not the face"

    def test_hits_face_when_target_not_a_creature(self, lib):
        actions, _ = lib.resolve_spell(
            "Volcanic Geyser", "Volcanic Geyser deals X damage to any target.",
            "Claude", "Rick",
            {"explicit_target_name": "Rick", "explicit_target_is_creature": False,
             "x_value": 3})
        dmg = next(a for a in actions if a["action"] == "deal_damage")
        assert dmg["amount"] == 3
        assert dmg.get("target_player") == "Rick"


# --------------------------------------------------------------------------- #
# #12 / #13 — combined dies triggers keep both clauses
# --------------------------------------------------------------------------- #
class TestCombinedDiesTriggers:
    def test_dark_prophecy_draws_and_loses_life(self, lib):
        actions, _ = lib.resolve_dies_trigger(
            "Dark Prophecy",
            "Whenever a creature you control dies, you draw a card and you lose 1 life.",
            "Some Creature", 2, 2, "Claude", "Rick", None)
        assert actions is not None
        kinds = [a["action"] for a in actions]
        assert "draw_cards" in kinds, "Dark Prophecy must still draw"
        assert "lose_life" in kinds, "Dark Prophecy must not drop 'you lose 1 life'"
        assert next(a for a in actions if a["action"] == "lose_life")["amount"] == 1

    def test_moldervine_gains_life_and_draws(self, lib):
        actions, _ = lib.resolve_dies_trigger(
            "Moldervine Reclamation",
            "Whenever a creature you control dies, you gain 1 life and draw a card.",
            "Some Creature", 2, 2, "Claude", "Rick", None)
        assert actions is not None
        kinds = [a["action"] for a in actions]
        assert "gain_life" in kinds, "Moldervine must still gain life"
        assert "draw_cards" in kinds, "Moldervine must not drop 'and draw a card'"
        assert next(a for a in actions if a["action"] == "gain_life")["amount"] == 1


# --------------------------------------------------------------------------- #
# #5 — plain "Draw N cards" resolves inline (not the cost-reapplying Tier 3);
#      the anchoring keeps loots at Tier 3 so their riders aren't dropped.
# --------------------------------------------------------------------------- #
class TestPlainDrawInlineAnchor:
    PAT = r'draw (a|one|two|three|four|five|\d+) cards?\.?\s*$'

    def test_bare_draw_matches(self):
        assert re.match(self.PAT, "draw a card.".strip())
        assert re.match(self.PAT, "draw two cards".strip())

    def test_loot_and_riders_do_not_match(self):
        assert not re.match(self.PAT, "draw two cards, then discard a card.".strip())
        assert not re.match(self.PAT, "draw a card, then you lose 1 life.".strip())
        assert not re.match(self.PAT, "draw a card for each creature you control.".strip())
