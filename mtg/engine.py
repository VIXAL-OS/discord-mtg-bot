"""GameEngine — turn loop, autoplay, and game lifecycle orchestration.

The largest class in the engine (~9k lines). Owns:

    - Game initialization (deck loading, mulligans, opening hand)
    - Turn flow (advance_phase, end_turn, untap step, draw step)
    - Action dispatch from Discord commands (play_land, cast_spell, etc.)
    - Combat orchestration (declare attackers/blockers/damage)
    - Autoplay (Claude-vs-Claude games for playtesting — see CLAUDE.md
      "Autoplay System" section for design notes)
    - Strategist + actor split for the AI (Phase 2 Parallel CoT)

Phase 2 of the OSS refactor would split this further into:

    - actions.py — _execute_action and the human-side action dispatch
    - autoplay.py — Rick logic and the autoplay loop
    - lifecycle.py — game init / mulligan / save-load

But that requires breaking up the class itself, which is a much bigger
change than the Phase 1 mechanical extraction. For now GameEngine stays
as one large coherent class.

Extracted from mtg_game.py during the Phase 1 OSS-readability refactor.
"""

import asyncio
import json
import logging
import os
import random
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import anthropic

from mtg.claude_player import ClaudePlayer
from mtg.constants import (
    Phase, Zone, PHASE_ORDER, FORMAT_STARTING_LIFE, COMMAND_ZONE_FORMATS,
    MELD_PAIRS,
)
from mtg.deck_loader import DeckLoader
from mtg.helpers import (
    _collapse_repeated_life_gain, _should_emit_resolve_hint,
    _normalize_pw_ability_idx, _resolve_player_or_card_target,
    get_mdfc_info,
)
from mtg.models import Card, Player, GameState, StackEntry, FormatValidator
from mtg import events
# Slice 2b (July 21, 2026): importing mtg.triggers registers the
# PERMANENT_ENTERED bus subscribers (creature watcher dispatch, snow
# watcher, parity recorder) at module load. The direct scan calls are gone,
# so an engine used without this import would silently drop every
# creature-enters trigger — import it eagerly at the hub.
from mtg import triggers as _triggers_bus_registration  # noqa: F401
from mtg.rules_engine import RulesEngine

# Optional: visual board renderer
try:
    from board_visual import render_game_board, render_player_hand
    HAS_BOARD_VISUAL = True
except ImportError:
    HAS_BOARD_VISUAL = False

# Optional: spell resolver engine
try:
    from rules import SpellResolver, TargetMode, ExecutionContext
    HAS_SPELL_RESOLVER = True
except ImportError:
    HAS_SPELL_RESOLVER = False

# Optional: card-specific effect templates
try:
    from rules.effect_templates import get_effect_library, build_game_context
    HAS_EFFECT_TEMPLATES = True
except ImportError:
    HAS_EFFECT_TEMPLATES = False

# Optional: structured mana cost parser
try:
    from rules.mana import ManaCost
    HAS_MANA_ENGINE = True
except ImportError:
    HAS_MANA_ENGINE = False

# Optional: 7-layer continuous effects (CR 613)
try:
    from rules.layers import Layer, create_pump_effect
    HAS_LAYERS_ENGINE = True
except ImportError:
    HAS_LAYERS_ENGINE = False

# Optional: replacement effects ("if would, instead")
try:
    from rules.replacement import GameEvent, EventType
    HAS_REPLACEMENT_ENGINE = True
except ImportError:
    HAS_REPLACEMENT_ENGINE = False

# Optional: pre-cast target legality
try:
    from rules.targeting_helpers import (
        _validate_target_for_action,
        _validate_player_target_for_action,
        _find_any_valid_target,
        _spell_requires_targets,
    )
    HAS_TARGETING = True
except ImportError:
    HAS_TARGETING = False

# Optional: planeswalker abilities
try:
    from rules.planeswalker import PlaneswalkerManager
    HAS_PLANESWALKER = True
except ImportError:
    HAS_PLANESWALKER = False

# Optional: XMage bridge
try:
    from rules.xmage_bridge import Permanent as XMagePermanent
    from rules.xmage_bridge import GameState as XMageGameState
    HAS_XMAGE_BRIDGE = True
except ImportError:
    HAS_XMAGE_BRIDGE = False

# Optional: XMage action translator
try:
    from rules.xmage_action_translator import XMageActionTranslator
    HAS_XMAGE_TRANSLATOR = True
except ImportError:
    HAS_XMAGE_TRANSLATOR = False

# Optional: LLM adapters for autoplay
try:
    from rules.llm_adapter import (
        create_deepseek_adapter, create_openrouter_adapter,
        create_deepseek_reasoner_adapter,
    )
except ImportError:
    create_deepseek_adapter = None
    create_openrouter_adapter = None
    create_deepseek_reasoner_adapter = None


# =============================================================================
# GAME ENGINE
# =============================================================================

def _collapse_repeated_life_gain(messages):
    """Collapse consecutive identical life-gain messages for the same player into
    one line with an ×N multiplier. Handles the Soul Warden / Impact Tremors
    style cascade where N copies of a triggered ability each emit their own line.

    Input format: "💚 **{name}** gains {amount} life (life: {final})"
    Output format: "💚 **{name}** gains {amount} life ×N (life: {final})"

    Only collapses runs of identical prefixes (same player, same amount). The
    trailing life total is taken from the LAST message in the run (reflects the
    cumulative total after all triggers fired).
    """
    if not messages or len(messages) < 2:
        return messages

    import re as _re
    pat = _re.compile(r'^(💚 \*\*[^*]+\*\* gains (\d+) life) \(life: (\d+)\)$')
    out = []
    i = 0
    while i < len(messages):
        m = pat.match(messages[i] or "")
        if not m:
            out.append(messages[i])
            i += 1
            continue
        prefix, amount, _life = m.group(1), m.group(2), m.group(3)
        # Scan forward for consecutive identical-prefix messages
        run_end = i
        last_life = _life
        while run_end + 1 < len(messages):
            m2 = pat.match(messages[run_end + 1] or "")
            if not m2 or m2.group(1) != prefix:
                break
            last_life = m2.group(3)
            run_end += 1
        count = run_end - i + 1
        if count > 1:
            out.append(f"{prefix} x{count} (life: {last_life})")
        else:
            out.append(messages[i])
        i = run_end + 1
    return out


def _normalize_action_target(action):
    """Normalize the 'target' field of an AI action dict to a string or None.

    The AI sometimes packs structured data into the target field (e.g.
    `{"X": 2}` for X-cost spells, or a list of targets). Downstream code
    calls .lower() / find_card / etc. on the target value and crashes with
    AttributeError when the value isn't a string.

    Side effects: if the dict contains an X-cost value and the action lacks
    one, hoist it onto the action under x_value. Mutates `action` in place
    via assignment of normalized target. Returns the normalized string/None.
    """
    target_name = action.get("target")
    if isinstance(target_name, dict):
        if 'X' in target_name and not any(k in action for k in ('x_value', 'X', 'x')):
            action['x_value'] = target_name['X']
        target_name = (target_name.get('card') or target_name.get('name')
                       or target_name.get('target') or None)
    elif isinstance(target_name, list):
        target_name = next((t for t in target_name if isinstance(t, str)), None)
    elif target_name is not None and not isinstance(target_name, str):
        target_name = str(target_name) if target_name else None
    action['target'] = target_name
    return target_name


def _should_emit_resolve_hint(game, effect_key: str) -> bool:
    """Check if a !judge/!resolve hint should be shown for this effect.

    In autoplay: only emit once per unique effect (prevents triple-judge spam).
    In normal play: always emit (human needs the prompt).
    """
    if not getattr(game, 'is_autoplay', False):
        return True  # Always show in human games
    hints = getattr(game, '_judge_hints_emitted', None)
    if hints is None:
        game._judge_hints_emitted = set()
        hints = game._judge_hints_emitted
    if effect_key in hints:
        return False
    hints.add(effect_key)
    return True


def _satisfies_sacrifice_cost(card, cost_text: str, game=None,
                              source=None) -> bool:
    """Return whether *card* can pay the typed sacrifice activation cost."""
    if card is None:
        return False
    _cost = (cost_text or '').lower()
    # July 24 batch-6 (reviewer A1): "Sacrifice ANOTHER creature" (Yawgmoth)
    # — the source itself can't pay it.
    if ('sacrifice another creature' in _cost and source is not None
            and getattr(card, 'id', None) == getattr(source, 'id', None)):
        return False
    if ('sacrifice a creature' in _cost
            or 'sacrifice another creature' in _cost):
        return card.is_creature(game)
    return True


def _activation_mana_cost(cost_text: str) -> str:
    """Extract the mana-symbol portion of an activated ability cost."""
    symbols = re.findall(r'\{([^}]+)\}', cost_text or '', re.IGNORECASE)
    return ''.join(f'{{{symbol.upper()}}}' for symbol in symbols
                   if symbol.upper() not in ('T', 'Q'))


class GameEngine:
    """Core game logic with rules enforcement and persistence."""
    
    GAMES_DIR = "data/games"
    
    def __init__(self, claude_client: anthropic.Anthropic, usage_callback=None):
        self.deck_loader = DeckLoader()
        self.claude_ai = ClaudePlayer(claude_client, usage_callback=usage_callback)
        self.claude_ai.engine_ref = self  # Back-reference for plan_turn() PW access
        self.rules = RulesEngine(claude_client, usage_callback=usage_callback)  # Rules enforcement
        self.rules.engine_ref = self  # Back-reference for landfall triggers from spells
        self.games: Dict[int, GameState] = {}  # thread_id -> GameState
        self.ended_games: Dict[int, GameState] = {}  # Keep ended games for chat context
        self.claude_deck: Optional[Dict] = None  # Loaded deck for Claude
        self.claude_client = claude_client  # Keep reference for spell resolver
        self.usage_callback = usage_callback  # Track API usage
        # XMage serialization cache — skip re-serializing when board unchanged
        self._cached_xmage_state = None
        self._cached_xmage_fingerprint = None
        self._cached_xmage_name_map = None

        # Initialize spell resolver if available
        if HAS_SPELL_RESOLVER:
            self.spell_resolver = SpellResolver(self, claude_client)
        else:
            self.spell_resolver = None
        
        # Initialize effect template library (tier 1.5)
        if HAS_EFFECT_TEMPLATES:
            self.effect_library = get_effect_library()
        else:
            self.effect_library = None
        
        # Initialize planeswalker manager if available
        if HAS_PLANESWALKER:
            self.planeswalker_manager = PlaneswalkerManager(claude_client)
            self.planeswalker_manager._rules_engine = self.rules
        else:
            self.planeswalker_manager = None

        # XMage bridge placeholder — async startup happens in MTGGameCog.cog_load()
        # because the bridge spawns a Java subprocess that needs async I/O
        self.xmage_bridge = None
        self._xmage_available = False
        if HAS_XMAGE_TRANSLATOR and HAS_EFFECT_TEMPLATES:
            self._xmage_translator = XMageActionTranslator(self.effect_library)
        elif HAS_XMAGE_TRANSLATOR:
            self._xmage_translator = XMageActionTranslator()
        else:
            self._xmage_translator = None

        # Ensure games directory exists
        os.makedirs(self.GAMES_DIR, exist_ok=True)
        
        # Load any saved games on startup
        self._load_all_games()
    
    def _load_all_games(self):
        """Load all saved games from disk.

        - Ended games → delete (cleanup).
        - Stale games (>24h untouched, likely from a crashed/abandoned session)
          → MOVE to data/games/stale/. They no longer clutter the active dir
          or spam the startup log, but they're still recoverable if you ever
          want to investigate (open the JSON or move it back). Auto-purges
          archived games older than 30 days so the stale dir doesn't grow
          forever after months of crash recovery.
        - Otherwise → load into self.games.

        Recovery: to actually resume a stale game (e.g. computer restarted
        mid-batch and you want to keep playing), move its .json back from
        data/games/stale/ to data/games/ and use !force-resume in the thread.
        """
        if not os.path.exists(self.GAMES_DIR):
            return

        stale_hours = 24       # untouched longer than this → archive
        archive_purge_days = 30  # files in stale/ older than this → delete

        stale_dir = os.path.join(self.GAMES_DIR, 'stale')

        # 1) Purge old files from the archive so it doesn't grow unbounded.
        if os.path.exists(stale_dir):
            purge_cutoff = datetime.now().timestamp() - archive_purge_days * 86400
            for fname in os.listdir(stale_dir):
                if not fname.endswith('.json'):
                    continue
                fpath = os.path.join(stale_dir, fname)
                try:
                    if os.path.getmtime(fpath) < purge_cutoff:
                        os.remove(fpath)
                        print(f"[GAME-LOAD] Purged old archived game {fname} "
                              f"(>{archive_purge_days}d in stale/)")
                except OSError as e:
                    print(f"⚠️ Failed to purge {fname}: {e}")

        # 2) Scan the active games dir.
        archived_count = 0
        for filename in os.listdir(self.GAMES_DIR):
            if not filename.endswith('.json'):
                continue
            filepath = os.path.join(self.GAMES_DIR, filename)
            if not os.path.isfile(filepath):
                continue  # Skip directories like stale/
            try:
                file_age_hours = (datetime.now().timestamp() - os.path.getmtime(filepath)) / 3600
                if file_age_hours > stale_hours:
                    # Move to archive (create on demand). Don't delete — you
                    # might want to look at why a batch crashed, and the JSON
                    # is the only record of mid-game state.
                    os.makedirs(stale_dir, exist_ok=True)
                    dest = os.path.join(stale_dir, filename)
                    # If a same-named file already exists in stale/ (rare —
                    # only if a thread_id collides across crashes), suffix
                    # with the original mtime to keep both.
                    if os.path.exists(dest):
                        suffix = f".{int(os.path.getmtime(filepath))}.json"
                        dest = os.path.join(stale_dir, filename[:-5] + suffix)
                    try:
                        os.replace(filepath, dest)
                        archived_count += 1
                    except OSError as e:
                        print(f"⚠️ Failed to archive {filename}: {e}")
                    continue

                with open(filepath, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                game = GameState.from_dict(data)
                # Only load games that aren't ended
                if not game.ended:
                    game._rules_engine = self.rules  # July 21: live wiring (was test-only)
                    self.games[game.thread_id] = game
                    print(f"✅ Loaded game in thread {game.thread_id} "
                          f"(turn {game.turn_number}, {file_age_hours:.1f}h old)")
                else:
                    # Clean up ended games
                    os.remove(filepath)
                    print(f"[GAME-LOAD] Cleaned up ended game {filename}")
            except Exception as e:
                print(f"⚠️ Failed to load game from {filename}: {e}")

        if archived_count:
            print(f"[GAME-LOAD] Archived {archived_count} stale game(s) to {stale_dir} "
                  f"(>{stale_hours}h untouched — likely crashed/aborted sessions)")
    
    def save_game(self, game: GameState):
        """Save a game to disk."""
        filepath = os.path.join(self.GAMES_DIR, f"{game.thread_id}.json")
        try:
            with open(filepath, 'w', encoding='utf-8') as f:
                json.dump(game.to_dict(), f, indent=2)
        except Exception as e:
            print(f"⚠️ Failed to save game {game.thread_id}: {e}")
    
    def delete_game(self, thread_id: int):
        """Delete a saved game but keep it in ended_games for chat context."""
        # Store in ended_games before removing (for the bot to comment on)
        if thread_id in self.games:
            self.ended_games[thread_id] = self.games.pop(thread_id)
            # May 2 audit: bumped from 10 → 50. The 10-game cap was too tight
            # during full-batch autoplay runs (122 games in one afternoon evicted
            # earlier games before users had a chance to ask the bot about
            # them). 50 games is still a few MB of state, well within budget.
            if len(self.ended_games) > 50:
                oldest_key = next(iter(self.ended_games))
                del self.ended_games[oldest_key]
        
        # Clean up priority system if using integrated engine
        if hasattr(self, 'integrated') and self.integrated:
            self.integrated.cleanup_priority(thread_id)
        
        # Delete the save file
        filepath = os.path.join(self.GAMES_DIR, f"{thread_id}.json")
        if os.path.exists(filepath):
            try:
                os.remove(filepath)
            except Exception as e:
                print(f"⚠️ Failed to delete game file {thread_id}: {e}")
    
    async def create_game(
        self, 
        thread_id: int,
        player1_name: str,
        player1_id: Optional[int],
        player2_name: str,
        player2_id: Optional[int],
        format: str = "standard",
        player1_deck: Optional[Dict] = None,
        player2_deck: Optional[Dict] = None
    ) -> GameState:
        """Create a new game."""
        starting_life = FORMAT_STARTING_LIFE.get(format, 20)
        
        player1 = Player(
            name=player1_name,
            user_id=player1_id,
            is_claude=player1_id is None,
            life=starting_life,
        )
        
        player2 = Player(
            name=player2_name,
            user_id=player2_id,
            is_claude=player2_id is None,
            life=starting_life,
        )
        
        # Load decks - explicitly passed decks take priority over self.claude_deck
        # (self.claude_deck is shared mutable state that causes race conditions in parallel autoplay)
        if player1_deck:
            await self._load_player_deck(player1, player1_deck, owner_index=0)
        elif player1.is_claude and self.claude_deck:
            await self._load_player_deck(player1, self.claude_deck, owner_index=0)

        if player2_deck:
            await self._load_player_deck(player2, player2_deck, owner_index=1)
        elif player2.is_claude and self.claude_deck:
            await self._load_player_deck(player2, self.claude_deck, owner_index=1)
        
        game = GameState(
            thread_id=thread_id,
            format=format,
            players=[player1, player2],
            turn_number=0,
        )

        game._rules_engine = self.rules  # July 21: live wiring (was test-only)
        self.games[thread_id] = game
        self.save_game(game)  # Persist to disk
        return game

    def setup_stack(self, game: GameState, auto_pass_seconds: float = 8.0,
                    send_func=None, ai_response_enabled: bool = False):
        """Initialize the PrioritySystem for a game with stack enabled.

        Call this after create_game() when you want stack/instant-speed interaction.

        Args:
            game: The game state
            auto_pass_seconds: Timer before auto-passing (use 0.05 for autoplay)
            send_func: Async callable(str) for sending Discord messages (or None for silent)
            ai_response_enabled: If True, AI players auto-respond via on_priority_change
        """
        try:
            from rules.priority import PrioritySystem, PriorityAction

            game.stack_enabled = True
            game._stack_resolution_event = asyncio.Event()

            # May 7 audit fix #1: stash send_func on the game so cast_spell_async
            # can emit the cast announcement BEFORE awaiting the priority window.
            # Without this, a counterspell response cast during the priority wait
            # gets posted to Discord before the originating cast announcement —
            # producing the out-of-order sequence: "B responds with Counterspell",
            # "A cast Sun Titan", "Sun Titan was countered".
            game._stack_send_func = send_func

            # Capture engine reference for the closure
            engine = self

            async def _send(content):
                """Send a Discord message if send_func is available."""
                if send_func:
                    try:
                        await send_func(content)
                    except Exception as e:
                        print(f"[STACK] Discord send error: {e}")

            async def on_stack_resolve(stack_obj):
                """Called by PrioritySystem when top of stack resolves.

                Matches the resolved StackObject back to its StackEntry via priority_id,
                then fires that specific entry's resolution_event. This enables stack wars
                (counter-counter) because each spell waits on its own event.

                For triggered abilities (is_spell=False), resolves effects directly
                through the tiered cascade since they don't have a resolution_event.
                """
                print(f"[STACK] Resolving: {stack_obj.name} (controller: {stack_obj.controller})")
                # Find the matching StackEntry by priority_id and fire its event
                matched = False
                for entry in reversed(game.stack):
                    if getattr(entry, 'priority_id', None) == stack_obj.id:
                        # July 21 batch audit (CR 608 LIFO gate): the
                        # PrioritySystem's stack can diverge from game.stack —
                        # response casts and cast-trigger entries push onto
                        # game.stack but not always into the ps. In
                        # game_1529172174773157998 the ps resolved Animate
                        # Dead (its top) while Disallow (targeting it!) and a
                        # Talrand trigger sat ABOVE it on game.stack — the
                        # reanimation resolved, Disallow fizzled, and the
                        # should-have-been-countered spell stayed. If the
                        # matched entry is buried, do NOT resolve it — the
                        # caster's _await_stack_window timeout + LIFO
                        # extension loop retries once it truly is on top.
                        if game.stack and game.stack[-1] is not entry:
                            _top = game.stack[-1]
                            _top_name = (_top.card.name if getattr(_top, 'card', None)
                                         else getattr(_top, 'trigger_source', '?'))
                            print(f"[STACK-LIFO-GUARD] {stack_obj.name} matched but is "
                                  f"buried under {_top_name} on game.stack — not "
                                  f"resolving out of order (CR 608)")
                            return
                        # Check if this is a triggered ability (no resolution_event)
                        if not entry.is_spell:
                            # [TRIGGER-RESOLVE] Resolve triggered ability from stack
                            if entry.countered:
                                print(f"[STIFLE] {entry.trigger_source}'s ability was countered")
                                if send_func:
                                    try:
                                        await send_func(f"🚫 **{entry.trigger_source}**'s triggered ability is countered!")
                                    except Exception:
                                        pass
                            else:
                                # [TARGETING] Set resolution source for triggered ability
                                game._current_resolution_source = (
                                    entry.trigger_source or (entry.card.name if entry.card else ""),
                                    entry.controller_name
                                )
                                # Resolve through template library
                                resolve_msgs = []
                                if HAS_EFFECT_TEMPLATES and entry.trigger_text:
                                    ctrl_name = entry.controller_name
                                    ctrl_idx = entry.controller_index
                                    opp_idx = 1 - ctrl_idx
                                    ctrl = game.players[ctrl_idx]
                                    opp = game.players[opp_idx]
                                    try:
                                        ctx = build_game_context(game, ctrl, opp, card=entry.card)
                                        lib = get_effect_library()
                                        actions, explanation = lib.resolve_etb(
                                            card_name=entry.trigger_source or entry.card.name,
                                            oracle_text=entry.trigger_text,
                                            controller=ctrl_name,
                                            opponent=opp.name,
                                            game_context=ctx,
                                        )
                                        if actions:
                                            for action in actions:
                                                if action.get("action") == "no_action":
                                                    reason = action.get("reason", "")
                                                    if reason:
                                                        resolve_msgs.append(f"⚡ {entry.trigger_source}: {reason}")
                                                    continue
                                                try:
                                                    msg = engine.rules._execute_action_on_state(game, action)
                                                    if msg:
                                                        resolve_msgs.append(msg)
                                                except Exception as e:
                                                    print(f"[TRIGGER-RESOLVE] Action failed: {e}")
                                            print(f"[TRIGGER-RESOLVE] Resolved {entry.trigger_source}: {explanation}")
                                    except Exception as e:
                                        print(f"[TRIGGER-RESOLVE] Error resolving {entry.trigger_source}: {e}")

                                if resolve_msgs and send_func:
                                    try:
                                        await send_func("\n".join(resolve_msgs))
                                    except Exception:
                                        pass
                                elif not resolve_msgs:
                                    print(f"[TRIGGER-RESOLVE] No template match for {entry.trigger_source}, "
                                          f"trigger text: {entry.trigger_text[:100]}")

                            # [TARGETING] Clear resolution source context
                            game._current_resolution_source = None
                            # Remove from game stack
                            if entry in game.stack:
                                game.stack.remove(entry)
                            matched = True
                            break

                        if entry.resolution_event:
                            entry.resolution_event.set()
                            print(f"[STACK] Fired resolution event for {stack_obj.name} (priority_id={stack_obj.id})")
                        matched = True
                        break
                if not matched:
                    # Fallback: fire shared event for legacy/non-priority callers.
                    # May 18 audit: with the spells.py fast-path now syncing
                    # game.stack ↔ PrioritySystem.stack via
                    # _drop_from_priority_stack(), this fallback should rarely
                    # fire. If it does, it means a stack object got into
                    # PrioritySystem without a matching StackEntry (race or
                    # legacy non-priority caller) — log it diagnostically so
                    # an audit can catch a regression.
                    print(f"[STACK] No priority_id match for {stack_obj.name} "
                          f"(likely already resolved via fast-path or non-priority caller); "
                          f"firing shared event no-op")
                    if game._stack_resolution_event:
                        game._stack_resolution_event.set()

            async def on_priority_change(player_name):
                """Called when priority passes to a different player."""
                print(f"[STACK] Priority → {player_name}")

                if not ai_response_enabled:
                    return  # Human games: wait for !pass / !respond commands

                # May 13 audit: pre-emptively cancel the auto-pass timer for
                # AI players so the timer can't fire while _handle_ai_priority
                # is still scheduling (the asyncio.create_task + sleep(0.01)
                # gap below was racing the 0.05s auto-pass timer, sometimes
                # auto-passing the AI before it ever got asked). Doing this
                # synchronously inside the callback — before the create_task —
                # closes that race. If the AI ultimately declines / passes,
                # _handle_ai_priority manually passes priority itself.
                target_player = None
                for p in game.players:
                    if p.name == player_name:
                        target_player = p
                        break
                if target_player is not None and (target_player.is_claude or target_player.user_id == 99999):
                    ps_local = game._priority_system
                    if ps_local:
                        try:
                            ps_local._cancel_pass_timer()
                        except Exception:
                            pass

                # Schedule AI response as a separate task (avoids lock deadlock)
                asyncio.create_task(_handle_ai_priority(player_name))

            async def _handle_ai_priority(player_name):
                """Check if AI player wants to respond, then pass or cast."""
                # Yield to ensure the lock from player_action is released
                await asyncio.sleep(0.01)

                # Find the player
                player = None
                player_idx = None
                for i, p in enumerate(game.players):
                    if p.name == player_name:
                        player = p
                        player_idx = i
                        break

                if player is None:
                    return

                # Only handle AI players (is_claude or pretend human with id=99999)
                if not player.is_claude and player.user_id != 99999:
                    return  # Real human: wait for !pass / !respond

                ps = game._priority_system
                if not ps:
                    return

                # May 13 audit: cancel the auto-pass timer at the TOP, not just
                # before decide_response. Previously the timer was 0.05s and
                # could fire while we were still doing affordability checks,
                # implicitly auto-passing the AI before it ever got asked.
                # `[STACK-AI] AI not queried (timeout)` fired 217 times across
                # the May 11 batch — each was a 6s dead-air wait where the
                # priority window had already collapsed. Now we control the
                # pass explicitly (every early-return below manually passes).
                try:
                    ps._cancel_pass_timer()
                except Exception:
                    pass

                # Check if we're in a combat priority window (empty stack is expected)
                in_combat_window = getattr(game, 'combat_priority_window', None) is not None

                # If stack is empty AND not in combat window, don't pass —
                # the game engine owns phase flow. After stack resolution,
                # cast_spell_async is waiting on the event and will continue.
                # Passing here would trigger an infinite priority bounce loop.
                if not game.stack and not in_combat_window:
                    print(f"[STACK-AI] {player_name} — stack empty, skipping (game engine owns phase flow)")
                    return

                # During combat windows with empty stack, the "spell" context is
                # the combat itself. During stack response, it's the top spell.
                if game.stack:
                    top_stack = game.stack[-1]
                    spell_name = top_stack.card.name if top_stack.card else "Unknown"
                    caster_name = top_stack.controller_name

                    # Auto-pass on triggered abilities (non-spell stack entries)
                    # unless player has Stifle-type effects that can counter the ability
                    if not top_stack.is_spell:
                        # Check if player has affordable Stifle-type cards
                        stifle_names = {"stifle", "trickbind", "tale's end", "voidslime", "disallow"}
                        has_stifle = False
                        for hand_card in player.hand:
                            card_name_lower = hand_card.name.lower()
                            oracle_lower = (hand_card.oracle_text or "").lower()
                            if (card_name_lower in stifle_names or
                                ("counter target" in oracle_lower and "ability" in oracle_lower)):
                                # Check if affordable
                                can_pay, _ = player.can_pay_mana_cost(hand_card.mana_cost)
                                if can_pay:
                                    has_stifle = True
                                    break

                        if has_stifle:
                            # Fall through to normal decide_response — player might want to Stifle
                            print(f"[STACK-AI] {player_name} has Stifle-type card — evaluating response to ability: {spell_name}")
                        else:
                            print(f"[STACK-AI] {player_name} auto-pass on triggered ability: {spell_name}")
                            await ps.player_action(player_name, PriorityAction.pass_priority())
                            return

                    # Don't respond to own spells — just pass
                    if caster_name == player_name:
                        try:
                            await ps.player_action(player_name, PriorityAction.pass_priority())
                        except Exception as e:
                            print(f"[STACK-AI] Error auto-passing own spell: {e}")
                        return
                else:
                    # Combat priority window — no spell on stack, but player can cast instants
                    spell_name = f"combat ({game.combat_priority_window})"
                    caster_name = game.players[game.active_player_index].name
                    top_stack = None

                # Pre-filter: does this player have instant-speed cards?
                instants = engine.claude_ai.has_instant_speed_cards(player)
                if not instants:
                    print(f"[STACK-AI] {player_name} has no instant-speed cards — auto-pass")
                    await ps.player_action(player_name, PriorityAction.pass_priority())
                    return

                # Check affordability (July 20: alternate-cost aware — FoW class)
                affordable = [c for c in instants
                              if player.can_pay_mana_cost(c.mana_cost)[0]
                              or player.can_pay_printed_alternate_cost(c)]
                if not affordable:
                    print(f"[STACK-AI] {player_name} has instants but can't afford any — auto-pass")
                    await ps.player_action(player_name, PriorityAction.pass_priority())
                    return

                # Cancel auto-pass timer while AI is deciding (prevents race condition
                # where 0.05s timer fires before decide_response() API call returns)
                ps._cancel_pass_timer()

                # Ask AI if it wants to respond
                print(f"[STACK-AI] Asking {player_name} about responding to {spell_name} "
                      f"(has {len(affordable)} affordable instant(s))")
                # May 7 audit: mark the top-of-stack entry as "AI was queried"
                # so cast_spell_async's timeout handler can tell the difference
                # between "AI declined" and "AI never got asked".
                if top_stack is not None:
                    top_stack._stack_ai_queried = True
                decision = await engine.claude_ai.decide_response(
                    game, player_idx, spell_name, caster_name
                )

                if not decision:
                    print(f"[STACK-AI] {player_name} passes on {spell_name}")
                    await ps.player_action(player_name, PriorityAction.pass_priority())
                    return

                # AI wants to cast a response — go through cast_spell_async for full
                # stack interaction (enables counter-counter wars / stack wars)
                response_card_name = decision.get("card", "")
                response_card = player.find_card(response_card_name, Zone.HAND)
                if not response_card:
                    # Fuzzy match
                    for c in affordable:
                        if c.name.lower() == response_card_name.lower() or response_card_name.lower() in c.name.lower():
                            response_card = c
                            break

                if not response_card:
                    print(f"[STACK-AI] {player_name} tried '{response_card_name}' but not found in hand")
                    await ps.player_action(player_name, PriorityAction.pass_priority())
                    return

                # Final mana check
                can_pay, reason = player.can_pay_mana_cost(response_card.mana_cost)
                if not can_pay:
                    print(f"[STACK-AI] {player_name} can't afford {response_card.name}: {reason}")
                    await ps.player_action(player_name, PriorityAction.pass_priority())
                    return

                # === Cast the response through the full stack flow ===
                # This pushes the response onto game.stack, gives all players priority
                # to respond (enabling counter-counter wars), waits for resolution,
                # then returns. No need to pass priority manually — cast_spell_async
                # handles the entire priority cycle for this spell.
                # Target: only point at the stack object when the response is
                # actually a counterspell. Otherwise leave target=None so
                # cast_spell_async auto-selects a valid target.
                # May 13 audit: previously this defaulted to `top_stack.card`,
                # which let Eerie Interlude (targets creatures you control) be
                # cast "targeting" Korvold-on-the-stack — illegal both because
                # Korvold isn't a creature on the battlefield and because
                # Claude doesn't control him. The spell went onto the stack
                # and then fizzled on resolution. Now we only forward the
                # stack target when the response card actually targets a
                # spell/ability on the stack.
                _resp_oracle = (response_card.oracle_text or '').lower()
                _targets_a_spell = (
                    'counter target' in _resp_oracle and
                    ('spell' in _resp_oracle or 'ability' in _resp_oracle)
                ) or (
                    # Stifle / Trickbind / Disallow — counter triggered/activated abilities
                    'counter target triggered' in _resp_oracle or
                    'counter target activated' in _resp_oracle
                )
                response_target = top_stack.card if (top_stack and _targets_a_spell) else None
                print(f"[STACK-AI] {player_name} casting response: {response_card.name}"
                      f"{f' targeting {spell_name}' if response_target else ' (auto-target)'}")
                try:
                    success, msg, effect_msgs = await engine.cast_spell_async(
                        game, player, response_card, target=response_target
                    )
                    if success:
                        await _send(f"⚡ **{player_name}** responds to **{spell_name}** with **{response_card.name}**!")
                        for em in (effect_msgs or []):
                            await _send(em)
                    else:
                        print(f"[STACK-AI] cast_spell_async failed for response: {msg}")
                        # Card was removed from hand but cast failed — put it back
                        # (cast_spell_async handles this internally, but just in case)
                except Exception as e:
                    print(f"[STACK-AI] Error casting response {response_card.name}: {e}")
                    import traceback
                    traceback.print_exc()

                # cast_spell_async already handled priority, resolution, and SBA.
                # Don't pass priority here — the spell went through the full cycle.

            game._priority_system = PrioritySystem(
                players=[p.name for p in game.players],
                auto_pass_seconds=auto_pass_seconds,
                on_stack_resolve=on_stack_resolve,
                on_priority_change=on_priority_change,
            )
            print(f"[STACK] PrioritySystem initialized for game {game.thread_id} "
                  f"(auto-pass: {auto_pass_seconds}s, ai_response: {ai_response_enabled})")
        except ImportError as e:
            print(f"[STACK] Could not import PrioritySystem: {e}")
            game.stack_enabled = False

    async def _combat_priority_round(self, game: GameState, send_func, window_name: str):
        """Give both players a priority window during combat.

        Used at key combat steps: after attackers declared, after blockers declared.
        Players can cast instant-speed spells (removal, combat tricks, pump).
        When all players pass on an empty stack, the combat window ends.

        Args:
            game: The game state
            send_func: Async callable(str) for sending messages
            window_name: Human-readable name (e.g., "after attackers declared")
        """
        if not game.stack_enabled or not game._priority_system:
            return  # No stack = no combat priority

        # Pre-filter: if nobody has affordable instants, skip entirely
        # This avoids unnecessary delays in autoplay
        any_instants = False
        for p in game.players:
            instants = self.claude_ai.has_instant_speed_cards(p) if self.claude_ai else []
            if instants:
                # July 20: alternate-cost aware — FoW class
                affordable = [c for c in instants
                              if p.can_pay_mana_cost(c.mana_cost)[0]
                              or p.can_pay_printed_alternate_cost(c)]
                if affordable:
                    any_instants = True
                    break
        if not any_instants:
            print(f"[COMBAT-PRIORITY] Skipped {window_name} (no affordable instants)")
            return

        ps = game._priority_system
        game.combat_priority_window = window_name

        # Set up combat window mode — PrioritySystem will signal us when done
        # instead of advancing the phase
        combat_done = asyncio.Event()

        # Temporarily set the combat_done callback
        original_callback = ps._on_combat_done
        async def _signal_combat_done():
            combat_done.set()
        ps._on_combat_done = _signal_combat_done
        ps.combat_window = True

        await send_func(f"⚔️ **Priority:** {window_name}. Cast instants with `!respond`, or `!pass`.")

        # Give active player priority and start the timer
        ps._passes_in_succession = []
        ps.priority_holder = ps.active_player
        await ps._notify_priority_change()
        ps._start_pass_timer()

        # Wait for all players to pass on empty stack
        try:
            await asyncio.wait_for(combat_done.wait(), timeout=60.0)
        except asyncio.TimeoutError:
            print(f"[COMBAT-PRIORITY] Timeout for {window_name}, continuing")
            ps.combat_window = False

        # Restore
        ps._on_combat_done = original_callback
        game.combat_priority_window = None
        print(f"[COMBAT-PRIORITY] {window_name} complete")

    async def create_game_from_cards(
        self,
        thread_id: int,
        player1_name: str,
        player1_id: Optional[int],
        player1_cards: List[Card],
        player2_name: str,
        player2_id: Optional[int],
        player2_cards: List[Card],
        format_name: str = "limited",
    ) -> GameState:
        """Create a game where players already have their cards (e.g., from draft).
        Unlike create_game(), this skips deck loading — cards are pre-built."""
        starting_life = FORMAT_STARTING_LIFE.get(format_name, 20)

        player1 = Player(
            name=player1_name,
            user_id=player1_id,
            is_claude=player1_id is None,
            life=starting_life,
        )
        player2 = Player(
            name=player2_name,
            user_id=player2_id,
            is_claude=player2_id is None,
            life=starting_life,
        )

        # Set owner indices and load cards directly into libraries
        for card in player1_cards:
            card.owner_index = 0
        player1.library = player1_cards
        player1.deck_name = "Draft Deck"
        random.shuffle(player1.library)

        for card in player2_cards:
            card.owner_index = 1
        player2.library = player2_cards
        player2.deck_name = "Draft Deck"
        random.shuffle(player2.library)

        game = GameState(
            thread_id=thread_id,
            format=format_name,
            players=[player1, player2],
            turn_number=0,
        )

        game._rules_engine = self.rules  # July 21: live wiring (was test-only)
        self.games[thread_id] = game
        self.save_game(game)
        return game

    async def _load_player_deck(self, player: Player, deck_data: Dict, owner_index: int = 0):
        """Load a deck into a player."""
        cards, name, commander, signature_spell = await self.deck_loader.load_from_json(deck_data)

        # Set owner index and color identity
        for card in cards:
            card.owner_index = owner_index
            card.color_identity = FormatValidator.get_color_identity(card)

        player.library = cards
        player.deck_name = name

        if commander:
            # Mark as commander and move to command zone
            commander.is_commander = True
            commander.color_identity = FormatValidator.get_color_identity(commander)
            player.library.remove(commander)
            player.command_zone.append(commander)

        # [PARTNER] Check for partner commander in deck JSON
        partner_name = deck_data.get("partner")
        if partner_name:
            partner_card = None
            for card in player.library:
                if card.name.lower() == partner_name.lower():
                    partner_card = card
                    break
            if not partner_card:
                # Partner not in cards list — fetch from Scryfall
                print(f"[PARTNER] Partner '{partner_name}' not in cards list, fetching from Scryfall...")
                scryfall_data = await self.deck_loader.fetch_card_data(partner_name)
                if scryfall_data and scryfall_data.get("type_line", "") != "":
                    partner_card = Card(
                        name=partner_name,
                        mana_cost=scryfall_data.get("mana_cost", ""),
                        type_line=scryfall_data.get("type_line", ""),
                        oracle_text=scryfall_data.get("oracle_text", ""),
                        power=scryfall_data.get("power"),
                        toughness=scryfall_data.get("toughness"),
                        loyalty=scryfall_data.get("loyalty"),
                        keywords=scryfall_data.get("keywords", []),
                    )
                    partner_card.owner_index = owner_index
                    partner_card.color_identity = FormatValidator.get_color_identity(partner_card)
                    player.library.append(partner_card)
                    print(f"[PARTNER] Created {partner_name} and added to deck")
                else:
                    print(f"[PARTNER] WARNING: Could not fetch '{partner_name}' from Scryfall!")
            if partner_card:
                partner_card.is_commander = True
                partner_card.color_identity = FormatValidator.get_color_identity(partner_card)
                if partner_card in player.library:
                    player.library.remove(partner_card)
                player.command_zone.append(partner_card)
                print(f"[PARTNER] {partner_card.name} placed in command zone as partner commander")

        # [COMPANION] Check for companion in deck JSON
        companion_name = deck_data.get("companion")
        if companion_name:
            companion_card = None
            for card in player.library:
                if card.name.lower() == companion_name.lower():
                    companion_card = card
                    break
            if not companion_card:
                # Companion not in cards list — fetch from Scryfall
                print(f"[COMPANION] Companion '{companion_name}' not in cards list, fetching from Scryfall...")
                scryfall_data = await self.deck_loader.fetch_card_data(companion_name)
                if scryfall_data and scryfall_data.get("type_line", "") != "":
                    companion_card = Card(
                        name=companion_name,
                        mana_cost=scryfall_data.get("mana_cost", ""),
                        type_line=scryfall_data.get("type_line", ""),
                        oracle_text=scryfall_data.get("oracle_text", ""),
                        power=scryfall_data.get("power"),
                        toughness=scryfall_data.get("toughness"),
                        loyalty=scryfall_data.get("loyalty"),
                        keywords=scryfall_data.get("keywords", []),
                    )
                    companion_card.owner_index = owner_index
                    companion_card.color_identity = FormatValidator.get_color_identity(companion_card)
                    print(f"[COMPANION] Created {companion_name} from Scryfall")
                else:
                    print(f"[COMPANION] WARNING: Could not fetch '{companion_name}' from Scryfall!")
            else:
                # Remove from library — companion starts outside the game
                player.library.remove(companion_card)
            if companion_card:
                companion_card.is_companion = True
                player.companion_zone.append(companion_card)
                print(f"[COMPANION] {companion_card.name} placed in companion zone")

        if signature_spell:
            # Mark as signature spell and move to command zone (oathbreaker)
            signature_spell.is_signature_spell = True
            signature_spell.color_identity = FormatValidator.get_color_identity(signature_spell)
            player.library.remove(signature_spell)
            player.command_zone.append(signature_spell)
            print(f"[OATHBREAKER] {signature_spell.name} placed in command zone as signature spell")

        random.shuffle(player.library)

    def start_game(self, game: GameState, first_player_index: int = 0):
        """Start the game - draw opening hands."""
        game.started = True
        game.turn_number = 1
        game.active_player_index = first_player_index
        game.priority_player_index = first_player_index
        game.phase = Phase.MAIN1
        
        # Draw opening hands
        for player in game.players:
            self.draw_cards(player, 7)
            player.has_drawn_for_turn = True  # Don't draw on first turn
        
        # [COMMANDER] Safety check: ensure each player has a commander in command zone
        if game.format in COMMAND_ZONE_FORMATS:
            for player in game.players:
                if not player.command_zone:
                    # Commander missing — search library for a legendary creature with is_commander flag
                    for card in player.library:
                        if getattr(card, 'is_commander', False):
                            player.library.remove(card)
                            player.command_zone.append(card)
                            print(f"[COMMANDER] Recovered {card.name} from library to command zone for {player.name}")
                            break
                    else:
                        print(f"[COMMANDER] WARNING: {player.name} has no commander in any zone!")

                # [OATHBREAKER] Safety check: recover signature spell if missing from command zone
                if game.format == "oathbreaker":
                    has_sig = any(getattr(c, 'is_signature_spell', False) for c in player.command_zone)
                    if not has_sig:
                        for card in player.library:
                            if getattr(card, 'is_signature_spell', False):
                                player.library.remove(card)
                                player.command_zone.append(card)
                                print(f"[OATHBREAKER] Recovered {card.name} from library to command zone for {player.name}")
                                break

        self.rules.log_event(f"Game started. {game.players[first_player_index].name} goes first.")
        self.save_game(game)  # Persist to disk
    
    def draw_cards(self, player: Player, count: int = 1, game: GameState = None) -> List[Card]:
        """Draw cards from library to hand. Each draw is a separate event for replacement effects."""
        drawn = []
        for _ in range(count):
            if not player.library:
                # July 21 (unexercised-paths suite): Laboratory Maniac / Jace,
                # Wielder of Mysteries REPLACE the empty draw with winning —
                # both print the same sentence. The engine had no win
                # replacement at all, so Jace WoM's whole reason to exist
                # lost its controller the game instead (CR 614.12).
                _win_src = None
                if game:
                    for _c in player.battlefield:
                        _ot = (_c.oracle_text or '').lower().replace('\n', ' ')
                        if (not getattr(_c, '_phased_out', False)
                                and 'library has no cards in it, you win the game' in _ot):
                            _win_src = _c
                            break
                if _win_src is not None:
                    game.ended = True
                    try:
                        game.winner = game.players.index(player)
                    except ValueError:
                        game.winner = None
                    print(f"[DRAW-EMPTY-WIN] {player.name} wins — {_win_src.name} "
                          f"replaces the draw from an empty library")
                    if not hasattr(game, '_pending_messages') or game._pending_messages is None:
                        game._pending_messages = []
                    game._pending_messages.append(
                        f"🏆 **{player.name}** wins the game! ({_win_src.name} "
                        f"replaces the draw from an empty library)")
                    break
                # CR 104.3c: A player who attempts to draw from an empty library loses the game
                # at the next state-based action check. Setting life=0 lets the
                # SBA pipeline handle it (which correctly treats simultaneous
                # losses as a draw via CR 104.3b — see sba.py player_loses path).
                # The previous code set game.game_over (wrong attr — SBA reads
                # game.ended) and bound game.winner to the opponent's name,
                # which let the loser keep playing through to a fake combat win.
                if game and player.name not in getattr(game, '_library_loss', set()):
                    print(f"[SBA] {player.name} attempted to draw from empty library — loses the game (CR 104.3c)")
                    if not hasattr(game, '_library_loss'):
                        game._library_loss = set()
                    game._library_loss.add(player.name)
                    # Keep the loss condition separate from life. Life gain
                    # later in the same resolving effect cannot undo an
                    # attempted draw from an empty library (CR 704.5b).
                    player.attempted_draw_from_empty = True
                    game.loss_reason = f"{player.name} drew from an empty library"
                break
            # [REPLACEMENT] Process draw replacement effects (Narset, Spirit of the Labyrinth)
            if game and HAS_REPLACEMENT_ENGINE and game._replacement_engine and game._replacement_engine.effects:
                event = GameEvent(
                    event_type=EventType.DRAW,
                    affected_player=player.name,
                    amount=1,
                )
                final = game._replacement_engine.process_event_sync(event)
                if final.is_prevented:
                    print(f"  [REPLACEMENT-APPLY] Draw prevented for {player.name} ({', '.join(final.replacement_chain)})")
                    continue
            card = player.library.pop(0)
            player.hand.append(card)
            drawn.append(card)

        # Check "whenever an opponent draws" triggers (Smothering Tithe, Consecrated Sphinx, etc.)
        # Messages are pushed to game._pending_messages so callers can flush them to Discord.
        if game and drawn:
            player_idx = game.players.index(player) if player in game.players else 0
            for opp_idx, opp in enumerate(game.players):
                if opp_idx == player_idx:
                    continue
                for bf_card in opp.battlefield:
                    oracle_lower = (bf_card.oracle_text or '').lower()
                    # Smothering Tithe: "Whenever an opponent draws a card, that player
                    # may pay {2}. If the player doesn't, you create a Treasure token."
                    if 'smothering tithe' in bf_card.name.lower() or (
                        'whenever an opponent draws' in oracle_lower and 'treasure' in oracle_lower
                    ):
                        # Route through the shared action interpreter so token
                        # replacement effects see the creation event.
                        before = len(opp.battlefield)
                        self.rules._execute_action_on_state(game, {
                            "action": "create_token", "player": opp.name,
                            "name": "Treasure", "count": len(drawn),
                            "types": "Token Artifact — Treasure",
                            "oracle_text": "{T}, Sacrifice this artifact: Add one mana of any color.",
                            "source": "Smothering Tithe",
                        })
                        created_count = len(opp.battlefield) - before
                        msg = f"💰 **Smothering Tithe** — {opp.name} creates {created_count} Treasure token(s)"
                        print(f"[DRAW-TRIGGER] {msg}")
                        if not hasattr(game, '_pending_messages'):
                            game._pending_messages = []
                        game._pending_messages.append(msg)

                    # Consecrated Sphinx: "Whenever an opponent draws a card, you may draw two cards."
                    elif 'consecrated sphinx' in bf_card.name.lower() or (
                        'whenever an opponent draws' in oracle_lower and 'draw two' in oracle_lower
                    ):
                        sphinx_count = len(drawn) * 2
                        sphinx_drawn = self.draw_cards(opp, sphinx_count, game=None)  # game=None prevents recursive triggers
                        if sphinx_drawn:
                            msg = f"🦋 **Consecrated Sphinx** — {opp.name} draws {len(sphinx_drawn)} card(s)"
                            print(f"[DRAW-TRIGGER] {msg}")
                            if not hasattr(game, '_pending_messages'):
                                game._pending_messages = []
                            game._pending_messages.append(msg)
        return drawn
    
    def play_land(self, game: GameState, player: Player, card: Card) -> Tuple[bool, str]:
        """Play a land from hand with rules enforcement."""
        # Check rules
        can_play, reason = self.rules.can_play_land(game, player)
        if not can_play:
            return False, reason
        
        if card not in player.hand:
            return False, "Card not in hand"
        if not card.is_land():
            return False, "Card is not a land"
        
        player.hand.remove(card)
        player.battlefield.append(card)
        card.entered_this_turn = True
        player.lands_played_this_turn += 1
        # Pub/sub slice 2: one PERMANENT_ENTERED per physical entry (snow
        # lands feed the Marit Lage's Slumber watcher).
        events.emit(events.PERMANENT_ENTERED, game, card=card,
                    controller=player, via="land_drop", rules=self.rules)

        # Handle land ETB-tapped conditions via consolidated utility
        enters_tapped, etb_msg = self.rules._check_enters_tapped(game, card, player)
        if enters_tapped:
            card.tapped = True

        self.rules.log_event(f"{player.name} plays {card.name}")
        return True, f"Played {card.name}{etb_msg}"
    
    def cast_spell(self, game: GameState, player: Player, card: Card, pay_mana: bool = True, target: Any = None) -> Tuple[bool, str, List[str]]:
        """Cast a spell from hand with rules enforcement.
        
        Returns:
            Tuple of (success, message, effect_messages)
        """
        # Check rules
        can_cast, reason = self.rules.can_cast_spell(game, player, card)
        if not can_cast:
            return False, reason, []
        
        if card not in player.hand:
            return False, "Card not in hand", []
        
        # Pay mana cost — color-aware tapping when mana engine is available
        if pay_mana and (card.mana_cost or card.cmc > 0):
            if HAS_MANA_ENGINE and card.mana_cost:
                tapped_ok = player.tap_sources_for_cost(card.mana_cost, game=game)
            else:
                tapped_ok = player.tap_lands_for_mana(card.cmc, game=game)
            if not tapped_ok:
                return False, f"Not enough mana to cast {card.name} (needs {card.cmc})", []

        player.hand.remove(card)
        effect_messages = []

        # July 24 (slice 4b groundwork): the legacy sync cast never ran cast
        # triggers (sync-gap class). No live caller is known, but if anything
        # ever routes here, the triggers queue for the Tier-3 drain and the
        # CARD_CAST parity ledger counts the cast (paired inside the helper).
        from mtg.triggers import queue_cast_triggers_sync
        queue_cast_triggers_sync(self, game, player, card, via="cast_sync")

        if card.is_instant() or card.is_sorcery():
            # Goes to graveyard after resolving (or command zone for signature spells)
            if getattr(card, 'is_signature_spell', False) and game.format == "oathbreaker":
                player.command_zone.append(card)
                print(f"[OATHBREAKER] {card.name} resolved → returns to command zone")
            else:
                player.graveyard.append(card)
            self.rules.log_event(f"{player.name} casts {card.name} (instant/sorcery)")

            # Store target for async resolution
            card._pending_target = target
        else:
            # Permanent goes to battlefield — reset stale state from previous zone
            card.reset_battlefield_state()
            card.summoning_sick = True
            card.entered_this_turn = True
            player.battlefield.append(card)
            self.rules.log_event(f"{player.name} casts {card.name} (permanent)")
            # Pub/sub slice 2: one PERMANENT_ENTERED per physical entry.
            events.emit(events.PERMANENT_ENTERED, game, card=card,
                        controller=player, via="cast_sync", rules=self.rules)

        return True, f"Cast {card.name}", effect_messages

    async def cast_spell_async(self, game: GameState, player: Player, card: Card, pay_mana: bool = True, target: Any = None, additional_cost: int = 0) -> Tuple[bool, str, List[str]]:
        """Delegates to mtg.spells.cast_spell_async (Phase 2G)."""
        from mtg.spells import cast_spell_async
        return await cast_spell_async(self, game, player, card, pay_mana, target, additional_cost)
    def _queue_async_trigger(self, game: GameState, source_card: Card, trigger_text: str,
                             trigger_type: str, controller_name: str, context: str = "") -> None:
        """Enqueue a sync-context trigger for async Tier 3 resolution.

        trigger_type is a short tag ("etb", "upkeep", "end_step", "dies", "ltb",
        "attack", "saga", "creature_enters", "trigger") used for dedup within a
        single drain cycle and for the [QUEUE-*] log tags.

        Dedup: within a single drain cycle, the same (source_card.id, trigger_type)
        pair won't be queued twice. Across drain cycles (e.g. next turn's upkeep)
        the same trigger fires again, which is correct MTG behavior.
        """
        if not hasattr(game, 'pending_async_triggers') or game.pending_async_triggers is None:
            game.pending_async_triggers = []
        # Dedup: same source+type already pending → skip
        src_id = getattr(source_card, 'id', None) or source_card.name
        for existing in game.pending_async_triggers:
            esrc = existing.get('source_card')
            eid = getattr(esrc, 'id', None) or (esrc.name if esrc else None)
            if eid == src_id and existing.get('trigger_type') == trigger_type:
                return  # already queued this cycle
        game.pending_async_triggers.append({
            'source_card': source_card,
            'trigger_text': trigger_text,
            'trigger_type': trigger_type,
            'controller_name': controller_name,
            'context': context,
        })
        print(f"[QUEUE-{trigger_type.upper()}] Queued {source_card.name} for async resolution")

    def queue_unhandled_dies(self, game: GameState, dead_card: Card,
                             dead_player: Player, unhandled) -> None:
        """Queue the unhandled half of a _check_dies_triggers_sync result for
        async Tier 3 drain.

        July 21 batch audit (R1-2/R1-3): the main SBA drain did this inline,
        but FIVE other call sites unpacked the unhandled list into `_` and
        dropped it — anything without a Tier 1/1.5 match (Judith, the
        Scourge Diva's dies-damage in game_1529154418816057364) vanished
        with no display and no Tier 3 fallback. Shared helper so every
        dies-scan site queues the tail identically.
        """
        for trigger_card, trigger_text in (unhandled or []):
            ctrl_player = dead_player
            for p in game.players:
                if trigger_card in p.battlefield:
                    ctrl_player = p
                    break
            self._queue_async_trigger(
                game, trigger_card, trigger_text, "dies",
                ctrl_player.name,
                context=f"{dead_card.name} just died (went to graveyard)",
            )

    async def drain_pending_triggers(self, game: GameState) -> List[str]:
        """Delegates to mtg.triggers.drain_pending_triggers (Phase 2E)."""
        from mtg.triggers import drain_pending_triggers
        return await drain_pending_triggers(self, game)
    def _check_creature_etb_triggers_sync(self, game: GameState, entering_player: Player, entering_creature: Card) -> Tuple[List[str], List[Tuple]]:
        """Delegates to mtg.triggers._check_creature_etb_triggers_sync (Phase 2E)."""
        from mtg.triggers import _check_creature_etb_triggers_sync
        return _check_creature_etb_triggers_sync(self, game, entering_player, entering_creature)
    def _spell_matches_cast_trigger(self, sentence_lower: str, card: Card,
                                     caster: Player = None, game: 'GameState' = None) -> bool:
        """Delegates to mtg.triggers._spell_matches_cast_trigger (Phase 2E)."""
        from mtg.triggers import _spell_matches_cast_trigger
        return _spell_matches_cast_trigger(self, sentence_lower, card, caster, game)
    def _flush_pending_messages(self, game: GameState, messages: list) -> None:
        """Drain game._pending_messages (e.g. draw triggers from draw_cards()) into messages.

        draw_cards() appends Smothering Tithe / Consecrated Sphinx announcements there
        rather than returning them, because its return type is List[Card] and changing
        the signature would break ~100 call sites.  Call this after any bulk draw
        operation before the surrounding function returns its message list to Discord.
        """
        pending = getattr(game, '_pending_messages', None)
        if pending:
            messages.extend(pending)
            game._pending_messages = []

    async def _check_cast_triggers(self, game: GameState, caster: Player, card: Card) -> List[str]:
        """Delegates to mtg.triggers._check_cast_triggers (Phase 2E)."""
        from mtg.triggers import _check_cast_triggers
        return await _check_cast_triggers(self, game, caster, card)
    async def _check_creature_etb_triggers(self, game: GameState, entering_player: Player, entering_creature: Card) -> List[str]:
        """Delegates to mtg.triggers._check_creature_etb_triggers (Phase 2E)."""
        from mtg.triggers import _check_creature_etb_triggers
        return await _check_creature_etb_triggers(self, game, entering_player, entering_creature)
    def _check_dies_triggers_sync(self, game: GameState, dying_card: Card, dying_player: Player) -> Tuple[List[str], List[Tuple]]:
        """Delegates to mtg.triggers._check_dies_triggers_sync (Phase 2E)."""
        from mtg.triggers import _check_dies_triggers_sync
        return _check_dies_triggers_sync(self, game, dying_card, dying_player)
    async def _check_dies_triggers(self, game: GameState, dying_card: Card, dying_player: Player) -> List[str]:
        """Async version: runs sync core + Tier 3 Claude API fallback for unhandled dies triggers."""
        messages, unhandled = self._check_dies_triggers_sync(game, dying_card, dying_player)
        player_idx = game.players.index(dying_player) if dying_player in game.players else 0
        opponent = game.players[1 - player_idx]

        for card, trigger_text in unhandled:
            if self.rules.client:
                print(f"[DIES-TRIGGER] Auto-resolving {card.name} trigger (creature died: {dying_card.name})")
                try:
                    resolve_msgs, actions = await self.rules.resolve_effect(
                        game,
                        effect_description=f"{trigger_text} (Triggered by {dying_card.name} dying)",
                        source_card=card.name,
                        controller=dying_player.name,
                        context=f"{dying_card.name} just died (went to graveyard)"
                    )
                    messages.extend(resolve_msgs)
                    if actions:
                        print(f"[DIES-TRIGGER] Executed {len(actions)} action(s) for {card.name}")
                except Exception as e:
                    print(f"[DIES-TRIGGER] Error resolving {card.name}: {e}")
                    # May 25 audit (F21): use format_trigger_line for oracle
                    # dedup. The hint suffix stays player-facing so they know
                    # how to manually resolve when the API errored.
                    from mtg.helpers import format_trigger_line
                    messages.append(
                        format_trigger_line(
                            "💀", card.name, trigger_text, game=game, max_chars=300,
                            suffix=f"\n  *(Use `!resolve {card.name} trigger, {dying_card.name} died` to resolve)*",
                        )
                    )
            else:
                from mtg.helpers import format_trigger_line
                messages.append(
                    format_trigger_line(
                        "💀", card.name, trigger_text, game=game, max_chars=300,
                        suffix="\n  *(Use `!resolve` or `!fix` to handle)*",
                    )
                )

        return messages

    # =========================================================================
    # LEAVES-THE-BATTLEFIELD (LTB) TRIGGER DETECTION
    # =========================================================================

    def _check_ltb_triggers_sync(self, game: GameState, leaving_card: Card, leaving_player: Player,
                                  destination: str = "graveyard") -> List[str]:
        """Delegates to mtg.triggers._check_ltb_triggers_sync (Phase 2E)."""
        from mtg.triggers import _check_ltb_triggers_sync
        return _check_ltb_triggers_sync(self, game, leaving_card, leaving_player, destination)
    def _track_exiled_by(self, game: GameState, source_card: Card, exiled_card_name: str, owner_idx: int):
        """Track that a card was exiled by a specific permanent (for LTB return triggers).

        Used by: Worldgorger Dragon, Oblivion Ring, Fiend Hunter, Banishing Light, etc.
        When the source permanent leaves the battlefield, _check_ltb_triggers_sync will
        find these tracked cards and return them.
        """
        exiled_by_key = f"_exiled_by_{source_card.id}"
        if not hasattr(game, exiled_by_key):
            setattr(game, exiled_by_key, [])
        getattr(game, exiled_by_key).append({
            'name': exiled_card_name,
            'owner': owner_idx,
        })
        print(f"[LTB-TRACK] {exiled_card_name} exiled by {source_card.name} (tracked for LTB return)")

    # =========================================================================
    # RESOLVE-PROMPT DEDUP
    # =========================================================================

    def _should_emit_resolve_prompt(self, game: GameState, source_name: str, reason: str) -> bool:
        """Dedup '!judge'/'!resolve' suggestion prompts. A given (source, reason)
        pair only fires once per turn — prevents the Beastmaster Ascension log spam
        seen in the Apr 19 audit (875 reps in one game). Returns False to suppress."""
        if not hasattr(game, '_resolve_prompt_seen_this_turn'):
            game._resolve_prompt_seen_this_turn = set()
            game._resolve_prompt_turn = game.turn_number
        if game._resolve_prompt_turn != game.turn_number:
            game._resolve_prompt_seen_this_turn = set()
            game._resolve_prompt_turn = game.turn_number
        key = (source_name.lower(), reason[:80].lower())
        if key in game._resolve_prompt_seen_this_turn:
            return False
        game._resolve_prompt_seen_this_turn.add(key)
        return True

    # =========================================================================
    # ATTACK TRIGGER DETECTION
    # =========================================================================

    def _check_attack_triggers_sync(self, game: GameState, attacker_card: Card, attacking_player: Player) -> Tuple[List[str], List[Tuple]]:
        """Delegates to mtg.triggers._check_attack_triggers_sync (Phase 2E)."""
        from mtg.triggers import _check_attack_triggers_sync
        return _check_attack_triggers_sync(self, game, attacker_card, attacking_player)
    async def _check_attack_triggers(self, game: GameState, attacker_card: Card, attacking_player: Player) -> List[str]:
        """Async version: runs sync core + Tier 3 Claude API fallback for unhandled attack triggers."""
        messages, unhandled = self._check_attack_triggers_sync(game, attacker_card, attacking_player)

        for card, trigger_text in unhandled:
            if self.rules.client:
                print(f"[ATTACK-TRIGGER] Auto-resolving {card.name} trigger ({attacker_card.name} attacks)")
                try:
                    resolve_msgs, actions = await self.rules.resolve_effect(
                        game,
                        effect_description=f"{trigger_text} (Triggered by {attacker_card.name} attacking)",
                        source_card=card.name,
                        controller=attacking_player.name,
                        context=f"{attacker_card.name} was declared as an attacker"
                    )
                    messages.extend(resolve_msgs)
                except Exception as e:
                    print(f"[ATTACK-TRIGGER] Error resolving {card.name}: {e}")
                    messages.append(
                        f"⚔️ **{card.name}** triggers: *{trigger_text[:300]}*\n"
                        f"  *(Use `!resolve {card.name} trigger` to resolve)*"
                    )
            else:
                messages.append(
                    f"⚔️ **{card.name}** triggers: *{trigger_text[:300]}*\n"
                    f"  *(Use `!resolve` or `!fix` to handle)*"
                )

        return messages

    # =========================================================================
    # TRANSFORM / DAY-NIGHT / WEREWOLF
    # =========================================================================

    def _check_day_night_and_werewolf_transforms(self, game: GameState) -> List[str]:
        """Delegates to mtg.triggers._check_day_night_and_werewolf_transforms (Phase 2E)."""
        from mtg.triggers import _check_day_night_and_werewolf_transforms
        return _check_day_night_and_werewolf_transforms(self, game)
    def _activate_day_night_if_needed(self, game: GameState, card: Card) -> List[str]:
        """Activate day/night when a daybound/nightbound card enters the battlefield."""
        messages = []
        if game.day_night_active:
            return messages
        oracle_lower = (card.oracle_text or '').lower()
        back_oracle_lower = (card.back_face_oracle_text or '').lower()
        if ('daybound' in oracle_lower or 'nightbound' in oracle_lower
                or 'daybound' in back_oracle_lower or 'nightbound' in back_oracle_lower):
            game.day_night_active = True
            game.is_day = True
            messages.append("☀️ **Day/Night cycle activated!** It is currently day.")
            print(f"[TRANSFORM] Day/Night activated by {card.name}")
        return messages

    # =========================================================================
    # UPKEEP TRIGGER DETECTION
    # =========================================================================

    def _check_upkeep_triggers_sync(self, game: GameState) -> Tuple[List[str], List[Tuple]]:
        """Delegates to mtg.triggers._check_upkeep_triggers_sync (Phase 2E)."""
        from mtg.triggers import _check_upkeep_triggers_sync
        return _check_upkeep_triggers_sync(self, game)
    async def _check_upkeep_triggers(self, game: GameState) -> List[str]:
        """Async version: runs sync core + Tier 3 Claude API fallback for unhandled upkeep triggers."""
        messages, unhandled = self._check_upkeep_triggers_sync(game)
        active = game.active_player

        for card, trigger_text in unhandled:
            if self.rules.client:
                print(f"[UPKEEP-TRIGGER] Auto-resolving {card.name} upkeep trigger")
                try:
                    ctrl_name = card.controller if hasattr(card, 'controller') and card.controller else active.name
                    resolve_msgs, actions = await self.rules.resolve_effect(
                        game,
                        effect_description=f"{trigger_text} (Triggered at beginning of upkeep)",
                        source_card=card.name,
                        controller=ctrl_name,
                        context=f"It is {active.name}'s upkeep"
                    )
                    messages.extend(resolve_msgs)
                except Exception as e:
                    print(f"[UPKEEP-TRIGGER] Error resolving {card.name}: {e}")
                    from mtg.helpers import format_trigger_line
                    messages.append(
                        format_trigger_line(
                            "📍", card.name, trigger_text, game=game, max_chars=300,
                            suffix=f"\n  *(Use `!resolve {card.name} upkeep trigger` to resolve)*",
                        )
                    )
            else:
                from mtg.helpers import format_trigger_line
                messages.append(
                    format_trigger_line(
                        "📍", card.name, trigger_text, game=game, max_chars=300,
                        suffix="\n  *(Use `!resolve` or `!fix` to handle)*",
                    )
                )

        return messages

    # =========================================================================
    # END STEP TRIGGER DETECTION
    # =========================================================================

    def _check_main_phase_triggers_sync(self, game: GameState, precombat: bool):
        """Delegates to mtg.triggers._check_main_phase_triggers_sync (July 27)."""
        from mtg.triggers import _check_main_phase_triggers_sync
        return _check_main_phase_triggers_sync(self, game, precombat)

    def _check_beginning_combat_triggers_sync(self, game: GameState) -> Tuple[List[str], List[Tuple]]:
        """Delegates to mtg.triggers._check_beginning_combat_triggers_sync (May 30 audit)."""
        from mtg.triggers import _check_beginning_combat_triggers_sync
        return _check_beginning_combat_triggers_sync(self, game)

    def _check_end_step_triggers_sync(self, game: GameState) -> Tuple[List[str], List[Tuple]]:
        """Delegates to mtg.triggers._check_end_step_triggers_sync (Phase 2E)."""
        from mtg.triggers import _check_end_step_triggers_sync
        return _check_end_step_triggers_sync(self, game)
    async def _check_end_step_triggers(self, game: GameState) -> List[str]:
        """Async version: runs sync core + Tier 3 Claude API fallback."""
        messages, unhandled = self._check_end_step_triggers_sync(game)
        active = game.active_player

        for card, trigger_text in unhandled:
            if self.rules.client:
                print(f"[ENDSTEP-TRIGGER] Auto-resolving {card.name} end step trigger")
                try:
                    resolve_msgs, actions = await self.rules.resolve_effect(
                        game,
                        effect_description=f"{trigger_text} (Triggered at beginning of end step)",
                        source_card=card.name,
                        controller=active.name,
                        context=f"It is the end step of {active.name}'s turn"
                    )
                    messages.extend(resolve_msgs)
                except Exception as e:
                    print(f"[ENDSTEP-TRIGGER] Error resolving {card.name}: {e}")
                    from mtg.helpers import format_trigger_line
                    messages.append(
                        format_trigger_line(
                            "📍", card.name, trigger_text, game=game, max_chars=300,
                            suffix=f"\n  *(Use `!resolve {card.name} end step trigger` to resolve)*",
                        )
                    )
            else:
                from mtg.helpers import format_trigger_line
                messages.append(
                    format_trigger_line(
                        "📍", card.name, trigger_text, game=game, max_chars=300,
                        suffix="\n  *(Use `!resolve` or `!fix` to handle)*",
                    )
                )

        return messages

    # =========================================================================
    # TRIGGER → STACK PLACEMENT (APNAP ordering)
    # =========================================================================

    def _place_triggers_on_stack(self, game: GameState, trigger_infos: List[Tuple],
                                 trigger_event: str = "unknown") -> List[str]:
        """Delegates to mtg.triggers._place_triggers_on_stack (Phase 2E)."""
        from mtg.triggers import _place_triggers_on_stack
        return _place_triggers_on_stack(self, game, trigger_infos, trigger_event)
    def _handle_etb_triggers(self, game: GameState, player: Player, card: Card) -> List[str]:
        """Delegates to mtg.triggers._handle_etb_triggers (Phase 2E)."""
        from mtg.triggers import _handle_etb_triggers
        return _handle_etb_triggers(self, game, player, card)
    def _handle_land_etb(self, game: GameState, player: Player, card: Card) -> List[str]:
        """Delegates to mtg.triggers._handle_land_etb (Phase 2E)."""
        from mtg.triggers import _handle_land_etb
        return _handle_land_etb(self, game, player, card)
    def _is_colorless_card(self, card: Card) -> bool:
        """Check if a card is colorless (no color identity or colors)."""
        # Check colors field first
        if hasattr(card, 'colors') and card.colors:
            return len(card.colors) == 0
        # Fall back to mana cost - if it only contains generic/colorless mana, it's colorless
        if card.mana_cost:
            colored = re.findall(r'\{[WUBRG]\}', card.mana_cost.upper())
            return len(colored) == 0
        # No mana cost (like lands) - check type
        if card.is_land():
            return True
        return True  # Default to colorless if we can't determine

    def _serialize_for_xmage(self, game: GameState) -> Tuple['XMageGameState', Dict[str, str]]:
        """Delegates to mtg.spells._serialize_for_xmage (Phase 2G)."""
        from mtg.spells import _serialize_for_xmage
        return _serialize_for_xmage(self, game)
    def resolve_special_effects(self, game: GameState, player: Player, card: Card, target: Any = None) -> List[str]:
        """Delegates to mtg.spells.resolve_special_effects (Phase 2G)."""
        from mtg.spells import resolve_special_effects
        return resolve_special_effects(self, game, player, card, target)
    def tap_permanent(self, card: Card) -> bool:
        """Tap a permanent."""
        if card.tapped:
            return False
        card.tapped = True
        return True
    
    def untap_permanent(self, card: Card) -> bool:
        """Untap a permanent."""
        if not card.tapped:
            return False
        card.tapped = False
        return True
    
    def untap_all(self, player: Player):
        """Untap all permanents for a player and clear summoning sickness."""
        for card in player.battlefield:
            was_tapped = card.tapped
            # `_skip_next_untap` is set by Icebreaker Kraken / Frozen Aether-
            # style effects ("don't untap during that player's next untap
            # step"). Skip the untap once and clear the flag so this permanent
            # untaps normally next turn.
            if getattr(card, '_skip_next_untap', False):
                card._skip_next_untap = False
                print(f"[UNTAP] {card.name} skips this untap (don't-untap-next-step flag)")
            else:
                card.tapped = False
            # Clear summoning sickness for creatures (only creatures have it)
            if card.summoning_sick:
                card.summoning_sick = False
            # Clear "entered this turn" flag
            card.entered_this_turn = False
            # Clear stale combat flags (e.g. from crashed/resumed games)
            if card.attacking:
                print(f"[UNTAP] Clearing stale attacking flag on {card.name}")
                card.attacking = False
                card.attacking_player = None
            if card.blocking:
                print(f"[UNTAP] Clearing stale blocking flag on {card.name}")
                card.blocking = []
            if card.blocked_by:
                card.blocked_by = []
    
    def clear_end_of_turn_effects(self, game: GameState):
        """Clear all end-of-turn effects (pump spells, temp keywords, etc.)."""
        # Clear turn effects
        game.turn_effects = []

        # Clear stale pending resolves (if AI didn't resolve them this turn, they're gone)
        if game.pending_resolves:
            print(f"[CLEANUP] Clearing {len(game.pending_resolves)} stale pending resolves")
            game.pending_resolves = []

        # Clear stale stack items — lingering items block sorcery-speed spells next turn
        if game.stack:
            print(f"[CLEANUP] Clearing {len(game.stack)} stale stack items at end of turn")
            game.stack.clear()

        # Clear death watchers (Searing Blood, etc.) — only valid for the turn registered
        if hasattr(game, '_death_watchers'):
            game._death_watchers = []

        # Clear per-turn activation counters (Sensei's Divining Top loop prevention)
        if hasattr(game, '_activation_counts'):
            game._activation_counts = {}

        # [LAYERS] Clear end-of-turn temporary effects from layers engine
        if HAS_LAYERS_ENGINE and game.layers_engine:
            game.layers_engine.clear_temporary_effects("end_of_turn")

        # Clear temporary modifiers and keywords from all creatures
        for player in game.players:
            for card in player.battlefield:
                card.power_modifier = 0
                card.toughness_modifier = 0
                card.temp_keywords = []
            # Clear playable from exile
            player.playable_from_exile = []
            # Clear playable from graveyard (Snapcaster flashback)
            if hasattr(player, 'playable_from_graveyard'):
                player.playable_from_graveyard = []
    
    def process_attack_triggers(self, game: GameState, attacking_player_idx: int) -> List[str]:
        """Delegates to mtg.triggers.process_attack_triggers (Phase 2E)."""
        from mtg.triggers import process_attack_triggers
        return process_attack_triggers(self, game, attacking_player_idx)
    def deal_damage(self, target_player: Player, amount: int, source: Optional[Card] = None, is_commander: bool = False, game: GameState = None):
        """Deal damage to a player."""
        source_name = source.name if source else ""
        source_id = source.id if source else ""
        if game:
            actual = self.rules._apply_noncombat_damage_to_player(game, target_player, amount, source_name, source_id)
        else:
            target_player.life -= amount
            target_player.record_life_loss(amount)
            actual = amount

        if is_commander and source and actual > 0:
            # Track commander damage
            source_owner = source.owner_index
            if source_owner not in target_player.commander_damage:
                target_player.commander_damage[source_owner] = 0
            target_player.commander_damage[source_owner] += actual
    
    def check_state_based_actions(self, game: GameState) -> List[str]:
        """Check for state-based actions using rules engine.

        After SBA processing, fires dies triggers for any creatures that died.
        Dies triggers are sync-only (Tiers 1/1.5) — async callers should use
        _check_dies_triggers() for Tier 3 fallback.
        """
        messages = self.rules.process_state_based_actions(game)

        # [DIES-TRIGGER] Fire dies triggers for creatures that died during SBAs
        recently_died = list(getattr(game, '_recently_died', []))
        if recently_died and not game.ended:
            # June 11 audit: freeze one simultaneous-death wave before
            # resolving it. Trigger actions may cause more deaths; those form
            # a later wave and must not be visible to sources that died here.
            game._recently_died = []
            game._active_dies_batch = recently_died
            if game.triggers_use_stack and game.stack_enabled:
                # Stack mode: collect all triggers and place on stack via APNAP ordering
                all_trigger_infos = []
                burst_msgs: List[str] = []
                for dead_card, dead_player in recently_died:
                    try:
                        trigger_msgs, unhandled = self._check_dies_triggers_sync(game, dead_card, dead_player)
                        # In stack mode, we DON'T execute the resolved triggers immediately
                        # Instead, collect them for stack placement
                        # (The sync detection already hardcodes Blood Artist etc. — those still resolve
                        #  immediately in stack mode for simplicity. Stack mode mainly helps with ordering
                        #  and giving opponents a chance to respond with Stifle.)
                        burst_msgs.extend(trigger_msgs)
                        for trigger_card, trigger_text in unhandled:
                            # Find the controller of the trigger card
                            ctrl_player = dead_player
                            for p in game.players:
                                if trigger_card in p.battlefield:
                                    ctrl_player = p
                                    break
                            all_trigger_infos.append((trigger_card, ctrl_player, trigger_text))
                    except Exception as e:
                        print(f"[DIES-TRIGGER] Error processing dies triggers for {dead_card.name}: {e}")
                # Collapse same-source bursts before extending messages.
                from mtg.triggers import collapse_trigger_burst
                messages.extend(collapse_trigger_burst(burst_msgs))
                # Place unhandled triggers on the stack with APNAP ordering
                if all_trigger_infos:
                    stack_msgs = self._place_triggers_on_stack(game, all_trigger_infos, "dies")
                    messages.extend(stack_msgs)
            else:
                # Immediate mode (default): resolve triggers right away.
                # May 14 audit (D3/D4): collect all per-creature trigger msgs
                # then collapse same-source consecutive runs before extending
                # `messages`. Without this, Athreos firing 9 times from a
                # 9-creature board wipe produces 9 sequential lines that
                # spam Discord even after the byte-identical D8 dedup runs
                # (the per-creature variation in life totals defeats
                # byte-level dedup; source-level collapse is the right layer).
                #
                # May 20 audit (APNAP-1): sort dying creatures so triggers
                # from the active player's permanents are scanned LAST in
                # immediate mode. CR 603.3b: AP places his triggers on the
                # stack first (bottom), NAP places hers second (top), LIFO
                # resolution → NAP's resolve first. Previously this iterated
                # in SBA insertion order (player 0 first), inverting LIFO.
                # game_1506618543322693684:861-863 showed Syr Konrad (AP)
                # firing before Meren (NAP) — should have been reverse.
                # June 10: ordering extracted to helpers.apnap_order_died so
                # the CR 603.3b NAP-first rule is unit-tested (the matrix has
                # never reached a both-sides wipe with both-sides triggers).
                from mtg.helpers import apnap_order_died
                recently_died = apnap_order_died(recently_died, game)
                burst_msgs: List[str] = []
                for dead_card, dead_player in recently_died:
                    try:
                        trigger_msgs, unhandled = self._check_dies_triggers_sync(game, dead_card, dead_player)
                        burst_msgs.extend(trigger_msgs)
                        # Sync context: queue unhandled dies triggers for async Tier 3 drain.
                        for trigger_card, trigger_text in unhandled:
                            ctrl_player = dead_player
                            for p in game.players:
                                if trigger_card in p.battlefield:
                                    ctrl_player = p
                                    break
                            self._queue_async_trigger(
                                game, trigger_card, trigger_text, "dies",
                                ctrl_player.name,
                                context=f"{dead_card.name} just died (went to graveyard)",
                            )
                            # June 11 audit: when Tier 3 is available, its
                            # drain emits the resolution line. The old queue
                            # placeholder announced Judith once here and once
                            # again at resolution (game 1514621840440561704).
                            # Keep the oracle hint only for no-client games,
                            # matching upkeep/end-step placeholder policy.
                            if not self.rules.client:
                                from mtg.helpers import format_trigger_line
                                burst_msgs.append(
                                    format_trigger_line(
                                        "💀", trigger_card.name, trigger_text,
                                        game=game, max_chars=300,
                                    )
                                )
                            else:
                                print(f"[DIES-PLACEHOLDER-SUPPRESSED] "
                                      f"{trigger_card.name}: queued for Tier 3 drain")
                    except Exception as e:
                        print(f"[DIES-TRIGGER] Error processing dies triggers for {dead_card.name}: {e}")
                # Collapse N consecutive same-source trigger messages from the
                # burst (Athreos × 9 → one line with "×9 fires" suffix).
                from mtg.triggers import collapse_trigger_burst
                messages.extend(collapse_trigger_burst(burst_msgs))
            game._active_dies_batch = []
            if game._recently_died and not game.ended:
                messages.extend(self.check_state_based_actions(game))

        return messages
    
    def advance_phase(self, game: GameState) -> Tuple[Phase, List[str]]:
        """Advance to the next phase. Returns (new_phase, messages)."""
        messages = []
        current_idx = PHASE_ORDER.index(game.phase)
        
        if current_idx >= len(PHASE_ORDER) - 1:
            # End of turn, go to next turn
            self.end_turn(game)
            return game.phase, messages
        
        old_phase = game.phase
        game.phase = PHASE_ORDER[current_idx + 1]
        self.rules.on_phase_change(game, game.phase)
        
        # Phase-specific actions
        if game.phase == Phase.UNTAP:
            self.rules.on_untap_step(game)
            messages.append(f"⏫ **Untap Step** - {game.active_player.name}'s permanents untap")
        
        elif game.phase == Phase.UPKEEP:
            messages.append(f"📍 **Upkeep**")
            # [TRANSFORM] Day/night transition and werewolf transform checks
            try:
                transform_msgs = self._check_day_night_and_werewolf_transforms(game)
                messages.extend(transform_msgs)
            except Exception as e:
                print(f"[TRANSFORM] Error in day/night check: {e}")
            # [DELAYED-TRIGGER] Fire delayed triggers scheduled for upkeep
            delayed_msgs = self._process_delayed_triggers(game, "upkeep")
            messages.extend(delayed_msgs)
            # [SOLITARY-CONFINEMENT] Upkeep sacrifice unless discard a card
            for perm in list(game.active_player.battlefield):
                if perm.name == "Solitary Confinement":
                    if game.active_player.hand:
                        # Auto-discard highest CMC card to keep it
                        discard = max(game.active_player.hand, key=lambda c: c.cmc or 0)
                        game.active_player.hand.remove(discard)
                        game.active_player.graveyard.append(discard)
                        messages.append(f"🛡️ {game.active_player.name} discards {discard.name} to keep Solitary Confinement")
                        # Set damage prevention + shroud flags (re-applied each upkeep)
                        game.active_player._damage_prevented = True
                        game.active_player._damage_prevented_expires_turn = game.turn_number + len(game.players)
                        game.active_player._skip_draw = True
                        print(f"[SOLITARY-CONFINEMENT] {game.active_player.name} keeps Solitary Confinement (discarded {discard.name})")
                    else:
                        # No cards to discard — sacrifice
                        game.unregister_static_effects(perm)
                        game.active_player.battlefield.remove(perm)
                        game.active_player.graveyard.append(perm)
                        game.active_player._damage_prevented = False
                        game.active_player._skip_draw = False
                        messages.append(f"💀 {game.active_player.name} sacrifices Solitary Confinement (no cards to discard)")
                        print(f"[SOLITARY-CONFINEMENT] Sacrificed — no cards in hand")
            # Process suspend - remove time counters from suspended cards
            suspend_messages = self._process_suspend_upkeep(game)
            messages.extend(suspend_messages)
            # [UPKEEP-TRIGGER] Fire upkeep triggers (Phyrexian Arena, Bitterblossom, etc.)
            try:
                upkeep_msgs, upkeep_unhandled = self._check_upkeep_triggers_sync(game)
                messages.extend(upkeep_msgs)
                if game.triggers_use_stack and game.stack_enabled and upkeep_unhandled:
                    # Stack mode: place unhandled upkeep triggers on stack with APNAP ordering
                    trigger_infos = []
                    active = game.active_player
                    for trigger_card, trigger_text in upkeep_unhandled:
                        ctrl_player = active
                        for p in game.players:
                            if trigger_card in p.battlefield:
                                ctrl_player = p
                                break
                        trigger_infos.append((trigger_card, ctrl_player, trigger_text))
                    stack_msgs = self._place_triggers_on_stack(game, trigger_infos, "upkeep")
                    messages.extend(stack_msgs)
                else:
                    # Sync context: queue unhandled triggers for async Tier 3 drain.
                    active = game.active_player
                    for trigger_card, trigger_text in upkeep_unhandled:
                        ctrl_player = active
                        for p in game.players:
                            if trigger_card in p.battlefield:
                                ctrl_player = p
                                break
                        self._queue_async_trigger(
                            game, trigger_card, trigger_text, "upkeep",
                            ctrl_player.name, context=f"Triggered at beginning of {active.name}'s upkeep",
                        )
                        # May 25 audit (F7): suppress 📍 placeholder when async
                        # Tier 3 will emit a ⚡ resolution line — was producing
                        # double emits (placeholder oracle + resolution prose)
                        # for the same trigger. If there's no AI client, keep
                        # the placeholder as a "triggered but unresolved" hint.
                        if not self.rules.client:
                            from mtg.helpers import format_trigger_line
                            messages.append(
                                format_trigger_line(
                                    "📍", trigger_card.name, trigger_text,
                                    game=game, max_chars=300,
                                )
                            )
                        else:
                            print(f"[UPKEEP-PLACEHOLDER-SUPPRESSED] {trigger_card.name}: queued for Tier 3 drain")
            except Exception as e:
                print(f"[UPKEEP-TRIGGER] Error processing upkeep triggers: {e}")
        
        elif game.phase == Phase.DRAW:
            skip_draw = getattr(game.active_player, '_skip_draw', False)
            if skip_draw:
                messages.append(f"🎴 **Draw Step** - {game.active_player.name} skips draw (Solitary Confinement)")
                print(f"[SOLITARY-CONFINEMENT] {game.active_player.name} skips draw step")
            elif game.turn_number > 1:  # First player skips draw on turn 1
                drawn = self.draw_cards(game.active_player, 1, game=game)
                if drawn:
                    messages.append(f"🎴 **Draw Step** - {game.active_player.name} draws a card")
                # Flush draw-trigger side-channel (Smothering Tithe, Consecrated Sphinx)
                self._flush_pending_messages(game, messages)
            # [SAGA] Add lore counters to sagas after draw step (CR 714.3a)
            try:
                saga_msgs = self._advance_sagas(game, game.active_player)
                messages.extend(saga_msgs)
            except Exception as e:
                print(f"[SAGA] Error advancing sagas: {e}")
        
        elif game.phase == Phase.MAIN1:
            messages.append(f"1️⃣ **Main Phase 1**")
            # Mana Drain-style "your next main phase" triggers fire after
            # on_phase_change has emptied old mana, so the new mana remains
            # available throughout this phase.
            messages.extend(self._process_delayed_triggers(game, "main_phase"))
            # July 27: "At the beginning of your precombat main phase" — the
            # battlefield scan the delayed-trigger drain above is NOT.
            _mp_msgs, _mp_unhandled = self._check_main_phase_triggers_sync(game, True)
            messages.extend(_mp_msgs)
            for _c, _t in _mp_unhandled:
                self._queue_async_trigger(game, _c, _t, "main_phase",
                                          game.active_player.name)
        
        elif game.phase == Phase.COMBAT_BEGIN:
            messages.append(f"⚔️ **Beginning of Combat**")
            # May 30 audit: dispatch "at the beginning of combat on your turn"
            # triggers (Luminarch Aspirant +1/+1 counter, Goblin Rabblemaster
            # token, Hero of Bladehold, etc.). Previously UNWIRED — this branch
            # only printed the display string, so the entire trigger class never
            # fired. Templated triggers resolve here synchronously; the rest queue
            # for async Tier 3 (drain_pending_triggers), like other sync paths.
            try:
                _bc_msgs, _bc_unhandled = self._check_beginning_combat_triggers_sync(game)
                messages.extend(_bc_msgs)
                for _bc_card, _bc_text in _bc_unhandled:
                    self._queue_async_trigger(
                        game, _bc_card, _bc_text, "beginning_combat",
                        game.active_player.name, "beginning of combat on your turn")
            except Exception as _bc_err:
                print(f"[COMBAT-BEGIN] Error dispatching beginning-of-combat triggers: {_bc_err}")

        elif game.phase == Phase.DECLARE_ATTACKERS:
            messages.append(f"🗡️ **Declare Attackers Step**")
        
        elif game.phase == Phase.DECLARE_BLOCKERS:
            messages.append(f"🛡️ **Declare Blockers Step**")
        
        elif game.phase == Phase.COMBAT_DAMAGE:
            messages.append(f"💥 **Combat Damage Step**")
            # NOTE: Combat damage is resolved by execute_claude_turn or player attack flow
            # Do NOT resolve here to avoid double damage
        
        elif game.phase == Phase.COMBAT_END:
            messages.append(f"🏁 **End of Combat**")
            # Clear combat state
            for player in game.players:
                for creature in player.creatures():
                    creature.attacking = False
                    creature.attacking_player = None
                    creature.blocking = []
                    creature.blocked_by = []
            game.attackers = []
            game.blockers = {}
        
        elif game.phase == Phase.MAIN2:
            messages.append(f"2️⃣ **Main Phase 2**")
            messages.extend(self._process_delayed_triggers(game, "main_phase"))
            # July 27: "At the beginning of your postcombat main phase" —
            # Tymna the Weaver et al. The delayed-trigger drain above is
            # one-shot scheduling (Necropotence), NOT a battlefield scan.
            _mp_msgs, _mp_unhandled = self._check_main_phase_triggers_sync(game, False)
            messages.extend(_mp_msgs)
            for _c, _t in _mp_unhandled:
                self._queue_async_trigger(game, _c, _t, "main_phase",
                                          game.active_player.name)
        
        elif game.phase == Phase.END:
            messages.append(f"📍 **End Step**")
            # [DELAYED-TRIGGER] Fire delayed triggers scheduled for end step
            delayed_msgs = self._process_delayed_triggers(game, "end_step")
            messages.extend(delayed_msgs)
            # [ENDSTEP-TRIGGER] Fire end step triggers (sacrifice effects, etc.)
            try:
                endstep_msgs, endstep_unhandled = self._check_end_step_triggers_sync(game)
                messages.extend(endstep_msgs)
                if game.triggers_use_stack and game.stack_enabled and endstep_unhandled:
                    # Stack mode: place unhandled end step triggers on stack with APNAP ordering
                    trigger_infos = []
                    active = game.active_player
                    for trigger_card, trigger_text in endstep_unhandled:
                        ctrl_player = active
                        for p in game.players:
                            if trigger_card in p.battlefield:
                                ctrl_player = p
                                break
                        trigger_infos.append((trigger_card, ctrl_player, trigger_text))
                    stack_msgs = self._place_triggers_on_stack(game, trigger_infos, "end_step")
                    messages.extend(stack_msgs)
                else:
                    # Sync context: queue unhandled triggers for async Tier 3 drain.
                    active = game.active_player
                    for trigger_card, trigger_text in endstep_unhandled:
                        ctrl_player = active
                        for p in game.players:
                            if trigger_card in p.battlefield:
                                ctrl_player = p
                                break
                        self._queue_async_trigger(
                            game, trigger_card, trigger_text, "end_step",
                            ctrl_player.name, context=f"Triggered at beginning of {active.name}'s end step",
                        )
                        # May 25 audit (F7): suppress 📍 placeholder when async
                        # Tier 3 will emit a ⚡ resolution line. Sire of Insanity
                        # was emitting both the placeholder oracle AND the
                        # Tier 3 prose for every end step.
                        if not self.rules.client:
                            from mtg.helpers import format_trigger_line
                            messages.append(
                                format_trigger_line(
                                    "📍", trigger_card.name, trigger_text,
                                    game=game, max_chars=300,
                                )
                            )
                        else:
                            print(f"[ENDSTEP-PLACEHOLDER-SUPPRESSED] {trigger_card.name}: queued for Tier 3 drain")
            except Exception as e:
                print(f"[ENDSTEP-TRIGGER] Error processing end step triggers: {e}")

        elif game.phase == Phase.CLEANUP:
            self.rules.on_end_step(game)
            # Continue to next turn
            return self.advance_phase(game)

        # [SBA] Check state-based actions at each phase transition (CR 704.3)
        # Players at 0 or less life die immediately, not on their next turn
        if game.phase not in (Phase.UNTAP, Phase.CLEANUP):
            try:
                game._recently_died = getattr(game, '_recently_died', [])
                sba_msgs = self.rules.process_state_based_actions(game)
                messages.extend(sba_msgs)
                # Fire dies triggers for creatures that died during phase-transition SBAs
                recently_died = list(getattr(game, '_recently_died', []))
                if recently_died and not game.ended:
                    game._recently_died = []
                    game._active_dies_batch = recently_died
                    # May 30 audit (F-LD2): apply the same APNAP sort the SBA drain
                    # uses (engine.py ~1774) so multi-controller dies-triggers
                    # resolve NAP-first per CR 603.3b LIFO. This drain previously
                    # iterated in raw insertion order (player 0 first), which can
                    # invert the ordering when a board wipe's deaths are first
                    # drained at a phase transition rather than by check_state_based_actions.
                    # June 10: shared CR 603.3b ordering helper (see above).
                    from mtg.helpers import apnap_order_died
                    recently_died = apnap_order_died(recently_died, game)
                    phase_burst: List[str] = []
                    for dead_card, dead_player in recently_died:
                        try:
                            trigger_msgs, _unh = self._check_dies_triggers_sync(game, dead_card, dead_player)
                            phase_burst.extend(trigger_msgs)
                            self.queue_unhandled_dies(game, dead_card, dead_player, _unh)
                        except Exception as e2:
                            print(f"[DIES-TRIGGER] Error in phase-transition dies trigger: {e2}")
                    # Collapse same-source bursts before extending messages.
                    from mtg.triggers import collapse_trigger_burst
                    messages.extend(collapse_trigger_burst(phase_burst))
                    game._active_dies_batch = []
                    if game._recently_died and not game.ended:
                        messages.extend(self.check_state_based_actions(game))
            except Exception as e:
                print(f"[SBA] Error in phase-transition SBA check: {e}")

        return game.phase, messages
    
    def _advance_sagas(self, game: GameState, player: Player) -> List[str]:
        """Delegates to mtg.spells._advance_sagas (Phase 2G)."""
        from mtg.spells import _advance_sagas
        return _advance_sagas(self, game, player)
    def _get_saga_chapter_text(self, card: Card, chapter_num: int) -> str:
        """Extract the text for a specific chapter from a saga's oracle text."""
        if not card.oracle_text:
            return ""
        import re
        # Saga oracle text uses Roman numerals: "I —", "II —", "III —", "IV —"
        roman = {1: 'I', 2: 'II', 3: 'III', 4: 'IV', 5: 'V'}
        target_roman = roman.get(chapter_num, str(chapter_num))
        # Match "I, II — text" or "III — text" patterns
        # Handle combined chapters like "I, II —" (both chapters have same text)
        #
        # May 20 audit: previously the lookahead `(?=\n[IVX]+\s*[—–-]|$)` only
        # accepted plain Roman + dash as the next-chapter marker. Sagas with
        # combined chapter syntax like Fall of the Thran's "II, III —" broke
        # this: the COMMA in "II, III —" wasn't in `[IVX]+`, so the lookahead
        # didn't fire at that point and chapter I's capture greedily ate the
        # rest of the oracle text. Then chapter 2's label match failed because
        # the regex already consumed everything. Net result: chapters II/III
        # silently never fired their abilities (game_1506623303794561024:476-567
        # had Fall of the Thran progress 1→2→3 with NO chapter resolutions).
        # Fix: include comma + whitespace in the lookahead too.
        pattern = rf'(?:^|\n)([IVX,\s]+)\s*[—–-]\s*(.+?)(?=\n[IVX,][IVX,\s]*\s*[—–-]|$)'
        for match in re.finditer(pattern, card.oracle_text, re.DOTALL):
            chapter_label = match.group(1).strip()
            chapter_text = match.group(2).strip()
            # Check if this chapter label includes our target
            label_parts = [p.strip() for p in chapter_label.split(',')]
            if target_roman in label_parts:
                return chapter_text
        return ""

    def _get_saga_total_chapters(self, card: Card) -> int:
        """Get the total number of chapters in a saga."""
        if not card.oracle_text:
            return 3  # Default
        # Count distinct chapter markers in oracle text
        import re
        chapters = re.findall(r'(?:^|\n)([IVX,\s]+)\s*[—–-]', card.oracle_text)
        if chapters:
            # Find the highest chapter number
            roman_to_int = {'I': 1, 'II': 2, 'III': 3, 'IV': 4, 'V': 5}
            max_chapter = 0
            for label in chapters:
                parts = [p.strip() for p in label.split(',')]
                for p in parts:
                    max_chapter = max(max_chapter, roman_to_int.get(p, 0))
            return max_chapter if max_chapter > 0 else 3
        return 3

    def _process_suspend_upkeep(self, game: GameState) -> List[str]:
        """Delegates to mtg.spells._process_suspend_upkeep (Phase 2G)."""
        from mtg.spells import _process_suspend_upkeep
        return _process_suspend_upkeep(self, game)
    def _process_delayed_triggers(self, game: GameState, phase_name: str) -> List[str]:
        """Fire delayed triggers scheduled for the given phase (upkeep, end_step, etc.)."""
        messages = []
        remaining_delayed = []
        for dt in game.delayed_triggers:
            if dt.get('trigger_at') == phase_name:
                # "your next upkeep" triggers (Pact of Negation, rebound) fire
                # only on their owner's upkeep. Without this gate they fired on
                # whichever upkeep came next — the opponent's, one turn late
                # for the caster, after the caster's lands were tapped again
                # (June 11 audit: decided 6 games). Non-matching upkeeps also
                # must not consume turn_delay.
                upkeep_of = dt.get('upkeep_of')
                phase_of = dt.get('phase_of', upkeep_of)
                # July 23 follow-up: end_step is gate-able too, so "at the
                # beginning of YOUR next end step" (Necropotence) can't fire on
                # the opponent's end step when the ability is activated at
                # instant speed during their turn. Opt-in via phase_of —
                # end_step triggers that set neither key (Yorion's delayed
                # return, Oath of Teferi, Eerie Interlude) resolve to None and
                # stay ungated, i.e. "the next end step", which is correct for
                # them.
                owner_gate = (upkeep_of if phase_name == 'upkeep'
                              else phase_of if phase_name in ('main_phase', 'end_step')
                              else None)
                if owner_gate is not None:
                    try:
                        active_idx = game.players.index(game.active_player)
                    except (ValueError, AttributeError):
                        active_idx = getattr(game, 'active_player_index', 0)
                    if active_idx != owner_gate:
                        remaining_delayed.append(dt)
                        continue
                delay = dt.get('turn_delay', 0)
                if delay <= 0:
                    source = dt.get('source', 'Unknown')
                    print(f"[DELAYED-TRIGGER] Firing from {source}")
                    for action in dt.get('actions', []):
                        try:
                            msg = self.rules._execute_action_on_state(game, action)
                            if msg:
                                messages.append(f"⏰ {source}: {msg}")
                        except Exception as e:
                            print(f"[DELAYED-TRIGGER] Action failed: {e}")
                    # Re-trigger ETBs for returned creatures
                    for action in dt.get('actions', []):
                        if action.get('action') == 'move_card' and action.get('to_zone') == 'battlefield':
                            card_name = action.get('card', '')
                            ctrl_idx = dt.get('controller', 0)
                            ctrl_player = game.players[ctrl_idx] if ctrl_idx < len(game.players) else game.players[0]
                            for c in ctrl_player.battlefield:
                                if c.name == card_name and c.entered_this_turn:
                                    etb_msgs = self._handle_etb_triggers(game, ctrl_player, c)
                                    messages.extend(etb_msgs)
                                    break
                    if not dt.get('once', True):
                        remaining_delayed.append(dt)
                else:
                    dt['turn_delay'] = delay - 1
                    remaining_delayed.append(dt)
            else:
                remaining_delayed.append(dt)
        game.delayed_triggers = remaining_delayed
        return messages

    def _resolve_suspend_spell(self, game: GameState, player: Player, card: Card, opponent: Player) -> List[str]:
        """Delegates to mtg.spells._resolve_suspend_spell (Phase 2G)."""
        from mtg.spells import _resolve_suspend_spell
        return _resolve_suspend_spell(self, game, player, card, opponent)
    def end_turn(self, game: GameState) -> List[str]:
        """End the current turn and start the next. Returns end-step messages (e.g. discard prompts)."""
        # (Slices 2c + 3c, July 24: both parity recorders retired — clean
        # post-flip batches at [EVENT-PARITY]=0 and [EVENT-PARITY-DIES]=0.)

        # [TRANSFORM] Save spell count for day/night and werewolf transform tracking
        game.active_player.spells_cast_prev_turn = game.active_player.spells_cast_this_turn
        # The day/night check runs at the NEXT upkeep and must see the turn that
        # just ended (Daybound: "If a player casts no spells during their own
        # turn, it becomes night next turn"). Reading the incoming active
        # player's own spells_cast_prev_turn instead looked one whole turn
        # further back and produced provably wrong flips.
        game._spells_cast_last_turn = game.active_player.spells_cast_this_turn
        # Reset EVERY player's per-turn counter, not just the active one. Only
        # the active player was reset, so instants a player cast during their
        # OPPONENT's turn stayed on the books and were later miscounted as
        # spells cast "during their own turn".
        for _p in game.players:
            _p.spells_cast_this_turn = 0
            _p.noncreature_spells_cast_this_turn = 0  # Reset for Esper Sentinel

        # Reset per-turn flicker dedup tracker (see actions.py flicker handler)
        if hasattr(game, '_flicker_announce_seen'):
            game._flicker_announce_seen = {}
        # Reset nested flicker depth counter — should always be 0 between turns,
        # but reset defensively in case an exception left the counter stuck.
        game._flicker_depth = 0

        # Reset turn-based state
        game.active_player.has_drawn_for_turn = False

        # [ENDSTEP-TRIGGER] Fire end step triggers BEFORE clearing effects
        # (Meren, Soulherder, Conjurer's Closet, Thassa, etc.)
        # This was previously only firing from advance_phase(Phase.END) which
        # end_turn() bypasses entirely. Bug found in Apr 2026 audit.
        endstep_trigger_msgs = []
        try:
            endstep_msgs_sync, endstep_unhandled = self._check_end_step_triggers_sync(game)
            endstep_trigger_msgs.extend(endstep_msgs_sync)
            # Sync context: queue unhandled triggers for async Tier 3 drain.
            active = game.active_player
            for trigger_card, trigger_text in endstep_unhandled:
                ctrl_player = active
                for p in game.players:
                    if trigger_card in p.battlefield:
                        ctrl_player = p
                        break
                self._queue_async_trigger(
                    game, trigger_card, trigger_text, "end_step",
                    ctrl_player.name, context=f"Triggered at beginning of {active.name}'s end step (end_turn path)",
                )
                # May 25 audit (F7): mirror the advance_phase end-step path —
                # suppress 📍 placeholder when async Tier 3 will emit ⚡ later.
                if not self.rules.client:
                    from mtg.helpers import format_trigger_line
                    endstep_trigger_msgs.append(
                        format_trigger_line(
                            "📍", trigger_card.name, trigger_text,
                            game=game, max_chars=300,
                        )
                    )
                else:
                    print(f"[ENDSTEP-PLACEHOLDER-SUPPRESSED] {trigger_card.name}: queued for Tier 3 drain (end_turn path)")
        except Exception as e:
            print(f"[ENDSTEP-TRIGGER] Error in end_turn: {e}")

        # [DELAYED-TRIGGER] Fire delayed triggers scheduled for end step
        try:
            delayed_msgs = self._process_delayed_triggers(game, "end_step")
            endstep_trigger_msgs.extend(delayed_msgs)
        except Exception as e:
            print(f"[DELAYED-TRIGGER] Error in end_turn: {e}")

        # Clear all end-of-turn effects (pump spells, temp keywords, turn triggers)
        self.clear_end_of_turn_effects(game)

        end_step_msgs = self.rules.on_end_step(game)
        end_step_msgs = endstep_trigger_msgs + end_step_msgs

        # Switch active player
        game.active_player_index = 1 - game.active_player_index
        game.priority_player_index = game.active_player_index
        game.turn_number += 1
        game.phase = Phase.UNTAP
        # Bloodchief Ascension's each-end-step condition reads life lost
        # during the current turn. Clear every player's ledger only after the
        # old turn's end-step triggers have resolved.
        for _player in game.players:
            _player.life_lost_this_turn = 0
            _player.dealt_combat_damage_this_turn = False

        # Clear the per-turn resolve_effect dedupe cache (keyed on turn number
        # in the dict itself, but pruning keeps the dict from growing unbounded
        # across long games).
        try:
            if hasattr(self, 'rules') and getattr(self.rules, '_resolve_dedupe', None) is not None:
                keep = {}
                for k, v in self.rules._resolve_dedupe.items():
                    if len(k) == 3 and k[2] >= game.turn_number - 1:
                        keep[k] = v
                self.rules._resolve_dedupe = keep
        except Exception:
            pass

        # CR 502 — full untap step for new active player. Includes phase-in,
        # temporary replacement-effect cleanup (Teferi's Protection, Fog),
        # and summoning-sickness clearing. Previously end_turn() only called
        # untap_all(), bypassing on_untap_step entirely — Teferi's Protection
        # damage prevention never expired (Apr 19 audit regression).
        try:
            self.rules.on_untap_step(game)
        except Exception as e:
            print(f"[UNTAP] Error in on_untap_step from end_turn: {e}")
        self.untap_all(game.active_player)
        game.active_player.lands_played_this_turn = 0
        game.active_player.landfall_count_this_turn = 0
        # Recalculate max land drops from static abilities
        # (Exploration, Oracle of Mul Daya, Wayward Swordtooth, Dryad of the Ilysian Grove, etc.)
        game.active_player.max_lands_per_turn = 1  # Base
        extra_land_keywords = [
            'you may play an additional land',
            'you may play two additional lands',
            'play an additional land on each of your turns',
        ]
        for c in game.active_player.battlefield:
            oracle = (c.oracle_text or '').lower()
            for kw in extra_land_keywords:
                if kw in oracle:
                    if 'two additional' in oracle:
                        game.active_player.max_lands_per_turn += 2
                    else:
                        game.active_player.max_lands_per_turn += 1
                    break  # Don't double-count from same card
        if game.active_player.max_lands_per_turn > 1:
            print(f"[LAND-DROPS] {game.active_player.name} can play {game.active_player.max_lands_per_turn} lands this turn")

        # Reset planeswalker activation tracking for the new turn
        if self.planeswalker_manager:
            self.planeswalker_manager.on_turn_start(game)

        return end_step_msgs or []

    def _validate_plan_mana(self, game: GameState, player_idx: int, plan: list) -> list:
        """Delegates to mtg.ai_turn._validate_plan_mana (Phase 2F)."""
        from mtg.ai_turn import _validate_plan_mana
        return _validate_plan_mana(self, game, player_idx, plan)
    async def execute_claude_turn(self, game: GameState) -> List[str]:
        """Delegates to mtg.ai_turn.execute_claude_turn (Phase 2F)."""
        from mtg.ai_turn import execute_claude_turn
        return await execute_claude_turn(self, game)
    async def continue_claude_post_combat(self, game: GameState) -> List[str]:
        """Delegates to mtg.ai_turn.continue_claude_post_combat (Phase 2F)."""
        from mtg.ai_turn import continue_claude_post_combat
        return await continue_claude_post_combat(self, game)
    def _get_action_error(self, game: GameState, player_index: int, action: Dict) -> str:
        """Delegates to mtg.ai_turn._get_action_error (Phase 2F)."""
        from mtg.ai_turn import _get_action_error
        return _get_action_error(self, game, player_index, action)
    async def _validate_activation(self, game: GameState, player: 'Player',
                                   card: 'Card', ability_cost: str = None) -> Tuple[bool, str]:
        """Delegates to mtg.ai_turn._validate_activation (Phase 2F)."""
        from mtg.ai_turn import _validate_activation
        return await _validate_activation(self, game, player, card, ability_cost)
    async def _execute_action(self, game: GameState, player_index: int, action: Dict) -> Optional[str]:
        """Execute a player action."""
        player = game.players[player_index]
        action_type = action.get("type")
        # June 10 audit (C3/V28): positional cast→resolve pairing — see the
        # twin logic in mtg/autoplay.py:_autoplay_execute_action. Capture the
        # previous action's stamp, clear it; cast/activate branches re-stamp.
        _prev_cast_like = getattr(game, '_last_exec_cast_like', None)
        game._last_exec_cast_like = None
        
        if action_type == "play_land":
            card_name = action.get("card")
            if not card_name:
                print(f"[EXECUTE] play_land action missing 'card' field: {action}")
                return None
            card = player.find_card(card_name, Zone.HAND)
            if card:
                # Check for MDFC and auto-select face based on deck needs
                mdfc_info = get_mdfc_info(card.name)
                if mdfc_info:
                    # Pick face based on what colors are needed in hand
                    hand_colors = set()
                    for c in player.hand:
                        if c.mana_cost:
                            for color in ['W', 'U', 'B', 'R', 'G']:
                                if f'{{{color}}}' in c.mana_cost.upper():
                                    hand_colors.add(color)
                    
                    # Choose back if we need that color and don't need front
                    if mdfc_info["back_produces"] in hand_colors and mdfc_info["front_produces"] not in hand_colors:
                        card.played_face = "back"
                        card.mdfc_back_name = mdfc_info["back_name"]
                        print(f"[EXECUTE] MDFC auto-selected BACK face: {mdfc_info['back_name']} (produces {{{mdfc_info['back_produces']}}})")
                    else:
                        card.played_face = "front"
                        print(f"[EXECUTE] MDFC auto-selected FRONT face: {mdfc_info['front_name']} (produces {{{mdfc_info['front_produces']}}})")
                
                success, msg = self.play_land(game, player, card)
                print(f"[EXECUTE] play_land {card_name}: success={success}, msg={msg}")
                if success:
                    # Check land ETB triggers
                    land_etb_msgs = self._handle_land_etb(game, player, card)
                    
                    # Show the correct face name for MDFCs
                    display_name = card.display_name().split(" ")[0]  # Just the name without indicators
                    if card.played_face == "back" and card.mdfc_back_name:
                        result_msg = f"🌍 {player.name} played **{card.mdfc_back_name}**"
                    else:
                        result_msg = f"🌍 {player.name} played **{card.name}**"
                    # Include shockland life payment in Discord message
                    if "paid 2 life" in msg:
                        result_msg += f" (paid 2 life — life: {player.life})"
                    elif "entered tapped" in msg and "didn't pay" in msg:
                        result_msg += " (entered tapped)"
                    
                    # Append ETB messages on separate lines so multi-event
                    # ETBs (Obscura Storefront: sacrifice + search + lifegain)
                    # don't get crammed onto one Discord line.
                    if land_etb_msgs:
                        result_msg += "\n" + "\n".join(land_etb_msgs)

                    return result_msg
            else:
                # [PLAN-STALE] Planner chose a land that was already played earlier this turn
                # (plan_turn context is one action stale). Skip silently — don't surface to Discord.
                hand_names = [c.name for c in player.hand]
                print(f"[PLAN-STALE] play_land '{card_name}' not in hand (already played this turn?). Hand: {hand_names}")

        elif action_type == "cast":
            card_name = action.get("card")
            if not card_name:
                print(f"[EXECUTE] cast action missing 'card' field: {action}")
                return None
            # June 10 (C3): stamp for positional cast→resolve pairing.
            game._last_exec_cast_like = {'turn': game.turn_number, 'type': 'cast',
                                         'card': card_name}
            target_name = _normalize_action_target(action)  # May be None
            adventure_name = action.get("adventure")  # Adventure half name
            card = player.find_card(card_name, Zone.HAND)

            # May 20 audit (Bug C): when the AI casts a detrimental targeted
            # spell by NAME and both players control a permanent with that
            # name, the engine's find_card_on_battlefield default returns the
            # FIRST match in player-index order — frequently Claude's own
            # creature when Claude is player 0. game_1506623255119925278:1492
            # had Claude cast Prismatic Ending targeting "Snapcaster Mage"
            # and exile his OWN Snapcaster instead of Rick's. Infer
            # target_controller=opponent for detrimental spells when no
            # target_controller is set, so the downstream resolution
            # disambiguates correctly.
            if target_name and not action.get("target_controller"):
                _hand_card = card or next(
                    (c for c in player.hand if c.name.lower() == card_name.lower()),
                    None,
                )
                if _hand_card and _hand_card.oracle_text:
                    _o = _hand_card.oracle_text.lower()
                    _is_detrimental_targeted = (
                        ('destroy target' in _o)
                        or ('exile target' in _o and 'creature' in _o)
                        or ('exile target' in _o and 'permanent' in _o)
                        or ('exile target' in _o and 'nonland' in _o)
                        or ('deal' in _o and 'damage to target' in _o
                            and ('creature' in _o or 'permanent' in _o or 'planeswalker' in _o))
                        or ('counter target' in _o)
                    )
                    # Exclude self-targeted beneficial effects (flicker, pump,
                    # protect — "target creature you control")
                    _has_self_target_clause = (
                        'target creature you control' in _o
                        or 'target permanent you control' in _o
                    )
                    if _is_detrimental_targeted and not _has_self_target_clause:
                        opp = game.players[1 - game.players.index(player)]
                        action["target_controller"] = opp.name
                        print(f"[CAST-TARGET] Inferred target_controller={opp.name} "
                              f"for detrimental cast {card_name} → {target_name}")

            # If card not found by name, check if Claude used the adventure name
            if not card:
                for c in player.hand:
                    if c.adventure_name and c.adventure_name.lower() == card_name.lower():
                        card = c
                        adventure_name = c.adventure_name  # Auto-set adventure flag
                        print(f"[EXECUTE] Claude used adventure name '{card_name}', found {c.name}")
                        break

            # Check if name matches a split card half
            if not card:
                for c in player.hand:
                    if c.split_names:
                        for i, sname in enumerate(c.split_names):
                            if card_name and card_name.lower() == sname.lower():
                                c.cast_as_split_half = i
                                card = c
                                print(f"[EXECUTE] Claude used split half '{card_name}', found {c.name}")
                                break
                    if card:
                        break

            # Check if it's playable from exile (Chandra impulse draw, Light Up the Stage, etc.)
            if not card and player.exile:
                for c in player.exile:
                    _castable_from_exile = (c.id in player.playable_from_exile
                                            or getattr(c, '_adventure_exiled', False))
                    if _castable_from_exile and card_name and c.name.lower() == card_name.lower():
                        card = c
                        player.exile.remove(c)
                        # cast_spell_async gates on zone membership first (July
                        # 20), so a card pulled out of exile with no home is
                        # rejected as "Card not in hand" — the same hand-append
                        # the cog and autoplay paths have always done.
                        player.hand.append(c)
                        if c.id in player.playable_from_exile:
                            player.playable_from_exile.remove(c.id)
                        c._adventure_exiled = False
                        print(f"[IMPULSE-DRAW] AI casting {c.name} from exile")
                        break

            # Bug #28: Check if it's playable from graveyard (Snapcaster flashback, native flashback, escape)
            from_graveyard = False
            if not card and player.playable_from_graveyard:
                for c in player.graveyard:
                    if c.id in player.playable_from_graveyard and card_name and c.name.lower() == card_name.lower():
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
                                c._escape_cost = _esc_cost
                                c._was_escaped = True
                                exiled_names = []
                                for _ in range(exile_count):
                                    if player.graveyard:
                                        exiled = player.graveyard.pop()
                                        player.exile.append(exiled)
                                        exiled_names.append(exiled.name)
                                print(f"[ESCAPE] AI casting {c.name} from graveyard, exiling {exile_count}: {', '.join(exiled_names)}")
                            else:
                                print(f"[FLASHBACK] AI casting {c.name} from graveyard via flashback")
                        else:
                            print(f"[FLASHBACK] AI casting {c.name} from graveyard via flashback")
                        break

            # Check command zone (commander)
            from_command_zone = False
            if not card and game.format in COMMAND_ZONE_FORMATS:
                for cmd_card in player.command_zone:
                    if card_name and (cmd_card.name.lower() == card_name.lower() or card_name.lower() in cmd_card.name.lower()):
                        card = cmd_card
                        from_command_zone = True
                        print(f"[COMMANDER] AI casting {cmd_card.name} from command zone")
                        break

            if card:
                # Set adventure flag if Claude chose to cast the adventure half
                if adventure_name and card.adventure_name:
                    card.cast_as_adventure = True
                    print(f"[EXECUTE] Casting adventure: {card.adventure_name} (of {card.name})")
                # Set split-half flag if Claude used "adventure" key for a split card
                # (AI says {"adventure": "Memory"} for Commit // Memory — route to split machinery)
                elif adventure_name and getattr(card, 'split_names', None):
                    for i, sname in enumerate(card.split_names):
                        if adventure_name.lower() == sname.lower():
                            card.cast_as_split_half = i
                            print(f"[EXECUTE] AI used 'adventure' key for split half '{sname}' (half {i} of {card.name})")
                            break

                # Find target if specified
                target = None
                if target_name:
                    # Shared with the autoplay cast path (mtg/helpers.py) —
                    # stack -> battlefield -> graveyard -> player/pronoun.
                    # These were two divergent copies until the July 27 fanout;
                    # see resolve_cast_target for what each one was missing.
                    from mtg.helpers import resolve_cast_target
                    target = resolve_cast_target(game, player, card, target_name)

                # Guard: don't cast counterspells when there's nothing on the stack
                # Exception 1: modal spells (Mystic Confluence, Cryptic Command) have other modes
                # Exception 2: creatures with counter ETBs (Frilled Mystic, Draining Whelk) — the
                # creature is legal to cast; the ETB will just fizzle if there's no target
                oracle_lower = (card.oracle_text or '').lower()
                is_modal = any(phrase in oracle_lower for phrase in ['choose one', 'choose two', 'choose three'])
                is_creature_with_counter_etb = card.is_creature() and ('enters' in oracle_lower or 'enter' in oracle_lower)
                if 'counter target' in oracle_lower and not game.stack and not is_modal and not is_creature_with_counter_etb:
                    print(f"[EXECUTE] {card.name} has no valid target (stack empty)")
                    return None

                # [TARGETING] Pre-cast target validation — block AI from casting
                # targeted spells when no legal target exists (CR 601.2c)
                if HAS_TARGETING and _spell_requires_targets(card):
                    if not _find_any_valid_target(game, card, player.name):
                        print(f"[TARGETING] AI tried to cast {card.name} with no valid targets")
                        return None

                # Set X value if AI provided one (Blue Sun's Zenith, etc.)
                x_val = action.get("x_value") or action.get("X") or action.get("x")
                if x_val is not None:
                    try:
                        card._x_value = int(x_val)
                    except (ValueError, TypeError):
                        pass

                # Handle command zone casting (commander tax + move to hand)
                commander_tax = 0
                if from_command_zone:
                    commander_tax = card.times_cast_from_command_zone * 2
                    player.command_zone.remove(card)
                    player.hand.append(card)
                    if commander_tax > 0:
                        print(f"[COMMANDER] {card.name} commander tax: {{{commander_tax}}}")

                # Handle graveyard casting (flashback/escape — move to hand for cast_spell_async)
                if from_graveyard and card not in player.hand:
                    player.hand.append(card)

                # Mark flashback/escape cast so templates (Increasing Devotion,
                # Snapcaster tracking) can detect graveyard-origin casting.
                if from_graveyard:
                    card._cast_from_graveyard = True

                # Apr 30 audit fix #21: stash modal-spell mode selection on the
                # card so the template path can read it via ctx['_modes']. AI
                # plan format: {"type":"cast","card":"Kolaghan's Command","modes":[3,4]}
                # OR mode names: {"modes":["damage","discard"]}.
                modes = action.get("modes") or action.get("mode")
                if modes:
                    card._modes_chosen = modes if isinstance(modes, list) else [modes]

                # Use async version with spell resolution
                success, msg, effect_msgs = await self.cast_spell_async(game, player, card, target=target, additional_cost=commander_tax)
                print(f"[EXECUTE] cast {card_name}: success={success}, msg={msg}")
                # July 20 batch-3 audit: keep the REAL failure reason for
                # _get_action_error — returning None below discards it, and
                # the re-derived reason misclassifies aura/graveyard-target
                # failures as "unknown reason — mana looks sufficient".
                if not success and msg:
                    game._last_cast_failure = (game.turn_number, card.name, msg)
                if effect_msgs:
                    for em in effect_msgs:
                        print(f"[EXECUTE] effect: {em}")
                # July 20 audit: a FAILED commander cast left the card
                # stranded in hand until the periodic CR-903.9 sweep — during
                # that window it was visible to hand-size/discard effects,
                # and a discard-to-hand-size could bury it in the graveyard,
                # which the sweep never scans (game_1526071401499328634
                # showed the strand; the graveyard hole is the latent trap).
                # Roll it straight back.
                if not success and from_command_zone and card in player.hand:
                    player.hand.remove(card)
                    player.command_zone.append(card)
                    print(f"[COMMANDER] {card.name} cast failed — returned to command zone immediately")
                if success:
                    if from_command_zone:
                        card.times_cast_from_command_zone += 1
                    # Flashback/escape: exile the card after resolution instead of graveyard
                    if from_graveyard:
                        if card in player.graveyard:
                            player.graveyard.remove(card)
                            player.exile.append(card)
                            print(f"[FLASHBACK] {card.name} exiled after casting from graveyard")
                    # May 7 audit fix #1: if cast_spell_async already announced
                    # the cast (before the priority window), don't duplicate it.
                    # Drop the prefix line and just return the effect messages.
                    already_announced = (
                        hasattr(game, '_early_announced_casts')
                        and id(card) in game._early_announced_casts
                    )
                    if already_announced:
                        # Consume the marker so it can be re-cast later (suspend/flashback recasts).
                        game._early_announced_casts.discard(id(card))
                        # Return only the effect messages — the caller's send loop
                        # will post them after the early-announced cast line.
                        if effect_msgs:
                            return "\n".join(effect_msgs)
                        # Nothing more to say (cast already announced + no effects).
                        # Return a tagged sentinel so the per-action plan loop
                        # ("if result:") sees success, but _sanitize_action_bullets
                        # strips it before it reaches Discord. The "[EARLY-CAST]"
                        # prefix is in the sanitizer's debug-prefix list.
                        return f"[EARLY-CAST] {card.name}"
                    # Return both cast message and effect messages
                    result = f"✨ {player.name} cast **{card.name}**"
                    if from_graveyard:
                        result += " (from graveyard)"
                    if effect_msgs:
                        result += "\n" + "\n".join(effect_msgs)
                    return result
            else:
                # [PLAN-STALE] Card not in hand. This often means a prior action in
                # the same plan moved the card out of hand (e.g. Aminatou's +1
                # returned it to library, Brainstorm put it back on top, a discard
                # effect already took it). Silently drop the action instead of
                # retrying — the plan's hand snapshot is stale, not the AI
                # hallucinating.
                hand_names = [c.name for c in player.hand]
                print(f"[EXECUTE] cast '{card_name}': NOT IN HAND. Hand contains: {hand_names}")
                # Try fuzzy match as last resort
                if card_name:
                    card_lower = card_name.lower()
                    for c in player.hand:
                        if card_lower in c.name.lower() or c.name.lower() in card_lower:
                            print(f"[EXECUTE] Fuzzy match found '{c.name}' for '{card_name}' — using it")
                            card = c
                            break
                    if card:
                        success, msg, effect_msgs = await self.cast_spell_async(game, player, card, target=None)
                        if success:
                            result = f"{player.name} cast {card.name}"
                            if effect_msgs:
                                result += "\n" + "\n".join(effect_msgs)
                            return result
                # Silent drop: log as PLAN-STALE (non-fatal, expected post-Aminatou/Brainstorm/etc.)
                print(f"[PLAN-STALE] '{card_name}' not in hand — prior plan action likely moved it. Available: {', '.join(hand_names[:10])}")
                return None

        elif action_type == "attack":
            creature_names = action.get("creatures", [])
            attacking = []
            for name in creature_names:
                card = player.find_card(name, Zone.BATTLEFIELD)
                # May 25 audit (F24): pass `game` to is_creature so devotion-
                # gated Theros gods (Heliod isn't a creature unless devotion
                # to white ≥5) can't attack while their condition fails.
                # CR 508.1a: attackers must be creatures.
                if card and card.is_creature(game=game) and not card.tapped:
                    card.attacking = True
                    card.attacking_player = 1 - player_index
                    self.tap_permanent(card)
                    attacking.append(card.name)
                    game.attackers.append(card.id)
            if attacking:
                return f"{player.name} attacks with {', '.join(attacking)}"
        
        elif action_type == "tap":
            card_name = action.get("card")
            card = player.find_card(card_name, Zone.BATTLEFIELD)
            if card and self.tap_permanent(card):
                return f"{player.name} tapped {card.name}"
        
        elif action_type == "activate":
            perm_name = action.get("permanent")
            if not perm_name:
                print(f"[EXECUTE] activate action missing 'permanent' field: {action}")
                return None
            # June 10 (C3): stamp for positional cast→resolve pairing.
            game._last_exec_cast_like = {'turn': game.turn_number, 'type': 'activate',
                                         'card': perm_name}
            try:
                ability_idx = int(action.get("ability", 0))
            except (ValueError, TypeError):
                ability_idx = 0
            target_name = _normalize_action_target(action)

            # Per-turn activation limit to prevent infinite loops (Sensei's Divining Top, etc.)
            # Only apply limit to non-sacrifice abilities — sacrifice outlets like Altar of Dementia
            # are naturally limited by available creatures to sacrifice
            if not hasattr(game, '_activation_counts'):
                game._activation_counts = {}
            act_key = (perm_name or "").lower()
            act_count = game._activation_counts.get(act_key, 0)
            # Check if this is a sacrifice-cost ability (no activation limit needed)
            perm_check = player.find_card(perm_name, Zone.BATTLEFIELD)
            if not perm_check and perm_name:
                perm_lower = perm_name.lower()
                for c in player.battlefield:
                    if perm_lower in c.name.lower() or c.name.lower().startswith(perm_lower):
                        perm_check = c
                        break
            # Also check hand for cycling/channel abilities
            hand_card_with_cycling = None
            if not perm_check and perm_name:
                perm_lower = perm_name.lower()
                for c in player.hand:
                    if perm_lower in c.name.lower() or c.name.lower().startswith(perm_lower):
                        oracle = (c.oracle_text or '').lower()
                        if 'cycling' in oracle or 'channel' in oracle:
                            perm_check = c
                            hand_card_with_cycling = c
                            break
            # Apr 30 audit: when AI plans `activate <card>` for a card in hand
            # whose only activated ability is cycling, route to the cycle action.
            if hand_card_with_cycling is not None:
                cycle_action = {
                    "action": "cycle",
                    "player": player.name,
                    "card": hand_card_with_cycling.name,
                    "x": action.get("X") or action.get("x") or 0,
                }
                try:
                    print(f"[ACTIVATE-CLAUDE] Routing activate→cycle for {hand_card_with_cycling.name} (in hand)")
                    return self.rules._execute_action_on_state(game, cycle_action)
                except Exception as e:
                    print(f"[ACTIVATE-CLAUDE] cycle routing failed: {e}")
                    return None
            is_sacrifice_ability = False
            if perm_check and perm_check.oracle_text:
                oracle = perm_check.oracle_text.lower()
                is_sacrifice_ability = 'sacrifice' in oracle and ('sacrifice a ' in oracle or 'sacrifice another' in oracle)
            max_activations = 15 if is_sacrifice_ability else 3
            if act_count >= max_activations:
                msg = f"{perm_name} already used its ability this turn — cannot activate again"
                print(f"[ACTIVATE-CLAUDE] {msg}")
                return msg

            perm = player.find_card(perm_name, Zone.BATTLEFIELD)
            # Fuzzy match: AI sends "Daretti" vs full "Daretti, Scrap Savant"
            if not perm and perm_name:
                perm_lower = perm_name.lower()
                for c in player.battlefield:
                    if perm_lower in c.name.lower() or c.name.lower().startswith(perm_lower):
                        perm = c
                        print(f"[ACTIVATE-CLAUDE] Fuzzy matched '{perm_name}' to '{c.name}'")
                        break
            if not perm:
                return None
            game._activation_counts[act_key] = act_count + 1

            # Cross-check with XMage bridge + Python-side validation
            if not perm.is_planeswalker():  # PW validation is handled by planeswalker_manager
                is_legal, reason = await self._validate_activation(game, player, perm)
                if not is_legal:
                    print(f"[VALIDATE-ACTIVATE] Blocked {perm.name}: {reason}")
                    return None

            # Handle planeswalker abilities
            if perm.is_planeswalker():
                if self.planeswalker_manager:
                    abilities = self.planeswalker_manager.parse_abilities(perm)
                    ability_idx = _normalize_pw_ability_idx(ability_idx, abilities)
                    if ability_idx is None or ability_idx >= len(abilities):
                        print(f"[ACTIVATE-PW] {perm.name}: ability index {action.get('ability')!r} out of range "
                              f"(has {len(abilities)} abilities)")
                        return None
                    ability = abilities[ability_idx]
                    can_act, reason = self.planeswalker_manager.can_activate(game, player, perm, ability_idx)
                    if not can_act:
                        print(f"[ACTIVATE-PW] {perm.name} activation blocked: {reason}")
                        return None
                    # [FIX-5] Forward explicit target from batch plan action dict.
                    # When plan contains {"type":"activate","permanent":"Aminatou","ability":1,"target":"Mulldrifter"},
                    # resolve the target name to the actual card/player object and pass it to activate().
                    explicit_target_name = _normalize_action_target(action)
                    auto_targets = None
                    if explicit_target_name:
                        target_obj = _resolve_player_or_card_target(game, player, explicit_target_name)
                        if target_obj is not None:
                            auto_targets = [target_obj]
                            tname = target_obj.name if hasattr(target_obj, 'name') else target_obj
                            print(f"[ACTIVATE-PW] Forwarding explicit target '{explicit_target_name}' → {tname}")
                        else:
                            print(f"[ACTIVATE-PW] Could not resolve explicit target '{explicit_target_name}' — proceeding without target")
                    result = await self.planeswalker_manager.activate(game, player, perm, ability_idx, auto_targets)
                    if result and result.success:
                        msgs = "\n".join(result.messages) if result.messages else ""
                        cost_str = f"+{ability.loyalty_cost}" if ability.loyalty_cost > 0 else str(ability.loyalty_cost)
                        # [PW-ACTIVATE] now prints from PlaneswalkerManager.activate
                        # so Rick's and the human's activations are tagged too.
                        # result.messages[0] is self-describing (header + oracle text);
                        # don't prepend a duplicate header.
                        return msgs or f"{player.name} activates {perm.name}'s [{cost_str}] ability"
                    # Bug 2a: surface the target-required message and the target prompt so the
                    # retry loop's last_error isn't just "None". Stash on game so the error
                    # helper can read it, and log the target prompt for debugging.
                    result_msgs = getattr(result, 'messages', None) if result else None
                    needs_targets = bool(getattr(result, 'needs_targets', False)) if result else False
                    target_prompt = getattr(result, 'target_prompt', None) if result else None
                    first_msg = (result_msgs[0] if result_msgs else None) or target_prompt
                    if needs_targets:
                        print(f"[ACTIVATE-PW] {perm.name} needs target: {first_msg or '(no prompt provided)'}")
                        game._last_pw_target_error = (
                            f"{perm.name} needs a target. {first_msg or 'Supply target=<name> in the action.'}"
                        )
                        # Bail after 1 attempt — the planner doesn't currently consume target
                        # prompts on retry, so looping just produces "retry 4/3: None" noise.
                        game._pw_activation_blocked = perm_name.lower() if perm_name else ""
                    else:
                        print(f"[ACTIVATE-PW] {perm.name} activate() returned failure: {result}")
                    # May 7 audit (Bug 1): surface the failure messages to Discord
                    # when the planeswalker manager returned a refund (no legal
                    # target → loyalty restored). The activate() return now
                    # includes a ❌ "activation refunded" line that the player
                    # needs to see; without this, the failure is silent.
                    if result_msgs and any('refund' in m.lower() or '❌' in m for m in result_msgs):
                        # Per-game dedup: if the same (planeswalker, ability)
                        # has already shown its full refund + oracle dump in
                        # this game, only emit a short follow-up so the thread
                        # isn't spammed with the same oracle text 5-8 times.
                        if not hasattr(game, '_pw_refund_shown'):
                            game._pw_refund_shown = set()
                        refund_key = ((perm.name or '').lower(), int(ability_idx))
                        if refund_key in game._pw_refund_shown:
                            cost_str = (f"+{ability.loyalty_cost}"
                                        if ability.loyalty_cost > 0
                                        else str(ability.loyalty_cost))
                            return (f"❌ {perm.name} [{cost_str}] — no legal target "
                                    f"(activation refunded again this game)")
                        game._pw_refund_shown.add(refund_key)
                        return "\n".join(result_msgs)
                    return None
                print(f"[ACTIVATE-PW] No planeswalker_manager available")
                return None
            
            # Handle non-planeswalker activated abilities
            # Parse abilities from oracle text
            abilities = []
            if perm.oracle_text:
                # CR 702.32: cycling is a hand-only activated ability. Never surface
                # it as an activatable while the card is on the battlefield — otherwise
                # the AI pays the cycling cost and gets the cycle triggers for free
                # without actually discarding the card (Shark Typhoon infinite loop).
                # "Channel" has the same zone restriction.
                for line in perm.oracle_text.split('\n'):
                    line_stripped = line.strip()
                    line_lower = line_stripped.lower()
                    if re.match(r'^(cycling|channel)\b', line_lower):
                        # Hand-only keyword ability — skip while on battlefield.
                        print(f"[ACTIVATE-CLAUDE] Skipping hand-only ability on {perm.name}: {line_stripped}")
                        continue
                    # Keyword abilities with no colon: Equip {N}, Cycling {N}, etc.
                    # June 11 live retest: require a word boundary. `^Equip\s*`
                    # also matched "Equipped creature gets..." because `\s*`
                    # may consume zero characters, creating a fake free equip
                    # ability before the real `Equip {N}` line.
                    equip_match = re.match(r'^Equip\b\s*(?:—\s*)?(.+?)(?:\s*\(.*\))?$', line_stripped, re.IGNORECASE)
                    if equip_match:
                        abilities.append({
                            'cost': equip_match.group(1).strip(),
                            'effect': 'Attach to target creature you control',
                            'needs_tap': False,
                            'is_equip': True,
                        })
                        continue
                    if ':' in line and not line_stripped.startswith('('):
                        parts = line.split(':', 1)
                        if len(parts) == 2:
                            cost = parts[0].strip()
                            effect = parts[1].strip()
                            # Skip triggered abilities and loyalty abilities
                            if not any(kw in cost.lower() for kw in ['when', 'whenever', 'at the beginning']):
                                if not re.match(r'^[+-]?\d+$', cost):
                                    # Skip any colon-line that surfaces a cycling/channel
                                    # trigger as an "activated" ability (defensive).
                                    if re.match(r'^(cycling|channel)\b', cost.lower()):
                                        continue
                                    has_tap = '{T}' in cost or '{Q}' in cost
                                    abilities.append({
                                        'cost': cost,
                                        'effect': effect,
                                        'needs_tap': has_tap
                                    })
            
            if not abilities:
                # No activated abilities found — might be a triggered ability the AI confused
                print(f"[ACTIVATE-CLAUDE] {perm.name} has no activated abilities (oracle: {(perm.oracle_text or '')[:100]})")
                return None  # Signal failure — AI should not try to activate this

            # AI often sends 1-indexed ability numbers; clamp to valid range
            if ability_idx >= len(abilities):
                if abilities:
                    print(f"[ACTIVATE-CLAUDE] {perm.name}: ability index {ability_idx} out of range "
                          f"(has {len(abilities)}), using index 0")
                    ability_idx = 0
                else:
                    return None

            if ability_idx < len(abilities):
                ability = abilities[ability_idx]

                # July 20 batch-3 audit (reviewer V4): the AI's standard equip
                # shape is {"ability": 0, "target": <own creature>}, but the
                # abilities list is oracle-line-ordered — Umezawa's Jitte's
                # charge-counter modal sits at [0] and Equip at [1], so every
                # equip attempt died at "no charge counter to remove" and
                # Jitte never equipped for 30+ turns. A request on an
                # Equipment that names the player's OWN creature as target is
                # unambiguous equip intent; reroute to the Equip ability.
                if (not ability.get('is_equip') and target_name
                        and 'equipment' in (perm.type_line or '').lower()):
                    _equip_idx = next((i for i, ab in enumerate(abilities)
                                       if ab.get('is_equip')), None)
                    if _equip_idx is not None:
                        _t = player.find_card(target_name, Zone.BATTLEFIELD)
                        if _t is not None and _t.is_creature():
                            print(f"[ACTIVATE-CLAUDE] {perm.name}: rerouting ability "
                                  f"{ability_idx} → {_equip_idx} (equip intent: own-creature target)")
                            ability_idx = _equip_idx
                            ability = abilities[_equip_idx]

                # Check if can activate
                if ability['needs_tap'] and perm.tapped:
                    return None
                if ability['needs_tap'] and perm.is_creature():
                    if perm.entered_this_turn and not perm.has_haste():
                        return None  # Summoning sickness

                counter_cost_match = re.search(
                    r'remove (?:a|one|1) ([\w +/\-]+) counter from (?:this|'
                    + re.escape(perm.name.lower()) + r')', ability['cost'].lower())
                if counter_cost_match:
                    counter_type = counter_cost_match.group(1).strip()
                    if perm.counters.get(counter_type, 0) < 1:
                        print(f"[ACTIVATE-CLAUDE] {perm.name}: no {counter_type} counter to remove")
                        return None

                # Deduct mana costs from the ability cost string
                # Parses costs like "{1}", "{2}{G}", "{W}{U}", etc.
                cost_str = ability['cost']
                # July 20 batch-3 audit (reviewer V5): equip-cost reducers
                # (Auriok Steelshaper "Equip costs you pay cost {1} less")
                # were never applied anywhere — equip always charged the
                # printed cost. Reduce the generic component (CR 601.2f).
                if ability.get('is_equip'):
                    _equip_red = 0
                    for _src in player.battlefield:
                        _rm = re.search(r'equip (?:abilities you activate|costs you pay) cost \{(\d+)\} less',
                                        (_src.oracle_text or '').lower())
                        if _rm:
                            _equip_red += int(_rm.group(1))
                    if _equip_red:
                        _gm = re.search(r'\{(\d+)\}', cost_str)
                        if _gm:
                            _newgen = max(0, int(_gm.group(1)) - _equip_red)
                            _new_cost = cost_str.replace(
                                _gm.group(0), f'{{{_newgen}}}' if _newgen else '', 1)
                            print(f"[ACTIVATE-CLAUDE] {perm.name}: equip cost "
                                  f"{cost_str} → {_new_cost or '{0}'} (reducers on battlefield)")
                            cost_str = _new_cost
                mana_cost = _activation_mana_cost(cost_str)
                if mana_cost:
                    # June 11 live retest: equip used an amount-only loop that
                    # ignored colored requirements and multi-mana rocks. Route
                    # activation payment through the same color-aware engine as
                    # spell casting instead.
                    if not player.tap_sources_for_cost(mana_cost, game=game):
                        print(f"[ACTIVATE-CLAUDE] {perm.name}: can't pay {mana_cost}")
                        return None
                    print(f"[ACTIVATE-CLAUDE] Paid {mana_cost} for {perm.name} ability")

                # NOTE: "Pay N life" cost is handled below at the [ACTIVATE-COST]
                # block, AFTER tap/sacrifice. Don't deduct life here — doing so
                # used to charge fetchlands twice (once here, once below) per
                # the Apr 29 audit. CR 117.6 lets costs pay in any order, so
                # paying after tap/sac is fine for fetchlands.

                # Tap if needed
                if ability['needs_tap']:
                    perm.tapped = True
                if counter_cost_match:
                    counter_type = counter_cost_match.group(1).strip()
                    perm.counters[counter_type] -= 1
                    print(f"[ACTIVATE-COST] {perm.name} removes a {counter_type} counter")

                # Process sacrifice/exile costs BEFORE effect execution
                cost_lower = ability['cost'].lower()
                perm_name_lower = perm.name.lower()
                sacrificed_self = False
                exiled_self = False
                # June 10 (C2): power-referencing effects (Altar of Dementia
                # mill, Greater Good draw) read the sacrificed creature —
                # capture it and its power BEFORE it leaves the battlefield.
                sacrificed_cost_card = None
                sac_power_snapshot = 0

                if 'sacrifice' in cost_lower and (perm_name_lower in cost_lower or 'sacrifice this' in cost_lower or f'sacrifice {perm_name_lower}' in cost_lower):
                    if perm in player.battlefield:
                        game.unregister_static_effects(perm)
                        player.battlefield.remove(perm)
                        player.graveyard.append(perm)
                        sacrificed_self = True
                        print(f"[ACTIVATE-CLAUDE] Sacrificed {perm.name} as cost")
                        # June 10 audit (V15, CR 700.4): self-sacrifice IS a
                        # death — this branch fired NEITHER sacrifice nor dies
                        # triggers before.
                        if perm.is_creature():
                            try:
                                from mtg.actions import _fire_sacrifice_triggers
                                _st_msgs = _fire_sacrifice_triggers(self.rules, game, player, perm) or []
                                from mtg.triggers import queue_death
                                queue_death(game, perm, player)
                                dies_msgs, _unh = self._check_dies_triggers_sync(game, perm, player)
                                self.queue_unhandled_dies(game, perm, player, _unh)
                                _all = _st_msgs + (dies_msgs or [])
                                if _all:
                                    game._pending_messages.extend(_all)
                                    print(f"[ACTIVATE-CLAUDE] Fired {len(_all)} trigger(s) for self-sac of {perm.name}")
                            except Exception as e:
                                print(f"[ACTIVATE-CLAUDE] self-sac trigger dispatch failed: {e}")
                                from mtg.util import maybe_reraise
                                maybe_reraise(e)
                    else:
                        return None  # Can't pay sacrifice cost
                elif ('sacrifice a creature' in cost_lower
                      # July 24 batch-6 (reviewer A1, CRITICAL): "Sacrifice
                      # ANOTHER creature" (Yawgmoth) matched neither phrase,
                      # so the whole sacrifice cost silently evaporated in
                      # the AI path while the life cost still charged —
                      # Bloodghast survived two Yawgmoth activations and
                      # attacked the same turn (game_1529979552258855062).
                      # The manual !activate path (mtg/cog.py) already had
                      # this phrase — the documented two-paths divergence.
                      or 'sacrifice another creature' in cost_lower
                      or 'sacrifice a permanent' in cost_lower):
                    # "Sacrifice a creature" as cost (e.g. Altar of Dementia, Ashnod's Altar)
                    # Find a creature to sacrifice (prefer target_name if provided, else weakest)
                    sac_target = None
                    if target_name:
                        sac_target = player.find_card(target_name, Zone.BATTLEFIELD)
                        # June 11 audit (game 1514621888587108423): Greater
                        # Good accepted an explicitly named Pernicious Deed
                        # even though its cost requires a creature.
                        if not _satisfies_sacrifice_cost(
                                sac_target, cost_lower, game, source=perm):
                            print(f"[ACTIVATE-COST] {getattr(sac_target, 'name', target_name)} "
                                  f"cannot pay {perm.name}'s sacrifice cost")
                            sac_target = None
                    if not sac_target:
                        # Auto-select: weakest creature (or a token)
                        creatures = [c for c in player.battlefield if c.is_creature() and c.id != perm.id]
                        if not creatures:
                            print(f"[ACTIVATE-CLAUDE] No creature to sacrifice for {perm.name}")
                            return None  # Can't pay sacrifice cost
                        # Prefer tokens, then lowest power
                        tokens = [c for c in creatures if getattr(c, 'is_token', False)]
                        if tokens:
                            sac_target = tokens[0]
                        else:
                            sac_target = min(creatures, key=lambda c: c.get_effective_power(game) if hasattr(c, 'get_effective_power') else (int(c.power) if c.power and str(c.power).lstrip('-').isdigit() else 0))
                    if sac_target and sac_target in player.battlefield:
                        # June 10 (C2): snapshot BEFORE removal — anthems and
                        # equipment stop applying once it leaves play.
                        sacrificed_cost_card = sac_target
                        try:
                            sac_power_snapshot = sac_target.get_effective_power(game)
                        except (ValueError, TypeError):
                            sac_power_snapshot = 0
                        game.unregister_static_effects(sac_target)
                        player.battlefield.remove(sac_target)
                        player.graveyard.append(sac_target)
                        print(f"[ACTIVATE-CLAUDE] Sacrificed {sac_target.name} as cost for {perm.name}")
                        # [SACRIFICE-TRIGGER] Fire Korvold/Mayhem Devil/etc.
                        # for sacrifice-as-cost (fetchlands, Greater Gargadon-style).
                        # Without this, Korvold's draw side never fires when
                        # the controller cracks a fetchland.
                        try:
                            from mtg.actions import _fire_sacrifice_triggers
                            sac_trig_msgs = _fire_sacrifice_triggers(self.rules, game, player, sac_target)
                            if sac_trig_msgs:
                                if not hasattr(game, '_pending_messages'):
                                    game._pending_messages = []
                                game._pending_messages.extend(sac_trig_msgs)
                            # June 10 audit (V15, CR 700.4): sacrifice IS a
                            # death — fire dies triggers too (Bastion of
                            # Remembrance never fired on Altar of Dementia
                            # sacs while every combat death fired it).
                            if sac_target.is_creature():
                                from mtg.triggers import queue_death
                                queue_death(game, sac_target, player)
                                dies_msgs, _unh = self._check_dies_triggers_sync(game, sac_target, player)
                                self.queue_unhandled_dies(game, sac_target, player, _unh)
                                if dies_msgs:
                                    game._pending_messages.extend(dies_msgs)
                                    print(f"[ACTIVATE-CLAUDE] Fired {len(dies_msgs)} dies-trigger(s) for {sac_target.name} (sac cost)")
                        except Exception as e:
                            print(f"[SAC-TRIGGER] sac-cost trigger scan failed: {e}")
                            from mtg.util import maybe_reraise
                            maybe_reraise(e)
                    else:
                        return None  # Can't pay sacrifice cost
                elif 'exile' in cost_lower and (perm_name_lower in cost_lower or 'exile this' in cost_lower or f'exile {perm_name_lower}' in cost_lower):
                    if perm in player.battlefield:
                        game.unregister_static_effects(perm)
                        player.battlefield.remove(perm)
                        player.exile.append(perm)
                        exiled_self = True
                        print(f"[ACTIVATE-CLAUDE] Exiled {perm.name} as cost")
                    else:
                        return None  # Can't pay exile cost

                # Handle "Pay N life" as cost (fetchlands, Necropotence, etc.)
                import re as _re
                life_paid = 0  # Track for display in activation message
                life_match = _re.search(r'pay (\d+) life', cost_lower)
                if life_match:
                    life_cost = int(life_match.group(1))
                    if player.life >= life_cost:  # CR 119.4: can pay as long as life >= cost
                        player.life -= life_cost
                        player.record_life_loss(life_cost)
                        life_paid = life_cost
                        print(f"[ACTIVATE-COST] {player.name} pays {life_cost} life for {perm.name} (life: {player.life})")
                    else:
                        print(f"[ACTIVATE-COST] {player.name} can't pay {life_cost} life (only has {player.life})")
                        return None  # Can't pay life cost

                # Handle "Discard a card" as a cost (Anje Falkenrath, Wild
                # Mongrel). CR 601.2h — costs are paid before the ability
                # resolves. NEITHER activation path had a discard branch, while
                # the cog's own parser explicitly ACCEPTS "Discard" as a cost
                # keyword, so the ability was offered and then only its {T} was
                # charged: Anje was a commander with a free "{T}: Draw a card",
                # and because the discard never happened her own madness-untap
                # trigger could never fire either.
                _discard_match = _re.search(
                    r'discard (a|one|two|three|\d+) cards?', cost_lower)
                if _discard_match or 'discard your hand' in cost_lower:
                    if not player.hand:
                        print(f"[ACTIVATE-COST] {player.name} can't discard for "
                              f"{perm.name} — hand is empty")
                        if ability.get('needs_tap'):
                            perm.tapped = False  # roll back the tap
                        return None
                    if 'discard your hand' in cost_lower:
                        _n = len(player.hand)
                    else:
                        _raw = _discard_match.group(1)
                        _n = ({'a': 1, 'one': 1, 'two': 2, 'three': 3}.get(_raw)
                              or (int(_raw) if _raw.isdigit() else 1))
                    for _ in range(min(_n, len(player.hand))):
                        # Route through the discard action so discard triggers
                        # (Anje's untap, madness) actually fire.
                        _dm = self.rules._execute_action_on_state(game, {
                            "action": "discard", "player": player.name,
                            "card": target_name if target_name else "worst",
                        })
                        if _dm:
                            # `cost_msgs` isn't in scope this early in the
                            # branch; _pending_messages is the established way
                            # to surface a message from deep in a cost path
                            # (same idiom as the sacrifice triggers above).
                            if not hasattr(game, '_pending_messages'):
                                game._pending_messages = []
                            game._pending_messages.append(_dm)
                        target_name = None  # only the named card once
                    print(f"[ACTIVATE-COST] {player.name} discards {_n} card(s) "
                          f"for {perm.name}")

                # Handle Equip: attach this equipment to target creature you control
                if ability.get('is_equip'):
                    # Find best creature to equip (or use target_name)
                    equip_target = None
                    if target_name:
                        equip_target = player.find_card(target_name, Zone.BATTLEFIELD)
                    if not equip_target:
                        # Auto-select: biggest creature without this equipment already attached
                        best_power = -1
                        for c in player.battlefield:
                            if c.is_creature() and perm.id not in c.attachments:
                                try:
                                    p_val = c.get_effective_power(game) if hasattr(c, 'get_effective_power') else (int(c.power) if c.power else 0)
                                except (ValueError, TypeError):
                                    p_val = 0
                                if p_val > best_power:
                                    best_power = p_val
                                    equip_target = c
                    if equip_target and equip_target.is_creature():
                        # Detach from old target if any
                        if perm.attached_to:
                            for c in player.battlefield:
                                if c.id == perm.attached_to and perm.id in c.attachments:
                                    c.attachments.remove(perm.id)
                                    break
                        # Attach to new target
                        perm.attached_to = equip_target.id
                        if perm.id not in equip_target.attachments:
                            equip_target.attachments.append(perm.id)
                        # [LAYERS] Recalculate P/T after equipment change
                        game.recalculate_power_toughness()
                        print(f"[ACTIVATE-CLAUDE] Equipped {perm.name} to {equip_target.name}")
                        return f"⚔️ {player.name} equips {perm.name} to {equip_target.name}"
                    return None

                # For mana abilities: signets, talismans, Sol Ring, mana dorks, etc.
                effect_lower = ability['effect'].lower()
                if perm.name.lower() == 'wishclaw talisman':
                    return self.rules._execute_action_on_state(game, {
                        "action": "wishclaw_tutor_transfer", "player": player.name,
                        "source": perm.name})
                if perm.name.lower() == 'isochron scepter':
                    return self.rules._execute_action_on_state(game, {
                        "action": "isochron_copy", "player": player.name,
                        "source": perm.name, "target": target_name or ""})
                if 'add' in effect_lower and ('mana' in effect_lower or '{' in effect_lower):
                    # Try to parse specific colors from oracle text
                    # Signets: "Add {R}{G}" / Talismans: "Add {C}{C}" / Sol Ring: "Add {C}{C}"
                    color_map = {'W': 'W', 'U': 'U', 'B': 'B', 'R': 'R', 'G': 'G', 'C': 'C'}
                    mana_pattern = re.findall(r'\{([WUBRGC])\}', ability['effect'], re.IGNORECASE)
                    if mana_pattern:
                        # June 10 audit: "Add {W} or {U}" is a CHOICE, not both.
                        # The old loop added every symbol found, so a Celestial
                        # Colonnade tap yielded {W}{U} — two mana from one land.
                        # When the add clause contains an or-list, keep ONE
                        # symbol (the color the pool currently has least of).
                        _add_clause = ability['effect']
                        _m_add = re.search(r'add [^.\n]*', _add_clause, re.IGNORECASE)
                        if _m_add:
                            _add_clause = _m_add.group(0)
                        if re.search(r'\}\s*(?:,\s*)?or\s*\{', _add_clause, re.IGNORECASE):
                            _chosen = min((cc.upper() for cc in mana_pattern),
                                          key=lambda cc: player.mana_pool.get(cc, 0))
                            print(f"[ACTIVATE-MANA] {perm.name}: or-choice → {{{_chosen}}}")
                            mana_pattern = [_chosen]
                        added_colors = []
                        for color_char in mana_pattern:
                            c = color_char.upper()
                            if c in color_map:
                                player.mana_pool[c] = player.mana_pool.get(c, 0) + 1
                                added_colors.append(f"{{{c}}}")
                        if added_colors:
                            mana_str = ''.join(added_colors)
                            print(f"[ACTIVATE-MANA] {perm.name}: added {mana_str} to {player.name}'s pool")
                            return f"{player.name} activates {perm.name}, adds {mana_str}"

                    # "Add one mana of any color" — default to most-needed color
                    if 'any color' in effect_lower or 'any one color' in effect_lower:
                        # Pick the color the player has least of (heuristic)
                        player.mana_pool['C'] = player.mana_pool.get('C', 0) + 1
                        print(f"[ACTIVATE-MANA] {perm.name}: added {{any}} to {player.name}'s pool")
                        return f"{player.name} activates {perm.name}, adds mana of any color"

                    # Selvala-type: "Add X mana where X = greatest power among creatures"
                    if 'greatest power' in effect_lower or 'power among' in effect_lower:
                        max_power = 0
                        for c in player.battlefield:
                            if c.is_creature():
                                try:
                                    p = c.get_effective_power(game) if game else (int(c.power) if c.power else 0)
                                    max_power = max(max_power, p)
                                except:
                                    pass
                        if max_power > 0:
                            player.mana_pool['G'] = player.mana_pool.get('G', 0) + max_power
                            return f"{player.name} activates {perm.name}, adds {max_power} mana"
                        return f"{player.name} activates {perm.name} (no creatures for mana)"

                    # Generic fallback: add 1 colorless
                    player.mana_pool['C'] = player.mana_pool.get('C', 0) + 1
                    return f"{player.name} activates {perm.name}, adds {{C}}"

                # Handle "search your library for a land / land subtype" effects
                # Fetchlands say "Swamp or Mountain card", not "land card"
                land_subtypes = ['plains', 'island', 'swamp', 'mountain', 'forest']
                has_land_search = 'search your library' in effect_lower and (
                    'land' in effect_lower or
                    any(st in effect_lower for st in land_subtypes)
                )
                if has_land_search:
                    # [PW-STATIC] Ashiok, Dream Render: opponents can't search their library
                    blocker = self.rules._opponent_prevents_library_search(game, player)
                    if blocker:
                        print(f"[PW-STATIC] {blocker} prevents {player.name} from searching their library (activation: {perm.name})")
                        return f"🚫 {player.name} can't search their library ({blocker})"
                    ability_enters_tapped = 'tapped' in effect_lower and 'untapped' not in effect_lower
                    can_get_basic = 'basic' in effect_lower
                    # Fetchlands: extract allowed subtypes from oracle text
                    allowed_subtypes = [st for st in land_subtypes if st in effect_lower]

                    found_land = None
                    for c in player.library:
                        if c.is_land():
                            if can_get_basic and 'basic' not in (c.type_line or '').lower():
                                continue
                            # Fetchland subtype filter: land must have one of the allowed subtypes
                            if allowed_subtypes:
                                type_lower = (c.type_line or '').lower()
                                if not any(st in type_lower for st in allowed_subtypes):
                                    continue
                            found_land = c
                            break

                    messages = []
                    life_note = f" (paid {life_paid} life, {player.life} life)" if life_paid > 0 else ""
                    if sacrificed_self:
                        messages.append(f"💀 **{perm.name}** sacrificed{life_note}")
                    if exiled_self:
                        messages.append(f"📤 **{perm.name}** exiled (cost)")

                    if found_land:
                        player.library.remove(found_land)
                        # Check land's own ETB-tapped + replacement effects, OR ability-level tapped
                        land_tapped, land_etb_note = self.rules._check_enters_tapped(game, found_land, player)
                        enters_tapped = ability_enters_tapped or land_tapped
                        found_land.tapped = enters_tapped
                        found_land.entered_this_turn = True
                        player.battlefield.append(found_land)
                        tapped_str = " tapped" if enters_tapped else ""
                        # Include shockland life payment in the message
                        etb_detail = land_etb_note if land_etb_note else ""
                        messages.append(f"🌍 {player.name} puts {found_land.name} onto the battlefield{tapped_str}{etb_detail}")
                        # Fire landfall triggers (Omnath, Courser of Kruphix, etc.)
                        try:
                            land_etb_msgs = self._handle_land_etb(game, player, found_land)
                            if land_etb_msgs:
                                messages.extend(land_etb_msgs)
                        except Exception as e:
                            print(f"[LANDFALL] Error in fetchland ETB: {e}")
                    else:
                        messages.append(f"⚠️ No matching land found in library")

                    import random
                    random.shuffle(player.library)
                    messages.append("📚 Library shuffled")
                    print(f"[ACTIVATE-CLAUDE] {perm.name} search-for-land resolved")
                    return "\n".join(messages)

                # Handle "search your library for an artifact card" (Inventors' Fair, Fabricate, etc.)
                has_artifact_search = 'search your library' in effect_lower and 'artifact' in effect_lower
                if has_artifact_search:
                    # [PW-STATIC] Ashiok, Dream Render: opponents can't search their library
                    blocker = self.rules._opponent_prevents_library_search(game, player)
                    if blocker:
                        print(f"[PW-STATIC] {blocker} prevents {player.name} from searching their library (activation: {perm.name})")
                        return f"🚫 {player.name} can't search their library ({blocker})"
                    import random as _rng
                    found_artifact = None
                    best_cmc = -1
                    for c in player.library:
                        type_lower = (c.type_line or '').lower()
                        if 'artifact' not in type_lower:
                            continue
                        cmc = int(c.cmc) if c.cmc else 0
                        if cmc > best_cmc:
                            found_artifact = c
                            best_cmc = cmc

                    messages = []
                    if sacrificed_self:
                        messages.append(f"💀 **{perm.name}** sacrificed (cost)")
                    if found_artifact:
                        player.library.remove(found_artifact)
                        if 'onto the battlefield' in effect_lower:
                            found_artifact.entered_this_turn = True
                            found_artifact.summoning_sick = True
                            player.battlefield.append(found_artifact)
                            messages.append(f"🔍 {player.name} searches library and puts **{found_artifact.name}** onto the battlefield")
                        else:
                            player.hand.append(found_artifact)
                            messages.append(f"🔍 {player.name} searches library and finds **{found_artifact.name}** (to hand)")
                    else:
                        messages.append(f"🔍 {player.name} searches library but finds no artifact")
                    _rng.shuffle(player.library)
                    messages.append("📚 Library shuffled")
                    print(f"[ACTIVATE-CLAUDE] {perm.name} artifact search resolved: {found_artifact.name if found_artifact else 'nothing'}")
                    return "\n".join(messages)

                effect_text = ability['effect'].lower()

                # === STONEFORGE STYLE: Put Equipment from hand onto battlefield ===
                # Stoneforge Mystic's second ability: "{1}{W}, {T}: You may put
                # an Equipment card from your hand onto the battlefield." Same
                # shape as Sneak Attack but for equipment.
                # May 14 audit: this activation was failing with "unknown reason"
                # which then caused chained equip actions in the AI plan to be
                # dropped. Voltron deck was effectively broken in autoplay.
                if ('put' in effect_text and 'equipment' in effect_text
                        and 'hand' in effect_text
                        and ('battlefield' in effect_text or 'onto' in effect_text)):
                    equipment_in_hand = [
                        c for c in player.hand
                        if 'equipment' in (getattr(c, 'type_line', '') or '').lower()
                    ]
                    if not equipment_in_hand:
                        print(f"[ACTIVATE-CLAUDE] {perm.name}: no Equipment in hand to cheat out")
                        return f"⚡ {player.name} activates {perm.name} but has no Equipment in hand"
                    # Pick the highest-CMC equipment — usually the biggest payoff
                    def _safe_cmc(c):
                        try:
                            return int(getattr(c, 'cmc', 0) or 0)
                        except (ValueError, TypeError):
                            return 0
                    target_equip = None
                    if target_name:
                        for c in equipment_in_hand:
                            if target_name.lower() in c.name.lower():
                                target_equip = c
                                break
                    if not target_equip:
                        target_equip = max(equipment_in_hand, key=_safe_cmc)
                    player.hand.remove(target_equip)
                    player.battlefield.append(target_equip)
                    target_equip.entered_this_turn = True
                    print(f"[ACTIVATE-CLAUDE] {perm.name}: cheated {target_equip.name} from hand to battlefield")
                    # July 20 batch-3 audit (reviewer V1): this path bypassed
                    # ALL entry plumbing — no static registration, no
                    # PERMANENT_ENTERED, no ETB resolution. A Stoneforged
                    # Batterskull entered with no Living Weapon Germ and was
                    # dead weight for the rest of the game
                    # (game_1528946322995150848).
                    game.register_static_keyword_grants(target_equip, player.name)
                    game.register_static_pt_effects(target_equip, player.name)
                    game.register_replacement_effects(target_equip, player.name)
                    events.emit(events.PERMANENT_ENTERED, game, card=target_equip,
                                controller=player, via="cheat_into_play", rules=self.rules)
                    from mtg.actions import _fire_noncast_battlefield_entry
                    entry_msgs = _fire_noncast_battlefield_entry(
                        self.rules, game, player, target_equip)
                    msgs = []
                    if sacrificed_self:
                        msgs.append(f"💀 **{perm.name}** sacrificed (cost)")
                    msgs.append(f"⚒️ {player.name} puts **{target_equip.name}** onto the battlefield "
                                f"(via {perm.name})")
                    msgs.extend(entry_msgs)
                    return "\n".join(msgs)

                # === SNEAK ATTACK STYLE: Put creature onto battlefield ===
                # Activated abilities like "{R}: You may put a creature card from your hand onto the battlefield."
                if 'put' in effect_text and 'creature' in effect_text and ('battlefield' in effect_text or 'onto' in effect_text) and 'hand' in effect_text:
                    creatures_in_hand = [c for c in player.hand if c.is_creature()]
                    if not creatures_in_hand:
                        print(f"[ACTIVATE-CLAUDE] {perm.name}: no creatures in hand to sneak")
                        return f"⚡ {player.name} activates {perm.name} but has no creatures in hand"
                    # Auto-pick: highest power creature (best to sneak)
                    target_creature = None
                    if target_name:
                        for c in creatures_in_hand:
                            if target_name.lower() in c.name.lower():
                                target_creature = c
                                break
                    if not target_creature:
                        def _safe_pt_sum(c):
                            try:
                                p = c.get_effective_power(game) if hasattr(c, 'get_effective_power') else 0
                            except (ValueError, TypeError):
                                p = int(c.power) if c.power and str(c.power).lstrip('-').isdigit() else 0
                            try:
                                t = c.get_effective_toughness(game) if hasattr(c, 'get_effective_toughness') else 0
                            except (ValueError, TypeError):
                                t = int(c.toughness) if c.toughness and str(c.toughness).lstrip('-').isdigit() else 0
                            return p + t
                        target_creature = max(creatures_in_hand, key=_safe_pt_sum)
                    player.hand.remove(target_creature)
                    player.battlefield.append(target_creature)
                    target_creature.entered_this_turn = True
                    if 'haste' in effect_text:
                        if 'Haste' not in target_creature.keywords:
                            target_creature.keywords.append('Haste')
                    # Mark for end-step sacrifice (Sneak Attack)
                    if 'sacrifice' in effect_text and 'end' in effect_text:
                        target_creature._sneak_attack_sac = True
                    # Slice 2b (July 21): this sneak-into-play path never
                    # emitted PERMANENT_ENTERED (an emit-side gap the parity
                    # recorder structurally couldn't see — scans without
                    # emits don't diff). The emit now drives the watcher
                    # dispatch; drain in place.
                    events.emit(events.PERMANENT_ENTERED, game,
                                card=target_creature, controller=player,
                                via="cheat_into_play", rules=self.rules)
                    from mtg.helpers import drain_pending_messages as _drain_pm
                    _sneak_watcher_msgs = _drain_pm(game)
                    print(f"[ACTIVATE-CLAUDE] {perm.name}: sneaked {target_creature.name} onto battlefield")
                    msgs = []
                    msgs.extend(_sneak_watcher_msgs)
                    if sacrificed_self:
                        msgs.append(f"💀 **{perm.name}** sacrificed (cost)")
                    msgs.append(f"🎭 {player.name} puts **{target_creature.name}** ({target_creature.power}/{target_creature.toughness}) onto the battlefield" + (" with haste" if 'haste' in effect_text else ""))
                    return "\n".join(msgs)

                # === SENSEI'S DIVINING TOP — ability 0: look at top N, reorder ===
                # May 24 audit fix: AI-side parallel of the human-path handler
                # in mtg/cog.py:_activate_permanent. Sensei's Divining Top
                # activates 1-3 times per turn in autoplay; without this
                # hardcoded handler each activation escalated to Tier 3
                # (~$0.005/call). The reorder_library action at
                # mtg/actions.py:704 has a mana-curve heuristic that picks
                # whatever's most useful for the AI's current state.
                look_reorder_match = re.search(
                    r'look at the top (?:(\w+) cards?|card) of your library(?:[,\.]| then| and).*?(?:put them back|put it back|rearrange)',
                    effect_text,
                    re.IGNORECASE,
                )
                if look_reorder_match:
                    count_word = look_reorder_match.group(1) or 'one'
                    _word_to_num = {'one': 1, 'two': 2, 'three': 3, 'four': 4, 'five': 5,
                                    'six': 6, 'seven': 7, 'eight': 8, 'nine': 9, 'ten': 10}
                    amount = _word_to_num.get(count_word.lower(), 3)
                    try:
                        amount = int(count_word)
                    except (ValueError, TypeError):
                        pass
                    reorder_msg = self.rules._execute_action_on_state(
                        game,
                        {"action": "reorder_library", "player": player.name, "amount": amount},
                    )
                    if reorder_msg:
                        msgs = []
                        if sacrificed_self:
                            msgs.append(f"💀 **{perm.name}** sacrificed (cost)")
                        if exiled_self:
                            msgs.append(f"📤 **{perm.name}** exiled (cost)")
                        msgs.append(reorder_msg)
                        print(f"[ACTIVATE-CLAUDE-LIBRARY-LOOK] {perm.name} reorders top {amount}")
                        return "\n".join(msgs)

                # === SENSEI'S DIVINING TOP — ability 2: draw, then put Top on top ===
                # Maps `{1}, {T}: Draw a card. Then put Sensei's Divining Top
                # on top of its owner's library.` Draw is the player-visible
                # effect; the put-back-on-top is what makes Top cyclic (so
                # the AI's next draw step re-draws it). Without the
                # put-back-on-top step the AI would re-tap a battlefield
                # permanent perpetually and the activation cap would kick in.
                if ('draw a card' in effect_text
                        and ('put ' + perm.name.lower() in effect_text
                             or 'put this on top' in effect_text
                             or 'put it on top of its owner' in effect_text)):
                    # Use the engine's draw helper for proper Maralen/etc. handling.
                    drawn_cards = []
                    if hasattr(self, 'draw_cards'):
                        drawn_cards = self.draw_cards(player, 1, game=game) or []
                    elif player.library:
                        # Defensive fallback: direct library pop if no draw helper.
                        c0 = player.library.pop(0)
                        player.hand.append(c0)
                        drawn_cards = [c0]
                    if perm in player.battlefield:
                        try:
                            game.unregister_static_effects(perm)
                        except Exception:
                            pass
                        player.battlefield.remove(perm)
                        # library[0] is the TOP per engine convention.
                        player.library.insert(0, perm)
                        perm.tapped = False
                        perm.entered_this_turn = False
                    drawn_count = len(drawn_cards)
                    msgs = []
                    if sacrificed_self:
                        msgs.append(f"💀 **{perm.name}** sacrificed (cost)")
                    msgs.append(
                        f"🔮 **{perm.name}**: drew {drawn_count} card, "
                        f"then put **{perm.name}** back on top of library"
                    )
                    print(f"[ACTIVATE-CLAUDE-TOP-CYCLE] {perm.name} drew 1 + returned to top of library")
                    return "\n".join(msgs)

                # === TIER 1.5: Try effect template library before falling through ===
                if HAS_EFFECT_TEMPLATES:
                    try:
                        opponent = game.players[1 - player_index]
                        ctx_dict = build_game_context(game, player, opponent, card=perm)
                        lib = get_effect_library()
                        # Apr 29 audit: event_type="activated" skips the
                        # name-keyed template (which is for ETB/static triggered
                        # abilities). Without this, activating Thassa, Deep-
                        # Dwelling's {3}{U} tap ability would run its end-step
                        # flicker template, duplicating with the actual trigger.
                        tmpl_actions, tmpl_explanation = lib.resolve_etb(
                            card_name=perm.name,
                            oracle_text=ability['effect'],
                            controller=player.name,
                            opponent=opponent.name,
                            game_context=ctx_dict,
                            event_type="activated",
                        )
                        if tmpl_actions is not None:
                            tmpl_msgs = []
                            if sacrificed_self:
                                tmpl_msgs.append(f"💀 **{perm.name}** sacrificed (cost)")
                            if exiled_self:
                                tmpl_msgs.append(f"📤 **{perm.name}** exiled (cost)")
                            # June 10 audit: sacrifice-as-cost was invisible in
                            # Discord (142 events, zero lines — creatures just
                            # vanished from the board).
                            if sacrificed_cost_card is not None:
                                tmpl_msgs.append(f"💀 **{sacrificed_cost_card.name}** sacrificed "
                                                 f"(cost for {perm.name})")
                            for act in tmpl_actions:
                                if act.get("action") != "no_action":
                                    try:
                                        m = self.rules._execute_action_on_state(game, act)
                                        if m:
                                            tmpl_msgs.append(m)
                                    except Exception as ae:
                                        print(f"[ACTIVATE-CLAUDE-TEMPLATE] Action failed: {ae}")
                            print(f"[ACTIVATE-CLAUDE-TEMPLATE] {perm.name}: {tmpl_explanation}")
                            if tmpl_msgs:
                                return "\n".join(tmpl_msgs)
                            return f"{player.name} activates {perm.name}: {tmpl_explanation}"
                    except Exception as e:
                        print(f"[ACTIVATE-CLAUDE-TEMPLATE] Error: {e}")

                # Build result message with cost info
                cost_msgs = []
                if sacrificed_self:
                    cost_msgs.append(f"💀 **{perm.name}** sacrificed")
                if exiled_self:
                    cost_msgs.append(f"📤 **{perm.name}** exiled")
                # June 10 audit: sacrifice-as-cost visibility (see template
                # branch above — same gap).
                if sacrificed_cost_card is not None:
                    cost_msgs.append(f"💀 **{sacrificed_cost_card.name}** sacrificed "
                                     f"(cost for {perm.name})")

                # === June 10 audit (C2): execute the effect before announcing ===
                # Costs are paid above (mana/tap/sacrifice/life), but the old
                # code fell through to an announce-only return — Rhys made no
                # Elves, Greater Good drew nothing, Altar of Dementia milled
                # nothing, Hidetsugu dealt nothing. One-sided cost payment is
                # state corruption. Inline imperative parsers first (free),
                # then Tier-3 escalation; the announce remains as last resort.
                effect_text2 = ability['effect'] or ''
                effect_lower2 = effect_text2.lower()
                inline_msgs = []

                # (a) "Create N P/T <desc> creature token(s)" — Rhys class.
                _tok_m = re.search(
                    r'create (a|an|one|two|three|four|five|x|\d+) (\d+)/(\d+) '
                    r'([a-z\' ]+?) creature tokens?((?: with [a-z ,]+)?)',
                    effect_lower2)
                if _tok_m:
                    _wordnum = {'a': 1, 'an': 1, 'one': 1, 'two': 2, 'three': 3,
                                'four': 4, 'five': 5}
                    _cnt = _wordnum.get(_tok_m.group(1))
                    if _cnt is None:
                        _cnt = int(_tok_m.group(1)) if _tok_m.group(1).isdigit() else 1
                    _tok_desc = _tok_m.group(4).strip()
                    _color_words = {'white', 'blue', 'black', 'red', 'green', 'colorless'}
                    _name_words = [w for w in _tok_desc.split()
                                   if w not in _color_words and w != 'and']
                    _tok_name = ' '.join(w.capitalize() for w in _name_words) or 'Token'
                    _kw_part = (_tok_m.group(5) or '').replace(' with ', '').strip()
                    _kws = [k.strip() for k in re.split(r',| and ', _kw_part) if k.strip()]
                    _tok_act = {"action": "create_token", "player": player.name,
                                "name": _tok_name, "power": int(_tok_m.group(2)),
                                "toughness": int(_tok_m.group(3)),
                                "types": f"Creature — {_tok_name}", "count": _cnt}
                    if _kws:
                        _tok_act["keywords"] = _kws
                    _tm = self.rules._execute_action_on_state(game, _tok_act)
                    if _tm:
                        inline_msgs.append(_tm)
                    print(f"[ACTIVATE-CLAUDE-INLINE] {perm.name}: token imperative parsed")

                # (b) Mill — fixed N, or "equal to the sacrificed creature's
                # power" (Altar of Dementia). Default target: opponent.
                elif 'mill' in effect_lower2:
                    _mill_n = 0
                    _mn = re.search(r'mills? (a|one|two|three|x|\d+) cards?', effect_lower2)
                    if _mn:
                        _wordnum = {'a': 1, 'one': 1, 'two': 2, 'three': 3}
                        _mill_n = _wordnum.get(_mn.group(1)) or (
                            int(_mn.group(1)) if _mn.group(1).isdigit() else 0)
                    elif ("equal to the sacrificed creature's power" in effect_lower2
                          or 'equal to its power' in effect_lower2):
                        _mill_n = sac_power_snapshot
                    if _mill_n > 0:
                        _mill_target = game.players[1 - player_index].name
                        _mm = self.rules._execute_action_on_state(game, {
                            "action": "mill", "player": _mill_target, "amount": _mill_n})
                        if _mm:
                            inline_msgs.append(_mm)
                        print(f"[ACTIVATE-CLAUDE-INLINE] {perm.name}: mill {_mill_n} → {_mill_target}")

                # (c) "Draw cards equal to its power" (Greater Good) + discard rider.
                elif 'draw cards equal to' in effect_lower2 and sac_power_snapshot > 0:
                    _dm = self.rules._execute_action_on_state(game, {
                        "action": "draw_cards", "player": player.name,
                        "amount": sac_power_snapshot})
                    if _dm:
                        inline_msgs.append(_dm)
                    _disc_m = re.search(r'discards? (a|one|two|three|\d+) cards?', effect_lower2)
                    if _disc_m:
                        _wordnum = {'a': 1, 'one': 1, 'two': 2, 'three': 3}
                        _dn = _wordnum.get(_disc_m.group(1)) or (
                            int(_disc_m.group(1)) if _disc_m.group(1).isdigit() else 0)
                        for _ in range(_dn):
                            _dmsg = self.rules._execute_action_on_state(game, {
                                "action": "discard", "player": player.name,
                                "card": "random"})
                            if _dmsg:
                                inline_msgs.append(_dmsg)
                    print(f"[ACTIVATE-CLAUDE-INLINE] {perm.name}: drew {sac_power_snapshot} (sac power)")

                # (d) Plain "Draw N card(s)" as the ENTIRE effect — Erebos, God
                # of the Dead ({1}{B}, Pay 2 life: Draw a card), Arguel's Blood
                # Fast, etc. July 23 audit (#5): escalating these to Tier 3
                # double-charged the life cost — the judge sees the full
                # "Pay N life: Draw a card" oracle in the game-state dump and
                # re-emits the life loss on top of the cost the engine already
                # paid above (game_1529674672545988631: Erebos charged 4 life).
                # Anchored to the whole effect so loots ("draw two, then
                # discard") still fall through to Tier 3.
                elif re.match(r'draw (a|one|two|three|four|five|\d+) cards?\.?\s*$',
                              effect_lower2.strip()):
                    _drm = re.match(r'draw (a|one|two|three|four|five|\d+) cards?',
                                    effect_lower2.strip())
                    _wordnum = {'a': 1, 'one': 1, 'two': 2, 'three': 3, 'four': 4, 'five': 5}
                    _dn = _wordnum.get(_drm.group(1)) or (
                        int(_drm.group(1)) if _drm.group(1).isdigit() else 1)
                    if _dn > 0:
                        _dmsg = self.rules._execute_action_on_state(game, {
                            "action": "draw_cards", "player": player.name, "amount": _dn})
                        if _dmsg:
                            inline_msgs.append(_dmsg)
                        print(f"[ACTIVATE-CLAUDE-INLINE] {perm.name}: drew {_dn} (plain draw)")

                # (e) Necropotence-class: "Exile the top card of your library
                # face down. Put that card into your hand at the beginning of
                # your next end step." July 23 audit (#7): no handler existed
                # anywhere and Tier 3 has no vocabulary for a delayed
                # exile->hand, so it returned no_action every time and the
                # ability silently became "pay 1 life for nothing" for a whole
                # game (game_1529674672545988631: six activations, zero cards).
                # The delayed-trigger scheduler already exists (Pact of
                # Negation, Mana Drain); this passes phase_of so "your next end
                # step" is owner-gated (see _process_delayed_triggers) rather
                # than firing on whichever end step comes first.
                elif ('exile the top card of your library' in effect_lower2
                      and 'into your hand' in effect_lower2
                      and 'end step' in effect_lower2):
                    if player.library:
                        _exiled = player.library.pop(0)
                        player.exile.append(_exiled)
                        self.rules._execute_action_on_state(game, {
                            "action": "schedule_delayed_trigger",
                            "trigger_at": "end_step", "turn_delay": 0,
                            # "your next end step" — gated so an instant-speed
                            # activation on the opponent's turn waits for the
                            # caster's own end step (CR 603.7).
                            "phase_of": player.name,
                            "source": perm.name,
                            "actions": [{"action": "move_card", "card": _exiled.name,
                                         "from_zone": "exile", "to_zone": "hand",
                                         # The card was exiled face down — the
                                         # return line must not name it either
                                         # (July 24 batch-6: "⏰ Necropotence:
                                         # 📦 **Toxic Deluge** → hand" leaked
                                         # hidden info to the opponent).
                                         "hide_card_name": True,
                                         "player": player.name}]})
                        # The exiled card is face down — never name it in Discord.
                        inline_msgs.append(
                            f"🕳️ **{player.name}** exiles the top card face down "
                            f"(returns to hand at the next end step)")
                        print(f"[ACTIVATE-CLAUDE-INLINE] {perm.name}: exiled "
                              f"{_exiled.name} face down → hand at next end step")
                    else:
                        inline_msgs.append(f"📍 **{perm.name}**: library is empty")

                # (f) Mishra's / Urza's Bauble: "Draw a card at the beginning of
                # the next turn's upkeep." A correct handler for exactly this
                # has existed on the MANUAL !activate path since June
                # (mtg/cog.py), but autoplay and the AI both route through this
                # executor, which had no equivalent — so the draw fell to a
                # Tier 3 that has no vocabulary for a delayed draw and returned
                # no actions 16 times in 20. Third instance of the documented
                # two-activation-paths divergence.
                # No phase_of gate: the card says "the next turn's upkeep",
                # whoever's turn that is — unlike Necropotence's "YOUR next end
                # step" above.
                if not inline_msgs and re.search(
                        r'draw a card at the beginning of the next turn', effect_lower2):
                    self.rules._execute_action_on_state(game, {
                        "action": "schedule_delayed_trigger",
                        "trigger_at": "upkeep", "turn_delay": 0, "once": True,
                        "source": perm.name,
                        "actions": [{"action": "draw_cards",
                                     "player": player.name, "amount": 1}],
                    })
                    inline_msgs.append(
                        f"🃏 **{perm.name}** schedules a card draw for next upkeep")
                    print(f"[ACTIVATE-CLAUDE-INLINE] {perm.name}: scheduled draw "
                          f"for {player.name} at next upkeep")

                if inline_msgs:
                    return "\n".join(cost_msgs + inline_msgs)

                # Tier 3: same cascade the manual !activate path has. Heartless
                # Hidetsugu rides the [RESOLVE-HIDETSUGU] short-circuit inside
                # resolve_effect, so it never actually hits the LLM.
                try:
                    _t3_desc = f"{player.name} activated {perm.name}'s ability: {effect_text2}"
                    if sacrificed_cost_card is not None:
                        _t3_desc += (f" (the sacrificed creature was "
                                     f"{sacrificed_cost_card.name}, power {sac_power_snapshot})")
                    t3_msgs, t3_actions = await self.rules.resolve_effect(
                        game, _t3_desc, source_card=perm.name, controller=player.name)
                    if t3_msgs or t3_actions:
                        print(f"[ACTIVATE-CLAUDE-TIER3] {perm.name}: resolved via judge "
                              f"({len(t3_actions or [])} action(s))")
                        _body = t3_msgs or [f"{player.name} activates {perm.name}"]
                        return "\n".join(cost_msgs + _body)
                    print(f"[ACTIVATE-CLAUDE-TIER3] {perm.name}: judge returned no actions — falling to announce")
                except Exception as e:
                    print(f"[ACTIVATE-CLAUDE-TIER3] resolve_effect failed for {perm.name}: {e}")
                    from mtg.util import maybe_reraise
                    maybe_reraise(e)

                # May 14 audit (D6): the old `[:200] + '...'` cut mid-sentence
                # for Cauldron of Souls, Avalanche Caller, etc., hiding the
                # critical second clause ("with a -1/-1 counter on it" for
                # persist). Strip reminder text (parenthesized aside) first,
                # then cut at the nearest sentence boundary under ~200 chars.
                effect_raw = ability['effect'] or ''
                # Remove parenthetical reminder text (CR: italicized rules text)
                effect_clean = re.sub(r'\s*\([^)]*\)', '', effect_raw).strip()
                if len(effect_clean) > 200:
                    # Find the last period within the first ~200 chars.
                    cut = effect_clean.rfind('. ', 0, 200)
                    if cut > 50:
                        effect_desc = effect_clean[:cut + 1]
                    else:
                        # No period — cut on word boundary.
                        cut = effect_clean.rfind(' ', 0, 197)
                        effect_desc = effect_clean[:cut if cut > 50 else 197] + ' …'
                else:
                    effect_desc = effect_clean
                result_msg = f"{player.name} activates {perm.name}: {effect_desc}"
                if cost_msgs:
                    result_msg = " | ".join(cost_msgs) + f"\n{result_msg}"
                return result_msg

        elif action_type == "resolve":
            # AI wants to resolve an unhandled ETB/trigger/effect
            # Same escalation as !judge: try resolve_effect → ask_judge_with_fix
            description = action.get("description", "")
            if not description:
                return None

            # June 10 audit (C3/V28): a resolve IMMEDIATELY following a
            # cast/activate is redundant (the cascade resolved the effects)
            # or an orphan (the cast failed) — either way it must not reach
            # Tier 3, which re-applies/invents effects (Austere Command's
            # paired resolve hallucinated "Supreme Verdict" and destroyed
            # lands). Plan-validate catches plans; this covers the inline
            # decide_action path.
            if (_prev_cast_like and _prev_cast_like.get('turn') == game.turn_number
                    and _prev_cast_like.get('type') in ('cast', 'activate')):
                print(f"[EXECUTE] Dropped resolve positionally paired with prior "
                      f"{_prev_cast_like.get('type')} of {_prev_cast_like.get('card', '?')}: "
                      f"'{description[:80]}'")
                return None

            # Find source card from the description for better context
            source_card = ""
            for card in player.battlefield:
                if card.name.lower() in description.lower():
                    source_card = card.name
                    break
            if not source_card:
                opponent = game.players[1 - player_index]
                for card in opponent.battlefield:
                    if card.name.lower() in description.lower():
                        source_card = card.name
                        break

            # May 16 audit: short-circuit if this card's ETB was already
            # resolved by a Tier 1.5 template (game._recently_resolved_etbs
            # is populated by mtg/spells.py:1761 whenever an ETB template
            # fires). The actor sometimes plans `resolve` for a Trinket Mage
            # / Mulldrifter / Solemn Simulacrum whose ETB the template
            # already fired — Tier 3 then returns 3 different no_action
            # descriptions, dodging the description-based dedup at
            # engine.py:3280. Trinket Mage in the May 15 audit produced 4
            # wasted Tier 3 calls per game from this pattern.
            if source_card:
                resolved_etbs = getattr(game, '_recently_resolved_etbs', None) or set()
                desc_lower = description.lower()
                is_etb_resolve = any(
                    phrase in desc_lower
                    for phrase in (
                        'etb', 'enters the battlefield', 'enters battlefield',
                        'when it enters', 'on entry', 'trigger searches',
                        "trigger will be added", "ability triggers",
                    )
                )
                if is_etb_resolve and source_card in resolved_etbs:
                    print(f"[AI-RESOLVE] Short-circuit: {source_card} ETB already "
                          f"template-resolved — skipping duplicate Tier 3 call")
                    return None

            try:
                messages, actions = await self.rules.resolve_effect(
                    game,
                    effect_description=description,
                    source_card=source_card,
                    controller=player.name,
                )

                if actions:
                    result_parts = list(messages) if messages else []
                    # NOTE: must not be named `events` — that shadows the
                    # module-level `from mtg import events` across ALL of
                    # _execute_action (the PERMANENT_ENTERED emit in the
                    # Stoneforge branch crashed on UnboundLocalError, 5 games
                    # in the July 21 batch). Pinned by tests/test_july21_batch_audit.py.
                    sba_events = self.check_state_based_actions(game)
                    result_parts.extend(f"⚡ {e}" for e in sba_events)

                    # Clear matching pending resolves
                    desc_lower = description.lower()
                    game.pending_resolves = [
                        pr for pr in game.pending_resolves
                        if not any(word in pr.lower() for word in desc_lower.split() if len(word) > 3)
                    ]

                    print(f"[AI-RESOLVE] Resolved via resolve_effect: {description[:100]}")
                    return "\n".join(result_parts) if result_parts else f"📜 Resolved: {description}"

                # resolve_effect returned no actions — but first check if the
                # description is a library-look effect that the engine doesn't
                # model (Rashmi reveal, scry, look at top of library). Those
                # always come back empty from Tier 3 too, so escalating to
                # judge is wasted tokens. Apr 28 audit: 8 wasted Tier 3 calls
                # per Rashmi-deck game eliminated by this shortcut.
                desc_lower_check = (description or "").lower()
                library_look_skip_phrases = (
                    "scry ", "look at the top", "look at the next",
                    "reveal the top", "rashmi", "library order",
                )
                is_library_look_skip = (
                    any(phrase in desc_lower_check for phrase in library_look_skip_phrases)
                    and "draw" not in desc_lower_check
                    and "destroy" not in desc_lower_check
                    and "exile" not in desc_lower_check
                    and "deal" not in desc_lower_check
                )
                if is_library_look_skip:
                    print(f"[AI-RESOLVE] Library-look description, skipping judge escalation: {description[:80]}")
                    return None

                # Dedup: don't send the same judge ruling twice.
                # Use first 40 chars normalized so minor description variations
                # for the same card effect are still caught by the guard.
                if not hasattr(game, '_judge_rulings_sent'):
                    game._judge_rulings_sent = set()
                desc_key = re.sub(r'\s+', ' ', description[:40]).lower().strip()
                if desc_key in game._judge_rulings_sent:
                    print(f"[AI-RESOLVE] Skipping duplicate judge escalation for: {description[:100]}")
                    return None
                game._judge_rulings_sent.add(desc_key)
                print(f"[AI-RESOLVE] resolve_effect returned no actions, escalating to judge: {description[:100]}")
                ruling = await self.rules.ask_judge_with_fix(game, description, player.name)

                # Clear matching pending resolves if judge applied changes
                if ruling and "Applied changes:" in ruling:
                    desc_lower = description.lower()
                    game.pending_resolves = [
                        pr for pr in game.pending_resolves
                        if not any(word in pr.lower() for word in desc_lower.split() if len(word) > 3)
                    ]
                    sba_events = self.check_state_based_actions(game)
                    if sba_events:
                        ruling += "\n" + "\n".join(f"⚡ {e}" for e in sba_events)
                    return ruling

                # No applied changes — suppress the bare "📜 Judge Ruling: No game
                # state change." embed (per CLAUDE.md design: console-only). The
                # original code returned the bare ruling unconditionally.
                if ruling and "No game state change" in ruling:
                    print(f"[AI-RESOLVE] Suppressed bare judge ruling for '{description[:60]}' — no state change")
                    return None
                return ruling
            except Exception as e:
                print(f"[AI-RESOLVE] Error resolving '{description}': {e}")
                return None

        elif action_type == "companion":
            # [COMPANION] Pay {3} to move companion from companion zone to hand
            card_name = action.get("card", "")
            comp_card = None
            for c in player.companion_zone:
                if c.name.lower() == card_name.lower() or not card_name:
                    comp_card = c
                    break
            if comp_card:
                available = player.available_mana()
                if available >= 3:
                    mana_to_pay = 3
                    for land in player.battlefield:
                        if land.is_land() and not land.tapped and mana_to_pay > 0:
                            land.tapped = True
                            mana_to_pay -= 1
                    player.companion_zone.remove(comp_card)
                    player.hand.append(comp_card)
                    print(f"[COMPANION] {player.name} paid {{3}} to move {comp_card.name} to hand")
                    return f"{player.name} pays {{3}} to move companion {comp_card.name} to hand"
                else:
                    print(f"[COMPANION] {player.name} can't afford {{3}} for companion (have {available})")
            else:
                print(f"[COMPANION] No companion found for '{card_name}'")

        elif action_type == "mutate":
            # [MUTATE] Cast a creature with mutate onto a non-Human creature
            card_name = action.get("card", "")
            target_name = _normalize_action_target(action) or ""
            on_top = action.get("on_top", True)
            card = player.find_card(card_name, Zone.HAND)
            if not card:
                print(f"[MUTATE] Card '{card_name}' not found in hand")
                return None
            target = player.find_card(target_name, Zone.BATTLEFIELD)
            if not target or not target.is_creature():
                print(f"[MUTATE] Invalid target '{target_name}'")
                return None
            if 'Human' in (target.type_line or ''):
                print(f"[MUTATE] Cannot mutate onto Human ({target.name})")
                return None
            player.hand.remove(card)
            if on_top:
                card.mutated_cards = [target] + target.mutated_cards
                target.mutated_cards = []
                target.mutated_under = True
                if target.oracle_text and card.oracle_text:
                    card.oracle_text = card.oracle_text + "\n" + target.oracle_text
                elif target.oracle_text:
                    card.oracle_text = target.oracle_text
                bf_idx = player.battlefield.index(target)
                player.battlefield[bf_idx] = card
            else:
                target.mutated_cards.append(card)
                card.mutated_under = True
                if card.oracle_text and target.oracle_text:
                    target.oracle_text = target.oracle_text + "\n" + card.oracle_text
                elif card.oracle_text:
                    target.oracle_text = card.oracle_text
            top_card = card if on_top else target
            pos_str = "on top of" if on_top else "under"
            print(f"[MUTATE] {card.name} mutated {pos_str} {target.name}")
            return f"{player.name} mutates {card.name} {pos_str} {target.name}"

        return None
