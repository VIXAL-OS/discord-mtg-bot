"""Game-state-independent constants and enums.

This module owns the data tables that the rest of the engine consults:
phase ordering, format rules, banned lists, MDFC pathway lands, etc.
Nothing here depends on Card/Player/GameState — only stdlib + Enum.

Extracted from mtg_game.py during the Phase 1 OSS-readability refactor.
Originally lived at lines 397-689 of the monolith.
"""

from enum import Enum


# =============================================================================
# PHASE / ZONE ENUMS
# =============================================================================

class Phase(Enum):
    UNTAP = "untap"
    UPKEEP = "upkeep"
    DRAW = "draw"
    MAIN1 = "main1"
    COMBAT_BEGIN = "combat_begin"
    DECLARE_ATTACKERS = "declare_attackers"
    DECLARE_BLOCKERS = "declare_blockers"
    COMBAT_DAMAGE = "combat_damage"
    COMBAT_END = "combat_end"
    MAIN2 = "main2"
    END = "end"
    CLEANUP = "cleanup"


class Zone(Enum):
    LIBRARY = "library"
    HAND = "hand"
    BATTLEFIELD = "battlefield"
    GRAVEYARD = "graveyard"
    EXILE = "exile"
    COMMAND = "command"
    STACK = "stack"


PHASE_ORDER = [
    Phase.UNTAP, Phase.UPKEEP, Phase.DRAW, Phase.MAIN1,
    Phase.COMBAT_BEGIN, Phase.DECLARE_ATTACKERS, Phase.DECLARE_BLOCKERS,
    Phase.COMBAT_DAMAGE, Phase.COMBAT_END, Phase.MAIN2, Phase.END, Phase.CLEANUP
]

PHASE_NAMES = {
    Phase.UNTAP: "⏫ Untap",
    Phase.UPKEEP: "⬆️ Upkeep",
    Phase.DRAW: "🎴 Draw",
    Phase.MAIN1: "1️⃣ Main Phase 1",
    Phase.COMBAT_BEGIN: "⚔️ Beginning of Combat",
    Phase.DECLARE_ATTACKERS: "🗡️ Declare Attackers",
    Phase.DECLARE_BLOCKERS: "🛡️ Declare Blockers",
    Phase.COMBAT_DAMAGE: "💥 Combat Damage",
    Phase.COMBAT_END: "🏁 End of Combat",
    Phase.MAIN2: "2️⃣ Main Phase 2",
    Phase.END: "🔚 End Step",
    Phase.CLEANUP: "🧹 Cleanup",
}


# =============================================================================
# FORMAT RULES
# =============================================================================

FORMAT_STARTING_LIFE = {
    "standard": 20,
    "modern": 20,
    "legacy": 20,
    "vintage": 20,
    "pioneer": 20,
    "pauper": 20,
    "commander": 40,
    "edh": 40,
    "brawl": 25,
    "oathbreaker": 20,
    "limited": 20,
    "cube": 20,
}

# Format deck construction rules
FORMAT_DECK_SIZE = {
    "standard": (60, None),   # (min, max) - None means no max
    "modern": (60, None),
    "legacy": (60, None),
    "vintage": (60, None),
    "pioneer": (60, None),
    "pauper": (60, None),
    "commander": (100, 100),  # Exactly 100 including commander
    "edh": (100, 100),
    "brawl": (60, 60),        # Exactly 60 including commander
    "oathbreaker": (60, 60),
    "limited": (40, None),
    "cube": (40, None),
}

# Formats that require singleton (no duplicates except basics)
SINGLETON_FORMATS = {"commander", "edh", "brawl", "oathbreaker"}

# Formats that use command zone
COMMAND_ZONE_FORMATS = {"commander", "edh", "brawl", "oathbreaker"}


# =============================================================================
# CARD-TYPE LOOKUPS
# =============================================================================

# [MELD] Known meld pairs: frozenset({half_a, half_b}) -> melded card data
MELD_PAIRS = {
    # Gisela's end-step trigger is handled explicitly in triggers.py. Keep
    # the result's correct copiable values here.
    frozenset({"Bruna, the Fading Light", "Gisela, the Broken Blade"}): {"name": "Brisela, Voice of Nightmares", "type_line": "Legendary Creature — Eldrazi Angel", "power": "9", "toughness": "10", "mana_cost": "", "oracle_text": "Flying, first strike, vigilance, lifelink\nYour opponents can't cast spells with mana value 3 or less."},
    frozenset({"Urza, Lord Protector", "The Mightstone and Weakstone"}): {"name": "Urza, Planeswalker", "type_line": "Legendary Planeswalker", "power": None, "toughness": None, "mana_cost": "", "loyalty": "7", "oracle_text": "Planeswalker abilities"},
    frozenset({"Mishra, Claimed by Gix", "Phyrexian Dragon Engine"}): {"name": "Mishra, Lost to Phyrexia", "type_line": "Legendary Creature", "power": "9", "toughness": "9", "mana_cost": "", "oracle_text": "Trample"},
}

# Basic lands (exempt from singleton)
BASIC_LAND_NAMES = {
    "plains", "island", "swamp", "mountain", "forest",
    "snow-covered plains", "snow-covered island", "snow-covered swamp",
    "snow-covered mountain", "snow-covered forest",
    "wastes"
}

# Banned lists by format (partial - high-impact cards)
# Full lists would be fetched from Scryfall
BANNED_CARDS = {
    "commander": {
        # Power Nine + most broken. NOTE: Timetwister is deliberately NOT
        # here — it is the one Power Nine card that has always been LEGAL
        # in Commander (July 21, 2026 audit; Scryfall legalities are the
        # ground truth).
        "ancestral recall", "black lotus", "mox emerald", "mox jet",
        "mox pearl", "mox ruby", "mox sapphire", "time walk",
        # Commander banlist. July 21, 2026 audit: pruned the 2024-2025
        # unbans (Gifts Ungiven, Braids, Coalition Victory, Lutri,
        # Panoptic Mirror, Sway of the Stars, Biorhythm, Worldfire) after
        # the new deck-list pin caught the validator stripping a LEGAL
        # Worldfire from mythic_madness every load. Verified against
        # data/scryfall_oracle_cards.json legalities.
        "balance", "channel",
        "emrakul, the aeons torn", "erayo, soratami ascendant",
        "falling star", "fastbond", "flash", "golos, tireless pilgrim",
        "griselbrand", "hullbreacher", "iona, shield of emeria", "karakas",
        "leovold, emissary of trest", "library of alexandria", "limited resources",
        "mana crypt", "paradox engine",
        "primeval titan", "prophet of kruphix", "recurring nightmare", "rofellos, llanowar emissary",
        "shahrazad", "sundering titan", "sylvan primordial",
        "tinker", "tolarian academy", "trade secrets", "upheaval",
        "yawgmoth's bargain", "dockside extortionist",
    },
    # Modern banlist as of late 2024 / 2025. Power Nine and pre-8th-edition
    # cards aren't included — they're already format-illegal (not banned),
    # and the Scryfall `legalities.modern` lookup catches them.
    #
    # Refreshed Apr 30 2026 from the official MTG B&R history:
    # - UNBANNED: Faithless Looting (Mar 2024), Lurrus of the Dream-Den
    #   (Apr 2024), Splinter Twin / Mox Opal / Green Sun's Zenith /
    #   Krark-Clan Ironworks (Sep 2024).
    # - NEWLY BANNED: Nadu, Winged Wisdom (Aug 2024), Grief / Up the Beanstalk /
    #   The One Ring / Underworld Breach (Sep 2024).
    "modern": {
        "blazing shoal", "bridge from below", "chrome mox", "cloudpost",
        "dark depths", "deathrite shaman", "dig through time", "dread return",
        "eye of ugin", "field of the dead", "gitaxian probe",
        "glimpse of nature", "golgari grave-troll",
        "grief", "hogaak, arisen necropolis", "hypergenesis",
        "mental misstep", "mycosynth lattice", "mystic sanctuary",
        "nadu, winged wisdom", "oko, thief of crowns", "once upon a time",
        "ponder", "preordain", "punishing fire", "rite of flame",
        "second sunrise", "seething song", "sensei's divining top",
        "simian spirit guide", "skullclamp", "summer bloom",
        "the one ring", "tibalt's trickery", "treasure cruise",
        "umezawa's jitte", "underworld breach", "up the beanstalk",
        "uro, titan of nature's wrath",
    },
    "legacy": {
        "ancestral recall", "black lotus", "mox emerald", "mox jet",
        "mox pearl", "mox ruby", "mox sapphire", "time walk", "timetwister",
        "balance", "channel", "demonic consultation", "demonic tutor", "earthcraft",
        "fastbond", "flash", "frantic search", "goblin recruiter", "gush",
        "hermit druid", "imperial seal", "library of alexandria", "mana crypt",
        "mana drain", "mana vault", "memory jar", "mind twist", "mind's desire",
        "mishra's workshop", "mystical tutor", "necropotence", "oath of druids",
        "skullclamp", "sol ring", "strip mine",
        "survival of the fittest", "time vault", "tinker", "tolarian academy",
        "treasure cruise", "vampiric tutor", "wheel of fortune", "windfall",
        "yawgmoth's bargain", "yawgmoth's will",
    },
    "standard": set(),  # Changes frequently, would need API
    "pioneer": {
        "balustrade spy", "bloodstained mire", "felidar guardian", "field of the dead",
        "flooded strand", "inverter of truth", "kethis, the hidden hand",
        "leyline of abundance", "lurrus of the dream-den", "nexus of fate",
        "oko, thief of crowns", "once upon a time", "polluted delta",
        "smuggler's copter", "teferi, time raveler", "undercity informer",
        "underworld breach", "uro, titan of nature's wrath", "veil of summer",
        "walking ballista", "wilderness reclamation", "windswept heath", "wooded foothills",
    },
    # Pauper: this list is now belt-and-suspenders. The deck validator (see
    # mtg/models.py FormatValidator.validate_deck) primarily checks Scryfall's
    # `legalities.pauper` field, which catches both not-legal-at-common and
    # explicit bans. Keeping this list ensures we still flag banned cards if
    # the Scryfall cache lookup misses.
    "pauper": {
        "arcum's astrolabe", "atog", "bonder's ornament", "chatterstorm",
        "cloud of faeries", "cloudpost", "cranial plating", "daze",
        "disciple of the vault", "empty the warrens", "frantic search",
        "galvanic relay", "gitaxian probe", "grapeshot", "gush",
        "high tide", "hymn to tourach", "invigorate",
        "monastery swiftspear", "mystic sanctuary",
        "peregrine drake", "prophetic prism", "sinkhole", "sojourner's companion",
        "temporal fissure", "treasure cruise",
    },
}

# Color identity mapping for mana symbols
MANA_COLOR_IDENTITY = {
    'W': 'W', 'U': 'U', 'B': 'B', 'R': 'R', 'G': 'G',
    'C': '',  # Colorless has no identity
    'X': '',  # X is colorless
}


# =============================================================================
# KEYWORD LIST
# =============================================================================
# Single source of truth for keywords recognized by Card._parse_keywords.
# Prefer the rules.keywords enum when available; fall back to a hardcoded
# list so the engine still works if rules/keywords.py isn't deployed.

try:
    from rules.keywords import Keyword
    _KEYWORD_LIST = [kw.value for kw in Keyword]
except ImportError:
    _KEYWORD_LIST = [
        'Flying', 'First strike', 'Double strike', 'Deathtouch', 'Haste',
        'Hexproof', 'Indestructible', 'Lifelink', 'Menace', 'Reach',
        'Trample', 'Vigilance', 'Flash', 'Defender', 'Fear', 'Intimidate',
        'Skulk', 'Protection', 'Shroud', 'Unblockable', 'Wither', 'Infect',
        'Persist', 'Undying', 'Prowess', 'Exalted', 'Flanking',
    ]


# =============================================================================
# MDFC (Modal Double-Faced Card) PATHWAYS
# =============================================================================

# Pathway lands - Format: "front_name": ("back_name", "front_produces", "back_produces")
MDFC_PATHWAYS = {
    # Zendikar Rising
    "branchloft pathway": ("boulderloft pathway", "G", "W"),
    "brightclimb pathway": ("grimclimb pathway", "W", "B"),
    "clearwater pathway": ("murkwater pathway", "U", "B"),
    "cragcrown pathway": ("timbercrown pathway", "R", "G"),
    "needleverge pathway": ("pillarverge pathway", "R", "W"),
    "riverglide pathway": ("lavaglide pathway", "U", "R"),
    # Kaldheim
    "barkchannel pathway": ("tidechannel pathway", "G", "U"),
    "blightstep pathway": ("searstep pathway", "B", "R"),
    "darkbore pathway": ("slitherbore pathway", "B", "G"),
    "hengegate pathway": ("mistgate pathway", "W", "U"),
}
