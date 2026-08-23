"""Game data model: FormatValidator, Card, Player, StackEntry, GameState.

These are the domain dataclasses the rest of the engine operates on.
Together with the optional rules/ subsystems (mana cost parser, layers
engine, replacement effects) they form the state representation for a
Magic game.

Class roster:

    FormatValidator — static methods for deck-format legality checks
    Card            — a card instance in any zone (mutable dataclass)
    Player          — a player's zones, life, mana pool, etc.
    StackEntry      — one entry on the spell stack
    GameState       — top-level container (players + stack + phase + turn)

Extracted from mtg_game.py during the Phase 1 OSS-readability refactor.
Originally lived between the FORMAT VALIDATION and RULES ENGINE section
markers of the monolith (~lines 696-3393 before the Phase 1A extractions).
"""

import asyncio
import hashlib
import random
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from mtg.constants import (
    Phase, Zone,
    FORMAT_DECK_SIZE, SINGLETON_FORMATS, COMMAND_ZONE_FORMATS,
    BASIC_LAND_NAMES, BANNED_CARDS,
    _KEYWORD_LIST,
)

from mtg.helpers import (get_mdfc_info, colors_among_permanents,
                         is_vivid_mana_line)

# Optional: structured mana cost parser (rules/mana.py)
try:
    from rules.mana import (
        ManaCost, ManaPool as RulesManaPool, ManaPaymentValidator, ManaColor,
    )
    HAS_MANA_ENGINE = True
except ImportError:
    HAS_MANA_ENGINE = False

# Optional: 7-layer continuous effects (CR 613)
try:
    from rules.layers import (
        LayersEngine, ContinuousEffect, Layer, Sublayer, LayeredPermanent,
        create_anthem_effect, create_humility_effect,
        create_blood_moon_effect, create_color_change_effect,
    )
    HAS_LAYERS_ENGINE = True
except ImportError:
    HAS_LAYERS_ENGINE = False

# Optional: "if would, instead" replacement effects
try:
    from rules.replacement import ReplacementEngine, scan_oracle_for_replacements
    HAS_REPLACEMENT_ENGINE = True
except ImportError:
    HAS_REPLACEMENT_ENGINE = False


# =============================================================================
# FORMAT VALIDATION
# =============================================================================

class FormatValidator:
    """Validates decks against format rules."""
    
    # The five printed ways a card grants a SECOND commander (CR 702.124
    # partner, 702.125 friends forever, 702.140 doctor's companion, plus the
    # Background and Doctor/companion pairings). Matched against oracle text
    # because that is where every one of them is printed.
    _SECOND_COMMANDER_GRANTS = (
        "partner",                 # covers plain Partner AND "Partner with X"
        "friends forever",
        "doctor's companion",
        "choose a background",
    )

    @staticmethod
    def _commander_pair_issues(commanders) -> List[str]:
        """Issues with the SET of commanders (CR 903.3), not with the deck.

        A single commander is always fine. Two are legal only when a printed
        ability says so, and this checks the specific pairing rather than just
        "somebody said partner":

          * "Partner with <name>" pairs with THAT card and no other.
          * plain Partner pairs with any other plain-Partner card.
          * "Choose a Background" pairs with a Background, and the Background
            is the one that has to be a Background — not a second legend.

        Three or more is never legal.

        Deliberately reports rather than strips, matching how the banned-list
        and identity checks behave here (the July-20 call: swap the offending
        card, do not auto-mutate someone's deck).
        """
        issues: List[str] = []
        if len(commanders) <= 1:
            return issues
        if len(commanders) > 2:
            issues.append(
                f"{len(commanders)} commanders — a deck may have at most two, "
                f"and only when a printed ability grants the second (CR 903.3)")
            return issues

        first, second = commanders
        texts = [(getattr(c, 'oracle_text', '') or '').lower() for c in commanders]
        types = [(getattr(c, 'type_line', '') or '').lower() for c in commanders]

        # "Partner with <name>" — the named partner must be the other card.
        # Matched against the ORIGINAL text (case-insensitively) so the name
        # keeps its printed casing when it is quoted back to the player.
        for i, cmdr in enumerate(commanders):
            match = re.search(r'[Pp]artner with ([^\n(]+)',
                              getattr(cmdr, 'oracle_text', '') or '')
            if match:
                named = match.group(1).strip().rstrip('.').lower()
                other = (getattr(commanders[1 - i], 'name', '') or '').lower()
                if named and named != other:
                    issues.append(
                        f"**{commanders[i].name}** has \"Partner with "
                        f"{match.group(1).strip()}\" and cannot partner with "
                        f"**{commanders[1 - i].name}**")
                return issues

        # Choose a Background — the OTHER card must actually be a Background.
        for i, text in enumerate(texts):
            if "choose a background" in text:
                if "background" not in types[1 - i]:
                    issues.append(
                        f"**{commanders[i].name}** says \"Choose a "
                        f"Background\", but **{commanders[1 - i].name}** is "
                        f"not a Background")
                return issues

        # Otherwise both need a symmetric grant (Partner / Friends Forever /
        # Doctor's Companion).
        symmetric = ("partner", "friends forever", "doctor's companion")
        for i, text in enumerate(texts):
            if not any(g in text for g in symmetric):
                issues.append(
                    f"**{commanders[i].name}** has no ability allowing a "
                    f"second commander (CR 903.3)")
        return issues

    @staticmethod
    def get_color_identity(card) -> List[str]:
        """
        Extract color identity from a card's mana cost and oracle text.
        Color identity includes: mana cost colors + mana symbols in rules text.

        For MDFCs / transform cards / split cards, the mana cost on the
        front face alone misses the back face's pips (e.g. Jorn, God of
        Winter is mono-G on the front but Sultai counting Kaldring's
        {U}{B}). When the card already has a populated `color_identity`
        attribute (typically loaded from Scryfall), prefer that — it
        reflects the canonical card-pool view that includes both faces.
        """
        existing = getattr(card, 'color_identity', None)
        if existing:
            # Treat as authoritative if any colors are listed.
            return sorted([c for c in existing if c in 'WUBRG'])

        colors = set()

        # From mana cost
        if card.mana_cost:
            symbols = re.findall(r'\{([^}]+)\}', card.mana_cost)
            for sym in symbols:
                # Handle hybrid mana {W/U}
                for part in sym.split('/'):
                    part = part.replace('P', '')  # Phyrexian
                    if part in 'WUBRG':
                        colors.add(part)

        # From oracle text (mana symbols in abilities)
        if card.oracle_text:
            symbols = re.findall(r'\{([WUBRGC])\}', card.oracle_text)
            for sym in symbols:
                if sym in 'WUBRG':
                    colors.add(sym)

        # MDFC / transform back-face fallback: cards loaded before the deck
        # loader populated color_identity from Scryfall (e.g. older test
        # cards) won't have a populated color_identity attribute, but if
        # they have transform back-face metadata we should union those
        # mana symbols too. CR 903.4 — color identity for commander
        # legality is computed across BOTH faces.
        if getattr(card, 'has_transform', False):
            back_cost = getattr(card, 'back_face_mana_cost', '') or ''
            back_oracle = getattr(card, 'back_face_oracle_text', '') or ''
            for sym in re.findall(r'\{([^}]+)\}', back_cost + back_oracle):
                for part in sym.split('/'):
                    part = part.replace('P', '')
                    if part in 'WUBRG':
                        colors.add(part)

        return sorted(list(colors))
    
    @staticmethod
    def validate_deck(cards: List, format_name: str, commander=None,
                      companion=None) -> Tuple[bool, List[str]]:
        """
        Validate a deck against format rules.

        Args:
            cards: List of Card objects
            format_name: Format name (commander, modern, etc.)
            commander: Commander card, or list of commander cards (for
                Partner / Friends Forever / Background / etc. — color
                identity is computed as the union of all commanders).

        Returns:
            (is_valid, list_of_issues)
        """
        issues = []
        format_name = format_name.lower()
        # Normalize commander argument to a list so partner pairs work.
        if commander is None:
            commanders = []
        elif isinstance(commander, (list, tuple, set)):
            commanders = [c for c in commander if c is not None]
        else:
            commanders = [commander]

        # Oathbreaker has one planeswalker Oathbreaker and one signature
        # instant/sorcery in the command zone. The signature spell is not a
        # second commander, so it must not enter the CR 903.3 partner-pair
        # validator. Keep it in `cards`, however, so its color identity is
        # still checked against the Oathbreaker's identity below.
        if format_name == "oathbreaker":
            commanders = [
                c for c in commanders
                if not getattr(c, 'is_signature_spell', False)
            ]
        
        # Check deck size
        if format_name in FORMAT_DECK_SIZE:
            min_size, max_size = FORMAT_DECK_SIZE[format_name]
            deck_size = len(cards)
            
            if deck_size < min_size:
                issues.append(f"Deck has {deck_size} cards, minimum is {min_size}")
            if max_size and deck_size > max_size:
                issues.append(f"Deck has {deck_size} cards, maximum is {max_size}")
        
        # Check singleton rule
        if format_name in SINGLETON_FORMATS:
            card_counts = {}
            for card in cards:
                name = card.name.lower()
                if name not in BASIC_LAND_NAMES:
                    card_counts[name] = card_counts.get(name, 0) + 1
            
            duplicates = {name: count for name, count in card_counts.items() if count > 1}
            if duplicates:
                for name, count in duplicates.items():
                    issues.append(f"Singleton violation: {name.title()} appears {count} times")
        
        # Check banned cards
        if format_name in BANNED_CARDS:
            banned = BANNED_CARDS[format_name]
            for card in cards:
                if card.name.lower() in banned:
                    issues.append(f"**{card.name}** is banned in {format_name}")
        
        # Check color identity for commander.
        # CR 903.4: deck color identity is the union of every commander's
        # color identity (Partner, Friends Forever, Doctor's Companion,
        # Choose-a-Background — multi-commander mechanics).
        if format_name in COMMAND_ZONE_FORMATS and commanders:
            # CR 903.3: a deck has ONE commander unless a card explicitly
            # grants a second. Aug 3, 2026: the union below was applied to any
            # number of commanders without ever asking whether the PAIR is
            # legal, so two arbitrary legends were accepted and only their
            # combined identity was ever questioned. Permissive rather than
            # corrupting — it let an illegal deck through, it never broke a
            # legal one — but decks are user-uploaded, and 32 "Choose a
            # Background" commanders plus 31 Backgrounds are exactly the kind
            # of pair someone will hand in.
            issues.extend(FormatValidator._commander_pair_issues(commanders))
            commander_identity = set()
            for cmdr in commanders:
                commander_identity.update(FormatValidator.get_color_identity(cmdr))
            commander_names = {c.name for c in commanders}

            for card in cards:
                if card.name in commander_names:
                    continue
                card_identity = set(FormatValidator.get_color_identity(card))

                # Card's color identity must be subset of commander identity
                if not card_identity.issubset(commander_identity):
                    extra_colors = card_identity - commander_identity
                    issues.append(
                        f"**{card.name}** has colors {extra_colors} outside commander's identity {commander_identity or {'C'}}"
                    )

        # Cached Scryfall legality for constructed formats. Check each card
        # name once so four-of decks do not emit four identical issues. Cache
        # misses remain permissive because load-time population may lag.
        legality_fields = {
            "standard": "standard",
            "modern": "modern",
            "legacy": "legacy",
            "vintage": "vintage",
            "pioneer": "pioneer",
            "pauper": "pauper",
            "commander": "commander",
            "edh": "commander",
            # This project intentionally retains its 60-card Brawl contract;
            # Scryfall calls that rotating format standardbrawl.
            "brawl": "standardbrawl",
            "oathbreaker": "oathbreaker",
        }
        legality_field = legality_fields.get(format_name)
        if legality_field:
            try:
                import json as _json
                import os as _os
                cache_path = _os.path.join(
                    _os.path.dirname(_os.path.dirname(__file__)),
                    "data", "card_data_cache.json"
                )
                if _os.path.exists(cache_path):
                    with open(cache_path, 'r', encoding='utf-8') as f:
                        scry_cache = _json.load(f)
                else:
                    scry_cache = {}
            except Exception:
                scry_cache = {}

            legality_cards = list(cards)
            if companion is not None:
                legality_cards.append(companion)
            seen_legality_names = set()
            for card in legality_cards:
                card_key = card.name.lower()
                if (card_key in BASIC_LAND_NAMES
                        or card_key in seen_legality_names):
                    continue
                seen_legality_names.add(card_key)
                scry = scry_cache.get(card_key, {})
                legal = ((scry.get('legalities') or {})
                         .get(legality_field, ''))
                if legal in ('not_legal', 'banned'):
                    issues.append(
                        f"**{card.name}** is not {format_name}-legal ({legal})"
                    )

        # Companion restriction (CR 702.139). There was no companion check at
        # all — the July 27 fanout found data/test_companion_lurrus.json running
        # 4x Street Wraith (a MV-5 permanent card) under Lurrus of the
        # Dream-Den, whose whole clause is "each permanent card in your starting
        # deck has mana value 2 or less", and validate_deck reported the deck
        # LEGAL. A fixture that violates the mechanic it exists to test is worse
        # than no fixture, and nothing could have told us.
        if companion is not None:
            issues.extend(FormatValidator._companion_issues(cards, companion))

        return len(issues) == 0, issues

    @staticmethod
    def _companion_issues(cards: List, companion) -> List[str]:
        """Check a deck against its companion's deckbuilding restriction.

        Only the restrictions expressible as a simple per-card predicate are
        modelled; anything else is reported as unmodelled rather than silently
        passed, so a companion we can't check never looks verified.
        """
        text = (getattr(companion, 'oracle_text', '') or '').lower()
        name = getattr(companion, 'name', 'companion')
        clause = ''
        for line in text.split('\n'):
            if line.strip().startswith('companion'):
                clause = line
                break
        if not clause:
            return []

        issues = []
        mv_match = re.search(
            r'each permanent card in your starting deck has mana value (\d+) or less',
            clause)
        if mv_match:
            limit = int(mv_match.group(1))
            permanent_types = ('creature', 'artifact', 'enchantment', 'land',
                               'planeswalker', 'battle')
            for card in cards:
                type_line = (getattr(card, 'type_line', '') or '').lower()
                if not any(t in type_line for t in permanent_types):
                    continue
                mv = getattr(card, 'cmc', 0) or 0
                if mv > limit:
                    issues.append(
                        f"**{card.name}** (mana value {int(mv)}) violates "
                        f"{name}'s companion restriction (permanents must be "
                        f"mana value {limit} or less)")
            return issues

        print(f"[COMPANION] Restriction for {name} is not modelled — deck not "
              f"checked against it: {clause.strip()[:120]}")
        return issues
    
    @staticmethod
    def check_commander_legality(card) -> Tuple[bool, str]:
        """Check if a card can be a commander."""
        type_line = card.type_line.lower() if card.type_line else ""
        oracle_text = card.oracle_text.lower() if card.oracle_text else ""
        
        # Must be legendary creature or have "can be your commander"
        is_legendary_creature = "legendary" in type_line and "creature" in type_line
        is_legendary_planeswalker = "legendary" in type_line and "planeswalker" in type_line
        can_be_commander = "can be your commander" in oracle_text
        
        if is_legendary_creature or can_be_commander:
            return True, ""
        elif is_legendary_planeswalker:
            # Some planeswalkers can be commanders (check for explicit text)
            if can_be_commander:
                return True, ""
            else:
                return False, f"{card.name} is a planeswalker but doesn't have 'can be your commander'"
        else:
            return False, f"{card.name} is not a legendary creature"
    
    @staticmethod
    def format_validation_message(issues: List[str], format_name: str) -> str:
        """Format validation issues into a Discord message."""
        if not issues:
            return f"✅ Deck is valid for **{format_name.title()}**!"
        
        msg = f"⚠️ **Deck Validation Issues for {format_name.title()}:**\n"
        for issue in issues[:10]:  # Limit to first 10 issues
            msg += f"• {issue}\n"
        
        if len(issues) > 10:
            msg += f"\n*...and {len(issues) - 10} more issues*"
        
        return msg


# =============================================================================
# DATA CLASSES
# =============================================================================

_LANDWALK_RE = re.compile(
    r'\b(plains|island|swamp|mountain|forest|desert|snow)walk\b', re.I)


def _parse_landwalk_types(oracle: str) -> set:
    """Land subtypes granting landwalk evasion (CR 702.14).

    Aug 2, 2026 — landwalk had NO implementation. Street Wraith's swampwalk
    is the entire reason to block with it or not, and it was inert. Parsed
    from the keyword rather than the reminder text so printings that omit
    reminders still work. Returns e.g. {"swamp"}; empty when absent.
    """
    return {m.group(1).lower() for m in _LANDWALK_RE.finditer(oracle or '')}


def _restricts_combat(oracle: str, what: str) -> bool:
    """Does this oracle text forbid `what` ('attack' or 'block')?

    July 27, 2026 — the substring trap, again. The checks here used to be a
    bare `"can't block" in oracle`, and the STANDARD Magic phrasing is
    "can't attack or block": that string contains "can't attack" but NOT
    "can't block". So Pacifism, Arrest, Faith's Fetters and every other
    "can't attack or block" aura correctly stopped attacking and silently
    failed to stop blocking. In game_1530434723992834068 a Pacifism'd
    Watcher in the Mist blocked and killed an attacker on seven separate
    turns while the engine was rejecting it as an attacker every turn.

    Same family as `'creature' in 'noncreature'` (Woodfall Primus, July 24)
    and the Coldsteel Heart -> Painter's Servant name match (May 17):
    substring tests over natural-language oracle text need the full phrasing
    enumerated, not the convenient fragment.
    """
    if not oracle:
        return False
    other = 'block' if what == 'attack' else 'attack'
    # "can't attack or block" / "can't block or attack" cover both verbs.
    if f"can't {what} or {other}" in oracle or f"can't {other} or {what}" in oracle:
        return True
    return f"can't {what}" in oracle


# Aug 10 audit: a global, controller-side "creatures you control can't attack"
# static (Glacial Chasm). ANCHORED on the whole sentence rather than a
# substring, because the loose test is the exact inversion this codebase has
# now shipped seven times: an inventory sweep of all cached cards for
# "can't attack" returns four hits, and TWO of them — Ghostly Prison and
# Sphere of Safety ("creatures can't attack YOU unless their controller
# pays…") — are taxes on the OPPONENT. A substring check would blank their
# own controller's attacks instead. Port Razer's "can't attack a player it
# has already attacked" is the fourth and is likewise not this shape.
_CONTROLLER_ATTACK_LOCK = re.compile(
    r"creatures you control can't attack(?:\.|$)", re.IGNORECASE)


_CARD_KINDS = ('enchantment', 'artifact', 'creature', 'land',
               'planeswalker', 'instant', 'sorcery')

# CR 205.4a — the complete supertype list. Matched against the type line so
# the layers engine can evaluate supertype-qualified effects (Narfi's "Other
# snow and Zombie creatures you control"). Deliberately exhaustive-and-small:
# a supertype is a closed set, unlike subtypes.
_SUPERTYPES = ('basic', 'legendary', 'ongoing', 'snow', 'world', 'host')

# The UNQUALIFIED "creatures you control get +N/+N" anthem, for the inline
# compute-on-read fallback in _get_anthem_power/toughness_bonus.
#
# Aug 10 card-targeted wave (B): the pattern was unanchored, so ANY adjective
# in front was swallowed and the anthem broadcast to every creature —
# Full Moon's Rise ("Werewolf creatures you control get +1/+0") and Instigator
# Gang ("Attacking creatures you control get +1/+0") each buffed the whole
# board. Three creatures died at toughness 2 that should have survived at 3.
# The registration ladder in register_static_pt_effects had the identical
# hole; both are fixed, and they are mutually exclusive per creature (the
# inline path runs only when the layers engine has no P/T effect at all —
# get_effective_power's _has_layers_pt_effect gate).
#
# THE ANCHOR IS A FIXED-WIDTH NEGATIVE LOOKBEHIND, and the width is the whole
# point: it inspects the two characters before "creatures" and rejects a
# lowercase-or-hyphen followed by a space ("werewolf ", "attacking ",
# "non-human ", "other "). It still matches at string start, after "\n", and
# — decisively — after ", ": Beastmaster Ascension prints "...has seven or
# more quest counters on it, creatures you control get +5/+5" and registers
# CORRECTLY today, so a `^`/`\n`/`\.\s` anchor would silently kill it.
# (Python re rejects variable-width lookbehind, so this cannot be widened
# into an alternation without restructuring.)
_ANTHEM_INLINE_RE = (
    r'(?<![a-z-] )(?:other )?creature(?:\s+token)?s you control get \+(\d+)/\+(\d+)')

# The same anchor for the layers REGISTRATION ladder's unqualified clause.
# Module-scope so a pin can exercise the real object rather than re-expressing
# it (a mirrored predicate is a comment, not a test).
#
# Honest scope note, in the spirit of the July-26 CR 601.2f clamp: with the
# subtype branch above it generalized, this anchor is DEFENCE IN DEPTH rather
# than load-bearing for anything in the current inventory — every qualifier
# shape measured in the deck JSONs is now claimed by an earlier branch, so
# mutation-reverting this anchor alone changes no card's behaviour today. It
# stays because the ladder's ORDER is what makes that true, and a future
# reorder or a narrowed subtype branch would silently hand the qualified
# clauses back to own_all. Pinned directly rather than end-to-end for exactly
# that reason.
_ANTHEM_OWN_ALL_RE = r'(?<![a-z-] )creatures you control get \+(\d+)/\+(\d+)'

# "gets +N/+M for each <X> you control" on an Aura or Equipment.
#
# Aug 10 deferred (A3): the aura reader's kind alternation was a literal
# (?:enchantment|artifact|creature|land), and the equipment reader had no
# multiplier handling at ALL, so Glaive of the Guildpact granted a flat +1/+0
# regardless of Gate count. "Gate" is a land SUBTYPE, not one of those four
# kinds, so lifting the aura helper verbatim would have been a no-op — which
# is exactly why this was deferred rather than fixed inline.
#
# The trailing lookahead is load-bearing. Widening to arbitrary words newly
# reaches two inventory cards with UNMODELLED restrictive clauses:
#   Sage's Reverie      "for each Aura you control THAT'S ATTACHED to a creature"
#   Stoneforge Masterwork "for each other creature you control THAT SHARES a type"
# Counting those without the restriction is an OVER-count, which is worse
# than the flat bonus they get today. A restrictive "that / that's / which"
# tail therefore declines the multiplier entirely. Glaive's own tail is
# " and has vigilance and menace" — a separate ability, not a restriction —
# so it is unaffected.
_FOR_EACH_YOU_CONTROL = re.compile(
    r"(?:equipped|enchanted) creature gets ([+-]\d+)/([+-]\d+)"
    r"(?:\s+for each\s+([a-z][\w'-]*(?:\s+(?:and/or|and|or)\s+[a-z][\w'-]*)*)"
    r"\s+you control(?!\s+(?:that\b|that's|which\b)))?",
    re.IGNORECASE)


def _for_each_you_control_count(game, controller, kinds_text) -> int:
    """Count permanents `controller` controls matching a "for each X" phrase.

    A token that names a card KIND is matched against the whole type line; any
    other token is treated as a SUBTYPE and matched against the part after the
    em-dash, which is where Scryfall puts subtypes ("Land — Gate").
    """
    if controller is None or not kinds_text:
        return 0
    tokens = [t for t in re.split(r'\s+(?:and/or|and|or)\s+', kinds_text.lower())
              if t]
    if not tokens:
        return 0
    count = 0
    for permanent in getattr(controller, 'battlefield', []) or []:
        type_line = (getattr(permanent, 'type_line', '') or '').lower()
        # Accept both separators, as _card_to_targetable already does: real
        # Scryfall data uses the em dash, but hand-built cards and some older
        # fixtures use " - ", and a subtype check that silently sees nothing
        # is indistinguishable from "no Gates in play".
        if '—' in type_line:
            subtypes = type_line.split('—')[-1]
        elif ' - ' in type_line:
            subtypes = type_line.split(' - ')[-1]
        else:
            subtypes = ''
        for token in tokens:
            if token in _CARD_KINDS:
                if token in type_line:
                    count += 1
                    break
            elif token in subtypes:
                count += 1
                break
    return count


def _controller_forbids_attacking(game, creature) -> bool:
    """True when a permanent the creature's controller controls prints a
    blanket "creatures you control can't attack."."""
    try:
        for player in getattr(game, 'players', []) or []:
            if creature not in player.battlefield:
                continue
            for permanent in player.battlefield:
                oracle = (getattr(permanent, 'oracle_text', '') or '')
                if _CONTROLLER_ATTACK_LOCK.search(oracle):
                    return True
            return False
    except (AttributeError, TypeError):
        return False
    return False


@dataclass
class Card:
    """Represents a card in any zone."""
    name: str
    id: str = ""  # Unique instance ID
    # -1 means "ownership unknown", NOT "player 0". Deck load stamps every card
    # (mtg/engine.py) and the token paths stamp their tokens, but a Card built
    # ad hoc at runtime has no owner — and defaulting those to 0 would let an
    # unstamped permanent on player 1's battlefield be treated as player 0's,
    # i.e. a brand-new way for cards to change hands. Consumers must treat
    # unknown as "owned by whoever controls it" (see helpers.owner_of /
    # helpers.owns_card), which is exactly the pre-July-28 behaviour.
    owner_index: int = -1
    
    # Card data (loaded from Scryfall or deck)
    mana_cost: str = ""
    cmc: int = 0  # Converted mana cost
    type_line: str = ""
    oracle_text: str = ""
    power: Optional[str] = None
    toughness: Optional[str] = None
    loyalty: Optional[int] = None
    keywords: List[str] = field(default_factory=list)  # Flying, Trample, etc.
    
    # State (when on battlefield)
    tapped: bool = False
    summoning_sick: bool = True  # Can't attack/tap for abilities until controller's next turn
    entered_this_turn: bool = False  # For "when ~ enters" triggers
    counters: Dict[str, int] = field(default_factory=dict)
    attached_to: Optional[str] = None  # ID of permanent this is attached to
    attachments: List[str] = field(default_factory=list)  # IDs of attached cards
    
    # Combat state
    attacking: bool = False
    attacking_player: Optional[int] = None
    # CR 508.1a: an attack is declared against a player OR a planeswalker that
    # player controls. The controller remains `attacking_player` (blocking,
    # attack taxes and defender grouping all key off the SEAT), so this only
    # names the planeswalker. Held as a card id, and read through
    # GameState.attacked_planeswalker_for(), which re-resolves it live.
    attacking_planeswalker: Optional[str] = None
    blocking: List[str] = field(default_factory=list)  # IDs of creatures being blocked
    blocked_by: List[str] = field(default_factory=list)  # IDs of blocking creatures
    damage_marked: int = 0
    deathtouch_damage: int = 0  # Damage from sources with deathtouch (CR 704.5h)

    # ---- Transient runtime state (reset/derived during play; NOT serialized;
    # to_dict is hand-written and skips these) ----
    # Convention: new runtime flags become DECLARED fields with defaults here,
    # not ad-hoc `card._x = ...` staples — tests/test_ratchets.py counts
    # undeclared-staple sites and fails CI if the count grows. compare=False
    # keeps equality semantics identical to pre-declaration behavior (staples
    # never participated in ==, and `card in zone` / `.remove(card)` rely on it).
    # Set by Tier 1/1.5/3 handlers when a cast's effect has resolved; cleared at
    # the start of every cast (Lingering Souls re-cast bug, May 13 audit).
    _spell_resolved: bool = field(default=False, repr=False, compare=False)
    # Phasing (Teferi's Protection): phased-out permanents are skipped by
    # combat / targeting / SBA sweeps (CR 702.26).
    _phased_out: bool = field(default=False, repr=False, compare=False)
    # June 10 audit (C8): set on a reanimation aura (Animate Dead) when the
    # inline cast-time handler already performed the reanimation, so the
    # name-keyed ETB template doesn't run a SECOND one (one cast was
    # returning two creatures).
    _reanimate_handled: bool = field(default=False, repr=False, compare=False)
    # Cast/linked-effect bookkeeping used while the card is on the stack or
    # exiled by a specific source.
    _cast_origin: str = field(default="", repr=False, compare=False)
    # Chosen X for the current cast. None means no caller-selected value;
    # the cast pipeline stamps the paid/defaulted value before resolution.
    _x_value: Optional[int] = field(default=None, repr=False, compare=False)
    # Decimate's four targets are chosen during casting and must not be
    # replaced by freshly selected permanents during resolution.
    _decimate_target_ids: Dict[str, str] = field(
        default_factory=dict, repr=False, compare=False)
    # Command-zone cards are temporarily moved to hand before the shared cast
    # pipeline runs. Preserve the real origin through that move. Executors
    # clear this per-cast stamp after success or failure.
    _cast_from_command_zone: bool = field(
        default=False, repr=False, compare=False)
    # Escape (CR 702.139). `_escape_cost` is the alternative cost the payment
    # Exact card requested while a tutor spell is resolving. Executors set
    # this transient bridge from the AI action and clear it after the cast.
    _tutor_card: Optional[str] = field(
        default=None, repr=False, compare=False)
    # Aug 7 queue item Q2a: destination-typed tutor choices for
    # split-destination tutors (Jarad's Orders — one search to hand, one to
    # graveyard). Each search_library action consumes the key matching its
    # own to_zone; the generic _tutor_card stays as the consumed-once
    # fallback. Same executor-set/cleared lifecycle as _tutor_card.
    _tutor_to_hand: Optional[str] = field(
        default=None, repr=False, compare=False)
    _tutor_to_graveyard: Optional[str] = field(
        default=None, repr=False, compare=False)
    # Multi-card tutor choice, consumed by the real search/hand handlers.
    _tutor_cards: List[str] = field(default_factory=list, repr=False,
                                    compare=False)
    # Aug 7 queue item Q3 (Draugr Necromancer's cast half): the death
    # redirect stamps WHO may cast this exiled card (Draugr's controller)
    # and that snow mana may be spent as any color to do it (the printed
    # permission). Valid while the card sits in exile and a Draugr remains
    # on the stamped player's battlefield — is_castable_from_exile
    # re-checks at offer AND cast time. Cleared when the cast succeeds.
    _castable_by_player: Optional[str] = field(
        default=None, repr=False, compare=False)
    _snow_as_any_color: bool = field(default=False, repr=False, compare=False)
    # Q3 adversarial review #10: WHICH Draugr granted the permission — the
    # predicate requires that exact object on the battlefield (CR 607
    # linked ability), so a replacement Draugr can't revive it.
    _draugr_source_id: Optional[str] = field(
        default=None, repr=False, compare=False)
    # stage should charge; `_was_escaped` is what Kroxa's "sacrifice it unless
    # it escaped" reads. Declared rather than stapled specifically because the
    # BUG here was a read with no writer anywhere — a flag nobody can find is a
    # flag nobody sets.
    _escape_cost: str = field(default="", repr=False, compare=False)
    _was_escaped: bool = field(default=False, repr=False, compare=False)
    # Madness (CR 702.35, Aug 1 2026). `_madness_cost` is stamped by the
    # discard redirect (helpers.madness_discard_to_exile) so the drain and
    # the cast pipeline share one parse; `_cast_via_madness` marks the cast
    # in flight so can_cast_spell checks the madness cost + waives timing
    # (CR 702.35a — the cast happens as the trigger resolves) and
    # _compute_alt_costs charges it. Cleared by the drain either way.
    _madness_cost: str = field(default="", repr=False, compare=False)
    _cast_via_madness: bool = field(default=False, repr=False, compare=False)
    # Spectacle (CR 702.137, Aug 1 2026): set by _compute_alt_costs when the
    # spectacle cost is taken (condition met + payable), read by effect
    # resolution ("If this spell's spectacle cost was paid..."). Reset at
    # the start of every cast alongside _spell_resolved.
    _was_spectacled: bool = field(default=False, repr=False, compare=False)
    # Kicker (CR 702.33, Aug 1 2026): set by _compute_alt_costs when the
    # kicker cost is paid (v1 gate: kick whenever affordable — the printed
    # kicked mode is the designed-better mode). Replaces the Gatekeeper
    # mana-paid>=N guess as the source of truth for ctx['kicked']. Reset at
    # the start of every cast alongside _was_spectacled; free casts and
    # madness casts never kick (documented at the cost site).
    _kicked: bool = field(default=False, repr=False, compare=False)
    # Aug 2 2026 (batch-13): True when the printed Entwine cost was actually
    # paid this cast (CR 702.42 — the kicker pattern's twin). Templates read
    # it as ctx['entwined']; without it Tooth and Nail resolved both modes
    # off the base cost. Reset alongside _kicked at every cast start.
    _entwined: bool = field(default=False, repr=False, compare=False)
    # Aug 2 2026: how many times the printed Multikicker cost was paid this
    # cast (CR 702.33c). Registry-gated auto-kick (helpers.MULTIKICKER_
    # MODELED); templates read it as ctx['kicked_times']. Reset per cast.
    _kicked_times: int = field(default=0, repr=False, compare=False)
    # Aug 10 audit: set when the cast funnel registers this permanent's
    # statics/replacements BEFORE emitting PERMANENT_ENTERED, so the
    # legacy registration further down the same function does not run a
    # second time — ReplacementEngine.add_effect has no dedup, and a
    # double registration quadruples a damage doubler. Cleared on every
    # battlefield exit so a flicker or recast registers afresh.
    _statics_registered_on_entry: bool = field(default=False, repr=False, compare=False)
    # Aug 3 2026: the cards spliced onto THIS spell for this cast (CR 702.46).
    # Unlike its four siblings above, which are flags read off the card being
    # cast, this holds live Card references to cards that stay in the caster's
    # HAND (CR 702.46c) — so it is declared for the same reason the others are
    # (visible to to_dict / !undo carry-over, and to the next reader) and
    # additionally so the countered/fizzled paths, which return before the
    # post-resolution clear, do not leave an undeclared hard reference from a
    # graveyard card to a hand card. Reset per cast alongside _kicked.
    _spliced_cards: list = field(default_factory=list, repr=False, compare=False)
    # Animate-land duration (Aug 1 2026): player index whose NEXT turn ends
    # the animation ("Until your next turn, all lands you control become
    # 2/2..." — Sylvan Awakening). _animated_until_eot stays SET alongside
    # it so every effective-P/T / SBA-promotion read keeps working; the
    # end-step revert skips cards carrying this, and end_turn's
    # turn-advance point reverts them when their controller's turn arrives.
    _animated_expires_at_turn_of: Optional[int] = field(
        default=None, repr=False, compare=False)
    # Graveyard-origin cast marker (flashback/escape). Both AI cast paths
    # stamp it so templates (Increasing Devotion, Snapcaster tracking) can
    # detect graveyard-origin casting; declared July 29 when the autoplay
    # path gained the same stamp.
    _cast_from_graveyard: bool = field(default=False, repr=False, compare=False)
    # Aug 7 2026 batch audit (C-1): per-turn declared-attacker count, read by
    # Moraug, Fury of Akoum's "+1/+0 for each time it has attacked this turn"
    # static (compute-on-read in get_effective_power). Incremented at the six
    # DECLARED-attacker sites only — a token put onto the battlefield attacking
    # was never declared and has not "attacked" (Moraug Gatherer ruling
    # 2020-09-25), so the create-token-attacking path deliberately skips it.
    # Cleared by end_turn's combat sweep.
    attacks_this_turn: int = field(default=0, repr=False, compare=False)
    # Adventure (CR 715.3): set when the adventure half resolves and the card
    # goes to exile, cleared when it leaves. Deliberately separate from
    # Player.playable_from_exile, which end_turn wipes every turn — adventure
    # castability persists for as long as the card stays exiled.
    _adventure_exiled: bool = field(default=False, repr=False, compare=False)
    # Aug 3 2026 — the alternate-cost / graveyard-casting wave.
    #
    # ONE routing marker for "this spell is exiled as it resolves instead of
    # going to the graveyard", carrying the printed reason for the display
    # line: flashback (CR 702.34a), jump-start (CR 702.132a), aftermath
    # (CR 702.127a). Three near-identical branches in the resolution zone
    # routing would have drifted; the flashback branch's old private flag was
    # folded into this one.
    _exile_after_resolution: str = field(default="", repr=False, compare=False)
    # Buyback (CR 702.26): the optional additional cost was PAID, so the
    # spell returns to its owner's hand as it resolves. Reset per cast
    # alongside _kicked / _entwined.
    _buyback_paid: bool = field(default=False, repr=False, compare=False)
    # Foretell (CR 702.143). `_foretold` is the persistent exile marker
    # (twin of _adventure_exiled — foretold cards stay castable for the rest
    # of the game, so Player.playable_from_exile, which end_turn expires,
    # is the wrong home); `_foretell_cost` is the alternative cost the
    # payment stage charges; `_foretold_turn` enforces CR 702.143b, which
    # forbids casting it the turn it was foretold.
    _foretold: bool = field(default=False, repr=False, compare=False)
    _foretell_cost: str = field(default="", repr=False, compare=False)
    _foretold_turn: Optional[int] = field(default=None, repr=False, compare=False)
    _cast_via_foretell: bool = field(default=False, repr=False, compare=False)
    # Unearth (CR 702.83): the permanent came back from the graveyard and is
    # exiled at the next end step or if it would leave the battlefield.
    _unearthed: bool = field(default=False, repr=False, compare=False)
    # Miracle (CR 702.94). Stamped when the card is drawn as the first card
    # of the turn and its owner may cast it for the miracle cost; the cast
    # pipeline reads _cast_via_miracle exactly as it reads _cast_via_madness.
    _miracle_cost: str = field(default="", repr=False, compare=False)
    _cast_via_miracle: bool = field(default=False, repr=False, compare=False)
    # A resolving effect grants permission to cast this object now and
    # without paying its mana cost. Ordinary timing/mana checks are waived,
    # but restrictions such as Teferi, Time Raveler still apply.
    _cast_via_effect: bool = field(default=False, repr=False, compare=False)
    _free_cast_source: str = field(default="", repr=False, compare=False)
    # Copies of spells are stack objects but are not cards. Instant/sorcery
    # copies cease after resolution; permanent-spell copies resolve as tokens.
    _is_spell_copy: bool = field(default=False, repr=False, compare=False)
    # Developer-only autoplay exercise hook. This flag is set after mulligans
    # on a card deliberately placed into an opening hand. It exists only on
    # the in-memory Card instance and is never serialized back into a deck or
    # the shared Scryfall cache.
    _autoplay_seeded: bool = field(default=False, repr=False, compare=False)
    # Converge (CR 702.100a): the distinct COLORS of mana actually spent
    # casting this spell, recorded by the mana engine at payment time. The
    # engine already resolves each tapped source to one committed color, so
    # this is that set — not the colors the cost merely asked for.
    _colors_spent: tuple = field(default=(), repr=False, compare=False)
    _snow_mana_spent: int = field(default=0, repr=False, compare=False)
    _declared_graveyard_target_id: Optional[str] = field(default=None, repr=False, compare=False)
    _declared_graveyard_target_owner: str = field(default="", repr=False, compare=False)
    _imprinted_card_id: Optional[str] = field(default=None, repr=False, compare=False)
    _imprinted_card_name: str = field(default="", repr=False, compare=False)
    _imprinted_owner_index: Optional[int] = field(default=None, repr=False, compare=False)
    # July 30: the card sits face down in a hidden-info zone (Necropotence /
    # Gonti exile). Set/cleared by move_card; GameState.visible_state masks
    # face-down cards for every viewer. Display-level hiding alone
    # (hide_card_name at emit sites) leaks through full-state serialization.
    _face_down: bool = field(default=False, repr=False, compare=False)
    # Copy-effect bookkeeping. The snapshot restores printed characteristics
    # when the permanent changes zones (CR 707.8); the other fields describe
    # the battlefield-only copy state.
    _pre_copy_snapshot: Any = field(default=None, repr=False, compare=False)
    _manifest_original: Any = field(default=None, repr=False, compare=False)
    _manifested: bool = field(default=False, repr=False, compare=False)
    _is_copy: bool = field(default=False, repr=False, compare=False)
    _copy_of: Optional[str] = field(default=None, repr=False, compare=False)
    _original_name: str = field(default="", repr=False, compare=False)
    _chosen_creature_type: str = field(default="", repr=False, compare=False)

    # Temporary modifiers (from pump effects, etc.)
    power_modifier: int = 0
    toughness_modifier: int = 0
    temp_keywords: List[str] = field(default_factory=list)  # Keywords granted until end of turn
    # Aug 7 batch audit (G2-1): "lose <keyword> until end of turn"
    # (Shadowspear: "Permanents your opponents control lose hexproof and
    # indestructible until end of turn") had NO action vocabulary — every
    # activation was a silent no-op. A removal here beats every grant source
    # for the turn (approximation of the Layer-6 later-timestamp ordering a
    # "lose" effect nearly always has). Cleared by clear_end_of_turn_effects.
    temp_removed_keywords: List[str] = field(default_factory=list)
    # Until-end-of-turn blocking restriction (Goblin Shortcutter, Chandra,
    # Pyromaster).  Declared state keeps save/undo truthful in the middle of a
    # turn; clear_end_of_turn_effects and zone changes remove it.
    cant_block_this_turn: bool = False
    
    # Planeswalker loyalty counters
    loyalty_counters: int = 0
    
    # MDFC (Modal Double-Faced Card) tracking
    played_face: str = ""  # "front" or "back" - which face this card entered as
    mdfc_back_name: str = ""  # Name of the back face if this is an MDFC

    # Transform / Double-Faced Card tracking
    has_transform: bool = False  # True if this card can transform (layout: "transform")
    is_transformed: bool = False  # True if currently showing back face
    back_face_name: str = ""
    back_face_type_line: str = ""
    back_face_oracle_text: str = ""
    back_face_power: str = ""
    back_face_toughness: str = ""
    back_face_mana_cost: str = ""
    front_face_name: str = ""  # Stored so we can always find the original front face name

    # Adventure tracking
    adventure_name: str = ""  # Name of the adventure half (e.g., "Fertile Footsteps")
    adventure_cost: str = ""  # Mana cost of the adventure
    adventure_text: str = ""  # Oracle text of the adventure effect
    adventure_type: str = ""  # Type line of adventure (e.g., "Sorcery — Adventure")
    cast_as_adventure: bool = False  # Whether this is being cast as the adventure half

    # Split card support (Commit // Memory, Wear // Tear, etc.)
    split_names: List[str] = field(default_factory=list)  # ["Commit", "Memory"]
    split_costs: List[str] = field(default_factory=list)  # ["{3}{U}", "{4}{U}{U}"]
    split_types: List[str] = field(default_factory=list)  # ["Instant", "Sorcery"]
    split_texts: List[str] = field(default_factory=list)  # [oracle for each half]
    cast_as_split_half: int = -1  # which half is being cast (-1 = not a split cast)
    
    # Commander tracking
    is_commander: bool = False  # Is this card a commander?
    is_signature_spell: bool = False  # Is this card an oathbreaker signature spell?
    is_companion: bool = False  # Is this card a companion?
    color_identity: List[str] = field(default_factory=list)  # Color identity (W/U/B/R/G)
    times_cast_from_command_zone: int = 0  # For commander tax calculation

    # Control-change tracking (for Agent of Treachery, Act of Treason, etc.)
    original_controller_index: Optional[int] = None  # Player index before steal
    control_gained_by: Optional[str] = None  # Card name that stole this (for LTB return)

    # Mutate tracking
    mutated_cards: List['Card'] = field(default_factory=list)  # Cards merged via mutate
    mutated_under: bool = False  # True if this card was placed under via mutate

    # Suspend tracking (for cards in exile)
    suspended: bool = False  # Is this card suspended?
    
    def __post_init__(self):
        if not self.id:
            self.id = f"{self.name}_{random.randint(10000, 99999)}"
        # Ensure cmc is always an int (Scryfall data or deck loading may pass strings)
        if isinstance(self.cmc, str):
            try:
                self.cmc = int(float(self.cmc))
            except (ValueError, TypeError):
                self.cmc = 0
        elif not isinstance(self.cmc, int):
            try:
                self.cmc = int(self.cmc)
            except (ValueError, TypeError):
                self.cmc = 0
        # Ensure card.name is always a string (some JSON data may pass lists)
        if isinstance(self.name, list):
            self.name = self.name[0] if self.name else "Unknown"
        # Parse CMC from mana cost if not set
        if self.mana_cost and not self.cmc:
            self.cmc = self._parse_cmc(self.mana_cost)
        # Parse keywords from oracle text
        if self.oracle_text and not self.keywords:
            self.keywords = self._parse_keywords(self.oracle_text)
    
    def reset_battlefield_state(self):
        """Reset all battlefield-specific state when a card changes zones.

        Per MTG rules (400.7), when an object changes zones it becomes a new object
        with no memory of its previous state. This resets combat state, damage,
        temporary modifiers, and other battlefield-only attributes so that a card
        re-entering the battlefield (e.g. a commander recast from command zone)
        doesn't carry stale state that would cause incorrect SBA deaths.
        """
        self.damage_marked = 0
        self.deathtouch_damage = 0
        self.power_modifier = 0
        self.toughness_modifier = 0
        self.temp_keywords = []
        self.temp_removed_keywords = []
        self.cant_block_this_turn = False
        self.tapped = False
        self.attacking = False
        self.attacking_player = None
        self.attacking_planeswalker = None
        self.blocking = []
        self.blocked_by = []
        # C-1 (Aug 7): the per-turn attack counter is battlefield history the
        # new object must not remember (CR 400.7) — a same-turn reanimation
        # would otherwise keep its pre-death Moraug bonus.
        self.attacks_this_turn = 0
        self.summoning_sick = True
        self.entered_this_turn = False
        self.attached_to = None
        self.attachments = []
        # June 10 deep-dive (B4): clear counters and the reanimation binding.
        # A commander that died carrying a +1/+1 counter kept it through the
        # command zone and attacked over-statted after the re-cast (Sythis
        # logged 6/21 commander damage instead of 3/21); worse, Glissa kept
        # `_reanimated_by_aura_id` from an earlier Animate Dead, so the SBA
        # sweep INSTANTLY sacrificed her brand-new re-cast ("binding aura
        # gone") with no player-visible explanation. A new object has none
        # of its old state (CR 400.7).
        self.counters = {}
        self._statics_registered_on_entry = False
        self._reanimated_by_aura_id = None
        # Linked exile belongs to this specific battlefield object. A Scepter
        # that leaves and later returns is a new object with no imprint link.
        self._imprinted_card_id = None
        self._imprinted_card_name = ""
        self._imprinted_owner_index = None
        # Reset transform state — cards re-enter on their front face (CR 712.10a)
        if self.is_transformed and self.has_transform:
            self.transform()

    def transform(self) -> bool:
        """Transform this double-faced card, swapping front and back face data.

        Returns True if the card was transformed, False if it can't transform.
        Per MTG rules (CR 712), transforming swaps all printed characteristics
        of the two faces. Counters, damage, and attachments remain.
        """
        if not self.has_transform:
            return False
        # Swap name
        self.name, self.back_face_name = self.back_face_name, self.name
        # Swap type line
        self.type_line, self.back_face_type_line = self.back_face_type_line, self.type_line
        # Swap oracle text
        self.oracle_text, self.back_face_oracle_text = self.back_face_oracle_text, self.oracle_text
        # Swap power/toughness (as strings — may be None or "*")
        self.power, self.back_face_power = self.back_face_power, self.power
        self.toughness, self.back_face_toughness = self.back_face_toughness, self.toughness
        # Swap mana cost (back face of transform cards often has no mana cost)
        self.mana_cost, self.back_face_mana_cost = self.back_face_mana_cost, self.mana_cost
        # Toggle transformed state
        self.is_transformed = not self.is_transformed
        # Re-parse keywords from new oracle text
        if self.oracle_text:
            self.keywords = self._parse_keywords(self.oracle_text)
        else:
            self.keywords = []
        print(f"[TRANSFORM] {self.back_face_name} transforms into {self.name}")
        return True

    def _parse_cmc(self, mana_cost: str) -> int:
        """Parse converted mana cost from mana cost string like '{2}{U}{U}'.

        Uses rules/mana.py ManaCost.parse() for accurate CMC calculation
        (handles hybrid generic like {2/W} = CMC 2 correctly).  Falls back
        to inline regex if the engine isn't available or parsing fails.
        """
        if not mana_cost:
            return 0
        # July 31 batch-10 reviewer: an ADVENTURE card's Scryfall mana_cost is
        # the combined "{creature} // {adventure}" string, and parsing both
        # halves priced Oakhame Ranger at CMC 8 — [PLAN-VALIDATE] rejected it
        # as unaffordable every turn of game_1532409540866212023 despite 4
        # real mana. An adventure card's mana value is the CREATURE face's
        # everywhere (the Adventure's characteristics exist only on the
        # stack). SPLIT cards deliberately keep the combined parse — CR
        # 708.4a: off the stack their halves are combined, so MV = sum.
        # (MDFC combined strings would want the front face too; unobserved,
        # left alone.)
        if ' // ' in mana_cost and 'adventure' in (self.type_line or '').lower():
            mana_cost = mana_cost.split(' // ')[0]
        # Structured parser — handles hybrid generic ({2/W} = CMC 2) correctly
        if HAS_MANA_ENGINE:
            try:
                return ManaCost.parse(mana_cost).cmc
            except Exception:
                pass  # Fall through to inline
        # Inline fallback
        cmc = 0
        symbols = re.findall(r'\{([^}]+)\}', mana_cost)
        for sym in symbols:
            if sym.isdigit():
                cmc += int(sym)
            elif sym in ['W', 'U', 'B', 'R', 'G']:
                cmc += 1
            elif '/' in sym:  # Hybrid mana like {W/U}
                cmc += 1
            elif sym == 'X':
                pass  # X doesn't add to CMC when not on stack
            else:
                cmc += 1  # Phyrexian, snow, etc.
        return cmc

    def _parse_keywords(self, oracle_text: str) -> List[str]:
        """Extract keyword abilities from oracle text.

        Derives the keyword list from the Keyword enum (rules/keywords.py)
        so there's a single source of truth for recognized keywords.

        May 13 audit: the previous naive `if kw.lower() in text_lower` matched
        anywhere in oracle text, producing phantom keywords any time the card
        MENTIONED a keyword without having it. Craterhoof Behemoth (creatures
        you control gain trample and haste) got Trample+Haste attributed to
        itself; Bogardan Hellkite (Flash, Flying + nothing else) somehow got
        Haste because the parser saw it in some interaction; Curious Pair
        (adventure) got Flying from the Food token's reminder text. ~21 cards
        in the May 11 cache had phantom keywords.

        MTG keyword abilities appear in oracle text as a STANDALONE keyword
        line (possibly comma-separated) at the start of the text, e.g.:
          "Flying"
          "Flying, vigilance"
          "Trample\nFirst strike"
          "Ward {2}"
          "Annihilator 4"
          "Protection from black"

        They do NOT appear as the keyword inside larger sentences:
          "Whenever ~ enters, creatures gain haste."  (grants haste, doesn't HAVE haste)
          "Counter target creature spell with flying."  (mentions flying)
          "Each creature with reach blocks..."           (mentions reach)

        This implementation: strip reminder text, split on newlines, and only
        accept a paragraph as a keyword line if it's a comma-separated list
        of bare keyword tokens (each optionally followed by a numeric arg or
        a "{mana}" payment, e.g. Ward {2} / Annihilator 4 / Protection from X).
        """
        if not oracle_text:
            return []
        # Strip reminder text. Anything in parens is flavor-explanatory and
        # not a separate ability paragraph.
        text = re.sub(r'\s*\([^)]*\)', '', oracle_text)
        # Lowercase keyword vocabulary for quick lookup; preserve canonical
        # spelling for the output.
        canonical = {kw.lower(): kw for kw in _KEYWORD_LIST}
        # Match: bare keyword, "keyword N", "keyword {cost}", "keyword from X",
        # or "keyword X — Y". Each comma-separated piece must be a keyword.
        piece_re = re.compile(
            r'^([a-z][a-z\s\']*?)'                                # keyword name
            r'(?:\s+(?:\d+|\{[^}]+\}|from\s+[a-z]+(?:\s+and\s+[a-z]+)?))?'  # optional arg
            r'$',
            re.IGNORECASE,
        )

        found = []
        for raw_paragraph in text.split('\n'):
            paragraph = raw_paragraph.strip().rstrip('.').strip()
            if not paragraph:
                continue
            # Don't accept paragraphs with verbs that indicate the keyword is
            # being GRANTED or REFERENCED, not POSSESSED.
            if re.search(r'\b(gain|gains|have|has|with|target|each|whenever|when|at)\b',
                         paragraph, re.IGNORECASE):
                continue
            # Split on commas. Each piece must be a bare keyword-shaped token
            # (matches `piece_re` — a word, optionally followed by a number /
            # mana cost / "from X"). Pieces that match piece_re but aren't in
            # our known-keyword vocabulary are tolerated (skipped, not
            # treated as line-killing) — so "Flying, ward 2" still extracts
            # Flying even when "ward" isn't yet in _KEYWORD_LIST. Pieces that
            # don't match piece_re at all (a verb-y phrase mid-line) reject
            # the entire line.
            parts = [p.strip() for p in re.split(r',\s*', paragraph) if p.strip()]
            line_keywords = []
            line_ok = True
            for part in parts:
                m = piece_re.match(part)
                if not m:
                    line_ok = False
                    break
                head = m.group(1).strip().lower()
                if head in canonical:
                    line_keywords.append(canonical[head])
                # else: unknown-but-keyword-shaped (e.g. "ward 2") — skip silently
            if line_ok and line_keywords:
                found.extend(line_keywords)

        # Dedupe while preserving order — a card occasionally lists a keyword
        # twice (e.g. when a face grants it AND the printed line has it).
        seen = set()
        unique = []
        for kw in found:
            if kw not in seen:
                seen.add(kw)
                unique.append(kw)
        return unique
    
    # ---- Equipment bonus helpers ----
    def _get_equipment_bonuses(self, game=None) -> tuple:
        """Calculate total P/T bonuses and keywords from all attached equipment.
        Returns (power_bonus, toughness_bonus, keyword_list)."""
        p_bonus = 0
        t_bonus = 0
        kw_list = []
        if not self.attachments or game is None:
            return p_bonus, t_bonus, kw_list
        for equip_id in self.attachments:
            result = game.find_card_global(equip_id)
            if not result:
                continue
            equip_card = result[0]
            # July 30 batch-9 reviewer audit: find_card_global searches EVERY
            # zone, and the exile/bounce paths don't clear the creature's
            # attachments list — an exiled Batterskull kept granting its Germ
            # +4/+4/vigilance/lifelink for two more combats (8 phantom damage
            # + 8 phantom lifelink, game_1532224002137784391). Equipment only
            # grants while ON the battlefield (CR 301.5); a 0/0-base token
            # then correctly dies to SBA the moment its bonus vanishes.
            if len(result) > 2 and result[2] != Zone.BATTLEFIELD:
                continue
            if not equip_card.oracle_text:
                continue
            # May 16 audit: skip non-equipment attachments. The attachments list
            # also contains auras, and the regex below ("gets +N/+N") matches
            # "Enchanted creature gets +N/+N" oracle text just as readily as
            # "Equipped creature gets +N/+N". Without this filter, every aura
            # on a creature was double-counted (once here, once in
            # _get_aura_pump_bonus). Carrion Feeder + Boar Umbra read as 7
            # power: 1 base + 3 (mis-counted equipment) + 3 (correct aura).
            if 'equipment' not in (equip_card.type_line or '').lower():
                continue
            oracle = equip_card.oracle_text.lower()
            if equip_card.name.lower() == "mantle of the ancients":
                # Its multiplier counts attachments on this creature, not
                # permanents controlled by Mantle's controller.
                attached = sum(
                    1 for owner in game.players for permanent in owner.battlefield
                    if getattr(permanent, 'attached_to', None) == self.id
                    and ('aura' in (getattr(permanent, 'type_line', '') or '').lower()
                         or 'equipment' in (getattr(permanent, 'type_line', '') or '').lower())
                )
                p_bonus += attached
                t_bonus += attached
                continue
            # Aug 7 confirmation-batch audit (A-2a, CRITICAL): the old
            # whole-oracle regex matched "+2/+2" inside Umezawa's Jitte's
            # COST-GATED MODAL activated ability ("Remove a charge counter…:
            # Choose one — • Equipped creature gets +2/+2 until end of
            # turn."), granting a permanent phantom +2/+2 while Jitte had
            # zero counters (game_1535222945050271766). Anchor the P/T parse
            # to LINES that BEGIN with "equipped creature get" — every
            # legitimate static bonus in the inventory is printed that way
            # (regression-swept: 23/24 equipment identical, only Jitte
            # changes), while Jitte's modal bullets begin with "• ". June 10
            # mixed-sign support ([+-]) preserved for Skullclamp's +1/-1.
            for _line in oracle.split('\n'):
                _line = _line.strip()
                if not _line.startswith('equipped creature get'):
                    continue
                # Aug 10 deferred (A3): honour a "for each <X> you control"
                # multiplier, which this reader ignored entirely — Glaive of
                # the Guildpact granted a flat +1/+0 no matter how many Gates
                # its controller had (zero, in the only deck that runs it, so
                # the correct bonus was +0/+0). Shares one helper with the
                # aura sibling so the two cannot drift again.
                _m = _FOR_EACH_YOU_CONTROL.search(_line)
                if _m:
                    _sp, _st = int(_m.group(1)), int(_m.group(2))
                    if _m.group(3):
                        _n = _for_each_you_control_count(
                            game, equip_card._find_controller(game), _m.group(3))
                        p_bonus += _sp * _n
                        t_bonus += _st * _n
                    else:
                        p_bonus += _sp
                        t_bonus += _st
                    continue
                pt_match = re.search(r'gets?\s*([+-]\d+)/([+-]\d+)', _line)
                if pt_match:
                    p_bonus += int(pt_match.group(1))
                    t_bonus += int(pt_match.group(2))
            # Parse granted keywords. May 17 audit: 'shroud' was missing from
            # this list, so Lightning Greaves' shroud was never granted.
            # Aug 7 (A-2a/A-4): the old test was `kw in oracle and 'equipped
            # creature' in oracle` — two GLOBAL substring tests, so a keyword
            # ANYWHERE in the text was granted: Shadowspear's "{1}:
            # Permanents your opponents control lose hexproof and
            # indestructible…" handed its own bearer permanent hexproof AND
            # indestructible; Colossus Hammer's "loses flying" granted
            # flying; Champion's Helm's "As long as equipped creature is
            # legendary, it has hexproof" ignored the condition. Now:
            # activated-ability lines are stripped, the keyword must share a
            # SENTENCE with "equipped creature", "loses X" never grants X,
            # and "as long as" conditions are evaluated when checkable
            # (legendary via the bearer's type line) and skipped otherwise
            # (undercount is the safe direction — never a phantom grant).
            try:
                from rules.effect_templates import strip_activated_ability_lines
                kw_source = strip_activated_ability_lines(
                    equip_card.oracle_text).lower()
            except ImportError:
                kw_source = oracle
            for _sentence in re.split(r'[.\n]', kw_source):
                if 'equipped creature' not in _sentence:
                    continue
                if 'as long as' in _sentence:
                    if 'is legendary' in _sentence:
                        if 'legendary' not in (self.type_line or '').lower():
                            continue
                    else:
                        continue
                for kw in ['vigilance', 'lifelink', 'trample', 'flying',
                           'first strike', 'double strike', 'deathtouch',
                           'haste', 'hexproof', 'shroud', 'indestructible',
                           'menace', 'reach', 'fear', 'intimidate']:
                    if kw not in _sentence:
                        continue
                    if re.search(rf'\blose[s]?\b[^.]*\b{re.escape(kw)}',
                                 _sentence):
                        continue
                    if kw not in kw_list:
                        kw_list.append(kw)
        return p_bonus, t_bonus, kw_list

    def get_effective_power(self, game=None) -> int:
        """Get effective power including base, counters, modifiers, equipment, and anthem effects.
        Handles */* creatures (Ulvenwald Hydra, Tarmogoyf, etc.) via oracle text.

        IMPORTANT — DO NOT read `card.power` directly for effective P/T checks.
        That gives you the printed (base) power as a string, which:
          - Doesn't include +1/+1 / -1/-1 counters
          - Doesn't include equipment / aura bonuses
          - Doesn't include layer effects (Humility, anthems, Glorious Anthem)
          - Doesn't resolve */* CDA creatures (returns the literal '*')

        Layer 7 modifications come from the cached `_layers_power_mod` field,
        which is refreshed by `GameState.recalculate_power_toughness()` after
        every action that could change continuous effects (~22 call sites
        across actions.py, sba.py, spells.py, engine.py). The cache is kept
        fresh by discipline at the call sites, not by recomputing on every
        read. If you add a new layer-changing action, call
        `game.recalculate_power_toughness()` after the mutation.

        For non-battlefield zones (library, hand, graveyard, exile), reading
        `card.power` raw is correct — layer effects don't apply there.
        Same for copy effects, which copy the printed values per CR.
        """
        # Sylvan Awakening / Living Lands / Wake the Bear etc. animate lands
        # into 2/2 creatures via _animated_power/toughness EOT attributes. The
        # land's printed power is empty (None / ''), so without this branch the
        # base computed below would be 0 and a 0-toughness SBA would destroy
        # the land — including indestructible ones, because CR 704.5f's
        # "0 toughness dies even if indestructible" applies. Treat the animated
        # value as the effective base while the EOT marker is set.
        if getattr(self, '_animated_until_eot', False) or getattr(self, '_animated_permanent', False):
            try:
                base = int(getattr(self, '_animated_power', 0))
            except (ValueError, TypeError):
                base = 0
        else:
            try:
                base = int(self.power) if self.power and self.power != '*' else 0
            except (ValueError, TypeError):
                base = 0
        # CDA resolution for */* creatures — check oracle text for common patterns
        if ((self.power == '*' or base == 0) and self.oracle_text and game
                and not getattr(self, '_animated_until_eot', False)
                and not getattr(self, '_animated_permanent', False)):
            base = self._resolve_star_power(game)
            if base > 0:
                self.power_modifier = 0  # CDA recalculates each call; don't accumulate pump
        result = base + self.power_modifier
        # Debug: catch impossible CDA power. June 11 audit: the old >15
        # threshold assumed Tarmogoyf-style counts, but lands-you-control
        # CDAs (Beanstalk Giant) routinely exceed 15 legitimately in EDH —
        # one game printed ~90 spam lines. 25+ is still suspicious for any
        # real CDA; log once per creature per game instead of every call.
        if self.power == '*' and result > 25 and not getattr(self, '_cda_debug_logged', False):
            self._cda_debug_logged = True
            eq_p_dbg, _, _ = self._get_equipment_bonuses(game)
            print(f"[CDA-DEBUG] {self.name}: base={base}, power_mod={self.power_modifier}, "
                  f"counters_11={self.counters.get('+1/+1', 0)}, equip={eq_p_dbg}, "
                  f"layers_mod={getattr(self, '_layers_power_mod', 0)}")
        result += self.counters.get('+1/+1', 0) - self.counters.get('-1/-1', 0)
        eq_p, _, _ = self._get_equipment_bonuses(game)
        result += eq_p
        # Aura static "Enchanted creature gets +N/+M" bonuses (Draconic Destiny, etc.)
        ap, _ = self._get_aura_pump_bonus(game)
        result += ap
        # Self-bonus "for each X attached to it" (Heavenly Blademaster). The
        # layers engine skips these because the amount is dynamic; compute
        # on read so combat damage sees the correct effective power.
        sp, _ = self._get_self_for_each_bonus(game)
        result += sp
        # Layer 7: P/T from layers engine (Humility, anthems); falls back to inline scan
        layers_mod = getattr(self, '_layers_power_mod', 0)
        # May 20 audit (CRITICAL #2): consult _has_layers_pt_effect sentinel
        # BEFORE deciding to fall back. When the layers engine has applied
        # ANY P/T effect (even one that nets to 0 delta), the cached value is
        # authoritative and the fallback would double-count anthems on top of
        # a Humility-set base.
        has_layers_effect = getattr(self, '_has_layers_pt_effect', False)
        if has_layers_effect or layers_mod != 0:
            result += layers_mod
        elif game and self.is_creature():
            result += self._get_anthem_power_bonus(game)
        # Self-referential life-total debuff (Death's Shadow). Computed on read
        # because the amount tracks the controller's current life total.
        result -= self._get_life_total_debuff(game)
        # Conditional self-buff gated on the controller's life total (Serra
        # Ascendant) — dynamic like the debuff above, computed on read.
        lt_p, _ = self._get_life_threshold_bonus(game)
        result += lt_p
        # Moraug, Fury of Akoum: "+1/+0 for each time it has attacked this
        # turn" — dynamic per-creature amount the layers engine can't cache,
        # computed on read (Aug 7 batch audit C-1: both combats of the Moraug
        # turn dealt base-power damage because this clause was unimplemented).
        result += self._get_attack_count_bonus(game)
        combat_p, _ = self._get_combat_state_anthem_bonus(game)
        result += combat_p
        return result

    def get_effective_toughness(self, game=None) -> int:
        """Get effective toughness including base, counters, modifiers, equipment, and anthem effects.
        Handles */* creatures via oracle text."""
        # Animated-land toughness (Sylvan Awakening etc.) — see get_effective_power
        # for full rationale. Without this, the SBA-zero-toughness check fires
        # on every land the moment Sylvan Awakening resolves.
        if getattr(self, '_animated_until_eot', False) or getattr(self, '_animated_permanent', False):
            try:
                base = int(getattr(self, '_animated_toughness', 0))
            except (ValueError, TypeError):
                base = 0
        else:
            try:
                base = int(self.toughness) if self.toughness and self.toughness != '*' else 0
            except (ValueError, TypeError):
                base = 0
        if ((self.toughness == '*' or base == 0) and self.oracle_text and game
                and not getattr(self, '_animated_until_eot', False)
                and not getattr(self, '_animated_permanent', False)):
            base = self._resolve_star_toughness(game)
        result = base + getattr(self, 'toughness_modifier', 0)
        result += self.counters.get('+1/+1', 0) - self.counters.get('-1/-1', 0)
        _, eq_t, _ = self._get_equipment_bonuses(game)
        result += eq_t
        # Aura static "Enchanted creature gets +N/+M" bonuses
        _, at = self._get_aura_pump_bonus(game)
        result += at
        # Self-bonus "for each X attached to it" (Heavenly Blademaster) —
        # see get_effective_power for rationale.
        _, st = self._get_self_for_each_bonus(game)
        result += st
        # Layer 7: P/T from layers engine; falls back to inline anthem scan
        # May 20 audit (CRITICAL #2): same sentinel check as get_effective_power.
        _lt_mod = getattr(self, '_layers_toughness_mod', 0)
        _has_layers_effect = getattr(self, '_has_layers_pt_effect', False)
        if _has_layers_effect or _lt_mod != 0:
            result += _lt_mod
        elif game and self.is_creature():
            result += self._get_anthem_toughness_bonus(game)
        # Self-referential life-total debuff (Death's Shadow) — see get_effective_power.
        result -= self._get_life_total_debuff(game)
        # Conditional self-buff gated on controller's life (Serra Ascendant).
        _, lt_t = self._get_life_threshold_bonus(game)
        result += lt_t
        _, combat_t = self._get_combat_state_anthem_bonus(game)
        result += combat_t
        return result

    def _get_attached_auras(self, game):
        """Return list of aura permanents attached to this card."""
        if not game:
            return []
        auras = []
        for p in game.players:
            for perm in p.battlefield:
                if (getattr(perm, 'attached_to', None) == self.id and
                        'aura' in (getattr(perm, 'type_line', '') or '').lower()):
                    auras.append(perm)
        return auras

    def _get_life_total_debuff(self, game) -> int:
        """Death's Shadow: "Death's Shadow gets -X/-X, where X is your life total."
        The layers engine skips it (the amount is dynamic — it tracks a player's
        life total), so we compute on read. Without this, Death's Shadow shipped
        as a vanilla 13/13 regardless of life (it dealt 13 at 6 life in the May 26
        batch, and would illegally survive at 13+ life where it should die to SBA).
        Returns the magnitude X (controller's life, floored at 0) when the oracle
        declares the penalty, else 0."""
        if not game or not self.oracle_text:
            return 0
        o = self.oracle_text.lower()
        if 'where x is your life total' not in o or 'gets -x/-x' not in o:
            return 0
        for p in game.players:
            if self in p.battlefield:
                return max(0, int(getattr(p, 'life', 0)))
        return 0

    def _get_attack_count_bonus(self, game) -> int:
        """Moraug, Fury of Akoum: "Each creature you control gets +1/+0 for
        each time it has attacked this turn." A battlefield-wide static whose
        amount is per-creature and per-turn — the layers engine skips dynamic
        amounts, so compute on read (the Death's Shadow family). Reads the
        declared `attacks_this_turn` counter maintained at the six
        declared-attacker sites and cleared by end_turn's combat sweep.
        Power only — the printed clause has no toughness half."""
        if not game or not self.attacks_this_turn:
            return 0
        for p in game.players:
            if self in p.battlefield:
                for perm in p.battlefield:
                    o = (getattr(perm, 'oracle_text', '') or '').lower()
                    if ('gets +1/+0 for each time it has attacked this turn'
                            in o):
                        return int(self.attacks_this_turn)
                return 0
        return 0

    def _get_life_threshold_bonus(self, game):
        """Serra Ascendant: "As long as you have 30 or more life, this creature
        gets +5/+5 and has flying." The bonus tracks the controller's live life
        total, so it's computed on read (same pattern as the Death's Shadow
        debuff above). June 10 audit (V24): this conditional self-buff had no
        implementation anywhere — Serra attacked at power 1 with her controller
        at 37 life. Returns (power_bonus, toughness_bonus)."""
        if not game or not self.oracle_text:
            return (0, 0)
        o = self.oracle_text.lower()
        m = re.search(
            r'as long as you have (\d+) or more life,?[^.]*? gets \+(\d+)/\+(\d+)', o)
        if not m:
            return (0, 0)
        threshold = int(m.group(1))
        for p in game.players:
            if self in p.battlefield:
                if int(getattr(p, 'life', 0)) >= threshold:
                    return (int(m.group(2)), int(m.group(3)))
                return (0, 0)
        return (0, 0)

    def _get_self_for_each_bonus(self, game):
        """Return (power, toughness) bonus from "for each X attached to it"
        self-buffing oracle text (Heavenly Blademaster, Sram's Expertise,
        Bruna). The layers engine skips these because the bonus is dynamic
        — it changes with attachment count — so we compute on read instead.

        Patterns:
            "<name> gets +N/+N for each Aura attached to it"
            "<name> gets +N/+N for each Equipment attached to it"
            "<name> gets +N/+N for each Aura and Equipment attached to it"
        """
        if not game or not self.oracle_text:
            return (0, 0)
        oracle = self.oracle_text.lower()
        if 'for each' not in oracle or 'attached to it' not in oracle:
            return (0, 0)
        import re as _re
        # Pattern: "gets +N/+M for each <thing> attached to it"
        match = _re.search(
            r'gets \+(\d+)/\+(\d+) for each (.+?) attached to it', oracle)
        if not match:
            return (0, 0)
        p_step, t_step = int(match.group(1)), int(match.group(2))
        what = match.group(3)
        wants_aura = 'aura' in what
        wants_equip = 'equipment' in what
        # Count attached items matching the requested types
        count = 0
        for p in game.players:
            for perm in p.battlefield:
                if getattr(perm, 'attached_to', None) != self.id:
                    continue
                tl = (getattr(perm, 'type_line', '') or '').lower()
                if wants_aura and 'aura' in tl:
                    count += 1
                elif wants_equip and 'equipment' in tl:
                    count += 1
        return (p_step * count, t_step * count)

    def _get_aura_pump_bonus(self, game):
        """Return (power, toughness) bonus from attached auras' static
        'Enchanted creature gets +N/+M' clauses. Handles both positive
        (Draconic Destiny, Unholy Strength) and negative (rare) patterns."""
        if not game:
            return (0, 0)
        p_bonus = 0
        t_bonus = 0
        import re as _re
        for aura in self._get_attached_auras(game):
            oracle = (aura.oracle_text or '').lower()
            if aura.name.lower() == "mantle of the ancients":
                attached = sum(
                    1 for owner in game.players for permanent in owner.battlefield
                    if getattr(permanent, 'attached_to', None) == self.id
                    and ('aura' in (getattr(permanent, 'type_line', '') or '').lower()
                         or 'equipment' in (getattr(permanent, 'type_line', '') or '').lower())
                )
                p_bonus += attached
                t_bonus += attached
                continue
            if aura.name.lower() == "sage's reverie":
                # Count only Auras this Aura's controller controls which are
                # themselves attached to creatures. Other restrictive tails
                # remain intentionally declined by the generic parser.
                controller = aura._find_controller(game)
                attached_auras = 0
                if controller is not None:
                    for permanent in controller.battlefield:
                        if ('aura' in (getattr(permanent, 'type_line', '') or '').lower()
                                and getattr(permanent, 'attached_to', None)):
                            target = game.find_card_global(permanent.attached_to)
                            if target and target[0].is_creature(game):
                                attached_auras += 1
                p_bonus += attached_auras
                t_bonus += attached_auras
                continue
            # Signed pattern covers +N/+M, -N/-M, and mixed-sign (+N/-M) auras
            # in one pass (June 10 audit, same class as the Skullclamp fix).
            # June 10 deep-dive (B7): the optional "for each <type> you
            # control" multiplier (Ethereal Armor) was silently discarded —
            # the flat +1/+1 applied while 4-6 enchantments were in play.
            # June 11 audit: compound kinds ("for each artifact and/or
            # enchantment you control" — All That Glitters) didn't match the
            # single-kind pattern, so the multiplier was silently discarded
            # and the aura applied a flat +1/+1 — the enchanted commander's
            # power froze while the artifact count grew, shifting the kill
            # turn by two in games 1514626038486007948 / 1514621744143667220.
            # Aug 10 deferred (A3): both readers now share
            # _FOR_EACH_YOU_CONTROL / _for_each_you_control_count, so the
            # equipment side cannot fall behind the aura side again (it had
            # NO multiplier handling at all). Behaviour on the aura side is
            # unchanged for the four card kinds; it additionally gains
            # subtype counting and the restrictive-clause decline.
            for m in _FOR_EACH_YOU_CONTROL.finditer(oracle):
                step_p, step_t = int(m.group(1)), int(m.group(2))
                if m.group(3):
                    count = _for_each_you_control_count(
                        game, aura._find_controller(game), m.group(3))
                    p_bonus += step_p * count
                    t_bonus += step_t * count
                else:
                    p_bonus += step_p
                    t_bonus += step_t
        return (p_bonus, t_bonus)

    def _get_anthem_power_bonus(self, game) -> int:
        """Calculate total power bonus from anthem-style continuous effects on the battlefield."""
        bonus = 0
        controller = self._find_controller(game)
        if not controller:
            return 0
        for p in game.players:
            for perm in p.battlefield:
                if perm.id == self.id:
                    continue  # Don't buff yourself
                oracle = (perm.oracle_text or '').lower()
                perm_controller = perm._find_controller(game)
                # "Creatures you control get +N/+N" (Glorious Anthem, Gaea's Anthem)
                # "Other creatures you control get +N/+N" (Lord of Atlantis, etc.)
                if perm_controller == controller:
                    import re as _re
                    # May 26 audit: skip conditional anthems whose threshold isn't
                    # met (Beastmaster Ascension +5/+5 only at 7+ quest counters).
                    # This inline fallback is a SECOND anthem path parallel to the
                    # layers engine; it needs the same gate as the registration.
                    if game._has_conditional_static(oracle) and not game._static_condition_met(perm, oracle):
                        continue
                    # June 10 audit (V18): scan only static sentences. The
                    # raw-oracle scan matched Castle Embereth's ACTIVATED
                    # ability ("{1}{R}{R}, {T}: Creatures you control get
                    # +1/+0 until end of turn") as an always-on anthem —
                    # every creature read +1/+0 with zero activations.
                    for sent in self._anthem_static_sentences(oracle):
                        # Match "+N/+N" patterns for creatures you control
                        # Covers: "creatures you control", "creature tokens you
                        # control", "other creature tokens you control"
                        # (Intangible Virtue, Phantom General)
                        for m in _re.finditer(_ANTHEM_INLINE_RE, sent):
                            # "creature tokens" only applies to tokens
                            if 'creature token' in m.group() and not getattr(self, 'is_token', False):
                                continue
                            bonus += int(m.group(1))
        return bonus

    def _anthem_static_sentences(self, oracle: str):
        """Yield the sentences eligible for inline anthem scanning.

        June 10 audit (V18): the inline anthem fallback scanned RAW oracle
        text, so activated abilities ("{1}{R}{R}, {T}: Creatures you control
        get +1/+0 until end of turn" — Castle Embereth) and triggered pumps
        read as permanent anthems. Filtering is SENTENCE-level (not
        paragraph-level) because single-line oracles like Beastmaster
        Ascension carry a trigger sentence AND the "As long as … get +5/+5"
        static in one paragraph — rejecting the whole paragraph for the
        leading trigger word killed the legitimate anthem (caught by
        tests/test_models.py on the first suite run).
        """
        for para in (oracle or '').split('\n'):
            p = para.strip()
            if not p:
                continue
            if ':' in p:
                cost_part = p.split(':', 1)[0].lower()
                if any(sym in cost_part for sym in ('{t}', '{q}', '{w}', '{u}', '{b}',
                                                    '{r}', '{g}', '{c}', '{x}', '{s}')) \
                   or any(ch.isdigit() for ch in cost_part):
                    continue  # activated ability — skip the whole paragraph
            for sent in p.split('. '):
                s = sent.strip().lower()
                if not s or 'until end of turn' in s:
                    continue
                if s.startswith(('when ', 'whenever ', 'at the beginning', 'at end')):
                    continue
                yield s

    def _get_anthem_toughness_bonus(self, game) -> int:
        """Calculate total toughness bonus from anthem-style continuous effects."""
        bonus = 0
        controller = self._find_controller(game)
        if not controller:
            return 0
        for p in game.players:
            for perm in p.battlefield:
                if perm.id == self.id:
                    continue
                oracle = (perm.oracle_text or '').lower()
                perm_controller = perm._find_controller(game)
                if perm_controller == controller:
                    import re as _re
                    # May 26 audit: conditional-anthem gate (see _get_anthem_power_bonus).
                    if game._has_conditional_static(oracle) and not game._static_condition_met(perm, oracle):
                        continue
                    # June 10 audit (V18): static-sentence filter — see
                    # _get_anthem_power_bonus for rationale (Castle Embereth).
                    for sent in self._anthem_static_sentences(oracle):
                        for m in _re.finditer(_ANTHEM_INLINE_RE, sent):
                            if 'creature token' in m.group() and not getattr(self, 'is_token', False):
                                continue
                            bonus += int(m.group(2))
        return bonus

    def _get_combat_state_anthem_bonus(self, game) -> tuple:
        """Return static anthem bonuses gated on this creature attacking.

        Combat state is intentionally absent from the layers snapshot, so an
        unconditional layer effect would boost nonattackers. Keep this small,
        dynamic read alongside the other dynamic P/T readers.
        """
        if not game or not getattr(self, 'attacking', False):
            return (0, 0)
        controller = self._find_controller(game)
        if controller is None:
            return (0, 0)
        p_bonus = t_bonus = 0
        import re as _re
        for perm in controller.battlefield:
            for sentence in self._anthem_static_sentences(
                    (perm.oracle_text or '').lower()):
                match = _re.search(
                    r'\battacking creatures you control get \+(\d+)/\+(\d+)',
                    sentence)
                if match:
                    p_bonus += int(match.group(1))
                    t_bonus += int(match.group(2))
        return p_bonus, t_bonus

    def _resolve_star_power(self, game) -> int:
        """Resolve */* power from oracle text (CDA). Used by get_effective_power."""
        oracle = (self.oracle_text or '').lower()
        owner = self._find_controller(game)
        if not owner:
            return 0
        if 'number of lands you control' in oracle:
            return len(owner.lands())
        if 'number of creatures you control' in oracle:
            return len(owner.creatures())
        if 'number of snow permanents you control' in oracle:
            return sum(
                1 for permanent in owner.battlefield
                if 'snow' in (permanent.type_line or '').lower()
                and not getattr(permanent, '_phased_out', False)
            )
        if 'number of spirits you control' in oracle:
            return sum(
                1 for permanent in owner.battlefield
                if permanent.is_creature(game=game)
                and 'spirit' in {t.lower() for t in permanent.get_creature_types()}
            )
        if 'number of cards in your hand' in oracle:
            return len(owner.hand)
        if 'equal to your life total' in oracle:
            return owner.life
        if 'card types among cards in all graveyards' in oracle:
            types_seen = set()
            for p in game.players:
                for c in p.graveyard:
                    for t in ['creature', 'land', 'instant', 'sorcery', 'artifact', 'enchantment', 'planeswalker']:
                        if t in (c.type_line or '').lower():
                            types_seen.add(t)
            return len(types_seen)
        # Generic "cards in all graveyards" (Lhurgoyf, Mortivore) — MUST be after the type-counting check
        # above, otherwise Tarmogoyf ("card TYPES among cards in all graveyards") would match this first
        if 'creature cards in all graveyards' in oracle or 'cards in all graveyards' in oracle:
            return sum(len(p.graveyard) for p in game.players)
        if 'cards in your graveyard' in oracle:
            return len(owner.graveyard)
        return 0  # Unknown CDA

    def _resolve_star_toughness(self, game) -> int:
        """Resolve */* toughness from oracle text. Tarmogoyf gets +1."""
        oracle = (self.oracle_text or '').lower()
        base = self._resolve_star_power(game)
        if 'toughness is equal' in oracle and 'plus 1' in oracle:
            return base + 1
        return base

    def _find_controller(self, game) -> 'Player':
        """Find which player controls this card on the battlefield."""
        if not game:
            return None
        for p in game.players:
            if self in p.battlefield:
                return p
        return None

    def is_creature(self, game=None) -> bool:
        if not self.type_line or "creature" not in self.type_line.lower():
            return False
        # IMPENDING (CR 702.166a, Aug 3 2026): "if you cast this spell for its
        # impending cost, it enters with N time counters and ISN'T A CREATURE
        # until the last is removed". The second half is the whole downside of
        # the discount, and without it the cheap cast was strictly better than
        # the expensive one. Gated on the stamp AND on counters remaining, so a
        # full-price cast is unaffected and the suppression ends by itself.
        if getattr(self, '_cast_via_impending', False):
            if (self.counters or {}).get('time', 0) > 0:
                return False
        # Gods with devotion threshold: "As long as your devotion to X is less than N, ~ isn't a creature"
        if game and "God" in self.type_line and self.oracle_text and "devotion" in self.oracle_text.lower():
            oracle_lower = self.oracle_text.lower()
            import re
            # Match "devotion to {color(s)} is less than {number}"
            dev_match = re.search(r"devotion to (\w+(?:\s+and\s+\w+)?)\s+is less than (\w+)", oracle_lower)
            if dev_match:
                color_map = {"white": "W", "blue": "U", "black": "B", "red": "R", "green": "G"}
                threshold_map = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7}
                color_text = dev_match.group(1)
                threshold = threshold_map.get(dev_match.group(2), 5)
                # Parse colors (single or "X and Y")
                symbols = []
                for word in color_text.split():
                    if word in color_map:
                        symbols.append(color_map[word])
                # Find controller
                controller = None
                for p in game.players:
                    if self in p.battlefield:
                        controller = p
                        break
                if controller and symbols:
                    # May 25 audit (F23): the old `mc.upper().count(f"{{{sym}}}")`
                    # only matched literal `{B}` and missed Phyrexian (`{B/P}`),
                    # hybrid (`{W/B}`), and monocolored-hybrid (`{2/B}`) symbols
                    # — all of which contribute to devotion per CR 700.2 +
                    # 107.4f. Iterate symbols and substring-check each color;
                    # a hybrid counts as 1 devotion toward the combined "X and Y"
                    # threshold (Gatherer ruling: hybrid contributes once to a
                    # multi-color devotion, not twice).
                    import re as _re_dev
                    devotion = 0
                    for perm in controller.battlefield:
                        mc = (perm.mana_cost or "").upper()
                        for symbol_match in _re_dev.finditer(r'\{([^}]+)\}', mc):
                            symbol_content = symbol_match.group(1)
                            if any(sym in symbol_content for sym in symbols):
                                devotion += 1
                                # Hybrid counts once per symbol (not per matching color)
                    # May 25 audit (F24 instrumentation): log every devotion check so
                    # the next batch can verify whether the type-flip is consistently
                    # applied across all call sites. The audit hypothesis was that
                    # the Tier 3 judge prompt saw Erebos as "not a creature" while
                    # the combat path saw him as one — instrumenting both sides lets
                    # us diff. Only log when we're actually about to flip the result
                    # (devotion < threshold) so noise stays bounded.
                    is_creature_now = devotion >= threshold
                    # May 30 audit: dedup — only log when the creature-status
                    # CHANGES (this fired 462x identically in one game). Track the
                    # last logged state per card on the game so transitions stay
                    # visible for auditing; with no game to dedup against, keep the
                    # original always-print behavior.
                    _dc = None
                    if game is not None:
                        _dc = getattr(game, '_devotion_check_last', None)
                        if _dc is None:
                            _dc = {}
                            game._devotion_check_last = _dc
                    if not is_creature_now:
                        if _dc is None or _dc.get(self.id) is not False:
                            print(f"[DEVOTION-CHECK] {self.name}: "
                                  f"devotion={devotion}/{threshold} ({''.join(symbols)}) "
                                  f"controller={controller.name} → NOT a creature")
                        if _dc is not None:
                            _dc[self.id] = False
                    elif _dc is not None:
                        _dc[self.id] = True
                    if devotion < threshold:
                        return False
        return True

    def get_creature_types(self) -> List[str]:
        """Return creature subtypes from the text after the type-line dash."""
        type_line = self.type_line or ""
        if "creature" not in type_line.lower():
            return []
        parts = re.split(r"\s+[—-]\s+", type_line, maxsplit=1)
        if len(parts) != 2:
            return []
        return [part for part in re.split(r"[\s/]+", parts[1].strip()) if part]
    
    def is_land(self) -> bool:
        return self.type_line and "land" in self.type_line.lower()
    
    def is_instant(self) -> bool:
        return self.type_line and "instant" in self.type_line.lower()
    
    def is_sorcery(self) -> bool:
        return self.type_line and "sorcery" in self.type_line.lower()
    
    def is_artifact(self) -> bool:
        return self.type_line and "artifact" in self.type_line.lower()
    
    def is_enchantment(self) -> bool:
        return self.type_line and "enchantment" in self.type_line.lower()
    
    def is_planeswalker(self) -> bool:
        return self.type_line and "planeswalker" in self.type_line.lower()

    def is_battle(self) -> bool:
        return self.type_line and "battle" in self.type_line.lower()

    def _remove_all_abilities_active(self, game) -> bool:
        """True if a board-wide "lose all abilities" effect (Humility) is
        registered in the layers engine. has_keyword uses this to defer keyword
        resolution to the engine's timestamp-ordered Layer-6 result."""
        le = getattr(game, 'layers_engine', None)
        if le is None:
            return False
        for eff in getattr(le, 'effects', []):
            if (getattr(eff, 'remove_all_abilities', False)
                    and 'all creature' in (getattr(eff, 'applies_to', '') or '').lower()):
                return True
        return False

    def has_keyword(self, keyword: str, game=None) -> bool:
        """Check if card has a keyword ability (permanent, temporary, equipment, or static effects)."""
        keyword_lower = keyword.lower()
        # G2-1 (Aug 7): "loses <keyword> until end of turn" (Shadowspear
        # class) — the removal wins over every grant source for the turn.
        if keyword_lower in (k.lower() for k in self.temp_removed_keywords):
            return False
        # May 30 audit (option b — compute-on-read for keywords): under a board-
        # wide "lose all abilities" effect (Humility, Layer 6), defer to the layers
        # engine's resolved ability set rather than reading raw keyword lists.
        # recalculate_power_toughness caches result.abilities onto
        # _resolved_keywords — printed keywords + static grants, with Humility's
        # remove-all and the grants applied in TIMESTAMP order (CR 613.7). This
        # replaces the May-26 blanket strip, which wrongly removed abilities
        # GRANTED AFTER Humility (e.g. Stonehoof Chieftain's trample). Equipment/
        # aura/temp grants aren't engine-registered effects, so under remove-all
        # the engine set is authoritative for them too (the rare equipment-granted-
        # after-Humility case stays approximate — documented, low frequency).
        if (game is not None and self.is_creature(game=game)
                and self._remove_all_abilities_active(game)):
            resolved = getattr(self, '_resolved_keywords', None)
            if resolved is not None:
                return keyword_lower in resolved
            return False  # recalc hasn't run since the effect registered — strip
        # May 30 audit: conditional self-keyword gated on life total (Serra
        # Ascendant: "As long as you have 30 or more life, ~ gets +5/+5 and has
        # lifelink"). The +5/+5 half is gated via the layers conditional-static
        # path, but the keyword half is baked into self.keywords at load, so it
        # applied unconditionally (Serra lifelinked at 25 life). If the card's own
        # oracle grants THIS keyword only while a life threshold holds and that
        # threshold isn't met, the keyword is off.
        if game is not None and self.oracle_text:
            _o = self.oracle_text.lower()
            if keyword_lower in _o and 'as long as you have' in _o and 'or more life' in _o:
                import re as _kre
                _m = _kre.search(
                    r'as long as you have (\d+) or more life[^.]*\b' + _kre.escape(keyword_lower), _o)
                if _m:
                    _ctrl = self._find_controller(game)
                    if _ctrl is not None and getattr(_ctrl, 'life', 0) < int(_m.group(1)):
                        return False
        all_keywords = [k.lower() for k in self.keywords] + [k.lower() for k in self.temp_keywords]
        # Also check keywords granted by continuous effects (layers engine)
        granted = getattr(self, '_granted_keywords', set())
        if granted:
            all_keywords.extend(k.lower() for k in granted)
        if keyword_lower in all_keywords:
            return True
        # Check equipment-granted keywords
        if game and self.attachments:
            _, _, eq_kw = self._get_equipment_bonuses(game)
            if keyword_lower in [k.lower() for k in eq_kw]:
                return True
        # Check aura-granted keywords: "Enchanted creature ... has <keywords>"
        # (Draconic Destiny grants flying + haste; Flight grants flying; etc.)
        if game:
            import re as _re
            for aura in self._get_attached_auras(game):
                aura_oracle = (aura.oracle_text or '').lower()
                if 'enchanted creature' not in aura_oracle:
                    continue
                # Capture everything after "enchanted creature ... has " up to the
                # next period or opening quote (which starts activated ability text).
                m = _re.search(r'enchanted creature[^."]*?\bhas\s+([^."]+)', aura_oracle)
                if not m:
                    continue
                grant_text = m.group(1)
                # Normalise multi-word keyword for substring check
                if keyword_lower in grant_text:
                    return True
        # June 10 audit (V24): conditional self-GRANT. The life-threshold gate
        # near the top of this function only DENIES a printed keyword while
        # the threshold is unmet; a keyword that exists ONLY inside the
        # conditional sentence ("...and has flying" — Serra Ascendant) was
        # never granted at all. Grant it while the threshold holds.
        if game is not None and self.oracle_text:
            import re as _gre
            _gm = _gre.search(
                r'as long as you have (\d+) or more life[^.]*\bha(?:s|ve)\b[^.]*\b'
                + _gre.escape(keyword_lower),
                self.oracle_text.lower())
            if _gm:
                _gctrl = self._find_controller(game)
                if _gctrl is not None and getattr(_gctrl, 'life', 0) >= int(_gm.group(1)):
                    return True
        return False

    def has_haste(self, game=None) -> bool:
        return self.has_keyword('Haste', game=game)

    def has_vigilance(self, game=None) -> bool:
        return self.has_keyword('Vigilance', game=game)

    def has_flying(self, game=None) -> bool:
        return self.has_keyword('Flying', game=game)

    def has_reach(self, game=None) -> bool:
        return self.has_keyword('Reach', game=game)

    def has_trample(self, game=None) -> bool:
        return self.has_keyword('Trample', game=game)

    def has_deathtouch(self, game=None) -> bool:
        return self.has_keyword('Deathtouch', game=game)

    def has_first_strike(self, game=None) -> bool:
        return self.has_keyword('First strike', game=game) or self.has_keyword('Double strike', game=game)

    def has_double_strike(self, game=None) -> bool:
        return self.has_keyword('Double strike', game=game)

    def has_lifelink(self, game=None) -> bool:
        return self.has_keyword('Lifelink', game=game)

    def has_defender(self, game=None) -> bool:
        return self.has_keyword('Defender', game=game)
    
    def can_attack(self, game=None) -> bool:
        """Check if creature can legally attack."""
        if not self.is_creature(game=game):
            return False
        if self.tapped:
            return False
        if self.has_defender():
            return False
        # Summoning sickness check (haste bypasses)
        if self.summoning_sick and not self.has_haste():
            return False
        # Check for "can't attack" effects from attached auras (Pacifism,
        # Arrest, Faith's Fetters, etc.). The legacy code looked at a
        # nonexistent `attached_auras` attribute — the actual storage is
        # `attachments` (list of IDs). When `game` is available, resolve
        # the IDs through `_get_attached_auras(game)` so Pacifism's
        # "can't attack or block" restriction actually enforces.
        if game is not None:
            try:
                for aura in self._get_attached_auras(game):
                    oracle = (getattr(aura, 'oracle_text', '') or '').lower()
                    if _restricts_combat(oracle, 'attack'):
                        return False
            except Exception:
                pass
        # Check for "can't attack" from layers/effects
        if getattr(self, 'cant_attack_this_turn', False):
            return False
        # Aug 8 batch audit (#2): Ascend (CR 702.131). "This creature can't
        # attack or block unless you have the city's blessing" is a
        # restriction printed on the card's OWN text (Wayward Swordtooth),
        # gated on its controller's blessing — a mechanic that previously
        # did not exist anywhere in the engine (the creature blocked and
        # killed a commander at six permanents, game_1535486721779568700).
        if not self._city_blessing_combat_ok(game):
            return False
        # Aug 10 audit (CRITICAL, game-deciding): a GLOBAL, non-Aura "creatures
        # you control can't attack" static had no consultation point anywhere.
        # can_attack only ever looked at attachments on the creature itself and
        # can_attack_with only adds attack TAXES — its scan explicitly skips
        # the attacking player's own battlefield — so Glacial Chasm's entire
        # drawback was inert while its damage-prevention half worked. Qwen
        # attacked out from under it for 12 commander damage and won
        # (game_1536023907918680074). CR 508.1c.
        if game is not None and _controller_forbids_attacking(game, self):
            return False
        return True

    def _city_blessing_combat_ok(self, game) -> bool:
        """False only when this card's own oracle carries the Ascend combat
        restriction AND its controller lacks the city's blessing. Shared by
        can_attack and can_block — the restriction's printed phrasing covers
        both verbs at once."""
        oracle = (self.oracle_text or '').lower()
        if "unless you have the city's blessing" not in oracle:
            return True
        if game is None:
            # No game context — cannot evaluate the blessing; err permissive
            # (matches how the aura-restriction loop degrades without game).
            return True
        controller = next(
            (p for p in getattr(game, 'players', []) or []
             if self in getattr(p, 'battlefield', [])), None)
        if controller is None:
            return True
        from mtg.helpers import has_city_blessing
        return has_city_blessing(game, controller)

    def can_block(self, attacker: 'Card' = None, game=None) -> bool:
        """Check if creature can legally block (optionally a specific attacker)."""
        # May 25 audit (F24): pass `game` through to is_creature so devotion-
        # gated Theros gods (Erebos isn't a creature unless devotion ≥5) are
        # correctly rejected as blockers. Without this, Erebos blocked
        # attackers at devotion=4 in game_1508578146641907722 (CR 509.1a:
        # blockers must be creatures).
        if not self.is_creature(game=game):
            return False
        if self.tapped:
            return False
        # "Can't block this turn" effects (e.g. Chandra, Pyromaster +1)
        if getattr(self, 'cant_block_this_turn', False):
            return False
        # Symmetric to can_attack: Pacifism etc. restrict blocking too.
        if game is not None:
            try:
                for aura in self._get_attached_auras(game):
                    oracle = (getattr(aura, 'oracle_text', '') or '').lower()
                    if _restricts_combat(oracle, 'block'):
                        return False
            except Exception:
                pass
        # Aug 8 batch audit (#2): the Ascend combat restriction covers
        # blocking too ("can't attack or block unless you have the city's
        # blessing") — the live defect WAS a block (Wayward Swordtooth
        # killed Jorn as a blocker at six permanents).
        if not self._city_blessing_combat_ok(game):
            return False
        if attacker:
            # Flying creatures can only be blocked by flying/reach
            if attacker.has_flying() and not (self.has_flying() or self.has_reach()):
                return False
            # Aug 10 deferred (E2) — CR 702.16c: a creature with protection
            # from a quality "can't be BLOCKED BY" creatures with that quality.
            # Note the direction, which corrects the recorded item (it cited
            # CR 509.1b): it is the ATTACKER's protection tested against THIS
            # candidate blocker's qualities, not the blocker's protection. The
            # live defect was Akroma (protection from black and from red)
            # blocked by a mono-black Butcher of Malakir. Same shape as the
            # flying/reach line above — an attacker-side quality checked
            # against `self`.
            try:
                from mtg.helpers import protection_blocks_from
                _prot, _why = protection_blocks_from(game, attacker, self)
                if _prot:
                    return False
            except ImportError:
                pass
            # Menace requires 2+ blockers (handled elsewhere)
            # Aug 2, 2026 — LANDWALK (CR 702.14). "This creature can't be
            # blocked as long as defending player controls a <type>." It had
            # no implementation at all: Street Wraith's swampwalk was inert
            # against a black deck, which is the only matchup where it does
            # anything. The check is on the BLOCKER's controller (the
            # defending player), so it belongs here rather than on the
            # attacker's evasion list.
            _lw = _parse_landwalk_types(
                getattr(attacker, 'oracle_text', '') or '')
            if _lw and game is not None:
                _me = self._find_controller(game) if hasattr(
                    self, '_find_controller') else None
                if _me is not None:
                    for _c in (getattr(_me, 'battlefield', []) or []):
                        _tl = (getattr(_c, 'type_line', '') or '').lower()
                        if 'land' in _tl and any(t in _tl for t in _lw):
                            return False
        return True
    
    def display_name(self) -> str:
        """Name with state indicators."""
        # Show MDFC back face name if played as back
        name = self.name
        if self.played_face == "back" and self.mdfc_back_name:
            name = self.mdfc_back_name
        
        indicators = []
        if self.tapped:
            indicators.append("(T)")
        if self.summoning_sick and not self.has_haste() and self.is_creature():
            indicators.append("🤢")  # Sick emoji for summoning sickness
        if self.counters:
            counter_str = ", ".join(f"{v} {k}" for k, v in self.counters.items())
            indicators.append(f"[{counter_str}]")
        if self.attacking:
            indicators.append("⚔️")
        if self.blocked_by:
            indicators.append("🛡️")
        if self.keywords:
            # Show first 2 keywords as abbreviations
            kw_abbrev = {'Flying': 'Fly', 'First strike': 'FS', 'Deathtouch': 'DT',
                        'Trample': 'Trmp', 'Lifelink': 'LL', 'Haste': 'Hst',
                        'Vigilance': 'Vig', 'Reach': 'Rch', 'Menace': 'Men'}
            # Skip 'Equip' (shown separately as (Equ)) and keywords that equipment GRANTS vs HAS
            skip_keywords = {'Equip', 'Haste', 'Hexproof', 'Shroud', 'Indestructible', 'Lifelink', 
                           'First strike', 'Double strike', 'Trample', 'Vigilance', 'Flying'}
            # Only skip for equipment - they grant these, they don't have them
            if self.type_line and "equipment" in self.type_line.lower():
                relevant_kws = [k for k in self.keywords[:4] if k not in skip_keywords]
            else:
                relevant_kws = [k for k in self.keywords[:2] if k != 'Equip']
            kws = [kw_abbrev.get(k, k[:3]) for k in relevant_kws]
            if kws:
                indicators.append(f"({','.join(kws)})")
        # Show planeswalker loyalty
        if self.is_planeswalker() and self.loyalty_counters > 0:
            indicators.append(f"[{self.loyalty_counters}]")
        # Show equipment status
        if "equipment" in self.type_line.lower() and not self.attached_to:
            indicators.append("(Equ)")
        
        suffix = " " + " ".join(indicators) if indicators else ""
        return f"{name}{suffix}"
    
    def to_dict(self) -> Dict:
        """Serialize card to JSON-compatible dict."""
        return {
            "name": self.name,
            "id": self.id,
            "owner_index": self.owner_index,
            "mana_cost": self.mana_cost,
            "cmc": self.cmc,
            "type_line": self.type_line,
            "oracle_text": self.oracle_text,
            "power": self.power,
            "toughness": self.toughness,
            "loyalty": self.loyalty,
            "keywords": self.keywords,
            "tapped": self.tapped,
            "summoning_sick": self.summoning_sick,
            "entered_this_turn": self.entered_this_turn,
            "counters": self.counters,
            "attached_to": self.attached_to,
            "attachments": self.attachments,
            "attacking": self.attacking,
            "attacking_player": self.attacking_player,
            "attacking_planeswalker": self.attacking_planeswalker,
            "blocking": self.blocking,
            "blocked_by": self.blocked_by,
            "damage_marked": self.damage_marked,
            "deathtouch_damage": self.deathtouch_damage,
            "power_modifier": self.power_modifier,
            "toughness_modifier": self.toughness_modifier,
            "temp_keywords": self.temp_keywords,
            "cant_block_this_turn": self.cant_block_this_turn,
            "loyalty_counters": self.loyalty_counters,
            "played_face": self.played_face,
            "mdfc_back_name": self.mdfc_back_name,
            "is_commander": self.is_commander,
            "is_signature_spell": self.is_signature_spell,
            "is_companion": self.is_companion,
            "color_identity": self.color_identity,
            "times_cast_from_command_zone": self.times_cast_from_command_zone,
            "mutated_cards": [c.to_dict() for c in self.mutated_cards],
            "mutated_under": self.mutated_under,
            "suspended": self.suspended,
        }
    
    @classmethod
    def from_dict(cls, data: Dict) -> 'Card':
        """Reconstruct card from dict."""
        card = cls(
            name=data["name"],
            id=data.get("id", ""),
            owner_index=data.get("owner_index", -1),
            mana_cost=data.get("mana_cost", ""),
            cmc=data.get("cmc", 0),
            type_line=data.get("type_line", ""),
            oracle_text=data.get("oracle_text", ""),
            power=data.get("power"),
            toughness=data.get("toughness"),
            loyalty=data.get("loyalty"),
            keywords=data.get("keywords", []),
            tapped=data.get("tapped", False),
            summoning_sick=data.get("summoning_sick", True),
            entered_this_turn=data.get("entered_this_turn", False),
            counters=data.get("counters", {}),
            attached_to=data.get("attached_to"),
            attachments=data.get("attachments", []),
            attacking=data.get("attacking", False),
            attacking_player=data.get("attacking_player"),
            attacking_planeswalker=data.get("attacking_planeswalker"),
            blocking=data.get("blocking", []),
            blocked_by=data.get("blocked_by", []),
            damage_marked=data.get("damage_marked", 0),
            deathtouch_damage=data.get("deathtouch_damage", 0),
            power_modifier=data.get("power_modifier", 0),
            toughness_modifier=data.get("toughness_modifier", 0),
            temp_keywords=data.get("temp_keywords", []),
            cant_block_this_turn=data.get("cant_block_this_turn", False),
            loyalty_counters=data.get("loyalty_counters", 0),
            played_face=data.get("played_face", ""),
            mdfc_back_name=data.get("mdfc_back_name", ""),
            is_commander=data.get("is_commander", False),
            is_signature_spell=data.get("is_signature_spell", False),
            is_companion=data.get("is_companion", False),
            color_identity=data.get("color_identity", []),
            times_cast_from_command_zone=data.get("times_cast_from_command_zone", 0),
            mutated_under=data.get("mutated_under", False),
            suspended=data.get("suspended", False),
        )
        # Restore mutated cards list
        card.mutated_cards = [Card.from_dict(mc) for mc in data.get("mutated_cards", [])]
        return card


@dataclass
class Player:
    """Represents a player in the game."""
    name: str
    user_id: Optional[int] = None  # Discord user ID, None for Claude
    is_claude: bool = False
    
    # Life and counters
    life: int = 20
    poison: int = 0
    energy: int = 0
    # Multiplayer seats are stable for the lifetime of a game. Eliminated
    # players remain in GameState.players so owner indices, saved references,
    # commander-damage keys, and drafted-seat results never renumber.
    seat_id: Optional[int] = None
    eliminated: bool = False
    loss_reason: str = ""
    # commander NAME -> combat damage taken from that commander. CR 903.10a
    # is per-COMMANDER ("by the same commander"), not per-player — keying by
    # controller index summed partner commanders into one bucket (Aug 1
    # batch-12: Thrasios 22 + Tymna 23 displayed as 45/21). Legacy saves may
    # carry int / digit-string keys (old per-player buckets); consumers
    # treat those as frozen legacy tallies.
    commander_damage: Dict[str, int] = field(default_factory=dict)
    # Bloodchief Ascension and similar "lost N life this turn" conditions.
    # Reset for every player when a new turn begins in GameEngine.end_turn.
    life_lost_this_turn: int = 0
    # "was dealt combat damage this turn" — Tymna the Weaver counts the
    # opponents this is true of. Added July 27, 2026 alongside the main-phase
    # trigger scan; nothing tracked it before because nothing could ask.
    # Reset for every player with life_lost_this_turn in GameEngine.end_turn.
    dealt_combat_damage_this_turn: bool = False
    # Aug 7 queue item Q3: while paying for a card that carries the Draugr
    # snow-as-any-color permission, _get_mana_production reports snow
    # sources as producing 'any'. Set (from spending_card) at the entry of
    # can_pay_mana_cost AND tap_sources_for_cost — every call overwrites it
    # from its own spending_card, so a stale True from an aborted call is
    # corrected by the next payment call (the narrow exception window is
    # documented at the setters; strict mode re-raises those anyway).
    _spend_snow_as_any: bool = field(default=False, repr=False, compare=False)
    
    # Zones
    library: List[Card] = field(default_factory=list)
    hand: List[Card] = field(default_factory=list)
    battlefield: List[Card] = field(default_factory=list)
    graveyard: List[Card] = field(default_factory=list)
    exile: List[Card] = field(default_factory=list)
    command_zone: List[Card] = field(default_factory=list)
    companion_zone: List[Card] = field(default_factory=list)  # Companion (outside the game)

    # Deck info
    deck_name: str = ""
    deck_source: str = ""  # Archidekt URL or file path
    
    # Game state
    lands_played_this_turn: int = 0
    max_lands_per_turn: int = 1
    has_drawn_for_turn: bool = False
    mulligans_taken: int = 0
    has_kept_hand: bool = False  # True once player keeps (can't mulligan after)
    spells_cast_this_turn: int = 0  # For day/night and werewolf transform tracking
    spells_cast_prev_turn: int = 0  # Spells cast during the player's previous turn
    noncreature_spells_cast_this_turn: int = 0  # For Esper Sentinel "first noncreature spell" tracking
    # Aug 2 batch-14: instants/sorceries only, for the delirium-adjacent
    # "cast three or more instant and sorcery spells this turn" family
    # (Arclight Phoenix). The noncreature counter above is too broad —
    # it also counts artifacts, enchantments and planeswalkers.
    instant_sorcery_spells_cast_this_turn: int = 0
    landfall_count_this_turn: int = 0  # Lands that entered under your control this turn (for Omnath, etc.)
    # Aug 3 2026 — miracle (CR 702.94) needs "is this the FIRST card you drew
    # this turn?", and nothing tracked per-turn draws at all. Incremented in
    # GameEngine.draw_cards (the draw choke point) and reset with the other
    # per-turn counters at turn advance.
    cards_drawn_this_turn: int = 0
    # Aug 8 2026 batch audit (#2) — Ascend / the city's blessing
    # (CR 702.131c-d): once earned it lasts the REST OF THE GAME, so this is
    # PERSISTENT state, not a per-turn transient — it serializes through
    # to_dict/from_dict (dropping it on save/load or !undo would strip an
    # earned blessing, a real correctness loss). Awarded sticky by
    # helpers.has_city_blessing.
    city_blessing: bool = False
    # Converge (CR 702.100a): colors committed by the most recent successful
    # tap_sources_for_cost. Read once by the cost stage and stamped onto the
    # spell as Card._colors_spent.
    _last_colors_spent: tuple = field(default=(), repr=False, compare=False)
    _last_payment: dict = field(
        default_factory=lambda: {'snow_spent': 0},
        repr=False, compare=False)
    # Aug 7 2026 queue item Q4 — snow provenance for FLOATING pool mana.
    # `_last_payment['snow_spent']` has been exact for mana consumed from
    # sources tapped by the payment engine, but `mana_pool` stores only
    # color totals, so mana floated BEFORE a payment (Phase-4 settle
    # excess, [ACTIVATE-MANA], rituals, Tier-3 add_mana) counted as
    # non-snow. This shadow dict tags the KNOWN-snow portion of the pool
    # per color. HARD RULES: a tag may never exceed the pool's own count
    # for that color (credit/debit clamp with min()), and a producer that
    # doesn't mark snow merely undercounts — the safe, documented
    # direction. Cleared with the pool; NOT serialized (a save/load drops
    # the tags, which is an undercount, per the declared-transient
    # convention).
    _pool_snow: Dict[str, int] = field(
        default_factory=dict, repr=False, compare=False)

    # ---- Transient runtime state (reset/derived during play; NOT serialized) ----
    # Same convention as Card: declare runtime flags, don't staple
    # (tests/test_ratchets.py ratchets the undeclared-staple count).
    # Teferi's Protection / Fog: damage prevention until expiry (checked
    # against game.turn_number; inf = never expires until flag cleared).
    _damage_prevented: bool = field(default=False, repr=False, compare=False)
    _damage_prevented_expires_turn: float = field(default=float("inf"), repr=False, compare=False)
    # Aug 10 2026 (G5): creature SUBTYPES exempt from the active prevention.
    # Moonmist prevents combat damage "by creatures other than Werewolves and
    # Wolves"; an empty list is the unconditional Fog / Teferi's Protection
    # case and leaves the gate's behaviour unchanged.
    _damage_prevented_except_subtypes: list = field(default_factory=list, repr=False, compare=False)
    # Aug 10 deferred (A2): ONE flag has always served two different printed
    # effects — prevent_combat_damage (Fog, Holy Day, Moment's Peace, Tangle,
    # Arachnogenesis, Moonmist: "prevent all COMBAT damage") and
    # prevent_all_damage (Teferi's Protection: all damage). The noncombat
    # player funnel gates on the flag deliberately, for Teferi's, which meant
    # a Fog also blanked burn spells. Extending the flag to creatures without
    # this distinction would have propagated that over-prevention, so the two
    # are separated here: True = combat damage only.
    _damage_prevented_combat_only: bool = field(default=False, repr=False, compare=False)
    # Teferi's Protection: life total can't change while locked (CR 119.3-adjacent).
    _life_total_locked: bool = field(default=False, repr=False, compare=False)
    _life_total_locked_expires_turn: float = field(default=float("inf"), repr=False, compare=False)
    # Card names blocked from casting this game (commander color identity,
    # CR 903.4) — surfaced in the AI's "DO NOT CAST" prompt section.
    _color_id_blocklist: set = field(default_factory=set, repr=False, compare=False)
    # June 10 audit (V3): deck color identity cached at first computation.
    # CR 903.4 — identity is a deck-construction constant; recomputing from
    # live card locations broke partner decks when a commander was stolen.
    _commander_identity_cache: Any = field(default=None, repr=False, compare=False)
    # July 20 audit: pain-land tap damage (City of Brass, Ancient Tomb) was
    # console-only — 13 invisible life drops in one July 16 game's Discord
    # log. Tap paths buffer a display line here; cast_spell_async drains
    # them into effect_messages after payment.
    _pending_tap_damage_msgs: list = field(default_factory=list, repr=False, compare=False)
    # Kessig Naturalist-class mana persists through this numbered turn.
    _retain_mana_through_turn: Optional[int] = field(
        default=None, repr=False, compare=False)
    # Aurelia's Fury: a player dealt damage by it cannot cast noncreature
    # spells for the rest of that numbered turn.
    _noncreature_cast_locked_turn: Optional[int] = field(
        default=None, repr=False, compare=False)

    # Cards exiled but playable this turn (Chandra 0, Outpost Siege, etc.)
    # List of card IDs that can be played from exile until end of turn
    playable_from_exile: List[str] = field(default_factory=list)

    # Bug #28: Cards in graveyard that gained flashback (Snapcaster Mage, etc.)
    # List of card IDs playable from graveyard until end of turn
    playable_from_graveyard: List[str] = field(default_factory=list)

    # Mana pool {color: amount} - W, U, B, R, G, C (colorless)
    mana_pool: Dict[str, int] = field(default_factory=lambda: {'W': 0, 'U': 0, 'B': 0, 'R': 0, 'G': 0, 'C': 0})
    # Mana with a spending restriction keeps its provenance until spent or
    # the pool empties. Each bucket: color, amount, restriction, source.
    restricted_mana_pool: List[Dict[str, Any]] = field(
        default_factory=list, repr=False, compare=False)


    def record_life_loss(self, amount: int, game=None, source_name: str = "") -> None:
        """Record positive life loss for turn-scoped trigger conditions.

        Aug 10 deferred (C2): this is also the LIFE_LOST emit point, mirroring
        apply_life_gain's LIFE_GAINED gate (slice 1). Mindcrank ("Whenever an
        opponent loses life, that player mills that many cards") had no
        dispatcher of any kind; its own printed reminder — "(Damage causes loss
        of life.)" — is why the emit belongs HERE rather than at a damage
        funnel: damage and non-damage life loss both converge on this call, so
        one emit covers both without double-counting.

        `game` is optional and a site that cannot supply it simply does not
        emit. That is an undercount, which is the safe direction, and it keeps
        every existing caller working unchanged.
        """
        try:
            amount = max(0, int(amount))
        except (TypeError, ValueError):
            return
        if amount <= 0:
            return
        self.life_lost_this_turn += amount
        if game is not None:
            from mtg import events
            events.emit(events.LIFE_LOST, game, player=self, amount=amount,
                        source_name=source_name)
    
    def get_zone(self, zone: Zone) -> List[Card]:
        """Get cards in a specific zone."""
        return {
            Zone.LIBRARY: self.library,
            Zone.HAND: self.hand,
            Zone.BATTLEFIELD: self.battlefield,
            Zone.GRAVEYARD: self.graveyard,
            Zone.EXILE: self.exile,
            Zone.COMMAND: self.command_zone,
        }.get(zone, [])
    
    def find_card(self, name_or_id, zone: Zone = None) -> Optional[Card]:
        """Find a card by name or ID, optionally in a specific zone.

        Handles P/T annotations like 'Insect(1/1)' that Claude's AI sometimes
        includes — strips trailing '(N/N)' before matching.

        Coerces list-valued names (AI sometimes returns `target: [A, B]` for
        multi-target spells) to the first element; None returns None.
        """
        if name_or_id is None:
            return None
        if isinstance(name_or_id, (list, tuple)):
            if not name_or_id:
                return None
            name_or_id = name_or_id[0]
        if not isinstance(name_or_id, str):
            name_or_id = str(name_or_id)
        zones = [zone] if zone else list(Zone)
        name_lower = name_or_id.lower().strip()

        # Strip trailing parenthetical: "Insect(1/1)" → "insect", "Plant(2)" → "plant"
        # Matches P/T like (1/1) AND index annotations like (1), (2) that Claude
        # generates to disambiguate duplicate-named tokens
        import re as _re
        name_lower = _re.sub(r'\s*\([^)]*\)\s*$', '', name_lower)

        for z in zones:
            for card in self.get_zone(z):
                # Skip phased-out permanents on battlefield (they don't exist per CR 702.26)
                if z == Zone.BATTLEFIELD and getattr(card, '_phased_out', False):
                    continue
                if card.id == name_or_id or card.name.lower() == name_lower:
                    return card
        return None
    
    def _is_active(self, card: Card) -> bool:
        """Check if a permanent is active (not phased out).
        Phased-out permanents are treated as though they don't exist per CR 702.26."""
        return not getattr(card, '_phased_out', False)

    def active_battlefield(self) -> List[Card]:
        """Get all active (non-phased-out) permanents on battlefield."""
        return [c for c in self.battlefield if self._is_active(c)]

    def creatures(self, game=None) -> List[Card]:
        """Get active creatures on battlefield (excludes phased-out).

        May 25 audit (F24): pass `game` through to is_creature so devotion-
        gated Theros gods (Erebos isn't a creature unless devotion ≥5) are
        correctly excluded. Callers that have a game reference SHOULD pass
        it; the default `game=None` keeps backward compat with call sites
        that only want a type-line filter.
        """
        return [c for c in self.battlefield if c.is_creature(game=game) and self._is_active(c)]

    def untapped_creatures(self, game=None) -> List[Card]:
        """Get untapped active creatures on battlefield."""
        return [c for c in self.creatures(game=game) if not c.tapped]

    def lands(self) -> List[Card]:
        """Get active lands on battlefield (excludes phased-out)."""
        return [c for c in self.battlefield if c.is_land() and self._is_active(c)]

    def untapped_lands(self) -> List[Card]:
        """Get untapped active lands on battlefield."""
        return [c for c in self.lands() if not c.tapped]

    def untapped_mana_sources(self) -> List[Card]:
        """Get all untapped active permanents that can produce mana (lands + mana rocks)."""
        sources = []
        for card in self.battlefield:
            if card.tapped or not self._is_active(card):
                continue
            # CR 302.6: a creature's {T} mana ability can't be activated while it's
            # summoning sick (no haste). Lands/artifacts aren't creatures and tap
            # the turn they enter, so this only filters creature mana dorks. May 30
            # audit: this was the [MANA-DIVERGENCE] green-dork loop — a summoning-
            # sick Llanowar Elves / Birds of Paradise counted as available mana, so
            # the AI planned green spells it couldn't pay, got rejected, re-proposed,
            # and eventually permanent-banned them (4 games last batch).
            if card.is_creature() and card.summoning_sick and not card.has_haste():
                continue
            if card.is_land() or self._can_produce_mana(card):
                sources.append(card)
        return sources
    
    def _can_produce_mana(self, card: Card) -> bool:
        """Check if a card can produce mana (based on oracle text or known cards)."""
        # Known mana rocks
        mana_rocks = {
            'sol ring', 'mana crypt', 'mana vault', 'grim monolith',
            'chrome mox', 'mox diamond', 'mox opal', 'mox amber',
            'arcane signet', 'fellwar stone', 'mind stone', 'thought vessel',
            'thran dynamo', 'gilded lotus', 'coalition relic', 'basalt monolith',
            'worn powerstone', 'hedron archive', 'everflowing chalice',
            'commander\'s sphere', 'darksteel ingot', 'cultivator\'s caravan',
            'honor-worn shaku', 'sisay\'s ring', 'ur-golem\'s eye',
            'fire diamond', 'sky diamond', 'moss diamond', 'charcoal diamond', 'marble diamond',
            'talisman of creativity', 'talisman of dominance', 'talisman of progress',
            'talisman of indulgence', 'talisman of impulse', 'talisman of unity',
            'talisman of hierarchy', 'talisman of conviction', 'talisman of resilience', 'talisman of curiosity',
            'signet', 'locket', 'cluestone',  # Partial matches
        }
        
        name_lower = card.name.lower()

        # Aug 8 (queue R4, found by the R4 pin's first run): a PLANESWALKER
        # whose LOYALTY text says "Add {R}{R}{R}" matched the oracle scan
        # below and became an auto-tappable mana source — the tap engine
        # tapped Jaya Ballard and minted phantom mana (CR 601.2g), and the
        # advertisement counted her. Loyalty abilities are once-per-turn,
        # sorcery-speed activations the payment engine may never auto-tap.
        # Live phantom sources: Jaya Ballard, Chandra Torch of Defiance,
        # Chandra Flameshaper, Domri Anarch of Bolas (all mythic-deck).
        # Their mana arrives ONLY via the explicit PW activation path,
        # which routes restrictions correctly.
        if 'planeswalker' in (card.type_line or '').lower():
            return False

        # Check known mana rocks
        for rock in mana_rocks:
            if rock in name_lower:
                return True

        # Check oracle text for mana abilities
        if card.oracle_text:
            text_lower = card.oracle_text.lower()
            # Look for "{T}: Add" patterns (mana abilities)
            if '{t}: add' in text_lower or '{t}, ' in text_lower and 'add' in text_lower:
                return True
            if 'add {' in text_lower and ('mana' in text_lower or '}' in text_lower):
                return True
            # Vivid (Bloom Tender, Faeburrow Elder): "{T}: For each color
            # among permanents you control, add one mana of that color."
            # None of the checks above see it — the ability names no mana
            # SYMBOL, so there is no "add {" and no "{t}: add". Bloom Tender
            # was therefore absent from untapped_mana_sources entirely and
            # contributed ZERO mana, in the four-colour deck it ships in.
            if self._produces_all_colors_at_once(card):
                return True

        return False

    def _produces_all_colors_at_once(self, card) -> bool:
        """Is this a Vivid-style source whose ONE tap yields every colour it
        lists SIMULTANEOUSLY, rather than a choice between them?

        This is the distinction the production dict cannot express on its
        own: `{'W': 1, 'U': 1}` means "two mana, one of each" for an Azorius
        Signet and "one mana, W or U" for a dual land. Everything downstream
        assumes the exclusive (dual) reading, which is the conservative one.
        This predicate is the ONLY thing that flips it, so it must be exact:
        see `is_vivid_mana_line` for why both halves of the ability are
        required on one line, and what testing the count alone did.
        """
        return is_vivid_mana_line(getattr(card, 'oracle_text', '') or '')

    def _one_tap_output(self, production: Dict[str, int], card=None) -> int:
        """How much mana ONE tap of this source actually yields.

        June 10 deep-dive (dual-land underpay): a source's one-tap output is
        the MAX over its colour options, not the sum — "Add {W} or {U}" duals
        were counted as 2 available mana and the commitment accounting
        credited both colours from one tap, so spells resolved UNDERPAID by
        (colours-1) per dual (CR 601.2g). Single-key sources (basics, Sol
        Ring {'C': 2}) are unchanged.

        The `card` argument is optional and defaults to the old behaviour, so
        callers that only hold a production dict are bit-identical. Passing
        the card lets a Vivid source (see `_produces_all_colors_at_once`) sum
        instead — its colours genuinely arrive together. Both-at-once
        multi-colour producers WITHOUT that wording ("Add {B}{R}") remain
        indistinguishable in this model and still under-count by design.
        """
        if not production:
            return 0
        if len(production) == 1:
            return next(iter(production.values()))
        if card is not None and self._produces_all_colors_at_once(card):
            return sum(production.values())
        return max(production.values())
    
    def _is_fetch_land(self, card: Card) -> bool:
        """Check if a card is a fetch land (sacrifice to search for a land).

        Fetch lands don't produce mana — they sacrifice to search for a land.
        They should NOT be counted as mana sources.
        """
        # Known fetch lands by name
        fetch_names = {
            'flooded strand', 'polluted delta', 'bloodstained mire',
            'wooded foothills', 'windswept heath', 'marsh flats',
            'scalding tarn', 'verdant catacombs', 'arid mesa',
            'misty rainforest',
            # Mirage slow fetches
            'bad river', 'flood plain', 'grasslands', 'mountain valley', 'rocky tar pit',
            # Panoramas
            'bant panorama', 'esper panorama', 'grixis panorama',
            'jund panorama', 'naya panorama',
            # Other search-for-land cards
            'terramorphic expanse', 'evolving wilds', 'fabled passage',
            'prismatic vista', 'myriad landscape',
            # Krosan Verge and similar
            'krosan verge', 'blighted woodland',
        }
        name_lower = card.name.lower()
        for fetch in fetch_names:
            if fetch in name_lower:
                return True

        # Oracle text pattern: "sacrifice ~: search your library for a ... land"
        if card.oracle_text:
            text_lower = card.oracle_text.lower()
            if ('sacrifice' in text_lower and 'search your library' in text_lower
                    and ('land' in text_lower or 'forest' in text_lower or 'island' in text_lower
                         or 'plains' in text_lower or 'swamp' in text_lower or 'mountain' in text_lower)):
                # It's a fetch-style land — check it doesn't also tap for mana
                # (Some lands like Krosan Verge both tap for mana AND sacrifice)
                if '{t}: add' in text_lower:
                    return False  # It's a land that taps for mana AND has a sacrifice ability
                return True

        return False

    def _all_lands_are_all_basic_types(self) -> bool:
        """Does this player control a "lands you control are every basic land
        type" effect (Dryad of the Ilysian Grove, Prismatic Omen)?

        Matched on the PHRASE rather than the card name so both cards — and any
        future one with the same wording — are covered, and so a name-substring
        misfire of the Coldsteel-Heart kind can't happen.
        """
        for permanent in self.battlefield:
            if getattr(permanent, '_phased_out', False):
                continue
            oracle = (getattr(permanent, 'oracle_text', '') or '').lower()
            if 'are every basic land type' in oracle:
                return True
        return False

    def _get_mana_production(self, card: Card) -> Dict[str, int]:
        """
        Get what mana a card produces when tapped.
        Returns dict like {'C': 2} for Ancient Tomb or {'R': 1} for Mountain.
        'any' key means can produce any color (Command Tower, etc.)

        Aug 7 queue item Q3: while _spend_snow_as_any is set (paying for a
        Draugr-permitted card), a SNOW source reports its one-tap output as
        'any' — the printed "spend snow mana as though it were mana of any
        color to cast it". One transform point, so every internal phase of
        the payment engine (strict pips, hybrid caps, generic, one-tap
        totals) inherits it consistently.
        """
        name_lower = card.name.lower()
        if getattr(self, '_spend_snow_as_any', False):
            _tl = getattr(card, 'type_line', '') or ''
            if 'Snow' in _tl:
                self._spend_snow_as_any = False  # avoid recursion below
                try:
                    _base = self._get_mana_production(card)
                finally:
                    self._spend_snow_as_any = True
                _out = self._one_tap_output(_base, card) if _base else 0
                if _out > 0:
                    return {'any': _out}
                return _base

        # === FETCH LANDS — produce NO mana ===
        if card.is_land() and self._is_fetch_land(card):
            return {'C': 0}

        # === SPECIAL LANDS ===
        # Lands that produce 2+ mana
        if 'ancient tomb' in name_lower:
            return {'C': 2}  # Damage applied in tap_lands_for_mana via _get_mana_tap_damage
        if 'city of traitors' in name_lower:
            return {'C': 2}
        if 'crystal vein' in name_lower:
            return {'C': 2}  # Sacrifices but produces 2
        if 'temple of the false god' in name_lower:
            # Only works with 5+ lands
            if len(self.lands()) >= 5:
                return {'C': 2}
            return {'C': 0}  # Can't tap for mana yet
        
        # Dynamic mana lands - count permanents!
        if 'gaea\'s cradle' in name_lower:
            creature_count = len(self.creatures())
            return {'G': creature_count} if creature_count > 0 else {'G': 0}
        if 'serra\'s sanctum' in name_lower:
            enchantment_count = len([c for c in self.active_battlefield() if c.is_enchantment()])
            return {'W': enchantment_count} if enchantment_count > 0 else {'W': 0}
        if 'tolarian academy' in name_lower:
            artifact_count = len([c for c in self.active_battlefield() if c.is_artifact()])
            return {'U': artifact_count} if artifact_count > 0 else {'U': 0}
        if 'cabal coffers' in name_lower:
            swamp_count = len([c for c in self.lands() if 'swamp' in c.name.lower()])
            return {'B': swamp_count} if swamp_count > 0 else {'B': 0}
        if 'nykthos' in name_lower:
            # Devotion is complex - approximate as 2 for now
            return {'C': 2}
        if 'itlimoc' in name_lower or 'growing rites' in name_lower:
            creature_count = len(self.creatures())
            return {'G': creature_count} if creature_count > 0 else {'G': 0}
            
        # === "Lands you control are every basic land type" ===
        # Dryad of the Ilysian Grove, Prismatic Omen. Only the extra-land-drop
        # half of Dryad was implemented; the type-adding half did not exist
        # anywhere, and nothing could have consumed it if it had — mana
        # production is derived per card with no static-effect consultation.
        # In a multicolour deck, that static ability is often the point of the card:
        # the card: every land taps for every colour, and without it the
        # castable list and can_pay_mana_cost reject casts that are legal on the
        # real board.
        #
        # Placed AFTER the special/dynamic lands above so Ancient Tomb keeps
        # {C:2}, Gaea's Cradle keeps its count, and fetchlands keep producing
        # nothing — a land that taps for something unusual is not suddenly a
        # one-mana rainbow. Scoped by `self.battlefield` because the effect
        # reads "lands YOU control", so an opponent's Dryad correctly does
        # nothing for us.
        if card.is_land() and self._all_lands_are_all_basic_types():
            return {'any': 1}

        # Lands that produce any color
        if 'command tower' in name_lower:
            return {'any': 1}
        if 'city of brass' in name_lower:
            return {'any': 1}
        if 'mana confluence' in name_lower:
            return {'any': 1}
        if 'reflecting pool' in name_lower:
            # Reflecting Pool does not intrinsically make any colour. It can
            # make one mana of a type another land we control could produce;
            # alone (or beside only other Pools) it makes nothing. Do not
            # recurse through another Pool, and inspect lands regardless of
            # tapped state because "could produce" is about their abilities,
            # not whether their costs can currently be paid.
            reflected = set()
            for other in self.lands():
                if other is card or 'reflecting pool' in other.name.lower():
                    continue
                production = self._get_mana_production(other)
                if production.get('any', 0) > 0:
                    return {'any': 1}
                reflected.update(
                    color for color, amount in production.items()
                    if color in 'WUBRGC' and amount > 0)
            if reflected:
                return {color: 1 for color in sorted(reflected)}
            return {'C': 0}
        if 'exotic orchard' in name_lower:
            return {'any': 1}
        if 'forbidden orchard' in name_lower:
            return {'any': 1}
        if 'gemstone mine' in name_lower:
            return {'any': 1}
        if 'tarnished citadel' in name_lower:
            return {'any': 1}
        if 'undiscovered paradise' in name_lower:
            return {'any': 1}
        
        # === MANA ROCKS ===
        if 'sol ring' in name_lower:
            return {'C': 2}
        if 'mana crypt' in name_lower:
            return {'C': 2}
        if 'mana vault' in name_lower:
            return {'C': 3}
        if 'grim monolith' in name_lower:
            return {'C': 3}
        if 'thran dynamo' in name_lower:
            return {'C': 3}
        if 'gilded lotus' in name_lower:
            return {'any': 3}
        if 'basalt monolith' in name_lower:
            return {'C': 3}
        if 'worn powerstone' in name_lower:
            return {'C': 2}
        if 'hedron archive' in name_lower:
            return {'C': 2}
        if 'mind stone' in name_lower:
            return {'C': 1}
        if 'thought vessel' in name_lower:
            return {'C': 1}
        if 'arcane signet' in name_lower:
            return {'any': 1}
        if 'commander\'s sphere' in name_lower:
            return {'any': 1}
        if 'chromatic lantern' in name_lower:
            return {'any': 1}
        if 'coalition relic' in name_lower:
            return {'any': 1}
        if 'fellwar stone' in name_lower:
            return {'any': 1}
        if 'darksteel ingot' in name_lower:
            return {'any': 1}
        
        # Signets produce 2 colors
        if 'signet' in name_lower:
            if 'azorius' in name_lower: return {'W': 1, 'U': 1}  # Filter, but approximate
            if 'dimir' in name_lower: return {'U': 1, 'B': 1}
            if 'rakdos' in name_lower: return {'B': 1, 'R': 1}
            if 'gruul' in name_lower: return {'R': 1, 'G': 1}
            if 'selesnya' in name_lower: return {'G': 1, 'W': 1}
            if 'orzhov' in name_lower: return {'W': 1, 'B': 1}
            if 'izzet' in name_lower: return {'U': 1, 'R': 1}
            if 'golgari' in name_lower: return {'B': 1, 'G': 1}
            if 'boros' in name_lower: return {'R': 1, 'W': 1}
            if 'simic' in name_lower: return {'G': 1, 'U': 1}
            return {'C': 1}  # Generic signet
        
        # Talismans (produce 2 colors with life payment)
        if 'talisman' in name_lower:
            return {'any': 1}  # Simplify - can produce either of 2 colors
        
        # Diamonds
        if 'fire diamond' in name_lower: return {'R': 1}
        if 'sky diamond' in name_lower: return {'U': 1}
        if 'moss diamond' in name_lower: return {'G': 1}
        if 'charcoal diamond' in name_lower: return {'B': 1}
        if 'marble diamond' in name_lower: return {'W': 1}
        
        # Everflowing Chalice: colorless equal to charge counters
        if 'everflowing chalice' in name_lower:
            _ch = card.counters.get('charge', 0) if hasattr(card, 'counters') else 0
            return {'C': max(_ch, 0)}
        # === MANA DORKS ===
        # Vivid (Bloom Tender, Faeburrow Elder): "{T}: For each color among
        # permanents you control, add one mana of that color." Phrase-matched
        # so both cards — and any reprint that drops the "Vivid —" ability
        # word, as Faeburrow Elder's printing does — are covered.
        #
        # The old entry here was `name_lower == 'bloom tender' -> {'any': 1}`,
        # which was DEAD (the card never reached untapped_mana_sources, so
        # this branch was unreachable) and would have been wrong if it had
        # run: 'any' claims a colour the card cannot make, and a mono-green
        # board would have advertised blue mana that the tap then fabricated.
        # The true set is the colours actually present, which is also why
        # basics contribute nothing — they are colourless (CR 202.2).
        #
        # `is_vivid_mana_line`, not the bare colour-count phrase: this branch
        # sits ABOVE the oracle-text Add-line scan below, so a loose match
        # here INTERCEPTS cards that scan already handled correctly. Chromatic
        # Orrery is the case that proves it — a real {T}: Add {C}{C}{C}{C}{C}
        # rock that also happens to count colours on another line.
        if is_vivid_mana_line(card.oracle_text or ''):
            _vivid = colors_among_permanents(self)
            return {c: 1 for c in sorted(_vivid)} if _vivid else {'C': 0}
        if name_lower in ('llanowar elves', 'elvish mystic', 'fyndhorn elves'): return {'G': 1}
        if name_lower == 'birds of paradise': return {'any': 1}
        if name_lower == 'noble hierarch': return {'any': 1}
        if name_lower == 'elves of deep shadow': return {'B': 1}
        if name_lower == "avacyn's pilgrim": return {'W': 1}
        if name_lower == 'deathrite shaman': return {'any': 1}
        if name_lower == 'priest of titania':
            _ec = len([c for c in self.creatures() if 'elf' in (c.type_line or '').lower()])
            return {'G': max(_ec, 1)}
        # === MDFC PATHWAY LANDS ===
        mdfc_info = get_mdfc_info(card.name)
        if mdfc_info:
            if card.played_face == "back":
                return {mdfc_info["back_produces"]: 1}
            else:
                # Default to front face if not tracked
                return {mdfc_info["front_produces"]: 1}
        
        # === BASIC LAND TYPES ===
        # Original duals, shocks, and typed triomes have more than one basic
        # land subtype. Name-first matching made Tropical/Volcanic Island stop
        # at Island and silently lose their second colour. CR 305.6 grants the
        # intrinsic mana ability for every printed basic land type.
        if card.is_land():
            type_lower = (card.type_line or '').lower()
            typed_colors = {}
            for subtype, color in (
                    ('plains', 'W'), ('island', 'U'), ('swamp', 'B'),
                    ('mountain', 'R'), ('forest', 'G')):
                if subtype in type_lower:
                    typed_colors[color] = 1
            if typed_colors:
                return typed_colors

        # === BASIC LANDS (defensive fallback for sparse hand-built cards) ===
        if 'plains' in name_lower:
            return {'W': 1}
        if 'island' in name_lower:
            return {'U': 1}
        if 'swamp' in name_lower:
            return {'B': 1}
        if 'mountain' in name_lower:
            return {'R': 1}
        if 'forest' in name_lower:
            return {'G': 1}
        
        # === ARTIFACT LANDS ===
        if 'great furnace' in name_lower:
            return {'R': 1}
        if 'seat of the synod' in name_lower:
            return {'U': 1}
        if 'vault of whispers' in name_lower:
            return {'B': 1}
        if 'ancient den' in name_lower:
            return {'W': 1}
        if 'tree of tales' in name_lower:
            return {'G': 1}
        if 'darksteel citadel' in name_lower:
            return {'C': 1}
        
        # === SAC-COST MANA LANDS ===
        # Phyrexian Tower: {T}: Add {C}. {T}, Sacrifice a creature: Add {B}{B}.
        # When a non-Tower creature is on the battlefield, the sac path is
        # the strictly better option (2 black mana vs 1 colorless), so we
        # report the upgraded total. May 16 audit: previously the oracle-text
        # scan returned {C: 1, B: 1} for the Tower, missing the BB upgrade
        # AND not gating on sac availability (it reported B mana even when
        # the player had no creatures to sac, breaking mana payment).
        if 'phyrexian tower' in name_lower:
            sac_targets = [c for c in self.creatures()
                            if c.id != card.id and not getattr(c, '_phased_out', False)]
            if sac_targets:
                return {'B': 2}  # Better option when a sac target exists
            return {'C': 1}
        # Diamond Valley: {T}, Sacrifice a creature: You gain life equal to
        # the sacrificed creature's toughness. Not a mana ability — skip.
        # (Included here as a comment so future audits don't add it as
        # "mana from sac creatures".)

        # === DUAL/FETCH/SHOCK LANDS ===
        # Check oracle text for what colors it produces
        # IMPORTANT: Only scan lines that contain "Add" — otherwise we pick up
        # color symbols from activation COSTS (e.g., Academy Ruins' "{1}{U}" cost
        # was being confused with mana production).
        if card.oracle_text:
            colors_found = {}
            for line in card.oracle_text.split('\n'):
                line_lower = line.lower()
                # Only look at mana-production lines (contain "add")
                if 'add' not in line_lower:
                    continue
                if '{w}' in line_lower or 'white' in line_lower: colors_found['W'] = 1
                if '{u}' in line_lower or 'blue' in line_lower: colors_found['U'] = 1
                if '{b}' in line_lower or 'black' in line_lower: colors_found['B'] = 1
                if '{r}' in line_lower or 'red' in line_lower: colors_found['R'] = 1
                if '{g}' in line_lower or 'green' in line_lower: colors_found['G'] = 1
                if '{c}' in line_lower: colors_found['C'] = 1
                if 'any color' in line_lower or 'any one color' in line_lower:
                    colors_found['any'] = 1
            if colors_found:
                return colors_found

        # Default: colorless
        return {'C': 1}
    
    def available_mana(self) -> int:
        """Count total available mana (untapped mana sources + mana pool)."""
        pool_total = sum(self.mana_pool.values())
        
        # Count mana from all untapped mana sources
        source_total = 0
        for card in self.untapped_mana_sources():
            production = self._get_mana_production(card)
            source_total += sum(production.values())
        
        return pool_total + source_total
    
    def _set_snow_waiver(self, spending_card=None) -> None:
        """Aug 7 Q3 adversarial review (#1/#6): the snow-as-any waiver is
        derived at the entry of EVERY payment/advertising function from that
        call's own spending_card — a stale True can never leak into
        advertising, plan validation, or tap_lands_for_mana (the reviewer
        reproduced fabricated {G} off a Snow-Covered Plains from exactly
        that leak). The permission cross-check (#6) stops a card that
        drifted into its OWNER's hand from paying with its opponent's
        waiver: the stamp names who may cast it.
        """
        self._spend_snow_as_any = bool(
            spending_card is not None
            and getattr(spending_card, '_snow_as_any_color', False)
            and getattr(spending_card, '_castable_by_player', None) == self.name)

    def one_tap_mana_total(self, spending_card=None) -> int:
        """Physical mana ceiling: floating pool + ONE tap of each untapped
        source (a source's single-tap output is max over its options — an
        OR-dual is 1, Sol Ring is 2).

        `available_mana()` sums EVERY color an OR-dual can produce (Sacred
        Foundry counts 2), a fine per-color capacity but an overstated
        TOTAL — a source taps once. July 31 batch-11: X auto-sizing read
        available_mana() and sized Volcanic Geyser to X=6 (total 8) on a
        7-physical-source board; the tap engine correctly refused and the
        batch's only Geyser cast was lost (game_1532536791742025739).
        Anything budgeting a TOTAL (X-sizing, the can_pay one-tap gate)
        must use this instead.
        """
        self._set_snow_waiver(spending_card)  # Q3 review #1: per-entry derive
        total = sum(self.mana_pool.values())
        total += sum(self._restricted_mana_available(spending_card).values())
        for src in self.untapped_mana_sources():
            prod = self._get_mana_production(src)
            if prod:
                # Shared with the payment engine's own accounting — a
                # re-expressed copy of this rule here is how the ceiling and
                # the tap could silently disagree.
                total += self._one_tap_output(prod, src)
        return total

    def available_mana_detailed(self, spending_card=None) -> Dict[str, int]:
        """Get detailed available mana by color including 'any' for flexible sources."""
        self._set_snow_waiver(spending_card)  # Q3 review #1: per-entry derive
        available = dict(self.mana_pool)  # Start with pool
        for color, amount in self._restricted_mana_available(spending_card).items():
            available[color] = available.get(color, 0) + amount

        any_mana = 0
        
        for card in self.untapped_mana_sources():
            production = self._get_mana_production(card)
            for color, amount in production.items():
                if color == 'any':
                    any_mana += amount
                else:
                    available[color] = available.get(color, 0) + amount
        
        available['any'] = any_mana  # Track flexible mana separately
        return available
    
    def empty_mana_pool(self):
        """Empty mana pool (happens at end of each phase)."""
        self.mana_pool = {'W': 0, 'U': 0, 'B': 0, 'R': 0, 'G': 0, 'C': 0}
        self.restricted_mana_pool = []
        # Q4: snow tags describe pool contents — they die with the pool.
        self._pool_snow = {}

    def add_mana(self, colors: str):
        """Add mana to pool. colors like 'WU' or 'RRR' or 'C'."""
        for c in colors.upper():
            if c in self.mana_pool:
                self.mana_pool[c] += 1

    # ---- Q4 (Aug 7, 2026): snow provenance for floating pool mana ----

    @staticmethod
    def _is_snow_source(card) -> bool:
        """Snow-source detection for the Q4 pool-provenance producers.

        Same shape as the payment engine's consumed-snow attribution walk
        (name in rules.mana.SNOW_LANDS, or 'snow' in the type line,
        case-insensitive — Scryfall prints the supertype capitalized, so
        the case-insensitive test is the superset the walk already uses).
        """
        try:
            from rules.mana import SNOW_LANDS as _SNOW_LANDS
        except ImportError:
            _SNOW_LANDS = set()
        name = getattr(card, 'name', None)
        type_line = getattr(card, 'type_line', '') or ''
        return bool((name and name in _SNOW_LANDS)
                    or 'snow' in type_line.lower())

    def credit_pool_snow(self, color: str, amount: int) -> None:
        """Tag `amount` of the pool's `color` mana as verified snow.

        Clamped so the tag can never exceed what the pool actually holds
        for that color (the Q4 hard rule) — call AFTER the pool add.
        """
        if amount <= 0 or color not in self.mana_pool:
            return
        self._pool_snow[color] = min(
            self._pool_snow.get(color, 0) + int(amount),
            self.mana_pool.get(color, 0))

    def debit_pool_snow(self, color: str, amount: int) -> int:
        """Consume snow tags for `amount` of `color` spent from the pool.

        Returns the verified-snow portion (≤ amount). Untagged pool mana
        spends as non-snow — the documented undercount direction.
        """
        if amount <= 0:
            return 0
        take = min(self._pool_snow.get(color, 0), int(amount))
        if take > 0:
            self._pool_snow[color] -= take
        return take

    def grant_pool_mana(self, color: str, amount: int = 1,
                        source=None) -> None:
        """Add floating mana to the pool, tagging snow provenance (Q4).

        The sanctioned pool-add for activation/effect paths (the
        [ACTIVATE-MANA] sites, the Tier-3 add_mana action). Marks snow
        only when `source` is a snow permanent — a caller with no source
        in hand adds untagged mana, which undercounts (safe).
        """
        color = (color or 'C').upper()
        if color not in self.mana_pool or amount <= 0:
            return
        self.mana_pool[color] += int(amount)
        if source is not None and self._is_snow_source(source):
            self.credit_pool_snow(color, int(amount))

    def add_restricted_mana(self, color: str, amount: int,
                            restriction: str, source: str = "") -> None:
        """Add mana that may be spent only when its restriction allows."""
        color = (color or 'C').upper()
        if color not in self.mana_pool or amount <= 0:
            return
        self.restricted_mana_pool.append({
            'color': color, 'amount': int(amount),
            'restriction': restriction, 'source': source,
        })

    @staticmethod
    def _restricted_mana_allows(bucket: Dict[str, Any], spending_card=None) -> bool:
        """Does this restricted-mana bucket permit paying for spending_card?

        Aug 8 (queue R4): grew the instant_sorcery / creature predicates —
        the Aug-5 machinery shipped with dragon_spell only, and the OTHER
        two inventory producers (Jaya Ballard, Castle Garenbrig) leaked
        their mana unrestricted because their branches never routed here.
        Unknown keys (the 'unmodeled:' family) fall through to False: the
        mana is HELD, never silently unrestricted. Type-line substring
        tests are safe here — no type LINE carries a "non" prefix (the
        substring-negation family lives in oracle text, not type lines).
        """
        restriction = bucket.get('restriction', '')
        if spending_card is None:
            return False
        type_line = (getattr(spending_card, 'type_line', '') or '').lower()
        oracle = (getattr(spending_card, 'oracle_text', '') or '').lower()
        # Aug 8 (R4 adversarial review #1): an ADVENTURE cast is an
        # instant/sorcery cast (CR 715.2a), but the Card's type_line is the
        # CREATURE face (deck_loader normalizes multi-face lines) — so
        # 'creature' in type_line was True while casting Stomp, and
        # Garenbrig's restricted G would have paid for it. The runtime
        # cast_as_adventure stamp is set before payment at all three
        # executors; the cache's adventure_type field is None and must not
        # be consulted.
        if getattr(spending_card, 'cast_as_adventure', False):
            type_line = 'instant sorcery'
        if restriction == 'dragon_spell':
            return bool(re.search(r'\bdragon\b', type_line) or 'changeling' in oracle)
        if restriction == 'instant_sorcery_spell':
            return 'instant' in type_line or 'sorcery' in type_line
        if restriction == 'creature_spell':
            # Castle Garenbrig's "or activate abilities of creatures" half
            # is deliberately NOT modeled (activation payments carry no
            # spending context) — ability spends hold, the safe direction.
            return 'creature' in type_line
        return False

    def _restricted_mana_available(self, spending_card=None) -> Dict[str, int]:
        available: Dict[str, int] = {}
        for bucket in self.restricted_mana_pool:
            if self._restricted_mana_allows(bucket, spending_card):
                color = bucket.get('color', 'C')
                available[color] = available.get(color, 0) + int(bucket.get('amount', 0))
        return available

    def _spend_restricted_mana(self, color: str, amount: int,
                               spending_card=None) -> int:
        spent = 0
        for bucket in self.restricted_mana_pool:
            if spent >= amount:
                break
            if (bucket.get('color') != color
                    or not self._restricted_mana_allows(bucket, spending_card)):
                continue
            take = min(int(bucket.get('amount', 0)), amount - spent)
            bucket['amount'] = int(bucket.get('amount', 0)) - take
            spent += take
        self.restricted_mana_pool = [
            b for b in self.restricted_mana_pool if int(b.get('amount', 0)) > 0
        ]
        return spent
    
    def _get_mana_tap_damage(self, card: Card, paid_generic: bool = False) -> int:
        """Self-damage dealt when tapping this permanent for mana.

        Aug 10 deferred (H1). This was a hardcoded FOUR-NAME list with no
        oracle path, so the 8 pain lands and 8 Talismans in the inventory
        (Adarkar Wastes, Battlefield Forge, Caves of Koilos, Karplusan Forest,
        Llanowar Wastes, Sulfurous Springs, Underground River, Yavimaya Coast;
        Talisman of Conviction/Curiosity/Dominance/Hierarchy/Impulse/
        Indulgence/Progress/Resilience) all tapped for free.

        SCOPED PER ABILITY LINE, which the record correctly insisted on:
        Talisman of Impulse prints "{T}: Add {C}." and "{T}: Add {R} or {G}.
        This artifact deals 1 damage to you." on SEPARATE lines, so a
        whole-oracle scan charges the free colorless tap too. Only a line that
        both produces mana and deals the damage counts.

        `paid_generic` closes what the Aug 10 note filed as a KNOWN
        LIMITATION. Aug 23 cube-FFA audit: it was outcome-affecting. A
        Talisman of Curiosity was charged 1 damage on all six of its taps,
        every one of them paying a GENERIC portion that its printed free line
        ("{T}: Add {C}.") covers, and those 6 life were exactly the margin
        Bot-Elspeth died by (game_1538942949243621457, turn 44).

        The discriminator is the PHASE, not the committed colour: a pain land
        tapped for generic still gets committed_color set to one of its two
        colours, so colour alone cannot tell "paid a pip" from "paid generic".
        tap_sources_for_cost snapshots tapped_cards at the Phase-3 boundary
        instead, which covers future generic tap sites by construction.

        Deliberately NOT wired into tap_lands_for_mana: that path is
        amount-based and colour-unaware, so it cannot establish that only
        generic was owed. Defaulting to False keeps it byte-identical.

        Mana Confluence is deliberately NOT here: it prints "{T}, Pay 1 life:
        Add one mana of any color" — a life PAYMENT, not damage, so it cannot
        be prevented or doubled and does not belong in a damage helper. It is
        charged as a cost by the callers instead. The old list had it as
        damage; that classification was wrong.
        """
        oracle = getattr(card, 'oracle_text', '') or ''
        lines = [raw.strip() for raw in oracle.split('\n')]
        dmg_idx = None
        dmg_amount = 0
        dmg_is_trigger = False
        for idx, line in enumerate(lines):
            low = line.lower()
            match = re.search(r'deals? (\d+) damage to you', low)
            if not match:
                continue
            # Either the damage rides a mana-producing ability on this same
            # line (Ancient Tomb, the pain lands, the Talismans) or it is a
            # becomes-tapped trigger that fires whatever the tap was for
            # (City of Brass).
            if 'add ' in low or 'becomes tapped' in low:
                dmg_idx = idx
                dmg_amount = int(match.group(1))
                dmg_is_trigger = 'becomes tapped' in low
                break
        if dmg_idx is None:
            return 0
        # City of Brass' damage is a becomes-tapped TRIGGER rather than a
        # rider on one ability, so it fires whichever ability was
        # activated — the free-line escape below must never reach it.
        if dmg_is_trigger or not paid_generic:
            return dmg_amount
        # Only generic was owed. A card printing a SEPARATE damage-free
        # mana ability (every Talisman and pain land: "{T}: Add {C}.")
        # could have used that line, so it takes no damage. Ancient Tomb
        # prints only the one damage-bearing line and still pays.
        for idx, line in enumerate(lines):
            if idx == dmg_idx:
                continue
            low = line.lower()
            if 'add ' in low and 'damage to you' not in low:
                print(f"[MANA-FREE-TAP] {card.name}: paid generic via its "
                      f"damage-free line — {dmg_amount} damage not dealt")
                return 0
        return dmg_amount

    def _get_mana_tap_life_cost(self, card: Card) -> int:
        """Life paid as part of a mana ability's COST (Mana Confluence).

        Aug 10 (H1): split out from _get_mana_tap_damage because the two are
        not interchangeable — a payment can't be prevented, doubled, or
        redirected, and now that protection and Torbran read damage, filing a
        cost as damage is a real mislabel rather than a wording nit.
        """
        oracle = getattr(card, 'oracle_text', '') or ''
        for raw in oracle.split('\n'):
            low = raw.strip().lower()
            if 'add ' not in low:
                continue
            match = re.search(r'pay (\d+) life', low.split(':')[0])
            if match:
                return int(match.group(1))
        return 0

    def _apply_sac_cost_at_tap(self, card: Card, game=None) -> Optional[Card]:
        """For sac-cost mana lands (Phyrexian Tower), pick a creature, move
        it to the graveyard, and return the sacrificed card. Returns None if
        the card isn't a sac-cost land or no valid target exists. May 16
        audit added this to model Phyrexian Tower's {B}{B} cost properly —
        before, the bot would happily tap the Tower for {B}{B} without ever
        sacrificing anything.
        """
        if 'phyrexian tower' not in card.name.lower():
            return None
        # Pick the least-valuable creature: prefer tokens, then lowest CMC,
        # then lowest power. Never sac the Tower itself or phased-out cards.
        sac_targets = [
            c for c in self.creatures()
            if c.id != card.id and not getattr(c, '_phased_out', False)
        ]
        if not sac_targets:
            return None
        sac_targets.sort(key=lambda c: (
            not getattr(c, 'is_token', False),  # tokens first (False sorts before True)
            int(c.cmc or 0),
            int(c.get_effective_power(game) if game and hasattr(c, 'get_effective_power') else (c.power or 0) or 0),
        ))
        victim = sac_targets[0]
        # Move to graveyard. Tokens cease to exist (handled by SBA at the
        # next check); regular creatures go to graveyard.
        try:
            self.battlefield.remove(victim)
        except ValueError:
            return None
        if not getattr(victim, 'is_token', False):
            self.graveyard.append(victim)
        print(f"[PHYREXIAN-TOWER] {self.name} sacrifices {victim.name} to tap Phyrexian Tower for {{B}}{{B}}")
        # Track for dies-trigger emission (Blood Artist, Zulaport, etc.)
        if game is not None:
            try:
                rd = getattr(game, '_recently_died', None)
                if rd is None:
                    rd = []
                    game._recently_died = rd
                rd.append((victim, self))
                # June 10 audit: surface the sacrifice to players — 43 Tower
                # sacs in the June batch had zero Discord lines (Savra, Judith,
                # Syr Konrad just vanished from the board).
                # June 11 audit: initialize the queue if missing — an
                # AttributeError here was swallowed by this try/except and the
                # message never showed (5 invisible Tower sacs incl. Meren in
                # game 1514621789555265558).
                if not hasattr(game, '_pending_messages') or game._pending_messages is None:
                    game._pending_messages = []
                game._pending_messages.append(
                    f"💀 **{self.name}** sacrifices **{victim.name}** to Phyrexian Tower")
                # CR 903.9: a sacrificed commander may go to the command zone
                # instead of the graveyard; autoplay always chooses it. June 11
                # audit: Meren was Tower-sacrificed into the graveyard and
                # vanished for the rest of the game.
                if (getattr(victim, 'is_commander', False)
                        and victim in self.graveyard):
                    self.graveyard.remove(victim)
                    if not hasattr(self, 'command_zone') or self.command_zone is None:
                        self.command_zone = []
                    self.command_zone.append(victim)
                    print(f"[CR-903.9] {victim.name} (commander) redirected graveyard → command zone after Tower sacrifice")
                    game._pending_messages.append(
                        f"👑 **{victim.name}** returns to the command zone (CR 903.9)")
                # June 10 audit: unregister the victim's layer/static effects.
                # Judith was Tower-sacrificed and her +1/+0 anthem stayed
                # registered, applying 32 more times across the rest of the
                # game. Mirror the normal death path's cleanup.
                game._remove_card_layer_effects(victim)
                if hasattr(game, 'unregister_static_effects'):
                    game.unregister_static_effects(victim)
                if hasattr(game, 'recalculate_power_toughness'):
                    game.recalculate_power_toughness()
            except Exception:
                pass

            # May 20 audit (CRITICAL): immediately fire dies-triggers for the
            # sacrificed creature. game_1506623303886966844:1214 sac'd Blood
            # Artist to Phyrexian Tower without firing BA's own dies trigger.
            # Recording to _recently_died is necessary but not sufficient —
            # the trigger needs to actually be dispatched. Hook into the
            # rules engine's sync dies-trigger scan if available, then
            # surface any returned messages via the game's pending-message
            # queue (flushed by the next display pass).
            try:
                rules_engine = getattr(game, '_rules_engine', None)
                # July 21 batch audit: game._rules_engine holds the
                # RulesEngine, but the dies scan lives on the GAME engine —
                # route via engine_ref. (Also: _rules_engine was only ever
                # assigned in tests until today, so this whole branch was
                # dead in live games.)
                _dies_owner = getattr(rules_engine, 'engine_ref', None) or rules_engine
                if _dies_owner and hasattr(_dies_owner, '_check_dies_triggers_sync'):
                    dies_msgs, _unhandled = _dies_owner._check_dies_triggers_sync(
                        game, victim, self
                    )
                    if hasattr(_dies_owner, 'queue_unhandled_dies'):
                        _dies_owner.queue_unhandled_dies(game, victim, self, _unhandled)
                    if dies_msgs:
                        pq = getattr(game, '_pending_messages', None)
                        if pq is None:
                            pq = []
                            game._pending_messages = pq
                        pq.extend(dies_msgs)
                        print(f"[PHYREXIAN-TOWER] Fired {len(dies_msgs)} dies-trigger(s) "
                              f"for {victim.name}")
            except Exception as _dt_err:
                print(f"[PHYREXIAN-TOWER] dies-trigger dispatch failed: {_dt_err}")
            # July 26 batch-7 audit: sacrificing is its own event (CR 701.17),
            # separate from dying — Korvold's "whenever you sacrifice a
            # permanent" never fired for a Tower sacrifice because only the
            # dies scan ran here. Both AI-activation branches in mtg/engine.py
            # and the manual !activate path in mtg/cog.py already fire this;
            # the Tower path was the odd one out. (Same two-divergent-paths
            # class the debugging checklist calls out.)
            try:
                from mtg.actions import _fire_sacrifice_triggers
                rules_engine = getattr(game, '_rules_engine', None)
                if rules_engine is not None:
                    sac_msgs = _fire_sacrifice_triggers(
                        rules_engine, game, self, victim) or []
                    if sac_msgs:
                        pq = getattr(game, '_pending_messages', None)
                        if pq is None:
                            pq = []
                            game._pending_messages = pq
                        pq.extend(sac_msgs)
                        print(f"[PHYREXIAN-TOWER] Fired {len(sac_msgs)} "
                              f"sacrifice-trigger(s) for {victim.name}")
            except (ValueError, KeyError, AttributeError, TypeError, ImportError) as _st_err:
                print(f"[PHYREXIAN-TOWER] sacrifice-trigger dispatch failed: {_st_err}")
                from mtg.util import maybe_reraise
                maybe_reraise(_st_err)

            # Aug 10 audit: the undying / persist / totem-armor / shield
            # death-SAVE chain. This path fired dies-triggers and sacrifice
            # triggers but never ran the save chain, so a Young Wolf fed to
            # Phyrexian Tower was PERMANENTLY lost while an identical Butcher
            # Ghoul dying via SBA returned correctly in the same game
            # (game_1536023914910588968) — and the console line above it
            # claimed "Undying/self-death trigger handled by SBA engine",
            # which is false on this path because the creature never reaches
            # SBA. Fourth sibling of the same family (May 30 spell damage,
            # June 10 single destroy, Aug 3 sacrifice-as-cost).
            try:
                from mtg.sba import apply_death_save_on_sacrifice
                rules_engine = getattr(game, '_rules_engine', None)
                if rules_engine is not None:
                    save_msgs = apply_death_save_on_sacrifice(
                        rules_engine, game, self, victim) or []
                    if save_msgs:
                        pq = getattr(game, '_pending_messages', None)
                        if pq is None:
                            pq = []
                            game._pending_messages = pq
                        pq.extend(save_msgs)
                        print(f"[PHYREXIAN-TOWER] death save returned "
                              f"{victim.name}")
            except (ValueError, KeyError, AttributeError, TypeError, ImportError) as _sv_err:
                print(f"[PHYREXIAN-TOWER] death-save chain failed: {_sv_err}")
                from mtg.util import maybe_reraise
                maybe_reraise(_sv_err)
        return victim

    def _fire_tap_for_mana_bonuses(self, land_card, production: dict) -> None:
        """Mirari's Wake / Zendikar Resurgent: "Whenever you tap a land for
        mana, add one mana of any type that land produced."

        July 30 (batch-8 deferred item): the anthem half has worked since
        forever; this half was unmodeled — a Wake on the battlefield
        changed nothing about mana. The bonus mana FLOATS straight into
        the pool (it is excess by construction — the payment engine never
        counts it toward the cost being paid), so the July 21 Phase-0 pool
        spend picks it up on the next cast this phase.

        Deliberately conservative (the July 21/26 payment-engine lessons):
        - availability advertisement does NOT count the bonus (a Wake
          player under-advertises rather than reopening the OR-dual /
          phantom-pool over-count classes);
        - the color is the tapped land's committed/first produced color
          ("any type that land produced" is the controller's choice —
          matching the produced color is the near-always-right pick);
        - Mana Reflection's "produces twice as much" is a REPLACEMENT
          with different scope (any permanent) and stays unmodeled.
        """
        try:
            if not land_card.is_land():
                return
        except TypeError:
            return
        _watchers = [c.name for c in self.battlefield
                     if 'whenever you tap a land for mana' in
                     (c.oracle_text or '').lower()
                     and 'add one mana' in (c.oracle_text or '').lower()]
        if not _watchers:
            return
        _color = next((pc for pc, pv in production.items() if pv > 0), 'C')
        if _color == 'any':
            _color = 'C'
        _n = len(_watchers)
        self.mana_pool[_color] = self.mana_pool.get(_color, 0) + _n
        print(f"[MANA-BONUS] {', '.join(_watchers)}: +{_n} {{{_color}}} "
              f"floats to {self.name}'s pool (tapped {land_card.name} for mana)")

    def tap_lands_for_mana(self, amount: int, preferred_colors: str = "",
                           game=None) -> bool:
        """
        Tap mana sources to add mana to pool (amount-based, color-unaware).
        Returns True if successful, False if not enough mana.
        Now handles mana rocks and special lands!

        Q3 review #1: this function had NO waiver setter, so a stale
        _spend_snow_as_any from an earlier can_pay put a color into the
        pool the source cannot produce (fabricated {G} off a snow Plains,
        CR 106.1). It takes no spending_card, so the setter always clears.

        For color-aware tapping, use tap_sources_for_cost() instead.
        """
        self._set_snow_waiver(None)  # Q3 review #1: always clears here
        # Calculate how much mana we can produce
        total_available = 0
        sources_with_production = []

        for card in self.untapped_mana_sources():
            production = self._get_mana_production(card)
            mana_amount = sum(production.values())
            sources_with_production.append((card, production, mana_amount))
            total_available += mana_amount

        if total_available < amount:
            return False

        # Sort by mana produced (tap high-producers first for efficiency)
        sources_with_production.sort(key=lambda x: x[2], reverse=True)

        mana_tapped = 0
        for card, production, mana_amount in sources_with_production:
            if mana_tapped >= amount:
                break

            card.tapped = True

            # Apply self-damage for painful lands (Ancient Tomb, etc.)
            # Aug 10 (H1): Mana Confluence prints "{T}, Pay 1 life:",
            # a COST rather than damage. Charged here so the life total
            # stays right while the damage helper stops mislabelling it
            # (a payment cannot be prevented, doubled or redirected, and
            # protection and Torbran both read damage now).
            tap_life = self._get_mana_tap_life_cost(card)
            if tap_life > 0:
                self.life -= tap_life
                self.record_life_loss(tap_life, game=game)
                print(f"[MANA-COST] {card.name}: {self.name} pays {tap_life} life (life: {max(0, self.life)})")
                self._pending_tap_damage_msgs.append(
                    f"💧 {card.name}: {self.name} pays {tap_life} life (life: {max(0, self.life)})")
            tap_damage = self._get_mana_tap_damage(card)
            if tap_damage > 0:
                self.life -= tap_damage
                self.record_life_loss(tap_damage)
                print(f"[MANA-DAMAGE] {card.name} deals {tap_damage} damage to {self.name} (life: {max(0, self.life)})")
                self._pending_tap_damage_msgs.append(
                    f"🩸 {card.name} deals {tap_damage} damage to {self.name} (life: {max(0, self.life)})")

            # Apply sac cost for sac-mana lands (Phyrexian Tower). If
            # production reports {B: 2} but no sac target now exists, fall
            # back to the {C: 1} ability — sac couldn't be paid.
            if 'phyrexian tower' in card.name.lower() and production.get('B', 0) >= 2:
                victim = self._apply_sac_cost_at_tap(card, game)
                if victim is None:
                    # Couldn't sac — degrade production to {C: 1}
                    production = {'C': 1}
                    mana_amount = 1

            # Add mana to pool based on what the card produces.
            # Q4: a snow source's contribution is tagged via a pool
            # before/after delta — immune to _add_production_to_pool's
            # internal 'any'-color branching. The Wake bonus below stays
            # OUTSIDE the delta window: bonus mana is from the Wake, not
            # the land, so it is deliberately untagged (non-snow).
            if self._is_snow_source(card):
                _pre_pool = dict(self.mana_pool)
                self._add_production_to_pool(production, preferred_colors)
                for _sc, _sv in self.mana_pool.items():
                    _gain = _sv - _pre_pool.get(_sc, 0)
                    if _gain > 0:
                        self.credit_pool_snow(_sc, _gain)
            else:
                self._add_production_to_pool(production, preferred_colors)
            # July 30: Mirari's Wake-class tap bonus (second producer site).
            self._fire_tap_for_mana_bonuses(card, production)

            mana_tapped += mana_amount

        return True

    def tap_sources_for_cost(self, mana_cost_str: str, additional_generic: int = 0,
                             x_value: int = 0, pay_phyrexian_with_life: bool = False,
                             game=None, spending_card=None) -> bool:
        """
        [MANA-ENGINE] Color-aware mana tapping using ManaCost parser.

        Taps the optimal set of mana sources to pay a spell's cost:
        1. Identifies colored requirements (e.g., {1}{W}{U} needs 1W, 1U, 1 any)
        2. Taps sources that produce needed colors FIRST
        3. Then taps remaining sources for generic mana
        4. Handles Phyrexian mana (pay life instead of color)

        Args:
            mana_cost_str: Scryfall mana cost string, e.g. "{2}{W}{U}"
            additional_generic: Extra generic cost (commander tax, etc.)
            x_value: Value chosen for X in X-cost spells
            pay_phyrexian_with_life: If True, pay Phyrexian mana with life when possible

        Returns True if payment succeeded, False if not enough mana.
        """
        # Per-payment provenance consumed by cards such as Blood on the Snow.
        # Q4 (Aug 7, 2026): floating pool mana now carries snow tags
        # (_pool_snow) — Phase-4's pool spend consumes them below. Untagged
        # pool mana still counts as non-snow rather than fabricating
        # provenance (the documented undercount direction).
        self._last_payment = {'snow_spent': 0}
        # Aug 7 (Q3): Draugr's snow-as-any-color permission, scoped to this
        # payment — the shared setter derives per-entry and cross-checks
        # the permission against the caster (adversarial review #1/#6).
        self._set_snow_waiver(spending_card)
        if not mana_cost_str and additional_generic <= 0 and x_value <= 0:
            return True  # Free spell

        if not HAS_MANA_ENGINE:
            # Fallback to amount-based tapping
            total = additional_generic
            if mana_cost_str:
                symbols = re.findall(r'\{([^}]+)\}', mana_cost_str)
                for sym in symbols:
                    if sym.isdigit():
                        total += int(sym)
                    elif sym.upper() == 'X':
                        total += x_value
                    else:
                        total += 1
            return self.tap_lands_for_mana(total, game=game)

        parsed = ManaCost.parse(mana_cost_str)

        # Handle Phyrexian mana: pay life instead of color when requested
        phyrexian_life_cost = 0
        phyrexian_symbols_paid = set()
        if pay_phyrexian_with_life:
            for i, sym in enumerate(parsed.symbols):
                if sym.phyrexian and sym.phyrexian_color:
                    # Pay with life if we have enough (> 4 life threshold for safety)
                    if self.life > 4:
                        phyrexian_life_cost += 2
                        phyrexian_symbols_paid.add(i)

        # Build list of colors we MUST have (strict colored requirements)
        color_needs = {}  # color_key -> amount needed
        snow_count = 0    # number of {S} symbols — must be paid from snow sources
        for i, sym in enumerate(parsed.symbols):
            if i in phyrexian_symbols_paid:
                continue  # Already paying with life
            if sym.color and sym.color.value in ('W', 'U', 'B', 'R', 'G'):
                key = sym.color.value
                color_needs[key] = color_needs.get(key, 0) + 1
            elif sym.color and sym.color.value == 'C':
                # Strict colorless (Eldrazi) — only colorless mana works
                color_needs['C'] = color_needs.get('C', 0) + 1
            elif getattr(sym, 'is_snow', False):
                # {S} — any snow mana of any color counts. Treat as generic
                # for the total-mana check, but require N snow sources to
                # tap below. May 13 audit: Icehide Golem ({S}) reported
                # "Not enough snow mana" even with Snow-Covered Forest in
                # play because this function ignored {S} symbols entirely.
                snow_count += 1
            elif getattr(sym, 'phyrexian', False) and sym.phyrexian_color:
                # Aug 9 audit (A-1): an UNPAID Phyrexian symbol must demand
                # its color. A phyrexian ManaSymbol sets only phyrexian_color
                # (color is None), so it matched NO branch of this chain and
                # contributed zero to total_cost — Hex Parasite's {B/P}
                # activation cost was free (no mana, no life), and a
                # Phyrexian CAST at life <= 4 (the life-payment block above
                # declines) was equally free. Reached only when the symbol
                # is not in phyrexian_symbols_paid (life declined or
                # unaffordable): CR 107.4c — pay the color or 2 life.
                key = sym.phyrexian_color.value
                color_needs[key] = color_needs.get(key, 0) + 1

        # Calculate generic mana needed
        # {S} symbols count toward the total mana requirement — they need
        # 1 mana paid each, just from a snow source. Adding them to
        # generic_needed makes the total_cost / total_available check
        # right, and the snow-source preference loop below handles the
        # snow-source restriction.
        generic_needed = parsed.generic_requirement + additional_generic + snow_count
        # Add X cost
        generic_needed += x_value * max(parsed.x_count, 0)
        # Hybrid symbols that aren't strict — treat as generic for now
        # (Hybrid is flexible: {W/U} can be paid with W or U)
        hybrid_count = 0
        hybrid_options = []  # list of (index, option_colors)
        for i, sym in enumerate(parsed.symbols):
            if i in phyrexian_symbols_paid:
                continue
            if sym.hybrid_colors:
                hybrid_count += 1
                hybrid_options.append((i, [sym.hybrid_colors[0].value, sym.hybrid_colors[1].value]))
            elif sym.hybrid_generic:
                hybrid_count += 1
                hybrid_options.append((i, [sym.hybrid_generic[1].value, 'generic']))

        # === PHASE 0 (July 21, 2026 batch audit): spend FLOATING pool mana ===
        # The pool holds genuinely-floating mana (Castle Vantress-style
        # [ACTIVATE-MANA] activations, rituals, Tier-3 add_mana). Before this
        # phase existed the payer ignored the pool entirely, so floated mana
        # was advertised (available_mana_detailed seeds from the pool) but
        # could never actually pay for anything — one half of the Bring Back
        # divergence in game_1529165073443197190. Spending here is LOGICAL
        # (recorded in pool_spent, applied in Phase 4) so early-return
        # failures leave the pool untouched.
        pool_spent = {}
        _pool_avail = {k: v for k, v in self.mana_pool.items() if v > 0}
        for _color, _amount in self._restricted_mana_available(spending_card).items():
            _pool_avail[_color] = _pool_avail.get(_color, 0) + _amount
        # Aug 8 (queue R4): audit visibility — when restricted buckets exist
        # but NONE of them permit this spell, say so once per payment. The
        # gate itself has held correctly since Aug 5; what was missing is
        # the line an audit greps to SEE it hold ([RESTRICTED-MANA]).
        # Aggregated by (color, restriction, source): the engine producer
        # appends one bucket PER UNIT, and six identical lines per refused
        # payment defeats the grep this line exists for (adversarial
        # review, note class).
        if self.restricted_mana_pool and not self._restricted_mana_available(spending_card):
            _held: Dict[tuple, int] = {}
            for _b in self.restricted_mana_pool:
                _key = (_b.get('color', '?'), _b.get('restriction', '?'),
                        _b.get('source', 'unknown source'))
                _held[_key] = _held.get(_key, 0) + int(_b.get('amount', 0))
            for (_c, _r, _s), _amt in _held.items():
                print(f"[RESTRICTED-MANA] {_amt} {_c} held — "
                      f"{_r} only ({_s})")


        def _spend_pool(color, amount):
            take = min(_pool_avail.get(color, 0), amount)
            if take > 0:
                _pool_avail[color] -= take
                pool_spent[color] = pool_spent.get(color, 0) + take
            return take

        # Strict colored pips (incl. strict {C}) from matching pool colors.
        for _color in list(color_needs.keys()):
            used = _spend_pool(_color, color_needs[_color])
            if used:
                color_needs[_color] -= used
                if color_needs[_color] <= 0:
                    del color_needs[_color]
        # Hybrid pips: either color pays (prefer the more-abundant pool color).
        _remaining_hybrids = []
        for _idx, _options in hybrid_options:
            _real = [c for c in _options if c != 'generic']
            _pick = max(_real, key=lambda c: _pool_avail.get(c, 0), default=None)
            if _pick is not None and _pool_avail.get(_pick, 0) > 0:
                _spend_pool(_pick, 1)
                continue
            _remaining_hybrids.append((_idx, _options))
        hybrid_options = _remaining_hybrids
        hybrid_count = len(hybrid_options)
        # Generic portion — but NOT the {S} part (pool mana can't be verified
        # snow, so {S} pips must still come from snow-source taps below).
        _generic_pool_payable = max(0, generic_needed - snow_count)
        for _color in sorted(_pool_avail, key=lambda c: (c != 'C', -_pool_avail[c])):
            if _generic_pool_payable <= 0:
                break
            used = _spend_pool(_color, min(_pool_avail[_color], _generic_pool_payable))
            _generic_pool_payable -= used
            generic_needed -= used

        # Gather all untapped sources and their production
        sources = []
        for card in self.untapped_mana_sources():
            production = self._get_mana_production(card)
            sources.append((card, production))

        # June 10 deep-dive (dual-land underpay): a source's ONE-TAP output is
        # the MAX over its color options, not the sum — "Add {W} or {U}" duals
        # were counted as 2 available mana, and the commitment accounting below
        # credited both colors from one tap, so spells resolved UNDERPAID by
        # (colors-1) per dual (CR 601.2g; three spells in game …069616767067).
        # Single-key sources (basics, Sol Ring {'C':2}) are unchanged; rare
        # both-at-once multi-color producers ("Add {B}{R}") are
        # indistinguishable in this production model and now under-count by 1
        # (a slight over-tap — the safe direction).
        # Extracted to Player._one_tap_output so `one_tap_mana_total` (the
        # advertisement ceiling) and this (the payment arbiter) can never
        # drift apart. Callers below that hold no card keep the old
        # card-less behaviour by omitting the argument.
        _one_tap_output = self._one_tap_output

        # Calculate total available mana
        total_available = sum(_one_tap_output(p, c) for c, p in sources)
        total_cost = sum(color_needs.values()) + generic_needed + hybrid_count
        if total_available < total_cost:
            return False

        # === PHASE 1: Assign sources to colored requirements ===
        # Categorize sources by what colors they produce
        colored_sources = []    # Sources that produce specific colors
        any_sources = []        # Sources that produce 'any' color
        colorless_sources = []  # Sources that only produce colorless

        for card, production in sources:
            produces_colors = {k for k, v in production.items() if v > 0 and k not in ('any', 'C')}
            produces_any = production.get('any', 0) > 0
            produces_colorless_only = not produces_colors and not produces_any

            if produces_any:
                any_sources.append((card, production))
            elif produces_colorless_only:
                colorless_sources.append((card, production))
            else:
                colored_sources.append((card, production))

        tapped_cards = set()  # Track which cards we're tapping (by id)
        # June 10: which single color each tapped source is committed to —
        # Phase 4 adds ONLY that color to the pool (a dual previously added
        # both colors: 2 pool mana from one land).
        committed_color = {}
        mana_produced = {'W': 0, 'U': 0, 'B': 0, 'R': 0, 'G': 0, 'C': 0}

        # Sort colored sources: prefer single-color sources first (don't waste dual-lands on requirements
        # that could be filled by basics). Tap sources with fewer color options first.
        colored_sources.sort(key=lambda x: len([k for k, v in x[1].items() if v > 0 and k not in ('any', 'C')]))

        # Tap colored sources to fill colored requirements
        remaining_needs = dict(color_needs)
        for card, production in colored_sources:
            if not remaining_needs:
                break
            produces = {k: v for k, v in production.items() if v > 0 and k not in ('any', 'C')}
            # Vivid: one tap yields EVERY listed colour at once, so it can
            # satisfy several colored pips by itself (Bloom Tender alone pays
            # {W}{U} on a four-colour board — that is the whole card). Only
            # tap it if it actually helps; the sort above already puts it
            # last, so basics are spent first.
            #
            # committed_color is deliberately left UNSET: Phase 4 narrows a
            # committed source to that one colour (the June 10 OR-dual fix),
            # which for a simultaneous source would throw away the rest.
            # Leaving it unset keeps the full production, and the Phase 4
            # settle then floats whatever the cost did not consume.
            if self._produces_all_colors_at_once(card):
                _useful = {c: a for c, a in produces.items()
                           if c in remaining_needs}
                if not _useful:
                    continue
                tapped_cards.add(id(card))
                # Credit ONLY the colours this cost actually needs.
                # `mana_produced` is the PAYMENT LEDGER, and the colour check
                # further down forgives a shortfall whenever total production
                # exceeds total cost (an 'any'-mana escape hatch). Crediting
                # every colour inflates that surplus with mana of the WRONG
                # colours: Bloom Tender alone would have paid {W}{W}, because
                # its unused {B} and {G} looked like flexible excess. The
                # unneeded colours are still produced — Phase 4 keeps the full
                # production dict and floats them.
                for _c, _amt in _useful.items():
                    _take = min(_amt, remaining_needs[_c])
                    mana_produced[_c] = mana_produced.get(_c, 0) + _take
                    remaining_needs[_c] -= _take
                    if remaining_needs[_c] <= 0:
                        del remaining_needs[_c]
                continue
            # Check if this source produces a color we need
            for color in list(remaining_needs.keys()):
                if color in produces and remaining_needs[color] > 0:
                    tapped_cards.add(id(card))
                    # June 10 deep-dive: credit ONLY the committed color. The
                    # old loop credited every color the source COULD produce,
                    # counting one dual land as 2 toward the total and letting
                    # Phase 3 under-tap (underpaid casts).
                    committed_color[id(card)] = color
                    mana_produced[color] = mana_produced.get(color, 0) + produces[color]
                    # June 10 audit (C1): decrement the satisfied need. Without
                    # this, the `if not remaining_needs: break` guard above was
                    # dead code and EVERY source producing a needed color got
                    # tapped — a 1-mana Bolt tapped the whole board (since Apr 4).
                    remaining_needs[color] -= produces[color]
                    if remaining_needs[color] <= 0:
                        del remaining_needs[color]
                    break  # Source is now committed

        # Check if colored requirements are met, use 'any' sources for shortfalls
        for color, needed in list(remaining_needs.items()):
            # July 26 batch-7 audit: `remaining_needs[color]` is ALREADY the
            # outstanding shortfall — Phase 1 decrements it by whatever it
            # tapped (line ~2462) and deletes the key once satisfied. The old
            # `needed - have` re-subtracted `mana_produced[color]`, i.e. the
            # very mana that decrement had already accounted for, so a
            # partially-filled colored requirement computed shortfall 0 and no
            # 'any' source was ever allocated to it. Swamp + Command Tower
            # could not pay {B}{B}; two Swamps could. That silently blocked any
            # multi-pip cost held up by one dedicated source plus any-color
            # sources — Command Tower, Arcane Signet, talismans, Chromatic
            # Lantern — which in Commander is the common case, not a corner.
            # (Found via game_1530441513702785114, where Phyrexian Arena was
            # rejected twice on a board that could pay for it.)
            shortfall = needed
            if shortfall > 0:
                for card, production in any_sources:
                    if id(card) in tapped_cards:
                        continue
                    if shortfall <= 0:
                        break
                    tapped_cards.add(id(card))
                    any_amount = sum(production.values())
                    # This 'any' source fills the color gap
                    mana_produced[color] = mana_produced.get(color, 0) + any_amount
                    shortfall -= any_amount

        # === PHASE 2: Handle hybrid mana ===
        for idx, options in hybrid_options:
            # Try to pay with mana we already have excess of
            paid = False
            for opt_color in options:
                if opt_color == 'generic':
                    generic_needed += 1  # Treat as generic
                    paid = True
                    break
                excess = mana_produced.get(opt_color, 0) - color_needs.get(opt_color, 0)
                if excess > 0:
                    # Already have excess of this color, use it
                    color_needs[opt_color] = color_needs.get(opt_color, 0) + 1
                    paid = True
                    break
            if not paid:
                # Need to tap a new source for one of the hybrid colors
                generic_needed += 1  # Fallback: treat as generic

        # === PHASE 3: Tap remaining sources for generic ===
        # Priority: multi-mana rocks (Sol Ring=2, Thran Dynamo=3) before single-mana
        # sources (basic lands). This keeps colored lands untapped for future spells.
        total_mana_committed = sum(mana_produced.values())
        # Aug 23 cube-FFA audit: everything tapped from here on pays GENERIC,
        # so a source printing a damage-free mana line (Talismans, pain lands)
        # could have used that line and takes no tap damage. Snapshotting the
        # set at the phase boundary rather than flagging each tap site keeps
        # this correct for tap sites added later.
        _colored_pip_taps = set(tapped_cards)
        # June 10 audit (V5): life-paid Phyrexian symbols are already EXCLUDED
        # from color_needs/total_cost (see phyrexian_symbols_paid skip above),
        # so no adjustment belongs here. The old `+ phyrexian_life_cost` term
        # conjured 2 phantom generic mana per symbol — Gitaxian Probe paid
        # 2 life AND tapped 2 Islands (CR 107.4f is either/or).
        generic_still_needed = total_cost - total_mana_committed

        # May 13 audit: {S} symbols require N snow sources to be tapped.
        # Walk every untapped source, prefer those whose name/type-line marks
        # them as snow, and tap until snow_count is satisfied. This MUST run
        # before the regular generic tapping so we don't waste a non-snow
        # land paying the {S} portion.
        if snow_count > 0:
            try:
                from rules.mana import SNOW_LANDS as _SNOW_LANDS
            except Exception:
                _SNOW_LANDS = set()
            snow_remaining = snow_count
            snow_taps = 0
            for card, production in sources:
                if snow_remaining <= 0:
                    break
                if id(card) in tapped_cards:
                    continue
                is_snow_src = (
                    (card.name and card.name in _SNOW_LANDS)
                    or ('Snow' in (getattr(card, 'type_line', '') or ''))
                )
                if not is_snow_src:
                    continue
                tapped_cards.add(id(card))
                snow_taps += 1
                snow_remaining -= 1
                # June 10: one-tap output + single committed color (snow duals
                # like Highland Forest are or-choices too).
                _snow_keys = [k for k in production if k != 'any'] or ['C']
                _snow_color = max(_snow_keys, key=lambda k: production.get(k, 0))
                _snow_amt = _one_tap_output(production)
                mana_produced[_snow_color] = mana_produced.get(_snow_color, 0) + _snow_amt
                committed_color[id(card)] = _snow_color
                generic_still_needed -= _snow_amt
            if snow_remaining > 0:
                # Not enough snow sources for the {S} requirement.
                print(f"[MANA-ENGINE] Need {snow_count} snow source(s) for {mana_cost_str}, "
                      f"only found {snow_taps} untapped — cannot pay")
                return False

        if generic_still_needed > 0:
            # Sort colorless sources by output (high-producers first: Sol Ring=2, Thran Dynamo=3)
            colorless_sources.sort(key=lambda x: sum(x[1].values()), reverse=True)
            for card, production in colorless_sources:
                if id(card) in tapped_cards:
                    continue
                if generic_still_needed <= 0:
                    break
                prod_amount = sum(production.values())
                # Aug 2 batch-13 (the ~4% +1 over-tap): ZERO-producers land in
                # this bucket — fetchlands ({'C': 0} by design), Temple of the
                # False God under 5 lands, an empty Gaea's Cradle — because the
                # produces_colors filter is v>0 and they have no 'any'. Tapping
                # one yields nothing, burns the tap, and the loop then reaches a
                # real source anyway (Flooded Strand tapped alongside two duals
                # for a {1}{W} Wall of Omens, game_1533272913728372764). Never
                # tap a source that produces nothing.
                if prod_amount <= 0:
                    continue
                tapped_cards.add(id(card))
                mana_produced['C'] = mana_produced.get('C', 0) + prod_amount
                generic_still_needed -= prod_amount

            # Then any-color sources (these are valuable — tap only if needed)
            for card, production in any_sources:
                if id(card) in tapped_cards:
                    continue
                if generic_still_needed <= 0:
                    break
                tapped_cards.add(id(card))
                # June 10: one tap = one output ('any' sources are single-key,
                # so behavior is unchanged for them; guards future shapes).
                _any_amt = _one_tap_output(production)
                mana_produced['C'] = mana_produced.get('C', 0) + _any_amt
                generic_still_needed -= _any_amt

            # Then colored sources (least preferred for generic — preserves color flexibility)
            for card, production in colored_sources:
                if id(card) in tapped_cards:
                    continue
                if generic_still_needed <= 0:
                    break
                tapped_cards.add(id(card))
                # Vivid tapped for generic: every colour arrives, so credit
                # them all and leave committed_color unset (same reasoning as
                # the Phase 1 branch — narrowing would discard the rest).
                if self._produces_all_colors_at_once(card):
                    # Same ledger discipline as the Phase 1 branch: generic
                    # accepts any colour, so credit up to the shortfall and
                    # no further. Over-crediting here would re-open the same
                    # wrong-colour-surplus hole in the colour check below.
                    _left = min(_one_tap_output(production, card),
                                generic_still_needed)
                    generic_still_needed -= _left
                    for _c, _amt in sorted(production.items()):
                        if _left <= 0:
                            break
                        if _c == 'any' or _amt <= 0:
                            continue
                        _take = min(_amt, _left)
                        mana_produced[_c] = mana_produced.get(_c, 0) + _take
                        _left -= _take
                    continue
                # June 10: a dual tapped for generic contributes ONE mana of
                # one color, not one of each (was over-crediting and
                # under-tapping the rest of the generic requirement).
                _gen_keys = [k for k in production if k != 'any'] or ['C']
                _gen_color = max(_gen_keys, key=lambda k: production.get(k, 0))
                _gen_amt = _one_tap_output(production)
                mana_produced[_gen_color] = mana_produced.get(_gen_color, 0) + _gen_amt
                committed_color[id(card)] = _gen_color
                generic_still_needed -= _gen_amt

        # Verify we have enough total mana (life-paid Phyrexian symbols are
        # excluded from total_cost, so produced mana alone must cover it)
        if sum(mana_produced.values()) < total_cost:
            return False

        # Verify all colored requirements are met
        for color, needed in color_needs.items():
            if mana_produced.get(color, 0) < needed:
                # Check if 'any' mana can fill the gap
                any_excess = sum(mana_produced.values()) - total_cost
                if any_excess < needed - mana_produced.get(color, 0):
                    return False

        # === PHASE 4: Actually tap the sources and settle the pool ===
        # July 21, 2026 batch audit: this phase previously added every tapped
        # source's production to the mana pool WITHOUT ever deducting the
        # cost — the pool accumulated all mana "produced this phase", so
        # available_mana_detailed() (which seeds from the pool) over-
        # advertised by everything already spent, and the July 20 one-tap
        # gate counted the same phantoms. Now: payment mana never touches
        # the pool; only true EXCESS (e.g. Sol Ring's second mana covering a
        # 1-generic remainder) floats, and Phase-0 pool spending is applied.
        produced_by_color = {}
        produced_by_source = []
        for card, production in sources:
            if id(card) in tapped_cards:
                card.tapped = True
                # Apply self-damage for painful lands
                # Aug 10 (H1): Mana Confluence prints "{T}, Pay 1 life:",
                # a COST rather than damage. Charged here so the life total
                # stays right while the damage helper stops mislabelling it
                # (a payment cannot be prevented, doubled or redirected, and
                # protection and Torbran both read damage now).
                tap_life = self._get_mana_tap_life_cost(card)
                if tap_life > 0:
                    self.life -= tap_life
                    self.record_life_loss(tap_life, game=game)
                    print(f"[MANA-COST] {card.name}: {self.name} pays {tap_life} life (life: {max(0, self.life)})")
                    self._pending_tap_damage_msgs.append(
                        f"💧 {card.name}: {self.name} pays {tap_life} life (life: {max(0, self.life)})")
                tap_damage = self._get_mana_tap_damage(
                    card, paid_generic=id(card) not in _colored_pip_taps)
                if tap_damage > 0:
                    self.life -= tap_damage
                    self.record_life_loss(tap_damage)
                    print(f"[MANA-DAMAGE] {card.name} deals {tap_damage} damage to {self.name} (life: {max(0, self.life)})")
                    self._pending_tap_damage_msgs.append(
                        f"🩸 {card.name} deals {tap_damage} damage to {self.name} (life: {max(0, self.life)})")
                # May 16 audit: pay sac cost for Phyrexian Tower's {B}{B}
                # ability. If we promised B mana via _get_mana_production
                # but the player has no sac target right now, degrade to
                # the {C: 1} ability so we don't fabricate mana.
                if 'phyrexian tower' in card.name.lower() and production.get('B', 0) >= 2:
                    victim = self._apply_sac_cost_at_tap(card, game)
                    if victim is None:
                        production = {'C': 1}
                # June 10 deep-dive: a Phase-1/3-committed source adds ONLY its
                # committed color to the pool — adding full production gave the
                # pool 2 mana from one dual-land tap.
                _cc = committed_color.get(id(card))
                if _cc is not None and _cc in production:
                    production = {_cc: production[_cc]}
                # Accumulate what was actually produced ('any' filed under C —
                # excess color fidelity for any-sources is not worth tracking).
                for pc, pv in production.items():
                    _key = 'C' if pc == 'any' else pc
                    produced_by_color[_key] = produced_by_color.get(_key, 0) + pv
                produced_by_source.append((card, {
                    ('C' if pc == 'any' else pc): pv
                    for pc, pv in production.items() if pv > 0
                }))
                # July 30: Mirari's Wake-class tap bonus — direct pool add
                # (never enters produced_by_color, so the cost settle below
                # can't consume it; it is pure floating excess).
                self._fire_tap_for_mana_bonuses(card, production)

        # Settle the pool: deduct the cost from what the taps produced;
        # whatever remains is true excess and floats. Then apply Phase-0
        # pool spending.
        _excess = dict(produced_by_color)
        _deduct = total_cost
        for _color, _needed in color_needs.items():
            _take = min(_excess.get(_color, 0), _needed, _deduct)
            if _take > 0:
                _excess[_color] -= _take
                _deduct -= _take
        for _k in sorted(_excess, key=lambda c: (c != 'C', -_excess[c])):
            if _deduct <= 0:
                break
            _take = min(_excess[_k], _deduct)
            _excess[_k] -= _take
            _deduct -= _take
        for _k, _v in _excess.items():
            if _v > 0 and _deduct <= 0:
                self.mana_pool[_k] = self.mana_pool.get(_k, 0) + _v
        # Q4 ordering rule — DEBIT before CREDIT. Phase-0 spent mana that
        # was in the pool BEFORE this payment, so its snow portion must be
        # judged against the PRE-EXISTING tags only. The excess floated
        # just above is credited AFTER this loop (in the attribution walk
        # below); crediting it first would let freshly-floated snow
        # masquerade as spent pool snow — an overcount, the forbidden
        # direction. Restricted-pool spends carry no tags (non-snow,
        # undercount-safe).
        _pool_snow_spent = 0
        for _k, _v in pool_spent.items():
            _ordinary = min(self.mana_pool.get(_k, 0), _v)
            _pool_snow_spent += self.debit_pool_snow(_k, _ordinary)
            self.mana_pool[_k] = max(0, self.mana_pool.get(_k, 0) - _ordinary)
            _restricted_needed = _v - _ordinary
            if _restricted_needed > 0:
                self._spend_restricted_mana(
                    _k, _restricted_needed, spending_card=spending_card)

        # Attribute the settled (non-excess) units back to their tapped
        # sources. Within one color, stable source order is the engine's
        # deterministic payment choice. This counts mana actually consumed,
        # not every unit a multi-mana snow source happened to produce.
        try:
            from rules.mana import SNOW_LANDS as _SNOW_LANDS
        except ImportError:
            _SNOW_LANDS = set()
        _consumed_by_color = {
            color: max(0, amount - _excess.get(color, 0))
            for color, amount in produced_by_color.items()
        }
        _snow_spent = 0
        for source, production in produced_by_source:
            _is_snow = ((source.name and source.name in _SNOW_LANDS)
                        or 'snow' in (source.type_line or '').lower())
            for color, amount in production.items():
                take = min(amount, _consumed_by_color.get(color, 0))
                if take > 0:
                    _consumed_by_color[color] -= take
                    if _is_snow:
                        _snow_spent += take
                # Q4: the un-consumed remainder of a snow source's
                # production is excess that just floated (the settle above
                # adds _excess to the pool only when _deduct <= 0, i.e.
                # the payment was fully covered). Tag it so a LATER
                # payment's Phase-0 pool spend can count it as snow.
                # Credit is clamped against the pool's final count, and
                # deliberately happens AFTER the pool-spend debit above —
                # see the ordering rule there.
                if _is_snow and _deduct <= 0:
                    _floated = amount - take
                    if _floated > 0:
                        self.credit_pool_snow(color, _floated)
        self._last_payment = {'snow_spent': _snow_spent + _pool_snow_spent}

        # Apply Phyrexian life payment
        if phyrexian_life_cost > 0:
            self.life -= phyrexian_life_cost
            self.record_life_loss(phyrexian_life_cost)
            print(f"[MANA-PHYREXIAN] {self.name} pays {phyrexian_life_cost} life for Phyrexian mana (life: {self.life})")
            # July 21 batch audit (R1-4): console-only before — Gitaxian
            # Probe's 2 life never reached Discord while the pain-land line
            # right above did. Same buffered-message drain.
            self._pending_tap_damage_msgs.append(
                f"🩸 {self.name} pays {phyrexian_life_cost} life for Phyrexian "
                f"mana (life: {max(0, self.life)})")

        # Converge (CR 702.100a) needs the COLORS actually spent — the engine
        # has already resolved each tapped source to exactly one committed
        # color, so record that set (plus any colors taken from the floating
        # pool) for the cost stage to stamp onto the spell.
        _spent = {c for c in committed_color.values() if c in 'WUBRG'}
        _spent |= {c for c in (pool_spent or {}) if c in 'WUBRG'}
        # A Vivid source has no committed colour by design (it contributes
        # every colour at once), so it would otherwise be invisible to
        # converge. Add whatever the settle actually consumed.
        #
        # This is NOT purely a no-op elsewhere, despite how it reads. Any
        # source tapped WITHOUT a committed colour is also newly visible —
        # in practice the 'any' sources, whose two tap sites never set one.
        # The Lorwyn Vivid lands (Vivid Grove and friends, no relation to the
        # ability word) produce {'G': 1, 'any': 1} and previously reported NO
        # colours spent at all; they now report the one they paid with, which
        # is the more correct answer for CR 702.100a. Recorded because the
        # invariant matters to anything that later consumes _last_colors_spent.
        _spent |= {c for c, v in produced_by_color.items()
                   if c in 'WUBRG' and v > _excess.get(c, 0)}
        self._last_colors_spent = tuple(sorted(_spent))

        _pool_note = (f" (+{sum(pool_spent.values())} from floating pool)"
                      if pool_spent else '')
        print(f"[MANA-ENGINE] Tapped {len(tapped_cards)} sources for {mana_cost_str}"
              f"{_pool_note}"
              f"{f' + {additional_generic} generic' if additional_generic else ''}"
              f"{f' (X={x_value})' if x_value else ''}"
              f"{f' ({phyrexian_life_cost} life for Phyrexian)' if phyrexian_life_cost else ''}")

        return True

    def _add_production_to_pool(self, production: Dict[str, int], preferred_colors: str = "",
                               color_shortfalls: Dict[str, int] = None):
        """Add a source's mana production to the mana pool.

        Handles 'any' color mana by picking the best color based on:
        1. color_shortfalls dict (colors still needed — pick the one with biggest deficit)
        2. preferred_colors parameter (e.g., colors needed by spell being cast)
        3. Commander's color identity (pick color we have least of)
        4. Colorless as last resort
        """
        for color, color_amount in production.items():
            if color == 'any':
                # Pick the color with the biggest remaining shortfall
                if color_shortfalls:
                    best_color = max(color_shortfalls.keys(),
                                     key=lambda c: color_shortfalls.get(c, 0))
                    if color_shortfalls.get(best_color, 0) > 0:
                        self.mana_pool[best_color] += color_amount
                        color_shortfalls[best_color] = max(0, color_shortfalls[best_color] - color_amount)
                        continue
                if preferred_colors:
                    # Pick the preferred color we have least of in the pool
                    best = min(preferred_colors, key=lambda c: self.mana_pool.get(c.upper(), 0))
                    self.mana_pool[best.upper()] += color_amount
                else:
                    cmd_colors = self._get_commander_colors()
                    if cmd_colors:
                        least_color = min(cmd_colors, key=lambda c: self.mana_pool.get(c, 0))
                        self.mana_pool[least_color] += color_amount
                    else:
                        self.mana_pool['C'] += color_amount
            else:
                self.mana_pool[color] = self.mana_pool.get(color, 0) + color_amount

    def _get_commander_colors(self) -> list:
        """Get the union of every commander's color identity.

        Partner / Friends Forever / Choose-a-Background / Doctor's Companion
        and Background mechanics all give a player two commanders. CR 903.4
        defines deck color identity as the union of every commander's
        identity, so this needs to scan all of them — not just return the
        first one found. Returning only the first was the source of the
        Apr 2026 partner-deck bug where Thrasios+Tymna decks couldn't cast
        white/black or green/blue cards across the partition.

        May 14 audit: scan EVERY zone (not just command_zone + battlefield).
        A commander in graveyard / exile / hand / library still contributes
        to the deck's color identity for CR 903.4 purposes — the deck-build
        constraint doesn't go away because the commander is temporarily out
        of those two zones. The partner-deck cast rejections traced to a
        commander dying and the scan skipping the graveyard.

        Returns colors in WUBRG order so callers see a stable shape.

        June 10 audit (V3): the result is CACHED after the first non-empty
        computation. CR 903.4 makes color identity a deck-construction
        constant — recomputing from live card locations meant a commander
        stolen onto the OPPONENT's battlefield (Animate Dead on Tymna)
        vanished from the scan and the player's identity collapsed to the
        remaining partner, blocking 23 legal casts in one June 10 game.
        """
        cached = getattr(self, '_commander_identity_cache', None)
        if cached is not None:
            return list(cached)
        identity = set()
        zones = [
            list(getattr(self, 'command_zone', []) or []),
            list(getattr(self, 'battlefield', []) or []),
            list(getattr(self, 'graveyard', []) or []),
            list(getattr(self, 'exile', []) or []),
            list(getattr(self, 'hand', []) or []),
            list(getattr(self, 'library', []) or []),
        ]
        seen_ids = set()
        for zone in zones:
            for cmd_card in zone:
                if not getattr(cmd_card, 'is_commander', False):
                    continue
                # Dedupe by card identity (same Card instance could appear
                # twice via stale references).
                card_id = id(cmd_card)
                if card_id in seen_ids:
                    continue
                seen_ids.add(card_id)
                if cmd_card.color_identity:
                    identity.update(cmd_card.color_identity)
                elif cmd_card.mana_cost:
                    for c in ['W', 'U', 'B', 'R', 'G']:
                        if f'{{{c}}}' in cmd_card.mana_cost.upper():
                            identity.add(c)
        # Stable WUBRG order
        order = ['W', 'U', 'B', 'R', 'G']
        result = [c for c in order if c in identity]
        if result:
            # Cache only a non-empty identity: at game init the commanders sit
            # in the command zone so the first call is authoritative; an empty
            # result (60-card formats) stays cheap to recompute and avoids
            # caching a pre-deck-load transient.
            self._commander_identity_cache = result
        return result
    
    def can_pay_printed_alternate_cost(self, card) -> bool:
        """Predicate twin of the cast-time alternate-cost branches in
        mtg/spells.py:_compute_alt_costs — can `card` be cast WITHOUT paying
        its printed mana cost right now?

        July 20 audit: the response-AI affordability filters checked only the
        printed cost, so Force of Will sat dead in hand for 51 turns in one
        game ("filtered unaffordable instants: ['Force of Will']" on every
        priority window) despite blue cards to exile the whole time. This
        mirrors the branches the payment stage actually takes — it must never
        say True for a cost the cast path can't complete. Checks only; no
        mutation.
        """
        oracle_lower = (getattr(card, 'oracle_text', '') or '').lower()
        if not oracle_lower:
            return False
        # Commander 2020 free-interaction cycle (Fierce Guardianship,
        # Deflecting Swat, etc.).  Mirror the payment-stage predicate.
        if (('control a commander' in oracle_lower
             or 'a commander you control' in oracle_lower)
                and 'without paying its mana cost' in oracle_lower):
            return any(getattr(c, 'is_commander', False)
                       for c in self.battlefield)
        # Force of Will family: "pay 1 life and exile a <color> card from
        # your hand rather than pay this spell's mana cost"
        if 'pay 1 life and exile a' in oracle_lower and 'from your hand' in oracle_lower:
            if self.life <= 1:
                return False
            color_map = {'blue': 'U', 'black': 'B', 'red': 'R', 'green': 'G', 'white': 'W'}
            exile_color = None
            for color_name, color_code in color_map.items():
                if color_name in oracle_lower:
                    exile_color = color_code
                    break
            if not exile_color:
                return False
            return any(c is not card and c.mana_cost
                       and exile_color in c.mana_cost.upper()
                       for c in self.hand)
        # Fireblast family: "sacrifice two Mountains rather than pay ..."
        if 'sacrifice' in oracle_lower and 'rather than pay' in oracle_lower:
            sac_match = re.search(r'sacrifice (\w+) (\w+)', oracle_lower)
            if sac_match:
                count_map = {'two': 2, 'three': 3, 'a': 1, 'an': 1, 'one': 1}
                sac_count = count_map.get(sac_match.group(1), 1)
                perm_type = sac_match.group(2).rstrip('s')
                candidates = [c for c in self.battlefield
                              if perm_type in c.name.lower()
                              or perm_type in (c.type_line or '').lower()]
                return len(candidates) >= sac_count
        # (Pacts pass the normal can_pay_mana_cost check — printed cost {0}.)
        return False

    def can_pay_mana_cost(self, mana_cost: str, spending_card=None) -> Tuple[bool, str]:
        """
        Check if player can pay a mana cost (color-aware).
        Returns (can_pay, reason).

        Uses rules/mana.py ManaCost parser for proper color requirement
        validation including hybrid and phyrexian costs.  Falls back to
        total-mana-only check when the engine isn't available.

        IMPORTANT: Checks mana from UNTAPPED SOURCES (lands + rocks), not just
        the mana pool dict.  The pool dict is empty until tap_sources_for_cost()
        actually taps lands — this function must look at what we COULD produce.
        """
        # Aug 7 (Q3): Draugr's snow-as-any-color permission, scoped to this
        # check — the shared setter derives per-entry (review #1/#6).
        self._set_snow_waiver(spending_card)
        if not mana_cost:
            return True, "No mana cost"

        # July 29 batch audit: split cards carry the COMBINED cost string
        # ("{3}{U} // {4}{U}{U}") — parsed as ONE cost it demands both
        # halves' pips at once (CMC 10 for Commit // Memory), so the
        # response-priority filter auto-passed with 7 untapped Islands at
        # the lethal moment. You cast one half (CR 709.3): payable when
        # either half is.
        if " // " in mana_cost:
            _reasons = []
            for _half in mana_cost.split(" // "):
                ok, reason = self.can_pay_mana_cost(
                    _half.strip(), spending_card=spending_card)
                if ok:
                    return True, f"Can pay split half {_half.strip()}"
                _reasons.append(reason)
            return False, "; ".join(_reasons) or "Cannot pay either split half"

        # Structured mana engine — proper color validation
        if HAS_MANA_ENGINE:
            try:
                # July 20 audit: available_mana_detailed() counts EVERY color an
                # OR-dual can produce (a W/B land adds 1 to W and 1 to B), so
                # each per-color number is a true capacity but their TOTAL is
                # not — 5 physical sources displayed as 12 "available" and
                # advertised Sun Titan ({4}{W}{W}) as castable off 5 taps
                # (game_1527451728084074550; the AI then burned whole main
                # phases in retry loops against tap_sources_for_cost's correct
                # refusal). A source taps ONCE: gate on the physical one-tap
                # total (floating pool + max-one-mana per untapped source)
                # before the color-wise check.
                _one_tap_total = self.one_tap_mana_total(spending_card=spending_card)
                _parsed_for_total = ManaCost.parse(mana_cost)
                _required_total = _parsed_for_total.generic_requirement
                for _sym in _parsed_for_total.symbols:
                    # Phyrexian symbols can be paid with life — exclude
                    # them so this gate never over-rejects.
                    if getattr(_sym, 'phyrexian', False):
                        continue
                    if (_sym.color is not None
                            or getattr(_sym, 'hybrid_colors', None)
                            or getattr(_sym, 'hybrid_generic', None)
                            or getattr(_sym, 'is_snow', False)):
                        _required_total += 1
                if _one_tap_total < _required_total:
                    # Keep the "Not enough mana" prefix — downstream reason
                    # classifiers and the AI retry feedback key on it.
                    return False, (f"Not enough mana: only {_one_tap_total} "
                                   f"untapped source(s) for {_required_total} "
                                   f"total mana")
                # July 21 batch audit: hybrid pips need units capable of ONE
                # of their two colors specifically — the per-color pool below
                # double-counts OR-duals and the one-tap gate above is color-
                # blind, so {G/W}{G/W}{G/W}{G/W} with 3 G/W-capable sources +
                # 1 floating {U} advertised as payable and burned retries
                # (game_1529165073443197190, Bring Back). Necessary-condition
                # check per hybrid color-pair: pool[A]+pool[B] + sources
                # producing A or B (or any) must cover the pair's hybrid pips
                # PLUS strict A/B pips (they draw from the same units). Any
                # payable state satisfies this, so it never over-rejects;
                # the tap engine stays the final arbiter.
                _hybrid_pairs = {}
                _strict_pips = {}
                for _sym in _parsed_for_total.symbols:
                    if getattr(_sym, 'phyrexian', False):
                        continue
                    _hc = getattr(_sym, 'hybrid_colors', None)
                    if _hc:
                        _pair = frozenset((_hc[0].value, _hc[1].value))
                        _hybrid_pairs[_pair] = _hybrid_pairs.get(_pair, 0) + 1
                    elif _sym.color is not None:
                        _strict_pips[_sym.color.value] = _strict_pips.get(_sym.color.value, 0) + 1
                for _pair, _pips in _hybrid_pairs.items():
                    _pcolors = sorted(_pair)
                    _cap = sum(self.mana_pool.get(_pc, 0) for _pc in _pcolors)
                    _cap += sum(self._restricted_mana_available(
                        spending_card).get(_pc, 0) for _pc in _pcolors)
                    for _src in self.untapped_mana_sources():
                        _prod = self._get_mana_production(_src)
                        if not _prod:
                            continue
                        if (_prod.get('any', 0) > 0
                                or any(_prod.get(_pc, 0) > 0 for _pc in _pcolors)):
                            _cap += 1
                    _need = _pips + sum(_strict_pips.get(_pc, 0) for _pc in _pcolors)
                    if _cap < _need:
                        _pair_str = '/'.join(_pcolors)
                        return False, (f"Not enough mana: only {_cap} source(s) "
                                       f"can pay {{{_pair_str}}}-compatible pips "
                                       f"({_need} needed)")
                # Build pool from untapped sources + existing pool (not just pool dict)
                # available_mana_detailed() already does this correctly
                detailed = self.available_mana_detailed(spending_card=spending_card)
                any_mana = detailed.get('any', 0)
                pool = RulesManaPool()
                pool.white = detailed.get('W', 0)
                pool.blue = detailed.get('U', 0)
                pool.black = detailed.get('B', 0)
                pool.red = detailed.get('R', 0)
                pool.green = detailed.get('G', 0)
                pool.colorless = detailed.get('C', 0)
                parsed = ManaCost.parse(mana_cost)
                # May 13 audit: snow {S} payability. ManaPaymentValidator.can_pay
                # checks `test_pool.total_snow() > 0` for any {S} symbol in the
                # cost, but `available_mana_detailed()` returns only color totals
                # (no snow accounting). Result: Icehide Golem ({S}) reported
                # "Not enough snow mana" even with an untapped Snow-Covered
                # Forest on the battlefield. Fix: when the cost requires {S},
                # count untapped snow sources and reflect them in pool.snow_*.
                # We don't subtract from the regular pool — the snow_* fields
                # represent additional snow-eligible mana, and `spend()` with
                # prefer_snow drains them first. For {S}-free casts this loop
                # is skipped, so non-snow decks see zero overhead.
                try:
                    needs_snow = any(getattr(sym, 'is_snow', False) for sym in parsed.symbols)
                except Exception:
                    needs_snow = False
                if needs_snow:
                    try:
                        from rules.mana import SNOW_LANDS as _SNOW_LANDS
                    except Exception:
                        _SNOW_LANDS = set()
                    _color_to_attr = {
                        'W': 'snow_white', 'U': 'snow_blue', 'B': 'snow_black',
                        'R': 'snow_red', 'G': 'snow_green', 'C': 'snow_colorless',
                        'any': 'snow_colorless',
                    }
                    for src in self.untapped_mana_sources():
                        is_snow_src = (
                            (src.name and src.name in _SNOW_LANDS)
                            or ('Snow' in (getattr(src, 'type_line', '') or ''))
                        )
                        if not is_snow_src:
                            continue
                        try:
                            production = self._get_mana_production(src)
                        except Exception:
                            production = {}
                        for color_key, amt in (production or {}).items():
                            attr = _color_to_attr.get(color_key)
                            if attr and amt:
                                setattr(pool, attr, getattr(pool, attr, 0) + amt)
                # First try without 'any' mana
                can, reason = ManaPaymentValidator.can_pay(pool, parsed, life_total=self.life)
                if can:
                    return True, reason
                # If failed, allocate 'any' mana to fill color shortfalls
                if any_mana > 0:
                    # Distribute 'any' to whichever colors are short.
                    # BUGFIX (Apr 17, 2026): ManaCost has NO .white/.black/etc
                    # attributes — only `color_requirements` returning
                    # Dict[ManaColor, int]. Accessing parsed.white etc. raised
                    # AttributeError which was silently swallowed by the
                    # `except Exception: pass` below, causing the predicate to
                    # fall through to the inline fallback. That fallback used
                    # `detailed.get('any', 0)` but the structured path had
                    # consumed it to zero conceptually — so the predicate kept
                    # rejecting valid costs (e.g. Animate Dead {1}{B} with
                    # B=1 + any=3). Use parsed.color_requirements instead.
                    color_fields = [('white', ManaColor.WHITE), ('blue', ManaColor.BLUE),
                                    ('black', ManaColor.BLACK), ('red', ManaColor.RED),
                                    ('green', ManaColor.GREEN)]
                    color_reqs = parsed.color_requirements  # Dict[ManaColor, int]
                    remaining_any = any_mana
                    for field_name, color_enum in color_fields:
                        have = getattr(pool, field_name)
                        need = color_reqs.get(color_enum, 0)
                        if have < need and remaining_any > 0:
                            fill = min(need - have, remaining_any)
                            setattr(pool, field_name, have + fill)
                            remaining_any -= fill
                    # Remaining 'any' can pay for generic
                    pool.colorless += remaining_any
                    filled = any_mana - remaining_any
                    if filled > 0:
                        print(f"[MANA-PAYMENT] {self.name}: distributed {filled}/{any_mana} 'any' mana to fill color gaps for {mana_cost}")
                    return ManaPaymentValidator.can_pay(pool, parsed, life_total=self.life)
                return can, reason
            except Exception as e:
                print(f"[MANA-PAYMENT] Structured predicate failed for {mana_cost}: {type(e).__name__}: {e} — falling back to inline check")
                pass  # Fall through to inline check

        # Inline fallback: parse manually, check total + colors
        symbols = re.findall(r'\{([^}]+)\}', mana_cost)

        total_needed = 0
        colored_needed = {'W': 0, 'U': 0, 'B': 0, 'R': 0, 'G': 0}

        for sym in symbols:
            if sym.isdigit():
                total_needed += int(sym)
            elif sym in colored_needed:
                colored_needed[sym] += 1
                total_needed += 1
            elif sym == 'X':
                pass  # X is 0 unless specified
            elif '/' in sym:  # Hybrid
                total_needed += 1
            else:
                total_needed += 1  # Phyrexian, snow, etc.

        available = self.available_mana()
        if available < total_needed:
            return False, f"Need {total_needed} mana, have {available} available"

        # Color check using untapped sources (not just total)
        if any(colored_needed.values()):
            detailed = self.available_mana_detailed() if HAS_MANA_ENGINE else {}
            any_mana = detailed.get('any', 0)
            for color, needed in colored_needed.items():
                if needed > 0:
                    have = detailed.get(color, 0)
                    if have < needed:
                        # Try to fill shortfall with 'any' mana
                        shortfall = needed - have
                        if any_mana >= shortfall:
                            any_mana -= shortfall
                        else:
                            return False, f"Not enough {color} mana"

        return True, "Mana available"

    def to_mana_pool_object(self):
        """Convert dict mana_pool to rules/mana.py ManaPool for validation.

        The game engine stores mana as a dict {'W': 3, 'U': 1, ...}.
        ManaPaymentValidator expects a ManaPool dataclass.  This bridges
        the two representations without changing the dict-based pool that
        hundreds of call sites depend on.
        """
        if not HAS_MANA_ENGINE:
            return None
        pool = RulesManaPool()
        pool.white = self.mana_pool.get('W', 0)
        pool.blue = self.mana_pool.get('U', 0)
        pool.black = self.mana_pool.get('B', 0)
        pool.red = self.mana_pool.get('R', 0)
        pool.green = self.mana_pool.get('G', 0)
        pool.colorless = self.mana_pool.get('C', 0)
        return pool

    def to_dict(self) -> Dict:
        """Serialize player to JSON-compatible dict."""
        return {
            "name": self.name,
            "user_id": self.user_id,
            "is_claude": self.is_claude,
            "life": self.life,
            "poison": self.poison,
            "energy": self.energy,
            "seat_id": self.seat_id,
            "eliminated": self.eliminated,
            "loss_reason": self.loss_reason,
            "commander_damage": self.commander_damage,
            "library": [c.to_dict() for c in self.library],
            "hand": [c.to_dict() for c in self.hand],
            "battlefield": [c.to_dict() for c in self.battlefield],
            "graveyard": [c.to_dict() for c in self.graveyard],
            "exile": [c.to_dict() for c in self.exile],
            "command_zone": [c.to_dict() for c in self.command_zone],
            "companion_zone": [c.to_dict() for c in self.companion_zone],
            "deck_name": self.deck_name,
            "deck_source": self.deck_source,
            "lands_played_this_turn": self.lands_played_this_turn,
            "max_lands_per_turn": self.max_lands_per_turn,
            "has_drawn_for_turn": self.has_drawn_for_turn,
            "mulligans_taken": self.mulligans_taken,
            "has_kept_hand": self.has_kept_hand,
            "mana_pool": self.mana_pool,
            "restricted_mana_pool": self.restricted_mana_pool,
            "playable_from_exile": self.playable_from_exile,
            "playable_from_graveyard": self.playable_from_graveyard,
            "landfall_count_this_turn": self.landfall_count_this_turn,
            # Aug 8 (#2): the city's blessing is game-lifetime state
            # (CR 702.131c) — it must survive save/load and !undo.
            "city_blessing": self.city_blessing,
        }
    
    @classmethod
    def from_dict(cls, data: Dict) -> 'Player':
        """Reconstruct player from dict."""
        player = cls(
            name=data["name"],
            user_id=data.get("user_id"),
            is_claude=data.get("is_claude", False),
            life=data.get("life", 20),
            poison=data.get("poison", 0),
            energy=data.get("energy", 0),
            seat_id=data.get("seat_id"),
            eliminated=data.get("eliminated", False),
            loss_reason=data.get("loss_reason", ""),
            commander_damage=data.get("commander_damage", {}),
            deck_name=data.get("deck_name", ""),
            deck_source=data.get("deck_source", ""),
            lands_played_this_turn=data.get("lands_played_this_turn", 0),
            max_lands_per_turn=data.get("max_lands_per_turn", 1),
            has_drawn_for_turn=data.get("has_drawn_for_turn", False),
            mulligans_taken=data.get("mulligans_taken", 0),
            has_kept_hand=data.get("has_kept_hand", False),
            mana_pool=data.get("mana_pool", {'W': 0, 'U': 0, 'B': 0, 'R': 0, 'G': 0, 'C': 0}),
            restricted_mana_pool=data.get("restricted_mana_pool", []),
        )
        # Reconstruct card zones
        player.library = [Card.from_dict(c) for c in data.get("library", [])]
        player.hand = [Card.from_dict(c) for c in data.get("hand", [])]
        player.battlefield = [Card.from_dict(c) for c in data.get("battlefield", [])]
        player.graveyard = [Card.from_dict(c) for c in data.get("graveyard", [])]
        player.exile = [Card.from_dict(c) for c in data.get("exile", [])]
        player.command_zone = [Card.from_dict(c) for c in data.get("command_zone", [])]
        player.companion_zone = [Card.from_dict(c) for c in data.get("companion_zone", [])]
        player.playable_from_exile = data.get("playable_from_exile", [])
        player.playable_from_graveyard = data.get("playable_from_graveyard", [])
        player.landfall_count_this_turn = data.get("landfall_count_this_turn", 0)
        player.city_blessing = data.get("city_blessing", False)
        return player


_RESOLUTION_CARD_FIELDS = (
    "has_transform", "is_transformed", "back_face_name",
    "back_face_type_line", "back_face_oracle_text", "back_face_power",
    "back_face_toughness", "back_face_mana_cost", "front_face_name",
    "adventure_name", "adventure_cost", "adventure_text",
    "adventure_type", "cast_as_adventure", "split_names", "split_costs",
    "split_types", "split_texts", "cast_as_split_half",
)

_RESOLUTION_CAST_FACTS = (
    "_cast_origin", "_x_value", "_decimate_target_ids",
    "_cast_from_command_zone", "_cast_from_graveyard", "_escape_cost",
    "_was_escaped", "_cast_via_madness", "_was_spectacled", "_kicked",
    "_entwined", "_kicked_times", "_exile_after_resolution",
    "_buyback_paid", "_cast_via_foretell", "_cast_via_miracle",
    "_cast_via_effect", "_free_cast_source", "_is_spell_copy",
    "_colors_spent", "_snow_mana_spent", "_mana_paid", "_modes_chosen",
    "_tutor_card", "_tutor_cards", "_tutor_to_hand",
    "_tutor_to_graveyard", "_spliced_cards",
)


@dataclass(frozen=True)
class ResolutionTargetRef:
    """Exact persisted reference to one declared target.

    Names are presentation only.  Recovery resolves players by stable seat,
    cards by instance ID, and stack objects by stable stack-entry ID.  A stale
    reference resolves to ``None`` and is handled by ordinary CR 608.2b
    legality; it never redirects to a same-named object.
    """
    kind: str
    stable_id: str

    def to_dict(self) -> Dict:
        return {"kind": self.kind, "stable_id": self.stable_id}

    @classmethod
    def from_dict(cls, data: Dict) -> 'ResolutionTargetRef':
        return cls(kind=str(data.get("kind", "")),
                   stable_id=str(data.get("stable_id", "")))

    @classmethod
    def capture(cls, game: 'GameState', value: Any) -> Optional['ResolutionTargetRef']:
        if isinstance(value, Player):
            stable = game.priority_identifier(value)
            return cls("player", stable) if stable else None
        for entry in getattr(game, "stack", []) or []:
            if getattr(entry, "card", None) is value:
                stable = (getattr(entry, "entry_id", None)
                          or getattr(entry, "priority_id", None))
                return cls("stack", str(stable)) if stable else None
        card_id = getattr(value, "id", None)
        return cls("card", str(card_id)) if card_id else None

    def resolve(self, game: 'GameState') -> Any:
        if self.kind == "player":
            return game.player_from_priority_identifier(
                self.stable_id, living_only=False, allow_legacy_name=False)
        if self.kind == "stack":
            for entry in getattr(game, "stack", []) or []:
                if (getattr(entry, "entry_id", None) == self.stable_id
                        or getattr(entry, "priority_id", None) == self.stable_id):
                    return getattr(entry, "card", None)
            return None
        if self.kind == "card":
            found = game.find_card_global(self.stable_id)
            return found[0] if found else None
        return None


@dataclass
class ResolutionEvent:
    """One trigger or SBA death created during a resolution, by stable id.

    Q-J slice 4.  The in-memory queues these mirror hold LIVE ``Card``
    references (``game.pending_async_triggers``) or ``(Card, Player)`` tuples
    (``game._recently_died``), so neither can be written to a JSON snapshot —
    and neither was.  A save taken while either queue was non-empty dropped
    every entry SILENTLY: a queued Blood Artist trigger round-tripped to
    ``[]`` rather than to an error.  Silent omission is exactly what the Q-J
    requirements forbid, which is why the durable record is by id.

    CHOSEN SEMANTIC — at-most-once, matching the action ledger.  A record is
    marked ``dispatched`` BEFORE the trigger resolves, so a process death
    mid-drain drops that one trigger rather than firing it twice.  The
    direction is the same argument the module docstring in mtg/resolution.py
    makes for actions, and it applies with MORE force here: a drained trigger
    resolves through Tier 3, which mints a fresh plan every time, so a
    re-dispatch would apply a second plan's mutations on top of the first.
    A dropped Blood Artist drain is visible and fixable at the table; a
    doubled one is an illegal state nobody can unwind.
    """
    event_id: str
    kind: str                      # "trigger" | "death"
    source_id: str
    source_name: str
    controller_name: str = ""
    trigger_text: str = ""
    trigger_type: str = ""
    context: str = ""
    occurrence_key: Optional[str] = None
    job_id: str = ""
    dispatched: bool = False

    def to_dict(self) -> Dict:
        return {
            "event_id": self.event_id,
            "kind": self.kind,
            "source_id": self.source_id,
            "source_name": self.source_name,
            "controller_name": self.controller_name,
            "trigger_text": self.trigger_text,
            "trigger_type": self.trigger_type,
            "context": self.context,
            "occurrence_key": self.occurrence_key,
            "job_id": self.job_id,
            "dispatched": self.dispatched,
        }

    @classmethod
    def from_dict(cls, data: Dict) -> "ResolutionEvent":
        return cls(
            event_id=str(data.get("event_id", "")),
            kind=str(data.get("kind", "trigger")),
            source_id=str(data.get("source_id", "")),
            source_name=str(data.get("source_name", "")),
            controller_name=str(data.get("controller_name", "")),
            trigger_text=str(data.get("trigger_text", "")),
            trigger_type=str(data.get("trigger_type", "")),
            context=str(data.get("context", "")),
            occurrence_key=data.get("occurrence_key"),
            job_id=str(data.get("job_id", "")),
            dispatched=bool(data.get("dispatched", False)),
        )


@dataclass
class NarrationEntry:
    """One user-visible line, with a stable id and a sent acknowledgement.

    Q-J requires that a restart neither DUPLICATES nor OMITS visible events.
    Those are two different failure modes and they pull in opposite
    directions, so both halves are recorded rather than one:

      - omission is prevented by enqueuing (and persisting) BEFORE the send,
        so a line whose send never completed is still on disk afterwards;
      - duplication is prevented by the ack, so a line already delivered is
        never re-sent, and by the id being deterministic per scope+position,
        so a replayed resolution re-derives the SAME id instead of minting a
        second entry for the same line.
    """
    message_id: str
    content: str
    sent: bool = False
    scope: str = "loose"

    def to_dict(self) -> Dict:
        return {
            "message_id": self.message_id,
            "content": self.content,
            "sent": self.sent,
            "scope": self.scope,
        }

    @classmethod
    def from_dict(cls, data: Dict) -> "NarrationEntry":
        return cls(
            message_id=str(data.get("message_id", "")),
            content=str(data.get("content", "")),
            sent=bool(data.get("sent", False)),
            scope=str(data.get("scope", "loose")),
        )


@dataclass
class ResolutionJob:
    """Durable continuation record for one spell/ability resolution.

    Q-I persists enough identity and cast truth to reconstruct a real
    ``StackEntry`` after process death.  Q-J owns deterministic action plans,
    idempotent application, and the narration outbox; those fields live here
    now so later checkpoints extend this record rather than inventing a
    second persistence seam.
    """
    job_id: str
    card_snapshot: Dict
    controller_id: str
    target_refs: List[ResolutionTargetRef] = field(default_factory=list)
    checkpoint: str = "costs_paid"
    priority_id: Optional[str] = None
    is_spell: bool = True
    is_copy: bool = False
    countered: bool = False
    trigger_source: Optional[str] = None
    trigger_text: Optional[str] = None
    cast_facts: Dict = field(default_factory=dict)
    modes: List[Any] = field(default_factory=list)
    additional_cost: int = 0
    final_destination_policy: str = "normal"
    replacement_effect_ids: List[str] = field(default_factory=list)
    unresolved_choice_ids: List[str] = field(default_factory=list)
    planned_actions: List[Dict] = field(default_factory=list)
    applied_action_keys: List[str] = field(default_factory=list)
    # Q-J slice 4: ids of the trigger/SBA events this resolution created.
    trigger_event_ids: List[str] = field(default_factory=list)
    recovery_error: str = ""

    @staticmethod
    def _snapshot_card(card: Card) -> Dict:
        snapshot = card.to_dict()
        for name in _RESOLUTION_CARD_FIELDS:
            value = getattr(card, name, None)
            snapshot[name] = list(value) if isinstance(value, tuple) else value
        return snapshot

    @staticmethod
    def _restore_card(snapshot: Dict, cast_facts: Optional[Dict] = None) -> Card:
        card = Card.from_dict(snapshot)
        for name in _RESOLUTION_CARD_FIELDS:
            if name in snapshot:
                setattr(card, name, snapshot[name])
        for name, value in (cast_facts or {}).items():
            if name == "_spliced_cards":
                # Stable IDs are rebound after every zone/stack object exists.
                continue
            if name == "_colors_spent" and isinstance(value, list):
                value = tuple(value)
            setattr(card, name, value)
        return card

    @classmethod
    def capture(cls, game: 'GameState', entry: 'StackEntry', *,
                checkpoint: str = "on_stack", additional_cost: int = 0
                ) -> 'ResolutionJob':
        raw_targets = (list(entry.target)
                       if isinstance(entry.target, (list, tuple))
                       else ([entry.target] if entry.target is not None else []))
        refs = [ref for ref in
                (ResolutionTargetRef.capture(game, value)
                 for value in raw_targets) if ref is not None]
        facts = {}
        for name in _RESOLUTION_CAST_FACTS:
            if not hasattr(entry.card, name):
                continue
            value = getattr(entry.card, name)
            if name == "_spliced_cards":
                value = [getattr(item, "id", "") for item in value]
            elif isinstance(value, tuple):
                value = list(value)
            facts[name] = value
        controller_id = game.priority_identifier(entry.controller_index) or ""
        job_id = entry.entry_id or f"resolution-{uuid.uuid4().hex}"
        return cls(
            job_id=job_id,
            card_snapshot=(cls._snapshot_card(entry.card)
                           if entry.card is not None else {
                               "name": entry.trigger_source or "Triggered ability",
                               "id": f"trigger-source-{entry.entry_id}",
                               "type_line": "Ability",
                               "oracle_text": entry.trigger_text or "",
                           }),
            controller_id=controller_id,
            target_refs=refs,
            checkpoint=checkpoint,
            priority_id=entry.priority_id,
            is_spell=entry.is_spell,
            is_copy=bool(getattr(entry.card, "_is_spell_copy", False)),
            countered=entry.countered,
            trigger_source=entry.trigger_source,
            trigger_text=entry.trigger_text,
            cast_facts=facts,
            modes=list(getattr(entry.card, "_modes_chosen", []) or []),
            additional_cost=int(additional_cost or 0),
            final_destination_policy=(
                getattr(entry.card, "_exile_after_resolution", "")
                or ("buyback" if getattr(entry.card, "_buyback_paid", False)
                    else "normal")),
        )

    def to_dict(self) -> Dict:
        return {
            "job_id": self.job_id,
            "card_snapshot": self.card_snapshot,
            "controller_id": self.controller_id,
            "target_refs": [ref.to_dict() for ref in self.target_refs],
            "checkpoint": self.checkpoint,
            "priority_id": self.priority_id,
            "is_spell": self.is_spell,
            "is_copy": self.is_copy,
            "countered": self.countered,
            "trigger_source": self.trigger_source,
            "trigger_text": self.trigger_text,
            "cast_facts": self.cast_facts,
            "modes": self.modes,
            "additional_cost": self.additional_cost,
            "final_destination_policy": self.final_destination_policy,
            "replacement_effect_ids": self.replacement_effect_ids,
            "unresolved_choice_ids": self.unresolved_choice_ids,
            "planned_actions": self.planned_actions,
            "applied_action_keys": self.applied_action_keys,
            "trigger_event_ids": self.trigger_event_ids,
            "recovery_error": self.recovery_error,
        }

    @classmethod
    def from_dict(cls, data: Dict) -> 'ResolutionJob':
        return cls(
            job_id=str(data.get("job_id", "")),
            card_snapshot=dict(data.get("card_snapshot") or {}),
            controller_id=str(data.get("controller_id", "")),
            target_refs=[ResolutionTargetRef.from_dict(item)
                         for item in data.get("target_refs", [])],
            checkpoint=str(data.get("checkpoint", "costs_paid")),
            priority_id=data.get("priority_id"),
            is_spell=bool(data.get("is_spell", True)),
            is_copy=bool(data.get("is_copy", False)),
            countered=bool(data.get("countered", False)),
            trigger_source=data.get("trigger_source"),
            trigger_text=data.get("trigger_text"),
            cast_facts=dict(data.get("cast_facts") or {}),
            modes=list(data.get("modes") or []),
            additional_cost=int(data.get("additional_cost", 0) or 0),
            final_destination_policy=str(
                data.get("final_destination_policy", "normal")),
            replacement_effect_ids=list(
                data.get("replacement_effect_ids") or []),
            unresolved_choice_ids=list(
                data.get("unresolved_choice_ids") or []),
            planned_actions=list(data.get("planned_actions") or []),
            applied_action_keys=list(data.get("applied_action_keys") or []),
            trigger_event_ids=list(data.get("trigger_event_ids") or []),
            recovery_error=str(data.get("recovery_error", "")),
        )


@dataclass
class StackEntry:
    """An object on the stack (spell or triggered ability)."""
    card: Card                              # The card being cast / source of trigger
    controller_name: str                    # Name of the player who controls this
    controller_index: int                   # Index in game.players
    target: Any = None                      # Target (Card, Player, or None)
    is_spell: bool = True                   # True = spell, False = triggered/activated ability
    trigger_source: Optional[str] = None    # Source card name for triggered abilities
    trigger_text: Optional[str] = None      # Oracle text of the trigger
    countered: bool = False                 # Marked True when countered by a counterspell
    # Per-spell resolution signal (enables stack wars — each spell waits on its own event)
    resolution_event: Any = field(default=None, repr=False)
    # Links this StackEntry to the corresponding StackObject.id in PrioritySystem
    priority_id: Optional[str] = None
    # July 31 batch-10 audit: how many end-of-turn cleanups this entry has
    # survived. A LIVE entry (unresolved resolution_event — its coroutine is
    # still choreographing) is preserved across ONE turn boundary so the
    # stack machinery can finish resolving it; surviving a second cleanup
    # means the coroutine leaked and the entry is swept as truly stale.
    cleanup_survivals: int = 0
    # Stable identity independent of PrioritySystem's runtime stack object.
    entry_id: str = field(default_factory=lambda: f"resolution-{uuid.uuid4().hex}")
    resolution_job_id: Optional[str] = None
    target_refs: List[ResolutionTargetRef] = field(default_factory=list)

    def to_dict(self) -> Dict:
        return {
            "card_name": self.card.name if self.card else "Unknown",
            "card_id": self.card.id if self.card else "",
            "card_snapshot": (ResolutionJob._snapshot_card(self.card)
                              if self.card else {}),
            "controller_name": self.controller_name,
            "controller_index": self.controller_index,
            "target_refs": [ref.to_dict() for ref in self.target_refs],
            "is_spell": self.is_spell,
            "trigger_source": self.trigger_source,
            "trigger_text": self.trigger_text,
            "countered": self.countered,
            "priority_id": self.priority_id,
            "cleanup_survivals": self.cleanup_survivals,
            "entry_id": self.entry_id,
            "resolution_job_id": self.resolution_job_id,
        }

    def bind_persisted_targets(self, game: 'GameState') -> None:
        """Resolve exact target references after the whole stack exists.

        Stack targets (counter-wars) cannot be rebound while entries are
        being constructed one at a time.  This deliberately runs as a
        second pass and never falls back to a display/card name.
        """
        resolved = [ref.resolve(game) for ref in self.target_refs]
        resolved = [value for value in resolved if value is not None]
        self.target = (resolved if len(self.target_refs) > 1 else
                       (resolved[0] if resolved else None))
        job = game.resolution_jobs.get(
            str(self.resolution_job_id or self.entry_id))
        if job is not None and "_spliced_cards" in job.cast_facts:
            rebound = []
            for card_id in job.cast_facts.get("_spliced_cards") or []:
                found = game.find_card_global(str(card_id))
                if found:
                    rebound.append(found[0])
            self.card._spliced_cards = rebound

    @classmethod
    def from_dict(cls, game: 'GameState', data: Dict,
                  job: Optional[ResolutionJob] = None) -> 'StackEntry':
        snapshot = ((job.card_snapshot if job else None)
                    or data.get("card_snapshot") or {
                        "name": data.get("card_name", "Unknown"),
                        "id": data.get("card_id", ""),
                    })
        card = ResolutionJob._restore_card(
            snapshot, job.cast_facts if job else None)
        refs = (job.target_refs if job is not None else
                [ResolutionTargetRef.from_dict(item)
                 for item in data.get("target_refs", [])])
        controller = (game.player_from_priority_identifier(
            job.controller_id, living_only=False, allow_legacy_name=False)
            if job is not None else None)
        try:
            controller_index = (game.players.index(controller)
                                if controller is not None
                                else int(data.get("controller_index", 0)))
            controller_name = game.players[controller_index].name
        except (ValueError, TypeError, IndexError):
            controller_index = 0
            controller_name = str(data.get("controller_name", ""))
        return cls(
            card=card,
            controller_name=controller_name,
            controller_index=controller_index,
            # Bound in a second pass after every restored stack entry exists.
            target=None,
            is_spell=(job.is_spell if job else bool(data.get("is_spell", True))),
            trigger_source=(job.trigger_source if job else data.get("trigger_source")),
            trigger_text=(job.trigger_text if job else data.get("trigger_text")),
            countered=(job.countered if job else bool(data.get("countered", False))),
            priority_id=(job.priority_id if job else data.get("priority_id")),
            cleanup_survivals=int(data.get("cleanup_survivals", 0) or 0),
            entry_id=str(data.get("entry_id") or
                         (job.job_id if job else f"resolution-{uuid.uuid4().hex}")),
            resolution_job_id=(job.job_id if job else data.get("resolution_job_id")),
            target_refs=list(refs),
        )


@dataclass
class GameState:
    """Full game state."""
    thread_id: int
    format: str
    players: List[Player]

    # Turn tracking
    turn_number: int = 0
    active_player_index: int = 0
    priority_player_index: int = 0
    phase: Phase = Phase.MAIN1

    def set_phase(self, new_phase: 'Phase', via: str = "") -> None:
        """The ONE sanctioned way to change game.phase (pub/sub slice 6).

        Emits PHASE_CHANGED; since the 6b flip (July 31, 2026) the
        MAIN-phase trigger dispatch IS a subscriber
        (mtg/triggers.py:_main_phase_bus_subscriber), so every MAIN1/MAIN2
        entry runs it with no caller cooperation — the direct-phase-set
        class (game.phase = Phase.MAIN2 bypassing the dispatch) produced
        the Tymna bug three times over (July 27 scan unwired, July 29 F3-C,
        July 30 F3) and is now structurally dead. A permanent structural
        pin (tests/test_slice6b_phase_bus.py) forbids raw assignments in
        the engine; construction / from_dict / !undo set the FIELD directly
        — restoration is not a game transition, so no hooks and no
        emission. `via` names the call site for log forensics.
        """
        old = self.phase
        self.phase = new_phase
        from mtg import events
        events.emit(events.PHASE_CHANGED, self, old_phase=old,
                    new_phase=new_phase, via=via)

    # Stack (list of StackEntry objects when stack_enabled, else empty list)
    stack: List = field(default_factory=list)

    # Durable CR 608 continuation records keyed by StackEntry.entry_id.  Jobs
    # remain after the physical stack object is popped so a process death in
    # the resolving/effect-routing window still has an exact continuation
    # record. Completed jobs are retained for audit/idempotency evidence and
    # may be compacted by a later journal policy.
    resolution_jobs: Dict[str, ResolutionJob] = field(default_factory=dict)
    # Q-J slice 2: durable narration. Bounded — see prune_narration().
    narration_outbox: List[NarrationEntry] = field(default_factory=list)
    # Q-J slice 4: durable trigger/SBA events keyed by event_id.  The
    # in-memory queues these mirror (pending_async_triggers, _recently_died)
    # hold live objects and cannot serialize; this is the half that can.
    # Bounded — see prune_resolution_events().
    resolution_events: Dict[str, ResolutionEvent] = field(default_factory=dict)

    # Stack/priority feature flag — when True, spells go on stack with priority passes
    stack_enabled: bool = False

    # PrioritySystem instance (only created when stack_enabled=True)
    _priority_system: Any = field(default=None, repr=False)

    # Recreated by mtg.resolution after load; never serialized directly.
    _resolution_coordinator: Any = field(
        default=None, repr=False, compare=False)

    # JSON snapshot consumed by GameEngine.setup_stack after load/undo. The
    # live PrioritySystem remains runtime-only because it owns tasks/callbacks.
    _restored_priority_state: Optional[Dict] = field(
        default=None, repr=False, compare=False)

    # Async event for waiting on stack resolution (legacy fallback)
    _stack_resolution_event: Any = field(default=None, repr=False)

    # Combat priority window — set during combat priority rounds (e.g., "after_attackers")
    combat_priority_window: Optional[str] = None

    # Autoplay mode — suppress human-facing prompts (!judge, !respond, !pass, etc.)
    is_autoplay: bool = False
    # Explicit marker for the bounded four-seat cube smoke path. Ordinary
    # two-player games and the shipped cube bracket remain unchanged.
    experimental_ffa: bool = False
    # Track which effects have already emitted a !judge/!resolve hint (dedup in autoplay)
    _judge_hints_emitted: set = field(default_factory=set, repr=False)

    # ---- Transient runtime state (rebuilt during play; NOT serialized —
    # to_dict is hand-written, so save/load and !undo restore defaults and the
    # engine repopulates). Same convention as Card/Player: declare runtime
    # flags here, don't staple (tests/test_ratchets.py ratchets the count). ----
    # July 21 batch audit: back-reference to the RulesEngine, consumed by
    # rules/spell_resolver.py (noncombat damage → replacement effects + SBA)
    # and Player._apply_sac_cost_at_tap (Phyrexian Tower dies triggers).
    # Was only ever assigned in TESTS — the May 30 D2 / June 10 C4 wiring
    # silently no-opped in every live game. GameEngine now stamps it at
    # game creation and load.
    _rules_engine: object = field(default=None, repr=False, compare=False)
    # (card, player) tuples queued for dies-trigger processing (SBA loop,
    # board wipes, Living Death F-LD1). Drained by the trigger dispatcher.
    _recently_died: list = field(default_factory=list, repr=False, compare=False)
    # Snapshot currently being dispatched. Deaths caused by those triggers go
    # into _recently_died as a separate wave, so a source that died in wave 1
    # cannot incorrectly see later wave-2 deaths.
    _active_dies_batch: list = field(default_factory=list, repr=False, compare=False)
    # Some effects (Living Death) return new permanents before queued dies
    # triggers dispatch. Record which permanents actually existed when each
    # death happened so returned cards do not trigger retroactively.
    _dies_source_ids_by_dead_id: dict = field(default_factory=dict, repr=False, compare=False)
    # Aug 10 deferred (C4): "whenever cards leave your graveyard" watchers.
    # _graveyard_snapshot is {player_index: {card_id: Card}} as of the last
    # observation; triggers.observe_graveyard_exits diffs against it and emits
    # CARDS_LEFT_GRAVEYARD for anything that vanished. An ABSENT entry seeds
    # without firing, which is what makes a fresh object safe — !undo swaps in
    # a restored GameState and save/load rebuilds one, and neither must read as
    # "the whole graveyard just left".
    _graveyard_snapshot: dict = field(default_factory=dict, repr=False, compare=False)
    # (card, owner) pairs accumulated by the bus subscriber, drained as ONE
    # batch: the batch IS the event for "one or more cards leave" (Tormod
    # fires once no matter how many left), while per-object clauses (Syr
    # Konrad) fire once per card in it.
    _cards_left_graveyard: list = field(default_factory=list, repr=False, compare=False)
    # {watcher card id: turn_number} for the printed "This ability triggers
    # only once each turn" cap (Oasis of Renewal, Kishla Skimmer) — a THIRD
    # firing arithmetic beyond consolidated/per-object.
    _gy_leave_once_turn: dict = field(default_factory=dict, repr=False, compare=False)
    # Strategist rejection backoff is per game: 25 concurrent autoplay games
    # share one ClaudePlayer, so these counters must never live on the client.
    _strategy_rejection_streak: int = field(default=0, repr=False, compare=False)
    _strategy_backoff_turns: int = field(default=0, repr=False, compare=False)
    # July 20: adaptive strategist degrade. Deadman/hard-cap fires are
    # counted per game (same shared-client reasoning as above); after 2
    # fires the game's remaining strategist calls drop reasoning_effort to
    # "low" ([STRATEGIST-DEGRADE]). The July 12-13 batch had 248 fires vs
    # the 0-2/batch healthy baseline on a bad-DeepSeek day; the deadman
    # already caps each hang, this stops paying the 90s tax repeatedly.
    _strategist_fires: int = field(default=0, repr=False, compare=False)
    _strategist_degraded: bool = field(default=False, repr=False, compare=False)
    # (Slices 2c + 3c, July 24, 2026: the PERMANENT_ENTERED and CREATURE_DIED
    # parity-tracking fields were retired with their recorders — clean
    # post-flip batches at [EVENT-PARITY]=0 and [EVENT-PARITY-DIES]=0.)
    # Slice 4a (July 24, 2026): CARD_CAST shadow parity — (card_id, name,
    # via) tuples recorded by the bus subscriber, and the per-cast ids the
    _strategy_task: Any = field(default=None, repr=False, compare=False)
    _strategy_memo: str = field(default="", repr=False, compare=False)
    # Cross-system display queue: trigger messages produced by sync helpers
    # (Phyrexian Tower dies-triggers, gain-life triggers) — flushed by the
    # next display pass.
    _pending_messages: list = field(default_factory=list, repr=False, compare=False)
    # A wheel discards several cards as one event. These stamps let discard
    # triggers deduplicate that wave without stapling invisible game state.
    _discard_event_serial: int = field(default=0, repr=False, compare=False)
    _active_discard_event_id: Optional[int] = field(
        default=None, repr=False, compare=False)
    # Re-entrancy guard for "whenever you gain life" trigger resolution
    # (June 10, V26).
    _in_gain_life_triggers: bool = field(default=False, repr=False, compare=False)
    # Re-entrancy guard for "whenever <someone> loses life" (Aug 10, C2).
    # Not defensive: Sanguine Bond / Exquisite Blood is the classic mutual
    # loop in exactly this family.
    _in_life_lost_triggers: bool = field(default=False, repr=False, compare=False)
    # Re-entrancy guard for "whenever a permanent becomes untapped" (Aug 10, C3).
    _in_untap_triggers: bool = field(default=False, repr=False, compare=False)
    # June 10 (C3/V28): positional cast→resolve pairing stamp — set by the
    # cast/activate branches of the two action executors, consumed by their
    # resolve branches to drop redundant/orphan free-text resolves.
    _last_exec_cast_like: Any = field(default=None, repr=False, compare=False)
    # July 20 batch-3 audit: (turn_number, card_name, msg) of the most recent
    # FAILED cast_spell_async. Both action executors print the real failure
    # reason then returned None, so _get_action_error re-derived a reason from
    # scratch — and for aura/graveyard-target failures (Animate Dead has no
    # literal "target" in its oracle text) fell through to "unknown reason —
    # mana looks sufficient", feeding the AI a wrong reason it retried against
    # (283 of 588 [MANA-DIVERGENCE] lines in the 15289 batch were non-mana
    # failures). Consumed (and cleared) by _get_action_error.
    _last_cast_failure: Any = field(default=None, repr=False, compare=False)
    # July 30 batch-9 (deferred July 29 item): same stash for ACTIVATIONS —
    # Rhys the Redeemed failed 8 activations across 24 turns with feedback of
    # None/'' because the activate failure sites just returned None and
    # _get_action_error's activate branch re-derived nothing (no summoning-
    # sickness or affordability checks). (turn, permanent_name, message);
    # consumed (and cleared) by _get_action_error.
    _last_activation_failure: Any = field(default=None, repr=False, compare=False)
    # Aug 7 2026 (confirmation-batch audit, B-5): (turn, reason) stashed when
    # an {"type": "attack"} action names only ineligible creatures — the
    # branch used to fall through returning bare None and the AI burned 3
    # retries on "unknown reason". Consumed by _get_action_error.
    _last_attack_action_failure: Any = field(default=None, repr=False, compare=False)
    # Aug 7 batch audit (C-4): same stash for deliberately-DROPPED redundant
    # `resolve` actions — the June-10 positional-pairing drop returned bare
    # None, surfacing to the AI as "Action failed (unknown reason)", so the
    # model re-proposed the identical resolve, burning a retry AND a Tier-3
    # judge call (two games in the e4057a0 batch). (turn, message); consumed
    # by _get_action_error's resolve branch.
    _last_resolve_drop_reason: Any = field(default=None, repr=False, compare=False)
    # July 24 batch-6: aura auto-target fizzle context — set when a beneficial
    # aura declines an opponent-only legal-target board so the fizzle message
    # can say "declined" instead of the misleading "no creature you control".
    # Consumed (and cleared) at the fizzle-message emit in mtg/spells.py.
    _aura_fizzle_note: Any = field(default=None, repr=False, compare=False)
    # Pub/sub slice 5b (July 31, 2026): the combat-damage trigger queues are
    # BUS-FED — _accumulate_combat_damage_subscriber (mtg/triggers.py) is the
    # sole sanctioned appender for both (the slice-3b pattern: subscribers
    # accumulate, the drain in resolve_combat_damage keeps its batch
    # semantics and its `not game.ended` gate). Player-kind entries are
    # (source_card, source_owner, amount, damaged_player); creature-kind entries are
    # (source_card, damaged_creature, amount) for the damaged-creature scan
    # (Phyrexian Obliterator class). The 5a parity recorder and its
    # _cdd_bus_seen/_cdd_consumer_seen scaffolding were retired at the flip.
    _combat_damage_to_player: list = field(default_factory=list, repr=False, compare=False)
    _combat_damage_to_creature: list = field(default_factory=list, repr=False, compare=False)
    # (Slice 6a's _phase_emissions/_phase_hook_runs parity scaffolding was
    # retired at the 6b flip, July 31 — the MAIN-phase dispatch is a
    # PHASE_CHANGED subscriber now, so hook pairing is structural.)
    # Madness (CR 702.35): (card, owner_index) pairs discarded into exile by
    # helpers.madness_discard_to_exile, awaiting the cast-or-graveyard choice
    # at the next async drain (spells.resolve_pending_madness, invoked from
    # drain_pending_triggers). Sync discard sites can't cast, so the pending
    # list bridges — the same sync-gap convention as the Tier-3 trigger
    # queue ([QUEUE-*] → [DRAIN-*]).
    _madness_pending: list = field(default_factory=list, repr=False, compare=False)
    # Miracle (CR 702.94), Aug 3 2026 — the same sync-gap bridge as madness:
    # draws are sync and casting is not, so the draw hook parks
    # (card, owner_index) here and spells.resolve_pending_miracles makes the
    # cast-or-keep call at the next async drain.
    _miracle_pending: list = field(default_factory=list, repr=False, compare=False)
    # Effect-granted casts (Rashmi, Capricious Hellraiser). Their producers
    # are synchronous template/action paths, while casting must traverse the
    # async stack pipeline, so descriptors wait here for an async choke point.
    _free_cast_pending: list = field(
        default_factory=list, repr=False, compare=False)
    # Dredge (CR 702.52) replaces at most ONE draw per turn PER PLAYER — see
    # helpers.try_dredge for why. Seat indices; reset with the per-turn
    # counters.
    _dredged_this_turn: set = field(default_factory=set, repr=False, compare=False)
    # Spell Queller bookkeeping: source card name → [(exiled_card, owner_name)]
    # (exile_from_stack records; release_queller_exile drains on LTB).
    _queller_exiles: dict = field(default_factory=dict, repr=False, compare=False)
    # July 31 batch-10: "exiled with it" linkage for Underworld Sentinel-class
    # cards. Key f"{source_name_lower}|{controller_name}" → list of exiled
    # card names. The dies-side generator VERIFIES each name is still in the
    # owner's exile before emitting a return, so a failed exile self-heals.
    _linked_exiles: dict = field(default_factory=dict, repr=False, compare=False)
    # July 31 batch-10: Yidris, Maelstrom Wielder's grant — player name →
    # turn number on which their hand-cast spells gain cascade (set by the
    # grant_hand_cascade action; consulted by the cascade block in
    # mtg/triggers.py). Self-expires on turn mismatch.
    _hand_cascade_grants: dict = field(default_factory=dict, repr=False, compare=False)
    # Chapter abilities whose tax duration is independent of their source
    # permanent (Elspeth Conquers Death II). Entries are JSON-compatible.
    _temporary_cost_increases: list = field(
        default_factory=list, repr=False, compare=False)
    # Per-turn byte-identical Discord message counts (trigger-burst dedup
    # Layer 3 in _autoplay_send). Reset at turn advance.
    _turn_burst_counts: dict = field(default_factory=dict, repr=False, compare=False)
    # Oracle-text dedup keys already shown this game (format_trigger_line /
    # format_activate_line emit short form on 2nd+ fire).
    _oracle_shown_keys: set = field(default_factory=set, repr=False, compare=False)
    # (card_name, controller_name) of the spell/ability currently resolving —
    # lets action handlers attribute damage / validate targets without every
    # caller threading source metadata explicitly.
    _current_resolution_source: Any = field(default=None, repr=False, compare=False)
    # Q-J slice 1: the durable ResolutionJob id whose effect is executing
    # right now, so the Tier-3 loop can persist its plan and claim each
    # action idempotently. Stamped by _dispatch_resolution, which is the
    # only place a cast's job is unambiguously in scope; resolve_effect
    # has 23 call sites and threading a kwarg through them all would put
    # the identity in the callers rather than at the one seam that owns
    # it. None means "no durable job" (trigger drains, manual
    # activations) and every Q-J path degrades to today's behaviour.
    _active_resolution_job_id: Any = field(default=None, repr=False, compare=False)
    # !undo snapshot stack (list of to_dict snapshots; depth-capped in cog).
    _undo_stack: list = field(default_factory=list, repr=False, compare=False)
    # Discord thread object for the running autoplay game (carried over on !undo).
    _autoplay_thread: Any = field(default=None, repr=False, compare=False)
    # Developer-only autoplay card exercise evidence. A fresh GameState gets
    # fresh lists, so a seed cannot leak into the next game. Injected cache
    # cards replace (rather than append to) an opening-hand card; the displaced
    # objects stay here for exact inventory accounting during the run.
    _card_seed_results: list = field(default_factory=list, repr=False, compare=False)
    _seed_replaced_cards: list = field(default_factory=list, repr=False, compare=False)
    _panharmonicon_repeat_active: bool = field(default=False, repr=False, compare=False)
    # Automated multiplayer turns choose a concrete opponent seat up front.
    # Ambiguous labels such as "opponent" resolve only through this recorded
    # choice; they never silently mean "the first other player" in an FFA.
    _default_opponent_index: Optional[int] = field(
        default=None, repr=False, compare=False)

    # Opt-in: put triggered abilities on the stack instead of resolving immediately
    triggers_use_stack: bool = False

    # Combat
    attackers: List[str] = field(default_factory=list)  # Card IDs
    blockers: Dict[str, List[str]] = field(default_factory=dict)  # attacker_id -> [blocker_ids]
    
    # Game state
    started: bool = False
    ended: bool = False
    winner: Optional[int] = None
    # Stable seat ids in elimination order. Persisted so a headless FFA
    # result and a save/undo snapshot agree about placements.
    elimination_order: List[int] = field(default_factory=list)
    created_at: datetime = field(default_factory=datetime.now)
    
    # Pending confirmations
    pending_action: Optional[Dict] = None

    # Q-F production choice sessions. Records are JSON-safe and stable across
    # reconnect/save. Opaque payloads and asyncio futures live separately.
    pending_choices: Dict[str, Dict] = field(default_factory=dict)
    _choice_runtime: Dict[str, Dict] = field(
        default_factory=dict, repr=False, compare=False)

    # Last unresolved effect (for argumentless !judge)
    last_unresolved_effect: Optional[Dict] = None

    # Pending unresolved effects the AI should know about (ETBs, triggers, etc.)
    # Items are strings like "Mystic Sanctuary ETB — put instant/sorcery from graveyard on top of library"
    # Cleared when the AI resolves them or at end of turn
    pending_resolves: List[str] = field(default_factory=list)

    # Sync Trigger Queue — triggers fired from sync paths (advance_phase, end_turn,
    # _handle_etb_triggers, SBA loops) that couldn't be resolved by Tier 1/1.5 and
    # therefore need async Tier 3 resolution. Drained by GameEngine.drain_pending_triggers()
    # from async command handlers and the autoplay loop. Each entry:
    #   {"source_card": Card, "trigger_text": str, "trigger_type": str,
    #    "controller_name": str, "context": str}
    # See CLAUDE.md "Known Limitation: Sync Trigger Gap".
    pending_async_triggers: List[Dict] = field(default_factory=list)
    
    # Turn-based effects (cleared at end of turn)
    # Format: [{"type": "on_attack_damage", "source": "Jaya", "target_id": "xyz", "calc": "num_attackers", "controller": 0}, ...]
    turn_effects: List[Dict] = field(default_factory=list)

    # Delayed triggers — fire at a future phase (end step, upkeep, etc.)
    # Format: [{"trigger_at": "end_step"|"upkeep", "actions": [...], "source": "Card Name",
    #           "controller": player_idx, "once": True, "turn_delay": 0}]
    delayed_triggers: List[Dict] = field(default_factory=list)

    # July 23 follow-up: turn number on which "damage can't be prevented this
    # turn" is active (Insult // Injury's first clause). -1 = never. Checked by
    # helpers.damage_prevention_disabled at every prevention gate; self-expires
    # because it only matches an exact turn number.
    _damage_prevention_off_turn: int = field(default=-1, repr=False, compare=False)

    # July 29 batch audit: >0 while a [CAST-TRIGGER-PRIORITY] window (the
    # Stifle-response LLM evaluation over a cast trigger) is open. The buried
    # spell's LIFO wait loop in mtg/spells.py reads it so an in-flight window
    # never burns the extension/rescue budget — batch 15315 resolved Beast
    # Whisperer beneath the Arcane Denial targeting it because the window's
    # evaluation outlived the whole budget (CR 608 violation, counter robbed).
    _trigger_window_depth: int = field(default=0, repr=False, compare=False)
    # Aug 1 2026 (deferred slate): pending additional combat phases this
    # turn (Moraug landfall — the original producer — plus Port Razer's
    # connect and Karlach's attack trigger via the additional_combat
    # action). Consumed by the autoplay HUMAN turn loop only; the Claude
    # path breadcrumbs + resets (documented gap, pre-existing for Moraug),
    # and end_turn resets it so a producer firing on one player's turn can
    # never grant the NEXT player a phantom extra combat (the stale-value
    # leak the sweep exists for).
    _additional_combats: int = field(default=0, repr=False, compare=False)
    # Aug 2 2026 (batch-14 audit, R-M3): pending "at the beginning of that
    # combat, untap all creatures you control" riders on granted extra
    # combats (Moraug, Fury of Akoum). CR 603.7 — the untap belongs to the
    # START of the granted combat, not to the landfall trigger that granted
    # it; running it inline untapped 0 creatures every time (land drops
    # happen in a main phase, before anything has attacked) and the extra
    # combat then had no eligible attackers. Consumed one-per-phase by the
    # extra-combat loops; reset alongside _additional_combats.
    _extra_combat_untaps: int = field(default=0, repr=False, compare=False)
    # Aug 2 2026: MORBID (CR 207.2c) — 'if a creature died this turn'.
    # Stamped by the CREATURE_DIED accumulator (the one choke point every
    # death path reaches) and cleared at turn advance. The wave-scoped
    # _recently_died list is reset mid-turn by the dies dispatcher, so it
    # structurally cannot answer a whole-turn question.
    _creature_died_this_turn: bool = field(default=False, repr=False, compare=False)
    # Aug 10 2026 (G2): per-turn spell counts captured in end_turn immediately
    # BEFORE the reset, because end-step triggers dispatch below it on that
    # path and would otherwise evaluate an "if you didn't cast a spell this
    # turn" intervening-if (CR 603.4) against an already-zeroed counter.
    # TURN-STAMPED: the advance_phase path fires end-step triggers before any
    # reset, where the live counter is authoritative, so a consumer must use
    # the snapshot ONLY when the stamp matches the current turn — otherwise it
    # reads a stale prior-turn value.
    _spells_cast_snapshot_turn: int = field(default=-1, repr=False, compare=False)
    _spells_cast_snapshot: dict = field(default_factory=dict, repr=False, compare=False)
    # Aug 7 2026 (confirmation-batch audit, A-2b/B-2): per-turn record of
    # which creature ids each dealer dealt COMBAT damage to this turn
    # (dealer card id -> set of damaged creature ids). Two consumers:
    # the "creature dealt damage by this creature this turn dies" dies-
    # watcher premise gate (Predator Ooze — Tier 3 fabricated the premise
    # in game_1535228613341872148), and equipment charge-counter triggers.
    # Populated in apply_combat_damage_to_creature; cleared at turn advance
    # beside the morbid flag.
    _creature_combat_damage_by_dealer: dict = field(default_factory=dict, repr=False, compare=False)
    # Aug 7 2026 (A-2b): equipment ids whose "equipped creature deals combat
    # damage -> charge counters" trigger already fired THIS damage step —
    # a trampler damaging blocker + player in one step is ONE damage event
    # (Jitte rulings: one trigger), while first-strike + regular steps are
    # two. Cleared at each resolve_combat_damage entry.
    _equip_charge_fired_ids: set = field(default_factory=set, repr=False, compare=False)
    # Aug 11 2026 (batch audit, reviewer D F5): blocker ids whose "whenever
    # this creature blocks" trigger has already fired THIS combat. The
    # first-strike and regular damage steps are two resolve_combat_damage
    # calls but ONE combat, so without this a blocking token-maker would
    # produce two tokens. Cleared everywhere `blockers` is cleared — i.e. at
    # each new declare-blockers, which is what makes extra combat phases
    # (Moraug, Aurelia, Port Razer) fire their block triggers again.
    _block_triggers_fired_ids: set = field(default_factory=set, repr=False, compare=False)
    # Stable-object delayed destruction for Gorgon Recluse's block trigger.
    # This is a combat-scoped queue, drained after combat damage/SBAs and
    # before callers clear the combat map.
    _end_of_combat_destructions: List[Dict] = field(default_factory=list, repr=False, compare=False)
    _gorgon_recluse_fired_pairs: set = field(default_factory=set, repr=False, compare=False)
    # Aug 2 2026 (batch-13 audit): True while the Moraug consumption loop is
    # running an ADDITIONAL combat phase. Karlach, Fury of Avernus's trigger
    # carries an intervening-if ("if it's the first combat phase of the
    # turn", CR 603.4) that must decline in extra combats — without the gate
    # her generator granted untap + first strike + another phase on every
    # attack, masked only by the loop-tail discard.
    _in_extra_combat: bool = field(default=False, repr=False, compare=False)
    # Aug 2 2026 (batch-13 audit): card name → turn number, stamped when a
    # "for the first time each turn" attack trigger fires (Aurelia, the
    # Warleader). A second attack the same turn (only reachable via an extra
    # combat) finds the stamp and declines per the printed condition.
    _attack_trigger_turn_stamps: dict = field(default_factory=dict, repr=False, compare=False)
    # Aug 2 2026 (batch-13): card id → turn number the impulse exile
    # happened. "Until the end of your NEXT turn" (Light Up the Stage) —
    # the old unconditional every-player end_turn clear ended the window the
    # same turn it opened. end_turn expires only the ENDING player's
    # previous-turn entries.
    _impulse_cast_turns: dict = field(default_factory=dict, repr=False, compare=False)
    # Card-id keyed "cast this exact exile object or take damage" windows
    # (Chandra, Torch of Defiance). Unlike ordinary impulse permissions this
    # state is persisted and never grants a land play.
    conditional_exile_casts: dict = field(default_factory=dict)
    # July 29 batch audit: True once a final=True game-summary send has gone
    # out. The post-game flush gate in cog._autoplay_send suppresses only
    # AFTER this — the ended→summary window carries the lethal combat's own
    # buffered messages, which were being eaten in ~150/152 games.
    _final_summary_posted: bool = field(default=False, repr=False, compare=False)

    # How many spells the player whose turn JUST ENDED cast during that turn.
    # Daybound's printed reminder is "If a player casts no spells during their
    # own turn, it becomes night next turn", so the day/night check at upkeep
    # must read the turn that just ended — not the incoming active player's own
    # previous turn, which in a two-player game is a turn older still. Written
    # in end_turn from the ending player's count, before the per-turn reset.
    _spells_cast_last_turn: int = field(default=0, repr=False, compare=False)

    # Combat flow: when Claude attacks a human, we pause for the human to declare blocks
    waiting_for_human_blocks: bool = False
    # Explicit pregame lock for production human lobbies. Legacy/headless
    # multiplayer constructors do not run Discord London mulligans and must
    # not be inferred as pending merely because Player.has_kept_hand defaults
    # false.
    opening_hands_pending: bool = False
    # Stable defender seats that have finalized blockers this combat. In a
    # multiplayer attack, damage waits until every attacked living seat has
    # submitted !doneblocking/!noblock. Persisted so reconnect/restart cannot
    # silently skip a defender's declaration.
    combat_defenders_done: List[int] = field(default_factory=list)

    # Day/Night tracking (CR 726) — for daybound/nightbound transform cards
    is_day: bool = True  # True = day, False = night (only meaningful when day_night_active)
    day_night_active: bool = False  # Becomes True when first daybound/nightbound card enters

    # Layers engine for continuous effects (static keyword grants, anthems, etc.)
    # Not serialized — rebuilt from battlefield state on game load
    _layers_engine: Any = field(default=None, repr=False)

    # Replacement effects engine ("if would, instead" processing)
    # Not serialized — rebuilt from battlefield state on game load
    _replacement_engine: Any = field(default=None, repr=False)
    
    @property
    def active_player(self) -> Player:
        return self.players[self.active_player_index]

    @property
    def is_multiplayer(self) -> bool:
        return len(self.players) > 2

    def living_player_indices(self) -> List[int]:
        """Stable seat indices for players still in the game."""
        return [
            index for index, player in enumerate(self.players)
            if not getattr(player, 'eliminated', False)
        ]

    def living_players_in_turn_order(
            self, start_index: Optional[int] = None) -> List[Player]:
        """Living players in cyclic seat order, beginning at start_index."""
        if not self.players:
            return []
        start = (self.active_player_index if start_index is None
                 else int(start_index)) % len(self.players)
        return [
            self.players[index]
            for offset in range(len(self.players))
            for index in [(start + offset) % len(self.players)]
            if not getattr(self.players[index], 'eliminated', False)
        ]

    def opponents_of(self, player: Player) -> List[Player]:
        """Living opponents in turn order after player (never list order)."""
        if player not in self.players:
            return []
        start = (self.players.index(player) + 1) % len(self.players)
        return [p for p in self.living_players_in_turn_order(start)
                if p is not player]

    def next_living_player_index(self, after_index: int) -> Optional[int]:
        """Next non-eliminated stable seat, or None when nobody remains."""
        if not self.players:
            return None
        for offset in range(1, len(self.players) + 1):
            index = (int(after_index) + offset) % len(self.players)
            if not getattr(self.players[index], 'eliminated', False):
                return index
        return None

    def default_opponent_for(self, player: Player) -> Optional[Player]:
        """Concrete opponent selected for singular automated choices."""
        if player not in self.players:
            return None
        chosen = self._default_opponent_index
        if chosen is not None and 0 <= chosen < len(self.players):
            target = self.players[chosen]
            if target is not player and not getattr(target, 'eliminated', False):
                return target
        opponents = self.opponents_of(player)
        if self.experimental_ffa and opponents:
            # Headless FFA has no human target prompt.  Make the automation's
            # singular choice explicit and reproducible: lowest life, with
            # cyclic seat order as the tie-break (``opponents`` is already
            # cyclic).  Explicit player names and "each opponent" bypass it.
            return min(opponents, key=lambda opponent: opponent.life)
        return opponents[0] if opponents else None

    def attacked_planeswalker_for(self, attacker: Card) -> Optional[Card]:
        """The planeswalker this attacker was declared against, if still legal.

        SELF-INVALIDATING BY DESIGN. Ten sites clear `attacking_player`, and
        adding a parallel clear to each is a leak waiting to happen — so this
        requires the creature to be CURRENTLY attacking and the planeswalker to
        still be on the defending player's battlefield. A stale id on a
        non-attacking creature therefore cannot route damage anywhere, which is
        the same guarantee a perfectly-maintained clear would give.

        Returning None when the planeswalker has left is also the CR-correct
        outcome: the attacker stays attacking, but its damage is NOT redirected
        to the defending player (CR 506.4 / 508.1) — the caller drops it.
        """
        pw_id = getattr(attacker, 'attacking_planeswalker', None)
        if not pw_id or not getattr(attacker, 'attacking', False):
            return None
        defender = self.defender_for(attacker)
        if defender is None:
            return None
        for card in defender.battlefield:
            if str(getattr(card, 'id', '')) == str(pw_id):
                return card if card.is_planeswalker() else None
        return None

    def defender_for(self, attacker: Card) -> Optional[Player]:
        """Resolve an attacker's stable defending-player seat."""
        seat = getattr(attacker, 'attacking_player', None)
        if isinstance(seat, int):
            # An explicit defender is a stable combat assignment, not a
            # target preference. If that seat leaves mid-combat, this
            # attacker cannot acquire a replacement defender in a later
            # first/double-strike damage step.
            if not 0 <= seat < len(self.players):
                return None
            defender = self.players[seat]
            return (None if getattr(defender, 'eliminated', False)
                    else defender)
        controller = attacker._find_controller(self)
        return self.default_opponent_for(controller) if controller else None

    def apnap_player_indices(self, *, resolution_order: bool = False) -> List[int]:
        """Cyclic APNAP seats; reverse for immediate-mode LIFO resolution."""
        ordered_players = self.living_players_in_turn_order(
            self.active_player_index)
        ordered = [self.players.index(player) for player in ordered_players]
        return list(reversed(ordered)) if resolution_order else ordered

    def eliminate_player(self, player_index: int, reason: str = "",
                         *, finalize: bool = True) -> List[str]:
        """Eliminate one stable seat and perform the CR 800.4 cleanup core.

        Owned objects leave the game; borrowed permanents return to their
        owner's battlefield; that player's stack/triggers/effects disappear.
        The Player object itself stays in ``players`` so every seat and owner
        index remains stable. ``finalize=False`` lets one SBA pass eliminate
        several players simultaneously before deciding the winner.
        """
        if not (0 <= int(player_index) < len(self.players)):
            return []
        index = int(player_index)
        player = self.players[index]
        if getattr(player, 'eliminated', False):
            return []

        player.eliminated = True
        player.loss_reason = reason or "lost the game"
        if index not in self.elimination_order:
            self.elimination_order.append(index)
        messages = [
            f"💀 **{player.name}** is eliminated! ({player.loss_reason})"
        ]

        # Snapshot combat participation before zone cleanup moves/removes the
        # cards. Stable ids are the only safe discriminator with duplicate
        # names and borrowed permanents.
        departing_ids = {
            card.id
            for controller_index, controller in enumerate(self.players)
            for card in controller.battlefield
            if (getattr(card, 'owner_index', controller_index) == index
                or controller_index == index)
        }

        # Battlefield objects owned by the departing player leave the game.
        # Objects they merely controlled return to their stable owner seat.
        for controller_index, controller in enumerate(self.players):
            for card in list(controller.battlefield):
                owner_index = getattr(card, 'owner_index', controller_index)
                if owner_index == index:
                    controller.battlefield.remove(card)
                    self.unregister_static_effects(card)
                elif controller_index == index:
                    controller.battlefield.remove(card)
                    if (isinstance(owner_index, int)
                            and 0 <= owner_index < len(self.players)
                            and not self.players[owner_index].eliminated):
                        owner = self.players[owner_index]
                        owner.battlefield.append(card)
                        self.unregister_static_effects(card)
                        self.register_static_keyword_grants(card, owner.name)
                        self.register_static_pt_effects(card, owner.name)
                        self.register_replacement_effects(card, owner.name)

        # CR 800.4a is ownership-based, not zone-container-based. A card the
        # departing player owns can be sitting in another seat's exile, while
        # a borrowed card in the departing seat's engine-owned zone must not
        # disappear with the wrong owner. Move the latter to its owner's
        # corresponding zone and remove the former from every zone.
        zone_names = ('library', 'hand', 'graveyard', 'exile',
                      'command_zone', 'companion_zone')
        departing_object_ids = set(departing_ids)
        for holder_index, holder in enumerate(self.players):
            for zone_name in zone_names:
                zone = getattr(holder, zone_name, [])
                kept = []
                for card in list(zone):
                    owner_index = getattr(card, 'owner_index', holder_index)
                    if not isinstance(owner_index, int) or owner_index < 0:
                        owner_index = holder_index
                    if owner_index == index:
                        departing_object_ids.add(card.id)
                        continue
                    if holder_index == index:
                        if (0 <= owner_index < len(self.players)
                                and not self.players[owner_index].eliminated):
                            owner_zone = getattr(self.players[owner_index], zone_name)
                            if card not in owner_zone:
                                owner_zone.append(card)
                        continue
                    if getattr(card, '_castable_by_player', None) == player.name:
                        card._castable_by_player = None
                    kept.append(card)
                zone[:] = kept
        player.playable_from_exile = []
        player.playable_from_graveyard = []

        def _record_value(record, key, default=None):
            if isinstance(record, dict):
                return record.get(key, default)
            return getattr(record, key, default)

        def _record_belongs_to_departing_player(record) -> bool:
            if (_record_value(record, 'controller_index') == index
                    or _record_value(record, 'controller_name') == player.name
                    or _record_value(record, 'controller') in (index, player.name)):
                return True
            card = _record_value(record, 'card')
            return (card is not None
                    and getattr(card, 'owner_index', None) == index)

        self.stack = [entry for entry in self.stack
                      if not _record_belongs_to_departing_player(entry)]
        self.pending_async_triggers = [
            trigger for trigger in self.pending_async_triggers
            if not _record_belongs_to_departing_player(trigger)
        ]
        self.turn_effects = [
            effect for effect in self.turn_effects
            if not _record_belongs_to_departing_player(effect)
        ]
        self.delayed_triggers = [
            trigger for trigger in self.delayed_triggers
            if not _record_belongs_to_departing_player(trigger)
        ]
        self.conditional_exile_casts = {
            card_id: record
            for card_id, record in self.conditional_exile_casts.items()
            if (card_id not in departing_object_ids
                and not _record_belongs_to_departing_player(record))
        }

        # Remove combat objects and defender assignments involving this seat.
        # A surviving attacker aimed at the departing defender stays on the
        # battlefield but leaves combat (CR 800.4). Clear its per-card flags
        # now: the end-turn COMBAT-SWEEP is a tripwire, not routine cleanup.
        from mtg.helpers import strip_combat_state
        for attacker_id in list(self.attackers):
            result = self.find_card_global(attacker_id)
            if (result is not None
                    and getattr(result[0], 'attacking_player', None) == index):
                strip_combat_state(self, result[0])
        self.attackers = [
            attacker_id for attacker_id in self.attackers
            if attacker_id not in departing_ids
            and (self.find_card_global(attacker_id) is not None)
            and getattr(self.find_card_global(attacker_id)[0],
                        'attacking_player', None) != index
        ]
        cleaned_blockers = {}
        for attacker_id, blocker_ids in self.blockers.items():
            if attacker_id not in self.attackers:
                continue
            live_blockers = [
                blocker_id for blocker_id in blocker_ids
                if (self.find_card_global(blocker_id) is not None)
            ]
            if live_blockers:
                cleaned_blockers[attacker_id] = live_blockers
        self.blockers = cleaned_blockers

        # PrioritySystem is already N-player capable; remove the dead seat
        # without renumbering GameState and advance a dead holder cyclically.
        priority = self._priority_system
        priority_key = self.priority_identifier(player)
        # Compatibility for tests and pre-Q-H runtime objects created with
        # display names. Newly constructed games always take the stable path.
        stable_priority = bool(
            priority is not None
            and priority_key in getattr(priority, 'players', []))
        priority_identity = priority_key if stable_priority else player.name
        if (priority is not None
                and priority_identity in getattr(priority, 'players', [])):
            priority.stack = [entry for entry in priority.stack
                              if getattr(entry, 'controller', None) !=
                              priority_identity]
            priority.players = [name for name in priority.players
                                if name != priority_identity]
            priority._passes_in_succession = [
                name for name in priority._passes_in_succession
                if name != priority_identity
            ]
            priority._auto_pass_configs.pop(priority_identity, None)
            priority._connected.pop(priority_identity, None)
            priority._holds.discard(priority_identity)
            priority.display_names.pop(priority_identity, None)
            if priority.priority_holder == priority_identity:
                next_index = self.next_living_player_index(index)
                priority.priority_holder = (
                    (self.priority_identifier(next_index)
                     if stable_priority
                     else self.players[next_index].name)
                    if next_index is not None else None)
            if priority.active_player == priority_identity:
                next_index = self.next_living_player_index(index)
                priority.active_player = (
                    ((self.priority_identifier(next_index)
                      if stable_priority
                      else self.players[next_index].name)
                     if next_index is not None else priority_identity))
        if self.priority_player_index == index:
            next_index = self.next_living_player_index(index)
            if next_index is not None:
                self.priority_player_index = next_index

        self.recalculate_power_toughness()
        if finalize:
            self.finalize_eliminations()
        return messages

    def finalize_eliminations(self) -> None:
        """End the game at one living player, or draw at zero."""
        living = self.living_player_indices()
        if len(living) == 1:
            self.ended = True
            self.winner = living[0]
        elif not living:
            self.ended = True
            self.winner = None
        else:
            self.ended = False
            self.winner = None
    
    @property
    def non_active_player(self) -> Player:
        opponent = self.default_opponent_for(self.active_player)
        if opponent is None:
            return self.active_player
        return opponent
    
    def get_player_by_user_id(self, user_id: int) -> Optional[Player]:
        for p in self.players:
            if p.user_id == user_id:
                return p
        return None
    
    def get_player_index(self, user_id: int) -> Optional[int]:
        for i, p in enumerate(self.players):
            if p.user_id == user_id:
                return i
        return None

    def stable_player_id(self, player: Player) -> Optional[int]:
        """Return the persistent seat identifier used by choice payloads.

        Older saved games predate ``seat_id``.  Their list position is a safe
        compatibility fallback because seats are never reordered in-place.
        """
        try:
            index = self.players.index(player)
        except ValueError:
            return None
        return player.seat_id if player.seat_id is not None else index

    def priority_identifier(self, player_or_index) -> Optional[str]:
        """Return the durable identifier used by the priority ring.

        Display names are presentation, not identity: Discord users may share
        a display name and may rename themselves between saves.  Seats never
        move inside ``players``, so ``seat:<stable id>`` survives both cases.
        """
        if isinstance(player_or_index, Player):
            player = player_or_index
            try:
                index = self.players.index(player)
            except ValueError:
                return None
        else:
            try:
                index = int(player_or_index)
                player = self.players[index]
            except (TypeError, ValueError, IndexError):
                return None
        stable_id = player.seat_id if player.seat_id is not None else index
        return f"seat:{stable_id}"

    def player_from_priority_identifier(
            self, identifier, *, living_only: bool = False,
            allow_legacy_name: bool = True) -> Optional[Player]:
        """Resolve a priority identity without operational name matching.

        ``allow_legacy_name`` exists only to migrate saves written before the
        stable-seat conversion.  Live command paths always send ``seat:N``.
        """
        value = str(identifier or "")
        if value.startswith("seat:"):
            player = self.get_player_by_stable_id(
                value.partition(":")[2], living_only=living_only)
            return player
        if allow_legacy_name:
            matches = [
                player for player in self.players
                if player.name == value
                and (not living_only or not player.eliminated)
            ]
            if len(matches) == 1:
                return matches[0]
        return None

    def priority_display_name(self, identifier) -> str:
        """Human label for an internal priority identifier."""
        player = self.player_from_priority_identifier(
            identifier, living_only=False, allow_legacy_name=True)
        if player is None:
            return str(identifier or "none")
        if sum(candidate.name == player.name for candidate in self.players) > 1:
            stable_id = self.stable_player_id(player)
            return f"{player.name} (seat {stable_id + 1})"
        return player.name

    def priority_identity_for(self, player_or_index, priority=None) -> Optional[str]:
        """Stable live key, with a narrow legacy-runtime compatibility path."""
        key = self.priority_identifier(player_or_index)
        system = priority if priority is not None else self._priority_system
        if system is None or key in getattr(system, 'players', []):
            return key
        try:
            player = (player_or_index if isinstance(player_or_index, Player)
                      else self.players[int(player_or_index)])
        except (TypeError, ValueError, IndexError):
            return key
        if player.name in getattr(system, 'players', []):
            return player.name
        return key

    def normalize_priority_state(self, data: Optional[Dict]) -> Optional[Dict]:
        """Migrate a serialized name-keyed priority window to stable seats."""
        if not data:
            return data
        import copy
        normalized = copy.deepcopy(data)

        def _key(value):
            if value is None:
                return None
            player = self.player_from_priority_identifier(
                value, living_only=False, allow_legacy_name=True)
            return self.priority_identifier(player) if player else str(value)

        normalized["players"] = [
            _key(value) for value in normalized.get("players", [])
        ]
        for field_name in ("active_player", "priority_holder"):
            normalized[field_name] = _key(normalized.get(field_name))
        for field_name in ("passes_in_succession", "holds"):
            normalized[field_name] = [
                _key(value) for value in normalized.get(field_name, [])
            ]
        for field_name in ("auto_pass_configs", "connected", "display_names"):
            normalized[field_name] = {
                _key(value): payload
                for value, payload in normalized.get(field_name, {}).items()
            }
        for item in normalized.get("stack", []):
            if isinstance(item, dict) and item.get("controller") is not None:
                item["controller"] = _key(item["controller"])
        normalized["version"] = max(2, int(normalized.get("version", 1)))
        return normalized

    def get_player_by_stable_id(self, seat_id, *, living_only: bool = False) -> Optional[Player]:
        """Resolve an exact seat reference without falling back by name."""
        try:
            wanted = int(seat_id)
        except (TypeError, ValueError):
            return None
        for index, player in enumerate(self.players):
            actual = player.seat_id if player.seat_id is not None else index
            if actual == wanted:
                if living_only and getattr(player, 'eliminated', False):
                    return None
                return player
        return None

    def find_battlefield_card_by_id(self, card_id: str, *,
                                    living_only: bool = False) -> Optional[Tuple[Card, Player]]:
        """Resolve an exact battlefield object for a persisted choice."""
        if not card_id:
            return None
        for player in self.players:
            if living_only and getattr(player, 'eliminated', False):
                continue
            for card in player.battlefield:
                if card.id == card_id:
                    return card, player
        return None
    
    def find_card_global(self, name_or_id: str) -> Optional[Tuple[Card, Player, Zone]]:
        """Find a card anywhere in the game."""
        for player in self.players:
            for zone in Zone:
                for card in player.get_zone(zone):
                    if card.id == name_or_id or card.name.lower() == name_or_id.lower():
                        return card, player, zone
        return None
    
    @property
    def layers_engine(self):
        """Lazily create LayersEngine for continuous effects."""
        if self._layers_engine is None and HAS_LAYERS_ENGINE:
            self._layers_engine = LayersEngine()
        return self._layers_engine

    @property
    def replacement_engine(self):
        """Lazily create ReplacementEngine for 'if would, instead' effects."""
        if self._replacement_engine is None and HAS_REPLACEMENT_ENGINE:
            self._replacement_engine = ReplacementEngine()
        return self._replacement_engine

    def _state_fingerprint(self) -> str:
        """Compute a lightweight fingerprint of the current game state.

        Used for *local CPU cache invalidation* (avoids rebuilding the
        Python state-description string when nothing material changed).
        The fingerprint deliberately excludes mana pool sums and tap state,
        which:
          (a) change after every cast/activation, defeating the cache 35:1 in
              the May 3 batch, and
          (b) get rebuilt fresh in the calling code from `mana_str` and
              `available_mana_detailed()` regardless — the cached state
              description doesn't depend on them.
        Counters and life totals stay in the fingerprint because they
        DO appear in the printed state description.
        """
        parts = []
        parts.append(f"t{self.turn_number}")
        parts.append(f"p{self.phase.value}")
        parts.append(f"a{self.active_player_index}")
        for p in self.players:
            parts.append(f"L{p.life}")
            parts.append(f"H{len(p.hand)}")
            # Battlefield names + counters (sorted for determinism).
            # Drop tap state — calling code prints tap state from a fresh
            # read so its cache invalidation isn't needed for that.
            bf = sorted(
                f"{c.name}{c.counters.get('+1/+1', 0)}"
                for c in p.battlefield
            )
            parts.append(f"B{'|'.join(bf)}")
            parts.append(f"G{len(p.graveyard)}")
            # mana_pool intentionally excluded — see docstring above.
        parts.append(f"S{len(self.stack)}")
        raw = ";".join(parts)
        return hashlib.md5(raw.encode()).hexdigest()[:12]

    def register_replacement_effects(self, card: Card, controller_name: str):
        """
        Scan a permanent's oracle text for replacement effects
        and register them with the engine.

        Handles cards like Rest in Peace, Doubling Season, Furnace of Rath.
        Uses named card templates first, then oracle text regex patterns.
        """
        if not HAS_REPLACEMENT_ENGINE:
            return
        oracle = getattr(card, 'oracle_text', '') or ''
        if not oracle:
            return
        effects = scan_oracle_for_replacements(
            card.id, card.name, oracle, controller_name
        )
        if effects:
            engine = self.replacement_engine
            for effect in effects:
                engine.add_effect(effect)
            print(f"  [REPLACEMENT] Registered {len(effects)} replacement effect(s) from {card.name}")

    @staticmethod
    def _has_conditional_static(oracle_lower: str) -> bool:
        """True if the oracle gates a static ability on a board-wide threshold
        ("As long as you control N or more X" / "As long as this has N or more Y
        counters") — Beastmaster Ascension, Hallowed Haunting, etc."""
        return (('as long as' in oracle_lower or 'if you control' in oracle_lower)
                and 'or more' in oracle_lower)

    def _static_condition_met(self, card: 'Card', oracle_lower: str) -> bool:
        """Evaluate a conditional static's "N or more" threshold against the live
        board. Returns True when the condition holds OR when no recognized
        threshold is present (so unconditional statics always register). May 26
        audit: these were registered unconditionally (Beastmaster Ascension gave
        +5/+5 at 1 quest counter; needs 7)."""
        import re as _re
        word_num = {'one': 1, 'two': 2, 'three': 3, 'four': 4, 'five': 5,
                    'six': 6, 'seven': 7, 'eight': 8, 'nine': 9, 'ten': 10}

        def _num(tok):
            tok = tok.strip()
            return int(tok) if tok.isdigit() else word_num.get(tok)
        # (a) the source's own counters: "has N or more <kind> counters on it"
        m = _re.search(r'has (\w+) or more (\w+) counters? on it', oracle_lower)
        if m:
            need, kind = _num(m.group(1)), m.group(2)
            if need is None:
                return True
            have = sum(v for k, v in (getattr(card, 'counters', {}) or {}).items()
                       if kind in k.lower())
            return have >= need
        # (a2) Aug 8 batch audit (#2): the city's blessing (CR 702.131).
        # "as long as you have the city's blessing" (Tendershoot Dryad's
        # anthem) must route through the STICKY blessing predicate, not the
        # generic live-count branch below — the blessing survives the count
        # dropping. Checked BEFORE (b) because Ascend's reminder text ("If
        # you control ten or more permanents...") also matches (b)'s regex,
        # whose <type>-substring counting can never see a "permanent" (no
        # type line contains that word) — which is exactly how Tendershoot's
        # anthem was permanently OFF before this branch existed.
        if "city's blessing" in oracle_lower:
            controller = next((p for p in self.players if card in p.battlefield), None)
            if controller is None:
                return True
            from mtg.helpers import has_city_blessing
            return has_city_blessing(self, controller)
        # (b) controller's permanents: "control N or more <type>"
        m = _re.search(r'control (\w+) or more (\w+)', oracle_lower)
        if m:
            need, kind = _num(m.group(1)), m.group(2).rstrip('s')
            if need is None:
                return True
            controller = next((p for p in self.players if card in p.battlefield), None)
            if controller is None:
                return True
            # Aug 8 (#2): "permanent" as the counted kind means EVERY
            # battlefield object (no type line contains the word
            # "permanent", so the substring count was structurally zero).
            if kind == 'permanent':
                have = len(controller.battlefield)
            else:
                have = sum(1 for perm in controller.battlefield
                           if kind in (getattr(perm, 'type_line', '') or '').lower())
            return have >= need
        # Recognized "as long as"/"or more" but unparseable specifics — be
        # conservative and register (prior behavior) rather than risk turning
        # off a real static.
        return True

    def _remove_card_layer_effects(self, card: 'Card'):
        """Drop every layer effect sourced from this card (used to deactivate a
        conditional static when its threshold stops being met)."""
        if not self.layers_engine:
            return
        prefix = f"{card.id}_"
        self.layers_engine.effects = [
            e for e in self.layers_engine.effects if not e.id.startswith(prefix)
        ]

    def register_static_keyword_grants(self, card: Card, controller_name: str):
        """
        Scan a permanent's oracle text for static keyword-granting abilities
        and register them with the layers engine.

        Handles patterns like:
        - "Other creatures you control have trample and haste." (Stonehoof Chieftain)
        - "Creatures you control have hexproof." (Archetype of Endurance)
        - "Other creatures you control have flying." (Favorable Winds - partial)
        """
        if not self.layers_engine:
            return

        oracle = (card.oracle_text or "").lower()

        # May 26 audit: gate conditional static grants ("As long as you control
        # seven or more enchantments, creatures you control have flying" —
        # Hallowed Haunting). When the threshold isn't met, drop any existing
        # grant and skip; recalc re-runs this so it switches on when met.
        if self._has_conditional_static(oracle) and not self._static_condition_met(card, oracle):
            self._remove_card_layer_effects(card)
            return

        # CR 604: only STATIC abilities grant ongoing keywords. Filter out
        # paragraphs that start with "when/whenever/at" (triggered) or contain
        # "until end of turn" (temporary pump). This prevents cards like
        # "Whenever X, creatures you control gain trample until end of turn"
        # from registering trample as a permanent static grant.
        def _keyword_static_paragraph(para: str) -> bool:
            p = para.strip().lower()
            if not p:
                return False
            if p.startswith(("when ", "whenever ", "at the beginning", "at end")):
                return False
            if ":" in p:
                cost_part = p.split(":", 1)[0]
                if any(sym in cost_part for sym in ("{t}", "{q}", "{w}", "{u}", "{b}", "{r}", "{g}", "{c}", "{x}")) \
                   or any(ch.isdigit() for ch in cost_part):
                    return False
            if "until end of turn" in p or "until your next" in p:
                return False
            return True

        oracle = "\n".join(para for para in oracle.split("\n") if _keyword_static_paragraph(para))

        # Common keyword list to scan for
        keyword_list = ['trample', 'haste', 'flying', 'vigilance', 'lifelink',
                        'deathtouch', 'first strike', 'double strike', 'hexproof',
                        'indestructible', 'menace', 'reach', 'defender']

        # Aura-qualified: "Auras you control have <keywords>" (Archon of Sun's Grace)
        # Must match BEFORE generic "creatures you control" to avoid over-broadcasting.
        auras_match = re.search(r'auras you control have (.+?)(?:\.|$)', oracle)
        # Subtype-qualified: "<Subtype> creatures you control have <keywords>"
        # e.g. "Pegasus creatures you control have lifelink." (Archon of Sun's Grace)
        # Must precede the generic "creatures you control" match or the grant over-broadcasts.
        _COLOR_WORDS = {'white', 'blue', 'black', 'red', 'green'}
        subtype_match = re.search(r'(?<!\w)([a-z]+) creatures you control have (.+?)(?:\.|$)', oracle)
        if subtype_match and subtype_match.group(1) in (_COLOR_WORDS | {'other'}):
            # May 30 audit: "other" is the CR-109.5 self-exclusion qualifier, NOT a
            # creature subtype. Capturing it here mis-routed "Other creatures you
            # control have X" grants to applies_to="other you control", which
            # neither has_granted_keyword (line ~3496) nor applies_to_permanent
            # recognizes (both require the substring "creatures you control") — so
            # Stonehoof-style keyword grants silently never applied. Let other_match
            # (the dedicated "other creatures you control" path) handle it.
            subtype_match = None  # colors / "other" handled by their own paths
        # Aug 10 deferred (E1): "Other PERMANENTS you control have
        # indestructible" (Avacyn, Angel of Hope) matched no pattern here at
        # all, so Akroma died under her to a board wipe. The grant ladder was
        # creature-shaped end to end.
        #
        # The clause-initial anchor is load-bearing, not decorative. Unanchored,
        # this newly reaches Avacyn's Memorial ("legendary permanents you
        # control have..."), Invasion of Pyrulea ("transformed ...") and
        # Dawnglade Regent ("As long as you're the monarch, ..."), and would
        # grant each unconditionally to EVERY permanent — an inversion.
        # Declining those is an under-count, which is the direction this
        # codebase prefers.
        perm_match = re.search(
            r'(?:(?<=^)|(?<=\n)|(?<=\. ))(other )?permanents you control have (.+?)(?:\.|$)',
            oracle)
        # Pattern: "Other creatures you control have <keywords>"
        # Pattern: "Creatures you control have <keywords>"
        other_match = re.search(r'other creatures you control have (.+?)(?:\.|$)', oracle)
        # For non-"other" version, check separately (avoid negative lookbehind issues)
        all_match = None
        if not other_match and not subtype_match:
            all_match = re.search(r'(?<!\w)creatures you control have (.+?)(?:\.|$)', oracle)
        # Power-conditional: "Creatures you control with power N or greater have X"
        # Captures threshold N for a filter_fn gate at has_granted_keyword time.
        power_cond_match = re.search(
            r'creatures you control with power (\d+) or greater have (.+?)(?:\.|$)',
            oracle,
        )
        if power_cond_match:
            # Override looser matches so we pick up the narrower clause.
            all_match = power_cond_match
            other_match = None
            subtype_match = None

        match = auras_match or subtype_match or other_match or all_match or perm_match
        is_other_only = other_match is not None and subtype_match is None
        is_aura_only = auras_match is not None
        is_subtype_only = subtype_match is not None and auras_match is None
        # A permanents-scoped grant only wins when no narrower clause matched.
        is_perm_only = (perm_match is not None and auras_match is None
                        and subtype_match is None and other_match is None
                        and all_match is None)
        power_threshold = int(power_cond_match.group(1)) if power_cond_match else 0

        if match:
            # subtype_match.group(2) holds the keyword text; others use group(1)
            if is_perm_only:
                keyword_text = perm_match.group(2)
            elif is_subtype_only:
                keyword_text = match.group(2)
            elif power_cond_match:
                keyword_text = power_cond_match.group(2)
            else:
                keyword_text = match.group(1)
            granted = []
            for kw in keyword_list:
                if kw in keyword_text:
                    # Capitalize properly for has_keyword() matching
                    granted.append(kw.title() if ' ' not in kw else
                                   ' '.join(w.capitalize() for w in kw.split()))

            if granted:
                if is_perm_only:
                    applies_to = (("other " if perm_match.group(1) else "")
                                  + "permanents you control")
                elif is_aura_only:
                    applies_to = "auras you control"
                elif is_subtype_only:
                    # Format matches applies_to_permanent's subtype regex:
                    # "<subtype>s? you control"
                    applies_to = f"{subtype_match.group(1)} you control"
                elif is_other_only:
                    applies_to = "other creatures you control"
                else:
                    applies_to = "creatures you control"
                effect_id = f"{card.id}_static_keywords"
                # Skip if we've already registered this card's static grant —
                # prevents ETB + flicker + save-reload from stacking duplicate
                # effects AND suppresses repeated [LAYERS] log lines.
                if any(e.id == effect_id for e in self.layers_engine.effects):
                    return
                # Build optional power-gate filter. Captured here so the closure
                # keeps the threshold value even if the pattern match goes out
                # of scope later.
                filter_fn = None
                if power_threshold > 0:
                    _th = power_threshold
                    def _power_gate(perm, game, _threshold=_th):
                        try:
                            pwr = perm.get_effective_power(game) if hasattr(perm, 'get_effective_power') else 0
                        except Exception:
                            pwr = 0
                        return pwr >= _threshold
                    filter_fn = _power_gate
                effect = ContinuousEffect(
                    id=effect_id,
                    source_name=card.name,
                    source_id=card.id,
                    controller=controller_name,
                    layer=Layer.ABILITY,
                    effect_type="add_abilities",
                    abilities_granted=granted,
                    applies_to=applies_to,
                    filter_fn=filter_fn,
                )
                self.layers_engine.add_effect(effect)
                if is_aura_only:
                    scope = "auras"
                elif is_subtype_only:
                    scope = f"{subtype_match.group(1)} creatures"
                elif is_other_only:
                    scope = "other creatures"
                else:
                    scope = "creatures"
                print(f"[LAYERS] Registered static keyword grant: {card.name} → {granted} to {scope}")

    def register_static_pt_effects(self, card: Card, controller_name: str):
        """Scan oracle text for static P/T-affecting abilities and register with layers engine."""
        if not self.layers_engine:
            return
        oracle = (card.oracle_text or "").lower()
        # May 26 audit: gate conditional static anthems ("As long as this has
        # seven or more quest counters, creatures you control get +5/+5" —
        # Beastmaster Ascension). When the threshold isn't met, drop any existing
        # anthem and skip; recalc re-runs this so it switches on when met.
        if self._has_conditional_static(oracle) and not self._static_condition_met(card, oracle):
            self._remove_card_layer_effects(card)
            return
        humility_match = re.search(r'(?:all )?creatures (?:lose all abilities and )?(?:have base power and toughness|are) (\d+)/(\d+)', oracle)
        if humility_match:
            set_p, set_t = int(humility_match.group(1)), int(humility_match.group(2))
            effects = create_humility_effect(card.name, card.id, controller_name)
            if set_p != 1 or set_t != 1:
                for eff in effects:
                    if eff.sublayer == Sublayer.PT_SET:
                        eff.set_power, eff.set_toughness = set_p, set_t
            for eff in effects:
                self.layers_engine.add_effect(eff)
            print(f"[LAYERS] Registered Humility-style P/T effect: {card.name} -> all creatures {set_p}/{set_t}")
            return
        # Blood Moon: "Nonbasic lands are Mountains"
        blood_moon_match = re.search(r'nonbasic lands are mountains', oracle)
        if blood_moon_match:
            effect = create_blood_moon_effect(card.name, card.id, controller_name)
            self.layers_engine.add_effect(effect)
            print(f"[LAYERS] Registered Blood Moon type-change: {card.name} -> nonbasic lands are Mountains")
            return
        # Painter's Servant: "All cards/spells/permanents are the chosen color"
        # Auto-choose: pick the color most common on the opponent's permanents
        # (Painter's Servant + Grindstone is the primary combo — Blue is the classic pick
        # when targeting all opponent cards for Grindstone, but more generally we pick
        # the color that names the most opponent permanents so removal/interaction is
        # maximised).
        #
        # May 20 audit (CRITICAL): the substring `"chosen color" in oracle` matched
        # ANY card whose oracle says "the chosen color" — including Coldsteel Heart's
        # "As Coldsteel Heart enters, choose a color. {T}: Add one mana of the chosen
        # color." (game_1506623254738112754_console.log:539 fired Painter's Servant on
        # a Coldsteel Heart ETB, turning ALL permanents+spells White). Same bug
        # would fire on Diamond Lion, Birds of Paradise (vintage), Cromat, etc. —
        # any color-choose mana rock. Tighten to exact card name match.
        if card.name.lower() == "painter's servant":
            chosen_color = "U"  # Default: Blue is the most combo-relevant choice

            # Find the controller player object so we can inspect the game state
            controller_player = None
            opponent_player = None
            for p in self.players:
                if p.name == controller_name:
                    controller_player = p
                else:
                    opponent_player = p

            # Count how many of each color appear on the opponent's permanents
            COLOR_NAMES = {"W": "white", "U": "blue", "B": "black", "R": "red", "G": "green"}
            if opponent_player:
                color_counts: dict = {c: 0 for c in COLOR_NAMES}
                for perm in opponent_player.battlefield:
                    # Check color_identity, then colors field, then mana_cost
                    perm_colors = set()
                    if getattr(perm, 'color_identity', None):
                        perm_colors.update(str(ci).upper() for ci in perm.color_identity)
                    elif getattr(perm, 'colors', None):
                        perm_colors.update(str(ci).upper() for ci in perm.colors)
                    elif getattr(perm, 'mana_cost', None):
                        for c in COLOR_NAMES:
                            if f'{{{c}}}' in perm.mana_cost.upper():
                                perm_colors.add(c)
                    for c in perm_colors:
                        if c in color_counts:
                            color_counts[c] += 1
                total_colored = sum(color_counts.values())
                if total_colored > 0:
                    chosen_color = max(color_counts, key=lambda c: color_counts[c])
                    reason = f"most common opponent color ({color_counts[chosen_color]} permanents)"
                else:
                    # Opponent has no colored permanents — fall back to commander identity
                    reason = "opponent has no colored permanents, using commander identity"
                    if controller_player and hasattr(controller_player, '_get_commander_colors'):
                        cmd_colors = controller_player._get_commander_colors()
                        if cmd_colors:
                            chosen_color = cmd_colors[0]
            else:
                reason = "default (no opponent info)"

            # Store the choice on the card so other systems can reference it
            card._painter_color = chosen_color

            effect = create_color_change_effect(card.name, card.id, controller_name,
                                                 colors_added=[chosen_color],
                                                 applies_to="all permanents and spells")
            self.layers_engine.add_effect(effect)
            color_word = {"W": "White", "U": "Blue", "B": "Black", "R": "Red", "G": "Green"}.get(chosen_color, chosen_color)
            print(f"[LAYERS] Painter's Servant: chose {color_word} ({reason}); all permanents/spells are now also {chosen_color}")
            # Surface to Discord via the standard pending-messages channel.
            #
            # May 20 audit fix: previously this read `getattr(self,
            # '_current_game', None)` — but `_current_game` is never assigned
            # anywhere in the codebase, so `game` was always None and the
            # color-choice announcement never reached Discord. `self` IS the
            # GameState (register_static_pt_effects is a GameState method),
            # so just push directly onto `self._pending_messages` — the
            # cast_spell_async flush at spells.py:2135 will drain it.
            painter_msg = (f"🎨 **Painter's Servant** enters — all permanents and spells "
                           f"become **{color_word}** in addition to their other colors. "
                           f"_(chosen because: {reason})_")
            if not hasattr(self, '_pending_messages') or self._pending_messages is None:
                self._pending_messages = []
            self._pending_messages.append(painter_msg)
            return
        # CR 604: anthem patterns must come from STATIC abilities, not triggered
        # ones. Split the oracle into paragraphs and filter out any paragraph that
        # begins with "when", "whenever", or "at " (triggered abilities), OR that
        # contains "until end of turn" / "until your next" (temporary pumps).
        # We build a "static-only" oracle string by joining paragraphs that pass.
        def _is_static_paragraph(para: str) -> bool:
            p = para.strip().lower()
            if not p:
                return False
            # Triggered abilities
            if p.startswith(("when ", "whenever ", "when,", "whenever,", "at the beginning", "at end")):
                return False
            # Activated abilities ("cost: effect" — has a colon and the text before
            # it is a legal cost, not a creature subtype line)
            # Only skip if the activation cost contains a mana/tap symbol — this
            # avoids false-positives on flavor colons.
            if ":" in p:
                cost_part = p.split(":", 1)[0]
                if any(sym in cost_part for sym in ("{t}", "{q}", "{w}", "{u}", "{b}", "{r}", "{g}", "{c}", "{x}")) \
                   or any(ch.isdigit() for ch in cost_part):
                    return False
            # Temporary pumps
            if "until end of turn" in p or "until your next" in p:
                return False
            return True

        static_oracle = "\n".join(
            para for para in oracle.split("\n") if _is_static_paragraph(para)
        )

        # Dedup guard: don't re-register this card's anthem if it's already
        # present (same logic the keyword-grant path added). Without this,
        # every recalculate_power_toughness tick stacks a fresh copy, and
        # tokens can end up with power/toughness totals like -834/-834.
        # Cards with multi-clause anthems (Elesh Norn: +2/+2 own + -2/-2 opp)
        # register multiple effects with IDs like "{card.id}_own_other" and
        # "{card.id}_opp_debuff", plus legacy "_anthem" / color-qualified.
        # Skip the entire registration if ANY layer effect from this card
        # already exists.
        anthem_id_prefix = f"{card.id}_"
        if any(e.id.startswith(anthem_id_prefix) and (
                e.id.endswith("_anthem") or e.id.endswith("_own_other")
                or e.id.endswith("_own_all") or e.id.endswith("_opp_debuff")
                or e.id.endswith("_all_buff") or e.id.endswith("_all_debuff")
                or e.id.endswith("_color") or e.id.endswith("_subtype"))
               for e in self.layers_engine.effects):
            return

        # "For each <X>" anthems (Heavenly Blademaster: +1/+1 per Aura/Equipment)
        # have dynamic amounts that change with attachment count. Registering
        # them as static +1/+1 is wrong (and Blademaster's double-register was
        # the root of the 834-stack Soldier bug). Defer these to Tier 3 so
        # they get resolved dynamically instead of over-broadcasting.
        if re.search(r'creatures you control get \+\d+/\+\d+ for each', static_oracle):
            print(f"[LAYERS] Skipping 'for each' anthem (dynamic amount): {card.name}")
            return

        # Token-qualified anthem patterns (Phantom General, Adriana, Captain of
        # the Guard, etc.). Must come before color-qualified and generic so the
        # more-specific filter wins. May 17 audit: Phantom General's "Other
        # creature tokens you control get +1/+1" was previously caught by the
        # generic "other creatures you control" regex and applied to non-token
        # creatures too, leaving token strategies under-buffed.
        token_anthem_match = re.search(
            r'(other )?creature tokens you control get \+(\d+)/\+(\d+)', static_oracle)
        if token_anthem_match:
            is_other = bool(token_anthem_match.group(1))
            p_val, t_val = int(token_anthem_match.group(2)), int(token_anthem_match.group(3))
            applies_to = ("other " if is_other else "") + "creature tokens you control"
            effect = create_anthem_effect(card.name, f"{card.id}_tokens", controller_name, p_val, t_val, applies_to)
            self.layers_engine.add_effect(effect)
            print(f"[LAYERS] Registered token anthem P/T: {card.name} -> {applies_to} +{p_val}/+{t_val}")
            return

        # Color-qualified anthem patterns (must come BEFORE generic patterns)
        color_anthem_match = re.search(
            r'(white|blue|black|red|green)\s+creatures you control get \+(\d+)/\+(\d+)', static_oracle)
        if color_anthem_match:
            color_word = color_anthem_match.group(1)
            p_val, t_val = int(color_anthem_match.group(2)), int(color_anthem_match.group(3))
            applies_to = f"{color_word} creatures you control"
            effect = create_anthem_effect(card.name, f"{card.id}_color", controller_name, p_val, t_val, applies_to)
            self.layers_engine.add_effect(effect)
            print(f"[LAYERS] Registered color anthem P/T: {card.name} -> {applies_to} +{p_val}/+{t_val}")
            return
        # Subtype-restricted anthems: "Other Werewolves you control get +1/+1",
        # "Other Wolves you control get +1/+1", etc. Also handles non-X variants
        # ("Other non-Human creatures you control get +1/+1" — Mikaeus).
        # The optional " creatures?" segment handles the Mikaeus form, where the
        # subtype is followed by the word "creatures" (e.g. "non-Human creatures
        # you control"). Must come before generic patterns so the more-specific
        # filter wins.
        # Aug 10 card-targeted wave (B). Two generalizations, both measured
        # against the deck inventory before being written:
        #   * "other" is now OPTIONAL. Full Moon's Rise prints "Werewolf
        #     creatures you control get +1/+0" and Tendershoot Dryad prints
        #     "Saprolings you control get +2/+2" — neither says "other", so both
        #     fell past this branch into the unanchored own_all and buffed the
        #     ENTIRE board. When the clause omits "other" the source is NOT
        #     excluded, so the captured group is threaded into applies_to
        #     rather than "other" being hardcoded as it was before.
        #   * the qualifier may be a LIST — "Other Wolves and Werewolves you
        #     control" (Nightpack Ambusher), "Other snow and Zombie creatures
        #     you control" (Narfi). A single [a-z]+ matched neither, so Narfi
        #     leaked to own_all and Nightpack registered nothing at all.
        # Order is load-bearing: this must stay BELOW the color branch, or
        # "White creatures you control" registers as subtype "white" and
        # matches nobody.
        subtype_anthem = re.search(
            r'(other )?(non-)?([a-z]+(?:\s+and\s+[a-z]+)*)'
            r'(?: creatures?)? you control get \+(\d+)/\+(\d+)',
            static_oracle,
        )
        # Combat-state qualifiers ("Attacking/Blocking creatures you control")
        # are NOT subtypes and cannot be expressed as an applies_to string:
        # LayeredPermanent.to_dict (rules/layers.py) carries no attacking key,
        # calculate_characteristics is called with game_state=None, and
        # create_anthem_effect sets no filter_fn. Registering "attackings you
        # control" would match nobody silently. Decline with a greppable
        # breadcrumb instead — under-applying is the safe direction, and it is
        # strictly better than the pre-Aug-10 behaviour of buffing the whole
        # board. See CLAUDE.md for the two implementation shapes.
        if subtype_anthem and subtype_anthem.group(3) in ('attacking', 'blocking'):
            print(f"[LAYERS] Skipping combat-state anthem (needs combat state, "
                  f"not modelled): {card.name}")
            return
        # Skip subtype pathway when the match is really "other creatures" — that
        # is a card TYPE, not a subtype, and needs the generic "other creatures
        # you control" clause (which applies_to_permanent handles via its
        # creature-type branch). Without this skip, Elesh Norn would register
        # under the subtype branch, where applies_to_permanent tries to match
        # "creatures" as a subtype on every creature, finds nothing, and
        # silently applies to no one.
        if subtype_anthem and subtype_anthem.group(3) in ("creature", "creatures"):
            subtype_anthem = None
        if subtype_anthem:
            is_other = bool(subtype_anthem.group(1))
            negation = subtype_anthem.group(2)  # "non-" or None
            sub_words = [w.strip() for w in
                         re.split(r'\s+and\s+', subtype_anthem.group(3)) if w.strip()]
            # Normalize a trailing plural "s" so the base subtype word matches
            # what's stored on cards (case-insensitive). "wolves" has no trailing
            # "s" after this (Wolves has irregular plural), "humans" -> "human".
            sub_words = [w[:-1] if (w.endswith("s") and len(w) > 1) else w
                         for w in sub_words]
            p_val, t_val = int(subtype_anthem.group(4)), int(subtype_anthem.group(5))
            prefix = "other " if is_other else ""
            if negation:
                applies_to = f"{prefix}non-{sub_words[0]} creatures you control"
            else:
                applies_to = f"{prefix}{' and '.join(w + 's' for w in sub_words)} you control"
            effect = create_anthem_effect(card.name, f"{card.id}_subtype", controller_name, p_val, t_val, applies_to)
            self.layers_engine.add_effect(effect)
            print(f"[LAYERS] Registered subtype anthem P/T: {card.name} -> {applies_to} +{p_val}/+{t_val}")
            return

        # Multi-clause static anthems (Elesh Norn: "+2/+2 to your creatures
        # AND -2/-2 to opponent creatures") need BOTH halves registered, not
        # just the first match. Iterate patterns and register every one that
        # matches, but skip overlapping pairs ("other creatures you control"
        # vs "creatures you control" — the more-specific one should win).
        anthem_patterns = [
            ('own_other', r'other creatures you control get \+(\d+)/\+(\d+)', "other creatures you control", False),
            # Aug 10 (B): anchored — see _ANTHEM_INLINE_RE for why the anchor is a
            # fixed-width lookbehind and why `^`/`\n` would kill Beastmaster Ascension.
            ('own_all', _ANTHEM_OWN_ALL_RE, "creatures you control", False),
            ('opp_debuff', r'creatures (?:your opponents?|opponents?) control get -(\d+)/-(\d+)', "creatures opponents control", True),
            ('all_buff', r'all creatures get \+(\d+)/\+(\d+)', "all creatures", False),
            # Board-wide negative anthem: "Creatures get -1/-1" / "All creatures get
            # -1/-1" (Night of Souls' Betrayal). May 30 audit: the printed text is
            # "All creatures get -1/-1." — the optional "all " was missing, so the
            # effect never registered and every creature kept full power for the
            # whole game.
            ('all_debuff', r'(?:^|\.\s*)(?:all )?creatures get -(\d+)/-(\d+)', "all creatures", True),
        ]
        registered = set()
        for tag, pattern, applies_to, is_negative in anthem_patterns:
            # Skip own_all if own_other already registered (more-specific wins)
            if tag == 'own_all' and 'own_other' in registered:
                continue
            match = re.search(pattern, static_oracle)
            if match:
                p_val, t_val = int(match.group(1)), int(match.group(2))
                if is_negative:
                    p_val, t_val = -p_val, -t_val
                # Make the effect ID tag-suffixed so multiple clauses on the
                # same card don't collide (Elesh Norn has both own_other AND
                # opp_debuff). Without this, the second register replaces
                # the first because they share the card's ID.
                effect = create_anthem_effect(card.name, f"{card.id}_{tag}", controller_name, p_val, t_val, applies_to)
                self.layers_engine.add_effect(effect)
                registered.add(tag)
                sp = '+' if p_val >= 0 else ''
                st = '+' if t_val >= 0 else ''
                print(f"[LAYERS] Registered anthem P/T: {card.name} -> {applies_to} {sp}{p_val}/{st}{t_val}")

    def recalculate_power_toughness(self):
        """Recalculate effective P/T for all creatures via layers engine (before SBAs)."""
        if not self.layers_engine:
            return
        # May 26 audit: conditional statics ("As long as you control N+ X" /
        # "...has N+ Y counters") register only at ETB, but their condition
        # changes as counters/permanents change. Re-evaluate them here (recalc
        # runs ~30x/turn) so the effect switches on/off as the threshold is
        # crossed. The register_* gates handle both add (when met) and remove
        # (when not met); this must run BEFORE the has_pt_effects short-circuit
        # below so a just-activated anthem isn't missed.
        for _player in self.players:
            for _card in list(_player.battlefield):
                _oc = (_card.oracle_text or "").lower()
                if self._has_conditional_static(_oc):
                    self.register_static_pt_effects(_card, _player.name)
                    self.register_static_keyword_grants(_card, _player.name)
        has_pt_effects = any(e.layer == Layer.POWER_TOUGHNESS for e in self.layers_engine.effects)
        if not has_pt_effects:
            for player in self.players:
                for card in player.battlefield:
                    card._layers_power_mod = 0
                    card._layers_toughness_mod = 0
                    # May 20 audit (CRITICAL #2): also clear the
                    # _has_layers_pt_effect sentinel when no layer effects are
                    # active, so the inline anthem fallback in
                    # get_effective_power/toughness can fire normally.
                    card._has_layers_pt_effect = False
                    # May 30 audit (option b): no layer effects → has_keyword uses
                    # the raw-list fast path, so drop any stale resolved set.
                    card._resolved_keywords = None
            return
        # [LAYERS-PT] Log a single summary line per call, not one line per creature.
        # Per-creature logging caused 600+ log entries in games with Massacre Wurm /
        # Judith / Glorious Anthem because this function is called ~30 times per turn.
        affected_summary = []
        for player in self.players:
            for card in player.battlefield:
                # May 26 audit: pass game so devotion-gated gods (Erebos at
                # devotion<5) aren't built as creatures in the layers P/T pass —
                # consistent with the F24 is_creature(game) sites (can_attack/
                # can_block/targeting). Without this the layers engine could
                # anthem-buff a non-creature god's cached P/T.
                if not card.is_creature(game=self):
                    continue
                try:
                    base_p = int(card.power) if card.power and card.power != '*' else 0
                except (ValueError, TypeError):
                    base_p = 0
                try:
                    base_t = int(card.toughness) if card.toughness and card.toughness != '*' else 0
                except (ValueError, TypeError):
                    base_t = 0
                # Extract card colors for color-restricted anthems (Honor of the Pure, etc.)
                card_colors = getattr(card, 'colors', []) or []
                if not card_colors and card.mana_cost:
                    for c in card.mana_cost.upper():
                        if c in 'WUBRG' and c not in card_colors:
                            card_colors.append(c)
                # Pull subtypes (Werewolf, Wolf, Human, Soldier, etc.) so subtype-
                # restricted anthems (Tovolar, Mikaeus, Lord of Atlantis) filter correctly.
                # Aug 10 card-targeted wave (CRITICAL): `Card` has NO
                # `subtypes` field, so this getattr always returned []
                # and rules/layers.py rejected every permanent — every
                # subtype-restricted anthem in the engine was inert
                # (Captivating Vampire registered correctly and its
                # Vampire Nighthawk still dealt 2, not 3, on four
                # combats). Registration was right; only the READ was
                # wrong. get_creature_types() is the real accessor,
                # already used at eight other sites. Same class as
                # game._rules_engine — a getattr chain whose happy
                # path never existed.
                card_subtypes = [t.lower() for t in (card.get_creature_types() or [])]
                # Aug 10 deferred (B): base_supertypes was never populated, so
                # LayeredPermanent.supertypes was ALWAYS empty and any filter
                # reading it silently matched nobody — the same shape as the
                # June-11 is_token finding. Narfi's "Other snow and Zombie
                # creatures you control" needs the snow SUPERTYPE to resolve.
                card_supertypes = [s for s in _SUPERTYPES
                                   if s in (card.type_line or '').lower()]
                lp = LayeredPermanent(
                    id=card.id, name=card.name, controller=player.name,
                    owner=player.name, base_types=["creature"],
                    base_subtypes=card_subtypes,
                    base_supertypes=card_supertypes,
                    base_colors=card_colors,
                    base_abilities=list(getattr(card, 'keywords', []) or []),
                    base_power=base_p, base_toughness=base_t,
                    plus_counters=card.counters.get('+1/+1', 0),
                    minus_counters=card.counters.get('-1/-1', 0),
                    # June 11 audit: without this, token-qualified anthems
                    # (Intangible Virtue) registered fine but never applied —
                    # the filter read is_token from a dict that never had it
                    # (game 1514621737994551457: 1/1 Humans fought as 1/1s
                    # for 9 turns under an active Virtue).
                    is_token=bool(getattr(card, 'is_token', False)))
                result = self.layers_engine.calculate_characteristics(lp, None)
                # May 30 audit (option b): the engine resolves Layer-6 abilities
                # (Humility's remove-all + static grants, applied in TIMESTAMP order
                # per CR 613.7) into result.abilities. Cache it (lowercased) so
                # has_keyword can defer to the engine under a "lose all abilities"
                # effect instead of the May-26 blanket strip — this correctly keeps
                # abilities GRANTED AFTER Humility (e.g. Stonehoof's trample).
                card._resolved_keywords = set(
                    a.lower() for a in (getattr(result, 'abilities', None) or []))
                layers_p = result.power if result.power is not None else base_p
                layers_t = result.toughness if result.toughness is not None else base_t
                # May 7 audit: `calculate_characteristics()` does NOT include
                # counter contribution in its returned power/toughness — only
                # base + layer effects (anthems, set-base, etc.). The previous
                # formula `(layers_p - base_p) - cd` double-subtracted counters,
                # so `get_effective_power()` (which adds counters separately)
                # silently cancelled the counter contribution. Symptom: Rhys
                # (base 1/1) with 3 +1/+1 counters and Glorious Anthem reported
                # effective power 2 instead of 5; in the audit-flagged
                # March-of-the-Multitudes + Cathar's Crusade chain, one Soldier
                # accumulated a `(-21/-21)` modifier across ETBs.
                #
                # Fix: just subtract base from the layer result. Counters are
                # already accounted for downstream by `Card.get_effective_*`.
                card._layers_power_mod = layers_p - base_p
                card._layers_toughness_mod = layers_t - base_t
                # May 20 audit (CRITICAL #2): set a sentinel even when the
                # delta is 0. Humility (Layer 7b set 1/1) + anthem (Layer 7c
                # +1/+1) on a 2/2 creature → layers_p=2, base_p=2, delta=0.
                # The old code in get_effective_power saw mod==0 and fell
                # back to _get_anthem_power_bonus, which re-added the anthem,
                # yielding 2+0+1=3 (Humility invisible).
                # game_1506623352666853486:896-913 had Crypt Ghast/Sram/
                # Ophiomancer/Kambal all attacking for 3 power with Humility
                # + Ethereal Absolution active; should be 2. Fix: flag this
                # permanent as "layers engine touched my P/T" so the fallback
                # is skipped even when delta is 0.
                card._has_layers_pt_effect = True
                cd = card.counters.get('+1/+1', 0) - card.counters.get('-1/-1', 0)
                # Suppress spurious deltas on X=0 creatures (Walking Ballista,
                # Hangarback Walker) whose base P/T is 0/0 with no counters:
                # the card is about to die from CREATURE_ZERO_TOUGHNESS and any
                # tiny non-zero delta from effect-filter edge cases just
                # confuses the log reader. Outcome is unchanged (SBA kills
                # them on the next sweep); this is purely a display guard.
                if base_p == 0 and base_t == 0 and cd == 0:
                    continue
                if card._layers_power_mod != 0 or card._layers_toughness_mod != 0:
                    # Disambiguate same-name tokens by appending a short id
                    # suffix — without it, March of the Multitudes producing 22
                    # Soldiers looks like one Soldier swinging from -1/-1 to
                    # -21/-21 across log lines (the May 7 confusion).
                    _id_tail = (getattr(card, 'id', '') or '')[-4:]
                    _disambig = f"#{_id_tail}" if _id_tail else ""
                    affected_summary.append(
                        f"{card.name}{_disambig}({card._layers_power_mod:+d}/{card._layers_toughness_mod:+d})"
                    )
        # Suppress identical summaries — SBA sweeps and phase transitions call
        # this function repeatedly with unchanged board state, producing the
        # same [LAYERS-PT] line dozens of times per turn.
        # May 20 audit: track suppression counts so the dedup is auditable.
        # Previously identical-summary suppression was silent — a real bug
        # that produced an identical-looking summary by coincidence would be
        # invisible. Now emit one `[LAYERS-PT-SUPPRESSED]` line at every
        # 25th suppression with the count so audits can verify the dedup
        # isn't masking unique state changes.
        prev = getattr(self, '_layers_pt_last_summary', None)
        summary_key = tuple(affected_summary)
        if affected_summary and summary_key != prev:
            print(f"[LAYERS-PT] {len(affected_summary)} creature(s) modified: {', '.join(affected_summary[:8])}"
                  + (f" +{len(affected_summary)-8} more" if len(affected_summary) > 8 else ""))
            self._layers_pt_last_summary = summary_key
            self._layers_pt_suppress_count = 0
        elif affected_summary and summary_key == prev:
            # Same summary as last call — increment suppression counter.
            self._layers_pt_suppress_count = getattr(self, '_layers_pt_suppress_count', 0) + 1
            # Surface every 25th suppression so audits can confirm dedup
            # is firing on genuinely identical state, not hiding diffs.
            if self._layers_pt_suppress_count % 25 == 0:
                print(f"[LAYERS-PT-SUPPRESSED] same summary x{self._layers_pt_suppress_count} "
                      f"({len(affected_summary)} creature(s): "
                      f"{', '.join(affected_summary[:4])}"
                      + (f" +{len(affected_summary)-4} more" if len(affected_summary) > 4 else "") + ")")
        elif not affected_summary:
            self._layers_pt_last_summary = None
            self._layers_pt_suppress_count = 0

    def unregister_static_effects(self, card: Card):
        """Remove all continuous/replacement effects from a permanent (when it leaves the battlefield)."""
        if self.layers_engine:
            # Only log if there were effects to remove — otherwise every
            # creature leaving the battlefield spams an empty-removal line.
            had_layer_effects = any(e.source_id == card.id for e in self.layers_engine.effects)
            self.layers_engine.remove_effects_from_source(card.id)
            if had_layer_effects:
                print(f"[LAYERS] Removed all effects from source: {card.name}")
        if HAS_REPLACEMENT_ENGINE and self._replacement_engine:
            had_repl_effects = any(e.source_id == card.id for e in self._replacement_engine.effects)
            self._replacement_engine.remove_effects_from_source(card.id)
            if had_repl_effects:
                print(f"[REPLACEMENT] Removed replacement effects from source: {card.name}")

    def has_granted_keyword(self, card: Card, keyword: str, controller_name: str) -> bool:
        """
        Check if a card has been granted a keyword by a continuous effect.

        This is a lightweight check that doesn't go through full layer calculation —
        it just scans active Layer 6 effects for matching keyword grants.
        """
        if not self.layers_engine:
            return False

        keyword_lower = keyword.lower()

        for effect in self.layers_engine.effects:
            if effect.layer != Layer.ABILITY:
                continue

            # Check if keyword is in granted list
            if not any(kw.lower() == keyword_lower for kw in effect.abilities_granted):
                continue

            # Paranoia: the effect's source must still be on the battlefield of
            # the effect's recorded controller. Stale effects from flickered /
            # exiled / copy-token sources are a common source of "Rick's
            # creature got trample from Claude's Uprising" reports.
            source_alive_under_controller = False
            for _p in self.players:
                if _p.name != effect.controller:
                    continue
                for _c in _p.battlefield:
                    if getattr(_c, 'id', None) == effect.source_id and not getattr(_c, '_phased_out', False):
                        source_alive_under_controller = True
                        break
                break
            if not source_alive_under_controller:
                continue

            # Check if this effect applies to the card
            applies_to = effect.applies_to.lower()

            # Custom filter (e.g. power>=N gates for "creatures with power 4 or
            # greater have trample"). Registered at effect creation time.
            if getattr(effect, 'filter_fn', None):
                try:
                    if not effect.filter_fn(card, self):
                        continue
                except Exception:
                    continue

            # Aug 10 deferred (E1): permanents-scoped grants (Avacyn, Angel of
            # Hope) must be checked BEFORE the creature branch and must NOT
            # carry an is_creature() gate — granting indestructible to an
            # artifact, enchantment or land is the entire point. The strings
            # are disjoint, so the order is for clarity, not correctness.
            if "permanents you control" in applies_to:
                if effect.controller != controller_name:
                    continue
                if "other" in applies_to and card.id == effect.source_id:
                    continue
                return True

            if "creatures you control" in applies_to:
                if not card.is_creature():
                    continue
                if effect.controller != controller_name:
                    continue
                # "Other creatures" — source can't grant to itself
                if "other" in applies_to and card.id == effect.source_id:
                    continue
                return True

            elif "all creatures" in applies_to or "each creature" in applies_to:
                if card.is_creature():
                    return True

        return False

    def rebuild_layers_from_battlefield(self):
        """Rebuild all continuous/replacement effects from current battlefield state.
        Called after loading a saved game."""
        if self.layers_engine:
            self.layers_engine.effects.clear()
        if HAS_REPLACEMENT_ENGINE and self._replacement_engine:
            self._replacement_engine.effects.clear()
        for player in self.players:
            for card in player.battlefield:
                self.register_static_keyword_grants(card, player.name)
                self.register_static_pt_effects(card, player.name)
                self.register_replacement_effects(card, player.name)
        self.recalculate_granted_keywords()
        self.recalculate_power_toughness()

    def recalculate_granted_keywords(self):
        """
        Recalculate _granted_keywords for all creatures on the battlefield
        based on active continuous effects in the layers engine.

        Call this after any permanent enters or leaves the battlefield.
        """
        # Clear all granted keywords first
        for player in self.players:
            for card in player.battlefield:
                card._granted_keywords = set()

        if not self.layers_engine:
            return

        # For each active creature on the battlefield, check what keywords are granted
        # Phased-out creatures don't receive or benefit from granted keywords
        for player in self.players:
            for card in player.battlefield:
                if not card.is_creature() or getattr(card, '_phased_out', False):
                    continue
                for keyword in ['Trample', 'Haste', 'Flying', 'Vigilance', 'Lifelink',
                                'Deathtouch', 'First Strike', 'Double Strike', 'Hexproof',
                                'Shroud',  # May 17 audit: Lightning Greaves' shroud was
                                # missing from this list, so Greaves-equipped creatures
                                # were targetable (its protection effect never applied).
                                'Indestructible', 'Menace', 'Reach', 'Defender']:
                    if self.has_granted_keyword(card, keyword, player.name):
                        card._granted_keywords.add(keyword)

        # Log what was granted — only when the set actually changed from the
        # previous sweep. Prevents Surrak-trample-style logs repeating 6+ times
        # per board state every SBA/layers recalculation.
        for player in self.players:
            for card in player.battlefield:
                granted = getattr(card, '_granted_keywords', set())
                prev = getattr(card, '_granted_keywords_logged', None)
                if granted and granted != prev:
                    print(f"[LAYERS] {card.name} ({player.name}): granted {granted}")
                    card._granted_keywords_logged = set(granted)
                elif not granted and prev:
                    card._granted_keywords_logged = set()

    def to_dict(self) -> Dict:
        """Serialize game state to JSON-compatible dict."""
        return {
            "thread_id": self.thread_id,
            "format": self.format,
            "players": [p.to_dict() for p in self.players],
            "turn_number": self.turn_number,
            "active_player_index": self.active_player_index,
            "priority_player_index": self.priority_player_index,
            "phase": self.phase.value,
            "stack": [s.to_dict() if hasattr(s, 'to_dict') else s for s in self.stack],
            "resolution_jobs": {
                job_id: job.to_dict() if hasattr(job, "to_dict") else job
                for job_id, job in self.resolution_jobs.items()
            },
            "narration_outbox": [
                entry.to_dict() if hasattr(entry, "to_dict") else entry
                for entry in self.narration_outbox
            ],
            "resolution_events": {
                event_id: event.to_dict() if hasattr(event, "to_dict") else event
                for event_id, event in self.resolution_events.items()
            },
            "stack_enabled": self.stack_enabled,
            "combat_priority_window": self.combat_priority_window,
            "priority_state": (
                self._priority_system.to_dict()
                if self._priority_system is not None
                and hasattr(self._priority_system, 'to_dict')
                else self._restored_priority_state
            ),
            "experimental_ffa": self.experimental_ffa,
            "attackers": self.attackers,
            "blockers": self.blockers,
            "waiting_for_human_blocks": self.waiting_for_human_blocks,
            "opening_hands_pending": self.opening_hands_pending,
            "combat_defenders_done": self.combat_defenders_done,
            "started": self.started,
            "ended": self.ended,
            "winner": self.winner,
            "elimination_order": self.elimination_order,
            "created_at": self.created_at.isoformat(),
            "pending_action": self.pending_action,
            "pending_choices": self.pending_choices,
            "turn_effects": self.turn_effects,
            "last_unresolved_effect": self.last_unresolved_effect,
            "pending_resolves": self.pending_resolves,
            "temporary_cost_increases": self._temporary_cost_increases,
            "conditional_exile_casts": self.conditional_exile_casts,
        }

    def visible_state(self, viewer_index: int) -> Dict:
        """Per-player filtered snapshot for DISPLAY layers — the React
        websocket serializer's foundation.

        Built July 30, 2026, deliberately BEFORE the frontend exists:
        to_dict() above is the omniscient save-game serializer, and
        hidden-information discipline retrofitted after the first
        "opponent's hand visible in the network tab" bug report is the
        classic failure. Arena's model — the server owns hidden zones, the
        client renders only what its player may see — is the spec here.

        Visibility rules:
          - the viewer's own hand is visible; opponents' hands are COUNTS
          - ALL libraries are counts (contents/order hidden even from the
            owner, CR 401.2)
          - face-down exile (Card._face_down — Necropotence, Gonti) is
            masked to a placeholder for EVERY viewer. Gonti's controller
            may peek per its printed text; per-card peek rights are
            card-specific and unmodeled — masking for all is the
            conservative direction.
          - battlefields, graveyards, the stack, and command zones are
            public (CR 400.2)
        """
        from mtg.choices import choice_views_for

        def _card_public(c):
            if getattr(c, '_face_down', False):
                return {"name": "Face-down card", "face_down": True}
            return c.to_dict()

        players = []
        for idx, p in enumerate(self.players):
            is_viewer = (idx == viewer_index)
            players.append({
                "name": p.name,
                "seat_id": p.seat_id,
                "eliminated": p.eliminated,
                "loss_reason": p.loss_reason,
                "life": p.life,
                "poison": getattr(p, 'poison', 0),
                "is_viewer": is_viewer,
                "hand": ([_card_public(c) for c in p.hand] if is_viewer
                         else {"count": len(p.hand)}),
                "library_count": len(p.library),
                "battlefield": [_card_public(c) for c in p.battlefield],
                "graveyard": [_card_public(c) for c in p.graveyard],
                "exile": [_card_public(c) for c in p.exile],
                "command_zone": [_card_public(c)
                                 for c in (getattr(p, 'command_zone', []) or [])],
                "commander_damage": {str(k): v for k, v in
                                     (getattr(p, 'commander_damage', {}) or {}).items()},
            })
        return {
            "viewer_index": viewer_index,
            "format": self.format,
            "turn_number": self.turn_number,
            "phase": self.phase.value,
            "active_player_index": self.active_player_index,
            "stack": [s.to_dict() if hasattr(s, 'to_dict') else s
                      for s in self.stack],
            "ended": self.ended,
            "winner": self.winner,
            "elimination_order": self.elimination_order,
            "pending_choice": (
                self.pending_action
                if (self.pending_action
                    and self.get_player_by_stable_id(
                        self.pending_action.get('chooser_player_id'),
                        living_only=False) is self.players[viewer_index])
                else None
            ),
            "pending_choices": choice_views_for(self, viewer_index),
            "players": players,
        }

    @classmethod
    def from_dict(cls, data: Dict) -> 'GameState':
        """Reconstruct game state from dict."""
        game = cls(
            thread_id=data["thread_id"],
            format=data["format"],
            players=[Player.from_dict(p) for p in data["players"]],
            turn_number=data.get("turn_number", 0),
            active_player_index=data.get("active_player_index", 0),
            priority_player_index=data.get("priority_player_index", 0),
            phase=Phase(data.get("phase", "main1")),
            stack=[],
            resolution_jobs={},
            stack_enabled=data.get("stack_enabled", False),
            combat_priority_window=data.get("combat_priority_window"),
            experimental_ffa=data.get("experimental_ffa", False),
            attackers=data.get("attackers", []),
            blockers=data.get("blockers", {}),
            waiting_for_human_blocks=data.get("waiting_for_human_blocks", False),
            opening_hands_pending=data.get("opening_hands_pending", False),
            combat_defenders_done=data.get("combat_defenders_done", []),
            started=data.get("started", False),
            ended=data.get("ended", False),
            winner=data.get("winner"),
            elimination_order=data.get("elimination_order", []),
            created_at=datetime.fromisoformat(data["created_at"]) if "created_at" in data else datetime.now(),
            pending_action=data.get("pending_action"),
            pending_choices=data.get("pending_choices", {}),
            turn_effects=data.get("turn_effects", []),
            last_unresolved_effect=data.get("last_unresolved_effect"),
            pending_resolves=data.get("pending_resolves", []),
            _temporary_cost_increases=data.get(
                "temporary_cost_increases", []),
            conditional_exile_casts=data.get("conditional_exile_casts", {}),
        )
        game.resolution_jobs = {
            str(job_id): ResolutionJob.from_dict(payload)
            for job_id, payload in (data.get("resolution_jobs") or {}).items()
        }
        game.narration_outbox = [
            NarrationEntry.from_dict(payload)
            for payload in (data.get("narration_outbox") or [])
            if isinstance(payload, dict)
        ]
        game.resolution_events = {
            str(event_id): ResolutionEvent.from_dict(payload)
            for event_id, payload in (data.get("resolution_events") or {}).items()
            if isinstance(payload, dict)
        }
        restored_stack = []
        for payload in data.get("stack", []):
            if not isinstance(payload, dict):
                continue
            job_id = payload.get("resolution_job_id") or payload.get("entry_id")
            job = game.resolution_jobs.get(str(job_id)) if job_id else None
            entry = StackEntry.from_dict(game, payload, job=job)
            # Version-1 stack snapshots cannot recover exact targets or full
            # cast state. Keep the real object for display/priority migration,
            # but mark the synthesized job honestly instead of name-matching.
            if job is None:
                job = ResolutionJob.capture(
                    game, entry, checkpoint="priority_open")
                job.recovery_error = (
                    "legacy stack snapshot lacks exact target/cast facts")
                entry.resolution_job_id = job.job_id
                game.resolution_jobs[job.job_id] = job
            restored_stack.append(entry)
        game.stack = restored_stack
        for entry in game.stack:
            entry.bind_persisted_targets(game)
        game._restored_priority_state = data.get("priority_state")
        # [LAYERS] Rebuild continuous effects from current battlefield state
        game.rebuild_layers_from_battlefield()
        return game
