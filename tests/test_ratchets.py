"""Debt ratchets + conventions (June 10, 2026).

Two documented debt classes get COUNTED here; CI fails if a count GROWS:

1. **Undeclared attribute staples** — `game._x = ...` / `player._x = ...` /
   `card._x = ...` where `_x` is not a declared dataclass field. Each staple
   is invisible to to_dict, !undo carry-over, and new contributors. Fix by
   DECLARING the field with a default in mtg/models.py (see the "Transient
   runtime state" blocks) — declared-field assignments are exempt, so
   declaring ratchets the count DOWN.

2. **Broad excepts per file** — `except Exception:` / bare `except:` in
   pure-engine paths convert crashes into silently-wrong game states. New
   ones should be narrowed; where a swallow is genuinely intended, keep the
   log line and add `maybe_reraise(e)` (see mtg/util.py docstring) so strict
   mode / CI re-raises. Note: the regex also matches mentions inside
   comments and docstrings — deterministic noise, included in baselines.

To recompute baselines after deliberate cleanup:

    venv\\Scripts\\python.exe tools\\_compute_ratchet_baselines.py

Lowering a baseline is progress — update freely. Raising one needs a reason
in the commit message.
"""
import re
from dataclasses import fields as dc_fields
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
MTG_FILES = sorted((REPO / "mtg").glob("*.py"))

# ---------------------------------------------------------------------------
# Ratchet 1: undeclared attribute staples
# ---------------------------------------------------------------------------

# Baseline June 10, 2026 (after declaring the 14 load-bearing transient
# fields on Card/Player/GameState, which removed ~55 sites from the census).
STAPLE_BASELINE = 107

_STAPLE_RE = re.compile(r"\b(game|player|card)\.(_[a-z0-9_]+)\s*=[^=]")


def _staple_sites():
    from mtg.models import Card, GameState, Player
    declared = {
        "game": {f.name for f in dc_fields(GameState)},
        "player": {f.name for f in dc_fields(Player)},
        "card": {f.name for f in dc_fields(Card)},
    }
    per_attr = {}
    for path in MTG_FILES:
        text = path.read_text(encoding="utf-8")
        for m in _STAPLE_RE.finditer(text):
            var, attr = m.group(1), m.group(2)
            if attr in declared[var]:
                continue
            per_attr.setdefault(attr, []).append(path.name)
    return per_attr


def test_undeclared_staple_count_does_not_grow():
    per_attr = _staple_sites()
    count = sum(len(v) for v in per_attr.values())
    top = ", ".join(
        f"{k}×{len(v)}" for k, v in
        sorted(per_attr.items(), key=lambda kv: -len(kv[1]))[:8]
    )
    assert count <= STAPLE_BASELINE, (
        f"Undeclared staple sites grew: {count} > baseline {STAPLE_BASELINE}.\n"
        f"Top offenders: {top}\n"
        f"Declare new runtime attrs as dataclass fields in mtg/models.py "
        f"(transient blocks) instead of stapling. Recompute via "
        f"tools/_compute_ratchet_baselines.py."
    )


# ---------------------------------------------------------------------------
# Ratchet 2: broad excepts per file
# ---------------------------------------------------------------------------

# Baseline June 10, 2026. Files absent from this dict are allowed 0.
EXCEPT_BASELINE = {
    "actions.py": 21,
    "ai_turn.py": 19,
    "autoplay.py": 18,
    "claude_player.py": 33,
    "cog.py": 30,
    "combat.py": 4,
    "coverage.py": 1,
    "deck_loader.py": 4,
    "engine.py": 41,
    "helpers.py": 1,
    "judge.py": 10,
    "models.py": 14,
    "rules_engine.py": 4,
    "sba.py": 3,
    "spells.py": 29,
    "triggers.py": 47,
    "util.py": 6,
}

_EXCEPT_RE = re.compile(r"except(\s+Exception(\s+as\s+\w+)?)?\s*:")


def test_broad_except_counts_do_not_grow_per_file():
    over = []
    for path in MTG_FILES:
        n = len(_EXCEPT_RE.findall(path.read_text(encoding="utf-8")))
        allowed = EXCEPT_BASELINE.get(path.name, 0)
        if n > allowed:
            over.append(f"{path.name}: {n} > baseline {allowed}")
    assert not over, (
        "Broad except count grew:\n  " + "\n  ".join(over) + "\n"
        "Narrow the new catch, or keep the log line and add maybe_reraise(e) "
        "(mtg/util.py) if a production crash barrier is genuinely intended — "
        "then bump the baseline with a justification in the commit."
    )


# ---------------------------------------------------------------------------
# Convention: MTG_STRICT / maybe_reraise (mtg/util.py)
# ---------------------------------------------------------------------------


class TestStrictMode:
    def test_reraises_under_strict(self, monkeypatch):
        monkeypatch.setenv("MTG_STRICT", "1")
        from mtg.util import maybe_reraise
        with pytest.raises(ValueError, match="boom"):
            try:
                raise ValueError("boom")
            except Exception as e:
                maybe_reraise(e)

    def test_swallows_without_strict(self, monkeypatch):
        monkeypatch.setenv("MTG_STRICT", "0")
        from mtg.util import maybe_reraise
        try:
            raise ValueError("boom")
        except Exception as e:
            maybe_reraise(e)  # must NOT raise


# ---------------------------------------------------------------------------
# Convention: names_match (mtg/helpers.py) — card-name identity checks
# ---------------------------------------------------------------------------


class TestNamesMatch:
    def test_exact_match_case_and_whitespace_insensitive(self):
        from mtg.helpers import names_match
        assert names_match("Painter's Servant", "  painter's servant ")

    def test_apostrophe_placement_does_not_match(self):
        # The May 17 bug class: these are DIFFERENT strings and must not match.
        from mtg.helpers import names_match
        assert not names_match("Cathars' Crusade", "Cathar's Crusade")

    def test_substring_does_not_match(self):
        # The Coldsteel Heart → Painter's Servant misfire class.
        from mtg.helpers import names_match
        assert not names_match("Painter's Servant", "Servant")
        assert not names_match("Heart", "Coldsteel Heart")

    def test_empty_never_matches(self):
        from mtg.helpers import names_match
        assert not names_match("", "")
        assert not names_match(None, None)
