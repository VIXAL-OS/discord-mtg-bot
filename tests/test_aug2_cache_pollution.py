"""Aug 2, 2026 — the phantom-keyword cache-pollution class (B4).

The mechanism, found while investigating an uncommitted cache diff
(which turned out to be BOT-WRITTEN, not a human edit): `_front_face_keywords` returned the
cache dict's own list object for single-faced cards, `Card(keywords=...)`
aliased it, four runtime grant sites appended durable keywords to
`card.keywords` (the Sneak Attack haste class in cog.py/engine.py + the
Tier-2 pump exec's until-EOT grants), and the next cache save persisted the
grants to disk. 20 committed entries carried phantom keywords — 19x Haste on
the mythic deck's sneak targets (Emrakuls, Kozileks, Wurmcoil, Hellkite
Tyrant...) + a lowercase 'flying' on Tovolar. This also retroactively
explains the July 30 "wrong Twinflame Tyrant haste edit" mystery: not a
hallucinated edit — the bot's own pollution, snapshotted mid-recovery.

Fixes pinned here:
- _front_face_keywords ALWAYS returns a copy (the boundary).
- The four grant sites write temp_keywords (correct duration too — the
  pump exec's own message already said "until end of turn"; sneaked
  creatures sacrifice at end step, so the end-of-turn clear is right).
- tools/validate_card_names.py grows find_keyword_pollution (additions-only
  vs the bulk) so CI catches any future writer regression.
- The 20 polluted entries were re-synced from the local bulk (the cache
  commit alongside this test).

Also settled: the reviewer-reported "cache mojibake" (U+FFFD in Converge —)
was a FALSE POSITIVE — the byte is U+2014, a real em dash; the reviewer read
the file through a cp1252 lens (the June 10 encoding-illusion class, second
occurrence — verify encoding claims with ord(), never terminal rendering).
"""
import asyncio
import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


class TestKeywordsBoundaryCopy:
    def test_front_face_keywords_never_aliases(self):
        from mtg.deck_loader import _front_face_keywords
        # Single-faced passthrough (the polluting branch)
        data = {"name": "Wurmcoil Engine",
                "keywords": ["Deathtouch", "Lifelink"]}
        out = _front_face_keywords(data)
        assert out == ["Deathtouch", "Lifelink"]
        assert out is not data["keywords"], (
            "the passthrough branch returned the cache dict's own list — "
            "every runtime keyword grant wrote straight into the cache")
        # Non-DFC multi-face layout passthrough (split/adventure)
        data2 = {"name": "Commit // Memory", "layout": "split",
                 "keywords": [], "card_faces": [{}, {}]}
        data2["keywords"] = ["Flash"]
        out2 = _front_face_keywords(data2)
        assert out2 is not data2["keywords"]

    def test_card_mutation_cannot_reach_the_source_dict(self):
        from mtg.deck_loader import _front_face_keywords
        from mtg.models import Card
        cache_entry = {"name": "Kozilek, the Great Distortion",
                       "keywords": ["Menace"]}
        card = Card(name="Kozilek, the Great Distortion",
                    type_line="Legendary Creature — Eldrazi",
                    power="12", toughness="12",
                    keywords=_front_face_keywords(cache_entry))
        card.keywords.append("Haste")  # a hypothetical bad grant site
        assert cache_entry["keywords"] == ["Menace"], (
            "the in-memory cache entry mutated — the persistence pollution "
            "vector is open again")


class TestGrantSitesUseTempKeywords:
    def test_pump_exec_grants_temporarily(self, game, make_card):
        from rules.effects import Effect, EffectType, ExecutionContext
        from rules.spell_resolver import SpellResolver
        rick = game.players[0]
        bear = make_card("Bear", type_line="Creature — Bear",
                         power="2", toughness="2")
        rick.battlefield.append(bear)
        printed = list(bear.keywords)
        effect = Effect(effect_type=EffectType.PUMP, power_mod=1,
                        toughness_mod=1, keywords_granted=["flying"])
        src = make_card("Trick", type_line="Instant",
                        power=None, toughness=None)
        ctx = ExecutionContext(game_state=game, source_card=src,
                               source_controller=rick, targets=[bear])
        asyncio.run(SpellResolver(None)._exec_pump(effect, ctx, game))
        assert "flying" in bear.temp_keywords
        assert bear.keywords == printed, (
            "an until-end-of-turn grant reached the PRINTED keywords list — "
            "the Tovolar phantom-'flying' vector")
        assert bear.has_keyword("flying"), (
            "temp_keywords must still satisfy has_keyword or the grant is lost")

    def test_no_raw_keyword_appends_at_the_grant_sites(self):
        """Structural: the three files whose durable grants polluted the
        cache must not grow a raw .keywords.append again (copy_token's
        append in actions.py operates on an explicitly copied list and
        stays sanctioned)."""
        for rel in ("mtg/cog.py", "mtg/engine.py", "rules/spell_resolver.py"):
            src = (REPO / rel).read_text(encoding="utf-8")
            assert ".keywords.append" not in src, (
                f"{rel}: a grant site writes the printed keywords list again")


class TestCacheClean:
    def test_committed_cache_has_no_phantom_haste(self):
        """The 20 repaired entries stay repaired (fast local check against
        printed reality for the known victims — the full bulk comparison
        runs in CI via find_keyword_pollution)."""
        cache = json.loads(
            (REPO / "data" / "card_data_cache.json").read_text(encoding="utf-8"))
        for name in ("kozilek, the great distortion", "wurmcoil engine",
                     "emrakul, the promised end", "hellkite tyrant",
                     "moraug, fury of akoum", "worldgorger dragon"):
            kws = cache.get(name, {}).get("keywords") or []
            assert "Haste" not in kws, f"{name} re-polluted"
        tovolar = cache.get("tovolar, dire overlord", {}).get("keywords") or []
        assert "flying" not in tovolar

    def test_validator_pollution_check(self):
        from tools.validate_card_names import find_keyword_pollution
        index = {"wurmcoil engine": {"name": "Wurmcoil Engine",
                                     "keywords": ["Deathtouch", "Lifelink"]}}
        clean = {"wurmcoil engine": {"name": "Wurmcoil Engine",
                                     "keywords": ["Deathtouch", "Lifelink"]}}
        assert find_keyword_pollution(clean, index) == []
        dirty = {"wurmcoil engine": {"name": "Wurmcoil Engine",
                                     "keywords": ["Deathtouch", "Lifelink",
                                                  "Haste"]}}
        hits = find_keyword_pollution(dirty, index)
        assert hits == [("wurmcoil engine", ["Haste"])]
        # Scryfall ADDING a keyword upstream is legitimate drift, not pollution
        behind = {"wurmcoil engine": {"name": "Wurmcoil Engine",
                                      "keywords": ["Deathtouch"]}}
        assert find_keyword_pollution(behind, index) == []
