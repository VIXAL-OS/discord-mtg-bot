"""Tier-coverage diagnostic — classify how the engine will handle each card.

Used by deck loading + a `!coverage` Discord command to tell the user (or
contributor) what tier each card in a deck will be handled at, so they
know what to expect:

    no_resolution  — nothing to resolve: vanilla / French-vanilla creature,
                     basic-ish land, pure mana rock. Works by definition.
    template       — fast, reliable, card-specific (Tier 1.5)
    pattern        — fast, reliable, oracle-text regex match (Tier 1.5)
    hardcoded      — fast, reliable, Tier 1 handler in mtg/spells.py or
                     mtg/triggers.py (curated list — see _TIER1_HARDCODED)
    spell_resolver — Tier 2 regex → JSON action; free and instant
    xmage          — Tier 2.5 XMage bridge knows the card (~10-50ms).
                     Only reported when a probe callable is supplied.
    tier3          — slower, costs tokens, uses Claude API at runtime
    unknown        — template library not loaded; classification unavailable

**Why this module is more than a `tier_for_card` passthrough (July 22, 2026).**
`EffectTemplateLibrary.tier_for_card` answers exactly one question — "does
the Tier 1.5 library handle this card?" — and returns "tier3" for everything
else. That is honest for the library but wrong as a *user-facing* forecast,
because the engine resolves cards at six other places the library can't see:
Tier 1 hardcodes, Tier 2 SpellResolver, Tier 2.5 XMage, and the mana / land /
equipment subsystems. Measured against the 3,000 most-played Commander cards,
the naive passthrough labelled ~57% of them "tier3" — so a new user running
`!coverage` on their own deck was told most of it would be slow and cost
tokens, which is badly misleading. This module layers the other subsystems on
top so the report reflects what will actually happen.

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

import re
from typing import Callable, Dict, Iterable, List, Optional

try:
    from rules.effect_templates import get_effect_library
    HAS_TEMPLATES = True
except ImportError:
    HAS_TEMPLATES = False
    get_effect_library = None

try:
    from rules.effects import parse_card_effects, EffectType
    HAS_SPELL_RESOLVER = True
except ImportError:
    HAS_SPELL_RESOLVER = False
    parse_card_effects = None
    EffectType = None

# Tier order, cheapest first. Used for the counts dict and report ordering.
TIERS = ("no_resolution", "template", "pattern", "hardcoded",
         "spell_resolver", "xmage", "tier3", "unknown")

# Tiers that cost nothing and resolve instantly — the "you're fine" group.
FREE_TIERS = ("no_resolution", "template", "pattern", "hardcoded", "spell_resolver")

# Keywords that need no resolution logic: combat//layers systems handle them.
_EVERGREEN = {
    'flying', 'first strike', 'double strike', 'deathtouch', 'haste', 'hexproof',
    'indestructible', 'lifelink', 'menace', 'reach', 'trample', 'vigilance',
    'defender', 'flash', 'ward', 'shroud', 'fear', 'intimidate', 'skulk',
    'horsemanship', 'protection', 'prowess',
}

# Tier 1 hardcoded handlers live as scattered `if "name" in card_name_lower`
# checks in mtg/spells.py (resolve_special_effects) and mtg/triggers.py
# (_check_creature_etb_triggers_sync / _check_cast_triggers), so they can't be
# enumerated programmatically. This list is CURATED from the "Tier 1" section
# of CLAUDE.md — it can drift if handlers are added without updating it, which
# only costs us a card reported one tier more expensive than reality.
_TIER1_HARDCODED = {
    'terror of the peaks', 'warstorm surge', 'impact tremors',
    'purphoros, god of the forge', 'soul of the harvest', 'beast whisperer',
    'panharmonicon', 'bojuka bog', 'radiant fountain', "ugin's labyrinth",
    'rhystic study', 'esper sentinel', 'mystic remora',
    'kambal, consul of allocation', 'blind obedience',
    'heartless hidetsugu', "painter's servant", 'summary dismissal',
}


def _needs_no_resolution(oracle_text: str, type_line: str = "") -> bool:
    """True when there is simply nothing for the engine to resolve.

    Vanilla and French-vanilla creatures, plain lands, and pure mana rocks
    all "work" without any template — reporting them as Tier 3 (as the raw
    library lookup does) overstates risk. Deliberately conservative: when in
    doubt this returns False and the card is classified by a later check.
    """
    o = re.sub(r'\([^)]*\)', '', oracle_text or '').strip()
    if not o:
        return True  # vanilla
    lines = [ln.strip() for ln in o.split('\n') if ln.strip()]
    if all(all(p.strip().lower() in _EVERGREEN for p in ln.split(','))
           for ln in lines):
        return True  # French vanilla — keywords only
    body = o.lower().replace('\n', ' ').strip()
    # Pure mana production ("{T}: Add {G}.", "Add {B}{B}{B}.")
    if re.fullmatch(r'(flying\s*)?(\{[^}]+\},?\s*)*(\{t\}:\s*)?add\b[^.]*\.?', body):
        return True
    # Lands whose only text is entering tapped / paying life to untap —
    # handled by the land-ETB system, not a template. A land with a REAL
    # ETB effect (Bojuka Bog exiling a graveyard) is excluded here.
    if 'land' in (type_line or '').lower():
        if re.search(r'enters tapped|as .* enters, you may pay', body) and not re.search(
                r'when(ever)?\b.*\b(exile|draw|create|search|destroy|gain|deal|return)', body):
            return True
    return False


def _spell_resolver_handles(oracle_text: str) -> bool:
    """True when Tier 2 (rules/effects.py regex parser) can fully parse the
    text — i.e. it yields at least one effect and none are COMPLEX.

    COMPLEX is the parser's own "I can't do this, escalate" marker, so this
    is the parser telling us directly rather than us second-guessing it.
    """
    if not (HAS_SPELL_RESOLVER and oracle_text):
        return False
    try:
        effects = parse_card_effects(oracle_text)
    except Exception:
        return False
    if not effects:
        return False
    return all(e.effect_type != EffectType.COMPLEX for e in effects)


def supported_at_tier(card_name: str, oracle_text: str = "",
                      type_line: str = "",
                      xmage_probe: Optional[Callable[[str], bool]] = None) -> str:
    """Classify how the engine will actually handle a card's effects.

    Checks the resolution subsystems in cost order, cheapest first, and
    returns the first that claims the card. See the module docstring for
    the full tier list and why this is more than a `tier_for_card` call.

    Args:
        card_name: exact card name (used for template + Tier 1 lookups)
        oracle_text: full oracle text (used for pattern + Tier 2 probes)
        type_line: optional; improves land / mana-rock detection
        xmage_probe: optional callable `(card_name) -> bool` answering "does
            the XMage bridge know this card?". Omitted by default because it
            costs a JSON-RPC round trip per card — fine for an explicit
            `!coverage` invocation, too slow for the per-game deck-load log.

    Returns one of the strings in TIERS.
    """
    if not HAS_TEMPLATES or get_effect_library is None:
        return "unknown"
    try:
        # 1. Nothing to resolve at all (vanilla, French vanilla, plain land).
        if _needs_no_resolution(oracle_text, type_line):
            return "no_resolution"
        # 2. Tier 1.5 — the template library's own verdict.
        lib = get_effect_library()
        tier = lib.tier_for_card(card_name, oracle_text)
        if tier in ("template", "pattern"):
            return tier
        # 3. Tier 1 — curated hardcoded handlers.
        if (card_name or "").lower().strip() in _TIER1_HARDCODED:
            return "hardcoded"
        # 4. Tier 2 — SpellResolver regex parser.
        if _spell_resolver_handles(oracle_text):
            return "spell_resolver"
        # 5. Tier 2.5 — XMage bridge, only when the caller supplies a probe.
        if xmage_probe is not None:
            try:
                if xmage_probe(card_name):
                    return "xmage"
            except Exception:
                pass  # bridge down / slow — fall through, don't crash a report
        return "tier3"
    except Exception:
        # Defensive: if a subsystem throws, classify as unknown rather
        # than crashing whatever called us (deck load, !coverage command).
        return "unknown"


def classify_deck(cards: Iterable,
                  xmage_probe: Optional[Callable[[str], bool]] = None) -> Dict:
    """Tier breakdown for a deck.

    Args:
        cards: iterable of Card-like objects with .name / .oracle_text (and
            optionally .type_line) attrs. Duplicate names are counted
            multiple times (matches deck reality).
        xmage_probe: optional; see supported_at_tier. Off by default so the
            per-game deck-load report stays fast.

    Returns:
        dict with two keys:
            "counts":  {tier: N} for every tier in TIERS
            "by_tier": {tier: [card_name, ...]} (preserves duplicates)
    """
    counts = {t: 0 for t in TIERS}
    by_tier: Dict[str, List[str]] = {t: [] for t in TIERS}
    for c in cards:
        name = getattr(c, "name", "") or ""
        oracle = getattr(c, "oracle_text", "") or ""
        type_line = getattr(c, "type_line", "") or ""
        tier = supported_at_tier(name, oracle, type_line, xmage_probe)
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

    free = sum(counts.get(t, 0) for t in FREE_TIERS)
    lines = [f"📊 **{deck_name} tier coverage** ({total} cards):"]
    if free:
        pct = round(100 * free / total)
        lines.append(f"   ✅ **{free} of {total} ({pct}%) resolve free and instantly:**")
    if counts["template"]:
        lines.append(f"      • {counts['template']} card-specific templates (Tier 1.5)")
    if counts["pattern"]:
        lines.append(f"      • {counts['pattern']} oracle-text patterns (Tier 1.5)")
    if counts.get("hardcoded"):
        lines.append(f"      • {counts['hardcoded']} hardcoded handlers (Tier 1)")
    if counts.get("spell_resolver"):
        lines.append(f"      • {counts['spell_resolver']} via SpellResolver regex (Tier 2)")
    if counts.get("no_resolution"):
        lines.append(
            f"      • {counts['no_resolution']} need no resolution at all "
            f"(vanilla creatures, lands, mana rocks)"
        )
    if counts.get("xmage"):
        lines.append(
            f"   ⚡ {counts['xmage']} via the XMage bridge (Tier 2.5) — free, ~10-50ms"
        )
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
    if counts["tier3"] and "xmage" not in (k for k, v in counts.items() if v):
        lines.append(
            "   _Tier 3 is a working fallback, not a failure — those cards_\n"
            "   _still resolve, just slower. Building the XMage bridge_\n"
            "   _(see README) moves many of them to Tier 2.5._"
        )
    return "\n".join(lines)
