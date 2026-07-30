"""July 30, 2026 — the templating-drift detector (tools/validate_card_names.py).

Templating drift is a named bug class: WotC retemplating silently
un-matches pattern families (2026 "this creature" made Blood Artist
detection dead code; Rancor's graveyard wording never matched the LTB
gate), and every instance so far was found post-hoc in a batch. The
weekly CI job now snapshots per-pattern bulk hit counts and alarms on
drops — drift announces itself BEFORE a batch loses a trigger family.
"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import validate_card_names as vcn  # noqa: E402


class TestBaselineFile:
    def test_baseline_exists_and_is_sane(self):
        p = ROOT / "data" / "pattern_hit_baseline.json"
        assert p.exists(), (
            "the drift detector is blind without a committed baseline — "
            "regenerate with tools/validate_card_names.py --update-baseline")
        b = json.loads(p.read_text(encoding="utf-8"))
        assert len(b) >= 50, "the pattern registry has ~90 families"
        assert all(isinstance(v, int) and v >= 0 for v in b.values())

    def test_baseline_mostly_matches_the_live_registry(self):
        # Lenient on purpose: a pattern edit legitimately changes keys and
        # regenerating needs the 170MB bulk (which contributors may not
        # have) — the validator itself warns on per-key mismatches. This
        # only catches MASS staleness (baseline forgotten for months).
        from rules.effect_templates import get_effect_library
        live = {p for p, _t in get_effect_library()._pattern_templates}
        base = set(json.loads(
            (ROOT / "data" / "pattern_hit_baseline.json")
            .read_text(encoding="utf-8")))
        overlap = len(live & base) / max(1, len(live))
        assert overlap >= 0.8, (
            f"only {overlap:.0%} of live patterns are in the baseline — "
            f"run tools/validate_card_names.py --update-baseline")


class TestDriftMath:
    def _fake_bulk(self, n_scry):
        cards = [{"name": f"Scry Guy {i}", "layout": "normal",
                  "oracle_text": "Scry 2."} for i in range(n_scry)]
        cards.append({"name": "Art Card", "layout": "art_series",
                      "oracle_text": "Scry 2."})  # must be excluded
        return cards

    def test_drop_past_both_thresholds_alarms(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(vcn, "PATTERN_BASELINE",
                            tmp_path / "baseline.json")
        # Baseline: the scry family matched 20 cards; live bulk has 10.
        vcn.check_pattern_drift(self._fake_bulk(20), update_baseline=True)
        capsys.readouterr()
        alarms = vcn.check_pattern_drift(self._fake_bulk(10))
        out = capsys.readouterr().out
        assert alarms >= 1
        assert "PATTERN-DRIFT" in out

    def test_small_or_relative_only_drops_stay_quiet(self, tmp_path, monkeypatch):
        monkeypatch.setattr(vcn, "PATTERN_BASELINE",
                            tmp_path / "baseline.json")
        vcn.check_pattern_drift(self._fake_bulk(20), update_baseline=True)
        # 20 -> 18: 10% relative and 2 absolute — both under threshold.
        assert vcn.check_pattern_drift(self._fake_bulk(18)) == 0

    def test_rises_never_alarm(self, tmp_path, monkeypatch):
        monkeypatch.setattr(vcn, "PATTERN_BASELINE",
                            tmp_path / "baseline.json")
        vcn.check_pattern_drift(self._fake_bulk(10), update_baseline=True)
        assert vcn.check_pattern_drift(self._fake_bulk(200)) == 0, (
            "new cards raising hit counts is normal, never drift")

    def test_art_series_layouts_excluded(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(vcn, "PATTERN_BASELINE",
                            tmp_path / "baseline.json")
        vcn.check_pattern_drift(self._fake_bulk(10), update_baseline=True)
        b = json.loads((tmp_path / "baseline.json").read_text(encoding="utf-8"))
        scry_counts = [v for k, v in b.items() if "scry" in k]
        assert scry_counts and max(scry_counts) == 10, (
            "the July 21 gotcha: token/art_series layouts must not count")
