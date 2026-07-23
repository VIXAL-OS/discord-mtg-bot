"""July 22, 2026 — coverage classification must account for every subsystem.

`EffectTemplateLibrary.tier_for_card` answers only "does the Tier 1.5 library
handle this?" and returns "tier3" for everything else. Reporting that verbatim
to a user told them ~57% of the most-played Commander cards would be slow and
cost tokens — badly misleading, since the engine also resolves cards at Tier 1
(hardcoded handlers), Tier 2 (SpellResolver), Tier 2.5 (XMage), and the mana /
land / equipment subsystems, none of which the library can see.

mtg/coverage.supported_at_tier now layers those on top. These pin the buckets
so the report can't silently regress to the Tier-1.5-only view.
"""
import pytest

from mtg.coverage import (FREE_TIERS, TIERS, classify_deck,
                          format_coverage_report, supported_at_tier)


class _Card:
    def __init__(self, name, oracle_text="", type_line=""):
        self.name = name
        self.oracle_text = oracle_text
        self.type_line = type_line


class TestNoResolutionNeeded:
    def test_vanilla_creature_is_not_tier3(self):
        # A French vanilla bear has nothing to resolve. Calling it "tier3"
        # implied it would cost tokens at runtime; it never resolves anything.
        assert supported_at_tier("Grizzly Bears", "", "Creature — Bear") == "no_resolution"

    def test_french_vanilla_keywords_only(self):
        assert supported_at_tier(
            "Serra Angel", "Flying, vigilance", "Creature — Angel") == "no_resolution"

    def test_basic_land(self):
        assert supported_at_tier("Forest", "({T}: Add {G}.)", "Basic Land — Forest") == "no_resolution"

    def test_plain_mana_rock(self):
        assert supported_at_tier(
            "Fellwar Stone", "{T}: Add one mana of any color.", "Artifact") == "no_resolution"

    def test_land_with_a_real_etb_is_NOT_waved_through(self):
        # Bojuka Bog actually does something on entry — it must not be
        # dismissed as a plain land.
        tier = supported_at_tier(
            "Bojuka Bog",
            "This land enters tapped.\nWhen this land enters, exile target "
            "player's graveyard.",
            "Land")
        assert tier != "no_resolution"


class TestSubsystemLayering:
    def test_tier1_hardcoded_card_reported_as_free(self):
        # Rhystic Study is a Tier 1 hardcoded handler; the 1.5 library has no
        # entry for it, so the naive lookup called it tier3.
        assert supported_at_tier(
            "Rhystic Study",
            "Whenever an opponent casts a spell, you may draw a card unless "
            "that player pays {1}.",
            "Enchantment") in ("hardcoded", "template", "pattern")

    def test_simple_spell_falls_to_spell_resolver_not_tier3(self):
        tier = supported_at_tier(
            "Lightning Bolt", "Lightning Bolt deals 3 damage to any target.",
            "Instant")
        assert tier in FREE_TIERS, f"got {tier}"

    def test_xmage_probe_is_consulted_only_when_supplied(self):
        oracle = "Whenever this permanent does something ineffable, the game blinks."
        name = "Totally Novel Card"
        assert supported_at_tier(name, oracle, "Enchantment") == "tier3"
        assert supported_at_tier(name, oracle, "Enchantment",
                                 xmage_probe=lambda n: True) == "xmage"

    def test_broken_xmage_probe_does_not_crash_the_report(self):
        def boom(_):
            raise RuntimeError("bridge is down")
        # A dead bridge must degrade the report, never break it.
        assert supported_at_tier(
            "Totally Novel Card", "Whenever the ineffable happens, blink.",
            "Enchantment", xmage_probe=boom) == "tier3"


class TestDeckReport:
    def _deck(self):
        return [
            _Card("Grizzly Bears", "", "Creature — Bear"),
            _Card("Forest", "({T}: Add {G}.)", "Basic Land — Forest"),
            _Card("Lightning Bolt", "Lightning Bolt deals 3 damage to any target.", "Instant"),
        ]

    def test_counts_cover_every_tier_key(self):
        cov = classify_deck(self._deck())
        assert set(cov["counts"]) == set(TIERS)
        assert sum(cov["counts"].values()) == 3

    def test_report_leads_with_the_free_total(self):
        cov = classify_deck(self._deck())
        out = format_coverage_report(cov, "test deck")
        assert "resolve free and instantly" in out
        # none of this deck should be reported as costing tokens
        assert cov["counts"]["tier3"] == 0, cov["counts"]

    def test_report_survives_empty_deck(self):
        assert "empty deck" in format_coverage_report(classify_deck([]), "d")
