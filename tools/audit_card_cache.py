"""Audit data/card_data_cache.json against Scryfall's bulk-data dump.

Background: the May 13 audit found Thassa, Deep-Dwelling cached as a 6/5 for
{3}{U} (real card: 3/5 for {2}{U}) and Heliod, Sun-Crowned cached at {2}{W}
(real card: {1}{W}). Both went through the standard Scryfall fetch path, so
either Scryfall briefly returned bad data at some point, or someone seeded
the cache by hand and got these wrong. Either way, we can't trust the cache
without a checker.

This script downloads Scryfall's `oracle-cards` bulk file (one big JSON
dump, ~150MB) and compares every cached card's printed stats to the
canonical oracle entry, flagging differences.

USAGE:
    # Audit only — report mismatches, don't modify the cache:
    python tools/audit_card_cache.py

    # Audit AND fix — overwrite the cache entries for any mismatches
    # with the canonical Scryfall data. Makes a .bak copy first.
    python tools/audit_card_cache.py --fix

    # Audit just a few cards by name (case-insensitive substring match):
    python tools/audit_card_cache.py --filter "thassa"

FIELDS COMPARED:
    name, mana_cost, cmc, type_line, power, toughness, loyalty, oracle_text,
    keywords (set comparison), colors (set), color_identity (set).

Format-level fields (legalities, prices, image_uris, set, released_at, etc.)
are NOT compared — those drift between snapshots and aren't gameplay-relevant.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
CACHE_PATH = ROOT / "data" / "card_data_cache.json"
BULK_URL = "https://api.scryfall.com/bulk-data/oracle-cards"
SCRYFALL_UA = "discord-mtg-bot-cache-audit/1.0"

# Fields that affect gameplay correctness. Mismatches in these are bugs.
GAMEPLAY_FIELDS = (
    "name", "mana_cost", "cmc", "type_line", "power", "toughness", "loyalty",
    "oracle_text",
)
# Set-valued fields. Compare as sets (order doesn't matter).
SET_FIELDS = ("keywords", "colors", "color_identity")


def _http_get_json(url: str) -> Any:
    # Scryfall requires both User-Agent and Accept headers. Without Accept
    # they return HTTP 400.
    req = urllib.request.Request(url, headers={
        "User-Agent": SCRYFALL_UA,
        "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=300) as resp:
        return json.load(resp)


def fetch_bulk_oracle() -> Dict[str, Dict[str, Any]]:
    """Returns oracle_id -> card-data dict from Scryfall's oracle-cards bulk."""
    print(f"[AUDIT] Fetching bulk-data metadata from {BULK_URL}")
    meta = _http_get_json(BULK_URL)
    download_uri = meta["download_uri"]
    size_mb = meta.get("size", 0) / 1024 / 1024
    print(f"[AUDIT] Downloading oracle-cards bulk ({size_mb:.1f} MB) from {download_uri}")
    cards = _http_get_json(download_uri)
    print(f"[AUDIT] Downloaded {len(cards)} oracle cards")
    by_oracle = {}
    for c in cards:
        oid = c.get("oracle_id")
        if oid:
            by_oracle[oid] = c
    return by_oracle


def _normalize_for_compare(value: Any) -> Any:
    """Strip whitespace, normalize line endings, collapse Nones."""
    if value is None:
        return None
    if isinstance(value, str):
        # Normalize newlines and trim trailing whitespace per-line.
        lines = [ln.rstrip() for ln in value.replace("\r\n", "\n").split("\n")]
        return "\n".join(lines).strip()
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, list):
        return [_normalize_for_compare(v) for v in value]
    return value


def diff_card(cached: Dict[str, Any], canonical: Dict[str, Any]
              ) -> List[Tuple[str, Any, Any]]:
    """Return list of (field, cached_value, canonical_value) mismatches.

    For double-faced / adventure / split cards (anything with `card_faces`),
    the engine's deck_loader deliberately caches the FRONT FACE's type_line,
    mana_cost, and oracle_text (see deck_loader.py:480-490). Comparing
    against Scryfall's top-level combined "Front // Back" type_line would
    produce false-positive mismatches on every DFC, so we prefer the
    front-face view of any DFC canonical entry.
    """
    mismatches: List[Tuple[str, Any, Any]] = []
    faces = canonical.get("card_faces") or []
    front = faces[0] if faces else {}
    is_dfc = bool(faces)

    # Type-line / colors live on the front face for DFCs (see comment).
    # mana_cost / oracle_text also live on the front face when the top-level
    # is empty; mana_cost on adventures lives on the front face's "creature".
    def _front_field(field_name: str, top_level: Any) -> Any:
        if is_dfc and field_name in ("type_line", "colors", "color_identity"):
            # Prefer front face — Scryfall reports "Front // Back" at top
            # level which is never what we cache. Fall back to top-level
            # when the front face is missing the field entirely (some
            # split cards put color_identity only on the top-level node).
            front_val = front.get(field_name)
            if front_val is None or (isinstance(front_val, (list, str)) and not front_val):
                return top_level
            return front_val
        if (top_level in (None, "", 0)) and front:
            front_val = front.get(field_name)
            if front_val not in (None, "", 0):
                return front_val
        return top_level

    for f in GAMEPLAY_FIELDS:
        cv = _normalize_for_compare(cached.get(f))
        raw_sv = canonical.get(f)
        sv = _normalize_for_compare(_front_field(f, raw_sv))
        if f == "cmc":
            try:
                cv = float(cv) if cv is not None else None
                sv = float(sv) if sv is not None else None
            except Exception:
                pass
        if cv != sv:
            mismatches.append((f, cv, sv))

    for f in SET_FIELDS:
        cv = set(cached.get(f) or [])
        raw_sv = canonical.get(f)
        sv_source = _front_field(f, raw_sv)
        sv = set(sv_source or [])
        if cv != sv:
            mismatches.append((f, sorted(cv), sorted(sv)))
    return mismatches


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--fix", action="store_true",
                        help="Overwrite cached entries with canonical Scryfall data")
    parser.add_argument("--filter", default="",
                        help="Only audit cards whose name contains this substring (case-insensitive)")
    parser.add_argument("--cache", default=str(CACHE_PATH),
                        help=f"Cache file path (default: {CACHE_PATH})")
    parser.add_argument("--report", default="",
                        help="Optional path to write a JSON report of mismatches")
    args = parser.parse_args()

    cache_path = Path(args.cache)
    if not cache_path.exists():
        print(f"[AUDIT] Cache not found: {cache_path}")
        sys.exit(1)

    print(f"[AUDIT] Loading cache from {cache_path}")
    with open(cache_path, "r", encoding="utf-8") as f:
        cache = json.load(f)
    print(f"[AUDIT] {len(cache)} cards in cache")

    canonical = fetch_bulk_oracle()
    filter_lc = args.filter.lower().strip()

    report: List[Dict[str, Any]] = []
    seen = 0
    missing_oracle_id = 0
    missing_in_scryfall = 0

    for cache_key, cached_entry in cache.items():
        name = cached_entry.get("name", cache_key)
        if filter_lc and filter_lc not in name.lower() and filter_lc not in cache_key.lower():
            continue
        seen += 1
        oid = cached_entry.get("oracle_id")
        if not oid:
            missing_oracle_id += 1
            continue
        canon = canonical.get(oid)
        if not canon:
            missing_in_scryfall += 1
            continue
        diffs = diff_card(cached_entry, canon)
        if not diffs:
            continue
        report.append({
            "key": cache_key,
            "name": name,
            "oracle_id": oid,
            "scryfall_uri": canon.get("scryfall_uri", ""),
            "diffs": [{"field": f, "cached": cv, "canonical": sv} for f, cv, sv in diffs],
        })
        # Console-friendly summary
        print(f"\n  MISMATCH: {name} ({cache_key})")
        for f, cv, sv in diffs:
            # Truncate long oracle_text to keep the report readable.
            if isinstance(cv, str) and len(cv) > 120:
                cv = cv[:117] + "..."
            if isinstance(sv, str) and len(sv) > 120:
                sv = sv[:117] + "..."
            print(f"     - {f}:")
            print(f"         cached:    {cv!r}")
            print(f"         canonical: {sv!r}")

    print(f"\n[AUDIT] Audited {seen} cards (filter={filter_lc!r})")
    print(f"[AUDIT] {len(report)} mismatch(es)")
    if missing_oracle_id:
        print(f"[AUDIT] {missing_oracle_id} cached entries have no oracle_id (cannot audit)")
    if missing_in_scryfall:
        print(f"[AUDIT] {missing_in_scryfall} cached entries not found in Scryfall bulk by oracle_id")

    if args.report:
        with open(args.report, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        print(f"[AUDIT] Wrote JSON report to {args.report}")

    if args.fix and report:
        # Back up the original cache before mutating.
        bak = cache_path.with_suffix(cache_path.suffix + ".bak")
        print(f"[AUDIT] --fix: backing up cache to {bak}")
        with open(bak, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=1)
        # Surgical fix: update ONLY the fields that mismatched (per diff_card),
        # using the front-face view for DFCs. This preserves engine-specific
        # caching choices (front-face-only type_line/mana_cost on DFCs, the
        # split_costs / adventure_cost fields the loader stamps, etc.) instead
        # of replacing the whole entry with Scryfall's top-level node.
        applied_fields = 0
        applied_entries = 0
        for entry in report:
            key = entry["key"]
            canon = canonical.get(entry["oracle_id"])
            if not canon:
                continue
            faces = canon.get("card_faces") or []
            front = faces[0] if faces else {}
            is_dfc = bool(faces)
            entry_changed = False
            for d in entry["diffs"]:
                field = d["field"]
                # Determine canonical value for this field (front-face-aware).
                if is_dfc and field in ("type_line", "colors", "color_identity"):
                    new_val = front.get(field)
                    if new_val is None or (isinstance(new_val, (list, str)) and not new_val):
                        new_val = canon.get(field)
                else:
                    new_val = canon.get(field)
                    if (new_val in (None, "", 0)) and front:
                        fv = front.get(field)
                        if fv not in (None, "", 0):
                            new_val = fv
                # Skip overwriting cached front-face type_line when the
                # canonical top-level still has "//" (would re-introduce
                # the engine-breaking combined string).
                if field == "type_line" and isinstance(new_val, str) and "//" in new_val:
                    front_tl = front.get("type_line")
                    if front_tl and "//" not in front_tl:
                        new_val = front_tl
                cache[key][field] = new_val
                applied_fields += 1
                entry_changed = True
            if entry_changed:
                applied_entries += 1
        # Write back with the original indent=1 to keep diffs small.
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=1)
        print(f"[AUDIT] --fix: updated {applied_fields} field(s) across {applied_entries} entries")

    if report and not args.fix:
        print("\nRe-run with --fix to overwrite cached entries with canonical Scryfall data.")

    sys.exit(0 if not report else 2)


if __name__ == "__main__":
    main()
