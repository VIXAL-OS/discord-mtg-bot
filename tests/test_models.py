"""Card / Player / GameState model behavior — May 26/30 audit backfill.

These pin the compute-on-read paths the layers engine can't express
(dynamic magnitudes), conditional keywords, conditional static anthems,
and mana-source legality. Every test names the audit fix it guards.
"""
import pytest


class TestDeathsShadow:
    ORACLE = "Death's Shadow gets -X/-X, where X is your life total."

    def _shadow(self, make_card):
        return make_card("Death's Shadow", type_line="Creature — Avatar",
                         power="13", toughness="13", oracle_text=self.ORACLE)

    def test_debuff_tracks_controller_life(self, game, make_card):
        # May 26: shipped as a vanilla 13/13 regardless of life — dealt 13
        # at 6 life, and would have illegally survived SBA at 13+ life.
        rick = game.players[0]
        shadow = self._shadow(make_card)
        rick.battlefield.append(shadow)
        rick.life = 5
        assert shadow.get_effective_power(game) == 8
        assert shadow.get_effective_toughness(game) == 8

    def test_dies_to_sba_at_thirteen_life(self, rules, game, make_card):
        rick = game.players[0]
        shadow = self._shadow(make_card)
        rick.battlefield.append(shadow)
        rick.life = 13
        assert shadow.get_effective_toughness(game) <= 0
        from mtg.sba import process_state_based_actions
        process_state_based_actions(rules, game)
        assert shadow in rick.graveyard


class TestSerraAscendant:
    ORACLE = ("As long as you have 30 or more life, Serra Ascendant gets "
              "+5/+5 and has lifelink.")

    def test_conditional_lifelink_gates_on_life(self, game, make_card):
        # May 26: the +5/+5 half was layer-gated but the keyword half was
        # baked into card.keywords at load — Serra lifelinked at 25 life.
        rick = game.players[0]
        serra = make_card("Serra Ascendant", power="1", toughness="1",
                          keywords=["Lifelink"], oracle_text=self.ORACLE)
        rick.battlefield.append(serra)
        rick.life = 25
        assert not serra.has_keyword("Lifelink", game=game)
        rick.life = 30
        assert serra.has_keyword("Lifelink", game=game)


class TestSummoningSickManaDorks:
    def test_sick_dork_excluded_from_untapped_sources(self, game, make_card):
        # May 30 ([MANA-DIVERGENCE] green-dork loop): a summoning-sick
        # Llanowar Elves counted as available mana, so the AI planned green
        # spells it couldn't pay, got rejected, re-proposed, and eventually
        # permanent-banned them (CR 302.6).
        rick = game.players[0]
        elves = make_card("Llanowar Elves", type_line="Creature — Elf Druid",
                          oracle_text="{T}: Add {G}.", summoning_sick=True)
        forest = make_card("Forest", type_line="Basic Land — Forest",
                           oracle_text="{T}: Add {G}.",
                           power=None, toughness=None, summoning_sick=True)
        rick.battlefield.extend([elves, forest])
        names = [c.name for c in rick.untapped_mana_sources()]
        assert "Forest" in names            # lands don't have summoning sickness
        assert "Llanowar Elves" not in names
        elves.summoning_sick = False
        assert "Llanowar Elves" in [c.name for c in rick.untapped_mana_sources()]

    def test_hasty_dork_counts_while_sick(self, game, make_card):
        rick = game.players[0]
        dork = make_card("Hasty Druid", type_line="Creature — Human Druid",
                         oracle_text="{T}: Add {G}.", keywords=["Haste"],
                         summoning_sick=True)
        rick.battlefield.append(dork)
        assert dork in rick.untapped_mana_sources()


class TestConditionalStaticAnthem:
    ORACLE = ("Whenever a creature you control attacks, you may put a quest "
              "counter on Beastmaster Ascension. As long as Beastmaster "
              "Ascension has seven or more quest counters on it, creatures "
              "you control get +5/+5.")

    def test_beastmaster_ascension_gates_on_quest_counters(self, game, make_card):
        # May 26: conditional statics registered unconditionally —
        # Beastmaster Ascension gave +5/+5 at ONE quest counter (needs 7).
        # The recalc-time refresh must switch the anthem on AND off as the
        # threshold is crossed.
        rick = game.players[0]
        ascension = make_card("Beastmaster Ascension", type_line="Enchantment",
                              power=None, toughness=None,
                              oracle_text=self.ORACLE, counters={"quest": 1})
        bear = make_card("Runeclaw Bear")
        rick.battlefield.extend([ascension, bear])
        game.recalculate_power_toughness()
        assert bear.get_effective_power(game) == 2
        ascension.counters["quest"] = 7
        game.recalculate_power_toughness()
        assert bear.get_effective_power(game) == 7
        ascension.counters["quest"] = 3
        game.recalculate_power_toughness()
        assert bear.get_effective_power(game) == 2


class TestHumilityKeywordStrip:
    ORACLE = "All creatures lose all abilities and have base power and toughness 1/1."

    def test_flyer_loses_flying_and_pt(self, game, make_card):
        # May 30 (option b): under a remove-all-abilities effect, has_keyword
        # defers to the layers engine's timestamp-ordered Layer-6 resolved
        # set instead of reading raw keyword lists.
        rick, claude = game.players
        humility = make_card("Humility", type_line="Enchantment",
                             power=None, toughness=None, oracle_text=self.ORACLE)
        flyer = make_card("Wind Drake", type_line="Creature — Drake",
                          power="4", toughness="4", keywords=["Flying"])
        rick.battlefield.append(humility)
        claude.battlefield.append(flyer)   # "all creatures" hits both sides
        game.register_static_pt_effects(humility, "Rick")
        game.recalculate_power_toughness()
        assert flyer.get_effective_power(game) == 1
        assert flyer.get_effective_toughness(game) == 1
        assert not flyer.has_keyword("Flying", game=game)
