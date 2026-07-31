"""Trigger scanning + dispatch (ETB, dies, LTB, attack, upkeep, end step, cast, landfall).

Fifteen free functions extracted from GameEngine. Together they handle
ALL trigger scanning in the engine: every time a state change happens
(creature enters, attacks, dies, leaves the battlefield, etc.), one of
these functions walks the relevant battlefield cards looking for triggers
that should fire and queues them for resolution.

Each function takes the GameEngine instance as `engine` (first arg) and
the rest of the parameters match the original method signature. The
GameEngine class keeps thin delegator methods so existing callers like
`engine._check_creature_etb_triggers_sync(...)` work unchanged.

Public free functions (each takes a GameEngine instance as first arg):

    drain_pending_triggers          (async)
    _check_creature_etb_triggers_sync
    _spell_matches_cast_trigger
    _check_cast_triggers            (async)
    _check_creature_etb_triggers    (async)
    _check_dies_triggers_sync
    _check_ltb_triggers_sync
    _check_attack_triggers_sync
    _check_day_night_and_werewolf_transforms
    _check_upkeep_triggers_sync
    _check_end_step_triggers_sync
    _place_triggers_on_stack
    _handle_etb_triggers
    _handle_land_etb
    process_attack_triggers

State touched on `engine`:

    engine.rules                — RulesEngine instance for SBA / actions
    engine.draw_cards           — GameEngine method for cantrip triggers
    engine._queue_async_trigger — queues triggers for async resolution
    engine.spell_resolver       — Tier 2 spell resolver
    engine._xmage_translator    — XMage bridge action translator
    engine.resolve_special_effects — Tier 1 hardcoded effects
    engine._is_colorless_card   — color identity helper
    engine._should_emit_resolve_prompt — debug log gate

Internal cross-calls within this module go through engine.X (the facade)
so behavior is identical to pre-refactor.

Extracted from mtg/engine.py during the Phase 2 OSS-readability refactor
(Phase 2E).
"""

import asyncio
import json
import random
import re
from typing import Any, Dict, List, Optional, Tuple

from mtg.constants import Phase, Zone, COMMAND_ZONE_FORMATS, MELD_PAIRS
from mtg.helpers import _collapse_repeated_life_gain, _should_emit_resolve_hint, sanitize_oracle_for_display, format_trigger_line, names_match
from mtg.models import Card, Player, GameState, StackEntry
from mtg.util import maybe_reraise
from mtg import events


# Reasons that are pure internal status (templates that intentionally no-op
# because the engine doesn't model the underlying mechanic). These shouldn't
# reach Discord — they're noise. Console-log only.
_INTERNAL_NOOP_PATTERNS = (
    "library order not modeled",
    "not yet wired",
    "library not modeled",
    "auto-equip handled by equipment system",
    "static -1/-1 to all creatures handled by layers engine",
    # Apr 29 audit: classic werewolf upkeep transform is auto-resolved by
    # _check_day_night_and_werewolf_transforms. The template hint is noisy
    # and never actionable for the player.
    "day/night transform — check if",
    "use !fix transform",
    # May 24 audit fix: loosen substring match to catch all "handled by
    # equipment system" variants (e.g., "Hammer of Nazahn: handled by
    # equipment system" without the "auto-equip" prefix). 7 instances
    # in the May 24 batch leaked dev-language to Discord.
    "handled by equipment system",
    # Companion patterns observed in May 24 batch: spells.py:1200 emits
    # "<card> resolves (no further state change)" for cantrips/utility
    # spells when no observable game-state change happened. These read as
    # dev-language to players. Suppressed at the noop-formatter level.
    "no further state change",
    # Library-reorder dev-language was the other half of the May 24 leak.
    # The fix in spells.py models the reorder properly, but if anything
    # else still emits the legacy text, suppress it here too.
    "library reordering is not modeled",
    # June 10 audit (V31b): the dies-trigger no_action path leaked
    # "Undying is handled mechanically by the SBA engine" to Discord in 4
    # games — the May 30 suppression only covered the two spells.py sites.
    "handled mechanically",
    "handled by the sba engine",
    "sba engine",
)


def _format_noop_reason(card_name: str, reason: str) -> Optional[str]:
    """Build a Discord-safe message for a no_action template result.

    Returns None when the reason should be SUPPRESSED (pure internal status,
    "library order not modeled" etc.). Otherwise returns a clean message
    with no doubled card names — if `reason` already starts with the card
    name, we don't add another prefix.
    """
    if not reason:
        return None
    reason_lower = reason.lower()
    # Suppress internal-only status pings entirely.
    if any(p in reason_lower for p in _INTERNAL_NOOP_PATTERNS):
        return None
    # Strip any leading "<card name>: " from the reason so we don't double-print
    # the card name. Tolerant of casing differences.
    prefix = f"{card_name}:"
    if reason.lower().startswith(prefix.lower()):
        reason = reason[len(prefix):].lstrip()
    return f"📍 {card_name}: {reason}" if reason else None

# Optional: Tier 2 spell resolver (TargetMode used by some trigger handlers)
try:
    from rules import SpellResolver, TargetMode, ExecutionContext
    HAS_SPELL_RESOLVER = True
except ImportError:
    HAS_SPELL_RESOLVER = False

# Optional: Tier 1.5 effect templates (the main trigger resolution path)
try:
    from rules.effect_templates import get_effect_library, build_game_context
    HAS_EFFECT_TEMPLATES = True
except ImportError:
    HAS_EFFECT_TEMPLATES = False

# Optional: layers (granted abilities affect trigger conditions)
try:
    from rules.layers import Layer, create_pump_effect
    HAS_LAYERS_ENGINE = True
except ImportError:
    HAS_LAYERS_ENGINE = False

# Optional: replacement effects (interpose on trigger events)
try:
    from rules.replacement import GameEvent, EventType
    HAS_REPLACEMENT_ENGINE = True
except ImportError:
    HAS_REPLACEMENT_ENGINE = False

# Optional: targeting validation
try:
    from rules.targeting_helpers import (
        _validate_target_for_action, _validate_player_target_for_action,
    )
    HAS_TARGETING = True
except ImportError:
    HAS_TARGETING = False

# Optional: planeswalker for ability activation
try:
    from rules.planeswalker import PlaneswalkerManager
    HAS_PLANESWALKER = True
except ImportError:
    HAS_PLANESWALKER = False


def _is_self_etb_trigger_paragraph(card: Card, paragraph: str) -> bool:
    """Return whether *paragraph* is this permanent's own enter trigger.

    Scryfall's 2025 wording update changed many printed-name subjects to
    "this creature" (for example, Inferno/Frost Titan now say "Whenever
    this creature enters or attacks"). Keep this deliberately narrower
    than a generic "enters" test so ongoing Soul Warden/landfall triggers
    remain in their dedicated scanners.
    """
    text = (paragraph or "").lower().strip()
    if not text or "enters" not in text:
        return False
    if text.startswith("when ") and not text.startswith("whenever "):
        return True
    if not text.startswith("whenever "):
        return False

    full_name = re.escape((card.name or "").lower())
    short_name = re.escape((card.name or "").split(",", 1)[0].lower())
    subject = rf"(?:this (?:creature|permanent)|{full_name}|{short_name})"
    # July 20 batch-3 audit (reviewer V3): allow "X or another <type> you
    # control" between subject and "enters" — Hammer of Nazahn's own ETB
    # ("Whenever Hammer of Nazahn or another Equipment you control enters")
    # never classified as self-ETB, so its self-attach template never ran
    # (its watcher half for LATER entries is a separate scan, unaffected).
    # A card entering alongside its "or another" wording does trigger for
    # itself per CR 603.2.
    return bool(re.match(
        rf"whenever\s+{subject}(?:\s+or\s+another\s+[\w\s]+?)?\s+enters\b", text))


def _is_self_attack_trigger_paragraph(card: Card, paragraph: str) -> bool:
    """Return whether *paragraph* triggers when this card attacks."""
    text = (paragraph or "").lower().strip()
    # July 31 batch-11 (limited reviewer): ability-word prefixes ("Battalion
    # — Whenever this creature and at least two other creatures attack...")
    # defeated the startswith gate AND the subject regex, so the whole
    # Battalion class was silently dropped — never queued, never logged
    # (Boros Elite attacked three qualifying combats at printed power,
    # game_1532532194684436573). Ability words are flavor per CR 207.2c:
    # strip "<Word> — " before shape-checking, and allow the "and at least
    # N other creatures" subject extension.
    text = re.sub(r"^[a-z][\w'-]*(?: [\w'-]+)? — ", "", text)
    if not text.startswith("whenever ") or "attack" not in text:
        return False
    full_name = re.escape((card.name or "").lower())
    short_name = re.escape((card.name or "").split(",", 1)[0].lower())
    subject = rf"(?:this (?:creature|permanent)|{full_name}|{short_name})"
    return bool(re.match(
        rf"whenever\s+{subject}\s+(?:and at least \w+ other creatures?\s+)?"
        rf"(?:enters(?: the battlefield)?\s+or\s+)?attacks?\b",
        text,
    ))

# Optional: XMage bridge types (used in trigger discovery)
try:
    from rules.xmage_bridge import Permanent as XMagePermanent
    from rules.xmage_bridge import GameState as XMageGameState
    HAS_XMAGE_BRIDGE = True
except ImportError:
    HAS_XMAGE_BRIDGE = False


def _log_life_change(player, delta: int, source: str) -> None:
    """Emit the canonical [LIFE-GAIN]/[LIFE-LOSS] console tag that audit
    reconciliation greps for. May 30 audit: several inline trigger paths
    (cast-trigger gains — Sythis/Herald/Soul Warden; Blood Artist / Zulaport /
    Bastion dies-drains; Syr Konrad) applied life directly without these tags,
    so life-math reconciliation couldn't see them (one Sythis gain logged
    nothing at all). State was always correct; this only restores observability.
    Mirrors the format emitted by the gain_life/lose_life actions in actions.py."""
    try:
        if delta > 0:
            print(f"[LIFE-GAIN] {player.name}: +{delta} life → {player.life} ({source})")
        elif delta < 0:
            print(f"[LIFE-LOSS] {player.name}: {delta} life → {player.life} ({source})")
    except Exception:
        pass


async def drain_pending_triggers(engine, game: GameState) -> List[str]:
    """Drain the pending async trigger queue by calling Tier 3 resolve_effect.

    Should be called from every async caller of advance_phase / end_turn /
    cast_spell_async-equivalent paths, ideally immediately after the sync
    phase work completes. Returns the combined user-facing messages from
    all resolutions (already formatted by resolve_effect).

    Safe to call with an empty queue (returns []). Idempotent — clears the
    queue as it processes.
    """
    messages: List[str] = []
    # Madness first (Aug 1, 2026): discards redirected to exile by the sync
    # choke point (helpers.madness_discard_to_exile) resolve their
    # cast-or-graveyard choice at the first async opportunity — this drain
    # is the established one (15 call sites across cog/autoplay/engine).
    if getattr(game, '_madness_pending', None):
        from mtg.spells import resolve_pending_madness
        messages.extend(await resolve_pending_madness(engine, game))
    if not hasattr(game, 'pending_async_triggers') or not game.pending_async_triggers:
        return messages
    # Snapshot + clear so reentrant triggers enqueued during resolution
    # (e.g. dies-triggers chaining) accumulate for the NEXT drain, not this one.
    pending = list(game.pending_async_triggers)
    game.pending_async_triggers = []

    for entry in pending:
        src: Card = entry.get('source_card')
        trigger_text = entry.get('trigger_text', '')
        trigger_type = entry.get('trigger_type', 'trigger')
        controller_name = entry.get('controller_name', '')
        ctx = entry.get('context', '')
        if src is None or not trigger_text:
            continue
        # CR 117.5: SBAs check whenever a player would receive priority,
        # which is between every trigger resolution. If a player has
        # already lost during this drain (e.g. the previous Blood Artist
        # ticked them to 0), bail out — the rest of the queue is moot
        # and resolving them was the source of "loses the game (life: -7)"
        # cascades in the May 3 batch.
        if getattr(game, 'ended', False):
            print(f"[DRAIN-{trigger_type.upper()}] Game ended mid-drain; skipping remaining triggers")
            break
        if not engine.rules.client:
            # No Claude client — fall back to the old hint form so the human
            # at least sees what triggered.
            messages.append(
                format_trigger_line("📍", src.name, trigger_text, game=game, max_chars=300)
                + "\n  *(Use `!resolve` or `!fix` to handle — no AI client available)*"
            )
            continue
        print(f"[DRAIN-{trigger_type.upper()}] Resolving {src.name} via Tier 3")
        try:
            resolve_msgs, actions = await engine.rules.resolve_effect(
                game,
                effect_description=f"{trigger_text} ({trigger_type} trigger)",
                source_card=src.name,
                controller=controller_name,
                context=ctx or f"{trigger_type} trigger for {src.name}",
            )
            if actions:
                messages.extend(resolve_msgs)
                print(f"[DRAIN-{trigger_type.upper()}] {src.name}: executed {len(actions)} action(s)")
            else:
                # July 26 batch-7 audit: the "(suppressed)" label described the
                # CONSOLE print only — `messages.extend` ran unconditionally
                # above it, so a no-op Tier 3 resolution still posted its raw
                # explanation to Discord. game_1530445545447886909 shipped a
                # verbatim chain-of-thought: "He chooses to return the Elvish
                # Mystic? No, Elvish Mystic is not an artifact; ... So there is
                # no target, ability fizzles". Same policy as F6 Option D for
                # trigger emits: drop the prose, keep it on the console so an
                # audit can still recover it.
                for _m in resolve_msgs:
                    print(f"[RESOLVE-PROSE-DROPPED] {src.name}: {_m}")
                print(f"[DRAIN-{trigger_type.upper()}] {src.name}: no state change (suppressed)")
            # SBA check between resolutions (CR 117.5). If this trigger
            # killed someone, surface the loss now so the next iteration
            # of the loop bails out instead of resolving more drain
            # triggers against a dead player.
            sba_msgs = engine.rules.process_state_based_actions(game)
            if sba_msgs:
                messages.extend(sba_msgs)
        except Exception as e:
            print(f"[DRAIN-{trigger_type.upper()}] Error resolving {src.name}: {e}")
            # Surface a lightweight hint so the human can manually intervene
            messages.append(
                format_trigger_line("📍", src.name, trigger_text, game=game, max_chars=200)
                + f"\n  *(Tier 3 resolution failed — use `!resolve {src.name} {trigger_type} trigger` to handle)*"
            )
    return messages


def _check_creature_etb_triggers_sync(engine, game: GameState, entering_player: Player, entering_creature: Card) -> Tuple[List[str], List[Tuple]]:
    """Check for 'whenever another creature enters' triggers from ALL players.

    Scans all players' battlefields and resolves in APNAP order (active player
    first). When game.triggers_use_stack and stack_enabled, places triggers on
    stack via _place_triggers_on_stack instead of resolving inline.
    
    Returns:
        Tuple of (messages, unhandled_triggers) where unhandled_triggers is
        a list of (card, trigger_text) tuples that need async auto-resolution.
    """
    # (Slice 2c, July 24: the parity recorder that shadowed this scan was
    # retired after two clean batches — [EVENT-PARITY]=0 in 15296 and 15299.)
    messages = []
    messages.extend(_check_permanent_etb_watchers(
        engine, game, entering_player, entering_creature))
    unhandled = []
    entering_player_idx = game.players.index(entering_player) if entering_player in game.players else 0

    # Phase 1: Collect qualifying trigger sources from ALL players' battlefields
    _etb_collected = []  # (trigger_card, controller_player, ctrl_idx)
    for _ci, _cp in enumerate(game.players):
        for card in _cp.battlefield:
            if getattr(card, '_phased_out', False):
                continue  # Phased-out permanents don't trigger
            if not card.oracle_text:
                continue

            oracle_lower = card.oracle_text.lower()

            # Skip if no creature-enters trigger
            has_creature_enters = (
                ("whenever another creature" in oracle_lower and "enters" in oracle_lower) or
                ("whenever a creature" in oracle_lower and "enters" in oracle_lower) or
                ("whenever a nontoken creature" in oracle_lower and "enters" in oracle_lower)
            )
            if not has_creature_enters:
                continue

            # Skip the entering creature itself UNLESS the trigger says "or another"
            # (e.g. Gruul Ragebeast: "Whenever Gruul Ragebeast or another creature enters")
            if card.id == entering_creature.id:
                # Only allow engine-trigger if oracle says "[Name] or another"
                if "or another" not in oracle_lower:
                    continue

            # "whenever another creature you control" / "under your control" only
            # fires when the entering creature is controlled by the trigger's controller.
            # Covers: "another creature you control enters", "a creature enters the
            # battlefield under your control" (Aura Shards), etc.
            trigger_requires_your_control = (
                "another creature you control" in oracle_lower or
                "under your control" in oracle_lower or
                ("you control" in oracle_lower and "creature" in oracle_lower and "enters" in oracle_lower)
            )
            if trigger_requires_your_control and _ci != entering_player_idx:
                continue

            # Skip triggers already handled by _handle_etb_triggers hardcoded handlers
            handled_set = getattr(game, '_handled_triggers_this_etb', set())
            if card.name.lower() in handled_set:
                print(f"[TRIGGER-DEDUP] Skipping {card.name} -- already handled by hardcoded path")
                continue
            _etb_collected.append((card, _cp, _ci))

    if not _etb_collected:
        # July 20 batch-3 audit: the meld check must still run on the
        # no-watchers path (it used to sit below this return, so meld only
        # worked when a Soul Warden-class watcher was coincidentally in play).
        messages.extend(_check_meld_completion(game, entering_player, entering_creature))
        return messages, unhandled

    # Phase 2: Sort by APNAP (active player first) for stack placement order.
    # May 20 audit (APNAP-4): for INLINE resolution mode (the default — see
    # `triggers_use_stack` below), the order needs to be REVERSED so NAP
    # triggers resolve first per CR 603.3b LIFO. Stack placement puts AP on
    # the bottom and NAP on top; LIFO resolves top first. The sort below
    # produces AP-first order; the inline-resolution loop at the bottom
    # of this function reverses it. The stack-placement branch (Phase 3)
    # keeps the AP-first order since `_place_triggers_on_stack` already
    # respects CR APNAP stack-placement semantics.
    _ai = game.active_player_index
    _etb_collected.sort(key=lambda t: (0 if t[2] == _ai else 1, t[2]))
    # May 17 audit: tag was previously a bare "[ETB-APNAP]" which auditors
    # confused with non-ETB events that share this code path. Be explicit
    # about it being the creature-enters trigger scan and name the entering
    # creature unambiguously.
    print(f"[CREATURE-ENTERS-APNAP] {len(_etb_collected)} triggers from "
          f"{entering_creature.name} entering")

    # Phase 3: If stack enabled, place on stack and return
    if getattr(game, 'triggers_use_stack', False) and game.stack_enabled:
        _tinfos = []
        for card, _cp, _ci in _etb_collected:
            _tt = ""
            for paragraph in card.oracle_text.split('\n'):
                if "whenever" in paragraph.lower() and "creature" in paragraph.lower() and "enters" in paragraph.lower():
                    _tt = paragraph.strip()
                    break
            if _tt:
                _tinfos.append((card, _cp, _tt))
        if _tinfos:
            stack_msgs = engine._place_triggers_on_stack(game, _tinfos, "enters")
            messages.extend(stack_msgs)
        # Meld completion also applies on the stack-enabled path (July 20
        # batch-3 audit — see _check_meld_completion).
        messages.extend(_check_meld_completion(game, entering_player, entering_creature))
        return messages, unhandled

    # Phase 4: Resolve inline. May 20 audit (APNAP-4): iterate in REVERSE of
    # the AP-first sort so NAP triggers resolve first, matching CR 603.3b's
    # LIFO-after-stack-placement semantics in immediate mode. Previously
    # inline mode resolved AP-first, which inverted LIFO and could cause
    # cross-controller trigger interactions to read game state in the
    # wrong order (e.g., AP's ETB destroys something NAP's ETB was about
    # to read).
    for card, _ctrl_player, _ctrl_idx in reversed(_etb_collected):
        oracle_lower = card.oracle_text.lower()
        opponent = game.players[1 - _ctrl_idx]
        player_idx = _ctrl_idx

        handled = False
        
        # ---- HARDCODED HANDLERS (fast, no API) ----
        
        # Terror of the Peaks / Warstorm Surge: deals damage equal to entering creature's power
        if "deals damage equal to that creature's power" in oracle_lower or (
            "deals damage equal to" in oracle_lower and "power" in oracle_lower and "any target" in oracle_lower
        ):
            try:
                creature_power = entering_creature.get_effective_power(game) if hasattr(entering_creature, 'get_effective_power') else 0
            except (ValueError, TypeError):
                creature_power = 0

            if creature_power > 0:
                actual_dmg = engine.rules._apply_noncombat_damage_to_player(game, opponent, creature_power, card.name)
                messages.append(f"🔥 {card.name} deals {actual_dmg} damage to {opponent.name}!")

                if actual_dmg > 0 and opponent.life <= 0:
                    game.ended = True
                    game.winner = player_idx
                    messages.append(f"💀 {opponent.name} loses the game!")
            handled = True

        # Impact Tremors / Purphoros: deal fixed damage when creature enters
        elif "deals" in oracle_lower and "damage to each opponent" in oracle_lower:
            dmg_match = re.search(r'deals\s+(\d+)\s+damage', oracle_lower)
            if dmg_match:
                dmg = int(dmg_match.group(1))
                actual_dmg = engine.rules._apply_noncombat_damage_to_player(game, opponent, dmg, card.name)
                messages.append(f"🔥 {card.name} deals {actual_dmg} damage to {opponent.name}!")

                if actual_dmg > 0 and opponent.life <= 0:
                    game.ended = True
                    game.winner = player_idx
                    messages.append(f"💀 {opponent.name} loses the game!")
                handled = True
        
        # Selvala, Heart of the Wilds: draw only if entering creature has greatest power
        elif "selvala" in card.name.lower() and "power is greater than" in oracle_lower:
            try:
                enter_power = entering_creature.get_effective_power(game) if hasattr(entering_creature, 'get_effective_power') else 0
            except (ValueError, TypeError):
                enter_power = 0
            # Check if entering creature's power > each other creature you control
            is_greatest = True
            for other in _ctrl_player.battlefield:
                if other.id == entering_creature.id or not other.is_creature():
                    continue
                if other.id == card.id:
                    continue  # Don't compare to Selvala herself
                try:
                    other_power = other.get_effective_power(game) if hasattr(other, 'get_effective_power') else 0
                except (ValueError, TypeError):
                    other_power = 0
                if other_power >= enter_power:
                    is_greatest = False
                    break
            if is_greatest and enter_power > 0:
                drawn_cards = engine.draw_cards(_ctrl_player, 1, game=game)
                if drawn_cards:
                    messages.append(f"🃏 {card.name} — {_ctrl_player.name} draws a card")
            handled = True

        # Soul of the Harvest / Beast Whisperer / Garruk's Packleader / Garruk's Uprising: draw a card
        elif "draw a card" in oracle_lower:
            # Power-conditional triggers (Garruk's Uprising, Garruk's Packleader, Temur Ascendancy)
            # "power 4 or greater" / "power 4+" — only fire if entering creature has enough power
            power_match = re.search(r'power (\d+) or greater', oracle_lower)
            if power_match:
                required_power = int(power_match.group(1))
                entering_power = entering_creature.get_effective_power(game) if hasattr(entering_creature, 'get_effective_power') else (entering_creature.power or 0)
                if entering_power < required_power:
                    print(f"[TRIGGER] {card.name}: {entering_creature.name} power {entering_power} < {required_power}, skipping draw")
                    handled = True  # Mark handled to prevent fallthrough, but don't draw
                    continue  # Skip to next trigger card
            drawn_cards = engine.draw_cards(_ctrl_player, 1, game=game)
            if drawn_cards:
                messages.append(f"🃏 {card.name} — {_ctrl_player.name} draws a card")
            handled = True

        # Gruul Ragebeast / similar: "that creature fights target creature an opponent controls"
        elif "fights" in oracle_lower and ("target creature" in oracle_lower or "fights target" in oracle_lower):
            # The entering creature fights an opponent's creature
            # Pick the best target: biggest creature on opponent's board
            opp_creatures = [c for c in opponent.battlefield if c.is_creature()]
            if opp_creatures:
                # Sort by power descending — fight the biggest threat
                def creature_power(c):
                    try:
                        return c.get_effective_power(game) if hasattr(c, 'get_effective_power') else 0
                    except (ValueError, TypeError):
                        return 0
                target_creature = max(opp_creatures, key=creature_power)

                # Calculate powers for fight
                try:
                    enter_power = entering_creature.get_effective_power(game) if hasattr(entering_creature, 'get_effective_power') else 0
                except (ValueError, TypeError):
                    enter_power = 0
                target_power = creature_power(target_creature)

                # Each deals damage equal to its power to the other
                entering_creature.damage_marked += target_power
                target_creature.damage_marked += enter_power
                messages.append(f"⚔️ {card.name} — {entering_creature.name} fights {target_creature.name}!")
                messages.append(f"💥 {entering_creature.name} deals {enter_power} damage to {target_creature.name}")
                messages.append(f"💥 {target_creature.name} deals {target_power} damage to {entering_creature.name}")
            else:
                messages.append(f"⚔️ {card.name} — no opponent creatures to fight!")
            handled = True

        # Panharmonicon/Yarok doubling is handled in the engine-ETB resolution path
        # (see ~line 12560). Those cards don't have "whenever a creature enters"
        # in their oracle text, so they're never collected here — this branch
        # used to produce a misleading "triggers an additional time" message for
        # non-ETB triggers. Intentionally left empty.
        
        # ---- TIER 1.5: Try template library ----
        if not handled and HAS_EFFECT_TEMPLATES:
            trigger_text = ""
            for paragraph in card.oracle_text.split('\n'):
                p_lower = paragraph.lower().strip()
                if "whenever" in p_lower and "creature" in p_lower and "enters" in p_lower:
                    trigger_text = paragraph.strip()
                    break
            
            if trigger_text:
                try:
                    ctx = build_game_context(game, _ctrl_player, opponent,
                                            card=card, entering_creature=entering_creature,
                                            entering_player=entering_player)
                    ctx['_trigger_source'] = card.name
                    lib = get_effect_library()
                    actions, explanation = lib.resolve_etb(
                        card_name=card.name,
                        oracle_text=trigger_text,
                        controller=_ctrl_player.name,
                        opponent=opponent.name,
                        game_context=ctx,
                    )
                    
                    if actions is not None:  # [] = deliberate template no-op (handled); only None means unhandled → Tier 3
                        any_real_action = False
                        for action in actions:
                            action_type = action.get("action", "")
                            if action_type == "no_action":
                                continue
                            any_real_action = True
                            try:
                                msg = engine.rules._execute_action_on_state(game, action)
                                if msg:
                                    messages.append(msg)
                            except Exception as e:
                                print(f"[TEMPLATE-TRIGGER] Action failed for {card.name}: {e}")

                        # Mark handled even if all actions were no_action — the template
                        # resolved the trigger (just found nothing to target), so don't
                        # fall through to [TRIGGER-UNHANDLED].
                        handled = True
                        # Distinguish in the log between "trigger fired and did
                        # something" vs "trigger evaluated and skipped (condition
                        # not met / no valid target)". The audit-time signal for
                        # actual game-state changes was getting drowned in
                        # template-matched-but-no-op spam.
                        if any_real_action:
                            print(f"[TRIGGER-TEMPLATE] Resolved {card.name} trigger via template: {explanation}")
                        else:
                            no_op_reason = next(
                                (a.get("reason", "no condition met") for a in actions
                                 if a.get("action") == "no_action"),
                                "no condition met")
                            print(f"[TRIGGER-TEMPLATE-SKIP] {card.name}: {no_op_reason}")
                        # Prevent double-resolution
                        if hasattr(game, 'pending_resolves') and game.pending_resolves:
                            game.pending_resolves = [
                                pr for pr in game.pending_resolves
                                if card.name.lower() not in pr.lower()
                            ]
                except Exception as e:
                    print(f"[TEMPLATE-TRIGGER] Error for {card.name}: {e}")
        
        # ---- UNHANDLED: queue for async auto-resolve ----
        if not handled:
            trigger_text = ""
            for paragraph in card.oracle_text.split('\n'):
                p_lower = paragraph.lower().strip()
                if "whenever" in p_lower and "creature" in p_lower and "enters" in p_lower:
                    trigger_text = paragraph.strip()
                    break
            
            if trigger_text:
                print(f"[TRIGGER-UNHANDLED] {card.name}: {trigger_text[:150]}")
                unhandled.append((card, trigger_text))

    messages.extend(_check_meld_completion(game, entering_player, entering_creature))

    return messages, unhandled


def _check_meld_completion(game: 'GameState', entering_player: Player,
                           entering_creature: Card) -> List[str]:
    """[MELD] If the entering creature completes a known meld pair, meld them.

    July 20 batch-3 audit, two bugs in the old inline block: (1) pair_set is a
    frozenset key, and frozenset difference returns a frozenset — which has no
    .pop(); any meld half entering crashed the whole creature-ETB scan
    (game_1528960244212961350: Gisela, the Broken Blade). (2) The block sat
    below the scan's "no watchers collected" early return, so meld only ran
    when an unrelated Soul Warden-class watcher happened to be on a
    battlefield. Extracted so every exit path of the scan runs it.
    """
    messages: List[str] = []
    entering_name = entering_creature.name if entering_creature else ""
    if not entering_name or entering_creature not in entering_player.battlefield:
        return messages
    for pair_set, melded_data in MELD_PAIRS.items():
        if entering_name not in pair_set:
            continue
        other_name = next(iter(pair_set - {entering_name}), None)
        if not other_name:
            continue
        other_card = None
        for c in entering_player.battlefield:
            if c.name == other_name and c is not entering_creature:
                other_card = c
                break
        if other_card:
            game.unregister_static_effects(entering_creature)
            game.unregister_static_effects(other_card)
            entering_player.battlefield.remove(entering_creature)
            entering_player.battlefield.remove(other_card)
            entering_player.exile.append(entering_creature)
            entering_player.exile.append(other_card)
            melded = Card(
                name=melded_data["name"],
                mana_cost=melded_data.get("mana_cost", ""),
                type_line=melded_data.get("type_line", ""),
                oracle_text=melded_data.get("oracle_text", ""),
                power=melded_data.get("power"),
                toughness=melded_data.get("toughness"),
                loyalty=melded_data.get("loyalty"),
            )
            melded.owner_index = entering_creature.owner_index
            melded.entered_this_turn = True
            entering_player.battlefield.append(melded)
            messages.append(f"**{entering_creature.name}** and **{other_card.name}** meld into **{melded.name}**!")
            print(f"[MELD] {entering_creature.name} + {other_card.name} -> {melded.name}")
            break
    return messages


def _spell_matches_cast_trigger(engine, sentence_lower: str, card: Card,
                                 caster: Player = None, game: 'GameState' = None) -> bool:
    """Check if a cast spell matches a 'whenever you cast' trigger's type requirement.

    Returns False if the spell doesn't match (trigger should NOT fire).
    Returns True if the spell matches or no specific type restriction found.

    Handles: noncreature, creature, instant/sorcery, artifact, enchantment,
    Aura/Equipment/Vehicle (Sram), first spell (Rashmi), mana-value checks
    (Eidolon), no-mana-spent checks (Roiling Vortex).
    """
    # --- Adventure half is NOT a creature spell ---
    # When cast as adventure, the spell type is the adventure's type (instant/sorcery),
    # not the card's creature type. This prevents Chulane/Beast Whisperer from triggering.
    is_adventure_cast = getattr(card, 'cast_as_adventure', False)
    is_creature_spell = card.is_creature() and not is_adventure_cast

    # --- Spell subtype/type filters ---
    # "noncreature spell" — skip creatures (but adventure half IS noncreature)
    if 'noncreature spell' in sentence_lower and is_creature_spell:
        return False
    # "creature spell" (without "noncreature") — skip non-creatures
    if 'creature spell' in sentence_lower and 'noncreature' not in sentence_lower and not is_creature_spell:
        return False
    # "Adventure instant or sorcery spell" (Lucky Clover) / "creature spell
    # that has an Adventure" (Edgewall Innkeeper). June 11 audit: the word
    # "adventure" was never tested, so Lucky Clover fired on 10 of 13 plain
    # instant/sorcery casts in game 1514629231433351168 and the Tier-3 judge
    # then invented "Farseek, an Adventure instant" to justify illegal copies.
    if 'adventure' in sentence_lower and 'spell' in sentence_lower:
        _is_adventure_spell = (is_adventure_cast
                               or 'adventure' in (card.type_line or '').lower()
                               or bool(getattr(card, 'adventure_name', None)))
        if not _is_adventure_spell:
            return False
    # "instant or sorcery spell" / "an instant or sorcery"
    if re.search(r'\b(?:instant|sorcery)\b.*\b(?:instant|sorcery)\b', sentence_lower):
        if not (card.is_instant() or card.is_sorcery()):
            return False
    # "artifact spell"
    if 'artifact spell' in sentence_lower and not card.is_artifact():
        return False
    # "enchantment spell"
    if 'enchantment spell' in sentence_lower and not card.is_enchantment():
        return False

    # Specific subtype lists: "an Aura, Equipment, or Vehicle spell" (Sram)
    # Parse patterns like "a/an X, Y, or Z spell"
    subtype_match = re.search(
        r'(?:an?)\s+((?:[\w-]+(?:,\s+)?)+(?:,?\s*or\s+[\w-]+))\s+spell',
        sentence_lower
    )
    if subtype_match:
        type_text = subtype_match.group(1)
        # Split "aura, equipment, or vehicle" into ['aura', 'equipment', 'vehicle']
        subtypes = [t.strip().lower() for t in re.split(r',\s*(?:or\s+)?|\s+or\s+', type_text) if t.strip()]
        # Filter out generic words that aren't subtypes
        generic = {'a', 'an', 'your', 'first', 'next', 'that', 'this', 'each'}
        subtypes = [s for s in subtypes if s not in generic]
        if subtypes:
            # Check known type categories first
            type_checks = {
                'creature': card.is_creature(), 'noncreature': not card.is_creature(),
                'instant': card.is_instant(), 'sorcery': card.is_sorcery(),
                'artifact': card.is_artifact(), 'enchantment': card.is_enchantment(),
                'planeswalker': card.is_planeswalker(), 'land': card.is_land(),
            }
            card_type_lower = (card.type_line or '').lower()
            matches_any = False
            for st in subtypes:
                if st in type_checks:
                    if type_checks[st]:
                        matches_any = True
                        break
                elif st in card_type_lower:
                    # Check subtypes (Aura, Equipment, Vehicle, etc.) in type_line
                    matches_any = True
                    break
            if not matches_any:
                return False

    # --- "their first noncreature spell each turn" (Esper Sentinel) ---
    if 'first noncreature spell' in sentence_lower and ('each turn' in sentence_lower or 'of each turn' in sentence_lower):
        if caster:
            # noncreature_spells_cast_this_turn is incremented BEFORE this check,
            # so "first" means the count should be exactly 1
            if getattr(caster, 'noncreature_spells_cast_this_turn', 0) > 1:
                return False

    # --- "your first spell each turn" (Rashmi) ---
    if 'first spell' in sentence_lower and 'first noncreature spell' not in sentence_lower and ('each turn' in sentence_lower or 'of each turn' in sentence_lower):
        if caster:
            # spells_cast_this_turn is incremented BEFORE this check (line ~8047),
            # so "first spell" means the count should be exactly 1
            if getattr(caster, 'spells_cast_this_turn', 0) > 1:
                return False

    # --- "casts a spell from a graveyard" (Ash Zealot) ---
    # July 31 batch-10 reviewer (oathbreaker mirror): this branch was MISSING,
    # so Ash Zealot's "Whenever a player casts a spell from a graveyard" fell
    # through to the unconditional return True and fired 3 damage on every
    # ordinary hand cast — five times in game_1532409452295360512, deciding
    # the winner (Claude died at 0 instead of surviving at 6). The signal has
    # existed on the Card since July 29 (_cast_from_graveyard, set in
    # _validate_cast); the matcher just never read it. Scoped to the
    # CONDITION clause (before the first comma) so a trigger whose EFFECT
    # half mentions a graveyard ("…, return target card from your
    # graveyard") isn't wrongly gated.
    _cond_clause = sentence_lower.split(',', 1)[0]
    if re.search(r'from (?:a|your|their) graveyard', _cond_clause):
        if not getattr(card, '_cast_from_graveyard', False):
            return False

    # --- "no mana was spent" / "without paying" (Roiling Vortex) ---
    if 'no mana was spent' in sentence_lower or 'without paying' in sentence_lower:
        mana_paid = getattr(card, '_mana_paid', None)
        card_cmc = getattr(card, 'cmc', 0) or 0
        if mana_paid and mana_paid > 0:
            return False
        if not mana_paid and card_cmc > 0:
            return False  # Assume mana was spent if card has nonzero CMC

    # --- "mana value N or less" (Eidolon of the Great Revel) ---
    mv_match = re.search(r'mana value (\d+) or less', sentence_lower)
    if mv_match:
        threshold = int(mv_match.group(1))
        if int(card.cmc or 0) > threshold:
            return False

    # --- Heroic: "that targets this creature" / "that targets ~" ---
    # Heroic only triggers when the cast spell targets the creature. The skip
    # itself is a deliberate approximation, but until July 28 2026 it was
    # SILENT — a bare `return False` at all three call sites, which are bare
    # `continue`s, so the trigger never reached the [CAST-TRIGGER-UNHANDLED]
    # queue either and was invisible to every audit grep. An approximation
    # nobody can see is indistinguishable from a bug.
    if 'that targets' in sentence_lower:
        print(f"[HEROIC-SKIP] targeting-conditional cast trigger not evaluated: "
              f"{sentence_lower[:80]}")
        return False

    return True


def queue_unhandled_combat_damage(game: GameState, attacker: Card,
                                  attacker_owner: Player, damage_amount: int) -> None:
    """Queue an unmatched "deals combat damage to a player" trigger for the
    async Tier-3 drain.

    resolve_combat_damage is sync, so it cannot escalate to Tier 3 itself — and
    before July 28 2026 it also printed nothing and queued nothing when no
    template matched, so the trigger simply vanished. Ragavan, Nimble Pilferer's
    whole ability (Treasure token + impulse exile) disappeared on every connect,
    invisible to every audit grep because there was no tag to grep for. The
    sibling scans queue their unhandled tails (queue_unhandled_dies, the July 24
    sync cast bridge); this one now does too.
    """
    # July 31 batch-10 audit: extract from activated-ability-stripped text so
    # a quoted grant inside an activated line (Ascendant Spirit) can never be
    # queued as the trigger sentence — the detection in mtg/combat.py strips
    # too, but a card with BOTH a real combat trigger and such a line should
    # extract the real one.
    from rules.effect_templates import strip_activated_ability_lines
    oracle = strip_activated_ability_lines(
        getattr(attacker, 'oracle_text', '') or '')
    # July 30 batch-9 audit: split on newlines too — a keyword line ends with
    # a newline, not a period, so the old period-only split glued "Flying,
    # trample" onto the front of every extracted trigger sentence.
    sentence = next(
        (s.strip() for para in oracle.split('\n') for s in para.split('.')
         if 'combat damage to a player' in s.lower()
         or 'combat damage to an opponent' in s.lower()),
        oracle.strip())
    print(f"[COMBAT-TRIGGER-UNHANDLED] {attacker.name}: {sentence[:120]}")
    engine = getattr(game, '_rules_engine', None)
    engine = getattr(engine, 'engine_ref', None) if engine is not None else None
    if engine is None or not hasattr(engine, '_queue_async_trigger'):
        # No engine to drain it — the tag above is still the audit trail.
        return
    engine._queue_async_trigger(
        game, attacker, sentence, "combat_damage", attacker_owner.name,
        context=(f"{attacker.name} dealt {damage_amount} combat damage to a player"),
    )


async def _check_cast_triggers(engine, game: GameState, caster: Player, card: Card) -> List[str]:
    """
    Check for on-cast triggers — abilities that fire when a spell is cast,
    before it resolves. Common on Eldrazi ("When you cast Oblivion Sower...").
    These fire even if the spell is countered.
    """
    # Pub/sub slice 4a parity: record that the scan saw this cast so
    # A LIST, not a set — the same card object can be cast twice in a turn
    # (adventure half then creature half) and each cast needs its own record.
    messages = []
    oracle = card.oracle_text or ''
    oracle_lower = oracle.lower()

    # Apr 30 audit fix: strip parenthesized reminder text before the cast-trigger
    # detection. Cascade reminder text "(When you cast this spell, exile ...)" was
    # being matched as an engine-cast trigger, which produced "🎯 Cast trigger —
    # Bloodbraid Elf: )" in Discord (the closing paren was the only sentence with
    # the period inside the parens removed). The actual Cascade keyword is handled
    # by its own block below; the reminder text is descriptive only.
    oracle_no_reminder = re.sub(r'\([^)]*\)', '', oracle)
    oracle_no_reminder_lower = oracle_no_reminder.lower()

    # Check the CAST card itself for "When you cast this spell" engine-triggers
    # These are Eldrazi-style triggers: "When you cast this spell, ..."
    # NOT ongoing "whenever you cast a [type] spell" (e.g. Magecraft) — those
    # are battlefield triggers handled by the battlefield scan below.
    card_name_lower = card.name.lower()
    has_self_cast_trigger = False
    if 'when you cast' in oracle_no_reminder_lower or 'whenever you cast' in oracle_no_reminder_lower:
        for sentence in oracle_no_reminder.split('.'):
            sl = sentence.lower().strip()
            if 'when you cast' not in sl and 'whenever you cast' not in sl:
                continue
            # It's a engine-cast trigger if it says "when you cast this spell" or "when you cast [cardname]"
            if ('this spell' in sl or card_name_lower in sl):
                has_self_cast_trigger = True
                break
            # If it says "whenever you cast a/an [type] spell" that's an ongoing trigger, skip.
            # July 20 batch-3 audit (reviewer V2): Rashmi's "Whenever you cast
            # your first spell each turn" matched neither the a/an exclusion
            # nor the self-name check, so the fallback fired her battlefield
            # trigger off her OWN casting while she was still on the stack
            # (CR 603.3a — a permanent's triggered ability functions only on
            # the battlefield unless it says "when you cast this spell").
            # Cover the other ongoing determiners too.
            if re.search(r'whenever you cast (?:or copy )?(?:a|an|your|another|the|one or more)\b', sl):
                continue
            # Fallback: treat as engine-cast trigger
            has_self_cast_trigger = True
            break
    if has_self_cast_trigger:
        # Extract the cast trigger paragraph (full multi-sentence ability text).
        # Apr 30 audit: Rashmi's trigger spans 3 sentences; using only the first
        # truncated the text mid-effect ("reveal the top card of your library"
        # without the "if it's a nonland card with mana value less..." clause).
        # We pick the paragraph (newline-separated section) containing the
        # "when/whenever you cast" cue, fall back to first sentence if no paragraph
        # boundary is helpful.
        trigger_text = ""
        for paragraph in oracle_no_reminder.split('\n'):
            p_lower = paragraph.lower().strip()
            if ('when you cast' in p_lower or 'whenever you cast' in p_lower) and \
                    not re.search(r'whenever you cast (?:or copy )?(?:a|an)\b', p_lower):
                trigger_text = paragraph.strip()
                break
        if not trigger_text:
            # Fallback: first sentence
            for sentence in oracle_no_reminder.split('.'):
                sentence_lower = sentence.lower().strip()
                if 'when you cast' in sentence_lower or 'whenever you cast' in sentence_lower:
                    if re.search(r'whenever you cast (?:or copy )?(?:a|an)\b', sentence_lower):
                        continue
                    trigger_text = sentence.strip()
                    break
        # Original loop for handler dispatch (sentence-level processing remains)
        for sentence in oracle.split('.'):
            sentence_lower = sentence.lower().strip()
            if 'when you cast' not in sentence_lower and 'whenever you cast' not in sentence_lower:
                continue
            # Skip ongoing "whenever you cast a [type] spell" triggers (Magecraft, etc.)
            if re.search(r'whenever you cast (?:or copy )?(?:a|an)\b', sentence_lower):
                continue
            print(f"[CAST-TRIGGER] {card.name}: {trigger_text}")

            # Try hardcoded handlers first for known patterns
            handled = False
            caster_idx = game.players.index(caster) if caster in game.players else 0
            opponent_idx = 1 - caster_idx
            opponent = game.players[opponent_idx]

            # "target opponent exiles the top N cards of their library.
            #  You may put any number of land cards ... onto the battlefield"
            exile_top_match = re.search(r'exiles? the top (\w+) cards? of their library', sentence_lower)
            if exile_top_match and 'land' in oracle_lower and 'onto the battlefield' in oracle_lower:
                num_word = exile_top_match.group(1)
                num_map = {'one': 1, 'two': 2, 'three': 3, 'four': 4, 'five': 5, 'six': 6}
                num = num_map.get(num_word, int(num_word) if num_word.isdigit() else 4)
                exiled_cards = []
                for _ in range(num):
                    if opponent.library:
                        c = opponent.library.pop(0)
                        opponent.exile.append(c)
                        exiled_cards.append(c)
                messages.append(f"📤 {opponent.name} exiles top {num}: {', '.join(c.name for c in exiled_cards)}")
                # Put any land cards from those exiled onto caster's battlefield
                lands_taken = [c for c in exiled_cards if c.is_land()]
                for land in lands_taken:
                    opponent.exile.remove(land)
                    land.tapped = False
                    land.entered_this_turn = True
                    caster.battlefield.append(land)
                if lands_taken:
                    messages.append(f"🌍 {caster.name} takes: {', '.join(c.name for c in lands_taken)}")
                else:
                    messages.append(f"🌍 No land cards among the exiled cards")
                handled = True

            if not handled:
                # Try to resolve via Claude API (these are complex effects)
                if engine.rules.client:
                    try:
                        effect_msgs, executed = await engine.rules.resolve_effect(
                            game, f"Cast trigger for {card.name}: {trigger_text}",
                            card.name, caster.name
                        )
                        if effect_msgs and executed:
                            messages.extend(effect_msgs)
                        else:
                            messages.append(f"🎯 **Cast trigger** — {card.name}: {trigger_text}")
                    except Exception as e:
                        print(f"[CAST-TRIGGER] Error resolving {card.name}: {e}")
                        messages.append(f"🎯 **Cast trigger** — {card.name}: {trigger_text}")
                else:
                    messages.append(f"🎯 **Cast trigger** — {card.name}: {trigger_text}")
            break  # Only first cast trigger sentence

    # =================================================================
    # CASCADE
    # Exile cards from top of library until you exile a nonland card with
    # lesser mana value, cast that card for free, bottom the rest randomly.
    # =================================================================
    # July 21 batch audit (R4-1): the old blind `'cascade' in oracle_lower`
    # substring made Yidris, Maelstrom Wielder SELF-cascade on cast — his
    # text only GRANTS cascade to other spells ("...as you cast spells from
    # your hand this turn, they gain cascade"), yet a Counterspell was
    # exiled off the top and cast free (game_1529168842905882755). Only
    # KEYWORD LINES count: after stripping reminder text, a line consisting
    # solely of comma-separated keywords ("Cascade, cascade", "Trample").
    # Grant clauses ("they gain cascade", "spells you cast have cascade")
    # never form such a line.
    _cascade_count = 0
    if 'cascade' in oracle_lower:
        _stripped_oracle = re.sub(r'\([^)]*\)', '', oracle_lower)
        for _line in _stripped_oracle.split('\n'):
            for _part in _line.split(','):
                if _part.strip().strip('.').strip() == 'cascade':
                    _cascade_count += 1
    # July 31 batch-10: Yidris, Maelstrom Wielder's grant — "as you cast
    # spells from your hand this turn, they gain cascade". Recorded by the
    # grant_hand_cascade action when his combat-damage template resolves;
    # hand casts only (graveyard/escape and cascade free-casts excluded —
    # the grant's own printed scope plus the no-self-recursion guard).
    if (_cascade_count == 0
            and game._hand_cascade_grants.get(caster.name) == game.turn_number
            and not getattr(card, '_cast_from_graveyard', False)
            and not getattr(card, '_from_cascade', False)):
        _cascade_count = 1
        print(f"[CASCADE-GRANT] {card.name} gains cascade (Yidris grant, "
              f"hand cast this turn)")
    if _cascade_count > 0 and not getattr(card, '_cascade_done', False):
        card._cascade_done = True  # Prevent re-entry if _check_cast_triggers called multiple times
        # Apex Devastator: "Cascade, cascade, cascade, cascade" = 4 cascades
        # Maelstrom Wanderer: "Cascade, cascade" = 2 cascades
        cascade_count = _cascade_count
        caster_cmc = card.cmc
        print(f"[CASCADE] {card.name} cascading {cascade_count}x (CMC {caster_cmc})")

        for cascade_num in range(cascade_count):
            exiled_cards = []
            found_card = None
            # Exile from top until nonland with CMC < caster CMC
            while caster.library:
                top_card = caster.library.pop(0)
                exiled_cards.append(top_card)
                if not top_card.is_land() and top_card.cmc < caster_cmc:
                    found_card = top_card
                    break

            if found_card:
                exiled_cards.remove(found_card)
                # Cast announcement first (top-down: what happened), exile details after.
                # Include the source name so when the parent spell was countered
                # but the cast-trigger cascade still resolves (CR 603.2 — the
                # trigger is on the stack above the spell), the player can see
                # WHICH cast triggered the cascade. Without this, a countered
                # Zhulodok looked like cascade just appeared from nowhere.
                messages.append(
                    f"🌀 **Cascade {cascade_num + 1}/{cascade_count}** "
                    f"(from {card.name}'s cast trigger) "
                    f"— casts **{found_card.name}** (CMC {found_card.cmc}) "
                    f"· exiled {len(exiled_cards) + 1} card(s)"
                )
                print(f"[CASCADE-SPELL] {card.name} cascade found {found_card.name} (CMC {found_card.cmc}, type: {found_card.type_line})")

                # "Cast" the found card for free
                if found_card.is_creature():
                    # Put creature onto battlefield
                    found_card.summoning_sick = True
                    found_card.entered_this_turn = True
                    # Mark as cascade-cast so async Tier 3 doesn't fire phantom effects
                    found_card._from_cascade = True
                    caster.battlefield.append(found_card)
                    messages.append(f"  → **{found_card.name}** enters the battlefield")
                    # Slice 2b (July 21): this cascade free-cast path never
                    # emitted PERMANENT_ENTERED (emit-side gap invisible to
                    # the parity net). The emit drives the watcher dispatch
                    # (_from_cascade set above keeps the Tier-3 guard);
                    # drain in place.
                    events.emit(events.PERMANENT_ENTERED, game,
                                card=found_card, controller=caster,
                                via="cascade", rules=engine.rules)
                    from mtg.helpers import drain_pending_messages as _drain_pm
                    messages.extend(_drain_pm(game))

                    # Bug #17: Also try Tier 1.5 templates for engine-ETB on cascaded creatures
                    if found_card.oracle_text and 'enters' in found_card.oracle_text.lower():
                        try:
                            # Use module-level get_effect_library (don't re-import locally — shadows the
                            # module-level import and causes UnboundLocalError later in this function)
                            lib = get_effect_library()
                            etb_text = found_card.oracle_text
                            opponent_name = game.players[1 - game.players.index(caster)].name if len(game.players) > 1 else "Opponent"
                            cascade_actions, cascade_desc = lib.resolve_etb(found_card.name, etb_text, caster.name, opponent_name)
                            if cascade_actions and not any(a.get('action') == 'no_action' for a in cascade_actions):
                                print(f"[CASCADE-ETB] Tier 1.5 resolved {found_card.name}: {cascade_desc}")
                                for action in cascade_actions:
                                    msg = engine.rules._execute_action_on_state(game, action)
                                    if msg:
                                        # May 20 audit: was emitting "[CASCADE-ETB]" log tag
                                        # into the Discord-bound message list.
                                        messages.append(f"  {msg}")
                        except Exception as e:
                            print(f"[CASCADE-ETB] Template resolution failed for {found_card.name}: {e}")

                elif found_card.is_land():
                    # Shouldn't happen (lands are skipped), but just in case
                    caster.battlefield.append(found_card)
                    messages.append(f"🌍 Cascade plays **{found_card.name}**")
                elif (found_card.is_artifact() or found_card.is_enchantment()
                      or found_card.is_planeswalker()):
                    # Non-creature permanents (Equipment, Artifacts, Enchantments,
                    # Planeswalkers) ENTER THE BATTLEFIELD when cascaded into,
                    # they don't resolve as spells and go to graveyard. Without
                    # this branch Swiftfoot Boots / Sword of X went to graveyard
                    # silently and emitted a `(use !judge resolve ...)` prompt.
                    found_card.summoning_sick = False  # Non-creatures don't have summoning sickness
                    found_card.entered_this_turn = True
                    found_card._from_cascade = True
                    if found_card.is_planeswalker() and hasattr(found_card, 'loyalty') and found_card.loyalty:
                        try:
                            found_card.current_loyalty = int(found_card.loyalty)
                        except (ValueError, TypeError):
                            pass
                    caster.battlefield.append(found_card)
                    messages.append(f"  → **{found_card.name}** enters the battlefield")
                    # Register static effects (anthems, keyword grants, etc.)
                    try:
                        game.register_static_keyword_grants(found_card, caster.name)
                        game.register_static_pt_effects(found_card, caster.name)
                        game.register_replacement_effects(found_card, caster.name)
                        game.recalculate_granted_keywords()
                        game.recalculate_power_toughness()
                    except Exception as e:
                        print(f"[CASCADE-ETB] Static-effect registration failed for {found_card.name}: {e}")
                    # Try ETB triggers / templates if any
                    if found_card.oracle_text and 'enters' in found_card.oracle_text.lower():
                        try:
                            lib = get_effect_library()
                            etb_text = found_card.oracle_text
                            opponent_name = game.players[1 - game.players.index(caster)].name if len(game.players) > 1 else "Opponent"
                            cascade_actions, cascade_desc = lib.resolve_etb(found_card.name, etb_text, caster.name, opponent_name)
                            if cascade_actions and not any(a.get('action') == 'no_action' for a in cascade_actions):
                                print(f"[CASCADE-ETB] Tier 1.5 resolved {found_card.name}: {cascade_desc}")
                                for action in cascade_actions:
                                    msg = engine.rules._execute_action_on_state(game, action)
                                    if msg:
                                        messages.append(f"  {msg}")
                        except Exception as e:
                            print(f"[CASCADE-ETB] Template resolution failed for {found_card.name}: {e}")
                else:
                    # Non-creature spell — try to resolve its effects
                    # First try Tier 1.5 spell templates
                    cascade_resolved = False
                    # July 30 batch-9 reviewer audit: counter-target spells
                    # cascaded into were half-resolved — the action loop below
                    # blocked only the counter_spell action, so Mana Drain's
                    # sibling schedule_delayed_trigger still granted the
                    # counter-contingent {C} bonus with nothing countered
                    # (game_1532232990367682571). Casting the hit is OPTIONAL
                    # (CR 702.85a "may cast"); a rational caster declines a
                    # counterspell whose only would-be target is their own
                    # cascading spell — technically legal, strategically
                    # suicidal. Decline BEFORE any tier resolves any part of
                    # it, and put the card on the library bottom like every
                    # other uncast cascade exile (the old paths wrongly sent
                    # it to the graveyard as if it had been cast).
                    if 'counter target' in (found_card.oracle_text or '').lower():
                        print(f"[CASCADE-SPELL] Declining to cast {found_card.name} — "
                              f"its only would-be target is the cascade source")
                        messages.append(f"  {found_card.name} — declined (nothing "
                                        f"worth countering); put on the bottom of the library")
                        caster.library.append(found_card)
                        cascade_resolved = True
                    if not cascade_resolved:
                        try:
                            # Use module-level get_effect_library (don't re-import locally — shadows the
                            # module-level import and causes UnboundLocalError later in this function)
                            lib = get_effect_library()
                            opp_idx = 1 - game.players.index(caster) if caster in game.players else 1
                            opponent = game.players[opp_idx] if 0 <= opp_idx < len(game.players) else None
                            opponent_name = opponent.name if opponent else "Opponent"
                            # Apr 30 audit: build the FULL game context (not a stub) so
                            # discard-target / damage-target / opponent_hand templates can
                            # actually pick a target. The previous {'stack_top_is_creature': ...}
                            # context made Inquisition of Kozilek resolve to no_action even
                            # though it has a working _gen_inquisition template.
                            if opponent:
                                cascade_ctx = build_game_context(game, caster, opponent, card=found_card)
                            else:
                                cascade_ctx = {}
                            cascade_ctx['stack_top_is_creature'] = card.is_creature()
                            cascade_ctx['stack_top_type_known'] = True
                            cascade_ctx['_from_cascade'] = True
                            spell_actions, spell_desc = lib.resolve_spell(found_card.name, found_card.oracle_text or '', caster.name, opponent_name, game_context=cascade_ctx)
                            if spell_actions and not any(a.get('action') == 'no_action' for a in spell_actions):
                                print(f"[CASCADE-SPELL] Tier 1.5 resolved {found_card.name}: {spell_desc}")
                                for action in spell_actions:
                                    # Belt-and-braces: the decline check above
                                    # preempts counter-target spells entirely,
                                    # but a template action list can still
                                    # carry a counter_spell (odd phrasings).
                                    if action.get('action') == 'counter_spell':
                                        print(f"[CASCADE-SPELL] Blocked counter_spell from {found_card.name} — "
                                              f"can't counter cascade source (no legal target)")
                                        messages.append(f"  {found_card.name} fizzles — no legal target to counter during cascade")
                                        continue
                                    msg = engine.rules._execute_action_on_state(game, action)
                                    if msg:
                                        messages.append(f"  {msg}")
                                caster.graveyard.append(found_card)
                                cascade_resolved = True
                        except Exception as e:
                            print(f"[CASCADE-SPELL] Template resolution failed: {e}")

                    if not cascade_resolved:
                        # Fall back to Tier 1 resolve_special_effects
                        effect_msgs = engine.resolve_special_effects(game, caster, found_card)
                        if effect_msgs:
                            messages.extend(effect_msgs)
                            caster.graveyard.append(found_card)
                            cascade_resolved = True

                    if not cascade_resolved:
                        # Fall back to Tier 2 SpellResolver (regex-based oracle parsing)
                        if engine.spell_resolver and found_card.oracle_text:
                            # June 10 deep-dive: mirror the Tier-1.5
                            # no-self-counter guard above — during cascade the
                            # only stack object is the cascade source, so a
                            # cascaded counterspell resolved at Tier 2
                            # countered its own parent (Bloodbraid Elf
                            # 4-for-0'd its caster via Mana Leak).
                            if 'counter target' in (found_card.oracle_text or '').lower():
                                print(f"[CASCADE-SPELL] Skipping Tier 2 for {found_card.name} — "
                                      f"can't counter cascade source (no legal target)")
                                messages.append(f"  {found_card.name} fizzles — no legal target to counter during cascade")
                                caster.graveyard.append(found_card)
                                cascade_resolved = True
                        if not cascade_resolved and engine.spell_resolver and found_card.oracle_text:
                            try:
                                sr_result = await engine.spell_resolver.cast_spell(
                                    game, caster, found_card, target=None, target_mode=TargetMode.AUTO
                                )
                                sr_msgs = sr_result.messages
                                if sr_msgs and not any("complex effect" in m.lower() for m in sr_msgs) and not any("not automated" in m for m in sr_msgs):
                                    messages.extend(sr_msgs)
                                    caster.graveyard.append(found_card)
                                    cascade_resolved = True
                                    print(f"[CASCADE-SPELL] Tier 2 SpellResolver resolved {found_card.name}")
                            except Exception as e:
                                print(f"[CASCADE-SPELL] SpellResolver failed for {found_card.name}: {e}")

                    if not cascade_resolved:
                        # Can't auto-resolve — put to graveyard with a note
                        caster.graveyard.append(found_card)
                        if _should_emit_resolve_hint(game, f"cascade:{found_card.name}"):
                            messages.append(f"  (use `!judge resolve {found_card.name}` if effect needed)")

                # CR 702.85a — cascade puts the spell on the stack as if cast.
                # Fire cast-triggers (Eidolon, Rhystic Study, Esper Sentinel, prowess,
                # magecraft, recursive cascade) for the cascaded-into card.
                # Pub/sub slice 4a: CARD_CAST shadow emit for the cascade
                # free-cast (the second of the two live cast funnels).
                events.emit(events.CARD_CAST, game, card=found_card,
                            caster=caster, via="cascade", engine=engine)
                try:
                    cascade_cast_msgs = await _check_cast_triggers(engine, game, caster, found_card)
                    if cascade_cast_msgs:
                        messages.extend(cascade_cast_msgs)
                except Exception as e:
                    print(f"[CASCADE-CAST-TRIGGER] Error firing cast-triggers for {found_card.name}: {e}")
            else:
                messages.append(f"🌀 **Cascade {cascade_num + 1}/{cascade_count}** — no valid card found (exiled {len(exiled_cards)} cards)")

            # Put exiled cards on bottom of library in random order
            import random as _rng
            _rng.shuffle(exiled_cards)
            caster.library.extend(exiled_cards)

        if cascade_count > 0:
            print(f"[CASCADE] {card.name} finished {cascade_count} cascade(s)")

    # Also check OTHER permanents on the battlefield for "Whenever you/a player cast(s)" triggers
    for bf_card in list(caster.battlefield):
        if bf_card.id == card.id:
            continue  # Skip the card being cast
        # Skip planeswalkers — their oracle text contains loyalty abilities, not triggered abilities.
        # e.g. Jaya's "-8: You get an emblem with 'Whenever you cast...'" is NOT a cast trigger.
        if bf_card.is_planeswalker() and not bf_card.is_creature():
            continue
        bf_oracle = bf_card.oracle_text or ''
        # July 22, 2026: scan with reminder text (parentheticals) STRIPPED.
        # Reminder text only ever restates a keyword — it never carries a
        # distinct triggered ability — but Prowess's reminder
        # ("(Whenever you cast a noncreature spell, this creature gets
        # +1/+1 until end of turn.)") matched the generic scanner, whose
        # self-pump handler then deliberately skips Prowess cards (the
        # dedicated PROWESS block below owns them), leaving the trigger to
        # fall through to a wasteful Tier-3 queue. Monastery Swiftspear was
        # 12 of 18 [CAST-TRIGGER-UNHANDLED] tags in the July 21 batch: it
        # got correctly pumped by the dedicated block AND redundantly
        # escalated. Stripping parens makes a Prowess-only card skip this
        # scan entirely, while cards with a REAL un-parenthesized cast
        # trigger (Monastery Mentor's token half) still match.
        bf_oracle_scan = re.sub(r'\([^)]*\)', '', bf_oracle)
        bf_oracle_lower = bf_oracle_scan.lower()
        # July 31 batch-10 reviewer: Extort's ENTIRE trigger condition lives
        # inside its reminder text ("Extort (Whenever you cast a spell, you
        # may pay {W/B}...)") — there is no un-parenthesized printing. The
        # July 22 strip above (a Prowess fix) therefore silently killed the
        # whole keyword: Crypt Ghast and Blind Obedience never extorted
        # across three qualifying casts in game_1532415549039050783, a
        # regression from a June 10 fix that had made extort work. For
        # extort-bearing cards, scan the RAW oracle — the reminder text IS
        # the ability, which the Tier-1.5 \bextort\b pattern then resolves.
        if re.search(r'(?:^|\n)\s*extort\b', bf_oracle, flags=re.IGNORECASE):
            bf_oracle_scan = bf_oracle
            bf_oracle_lower = bf_oracle.lower()
        if 'whenever you cast' in bf_oracle_lower or 'whenever a player casts' in bf_oracle_lower:
            # Check if this trigger matches the spell type
            for sentence in bf_oracle_scan.split('.'):
                sentence_lower = sentence.lower().strip()
                if 'whenever you cast' not in sentence_lower and 'whenever a player casts' not in sentence_lower:
                    continue
                # Skip loyalty ability text (e.g. "−8: You get an emblem with...")
                # This catches creature-planeswalkers like Gideon
                if re.match(r'^[+\u2212\-]?\d+\s*:', sentence.strip()):
                    continue
                # Comprehensive type/condition matching (Sram, Rashmi, Eidolon, etc.)
                if not engine._spell_matches_cast_trigger(sentence_lower, card, caster, game):
                    continue
                # Apr 30 audit fix: prefer the full trigger paragraph over the
                # first sentence — Rashmi's trigger spans 3 sentences ("reveal
                # the top card... if it's a nonland card with mana value less...
                # otherwise put it into your hand") and showing only sentence 1
                # confused players who couldn't see the conditional.
                trigger_text = sentence.strip()
                for paragraph in bf_oracle.split('\n'):
                    p_lower = paragraph.lower().strip()
                    if 'whenever you cast' in p_lower or 'whenever a player casts' in p_lower:
                        # Pick paragraph that contains this exact match cue
                        if any(seg.strip().lower() == sentence_lower for seg in paragraph.split('.')):
                            trigger_text = paragraph.strip()
                            break
                print(f"[CAST-TRIGGER] {bf_card.name} triggers from casting {card.name}: {trigger_text}")
                # CR 603.2 ordering: cast triggers go on the stack ABOVE the
                # spell that triggered them. The spell is already on the
                # stack at this point (caller pushed it before calling us);
                # the LIFO resolution means the trigger resolves FIRST. The
                # bot fires the trigger's effect inline below because the
                # net game state is identical when stack_enabled is False
                # (autoplay default), but emit a [CAST-TRIGGER-STACK] log
                # so post-batch grep can verify ordering matches CR. When
                # priority.py is fully wired in (React frontend work, see
                # CLAUDE.md "Future" section), the stack push should become
                # a real StackEntry(is_spell=False) and the inline execution
                # below should move into the stack resolver's trigger handler.
                trigger_entry = None
                if getattr(game, 'stack_enabled', False):
                    try:
                        from mtg.models import StackEntry
                        ctrl_idx = game.players.index(caster) if caster in game.players else 0
                        trigger_entry = StackEntry(
                            card=bf_card,
                            controller_name=caster.name,
                            controller_index=ctrl_idx,
                            is_spell=False,
                            trigger_source=bf_card.name,
                            trigger_text=trigger_text,
                        )
                        game.stack.append(trigger_entry)
                        print(f"[CAST-TRIGGER-STACK] {bf_card.name}'s trigger pushed on stack "
                              f"above {card.name} (depth: {len(game.stack)}) — resolves first per CR 603.2")
                    except Exception as _se:
                        print(f"[CAST-TRIGGER-STACK] Push failed: {_se}")
                        trigger_entry = None

                # May 20 audit (APNAP-5): per CR 603.2, after the cast trigger
                # goes on the stack above the cast spell, the CASTER gets
                # priority and can respond (e.g., Stifle the trigger, Voidslime,
                # Trickbind, Disallow). Previously the engine resolved the
                # trigger inline without offering this window. Now: scan
                # caster's hand for trigger-countering instants; if any exist
                # and the priority infrastructure is available, open a window.
                # Otherwise fall through to inline resolution (the common case
                # — autoplay decks rarely include Stifle-shaped cards).
                if trigger_entry is not None and getattr(game, 'stack_enabled', False):
                    _caster_has_stifle = False
                    try:
                        for _hc in caster.hand:
                            if not _hc.oracle_text:
                                continue
                            _o = _hc.oracle_text.lower()
                            if ('counter target triggered ability' in _o
                                    or 'counter target activated or triggered ability' in _o):
                                _caster_has_stifle = True
                                break
                    except Exception:
                        pass
                    if _caster_has_stifle:
                        send_fn = getattr(game, '_stack_send_func', None)
                        if send_fn:
                            print(f"[CAST-TRIGGER-PRIORITY] {caster.name} has Stifle-shaped "
                                  f"card in hand — opening priority window for "
                                  f"{bf_card.name}'s trigger from cast of {card.name}")
                            # July 29 batch audit: expose the open window to
                            # the buried spell's LIFO wait loop (mtg/spells.py)
                            # — while this LLM evaluation runs, the spell at
                            # the bottom of the stack must keep waiting rather
                            # than burn its extension/rescue budget and
                            # resolve out of order beneath a live counter.
                            game._trigger_window_depth = getattr(game, '_trigger_window_depth', 0) + 1
                            try:
                                await engine._combat_priority_round(
                                    game, send_fn,
                                    f"trigger response: {bf_card.name} from {card.name}",
                                )
                            except Exception as _pe:
                                print(f"[CAST-TRIGGER-PRIORITY] window error: {_pe}")
                            finally:
                                game._trigger_window_depth = max(
                                    0, getattr(game, '_trigger_window_depth', 1) - 1)
                            # July 21 batch audit: "not on the stack anymore"
                            # is NOT proof of a counter — in
                            # game_1529172174773157998 the window's async
                            # timeout fired turns of churn later, the entry
                            # was long gone for unrelated reasons, and the
                            # trigger was declared countered with Stifle
                            # still sitting in Rick's hand (the Drake was
                            # never created). Only the counterspell handler's
                            # explicit `countered` flag counts; a vanished
                            # entry falls through to inline resolution.
                            if getattr(trigger_entry, 'countered', False):
                                print(f"[CAST-TRIGGER-COUNTERED] {bf_card.name}'s "
                                      f"trigger was countered during priority "
                                      f"window — skipping inline resolution")
                                if trigger_entry in game.stack:
                                    game.stack.remove(trigger_entry)
                                # Match the existing "one trigger per bf_card
                                # per cast" pattern — break out of the
                                # sentence loop so we move to the next
                                # bf_card rather than process another
                                # matching sentence.
                                break
                            if trigger_entry not in game.stack:
                                print(f"[CAST-TRIGGER-VANISHED] {bf_card.name}'s "
                                      f"trigger entry left the stack without a "
                                      f"counter (window churn) — resolving inline "
                                      f"anyway")

                # Try to execute common cast-trigger patterns inline
                executed_trigger = False

                # "discard a card, then draw a card" (Ashling Flame Dancer magecraft, etc.)
                if 'discard a card' in sentence_lower and 'draw a card' in sentence_lower:
                    caster_idx = game.players.index(caster) if caster in game.players else 0
                    if caster.is_claude:
                        # Auto-resolve for AI: discard worst card, draw
                        import random as _rng
                        if caster.hand:
                            # Discard a madness card first (Aug 1, CR 702.35
                            # — exiles + casts for its madness cost), else a
                            # land, else random.
                            from mtg.helpers import (madness_discard_to_exile,
                                                     parse_madness_cost)
                            _mad_in_hand = [c for c in caster.hand
                                            if parse_madness_cost(c.oracle_text or '')]
                            lands_in_hand = [c for c in caster.hand if c.is_land()]
                            discard_card = (_mad_in_hand[0] if _mad_in_hand
                                            else lands_in_hand[0] if lands_in_hand
                                            else _rng.choice(caster.hand))
                            caster.hand.remove(discard_card)
                            _mm = madness_discard_to_exile(game, caster, discard_card)
                            if _mm:
                                messages.append(_mm)
                            else:
                                caster.graveyard.append(discard_card)
                            drawn_cards = engine.draw_cards(caster, 1, game=game)
                            messages.append(f"⚡ {bf_card.name} — {caster.name} discards {discard_card.name}, draws a card")
                        executed_trigger = True
                    else:
                        # Human player: set pending action so they choose which card to discard
                        game.pending_action = {
                            'type': 'loot_discard_draw',
                            'player_idx': caster_idx,
                            'source': bf_card.name,
                        }
                        hand_names = ', '.join(c.name for c in caster.hand)
                        messages.append(
                            f"⚡ {bf_card.name} — {caster.name} must discard a card, then draw a card.\n"
                            f"  Use `!discard <card name>` to choose. Hand: {hand_names}"
                        )
                        executed_trigger = True

                # July 24 batch-6 audit (reviewer S2, CRITICAL): both token
                # branches below stopped the regex at "token(s)" and never
                # forwarded a trailing "with flying[, ...]" clause — every
                # Talrand Drake in game_1529988360263827656 was a vanilla 2/2
                # all game (blocked by ground creatures, blocked ground
                # attackers; CR 702.9b). Sibling of the May 17 ETB-path fix
                # (make_token_action forwards tok.keywords); the create_token
                # handler already accepts a keywords list.
                def _token_keywords(sent: str) -> list:
                    m = re.search(r'tokens? with ([\w\s,\'-]+?)(?:\.|$)', sent)
                    if not m:
                        return []
                    KNOWN = {
                        'flying', 'deathtouch', 'vigilance', 'trample',
                        'haste', 'lifelink', 'first strike', 'double strike',
                        'menace', 'reach', 'defender', 'flash', 'hexproof',
                        'indestructible', 'prowess',
                    }
                    parts = re.split(r',\s*|\s+and\s+', m.group(1))
                    return [p.strip().title() for p in parts
                            if p.strip() in KNOWN]

                # Token creation: "create X P/T [type] token(s), where X is that spell's mana value"
                # (Endrek Sahr, Master Breeder)
                if not executed_trigger and "where x is" in sentence_lower and "mana value" in sentence_lower:
                    x_token_match = re.search(r'create x (\d+)/(\d+) ([\w\s]+?)(?:creature )?tokens?', sentence_lower)
                    if x_token_match:
                        t_power = int(x_token_match.group(1))
                        t_tough = int(x_token_match.group(2))
                        t_type = x_token_match.group(3).strip().title()
                        # X = the cast spell's mana value
                        count = max(1, card.cmc or 0)
                        # July 20 batch-3 audit (reviewer S2): route through the
                        # create_token action — the inline Card() append skipped
                        # is_token, the creature-ETB watcher scan, AND the
                        # PERMANENT_ENTERED emit (invisible to the parity net).
                        tok_msg = engine.rules._execute_action_on_state(game, {
                            "action": "create_token", "player": caster.name,
                            "name": t_type, "power": t_power, "toughness": t_tough,
                            "types": f"Creature — {t_type}", "count": count,
                            "keywords": _token_keywords(sentence_lower),
                            "source": bf_card.name})
                        messages.append(f"⚡ {bf_card.name} creates {count} {t_power}/{t_tough} {t_type} token(s) (X = {card.name}'s mana value {card.cmc})")
                        if tok_msg:
                            messages.append(tok_msg)
                        executed_trigger = True

                # Token creation: "create a/N P/T [type] token(s)" (Talrand, Young Pyromancer)
                if not executed_trigger:
                    token_match = re.search(r'create (?:a |(\w+) )?(\d+)/(\d+) ([\w\s]+?)(?:creature )?tokens?', sentence_lower)
                    if token_match:
                        count_word = token_match.group(1)
                        _wtn = {'one': 1, 'two': 2, 'three': 3, 'four': 4, 'five': 5, 'six': 6, 'seven': 7, 'eight': 8}
                        count = _wtn.get(count_word, 1) if count_word and not count_word.isdigit() else (int(count_word) if count_word and count_word.isdigit() else 1)
                        t_power = int(token_match.group(2))
                        t_tough = int(token_match.group(3))
                        t_type = token_match.group(4).strip().title()
                        # July 20 batch-3 audit (reviewer S2): same routing as
                        # the X-token branch above — Sigil of the Empty
                        # Throne's Angels never triggered Aura Shards because
                        # this inline append bypassed the ETB scan entirely
                        # (game_1528957318224678980).
                        tok_msg = engine.rules._execute_action_on_state(game, {
                            "action": "create_token", "player": caster.name,
                            "name": t_type, "power": t_power, "toughness": t_tough,
                            "types": f"Creature — {t_type}", "count": count,
                            "keywords": _token_keywords(sentence_lower),
                            "source": bf_card.name})
                        messages.append(f"⚡ {bf_card.name} creates {count} {t_power}/{t_tough} {t_type} token(s)")
                        if tok_msg:
                            messages.append(tok_msg)
                        executed_trigger = True

                # Draw: "draw a card" (Archmage Emeritus, Beast Whisperer)
                if not executed_trigger:
                    if 'draw a card' in sentence_lower and 'discard' not in sentence_lower:
                        # Only handle it here when the draw IS the whole trigger.
                        # Chulane reads "draw a card, then you may put a land card
                        # from your hand onto the battlefield" — one sentence, so
                        # this partial match used to draw, set executed_trigger,
                        # and thereby suppress BOTH the Tier 1.5 lookup and the
                        # [CAST-TRIGGER-UNHANDLED] Tier-3 queue. The ramp half
                        # never fired in any observed game and never entered the
                        # backlog either: invisible to every audit grep. Same
                        # shape as the July 23 Dark Prophecy / Moldervine fixes —
                        # a partial handler must not swallow the clause it can't
                        # resolve. Falling through entirely (rather than drawing
                        # here and flagging it) keeps the whole trigger in one
                        # place and cannot double-draw.
                        _residual = (
                            'put a land', 'you may put', 'create', 'gain ', 'lose ',
                            'exile', 'destroy', 'return', 'search', 'scry',
                            'surveil', 'counter target', 'sacrifice',
                        )
                        if any(v in sentence_lower for v in _residual):
                            print(f"[CAST-TRIGGER-PARTIAL] {bf_card.name}: compound "
                                  f"trigger — left to the template/Tier-3 path "
                                  f"instead of resolving only the draw")
                        else:
                            drawn = engine.draw_cards(caster, 1, game=game)
                            if drawn:
                                messages.append(f"⚡ {bf_card.name} — {caster.name} draws a card")
                            executed_trigger = True

                # Damage: "deals N damage to each opponent/that player/any target"
                # Covers: Kessig Flamebreather, Eidolon of the Great Revel, Guttersnipe, etc.
                if not executed_trigger:
                    dmg_match = re.search(r'deals? (\d+) damage to (?:any target|each opponent|that player)', sentence_lower)
                    if dmg_match:
                        dmg = int(dmg_match.group(1))
                        caster_idx = game.players.index(caster) if caster in game.players else 0
                        # "that player" = the caster (for Eidolon-style triggers)
                        # "each opponent" = opponent of the trigger's controller
                        if 'that player' in sentence_lower:
                            target_player = caster  # Eidolon damages the caster of the spell
                        else:
                            target_player = game.players[1 - caster_idx]
                        actual = engine.rules._apply_noncombat_damage_to_player(game, target_player, dmg, bf_card.name)
                        messages.append(f"⚡ {bf_card.name} deals {actual} damage to {target_player.name}")
                        executed_trigger = True

                # Gain life: "gain N life" (trigger)
                # No `if not executed_trigger` guard here — life gain and draw are distinct
                # effects that can both appear in the same sentence (Sythis, Harvest's Hand)
                life_match = re.search(r'you gain (\d+) life', sentence_lower)
                if life_match:
                    life_amt = int(life_match.group(1))
                    caster.life += life_amt
                    # May 30 audit: emit the canonical [LIFE-GAIN] tag the audit
                    # reconciliation greps for — this cast-trigger path (Sythis,
                    # Herald of the Pantheon, Soul Warden family) applied life
                    # directly and one Sythis gain logged nothing at all.
                    _log_life_change(caster, life_amt, f"cast trigger: {bf_card.name}")
                    messages.append(f"⚡ {bf_card.name} — {caster.name} gains {life_amt} life")
                    executed_trigger = True

                # June 10 audit (V21): self-pump cast triggers (Kiln Fiend
                # "+3/+0", Wee Dragonauts class). These were announce-only —
                # the trigger printed oracle text and no pump ever applied
                # (Kiln Fiend attacked at power 1 when correct power 4 was
                # lethal). Mirrors the Prowess application below.
                if not executed_trigger and 'Prowess' not in (bf_card.keywords or []):
                    _pump_m = re.search(
                        r'(?:this creature|' + re.escape(bf_card.name.lower()) +
                        r') gets \+(\d+)/\+(\d+) until end of turn',
                        sentence_lower)
                    if _pump_m:
                        _sp_p, _sp_t = int(_pump_m.group(1)), int(_pump_m.group(2))
                        if not (HAS_LAYERS_ENGINE and game.layers_engine):
                            bf_card.power_modifier = getattr(bf_card, 'power_modifier', 0) + _sp_p
                            bf_card.toughness_modifier = getattr(bf_card, 'toughness_modifier', 0) + _sp_t
                        else:
                            _sp_effs = create_pump_effect(
                                source_name=f"{bf_card.name} (cast trigger)",
                                source_id=f"selfpump_{bf_card.id}_{card.id}",
                                controller=caster.name, target_id=bf_card.id,
                                power_mod=_sp_p, toughness_mod=_sp_t,
                                duration="end_of_turn")
                            for _pe in _sp_effs:
                                game.layers_engine.add_effect(_pe)
                        messages.append(f"⚡ **{bf_card.name}** gets +{_sp_p}/+{_sp_t} until end of turn")
                        print(f"[CAST-TRIGGER] {bf_card.name} self-pump +{_sp_p}/+{_sp_t} from casting {card.name}")
                        executed_trigger = True

                # Tier 1.5: Template library for cast triggers
                if not executed_trigger and HAS_EFFECT_TEMPLATES:
                    try:
                        caster_idx = game.players.index(caster) if caster in game.players else 0
                        opponent = game.players[1 - caster_idx]
                        ctx = build_game_context(game, caster, opponent, card=bf_card)
                        # Inject the triggering spell's mana value so templates like
                        # Shark Typhoon can size X/X tokens by the cast spell's MV.
                        # `card` here (the outer param of _check_cast_triggers) is the
                        # spell being cast; `bf_card` is the trigger source on battlefield.
                        try:
                            _cast_mv = getattr(card, '_mana_paid', None)
                            if _cast_mv is None or _cast_mv == 0:
                                _cast_mv = getattr(card, 'cmc', 0) or 0
                            ctx['cast_spell_mv'] = _cast_mv
                            ctx['spell_mv'] = _cast_mv
                        except Exception:
                            pass
                        lib = get_effect_library()
                        actions, explanation = lib.resolve_spell(
                            card_name=bf_card.name, oracle_text=trigger_text,
                            controller=caster.name, opponent=opponent.name,
                            game_context=ctx,
                        )
                        if actions is not None:
                            for action in actions:
                                if action.get("action") != "no_action":
                                    msg = engine.rules._execute_action_on_state(game, action)
                                    if msg:
                                        messages.append(f"⚡ {bf_card.name}: {msg}")
                            executed_trigger = True
                            print(f"[CAST-TRIGGER-TEMPLATE] {bf_card.name} resolved: {explanation}")
                    except Exception as e:
                        print(f"[CAST-TRIGGER-TEMPLATE] Error for {bf_card.name}: {e}")

                if not executed_trigger:
                    # May 16 audit: was emitting raw trigger_text, which (for
                    # Monastery Swiftspear and similar multi-keyword cards) is
                    # produced by `bf_oracle.split('.')` and ends up with
                    # truncated parentheticals. Route through format_trigger_line
                    # so reminder text is stripped AND repeated oracle text is
                    # collapsed to short form on the 2nd+ fire per game.
                    messages.append(
                        format_trigger_line("⚡", bf_card.name, trigger_text, game=game, max_chars=300)
                    )
                    # June 10 audit (V27): no inline handler + no template used
                    # to mean the trigger was ANNOUNCED then silently dropped
                    # (Crypt Ghast's extort was detected, pushed on the stack
                    # per CR 603.2, then discarded unexecuted). Queue it for
                    # Tier-3 auto-resolve like other unhandled trigger classes.
                    if hasattr(engine, '_queue_async_trigger'):
                        engine._queue_async_trigger(
                            game, bf_card, trigger_text, "cast_trigger", caster.name,
                            context=f"{caster.name} cast {card.name}")
                        print(f"[CAST-TRIGGER-UNHANDLED] {bf_card.name} queued for Tier 3 auto-resolve")
                # Emit resolve log at the actual moment of effect execution so
                # post-batch console-log audits see the CR-correct order
                # (trigger detection → trigger resolution → spell resolution).
                # Pair this with [CAST-TRIGGER-STACK] above.
                if executed_trigger:
                    print(f"[CAST-TRIGGER-RESOLVE] {bf_card.name} resolved before {card.name}")
                # Clean up the trigger StackEntry we pushed above. Because the
                # effect was applied inline (not via the stack resolver), we
                # have to remove it manually — otherwise [STACK] resolution
                # logs would show a phantom trigger entry sitting there until
                # the spell resolves.
                if getattr(game, 'stack_enabled', False):
                    for i in range(len(game.stack) - 1, -1, -1):
                        se = game.stack[i]
                        if (not getattr(se, 'is_spell', True)
                                and getattr(se, 'trigger_source', None) == bf_card.name
                                and getattr(se, 'trigger_text', None) == trigger_text):
                            del game.stack[i]
                            break
                break

    # === PROWESS (keyword trigger: whenever you cast a noncreature spell, +1/+1 until EOT) ===
    prowess_applied = False
    if not card.is_creature():
        for bf_card in list(caster.battlefield):
            if bf_card.is_creature() and 'Prowess' in (bf_card.keywords or []):
                if not (HAS_LAYERS_ENGINE and game.layers_engine):
                    bf_card.power_modifier = getattr(bf_card, 'power_modifier', 0) + 1
                    bf_card.toughness_modifier = getattr(bf_card, 'toughness_modifier', 0) + 1
                # [LAYERS] Register prowess pump as Layer 7c temporary effect
                if HAS_LAYERS_ENGINE and game.layers_engine:
                    _prowess_effs = create_pump_effect(
                        source_name=f"{bf_card.name} (Prowess)",
                        source_id=f"prowess_{bf_card.id}_{card.id}",
                        controller=caster.name, target_id=bf_card.id,
                        power_mod=1, toughness_mod=1, duration="end_of_turn")
                    for _pe in _prowess_effs:
                        game.layers_engine.add_effect(_pe)
                messages.append(f"⚡ **{bf_card.name}** prowess triggers (+1/+1 until end of turn)")
                print(f"[PROWESS] {bf_card.name} gets +1/+1 from casting {card.name}")
                prowess_applied = True
    if prowess_applied:
        # June 11 audit: layer effects were registered but their cached P/T
        # was not refreshed, so readers between cast-trigger resolution and
        # the next unrelated state change still saw printed stats.
        game.recalculate_power_toughness()

    # === OPPONENT CAST TRIGGERS (e.g. Rhystic Study, Esper Sentinel) ===
    # Scan OPPONENT's battlefield for "whenever an opponent casts a spell" triggers
    caster_idx = game.players.index(caster) if caster in game.players else 0
    for opp_idx, opp_player in enumerate(game.players):
        if opp_idx == caster_idx:
            continue  # Skip the caster — already scanned above
        for bf_card in list(opp_player.battlefield):
            if getattr(bf_card, '_phased_out', False):
                continue  # Phased-out permanents don't trigger
            bf_oracle = bf_card.oracle_text or ''
            bf_oracle_lower = bf_oracle.lower()
            # Match "whenever an opponent casts" or "whenever a player casts"
            if 'whenever an opponent casts' not in bf_oracle_lower and 'whenever a player casts' not in bf_oracle_lower:
                continue
            # Skip planeswalker loyalty text
            if bf_card.is_planeswalker() and not bf_card.is_creature():
                continue
            for sentence in bf_oracle.split('.'):
                sentence_lower = sentence.lower().strip()
                if 'whenever an opponent casts' not in sentence_lower and 'whenever a player casts' not in sentence_lower:
                    continue
                # Skip loyalty ability text
                if re.match(r'^[+\u2212\-]?\d+\s*:', sentence.strip()):
                    continue
                # Comprehensive type/condition matching (same helper as engine-cast)
                if not engine._spell_matches_cast_trigger(sentence_lower, card, caster, game):
                    continue
                trigger_text = sentence.strip()
                print(f"[OPP-CAST-TRIGGER] {bf_card.name} (controlled by {opp_player.name}) triggers from {caster.name} casting {card.name}: {trigger_text}")

                # Handle Rhystic Study inline: "draw a card unless that player pays {1}"
                if 'draw a card' in sentence_lower and ('unless' in sentence_lower or 'pay' in sentence_lower):
                    # Extract the tax amount (usually {1})
                    tax_match = re.search(r'pays?\s*\{(\d+)\}', sentence_lower)
                    tax_amount = int(tax_match.group(1)) if tax_match else 1

                    # In autoplay, caster pays the tax if they have mana available
                    caster_total_mana = sum(caster.mana_pool.values()) if hasattr(caster, 'mana_pool') else 0
                    # Also count untapped lands/rocks as available mana (mana pool might be empty)
                    caster_available = sum(1 for c in caster.battlefield
                                          if (c.is_land() or caster._can_produce_mana(c)) and not c.tapped)
                    should_pay = (caster_available >= tax_amount) if getattr(game, 'is_autoplay', False) else False

                    if should_pay:
                        # Caster pays — no card drawn
                        print(f"[OPP-CAST-TRIGGER] {bf_card.name}: {caster.name} pays {{{tax_amount}}} (has {caster_available} sources)")
                        messages.append(f"📖 {bf_card.name}: {caster.name} pays {{{tax_amount}}} — no card drawn")
                    else:
                        # Caster doesn't pay — opponent draws
                        drawn = engine.draw_cards(opp_player, 1, game=game)
                        if drawn:
                            print(f"[OPP-CAST-TRIGGER] {bf_card.name}: {opp_player.name} draws 1 card (opponent didn't pay)")
                            messages.append(f"📖 {bf_card.name}: {opp_player.name} draws a card ({caster.name} didn't pay {{{tax_amount}}})")
                        else:
                            messages.append(f"📖 {bf_card.name} triggers but {opp_player.name} has no cards to draw")
                # Handle Esper Sentinel: "draw a card unless that player pays {X}"
                elif 'draw a card' in sentence_lower:
                    drawn = engine.draw_cards(opp_player, 1, game=game)
                    if drawn:
                        print(f"[OPP-CAST-TRIGGER] {bf_card.name}: {opp_player.name} draws 1 card (hand: {len(opp_player.hand)})")
                        messages.append(f"📖 {bf_card.name}: {opp_player.name} draws a card")
                # Damage: "deals N damage to that player/each opponent" (Eidolon of the Great Revel, etc.)
                elif re.search(r'deals? (\d+) damage to (?:that player|each opponent|any target)', sentence_lower):
                    dmg_match = re.search(r'deals? (\d+) damage to', sentence_lower)
                    if dmg_match:
                        dmg = int(dmg_match.group(1))
                        # "that player" = the caster (who triggered Eidolon)
                        # "each opponent" = the caster (opponent of the controller)
                        target_player = caster
                        actual = engine.rules._apply_noncombat_damage_to_player(game, target_player, dmg, bf_card.name)
                        messages.append(f"⚡ {bf_card.name} deals {actual} damage to {target_player.name}")
                else:
                    # Generic opponent-cast trigger — try template library first
                    opp_trigger_resolved = False
                    if HAS_EFFECT_TEMPLATES:
                        try:
                            ctx = build_game_context(game, opp_player,
                                                     caster, card=bf_card)
                            lib = get_effect_library()
                            # May 14 audit: pass event_type="cast_trigger" so the
                            # name-keyed Kambal / Smothering Tithe / Rhystic Study
                            # templates (description starts "Whenever an opponent
                            # casts...") aren't skipped by the C1 whenever-guard.
                            actions, explanation = lib.resolve_etb(
                                card_name=bf_card.name, oracle_text=trigger_text,
                                controller=opp_player.name, opponent=caster.name,
                                game_context=ctx,
                                event_type="cast_trigger",
                            )
                            if actions is not None and any(a.get("action") != "no_action" for a in actions):
                                for action in actions:
                                    if action.get("action") != "no_action":
                                        msg = engine.rules._execute_action_on_state(game, action)
                                        if msg:
                                            # May 23 audit (MINOR #26): if the action's
                                            # message already attributes a DIFFERENT source
                                            # for a replacement effect (e.g. "Life gain
                                            # prevented for Claude (Erebos: prevent_lifegain)"
                                            # while bf_card is Blind Obedience), don't
                                            # double-attribute by prepending Blind Obedience.
                                            # The inner source attribution is the truthful one.
                                            # June 10 audit (V29): the old value pattern (\w+)
                                            # also matched the "(life: 37)" display suffix,
                                            # capturing the literal word "life" as the source —
                                            # "⚡ life: 🩸 Rick Deckard loses 2 life (life: 37)"
                                            # hid the real source (Kambal) in 7 games. Require
                                            # a word-shaped value (replacement ids like
                                            # prevent_lifegain) and exclude display keys.
                                            inner_source_match = re.search(
                                                r'\(([^():]+):\s*[a-z_]\w*\)',
                                                msg,
                                            )
                                            if inner_source_match and inner_source_match.group(1).strip().lower() in ('life', 'mana', 'x'):
                                                inner_source_match = None
                                            if inner_source_match and inner_source_match.group(1).strip().lower() != bf_card.name.lower():
                                                # Use the inner source as the prefix —
                                                # ⚡ Erebos, God of the Dead: 🚫 Life gain
                                                # prevented for Claude (...)
                                                inner_source = inner_source_match.group(1).strip()
                                                messages.append(f"⚡ {inner_source}: {msg}")
                                            else:
                                                messages.append(f"⚡ {bf_card.name}: {msg}")
                                opp_trigger_resolved = True
                                print(f"[OPP-CAST-TRIGGER-TEMPLATE] {bf_card.name} resolved: {explanation}")
                        except Exception as e:
                            print(f"[OPP-CAST-TRIGGER-TEMPLATE] Error for {bf_card.name}: {e}")
                    if not opp_trigger_resolved:
                        # Fallback: announce for manual resolution
                        if _should_emit_resolve_hint(game, f"opp_cast:{bf_card.name}"):
                            messages.append(
                                f"⚡ **{bf_card.name}** ({opp_player.name}) triggers from opponent casting {card.name}: {trigger_text}\n"
                                f"  (Use `!judge` to resolve if needed.)"
                            )
                break  # Only fire once per permanent

    return messages


def _dispatch_creature_entered(engine, game: GameState, controller: Player,
                               card: Card) -> List[str]:
    """Slice 2b (July 21, 2026): the single sync dispatch for a creature
    entering the battlefield — Tier 1/1.5 watcher scan, Tier 2.5 XMage
    translation, and Tier-3 QUEUEING (drained async by
    engine.drain_pending_triggers). This is what the PERMANENT_ENTERED
    subscriber runs; the former per-call-site scan invocations are gone.

    Behavior delta vs the legacy async wrapper (documented, accepted):
    Tier 3 creature-enters triggers are queued and drain at the next
    async boundary instead of resolving inline at ETB time.
    """
    messages, unhandled = engine._check_creature_etb_triggers_sync(
        game, controller, card)

    # Tier 2.5: XMage translator (sync JSON-RPC, ~10-50ms) — same pass the
    # legacy async wrapper ran.
    still_unhandled = []
    if unhandled and engine._xmage_translator:
        ctrl_idx = game.players.index(controller) if controller in game.players else 0
        opponent = game.players[1 - ctrl_idx]
        entering_power = 0
        try:
            entering_power = card.get_effective_power(game) if hasattr(card, 'get_effective_power') else 0
        except (ValueError, TypeError):
            pass
        for trigger_card, trigger_text in unhandled:
            try:
                ctx = build_game_context(game, controller, opponent,
                                         card=trigger_card, entering_creature=card)
                t_actions, t_expl = engine._xmage_translator.translate_trigger(
                    source_card=trigger_card.name,
                    ability_text=trigger_text,
                    controller=controller.name,
                    opponent=opponent.name,
                    game_context=ctx,
                    entering_creature_name=card.name,
                    entering_creature_power=entering_power,
                )
                if t_actions:
                    resolved_something = False
                    for action in t_actions:
                        if action.get("action") == "no_action":
                            continue
                        try:
                            msg = engine.rules._execute_action_on_state(game, action)
                            if msg:
                                messages.append(msg)
                                resolved_something = True
                        except Exception as e:
                            print(f"[XMAGE-TRIGGER] Action failed for {trigger_card.name}: {e}")
                    if resolved_something:
                        print(f"[XMAGE-TRIGGER] Resolved {trigger_card.name} trigger: {t_expl}")
                    else:
                        still_unhandled.append((trigger_card, trigger_text))
                else:
                    still_unhandled.append((trigger_card, trigger_text))
            except Exception as e:
                print(f"[XMAGE-TRIGGER] Error translating {trigger_card.name}: {e}")
                still_unhandled.append((trigger_card, trigger_text))
    else:
        still_unhandled = unhandled

    # Tier 3: queue for the async drain. Keep the cascade guard — cascade-
    # cast creatures skip Tier 3 to prevent phantom effects.
    for trigger_card, trigger_text in still_unhandled:
        if getattr(card, '_from_cascade', False):
            print(f"[TRIGGER-AUTO] Skipping Tier 3 for cascade-cast {card.name} (prevents phantom effects)")
            messages.append(f"⚡ **{trigger_card.name}** triggers on {card.name} ETB (cascade)")
            continue
        ctrl_player = controller
        for p in game.players:
            if trigger_card in p.battlefield:
                ctrl_player = p
                break
        engine._queue_async_trigger(
            game, trigger_card, trigger_text, "creature_enters",
            ctrl_player.name,
            context=f"{card.name} entered the battlefield under {controller.name}'s control",
        )
        messages.append(
            format_trigger_line("⚡", trigger_card.name, trigger_text, game=game, max_chars=300))

    return _collapse_repeated_life_gain(messages)


def _creature_entered_subscriber(game, card=None, controller=None, via=None,
                                 rules=None, **_):
    """PERMANENT_ENTERED → creature-enters watcher dispatch (slice 2b).

    Display lines go to game._pending_messages; former call sites drain
    them at the exact position the old direct scan call occupied, so
    Discord ordering is unchanged. A payload without a usable engine ref
    is logged and skipped — the (inverted) parity recorder then flags the
    entry, because the scan-side id recording never happened.
    """
    if card is None or controller is None:
        return
    engine = getattr(rules, 'engine_ref', None) if rules is not None else None
    if engine is None or not hasattr(engine, '_check_creature_etb_triggers_sync'):
        print(f"[ETB-BUS] {card.name} entered (via={via or '?'}) with no "
              f"usable engine in payload — creature watcher dispatch skipped")
        return
    try:
        if not card.is_creature(game):
            return
    except Exception:
        return
    msgs = _dispatch_creature_entered(engine, game, controller, card)
    if msgs:
        if not hasattr(game, '_pending_messages') or game._pending_messages is None:
            game._pending_messages = []
        game._pending_messages.extend(msgs)


def _enchantment_entered_subscriber(game, card=None, controller=None, via=None,
                                    rules=None, **_):
    """PERMANENT_ENTERED → constellation watcher dispatch (slice 2b, 2/2).

    Same shape as the creature subscriber: display lines go to
    game._pending_messages; former call sites drain in place.
    """
    if card is None or controller is None:
        return
    engine = getattr(rules, 'engine_ref', None) if rules is not None else None
    if engine is None:
        print(f"[ETB-BUS] {card.name} entered (via={via or '?'}) with no "
              f"usable engine in payload — enchantment watcher dispatch skipped")
        return
    try:
        if not card.is_enchantment():
            return
    except Exception:
        return
    msgs = _check_enchantment_etb_watchers(engine, game, controller, card)
    if msgs:
        if not hasattr(game, '_pending_messages') or game._pending_messages is None:
            game._pending_messages = []
        game._pending_messages.extend(msgs)


async def _check_creature_etb_triggers(engine, game: GameState, entering_player: Player, entering_creature: Card) -> List[str]:
    """LEGACY async version (pre-slice-2b): hardcoded + templates + XMage +
    INLINE Claude auto-resolve. No live callers since July 21, 2026 — entry
    dispatch now runs through the PERMANENT_ENTERED subscriber
    (_dispatch_creature_entered), which queues Tier 3 instead of resolving
    inline. Kept for backward compat until the wrapper delegators are
    retired in a later slice.

    When game.triggers_use_stack=True, resolved triggers go on the stack as
    StackEntry(is_spell=False) and players get priority to respond before
    each trigger resolves. When False (default), triggers resolve immediately
    using the tiered cascade — faster for casual play.
    """
    messages, unhandled = engine._check_creature_etb_triggers_sync(game, entering_player, entering_creature)

    # Tier 2.5: Try XMage action translator on unhandled triggers
    # The translator converts ability text → JSON actions without an API call.
    # Only truly untranslatable triggers fall through to Claude (tier 3).
    still_unhandled = []
    if unhandled and engine._xmage_translator:
        player_idx = game.players.index(entering_player) if entering_player in game.players else 0
        opponent_idx = 1 - player_idx
        opponent = game.players[opponent_idx]

        # Calculate entering creature's effective power
        entering_power = 0
        try:
            entering_power = entering_creature.get_effective_power(game) if hasattr(entering_creature, 'get_effective_power') else 0
        except (ValueError, TypeError):
            pass

        for card, trigger_text in unhandled:
            try:
                ctx = build_game_context(
                    game, entering_player, opponent,
                    card=card, entering_creature=entering_creature
                )
                t_actions, t_expl = engine._xmage_translator.translate_trigger(
                    source_card=card.name,
                    ability_text=trigger_text,
                    controller=entering_player.name,
                    opponent=opponent.name,
                    game_context=ctx,
                    entering_creature_name=entering_creature.name,
                    entering_creature_power=entering_power,
                )

                if t_actions is not None and t_actions:
                    resolved_something = False
                    for action in t_actions:
                        if action.get("action") == "no_action":
                            continue
                        try:
                            msg = engine.rules._execute_action_on_state(game, action)
                            if msg:
                                messages.append(msg)
                                resolved_something = True
                        except Exception as e:
                            print(f"[XMAGE-TRIGGER] Action failed for {card.name}: {e}")
                    if resolved_something:
                        print(f"[XMAGE-TRIGGER] Resolved {card.name} trigger: {t_expl}")
                    else:
                        still_unhandled.append((card, trigger_text))
                else:
                    still_unhandled.append((card, trigger_text))
            except Exception as e:
                print(f"[XMAGE-TRIGGER] Error translating {card.name}: {e}")
                still_unhandled.append((card, trigger_text))
    else:
        still_unhandled = unhandled

    # Tier 3: Auto-resolve truly unhandled triggers via Claude API
    # Skip Tier 3 for cascade-cast creatures to prevent phantom effects
    for card, trigger_text in still_unhandled:
        if getattr(entering_creature, '_from_cascade', False):
            print(f"[TRIGGER-AUTO] Skipping Tier 3 for cascade-cast {entering_creature.name} (prevents phantom effects)")
            messages.append(
                f"⚡ **{card.name}** triggers on {entering_creature.name} ETB (cascade)")
            continue
        if engine.rules.client:
            print(f"[TRIGGER-AUTO] {card.name} trigger on {entering_creature.name} ETB")
            try:
                resolve_msgs, actions = await engine.rules.resolve_effect(
                    game,
                    effect_description=f"{trigger_text} (Triggered by {entering_creature.name} entering. "
                                     f"Entering creature: power={entering_creature.power}, "
                                     f"toughness={entering_creature.toughness})",
                    source_card=card.name,
                    controller=entering_player.name,
                    context=f"{entering_creature.name} just entered the battlefield under {entering_player.name}'s control"
                )
                messages.extend(resolve_msgs)
                if actions:
                    print(f"[TRIGGER-AUTO] Executed {len(actions)} action(s) for {card.name}")
            except Exception as e:
                print(f"[TRIGGER-AUTO] Error resolving {card.name}: {e}")
                if _should_emit_resolve_hint(game, f"trigger:{card.name}:{entering_creature.name}"):
                    messages.append(
                        format_trigger_line("⚡", card.name, trigger_text, game=game, max_chars=300)
                        + f"\n  *(Use `!resolve {card.name} trigger, {entering_creature.name} entered` to resolve)*"
                    )
                game.pending_resolves.append(
                    f"{card.name} trigger: {trigger_text[:150]} ({entering_creature.name} entered)"
                )
        else:
            if _should_emit_resolve_hint(game, f"trigger:{card.name}"):
                messages.append(
                    format_trigger_line("⚡", card.name, trigger_text, game=game, max_chars=300)
                    + f"\n  *(Use `!resolve` or `!fix` to handle)*"
                )
            game.pending_resolves.append(
                f"{card.name} trigger: {trigger_text[:150]}"
            )

    # Collapse repeated identical life-gain messages (Soul Warden family etc.)
    messages = _collapse_repeated_life_gain(messages)
    return messages

# =========================================================================
# DIES TRIGGER DETECTION
# =========================================================================


def _check_enchantment_etb_watchers(engine, game: GameState, controller: Player,
                                    entered_card: Card) -> List[str]:
    """Constellation / "whenever an enchantment enters" watchers (CR 603.2).

    June 10 deep-dive (B9): this trigger class had NO scan — Eidolon of
    Blossoms drew 0 of 3 owed cards while Banishing Light and Enchantress's
    Presence entered past it. Scans the entering player's battlefield (these
    are "you control"-scoped in practice), resolves the dominant draw shape
    inline (free), and queues anything else for Tier-3 auto-resolve.
    """
    messages: List[str] = []
    for bf_card in list(controller.battlefield):
        if getattr(bf_card, '_phased_out', False):
            continue
        oracle = bf_card.oracle_text or ''
        ol = oracle.lower()
        if ('constellation' not in ol
                and 'whenever an enchantment' not in ol
                and 'whenever another enchantment' not in ol):
            continue
        if 'enchantment' in ol and 'enters' not in ol and 'constellation' not in ol:
            continue
        # "another enchantment" excludes the entering card itself.
        if 'another enchantment' in ol and bf_card.id == entered_card.id:
            continue
        # Pull the trigger sentence for display / Tier-3 context.
        _m = re.search(
            r'(constellation[^.]*\.|whenever an(?:other)? enchantment[^.]*\.)',
            ol)
        trigger_text = _m.group(1) if _m else ol[:160]
        if 'draw a card' in trigger_text or ('constellation' in ol and 'draw a card' in ol):
            msg = engine.rules._execute_action_on_state(game, {
                "action": "draw_cards", "player": controller.name, "amount": 1})
            if msg:
                messages.append(f"🌟 **{bf_card.name}** (constellation): {msg}")
            print(f"[CONSTELLATION] {bf_card.name} fires for {entered_card.name} entering")
        elif hasattr(engine, '_queue_async_trigger'):
            engine._queue_async_trigger(
                game, bf_card, trigger_text, "enchant_etb", controller.name,
                context=f"{entered_card.name} (an enchantment) just entered the battlefield")
            messages.append(
                format_trigger_line("🌟", bf_card.name, trigger_text, game=game, max_chars=200))
            print(f"[CONSTELLATION] {bf_card.name} queued for Tier 3 "
                  f"({entered_card.name} entered)")
    return messages


def _check_equipment_etb_watchers(engine, game: GameState, controller: Player,
                                  entered_card: Card) -> List[str]:
    """Resolve attach-on-Equipment-ETB watchers (Hammer of Nazahn,
    Sigarda's Aid) for Equipment entering after the watcher.

    A watcher entering itself is handled by its own Tier 1.5 template. This
    covers every later Equipment entering under the same controller, without
    charging an equip cost (the triggered ability says "attach", not "equip").

    July 30 batch-9 reviewer audit: this was hardcoded to Hammer of Nazahn
    BY NAME, so Sigarda's Aid ("Whenever an Equipment you control enters,
    you may attach it to target creature you control") did nothing for all
    8 Equipment casts in game_1532224002137784391 — 5 of them sat unattached
    the entire game while the AI's own memo relied on the free attach.
    Generalized to the printed attach shape.
    """
    if 'equipment' not in (entered_card.type_line or '').lower():
        return []

    watcher = None
    for c in controller.battlefield:
        if c.id == entered_card.id or getattr(c, '_phased_out', False):
            continue
        _o = (c.oracle_text or '').lower()
        if (re.search(r"whenever (?:[\w\s,']+ or )?an(?:other)? equipment "
                      r"you control enters", _o)
                and 'attach' in _o):
            watcher = c
            break
    if watcher is None:
        return []

    target = next((c for c in controller.battlefield
                   if c.id not in (watcher.id, entered_card.id)
                   and not getattr(c, '_phased_out', False)
                   and c.is_creature(game)), None)
    if target is None:
        print(f"[EQUIPMENT-ETB] {watcher.name}: no creature for {entered_card.name}")
        return []

    msg = engine.rules._execute_action_on_state(game, {
        "action": "equip", "equipment": entered_card.name,
        "creature": target.name, "player": controller.name,
    })
    if msg:
        print(f"[EQUIPMENT-ETB] {watcher.name} attaches {entered_card.name} "
              f"to {target.name}")
        return [f"🔨 **{watcher.name}**: {msg}"]
    return []


def _check_dies_triggers_sync(engine, game: GameState, dying_card: Card, dying_player: Player) -> Tuple[List[str], List[Tuple]]:
    """Check for 'whenever a creature dies' and 'when THIS dies' triggers.

    Returns:
        Tuple of (messages, unhandled_triggers) where unhandled_triggers is
        a list of (card, trigger_text) tuples that need async auto-resolution.
    """
    # (Slice 3c, July 24: the death parity recorder that shadowed this
    # dispatcher was retired — [EVENT-PARITY-DIES]=0 in the post-3b batch.)
    messages = []
    unhandled = []
    player_idx = game.players.index(dying_player) if dying_player in game.players else 0
    opponent_idx = 1 - player_idx
    opponent = game.players[opponent_idx]
    trigger_count = 0
    MAX_DIES_TRIGGERS = 20  # Depth guard: prevent Blood Artist infinite loops
    allowed_source_ids = game._dies_source_ids_by_dead_id.get(dying_card.id)

    # Calculate dying creature's power/toughness for context (last-known info)
    dying_power = 0
    dying_toughness = 0
    try:
        dying_power = dying_card.get_effective_power(game) if hasattr(dying_card, 'get_effective_power') else 0
    except (ValueError, TypeError):
        pass
    try:
        dying_toughness = dying_card.get_effective_toughness(game) if hasattr(dying_card, 'get_effective_toughness') else 0
    except (ValueError, TypeError):
        pass

    # Scan ALL players' battlefields for permanents with dies triggers.
    # Also scan _recently_died — per CR 603.10, creatures that die simultaneously
    # see each other die and trigger accordingly (e.g., Blood Artist sees itself die).
    cards_to_scan = []
    for player in game.players:
        for card in player.battlefield:
            if (not getattr(card, '_phased_out', False)
                    and (allowed_source_ids is None or card.id in allowed_source_ids)):
                cards_to_scan.append((card, player))
    # Include recently-died creatures (they see each other die simultaneously)
    recently_died = (game._active_dies_batch
                     if game._active_dies_batch else game._recently_died)
    for dead_card, dead_player in recently_died:
        if (dead_card.id != dying_card.id
                and (allowed_source_ids is None or dead_card.id in allowed_source_ids)):
            cards_to_scan.append((dead_card, dead_player))
    # Also include the dying card itself if it has "Blood Artist" style engine-trigger
    # ("Whenever Blood Artist or another creature dies" — sees its own death)
    if dying_card.oracle_text and "dies" in dying_card.oracle_text.lower():
        dying_oracle = dying_card.oracle_text.lower()
        if (dying_card.name.lower() in dying_oracle and "dies" in dying_oracle) or \
           "whenever a creature" in dying_oracle or \
           "whenever this creature" in dying_oracle or \
           "this creature or another creature" in dying_oracle or \
           "nontoken creature" in dying_oracle:  # May 30 audit: Midnight Reaper et al.; June 10: 2026 "this creature" templating (Blood Artist)
            cards_to_scan.append((dying_card, dying_player))

    # May 20 audit (APNAP-1): sort by NON-active player first so dies triggers
    # resolve in CR 603.3b LIFO order in immediate mode. AP places his triggers
    # on the stack first (bottom), NAP places hers on top, LIFO resolves NAP
    # first. Inline resolution should therefore iterate NAP's triggers first.
    # June 10: ordering extracted to helpers.apnap_order_died (CR 603.3b
    # NAP-first), shared with the two engine.py drain sites and unit-tested.
    from mtg.helpers import apnap_order_died
    cards_to_scan = apnap_order_died(cards_to_scan, game)

    for card, player in cards_to_scan:
        if trigger_count >= MAX_DIES_TRIGGERS:
            messages.append(f"⚠️ Dies trigger limit ({MAX_DIES_TRIGGERS}) reached — remaining triggers skipped")
            break
        if not card.oracle_text:
            continue
        oracle_lower = card.oracle_text.lower()

        # Quick oracle guard — skip if no "dies" keyword
        if "dies" not in oracle_lower:
            continue

        # Determine who controls this trigger source
        ctrl_idx = game.players.index(player) if player in game.players else 0
        ctrl = player
        opp = game.players[1 - ctrl_idx]

        # Skip "whenever ANOTHER creature dies" triggers from the dying card
        # (but allow "whenever a creature dies" or engine-referencing triggers like Blood Artist)
        if card.id == dying_card.id and (
                "whenever another creature" in oracle_lower
                or "whenever another nontoken creature" in oracle_lower):
            continue

        # Check if this is a "whenever a/another creature dies" trigger
        # May 30 audit: the "nontoken creature" branch was missing, so
        # "Whenever a nontoken creature you control dies" (Midnight Reaper, Judith
        # the Scourge Diva, Liliana Heretical Healer's flip) never matched — the
        # "nontoken" qualifier defeats "whenever a creature" and modern "this
        # creature" templating defeats the name-in-oracle check, so the trigger
        # was silently dropped at the `continue` below.
        has_dies_trigger = (
            ("whenever a creature" in oracle_lower and "dies" in oracle_lower) or
            ("whenever another creature" in oracle_lower and "dies" in oracle_lower) or
            ("whenever a creature you control dies" in oracle_lower) or
            ("nontoken creature" in oracle_lower and "dies" in oracle_lower) or
            # June 10 deep-dive (B1): 2026 Oracle templating — "Whenever THIS
            # CREATURE or another creature dies" (Blood Artist, Zulaport per
            # the card cache). All previous branches missed it, making the
            # hardcoded Blood Artist/Zulaport drain handler UNREACHABLE dead
            # code (~12-life swing in game …069646262302).
            ("whenever this creature" in oracle_lower and "dies" in oracle_lower) or
            ("this creature or another creature" in oracle_lower and "dies" in oracle_lower) or
            (card.name.lower() in oracle_lower and "dies" in oracle_lower)
        )

        if not has_dies_trigger:
            continue

        # June 10 deep-dive (B2, CRITICAL): "whenever a creature AN OPPONENT
        # CONTROLS dies" (Massacre Wurm, Glissa the Traitor) had no scope
        # gate — it fired on the trigger controller's OWN deaths too. Wurm
        # fires only when the dying creature's controller is an opponent of
        # the WURM's controller; three misfires on own-side deaths drained
        # the wrong player, the last taking Claude from 1 to 0 (an illegal
        # game loss). In 1v1 this gate also makes the "that player loses"
        # recipient coincide with the dying creature's controller.
        if "an opponent controls" in oracle_lower and dying_player is player:
            continue

        # "whenever a creature you control dies" — only fire if dying creature is ours
        if "you control" in oracle_lower and dying_player != player:
            continue

        # June 10 audit (V16): enforce the "nontoken" qualifier against the
        # dying card. Detection matched Midnight Reaper, but nothing checked
        # the dying creature's token-ness — a Bitterblossom Faerie token death
        # fired "Whenever a NONTOKEN creature you control dies".
        if "nontoken" in oracle_lower and getattr(dying_card, 'is_token', False):
            continue

        # July 30 batch-9 reviewer audit (CR 603.4): "if it had a +1/+1
        # counter on it" (Basri's Lieutenant) was checked NOWHERE, and the
        # Tier-3 dies context carries no counter info — so the LLM FABRICATED
        # the condition as true on all 5 firings in game_1532236167368544388,
        # minting a replacement Knight for every counterless Knight that died
        # in a self-sustaining loop. The dying card object is right here;
        # gate deterministically before any tier sees the trigger.
        if ("if it had a +1/+1 counter on it" in oracle_lower
                and not (getattr(dying_card, 'counters', None) or {}).get('+1/+1', 0)):
            print(f"[DIES-TRIGGER] {card.name}: intervening-if not met — "
                  f"{dying_card.name} had no +1/+1 counter (CR 603.4)")
            continue

        # Species Specialist only watches the type chosen as it entered. The
        # old name template drew for every death because no choice was stored
        # and no subtype gate existed (7 draws from 4 initial Living Death
        # deaths plus secondary sacrifices in game 1514636909593497602).
        if card.name.lower() == "species specialist":
            chosen_type = (card._chosen_creature_type or "").lower()
            dying_types = {
                creature_type.lower()
                for creature_type in dying_card.get_creature_types()
            }
            if not chosen_type or chosen_type not in dying_types:
                continue

        handled = False

        # ---- HARDCODED HANDLERS ----

        # Blood Artist / Zulaport Cutthroat: drain 1
        if card.name.lower() in ("blood artist", "zulaport cutthroat", "bastion of remembrance"):
            opp.life -= 1
            opp.record_life_loss(1)
            _log_life_change(opp, -1, f"dies trigger: {card.name}")
            # June 10 audit (V22): route the gain through the centralized
            # life-gain path so prevention statics (Erebos: "Your opponents
            # can't gain life") and gain-life triggers apply. The old naked
            # `ctrl.life += 1` bypassed the prevention that the SAME card's
            # ETB path correctly honored — the display contradicted itself
            # within two turns.
            _gain_ok, _gain_amt, _gain_chain = engine.rules._apply_life_gain(
                game, ctrl, 1, source_name=card.name)
            _gained = bool(_gain_ok and _gain_amt)
            print(f"[DIES-TRIGGER] {card.name}: {opp.name} loses 1 life (life: {max(0, opp.life)}), "
                  f"{ctrl.name} {'gains 1 life' if _gained else 'gain PREVENTED'} (life: {max(0, ctrl.life)})")
            if _gained:
                _log_life_change(ctrl, _gain_amt, f"dies trigger: {card.name}")
                # July 20 display audit: Zulaport's drain was the sole
                # recurring life event WITHOUT (life: N) totals in Discord
                # (4×/game while console had them) — standardized notation.
                messages.append(f"💀 {card.name}: {opp.name} loses 1 life (life: {max(0, opp.life)}), "
                                f"{ctrl.name} gains 1 life (life: {max(0, ctrl.life)})")
            else:
                messages.append(f"💀 {card.name}: {opp.name} loses 1 life (life: {max(0, opp.life)}) (life gain prevented"
                                f"{': ' + ', '.join(_gain_chain) if _gain_chain else ''})")
            trigger_count += 1
            handled = True

        # Grave Pact / Dictate of Erebos: opponents sacrifice
        elif card.name.lower() in ("grave pact", "dictate of erebos"):
            sacrifice_msg = engine.rules._execute_action_on_state(game, {
                "action": "sacrifice_permanent", "player": opp.name,
                "type_filter": "creature",
                "reason": f"{card.name}: a creature its controller controlled died",
            })
            if sacrifice_msg:
                messages.append(f"💀 {card.name}: {sacrifice_msg}")
            trigger_count += 1
            handled = True

        # Syr Konrad, the Grim: deal 1 damage to each opponent
        elif card.name.lower() == "syr konrad, the grim":
            opp.life -= 1
            opp.record_life_loss(1)
            # June 10 audit (V31a): clamp the console print like the Discord
            # twin below — this was one of the two negative-life leak sites.
            print(f"[DIES-TRIGGER] Syr Konrad, the Grim: deals 1 damage to {opp.name} (life: {max(0, opp.life)})")
            _log_life_change(opp, -1, "Syr Konrad, the Grim")  # May 30 audit: canonical tag
            messages.append(f"💀 Syr Konrad: deals 1 damage to {opp.name} ({max(0, opp.life)} life)")
            trigger_count += 1
            handled = True

        # Pitiless Plunderer: create a Treasure token when creature you control dies
        elif card.name.lower() == "pitiless plunderer":
            if dying_player == player:  # Only triggers on your creatures
                from mtg_game import Card
                treasure = Card(
                    name="Treasure",
                    mana_cost="",
                    type_line="Token Artifact — Treasure",
                    oracle_text="{T}, Sacrifice this artifact: Add one mana of any color.",
                    power="0",
                    toughness="0",
                )
                treasure.is_token = True
                treasure.owner_index = game.players.index(ctrl)
                ctrl.battlefield.append(treasure)
                messages.append(f"💀 Pitiless Plunderer: {ctrl.name} creates a Treasure token")
                trigger_count += 1
                handled = True

        # ---- TIER 1.5: Template library ----
        if not handled and HAS_EFFECT_TEMPLATES:
            trigger_text = ""
            # Match both "Whenever a creature dies" (other-creature dies trigger)
            # AND "When [self] dies" (self-dies trigger like Solemn Simulacrum,
            # Mulldrifter evoke, Reflector Mage). Without the "when" branch,
            # self-dies triggers silently dropped.
            self_dies_pattern = (card.name.lower() + " dies")
            for paragraph in card.oracle_text.split('\n'):
                p_lower = paragraph.lower().strip()
                if "dies" in p_lower and (
                    "whenever" in p_lower
                    or ("when" in p_lower and self_dies_pattern in p_lower)
                ):
                    trigger_text = paragraph.strip()
                    break

            if trigger_text:
                try:
                    ctx = build_game_context(game, ctrl, opp,
                                             card=card, dying_creature=dying_card)
                    ctx['_trigger_source'] = card.name
                    lib = get_effect_library()
                    actions, explanation = lib.resolve_dies_trigger(
                        trigger_card_name=card.name,
                        trigger_oracle=trigger_text,
                        dying_creature_name=dying_card.name,
                        dying_creature_power=dying_power,
                        dying_creature_toughness=dying_toughness,
                        controller=ctrl.name,
                        opponent=opp.name,
                        game_context=ctx,
                    )
                    if actions is not None:  # [] = deliberate template no-op (handled); only None means unhandled → Tier 3
                        for action in actions:
                            if action.get("action") == "no_action":
                                reason = action.get("reason", "")
                                msg = _format_noop_reason(card.name, reason)
                                if msg:
                                    # Dies trigger style — keep the skull emoji on user-visible no-ops.
                                    messages.append(msg.replace("📍", "💀", 1))
                                continue
                            try:
                                msg = engine.rules._execute_action_on_state(game, action)
                                if msg:
                                    messages.append(msg)
                            except Exception as e:
                                print(f"[DIES-TEMPLATE] Action failed for {card.name}: {e}")
                        print(f"[DIES-TEMPLATE] Resolved {card.name} trigger: {explanation}")
                        trigger_count += 1
                        handled = True
                except Exception as e:
                    print(f"[DIES-TEMPLATE] Error for {card.name}: {e}")

        if not handled:
            # Find trigger text for unhandled
            trigger_text = ""
            if card.oracle_text:
                for paragraph in card.oracle_text.split('\n'):
                    p_lower = paragraph.lower().strip()
                    if "dies" in p_lower and "whenever" in p_lower:
                        trigger_text = paragraph.strip()
                        break
            if trigger_text:
                unhandled.append((card, trigger_text))
                trigger_count += 1

    # Also check the dying card itself for "when [this] dies" triggers
    if dying_card.oracle_text and trigger_count < MAX_DIES_TRIGGERS:
        oracle_lower = dying_card.oracle_text.lower()
        dying_name_lower = dying_card.name.lower()
        # "when {card_name} dies" or "when this creature dies"
        if (f"when {dying_name_lower} dies" in oracle_lower or
            "when this creature dies" in oracle_lower or
            f"when {dying_name_lower.split(',')[0]} dies" in oracle_lower):

            handled = False

            # ---- TIER 1.5: Template library for engine-death triggers ----
            if HAS_EFFECT_TEMPLATES:
                trigger_text = ""
                for paragraph in dying_card.oracle_text.split('\n'):
                    p_lower = paragraph.lower().strip()
                    if "dies" in p_lower and ("when" in p_lower):
                        trigger_text = paragraph.strip()
                        break

                if trigger_text:
                    try:
                        ctx = build_game_context(game, dying_player, opponent,
                                                 card=dying_card, dying_creature=dying_card)
                        lib = get_effect_library()
                        actions, explanation = lib.resolve_dies_trigger(
                            trigger_card_name=dying_card.name,
                            trigger_oracle=trigger_text,
                            dying_creature_name=dying_card.name,
                            dying_creature_power=dying_power,
                            dying_creature_toughness=dying_toughness,
                            controller=dying_player.name,
                            opponent=opponent.name,
                            game_context=ctx,
                        )
                        if actions is not None:  # [] = deliberate template no-op (handled); only None means unhandled → Tier 3
                            for action in actions:
                                if action.get("action") == "no_action":
                                    reason = action.get("reason", "")
                                    msg = _format_noop_reason(dying_card.name, reason)
                                    if msg:
                                        messages.append(msg.replace("📍", "💀", 1))
                                    continue
                                try:
                                    msg = engine.rules._execute_action_on_state(game, action)
                                    if msg:
                                        messages.append(msg)
                                except Exception as e:
                                    print(f"[DIES-TEMPLATE] Action failed for {dying_card.name}: {e}")
                            print(f"[DIES-TEMPLATE] Self-death trigger: {dying_card.name} → {explanation}")
                            handled = True
                    except Exception as e:
                        print(f"[DIES-TEMPLATE] Error for engine-death {dying_card.name}: {e}")

            if not handled:
                trigger_text = ""
                for paragraph in dying_card.oracle_text.split('\n'):
                    p_lower = paragraph.lower().strip()
                    if "dies" in p_lower and "when" in p_lower:
                        # July 23 audit (#4): Persist and Undying are KEYWORD
                        # abilities the death-save chain resolves mechanically
                        # (SBA, single-target destroy, board wipe, and — as of
                        # this audit — sacrifice), each gated on the -1/-1 /
                        # +1/+1 counter per CR 702.77b / 702.92b. Their reminder
                        # text ("When this creature dies, if it had no ...
                        # counters on it, return it to the battlefield ...")
                        # matches this self-death extraction, so queueing it for
                        # Tier 3 made the judge return the creature a SECOND
                        # time, ungated — a free-life loop across repeat deaths
                        # (game_1529674672545988631, Kitchen Finks). Keep
                        # scanning: a card can carry a real dies trigger in a
                        # later paragraph.
                        if p_lower.startswith('persist') or p_lower.startswith('undying'):
                            print(f"[DIES-KEYWORD] {dying_card.name}: "
                                  f"{p_lower.split('(')[0].strip()} handled by the "
                                  f"death-save chain — not escalating to Tier 3")
                            continue
                        trigger_text = paragraph.strip()
                        break
                if trigger_text:
                    unhandled.append((dying_card, trigger_text))

    # [DEATH-WATCHER] Check death watchers (Searing Blood, etc.)
    if hasattr(game, '_death_watchers'):
        for watcher in list(game._death_watchers):
            if watcher.get('turn_registered') == game.turn_number:
                watch_target = watcher.get('watch_target', '').lower()
                if dying_card.name.lower() == watch_target or watch_target in dying_card.name.lower():
                    source = watcher.get('source', 'Unknown')
                    print(f"[DEATH-WATCHER] {source}: watched creature {dying_card.name} died!")
                    for action in watcher.get('on_death_actions', []):
                        try:
                            msg = engine.rules._execute_action_on_state(game, action)
                            if msg:
                                messages.append(f"⚡ {source}: {msg}")
                        except Exception as e:
                            print(f"[DEATH-WATCHER] Action failed: {e}")
                    game._death_watchers.remove(watcher)

    game._dies_source_ids_by_dead_id.pop(dying_card.id, None)
    return messages, unhandled


def _check_permanent_etb_watchers(engine, game: GameState,
                                  entering_player: Player,
                                  entering_card: Card) -> List[str]:
    """Resolve mandatory watchers for any permanent entering the battlefield.

    Creature, token, land, and noncreature cast paths converge here for Altar
    of the Brood. Previously each path only scanned type-specific watchers,
    so Altar printed its oracle text but milled zero cards.
    """
    messages = []
    for watcher in list(entering_player.battlefield):
        if watcher.id == entering_card.id or getattr(watcher, '_phased_out', False):
            continue
        oracle = (watcher.oracle_text or '').lower()
        if (watcher.name.lower() == 'altar of the brood'
                or ('whenever another permanent you control enters' in oracle
                    and 'each opponent mills a card' in oracle)):
            for opponent in game.players:
                if opponent is entering_player:
                    continue
                msg = engine.rules._execute_action_on_state(game, {
                    'action': 'mill', 'player': opponent.name, 'amount': 1,
                    'source': watcher.name,
                })
                if msg:
                    messages.append(f"⚙️ {watcher.name}: {msg}")
            print(f"[PERMANENT-ETB-TRIGGER] {watcher.name} fires for {entering_card.name}")
    return messages


# ---------------------------------------------------------------------------
# Source-side trigger batching
# ---------------------------------------------------------------------------

# Regex captures the "💀 Source Name: ..." pattern emitted by dies/ltb/etb/etc.
# trigger messages. We use the emoji + source name (everything before the first
# colon) as the grouping key so consecutive triggers from the same source
# collapse into "Source × N — final_message".
_TRIGGER_SOURCE_RE = re.compile(
    r'^(?P<emoji>[\U0001F300-\U0001FAFF☀-➿✀-➿⚡⛔]+️?)\s*'
    r'(?:\*\*)?(?P<source>[^:*]+?)(?:\*\*)?\s*:'
)


def _trigger_source_key(msg: str) -> Optional[str]:
    """Extract a normalized source key from a trigger message.

    Returns None if the message doesn't match the trigger pattern (so it
    won't be batched — phase markers, board-wipe summaries, etc. pass
    through unchanged).
    """
    m = _TRIGGER_SOURCE_RE.match(msg)
    if not m:
        return None
    # Use the emoji + source name as the key. Different emojis = different
    # event types (💀 = dies, 📦 = zone change, ⚡ = generic trigger), so we
    # keep them separate.
    return f"{m.group('emoji').strip()}|{m.group('source').strip().lower()}"


def collapse_trigger_burst(messages: List[str]) -> List[str]:
    """Collapse consecutive runs of trigger messages that share a source.

    When a board wipe kills 9 creatures, Athreos's dies trigger fires 9
    times. Even if the messages aren't byte-identical (life totals shift,
    counter values change), they all share the prefix "💀 Athreos:". This
    helper collapses such runs into a single representative message + a
    `(×N total)` annotation so Discord doesn't get spammed.

    Format examples:
      Input:  ["💀 Blood Artist: Rick loses 1 life",
               "💀 Blood Artist: Rick loses 1 life",
               "💀 Blood Artist: Rick loses 1 life"]
      Output: ["💀 Blood Artist: Rick loses 1 life (×3 fires)"]

      Input:  ["💀 Syr Konrad: deals 1 damage to Claude (29 life)",
               "💀 Syr Konrad: deals 1 damage to Claude (28 life)",
               "💀 Syr Konrad: deals 1 damage to Claude (27 life)"]
      Output: ["💀 Syr Konrad: deals 1 damage to Claude (27 life) (×3 fires)"]

    Non-trigger messages (no source: prefix) pass through unchanged.
    Single-fire triggers also pass through unchanged.
    """
    if not messages:
        return messages
    out: List[str] = []
    run_key: Optional[str] = None
    run_msgs: List[str] = []

    def _flush() -> None:
        if not run_msgs:
            return
        if len(run_msgs) == 1:
            out.append(run_msgs[0])
        else:
            # July 24 batch-6 (reviewer A1, MAJOR): when each fire names a
            # DIFFERENT sacrificed object, the last-message representative
            # hides real events — "💀 Grave Pact: Claude sacrifices Mother
            # of Runes (×2 fires)" made Kor Soldier's sacrifice invisible
            # (game_1529979552258855062). Enumerate the distinct objects for
            # the sacrifice shape; cumulative-value shapes (Syr Konrad's
            # running life total) keep the last-message form.
            _sac_names = []
            for m in run_msgs:
                sm = re.search(r'sacrifices\s+(.+?)(?:\s*\(|\s*$)', m)
                _sac_names.append(sm.group(1).strip() if sm else None)
            if all(_sac_names) and len(set(_sac_names)) > 1:
                names = ', '.join(dict.fromkeys(_sac_names))
                combined = re.sub(
                    r'(sacrifices\s+).+?(\s*\(|\s*$)',
                    lambda m2: m2.group(1) + names + m2.group(2),
                    run_msgs[-1], count=1)
                out.append(f"{combined} (×{len(run_msgs)} fires)")
            else:
                # Use the LAST message in the run as the representative — it
                # has the cumulative life total / counter value that reflects
                # the final state after all fires.
                out.append(f"{run_msgs[-1]} (×{len(run_msgs)} fires)")

    for msg in messages:
        key = _trigger_source_key(msg) if isinstance(msg, str) else None
        if key is None:
            # Not a batchable trigger message — flush any open run and emit.
            _flush()
            run_key = None
            run_msgs = []
            out.append(msg)
            continue
        if key == run_key:
            run_msgs.append(msg)
        else:
            _flush()
            run_key = key
            run_msgs = [msg]
    _flush()
    return out


def _check_ltb_triggers_sync(engine, game: GameState, leaving_card: Card, leaving_player: Player,
                              destination: str = "graveyard") -> List[str]:
    """Check for 'when [this] leaves the battlefield' triggers.

    Handles:
    1. Self-LTB triggers ("When [this] leaves the battlefield, ...")
    2. Exiled-by tracking: returns cards that were exiled by this permanent
       (Worldgorger Dragon, Oblivion Ring, Fiend Hunter, Banishing Light, etc.)

    Args:
        leaving_card: The card leaving the battlefield
        leaving_player: The player who controlled it
        destination: Where the card is going ("graveyard", "exile", "hand", "library")

    Returns:
        List of messages describing what happened.
    """
    messages = []
    oracle = leaving_card.oracle_text or ''
    oracle_lower = oracle.lower()
    card_name_lower = leaving_card.name.lower()

    # Linked exile is keyed to the source permanent, regardless of whether
    # that source itself has printed LTB text (Calix links the return to an
    # arbitrary enchantment). Return tracked cards before self-LTB parsing.
    exiled_by_key = f"_exiled_by_{leaving_card.id}"
    exiled_cards = getattr(game, exiled_by_key, [])
    if exiled_cards:
        returned = []
        for exiled_info in exiled_cards:
            owner_idx = exiled_info.get('owner', 0)
            owner = game.players[owner_idx] if owner_idx < len(game.players) else leaving_player
            for exiled_card in list(owner.exile):
                if exiled_card.name == exiled_info.get('name', ''):
                    owner.exile.remove(exiled_card)
                    exiled_card.summoning_sick = True
                    exiled_card.entered_this_turn = True
                    owner.battlefield.append(exiled_card)
                    returned.append(exiled_card.name)
                    break
        delattr(game, exiled_by_key)
        if returned:
            messages.append(f"↩️ {leaving_card.name} left: returned {', '.join(returned)} from exile")

    # ---- Self-LTB triggers ----
    # Pattern: "When [this] leaves the battlefield, ..."
    # July 20 batch-3 audit (reviewer A1): also match the Aura-family
    # "is put into a graveyard from the battlefield" phrasing (Rancor) —
    # gated on destination == graveyard so an exiled Rancor doesn't bounce.
    # Note the aura_invalid SBA handler DOES run this scan; the gap was
    # only this phrasing gate (game_1528957329452830760: Rancor was
    # permanently lost after Sythis died).
    has_ltb = False
    ltb_text = ''
    for sentence in oracle.split('.'):
        sl = sentence.lower().strip()
        _gy_from_bf = (destination == 'graveyard'
                       and 'put into a graveyard from the battlefield' in sl)
        if ('leaves the battlefield' in sl or 'leaves play' in sl or _gy_from_bf) and (
            'when' in sl or 'whenever' in sl):
            # Make sure it's a engine-referential trigger
            if card_name_lower in sl or 'this' in sl or 'it leaves' in sl:
                has_ltb = True
                ltb_text = sentence.strip()
                break

    if has_ltb:
        print(f"[LTB-TRIGGER] {leaving_card.name}: {ltb_text[:150]}")
        player_idx = game.players.index(leaving_player) if leaving_player in game.players else 0
        opponent_idx = 1 - player_idx
        opponent = game.players[opponent_idx]

        # May 14 audit (L4): reanimation auras (Animate Dead, Dance of the Dead,
        # Necromancy) have their LTB sacrifice handled by the SBA at
        # sba.py:251 via the _reanimated_by_aura_id binding. The LTB-trigger
        # display + Tier 3 judge path are redundant — they emit two oracle-
        # text dumps and a free-text Claude ruling, none of which actually
        # cause the sacrifice. Short-circuit here so the binding-aware SBA
        # is the sole authoritative path.
        REANIMATE_AURA_NAMES = {"animate dead", "dance of the dead", "necromancy"}
        if card_name_lower in REANIMATE_AURA_NAMES:
            print(f"[LTB-TRIGGER] {leaving_card.name} (reanimation aura) — "
                  f"sacrifice handled by SBA binding, suppressing LTB-trigger display")
            return messages

        # ---- Self-recursion to hand (Rancor family) ----
        # "…return it to its owner's hand." (July 20 batch-3, reviewer A1)
        if re.search(r"return (?:it|this card) to its owner's hand", ltb_text.lower()):
            owner_idx = getattr(leaving_card, 'owner_index', None)
            owner = (game.players[owner_idx]
                     if owner_idx is not None and 0 <= owner_idx < len(game.players)
                     else leaving_player)
            for _gy_player in game.players:
                if leaving_card in _gy_player.graveyard:
                    _gy_player.graveyard.remove(leaving_card)
                    owner.hand.append(leaving_card)
                    messages.append(f"↩️ **{leaving_card.name}** returns to {owner.name}'s hand")
                    print(f"[LTB-TRIGGER] {leaving_card.name}: returned to owner's hand (Rancor-style)")
                    return messages
            # Not in a graveyard yet (caller fires the scan pre-move):
            # leave it for the caller's move + manual !resolve fallback.

        # ---- Return exiled cards (Worldgorger Dragon, Oblivion Ring, etc.) ----
        if 'return' in ltb_text.lower() and ('exiled' in ltb_text.lower() or 'exile' in oracle_lower):
            # Find cards that were exiled by this permanent
            exiled_cards = getattr(game, exiled_by_key, [])
            if exiled_cards:
                returned = []
                for exiled_info in exiled_cards:
                    card_name = exiled_info.get('name', '')
                    owner_idx = exiled_info.get('owner', 0)
                    owner = game.players[owner_idx] if owner_idx < len(game.players) else leaving_player
                    # Find the card in exile and return it
                    for c in list(owner.exile):
                        if c.name == card_name:
                            owner.exile.remove(c)
                            c.summoning_sick = True
                            c.entered_this_turn = True
                            owner.battlefield.append(c)
                            returned.append(c.name)
                            break
                if returned:
                    messages.append(f"↩️ {leaving_card.name} LTB: returned {len(returned)} card(s) from exile: {', '.join(returned[:5])}{'...' if len(returned) > 5 else ''}")
                    print(f"[LTB-TRIGGER] {leaving_card.name}: returned {len(returned)} exiled cards")
                # Clear tracking
                delattr(game, exiled_by_key)

        # ---- Create token on LTB (Thragtusk) ----
        if 'create' in ltb_text.lower() and 'token' in ltb_text.lower():
            # (July 20 batch-3: the local `import re` here shadowed the
            # module-level import and made `re` function-local, so any
            # EARLIER `re.` use in this function raised UnboundLocalError —
            # same scoping class as the Apr 6 Swords EventType bug.)
            token_match = re.search(r'create (?:a |an? )?(\d+)/(\d+) ([\w\s]+?)(?:creature )?tokens?', ltb_text.lower())
            if token_match:
                t_power = int(token_match.group(1))
                t_tough = int(token_match.group(2))
                t_type = token_match.group(3).strip().title()
                token = Card(
                    name=t_type,
                    type_line=f"Token Creature - {t_type}",
                    power=str(t_power),
                    toughness=str(t_tough),
                    owner_index=player_idx,
                )
                token.summoning_sick = True
                token.entered_this_turn = True
                leaving_player.battlefield.append(token)
                messages.append(f"⚡ {leaving_card.name} LTB: created {t_power}/{t_tough} {t_type} token")

        # ---- Generic LTB: try template library (oracle-pattern only, no name match) ----
        # May 24 audit fix: track whether the template "claimed" this LTB —
        # template_handled_silently=True means the template ran, all its
        # actions returned None (legitimate silent no-op like Spell Queller's
        # release_queller_exile finding an empty exile bucket), and we should
        # NOT fall through to the Tier 3 queue + oracle-text display. Previous
        # behavior: every Queller LTB emitted the full 💨 oracle line + queued
        # a Tier 3 ruling even when nothing was exiled (41 Queller mentions
        # vs 7 actual exiles in the May 24 batch).
        template_handled_silently = False
        if not messages and HAS_EFFECT_TEMPLATES:
            try:
                lib = get_effect_library()
                ctx = build_game_context(game, leaving_player, opponent, card=leaving_card)
                actions, explanation = lib.resolve_etb(
                    card_name=leaving_card.name,
                    oracle_text=ltb_text,
                    controller=leaving_player.name,
                    opponent=opponent.name,
                    game_context=ctx,
                    event_type="ltb",
                )
                if actions and not any(a.get('action') == 'no_action' for a in actions):
                    any_msg = False
                    for action in actions:
                        try:
                            msg = engine.rules._execute_action_on_state(game, action)
                            if msg:
                                messages.append(f"⚡ {leaving_card.name} LTB: {msg}")
                                any_msg = True
                        except Exception as e:
                            print(f"[LTB-TRIGGER] Action failed: {e}")
                    print(f"[LTB-TRIGGER] Template resolved {leaving_card.name}: {explanation}")
                    # If the template ran but every action returned None, it
                    # was a legitimate silent no-op (e.g., release_queller_exile
                    # with empty bucket, schedule-only actions). Suppress the
                    # Tier 3 fallback so we don't emit the full oracle text.
                    if not any_msg:
                        template_handled_silently = True
                        print(f"[LTB-TRIGGER-SILENT] {leaving_card.name}: template fired, all actions no-op — suppressing fallback display")
            except Exception as e:
                print(f"[LTB-TRIGGER] Template error for {leaving_card.name}: {e}")

        # If still unhandled, queue for async Tier 3 drain (sync context)
        if not messages and not template_handled_silently:
            engine._queue_async_trigger(
                game, leaving_card, ltb_text, "ltb",
                leaving_player.name,
                context=f"{leaving_card.name} left the battlefield (destination: {destination})",
            )
            messages.append(
                f"💨 **{leaving_card.name}** LTB trigger: *{sanitize_oracle_for_display(ltb_text, 200)}*"
            )
            game.pending_resolves.append(
                f"{leaving_card.name} LTB trigger: {ltb_text[:150]}"
            )

    # ---- Control-change LTB: return stolen permanents when steal source dies ----
    # Agent of Treachery, Sower of Temptation, etc.
    stolen_cards = []
    for p in game.players:
        for c in list(p.battlefield):
            if (c.control_gained_by and c.control_gained_by.lower() == card_name_lower
                    and c.original_controller_index is not None):
                original_owner = game.players[c.original_controller_index]
                if p != original_owner:
                    game.unregister_static_effects(c)
                    p.battlefield.remove(c)
                    original_owner.battlefield.append(c)
                    c.control_gained_by = None
                    c.original_controller_index = None
                    # Re-register under the original controller
                    game.register_static_keyword_grants(c, original_owner.name)
                    game.register_static_pt_effects(c, original_owner.name)
                    game.register_replacement_effects(c, original_owner.name)
                    stolen_cards.append(c.name)
    if stolen_cards:
        msgs = f"↩️ {leaving_card.name} LTB: returned {len(stolen_cards)} permanent(s): {', '.join(stolen_cards[:5])}"
        messages.append(msgs)
        print(f"[LTB-STEAL-RETURN] {leaving_card.name} returned {len(stolen_cards)} stolen cards")

    return messages


def _check_attack_triggers_sync(engine, game: GameState, attacker_card: Card, attacking_player: Player) -> Tuple[List[str], List[Tuple]]:
    """Check for 'whenever [this/a creature] attacks' triggers.

    Returns:
        Tuple of (messages, unhandled_triggers).
    """
    messages = []
    unhandled = []
    player_idx = game.players.index(attacking_player) if attacking_player in game.players else 0
    opponent_idx = 1 - player_idx
    opponent = game.players[opponent_idx]

    # Calculate attacking creature's power for context
    attacking_power = 0
    try:
        attacking_power = attacker_card.get_effective_power(game) if hasattr(attacker_card, 'get_effective_power') else 0
    except (ValueError, TypeError):
        pass

    # 1. Check the attacker itself for "whenever [this] attacks" oracle text
    if attacker_card.oracle_text:
        self_attack_text = next(
            (paragraph.strip() for paragraph in attacker_card.oracle_text.split('\n')
             if _is_self_attack_trigger_paragraph(attacker_card, paragraph)),
            "",
        )
        if self_attack_text:

            handled = False

            # ---- TIER 1.5: Template library ----
            if HAS_EFFECT_TEMPLATES:
                trigger_text = self_attack_text

                if trigger_text:
                    try:
                        ctx = build_game_context(game, attacking_player, opponent,
                                                 card=attacker_card, attacking_creature=attacker_card)
                        lib = get_effect_library()
                        actions, explanation = lib.resolve_attack_trigger(
                            trigger_card_name=attacker_card.name,
                            trigger_oracle=trigger_text,
                            attacking_creature_name=attacker_card.name,
                            attacking_creature_power=attacking_power,
                            controller=attacking_player.name,
                            opponent=opponent.name,
                            game_context=ctx,
                        )
                        if actions is not None:  # [] = deliberate template no-op (handled); only None means unhandled → Tier 3
                            for action in actions:
                                if action.get("action") == "no_action":
                                    reason = action.get("reason", "")
                                    if engine._should_emit_resolve_prompt(game, attacker_card.name, reason):
                                        msg = _format_noop_reason(attacker_card.name, reason)
                                        if msg:
                                            messages.append(msg.replace("📍", "⚔️", 1))
                                    continue
                                try:
                                    msg = engine.rules._execute_action_on_state(game, action)
                                    if msg:
                                        messages.append(msg)
                                except Exception as e:
                                    print(f"[ATTACK-TEMPLATE] Action failed for {attacker_card.name}: {e}")
                            print(f"[ATTACK-TEMPLATE] Resolved {attacker_card.name} attack trigger: {explanation}")
                            handled = True
                    except Exception as e:
                        print(f"[ATTACK-TEMPLATE] Error for {attacker_card.name}: {e}")

            if not handled:
                trigger_text = self_attack_text
                if trigger_text:
                    unhandled.append((attacker_card, trigger_text))

    # 2. Check attacking player's permanents for "whenever a creature you control attacks"
    for card in attacking_player.battlefield:
        if card.id == attacker_card.id:
            continue  # Already handled above
        if getattr(card, '_phased_out', False):
            continue  # Phased-out permanents don't trigger
        if not card.oracle_text:
            continue
        # July 31 batch-10 audit: strip activated-ability lines first — Jaya,
        # Fiery Negotiator's "−2: … Whenever you attack this turn, …" is a
        # LOYALTY ability, and this scan matched its quoted text as a
        # battlefield attack watcher on every combat (16 queued, all Tier-3
        # refused, batch 15324). Loyalty heads ("−2") are activated abilities
        # per CR 606; the strip helper drops those lines. Same class as the
        # Ascendant Spirit combat-damage-scan fix.
        from rules.effect_templates import strip_activated_ability_lines
        oracle_lower = strip_activated_ability_lines(card.oracle_text).lower()
        # June 10 deep-dive (Karlach): the bare "attacks" pre-filter made the
        # "whenever you attack" branch below UNREACHABLE — Karlach, Fury of
        # Avernus's oracle says "Whenever you attack, …" (contains "attack,"
        # but never the literal "attacks"), so her untap + first-strike +
        # extra-combat trigger was silently skipped every combat.
        if "attacks" not in oracle_lower and "whenever you attack" not in oracle_lower:
            continue

        has_attack_trigger = (
            ("whenever a creature you control attacks" in oracle_lower) or
            ("whenever a creature attacks" in oracle_lower) or
            ("whenever you attack" in oracle_lower)
        )
        if not has_attack_trigger:
            continue

        # "whenever you attack" fires once per combat, not per creature
        if "whenever you attack" in oracle_lower and (attacker_card.id != game.attackers[0] if game.attackers else False):
            continue  # Only fire on the first attacker declared

        handled = False

        # ---- HARDCODED ATTACK TRIGGER HANDLERS ----

        # Adeline, Resplendent Cathar: create a 1/1 white Human token tapped and attacking
        if card.name.lower() == "adeline, resplendent cathar":
            token = Card(
                name="Human",
                mana_cost="",
                type_line="Token Creature — Human",
                oracle_text="",
                power="1",
                toughness="1",
            )
            token.is_token = True
            token.tapped = True
            token.summoning_sick = False  # Attacking tokens aren't summoning sick
            token.owner_index = game.players.index(attacking_player)
            attacking_player.battlefield.append(token)
            messages.append(f"⚔️ Adeline creates a 1/1 white Human token tapped and attacking")
            handled = True

        if not handled and HAS_EFFECT_TEMPLATES:
            trigger_text = ""
            for paragraph in card.oracle_text.split('\n'):
                p_lower = paragraph.lower().strip()
                if "attack" in p_lower and "whenever" in p_lower:
                    trigger_text = paragraph.strip()
                    break

            if trigger_text:
                try:
                    ctx = build_game_context(game, attacking_player, opponent,
                                             card=card, attacking_creature=attacker_card)
                    ctx['_trigger_source'] = card.name
                    lib = get_effect_library()
                    actions, explanation = lib.resolve_attack_trigger(
                        trigger_card_name=card.name,
                        trigger_oracle=trigger_text,
                        attacking_creature_name=attacker_card.name,
                        attacking_creature_power=attacking_power,
                        controller=attacking_player.name,
                        opponent=opponent.name,
                        game_context=ctx,
                    )
                    if actions is not None:  # [] = deliberate template no-op (handled); only None means unhandled → Tier 3
                        for action in actions:
                            if action.get("action") == "no_action":
                                reason = action.get("reason", "")
                                if engine._should_emit_resolve_prompt(game, card.name, reason):
                                    msg = _format_noop_reason(card.name, reason)
                                    if msg:
                                        messages.append(msg.replace("📍", "⚔️", 1))
                                continue
                            try:
                                msg = engine.rules._execute_action_on_state(game, action)
                                if msg:
                                    messages.append(msg)
                            except Exception as e:
                                print(f"[ATTACK-TEMPLATE] Action failed for {card.name}: {e}")
                        print(f"[ATTACK-TEMPLATE] Resolved {card.name} attack trigger: {explanation}")
                        handled = True
                except Exception as e:
                    print(f"[ATTACK-TEMPLATE] Error for {card.name}: {e}")

        if not handled:
            trigger_text = ""
            for paragraph in card.oracle_text.split('\n'):
                p_lower = paragraph.lower().strip()
                if "attack" in p_lower and "whenever" in p_lower:
                    trigger_text = paragraph.strip()
                    break
            if trigger_text:
                unhandled.append((card, trigger_text))

    return messages, unhandled


def _check_day_night_and_werewolf_transforms(engine, game: GameState) -> List[str]:
    """Check for day/night transitions and werewolf transform triggers at upkeep."""
    messages = []
    active = game.active_player
    if game.day_night_active:
        # Read the turn that JUST ENDED, per Daybound's printed reminder ("If a
        # player casts no spells during their own turn, it becomes night next
        # turn"). This used to read `active.spells_cast_prev_turn`, i.e. the
        # incoming active player's OWN last turn — a turn older in a two-player
        # game — which flipped day/night on the wrong evidence and changed
        # combat math for every werewolf on the board.
        prev_spells = getattr(game, '_spells_cast_last_turn', 0)
        old_is_day = game.is_day
        if game.is_day and prev_spells == 0:
            game.is_day = False
            messages.append("🌙 **It becomes night!** (no spells were cast last turn)")
            print(f"[TRANSFORM] Day -> Night (0 spells cast on the previous turn)")
        elif not game.is_day and prev_spells >= 2:
            game.is_day = True
            messages.append("☀️ **It becomes day!** (2+ spells were cast last turn)")
            print(f"[TRANSFORM] Night -> Day ({prev_spells} spells cast on the previous turn)")
        if game.is_day != old_is_day:
            for player in game.players:
                for card in player.battlefield:
                    if not card.has_transform:
                        continue
                    oracle_lower = (card.oracle_text or '').lower()
                    back_oracle_lower = (card.back_face_oracle_text or '').lower()
                    has_db = 'daybound' in oracle_lower or 'daybound' in back_oracle_lower
                    has_nb = 'nightbound' in oracle_lower or 'nightbound' in back_oracle_lower
                    if not (has_db or has_nb):
                        continue
                    if game.is_day and card.is_transformed:
                        old_name = card.name
                        card.transform()
                        messages.append(f"🔄 **{old_name}** transforms into **{card.name}** (it is now day)")
                    elif not game.is_day and not card.is_transformed:
                        old_name = card.name
                        card.transform()
                        messages.append(f"🔄 **{old_name}** transforms into **{card.name}** (it is now night)")
    # Classic werewolf transform triggers (non-daybound)
    opponent_idx = 1 - game.active_player_index
    opponent = game.players[opponent_idx]
    # Classic werewolves ask whether NO spells were cast during the previous
    # turn (by anyone), which is the same single turn the day/night check reads
    # — not the sum of two players' own-turn counts from different turns.
    last_turn_spells = getattr(game, '_spells_cast_last_turn', 0)
    for player in game.players:
        for card in player.battlefield:
            if not card.has_transform:
                continue
            oracle_lower = (card.oracle_text or '').lower()
            back_oracle_lower = (card.back_face_oracle_text or '').lower()
            if 'daybound' in oracle_lower or 'nightbound' in oracle_lower:
                continue
            if 'daybound' in back_oracle_lower or 'nightbound' in back_oracle_lower:
                continue
            if (not card.is_transformed
                    and 'at the beginning of each upkeep' in oracle_lower
                    and 'if no spells were cast last turn' in oracle_lower):
                if last_turn_spells == 0:
                    old_name = card.name
                    card.transform()
                    messages.append(f"🐺 **{old_name}** transforms into **{card.name}**! (no spells cast last turn)")
            elif (card.is_transformed
                    and 'at the beginning of each upkeep' in oracle_lower
                    and 'two or more spells' in oracle_lower):
                if last_turn_spells >= 2:
                    old_name = card.name
                    card.transform()
                    messages.append(f"🐺 **{old_name}** transforms back into **{card.name}**! (2+ spells cast last turn)")
            elif (card.is_transformed
                    and 'at the beginning of each upkeep' in back_oracle_lower
                    and 'two or more spells' in back_oracle_lower):
                if last_turn_spells >= 2:
                    old_name = card.name
                    card.transform()
                    messages.append(f"🐺 **{old_name}** transforms back into **{card.name}**! (2+ spells cast last turn)")
    return messages


def _check_upkeep_triggers_sync(engine, game: GameState) -> Tuple[List[str], List[Tuple]]:
    """Check for 'at the beginning of your upkeep' triggers.

    Returns:
        Tuple of (messages, unhandled_triggers).
    """
    # (Slice 6b retired the 6a hook recording that lived here. The upkeep
    # scan deliberately stays advance_phase-invoked — see the scoping
    # decision at the _main_phase_bus_subscriber block.)
    messages = []
    unhandled = []
    active = game.active_player
    active_idx = game.active_player_index
    opponent = game.players[1 - active_idx]

    # NOTE (May 20 audit, APNAP-2): the post-batch audit flagged this Phase 1
    # scan as missing NAP's "each player's upkeep" triggers because it walks
    # only `active.battlefield`. After verification, that claim is a
    # FALSE POSITIVE — the Phase 2 scan at line 2546-2604 already walks
    # NON-active players' battlefields and dispatches "at the beginning of
    # each" patterns (which matches both "each upkeep" and "each player's
    # upkeep"). So NAP cards ARE handled, just via the secondary scan.
    # Phase 1 stays AP-only to avoid double-firing NAP "each" triggers.

    # Scan active player's permanents for "at the beginning of your upkeep"
    for card in active.battlefield:
        if not card.oracle_text:
            continue
        oracle_lower = card.oracle_text.lower()
        if "upkeep" not in oracle_lower:
            continue

        has_upkeep_trigger = (
            "at the beginning of your upkeep" in oracle_lower or
            "at the beginning of each upkeep" in oracle_lower or
            "at the beginning of each player's upkeep" in oracle_lower
        )
        if not has_upkeep_trigger:
            continue

        handled = False

        # ---- HARDCODED HANDLERS ----

        # Phyrexian Arena: draw + lose 1 life
        if card.name.lower() == "phyrexian arena":
            drawn = engine.draw_cards(active, 1, game=game)
            active.life -= 1
            active.record_life_loss(1)
            print(f"[UPKEEP-TRIGGER] Phyrexian Arena: {active.name} loses 1 life (life: {active.life})")
            _log_life_change(active, -1, "Phyrexian Arena")  # May 30 audit: canonical tag
            if drawn:
                messages.append(f"📖 Phyrexian Arena: {active.name} draws a card, loses 1 life")
            handled = True

        # Cumulative upkeep: increment age counter, pay or sacrifice
        # Mystic Remora ({1}), Glacial Chasm (pay life), etc.
        if not handled and "cumulative upkeep" in oracle_lower:
            # Track age counters
            if not hasattr(card, '_age_counters'):
                card._age_counters = 0
            card._age_counters += 1
            age = card._age_counters

            # Parse the cumulative upkeep cost
            cu_match = re.search(r'cumulative upkeep[\s—:]+(.+?)(?:\.|$)', oracle_lower)
            cu_cost_text = cu_match.group(1).strip() if cu_match else "{1}"

            # Calculate total cost (age * per-age cost)
            mana_per_age = sum(int(m) for m in re.findall(r'\{(\d+)\}', cu_cost_text))
            colored_per_age = sum(1 for m in re.findall(r'\{([WUBRGC])\}', cu_cost_text))
            total_per_age = mana_per_age + colored_per_age
            total_cost = total_per_age * age if total_per_age > 0 else age  # Default {1} per age

            # Check if controller can pay
            available_mana = sum(active.available_mana_detailed().values())
            # Cumulative upkeep is OPTIONAL (CR 702.24). Decide whether
            # paying is worth it. For Mystic Remora-style draw engines,
            # paying stops being worth it when the cost exceeds the
            # average CMC of what we'd reasonably expect to draw.
            card_name_lower = card.name.lower()
            # By default, pay if cost <= 3; for Mystic Remora specifically,
            # bail once total cost reaches 4+ (classic ~3-turn lifespan).
            pay_threshold = 3
            if 'mystic remora' in card_name_lower:
                pay_threshold = 3
            elif 'glacial chasm' in card_name_lower:
                # Always worth paying Glacial Chasm's life cost while
                # damage prevention is needed — handled separately.
                pay_threshold = 999
            will_pay = (available_mana >= total_cost) and (total_cost <= pay_threshold)
            if will_pay:
                # Pay the cost (tap lands)
                mana_to_pay = total_cost
                for land in active.battlefield:
                    if mana_to_pay <= 0:
                        break
                    if land.is_land() and not land.tapped:
                        land.tapped = True
                        mana_to_pay -= 1
                for rock in active.battlefield:
                    if mana_to_pay <= 0:
                        break
                    if not rock.is_land() and not rock.tapped and active._can_produce_mana(rock):
                        rock.tapped = True
                        mana_to_pay -= 1
                messages.append(
                    f"⏳ {card.name}: Cumulative upkeep paid ({total_cost} mana, age {age})")
                print(f"[UPKEEP] {card.name} cumulative upkeep: age={age}, cost={total_cost}, paid")
            else:
                # Chose not to pay (or can't) — sacrifice
                if card in active.battlefield:
                    game.unregister_static_effects(card)
                    active.battlefield.remove(card)
                    active.graveyard.append(card)
                reason = "can't pay" if available_mana < total_cost else "declined to pay"
                messages.append(
                    f"⏳ {card.name}: {reason} cumulative upkeep ({total_cost} mana, age {age}) — sacrificed!")
                print(f"[UPKEEP] {card.name} cumulative upkeep: age={age}, cost={total_cost}, {reason} -> sacrificed")
            handled = True

        # ---- TIER 1.5: Template library ----
        if not handled and HAS_EFFECT_TEMPLATES:
            trigger_text = ""
            for paragraph in card.oracle_text.split('\n'):
                p_lower = paragraph.lower().strip()
                if "upkeep" in p_lower and ("at the beginning" in p_lower or "beginning of your upkeep" in p_lower):
                    trigger_text = paragraph.strip()
                    break

            if trigger_text:
                try:
                    ctx = build_game_context(game, active, opponent, card=card)
                    ctx['_trigger_source'] = card.name
                    lib = get_effect_library()
                    actions, explanation = lib.resolve_upkeep_trigger(
                        trigger_card_name=card.name,
                        trigger_oracle=trigger_text,
                        controller=active.name,
                        opponent=opponent.name,
                        game_context=ctx,
                    )
                    # Template dispatch convention:
                    #   actions is None  → no template matched (fall through)
                    #   actions == []    → template matched, silent no-op (e.g.
                    #                      Emeria with <7 Plains) — handled
                    #   actions non-empty → execute
                    if actions is not None:
                        for action in actions:
                            if action.get("action") == "no_action":
                                reason = action.get("reason", "")
                                msg = _format_noop_reason(card.name, reason)
                                if msg:
                                    messages.append(msg)
                                continue
                            try:
                                msg = engine.rules._execute_action_on_state(game, action)
                                if msg:
                                    messages.append(msg)
                            except Exception as e:
                                print(f"[UPKEEP-TEMPLATE] Action failed for {card.name}: {e}")
                        # May 7 audit fix #8: Inventors' Fair / Charming Prince
                        # return [{"action": "no_action", "reason": "..."}] when
                        # the conditional isn't met. The old "if actions:"
                        # treated this list as truthy and logged "Resolved" —
                        # misleading. Detect all-no_action lists and log them
                        # as conditional-not-met instead.
                        any_real_action = any(
                            a.get("action") != "no_action" for a in actions
                        )
                        if any_real_action:
                            print(f"[UPKEEP-TEMPLATE] Resolved {card.name}: {explanation}")
                        elif actions:
                            # Surface the no_action reason so the audit log shows
                            # *why* the condition didn't fire.
                            cond_reason = next(
                                (a.get("reason", "") for a in actions
                                 if a.get("action") == "no_action" and a.get("reason")),
                                ""
                            )
                            print(f"[UPKEEP-TEMPLATE] Conditional not met for {card.name}: {cond_reason or explanation}")
                        else:
                            print(f"[UPKEEP-TEMPLATE] {card.name}: silent no-op (condition not met)")
                        handled = True
                except Exception as e:
                    print(f"[UPKEEP-TEMPLATE] Error for {card.name}: {e}")

        if not handled:
            trigger_text = ""
            for paragraph in card.oracle_text.split('\n'):
                p_lower = paragraph.lower().strip()
                if "upkeep" in p_lower and "at the beginning" in p_lower:
                    trigger_text = paragraph.strip()
                    break
            if trigger_text:
                unhandled.append((card, trigger_text))

    # Also scan ALL permanents for "at the beginning of each upkeep" (opponent-side)
    for player in game.players:
        if player == active:
            continue  # Already scanned above
        for card in player.battlefield:
            if not card.oracle_text:
                continue
            oracle_lower = card.oracle_text.lower()
            if "at the beginning of each" not in oracle_lower or "upkeep" not in oracle_lower:
                continue

            ctrl_idx = game.players.index(player)
            ctrl = player
            opp = game.players[1 - ctrl_idx]

            if HAS_EFFECT_TEMPLATES:
                trigger_text = ""
                for paragraph in card.oracle_text.split('\n'):
                    p_lower = paragraph.lower().strip()
                    if "upkeep" in p_lower and "each" in p_lower:
                        trigger_text = paragraph.strip()
                        break
                if trigger_text:
                    try:
                        ctx = build_game_context(game, ctrl, opp, card=card)
                        ctx['_trigger_source'] = card.name
                        lib = get_effect_library()
                        actions, explanation = lib.resolve_upkeep_trigger(
                            trigger_card_name=card.name,
                            trigger_oracle=trigger_text,
                            controller=ctrl.name,
                            opponent=opp.name,
                            game_context=ctx,
                        )
                        # Template match convention: None → not found,
                        # [] → matched silent no-op, non-empty → execute
                        if actions is not None:
                            for action in actions:
                                if action.get("action") == "no_action":
                                    reason = action.get("reason", "")
                                    msg = _format_noop_reason(card.name, reason)
                                    if msg:
                                        messages.append(msg)
                                    continue
                                try:
                                    msg = engine.rules._execute_action_on_state(game, action)
                                    if msg:
                                        messages.append(msg)
                                except Exception as e:
                                    print(f"[UPKEEP-TEMPLATE] Action failed for {card.name}: {e}")
                            # July 31 batch-10 reviewer: the May 7 label fix
                            # ("Resolved" only when a real action ran) never
                            # reached this Phase-2 (non-active-player) scan —
                            # an all-no_action list is truthy, so Lambholt
                            # Pacifist's static stub printed "Resolved" on
                            # every OPPONENT upkeep while the active-player
                            # scan correctly said "Conditional not met".
                            if any(a.get("action") != "no_action" for a in actions):
                                print(f"[UPKEEP-TEMPLATE] Resolved {card.name}: {explanation}")
                            elif actions:
                                print(f"[UPKEEP-TEMPLATE] Conditional not met for {card.name}: {explanation}")
                            else:
                                print(f"[UPKEEP-TEMPLATE] {card.name}: silent no-op")
                            continue  # Handled
                    except Exception as e:
                        print(f"[UPKEEP-TEMPLATE] Error for {card.name}: {e}")
                    unhandled.append((card, trigger_text))

    return messages, unhandled


def _check_beginning_combat_triggers_sync(engine, game: GameState) -> Tuple[List[str], List[Tuple]]:
    """Check for 'At the beginning of combat on your turn' triggers — Luminarch
    Aspirant (+1/+1 counter), Goblin Rabblemaster (Goblin token), Hero of
    Bladehold, etc. May 30 audit: this whole trigger class was UNWIRED — the
    template and the effect_templates `beginning_combat` event type existed, but
    nothing in mtg/ ever dispatched it (advance_phase's COMBAT_BEGIN branch only
    printed a display string), so the triggers silently never fired.

    "on your turn" -> only the active player's permanents trigger.
    Returns (messages, unhandled_triggers)."""
    messages = []
    unhandled = []
    active = game.active_player
    active_idx = game.active_player_index
    opponent = game.players[1 - active_idx]
    lib = get_effect_library() if HAS_EFFECT_TEMPLATES else None

    for card in list(active.battlefield):
        if getattr(card, '_phased_out', False) or not card.oracle_text:
            continue
        oracle_lower = card.oracle_text.lower()
        if "beginning of combat" not in oracle_lower or card.is_planeswalker():
            continue
        # Isolate the trigger paragraph (mirrors the end-step extraction).
        trigger_text = ""
        for paragraph in card.oracle_text.split('\n'):
            if "at the beginning of combat" in paragraph.lower():
                trigger_text = paragraph.strip()
                break
        if not trigger_text:
            continue
        handled = False
        if lib is not None:
            try:
                ctx = build_game_context(game, active, opponent, card=card)
                ctx['_trigger_source'] = card.name
                actions, explanation = lib.resolve_etb(
                    card_name=card.name, oracle_text=trigger_text,
                    controller=active.name, opponent=opponent.name,
                    game_context=ctx, event_type="beginning_combat",
                )
                if actions:
                    for action in actions:
                        if action.get("action") == "no_action":
                            continue
                        try:
                            msg = engine.rules._execute_action_on_state(game, action)
                            if msg:
                                messages.append(msg)
                        except Exception as e:
                            print(f"[COMBAT-BEGIN-TEMPLATE] Action failed for {card.name}: {e}")
                    print(f"[COMBAT-BEGIN-TRIGGER] Resolved {card.name}: {explanation}")
                    handled = True
            except Exception as e:
                print(f"[COMBAT-BEGIN-TEMPLATE] Error for {card.name}: {e}")
        if not handled:
            unhandled.append((card, trigger_text))
    return messages, unhandled


def _check_main_phase_triggers_sync(engine, game: GameState,
                                    precombat: bool) -> Tuple[List[str], List[Tuple]]:
    """Check "At the beginning of your pre/postcombat main phase" triggers.

    July 27, 2026: this whole trigger class was UNWIRED — exactly the shape the
    May 30 `beginning_combat` finding had. The MAIN1/MAIN2 branches of
    advance_phase only printed a banner and drained one-shot DELAYED triggers
    (`_process_delayed_triggers(game, "main_phase")`, which is Necropotence-style
    scheduling, not a battlefield scan), and `scheduled_event_types` in
    rules/effect_templates.py excluded the event entirely. So no permanent's
    main-phase trigger could fire, ever.

    Found because Tymna the Weaver — a COMMANDER whose whole card-advantage
    engine is "At the beginning of each of your postcombat main phases, you may
    pay X life ... draw X cards" — did nothing across a full game. Same shape as
    Baral's cost reduction: a commander whose defining ability was a no-op.

    "your ... main phase" -> only the active player's permanents trigger.
    Returns (messages, unhandled_triggers)."""
    messages = []
    unhandled = []
    active = game.active_player
    active_idx = game.active_player_index
    opponent = game.players[1 - active_idx]
    lib = get_effect_library() if HAS_EFFECT_TEMPLATES else None
    want = 'precombat main phase' if precombat else 'postcombat main phase'
    tag = 'MAIN1' if precombat else 'MAIN2'

    for card in list(active.battlefield):
        if getattr(card, '_phased_out', False) or not card.oracle_text:
            continue
        oracle_lower = card.oracle_text.lower()
        if want not in oracle_lower or card.is_planeswalker():
            continue
        # Isolate the trigger paragraph (mirrors the beginning-combat extraction).
        trigger_text = ""
        for paragraph in card.oracle_text.split('\n'):
            if want in paragraph.lower():
                trigger_text = paragraph.strip()
                break
        if not trigger_text:
            continue
        handled = False
        if lib is not None:
            try:
                ctx = build_game_context(game, active, opponent, card=card)
                ctx['_trigger_source'] = card.name
                # Tymna-style "opponents dealt combat damage this turn" —
                # exposed here because only the trigger knows to ask.
                ctx['_opponents_dealt_combat_damage'] = sum(
                    1 for p in game.players
                    if p is not active
                    and getattr(p, 'dealt_combat_damage_this_turn', False))
                actions, explanation = lib.resolve_etb(
                    card_name=card.name, oracle_text=trigger_text,
                    controller=active.name, opponent=opponent.name,
                    game_context=ctx, event_type="main_phase",
                )
                if actions:
                    for action in actions:
                        if action.get("action") == "no_action":
                            continue
                        try:
                            msg = engine.rules._execute_action_on_state(game, action)
                            if msg:
                                messages.append(msg)
                        except (ValueError, KeyError, AttributeError,
                                TypeError, IndexError) as e:
                            print(f"[{tag}-TEMPLATE] Action failed for {card.name}: {e}")
                            maybe_reraise(e)
                    print(f"[{tag}-TRIGGER] Resolved {card.name}: {explanation}")
                    handled = True
                elif actions == []:
                    # Library contract: [] is a deliberate handled no-op (the
                    # condition was false), None means unhandled. Do NOT
                    # escalate — that is the July 21 Meren regression.
                    print(f"[{tag}-TRIGGER] {card.name}: handled no-op "
                          f"(condition not met)")
                    handled = True
            except (ValueError, KeyError, AttributeError,
                    TypeError, IndexError) as e:
                print(f"[{tag}-TEMPLATE] Error for {card.name}: {e}")
                maybe_reraise(e)
        if not handled:
            unhandled.append((card, trigger_text))
    return messages, unhandled


def _check_end_step_triggers_sync(engine, game: GameState) -> Tuple[List[str], List[Tuple]]:
    """Check for 'at the beginning of the end step' triggers.

    Handles Through the Breach / Sneak Attack sacrifice timing.

    Returns:
        Tuple of (messages, unhandled_triggers).
    """
    messages = []
    unhandled = []
    active = game.active_player
    active_idx = game.active_player_index
    opponent = game.players[1 - active_idx]

    # Scan ALL players' permanents for end step triggers
    # ("at the beginning of each end step" fires for all players, not just active)
    cards_to_sacrifice = []
    all_bf_cards = []
    for p in game.players:
        for c in p.battlefield:
            if not getattr(c, '_phased_out', False):
                all_bf_cards.append((c, p))

    # Sneak Attack / Through the Breach: creatures marked for end-step sacrifice
    # by another permanent's activated ability (the sneaked creature itself has no
    # such oracle text — the trigger lives on Sneak Attack, which stays on board).
    for card, card_owner in all_bf_cards:
        if getattr(card, '_sneak_attack_sac', False):
            cards_to_sacrifice.append(card)
            messages.append(f"📍 End step: {card.name} is sacrificed (Sneak Attack)")
            # Clear the flag so we don't double-process if SBA loop re-enters
            card._sneak_attack_sac = False
    for card, card_owner in all_bf_cards:
        if not card.oracle_text:
            continue
        oracle_lower = card.oracle_text.lower()
        if "end step" not in oracle_lower:
            continue

        # Skip "at the beginning of the next end step" — these are one-shot delayed
        # triggers created by ability activations (e.g. Teferi Hero +1 untap lands,
        # Charming Prince flicker return, Roon of the Hidden Realm). They are handled
        # by the delayed_triggers system, not by scanning oracle text every end step.
        # Also skip planeswalkers — their end step text is part of ability descriptions,
        # not static triggered abilities on the permanent.
        if card.is_planeswalker():
            continue

        has_recurring_end_step = (
            "at the beginning of your end step" in oracle_lower or
            "at the beginning of each end step" in oracle_lower
        )
        # "at the beginning of the end step" (without "next") is ambiguous —
        # only match it if there's no "next" qualifier
        has_generic_end_step = (
            "at the beginning of the end step" in oracle_lower and
            "at the beginning of the next end step" not in oracle_lower
        )
        if not has_recurring_end_step and not has_generic_end_step:
            continue

        # "at the beginning of your end step" should only fire on the controller's turn
        if ("at the beginning of your end step" in oracle_lower and
                "at the beginning of each end step" not in oracle_lower):
            if card_owner != active:
                continue

        handled = False

        # Check for sacrifice at end step (Ball Lightning-class printed
        # self-sacrifice; Sneak Attack / Through the Breach ride the delayed
        # -trigger scheduler instead).
        # July 31 batch-11 (madness reviewer, REPRODUCED): the old
        # whole-oracle `"sacrifice" in oracle and "end step" in oracle`
        # conjunction matched UNRELATED clauses — Herald of Anguish
        # ("{1}{B}, Sacrifice an artifact: ..." + "At the beginning of your
        # end step, each opponent discards a card") was auto-sacrificed on
        # every end step, twice in game_1532532252825616466, AND handled=True
        # suppressed his real discard trigger. Sixth instance of the
        # substring family. Require ONE sentence containing both the
        # end-step schedule and a SELF-sacrifice.
        _self_sac_re = re.compile(
            r'sacrifice (?:it|this creature|this permanent|'
            + re.escape(card.name.lower()) + r')\b')
        _self_sac_at_end_step = any(
            'end step' in _sent and _self_sac_re.search(_sent)
            for _sent in re.split(r'[.\n]', oracle_lower))
        if _self_sac_at_end_step:
            # Mark for sacrifice after processing all triggers
            cards_to_sacrifice.append(card)
            messages.append(f"📍 End step: {card.name} is sacrificed")
            handled = True

        # ---- TIER 1.5: Template library ----
        if not handled and HAS_EFFECT_TEMPLATES:
            trigger_text = ""
            for paragraph in card.oracle_text.split('\n'):
                p_lower = paragraph.lower().strip()
                if "end step" in p_lower and ("at the beginning" in p_lower):
                    trigger_text = paragraph.strip()
                    break

            if trigger_text:
                try:
                    trigger_ctrl = card_owner
                    trigger_opp = game.players[1 - game.players.index(trigger_ctrl)] if len(game.players) == 2 else opponent
                    ctx = build_game_context(game, trigger_ctrl, trigger_opp, card=card)
                    ctx['_trigger_source'] = card.name
                    lib = get_effect_library()
                    # Use resolve_etb since end step triggers share same action format.
                    # event_type="end_step" disables the scheduled-prefix guard so
                    # name-keyed end-step templates (Meren, Bloodchief Ascension,
                    # Bitterblossom etc.) fire instead of falling through to
                    # partial pattern matches.
                    actions, explanation = lib.resolve_etb(
                        card_name=card.name,
                        oracle_text=trigger_text,
                        controller=trigger_ctrl.name,
                        opponent=trigger_opp.name,
                        game_context=ctx,
                        event_type="end_step",
                    )
                    if actions is not None:  # [] = deliberate template no-op (handled); only None means unhandled → Tier 3
                        _executed_real = False
                        for action in actions:
                            if action.get("action") == "no_action":
                                reason = action.get("reason", "")
                                # Suppress noisy no-op messages from Discord — log only
                                if reason:
                                    print(f"[ENDSTEP-TRIGGER] {card.name}: {reason} (suppressed)")
                                continue
                            _executed_real = True
                            try:
                                msg = engine.rules._execute_action_on_state(game, action)
                                if msg:
                                    messages.append(msg)
                            except Exception as e:
                                print(f"[ENDSTEP-TEMPLATE] Action failed for {card.name}: {e}")
                        # July 31 batch-11 (cube reviewer): the truthy
                        # "Resolved" label printed even when every action was
                        # a no_action (Agent of Treachery below threshold) —
                        # the upkeep scan's July-31 sibling, in the end-step
                        # scan. Console-only, but audits read these labels.
                        if _executed_real:
                            print(f"[ENDSTEP-TRIGGER] Resolved {card.name}: {explanation}")
                        else:
                            print(f"[ENDSTEP-TRIGGER] {card.name}: handled no-op (condition not met)")
                        handled = True
                except Exception as e:
                    print(f"[ENDSTEP-TEMPLATE] Error for {card.name}: {e}")

        if not handled:
            trigger_text = ""
            for paragraph in card.oracle_text.split('\n'):
                p_lower = paragraph.lower().strip()
                if "end step" in p_lower and "at the beginning" in p_lower:
                    trigger_text = paragraph.strip()
                    break
            if trigger_text:
                unhandled.append((card, trigger_text))

    # Process sacrifices (Through the Breach / Sneak Attack cleanup)
    for card in cards_to_sacrifice:
        if card in active.battlefield:
            game.unregister_static_effects(card)
            active.battlefield.remove(card)
            if card.is_commander and game.format in COMMAND_ZONE_FORMATS:
                card.reset_battlefield_state()  # Clear damage/modifiers so recast starts clean
                active.command_zone.append(card)
            else:
                active.graveyard.append(card)

    return messages, unhandled


def _place_triggers_on_stack(engine, game: GameState, trigger_infos: List[Tuple],
                             trigger_event: str = "unknown") -> List[str]:
    """Place detected triggers on the stack using APNAP ordering.

    Called when game.triggers_use_stack is True. Creates StackEntry objects
    for each trigger, ordered by APNAP rules (active player's triggers
    go on stack first → resolve last).

    Args:
        game: The game state
        trigger_infos: List of (source_card, controller_player, trigger_text) tuples
        trigger_event: The event type ("dies", "attacks", "upkeep", "end_step")

    Returns:
        Messages describing what was placed on the stack
    """
    if not trigger_infos:
        return []

    messages = []

    try:
        from rules.priority import TriggerManager, TriggeredAbility
    except ImportError:
        # Fallback: just resolve immediately without ordering
        print("[TRIGGER-STACK] TriggerManager not available, resolving immediately")
        return []

    # Build player names
    player_names = [p.name for p in game.players]
    active_name = game.active_player.name

    # Create TriggerManager and add all triggers
    tm = TriggerManager(player_names, active_name)
    trigger_map = {}  # Map trigger text hash → (source_card, controller_player)

    for source_card, controller_player, trigger_text in trigger_infos:
        ability = TriggeredAbility(
            source=source_card.name,
            controller=controller_player.name,
            trigger_text=trigger_text,
            trigger_event=trigger_event,
        )
        tm.add_trigger(ability)
        # Store mapping for stack entry creation
        key = f"{source_card.name}_{trigger_text}_{controller_player.name}"
        trigger_map[key] = (source_card, controller_player)

    # Get APNAP-ordered triggers
    ordered = tm.get_all_ordered_triggers()

    # Create StackEntry for each trigger (bottom-up: first in list = bottom of stack)
    for ability in ordered:
        key = f"{ability.source}_{ability.trigger_text}_{ability.controller}"
        source_card, controller_player = trigger_map.get(key, (None, None))
        if not source_card or not controller_player:
            continue

        ctrl_idx = game.players.index(controller_player) if controller_player in game.players else 0

        entry = StackEntry(
            card=source_card,
            controller_name=controller_player.name,
            controller_index=ctrl_idx,
            is_spell=False,
            trigger_source=ability.source,
            trigger_text=ability.trigger_text,
        )
        game.stack.append(entry)
        messages.append(f"📚 [{trigger_event.upper()}] {ability.source}'s trigger goes on the stack")
        print(f"[TRIGGER-STACK] Placed {ability.source} ({trigger_event}) on stack "
              f"(controller: {ability.controller})")

        # If priority system is active, create corresponding StackObject
        if game._priority_system:
            try:
                from rules.priority import StackObject
                stack_obj = StackObject(
                    card_name=ability.source,
                    controller=ability.controller,
                    is_spell=False,
                )
                game._priority_system.stack.append(stack_obj)
                entry.priority_id = stack_obj.id
            except Exception as e:
                print(f"[TRIGGER-STACK] Error creating StackObject: {e}")

    return messages


def _handle_etb_triggers(engine, game: GameState, player: Player, card: Card) -> List[str]:
    """Handle all enter-the-battlefield triggers for a card."""
    messages = []
    # Track triggers handled by hardcoded Tier 1 below — prevents double-fire
    # when _check_creature_etb_triggers_sync also scans the same permanents
    game._handled_triggers_this_etb = set()

    # Check for "whenever another creature enters" triggers (Terror of the Peaks, etc.)
    if card.is_creature():
        # Slice 2b (July 21): the watcher dispatch (scan + XMage + Tier-3
        # queue) runs in the PERMANENT_ENTERED subscriber — every caller of
        # this funnel sits downstream of an emit. Drain in place.
        from mtg.helpers import drain_pending_messages as _drain_pm_f
        messages.extend(_drain_pm_f(game))
    else:
        messages.extend(_check_permanent_etb_watchers(
            engine, game, player, card))

    # Pub/sub slice 2 (July 20, 2026): the June 10 B9 fix added constellation
    # watchers to the CAST path only — enchantments entering via this noncast
    # funnel (move_card, mass_flicker, reanimate) skipped Eidolon of Blossoms
    # entirely. Same gap, other half. Found while wiring the PERMANENT_ENTERED
    # parity recorder, which would have flagged it next batch anyway.
    if card.is_enchantment():
        # Slice 2b (2/2, July 21): constellation dispatch runs in the
        # PERMANENT_ENTERED subscriber. Drain in place.
        from mtg.helpers import drain_pending_messages as _drain_pm_e
        messages.extend(_drain_pm_e(game))

    # Check for engine-ETB triggers on the card itself
    if card.oracle_text:
        oracle_lower = card.oracle_text.lower()

        # Guardian Project: "Whenever a nontoken creature enters the battlefield under your control,
        # if it doesn't have the same name as another creature you control, draw a card."
        if "guardian project" in [c.name.lower() for c in player.battlefield if c.name.lower() == "guardian project"]:
            if card.is_creature() and not getattr(card, 'is_token', False):
                # Check for duplicate names
                same_name_count = sum(1 for c in player.battlefield if c.name == card.name)
                if same_name_count <= 1:  # Only this one
                    drawn_cards = engine.draw_cards(player, 1, game=game)
                    if drawn_cards:
                        messages.append(f"🎴 Guardian Project: drew a card")
                    # Mark as handled to prevent double-resolution by generic trigger scan
                    if not hasattr(game, '_handled_triggers_this_etb'):
                        game._handled_triggers_this_etb = set()
                    game._handled_triggers_this_etb.add("guardian project")
        
        # Garruk's Uprising: Draw when a creature YOU CONTROL with power 4+ enters.
        # Oracle: "Whenever a creature you control with power 4 or greater enters, draw a card."
        # Must check: (a) dedup set so we don't double-fire after
        # _check_creature_etb_triggers_sync already processed it, and (b) the trigger
        # controller is the same as the entering creature's controller (both must be `player`).
        if "garruk's uprising" not in getattr(game, '_handled_triggers_this_etb', set()):
            for perm in player.battlefield:
                if perm.name.lower() == "garruk's uprising" and perm != card:
                    if card.is_creature():
                        power = card.get_effective_power(game)
                        if power >= 4:
                            drawn_cards = engine.draw_cards(player, 1, game=game)
                            if drawn_cards:
                                messages.append(f"🎴 Garruk's Uprising: drew a card (power 4+ creature entered)")
                            if not hasattr(game, '_handled_triggers_this_etb'):
                                game._handled_triggers_this_etb = set()
                            game._handled_triggers_this_etb.add("garruk's uprising")
                            break  # Only fire once even if controller has multiple copies
        
        # Warstorm Surge: Deal damage equal to creature's power
        for perm in player.battlefield:
            if perm.name.lower() == "warstorm surge" and perm != card:
                if card.is_creature(game):  # May 30 audit: devotion-god ETB (CR 603.2a)
                    power = card.get_effective_power(game)
                    if power > 0:
                        player_idx = game.players.index(player)
                        opponent = game.players[1 - player_idx]
                        actual_dmg = engine.rules._apply_noncombat_damage_to_player(game, opponent, power, "Warstorm Surge")
                        if actual_dmg > 0:
                            messages.append(f"🔥 Warstorm Surge deals {actual_dmg} damage to {opponent.name}!")
                            if opponent.life <= 0:
                                game.ended = True
                                game.winner = player_idx
                                messages.append(f"💀 {opponent.name} loses the game!")
    
    return messages


def _handle_land_etb(engine, game: GameState, player: Player, card: Card) -> List[str]:
    """Handle enter-the-battlefield triggers for lands.

    Covers:
    - Specific lands with known ETBs (Ugin's Labyrinth, fetch lands, etc.)
    - Generic oracle text detection for any land with 'when ~ enters'
    - Triggers from other permanents when a land enters (e.g. Lotus Cobra landfall)
    """
    messages = []
    oracle = card.oracle_text.lower() if card.oracle_text else ""
    card_lower = card.name.lower()
    player_idx = game.players.index(player) if player in game.players else 0
    opponent = game.players[1 - player_idx]

    # Track landfall count for Omnath and similar multi-landfall cards
    player.landfall_count_this_turn += 1
    print(f"[LANDFALL] {player.name}: land #{player.landfall_count_this_turn} this turn ({card.name})")

    # === SPECIFIC LAND HANDLERS ===
    
    # Ugin's Labyrinth: "you may exile a colorless card with mana value 7 or greater from your hand"
    if "ugin's labyrinth" in card_lower:
        eligible = [c for c in player.hand 
                   if c.cmc >= 7 and engine._is_colorless_card(c)]
        if eligible:
            names = ", ".join(c.name for c in eligible[:5])
            messages.append(
                f"🔮 **Ugin's Labyrinth** enters! You may exile a colorless card "
                f"with MV 7+ from your hand.\n"
                f"  Eligible: {names}\n"
                f"  Use `!exile <card name>` to imprint, or ignore to skip."
            )
        else:
            messages.append(
                f"🔮 **Ugin's Labyrinth** enters! (No eligible colorless MV 7+ cards in hand to exile.)"
            )
    
    # Bojuka Bog: "exile all cards from target player's graveyard"
    elif "bojuka bog" in card_lower:
        if opponent.graveyard:
            count = len(opponent.graveyard)
            opponent.exile.extend(opponent.graveyard)
            opponent.graveyard.clear()
            messages.append(f"⚰️ Bojuka Bog exiles {count} card(s) from {opponent.name}'s graveyard!")
        else:
            messages.append(f"⚰️ Bojuka Bog enters (opponent's graveyard was empty).")
    
    # Halimar Depths / similar: "look at top 3 cards, put them back in any order".
    # We don't model library order, so this is effectively a no-op. The previous
    # message added a "Use !judge to resolve manually" hint that was misleading
    # (the !judge path also no-ops). Brief informational announcement instead.
    elif "halimar depths" in card_lower:
        messages.append(
            f"🔍 **Halimar Depths** enters (looks at top 3 of library)."
        )
    
    # Kabira Takedown (MDFC lands don't usually have ETBs but just in case)
    # Radiant Fountain: "gain 2 life"
    elif "radiant fountain" in card_lower:
        player.life += 2
        messages.append(f"💧 Radiant Fountain: {player.name} gains 2 life! ({player.life})")
        # Apr 30 audit: console mirror for log reconciliation
        print(f"[LIFE-GAIN] {player.name}: +2 life → {player.life} (Radiant Fountain ETB)")
    
    # Bounce lands (Simic Growth Chamber, Azorius Chancery, Gruul Turf, etc.)
    # Bug #13: "When ~ enters, return a land you control to its owner's hand"
    elif 'return a land you control' in oracle or 'return a land' in oracle:
        basic_land_names = ['Plains', 'Island', 'Swamp', 'Mountain', 'Forest']
        other_lands = [c for c in player.battlefield if c.is_land() and c.id != card.id]
        if other_lands:
            # Prefer returning a basic land (less impactful)
            basics = [c for c in other_lands if c.name in basic_land_names]
            bounce_target = basics[0] if basics else other_lands[0]
            game.unregister_static_effects(bounce_target)
            player.battlefield.remove(bounce_target)
            bounce_target.tapped = False
            player.hand.append(bounce_target)
            messages.append(f"🏠 {card.name} bounces {bounce_target.name} to hand")
            print(f"[BOUNCE-LAND] {card.name} returned {bounce_target.name} to hand")
        else:
            # No other lands: must bounce itself (prevents infinite loop)
            game.unregister_static_effects(card)
            player.battlefield.remove(card)
            card.tapped = False
            player.hand.append(card)
            messages.append(f"🏠 {card.name} bounces itself (only land)")
            print(f"[BOUNCE-LAND] {card.name} returned itself (no other lands)")

    # Sejiri Steppe: "target creature you control gains protection from a color"
    elif "sejiri steppe" in card_lower:
        messages.append(
            f"🛡️ **Sejiri Steppe** enters! Target creature you control gains protection "
            f"from a color until end of turn.\n  *(Use `!judge` to resolve.)*"
        )
        game.pending_resolves.append(
            "Sejiri Steppe ETB: target creature you control gains protection from a color until end of turn"
        )
    
    # === GENERIC LAND ETB DETECTION ===
    # If we didn't handle it specifically, check oracle text for ETB patterns
    if not messages and oracle:
        # Skip lands whose ETB was already handled by _check_enters_tapped
        # (shocklands, checklands, fastlands, etc. — "pay 2 life" / "enters tapped unless")
        already_handled = (
            ("you may pay 2 life" in oracle and "enters tapped" in oracle) or
            ("enters tapped unless" in oracle) or
            ("enters the battlefield tapped unless" in oracle) or
            ("enters tapped" in oracle and "you may pay" not in oracle and "when" not in oracle and "as" not in oracle.replace("enters tapped", ""))
        )

        # Look for "when ~ enters" or "when this land enters"
        card_name_pattern = card.name.lower().replace(",", "").replace("'", "")
        has_etb = False

        if not already_handled:
            if "when" in oracle and "enters" in oracle:
                has_etb = True
            elif "as" in oracle and "enters" in oracle:
                has_etb = True

        if has_etb:
            # Tier 1.5: Try template library FIRST (handles Temple scry, Obscura Storefront, etc.)
            template_matched = False
            if HAS_EFFECT_TEMPLATES:
                try:
                    lib = get_effect_library()
                    etb_text = card.oracle_text or ""
                    ctx = build_game_context(game, player, opponent, card=card)
                    actions, explanation = lib.resolve_etb(
                        card_name=card.name,
                        oracle_text=etb_text,
                        controller=player.name,
                        opponent=opponent.name,
                        game_context=ctx,
                    )
                    if actions is not None:
                        # Template matched the card. Even if individual actions
                        # produce no Discord message (scry that didn't change
                        # library order, no_action that's pure status), the
                        # template DID handle the trigger — don't fall through
                        # to the !judge fallback.
                        template_matched = True
                        for action in actions:
                            action_type = action.get("action", "")
                            if action_type == "no_action":
                                reason = action.get("reason", "")
                                # Strip leading card name to avoid doubling.
                                msg = _format_noop_reason(card.name, reason)
                                if msg:
                                    # Land ETB style — keep scroll emoji on user-visible no-ops.
                                    messages.append(msg.replace("📍", "📜", 1))
                                continue
                            try:
                                msg = engine.rules._execute_action_on_state(game, action)
                                if msg:
                                    messages.append(msg)
                            except Exception as e:
                                print(f"[LAND-ETB-TEMPLATE] Action failed for {card.name}: {action} — {e}")
                        if messages:
                            print(f"[LAND-ETB-TEMPLATE] Resolved {card.name} via template: {explanation}")
                        else:
                            print(f"[LAND-ETB-TEMPLATE] {card.name}: silent resolution (no visible state change)")
                except Exception as e:
                    print(f"[LAND-ETB-TEMPLATE] Error for {card.name}: {e}")

            # Fallback: emit !judge hint only when no template matched at all.
            # When a template matched but produced no visible message (scry,
            # no_action), the trigger was handled — don't show !judge prompt.
            if not messages and not template_matched:
                for line in card.oracle_text.split('\n'):
                    line_lower = line.lower()
                    if ("enters" in line_lower and
                        ("when" in line_lower or "as" in line_lower)):
                        if _should_emit_resolve_hint(game, f"land_etb:{card.name}"):
                            messages.append(
                                f"📜 **{card.name}** ETB: *{line.strip()}*\n"
                                f"  (Use `!judge` or appropriate command to resolve.)"
                            )
                        # Track for AI (always, even if hint suppressed)
                        game.pending_resolves.append(
                            f"{card.name} ETB: {line.strip()[:150]}"
                        )
                        break
    
    # === LANDFALL TRIGGERS FROM OTHER PERMANENTS ===
    # CRITICAL: snapshot the list to avoid mutation during iteration.
    # Scute Swarm creates copies that get picked up by the iterator,
    # causing exponential token explosion (339K tokens from 1 land drop).
    for perm in list(player.battlefield):
        if perm.id == card.id:
            continue
        if not perm.oracle_text:
            continue
        perm_oracle = perm.oracle_text.lower()
        
        # Lotus Cobra: "Whenever a land enters the battlefield under your control, add one mana"
        if "lotus cobra" in perm.name.lower() or (
            "whenever a land enters" in perm_oracle and "add" in perm_oracle
        ):
            messages.append(f"🐍 {perm.name}: Landfall! Add one mana of any color.")

        # Quest-counter landfall (Khalni Heart Expedition): "you may put a
        # quest counter on this enchantment" — autoplay always says yes.
        # June 11 audit: this trigger only printed oracle text plus
        # "(Use `!judge` to resolve if needed.)" and no counter was ever
        # placed (game 1514633271047225385 — the sac ability then resolved
        # cost-free with 0 counters).
        elif ("quest counter" in perm_oracle
              and ("whenever a land" in perm_oracle or "landfall" in perm_oracle)):
            _q_add = 1
            _rep = getattr(game, '_replacement_engine', None)
            if _rep is not None and getattr(_rep, 'effects', None):
                try:
                    from rules.replacement import GameEvent, EventType
                    _qev = GameEvent(
                        event_type=EventType.COUNTER_PLACED,
                        affected_object=perm.id,
                        affected_object_name=perm.name,
                        affected_player=player.name,
                        amount=1,
                        counter_type='quest',
                        source_name=perm.name,
                    )
                    _q_add = _rep.process_event_sync(_qev).amount
                except (ImportError, ValueError, KeyError, AttributeError, TypeError) as _q_err:
                    print(f"[LANDFALL] quest-counter replacement check failed: {_q_err}")
            perm.counters['quest'] = perm.counters.get('quest', 0) + _q_add
            messages.append(f"🗺️ {perm.name}: Landfall! Quest counter added "
                            f"(total: {perm.counters['quest']})")
            print(f"[LANDFALL] {perm.name}: +{_q_add} quest counter "
                  f"(total {perm.counters['quest']})")
        
        # Avenger of Zendikar: "Whenever a land enters... put a +1/+1 counter on each Plant"
        elif "avenger of zendikar" in perm.name.lower() and "plant" in perm_oracle:
            plant_count = sum(1 for c in player.battlefield
                            if c.type_line and "plant" in c.type_line.lower())
            if plant_count > 0:
                # June 11 audit: route each placement through the replacement
                # engine — this path incremented dicts directly, so Doubling
                # Season doubled token creation and quest counters but NOT
                # landfall counters (game 1514626038192144445: Plants attacked
                # at half strength, delaying a won game two turns).
                _per_plant_total = 0
                for c in player.battlefield:
                    if c.type_line and "plant" in c.type_line.lower():
                        _add = 1
                        _rep = getattr(game, '_replacement_engine', None)
                        if _rep is not None and getattr(_rep, 'effects', None):
                            try:
                                from rules.replacement import GameEvent, EventType
                                _ev = GameEvent(
                                    event_type=EventType.COUNTER_PLACED,
                                    affected_object=c.id,
                                    affected_object_name=c.name,
                                    affected_player=player.name,
                                    amount=1,
                                    counter_type='+1/+1',
                                    source_name=perm.name,
                                )
                                _add = _rep.process_event_sync(_ev).amount
                            except (ImportError, ValueError, KeyError, AttributeError, TypeError) as _rep_err:
                                print(f"[LANDFALL] Avenger replacement check failed: {_rep_err}")
                        c.counters['+1/+1'] = c.counters.get('+1/+1', 0) + _add
                        _per_plant_total = _add
                game.recalculate_power_toughness()
                _each = f"{_per_plant_total} +1/+1 counter(s)" if _per_plant_total != 1 else "a +1/+1 counter"
                messages.append(f"🌱 Avenger of Zendikar: {plant_count} Plant(s) each get {_each}!")
                print(f"[LANDFALL] Avenger of Zendikar: +{_per_plant_total} counter on {plant_count} Plants")
        
        # Rampaging Baloths: create a 4/4 green Beast creature token.
        # The loose "landfall + create + beast" fallback used to live here and
        # claimed FELIDAR RETREAT, whose token is a 2/2 white Cat BEAST — the
        # substring matched on "Cat Beast". Because this chain is elif, that
        # also made the Tier 1.5 lookup further down unreachable for it, so the
        # modal +1/+1-counter half was never even offered. Eight wrong tokens
        # across the loose logs. Require the printed token instead of guessing.
        elif "rampaging baloths" in perm.name.lower() or (
            "landfall" in perm_oracle and "4/4 green beast" in perm_oracle
        ):
            beast = Card(
                name="Beast",
                mana_cost="",
                type_line="Creature Token — Beast",
                oracle_text="",
                power="4",
                toughness="4",
            )
            beast.is_token = True
            beast.summoning_sick = True
            beast.entered_this_turn = True
            beast.colors = ['G']
            player.battlefield.append(beast)
            messages.append(f"🦬 {perm.name}: Landfall! Created a 4/4 green Beast creature token.")

        # Omnath, Locus of Creation: incremental landfall (1st: gain 4 life, 2nd: add WURG, 3rd: deal 4 to opponents + PWs)
        # Tier 1 handler prevents Tier 3 Claude from hallucinating damage to Omnath itself
        # Bug fix: Omnath's "this is the Nth landfall trigger" counter must be
        # per-Omnath, NOT total lands played. If Omnath enters AFTER the player
        # has already played a land this turn, the next landfall is Omnath's
        # FIRST observed trigger (gain 4 life), not the third (deal 4 damage).
        elif "omnath, locus of creation" in perm.name.lower():
            # Reset per-turn counter when turn changes
            if getattr(perm, '_omnath_turn_seen', -1) != game.turn_number:
                perm._omnath_landfall_count = 0
                perm._omnath_turn_seen = game.turn_number
            perm._omnath_landfall_count = (getattr(perm, '_omnath_landfall_count', 0) or 0) + 1
            lf = perm._omnath_landfall_count
            if lf == 1:
                player.life += 4
                messages.append(f"🌊 {perm.name}: Landfall #1! {player.name} gains 4 life. (life: {player.life})")
                print(f"[LANDFALL-OMNATH] #{lf}: {player.name} gains 4 life")
                # May 23 audit (MAJOR #14): emit symmetric [LIFE-GAIN] tag so
                # post-batch ledger reconciliation scripts can detect the gain.
                print(f"[LIFE-GAIN] {player.name} gains 4 life from {perm.name} (Omnath landfall #1)")
            elif lf == 2:
                for color in ['W', 'U', 'R', 'G']:
                    player.mana_pool[color] = player.mana_pool.get(color, 0) + 1
                messages.append(f"🌊 {perm.name}: Landfall #2! Add {{W}}{{U}}{{R}}{{G}}.")
                print(f"[LANDFALL-OMNATH] #{lf}: {player.name} adds WURG")
            elif lf == 3:
                # Deal 4 damage to each opponent and each planeswalker you don't control
                for opp_idx, opp in enumerate(game.players):
                    if opp_idx != player_idx:
                        opp.life -= 4
                        opp.record_life_loss(4)
                        messages.append(f"🌊 {perm.name}: Landfall #3! Deals 4 damage to {opp.name}. (life: {max(0, opp.life)})")
                        # May 23 audit (MAJOR #14): emit symmetric [SPELL-DAMAGE]
                        # tag for ledger reconciliation.
                        print(f"[SPELL-DAMAGE] {perm.name} (Omnath landfall #3) deals 4 to {opp.name}")
                        # Also damage planeswalkers the opponent controls
                        for opp_perm in opp.battlefield:
                            if opp_perm.is_planeswalker():
                                opp_perm.loyalty_counters = max(0, getattr(opp_perm, 'loyalty_counters', 0) - 4)
                                messages.append(f"🌊 {perm.name}: Deals 4 damage to {opp_perm.name}. (loyalty: {opp_perm.loyalty_counters})")
                print(f"[LANDFALL-OMNATH] #{lf}: {player.name} deals 4 to each opponent + enemy PWs")
            # 4th+ landfall: Omnath has no additional triggered abilities
            # (but we still handled it so it doesn't fall through to generic/Tier 3)

        # Courser of Kruphix: gain 1 life
        elif "courser of kruphix" in perm.name.lower() or (
            "landfall" in perm_oracle and "gain 1 life" in perm_oracle and "whenever a land" in perm_oracle
        ):
            player.life += 1
            messages.append(f"🌿 {perm.name}: Landfall! {player.name} gains 1 life. (life: {player.life})")
            print(f"[LANDFALL] {perm.name}: {player.name} gains 1 life")

        # Tireless Provisioner: create a Treasure token
        elif "tireless provisioner" in perm.name.lower() or (
            "landfall" in perm_oracle and "create a treasure" in perm_oracle
        ):
            treasure = Card(
                name="Treasure",
                type_line="Token Artifact — Treasure",
                oracle_text="Sacrifice this artifact: Add one mana of any color.",
                power="0", toughness="0",
            )
            treasure.is_token = True
            treasure.entered_this_turn = True
            player.battlefield.append(treasure)
            messages.append(f"💎 {perm.name}: Landfall! Created a Treasure token.")
            print(f"[LANDFALL] {perm.name}: created Treasure token")

        # Moraug, Fury of Akoum: landfall → additional combat phase + untap creatures
        elif "moraug" in perm.name.lower() and "additional combat" in perm_oracle:
            if not hasattr(game, '_additional_combats'):
                game._additional_combats = 0
            game._additional_combats += 1
            # Moraug also untaps each creature you control (they get +X/+0)
            untapped_count = 0
            for c in player.battlefield:
                if c.is_creature() and c.tapped:
                    c.tapped = False
                    untapped_count += 1
            messages.append(f"⚔️ {perm.name}: Landfall! Additional combat phase #{game._additional_combats} this turn. Untapped {untapped_count} creatures.")
            print(f"[LANDFALL] {perm.name}: additional combat #{game._additional_combats}, untapped {untapped_count} creatures")

        # Roil Elemental: landfall → gain control of target creature
        elif "roil elemental" in perm.name.lower() or (
            "landfall" in perm_oracle and "gain control" in perm_oracle
        ):
            # Steal best opponent creature
            opp_idx = 1 - player_idx
            opp = game.players[opp_idx]
            best_target = None
            best_power = -1
            for c in opp.battlefield:
                if c.is_creature() and not getattr(c, '_phased_out', False):
                    try:
                        p = c.get_effective_power(game) if hasattr(c, 'get_effective_power') else (int(c.power) if c.power else 0)
                    except (ValueError, TypeError):
                        p = 0
                    if p > best_power:
                        best_power = p
                        best_target = c
            if best_target:
                game.unregister_static_effects(best_target)
                opp.battlefield.remove(best_target)
                player.battlefield.append(best_target)
                # Re-register under new controller
                game.register_static_keyword_grants(best_target, player.name)
                game.register_static_pt_effects(best_target, player.name)
                game.register_replacement_effects(best_target, player.name)
                messages.append(f"🌊 {perm.name}: Landfall! Gains control of {best_target.name}!")
                print(f"[LANDFALL] {perm.name}: stole {best_target.name}")
            else:
                messages.append(f"🌊 {perm.name}: Landfall triggers (no creature to steal)")

        # Phylath, World Sculptor: landfall → +1/+1 on Plants (same as Avenger)
        elif "phylath" in perm.name.lower() and "plant" in perm_oracle:
            plant_count = sum(1 for c in player.battlefield
                            if c.type_line and "plant" in c.type_line.lower())
            if plant_count > 0:
                for c in player.battlefield:
                    if c.type_line and "plant" in c.type_line.lower():
                        c.counters['+1/+1'] = c.counters.get('+1/+1', 0) + 1
                messages.append(f"🌱 {perm.name}: Landfall! {plant_count} Plant(s) each get a +1/+1 counter!")
                print(f"[LANDFALL] {perm.name}: +1/+1 on {plant_count} Plants")

        # Scute Swarm: landfall → create token (copy if 6+ lands)
        elif "scute swarm" in perm.name.lower():
            land_count = sum(1 for c in player.battlefield if c.is_land())
            if land_count >= 6:
                # Create a copy of Scute Swarm
                scute_copy = Card(
                    name="Scute Swarm",
                    mana_cost="{2}{G}",
                    type_line="Creature — Insect",
                    oracle_text=perm.oracle_text or "Landfall — Whenever a land enters the battlefield under your control, create a 1/1 green Insect creature token. If you control six or more lands, create a token that's a copy of Scute Swarm instead.",
                    power="1", toughness="1",
                )
                scute_copy.is_token = True
                scute_copy.summoning_sick = True
                scute_copy.entered_this_turn = True
                scute_copy.colors = ['G']
                player.battlefield.append(scute_copy)
                messages.append(f"🐛 {perm.name}: Landfall (6+ lands)! Created a copy of Scute Swarm!")
            else:
                insect = Card(
                    name="Insect",
                    type_line="Token Creature — Insect",
                    power="1", toughness="1",
                )
                insect.is_token = True
                insect.summoning_sick = True
                insect.entered_this_turn = True
                insect.colors = ['G']
                player.battlefield.append(insect)
                messages.append(f"🐛 {perm.name}: Landfall! Created a 1/1 green Insect token.")
            print(f"[LANDFALL] {perm.name}: token created (lands={land_count})")

        # Generic landfall detection
        elif "whenever a land enters" in perm_oracle or "landfall" in perm_oracle:
            # Skip triggers that only work from the graveyard (e.g. Bloodghast)
            # "you may return X from your graveyard" should NOT fire from battlefield
            if "from your graveyard" in perm_oracle:
                continue

            # May 7 audit (Bug 2): try the Tier 1.5 template library before
            # falling back to the !judge prompt. Maja, Bretagard Protector
            # and similar landfall payoffs have card-name-keyed templates
            # that already produce the right token-create + pump actions,
            # but were silently bypassed because this branch went straight
            # to the !judge text. Template covers any landfall card added
            # to effect_templates._card_templates.
            template_msgs = []
            if HAS_EFFECT_TEMPLATES:
                try:
                    lib = get_effect_library()
                    ctx = build_game_context(game, player, opponent, card=perm,
                                             entering_creature=card)
                    landfall_actions, landfall_desc = lib.resolve_etb(
                        card_name=perm.name,
                        oracle_text=perm.oracle_text or "",
                        controller=player.name,
                        opponent=opponent.name,
                        game_context=ctx,
                    )
                    if landfall_actions:
                        for act in landfall_actions:
                            if act.get('action') == 'no_action':
                                continue
                            try:
                                msg = engine.rules._execute_action_on_state(game, act)
                                if msg:
                                    template_msgs.append(msg)
                            except Exception as e:
                                print(f"[LANDFALL-TEMPLATE] Action failed for {perm.name}: {act} — {e}")
                        if template_msgs:
                            print(f"[LANDFALL-TEMPLATE] Resolved {perm.name} via template: {landfall_desc}")
                            # Prepend a single-line announcement of the trigger
                            # so the Discord output remains readable.
                            messages.append(f"🌍 {perm.name}: Landfall! {landfall_desc}")
                            messages.extend(template_msgs)
                            continue  # skip the !judge fallback for this perm
                except Exception as e:
                    print(f"[LANDFALL-TEMPLATE] Error for {perm.name}: {e}")

            # Extract the trigger text for the generic !judge fallback
            for line in perm.oracle_text.split('\n'):
                line_lower = line.lower()
                if "land enters" in line_lower or "landfall" in line_lower:
                    messages.append(
                        f"🌍 {perm.name} landfall trigger: *{line.strip()[:200]}{'...' if len(line.strip()) > 200 else ''}*\n"
                        f"  (Use `!judge` to resolve if needed.)"
                    )
                    break

    # === LANDFALL TRIGGERS FROM GRAVEYARD (Bloodghast family) ===
    # May 20 audit: Bloodghast's landfall reads "you may return Bloodghast from
    # your graveyard to the battlefield" — the trigger source IS the card in
    # the graveyard, but the battlefield-iterator above never scanned graveyards.
    # game_1506202586036830232 had Bloodghast in Claude's graveyard from turn 27
    # and ~9 subsequent land plays produced zero return triggers.
    for gy_card in list(player.graveyard):
        if not gy_card.oracle_text:
            continue
        gy_oracle = gy_card.oracle_text.lower()
        if ('landfall' not in gy_oracle and 'whenever a land enters' not in gy_oracle):
            continue
        if 'from your graveyard' not in gy_oracle:
            continue
        # Bloodghast (and lookalikes): return from graveyard to battlefield.
        # The "may" wording is taken as "yes" in autoplay — the autoplay AI
        # rarely wants to decline a free return of a recurring threat.
        if 'bloodghast' in gy_card.name.lower() or (
            'return' in gy_oracle and 'to the battlefield' in gy_oracle
        ):
            game.unregister_static_effects(gy_card)  # No-op if not registered
            player.graveyard.remove(gy_card)
            # CR 400.7: a card returning from the graveyard is a NEW object.
            # Without this, damage_marked survived the trip and a 2/1 Bloodghast
            # that died to 1 damage came back with that damage still on it and
            # re-died to the very next SBA check — plus stale counters,
            # attachments, attacking/blocking flags and pump modifiers. Every
            # other re-entry path already calls this; the landfall recursion
            # was the one that didn't. Must precede the assignments below:
            # reset sets summoning_sick=True / entered_this_turn=False.
            gy_card.reset_battlefield_state()
            gy_card.tapped = False
            gy_card.summoning_sick = False  # Bloodghast has haste implicitly via reanimation
            gy_card.entered_this_turn = True
            player.battlefield.append(gy_card)
            try:
                game.register_static_keyword_grants(gy_card, player.name)
                game.register_static_pt_effects(gy_card, player.name)
                game.register_replacement_effects(gy_card, player.name)
            except Exception:
                pass
            messages.append(f"🩸 {gy_card.name}: Landfall! Returns from graveyard to battlefield.")
            print(f"[LANDFALL-RECUR] {gy_card.name}: returned from {player.name}'s graveyard")

    # Dedup: identical messages can arise when multiple copies of the same
    # landfall permanent are on the battlefield, OR when a specific-card handler
    # and the generic fallthrough both fire for the same permanent (shouldn't
    # happen with elif chain, but template library may also queue resolves).
    seen = set()
    deduped = []
    for m in messages:
        if m not in seen:
            seen.add(m)
            deduped.append(m)
    return deduped


def process_attack_triggers(engine, game: GameState, attacking_player_idx: int) -> List[str]:
    """Process triggers that fire when a player attacks. Returns messages."""
    messages = []
    num_attackers = len(game.attackers)
    attacking_player = game.players[attacking_player_idx]

    # Process turn_effects with type "on_attack_damage"
    for effect in game.turn_effects:
        if effect.get("type") == "on_attack_damage" and effect.get("controller") == attacking_player_idx:
            target_id = effect.get("target_id")
            target_name = effect.get("target_name", "target")
            source = effect.get("source", "Effect")
            calc = effect.get("calc", "")

            # Calculate damage
            damage = 0
            if calc == "num_attackers":
                damage = num_attackers

            # Find and damage the target
            if target_id:
                for player in game.players:
                    for card in player.battlefield:
                        if card.id == target_id:
                            card.damage_marked += damage
                            messages.append(f"🔥 {source} deals {damage} damage to {card.name}!")
                            break
            elif target_name and damage > 0:
                messages.append(f"🔥 {source} triggers for {damage} damage to {target_name}!")

    # [ATTACK-TRIGGER] Fire attack triggers for each attacker
    all_attack_trigger_infos = []
    for attacker_id in game.attackers:
        attacker_card = None
        for card in attacking_player.battlefield:
            if card.id == attacker_id:
                attacker_card = card
                break
        if attacker_card:
            try:
                trigger_msgs, unhandled = engine._check_attack_triggers_sync(game, attacker_card, attacking_player)
                messages.extend(trigger_msgs)
                if game.triggers_use_stack and game.stack_enabled:
                    # Stack mode: collect unhandled for APNAP stack placement
                    for trigger_card, trigger_text in unhandled:
                        ctrl_player = attacking_player
                        for p in game.players:
                            if trigger_card in p.battlefield:
                                ctrl_player = p
                                break
                        all_attack_trigger_infos.append((trigger_card, ctrl_player, trigger_text))
                else:
                    # Sync context: queue unhandled attack triggers for async Tier 3 drain.
                    for trigger_card, trigger_text in unhandled:
                        ctrl_player = attacking_player
                        for p in game.players:
                            if trigger_card in p.battlefield:
                                ctrl_player = p
                                break
                        engine._queue_async_trigger(
                            game, trigger_card, trigger_text, "attack",
                            ctrl_player.name,
                            context=f"{attacker_card.name} was declared as an attacker",
                        )
                        messages.append(
                            format_trigger_line("⚔️", trigger_card.name, trigger_text, game=game, max_chars=300)
                        )
            except Exception as e:
                print(f"[ATTACK-TRIGGER] Error processing attack triggers for {attacker_card.name}: {e}")

    # Stack mode: place all unhandled attack triggers on the stack with APNAP ordering
    if all_attack_trigger_infos:
        stack_msgs = engine._place_triggers_on_stack(game, all_attack_trigger_infos, "attacks")
        messages.extend(stack_msgs)

    return messages


# =============================================================================
# Pub/sub slice 2 (July 20, 2026): PERMANENT_ENTERED subscribers
# =============================================================================
# Emit sites (mtg/spells.py cast + suspend, mtg/engine.py play_land +
# cast_spell, mtg/actions.py noncast funnel, mtg/sba.py death-save returns)
# fire events.PERMANENT_ENTERED once per physical battlefield entry. The
# legacy creature-enters / enchantment-enters scans stay authoritative this
# batch; the parity recorder below cross-checks them so one clean batch can
# gate flipping the scans into subscribers (slice 2b — see mtg/events.py).


def _snow_permanent_entered_watcher(game, card=None, controller=None,
                                    via=None, rules=None, **_):
    """Marit Lage's Slumber: "Whenever Marit Lage's Slumber or another snow
    permanent you control enters, scry 1."

    The June 10 deep-dive fixed Slumber's upkeep flip + its own-entry scry
    (ETB pattern) but deferred the OTHER-snow-permanent half to this slice —
    there was no snow-permanent-enters watcher class to hang it on. Now
    there is: this fires on every PERMANENT_ENTERED whose card is snow,
    for each Slumber its controller already has on the battlefield.
    Display rides game._pending_messages (flushed by the existing engine
    flush sites); the scried card is never named in Discord (scry is
    private information — console may log it).
    """
    if card is None or controller is None:
        return
    if 'Snow' not in (getattr(card, 'type_line', '') or ''):
        return
    for watcher in list(controller.battlefield):
        if watcher is card or getattr(watcher, '_phased_out', False):
            continue
        if not names_match(watcher.name, "Marit Lage's Slumber"):
            continue
        # Slumber's own entry is handled by its ETB pattern at cast time —
        # this watcher only covers OTHER snow permanents entering.
        if rules is not None and hasattr(rules, '_execute_action_on_state'):
            msg = rules._execute_action_on_state(game, {
                "action": "scry", "player": controller.name, "amount": 1,
            })
            print(f"[SNOW-WATCHER] Marit Lage's Slumber: {controller.name} "
                  f"scries 1 ({card.name} entered, via={via})")
            if msg:
                if not hasattr(game, '_pending_messages') or game._pending_messages is None:
                    game._pending_messages = []
                game._pending_messages.append(
                    f"❄️ **Marit Lage's Slumber** — {msg.lstrip('🔮 ')}")
        else:
            print(f"[SNOW-WATCHER] Marit Lage's Slumber trigger for "
                  f"{controller.name} ({card.name} entered) — no rules "
                  f"engine in payload, scry skipped")


def queue_death(game, card, player) -> None:
    """Slice 3b (July 23, 2026): the single choke-point for queueing a death is
    now the CREATURE_DIED emit.

    The physical `_recently_died` append moved into
    `_accumulate_death_subscriber` (the bus subscriber below), so the legacy
    dies queue is now BUS-FED: emit -> subscriber -> _recently_died, all
    synchronous (events.emit dispatches in registration order, in-line), so
    every caller still sees _recently_died populated the instant queue_death
    returns — behavior-identical to the 3a direct append. The dies dispatcher,
    wave semantics (_active_dies_batch), APNAP ordering
    (helpers.apnap_order_died), and _dies_source_ids_by_dead_id (populated at
    the call sites) are all UNCHANGED — only the queue's INPUT flipped from a
    direct append here to the subscriber.
    """
    events.emit(events.CREATURE_DIED, game, card=card, player=player)


def queue_deaths(game, pairs) -> None:
    """Batch form of queue_death (board wipes, SBA sweeps)."""
    for card, player in (pairs or []):
        queue_death(game, card, player)


def _accumulate_death_subscriber(game, card=None, player=None, **_):
    """Slice 3b (July 23, 2026): the CREATURE_DIED subscriber that feeds the
    legacy dies queue.

    This is now the ONE sanctioned `_recently_died` appender (structural test
    `test_no_raw_queue_mutations_outside_the_choke_point` enforces it). It only
    ACCUMULATES — it never resolves triggers inline — because the dies consumer
    has batch-level semantics a per-event subscriber can't carry:
      - wave separation (_active_dies_batch): the dispatcher resets
        _recently_died to [] before draining a wave, so deaths caused by that
        wave's triggers land in a FRESH _recently_died via THIS subscriber and
        become the next wave (CR — a source that died in wave 1 doesn't see
        wave-2 deaths). Firing on emit would destroy that separation.
      - APNAP ordering (helpers.apnap_order_died) sorts a whole batch (CR
        603.3b, NAP first) — a batch operation, impossible per-event.
    So the explicit drain (engine.py / actions.py / models.py) still owns
    resolution; this subscriber just makes the bus the source of truth for
    "a creature died." _recently_died is a declared field (default []), so no
    init guard is needed.
    """
    if card is None:
        return
    game._recently_died.append((card, player))
    # July 29 batch audit: the Meren/Ezuri experience-counter bump lived only
    # in process_state_based_actions' LOCAL death list, so the whole
    # sacrifice-as-cost death class (Viscera Seer, Altar of Dementia,
    # Phyrexian Tower) never granted XP — every Meren end-step return went to
    # hand all game (game_1531564156203827213). The bus is the one choke-point
    # EVERY death path reaches (slice 3b), and at emit time the granter is
    # still on the battlefield, which is closer to the CR 603.6e pre-event
    # state the post-batch SBA sweep got wrong for simultaneous deaths.
    try:
        if player is not None and card.is_creature():
            for perm in getattr(player, 'battlefield', None) or []:
                if perm is card:
                    continue
                otext = (getattr(perm, 'oracle_text', '') or '').lower()
                if ('experience counter' in otext
                        and 'another creature you control dies' in otext):
                    prev = int(getattr(player, '_experience_counters', 0) or 0)
                    player._experience_counters = prev + 1
                    print(f"[EXPERIENCE] {player.name} gains an experience counter "
                          f"({prev} → {prev + 1}) from {card.name} dying ({perm.name})")
                    break
    except (AttributeError, TypeError, ValueError) as e:
        print(f"[EXPERIENCE] increment failed: {e}")
        maybe_reraise(e)


def fire_counters_a_spell_triggers(game, controller_name):
    """July 29 batch audit: "Whenever a spell or ability you control counters
    a spell" had NO event scan anywhere — Baral, Chief of Compliance's loot
    half was a structural no-op across three real counter events in
    game_1531555430847873116 (the Tymna/Kroxa/Baral commander-half-card
    family). Called from the sites that mark a stack entry countered
    (mtg/actions.py counter handlers + rules/spell_resolver.py).

    Models the Baral-class loot: draw a card, then discard the highest-CMC
    card ("you may draw" taken whenever the library isn't empty).
    Returns display messages.
    """
    msgs = []
    if not controller_name:
        return msgs
    player = next((p for p in game.players
                   if p.name == controller_name), None)
    if player is None:
        return msgs
    for perm in list(player.battlefield):
        ot = (getattr(perm, 'oracle_text', '') or '').lower()
        if 'counters a spell' in ot and 'draw a card' in ot:
            if not player.library:
                continue
            drawn = player.library.pop(0)
            player.hand.append(drawn)
            line = f"🃏 **{perm.name}**: {player.name} draws a card"
            if player.hand:
                # Aug 1 (madness, CR 702.35): prefer + redirect.
                from mtg.helpers import (madness_discard_to_exile,
                                         parse_madness_cost)
                _mad_opts = [c for c in player.hand
                             if parse_madness_cost(c.oracle_text or '')]
                discard = (_mad_opts[0] if _mad_opts
                           else max(player.hand, key=lambda c: (c.cmc or 0)))
                player.hand.remove(discard)
                _mm = madness_discard_to_exile(game, player, discard)
                if _mm:
                    msgs.append(_mm)
                else:
                    player.graveyard.append(discard)
                line += f", then discards **{discard.name}**"
            msgs.append(line)
            print(f"[COUNTER-TRIGGER] {perm.name} loots for {player.name} "
                  f"(a spell they control countered a spell)")
    return msgs


def queue_cast_triggers_sync(engine, game, caster, card, via: str = "sync") -> int:
    """Sync-context cast-trigger bridge (July 24, 2026 — slice 4b groundwork).

    Suspend resolution, Etali free casts, template "cast … for free" moves,
    and the legacy sync cast are real CR 601 casts (a suspended Rift Bolt
    should make Talrand a Drake), but they run in SYNC contexts that can't
    await _check_cast_triggers — the documented sync-trigger-gap class, so
    battlefield cast triggers never fired for them at all. Scan both
    battlefields for "whenever you cast / whenever a player casts" sources
    whose spell-filter matches this cast and queue each for the async Tier-3
    drain (engine._queue_async_trigger — the same mechanism the unhandled
    cast-trigger tail uses). Prowess and self-cast ("when you cast this
    spell") triggers remain out of scope here — they need inline resolution
    semantics the queue can't carry.

    Also emits CARD_CAST and records the cast in the slice-4a parity ledger,
    so these sites participate in the shadow without breaking the zero gate.
    Returns the number of triggers queued.
    """
    events.emit(events.CARD_CAST, game, card=card, caster=caster,
                via=via, engine=engine)
    if engine is None or not hasattr(engine, '_queue_async_trigger'):
        return 0
    queued = 0
    for p in game.players:
        for bf_card in list(p.battlefield):
            if getattr(bf_card, '_phased_out', False):
                continue
            oracle = (bf_card.oracle_text or '')
            ol = oracle.lower()
            if 'whenever you cast' not in ol and 'whenever a player casts' not in ol:
                continue
            for sentence in oracle.replace('\n', '.').split('.'):
                sl = sentence.lower().strip()
                if not sl:
                    continue
                if p is caster:
                    if ('whenever you cast' not in sl
                            and 'whenever a player casts' not in sl):
                        continue
                else:
                    # An opponent's permanent only sees this cast through
                    # the any-player phrasing.
                    if 'whenever a player casts' not in sl:
                        continue
                # Skip activated-ability cost lines ("{2}: ...").
                if re.match(r'^[+−\-]?\d+\s*:', sentence.strip()):
                    continue
                try:
                    if not engine._spell_matches_cast_trigger(sl, card, caster, game):
                        continue
                except (AttributeError, TypeError):
                    continue
                engine._queue_async_trigger(
                    game, bf_card, sentence.strip(), "cast_trigger", p.name,
                    context=f"{caster.name} cast {card.name} (via {via})")
                queued += 1
                print(f"[CAST-TRIGGER-SYNC] {bf_card.name} queued for Tier 3 "
                      f"({caster.name} cast {card.name} via {via})")
    return queued


events.subscribe(events.PERMANENT_ENTERED, _snow_permanent_entered_watcher)
# Slice 2b (July 21, 2026): the creature-enters watcher scan is DRIVEN BY
# the bus. Slice 2c (July 24, 2026): the parity recorder that shadowed the
# migration was retired after two clean batches ([EVENT-PARITY]=0 in 15296
# and 15299) — [ETB-BUS] remains the emit-side net (a subscriber that gets
# an unusable engine ref logs it), and tests/test_slice2b_bus_dispatch.py
# pins the end-to-end dispatch.
events.subscribe(events.PERMANENT_ENTERED, _creature_entered_subscriber)
events.subscribe(events.PERMANENT_ENTERED, _enchantment_entered_subscriber)
# Slice 3b (July 23, 2026): the dies queue is bus-fed — the accumulator
# subscriber is the sole sanctioned _recently_died appender. Slice 3c
# (July 24, 2026): the death parity recorder retired after the post-3b
# batch (game_15299*) returned [EVENT-PARITY-DIES]=0; the structural pin in
# tests/test_slice3a_death_shadow.py (no raw _recently_died mutations
# outside the accumulator) remains as the permanent net.
events.subscribe(events.CREATURE_DIED, _accumulate_death_subscriber)
# Slice 4b (July 26, 2026): the CARD_CAST parity recorder is RETIRED after
# game_15304* returned [EVENT-PARITY-CAST]=0 on post-4a code. The consumer
# deliberately did NOT move onto the bus: _check_cast_triggers is async and
# needs `await` for Tier-3 resolve_effect, the cascade free-cast, its own
# recursion, and — decisively — engine._combat_priority_round, which is the
# [CAST-TRIGGER-PRIORITY] window that lets a Stifle counter a cast trigger
# (19 fires in that batch). The bus contract is sync handlers only, so
# subscribing a queuer would demote every inline cast trigger (Talrand
# tokens, prowess, that whole counter-a-trigger interaction) to a Tier-3
# drain — a real behaviour downgrade in exchange for uniformity.
#
# The migration's actual goals are met without the flip: CARD_CAST is
# emitted at EVERY cast path (both async funnels + the sync bridge), which
# is the "one spine, no missed call sites" property, and the parity gate
# proved it. Consumption differs by path — the async funnels consume
# directly (they ARE the funnel), the sync sites consume via
# queue_cast_triggers_sync. Emission is pinned by
# tests/test_slice4a_cast_shadow.py so the spine can't silently rot with no
# subscriber watching it; the React websocket layer is the intended next
# subscriber.


# ---------------------------------------------------------------------------
# Pub/sub slice 5b (July 31, 2026): COMBAT_DAMAGE_DEALT is BUS-FED into the
# combat-damage trigger queues. The subscriber below is the SOLE sanctioned
# appender for game._combat_damage_to_player and ._combat_damage_to_creature
# (the slice-3b pattern): it only ACCUMULATES — the drain in
# mtg/combat.py's resolve_combat_damage keeps the batch semantics (per-step
# waves and the `not game.ended` gate, CR 104.2a). Player-kind entries feed
# the battlefield-watcher + attacker self-trigger dispatch; creature-kind
# entries feed the damaged-creature scan (Phyrexian Obliterator class).
# The 5a parity recorder was retired at this flip — its gate cleared at
# ZERO mismatches over 134 FS-step combats and 2,496 player-damage events
# (batch 15324). [EVENT-PARITY-CDD] is now a stale-code tripwire like its
# 2c/3c/4b siblings.
# ---------------------------------------------------------------------------

def _accumulate_combat_damage_subscriber(game, source=None, target=None,
                                         amount=0, target_kind="", **_payload):
    """Accumulate combat-damage events for the resolve_combat_damage drain."""
    if amount <= 0 or source is None:
        return
    if target_kind == "player":
        src_owner = next(
            (p for p in game.players
             if any(c.id == getattr(source, 'id', None)
                    for c in p.battlefield)),
            None)
        if src_owner is None:
            # Dealer already left the battlefield (FS trades). In 2-player
            # combat the dealer's controller is the non-target player.
            src_owner = next((p for p in game.players if p is not target), None)
        if src_owner is None:
            return
        game._combat_damage_to_player.append((source, src_owner, amount))
    elif target_kind == "creature":
        game._combat_damage_to_creature.append((source, target, amount))


events.subscribe(events.COMBAT_DAMAGE_DEALT, _accumulate_combat_damage_subscriber)


# ---------------------------------------------------------------------------
# Pub/sub slice 6b (July 31, 2026 — the FLIP; gate cleared on batch 15325 at
# [EVENT-PARITY-PHASE]=0): the MAIN-phase trigger dispatch is now a
# PHASE_CHANGED subscriber. EVERY entry into MAIN1/MAIN2 — advance_phase's
# walk AND all seven combat-path direct sets — runs the dispatch with no
# caller cooperation needed, which permanently retires the class that bit
# three times (the Tymna family: a set_phase caller forgetting the
# dispatch). Messages buffer into game._pending_messages; the old call
# sites drain at their exact old positions so Discord ordering is
# unchanged (the slice-2b convention).
#
# Scoping decision (the 4b proportionality precedent): the UPKEEP scan
# deliberately does NOT flip. UPKEEP has exactly ONE entry path —
# advance_phase's PHASE_ORDER walk, where the scan is unconditional inside
# a strictly ordered sequence (day/night transforms → delayed triggers →
# Solitary Confinement → suspend → the scan → stack/queue routing) — so
# its hook structurally cannot be orphaned, and flipping it would reorder
# that sequence for uniformity's sake. Revisit only if a second UPKEEP
# entry path ever appears (the structural no-raw-phase-assignment pin
# would catch the attempt first).
#
# Per-entry semantics are CR-correct: "at the beginning of each of your
# postcombat main phases" (Tymna) fires at EACH entry, so Moraug-style
# extra main phases legitimately re-dispatch. via='game_start' is exempt
# (empty battlefield); ended games skip per CR 104.2a.
# ---------------------------------------------------------------------------


def _main_phase_bus_subscriber(game, old_phase=None, new_phase=None, via="",
                               **_payload):
    """Run the MAIN-phase trigger dispatch on every MAIN1/MAIN2 entry."""
    name = getattr(new_phase, 'name', str(new_phase))
    if name not in ("MAIN1", "MAIN2"):
        return
    if via == "game_start" or getattr(game, 'ended', False):
        return
    rules = getattr(game, '_rules_engine', None)
    engine = getattr(rules, 'engine_ref', None) if rules is not None else None
    if engine is None or not hasattr(engine, 'dispatch_main_phase_triggers'):
        # The [ETB-BUS] convention: a MAIN entry the bus can't dispatch is a
        # wiring gap, not a silent skip.
        print(f"[PHASE-BUS] {name} entry (via={via or '?'}) with no usable "
              f"engine ref — main-phase dispatch skipped")
        return
    msgs = engine.dispatch_main_phase_triggers(game, name == "MAIN1")
    if msgs:
        game._pending_messages.extend(msgs)


events.subscribe(events.PHASE_CHANGED, _main_phase_bus_subscriber)
