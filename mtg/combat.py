"""Combat math + damage application + life gain.

Eight free functions extracted from RulesEngine. Together they handle
the math of combat damage assignment (lethal/deathtouch/trample), the
side effects (lifelink, replacement effects), and a couple of related
helpers used by the action interpreter (life gain, counter aggregation).

Public free functions:

    resolve_combat_damage(rules, game)
        Top-level: resolves all combat damage for the current step.

    deal_combat_damage(rules, game, attackers, ...)
        Inner loop — assigns damage from each attacker to its blockers
        (or the defending player). Handles deathtouch + trample.

    apply_combat_damage_to_player(rules, game, source, player, amount)
        One source dealing N damage to a player. Lifelink + commander
        damage tracking. Routes through replacement effects.

    apply_combat_damage_to_creature(rules, game, source, target, amount)
        One source dealing N damage to a creature. Damage marker. Routes
        through replacement effects.

    apply_noncombat_damage_to_player(rules, game, source, player, amount)
        Damage from spells/abilities (Lightning Bolt etc.). Same shape
        as combat-to-player but used by the action interpreter.

    apply_life_gain(rules, game, player, amount, source=None)
        Life gain with replacement-effect routing + Soul-Warden-style
        chain triggering.

    make_replacement_callback(rules, game)
        Returns a callback closure that replacement effects use to apply
        their effect when they fire.

    aggregate_counter_msgs(rules, msgs)
        Collapses N consecutive "+1 counter on X" messages into one
        "+N counter on X" line.

State touched on `rules`:

    rules.engine_ref     — back-reference to GameEngine for trigger calls
    rules.game_log       — game event log

Internal cross-calls within this module go through the RulesEngine facade
(`rules.X`) so behavior is identical to pre-refactor.

Extracted from mtg/rules_engine.py during the Phase 2 OSS-readability
refactor (Phase 2D).
"""

from typing import Any, Dict, List, Optional, Tuple

from mtg.constants import Phase, Zone, COMMAND_ZONE_FORMATS
from mtg.models import Card, Player, GameState

# Optional: replacement effects (lifelink, prevention, conversion)
try:
    from rules.replacement import (
        ReplacementEngine, ReplacementEffect, GameEvent, EventType,
    )
    HAS_REPLACEMENT_ENGINE = True
except ImportError:
    HAS_REPLACEMENT_ENGINE = False

# Optional: layers (granted keywords like lifelink, deathtouch)
try:
    from rules.layers import Layer
    HAS_LAYERS_ENGINE = True
except ImportError:
    HAS_LAYERS_ENGINE = False

# Optional: Tier 1.5 effect templates (combat-damage triggers like Glissa)
try:
    from rules.effect_templates import get_effect_library, build_game_context
    HAS_EFFECT_TEMPLATES = True
except ImportError:
    HAS_EFFECT_TEMPLATES = False


def resolve_combat_damage(rules, game: GameState) -> List[str]:
    """
    Resolve combat damage with keyword abilities.
    Returns list of messages describing what happened.
    """
    messages = []
    lifelink_healing = {0: 0, 1: 0}  # player_index -> life to gain

    # Refresh layer cache before reading effective P/T. The cache is updated
    # at 22+ sites but a stale `_layers_power_mod` was reported in the Apr 28
    # audit (Sythis displayed power=5/2 with no visible source). Recalculating
    # right before reading effective power is cheap and forecloses any drift.
    try:
        game.recalculate_power_toughness()
    except Exception as e:
        print(f"[COMBAT] Layer recalc failed before damage step: {e}")
    
    # Separate first strike damage.
    # CR 510.5: the first-strike damage step fires if ANY attacker or
    # blocker in combat has first strike or double strike. A vanilla
    # attacker blocked by a double-strike blocker must still be processed
    # in the FS step so the blocker deals damage in both steps.
    first_strikers = []
    regular_attackers = []

    for attacker_id in game.attackers:
        result = game.find_card_global(attacker_id)
        if result:
            attacker, attacker_owner, _ = result
            attacker_has_fs_ds = attacker.has_first_strike(game=game) or attacker.has_double_strike(game=game)
            any_blocker_has_fs_ds = False
            for blocker_id in game.blockers.get(attacker_id, []) or []:
                bres = game.find_card_global(blocker_id)
                if bres and (bres[0].has_first_strike(game=game) or bres[0].has_double_strike(game=game)):
                    any_blocker_has_fs_ds = True
                    break
            if attacker_has_fs_ds or any_blocker_has_fs_ds:
                first_strikers.append((attacker, attacker_owner))
            if not attacker.has_first_strike(game=game) or attacker.has_double_strike(game=game):
                regular_attackers.append((attacker, attacker_owner))
    
    # First strike damage step
    if first_strikers:
        print(f"[COMBAT-STEP] FIRST-STRIKE step: {len(first_strikers)} attacker(s) with FS/DS involvement")
        messages.append("**First Strike Damage:**")
        fs_messages, fs_healing = rules._deal_combat_damage(game, first_strikers, is_first_strike_step=True)
        messages.extend(fs_messages)
        for idx, heal in fs_healing.items():
            lifelink_healing[idx] += heal

        # CR 119.3d / 702.15b: lifelink causes life gain SIMULTANEOUSLY with
        # the damage event, BEFORE state-based actions check. Apply pending
        # FS-step lifelink heals now so a lifelink attacker who takes lethal
        # simultaneous damage isn't lost to SBA before his life can rebound.
        # May 14 audit: was applied after SBA, which lost games where the
        # lifelink would have saved the controller (Rick at 0 with lifelink
        # attacker dealing damage that would have healed him above 0).
        for idx, heal in list(lifelink_healing.items()):
            if heal > 0:
                ok, actual_heal, chain = rules._apply_life_gain(
                    game, game.players[idx], heal, source_name="Lifelink"
                )
                if ok:
                    messages.append(f"💚 {game.players[idx].name} gains {actual_heal} life (lifelink)")
                    print(f"[LIFELINK] {game.players[idx].name}: +{actual_heal} life → {game.players[idx].life}")
                else:
                    messages.append(f"🚫 Lifelink prevented for {game.players[idx].name} ({', '.join(chain)})")
                    print(f"[LIFELINK-PREVENTED] {game.players[idx].name}: heal={heal} chain={','.join(chain)}")
                lifelink_healing[idx] = 0

        # Check SBAs after first strike — creatures that took lethal in FS
        # must die before the regular step so they can't swing back.
        sba_messages = rules.process_state_based_actions(game)
        if sba_messages:
            print(f"[COMBAT-STEP] FIRST-STRIKE SBAs fired: {len(sba_messages)} event(s)")
        messages.extend(sba_messages)
    else:
        print(f"[COMBAT-STEP] FIRST-STRIKE skipped (no FS/DS involvement)")

    # CR 704.5a: a player at ≤0 life loses; if SBAs fired during the FS step
    # ended the game, stop here — no regular damage step on a finished game.
    # (Still apply first-strike-step lifelink — CR 119.3d, the life gain is
    # part of the damage event that already happened — so the final scoreboard
    # is right. Must return a plain List[str]: callers iterate `for m in msgs`,
    # and returning a (messages, healing) tuple here once leaked the raw list
    # and dict into a Discord combat embed.)
    if getattr(game, 'ended', False):
        print(f"[COMBAT-STEP] Game ended after FS step — skipping regular damage")
        for idx, heal in lifelink_healing.items():
            if heal > 0:
                ok, actual_heal, chain = rules._apply_life_gain(
                    game, game.players[idx], heal, source_name="Lifelink"
                )
                if ok:
                    messages.append(f"💚 {game.players[idx].name} gains {actual_heal} life (lifelink)")
                    print(f"[LIFELINK] {game.players[idx].name}: +{actual_heal} life → {game.players[idx].life}")
        return messages

    # Regular damage step
    # BUG-10 fix: non-FS blockers always deal retaliatory damage in the regular step,
    # even when blocking first-strikers that already dealt damage in the FS step.
    if regular_attackers or first_strikers:
        print(f"[COMBAT-STEP] REGULAR step: {len(regular_attackers)} regular attacker(s)")
        if first_strikers:
            messages.append("**Regular Combat Damage:**")
        # Regular attackers deal their damage normally (attackers + their blockers)
        if regular_attackers:
            reg_messages, reg_healing = rules._deal_combat_damage(game, regular_attackers)
            messages.extend(reg_messages)
            for idx, heal in reg_healing.items():
                lifelink_healing[idx] += heal
        # First-strikers' non-FS blockers deal retaliatory damage (skip_attacker_damage=True
        # prevents first-strikers from dealing damage a second time — only blockers act here).
        # Only pure-FS attackers (first strike, not double strike) need this pass:
        # DS attackers and non-FS attackers blocked by DS creatures are already in
        # regular_attackers and had their full combat resolved above.
        # May 24 Tier-2 audit fix: also require attackers to actually have
        # blockers — unblocked first-strikers have nothing to retaliate to,
        # so running the pass was a no-op that still emitted a noisy
        # `[COMBAT-DAMAGE] Resolving regular-step blockers-retaliation`
        # console line that misled audit agents into thinking damage was
        # dealt twice. Empty filter → skip the call entirely.
        pure_fs_attackers = [
            (a, o) for (a, o) in first_strikers
            if a.has_first_strike(game=game) and not a.has_double_strike(game=game)
            and bool(game.blockers.get(a.id))
        ]
        if pure_fs_attackers:
            ret_messages, ret_healing = rules._deal_combat_damage(
                game, pure_fs_attackers, is_first_strike_step=False, skip_attacker_damage=True
            )
            messages.extend(ret_messages)
            for idx, heal in ret_healing.items():
                lifelink_healing[idx] += heal

    # Apply lifelink from regular step BEFORE the post-damage SBA check
    # (CR 119.3d / 702.15b — see note in FS step above). May 14 audit found
    # a corner case where simultaneous damage + lifelink heal would have
    # left the controller alive, but SBA fired before the heal applied.
    for idx, heal in list(lifelink_healing.items()):
        if heal > 0:
            ok, actual_heal, chain = rules._apply_life_gain(
                game, game.players[idx], heal, source_name="Lifelink"
            )
            if ok:
                messages.append(f"💚 {game.players[idx].name} gains {actual_heal} life (lifelink)")
                print(f"[LIFELINK] {game.players[idx].name}: +{actual_heal} life → {game.players[idx].life}")
            else:
                messages.append(f"🚫 Lifelink prevented for {game.players[idx].name} ({', '.join(chain)})")
                print(f"[LIFELINK-PREVENTED] {game.players[idx].name}: heal={heal} chain={','.join(chain)}")
            lifelink_healing[idx] = 0

    # Check SBAs after regular combat damage (kills creatures with lethal damage)
    # Without this, blockers survive combat with lethal damage marked
    sba_messages = rules.process_state_based_actions(game)
    messages.extend(sba_messages)

    # Fire combat damage triggers (Ancient Bronze Dragon, Quartzwood Crasher, etc.)
    combat_damage_dealt = getattr(game, '_combat_damage_to_player', [])
    if combat_damage_dealt and HAS_EFFECT_TEMPLATES:
        for attacker, attacker_owner, damage_amount in combat_damage_dealt:
            if not attacker.oracle_text:
                continue
            oracle_lower = attacker.oracle_text.lower()
            if "combat damage to a player" not in oracle_lower and "combat damage to an opponent" not in oracle_lower:
                continue
            try:
                opp_idx = 1 - game.players.index(attacker_owner)
                opp = game.players[opp_idx]
                ctx = build_game_context(game, attacker_owner, opp,
                                         card=attacker, attacking_creature=attacker)
                ctx['damage_dealt'] = damage_amount
                ctx['attacking_power'] = damage_amount
                lib = get_effect_library()
                actions, explanation = lib.resolve_attack_trigger(
                    trigger_card_name=attacker.name,
                    trigger_oracle=attacker.oracle_text,
                    attacking_creature_name=attacker.name,
                    attacking_creature_power=damage_amount,
                    controller=attacker_owner.name,
                    opponent=opp.name,
                    game_context=ctx,
                )
                if actions and any(a.get("action") != "no_action" for a in actions):
                    for action in actions:
                        if action.get("action") == "no_action":
                            continue
                        try:
                            msg = rules._execute_action_on_state(game, action)
                            if msg:
                                messages.append(f"💥 {attacker.name} combat damage trigger: {msg}")
                        except Exception as e:
                            print(f"[COMBAT-TRIGGER] Action failed for {attacker.name}: {e}")
                    print(f"[COMBAT-TRIGGER] {attacker.name}: {explanation}")
            except Exception as e:
                print(f"[COMBAT-TRIGGER] Error for {attacker.name}: {e}")
        game._combat_damage_to_player = []  # Clear after processing

    # Apply lifelink (single event per controller per damage step, CR 119.3d/702.15)
    for idx, heal in lifelink_healing.items():
        if heal > 0:
            ok, actual_heal, chain = rules._apply_life_gain(
                game, game.players[idx], heal, source_name="Lifelink"
            )
            if not ok:
                messages.append(f"🚫 Lifelink prevented for {game.players[idx].name} ({', '.join(chain)})")
                # Apr 30 audit: console mirror for log-based reconciliation.
                print(f"[LIFELINK-PREVENTED] {game.players[idx].name}: heal={heal} chain={','.join(chain)}")
                continue
            messages.append(f"💚 {game.players[idx].name} gains {actual_heal} life (lifelink)")
            # Apr 30 audit: console mirror so post-batch life-total reconciliation
            # doesn't go missing in the per-game console log (Discord-only events
            # broke the audit's math three times).
            print(f"[LIFELINK] {game.players[idx].name}: +{actual_heal} life → {game.players[idx].life}")

    return messages


def apply_life_gain(rules, game: GameState, player: 'PlayerState', amount: int,
                      source_name: str = "") -> Tuple[bool, int, List[str]]:
    """
    Apply a single life-gain event to a player after processing LIFE_GAIN
    replacement effects (Erebos, Sulfuric Vortex, Rhox Faithmender, etc.).
    Returns (applied, final_amount, replacement_chain). Applied=False means
    the gain was prevented and no life was added.
    Centralizing this makes the life-gain hook the single place to wire
    "whenever you gain life" triggers (Soul Warden, Ajani's Pridemate, etc.)
    when that subsystem comes online.
    """
    if amount <= 0:
        return True, 0, []
    final_amount = amount
    chain: List[str] = []
    if HAS_REPLACEMENT_ENGINE and game._replacement_engine and game._replacement_engine.effects:
        event = GameEvent(
            event_type=EventType.LIFE_GAIN,
            affected_player=player.name,
            amount=amount,
            source_name=source_name,
        )
        final = game._replacement_engine.process_event_sync(event)
        chain = list(getattr(final, 'replacement_chain', []) or [])
        if final.is_prevented:
            return False, 0, chain
        if final.amount != amount:
            print(f"  [REPLACEMENT-APPLY] Life gain modified: {amount} → {final.amount} ({', '.join(chain)})")
        final_amount = final.amount
    if final_amount <= 0:
        return True, 0, chain
    player.life += final_amount
    # Record the most recent life-gain amount for trigger scanners to consume.
    player._last_life_gain = final_amount
    return True, final_amount, chain


def apply_combat_damage_to_player(rules, game: GameState, player: 'PlayerState',
                                     amount: int, source_card: Card, is_combat: bool = True) -> int:
    """Apply damage to a player, processing replacement effects first. Returns final amount."""
    if amount <= 0:
        return 0
    # Damage prevention flag (Teferi's Protection, Fog, etc.)
    if getattr(player, '_damage_prevented', False):
        # Check turn-based expiration (Teferi's = next untap, Fog = end of turn)
        expires = getattr(player, '_damage_prevented_expires_turn', float('inf'))
        if game.turn_number >= expires:
            player._damage_prevented = False
            print(f"  [DAMAGE-PREVENTED] Expired for {player.name} (set turn expired)")
        else:
            print(f"  [DAMAGE-PREVENTED] {source_card.name} → {player.name}: {amount} damage prevented")
            return 0
    if HAS_REPLACEMENT_ENGINE and game._replacement_engine and game._replacement_engine.effects:
        # Populate source_controller so replacement effects like Fiery
        # Emancipation / Gisela / Dictate / Furnace can filter by who
        # controls the damage source (CR 614 house-rule: multipliers
        # only fire for damage from sources you control).
        source_controller_name = ""
        if hasattr(source_card, '_find_controller'):
            sctrl = source_card._find_controller(game)
            if sctrl is not None:
                source_controller_name = getattr(sctrl, 'name', "") or ""
        event = GameEvent(
            event_type=EventType.DAMAGE,
            affected_player=player.name,
            amount=amount,
            source_name=source_card.name,
            source_id=getattr(source_card, 'id', ''),
            source_controller=source_controller_name,
            is_combat_damage=is_combat,
            has_deathtouch=source_card.has_deathtouch() if hasattr(source_card, 'has_deathtouch') else False,
            has_lifelink=source_card.has_lifelink() if hasattr(source_card, 'has_lifelink') else False,
        )
        final = game._replacement_engine.process_event_sync(event)
        if final.is_prevented:
            print(f"  [REPLACEMENT-APPLY] Combat damage prevented: {source_card.name} → {player.name} ({', '.join(final.replacement_chain)})")
            return 0
        if final.amount != amount:
            print(f"  [REPLACEMENT-APPLY] Combat damage modified: {amount} → {final.amount} ({', '.join(final.replacement_chain)})")
        amount = final.amount

    # CR 702.90b — Infect: damage to a player is dealt as poison counters
    # instead of life loss. Detected via has_keyword('Infect') or oracle text.
    source_oracle = (getattr(source_card, 'oracle_text', '') or '').lower()
    has_infect = (
        (hasattr(source_card, 'has_keyword') and source_card.has_keyword('Infect'))
        or 'infect' in source_oracle
    )
    if has_infect:
        player.poison = getattr(player, 'poison', 0) + amount
        print(f"[POISON] {player.name} gets {amount} poison counter(s) from {source_card.name} "
              f"(infect; total: {player.poison})")
        # Commander damage from a commander with infect still counts toward 21 (CR 903.10a),
        # but the player doesn't lose life. Continue to commander-damage tracking below.
    else:
        player.life -= amount
        if amount > 0:
            # May 18 audit: clamp the displayed life at 0 so a multi-creature
            # combat-damage step against a low-life player doesn't print a
            # series of accumulating negatives ("life: -5 → -13 → -21 → -32")
            # that scared the user reading the log. The underlying state still
            # tracks the real (possibly-negative) life total — only the
            # display is clamped. PLAYER_LOSES_ZERO_LIFE SBA fires correctly
            # at the next state-based check regardless. CR 119.3 allows a
            # life total to go below 0 internally; this is purely a UX clamp.
            displayed_life = max(0, player.life)
            print(f"[COMBAT-LIFE] {player.name} takes {amount} combat damage from {source_card.name} (life: {displayed_life})")

    # CR 903.10a / 704.5b — Commander damage tracking. If the source is a
    # commander dealing combat damage to a player, accumulate per source-controller.
    # commander_damage maps source_controller_index -> total damage.
    if is_combat and getattr(source_card, 'is_commander', False):
        # Find the controller of the source (commander)
        source_controller_idx = None
        for idx, p in enumerate(game.players):
            if source_card in p.battlefield or source_card in getattr(p, 'command_zone', []):
                source_controller_idx = idx
                break
        if source_controller_idx is None and hasattr(source_card, '_find_controller'):
            ctrl = source_card._find_controller(game)
            if ctrl:
                for idx, p in enumerate(game.players):
                    if p is ctrl:
                        source_controller_idx = idx
                        break
        if source_controller_idx is not None:
            prior = player.commander_damage.get(source_controller_idx, 0)
            player.commander_damage[source_controller_idx] = prior + amount
            total = player.commander_damage[source_controller_idx]
            print(f"[COMMANDER-DAMAGE] {player.name} takes {amount} commander damage from "
                  f"{source_card.name} (total from player {source_controller_idx}: {total}/21)")
    return amount


def apply_combat_damage_to_creature(rules, game: GameState, creature: Card,
                                      amount: int, source_card: Card,
                                      source_has_deathtouch: bool = False) -> int:
    """Apply combat damage to a creature, processing replacement effects. Returns final amount."""
    if amount <= 0:
        return 0
    if HAS_REPLACEMENT_ENGINE and game._replacement_engine and game._replacement_engine.effects:
        # CR 614: replacement conditions like Gisela/Fiery Emancipation ("damage to
        # an opponent or a permanent an opponent controls") need affected_player set
        # to the CREATURE'S controller so the condition can compare against the
        # replacement effect's own controller.
        creature_controller_name = ""
        if hasattr(creature, '_find_controller'):
            ctrl = creature._find_controller(game)
            if ctrl is not None:
                creature_controller_name = getattr(ctrl, 'name', "") or ""
        source_controller_name = ""
        if hasattr(source_card, '_find_controller'):
            sctrl = source_card._find_controller(game)
            if sctrl is not None:
                source_controller_name = getattr(sctrl, 'name', "") or ""
        event = GameEvent(
            event_type=EventType.DAMAGE,
            affected_player=creature_controller_name,
            affected_object=getattr(creature, 'id', ''),
            affected_object_name=creature.name,
            amount=amount,
            source_name=source_card.name,
            source_id=getattr(source_card, 'id', ''),
            source_controller=source_controller_name,
            is_combat_damage=True,
            has_deathtouch=source_has_deathtouch,
        )
        final = game._replacement_engine.process_event_sync(event)
        if final.is_prevented:
            print(f"  [REPLACEMENT-APPLY] Creature damage prevented: {source_card.name} → {creature.name} ({', '.join(final.replacement_chain)})")
            return 0
        if final.amount != amount:
            print(f"  [REPLACEMENT-APPLY] Creature damage modified: {amount} → {final.amount} ({', '.join(final.replacement_chain)})")
        amount = final.amount
    creature.damage_marked += amount
    # Track deathtouch damage separately for SBA 704.5h
    if source_has_deathtouch and amount > 0:
        creature.deathtouch_damage += amount
    return amount


def apply_noncombat_damage_to_player(rules, game: GameState, player: 'PlayerState',
                                       amount: int, source_name: str = "",
                                       source_id: str = "") -> int:
    """Apply non-combat damage to a player, processing replacement effects. Returns final amount."""
    if amount <= 0:
        return 0
    # Fallback damage prevention flag (when replacement engine not available)
    if getattr(player, '_damage_prevented', False):
        expires = getattr(player, '_damage_prevented_expires_turn', float('inf'))
        if game.turn_number >= expires:
            player._damage_prevented = False
            print(f"  [DAMAGE-PREVENTED] Expired for {player.name} (set turn expired)")
        else:
            print(f"  [DAMAGE-PREVENTED] {source_name} → {player.name}: {amount} noncombat damage prevented")
            return 0
    if HAS_REPLACEMENT_ENGINE and game._replacement_engine and game._replacement_engine.effects:
        # Try to find the controller of the source by id or name so the
        # replacement layer can filter by source_controller (Fiery
        # Emancipation, Gisela, etc. — see _apply_combat_damage_to_player).
        source_controller_name = ""
        if source_id or source_name:
            for _p in game.players:
                for _c in _p.battlefield:
                    if (source_id and getattr(_c, 'id', '') == source_id) or \
                       (source_name and _c.name == source_name):
                        source_controller_name = _p.name
                        break
                if source_controller_name:
                    break
        event = GameEvent(
            event_type=EventType.DAMAGE,
            affected_player=player.name,
            amount=amount,
            source_name=source_name,
            source_id=source_id,
            source_controller=source_controller_name,
            is_combat_damage=False,
        )
        final = game._replacement_engine.process_event_sync(event)
        if final.is_prevented:
            print(f"  [REPLACEMENT-APPLY] Noncombat damage prevented: {source_name} → {player.name} ({', '.join(final.replacement_chain)})")
            return 0
        if final.amount != amount:
            print(f"  [REPLACEMENT-APPLY] Noncombat damage modified: {amount} → {final.amount} ({', '.join(final.replacement_chain)})")
        amount = final.amount
    player.life -= amount
    if amount > 0:
        # May 18 audit: clamp displayed life at 0 (same rationale as [COMBAT-LIFE]).
        print(f"[NONCOMBAT-LIFE] {player.name} takes {amount} noncombat damage from {source_name} (life: {max(0, player.life)})")
    return amount


def make_replacement_callback(rules, game: GameState, channel=None):
    """Create an async callback for player choice when multiple replacement effects compete.

    Returns a coroutine that:
    1. Sets game.pending_action with type 'choose_replacement'
    2. Sends a choice prompt to the Discord channel (if provided)
    3. Awaits the player's !replacement command via asyncio.Future
    4. Returns the chosen ReplacementEffect

    Used as choose_callback for ReplacementEngine.process_event() (async path).
    """
    import asyncio

    async def callback(chooser: str, effects):
        # Create a future to wait for the player's choice
        loop = asyncio.get_event_loop()
        future = loop.create_future()

        # Determine which player is choosing
        player_idx = 0
        for i, p in enumerate(game.players):
            if p.name.lower() == chooser.lower():
                player_idx = i
                break

        # Build the choice list
        effect_descs = []
        for i, eff in enumerate(effects):
            effect_descs.append(f"  `{i}` - {eff.source_name}: {eff.description}")

        game.pending_action = {
            'type': 'choose_replacement',
            'player_idx': player_idx,
            'effects': effects,
            'future': future,
        }

        # Send prompt if channel is available
        if channel:
            lines = [f"⚡ **Multiple replacement effects can apply.** {chooser}, choose one:"]
            lines.extend(effect_descs)
            lines.append(f"\n*Reply with `!replacement <number>` to select*")
            await channel.send("\n".join(lines))

        # Wait for player choice
        chosen_effect = await future
        return chosen_effect

    return callback


def deal_combat_damage(rules, game: GameState, attackers: List[Tuple[Card, Player]], is_first_strike_step: bool = False, skip_attacker_damage: bool = False) -> Tuple[List[str], Dict[int, int]]:
    """
    Deal combat damage for a set of attackers.
    Returns (messages, lifelink_healing_by_player_index)
    skip_attacker_damage=True: blockers still deal retaliatory damage but attackers don't
    deal damage (used in regular step when all attackers already dealt FS damage).
    """
    messages = []
    lifelink_healing = {0: 0, 1: 0}
    defending_player_idx = 1 - game.active_player_index
    defending_player = game.players[defending_player_idx]

    # May 25 audit (F19): build a name-disambiguation table for this combat
    # resolution so "Thassa, Deep-Dwelling deals 6 damage to Thassa, Deep-Dwelling"
    # disambiguates as "Thassa, Deep-Dwelling #1 deals 6 damage to Thassa,
    # Deep-Dwelling #2". The block-declaration path in autoplay.py and cog.py
    # builds the same shape locally (via `_label_for`) but those labels were
    # never propagated through to the combat-damage emit. Build once per
    # `resolve_combat_damage` call, keyed by card.id. Only assign #N when a
    # name appears 2+ times among the participants — single occurrences keep
    # the bare name.
    _participants_by_name: Dict[str, List[str]] = {}  # name → [card_id, ...]
    for atk, _own in attackers:
        _participants_by_name.setdefault(atk.name, []).append(atk.id)
    for _atk_id, _blk_ids in (game.blockers or {}).items():
        for _bid in _blk_ids or []:
            _br = game.find_card_global(_bid)
            if _br:
                _participants_by_name.setdefault(_br[0].name, []).append(_bid)
    _disambig: Dict[str, str] = {}  # card_id → display label
    for _nm, _ids in _participants_by_name.items():
        if len(_ids) > 1:
            for _i, _cid in enumerate(_ids, start=1):
                _disambig[_cid] = f"{_nm} #{_i}"

    def _dispname(card):
        """Return name + #N when the same name appears 2+ times this combat."""
        return _disambig.get(card.id, card.name)

    # May 20 audit (#17): tag retaliation passes distinctly so the regular
    # step's `Resolving for 1 attacker(s)` from a pure-first-striker
    # retaliation isn't mistaken for a duplicate normal-attack iteration
    # (game_1506608500023754882:2251-2253). The skip_attacker_damage=True
    # branch is the regular-step blockers-only retaliation pass following a
    # first-strike step where the attacker already dealt damage.
    _phase_tag = (
        "FS step" if is_first_strike_step
        else ("regular-step blockers-retaliation" if skip_attacker_damage
              else "regular step")
    )
    print(f"[COMBAT-DAMAGE] Resolving {_phase_tag} for {len(attackers)} attacker(s). "
          f"game.blockers={game.blockers}")

    for attacker, attacker_owner in attackers:
        # CR 510.1c: only creatures still on the battlefield deal combat damage.
        # Apr 30 audit: a creature that died in the first-strike SBA pass was
        # still dealing 2 damage in the regular step (Trinket Mage post-mortem).
        # Skip attackers no longer on the battlefield.
        if attacker not in attacker_owner.battlefield:
            print(f"[COMBAT-DAMAGE] Skipping {attacker.name} — no longer on battlefield (died in first-strike step)")
            continue

        # Use get_effective_power which includes base, counters, modifiers,
        # equipment, CDA resolution, and anthem/continuous effects
        attacker_power = attacker.get_effective_power(game)

        if attacker_power <= 0:
            print(f"[COMBAT-DAMAGE] {attacker.name} has 0 power, deals no damage (skipped)")
            continue

        blocker_ids = game.blockers.get(attacker.id, [])

        # Also check by blocked_by attribute on the card itself as a fallback
        if not blocker_ids and attacker.blocked_by:
            print(f"[COMBAT-DAMAGE] WARNING: game.blockers missing for {attacker.name} (id={attacker.id}) but card.blocked_by={attacker.blocked_by}")
            blocker_ids = attacker.blocked_by

        print(f"[COMBAT-DAMAGE] {attacker.name} (id={attacker.id}, power={attacker_power}): blocker_ids={blocker_ids}")

        if not blocker_ids:
            # Unblocked - damage to defending player (with replacement effect processing)
            if skip_attacker_damage:
                # Regular step for FS-only board: unblocked FS attackers already dealt damage
                continue
            # CR 510.5: a non-FS/DS attacker deals no damage in the FS step.
            if is_first_strike_step and not (attacker.has_first_strike(game=game) or attacker.has_double_strike(game=game)):
                continue
            actual_damage = rules._apply_combat_damage_to_player(game, defending_player, attacker_power, attacker)
            if actual_damage > 0:
                # May 16 audit: append running life total so players can follow
                # the combat math live without scrolling for next !state. Burn
                # spell damage already does this (`(life: 4)` format); combat
                # damage was silent on 121/122 games in the May 15 batch.
                messages.append(
                    f"⚔️ {attacker.name} deals {actual_damage} damage to "
                    f"{defending_player.name} (life: {max(0, defending_player.life)})"
                )
                # Track for combat damage triggers (Ancient Bronze Dragon, etc.)
                if not hasattr(game, '_combat_damage_to_player'):
                    game._combat_damage_to_player = []
                game._combat_damage_to_player.append((attacker, attacker_owner, actual_damage))
            else:
                messages.append(f"🛡️ {attacker.name}'s damage to {defending_player.name} was prevented")

            if attacker.has_lifelink(game=game) and actual_damage > 0:
                owner_idx = game.active_player_index
                lifelink_healing[owner_idx] += actual_damage
                print(f"[LIFELINK] {attacker.name} unblocked → {game.players[owner_idx].name} queued +{actual_damage}")
        
        else:
            # Blocked - damage assignment
            remaining_damage = attacker_power
            trample_damage = 0
            if attacker.has_trample(game=game):
                print(f"[TRAMPLE-MATH] {attacker.name}: power={attacker_power}, blockers={len(blocker_ids)}")

            # May 20 audit (Combat G1): CR 510.1d gives the ATTACKING player
            # the right to choose the damage assignment order across multi-
            # blockers. Previously the code iterated `game.blockers[attacker_id]`
            # in raw insertion order — which is the BLOCKER's chosen order, not
            # the attacker's. Heuristic that approximates optimal attacker
            # choice: sort blockers by effective toughness ASCENDING so the
            # smallest die first (front-loading lethal). Maximizes kills per
            # power. Same heuristic with deathtouch+multi-blocker is moot
            # because each blocker only gets 1 damage anyway.
            # TODO (React frontend): once an interactive priority window
            # exists, delegate the order to the attacker's controller via a
            # decide_damage_order AI call. For autoplay, the toughness-asc
            # heuristic is CR-permissible (any order is legal — this just
            # picks a smart one).
            if len(blocker_ids) > 1:
                def _blocker_sort_key(bid):
                    res = game.find_card_global(bid)
                    if not res:
                        return (999, bid)
                    bc, _bo, _bz = res
                    try:
                        bt = bc.get_effective_toughness(game)
                    except Exception:
                        bt = getattr(bc, 'toughness', 1) or 1
                    return (bt, bid)
                blocker_ids = sorted(blocker_ids, key=_blocker_sort_key)
                print(f"[COMBAT-ASSIGNMENT-ORDER] {attacker.name} damage-order: "
                      f"{[game.find_card_global(b)[0].name + f'(t={game.find_card_global(b)[0].get_effective_toughness(game)})' for b in blocker_ids if game.find_card_global(b)]}")

            for blocker_id in blocker_ids:
                result = game.find_card_global(blocker_id)
                if not result:
                    continue
                blocker, blocker_owner, zone = result
                # Skip blockers that died in first strike step (moved to graveyard/exile/command zone)
                if zone != Zone.BATTLEFIELD:
                    print(f"[COMBAT-DAMAGE] Skipping {blocker.name} — no longer on battlefield (zone={zone.name})")
                    continue

                # Use centralized get_effective methods (includes base, CDA, counters,
                # modifiers, equipment, anthems, and layers engine)
                blocker_power = blocker.get_effective_power(game)
                blocker_toughness = blocker.get_effective_toughness(game)

                # Attacker damages blocker (with replacement effect processing)
                # skip_attacker_damage=True: attacker already dealt damage in FS step,
                # so we only process blocker retaliation here.
                # CR 510.5: in the FS step, only creatures with first strike or
                # double strike deal damage. A non-FS/DS attacker blocked by an FS/DS
                # blocker is in first_strikers only so the blocker can fire; the
                # attacker itself must skip damage until the regular step.
                actual_dmg = 0
                attacker_can_damage_now = not skip_attacker_damage
                if is_first_strike_step and not (attacker.has_first_strike(game=game) or attacker.has_double_strike(game=game)):
                    attacker_can_damage_now = False
                if attacker_can_damage_now:
                    if attacker.has_deathtouch(game=game):
                        # Deathtouch damage assignment.
                        #
                        # May 20 audit (Combat G3): single-blocker case with BOTH
                        # deathtouch AND trample previously assigned all power to
                        # the lone blocker (wasting trample-to-player damage). Per
                        # CR 702.19c, with deathtouch any non-zero damage is
                        # lethal, so a 6-power DT+trample attacker into a 2/2
                        # blocker should assign 1 to the blocker (lethal via DT)
                        # and 5 to the defending player (trample), not 6 to the
                        # blocker with 4 wasted.
                        #
                        # May 25 audit (F22): the old `elif len(blocker_ids) == 1
                        # or not has_trample_dt` wrongly matched MULTI-blocker
                        # DT-no-trample too — Glissa, the Traitor (3 power, DT)
                        # blocked by Luminarch Aspirant (1/1) + Stoneforge Mystic
                        # (1/2) assigned all 3 to the first blocker and 0 to the
                        # second, leaving Stoneforge alive to retaliate. Per
                        # CR 702.2c a DT attacker assigns AT LEAST 1 to each
                        # blocker; with DT, 1 IS lethal, so the optimal
                        # assignment is 1 per blocker with the remainder either
                        # trampling (if trample) or wasted (if not). New rule:
                        # single-blocker no-trample dumps all damage (preserves
                        # the lifelink/Fiery Emancipation math from the May 7
                        # non-DT fix); every other DT case assigns min(1, rem).
                        has_trample_dt = attacker.has_trample(game=game)
                        if len(blocker_ids) == 1 and not has_trample_dt:
                            # Single blocker + DT + no trample: still dump all
                            # damage to the blocker so lifelink + replacement
                            # effects (Furnace of Rath etc.) see the full event.
                            damage_to_blocker = remaining_damage
                        else:
                            # Single + trample: 1 to blocker, rest tramples.
                            # Multi + trample: 1 per blocker, rest tramples after.
                            # Multi + no trample: 1 per blocker, excess wasted.
                            damage_to_blocker = min(1, remaining_damage)
                        actual_dmg = rules._apply_combat_damage_to_creature(game, blocker, damage_to_blocker, attacker, source_has_deathtouch=True)
                        remaining_damage -= damage_to_blocker  # Use original for trample math
                        # deathtouch_damage is set by _apply_combat_damage_to_creature;
                        # SBA checks deathtouch_damage > 0 separately (no need to inflate damage_marked)
                        messages.append(f"☠️ {_dispname(attacker)} deals {actual_dmg} deathtouch damage to {_dispname(blocker)}")
                        # Skip the generic damage line for deathtouch (was duplicated)
                    else:
                        # Normal (non-deathtouch) damage assignment.
                        # CR 702.19 / 510.1: without trample, an attacker must
                        # assign damage equal to its power to creatures
                        # blocking it. With a single blocker (and no trample),
                        # ALL of the attacker's power goes to that blocker —
                        # not just the lethal-clamped portion.
                        #
                        # The May 7 audit caught this via Fiery Emancipation:
                        # Surrak (6 power) blocked by a 4-toughness Germ was
                        # raising a 4-damage event (clamped to lethal), which
                        # FE tripled to 12. The correct event is the full 6
                        # damage, which FE triples to 18 — and lifelink would
                        # then credit 18 instead of 12.
                        is_only_blocker = len(blocker_ids) == 1
                        has_trample = attacker.has_trample(game=game)
                        if is_only_blocker and not has_trample:
                            damage_to_blocker = remaining_damage  # Full assignment
                        else:
                            # Trample: assign minimum lethal here, excess flows
                            # to next blocker / player below.
                            # Multi-blocker no-trample: assign minimum lethal
                            # per blocker; the last blocker absorbs the
                            # remainder (handled implicitly by the loop end —
                            # the final blocker's `damage_needed` is bounded
                            # by `remaining_damage`).
                            damage_needed = blocker_toughness - blocker.damage_marked
                            damage_to_blocker = min(damage_needed, remaining_damage)
                            # Multi-blocker no-trample correction: if this is
                            # the LAST blocker and we have leftover power, dump
                            # the remainder here. (Without trample, the excess
                            # is still assigned, even though it's wasted past
                            # lethal — and replacement effects like Fiery
                            # Emancipation need to see the full event amount.)
                            if not has_trample and blocker_id == blocker_ids[-1]:
                                damage_to_blocker = remaining_damage
                        actual_dmg = rules._apply_combat_damage_to_creature(game, blocker, damage_to_blocker, attacker)
                        remaining_damage -= damage_to_blocker  # Use original for trample math
                        # Display attacker's full power (not clamped amount) for clarity
                        display_dmg = min(attacker_power, remaining_damage + damage_to_blocker) if len(blocker_ids) == 1 else actual_dmg
                        messages.append(f"⚔️ {_dispname(attacker)} deals {display_dmg} damage to {_dispname(blocker)}")
                
                # Blocker damages attacker (with replacement effect processing)
                # In first strike step, blockers without first strike/double strike don't deal damage
                blocker_deals_damage = blocker_power > 0
                if is_first_strike_step and not (blocker.has_first_strike(game=game) or blocker.has_double_strike(game=game)):
                    blocker_deals_damage = False
                if blocker_deals_damage:
                    actual_blocker_dmg = rules._apply_combat_damage_to_creature(
                        game, attacker, blocker_power, blocker,
                        source_has_deathtouch=blocker.has_deathtouch(game=game)
                    )
                    if blocker.has_deathtouch(game=game):
                        # deathtouch_damage is set by _apply_combat_damage_to_creature;
                        # SBA checks deathtouch_damage > 0 separately
                        messages.append(f"☠️ {_dispname(blocker)} deals {actual_blocker_dmg} deathtouch damage to {_dispname(attacker)}")
                    else:
                        messages.append(f"🛡️ {_dispname(blocker)} deals {actual_blocker_dmg} damage to {_dispname(attacker)}")

                    if blocker.has_lifelink(game=game) and actual_blocker_dmg > 0:
                        blocker_owner_idx = 1 - game.active_player_index
                        lifelink_healing[blocker_owner_idx] += actual_blocker_dmg
                        print(f"[LIFELINK] {blocker.name} blocking → {game.players[blocker_owner_idx].name} queued +{actual_blocker_dmg}")

                if attacker.has_lifelink(game=game) and actual_dmg > 0:
                    owner_idx = game.active_player_index
                    lifelink_healing[owner_idx] += actual_dmg
                    print(f"[LIFELINK] {attacker.name} → blocker → {game.players[owner_idx].name} queued +{actual_dmg}")

            # Trample - remaining damage to defending player (with replacement effect processing)
            # skip_attacker_damage=True: trample was already resolved in the FS step, skip here
            if skip_attacker_damage:
                continue
            # CR 510.5: non-FS/DS attacker's trample damage waits for the regular step.
            if is_first_strike_step and not (attacker.has_first_strike(game=game) or attacker.has_double_strike(game=game)):
                continue
            if attacker.has_trample(game=game):
                print(f"[TRAMPLE-MATH] {attacker.name}: remaining_damage={remaining_damage} after blockers (power was {attacker_power})")
            if remaining_damage > attacker_power:
                # Sanity clamp: trample can't exceed attacker's power.
                # Using effective toughness avoids ValueError on "*" toughness strings.
                print(f"[TRAMPLE-BUG] remaining_damage {remaining_damage} > attacker_power {attacker_power}! Clamping.")
                remaining_damage = attacker_power
            if remaining_damage > 0 and attacker.has_trample(game=game):
                actual_trample = rules._apply_combat_damage_to_player(game, defending_player, remaining_damage, attacker)
                if actual_trample > 0:
                    messages.append(
                        f"🦏 {attacker.name} tramples for {actual_trample} damage to "
                        f"{defending_player.name} (life: {max(0, defending_player.life)})"
                    )
                    # Track for combat damage triggers
                    if not hasattr(game, '_combat_damage_to_player'):
                        game._combat_damage_to_player = []
                    game._combat_damage_to_player.append((attacker, attacker_owner, actual_trample))
                else:
                    messages.append(f"🛡️ {attacker.name}'s trample damage to {defending_player.name} was prevented")
                if attacker.has_lifelink(game=game) and actual_trample > 0:
                    owner_idx = game.active_player_index
                    lifelink_healing[owner_idx] += actual_trample
                    print(f"[LIFELINK] {attacker.name} trample → {game.players[owner_idx].name} queued +{actual_trample}")

    # May 14 audit (L3): when 18 Plant tokens with the same name swing for 5
    # damage each, emit "18× Plant deals 5 damage to Rick (90 total)" instead
    # of 18 sequential identical lines. Only collapses unblocked-attacker
    # damage to the defending player (the only place per-token-name spam
    # actually piles up; blocked combat is more interesting per-attacker).
    messages = _collapse_repeated_combat_damage(messages)
    return messages, lifelink_healing


def _collapse_repeated_combat_damage(messages: List[str]) -> List[str]:
    """Collapse runs of identical "⚔️ X deals N damage to Y" lines.

    Three identical Plant tokens dealing 5 damage to Rick become:
      "⚔️ 3× Plant deals 5 damage to Rick (15 total)"

    Single attackers and unique attackers are passed through unchanged.
    """
    import re as _re
    # May 16 audit: pattern now captures optional " (life: N)" running-total
    # suffix (added to combat damage lines so players can see life draining
    # turn-by-turn). When collapsing identical runs, we keep the LAST line's
    # life total since that reflects the cumulative state after all hits.
    pattern = _re.compile(
        r'^⚔️ (?P<atk>.+?) deals (?P<dmg>\d+) damage to (?P<def>.+?)'
        r'(?:\s+\(life:\s*(?P<life>\d+)\))?$'
    )
    collapsed: List[str] = []
    run_key: Optional[Tuple[str, int, str]] = None
    run_count = 0
    run_last_life: Optional[str] = None
    for line in messages:
        m = pattern.match(line)
        if m:
            key = (m.group('atk'), int(m.group('dmg')), m.group('def'))
            life = m.group('life')
            if key == run_key:
                run_count += 1
                run_last_life = life  # Always reflect the latest hit's life total
                continue
            # Flush prior run before starting a new one
            if run_key is not None and run_count > 0:
                _flush_combat_run(collapsed, run_key, run_count, run_last_life)
            run_key = key
            run_count = 1
            run_last_life = life
        else:
            if run_key is not None and run_count > 0:
                _flush_combat_run(collapsed, run_key, run_count, run_last_life)
                run_key = None
                run_count = 0
                run_last_life = None
            collapsed.append(line)
    if run_key is not None and run_count > 0:
        _flush_combat_run(collapsed, run_key, run_count, run_last_life)
    return collapsed


def _flush_combat_run(out: List[str], key: Tuple[str, int, str], count: int,
                       last_life: Optional[str] = None) -> None:
    atk, dmg, defender = key
    life_suffix = f" (life: {last_life})" if last_life is not None else ""
    if count == 1:
        out.append(f"⚔️ {atk} deals {dmg} damage to {defender}{life_suffix}")
    else:
        out.append(
            f"⚔️ {count}× {atk} deals {dmg} damage to {defender} "
            f"({count * dmg} total){life_suffix}"
        )

# =========================================================================
# CLAUDE-ASSISTED JUDGING (Medium/Hard)
# =========================================================================


def aggregate_counter_msgs(rules, msgs: List[str]) -> List[str]:
    """Collapse a long cascade of '⭕ N +1/+1 counter(s) on **X** (total: T)' messages
    into one summary line per (card, counter_type), preserving non-counter messages
    intact and order. Used when Cathar's Crusade or similar enters-trigger fires per
    token created in an Avenger of Zendikar / Scute Swarm cascade."""
    import re
    pattern = re.compile(r"^⭕ (\d+) ([+\-/0-9]+) counter\(s\) on \*\*([^*]+)\*\* \(total: (\d+)\)$")
    # totals[(card, ctype)] = (total_added, latest_total)
    totals: Dict[Tuple[str, str], Tuple[int, int]] = {}
    order: List[Tuple[str, str]] = []
    passthrough: List[str] = []
    for m in msgs:
        mt = pattern.match(m.strip())
        if mt:
            added = int(mt.group(1))
            ctype = mt.group(2)
            card = mt.group(3)
            latest = int(mt.group(4))
            key = (card, ctype)
            if key not in totals:
                totals[key] = (added, latest)
                order.append(key)
            else:
                prev_added, _ = totals[key]
                totals[key] = (prev_added + added, latest)
        else:
            passthrough.append(m)
    if not totals:
        return msgs
    summary_lines = []
    for key in order:
        card, ctype = key
        added, latest = totals[key]
        summary_lines.append(
            f"⭕ +{added} {ctype} counter(s) on **{card}** (total: {latest})"
        )
    return passthrough + summary_lines
