"""Pure rules/ subsystem seeds: mana cost parsing, the 7-layer engine, and
the SBA loop. These modules are the most deterministic code in the repo —
string/dataclass in, value out — so they get table tests."""
import pytest


class TestManaCostParsing:
    @pytest.mark.parametrize("cost,cmc", [
        ("{2}{W}{W}", 4),
        ("{X}{R}{R}", 2),      # X counts 0 toward mana value (CR 203.3b)
        ("{W/U}{W/U}", 2),     # hybrid counts 1
        ("{B/P}", 1),          # phyrexian counts 1
        ("{2/W}", 2),          # monocolored hybrid counts its generic half
        ("{S}", 1),            # snow counts 1
        ("{10}", 10),
        ("", 0),
    ])
    def test_cmc(self, cost, cmc):
        from rules.mana import ManaCost
        assert ManaCost.parse(cost).cmc == cmc


class TestLayersEngine:
    def _bear(self, controller="Rick"):
        from rules.layers import LayeredPermanent
        # NOTE: the layers engine compares types lowercase (the engine-side
        # adapter lowercases type_line before building LayeredPermanents).
        return LayeredPermanent(id="bear_1", name="Bear", controller=controller,
                                owner=controller, base_types=["creature"],
                                base_power=2, base_toughness=2)

    def test_anthem_boosts_own_creatures_only(self):
        from rules.layers import LayersEngine, create_anthem_effect
        eng = LayersEngine()
        eng.add_effect(create_anthem_effect("Glorious Anthem", "ga1", "Rick", 1, 1))
        mine = eng.calculate_characteristics(self._bear("Rick"))
        theirs = eng.calculate_characteristics(self._bear("Claude"))
        assert (mine.power, mine.toughness) == (3, 3)
        assert (theirs.power, theirs.toughness) == (2, 2)

    def test_humility_sets_one_one_and_strips_abilities(self):
        from rules.layers import LayersEngine, LayeredPermanent, create_humility_effect
        eng = LayersEngine()
        for eff in create_humility_effect("Humility", "hum1", "Rick"):
            eng.add_effect(eff)
        drake = LayeredPermanent(id="drake_1", name="Drake", controller="Claude",
                                 owner="Claude", base_types=["creature"],
                                 base_power=4, base_toughness=4,
                                 base_abilities=["Flying"])
        final = eng.calculate_characteristics(drake)
        assert (final.power, final.toughness) == (1, 1)
        assert "Flying" not in final.abilities

    def test_pt_set_applies_before_pt_mod_regardless_of_timestamp(self):
        # CR 613.4: sublayer 7b (set 1/1) applies before 7c (+1/+1) even
        # though the anthem has a LATER timestamp → 2/2, not 1/1 or 3/3.
        from rules.layers import LayersEngine, create_anthem_effect, create_humility_effect
        eng = LayersEngine()
        for eff in create_humility_effect("Humility", "hum1", "Rick"):
            eng.add_effect(eff)
        eng.add_effect(create_anthem_effect("Glorious Anthem", "ga1", "Rick", 1, 1))
        final = eng.calculate_characteristics(self._bear("Rick"))
        assert (final.power, final.toughness) == (2, 2)


class TestStateBasedActions:
    """CR 704 loop via mtg/sba.py — uses the same save-chain order the
    board-wipe action mirrors (shield → totem armor → death)."""

    def _run(self, rules, game):
        from mtg.sba import process_state_based_actions
        return process_state_based_actions(rules, game)

    def test_zero_toughness_dies_no_saves_apply(self, rules, game, make_card):
        # CR 704.5f: 0 toughness is not destruction — indestructible and
        # shield counters don't save it.
        wisp = make_card("Fragile Wisp", power="1", toughness="0",
                         counters={"shield": 1}, keywords=["Indestructible"])
        rick = game.players[0]
        rick.battlefield.append(wisp)
        self._run(rules, game)
        assert wisp in rick.graveyard

    def test_lethal_damage_dies_and_queues_dies_trigger(self, rules, game, make_card):
        bear = make_card("Runeclaw Bear")
        bear.damage_marked = 2
        rick = game.players[0]
        rick.battlefield.append(bear)
        self._run(rules, game)
        assert bear in rick.graveyard
        assert (bear, rick) in getattr(game, "_recently_died", [])

    def test_shield_counter_saves_from_lethal_damage(self, rules, game, make_card):
        warden = make_card("Sanctuary Warden", counters={"shield": 1})
        warden.damage_marked = 2
        rick = game.players[0]
        rick.battlefield.append(warden)
        self._run(rules, game)
        assert warden in rick.battlefield
        assert warden.counters["shield"] == 0
        assert warden.damage_marked == 0

    def test_totem_armor_saves_from_lethal_damage(self, rules, game, make_card):
        # May 30 (D1): cards print "Umbra armor"; the engine matched only
        # "totem armor" — the whole wired SBA save path was dead until the
        # string match was widened.
        rick = game.players[0]
        bear = make_card("Runeclaw Bear")
        bear.damage_marked = 2
        umbra = make_card(
            "Bear Umbra", type_line="Enchantment — Aura",
            power=None, toughness=None,
            oracle_text="Enchant creature. Umbra armor (If enchanted creature "
                        "would be destroyed, instead remove all damage from it "
                        "and destroy this Aura.)")
        umbra.attached_to = bear.id
        bear.attachments.append(umbra.id)
        rick.battlefield.extend([bear, umbra])
        self._run(rules, game)
        assert bear in rick.battlefield
        assert bear.damage_marked == 0
        assert umbra in rick.graveyard

    def test_planeswalker_zero_loyalty_dies(self, rules, game, make_card):
        pw = make_card("Jace Beleren", type_line="Legendary Planeswalker — Jace",
                       power=None, toughness=None)
        pw.loyalty_counters = 0
        rick = game.players[0]
        rick.battlefield.append(pw)
        self._run(rules, game)
        assert pw in rick.graveyard

    def test_plus_and_minus_counters_annihilate(self, rules, game, make_card):
        # CR 704.5q: +1/+1 and -1/-1 counters cancel in pairs.
        bear = make_card("Runeclaw Bear", counters={"+1/+1": 2, "-1/-1": 1})
        rick = game.players[0]
        rick.battlefield.append(bear)
        self._run(rules, game)
        assert bear.counters.get("+1/+1", 0) == 1
        assert bear.counters.get("-1/-1", 0) == 0
