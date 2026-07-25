"""Autoplay loop — Claude-vs-Claude automated games for playtesting.

Six free functions extracted from MTGGameCog. Together they implement the
autoplay simulation: a "pretend human" player (Rick Deckard) plays a turn
through the human-facing code paths (play_land, cast_spell, etc.) so the
test exercises the same code a real Discord user would, while a Claude
opponent uses the AI fast-path. See CLAUDE.md "Autoplay System" for the
full design.

The user-facing Discord commands (`!autoplay`, `!autoplay-batch`,
`!autoplay-parallel`) stay in cog.py as @commands.command handlers — they
delegate to _run_single_autoplay here.

Public free functions (each takes the MTGGameCog instance as `cog`):

    _run_single_autoplay(cog, ...)              (async)
        Top-level: runs one full autoplay game. Loops calling
        _autoplay_human_turn (Rick) and engine.execute_claude_turn
        (Claude) until the game ends.

    _autoplay_human_turn(cog, ...)              (async)
        Simulates Rick's full turn: MAIN1 -> COMBAT -> MAIN2 -> END,
        calling decide_action / decide_attackers / decide_blocks AI
        functions but executing through the same human code paths a
        Discord user would trigger with !play, !attack, etc.

    _autoplay_execute_action(cog, ...)          (async)
        Single action through human code paths. Main dispatch loop body.

    _autoplay_resolve_pending_action(cog, ...)  (async)
        Auto-resolves !target/!discard-style prompts since there's no
        human to respond.

    _resolve_combat(cog, ...)                   (async)
        Combat damage resolution path used by autoplay (without ctx).

    _check_deepseek_balance(cog, ...)           (async)
        Pre-flight: verify DeepSeek API key has enough credit before
        kicking off a long autoplay run.

Extracted from mtg/cog.py during Phase 2F-cog.
"""

import asyncio, json, logging, os, random, re, time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import aiohttp, discord

from mtg.constants import Phase, Zone, COMMAND_ZONE_FORMATS, PHASE_NAMES
from mtg.helpers import _normalize_pw_ability_idx, _resolve_player_or_card_target, coerce_ai_string
from mtg.models import Card, Player, GameState
from mtg.util import GameLogger

# June 11 audit: process-wide autoplay concurrency counters. Concurrent games
# share one API adapter, so per-game stat deltas sweep up other games' tokens;
# these let the [STATS-GAME] emit label itself unreliable when that happened.
_ACTIVE_AUTOPLAY_GAMES = 0
_AUTOPLAY_GAMES_STARTED = 0
_THREAD_CREATE_LOCK = None
_THREAD_CREATE_LAST = 0.0


async def _create_autoplay_thread(channel, name):
    """Rate-limit parallel Discord thread creation and retry explicit 429s."""
    global _THREAD_CREATE_LOCK, _THREAD_CREATE_LAST
    if _THREAD_CREATE_LOCK is None:
        _THREAD_CREATE_LOCK = asyncio.Lock()
    async with _THREAD_CREATE_LOCK:
        gap = 1.0 - (time.monotonic() - _THREAD_CREATE_LAST)
        if gap > 0:
            await asyncio.sleep(gap)
        for attempt in range(4):
            try:
                thread = await channel.create_thread(
                    name=name, type=discord.ChannelType.public_thread)
                _THREAD_CREATE_LAST = time.monotonic()
                return thread
            except discord.HTTPException as exc:
                if exc.status != 429 or attempt == 3:
                    raise
                retry_after = float(getattr(exc, 'retry_after', 2.0) or 2.0)
                print(f"[AUTOPLAY-THREAD] Discord 429; retrying in {retry_after}s")
                await asyncio.sleep(retry_after)

try:
    from rules.effect_templates import get_effect_library, build_game_context
    HAS_EFFECT_TEMPLATES = True
except ImportError:
    HAS_EFFECT_TEMPLATES = False

try:
    from rules.targeting_helpers import (
        _find_any_valid_target,
        _spell_requires_targets,
    )
    HAS_TARGETING = True
except ImportError:
    HAS_TARGETING = False

# Optional: LLM adapters — _run_single_autoplay uses create_openrouter_adapter
# to construct a per-game OpenRouter client when --openrouter is passed. Set
# to None if unavailable so the `if create_openrouter_adapter` guard falls
# through gracefully.
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


def _format_blocker_list(names):
    """Collapse repeated blocker names: ['Warrior Token']*5 + ['Scute Swarm']
    becomes "5x Warrior Token + Scute Swarm". Apr 29 audit display fix.
    """
    if not names:
        return ""
    counts = {}
    order = []
    for n in names:
        if n not in counts:
            order.append(n)
            counts[n] = 0
        counts[n] += 1
    parts = []
    for n in order:
        if counts[n] > 1:
            parts.append(f"{counts[n]}x {n}")
        else:
            parts.append(n)
    return " + ".join(parts)


async def _resolve_combat(cog, ctx, game: GameState):
    """Resolve combat damage using rules engine."""
    game.phase = Phase.COMBAT_DAMAGE
    
    # Use rules engine for combat resolution (handles keywords)
    damage_msgs = cog.engine.rules.resolve_combat_damage(game)
    
    if damage_msgs:
        await ctx.send("**💥 Combat Damage:**\n" + "\n".join(f"• {m}" for m in damage_msgs))
    
    # Clear combat state
    for attacker_id in game.attackers:
        result = game.find_card_global(attacker_id)
        if result:
            attacker, _, _ = result
            attacker.attacking = False
            attacker.attacking_player = None
            attacker.blocked_by = []
    
    for player in game.players:
        for creature in player.creatures():
            creature.blocking = []
    
    game.attackers = []
    game.blockers = {}
    
    # Check state-based actions (creature deaths, etc.)
    events = cog.engine.check_state_based_actions(game)
    if events:
        await ctx.send("\n".join(events))

    # June 11 audit: Judith's dies trigger was queued during combat but not
    # drained until after main phase 2, allowing postcombat actions before a
    # trigger that should already have resolved (game 1514621840440561704).
    # Combat damage + SBAs form a priority boundary: drain before MAIN2.
    for msg in await cog.engine.drain_pending_triggers(game):
        await ctx.send(msg)
    
    # Save game state
    cog.engine.save_game(game)
    
    if game.ended:
        await ctx.send(f"🏆 **{game.players[game.winner].name} wins!**")
        cog.engine.delete_game(game.thread_id)
    else:
        # Move to main 2
        game.phase = Phase.MAIN2
        await ctx.send(f"➡️ Moving to {PHASE_NAMES[game.phase]}")

        # If it's Claude's turn (human was blocking Claude's attack),
        # continue with Claude's MAIN2 actions, then end Claude's turn
        if game.active_player.is_claude:
            post_combat = await cog.engine.continue_claude_post_combat(game)
            post_combat = cog._sanitize_action_bullets(post_combat)
            if post_combat:
                msg = "**Claude (post-combat):**\n" + "\n".join(f"• {a}" for a in post_combat)
                if len(msg) > 1900:
                    for action in post_combat:
                        await ctx.send(f"• {action[:1900]}")
                else:
                    await ctx.send(msg)

            if game.ended:
                await ctx.send(f"🏆 **{game.players[game.winner].name} wins!**")
                cog.engine.delete_game(game.thread_id)
            else:
                # End Claude's turn, pass to human
                cog.engine.end_turn(game)
                _, _p1 = cog.engine.advance_phase(game)  # UNTAP → UPKEEP
                _, _p2 = cog.engine.advance_phase(game)  # UPKEEP → DRAW (upkeep triggers here)
                _, _p3 = cog.engine.advance_phase(game)  # DRAW → MAIN1
                for _m in _p1 + _p2 + _p3:
                    await ctx.send(_m)
                # Drain sync-queued triggers via Tier 3
                for _m in await cog.engine.drain_pending_triggers(game):
                    await ctx.send(_m)
                await ctx.send(f"🔄 Turn {game.turn_number} - **{game.active_player.name}**'s turn! (Drew a card)")
                await ctx.send(embed=cog.display.create_game_embed(game))
                cog.engine.save_game(game)

# =================================================================
# AUTOPLAY — Claude vs "pretend human" for automated playtesting
# =================================================================

AUTOPLAY_DECKS = {
    # Commander decks (100 cards, singleton, 40 life)
    "surrak": "surrak_stompy",
    "aminatou": "claude_deck_aminatou",
    "baral": "claude_deck_baral",
    "rashmi": "claude_deck_rashmi",
    "mythic": "mythic_sanity",
    "mythic_madness": "mythic_madness",
    "tokens": "test_tokens_rhys",
    "graveyard": "test_graveyard_meren",
    "voltron": "test_voltron_sram",
    "aristocrats": "test_aristocrats_korvold",
    "adventure": "test_adventure_chulane",
    "madness": "test_madness_anje",
    "escape": "test_escape_kroxa",
    "transform": "test_transform_werewolves",
    "partner": "test_partner_thrasios_tymna",
    # Modern decks (60 cards, 4-of, 20 life)
    "burn": "test_burn_modern",
    "uw_control": "test_uw_control_modern",
    "jund": "test_jund_modern",
    "companion_lurrus": "test_companion_lurrus",
    # Pauper deck (60 cards, 4-of, 20 life, commons-only)
    "burn_pauper": "test_burn_pauper",
    # Limited decks (40 cards, 20 life)
    "limited_aggro": "test_limited_aggro",
    "limited_control": "test_limited_control",
    # Brawl (60 cards, singleton, 25 life)
    "brawl_omnath": "test_brawl_omnath",
    # Oathbreaker (60 cards, singleton, 20 life, signature spell)
    "oathbreaker_chandra": "test_oathbreaker_chandra",
    # Mechanic stress test decks
    "layers": "test_layers_humility",           # Humility, Painter's Servant, The Abyss, anthems, gods, P/T setting
    "replacement": "test_replacement_fog",      # Fog, damage doublers, life gain prevention
    "delve": "test_delve_improvise",            # Delve + Improvise (Modern)
    "sagas": "test_sagas_enchantress",          # Sagas + enchantress draw
    "snow": "test_snow_jorn",                   # Snow mana ({S}), snow permanents, Jorn untap trigger
    "death_replacement": "test_death_replacement", # Undying, persist, shield counters, totem armor, World Enchantment
    "aura_equipment": "test_aura_equipment_combo", # May 17: Bug A — umbras + equipment stacking on same creature
    "cascade": "test_cascade",                  # May 17: Apex Devastator + Maelstrom Wanderer + Yidris cascade pipeline
    "devotion": "test_devotion_erebos",         # May 20: Theros devotion gods (Erebos) — Layer 4 type-flip when devotion crosses 5
    "combat_keywords": "test_combat_keywords_glissa",  # May 23: Glissa + Bow of Nylea + tramplers — trample+deathtouch CR 702.19c × 702.2c
    "replacement_chain": "test_replacement_chain_gisela",  # May 23: Gisela commander + Furnace of Rath + Dictate + Fiery Emancipation — multi-replacement chain CR 615.5
    "stifle": "test_stifle_talrand",            # July 21: Talrand + 6 Stifle-shapes — [CAST-TRIGGER-PRIORITY] window (APNAP-5), pacts, FoN alternate, delve
}

# Full playtest matrix — 85 matchups across 7 phases (from CLAUDE.md)
# Each entry: (matchup_number, format, deck1, deck2, description)
AUTOPLAY_MATRIX = [
    # Phase 1: Commander — Existing Decks + Reverses (1-20)
    (1,  "commander", "surrak",  "aminatou",   "ETB damage vs blink"),
    (2,  "commander", "aminatou",    "surrak", "Blink vs ETB damage"),
    (3,  "commander", "surrak",  "baral",      "Stompy vs counterspells"),
    (4,  "commander", "baral",       "surrak", "Counterspells vs stompy"),
    (5,  "commander", "surrak",  "rashmi",     "Ramp vs cast triggers"),
    (6,  "commander", "rashmi",      "surrak", "Cast triggers vs ramp"),
    (7,  "commander", "surrak",  "mythic",     "Big creatures vs cheat-into-play"),
    (8,  "commander", "mythic",      "surrak", "Sneak Attack/PWs vs stompy"),
    (9,  "commander", "aminatou",    "baral",      "Blink vs counters"),
    (10, "commander", "baral",       "aminatou",   "Counters vs blink"),
    (11, "commander", "aminatou",    "rashmi",     "Blink vs value"),
    (12, "commander", "rashmi",      "aminatou",   "Value vs blink"),
    (13, "commander", "aminatou",    "mythic",     "Blink vs PWs + Eldrazi"),
    (14, "commander", "mythic",      "aminatou",   "PWs + Eldrazi vs blink"),
    (15, "commander", "baral",       "rashmi",     "Counter-draw vs value"),
    (16, "commander", "rashmi",      "baral",      "Value vs counter-draw"),
    (17, "commander", "baral",       "mythic",     "Counters vs Sneak Attack"),
    (18, "commander", "mythic",      "baral",      "Sneak Attack vs counters"),
    (19, "commander", "rashmi",      "mythic",     "Value vs explosiveness"),
    (20, "commander", "mythic",      "rashmi",     "Explosiveness vs value"),
    # Phase 2: Commander — New Decks vs Existing + Reverses (21-60)
    (21, "commander", "surrak",  "tokens",      "Stompy vs tokens"),
    (22, "commander", "tokens",      "surrak",  "Tokens vs stompy"),
    (23, "commander", "surrak",  "graveyard",   "ETB vs recursion"),
    (24, "commander", "graveyard",   "surrak",  "Recursion vs ETB"),
    (25, "commander", "surrak",  "voltron",     "Big creatures vs equipment"),
    (26, "commander", "voltron",     "surrak",  "Equipment vs big creatures"),
    (27, "commander", "surrak",  "aristocrats", "Stompy vs sacrifice"),
    (28, "commander", "aristocrats", "surrak",  "Sacrifice vs stompy"),
    (29, "commander", "aminatou",    "tokens",      "Blink vs tokens"),
    (30, "commander", "tokens",      "aminatou",    "Tokens vs blink"),
    (31, "commander", "aminatou",    "graveyard",   "Blink vs graveyard"),
    (32, "commander", "graveyard",   "aminatou",    "Graveyard vs blink"),
    (33, "commander", "aminatou",    "voltron",     "Blink vs equipment"),
    (34, "commander", "voltron",     "aminatou",    "Equipment vs blink"),
    (35, "commander", "aminatou",    "aristocrats", "Blink vs sacrifice"),
    (36, "commander", "aristocrats", "aminatou",    "Sacrifice vs blink"),
    (37, "commander", "baral",       "tokens",      "Counters vs tokens"),
    (38, "commander", "tokens",      "baral",       "Tokens vs counters"),
    (39, "commander", "baral",       "graveyard",   "Counters vs reanimate"),
    (40, "commander", "graveyard",   "baral",       "Reanimate vs counters"),
    (41, "commander", "baral",       "voltron",     "Counters vs equipment"),
    (42, "commander", "voltron",     "baral",       "Equipment vs counters"),
    (43, "commander", "baral",       "aristocrats", "Counters vs drain"),
    (44, "commander", "aristocrats", "baral",       "Drain vs counters"),
    (45, "commander", "rashmi",      "tokens",      "Value vs tokens"),
    (46, "commander", "tokens",      "rashmi",      "Tokens vs value"),
    (47, "commander", "rashmi",      "graveyard",   "Value vs recursion"),
    (48, "commander", "graveyard",   "rashmi",      "Recursion vs value"),
    (49, "commander", "rashmi",      "voltron",     "Value vs equipment"),
    (50, "commander", "voltron",     "rashmi",      "Equipment vs value"),
    (51, "commander", "rashmi",      "aristocrats", "Value vs sacrifice"),
    (52, "commander", "aristocrats", "rashmi",      "Sacrifice vs value"),
    (53, "commander", "mythic",      "tokens",      "Eldrazi vs tokens"),
    (54, "commander", "tokens",      "mythic",      "Tokens vs Eldrazi"),
    (55, "commander", "mythic",      "graveyard",   "Artifacts vs graveyard"),
    (56, "commander", "graveyard",   "mythic",      "Graveyard vs artifacts"),
    (57, "commander", "mythic",      "voltron",     "PWs vs equipment"),
    (58, "commander", "voltron",     "mythic",      "Equipment vs PWs"),
    (59, "commander", "mythic",      "aristocrats", "Sneak Attack vs drain"),
    (60, "commander", "aristocrats", "mythic",      "Drain vs Sneak Attack"),
    # Phase 3: Commander — New Decks vs New Decks + Reverses (61-72)
    (61, "commander", "tokens",      "graveyard",   "Tokens vs recursion"),
    (62, "commander", "graveyard",   "tokens",      "Recursion vs tokens"),
    (63, "commander", "tokens",      "voltron",     "Token swarm vs equipment"),
    (64, "commander", "voltron",     "tokens",      "Equipment vs token swarm"),
    (65, "commander", "tokens",      "aristocrats", "Tokens vs sacrifice"),
    (66, "commander", "aristocrats", "tokens",      "Sacrifice vs tokens"),
    (67, "commander", "graveyard",   "voltron",     "Recursion vs equipment"),
    (68, "commander", "voltron",     "graveyard",   "Equipment vs recursion"),
    (69, "commander", "graveyard",   "aristocrats", "Graveyard vs sacrifice"),
    (70, "commander", "aristocrats", "graveyard",   "Sacrifice vs graveyard"),
    (71, "commander", "voltron",     "aristocrats", "Equipment vs sacrifice"),
    (72, "commander", "aristocrats", "voltron",     "Sacrifice vs equipment"),
    # Phase 4: Modern (73-78)
    (73, "modern", "burn",       "uw_control", "Aggro vs wrath + counters"),
    (74, "modern", "uw_control", "burn",       "Control vs aggro"),
    (75, "modern", "burn",       "jund",       "Burn vs discard"),
    (76, "modern", "jund",       "burn",       "Discard vs burn"),
    (77, "modern", "jund",       "uw_control", "Cascade vs counters"),
    (78, "modern", "uw_control", "jund",       "Counters vs cascade"),
    # Phase 5: Limited (79)
    (79, "limited", "limited_aggro", "limited_control", "40-card decks, draw-to-empty SBA"),
    # Phase 6: Brawl + Oathbreaker (80-81)
    (80, "brawl",      "brawl_omnath",       "brawl_omnath",       "25 life, 60 singleton, landfall mirror"),
    (81, "oathbreaker", "oathbreaker_chandra", "oathbreaker_chandra", "Signature spell, PW commander"),
    # Phase 7: Format Spot-Checks (82-85)
    (82, "edh",      "surrak", "rashmi", "EDH alias = commander"),
    (83, "standard", "burn",       "uw_control", "20 life, 60 cards, no command zone"),
    (84, "legacy",   "burn",       "jund",       "Legacy format string works"),
    (85, "pauper",   "burn_pauper", "burn_pauper", "Pauper: 20 life, commons-only, suspend/landfall/alt sac costs (Fireblast, Shard Volley)"),
    # Phase 8: Mechanic-Specific Tests (86-99)
    (86,  "commander", "adventure",       "surrak",  "Adventure (human) vs stompy (AI)"),
    (87,  "commander", "surrak",      "adventure",   "Stompy (human) vs adventure (AI)"),
    (88,  "commander", "madness",         "graveyard",   "Madness (human) vs graveyard (AI) — discard synergy"),
    (89,  "commander", "graveyard",       "madness",     "Graveyard (human) vs madness (AI)"),
    (90,  "commander", "escape",          "graveyard",   "Escape (human) vs graveyard (AI) — graveyard resource"),
    (91,  "commander", "graveyard",       "escape",      "Graveyard (human) vs escape (AI)"),
    (92,  "commander", "transform",       "surrak",  "Transform/werewolves (human) vs stompy (AI)"),
    (93,  "commander", "surrak",      "transform",   "Stompy (human) vs transform (AI)"),
    (94,  "commander", "partner",         "aminatou",    "Partner (human) vs blink (AI) — multi-commander"),
    (95,  "commander", "aminatou",        "partner",     "Blink (human) vs partner (AI)"),
    (96,  "commander", "adventure",       "madness",     "Adventure (human) vs madness (AI) — alternate casting"),
    (97,  "commander", "madness",         "escape",      "Madness (human) vs escape (AI) — discard fuels graveyard"),
    (98,  "commander", "transform",       "adventure",   "Transform (human) vs adventure (AI)"),
    (99,  "modern",    "companion_lurrus", "burn",       "Companion Lurrus (human) vs burn (AI)"),
    # Phase 9: Cube Draft — full pipeline test (100)
    (100, "draft",   "test",       "",           "Full cube draft pipeline: load → pick → build → play"),

    # Phase 10: Rules Engine Stress Tests — new mechanic decks (101-112)
    (101, "commander", "layers",      "aminatou",     "Humility+anthems (human) vs blink (AI)"),
    (102, "commander", "aminatou",    "layers",       "Blink (human) vs Humility+anthems (AI)"),
    (103, "commander", "layers",      "tokens",       "P/T setting (human) vs token swarm (AI)"),
    (104, "commander", "replacement", "surrak",   "Fog+doublers (human) vs stompy (AI)"),
    (105, "commander", "surrak",  "replacement",  "Stompy (human) vs fog+doublers (AI)"),
    (106, "commander", "replacement", "aristocrats",  "Damage prevention (human) vs sacrifice (AI)"),
    (107, "modern",    "delve",       "burn",         "Delve (human) vs burn (AI)"),
    (108, "modern",    "burn",        "delve",        "Burn (human) vs delve (AI)"),
    (109, "modern",    "delve",       "uw_control",   "Delve (human) vs control (AI)"),
    (110, "commander", "sagas",       "graveyard",    "Sagas+enchantress (human) vs graveyard (AI)"),
    (111, "commander", "graveyard",   "sagas",        "Graveyard (human) vs sagas+enchantress (AI)"),
    (112, "commander", "sagas",       "layers",       "Enchantress (human) vs Humility (AI)"),
    (113, "commander", "snow",        "surrak",   "Snow mana + Jorn untap (human) vs stompy (AI)"),
    (114, "commander", "surrak",  "snow",         "Stompy (human) vs snow mana (AI)"),
    (115, "commander", "snow",        "layers",       "Snow (human) vs Humility+Painter's Servant (AI)"),
    (116, "commander", "snow",        "graveyard",    "Snow board wipes (human) vs recursion (AI)"),
    # Phase 11: Death Replacement Tests — undying, persist, shield, totem armor (117-122)
    (117, "commander", "death_replacement", "surrak",   "Undying+persist (human) vs stompy (AI)"),
    (118, "commander", "surrak",        "death_replacement", "Stompy (human) vs undying+persist (AI)"),
    (119, "commander", "death_replacement", "aristocrats",  "Undying (human) vs sacrifice (AI) — loops"),
    (120, "commander", "aristocrats",       "death_replacement", "Sacrifice (human) vs undying (AI)"),
    (121, "commander", "death_replacement", "replacement",  "Death replacement (human) vs fog+doublers (AI)"),
    (122, "commander", "death_replacement", "layers",       "Undying (human) vs Humility (AI) — interaction test"),
    # Phase 12: May 17 audit-exercise decks — verify fixes that the May 16
    # batch couldn't reach because the test inventory was missing the right
    # interactions.
    (123, "commander", "aura_equipment",    "death_replacement", "Bug A: umbras + equipment on same creature (redesigned May 20: dropped 12 off-color cards, kept Sram cantrip engine)"),
    (124, "commander", "death_replacement", "aura_equipment",    "Bug A reverse"),
    (125, "commander", "cascade",           "mythic",            "Apex Devastator + Maelstrom Wanderer cascade pipeline + [CAST-FALLTHROUGH] diagnostic (redesigned May 20: dropped 8 W-illegal cards)"),
    (126, "modern",    "burn",              "jund",              "Monastery Swiftspear Prowess oracle truncation (re-run for verification)"),
    # Phase 13: May 20 audit-exercise decks — fixed test infra + new devotion deck
    (127, "commander", "devotion",          "surrak",        "Devotion to black (Erebos type-flip) vs stompy — verifies Layer 4 type-changing as devotion crosses 5"),
    (128, "commander", "surrak",        "devotion",          "Stompy vs Erebos devotion type-flip — reverse direction"),
    (129, "commander", "devotion",          "aristocrats",       "Devotion (Erebos + Gray Merchant) vs sacrifice — drain race, exercises devotion math + dies triggers"),
    (130, "commander", "devotion",          "layers",            "Devotion gods (Erebos type-flip) vs Humility — does Humility's 'isn't a creature' interact with Erebos's 'is a creature' devotion clause?"),
    # Phase 14: May 23 audit-coverage-gap decks — close the three UNVERIFIED
    # combat-and-replacement mechanics flagged by web-Opus in the May 23 audit.
    (131, "commander", "combat_keywords",   "surrak",        "Glissa+Ohran trample+deathtouch vs stompy — exercises CR 702.19c × 702.2c (deathtouch+trample 1-damage-per-blocker assignment)"),
    (132, "commander", "surrak",        "combat_keywords",   "Stompy vs Glissa trample+deathtouch — reverse direction"),
    (133, "commander", "combat_keywords",   "aristocrats",       "Glissa trample+deathtouch vs sacrifice — exercises DT+trample against many small blockers"),
    (134, "commander", "combat_keywords",   "tokens",            "Glissa trample+deathtouch vs token swarm — deathtouch makes 1 damage lethal to each blocker; trample carries the rest"),
    (135, "commander", "replacement_chain", "surrak",        "Gisela commander + Furnace/Dictate/Fiery doublers vs stompy — exercises CR 615.5 multi-replacement controller-chooses-order"),
    (136, "commander", "surrak",        "replacement_chain", "Stompy vs Gisela's halve-damage commander + on-board doublers — reverse direction (damage to Gisela's controller halved AND doubled, order matters)"),
    (137, "commander", "replacement_chain", "aristocrats",       "Gisela halve+doublers vs Korvold sacrifice — exercises chain on damage from both directions (Korvold attacks trigger doublers; Gisela halve applies to her controller's incoming damage)"),
    (138, "commander", "layers",            "combat_keywords",   "Humility's Layer 7b vs Glissa's first-strike+deathtouch (granted) — does Humility remove DT from Glissa (yes, per Humility) so trample only matters?"),
    (139, "commander", "replacement_chain", "layers",            "Gisela's halve-damage vs Humility — Humility doesn't affect static replacement effects on permanents (CR 614 not creature-typed), so Gisela's halving still applies under Humility"),
    # Phase 15: July 21 coverage deck — the [CAST-TRIGGER-PRIORITY] window
    # (APNAP-5, May 20) has never fired in a batch because no test deck held
    # Stifle-shaped cards. Talrand's own cast trigger means every instant
    # cast with a Stifle in hand opens the window even without opponent help.
    (140, "commander", "stifle",            "rashmi",            "Stifle window vs Rashmi cast triggers — [CAST-TRIGGER-PRIORITY] + [CAST-TRIGGER-COUNTERED]; Talrand Drakes vs Rashmi value"),
    (141, "commander", "rashmi",            "stifle",            "Rashmi (human path) vs stifle deck — reverse direction; response-side stifle windows"),
    (142, "commander", "stifle",            "aminatou",          "Stifle vs Rhystic Study / Smothering Tithe — opponent-cast triggers as Stifle targets; also pacts + FoN + Spellstutter vs blink"),
    (143, "commander", "stifle",            "sagas",             "Stifle vs Sythis enchantresses — enchantment-cast triggers and saga chapters as trigger-counter targets"),
    # Phase 16: July 21 batch-4 follow-up — reverse directions for the
    # coverage decks' specialty matchups. the player's catch: the newer audit
    # decks mostly sat in the pretend-human seat (deck0) for their specialty
    # pairings, so their mechanics never exercised the AI path
    # (execute_claude_turn / decide_response / decide_attackers) against
    # those opponents. Asymmetric-path bugs (the whole reason reverses
    # exist) were invisible there. Both seats now pinned structurally in
    # tests/test_july21_coverage.py.
    (144, "commander", "aminatou",          "stifle",            "Blink (human) vs stifle deck (AI path) — reverse of 142; AI-side response windows, pacts, FoN"),
    (145, "commander", "sagas",             "stifle",            "Sythis enchantresses (human) vs stifle deck (AI path) — reverse of 143"),
    (146, "commander", "aristocrats",       "devotion",          "Sacrifice (human) vs devotion (AI path) — reverse of 129; AI-side Gray Merchant drain + devotion math"),
    (147, "commander", "layers",            "devotion",          "Humility (human) vs devotion gods (AI path) — reverse of 130"),
    (148, "commander", "aristocrats",       "combat_keywords",   "Sacrifice (human) vs Glissa trample+deathtouch (AI path) — reverse of 133; AI declares the DT+trample attacks"),
    (149, "commander", "tokens",            "combat_keywords",   "Token swarm (human) vs Glissa (AI path) — reverse of 134; AI-side CR 702.19c damage assignment"),
    (150, "commander", "aristocrats",       "replacement_chain", "Korvold sacrifice (human) vs Gisela + doublers (AI path) — reverse of 137"),
    (151, "commander", "layers",            "replacement_chain", "Humility (human) vs Gisela + doublers (AI path) — reverse of 139"),
    (152, "commander", "combat_keywords",   "layers",            "Glissa trample+deathtouch (human) vs Humility (AI path) — reverse of 138"),
]

AUTOPLAY_PHASES = {
    "phase1": (1, 20),   "phase2": (21, 60),  "phase3": (61, 72),
    "phase4": (73, 78),  "phase5": (79, 79),  "phase6": (80, 81),
    "phase7": (82, 85),  "mechanics": (86, 99), "phase8": (86, 99),
    "phase9": (100, 100), "draft": (100, 100),
    "stress": (101, 122),  # Rules engine stress tests (incl. snow + death replacement)
    "snow": (113, 116),    # Snow deck matchups
    "death": (117, 122),   # Death replacement matchups (undying, persist, shield, totem armor)
    "may17": (123, 126),   # May 17 audit-exercise decks (Bug A, cascade, prowess)
    "devotion": (127, 130),  # May 20 audit: Theros devotion gods (Erebos type-flip)
    "may20": (127, 130),   # Alias for the May 20 devotion stress decks
    "may23": (131, 139),   # May 23 audit-coverage-gap decks (trample+DT, multi-replacement chain)
    "combat_keywords": (131, 134),    # Specifically the trample+deathtouch matchups
    "replacement_chain": (135, 139),  # Specifically the multi-replacement matchups
    "stifle": (140, 145),  # July 21: [CAST-TRIGGER-PRIORITY] window (Talrand + Stifle-shapes) + both-seat reverses
    "july21": (140, 145),  # Alias for the July 21 coverage deck
    "reverses": (144, 152),  # July 21 batch-4 follow-up: AI-path reverses for the coverage decks' specialty matchups
    "all": (1, 152),
}


async def _autoplay_human_turn(cog, thread, game: GameState, player_idx: int):
    """Simulate a human player's turn using AI decisions but human code paths.

    The pretend human uses the same AI (decide_action, decide_attackers) but
    actions go through the human-like code paths (play_land + ETBs, cast_spell_async
    + SBA checks, etc.) rather than execute_claude_turn's _execute_action shortcut.
    """
    # Reset state description caches at turn boundary
    cog.engine.claude_ai._cached_state_desc = None
    cog.engine.claude_ai._cached_state_fingerprint = None
    cog.engine.claude_ai._cached_hand_desc = None
    cog.engine.claude_ai._cached_hand_hash = None
    # May 23 audit (template backlog reduction): reset the per-turn
    # already-acted card set so dedup windows are turn-scoped.
    game._recent_action_card_names = set()

    player = game.players[player_idx]
    actions_taken = []
    max_actions = 20
    max_retries = 3
    retry_count = 0
    last_error = None
    last_action_key = None
    repeat_count = 0
    # Conversation mode: maintain message history per phase
    conversation = []
    last_action_result = None

    # --- MAIN PHASE 1 ---
    # Try batch planning first (one API call for the whole phase)
    plan_used = False
    if game.phase == Phase.MAIN1 and not game.ended:
        ap_player = game.players[player_idx]
        has_hand = bool(ap_player.hand)
        has_pending = bool(getattr(game, 'pending_resolves', None))
        if not has_hand and not has_pending:
            has_activatable = any(
                hasattr(c, 'activated_abilities') and c.activated_abilities and not c.tapped
                for c in ap_player.battlefield if not c.is_land()
            )
            has_fetchlands = any(
                not c.tapped and c.is_land() and 'search your library' in (c.oracle_text or '').lower()
                and 'sacrifice' in (c.oracle_text or '').lower()
                for c in ap_player.battlefield
            )
            if not has_activatable and not has_fetchlands:
                print(f"[AUTOPLAY] Auto-pass MAIN1: empty hand, no activatable, no pending")
                cog.engine.advance_phase(game)
                plan_used = True
        elif not has_pending:
            plan = await cog.engine.claude_ai.plan_turn(game, player_idx,
                                                       call_source="autoplay:main1")
            plan_failed = False
            for action in plan:
                if game.ended:
                    break
                if action.get("type") == "pass":
                    cog.engine.advance_phase(game)
                    break
                result = await cog._autoplay_execute_action(thread, game, player_idx, action)
                if result:
                    actions_taken.append(result)
                    await cog._autoplay_resolve_pending_action(thread, game)
                else:
                    error = cog.engine._get_action_error(game, player_idx, action)
                    # May 20 audit (#14): structured [PLAN-REJECTED] tag — see
                    # mtg/ai_turn.py:850 for the categorization rationale. The
                    # autoplay path also takes this fallback, so mirror the
                    # instrumentation here to capture the full distribution
                    # (autoplay:main1/2 + ai_turn:main + decide_action_inline).
                    err_lower = (error or '').lower()
                    if 'mana' in err_lower:
                        reason_tag = 'mana'
                    elif 'no legal target' in err_lower or 'no valid target' in err_lower:
                        reason_tag = 'no_target'
                    elif 'not in hand' in err_lower or 'plan-stale' in err_lower:
                        reason_tag = 'plan_stale'
                    elif 'no activated' in err_lower:
                        reason_tag = 'no_activated_ability'
                    elif 'already activated' in err_lower:
                        reason_tag = 'already_activated'
                    elif 'cr 903.4' in err_lower or 'color identity' in err_lower:
                        reason_tag = 'color_identity'
                    elif 'summoning' in err_lower:
                        reason_tag = 'summoning_sick'
                    elif 'sorcery' in err_lower and 'main' in err_lower:
                        reason_tag = 'wrong_phase'
                    else:
                        reason_tag = 'other'
                    print(f"[PLAN-REJECTED] reason={reason_tag} "
                          f"action={action.get('type', '?')} "
                          f"card={action.get('card') or action.get('permanent') or '?'} "
                          f"err='{(error or '')[:120]}'")
                    print(f"[AUTOPLAY] [PLAN] Action failed ({error}), falling back to per-action")
                    plan_failed = True
                    last_error = error
                    break
            if not plan_failed:
                plan_used = True

    # Fallback: per-action loop (if plan failed or pending resolves)
    if not plan_used:
        while game.phase == Phase.MAIN1 and not game.ended and len(actions_taken) < max_actions:
            ap_player = game.players[player_idx]
            if not ap_player.hand and not getattr(game, 'pending_resolves', None):
                has_activatable = any(
                    hasattr(c, 'activated_abilities') and c.activated_abilities and not c.tapped
                    for c in ap_player.battlefield if not c.is_land()
                )
                if not has_activatable:
                    print(f"[AUTOPLAY] Auto-pass MAIN1: empty hand, no activatable, no pending")
                    break
            action, conversation = await cog.engine.claude_ai.decide_action(
                game, player_idx, last_error=last_error,
                conversation=conversation, action_result=last_action_result
            )

            # Guard against AI returning a list instead of a dict (e.g. target list)
            if not isinstance(action, dict):
                print(f"[AUTOPLAY] AI returned non-dict action ({type(action).__name__}), treating as pass: {action}")
                last_error = "Invalid action format — please return a JSON object with 'type' field, not a list."
                retry_count += 1
                if retry_count > max_retries:
                    cog.engine.advance_phase(game)
                    break
                continue

            if action.get("type", "pass") == "pass":
                cog.engine.advance_phase(game)
                break

            action_key = f"{action.get('type')}:{action.get('card', action.get('permanent', ''))}:{action.get('ability', '')}"
            if action_key == last_action_key:
                repeat_count += 1
                if repeat_count >= 3:
                    if action.get('type') == 'resolve':
                        desc = coerce_ai_string(action.get('description', ''))
                        if desc:
                            desc_lower = desc.lower()
                            game.pending_resolves = [
                                pr for pr in game.pending_resolves
                                if not any(word in str(pr).lower() for word in desc_lower.split() if len(word) > 3)
                            ]
                            print(f"[AUTOPLAY] Cleared stale pending resolve: {desc[:80]}")
                    last_error = f"You've tried '{action.get('card', action.get('permanent', ''))}' multiple times. Try something else or pass."
                    last_action_result = None
                    if repeat_count >= 5:
                        print(f"[AUTOPLAY] Repeated action limit, auto-passing MAIN1")
                        cog.engine.advance_phase(game)
                        break
                    continue
            else:
                repeat_count = 0
            last_action_key = action_key

            # [FIX-2] Per-action counterspell guard for autoplay loop
            if action.get('type') == 'cast':
                _ap_card_name = action.get('card', '')
                _ap_card_obj = next((c for c in game.players[player_idx].hand
                                     if c.name.lower() == _ap_card_name.lower()), None)
                if _ap_card_obj and not game.stack:
                    _ap_oracle = (_ap_card_obj.oracle_text or '').lower()
                    _ap_is_counter = 'counter target' in _ap_oracle and 'spell' in _ap_oracle
                    _ap_is_creature_with_etb = _ap_card_obj.is_creature() and ('enters' in _ap_oracle or 'enter' in _ap_oracle)
                    _ap_is_modal = (
                        _ap_card_obj.name in {
                            "Mystic Confluence", "Archmage's Charm", "Cryptic Command",
                            "Fuel for the Cause", "Rewind", "Absorb", "Sinister Sabotage",
                            "Sublime Epiphany", "Commit // Memory",
                        }
                        or ('•' in _ap_oracle and any(kw in _ap_oracle for kw in ['draw a card', 'return target', 'tap target']))
                    )
                    if _ap_is_counter and not _ap_is_creature_with_etb and not _ap_is_modal:
                        print(f"[PLAN-VALIDATE] Per-action: skipped counterspell {_ap_card_obj.name} with empty stack")
                        last_error = f"{_ap_card_obj.name} requires a spell on the stack to counter. Try a different action or pass."
                        continue

            result = await cog._autoplay_execute_action(thread, game, player_idx, action)
            if result:
                actions_taken.append(result)
                last_action_result = result
                last_error = None
                retry_count = 0
                await cog._autoplay_resolve_pending_action(thread, game)
            else:
                _err_preview = cog.engine._get_action_error(game, player_idx, action)
                if _err_preview and _err_preview.startswith("[PLAN-STALE]"):
                    print(f"[AUTOPLAY] {_err_preview}")
                    last_error = None
                    last_action_result = None
                    continue
                retry_count += 1
                last_error = _err_preview
                last_action_result = None
                if retry_count > max_retries:
                    print(f"[AUTOPLAY] Action FAILED (retry {max_retries}/{max_retries}): {last_error or 'no error message; action handler returned None'}")
                    cog.engine.advance_phase(game)
                    break
                print(f"[AUTOPLAY] Action FAILED (retry {retry_count}/{max_retries}): {last_error or 'no error message; action handler returned None'}")

    # --- COMBAT ---
    # Two passes to get from MAIN1 exit to DECLARE_ATTACKERS (like human !pass !pass)
    if game.phase == Phase.COMBAT_BEGIN and not game.ended:
        # July 20 display audit: same dropped-return as the Claude path —
        # beginning-of-combat trigger output was invisible to players.
        _, _cb_msgs = cog.engine.advance_phase(game)  # → DECLARE_ATTACKERS
        for _m in (_cb_msgs or []):
            await cog._autoplay_send(thread, _m)

    if game.phase == Phase.DECLARE_ATTACKERS and not game.ended:
        attacker_names = await cog.engine.claude_ai.decide_attackers(game, player_idx)
        attacked = []

        if attacker_names:
            used_ids = set()
            for name in attacker_names:
                card = None
                for c in player.get_zone(Zone.BATTLEFIELD):
                    if (c.name.lower() == name.lower() and c.id not in used_ids
                            and c.is_creature() and not c.tapped):
                        # Validate with rules engine (like !attack does)
                        can_attack, reason = cog.engine.rules.can_attack_with(game, player, c)
                        if can_attack:
                            paid, tax_reason = cog.engine.rules.pay_attack_tax(game, player, c)
                            if paid:
                                card = c
                                break
                if card:
                    card.attacking = True
                    card.attacking_player = 1 - player_idx
                    if not card.has_vigilance():
                        cog.engine.tap_permanent(card)
                    game.attackers.append(card.id)
                    used_ids.add(card.id)
                    attacked.append(card.name + (" (vigilance)" if card.has_vigilance() else ""))
                else:
                    # June 10 round 3 (A10b follow-up): the silent drop here
                    # made legal filtering (summoning-sick twin token, all
                    # same-name instances already claimed) unauditable — a
                    # deep-dive read it as a name-dedup bug. The multiset
                    # matching above (used_ids) is correct; just say why the
                    # name didn't land.
                    print(f"[COMBAT] Proposed attacker '{name}' skipped — no untapped, "
                          f"eligible, unclaimed instance on {player.name}'s battlefield")

            if attacked:
                await cog._autoplay_send(thread, f"⚔️ **{player.name}** attacks with: {', '.join(attacked)}")

                # Process attack triggers
                trigger_msgs = cog.engine.process_attack_triggers(game, player_idx)
                for msg in trigger_msgs:
                    await cog._autoplay_send(thread, msg)

                # SBA check after attack triggers
                sba_msgs = cog.engine.check_state_based_actions(game)
                for msg in sba_msgs:
                    await cog._autoplay_send(thread, f"⚡ {msg}")

        if game.attackers and not game.ended:
            # Combat priority window: after attackers declared
            if game.stack_enabled:
                send_fn = lambda msg: cog._autoplay_send(thread, msg)
                await cog.engine._combat_priority_round(game, send_fn, "after attackers declared")
                # Check if attackers survived instant-speed removal
                game.attackers = [aid for aid in game.attackers
                                  if any(c.id == aid and c.attacking for p in game.players for c in p.battlefield)]
                if not game.attackers and not game.ended:
                    await cog._autoplay_send(thread, "⚔️ No attackers remain after priority — skipping combat.")
                    while game.phase not in [Phase.MAIN2, Phase.END, Phase.CLEANUP]:
                        cog.engine.advance_phase(game)
                    # Fall through to MAIN2

            # Opponent blocks (Claude is the opponent since pretend human is attacking)
            opponent_idx = 1 - player_idx
            opponent = game.players[opponent_idx]
            if game.attackers and not game.ended and opponent.is_claude:
                await asyncio.sleep(1)
                attacker_cards = []
                for a_id in game.attackers:
                    result = game.find_card_global(a_id)
                    if result:
                        attacker_cards.append(result[0])
                blocks = await cog.engine.claude_ai.decide_blocks(game, opponent_idx, attacker_cards)
                if blocks:
                    # May 7 audit fix #2: disambiguate same-name creatures by ID
                    # so "Plant blocks Plant" repeated 8 times becomes
                    # "Plant #1 blocks Plant #1", "Plant #2 blocks Plant #2", etc.
                    # Build per-name index from ALL combatants (both sides).
                    name_counts = {}
                    for a_id in game.attackers:
                        atk_r = game.find_card_global(a_id)
                        if atk_r:
                            name_counts[atk_r[0].name] = name_counts.get(atk_r[0].name, 0) + 1
                    for blocker_ids in blocks.values():
                        for b_id in blocker_ids or []:
                            br = game.find_card_global(b_id)
                            if br:
                                name_counts[br[0].name] = name_counts.get(br[0].name, 0) + 1
                    # Assign #N labels only when a name appears more than once.
                    name_index = {}  # card_id → label
                    name_running = {}  # name → next index
                    def _label_for(card):
                        if card.id in name_index:
                            return name_index[card.id]
                        if name_counts.get(card.name, 0) > 1:
                            idx = name_running.get(card.name, 0) + 1
                            name_running[card.name] = idx
                            label = f"{card.name} #{idx}"
                        else:
                            label = card.name
                        name_index[card.id] = label
                        return label

                    block_msgs = []
                    for attacker_id, blocker_ids in blocks.items():
                        if blocker_ids:
                            atk_result = game.find_card_global(attacker_id)
                            if not atk_result:
                                continue
                            attacker = atk_result[0]
                            attacker_label = _label_for(attacker)
                            blk_names = []
                            for blocker_id in blocker_ids:
                                blk_result = game.find_card_global(blocker_id)
                                if not blk_result:
                                    continue
                                blocker = blk_result[0]
                                # May 20 audit: validate evasion (flying/reach,
                                # menace count, tapped state). Without this an
                                # Eldrazi Spawn could legally block Baleful
                                # Strix despite Strix's flying (CR 509.1b).
                                if not blocker.can_block(attacker, game=game):
                                    print(f"[BLOCK-INVALID] {blocker.name} cannot block "
                                          f"{attacker.name} (evasion / not a creature / "
                                          f"block restriction) — skipped")
                                    continue
                                blocker.blocking.append(attacker.id)
                                attacker.blocked_by.append(blocker.id)
                                if attacker.id not in game.blockers:
                                    game.blockers[attacker.id] = []
                                game.blockers[attacker.id].append(blocker.id)
                                blk_names.append(_label_for(blocker))
                            # Apr 29 audit: collapse repeated blocker names
                            # ("Warrior Token, Warrior Token, Warrior Token, ...
                            # blocks X") into "5x Warrior Token + Y + Z blocks X".
                            # When all blockers share a (now-disambiguated) label,
                            # _format_blocker_list still collapses by exact match —
                            # which won't happen post-disambiguation, so each blocker
                            # gets its own slot. That's desired.
                            # May 24 audit fix: mirror the cog.py:3107 filter so
                            # `[BLOCK-INVALID]` evasion-rejections (e.g., Esper
                            # Sentinel proposed to block flying Sphinx of Uthuun)
                            # don't leave blk_names=[] and emit "• blocks Sphinx
                            # of Uthuun" with no blocker name (60 instances in
                            # the May 24 batch).
                            blk_names = [n for n in blk_names if n and n.strip()]
                            if not blk_names:
                                continue
                            block_msgs.append(f"{_format_blocker_list(blk_names)} blocks {attacker_label}")
                    if block_msgs:
                        await cog._autoplay_send(thread, f"🛡️ **{opponent.name}** blocks:\n" + "\n".join(f"• {b}" for b in block_msgs))
                else:
                    # May 2 audit: distinguish "no creatures available to block"
                    # from "had blockers, chose not to use them." This made the
                    # graveyard-vs-sagas game LOOK like a block bug when Sythis
                    # had been wiped and Claude truly had nothing to put in front.
                    untapped_creature_count = sum(1 for c in opponent.battlefield
                                                  if c.is_creature() and not c.tapped
                                                  and not getattr(c, '_phased_out', False))
                    if untapped_creature_count == 0:
                        await cog._autoplay_send(thread, f"🛡️ **{opponent.name}** can't block (no untapped creatures).")
                    else:
                        await cog._autoplay_send(thread, f"🛡️ **{opponent.name}** doesn't block ({untapped_creature_count} potential blocker(s) held back).")
            else:
                # Both are pretend humans — use AI for the other too
                await asyncio.sleep(1)
                attacker_cards = []
                for a_id in game.attackers:
                    result = game.find_card_global(a_id)
                    if result:
                        attacker_cards.append(result[0])
                blocks = await cog.engine.claude_ai.decide_blocks(game, opponent_idx, attacker_cards)
                if blocks:
                    for attacker_id, blocker_ids in blocks.items():
                        if blocker_ids:
                            atk_result = game.find_card_global(attacker_id)
                            if not atk_result:
                                continue
                            attacker = atk_result[0]
                            for blocker_id in blocker_ids:
                                blk_result = game.find_card_global(blocker_id)
                                if not blk_result:
                                    continue
                                blocker = blk_result[0]
                                # May 20 audit: validate evasion (flying/reach,
                                # menace count, tapped state). Without this an
                                # Eldrazi Spawn could legally block Baleful
                                # Strix despite Strix's flying (CR 509.1b).
                                if not blocker.can_block(attacker, game=game):
                                    print(f"[BLOCK-INVALID] {blocker.name} cannot block "
                                          f"{attacker.name} (evasion / not a creature / "
                                          f"block restriction) — skipped")
                                    continue
                                blocker.blocking.append(attacker.id)
                                attacker.blocked_by.append(blocker.id)
                                if attacker.id not in game.blockers:
                                    game.blockers[attacker.id] = []
                                game.blockers[attacker.id].append(blocker.id)

            # Combat priority window: after blockers declared (combat tricks!)
            if game.stack_enabled and game.attackers:
                send_fn = lambda msg: cog._autoplay_send(thread, msg)
                await cog.engine._combat_priority_round(game, send_fn, "after blockers declared")

            # Resolve combat damage
            if game.attackers and not game.ended:
                await cog._autoplay_resolve_combat(thread, game)
        elif not game.attackers and game.phase == Phase.DECLARE_ATTACKERS and not game.ended:
            # No attackers — skip to MAIN2
            while game.phase not in [Phase.MAIN2, Phase.END, Phase.CLEANUP]:
                cog.engine.advance_phase(game)

    # --- ADDITIONAL COMBAT PHASES (Moraug, Aurelia, etc.) ---
    additional_combats = getattr(game, '_additional_combats', 0)
    combat_round = 0
    while additional_combats > 0 and not game.ended:
        additional_combats -= 1
        game._additional_combats = additional_combats
        combat_round += 1
        await cog._autoplay_send(thread, f"⚔️ **Additional Combat Phase #{combat_round}!**")

        # Reset to declare attackers
        game.phase = Phase.DECLARE_ATTACKERS
        attacker_names = await cog.engine.claude_ai.decide_attackers(game, player_idx)
        attacked = []
        if attacker_names:
            used_ids = set()
            for name in attacker_names:
                card = None
                for c in player.get_zone(Zone.BATTLEFIELD):
                    if (c.name.lower() == name.lower() and c.id not in used_ids
                            and c.is_creature() and not c.tapped):
                        can_attack, reason = cog.engine.rules.can_attack_with(game, player, c)
                        if can_attack:
                            paid, tax_reason = cog.engine.rules.pay_attack_tax(game, player, c)
                            if paid:
                                card = c
                                break
                if card:
                    card.attacking = True
                    card.attacking_player = 1 - player_idx
                    if not card.has_vigilance():
                        cog.engine.tap_permanent(card)
                    game.attackers.append(card.id)
                    used_ids.add(card.id)
                    attacked.append(card.name)
            if attacked:
                await cog._autoplay_send(thread, f"⚔️ **{player.name}** attacks with: {', '.join(attacked)}")
                trigger_msgs = cog.engine.process_attack_triggers(game, player_idx)
                for msg in trigger_msgs:
                    await cog._autoplay_send(thread, msg)

        if game.attackers and not game.ended:
            # Opponent blocks
            opponent_idx = 1 - player_idx
            opponent = game.players[opponent_idx]
            attacker_cards = []
            for a_id in game.attackers:
                result = game.find_card_global(a_id)
                if result:
                    attacker_cards.append(result[0])
            blocks = await cog.engine.claude_ai.decide_blocks(game, opponent_idx, attacker_cards)
            if blocks:
                for attacker_id, blocker_ids in blocks.items():
                    if blocker_ids:
                        atk_result = game.find_card_global(attacker_id)
                        if not atk_result:
                            continue
                        attacker = atk_result[0]
                        for blocker_id in blocker_ids:
                            blk_result = game.find_card_global(blocker_id)
                            if not blk_result:
                                continue
                            blocker = blk_result[0]
                            # May 30 audit: same can_block guard as the main block paths —
                            # this additional-combat (Moraug) path was also missing it, so a
                            # non-creature god or illegal blocker could slip in here too
                            # (CR 509.1a/b). Mirror the guarded paths at ~727 and ~793.
                            if not blocker.can_block(attacker, game=game):
                                print(f"[BLOCK-INVALID] {blocker.name} cannot block "
                                      f"{attacker.name} (evasion / not a creature / "
                                      f"block restriction) — skipped")
                                continue
                            blocker.blocking.append(attacker.id)
                            attacker.blocked_by.append(blocker.id)
                            if attacker.id not in game.blockers:
                                game.blockers[attacker.id] = []
                            game.blockers[attacker.id].append(blocker.id)
            # Resolve combat damage
            if game.attackers and not game.ended:
                await cog._autoplay_resolve_combat(thread, game)
        else:
            await cog._autoplay_send(thread, f"⚔️ No attackers for additional combat — skipping.")
        print(f"[MORAUG] Additional combat #{combat_round} complete, {additional_combats} remaining")
    # Clear additional combats counter for next turn
    game._additional_combats = 0

    # Advance to MAIN2 if we're stuck in combat phases
    if game.phase not in [Phase.MAIN2, Phase.END, Phase.CLEANUP] and not game.ended:
        while game.phase not in [Phase.MAIN2, Phase.END, Phase.CLEANUP]:
            cog.engine.advance_phase(game)

    # --- MAIN PHASE 2 ---
    if game.phase == Phase.MAIN2 and not game.ended:
        retry_count = 0
        last_error = None
        last_action_key = None
        repeat_count = 0
        conversation_m2 = []
        last_action_result_m2 = None

        # Try batch planning for MAIN2 too
        plan_used_m2 = False
        ap_player = game.players[player_idx]
        has_hand = bool(ap_player.hand)
        has_pending = bool(getattr(game, 'pending_resolves', None))
        if not has_hand and not has_pending:
            has_activatable = any(
                hasattr(c, 'activated_abilities') and c.activated_abilities and not c.tapped
                for c in ap_player.battlefield if not c.is_land()
            )
            if not has_activatable:
                print(f"[AUTOPLAY] Auto-pass MAIN2: empty hand, no activatable, no pending")
                plan_used_m2 = True
        elif not has_pending:
            plan = await cog.engine.claude_ai.plan_turn(game, player_idx,
                                                       call_source="autoplay:main2")
            plan_failed_m2 = False
            for action in plan:
                if game.ended:
                    break
                if action.get("type") == "pass":
                    break
                result = await cog._autoplay_execute_action(thread, game, player_idx, action)
                if result:
                    actions_taken.append(result)
                    await cog._autoplay_resolve_pending_action(thread, game)
                else:
                    error = cog.engine._get_action_error(game, player_idx, action)
                    print(f"[AUTOPLAY] [PLAN] MAIN2 action failed ({error}), falling back")
                    plan_failed_m2 = True
                    last_error = error
                    break
            if not plan_failed_m2:
                plan_used_m2 = True

        # Fallback per-action loop for MAIN2
        if not plan_used_m2:
            while game.phase == Phase.MAIN2 and not game.ended and len(actions_taken) < max_actions:
                ap_player = game.players[player_idx]
                if not ap_player.hand and not getattr(game, 'pending_resolves', None):
                    has_activatable = any(
                        hasattr(c, 'activated_abilities') and c.activated_abilities and not c.tapped
                        for c in ap_player.battlefield if not c.is_land()
                    )
                    if not has_activatable:
                        print(f"[AUTOPLAY] Auto-pass MAIN2: empty hand, no activatable, no pending")
                        break
                action, conversation_m2 = await cog.engine.claude_ai.decide_action(
                    game, player_idx, last_error=last_error,
                    conversation=conversation_m2, action_result=last_action_result_m2
                )

                if action.get("type", "pass") == "pass":
                    break

                action_key = f"{action.get('type')}:{action.get('card', action.get('permanent', ''))}:{action.get('ability', '')}"
                if action_key == last_action_key:
                    repeat_count += 1
                    if repeat_count >= 3:
                        if action.get('type') == 'resolve':
                            desc = coerce_ai_string(action.get('description', ''))
                            if desc:
                                desc_lower = desc.lower()
                                game.pending_resolves = [
                                    pr for pr in game.pending_resolves
                                    if not any(word in str(pr).lower() for word in desc_lower.split() if len(word) > 3)
                                ]
                                print(f"[AUTOPLAY] Cleared stale pending resolve (MAIN2): {desc[:80]}")
                        if repeat_count >= 5:
                            break
                        last_error = f"You've tried this multiple times. Try something else or pass."
                        last_action_result_m2 = None
                        continue
                else:
                    repeat_count = 0
                last_action_key = action_key

                result = await cog._autoplay_execute_action(thread, game, player_idx, action)
                if result:
                    actions_taken.append(result)
                    last_action_result_m2 = result
                    last_error = None
                    retry_count = 0
                    await cog._autoplay_resolve_pending_action(thread, game)
                else:
                    retry_count += 1
                    last_error = cog.engine._get_action_error(game, player_idx, action)
                    last_action_result_m2 = None
                    if retry_count > max_retries:
                        break

    return actions_taken


async def _autoplay_execute_action(cog, thread, game: GameState, player_idx: int, action: Dict) -> Optional[str]:
    """Execute a single action through human-like code paths.

    Mirrors what the !play, !activate, etc. command handlers do, but without
    requiring a Discord ctx object.
    """
    player = game.players[player_idx]
    # The LLM occasionally emits structured values (dict/list/int) in fields
    # the schema types as strings; every downstream consumer assumes str
    # (.strip()/.lower()), so coerce once here rather than at each use site.
    for _sfield in ("card", "permanent", "target", "description"):
        if _sfield in action and not isinstance(action[_sfield], str):
            action[_sfield] = coerce_ai_string(action[_sfield])
    # June 10 audit (C3/V28): positional cast→resolve pairing. Capture the
    # previous action's cast/activate stamp, then clear; the cast/activate
    # branches below re-stamp on entry. A `resolve` that IMMEDIATELY follows
    # a cast/activate is always dropped — on success the cascade already
    # resolved the effects (Austere Command's paired resolve ran a second
    # Tier-3 wipe that destroyed LANDS; Mind Stone's mana tap yielded a free
    # "Draw a card"), and on failure it's an orphan (May 17 rule).
    _prev_cast_like = getattr(game, '_last_exec_cast_like', None)
    game._last_exec_cast_like = None
    action_type = action.get("type")

    if action_type == "play_land":
        card_name = action.get("card")
        if not card_name:
            print(f"[AUTOPLAY] play_land action missing 'card' field: {action}")
            return None
        card = player.find_card(card_name, Zone.HAND)
        if not card:
            # [PLAN-STALE] Plan chose a land already played earlier this turn. Skip silently.
            hand_names = [c.name for c in player.hand]
            print(f"[PLAN-STALE] play_land '{card_name}' not in hand (already played?). Hand: {hand_names}")
            return None

        success, msg = cog.engine.play_land(game, player, card)
        if success:
            result_msg = f"🌍 {player.name} played **{card.name}**"
            # Include shockland life payment in Discord message
            if "paid 2 life" in msg:
                result_msg += f" (paid 2 life — life: {player.life})"
            elif "entered tapped" in msg:
                result_msg += " (entered tapped)"
            await cog._autoplay_send(thread, result_msg)

            # May 23 audit (template backlog reduction): record this card
            # name so a subsequent paired `resolve:` action that mentions
            # the same card (e.g. "Return Bloodghast from graveyard" after
            # Bloodghast's landfall already fired via [LANDFALL-RECUR])
            # gets dropped instead of escalating to Tier 3 judge.
            try:
                recent = getattr(game, '_recent_action_card_names', set())
                recent.add(card.name)
                # Also record cards that landfall triggered for (Bloodghast
                # returning from graveyard is the canonical case)
                for p in game.players:
                    for gc in list(p.graveyard) + list(p.battlefield):
                        if gc.oracle_text and 'landfall' in gc.oracle_text.lower():
                            recent.add(gc.name)
                game._recent_action_card_names = recent
            except Exception:
                pass

            # Land ETB triggers (human path)
            land_etb_msgs = cog.engine._handle_land_etb(game, player, card)
            for m in land_etb_msgs:
                await cog._autoplay_send(thread, m)

            # Global ETB triggers
            global_etb_msgs = cog.engine._handle_etb_triggers(game, player, card)
            for m in global_etb_msgs:
                await cog._autoplay_send(thread, m)

            # SBA check (human path does this after each action)
            events = cog.engine.check_state_based_actions(game)
            for e in events:
                await cog._autoplay_send(thread, f"⚡ {e}")

            return result_msg
        else:
            print(f"[AUTOPLAY] play_land failed: {msg}")

    elif action_type == "cast":
        # Accept alternative field names some models emit (spell/name)
        # so a misnamed key doesn't crash the command-zone lookup below
        # with NoneType.lower().
        card_name = action.get("card") or action.get("spell") or action.get("name")
        # June 10 (C3): stamp for positional cast→resolve pairing (see top).
        game._last_exec_cast_like = {'turn': game.turn_number, 'type': 'cast',
                                     'card': card_name or '?'}
        if not card_name:
            print(f"[AUTOPLAY] cast action missing card name (no 'card'/'spell'/'name' field): {action}")
            return None
        target_name = action.get("target")
        adventure_name = action.get("adventure")
        # Apr 30 audit: route mode='cycling' through the cycle action so the AI
        # pays the cycling cost (not the full hardcast cost) and gets the cycle
        # triggers (Shark Typhoon's X/X token).
        # May 7 audit: DeepSeek sometimes returns mode as a list for modal spells
        # (e.g. Kolaghan's Command: ['damage_to_opponent', 'discard']). Normalize
        # to a string so `.lower()` doesn't crash with AttributeError.
        _mode_val = action.get("mode")
        if isinstance(_mode_val, list):
            _mode_str = _mode_val[0] if _mode_val else ""
        elif _mode_val is None:
            _mode_str = ""
        else:
            _mode_str = str(_mode_val)
        if _mode_str.lower() == "cycling":
            print(f"[AUTOPLAY] Routing cast→cycle for {card_name} (mode='cycling')")
            cycle_action = {
                "action": "cycle",
                "player": player.name,
                "card": card_name,
                "x": action.get("X") or action.get("x") or 0,
            }
            try:
                msg = cog.engine.rules._execute_action_on_state(game, cycle_action)
                return msg
            except Exception as e:
                print(f"[AUTOPLAY] cycle action failed: {e}")
                return None
        # Defensive: AI sometimes returns target as a list or dict instead of string
        if isinstance(target_name, list):
            target_name = target_name[0] if target_name else None
            if isinstance(target_name, dict):
                target_name = target_name.get('target') or target_name.get('name') or str(target_name)
        elif isinstance(target_name, dict):
            target_name = target_name.get('target') or target_name.get('name') or str(target_name)
        # AI sometimes packs X-value into target for X-cost spells (Finale of Devastation, Devil's Play)
        if isinstance(target_name, (int, float)) and not isinstance(target_name, bool):
            if action.get("X") is None:
                action["X"] = int(target_name)
                print(f"[AUTOPLAY] Lifted int target={target_name} to X for {card_name}")
            target_name = None
        if isinstance(card_name, (list, dict)):
            card_name = str(card_name) if card_name else None

        card = player.find_card(card_name, Zone.HAND)

        # Check adventure name (like !play does)
        if not card:
            for c in player.hand:
                if c.adventure_name and c.adventure_name.lower() == card_name.lower():
                    card = c
                    adventure_name = c.adventure_name
                    break

        # Check split card half name (like !play does)
        if not card:
            for c in player.hand:
                if c.split_names:
                    for i, sname in enumerate(c.split_names):
                        if card_name.lower() == sname.lower():
                            c.cast_as_split_half = i
                            card = c
                            print(f"[SPLIT] Autoplay casting {sname} (half {i} of {c.name})")
                            break
                    if card:
                        break

        # Check exile zone (like !play does)
        from_exile = False
        if not card:
            exile_card = player.find_card(card_name, Zone.EXILE)
            if exile_card and exile_card.id in player.playable_from_exile:
                card = exile_card
                from_exile = True

        # Apr 30 audit: check graveyard for Snapcaster-granted flashback / native
        # flashback / escape. The Claude AI fast-path checks here at engine.py:2153
        # but the autoplay (Rick) path was missing it, so the AI's "FLASHBACK from
        # graveyard" castable list pointed at cards the cast resolver would never find.
        from_graveyard = False
        if not card and player.playable_from_graveyard:
            for c in player.graveyard:
                if c.id in player.playable_from_graveyard and c.name.lower() == card_name.lower():
                    card = c
                    from_graveyard = True
                    player.graveyard.remove(c)
                    player.playable_from_graveyard.remove(c.id)
                    # Pay escape exile cost if applicable
                    if c.oracle_text:
                        esc_match = re.search(
                            r'escape.{1,3}\{[^}]+\}(?:\{[^}]+\})*,?\s*exile\s+(\d+)\s+other\s+cards?\s+from\s+your\s+graveyard',
                            c.oracle_text.lower()
                        )
                        if esc_match:
                            exile_count = int(esc_match.group(1))
                            for _ in range(exile_count):
                                if player.graveyard:
                                    exiled = player.graveyard.pop()
                                    player.exile.append(exiled)
                            print(f"[AUTOPLAY-ESCAPE] {c.name} cast from graveyard, exiled {exile_count} cards")
                        else:
                            print(f"[AUTOPLAY-FLASHBACK] {c.name} cast from graveyard")
                    break

        # Check command zone (like !play does)
        from_command_zone = False
        if not card and game.format in COMMAND_ZONE_FORMATS:
            for cmd_card in player.command_zone:
                if cmd_card.name.lower() == card_name.lower() or card_name.lower() in cmd_card.name.lower():
                    card = cmd_card
                    from_command_zone = True
                    break

        if not card:
            return None

        # [OATHBREAKER] Signature spell can only be cast while oathbreaker is on battlefield
        if from_command_zone and getattr(card, 'is_signature_spell', False) and game.format == "oathbreaker":
            oathbreaker_on_board = any(c.is_commander for c in player.battlefield)
            if not oathbreaker_on_board:
                print(f"[AUTOPLAY] Skipping signature spell {card.name} — oathbreaker not on battlefield")
                return None

        # [TARGETING] Pre-cast target validation — block autoplay from casting
        # targeted spells when no legal target exists (CR 601.2c)
        if HAS_TARGETING and _spell_requires_targets(card):
            if not _find_any_valid_target(game, card, player.name):
                print(f"[TARGETING] Autoplay tried to cast {card.name} with no valid targets")
                return None

        # Set adventure flag
        if adventure_name and card.adventure_name:
            card.cast_as_adventure = True
        # Set split-half flag if AI used "adventure" key for a split card
        elif adventure_name and getattr(card, 'split_names', None):
            for i, sname in enumerate(card.split_names):
                if adventure_name.lower() == sname.lower():
                    card.cast_as_split_half = i
                    print(f"[SPLIT] Autoplay 'adventure' key routed to split half '{sname}' (half {i} of {card.name})")
                    break

        # Handle exile/command zone movement (like !play does)
        if from_exile:
            player.exile.remove(card)
            player.hand.append(card)
            player.playable_from_exile.remove(card.id)

        commander_tax = 0
        if from_command_zone:
            commander_tax = card.times_cast_from_command_zone * 2
            player.command_zone.remove(card)
            player.hand.append(card)

        # Resolve target
        target = None
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

        # Pass explicit X value from batch plan (Walking Ballista, Hangarback Walker, etc.)
        if action.get("X") is not None:
            card._x_value = int(action["X"])

        # Apr 30 audit fix #21: stash modal mode selection on card so templates
        # can read ctx['_modes']. Same convention as engine.py:_execute_action.
        modes = action.get("modes") or action.get("mode")
        if modes:
            card._modes_chosen = modes if isinstance(modes, list) else [modes]

        success, msg, effect_msgs = await cog.engine.cast_spell_async(
            game, player, card, target=target,
            additional_cost=commander_tax
        )

        # May 23 audit (CRITICAL #4): even when cast_spell_async returns
        # success=True, the spell may have been countered on the stack. Record
        # the countered status so a paired plan-resolve action can be dropped
        # rather than applying free arbitrary effects.
        game._last_cast_countered = bool(success and msg and "(countered)" in msg)

        if success:
            source = " from exile" if from_exile else (" from command zone" if from_command_zone else "")
            tax_msg = f" (paid {{{commander_tax}}} commander tax)" if commander_tax > 0 else ""
            result_msg = f"✨ {player.name} cast **{card.name}**{source}{tax_msg}"
            # May 7 audit fix #1: cast_spell_async may have already announced
            # the cast BEFORE the priority window (so the announcement appears
            # before any counterspell response). If so, skip the duplicate
            # announcement here; the tax/source info is identical anyway.
            already_announced = (
                hasattr(game, '_early_announced_casts')
                and id(card) in game._early_announced_casts
            )
            if not already_announced:
                await cog._autoplay_send(thread, result_msg)
            else:
                # Clean up so the card can be cast again later (suspend recasts, etc.)
                game._early_announced_casts.discard(id(card))
                # Still post the tax suffix as a separate line if relevant —
                # the early announcement doesn't include the paid-tax text.
                if commander_tax > 0:
                    await cog._autoplay_send(thread, f"💰 {player.name} paid {{{commander_tax}}} commander tax")

            if from_command_zone:
                card.times_cast_from_command_zone += 1

            for em in (effect_msgs or []):
                await cog._autoplay_send(thread, em)

            # SBA check (human path)
            events = cog.engine.check_state_based_actions(game)
            for e in events:
                await cog._autoplay_send(thread, f"⚡ {e}")

            if game.ended:
                return result_msg
            return result_msg
        else:
            # Reset adventure flag on failure
            if getattr(card, 'cast_as_adventure', False):
                card.cast_as_adventure = False
            # Move card back if cast failed (like !play does)
            if from_exile and card in player.hand:
                player.hand.remove(card)
                player.exile.append(card)
                player.playable_from_exile.append(card.id)
            if from_command_zone and card in player.hand:
                player.hand.remove(card)
                player.command_zone.append(card)
            print(f"[AUTOPLAY] cast failed: {msg}")
            # July 20 batch-3 audit: same stash as engine.py's _execute_action
            # cast branch — the None return discards the real reason and
            # _get_action_error's re-derivation misses aura/graveyard-target
            # failures ("unknown reason — mana looks sufficient" retry storms).
            if msg:
                game._last_cast_failure = (game.turn_number, card.name, msg)

    elif action_type == "activate":
        perm_name = action.get("permanent")
        # June 10 (C3): stamp for positional pairing (see function top).
        game._last_exec_cast_like = {'turn': game.turn_number, 'type': 'activate',
                                     'card': perm_name or '?'}
        try:
            ability_idx = int(action.get("ability", 0))
        except (ValueError, TypeError):
            ability_idx = 0
        target_name = action.get("target")

        perm = player.find_card(perm_name, Zone.BATTLEFIELD)
        if not perm:
            return None

        # May 23 audit (template backlog reduction): record this card name
        # so a subsequent paired `resolve:` action that mentions the same
        # card gets dropped. E.g. "[ACTIVATE-CLAUDE] Sacrificed Bloodghast
        # as cost for Altar of Dementia" → AI then proposes resolve:
        # "Sacrifice Bloodghast, mill 1" → judge returns no state change.
        try:
            recent = getattr(game, '_recent_action_card_names', set())
            recent.add(perm.name)
            if target_name:
                recent.add(target_name)
            game._recent_action_card_names = recent
        except Exception:
            pass

        # Planeswalker abilities (like !activate does)
        if perm.is_planeswalker() and cog.engine.planeswalker_manager:
            abilities = cog.engine.planeswalker_manager.parse_abilities(perm)
            normalized_idx = _normalize_pw_ability_idx(
                action.get("ability", ability_idx), abilities
            )
            if normalized_idx is None:
                print(f"[ACTIVATE-PW] {perm.name}: ability '{action.get('ability', ability_idx)}' "
                      f"not a valid index or loyalty cost (has {len(abilities)} abilities)")
                return None
            ability_idx = normalized_idx
            ability = abilities[ability_idx]
            can_act, reason = cog.engine.planeswalker_manager.can_activate(game, player, perm, ability_idx)
            if not can_act:
                print(f"[ACTIVATE-PW] {perm.name} activation blocked: {reason}")
                return None
            # Auto-supply targets for autoplay PW abilities that need them
            # [PW-TARGET] Targets must be player/card OBJECTS, not name strings,
            # because _execute_ability checks hasattr(target, 'life') for players
            # and hasattr(target, 'damage_marked') for creatures.
            auto_targets = None
            # If the plan provided an explicit target, try to resolve it first.
            if target_name and ability.needs_target:
                target_obj = _resolve_player_or_card_target(game, player, target_name)
                if target_obj is not None:
                    auto_targets = [target_obj]
                    tname = target_obj.name if hasattr(target_obj, 'name') else str(target_obj)
                    print(f"[PW-TARGET] Forwarding explicit target '{target_name}' → {tname} for {perm.name}")
            if auto_targets is None and ability.needs_target:
                target_desc = (ability.target_description or '').lower()
                oracle_lower = (ability.text or '').lower()
                opp_idx = 1 - game.players.index(player)
                opp = game.players[opp_idx]
                if 'player' in target_desc or 'opponent' in target_desc:
                    # Target a player — default to opponent for mill/damage, cog for draw/gain
                    # Only cog-target if the ability is purely beneficial (draw/scry/gain)
                    # and does NOT also mill/damage the target. Jace +1 mills the target
                    # AND draws for cog, so the target should be the opponent.
                    is_beneficial_only = (
                        any(kw in oracle_lower for kw in ['draw', 'scry', 'look at', 'gain'])
                        and not any(kw in oracle_lower for kw in ['mill', 'graveyard', 'damage', 'lose'])
                    )
                    if 'opponent' in target_desc:
                        # "target opponent" — must be opponent
                        auto_targets = [opp]
                        print(f"[PW-TARGET] Auto-targeting opponent {opp.name} for {perm.name} (target opponent)")
                    elif is_beneficial_only:
                        auto_targets = [player]
                        print(f"[PW-TARGET] Auto-targeting cog ({player.name}) for {perm.name} (beneficial)")
                    else:
                        auto_targets = [opp]
                        print(f"[PW-TARGET] Auto-targeting opponent ({opp.name}) for {perm.name} (mill/damage)")
                elif 'creature' in target_desc:
                    # Determine targeting preference: beneficial effects (pump/keywords) → own
                    # creatures; detrimental effects (damage/destroy/exile) → opponent's.
                    # Vivien, Arkbow Ranger +1: "gets +3/+3 and gains trample" is beneficial.
                    is_beneficial_pump = bool(
                        re.search(r'\+\d+/\+\d+', oracle_lower)
                        or any(kw in oracle_lower for kw in [
                            'trample', 'flying', 'hexproof', 'protection', 'indestructible',
                            'lifelink', 'vigilance', 'first strike', 'double strike', 'reach',
                            'regenerate', 'gets +', 'gains ', 'counter on',
                        ])
                    ) and not any(bad in oracle_lower for bad in [
                        'damage', 'destroy', 'exile', 'return to', 'sacrifice', 'tap target',
                    ])
                    own_creatures = [c for c in player.battlefield if c.is_creature()]
                    opp_creatures = [c for c in opp.battlefield if c.is_creature()]
                    if is_beneficial_pump and own_creatures:
                        # Pump/keyword grant → target own best creature (highest power)
                        best = max(own_creatures, key=lambda c: (
                            c.get_effective_power(game) if hasattr(c, 'get_effective_power') else int(c.power or 0)
                        ))
                        auto_targets = [best]
                        print(f"[PW-TARGET] Auto-targeting own creature {best.name} for {perm.name} (beneficial pump)")
                    elif opp_creatures:
                        best = max(opp_creatures, key=lambda c: (c.cmc or 0))
                        auto_targets = [best]
                        print(f"[PW-TARGET] Auto-targeting opponent creature {best.name} for {perm.name}")
                    else:
                        # Try own creatures for cog-targeting abilities (e.g. -2: sacrifice)
                        if own_creatures and any(kw in oracle_lower for kw in ['sacrifice', 'return', 'put']):
                            best = max(own_creatures, key=lambda c: (c.cmc or 0))
                            auto_targets = [best]
                            print(f"[PW-TARGET] Auto-targeting own creature {best.name} for {perm.name}")
                        elif is_beneficial_pump:
                            # Beneficial but no own creatures — try opponent's as last resort
                            print(f"[PW-TARGET] No own creatures for {perm.name} beneficial pump — skipping")
                            return None
                        else:
                            print(f"[PW-TARGET] No creature target for {perm.name} — skipping")
                            return None
                elif 'permanent' in target_desc:
                    # May 7 audit (Bug 1): respect "you own" / "you control" /
                    # "another" qualifiers in the oracle text. Aminatou's -1
                    # says "exile another target permanent you own" — picking
                    # an opponent's permanent is illegal and leads to the
                    # template fizzling after loyalty has already been paid.
                    wants_own = any(kw in oracle_lower for kw in [
                        'permanent you own', 'permanent you control',
                        'another target permanent you',
                    ])
                    is_another = 'another target' in oracle_lower
                    if wants_own:
                        # Restrict to controller's permanents, excluding self
                        # (the planeswalker activating the ability) when the
                        # ability says "another".
                        own_perms = [c for c in player.battlefield if not c.is_land()]
                        if is_another:
                            own_perms = [c for c in own_perms if c.id != perm.id]
                        if own_perms:
                            best = max(own_perms, key=lambda c: (c.cmc or 0))
                            auto_targets = [best]
                            print(f"[PW-TARGET] Auto-targeting own permanent {best.name} for {perm.name} (you-own restriction)")
                        else:
                            print(f"[PW-TARGET] No own permanent target for {perm.name} — skipping")
                            return None
                    else:
                        # Default: target an opponent's permanent — pick best non-land
                        opp_perms = [c for c in opp.battlefield if not c.is_land()]
                        if opp_perms:
                            best = max(opp_perms, key=lambda c: (c.cmc or 0))
                            auto_targets = [best]
                            print(f"[PW-TARGET] Auto-targeting permanent {best.name} for {perm.name}")
                        else:
                            print(f"[PW-TARGET] No permanent target for {perm.name} — skipping")
                            return None
                elif 'planeswalker' in target_desc:
                    # Target a planeswalker
                    opp_pws = [c for c in opp.battlefield if c.is_planeswalker()]
                    if opp_pws:
                        auto_targets = [opp_pws[0]]
                        print(f"[PW-TARGET] Auto-targeting planeswalker {opp_pws[0].name} for {perm.name}")
                    else:
                        print(f"[PW-TARGET] No planeswalker target for {perm.name} — skipping")
                        return None
                elif 'artifact' in target_desc or 'enchantment' in target_desc:
                    # Target an artifact or enchantment
                    opp_perms = [c for c in opp.battlefield
                                 if ('artifact' in target_desc and c.is_artifact())
                                 or ('enchantment' in target_desc and c.is_enchantment())]
                    if not opp_perms:
                        opp_perms = [c for c in opp.battlefield if not c.is_land() and not c.is_creature()]
                    if opp_perms:
                        best = max(opp_perms, key=lambda c: (c.cmc or 0))
                        auto_targets = [best]
                        print(f"[AUTOPLAY-PW] Auto-targeting {best.name} for {perm.name} ability {ability_idx}")
                    else:
                        print(f"[PW-TARGET] No artifact/enchantment target for {perm.name} — skipping")
                        return None
                elif ability.needs_target:
                    # [FIX-4] Fallback: uncovered target_desc — try any battlefield permanent
                    all_perms = [c for c in opp.battlefield if not c.is_land()]
                    if not all_perms:
                        all_perms = [c for c in player.battlefield if not c.is_land()]
                    if all_perms:
                        best = max(all_perms, key=lambda c: (c.cmc or 0))
                        auto_targets = [best]
                        print(f"[AUTOPLAY-PW] Auto-targeting {best.name} for {perm.name} ability {ability_idx} (fallback, target_desc='{target_desc}')")
                    else:
                        # No valid targets available — skip rather than retry with None
                        print(f"[AUTOPLAY-PW] No target found for {perm.name} ability {ability_idx} (target_desc='{target_desc}') — skipping")
                        return None
            result = await cog.engine.planeswalker_manager.activate(game, player, perm, ability_idx, auto_targets)
            if result and result.success:
                # result.messages[0] is cog-describing (has header + oracle text);
                # emit result.messages directly and skip the separate outer header.
                for msg in (result.messages or []):
                    await cog._autoplay_send(thread, msg)
                events = cog.engine.check_state_based_actions(game)
                for e in events:
                    await cog._autoplay_send(thread, f"⚡ {e}")
                return result.messages[0] if result.messages else f"🔮 {player.name} activates {perm.name}"
            # [FIX-4] If still needs_targets after auto-supply, log a real error message
            if result and result.needs_targets:
                err_msg = result.messages[0] if result.messages else f"{perm.name} ability {ability_idx} needs a target"
                print(f"[ACTIVATE-PW] {perm.name} still needs targets after auto-supply — skipping. {err_msg}")
                return None
            # May 7 audit (Bug 1): if activate() returned success=False with
            # a ❌ refund message, surface it to Discord so the player sees
            # WHY the planeswalker did nothing (and that loyalty was refunded).
            result_msgs = getattr(result, 'messages', None) if result else None
            if result_msgs and any('refund' in m.lower() or '❌' in m for m in result_msgs):
                for msg in result_msgs:
                    await cog._autoplay_send(thread, msg)
                return result_msgs[0]
            print(f"[ACTIVATE-PW] {perm.name} activate() returned failure: {result}")
            return None

        # Cross-check with XMage bridge + Python-side validation
        is_legal, reason = await cog.engine._validate_activation(game, player, perm)
        if not is_legal:
            print(f"[VALIDATE-ACTIVATE] Blocked {perm.name} in autoplay: {reason}")
            return None

        # Non-planeswalker activated abilities — delegate to full _execute_action
        # which handles life payment, sacrifice, search-library, and all effects.
        # The autoplay-specific stub was incomplete (missing fetchland search, mana costs).
        print(f"[AUTOPLAY] Delegating {perm.name} activation to _execute_action()")
        result_msg = await cog.engine._execute_action(game, player_idx, {
            "type": "activate",
            "permanent": perm_name,
            "ability": ability_idx,
            "target": target_name,
        })
        if result_msg:
            await cog._autoplay_send(thread, result_msg)
        return result_msg

    elif action_type == "tap":
        card_name = action.get("card")
        card = player.find_card(card_name, Zone.BATTLEFIELD)
        if card and cog.engine.tap_permanent(card):
            result_msg = f"🔄 {player.name} tapped **{card.name}**"
            await cog._autoplay_send(thread, result_msg)
            return result_msg

    elif action_type == "resolve":
        # AI wants to resolve an unhandled ETB/trigger/effect (like !resolve)
        description = action.get("description", "")
        if not description:
            return None

        # May 23 audit (CRITICAL #4): if the immediately prior cast was
        # countered on the stack, the paired resolve would apply the spell's
        # effects for free. Per CR 701.5a a countered spell has no effect.
        # Drop the orphan resolve and clear the flag so it doesn't bleed into
        # an unrelated later resolve.
        if getattr(game, '_last_cast_countered', False):
            print(f"[AUTOPLAY-RESOLVE] Dropped orphan resolve — prior cast was countered: '{description[:80]}'")
            game._last_cast_countered = False
            return None

        # June 10 audit (C3/V28): positional pairing — name-matching below
        # can't catch resolves whose description describes the EFFECT rather
        # than the card. Any resolve immediately following a cast/activate
        # is redundant (success) or an orphan (failure) either way.
        if (_prev_cast_like and _prev_cast_like.get('turn') == game.turn_number
                and _prev_cast_like.get('type') in ('cast', 'activate')):
            print(f"[AUTOPLAY-RESOLVE] Dropped resolve positionally paired with prior "
                  f"{_prev_cast_like.get('type')} of {_prev_cast_like.get('card', '?')} — "
                  f"the cascade already resolves its own effects: '{description[:80]}'")
            return None

        # Prevent double-resolution: if this spell/effect was already resolved
        # by the spell cascade (Tier 1/1.5/2/3), don't resolve it again
        recently_resolved = getattr(game, '_recently_resolved_spells', set())
        desc_lower = description.lower()
        for resolved_name in recently_resolved:
            if resolved_name.lower() in desc_lower:
                print(f"[AUTOPLAY-RESOLVE] Skipping duplicate resolve for '{description[:80]}' — already resolved by spell cascade")
                # Clear from pending
                game.pending_resolves = [pr for pr in game.pending_resolves if resolved_name.lower() not in pr.lower()]
                return None

        # May 23 audit (template backlog reduction): a major source of
        # `[AUTOPLAY-JUDGE] No state change` was the AI proposing
        # `resolve:` AFTER play_land or activate already triggered the
        # underlying effect (Bloodghast landfall recur, Altar of Dementia
        # sac-mill, Sun Titan attack-trigger, Aminatou bottom-target, etc.).
        # The action ran fine via [LANDFALL-RECUR] / [ACTIVATE-CLAUDE], then
        # the AI's paired resolve hit Tier 3, found no state change, and
        # logged a redundant suppression. Track recent play-land and
        # activate actions per-turn and drop resolves that mention the
        # same card.
        recent_acted_cards = getattr(game, '_recent_action_card_names', set())
        for acted_name in recent_acted_cards:
            if acted_name and acted_name.lower() in desc_lower:
                print(f"[AUTOPLAY-RESOLVE] Skipping orphan resolve — '{acted_name}' "
                      f"action already fired this turn: '{description[:80]}'")
                return None
        # Also: certain description verbs are almost always redundant
        # filler ("Untap your lands", "Discard none, draw 0 cards",
        # "Attack with X to deal damage" — combat already happened).
        REDUNDANT_VERB_PREFIXES = (
            'untap your land', 'discard none', 'attack with ', 'no lands in hand',
            # June 10 (template backlog): standalone "Scry 2, keep best on
            # top" resolves — the scry already happened via the template
            # during resolution; the trailing free-text re-resolve is noise.
            'scry ',
        )
        for verb in REDUNDANT_VERB_PREFIXES:
            if desc_lower.startswith(verb):
                print(f"[AUTOPLAY-RESOLVE] Skipping no-op resolve (engine handles automatically): "
                      f"'{description[:80]}'")
                return None

        # Find source card from the description.
        # May 23 audit (CRITICAL #5): skip basic lands and prefer stack-top
        # source — Kodama's Reach searches "for a Forest" and the basic land
        # matches before the actual source spell.
        BASIC_LAND_NAMES = {"plains", "island", "swamp", "mountain", "forest", "wastes"}
        source_card = ""
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
        if not source_card:
            opponent = game.players[1 - player_idx]
            for card in opponent.battlefield:
                if card.name.lower() in BASIC_LAND_NAMES:
                    continue
                if card.name.lower() in description.lower():
                    source_card = card.name
                    break

        try:
            messages, actions = await cog.engine.rules.resolve_effect(
                game,
                effect_description=description,
                source_card=source_card,
                controller=player.name,
            )

            if actions:
                if messages:
                    for msg in messages:
                        await cog._autoplay_send(thread, msg)

                events = cog.engine.check_state_based_actions(game)
                for e in events:
                    await cog._autoplay_send(thread, f"⚡ {e}")

                # Clear matching pending resolves
                desc_lower = description.lower()
                game.pending_resolves = [
                    pr for pr in game.pending_resolves
                    if not any(word in pr.lower() for word in desc_lower.split() if len(word) > 3)
                ]

                result_msg = f"📜 {player.name} resolved: {description[:100]}"
                print(f"[AUTOPLAY-RESOLVE] {result_msg}")
                return result_msg

            # resolve_effect returned no actions — escalate to judge-with-hands
            # Dedup: don't fire the same judge query twice per game.
            # Use first 40 chars normalized (not 100) so turn-specific wording
            # differences in the same card effect don't bypass the guard.
            if not hasattr(game, '_judge_rulings_sent'):
                game._judge_rulings_sent = set()
            desc_key = re.sub(r'\s+', ' ', description[:40]).lower().strip()
            if desc_key in game._judge_rulings_sent:
                print(f"[AUTOPLAY-RESOLVE] Skipping duplicate judge escalation for: {description[:100]}")
                return None
            game._judge_rulings_sent.add(desc_key)
            print(f"[AUTOPLAY-RESOLVE] resolve_effect returned no actions, escalating to judge: {description[:100]}")
            ruling = await cog.engine.rules.ask_judge_with_fix(game, description, player.name)

            # Suppress the full ruling text from Discord — only post a clean
            # one-line summary of changes (or nothing if no state change).
            if ruling and "Applied changes:" in ruling:
                changes_line = ruling.split("Applied changes:")[-1].strip().split("\n")[0]
                await cog._autoplay_send(thread, f"📜 {description[:60]}: {changes_line}")
                desc_lower = description.lower()
                game.pending_resolves = [
                    pr for pr in game.pending_resolves
                    if not any(word in pr.lower() for word in desc_lower.split() if len(word) > 3)
                ]
                events = cog.engine.check_state_based_actions(game)
                for e in events:
                    await cog._autoplay_send(thread, f"⚡ {e}")
            else:
                # No game state change — log internally only, don't clutter Discord
                print(f"[AUTOPLAY-JUDGE] No state change for '{description[:60]}' — suppressed from Discord")

            print(f"[AUTOPLAY-RESOLVE] Judge resolved: {description[:100]}")
            return ruling
        except Exception as e:
            print(f"[AUTOPLAY-RESOLVE] Error resolving '{description}': {e}")
            return None

    elif action_type == "companion":
        # [COMPANION] Pay {3} to move companion to hand (autoplay path)
        card_name = action.get("card", "")
        comp_card = None
        for c in player.companion_zone:
            if c.name.lower() == card_name.lower() or not card_name:
                comp_card = c
                break
        if comp_card and player.available_mana() >= 3:
            mana_to_pay = 3
            for land in player.battlefield:
                if land.is_land() and not land.tapped and mana_to_pay > 0:
                    land.tapped = True
                    mana_to_pay -= 1
            player.companion_zone.remove(comp_card)
            player.hand.append(comp_card)
            result_msg = f"{player.name} pays {{3}} to move companion {comp_card.name} to hand"
            await cog._autoplay_send(thread, result_msg)
            print(f"[COMPANION] {result_msg}")
            return result_msg

    elif action_type == "mutate":
        # [MUTATE] Mutate onto a non-Human creature (autoplay path)
        result = await cog.engine._execute_action(game, player_idx, action)
        if result:
            await cog._autoplay_send(thread, result)
            return result

    return None


async def _autoplay_resolve_pending_action(cog, thread, game: GameState):
    """Auto-resolve pending actions that would normally wait for human !target/!discard commands.

    In autoplay, both players are AI-controlled so we resolve these automatically
    using simple heuristics instead of waiting for Discord commands.
    """
    if not game.pending_action:
        return

    pa = game.pending_action
    pa_type = pa.get('type', '')
    pi = pa.get('player_idx', 0)
    player = game.players[pi]

    if pa_type == 'discard_to_hand_size':
        # Already handled in the main loop, but handle here too as safety net.
        # Collect discards into a single summary line so a 31-card overflow
        # doesn't post 31 separate Discord messages.
        discard_count = pa.get('cards_to_discard', 1)
        discarded_names = []
        for _ in range(discard_count):
            if not player.hand:
                break
            non_lands = [c for c in player.hand if not c.is_land()]
            if non_lands:
                worst = max(non_lands, key=lambda c: c.cmc if c.cmc else 0)
            else:
                worst = player.hand[-1]
            player.hand.remove(worst)
            player.graveyard.append(worst)
            discarded_names.append(worst.name)
        if discarded_names:
            n = len(discarded_names)
            if n == 1:
                summary = f"📤 {player.name} discards {discarded_names[0]} to hand size"
            else:
                preview = ", ".join(discarded_names[:8])
                more = f" +{n - 8} more" if n > 8 else ""
                summary = (f"📤 {player.name} discards {n} cards to hand size: "
                           f"{preview}{more}")
            await cog._autoplay_send(thread, summary)
        game.pending_action = None

    elif pa_type == 'loot_discard_draw':
        # Discard worst card, then draw
        if player.hand:
            non_lands = [c for c in player.hand if not c.is_land()]
            if non_lands:
                worst = max(non_lands, key=lambda c: c.cmc if c.cmc else 0)
            else:
                worst = player.hand[-1]
            player.hand.remove(worst)
            player.graveyard.append(worst)
            drawn_cards = cog.engine.draw_cards(player, 1, game=game)
            source = pa.get('source', 'effect')
            draw_msg = ", draws a card" if drawn_cards else ""
            await cog._autoplay_send(thread,
                f"⚡ {source} — {player.name} discards {worst.name}{draw_msg}")
        game.pending_action = None

    elif pa_type == 'pay_life_etb':
        # Auto-pay life for Phyrexian Processor (same formula as template)
        card_name = pa.get('card_name', 'Unknown')
        # If the template already paid life (check if card has stored a token size),
        # just acknowledge it. Otherwise pay min(life-5, 10).
        card_id = pa.get('card_id', '')
        already_paid = False
        for c in player.battlefield:
            if c.id == card_id and hasattr(c, '_processor_paid'):
                already_paid = True
                break
        if not already_paid:
            pay = max(0, min(player.life - 5, 10))
            if pay > 0:
                player.life -= pay
                player.record_life_loss(pay)
                # Store token size on the card
                for c in player.battlefield:
                    if c.id == card_id:
                        c._processor_paid = pay
                        break
                await cog._autoplay_send(thread,
                    f"🤖 {player.name} pays {pay} life for {card_name} (life: {player.life})")
            else:
                await cog._autoplay_send(thread,
                    f"🤖 {player.name} pays 0 life for {card_name} (too low on life)")
        else:
            await cog._autoplay_send(thread,
                f"🤖 {card_name} life payment already resolved via template")
        game.pending_action = None

    elif pa_type == 'permanent_ability' and pa.get('effect_type') == 'sneak_creature':
        # Pick the biggest creature from hand to sneak in.
        # June 10 deep-dive: two fixes — (1) P/T are STRINGS, so the old
        # `(c.power or 0) + (c.toughness or 0)` concatenated ("7"+"7"="77"
        # outsorted "12"+"12"="1212"… actually sorted lexically wrong either
        # way) and picked Drakuseth 7/7 over Kozilek 12/12; (2) the creature
        # never got the `_sneak_attack_sac` flag (only the human !target
        # executor at mtg/engine.py sets it), so Through the Breach /
        # Sneak Attack creatures PERMANENTLY survived the end step. Also
        # grant haste — that's the whole point of the sneak.
        def _pt_int(v):
            try:
                return int(v)
            except (TypeError, ValueError):
                return 0
        creatures = [c for c in player.hand if c.is_creature()]
        if creatures:
            best = max(creatures, key=lambda c: _pt_int(c.power) + _pt_int(c.toughness))
            player.hand.remove(best)
            player.battlefield.append(best)
            best.entered_this_turn = True
            best._sneak_attack_sac = True
            if 'Haste' not in (best.temp_keywords or []):
                best.temp_keywords.append('Haste')
            await cog._autoplay_send(thread,
                f"🎭 {player.name} puts **{best.name}** ({best.power}/{best.toughness}) onto the "
                f"battlefield (haste — sacrificed at the next end step)")
        game.pending_action = None

    elif pa_type == 'planeswalker_target':
        # Pick first legal target
        legal_targets = pa.get('legal_targets', [])
        if legal_targets:
            target, desc = legal_targets[0]
            card_id = pa.get('card_id')
            ability_index = pa.get('ability_index', 0)
            card = None
            for c in player.battlefield:
                if c.id == card_id:
                    card = c
                    break
            if card and cog.engine.planeswalker_manager:
                result = await cog.engine.planeswalker_manager.activate(
                    game, player, card, ability_index, [target])
                if result and hasattr(result, 'messages'):
                    for msg in result.messages:
                        await cog._autoplay_send(thread, msg)
            await cog._autoplay_send(thread, f"🎯 Target: {desc}")
        game.pending_action = None

    elif pa_type in ('chandra_dual_target_player', 'chandra_dual_target_creature'):
        # Auto-pick first opponent / biggest creature
        if pa_type == 'chandra_dual_target_player':
            # Target the opponent
            opponent_idx = 1 - pi
            opponent = game.players[opponent_idx]
            dmg = pa.get('player_dmg', 0)
            card_id = pa.get('card_id', '')
            actual_dmg = cog.engine.rules._apply_noncombat_damage_to_player(game, opponent, dmg, "Chandra", card_id)
            await cog._autoplay_send(thread,
                f"🔥 Deals {actual_dmg} damage to {opponent.name} (Life: {opponent.life})")
        else:
            # Pick biggest creature to damage
            target_pi = pa.get('target_player_idx', 1 - pi)
            target_player = game.players[target_pi]
            creatures = [c for c in target_player.battlefield if c.is_creature()]
            dmg = pa.get('creature_dmg', 0)
            if creatures:
                biggest = max(creatures, key=lambda c: (c.toughness or 0))
                biggest.damage_marked += dmg
                await cog._autoplay_send(thread,
                    f"🔥 Deals {dmg} damage to {biggest.name}")
        game.pending_action = None

    elif pa_type == 'choose_replacement':
        # Auto-pick first effect (timestamp order, same as sync fallback)
        effects = pa.get('effects', [])
        future = pa.get('future')
        if effects:
            chosen = effects[0]
            await cog._autoplay_send(thread,
                f"⚡ Auto-chose replacement: {chosen.source_name}")
            if future and not future.done():
                future.set_result(chosen)
        game.pending_action = None

    else:
        # Unknown pending action type — clear it and log
        print(f"[AUTOPLAY] Clearing unhandled pending_action type: {pa_type}")
        game.pending_action = None


async def _run_single_autoplay(cog, channel, game_format: str, deck1_name: str, deck2_name: str,
                               matchup_label: str = None, force_claude: bool = False,
                               openrouter_model: str = None) -> dict:
    """Run a single autoplay game. Returns a result dict.

    Used by both !autoplay (single game) and !autoplay-batch (matrix).
    Creates its own thread, runs the full game, cleans up, returns outcome.

    Provider priority: --openrouter > --deepseek (default) > --claude (fallback).
    """
    import time as _time
    start_time = _time.time()
    max_turns = 70
    thread = None
    result = {"format": game_format, "deck1": deck1_name, "deck2": deck2_name,
              "outcome": "crash", "winner": None, "turns": 0,
              "p1_life": 0, "p2_life": 0, "error": None, "thread_id": None,
              "duration_seconds": 0}

    # June 11 audit: concurrency tracking so the per-game stats line can say
    # when its numbers are polluted by parallel games (all concurrent games
    # share one adapter — see the PARALLEL-MODE CAVEAT near the emit).
    global _ACTIVE_AUTOPLAY_GAMES, _AUTOPLAY_GAMES_STARTED
    global _AUTOPLAY_SWAP_DEPTH, _AUTOPLAY_TRUE_ORIGINALS
    try:
        _ACTIVE_AUTOPLAY_GAMES += 1
        _AUTOPLAY_GAMES_STARTED += 1
    except NameError:
        _ACTIVE_AUTOPLAY_GAMES = 1
        _AUTOPLAY_GAMES_STARTED = 1
    _concurrency_at_start = _ACTIVE_AUTOPLAY_GAMES
    _games_started_snapshot = _AUTOPLAY_GAMES_STARTED

    # --- Alternative provider swap for cost-efficient autoplay ---
    # Duck-typing means ClaudePlayer/RulesEngine work unchanged with any adapter.
    _original_clients = None
    _openrouter_adapter = None  # created per-game so different models can be tested

    # Determine which adapter to use
    if openrouter_model and not force_claude:
        if create_openrouter_adapter:
            _openrouter_adapter = create_openrouter_adapter(model=openrouter_model)
        if not _openrouter_adapter:
            print(f"[AUTOPLAY] OpenRouter adapter failed for {openrouter_model}, falling back")

    use_alt_adapter = None
    if _openrouter_adapter:
        use_alt_adapter = _openrouter_adapter
        alt_model = openrouter_model
        alt_label = f"OpenRouter ({openrouter_model})"
    elif cog._deepseek_adapter and not force_claude:
        use_alt_adapter = cog._deepseek_adapter
        alt_model = "deepseek-v4-flash"
        alt_label = "Deepseek"

    if use_alt_adapter:
        _original_clients = {
            'claude_ai_client': cog.engine.claude_ai.client,
            'claude_ai_model': cog.engine.claude_ai.model,
            'claude_ai_strategist_client': cog.engine.claude_ai.strategist_client,
            'claude_ai_strategist_model': cog.engine.claude_ai.strategist_model,
            'rules_client': cog.engine.rules.client,
            'rules_model': cog.engine.rules.model,
        }
        # July 20 audit: the swapped clients live on SHARED engine objects, so
        # under !autoplay-parallel the first game to FINISH used to restore the
        # original Anthropic client while sibling games were still running —
        # their remaining calls silently billed Anthropic and, with
        # claude-sonnet-5 returning thinking blocks, crashed response parsing
        # ('ThinkingBlock' object has no attribute 'text', 20 games in the
        # July 16 batch). Reference-count the swap: only the FIRST swap in a
        # quiescent state captures the true originals, and only the LAST
        # finishing game restores them (see the finally block).
        try:
            _AUTOPLAY_SWAP_DEPTH += 1
        except NameError:
            _AUTOPLAY_SWAP_DEPTH = 1
            _AUTOPLAY_TRUE_ORIGINALS = None
        if _AUTOPLAY_SWAP_DEPTH == 1:
            _AUTOPLAY_TRUE_ORIGINALS = dict(_original_clients)
        # Snapshot stats at game start for per-game delta calculation.
        # We snapshot both the actor adapter (V4-Flash) and, if Phase 3 is
        # active, the strategist adapter (V4-Pro) so we can bill them at
        # different rates in the finally block below.
        if hasattr(use_alt_adapter, 'get_stats'):
            _game_start_stats = use_alt_adapter.get_stats().copy()
        else:
            _game_start_stats = None
        _game_start_strat_stats = None
        if (cog._deepseek_reasoner_adapter
                and use_alt_adapter is cog._deepseek_adapter
                and hasattr(cog._deepseek_reasoner_adapter, 'get_stats')):
            _game_start_strat_stats = cog._deepseek_reasoner_adapter.get_stats().copy()
        cog.engine.claude_ai.client = use_alt_adapter
        cog.engine.claude_ai.model = alt_model
        cog.engine.rules.client = use_alt_adapter
        cog.engine.rules.model = alt_model

        # Phase 3: wire reasoner as strategist when using DeepSeek actor.
        # OpenRouter games get the same model for both roles (OpenRouter has
        # its own reasoning models selectable via --openrouter).
        if cog._deepseek_reasoner_adapter and use_alt_adapter is cog._deepseek_adapter:
            cog.engine.claude_ai.strategist_client = cog._deepseek_reasoner_adapter
            cog.engine.claude_ai.strategist_model = "deepseek-v4-pro"
            print("[AUTOPLAY] Phase 3 split: actor=deepseek-v4-flash (non-thinking), strategist=deepseek-v4-pro (reasoning_effort=medium since May 23)")
        else:
            # OpenRouter or single-model path: actor and strategist share one model
            cog.engine.claude_ai.strategist_client = None
            cog.engine.claude_ai.strategist_model = None

        # Swap SpellResolver's EffectExecutor if present (tier 2 complex fallback)
        if (cog.engine.spell_resolver
                and hasattr(cog.engine.spell_resolver, 'effect_executor')
                and cog.engine.spell_resolver.effect_executor):
            _original_clients['effect_executor_client'] = cog.engine.spell_resolver.effect_executor.claude_client
            if _AUTOPLAY_SWAP_DEPTH == 1:
                _AUTOPLAY_TRUE_ORIGINALS['effect_executor_client'] = _original_clients['effect_executor_client']
            cog.engine.spell_resolver.effect_executor.claude_client = use_alt_adapter

        # Reset circuit breaker — different provider gets a fresh start
        cog.engine.claude_ai._consecutive_failures = 0
        cog.engine.claude_ai._api_disabled = False

        print(f"[AUTOPLAY] Using {alt_label} for AI decisions")

    try:
        # Load decks
        available = list(cog.AUTOPLAY_DECKS.keys())

        if deck1_name:
            deck1_data = cog._load_deck_by_name(deck1_name)
            if not deck1_data:
                result["error"] = f"Deck not found: {deck1_name}"
                return result
        else:
            deck1_name = random.choice(available)
            deck1_data = cog._load_deck_by_name(deck1_name)

        if deck2_name:
            deck2_data = cog._load_deck_by_name(deck2_name)
            if not deck2_data:
                result["error"] = f"Deck not found: {deck2_name}"
                return result
        else:
            remaining = [d for d in available if d != deck1_name]
            deck2_name = random.choice(remaining) if remaining else random.choice(available)
            deck2_data = cog._load_deck_by_name(deck2_name)

        # NOTE: Do NOT set cog.engine.claude_deck here — it's a shared mutable
        # field and causes a race condition in parallel autoplay (all concurrent
        # games would get whichever deck was written last). Instead, pass deck2_data
        # directly to create_game() via player2_deck parameter.
        p1_name = "Rick Deckard"
        label = matchup_label or f"{deck1_name} vs {deck2_name}"

        # Create game thread
        thread = await _create_autoplay_thread(
            channel, f"MTG Autoplay: {label} ({game_format})")
        result["thread_id"] = thread.id

        # Create game
        game = await cog.engine.create_game(
            thread_id=thread.id,
            player1_name=p1_name,
            player1_id=99999,
            player2_name="Claude",
            player2_id=None,
            format=game_format,
            player1_deck=deck1_data,
            player2_deck=deck2_data
        )
        game.is_autoplay = True

        cog.engine.setup_stack(
            game, auto_pass_seconds=2.0,  # Give AI time to consider counterspells
            send_func=lambda msg: cog._autoplay_send(thread, msg),
            ai_response_enabled=True,
        )

        # May 2 audit: stash deck names on players so post-game chat context
        # ("which deck did I play?") can answer correctly. the bot was
        # confidently misremembering deck identities in chat ("I was piloting
        # Sythis enchantress" was lucky-correct; "Zulaport Cutthroat tells the
        # whole story" was a fabrication grounded only in last-5 graveyard
        # cards). Now the deck names live on each player so chat sees them.
        try:
            game.players[0]._deck_name = deck1_name
            game.players[1]._deck_name = deck2_name
        except Exception as e:
            print(f"[AUTOPLAY] Failed to stash deck names: {e}")

        # Set up logging
        game_logger = GameLogger(thread.id)
        cog.game_loggers[thread.id] = game_logger
        if cog._stdout_tee:
            cog._stdout_tee.add_game(thread.id, game_logger.console_path)
            cog._stdout_tee.active_thread = thread.id
        print(f"[AUTOPLAY] Logging to {game_logger.console_path}")

        # Start game
        first_player = random.randint(0, 1)
        cog.engine.start_game(game, first_player)

        # Emit format header NOW — before mulligan — so it's always in the log
        # even if mulligan evaluation throws an exception.
        embed = cog.display.create_game_embed(game)
        await cog._autoplay_send(thread,
            f"**Autoplay started!** {game.players[first_player].name} goes first.\n"
            f"Format: {game_format} | Decks: {deck1_name} vs {deck2_name}",
            embed=embed
        )

        # Apr 29 audit: emit a structured init line to the console log so
        # post-batch compliance audits can grep [GAME-INIT] for format / life
        # / deck info instead of scraping the discord log.
        try:
            p0, p1 = game.players[0], game.players[1]
            # June 11: strict= stamps every per-game console log with the
            # MTG_STRICT status, so "was the batch actually strict?" is a
            # grep instead of a memory test (the June 10 batch's strict
            # status was never confirmable after the fact).
            from mtg.util import strict_mode, git_sha
            # July 24: sha= makes batch-vintage checking one grep (was
            # snowflake-timestamp decode + corroborating-tag archaeology
            # every audit round).
            print(
                f"[GAME-INIT] format={game_format} "
                f"life={p0.life}/{p1.life} "
                f"deck0={deck1_name}({len(p0.library) + len(p0.hand) + len(getattr(p0, 'command_zone', []))}) "
                f"deck1={deck2_name}({len(p1.library) + len(p1.hand) + len(getattr(p1, 'command_zone', []))}) "
                f"first_player={game.players[first_player].name} "
                f"strict={1 if strict_mode() else 0} "
                f"sha={git_sha()}"
            )
        except Exception as e:
            print(f"[GAME-INIT] log emission failed: {e}")

        # Apr 30 audit: emit XMage bridge state into per-game logs so audits
        # can tell at a glance whether Tier 2.5 was active. The cog_load
        # captured the init outcome onto the engine in cog.py.
        try:
            if getattr(cog.engine, '_xmage_available', False):
                print(f"[XMAGE-INIT] Bridge available — Tier 2.5 active for this game")
            else:
                reason = getattr(cog.engine, '_xmage_init_reason', '') or "no reason captured"
                print(f"[XMAGE-INIT] Bridge unavailable — Tier 2.5 inactive (reason: {reason})")
        except Exception as e:
            print(f"[XMAGE-INIT] state log failed: {e}")

        # May 25 audit follow-up: emit per-deck tier-coverage breakdown so post-
        # batch audits can distinguish expected Tier 3 escalations (vanilla
        # creatures, novel cards) from unexpected ones (templates that should
        # have caught them but didn't). The supported_at_tier function exists
        # in mtg/coverage.py but was not wired into deck-load until now.
        try:
            from mtg.coverage import classify_deck
            for idx, (p, dname) in enumerate([(p0, deck1_name), (p1, deck2_name)]):
                # Iterate every zone the deck loader populated — same shape as
                # the [DECK-VALIDATE] block below uses.
                full_deck = []
                for zone_name in ("library", "hand", "command_zone", "companion_zone"):
                    full_deck.extend(getattr(p, zone_name, []) or [])
                if not full_deck:
                    continue
                cov = classify_deck(full_deck)
                counts = cov["counts"]
                # Pick 5 highest-value Tier 3 cards to show — dedupe + cap so
                # the line stays one-liner-grep-able.
                t3_seen = set()
                t3_sample = []
                for n in cov["by_tier"]["tier3"]:
                    nl = n.lower()
                    if nl not in t3_seen:
                        t3_seen.add(nl)
                        t3_sample.append(n)
                    if len(t3_sample) >= 5:
                        break
                sample_str = ", ".join(t3_sample) if t3_sample else "none"
                t3_extra = max(0, len(set(cov["by_tier"]["tier3"])) - len(t3_sample))
                if t3_extra:
                    sample_str += f" (+{t3_extra} more)"
                # July 22: report every subsystem, not just Tier 1.5. The old
                # line lumped Tier 1 hardcodes, Tier 2 SpellResolver, and
                # no-resolution cards (vanilla creatures, lands, mana rocks)
                # into tier3, overstating it ~2x. `free=` is the honest
                # headline: cards that cost nothing and resolve instantly.
                from mtg.coverage import FREE_TIERS
                free = sum(counts.get(t, 0) for t in FREE_TIERS)
                print(
                    f"[DECK-COVERAGE] deck{idx}={dname}: "
                    f"free={free} "
                    f"(templates={counts['template']} "
                    f"patterns={counts['pattern']} "
                    f"hardcoded={counts.get('hardcoded', 0)} "
                    f"spell_resolver={counts.get('spell_resolver', 0)} "
                    f"no_resolution={counts.get('no_resolution', 0)}) "
                    f"tier3={counts['tier3']} "
                    f"unknown={counts['unknown']} | "
                    f"tier3_sample=[{sample_str}]"
                )
        except Exception as e:
            print(f"[DECK-COVERAGE] log emission failed: {e}")

        # Apr 29 audit: validate each deck against the format being played.
        # Violations are logged + posted to Discord but do NOT abort the game —
        # a warning beats silent illegality, and aborting in autoplay would
        # break the matrix on legitimate-but-undeclared issues (cache misses
        # for new cards, etc.). Real users can investigate via [DECK-VALIDATE]
        # log lines.
        try:
            from mtg.models import FormatValidator
            for idx, (p, dname) in enumerate([(p0, deck1_name), (p1, deck2_name)]):
                # Reconstruct full-deck card list from all zones the loader
                # populated (library + hand at start; command_zone for EDH).
                full_deck = list(p.library) + list(p.hand)
                cmd_zone = list(getattr(p, 'command_zone', []) or [])
                full_deck.extend(cmd_zone)
                # Pass the WHOLE command zone (not just cmd_zone[0]) so
                # partner / friends-forever / background pairs validate
                # against their combined color identity.
                cmdrs = cmd_zone if cmd_zone else None
                ok, issues = FormatValidator.validate_deck(full_deck, game_format, commander=cmdrs)
                if ok:
                    print(f"[DECK-VALIDATE] {dname} ({game_format}): legal")
                else:
                    print(f"[DECK-VALIDATE] {dname} ({game_format}): {len(issues)} issue(s)")
                    for issue in issues[:10]:  # cap log spam
                        print(f"[DECK-VALIDATE]   - {issue}")
                    # Banned-card violations are HARD failures — strip the
                    # banned cards from the player's library so the game
                    # can't draw / play them. Color identity issues stay
                    # advisory (the engine blocks illegal casts at runtime
                    # via [COLOR-IDENTITY] anyway, and Scryfall fetches
                    # may have populated identity slightly differently
                    # for MDFCs / silver-bordered prints).
                    banned_issues = [i for i in issues if 'banned' in i.lower()]
                    if banned_issues:
                        # Extract the banned card names from issue strings
                        # like "**Primeval Titan** is banned in commander"
                        import re as _re
                        banned_names = set()
                        for issue in banned_issues:
                            m = _re.search(r'\*\*(.+?)\*\* is banned', issue)
                            if m:
                                banned_names.add(m.group(1).lower())
                        if banned_names:
                            removed = []
                            for zone_attr in ('library', 'hand', 'command_zone'):
                                zone = getattr(p, zone_attr, None) or []
                                kept = []
                                for c in zone:
                                    if c.name.lower() in banned_names:
                                        removed.append(c.name)
                                    else:
                                        kept.append(c)
                                if zone_attr == 'library':
                                    p.library = kept
                                elif zone_attr == 'hand':
                                    p.hand = kept
                                elif zone_attr == 'command_zone':
                                    p.command_zone = kept
                            if removed:
                                print(f"[DECK-VALIDATE] {dname}: stripped {len(removed)} banned card(s): {', '.join(removed)}")
                                await cog._autoplay_send(
                                    thread,
                                    f"⚠️ Stripped {len(removed)} banned card(s) from {dname}: "
                                    f"{', '.join(removed)} — game will continue with the rest of the deck."
                                )
                    # Single Discord summary so players see something is off
                    # without dumping 60 lines.
                    other_issues = len(issues) - len(banned_issues)
                    if other_issues > 0:
                        summary = f"⚠️ Deck {dname} has {other_issues} {game_format} legality issue(s) — see console log."
                        await cog._autoplay_send(thread, summary)
        except Exception as e:
            print(f"[DECK-VALIDATE] validation skipped due to error: {e}")

        # Mulligan evaluation
        for pi, player in enumerate(game.players):
            mulligans = 0
            max_mulligans = 3
            while mulligans < max_mulligans:
                should_mull = await cog.engine.claude_ai.decide_mulligan(player.hand, mulligans)
                if not should_mull:
                    break
                mulligans += 1
                player.library.extend(player.hand)
                player.hand.clear()
                random.shuffle(player.library)
                cog.engine.draw_cards(player, 7)
            if mulligans > 0:
                player.hand.sort(key=lambda c: int(c.cmc) if isinstance(c.cmc, (int, float)) else 0, reverse=True)
                for _ in range(mulligans):
                    if player.hand:
                        bottomed = player.hand.pop(0)
                        player.library.append(bottomed)
                await cog._autoplay_send(thread, f"\U0001f504 {player.name} mulliganed {mulligans}x, keeps {len(player.hand)} cards.")
            player.mulligans_taken = mulligans
            player.has_kept_hand = True

        # ============================
        # MAIN GAME LOOP
        # ============================
        consecutive_zero_action_turns = 0
        try:
            for turn_num in range(max_turns):
                if game.ended:
                    break

                # Immediate abort: user ran !autoplay-stop
                if cog._batch_stop_flag:
                    game.ended = True
                    game.winner = None
                    await cog._autoplay_send(thread,
                        "⏹️ **[AUTOPLAY] Game aborted by !autoplay-stop**")
                    result["outcome"] = "aborted"
                    break

                # Circuit breaker abort: if API is disabled and both players
                # are doing nothing, the game is a zombie — abort early.
                # Also abort unconditionally after 10 consecutive zero-action turns
                # (handles mana stalls where DeepSeek keeps passing with full hands).
                if (cog.engine.claude_ai._api_disabled and consecutive_zero_action_turns >= 3) or consecutive_zero_action_turns >= 10:
                    game.ended = True
                    game.winner = None
                    reason = "API disabled (circuit breaker)" if cog.engine.claude_ai._api_disabled else f"{consecutive_zero_action_turns} consecutive turns with no actions (mana stall)"
                    await cog._autoplay_send(thread,
                        f"\u26a0\ufe0f **[AUTOPLAY] Aborting: {reason} — declaring draw.**")
                    result["outcome"] = "circuit_breaker"
                    break

                # May 17 audit: stagnation detection. Track life totals; if
                # past turn 30 AND no >=3 life swing for either player over
                # the last 15 turns, declare a draw (likely flicker/lock loop
                # neither side can break — `game_1505393618897342576` was
                # the canonical case in the May 16 batch).
                if not hasattr(game, '_life_history'):
                    game._life_history = []
                try:
                    game._life_history.append(tuple(int(p.life) for p in game.players))
                except Exception:
                    pass
                if turn_num >= 30 and len(game._life_history) >= 15:
                    recent = game._life_history[-15:]
                    has_swing = False
                    for pi in range(len(recent[0])):
                        max_l = max(t[pi] for t in recent)
                        min_l = min(t[pi] for t in recent)
                        if max_l - min_l >= 3:
                            has_swing = True
                            break
                    # June 11 audit: don't call a stagnation draw while a kill
                    # is visibly pending — game 1514621789630631936 was drawn
                    # with Rick at 1 life, empty board, one combat step before
                    # Claude's newly-cast (summoning-sick) Peregrine Drake
                    # could swing for lethal.
                    if not has_swing:
                        for _si, _sp in enumerate(game.players):
                            _opp_p = game.players[1 - _si]
                            if _sp.life <= 5 and any(
                                    c.is_creature() for c in _opp_p.battlefield):
                                has_swing = True
                                print(f"[AUTOPLAY] [STUCK-GAME] Stagnation suppressed: "
                                      f"{_sp.name} at {_sp.life} life with opposing "
                                      f"creatures on board — kill pending")
                                break
                    if not has_swing:
                        game.ended = True
                        game.winner = None
                        # June 11 audit: without this, the Draw summary's
                        # fallback printed "Reason: simultaneous loss" for
                        # stagnation timeouts.
                        game.loss_reason = ("stagnation — no significant life "
                                            "change in 15 turns")
                        await cog._autoplay_send(thread,
                            "⏸️ **Stagnation draw — no significant "
                            "life change in 15 turns past turn 30 (likely a flicker or "
                            "soft-lock loop neither side can break)**")
                        print(f"[AUTOPLAY] [STUCK-GAME] Stagnation draw at turn "
                              f"{turn_num}; life history last 15: {recent}")
                        result["outcome"] = "stagnation_draw"
                        break

                # June 10 audit (V31h): emit the turn banner BEFORE the
                # upkeep/draw phase lines — it used to post after them, so
                # upkeep triggers (Phyrexian Arena, Bitterblossom) visually
                # landed in the PREVIOUS turn's block in every game. Dedup
                # guard unchanged (see comment at the original site below).
                _turn_key = (game.turn_number, game.active_player.name)
                if getattr(game, '_last_emitted_turn_key', None) != _turn_key:
                    await cog._autoplay_send(
                        thread,
                        "\U0001f504 **Turn {}** — **{}**'s turn".format(
                            game.turn_number, game.active_player.name))
                    game._last_emitted_turn_key = _turn_key

                if game.phase == Phase.UNTAP:
                    _, _p1 = cog.engine.advance_phase(game)  # UNTAP → UPKEEP
                    _, _p2 = cog.engine.advance_phase(game)  # UPKEEP → DRAW (upkeep triggers here)
                    _, _p3 = cog.engine.advance_phase(game)  # DRAW → MAIN1
                    for _m in _p1 + _p2 + _p3:
                        await cog._autoplay_send(thread, _m)
                    # Drain sync-queued triggers via Tier 3 (Meren/Abyss/Emeria/etc.)
                    # July 20 batch-3 audit (reviewer S1): collapse identical
                    # consecutive drain lines into one ×N line BEFORE sending —
                    # Fall of the Thran returning two Forests produced
                    # byte-identical messages that Layer-1 dedup silently ate.
                    _drained = await cog.engine.drain_pending_triggers(game)
                    _runs = []
                    for _m in _drained:
                        if _runs and _runs[-1][0] == _m:
                            _runs[-1][1] += 1
                        else:
                            _runs.append([_m, 1])
                    for _m, _n in _runs:
                        await cog._autoplay_send(thread, _m if _n == 1 else f"{_m} (×{_n})")

                # Guard against duplicate turn headers: advance_phase may loop
                # or trigger spurious re-entry; emit each (turn, player) banner once.
                # May 14 audit: `cog._last_emitted_turn_key` was at cog scope \u2014
                # in parallel autoplay it was shared across games, so Game B's
                # turn 5 would be skipped if Game A had just emitted (5, X).
                # Move the key to the game state itself so each game tracks
                # its own emission status independently.
                # (June 10, V31h: the banner emit itself moved ABOVE the
                # UNTAP phase-advance block so it precedes upkeep/draw lines.)

                turn_had_actions = False
                if game.active_player.is_claude:
                    actions = await cog.engine.execute_claude_turn(game)
                    actions = cog._sanitize_action_bullets(actions)
                    if actions:
                        turn_had_actions = True
                        msg = f"**Claude's turn:**\n" + "\n".join(f"\u2022 {a}" for a in actions)
                        if len(msg) > 1900:
                            await cog._autoplay_send(thread, "**Claude's turn:**")
                            for a in actions:
                                await cog._autoplay_send(thread, f"\u2022 {a[:1900]}")
                        else:
                            await cog._autoplay_send(thread, msg)
                    else:
                        await cog._autoplay_send(thread, "*Claude thinks, then passes.*")

                    await cog._autoplay_resolve_pending_action(thread, game)

                    if game.waiting_for_human_blocks:
                        game.waiting_for_human_blocks = False
                        defender_idx = 1 - game.active_player_index
                        attacker_cards = []
                        for a_id in game.attackers:
                            card_result = game.find_card_global(a_id)
                            if card_result:
                                attacker_cards.append(card_result[0])
                        blocks = await cog.engine.claude_ai.decide_blocks(game, defender_idx, attacker_cards)
                        defender = game.players[defender_idx]
                        if blocks:
                            # May 7 audit fix #2: disambiguate same-name creatures
                            # (Plant blocks Plant repeated 8 times). Per-name index
                            # for both attackers and blockers.
                            name_counts2 = {}
                            for a_id2 in game.attackers:
                                ar2 = game.find_card_global(a_id2)
                                if ar2:
                                    name_counts2[ar2[0].name] = name_counts2.get(ar2[0].name, 0) + 1
                            for blocker_ids2 in blocks.values():
                                for b_id2 in blocker_ids2 or []:
                                    br2 = game.find_card_global(b_id2)
                                    if br2:
                                        name_counts2[br2[0].name] = name_counts2.get(br2[0].name, 0) + 1
                            name_index2 = {}
                            name_running2 = {}
                            def _label_for2(card):
                                if card.id in name_index2:
                                    return name_index2[card.id]
                                if name_counts2.get(card.name, 0) > 1:
                                    idx = name_running2.get(card.name, 0) + 1
                                    name_running2[card.name] = idx
                                    label = f"{card.name} #{idx}"
                                else:
                                    label = card.name
                                name_index2[card.id] = label
                                return label

                            block_msgs = []
                            for attacker_id, blocker_ids in blocks.items():
                                if blocker_ids:
                                    atk_result = game.find_card_global(attacker_id)
                                    if not atk_result:
                                        continue
                                    attacker = atk_result[0]
                                    attacker_label = _label_for2(attacker)
                                    blk_names = []
                                    for blocker_id in blocker_ids:
                                        blk_result = game.find_card_global(blocker_id)
                                        if not blk_result:
                                            continue
                                        blocker = blk_result[0]
                                        # May 30 audit: guard this path (Claude attacks, Rick
                                        # blocks — reached after "[EXECUTE_CLAUDE] Pausing for
                                        # human blocks") with can_block. It was the one block-
                                        # application loop missing the check, letting non-creature
                                        # devotion gods (Erebos at devotion<5) block and deal
                                        # combat damage, and letting ground creatures block fliers
                                        # (CR 509.1a/b). Mirror the guarded paths at ~727 and ~793.
                                        if not blocker.can_block(attacker, game=game):
                                            print(f"[BLOCK-INVALID] {blocker.name} cannot block "
                                                  f"{attacker.name} (evasion mismatch) — skipped")
                                            continue
                                        blocker.blocking.append(attacker.id)
                                        attacker.blocked_by.append(blocker.id)
                                        if attacker.id not in game.blockers:
                                            game.blockers[attacker.id] = []
                                        game.blockers[attacker.id].append(blocker.id)
                                        blk_names.append(_label_for2(blocker))
                                    # May 30 audit: mirror the ~750 filter so a fully-rejected
                                    # block doesn't emit "• blocks <Attacker>" with no blocker name.
                                    blk_names = [n for n in blk_names if n and n.strip()]
                                    if not blk_names:
                                        continue
                                    block_msgs.append(f"{', '.join(blk_names)} blocks {attacker_label}")
                            if block_msgs:
                                await cog._autoplay_send(thread,
                                    f"\U0001f6e1\ufe0f **{defender.name}** blocks:\n" + "\n".join(f"\u2022 {b}" for b in block_msgs))
                        else:
                            await cog._autoplay_send(thread, f"\U0001f6e1\ufe0f **{defender.name}** doesn't block.")

                        if game.stack_enabled and game.attackers:
                            send_fn = lambda msg: cog._autoplay_send(thread, msg)
                            await cog.engine._combat_priority_round(game, send_fn, "after blockers declared")

                        if game.attackers and not game.ended:
                            await cog._autoplay_resolve_combat(thread, game)

                        if game.phase == Phase.MAIN2 and not game.ended:
                            post_combat = await cog.engine.continue_claude_post_combat(game)
                            post_combat = cog._sanitize_action_bullets(post_combat)
                            if post_combat:
                                msg = "**Claude (post-combat):**\n" + "\n".join(f"\u2022 {a}" for a in post_combat)
                                await cog._autoplay_send(thread, msg)
                else:
                    actions = await cog._autoplay_human_turn(thread, game, game.active_player_index)
                    if actions:
                        turn_had_actions = True

                # Drain sync-queued triggers (dies from combat, attack triggers, etc.)
                for _m in await cog.engine.drain_pending_triggers(game):
                    await cog._autoplay_send(thread, _m)

                # Track consecutive zero-action turns for circuit breaker abort
                if turn_had_actions:
                    consecutive_zero_action_turns = 0
                else:
                    consecutive_zero_action_turns += 1

                if not game.ended:
                    await cog._autoplay_send(thread, embed=cog.display.create_game_embed(game))

                if game.ended:
                    break

                end_msgs = cog.engine.end_turn(game)
                for msg in end_msgs:
                    await cog._autoplay_send(thread, msg)
                # Drain end-step triggers queued by end_turn (Meren, Athreos, etc.)
                for _m in await cog.engine.drain_pending_triggers(game):
                    await cog._autoplay_send(thread, _m)

                if game.pending_action and game.pending_action.get('type') == 'discard_to_hand_size':
                    pa = game.pending_action
                    pi = pa['player_idx']
                    discard_count = pa['cards_to_discard']
                    discard_player = game.players[pi]
                    discarded_names = []
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
                        discarded_names.append(worst.name)
                    if discarded_names:
                        n = len(discarded_names)
                        if n == 1:
                            summary = (f"\U0001f4e4 {discard_player.name} discards "
                                       f"{discarded_names[0]} to hand size")
                        else:
                            preview = ", ".join(discarded_names[:8])
                            more = f" +{n - 8} more" if n > 8 else ""
                            summary = (f"\U0001f4e4 {discard_player.name} discards "
                                       f"{n} cards to hand size: {preview}{more}")
                        await cog._autoplay_send(thread, summary)
                    game.pending_action = None

                # Expire pending resolves that have been sitting for 3+ turns without
                # being auto-resolved. These are typically triggers the autoplay resolver
                # can't handle (e.g. "Teferi end step trigger", "Search for Azcanta
                # upkeep"). Without expiry they get re-announced every turn, spamming
                # Discord and misleading the audit log. Console emits a warning so the
                # entry still appears in logs as a template-coverage signal.
                if getattr(game, 'pending_resolves', None):
                    current_turn = game.turn_number
                    if not hasattr(game, '_pending_resolve_added_turn'):
                        game._pending_resolve_added_turn = {}
                    # Record the turn number when each item first appeared
                    for pr in game.pending_resolves:
                        if pr not in game._pending_resolve_added_turn:
                            game._pending_resolve_added_turn[pr] = current_turn
                    # Remove items that have been pending for 3 or more turns
                    stale = [pr for pr in list(game.pending_resolves)
                             if current_turn - game._pending_resolve_added_turn.get(pr, current_turn) >= 3]
                    for pr in stale:
                        print(f"[AUTOPLAY] Expiring stale pending resolve (3+ turns): {pr[:80]}")
                        game.pending_resolves.remove(pr)
                        game._pending_resolve_added_turn.pop(pr, None)

                await asyncio.sleep(2)

        except Exception as e:
            import traceback
            traceback.print_exc()
            await cog._autoplay_send(thread, f"\u274c Autoplay error on turn {game.turn_number}: {e}")
            result["error"] = str(e)

        # ============================
        # GAME SUMMARY
        # ============================
        result["turns"] = game.turn_number
        result["p1_life"] = game.players[0].life
        result["p2_life"] = game.players[1].life

        # May 20 audit: clamp displayed life at 0 (CR 119.3 lets life go negative,
        # but the player-facing summary should never show "-N life"). The May 19
        # clamp landed at [COMBAT-LIFE]/[NONCOMBAT-LIFE]/[SPELL-DAMAGE] print
        # sites but missed these three game-end summary paths.
        if game.ended and game.winner is not None:
            winner = game.players[game.winner]
            loser = game.players[1 - game.winner]
            result["outcome"] = "win_p1" if game.winner == 0 else "win_p2"
            result["winner"] = winner.name
            # June 11 audit: lose_the_game/pay_or_lose zero the loser's life as
            # an SBA shortcut, which fabricated "Final life: 0" for players who
            # died to a pact at 26 life. Prefer the stashed pre-loss total.
            _loser_life = getattr(loser, '_final_life_before_loss', loser.life)
            await cog._autoplay_send(thread,
                f"\U0001f3c6 **{winner.name} wins!**\n"
                f"\u2022 Final life: {winner.name} {max(0, winner.life)} / {loser.name} {max(0, _loser_life)}\n"
                f"\u2022 Turns: {game.turn_number}\n"
                f"\u2022 Format: {game_format}")
        elif game.ended and game.winner is None:
            # Genuine draw (CR 104.3b \u2014 multiple players lost simultaneously).
            # Report the actual end state, not a 70-turn timeout.
            result["outcome"] = "draw"
            await cog._autoplay_send(thread,
                f"\U0001f91d **Draw!**\n"
                f"\u2022 Reason: {getattr(game, 'loss_reason', 'simultaneous loss')}\n"
                f"\u2022 Final life: {game.players[0].name} {max(0, game.players[0].life)} / {game.players[1].name} {max(0, game.players[1].life)}\n"
                f"\u2022 Turns: {game.turn_number}\n"
                f"\u2022 Format: {game_format}")
        elif result["error"]:
            result["outcome"] = "crash"
        else:
            result["outcome"] = "timeout"
            await cog._autoplay_send(thread,
                f"\u23f1\ufe0f Game ended after {max_turns} turns (no winner)\n"
                f"\u2022 Life: {game.players[0].name} {max(0, game.players[0].life)} / {game.players[1].name} {max(0, game.players[1].life)}")

    except Exception as e:
        import traceback
        traceback.print_exc()
        result["outcome"] = "crash"
        result["error"] = str(e)
        if thread:
            try:
                await cog._autoplay_send(thread, f"\u274c Fatal autoplay error: {e}")
            except Exception:
                pass
    finally:
        # Restore original Claude clients if we swapped to Deepseek.
        # July 20 audit: gated on the swap depth counter — only the LAST
        # finishing autoplay game restores, and it restores the TRUE originals
        # captured by the first swap (its own _original_clients may just be
        # the already-swapped DeepSeek adapter under parallel mode).
        if _original_clients:
            _AUTOPLAY_SWAP_DEPTH -= 1
            if _AUTOPLAY_SWAP_DEPTH <= 0 and _AUTOPLAY_TRUE_ORIGINALS:
                _restore = _AUTOPLAY_TRUE_ORIGINALS
                cog.engine.claude_ai.client = _restore['claude_ai_client']
                cog.engine.claude_ai.model = _restore['claude_ai_model']
                cog.engine.claude_ai.strategist_client = _restore['claude_ai_strategist_client']
                cog.engine.claude_ai.strategist_model = _restore['claude_ai_strategist_model']
                cog.engine.rules.client = _restore['rules_client']
                cog.engine.rules.model = _restore['rules_model']
                if 'effect_executor_client' in _restore:
                    cog.engine.spell_resolver.effect_executor.claude_client = _restore['effect_executor_client']
            # Log Deepseek stats — both per-game delta and cumulative.
            # DeepSeek V4 pricing — REAL rates, verified May 30 2026 against the
            # account's own usage export (usage_data_2026_5.zip, `price` column).
            # These reproduce the billed cost to the cent ($3.03 for May 26).
            # The earlier list rates (V4-Flash $0.27/$0.07/$1.10, V4-Pro
            # $0.56/$0.14/$1.68) over-estimated ~38x — overwhelmingly because the
            # cache-HIT price is ~25-39x lower than modeled, and hits are ~66-84%
            # of input tokens; output is also 2-4x lower than the old list rate.
            #   V4-Flash (actor):      hit $0.0028/M, miss $0.14/M,  out $0.28/M
            #   V4-Pro   (strategist): hit $0.0036/M, miss $0.435/M, out $0.87/M
            ACTOR_INPUT_MISS_RATE = 0.14   / 1_000_000
            ACTOR_INPUT_HIT_RATE  = 0.0028 / 1_000_000
            ACTOR_OUTPUT_RATE     = 0.28   / 1_000_000
            STRAT_INPUT_MISS_RATE = 0.435  / 1_000_000
            STRAT_INPUT_HIT_RATE  = 0.0036 / 1_000_000
            STRAT_OUTPUT_RATE     = 0.87   / 1_000_000
            # PARALLEL-MODE CAVEAT: with real rates the CUMULATIVE
            # [STATS-CUMULATIVE] est_cost tracks the real bill. The per-game
            # [STATS-GAME] delta is UNRELIABLE under `!autoplay-parallel` — all
            # concurrent games share one adapter, so each game's
            # (stats - game_start) delta sweeps up other games' tokens. Summing
            # per-game costs over a parallel batch over-counts ~18x (that was the
            # bogus "$115" — DeepSeek billed ~$3). Trust the cumulative line for
            # batch cost; the per-game line is only clean in sequential autoplay.
            def _split_input(prompt_tokens: int, hit_tokens: int, miss_tokens: int):
                """Split a prompt-token total proportionally between hit/miss
                when the breakdown is known. Falls back to all-miss when no
                cache data is available."""
                seen = hit_tokens + miss_tokens
                if prompt_tokens <= 0:
                    return 0, 0
                if seen <= 0:
                    return 0, prompt_tokens
                hit_share = int(round(prompt_tokens * (hit_tokens / seen)))
                miss_share = max(0, prompt_tokens - hit_share)
                return hit_share, miss_share
            def _actor_cost(prompt_tokens: int, completion_tokens: int,
                            hit_tokens: int = 0, miss_tokens: int = 0) -> float:
                hit, miss = _split_input(prompt_tokens, hit_tokens, miss_tokens)
                return (hit * ACTOR_INPUT_HIT_RATE
                        + miss * ACTOR_INPUT_MISS_RATE
                        + completion_tokens * ACTOR_OUTPUT_RATE)
            def _strat_cost(prompt_tokens: int, completion_tokens: int,
                            hit_tokens: int = 0, miss_tokens: int = 0) -> float:
                hit, miss = _split_input(prompt_tokens, hit_tokens, miss_tokens)
                return (hit * STRAT_INPUT_HIT_RATE
                        + miss * STRAT_INPUT_MISS_RATE
                        + completion_tokens * STRAT_OUTPUT_RATE)
            # May 18 audit: nominal (cache-unadjusted) costs so the per-game
            # [STATS-GAME] line is comparable to bot.py:get_cost_summary
            # which uses nominal rates only. Without these, the per-game
            # decomposition charged input at hit-discounted rates and looked
            # like a different model than the lifetime !cost output.
            def _actor_cost_nominal(prompt_tokens: int, completion_tokens: int) -> float:
                return (prompt_tokens * ACTOR_INPUT_MISS_RATE
                        + completion_tokens * ACTOR_OUTPUT_RATE)
            def _strat_cost_nominal(prompt_tokens: int, completion_tokens: int) -> float:
                return (prompt_tokens * STRAT_INPUT_MISS_RATE
                        + completion_tokens * STRAT_OUTPUT_RATE)

            # May 17 audit: wrap the STATS emit in defensive try/except. If
            # any of the math/stats lookups errors, we still want SOME signal
            # in the log instead of silently dropping the line — game
            # `1505386281578921984` showed this hole in the May 16 batch.
            try:
                stats = cog._deepseek_adapter.get_stats()
                strat_stats = None
                if cog._deepseek_reasoner_adapter and hasattr(cog._deepseek_reasoner_adapter, 'get_stats'):
                    strat_stats = cog._deepseek_reasoner_adapter.get_stats()
            except Exception as _stats_err:
                print(f"[AUTOPLAY] STATS-GAME stats lookup failed: {_stats_err}")
                stats = None
                strat_stats = None

            # May 16 audit: detect "this game ran but the API was dead the
            # whole time" — emit STATS-GAME-ABORTED instead of normal STATS-GAME
            # so an auditor doesn't accidentally count 12 trailing $0 games as
            # $0.74 each (the frozen-stats footgun from the May 15 batch).
            api_disabled = getattr(cog.engine.claude_ai, '_api_disabled', False)

            try:
                if _game_start_stats and stats:
                    game_calls = stats['calls'] - _game_start_stats.get('calls', 0)
                    game_prompt = stats['prompt_tokens'] - _game_start_stats.get('prompt_tokens', 0)
                    game_completion = stats['completion_tokens'] - _game_start_stats.get('completion_tokens', 0)
                    # May 17 audit: thread cumulative cache hit/miss into per-game
                    # cost math. The breakdown is cumulative-only so we use the
                    # batch-level ratio as a proxy for this game's split — close
                    # enough since the same prompts dominate across all games.
                    game_hit = stats.get('cache_hit_tokens', 0)
                    game_miss = stats.get('cache_miss_tokens', 0)
                    actor_game_cost = _actor_cost(game_prompt, game_completion, game_hit, game_miss)

                    strat_game_calls = 0
                    strat_game_prompt = 0
                    strat_game_completion = 0
                    strat_game_cost = 0.0
                    if _game_start_strat_stats and strat_stats:
                        strat_game_calls = strat_stats['calls'] - _game_start_strat_stats.get('calls', 0)
                        strat_game_prompt = strat_stats['prompt_tokens'] - _game_start_strat_stats.get('prompt_tokens', 0)
                        strat_game_completion = strat_stats['completion_tokens'] - _game_start_strat_stats.get('completion_tokens', 0)
                        strat_hit = strat_stats.get('cache_hit_tokens', 0)
                        strat_miss = strat_stats.get('cache_miss_tokens', 0)
                        strat_game_cost = _strat_cost(strat_game_prompt, strat_game_completion,
                                                      strat_hit, strat_miss)

                    total_game_cost = actor_game_cost + strat_game_cost
                    total_game_calls = game_calls + strat_game_calls
                    total_game_prompt = game_prompt + strat_game_prompt
                    total_game_completion = game_completion + strat_game_completion

                    tag = "STATS-GAME-ABORTED" if (api_disabled and total_game_calls == 0) else "STATS-GAME"
                    # June 11 audit: label the line when other games shared the
                    # adapter during this game — the delta then includes THEIR
                    # tokens (the batch showed 814-2807 "per-game" calls that
                    # were really batch-wide totals, incl. decide_mulligan=81
                    # for a game with 3 mulligans).
                    _shared = (_concurrency_at_start > 1
                               or _AUTOPLAY_GAMES_STARTED > _games_started_snapshot)
                    if _shared and tag == "STATS-GAME":
                        tag = "STATS-GAME-SHARED(parallel batch — delta includes other games)"
                    # May 18 audit: also emit nominal (no-cache-discount) costs
                    # so this line reconciles with the lifetime `!cost` output
                    # (bot.py:get_cost_summary), which uses nominal rates only.
                    # The May 17 audit found the two outputs were comparing
                    # apples to oranges — auditors couldn't cross-check.
                    actor_nominal = _actor_cost_nominal(game_prompt, game_completion)
                    strat_nominal = _strat_cost_nominal(strat_game_prompt, strat_game_completion)
                    total_nominal = actor_nominal + strat_nominal
                    print(f"[AUTOPLAY] [{tag}] calls={total_game_calls} "
                          f"(actor={game_calls}, strat={strat_game_calls}) "
                          f"prompt_tokens={total_game_prompt} completion_tokens={total_game_completion} "
                          f"est_cost=${total_game_cost:.4f} "
                          f"(actor=${actor_game_cost:.4f}, strat=${strat_game_cost:.4f}) "
                          f"nominal=${total_nominal:.4f} "
                          f"(actor_nom=${actor_nominal:.4f}, strat_nom=${strat_nominal:.4f})")
                elif _game_start_stats:
                    # Stats lookup failed earlier but we still want a marker
                    # in the log so the auditor knows the game finished.
                    print(f"[AUTOPLAY] [STATS-GAME-INCOMPLETE] stats lookup failed; "
                          f"game completed but per-game token math unavailable")
                else:
                    # May 20 audit: a game in the May 19 batch finished with no
                    # STATS-GAME line at all (game_1506212790317088768). The
                    # defensive try/except above only catches errors AFTER
                    # _game_start_stats is set; if it was None from the start,
                    # the elif silently skipped. Emit an explicit marker so
                    # auditors can count games-emitted vs games-played.
                    print(f"[AUTOPLAY] [STATS-GAME-INCOMPLETE] no per-game start snapshot; "
                          f"adapter may have been unavailable at game start")
            except Exception as _emit_err:
                print(f"[AUTOPLAY] STATS-GAME emit error: {_emit_err}")
                # Still emit an INCOMPLETE marker so the game doesn't go
                # entirely silent in the audit grep.
                print(f"[AUTOPLAY] [STATS-GAME-INCOMPLETE] emit raised: {type(_emit_err).__name__}")

            try:
                if stats:
                    actor_total_cost = _actor_cost(
                        stats['prompt_tokens'], stats['completion_tokens'],
                        stats.get('cache_hit_tokens', 0), stats.get('cache_miss_tokens', 0),
                    )
                    strat_total_cost = 0.0
                    strat_total_calls = 0
                    strat_total_prompt = 0
                    strat_total_completion = 0
                    if strat_stats:
                        strat_total_calls = strat_stats['calls']
                        strat_total_prompt = strat_stats['prompt_tokens']
                        strat_total_completion = strat_stats['completion_tokens']
                        strat_total_cost = _strat_cost(
                            strat_total_prompt, strat_total_completion,
                            strat_stats.get('cache_hit_tokens', 0),
                            strat_stats.get('cache_miss_tokens', 0),
                        )
                    total_cost = actor_total_cost + strat_total_cost
                    # May 17 audit: surface cache hit rate so cost-regression
                    # debugging doesn't have to guess. Numbers are cumulative
                    # across the adapter session (multi-game run).
                    hit_tokens = stats.get('cache_hit_tokens', 0)
                    miss_tokens = stats.get('cache_miss_tokens', 0)
                    cache_seen = hit_tokens + miss_tokens
                    cache_hit_pct = (100.0 * hit_tokens / cache_seen) if cache_seen else 0.0
                    print(f"[AUTOPLAY] [STATS-CUMULATIVE] calls={stats['calls'] + strat_total_calls} "
                          f"(actor={stats['calls']}, strat={strat_total_calls}) "
                          f"prompt_tokens={stats['prompt_tokens'] + strat_total_prompt} "
                          f"completion_tokens={stats['completion_tokens'] + strat_total_completion} "
                          f"est_cost=${total_cost:.4f} "
                          f"(actor=${actor_total_cost:.4f}, strat=${strat_total_cost:.4f}) "
                          f"cache_hit={cache_hit_pct:.1f}% "
                          f"({hit_tokens}/{cache_seen} prompt tokens)")
                    # May 17 audit: always emit per-purpose breakdown at
                    # game-end, not just every 200th call. ~60% of games
                    # in the May 16 batch never crossed the 200-call mark
                    # so the per-purpose data was missing from most logs.
                    # May 20 audit: get_stats() returns process-wide cumulative
                    # counters, so the unmodified breakdown printed batch totals
                    # for every game (only the last game's line was accurate).
                    # Subtract per-game start snapshots when available.
                    actor_purposes = dict(stats.get('purpose_counts', {}) or {})
                    if _game_start_stats:
                        start_actor_purposes = _game_start_stats.get('purpose_counts', {}) or {}
                        actor_purposes = {
                            k: v - int(start_actor_purposes.get(k, 0))
                            for k, v in actor_purposes.items()
                        }
                    strat_purposes = dict((strat_stats.get('purpose_counts', {}) or {})) if strat_stats else {}
                    if strat_purposes and _game_start_strat_stats:
                        start_strat_purposes = _game_start_strat_stats.get('purpose_counts', {}) or {}
                        strat_purposes = {
                            k: v - int(start_strat_purposes.get(k, 0))
                            for k, v in strat_purposes.items()
                        }
                    merged_purposes: Dict[str, int] = {}
                    for src in (actor_purposes, strat_purposes):
                        for k, v in src.items():
                            # Negative diffs would be nonsensical; drop them defensively
                            # in case purpose_counts ever resets mid-game.
                            if v > 0:
                                merged_purposes[k] = merged_purposes.get(k, 0) + int(v)
                    # May 20 audit (#16): always emit CALL-BREAKDOWN-FINAL,
                    # even when merged_purposes is empty. The May 20 batch had
                    # 125/126 games emit; the one miss (game_1506618495641587802)
                    # ended via a path where purpose tracking either never
                    # accumulated calls (very short game) or the per-game start
                    # snapshot equalled the end snapshot. Emit `(none)` so a
                    # post-batch grep counts every game uniformly.
                    if merged_purposes:
                        ranked = sorted(merged_purposes.items(), key=lambda x: -x[1])
                        breakdown = ", ".join(f"{k}={v}" for k, v in ranked)
                        print(f"[AUTOPLAY] [CALL-BREAKDOWN-FINAL] {breakdown}")
                    else:
                        print(f"[AUTOPLAY] [CALL-BREAKDOWN-FINAL] (none — no purpose-tagged calls this game)")
            except Exception as _cum_err:
                print(f"[AUTOPLAY] STATS-CUMULATIVE emit error: {_cum_err}")

        # Cleanup logging and game state
        if cog._stdout_tee:
            cog._stdout_tee.active_thread = None
        if thread:
            cog._cleanup_game_logging(thread.id)
            if thread.id in cog.engine.games:
                cog.engine.delete_game(thread.id)
        result["duration_seconds"] = _time.time() - start_time
        _ACTIVE_AUTOPLAY_GAMES = max(0, _ACTIVE_AUTOPLAY_GAMES - 1)

    return result


async def _check_deepseek_balance(cog) -> dict | None:
    """Query DeepSeek account balance before starting a batch.

    Returns a dict with keys:
      - 'ok': bool — True if balance > 0 and account is available
      - 'balance_cny': str — total balance in CNY ('' if unavailable)
      - 'message': str — human-readable summary
    Returns None if DeepSeek isn't configured (no adapter, so no need to check).

    DeepSeek balance API: GET https://api.deepseek.com/user/balance
    Response: {"is_available": true, "balance_infos": [
        {"currency": "CNY", "total_balance": "3.50", ...}
    ]}
    A 402 mid-batch means the account ran dry; this check catches it up front.
    """
    if not cog._deepseek_adapter:
        return None  # Not using DeepSeek — nothing to check

    import os, aiohttp
    api_key = os.getenv("DEEPSEEK_API_KEY")
    if not api_key:
        return None  # Key not in env; adapter may have gotten it elsewhere

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                "https://api.deepseek.com/user/balance",
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    print(f"[DEEPSEEK-BALANCE] HTTP {resp.status}: {body[:200]}")
                    return {
                        "ok": False,
                        "balance_cny": "",
                        "message": f"Balance check returned HTTP {resp.status} — proceeding anyway.",
                    }
                data = await resp.json()
    except Exception as e:
        print(f"[DEEPSEEK-BALANCE] Request failed: {e}")
        return {
            "ok": True,  # Don't block on network errors
            "balance_cny": "",
            "message": f"Balance check failed ({e}) — proceeding anyway.",
        }

    is_available = data.get("is_available", True)
    infos = data.get("balance_infos", [])
    # Find CNY entry (DeepSeek's primary currency)
    cny_info = next((b for b in infos if b.get("currency") == "CNY"), infos[0] if infos else {})
    total = cny_info.get("total_balance", "0")
    print(f"[DEEPSEEK-BALANCE] is_available={is_available}, total_balance={total} CNY")

    try:
        balance_float = float(total)
    except (ValueError, TypeError):
        balance_float = 0.0

    ok = is_available and balance_float > 0.0
    if ok:
        msg = f"✅ DeepSeek balance: **¥{total} CNY** (available)"
    else:
        msg = (
            f"⚠️ DeepSeek balance: **¥{total} CNY** — account {'unavailable' if not is_available else 'at zero'}. "
            f"Games will 402 immediately. Top up at platform.deepseek.com before running."
        )
    return {"ok": ok, "balance_cny": total, "message": msg}
