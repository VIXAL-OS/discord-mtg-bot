"""
Visual Board State Renderer for MTG Discord Bot
================================================
Generates composite images of the game board using Pillow.
Fetches card art from Scryfall and arranges it on a playmat-style layout.
"""

import asyncio
import aiohttp
import io
import os
from typing import Optional, Dict, List, Tuple, TYPE_CHECKING
from PIL import Image, ImageDraw, ImageFont
from pathlib import Path
import hashlib

if TYPE_CHECKING:
    from mtg_game import GameState, Player, Card


# =============================================================================
# CONFIGURATION
# =============================================================================

# Image dimensions
BOARD_WIDTH = 1800
BOARD_HEIGHT = 1200  # Default / minimum, actual height is dynamic
CARD_WIDTH = 180
CARD_HEIGHT = 252
CARD_SPACING = 15
LAND_HEIGHT = 196  # Lands are shown smaller but still readable
LAND_WIDTH = 140   # Width for lands (maintains card aspect ratio)

# Hand view dimensions (larger for readability)
HAND_CARD_WIDTH = 244
HAND_CARD_HEIGHT = 340
HAND_CARD_SPACING = 18

# Layout constants
MAIN_AREA_WIDTH = BOARD_WIDTH - 200  # Reserve 200px for commander zone
PLAYER_PADDING = 15  # Horizontal padding from edges
INFO_BAR_HEIGHT = 35  # Height reserved for the player info bar
ROW_GAP = 10  # Vertical gap between card rows

# Colors (RGB)
COLOR_BG = (30, 30, 35)  # Dark background
COLOR_PLAYER1_BG = (40, 45, 60)  # Slightly blue tint for player 1
COLOR_PLAYER2_BG = (60, 45, 40)  # Slightly red tint for player 2  
COLOR_ACTIVE = (255, 215, 0)  # Gold border for active player
COLOR_TEXT = (240, 240, 240)  # White text
COLOR_LIFE = (220, 50, 50)  # Red for life
COLOR_MANA = (100, 150, 255)  # Blue for mana
COLOR_POISON = (100, 200, 100)  # Green for poison
COLOR_TAPPED = (80, 80, 80)  # Darkened overlay for tapped
COLOR_ZONE_LABEL = (180, 180, 180)  # Gray for zone labels
COLOR_DIVIDER = (80, 80, 90)  # Divider lines

# Cache directory for card images
CACHE_DIR = Path(__file__).parent / "data" / "card_cache"


def _player_area_layout(
        player_heights: List[int], divider_height: int = 30
) -> Tuple[Dict[int, int], List[int], int]:
    """Lay out every stable seat once, with seat 0 at the bottom.

    The original renderer calculated only ``p2_base_y`` and ``p1_base_y``;
    indices 2 and 3 were consequently painted directly over seat 1.  A pure
    layout helper makes the N-seat invariant cheap to pin without fetching
    card images in tests.
    """
    base_by_index: Dict[int, int] = {}
    divider_starts: List[int] = []
    cursor = 0
    visual_order = list(reversed(range(len(player_heights))))
    for visual_position, player_index in enumerate(visual_order):
        base_by_index[player_index] = cursor
        cursor += player_heights[player_index]
        if visual_position < len(visual_order) - 1:
            divider_starts.append(cursor)
            cursor += divider_height
    return base_by_index, divider_starts, cursor


# =============================================================================
# CARD IMAGE FETCHER
# =============================================================================

class CardImageFetcher:
    """Fetches and caches card images from Scryfall."""
    
    def __init__(self):
        self.session: Optional[aiohttp.ClientSession] = None
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
    
    async def _get_session(self) -> aiohttp.ClientSession:
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession()
        return self.session
    
    def _cache_path(self, card_name: str) -> Path:
        """Get cache file path for a card."""
        # Use hash to handle special characters in card names
        safe_name = hashlib.md5(card_name.lower().encode()).hexdigest()
        return CACHE_DIR / f"{safe_name}.png"
    
    async def get_card_image(self, card_name: str) -> Optional[Image.Image]:
        """
        Get card image, from cache or Scryfall.
        Returns PIL Image or None on failure.
        """
        cache_path = self._cache_path(card_name)

        # Check cache first
        if cache_path.exists():
            try:
                return Image.open(cache_path)
            except Exception:
                cache_path.unlink(missing_ok=True)

        # Fetch from Scryfall with retry (rate limit is 75ms between requests)
        session = await self._get_session()
        for attempt in range(2):
            try:
                # Rate limit: wait between Scryfall requests
                await asyncio.sleep(0.1 * (attempt + 1))

                # First, get card data to find image URL
                async with session.get(
                    f"https://api.scryfall.com/cards/named?fuzzy={card_name}",
                    timeout=aiohttp.ClientTimeout(total=15)
                ) as resp:
                    if resp.status == 429:  # Rate limited
                        await asyncio.sleep(1)
                        continue
                    if resp.status != 200:
                        return None
                    data = await resp.json()

                # Get image URL (prefer large for hand view)
                image_uris = data.get("image_uris", {})
                if not image_uris and data.get("card_faces"):
                    image_uris = data["card_faces"][0].get("image_uris", {})

                image_url = image_uris.get("large") or image_uris.get("normal") or image_uris.get("small")
                if not image_url:
                    return None

                # Download image
                async with session.get(image_url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status != 200:
                        return None
                    image_data = await resp.read()

                # Save to cache and return
                img = Image.open(io.BytesIO(image_data))
                # Convert to RGB if needed (PNG might have alpha)
                if img.mode in ('RGBA', 'P'):
                    img = img.convert('RGB')
                img.save(cache_path, "PNG")
                return img

            except Exception as e:
                if attempt == 0:
                    print(f"[CARD-IMG] Retry for {card_name}: {e}")
                    continue
                print(f"[CARD-IMG] Failed to fetch {card_name}: {e}")
                return None
        return None
    
    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()


# =============================================================================
# BOARD RENDERER
# =============================================================================

class BoardRenderer:
    """Renders game state as a composite image."""
    
    def __init__(self):
        self.fetcher = CardImageFetcher()
        self._font = None
        self._font_small = None
        self._font_large = None
        self._font_emoji = None       # Color emoji font (for 🤖 ⚔ ☠ etc.)
        self._font_emoji_small = None

    def _get_fonts(self) -> Tuple:
        """Get fonts, with fallbacks."""
        if self._font is None:
            # Try to find a good text font, fall back to default
            font_paths = [
                "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                "/usr/share/fonts/TTF/DejaVuSans.ttf",
                "/System/Library/Fonts/Helvetica.ttc",
                "C:\\Windows\\Fonts\\arial.ttf",
            ]
            font_path = None
            for fp in font_paths:
                if os.path.exists(fp):
                    font_path = fp
                    break

            try:
                if font_path:
                    self._font = ImageFont.truetype(font_path, 14)
                    self._font_small = ImageFont.truetype(font_path, 11)
                    self._font_large = ImageFont.truetype(font_path, 18)
                else:
                    self._font = ImageFont.load_default()
                    self._font_small = self._font
                    self._font_large = self._font
            except Exception:
                self._font = ImageFont.load_default()
                self._font_small = self._font
                self._font_large = self._font

            # Try to find a color emoji font for emoji rendering
            emoji_font_paths = [
                "C:\\Windows\\Fonts\\seguiemj.ttf",           # Windows: Segoe UI Emoji
                "/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf",  # Linux
                "/usr/share/fonts/noto-colk/NotoColorEmoji.ttf",      # Fedora
                "/System/Library/Fonts/Apple Color Emoji.ttc",         # macOS
            ]
            emoji_path = None
            for fp in emoji_font_paths:
                if os.path.exists(fp):
                    emoji_path = fp
                    break

            if emoji_path:
                try:
                    self._font_emoji = ImageFont.truetype(emoji_path, 18)
                    self._font_emoji_small = ImageFont.truetype(emoji_path, 14)
                except Exception:
                    self._font_emoji = None
                    self._font_emoji_small = None

        return self._font, self._font_small, self._font_large

    def _draw_emoji(self, draw: ImageDraw, pos: Tuple[int, int], emoji: str,
                    fill=(240, 240, 240), size: str = "normal"):
        """
        Draw an emoji character. Uses color emoji font if available,
        falls back to text font (may render as outline/missing glyph).
        """
        font = self._font_emoji if size == "normal" else self._font_emoji_small
        if font:
            # Color emoji font available — use embedded_color for full color rendering
            draw.text(pos, emoji, font=font, embedded_color=True)
        else:
            # Fallback: render with text font (monochrome/outline)
            fallback = self._font if size == "normal" else self._font_small
            draw.text(pos, emoji, fill=fill, font=fallback)
    
    def _draw_card_placeholder(self, draw: ImageDraw, x: int, y: int,
                                width: int, height: int, name: str,
                                tapped: bool = False, attacking: bool = False):
        """Draw a placeholder rectangle for a card (when image unavailable)."""
        # Card background
        color = (60, 60, 70) if not tapped else (40, 40, 45)
        draw.rectangle([x, y, x + width, y + height], fill=color, outline=(100, 100, 110))

        # Card name — word-wrap to fit within the card width
        font, font_small, _ = self._get_fonts()
        max_text_width = width - 10  # 5px padding on each side
        words = name.split()
        lines = []
        current_line = ""
        for word in words:
            test_line = f"{current_line} {word}".strip() if current_line else word
            bbox = font_small.getbbox(test_line) if hasattr(font_small, 'getbbox') else (0, 0, len(test_line) * 7, 12)
            text_width = bbox[2] - bbox[0]
            if text_width <= max_text_width:
                current_line = test_line
            else:
                if current_line:
                    lines.append(current_line)
                current_line = word
        if current_line:
            lines.append(current_line)

        for i, line in enumerate(lines[:4]):  # Max 4 lines
            draw.text((x + 5, y + 5 + i * 16), line, fill=COLOR_TEXT, font=font_small)
        
        # Attacking indicator
        if attacking:
            self._draw_emoji(draw, (x + width - 20, y + 2), "⚔",
                            fill=(255, 80, 80), size="small")

    def _paste_card(self, board: Image.Image, card_img: Image.Image,
                    x: int, y: int, width: int, height: int,
                    tapped: bool = False):
        """Paste a card image onto the board, handling tapping rotation."""
        # Resize card image
        card_img = card_img.resize((width, height), Image.Resampling.LANCZOS)
        
        if tapped:
            # Rotate 90 degrees for tapped
            card_img = card_img.rotate(90, expand=True)
            # Darken slightly
            from PIL import ImageEnhance
            enhancer = ImageEnhance.Brightness(card_img)
            card_img = enhancer.enhance(0.7)
            # Adjust position for rotated dimensions
            board.paste(card_img, (x, y + (height - width) // 2))
        else:
            board.paste(card_img, (x, y))
    
    async def _render_card(self, board: Image.Image, draw: ImageDraw,
                           card: 'Card', x: int, y: int,
                           width: int, height: int) -> int:
        """
        Render a single card at position. Returns the actual width used.
        """
        # Try to get card image
        card_img = await self.fetcher.get_card_image(card.name)
        
        if card_img:
            self._paste_card(board, card_img, x, y, width, height, card.tapped)
            actual_width = height if card.tapped else width  # Tapped cards are rotated
        else:
            # Draw placeholder
            self._draw_card_placeholder(draw, x, y, width, height, card.name, 
                                        card.tapped, card.attacking)
            actual_width = width
        
        # Draw overlays (counters, attacking indicator)
        font, font_small, _ = self._get_fonts()
        
        if card.counters:
            counter_text = " ".join(f"+{v}" for v in card.counters.values())
            draw.text((x + 2, y + height - 15), counter_text, fill=(100, 255, 100), font=font_small)
        
        if card.attacking:
            self._draw_emoji(draw, (x + width - 20, y + 2), "⚔",
                            fill=(255, 80, 80), size="small")
        
        return actual_width
    
    def _calc_card_layout(self, count: int, available_width: int,
                          base_w: int, base_h: int,
                          is_land: bool = False) -> dict:
        """
        Calculate card dimensions, spacing, and row layout for a set of cards.
        Returns dict with card_w, card_h, cards_per_row, num_rows, spacing/overlap.
        """
        if count == 0:
            return {"card_w": base_w, "card_h": base_h, "cards_per_row": 0,
                    "num_rows": 0, "spacing": 0, "total_height": 0}

        if is_land:
            # Lands use overlap instead of spacing
            if count <= 6:
                land_w, land_h = base_w, base_h
                overlap = 25
            elif count <= 10:
                land_w, land_h = 120, 168
                overlap = 35
            else:
                land_w, land_h = 100, 140
                overlap = 50
            # How many fit in one row? First card takes full width, rest take (w - overlap)
            if land_w - overlap > 0:
                cards_per_row = 1 + max(0, (available_width - land_w) // (land_w - overlap))
            else:
                cards_per_row = max(1, available_width // land_w)
            cards_per_row = max(1, cards_per_row)
            num_rows = (count + cards_per_row - 1) // cards_per_row
            total_height = num_rows * land_h + (num_rows - 1) * ROW_GAP
            return {"card_w": land_w, "card_h": land_h, "cards_per_row": cards_per_row,
                    "num_rows": num_rows, "overlap": overlap, "total_height": total_height,
                    "is_land": True}
        else:
            # Permanents use spacing (no overlap)
            if count <= 8:
                card_w, card_h = base_w, base_h
                card_spacing = CARD_SPACING
            elif count <= 14:
                card_w, card_h = 150, 210
                card_spacing = 10
            else:
                card_w, card_h = 120, 168
                card_spacing = 5
            cards_per_row = max(1, (available_width + card_spacing) // (card_w + card_spacing))
            num_rows = (count + cards_per_row - 1) // cards_per_row
            total_height = num_rows * card_h + (num_rows - 1) * ROW_GAP
            return {"card_w": card_w, "card_h": card_h, "cards_per_row": cards_per_row,
                    "num_rows": num_rows, "spacing": card_spacing, "total_height": total_height,
                    "is_land": False}

    def _calc_player_height(self, player, available_width: int) -> Tuple[int, dict, dict]:
        """
        Calculate the total height needed for a player's area.
        Returns (height, land_layout, perm_layout).
        """
        lands = [c for c in player.battlefield if c.is_land()]
        non_lands = [c for c in player.battlefield if not c.is_land()]

        land_layout = self._calc_card_layout(len(lands), available_width, LAND_WIDTH, LAND_HEIGHT, is_land=True)
        perm_layout = self._calc_card_layout(len(non_lands), available_width, CARD_WIDTH, CARD_HEIGHT, is_land=False)

        # Height = info bar + gap + creatures rows + gap + lands rows + padding
        height = INFO_BAR_HEIGHT + 10  # info bar + small gap
        if perm_layout["num_rows"] > 0:
            height += perm_layout["total_height"] + ROW_GAP
        if land_layout["num_rows"] > 0:
            height += land_layout["total_height"] + ROW_GAP
        height += 10  # bottom padding

        # Minimum height so the area isn't too cramped
        height = max(height, 280)

        return height, land_layout, perm_layout

    async def render_board(self, game: 'GameState') -> io.BytesIO:
        """
        Render the full game board as a PNG image.
        Board height is dynamic — grows to fit all cards with multiple rows.
        Returns BytesIO buffer ready for Discord upload.
        """
        font, font_small, font_large = self._get_fonts()
        available_width = MAIN_AREA_WIDTH - 2 * PLAYER_PADDING

        # --- Calculate dynamic height for each player ---
        player_heights = []
        player_layouts = []
        for player in game.players:
            h, land_layout, perm_layout = self._calc_player_height(player, available_width)
            player_heights.append(h)
            player_layouts.append((land_layout, perm_layout))

        divider_height = 30  # Space between adjacent player areas
        player_base_y, divider_starts, total_height = _player_area_layout(
            player_heights, divider_height)

        # Create board image with dynamic height
        board = Image.new('RGB', (BOARD_WIDTH, total_height), COLOR_BG)
        draw = ImageDraw.Draw(board)

        for player_idx, player in enumerate(game.players):
            land_layout, perm_layout = player_layouts[player_idx]
            base_y = player_base_y[player_idx]
            player_height = player_heights[player_idx]

            # Background for this player's area
            is_active = player_idx == game.active_player_index
            bg_color = (COLOR_PLAYER1_BG if player_idx % 2 == 0
                        else COLOR_PLAYER2_BG)
            draw.rectangle(
                [0, base_y, MAIN_AREA_WIDTH, base_y + player_height - 1],
                fill=bg_color
            )

            # Commander zone background (right side)
            draw.rectangle(
                [MAIN_AREA_WIDTH, base_y, BOARD_WIDTH, base_y + player_height - 1],
                fill=(50, 50, 55)
            )

            # Active player highlight
            if is_active:
                draw.rectangle(
                    [0, base_y, BOARD_WIDTH - 1, base_y + player_height - 1],
                    outline=COLOR_ACTIVE, width=3
                )

            # --- Player info bar ---
            # Player 1 (bottom): info at bottom of area. Player 2 (top): info at top.
            if player_idx == 0:
                info_y = base_y + player_height - INFO_BAR_HEIGHT
            else:
                info_y = base_y + 5

            # Player info bar with color emoji
            name_prefix = "> " if is_active else ""
            if getattr(player, 'eliminated', False):
                name_prefix += "[OUT] "
            name_text = f"{name_prefix}{player.name}"
            has_poison = player.poison > 0

            x_offset = PLAYER_PADDING
            draw.text((x_offset, info_y), name_text, fill=COLOR_TEXT, font=font_large)
            # Measure actual name width so bot emoji sits right after it
            name_bbox = draw.textbbox((0, 0), name_text, font=font_large)
            x_offset += (name_bbox[2] - name_bbox[0]) + 4
            if player.is_claude:
                self._draw_emoji(draw, (x_offset, info_y), "🤖")
                x_offset += 24
            x_offset = max(x_offset, PLAYER_PADDING + 200)  # min spacing

            # ❤️ Life
            self._draw_emoji(draw, (x_offset, info_y - 2), "❤️", size="small")
            x_offset += 20
            draw.text((x_offset, info_y), str(player.life), fill=COLOR_LIFE, font=font_large)
            x_offset += 50

            # ☠ Poison (only if > 0)
            if has_poison:
                self._draw_emoji(draw, (x_offset, info_y), "☠", fill=COLOR_POISON, size="small")
                x_offset += 20
                draw.text((x_offset, info_y), str(player.poison), fill=COLOR_POISON, font=font)
                x_offset += 30

            # ✋ Hand
            self._draw_emoji(draw, (x_offset, info_y - 2), "✋", size="small")
            x_offset += 20
            draw.text((x_offset, info_y), str(len(player.hand)), fill=COLOR_TEXT, font=font)
            x_offset += 35

            # 🃏 Deck
            self._draw_emoji(draw, (x_offset, info_y - 2), "🃏", size="small")
            x_offset += 20
            draw.text((x_offset, info_y), str(len(player.library)), fill=COLOR_MANA, font=font)
            x_offset += 40

            # 💀 Graveyard (only if non-empty)
            if player.graveyard:
                self._draw_emoji(draw, (x_offset, info_y - 2), "💀", size="small")
                x_offset += 20
                draw.text((x_offset, info_y), str(len(player.graveyard)), fill=COLOR_ZONE_LABEL, font=font)

            # --- Calculate card row positions ---
            # Player 1 (bottom): info bar at bottom, then lands above, then creatures above lands
            # Player 2 (top): info bar at top, then lands below, then creatures below lands
            if player_idx == 0:
                # Bottom player: build upward from info bar
                cursor_y = info_y - 10  # gap before info bar
                # Lands row(s) — closest to info bar
                if land_layout["num_rows"] > 0:
                    lands_start_y = cursor_y - land_layout["total_height"]
                    cursor_y = lands_start_y - ROW_GAP
                else:
                    lands_start_y = cursor_y
                # Creatures row(s) — above lands
                if perm_layout["num_rows"] > 0:
                    creatures_start_y = cursor_y - perm_layout["total_height"]
                else:
                    creatures_start_y = cursor_y
            else:
                # Top player: build downward from info bar
                cursor_y = info_y + INFO_BAR_HEIGHT + 5
                # Lands row(s) — closest to info bar
                lands_start_y = cursor_y
                if land_layout["num_rows"] > 0:
                    cursor_y = lands_start_y + land_layout["total_height"] + ROW_GAP
                # Creatures row(s) — below lands
                creatures_start_y = cursor_y

            # --- Render lands (all rows) ---
            lands = [c for c in player.battlefield if c.is_land()]
            if lands:
                l_w = land_layout["card_w"]
                l_h = land_layout["card_h"]
                l_overlap = land_layout.get("overlap", 25)
                cpr = land_layout["cards_per_row"]
                for i, land in enumerate(lands):
                    row = i // cpr
                    col = i % cpr
                    lx = PLAYER_PADDING + col * (l_w - l_overlap)
                    ly = lands_start_y + row * (l_h + ROW_GAP)
                    await self._render_card(board, draw, land, lx, ly, l_w, l_h)

            # --- Render permanents (all rows) ---
            non_lands = [c for c in player.battlefield if not c.is_land()]
            if non_lands:
                c_w = perm_layout["card_w"]
                c_h = perm_layout["card_h"]
                c_spacing = perm_layout.get("spacing", CARD_SPACING)
                cpr = perm_layout["cards_per_row"]
                for i, perm in enumerate(non_lands):
                    row = i // cpr
                    col = i % cpr
                    px = PLAYER_PADDING + col * (c_w + c_spacing)
                    py = creatures_start_y + row * (c_h + ROW_GAP)
                    await self._render_card(board, draw, perm, px, py, c_w, c_h)

            # --- Commander zone (right side) ---
            commander_y = base_y + 30 if player_idx == 1 else base_y + 30
            draw.text((MAIN_AREA_WIDTH + 10, commander_y - 25), "Command Zone",
                     fill=COLOR_ZONE_LABEL, font=font_small)

            if hasattr(player, 'command_zone') and player.command_zone:
                cmd_y = commander_y
                for cmd in player.command_zone[:2]:
                    await self._render_card(board, draw, cmd, MAIN_AREA_WIDTH + 10, cmd_y,
                                           160, 224)
                    # Label signature spells vs commander/oathbreaker
                    if getattr(cmd, 'is_signature_spell', False):
                        draw.text((MAIN_AREA_WIDTH + 10, cmd_y + 224),
                                 "📜 Sig. Spell", fill=(200, 180, 255), font=font_small)
                    elif getattr(cmd, 'is_commander', False):
                        label = "⚔ Oathbreaker" if game and getattr(game, 'format', '') == "oathbreaker" else "👑 Commander"
                        draw.text((MAIN_AREA_WIDTH + 10, cmd_y + 224),
                                 label, fill=(255, 215, 0), font=font_small)
                    cmd_y += 245
            else:
                draw.text((MAIN_AREA_WIDTH + 15, commander_y + 50), "(empty)",
                         fill=COLOR_ZONE_LABEL, font=font_small)

            # Show suspended cards if any
            if hasattr(player, 'exile'):
                suspended = [c for c in player.exile if getattr(c, 'suspended', False)]
                if suspended:
                    suspend_y = commander_y + (240 if hasattr(player, 'command_zone') and player.command_zone else 0)
                    draw.text((MAIN_AREA_WIDTH + 10, suspend_y), "Suspended:",
                             fill=(255, 200, 100), font=font_small)
                    suspend_y += 15
                    for sc in suspended[:3]:
                        time_counters = sc.counters.get('time', 0)
                        self._draw_emoji(draw, (MAIN_AREA_WIDTH + 15, suspend_y),
                                        "⏳", fill=COLOR_TEXT, size="small")
                        draw.text((MAIN_AREA_WIDTH + 33, suspend_y),
                                 f"{sc.name} ({time_counters})",
                                 fill=COLOR_TEXT, font=font_small)
                        suspend_y += 15

        # --- Dividers and turn info ---
        divider_centers = [
            start + divider_height // 2 for start in divider_starts
        ]
        for center in divider_centers:
            draw.line([(0, center), (MAIN_AREA_WIDTH, center)],
                      fill=COLOR_DIVIDER, width=2)
        center_y = (divider_centers[len(divider_centers) // 2]
                    if divider_centers else total_height // 2)

        # Turn and phase info
        phase_name = game.phase.value.replace("_", " ").title()
        turn_text = f"Turn {game.turn_number} - {phase_name}"

        bbox = draw.textbbox((0, 0), turn_text, font=font)
        text_width = bbox[2] - bbox[0]
        text_x = (MAIN_AREA_WIDTH - text_width) // 2

        draw.rectangle(
            [text_x - 10, center_y - 12, text_x + text_width + 10, center_y + 12],
            fill=COLOR_BG
        )
        draw.text((text_x, center_y - 8), turn_text, fill=COLOR_TEXT, font=font)

        # Stack indicator
        if game.stack:
            stack_x = BOARD_WIDTH - 200
            self._draw_emoji(draw, (stack_x, center_y - 10), "📚",
                            fill=(255, 200, 100), size="small")
            draw.text((stack_x + 22, center_y - 8),
                     f"Stack: {len(game.stack)} item(s)",
                     fill=(255, 200, 100), font=font)

        # Save to buffer
        buffer = io.BytesIO()
        board.save(buffer, format='PNG', optimize=True)
        buffer.seek(0)
        return buffer
    
    async def render_hand(self, player: 'Player') -> io.BytesIO:
        """
        Render a player's hand as an image.
        Returns BytesIO buffer ready for Discord upload.
        """
        if not player.hand:
            # Empty hand - just return a small image
            img = Image.new('RGB', (300, 50), COLOR_BG)
            draw = ImageDraw.Draw(img)
            font, _, _ = self._get_fonts()
            draw.text((10, 15), "Your hand is empty", fill=COLOR_TEXT, font=font)
            buffer = io.BytesIO()
            img.save(buffer, format='PNG')
            buffer.seek(0)
            return buffer
        
        # Calculate image size - use larger hand card dimensions
        cards_per_row = min(4, len(player.hand))  # 4 per row for bigger cards
        rows = (len(player.hand) + 3) // 4
        width = cards_per_row * (HAND_CARD_WIDTH + HAND_CARD_SPACING) + 30
        height = rows * (HAND_CARD_HEIGHT + HAND_CARD_SPACING) + 60
        
        img = Image.new('RGB', (width, height), COLOR_BG)
        draw = ImageDraw.Draw(img)
        font, font_small, font_large = self._get_fonts()
        
        # Header
        draw.text((15, 12), f"Your Hand ({len(player.hand)} cards)", 
                 fill=COLOR_TEXT, font=font_large)
        
        # Render cards in grid
        for i, card in enumerate(player.hand):
            row = i // 4
            col = i % 4
            x = 15 + col * (HAND_CARD_WIDTH + HAND_CARD_SPACING)
            y = 50 + row * (HAND_CARD_HEIGHT + HAND_CARD_SPACING)
            
            card_img = await self.fetcher.get_card_image(card.name)
            if card_img:
                card_img = card_img.resize((HAND_CARD_WIDTH, HAND_CARD_HEIGHT), Image.Resampling.LANCZOS)
                img.paste(card_img, (x, y))
            else:
                self._draw_card_placeholder(draw, x, y, HAND_CARD_WIDTH, HAND_CARD_HEIGHT, card.name)
            
            # Card number overlay with background for visibility
            num_text = str(i + 1)
            draw.rectangle([x + 2, y + 2, x + 22, y + 22], fill=(0, 0, 0, 180))
            draw.text((x + 6, y + 4), num_text, fill=(255, 255, 255), font=font)
        
        buffer = io.BytesIO()
        img.save(buffer, format='PNG', optimize=True)
        buffer.seek(0)
        return buffer
    
    async def close(self):
        await self.fetcher.close()


# =============================================================================
# CONVENIENCE FUNCTION
# =============================================================================

# Global renderer instance
_renderer: Optional[BoardRenderer] = None

async def get_renderer() -> BoardRenderer:
    """Get or create the global board renderer."""
    global _renderer
    if _renderer is None:
        _renderer = BoardRenderer()
    return _renderer

async def render_game_board(game: 'GameState') -> io.BytesIO:
    """Render a game board image. Convenience function."""
    renderer = await get_renderer()
    return await renderer.render_board(game)

async def render_player_hand(player: 'Player') -> io.BytesIO:
    """Render a player's hand. Convenience function."""
    renderer = await get_renderer()
    return await renderer.render_hand(player)


# =============================================================================
# TEST
# =============================================================================

if __name__ == "__main__":
    # Quick test with mock data
    from dataclasses import dataclass, field
    from typing import List
    from enum import Enum
    
    class Phase(Enum):
        MAIN1 = "main1"
    
    LAND_NAMES = {
        "Forest", "Island", "Mountain", "Swamp", "Plains",
        "Breeding Pool", "Hinterland Harbor", "Misty Rainforest",
        "Command Tower", "Simic Growth Chamber", "Yavimaya Coast",
        "Botanical Sanctum", "Flooded Grove", "Blood Crypt",
        "Dragonskull Summit", "Buried Ruin", "Myriad Landscape",
    }

    @dataclass
    class MockCard:
        name: str
        tapped: bool = False
        attacking: bool = False
        counters: dict = field(default_factory=dict)
        _is_land: bool = False
        def is_land(self): return self._is_land
    
    @dataclass
    class MockPlayer:
        name: str
        life: int = 20
        poison: int = 0
        is_claude: bool = False
        hand: List = field(default_factory=list)
        library: List = field(default_factory=list)
        graveyard: List = field(default_factory=list)
        battlefield: List = field(default_factory=list)
    
    @dataclass
    class MockGame:
        players: List[MockPlayer]
        turn_number: int = 1
        phase: Phase = Phase.MAIN1
        active_player_index: int = 0
        stack: List = field(default_factory=list)
    
    def mc(name, **kwargs):
        """Helper to create MockCard with auto-detected land status."""
        kwargs.setdefault('_is_land', name in LAND_NAMES)
        return MockCard(name=name, **kwargs)

    async def test():
        # Test with lots of cards to exercise multi-row layout
        player1 = MockPlayer(
            name="viksalos",
            life=18,
            battlefield=[
                mc("Forest"), mc("Forest"), mc("Island"),
                mc("Breeding Pool"), mc("Hinterland Harbor"),
                mc("Misty Rainforest"), mc("Command Tower"),
                mc("Simic Growth Chamber"), mc("Yavimaya Coast"),
                mc("Botanical Sanctum"), mc("Flooded Grove"),
                mc("Forest"), mc("Island"), mc("Forest"),
                mc("Llanowar Elves", tapped=True),
                mc("Tarmogoyf", attacking=True, counters={"+1/+1": 2}),
                mc("Birds of Paradise"),
                mc("Coiling Oracle"),
                mc("Eternal Witness"),
                mc("Thragtusk"),
                mc("Avenger of Zendikar"),
                mc("Craterhoof Behemoth"),
                mc("Oracle of Mul Daya"),
                mc("Courser of Kruphix"),
            ],
            hand=[mc("Lightning Bolt"), mc("Giant Growth")],
            library=[mc("x")] * 50,
        )

        player2 = MockPlayer(
            name="Claude",
            life=15,
            is_claude=True,
            battlefield=[
                mc("Mountain"), mc("Mountain"), mc("Swamp"),
                mc("Blood Crypt"), mc("Dragonskull Summit"),
                mc("Goblin Guide"),
                mc("Arclight Phoenix"),
                mc("Batterskull"),
                mc("Sarkhan Fireblood"),
                mc("Chandra Pyromaster"),
                mc("Terror of the Peaks"),
                mc("Phyrexian Processor"),
                mc("Emrakul the World Anew"),
            ],
            hand=[mc("x")] * 4,
            library=[mc("x")] * 45,
            graveyard=[mc("x")] * 3,
        )

        game = MockGame(players=[player1, player2])
        
        renderer = BoardRenderer()
        buffer = await renderer.render_board(game)
        
        # Save test image
        with open("test_board.png", "wb") as f:
            f.write(buffer.read())
        print("Saved test_board.png")
        
        await renderer.close()
    
    asyncio.run(test())
