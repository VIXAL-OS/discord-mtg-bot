"""Validate hardcoded names and the local card cache against Scryfall bulk data.

The exact-name-typo bug class: template keys and saga-table entries are
string-keyed by card name, so a typo silently falls through to a lower tier
(or to nothing). The May 17 "Cathars' Crusade" apostrophe bug shipped this
way and cost an audit cycle — this script would have flagged it instantly.

What it checks:
  - rules/effect_templates.py: every key in _card_templates,
    _dies_templates, _attack_templates (synthetic scheduling suffixes like
    "agent of treachery endstep" are stripped before lookup)
  - mtg/sba.py: _TRANSFORMING_SAGA_BACK_FACES keys AND back-face names
  - data/card_data_cache.json: top-level and card-face oracle text exactly
    matches the current Scryfall oracle record (apart from line endings and
    surrounding whitespace)

Against:
  - Scryfall "oracle_cards" bulk data (~170 MB, one request, cached at
    data/scryfall_oracle_cards.json — gitignored). Full card names,
    split/DFC halves, and individual face names all count as valid.

Usage (from repo root):
  python tools/validate_card_names.py             # download bulk if missing
  python tools/validate_card_names.py --refresh   # force re-download
  python tools/validate_card_names.py --bulk P    # use an existing bulk file

Exit codes: 0 = all names valid, 1 = unknown names found, 2 = bulk data
unavailable. CI runs this weekly + on template/saga-table changes
(.github/workflows/card-names.yml).
"""
import argparse
import difflib
import json
import sys
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

# Windows consoles default to cp1252; mtg/__init__ prints a ✅ probe line.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BULK_CACHE = REPO / "data" / "scryfall_oracle_cards.json"
CARD_CACHE = REPO / "data" / "card_data_cache.json"
BULK_INDEX_URL = "https://api.scryfall.com/bulk-data"
USER_AGENT = "mtg-bot-card-name-validator/1.0 (https://github.com/VIXAL-OS)"

# Template keys registered under a "<card name> <event>" scheduling key —
# strip the suffix and validate the card-name half. (Both space- and
# underscore-joined variants exist in the registries: "spell queller_ltb",
# "detention sphere ltb", "korvold, fae-cursed king sacrifice".)
SYNTHETIC_SUFFIXES = (
    " endstep", " upkeep", " beginningcombat",
    " ltb", "_ltb", " sacrifice",
)

# Intentional non-card keys. Every entry needs a justifying comment.
ALLOWLIST = {
    # Deliberate alt-spelling key: autoplay logs have shown both spellings in
    # the wild, so the template registers under the typo too (May 17 audit).
    # The real card is "Cathars' Crusade".
    "cathar's crusade",
}


def _request(url: str) -> urllib.request.Request:
    return urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})


def ensure_bulk(refresh: bool) -> Path:
    """Download the Scryfall oracle-cards bulk file if missing (or refresh)."""
    if BULK_CACHE.exists() and not refresh:
        return BULK_CACHE
    print(f"[VALIDATOR] fetching bulk-data index: {BULK_INDEX_URL}")
    with urllib.request.urlopen(_request(BULK_INDEX_URL), timeout=120) as r:
        index = json.load(r)
    uri = next(d["download_uri"] for d in index["data"]
               if d["type"] == "oracle_cards")
    print(f"[VALIDATOR] downloading oracle_cards bulk (~170 MB): {uri}")
    BULK_CACHE.parent.mkdir(parents=True, exist_ok=True)
    tmp = BULK_CACHE.with_suffix(".part")
    with urllib.request.urlopen(_request(uri), timeout=900) as r, \
            open(tmp, "wb") as f:
        while chunk := r.read(1 << 20):
            f.write(chunk)
    tmp.replace(BULK_CACHE)
    print(f"[VALIDATOR] saved {BULK_CACHE} ({BULK_CACHE.stat().st_size // (1 << 20)} MB)")
    return BULK_CACHE


def build_name_set(bulk_path: Path) -> set:
    """All valid lowercase names: full names, split halves, face names."""
    cards = json.loads(Path(bulk_path).read_text(encoding="utf-8"))
    names = set()
    for c in cards:
        full = (c.get("name") or "").strip().lower()
        if full:
            names.add(full)
            if "//" in full:
                for half in full.split("//"):
                    names.add(half.strip())
        for face in c.get("card_faces") or []:
            fn = (face.get("name") or "").strip().lower()
            if fn:
                names.add(fn)
    return names


def build_oracle_index(cards: list) -> dict:
    """Map lowercase full/face names to their current Scryfall records."""
    records = {}
    priorities = {}

    def add(name: str, record: dict, priority: int):
        key = (name or "").strip().lower()
        if not key:
            return
        existing = records.get(key)
        existing_priority = priorities.get(key, -1)
        # Bulk data may contain duplicate-name art/face records with no rules
        # text. A standalone normal card outranks a same-name spell face
        # (e.g. 1999 Replenish vs the 2026 prepared face) and token record.
        if (existing is None or priority > existing_priority or (
                priority == existing_priority
                and not _normalized_oracle(existing)
                and _normalized_oracle(record))):
            records[key] = record
            priorities[key] = priority

    for card in cards:
        layout = (card.get("layout") or "normal").lower()
        full_priority = 3 if layout not in {
            "token", "double_faced_token", "emblem", "art_series"} else 0
        add(card.get("name") or "", card, full_priority)
        for face in card.get("card_faces") or []:
            add(face.get("name") or "", face, 1)
    return records


def _normalized_oracle(record: dict) -> str:
    return (record.get("oracle_text") or "").replace("\r\n", "\n").strip()


def find_oracle_mismatches(cache: dict, oracle_index: dict) -> list:
    """Return cache oracle differences as (name, cached, current) tuples."""
    mismatches = []

    def compare(record: dict, fallback_name: str = ""):
        name = (record.get("name") or fallback_name).strip()
        if not name:
            return
        reference = oracle_index.get(name.lower())
        if reference is None:
            return  # Name validation/reporting is handled separately.
        cached_text = _normalized_oracle(record)
        current_text = _normalized_oracle(reference)
        # Split/transform cards intentionally have no top-level oracle text;
        # their face records are compared separately below. Empty art/token
        # records likewise provide no authoritative text to compare.
        if not current_text:
            return
        if cached_text != current_text:
            mismatches.append((name, cached_text, current_text))

    for cache_key, card in cache.items():
        if not isinstance(card, dict):
            continue
        compare(card, fallback_name=cache_key)
        for face in card.get("card_faces") or []:
            if isinstance(face, dict):
                compare(face)
    return mismatches


def collect_hardcoded_names():
    """Yield (raw_key, lookup_name, source) for every hardcoded card name."""
    out = []

    from rules.effect_templates import get_effect_library
    lib = get_effect_library()
    for registry in ("_card_templates", "_dies_templates", "_attack_templates"):
        for key in sorted(getattr(lib, registry, {})):
            lookup = key.strip().lower()
            for suffix in SYNTHETIC_SUFFIXES:
                if lookup.endswith(suffix):
                    lookup = lookup[: -len(suffix)].strip()
                    break
            out.append((key, lookup, f"effect_templates.{registry}"))

    from mtg.sba import _TRANSFORMING_SAGA_BACK_FACES as SAGAS
    for front, back in sorted(SAGAS.items()):
        out.append((front, front.strip().lower(), "sba saga-table key"))
        back_name = (back.get("name") or "").strip().lower()
        if back_name:
            out.append((back_name, back_name,
                        f"sba saga back-face of '{front}'"))
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--bulk", type=Path, default=None,
                    help="path to an existing oracle_cards bulk JSON")
    ap.add_argument("--refresh", action="store_true",
                    help="force re-download of the bulk file")
    ap.add_argument("--cache-only", action="store_true",
                    help="only validate card_data_cache.json oracle text")
    ap.add_argument("--names-only", action="store_true",
                    help="only validate hardcoded card names")
    args = ap.parse_args(argv)
    if args.cache_only and args.names_only:
        ap.error("--cache-only and --names-only are mutually exclusive")

    try:
        bulk_path = args.bulk or ensure_bulk(args.refresh)
        bulk_cards = json.loads(Path(bulk_path).read_text(encoding="utf-8"))
        names = build_name_set(bulk_path)
        oracle_index = build_oracle_index(bulk_cards)
    except Exception as e:
        print(f"[VALIDATOR] FAILED to obtain/parse bulk data: {e}")
        return 2
    print(f"[VALIDATOR] {len(names):,} valid names loaded")

    failed = False
    unknown = []
    if not args.cache_only:
        entries = collect_hardcoded_names()
        print(f"[VALIDATOR] checking {len(entries)} hardcoded names "
              f"({len(ALLOWLIST)} allowlisted)")

        for raw, lookup, source in entries:
            if lookup in ALLOWLIST or lookup in names:
                continue
            suggestion = difflib.get_close_matches(lookup, names, n=1, cutoff=0.8)
            unknown.append((raw, source, suggestion[0] if suggestion else "(no close match)"))

        if unknown:
            failed = True
            print(f"[VALIDATOR] {len(unknown)} UNKNOWN name(s):")
            for raw, source, suggestion in unknown:
                print(f"  - {raw!r}  [{source}]  did you mean: {suggestion!r}?")
            print("Fix the key, or add it to ALLOWLIST with a justifying comment.")
        else:
            print("[VALIDATOR] OK — every hardcoded card name exists on Scryfall")

    if not args.names_only:
        try:
            cache = json.loads(CARD_CACHE.read_text(encoding="utf-8"))
            mismatches = find_oracle_mismatches(cache, oracle_index)
        except Exception as e:
            print(f"[VALIDATOR] FAILED to parse {CARD_CACHE}: {e}")
            return 2
        print(f"[VALIDATOR] checking oracle text for {len(cache):,} cached cards")
        if mismatches:
            failed = True
            print(f"[VALIDATOR] {len(mismatches)} STALE oracle text entr{'y' if len(mismatches) == 1 else 'ies'}:")
            for name, cached, current in mismatches[:50]:
                print(f"  - {name}")
                print(f"      cache:    {cached[:240]!r}")
                print(f"      Scryfall: {current[:240]!r}")
            if len(mismatches) > 50:
                print(f"  ... and {len(mismatches) - 50} more")
        else:
            print("[VALIDATOR] OK — cached oracle text matches Scryfall")

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
