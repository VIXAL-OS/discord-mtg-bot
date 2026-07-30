"""Spell casting + resolution flow + suspend + sagas + XMage serialization.

Six free functions extracted from GameEngine. The core is `cast_spell_async`
— the main entry point that routes a spell from "in player's hand" through
mana payment, stack placement, replacement effects, target validation,
trigger ordering, and final resolution.

Public free functions (each takes a GameEngine instance as first arg):

    cast_spell_async(engine, ...)              (async)
        The main spell-casting orchestrator. ~1400 lines.

    resolve_special_effects(engine, ...)
        Tier 1 hardcoded ETB / spell handlers (~15 specific cards: Terror
        of the Peaks, Warstorm Surge, Soul of the Harvest, etc.). Free,
        instant, 100% reliable.

    _resolve_suspend_spell(engine, ...)
        When a suspended spell's time counters reach zero, this casts it
        from exile.

    _process_suspend_upkeep(engine, ...)
        Tick the time counter on each suspended spell at upkeep.

    _advance_sagas(engine, ...)
        Add a lore counter to each saga at the start of the controller's
        first main phase. SBA fires the chapter abilities.

    _serialize_for_xmage(engine, ...)
        Snapshot the game state into the JSON shape the XMage Java bridge
        expects for trigger discovery.

State touched on `engine`:

    engine.rules                — RulesEngine instance
    engine.spell_resolver       — Tier 2 SpellResolver
    engine.claude_ai            — anthropic client wrapper
    engine.xmage_bridge         — XMage Java bridge handle
    engine._xmage_translator    — XMage action translator
    engine._cached_xmage_*      — XMage state snapshot cache
    engine.draw_cards           — for cantrip-style effects
    engine._handle_etb_triggers, engine._check_cast_triggers,
    engine._check_creature_etb_triggers,
    engine._check_creature_etb_triggers_sync — Phase 2E delegators
    engine._activate_day_night_if_needed,
    engine._flush_pending_messages,
    engine._queue_async_trigger
    engine._get_saga_chapter_text,
    engine._get_saga_total_chapters

Extracted from mtg/engine.py during the Phase 2 OSS-readability refactor
(Phase 2G).
"""

import asyncio
import json
import random
import re
from typing import Any, Dict, List, Optional, Tuple

from mtg.constants import Phase, Zone, COMMAND_ZONE_FORMATS
from mtg import helpers
from mtg.helpers import _should_emit_resolve_hint
from mtg.models import Card, Player, GameState, StackEntry
from mtg import events

# Optional: Tier 2 spell resolver
try:
    from rules import SpellResolver, TargetMode, ExecutionContext
    HAS_SPELL_RESOLVER = True
except ImportError:
    HAS_SPELL_RESOLVER = False

# Optional: Tier 1.5 effect templates
try:
    from rules.effect_templates import get_effect_library, build_game_context
    HAS_EFFECT_TEMPLATES = True
except ImportError:
    HAS_EFFECT_TEMPLATES = False

# Optional: layers (granted abilities)
try:
    from rules.layers import Layer, create_pump_effect
    HAS_LAYERS_ENGINE = True
except ImportError:
    HAS_LAYERS_ENGINE = False

# Optional: replacement effects
try:
    from rules.replacement import GameEvent, EventType
    HAS_REPLACEMENT_ENGINE = True
except ImportError:
    HAS_REPLACEMENT_ENGINE = False

# Optional: targeting validation
try:
    from rules.targeting_helpers import (
        _validate_target_for_action,
        _validate_player_target_for_action,
        _find_any_valid_target,
        _spell_requires_targets,
        _check_resolution_targets,
    )
    HAS_TARGETING = True
except ImportError:
    HAS_TARGETING = False

# Optional: structured mana cost parser
try:
    from rules.mana import ManaCost
    HAS_MANA_ENGINE = True
except ImportError:
    HAS_MANA_ENGINE = False

# Optional: planeswalker abilities
try:
    from rules.planeswalker import PlaneswalkerManager
    HAS_PLANESWALKER = True
except ImportError:
    HAS_PLANESWALKER = False


def _clone_target_is_legal(source_card: Card, target_card: Card,
                           entering_player: Player,
                           target_controller: Player) -> bool:
    """Apply the copy-choice restriction printed on the clone card."""
    if source_card.name.lower() == "spark double":
        return (target_controller is entering_player
                and (target_card.is_creature() or target_card.is_planeswalker()))
    # Preserve the existing clone-family scope here; broader noncreature-copy
    # cards are handled by their dedicated template/action paths.
    return target_card.is_creature()


def _apply_clone_characteristics(card: Card, copy_target: Card) -> str:
    """Apply battlefield copy values and Spark Double's exceptions."""
    from mtg.actions import _snapshot_copy_source

    original_name = card.name
    _snapshot_copy_source(card)
    card.name = copy_target.name
    card.power = copy_target.power
    card.toughness = copy_target.toughness
    card.type_line = copy_target.type_line
    card.oracle_text = copy_target.oracle_text
    card.mana_cost = copy_target.mana_cost
    card.cmc = copy_target.cmc
    card.loyalty = copy_target.loyalty
    card.keywords = list(copy_target.keywords or [])
    card._is_copy = True
    card._copy_of = copy_target.name
    card._original_name = original_name

    if original_name.lower() == "spark double":
        # Spark Double's copy is explicitly nonlegendary. Removing the
        # supertype keeps both the inline and delegated legend-rule checks
        # from sacrificing a real permanent.
        card.type_line = re.sub(r'\blegendary\s*', '', card.type_line or '',
                                flags=re.IGNORECASE).strip()
        if card.is_creature():
            card.counters['+1/+1'] = card.counters.get('+1/+1', 0) + 1
        elif card.is_planeswalker():
            try:
                card.loyalty_counters = int(card.loyalty or 0) + 1
            except (TypeError, ValueError):
                card.loyalty_counters = 1
    elif card.is_planeswalker():
        try:
            card.loyalty_counters = int(card.loyalty or 0)
        except (TypeError, ValueError):
            card.loyalty_counters = 0
    return original_name


def _ghostly_flicker_targets(game: GameState, player: Player,
                             target: Any) -> Tuple[List[Card], str]:
    """Normalize and validate Ghostly Flicker's exactly-two target choice."""
    legal = [
        permanent for permanent in player.battlefield
        if not getattr(permanent, '_phased_out', False)
        and (permanent.is_creature() or permanent.is_land()
             or 'artifact' in (permanent.type_line or '').lower())
    ]
    if target is None:
        chosen = legal[:2]
    else:
        raw_targets = list(target) if isinstance(target, (list, tuple)) else [target]
        chosen = []
        for raw in raw_targets:
            name = getattr(raw, 'name', str(raw))
            match = next((card for card in legal
                          if card is raw or card.name.lower() == name.lower()), None)
            if match is None:
                return [], (f"Ghostly Flicker can't target {name} — choose "
                            "artifacts, creatures, or lands you control")
            chosen.append(match)
    if len(chosen) != 2 or chosen[0].id == chosen[1].id:
        return [], "Ghostly Flicker requires exactly two distinct legal targets (CR 601.2c)"
    return chosen, ""

# Optional: XMage bridge for trigger discovery (used by _serialize_for_xmage)
try:
    from rules.xmage_bridge import Permanent as XMagePermanent
    from rules.xmage_bridge import GameState as XMageGameState
    HAS_XMAGE_BRIDGE = True
except ImportError:
    HAS_XMAGE_BRIDGE = False


def _serialize_for_xmage(engine, game: GameState) -> Tuple['XMageGameState', Dict[str, str]]:
    """Convert engine GameState to XMage bridge format for subprocess calls.

    Returns:
        (xmage_state, player_name_map) where player_name_map maps
        real names ("playerA-name") to bridge identifiers ("playerA").
    """
    # Cache check — skip re-serializing when board unchanged
    current_fp = game._state_fingerprint()
    if (current_fp == engine._cached_xmage_fingerprint
            and engine._cached_xmage_state is not None):
        print(f"[XMAGE-CACHE] HIT (fp={current_fp})")
        return engine._cached_xmage_state, engine._cached_xmage_name_map

    xmage_state = XMageGameState()

    # Map player names to playerA/playerB
    player_name_map = {}
    for i, p in enumerate(game.players):
        key = f"player{'A' if i == 0 else 'B'}"
        player_name_map[p.name] = key
        xmage_state.player_life[key] = p.life
        xmage_state.poison_counters[key] = getattr(p, 'poison', 0)
        xmage_state.hands[key] = [c.name for c in p.hand]
        xmage_state.graveyards[key] = [c.name for c in p.graveyard]
        xmage_state.untapped_lands[key] = len(p.untapped_lands())

    xmage_state.active_player = player_name_map.get(
        game.active_player.name if game.active_player else game.players[0].name,
        "playerA"
    )
    xmage_state.phase = game.phase.value
    xmage_state.stack_size = len(game.stack)

    # Serialize battlefield permanents
    for player in game.players:
        player_key = player_name_map.get(player.name, "playerA")
        for card in player.battlefield:
            # Use get_effective_power/toughness for accurate P/T including layers
            eff_power = card.get_effective_power(game) if hasattr(card, 'get_effective_power') else 0
            eff_tough = card.get_effective_toughness(game) if hasattr(card, 'get_effective_toughness') else 0

            perm = XMagePermanent(
                name=card.name,
                controller=player_key,
                is_creature=card.is_creature(),
                is_legendary="legendary" in (card.type_line or "").lower(),
                power=eff_power,
                toughness=eff_tough,
                power_modifier=0,  # Already folded into effective values above
                toughness_modifier=0,
                plus_counters=card.counters.get('+1/+1', 0),
                minus_counters=card.counters.get('-1/-1', 0),
                damage_marked=getattr(card, 'damage_marked', 0),
                tapped=card.tapped,
                summoning_sick=getattr(card, 'summoning_sick', False),
                keywords=list(card.keywords) if card.keywords else [],
            )
            xmage_state.battlefield.append(perm)

    # Store in cache for next call
    engine._cached_xmage_state = xmage_state
    engine._cached_xmage_fingerprint = current_fp
    engine._cached_xmage_name_map = player_name_map
    print(f"[XMAGE-CACHE] MISS — rebuilt (fp={current_fp})")
    return xmage_state, player_name_map


def _validate_cast(engine, game: GameState, player: Player, card: Card,
                   target: Any) -> Tuple[Optional[Tuple[bool, str, List[str]]], bool, Any]:
    """CR 601 cast-legality gates (refactor #2 step 2 — extracted July 20, 2026).

    Gate order (behavior-preserving; each gate's rationale is inline):
      1. per-cast `_spell_resolved` reset
      2. zone membership — hand ∪ marked-graveyard runs FIRST (July 10 fix)
      3. rules gate (mana/timing via engine.rules.can_cast_spell)
      4. Ghostly Flicker two-target selection (mutates `target`)
      5. aura target scans — battlefield + "in a graveyard" variants (CR 601.2c)
      6. commander color identity (CR 903.4)
      7. counter-with-empty-stack gate (CR 601.2c, modal/creature carve-outs)
      8. targeting module — any-legal-target + declared-target validation

    Returns (rejection, cast_from_graveyard, target):
      rejection            — the (False, reason, []) tuple to return verbatim,
                             or None when every gate passes
      cast_from_graveyard  — flashback/escape zone flag consumed by the
                             payment + zone-move stages
      target               — possibly rewritten (Ghostly Flicker picks its
                             own two targets)
    """
    # Reset per-cast resolution flag. `card._spell_resolved` is set by Tier 1
    # / Tier 1.5 / Tier 3 handlers to prevent double-resolution within a
    # single cast — but the flag persisted on the Card instance after the
    # spell moved to graveyard. May 13 audit: Lingering Souls cast normally
    # → resolved → moved to graveyard → re-cast next turn → tier-1.5 template
    # skipped (because `_spell_resolved` still True from previous cast) →
    # spell logged "(no automatic state change)" with no tokens created.
    # Same risk for any spell that can be re-cast (flashback, escape,
    # disturb, jump-start, returned to hand). Clear at the start of every
    # cast so each cast gets a fresh template/SpellResolver run.
    if hasattr(card, '_spell_resolved'):
        card._spell_resolved = False

    # June 11 audit: flashback/escape casts arrive here with the card in the
    # GRAVEYARD (marked playable by the castable-list generator), but this
    # gate only accepted hand membership — "'Momentary Blink' not found in
    # hand" killed every flashback attempt in game 1514618481029677117.
    # July 10: zone membership is checked BEFORE the mana/timing rules gate —
    # a card the player doesn't hold must fail as "not in hand", not as a
    # misleading "Not enough black mana" (which sent the autoplay AI into
    # mana-fixing retries for cards that had already left the hand). All
    # non-hand cast paths (command zone, rebound) pre-move the card into
    # hand before calling this function, so hand ∪ marked-graveyard is the
    # complete castable set here.
    _cast_from_graveyard = (
        card not in player.hand
        and card in player.graveyard
        and card.id in (getattr(player, 'playable_from_graveyard', None) or [])
    )
    if card not in player.hand and not _cast_from_graveyard:
        return (False, "Card not in hand", []), False, target

    # Check rules
    can_cast, reason = engine.rules.can_cast_spell(game, player, card)
    if not can_cast:
        return (False, reason, []), _cast_from_graveyard, target

    # CR 601.2c: a spell that requires a target can't be cast unless at least
    # one legal target exists. May 17 audit: previously the engine would let
    # auras be cast with no legal target, charge mana, and then fizzle on
    # resolution. Spider Umbra in the May 16 batch showed this — mana wasted,
    # nothing happened, AI confused. Aura is the most common offender; the
    # broader "any spell that says target" check is too invasive to ship
    # today, so we narrow to auras here.
    _oracle_lower = (card.oracle_text or '').lower()
    _type_line_lower = (card.type_line or '').lower()
    if card.name.lower() == 'ghostly flicker':
        chosen_targets, target_error = _ghostly_flicker_targets(game, player, target)
        if target_error:
            return (False, target_error, []), _cast_from_graveyard, target
        target = chosen_targets
    if 'aura' in _type_line_lower and ('enchant creature' in _oracle_lower or 'enchant permanent' in _oracle_lower):
        # June 11 audit: Animate Dead / Necromancy / Dance of the Dead say
        # "Enchant creature card in a graveyard" — this scan checked the
        # BATTLEFIELD for them, rejecting the cast 6 times in one game while
        # the graveyard was full of legal targets (surfacing upstream as the
        # "unknown reason — mana looks sufficient" retry storm). Scan the
        # zone the aura actually enchants.
        if 'in a graveyard' in _oracle_lower:
            declared_graveyard_target = None
            declared_graveyard_owner = None
            if target is not None:
                target_name = getattr(target, 'name', target)
                for p in game.players:
                    for gc in p.graveyard:
                        if gc is target or (isinstance(target_name, str) and gc.name.lower() == target_name.lower()):
                            declared_graveyard_target = gc
                            declared_graveyard_owner = p
                            break
                    if declared_graveyard_target:
                        break
                if not declared_graveyard_target or not declared_graveyard_target.is_creature():
                    return ((False,
                             f"{card.name} can't target {target_name} — it is not a creature card in a graveyard",
                             []), _cast_from_graveyard, target)
                card._declared_graveyard_target_id = declared_graveyard_target.id
                card._declared_graveyard_target_owner = declared_graveyard_owner.name
            any_target = any(
                gc.is_creature()
                for p in game.players
                for gc in p.graveyard
            )
            if not any_target:
                return ((False,
                         f"{card.name} can't be cast — no creature cards in any graveyard (CR 601.2c)",
                         []), _cast_from_graveyard, target)
        else:
            # Quick legal-target scan: does ANY permanent on the battlefield
            # satisfy "enchant creature" / "enchant permanent"? Hexproof/shroud
            # checks are done at resolution; here we just want to see one body.
            # July 20: "enchant creature YOU CONTROL" scans only the caster's
            # battlefield — Draconic Destiny passed this gate off opponent
            # creatures, paid its mana, then fizzled at resolution.
            is_creature_only = 'enchant creature' in _oracle_lower
            _own_only = ('enchant creature you control' in _oracle_lower
                         or 'enchant permanent you control' in _oracle_lower)
            any_target = False
            for p in game.players:
                if _own_only and p is not player:
                    continue
                for c in p.battlefield:
                    if c.id == card.id:
                        continue
                    if is_creature_only and not c.is_creature():
                        continue
                    any_target = True
                    break
                if any_target:
                    break
            if not any_target:
                return ((False,
                         f"{card.name} can't be cast — no legal targets on the battlefield (CR 601.2c)",
                         []), _cast_from_graveyard, target)

    # CR 903.4: a card may not be cast in a singleton command-zone format if its
    # color identity is outside the player's commander color identity. Apr 30
    # audit: deck-validation warned but didn't enforce, so a {B} spell got cast
    # in a U/G partner deck. Block at cast time when we can compute the answer
    # cheaply. Skipped if either side has empty/unknown color identity (avoids
    # blocking on cache misses).
    if game.format in ('commander', 'edh', 'brawl', 'oathbreaker'):
        try:
            commander_colors = set(player._get_commander_colors())
            # May 7 audit: when casting a partner commander, the cast pipeline
            # removes the card from `command_zone` BEFORE this check runs (see
            # mtg/autoplay.py:1070 and similar paths in cog.py / engine.py),
            # so `_get_commander_colors()` sees only the OTHER partner and
            # reports identity {B,W} for a Thrasios cast in Thrasios+Tymna.
            # If the card being cast is itself a commander, fold its identity
            # back into the union so the cast is never blocked by its own
            # removal-during-cast.
            if getattr(card, 'is_commander', False):
                commander_colors.update(getattr(card, 'color_identity', []) or [])
            card_identity = set(getattr(card, 'color_identity', []) or [])
            if commander_colors and card_identity and not card_identity.issubset(commander_colors):
                outside = card_identity - commander_colors
                msg = f"{card.name} ({{{','.join(sorted(card_identity))}}}) is outside commander identity ({{{','.join(sorted(commander_colors))}}}) — extra: {','.join(sorted(outside))}"
                print(f"[COLOR-IDENTITY] Blocked {player.name} from casting {card.name}: {msg}")
                # Track blocked-this-game cards on the player so the AI's
                # next decision prompt can list them under "DO NOT CAST".
                # Without this, the May 3 batch logged 24 repeated attempts
                # to cast Bloom Tender in a single deck — same card, same
                # block reason, every turn.
                if not hasattr(player, '_color_id_blocklist'):
                    player._color_id_blocklist = set()
                player._color_id_blocklist.add(card.name)
                return (False, msg, []), _cast_from_graveyard, target
        except Exception as e:
            print(f"[COLOR-IDENTITY] Check failed for {card.name}: {e}")

    # Block counterspells when nothing is on the stack to target (CR 601.2c).
    # May 14 audit: this rejection treats "counter target spell" as a hard
    # requirement, but it's actually one of N modes on modal spells (Mystic
    # Confluence, Archmage's Charm, Cryptic Command, etc.). Those spells have
    # bounce/draw/exile modes that work fine on an empty stack. Only block
    # cast when the spell ONLY has counter-target-spell and no other modes.
    # Caused 7+ counterspells to die in Baral's hand across game
    # 1504535777634160680 because she "couldn't cast" Mystic Confluence in
    # her own main phase even though its bounce/draw modes were available.
    oracle_lower = (card.oracle_text or '').lower()
    if 'counter target' in oracle_lower and 'spell' in oracle_lower:
        stack_has_spells = any(
            entry for entry in getattr(game, 'stack', [])
            if hasattr(entry, 'card') and entry.card
        )
        # A creature with counter-in-oracle is cast for its body; even if
        # the ETB counter fizzles on empty stack, the creature still enters.
        is_creature = card.is_creature()
        # Modal spells have bullet markers ('•') OR an explicit "choose one"
        # / "choose N" clause. Either is enough to indicate non-counter modes
        # exist that are legal on an empty stack.
        is_modal_with_other_modes = (
            ('•' in oracle_lower
             and any(kw in oracle_lower for kw in (
                 'draw a card', 'draw cards', 'return target', 'tap target',
                 'gain', 'destroy target', 'create', 'exile target', 'put a',
             )))
            or 'choose one' in oracle_lower
            or 'choose two' in oracle_lower
            or 'choose three' in oracle_lower
        )
        if not stack_has_spells and not is_creature and not is_modal_with_other_modes:
            return ((False, f"{card.name} requires a target spell on the stack", []),
                    _cast_from_graveyard, target)

    # July 24 batch-6 audit (reviewer S2): ability-ONLY counters (Stifle,
    # Trickbind — "Counter target activated or triggered ability", no spell
    # clause) were castable with only SPELLS on the stack; the counter_ability
    # fallback then illegally countered the spell (Stifle beat a Scroll Rack
    # SPELL, game_1529988360263827656). CR 601.2c: no legal target, no cast.
    # Voidslime/Disallow/Tale's End name spells in their text and skip this.
    if ('counter target' in oracle_lower
            and ('activated' in oracle_lower or 'triggered' in oracle_lower)
            and 'spell' not in oracle_lower):
        stack_has_ability = any(
            not getattr(entry, 'is_spell', True)
            for entry in getattr(game, 'stack', []))
        if not stack_has_ability and not card.is_creature():
            return ((False, f"{card.name} requires a target activated or "
                            f"triggered ability on the stack", []),
                    _cast_from_graveyard, target)

    # July 20 audit (Diabolic Intent): "As an additional cost to cast this
    # spell, sacrifice a creature" — the plan-validate path checked this, but
    # the decide_action_inline fallback didn't, so the cast went through,
    # burned the card and its mana, and fizzled with "no creature to
    # sacrifice" (game_1526071467035459665). Gate it here so EVERY cast path
    # is covered (CR 601.2g — a spell can't be cast without paying its costs).
    _sac_m = re.search(
        r'as an additional cost to cast this spell, sacrifice '
        r'(?:a|an|two|three)?\s*(\w+)', oracle_lower)
    if _sac_m:
        _sac_type = _sac_m.group(1).rstrip('s')
        _type_checks = {
            'creature': lambda c: c.is_creature(),
            'artifact': lambda c: c.is_artifact(),
            'land': lambda c: c.is_land(),
            'permanent': lambda c: True,
        }
        _chk = _type_checks.get(_sac_type)
        if _chk is not None and not any(
                _chk(c) and c is not card for c in player.battlefield):
            return ((False,
                     f"{card.name} needs a {_sac_type} to sacrifice as an additional cost",
                     []), _cast_from_graveyard, target)

    # [TARGETING] Pre-cast target validation (CR 601.2c) — block spells with
    # no legal targets.  Only checks instant/sorcery spells that require targets.
    # Permissive: if the targeting module errors or can't parse, allows the cast.
    if HAS_TARGETING and _spell_requires_targets(card):
        if not _find_any_valid_target(game, card, player.name):
            print(f"[TARGETING] {card.name} has no valid targets — cast blocked")
            return ((False, f"{card.name} has no valid targets", []),
                    _cast_from_graveyard, target)

        # Validate the target actually declared by the caller, not merely the
        # existence of some other legal target. Graveyard Auras are handled
        # above because the generic adapter is battlefield-oriented.
        if target is not None and 'in a graveyard' not in _oracle_lower:
            declared_targets = (list(target)
                                if isinstance(target, (list, tuple)) else [target])
            for declared_target in declared_targets:
                # A declared target that is a spell ON THE STACK (counterspell
                # responses forward top_stack.card from mtg/engine.py) must not
                # go through the battlefield-oriented validator — it would be
                # rejected as "not a valid target type". July 20 audit: every
                # explicit-target counter response in the July 16 batch fizzled
                # at cast this way. Stack targets are validated by the
                # counter-gate above (stack_has_spells) and fizzle-checked at
                # resolution instead.
                if any(getattr(entry, 'card', None) is declared_target
                       for entry in getattr(game, 'stack', [])):
                    continue
                if hasattr(declared_target, 'battlefield'):
                    legal, target_reason = _validate_player_target_for_action(
                        game, declared_target, card, player.name)
                else:
                    target_owner = next(
                        (p for p in game.players
                         if declared_target in p.battlefield), player)
                    legal, target_reason = _validate_target_for_action(
                        game, declared_target, target_owner, card, player.name)
                if not legal:
                    return ((False, f"Illegal target for {card.name}: {target_reason}", []),
                            _cast_from_graveyard, target)
    return None, _cast_from_graveyard, target


def _compute_alt_costs(engine, game: GameState, player: Player, card: Card,
                       pay_mana: bool, additional_cost: int
                       ) -> Tuple[Optional[Tuple[bool, str, List[str]]], Optional[Dict]]:
    """Cost selection + alternative/additional cost handling (refactor #2 step 2b).

    Covers, in original order (rationale comments inline):
      1. effective cost selection — flashback / adventure-half / split-half
      2. suspend-only gate (no mana cost ⇒ can only be suspended)
      3. X-cost computation (+ the X-bounded re-target check)
      4. free-cast turn effects (Rishkar's Expertise, cascade)
      5. printed alternate costs (Force of Will, Fireblast, Pacts) — these
         MUTATE state when taken (exile/life/sacrifice), same as before
      6. convoke / delve / improvise generic-cost reductions (tap/exile
         state mutations happen here, same as before)

    Returns (rejection, costs): rejection is the (False, reason, []) tuple to
    return verbatim (costs is then None); on success costs carries
    effective_mana_cost / effective_cmc / total_cost / x_value_chosen /
    pay_mana / free_cast_source / total_alt_reduction for the payment stage.
    NOTE (pre-existing, pinned behavior): the alternate-cost branches build a
    local effect_messages line that the orchestrator has always discarded —
    kept verbatim, not surfaced, so the split stays behavior-preserving.
    """
    # Calculate total cost including commander tax
    # Handle X-cost spells: X defaults to all available mana minus fixed costs
    # Use adventure cost if casting the adventure half
    effective_mana_cost = card.mana_cost
    effective_cmc = card.cmc
    # Use flashback cost if casting via flashback / Snapcaster-granted flashback.
    # May 13 audit: previously gated on `card not in player.hand`, but the
    # graveyard-cast pipeline moves the card briefly into `player.hand`
    # before invoking cast_spell_async (to reuse the from-hand machinery),
    # so the zone check was always False and the spell paid the regular
    # cost (Lingering Souls {2}{W} instead of flashback {1}{B}). The
    # `_flashback_cost` marker is the authoritative signal — it's only set
    # by the graveyard-cast initiator.
    if getattr(card, '_flashback_cost', None):
        effective_mana_cost = card._flashback_cost
        print(f"[FLASHBACK] Using flashback cost {card._flashback_cost} instead of {card.mana_cost}")
        card._flashback_cost = None  # Clear after use
    # Escape (CR 702.139): the alternative cost, like flashback above. There was
    # no branch here at all, so even once detection worked an escaped Kroxa
    # would have been charged his printed {B}{R} rather than {B}{B}{R}{R}.
    # NOTE the marker is deliberately NOT cleared here, unlike _flashback_cost:
    # the ETB reads `was_escaped` after resolution ("sacrifice it unless it
    # escaped"), so clearing it at payment time would make every escaped
    # creature sacrifice itself anyway.
    if getattr(card, '_escape_cost', None):
        effective_mana_cost = card._escape_cost
        effective_cmc = helpers.cmc_of_cost_string(card._escape_cost)
        print(f"[ESCAPE] Using escape cost {card._escape_cost} "
              f"(CMC {effective_cmc}) instead of {card.mana_cost}")
    if getattr(card, 'cast_as_adventure', False) and card.adventure_cost:
        effective_mana_cost = card.adventure_cost
        # July 21 batch audit: was a digits + plain-single-pip count that gave
        # hybrid pips 0 — Bring Back ({G/W}{G/W}{G/W}{G/W}) computed CMC 0.
        effective_cmc = helpers.cmc_of_cost_string(card.adventure_cost)
        print(f"[ADVENTURE] Using adventure cost {card.adventure_cost} (CMC {effective_cmc}) instead of creature cost {card.mana_cost}")
    elif (' // ' in (effective_mana_cost or '')
          and getattr(card, 'adventure_name', '')):
        # May 20 audit fix: adventure cards' card.mana_cost is the FULL
        # Scryfall split form "{2}{U} // {2}{U}". When casting as the creature
        # half (default — cast_as_adventure=False), strip to just the creature
        # cost before the half-separator so the mana parser doesn't sum BOTH
        # halves (game_1506618589132755035:1003 tapped 4 sources for what
        # should be CMC 3 because parser found 4 brace-groups). Fix the
        # display too — "{2}{U}" is clearer than "{2}{U} // {2}{U}".
        creature_half = effective_mana_cost.split(' // ', 1)[0].strip()
        if creature_half:
            print(f"[ADVENTURE] Casting as creature half — using {creature_half} "
                  f"(stripped from {effective_mana_cost})")
            effective_mana_cost = creature_half
            # Recompute effective_cmc against the stripped creature cost
            # (hybrid-aware — July 21 batch audit).
            effective_cmc = helpers.cmc_of_cost_string(creature_half)
    elif getattr(card, 'cast_as_split_half', -1) >= 0 and card.split_costs:
        half_idx = card.cast_as_split_half
        effective_mana_cost = card.split_costs[half_idx]
        # Split-half CMC (hybrid-aware — July 21 batch audit).
        effective_cmc = helpers.cmc_of_cost_string(effective_mana_cost)
        half_name = card.split_names[half_idx] if card.split_names else card.name
        print(f"[SPLIT] Using {half_name} cost {effective_mana_cost} (CMC {effective_cmc}) instead of full card cost {card.mana_cost}")

    # Block suspend-only cards from being cast normally (Mox Tantalite, Lotus Bloom, etc.)
    # These have no mana cost and can only be suspended, not cast from hand
    if not effective_mana_cost and card.oracle_text:
        oracle_lower = card.oracle_text.lower()
        if re.search(r'suspend\s+\d', oracle_lower) and card in player.hand:
            return ((False, f"{card.name} has no mana cost — it can only be suspended, not cast from hand", []),
                    None)

    # [COST-ADJUST] Static cost increases and reductions (CR 601.2f), computed
    # BEFORE X so the X budget can see them: a Medallion lets you pay 1 more
    # into X, a Sphere of Resistance 1 less. CR 601.2f order is mana cost +
    # additional costs + INCREASES, then reductions — increases are therefore
    # never shrunk by a reducer that ran first.
    cost_increase = 0
    raw_reduction = 0
    _red_sources: List[str] = []
    if pay_mana:
        from mtg.helpers import compute_cost_increase, compute_cost_reduction
        _from_gy = card.id in (getattr(player, 'playable_from_graveyard', None) or [])
        cost_increase, _inc_sources = compute_cost_increase(game, player, card)
        if cost_increase:
            print(f"[COST-INCREASE] {card.name}: +{cost_increase} generic from "
                  f"{', '.join(_inc_sources)}")
        raw_reduction, _red_sources = compute_cost_reduction(
            player, card, from_graveyard=_from_gy)

    total_cost = effective_cmc + additional_cost + cost_increase
    x_value_chosen = 0

    if effective_mana_cost and 'X' in effective_mana_cost.upper():
        # [MANA-ENGINE] Use ManaCost for structured X-cost parsing when available
        if HAS_MANA_ENGINE:
            _xp = ManaCost.parse(effective_mana_cost)
            x_count = _xp.x_count
            fixed_cost = _xp.cmc  # ManaCost.cmc treats X as 0
        else:
            cost_upper = effective_mana_cost.upper()
            x_count = cost_upper.count('X')
            colored_cost = sum(cost_upper.count(f'{{{c}}}') for c in ['W', 'U', 'B', 'R', 'G'])
            generic_cost = sum(int(m) for m in re.findall(r'\{(\d+)\}', cost_upper))
            fixed_cost = colored_cost + generic_cost
        
        # Check if caller specified X value via _x_value attribute
        if hasattr(card, '_x_value') and card._x_value is not None:
            x_value_chosen = card._x_value
            total_cost = (fixed_cost + (x_value_chosen * x_count)
                          + additional_cost + cost_increase)
        else:
            # Auto-calculate: use all available mana for X.
            # July 26: adjustments belong in this budget. An X spell's {X}
            # becomes generic once X is chosen, so a reduction always has
            # generic to eat here — Blue Sun's Zenith with a Sapphire
            # Medallion should draw one MORE card, not bank the mana.
            available = player.available_mana()
            remaining_for_x = max(0, available - fixed_cost - additional_cost
                                  - cost_increase + raw_reduction)
            x_value_chosen = remaining_for_x // max(x_count, 1)
            total_cost = (fixed_cost + (x_value_chosen * x_count)
                          + additional_cost + cost_increase)

        _adj = (f", increase=+{cost_increase}" if cost_increase else "") + \
               (f", reduction=-{raw_reduction}" if raw_reduction else "")
        print(f"[X-COST] {card.name}: X={x_value_chosen}, fixed={fixed_cost}, "
              f"total={total_cost}{_adj}")
        # Store X value on card so templates/context can access it
        card._x_value = x_value_chosen

        # [X-TARGET-CHECK] If the oracle text restricts targets by X (e.g.
        # Prismatic Ending: "target nonland permanent with mana value X or
        # less"), the initial target check at ~10810 ran with X=0 and may
        # have been permissive. Re-check now that X is known so we don't
        # commit mana to a spell with zero legal targets.
        if HAS_TARGETING and _spell_requires_targets(card):
            _oracle_lower = (card.oracle_text or '').lower()
            _x_restricted = re.search(
                r'target[^.]*?mana value\s+x\s+or\s+(less|greater)',
                _oracle_lower,
            )
            if _x_restricted:
                # Scan all nonland permanents and see if any fit the MV bound.
                _found = False
                _op = _x_restricted.group(1)
                for _pl in game.players:
                    for _perm in _pl.battlefield:
                        if _perm.is_land():
                            continue
                        _mv = getattr(_perm, 'cmc', 0) or 0
                        if (_op == 'less' and _mv <= x_value_chosen) or \
                           (_op == 'greater' and _mv >= x_value_chosen):
                            _found = True
                            break
                    if _found:
                        break
                if not _found:
                    print(f"[X-TARGET-CHECK] {card.name}: no legal targets "
                          f"with MV {_op} X={x_value_chosen} — cast blocked")
                    return ((False, (
                        f"{card.name}: no legal targets with mana value X={x_value_chosen} "
                        f"or {_op}"
                    ), []), None)

    # Check for free-cast from turn effects (Rishkar's Expertise, Cascade, etc.)
    free_cast_source = None
    if pay_mana and hasattr(game, 'turn_effects'):
        player_idx = game.players.index(player) if player in game.players else 0
        for te in game.turn_effects:
            if (te.get('type') == 'free_cast'
                    and te.get('controller') == player_idx
                    and not te.get('used', False)):
                max_mv = te.get('max_mv', 5)
                card_mv = card.cmc or 0
                if card_mv <= max_mv:
                    te['used'] = True
                    pay_mana = False
                    free_cast_source = te.get('source', 'ability')
                    print(f"[FREE-CAST] {card.name} (MV {card_mv}) cast for free via {free_cast_source} (max MV {max_mv})")
                    break

    # Check for alternate costs (Force of Will, Pact of Negation, etc.)
    used_alternate_cost = False
    if pay_mana and (effective_mana_cost or additional_cost > 0):
        oracle_lower = (card.oracle_text or '').lower()
        # Force of Will: "You may pay 1 life and exile a blue card from your hand rather than pay this spell's mana cost"
        if 'pay 1 life and exile a' in oracle_lower and 'from your hand' in oracle_lower:
            # Check if player can't afford mana but can pay alternate cost
            available = player.available_mana()
            if available < total_cost:
                # Find a card of the right color to exile
                exile_color = None
                for color in ['U', 'B', 'R', 'G', 'W']:
                    if f'{color.lower()} card' in oracle_lower or f'{color.lower()} card' in oracle_lower:
                        exile_color = color
                        break
                if not exile_color:
                    # Parse "a blue card" etc.
                    color_map = {'blue': 'U', 'black': 'B', 'red': 'R', 'green': 'G', 'white': 'W'}
                    for color_name, color_code in color_map.items():
                        if color_name in oracle_lower:
                            exile_color = color_code
                            break
                if exile_color:
                    exile_candidates = [c for c in player.hand if c != card and c.mana_cost and exile_color in c.mana_cost.upper()]
                    if exile_candidates:
                        # Pick least valuable card to exile
                        to_exile = min(exile_candidates, key=lambda c: c.cmc or 0)
                        player.hand.remove(to_exile)
                        player.exile.append(to_exile)
                        player.life -= 1
                        player.record_life_loss(1)
                        effect_messages = [f"💫 {card.name} cast via alternate cost: exile {to_exile.name}, pay 1 life (Life: {player.life})"]
                        pay_mana = False
                        used_alternate_cost = True
                        print(f"[ALTERNATE-COST] {card.name}: exiled {to_exile.name}, paid 1 life")
        # Fireblast: "You may sacrifice two Mountains rather than pay this spell's mana cost"
        elif 'sacrifice' in oracle_lower and 'rather than pay' in oracle_lower:
            available = player.available_mana()
            if available < total_cost:
                # Parse what to sacrifice (e.g., "two Mountains")
                sac_match = re.search(r'sacrifice (\w+) (\w+)', oracle_lower)
                if sac_match:
                    count_word = sac_match.group(1)
                    perm_type = sac_match.group(2).rstrip('s')  # "mountains" -> "mountain"
                    count_map = {'two': 2, 'three': 3, 'a': 1, 'an': 1, 'one': 1}
                    sac_count = count_map.get(count_word, 1)
                    # Find matching permanents
                    candidates = [c for c in player.battlefield
                                  if perm_type in c.name.lower() or perm_type in (c.type_line or '').lower()]
                    if len(candidates) >= sac_count:
                        for i in range(sac_count):
                            sac_card = candidates[i]
                            game.unregister_static_effects(sac_card)
                            player.battlefield.remove(sac_card)
                            player.graveyard.append(sac_card)
                        sac_names = [candidates[i].name for i in range(sac_count)]
                        effect_messages = [f"💫 {card.name} cast via alternate cost: sacrifice {', '.join(sac_names)}"]
                        pay_mana = False
                        used_alternate_cost = True
                        print(f"[ALTERNATE-COST] {card.name}: sacrificed {', '.join(sac_names)}")
        # Pact of Negation, Pact of the Titan, etc.: costs 0 now, pay next upkeep
        elif card.cmc == 0 and 'pact' in card.name.lower() and effective_mana_cost in ('', '{0}', None):
            pay_mana = False
            used_alternate_cost = True
            print(f"[PACT] {card.name} cast for free (pact cost due next upkeep)")

    # July 27 fanout audit: an X spell's {X} becomes generic once X is
    # chosen, so convoke/delve/improvise can pay it -- but their
    # generic_cost scans only for digit-brace symbols, which find nothing
    # in "{X}{U}{U}". Logic Knot with a 3-card graveyard exiled ZERO cards
    # and the cast then failed. The July 26 cost-reduction headroom below
    # already accounted for X; this closes the same gap for the three
    # alternative-cost reducers, which is an asymmetry I left behind.
    _x_derived_generic = x_value_chosen * (
        effective_mana_cost.upper().count('X') if effective_mana_cost else 0)

    # [CONVOKE] Tap untapped creatures to help pay for spells with convoke
    # Each tapped creature pays {1} or one mana of its color
    convoke_reduction = 0
    if pay_mana and card.oracle_text and 'convoke' in card.oracle_text.lower():
        untapped_creatures = [c for c in player.creatures() if not c.tapped and c != card]
        # Tap creatures to reduce generic cost (simple: each taps for {1})
        generic_cost = 0
        if effective_mana_cost:
            for sym in re.findall(r'\{(\d+)\}', effective_mana_cost):
                generic_cost += int(sym)
        generic_cost += additional_cost + _x_derived_generic
        creatures_to_tap = min(len(untapped_creatures), generic_cost)
        for i in range(creatures_to_tap):
            untapped_creatures[i].tapped = True
            convoke_reduction += 1
        if convoke_reduction > 0:
            additional_cost = max(0, additional_cost - convoke_reduction)
            print(f"[CONVOKE] {card.name}: tapped {convoke_reduction} creature(s) to help pay")

    # [DELVE] Exile cards from graveyard to reduce generic mana cost
    # Each exiled card pays {1} generic (CR 702.66)
    delve_reduction = 0
    if pay_mana and card.oracle_text and 'delve' in card.oracle_text.lower():
        # Auto-select: exile highest-CMC non-creature cards first (preserve reanimation targets)
        gy_candidates = sorted(player.graveyard, key=lambda c: -(c.cmc or 0))
        generic_cost = 0
        if effective_mana_cost:
            for sym in re.findall(r'\{(\d+)\}', effective_mana_cost):
                generic_cost += int(sym)
        generic_cost += additional_cost + _x_derived_generic - convoke_reduction
        cards_to_exile = min(len(gy_candidates), max(0, generic_cost))
        exiled_names = []
        for i in range(cards_to_exile):
            c = gy_candidates[i]
            player.graveyard.remove(c)
            player.exile.append(c)
            delve_reduction += 1
            exiled_names.append(c.name)
        if delve_reduction > 0:
            # May 20 audit (Bug F): show ALL exiled names when ≤6, append "..."
            # when truncating. Was "exiled 5 card(s) from graveyard (A, B, C)"
            # — count says 5 but only listed 3, looked like a truncation bug.
            _names_str = ', '.join(exiled_names) if len(exiled_names) <= 6 else (
                ', '.join(exiled_names[:6]) + f', ... +{len(exiled_names) - 6} more'
            )
            print(f"[DELVE] {card.name}: exiled {delve_reduction} card(s) from graveyard ({_names_str})")

    # [IMPROVISE] Tap untapped non-creature artifacts to reduce generic cost
    # Each tapped artifact pays {1} generic (CR 702.126)
    improvise_reduction = 0
    if pay_mana and card.oracle_text and 'improvise' in card.oracle_text.lower():
        untapped_artifacts = [c for c in player.active_battlefield()
                              if not c.tapped and c.is_artifact() and not c.is_creature() and c != card]
        generic_cost = 0
        if effective_mana_cost:
            for sym in re.findall(r'\{(\d+)\}', effective_mana_cost):
                generic_cost += int(sym)
        generic_cost += (additional_cost + _x_derived_generic
                         - convoke_reduction - delve_reduction)
        artifacts_to_tap = min(len(untapped_artifacts), max(0, generic_cost))
        for i in range(artifacts_to_tap):
            untapped_artifacts[i].tapped = True
            improvise_reduction += 1
        if improvise_reduction > 0:
            print(f"[IMPROVISE] {card.name}: tapped {improvise_reduction} artifact(s) to help pay")

    # [COST-REDUCTION] Static "<X> spells you cast cost {N} less to cast"
    # (CR 601.2f) — July 26, 2026. Before this, ManaCost.cost_reductions was
    # declared in rules/mana.py and never written by anything, so all nine
    # reducers in the test decks were blank cards; Baral, Chief of Compliance
    # is a COMMANDER whose whole defining ability did nothing in the deck
    # named after him.
    # Cap the reduction computed above, now that X, the increase, and the
    # convoke/delve/improvise draws on the same pool are all known.
    cost_reduction = 0
    if raw_reduction > 0:
        # CR 601.2f: a reduction can only eat the GENERIC portion — it can
        # never pay a colored pip. `generic_needed` downstream is NOT clamped
        # at zero, so an uncapped reduction here would silently under-require
        # colored mana. The increase is INSIDE the headroom because increases
        # apply first and are themselves reducible.
        printed_generic = 0
        if effective_mana_cost:
            for sym in re.findall(r'\{(\d+)\}', effective_mana_cost):
                printed_generic += int(sym)
        headroom = max(0, printed_generic + additional_cost + cost_increase
                       + _x_derived_generic
                       - (convoke_reduction + delve_reduction
                          + improvise_reduction))
        cost_reduction = min(raw_reduction, headroom)
        if cost_reduction > 0:
            print(f"[COST-REDUCTION] {card.name}: -{cost_reduction} generic "
                  f"from {', '.join(_red_sources)}")
        else:
            print(f"[COST-REDUCTION] {card.name}: {raw_reduction} available "
                  f"from {', '.join(_red_sources)} but no generic left to "
                  f"reduce (CR 601.2f)")

    total_alt_reduction = (convoke_reduction + delve_reduction
                           + improvise_reduction + cost_reduction)
    return None, {
        'effective_mana_cost': effective_mana_cost,
        'effective_cmc': effective_cmc,
        'total_cost': total_cost,
        'x_value_chosen': x_value_chosen,
        'pay_mana': pay_mana,
        'free_cast_source': free_cast_source,
        'total_alt_reduction': total_alt_reduction,
        'cost_increase': cost_increase,
    }


def _pay_costs(engine, game: GameState, player: Player, card: Card,
               costs: Dict, additional_cost: int
               ) -> Optional[Tuple[bool, str, List[str]]]:
    """Mana payment (refactor #2 step 2c — extracted July 20, 2026).

    Color-aware tapping via the mana engine when available (convoke/delve/
    improvise reductions apply to the GENERIC portion, commander tax rides
    as additional generic); amount-based tapping fallback otherwise. No-op
    when the cost stage already waived payment (free cast, alternate cost).

    Returns the (False, reason, []) rejection tuple when tapping fails, or
    None on success (card._mana_paid stamped for X-spell math).
    """
    effective_mana_cost = costs['effective_mana_cost']
    effective_cmc = costs['effective_cmc']
    total_cost = costs['total_cost']
    x_value_chosen = costs['x_value_chosen']
    total_alt_reduction = costs['total_alt_reduction']
    cost_increase = costs.get('cost_increase', 0)
    pay_mana = costs['pay_mana']

    # Pay mana cost — use color-aware tapping when mana engine is available
    if pay_mana and (effective_mana_cost or additional_cost > 0):
        # [MANA-ENGINE] Color-aware tapping ensures we tap the RIGHT lands
        # (e.g., Plains for W, Island for U) instead of naively tapping N lands.
        has_phyrexian = bool(effective_mana_cost and '/P}' in effective_mana_cost.upper())
        if HAS_MANA_ENGINE and effective_mana_cost:
            # Delve/convoke/improvise reduce the GENERIC portion of the cost,
            # not the additional cost (commander tax). Pass as negative additional
            # to reduce the parsed generic requirement from the mana cost string.
            tapped_ok = player.tap_sources_for_cost(
                effective_mana_cost,
                additional_generic=additional_cost + cost_increase - total_alt_reduction,
                x_value=x_value_chosen,
                pay_phyrexian_with_life=has_phyrexian,
                game=game,
            )
        else:
            # Fallback: amount-based tapping (no color awareness)
            tapped_ok = player.tap_lands_for_mana(max(0, total_cost - total_alt_reduction), game=game)
        if not tapped_ok:
            tax_note = f" + {additional_cost} commander tax" if additional_cost > 0 else ""
            cast_name = card.adventure_name if getattr(card, 'cast_as_adventure', False) else card.name
            return False, f"Not enough mana to cast {cast_name} (needs {effective_cmc}{tax_note} = {total_cost} total)", []
        # Track mana paid for X spell calculations
        card._mana_paid = total_cost
    return None


# Absolute ceiling on LIFO-rescue cycles after the extension cap is hit. The
# rescue loop spends the first `_rescue_min_cycles` unconditionally, then only
# keeps going while the stack above is demonstrably moving (see the July 26
# comment in `_await_stack_window`). This cap is what preserves the
# anti-deadlock guarantee — it must stay finite.
_MAX_LIFO_RESCUE_CYCLES = 12


def _force_stack_above(engine, game: GameState, stack_entry,
                       effect_messages: List[str]) -> bool:
    """LIFO-order rescue for a buried entry whose extension cap ran out.

    July 24 batch-6 audit (reviewer S2, CRITICAL): the cap-hit branch used to
    resolve the buried entry anyway — Smothering Tithe resolved BENEATH the
    An Offer You Can't Refuse targeting it, and 4/4 cap-hits in
    game_1529988360263827656 were CR 608 violations that defeated correctly
    cast counterspells. Instead of resolving out of order, act on the stack
    ABOVE us: resolve stalled TRIGGER entries inline (the same template path
    on_stack_resolve uses), then wake the topmost SPELL entry's
    resolution_event so its own coroutine resolves it (that is the normal
    contract — the event means "your turn to resolve", and a counter
    resolving will mark us countered). Returns True if anything was acted on.
    """
    from mtg.util import maybe_reraise
    # July 29 batch audit: while a [CAST-TRIGGER-PRIORITY] window is open, the
    # trigger it guards is NOT stalled — its owner is mid-evaluation, and
    # resolving it inline here double-resolves it when the window closes
    # (batch 15315: the Murmuring Mystic trigger resolved at the cap-hit AND
    # again via [CAST-TRIGGER-VANISHED], with the Bird token materializing
    # silently). The wait loop upstream now waits windows out, so reaching
    # here with one open means the window hung — still don't touch it.
    if getattr(game, '_trigger_window_depth', 0) > 0:
        print("[STACK-LIFO-FORCE] cast-trigger priority window open — "
              "not force-resolving anything above the cap-hit entry")
        return False
    try:
        idx = game.stack.index(stack_entry)
    except ValueError:
        return False
    acted = False
    # Resolve stalled trigger entries above us, top-down.
    for i in range(len(game.stack) - 1, idx, -1):
        entry = game.stack[i]
        if getattr(entry, 'is_spell', True):
            continue
        src_name = getattr(entry, 'trigger_source', None) or '?'
        if getattr(entry, 'countered', False):
            effect_messages.append(
                f"🚫 **{src_name}**'s triggered ability is countered!")
        elif HAS_EFFECT_TEMPLATES and getattr(entry, 'trigger_text', None):
            try:
                ctrl_idx = getattr(entry, 'controller_index', 0)
                ctrl = game.players[ctrl_idx]
                opp = game.players[1 - ctrl_idx]
                ctx = build_game_context(game, ctrl, opp, card=entry.card)
                lib = get_effect_library()
                actions, explanation = lib.resolve_etb(
                    card_name=src_name, oracle_text=entry.trigger_text,
                    controller=ctrl.name, opponent=opp.name, game_context=ctx)
                for act in (actions or []):
                    if act.get("action") == "no_action":
                        continue
                    try:
                        msg = engine.rules._execute_action_on_state(game, act)
                        if msg:
                            effect_messages.append(msg)
                    except (ValueError, KeyError, AttributeError,
                            TypeError, IndexError) as te:
                        print(f"[STACK-LIFO-FORCE] trigger action failed: {te}")
                        maybe_reraise(te)
                print(f"[STACK-LIFO-FORCE] resolved stalled trigger "
                      f"{src_name} above the cap-hit entry")
            except (ValueError, KeyError, AttributeError,
                    TypeError, IndexError) as te:
                print(f"[STACK-LIFO-FORCE] trigger resolve failed for "
                      f"{src_name}: {te}")
                maybe_reraise(te)
        del game.stack[i]
        _pid = getattr(entry, 'priority_id', None)
        ps = getattr(game, '_priority_system', None)
        if _pid and ps and hasattr(ps, 'remove_stack_entry_by_priority_id'):
            try:
                ps.remove_stack_entry_by_priority_id(_pid)
            except (ValueError, KeyError, AttributeError):
                pass
        acted = True
    # Wake the topmost SPELL entry still above us — its awaiting coroutine
    # owns the resolution (it may counter us).
    try:
        idx = game.stack.index(stack_entry)
    except ValueError:
        return acted
    for i in range(len(game.stack) - 1, idx, -1):
        entry = game.stack[i]
        ev = getattr(entry, 'resolution_event', None)
        if ev is not None and not ev.is_set():
            _e_name = (entry.card.name if getattr(entry, 'card', None) else '?')
            print(f"[STACK-LIFO-FORCE] waking buried-above spell {_e_name} "
                  f"to resolve in LIFO order")
            ev.set()
            acted = True
            break
    return acted


def _entries_above(game: GameState, stack_entry) -> int:
    """How many stack entries sit ABOVE `stack_entry` (0 == we are on top).

    Returns -1 when the entry has already left the stack, which callers treat
    as "nothing left to wait for".
    """
    try:
        return len(game.stack) - game.stack.index(stack_entry) - 1
    except ValueError:
        return -1


def _awake_spell_above(game: GameState, stack_entry) -> bool:
    """True if a SPELL above us already has its resolution_event set.

    An already-set event means that spell's own coroutine owns the pop and is
    on its way to resolving — `_force_stack_above` has nothing left to do for
    it, but we are NOT deadlocked, just slower than our rescue budget. The
    caller uses this to keep waiting instead of resolving out of LIFO order.
    """
    idx = game.stack.index(stack_entry) if stack_entry in game.stack else -1
    if idx < 0:
        return False
    for i in range(len(game.stack) - 1, idx, -1):
        ev = getattr(game.stack[i], 'resolution_event', None)
        if ev is not None and ev.is_set():
            return True
    return False


async def _await_stack_window(engine, game: GameState, player: Player,
                              card: Card, target: Any,
                              effect_messages: List[str]
                              ) -> Tuple[Optional[Tuple[bool, str, List[str]]], List[str], int]:
    """Stack push + priority window (refactor #2 step 2d — extracted July 20, 2026).

    In original order: push the StackEntry, early cast announcement, cast
    triggers (Rhystic Study fire on CAST, before any counter window),
    priority-system resync + announce, the adaptive resolution timeout with
    LIFO extensions, the countered-spell zone routing (command zone /
    suspend-exile / library / graveyard per CR 903.9b etc.), resolution-time
    target fizzle (CR 608.2b), and the pre-resolve stack pop.

    Returns (final, cast_trigger_msgs, player_idx): `final` is a completed
    (success, message, effect_messages) result when the spell was countered
    or fizzled — the caller returns it verbatim; None means resolve on.
    `effect_messages` is mutated in place throughout (shared list).
    """
    # [STACK] Push spell onto the stack
    player_idx = game.players.index(player) if player in game.players else 0
    stack_entry = StackEntry(
        card=card,
        controller_name=player.name,
        controller_index=player_idx,
        target=target,
        is_spell=True,
    )
    game.stack.append(stack_entry)
    print(f"[STACK] {card.name} goes on the stack (controller: {player.name}, stack size: {len(game.stack)})")

    # May 18 audit: helper to keep game.stack and PrioritySystem.stack in
    # sync. The fast-path resolution (0.5s auto-resolve when no opponent
    # interaction) bypasses PrioritySystem._resolve_top_of_stack and just
    # pops from game.stack directly. Without this dual-pop, the
    # PrioritySystem holds phantom StackObjects for already-resolved
    # spells, and when its priority cycle later fires, it calls
    # on_stack_resolve(phantom_obj) → "[STACK] No priority_id match for X,
    # using shared event" (a harmless no-op, but visibly noisy). This was
    # the cascade visible in `game_1505768915773685800` where 5 spells
    # showed the warning while combat priority spun until timeout.
    def _drop_from_priority_stack():
        pid = getattr(stack_entry, 'priority_id', None)
        if not pid:
            return
        ps = getattr(game, '_priority_system', None)
        if ps is None:
            return
        try:
            if hasattr(ps, 'remove_stack_entry_by_priority_id'):
                ps.remove_stack_entry_by_priority_id(pid)
        except Exception as _ps_err:
            print(f"[STACK] priority-stack sync failed for {card.name}: {_ps_err}")

    # May 7 audit fix #1: emit cast announcement BEFORE the priority wait so it
    # appears in Discord before any counterspell response messages. Without this,
    # the response ("B responds with Counterspell!") races ahead of the cast
    # ("A cast Sun Titan"), producing the out-of-order: response → cast → countered.
    # Only when stack is enabled + send_func wired; caller still adds its own
    # announcement (which becomes a duplicate-suppressed echo in the same path).
    early_cast_announced = False
    if game.stack_enabled and getattr(game, '_stack_send_func', None):
        try:
            send_func = game._stack_send_func
            source_tag = ""
            if getattr(card, 'cast_from_command_zone', False):
                source_tag = " from command zone"
            elif getattr(card, '_cast_from_graveyard', False):
                source_tag = " from graveyard"
            await send_func(f"✨ {player.name} cast **{card.name}**{source_tag}")
            stack_entry._cast_announced = True
            # Mark on the game so callers can suppress duplicate announcement.
            # Use a dict keyed by id(card) so multiple in-flight casts don't collide.
            if not hasattr(game, '_early_announced_casts'):
                game._early_announced_casts = set()
            game._early_announced_casts.add(id(card))
            early_cast_announced = True
        except Exception as e:
            print(f"[STACK] Early cast announcement failed: {e}")

    # Pub/sub slice 4a (July 24, 2026): CARD_CAST shadow emit — once per
    # cast, at announcement time (CR 601.2i; cast triggers fire even if the
    # spell is later countered). This funnel carries every cast_spell_async
    # cast: hand, response, adventure half, flashback, commander, signature.
    # The scan below stays authoritative; the recorder diffs at end_turn.
    events.emit(events.CARD_CAST, game, card=card, caster=player,
                via="cast", engine=engine)
    # Cast triggers fire when spell is put on stack (before resolution/countering)
    # This is correct for Rhystic Study, Esper Sentinel, etc. — they trigger on cast
    cast_trigger_msgs = await engine._check_cast_triggers(game, player, card)
    effect_messages.extend(cast_trigger_msgs)

    # If stack is enabled, announce to PrioritySystem and wait for resolution
    if game.stack_enabled and game._priority_system:
        from rules.priority import PriorityAction

        # Stack depth guard — prevent infinite counter-counter loops
        MAX_STACK_DEPTH = 10
        if len(game.stack) > MAX_STACK_DEPTH:
            print(f"[STACK] Max depth {MAX_STACK_DEPTH} reached for {card.name}, skipping priority interaction")
        else:
            # Per-entry resolution event (enables stack wars — each spell has its own signal)
            stack_entry.resolution_event = asyncio.Event()

            # May 14 audit: the GameEngine advances its phase state independently
            # of rules.priority.PrioritySystem.phase. Nobody calls
            # priority_system.advance_phase(), so its `active_player` stays
            # frozen at `players[0]` ("Rick") and its `priority_holder` can
            # be left as the previous-turn's caster ("Claude" after she
            # responded last turn). That means when Rick now casts a
            # main-phase spell on his turn, player_action() rejects it
            # silently ("not your priority") — and cast_spell_async then
            # waits 6s on a resolution_event nobody will ever fire. Across
            # game_1504535777634160680 the audited Baral matchup, only 1/15
            # Rick casts got a priority response window because of this.
            # Sync the priority system with the actual caster + active player
            # before announcing the cast.
            ps = game._priority_system
            try:
                caster_name = player.name
                active_name = game.players[game.active_player_index].name
                if ps.active_player != active_name:
                    print(f"[STACK] Resyncing priority_system.active_player "
                          f"({ps.active_player} → {active_name})")
                    ps.active_player = active_name
                if ps.priority_holder != caster_name:
                    print(f"[STACK] Resyncing priority_system.priority_holder "
                          f"({ps.priority_holder} → {caster_name}) — caster has "
                          f"priority by CR 117.1a (they just initiated a cast)")
                    ps.priority_holder = caster_name
                    # Drop stale pass tracking — the new caster's action starts
                    # a fresh priority round.
                    ps._passes_in_succession = []
            except Exception as e:
                print(f"[STACK] Priority resync failed (continuing): {e}")

            # Announce spell — caster retains priority, auto-pass timer starts
            result = await game._priority_system.player_action(
                player.name,
                PriorityAction.cast(card.name, targets=[str(target)] if target else [])
            )
            # May 14 audit: if player_action still rejects after resync (race
            # condition or unexpected state), surface it so a future audit
            # can spot recurring failures. The fallback to a short timeout
            # below prevents a dead 6s wait.
            cast_rejected = bool(result) and not result.get("success", True)
            if cast_rejected:
                print(f"[STACK] player_action rejected cast for {card.name} "
                      f"after resync: {result.get('message', 'unknown')} — "
                      f"skipping priority window, resolving directly")
            # Correlate this StackEntry with the PrioritySystem's StackObject
            if result and result.get("stack_object"):
                stack_entry.priority_id = result["stack_object"]["id"]

            # Wait for this specific spell to resolve (or be countered)
            # Bug #34/#37: Use game-mode-appropriate timeout to prevent hangs
            resolution_timeout = 30.0
            if getattr(game, 'is_autoplay', False):
                # [STACK-PRIORITY] Check if opponent has affordable instant-speed interaction
                # before choosing timeout. Without this, 0.5s timeout resolves spells
                # before the AI can evaluate counterspells (decide_response is an API call).
                opponent_has_interaction = False
                caster_idx = game.players.index(player) if player in game.players else 0
                for opp_idx, opp in enumerate(game.players):
                    if opp_idx == caster_idx:
                        continue
                    if engine.claude_ai:
                        opp_instants = engine.claude_ai.has_instant_speed_cards(opp)
                        if opp_instants:
                            # July 20: alternate-cost aware — FoW class
                            affordable = [c for c in opp_instants
                                          if opp.can_pay_mana_cost(c.mana_cost)[0]
                                          or opp.can_pay_printed_alternate_cost(c)]
                            if affordable:
                                opponent_has_interaction = True
                                instant_names = [c.name for c in affordable[:3]]
                                print(f"[STACK-PRIORITY] Opponent {opp.name} has affordable interaction: "
                                      f"{', '.join(instant_names)} — extending timeout for AI response")
                                break
                if opponent_has_interaction:
                    # Was 12s when decide_response was broken (bug May 3 audit
                    # #1) and never actually responded — that wasted ~2.6h
                    # across the May 3 batch on dead-air waits. Now that
                    # decide_response works, the typical path is:
                    #   API call (1-3s) + decide_pass (~0s) + auto-pass timer
                    #   (~0.5s) → event fires within ~3s.
                    # If the AI casts a counterspell instead, the response
                    # cast cycle adds another ~2s. 6s gives a generous
                    # safety margin without leaving idle.
                    resolution_timeout = 6.0
                    # May 7 audit: track whether decide_response was actually
                    # queried during the priority window. If timeout fires
                    # without the AI ever being asked, we want a tagged log
                    # so an audit can distinguish "AI was queried and chose
                    # to pass" from "AI never got the question."
                    stack_entry._stack_ai_queried = False
                else:
                    resolution_timeout = 0.5  # No interaction possible — resolve fast
            elif game._priority_system and hasattr(game._priority_system, 'auto_pass_seconds'):
                resolution_timeout = max(game._priority_system.auto_pass_seconds * 10, 3.0)
            # May 14 audit: if player_action rejected the cast, drop timeout
            # to ~0 — there's no priority window to wait for, no resolution
            # event will ever fire, and waiting 6s is pure dead-air.
            if cast_rejected:
                resolution_timeout = 0.1
            try:
                await asyncio.wait_for(stack_entry.resolution_event.wait(), timeout=resolution_timeout)
            except asyncio.TimeoutError:
                # Safety net: only auto-resolve when this entry is at the TOP of
                # the stack (CR 608 — LIFO). If a counterspell or other later
                # cast is sitting on top of us, the underlying spell must wait
                # for that to resolve first. Without this guard, our timer
                # races the counter and resolves the targeted spell out-of-order,
                # then the counter "fizzles — no longer on the battlefield".
                # When not at top, extend the wait by one more cycle so the
                # later cast gets to resolve first.
                # July 30 batch-9 audit (CRITICAL): an entry REMOVED from the
                # stack is not "at top" — Summary Dismissal exiled Song of the
                # Worldsoul, the old `not game.stack` read the empty stack as
                # at-top, and the exiled spell resolved onto the battlefield.
                # An entry that vanished without a counter mark must never
                # resolve; treat it as countered so the branch below unwinds it.
                if (not stack_entry.countered
                        and game.stack is not None
                        and stack_entry not in game.stack):
                    print(f"[STACK-ENTRY-VANISHED] {card.name} left the stack "
                          f"without a counter mark — treating as countered, "
                          f"not resolving (CR 608)")
                    stack_entry.countered = True
                elif game.stack and game.stack[-1] is not stack_entry:
                    print(f"[STACK] Resolution timeout ({resolution_timeout}s) for {card.name}, "
                          f"but later cast on top — extending wait for LIFO order")
                    # May 7 audit: keep extending while a later cast remains on
                    # top — the previous single extension would still
                    # auto-resolve out-of-order if a stack war went deeper than
                    # one response (Supreme Verdict ← Counterspell ← Force of
                    # Will resolved bottom-up because Verdict's extended
                    # timeout fired before FoW resolved).
                    max_lifo_extensions = 5
                    extensions_used = 0
                    # July 29 batch audit: an OPEN cast-trigger priority window
                    # ([CAST-TRIGGER-PRIORITY] — an LLM evaluation in flight)
                    # blocks the response spell's whole coroutine for longer
                    # than this budget. Batch 15315: Beast Whisperer burned all
                    # 5 extensions + the rescue while Claude's Stifle
                    # evaluation over the Murmuring Mystic trigger was still
                    # running, then "resolved anyway" BENEATH the Arcane
                    # Denial targeting it (CR 608 — the counter fizzled).
                    # While a window is open, extensions don't count against
                    # the cap; a separate generous bound keeps the
                    # anti-deadlock guarantee if the window itself hangs.
                    max_window_waits = 20
                    window_waits_used = 0
                    while extensions_used < max_lifo_extensions:
                        try:
                            await asyncio.wait_for(stack_entry.resolution_event.wait(),
                                                   timeout=resolution_timeout)
                            # Event fired — spell resolved (or was countered) in proper order.
                            break
                        except asyncio.TimeoutError:
                            if (getattr(game, '_trigger_window_depth', 0) > 0
                                    and window_waits_used < max_window_waits):
                                window_waits_used += 1
                                print(f"[STACK] {card.name} buried but a cast-trigger "
                                      f"priority window is open — waiting it out "
                                      f"({window_waits_used}/{max_window_waits}, "
                                      f"extensions not consumed)")
                                continue
                            extensions_used += 1
                            # If we've now reached the top, fall through to
                            # the normal resolve-now path on the next loop
                            # iteration. If still buried, keep waiting.
                            if stack_entry.countered:
                                # Marked while we waited — the countered
                                # branch below owns the unwind.
                                break
                            if stack_entry not in game.stack:
                                # July 30 batch-9 audit (CRITICAL): the old
                                # `not game.stack` misread "entry removed,
                                # stack now empty" as "now at top" and
                                # resolved a Summary-Dismissal-exiled spell
                                # onto the battlefield. Vanished without a
                                # counter mark = external removal; never
                                # resolve.
                                print(f"[STACK-ENTRY-VANISHED] {card.name} left "
                                      f"the stack without a counter mark — "
                                      f"treating as countered, not resolving "
                                      f"(CR 608)")
                                stack_entry.countered = True
                                break
                            if game.stack[-1] is stack_entry:
                                print(f"[STACK] {card.name} now at top after {extensions_used} extension(s), resolving")
                                break
                            print(f"[STACK] {card.name} still buried under {game.stack[-1].card.name if game.stack[-1].card else '?'} — extending again ({extensions_used}/{max_lifo_extensions})")
                    else:
                        # Hit the safety cap while still buried. July 24
                        # batch-6 audit (reviewer S2, CRITICAL): resolving
                        # anyway here defeated counterspells — Smothering
                        # Tithe resolved beneath the An Offer You Can't
                        # Refuse targeting it (CR 608). Force the stack
                        # above us to act first (stalled triggers resolve
                        # inline; the topmost buried spell's coroutine is
                        # woken), then grant short extra wait cycles for
                        # those resolutions — a counter above will mark us
                        # countered. Resolve-anyway remains only as the
                        # true-deadlock last resort.
                        print(f"[STACK] LIFO extension cap ({max_lifo_extensions}) hit for {card.name}; "
                              f"forcing the stack above to act first (CR 608)")
                        # July 26 batch-7 audit: a FIXED 3-cycle budget gave up
                        # while the stack above was still actively resolving,
                        # and we then resolved out of LIFO order anyway — in
                        # game_1530441479389184000 Worldly Tutor resolved
                        # beneath the Pact of Negation that targeted it (5 of 7
                        # cap-hits batch-wide ended "rescue exhausted"). A spell
                        # above whose resolution_event is ALREADY SET is not a
                        # deadlock: its own coroutine owns the pop and is merely
                        # slower than our budget, so _force_stack_above has
                        # nothing left to do for it and every extra cycle we
                        # spend is cycles it needs. Keep going while we can see
                        # PROGRESS (the stack above is shrinking) or an awake
                        # spell above; the absolute cap preserves the original
                        # anti-deadlock guarantee.
                        _rescue_min_cycles = 3
                        _rescue_used = 0
                        _prev_above = _entries_above(game, stack_entry)
                        while _rescue_used < _MAX_LIFO_RESCUE_CYCLES:
                            if (stack_entry.countered
                                    or not game.stack
                                    or game.stack[-1] is stack_entry
                                    or stack_entry not in game.stack):
                                break
                            _rescue_used += 1
                            _force_stack_above(engine, game, stack_entry,
                                               effect_messages)
                            try:
                                await asyncio.wait_for(
                                    stack_entry.resolution_event.wait(),
                                    timeout=min(resolution_timeout, 3.0))
                                break
                            except asyncio.TimeoutError:
                                pass
                            _above = _entries_above(game, stack_entry)
                            _progress = 0 <= _above < _prev_above
                            _prev_above = _above
                            # Past the base budget, only keep spending cycles
                            # while something above is demonstrably moving.
                            if (_rescue_used >= _rescue_min_cycles
                                    and not _progress
                                    and not _awake_spell_above(game, stack_entry)):
                                print(f"[STACK-LIFO-FORCE] no progress above "
                                      f"{card.name} after {_rescue_used} rescue "
                                      f"cycle(s) — stack above looks stuck")
                                break
                        if (not stack_entry.countered and game.stack
                                and stack_entry in game.stack
                                and game.stack[-1] is not stack_entry):
                            print(f"[STACK] LIFO rescue exhausted for {card.name} "
                                  f"after {_rescue_used} cycle(s); "
                                  f"resolving anyway to prevent deadlock")
                    if stack_entry in game.stack and stack_entry is game.stack[-1]:
                        game.stack.remove(stack_entry)
                        _drop_from_priority_stack()
                        print(f"[STACK] Cleaned up timed-out stack entry for {card.name}")
                else:
                    print(f"[STACK] Resolution timeout ({resolution_timeout}s) for {card.name}, resolving now")
                    # May 7 audit: surface "AI never got the chance to respond"
                    # cases so log audits can distinguish "AI declined" from
                    # "AI was never queried" (the latter means the priority
                    # window was too short or the priority system didn't fire).
                    if hasattr(stack_entry, '_stack_ai_queried') and not stack_entry._stack_ai_queried:
                        print(f"[STACK-AI] AI not queried (timeout) for response to {card.name}")
                    if stack_entry in game.stack:
                        game.stack.remove(stack_entry)
                        _drop_from_priority_stack()
                        print(f"[STACK] Cleaned up timed-out stack entry for {card.name}")

        # Check if the spell was countered during the response window
        if stack_entry.countered:
            if stack_entry in game.stack:
                game.stack.remove(stack_entry)
            _drop_from_priority_stack()
            # Signature spells return to command zone even when countered
            _countered_to = getattr(stack_entry, 'countered_to', None)
            if _countered_to == "already_handled":
                # July 30 batch-9 audit: the removing effect (Summary
                # Dismissal, Spell Queller) already moved the card to its
                # destination zone and posted its own message — just unwind.
                # Re-routing here would clone the card into a second zone.
                print(f"[STACK] {card.name} was removed from the stack by "
                      f"another effect — unwinding without resolving")
                engine.rules.log_event(f"{player.name}'s {card.name} was removed from the stack")
                return ((True, f"Cast {card.name} (removed from stack)", effect_messages),
                        cast_trigger_msgs, player_idx)
            if getattr(card, 'is_signature_spell', False) and game.format == "oathbreaker":
                player.command_zone.append(card)
                print(f"[OATHBREAKER] {card.name} was countered → returns to command zone")
                effect_messages.append(f"❌ **{card.name}** was countered! → returns to command zone")
            elif _countered_to == "exile_suspend":
                # Delay: exile with three time counters, suspended (owner
                # recasts free when they run out — CR 702.62). June 11 audit:
                # this went to graveyard, and the "countered" Bloodghast
                # landfall-returned two turns later.
                card.suspended = True
                card.counters['time'] = 3
                player.exile.append(card)
                print(f"[STACK] {card.name} was countered — exiled with 3 time counters (suspend)")
                effect_messages.append(f"❌ **{card.name}** was countered — exiled with three time counters (suspended)!")
            elif _countered_to == "library":
                # Commit: owner's library, second from the top (CR 608.2 —
                # a resolved Commit moves the spell off the stack itself).
                _pos = min(1, len(player.library))
                player.library.insert(_pos, card)
                print(f"[STACK] {card.name} was countered — put into library second from top")
                effect_messages.append(f"❌ **{card.name}** is put into its owner's library, second from the top!")
            elif _countered_to == "library_top":
                player.library.insert(0, card)
                print(f"[STACK] {card.name} was countered — put on top of its owner's library")
                effect_messages.append(f"❌ **{card.name}** is put on top of its owner's library!")
            elif _countered_to == "exile":
                # July 23 audit (#8): Force of Negation — "exile it instead of
                # putting it into its owner's graveyard" (CR 614 zone-change
                # replacement). Was falling through to the graveyard branch,
                # leaving the countered spell recoverable.
                player.exile.append(card)
                print(f"[STACK] {card.name} was countered — exiled instead of graveyard")
                effect_messages.append(f"❌ **{card.name}** was countered — exiled!")
            elif (getattr(card, 'is_commander', False)
                  and game.format in COMMAND_ZONE_FORMATS):
                # CR 903.9b: a countered commander may go to the command zone
                # instead of the graveyard; autoplay always chooses it. June 11
                # audit: Aminatou was countered into the graveyard and lost for
                # the remaining 16 turns (game 1514618481029677117).
                if not hasattr(player, 'command_zone') or player.command_zone is None:
                    player.command_zone = []
                player.command_zone.append(card)
                print(f"[STACK] {card.name} was countered — commander returns to command zone (CR 903.9)")
                effect_messages.append(f"❌ **{card.name}** was countered! → returns to command zone")
            else:
                player.graveyard.append(card)
                print(f"[STACK] {card.name} was countered — goes to graveyard")
                effect_messages.append(f"❌ **{card.name}** was countered!")
            engine.rules.log_event(f"{player.name}'s {card.name} was countered")
            return ((True, f"Cast {card.name} (countered)", effect_messages),
                    cast_trigger_msgs, player_idx)

    # [TARGETING] Resolution-time target validation (CR 608.2b)
    # If ALL targets are now illegal, the spell fizzles (countered by game rules).
    if HAS_TARGETING:
        should_fizzle, fizzle_reason = _check_resolution_targets(game, stack_entry)
        if should_fizzle:
            if stack_entry in game.stack:
                game.stack.remove(stack_entry)
            _drop_from_priority_stack()
            # Signature spells return to command zone even when they fizzle
            if getattr(card, 'is_signature_spell', False) and game.format == "oathbreaker":
                player.command_zone.append(card)
                effect_messages.append(f"💨 **{fizzle_reason}** → returns to command zone")
                print(f"[OATHBREAKER] {card.name} fizzled → returns to command zone")
            else:
                player.graveyard.append(card)
                effect_messages.append(f"💨 **{fizzle_reason}**")
            print(f"[TARGETING] {fizzle_reason}")
            engine.rules.log_event(f"{card.name} fizzled — all targets illegal")
            return ((True, f"Cast {card.name} (fizzled)", effect_messages),
                    cast_trigger_msgs, player_idx)

    # Pop from stack before resolving
    if stack_entry in game.stack:
        game.stack.remove(stack_entry)
    _drop_from_priority_stack()
    return None, cast_trigger_msgs, player_idx


async def _dispatch_resolution(engine, game: GameState, player: Player,
                               card: Card, target: Any,
                               effect_messages: List[str],
                               cast_trigger_msgs: List[str],
                               player_idx: int) -> Tuple[bool, str, List[str]]:
    """Spell-effect resolution (refactor #2 step 2d — extracted July 20, 2026).

    Everything after the spell survives the stack: split/adventure half
    handling, permanent-vs-nonpermanent zone routing, ETB scans + Tier
    1/1.5/2/2.5/3 effect cascade, clone/aura/equipment handling, saga
    setup, layers/replacement registration, and the pending-message flush.
    Returns the final (success, message, effect_messages) for the cast.
    """
    # Handle split card casting — resolve only the chosen half's effect,
    # then the card goes to graveyard (both halves share one physical card)
    if getattr(card, 'cast_as_split_half', -1) >= 0 and card.split_texts:
        half_idx = card.cast_as_split_half
        half_name = card.split_names[half_idx] if card.split_names else card.name
        half_text = card.split_texts[half_idx]
        half_type = card.split_types[half_idx] if card.split_types else ""
        print(f"[SPLIT] Casting {half_name} (half {half_idx} of {card.name})")

        # Create a virtual card for the half so tier cascade works correctly
        split_half_card = Card(
            name=half_name,
            mana_cost=card.split_costs[half_idx] if card.split_costs else card.mana_cost,
            type_line=half_type,
            oracle_text=half_text,
        )
        split_msgs = engine.resolve_special_effects(game, player, split_half_card, target)
        if not split_msgs and HAS_EFFECT_TEMPLATES and half_text:
            try:
                player_idx = game.players.index(player) if player in game.players else 0
                opponent = game.players[1 - player_idx]
                ctx = build_game_context(game, player, opponent, card=split_half_card, explicit_target=target)
                lib = get_effect_library()
                tmpl_actions, tmpl_explanation = lib.resolve_spell(
                    card_name=half_name, oracle_text=half_text,
                    controller=player.name, opponent=opponent.name, game_context=ctx,
                )
                if tmpl_actions is not None:
                    for action in tmpl_actions:
                        if action.get("action") != "no_action":
                            try:
                                msg = engine.rules._execute_action_on_state(game, action)
                                if msg:
                                    split_msgs.append(msg)
                            except Exception as ae:
                                print(f"[SPLIT-TEMPLATE] Action failed: {ae}")
            except Exception as e:
                print(f"[SPLIT] Template error for {half_name}: {e}")
        if not split_msgs and engine.rules.client:
            try:
                resolved_msgs, _ = await engine.rules.resolve_effect(
                    game, half_text, half_name, player.name
                )
                if resolved_msgs:
                    split_msgs.extend(resolved_msgs)
            except Exception as e:
                print(f"[SPLIT] resolve_effect error for {half_name}: {e}")
        if not split_msgs:
            _ht = (half_text or '').replace('\n', ' ').strip()
            if len(_ht) > 500:
                _ht = _ht[:497].rstrip() + '…'
            split_msgs = [f"🧙 **{half_name}** resolves — {_ht}"]
        effect_messages.extend(split_msgs)
        # Split cards go to graveyard after resolving
        player.graveyard.append(card)
        card.cast_as_split_half = -1  # Reset flag
        engine.rules.log_event(f"{player.name} casts {half_name} (split half of {card.name})")
        return True, f"Cast {half_name}", effect_messages

    # Handle Adventure casting — adventure half resolves like a sorcery,
    # then card goes to exile (can be cast as creature from exile later)
    if card.cast_as_adventure and card.adventure_text:
        print(f"[ADVENTURE] Casting {card.adventure_name} (adventure of {card.name})")
        # Resolve the adventure effect using the tier cascade
        adventure_card = Card(
            name=card.adventure_name,
            mana_cost=card.adventure_cost,
            type_line=card.adventure_type,
            oracle_text=card.adventure_text,
        )
        adv_msgs = engine.resolve_special_effects(game, player, adventure_card, target)
        # Tier 1.5: try template library on the adventure name (Welcome Home,
        # Oaken Boon, etc.) — without this, adventure halves leaked
        # "Complex effect:" text without actually creating tokens / counters.
        if not adv_msgs:
            try:
                # Module-level import at line 74 already provides these names.
                # A redundant local import here would shadow the module-level
                # binding for the entire function (Python scope analysis pre-binds
                # the names as local), causing UnboundLocalError on every other
                # use site in cast_spell_async. See Apr 29 audit Bug #1.
                opp_idx_adv = 1 - (game.players.index(player) if player in game.players else 0)
                opp_adv = game.players[opp_idx_adv]
                lib_adv = get_effect_library()
                ctx_adv = build_game_context(game, player, opp_adv, card=adventure_card)
                adv_actions, adv_desc = lib_adv.resolve_etb(
                    card_name=card.adventure_name,
                    oracle_text=card.adventure_text or '',
                    controller=player.name,
                    opponent=opp_adv.name,
                    game_context=ctx_adv,
                )
                if adv_actions:
                    for a_act in adv_actions:
                        if a_act.get('action') == 'no_action':
                            continue
                        try:
                            a_msg = engine.rules._execute_action_on_state(game, a_act)
                            if a_msg:
                                adv_msgs.append(a_msg)
                        except Exception as e:
                            print(f"[ADVENTURE-TEMPLATE] Action failed for {card.adventure_name}: {e}")
                    if adv_msgs:
                        print(f"[ADVENTURE-TEMPLATE] Resolved {card.adventure_name}: {adv_desc}")
            except Exception as e:
                print(f"[ADVENTURE-TEMPLATE] Lookup failed for {card.adventure_name}: {e}")
        if not adv_msgs and engine.spell_resolver and card.adventure_text:
            try:
                result = await engine.spell_resolver.cast_spell(
                    game, player, adventure_card, target=target, target_mode=TargetMode.AUTO
                )
                adv_msgs = result.messages
            except Exception as e:
                print(f"[ADVENTURE] SpellResolver error for {card.adventure_name}: {e}")
        if not adv_msgs:
            # Fallback: try resolve_effect via Claude API
            if engine.rules.client:
                try:
                    effect_msgs, _ = await engine.rules.resolve_effect(
                        game, card.adventure_text,
                        card.adventure_name, player.name
                    )
                    if effect_msgs:
                        adv_msgs.extend(effect_msgs)
                except Exception as e:
                    print(f"[ADVENTURE] resolve_effect error for {card.adventure_name}: {e}")
        if not adv_msgs:
            _at = (card.adventure_text or '').replace('\n', ' ').strip()
            if len(_at) > 500:
                _at = _at[:497].rstrip() + '…'
            adv_msgs = [f"🧙 **{card.adventure_name}** resolves — {_at}"]
        effect_messages.extend(adv_msgs)
        # Adventure cards go to exile after the adventure resolves, and CR 715.3
        # lets the owner cast the CREATURE half from exile for as long as it
        # stays there. Nothing marked it castable, and every exile-cast gate
        # tests membership of `playable_from_exile`, so the creature half — the
        # entire point of the mechanic and of the adventure test deck — could
        # never be cast. Deliberately NOT reusing playable_from_exile: end_turn
        # clears that list every turn, while adventure castability persists.
        player.exile.append(card)
        card._adventure_exiled = True
        card.cast_as_adventure = False  # Reset flag
        engine.rules.log_event(f"{player.name} casts {card.adventure_name} (adventure of {card.name})")
        return True, f"Cast {card.adventure_name}", effect_messages

    # July 24 batch-6 anomaly (reviewer A1 #5, root-caused): the PERMANENT
    # branch's Tier-1 rebind (effect_messages = resolve_special_effects(...))
    # discarded everything already appended — the cast-trigger draw message
    # ("⚡ Sram — Claude draws a card") AND the aura's own "✨ enchants" line
    # both vanished whenever a permanent spell had self-ETB text (state was
    # correct; display only). Saved in the permanent branch below, restored
    # at the shared tail. The instant/sorcery branch has had its own
    # save/restore since May (cast_trigger_msgs_saved) and is unaffected.
    _perm_msgs_saved = None

    if card.is_instant() or card.is_sorcery():
        # [TARGETING] Set resolution source context so _execute_action_on_state
        # can enforce hexproof/protection/shroud on targeted actions.
        game._current_resolution_source = (card.name, player.name)

        # Preserve cast trigger messages (Eidolon, Rhystic Study, etc.)
        # before resolution replaces effect_messages
        cast_trigger_msgs_saved = list(effect_messages)

        # Tier 1: Hardcoded special effects (mass pump, ramp, delayed triggers)
        effect_messages = engine.resolve_special_effects(game, player, card, target)

        # Bug fix: mark spell as resolved by Tier 1 to prevent duplicate resolution
        # (Cultivate was resolving twice — once in resolve_special_effects and again in SpellResolver)
        if effect_messages:
            card._spell_resolved = True
            print(f"[SPELL-TIER1] {card.name} resolved via resolve_special_effects")

        # Tier 1.5: Effect template library (card name + oracle pattern matching)
        if not effect_messages and not getattr(card, '_spell_resolved', False) and HAS_EFFECT_TEMPLATES and card.oracle_text:
            try:
                player_idx = game.players.index(player) if player in game.players else 0
                opponent = game.players[1 - player_idx]
                ctx = build_game_context(game, player, opponent, card=card, explicit_target=target)
                # Apr 30 audit fix #21: pass modal mode selection to templates
                if getattr(card, '_modes_chosen', None):
                    ctx['_modes'] = card._modes_chosen
                lib = get_effect_library()
                tmpl_actions, tmpl_explanation = lib.resolve_spell(
                    card_name=card.name,
                    oracle_text=card.oracle_text,
                    controller=player.name,
                    opponent=opponent.name,
                    game_context=ctx,
                )
                if tmpl_actions is not None:
                    spell_fizzled = False
                    actions_executed = 0  # Apr 30 audit: track action runs separately
                    for action in tmpl_actions:
                        # If a counter_spell fizzled, skip remaining actions
                        # (Arcane Denial shouldn't draw when counter has no target)
                        if spell_fizzled:
                            print(f"[SPELL-TEMPLATE] Skipping {action.get('action')} — spell fizzled")
                            break
                        if action.get("action") != "no_action":
                            # May 7 audit fix #6: inject source card name into
                            # the action so deal_damage can emit per-spell burn
                            # lines (🔥 Lava Spike deals 3 damage to Claude).
                            # Without this, burn spells just silently mutate life
                            # totals — auditors can't trace per-spell events.
                            if 'source' not in action and '_source_card_name' not in action:
                                action['_source_card_name'] = card.name
                                action['_source_controller'] = player.name
                                action['_source_oracle'] = card.oracle_text or ''
                                # The deal_damage handler reads `source` first;
                                # set it too for templates that don't supply one.
                                if action.get('action') == 'deal_damage' and not action.get('source'):
                                    action['source'] = card.name
                            # July 24 batch-6 audit (reviewer S2): counter
                            # templates hardcode "stack_top", so a counter
                            # resolving late acted on whatever spell happened
                            # to be top (a stale Spell Pierce extracted a {2}
                            # payment from An Offer You Can't Refuse, which
                            # has no unless-clause at all). Thread the
                            # DECLARED target through; the handlers fizzle
                            # per CR 608.2b when it has left the stack.
                            if (action.get('action') in ('counter_spell',
                                                         'counter_unless_pays',
                                                         'counter_ability')
                                    and not action.get('target_name')
                                    and getattr(target, 'name', None)):
                                action['target_name'] = target.name
                            try:
                                msg = engine.rules._execute_action_on_state(game, action)
                                actions_executed += 1
                                if msg:
                                    effect_messages.append(msg)
                                    if 'fizzle' in msg.lower():
                                        spell_fizzled = True
                            except Exception as ae:
                                print(f"[SPELL-TEMPLATE] Action failed: {ae}")
                        else:
                            reason = action.get("reason", "")
                            if reason:
                                # May 7 audit fix #7: skip internal-diagnostic
                                # reasons (no player context, no _foo, etc.) —
                                # these are template-runtime errors, not
                                # things the player can act on.
                                _r_lower = reason.lower()
                                _is_internal = any(marker in _r_lower for marker in (
                                    "no player context", "no _player",
                                    "no context", "context missing",
                                ))
                                if not _is_internal:
                                    effect_messages.append(f"📋 {reason}")
                                else:
                                    print(f"[SPELL-TEMPLATE] Suppressed internal-diagnostic reason: {reason}")
                    # Apr 30 audit fix: even when an action runs silently (flicker
                    # dedup, etc.), the spell DID resolve — don't fall through to
                    # Tier 2/3 and emit the misleading "no automatic state change"
                    # message. Ephemerate had 5 silent-resolution hits in the audit
                    # because the flicker action produced no message on duplicate
                    # flickers, and the spell pipeline interpreted that as "tier 1.5
                    # didn't handle this." If actions executed without error, we mark
                    # the spell resolved AND emit a minimal "Ephemerate resolves"
                    # line so players know the spell completed.
                    if effect_messages:
                        card._spell_resolved = True
                        # May 7 audit fix #8: distinguish "Resolved" from
                        # "Conditional not met". If every action that ran was
                        # a no_action (Inventors' Fair without 3 artifacts),
                        # log the failed condition instead of "Resolved".
                        _any_real = any(
                            a.get("action") != "no_action" for a in tmpl_actions
                        )
                        if _any_real:
                            print(f"[SPELL-TEMPLATE] {card.name} resolved via template library: {tmpl_explanation}")
                        else:
                            _cond_reason = next(
                                (a.get("reason", "") for a in tmpl_actions
                                 if a.get("action") == "no_action" and a.get("reason")),
                                ""
                            )
                            print(f"[SPELL-TEMPLATE] Conditional not met for {card.name}: {_cond_reason or tmpl_explanation}")
                        if not hasattr(game, '_recently_resolved_spells'):
                            game._recently_resolved_spells = set()
                        game._recently_resolved_spells.add(card.name)
                    elif actions_executed > 0:
                        # Template ran actions but they produced no visible message
                        # (flicker dedup, no-op fall-through). Spell DID resolve.
                        # May 24 audit fix: suppress the "(no further state change)"
                        # Discord post entirely — it's developer-language and the
                        # cast event itself was already announced. Just mark the
                        # spell resolved internally. Console line preserved so
                        # audits can still grep [SPELL-TEMPLATE] activity.
                        card._spell_resolved = True
                        print(f"[SPELL-TEMPLATE] {card.name} resolved silently (actions ran but emitted no message)")
                        if not hasattr(game, '_recently_resolved_spells'):
                            game._recently_resolved_spells = set()
                        game._recently_resolved_spells.add(card.name)
            except Exception as e:
                print(f"[SPELL-TEMPLATE] Error for {card.name}: {e}")

        # Tier 2: SpellResolver (regex-based oracle text parsing)
        has_complex_effect = False
        if not effect_messages and not getattr(card, '_spell_resolved', False) and engine.spell_resolver and card.oracle_text:
            try:
                result = await engine.spell_resolver.cast_spell(
                    game, player, card, target=target, target_mode=TargetMode.AUTO
                )
                effect_messages = result.messages
                # Check if SpellResolver punted to "complex effect" marker (meaning it couldn't parse).
                # Match case-insensitively so the suffix marker (lowercase "_(complex effect, ...)_")
                # still triggers Tier 3 escalation while keeping the user-visible text clean.
                has_complex_effect = any("complex effect" in m.lower() for m in effect_messages)
            except Exception as e:
                print(f"[SPELL_RESOLVER] Error resolving {card.name}: {e}")
                # June 10 audit (C4): this barrier masked an UnboundLocalError
                # in spell_resolver's damage path for weeks — the Tier 3
                # "recovery" RE-RESOLVED the spell after state had already
                # been mutated (one Bolt, two effects). Strict batches now see
                # the underlying exception instead of the double-resolution.
                from mtg.util import maybe_reraise
                maybe_reraise(e)
                has_complex_effect = True  # Fallback to Tier 3
        elif not effect_messages:
            has_complex_effect = True  # No resolver available, try Tier 3

        # Tier 3: Claude API fallback for complex/unparseable instant/sorcery effects
        if has_complex_effect and card.oracle_text and not getattr(card, '_spell_resolved', False):
            try:
                print(f"[SPELL-TIER3] Using resolve_effect for {card.name}")
                # Include X value in context for X-cost spells so Claude knows what X is
                spell_context = f"{card.name} was just cast as a spell"
                if hasattr(card, '_x_value') and card._x_value is not None and card._x_value > 0:
                    spell_context += f". X was determined to be {card._x_value}."
                elif hasattr(card, '_mana_paid') and card._mana_paid and 'X' in (card.mana_cost or ''):
                    # Calculate X from mana paid if _x_value wasn't set
                    import re as _re
                    colored = sum(1 for c in (card.mana_cost or '') if c in 'WUBRGC' and c != 'X')
                    generic = sum(int(m) for m in _re.findall(r'\{(\d+)\}', card.mana_cost or ''))
                    x_count = (card.mana_cost or '').count('X')
                    if x_count > 0:
                        x_val = (card._mana_paid - colored - generic) // x_count
                        spell_context += f". X was determined to be {max(0, x_val)}."
                resolve_msgs, resolve_actions = await engine.rules.resolve_effect(
                    game,
                    effect_description=card.oracle_text,
                    source_card=card.name,
                    controller=player.name,
                    context=spell_context
                )
                if resolve_actions:
                    effect_messages = resolve_msgs
                    print(f"[SPELL-TIER3] {card.name} resolved via Claude API: {len(resolve_actions)} actions")
                    # Track resolved spell to prevent AI double-resolution
                    if not hasattr(game, '_recently_resolved_spells'):
                        game._recently_resolved_spells = set()
                    game._recently_resolved_spells.add(card.name)
                else:
                    print(f"[SPELL-TIER3] {card.name}: Claude API returned no actions")
                    # [AUTOPLAY-JUDGE] Mirror the console suppression on Discord side:
                    # if Tier 3 produced no state change, strip the raw "complex effect"
                    # placeholder that SpellResolver emitted so players don't see a bare
                    # oracle-text line with no outcome. Case-insensitive so the lowercase
                    # marker (_(complex effect, ...)_) is also stripped.
                    effect_messages = [m for m in (effect_messages or []) if "complex effect" not in m.lower()]
                    if not effect_messages:
                        # May 17 audit: previous "effect could not be resolved
                        # automatically — no state change" line was internal
                        # scaffolding leaking to players (Telling Time and other
                        # library-look effects fell here). Use a natural
                        # "resolves" form and only mention the unmodeled aspect
                        # for library-look effects.
                        oracle_low = (card.oracle_text or "").lower()
                        if any(p in oracle_low for p in (
                            "scry ", "look at the top", "look at the next",
                            "reveal the top", "rearrange them in any order",
                        )):
                            effect_messages = [
                                f"🔮 **{card.name}** resolves — library reordering "
                                f"is not modeled by the engine."
                            ]
                        else:
                            effect_messages = [f"✨ **{card.name}** resolves."]
            except Exception as e:
                print(f"[SPELL-TIER3] Error resolving {card.name}: {e}")

        if not effect_messages:
            # Include oracle text so players can see what the spell actually does
            oracle_preview = ""
            if card.oracle_text:
                # Strip reminder text in parens, then strip * chars so they don't
                # break the italic markdown wrapper (Increasing Vengeance has *(This...*))
                clean_oracle = re.sub(r'\([^)]*\)', '', card.oracle_text).strip()
                clean_oracle = clean_oracle.replace('*', '')
                # Strip trailing standalone keyword tokens (Rebound, Flashback,
                # Cycling, etc.) — they're ability words / static keyword cues,
                # not part of the spell's effect text.
                clean_oracle = re.sub(
                    r'\.\s*(?:Rebound|Flashback|Cycling|Echo|Buyback|Storm|Madness|Suspend|Dredge|Convoke|Delve|Improvise)\s*$',
                    '.', clean_oracle, flags=re.IGNORECASE).strip()
                # Use textwrap.shorten to truncate at a word boundary (avoids mid-word cuts)
                import textwrap as _textwrap
                clean_oracle = _textwrap.shorten(clean_oracle, width=300, placeholder="...")
                oracle_preview = f": *{clean_oracle}*" if clean_oracle else ""
            # The engine didn't generate any state-change actions for this spell.
            # Most commonly: a complex effect that escalated to Tier 3 with an
            # explanation but no JSON actions (e.g. modal spells where the
            # judge can't pick a mode). Keep it player-readable — emit just
            # "resolves" + oracle preview; don't surface internal scaffolding.
            effect_messages = [f"🧙 **{card.name}** resolves{oracle_preview}"]

        # Restore cast trigger messages (Eidolon damage, Rhystic Study draw, etc.)
        # that were dropped when effect_messages was reassigned during resolution
        if cast_trigger_msgs_saved:
            effect_messages = cast_trigger_msgs_saved + effect_messages

        # Goes to graveyard after resolving (or command zone for signature spells,
        # or exile for rebound spells with upkeep re-cast trigger)
        oracle_lower = (card.oracle_text or '').lower()
        if getattr(card, 'is_signature_spell', False) and game.format == "oathbreaker":
            player.command_zone.append(card)
            effect_messages.append(f"📜 {card.name} returns to command zone (signature spell)")
            print(f"[OATHBREAKER] {card.name} resolved → returns to command zone")
        elif getattr(card, '_resolving_flashback', False):
            # CR 702.34a: flashbacked spells exile instead of going to the
            # graveyard (June 11 audit — flashback casts previously either
            # failed outright or would have recycled into the graveyard).
            card._resolving_flashback = False
            player.exile.append(card)
            effect_messages.append(f"📤 {card.name} is exiled (flashback)")
            print(f"[FLASHBACK] {card.name} resolved → exiled (CR 702.34a)")
        elif 'rebound' in oracle_lower and not getattr(card, '_from_rebound', False):
            # Rebound: exile instead of graveyard, recast at next upkeep
            player.exile.append(card)
            player_idx = game.players.index(player) if player in game.players else 0
            # Schedule delayed trigger to recast at next upkeep
            game.delayed_triggers.append({
                "trigger_at": "upkeep",
                "source": f"{card.name} (rebound)",
                "controller": player_idx,
                "once": True,
                "turn_delay": 0,
                # Rebound recasts at the CASTER'S next upkeep (CR 702.87a);
                # without upkeep_of it fired on the opponent's upkeep instead.
                "upkeep_of": player_idx,
                "actions": [
                    {"action": "rebound_cast", "card": card.name, "player": player.name}
                ],
            })
            effect_messages.append(f"🔄 {card.name} exiled with rebound — will recast at your next upkeep")
            print(f"[REBOUND] {card.name} exiled, scheduled for next upkeep")
        elif f"shuffle {card.name.lower()} into its owner's library" in oracle_lower:
            player.library.append(card)
            random.shuffle(player.library)
            effect_messages.append(f"🔀 {card.name} is shuffled into its owner's library")
        elif getattr(card, 'is_signature_spell', False) and game.format == "oathbreaker":
            # CR Oathbreaker: Signature spell returns to command zone on resolve.
            if not hasattr(player, 'command_zone') or player.command_zone is None:
                player.command_zone = []
            player.command_zone.append(card)
            effect_messages.append(f"👑 {card.name} returns to command zone")
            print(f"[OATHBREAKER] {card.name} resolved → returns to command zone")
        else:
            player.graveyard.append(card)
        engine.rules.log_event(f"{player.name} casts {card.name} (instant/sorcery)")
        # [TARGETING] Clear resolution source context
        game._current_resolution_source = None
    else:
        # Permanent goes to battlefield — reset stale state from previous zone
        # (e.g. commander recast from command zone still had damage_marked from last death)
        card.reset_battlefield_state()
        # Only creatures get summoning sickness (planeswalkers, artifacts, enchantments don't)
        card.summoning_sick = True if card.is_creature() else False
        card.entered_this_turn = True
        player.battlefield.append(card)
        engine.rules.log_event(f"{player.name} casts {card.name} (permanent)")
        # (Pub/sub slice 2: the PERMANENT_ENTERED emit for the cast path
        # lives further down, next to the watcher-scan dispatch — NOT here
        # at the raw append. Today's-batch parity lines proved the append is
        # too early: a fizzled aura appends-then-leaves (no entry per CR),
        # and a clone's characteristics apply after the append, so kind was
        # recorded pre-copy — Clever Impersonator copying devotion-gated
        # Thassa was flagged as a missed creature scan.)

        if card.name.lower() == 'wishclaw talisman':
            card.counters['wish'] = 3
            effect_messages.append("🔮 Wishclaw Talisman enters with three wish counters")

        # Escape rider (CR 702.139e): "This creature escapes with N +1/+1
        # counters on it." Applied AFTER reset_battlefield_state (which
        # clears counters) and only when this cast actually escaped —
        # batch 15315's first live escape (Woe Strider) entered as a bare
        # 3/2 with the printed two-counter rider silently dropped.
        if getattr(card, '_was_escaped', False):
            _esc_counters = helpers.parse_escapes_with_counters(card.oracle_text)
            if _esc_counters:
                card.counters['+1/+1'] = card.counters.get('+1/+1', 0) + _esc_counters
                effect_messages.append(
                    f"⭕ **{card.name}** escapes with {_esc_counters} +1/+1 counter(s)")
                print(f"[ESCAPE] {card.name} escapes with {_esc_counters} +1/+1 counter(s) (CR 702.139e)")

        # [SAGA] Sagas get their first lore counter on ETB (CR 714.3a)
        if 'saga' in (card.type_line or '').lower():
            if not hasattr(card, 'counters') or card.counters is None:
                card.counters = {}
            card.counters['lore'] = 1
            print(f"[SAGA] {card.name} enters with lore counter 1")
            chapter_text = engine._get_saga_chapter_text(card, 1)
            if chapter_text:
                effect_messages.append(f"📖 **{card.name}** — Chapter I: *{chapter_text[:200]}*")
                # May 20 audit: previously this path ONLY tried the template
                # library — if no template matched, chapter I silently failed.
                # game_1506623303794561024 had Fall of the Thran chapter I
                # ("Destroy all lands.") never fire because no template exists
                # AND no Tier 3 fallback was wired here. The progression path
                # at spells.py:_progress_sagas already has the template +
                # Tier 3 fallback — mirror that pattern for the ETB path.
                if HAS_EFFECT_TEMPLATES:
                    template_fired = False
                    try:
                        lib = get_effect_library()
                        opp_idx = 1 - game.players.index(player)
                        opp = game.players[opp_idx]
                        # June 10 round 3: saga chapters used to call the
                        # library with NO game_context, so generators needing
                        # board state (chapter II "+1/+1 counter on target
                        # creature you control") had nothing to target with.
                        _saga_ctx = build_game_context(game, player, opp, card=card)
                        actions, desc = lib.resolve_etb(card.name, chapter_text, player.name, opp.name,
                                                        game_context=_saga_ctx)
                        if actions:
                            for act in actions:
                                result = engine.rules._execute_action_on_state(game, act)
                                if result:
                                    effect_messages.append(f"  {result}")
                            print(f"[SAGA-CHAPTER] Resolved {card.name} chapter I via template")
                            template_fired = True
                    except Exception as e:
                        print(f"[SAGA-CHAPTER] Template error: {e}")
                    # Tier 3 fallback when no template matched.
                    if not template_fired:
                        try:
                            engine._queue_async_trigger(
                                game, card, chapter_text, "saga_chapter_1",
                                player.name,
                                context=f"{card.name} Chapter I (ETB) of {engine._get_saga_total_chapters(card)}",
                            )
                            print(f"[SAGA-CHAPTER] Queued {card.name} chapter I for Tier 3 (no template)")
                        except Exception as e:
                            print(f"[SAGA-CHAPTER] Tier 3 queue error: {e}")

        # [CLONE] Handle "enters as a copy" replacement effects (Clone, Phantasmal Image,
        # Spark Double, Clever Impersonator). Must happen before ETB triggers.
        if card.is_creature() and card.oracle_text:
            clone_oracle = card.oracle_text.lower()
            is_clone = (
                "enters the battlefield as a copy" in clone_oracle or
                "enters as a copy" in clone_oracle or
                ("you may have" in clone_oracle and "enter as a copy" in clone_oracle)
            )
            if is_clone:
                copy_target = None
                # [CLONE] Honor the AI-specified target first if it resolves to a
                # legal copy choice on the battlefield. Falls back to
                # the best-power heuristic only if no target was given or it can't
                # be found. Applies to Clone, Spark Double, Phantasmal Image,
                # Body Double, Phyrexian Metamorph, Sakashima the Impostor, etc.
                target_name = None
                if target is not None:
                    if hasattr(target, 'name'):
                        target_name = target.name
                    elif isinstance(target, str):
                        target_name = target
                    elif isinstance(target, dict) and target.get('name'):
                        target_name = target['name']
                if target_name:
                    for sp in game.players:
                        for c in sp.battlefield:
                            if (c.id == card.id
                                    or not _clone_target_is_legal(card, c, player, sp)):
                                continue
                            if c.name.lower() == target_name.lower():
                                copy_target = c
                                break
                        if copy_target:
                            break
                    if copy_target:
                        print(f"[CLONE] Honoring AI target '{target_name}' for {card.name}")
                    else:
                        print(f"[CLONE] AI target '{target_name}' not found on battlefield for {card.name} — falling back")
                if not copy_target:
                    best_value = -1
                    for sp in game.players:
                        for c in sp.battlefield:
                            if (c.id == card.id
                                    or not _clone_target_is_legal(card, c, player, sp)):
                                continue
                            try:
                                p_val = c.get_effective_power(game) if hasattr(c, 'get_effective_power') else (int(c.power) if c.power else 0)
                                t_val = c.get_effective_toughness(game) if hasattr(c, 'get_effective_toughness') else (int(c.toughness) if c.toughness else 0)
                            except (ValueError, TypeError):
                                p_val, t_val = 0, 0
                            if p_val + t_val > best_value:
                                best_value = p_val + t_val
                                copy_target = c
                if copy_target:
                    original_name = _apply_clone_characteristics(card, copy_target)
                    effect_messages.append(f"🪞 {original_name} enters as a copy of {copy_target.name}!")
                    print(f"[CLONE] {original_name} copies {copy_target.name} ({copy_target.power}/{copy_target.toughness})")
                else:
                    print(f"[CLONE] {card.name} found no creature to copy — enters as 0/0")

        # [TRANSFORM] Activate day/night cycle if a daybound card enters
        dn_msgs = engine._activate_day_night_if_needed(game, card)
        effect_messages.extend(dn_msgs)

        # [ETB-TAPPED] Check if permanent enters tapped (oracle text + replacement effects)
        if not card.is_land():
            enters_tapped, etb_msg = engine.rules._check_enters_tapped(game, card, player)
            if enters_tapped:
                card.tapped = True
                effect_messages.append(f"⏳ {card.name} enters the battlefield tapped{etb_msg}")

        # Handle Aura attachment
        if card.is_enchantment() and card.oracle_text:
            oracle_lower = card.oracle_text.lower()

            # Bug #9: Animate Dead / Dance of the Dead / Necromancy — target graveyard, not battlefield
            reanimate_auras = ["animate dead", "dance of the dead", "necromancy"]
            if ("enchant creature card in a graveyard" in oracle_lower or
                card.name.lower() in reanimate_auras):
                # Honor the target declared at cast time. Only targetless
                # autoplay/manual fallbacks use the best-power heuristic.
                target_card, target_player = None, None
                declared_id = getattr(card, '_declared_graveyard_target_id', None)
                declared_name = getattr(target, 'name', target) if target is not None else None
                best_power = -1
                for p in game.players:
                    for c in p.graveyard:
                        is_declared = (
                            declared_id and c.id == declared_id
                        ) or (
                            not declared_id and isinstance(declared_name, str)
                            and c.name.lower() == declared_name.lower()
                        )
                        if c.is_creature() and (target is None or is_declared):
                            try:
                                p_val = c.get_effective_power(game) if hasattr(c, 'get_effective_power') else 0
                            except (ValueError, TypeError):
                                p_val = int(c.power) if c.power and str(c.power).lstrip('-').isdigit() else 0
                            if is_declared or p_val > best_power:
                                best_power, target_card, target_player = p_val, c, p
                                if is_declared:
                                    break
                    if target_card and target is not None:
                        break
                if target_card and target_player:
                    target_player.graveyard.remove(target_card)
                    target_card.reset_battlefield_state()
                    target_card.entered_this_turn = True
                    player.battlefield.append(target_card)
                    # Attach aura to the reanimated creature
                    card.attached_to = target_card.id
                    if not hasattr(target_card, 'attachments'):
                        target_card.attachments = []
                    target_card.attachments.append(card.id)
                    # May 14 audit (L4): set the two-way binding so the SBA
                    # cleanup at sba.py:251 ([REANIMATE-AURA]) can sacrifice
                    # the reanimated creature when the binding aura leaves.
                    # Without this, Animate Dead → graveyard would leave the
                    # creature on battlefield permanently — and the LTB trigger
                    # display would say "sacrifice it" without anything
                    # actually being sacrificed.
                    card._bound_creature_id = target_card.id
                    target_card._reanimated_by_aura_id = card.id
                    # June 10 audit (C8): mark the reanimation as handled so the
                    # name-keyed ETB template doesn't run a SECOND one — one
                    # Animate Dead cast was returning two creatures (the inline
                    # best-power pick here + the template's best-CMC pick).
                    card._reanimate_handled = True
                    print(f"[REANIMATE-BIND] {card.name} bound to {target_card.name}")
                    effect_messages.append(f"💀 {card.name} reanimates {target_card.name}!")
                    print(f"[REANIMATE] {card.name} brought back {target_card.name} from {target_player.name}'s graveyard")
                    # Slice 2b (July 21): this inline aura-reanimation path
                    # never emitted PERMANENT_ENTERED (the actions.py
                    # reanimate handler does) — emit now drives the watcher
                    # dispatch, then drain in place.
                    events.emit(events.PERMANENT_ENTERED, game, card=target_card,
                                controller=player, via="reanimate",
                                rules=engine.rules)
                    effect_messages.extend(helpers.drain_pending_messages(game))
                else:
                    effect_messages.append(f"💨 {card.name} fizzles (no creatures in any graveyard)")
                    if card in player.battlefield:
                        player.battlefield.remove(card)
                    player.graveyard.append(card)
                # Skip normal aura attachment — we handled it
                pass

            elif re.search(r'enchant (land|forest|plains|island|swamp|mountain)\b', oracle_lower):
                # June 10 deep-dive (B8): land-auras had NO attach branch —
                # Wild Growth resolved unattached and the AURA_INVALID SBA
                # destroyed it on the spot (mana spent, card lost) despite
                # the AI naming a legal target. Attach to the named land or
                # the controller's first matching land. (The aura's
                # extra-mana production half is not yet modeled — surviving
                # attachment is the prerequisite.)
                _lsub_m = re.search(r'enchant (forest|plains|island|swamp|mountain)\b', oracle_lower)
                _lsub = _lsub_m.group(1) if _lsub_m else None
                _named_land = target if isinstance(target, str) else None
                _target_land = target if (isinstance(target, Card) and target.is_land()) else None
                if _target_land is None:
                    for _lc in player.battlefield:
                        if not _lc.is_land():
                            continue
                        _ltl = (_lc.type_line or '').lower()
                        if _lsub and _lsub not in _ltl:
                            continue
                        if _named_land and _named_land.lower() not in _lc.name.lower():
                            continue
                        _target_land = _lc
                        break
                    if _target_land is None and _named_land:
                        for _lc in player.battlefield:
                            if _lc.is_land() and (not _lsub or _lsub in (_lc.type_line or '').lower()):
                                _target_land = _lc
                                break
                if _target_land is not None:
                    card.attached_to = _target_land.id
                    if not hasattr(_target_land, 'attachments'):
                        _target_land.attachments = []
                    _target_land.attachments.append(card.id)
                    effect_messages.append(f"🌿 {card.name} enchants {_target_land.name}")
                    print(f"[AURA-LAND] {card.name} attached to {_target_land.name}")
                else:
                    effect_messages.append(f"💨 {card.name} fizzles (no legal land to enchant)")

            elif "enchant creature" in oracle_lower or "enchant permanent" in oracle_lower:
                # This is an Aura - attach to target
                target_card = None
                # [AURA-TARGET] Resolve target from AI action — accept Card, Player
                # (reject), string name, or dict with a 'name' key. Honor the AI's
                # choice over the auto-select heuristic.
                if target is not None:
                    resolved_name = None
                    if isinstance(target, Card):
                        target_card = target
                    elif isinstance(target, str):
                        resolved_name = target
                    elif isinstance(target, dict) and target.get('name'):
                        resolved_name = target['name']
                    elif hasattr(target, 'name') and not isinstance(target, Player):
                        resolved_name = target.name
                    if resolved_name and not target_card:
                        for p in game.players:
                            for c in p.battlefield:
                                if c.id == card.id:
                                    continue
                                if c.name.lower() == resolved_name.lower() or c.id == resolved_name:
                                    target_card = c
                                    break
                            if target_card:
                                break
                    # Validate AI-chosen target against oracle restriction + hexproof/ward
                    if target_card:
                        is_creature_only_check = "enchant creature" in oracle_lower
                        if is_creature_only_check and not target_card.is_creature():
                            print(f"[AURA-TARGET] AI target {target_card.name} is not a creature — falling back to auto-select")
                            target_card = None
                        # July 20 audit (Faith Unbroken): "Enchant creature
                        # YOU CONTROL" — the honoring path attached the aura
                        # to the OPPONENT's creature (the AI's single target
                        # was really the ETB-exile target). Enforce the
                        # controller restriction; auto-select then picks a
                        # legal own-side bearer.
                        elif ('enchant creature you control' in oracle_lower
                                and target_card not in player.battlefield):
                            print(f"[AURA-TARGET] AI target {target_card.name} "
                                  f"isn't controlled by {player.name} "
                                  f"(enchant creature you control) — falling "
                                  f"back to auto-select")
                            target_card = None
                        elif HAS_TARGETING:
                            # Find the target's controller for validation
                            target_controller = None
                            for p in game.players:
                                if target_card in p.battlefield:
                                    target_controller = p
                                    break
                            if target_controller:
                                legal, reason = _validate_target_for_action(
                                    game, target_card, target_controller, card, player.name)
                                if not legal:
                                    print(f"[AURA-TARGET] AI target {target_card.name} illegal ({reason}) — falling back")
                                    target_card = None
                    if target_card:
                        print(f"[AURA-TARGET] Honoring AI target {target_card.name} for {card.name}")

                if not target_card:
                    # No target provided — auto-select from valid targets
                    # "enchant creature" targets creatures; "enchant permanent" targets any permanent
                    is_creature_only = "enchant creature" in oracle_lower
                    valid_targets = []
                    for p in game.players:
                        for c in p.battlefield:
                            if c.id == card.id:
                                continue  # Skip the aura itself
                            if is_creature_only and not c.is_creature():
                                continue
                            # Full targeting validation (hexproof, shroud, protection, ward)
                            if HAS_TARGETING:
                                legal, reason = _validate_target_for_action(
                                    game, c, p, card, player.name)
                                if not legal:
                                    print(f"[AURA-TARGET] {card.name} cannot enchant {c.name}: {reason}")
                                    continue
                            else:
                                # Fallback: basic hexproof/shroud check
                                if c not in player.battlefield:
                                    if c.has_keyword("Hexproof") or c.has_keyword("Shroud"):
                                        continue
                            valid_targets.append(c)

                    print(f"[AURA] {card.name}: creature_only={is_creature_only}, valid_targets={[c.name for c in valid_targets]}")

                    # May 20 audit: hoist beneficial/detrimental classification out
                    # of the `if valid_targets:` block so the fizzle-hint code path
                    # downstream can see it even when valid_targets was empty.
                    detrimental_cues = (
                        "can't attack", "can't block", "can't be activated",
                        "doesn't untap", "loses all abilities", "loses flying",
                        "is a creature with base power and toughness",
                        "gets -",
                    )
                    is_detrimental = any(cue in oracle_lower for cue in detrimental_cues)

                    if valid_targets:
                        own_creatures = [c for c in valid_targets if c in player.battlefield]
                        opp_creatures = [c for c in valid_targets if c not in player.battlefield]
                        if is_detrimental and opp_creatures:
                            # July 24 batch-6 (reviewer D1): despite the name,
                            # opp_creatures is "opponent's legal permanents in
                            # enumeration order" for enchant-PERMANENT auras —
                            # Faith's Fetters auto-picked a basic Swamp (a
                            # total non-effect) while the two creatures dealing
                            # lethal every turn sat in the same list
                            # (game_1529985418743910420). Prefer real creatures
                            # (biggest threat first), then nonland permanents;
                            # a land only as last resort.
                            _real_creatures = [c for c in opp_creatures
                                               if c.is_creature(game)]
                            if _real_creatures:
                                target_card = max(
                                    _real_creatures,
                                    key=lambda c: c.get_effective_power(game)
                                    if hasattr(c, 'get_effective_power') else 0)
                            else:
                                _nonlands = [c for c in opp_creatures
                                             if not c.is_land()]
                                target_card = (_nonlands[0] if _nonlands
                                               else opp_creatures[0])
                        elif is_detrimental and not opp_creatures:
                            # Detrimental aura with no opponent creature — fizzle
                            # rather than punish caster.
                            print(f"[AURA] {card.name} is detrimental but no opponent creature legal — fizzling")
                            target_card = None
                        elif own_creatures:
                            target_card = own_creatures[0]
                        else:
                            # Beneficial aura but only opponent creatures legal —
                            # fizzle rather than buff opponent.
                            print(f"[AURA] {card.name} is beneficial but only opponent targets — fizzling")
                            target_card = None
                            # July 24 batch-6 (reviewer D1, MINOR): the fizzle
                            # message downstream said "no creature you control
                            # on battlefield", hiding that a legal target WAS
                            # found and declined strategically (Bear Umbra vs
                            # an opponent-only board).
                            game._aura_fizzle_note = (
                                "declined — the only legal targets are "
                                "opponent-controlled")
                        if target_card:
                            print(f"[AURA] Auto-selected target {target_card.name} for {card.name} (detrimental={is_detrimental})")

                if target_card:
                    card.attached_to = target_card.id
                    target_card.attachments.append(card.id)
                    effect_messages.append(f"✨ {card.name} enchants {target_card.name}")
                else:
                    # Genuinely no valid targets on battlefield - fizzle
                    player.battlefield.remove(card)
                    player.graveyard.append(card)
                    # May 20 audit: the hint must respect the same beneficial/detrimental
                    # split that drove the fizzle. Suggesting opponent's commander for a
                    # beneficial aura ("Try !play Ethereal Armor target Thassa") is a UX
                    # bug because the play would either fizzle again (filtered by the
                    # same beneficial-aura branch) or buff opponent if user forces it.
                    own_creature_names = [c.name for c in player.battlefield if c.is_creature()]
                    opp_creature_names = [
                        c.name for p in game.players for c in p.battlefield
                        if c.is_creature() and p is not player
                    ]
                    if is_detrimental and opp_creature_names:
                        hint_target = opp_creature_names[0]
                    elif not is_detrimental and own_creature_names:
                        hint_target = own_creature_names[0]
                    else:
                        hint_target = None
                    if hint_target:
                        effect_messages.append(f"💨 {card.name} fizzles (no valid target). Try `!play {card.name} target {hint_target}`")
                    else:
                        _note = getattr(game, '_aura_fizzle_note', None)
                        if _note:
                            game._aura_fizzle_note = None
                            effect_messages.append(f"💨 {card.name} fizzles ({_note})")
                        else:
                            kind = "opponent creature" if is_detrimental else "creature you control"
                            effect_messages.append(f"💨 {card.name} fizzles (no valid target — no {kind} on battlefield)")
                    return True, f"Cast {card.name}", effect_messages
        
        # Handle Living Weapon (Batterskull, etc.)
        # Creates a 0/0 Germ token and auto-attaches the equipment
        if card.is_artifact() and card.oracle_text and 'living weapon' in card.oracle_text.lower():
            # Create 0/0 black Phyrexian Germ creature token with stable id
            import uuid as _uuid_lw
            germ = Card(
                name="Phyrexian Germ",
                mana_cost="",
                type_line="Creature Token — Phyrexian Germ",
                oracle_text="",
                power="0",
                toughness="0",
            )
            germ.id = f"token_Phyrexian_Germ_{_uuid_lw.uuid4().hex[:8]}"
            germ.is_token = True
            germ.summoning_sick = True
            germ.entered_this_turn = True
            germ.owner_index = game.players.index(player) if player in game.players else 0
            # Defensive: ensure attachments list exists on the token (dataclass default
            # should handle this, but re-initialize in case a subclass reset it).
            if not hasattr(germ, 'attachments') or germ.attachments is None:
                germ.attachments = []
            player.battlefield.append(germ)
            # Attach equipment to the germ. MUST happen before any SBA check —
            # without the attachment, Germ has 0 toughness and dies immediately.
            card.attached_to = germ.id
            germ.attachments.append(card.id)
            # Verify bonus is visible right after attachment to catch future breakage.
            try:
                eff_p = germ.get_effective_power(game)
                eff_t = germ.get_effective_toughness(game)
                print(f"[LIVING-WEAPON] {card.name} -> {germ.name} (germ_id={germ.id}, equip_id={card.id}) effective={eff_p}/{eff_t}")
                if eff_t <= 0:
                    print(f"[LIVING-WEAPON] WARNING: {germ.name} still has 0 toughness after attachment. Equipment oracle: {card.oracle_text[:100]!r}")
            except Exception as _lw_exc:
                print(f"[LIVING-WEAPON] P/T check raised: {_lw_exc}")
            effect_messages.append(f"⚔️ {card.name} — Living weapon creates a 0/0 Phyrexian Germ token, equipped with {card.name}")
            # Pub/sub slice 2 (July 20): the germ is its own battlefield
            # entry — emit, and run the creature-enters watcher scan that
            # this path never had (Soul Warden never saw a Batterskull
            # germ; found by relocating the cast emit, fixed by reading).
            events.emit(events.PERMANENT_ENTERED, game, card=germ,
                        controller=player, via="living_weapon",
                        rules=engine.rules)
            # Slice 2b (July 21): watcher dispatch ran in the
            # PERMANENT_ENTERED subscriber (emit above). Drain in place.
            effect_messages.extend(helpers.drain_pending_messages(game))

        # Handle "As ~ enters, pay any amount of life" (Phyrexian Processor)
        if card.oracle_text and card.is_artifact():
            oracle_lower = card.oracle_text.lower()
            if 'pay any amount of life' in oracle_lower and 'enters' in oracle_lower:
                # Prompt the player to choose how much life to pay
                game.pending_action = {
                    'type': 'pay_life_etb',
                    'card_id': card.id,
                    'card_name': card.name,
                    'player_idx': game.players.index(player),
                }
                effect_messages.append(
                    f"💀 **{card.name}** — As it enters, you may pay any amount of life.\n"
                    f"  Use `!target <amount>` to choose how much life to pay (0 for none).\n"
                    f"  (Current life: {player.life})"
                )
                print(f"[ETB] {card.name}: waiting for life payment choice")

        # Handle "enters with X +1/+1 counters" (Omarthis, Walking Ballista, etc.)
        # This is a REPLACEMENT EFFECT - counters apply AS it enters, not after
        if card.oracle_text and card.is_creature():
            oracle_lower = card.oracle_text.lower()
            if (("enters with x" in oracle_lower or "enters the battlefield with x" in oracle_lower) 
                and "+1/+1 counter" in oracle_lower):
                # Figure out X from _x_value (set during casting) or mana paid
                # For {X}{X} costs like Omarthis, X = total_paid / 2
                # For {X} costs like Walking Ballista, X = total_paid - colored_costs
                x_value = 0
                # Prefer _x_value set during cast_spell_async (most reliable)
                if hasattr(card, '_x_value') and card._x_value is not None and card._x_value > 0:
                    x_value = card._x_value
                elif card.mana_cost:
                    cost_upper = card.mana_cost.upper()
                    # Count X's in cost
                    x_count = cost_upper.count('X')
                    # Count colored mana requirements
                    colored_cost = sum(cost_upper.count(f'{{{c}}}') for c in ['W', 'U', 'B', 'R', 'G'])
                    # Parse generic mana (numbers in braces)
                    generic_cost = 0
                    for match in re.findall(r'\{(\d+)\}', cost_upper):
                        generic_cost += int(match)

                    # X = (mana_paid - colored - generic) / x_count
                    if x_count > 0 and hasattr(card, '_mana_paid') and card._mana_paid > 0:
                        x_value = (card._mana_paid - colored_cost - generic_cost) // x_count
                    else:
                        # No mana tracking — shouldn't happen after the X-cost fix,
                        # but as a fallback, use 1 so 0/0 creatures don't instantly die
                        x_value = max(1, (card.cmc - colored_cost - generic_cost) // max(x_count, 1))
                
                if x_value > 0:
                    card.counters['+1/+1'] = card.counters.get('+1/+1', 0) + x_value
                    effect_messages.append(f"⭕ {card.name} enters with {x_value} +1/+1 counter(s)")

            # June 10 audit (V14): "enters with N shield counters" (Sanctuary
            # Warden, CR 702.154). The death-side checks (SBA lethal damage,
            # delegated checker, board-wipe save chain) all already honor
            # shield counters — but nothing ADDED them at ETB, so the Warden
            # died twice with zero [SHIELD-COUNTER] events in the June batch.
            _shield_m = re.search(
                r'enters (?:the battlefield )?(?:tapped )?with (a|an|one|two|three|four|five|\d+) shield counters?',
                oracle_lower)
            if _shield_m:
                _word_num = {'a': 1, 'an': 1, 'one': 1, 'two': 2, 'three': 3,
                             'four': 4, 'five': 5}
                _raw = _shield_m.group(1)
                _n = _word_num.get(_raw)
                if _n is None:
                    try:
                        _n = int(_raw)
                    except ValueError:
                        _n = 1
                card.counters['shield'] = card.counters.get('shield', 0) + _n
                effect_messages.append(f"🛡️ {card.name} enters with {_n} shield counter(s)")
                print(f"[SHIELD-COUNTER] {card.name} enters with {_n} shield counter(s)")

        # Initialize planeswalker loyalty
        if card.is_planeswalker():
            base_loyalty = 0
            if card.loyalty:
                try:
                    base_loyalty = int(card.loyalty)
                except (ValueError, TypeError):
                    base_loyalty = 0
            
            # Special planeswalker loyalty rules
            oracle_lower = card.oracle_text.lower() if card.oracle_text else ""
            
            # Jeska, Thrice Reborn class: enters with loyalty = times you've
            # cast a commander from the command zone. July 21 batch audit
            # (game_1529160614050791549): the old predicate here checked for
            # an "enters with a number of..." wording that matches NO
            # printing of Jeska ("...with a loyalty counter on her for each
            # time you've cast a commander...") — dead branch, so the MAIN
            # cast path fell to base loyalty 0 and she died to the SBA even
            # after Daretti was cast from the CZ. The July 20 helper had only
            # been wired into the suspend + noncast paths. Route through it.
            from mtg.helpers import loyalty_from_commander_casts
            _cmd_bonus = loyalty_from_commander_casts(game, player, card)
            if _cmd_bonus:
                card.loyalty_counters = base_loyalty + _cmd_bonus
                effect_messages.append(
                    f"⚡ {card.name} enters with {card.loyalty_counters} loyalty "
                    f"({_cmd_bonus} commander cast(s) this game)")
            else:
                card.loyalty_counters = base_loyalty
                if base_loyalty > 0:
                    effect_messages.append(f"⚡ {card.name} enters with {card.loyalty_counters} loyalty")
        
        # Handle ETB effects for permanents
        # Only match SELF-ETB effects, not ongoing "whenever" triggers
        if card.oracle_text:
            oracle_lower = card.oracle_text.lower()
            card_name_lower = card.name.lower()
            
            # Extract just the engine-ETB paragraph(s) from oracle text
            etb_paragraphs = []
            for paragraph in card.oracle_text.split('\n'):
                p_lower = paragraph.lower().strip()
                if not p_lower:
                    continue
                
                # Match engine-ETB patterns:
                # "When [card name] enters..." 
                # "When this [type] enters..."
                # "When ~ enters..." (Scryfall uses card name, not ~)
                # But NOT "Whenever another creature enters..." (ongoing trigger)
                # And NOT "Whenever a land enters..." (landfall - ongoing)
                # June 11 audit: modern Oracle text uses "this creature" for
                # the Titan cycle's combined enter/attack triggers. The old
                # printed-name-only branch silently skipped both Titan ETBs in
                # game 1514633271047225385.
                from mtg.triggers import _is_self_etb_trigger_paragraph
                is_self_etb = _is_self_etb_trigger_paragraph(card, paragraph)
                
                # Also match "As [card name] enters" (replacement effects like clone)
                if p_lower.startswith("as ") and "enters" in p_lower:
                    is_self_etb = True
                
                if is_self_etb:
                    etb_paragraphs.append(paragraph.strip())
            
            if etb_paragraphs:
                # Save everything appended so far (cast-trigger draws, the
                # aura's "✨ enchants" line) — the rebind below discards the
                # list, and the restore happens at the shared tail, NOT here,
                # because the downstream tier gates key on "did Tier 1
                # produce output" via `if effect_messages` (see the
                # function-top comment, July 24 batch-6 anomaly #5).
                _perm_msgs_saved = list(effect_messages)
                # Tier 1: Check special effects (handles ramp creatures like Wood Elves)
                effect_messages = engine.resolve_special_effects(game, player, card, target)

                # June 10 audit (C8): the inline reanimation-aura handler already
                # performed this card's reanimation — running the name-keyed ETB
                # template would reanimate a SECOND creature (one Animate Dead
                # cast returned both Plaguecrafter and Sram). One-shot flag:
                # cleared here so non-cast re-entries (flicker) still use the
                # template path.
                # June 11 audit: the C8 skip left effect_messages empty, so the
                # Tier 2 gate below re-resolved the aura anyway via
                # SpellResolver → XMage bridge (game 1514621789555265558: one
                # Animate Dead reanimated Craterhoof inline AND a phantom
                # Shriekmaw via the bridge, and discord reported the wrong
                # one). _etb_handled_inline gates every later tier.
                _etb_handled_inline = False
                if not effect_messages and getattr(card, '_reanimate_handled', False):
                    print(f"[ETB-TEMPLATE] Skipping {card.name} — reanimation already handled inline (C8)")
                    card._reanimate_handled = False
                    _etb_handled_inline = True
                # Tier 1.5: Try effect template library (data-driven, no API)
                elif not effect_messages and HAS_EFFECT_TEMPLATES:
                    try:
                        etb_text = "\n".join(etb_paragraphs)
                        opponent_idx = 1 - (game.players.index(player) if player in game.players else 0)
                        opponent = game.players[opponent_idx]
                        
                        ctx = build_game_context(game, player, opponent, card=card, explicit_target=target)
                        lib = get_effect_library()
                        actions, explanation = lib.resolve_etb(
                            card_name=card.name,
                            oracle_text=etb_text,
                            controller=player.name,
                            opponent=opponent.name,
                            game_context=ctx,
                        )
                        
                        if actions is not None:
                            if actions:  # Non-empty action list
                                _any_action_executed = False  # May 13: track silent successes
                                for action in actions:
                                    action_type = action.get("action", "")
                                    if action_type == "no_action":
                                        reason = action.get("reason", "")
                                        # May 30 audit: don't surface internal-engine
                                        # jargon ("handled mechanically by the SBA
                                        # engine") to players — console keeps it.
                                        if reason and 'sba engine' not in reason.lower() \
                                                and 'handled mechanically' not in reason.lower():
                                            # June 10: clamp CoT-length reasons
                                            # (Land Tax paragraph leak); console
                                            # keeps the full text.
                                            from mtg.helpers import clamp_noop_reason
                                            print(f"[NO-ACTION-REASON] {card.name}: {reason}")
                                            effect_messages.append(f"📜 {clamp_noop_reason(reason)}")
                                        continue
                                    # [TARGETING] Validate target for targeted ETB actions
                                    # (e.g., Ravenous Chupacabra targeting a hexproof creature)
                                    if HAS_TARGETING and action.get("target_card"):
                                        target_name = action.get("target_card", "")
                                        target_ctrl = action.get("target_controller", "")
                                        legal, reason = _validate_target_for_action(
                                            game, target_name, target_ctrl, card, player.name)
                                        if not legal:
                                            print(f"[ETB-TARGET] {card.name} can't target {target_name}: {reason}")
                                            effect_messages.append(f"⚡ {card.name}'s ETB fizzles — {target_name} {reason}")
                                            continue
                                    try:
                                        msg = engine.rules._execute_action_on_state(game, action)
                                        _any_action_executed = True
                                        if msg:
                                            effect_messages.append(msg)
                                    except Exception as e:
                                        print(f"[TEMPLATE] Action failed for {card.name}: {action} — {e}")

                                # May 13 audit: template fired, action executed,
                                # but the action handler returned None (e.g. scry
                                # that kept all cards on top — Omenspeaker, Watcher
                                # in the Mist scry/surveil). Without a fallback
                                # line `effect_messages` stays empty, the "resolved"
                                # bookkeeping below is skipped, and we fall through
                                # to ETB-UNHANDLED + the manual `!resolve` prompt
                                # even though the template DID handle it.
                                if _any_action_executed and not effect_messages:
                                    effect_messages.append(f"📜 {card.name}: {explanation}")

                                if effect_messages:
                                    # May 7 audit fix #8: same as spell-template
                                    # path — distinguish "Resolved" from
                                    # "Conditional not met" (Charming Prince
                                    # picking a mode whose effect ended up
                                    # silent because of internal RNG).
                                    _any_real_etb = any(
                                        a.get("action") != "no_action" for a in actions
                                    )
                                    if _any_real_etb:
                                        print(f"[ETB-TEMPLATE] Resolved {card.name} via template: {explanation}")
                                    else:
                                        _cond_etb = next(
                                            (a.get("reason", "") for a in actions
                                             if a.get("action") == "no_action" and a.get("reason")),
                                            ""
                                        )
                                        print(f"[ETB-TEMPLATE] Conditional not met for {card.name}: {_cond_etb or explanation}")
                                    # Track resolved ETB to prevent AI double-resolution
                                    if not hasattr(game, '_recently_resolved_etbs'):
                                        game._recently_resolved_etbs = set()
                                    game._recently_resolved_etbs.add(card.name)
                                    # Also add to _recently_resolved_spells for broader dedup
                                    if not hasattr(game, '_recently_resolved_spells'):
                                        game._recently_resolved_spells = set()
                                    game._recently_resolved_spells.add(card.name)
                                    # Prevent double-resolution: clear pending_resolves for this card
                                    # so the AI doesn't re-fire via {"type":"resolve"}
                                    if hasattr(game, 'pending_resolves') and game.pending_resolves:
                                        game.pending_resolves = [
                                            pr for pr in game.pending_resolves
                                            if card.name.lower() not in pr.lower()
                                        ]
                                    # Panharmonicon: if on battlefield, fire the ETB again
                                    has_panharmonicon = any(
                                        p.name.lower() == "panharmonicon"
                                        for p in player.battlefield
                                        if p != card  # Panharmonicon doesn't double itself
                                    )
                                    # Panharmonicon doubles artifact AND creature ETBs (CR 603.1).
                                    # Restricting to entering type ensures non-ETB triggers can
                                    # never reach this announcement branch.
                                    if has_panharmonicon and (card.is_creature() or card.is_artifact()):
                                        print(f"[PANHARMONICON] Doubling ETB for {card.name}")
                                        effect_messages.append(f"⚡ Panharmonicon doubles {card.name}'s ETB!")
                                        # Re-resolve template with FRESH context (board state changed after 1st ETB)
                                        try:
                                            ctx2 = build_game_context(game, player, opponent, card=card, explicit_target=target)
                                            actions2, explanation2 = lib.resolve_etb(
                                                card_name=card.name,
                                                oracle_text="\n".join(etb_paragraphs),
                                                controller=player.name,
                                                opponent=opponent.name,
                                                game_context=ctx2,
                                            )
                                            pan_actions = actions2 if actions2 else actions  # Fall back to original if re-resolve fails
                                        except Exception:
                                            pan_actions = actions  # Fall back to replaying same actions
                                        for action in pan_actions:
                                            if action.get("action") == "no_action":
                                                continue
                                            try:
                                                msg = engine.rules._execute_action_on_state(game, action)
                                                if msg:
                                                    effect_messages.append(f"⚡ (Panharmonicon) {msg}")
                                            except Exception as e:
                                                print(f"[PANHARMONICON] Action failed: {action} — {e}")
                            else:
                                # Empty list = template returned no actions. This is normal for:
                                #  - cards with only static/keyword ETB clauses (ex: trample, flying)
                                #  - X-counter creatures (Walking Ballista, Hangarback) — handled
                                #    separately by the "enters with X +1/+1 counters" block below
                                oracle_lower_t = (card.oracle_text or '').lower()
                                if 'enters with x' in oracle_lower_t or 'enters the battlefield with x' in oracle_lower_t:
                                    print(f"[ETB-TEMPLATE] {card.name}: deferred to X-counter handler")
                                else:
                                    print(f"[ETB-TEMPLATE] {card.name}: no ETB actions (static abilities only)")
                    except Exception as e:
                        print(f"[TEMPLATE] Error for {card.name}: {e}")
                
                # Tier 2: If no special handling, try SpellResolver with just the ETB text
                if not effect_messages and not _etb_handled_inline and engine.spell_resolver:
                    try:
                        etb_text = "\n".join(etb_paragraphs)
                        
                        # Temporarily swap oracle text for ETB-only parsing
                        original_oracle = card.oracle_text
                        card.oracle_text = etb_text
                        
                        result = await engine.spell_resolver.cast_spell(
                            game, player, card, target=target, target_mode=TargetMode.AUTO
                        )
                        
                        # Restore original oracle text
                        card.oracle_text = original_oracle
                        
                        # Check if SpellResolver actually handled it or just dumped a "complex effect" marker
                        resolved_by_spell_resolver = False
                        for msg in result.messages:
                            if "complex effect" not in msg.lower() and "effects not automated" not in msg:
                                effect_messages.append(msg)
                                resolved_by_spell_resolver = True
                        
                        # If SpellResolver couldn't handle it, try XMage bridge (tier 2.5)
                        if not resolved_by_spell_resolver and engine._xmage_available and engine._xmage_translator:
                            try:
                                print(f"[ETB] SpellResolver punted on {card.name}, trying XMage bridge")
                                xmage_state, name_map = engine._serialize_for_xmage(game)
                                reverse_map = {v: k for k, v in name_map.items()}

                                triggers = await engine.xmage_bridge.get_triggers(
                                    "enters", xmage_state, source=card.name
                                )

                                # Filter to engine-ETB triggers from THIS card
                                self_triggers = [
                                    t for t in triggers
                                    if t.get("source", "").lower() == card.name.lower()
                                    and "enters" in t.get("ability", "").lower()
                                ]

                                if self_triggers:
                                    opponent_idx = 1 - (game.players.index(player) if player in game.players else 0)
                                    opponent = game.players[opponent_idx]
                                    ctx = build_game_context(game, player, opponent, card=card)

                                    for trigger in self_triggers:
                                        trig_ctrl = reverse_map.get(trigger.get("controller", ""), player.name)
                                        trig_opp_key = "playerB" if trigger.get("controller") == "playerA" else "playerA"
                                        trig_opp = reverse_map.get(trig_opp_key, opponent.name)

                                        t_actions, t_expl = engine._xmage_translator.translate_trigger(
                                            source_card=card.name,
                                            ability_text=trigger["ability"],
                                            controller=trig_ctrl,
                                            opponent=trig_opp,
                                            game_context=ctx,
                                        )

                                        if t_actions is not None:
                                            for action in t_actions:
                                                if action.get("action") == "no_action":
                                                    reason = action.get("reason", "")
                                                    # May 30 audit: suppress internal-engine jargon.
                                                    if reason and 'sba engine' not in reason.lower() \
                                                            and 'handled mechanically' not in reason.lower():
                                                        # June 10: clamp CoT-length reasons (see helpers).
                                                        from mtg.helpers import clamp_noop_reason
                                                        print(f"[NO-ACTION-REASON] {card.name}: {reason}")
                                                        effect_messages.append(f"📜 {clamp_noop_reason(reason)}")
                                                    continue
                                                try:
                                                    msg = engine.rules._execute_action_on_state(game, action)
                                                    if msg:
                                                        effect_messages.append(msg)
                                                except Exception as e:
                                                    print(f"[XMAGE-ETB] Action failed for {card.name}: {e}")

                                            if effect_messages:
                                                print(f"[XMAGE-ETB] Resolved {card.name} ETB: {t_expl}")
                            except Exception as e:
                                print(f"[XMAGE-ETB] Error for {card.name}: {e}")

                        # Early-exit: if the ETB paragraph contains no actionable
                        # verb, don't waste a Tier 3 API call. Matches paragraphs
                        # like "When X enters, you may..." that got captured by the
                        # pattern but have no effect the engine could execute
                        # (Swiftfoot Boots-style false positives where the ETB
                        # paragraph is flavor or conditional with no trigger body).
                        if not resolved_by_spell_resolver and not effect_messages:
                            _etb_lower = "\n".join(etb_paragraphs).lower()
                            _verbs = (
                                'draw', 'deal', 'damage', 'create', 'destroy',
                                'exile', 'return', 'counter', 'gain', 'lose',
                                'sacrifice', 'search', 'scry', 'surveil',
                                'tap', 'untap', 'put', 'add', 'mill', 'discard',
                                'reveal', 'copy', 'fight', 'transform', 'regenerate',
                                '+1/+1', '-1/-1', 'counter on', 'token',
                            )
                            if not any(v in _etb_lower for v in _verbs):
                                print(f"[ETB] {card.name}: no actionable verb in ETB text — skipping Tier 3")
                                effect_messages.append(f"📜 {card.name} enters the battlefield")
                        if not resolved_by_spell_resolver and not effect_messages:
                            print(f"[ETB] Using resolve_effect (tier 3) for {card.name}")
                            controller_name = player.name
                            resolve_msgs, actions = await engine.rules.resolve_effect(
                                game,
                                effect_description=etb_text,
                                source_card=card.name,
                                controller=controller_name,
                                context=f"{card.name} just entered the battlefield"
                            )
                            effect_messages.extend(resolve_msgs)
                            if actions:
                                print(f"[ETB] resolve_effect executed {len(actions)} action(s) for {card.name}")
                            elif resolve_msgs:
                                # Tier 3 returned an explanation but no executable actions (e.g. "no valid targets")
                                # This counts as "handled" — don't fall through to ETB-UNHANDLED
                                print(f"[ETB] resolve_effect: no actions but has explanation for {card.name}")
                            else:
                                # Tier 3 returned nothing at all (JSON parse error,
                                # API outage, library-look short-circuit, dedupe).
                                # Don't post a false "no effect" / "fizzled" line:
                                # the trigger queue (drain_pending_triggers) often
                                # resolves the same ETB seconds later, and players
                                # see contradictory messages. Let it handle itself
                                # silently — pending_resolves still logs anything
                                # genuinely unhandled for diagnostics.
                                print(f"[ETB] resolve_effect returned no actions for {card.name} — suppressing (trigger queue may still handle it)")
                                
                    except Exception as e:
                        print(f"[SPELL_RESOLVER] Error resolving ETB for {card.name}: {e}")
                
                # If still nothing resolved, log for future hardcoding
                if not effect_messages and not _etb_handled_inline:
                    etb_text = "\n".join(etb_paragraphs)
                    print(f"[ETB-UNHANDLED] {card.name}: {etb_text[:150]}")
                    if _should_emit_resolve_hint(game, f"etb:{card.name}"):
                        effect_messages.append(
                            f"📜 **{card.name}** ETB: *{etb_text[:200]}{'...' if len(etb_text) > 200 else ''}*\n"
                            f"  *(Use `!resolve {card.name} ETB` or `!fix` to handle manually)*"
                        )
                    # Track for AI — so it knows to resolve this on its turn
                    game.pending_resolves.append(
                        f"{card.name} ETB: {etb_text[:150]}"
                    )
        
        # Pub/sub slice 2: one PERMANENT_ENTERED per physical entry, emitted
        # HERE — after aura-fizzle early-returns (a fizzled aura never
        # entered, CR 303.4) and after clone characteristics applied (kind
        # must be post-copy). The only early return between the battlefield
        # append and this point is the aura fizzle, verified July 20.
        if card in player.battlefield:
            events.emit(events.PERMANENT_ENTERED, game, card=card,
                        controller=player, via="cast", rules=engine.rules)
            # Slice 2b (July 21): the emit above ran the creature watcher
            # dispatch (subscriber). Drain its lines at the position the
            # old direct scan call occupied.
            effect_messages.extend(helpers.drain_pending_messages(game))

        # Check for "whenever another creature enters" triggers (Terror of the Peaks, etc.)
        # May 30 audit: pass game so a devotion-gated god entering as a NON-creature
        # (Purphoros at devotion<5) doesn't trigger creature-ETB watchers (CR 603.2a).
        if card.is_creature(game):
            # Slice 2b (July 21): watcher dispatch ran in the
            # PERMANENT_ENTERED subscriber (emit above); already drained.
            pass
        else:
            from mtg.triggers import _check_permanent_etb_watchers
            effect_messages.extend(_check_permanent_etb_watchers(
                engine, game, player, card))

        # June 10 deep-dive (B9): constellation / "whenever an enchantment
        # enters" watchers had no scan AT ALL — Eidolon of Blossoms drew 0 of
        # 3 owed cards while three enchantments entered past it.
        if card.is_enchantment():
            # Slice 2b (2/2, July 21): constellation dispatch ran in the
            # PERMANENT_ENTERED subscriber (cast emit above). Drain in place.
            effect_messages.extend(helpers.drain_pending_messages(game))

        # June 11 smaller queue: Hammer of Nazahn's placeholder claimed this
        # watcher already existed. Resolve its free attach for Equipment that
        # enter after Hammer; Hammer's own entry is handled by its template.
        if card.is_artifact() and 'equipment' in (card.type_line or '').lower():
            from mtg.triggers import _check_equipment_etb_watchers
            equipment_watcher_msgs = _check_equipment_etb_watchers(
                engine, game, player, card)
            effect_messages.extend(equipment_watcher_msgs)

    # [LAYERS] Register static keyword-granting abilities and recalculate
    # [REPLACEMENT] Register replacement effects (Furnace of Rath, Rest in Peace, etc.)
    if card in player.battlefield:
        game.register_static_keyword_grants(card, player.name)
        game.register_static_pt_effects(card, player.name)
        game.register_replacement_effects(card, player.name)
        game.recalculate_granted_keywords()
        game.recalculate_power_toughness()

    # Restore the messages the permanent branch's Tier-1 rebind discarded
    # (cast-trigger draws, aura attach lines) — see the function-top comment.
    if _perm_msgs_saved:
        effect_messages = _perm_msgs_saved + (effect_messages or [])

    # Flush draw-trigger side-channel (Smothering Tithe, Consecrated Sphinx)
    engine._flush_pending_messages(game, effect_messages)
    return True, f"Cast {card.name}", effect_messages


async def cast_spell_async(engine, game: GameState, player: Player, card: Card, pay_mana: bool = True, target: Any = None, additional_cost: int = 0) -> Tuple[bool, str, List[str]]:
    """Cast a spell with full effect resolution (async version).

    Args:
        game: Game state
        player: Casting player
        card: Card to cast
        pay_mana: Whether to pay mana cost
        target: Optional target for the spell
        additional_cost: Additional generic mana cost (e.g., commander tax)

    Returns:
        Tuple of (success, message, effect_messages)
    """
    _rejection, _cast_from_graveyard, target = _validate_cast(
        engine, game, player, card, target)
    if _rejection is not None:
        return _rejection

    _rejection, _costs = _compute_alt_costs(
        engine, game, player, card, pay_mana, additional_cost)
    if _rejection is not None:
        return _rejection
    free_cast_source = _costs['free_cast_source']

    _rejection = _pay_costs(engine, game, player, card, _costs, additional_cost)
    if _rejection is not None:
        return _rejection

    # Preserve the origin for effects such as Wash Away that inspect where
    # the target spell was cast from after it has moved to the stack.
    if getattr(card, 'cast_from_command_zone', False):
        card._cast_origin = 'command_zone'
    elif _cast_from_graveyard:
        card._cast_origin = 'graveyard'
    else:
        card._cast_origin = 'hand'

    if _cast_from_graveyard:
        player.graveyard.remove(card)
        try:
            player.playable_from_graveyard.remove(card.id)
        except (ValueError, AttributeError):
            pass
        # CR 702.34a: a flashbacked spell is exiled as it resolves —
        # consumed by the resolution zone-routing below.
        card._resolving_flashback = True
        print(f"[FLASHBACK] {card.name} cast from graveyard (leaves graveyard)")
    else:
        player.hand.remove(card)

    # Track spells cast this turn (for day/night, werewolf transform, Esper Sentinel)
    player.spells_cast_this_turn += 1
    if not card.is_creature():
        player.noncreature_spells_cast_this_turn += 1

    # May 14 audit (A7): track per-game spell-type counts on the game state so
    # the strategist can detect "opponent has cast 0 noncreature spells in 8
    # turns — your Mana Drain is dead this matchup, pivot." Without this, the
    # control deck holds counterspells for 20 turns and dies.
    if not hasattr(game, '_spell_counts_by_player'):
        game._spell_counts_by_player = {}  # player_name -> {creature, noncreature, instant, sorcery, total}
    counts = game._spell_counts_by_player.setdefault(
        player.name, {'creature': 0, 'noncreature': 0, 'instant': 0, 'sorcery': 0, 'total': 0}
    )
    type_l = (card.type_line or '').lower()
    counts['total'] += 1
    if 'creature' in type_l:
        counts['creature'] += 1
    else:
        counts['noncreature'] += 1
    if 'instant' in type_l:
        counts['instant'] += 1
    elif 'sorcery' in type_l:
        counts['sorcery'] += 1

    effect_messages = []
    # July 20 audit: surface pain-land tap damage (City of Brass, Ancient
    # Tomb) buffered by tap_sources_for_cost — it was console-only, leaving
    # unexplained life drops in the Discord narration (13 in one July 16
    # game). Drained here so the lines ride the normal effect_messages path.
    _tap_dmg_msgs = getattr(player, '_pending_tap_damage_msgs', None)
    if _tap_dmg_msgs:
        effect_messages.extend(_tap_dmg_msgs)
        _tap_dmg_msgs.clear()
    if free_cast_source:
        effect_messages.append(f"🆓 {card.name} cast for free via {free_cast_source}!")

    _final, cast_trigger_msgs, player_idx = await _await_stack_window(
        engine, game, player, card, target, effect_messages)
    if _final is not None:
        return _final

    return await _dispatch_resolution(engine, game, player, card, target,
                                      effect_messages, cast_trigger_msgs,
                                      player_idx)

# =========================================================================
# PENDING ASYNC TRIGGER QUEUE
# =========================================================================
# Sync paths (advance_phase, end_turn, _handle_etb_triggers, SBA loops) can't
# call the async Tier 3 resolver. Instead of emitting a `*(Use !resolve ...)*`
# hint and dropping the trigger, they enqueue here. The next async caller
# drains the queue via drain_pending_triggers() and actually resolves the
# effects via engine.rules.resolve_effect(). See CLAUDE.md "Known Limitation:
# Sync Trigger Gap".


def _advance_sagas(engine, game: GameState, player: Player) -> List[str]:
    """Add a lore counter to each saga the player controls and fire chapter abilities.
    Called after draw step (CR 714.3a). Also handles saga ETB (first lore counter)."""
    messages = []
    for card in list(player.battlefield):
        tl = (card.type_line or '').lower()
        if 'saga' not in tl:
            continue
        # Add lore counter
        if not hasattr(card, 'counters') or card.counters is None:
            card.counters = {}
        old_lore = card.counters.get('lore', 0)
        card.counters['lore'] = old_lore + 1
        new_lore = card.counters['lore']
        print(f"[SAGA] {card.name}: lore counter {old_lore} → {new_lore}")
        # Fire chapter ability — parse oracle text for chapter N text
        chapter_text = engine._get_saga_chapter_text(card, new_lore)
        if chapter_text:
            messages.append(f"📖 **{card.name}** — Chapter {new_lore}: *{chapter_text[:200]}*")
            # May 20 audit (Fix #50): for the FINAL chapter of a transforming
            # saga, the chapter ability IS the transform (CR 715.4: "exile
            # this Saga, then return it to the battlefield transformed").
            # SBA SAGA_COMPLETE handler at mtg/sba.py performs the transform
            # using _TRANSFORMING_SAGA_BACK_FACES. Running Tier 3 on the
            # chapter text in parallel produces a duplicate drain — visible in
            # game_1506623303794561024:1209-1213 where Restoration of Eiganjo
            # transformed AND then ran [DRAIN-SAGA_CHAPTER_3] on the back face.
            # Skip the chapter-resolution step when the chapter text describes
            # the transform itself.
            from mtg.sba import _TRANSFORMING_SAGA_BACK_FACES
            saga_chapters = engine._get_saga_total_chapters(card)
            is_final_chapter = new_lore >= saga_chapters
            is_transforming_saga = (
                is_final_chapter
                and (card.name or '').lower() in _TRANSFORMING_SAGA_BACK_FACES
            )
            # Belt-and-suspenders: also catch raw "exile this saga ... return
            # ... transformed" text for sagas not yet in the lookup table.
            chapter_lower = (chapter_text or '').lower()
            looks_like_transform_chapter = (
                'exile this saga' in chapter_lower
                and ('return' in chapter_lower)
                and ('transformed' in chapter_lower or 'transforms' in chapter_lower)
            )
            if is_transforming_saga or looks_like_transform_chapter:
                print(f"[SAGA-CHAPTER] {card.name} chapter {new_lore} IS the transform — "
                      f"skipping Tier 3 resolution (SBA SAGA_COMPLETE will handle)")
            elif HAS_EFFECT_TEMPLATES:
                # Try to resolve the chapter via template/tier system
                try:
                    lib = get_effect_library()
                    opp_idx = 1 - game.players.index(player)
                    opp = game.players[opp_idx]
                    # June 10 round 3: pass real context (see chapter-I site).
                    _saga_ctx = build_game_context(game, player, opp, card=card)
                    actions, desc = lib.resolve_etb(card.name, chapter_text, player.name, opp.name,
                                                    game_context=_saga_ctx)
                    if actions:
                        for act in actions:
                            result = engine.rules._execute_action_on_state(game, act)
                            if result:
                                messages.append(f"  {result}")
                        print(f"[SAGA-CHAPTER] Resolved {card.name} chapter {new_lore} via template")
                    else:
                        # Queue for async Tier 3 drain (sync context). Don't post a
                        # "queued for Tier 3 resolution" line to Discord — the actual
                        # outcome appears when drain_pending_triggers fires, which
                        # is more useful than an internal status placeholder.
                        engine._queue_async_trigger(
                            game, card, chapter_text, f"saga_chapter_{new_lore}",
                            player.name,
                            context=f"{card.name} Chapter {new_lore} of {engine._get_saga_total_chapters(card)}",
                        )
                except Exception as e:
                    print(f"[SAGA-CHAPTER] Error resolving {card.name} chapter {new_lore}: {e}")
                    engine._queue_async_trigger(
                        game, card, chapter_text, f"saga_chapter_{new_lore}",
                        player.name,
                        context=f"{card.name} Chapter {new_lore} (template error)",
                    )
        # Check if saga is complete (sacrifice via SBA)
        saga_chapters = engine._get_saga_total_chapters(card)
        if new_lore >= saga_chapters:
            print(f"[SAGA] {card.name}: final chapter reached ({new_lore}/{saga_chapters}), will sacrifice via SBA")
    return messages


def _process_suspend_upkeep(engine, game: GameState) -> List[str]:
    """Process suspended cards at upkeep - remove time counters and cast if 0."""
    messages = []
    player = game.active_player
    player_idx = game.players.index(player) if player in game.players else 0
    opponent = game.players[1 - player_idx]
    
    # Find all suspended cards in exile
    cards_to_cast = []
    for card in player.exile:
        if card.suspended and card.counters.get('time', 0) > 0:
            # Remove a time counter
            card.counters['time'] -= 1
            remaining = card.counters['time']
            messages.append(f"⏳ {card.name}: removed time counter ({remaining} remaining)")
            
            # If no counters left, queue it for casting
            if remaining <= 0:
                cards_to_cast.append(card)
    
    # Cast cards with no time counters left
    for card in cards_to_cast:
        card.suspended = False
        if 'time' in card.counters:
            del card.counters['time']

        # Move from exile
        player.exile.remove(card)
        oracle = card.oracle_text.lower() if card.oracle_text else ""

        # July 24 (slice 4b groundwork): coming off suspend IS a cast
        # (CR 702.62e) — battlefield cast triggers (Talrand, Rhystic-class)
        # never fired for it because this path is sync. Queue them for the
        # async Tier-3 drain + emit CARD_CAST (parity-paired inside the
        # helper). Lands aren't cast (they're put onto the battlefield).
        if not card.is_land():
            from mtg.triggers import queue_cast_triggers_sync
            queue_cast_triggers_sync(engine, game, player, card, via="suspend")
        
        if card.is_creature():
            # Creatures from suspend have haste
            card.summoning_sick = False
            if 'Haste' not in card.temp_keywords:
                card.temp_keywords.append('Haste')
            player.battlefield.append(card)
            card.entered_this_turn = True
            messages.append(f"⏰ {card.name} comes off suspend and enters the battlefield with haste!")
            events.emit(events.PERMANENT_ENTERED, game, card=card,
                        controller=player, via="suspend", rules=engine.rules)

            # Check for ETB triggers
            etb_msgs = engine._handle_etb_triggers(game, player, card)
            messages.extend(etb_msgs)

        elif card.is_land():
            # Lands just enter
            player.battlefield.append(card)
            messages.append(f"⏰ {card.name} comes off suspend and enters the battlefield!")
            events.emit(events.PERMANENT_ENTERED, game, card=card,
                        controller=player, via="suspend", rules=engine.rules)
            
        elif card.is_instant() or card.is_sorcery():
            # Resolve spell effects
            messages.append(f"⏰ {card.name} comes off suspend!")
            spell_msgs = engine._resolve_suspend_spell(game, player, card, opponent)
            messages.extend(spell_msgs)
            # Goes to graveyard after resolving (or command zone for signature spells)
            if getattr(card, 'is_signature_spell', False) and game.format == "oathbreaker":
                player.command_zone.append(card)
                messages.append(f"📜 {card.name} returns to command zone (signature spell)")
            else:
                player.graveyard.append(card)
            
        else:
            # Artifact, enchantment, planeswalker - enters battlefield
            player.battlefield.append(card)
            card.entered_this_turn = True
            messages.append(f"⏰ {card.name} comes off suspend and enters the battlefield!")
            events.emit(events.PERMANENT_ENTERED, game, card=card,
                        controller=player, via="suspend", rules=engine.rules)
            
            # Handle planeswalker loyalty
            if card.is_planeswalker() and card.loyalty:
                try:
                    card.loyalty_counters = int(card.loyalty)
                except:
                    pass
                # July 20 audit (Jeska, Thrice Reborn): printed loyalty 0 +
                # "enters with a loyalty counter for each time you've cast a
                # commander" — without the bonus she died to SBA the instant
                # she resolved, with zero player-visible explanation.
                from mtg.helpers import loyalty_from_commander_casts
                _cmd_bonus = loyalty_from_commander_casts(game, player, card)
                if _cmd_bonus:
                    card.loyalty_counters += _cmd_bonus
                    effect_messages.append(
                        f"⚡ {card.name} enters with {card.loyalty_counters} "
                        f"loyalty ({_cmd_bonus} commander cast(s) this game)")
            
            # Check for ETB triggers
            etb_msgs = engine._handle_etb_triggers(game, player, card)
            messages.extend(etb_msgs)
    
    return messages


def _resolve_suspend_spell(engine, game: GameState, player: Player, card: Card, opponent: Player) -> List[str]:
    """Resolve a spell that came off suspend."""
    messages = []
    oracle = card.oracle_text.lower() if card.oracle_text else ""
    
    # Ancestral Vision: "Target player draws three cards."
    if "draws three cards" in oracle or "draw three cards" in oracle:
        drawn = engine.draw_cards(player, 3, game=game)
        messages.append(f"🎴 {player.name} draws {len(drawn)} cards!")
        return messages
    
    # Wheel of Fate: "Each player discards their hand, then draws seven cards."
    if "discards their hand" in oracle and "draws seven" in oracle:
        for p in game.players:
            discarded = len(p.hand)
            p.graveyard.extend(p.hand)
            p.hand = []
            drawn = engine.draw_cards(p, 7, game=game)
            messages.append(f"🔄 {p.name} discards {discarded}, draws {len(drawn)}")
        return messages
    
    # Lotus Bloom is an artifact - it enters battlefield, not resolved as spell
    # This code path shouldn't be hit for Lotus Bloom, but just in case:
    if "lotus bloom" in card.name.lower():
        # Lotus Bloom enters as an artifact, user activates to sac for mana
        messages.append(f"✨ {card.name} enters the battlefield! Use !activate to sacrifice for 3 mana.")
        return messages
    
    # Restore Balance: "Each player chooses a number of lands they control equal to 
    # the number of lands controlled by the player who controls the fewest, then sacrifices the rest. 
    # Players sacrifice creatures and discard cards the same way."
    if "balance" in card.name.lower() or ("sacrifices the rest" in oracle and "fewest" in oracle):
        messages.append(f"⚖️ {card.name} resolves!")
        
        # Balance lands
        land_counts = []
        for p in game.players:
            lands = [c for c in p.battlefield if c.is_land()]
            land_counts.append((p, lands, len(lands)))
        
        min_lands = min(count for _, _, count in land_counts)
        for p, lands, count in land_counts:
            to_sacrifice = count - min_lands
            if to_sacrifice > 0:
                # Sacrifice from end of list (most recent)
                sacrificed = []
                for _ in range(to_sacrifice):
                    if lands:
                        land = lands.pop()
                        game.unregister_static_effects(land)
                        p.battlefield.remove(land)
                        p.graveyard.append(land)
                        sacrificed.append(land.name)
                if sacrificed:
                    messages.append(f"🏔️ {p.name} sacrifices {to_sacrifice} land(s): {', '.join(sacrificed[:3])}{'...' if len(sacrificed) > 3 else ''}")
        
        # Balance creatures
        creature_counts = []
        for p in game.players:
            creatures = [c for c in p.battlefield if c.is_creature()]
            creature_counts.append((p, creatures, len(creatures)))
        
        min_creatures = min(count for _, _, count in creature_counts)
        for p, creatures, count in creature_counts:
            to_sacrifice = count - min_creatures
            if to_sacrifice > 0:
                sacrificed = []
                for _ in range(to_sacrifice):
                    if creatures:
                        creature = creatures.pop()
                        game.unregister_static_effects(creature)
                        p.battlefield.remove(creature)
                        p.graveyard.append(creature)
                        sacrificed.append(creature.name)
                if sacrificed:
                    messages.append(f"💀 {p.name} sacrifices {to_sacrifice} creature(s): {', '.join(sacrificed[:3])}{'...' if len(sacrificed) > 3 else ''}")
        
        # Balance hand size
        hand_counts = [(p, len(p.hand)) for p in game.players]
        min_hand = min(count for _, count in hand_counts)
        for p, count in hand_counts:
            to_discard = count - min_hand
            if to_discard > 0:
                discarded = []
                for _ in range(to_discard):
                    if p.hand:
                        # Discard from end (most recently drawn)
                        card_to_discard = p.hand.pop()
                        p.graveyard.append(card_to_discard)
                        discarded.append(card_to_discard.name)
                if discarded:
                    messages.append(f"🗑️ {p.name} discards {to_discard} card(s)")
        
        return messages
    
    # Living End: "Each player exiles all creature cards from their graveyard, 
    # then sacrifices all creatures they control, then puts all cards exiled this way onto the battlefield."
    if "living end" in card.name.lower() or ("exiles all creature cards from their graveyard" in oracle):
        for p in game.players:
            # Collect graveyard creatures
            gy_creatures = [c for c in p.graveyard if c.is_creature()]
            # Collect battlefield creatures
            bf_creatures = [c for c in p.battlefield if c.is_creature()]
            
            # Move graveyard creatures to battlefield
            for c in gy_creatures:
                p.graveyard.remove(c)
                c.entered_this_turn = True
                c.summoning_sick = True
                p.battlefield.append(c)
            
            # Move battlefield creatures to graveyard
            for c in bf_creatures:
                game.unregister_static_effects(c)
                p.battlefield.remove(c)
                p.graveyard.append(c)
            
            messages.append(f"💀 {p.name}: {len(bf_creatures)} creatures die, {len(gy_creatures)} return!")
        return messages
    
    # Hypergenesis: "Starting with you, each player may put an artifact, creature, 
    # enchantment, or land card from their hand onto the battlefield. Repeat this process 
    # until no one puts a card onto the battlefield."
    if "hypergenesis" in card.name.lower() or ("each player may put" in oracle and "artifact, creature, enchantment, or land" in oracle):
        messages.append(f"🌀 {card.name} resolves!")
        
        # For simplicity: both players put ALL permanents from hand onto battlefield
        for p in game.players:
            permanents_to_play = []
            for c in list(p.hand):
                # Check if it's a permanent (not instant/sorcery)
                if c.is_creature() or c.is_land() or c.is_artifact() or c.is_enchantment() or c.is_planeswalker():
                    permanents_to_play.append(c)
            
            played_names = []
            for c in permanents_to_play:
                p.hand.remove(c)
                p.battlefield.append(c)
                c.entered_this_turn = True
                c.summoning_sick = True if c.is_creature() else False
                
                # Handle planeswalker loyalty
                if c.is_planeswalker() and c.loyalty:
                    try:
                        c.loyalty_counters = int(c.loyalty)
                    except:
                        pass
                
                played_names.append(c.name)
            
            if played_names:
                messages.append(f"🎭 {p.name} puts {len(played_names)} permanent(s): {', '.join(played_names[:5])}{'...' if len(played_names) > 5 else ''}")
            else:
                messages.append(f"🎭 {p.name} puts nothing onto the battlefield")
        
        # Trigger ETBs for all new creatures
        for p in game.players:
            for c in p.battlefield:
                if c.entered_this_turn and c.is_creature():
                    etb_msgs = engine._handle_etb_triggers(game, p, c)
                    messages.extend(etb_msgs)
        
        return messages
    
    # Generic damage spell
    damage_match = re.search(r'deals? (\d+) damage', oracle)
    if damage_match:
        damage = int(damage_match.group(1))
        if "to any target" in oracle or "target creature or player" in oracle:
            # Default to opponent
            actual_dmg = engine.rules._apply_noncombat_damage_to_player(game, opponent, damage, card.name)
            if actual_dmg > 0:
                messages.append(f"🔥 {card.name} deals {actual_dmg} damage to {opponent.name}!")
            return messages
        elif "to each creature" in oracle:
            killed = []
            for p in game.players:
                for c in list(p.battlefield):
                    if c.is_creature():
                        c.damage_marked += damage
                        # Let SBAs handle the actual death checks
            # Run SBAs to properly kill creatures (handles */* CDA creatures)
            game._recently_died = getattr(game, '_recently_died', [])
            sba_msgs = engine.rules.process_state_based_actions(game)
            killed = [m.split(" dies")[0].replace("☠️ ", "") for m in sba_msgs if "dies" in m]
            if killed:
                messages.append(f"🔥 {card.name} kills: {', '.join(killed)}")
            else:
                messages.append(f"🔥 {card.name} deals {damage} to each creature")
            return messages
    
    # Fallback — couldn't parse effect. Emit the full oracle (up to 500 chars)
    # so the user sees what the spell was supposed to do. Previous 200-char
    # truncation cut "costs less" and similar descriptions mid-sentence.
    otxt = card.oracle_text or 'none'
    # Collapse internal newlines so the Discord bullet doesn't break up.
    otxt = otxt.replace('\n', ' ').strip()
    if len(otxt) > 500:
        otxt = otxt[:497].rstrip() + '…'
    messages.append(f"🧙 **{card.name}** resolves — {otxt}")
    return messages


def resolve_special_effects(engine, game: GameState, player: Player, card: Card, target: Any = None) -> List[str]:
    """
    Handle special effects that SpellResolver doesn't cover:
    - Mass pump ("creatures you control get +X/+X")
    - Variable calculations ("X = greatest power")
    - Delayed triggers ("whenever you attack this turn")
    
    Returns effect messages, or empty list if no special handling needed.
    """
    messages = []
    oracle = card.oracle_text.lower() if card.oracle_text else ""
    card_name_lower = card.name.lower()
    player_idx = game.players.index(player) if player in game.players else 0
    opponent_idx = 1 - player_idx
    opponent = game.players[opponent_idx]

    # Summary Dismissal: exile all other spells and counter all abilities.
    # July 21 batch audit (R2-2): Tier 2's EXILE regex garbled this into an
    # untargeted no-op with EMPTY messages, which also dodged the Tier 3
    # escalation gate — the whole effect silently evaporated while the
    # cosmetic fallback line claimed it resolved, and a Summary Dismissal
    # cast specifically at Avenger of Zendikar let the Avenger resolve in
    # full (game_1529172161636597770). Needs direct stack access → Tier 1.
    if card_name_lower == "summary dismissal":
        exiled_names = []
        countered_triggers = 0
        for entry in list(getattr(game, 'stack', [])):
            e_card = getattr(entry, 'card', None)
            if e_card is None or e_card is card:
                continue
            try:
                game.stack.remove(entry)
            except ValueError:
                continue
            # Keep the PrioritySystem's mirror stack in sync (May 19
            # phantom-entry class).
            _pid = getattr(entry, 'priority_id', None)
            _ps = getattr(game, '_priority_system', None)
            if _pid and _ps is not None and hasattr(_ps, 'remove_stack_entry_by_priority_id'):
                try:
                    _ps.remove_stack_entry_by_priority_id(_pid)
                except Exception as _ps_err:
                    print(f"[SUMMARY-DISMISSAL] priority-stack sync failed: {_ps_err}")
                    from mtg.util import maybe_reraise
                    maybe_reraise(_ps_err)
            # July 30 batch-9 audit: a trigger entry's .card is its SOURCE
            # permanent (usually still on the battlefield) — exiling it
            # would clone the object into two zones. "Counter all
            # abilities" just removes the entry.
            if getattr(entry, 'is_spell', True) is False:
                entry.countered = True
                _wake = getattr(entry, 'resolution_event', None)
                if _wake is not None:
                    _wake.set()
                countered_triggers += 1
                print(f"[SUMMARY-DISMISSAL] Countered {e_card.name}'s triggered ability on the stack")
                continue
            e_owner_idx = getattr(e_card, 'owner_index', None)
            e_owner = (game.players[e_owner_idx]
                       if isinstance(e_owner_idx, int) and 0 <= e_owner_idx < len(game.players)
                       else getattr(entry, 'controller', None) or player)
            if not hasattr(e_owner, 'exile'):
                e_owner.exile = []
            e_owner.exile.append(e_card)
            # July 30 batch-9 audit (CRITICAL): without a counter mark, the
            # exiled spell's cast_spell_async coroutine timed out, read the
            # now-empty stack as "now at top", and RESOLVED the exiled spell
            # — Song of the Worldsoul entered the battlefield and triggered
            # all game (game_1532236251619528895). countered_to =
            # "already_handled" tells the countered branch the zone move is
            # done here; waking the event unwinds the caster promptly.
            entry.countered = True
            entry.countered_to = "already_handled"
            _wake = getattr(entry, 'resolution_event', None)
            if _wake is not None:
                _wake.set()
            exiled_names.append(e_card.name)
            print(f"[SUMMARY-DISMISSAL] Exiled {e_card.name} from the stack")
        # "Counter all abilities" — wipe the pending trigger queues (the
        # engine's stack-adjacent representation of triggered abilities).
        pat = getattr(game, 'pending_async_triggers', None)
        if pat:
            countered_triggers += len(pat)
            game.pending_async_triggers = []
            print(f"[SUMMARY-DISMISSAL] Countered {len(pat)} pending triggered abilit(ies)")
        if exiled_names:
            messages.append("🌀 **Summary Dismissal** exiles "
                            + ", ".join(f"**{n}**" for n in exiled_names)
                            + " from the stack")
        if countered_triggers:
            messages.append(f"🌀 **Summary Dismissal** counters {countered_triggers} "
                            f"triggered abilit{'y' if countered_triggers == 1 else 'ies'}")
        if not exiled_names and not countered_triggers:
            messages.append("🌀 **Summary Dismissal** resolves — no other spells "
                            "or abilities on the stack")
        return messages

    # Chaos Warp: target permanent → shuffle into library, reveal top, if permanent put onto battlefield
    if "chaos warp" in card_name_lower or (
        "shuffles it into their library" in oracle and "reveals the top card" in oracle
    ):
        # Find target permanent — prefer opponent's most threatening non-land, or use explicit target
        target_permanent = None
        target_owner = None

        if target:
            # Explicit target provided
            for p in game.players:
                for c in p.battlefield:
                    if (isinstance(target, str) and (c.name.lower() == target.lower() or c.id == target)) \
                            or c is target:
                        target_permanent = c
                        target_owner = p
                        break
                if target_permanent:
                    break

        if not target_permanent:
            # Auto-select: opponent's best non-land permanent
            opp_perms = [c for c in opponent.battlefield if not c.is_land()]
            if opp_perms:
                # Prefer creatures, then planeswalkers, then other
                creatures = [c for c in opp_perms if c.is_creature()]
                pws = [c for c in opp_perms if c.is_planeswalker()]
                pick_from = creatures or pws or opp_perms
                # Pick biggest threat (highest power for creatures, first PW, or first)
                def threat_val(c):
                    try:
                        return c.get_effective_power(game) if hasattr(c, 'get_effective_power') else 0
                    except (ValueError, TypeError):
                        return 0
                target_permanent = max(pick_from, key=threat_val)
                target_owner = opponent
            elif opponent.battlefield:
                # Only lands — pick any
                target_permanent = opponent.battlefield[0]
                target_owner = opponent

        if target_permanent and target_owner:
            game.unregister_static_effects(target_permanent)
            target_owner.battlefield.remove(target_permanent)
            # Shuffle into library
            target_owner.library.append(target_permanent)
            import random as _rng
            _rng.shuffle(target_owner.library)
            messages.append(f"🌀 Chaos Warp shuffles {target_permanent.name} into {target_owner.name}'s library!")
            # Reveal top card
            if target_owner.library:
                revealed = target_owner.library[0]
                messages.append(f"🔮 {target_owner.name} reveals: **{revealed.name}**")
                if not (revealed.is_instant() or revealed.is_sorcery()):
                    # It's a permanent — put onto battlefield
                    target_owner.library.pop(0)
                    revealed.entered_this_turn = True
                    revealed.summoning_sick = True if revealed.is_creature() else False
                    # Planeswalkers enter with starting loyalty (CR 306.5b)
                    if revealed.is_planeswalker() and hasattr(revealed, 'loyalty') and revealed.loyalty:
                        try:
                            revealed.current_loyalty = int(revealed.loyalty)
                            print(f"[CHAOS-WARP] {revealed.name} enters with {revealed.current_loyalty} starting loyalty")
                        except (ValueError, TypeError):
                            pass
                    target_owner.battlefield.append(revealed)
                    messages.append(f"🌍 {revealed.name} enters the battlefield under {target_owner.name}'s control!")
                else:
                    messages.append(f"📚 {revealed.name} is not a permanent — stays on top")
            else:
                messages.append(f"📚 {target_owner.name}'s library is empty — nothing revealed")
        else:
            messages.append("⚠️ Chaos Warp: no valid target")
        return messages

    # Cyclonic Rift: bounce 1 target (base) or all opponents' nonland permanents (overload)
    # Tier 1 hardcoded to prevent Tier 3 always overloading
    if "cyclonic rift" in card_name_lower:
        mana_paid = getattr(card, '_mana_paid', 0) or 0
        if mana_paid >= 7:
            # Overloaded: bounce ALL nonland permanents opponents control
            bounced = 0
            for opp in game.players:
                if opp == player:
                    continue
                to_bounce = [c for c in opp.battlefield if not c.is_land()]
                for perm in to_bounce:
                    game.unregister_static_effects(perm)
                    opp.battlefield.remove(perm)
                    if getattr(perm, 'is_token', False):
                        print(f"[TOKEN-SBA] {perm.name} bounced — token ceases to exist")
                    else:
                        opp.hand.append(perm)
                    bounced += 1
            messages.append(f"🌊 Cyclonic Rift (overloaded): Bounced {bounced} nonland permanents!")
        else:
            # Normal: bounce one target nonland permanent opponent controls
            target_card = None
            if target:
                for c in opponent.battlefield:
                    if (isinstance(target, str) and (c.name.lower() == target.lower() or c.id == target)) or c is target:
                        target_card = c
                        break
            if not target_card:
                # Auto-select best opponent nonland permanent
                opp_nonlands = [c for c in opponent.battlefield if not c.is_land()]
                if opp_nonlands:
                    opp_nonlands.sort(key=lambda c: c.cmc if c.cmc else 0, reverse=True)
                    target_card = opp_nonlands[0]
            if target_card:
                game.unregister_static_effects(target_card)
                opponent.battlefield.remove(target_card)
                if getattr(target_card, 'is_token', False):
                    print(f"[TOKEN-SBA] {target_card.name} bounced — token ceases to exist")
                else:
                    opponent.hand.append(target_card)
                messages.append(f"🌊 Cyclonic Rift: {target_card.name} returned to {opponent.name}'s hand")
            else:
                messages.append("⚠️ Cyclonic Rift: no valid target")
        return messages

    # Craterhoof Behemoth: handled by Tier 1.5 template in effect_templates.py
    # (Tier 1 handler removed — was double-applying P/T pump via both power_modifier
    # AND layers engine, causing accumulating deltas like -1381/-1381 on tokens)

    # Overwhelming Stampede: creatures get +X/+X and trample where X = greatest power
    if "creatures you control" in oracle and "trample" in oracle and "greatest power" in oracle:
        max_power = 0
        for c in player.battlefield:
            if c.is_creature():
                try:
                    p = c.get_effective_power(game) if hasattr(c, 'get_effective_power') else 0
                    max_power = max(max_power, p)
                except:
                    pass
        
        if max_power > 0:
            count = 0
            for c in player.battlefield:
                if c.is_creature():
                    if not (HAS_LAYERS_ENGINE and game.layers_engine):
                        c.power_modifier += max_power
                        c.toughness_modifier += max_power
                    # [LAYERS] Register Stampede pump as Layer 7c
                    if HAS_LAYERS_ENGINE and game.layers_engine:
                        _os_effs = create_pump_effect(
                            source_name="Overwhelming Stampede",
                            source_id=f"stampede_{c.id}",
                            controller=player.name, target_id=c.id,
                            power_mod=max_power, toughness_mod=max_power,
                            keywords=["Trample"], duration="end_of_turn")
                        for _pe in _os_effs:
                            game.layers_engine.add_effect(_pe)
                    if 'Trample' not in c.temp_keywords:
                        c.temp_keywords.append('Trample')
                    count += 1
            messages.append(f"💪 {count} creature(s) get +{max_power}/+{max_power} and trample until end of turn!")
        else:
            messages.append("⚠️ No creatures to pump (greatest power is 0)")
        return messages
    
    # Overrun-style: creatures get +X/+X and trample
    overrun_match = re.search(r'creatures you control get \+(\d+)/\+(\d+) and gain trample', oracle)
    if overrun_match:
        power_boost = int(overrun_match.group(1))
        tough_boost = int(overrun_match.group(2))
        count = 0
        for c in player.battlefield:
            if c.is_creature():
                if not (HAS_LAYERS_ENGINE and game.layers_engine):
                    c.power_modifier += power_boost
                    c.toughness_modifier += tough_boost
                # [LAYERS] Register overrun pump as Layer 7c
                if HAS_LAYERS_ENGINE and game.layers_engine:
                    _or_effs = create_pump_effect(
                        source_name="Overrun", source_id=f"overrun_{c.id}",
                        controller=player.name, target_id=c.id,
                        power_mod=power_boost, toughness_mod=tough_boost,
                        keywords=["Trample"], duration="end_of_turn")
                    for _pe in _or_effs:
                        game.layers_engine.add_effect(_pe)
                if 'Trample' not in c.temp_keywords:
                    c.temp_keywords.append('Trample')
                count += 1
        messages.append(f"💪 {count} creature(s) get +{power_boost}/+{tough_boost} and trample!")
        return messages
    
    # Generic mass pump: "creatures you control get +X/+X"
    pump_match = re.search(r'creatures you control get \+(\d+)/\+(\d+)', oracle)
    if pump_match:
        power_boost = int(pump_match.group(1))
        tough_boost = int(pump_match.group(2))
        count = 0
        for c in player.battlefield:
            if c.is_creature():
                if not (HAS_LAYERS_ENGINE and game.layers_engine):
                    c.power_modifier += power_boost
                    c.toughness_modifier += tough_boost
                # [LAYERS] Register generic mass pump as Layer 7c
                if HAS_LAYERS_ENGINE and game.layers_engine:
                    _gp_effs = create_pump_effect(
                        source_name=card.name, source_id=f"masspump_{c.id}",
                        controller=player.name, target_id=c.id,
                        power_mod=power_boost, toughness_mod=tough_boost,
                        duration="end_of_turn")
                    for _pe in _gp_effs:
                        game.layers_engine.add_effect(_pe)
                count += 1
        messages.append(f"💪 {count} creature(s) get +{power_boost}/+{tough_boost} until end of turn!")
        return messages
    
    # Delayed attack trigger (Jaya's -2 style)
    if "whenever you attack this turn" in oracle and "number of attacking creatures" in oracle:
        if target:
            game.turn_effects.append({
                "type": "on_attack_damage",
                "source": card.name,
                "target_id": target.id if hasattr(target, 'id') else None,
                "target_name": target.name if hasattr(target, 'name') else str(target),
                "calc": "num_attackers",
                "controller": player_idx
            })
            messages.append(f"🎯 {target.name} will take damage equal to attacking creatures when you attack!")
        return messages
    
    # Shamanic Revelation style (draw per creature + ferocious)
    if "draw a card for each creature you control" in oracle:
        creature_count = sum(1 for c in player.battlefield if c.is_creature())
        drawn = engine.draw_cards(player, creature_count, game=game)
        messages.append(f"🎴 {player.name} draws {len(drawn)} card(s)")

        if "with power 4 or greater" in oracle:
            big_creatures = sum(1 for c in player.battlefield
                              if c.is_creature() and c.get_effective_power(game) >= 4)
            if big_creatures > 0:
                life_gain = big_creatures * 4
                player.life += life_gain
                messages.append(f"💚 {player.name} gains {life_gain} life (life: {player.life})")
        return messages
    
    # "Draw cards equal to greatest power" (Rishkar's Expertise, Soul's Majesty, etc.)
    if ("draw" in oracle and "equal to" in oracle and
            ("greatest power" in oracle or "power" in oracle) and
            "creatures you control" in oracle):
        max_power = 0
        for c in player.battlefield:
            if c.is_creature():
                max_power = max(max_power, c.get_effective_power(game))
        if max_power > 0:
            drawn = engine.draw_cards(player, max_power, game=game)
            messages.append(f"🎴 {player.name} draws {len(drawn)} card(s) (greatest power = {max_power})")
        else:
            messages.append(f"⚠️ No creatures — no cards drawn")

        # Rishkar's Expertise: "you may cast a spell with mana value 5 or less without paying"
        if "cast" in oracle and "without paying" in oracle:
            mv_match = re.search(r'mana value (\d+) or less', oracle)
            max_mv = int(mv_match.group(1)) if mv_match else 5
            castable = [c for c in player.hand
                        if (c.cmc or 0) <= max_mv and not c.is_land()]
            if castable:
                messages.append(f"🆓 May cast a spell with MV {max_mv} or less for free! (Use `!play <card>` — mana cost will be waived)")
                # Set a flag so the next cast is free
                game.turn_effects.append({
                    "type": "free_cast",
                    "max_mv": max_mv,
                    "controller": player_idx,
                    "source": card.name,
                    "used": False
                })
        return messages

    # =================================================================
    # RAMP SPELLS (search library for lands)
    # =================================================================
    
    # Detect ramp spells: search library for land + put onto battlefield
    # Also handles "Forest card", "Island card" etc (Wood Elves, Nature's Lore)
    has_land_search = ("land" in oracle or "forest card" in oracle or "island card" in oracle or 
                      "plains card" in oracle or "mountain card" in oracle or "swamp card" in oracle)
    is_ramp = ("search your library" in oracle and has_land_search and 
               ("onto the battlefield" in oracle or "put that card onto" in oracle or 
                "put it onto" in oracle or "put them onto" in oracle))
    
    if is_ramp:
        # Skip if already resolved by template library (prevents double-resolution)
        recently_resolved = getattr(game, '_recently_resolved_spells', set())
        if card.name in recently_resolved:
            print(f"[RAMP] {card.name} already resolved by template library — skipping resolve_special_effects ramp handler")
            return messages
        # [PW-STATIC] Ashiok, Dream Render: opponents can't search their library
        blocker = engine.rules._opponent_prevents_library_search(game, player)
        if blocker:
            print(f"[PW-STATIC] {blocker} prevents {player.name} from searching their library (ramp spell: {card.name})")
            messages.append(f"🚫 {player.name} can't search their library ({blocker})")
            return messages
        # Determine what types of lands we can get
        can_get_basic = "basic" in oracle
        can_get_forest = "forest" in oracle
        can_get_island = "island" in oracle  
        can_get_plains = "plains" in oracle
        can_get_mountain = "mountain" in oracle
        can_get_swamp = "swamp" in oracle
        
        # Nature's Lore, Three Visits: "Forest card" (includes duals!)
        forest_card = "forest card" in oracle
        
        # How many lands?
        # Variable X: "up to X basic land cards, where X is the greatest power"
        # Handles Traverse the Outlands and similar
        if "greatest power" in oracle and ("up to x" in oracle or "where x is" in oracle):
            max_power = 0
            for c in player.battlefield:
                if c.is_creature():
                    p = c.get_effective_power(game)
                    max_power = max(max_power, p)
            num_lands = max(0, max_power)
            print(f"[RAMP] {card.name}: X = greatest power = {num_lands}")
        elif "two" in oracle or "up to two" in oracle:
            num_lands = 2
        elif "three" in oracle:
            num_lands = 3
        elif "four" in oracle or "up to four" in oracle:
            num_lands = 4
        else:
            num_lands = 1
        
        # Spell-level enters-tapped (Rampant Growth says "tapped", Nature's Lore says "untapped")
        spell_enters_tapped = "tapped" in oracle and "untapped" not in oracle
        if "untapped" in oracle:
            spell_enters_tapped = False

        # To hand instead? (Cultivate puts one to hand)
        one_to_hand = "put one onto the battlefield" in oracle and "into your hand" in oracle

        # Find matching lands
        found_lands = []
        for c in player.library[:]:  # Copy to avoid mutation during iteration
            if not c.is_land():
                continue

            type_line_lower = c.type_line.lower()
            name_lower = c.name.lower()

            # Check if this land matches the search criteria
            matches = False

            if can_get_basic and "basic" in type_line_lower:
                matches = True
            elif forest_card and "forest" in type_line_lower:
                # Forest card includes Tropical Island, Breeding Pool, etc.
                matches = True
            elif can_get_forest and ("forest" in name_lower or "forest" in type_line_lower):
                matches = True
            elif can_get_island and ("island" in name_lower or "island" in type_line_lower):
                matches = True
            elif can_get_plains and ("plains" in name_lower or "plains" in type_line_lower):
                matches = True
            elif can_get_mountain and ("mountain" in name_lower or "mountain" in type_line_lower):
                matches = True
            elif can_get_swamp and ("swamp" in name_lower or "swamp" in type_line_lower):
                matches = True
            elif not any([can_get_basic, can_get_forest, can_get_island, can_get_plains,
                         can_get_mountain, can_get_swamp, forest_card]):
                # Generic "land card" - any land works
                matches = True

            if matches:
                found_lands.append(c)
                if len(found_lands) >= num_lands:
                    break

        if found_lands:
            battlefield_lands = []
            hand_lands = []
            any_tapped = False

            for i, land in enumerate(found_lands):
                player.library.remove(land)

                # Cultivate: first to battlefield, second to hand
                if one_to_hand and i == len(found_lands) - 1 and len(found_lands) > 1:
                    player.hand.append(land)
                    hand_lands.append(land.name)
                else:
                    # Check land's own ETB-tapped + replacement effects, OR spell-level tapped
                    # June 11 audit: the message (shockland "pays 2 life") was
                    # discarded here, so life payments on fetched shocklands
                    # were console-only (game 1514629231433351168 turn 23).
                    land_tapped, _etb_msg = engine.rules._check_enters_tapped(game, land, player)
                    if _etb_msg:
                        messages.append(f"🩸 {land.name}{_etb_msg}")
                    enters_tapped = spell_enters_tapped or land_tapped
                    land.tapped = enters_tapped
                    land.entered_this_turn = True
                    player.battlefield.append(land)
                    battlefield_lands.append(land.name)
                    if enters_tapped:
                        any_tapped = True
                    # June 11 audit: landfall (Avenger of Zendikar, Omnath…)
                    # fired for ability-based fetches but NOT for this
                    # Kodama's Reach / Migration Path spell path — CR 603.2
                    # doesn't care how the land entered. Two missed Avenger
                    # triggers delayed a won game two full turns
                    # (game 1514626038192144445).
                    try:
                        _lf_msgs = engine._handle_land_etb(game, player, land)
                        if _lf_msgs:
                            messages.extend(_lf_msgs)
                    except (ValueError, KeyError, AttributeError, TypeError, IndexError) as _lf_err:
                        print(f"[LANDFALL] Error in ramp-spell land ETB: {_lf_err}")

            if battlefield_lands:
                tapped_str = " tapped" if any_tapped else ""
                messages.append(f"🌍 {player.name} puts {', '.join(battlefield_lands)} onto the battlefield{tapped_str}")
            if hand_lands:
                messages.append(f"✋ {player.name} puts {', '.join(hand_lands)} into their hand")
            
            # Shuffle library after searching
            import random
            random.shuffle(player.library)
            messages.append("📚 Library shuffled")
        else:
            messages.append("⚠️ No matching land found in library")
            import random
            random.shuffle(player.library)
        
        return messages
    
    # =================================================================
    # THUNDERMAW HELLKITE / TARGETED MASS DAMAGE (ETB)
    # "deals N damage to each creature with flying your opponents control" + tap
    # Must come BEFORE the generic mass damage handler to catch the qualified pattern
    # =================================================================
    if ("each creature with flying" in oracle and "opponents control" in oracle and
            re.search(r'deals?\s+(\d+)\s+damage', oracle)):
        dmg_match = re.search(r'deals?\s+(\d+)\s+damage', oracle)
        dmg = int(dmg_match.group(1))
        hit_count = 0
        should_tap = "tap those creatures" in oracle or "tap them" in oracle

        for p in game.players:
            if p == player:
                continue  # Only opponent's creatures
            for c in list(p.battlefield):
                all_kws = [kw.lower() for kw in (c.keywords or [])] + [kw.lower() for kw in (c.temp_keywords or [])]
                if c.is_creature() and 'flying' in all_kws:
                    c.damage_marked += dmg
                    if should_tap:
                        c.tapped = True
                    hit_count += 1

        tap_msg = " and taps them" if should_tap else ""
        messages.append(f"🔥 {card.name} deals {dmg} damage to {hit_count} creature(s) with flying opponents control{tap_msg}")
        return messages

    # =================================================================
    # MASS DAMAGE ("deals N damage to each creature" / "to each creature and each player")
    # Handles: Kozilek's Return, Anger of the Gods, Blasphemous Act, Pyroclasm, etc.
    # IMPORTANT: Only matches UNQUALIFIED "each creature" — NOT "each creature with flying
    # your opponents control" (Thundermaw) or other filtered variants. Those need their own
    # handlers above this point or in the ETB system.
    # =================================================================
    mass_dmg_match = re.search(r'deals?\s+(\d+)\s+damage\s+to\s+each\s+creature(?!\s+with\b)(?!\s+an?\s+opponent)', oracle)
    if mass_dmg_match:
        dmg = int(mass_dmg_match.group(1))
        hit_count = 0
        for p in game.players:
            for c in list(p.battlefield):
                if c.is_creature():
                    c.damage_marked += dmg
                    hit_count += 1
        messages.append(f"🔥 {card.name} deals {dmg} damage to each creature ({hit_count} hit)")
        # Also check "and each player" / "and each planeswalker"
        if "each player" in oracle:
            for p in game.players:
                actual_dmg = engine.rules._apply_noncombat_damage_to_player(game, p, dmg, card.name)
                if actual_dmg > 0:
                    messages.append(f"🔥 {card.name} deals {actual_dmg} damage to {p.name} ({p.life} life)")
        return messages

    # =================================================================
    # THROUGH THE BREACH / SNEAK ATTACK SPELLS
    # "Put a creature card from your hand onto the battlefield. It gains haste.
    #  Sacrifice it at the beginning of the next end step."
    # =================================================================
    if ("put" in oracle and "creature" in oracle and "from your hand" in oracle
            and "onto the battlefield" in oracle and "haste" in oracle and "sacrifice" in oracle):
        creatures_in_hand = [c for c in player.hand if c.is_creature()]
        if not creatures_in_hand:
            messages.append(f"⚠️ {card.name}: No creature cards in hand to put onto the battlefield.")
            return messages
        # Store pending action for creature selection
        game.pending_action = {
            'type': 'permanent_ability',
            'effect_type': 'sneak_creature',
            'card_id': card.id,
            'ability': {'effect': 'haste sacrifice'},
            'player_idx': player_idx,
        }
        lines = [f"🎭 **{card.name}** — Choose a creature from your hand to put onto the battlefield:"]
        for i, c in enumerate(creatures_in_hand):
            lines.append(f"  `{i}` - {c.name} ({c.power}/{c.toughness}) [{c.mana_cost}]")
        lines.append(f"\n*Reply with `!target <number>` to select*")
        messages.append("\n".join(lines))
        return messages

    return messages  # Empty = let SpellResolver handle it
