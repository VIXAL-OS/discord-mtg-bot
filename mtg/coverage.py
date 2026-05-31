"""Tier-coverage diagnostic — classify how the engine will handle each card.

Used by deck loading + a `!coverage` Discord command to tell the user (or
contributor) what tier each card in a deck will be handled at, so they
know what to expect:

    template — fast, reliable, card-specific (Tier 1.5)
    pattern  — fast, reliable, oracle-text regex match (Tier 1.5)
    tier3    — slower, costs tokens, uses Claude API at runtime (Tier 3)
    unknown  — template library not loaded; classification unavailable

This is the "medium boundary" implementation from the Architectural
Readability for OSS section of CLAUDE.md (item #5). The engine accepts
any deck — there's no hard "supported cards only" gate — but at deck
load time the user gets a transparent report on what to expect for each
card.

Public functions:

    supported_at_tier(card_name, oracle_text) -> str
        One-card lookup. Pure query, no side effects.

    classify_deck(cards) -> dict
        Tier breakdown for a list of Card objects. Returns counts +
        per-tier card name lists.

    format_coverage_report(coverage, deck_name) -> str
        Discord-friendly summary string built from classify_deck() output.
"""

from typing import Dict, Iterable, List

try:
    from rules.effect_templates import get_effect_library
    HAS_TEMPLATES = True
except ImportError:
    HAS_TEMPLATES = False
    get_effect_library = None


def supported_at_tier(card_name: str, oracle_text: str = "") -> str:
    """Classify how the engine will handle a card's effects.

    Returns:
        "template" — exact card-name match in the template library
        "pattern"  — oracle text matches a regex pattern family
        "tier3"    — no template/pattern match; will use Claude API
        "unknown"  — template library not available (can't classify)

    See `EffectTemplateLibrary.tier_for_card` (in rules/effect_templates.py)
    for the actual classification logic. This wrapper just adds the
    "unknown" fallback for when rules/ isn't loaded.
    """
    if not HAS_TEMPLATES or get_effect_library is None:
        return "unknown"
    try:
        lib = get_effect_library()
        return lib.tier_for_card(card_name, oracle_text)
    except Exception:
        # Defensive: if the library throws, classify as unknown rather
        # than crashing whatever called us (deck load, !coverage command).
        return "unknown"


def classify_deck(cards: Iterable) -> Dict:
    """Tier breakdown for a deck.

    Args:
        cards: iterable of Card-like objects with .name and .oracle_text attrs.
            Duplicate names are counted multiple times (matches deck reality).

    Returns:
        dict with two keys:
            "counts": dict of {"template": N, "pattern": N, "tier3": N, "unknown": N}
            "by_tier": dict of {tier: [card_name, ...]} (preserves duplicates)
    """
    counts = {"template": 0, "pattern": 0, "tier3": 0, "unknown": 0}
    by_tier: Dict[str, List[str]] = {
        "template": [], "pattern": [], "tier3": [], "unknown": [],
    }
    for c in cards:
        name = getattr(c, "name", "") or ""
        oracle = getattr(c, "oracle_text", "") or ""
        tier = supported_at_tier(name, oracle)
        counts[tier] += 1
        by_tier[tier].append(name)
    return {"counts": counts, "by_tier": by_tier}


def format_coverage_report(coverage: Dict, deck_name: str = "deck",
                           show_tier3_names: bool = True,
                           max_names: int = 12) -> str:
    """Format classify_deck() output into a Discord-friendly summary.

    Args:
        coverage: output of classify_deck()
        deck_name: name to put in the header
        show_tier3_names: include the names of Tier 3 (Claude API) cards
            so the user can spot-check whether they're real triggerable
            cards or vanilla "tier3" non-issues
        max_names: cap the per-tier name list at N entries (rest are
            summarized as "...and M more")

    Returns:
        Multi-line string ready to paste into a Discord message. Empty
        string if the deck is empty.
    """
    counts = coverage["counts"]
    by_tier = coverage["by_tier"]
    total = sum(counts.values())
    if total == 0:
        return f"📊 {deck_name}: empty deck"

    lines = [f"📊 **{deck_name} tier coverage** ({total} cards):"]
    if counts["template"]:
        lines.append(f"   ✅ {counts['template']} via card-specific templates (Tier 1.5)")
    if counts["pattern"]:
        lines.append(f"   ✅ {counts['pattern']} via oracle-text patterns (Tier 1.5)")
    if counts["tier3"]:
        lines.append(
            f"   ⚠️ {counts['tier3']} will use Claude API fallback (Tier 3) "
            f"— slower (~2s), costs tokens"
        )
        if show_tier3_names:
            # Deduplicate while preserving first-occurrence order
            seen = set()
            unique = []
            for n in by_tier["tier3"]:
                if n.lower() not in seen:
                    seen.add(n.lower())
                    unique.append(n)
            shown = unique[:max_names]
            extra = len(unique) - len(shown)
            sample = ", ".join(shown)
            if extra > 0:
                sample += f", ...and {extra} more"
            lines.append(f"      Tier 3 cards: {sample}")
    if counts["unknown"]:
        lines.append(
            f"   ❓ {counts['unknown']} unknown (template library not loaded)"
        )
    lines.append(
        "   _Note: Tier 3 count includes vanilla creatures with no triggers_\n"
        "   _(they're fine — they'll never need resolution). Spot-check the_\n"
        "   _list above for cards with actual abilities you care about._"
    )
    return "\n".join(lines)
