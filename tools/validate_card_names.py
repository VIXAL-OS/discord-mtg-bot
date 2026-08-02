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
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

# Windows consoles default to cp1252; mtg/__init__ prints a ✅ probe line.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BULK_CACHE = REPO / "data" / "scryfall_oracle_cards.json"
CARD_CACHE = REPO / "data" / "card_data_cache.json"
PATTERN_BASELINE = REPO / "data" / "pattern_hit_baseline.json"

# Templating-drift alarm thresholds (July 30, 2026): a pattern family's
# bulk hit count DROPPING is WotC retemplating announcing itself — the
# 2026 "this creature" rewording made Blood Artist detection dead code,
# and Rancor's "put into a graveyard from the battlefield" never matched
# the LTB gate; both were found post-hoc in batches. Rises are normal
# (new cards); only drops alarm, and only when both thresholds trip.
DRIFT_REL_THRESHOLD = 0.15  # >= 15% relative drop ...
DRIFT_ABS_THRESHOLD = 5     # ... AND >= 5 cards absolute
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


# July 31, 2026: retry transient fetch failures instead of failing the run.
# A single Scryfall 503 blip turned BOTH repos' card-names workflows red on
# July 30 (runs 30551833137 / 30552143272, fired in the same three-minute
# window; the next runs passed untouched). This workflow's value is that red
# MEANS something — a typo, WotC retemplating, a real API break — so
# transient availability must not cry wolf. Retryable: 5xx, 429, and network/
# timeout errors. NOT retryable: other 4xx (a 404 here is the July 29
# index-shape-change class — fail fast with the real message).
_RETRY_DELAYS = (10, 30, 60)  # seconds between attempts (4 attempts total)


def _urlopen_with_retries(url: str, timeout: int):
    """urlopen with backoff on transient failures. Returns the response."""
    import time as _time
    last_err = None
    for attempt, delay in enumerate((0,) + _RETRY_DELAYS):
        if delay:
            print(f"[VALIDATOR] retrying in {delay}s "
                  f"(attempt {attempt + 1}/{len(_RETRY_DELAYS) + 1}) after: {last_err}")
            _time.sleep(delay)
        try:
            return urllib.request.urlopen(_request(url), timeout=timeout)
        except urllib.error.HTTPError as e:
            if e.code >= 500 or e.code == 429:
                last_err = e
                continue
            raise  # other 4xx = a real problem, fail fast
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last_err = e
            continue
    raise last_err


def ensure_bulk(refresh: bool) -> Path:
    """Download the Scryfall oracle-cards bulk file if missing (or refresh).

    July 29, 2026: Scryfall replaced `download_uri` (a plain JSON array)
    with `jsonl_download_uri` (gzipped JSONL) in the bulk-data index — the
    old key now raises KeyError and CI died with exit 2 on every push.
    Support both shapes; the cache on disk stays a plain JSON array either
    way, so every consumer (build_name_set, --bulk users) is unchanged.
    gzip is stdlib, preserving the no-pip-installs contract.
    """
    if BULK_CACHE.exists() and not refresh:
        return BULK_CACHE
    print(f"[VALIDATOR] fetching bulk-data index: {BULK_INDEX_URL}")
    with _urlopen_with_retries(BULK_INDEX_URL, timeout=120) as r:
        index = json.load(r)
    entry = next(d for d in index["data"] if d["type"] == "oracle_cards")
    array_uri = entry.get("download_uri")
    jsonl_uri = entry.get("jsonl_download_uri")
    dl = array_uri or jsonl_uri
    if not dl:
        raise KeyError(
            "bulk-data index has neither download_uri nor jsonl_download_uri "
            f"(keys: {sorted(entry.keys())})")
    print(f"[VALIDATOR] downloading oracle_cards bulk: {dl}")
    BULK_CACHE.parent.mkdir(parents=True, exist_ok=True)
    tmp = BULK_CACHE.with_suffix(".part")
    with _urlopen_with_retries(dl, timeout=900) as r, \
            open(tmp, "wb") as f:
        while chunk := r.read(1 << 20):
            f.write(chunk)
    if not array_uri:
        # JSONL(.gz) → normalize to the JSON array every consumer expects.
        import gzip
        opener = gzip.open if dl.endswith(".gz") else open
        with opener(tmp, "rt", encoding="utf-8") as f:
            cards = [json.loads(line) for line in f if line.strip()]
        tmp.write_text(json.dumps(cards), encoding="utf-8")
        print(f"[VALIDATOR] normalized JSONL bulk to a JSON array "
              f"({len(cards):,} cards)")
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


def find_keyword_pollution(cache: dict, oracle_index: dict) -> list:
    """Cache entries whose keywords contain words NOT on the Scryfall record.

    Aug 2, 2026: runtime keyword grants (the Sneak Attack haste class) wrote
    through Card.keywords, which ALIASED the in-memory card cache, and the
    next cache save persisted phantom keywords to disk — 20 committed
    entries carried Haste no printing has (plus Tovolar's lowercase
    'flying' from the Tier-2 pump exec). Additions-only check: Scryfall
    ADDING keywords over time is legitimate upstream drift; the cache
    carrying keywords Scryfall lacks is pollution.
    """
    polluted = []
    for cache_key, card in cache.items():
        if not isinstance(card, dict):
            continue
        name = (card.get("name") or cache_key).strip().lower()
        ref = oracle_index.get(name)
        if ref is None or "keywords" not in ref:
            continue  # face records carry no keywords field — skip
        extra = set(card.get("keywords") or []) - set(ref.get("keywords") or [])
        if extra:
            polluted.append((name, sorted(extra)))
    return polluted


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


def check_pattern_drift(bulk_cards, update_baseline: bool = False) -> int:
    """Count bulk-oracle hits per Tier-1.5 regex pattern family; alarm on drops.

    Templating drift is a named bug class in this project: Arena
    auto-implements most cards because WotC's oracle templating is rigid,
    and our pattern families make the same bet — so when WotC RETEMPLATES
    (2019 "enters the battlefield" -> 2024 "enters"; 2026 "this creature"),
    pattern families silently stop matching and the loss is only visible
    post-hoc in an autoplay batch. This stage snapshots per-pattern hit
    counts against the full bulk and alarms when a family shrinks.

    Semantics note: counts are raw `re.search` over each card's lowercased
    oracle text (reminder text included) — an approximation of the runtime
    match paths, but a CONSISTENT one, which is all drift detection needs.

    Returns the number of alarms (0 = clean or baseline just created).
    """
    from rules.effect_templates import get_effect_library
    lib = get_effect_library()
    patterns = [p for p, _t in getattr(lib, "_pattern_templates", [])]
    if not patterns:
        print("[VALIDATOR] no pattern templates loaded — skipping drift check")
        return 0

    oracles = [
        (c.get("oracle_text") or "").lower()
        for c in bulk_cards
        # July 21 gotcha: token/art_series layouts shadow real cards.
        if c.get("layout") not in ("token", "art_series")
        and c.get("oracle_text")
    ]
    counts = {}
    for pat in patterns:
        try:
            rx = re.compile(pat)
        except re.error as e:
            print(f"[VALIDATOR] unparseable pattern {pat[:70]!r}: {e}")
            continue
        counts[pat] = sum(1 for o in oracles if rx.search(o))

    if update_baseline or not PATTERN_BASELINE.exists():
        PATTERN_BASELINE.write_text(
            json.dumps(counts, indent=1, sort_keys=True) + "\n",
            encoding="utf-8")
        print(f"[VALIDATOR] pattern-hit baseline "
              f"{'updated' if update_baseline else 'CREATED'} "
              f"({len(counts)} patterns) → {PATTERN_BASELINE.name}")
        return 0

    baseline = json.loads(PATTERN_BASELINE.read_text(encoding="utf-8"))
    alarms = []
    for pat, n in counts.items():
        b = baseline.get(pat)
        if b is None:
            continue  # new pattern — flagged below, recorded on next update
        drop = b - n
        if b > 0 and drop >= DRIFT_ABS_THRESHOLD and drop / b >= DRIFT_REL_THRESHOLD:
            alarms.append((pat, b, n))

    new_patterns = [p for p in counts if p not in baseline]
    stale_patterns = [p for p in baseline if p not in counts]
    if new_patterns:
        print(f"[VALIDATOR] note: {len(new_patterns)} pattern(s) not in the "
              f"baseline — run with --update-baseline to record them")
    if stale_patterns:
        print(f"[VALIDATOR] note: {len(stale_patterns)} baseline pattern(s) "
              f"no longer registered — --update-baseline prunes them")

    if alarms:
        print(f"[VALIDATOR] {len(alarms)} PATTERN-DRIFT alarm(s) — a family's "
              f"bulk hit count dropped (WotC retemplating, or a deliberate "
              f"pattern change that needs --update-baseline in this commit):")
        for pat, b, n in sorted(alarms, key=lambda a: a[1] - a[2], reverse=True):
            print(f"  - {b} → {n} hits: {pat[:100]!r}")
    else:
        print(f"[VALIDATOR] OK — {len(counts)} pattern families within drift "
              f"thresholds (rel {DRIFT_REL_THRESHOLD:.0%}, abs {DRIFT_ABS_THRESHOLD})")
    return len(alarms)


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
    ap.add_argument("--update-baseline", action="store_true",
                    help="rewrite data/pattern_hit_baseline.json from the "
                         "current bulk + pattern registry")
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
        polluted = find_keyword_pollution(cache, oracle_index)
        if polluted:
            failed = True
            print(f"[VALIDATOR] {len(polluted)} cache entr{'y' if len(polluted) == 1 else 'ies'} "
                  f"with PHANTOM keywords (runtime-grant pollution — no printing has them):")
            for name, extra in polluted[:30]:
                print(f"  - {name}: {extra}")
            print("Re-sync the entry's keywords from the bulk; find the grant "
                  "site writing through card.keywords instead of temp_keywords.")
        else:
            print("[VALIDATOR] OK — no phantom keywords in the cache")

    # Templating-drift stage (July 30, 2026): full runs only — it needs the
    # live pattern registry, which the names stage already imported.
    if not args.cache_only and not args.names_only:
        if check_pattern_drift(bulk_cards, update_baseline=args.update_baseline):
            failed = True

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
