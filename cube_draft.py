"""
Cube Draft Engine for Discord
==============================
8-seat booster draft simulation with AI bots, Claude, and human players.
Supports cube loading from text/JSON files and CubeCobra URLs.

Commands:
- !cube <url_or_file>    - Load a cube list
- !draft <opponent>      - Start a draft (claude, @user, or solo)
- !pick <number>         - Pick a card from current pack
- !pack                  - Re-show current pack (DM)
- !pool                  - View drafted pool (DM)
- !build                 - Show deck building interface
- !addcard <name>        - Add card from pool to deck
- !cut <name>            - Remove card from deck to sideboard
- !autoland [counts]     - Auto-fill basic lands
- !finalize              - Lock in deck
- !draftgame             - Start post-draft game
- !draftstatus           - Show draft progress
- !autodraft [source]    - Run fully automated draft + game (testing)
"""

import discord
from discord.ext import commands
from dataclasses import dataclass, field
from typing import Optional, Dict, List, Tuple, Any
from datetime import datetime
import json
import random
import asyncio
import aiohttp
import os
import re
import time as _time
import anthropic

# Import Card and related classes from game engine
from mtg_game import (
    Card, Player, GameState, Phase, GameEngine,
    FORMAT_STARTING_LIFE, FORMAT_DECK_SIZE,
    DeckLoader, FormatValidator,
    GameDisplay, GameLogger,
)

# =============================================================================
# CONSTANTS
# =============================================================================

POD_SIZE = 8
PACK_SIZE = 15
NUM_ROUNDS = 3
DEFAULT_DECK_SIZE = 40
DEFAULT_STARTING_LIFE = 20

# Fun bot names for the 6 AI filler seats
BOT_NAMES = [
    "Bot-Nissa", "Bot-Jace", "Bot-Chandra", "Bot-Liliana",
    "Bot-Gideon", "Bot-Teferi", "Bot-Karn", "Bot-Vivien",
    "Bot-Ajani", "Bot-Sorin", "Bot-Elspeth", "Bot-Garruk",
]

# Keywords that signal strong draft picks (for bot heuristic)
REMOVAL_KEYWORDS = [
    'destroy target', 'exile target', 'deals.*damage to', 'target creature gets -',
    '-x/-x', 'destroy all', 'fights target', 'return target.*to.*hand',
]
EVASION_KEYWORDS = ['flying', 'menace', 'trample', 'fear', 'intimidate', 'unblockable', 'skulk']
BOMB_KEYWORDS = ['when.*enters', 'whenever.*dies', 'at the beginning of', 'you may', 'draw.*card']

# Color letters for mana analysis
COLORS = ['W', 'U', 'B', 'R', 'G']


# =============================================================================
# DATA MODELS
# =============================================================================

@dataclass
class DraftSeat:
    """One seat in the 8-seat draft pod."""
    seat_index: int
    name: str
    is_human: bool = False
    is_claude: bool = False
    discord_user_id: Optional[int] = None

    # Card pools
    pool: List[Card] = field(default_factory=list)       # All drafted cards
    deck: List[Card] = field(default_factory=list)        # Final deck (pool subset + basics)
    sideboard: List[Card] = field(default_factory=list)   # Pool cards not in deck

    # Bot AI state
    color_preferences: List[str] = field(default_factory=list)  # Top 2 colors
    has_picked: bool = False          # Has this seat picked from current pack?
    deck_finalized: bool = False      # Ready to play?

    def to_dict(self) -> Dict:
        return {
            "seat_index": self.seat_index,
            "name": self.name,
            "is_human": self.is_human,
            "is_claude": self.is_claude,
            "discord_user_id": self.discord_user_id,
            "pool": [c.to_dict() for c in self.pool],
            "deck": [c.to_dict() for c in self.deck],
            "sideboard": [c.to_dict() for c in self.sideboard],
            "color_preferences": self.color_preferences,
            "has_picked": self.has_picked,
            "deck_finalized": self.deck_finalized,
        }

    @staticmethod
    def from_dict(data: Dict) -> 'DraftSeat':
        seat = DraftSeat(
            seat_index=data["seat_index"],
            name=data["name"],
            is_human=data.get("is_human", False),
            is_claude=data.get("is_claude", False),
            discord_user_id=data.get("discord_user_id"),
            color_preferences=data.get("color_preferences", []),
            has_picked=data.get("has_picked", False),
            deck_finalized=data.get("deck_finalized", False),
        )
        seat.pool = [Card.from_dict(c) for c in data.get("pool", [])]
        seat.deck = [Card.from_dict(c) for c in data.get("deck", [])]
        seat.sideboard = [Card.from_dict(c) for c in data.get("sideboard", [])]
        return seat


@dataclass
class DraftState:
    """Full state of an in-progress draft."""
    thread_id: int
    phase: str  # "picking" | "deck_building" | "ready" | "game_started"
    cube_name: str

    # Pod setup
    seats: List[DraftSeat] = field(default_factory=list)
    human_seats: List[int] = field(default_factory=list)      # Seat indices of humans
    claude_seat: Optional[int] = None                          # Seat index of Claude

    # Draft progress
    pack_round: int = 1              # 1, 2, or 3
    pick_number: int = 1             # 1-15 within a round
    pass_direction: int = 1          # +1 = left, -1 = right

    # Packs: seat_index -> list of Card objects currently in front of that seat
    packs: Dict[int, List[Card]] = field(default_factory=dict)

    # All 3 rounds of packs pre-dealt: round_num -> seat_index -> list of Cards
    all_packs: Dict[int, Dict[int, List[Card]]] = field(default_factory=dict)

    # Settings
    pack_size: int = PACK_SIZE
    num_rounds: int = NUM_ROUNDS
    deck_size: int = DEFAULT_DECK_SIZE
    starting_life: int = DEFAULT_STARTING_LIFE

    created_at: str = ""

    def to_dict(self) -> Dict:
        packs_dict = {}
        for seat_idx, cards in self.packs.items():
            packs_dict[str(seat_idx)] = [c.to_dict() for c in cards]

        all_packs_dict = {}
        for round_num, round_packs in self.all_packs.items():
            all_packs_dict[str(round_num)] = {}
            for seat_idx, cards in round_packs.items():
                all_packs_dict[str(round_num)][str(seat_idx)] = [c.to_dict() for c in cards]

        return {
            "thread_id": self.thread_id,
            "phase": self.phase,
            "cube_name": self.cube_name,
            "seats": [s.to_dict() for s in self.seats],
            "human_seats": self.human_seats,
            "claude_seat": self.claude_seat,
            "pack_round": self.pack_round,
            "pick_number": self.pick_number,
            "pass_direction": self.pass_direction,
            "packs": packs_dict,
            "all_packs": all_packs_dict,
            "pack_size": self.pack_size,
            "num_rounds": self.num_rounds,
            "deck_size": self.deck_size,
            "starting_life": self.starting_life,
            "created_at": self.created_at,
        }

    @staticmethod
    def from_dict(data: Dict) -> 'DraftState':
        draft = DraftState(
            thread_id=data["thread_id"],
            phase=data["phase"],
            cube_name=data["cube_name"],
            human_seats=data.get("human_seats", []),
            claude_seat=data.get("claude_seat"),
            pack_round=data.get("pack_round", 1),
            pick_number=data.get("pick_number", 1),
            pass_direction=data.get("pass_direction", 1),
            pack_size=data.get("pack_size", PACK_SIZE),
            num_rounds=data.get("num_rounds", NUM_ROUNDS),
            deck_size=data.get("deck_size", DEFAULT_DECK_SIZE),
            starting_life=data.get("starting_life", DEFAULT_STARTING_LIFE),
            created_at=data.get("created_at", ""),
        )
        draft.seats = [DraftSeat.from_dict(s) for s in data.get("seats", [])]

        # Restore packs
        for seat_str, cards_data in data.get("packs", {}).items():
            draft.packs[int(seat_str)] = [Card.from_dict(c) for c in cards_data]

        # Restore all_packs
        for round_str, round_data in data.get("all_packs", {}).items():
            draft.all_packs[int(round_str)] = {}
            for seat_str, cards_data in round_data.items():
                draft.all_packs[int(round_str)][int(seat_str)] = [Card.from_dict(c) for c in cards_data]

        return draft


# =============================================================================
# CUBE LOADER
# =============================================================================

class CubeLoader:
    """Load cube lists from text, JSON, or CubeCobra."""

    def __init__(self, deck_loader: DeckLoader):
        # Reuse the game engine's DeckLoader for Scryfall fetching + cache
        self.deck_loader = deck_loader

    async def load_from_text(self, text: str) -> Tuple[List[Card], str]:
        """
        Load cube from text (one card name per line).
        Returns (cards, cube_name).
        """
        lines = [line.strip() for line in text.strip().split('\n') if line.strip()]
        # Filter out comments and section headers
        card_names = []
        for line in lines:
            if line.startswith('#') or line.startswith('//'):
                continue
            # Handle "1x Card Name" or "1 Card Name" format
            match = re.match(r'^(\d+)x?\s+(.+)$', line)
            if match:
                qty = int(match.group(1))
                name = match.group(2).strip()
                for _ in range(qty):
                    card_names.append(name)
            else:
                card_names.append(line)

        if not card_names:
            raise ValueError("No card names found in text!")

        cards = await self._fetch_cards(card_names)
        return cards, f"Custom Cube ({len(cards)} cards)"

    async def load_from_json(self, json_data: Dict) -> Tuple[List[Card], str]:
        """
        Load cube from JSON.
        Expected: {"name": "My Cube", "cards": ["Card A", "Card B", ...]}
        """
        cube_name = json_data.get("name", "Unnamed Cube")
        card_names = json_data.get("cards", [])
        if not card_names:
            raise ValueError("JSON cube has no 'cards' list!")

        cards = await self._fetch_cards(card_names)
        return cards, cube_name

    async def load_from_cubecobra(self, cube_id: str) -> Tuple[List[Card], str]:
        """
        Load cube from CubeCobra API.
        cube_id extracted from URLs like cubecobra.com/cube/list/my-cube-id
        """
        url = f"https://cubecobra.com/cube/api/cubeJSON/{cube_id}"
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                if resp.status != 200:
                    raise ValueError(
                        f"CubeCobra API returned status {resp.status}. "
                        f"Check the cube ID: {cube_id}"
                    )
                data = await resp.json()

        cube_name = data.get("name", f"CubeCobra: {cube_id}")
        # CubeCobra returns cards as objects with cardID, details, etc.
        card_names = []
        for card_entry in data.get("cards", {}).get("mainboard", []):
            # Try different CubeCobra JSON shapes
            name = None
            if isinstance(card_entry, str):
                name = card_entry
            elif isinstance(card_entry, dict):
                details = card_entry.get("details", {})
                name = details.get("name") or card_entry.get("name")
            if name:
                card_names.append(name)

        if not card_names:
            raise ValueError(f"No cards found in CubeCobra cube '{cube_id}'")

        print(f"[CUBE] Loading {len(card_names)} cards from CubeCobra cube '{cube_name}'")
        cards = await self._fetch_cards(card_names)
        return cards, cube_name

    async def _fetch_cards(self, card_names: List[str]) -> List[Card]:
        """Fetch card data from Scryfall for a list of card names."""
        cards = []
        total = len(card_names)
        failed = []

        for i, name in enumerate(card_names):
            if (i + 1) % 50 == 0:
                print(f"[CUBE] Loading card {i + 1}/{total}...")
            # Rate-limit Scryfall: 75ms between requests (max ~13/sec)
            if i > 0 and name.lower() not in self.deck_loader.card_cache:
                await asyncio.sleep(0.08)

            try:
                scryfall_data = await self.deck_loader.fetch_card_data(name)
                card = Card(
                    name=scryfall_data.get("name", name),
                    mana_cost=scryfall_data.get("mana_cost", ""),
                    type_line=scryfall_data.get("type_line", ""),
                    oracle_text=scryfall_data.get("oracle_text", ""),
                    power=scryfall_data.get("power"),
                    toughness=scryfall_data.get("toughness"),
                    loyalty=scryfall_data.get("loyalty"),
                    keywords=scryfall_data.get("keywords", []),
                )
                # Extract adventure data
                self.deck_loader._extract_adventure_data(card, scryfall_data)
                cards.append(card)
            except Exception as e:
                failed.append(name)
                print(f"[CUBE] Failed to load '{name}': {e}")

        if failed:
            print(f"[CUBE] {len(failed)} cards failed to load: {failed[:10]}{'...' if len(failed) > 10 else ''}")

        return cards


# =============================================================================
# BOT DRAFT HEURISTIC
# =============================================================================

def _get_card_colors(card: Card) -> List[str]:
    """Extract colors from a card's mana cost."""
    colors = []
    if not card.mana_cost:
        return colors
    for c in COLORS:
        if f'{{{c}}}' in card.mana_cost or f'{{{c}/' in card.mana_cost or f'/{c}}}' in card.mana_cost:
            colors.append(c)
    return colors


def _score_card_for_bot(card: Card, color_prefs: List[str], pick_number: int) -> float:
    """
    Score a card for a heuristic bot drafter.
    Higher = better pick.
    """
    score = 5.0  # Base score for any card
    oracle_lower = (card.oracle_text or "").lower()
    type_lower = (card.type_line or "").lower()

    # Removal is king in limited
    for pattern in REMOVAL_KEYWORDS:
        if re.search(pattern, oracle_lower):
            score += 8.0
            break

    # Evasion is very good
    for kw in EVASION_KEYWORDS:
        if kw in oracle_lower or kw in [k.lower() for k in card.keywords]:
            score += 3.0
            break

    # "Bomb" indicators (ETBs, repeated value, card draw)
    for pattern in BOMB_KEYWORDS:
        if re.search(pattern, oracle_lower):
            score += 2.0

    # Creatures are generally good in limited
    if 'creature' in type_lower:
        score += 2.0
        # Prefer good stats for cost
        try:
            p = int(card.power) if card.power else 0
            t = int(card.toughness) if card.toughness else 0
            if p + t > card.cmc * 2:
                score += 1.5  # Above-rate body
        except (ValueError, TypeError):
            pass

    # Planeswalkers are bombs
    if 'planeswalker' in type_lower:
        score += 10.0

    # Color preference bonus (stronger as draft progresses)
    card_colors = _get_card_colors(card)
    if color_prefs and card_colors:
        matching = sum(1 for c in card_colors if c in color_prefs)
        if matching == len(card_colors):
            # All colors match preferences
            color_bonus = 3.0 + (pick_number * 0.2)  # Gets stronger later
            score += color_bonus
        elif matching == 0:
            # Completely off-color — penalty increases as draft goes on
            penalty = 2.0 + (pick_number * 0.3)
            score -= penalty

    # Colorless cards (artifacts, lands) are flexible
    if not card_colors and ('artifact' in type_lower or 'land' in type_lower):
        score += 1.5

    # Lands with abilities are good fixing
    if 'land' in type_lower and oracle_lower:
        score += 2.0

    # Small random factor to break ties
    score += random.uniform(0, 0.5)

    return score


def _update_bot_color_prefs(seat: DraftSeat):
    """
    After 5+ picks, lock the bot into its top 2 colors
    based on what it's drafted so far.
    """
    if len(seat.pool) < 5:
        return

    color_counts = {c: 0 for c in COLORS}
    for card in seat.pool:
        for c in _get_card_colors(card):
            color_counts[c] += 1

    # Sort by count, take top 2
    sorted_colors = sorted(color_counts.items(), key=lambda x: x[1], reverse=True)
    seat.color_preferences = [c for c, count in sorted_colors[:2] if count > 0]


def bot_make_pick(seat: DraftSeat, pack: List[Card], pick_number: int) -> Card:
    """Have a heuristic bot pick a card from a pack."""
    if not pack:
        return None

    scored = [(card, _score_card_for_bot(card, seat.color_preferences, pick_number)) for card in pack]
    scored.sort(key=lambda x: x[1], reverse=True)
    chosen = scored[0][0]

    seat.pool.append(chosen)
    pack.remove(chosen)
    seat.has_picked = True

    _update_bot_color_prefs(seat)
    return chosen


# =============================================================================
# CLAUDE DRAFT AI
# =============================================================================

async def claude_make_pick(
    client: anthropic.Anthropic,
    seat: DraftSeat,
    pack: List[Card],
    pack_round: int,
    pick_number: int,
    usage_callback=None,
) -> Card:
    """Have Claude AI pick a card from a pack."""
    if not pack:
        return None

    pool_str = ", ".join(c.name for c in seat.pool) if seat.pool else "(empty)"
    pack_lines = []
    for i, c in enumerate(pack):
        cost = c.mana_cost or "(no cost)"
        type_short = c.type_line.split("—")[0].strip() if c.type_line else ""
        pack_lines.append(f"  {i}. {c.name}  {cost}  {type_short}")

    prompt = (
        f"You're drafting a 40-card limited deck. Pack {pack_round}, Pick {pick_number}.\n"
        f"Your pool so far ({len(seat.pool)} cards): {pool_str}\n\n"
        f"Pack contents:\n" + "\n".join(pack_lines) + "\n\n"
        f"Pick the single best card for your draft. Reply with ONLY the card number (e.g. '3')."
    )

    try:
        response = await asyncio.to_thread(
            client.messages.create,
            model="claude-sonnet-4-6",
            max_tokens=50,
            messages=[{"role": "user", "content": prompt}],
        )

        if usage_callback and hasattr(response, 'usage'):
            usage_callback(response.usage, "claude-sonnet-4-6")

        text = response.content[0].text.strip()
        # Parse the number from Claude's response
        match = re.search(r'(\d+)', text)
        if match:
            idx = int(match.group(1))
            if 0 <= idx < len(pack):
                chosen = pack[idx]
                seat.pool.append(chosen)
                pack.remove(chosen)
                seat.has_picked = True
                _update_bot_color_prefs(seat)
                print(f"[DRAFT-CLAUDE] Picked {chosen.name} (index {idx})")
                return chosen

        # Fallback: Claude gave something unparseable
        print(f"[DRAFT-CLAUDE] Unparseable response: '{text}', falling back to heuristic")
    except Exception as e:
        print(f"[DRAFT-CLAUDE] API error: {e}, falling back to heuristic")

    # Fallback: use bot heuristic
    return bot_make_pick(seat, pack, pick_number)


# =============================================================================
# AUTO DECK BUILDER (for Claude + as suggestion for humans)
# =============================================================================

def auto_build_deck(pool: List[Card], deck_size: int = 40) -> Tuple[List[Card], List[Card]]:
    """
    Automatically build a limited deck from a draft pool.
    Returns (deck, sideboard).
    Strategy: pick the best 2 colors, take ~23 nonlands + ~17 basics.
    """
    # Count colors in pool
    color_counts = {c: 0 for c in COLORS}
    for card in pool:
        for c in _get_card_colors(card):
            color_counts[c] += 1

    sorted_colors = sorted(color_counts.items(), key=lambda x: x[1], reverse=True)
    main_colors = [c for c, count in sorted_colors[:2] if count > 0]

    if not main_colors:
        main_colors = ['W', 'U']  # Default if pool is all colorless

    # Score each card for the chosen colors
    nonland_pool = [c for c in pool if 'land' not in (c.type_line or '').lower() or c.oracle_text]
    land_pool = [c for c in pool if 'land' in (c.type_line or '').lower() and not c.oracle_text]

    scored = []
    for card in nonland_pool:
        card_colors = _get_card_colors(card)
        score = 5.0

        # On-color bonus
        if card_colors:
            matching = sum(1 for c in card_colors if c in main_colors)
            if matching == len(card_colors):
                score += 5.0
            elif matching > 0:
                score += 2.0  # Splash
            else:
                score -= 10.0  # Off-color, skip unless amazing

        # Creatures are important in limited
        if 'creature' in (card.type_line or '').lower():
            score += 2.0

        # Removal
        oracle_lower = (card.oracle_text or '').lower()
        for pattern in REMOVAL_KEYWORDS:
            if re.search(pattern, oracle_lower):
                score += 3.0
                break

        scored.append((card, score))

    scored.sort(key=lambda x: x[1], reverse=True)

    # Take top ~23 nonlands (deck_size - 17 lands)
    num_nonlands = deck_size - 17
    deck_nonlands = [card for card, _ in scored[:num_nonlands]]
    sideboard_nonlands = [card for card, _ in scored[num_nonlands:]]

    # Also add any special lands from the pool
    deck_lands_from_pool = []
    for land in land_pool:
        land_colors = _get_card_colors(land)
        if not land_colors or any(c in main_colors for c in land_colors):
            deck_lands_from_pool.append(land)

    # Remove pool lands from sideboard count and adjust basic count
    deck = deck_nonlands + deck_lands_from_pool
    basics_needed = deck_size - len(deck)

    # Distribute basics based on color symbols in deck
    deck_color_counts = {c: 0 for c in COLORS}
    for card in deck_nonlands:
        for c in _get_card_colors(card):
            if c in main_colors:
                deck_color_counts[c] += 1

    total_symbols = sum(deck_color_counts[c] for c in main_colors) or 1
    for c in main_colors:
        count = max(1, round(basics_needed * deck_color_counts[c] / total_symbols))
        basic_name = {'W': 'Plains', 'U': 'Island', 'B': 'Swamp', 'R': 'Mountain', 'G': 'Forest'}[c]
        for _ in range(count):
            if len(deck) < deck_size:
                deck.append(Card(name=basic_name, type_line="Basic Land"))

    # Fill any remaining slots with the most-needed basic
    while len(deck) < deck_size:
        basic_name = {'W': 'Plains', 'U': 'Island', 'B': 'Swamp', 'R': 'Mountain', 'G': 'Forest'}[main_colors[0]]
        deck.append(Card(name=basic_name, type_line="Basic Land"))

    sideboard = sideboard_nonlands + [l for l in land_pool if l not in deck_lands_from_pool]
    return deck, sideboard


# =============================================================================
# DISPLAY HELPERS
# =============================================================================

def format_pack_display(pack: List[Card], pack_round: int, pick_number: int,
                        pool_size: int, color_prefs: List[str]) -> str:
    """Format a pack for DM display."""
    color_str = "/".join(color_prefs) if color_prefs else "?"
    lines = [
        f"**Pack {pack_round}, Pick {pick_number}** ({len(pack)} cards remaining)",
        f"Your pool: {pool_size} cards | Strongest colors: {color_str}",
        "",
    ]
    for i, card in enumerate(pack):
        cost = card.mana_cost or ""
        type_short = card.type_line.split("\u2014")[0].strip() if card.type_line else ""
        # Truncate long oracle text
        oracle_preview = ""
        if card.oracle_text:
            oracle_preview = card.oracle_text[:60]
            if len(card.oracle_text) > 60:
                oracle_preview += "..."
        lines.append(f"  `{i}` \u2014 **{card.name}**  {cost}  *{type_short}*")
        if oracle_preview:
            lines.append(f"       {oracle_preview}")

    lines.append(f"\nReply with `!pick <number>` in the draft thread.")
    return "\n".join(lines)


def format_pool_display(pool: List[Card]) -> str:
    """Format drafted pool grouped by color then CMC."""
    if not pool:
        return "Your pool is empty!"

    # Group by primary color
    color_groups = {c: [] for c in COLORS}
    color_groups['Colorless'] = []
    color_groups['Multi'] = []

    for card in pool:
        colors = _get_card_colors(card)
        if len(colors) == 0:
            color_groups['Colorless'].append(card)
        elif len(colors) > 1:
            color_groups['Multi'].append(card)
        else:
            color_groups[colors[0]].append(card)

    # Sort each group by CMC
    for group in color_groups.values():
        group.sort(key=lambda c: c.cmc)

    color_names = {'W': 'White', 'U': 'Blue', 'B': 'Black', 'R': 'Red', 'G': 'Green'}
    lines = [f"**Your Draft Pool** ({len(pool)} cards)\n"]

    for key in COLORS + ['Multi', 'Colorless']:
        cards = color_groups[key]
        if not cards:
            continue
        label = color_names.get(key, key)
        lines.append(f"__{label}__ ({len(cards)})")
        for card in cards:
            cost = card.mana_cost or ""
            type_short = card.type_line.split("\u2014")[0].strip() if card.type_line else ""
            lines.append(f"  {card.name}  {cost}  *{type_short}*")
        lines.append("")

    return "\n".join(lines)


def format_deck_building_display(seat: DraftSeat) -> str:
    """Format deck building view."""
    lines = [f"**Deck Building** \u2014 {len(seat.deck)}/{DEFAULT_DECK_SIZE} cards\n"]

    if seat.deck:
        # Group deck by type
        creatures = [c for c in seat.deck if 'creature' in (c.type_line or '').lower()]
        spells = [c for c in seat.deck if 'creature' not in (c.type_line or '').lower()
                  and 'land' not in (c.type_line or '').lower()]
        lands = [c for c in seat.deck if 'land' in (c.type_line or '').lower()]

        if creatures:
            lines.append(f"__Creatures__ ({len(creatures)})")
            for c in sorted(creatures, key=lambda x: x.cmc):
                lines.append(f"  {c.name}  {c.mana_cost}")
        if spells:
            lines.append(f"__Spells__ ({len(spells)})")
            for c in sorted(spells, key=lambda x: x.cmc):
                lines.append(f"  {c.name}  {c.mana_cost}")
        if lands:
            lines.append(f"__Lands__ ({len(lands)})")
            # Group basic lands by name
            land_counts = {}
            for c in lands:
                land_counts[c.name] = land_counts.get(c.name, 0) + 1
            for name, count in sorted(land_counts.items()):
                lines.append(f"  {name} x{count}" if count > 1 else f"  {name}")
        lines.append("")

    if seat.sideboard:
        lines.append(f"__Sideboard__ ({len(seat.sideboard)} cards)")
        for c in sorted(seat.sideboard, key=lambda x: x.cmc):
            lines.append(f"  {c.name}  {c.mana_cost or ''}")
        lines.append("")

    needed = DEFAULT_DECK_SIZE - len(seat.deck)
    if needed > 0:
        lines.append(f"Need {needed} more cards. Use `!addcard <name>` or `!autoland`")
    else:
        lines.append(f"Deck is {len(seat.deck)} cards. Use `!finalize` when ready!")

    return "\n".join(lines)


# =============================================================================
# DRAFT COG
# =============================================================================

class CubeDraftCog(commands.Cog, name="Cube Draft"):
    """Cube draft commands."""

    DRAFTS_DIR = "data/drafts"

    def __init__(self, bot):
        self.bot = bot
        self.drafts: Dict[int, DraftState] = {}   # thread_id -> DraftState
        self.loaded_cubes: Dict[int, Tuple[List[Card], str]] = {}  # user_id -> (cards, name)

        # Get engine reference from MTGGameCog for Scryfall cache + game creation
        self.engine: Optional[GameEngine] = None
        self.deck_loader = DeckLoader()  # Own loader in case MTGGameCog isn't loaded yet
        self.cube_loader = CubeLoader(self.deck_loader)

        os.makedirs(self.DRAFTS_DIR, exist_ok=True)
        self._load_all_drafts()

    async def cog_load(self):
        """Called when cog loads — grab engine reference from MTGGameCog."""
        game_cog = self.bot.get_cog("MTG Game")
        if game_cog:
            self.engine = game_cog.engine
            self.game_cog = game_cog  # For reusing autoplay methods in !autodraft
            # Share the Scryfall card cache
            self.cube_loader.deck_loader = self.engine.deck_loader
            print("[DRAFT] Linked to MTG Game engine (shared Scryfall cache)")
        else:
            self.game_cog = None
            print("[DRAFT] MTGGameCog not loaded yet, using standalone DeckLoader")

    def _load_all_drafts(self):
        """Load saved drafts from disk on startup."""
        if not os.path.exists(self.DRAFTS_DIR):
            return
        for filename in os.listdir(self.DRAFTS_DIR):
            if filename.endswith('.json'):
                filepath = os.path.join(self.DRAFTS_DIR, filename)
                try:
                    with open(filepath, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                    draft = DraftState.from_dict(data)
                    if draft.phase != "game_started":
                        self.drafts[draft.thread_id] = draft
                        print(f"[DRAFT] Loaded draft in thread {draft.thread_id}")
                except Exception as e:
                    print(f"[DRAFT] Failed to load draft from {filename}: {e}")

    def _save_draft(self, draft: DraftState):
        """Save draft state to disk."""
        filepath = os.path.join(self.DRAFTS_DIR, f"{draft.thread_id}.json")
        try:
            with open(filepath, 'w', encoding='utf-8') as f:
                json.dump(draft.to_dict(), f, indent=2)
        except Exception as e:
            print(f"[DRAFT] Failed to save draft {draft.thread_id}: {e}")

    def _delete_draft_file(self, thread_id: int):
        """Delete draft file from disk."""
        filepath = os.path.join(self.DRAFTS_DIR, f"{thread_id}.json")
        if os.path.exists(filepath):
            os.remove(filepath)

    def _get_draft(self, ctx) -> Optional[DraftState]:
        """Get draft for current thread."""
        return self.drafts.get(ctx.channel.id)

    def _get_seat_for_user(self, draft: DraftState, user_id: int) -> Optional[DraftSeat]:
        """Find the seat belonging to a Discord user."""
        for seat in draft.seats:
            if seat.discord_user_id == user_id:
                return seat
        return None

    # -------------------------------------------------------------------------
    # !cube — Load a cube list
    # -------------------------------------------------------------------------
    @commands.command(name="cube")
    async def load_cube(self, ctx, *, source: str = ""):
        """
        Load a cube list for drafting.

        Usage:
            !cube <cubecobra_url>        - Load from CubeCobra
            !cube                        - Upload a .txt or .json file as attachment
        """
        cards = None
        cube_name = ""

        # Check for file attachment
        if ctx.message.attachments:
            attachment = ctx.message.attachments[0]
            content = (await attachment.read()).decode('utf-8')

            if attachment.filename.endswith('.json'):
                json_data = json.loads(content)
                await ctx.send(f"Loading cube from JSON file `{attachment.filename}`...")
                cards, cube_name = await self.cube_loader.load_from_json(json_data)
            else:
                await ctx.send(f"Loading cube from text file `{attachment.filename}`...")
                cards, cube_name = await self.cube_loader.load_from_text(content)

        # Check for CubeCobra URL
        elif source:
            cc_match = re.search(r'cubecobra\.com/cube/(?:list|overview|playtest)/([^\s/]+)', source)
            if cc_match:
                cube_id = cc_match.group(1)
                await ctx.send(f"Loading cube from CubeCobra: `{cube_id}`...")
                try:
                    cards, cube_name = await self.cube_loader.load_from_cubecobra(cube_id)
                except Exception as e:
                    await ctx.send(f"Failed to load from CubeCobra: {e}\nTry uploading a text file instead.")
                    return
            else:
                # Treat as inline text (for small cubes or testing)
                await ctx.send("Loading cube from text...")
                cards, cube_name = await self.cube_loader.load_from_text(source)
        else:
            await ctx.send(
                "**Usage:** `!cube <cubecobra_url>` or attach a `.txt`/`.json` file.\n"
                "Text format: one card name per line.\n"
                "JSON format: `{\"name\": \"My Cube\", \"cards\": [\"Card A\", \"Card B\", ...]}`"
            )
            return

        if not cards:
            await ctx.send("No cards loaded!")
            return

        # Validate cube size
        min_cards = PACK_SIZE * NUM_ROUNDS * POD_SIZE  # 360
        if len(cards) < min_cards:
            await ctx.send(
                f"Loaded **{len(cards)}** cards, but a full 8-seat draft needs "
                f"**{min_cards}**. Draft will use smaller packs or fewer rounds."
            )

        self.loaded_cubes[ctx.author.id] = (cards, cube_name)
        await ctx.send(
            f"Loaded **{cube_name}** \u2014 {len(cards)} cards.\n"
            f"Start a draft with `!draft claude` (vs AI) or `!draft @opponent` (vs human)."
        )

    # -------------------------------------------------------------------------
    # !draft — Start a draft
    # -------------------------------------------------------------------------
    @commands.command(name="draft")
    async def start_draft(self, ctx, opponent: str = None):
        """
        Start a cube draft.

        Usage:
            !draft claude       - Draft vs Claude AI
            !draft @Player      - Draft vs another human
            !draft solo         - Solo draft (just you picking)
        """
        if not opponent:
            await ctx.send("Usage: `!draft claude`, `!draft @Player`, or `!draft solo`")
            return

        # Check cube is loaded
        if ctx.author.id not in self.loaded_cubes:
            await ctx.send("Load a cube first with `!cube`!")
            return

        cube_cards, cube_name = self.loaded_cubes[ctx.author.id]

        # Determine draft mode
        is_claude = opponent.lower() in ["claude", "bot", "ai"]
        is_solo = opponent.lower() == "solo"
        opponent_user = None

        if not is_claude and not is_solo:
            if ctx.message.mentions:
                opponent_user = ctx.message.mentions[0]
            else:
                await ctx.send("Please mention your opponent (e.g., `!draft @Player`) or use `claude`/`solo`.")
                return

        # Create draft thread
        thread = await ctx.channel.create_thread(
            name=f"Draft: {cube_name}",
            type=discord.ChannelType.public_thread,
        )

        # Build 8 seats
        seats = []
        available_bot_names = random.sample(BOT_NAMES, min(len(BOT_NAMES), POD_SIZE))
        bot_name_idx = 0

        # Randomly assign human/Claude seats
        human_positions = random.sample(range(POD_SIZE), 2 if (is_claude or opponent_user) else 1)
        p1_seat_idx = human_positions[0]
        p2_seat_idx = human_positions[1] if len(human_positions) > 1 else None

        human_seat_indices = [p1_seat_idx]
        claude_seat_idx = None

        for i in range(POD_SIZE):
            if i == p1_seat_idx:
                seats.append(DraftSeat(
                    seat_index=i,
                    name=ctx.author.display_name,
                    is_human=True,
                    discord_user_id=ctx.author.id,
                ))
            elif i == p2_seat_idx:
                if is_claude:
                    seats.append(DraftSeat(
                        seat_index=i,
                        name="Claude",
                        is_claude=True,
                    ))
                    claude_seat_idx = i
                elif opponent_user:
                    seats.append(DraftSeat(
                        seat_index=i,
                        name=opponent_user.display_name,
                        is_human=True,
                        discord_user_id=opponent_user.id,
                    ))
                    human_seat_indices.append(i)
                else:
                    # Solo mode — fill with bot
                    seats.append(DraftSeat(
                        seat_index=i,
                        name=available_bot_names[bot_name_idx],
                    ))
                    bot_name_idx += 1
            else:
                seats.append(DraftSeat(
                    seat_index=i,
                    name=available_bot_names[bot_name_idx % len(available_bot_names)],
                ))
                bot_name_idx += 1

        # Shuffle and deal packs
        pool = list(cube_cards)  # Copy the card list
        # Give each card a fresh unique ID for this draft
        for card in pool:
            card.id = f"{card.name}_{random.randint(10000, 99999)}"
        random.shuffle(pool)

        # Calculate actual pack size based on available cards
        total_needed = PACK_SIZE * NUM_ROUNDS * POD_SIZE
        actual_pack_size = PACK_SIZE
        actual_rounds = NUM_ROUNDS
        if len(pool) < total_needed:
            # Reduce pack size or rounds to fit
            actual_pack_size = len(pool) // (NUM_ROUNDS * POD_SIZE)
            if actual_pack_size < 5:
                actual_rounds = max(1, len(pool) // (5 * POD_SIZE))
                actual_pack_size = len(pool) // (actual_rounds * POD_SIZE)

        # Deal all packs upfront
        all_packs = {}
        card_idx = 0
        for round_num in range(1, actual_rounds + 1):
            all_packs[round_num] = {}
            for seat_idx in range(POD_SIZE):
                pack = pool[card_idx:card_idx + actual_pack_size]
                all_packs[round_num][seat_idx] = pack
                card_idx += actual_pack_size

        # Set up first round packs
        current_packs = {i: list(all_packs[1][i]) for i in range(POD_SIZE)}

        draft = DraftState(
            thread_id=thread.id,
            phase="picking",
            cube_name=cube_name,
            seats=seats,
            human_seats=human_seat_indices,
            claude_seat=claude_seat_idx,
            pack_round=1,
            pick_number=1,
            pass_direction=1,  # Round 1: pass left
            packs=current_packs,
            all_packs=all_packs,
            pack_size=actual_pack_size,
            num_rounds=actual_rounds,
            created_at=datetime.now().isoformat(),
        )

        self.drafts[thread.id] = draft
        self._save_draft(draft)

        # Announce in thread
        seat_list = "\n".join(
            f"  Seat {s.seat_index + 1}: {'**' + s.name + '**' if s.is_human or s.is_claude else s.name}"
            f"{'  ' if not (s.is_human or s.is_claude) else ''}"
            for s in seats
        )
        await thread.send(
            f"**Draft: {cube_name}**\n"
            f"{actual_rounds} rounds of {actual_pack_size}-card packs \u2014 {POD_SIZE}-seat pod\n\n"
            f"{seat_list}\n\n"
            f"Starting Pack 1, Pick 1! Check your DMs."
        )

        # Process first pick cycle (bots + Claude pick, then DM humans)
        await self._process_non_human_picks(draft, thread)
        await self._dm_packs_to_humans(draft, thread)

    # -------------------------------------------------------------------------
    # Internal: Process bot + Claude picks
    # -------------------------------------------------------------------------
    async def _process_non_human_picks(self, draft: DraftState, channel):
        """Have all bots and Claude make their picks for the current pack."""
        for seat in draft.seats:
            if seat.is_human or seat.has_picked:
                continue

            pack = draft.packs.get(seat.seat_index, [])
            if not pack:
                seat.has_picked = True
                continue

            if seat.is_claude:
                # Claude picks via API
                usage_cb = getattr(self.bot, 'track_mtg_usage', None)
                await claude_make_pick(
                    self.bot.claude,
                    seat, pack,
                    draft.pack_round, draft.pick_number,
                    usage_callback=usage_cb,
                )
            else:
                # Heuristic bot
                bot_make_pick(seat, pack, draft.pick_number)

    async def _dm_packs_to_humans(self, draft: DraftState, channel):
        """DM current pack to each human player."""
        for seat_idx in draft.human_seats:
            seat = draft.seats[seat_idx]
            if seat.has_picked:
                continue

            pack = draft.packs.get(seat.seat_index, [])
            if not pack:
                seat.has_picked = True
                continue

            # Build color preferences from pool for display
            _update_bot_color_prefs(seat)

            msg = format_pack_display(
                pack, draft.pack_round, draft.pick_number,
                len(seat.pool), seat.color_preferences,
            )

            user = self.bot.get_user(seat.discord_user_id)
            if not user:
                try:
                    user = await self.bot.fetch_user(seat.discord_user_id)
                except Exception:
                    await channel.send(f"Could not find user for seat {seat_idx}!")
                    continue

            try:
                await user.send(msg)
            except discord.Forbidden:
                await channel.send(
                    f"{seat.name}: I can't DM you! Please enable DMs from server members."
                )

    # -------------------------------------------------------------------------
    # Internal: Advance draft after all picks are in
    # -------------------------------------------------------------------------
    async def _advance_draft(self, draft: DraftState, channel):
        """After all seats have picked, pass packs and start next pick cycle."""
        # Reset pick flags
        for seat in draft.seats:
            seat.has_picked = False

        # Pass packs in current direction
        new_packs = {}
        for seat_idx, pack in draft.packs.items():
            next_seat = (seat_idx + draft.pass_direction) % POD_SIZE
            new_packs[next_seat] = pack
        draft.packs = new_packs

        draft.pick_number += 1

        # Check if round is over (all cards picked from packs)
        packs_empty = all(len(p) == 0 for p in draft.packs.values())
        if packs_empty or draft.pick_number > draft.pack_size:
            # Next round
            draft.pack_round += 1

            if draft.pack_round > draft.num_rounds:
                # Draft complete!
                draft.phase = "deck_building"
                self._save_draft(draft)

                await channel.send(
                    f"**Draft complete!** \U0001F389\n"
                    f"Each player drafted {len(draft.seats[0].pool)} cards.\n"
                    f"Check your DMs for deck building. Use `!build`, `!addcard`, `!cut`, `!autoland`, `!finalize`."
                )

                # Auto-build Claude's deck
                if draft.claude_seat is not None:
                    claude_seat = draft.seats[draft.claude_seat]
                    deck, sideboard = auto_build_deck(claude_seat.pool)
                    claude_seat.deck = deck
                    claude_seat.sideboard = sideboard
                    claude_seat.deck_finalized = True
                    print(f"[DRAFT-CLAUDE] Auto-built deck: {len(deck)} cards")

                # DM each human their pool + auto-suggestion
                for seat_idx in draft.human_seats:
                    seat = draft.seats[seat_idx]
                    # Auto-suggest a deck
                    suggested_deck, suggested_sb = auto_build_deck(seat.pool)
                    seat.deck = suggested_deck
                    seat.sideboard = suggested_sb

                    user = self.bot.get_user(seat.discord_user_id)
                    if not user:
                        try:
                            user = await self.bot.fetch_user(seat.discord_user_id)
                        except Exception:
                            continue

                    try:
                        pool_msg = format_pool_display(seat.pool)
                        await user.send(pool_msg)
                        await user.send(
                            "I've auto-suggested a deck for you! Use `!build` in the draft thread to see it.\n"
                            "Commands: `!addcard <name>`, `!cut <name>`, `!autoland`, `!finalize`"
                        )
                    except discord.Forbidden:
                        await channel.send(f"{seat.name}: Enable DMs to see your pool!")

                self._save_draft(draft)
                return

            # Set up next round
            draft.pick_number = 1
            # Alternate pass direction: round 1 left, round 2 right, round 3 left
            draft.pass_direction = 1 if draft.pack_round % 2 == 1 else -1

            # Deal next round packs
            if draft.pack_round in draft.all_packs:
                draft.packs = {i: list(draft.all_packs[draft.pack_round][i]) for i in range(POD_SIZE)}
            else:
                # Shouldn't happen, but safety
                draft.packs = {i: [] for i in range(POD_SIZE)}

            await channel.send(
                f"**Pack {draft.pack_round}** \u2014 "
                f"{'Passing left \u2b05\ufe0f' if draft.pass_direction == 1 else 'Passing right \u27a1\ufe0f'}"
            )

        self._save_draft(draft)

        # Process next pick cycle
        await self._process_non_human_picks(draft, channel)
        await self._dm_packs_to_humans(draft, channel)

    # -------------------------------------------------------------------------
    # !pick — Pick a card from current pack
    # -------------------------------------------------------------------------
    @commands.command(name="pick")
    async def pick_card(self, ctx, card_index: int):
        """
        Pick a card from your current pack.

        Usage:
            !pick 3    - Pick card at index 3
        """
        draft = self._get_draft(ctx)
        if not draft:
            await ctx.send("No active draft in this thread!")
            return

        if draft.phase != "picking":
            await ctx.send(f"Draft is in **{draft.phase}** phase, not picking!")
            return

        seat = self._get_seat_for_user(draft, ctx.author.id)
        if not seat:
            await ctx.send("You're not in this draft!")
            return

        if seat.has_picked:
            await ctx.send("You've already picked from this pack! Waiting for other players...")
            return

        pack = draft.packs.get(seat.seat_index, [])
        if not pack:
            await ctx.send("Your current pack is empty!")
            return

        if card_index < 0 or card_index >= len(pack):
            await ctx.send(f"Invalid pick! Choose 0\u2013{len(pack) - 1}.")
            return

        # Make the pick
        chosen = pack[card_index]
        seat.pool.append(chosen)
        pack.remove(chosen)
        seat.has_picked = True
        _update_bot_color_prefs(seat)

        await ctx.send(f"\u2705 {seat.name} picked **{chosen.name}** (Pack {draft.pack_round}, Pick {draft.pick_number})")

        # Check if all seats have picked
        all_picked = all(s.has_picked for s in draft.seats)
        if all_picked:
            await self._advance_draft(draft, ctx.channel)
        else:
            # Show who we're waiting for
            waiting = [s.name for s in draft.seats if not s.has_picked and s.is_human]
            if waiting:
                await ctx.send(f"Waiting for: {', '.join(waiting)}")
            self._save_draft(draft)

    # -------------------------------------------------------------------------
    # !pack — Re-show current pack
    # -------------------------------------------------------------------------
    @commands.command(name="pack")
    async def show_pack(self, ctx):
        """Re-show your current pack (DM)."""
        draft = self._get_draft(ctx)
        if not draft or draft.phase != "picking":
            await ctx.send("No active draft picking phase!")
            return

        seat = self._get_seat_for_user(draft, ctx.author.id)
        if not seat:
            await ctx.send("You're not in this draft!")
            return

        if seat.has_picked:
            await ctx.send("You've already picked. Waiting for others...")
            return

        pack = draft.packs.get(seat.seat_index, [])
        _update_bot_color_prefs(seat)
        msg = format_pack_display(pack, draft.pack_round, draft.pick_number,
                                  len(seat.pool), seat.color_preferences)
        try:
            await ctx.author.send(msg)
            await ctx.send("Check your DMs!")
        except discord.Forbidden:
            await ctx.send("I can't DM you! Enable DMs from server members.")

    # -------------------------------------------------------------------------
    # !pool — Show drafted pool
    # -------------------------------------------------------------------------
    @commands.command(name="pool")
    async def show_pool(self, ctx):
        """View your drafted card pool (DM)."""
        draft = self._get_draft(ctx)
        if not draft:
            await ctx.send("No active draft in this thread!")
            return

        seat = self._get_seat_for_user(draft, ctx.author.id)
        if not seat:
            await ctx.send("You're not in this draft!")
            return

        msg = format_pool_display(seat.pool)
        try:
            await ctx.author.send(msg)
            await ctx.send("Check your DMs!")
        except discord.Forbidden:
            # Fall back to thread message (not ideal for secrecy, but functional)
            await ctx.send(msg)

    # -------------------------------------------------------------------------
    # !build — Show deck building interface
    # -------------------------------------------------------------------------
    @commands.command(name="build")
    async def show_build(self, ctx):
        """Show your deck building interface."""
        draft = self._get_draft(ctx)
        if not draft:
            await ctx.send("No active draft in this thread!")
            return

        if draft.phase not in ("deck_building", "ready"):
            await ctx.send(f"Draft is in **{draft.phase}** phase, not deck building!")
            return

        seat = self._get_seat_for_user(draft, ctx.author.id)
        if not seat:
            await ctx.send("You're not in this draft!")
            return

        msg = format_deck_building_display(seat)
        try:
            await ctx.author.send(msg)
            await ctx.send("Check your DMs!")
        except discord.Forbidden:
            await ctx.send(msg)

    # -------------------------------------------------------------------------
    # !addcard — Add card from sideboard to deck
    # -------------------------------------------------------------------------
    @commands.command(name="addcard")
    async def add_card(self, ctx, *, card_name: str):
        """Add a card from your sideboard to your deck."""
        draft = self._get_draft(ctx)
        if not draft or draft.phase not in ("deck_building", "ready"):
            await ctx.send("Not in deck building phase!")
            return

        seat = self._get_seat_for_user(draft, ctx.author.id)
        if not seat:
            await ctx.send("You're not in this draft!")
            return

        if seat.deck_finalized:
            await ctx.send("Your deck is already finalized! Use `!unfinalize` to reopen.")
            return

        # Find card in sideboard (fuzzy match)
        card_lower = card_name.lower()
        found = None
        for card in seat.sideboard:
            if card.name.lower() == card_lower:
                found = card
                break
        if not found:
            # Fuzzy search
            matches = [c for c in seat.sideboard if card_lower in c.name.lower()]
            if len(matches) == 1:
                found = matches[0]
            elif len(matches) > 1:
                names = ", ".join(m.name for m in matches[:5])
                await ctx.send(f"Multiple matches: {names}. Be more specific!")
                return
            else:
                await ctx.send(f"'{card_name}' not found in your sideboard!")
                return

        seat.sideboard.remove(found)
        seat.deck.append(found)
        self._save_draft(draft)
        await ctx.send(f"\u2705 Added **{found.name}** to deck ({len(seat.deck)} cards)")

    # -------------------------------------------------------------------------
    # !cut — Remove card from deck to sideboard
    # -------------------------------------------------------------------------
    @commands.command(name="cut")
    async def cut_card(self, ctx, *, card_name: str):
        """Remove a card from your deck back to sideboard."""
        draft = self._get_draft(ctx)
        if not draft or draft.phase not in ("deck_building", "ready"):
            await ctx.send("Not in deck building phase!")
            return

        seat = self._get_seat_for_user(draft, ctx.author.id)
        if not seat:
            await ctx.send("You're not in this draft!")
            return

        if seat.deck_finalized:
            await ctx.send("Your deck is already finalized! Use `!unfinalize` to reopen.")
            return

        card_lower = card_name.lower()
        found = None
        for card in seat.deck:
            if card.name.lower() == card_lower:
                found = card
                break
        if not found:
            matches = [c for c in seat.deck if card_lower in c.name.lower()]
            if len(matches) == 1:
                found = matches[0]
            elif len(matches) > 1:
                names = ", ".join(m.name for m in matches[:5])
                await ctx.send(f"Multiple matches: {names}. Be more specific!")
                return
            else:
                await ctx.send(f"'{card_name}' not found in your deck!")
                return

        seat.deck.remove(found)
        # Only add back to sideboard if it's a drafted card (not a basic land)
        if found.type_line and 'basic land' in found.type_line.lower():
            pass  # Discard basic lands
        else:
            seat.sideboard.append(found)
        self._save_draft(draft)
        await ctx.send(f"\u2702\ufe0f Cut **{found.name}** from deck ({len(seat.deck)} cards)")

    # -------------------------------------------------------------------------
    # !autoland — Auto-fill basic lands
    # -------------------------------------------------------------------------
    @commands.command(name="autoland")
    async def auto_land(self, ctx, *, counts: str = ""):
        """
        Auto-fill basic lands to reach 40 cards.

        Usage:
            !autoland           - Auto-distribute based on deck colors
            !autoland 8 0 0 6 3 - Specify counts (W U B R G)
        """
        draft = self._get_draft(ctx)
        if not draft or draft.phase not in ("deck_building", "ready"):
            await ctx.send("Not in deck building phase!")
            return

        seat = self._get_seat_for_user(draft, ctx.author.id)
        if not seat:
            await ctx.send("You're not in this draft!")
            return

        if seat.deck_finalized:
            await ctx.send("Your deck is already finalized!")
            return

        # Remove existing basic lands from deck first
        seat.deck = [c for c in seat.deck if not (c.type_line and 'basic land' in c.type_line.lower())]

        basics_needed = max(0, draft.deck_size - len(seat.deck))
        if basics_needed == 0:
            await ctx.send(f"Your deck already has {len(seat.deck)} cards \u2014 no basics needed!")
            self._save_draft(draft)
            return

        basic_names = {'W': 'Plains', 'U': 'Island', 'B': 'Swamp', 'R': 'Mountain', 'G': 'Forest'}

        if counts.strip():
            # Parse explicit counts: "8 0 0 6 3" = 8 Plains, 0 Islands, 0 Swamps, 6 Mountains, 3 Forests
            parts = counts.strip().split()
            if len(parts) != 5:
                await ctx.send("Provide 5 numbers for W U B R G (e.g., `!autoland 8 0 0 6 3`)")
                return
            try:
                land_counts = {COLORS[i]: int(parts[i]) for i in range(5)}
            except ValueError:
                await ctx.send("Invalid numbers! Use e.g., `!autoland 8 0 0 6 3`")
                return
        else:
            # Auto-distribute based on colored mana symbols in deck
            color_symbols = {c: 0 for c in COLORS}
            for card in seat.deck:
                for c in _get_card_colors(card):
                    color_symbols[c] += 1

            total = sum(color_symbols.values()) or 1
            land_counts = {}
            assigned = 0
            sorted_colors = sorted(color_symbols.items(), key=lambda x: x[1], reverse=True)
            for c, sym_count in sorted_colors:
                if sym_count > 0:
                    n = max(1, round(basics_needed * sym_count / total))
                    land_counts[c] = n
                    assigned += n
                else:
                    land_counts[c] = 0

            # Adjust to hit exact count
            diff = basics_needed - assigned
            if diff != 0 and sorted_colors:
                top_color = sorted_colors[0][0]
                land_counts[top_color] = max(0, land_counts.get(top_color, 0) + diff)

        # Add the basics
        added = []
        for c in COLORS:
            n = land_counts.get(c, 0)
            if n > 0:
                for _ in range(n):
                    seat.deck.append(Card(name=basic_names[c], type_line="Basic Land"))
                added.append(f"{n} {basic_names[c]}")

        self._save_draft(draft)
        added_str = ", ".join(added) if added else "no lands"
        await ctx.send(f"\U0001F3D4\ufe0f Added {added_str} \u2014 deck is now {len(seat.deck)} cards.")

    # -------------------------------------------------------------------------
    # !finalize — Lock in deck
    # -------------------------------------------------------------------------
    @commands.command(name="finalize")
    async def finalize_deck(self, ctx):
        """Lock in your deck and mark as ready to play."""
        draft = self._get_draft(ctx)
        if not draft or draft.phase not in ("deck_building", "ready"):
            await ctx.send("Not in deck building phase!")
            return

        seat = self._get_seat_for_user(draft, ctx.author.id)
        if not seat:
            await ctx.send("You're not in this draft!")
            return

        if len(seat.deck) < draft.deck_size:
            needed = draft.deck_size - len(seat.deck)
            await ctx.send(
                f"Your deck has only {len(seat.deck)} cards (need {draft.deck_size}). "
                f"Add {needed} more cards or use `!autoland` to fill with basics!"
            )
            return

        seat.deck_finalized = True
        self._save_draft(draft)
        await ctx.send(f"\u2705 **{seat.name}** has finalized their {len(seat.deck)}-card deck!")

        # Check if all players (humans + Claude) are ready
        player_seats = [draft.seats[i] for i in draft.human_seats]
        if draft.claude_seat is not None:
            player_seats.append(draft.seats[draft.claude_seat])

        all_ready = all(s.deck_finalized for s in player_seats)
        if all_ready:
            draft.phase = "ready"
            self._save_draft(draft)
            await ctx.send(
                "**All players are ready!** \U0001F3AE\n"
                "Start the game with `!draftgame`."
            )

    # -------------------------------------------------------------------------
    # !unfinalize — Reopen deck for editing
    # -------------------------------------------------------------------------
    @commands.command(name="unfinalize")
    async def unfinalize_deck(self, ctx):
        """Reopen your deck for editing."""
        draft = self._get_draft(ctx)
        if not draft or draft.phase not in ("deck_building", "ready"):
            await ctx.send("Not in deck building phase!")
            return

        seat = self._get_seat_for_user(draft, ctx.author.id)
        if not seat:
            await ctx.send("You're not in this draft!")
            return

        seat.deck_finalized = False
        draft.phase = "deck_building"
        self._save_draft(draft)
        await ctx.send(f"\U0001F513 **{seat.name}** reopened their deck for editing.")

    # -------------------------------------------------------------------------
    # !draftgame — Start the post-draft game
    # -------------------------------------------------------------------------
    @commands.command(name="draftgame")
    async def start_draft_game(self, ctx):
        """Start a game with your drafted decks."""
        draft = self._get_draft(ctx)
        if not draft:
            await ctx.send("No active draft in this thread!")
            return

        if draft.phase not in ("ready", "deck_building"):
            await ctx.send(f"Draft is in **{draft.phase}** phase. Everyone must `!finalize` first!")
            return

        # Check all players are finalized
        player_seats = [draft.seats[i] for i in draft.human_seats]
        if draft.claude_seat is not None:
            player_seats.append(draft.seats[draft.claude_seat])

        not_ready = [s.name for s in player_seats if not s.deck_finalized]
        if not_ready:
            await ctx.send(f"Waiting for: {', '.join(not_ready)} to `!finalize`!")
            return

        if not self.engine:
            game_cog = self.bot.get_cog("MTG Game")
            if game_cog:
                self.engine = game_cog.engine
            else:
                await ctx.send("Game engine not available!")
                return

        # Determine the two players
        # Player 1: the human who started the draft
        p1_seat = draft.seats[draft.human_seats[0]]

        # Player 2: Claude, second human, or first bot
        if draft.claude_seat is not None:
            p2_seat = draft.seats[draft.claude_seat]
        elif len(draft.human_seats) > 1:
            p2_seat = draft.seats[draft.human_seats[1]]
        else:
            # Solo mode: play against a bot's drafted deck
            bot_seats = [s for s in draft.seats if not s.is_human and not s.is_claude]
            p2_seat = bot_seats[0] if bot_seats else None
            if not p2_seat:
                await ctx.send("No opponent to play against!")
                return

        # Create game from drafted cards
        game = await self.engine.create_game_from_cards(
            thread_id=ctx.channel.id,
            player1_name=p1_seat.name,
            player1_id=p1_seat.discord_user_id,
            player1_cards=list(p1_seat.deck),
            player2_name=p2_seat.name,
            player2_id=p2_seat.discord_user_id if p2_seat.is_human else None,
            player2_cards=list(p2_seat.deck),
            format_name="limited",
        )

        # Start the game
        import random as rng
        first_player = rng.randint(0, 1)
        self.engine.start_game(game, first_player)

        # Handle Claude mulligans if applicable
        claude_player = None
        for p in game.players:
            if p.is_claude:
                claude_player = p
                break

        if claude_player:
            claude_mulligans = 0
            while claude_mulligans < 2:
                should_mull = await self.engine.claude_ai.decide_mulligan(
                    claude_player.hand, claude_mulligans
                )
                if not should_mull:
                    break
                claude_mulligans += 1
                claude_player.library.extend(claude_player.hand)
                claude_player.hand.clear()
                random.shuffle(claude_player.library)
                self.engine.draw_cards(claude_player, 7)

            if claude_mulligans > 0:
                claude_player.hand.sort(key=lambda c: c.cmc, reverse=True)
                for _ in range(claude_mulligans):
                    if claude_player.hand:
                        card = claude_player.hand.pop(0)
                        claude_player.library.append(card)
                random.shuffle(claude_player.library)

        # Clean up draft state
        draft.phase = "game_started"
        self._save_draft(draft)
        self._delete_draft_file(draft.thread_id)
        if draft.thread_id in self.drafts:
            del self.drafts[draft.thread_id]

        # Show game start
        first_name = game.players[first_player].name
        await ctx.send(
            f"**Game Start!** \u2694\ufe0f Limited format \u2014 {draft.starting_life} life\n"
            f"**{game.players[0].name}** ({len(game.players[0].hand)} cards) vs "
            f"**{game.players[1].name}** ({len(game.players[1].hand)} cards)\n"
            f"{first_name} goes first!\n\n"
            f"Use `!state` to see the board, `!play <card>` to start playing."
        )

        self.engine.save_game(game)

    # -------------------------------------------------------------------------
    # !draftstatus — Show draft progress
    # -------------------------------------------------------------------------
    @commands.command(name="draftstatus")
    async def draft_status(self, ctx):
        """Show current draft progress."""
        draft = self._get_draft(ctx)
        if not draft:
            await ctx.send("No active draft in this thread!")
            return

        if draft.phase == "picking":
            picked = [s.name for s in draft.seats if s.has_picked and (s.is_human or s.is_claude)]
            waiting = [s.name for s in draft.seats if not s.has_picked and s.is_human]
            total_picked = len(draft.seats[0].pool) if draft.seats else 0

            msg = (
                f"**Draft Status** \u2014 Pack {draft.pack_round}, Pick {draft.pick_number}\n"
                f"Cards drafted: {total_picked}/{draft.pack_size * draft.num_rounds}\n"
                f"{'Passing left' if draft.pass_direction == 1 else 'Passing right'}\n"
            )
            if waiting:
                msg += f"Waiting for: {', '.join(waiting)}"
            else:
                msg += "All picks in!"
            await ctx.send(msg)

        elif draft.phase in ("deck_building", "ready"):
            lines = ["**Deck Building Status**\n"]
            for seat_idx in draft.human_seats:
                seat = draft.seats[seat_idx]
                status = "\u2705 Finalized" if seat.deck_finalized else f"\U0001F527 Building ({len(seat.deck)}/{draft.deck_size})"
                lines.append(f"  {seat.name}: {status}")
            if draft.claude_seat is not None:
                claude_seat = draft.seats[draft.claude_seat]
                status = "\u2705 Finalized" if claude_seat.deck_finalized else "\U0001F527 Building..."
                lines.append(f"  Claude: {status}")
            await ctx.send("\n".join(lines))

        else:
            await ctx.send(f"Draft phase: **{draft.phase}**")

    # =========================================================================
    # AUTO-DRAFT — Fully automated draft + game for testing
    # =========================================================================

    async def _autodraft_send(self, thread, content=None, embed=None):
        """Send a message to the autodraft thread.

        June 10 audit: delegate to the cog's _autoplay_send so autodraft
        messages ride the SAME pipeline as regular autoplay — per-game
        discord-LOG mirroring, dangling-article sanitizers, and all three
        burst-dedup layers. The June 10 cube game's discord log was 31
        lines because everything sent from here bypassed the logger; only
        the combat / Rick-turn lines (already routed through
        _autoplay_send) were recorded, making the game unauditable.
        Falls back to a bare send when the cog isn't wired (standalone
        draft tests).
        """
        cog = getattr(self, 'game_cog', None)
        if cog is not None and hasattr(cog, '_autoplay_send'):
            await cog._autoplay_send(thread, content=content, embed=embed)
            return
        try:
            if content and embed:
                await thread.send(content, embed=embed)
            elif content:
                await thread.send(content)
            elif embed:
                await thread.send(embed=embed)
            await asyncio.sleep(0.5)
        except Exception as e:
            print(f"[AUTO-DRAFT] Discord send error: {e}")

    def _format_deck_summary(self, player_name: str, deck: list) -> str:
        """Format a compact deck summary for thread posting."""
        creatures = [c for c in deck if 'creature' in (c.type_line or '').lower()]
        spells = [c for c in deck if 'creature' not in (c.type_line or '').lower()
                  and 'land' not in (c.type_line or '').lower()]
        lands = [c for c in deck if 'land' in (c.type_line or '').lower()]

        # Show top nonland picks by CMC
        top_picks = sorted(
            [c for c in deck if 'land' not in (c.type_line or '').lower()],
            key=lambda c: c.cmc or 0, reverse=True
        )[:5]

        return (
            f"**{player_name}'s Deck** ({len(deck)} cards)\n"
            f"Creatures: {len(creatures)} | Spells: {len(spells)} | Lands: {len(lands)}\n"
            f"Top picks: {', '.join(c.name for c in top_picks)}"
        )

    async def _load_test_cube(self, cube_source: str) -> Tuple[List[Card], str]:
        """Load a cube for autodraft. Returns (cards, cube_name)."""
        if cube_source in ("test", "360"):
            cube_path = os.path.join(os.path.dirname(__file__), "data", "test_cube_360.json")
        else:
            cube_path = os.path.join(os.path.dirname(__file__), "data", f"test_cube_{cube_source}.json")

        if not os.path.exists(cube_path):
            raise ValueError(f"Cube file not found: {cube_path}")

        with open(cube_path, 'r', encoding='utf-8') as f:
            cube_data = json.load(f)

        return await self.cube_loader.load_from_json(cube_data)

    @commands.command(name="autodraft")
    async def autodraft(self, ctx, cube_source: str = "test"):
        """
        Run a fully automated cube draft + post-draft game.
        Exercises the full pipeline: cube load → 8-seat draft → deck build → limited game.

        Usage:
            !autodraft              - Use default test cube (360 cards)
            !autodraft <source>     - Use data/test_cube_<source>.json
        """
        if not self.engine or not self.game_cog:
            # Try to grab references if cog_load ran before MTGGameCog
            game_cog = self.bot.get_cog("MTG Game")
            if game_cog:
                self.engine = game_cog.engine
                self.game_cog = game_cog
                self.cube_loader.deck_loader = self.engine.deck_loader
            else:
                await ctx.send("❌ Game engine not available! Make sure the MTG Game cog is loaded.")
                return

        await ctx.send(f"🎲 Starting automated draft (cube: {cube_source})...")
        result = await self._run_autodraft(ctx.channel, cube_source)

        # Post final summary back in original channel
        icon = {"win_p1": "🏆", "win_p2": "🏆", "timeout": "⏱️",
                "crash": "❌", "circuit_breaker": "⚡"}.get(result["outcome"], "❓")
        await ctx.send(
            f"{icon} **Auto-draft complete!** {result.get('winner', 'No winner')} | "
            f"{result['turns']}t | {result['duration_seconds']:.0f}s | "
            f"Outcome: {result['outcome']}"
        )

    async def _run_autodraft(self, channel, cube_source: str = "test") -> dict:
        """Run a full automated cube draft + post-draft game.

        Creates its own thread, runs draft picks, builds decks, plays the game.
        Returns a result dict compatible with the autoplay batch format.
        """
        start_time = _time.time()
        max_turns = 60
        thread = None
        result = {
            "format": "draft", "deck1": "Rick (drafted)", "deck2": "Claude (drafted)",
            "outcome": "crash", "winner": None, "turns": 0,
            "p1_life": 0, "p2_life": 0, "error": None, "thread_id": None,
            "duration_seconds": 0,
            "draft_rounds": 0, "draft_picks": 0,
        }

        try:
            # ================================================================
            # PHASE 1: Load cube
            # ================================================================
            print(f"[AUTO-DRAFT] Loading cube: {cube_source}")
            try:
                cube_cards, cube_name = await self._load_test_cube(cube_source)
            except Exception as e:
                result["error"] = f"Failed to load cube: {e}"
                print(f"[AUTO-DRAFT] {result['error']}")
                return result

            min_cards = PACK_SIZE * NUM_ROUNDS * POD_SIZE  # 360
            if len(cube_cards) < min_cards:
                result["error"] = f"Cube too small: {len(cube_cards)} cards (need {min_cards})"
                print(f"[AUTO-DRAFT] {result['error']}")
                return result

            print(f"[AUTO-DRAFT] Loaded {len(cube_cards)} cards from '{cube_name}'")

            # ================================================================
            # PHASE 2: Create Discord thread
            # ================================================================
            thread = await channel.create_thread(
                name=f"Auto-Draft: {cube_name}",
                type=discord.ChannelType.public_thread,
            )
            result["thread_id"] = thread.id

            # ================================================================
            # PHASE 3: Build 8-seat pod
            # ================================================================
            # Rick Deckard = fake human (heuristic picks, human game code paths)
            # Claude = AI picks via API
            # Seats 2-7 = filler bots (heuristic picks)
            seats = []
            bot_names = random.sample(BOT_NAMES, min(len(BOT_NAMES), 6))

            seats.append(DraftSeat(
                seat_index=0, name="Rick Deckard",
                is_human=False, is_claude=False,
                discord_user_id=99999,
            ))
            seats.append(DraftSeat(
                seat_index=1, name="Claude",
                is_claude=True,
            ))
            for i in range(2, POD_SIZE):
                seats.append(DraftSeat(
                    seat_index=i, name=bot_names[i - 2],
                ))

            # Shuffle and assign unique IDs to cube cards
            pool = list(cube_cards)
            for card in pool:
                card.id = f"{card.name}_{random.randint(10000, 99999)}"
            random.shuffle(pool)

            # Deal all packs upfront
            all_packs = {}
            card_idx = 0
            for round_num in range(1, NUM_ROUNDS + 1):
                all_packs[round_num] = {}
                for seat_idx in range(POD_SIZE):
                    pack = pool[card_idx:card_idx + PACK_SIZE]
                    all_packs[round_num][seat_idx] = pack
                    card_idx += PACK_SIZE

            seat_list = "\n".join(
                f"  Seat {s.seat_index + 1}: **{s.name}**"
                + (" 🤖" if s.is_claude else " 🎲" if s.discord_user_id == 99999 else "")
                for s in seats
            )
            await self._autodraft_send(thread,
                f"**Auto-Draft: {cube_name}**\n"
                f"{NUM_ROUNDS} rounds of {PACK_SIZE}-card packs — {POD_SIZE}-seat pod\n\n"
                f"{seat_list}\n\n"
                f"Rick Deckard 🎲 = heuristic picks (human game paths)\n"
                f"Claude 🤖 = AI picks via API")

            # ================================================================
            # PHASE 4: Draft picks (3 rounds × 15 picks)
            # ================================================================
            total_picks = 0
            for round_num in range(1, NUM_ROUNDS + 1):
                # Set up packs for this round
                current_packs = {i: list(all_packs[round_num][i]) for i in range(POD_SIZE)}
                pass_direction = 1 if round_num % 2 == 1 else -1
                direction_str = "Passing left ⬅️" if pass_direction == 1 else "Passing right ➡️"

                await self._autodraft_send(thread,
                    f"\n📦 **Round {round_num}** — {direction_str}")

                for pick_num in range(1, PACK_SIZE + 1):
                    # All 8 seats pick simultaneously
                    for seat in seats:
                        pack = current_packs.get(seat.seat_index, [])
                        if not pack:
                            continue

                        if seat.is_claude:
                            usage_cb = getattr(self.bot, 'track_mtg_usage', None)
                            await claude_make_pick(
                                self.bot.claude, seat, pack,
                                round_num, pick_num,
                                usage_callback=usage_cb,
                            )
                        else:
                            bot_make_pick(seat, pack, pick_num)

                    total_picks += 1

                    # Post pick summary every 5 picks, plus first and last
                    if pick_num % 5 == 0 or pick_num == 1 or pick_num == PACK_SIZE:
                        rick_latest = seats[0].pool[-1].name if seats[0].pool else "?"
                        claude_latest = seats[1].pool[-1].name if seats[1].pool else "?"
                        await self._autodraft_send(thread,
                            f"R{round_num}P{pick_num}: "
                            f"Rick → **{rick_latest}** | Claude → **{claude_latest}**")

                    # Pass packs to next seat
                    new_packs = {}
                    for seat_idx, pack in current_packs.items():
                        next_seat = (seat_idx + pass_direction) % POD_SIZE
                        new_packs[next_seat] = pack
                    current_packs = new_packs

            result["draft_rounds"] = NUM_ROUNDS
            result["draft_picks"] = total_picks

            rick_seat = seats[0]
            claude_seat_obj = seats[1]

            await self._autodraft_send(thread,
                f"\n✅ **Draft complete!** Each player drafted {len(rick_seat.pool)} cards.")

            # ================================================================
            # PHASE 5: Deck building
            # ================================================================
            rick_deck, rick_sb = auto_build_deck(rick_seat.pool)
            rick_seat.deck = rick_deck
            rick_seat.sideboard = rick_sb

            claude_deck, claude_sb = auto_build_deck(claude_seat_obj.pool)
            claude_seat_obj.deck = claude_deck
            claude_seat_obj.sideboard = claude_sb

            await self._autodraft_send(thread, self._format_deck_summary("Rick Deckard", rick_deck))
            await self._autodraft_send(thread, self._format_deck_summary("Claude", claude_deck))

            print(f"[AUTO-DRAFT] Rick's deck: {len(rick_deck)} cards, "
                  f"Claude's deck: {len(claude_deck)} cards")

            # ================================================================
            # PHASE 6: Post-draft game
            # ================================================================
            await self._autodraft_send(thread,
                "\n⚔️ **Starting post-draft game!** (limited format, 20 life)")

            # Create game from drafted cards
            game = await self.engine.create_game_from_cards(
                thread_id=thread.id,
                player1_name="Rick Deckard",
                player1_id=99999,
                player1_cards=list(rick_seat.deck),
                player2_name="Claude",
                player2_id=None,
                player2_cards=list(claude_seat_obj.deck),
                format_name="limited",
            )
            game.is_autoplay = True

            # Set up stack with fast auto-pass
            self.engine.setup_stack(
                game, auto_pass_seconds=0.05,
                send_func=lambda msg: self._autodraft_send(thread, msg),
                ai_response_enabled=True,
            )

            # Set up logging
            game_logger = GameLogger(thread.id)
            if hasattr(self.game_cog, 'game_loggers'):
                self.game_cog.game_loggers[thread.id] = game_logger
            if hasattr(self.game_cog, '_stdout_tee') and self.game_cog._stdout_tee:
                self.game_cog._stdout_tee.add_game(thread.id, game_logger.console_path)
                self.game_cog._stdout_tee.active_thread = thread.id
            print(f"[AUTO-DRAFT] Game logging to {game_logger.console_path}")

            # Start game
            first_player = random.randint(0, 1)
            self.engine.start_game(game, first_player)
            # May 17 audit: emit [GAME-INIT] so audit greps treat autodraft
            # games the same as autoplay games. Without this line, the format
            # tally is missing 1 game per autodraft and audit log-count sanity
            # checks were undercounting the autodraft path entirely.
            try:
                fp_name = game.players[first_player].name
                # June 11: strict= stamp — see the autoplay [GAME-INIT] twin.
                from mtg.util import strict_mode
                print(
                    f"[GAME-INIT] format=cube life=20/20 "
                    f"deck0=draft(40) deck1=draft(40) "
                    f"first_player={fp_name} "
                    f"strict={1 if strict_mode() else 0}"
                )
            except Exception as _init_err:
                print(f"[AUTO-DRAFT] GAME-INIT emit failed: {_init_err}")

            # Mulligan evaluation for both players
            for pi, player in enumerate(game.players):
                mulligans = 0
                max_mulligans = 3
                while mulligans < max_mulligans:
                    should_mull = await self.engine.claude_ai.decide_mulligan(
                        player.hand, mulligans)
                    if not should_mull:
                        break
                    mulligans += 1
                    player.library.extend(player.hand)
                    player.hand.clear()
                    random.shuffle(player.library)
                    self.engine.draw_cards(player, 7)
                if mulligans > 0:
                    # Bottom highest-CMC cards
                    player.hand.sort(key=lambda c: c.cmc, reverse=True)
                    for _ in range(mulligans):
                        if player.hand:
                            bottomed = player.hand.pop(0)
                            player.library.append(bottomed)
                    await self._autodraft_send(thread,
                        f"🔄 {player.name} mulliganed {mulligans}x, keeps {len(player.hand)} cards.")
                player.mulligans_taken = mulligans
                player.has_kept_hand = True

            # Initial state embed
            display = GameDisplay()
            embed = display.create_game_embed(game)
            await self._autodraft_send(thread,
                f"**Game started!** {game.players[first_player].name} goes first.",
                embed=embed)

            # ==============================================================
            # GAME LOOP — reuse autoplay infrastructure via game_cog
            # ==============================================================
            consecutive_zero_action_turns = 0
            try:
                for turn_num in range(max_turns):
                    if game.ended:
                        break

                    # Circuit breaker: if API disabled and nobody's doing anything
                    if self.engine.claude_ai._api_disabled and consecutive_zero_action_turns >= 3:
                        game.ended = True
                        game.winner = None
                        await self._autodraft_send(thread,
                            "⚠️ **[AUTO-DRAFT] Aborting: API disabled and no gameplay for 3 turns.**")
                        result["outcome"] = "circuit_breaker"
                        break

                    # June 10 audit: banner BEFORE the phase advances (same
                    # V31h ordering fix as autoplay), and the upkeep/draw
                    # trigger messages are now SENT instead of discarded —
                    # advance_phase returns (phase, messages) and the old
                    # code dropped the tuple on the floor, so Phyrexian
                    # Arena-class upkeep triggers were invisible in cube
                    # games.
                    await self._autodraft_send(thread,
                        f"🔄 **Turn {game.turn_number}** — **{game.active_player.name}**'s turn")

                    # Advance through untap/upkeep/draw to main phase
                    if game.phase == Phase.UNTAP:
                        _, _p1 = self.engine.advance_phase(game)  # UNTAP → UPKEEP
                        _, _p2 = self.engine.advance_phase(game)  # UPKEEP → DRAW (upkeep triggers)
                        _, _p3 = self.engine.advance_phase(game)  # DRAW → MAIN1
                        for _m in (_p1 + _p2 + _p3):
                            await self._autodraft_send(thread, _m)
                        # Drain sync-queued triggers via Tier 3 (same as autoplay)
                        for _m in await self.engine.drain_pending_triggers(game):
                            await self._autodraft_send(thread, _m)

                    turn_had_actions = False

                    if game.active_player.is_claude:
                        # Claude's turn — use engine directly
                        actions = await self.engine.execute_claude_turn(game)
                        if actions:
                            turn_had_actions = True
                            msg = f"**Claude's turn:**\n" + "\n".join(f"• {a}" for a in actions)
                            if len(msg) > 1900:
                                await self._autodraft_send(thread, "**Claude's turn:**")
                                for a in actions:
                                    await self._autodraft_send(thread, f"• {a[:1900]}")
                            else:
                                await self._autodraft_send(thread, msg)
                        else:
                            await self._autodraft_send(thread, "*Claude thinks, then passes.*")

                        # Resolve any pending actions (ETBs, targets, etc.)
                        await self.game_cog._autoplay_resolve_pending_action(thread, game)

                        # Handle blocks if Claude attacked
                        if game.waiting_for_human_blocks:
                            game.waiting_for_human_blocks = False
                            defender_idx = 1 - game.active_player_index
                            attacker_cards = []
                            for a_id in game.attackers:
                                card_result = game.find_card_global(a_id)
                                if card_result:
                                    attacker_cards.append(card_result[0])
                            blocks = await self.engine.claude_ai.decide_blocks(
                                game, defender_idx, attacker_cards)
                            defender = game.players[defender_idx]
                            if blocks:
                                block_msgs = []
                                for attacker_id, blocker_ids in blocks.items():
                                    if blocker_ids:
                                        atk_result = game.find_card_global(attacker_id)
                                        if not atk_result:
                                            continue
                                        attacker = atk_result[0]
                                        blk_names = []
                                        for blocker_id in blocker_ids:
                                            blk_result = game.find_card_global(blocker_id)
                                            if not blk_result:
                                                continue
                                            blocker = blk_result[0]
                                            blocker.blocking.append(attacker.id)
                                            attacker.blocked_by.append(blocker.id)
                                            if attacker.id not in game.blockers:
                                                game.blockers[attacker.id] = []
                                            game.blockers[attacker.id].append(blocker.id)
                                            blk_names.append(blocker.name)
                                        block_msgs.append(
                                            f"{', '.join(blk_names)} blocks {attacker.name}")
                                if block_msgs:
                                    await self._autodraft_send(thread,
                                        f"🛡️ **{defender.name}** blocks:\n"
                                        + "\n".join(f"• {b}" for b in block_msgs))
                            else:
                                await self._autodraft_send(thread,
                                    f"🛡️ **{defender.name}** doesn't block.")

                            # Post-blocker priority window
                            if game.stack_enabled and game.attackers:
                                send_fn = lambda msg: self._autodraft_send(thread, msg)
                                await self.engine._combat_priority_round(
                                    game, send_fn, "after blockers declared")

                            # Resolve combat damage
                            if game.attackers and not game.ended:
                                await self.game_cog._autoplay_resolve_combat(thread, game)

                            # Post-combat main phase
                            if game.phase == Phase.MAIN2 and not game.ended:
                                post_combat = await self.engine.continue_claude_post_combat(game)
                                if post_combat:
                                    msg = ("**Claude (post-combat):**\n"
                                           + "\n".join(f"• {a}" for a in post_combat))
                                    await self._autodraft_send(thread, msg)
                    else:
                        # Rick's turn — use autoplay human turn (human code paths)
                        actions = await self.game_cog._autoplay_human_turn(
                            thread, game, game.active_player_index)
                        if actions:
                            turn_had_actions = True

                    # Track zero-action turns for circuit breaker
                    if turn_had_actions:
                        consecutive_zero_action_turns = 0
                    else:
                        consecutive_zero_action_turns += 1

                    # Post state embed
                    if not game.ended:
                        await self._autodraft_send(thread,
                            embed=display.create_game_embed(game))

                    if game.ended:
                        break

                    # End turn + discard to hand size
                    end_msgs = self.engine.end_turn(game)
                    for msg in end_msgs:
                        await self._autodraft_send(thread, msg)

                    if (game.pending_action
                            and game.pending_action.get('type') == 'discard_to_hand_size'):
                        pa = game.pending_action
                        pi = pa['player_idx']
                        discard_count = pa['cards_to_discard']
                        discard_player = game.players[pi]
                        for _ in range(discard_count):
                            if not discard_player.hand:
                                break
                            non_lands = [c for c in discard_player.hand if not c.is_land()]
                            if non_lands:
                                worst = max(non_lands, key=lambda c: c.cmc if c.cmc else 0)
                            else:
                                worst = discard_player.hand[-1]
                            discard_player.hand.remove(worst)
                            discard_player.graveyard.append(worst)
                            await self._autodraft_send(thread,
                                f"📤 {discard_player.name} discards {worst.name} to hand size")
                        game.pending_action = None

                    await asyncio.sleep(2)

            except Exception as e:
                import traceback
                traceback.print_exc()
                await self._autodraft_send(thread,
                    f"❌ Auto-draft game error on turn {game.turn_number}: {e}")
                result["error"] = str(e)

            # ================================================================
            # PHASE 7: Results
            # ================================================================
            result["turns"] = game.turn_number
            result["p1_life"] = game.players[0].life
            result["p2_life"] = game.players[1].life

            # May 23 audit (MINOR #29): when the cube-draft game ended on
            # lethal damage (game_1507609083874775110), game.winner stayed
            # None despite game.ended=True and one player at 0 life. The
            # primary path silently dropped the wins announcement. Add a
            # fallback that infers the winner from life totals when winner
            # is None.
            if game.ended and game.winner is None:
                lives = [(i, p.life) for i, p in enumerate(game.players)]
                alive = [(i, l) for i, l in lives if l > 0]
                if len(alive) == 1:
                    print(f"[CUBE-DRAFT] game.ended=True but winner=None; "
                          f"inferring winner={alive[0][0]} from life totals")
                    game.winner = alive[0][0]
            if game.ended and game.winner is not None:
                winner = game.players[game.winner]
                loser = game.players[1 - game.winner]
                result["outcome"] = "win_p1" if game.winner == 0 else "win_p2"
                result["winner"] = winner.name
                await self._autodraft_send(thread,
                    f"🏆 **{winner.name} wins!**\n"
                    f"• Final life: {winner.name} {winner.life} / {loser.name} {loser.life}\n"
                    f"• Turns: {game.turn_number}\n"
                    f"• Draft rounds: {result['draft_rounds']}, picks: {result['draft_picks']}")
            elif result.get("error"):
                result["outcome"] = "crash"
            elif result["outcome"] != "circuit_breaker":
                result["outcome"] = "timeout"
                await self._autodraft_send(thread,
                    f"⏱️ Game ended after {max_turns} turns (no winner)\n"
                    f"• Life: {game.players[0].name} {game.players[0].life} / "
                    f"{game.players[1].name} {game.players[1].life}")
            # (June 10 audit: the stats emits moved to the `finally` block —
            # the June 10 batch's cube game exited via a path that skipped
            # this tail, making it the only 1 of 139 games missing both
            # [STATS-GAME] and [CALL-BREAKDOWN-FINAL].)

        except Exception as e:
            import traceback
            traceback.print_exc()
            result["outcome"] = "crash"
            result["error"] = str(e)
            if thread:
                try:
                    await self._autodraft_send(thread, f"❌ Fatal auto-draft error: {e}")
                except Exception:
                    pass
        finally:
            # June 10 audit: emit stats from `finally` so NO exit path can
            # skip them, and before the log tee detaches below. (May 23 hook
            # was at the try-tail; one June 10 game still missed it.)
            try:
                actor_adapter = getattr(self.engine, '_deepseek_actor_adapter', None) \
                    or getattr(self.engine, '_openrouter_adapter', None)
                if actor_adapter is not None and hasattr(actor_adapter, 'get_stats'):
                    stats = actor_adapter.get_stats()
                    print(f"[STATS-GAME] cube_draft: calls={stats.get('calls', 0)} "
                          f"prompt_tokens={stats.get('prompt_tokens', 0)} "
                          f"completion_tokens={stats.get('completion_tokens', 0)} "
                          f"(cumulative under parallel batches — see CLAUDE.md caveat)")
                    print(f"[CALL-BREAKDOWN-FINAL] cube_draft: "
                          f"calls={stats.get('calls', 0)} "
                          f"purpose_counts={stats.get('purpose_counts', {})}")
            except Exception as _cb_err:
                print(f"[CALL-BREAKDOWN-FINAL] cube_draft emit failed: {_cb_err}")
            # Cleanup logging and game state
            if hasattr(self.game_cog, '_stdout_tee') and self.game_cog._stdout_tee:
                self.game_cog._stdout_tee.active_thread = None
            if thread:
                if hasattr(self.game_cog, '_cleanup_game_logging'):
                    self.game_cog._cleanup_game_logging(thread.id)
                if thread.id in self.engine.games:
                    self.engine.delete_game(thread.id)
            result["duration_seconds"] = _time.time() - start_time
            print(f"[AUTO-DRAFT] Complete: {result['outcome']} in {result['duration_seconds']:.0f}s, "
                  f"{result['turns']} turns")

        return result
