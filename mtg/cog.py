"""MTGGameCog — Discord command handlers for the MTG engine.

The Discord-facing layer. Every `!play`, `!attack`, `!state`, `!autoplay`,
`!resolve`, `!judge`, `!fix`, etc. command lives here as a method on
MTGGameCog. The cog delegates game logic to GameEngine + RulesEngine, so
this file is mostly "parse Discord input → call engine method → format
Discord response."

Phase 2 of the OSS refactor would split this further into commands per
concern (gameplay.py, deck_management.py, autoplay_commands.py, debug.py),
but for now MTGGameCog stays as one large coherent class.

The async setup(bot) function at the bottom is the entry point that
discord.py calls when the extension is loaded via bot.load_extension().
mtg_game.py re-exports it for backward compatibility with the existing
bot.load_extension('mtg_game') call site.

Extracted from mtg_game.py during the Phase 1 OSS-readability refactor.
"""

import asyncio
import json
import logging
import os
import random
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import aiohttp
import anthropic
import discord
from discord import ui
from discord.ext import commands

from mtg.autoplay import AUTOPLAY_DECKS, AUTOPLAY_MATRIX, AUTOPLAY_PHASES
from mtg.claude_player import ClaudePlayer
from mtg.constants import (
    Phase, Zone, PHASE_NAMES, FORMAT_STARTING_LIFE, FORMAT_DECK_SIZE,
    SINGLETON_FORMATS, COMMAND_ZONE_FORMATS, BANNED_CARDS,
)
from mtg.deck_loader import DeckLoader
from mtg.display import GameDisplay
from mtg.engine import GameEngine
from mtg.helpers import _normalize_pw_ability_idx, _resolve_player_or_card_target
from mtg.models import Card, Player, GameState, FormatValidator
from mtg.rules_engine import RulesEngine
from mtg.util import GameLogger, StdoutTee, StderrTee

# Optional: visual board renderer
try:
    from board_visual import render_game_board, render_player_hand
    HAS_BOARD_VISUAL = True
except ImportError:
    HAS_BOARD_VISUAL = False

# Optional: spell resolver engine
try:
    from rules import SpellResolver, ExecutionContext
    HAS_SPELL_RESOLVER = True
except ImportError:
    HAS_SPELL_RESOLVER = False

# Optional: card-specific effect templates
try:
    from rules.effect_templates import get_effect_library, build_game_context
    HAS_EFFECT_TEMPLATES = True
except ImportError:
    HAS_EFFECT_TEMPLATES = False

# Optional: pre-cast target legality
try:
    from rules.targeting_helpers import (
        _validate_target_for_action,
        _validate_player_target_for_action,
    )
    HAS_TARGETING = True
except ImportError:
    HAS_TARGETING = False

# Optional: XMage bridge (used in some debug commands)
try:
    from rules.xmage_bridge import XMageBridge
    HAS_XMAGE_BRIDGE = True
except ImportError:
    HAS_XMAGE_BRIDGE = False

# Optional: planeswalker target legality (for ability targeting in cog handlers)
try:
    from rules.planeswalker import get_legal_planeswalker_targets
    HAS_PLANESWALKER = True
except ImportError:
    HAS_PLANESWALKER = False
    get_legal_planeswalker_targets = None

# Optional: LLM adapters for cost-efficient autoplay (Phase 3 multi-model split)
# MTGGameCog.__init__ uses create_deepseek_adapter + create_deepseek_reasoner_adapter
# to wire up the actor + strategist for autoplay. Set to None if unavailable so
# the `if create_X_adapter else None` guard in __init__ falls through gracefully.
try:
    from rules.llm_adapter import (
        create_deepseek_adapter,
        create_openrouter_adapter,
        create_deepseek_reasoner_adapter,
    )
except ImportError:
    create_deepseek_adapter = None
    create_openrouter_adapter = None
    create_deepseek_reasoner_adapter = None


# =============================================================================
# DISCORD COG
# =============================================================================

class MTGGameCog(commands.Cog, name="MTG Game"):
    """MTG Game commands with enhanced rules integration."""

    # Re-bind module-level autoplay constants (defined in mtg/autoplay.py)
    # as class attributes so the !autoplay-batch / !autoplay-parallel /
    # !autoplay-resume command handlers can keep accessing them via
    # `self.AUTOPLAY_DECKS` etc. — the same form they had before the
    # Phase 2F-cog extraction. The data lives in mtg/autoplay.py (single
    # source of truth); these bindings are just aliases.
    AUTOPLAY_DECKS = AUTOPLAY_DECKS
    AUTOPLAY_MATRIX = AUTOPLAY_MATRIX
    AUTOPLAY_PHASES = AUTOPLAY_PHASES

    def __init__(self, bot):
        self.bot = bot
        # Get usage callback from bot if available
        usage_callback = getattr(bot, 'track_mtg_usage', None)
        self.engine = GameEngine(bot.claude, usage_callback=usage_callback)
        self.display = GameDisplay()
        self.player_decks: Dict[int, Dict] = {}  # user_id -> deck_data

        # Per-game file logging
        self.game_loggers: Dict[int, GameLogger] = {}
        self._stdout_tee: Optional[StdoutTee] = None
        self._stderr_tee: Optional[StderrTee] = None
        self._stderr_log_handler: Optional[logging.Handler] = None

        # Batch autoplay state
        self._batch_running = False
        self._batch_stop_flag = False

        # Deepseek adapter for cost-efficient autoplay (~12x cheaper than Claude)
        # Reads DEEPSEEK_API_KEY from env; None if not configured (falls back to Claude)
        self._deepseek_adapter = create_deepseek_adapter() if create_deepseek_adapter else None

        # Phase 3 parallel CoT: separate strategist adapter (V4-Pro for deep reasoning).
        # Actor (plan_turn) stays on deepseek-v4-flash with thinking disabled (fast JSON);
        # Strategist (_update_strategy) uses deepseek-v4-pro with reasoning_effort=high
        # for deeper per-turn reasoning. Same DEEPSEEK_API_KEY for both.
        # (Function name is `_reasoner_adapter` for backward compat — see llm_adapter.py.)
        self._deepseek_reasoner_adapter = (
            create_deepseek_reasoner_adapter() if create_deepseek_reasoner_adapter else None
        )
        if self._deepseek_reasoner_adapter:
            print("[DEEPSEEK] Phase 3 split active: actor=deepseek-v4-flash, strategist=deepseek-v4-pro")

        # Wrap engine.delete_game so logging cleanup happens automatically
        _original_delete = self.engine.delete_game
        def _delete_with_logging(thread_id):
            self._cleanup_game_logging(thread_id)
            _original_delete(thread_id)
        self.engine.delete_game = _delete_with_logging

        # Set up integrated engine for enhanced features
        self._setup_integrated_engine()

        # Try to auto-load the default deck
        self._load_default_deck()
    
    def _setup_integrated_engine(self):
        """Set up the integrated engine with priority system.

        Note: mtg_enhanced_integration.py is deprecated (Apr 2026).
        Integration is now done directly in GameEngine. This method
        is kept for backward compatibility but no longer loads the module.
        """
        self.integrated = None
    
    async def cog_load(self):
        """Called when the cog is loaded - start async components."""
        # Install stdout tee so engine print() calls get routed to game logs
        self._stdout_tee = StdoutTee(sys.stdout)
        sys.stdout = self._stdout_tee

        # Install stderr tee so Python tracebacks + discord.py logging warnings
        # (heartbeat blocks, gateway reconnects) get captured alongside stdout.
        # Fallback path catches warnings fired from discord.py's own task that
        # doesn't inherit the per-game contextvar.
        logs_dir = Path("logs")
        logs_dir.mkdir(exist_ok=True)
        self._stderr_tee = StderrTee(sys.stderr, self._stdout_tee, logs_dir / "stderr.log")
        sys.stderr = self._stderr_tee

        # Attach a logging handler on the root logger pointing at the teed
        # stderr. discord.py's gateway heartbeat warnings go through the
        # logging module (not print()), so without this handler they never
        # reach the tee. Level WARNING covers heartbeat blocks + reconnects
        # without spamming on every INFO-level event.
        self._stderr_log_handler = logging.StreamHandler(sys.stderr)
        self._stderr_log_handler.setLevel(logging.WARNING)
        self._stderr_log_handler.setFormatter(
            logging.Formatter('[%(asctime)s] [%(name)s %(levelname)s] %(message)s',
                              datefmt='%H:%M:%S')
        )
        root_logger = logging.getLogger()
        # Ensure root logger will actually forward WARNING records to handlers
        if root_logger.level == logging.NOTSET or root_logger.level > logging.WARNING:
            root_logger.setLevel(logging.WARNING)
        root_logger.addHandler(self._stderr_log_handler)

        # `self.integrated` has been hardwired to None since the Apr 2026
        # integrated-engine deprecation, so this branch never executes.
        # The corresponding `_on_message_priority` listener was deleted in the
        # May 17 deprecation-rot pass. Kept the conditional as a no-op marker
        # in case someone re-introduces an integrated engine later.

        # Reuse XMage bridge from integrated engine (already started above)
        # instead of spawning a second Java subprocess.
        #
        # Apr 30 audit: capture the bridge state and (if it failed) the reason,
        # then stash both on the engine so per-game logs can emit a definitive
        # [XMAGE-INIT] line. Without this, "XMage didn't fire" is invisible
        # in per-game audits — you can't tell if it was a Java-not-installed
        # error, a JAR-build mismatch, or an init bug.
        self.engine._xmage_init_reason = ""
        if not HAS_XMAGE_BRIDGE:
            self.engine._xmage_init_reason = "HAS_XMAGE_BRIDGE=False (rules.xmage_bridge import failed at module load)"
        elif self.integrated and self.integrated.enhanced:
            # Legacy path: integrated engine ran — reuse its bridge.
            effects_mgr = self.integrated.enhanced.effects
            if not effects_mgr.xmage:
                self.engine._xmage_init_reason = "CardEffectsManager.xmage is None (HybridRulesEngine wasn't constructed)"
            elif effects_mgr.xmage._xmage_available:
                self.engine.xmage_bridge = effects_mgr.xmage._xmage
                self.engine._xmage_available = True
                print("[XMAGE] Bridge available (shared from integrated engine)")
            else:
                err = effects_mgr.xmage._last_init_error or "unknown (no exception captured)"
                self.engine._xmage_init_reason = f"HybridRulesEngine.start failed: {err}"
                print(f"[XMAGE] Integrated engine bridge not available — running without XMage ({err})")
        else:
            # May 14 audit: the integrated engine was deprecated (Apr 2026) and
            # self.integrated is now always None, so the XMage bridge was
            # permanently inactive across the May 14 batch. Try a direct
            # spawn here as a fallback. Failure stays graceful — the engine
            # works without XMage, just at slightly higher Tier 3 cost.
            try:
                from rules.xmage_bridge import XMageBridge
                bridge = XMageBridge()
                await bridge.start()
                self.engine.xmage_bridge = bridge
                self.engine._xmage_available = True
                print("[XMAGE] Bridge spawned directly (no integrated-engine wrapper)")
            except Exception as e:
                self.engine._xmage_init_reason = f"direct-spawn failed: {type(e).__name__}: {e}"
                print(f"[XMAGE] Direct spawn failed: {e} — Tier 2.5 inactive")

    async def cog_unload(self):
        """Called when the cog is unloaded - clean up."""
        # Detach logging handler first — it holds a reference to the tee.
        if self._stderr_log_handler:
            logging.getLogger().removeHandler(self._stderr_log_handler)
            self._stderr_log_handler = None

        # Restore original stderr before stdout (no ordering dependency but
        # matches install order reversed).
        if self._stderr_tee:
            sys.stderr = self._stderr_tee.original
            self._stderr_tee = None

        # Restore original stdout
        if self._stdout_tee:
            sys.stdout = self._stdout_tee.original
            self._stdout_tee = None

        # See cog_load: self.integrated has been None since Apr 2026, so this
        # branch is dead. The `_on_message_priority` listener was also deleted.

        # May 14 audit: when the integrated engine was deprecated the bridge
        # lifecycle moved to direct-spawn in cog_load, but the unload path
        # still assumed "integrated engine owns lifecycle" — which would
        # leave the Java subprocess running on cog reload. Stop the bridge
        # explicitly when we spawned it ourselves. Has-attr guarded for
        # extra safety (bridge may have failed to spawn).
        if not self.integrated and getattr(self.engine, 'xmage_bridge', None):
            try:
                await self.engine.xmage_bridge.stop()
            except Exception as e:
                print(f"[XMAGE] Bridge stop failed (continuing): {e}")
        self.engine.xmage_bridge = None
        self.engine._xmage_available = False

    async def cog_before_invoke(self, ctx):
        """Set active thread for stdout tee + patch ctx.send for Discord logging."""
        thread_id = getattr(ctx.channel, 'id', None)

        # Route engine print() output to this game's console log
        if self._stdout_tee and thread_id in self.game_loggers:
            self._stdout_tee.active_thread = thread_id

        # Patch ctx.send to log outgoing Discord messages
        logger = self.game_loggers.get(thread_id)
        if logger:
            original_send = ctx.send
            async def logged_send(content=None, **kwargs):
                if content:
                    logger.log_discord_out(str(content))
                return await original_send(content, **kwargs)
            ctx.send = logged_send

    async def cog_after_invoke(self, ctx):
        """Clear active thread after command finishes."""
        if self._stdout_tee:
            self._stdout_tee.active_thread = None

    # _on_message_priority listener removed May 17, 2026.
    # The body was gated by `self.integrated` which is hardwired to None since
    # the Apr 2026 integrated-engine deprecation, and the listener was never
    # actually registered (the add_listener call was gated by the same flag).
    # Both branches were dead code per the May 17 deprecation-rot audit.

    def _load_default_deck(self):
        """Load the default deck if it exists."""
        import os
        deck_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "surrak_stompy.json")
        if os.path.exists(deck_path):
            try:
                with open(deck_path, 'r', encoding='utf-8') as f:
                    self.engine.claude_deck = json.load(f)
                print(f"✅ Loaded the default deck: {self.engine.claude_deck.get('name', 'Unknown')}")
            except Exception as e:
                print(f"⚠️ Could not load default deck: {e}")
    
    async def _thread_send(self, thread, content=None, **kwargs):
        """Send a message to a thread and log it to the game's Discord log."""
        thread_id = getattr(thread, 'id', None)
        if thread_id and content:
            logger = self.game_loggers.get(thread_id)
            if logger:
                logger.log_discord_out(str(content))
        return await thread.send(content, **kwargs)

    @staticmethod
    def _sanitize_action_bullets(actions):
        """Strip engine-internal debug strings from player-facing action bullets.

        The `actions_taken` list gathers messages from many sources (human
        path `_execute_action`, combat/block/trigger messages, SBA messages).
        Defense-in-depth: any bracketed log tag or raw Python exception
        fragment that slips through gets filtered here before it reaches
        Discord.
        """
        if not actions:
            return actions
        debug_prefixes = (
            "[PLAN-VALIDATE]", "[EXECUTE]", "[RESOLVE]", "[DEBUG]",
            "[ETB-", "[TRIGGER-", "[XMAGE", "[SPELL_RESOLVER]",
            "[SEMANTIC]", "[ACCUMULATOR]", "[OPP-CAST-TRIGGER]",
            "[LANDFALL]", "[COMBAT]", "[DAMAGE-PREVENTED]",
            "[AUTO-DRAFT]", "[DRAFT-CLAUDE]", "[AI-RESOLVE]",
            "[AUTOPLAY-JUDGE]", "[AUTOPLAY]", "[STRIP]", "[JUDGE-FIX]",
            # May 7 audit fix #1: filter the success sentinel that the
            # early-cast-announcement path returns from _execute_action.
            "[EARLY-CAST]",
        )
        exception_markers = (
            "Traceback (most recent", " at 0x",
            "KeyError:", "AttributeError:", "TypeError:", "ValueError:",
            "IndexError:", "NameError:", "RuntimeError:", "AssertionError:",
        )
        cleaned = []
        for a in actions:
            if not a:
                continue
            s = str(a).strip()
            if not s:
                continue
            if any(s.startswith(p) for p in debug_prefixes):
                continue
            if any(m in s for m in exception_markers):
                continue
            cleaned.append(a)
        return cleaned

    def _cleanup_game_logging(self, thread_id: int):
        """Remove game logger and unregister from stdout tee."""
        logger = self.game_loggers.pop(thread_id, None)
        if logger:
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            logger._write_console(f"=== Game {thread_id} ended at {timestamp} ===")
            logger._write_discord(f"=== Game {thread_id} ended at {timestamp} ===")
        if self._stdout_tee:
            self._stdout_tee.remove_game(thread_id)

    def _mtg_channel_id(self, ctx) -> Optional[int]:
        """The designated MTG channel for THIS guild, or None if unrestricted.

        July 26, 2026: resolves per-guild. Falls back to the old scalar
        attribute so a host bot that hasn't adopted the mapping still works.
        """
        resolver = getattr(self.bot, 'mtg_channel_for', None)
        if callable(resolver):
            guild = getattr(ctx, 'guild', None)
            return resolver(guild.id if guild else None)
        return getattr(self.bot, 'mtg_channel_id', None)

    def _is_mtg_channel(self, ctx) -> bool:
        """Check if we're in the designated MTG channel (or its threads)."""
        mtg_channel_id = self._mtg_channel_id(ctx)
        if not mtg_channel_id:
            return True  # No restriction configured in this guild

        channel_id = ctx.channel.id
        parent_id = getattr(ctx.channel, 'parent_id', None)
        return channel_id == mtg_channel_id or parent_id == mtg_channel_id
    
    async def cog_check(self, ctx) -> bool:
        """Check that MTG commands are used in the right channel."""
        # Always allow help command to see these commands
        if ctx.invoked_with == "help":
            return True
        
        if self._is_mtg_channel(ctx):
            return True
        
        # Only warn once per message (prevents spam during !help)
        warned_key = f"mtg_warn_{ctx.message.id}"
        if not hasattr(self.bot, '_mtg_warnings'):
            self.bot._mtg_warnings = set()
        
        if warned_key not in self.bot._mtg_warnings:
            self.bot._mtg_warnings.add(warned_key)
            # Clean up old warnings (keep last 100)
            if len(self.bot._mtg_warnings) > 100:
                self.bot._mtg_warnings = set(list(self.bot._mtg_warnings)[-50:])
            
            _cid = self._mtg_channel_id(ctx)
            mtg_channel = self.bot.get_channel(_cid) if _cid else None
            channel_mention = (mtg_channel.mention if mtg_channel
                               else (f"<#{_cid}>" if _cid else "the MTG channel"))
            await ctx.send(f"*ear flick* MTG games are only available in {channel_mention}!")
        
        return False
    
    @commands.command(name="game")
    async def start_game(self, ctx, opponent: str = None, format: str = "standard"):
        """
        Start an MTG game.
        
        Usage:
            !game @Friend commander  - Play against another player
            !game claude modern       - Play against Claude
        """
        if not opponent:
            await ctx.send("Usage: `!game @opponent [format]` or `!game claude [format]`")
            return
        
        # Determine opponent
        opponent_name = opponent.lower()
        opponent_id = None
        is_claude_opponent = opponent_name in ["claude", "bot", "ai"]
        
        if not is_claude_opponent:
            # Try to parse mention
            if ctx.message.mentions:
                opponent_user = ctx.message.mentions[0]
                opponent_name = opponent_user.display_name
                opponent_id = opponent_user.id
            else:
                await ctx.send("Please mention your opponent or use `claude` to play against me!")
                return
        
        if is_claude_opponent and not self.engine.claude_deck:
            await ctx.send("I don't have a deck loaded! Use `!deck <archidekt_url>` or upload a deck JSON first.")
            return
        
        # Create game thread
        thread = await ctx.channel.create_thread(
            name=f"MTG: {ctx.author.display_name} vs {opponent_name}",
            type=discord.ChannelType.public_thread
        )
        
        # Get player decks
        p1_deck = self.player_decks.get(ctx.author.id)
        p2_deck = self.player_decks.get(opponent_id) if opponent_id else None
        
        # Create game
        game = await self.engine.create_game(
            thread_id=thread.id,
            player1_name=ctx.author.display_name,
            player1_id=ctx.author.id,
            player2_name=opponent_name if is_claude_opponent else opponent_name,
            player2_id=opponent_id,
            format=format,
            player1_deck=p1_deck,
            player2_deck=p2_deck
        )

        # Set up per-game file logging
        game_logger = GameLogger(thread.id)
        self.game_loggers[thread.id] = game_logger
        if self._stdout_tee:
            self._stdout_tee.add_game(thread.id, game_logger.console_path)
        print(f"[GAME-LOG] Logging to {game_logger.console_path} and {game_logger.discord_path}")

        # Start game
        first_player = random.randint(0, 1)
        self.engine.start_game(game, first_player)

        # Claude mulligan evaluation
        claude_player = game.players[0] if game.players[0].is_claude else game.players[1] if game.players[1].is_claude else None
        if claude_player:
            claude_mulligans = 0
            max_mulligans = 3  # Don't mulligan to oblivion
            
            while claude_mulligans < max_mulligans:
                should_mull = await self.engine.claude_ai.decide_mulligan(claude_player.hand, claude_mulligans)
                if not should_mull:
                    break
                
                # Mulligan: shuffle hand back, draw 7
                claude_mulligans += 1
                claude_player.library.extend(claude_player.hand)
                claude_player.hand.clear()
                random.shuffle(claude_player.library)
                self.engine.draw_cards(claude_player, 7)
            
            # Put cards on bottom equal to mulligans taken
            if claude_mulligans > 0:
                # Simple strategy: put highest cost cards on bottom
                claude_player.hand.sort(key=lambda c: int(c.cmc) if isinstance(c.cmc, (int, float)) else 0, reverse=True)
                for _ in range(claude_mulligans):
                    if claude_player.hand:
                        bottomed = claude_player.hand.pop(0)  # Remove highest cost
                        claude_player.library.append(bottomed)
                
                await self._thread_send(thread, f"🔄 Claude mulliganed {claude_mulligans}x and keeps {len(claude_player.hand)} cards.")
            
            claude_player.mulligans_taken = claude_mulligans
            claude_player.has_kept_hand = True
        
        # Send initial state
        embed = self.display.create_game_embed(game)
        await self._thread_send(thread,
            f"**Game started!** {game.players[first_player].name} goes first.\n"
            f"Use `!hand` to see your cards (I'll DM you).\n"
            f"Use `!state` to see the board.",
            embed=embed
        )
        
        # Set up priority system for this game
        if self.integrated:
            async def on_priority_change(player_name):
                await self._thread_send(thread, f"⏳ **{player_name}** has priority")
            
            self.integrated.setup_priority(
                thread.id, game,
                on_priority_change=on_priority_change
            )
        
        # If Claude goes first, take turn
        if game.players[first_player].is_claude:
            try:
                await asyncio.sleep(1)
                print("[DEBUG] Claude going first, executing turn...")
                actions = await self.engine.execute_claude_turn(game)
                print(f"[DEBUG] Claude took {len(actions)} actions: {actions}")
                actions = self._sanitize_action_bullets(actions)
                if actions:
                    msg = "**Claude's turn:**\n" + "\n".join(f"• {a}" for a in actions)
                    # Discord has 2000 char limit
                    if len(msg) > 1900:
                        await self._thread_send(thread, "**Claude's turn:**")
                        for action in actions:
                            await self._thread_send(thread, f"• {action[:1900]}")
                    else:
                        await self._thread_send(thread, msg)
                else:
                    if hasattr(self.engine.claude_ai, 'last_error') and self.engine.claude_ai.last_error:
                        await self._thread_send(thread, f"⚠️ Claude had trouble deciding. Passing.\n`{self.engine.claude_ai.last_error[:100]}`")
                        self.engine.claude_ai.last_error = None
                    else:
                        await self._thread_send(thread, "*Claude thinks, then passes.*")
                await self._thread_send(thread, embed=self.display.create_game_embed(game))

                # Check if combat is paused for human blocks
                if game.waiting_for_human_blocks:
                    await self._thread_send(thread, f"🛡️ Declare blockers with `!block <attacker> with <blocker>`, then `!doneblocking` (or `!noblock` for no blocks)")
                    self.engine.save_game(game)
                    return  # Human blocks, then _resolve_combat continues the turn

                # End Claude's turn and pass to human
                print("[DEBUG] Ending Claude's turn, passing to human...")
                self.engine.end_turn(game)
                # Advance human through beginning phases to Main Phase 1
                _, _p1 = self.engine.advance_phase(game)  # UNTAP → UPKEEP
                _, _p2 = self.engine.advance_phase(game)  # UPKEEP → DRAW (upkeep triggers here)
                _, _p3 = self.engine.advance_phase(game)  # DRAW → MAIN1
                for _m in _p1 + _p2 + _p3:
                    await self._thread_send(thread, _m)
                # Drain sync-queued triggers via Tier 3 (upkeep/end-step/dies)
                for _m in await self.engine.drain_pending_triggers(game):
                    await self._thread_send(thread, _m)
                await self._thread_send(thread, f"🔄 Turn {game.turn_number} - **{game.active_player.name}**'s turn!")
                await self._thread_send(thread, embed=self.display.create_game_embed(game))
                print("[DEBUG] Turn handoff complete")
            except Exception as e:
                print(f"[ERROR] Claude turn failed: {e}")
                import traceback
                traceback.print_exc()
                await self._thread_send(thread, f"⚠️ Something went wrong during Claude's turn: {str(e)[:100]}")
    
    @commands.command(name="resumegame")
    async def resume_game(self, ctx):
        """
        Force-load a stale game save for this thread.

        If a game crashed or the bot restarted after 24+ hours,
        the auto-loader skips the save file. This command forces it
        back into memory so you can keep playing.

        Usage:
            !resumegame   (run in the game thread)
        """
        thread_id = ctx.channel.id
        # Check if there's already an active game
        if thread_id in self.engine.games:
            await ctx.send("✅ There's already an active game in this thread!")
            return

        filepath = os.path.join(self.engine.GAMES_DIR, f"{thread_id}.json")
        if not os.path.exists(filepath):
            await ctx.send("❌ No saved game found for this thread.")
            return

        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                data = json.load(f)
            game = GameState.from_dict(data)

            if game.ended:
                await ctx.send("❌ That game already ended. Start a new one with `!game`.")
                return

            # Touch the file so it won't be skipped on next restart
            os.utime(filepath, None)

            self.engine.games[thread_id] = game
            file_age_hours = (datetime.now().timestamp() - os.path.getmtime(filepath)) / 3600
            print(f"[GAME-LOAD] Force-resumed game {thread_id} (turn {game.turn_number})")

            await ctx.send(
                f"✅ Game resumed! Turn {game.turn_number} — **{game.active_player.name}**'s turn.\n"
                f"Use `!board` to see the current state."
            )
            await ctx.send(embed=self.display.create_game_embed(game))
            self.engine.save_game(game)
        except Exception as e:
            print(f"[GAME-LOAD] Failed to resume game {thread_id}: {e}")
            import traceback
            traceback.print_exc()
            await ctx.send(f"❌ Failed to load game: {str(e)[:200]}")

    @commands.command(name="deck")
    async def load_deck(self, ctx, source: str = None):
        """
        Load a deck for Claude to use.

        Usage:
            !deck https://archidekt.com/decks/123456  - Load from Archidekt
            !deck surrak                               - Load from data/ folder
            !deck                                      - Upload a JSON file
        """
        # Check for attachment
        if ctx.message.attachments:
            attachment = ctx.message.attachments[0]
            if attachment.filename.endswith('.json'):
                content = await attachment.read()
                deck_data = json.loads(content.decode('utf-8'))
                self.engine.claude_deck = deck_data
                total_cards = sum(c.get('quantity', 1) for c in deck_data.get('cards', []))
                await ctx.send(f"✅ Loaded deck: **{deck_data.get('name', 'Unknown')}** ({total_cards} cards)")
                return
        
        if source:
            # Parse Archidekt URL
            match = re.search(r'archidekt\.com/decks/(\d+)', source)
            if match:
                deck_id = match.group(1)
                async with ctx.typing():
                    try:
                        cards, name, commander, sig_spell = await self.engine.deck_loader.load_from_archidekt(deck_id)
                        # Store as JSON format for consistency
                        deck_dict = {
                            "name": name,
                            "commander": commander.name if commander else None,
                            "cards": [{"name": c.name, "quantity": 1} for c in cards]
                        }
                        if sig_spell:
                            deck_dict["signature_spell"] = sig_spell.name
                        self.engine.claude_deck = deck_dict
                        await ctx.send(f"✅ Loaded deck from Archidekt: **{name}** ({len(cards)} cards)")
                    except Exception as e:
                        await ctx.send(f"❌ Failed to load deck: {e}")
                return
            
            # Try to load from local data folder
            import os
            deck_name = source.replace('.json', '')  # Strip .json if provided
            deck_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", f"{deck_name}.json")
            if os.path.exists(deck_path):
                try:
                    with open(deck_path, 'r', encoding='utf-8') as f:
                        deck_data = json.load(f)
                    self.engine.claude_deck = deck_data
                    total_cards = sum(c.get('quantity', 1) for c in deck_data.get('cards', []))
                    await ctx.send(f"✅ Loaded deck: **{deck_data.get('name', 'Unknown')}** ({total_cards} cards)")
                    return
                except Exception as e:
                    await ctx.send(f"❌ Failed to load local deck: {e}")
                    return
        
        await ctx.send("Usage: `!deck <archidekt_url>`, `!deck <name>` (from data/), or attach a JSON file")
    
    @commands.command(name="mydeck")
    async def load_my_deck(self, ctx, source: str = None):
        """
        Load a deck for yourself (human player).
        
        Usage:
            !mydeck https://archidekt.com/decks/123456  - Load from Archidekt
            !mydeck surrak                               - Load from data/ folder
            !mydeck                                       - Upload a JSON file
        """
        user_id = ctx.author.id
        
        # Check for attachment
        if ctx.message.attachments:
            attachment = ctx.message.attachments[0]
            if attachment.filename.endswith('.json'):
                content = await attachment.read()
                deck_data = json.loads(content.decode('utf-8'))
                self.player_decks[user_id] = deck_data
                total_cards = sum(c.get('quantity', 1) for c in deck_data.get('cards', []))
                await ctx.send(f"✅ Loaded YOUR deck: **{deck_data.get('name', 'Unknown')}** ({total_cards} cards)")
                return
        
        if source:
            # Parse Archidekt URL
            match = re.search(r'archidekt\.com/decks/(\d+)', source)
            if match:
                deck_id = match.group(1)
                async with ctx.typing():
                    try:
                        cards, name, commander, sig_spell = await self.engine.deck_loader.load_from_archidekt(deck_id)
                        deck_dict = {
                            "name": name,
                            "commander": commander.name if commander else None,
                            "cards": [{"name": c.name, "quantity": 1} for c in cards]
                        }
                        if sig_spell:
                            deck_dict["signature_spell"] = sig_spell.name
                        self.player_decks[user_id] = deck_dict
                        await ctx.send(f"✅ Loaded YOUR deck from Archidekt: **{name}** ({len(cards)} cards)")
                    except Exception as e:
                        await ctx.send(f"❌ Failed to load deck: {e}")
                return
            
            # Try to load from local data folder
            import os
            deck_name = source.replace('.json', '')
            deck_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", f"{deck_name}.json")
            if os.path.exists(deck_path):
                try:
                    with open(deck_path, 'r', encoding='utf-8') as f:
                        deck_data = json.load(f)
                    self.player_decks[user_id] = deck_data
                    total_cards = sum(c.get('quantity', 1) for c in deck_data.get('cards', []))
                    await ctx.send(f"✅ Loaded YOUR deck: **{deck_data.get('name', 'Unknown')}** ({total_cards} cards)")
                    return
                except Exception as e:
                    await ctx.send(f"❌ Failed to load local deck: {e}")
                    return
        
        await ctx.send("Usage: `!mydeck <archidekt_url>`, `!mydeck <name>` (from data/), or attach a JSON file")
    
    @commands.command(name="validate", aliases=["check", "deckcheck"])
    async def validate_deck(self, ctx, format_name: str = "commander"):
        """
        Validate your deck against format rules.
        
        Usage:
            !validate commander  - Check against Commander rules
            !validate modern     - Check against Modern rules
            !validate            - Defaults to Commander
        """
        user_id = ctx.author.id
        
        if user_id not in self.player_decks:
            await ctx.send("You haven't loaded a deck! Use `!mydeck` first.")
            return
        
        deck_data = self.player_decks[user_id]
        format_name = format_name.lower()
        
        # Load cards for validation
        cards = []
        commander = None
        commander_name = deck_data.get("commander")
        
        for card_entry in deck_data.get("cards", []):
            name = card_entry.get("name") if isinstance(card_entry, dict) else card_entry
            qty = card_entry.get("quantity", 1) if isinstance(card_entry, dict) else 1
            
            # Create card with basic info
            for _ in range(qty):
                card = Card(
                    name=name,
                    mana_cost=card_entry.get("mana_cost", "") if isinstance(card_entry, dict) else "",
                    type_line=card_entry.get("type_line", "") if isinstance(card_entry, dict) else "",
                    oracle_text=card_entry.get("oracle_text", "") if isinstance(card_entry, dict) else "",
                )
                card.color_identity = FormatValidator.get_color_identity(card)
                cards.append(card)
                
                if commander_name and name.lower() == commander_name.lower():
                    commander = card
        
        # Validate
        is_valid, issues = FormatValidator.validate_deck(cards, format_name, commander)
        
        # Format response
        msg = FormatValidator.format_validation_message(issues, format_name)
        
        # Add deck summary
        msg += f"\n\n**Deck Summary:** {len(cards)} cards"
        if commander:
            msg += f" | Commander: {commander.name}"

        await ctx.send(msg)

    @commands.command(name="coverage", aliases=["tier", "cov"])
    async def coverage_report(self, ctx, *, deck_name: str = ""):
        """
        Show the per-tier coverage for a deck.

        Reports how the engine will handle each card's effects:
          ✅ template — fast, card-specific (Tier 1.5)
          ✅ pattern  — fast, oracle-text regex (Tier 1.5)
          ⚠️ tier3    — slower, uses Claude API (~2s, costs tokens)

        Usage:
            !coverage              - Your loaded deck (via !mydeck)
            !coverage mythic       - An autoplay deck by short name
            !coverage surrak       - Any autoplay deck name
        """
        from mtg.coverage import classify_deck, format_coverage_report

        cards = []
        report_name = deck_name or "your deck"

        if deck_name:
            # Load by autoplay short name OR raw filename
            deck_data = self._load_deck_by_name(deck_name)
            if not deck_data:
                available = ", ".join(sorted(self.AUTOPLAY_DECKS.keys())[:15])
                await ctx.send(
                    f"❌ Deck `{deck_name}` not found. Try one of: {available}... "
                    f"or `!coverage` for your loaded deck."
                )
                return
            report_name = deck_data.get("name", deck_name)
            for entry in deck_data.get("cards", []):
                name = entry.get("name") if isinstance(entry, dict) else entry
                qty = entry.get("quantity", 1) if isinstance(entry, dict) else 1
                if qty <= 0 or not name:
                    continue
                for _ in range(qty):
                    cards.append(Card(
                        name=name,
                        mana_cost=entry.get("mana_cost", "") if isinstance(entry, dict) else "",
                        type_line=entry.get("type_line", "") if isinstance(entry, dict) else "",
                        oracle_text=entry.get("oracle_text", "") if isinstance(entry, dict) else "",
                    ))
        else:
            # Default: classify the player's loaded deck (via !mydeck)
            user_id = ctx.author.id
            if user_id not in self.player_decks:
                await ctx.send(
                    "You haven't loaded a deck. Use `!mydeck` first, or pass a "
                    "deck name like `!coverage mythic` to check an autoplay deck."
                )
                return
            deck_data = self.player_decks[user_id]
            report_name = deck_data.get("name", "your deck")
            for entry in deck_data.get("cards", []):
                name = entry.get("name") if isinstance(entry, dict) else entry
                qty = entry.get("quantity", 1) if isinstance(entry, dict) else 1
                if qty <= 0 or not name:
                    continue
                for _ in range(qty):
                    cards.append(Card(
                        name=name,
                        mana_cost=entry.get("mana_cost", "") if isinstance(entry, dict) else "",
                        type_line=entry.get("type_line", "") if isinstance(entry, dict) else "",
                        oracle_text=entry.get("oracle_text", "") if isinstance(entry, dict) else "",
                    ))

        if not cards:
            await ctx.send(f"❌ `{report_name}` has no cards to classify.")
            return

        coverage = classify_deck(cards)
        report = format_coverage_report(coverage, deck_name=report_name)
        await ctx.send(report)

    @commands.command(name="banned")
    async def check_banned(self, ctx, format_name: str, *, card_name: str = ""):
        """
        Check if a card is banned in a format.
        
        Usage:
            !banned commander Sol Ring  - Check if Sol Ring is banned in Commander
            !banned modern              - List some banned cards in Modern
        """
        format_name = format_name.lower()
        
        if format_name not in BANNED_CARDS:
            await ctx.send(f"Unknown format: {format_name}. Try: commander, modern, legacy, pioneer, pauper")
            return
        
        banned = BANNED_CARDS[format_name]
        
        if card_name:
            # Check specific card
            if card_name.lower() in banned:
                await ctx.send(f"🚫 **{card_name}** is **BANNED** in {format_name.title()}!")
            else:
                await ctx.send(f"✅ **{card_name}** is legal in {format_name.title()} (as far as I know)")
        else:
            # List some banned cards
            if banned:
                sample = list(banned)[:20]
                msg = f"**Banned in {format_name.title()}** ({len(banned)} cards):\n"
                msg += ", ".join(s.title() for s in sorted(sample))
                if len(banned) > 20:
                    msg += f"\n*...and {len(banned) - 20} more*"
                await ctx.send(msg)
            else:
                await ctx.send(f"No banned list stored for {format_name.title()} (changes frequently)")
    
    @commands.command(name="formats")
    async def list_formats(self, ctx):
        """
        Show available game formats and their rules.
        
        Usage:
            !formats  - List all supported formats
        """
        lines = ["**Supported Formats:**\n"]
        
        for fmt, life in FORMAT_STARTING_LIFE.items():
            min_size, max_size = FORMAT_DECK_SIZE.get(fmt, (60, None))
            size_str = f"{min_size}" if max_size is None else f"{min_size}" if min_size == max_size else f"{min_size}-{max_size}"
            
            singleton = "✓" if fmt in SINGLETON_FORMATS else ""
            cmd_zone = "✓" if fmt in COMMAND_ZONE_FORMATS else ""
            
            lines.append(f"**{fmt.title()}** - {life} life, {size_str} cards")
            if singleton or cmd_zone:
                notes = []
                if singleton:
                    notes.append("singleton")
                if cmd_zone:
                    notes.append("command zone")
                if fmt == "oathbreaker":
                    notes.append("signature spell")
                lines[-1] += f" ({', '.join(notes)})"
        
        lines.append("\n*Use `!game @opponent format_name` to start*")
        await ctx.send("\n".join(lines))
    
    @commands.command(name="commander", aliases=["cmd", "cmdr"])
    async def show_commander(self, ctx):
        """
        Show your commander and command zone status.

        Usage:
            !commander  - Show your commander info
        """
        game = self._get_game(ctx)
        if not game:
            await ctx.send("No active game in this thread!")
            return

        if game.format not in COMMAND_ZONE_FORMATS:
            await ctx.send(f"This game is **{game.format}** format - no commander!")
            return

        player_idx = game.get_player_index(ctx.author.id)
        if player_idx is None:
            await ctx.send("You're not in this game!")
            return

        player = game.players[player_idx]

        lines = [f"**{player.name}'s Command Zone:**"]

        # Check for commander/oathbreaker on battlefield
        commanders_on_board = [c for c in player.battlefield if c.is_commander]
        oathbreaker_on_board = len(commanders_on_board) > 0

        # Show cards in command zone
        cmd_zone_cards = list(player.command_zone)
        if not cmd_zone_cards and not commanders_on_board:
            lines.append("*(No commander in command zone or battlefield)*")
        else:
            # Show commanders on battlefield
            for cmd in commanders_on_board:
                tax = cmd.times_cast_from_command_zone * 2
                label = "Oathbreaker" if game.format == "oathbreaker" else "Commander"
                lines.append(f"• **{cmd.name}** ({label}) - On battlefield")
                lines.append(f"  Times cast: {cmd.times_cast_from_command_zone}, Next tax: {{{tax + 2}}}")

            # Show cards in command zone (commander + signature spell)
            for cmd in cmd_zone_cards:
                tax = cmd.times_cast_from_command_zone * 2
                total = cmd.cmc + tax
                if getattr(cmd, 'is_signature_spell', False):
                    castable = "castable" if oathbreaker_on_board else "locked — oathbreaker not on battlefield"
                    lines.append(f"• **{cmd.name}** ({cmd.mana_cost}) — Signature Spell ({castable})")
                else:
                    label = "Oathbreaker" if game.format == "oathbreaker" else "Commander"
                    lines.append(f"• **{cmd.name}** ({cmd.mana_cost}) — {label}")
                lines.append(f"  Times cast: {cmd.times_cast_from_command_zone} | Tax: {{{tax}}} | Total: {{{total}}}")

        # Show companion zone if any
        if player.companion_zone:
            lines.append("")
            lines.append("**Companion Zone:**")
            for comp in player.companion_zone:
                lines.append(f"• **{comp.name}** ({comp.mana_cost}) — pay {{3}} to move to hand")

        await ctx.send("\n".join(lines))

    @commands.command(name="companion")
    async def use_companion(self, ctx):
        """Pay {3} to move your companion from companion zone to hand."""
        game = self._get_game(ctx)
        if not game:
            await ctx.send("No active game in this thread!")
            return
        player_idx = game.get_player_index(ctx.author.id)
        if player_idx is None:
            await ctx.send("You're not in this game!")
            return
        if player_idx != game.active_player_index:
            await ctx.send(self._not_your_turn_msg(game))
            return
        if game.phase not in (Phase.MAIN1, Phase.MAIN2):
            await ctx.send("You can only use companion during a main phase!")
            return
        player = game.players[player_idx]
        if not player.companion_zone:
            await ctx.send("You don't have a companion!")
            return
        available = player.available_mana()
        if available < 3:
            await ctx.send(f"You need {{3}} mana to move your companion to hand (have {available} available).")
            return
        comp_card = player.companion_zone[0]
        mana_to_pay = 3
        for land in player.battlefield:
            if land.is_land() and not land.tapped and mana_to_pay > 0:
                land.tapped = True
                mana_to_pay -= 1
        player.companion_zone.remove(comp_card)
        player.hand.append(comp_card)
        print(f"[COMPANION] {player.name} paid {{3}} to move {comp_card.name} to hand")
        await ctx.send(f"**{player.name}** pays {{3}} to move companion **{comp_card.name}** to hand!")
        self.engine.save_game(game)

    @commands.command(name="mulligan", aliases=["mull"])
    async def mulligan(self, ctx):
        """
        Mulligan your opening hand (London Mulligan rules).
        
        Draw 7 new cards, then put cards on bottom equal to mulligans taken.
        Can only be used before you've played any cards.
        """
        game = self._get_game(ctx)
        if not game:
            await ctx.send("No active game in this thread!")
            return
        
        player_idx = game.get_player_index(ctx.author.id)
        if player_idx is None:
            await ctx.send("You're not in this game!")
            return
        
        player = game.players[player_idx]
        
        # Check if player has already kept
        if player.has_kept_hand:
            await ctx.send("You've already kept your hand! Can't mulligan now.")
            return
        
        # Check if player has played anything
        if player.battlefield or player.graveyard:
            await ctx.send("You've already started playing! Can't mulligan now.")
            return
        
        # Perform mulligan
        player.mulligans_taken += 1
        
        # Shuffle hand back into library
        player.library.extend(player.hand)
        player.hand.clear()
        random.shuffle(player.library)
        
        # Draw 7 new cards
        self.engine.draw_cards(player, 7)
        
        cards_to_bottom = player.mulligans_taken
        
        await ctx.send(
            f"🔄 **Mulligan #{player.mulligans_taken}!** Shuffled and drew 7 new cards.\n"
            f"You need to put **{cards_to_bottom}** card(s) on the bottom of your library.\n"
            f"Use `!bottom <card name>` to put a card on bottom, or `!keephand` when done."
        )
        
        # DM them their new hand
        try:
            user = ctx.author
            if HAS_BOARD_VISUAL:
                hand_img = await render_player_hand(player)
                await user.send(f"**Your new hand ({len(player.hand)} cards):**", 
                               file=discord.File(hand_img, "hand.png"))
            else:
                hand_text = "\n".join(f"{i+1}. {c.name}" for i, c in enumerate(player.hand))
                await user.send(f"**Your new hand:**\n{hand_text}")
        except discord.Forbidden:
            await ctx.send("(Couldn't DM you - check your hand with `!hand`)")
    
    @commands.command(name="bottom")
    async def put_bottom(self, ctx, *, card_name: str):
        """Put a card from your hand on the bottom of your library (for mulligans)."""
        game = self._get_game(ctx)
        if not game:
            await ctx.send("No active game in this thread!")
            return
        
        player_idx = game.get_player_index(ctx.author.id)
        if player_idx is None:
            await ctx.send("You're not in this game!")
            return
        
        player = game.players[player_idx]
        
        # Check if they need to bottom cards
        cards_to_bottom = player.mulligans_taken - (7 - len(player.hand))
        if cards_to_bottom <= 0 and player.mulligans_taken > 0:
            await ctx.send("You've already put enough cards on bottom! Use `!keephand` to confirm.")
            return
        
        if player.has_kept_hand:
            await ctx.send("You've already kept your hand!")
            return
        
        # Find the card
        card = player.find_card(card_name, Zone.HAND)
        if not card:
            await ctx.send(f"Couldn't find '{card_name}' in your hand!")
            return
        
        # Move to bottom of library
        player.hand.remove(card)
        player.library.insert(0, card)  # Index 0 = bottom
        
        remaining = player.mulligans_taken - (7 - len(player.hand))
        
        if remaining > 0:
            await ctx.send(f"📥 Put **{card.name}** on bottom. ({remaining} more to go)")
        else:
            await ctx.send(f"📥 Put **{card.name}** on bottom. Use `!keephand` to confirm your hand!")
    
    @commands.command(name="keephand", aliases=["kh"])
    async def keep_hand(self, ctx):
        """Keep your current hand (done mulliganing)."""
        game = self._get_game(ctx)
        if not game:
            await ctx.send("No active game in this thread!")
            return
        
        player_idx = game.get_player_index(ctx.author.id)
        if player_idx is None:
            await ctx.send("You're not in this game!")
            return
        
        player = game.players[player_idx]
        
        if player.has_kept_hand:
            await ctx.send("You've already kept!")
            return
        
        # Check if they still need to bottom cards
        if player.mulligans_taken > 0:
            cards_to_bottom = player.mulligans_taken - (7 - len(player.hand))
            if cards_to_bottom > 0:
                await ctx.send(f"You still need to put **{cards_to_bottom}** card(s) on bottom first! Use `!bottom <card>`")
                return
        
        player.has_kept_hand = True
        
        if player.mulligans_taken == 0:
            await ctx.send(f"✋ **{player.name}** keeps their opening 7!")
        else:
            await ctx.send(f"✋ **{player.name}** keeps with {len(player.hand)} cards (mulliganed {player.mulligans_taken}x)")
        
        self.engine.save_game(game)
    
    @commands.command(name="state")
    async def show_state(self, ctx, mode: str = "text"):
        """Show current game state.
        
        Usage:
            !state        - Text-based state
            !state visual - Visual board image
        """
        game = self._get_game(ctx)
        if not game:
            await ctx.send("No active game in this thread!")
            return
        
        if mode.lower() in ["visual", "v", "image", "img", "board"]:
            await self._send_visual_board(ctx, game)
        else:
            embed = self.display.create_game_embed(game)
            await ctx.send(embed=embed)
    
    @commands.command(name="board")
    async def show_board(self, ctx):
        """Show visual board state as an image."""
        game = self._get_game(ctx)
        if not game:
            await ctx.send("No active game in this thread!")
            return
        
        await self._send_visual_board(ctx, game)
    
    async def _send_visual_board(self, ctx, game):
        """Send a visual board state image."""
        if not HAS_BOARD_VISUAL:
            await ctx.send("⚠️ Visual board not available - Pillow not installed.\nUse `!state` for text view.")
            return
        
        async with ctx.typing():
            try:
                buffer = await render_game_board(game)
                file = discord.File(buffer, filename="board.png")
                
                # Create a small embed with the image
                embed = discord.Embed(
                    title=f"⚔️ {game.format.title()} - Turn {game.turn_number}",
                    color=discord.Color.gold() if game.active_player_index == 0 else discord.Color.blue()
                )
                embed.set_image(url="attachment://board.png")
                
                await ctx.send(embed=embed, file=file)
            except Exception as e:
                print(f"Board render error: {e}")
                await ctx.send(f"⚠️ Failed to render board: {e}\nUse `!state` for text view.")
    
    @commands.command(name="hand")
    async def show_hand(self, ctx, mode: str = "visual"):
        """View your hand (sent via DM).
        
        Usage:
            !hand        - Visual hand with card images (default)
            !hand text   - Text-only hand list
        """
        game = self._get_game(ctx)
        if not game:
            await ctx.send("No active game in this thread!")
            return
        
        player = game.get_player_by_user_id(ctx.author.id)
        if not player:
            await ctx.send("You're not in this game!")
            return
        
        try:
            if mode.lower() in ["visual", "v", "image", "img"] and HAS_BOARD_VISUAL:
                async with ctx.typing():
                    buffer = await render_player_hand(player)
                    file = discord.File(buffer, filename="hand.png")
                    embed = discord.Embed(
                        title=f"🎴 Your Hand ({len(player.hand)} cards)",
                        color=discord.Color.blue()
                    )
                    embed.set_image(url="attachment://hand.png")
                    await ctx.author.send(embed=embed, file=file)
            else:
                hand_text = self.display.format_hand(player)
                await ctx.author.send(hand_text)
            
            await ctx.send("📬 Check your DMs!")
        except discord.Forbidden:
            await ctx.send("I can't DM you! Please enable DMs from server members.")
    
    @commands.command(name="play")
    async def play_card(self, ctx, *, card_name: str):
        """
        Play a card from your hand.
        
        Usage:
            !play Mountain                    - Play a land
            !play Lightning Bolt              - Cast a spell (auto-target)
            !play Lightning Bolt target Bob   - Cast with specific target
            !play Ram Through targeting Ragavan - Target a creature
            !play Omarthis X=3               - Cast X-cost spell with X=3
            !play Walking Ballista X=5       - X-cost (auto-calculates if X= omitted)
        """
        game = self._get_game(ctx)
        if not game:
            await ctx.send("No active game in this thread!")
            return
        
        player_idx = game.get_player_index(ctx.author.id)
        if player_idx is None:
            await ctx.send("You're not in this game!")
            return
        
        if player_idx != game.active_player_index:
            await ctx.send(self._not_your_turn_msg(game))
            return

        player = game.players[player_idx]
        # Undo snapshot: capture pre-play state for the !undo command.
        self._snapshot_for_undo(game, f"{player.name} played {card_name}")

        # Parse target from card name if present
        # Formats: "Card target X", "Card targeting X", "Card on X"
        target = None
        target_name = None
        actual_card_name = card_name
        x_value_override = None

        # Parse X=N for X-cost spells: "Omarthis X=3" or "Walking Ballista x=5"
        x_match = re.search(r'\s+[xX]=(\d+)\s*$', actual_card_name)
        if x_match:
            x_value_override = int(x_match.group(1))
            actual_card_name = actual_card_name[:x_match.start()].strip()
        
        for sep in [" targeting ", " target ", " on "]:
            if sep in actual_card_name.lower():
                parts = actual_card_name.lower().split(sep, 1)
                actual_card_name = actual_card_name[:len(parts[0])]  # Preserve original case
                target_name = parts[1].strip()
                break
        
        card = player.find_card(actual_card_name, Zone.HAND)
        from_exile = False
        from_command_zone = False

        # Check if player is casting by adventure name (e.g. "!play Fertile Footsteps")
        if not card:
            for c in player.hand:
                if c.adventure_name and c.adventure_name.lower() == actual_card_name.lower():
                    card = c
                    card.cast_as_adventure = True
                    print(f"[ADVENTURE] Player casting {c.adventure_name} (adventure of {c.name})")
                    break

        # Check if name matches a split card half (e.g. "!play Commit" for Commit // Memory)
        if not card:
            for c in player.hand:
                if c.split_names:
                    for i, sname in enumerate(c.split_names):
                        if actual_card_name.lower() == sname.lower():
                            c.cast_as_split_half = i
                            card = c
                            print(f"[SPLIT] Player casting {sname} (half {i} of {c.name})")
                            break
                    if card:
                        break

        # If not in hand, check if it's playable from exile (Chandra 0, etc.)
        if not card:
            exile_card = player.find_card(actual_card_name, Zone.EXILE)
            if exile_card and (exile_card.id in player.playable_from_exile
                               or getattr(exile_card, '_adventure_exiled', False)):
                card = exile_card
                from_exile = True

        # Bug #28: Check if it's playable from graveyard (Snapcaster flashback, native flashback, escape)
        from_graveyard = False
        if not card and player.playable_from_graveyard:
            for c in player.graveyard:
                if c.id in player.playable_from_graveyard and c.name.lower() == actual_card_name.lower():
                    card = c
                    from_graveyard = True
                    player.graveyard.remove(c)
                    player.playable_from_graveyard.remove(c.id)
                    # Pay escape exile cost if applicable
                    if c.oracle_text:
                        from mtg.helpers import parse_escape_cost
                        _esc = parse_escape_cost(c.oracle_text)
                        if _esc:
                            _esc_cost, exile_count = _esc
                            # Charge the escape cost, and let the ETB see that
                            # it WAS escaped (Kroxa's "sacrifice it unless it
                            # escaped" had no producer for this at all).
                            c._escape_cost = _esc_cost
                            c._was_escaped = True
                            exiled_names = []
                            for _ in range(exile_count):
                                if player.graveyard:
                                    exiled = player.graveyard.pop()
                                    player.exile.append(exiled)
                                    exiled_names.append(exiled.name)
                            print(f"[ESCAPE] Casting {c.name} from graveyard, exiling {exile_count}: {', '.join(exiled_names)}")
                        else:
                            print(f"[FLASHBACK] Casting {c.name} from graveyard via flashback")
                    else:
                        print(f"[FLASHBACK] Casting {c.name} from graveyard via flashback")
                    break

        # If not in hand/exile, check command zone (commander / signature spell)
        if not card and game.format in COMMAND_ZONE_FORMATS:
            for cmd_card in player.command_zone:
                if cmd_card.name.lower() == actual_card_name.lower() or actual_card_name.lower() in cmd_card.name.lower():
                    card = cmd_card
                    from_command_zone = True
                    break

        # [OATHBREAKER] Signature spell can only be cast while oathbreaker is on the battlefield
        if card and from_command_zone and getattr(card, 'is_signature_spell', False) and game.format == "oathbreaker":
            oathbreaker_on_board = any(c.is_commander for c in player.battlefield)
            if not oathbreaker_on_board:
                await ctx.send(f"⚠️ **{card.name}** is your signature spell — it can only be cast while your oathbreaker is on the battlefield!")
                return

        if not card:
            # Check if the card exists in exile but isn't playable
            exile_card = player.find_card(actual_card_name, Zone.EXILE)
            if exile_card:
                await ctx.send(f"'{actual_card_name}' is in exile but can't be played right now.")
            # Check if it's in command zone
            elif game.format in COMMAND_ZONE_FORMATS:
                for cmd_card in player.command_zone:
                    if actual_card_name.lower() in cmd_card.name.lower():
                        await ctx.send(f"'{cmd_card.name}' is in your command zone. Use `!play {cmd_card.name}` to cast it (commander tax applies).")
                        return
                await ctx.send(f"Couldn't find '{actual_card_name}' in your hand or command zone.")
            else:
                await ctx.send(f"Couldn't find '{actual_card_name}' in your hand.")
            return
        
        # Find target if specified
        if target_name:
            # Look for target creature/permanent
            for p in game.players:
                target = p.find_card(target_name, Zone.BATTLEFIELD)
                if target:
                    break
            # Check if targeting a player
            if not target:
                for p in game.players:
                    if p.name.lower() == target_name.lower():
                        target = p
                        break
            if not target:
                await ctx.send(f"⚠️ Couldn't find target '{target_name}'")
                return
        
        if card.is_land():
            # If from exile, move to hand first (play_land expects hand)
            if from_exile:
                player.exile.remove(card)
                player.hand.append(card)
                # An adventure card is castable from exile without being
                # in playable_from_exile — don't remove what isn't there.
                if card.id in player.playable_from_exile:
                    player.playable_from_exile.remove(card.id)
                card._adventure_exiled = False
            
            success, msg = self.engine.play_land(game, player, card)
            if success:
                source = " from exile" if from_exile else ""
                await ctx.send(f"🌍 Played **{card.name}**{source}")
                
                # Check land ETB triggers
                land_etb_msgs = self.engine._handle_land_etb(game, player, card)
                for etb_msg in land_etb_msgs:
                    await ctx.send(etb_msg)
                
                # Check global ETB triggers (e.g. from other permanents)
                global_etb_msgs = self.engine._handle_etb_triggers(game, player, card)
                for etb_msg in global_etb_msgs:
                    await ctx.send(etb_msg)
            else:
                # If it failed and was from exile, move back
                if from_exile and card in player.hand:
                    player.hand.remove(card)
                    player.exile.append(card)
                    player.playable_from_exile.append(card.id)
                await ctx.send(f"⚠️ {msg}")
                return
        else:
            # If from exile, move to hand first (cast_spell expects hand)
            if from_exile:
                player.exile.remove(card)
                player.hand.append(card)
                player.playable_from_exile.remove(card.id)

            # If from graveyard (flashback/escape), move to hand for cast_spell_async
            if from_graveyard and card not in player.hand:
                player.hand.append(card)
            # Mark flashback/escape cast so templates can detect graveyard origin
            # (e.g., Increasing Devotion creates 10 tokens instead of 5 from GY).
            if from_graveyard:
                card._cast_from_graveyard = True

            # If from command zone, handle commander tax
            commander_tax = 0
            if from_command_zone:
                commander_tax = card.times_cast_from_command_zone * 2
                if commander_tax > 0:
                    await ctx.send(f"💰 Commander tax: {{{commander_tax}}} additional mana required")

                # Move to hand temporarily for casting
                player.command_zone.remove(card)
                player.hand.append(card)

            # Use async spell casting with effect resolution
            # Set X value for X-cost spells
            if x_value_override is not None:
                card._x_value = x_value_override
            success, msg, effect_msgs = await self.engine.cast_spell_async(
                game, player, card, target=target,
                additional_cost=commander_tax
            )
            if success:
                # Flashback/escape: exile the card after resolution instead of graveyard
                if from_graveyard:
                    if card in player.graveyard:
                        player.graveyard.remove(card)
                        player.exile.append(card)
                        print(f"[FLASHBACK] {card.name} exiled after casting from graveyard")
                source = " from exile" if from_exile else (" from command zone" if from_command_zone else (" from graveyard" if from_graveyard else ""))
                tax_msg = f" (paid {{{commander_tax}}} commander tax)" if commander_tax > 0 else ""
                x_msg = ""
                if hasattr(card, '_mana_paid') and card.mana_cost and 'X' in card.mana_cost.upper():
                    # Show what X was
                    cost_upper = card.mana_cost.upper()
                    x_count = cost_upper.count('X')
                    colored = sum(cost_upper.count(f'{{{c}}}') for c in ['W', 'U', 'B', 'R', 'G'])
                    generic = sum(int(m) for m in re.findall(r'\{(\d+)\}', cost_upper))
                    x_val = (card._mana_paid - colored - generic) // max(x_count, 1)
                    x_msg = f" (X={x_val})"
                # May 7 audit fix #1: skip duplicate cast announcement if
                # cast_spell_async already emitted it before the priority window.
                already_announced = (
                    hasattr(game, '_early_announced_casts')
                    and id(card) in game._early_announced_casts
                )
                if already_announced:
                    game._early_announced_casts.discard(id(card))
                    # Still post the tax/X info if relevant — early announcement omits it.
                    if x_msg or (commander_tax > 0):
                        suffix_parts = []
                        if commander_tax > 0:
                            suffix_parts.append(f"paid {{{commander_tax}}} commander tax")
                        if x_msg:
                            suffix_parts.append(x_msg.strip(" ()"))
                        if suffix_parts:
                            await ctx.send(f"💰 {' · '.join(suffix_parts)}")
                else:
                    await ctx.send(f"✨ Cast **{card.name}**{source}{tax_msg}{x_msg}")

                # Track commander casts for tax
                if from_command_zone:
                    card.times_cast_from_command_zone += 1

                # Send effect messages
                if effect_msgs:
                    for effect_msg in effect_msgs:
                        await ctx.send(effect_msg)
            else:
                # If failed and was from exile, move back
                if from_exile and card in player.hand:
                    player.hand.remove(card)
                    player.exile.append(card)
                    player.playable_from_exile.append(card.id)
                # If failed and was from command zone, move back
                if from_command_zone and card in player.hand:
                    player.hand.remove(card)
                    player.command_zone.append(card)
                await ctx.send(f"⚠️ {msg}")
                return
        
        # Check state-based actions
        events = self.engine.check_state_based_actions(game)
        if events:
            await ctx.send("\n".join(f"⚡ {e}" for e in events))
        
        # Save game state
        self.engine.save_game(game)
        
        if game.ended:
            await ctx.send(f"🏆 **{game.players[game.winner].name} wins!**")
            self.engine.delete_game(game.thread_id)
    
    @commands.command(name="suspend")
    async def suspend_card(self, ctx, *, card_name: str):
        """
        Suspend a card with Suspend from your hand.
        
        Usage:
            !suspend Mox Tantalite
            !suspend Ancestral Vision
        """
        game = self._get_game(ctx)
        if not game:
            await ctx.send("No active game in this thread!")
            return
        
        player_idx = game.get_player_index(ctx.author.id)
        if player_idx is None or player_idx != game.active_player_index:
            await ctx.send(self._not_your_turn_msg(game))
            return
        
        player = game.players[player_idx]
        
        # Find card in hand
        card = player.find_card(card_name, Zone.HAND)
        if not card:
            await ctx.send(f"Can't find '{card_name}' in your hand!")
            return
        
        # Check if card has Suspend
        oracle_lower = card.oracle_text.lower() if card.oracle_text else ""
        
        if "suspend" not in oracle_lower:
            await ctx.send(f"**{card.name}** doesn't have Suspend!")
            return
        
        # Parse suspend number (e.g., "Suspend 3" or "Suspend 4—{U}")
        suspend_match = re.search(r'suspend\s*(\d+)', oracle_lower)
        if not suspend_match:
            await ctx.send(f"Couldn't parse Suspend number from {card.name}'s text.")
            return
        
        suspend_count = int(suspend_match.group(1))
        
        # Move card from hand to exile with time counters
        player.hand.remove(card)
        player.exile.append(card)
        card.suspended = True
        card.counters['time'] = suspend_count
        
        await ctx.send(f"⏳ Suspended **{card.name}** with {suspend_count} time counters!")
        await ctx.send(f"*At the beginning of your upkeep, a time counter will be removed. When the last is removed, you'll cast it for free!*")
        
        # Save game state
        self.engine.save_game(game)
    
    @commands.command(name="activate", aliases=["act", "ability", "pw"])
    async def activate_ability(self, ctx, *, args: str = ""):
        """
        Activate an ability on a permanent.
        
        Usage:
            !activate Chandra +1                → Planeswalker +1 ability
            !activate Chandra +1 target claude  → +1 targeting Claude
            !activate Sneak Attack              → Show Sneak Attack's abilities
            !activate Sneak Attack 1            → Activate ability #1
            !activate Deathrite Shaman 1        → Activate first ability
            
        Or to see abilities:
            !activate <permanent>               → Show available abilities
        """
        game = self._get_game(ctx)
        if not game:
            await ctx.send("No active game in this thread!")
            return
        
        player_idx = game.get_player_index(ctx.author.id)
        if player_idx is None:
            await ctx.send("You're not in this game!")
            return
        
        player = game.players[player_idx]

        # Parse arguments
        args = args.strip()
        if not args:
            # Show all permanents with activated abilities — read-only,
            # no snapshot needed.
            activatable = []
            for c in player.battlefield:
                if c.is_planeswalker():
                    activatable.append(f"• **{c.name}** (planeswalker)")
                elif c.oracle_text and ':' in c.oracle_text and not c.is_land():
                    activatable.append(f"• **{c.name}**")

            if activatable:
                await ctx.send("**Permanents with activated abilities:**\n" + "\n".join(activatable))
            else:
                await ctx.send("You don't control any permanents with activated abilities!")
            return
        # Undo snapshot: capture pre-activation state for !undo.
        self._snapshot_for_undo(game, f"{player.name} activated {args}")
        
        # Parse target from args
        target_name = None
        working_args = args
        
        for sep in [" targeting ", " target "]:
            if sep in args.lower():
                parts = args.lower().split(sep, 1)
                working_args = args[:len(parts[0])]
                target_name = parts[1].strip()
                break
        
        # Check for loyalty cost pattern (planeswalker) or ability index
        loyalty_pattern = r'([+-]?\d+)(?:\s+(\d+))?$'
        match = re.search(loyalty_pattern, working_args)
        
        ability_specifier = None
        sub_index = 0
        user_chose_sub_index = False
        card_name = working_args
        
        if match:
            ability_specifier = match.group(1)
            if match.group(2):
                sub_index = int(match.group(2)) - 1
                user_chose_sub_index = True
            card_name = working_args[:match.start()].strip()
        
        # Find the permanent
        card = None
        card_name_lower = card_name.lower().strip()
        
        for c in player.battlefield:
            if c.name.lower() == card_name_lower or card_name_lower in c.name.lower():
                card = c
                break
        
        if not card:
            await ctx.send(f"❌ You don't control a permanent matching '{card_name}'")
            return
        
        # Route to appropriate handler
        if card.is_planeswalker():
            await self._activate_planeswalker(ctx, game, player, player_idx, card, ability_specifier, sub_index, target_name, user_chose_sub_index)
        else:
            await self._activate_permanent(ctx, game, player, player_idx, card, ability_specifier, target_name)

    async def _activate_planeswalker(self, ctx, game, player, player_idx, card, ability_specifier, sub_index, target_name, user_chose_sub_index=False):
        """Handle planeswalker ability activation."""
        if not self.engine.planeswalker_manager:
            await ctx.send("⚠️ Planeswalker abilities not available.")
            return
        
        pw_manager = self.engine.planeswalker_manager
        
        # If no ability specified, show abilities
        if ability_specifier is None:
            display = pw_manager.get_ability_display(card)
            await ctx.send(display)
            return
        
        # Parse loyalty cost
        loyalty_cost = int(ability_specifier)
        
        # Check if user explicitly specified which ability (for duplicates)
        user_specified_index = user_chose_sub_index
        
        # Find ability by loyalty cost
        ability, error = pw_manager.get_ability_by_cost(card, loyalty_cost, sub_index)
        
        # If multiple abilities with same cost and user didn't specify which one, prompt
        matching = pw_manager.find_abilities_by_cost(card, loyalty_cost)
        if len(matching) > 1 and not user_specified_index:
            cost_str = f"+{loyalty_cost}" if loyalty_cost > 0 else str(loyalty_cost)
            lines = [f"**{card.name}** has multiple [{cost_str}] abilities:"]
            for i, a in enumerate(matching, 1):
                text = a.text
                lines.append(f"  `{i}` - {text}")
            target_hint = f" target {target_name}" if target_name else ""
            lines.append(f"\n*Use `!activate {card.name} {cost_str} <number>{target_hint}` to pick one*")
            await ctx.send("\n".join(lines))
            return
        
        if not ability:
            await ctx.send(f"❌ {error}")
            return
        
        ability_index = ability.index
        
        # Validate activation
        can_act, reason = pw_manager.can_activate(game, player, card, ability_index)
        if not can_act:
            await ctx.send(f"❌ {reason}")
            return
        
        # Handle dual-target abilities (Chandra, Pyromaster +1 pattern)
        # Printed: "deals 1 damage to target player and 1 damage to up to one target creature that player controls"
        # Scryfall errata: "deals 1 damage to target player or planeswalker and 1 damage to up to one
        #   target creature that player or that planeswalker's controller controls"
        ability_lower = ability.text.lower()
        dual_target_match = re.search(
            r'deals?\s+(\d+)\s+damage\s+to\s+target\s+player(?:\s+or\s+planeswalker)?\s+and\s+(\d+)\s+damage\s+to\s+up\s+to\s+one\s+target\s+creature\s+that\s+player(?:\s+or\s+that\s+planeswalker.s\s+controller)?\s+controls',
            ability_lower
        )
        if dual_target_match:
            player_dmg = int(dual_target_match.group(1))
            creature_dmg = int(dual_target_match.group(2))

            # Stage 1: pick a target player (opponents only for damage)
            opponent_players = [p for p in game.players if p != player]
            if not opponent_players:
                await ctx.send(f"❌ No legal targets for {card.name}'s ability")
                return

            if target_name:
                # Player specified target inline
                target_player = None
                for p in game.players:
                    if target_name.lower() in p.name.lower():
                        target_player = p
                        break
                if not target_player:
                    await ctx.send(f"❌ '{target_name}' is not a valid player target")
                    return
            elif len(opponent_players) == 1:
                target_player = opponent_players[0]
            else:
                # Prompt for player choice
                game.pending_action = {
                    'type': 'chandra_dual_target_player',
                    'card_id': card.id,
                    'ability_index': ability_index,
                    'player_idx': player_idx,
                    'player_dmg': player_dmg,
                    'creature_dmg': creature_dmg,
                }
                cost_str = f"+{ability.loyalty_cost}" if ability.loyalty_cost > 0 else str(ability.loyalty_cost)
                lines = [f"🎯 **Choose a target player for {card.name}'s [{cost_str}] ability:**"]
                for i, p in enumerate(game.players):
                    lines.append(f"  `{i}` - {p.name}")
                lines.append(f"\n*Reply with `!target <number>` to select*")
                await ctx.send("\n".join(lines))
                return

            # Execute: apply loyalty cost manually (Chandra dual-target handles damage itself)
            old_loyalty = card.loyalty_counters
            card.loyalty_counters += ability.loyalty_cost
            # Record activation for this turn
            if hasattr(pw_manager, '_activations_this_turn'):
                game_id = game.thread_id
                if game_id not in pw_manager._activations_this_turn:
                    pw_manager._activations_this_turn[game_id] = set()
                pw_manager._activations_this_turn[game_id].add(card.id)
            actual_dmg = self.engine.rules._apply_noncombat_damage_to_player(game, target_player, player_dmg, card.name, card.id)
            ability_text = (getattr(ability, 'text', '') or '').strip()
            # May 18 audit: dedupe repeat oracle text across PW activations.
            from mtg.helpers import format_activate_line
            header = format_activate_line(card.name, ability.loyalty_cost, ability_text, game=game)
            await ctx.send(f"{header}\n"
                           f"Loyalty: {card.loyalty_counters - ability.loyalty_cost} → {card.loyalty_counters}\n"
                           f"🔥 {card.name} deals {actual_dmg} damage to {target_player.name} (Life: {target_player.life})")

            # Stage 2: optionally pick a creature that target player controls
            target_creatures = [c for c in target_player.battlefield if c.is_creature()]
            if target_creatures:
                game.pending_action = {
                    'type': 'chandra_dual_target_creature',
                    'card_id': card.id,
                    'player_idx': player_idx,
                    'creature_dmg': creature_dmg,
                    'target_player_name': target_player.name,
                    'target_player_idx': game.players.index(target_player),
                }
                lines = [f"🎯 **Optionally choose a creature {target_player.name} controls (up to one):**"]
                for i, c in enumerate(target_creatures):
                    lines.append(f"  `{i}` - {c.name} ({c.power}/{c.toughness})")
                lines.append(f"  `{len(target_creatures)}` - No creature target (skip)")
                lines.append(f"\n*Reply with `!target <number>` to select*")
                await ctx.send("\n".join(lines))
            else:
                await ctx.send(f"*No creatures controlled by {target_player.name} to target.*")
                game.pending_action = None

            self.engine.save_game(game)
            return

        # Handle targeting
        if ability.needs_target:
            legal_targets = get_legal_planeswalker_targets(game, player, ability)

            if not legal_targets:
                await ctx.send(f"❌ No legal targets for {card.name}'s ability")
                return

            if target_name:
                found_target = None
                target_name_lower_t = target_name.lower()

                for p in game.players:
                    if p.name.lower() == target_name_lower_t or target_name_lower_t in p.name.lower():
                        for t, desc in legal_targets:
                            if t == p or (hasattr(t, 'name') and t.name == p.name):
                                found_target = t
                                break
                        break

                if not found_target:
                    for t, desc in legal_targets:
                        if hasattr(t, 'name') and target_name_lower_t in t.name.lower():
                            found_target = t
                            break

                if found_target:
                    targets = [found_target]
                else:
                    await ctx.send(f"❌ '{target_name}' is not a legal target for this ability")
                    return
            elif len(legal_targets) == 1:
                targets = [legal_targets[0][0]]
            else:
                game.pending_action = {
                    'type': 'planeswalker_target',
                    'card_id': card.id,
                    'ability_index': ability_index,
                    'legal_targets': legal_targets,
                    'player_idx': player_idx,
                }

                cost_str = f"+{ability.loyalty_cost}" if ability.loyalty_cost > 0 else str(ability.loyalty_cost)
                lines = [f"🎯 **Choose a target for {card.name}'s [{cost_str}] ability:**"]
                for i, (target, desc) in enumerate(legal_targets):
                    lines.append(f"  `{i}` - {desc}")
                lines.append(f"\n*Reply with `!target <number>` to select*")
                await ctx.send("\n".join(lines))
                return
        else:
            targets = []
        
        # Execute the ability
        result = await pw_manager.activate(game, player, card, ability_index, targets)
        
        for msg in result.messages:
            await ctx.send(msg)
        
        events = self.engine.check_state_based_actions(game)
        if events:
            await ctx.send("\n".join(f"⚡ {e}" for e in events))
        
        self.engine.save_game(game)

    def _parse_activated_abilities(self, oracle_text: str) -> List[Dict]:
        """
        Parse activated abilities from oracle text.
        Format: COST: EFFECT (where COST contains mana symbols, {T}, or other costs)
        
        Returns list of {index, cost, effect, needs_tap, mana_cost}
        """
        if not oracle_text:
            return []
        
        abilities = []
        
        # Normalize unicode
        text = oracle_text.replace('−', '-')
        
        # Pattern for activated abilities: cost followed by colon
        # Costs can include: {W}, {U}, {B}, {R}, {G}, {C}, {X}, {T}, {Q}, numbers, "Sacrifice", "Pay N life", etc.
        # Split by newlines first to get individual abilities
        lines = text.split('\n')
        
        idx = 0
        for line in lines:
            line = line.strip()

            # CR 702.32: Cycling / Channel are hand-only activated abilities. Never
            # surface them from the battlefield — would let the user pay the cost
            # and get the "when you cycle" trigger without actually discarding.
            if re.match(r'^(cycling|channel)\b', line.lower()):
                continue

            # Handle "Equip {N}" keyword (no colon format)
            equip_match = re.match(r'^Equip\s+(\{[^}]+\}(?:\{[^}]+\})*)', line)
            if equip_match:
                mana_cost = equip_match.group(1)
                abilities.append({
                    'index': idx,
                    'cost': f"Equip {mana_cost}",
                    'effect': "Attach this Equipment to target creature you control",
                    'needs_tap': False,
                    'mana_cost': mana_cost,
                    'full_text': line,
                })
                idx += 1
                continue

            if ':' not in line:
                continue
            
            # Split on first colon
            colon_pos = line.find(':')
            cost_part = line[:colon_pos].strip()
            effect_part = line[colon_pos + 1:].strip()
            
            # Skip if this looks like a keyword ability definition or reminder text
            if not cost_part or not effect_part:
                continue
            
            # Check if cost_part looks like an activated ability cost
            # Should contain mana symbols {X}, {T}, "Sacrifice", "Pay", "Discard", or ","
            has_mana = '{' in cost_part
            has_tap = '{T}' in cost_part or '{Q}' in cost_part
            has_other_cost = any(kw in cost_part for kw in ['Sacrifice', 'Pay', 'Discard', 'Remove', 'Exile', 'Equip'])
            
            # Skip loyalty abilities (handled by planeswalker code)
            if re.match(r'^[+-]?\d+$', cost_part):
                continue
            
            if has_mana or has_tap or has_other_cost:
                # Parse mana cost from cost_part
                mana_symbols = re.findall(r'\{([^}]+)\}', cost_part)
                mana_cost = ''.join(f'{{{s}}}' for s in mana_symbols if s != 'T' and s != 'Q')
                
                abilities.append({
                    'index': idx,
                    'cost': cost_part,
                    'effect': effect_part,
                    'needs_tap': has_tap,
                    'mana_cost': mana_cost,
                    'full_text': line,
                })
                idx += 1
        
        return abilities

    async def _activate_permanent(self, ctx, game, player, player_idx, card, ability_specifier, target_name):
        """Handle non-planeswalker activated ability."""
        
        # Parse abilities from oracle text
        abilities = self._parse_activated_abilities(card.oracle_text)
        
        if not abilities:
            await ctx.send(f"❌ **{card.name}** has no activated abilities.")
            return
        
        # If no ability specified, show abilities
        if ability_specifier is None:
            lines = [f"**{card.name}** - Activated Abilities:"]
            for ab in abilities:
                tap_icon = "⟳" if ab['needs_tap'] else ""
                lines.append(f"  **[{ab['index'] + 1}]** {tap_icon} `{ab['cost']}`: {ab['effect']}")
            lines.append(f"\n*Use `!activate {card.name} <number>` to activate*")
            await ctx.send("\n".join(lines))
            return
        
        # Find the ability by index
        try:
            ability_idx = int(ability_specifier) - 1  # Convert to 0-indexed
        except ValueError:
            await ctx.send(f"❌ Invalid ability number. Use `!activate {card.name}` to see abilities.")
            return
        
        if ability_idx < 0 or ability_idx >= len(abilities):
            await ctx.send(f"❌ Invalid ability number. {card.name} has {len(abilities)} ability/abilities.")
            return
        
        ability = abilities[ability_idx]
        
        # Check if card needs to tap and is already tapped
        if ability['needs_tap'] and card.tapped:
            await ctx.send(f"❌ **{card.name}** is already tapped!")
            return
        
        # Check summoning sickness for tap abilities on creatures
        if ability['needs_tap'] and card.is_creature():
            if card.entered_this_turn and not card.has_haste():
                await ctx.send(f"❌ **{card.name}** has summoning sickness!")
                return
        
        # Validate activation: mana cost, sorcery speed, XMage bridge cross-check
        is_legal, reason = await self.engine._validate_activation(
            game, player, card, ability_cost=ability['cost']
        )
        if not is_legal:
            await ctx.send(f"❌ Cannot activate **{card.name}**: {reason}")
            return

        # June 11 live retest: the post-refactor manual path called the
        # nonexistent `self._validate_activation`, never paid Equip's mana,
        # then sent its attach prose to Tier 3. Resolve this deterministic
        # keyword action locally, mirroring the autoplay path.
        is_equip = ability['cost'].lower().startswith('equip')
        if is_equip:
            equip_target = (player.find_card(target_name, Zone.BATTLEFIELD)
                            if target_name else None)
            if equip_target is None:
                candidates = [c for c in player.battlefield
                              if c.is_creature() and c.id != card.id]
                if candidates:
                    equip_target = max(
                        candidates,
                        key=lambda c: c.get_effective_power(game))
            if equip_target is None or not equip_target.is_creature(game):
                await ctx.send(f"❌ **{card.name}** needs a creature you control to equip.")
                return
            mana_cost = ability.get('mana_cost', '')
            if mana_cost and not player.tap_sources_for_cost(mana_cost, game=game):
                await ctx.send(f"❌ Cannot pay {mana_cost} to equip **{card.name}**.")
                return
            msg = self.engine.rules._execute_action_on_state(game, {
                "action": "equip",
                "equipment": card.name,
                "creature": equip_target.name,
                "player": player.name,
            })
            if msg:
                await ctx.send(msg)
            events = self.engine.check_state_based_actions(game)
            if events:
                await ctx.send("\n".join(f"⚡ {event}" for event in events))
            self.engine.save_game(game)
            return

        # Parse X value from command args (supports "!activate Card 1 X=3" syntax)
        x_value = None
        cost_text = ability['cost']
        if '{X}' in cost_text:
            # Check if target_name contains X=N (it gets lumped into target_name by the parser)
            if target_name and re.search(r'x\s*=\s*(\d+)', target_name, re.IGNORECASE):
                x_match = re.search(r'x\s*=\s*(\d+)', target_name, re.IGNORECASE)
                x_value = int(x_match.group(1))
                # Remove X=N from target_name so it doesn't interfere with targeting
                target_name = re.sub(r'\s*x\s*=\s*\d+\s*', '', target_name, flags=re.IGNORECASE).strip() or None
            else:
                # Default: auto-calculate X from available mana (total available minus non-X costs)
                total_available = sum(player.mana_pool.values()) + len(player.untapped_lands())
                # Count non-X mana symbols in cost
                non_x_symbols = re.findall(r'\{([^}]+)\}', cost_text)
                non_x_cost = sum(1 for s in non_x_symbols if s not in ('X', 'T', 'Q'))
                x_value = max(0, total_available - non_x_cost)
                # Cap at reasonable value
                x_value = min(x_value, 20)

        # July 30 batch-9 reviewer audit: plural counter costs ("Remove three
        # quest counters ... and sacrifice it" — Khalni Heart Expedition) never
        # matched the singular-only regex, so the whole cost went unenforced.
        # Manual-path twin of the mtg/engine.py fix (the two-activation-paths
        # divergence class, again).
        from mtg.helpers import _NUMBER_WORDS as _num_words
        counter_cost_match = re.search(
            r'remove (a|an|one|two|three|four|five|six|seven|eight|nine|ten|\d+)'
            r' ([\w +/\-]+?) counters? from (?:this|'
            + re.escape(card.name.lower()) + r')', cost_text.lower())
        counter_cost_n = 0
        if counter_cost_match:
            _nw = counter_cost_match.group(1)
            counter_cost_n = (_num_words.get(_nw)
                              or (int(_nw) if _nw.isdigit() else 1))
            counter_type = counter_cost_match.group(2).strip()
            if card.counters.get(counter_type, 0) < counter_cost_n:
                await ctx.send(f"❌ **{card.name}** has only "
                               f"{card.counters.get(counter_type, 0)} {counter_type} "
                               f"counter(s) — the cost needs {counter_cost_n}.")
                return

        # Validation only proved the cost was affordable; actually tap the
        # sources here for every non-Equip activated ability.
        mana_cost = ability.get('mana_cost', '')
        if mana_cost and not player.tap_sources_for_cost(mana_cost, game=game):
            await ctx.send(f"❌ Cannot pay {mana_cost} to activate **{card.name}**.")
            return

        # Parse "Pay N life" additional cost (e.g. Inventors' Fair).
        life_cost_match = re.search(r'pay\s+(\d+)\s+life', cost_text, re.IGNORECASE)
        if life_cost_match:
            life_cost = int(life_cost_match.group(1))
            if player.life <= life_cost:
                await ctx.send(f"❌ Cannot pay {life_cost} life for **{card.name}** (you have {player.life}).")
                return
            player.life -= life_cost
            player.record_life_loss(life_cost)
            print(f"[ACTIVATE] {card.name}: {player.name} paid {life_cost} life (life now {player.life})")

        # Parse "Discard a card" as a cost (Anje Falkenrath, Wild Mongrel).
        # CR 601.2h — paid before the ability resolves. This parser already
        # ACCEPTS "Discard" as a cost keyword (see the has_other_cost list
        # above), so the ability was offered and then only its {T} charged.
        # Added to both activation paths in one commit: they have a documented
        # history of diverging, and this is the third instance of that class.
        discard_cost_match = re.search(r'discard\s+(a|one|two|three|\d+)\s+cards?',
                                       cost_text, re.IGNORECASE)
        if discard_cost_match or re.search(r'discard your hand', cost_text, re.IGNORECASE):
            if not player.hand:
                await ctx.send(f"❌ Cannot discard for **{card.name}** — your hand is empty.")
                return
            if re.search(r'discard your hand', cost_text, re.IGNORECASE):
                n_discard = len(player.hand)
            else:
                raw = discard_cost_match.group(1).lower()
                n_discard = ({'a': 1, 'one': 1, 'two': 2, 'three': 3}.get(raw)
                             or (int(raw) if raw.isdigit() else 1))
            for _ in range(min(n_discard, len(player.hand))):
                # Through the discard action so discard triggers fire (Anje's
                # own madness untap could never happen without this).
                dm = self.engine.rules._execute_action_on_state(game, {
                    "action": "discard", "player": player.name, "card": "worst",
                })
                if dm:
                    # `messages` doesn't exist yet this early in the function;
                    # send the cost line directly, which is also the right
                    # order — costs are paid before the ability resolves.
                    await ctx.send(dm)
            print(f"[ACTIVATE-COST] {player.name} discards {n_discard} card(s) for {card.name}")

        # Tap the card if needed
        if ability['needs_tap']:
            card.tapped = True
        if counter_cost_match:
            counter_type = counter_cost_match.group(2).strip()
            card.counters[counter_type] -= counter_cost_n
            print(f"[ACTIVATE-COST] {card.name} removes {counter_cost_n} "
                  f"{counter_type} counter(s)")

        # Process exile/sacrifice costs BEFORE effect execution
        cost_lower = cost_text.lower()
        card_name_lower = card.name.lower()
        exiled_self = False
        sacrificed_self = False

        # May 7 audit fix #4: track if a non-self sacrifice cost was paid
        # so the message builder can mention it. Without this, Viscera Seer's
        # "Sacrifice a creature" cost was silently skipped on the human path.
        non_self_sacrificed = None

        if 'exile' in cost_lower and (card_name_lower in cost_lower or 'exile this' in cost_lower or f'exile {card_name_lower}' in cost_lower):
            # Exile this permanent as part of the cost
            if card in player.battlefield:
                game.unregister_static_effects(card)
                player.battlefield.remove(card)
                player.exile.append(card)
                exiled_self = True
        elif 'sacrifice' in cost_lower and (card_name_lower in cost_lower or 'sacrifice this' in cost_lower or 'sacrifice it' in cost_lower or f'sacrifice {card_name_lower}' in cost_lower):
            # Sacrifice this permanent as part of the cost
            if card in player.battlefield:
                # [LAYERS] Unregister static effects before removal
                game.unregister_static_effects(card)
                player.battlefield.remove(card)
                player.graveyard.append(card)
                sacrificed_self = True
                # [SACRIFICE-TRIGGER] Fire Korvold/Mayhem Devil for sac-as-cost
                # (fetchland, Greater Gargadon, etc.) — direct removal path
                # bypassed the sacrifice_permanent action, so Korvold's draw
                # side was silent in the Apr 28 audit. Stash on game so the
                # message-builder a few lines below can pick them up.
                try:
                    from mtg.actions import _fire_sacrifice_triggers
                    game._pending_sac_trigger_msgs = _fire_sacrifice_triggers(
                        self.engine.rules, game, player, card)
                except Exception as e:
                    print(f"[SAC-TRIGGER] manual !activate sac-cost scan failed: {e}")
                    game._pending_sac_trigger_msgs = []
        elif ('sacrifice a creature' in cost_lower
              or 'sacrifice another creature' in cost_lower
              or 'sacrifice a permanent' in cost_lower):
            # May 7 audit fix #4: "Sacrifice a creature" as cost (Viscera Seer,
            # Altar of Dementia, Ashnod's Altar). The human path was missing
            # this — Viscera Seer would scry without paying the sacrifice cost.
            # Auto-select weakest creature (prefer tokens) to sacrifice.
            allow_self = 'another creature' not in cost_lower
            sac_pool = [c for c in player.battlefield
                        if c.is_creature() and (allow_self or c.id != card.id)]
            if not sac_pool:
                await ctx.send(f"❌ Cannot pay sacrifice cost for **{card.name}** (no creatures available).")
                # Roll back the tap from earlier — we couldn't actually activate.
                if ability['needs_tap']:
                    card.tapped = False
                return
            # Prefer tokens, then lowest effective power.
            tokens = [c for c in sac_pool if getattr(c, 'is_token', False)]
            if tokens:
                sac_target = tokens[0]
            else:
                def _safe_power(c):
                    try:
                        return c.get_effective_power(game) if hasattr(c, 'get_effective_power') else 0
                    except (ValueError, TypeError):
                        return int(c.power) if c.power and str(c.power).lstrip('-').isdigit() else 0
                sac_target = min(sac_pool, key=_safe_power)
            game.unregister_static_effects(sac_target)
            player.battlefield.remove(sac_target)
            player.graveyard.append(sac_target)
            non_self_sacrificed = sac_target.name
            print(f"[ACTIVATE-COST] {player.name} sacrificed {sac_target.name} for {card.name}")
            # Fire sacrifice triggers (Korvold, Blood Artist, Mayhem Devil, etc.)
            try:
                from mtg.actions import _fire_sacrifice_triggers
                sac_trig_msgs = _fire_sacrifice_triggers(self.engine.rules, game, player, sac_target)
                if sac_trig_msgs:
                    if not hasattr(game, '_pending_sac_trigger_msgs'):
                        game._pending_sac_trigger_msgs = []
                    game._pending_sac_trigger_msgs.extend(sac_trig_msgs)
            except Exception as e:
                print(f"[SAC-TRIGGER] !activate sac-creature-cost scan failed: {e}")

        # Execute the effect
        effect_text = ability['effect'].lower()

        # Substitute X with actual value in effect text for resolution
        resolved_effect = ability['effect']
        if x_value is not None:
            resolved_effect = re.sub(r'\bX\b', str(x_value), resolved_effect)
            effect_text = resolved_effect.lower()

        messages = [f"⚡ **{card.name}** activates: `{ability['cost']}`"]
        if x_value is not None:
            messages.append(f"🔢 X = {x_value}")
        if exiled_self:
            messages.append(f"📤 **{card.name}** exiled (cost)")
        if sacrificed_self:
            messages.append(f"💀 **{card.name}** sacrificed (cost)")
            # Drain any sacrifice-trigger messages stashed during the sac path
            stashed = getattr(game, '_pending_sac_trigger_msgs', None)
            if stashed:
                messages.extend(stashed)
                game._pending_sac_trigger_msgs = []
        # May 7 audit fix #4: announce non-self sacrifice (Viscera Seer eats a creature).
        if non_self_sacrificed is not None:
            messages.append(f"💀 **{non_self_sacrificed}** sacrificed (cost for {card.name})")
            stashed2 = getattr(game, '_pending_sac_trigger_msgs', None)
            if stashed2:
                messages.extend(stashed2)
                game._pending_sac_trigger_msgs = []

        # Try to parse common effects
        effect_resolved = False

        if card.name.lower() == 'wishclaw talisman':
            msg = self.engine.rules._execute_action_on_state(game, {
                "action": "wishclaw_tutor_transfer", "player": player.name,
                "source": card.name})
            if msg:
                messages.append(msg)
            effect_resolved = True

        if card.name.lower() == 'isochron scepter':
            msg = self.engine.rules._execute_action_on_state(game, {
                "action": "isochron_copy", "player": player.name,
                "source": card.name, "target": target_name or ""})
            if msg:
                messages.append(msg)
            effect_resolved = True

        # === DELAYED TRIGGER: "draw a card at the beginning of the next turn's upkeep" ===
        # Mishra's Bauble, Urza's Bauble. Uses the existing delayed_triggers
        # system (engine._process_delayed_triggers). Schedule with turn_delay=0
        # and trigger_at='upkeep' so it fires on the very next upkeep.
        if re.search(r'draw a card at the beginning of the next turn', effect_text):
            if not hasattr(game, 'delayed_triggers'):
                game.delayed_triggers = []
            game.delayed_triggers.append({
                'trigger_at': 'upkeep',
                'turn_delay': 0,
                'once': True,
                'source': card.name,
                'controller': player_idx,
                'actions': [
                    {'action': 'draw_cards', 'player': player.name, 'amount': 1},
                ],
            })
            messages.append(f"🃏 {card.name} schedules a card draw for next upkeep")
            print(f"[DELAYED-TRIGGER] {card.name}: scheduled draw for {player.name} at next upkeep")

        # === SNEAK ATTACK STYLE: Put creature onto battlefield ===
        if 'put' in effect_text and 'creature' in effect_text and 'battlefield' in effect_text:
            # Find a creature in hand
            creatures_in_hand = [c for c in player.hand if c.is_creature()]
            if not creatures_in_hand:
                messages.append("❌ No creatures in hand to put onto the battlefield!")
            else:
                if target_name:
                    # Find specific creature
                    target_creature = None
                    for c in creatures_in_hand:
                        if target_name.lower() in c.name.lower():
                            target_creature = c
                            break
                    if target_creature:
                        player.hand.remove(target_creature)
                        player.battlefield.append(target_creature)
                        target_creature.entered_this_turn = True
                        # Grant haste if mentioned
                        if 'haste' in effect_text:
                            if 'Haste' not in target_creature.keywords:
                                target_creature.keywords.append('Haste')
                        messages.append(f"🎭 **{target_creature.name}** enters the battlefield with haste!")
                        # Note sacrifice clause
                        if 'sacrifice' in effect_text:
                            messages.append(f"*(Will be sacrificed at end step)*")
                        effect_resolved = True
                    else:
                        messages.append(f"❌ No creature named '{target_name}' in hand")
                else:
                    # Prompt for creature selection
                    game.pending_action = {
                        'type': 'permanent_ability',
                        'card_id': card.id,
                        'ability': ability,
                        'player_idx': player_idx,
                        'effect_type': 'sneak_creature',
                    }
                    lines = [f"🎯 **Choose a creature to put onto the battlefield:**"]
                    for i, c in enumerate(creatures_in_hand):
                        lines.append(f"  `{i}` - {c.name} ({c.power}/{c.toughness})")
                    lines.append(f"\n*Reply with `!target <number>` to select*")
                    messages = lines
                    await ctx.send("\n".join(messages))
                    self.engine.save_game(game)
                    return
        
        # === DEAL DAMAGE ===
        damage_match = re.search(r'deals? (\d+|x) damage', effect_text)
        if damage_match and not effect_resolved:
            damage_str = damage_match.group(1)
            damage = int(damage_str) if damage_str.isdigit() else 0
            
            if damage > 0 and target_name:
                # Find target
                target = None
                for p in game.players:
                    if target_name.lower() in p.name.lower():
                        target = p
                        break
                if not target:
                    for p in game.players:
                        for c in p.battlefield:
                            if target_name.lower() in c.name.lower():
                                target = c
                                break
                
                if target:
                    # [TARGETING] Validate hexproof/protection for activated ability
                    if HAS_TARGETING:
                        if hasattr(target, 'life'):
                            t_legal, t_reason = _validate_player_target_for_action(
                                game, target, card.name, player.name)
                        else:
                            t_legal, t_reason = _validate_target_for_action(
                                game, target, target, card.name, player.name)
                        if not t_legal:
                            messages.append(f"🛡️ **{target.name}** can't be targeted ({t_reason})")
                            await ctx.send("\n".join(messages))
                            return
                    if hasattr(target, 'life'):
                        actual_dmg = self.engine.rules._apply_noncombat_damage_to_player(game, target, damage, card.name, card.id)
                        messages.append(f"🔥 Deals {actual_dmg} damage to **{target.name}** ({target.life} life)")
                    else:
                        target.damage_marked = getattr(target, 'damage_marked', 0) + damage
                        messages.append(f"🔥 Deals {damage} damage to **{target.name}**")
                    effect_resolved = True
                else:
                    messages.append(f"❌ Couldn't find target '{target_name}'")
        
        # === DRAW CARDS ===
        draw_match = re.search(r'draw (\d+|a) cards?', effect_text)
        if draw_match and not effect_resolved:
            amount_str = draw_match.group(1)
            amount = 1 if amount_str == 'a' else int(amount_str)
            drawn = self.draw_cards(player, amount, game=game)
            if drawn:
                messages.append(f"🎴 Drew {len(drawn)} card(s)")
            effect_resolved = True
        
        # === ADD MANA ===
        mana_match = re.search(r'add \{([WUBRGC])\}', effect_text, re.IGNORECASE)
        if mana_match and not effect_resolved:
            color = mana_match.group(1).upper()
            player.mana_pool[color] = player.mana_pool.get(color, 0) + 1
            messages.append(f"💎 Added {{{color}}} to mana pool")
            effect_resolved = True
        
        # === LOTUS BLOOM / BLACK LOTUS STYLE: Add three mana of any one color ===
        if 'add three mana of any one color' in effect_text and not effect_resolved:
            # Check if sacrifice is in the cost
            if 'sacrifice' in ability['cost'].lower():
                # Sacrifice the card
                if card in player.battlefield:
                    game.unregister_static_effects(card)
                    player.battlefield.remove(card)
                    player.graveyard.append(card)
                    messages.append(f"💀 Sacrificed **{card.name}**")

            # For simplicity, let user choose color or default to colorless
            # In a real implementation, we'd prompt for color choice
            # For now, add 3 colorless (user can !fix if they need specific color)
            messages.append(f"💎💎💎 Added 3 mana of any one color to mana pool!")
            messages.append(f"*(Use `!fix set mana W/U/B/R/G +3` to specify color if needed)*")
            effect_resolved = True
        
        # === OTHER SACRIFICE MANA ABILITIES (Mox Opal, Chromatic Sphere, etc.) ===
        sacrifice_mana_match = re.search(r'add (?:one mana of any color|(\{[WUBRGC]\}))', effect_text, re.IGNORECASE)
        if sacrifice_mana_match and 'sacrifice' in ability['cost'].lower() and not effect_resolved:
            # Sacrifice the card
            if card in player.battlefield:
                game.unregister_static_effects(card)
                player.battlefield.remove(card)
                player.graveyard.append(card)
                messages.append(f"💀 Sacrificed **{card.name}**")
            
            if sacrifice_mana_match.group(1):
                color = sacrifice_mana_match.group(1)[1].upper()
                messages.append(f"💎 Added {{{color}}} to mana pool")
            else:
                messages.append(f"💎 Added one mana of any color to mana pool")
            effect_resolved = True

        # === LOOK AT TOP N CARDS AND REORDER (Sensei's Divining Top ability 0, +variants) ===
        # May 24 audit fix: Sensei's Divining Top + similar library-look
        # activated abilities ("Look at the top N cards of your library, then
        # put them back in any order") used to escalate to Tier 3 (Claude
        # API ~$0.005/activation, and Top is ACTIVATED EVERY TURN in decks
        # running it). The reorder_library action at mtg/actions.py:704
        # already has a mana-curve heuristic; wire it here so Sensei's Top
        # resolves deterministically for free.
        look_reorder_match = re.search(
            r'look at the top (?:(\w+) cards?|card) of your library(?:[,\.]| then| and).*?(?:put them back|put it back|rearrange)',
            effect_text,
            re.IGNORECASE,
        )
        if look_reorder_match and not effect_resolved:
            count_word = look_reorder_match.group(1) or 'one'
            _word_to_num = {'one': 1, 'two': 2, 'three': 3, 'four': 4, 'five': 5,
                            'six': 6, 'seven': 7, 'eight': 8, 'nine': 9, 'ten': 10}
            amount = _word_to_num.get(count_word.lower(), 3)  # default 3 for Top
            try:
                amount = int(count_word)
            except (ValueError, TypeError):
                pass
            msg = self.engine.rules._execute_action_on_state(
                game,
                {"action": "reorder_library", "player": player.name, "amount": amount},
            )
            if msg:
                messages.append(msg)
                print(f"[ACTIVATE-LIBRARY-LOOK] {card.name} reorders top {amount}")
                effect_resolved = True

        # === SENSEI'S DIVINING TOP — ability 2: draw, then put Top on top ===
        # The second ability is `{1}, {T}: Draw a card. Then put Sensei's
        # Divining Top on top of its owner's library.` In autoplay we draw
        # 1 (CR-correct), then physically move the card from battlefield to
        # top of library so the next draw step actually re-draws it. This
        # is what makes Top genuinely cyclic — without the move-to-library
        # step, the AI would just re-tap a battlefield permanent forever.
        if (not effect_resolved
                and 'draw a card' in effect_text.lower()
                and ('put ' + card.name.lower() in effect_text.lower()
                     or 'put this on top' in effect_text.lower()
                     or 'put it on top of its owner' in effect_text.lower())):
            # Draw 1 first (uses the same draw_cards helper as other handlers).
            drawn = self.draw_cards(player, 1, game=game)
            # Then move the source card from battlefield to top of library.
            if card in player.battlefield:
                self.engine.unregister_static_effects(card) if hasattr(self.engine, 'unregister_static_effects') else None
                try:
                    game.unregister_static_effects(card)
                except Exception:
                    pass
                player.battlefield.remove(card)
                # `library[0]` is the TOP per the engine's convention (draws
                # come from index 0). Insert at position 0 = put on top.
                player.library.insert(0, card)
                # Reset per-zone state so the next time it's drawn + cast/
                # played it acts as a fresh permanent.
                card.tapped = False
                card.entered_this_turn = False
            drawn_count = len(drawn) if drawn else 0
            messages.append(
                f"🔮 **{card.name}**: drew {drawn_count} card, "
                f"then put **{card.name}** back on top of library"
            )
            print(f"[ACTIVATE-TOP-CYCLE] {card.name} drew 1 + returned to top of library")
            effect_resolved = True

        # === TIER 1.5: Try effect template library (card name + pattern matching) ===
        if not effect_resolved and HAS_EFFECT_TEMPLATES:
            try:
                opponent = game.players[1 - player_idx]
                effect_desc = resolved_effect if x_value is not None else ability['effect']
                ctx = build_game_context(game, player, opponent, card=card)
                if x_value is not None:
                    ctx['x_value'] = int(x_value)
                lib = get_effect_library()
                tmpl_actions, tmpl_explanation = lib.resolve_etb(
                    card_name=card.name,
                    oracle_text=effect_desc,
                    controller=player.name,
                    opponent=opponent.name,
                    game_context=ctx,
                )
                if tmpl_actions is not None:
                    for action in tmpl_actions:
                        if action.get("action") != "no_action":
                            try:
                                msg = self.engine.rules._execute_action_on_state(game, action)
                                if msg:
                                    messages.append(msg)
                            except Exception as ae:
                                print(f"[ACTIVATE-TEMPLATE] Action failed: {ae}")
                    effect_resolved = True
                    print(f"[ACTIVATE-TEMPLATE] {card.name} ability resolved via template library: {tmpl_explanation}")
            except Exception as e:
                print(f"[ACTIVATE-TEMPLATE] Error for {card.name}: {e}")

        # === TIER 2: Try SpellResolver for pattern-matched effects ===
        if not effect_resolved and self.engine.spell_resolver:
            try:
                effect_desc = resolved_effect if x_value is not None else ability['effect']
                parsed_effects = self.engine.spell_resolver.effect_executor.parse_effects(effect_desc)
                # Filter out COMPLEX effects — those are just "I couldn't parse it"
                actionable_effects = [e for e in parsed_effects if e.effect_type.name != 'COMPLEX']

                if actionable_effects:
                    # Build execution context for the ability
                    exec_ctx = ExecutionContext(
                        game_state=game,
                        source_card=card,
                        source_controller=player,
                        targets=[],
                        x_value=x_value or 0,
                    )

                    # Auto-select targets from opponent if needed
                    opponent = game.players[1 - player_idx]
                    target_patterns = re.findall(r'target (creature|permanent|player|planeswalker|artifact|enchantment)', effect_desc.lower())
                    if target_patterns:
                        for tp in target_patterns:
                            if tp == 'player':
                                exec_ctx.targets.append(opponent)
                            elif tp in ('creature', 'permanent', 'artifact', 'enchantment'):
                                for c in opponent.battlefield:
                                    if tp == 'creature' and c.is_creature():
                                        exec_ctx.targets.append(c)
                                        break
                                    elif tp == 'artifact' and c.is_artifact():
                                        exec_ctx.targets.append(c)
                                        break
                                    elif tp == 'enchantment' and c.is_enchantment():
                                        exec_ctx.targets.append(c)
                                        break
                                    elif tp == 'permanent':
                                        exec_ctx.targets.append(c)
                                        break

                    for eff in actionable_effects:
                        eff_msgs = await self.engine.spell_resolver._execute_effect(eff, exec_ctx, game)
                        messages.extend(eff_msgs)

                    effect_resolved = True
                    print(f"[ACTIVATE-SPELL_RESOLVER] {card.name} ability resolved via SpellResolver: {len(actionable_effects)} effects")
            except Exception as e:
                print(f"[ACTIVATE-SPELL_RESOLVER] Error resolving {card.name} ability via SpellResolver: {e}")

        # === TIER 2.5: Try XMage action translator (dedicated regex patterns) ===
        if not effect_resolved and getattr(self.engine, '_xmage_translator', None):
            try:
                effect_desc = resolved_effect if x_value is not None else ability['effect']
                opponent = game.players[1 - player_idx]
                # The translator has its own regex patterns beyond what SpellResolver covers
                # (e.g., "deals damage equal to that creature's power", "each opponent" patterns)
                t_actions, t_expl = self.engine._xmage_translator.translate_trigger(
                    source_card=card.name,
                    ability_text=effect_desc,
                    controller=player.name,
                    opponent=opponent.name,
                    game_context=build_game_context(game, player, opponent, card=card) if HAS_EFFECT_TEMPLATES else None,
                )
                if t_actions is not None:
                    for action in t_actions:
                        if action.get("action") != "no_action":
                            try:
                                msg = self.engine.rules._execute_action_on_state(game, action)
                                if msg:
                                    messages.append(msg)
                            except Exception as ae:
                                print(f"[ACTIVATE-XMAGE] Action failed: {ae}")
                    effect_resolved = True
                    print(f"[ACTIVATE-XMAGE] {card.name} ability resolved via XMage translator: {t_expl}")
            except Exception as e:
                print(f"[ACTIVATE-XMAGE] Error for {card.name}: {e}")

        # === TIER 3: Claude API fallback for complex effects ===
        if not effect_resolved:
            try:
                effect_desc = resolved_effect if x_value is not None else ability['effect']
                resolve_msgs, resolve_actions = await self.engine.rules.resolve_effect(
                    game,
                    effect_description=f"{card.name}'s activated ability: {effect_desc}",
                    source_card=card.name,
                    controller=player.name,
                )
                if resolve_actions:
                    messages.extend(resolve_msgs)
                    effect_resolved = True
                    print(f"[ACTIVATE-RESOLVE] {card.name} ability resolved via Claude API: {len(resolve_actions)} actions")
                else:
                    # Claude API returned no actions — fall back to manual
                    messages.append(f"📜 Effect: *{ability['effect']}*")
                    messages.append(f"*(Manual resolution may be needed - use `!judge` for complex effects)*")
                    game.last_unresolved_effect = {
                        'effect': ability['effect'],
                        'source': card.name,
                        'controller': player.name,
                    }
            except Exception as e:
                print(f"[ACTIVATE-RESOLVE] Error resolving {card.name} ability via Claude API: {e}")
                messages.append(f"📜 Effect: *{ability['effect']}*")
                messages.append(f"*(Manual resolution may be needed - use `!judge` for complex effects)*")
                game.last_unresolved_effect = {
                    'effect': ability['effect'],
                    'source': card.name,
                    'controller': player.name,
                }

        for msg in messages:
            await ctx.send(msg)

        # Check state-based actions
        events = self.engine.check_state_based_actions(game)
        if events:
            await ctx.send("\n".join(f"⚡ {e}" for e in events))
        
        self.engine.save_game(game)

    @commands.command(name="target")
    async def select_target(self, ctx, target_index: int):
        """
        Select a target for a pending ability.
        
        Usage:
            !target 0    → Select target #0 from the list
        """
        game = self._get_game(ctx)
        if not game:
            await ctx.send("No active game in this thread!")
            return
        
        if not game.pending_action:
            await ctx.send("No pending target selection!")
            return
        
        action_type = game.pending_action.get('type')
        
        player_idx = game.get_player_index(ctx.author.id)
        if player_idx != game.pending_action.get('player_idx'):
            await ctx.send("It's not your ability to target for!")
            return
        
        player = game.players[player_idx]
        
        # Handle permanent ability targeting (Sneak Attack, etc.)
        if action_type == 'permanent_ability':
            effect_type = game.pending_action.get('effect_type')
            
            if effect_type == 'sneak_creature':
                creatures_in_hand = [c for c in player.hand if c.is_creature()]
                if target_index < 0 or target_index >= len(creatures_in_hand):
                    await ctx.send(f"Invalid target! Choose 0-{len(creatures_in_hand)-1}")
                    return
                
                target_creature = creatures_in_hand[target_index]
                ability = game.pending_action.get('ability', {})
                effect_text = ability.get('effect', '').lower()
                
                player.hand.remove(target_creature)
                player.battlefield.append(target_creature)
                target_creature.entered_this_turn = True
                
                # Grant haste
                if 'haste' in effect_text or 'Sneak Attack' in game.pending_action.get('card_id', ''):
                    if 'Haste' not in target_creature.keywords:
                        target_creature.keywords.append('Haste')
                
                await ctx.send(f"🎭 **{target_creature.name}** enters the battlefield with haste!")
                if 'sacrifice' in effect_text:
                    await ctx.send(f"*(Will be sacrificed at beginning of next end step)*")
                
                game.pending_action = None
                self.engine.save_game(game)
                return
            else:
                await ctx.send("Unknown pending action type!")
                game.pending_action = None
                return
        
        # Handle planeswalker targeting
        elif action_type == 'planeswalker_target':
            legal_targets = game.pending_action.get('legal_targets', [])
            if target_index < 0 or target_index >= len(legal_targets):
                await ctx.send(f"Invalid target index! Choose 0-{len(legal_targets)-1}")
                return
            
            target, desc = legal_targets[target_index]
            targets = [target]
            
            card_id = game.pending_action['card_id']
            ability_index = game.pending_action['ability_index']
            
            card = None
            for c in player.battlefield:
                if c.id == card_id:
                    card = c
                    break
            
            if not card:
                await ctx.send("Planeswalker no longer on battlefield!")
                game.pending_action = None
                return
            
            game.pending_action = None
            
            pw_manager = self.engine.planeswalker_manager
            result = await pw_manager.activate(game, player, card, ability_index, targets)
            
            for msg in result.messages:
                await ctx.send(msg)
            
            events = self.engine.check_state_based_actions(game)
            if events:
                await ctx.send("\n".join(f"⚡ {e}" for e in events))
            
            self.engine.save_game(game)

        # Handle Chandra dual-target: stage 1 — player selection
        elif action_type == 'chandra_dual_target_player':
            player_dmg = game.pending_action['player_dmg']
            creature_dmg = game.pending_action['creature_dmg']
            card_id = game.pending_action['card_id']
            ability_index = game.pending_action['ability_index']

            if target_index < 0 or target_index >= len(game.players):
                await ctx.send(f"Invalid target! Choose 0-{len(game.players)-1}")
                return

            target_player = game.players[target_index]

            # Find and activate the planeswalker
            card = None
            for c in player.battlefield:
                if c.id == card_id:
                    card = c
                    break
            if not card:
                await ctx.send("Planeswalker no longer on battlefield!")
                game.pending_action = None
                return

            pw_manager = self.engine.planeswalker_manager
            # Loyalty cost already paid in the first stage (chandra_dual_target_player handler)
            # Just apply the damage here
            actual_dmg = self.engine.rules._apply_noncombat_damage_to_player(game, target_player, player_dmg, card.name, card.id)
            await ctx.send(f"🔥 {card.name} deals {actual_dmg} damage to {target_player.name} (Life: {target_player.life})")

            # Stage 2: creature target
            target_creatures = [c for c in target_player.creatures() if c.is_creature()]
            if target_creatures:
                game.pending_action = {
                    'type': 'chandra_dual_target_creature',
                    'card_id': card_id,
                    'player_idx': player_idx,
                    'creature_dmg': creature_dmg,
                    'target_player_name': target_player.name,
                    'target_player_idx': target_index,
                }
                lines = [f"🎯 **Optionally choose a creature {target_player.name} controls:**"]
                for i, c in enumerate(target_creatures):
                    lines.append(f"  `{i}` - {c.name} ({c.power}/{c.toughness})")
                lines.append(f"  `{len(target_creatures)}` - No creature target (skip)")
                lines.append(f"\n*Reply with `!target <number>` to select*")
                await ctx.send("\n".join(lines))
            else:
                await ctx.send(f"*No creatures controlled by {target_player.name} to target.*")
                game.pending_action = None

            self.engine.save_game(game)

        # Handle Chandra dual-target: stage 2 — creature selection
        elif action_type == 'chandra_dual_target_creature':
            creature_dmg = game.pending_action['creature_dmg']
            target_player_idx = game.pending_action['target_player_idx']
            target_player = game.players[target_player_idx]
            target_creatures = [c for c in target_player.creatures() if c.is_creature()]

            # Check if player chose "skip"
            if target_index == len(target_creatures):
                await ctx.send("*No creature targeted.*")
                game.pending_action = None
                self.engine.save_game(game)
                return

            if target_index < 0 or target_index >= len(target_creatures):
                await ctx.send(f"Invalid target! Choose 0-{len(target_creatures)} (last option = skip)")
                return

            target_creature = target_creatures[target_index]
            target_creature.damage_marked += creature_dmg
            msgs = [f"🔥 Deals {creature_dmg} damage to {target_creature.name}"]

            # "That creature can't block this turn" effect
            ability_lower = ""
            card_id = game.pending_action['card_id']
            for c in player.battlefield:
                if c.id == card_id and c.oracle_text:
                    ability_lower = c.oracle_text.lower()
                    break
            if "can't block this turn" in ability_lower:
                if 'Defender' not in target_creature.temp_keywords:
                    # Mark creature as unable to block (use temp keyword hack)
                    target_creature.temp_keywords.append('CantBlock')
                msgs.append(f"🚫 {target_creature.name} can't block this turn")

            for msg in msgs:
                await ctx.send(msg)

            game.pending_action = None
            events = self.engine.check_state_based_actions(game)
            if events:
                await ctx.send("\n".join(f"⚡ {e}" for e in events))
            self.engine.save_game(game)

        # Handle "pay any amount of life" ETB (Phyrexian Processor)
        elif action_type == 'pay_life_etb':
            card_name = game.pending_action['card_name']
            card_id = game.pending_action['card_id']
            life_to_pay = max(0, target_index)  # target_index used as life amount

            if life_to_pay > player.life:
                await ctx.send(f"⚠️ You only have {player.life} life! Choose a smaller amount.")
                return

            player.life -= life_to_pay
            player.record_life_loss(life_to_pay)
            # Store the paid amount on the card for later (token creation ability uses it)
            for c in player.battlefield:
                if c.id == card_id:
                    c.counters['life_paid'] = life_to_pay
                    break

            if life_to_pay > 0:
                await ctx.send(f"🩸 Paid {life_to_pay} life for **{card_name}** (Life: {player.life})")
            else:
                await ctx.send(f"💀 Chose to pay 0 life for **{card_name}**")

            game.pending_action = None
            self.engine.save_game(game)

        else:
            await ctx.send("No pending target selection!")
            return

    @commands.command(name="replacement", aliases=["replace"])
    async def choose_replacement(self, ctx, choice: int):
        """
        Choose which replacement effect applies when multiple compete.

        Usage:
            !replacement 0    → Select replacement effect #0 from the list
            !replace 1        → Select replacement effect #1
        """
        game = self._get_game(ctx)
        if not game:
            await ctx.send("No active game in this thread!")
            return

        if not game.pending_action or game.pending_action.get('type') != 'choose_replacement':
            await ctx.send("No pending replacement choice!")
            return

        player_idx = game.get_player_index(ctx.author.id)
        if player_idx != game.pending_action.get('player_idx'):
            await ctx.send("It's not your choice to make!")
            return

        effects = game.pending_action.get('effects', [])
        if choice < 0 or choice >= len(effects):
            await ctx.send(f"Invalid choice! Choose 0-{len(effects)-1}")
            return

        chosen = effects[choice]
        future = game.pending_action.get('future')

        game.pending_action = None

        if future and not future.done():
            future.set_result(chosen)
            await ctx.send(f"✅ Applied: **{chosen.source_name}** — {chosen.description}")
        else:
            await ctx.send("⚠️ Replacement choice already resolved or timed out.")

        self.engine.save_game(game)

    @commands.command(name="loyalty", aliases=["loy"])
    async def show_loyalty(self, ctx, *, card_name: str = ""):
        """
        Show planeswalker loyalty and abilities.

        Usage:
            !loyalty           → Show all your planeswalkers
            !loyalty Chandra   → Show specific planeswalker
        """
        game = self._get_game(ctx)
        if not game:
            await ctx.send("No active game in this thread!")
            return
        
        player_idx = game.get_player_index(ctx.author.id)
        if player_idx is None:
            await ctx.send("You're not in this game!")
            return
        
        player = game.players[player_idx]
        
        if not self.engine.planeswalker_manager:
            planeswalkers = [c for c in player.battlefield if c.is_planeswalker()]
            if not planeswalkers:
                await ctx.send("You don't control any planeswalkers!")
                return
            
            lines = ["**Your Planeswalkers:**"]
            for pw in planeswalkers:
                lines.append(f"• **{pw.name}** - Loyalty: {pw.loyalty_counters}")
            await ctx.send("\n".join(lines))
            return
        
        pw_manager = self.engine.planeswalker_manager
        
        if card_name:
            card_name_lower = card_name.lower()
            card = None
            for c in player.battlefield:
                if c.is_planeswalker() and card_name_lower in c.name.lower():
                    card = c
                    break
            
            if not card:
                await ctx.send(f"You don't control a planeswalker matching '{card_name}'")
                return
            
            await ctx.send(pw_manager.get_ability_display(card))
        else:
            planeswalkers = [c for c in player.battlefield if c.is_planeswalker()]
            if not planeswalkers:
                await ctx.send("You don't control any planeswalkers!")
                return
            
            lines = ["**Your Planeswalkers:**"]
            for pw in planeswalkers:
                lines.append(pw_manager.get_ability_display(pw))
            await ctx.send("\n\n".join(lines))

    @commands.command(name="priority", aliases=["pri", "stack"])
    async def show_priority(self, ctx):
        """
        Show current priority state and stack.
        
        Usage:
            !priority    → Show who has priority and what's on the stack
            !stack       → Same as above
        """
        game = self._get_game(ctx)
        if not game:
            await ctx.send("No active game in this thread!")
            return
        
        if self.integrated:
            display = self.integrated.get_priority_display(ctx.channel.id)
            await ctx.send(display)
        else:
            # Fallback display
            current = game.players[game.active_player_index].name
            await ctx.send(f"**Turn {game.turn_number}** - {PHASE_NAMES[game.phase]}\n"
                          f"Active player: {current}\n"
                          f"*Stack is empty*")

    # !legend command removed May 17, 2026. The legend rule now runs through
    # SBA (rules/sba_adapter.py — see Bug 3 fix); the controller's "keep one,
    # sacrifice the rest" choice is handled by the SBA dispatch heuristic
    # (keep most-recently-added). The old command depended on self.integrated
    # which has been hardwired to None since the Apr 2026 integrated-engine
    # deprecation — every invocation returned "requires the integrated engine"
    # and did nothing.

    @commands.command(name="f6", aliases=["yield"])
    async def yield_priority(self, ctx):
        """
        Auto-pass priority until end of turn (F6 mode).
        
        Usage:
            !f6      → Pass priority for all remaining steps this turn
            !yield   → Same as above
        """
        game = self._get_game(ctx)
        if not game:
            await ctx.send("No active game in this thread!")
            return
        
        player_idx = game.get_player_index(ctx.author.id)
        if player_idx is None:
            await ctx.send("You're not in this game!")
            return
        
        player_name = game.players[player_idx].name

        # Use game's own PrioritySystem if stack is enabled
        if game.stack_enabled and game._priority_system:
            result = await game._priority_system.set_auto_pass(
                player_name, until="end_of_turn")
            if result.get("success"):
                await ctx.send(f"⏩ **{player_name}** yields until end of turn (F6)")
            else:
                await ctx.send(f"⚠️ {result.get('message', 'Could not set auto-pass')}")
        elif self.integrated:
            result = await self.integrated.handle_priority_message(
                ctx.channel.id, player_name, "f6", game
            )
            if result.handled:
                await ctx.send(f"⏩ **{player_name}** yields until end of turn")
        else:
            await ctx.send("⏩ Priority yield noted (stack system not active this game).")

    @commands.command(name="respond", aliases=["resp", "counter"])
    async def respond_to_stack(self, ctx, *, card_name: str):
        """
        Respond to a spell on the stack with an instant or flash card.

        Usage:
            !respond Counterspell          - Counter the top spell on the stack
            !respond Lightning Bolt target Bob - Cast instant in response
        """
        game = self._get_game(ctx)
        if not game:
            await ctx.send("No active game in this thread!")
            return

        if not game.stack_enabled or not game.stack:
            await ctx.send("❌ No spells on the stack to respond to.")
            return

        player_idx = game.get_player_index(ctx.author.id)
        if player_idx is None:
            await ctx.send("You're not in this game!")
            return

        player = game.players[player_idx]

        # Parse target from card_name string (e.g., "Lightning Bolt target Bob")
        target = None
        target_name = None
        if ' target ' in card_name.lower():
            parts = card_name.lower().split(' target ', 1)
            card_name = parts[0].strip()
            target_name = parts[1].strip()

        # Find the card in hand
        card = player.find_card(card_name, Zone.HAND)
        if not card:
            await ctx.send(f"❌ '{card_name}' not found in your hand.")
            return

        # Validate: must be instant or have flash
        if not card.is_instant() and not (card.oracle_text and 'flash' in card.oracle_text.lower()):
            await ctx.send(f"❌ **{card.name}** is not an instant and doesn't have flash. "
                           f"You can only respond with instant-speed cards.")
            return

        # Check mana
        can_pay, reason = player.can_pay_mana_cost(card.mana_cost)
        if not can_pay:
            await ctx.send(f"❌ Can't cast {card.name}: {reason}")
            return

        # Resolve target
        if target_name:
            for p in game.players:
                target = p.find_card(target_name, Zone.BATTLEFIELD)
                if target:
                    break
            if not target:
                for p in game.players:
                    if p.name.lower() == target_name.lower():
                        target = p
                        break

        # Cast the response spell through cast_spell_async
        # This will push it onto the stack too
        success, msg, effect_msgs = await self.engine.cast_spell_async(
            game, player, card, target=target
        )

        if success:
            await ctx.send(f"⚡ **{player.name}** responds with **{card.name}**!")
            for em in (effect_msgs or []):
                await ctx.send(em)

            # Check SBA
            events = self.engine.check_state_based_actions(game)
            for e in events:
                await ctx.send(f"⚡ {e}")

            self.engine.save_game(game)
        else:
            await ctx.send(f"❌ Could not cast {card.name}: {msg}")

    @commands.command(name="attack")
    async def declare_attackers(self, ctx, *, creatures: str = "all"):
        """
        Declare attackers.

        Usage:
            !attack all                    - Attack with all untapped creatures
            !attack Goblin Guide, Swiftspear - Attack with specific creatures
        """
        game = self._get_game(ctx)
        if not game:
            await ctx.send("No active game in this thread!")
            return
        
        player_idx = game.get_player_index(ctx.author.id)
        if player_idx is None or player_idx != game.active_player_index:
            await ctx.send(self._not_your_turn_msg(game))
            return

        player = game.players[player_idx]
        # Undo snapshot: capture pre-attack state. Declaring attackers taps
        # creatures + commits to combat, so being able to back out is useful
        # when the AI rejected a block-tricks-considered analysis.
        self._snapshot_for_undo(game, f"{player.name} declared attackers ({creatures})")

        # Get potential attackers
        if creatures.lower() == "all":
            potential_attackers = player.untapped_creatures()
            # Debug: log all creatures and why some are excluded
            all_creatures = player.creatures()
            if len(all_creatures) != len(potential_attackers):
                for c in all_creatures:
                    if c not in potential_attackers:
                        print(f"[ATTACK-DEBUG] {c.name}: excluded from !attack all "
                              f"(tapped={c.tapped}, sick={c.summoning_sick}, "
                              f"is_creature={c.is_creature()}, type_line='{c.type_line}')")
        else:
            creature_names = [c.strip() for c in creatures.split(",")]
            potential_attackers = []
            for name in creature_names:
                card = player.find_card(name, Zone.BATTLEFIELD)
                if card and card.is_creature():
                    potential_attackers.append(card)
        
        # Validate each attacker with rules engine
        attackers = []
        warnings = []
        for card in potential_attackers:
            can_attack, reason = self.engine.rules.can_attack_with(game, player, card)
            if can_attack:
                paid, tax_reason = self.engine.rules.pay_attack_tax(game, player, card)
                if paid:
                    attackers.append(card)
                else:
                    warnings.append(f"⚠️ {card.name}: {tax_reason}")
            else:
                warnings.append(f"⚠️ {card.name}: {reason}")
        
        if warnings:
            await ctx.send("\n".join(warnings))
        
        if not attackers:
            await ctx.send("No valid attackers!")
            return
        
        # Declare attacks
        game.attackers = []
        game.phase = Phase.DECLARE_ATTACKERS
        
        for card in attackers:
            card.attacking = True
            card.attacking_player = 1 - player_idx
            # Tap attacker (unless vigilance)
            if not card.has_vigilance():
                self.engine.tap_permanent(card)
            game.attackers.append(card.id)
            self.engine.rules.log_event(f"{card.name} attacks")
        
        attacker_display = [f"{c.name}" + (" (vigilance)" if c.has_vigilance() else "") for c in attackers]
        await ctx.send(f"⚔️ **{player.name}** attacks with: {', '.join(attacker_display)}")
        
        # Process attack triggers (Jaya's -2, etc.)
        trigger_msgs = self.engine.process_attack_triggers(game, player_idx)
        for msg in trigger_msgs:
            await ctx.send(msg)
        
        # Check state-based actions (creatures dying from trigger damage, etc.)
        sba_msgs = self.engine.check_state_based_actions(game)
        for msg in sba_msgs:
            await ctx.send(msg)
        
        # Combat priority window: after attackers declared (removal before blocks)
        if game.stack_enabled:
            await self.engine._combat_priority_round(game, ctx.send, "after attackers declared")
            # Check if any attackers died from instant-speed removal
            attackers = [c for c in attackers if c in player.battlefield and c.attacking]
            if not attackers:
                await ctx.send("⚔️ No attackers remain after priority — skipping to end of combat.")
                game.phase = Phase.COMBAT_END
                self.engine.save_game(game)
                return

        # If opponent is Claude, have it block
        opponent = game.players[1 - player_idx]
        if opponent.is_claude:
            await asyncio.sleep(1)
            blocks = await self.engine.claude_ai.decide_blocks(game, 1 - player_idx, attackers)
            
            if blocks:
                # May 7 audit fix #2: disambiguate same-name creatures
                # ("Plant blocks Plant" repeated for 8 separate combats).
                name_counts3 = {}
                for a_id3 in game.attackers:
                    ar3 = game.find_card_global(a_id3)
                    if ar3:
                        name_counts3[ar3[0].name] = name_counts3.get(ar3[0].name, 0) + 1
                for blocker_ids3 in blocks.values():
                    for b_id3 in blocker_ids3 or []:
                        br3 = game.find_card_global(b_id3)
                        if br3:
                            name_counts3[br3[0].name] = name_counts3.get(br3[0].name, 0) + 1
                name_index3 = {}
                name_running3 = {}
                def _label_for3(card):
                    if card.id in name_index3:
                        return name_index3[card.id]
                    if name_counts3.get(card.name, 0) > 1:
                        idx = name_running3.get(card.name, 0) + 1
                        name_running3[card.name] = idx
                        label = f"{card.name} #{idx}"
                    else:
                        label = card.name
                    name_index3[card.id] = label
                    return label

                block_msgs = []
                for attacker_id, blocker_ids in blocks.items():
                    if blocker_ids:
                        atk_result = game.find_card_global(attacker_id)
                        if not atk_result:
                            print(f"[COMBAT] Claude tried to block attacker ID '{attacker_id[:8]}' but couldn't find it")
                            continue
                        attacker = atk_result[0]
                        attacker_label = _label_for3(attacker)
                        blk_names = []
                        for blocker_id in blocker_ids:
                            blk_result = game.find_card_global(blocker_id)
                            if not blk_result:
                                print(f"[COMBAT] Claude tried to block with ID '{blocker_id[:8]}' but couldn't find it")
                                continue
                            blocker = blk_result[0]
                            # Register block the same way declare_blocker does
                            blocker.blocking.append(attacker.id)
                            attacker.blocked_by.append(blocker.id)
                            if attacker.id not in game.blockers:
                                game.blockers[attacker.id] = []
                            game.blockers[attacker.id].append(blocker.id)
                            blk_names.append(_label_for3(blocker))
                        # May 20 audit fix: skip the append when no blocker
                        # names resolved — `find_card_global` returning None
                        # for every blocker_id in this group would otherwise
                        # yield " blocks Brago, King Eternal" with a leading
                        # empty join (visible in game_1506604518098342018:328,
                        # 367-368; game_1506618495738052648:209,264,339 etc).
                        # May 23 audit (MAJOR #11): list-empty check wasn't
                        # enough — `_label_for3` was returning empty strings
                        # for some inputs, so `blk_names == ['']` slipped
                        # past `if not blk_names` and emitted "• blocks Spell
                        # Queller" (game_1507600803190538301:92 + 67 others).
                        # Filter empties before deciding.
                        blk_names = [n for n in blk_names if n and n.strip()]
                        if not blk_names:
                            continue
                        block_msgs.append(f"{', '.join(blk_names)} blocks {attacker_label}")
                if block_msgs:
                    await ctx.send(f"🛡️ **Claude** blocks:\n" + "\n".join(f"• {b}" for b in block_msgs))
            else:
                await ctx.send("🛡️ **Claude** doesn't block.")

            # Combat priority window: after blockers declared (combat tricks!)
            if game.stack_enabled:
                await self.engine._combat_priority_round(game, ctx.send, "after blockers declared")

            # Resolve combat damage
            await self._resolve_combat(ctx, game)
        else:
            await ctx.send(f"{opponent.name}, declare blockers with `!block <attacker> with <blocker>`, then `!doneblocking` (or `!noblock` for no blocks)")
    
    @commands.command(name="block")
    async def declare_blocker(self, ctx, *, block_str: str):
        """
        Declare a blocker.
        
        Usage:
            !block Tarmogoyf with Snapcaster Mage
        """
        game = self._get_game(ctx)
        if not game:
            await ctx.send("No active game in this thread!")
            return
        
        # Parse "X with Y"
        match = re.match(r'(.+?)\s+with\s+(.+)', block_str, re.IGNORECASE)
        if not match:
            await ctx.send("Usage: `!block <attacker> with <blocker>`")
            return
        
        attacker_name = match.group(1).strip()
        blocker_name = match.group(2).strip()
        
        player_idx = game.get_player_index(ctx.author.id)
        if player_idx is None:
            await ctx.send("You're not in this game!")
            return
        
        player = game.players[player_idx]
        blocker = player.find_card(blocker_name, Zone.BATTLEFIELD)
        
        if not blocker or not blocker.is_creature():
            await ctx.send(f"Couldn't find creature '{blocker_name}' on your battlefield.")
            return
        
        # Find attacker
        opponent = game.players[1 - player_idx]
        attacker = opponent.find_card(attacker_name, Zone.BATTLEFIELD)
        
        if not attacker or attacker.id not in game.attackers:
            await ctx.send(f"'{attacker_name}' isn't attacking!")
            return
        
        # Rules check for blocking
        can_block, reason = self.engine.rules.can_block_with(game, player, blocker, attacker)
        if not can_block:
            await ctx.send(f"⚠️ {reason}")
            return
        
        # Assign block
        blocker.blocking.append(attacker.id)
        attacker.blocked_by.append(blocker.id)
        
        if attacker.id not in game.blockers:
            game.blockers[attacker.id] = []
        game.blockers[attacker.id].append(blocker.id)
        
        self.engine.rules.log_event(f"{blocker.name} blocks {attacker.name}")
        await ctx.send(f"🛡️ **{blocker.name}** blocks **{attacker.name}**")
    
    @commands.command(name="noblock")
    async def no_blockers(self, ctx):
        """Decline to block (or finalize blocks) and proceed to damage."""
        game = self._get_game(ctx)
        if not game:
            await ctx.send("No active game in this thread!")
            return

        player_idx = game.get_player_index(ctx.author.id)
        if player_idx is None or player_idx == game.active_player_index:
            await ctx.send("Only the defending player can decline blocks!")
            return

        if game.blockers:
            # Blocks were already declared — finalize them
            block_msgs = []
            for atk_id, blk_ids in game.blockers.items():
                atk_result = game.find_card_global(atk_id)
                if atk_result:
                    atk_name = atk_result[0].name
                    blk_names = []
                    for b_id in blk_ids:
                        b_result = game.find_card_global(b_id)
                        if b_result:
                            blk_names.append(b_result[0].name)
                    block_msgs.append(f"{', '.join(blk_names)} blocking {atk_name}")
            if block_msgs:
                await ctx.send("🛡️ Finalizing blocks: " + "; ".join(block_msgs))
        else:
            await ctx.send("🛡️ No blockers declared.")

        # Combat priority window: after blockers declared (combat tricks!)
        if game.stack_enabled:
            await self.engine._combat_priority_round(game, ctx.send, "after blockers declared")

        await self._resolve_combat(ctx, game)

    @commands.command(name="doneblocking")
    async def done_blocking(self, ctx):
        """Finalize declared blocks and proceed to combat damage."""
        game = self._get_game(ctx)
        if not game:
            await ctx.send("No active game in this thread!")
            return

        player_idx = game.get_player_index(ctx.author.id)
        if player_idx is None or player_idx == game.active_player_index:
            await ctx.send("Only the defending player can finalize blocks!")
            return

        if not game.attackers:
            await ctx.send("No combat to resolve!")
            return

        if game.blockers:
            block_msgs = []
            for atk_id, blk_ids in game.blockers.items():
                atk_result = game.find_card_global(atk_id)
                if atk_result:
                    atk_name = atk_result[0].name
                    blk_names = []
                    for b_id in blk_ids:
                        b_result = game.find_card_global(b_id)
                        if b_result:
                            blk_names.append(b_result[0].name)
                    block_msgs.append(f"{', '.join(blk_names)} blocking {atk_name}")
            await ctx.send("🛡️ Blocks confirmed: " + "; ".join(block_msgs))
        else:
            await ctx.send("🛡️ No blockers declared, proceeding to damage.")

        # Combat priority window: after blockers declared (combat tricks!)
        if game.stack_enabled:
            await self.engine._combat_priority_round(game, ctx.send, "after blockers declared")

        await self._resolve_combat(ctx, game)

    @commands.command(name="combat")
    async def resolve_combat(self, ctx):
        """Resolve combat after blocks are declared."""
        game = self._get_game(ctx)
        if not game:
            await ctx.send("No active game in this thread!")
            return

        if not game.attackers:
            await ctx.send("No attackers to resolve!")
            return
        
        await self._resolve_combat(ctx, game)
    
    @commands.command(name="judge")
    async def ask_judge(self, ctx, *, question: str = ""):
        """
        Ask the judge to rule on a rules question AND apply game state changes.

        The judge has hands — it doesn't just explain what should happen, it
        actually modifies the game state. Three-stage resolution:
        1. Try structured effect resolution (fast, reliable)
        2. If that fails, ask the judge for a ruling WITH executable actions
        3. Apply any actions to the game state

        Usage:
            !judge                                    → resolve last pending effect
            !judge Mystic Sanctuary ETB — put Counterspell on top of library
            !judge Resolve Terror of the Peaks trigger — 5/5 creature entered
            !judge Does first strike damage happen before regular damage resolves?
        """
        game = self._get_game(ctx)

        # If no question provided, try to use last unresolved effect
        if not question.strip():
            if game and game.last_unresolved_effect:
                effect_info = game.last_unresolved_effect
                question = f"Resolve {effect_info['source']}'s ability: {effect_info['effect']}"
                await ctx.send(f"🔄 Resolving pending effect: **{effect_info['source']}** — *{effect_info['effect']}*")
                game.last_unresolved_effect = None  # Clear after use
            else:
                await ctx.send("❌ No question provided and no pending effect to resolve. Usage: `!judge <question>`")
                return

        async with ctx.typing():
            if game:
                player_idx = game.get_player_index(ctx.author.id)
                player = game.players[player_idx] if player_idx is not None else None

                # Detect if this looks like it needs game state changes
                # (mentions specific cards, triggers, ETBs, damage, etc.)
                q_lower = question.lower()
                needs_execution = any(keyword in q_lower for keyword in [
                    'resolve', 'trigger', 'etb', 'enters', 'damage', 'destroy',
                    'draw', 'discard', 'counter', 'token', 'dies', 'sacrifice',
                    'tap', 'untap', 'gain life', 'lose life', 'mana',
                ])
                # Undo snapshot: only when this !judge call is going to mutate
                # state (matched by needs_execution above). Plain rule-question
                # !judge calls don't change game state, so snapshotting would
                # just bloat the stack with no recovery value.
                if needs_execution:
                    self._snapshot_for_undo(game, f"judge: {question[:60]}")
                
                # Also check if question mentions a card on the battlefield
                if not needs_execution and player:
                    for p in game.players:
                        for c in p.battlefield:
                            if c.name.lower() in q_lower:
                                needs_execution = True
                                break
                        if needs_execution:
                            break
                
                if needs_execution and player:
                    # Try resolve_effect first (actually executes)
                    source_card = ""
                    for p in game.players:
                        for c in p.battlefield:
                            if c.name.lower() in q_lower:
                                source_card = c.name
                                break
                        if source_card:
                            break

                    messages, actions = await self.engine.rules.resolve_effect(
                        game,
                        effect_description=question,
                        source_card=source_card,
                        controller=player.name if player else "",
                    )

                    if actions:
                        # Resolution worked! Show results
                        for msg in messages:
                            await ctx.send(msg)

                        # Clear matching pending resolves
                        game.pending_resolves = [
                            pr for pr in game.pending_resolves
                            if not any(word in pr.lower() for word in q_lower.split() if len(word) > 3)
                        ]

                        # Check state-based actions
                        events = self.engine.check_state_based_actions(game)
                        if events:
                            await ctx.send("\n".join(f"⚡ {e}" for e in events))

                        if game.ended:
                            await ctx.send(f"🏆 **{game.players[game.winner].name} wins!**")

                        self.engine.save_game(game)
                        return
                    elif messages:
                        # Got messages but no actions — show them but also get a text ruling
                        for msg in messages:
                            if "No game state changes" not in msg:
                                await ctx.send(msg)

                # Fall through to judge-with-hands: get a ruling AND execute fix commands
                ruling = await self.engine.rules.ask_judge_with_fix(game, question, player.name if player else "")

                # If the judge applied changes, check SBA and clear pending resolves
                if "Applied changes:" in ruling:
                    game.pending_resolves = [
                        pr for pr in game.pending_resolves
                        if not any(word in pr.lower() for word in q_lower.split() if len(word) > 3)
                    ]
                    events = self.engine.check_state_based_actions(game)
                    if events:
                        ruling += "\n" + "\n".join(f"⚡ {e}" for e in events)
                    self.engine.save_game(game)
                    if game.ended:
                        ruling += f"\n🏆 **{game.players[game.winner].name} wins!**"
            else:
                # General rules question (no game)
                ruling = await self.engine.rules.ask_judge(
                    GameState(0, "standard", []), 
                    question,
                    "No active game - general rules question"
                )
        
        # Split long rulings if needed
        if len(ruling) > 1900:
            chunks = [ruling[i:i+1900] for i in range(0, len(ruling), 1900)]
            for i, chunk in enumerate(chunks):
                await ctx.send(f"📜 **Judge Ruling ({i+1}/{len(chunks)}):**\n{chunk}")
        else:
            await ctx.send(f"📜 **Judge Ruling:**\n{ruling}")

    @ask_judge.error
    async def ask_judge_error(self, ctx, error):
        """Handle !judge with no arguments — discord.py won't accept the default for keyword-only params."""
        if isinstance(error, commands.MissingRequiredArgument):
            await self.ask_judge(ctx, question='')
        else:
            raise error

    @commands.command(name="transform", aliases=["flip"])
    async def transform_card(self, ctx, *, card_name: str):
        """Transform a double-faced card on the battlefield.

        Usage: !transform Delver of Secrets | !flip Brutal Cathar
        """
        game = self._get_game(ctx)
        if not game:
            await ctx.send("No active game in this thread!")
            return
        player_idx = game.get_player_index(ctx.author.id)
        if player_idx is None:
            await ctx.send("You're not in this game!")
            return
        player = game.players[player_idx]
        target_card = None
        card_name_lower = card_name.strip().lower()
        for card in player.battlefield:
            if (card.name.lower() == card_name_lower
                    or card.back_face_name.lower() == card_name_lower
                    or card.front_face_name.lower() == card_name_lower
                    or card_name_lower in card.name.lower()):
                target_card = card
                break
        if not target_card:
            await ctx.send(f"Could not find **{card_name}** on your battlefield.")
            return
        if not target_card.has_transform:
            await ctx.send(f"**{target_card.name}** is not a transforming card.")
            return
        oracle_lower = (target_card.oracle_text or '').lower()
        back_oracle_lower = (target_card.back_face_oracle_text or '').lower()
        if ('daybound' in oracle_lower or 'nightbound' in oracle_lower
                or 'daybound' in back_oracle_lower or 'nightbound' in back_oracle_lower):
            await ctx.send(f"**{target_card.name}** has daybound/nightbound and transforms automatically with the day/night cycle.")
            return
        old_name = target_card.name
        if target_card.transform():
            if hasattr(self.engine, '_register_static_effects'):
                try:
                    self.engine._register_static_effects(game)
                except Exception:
                    pass
            msg = f"🔄 **{old_name}** transforms into **{target_card.name}**!"
            if target_card.power is not None and target_card.toughness is not None:
                msg += f"\n   {target_card.name} — {target_card.power}/{target_card.toughness}"
            if target_card.oracle_text:
                text_preview = target_card.oracle_text[:300]
                if len(target_card.oracle_text) > 300:
                    text_preview += "..."
                msg += f"\n   *{text_preview}*"
            await ctx.send(msg)
        else:
            await ctx.send(f"**{target_card.name}** could not be transformed.")

    @commands.command(name="resolve")
    async def resolve_effect(self, ctx, *, description: str):
        """
        Ask Claude to resolve an effect and EXECUTE it on the game state.
        
        Unlike !judge (text-only ruling), this actually modifies the game.
        
        Usage:
            !resolve Terror of the Peaks trigger, 5/5 creature entered
            !resolve Mulldrifter ETB draw 2
            !resolve Acidic Slime ETB destroy target artifact
            !resolve Sarkhan ultimate — create three 5/5 Dragons
        """
        game = self._get_game(ctx)
        if not game:
            await ctx.send("No active game in this thread!")
            return
        
        player_idx = game.get_player_index(ctx.author.id)
        if player_idx is None:
            await ctx.send("You're not in this game!")
            return

        player = game.players[player_idx]
        # Undo snapshot: resolve_effect actually mutates state via the Tier 3
        # judge's JSON actions, so undo is meaningful here.
        self._snapshot_for_undo(game, f"{player.name} resolved: {description[:60]}")

        # Try to extract a card name from the description for better context.
        # May 23 audit (CRITICAL #5): basic lands get matched first when a card
        # like Kodama's Reach searches "for a Forest and a Mountain" — the basic
        # land name appears in the description and matches before the actual
        # source spell. Skip basic lands and prefer the stack-top source if any.
        BASIC_LAND_NAMES = {"plains", "island", "swamp", "mountain", "forest", "wastes"}
        source_card = ""

        # Highest-precedence: the top of the stack is almost certainly the source.
        try:
            if game.stack:
                top = game.stack[-1]
                top_name = getattr(getattr(top, 'card', None), 'name', None) or getattr(top, 'name', None)
                if top_name and top_name.lower() in description.lower():
                    source_card = top_name
        except Exception:
            pass

        if not source_card:
            for card in player.battlefield:
                if card.name.lower() in BASIC_LAND_NAMES:
                    continue
                if card.name.lower() in description.lower():
                    source_card = card.name
                    break

        # Also check opponent's battlefield
        if not source_card:
            opponent = game.players[1 - player_idx]
            for card in opponent.battlefield:
                if card.name.lower() in BASIC_LAND_NAMES:
                    continue
                if card.name.lower() in description.lower():
                    source_card = card.name
                    break
        
        async with ctx.typing():
            messages, actions = await self.engine.rules.resolve_effect(
                game,
                effect_description=description,
                source_card=source_card,
                controller=player.name,
            )
        
        if messages:
            for msg in messages:
                await ctx.send(msg)
        else:
            await ctx.send("📜 No game state changes needed.")
        
        if actions:
            # Check state-based actions after resolution
            events = self.engine.check_state_based_actions(game)
            if events:
                await ctx.send("\n".join(f"⚡ {e}" for e in events))
            
            if game.ended:
                await ctx.send(f"🏆 **{game.players[game.winner].name} wins!**")
            
            self.engine.save_game(game)
    
    @commands.command(name="damage")
    async def deal_damage(self, ctx, amount: int, target: str = None):
        """
        Deal damage to a player.
        
        Usage:
            !damage 5 @Player
            !damage 3 opponent
        """
        game = self._get_game(ctx)
        if not game:
            await ctx.send("No active game in this thread!")
            return
        
        player_idx = game.get_player_index(ctx.author.id)
        if player_idx is None:
            await ctx.send("You're not in this game!")
            return
        
        # Determine target
        if ctx.message.mentions:
            target_player = game.get_player_by_user_id(ctx.message.mentions[0].id)
        elif target and target.lower() in ["opponent", "them", "opp"]:
            target_player = game.players[1 - player_idx]
        elif target and target.lower() in ["self", "me"]:
            target_player = game.players[player_idx]
        else:
            target_player = game.players[1 - player_idx]  # Default to opponent
        
        if not target_player:
            await ctx.send("Couldn't find that player!")
            return
        
        self.engine.deal_damage(target_player, amount, game=game)
        await ctx.send(f"💥 **{target_player.name}** takes {amount} damage! (Life: {target_player.life})")
        
        events = self.engine.check_state_based_actions(game)
        if events:
            await ctx.send("\n".join(f"⚡ {e}" for e in events))
        
        if game.ended:
            await ctx.send(f"🏆 **{game.players[game.winner].name} wins!**")
    
    @commands.command(name="life")
    async def adjust_life(self, ctx, adjustment: str, target: str = None):
        """
        Adjust life total.
        
        Usage:
            !life -5          - You lose 5 life
            !life +2 @Player - Player gains 2 life
        """
        game = self._get_game(ctx)
        if not game:
            await ctx.send("No active game in this thread!")
            return
        
        # Parse adjustment
        try:
            amount = int(adjustment)
        except ValueError:
            await ctx.send("Usage: `!life +/-N [@player]`")
            return
        
        player_idx = game.get_player_index(ctx.author.id)
        if player_idx is None:
            await ctx.send("You're not in this game!")
            return
        
        # Determine target
        if ctx.message.mentions:
            target_player = game.get_player_by_user_id(ctx.message.mentions[0].id)
        else:
            target_player = game.players[player_idx]
        
        if not target_player:
            await ctx.send("Couldn't find that player!")
            return
        
        target_player.life += amount
        
        if amount > 0:
            await ctx.send(f"💚 **{target_player.name}** gains {amount} life! (Life: {target_player.life})")
        else:
            await ctx.send(f"💔 **{target_player.name}** loses {abs(amount)} life! (Life: {target_player.life})")
        
        events = self.engine.check_state_based_actions(game)
        if events:
            await ctx.send("\n".join(f"⚡ {e}" for e in events))
    
    @commands.command(name="pass")
    async def pass_priority(self, ctx):
        """Pass priority or move to next phase."""
        game = self._get_game(ctx)
        if not game:
            await ctx.send("No active game in this thread!")
            return

        player_idx = game.get_player_index(ctx.author.id)
        if player_idx is None or player_idx != game.active_player_index:
            await ctx.send(self._not_your_turn_msg(game))
            return

        # Handle pending "may discard → draw" — player chose not to discard
        if (game.pending_action
                and game.pending_action.get('type') == 'may_discard_draw'
                and game.pending_action.get('player_idx') == player_idx):
            game.pending_action = None
            await ctx.send("✋ Chose not to discard. No card drawn.")
            self.engine.save_game(game)
            return

        # Block pass if mandatory loot discard is pending (Ashling/Magecraft etc.)
        if (game.pending_action
                and game.pending_action.get('type') == 'loot_discard_draw'
                and game.pending_action.get('player_idx') == player_idx):
            source = game.pending_action.get('source', 'ability')
            await ctx.send(f"⚠️ You must discard a card for {source}. Use `!discard <card name>` first.")
            return

        # Auto-resolve pending hand-size discard if player passes without discarding
        if (game.pending_action
                and game.pending_action.get('type') == 'discard_to_hand_size'
                and game.pending_action.get('player_idx') == player_idx):
            remaining = game.pending_action['cards_to_discard']
            player = game.players[player_idx]
            for _ in range(remaining):
                if player.hand:
                    discarded = player.hand.pop()
                    player.graveyard.append(discarded)
                    await ctx.send(f"📤 Auto-discarded **{discarded.name}** to hand size")
            game.pending_action = None

        old_phase = game.phase
        _, _phase_msgs = self.engine.advance_phase(game)
        for _m in _phase_msgs:
            await ctx.send(_m)
        # Drain any sync-queued triggers via Tier 3 (Meren, Abyss, Emeria, etc.)
        drain_msgs = await self.engine.drain_pending_triggers(game)
        for _m in drain_msgs:
            await ctx.send(_m)
        await ctx.send(f"➡️ Moving to {PHASE_NAMES[game.phase]}")

        # If Claude's turn now, execute
        if game.active_player.is_claude and game.phase == Phase.MAIN1:
            actions = await self.engine.execute_claude_turn(game)
            actions = self._sanitize_action_bullets(actions)
            if actions:
                msg = "**Claude's turn:**\n" + "\n".join(f"• {a}" for a in actions)
                if len(msg) > 1900:
                    await ctx.send("**Claude's turn:**")
                    for action in actions:
                        await ctx.send(f"• {action[:1900]}")
                else:
                    await ctx.send(msg)
            await ctx.send(embed=self.display.create_game_embed(game))

    @commands.command(name="turn")
    async def end_turn(self, ctx):
        """End your turn."""
        game = self._get_game(ctx)
        if not game:
            await ctx.send("No active game in this thread!")
            return

        player_idx = game.get_player_index(ctx.author.id)
        if player_idx is None or player_idx != game.active_player_index:
            await ctx.send(self._not_your_turn_msg(game))
            return

        # Block turn end if mandatory loot discard is pending (Ashling/Magecraft etc.)
        if (game.pending_action
                and game.pending_action.get('type') == 'loot_discard_draw'
                and game.pending_action.get('player_idx') == player_idx):
            source = game.pending_action.get('source', 'ability')
            await ctx.send(f"⚠️ You must discard a card for {source}. Use `!discard <card name>` first.")
            return

        # Auto-resolve pending hand-size discard if player ends turn without discarding
        if (game.pending_action
                and game.pending_action.get('type') == 'discard_to_hand_size'
                and game.pending_action.get('player_idx') == player_idx):
            remaining = game.pending_action['cards_to_discard']
            player = game.players[player_idx]
            for _ in range(remaining):
                if player.hand:
                    discarded = player.hand.pop()
                    player.graveyard.append(discarded)
                    await ctx.send(f"📤 Auto-discarded **{discarded.name}** to hand size")
            game.pending_action = None

        end_step_msgs = self.engine.end_turn(game)

        # Show any end-step messages (discard prompts, etc.)
        for msg in end_step_msgs:
            await ctx.send(msg)
        # Drain end-step triggers queued by end_turn (Meren, Athreos, etc.)
        for _m in await self.engine.drain_pending_triggers(game):
            await ctx.send(_m)

        # If player needs to discard to hand size, pause here — don't proceed to next turn yet
        # end_turn() already switched the active player, so we flag that the turn
        # continuation should happen after discards are done
        if game.pending_action and game.pending_action.get('type') == 'discard_to_hand_size':
            game.pending_action['continue_turn'] = True
            self.engine.save_game(game)
            return

        await ctx.send(f"🔄 Turn {game.turn_number} - **{game.active_player.name}**'s turn!")
        
        # If Claude's turn, execute
        if game.active_player.is_claude:
            # Advance through beginning phases to Main Phase 1
            # After end_turn, phase is UNTAP
            _, _p1 = self.engine.advance_phase(game)  # UNTAP → UPKEEP
            _, _p2 = self.engine.advance_phase(game)  # UPKEEP → DRAW (upkeep triggers here)
            _, _p3 = self.engine.advance_phase(game)  # DRAW → MAIN1
            for _m in _p1 + _p2 + _p3:
                await ctx.send(_m)
            # Drain sync-queued triggers via Tier 3 (upkeep/end-step/dies)
            for _m in await self.engine.drain_pending_triggers(game):
                await ctx.send(_m)
            # Now at MAIN1, ready for actions

            actions = await self.engine.execute_claude_turn(game)
            actions = self._sanitize_action_bullets(actions)
            if actions:
                msg = "**Claude's turn:**\n" + "\n".join(f"• {a}" for a in actions)
                if len(msg) > 1900:
                    await ctx.send("**Claude's turn:**")
                    for action in actions:
                        await ctx.send(f"• {action[:1900]}")
                else:
                    await ctx.send(msg)
            else:
                # Check if there was an API error
                if hasattr(self.engine.claude_ai, 'last_error') and self.engine.claude_ai.last_error:
                    await ctx.send(f"⚠️ Claude had trouble deciding (API error). Passing turn.\n`{self.engine.claude_ai.last_error[:100]}`")
                    self.engine.claude_ai.last_error = None
                else:
                    await ctx.send("*Claude thinks, then passes.*")
            await ctx.send(embed=self.display.create_game_embed(game))

            # Check if combat is paused for human blocks
            if game.waiting_for_human_blocks:
                await ctx.send(f"🛡️ Declare blockers with `!block <attacker> with <blocker>`, then `!doneblocking` (or `!noblock` for no blocks)")
                self.engine.save_game(game)
                return

            # Check if game ended during Claude's turn
            if game.ended:
                await ctx.send(f"🏆 **{game.players[game.winner].name} wins!**")
                # Delete saved game (it's over)
                self.engine.delete_game(game.thread_id)
                return

            # Claude's turn is over, pass back to human
            self.engine.end_turn(game)
            # Advance human through beginning phases to Main Phase 1
            _, _p1 = self.engine.advance_phase(game)  # UNTAP → UPKEEP
            _, _p2 = self.engine.advance_phase(game)  # UPKEEP → DRAW (upkeep triggers here)
            _, _p3 = self.engine.advance_phase(game)  # DRAW → MAIN1
            for _m in _p1 + _p2 + _p3:
                await ctx.send(_m)
            # Drain sync-queued triggers via Tier 3 (upkeep/end-step/dies)
            for _m in await self.engine.drain_pending_triggers(game):
                await ctx.send(_m)
            await ctx.send(f"🔄 Turn {game.turn_number} - **{game.active_player.name}**'s turn! (Drew a card)")
            await ctx.send(embed=self.display.create_game_embed(game))
        
        # Save game state
        self.engine.save_game(game)
    
    @commands.command(name="gg")
    async def concede(self, ctx):
        """Concede the game."""
        game = self._get_game(ctx)
        if not game:
            await ctx.send("No active game in this thread!")
            return
        
        player_idx = game.get_player_index(ctx.author.id)
        if player_idx is None:
            await ctx.send("You're not in this game!")
            return
        
        game.ended = True
        game.winner = 1 - player_idx
        
        await ctx.send(f"🏳️ **{game.players[player_idx].name}** concedes!")
        await ctx.send(f"🏆 **{game.players[game.winner].name}** wins!")
        
        # Delete saved game (it's over)
        self.engine.delete_game(game.thread_id)
    
    @commands.command(name="graveyard", aliases=["gy"])
    async def show_graveyard(self, ctx, player_name: str = None):
        """Show a player's graveyard."""
        game = self._get_game(ctx)
        if not game:
            await ctx.send("No active game in this thread!")
            return
        
        if player_name:
            player = next((p for p in game.players if p.name.lower() == player_name.lower()), None)
        else:
            player = game.get_player_by_user_id(ctx.author.id)
        
        if not player:
            await ctx.send("Couldn't find that player!")
            return
        
        await ctx.send(self.display.format_graveyard(player))

    @commands.command(name="exile")
    async def exile_command(self, ctx, *, args: str = ""):
        """
        View exile zone OR exile a card from hand.
        
        Usage:
            !exile                    → Show your exile zone
            !exile claude             → Show Claude's exile zone
            !exile Ugin, Spirit Dragon → Exile Ugin from your hand (for imprint, etc.)
            !exile Kozilek from hand  → Also works with explicit "from hand"
        """
        game = self._get_game(ctx)
        if not game:
            await ctx.send("No active game in this thread!")
            return
        
        args = args.strip()
        
        # Check if this is an "exile from hand" action
        if " from hand" in args.lower():
            # Exile a card from hand
            player_idx = game.get_player_index(ctx.author.id)
            if player_idx is None:
                await ctx.send("You're not in this game!")
                return
            
            player = game.players[player_idx]
            card_name = args.lower().replace(" from hand", "").strip()
            
            # Find the card in hand
            card = None
            for c in player.hand:
                if card_name in c.name.lower():
                    card = c
                    break
            
            if not card:
                await ctx.send(f"❌ Couldn't find '{card_name}' in your hand.")
                return
            
            # Move to exile
            player.hand.remove(card)
            player.exile.append(card)
            
            await ctx.send(f"📤 Exiled **{card.name}** from hand.")
            self.engine.save_game(game)
            return
        
        # Otherwise, check if it's a player name (show their exile) or a card name (exile from hand)
        if args:
            # First check if it's a player name
            player = next((p for p in game.players if p.name.lower() == args.lower()), None)
            
            if not player:
                # Not a player name — assume they want to exile a card from hand
                player_idx = game.get_player_index(ctx.author.id)
                if player_idx is None:
                    await ctx.send("You're not in this game!")
                    return
                
                player = game.players[player_idx]
                card_name = args.strip().lower()
                
                # Find the card in hand
                card = None
                for c in player.hand:
                    if card_name in c.name.lower():
                        card = c
                        break
                
                if not card:
                    await ctx.send(f"❌ Couldn't find '{args}' in your hand or as a player name.\n"
                                   f"Usage: `!exile` (view yours), `!exile claude` (view theirs), "
                                   f"`!exile <card>` (exile from hand)")
                    return
                
                # Move to exile
                player.hand.remove(card)
                player.exile.append(card)
                
                await ctx.send(f"📤 Exiled **{card.name}** from hand.")
                self.engine.save_game(game)
                return
        else:
            player = game.get_player_by_user_id(ctx.author.id)
        
        if not player:
            await ctx.send("Couldn't find that player!")
            return
        
        if not player.exile:
            await ctx.send(f"**{player.name}'s Exile:** *Empty*")
            return
        
        lines = [f"**{player.name}'s Exile** ({len(player.exile)} cards):"]
        for i, card in enumerate(player.exile, 1):
            playable = "✨" if card.id in player.playable_from_exile else ""
            lines.append(f"  {i}. {card.name} {playable}")
        
        if player.playable_from_exile:
            lines.append("\n*✨ = playable this turn*")
        
        await ctx.send("\n".join(lines))
    
    @commands.command(name="return")
    async def return_from_exile(self, ctx, *, args: str = ""):
        """
        Return a card from exile to hand (for Ugin's Labyrinth, etc.)
        
        Usage:
            !return Kozilek           → Return Kozilek from exile to hand
            !return Kozilek to battlefield → Return to battlefield instead
        """
        game = self._get_game(ctx)
        if not game:
            await ctx.send("No active game in this thread!")
            return
        
        player_idx = game.get_player_index(ctx.author.id)
        if player_idx is None:
            await ctx.send("You're not in this game!")
            return
        
        player = game.players[player_idx]
        args_lower = args.lower().strip()
        
        # Parse destination
        destination = "hand"
        card_name = args_lower
        
        if " to battlefield" in args_lower:
            destination = "battlefield"
            card_name = args_lower.replace(" to battlefield", "").strip()
        elif " to hand" in args_lower:
            card_name = args_lower.replace(" to hand", "").strip()
        
        # Find card in exile
        card = None
        for c in player.exile:
            if card_name in c.name.lower():
                card = c
                break
        
        if not card:
            await ctx.send(f"❌ Couldn't find '{card_name}' in your exile zone.")
            return
        
        # Move card
        player.exile.remove(card)
        if destination == "battlefield":
            card.entered_this_turn = True
            player.battlefield.append(card)
            await ctx.send(f"📥 Returned **{card.name}** from exile to battlefield!")
        else:
            player.hand.append(card)
            await ctx.send(f"📥 Returned **{card.name}** from exile to hand!")
        
        self.engine.save_game(game)

    @commands.command(name="discard")
    async def discard_card(self, ctx, *, card_name: str):
        """
        Discard a card from hand to graveyard.

        Usage:
            !discard Lightning Bolt
        """
        game = self._get_game(ctx)
        if not game:
            await ctx.send("No active game in this thread!")
            return

        player_idx = game.get_player_index(ctx.author.id)
        if player_idx is None:
            await ctx.send("You're not in this game!")
            return

        player = game.players[player_idx]
        card_name_lower = card_name.lower().strip()

        # Find card in hand
        card = None
        for c in player.hand:
            if card_name_lower in c.name.lower():
                card = c
                break

        if not card:
            await ctx.send(f"❌ Couldn't find '{card_name}' in your hand.")
            return

        player.hand.remove(card)
        player.graveyard.append(card)
        await ctx.send(f"🗑️ Discarded **{card.name}**")

        # Handle pending "may discard → draw" from planeswalker abilities (Sarkhan, etc.)
        if (game.pending_action
                and game.pending_action.get('type') == 'may_discard_draw'
                and game.pending_action.get('player_idx') == player_idx):
            game.pending_action = None
            # Draw a card since they chose to discard
            drawn_cards = self.engine.draw_cards(player, 1, game=game)
            if drawn_cards:
                await ctx.send(f"🎴 Drew 1 card (discarded to draw)")
            else:
                await ctx.send("📚 Library is empty — no card to draw!")
            self.engine.save_game(game)
            return

        # Handle pending "loot" (mandatory discard then draw) from Ashling/Magecraft etc.
        if (game.pending_action
                and game.pending_action.get('type') == 'loot_discard_draw'
                and game.pending_action.get('player_idx') == player_idx):
            source = game.pending_action.get('source', 'Unknown')
            game.pending_action = None
            drawn_cards = self.engine.draw_cards(player, 1, game=game)
            if drawn_cards:
                await ctx.send(f"🃏 {source} — drew 1 card")
            else:
                await ctx.send("📚 No card drawn (library empty or draw prevented)")
            self.engine.save_game(game)
            return

        # Handle pending hand-size discard
        if (game.pending_action
                and game.pending_action.get('type') == 'discard_to_hand_size'
                and game.pending_action.get('player_idx') == player_idx):
            remaining = game.pending_action['cards_to_discard'] - 1
            if remaining <= 0:
                should_continue = game.pending_action.get('continue_turn', False)
                game.pending_action = None
                await ctx.send("✅ Hand size requirement met.")

                # Auto-continue to next turn if we paused mid-turn for discard
                if should_continue:
                    await ctx.send(f"🔄 Turn {game.turn_number} - **{game.active_player.name}**'s turn!")

                    if game.active_player.is_claude:
                        # Advance through beginning phases
                        _, _p1 = self.engine.advance_phase(game)  # UNTAP → UPKEEP
                        _, _p2 = self.engine.advance_phase(game)  # UPKEEP → DRAW (upkeep triggers here)
                        _, _p3 = self.engine.advance_phase(game)  # DRAW → MAIN1
                        for _m in _p1 + _p2 + _p3:
                            await ctx.send(_m)
                        # Drain sync-queued triggers via Tier 3
                        for _m in await self.engine.drain_pending_triggers(game):
                            await ctx.send(_m)

                        actions = await self.engine.execute_claude_turn(game)
                        actions = self._sanitize_action_bullets(actions)
                        if actions:
                            msg = "**Claude's turn:**\n" + "\n".join(f"• {a}" for a in actions)
                            if len(msg) > 1900:
                                await ctx.send("**Claude's turn:**")
                                for action in actions:
                                    await ctx.send(f"• {action[:1900]}")
                            else:
                                await ctx.send(msg)
                        else:
                            await ctx.send("*Claude thinks, then passes.*")
                        await ctx.send(embed=self.display.create_game_embed(game))

                        if game.waiting_for_human_blocks:
                            await ctx.send(f"🛡️ Declare blockers with `!block <attacker> with <blocker>`, then `!doneblocking` (or `!noblock` for no blocks)")
                            self.engine.save_game(game)
                            return

                        if game.ended:
                            await ctx.send(f"🏆 **{game.players[game.winner].name} wins!**")
                            self.engine.delete_game(game.thread_id)
                            return

                        # End Claude's turn, start human's
                        end_msgs2 = self.engine.end_turn(game)
                        for m in end_msgs2:
                            await ctx.send(m)
                        # Drain end-step triggers queued by end_turn before advancing
                        for _m in await self.engine.drain_pending_triggers(game):
                            await ctx.send(_m)
                        _, _p1 = self.engine.advance_phase(game)  # UNTAP → UPKEEP
                        _, _p2 = self.engine.advance_phase(game)  # UPKEEP → DRAW (upkeep triggers here)
                        _, _p3 = self.engine.advance_phase(game)  # DRAW → MAIN1
                        for _m in _p1 + _p2 + _p3:
                            await ctx.send(_m)
                        # Drain sync-queued triggers via Tier 3 (upkeep etc.)
                        for _m in await self.engine.drain_pending_triggers(game):
                            await ctx.send(_m)
                        await ctx.send(f"🔄 Turn {game.turn_number} - **{game.active_player.name}**'s turn! (Drew a card)")
                        await ctx.send(embed=self.display.create_game_embed(game))
                    else:
                        # Human's turn — draw and announce
                        _, _p1 = self.engine.advance_phase(game)  # UNTAP → UPKEEP
                        _, _p2 = self.engine.advance_phase(game)  # UPKEEP → DRAW (upkeep triggers here)
                        _, _p3 = self.engine.advance_phase(game)  # DRAW → MAIN1
                        for _m in _p1 + _p2 + _p3:
                            await ctx.send(_m)
                        # Drain sync-queued triggers via Tier 3
                        for _m in await self.engine.drain_pending_triggers(game):
                            await ctx.send(_m)
                        await ctx.send(embed=self.display.create_game_embed(game))
            else:
                game.pending_action['cards_to_discard'] = remaining
                await ctx.send(f"📋 {remaining} more card(s) to discard.")

        self.engine.save_game(game)

    @commands.command(name="fix")
    async def fix_game_state(self, ctx, *, instruction: str):
        """
        Fix game state for bug corrections (bypasses rules).

        Usage:
            !fix move Mountain from exile to hand
            !fix set claude life to 20
            !fix return Dragonmaster Outcast from graveyard to battlefield
            !fix give me 5 red mana

        This is for correcting bot bugs, not gameplay mistakes!
        """
        game = self._get_game(ctx)
        if not game:
            await ctx.send("No active game in this thread!")
            return
        # Undo snapshot: !fix is the bluntest state-mutation command — snapshot
        # so an over-aggressive fix can be rolled back. Especially important
        # since "set life to 20" / "move X to battlefield" can have downstream
        # SBA / trigger effects the user didn't intend.
        self._snapshot_for_undo(game, f"fix: {instruction[:60]}")


        player_idx = game.get_player_index(ctx.author.id)
        if player_idx is None:
            await ctx.send("You're not in this game!")
            return
        
        player = game.players[player_idx]
        opponent = game.players[1 - player_idx]
        instruction_lower = instruction.lower()
        
        # Parse common fix patterns
        result_msg = None
        
        # Discard card (hand to graveyard)
        # "discard X"
        discard_match = re.search(r'discard\s+(.+)', instruction_lower)
        if discard_match:
            card_name = discard_match.group(1).strip()
            card = None
            for c in player.hand:
                if card_name in c.name.lower():
                    card = c
                    break
            
            if card:
                player.hand.remove(card)
                player.graveyard.append(card)
                result_msg = f"✅ Discarded **{card.name}**"
            else:
                result_msg = f"❌ Couldn't find '{card_name}' in your hand"
        
        # Move card between zones
        # "move X from ZONE to ZONE"
        if not result_msg:
            move_match = re.search(r'move\s+(.+?)\s+from\s+(\w+)\s+to\s+(\w+)', instruction_lower)
            if move_match:
                card_name = move_match.group(1).strip()
                from_zone = move_match.group(2)
                to_zone = move_match.group(3)
                
                # Map zone names
                zone_map = {
                    'hand': (player.hand, 'hand'),
                    'battlefield': (player.battlefield, 'battlefield'),
                    'graveyard': (player.graveyard, 'graveyard'),
                    'exile': (player.exile, 'exile'),
                    'library': (player.library, 'library'),
                }
                
                # Also check opponent zones if specified
                if 'claude' in card_name or 'opponent' in card_name:
                    card_name = re.sub(r"(claude'?s?|opponent'?s?)\s*", '', card_name).strip()
                    zone_map = {
                        'hand': (opponent.hand, 'hand'),
                        'battlefield': (opponent.battlefield, 'battlefield'),
                        'graveyard': (opponent.graveyard, 'graveyard'),
                        'exile': (opponent.exile, 'exile'),
                        'library': (opponent.library, 'library'),
                    }
                
                if from_zone in zone_map and to_zone in zone_map:
                    from_list, from_name = zone_map[from_zone]
                    to_list, to_name = zone_map[to_zone]
                    
                    # Find card
                    card = None
                    for c in from_list:
                        if card_name in c.name.lower():
                            card = c
                            break
                    
                    if card:
                        from_list.remove(card)
                        # Clear zone-dependent state (MTG: zone change = new object)
                        card.damage_marked = 0
                        card.deathtouch_damage = 0
                        card.tapped = False
                        card.attacking = False
                        card.attacking_player = None
                        card.blocking = []
                        card.blocked_by = []
                        card.power_modifier = 0
                        card.toughness_modifier = 0
                        card.temp_keywords = []
                        to_list.append(card)
                        result_msg = f"✅ Moved **{card.name}** from {from_name} to {to_name}"
                    else:
                        result_msg = f"❌ Couldn't find '{card_name}' in {from_name}"
                else:
                    result_msg = f"❌ Unknown zone. Use: hand, battlefield, graveyard, exile, library"

        # Return card (shorthand for move from graveyard/exile to hand/battlefield)
        # "return X from ZONE to ZONE"
        if not result_msg:
            return_match = re.search(r'return\s+(.+?)\s+from\s+(\w+)\s+to\s+(\w+)', instruction_lower)
            if return_match:
                # Delegate to move logic
                instruction_lower = f"move {return_match.group(1)} from {return_match.group(2)} to {return_match.group(3)}"
                move_match = re.search(r'move\s+(.+?)\s+from\s+(\w+)\s+to\s+(\w+)', instruction_lower)
                if move_match:
                    card_name = move_match.group(1).strip()
                    from_zone = move_match.group(2)
                    to_zone = move_match.group(3)
                    
                    zone_map = {
                        'hand': (player.hand, 'hand'),
                        'battlefield': (player.battlefield, 'battlefield'),
                        'graveyard': (player.graveyard, 'graveyard'),
                        'exile': (player.exile, 'exile'),
                    }
                    
                    if from_zone in zone_map and to_zone in zone_map:
                        from_list, from_name = zone_map[from_zone]
                        to_list, to_name = zone_map[to_zone]
                        
                        card = None
                        for c in from_list:
                            if card_name in c.name.lower():
                                card = c
                                break
                        
                        if card:
                            from_list.remove(card)
                            # Clear zone-dependent state (MTG: zone change = new object)
                            card.damage_marked = 0
                            card.deathtouch_damage = 0
                            card.tapped = False
                            card.attacking = False
                            card.attacking_player = None
                            card.blocking = []
                            card.blocked_by = []
                            card.power_modifier = 0
                            card.toughness_modifier = 0
                            card.temp_keywords = []
                            to_list.append(card)
                            result_msg = f"✅ Returned **{card.name}** from {from_name} to {to_name}"
                        else:
                            result_msg = f"❌ Couldn't find '{card_name}' in {from_name}"
        
        # Add/remove counters
        # "add N +1/+1 counters to X" or "add N counters to X" or "remove N counters from X"
        if not result_msg:
            counter_match = re.search(
                r'(add|remove|put)\s+(\d+)\s+(\+1/\+1|\-1/\-1|loyalty|charge|counter)?\s*counters?\s+(?:to|on|from)\s+(.+)',
                instruction_lower
            )
            if counter_match:
                action = counter_match.group(1)  # add/remove/put
                count = int(counter_match.group(2))
                counter_type = counter_match.group(3) or '+1/+1'  # Default to +1/+1
                card_name = counter_match.group(4).strip()
                
                # Normalize counter type
                if counter_type == 'counter':
                    counter_type = '+1/+1'  # Default
                
                # Find card on any player's battlefield
                card = None
                for p in game.players:
                    for c in p.battlefield:
                        if card_name in c.name.lower():
                            card = c
                            break
                    if card:
                        break
                
                if card:
                    if action in ('add', 'put'):
                        if counter_type == 'loyalty':
                            card.loyalty_counters = getattr(card, 'loyalty_counters', 0) + count
                            result_msg = f"✅ Added {count} loyalty counter(s) to **{card.name}** (now {card.loyalty_counters})"
                        else:
                            card.counters[counter_type] = card.counters.get(counter_type, 0) + count
                            result_msg = f"✅ Added {count} {counter_type} counter(s) to **{card.name}** (now {card.counters[counter_type]})"
                    else:  # remove
                        if counter_type == 'loyalty':
                            card.loyalty_counters = max(0, getattr(card, 'loyalty_counters', 0) - count)
                            result_msg = f"✅ Removed {count} loyalty counter(s) from **{card.name}** (now {card.loyalty_counters})"
                        else:
                            current = card.counters.get(counter_type, 0)
                            card.counters[counter_type] = max(0, current - count)
                            result_msg = f"✅ Removed {count} {counter_type} counter(s) from **{card.name}** (now {card.counters[counter_type]})"
                else:
                    result_msg = f"❌ Couldn't find '{card_name}' on the battlefield"
        
        # Set life
        # "set X life to N" or "set life to N"
        if not result_msg:
            life_match = re.search(r'set\s+(?:(\w+)\s+)?life\s+to\s+(\d+)', instruction_lower)
            if life_match:
                target_name = life_match.group(1)
                new_life = int(life_match.group(2))
                
                target = player
                if target_name and target_name.lower() == 'claude':
                    target = opponent if opponent.is_claude else player
                elif target_name and target_name.lower() not in ['my', 'me']:
                    target = next((p for p in game.players if target_name in p.name.lower()), player)
                
                old_life = target.life
                target.life = new_life
                result_msg = f"✅ Set **{target.name}**'s life: {old_life} → {new_life}"
        
        # Deal damage
        # "deal N damage to X"
        if not result_msg:
            dmg_match = re.search(r'deal\s+(\d+)\s+damage\s+to\s+(\w+)', instruction_lower)
            if dmg_match:
                damage = int(dmg_match.group(1))
                target_name = dmg_match.group(2)
                
                target = None
                if target_name == 'claude':
                    target = opponent if opponent.is_claude else None
                elif target_name in ['me', 'myself']:
                    target = player
                else:
                    target = next((p for p in game.players if target_name in p.name.lower()), None)
                
                if target:
                    target.life -= damage
                    target.record_life_loss(damage)
                    result_msg = f"✅ Dealt {damage} damage to **{target.name}** (now at {target.life} life)"
                else:
                    result_msg = f"❌ Couldn't find player '{target_name}'"
        
        if result_msg:
            await ctx.send(result_msg)
            self.engine.save_game(game)
        else:
            await ctx.send(
                "❌ Couldn't parse fix instruction. Examples:\n"
                "• `!fix move Mountain from exile to hand`\n"
                "• `!fix return Dragonmaster from graveyard to battlefield`\n"
                "• `!fix add 3 +1/+1 counters to Omarthis`\n"
                "• `!fix remove 2 loyalty counters from Garruk`\n"
                "• `!fix set claude life to 20`\n"
                "• `!fix deal 5 damage to claude`\n"
                "• `!fix discard Lightning Bolt`"
            )
    
    async def _resolve_combat(self, ctx, game: GameState):
        """Delegates to mtg.autoplay._resolve_combat (Phase 2F)."""
        from mtg.autoplay import _resolve_combat
        return await _resolve_combat(self, ctx, game)
    def _load_deck_by_name(self, name: str) -> Optional[Dict]:
        """Load a deck JSON from data/ by short name or filename."""
        import os
        # Try short name mapping first
        filename = self.AUTOPLAY_DECKS.get(name.lower(), name)
        deck_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", f"{filename}.json")
        if os.path.exists(deck_path):
            with open(deck_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        return None

    async def _autoplay_human_turn(self, thread, game: GameState, player_idx: int):
        """Delegates to mtg.autoplay._autoplay_human_turn (Phase 2F)."""
        from mtg.autoplay import _autoplay_human_turn
        return await _autoplay_human_turn(self, thread, game, player_idx)
    async def _autoplay_execute_action(self, thread, game: GameState, player_idx: int, action: Dict) -> Optional[str]:
        """Delegates to mtg.autoplay._autoplay_execute_action (Phase 2F)."""
        from mtg.autoplay import _autoplay_execute_action
        return await _autoplay_execute_action(self, thread, game, player_idx, action)
    async def _autoplay_resolve_pending_action(self, thread, game: GameState):
        """Delegates to mtg.autoplay._autoplay_resolve_pending_action (Phase 2F)."""
        from mtg.autoplay import _autoplay_resolve_pending_action
        return await _autoplay_resolve_pending_action(self, thread, game)
    async def _autoplay_resolve_combat(self, thread, game: GameState):
        """Resolve combat damage (mirrors _resolve_combat but without ctx)."""
        game.phase = Phase.COMBAT_DAMAGE

        damage_msgs = self.engine.rules.resolve_combat_damage(game)
        if damage_msgs:
            await self._autoplay_send(thread, "**💥 Combat Damage:**\n" + "\n".join(f"• {m}" for m in damage_msgs))

        # Clear combat state
        for attacker_id in game.attackers:
            result = game.find_card_global(attacker_id)
            if result:
                attacker, _, _ = result
                attacker.attacking = False
                attacker.attacking_player = None
                attacker.blocked_by = []

        for p in game.players:
            for creature in p.creatures():
                creature.blocking = []

        game.attackers = []
        game.blockers = {}

        events = self.engine.check_state_based_actions(game)
        if events:
            await self._autoplay_send(thread, "\n".join(events))

        # June 11 audit: resolve combat-queued dies triggers at the combat
        # priority boundary, before the autoplay planner sees MAIN2.
        for msg in await self.engine.drain_pending_triggers(game):
            await self._autoplay_send(thread, msg)

        if not game.ended:
            game.phase = Phase.MAIN2
            # July 30 batch-9 audit: direct phase set bypassed advance_phase,
            # so postcombat main-phase triggers (Tymna) never fired on
            # combat turns — the only turns their condition can be true.
            for _m in self.engine.dispatch_main_phase_triggers(game, False):
                await self._autoplay_send(thread, _m)

    async def _autoplay_send(self, thread, content=None, embed=None,
                             _is_chunk=False, final=False):
        """Send a message to the autoplay thread, with rate limiting and logging.

        _is_chunk: set by the 1900-char splitter below for the pieces of ONE
        logical message. July 20 display audit: chunks used to re-enter the
        full pipeline, where the per-turn burst dedup could swallow a
        later chunk as a "repeat" of similar earlier content — a Yorion
        flicker-cascade turn lost a real life-total line this way
        (game_1526059786242752615: engine-confirmed life 39 never reached
        Discord). Chunks skip the suppression layers; the whole message
        already went through them once.
        """
        # Post-game-end gate (CR 104.2a in spirit — nothing happens after a
        # player loses). July 24 gated trigger DISPATCH on `not game.ended`, but
        # nothing gated the message FLUSH, so a cast coroutine suspended across
        # the end of the game could still post afterwards. Seen in
        # game_1530441479389184000: Pact of Negation was countered by Frilled
        # Mystic at 01:18:13 and Discord said so at the time, then the Pact's
        # own long-suspended cast_spell_async unwound at 01:18:59 and re-posted
        # "❌ Pact of Negation was countered!" AFTER the "🏆 Claude wins!"
        # summary — which reads as a rules bug and is merely a stale flush.
        # Suppressed from Discord, kept on console so the record survives for
        # audits (same shape as [RESOLVE-PROSE-DROPPED]). Chunks are exempt:
        # they are pieces of a message whose parent already passed this gate.
        # Fails OPEN when the game can't be resolved, so nothing is lost by
        # accident.
        #
        # July 29 batch audit: gating on `ended` alone over-suppressed — the
        # SBA sets `ended` mid-combat, BEFORE the winner banner posts, so the
        # lethal combat's own damage summary (buffered during resolution,
        # flushed right after) was eaten in ~150/152 games and players never
        # saw how the game ended. Suppress only AFTER the final summary has
        # actually posted: content in the ended→summary window is the
        # ending's own record arriving in order; content after the summary
        # is the stale-flush class the gate was built for.
        if content and not final and not _is_chunk:
            _tid = getattr(thread, 'id', None)
            _g = self.engine.games.get(_tid) if _tid is not None else None
            if (_g is not None and getattr(_g, 'ended', False)
                    and getattr(_g, '_final_summary_posted', False)):
                print(f"[POST-GAME-SUPPRESSED] {str(content)[:200]}")
                return
        if final:
            _tid = getattr(thread, 'id', None)
            _g = self.engine.games.get(_tid) if _tid is not None else None
            if _g is not None:
                _g._final_summary_posted = True

        # May 17 audit: final defense-in-depth strip of dangling-article
        # artifacts ("The .", trailing " The") that leak through from
        # judge.py / triggers.py / spells.py sanitizers. This catches the
        # long tail without having to chase down every emit site.
        if content and embed is None and not _is_chunk:
            try:
                from mtg.helpers import strip_dangling_articles
                content = strip_dangling_articles(content)
            except Exception:
                pass
        # May 30 audit: collapse runs of identical lines WITHIN one multi-line
        # message. The dedup below keys on the whole content string, so bursts
        # built as a single send slipped through — Martial Coup's 8x "⭕ 1 +1/+1
        # counter on each ...", a board-wipe's 7x "💀 Glissa triggers", or 5x
        # "☠️ Plant dies" (the per-creature combat DAMAGE lines already collapse
        # upstream; the death/trigger lines did not). Group consecutive
        # byte-identical non-empty lines into "<line> _(×N)_".
        if content and embed is None and '\n' in content and not _is_chunk:
            _lines = content.split('\n')
            _collapsed = []
            _i = 0
            while _i < len(_lines):
                _j = _i + 1
                while _j < len(_lines) and _lines[_j] == _lines[_i]:
                    _j += 1
                _run = _j - _i
                if _run >= 2 and _lines[_i].strip():
                    _collapsed.append(f"{_lines[_i]} _(×{_run})_")
                else:
                    _collapsed.append(_lines[_i])
                _i = _j
            content = '\n'.join(_collapsed)
        # May 14 audit: when the same trigger fires multiple times in one
        # event (Athreos firing 9× from a 9-creature board wipe, Meren's
        # oracle text printed twice on a single resolution), Discord ends up
        # with duplicate consecutive bot messages. Track the most recent
        # send per-thread and either skip exact duplicates or collapse them
        # into a "(×N)" suffix when a third identical hit arrives.
        if content and embed is None and not _is_chunk:
            tid = getattr(thread, 'id', None)
            if tid is not None:
                if not hasattr(self, '_dedup_state'):
                    self._dedup_state: Dict[int, Tuple[str, int]] = {}
                last_content, last_count = self._dedup_state.get(tid, ("", 0))
                if content == last_content:
                    # Skip back-to-back identical sends entirely; we'll catch
                    # high counts at flush time below.
                    self._dedup_state[tid] = (content, last_count + 1)
                    return
                else:
                    self._dedup_state[tid] = (content, 1)
                # May 18 audit: extend the byte-identical adjacent dedup above
                # to byte-identical PER-TURN dedup. Species Specialist's
                # "🃏 Species Specialist — Claude draws a card" fired 11 times
                # in `game_1505768915773685800`'s discord log, only one pair
                # collapsed (the adjacent ones); the rest were interleaved
                # with attack/damage lines so the adjacent dedup missed them.
                # Same trigger firing 3+ times in one turn is spam — show the
                # first two, suppress the rest. The trigger's effect still
                # happens in game state; we just stop bloating the channel.
                g = self.engine.games.get(tid)
                if g is not None:
                    cur_turn = getattr(g, 'turn_number', 0)
                    if (not hasattr(g, '_turn_burst_counts')
                            or getattr(g, '_turn_burst_turn', -1) != cur_turn):
                        g._turn_burst_counts = {}
                        g._turn_burst_turn = cur_turn
                    # May 23 (#19) numeric-paren strip + May 25 (F8) bold-name
                    # strip, now centralized in helpers.burst_dedup_key.
                    # June 10 audit (V19): the F8 strip is RESTRICTED to
                    # draw/discard/exile/reveal shapes — unrestricted, every
                    # "✨ P cast **X**" in a turn shared one key and every
                    # 3rd+ DISTINCT cast was suppressed (44/139 games;
                    # creatures visibly "attacked out of nowhere").
                    from mtg.helpers import burst_dedup_key
                    dedup_key = burst_dedup_key(content)
                    seen = g._turn_burst_counts.get(dedup_key, 0)
                    g._turn_burst_counts[dedup_key] = seen + 1
                    if seen >= 2:
                        # 3rd+ identical message this turn — suppress.
                        # Emit a single sentinel on the 3rd to flag the burst,
                        # then go silent on subsequent fires.
                        if seen == 2:
                            await self._thread_send(
                                thread,
                                f"_(…suppressing further identical fires this turn)_",
                            )
                            try:
                                g._last_bot_message_time = time.time()
                            except Exception:
                                pass
                        return
        # Discord enforces a 2000-char limit per message. Long combat
        # damage blocks (multi-blocker scenarios + drain triggers + SBA
        # death messages) routinely exceed it, and discord.py raises
        # HTTPException 50035, dropping the entire message — players
        # would miss critical events like deaths or layer recalcs.
        # Split on newline boundaries to keep readability.
        if content and len(content) > 1900:
            chunks = []
            current = ""
            for line in content.split("\n"):
                # Single line too long? Hard-split it — at a space when one
                # exists in the tail window (July 20: the raw 1900-slice cut
                # "🃏 **Claude** draws 2 card(s)" mid-phrase in a giant
                # single-line flicker cascade).
                if len(line) > 1900:
                    if current:
                        chunks.append(current)
                        current = ""
                    _rest = line
                    while len(_rest) > 1900:
                        _cut = _rest.rfind(' ', 1500, 1900)
                        if _cut <= 0:
                            _cut = 1900
                        chunks.append(_rest[:_cut])
                        _rest = _rest[_cut:].lstrip()
                    if _rest:
                        chunks.append(_rest)
                    continue
                if len(current) + len(line) + 1 > 1900:
                    chunks.append(current)
                    current = line
                else:
                    current = (current + "\n" + line) if current else line
            if current:
                chunks.append(current)
            for i, chunk in enumerate(chunks):
                # Only attach the embed to the LAST chunk so it shows
                # below the full text rather than after the first split.
                chunk_embed = embed if i == len(chunks) - 1 else None
                await self._autoplay_send(thread, chunk, embed=chunk_embed,
                                          _is_chunk=True)
            return

        for attempt in range(3):
            try:
                if content:
                    # Use _thread_send for Discord logging (logs to game's discord log file)
                    await self._thread_send(thread, content, embed=embed)
                elif embed:
                    await thread.send(embed=embed)
                # May 18 audit: stamp the game with the last successful send time
                # so the bot's Q&A "what happened?" path can detect channel
                # silence vs. recent activity. The May 17 stall ran for 28
                # minutes between sends; the Q&A path confabulated "no crashes
                # detected" because it had no signal that the channel had
                # gone quiet. See bot.py:get_game_context_for_channel.
                try:
                    tid = getattr(thread, 'id', None)
                    if tid is not None:
                        g = self.engine.games.get(tid)
                        if g is not None:
                            g._last_bot_message_time = time.time()
                except Exception:
                    pass
                await asyncio.sleep(0.3)  # Rate limit buffer (0.3s supports ~20 concurrent games)
                return  # Sent successfully
            except discord.HTTPException as e:
                if e.status == 429:
                    # Rate limited — back off and retry once
                    retry_after = getattr(e, 'retry_after', 2.0) or 2.0
                    print(f"[AUTOPLAY] Discord 429, backing off {retry_after}s")
                    await asyncio.sleep(retry_after)
                    # Continue loop to retry
                elif e.status == 503 and attempt < 2:
                    # [FIX-8] Service unavailable — exponential backoff and retry
                    wait = 2 ** attempt  # 1s, 2s
                    print(f"[AUTOPLAY] Discord 503, retrying in {wait}s (attempt {attempt + 1}/3)")
                    await asyncio.sleep(wait)
                    # Continue loop to retry
                else:
                    print(f"[AUTOPLAY] Discord send error: {e}")
                    return  # Drop the message rather than crash the game
            except Exception as e:
                print(f"[AUTOPLAY] Discord send error: {e}")
                return  # Drop the message rather than crash the game

    async def _run_single_autoplay(self, channel, game_format: str, deck1_name: str, deck2_name: str,
                                   matchup_label: str = None, force_claude: bool = False,
                                   openrouter_model: str = None) -> dict:
        """Delegates to mtg.autoplay._run_single_autoplay (Phase 2F)."""
        from mtg.autoplay import _run_single_autoplay
        return await _run_single_autoplay(self, channel, game_format, deck1_name, deck2_name, matchup_label, force_claude, openrouter_model)
    def _parse_openrouter_flag(self, args: list) -> str | None:
        """Extract --openrouter <model> from args list. Mutates args in place
        to remove the model name (the --openrouter flag itself gets stripped
        by the caller's `[a for a in args if not a.startswith("--")]`)."""
        if "--openrouter" not in args:
            return None
        idx = args.index("--openrouter")
        # Next arg is the model name (if it's not another flag)
        if idx + 1 < len(args) and not args[idx + 1].startswith("--"):
            model_name = args.pop(idx + 1)
            # Allow short names — auto-prefix with openrouter/ if no slash
            if "/" not in model_name:
                model_name = f"openrouter/{model_name}"
            return model_name
        # No model specified — use default
        return "openrouter/optimus-alpha"

    def _get_ai_label(self, force_claude: bool, openrouter_model: str = None) -> str:
        """Get display label for the AI provider being used."""
        if force_claude:
            return "Claude"
        if openrouter_model:
            short = openrouter_model.split("/")[-1] if "/" in openrouter_model else openrouter_model
            return f"OpenRouter ({short})"
        if self._deepseek_adapter:
            return "Deepseek"
        return "Claude"

    async def _check_deepseek_balance(self) -> dict | None:
        """Delegates to mtg.autoplay._check_deepseek_balance (Phase 2F)."""
        from mtg.autoplay import _check_deepseek_balance
        return await _check_deepseek_balance(self)

    @commands.command(name="autoplay")
    async def autoplay(self, ctx, format: str = "commander", deck1: str = None, deck2: str = None):
        """
        Run an automated game for playtesting.

        Usage:
            !autoplay commander                                      - Random deck pairing (Deepseek if configured)
            !autoplay commander surrak rashmi                     - Specific decks
            !autoplay --claude commander surrak rashmi             - Force Claude
            !autoplay --openrouter optimus-alpha commander res rashmi  - OpenRouter model
        """
        if self._batch_running:
            await ctx.send("\u274c A batch is running. Use `!autoplay-stop` first.")
            return

        parts = ctx.message.content.split()
        args = parts[1:] if len(parts) > 1 else []

        # Parse provider flags
        force_claude = "--claude" in args or "--anthropic" in args
        openrouter_model = self._parse_openrouter_flag(args)
        args = [a for a in args if not a.startswith("--")]

        game_format = args[0] if args else "commander"
        deck1_name = args[1] if len(args) > 1 else None
        deck2_name = args[2] if len(args) > 2 else None

        available = list(self.AUTOPLAY_DECKS.keys())
        if deck1_name and not self._load_deck_by_name(deck1_name):
            await ctx.send(f"\u274c Deck '{deck1_name}' not found. Available: {', '.join(available)}")
            return
        if deck2_name and not self._load_deck_by_name(deck2_name):
            await ctx.send(f"\u274c Deck '{deck2_name}' not found. Available: {', '.join(available)}")
            return

        p1 = deck1_name or "random"
        p2 = deck2_name or "random"
        ai_label = self._get_ai_label(force_claude, openrouter_model)
        await ctx.send(f"\U0001f916\U0001f19a\U0001f916 Starting autoplay: **Rick Deckard** ({p1}) vs **{ai_label}** ({p2}) \u2014 {game_format}")
        await self._run_single_autoplay(ctx.channel, game_format, deck1_name, deck2_name,
                                        force_claude=force_claude, openrouter_model=openrouter_model)

    @commands.command(name="autoplay-batch")
    async def autoplay_batch(self, ctx, spec: str = "all", start: str = None):
        """
        Run multiple autoplay games from the playtest matrix.

        Usage:
            !autoplay-batch phase1             - Run Phase 1 (Deepseek if configured)
            !autoplay-batch --claude phase1     - Force Claude for the batch
            !autoplay-batch all                 - Run all 85 matchups
            !autoplay-batch phase1 5            - Start Phase 1 from matchup #5
            !autoplay-batch 15-30               - Run matchups 15 through 30

        Phases: phase1 (1-20), phase2 (21-60), phase3 (61-72),
                phase4 (73-78), phase5 (79), phase6 (80-81), phase7 (82-85),
                phase8/mechanics (86-99), phase9/draft (100)
        Use !autoplay-stop to halt after the current game.
        """
        if self._batch_running:
            await ctx.send("\u274c A batch is already running. Use `!autoplay-stop` first.")
            return

        # Parse provider flags from raw args (discord.py eats flags)
        parts = ctx.message.content.split()
        raw_args = parts[1:] if len(parts) > 1 else []
        force_claude = "--claude" in raw_args or "--anthropic" in raw_args
        openrouter_model = self._parse_openrouter_flag(raw_args)
        raw_args = [a for a in raw_args if not a.startswith("--")]
        # Re-derive spec and start from cleaned args
        if raw_args:
            spec = raw_args[0]
        if len(raw_args) > 1:
            start = raw_args[1]

        # Parse spec into matchup range
        import re
        range_match = re.match(r'^(\d+)-(\d+)$', spec)
        if range_match:
            start_num, end_num = int(range_match.group(1)), int(range_match.group(2))
        elif spec.lower() in self.AUTOPLAY_PHASES:
            start_num, end_num = self.AUTOPLAY_PHASES[spec.lower()]
        else:
            phases = ", ".join(self.AUTOPLAY_PHASES.keys())
            await ctx.send(f"\u274c Unknown spec '{spec}'. Use: {phases}, or a range like `15-30`")
            return

        # Optional start override
        if start is not None:
            try:
                start_num = int(start)
            except ValueError:
                await ctx.send(f"\u274c Invalid start number: {start}")
                return

        # Filter matchups
        matchups = [(n, f, d1, d2, desc) for n, f, d1, d2, desc in self.AUTOPLAY_MATRIX
                    if start_num <= n <= end_num]
        if not matchups:
            await ctx.send(f"\u274c No matchups in range {start_num}-{end_num}")
            return

        # Pre-flight DeepSeek balance check (only applies when DeepSeek is the provider)
        if not force_claude and not openrouter_model:
            balance_info = await self._check_deepseek_balance()
            if balance_info is not None:
                await ctx.send(balance_info["message"])
                if not balance_info["ok"]:
                    # Hard stop — don't burn a thread creation on a batch that will 402 immediately
                    return

        self._batch_running = True
        self._batch_stop_flag = False
        results = []

        # Create batch status thread
        batch_thread = await ctx.channel.create_thread(
            name=f"Autoplay Batch: {spec} (#{start_num}-#{end_num}, {len(matchups)} games)",
            type=discord.ChannelType.public_thread
        )
        await batch_thread.send(
            f"**Autoplay Batch: {spec}**\n"
            f"Running matchups #{start_num}\u2013#{end_num} ({len(matchups)} games)\n"
            f"Use `!autoplay-stop` to stop after the current game finishes.")

        try:
            for i, (matchup_num, fmt, d1, d2, desc) in enumerate(matchups):
                if self._batch_stop_flag:
                    await batch_thread.send(
                        f"\u23f9\ufe0f **Batch stopped** by user after {len(results)} games.")
                    break

                await batch_thread.send(
                    f"**[{i+1}/{len(matchups)}]** Starting #{matchup_num}: "
                    f"`{fmt}` {d1} vs {d2} \u2014 {desc}")

                if fmt == "draft":
                    # Cube draft — delegate to CubeDraftCog
                    cube_cog = self.bot.get_cog("Cube Draft")
                    if cube_cog:
                        game_result = await cube_cog._run_autodraft(
                            ctx.channel, cube_source=d1 or "test")
                    else:
                        game_result = {
                            "format": "draft", "deck1": d1, "deck2": "",
                            "outcome": "crash", "winner": None, "turns": 0,
                            "p1_life": 0, "p2_life": 0,
                            "error": "CubeDraftCog not loaded",
                            "thread_id": None, "duration_seconds": 0,
                        }
                else:
                    game_result = await self._run_single_autoplay(
                        ctx.channel, fmt, d1, d2,
                        matchup_label=f"#{matchup_num} {d1} vs {d2}",
                        force_claude=force_claude,
                        openrouter_model=openrouter_model
                    )
                game_result["matchup"] = matchup_num
                game_result["description"] = desc
                results.append(game_result)

                # One-line result
                icons = {"win_p1": "\U0001f7e2 P1", "win_p2": "\U0001f535 P2",
                         "timeout": "\U0001f7e1 Draw", "crash": "\U0001f534 ERR",
                         "circuit_breaker": "\u26a1 CB", "aborted": "⏹️ STOP"}
                line = (f"  \u2192 #{matchup_num} [{icons.get(game_result['outcome'], '?')}] "
                        f"{game_result['turns']}t, {game_result['duration_seconds']:.0f}s")
                if game_result.get("error"):
                    line += f" \u274c {game_result['error'][:80]}"
                await batch_thread.send(line)

                # Auto-abort: if the first 3 games ALL crash/circuit-break, the batch
                # is probably spoiled by a systemic bug. Stop early to save API costs.
                if len(results) >= 3:
                    recent = results[-3:]
                    bad_outcomes = {"crash", "circuit_breaker"}
                    if all(r["outcome"] in bad_outcomes for r in recent):
                        await batch_thread.send(
                            f"🛑 **Batch auto-aborted:** last 3 games all failed "
                            f"({', '.join(r['outcome'] for r in recent)}). "
                            f"Fix the bug before continuing.\n"
                            f"Resume with: `!autoplay-batch {spec} {matchup_num + 1}`")
                        break

                await asyncio.sleep(3)

            # Post summary
            await self._post_batch_summary(batch_thread, results, spec)

        except Exception as e:
            import traceback
            traceback.print_exc()
            await batch_thread.send(f"\u274c Batch crashed: {e}")
        finally:
            self._batch_running = False
            self._batch_stop_flag = False

    @commands.command(name="autoplay-parallel")
    async def autoplay_parallel(self, ctx, spec: str = "all", concurrency: str = "3"):
        """Run autoplay games in parallel (N at a time).

        Usage:
            !autoplay-parallel phase1 3        - Phase 1, 3 games at a time
            !autoplay-parallel mechanics 2     - Mechanic tests, 2 concurrent
            !autoplay-parallel 86-99 4         - Matchups 86-99, 4 concurrent
            !autoplay-parallel all 3           - All 100 matchups, 3 at a time
        """
        if self._batch_running:
            await ctx.send("❌ A batch is already running. Use `!autoplay-stop` first.")
            return
        parts = ctx.message.content.split()
        raw_args = parts[1:] if len(parts) > 1 else []
        force_claude = "--claude" in raw_args or "--anthropic" in raw_args
        openrouter_model = self._parse_openrouter_flag(raw_args)
        raw_args = [a for a in raw_args if not a.startswith("--")]
        if raw_args:
            spec = raw_args[0]
        if len(raw_args) > 1:
            concurrency = raw_args[1]
        try:
            n_concurrent = max(1, min(25, int(concurrency)))
        except ValueError:
            await ctx.send(f"❌ Invalid concurrency: {concurrency}")
            return
        import re
        range_match = re.match(r'^(\d+)-(\d+)$', spec)
        if range_match:
            start_num, end_num = int(range_match.group(1)), int(range_match.group(2))
        elif spec.lower() in self.AUTOPLAY_PHASES:
            start_num, end_num = self.AUTOPLAY_PHASES[spec.lower()]
        else:
            await ctx.send(f"❌ Unknown spec '{spec}'. Use phase name or range like `15-30`")
            return
        matchups = [(n, f, d1, d2, desc) for n, f, d1, d2, desc in self.AUTOPLAY_MATRIX
                    if start_num <= n <= end_num]
        if not matchups:
            await ctx.send(f"❌ No matchups in range {start_num}-{end_num}")
            return
        self._batch_running = True
        self._batch_stop_flag = False
        results = []
        batch_thread = await ctx.channel.create_thread(
            name=f"Parallel: {spec} (#{start_num}-#{end_num}, {len(matchups)} games, {n_concurrent}x)",
            type=discord.ChannelType.public_thread)
        await batch_thread.send(
            f"**Parallel Autoplay: {spec}**\n"
            f"{len(matchups)} games, **{n_concurrent} concurrent**\n"
            f"Use `!autoplay-stop` to halt after current wave.")

        # Pre-warm Scryfall cache: bulk-fetch all unique uncached cards before the first wave
        # so concurrent games don't all race to fetch the same cards simultaneously.
        # Uses Scryfall's /cards/collection endpoint (75 cards per request) instead of
        # individual /cards/named requests (which hit rate limits with new decks).
        if n_concurrent > 3:
            unique_decks = set()
            for _, fmt, d1, d2, _ in matchups:
                if d1 and fmt != "draft":
                    unique_decks.add(d1)
                if d2 and fmt != "draft":
                    unique_decks.add(d2)
            if unique_decks:
                # Collect ALL card names across all decks
                all_card_names = []
                for deck_name in unique_decks:
                    deck_data = self._load_deck_by_name(deck_name)
                    if deck_data:
                        for card_entry in deck_data.get("cards", []):
                            card_name = card_entry.get("name", "")
                            if card_name:
                                all_card_names.append(card_name)
                        for key in ("commander", "signature_spell"):
                            cmd_name = deck_data.get(key)
                            if cmd_name:
                                all_card_names.append(cmd_name)

                # Filter to uncached only
                uncached = [n for n in all_card_names if n.lower() not in self.engine.deck_loader.card_cache]
                # Deduplicate
                seen = set()
                uncached_unique = []
                for n in uncached:
                    if n.lower() not in seen:
                        seen.add(n.lower())
                        uncached_unique.append(n)

                if uncached_unique:
                    await batch_thread.send(
                        f"🔄 Pre-warming Scryfall cache: {len(uncached_unique)} uncached cards "
                        f"across {len(unique_decks)} decks (bulk fetch, ~{len(uncached_unique)//75 + 1} requests)...")
                    cache_start = asyncio.get_event_loop().time()
                    fetch_count = await self.engine.deck_loader.fetch_card_data_bulk(uncached_unique)
                    cache_elapsed = asyncio.get_event_loop().time() - cache_start
                    cache_size = len(self.engine.deck_loader.card_cache)
                    await batch_thread.send(
                        f"✅ Cache warm: fetched {fetch_count} new cards in {cache_elapsed:.1f}s "
                        f"({cache_size} total cached)")
                else:
                    cache_size = len(self.engine.deck_loader.card_cache)
                    await batch_thread.send(f"✅ All {cache_size} cards already cached — no Scryfall fetch needed")

        try:
            for wave_start in range(0, len(matchups), n_concurrent):
                if self._batch_stop_flag:
                    await batch_thread.send(f"⏹️ **Stopped** after {len(results)} games.")
                    break
                wave = matchups[wave_start:wave_start + n_concurrent]
                await batch_thread.send(
                    f"🚀 **Wave {wave_start // n_concurrent + 1}:** "
                    f"{', '.join(f'#{m[0]}' for m in wave)}")
                async def _run_one(mt):
                    mn, fmt, d1, d2, desc = mt
                    if fmt == "draft":
                        cube_cog = self.bot.get_cog("Cube Draft")
                        if cube_cog:
                            return await cube_cog._run_autodraft(ctx.channel, cube_source=d1 or "test")
                        return {"format": "draft", "outcome": "crash", "error": "No CubeDraftCog",
                                "turns": 0, "duration_seconds": 0, "deck1": d1, "deck2": "",
                                "winner": None, "p1_life": 0, "p2_life": 0, "thread_id": None}
                    return await self._run_single_autoplay(
                        ctx.channel, fmt, d1, d2, matchup_label=f"#{mn} {d1} vs {d2}",
                        force_claude=force_claude, openrouter_model=openrouter_model)
                wave_results = await asyncio.gather(*[_run_one(m) for m in wave], return_exceptions=True)
                icons = {"win_p1": "🟢 P1", "win_p2": "🔵 P2", "timeout": "🟡 Draw",
                         "crash": "🔴 ERR", "circuit_breaker": "⚡ CB"}
                for (mn, fmt, d1, d2, desc), result in zip(wave, wave_results):
                    if isinstance(result, Exception):
                        result = {"format": fmt, "deck1": d1, "deck2": d2, "outcome": "crash",
                                  "winner": None, "turns": 0, "p1_life": 0, "p2_life": 0,
                                  "error": str(result)[:200], "thread_id": None, "duration_seconds": 0}
                    result["matchup"] = mn
                    result["description"] = desc
                    results.append(result)
                    line = f"  → #{mn} [{icons.get(result['outcome'], '?')}] {result['turns']}t, {result['duration_seconds']:.0f}s"
                    if result.get("error"):
                        line += f" ❌ {result['error'][:80]}"
                    await batch_thread.send(line)
                if len(results) >= 3 and all(r["outcome"] in {"crash", "circuit_breaker"} for r in results[-3:]):
                    await batch_thread.send("🛑 **Auto-aborted:** last 3 games all failed.")
                    break
                await asyncio.sleep(2)
            await self._post_batch_summary(batch_thread, results, spec)
        except Exception as e:
            import traceback
            traceback.print_exc()
            await batch_thread.send(f"❌ Parallel batch crashed: {e}")
        finally:
            self._batch_running = False
            self._batch_stop_flag = False

    @commands.command(name="autoplay-resume")
    async def autoplay_resume(self, ctx, concurrency: str = "3"):
        """Resume autoplay from where it left off by scanning existing logs.

        Usage:
            !autoplay-resume        - Resume with 3 concurrent games
            !autoplay-resume 4      - Resume with 4 concurrent games
        """
        # Scan logs to find which matchup numbers have been completed
        import os, re
        log_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "logs")
        completed = set()
        if os.path.isdir(log_dir):
            for fname in os.listdir(log_dir):
                if fname.endswith("_console.log"):
                    fpath = os.path.join(log_dir, fname)
                    try:
                        with open(fpath, 'r', encoding='utf-8', errors='ignore') as f:
                            first_lines = f.read(2000)
                        # Look for matchup label pattern: #N deck1 vs deck2
                        m = re.search(r'#(\d+)\s+\w+\s+vs\s+\w+', first_lines)
                        if m:
                            completed.add(int(m.group(1)))
                    except Exception:
                        pass
        if not completed:
            await ctx.send("No completed matchups found in logs. Use `!autoplay-parallel all 3` to start fresh.")
            return
        max_completed = max(completed)
        resume_from = max_completed + 1
        if not hasattr(self, 'AUTOPLAY_MATRIX') or not self.AUTOPLAY_MATRIX:
            await ctx.send("❌ No AUTOPLAY_MATRIX defined.")
            return
        max_matchup = max(n for n, *_ in self.AUTOPLAY_MATRIX)
        if resume_from > max_matchup:
            await ctx.send(f"✅ All {max_matchup} matchups already completed! ({len(completed)} found in logs)")
            return
        await ctx.send(
            f"📊 Found {len(completed)} completed matchups (up to #{max_completed}).\n"
            f"▶️ Resuming from **#{resume_from}** to #{max_matchup} with {concurrency} concurrent...")
        # Delegate to autoplay-parallel with the right range
        ctx.message.content = f"!autoplay-parallel {resume_from}-{max_matchup} {concurrency}"
        await self.autoplay_parallel(ctx, spec=f"{resume_from}-{max_matchup}", concurrency=concurrency)

    @commands.command(name="autoplay-stop")
    async def autoplay_stop(self, ctx):
        """Stop the current autoplay — kills the current game immediately."""
        if not self._batch_running:
            await ctx.send("No batch is running.")
            return
        self._batch_stop_flag = True
        await ctx.send("⏹️ Aborting current game and stopping batch...")

    async def _post_batch_summary(self, thread, results: list, spec: str):
        """Post a formatted summary table to the batch status thread."""
        wins_p1 = sum(1 for r in results if r["outcome"] == "win_p1")
        wins_p2 = sum(1 for r in results if r["outcome"] == "win_p2")
        timeouts = sum(1 for r in results if r["outcome"] == "timeout")
        crashes = sum(1 for r in results if r["outcome"] == "crash")
        cb = sum(1 for r in results if r["outcome"] == "circuit_breaker")
        total_time = sum(r["duration_seconds"] for r in results)

        header = (
            f"**Batch Complete: {spec}**\n"
            f"Games: {len(results)} | P1 wins: {wins_p1} | P2 wins: {wins_p2} | "
            f"Timeouts: {timeouts} | Crashes: {crashes}"
            + (f" | Circuit breakers: {cb}" if cb else "") +
            f"\nTotal time: {total_time/60:.1f} minutes\n")
        await thread.send(header)

        # Build table rows, chunked to fit Discord's 2000 char limit
        rows = []
        for r in results:
            outcome_str = {"win_p1": "P1 Win", "win_p2": "P2 Win",
                           "timeout": "Timeout", "crash": "CRASH",
                           "circuit_breaker": "CB", "aborted": "ABORT"}.get(r["outcome"], r["outcome"])
            rows.append(
                f"{r['matchup']:>3} {r['format']:<10} {r['deck1']:<14} {r['deck2']:<14} "
                f"{outcome_str:<8} {r['turns']:>3}t {r['duration_seconds']:>5.0f}s")

        # Send in chunks
        chunk = "```\n  # Format     Deck1          Deck2          Result    Turns  Time\n"
        for row in rows:
            if len(chunk) + len(row) + 5 > 1900:
                chunk += "```"
                await thread.send(chunk)
                chunk = "```\n"
            chunk += row + "\n"
        if chunk.strip() != "```":
            chunk += "```"
            await thread.send(chunk)

        # List errors if any
        errors = [r for r in results if r.get("error")]
        if errors:
            err_msg = "**Errors:**\n"
            for r in errors:
                err_msg += f"\u2022 #{r['matchup']}: {r['error'][:120]}\n"
            if len(err_msg) > 1900:
                err_msg = err_msg[:1900] + "..."
            await thread.send(err_msg)

    def _get_game(self, ctx) -> Optional[GameState]:
        """Get the game for the current thread."""
        return self.engine.games.get(ctx.channel.id)

    # =========================================================================
    # !undo — snapshot stack for risky commands
    # =========================================================================
    #
    # Architecture (May 25 sprint, OSS-prep #2):
    #   Each "risky" command (!play, !cast, !attack, !activate, !resolve,
    #   !judge --apply, !fix) calls `_snapshot_for_undo(game, label)` AFTER
    #   permission checks but BEFORE mutation. The snapshot is a full
    #   `game.to_dict()` payload pushed onto `game._undo_stack`, capped at
    #   `_UNDO_MAX_DEPTH` (default 5). `!undo` pops the most recent snapshot
    #   and restores the game via `GameState.from_dict()`, replacing the
    #   game reference in `self.engine.games[thread_id]`.
    #
    # Why this design:
    #   - The existing `to_dict/from_dict` round-trip is already used by the
    #     engine's save_game/load_game path (engine.py:359, 381) and by
    #     `!resumegame` (cog.py:633). Reusing it means undo gets every field
    #     the persistence layer already supports — players, battlefield,
    #     life totals, counters, stack entries, attached auras, etc.
    #   - Bounded depth (5) keeps memory usage modest. A snapshot for a
    #     mid-game Commander board is ~50-100KB serialized; 5 × 100KB = 500KB
    #     per game, which is fine in-process.
    #   - Snapshots live ON the game object (not on the cog) so they
    #     naturally die when the game ends.
    #   - The label is included in the !undo confirmation message so the
    #     player can see "Undid: Alice played Lightning Bolt" rather than
    #     a bare "Undone."
    #
    # NOT snapshotting during autoplay: the bot won't undo itself, and 5
    # snapshots × hundreds of bot actions/sec would balloon memory. The
    # helper short-circuits when `game.is_autoplay` is True.

    _UNDO_MAX_DEPTH = 5

    def _snapshot_for_undo(self, game, label: str) -> None:
        """Capture pre-action state so !undo can restore it later.

        Call this AFTER permission/legality checks pass but BEFORE any
        mutation to game state. Safe to call multiple times — each call
        adds a new snapshot; over-depth oldest entries are dropped.
        """
        if game is None:
            return
        if getattr(game, 'is_autoplay', False):
            # Autoplay won't undo itself; skip the cost.
            return
        try:
            if not hasattr(game, '_undo_stack'):
                game._undo_stack = []
            snapshot = {
                "label": label,
                "ts": datetime.now().isoformat(),
                "state": game.to_dict(),
            }
            game._undo_stack.append(snapshot)
            # Bound depth — drop oldest if over cap. Keeps memory bounded.
            while len(game._undo_stack) > self._UNDO_MAX_DEPTH:
                game._undo_stack.pop(0)
        except Exception as e:
            # Snapshot failures must never block the actual command. Log
            # and continue — !undo just won't work for this entry.
            print(f"[UNDO] snapshot failed for '{label}': {e}")

    @commands.command(name="undo")
    async def undo_command(self, ctx):
        """
        Revert the most recent risky action (play / cast / attack / activate /
        resolve / fix / judge --apply).

        Snapshots are taken automatically before each risky command, capped
        at the last 5. Stack-resolution mid-flight is not reconstructed
        (CR 608 transient state) — undoing a cast restores the pre-cast
        board, the spell is back in hand.

        Usage:
            !undo                - Revert the most recent action
        """
        game = self._get_game(ctx)
        if not game:
            await ctx.send("No active game in this thread!")
            return
        stack = getattr(game, '_undo_stack', None)
        if not stack:
            await ctx.send("⏪ Nothing to undo.")
            return
        snapshot = stack.pop()
        try:
            restored = GameState.from_dict(snapshot["state"])
        except Exception as e:
            print(f"[UNDO] restore failed: {e}")
            await ctx.send(
                f"⚠️ Couldn't restore the snapshot ({e}). "
                f"This is a bug — the game state is unchanged."
            )
            # Put the snapshot back so the user can retry or report it.
            stack.append(snapshot)
            return
        # The restored game is a fresh object — carry over the undo stack
        # (the surviving snapshots) so successive undos walk back further.
        restored._undo_stack = stack
        # Carry over runtime-only flags that to_dict doesn't preserve.
        for attr in ('is_autoplay', '_autoplay_thread'):
            if hasattr(game, attr):
                setattr(restored, attr, getattr(game, attr))
        self.engine.games[ctx.channel.id] = restored
        depth_left = len(stack)
        label = snapshot.get("label", "previous action")
        await ctx.send(
            f"⏪ Undid: **{label}**\n"
            f"({depth_left} more undo{'s' if depth_left != 1 else ''} available)"
        )
        print(f"[UNDO] Restored snapshot '{label}' (depth_left={depth_left})")

    def _not_your_turn_msg(self, game) -> str:
        """Format the "not your turn" rejection with autoplay-aware context.

        May 18 audit: a user typed `!pass` during a stalled autoplay game to
        try to unstick it and got a bare "It's not your turn!" with no hint
        that autoplay was running. Surface the autoplay state so the human
        can act on it (use `!autoplay-stop` instead of poking at the prompt).
        """
        if getattr(game, 'is_autoplay', False):
            try:
                active = game.players[game.active_player_index].name
            except (IndexError, AttributeError):
                active = "the AI"
            return (f"⏸️ It's not your turn — this game is in autoplay "
                    f"({active}'s turn). Use `!autoplay-stop` to halt the batch.")
        return "It's not your turn!"


# =============================================================================
# SETUP
# =============================================================================

async def setup(bot):
    """Add the cog to the bot."""
    await bot.add_cog(MTGGameCog(bot))
