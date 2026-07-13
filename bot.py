"""
Discord MTG Bot

A Discord bot that plays Magic: The Gathering — full Commander / Modern /
Legacy / Vintage / Pioneer / Pauper / Limited / Brawl / Oathbreaker / Cube
support, with a tiered effect-resolution engine (templates → patterns →
XMage bridge → LLM judge), an XMage card-database integration, autoplay
loop for batch testing, an `!undo` snapshot stack, and persona-driven
chat during game threads.

Setup:
  1. cp config.json.example config.json   (edit it)
  2. cp .env.example .env                  (add DISCORD_TOKEN + ANTHROPIC_API_KEY)
  3. pip install -r requirements.txt
  4. python bot.py
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

import aiohttp
import anthropic
import discord
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()


# =============================================================================
# CONFIG
# =============================================================================

@dataclass
class BotConfig:
    # Model selection — Sonnet handles everything for the MTG bot.
    # (The companion bot in the sibling repo handles the Opus/Haiku distress
    # switching; here we just want fast, capable chat during games.)
    model_default: str = "claude-sonnet-5"
    max_tokens: int = 2048

    # Context management
    max_messages_per_thread: int = 20
    max_input_tokens: int = 30000
    chars_per_token: float = 4.0

    # Cost tracking (Sonnet 4)
    sonnet_input_cost_per_million: float = 3.0
    sonnet_output_cost_per_million: float = 15.0

    # DeepSeek V4 pricing — used by the MTG actor/strategist split in autoplay.
    # REAL rates verified May 30 2026 against an account usage export (the old
    # list rates $0.27/$1.10 + $0.56/$1.68 over-estimated ~38x). This tracker
    # has no per-call cache split, so input is priced at the cache-BLENDED
    # effective rate (Flash ~66% hit, Pro ~84% hit); output uses exact rates.
    # Per-category for reference: Flash hit $0.0028/M / miss $0.14/M;
    # Pro hit $0.0036/M / miss $0.435/M.
    deepseek_input_cost_per_million: float = 0.0497
    deepseek_output_cost_per_million: float = 0.28
    deepseek_pro_input_cost_per_million: float = 0.0744
    deepseek_pro_output_cost_per_million: float = 0.87

    # Attachment handling
    image_types: tuple = ('.png', '.jpg', '.jpeg', '.gif', '.webp')
    max_image_size_mb: float = 20.0


CONFIG = BotConfig()


# =============================================================================
# SCRYFALL CLIENT
# =============================================================================

class ScryfallClient:
    """Async client for the Scryfall MTG card API."""

    BASE_URL = "https://api.scryfall.com"

    async def search_card(self, query: str) -> Optional[Dict]:
        """Search for a card by name (fuzzy match)."""
        async with aiohttp.ClientSession() as session:
            url = f"{self.BASE_URL}/cards/named"
            params = {"fuzzy": query}
            try:
                async with session.get(url, params=params) as resp:
                    if resp.status == 200:
                        return await resp.json()
                    elif resp.status == 404:
                        url = f"{self.BASE_URL}/cards/search"
                        params = {"q": query, "order": "released", "dir": "desc"}
                        async with session.get(url, params=params) as search_resp:
                            if search_resp.status == 200:
                                data = await search_resp.json()
                                if data.get("data"):
                                    return data["data"][0]
            except Exception as e:
                print(f"Scryfall error: {e}")
        return None

    async def random_card(self, query: str = None) -> Optional[Dict]:
        """Get a random card, optionally filtered with a Scryfall query."""
        async with aiohttp.ClientSession() as session:
            url = f"{self.BASE_URL}/cards/random"
            params = {"q": query} if query else {}
            try:
                async with session.get(url, params=params) as resp:
                    if resp.status == 200:
                        return await resp.json()
            except Exception as e:
                print(f"Scryfall error: {e}")
        return None

    def format_card(self, card: Dict) -> Tuple[str, Optional[discord.Embed]]:
        """Format card data for Discord display with image embed."""
        lines = []
        name = card.get("name", "Unknown")
        mana = card.get("mana_cost", "")
        type_line = card.get("type_line", "")
        oracle = card.get("oracle_text", "")

        lines.append(f"**{name}** {mana}")
        lines.append(f"*{type_line}*")

        if oracle:
            if len(oracle) > 500:
                oracle = oracle[:497] + "..."
            lines.append(oracle)

        if "power" in card:
            lines.append(f"**{card['power']}/{card['toughness']}**")
        if "loyalty" in card:
            lines.append(f"Loyalty: {card['loyalty']}")

        prices = card.get("prices", {})
        if prices.get("usd"):
            lines.append(f"💵 ${prices['usd']}")

        text = "\n".join(lines)

        embed = None
        image_uris = card.get("image_uris", {})
        if not image_uris and "card_faces" in card:
            image_uris = card["card_faces"][0].get("image_uris", {})

        if image_uris:
            embed = discord.Embed(title=name, description=f"{mana}\n{type_line}")
            embed.set_image(url=image_uris.get("normal") or image_uris.get("large") or image_uris.get("small"))
            if card.get("scryfall_uri"):
                embed.url = card["scryfall_uri"]
            colors = card.get("colors", [])
            color_map = {'W': 0xF9FAF4, 'U': 0x0E68AB, 'B': 0x150B00, 'R': 0xD3202A, 'G': 0x00733E}
            if len(colors) == 1:
                embed.color = color_map.get(colors[0], 0x888888)
            elif len(colors) > 1:
                embed.color = 0xC9A048  # gold for multicolor
            else:
                embed.color = 0x888888  # gray for colorless

        return text, embed


# =============================================================================
# THE BOT
# =============================================================================

class MTGBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True

        super().__init__(
            command_prefix="!",
            intents=intents,
            description="Discord MTG Bot"
        )

        self.claude = anthropic.Anthropic()
        self.scryfall = ScryfallClient()

        # Per-thread conversation history for light chat during game threads
        self.conversations: Dict[int, List[Dict]] = {}

        # Config — loaded from config.json
        self.mtg_channel_id: Optional[int] = None
        self.excluded_channels: set[int] = set()

        # Active persona — populated by load_config -> load_persona.
        # Minimal default so any pre-load access doesn't crash.
        self.persona: Dict = {
            "name": "Claude",
            "pronouns": "it",
            "intro": "an AI assistant here as a Discord MTG companion.",
            "personality_traits": [],
            "mannerisms": [],
            "voice_notes": "",
        }

        # Cost tracking
        self.total_input_tokens: int = 0
        self.total_output_tokens: int = 0
        self.api_calls: int = 0
        self.mtg_game_input_tokens: int = 0
        self.mtg_game_output_tokens: int = 0
        self.mtg_game_calls: int = 0
        self.deepseek_input_tokens: int = 0
        self.deepseek_output_tokens: int = 0
        self.deepseek_calls: int = 0
        self.deepseek_pro_input_tokens: int = 0
        self.deepseek_pro_output_tokens: int = 0
        self.deepseek_pro_calls: int = 0
        self._load_persistent_costs()

        self.load_config()

    # -------------------------------------------------------------------------
    # Persona loading
    # -------------------------------------------------------------------------

    def load_persona(self, name: str = "plain") -> Dict:
        """Load a persona JSON from personas/<name>.json.

        Falls back to personas/plain.json (no roleplay) if the requested
        persona doesn't exist. If even plain.json is missing, returns a
        minimal hard-coded default.
        """
        candidates = [f"personas/{name}.json", "personas/plain.json"] if name else ["personas/plain.json"]
        for path in candidates:
            try:
                with open(path, encoding='utf-8') as f:
                    data = json.load(f)
                tag = "" if path.endswith(f"{name}.json") else f" (requested '{name}' not found)"
                print(f"🎭 Loaded persona: {data.get('name', name)} ({path}){tag}")
                return data
            except (FileNotFoundError, json.JSONDecodeError):
                continue
        print("🎭 No persona files found; using built-in minimal default")
        return {
            "name": "Claude",
            "pronouns": "it",
            "intro": "an AI assistant here as a Discord MTG companion.",
            "personality_traits": ["You're thoughtful, direct, and warm."],
            "mannerisms": [],
            "voice_notes": "",
        }

    def _persona_intro(self) -> str:
        p = self.persona
        return f"You are {p.get('name', 'Claude')} ({p.get('pronouns', 'it')}), {p.get('intro', 'an AI assistant.')}"

    def _persona_traits_block(self) -> str:
        traits = self.persona.get("personality_traits", [])
        if not traits:
            return ""
        return "Your personality:\n" + "\n".join(f"- {t}" for t in traits)

    def _persona_mannerisms_block(self) -> str:
        manns = self.persona.get("mannerisms", [])
        if not manns:
            return ""
        notes = self.persona.get("voice_notes", "")
        prefix = f"Roleplay mannerisms ({notes}):" if notes else "Roleplay mannerisms (use sparingly, naturally):"
        return f"{prefix}\n" + "\n".join(manns)

    def build_base_prompt(self) -> str:
        """Compose the system prompt for chat (in game threads, or wherever
        the bot's been mentioned). MTG-focused — the bot's capabilities are
        all about playing Magic and looking up cards."""
        name = self.persona.get("name", "Claude")
        return "\n\n".join(part for part in [
            self._persona_intro(),
            self._persona_traits_block(),
            f"""You have several capabilities:
1. MTG Game Engine — facilitate full Magic games in Discord threads:
   - !game @opponent [format] — Start a game (or "!game claude" to play against you)
   - !deck <archidekt_url> — Load a deck for yourself
   - !play <card> — Play a card from hand
   - !attack <creatures> — Declare attackers
   - !block <attacker> with <blocker> — Declare blockers
   - !pass — Pass priority, !turn — End turn, !gg — Concede
   - !state — Show board state, !hand — View hand (DMs you), !graveyard — Check graveyards
   - !life, !damage — Track life totals
   - !judge <question> — Get a rules ruling
   - !undo — Roll back the most recent risky action (depth 5)
   - !coverage <deck> — See how the engine will handle each card's effects
   Players can challenge you directly with "!game @{name} commander" — you play as the AI opponent and guide them on commands during games.
2. Card lookups: !card (pretty Scryfall display), !xmage (raw rules engine data), !random (random card), !rulings, !price.
3. Conversation during games — chat naturally while you play.""",
            "Discord has a 2000 character limit per message. Be concise. Single newlines between actions and speech, not double spacing.",
            self._persona_mannerisms_block(),
        ] if part)

    # -------------------------------------------------------------------------
    # Config + cost persistence
    # -------------------------------------------------------------------------

    def load_config(self):
        """Load configuration from config.json."""
        try:
            with open("config.json", encoding='utf-8') as f:
                config = json.load(f)
                self.mtg_channel_id = config.get("mtg_channel_id")
                self.excluded_channels = set(config.get("excluded_channels", []))
                self.persona = self.load_persona(config.get("bot_persona", "plain"))
        except FileNotFoundError:
            print("No config.json found, using defaults")
            self.persona = self.load_persona("plain")

    def _load_persistent_costs(self):
        """Load lifetime token totals from disk if present."""
        try:
            with open("data/api_costs.json", encoding='utf-8') as f:
                data = json.load(f)
            for key, value in data.items():
                if hasattr(self, key) and isinstance(value, (int, float)):
                    setattr(self, key, value)
        except (FileNotFoundError, json.JSONDecodeError):
            pass

    def _save_persistent_costs(self):
        """Persist lifetime token totals to disk."""
        os.makedirs("data", exist_ok=True)
        tracked_fields = [
            "total_input_tokens", "total_output_tokens", "api_calls",
            "mtg_game_input_tokens", "mtg_game_output_tokens", "mtg_game_calls",
            "deepseek_input_tokens", "deepseek_output_tokens", "deepseek_calls",
            "deepseek_pro_input_tokens", "deepseek_pro_output_tokens", "deepseek_pro_calls",
        ]
        data = {k: getattr(self, k, 0) for k in tracked_fields}
        with open("data/api_costs.json", 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2)

    def _track_usage(self, input_tokens: int, output_tokens: int,
                     bucket: str = "chat") -> None:
        """Track API usage by bucket (chat / mtg_game)."""
        self.total_input_tokens += input_tokens
        self.total_output_tokens += output_tokens
        self.api_calls += 1
        if bucket == "mtg_game":
            self.mtg_game_input_tokens += input_tokens
            self.mtg_game_output_tokens += output_tokens
            self.mtg_game_calls += 1
        self._save_persistent_costs()

    def get_cost_summary(self) -> str:
        """Return a human-readable lifetime cost summary."""
        chat_in = self.total_input_tokens - self.mtg_game_input_tokens
        chat_out = self.total_output_tokens - self.mtg_game_output_tokens
        chat_cost = (
            chat_in * CONFIG.sonnet_input_cost_per_million / 1_000_000
            + chat_out * CONFIG.sonnet_output_cost_per_million / 1_000_000
        )
        ds_cost = (
            self.deepseek_input_tokens * CONFIG.deepseek_input_cost_per_million / 1_000_000
            + self.deepseek_output_tokens * CONFIG.deepseek_output_cost_per_million / 1_000_000
            + self.deepseek_pro_input_tokens * CONFIG.deepseek_pro_input_cost_per_million / 1_000_000
            + self.deepseek_pro_output_tokens * CONFIG.deepseek_pro_output_cost_per_million / 1_000_000
        )
        # MTG-game Sonnet portion (if any games went via Claude actor)
        mtg_sonnet_cost = (
            self.mtg_game_input_tokens * CONFIG.sonnet_input_cost_per_million / 1_000_000
            + self.mtg_game_output_tokens * CONFIG.sonnet_output_cost_per_million / 1_000_000
        )
        lines = [
            "💰 **Lifetime API Usage**",
            f"Total API calls: {self.api_calls:,}",
            "",
            f"**Chat (Sonnet)**: {chat_in:,} in / {chat_out:,} out → ${chat_cost:.4f}",
            f"**MTG games (Sonnet portion)**: {self.mtg_game_input_tokens:,} in / "
            f"{self.mtg_game_output_tokens:,} out → ${mtg_sonnet_cost:.4f}",
            f"**MTG games (DeepSeek)**: {self.deepseek_input_tokens + self.deepseek_pro_input_tokens:,} in / "
            f"{self.deepseek_output_tokens + self.deepseek_pro_output_tokens:,} out → ${ds_cost:.4f}",
            "",
            f"**Total**: ${chat_cost + mtg_sonnet_cost + ds_cost:.4f}",
        ]
        return "\n".join(lines)

    # -------------------------------------------------------------------------
    # Event handlers
    # -------------------------------------------------------------------------

    async def setup_hook(self):
        """Discord.py lifecycle hook — load cogs here."""
        try:
            await self.load_extension("mtg.cog")
            print("📦 Loaded MTGGameCog")
        except Exception as e:
            print(f"⚠️ Failed to load mtg.cog: {e}")
        await self.add_cog(MTGCog(self))
        await self.add_cog(UtilityCog(self))

    async def on_ready(self):
        print(f"Logged in as {self.user} (ID: {self.user.id})")
        print(f"MTG channel: {self.mtg_channel_id or 'any'}")
        if self.excluded_channels:
            print(f"Excluded channels: {self.excluded_channels}")
        print(f"🎮 Discord MTG Bot ready")

    async def on_message(self, message: discord.Message):
        # Ignore self
        if message.author == self.user:
            return
        # Ignore excluded channels entirely
        channel_id = message.channel.id
        if channel_id in self.excluded_channels:
            return

        # Always process commands first
        await self.process_commands(message)

        # If the message looked like a command, stop here (don't also chat-reply)
        if message.content.startswith("!"):
            return

        # Respond to: (a) mentions, (b) MTG channel, (c) bot-owned threads
        is_mentioned = self.user.mentioned_in(message)
        is_mtg_channel = (self.mtg_channel_id is not None and channel_id == self.mtg_channel_id)
        is_bot_thread = (
            isinstance(message.channel, discord.Thread)
            and message.channel.owner_id == self.user.id
        )
        if not (is_mentioned or is_mtg_channel or is_bot_thread):
            return

        await self._chat_reply(message)

    async def _chat_reply(self, message: discord.Message):
        """Send a chat reply using the persona + game-context system prompt."""
        text = (message.content or "").strip()
        if not text:
            return

        thread_id = message.channel.id
        history = self.conversations.setdefault(thread_id, [])
        history.append({"role": "user", "content": text})
        # Trim history
        if len(history) > CONFIG.max_messages_per_thread * 2:
            history[:] = history[-CONFIG.max_messages_per_thread * 2:]

        system_prompt = self.build_base_prompt()
        # If a game is active in this thread, append a brief game-context line
        game_context = self.get_game_context_for_channel(channel_id=thread_id)
        if game_context:
            system_prompt += "\n\n--- Current MTG Game ---\n" + game_context

        async with message.channel.typing():
            try:
                response = await asyncio.to_thread(
                    self.claude.messages.create,
                    model=CONFIG.model_default,
                    max_tokens=CONFIG.max_tokens,
                    system=system_prompt,
                    messages=history,
                )
                if hasattr(response, "usage"):
                    self._track_usage(response.usage.input_tokens,
                                      response.usage.output_tokens, bucket="chat")
                reply_text = "".join(
                    block.text for block in response.content if hasattr(block, "text")
                ).strip()
            except Exception as e:
                reply_text = f"⚠️ Couldn't generate a reply: {type(e).__name__}: {e}"

        history.append({"role": "assistant", "content": reply_text})
        await self.send_long_message(message.channel, reply_text)

    def get_game_context_for_channel(self, channel_id: int) -> str:
        """If an MTG game is active in this channel, return a brief
        description for the chat-system-prompt context. Empty string if no
        game.

        The MTG cog stores active games in `engine.games[thread_id]`.
        """
        mtg_cog = self.get_cog("MTG Game")
        if mtg_cog is None:
            return ""
        engine = getattr(mtg_cog, "engine", None)
        if engine is None:
            return ""
        game = getattr(engine, "games", {}).get(channel_id)
        if game is None:
            return ""
        try:
            players = " vs ".join(p.name for p in game.players)
            return (
                f"A {game.format} game is in progress: {players}. "
                f"Turn {game.turn_number}, phase {game.phase.value}. "
                f"Active player: {game.players[game.active_player_index].name}."
            )
        except Exception:
            return ""

    async def send_long_message(self, channel, content: str):
        """Send a string that may exceed Discord's 2000-char limit, splitting at
        line boundaries where possible."""
        if not content:
            return
        if len(content) <= 1900:
            await channel.send(content)
            return
        # Split at line boundaries, keeping each chunk under 1900 chars
        chunks: List[str] = []
        current = ""
        for line in content.split("\n"):
            if len(current) + len(line) + 1 > 1900 and current:
                chunks.append(current)
                current = line
            else:
                current = f"{current}\n{line}" if current else line
        if current:
            chunks.append(current)
        for chunk in chunks:
            await channel.send(chunk)


# =============================================================================
# CARD LOOKUP COG (!card, !random, !rulings, !price, !xmage)
# =============================================================================

class MTGCog(commands.Cog, name="MTG"):
    """Magic: The Gathering card lookup commands."""

    def __init__(self, bot: MTGBot):
        self.bot = bot

    @commands.command(name="card")
    async def lookup_card(self, ctx, *, card_name: str):
        """Look up an MTG card by name.

        Usage:
            !card Lightning Bolt
            !card Jace, the Mind Sculptor
        """
        async with ctx.typing():
            card = await self.bot.scryfall.search_card(card_name)
            if card:
                text, embed = self.bot.scryfall.format_card(card)
                if embed:
                    await ctx.send(text, embed=embed)
                else:
                    await ctx.send(text)
            else:
                await ctx.send(f"Couldn't find a card matching '{card_name}'")

    @commands.command(name="random")
    async def random_card(self, ctx, *, query: str = None):
        """Get a random MTG card, optionally filtered.

        Usage:
            !random              - Any random card
            !random c:red        - Random red card
            !random t:legendary  - Random legendary
            !random set:mh3      - Random from Modern Horizons 3
        """
        async with ctx.typing():
            card = await self.bot.scryfall.random_card(query)
            if card:
                text, embed = self.bot.scryfall.format_card(card)
                if embed:
                    await ctx.send(text, embed=embed)
                else:
                    await ctx.send(text)
            else:
                await ctx.send("Couldn't get a random card. Try a different filter?")

    @commands.command(name="rulings")
    async def card_rulings(self, ctx, *, card_name: str):
        """Get Wizards rulings for an MTG card."""
        async with ctx.typing():
            card = await self.bot.scryfall.search_card(card_name)
            if card and "rulings_uri" in card:
                async with aiohttp.ClientSession() as session:
                    async with session.get(card["rulings_uri"]) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            rulings = data.get("data", [])[:5]
                            if rulings:
                                lines = [f"**Rulings for {card['name']}:**\n"]
                                for r in rulings:
                                    lines.append(f"• {r['comment']}")
                                await self.bot.send_long_message(ctx.channel, "\n".join(lines))
                            else:
                                await ctx.send(f"No rulings found for {card['name']}")
                            return
            await ctx.send(f"Couldn't find rulings for '{card_name}'")

    @commands.command(name="price")
    async def card_price(self, ctx, *, card_name: str):
        """Get current price info for an MTG card."""
        async with ctx.typing():
            card = await self.bot.scryfall.search_card(card_name)
            if card:
                prices = card.get("prices", {})
                lines = [f"**{card['name']}** prices:"]
                if prices.get("usd"):
                    lines.append(f"• Normal: ${prices['usd']}")
                if prices.get("usd_foil"):
                    lines.append(f"• Foil: ${prices['usd_foil']}")
                if prices.get("usd_etched"):
                    lines.append(f"• Etched: ${prices['usd_etched']}")
                if len(lines) == 1:
                    lines.append("• No price data available")
                await ctx.send("\n".join(lines))
            else:
                await ctx.send(f"Couldn't find '{card_name}'")

    @commands.command(name="xmage")
    async def xmage_lookup(self, ctx, *, card_name: str):
        """Look up a card via the XMage rules engine (raw rules data).

        Shows authoritative data from XMage's card database. Use !card for
        pretty Scryfall embeds, !xmage for the rules-engine view.

        Usage:
            !xmage Lightning Bolt
            !xmage Humility
        """
        async with ctx.typing():
            try:
                # Reuse the persistent XMage bridge started by the MTG game cog
                # if it's running — saves a ~13s JAR cold start.
                mtg_cog = self.bot.get_cog("MTG Game")
                bridge = None
                if mtg_cog is not None:
                    engine = getattr(mtg_cog, "engine", None)
                    if engine is not None and getattr(engine, "_xmage_available", False):
                        bridge = getattr(engine, "xmage_bridge", None)
                if bridge is not None:
                    data = await bridge.lookup(card_name)
                else:
                    from rules.xmage_bridge import XMageBridge
                    async with XMageBridge() as ephemeral:
                        data = await ephemeral.lookup(card_name)

                if data is None:
                    await ctx.send(f"XMage doesn't have a card called '{card_name}'.")
                    return

                lines = [f"**{data.get('name', card_name)}** (XMage)"]
                if data.get("manaCost"):
                    lines.append(f"**Mana Cost:** {data['manaCost']}")
                if data.get("cmc"):
                    lines.append(f"**CMC:** {data['cmc']}")
                if data.get("types"):
                    lines.append(f"**Types:** {' '.join(data['types'])}")
                if data.get("supertypes"):
                    lines.append(f"**Supertypes:** {' '.join(data['supertypes'])}")
                if data.get("subtypes"):
                    lines.append(f"**Subtypes:** {' '.join(data['subtypes'])}")
                if data.get("colors"):
                    color_map = {"W": "White", "U": "Blue", "B": "Black", "R": "Red", "G": "Green"}
                    colors = [color_map.get(c, c) for c in data['colors']]
                    lines.append(f"**Colors:** {', '.join(colors)}")
                if data.get("text"):
                    lines.append(f"**Oracle Text:** {data['text']}")
                if data.get("power") and data.get("toughness"):
                    if data['power'] != "0" or data['toughness'] != "0":
                        lines.append(f"**P/T:** {data['power']}/{data['toughness']}")
                if data.get("keywords"):
                    lines.append(f"**Keywords:** {', '.join(data['keywords'])}")
                if data.get("abilities"):
                    lines.append(f"**Abilities ({len(data['abilities'])}):**")
                    for ab in data['abilities'][:5]:
                        lines.append(f"  • {ab.get('type', '?')}: {ab.get('rule', '?')[:100]}")

                await self.bot.send_long_message(ctx.channel, "\n".join(lines))
            except FileNotFoundError:
                await ctx.send("⚠️ XMage bridge JAR not found. Run `mvn package` in `rules/xmage-bridge/` first.")
            except Exception as e:
                await ctx.send(f"⚠️ XMage lookup failed: {type(e).__name__}: {e}")


# =============================================================================
# UTILITY COG (!cost, !clear, !context, !about)
# =============================================================================

class UtilityCog(commands.Cog, name="Utility"):
    """Bot utility commands."""

    def __init__(self, bot: MTGBot):
        self.bot = bot

    @commands.command(name="cost")
    async def show_cost(self, ctx):
        """Show lifetime API usage and cost summary."""
        await ctx.send(self.bot.get_cost_summary())

    @commands.command(name="clear")
    async def clear_history(self, ctx):
        """Clear this thread's chat conversation history (doesn't touch MTG game state).

        Useful if the bot has been chatting for a while and you want a fresh
        start without losing your game.
        """
        thread_id = ctx.channel.id
        if thread_id in self.bot.conversations:
            self.bot.conversations[thread_id].clear()
            await ctx.send("🧹 Chat history cleared. (Active MTG game state is unaffected.)")
        else:
            await ctx.send("No chat history to clear here.")

    @commands.command(name="context")
    async def show_context(self, ctx):
        """Show what game (if any) the bot has in context for this thread."""
        game_context = self.bot.get_game_context_for_channel(channel_id=ctx.channel.id)
        if game_context:
            await ctx.send(f"📋 **Game in this thread:**\n{game_context}")
        else:
            await ctx.send("No active MTG game in this thread.")

    @commands.command(name="about")
    async def about_bot(self, ctx):
        """Show what this bot is and links to docs."""
        lines = [
            f"**{self.bot.persona.get('name', 'Discord MTG Bot')}** — a Discord bot that plays Magic: The Gathering.",
            "",
            "Capabilities:",
            "• Full MTG game engine — Commander / Modern / Legacy / Vintage / Pioneer / Pauper / Limited / Brawl / Oathbreaker / Cube",
            "• Tiered effect resolution (templates → patterns → XMage bridge → LLM judge)",
            "• Card lookups (`!card`, `!xmage`, `!random`, `!rulings`, `!price`)",
            "• `!undo` snapshot stack for bug recovery",
            "• `!coverage` to see how the engine will handle each card in a deck",
            "",
            "Source: <https://github.com/VIXAL-OS/discord-mtg-bot>",
        ]
        await ctx.send("\n".join(lines))


# =============================================================================
# MAIN
# =============================================================================

def main():
    if not os.getenv("DISCORD_TOKEN"):
        print("Error: DISCORD_TOKEN not set in environment (see .env.example)")
        return
    if not os.getenv("ANTHROPIC_API_KEY"):
        print("Error: ANTHROPIC_API_KEY not set in environment (see .env.example)")
        return

    bot = MTGBot()
    bot.run(os.getenv("DISCORD_TOKEN"))


if __name__ == "__main__":
    main()
