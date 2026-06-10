"""Validate every hardcoded card name against Scryfall bulk data.

The exact-name-typo bug class: template keys and saga-table entries are
string-keyed by card name, so a typo silently falls through to a lower tier
(or to nothing). The May 17 "Cathars' Crusade" apostrophe bug shipped this
way and cost an audit cycle — this script would have flagged it instantly.

What it checks:
  - rules/effect_templates.py: every key in _card_templates,
    _dies_templates, _attack_templates (synthetic scheduling suffixes like
    "agent of treachery endstep" are stripped before lookup)
  - mtg/sba.py: _TRANSFORMING_SAGA_BACK_FACES keys AND back-face names

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
    args = ap.parse_args(argv)

    try:
        bulk_path = args.bulk or ensure_bulk(args.refresh)
        names = build_name_set(bulk_path)
    except Exception as e:
        print(f"[VALIDATOR] FAILED to obtain/parse bulk data: {e}")
        return 2
    print(f"[VALIDATOR] {len(names):,} valid names loaded")

    entries = collect_hardcoded_names()
    print(f"[VALIDATOR] checking {len(entries)} hardcoded names "
          f"({len(ALLOWLIST)} allowlisted)")

    unknown = []
    for raw, lookup, source in entries:
        if lookup in ALLOWLIST or lookup in names:
            continue
        suggestion = difflib.get_close_matches(lookup, names, n=1, cutoff=0.8)
        unknown.append((raw, source, suggestion[0] if suggestion else "(no close match)"))

    if not unknown:
        print("[VALIDATOR] OK — every hardcoded card name exists on Scryfall")
        return 0

    print(f"[VALIDATOR] {len(unknown)} UNKNOWN name(s):")
    for raw, source, suggestion in unknown:
        print(f"  - {raw!r}  [{source}]  did you mean: {suggestion!r}?")
    print("Fix the key, or add it to ALLOWLIST with a justifying comment.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
