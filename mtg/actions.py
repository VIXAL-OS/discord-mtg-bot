"""Action interpreter — executes JSON-format actions against a GameState.

The single biggest extraction of the Phase 2 OSS-readability refactor.
This was previously the `_execute_action_on_state` method of RulesEngine,
~2300 lines dispatching on 81+ action types like:

    {"action": "deal_damage", "amount": N, "target_player": "name"}
    {"action": "draw_cards", "player": "name", "amount": N}
    {"action": "create_token", "player": "name", "name": "...", ...}

It's now a free function `execute_action_on_state(rules, game, action)`
that takes the RulesEngine instance as its first argument. RulesEngine
keeps a thin `_execute_action_on_state` method that delegates here, so
existing callers (`rules._execute_action_on_state(game, action)`) work
unchanged.

Why a free function instead of a method:

    - Cleanly separates "the dispatch logic" from "the engine's stateful
      coordination" (which stays in rules_engine.py).
    - Makes it possible to grep / browse just the action vocabulary
      without scrolling past 2k+ lines of unrelated engine code.
    - Phase 2B / 2C / 2D will use the same pattern for SBA, judge, combat.

The function still uses `rules.X` for the few RulesEngine attributes /
methods it needs:

    rules.engine_ref     — back-reference to GameEngine for trigger calls
    rules.log_event      — game event logger
    rules._aggregate_counter_msgs, rules._apply_life_gain,
    rules._apply_noncombat_damage_to_player,
    rules._opponent_prevents_library_search

Extracted from mtg/rules_engine.py during the Phase 2 OSS-readability
refactor (Phase 2A — the action interpreter extraction).
"""

import random
import re
from typing import Any, Dict, List, Optional, Tuple

from mtg.constants import Phase, Zone, COMMAND_ZONE_FORMATS
from mtg.models import Card, Player, GameState
from mtg import events
from mtg import helpers
# Slice 3a (July 21): every death queue goes through the choke-point
# (append + CREATURE_DIED shadow emit).
from mtg.triggers import queue_deaths as _queue_deaths_3a
from mtg.util import maybe_reraise

# Optional: 7-layer continuous effects (CR 613) — used by pump/control/copy actions
try:
    from rules.layers import (
        Layer, create_pump_effect, create_control_effect, create_copy_effect,
    )
    HAS_LAYERS_ENGINE = True
except ImportError:
    HAS_LAYERS_ENGINE = False

# Optional: "if would, instead" replacement effects — used by damage/draw/etc.
try:
    from rules.replacement import (
        ReplacementEngine, ReplacementEffect, GameEvent, EventType,
    )
    HAS_REPLACEMENT_ENGINE = True
except ImportError:
    HAS_REPLACEMENT_ENGINE = False

# Optional: pre-execution target legality checks
try:
    from rules.targeting_helpers import (
        _validate_target_for_action, _validate_player_target_for_action,
    )
    HAS_TARGETING = True
except ImportError:
    HAS_TARGETING = False


_BREADCRUMB_RE = re.compile(r'^\s*↳\s*[^:]*:\s*')


def _strip_flicker_breadcrumb(msg: str) -> str:
    """Remove a leading "↳ <source>:" / "↳ flicker:" breadcrumb prefix.

    Nested flicker chains (Conjurer's Closet + Thassa + Soulherder + Aminatou)
    used to print one breadcrumb per layer per line; ~500 stacked prefixes were
    observed in a single Discord message. Stripping the prefix when the message
    is consumed by an outer flicker prevents the visual stack-up while keeping
    the *outermost* breadcrumb (the one this layer adds) intact.
    """
    if not msg or '↳' not in msg:
        return msg
    # Strip up to two leading breadcrumbs (handles already-stripped messages
    # that picked up a fresh one from a deeper recursive call).
    out = _BREADCRUMB_RE.sub('', msg, count=1)
    out = _BREADCRUMB_RE.sub('', out, count=1)
    return out


def _format_destroyed_list(names: List[str], max_chars: int = 350) -> str:
    """Format a list of destroyed permanent names for a Discord message.

    Compresses duplicates ("3× Plant Token, 2× Soldier, Blood Artist...") so
    huge token-heavy wipes don't dump 30+ identical names. Caps the output
    at max_chars and signals truncation with "(+N more)". Replaces the old
    `dest[:8] + '...'` truncation which hid which permanents actually
    survived a wipe (May 14 audit).
    """
    if not names:
        return ""
    # Preserve first-occurrence order so the message still reads naturally
    counts: Dict[str, int] = {}
    order: List[str] = []
    for n in names:
        if n not in counts:
            order.append(n)
            counts[n] = 0
        counts[n] += 1
    parts: List[str] = []
    for n in order:
        c = counts[n]
        parts.append(f"{c}× {n}" if c > 1 else n)
    rendered = ", ".join(parts)
    if len(rendered) <= max_chars:
        return rendered
    # Truncate at a part boundary to avoid mid-name cuts
    out_parts: List[str] = []
    used = 0
    for p in parts:
        if used + len(p) + 2 > max_chars:
            break
        out_parts.append(p)
        used += len(p) + 2
    remaining = len(parts) - len(out_parts)
    suffix = f" (+{remaining} more)" if remaining > 0 else ""
    return ", ".join(out_parts) + suffix


def _snapshot_copy_source(card) -> None:
    """Save the printed object before a battlefield copy effect mutates it."""
    if card._pre_copy_snapshot is not None:
        return
    card._pre_copy_snapshot = {
        'name': card.name,
        'power': card.power,
        'toughness': card.toughness,
        'type_line': card.type_line,
        'oracle_text': card.oracle_text,
        'mana_cost': card.mana_cost,
        'cmc': card.cmc,
        'loyalty': card.loyalty,
        'keywords': list(card.keywords or []),
        'color_identity': list(card.color_identity or []),
    }


def _revert_copy_if_leaving_battlefield(card) -> None:
    """Restore printed characteristics when a copy/manifest leaves the battlefield.

    Per CR 706.10, copy effects only apply on the battlefield (and stack).
    A Phantasmal Image that copied Korvold is back to being Phantasmal Image
    once it moves to hand/graveyard/exile/library. Without this revert step,
    clones retain their copied name/cost forever and (in game_1506202586036830232)
    let a player's Phantasmal Image surface as a "Korvold" castable in hand.
    """
    snap = card._pre_copy_snapshot
    if snap:
        card.name = snap['name']
        card.power = snap['power']
        card.toughness = snap['toughness']
        card.type_line = snap['type_line']
        card.oracle_text = snap['oracle_text']
        card.mana_cost = snap['mana_cost']
        card.cmc = snap['cmc']
        card.loyalty = snap['loyalty']
        card.keywords = list(snap['keywords'])
        card.color_identity = list(snap['color_identity'])
        card.loyalty_counters = 0
        card.counters.clear()
        card._pre_copy_snapshot = None
        card._is_copy = False
        card._copy_of = None
        card._original_name = ""
        print(f"[COPY-REVERT] {snap['name']} reverted to printed characteristics on leave-battlefield")

    # CR 701.34d: a manifested permanent is face up again immediately after
    # it leaves the battlefield. Reality Shift exposed the missing inverse:
    # bounced/manifests were remaining named "Manifest" in hand.
    manifest = getattr(card, '_manifest_original', None)
    if manifest:
        for attr in ('name', 'type_line', 'oracle_text', 'mana_cost', 'cmc',
                     'power', 'toughness', 'loyalty', 'has_transform'):
            setattr(card, attr, manifest[attr])
        card.keywords = list(manifest.get('keywords', []))
        card.colors = list(manifest.get('colors', []))
        card._manifest_original = None
        card._manifested = False
        card.loyalty_counters = 0
        card.counters.clear()
        print(f"[MANIFEST-REVERT] {card.name} restored on leave-battlefield")


def _fire_noncast_battlefield_entry(rules, game: GameState,
                                    player: Player, card: Card) -> List[str]:
    """Run deterministic ETB handling for an action-driven zone change.

    The cast path in ``mtg.spells`` already resolves the entering card's own
    Tier 1.5 template and then scans battlefield watchers. Generic
    ``move_card`` / ``reanimate`` actions used to do neither, so Dread Return
    could put Craterhoof onto the battlefield without its pump ever firing
    (June 11 game 1514626089496875188).

    Pub/sub slice 2 note: PERMANENT_ENTERED is emitted at the MUTATION sites
    (the battlefield.append callers), not here — this helper is trigger
    processing, is gated on engine_ref, and re-runs under Panharmonicon
    doubling; none of those should gate or duplicate the entry event.
    """
    # Aug 10 audit (F1): the enters-with-counters clause is a replacement
    # effect, not a trigger, so it must apply even when there is no engine
    # ref to run trigger processing with — hence ABOVE the gate below.
    from mtg.helpers import apply_enters_with_counters
    entry_counter_msgs = apply_enters_with_counters(card)

    engine = getattr(rules, 'engine_ref', None)
    if engine is None:
        return entry_counter_msgs

    messages: List[str] = list(entry_counter_msgs)
    oracle = card.oracle_text or ''
    if oracle:
        from mtg.triggers import _is_self_etb_trigger_paragraph
        etb_paragraphs = [
            paragraph.strip() for paragraph in oracle.split('\n')
            if _is_self_etb_trigger_paragraph(card, paragraph)
        ]
        if etb_paragraphs:
            try:
                from rules.effect_templates import get_effect_library, build_game_context
            except ImportError:
                pass
            else:
                opponent = next((p for p in game.players if p is not player), player)
                ctx = build_game_context(game, player, opponent, card=card)
                actions, explanation = get_effect_library().resolve_etb(
                    card_name=card.name,
                    oracle_text='\n'.join(etb_paragraphs),
                    controller=player.name,
                    opponent=opponent.name,
                    game_context=ctx,
                )
                if actions is not None:
                    for generated_action in actions:
                        if generated_action.get('action') == 'no_action':
                            continue
                        msg = rules._execute_action_on_state(game, generated_action)
                        if msg:
                            messages.append(msg)
                    print(f"[NONCAST-ETB-TEMPLATE] Resolved {card.name}: {explanation}")

    # The engine helper covers Soul Warden-style creature-enter watchers and
    # the remaining hardcoded ETB observers (Warstorm Surge, Guardian Project).
    watcher_messages = engine._handle_etb_triggers(game, player, card)
    if watcher_messages:
        messages.extend(watcher_messages)
    # Living weapon (July 20 batch-3, reviewer V1): the keyword's Tier 1
    # handler lives only in the cast path (resolve_special_effects) — any
    # noncast entry (Stoneforge cheat-into-play, reanimation, flicker,
    # Living Death) put the Equipment onto the battlefield with no Germ,
    # leaving it dead weight (Batterskull, game_1528946322995150848).
    # create_token supplies is_token/emit/watcher-scan; attach must follow
    # immediately so the 0/0 doesn't die to SBA.
    if (card.is_artifact() and 'living weapon' in (card.oracle_text or '').lower()
            and not card.attached_to):
        rules._execute_action_on_state(game, {
            "action": "create_token", "player": player.name,
            "name": "Phyrexian Germ", "power": 0, "toughness": 0,
            "types": "Creature Token — Phyrexian Germ", "colors": "B",
            "count": 1, "source": card.name})
        germ = next((c for c in reversed(player.battlefield)
                     if c.name == "Phyrexian Germ" and not c.attachments), None)
        if germ is not None:
            card.attached_to = germ.id
            germ.attachments.append(card.id)
            game.recalculate_power_toughness()
            messages.append(f"⚔️ {card.name} — Living weapon creates a 0/0 Phyrexian Germ token, equipped with {card.name}")
            print(f"[LIVING-WEAPON] {card.name} -> {germ.name} (noncast entry)")
    has_panharmonicon = (
        (card.is_creature(game) or card.is_artifact())
        and any(p.name.lower() == 'panharmonicon' and p is not card
                for p in player.battlefield)
    )
    if has_panharmonicon and not getattr(game, '_panharmonicon_repeat_active', False):
        game._panharmonicon_repeat_active = True
        try:
            messages.append(f"⚡ Panharmonicon doubles {card.name}'s ETB!")
            messages.extend(_fire_noncast_battlefield_entry(rules, game, player, card))
        finally:
            game._panharmonicon_repeat_active = False
    return messages


def _fire_creature_exiled_watchers(game: GameState, exiled_card: Card) -> List[str]:
    """Fire Soulherder's battlefield-to-exile trigger once per watcher."""
    if not exiled_card.is_creature():
        return []
    messages = []
    for player in game.players:
        for watcher in player.battlefield:
            if watcher is exiled_card or watcher.name.lower() != 'soulherder':
                continue
            oracle = (watcher.oracle_text or '').lower()
            if 'whenever a creature is exiled from the battlefield' not in oracle:
                continue
            watcher.counters['+1/+1'] = watcher.counters.get('+1/+1', 0) + 1
            messages.append(f"👻 Soulherder gets a +1/+1 counter ({exiled_card.name} was exiled)")
    if messages:
        game.recalculate_power_toughness()
    return messages


def _fire_sacrifice_triggers(rules, game: GameState, sac_player: Player, sacrificed_card: Card) -> List[str]:
    """Scan sac_player's battlefield for 'Whenever you sacrifice a permanent' triggers.

    Resolves them via the Tier 1.5 template library. Korvold (Fae-Cursed King +
    draw a card), Mayhem Devil (deal 1 damage), and similar cards rely on this.
    Without this scan, sacrifice events were silent and Korvold's draw side
    never fired (Apr 28 audit).

    Returns a list of trigger messages to append to the sacrifice's display.
    """
    messages: List[str] = []
    try:
        # July 21 batch audit (R1-2): the sacrificed permanent's OWN
        # "whenever you sacrifice" ability triggers for its own sacrifice
        # (the event happened while it was on the battlefield — same
        # last-known-info principle as Blood Artist seeing its own death;
        # official Mayhem Devil rulings confirm). The old `continue` skip,
        # plus every call site running this scan after battlefield.remove,
        # meant Korvold sacrificed to Viscera Seer drew no card
        # (game_1529154418816057364). Self-referential actions like "put a
        # counter on Korvold" simply fizzle since it's no longer there.
        scan = [(c, sac_player) for c in sac_player.battlefield]
        if sacrificed_card not in sac_player.battlefield:
            scan.append((sacrificed_card, sac_player))
        # July 24 batch-6 audit (reviewer A1, MAJOR): "Whenever a PLAYER
        # sacrifices a permanent" (Mayhem Devil) fires on ANY player's
        # sacrifice — the old scan covered only sac_player's battlefield
        # and only the "you sacrifice" phrasings, so Mayhem Devil never
        # triggered once across 7 qualifying sacrifices while on the
        # battlefield (game_1529979552258855062).
        for _other in game.players:
            if _other is sac_player:
                continue
            scan.extend((c, _other) for c in _other.battlefield)
        for source, src_controller in scan:
            oracle = (getattr(source, 'oracle_text', '') or '').lower()
            if src_controller is sac_player:
                if ('whenever you sacrifice' not in oracle
                        and 'whenever a permanent you control is sacrificed' not in oracle
                        and 'whenever a player sacrifices' not in oracle):
                    continue
            else:
                # An opponent's permanent only sees this sacrifice through
                # the any-player phrasing.
                if 'whenever a player sacrifices' not in oracle:
                    continue
            try:
                from rules.effect_templates import get_effect_library, build_game_context
            except ImportError:
                # rules/ not available — silently skip
                continue
            try:
                # Find the trigger sentence(s). July 23 audit (#16): a single
                # card can carry SEVERAL color-qualified sacrifice triggers
                # (Savra, Queen of the Golgari: black -> edict, green -> gain 2
                # life). The old code took only the FIRST matching sentence and
                # never checked the sacrificed creature's color, so sacrificing
                # a green Sakura-Tribe Elder tried to resolve Savra's BLACK
                # clause while her green one never fired at all
                # (game_1529677634588377108). Collect every clause and gate each
                # on the sacrificed creature's colors. Split on newlines too —
                # separate abilities are separate lines, not sentences.
                _color_words = {'white': 'W', 'blue': 'U', 'black': 'B',
                                'red': 'R', 'green': 'G'}
                _sac_colors = set(getattr(sacrificed_card, 'colors', []) or [])
                trigger_texts = []
                for sentence in (source.oracle_text or '').replace('\n', '.').split('.'):
                    sl = sentence.lower().strip()
                    if ('whenever you sacrifice' not in sl
                            and 'whenever a permanent you control is sacrificed' not in sl
                            and 'whenever a player sacrifices' not in sl):
                        continue
                    if (src_controller is not sac_player
                            and 'whenever a player sacrifices' not in sl):
                        continue
                    _req = next((sym for word, sym in _color_words.items()
                                 if f'a {word} creature' in sl), None)
                    if _req and _req not in _sac_colors:
                        print(f"[SAC-TRIGGER] {source.name}: skipping "
                              f"{_req}-qualified clause — {sacrificed_card.name} "
                              f"is {sorted(_sac_colors) or 'colorless'}")
                        continue
                    trigger_texts.append(sentence.strip())
                if not trigger_texts:
                    continue
                # Resolve from the TRIGGER SOURCE's controller's perspective —
                # an opponent's Mayhem Devil pings for ITS controller, not for
                # the sacrificing player.
                opp = next((p for p in game.players if p is not src_controller),
                           src_controller)
                lib = get_effect_library()
                ctx = build_game_context(game, src_controller, opp, card=source)
                ctx['sacrificed_card_name'] = sacrificed_card.name
                # Try the dedicated "korvold sacrifice" template by appending suffix
                key_with_suffix = source.name.lower() + " sacrifice"
                for trigger_text in trigger_texts:
                    if key_with_suffix in getattr(lib, '_card_templates', {}):
                        template = lib._card_templates[key_with_suffix]
                        actions = template.action_generator(src_controller.name, opp.name, ctx)
                    else:
                        actions, _desc = lib.resolve_etb(
                            card_name=source.name,
                            oracle_text=trigger_text,
                            controller=src_controller.name,
                            opponent=opp.name,
                            game_context=ctx,
                        )
                    if actions:
                        for action in actions:
                            if action.get('action') == 'no_action':
                                continue
                            try:
                                msg = rules._execute_action_on_state(game, action)
                                if msg:
                                    messages.append(f"⚡ {source.name}: {msg}")
                                    print(f"[SAC-TRIGGER-RESULT] {source.name}: "
                                          f"{msg}")
                            except Exception as e:
                                print(f"[SAC-TRIGGER] Action failed for {source.name}: {e}")
                        print(f"[SAC-TRIGGER] {source.name} fired from {sac_player.name} sacrificing {sacrificed_card.name}")
                    # A dedicated suffix template resolves the whole card once.
                    if key_with_suffix in getattr(lib, '_card_templates', {}):
                        break
            except Exception as e:
                print(f"[SAC-TRIGGER] Error processing {source.name}: {e}")
    except Exception as e:
        print(f"[SAC-TRIGGER] Top-level scan failed: {e}")
        maybe_reraise(e)
    return messages


def execute_action_on_state(rules, game: GameState, action: Dict) -> Optional[str]:
    """Execute a single judge action on the game state. Returns display message."""
    # [TARGETING] Auto-enrich action with source metadata from game context.
    # When resolving a spell/ability, callers set game._current_resolution_source
    # = (card_name, controller_name). This lets targeting checks in handlers below
    # work without every caller injecting _source_card_name/_source_controller.
    if "_source_card_name" not in action:
        src_ctx = getattr(game, '_current_resolution_source', None)
        if src_ctx:
            action["_source_card_name"] = src_ctx[0]
            action["_source_controller"] = src_ctx[1]

    action_type = action.get("action", "")

    # Helper: is this player under Teferi's Protection-style life lock?
    # Set by prevent_all_damage(lock_life_total=True); expires on the
    # controller's next untap step along with the damage prevention flag.
    def _life_total_locked(p: Player, g: GameState) -> bool:
        if not getattr(p, '_life_total_locked', False):
            return False
        expires = getattr(p, '_life_total_locked_expires_turn', float('inf'))
        if g.turn_number >= expires:
            p._life_total_locked = False
            return False
        return True

    # Helper: find player by name
    def find_player(name: str) -> Optional[Player]:
        if not name:
            return None
        name_lower = name.lower()
        for p in game.players:
            if p.name.lower() == name_lower:
                return p
        # Fuzzy match
        for p in game.players:
            if name_lower in p.name.lower():
                return p
        return None
    
    # Helper: find card on battlefield
    def find_card_on_battlefield(card_name: str, controller: str = None) -> Optional[Tuple[Card, Player]]:
        if not card_name:
            return None
        name_lower = card_name.lower()
        search_players = game.players
        if controller:
            p = find_player(controller)
            if p:
                search_players = [p]
        # Two-pass: exact match first (prefer tokens when tied),
        # then substring fallback (prefer tokens — searching for "Treasure"
        # should hit the Treasure token, not "Treasure Mage").
        exact_token = None
        exact_nontoken = None
        partial_token = None
        partial_nontoken = None
        for p in search_players:
            for c in p.battlefield:
                if getattr(c, '_phased_out', False):
                    continue
                cn = c.name.lower()
                is_tok = getattr(c, 'is_token', False)
                if cn == name_lower:
                    if is_tok and exact_token is None:
                        exact_token = (c, p)
                    elif not is_tok and exact_nontoken is None:
                        exact_nontoken = (c, p)
                elif name_lower in cn:
                    if is_tok and partial_token is None:
                        partial_token = (c, p)
                    elif not is_tok and partial_nontoken is None:
                        partial_nontoken = (c, p)
        return exact_token or exact_nontoken or partial_token or partial_nontoken
    
    # Helper: find card in a specific zone
    def find_card_in_zone(card_name: str, zone_name: str, player: Player) -> Optional[Card]:
        zone_map = {
            'hand': player.hand,
            'battlefield': player.battlefield,
            'graveyard': player.graveyard,
            'exile': player.exile,
            'library': player.library,
        }
        zone = zone_map.get(zone_name.lower(), [])
        name_lower = card_name.lower()
        for c in zone:
            if c.name.lower() == name_lower or name_lower in c.name.lower():
                return c
        return None
    
    # ---- DEAL DAMAGE ----
    if action_type == "deal_damage":
        amount = int(action.get("amount", 0))
        if amount <= 0:
            return None

        # May 20 audit (#19): default source from the active resolution
        # source so the [SPELL-DAMAGE] tag isn't attributed to "unknown source"
        # when the action dict lacks a `source` field. 23/126 games in May 20
        # batch logged "(unknown source)" — most were Tier 3 judge JSON
        # actions that omit the `source` key. Fall back to
        # game._current_resolution_source (set during cast_spell_async and
        # restored by the flicker save/restore in Fix #8) when present.
        source_name = action.get("source", "") or ""
        if not source_name:
            source_name = action.get("_source_card_name", "") or ""
        if not source_name:
            try:
                src_ctx = getattr(game, '_current_resolution_source', None)
                if src_ctx and src_ctx[0]:
                    source_name = src_ctx[0]
            except Exception:
                pass

        # [REPLACEMENT] Process damage replacement effects (Furnace of Rath, etc.)
        if HAS_REPLACEMENT_ENGINE and game._replacement_engine and game._replacement_engine.effects:
            from mtg.helpers import damage_source_colors
            # Resolve affected_player: for card damage, use the target card's controller
            # so conditions like Gisela ("damage to a permanent an opponent controls")
            # fire correctly. CR 614 checks require both object + controller context.
            affected_player_name = action.get("target_player", "") or ""
            affected_object_name = ""
            target_card_name = action.get("target_card")
            if target_card_name and not affected_player_name:
                tc_result = find_card_on_battlefield(target_card_name, action.get("target_controller"))
                if tc_result:
                    tc_card, tc_owner = tc_result
                    affected_object_name = tc_card.name
                    affected_player_name = getattr(tc_owner, 'name', '') or ''
            event = GameEvent(
                event_type=EventType.DAMAGE,
                affected_player=affected_player_name,
                affected_object=action.get("target_card", ""),
                affected_object_name=affected_object_name,
                amount=amount,
                source_name=source_name,
                source_controller=action.get("_source_controller", "") or "",
                # Aug 2 batch-14: color-gated replacements (Torbran) need the
                # source's colors — CR 202.2, from the mana cost. This is the
                # template/Tier-3 damage path, so the source is usually a
                # resolving spell rather than a permanent; the resolver
                # searches battlefields, the stack, and the resolving slot.
                source_colors=damage_source_colors(
                    game, source_name=source_name),
            )
            final = game._replacement_engine.process_event_sync(event)
            if final.is_prevented:
                print(f"  [REPLACEMENT-APPLY] Damage prevented: {', '.join(final.replacement_chain)}")
                return f"🛡️ Damage prevented ({', '.join(final.replacement_chain)})"
            if final.amount != amount:
                print(f"  [REPLACEMENT-APPLY] Damage modified: {amount} → {final.amount} ({', '.join(final.replacement_chain)})")
            amount = final.amount

        # Damage to player
        if "target_player" in action:
            player = find_player(action["target_player"])
            if player:
                # [TARGETING] Check player hexproof (Leyline of Sanctity, etc.)
                if HAS_TARGETING and action.get("_source_card_name") and action.get("_source_controller"):
                    legal, reason = _validate_player_target_for_action(
                        game, player, action["_source_card_name"], action["_source_controller"])
                    if not legal:
                        print(f"[TARGETING] Damage to player blocked: {player.name} — {reason}")
                        return f"🛡️ {amount} damage to **{player.name}** blocked ({reason})"
                # Damage prevention flag (Teferi's Protection, Fog, etc.)
                from mtg.helpers import damage_prevention_disabled, player_has_prevent_all_static
                _prevent_flag = getattr(player, '_damage_prevented', False)
                if _prevent_flag:
                    expires = getattr(player, '_damage_prevented_expires_turn', float('inf'))
                    if game.turn_number >= expires:
                        player._damage_prevented = False
                        _prevent_flag = False
                        print(f"  [DAMAGE-PREVENTED] Expired for {player.name}")
                if _prevent_flag or player_has_prevent_all_static(player):
                    if damage_prevention_disabled(game):
                        print(f"  [DAMAGE-PREVENTED] Overridden for {player.name} — "
                              f"damage can't be prevented this turn (Insult // Injury)")
                    else:
                        print(f"  [DAMAGE-PREVENTED] {source_name} → {player.name}: {amount} damage prevented")
                        return f"🛡️ {amount} damage to **{player.name}** prevented"
                player.life -= amount
                player.record_life_loss(amount)
                rules.log_event(f"Dealt {amount} damage to {player.name}")
                # May 7 audit fix #6: when source is a burn spell (Lava Spike,
                # Shard Volley, Lightning Bolt), prefix the line with 🔥 + name
                # so per-spell damage can be traced in the log. Without a
                # source, fall back to the generic 💥 line.
                source_name_clean = (source_name or
                                     action.get("_source_card_name", "") or "")
                # May 16 audit: console mirror for life-total reconciliation.
                # The gain_life branch already prints [LIFE-GAIN]; missing this
                # tag made silent direct-damage (Shard Volley, Thoughtseize self-
                # cost) impossible to grep in post-batch audits.
                # May 18 audit: clamp displayed life at 0 (see combat.py rationale).
                displayed_life = max(0, player.life)
                # May 20 audit (#18): if a code path produces a synthetic
                # "lethal" sentinel (e.g. ≥999 damage), display as "lethal"
                # rather than the raw sentinel. Fix #2 closes the "Attack for
                # lethal" judge guard at-source; this is defensive coverage
                # for any other path that might construct a sentinel amount.
                if amount >= 999:
                    displayed_amount = "lethal"
                else:
                    displayed_amount = str(amount)
                print(f"[SPELL-DAMAGE] {player.name}: -{amount} life → {displayed_life} "
                      f"({source_name_clean or 'unknown source'})")
                if source_name_clean:
                    return (f"🔥 **{source_name_clean}** deals {displayed_amount} damage "
                            f"to **{player.name}** (life: {displayed_life})")
                return f"💥 {displayed_amount} damage to **{player.name}** (life: {displayed_life})"

        # Damage to creature/planeswalker
        if "target_card" in action:
            result = find_card_on_battlefield(action["target_card"], action.get("target_controller"))
            if result:
                card, owner = result
                # [TARGETING] Check hexproof/protection/shroud before dealing damage
                if HAS_TARGETING and action.get("_source_card_name") and action.get("_source_controller"):
                    legal, reason = _validate_target_for_action(
                        game, card, owner, action["_source_card_name"], action["_source_controller"])
                    if not legal:
                        print(f"[TARGETING] Damage to creature blocked: {card.name} — {reason}")
                        return f"🛡️ Damage to **{card.name}** blocked ({reason})"
                if card.is_planeswalker():
                    card.loyalty_counters = max(0, getattr(card, 'loyalty_counters', 0) - amount)
                    return f"💥 {amount} damage to **{card.name}** (loyalty: {card.loyalty_counters})"
                else:
                    card.damage_marked = getattr(card, 'damage_marked', 0) + amount
                    # Aug 1: "whenever a source deals damage to this
                    # creature" fires on NONCOMBAT damage too (CR 603.2 —
                    # any source; Obliterator vs burn was the batch-10 R14
                    # evidence, combat-only until now). Source controller:
                    # the explicit threading when present, else the damaged
                    # owner's opponent (2-player heuristic — burning your
                    # own creature is the rare case, and getting the edict's
                    # target wrong matters more than modeling it).
                    from mtg.triggers import scan_damaged_creature
                    _dt_ctrl = (find_player(action.get("_source_controller", ""))
                                or next((p for p in game.players
                                         if p is not owner), None))
                    _dt_msgs = scan_damaged_creature(
                        rules, game, card, amount, _dt_ctrl)
                    _dmg_line = f"💥 {amount} damage to **{card.name}**"
                    if _dt_msgs:
                        _dmg_line += "\n" + "\n".join(_dt_msgs)
                    return _dmg_line

        # Damage target was specified but couldn't be resolved on battlefield.
        # This is usually a fizzle (target died/exiled between cast and resolve)
        # or a template generating actions for a missing target. Log to console
        # for diagnostics but suppress from Discord — the user already saw
        # the spell announcement; an extra "target not found" line just looks
        # like an error.
        target_name = action.get("target_card") or action.get("target_player") or "unknown"
        print(f"[DAMAGE] target not found: target={target_name!r}, action={action}")
        return None
    
    # ---- GAIN/LOSE LIFE ----
    elif action_type == "gain_life":
        player = find_player(action.get("player", ""))
        amount = int(action.get("amount", 0))
        if player and amount > 0:
            # Teferi's Protection: life total can't change.
            if _life_total_locked(player, game):
                print(f"  [LIFE-LOCK] Life gain blocked for {player.name} (Teferi's Protection)")
                return f"🛡️ **{player.name}**'s life total can't change (Teferi's Protection)"
            ok, final_amount, chain = rules._apply_life_gain(
                game, player, amount, source_name=action.get("source", "")
            )
            if not ok:
                return f"🚫 Life gain prevented for **{player.name}** ({', '.join(chain)})"
            rules.log_event(f"{player.name} gains {final_amount} life")
            # Apr 30 audit: console mirror so post-batch life-total
            # reconciliation has a tag to grep (modal-spell life gains were
            # invisible in console). Includes source for traceability.
            src = action.get("source", "") or "spell/ability"
            print(f"[LIFE-GAIN] {player.name}: +{final_amount} life → {player.life} ({src})")
            return f"💚 **{player.name}** gains {final_amount} life (life: {player.life})"

    elif action_type == "lose_life":
        player = find_player(action.get("player", ""))
        amount = int(action.get("amount", 0))
        if player and amount > 0:
            # Teferi's Protection: life total can't change — blocks non-damage
            # life loss (Kambal, Sulfuric Vortex, etc.) before the replacement
            # engine runs.
            if _life_total_locked(player, game):
                print(f"  [LIFE-LOCK] Life loss blocked for {player.name} (Teferi's Protection)")
                return f"🛡️ **{player.name}**'s life total can't change (Teferi's Protection)"
            # [REPLACEMENT] Process life loss replacement effects
            if HAS_REPLACEMENT_ENGINE and game._replacement_engine and game._replacement_engine.effects:
                event = GameEvent(
                    event_type=EventType.LIFE_LOSS,
                    affected_player=player.name,
                    amount=amount,
                )
                final = game._replacement_engine.process_event_sync(event)
                if final.is_prevented:
                    return f"🛡️ Life loss prevented ({', '.join(final.replacement_chain)})"
                if final.amount != amount:
                    print(f"  [REPLACEMENT-APPLY] Life loss modified: {amount} → {final.amount}")
                amount = final.amount
            player.life -= amount
            player.record_life_loss(amount)
            rules.log_event(f"{player.name} loses {amount} life")
            # May 16 audit: console mirror — symmetric with [LIFE-GAIN]. Was
            # missing, so Bastion of Remembrance ETB drains, Thoughtseize
            # self-loss, and Phyrexian-mana payments were silent in console
            # logs (only Discord saw them).
            src = action.get("source", "") or "spell/ability"
            # May 18 audit: clamp displayed life at 0 (see combat.py rationale).
            displayed_life = max(0, player.life)
            print(f"[LIFE-LOSS] {player.name}: -{amount} life → {displayed_life} ({src})")
            return f"🩸 **{player.name}** loses {amount} life (life: {displayed_life})"

    elif action_type == "extort_drain":
        controller = find_player(action.get("player", ""))
        opponent = find_player(action.get("opponent", ""))
        amount = int(action.get("amount", 1))
        if not controller or not opponent or amount <= 0:
            return None
        before = opponent.life
        loss_msg = execute_action_on_state(rules, game, {
            "action": "lose_life", "player": opponent.name,
            "amount": amount, "source": action.get("source", "Extort")})
        actual_lost = max(0, before - opponent.life)
        gain_msg = None
        if actual_lost:
            gain_msg = execute_action_on_state(rules, game, {
                "action": "gain_life", "player": controller.name,
                "amount": actual_lost, "source": action.get("source", "Extort")})
        return "\n".join(msg for msg in (loss_msg, gain_msg) if msg)

    # ---- LOSE THE GAME ----
    # Pact of Negation, Phage the Untouchable, Door to Nothingness, etc.
    # The "lose 999 life" hack reliably triggers a loss SBA but produces
    # nonsense Discord messages ("Rick loses 999 life (life: -970)"). Use
    # this action type to set life=0 + ended outright.
    elif action_type == "lose_the_game":
        player = find_player(action.get("player", ""))
        if player:
            reason = action.get("reason", "lose-the-game effect")
            # Keep the true total for the end-of-game summary (June 11 audit:
            # pact deaths displayed "Final life: 0" for a player at 26).
            player._final_life_before_loss = player.life
            player.life = 0
            game.ended = True
            try:
                game.winner = 1 - game.players.index(player)
            except (ValueError, AttributeError):
                game.winner = None
            rules.log_event(f"{player.name} loses the game ({reason})")
            return f"💀 **{player.name}** loses the game! ({reason})"

    # ---- PAY OR LOSE (Pact upkeep triggers) ----
    # Attempt to pay a mana cost; lose the game only if it can't be paid.
    # Autoplay always pays when able — no rational player declines a pact.
    elif action_type == "pay_or_lose":
        player = find_player(action.get("player", ""))
        if player:
            cost = action.get("cost", "")
            reason = action.get("reason", "failed to pay cost")
            source = action.get("source", "cost")
            paid = False
            if cost:
                try:
                    paid = player.tap_sources_for_cost(cost, game=game)
                except (ValueError, KeyError, AttributeError, TypeError) as e:
                    print(f"[PAY-OR-LOSE] {source}: payment attempt errored: {e}")
            if paid:
                rules.log_event(f"{player.name} pays {cost} for {source}")
                return f"💰 **{player.name}** pays {cost} for {source}"
            player._final_life_before_loss = player.life
            player.life = 0
            game.ended = True
            try:
                game.winner = 1 - game.players.index(player)
            except (ValueError, AttributeError):
                game.winner = None
            rules.log_event(f"{player.name} loses the game ({reason})")
            return f"💀 **{player.name}** loses the game! ({reason})"

    # ---- DRAW CARDS ----
    elif action_type == "draw_cards":
        player = find_player(action.get("player", ""))
        amount = int(action.get("amount", 0))
        if player and amount > 0:
            drawn = []
            for _ in range(amount):
                if not player.library:
                    player.attempted_draw_from_empty = True
                    if not hasattr(game, '_library_loss'):
                        game._library_loss = set()
                    if player.name not in game._library_loss:
                        print(f"[SBA] {player.name} attempted to draw from empty library — loses the game (CR 104.3c)")
                    game._library_loss.add(player.name)
                    game.loss_reason = f"{player.name} drew from an empty library"
                    break
                # [REPLACEMENT] Process draw replacement effects (Narset, Spirit of the Labyrinth)
                if HAS_REPLACEMENT_ENGINE and game._replacement_engine and game._replacement_engine.effects:
                    event = GameEvent(
                        event_type=EventType.DRAW,
                        affected_player=player.name,
                        amount=1,
                    )
                    final = game._replacement_engine.process_event_sync(event)
                    if final.is_prevented:
                        print(f"  [REPLACEMENT-APPLY] Draw prevented for {player.name} ({', '.join(final.replacement_chain)})")
                        continue
                # Dredge REPLACES the draw (CR 702.52a) — before the pop.
                from mtg.helpers import note_miracle_on_draw, try_dredge
                if try_dredge(game, player) is not None:
                    continue
                card = player.library.pop(0)
                player.hand.append(card)
                drawn.append(card.name)
                # Miracle (CR 702.94a) reacts to it — after.
                player.cards_drawn_this_turn += 1
                note_miracle_on_draw(game, player, card)
            actual_drawn = len(drawn)
            rules.log_event(f"{player.name} draws {actual_drawn}")
            if actual_drawn == 0:
                if not player.library:
                    return f"📚 **{player.name}** cannot draw — library is empty!"
                return f"🚫 All draws prevented for **{player.name}**"
            # June 11 audit: Discord output is public to both players. Rick is
            # represented by a non-Claude Player during autoplay, but that does
            # not make his hand public information. The old branch printed every
            # effect-drawn card name for Rick while hiding Claude's, leaking
            # private hand state in game logs. Keep names in the actual hand and
            # expose only the count for every player.
            return f"🃏 **{player.name}** draws {actual_drawn} card(s)"
    
    # ---- SCRY ----
    elif action_type == "fateshift":
        # Aminatou +1: "The top card of each player's library becomes that
        # player's library bottom card." player.library[0] is the top, the
        # end of the list is the bottom (mirrors scry/surveil convention).
        moved = []
        for p in game.players:
            if p.library:
                top = p.library.pop(0)
                p.library.append(top)
                moved.append(p.name)
        if moved:
            return f"🔮 Fateshift: top of {' and '.join(moved)}'s library moved to bottom"
        return None

    elif action_type == "scry":
        player = find_player(action.get("player", ""))
        amount = int(action.get("amount", 1))
        if player and amount > 0 and player.library:
            # Look at top N cards. Simple heuristic: lands go to bottom if
            # controller has 4+ lands in play; non-lands go to bottom if
            # controller has <3 lands. Otherwise keep on top.
            top_n = player.library[:amount]
            del player.library[:amount]
            keep_top = []
            to_bottom = []
            land_count = sum(1 for c in player.battlefield if c.is_land())
            for c in top_n:
                is_land = c.is_land() if hasattr(c, 'is_land') else False
                # Bottom lands if flooding, bottom spells if starved
                if is_land and land_count >= 4:
                    to_bottom.append(c)
                elif (not is_land) and land_count < 3 and (getattr(c, 'cmc', 0) or 0) >= 4:
                    to_bottom.append(c)
                else:
                    keep_top.append(c)
            # Put kept cards back on top in original order
            player.library = keep_top + player.library
            # Put bottomed cards at the bottom
            player.library.extend(to_bottom)
            if to_bottom:
                return f"🔮 **{player.name}** scries {amount} — bottoms {len(to_bottom)}"
            # May 20 audit: previously suppressed "keeps all on top" entirely,
            # which made Charming Prince's default "scry 2" mode appear as
            # silent partial-resolution to auditors. Now suppress ONLY for
            # recurring auto-scry sources (Azcanta upkeep, Mirri's Guile)
            # tagged via _silent_scry_source, but emit for one-shot scry from
            # ETB modals (Charming Prince).
            if action.get('_silent_scry_source'):
                return None
            return f"🔮 **{player.name}** scries {amount} — keeps top"
        return None

    # ---- SURVEIL (Watcher in the Mist, Notion Rain, etc.) ----
    # CR 701.43: look at top N cards, put any number into graveyard, rest
    # back on top in any order. May 13 audit: previously had no handler at
    # all, so Watcher in the Mist / Doom Whisperer ETBs went unresolved.
    # Heuristic: mirror scry but bottom-to-graveyard instead of library
    # bottom — flooded → mill lands; starved → keep everything. Skipping
    # delve/reanimate strategic considerations (those are deck-specific).
    elif action_type == "surveil":
        player = find_player(action.get("player", ""))
        amount = int(action.get("amount", 1))
        if player and amount > 0 and player.library:
            top_n = player.library[:amount]
            del player.library[:amount]
            keep_top = []
            to_graveyard = []
            land_count = sum(1 for c in player.battlefield if c.is_land())
            for c in top_n:
                is_land = c.is_land() if hasattr(c, 'is_land') else False
                if is_land and land_count >= 4:
                    to_graveyard.append(c)
                else:
                    keep_top.append(c)
            player.library = keep_top + player.library
            player.graveyard.extend(to_graveyard)
            if to_graveyard:
                return f"🔮 **{player.name}** surveils {amount} — mills {len(to_graveyard)}"
            return None
        return None

    # ---- LOOK AT TOP (Telepathy, peek effects) ----
    # Pure observation — doesn't change library order. Useful for cards
    # like "look at the top card of your library" (Future Sight, Mystic
    # Speculation precursors). Returns a brief, non-spoilery message in
    # public games; full names only if hidden_info is allowed.
    elif action_type == "look_at_top":
        player = find_player(action.get("player", ""))
        amount = int(action.get("amount", 1))
        if player and amount > 0 and player.library:
            top_n = player.library[:amount]
            names = [getattr(c, 'name', 'card') for c in top_n]
            # AI players: don't leak hidden info to the public log
            if getattr(player, 'is_claude', False):
                return f"👁️ {player.name} looks at the top {len(top_n)} card(s) of their library"
            return f"👁️ {player.name} looks at: {', '.join(names)}"
        return None

    # ---- FATESEAL (Jace TMS [+2], Jace, Memory Adept, etc.) ----
    # May 20 audit (Bug B): Jace TMS [+2] activations after the first were
    # silently refunded by the Tier 3 dedup at rules/planeswalker.py because
    # no template existed. Add a proper action so the template can route
    # through it. CR 701.20: "To fateseal N, look at the top N cards of an
    # opponent's library, then put any number of them on the bottom of that
    # library and the rest on top in any order."
    #
    # Strategic heuristic mirrors the May 17 judge.py fateseal mini-model:
    # bottom a high-CMC non-land (deny their best draw), or bottom a land
    # when opponent already has 5+ lands (waste their next draw).
    elif action_type == "fateseal":
        target_player = find_player(action.get("target_player", "") or action.get("player", ""))
        amount = int(action.get("amount", 1))
        if not target_player or amount <= 0 or not target_player.library:
            return None
        top_n = target_player.library[:amount]
        bottomed = []
        for top_card in top_n:
            is_land = top_card.is_land() if hasattr(top_card, 'is_land') else False
            cmc = getattr(top_card, 'cmc', 0) or 0
            opp_land_count = sum(1 for c in target_player.battlefield if c.is_land())
            should_bottom = (
                (not is_land and cmc >= 3)
                or (is_land and opp_land_count >= 5)
            )
            if should_bottom:
                target_player.library.remove(top_card)
                target_player.library.append(top_card)
                bottomed.append(top_card)
        if bottomed:
            print(f"[FATESEAL] {target_player.name} — bottomed {len(bottomed)} card(s) "
                  f"(CMCs {[getattr(c, 'cmc', 0) for c in bottomed]})")
            return (f"🔮 fatesealed **{target_player.name}** — "
                    f"bottomed {len(bottomed)} of the top {amount} card(s)")
        print(f"[FATESEAL] {target_player.name} — left top {amount} card(s) in place")
        return f"🔮 fatesealed **{target_player.name}** — left top card(s) in place"

    # ---- REORDER LIBRARY (Sensei's Divining Top, Brainstorm-style) ----
    # Reorder the top N cards. Heuristic: lands distributed evenly across
    # the top to avoid mana flood/screw, with curve-appropriate spells
    # interleaved. For autoplay, we approximate by sorting top N as
    # [land, spell, land, spell, ...] when the player is mana-light.
    elif action_type == "reorder_library":
        player = find_player(action.get("player", ""))
        amount = int(action.get("amount", 3))
        if player and amount > 0 and player.library:
            top_n = player.library[:amount]
            del player.library[:amount]
            land_count = sum(1 for c in player.battlefield if c.is_land())
            lands = [c for c in top_n if c.is_land()]
            spells = [c for c in top_n if not c.is_land()]
            # If mana-light, draw a land first; otherwise spells first.
            if land_count < 4 and lands:
                new_top = []
                while lands or spells:
                    if lands:
                        new_top.append(lands.pop(0))
                    if spells:
                        new_top.append(spells.pop(0))
            else:
                spells.sort(key=lambda c: getattr(c, 'cmc', 0) or 0)
                new_top = spells + lands
            player.library = new_top + player.library
            return f"🔀 {player.name} reorders the top {amount} card(s) of their library"
        return None

    # ---- DISCARD ----
    elif action_type == "discard":
        player = find_player(action.get("player", ""))
        if player:
            card_name = action.get("card", "random")
            # Wheel of Fortune-style "discard your hand"
            if card_name == "all":
                if not player.hand:
                    return None
                hand_size = len(player.hand)
                discarded_names = [c.name for c in player.hand]
                # Madness (CR 702.35, Aug 1 2026): each discarded card with
                # madness goes to EXILE with a pending cast-or-graveyard
                # choice — Wheel of Fortune / Reforge the Soul discarded
                # Bloodmad Vampire / Violent Eruption silently to graveyard
                # in batch 15325 (the whole mechanic was missing).
                from mtg.helpers import madness_discard_to_exile
                _wheel_hand = player.hand
                player.hand = []
                _mad_lines = []
                previous_discard_event = getattr(
                    game, '_active_discard_event_id', None)
                game._discard_event_serial = int(
                    getattr(game, '_discard_event_serial', 0) or 0) + 1
                game._active_discard_event_id = game._discard_event_serial
                for _c in _wheel_hand:
                    _mm = madness_discard_to_exile(game, player, _c)
                    if _mm:
                        _mad_lines.append(_mm)
                    else:
                        player.graveyard.append(_c)
                if previous_discard_event is None:
                    game._active_discard_event_id = None
                else:
                    game._active_discard_event_id = previous_discard_event
                rules.log_event(f"{player.name} discards their hand ({hand_size} cards)")
                # June 10 audit: don't hide exactly ONE name behind "+1 more" —
                # collapse only when 2+ names are hidden.
                if hand_size <= 7:
                    preview = ', '.join(discarded_names)
                    more = ""
                else:
                    preview = ', '.join(discarded_names[:6])
                    more = f" (+{hand_size - 6} more)"
                _wheel_msg = f"🗑️ **{player.name}** discards their hand ({hand_size}): {preview}{more}"
                if _mad_lines:
                    _wheel_msg += "\n" + "\n".join(_mad_lines)
                return _wheel_msg
            if card_name == "random" and player.hand:
                import random as rng
                card = rng.choice(player.hand)
            elif card_name == "best_nonland" and player.hand:
                # Thoughtseize-class: the CASTER chooses, so this is the
                # opposite polarity to "worst" below — take the opponent's
                # strongest nonland. July 27 fanout: this sentinel (unique to
                # data/card_templates.json's thoughtseize entry, added by the
                # July 20 JSON migration) had no branch, so it fell to the
                # name lookup, found no card called "best_nonland", and the
                # discard was skipped while the caster still paid 2 life.
                # Same shape as the June 11 "worst" bug one branch down.
                nonlands = [c for c in player.hand if not c.is_land()]
                # An all-lands hand means no legal choice — CR-correct to
                # discard nothing (the caster still pays their life).
                card = max(nonlands, key=lambda c: (c.cmc or 0)) if nonlands else None
                if card is None:
                    print(f"[DISCARD] {player.name}: hand has no nonland card — "
                          f"nothing discarded")
            elif card_name in ("worst", "choose", "any") and player.hand:
                # Heuristic self-discard (Daretti +2, looting). June 11 audit:
                # "worst" previously matched no selector, find_card_in_zone
                # found no card literally named "worst", and the handler
                # returned None silently — so Daretti's +2 drew 2 with ZERO
                # discards, every activation, all batch. Excess lands first,
                # then the highest-CMC (least castable) card.
                # Aug 1 (madness): a SELF-chosen discard prefers a madness
                # card — it's discarded into exile and castable for its
                # madness cost, which is Anje's whole engine. Only for the
                # chooser's own picks; "random"/"best_nonland" stay put.
                from mtg.helpers import parse_madness_cost
                _mad_choices = [c for c in player.hand
                                if parse_madness_cost(c.oracle_text or '')]
                lands = [c for c in player.hand if c.is_land()]
                if _mad_choices:
                    card = _mad_choices[0]
                elif len(lands) > 2:
                    card = lands[-1]
                else:
                    card = max(player.hand, key=lambda c: (c.cmc or 0))
            else:
                card = find_card_in_zone(card_name, "hand", player)
                if card is None:
                    # Aug 2 batch-14 audit (legacy reviewer): the sentinel
                    # selectors ("worst", "random", "best_nonland") only reach
                    # here when the hand is EMPTY — their own branches above
                    # all require `player.hand`. The old wording read as if
                    # the engine had searched for a card literally named
                    # "worst", and nearly produced a false positive in that
                    # audit. Say which case it is.
                    if not player.hand:
                        print(f"[DISCARD] {player.name}: hand is empty — "
                              f"discard is a legal no-op (CR 701.8a)")
                    else:
                        print(f"[DISCARD] {player.name}: '{card_name}' not in "
                              f"hand — discard skipped")

            if card:
                player.hand.remove(card)
                # Madness (CR 702.35) checks FIRST — its own replacement
                # sends the card to exile AND sets up the cast choice, which
                # a plain RiP-style exile redirect below can't offer.
                from mtg.helpers import madness_discard_to_exile
                _mad_msg = madness_discard_to_exile(game, player, card)
                if _mad_msg:
                    rules.log_event(f"{player.name} discards {card.name} into exile (madness)")
                    return _mad_msg
                # [REPLACEMENT] Check for graveyard replacement (Rest in Peace → exile instead)
                destination = "graveyard"
                if HAS_REPLACEMENT_ENGINE and game._replacement_engine and game._replacement_engine.effects:
                    event = GameEvent(
                        event_type=EventType.DISCARD,
                        affected_object=getattr(card, 'id', ''),
                        affected_object_name=card.name,
                        affected_player=player.name,
                        from_zone="hand",
                        to_zone="graveyard",
                    )
                    final = game._replacement_engine.process_event_sync(event)
                    if final.to_zone != "graveyard":
                        destination = final.to_zone
                        print(f"  [REPLACEMENT-APPLY] Discard redirected: graveyard → {destination} ({', '.join(final.replacement_chain)})")
                if destination == "exile":
                    player.exile.append(card)
                    rules.log_event(f"{player.name} discards {card.name} (exiled)")
                    return f"🗑️ **{player.name}** discards **{card.name}** (exiled by replacement effect)"
                else:
                    player.graveyard.append(card)
                    rules.log_event(f"{player.name} discards {card.name}")
                    return f"🗑️ **{player.name}** discards **{card.name}**"
    
    # ---- MOVE CARD ----
    elif action_type == "move_card":
        player = find_player(action.get("player", ""))
        if not player:
            player = game.active_player

        from_zone = action.get("from_zone", "")
        to_zone = action.get("to_zone", "")
        # May 23 audit (MAJOR #13): reject same-zone moves (e.g. battlefield →
        # battlefield) as no-ops instead of silently "applying" them. AI
        # judge sometimes generates these as filler when the actual effect
        # was already applied by a prior action. Logging the rejection makes
        # the case grep-able without corrupting state.
        if from_zone.lower() == to_zone.lower():
            card_name = action.get("card", "")
            print(f"[ACTIONS] Rejected no-op move_card: {card_name} {from_zone}→{to_zone}")
            return None
        card = find_card_in_zone(action.get("card", ""), from_zone, player)
        
        if card:
            # [TARGETING] Check hexproof/protection for targeted exile/bounce from battlefield
            if (from_zone.lower() == 'battlefield'
                    and HAS_TARGETING
                    and action.get("_source_card_name") and action.get("_source_controller")):
                legal, reason = _validate_target_for_action(
                    game, card, player, action["_source_card_name"], action["_source_controller"])
                if not legal:
                    print(f"[TARGETING] Move blocked: {card.name} — {reason}")
                    return f"🛡️ **{card.name}** can't be targeted ({reason})"
            # Tokens cease to exist when they leave the battlefield (MTG rule 111.8)
            if getattr(card, 'is_token', False) and to_zone.lower() != 'battlefield':
                zone_map_tok = {
                    'hand': player.hand, 'battlefield': player.battlefield,
                    'graveyard': player.graveyard, 'exile': player.exile, 'library': player.library,
                }
                from_list_tok = zone_map_tok.get(from_zone.lower())
                if from_list_tok is not None and card in from_list_tok:
                    if from_zone.lower() == 'battlefield' and to_zone.lower() == 'exile':
                        _fire_creature_exiled_watchers(game, card)
                    if from_zone.lower() == 'battlefield':
                        game.unregister_static_effects(card)
                    from_list_tok.remove(card)
                    print(f"[TOKEN-SBA] Token {card.name} ceased to exist (moved from {from_zone} to {to_zone})")
                    return f"📦 Token **{card.name}** ceases to exist"

            zone_map = {
                'hand': player.hand,
                'battlefield': player.battlefield,
                'graveyard': player.graveyard,
                'exile': player.exile,
                'library': player.library,
            }

            from_list = zone_map.get(from_zone.lower())
            to_list = zone_map.get(to_zone.lower())

            if from_list is not None and to_list is not None and card in from_list:
                # May 20 audit: clones (Phantasmal Image, Clone, Spark Double)
                # revert their copy effect when leaving the battlefield (CR 706.10).
                if from_zone.lower() == 'battlefield' and to_zone.lower() != 'battlefield':
                    _revert_copy_if_leaving_battlefield(card)
                    # Aug 7 confirmation-batch audit (C-2, MAJOR class): a
                    # permanent leaving the battlefield is removed from
                    # combat (CR 506.4) — this generic move path never
                    # stripped combat state, so Eerie Interlude exiling
                    # mid-combat attackers leaked .attacking=True through
                    # the exile→return round trip (Sun Titan + Trinket
                    # Mage, game_1535217860513890324; the [COMBAT-SWEEP]
                    # net caught it a full turn later). Any mid-combat
                    # exile/bounce/tuck through move_card had the leak.
                    from mtg.helpers import strip_combat_state
                    strip_combat_state(game, card)
                # [REPLACEMENT] Check for graveyard/death replacement (Rest in Peace → exile instead)
                actual_to_zone = to_zone.lower()
                if actual_to_zone == 'graveyard' and HAS_REPLACEMENT_ENGINE and game._replacement_engine and game._replacement_engine.effects:
                    # Determine event type: DEATH for creatures leaving battlefield, DISCARD from hand, generic move otherwise
                    if from_zone.lower() == 'battlefield':
                        evt_type = EventType.DEATH
                    elif from_zone.lower() == 'hand':
                        evt_type = EventType.DISCARD
                    else:
                        evt_type = EventType.DEATH  # Mill, etc. — use DEATH as catch-all for "goes to graveyard"
                    event = GameEvent(
                        event_type=evt_type,
                        affected_object=getattr(card, 'id', ''),
                        affected_object_name=card.name,
                        affected_player=player.name,
                        from_zone=from_zone.lower(),
                        to_zone="graveyard",
                    )
                    final = game._replacement_engine.process_event_sync(event)
                    if final.to_zone != "graveyard":
                        actual_to_zone = final.to_zone
                        to_list = zone_map.get(actual_to_zone, to_list)
                        print(f"  [REPLACEMENT-APPLY] Zone redirect: graveyard → {actual_to_zone} ({', '.join(final.replacement_chain)})")

                # [CR 903.9] Commander zone redirect for exile/hand/library.
                # Graveyard is handled by SBA dies handlers; here we catch the
                # direct move paths (Path to Exile, bounce, library tuck, etc.).
                # Autoplay always chooses the redirect (strictly better than
                # losing the commander).
                if (getattr(card, 'is_commander', False)
                        and game.format in COMMAND_ZONE_FORMATS
                        and actual_to_zone in ('exile', 'hand', 'library')):
                    owner_idx = getattr(card, 'owner_index', None)
                    if owner_idx is not None and 0 <= owner_idx < len(game.players):
                        owner = game.players[owner_idx]
                    else:
                        owner = player
                    if not hasattr(owner, 'command_zone') or owner.command_zone is None:
                        owner.command_zone = []
                    # Redirect destination to the owner's command zone
                    to_list = owner.command_zone
                    print(f"  [CR-903.9] Commander {card.name} redirected from {actual_to_zone} → command zone (owner={owner.name})")
                    actual_to_zone = 'command_zone'

                if from_zone.lower() == 'battlefield' and actual_to_zone == 'exile':
                    _fire_creature_exiled_watchers(game, card)

                # [LTB] Fire leaves-the-battlefield triggers before removal
                ltb_trigger_msgs = []
                if from_zone.lower() == 'battlefield' and hasattr(rules, 'engine_ref') and rules.engine_ref:
                    ltb_trigger_msgs = rules.engine_ref._check_ltb_triggers_sync(game, card, player, actual_to_zone)
                # [LAYERS] Unregister if leaving battlefield
                if from_zone.lower() == 'battlefield':
                    game.unregister_static_effects(card)
                from_list.remove(card)
                # Support position="top" for "put on top of library" effects
                position = action.get("position", "")
                if position == "top" and actual_to_zone == "library":
                    to_list.insert(0, card)
                else:
                    to_list.append(card)
                entry_trigger_msgs = []
                # [LAYERS] Register if entering battlefield, recalculate either way
                if actual_to_zone == 'battlefield':
                    # Reset damage, modifiers from previous zone (prevents reanimated creatures dying to stale damage)
                    card.damage_marked = 0
                    card.deathtouch_damage = 0
                    # Zone changes reset tapped status unless the effect says
                    # the permanent enters tapped (Victimize, etc.).
                    card.tapped = bool(action.get("tapped", False))
                    card.summoning_sick = True if card.is_creature() else False
                    card.entered_this_turn = True
                    # June 10 audit (V8): initialize planeswalker loyalty on
                    # non-cast battlefield entry (Rashmi free-cast, reanimation
                    # via move_card). The cast path does this in spells.py;
                    # without it the PW entered at 0 loyalty and died to SBA
                    # before any player-visible line existed (a Teferi "death"
                    # for a card that never visibly entered).
                    if card.is_planeswalker():
                        try:
                            card.loyalty_counters = int(card.loyalty)
                        except (TypeError, ValueError):
                            card.loyalty_counters = 0
                        # July 20 (Jeska, Thrice Reborn): commander-cast bonus,
                        # same as the cast path.
                        from mtg.helpers import loyalty_from_commander_casts
                        card.loyalty_counters += loyalty_from_commander_casts(
                            game, player, card)
                        print(f"[MOVE-CARD] {card.name} enters with {card.loyalty_counters} loyalty")
                    # [AURA-ETB] Auto-attach auras entering via non-cast paths (CR 303.4f)
                    # When an Aura enters the battlefield without being cast, controller
                    # chooses a legal target to attach it to.
                    if card.is_enchantment() and 'aura' in (card.type_line or '').lower():
                        attached = False
                        enchant_type = 'creature'  # Default
                        oracle_lower = (card.oracle_text or '').lower()
                        # July 30 batch-9 reviewer audit: "Enchant creature
                        # card in a graveyard" (Animate Dead / Dance of the
                        # Dead) must NOT auto-attach to a battlefield
                        # creature — this fallback ran BEFORE the name-keyed
                        # reanimate template and stuck Animate Dead's -1/-0
                        # on Thassa permanently while the template
                        # reanimated Restoration Angel
                        # (game_1532224002137784391). The reanimate action
                        # attaches the aura to the creature it returns;
                        # everything below (static registration, the
                        # PERMANENT_ENTERED emit, entry triggers) still runs.
                        if 'card in a graveyard' in oracle_lower:
                            attached = True  # suppress the search + SBA note
                            print(f"[AURA-ETB] {card.name} enchants a card in "
                                  f"a graveyard — skipping battlefield "
                                  f"auto-attach (reanimate handler attaches)")
                        elif 'enchant land' in oracle_lower or 'enchant forest' in oracle_lower or 'enchant plains' in oracle_lower:
                            enchant_type = 'land'
                        elif 'enchant permanent' in oracle_lower:
                            enchant_type = 'permanent'
                        elif 'enchant artifact' in oracle_lower:
                            enchant_type = 'artifact'
                        # Find best target on controller's battlefield
                        # (skipped when the graveyard-enchant branch above
                        # already claimed the attach).
                        for target_card in (player.battlefield if not attached else []):
                            if target_card.id == card.id:
                                continue
                            type_match = False
                            if enchant_type == 'creature' and target_card.is_creature():
                                type_match = True
                            elif enchant_type == 'land' and target_card.is_land():
                                # Check subtype match (Enchant Forest, etc.)
                                if 'forest' in oracle_lower and 'forest' in (target_card.type_line or '').lower():
                                    type_match = True
                                elif 'plains' in oracle_lower and 'plains' in (target_card.type_line or '').lower():
                                    type_match = True
                                elif 'enchant land' in oracle_lower:
                                    type_match = True
                            elif enchant_type == 'permanent':
                                type_match = True
                            elif enchant_type == 'artifact' and target_card.is_artifact():
                                type_match = True
                            if type_match:
                                card.attached_to = target_card.id
                                if not hasattr(target_card, 'attachments'):
                                    target_card.attachments = []
                                target_card.attachments.append(card.id)
                                attached = True
                                print(f"[AURA-ETB] {card.name} auto-attached to {target_card.name}")
                                break
                        if not attached:
                            print(f"[AURA-ETB] {card.name}: no valid {enchant_type} to enchant — will be removed by SBA")
                    game.register_static_keyword_grants(card, player.name)
                    game.register_static_pt_effects(card, player.name)
                    game.register_replacement_effects(card, player.name)
                    # Pub/sub slice 2: one PERMANENT_ENTERED per physical
                    # entry — unconditional, unlike the engine_ref-gated
                    # trigger processing below.
                    events.emit(events.PERMANENT_ENTERED, game, card=card,
                                controller=player, via="move_card", rules=rules)
                    # Fire landfall triggers when lands enter via spells
                    # (Nature's Lore, Three Visits, Wood Elves ETB, etc.)
                    if card.is_land() and hasattr(rules, 'engine_ref') and rules.engine_ref:
                        try:
                            land_etb_msgs = rules.engine_ref._handle_land_etb(game, player, card)
                            if land_etb_msgs:
                                if not hasattr(game, '_pending_trigger_messages'):
                                    game._pending_trigger_messages = []
                                if isinstance(land_etb_msgs, list):
                                    game._pending_trigger_messages.extend(land_etb_msgs)
                                elif isinstance(land_etb_msgs, str):
                                    game._pending_trigger_messages.append(land_etb_msgs)
                                print(f"[LANDFALL] Fired triggers for {card.name} entering via spell")
                        except Exception as e:
                            print(f"[LANDFALL] Error firing triggers for {card.name}: {e}")
                            maybe_reraise(e)
                    elif hasattr(rules, 'engine_ref') and rules.engine_ref:
                        # June 11 audit: action-driven entries (notably Dread
                        # Return -> Craterhoof) bypassed the cast pipeline, so
                        # their own ETBs and creature-enter watchers never fired.
                        entry_trigger_msgs = _fire_noncast_battlefield_entry(
                            rules, game, player, card)
                if from_zone.lower() == 'battlefield' or actual_to_zone == 'battlefield':
                    game.recalculate_granted_keywords()
                    game.recalculate_power_toughness()
                rules.log_event(f"Moved {card.name} from {from_zone} to {actual_to_zone}")
                reason = action.get("reason", "") or ""
                # May 14 audit (L5): zone-change messages like "📦 Inferno Titan
                # → exile" gave players no idea WHY a permanent was exiled.
                # Surface the action's `source` or `reason` field when provided.
                source = (action.get("source") or "").strip()
                if (from_zone.lower() == 'library'
                        and actual_to_zone == 'battlefield'
                        and 'cast' in reason.lower()
                        and 'free' in reason.lower()):
                    msg = f"✨ **{card.name}** is cast for free ({reason})"
                    # July 24 (slice 4b groundwork): template free-cast moves
                    # (Rashmi-class "cast it without paying") are real casts —
                    # queue battlefield cast triggers for the Tier-3 drain +
                    # emit CARD_CAST (parity-paired inside the helper).
                    _er = getattr(rules, 'engine_ref', None)
                    if _er is not None:
                        from mtg.triggers import queue_cast_triggers_sync
                        queue_cast_triggers_sync(_er, game, player, card,
                                                 via="free_cast_move")
                else:
                    # July 20 batch-3 audit (reviewer S1): battlefield entries
                    # carry the controller's name — "📦 **Forest** →
                    # battlefield" was byte-identical for BOTH players' Fall
                    # of the Thran returns, so _autoplay_send's Layer-1 dedup
                    # silently ate 2 of the 4 lines (the drain site also
                    # collapses same-player duplicates into ×N now).
                    _who = f" ({player.name})" if actual_to_zone == 'battlefield' else ""
                    # July 24 batch-6 audit: face-down exile returns (Necropotence)
                    # must not name the card in the shared Discord thread — the
                    # opponent never learned it. Console log_event above keeps
                    # the true name for audits.
                    # July 30: persist face-down-ness ON the card so the
                    # per-player serializer (GameState.visible_state) can
                    # mask it — display-level hiding alone leaks through any
                    # full-state serialization (the frontend's network tab).
                    if action.get("hide_card_name") and actual_to_zone == 'exile':
                        card._face_down = True
                    elif actual_to_zone in ('hand', 'battlefield', 'graveyard', 'library'):
                        card._face_down = False
                    _shown = ("a face-down card" if action.get("hide_card_name")
                              else f"**{card.name}**")
                    _display_zone = actual_to_zone.replace('_', ' ')
                    if source:
                        msg = f"📦 {_shown} → {_display_zone}{_who} (from {source})"
                    elif reason:
                        msg = f"📦 {_shown} → {_display_zone}{_who} ({reason})"
                    else:
                        msg = f"📦 {_shown} → {_display_zone}{_who}"
                if ltb_trigger_msgs:
                    msg += "\n" + "\n".join(ltb_trigger_msgs)
                if entry_trigger_msgs:
                    msg += "\n" + "\n".join(entry_trigger_msgs)
                return msg
    
    # ---- CHOOSE CREATURE TYPE (Species Specialist, etc.) ----
    elif action_type == "choose_creature_type":
        from collections import Counter

        choose_player = find_player(action.get("player", ""))
        source_name = (action.get("card") or action.get("source") or "").lower()
        if choose_player and source_name:
            source_card = next(
                (c for c in choose_player.battlefield
                 if c.name.lower() == source_name), None)
            if source_card:
                type_counts = Counter()
                first_seen = []
                for candidate in choose_player.battlefield:
                    if candidate is source_card or not candidate.is_creature(game):
                        continue
                    for creature_type in candidate.get_creature_types():
                        key = creature_type.lower()
                        type_counts[key] += 1
                        if key not in first_seen:
                            first_seen.append(key)
                if type_counts:
                    chosen_key = max(
                        first_seen, key=lambda key: type_counts[key])
                    chosen = next(
                        creature_type
                        for candidate in choose_player.battlefield
                        for creature_type in candidate.get_creature_types()
                        if creature_type.lower() == chosen_key)
                else:
                    own_types = source_card.get_creature_types()
                    chosen = own_types[0] if own_types else "Human"
                source_card._chosen_creature_type = chosen
                print(f"[CHOOSE-CREATURE-TYPE] {source_card.name}: {chosen}")
                return f"🧬 **{source_card.name}** chooses {chosen}"
        return None

    elif action_type == "select_from_top":
        # Calix/Growing Rites family: inspect the top N, put one matching card
        # into hand, and put the rest on the bottom in random order.
        player = find_player(action.get("player", ""))
        amount = int(action.get("amount", 1))
        card_type = (action.get("card_type", "") or "").lower()
        if not player or amount <= 0 or not player.library:
            return None
        top_n = list(player.library[:amount])
        del player.library[:len(top_n)]
        chosen = next((c for c in top_n
                       if card_type in (getattr(c, 'type_line', '') or '').lower()), None)
        if chosen:
            top_n.remove(chosen)
            player.hand.append(chosen)
        import random as _rng
        _rng.shuffle(top_n)
        player.library.extend(top_n)
        if chosen:
            return f"👁️ {player.name} reveals {chosen.name} and puts it into their hand"
        return f"👁️ {player.name} finds no {card_type} among the top {len(top_n)} cards"

    elif action_type == "manifest_top":
        # CR 701.34: move actual cards from the top of the library onto the
        # battlefield face down as colorless 2/2 creatures. Preserve their
        # copiable values for a future turn-face-up action; do not create
        # tokens or leave the cards in the library.
        player = find_player(action.get("player", ""))
        count = max(0, int(action.get("count", 1)))
        if not player or count <= 0:
            return None
        manifested = []
        entry_messages = []
        for _ in range(min(count, len(player.library))):
            card = player.library.pop(0)
            card._manifest_original = {
                "name": card.name, "type_line": card.type_line,
                "oracle_text": card.oracle_text, "mana_cost": card.mana_cost,
                "cmc": card.cmc, "power": card.power,
                "toughness": card.toughness, "loyalty": card.loyalty,
                "keywords": list(card.keywords or []),
                "colors": list(getattr(card, 'colors', []) or []),
                "has_transform": card.has_transform,
            }
            card._manifested = True
            card.name = "Manifest"
            card.type_line = "Creature"
            card.oracle_text = ""
            card.mana_cost = ""
            card.cmc = 0
            card.power = "2"
            card.toughness = "2"
            card.loyalty = None
            card.keywords = []
            card.colors = []
            card.has_transform = False
            card.tapped = False
            card.summoning_sick = True
            card.entered_this_turn = True
            card.controller = player.name
            player.battlefield.append(card)
            manifested.append(card)
            entry_messages.extend(
                _fire_noncast_battlefield_entry(
                    rules, game, player, card))
            events.emit(events.PERMANENT_ENTERED, game, card=card,
                        controller=player, via="manifest_top", rules=rules)
        if not manifested:
            return None
        game.recalculate_granted_keywords()
        game.recalculate_power_toughness()
        result = f"{player.name} manifests {len(manifested)} card(s)"
        if entry_messages:
            result += "\n" + "\n".join(entry_messages)
        print(f"[MANIFEST] {player.name}: {len(manifested)} card(s)")
        return result

    elif action_type == "transform_permanent":
        player = find_player(action.get("player", ""))
        card_name = action.get("card", "")
        if not player:
            return None
        permanent = next((c for c in player.battlefield
                          if c.name.lower() == card_name.lower()), None)
        if not permanent or not permanent.has_transform:
            return None
        old_name = permanent.name
        game.unregister_static_effects(permanent)
        if not permanent.transform():
            return None
        game.register_static_keyword_grants(permanent, player.name)
        game.register_static_pt_effects(permanent, player.name)
        game.register_replacement_effects(permanent, player.name)
        game.recalculate_granted_keywords()
        game.recalculate_power_toughness()
        return f"🌞 **{old_name}** transforms into **{permanent.name}**"

    elif action_type == "imprint_isochron":
        player = find_player(action.get("player", ""))
        source_name = action.get("source", "Isochron Scepter")
        if not player:
            return None
        scepter = next((c for c in player.battlefield
                        if c.name.lower() == source_name.lower()), None)
        candidates = [c for c in player.hand
                      if c.is_instant() and (getattr(c, 'cmc', 0) or 0) <= 2]
        if not scepter or not candidates:
            return None
        imprinted = max(candidates, key=lambda c: getattr(c, 'cmc', 0) or 0)
        player.hand.remove(imprinted)
        player.exile.append(imprinted)
        scepter._imprinted_card_id = imprinted.id
        scepter._imprinted_card_name = imprinted.name
        scepter._imprinted_owner_index = game.players.index(player)
        return f"📜 {source_name} imprints **{imprinted.name}**"

    elif action_type == "isochron_copy":
        player = find_player(action.get("player", ""))
        source_name = action.get("source", "Isochron Scepter")
        if not player:
            return None
        scepter = next((c for c in player.battlefield
                        if c.name.lower() == source_name.lower()), None)
        if not scepter or not getattr(scepter, '_imprinted_card_id', None):
            return f"🚫 {source_name} has no imprinted card"
        imprinted = next((c for p in game.players for c in p.exile
                          if c.id == scepter._imprinted_card_id), None)
        if not imprinted:
            return f"🚫 {source_name}'s imprinted card is no longer in exile"
        try:
            from rules.effect_templates import get_effect_library, build_game_context
            opponent = next((p for p in game.players if p is not player), player)
            ctx = build_game_context(
                game, player, opponent, card=imprinted,
                explicit_target=action.get("target") or None)
            generated, explanation = get_effect_library().resolve_spell(
                imprinted.name, imprinted.oracle_text or '', player.name,
                opponent.name, game_context=ctx)
            messages = []
            for generated_action in generated or []:
                if generated_action.get('action') == 'no_action':
                    continue
                generated_action.setdefault('_source_card_name', imprinted.name)
                generated_action.setdefault('_source_controller', player.name)
                generated_action.setdefault('_source_oracle', imprinted.oracle_text or '')
                msg = execute_action_on_state(rules, game, generated_action)
                if msg:
                    messages.append(msg)
            if messages:
                return f"📜 {source_name} copies **{imprinted.name}**:\n" + "\n".join(messages)
            return f"📜 {source_name} copies **{imprinted.name}** ({explanation or 'no deterministic actions'})"
        except (ImportError, ValueError, TypeError, AttributeError, KeyError) as e:
            print(f"[ISOCHRON] Failed to resolve imprinted {imprinted.name}: {e}")
            maybe_reraise(e)
            return None

    elif action_type == "wishclaw_tutor_transfer":
        player = find_player(action.get("player", ""))
        if not player:
            return None
        opponent = next((p for p in game.players if p is not player), None)
        source_name = action.get("source", "Wishclaw Talisman")
        source = next((c for c in player.battlefield
                       if c.name.lower() == source_name.lower()), None)
        if not source or not player.library:
            return None
        chosen = max(player.library, key=lambda c: getattr(c, 'cmc', 0) or 0)
        player.library.remove(chosen)
        player.hand.append(chosen)
        import random as _rng
        _rng.shuffle(player.library)
        transfer_msg = None
        if opponent:
            transfer_msg = execute_action_on_state(rules, game, {
                "action": "steal_permanent", "player": opponent.name,
                "from_player": player.name, "card": source.name,
                "source": source.name})
        msg = f"🔍 {player.name} tutors **{chosen.name}** with {source.name}"
        return msg + (f"\n{transfer_msg}" if transfer_msg else "")

    elif action_type == "double_creature_tokens":
        player = find_player(action.get("player", ""))
        if not player:
            return None
        originals = [c for c in list(player.battlefield)
                     if getattr(c, 'is_token', False) and c.is_creature()]
        created = []
        for original in originals:
            count = 1
            if HAS_REPLACEMENT_ENGINE and game._replacement_engine and game._replacement_engine.effects:
                event = GameEvent(event_type=EventType.TOKEN_CREATED,
                                  affected_player=player.name, amount=1,
                                  source_name="Rhys the Redeemed")
                count = game._replacement_engine.process_event_sync(event).amount
            for _ in range(count):
                token = Card(
                    name=original.name, mana_cost=original.mana_cost or "",
                    cmc=original.cmc or 0, type_line=original.type_line or "Creature",
                    oracle_text=original.oracle_text or "", power=original.power or "0",
                    toughness=original.toughness or "0")
                token.is_token = True
                token.keywords = list(original.keywords or [])
                token.color_identity = list(original.color_identity or [])
                token.summoning_sick = True
                token.entered_this_turn = True
                player.battlefield.append(token)
                created.append(token.name)
                _fire_noncast_battlefield_entry(rules, game, player, token)
                events.emit(events.PERMANENT_ENTERED, game, card=token,
                            controller=player, via="create_token", rules=rules)
        if created:
            return f"🌿 {player.name} creates {len(created)} token copies"
        return None

    # ---- PROLIFERATE (Atraxa, Flux Channeler, Evolution Sage, Karn's Bastion) ----
    # CR 701.27: choose any number of permanents and/or players with a counter,
    # then give each another counter of a kind already there. The controller
    # chooses, so we proliferate only what helps them: their own permanents'
    # counters (skipping -1/-1), loyalty on their own planeswalkers, their own
    # energy, and poison/-1/-1 on opponents. It NEVER touches life — May 26 audit:
    # with no lower-tier handler, Atraxa's end-step proliferate escalated to Tier 3,
    # which hallucinated a +1/-1 life swing (proliferate has no life component).
    elif action_type == "proliferate":
        prolif = find_player(action.get("player", ""))
        if prolif is None:
            prolif = game.players[game.active_player_index]
        proliferated = []
        for p in game.players:
            is_own = (p is prolif)
            for c in list(p.battlefield):
                ctrs = getattr(c, 'counters', None)
                if ctrs:
                    for ctype, cnt in list(ctrs.items()):
                        if cnt <= 0:
                            continue
                        beneficial = (ctype != '-1/-1')
                        # own -> proliferate beneficial; opponent -> only detrimental
                        if is_own != beneficial:
                            continue
                        ctrs[ctype] = cnt + 1
                        proliferated.append(f"{c.name} ({ctype})")
                # Loyalty on own planeswalkers (stored separately from counters dict)
                if is_own and getattr(c, 'loyalty_counters', 0) and c.loyalty_counters > 0 \
                        and 'planeswalker' in (c.type_line or '').lower():
                    c.loyalty_counters += 1
                    proliferated.append(f"{c.name} (loyalty)")
            # Player-level counters: own energy (beneficial), opponents' poison
            if is_own and getattr(p, 'energy', 0) > 0:
                p.energy += 1
                proliferated.append(f"{p.name} (energy)")
            if (not is_own) and getattr(p, 'poison', 0) > 0:
                p.poison += 1
                proliferated.append(f"{p.name} (poison)")
        game.recalculate_power_toughness()
        if not proliferated:
            print(f"[PROLIFERATE] {prolif.name}: nothing to proliferate")
            return None
        shown = ", ".join(proliferated[:8]) + (f" +{len(proliferated) - 8} more" if len(proliferated) > 8 else "")
        print(f"[PROLIFERATE] {prolif.name}: {len(proliferated)} target(s) — {shown}")
        return f"🔬 **{prolif.name}** proliferates: {shown}"

    # ---- COUNTERS ----
    elif action_type == "add_counters":
        counter_type = action.get("counter_type", "+1/+1")
        amount = int(action.get("amount", 1))
        # Bulk target ("all_own_creatures" / "all_creatures_you_control") —
        # Cathar's Crusade, Inspiring Call, etc. May 13 audit: previously the
        # Cathar's Crusade template emitted N add_counters actions, one per
        # controller creature, looked up by name. When N tokens shared a
        # name ("Plant", "Soldier"), `find_card_on_battlefield` returned the
        # SAME first token every time, so all N counters stacked on one
        # token (a power=2694 mega-Soldier next to thirty-one 6/6 siblings).
        # Bulk mode iterates by identity, not by name.
        target_spec = action.get("target", "")
        if target_spec in ("all_own_creatures", "all_creatures_you_control"):
            player = find_player(action.get("player", ""))
            if not player:
                return None
            affected = 0
            for c in list(player.battlefield):
                if not c.is_creature():
                    continue
                # Apply replacement effects (Doubling Season, Hardened Scales) per-target.
                per_target_amount = amount
                if HAS_REPLACEMENT_ENGINE and game._replacement_engine and game._replacement_engine.effects:
                    event = GameEvent(
                        event_type=EventType.COUNTER_PLACED,
                        affected_object=c.id,
                        affected_object_name=c.name,
                        affected_player=player.name,
                        amount=per_target_amount,
                        counter_type=counter_type,
                        source_name=action.get("source", ""),
                    )
                    final = game._replacement_engine.process_event_sync(event)
                    if final.amount != per_target_amount:
                        per_target_amount = final.amount
                c.counters[counter_type] = c.counters.get(counter_type, 0) + per_target_amount
                affected += 1
            if counter_type in ('+1/+1', '-1/-1'):
                game.recalculate_power_toughness()
            if affected == 0:
                return None
            if action.get("_silent"):
                print(f"[COUNTER-BULK-SILENT] {affected} creature(s) gain {amount} {counter_type}")
                return None
            return f"⭕ {amount} {counter_type} counter(s) on each of **{player.name}**'s {affected} creatures"
        result = find_card_on_battlefield(action.get("card", ""))
        if result:
            card, owner = result
            # [REPLACEMENT] Process counter placement effects (Doubling Season, Hardened Scales)
            if HAS_REPLACEMENT_ENGINE and game._replacement_engine and game._replacement_engine.effects:
                event = GameEvent(
                    event_type=EventType.COUNTER_PLACED,
                    affected_object=card.id,
                    affected_object_name=card.name,
                    affected_player=owner.name,
                    amount=amount,
                    counter_type=counter_type,
                    source_name=action.get("source", ""),
                )
                final = game._replacement_engine.process_event_sync(event)
                if final.amount != amount:
                    print(f"  [REPLACEMENT-APPLY] Counters modified: {amount} → {final.amount} ({', '.join(final.replacement_chain)})")
                amount = final.amount
            card.counters[counter_type] = card.counters.get(counter_type, 0) + amount
            # [LAYERS] Recalculate P/T when counters change
            if counter_type in ('+1/+1', '-1/-1'):
                game.recalculate_power_toughness()
            if action.get("_silent"):
                print(f"[COUNTER-SILENT] {card.name} now has {card.counters[counter_type]} {counter_type} counters")
                return None
            return f"⭕ {amount} {counter_type} counter(s) on **{card.name}** (total: {card.counters[counter_type]})"
    
    elif action_type == "remove_counters":
        result = find_card_on_battlefield(action.get("card", ""))
        if result:
            card, owner = result
            counter_type = action.get("counter_type", "+1/+1")
            amount = int(action.get("amount", 1))
            current = card.counters.get(counter_type, 0)
            card.counters[counter_type] = max(0, current - amount)
            # [LAYERS] Recalculate P/T when counters change
            if counter_type in ('+1/+1', '-1/-1'):
                game.recalculate_power_toughness()
            return f"⭕ Removed {amount} {counter_type} counter(s) from **{card.name}** (total: {card.counters[counter_type]})"
    
    # ---- CREATE TOKEN ----
    elif action_type == "create_token":
        player = find_player(action.get("player", ""))
        if player:
            count = int(action.get("count", 1))
            token_name = action.get("name", "Token")
            power = action.get("power", 0)
            toughness = action.get("toughness", 0)
            if power != "*":
                power = int(power)
            if toughness != "*":
                toughness = int(toughness)
            types = action.get("types", "Creature — Token")
            token_oracle = action.get("oracle_text", "")

            # [REPLACEMENT] Process token creation effects (Doubling Season, Parallel Lives)
            if HAS_REPLACEMENT_ENGINE and game._replacement_engine and game._replacement_engine.effects:
                event = GameEvent(
                    event_type=EventType.TOKEN_CREATED,
                    affected_player=player.name,
                    amount=count,
                    source_name=action.get("source", token_name),
                )
                final = game._replacement_engine.process_event_sync(event)
                if final.amount != count:
                    print(f"  [REPLACEMENT-APPLY] Token count modified: {count} → {final.amount} ({', '.join(final.replacement_chain)})")
                count = final.amount

            created_tokens = []
            # [TOKEN] Give tokens from create_token the same full state the
            # hardcoded token-creators set (owner_index, explicit stable id).
            # Combat-time resolution and attachment lookups use `.id` —
            # tokens without owner_index or with colliding names would fail.
            owner_idx = game.players.index(player) if player in game.players else 0
            # Forward keywords + colors from the action dict to the produced
            # tokens (Hornet Queen Insects need flying/deathtouch, Bitterblossom
            # Faerie Rogues need flying, Talrand's Drakes need flying, etc.).
            # Without this the registry-listed keywords were silently dropped.
            # May 20 audit: also accept list inputs (Vivien MA template passes
            # `["Trample"]`) — the old comma-split path crashed with
            # "'list' object has no attribute 'split'" after each token create.
            tok_kw_raw = action.get("keywords", "")
            if isinstance(tok_kw_raw, list):
                tok_keywords = [str(k).strip() for k in tok_kw_raw if str(k).strip()]
            else:
                tok_keywords = [k.strip() for k in tok_kw_raw.split(",") if k.strip()] if tok_kw_raw else []
            tok_color_raw = action.get("colors", "")
            if isinstance(tok_color_raw, list):
                tok_colors = [str(c).strip() for c in tok_color_raw if str(c).strip()]
            else:
                tok_colors = [c.strip() for c in tok_color_raw.split(",") if c.strip()] if tok_color_raw else []
            for i in range(count):
                token = Card(
                    name=token_name,
                    mana_cost="",
                    cmc=0,
                    type_line=types,
                    oracle_text=token_oracle,
                    power=str(power),
                    toughness=str(toughness),
                )
                # Force a fresh, stable id (the dataclass auto-id runs in
                # __post_init__ but we re-assert here for clarity and to
                # guarantee uniqueness across rapid-fire token creation)
                import uuid as _uuid
                token.id = f"token_{token_name.replace(' ', '_')}_{_uuid.uuid4().hex[:8]}"
                token.is_token = True
                token.summoning_sick = True
                token.entered_this_turn = True
                token.owner_index = owner_idx
                if tok_keywords:
                    token.keywords = list(tok_keywords)
                if tok_colors:
                    token.colors = list(tok_colors)
                if not hasattr(token, 'attachments') or token.attachments is None:
                    token.attachments = []
                # May 20 audit: honor `tapped` + `attacking` flags on the
                # action dict for tokens that enter tapped-and-attacking
                # (Goblin Rabblemaster — "create a 1/1 red Goblin creature
                # token that's tapped and attacking"). Previously the
                # Rabblemaster token entered untapped and didn't attack with
                # the trigger source. CR 116.3a — these tokens skip the
                # normal declare-attackers step and ARE attacking the same
                # defender as the source.
                if action.get("tapped"):
                    token.tapped = True
                if action.get("attacking"):
                    token.attacking = True
                    # Inherit the source attacker's target (defender) if known
                    src_atk_player = action.get("attacking_player")
                    if src_atk_player:
                        token.attacking_player = src_atk_player
                    # Aug 1 batch-12 follow-up: an attacking token must JOIN
                    # game.attackers — every per-combat clear (and the damage
                    # resolution itself) iterates that list, so a token that
                    # only set the flag neither dealt combat damage via the
                    # normal loop nor ever got its flag cleared (the stale-
                    # flag leak class).
                    game.attackers.append(token.id)
                player.battlefield.append(token)
                created_tokens.append(token)
                # Pub/sub slice 2: one PERMANENT_ENTERED per token entering.
                events.emit(events.PERMANENT_ENTERED, game, card=token,
                            controller=player, via="create_token", rules=rules)

            # Fire creature-enters triggers for each token (Aura Shards, Soul Warden, etc.)
            # Cathar's Crusade + Avenger of Zendikar can produce N×N counter messages —
            # collect all msgs first, then collapse identical-prefix counter cascades
            # into one summary line per (counter_type, target) pair.
            # Slice 2b (July 21): the per-token PERMANENT_ENTERED emits above
            # ran the creature watcher dispatch (subscriber). Drain here so
            # the counter-cascade aggregation below still applies.
            token_trigger_msgs = []
            from mtg.helpers import drain_pending_messages as _drain_pm
            for msg in _drain_pm(game):
                print(f"[TOKEN-TRIGGER] {msg}")
                token_trigger_msgs.append(msg)
            if len(token_trigger_msgs) > 12:
                token_trigger_msgs = rules._aggregate_counter_msgs(token_trigger_msgs)

            # [LAYERS] Recalculate after tokens enter (anthems affect new creatures)
            game.recalculate_granted_keywords()
            game.recalculate_power_toughness()
            rules.log_event(f"{player.name} creates {count}x {token_name}")
            # Only show P/T for creature tokens — non-creature artifact
            # tokens like Treasure, Food, Clue get default power=0/toughness=0
            # which otherwise renders as a confusing "0/0".
            is_creature_token = 'creature' in types.lower()
            if is_creature_token:
                # Apr 29 audit: show effective P/T after anthems (Intangible
                # Virtue, Anointed Procession-style boosts), not the raw printed
                # values. Without this, "Human (1/1)" deals 2 damage and the
                # player is left wondering where the buff came from.
                eff_p, eff_t = power, toughness
                if created_tokens:
                    sample = created_tokens[0]
                    try:
                        eff_p = sample.get_effective_power(game)
                        eff_t = sample.get_effective_toughness(game)
                    except Exception:
                        pass
                # May 25 audit (F18): surface creature-token keywords (flying,
                # deathtouch, etc.) in the display so Swan Song Birds read as
                # "(2/2 flying)" not bare "(2/2)". `tok_keywords` was populated
                # earlier in this block from action.get("keywords"); they were
                # being attached to the Card object but never shown to the
                # player. Hornet Queen tokens, Bitterblossom Faeries, Talrand
                # Drakes all benefit.
                kw_suffix = ""
                if tok_keywords:
                    kw_suffix = " " + ", ".join(k.lower() for k in tok_keywords)
                if (eff_p, eff_t) != (power, toughness):
                    # May 25 audit (F4 deep-dive): when a Layer 7b set-effect
                    # (Humility, Lignify, Song of the Dryads) overwrote a token's
                    # CDA value, the action-dict P/T no longer represents the
                    # token's effective base. Showing "base 8/8" alongside "(2/2)"
                    # implies the token "really" has an 8/8 base under the surface,
                    # which is what misled the May 25 audit into flagging Voice
                    # of Resurgence Elementals as broken (the math was CR-correct:
                    # Voice CDA Layer 7a → Humility Layer 7b set to 1 → anthem
                    # Layer 7c net 2/2). Suppress "base X/Y" when eff < printed
                    # (negative delta = set-effect active). Anthem/counter buffs
                    # (positive delta) keep the suffix because "base X/Y, eff
                    # (X+1)/(Y+1)" is meaningful context for the player.
                    if power == "*" or toughness == "*" or eff_p < power or eff_t < toughness:
                        base_msg = f"🪙 **{player.name}** creates {count}x **{token_name}** ({eff_p}/{eff_t}{kw_suffix})"
                    else:
                        base_msg = f"🪙 **{player.name}** creates {count}x **{token_name}** ({eff_p}/{eff_t}{kw_suffix}, base {power}/{toughness})"
                else:
                    base_msg = f"🪙 **{player.name}** creates {count}x **{token_name}** ({power}/{toughness}{kw_suffix})"
            else:
                base_msg = f"🪙 **{player.name}** creates {count}x **{token_name}**"
            if token_trigger_msgs:
                return base_msg + "\n" + "\n".join(token_trigger_msgs)
            return base_msg

    elif action_type == "create_copy_token":
        # Clone an existing creature as a token.
        # Used by: Thousand-Faced Shadow, Helm of the Host, Rite of Replication,
        # and (with zone="graveyard") Feldon of the Third Path.
        player_name = action.get("player")
        target_name = action.get("target", "")
        target_filter = action.get("filter", "")  # "attacking", "own", "any"
        count = int(action.get("count", 1))
        # July 27 fanout: "a copy of target creature CARD in your graveyard"
        # (Feldon) was matched by the generic BATTLEFIELD copy pattern, which
        # copied a live creature — observed with no creature in any graveyard.
        # A card in a graveyard is not a permanent, so the battlefield scan
        # below can never see it; it needs its own zone.
        zone = (action.get("zone") or "battlefield").lower()
        p = find_player(player_name)
        if p:
            # Find the creature to copy
            source_card = None
            if zone in ("graveyard", "exile"):
                # Aug 3: "exile" joined "graveyard" for embalm/eternalize
                # (CR 702.87a / 702.129a) — the card is exiled as the
                # activation COST, so by the time the copy is made the source
                # sits in exile, not the graveyard.
                zone_owner = (action.get("zone_owner") or "controller").lower()
                pools = [p] if zone_owner == "controller" else list(game.players)
                _pool_attr = "graveyard" if zone == "graveyard" else "exile"
                # Plain is_creature() is correct here: the devotion type-flip
                # (CR 207.4) only applies to permanents on the battlefield, so a
                # devotion-gated god in a graveyard IS a creature card.
                candidates = [c for pl in pools
                              for c in getattr(pl, _pool_attr, [])
                              if c.is_creature()]
                if target_name and target_name not in (
                        "best_creature", "best_attacking_creature"):
                    from mtg.helpers import names_match as _names_match
                    source_card = next(
                        (c for c in candidates if _names_match(c.name, target_name)),
                        None)
                if source_card is None and candidates:
                    source_card = max(candidates, key=lambda c: (c.cmc or 0))
                if source_card is None:
                    print(f"[COPY-TOKEN] No creature card in the "
                          f"{'controller' if zone_owner == 'controller' else 'targeted'} "
                          f"{_pool_attr} to copy")
                    return None
            elif target_name and target_name not in ("best_creature", "best_attacking_creature"):
                result = find_card_on_battlefield(target_name)
                if result:
                    source_card, _ = result

            if not source_card:
                # Auto-select: best creature matching filter
                candidates = []
                for pl in game.players:
                    for c in pl.battlefield:
                        if not c.is_creature():
                            continue
                        if target_filter == "attacking" and not getattr(c, 'attacking', False):
                            continue
                        if target_filter == "own" and c not in p.battlefield:
                            continue
                        candidates.append(c)

                if candidates:
                    # Pick the one with highest power
                    source_card = max(candidates, key=lambda c: c.get_effective_power(game))

            if source_card:
                # [REPLACEMENT] Process token creation (Doubling Season, etc.)
                actual_count = count
                if HAS_REPLACEMENT_ENGINE and game._replacement_engine and game._replacement_engine.effects:
                    event = GameEvent(
                        event_type=EventType.TOKEN_CREATED,
                        affected_player=p.name,
                        amount=count,
                        source_name=source_card.name,
                    )
                    final = game._replacement_engine.process_event_sync(event)
                    if final.amount != count:
                        print(f"  [REPLACEMENT-APPLY] Copy token count modified: {count} → {final.amount}")
                    actual_count = final.amount

                created_names = []
                copy_entry_msgs: List[str] = []
                # "except it's an artifact in addition to its other types"
                # (Feldon) and any keywords the copy is granted ("It gains haste").
                extra_types = action.get("extra_types") or []
                if isinstance(extra_types, str):
                    extra_types = [extra_types]
                extra_keywords = action.get("keywords") or []
                if isinstance(extra_keywords, str):
                    extra_keywords = [k.strip() for k in extra_keywords.split(",") if k.strip()]
                # Aug 3: copy-with-EXCEPTIONS (CR 706.2 lets a copy effect
                # name changes). Embalm's copy is "a white Zombie <types> with
                # no mana cost"; eternalize's is "a 4/4 black Zombie <types>
                # with no mana cost" — three characteristics the handler could
                # not express, so it produced a plain copy of the original.
                override_p = action.get("power")
                override_t = action.get("toughness")
                override_colors = action.get("colors")
                if isinstance(override_colors, str):
                    override_colors = [c.strip() for c in override_colors.split(",")
                                       if c.strip()]
                clear_mana_cost = bool(action.get("clear_mana_cost", False))
                extra_subtypes = action.get("extra_subtypes") or []
                if isinstance(extra_subtypes, str):
                    extra_subtypes = [x.strip() for x in extra_subtypes.split(",")
                                      if x.strip()]
                for i in range(actual_count):
                    # Create a token copy with the source creature's stats
                    copy_token = Card(
                        name=source_card.name,
                        mana_cost="" if clear_mana_cost else (source_card.mana_cost or ""),
                        cmc=0 if clear_mana_cost else (source_card.cmc or 0),
                        type_line=source_card.type_line or "Creature",
                        oracle_text=source_card.oracle_text or "",
                        power=(str(override_p) if override_p is not None
                               else (source_card.power or "0")),
                        toughness=(str(override_t) if override_t is not None
                                   else (source_card.toughness or "0")),
                    )
                    # July 27 fanout: is_token was never set here, so a copy
                    # token that died did NOT cease to exist (CR 111.7) — it sat
                    # in a graveyard and was later reanimated by Animate Dead.
                    # The sibling token paths (populate, create_token) both set
                    # it; this one was the odd one out.
                    copy_token.is_token = True
                    import uuid as _uuid
                    copy_token.id = f"token_{source_card.name.replace(' ', '_')}_{_uuid.uuid4().hex[:8]}"
                    copy_token.owner_index = game.players.index(p) if p in game.players else 0
                    # Copy keywords
                    if source_card.keywords:
                        copy_token.keywords = list(source_card.keywords)
                    for kw in extra_keywords:
                        if kw and kw not in copy_token.keywords:
                            copy_token.keywords.append(kw)
                    for t in extra_types:
                        if t and t.lower() not in (copy_token.type_line or "").lower():
                            copy_token.type_line = f"{str(t).capitalize()} {copy_token.type_line}".strip()
                    # SUBTYPES belong AFTER the em-dash (CR 205.3). Prepending
                    # them the way extra_types does yields "Zombie Creature —
                    # Angel", which puts Zombie in the supertype slot — the
                    # engine's subtype reader looks only after the dash, so an
                    # embalm token was not a Zombie to anything that checks.
                    for t in extra_subtypes:
                        if not t:
                            continue
                        tl = copy_token.type_line or "Creature"
                        if t.lower() in tl.split("—")[-1].lower():
                            continue
                        if "—" in tl:
                            head, tail = tl.split("—", 1)
                            copy_token.type_line = (
                                f"{head.strip()} — {str(t).capitalize()} {tail.strip()}")
                        else:
                            copy_token.type_line = f"{tl.strip()} — {str(t).capitalize()}"

                    # Copy color identity — unless the copy effect replaces
                    # the colors outright ("except it's a white Zombie ...").
                    if override_colors is not None:
                        copy_token.color_identity = list(override_colors)
                        copy_token.colors = list(override_colors)
                    elif source_card.color_identity:
                        copy_token.color_identity = list(source_card.color_identity)
                    copy_token.summoning_sick = True
                    copy_token.entered_this_turn = True
                    # July 30 (deferred July 28 item, CR 706.2): a copy
                    # effect copies the printed/copiable values only — NOT
                    # counters. The old loop cloned the source's +1/+1
                    # counters onto the token.
                    p.battlefield.append(copy_token)
                    created_names.append(copy_token.name)
                    # This site had NO entry plumbing at all. Two consequences,
                    # both fixed here by mirroring the `populate` sibling:
                    #  1. the copy's OWN ETB never resolved, so Rite of
                    #     Replication on a Mulldrifter drew nothing;
                    #  2. no PERMANENT_ENTERED, so Soul Warden-class watchers
                    #     never saw it and it was invisible to the slice-2 bus.
                    copy_entry_msgs.extend(
                        _fire_noncast_battlefield_entry(rules, game, p, copy_token))
                    events.emit(events.PERMANENT_ENTERED, game, card=copy_token,
                                controller=p, via="create_copy_token", rules=rules)

                copy_trigger_msgs = list(copy_entry_msgs)
                from mtg.helpers import drain_pending_messages as _drain_pm
                for msg in _drain_pm(game):
                    print(f"[COPY-TOKEN-TRIGGER] {msg}")
                    copy_trigger_msgs.append(msg)

                print(f"[COPY-TOKEN] {p.name} creates {actual_count}x copy of {source_card.name}"
                      f"{' from graveyard' if zone == 'graveyard' else ''}")
                # Report the TOKEN's characteristics, not the source's — an
                # eternalize copy is a 4/4 however big the original was.
                ep = copy_token.get_effective_power(game)
                et = copy_token.get_effective_toughness(game)
                base_copy_msg = (f"📋 **{p.name}** creates {actual_count}x token copy of "
                                 f"**{source_card.name}** ({ep}/{et})")
                if copy_trigger_msgs:
                    return base_copy_msg + "\n" + "\n".join(copy_trigger_msgs)
                return base_copy_msg
            else:
                print(f"[COPY-TOKEN] No valid creature to copy (filter={target_filter})")
                return None

    # ---- EQUIP ----
    elif action_type in ("equip", "attach"):
        # May 14 audit (C4): the Embercleave template (and any future template
        # that wants to attach an Equipment to a creature post-cast) emits
        # `{"action": "equip", "equipment": "X", "creature": "Y", "player": "Z"}`
        # but no handler existed — the action was silently dropped. Add one.
        # Used by: Embercleave ETB, Sigarda's Aid auto-attach, Stoneforge
        # Mystic equip-from-hand follow-up plans.
        # Aug 5 confirmation audit: Ardenn may move both Auras and Equipment.
        # Keep equip backward-compatible while exposing a neutral attach
        # spelling so Aura movement updates attached_to/attachments instead of
        # being misrepresented as a battlefield-to-battlefield zone move.
        is_generic_attach = action_type == "attach"
        attachment_name = (
            action.get("attachment", "") if is_generic_attach
            else action.get("equipment", "")
        )
        target_name = (
            action.get("target", "") if is_generic_attach
            else action.get("creature", "")
        )
        player_name = action.get("player", "")
        p = find_player(player_name)
        if not p or not attachment_name or not target_name:
            return None
        attachment = next(
            (c for c in p.battlefield
             if c.name.lower() == attachment_name.lower()),
            None,
        )
        target = next(
            (c for target_player in game.players
             for c in target_player.battlefield
             if c.name.lower() == target_name.lower()
             and (is_generic_attach or c.is_creature(game))),
            None,
        )
        if not attachment or not target:
            return None
        # Detach from previous target
        if attachment.attached_to:
            for target_player in game.players:
                previous = next(
                    (c for c in target_player.battlefield
                     if c.id == attachment.attached_to),
                    None,
                )
                if previous is not None:
                    if attachment.id in previous.attachments:
                        previous.attachments.remove(attachment.id)
                    break
        attachment.attached_to = target.id
        if not hasattr(target, 'attachments'):
            target.attachments = []
        if attachment.id not in target.attachments:
            target.attachments.append(attachment.id)
        if is_generic_attach:
            game.recalculate_power_toughness()
            print(f"[ATTACH-ACTION] {attachment.name} attached to {target.name}")
            return f"**{attachment.name}** attached to **{target.name}**"
        equip_card = attachment
        target_creature = target
        game.recalculate_power_toughness()
        print(f"[EQUIP-ACTION] {equip_card.name} attached to {target_creature.name}")
        return f"⚔️ **{equip_card.name}** equipped to **{target_creature.name}**"

    # ---- TAP/UNTAP ----
    elif action_type == "tap":
        # Bulk variant: tap a category of an opponent's permanents (Icebreaker
        # Kraken, Frozen Aether, Stasis-like effects). Supported scopes:
        #   target_player: which player's permanents
        #   types: "creatures+artifacts" (default for Icebreaker Kraken),
        #          "creatures", "artifacts", or "all"
        #   skip_next_untap: bool — set the _skip_next_untap flag for "they
        #                    don't untap during that player's next untap step"
        if action.get("scope") in ("bulk", "all_creatures", "all_artifacts", "creatures_and_artifacts"):
            target_player_name = action.get("target_player") or action.get("player", "")
            tgt = find_player(target_player_name)
            if not tgt:
                return None
            scope = action.get("scope", "creatures_and_artifacts")
            skip_next = bool(action.get("skip_next_untap", False))
            # Aug 8 batch audit (#5): opt-in "no_tap" — Icebreaker Kraken's
            # printed ETB says ONLY "artifacts and creatures target opponent
            # controls don't untap during that player's next untap step";
            # this handler was force-tapping them on the spot (an illegal
            # extra effect the card never grants), and the message counted
            # only the newly-tapped under a "won't untap" label (displayed
            # 4 while 8 were affected). Sleep-class cards that print BOTH
            # "tap" and "don't untap" keep the default tapping behavior;
            # Cryptic Command mode 3 (tap-only) is the untouched control.
            no_tap = bool(action.get("no_tap", False))
            tapped_cards = []
            affected = []
            for c in tgt.battlefield:
                if c.is_land():
                    continue
                want = False
                if scope in ("bulk", "creatures_and_artifacts"):
                    want = c.is_creature() or c.is_artifact()
                elif scope == "all_creatures":
                    want = c.is_creature()
                elif scope == "all_artifacts":
                    want = c.is_artifact()
                if not want:
                    continue
                if not no_tap and not c.tapped:
                    c.tapped = True
                    tapped_cards.append(c.name)
                if skip_next:
                    c._skip_next_untap = True
                    affected.append(c.name)
            if not tapped_cards and not affected:
                return None
            if skip_next:
                # Honest count: EVERY permanent carrying the skip flag, not
                # just the ones newly tapped by this action.
                return (f"❄️ {len(affected)} of **{tgt.name}**'s permanents "
                        f"won't untap next turn")
            return f"❄️ {len(tapped_cards)} of **{tgt.name}**'s permanents tapped"
        result = find_card_on_battlefield(action.get("card", ""))
        if result:
            card, owner = result
            card.tapped = True
            if action.get("skip_next_untap"):
                card._skip_next_untap = True
                return f"❄️ Tapped **{card.name}** (won't untap next turn)"
            return f"↩️ Tapped **{card.name}**"
    
    elif action_type == "additional_combat":
        # Aug 1 deferred slate: Port Razer's connect / Karlach's attack
        # trigger grant an additional combat phase. The autoplay HUMAN turn
        # loop consumes game._additional_combats (the Moraug machinery);
        # the Claude path breadcrumbs + discards (documented gap), and
        # end_turn discards unconsumed phases so they can never leak into
        # the next player's turn.
        game._additional_combats = getattr(game, '_additional_combats', 0) + 1
        _ac_src = action.get("source", "")
        return (f"⚔️ {(_ac_src + ': ') if _ac_src else ''}an additional combat "
                f"phase follows this one (#{game._additional_combats} pending)")

    elif action_type == "untap":
        result = find_card_on_battlefield(action.get("card", ""))
        if result:
            card, owner = result
            card.tapped = False
            return f"↪️ Untapped **{card.name}**"

    # ---- ANIMATE LAND / ARTIFACT (Sylvan Awakening, Living Lands, Awaken,
    # Ensoul Artifact, Karn animate, Mishra's Self-Replicator) ----
    # Until end of turn (or permanently for Ensoul Artifact), target lands or
    # artifacts become creatures with given P/T while remaining their original
    # types. Implementation: stamp temporary attributes on the affected
    # permanent cards; EOT cleanup flushes the _animated_until_eot flag.
    # action_type=="animate_land" stays for backward compat; new permanent
    # types use "animate_permanent" with required_type="artifact" etc.
    elif action_type in ("animate_land", "animate_permanent"):
        player_name = action.get("player", "")
        target_player = find_player(player_name)
        if not target_player:
            return None
        power = int(action.get("power", 2))
        toughness = int(action.get("toughness", 2))
        scope = (action.get("scope") or "all").lower()  # "all" | "target"
        kw_str = action.get("keywords", "")  # comma-separated, e.g. "haste,trample"
        extra_keywords = [k.strip().lower() for k in kw_str.split(",") if k.strip()]
        # May 17 audit: type filter so the action can animate artifacts
        # (Ensoul Artifact: noncreature artifact → 5/5; Karn Liberated's
        # "noncreature artifact becomes 5/5 creature artifact" mode; Mishra's
        # Self-Replicator) without growing a second nearly-identical action.
        # Default 'land' preserves legacy behavior for Sylvan Awakening etc.
        required_type = (action.get("required_type") or "land").lower()
        def _matches_required_type(card) -> bool:
            if required_type == "land":
                return card.is_land()
            if required_type == "artifact":
                return 'artifact' in (card.type_line or '').lower()
            if required_type == "any":
                return True
            return card.is_land()
        affected = []
        if scope == "target":
            tgt_name = action.get("card", "")
            hit = find_card_on_battlefield(tgt_name)
            if hit:
                card, _ = hit
                if _matches_required_type(card):
                    affected.append(card)
        else:
            for land in target_player.battlefield:
                if _matches_required_type(land):
                    affected.append(land)
        if not affected:
            return None
        # Ensoul Artifact is "permanent" not "until end of turn" — let the
        # template signal that with action.get("permanent") = True so we
        # don't flag the EOT cleanup. Default stays EOT for safety.
        is_permanent_animation = bool(action.get("permanent_until_leaves", False))
        # Aug 1: "until your next turn" (Sylvan Awakening's printed
        # duration — the animated lands survive into the opponent's turn as
        # blockers and revert when their controller's next turn begins).
        # _animated_until_eot stays set alongside the expiry marker so
        # every effective-P/T and SBA-promotion read keeps working.
        until_your_next_turn = (action.get("duration") == "until_your_next_turn")
        _controller_idx = next(
            (i for i, p in enumerate(game.players) if p is target_player), 0)
        # Subtype to append depends on the source type. Lands → Elemental
        # (vanilla autoplay convention). Artifacts → Construct.
        appended_subtype = "Elemental" if required_type == "land" else "Construct"
        for land in affected:
            if not is_permanent_animation:
                land._animated_until_eot = True
                if until_your_next_turn:
                    land._animated_expires_at_turn_of = _controller_idx
            else:
                # Mark as permanently animated so EOT cleanup leaves it alone.
                land._animated_permanent = True
            # Aug 2 (crew/Chandra): Vehicles carry PRINTED P/T — "it becomes
            # an artifact creature" animations use them instead of a stamped
            # value ("use_printed_pt": true on the action).
            if action.get("use_printed_pt"):
                try:
                    land._animated_power = int(land.power or 0)
                    land._animated_toughness = int(land.toughness or 0)
                except (TypeError, ValueError):
                    land._animated_power = power
                    land._animated_toughness = toughness
            else:
                land._animated_power = power
                land._animated_toughness = toughness
            # Land/artifact becomes a creature in addition to its other types.
            # Preserve original types so end-of-turn cleanup can restore them.
            if 'Creature' not in (land.type_line or ''):
                land._original_type_line = land.type_line
                fallback_base = 'Land' if required_type == 'land' else 'Artifact'
                land.type_line = f"{land.type_line or fallback_base} Creature — {appended_subtype}"
            if extra_keywords:
                land._animated_keywords = list(extra_keywords)
                for kw in extra_keywords:
                    if kw not in [k.lower() for k in (land.keywords or [])]:
                        land.keywords = list(land.keywords or []) + [kw]
        _dur_text = ("until your next turn" if until_your_next_turn
                     else "until end of turn")
        return f"🌳 {len(affected)} land(s) become {power}/{toughness} creatures {_dur_text}"

    # ---- TRANSFORM ----
    elif action_type == "transform":
        result = find_card_on_battlefield(action.get("card", ""))
        if result:
            card, owner = result
            if card.has_transform:
                old_name = card.name
                card.transform()
                return f"🔄 **{old_name}** transforms into **{card.name}**"
            else:
                return f"⚠️ **{card.name}** cannot transform"

    # ---- UNTAP LANDS (Snap, Frantic Search, etc.) ----
    elif action_type == "untap_lands":
        player = find_player(action.get("player", ""))
        # exclude_lands + include_nonlands: Dramatic Reversal mode (untap all
        # nonland permanents). Default: untap N lands.
        # May 24 Tier-2 audit fix: `filter_supertype` lets Jorn-style "untap
        # each snow permanent" effects target just the matching subset. Set
        # via `"filter_supertype": "snow"` in the action dict.
        exclude_lands = bool(action.get("exclude_lands", False))
        include_nonlands = bool(action.get("include_nonlands", False))
        filter_supertype = (action.get("filter_supertype") or "").lower()
        def _supertype_match(c):
            if not filter_supertype:
                return True
            tl = (getattr(c, 'type_line', '') or '').lower()
            return filter_supertype in tl
        if player:
            if exclude_lands and include_nonlands:
                # Dramatic Reversal: untap all nonland permanents you control
                untapped = 0
                untapped_names = []
                for c in player.battlefield:
                    if c.is_land():
                        continue
                    if not _supertype_match(c):
                        continue
                    if c.tapped:
                        c.tapped = False
                        untapped += 1
                        untapped_names.append(c.name)
                if untapped > 0:
                    preview = ', '.join(untapped_names[:5])
                    more = f" (+{untapped - 5} more)" if untapped > 5 else ""
                    return f"↪️ {player.name} untaps {untapped} nonland permanent(s): {preview}{more}"
                return None
            count = int(action.get("count", 2))
            untapped = 0
            for land in player.battlefield:
                if untapped >= count:
                    break
                if not _supertype_match(land):
                    continue
                if land.is_land() and land.tapped:
                    land.tapped = False
                    untapped += 1
                elif include_nonlands and not land.is_land() and land.tapped:
                    land.tapped = False
                    untapped += 1
            if untapped > 0:
                qualifier = f"{filter_supertype} " if filter_supertype else ""
                return f"↪️ {player.name} untaps {untapped} {qualifier}permanent(s)"
            else:
                return None  # No tapped lands to untap (e.g., Panharmonicon double with <10 lands)

    # ---- ADD MANA ----
    elif action_type == "add_mana":
        player = find_player(action.get("player", ""))
        if player:
            color = action.get("color", "C").upper()
            amount = int(action.get("amount", 1))
            if color in player.mana_pool:
                # Q4: NON-snow unless the emitting source resolves to a
                # snow permanent on the player's battlefield (an action
                # 'source' key, else the resolution-source name). A
                # source we can't resolve stays untagged — undercount,
                # the safe direction.
                _src_ctx = getattr(game, '_current_resolution_source', None)
                _src_name = action.get("source") or action.get(
                    "_source_card_name") or (
                    _src_ctx[0] if isinstance(_src_ctx, tuple) and _src_ctx
                    else None)
                _src_card = None
                if _src_name:
                    _want = str(_src_name).strip().lower()
                    _src_card = next(
                        (c for c in player.battlefield
                         if (c.name or '').strip().lower() == _want), None)
                player.grant_pool_mana(color, amount, source=_src_card)
            if action.get("retains_through_turn"):
                player._retain_mana_through_turn = game.turn_number

            return f"💎 **{player.name}** adds {{{color}}} x{amount}"
    
    # ---- COUNTER SPELL (from counterspell templates) ----
    elif action_type == "counter_spell":
        # Mark a spell on the stack as countered
        target_spell_name = action.get("target_name")  # Optional: specific target
        max_mv = action.get("max_mv")  # Optional: only counter if MV ≤ this (Mesmeric Glare)
        # Guard: if the stack is empty, the counter has no target — log and bail out
        # (The plan validator blocks this before execution, but Tier 3 / !resolve can
        # still reach here with an empty stack.)
        if not game.stack:
            print(f"[COUNTER-FIZZLE] Stack is empty, counter has no target")
            return "🚫 Counter effect fizzles — no spell on the stack to target"
        if game.stack:
            # Find the target spell (specific name or top of stack)
            target = None
            if target_spell_name:
                for entry in reversed(game.stack):
                    entry_name = getattr(getattr(entry, 'card', None), 'name', None) or (
                        entry.get('card_name') if isinstance(entry, dict) else None)
                    if entry_name and entry_name.lower() == target_spell_name.lower():
                        if not getattr(entry, 'countered', False):
                            target = entry
                            break
            if not target:
                # Fall back to top of stack (backward compatible)
                target = game.stack[-1]
                if getattr(target, 'countered', False):
                    return f"🚫 Counter fizzles — target spell already countered"
            # Enforce the counter's printed target restriction at the central
            # mutation point. Templates and Tier 3 both funnel through here.
            tcard = getattr(target, 'card', None)
            target_types = (getattr(tcard, 'type_line', '') or '').lower()
            source_oracle = (action.get('_source_oracle') or '').lower()
            source_name = (action.get('_source_card_name') or '').lower()
            restriction_failed = None
            if 'counter target noncreature spell' in source_oracle and 'creature' in target_types:
                restriction_failed = 'target is a creature spell'
            elif 'counter target instant spell' in source_oracle and 'instant' not in target_types:
                restriction_failed = 'target is not an instant spell'
            elif 'counter target instant or sorcery spell' in source_oracle and not any(
                    kind in target_types for kind in ('instant', 'sorcery')):
                restriction_failed = 'target is not an instant or sorcery spell'
            elif 'counter target creature spell' in source_oracle and 'creature' not in target_types:
                restriction_failed = 'target is not a creature spell'
            elif 'counter target artifact or enchantment spell' in source_oracle and not any(
                    kind in target_types for kind in ('artifact', 'enchantment')):
                restriction_failed = 'target is not an artifact or enchantment spell'
            elif source_name == 'swan song' and not any(
                    kind in target_types for kind in ('enchantment', 'instant', 'sorcery')):
                restriction_failed = 'target is not an enchantment, instant, or sorcery spell'
            elif source_name == 'wash away' and getattr(tcard, '_cast_origin', 'hand') == 'hand':
                restriction_failed = "target was cast from its owner's hand"
            if restriction_failed:
                # Aug 2 batch-14 follow-up: this message reached Discord all
                # along (9 fizzles across the batch) but had NO console tag,
                # so a console-based audit could not see it — and a reviewer
                # duly filed "Wash Away's fizzle is invisible" as a bug. It
                # wasn't: async stack resolution interleaves a response's
                # effect messages with the cast triggers ahead of it, so the
                # line landed six entries after the cast rather than next to
                # it. A greppable tag makes the correct behavior auditable.
                _src_dbg = (action.get('_source_card_name') or 'counter')
                print(f"[COUNTER-FIZZLE] {_src_dbg}: {restriction_failed}")
                return f"🚫 Counter fizzles — {restriction_failed}"
            # Apply max_mv filter (Mesmeric Glare: counter target spell with MV ≤ 3)
            if max_mv is not None and target is not None:
                tcard = getattr(target, 'card', None)
                try:
                    tcmc = int(tcard.cmc) if tcard and tcard.cmc else 0
                except (ValueError, TypeError):
                    tcmc = 0
                if tcmc > max_mv:
                    return f"🚫 Counter fizzles — target's mana value ({tcmc}) exceeds limit ({max_mv})"
            if hasattr(target, 'countered'):
                target.countered = True
                # June 11 audit: Delay must exile-with-suspend and Commit must
                # put the spell in its owner's library — both were dumping to
                # graveyard, enabling recursion that shaped a game's endgame
                # (Bloodghast landfall-returned two turns after a Delay).
                # The cast pipeline reads this when moving the countered card.
                if action.get("countered_to"):
                    target.countered_to = action.get("countered_to")
                t_name = target.card.name if hasattr(target, 'card') and target.card else "spell"
                # July 29: Baral-class "counters a spell" triggers.
                from mtg.triggers import fire_counters_a_spell_triggers
                _loot = fire_counters_a_spell_triggers(
                    game, action.get("_source_controller"))
                _base = f"🚫 **{t_name}** on the stack is countered!"
                return _base + ("\n" + "\n".join(_loot) if _loot else "")
            else:
                # Dict-style stack entry (legacy)
                top_name = target.get('card_name', 'spell') if isinstance(target, dict) else str(target)
                return f"🚫 **{top_name}** would be countered (stack not fully integrated)"
        else:
            return f"🚫 Counter effect fizzles — no spell on the stack to target"

    # ---- COUNTER UNLESS PAYS (Mana Leak, Daze, Spell Pierce...) ----
    # CR 601 "unless its controller pays {N}": offer the payment. Autoplay
    # always pays when able — no rational player declines. June 11 audit:
    # these resolved as hard counters with no payment window (Mana Leak
    # countered a spell whose controller had the {3} floating).
    elif action_type == "counter_unless_pays":
        if not game.stack:
            print(f"[COUNTER-FIZZLE] Stack is empty, counter has no target")
            return "🚫 Counter effect fizzles — no spell on the stack to target"
        # July 24 batch-6 audit (reviewer S2): honor the DECLARED target like
        # counter_spell does. A stale Spell Pierce force-resolved via the LIFO
        # cap acted on game.stack[-1] — an unrelated spell with no
        # unless-clause. If the declared target left the stack, fizzle
        # (CR 608.2b) instead of grabbing whatever is on top.
        target = None
        _cu_target_name = action.get("target_name")
        if _cu_target_name:
            for entry in reversed(game.stack):
                entry_name = getattr(getattr(entry, 'card', None), 'name', None)
                if (entry_name and entry_name.lower() == _cu_target_name.lower()
                        and not getattr(entry, 'countered', False)):
                    target = entry
                    break
            if target is None:
                return (f"💨 Counter fizzles — **{_cu_target_name}** is no "
                        f"longer on the stack")
        if target is None:
            target = game.stack[-1]
        if getattr(target, 'countered', False):
            return f"🚫 Counter fizzles — target spell already countered"
        pay_cost = action.get("cost", "")
        t_name = target.card.name if hasattr(target, 'card') and target.card else "spell"
        # Find the countered spell's controller
        t_ctrl = None
        t_ctrl_name = getattr(target, 'controller_name', None)
        if t_ctrl_name:
            t_ctrl = find_player(t_ctrl_name)
        if t_ctrl is not None and pay_cost:
            paid = False
            try:
                paid = t_ctrl.tap_sources_for_cost(pay_cost, game=game)
            except (ValueError, KeyError, AttributeError, TypeError) as e:
                print(f"[COUNTER-UNLESS] payment attempt errored: {e}")
            if paid:
                rules.log_event(f"{t_ctrl.name} pays {pay_cost}; {t_name} is not countered")
                return f"💰 **{t_ctrl.name}** pays {pay_cost} — **{t_name}** is not countered"
        if hasattr(target, 'countered'):
            target.countered = True
            rules.log_event(f"{t_name} countered (cost {pay_cost} not paid)")
            from mtg.triggers import fire_counters_a_spell_triggers
            _loot = fire_counters_a_spell_triggers(
                game, action.get("_source_controller"))
            _base = f"🚫 **{t_name}** is countered! (controller couldn't pay {pay_cost})"
            return _base + ("\n" + "\n".join(_loot) if _loot else "")
        return None

    # ---- COUNTER ABILITY (from Stifle/Trickbind templates) ----
    elif action_type == "counter_ability":
        # Find the topmost non-spell entry on the stack (triggered/activated ability)
        if game.stack:
            # Look for topmost ability (is_spell=False) on the stack
            target_entry = None
            for entry in reversed(game.stack):
                if hasattr(entry, 'is_spell') and not entry.is_spell:
                    target_entry = entry
                    break
            if target_entry:
                target_entry.countered = True
                source_name = getattr(target_entry, 'trigger_source', None)
                trigger_text = getattr(target_entry, 'trigger_text', '')
                ability_desc = source_name or trigger_text[:60] or "ability"
                return f"🚫 **{ability_desc}**'s triggered ability is countered!"
            else:
                # No ability on stack — Voidslime/Disallow ("Counter target
                # spell, activated ability, or triggered ability") may counter
                # the spell instead; Tale's End only a LEGENDARY spell.
                # July 24 batch-6 audit (reviewer S2): this fallback was wired
                # to all five COUNTER_ABILITY_CARDS, so Stifle countered a
                # Scroll Rack SPELL (its text has no spell clause at all —
                # CR 601.2c). Gate on the source's own oracle text; stay
                # permissive only when the oracle is unknown (legacy/Tier-3
                # callers that don't thread _source_oracle).
                # Aug 9 audit (C-F2-1): strip REMINDER text before the test —
                # Trickbind's Split Second reminder contains 'spell', which
                # let this fallback counter a creature SPELL (the resolution
                # half of the same gap the cast gate had).
                from mtg.helpers import strip_reminder_text
                _ca_oracle = strip_reminder_text(
                    action.get('_source_oracle') or '').lower()
                if _ca_oracle and 'spell' not in _ca_oracle:
                    return (f"🚫 Counter ability fizzles — no triggered/"
                            f"activated abilities on the stack")
                top = game.stack[-1]
                if hasattr(top, 'countered'):
                    if 'legendary spell' in _ca_oracle:
                        _top_tl = (getattr(getattr(top, 'card', None),
                                           'type_line', '') or '').lower()
                        if 'legendary' not in _top_tl:
                            return (f"🚫 Counter ability fizzles — target "
                                    f"spell is not legendary")
                    top.countered = True
                    target_name = top.card.name if hasattr(top, 'card') and top.card else "spell"
                    from mtg.triggers import fire_counters_a_spell_triggers
                    _loot = fire_counters_a_spell_triggers(
                        game, action.get("_source_controller"))
                    _base = f"🚫 **{target_name}** is countered!"
                    return _base + ("\n" + "\n".join(_loot) if _loot else "")
                return f"🚫 Counter ability fizzles — no triggered/activated abilities on the stack"
        else:
            return f"🚫 Counter ability fizzles — stack is empty"

    # ---- WIN GAME (Thassa's Oracle, Jace, etc.) ----
    # ---- ETALI TRIGGER (exile top card of each library, cast nonland free) ----
    elif action_type == "etali_trigger":
        etali_player_name = action.get("player", "")
        etali_player = find_player(etali_player_name)
        if etali_player:
            results = []
            for p in game.players:
                if not p.library:
                    results.append(f"{p.name}: empty library")
                    continue
                exiled = p.library.pop(0)
                # Track the exile (simplified — just reveal)
                if exiled.is_land():
                    results.append(f"🃏 {p.name}'s top card: **{exiled.name}** (land — stays exiled)")
                    # Put lands into exile (simplified: bottom of library)
                    p.library.append(exiled)
                else:
                    # July 24 (slice 4b groundwork): Etali's free casts ARE
                    # casts (CR 601) — battlefield cast triggers never fired
                    # for them (sync-gap class). Queue + emit (parity-paired
                    # inside the helper).
                    _er = getattr(rules, 'engine_ref', None)
                    if _er is not None:
                        from mtg.triggers import queue_cast_triggers_sync
                        queue_cast_triggers_sync(_er, game, etali_player,
                                                 exiled, via="etali_free_cast")
                    # Cast for free — put onto battlefield if permanent, or resolve if spell
                    if exiled.is_creature() or exiled.is_artifact() or exiled.is_enchantment() or exiled.is_planeswalker():
                        etali_player.battlefield.append(exiled)
                        exiled.controller = etali_player.name
                        exiled.tapped = False
                        exiled.summoning_sick = True
                        results.append(f"🌀 Etali exiles **{exiled.name}** from {p.name}'s library → battlefield!")
                        print(f"[ETALI] Cast {exiled.name} from {p.name}'s library for free → {etali_player.name}'s battlefield")
                    else:
                        # Instant/sorcery — resolve effect then graveyard
                        results.append(f"🌀 Etali exiles **{exiled.name}** from {p.name}'s library (cast for free)")
                        etali_player.graveyard.append(exiled)
                        print(f"[ETALI] Cast {exiled.name} (instant/sorcery) from {p.name}'s library → graveyard")
            return "\n".join(results) if results else "Etali trigger: no cards exiled"
        return "Etali trigger: controller not found"

    elif action_type == "win_game":
        winner_name = action.get("player", "")
        reason = action.get("reason", "win condition met")
        winner = find_player(winner_name)
        if winner:
            game.ended = True
            # game.winner is an INDEX everywhere else (every consumer does
            # game.players[game.winner]) — storing the Player object here
            # crashed the autoplay summary the first time an alternate win
            # condition fired live (Hellkite Tyrant's 20-artifact upkeep win,
            # batch 15332).
            game.winner = game.players.index(winner)
            print(f"[WIN_GAME] {winner.name} wins! Reason: {reason}")
            return f"🏆 **{winner.name} wins the game!** ({reason})"
        return f"🏆 Win condition triggered but player '{winner_name}' not found"

    # ---- PUMP ALL CREATURES (Craterhoof, Overrun, etc.) ----
    elif action_type == "pump_all_creatures":
        pump_player_name = action.get("player", "")
        pump_power = action.get("power", 0)
        pump_toughness = action.get("toughness", 0)
        pump_keywords = action.get("keywords", [])
        min_power = action.get("min_power", 0)  # Filter: only creatures with power >= this
        exclude_types = action.get("exclude_types", [])  # Filter: exclude creature types
        # May 20 audit: support include-by-subtype filter for Goblin Rabblemaster
        # (and similar attack-triggered anthems). When `subtype` is set, only
        # pump creatures whose type_line contains that subtype. `exclude_name`
        # skips a specific permanent (the trigger source itself).
        include_subtype = action.get("subtype") or action.get("include_subtype") or ""
        exclude_name = action.get("exclude", "") or action.get("exclude_name", "")
        include_name = action.get("card", "") or action.get("include_name", "")
        include_id = action.get("card_id", "") or action.get("include_id", "")
        # Aug 2 2026: battle cry (CR 702.92) pumps each OTHER ATTACKING
        # creature, so it needs both an attacking-only filter and an
        # id-exclude for the source itself (exclude_name would also hit
        # a second copy of the same card, which battle cry must pump).
        only_attacking = bool(action.get("only_attacking"))
        exclude_id = action.get("exclude_id", "")
        # June 10 audit (V23): support symmetric effects ("All creatures get
        # -X/-X" — Toxic Deluge). The template now emits player="all"; the old
        # single-player handler received "" → find_player(None) → silent no-op,
        # so the -X/-X never applied while the life was still paid.
        if (pump_player_name or '').lower() in ('all', 'each', 'both', 'everyone'):
            pump_targets = list(game.players)
        else:
            _pp = find_player(pump_player_name)
            pump_targets = [_pp] if _pp else []
        pumped = []
        for pump_player in pump_targets:
            for c in pump_player.battlefield:
                if not c.is_creature():
                    continue
                # Subtype-include filter (Goblin Rabblemaster: other Goblins only)
                if include_subtype:
                    type_line = (c.type_line or '').lower()
                    if include_subtype.lower() not in type_line:
                        continue
                if include_id and c.id != include_id:
                    continue
                if exclude_id and c.id == exclude_id:
                    continue
                if only_attacking and not getattr(c, "attacking", False):
                    continue
                if include_name and c.name.lower() != include_name.lower():
                    continue
                # Name-exclude filter (Goblin Rabblemaster: not itself)
                if exclude_name and c.name.lower() == exclude_name.lower():
                    continue
                # Power filter (Return of the Wildspeaker: power 4+)
                if min_power > 0:
                    effective_power = c.get_effective_power(game) if hasattr(c, 'get_effective_power') else 0
                    if effective_power < min_power:
                        continue
                # Type exclusion filter (non-Human, etc.)
                if exclude_types:
                    type_line = (c.type_line or '').lower()
                    if any(t.lower() in type_line for t in exclude_types):
                        continue
                if True:  # was: if c.is_creature()
                    # Only modify power_modifier directly if layers engine is NOT available.
                    # If layers engine IS active, it handles P/T via Layer 7c effects —
                    # modifying power_modifier AND registering a layer effect causes double-pump
                    # that accumulates on each recalculation (Bug: delta=-1381/-1381 on tokens).
                    if not (HAS_LAYERS_ENGINE and game.layers_engine):
                        c.power_modifier = getattr(c, 'power_modifier', 0) + pump_power
                        c.toughness_modifier = getattr(c, 'toughness_modifier', 0) + pump_toughness
                    # [LAYERS] Register pump as Layer 7c temporary effect
                    if HAS_LAYERS_ENGINE and game.layers_engine:
                        _pump_effs = create_pump_effect(
                            source_name=action.get("source", "pump"),
                            source_id=f"pump_{c.id}_{id(action)}",
                            controller=pump_player.name, target_id=c.id,
                            power_mod=pump_power, toughness_mod=pump_toughness,
                            keywords=pump_keywords if pump_keywords else None,
                            duration="end_of_turn")
                        for _pe in _pump_effs:
                            game.layers_engine.add_effect(_pe)
                    for kw in pump_keywords:
                        if not c.has_keyword(kw):
                            # June 11 audit: EOT pumps are temporary abilities,
                            # not static battlefield grants. Storing Craterhoof's
                            # trample in _granted_keywords let the next static
                            # recalculation erase it before combat.
                            c.temp_keywords.append(kw)
                    pumped.append(c.name)
        if pumped:
            kw_str = f" and {', '.join(pump_keywords)}" if pump_keywords else ""
            p_str = f"+{pump_power}" if pump_power >= 0 else str(pump_power)
            t_str = f"+{pump_toughness}" if pump_toughness >= 0 else str(pump_toughness)
            scope = "All" if len(pump_targets) > 1 else f"{pump_targets[0].name}'s"
            print(f"[PUMP] {scope} creatures get {p_str}/{t_str}{kw_str}: {pumped}")
            if len(pumped) == 1:
                # July 21 batch audit (R4-3): a single-creature pump phrased
                # as "<player>'s creatures get..." reads like a team anthem
                # (Inferno Titan's {R} self-pump, game_1529168842905882755).
                result_msg = (f"💪 **{pumped[0]}** gets {p_str}/{t_str}{kw_str} "
                              f"until end of turn")
            else:
                result_msg = (f"💪 {scope} creatures get {p_str}/{t_str}{kw_str} "
                              f"until end of turn ({len(pumped)} creatures)")
            # June 10 (V23): a negative toughness pump can be lethal — refresh
            # P/T and run SBA now (CR 704.5g) instead of waiting for the next
            # sweep (Toxic Deluge left 0/1s alive to block the same turn).
            if pump_toughness < 0:
                game.recalculate_power_toughness()
                sba_msgs = rules.process_state_based_actions(game)
                if sba_msgs:
                    result_msg += "\n" + "\n".join(sba_msgs)
            return result_msg
        # May 26 audit: this branch used to leak the raw scaffolding string
        # "Pump effect: no creatures found for <name>" (trailing space when the
        # player name was empty) to Discord — e.g. Toxic Deluge "creatures get
        # -X/-X" cast into an empty board. Return None like the other no-op
        # action paths (add_counters at affected==0) so nothing is emitted.
        return None

    # ---- PUT BACK FROM HAND (Brainstorm, etc.) ----
    elif action_type == "put_back_from_hand":
        pb_player_name = action.get("player", "")
        pb_count = action.get("count", 2)
        pb_player = find_player(pb_player_name)
        if pb_player and pb_player.hand:
            # [BRAINSTORM] Snapshot the live hand BEFORE selecting cards so
            # future audits can confirm we only moved cards that were
            # actually present. Select strictly from pb_player.hand.
            hand_snapshot = [c.name for c in pb_player.hand]
            print(f"[BRAINSTORM] Hand snapshot before put-back for {pb_player.name}: {hand_snapshot}")

            # May 20 audit (Bug A): the old heuristic `(is_land first, then
            # -cmc)` failed three ways in game_1506623255119925278:
            #   1. Lands sorted identically — Celestial Colonnade (manland,
            #      4/4 flying win condition) was bottomed like a Swamp.
            #   2. X-cost spells (Logic Knot, Walking Ballista) had cmc=0
            #      from Scryfall, treated as low-priority "keep" instead of
            #      "their cost adapts to the situation".
            #   3. No "is this my only threat?" check — Gurmag Angler (the
            #      deck's primary win condition, in hand) was put back on
            #      top of library where it was eventually exiled.
            # Score each card; higher score = MORE likely to bottom.
            lands_in_play = sum(1 for c in pb_player.battlefield if c.is_land())
            lands_in_hand = sum(1 for c in pb_player.hand if c.is_land())
            opp = next((p for p in game.players if p is not pb_player), None)
            opp_has_creature_board = bool(
                opp and any(c.is_creature() for c in opp.battlefield)
            )
            opp_has_any_threat = bool(
                opp and any(not c.is_land() for c in opp.battlefield)
            )

            # Identify cards we already have copies of on battlefield — having
            # a copy in play reduces the value of keeping another in hand.
            bf_names = {c.name.lower() for c in pb_player.battlefield}

            def _put_back_score(card):
                """Higher = better to BOTTOM. Lower = keep in hand."""
                oracle_l = (card.oracle_text or '').lower()
                score = 0
                # --- LAND HANDLING ---
                if card.is_land():
                    # Manland detection: lands with `: becomes a N/N creature`
                    is_manland = bool(re.search(
                        r'becomes a \d+/\d+\b|\bbecomes a .{0,30}creature\b',
                        oracle_l,
                    ))
                    if is_manland:
                        return -200  # Strong KEEP — manlands are win cons
                    # Lands with strategic activated abilities (Castle Vantress
                    # scry, Bojuka Bog grave-exile, fetchlands sacrifice)
                    has_strategic_ability = (
                        ':' in oracle_l
                        and any(kw in oracle_l for kw in (
                            'scry', 'search your library', 'exile target',
                            'destroy target', 'target creature gets',
                        ))
                    )
                    if has_strategic_ability:
                        return -50  # Mild keep
                    # Plain mana lands: bottom them when we have ENOUGH for the
                    # turn (lands_in_play + lands_in_hand > expected need).
                    # Vanilla land in a flooded hand → bottom; in a screwed
                    # hand → keep.
                    if lands_in_play + lands_in_hand > 4:
                        return 100  # Excess land — bottom this
                    return 30  # Need lands — slight bottom preference (don't fight if better cards exist)
                # --- NONLAND HANDLING ---
                cmc = card.cmc or 0
                # X-cost spells: treat their effective cost as 2-3 (variable),
                # not the literal 0 Scryfall returns. They scale with the game
                # state and should NOT be aggressively bottomed.
                if card.mana_cost and 'X' in card.mana_cost.upper():
                    cmc = max(cmc, 3)
                # Removal / interaction / counterspells — keep when opp has a board
                is_removal = (
                    'destroy target' in oracle_l
                    or 'exile target' in oracle_l and 'creature' in oracle_l
                    or 'counter target' in oracle_l
                    or ('deal' in oracle_l and 'damage to target' in oracle_l
                        and 'creature' in oracle_l)
                )
                if is_removal:
                    if opp_has_creature_board:
                        score -= 80  # KEEP — we need the answer now
                    else:
                        score += 20  # Dead card right now — bottom is okay
                # Win-condition creatures: CMC ≥ 4 creatures, or
                # creatures with "win the game" text, OR creatures we don't
                # already have a copy of on battlefield. Without copies of the
                # threat on battlefield, this MIGHT be our only win con.
                if card.is_creature() and cmc >= 4:
                    if card.name.lower() not in bf_names:
                        score -= 120  # Strong keep — likely main threat
                    else:
                        score += 5  # Have copies; this one is excess
                # "Win the game" text — never bottom
                if 'win the game' in oracle_l or 'wins the game' in oracle_l:
                    score -= 300
                # Card draw / cantrips — generally bottom-able (they're cheap
                # and the next draw is fresh).
                if cmc <= 1 and ('draw a card' in oracle_l or 'scry' in oracle_l):
                    score += 50
                # Default: higher CMC = more likely to bottom IF we don't
                # have the mana to cast it soon. But subtract from the keep
                # score for win-cons.
                # Simple proxy: higher CMC = more bottom-prone for SAME card
                # class (favors keeping cheap interaction over expensive bomb
                # we can't cast).
                untapped_lands = sum(
                    1 for c in pb_player.battlefield
                    if c.is_land() and not c.tapped
                )
                if cmc > untapped_lands + 2:
                    score += cmc * 4  # Way too expensive — bottom it
                else:
                    score -= cmc * 2  # Castable soonish — keep
                return score

            # Sort by score DESC (highest score = best bottom candidate).
            # Stable sort preserves original hand order for ties.
            candidates = sorted(pb_player.hand, key=_put_back_score, reverse=True)
            actual_count = min(pb_count, len(candidates))
            to_put_back = candidates[:actual_count]
            # Log the chosen cards WITH scores for audit traceability.
            scored = [(c.name, _put_back_score(c)) for c in candidates[:max(actual_count + 2, 4)]]
            print(f"[BRAINSTORM] Top {len(scored)} bottom-candidates (highest score = most bottom-prone): {scored}")
            for card in to_put_back:
                if card in pb_player.hand:  # Defensive: only remove if truly present
                    pb_player.hand.remove(card)
                    pb_player.library.insert(0, card)  # Top of library
                else:
                    print(f"[BRAINSTORM] WARN: {card.name} not in hand at removal time — skipping")
            names = [c.name for c in to_put_back]
            print(f"[BRAINSTORM] {pb_player.name} puts {names} on top of library")
            return f"📚 {pb_player.name} puts {len(to_put_back)} card(s) on top of library"
        return f"Put back: {pb_player_name} has no cards in hand"

    # ---- BECOME COPY (Spark Double, Clone, Clever Impersonator, Phyrexian Metamorph, etc.) ----
    elif action_type == "become_copy":
        copy_player_name = action.get("player", "")
        target_name = action.get("target", "")
        modifications = action.get("modifications", [])
        copy_player = find_player(copy_player_name)
        if copy_player and target_name:
            # Search ALL battlefields — supports non-creature permanents
            # (Clever Impersonator, Phyrexian Metamorph, etc.)
            target_card = None
            for sp in game.players:
                for c in sp.battlefield:
                    if c.name.lower() == target_name.lower():
                        target_card = c
                        break
                if target_card:
                    break
            if not target_card:
                # Try fuzzy match across all battlefields
                for sp in game.players:
                    for c in sp.battlefield:
                        if target_name.lower() in c.name.lower():
                            target_card = c
                            break
                    if target_card:
                        break
            if target_card:
                # Find the most recently entered permanent (not just creatures —
                # supports Clever Impersonator, Phyrexian Metamorph, etc.)
                copy_card = None
                for c in reversed(copy_player.battlefield):
                    if getattr(c, 'entered_this_turn', False):
                        copy_card = c
                        break
                if copy_card and copy_card != target_card:
                    original_name = copy_card.name

                    # May 20 audit: snapshot ALL printed properties so the copy
                    # effect can be reverted when the card leaves the battlefield.
                    # Per CR 706.10, copy effects only apply on the battlefield
                    # and on the stack — when the copy moves to hand/graveyard/
                    # exile/library, it must revert to its printed characteristics.
                    # Game game_1506202586036830232 had a Phantasmal Image clone
                    # of Korvold end up in Rick's hand as "Korvold" with full
                    # commander cost, because no revert step ever fired.
                    _snapshot_copy_source(copy_card)

                    # [LAYERS] Register Layer 1 copy effect so anthems /
                    # Humility / etc. interact correctly with copied stats.
                    if HAS_LAYERS_ENGINE and game.layers_engine:
                        try:
                            copy_data = {
                                'name': target_card.name,
                                'power': int(target_card.power) if target_card.power not in (None, '', '*') else None,
                                'toughness': int(target_card.toughness) if target_card.toughness not in (None, '', '*') else None,
                                'abilities': list(target_card.keywords) if target_card.keywords else [],
                            }
                            layer1_effect = create_copy_effect(
                                source_name=original_name,
                                source_id=copy_card.id,
                                controller=copy_player.name,
                                target_id=target_card.id,
                                copy_data=copy_data,
                            )
                            game.layers_engine.add_effect(layer1_effect)
                            print(f"[COPY-LAYER1] Registered Layer 1 copy: {original_name} -> {target_card.name}")
                        except Exception as e:
                            print(f"[COPY-LAYER1] Failed to register: {e}")

                    # Direct attribute copy (always — Layer 1 effect above
                    # handles interactions with other continuous effects)
                    copy_card.name = target_card.name
                    copy_card.power = target_card.power
                    copy_card.toughness = target_card.toughness
                    copy_card.type_line = target_card.type_line
                    copy_card.oracle_text = target_card.oracle_text
                    copy_card.mana_cost = target_card.mana_cost
                    copy_card.cmc = target_card.cmc
                    copy_card._is_copy = True
                    copy_card._copy_of = target_card.name
                    copy_card._original_name = original_name
                    # Apply modifications (e.g., Spark Double +1/+1 counter)
                    for mod in modifications:
                        if mod.get("action") == "add_counters":
                            ct = mod.get("counter_type", "+1/+1")
                            amt = mod.get("amount", 1)
                            copy_card.counters[ct] = copy_card.counters.get(ct, 0) + amt
                    print(f"[COPY] {original_name} becomes a copy of {target_card.name}")
                    mod_str = ""
                    if modifications:
                        mod_str = " with " + ", ".join(
                            f"{m.get('amount', 1)} {m.get('counter_type', '+1/+1')} counter(s)"
                            for m in modifications if m.get("action") == "add_counters"
                        )
                    return f"🪞 {original_name} enters as a copy of {target_card.name}{mod_str}"
                return f"🪞 Copy: no valid entering creature found"
            return f"🪞 Copy: target '{target_name}' not found on battlefield"
        return f"🪞 Copy: missing player or target"

    # ---- DESTROY ----
    elif action_type == "destroy":
        result = find_card_on_battlefield(action.get("card", ""), action.get("target_controller"))
        if result:
            card, owner = result
            # [TARGETING] Check hexproof/protection/shroud before destroying
            if HAS_TARGETING and action.get("_source_card_name") and action.get("_source_controller"):
                legal, reason = _validate_target_for_action(game, card, owner, action["_source_card_name"], action["_source_controller"])
                if not legal:
                    print(f"[TARGETING] Destroy blocked: {card.name} — {reason}")
                    return f"🛡️ **{card.name}** can't be targeted ({reason})"
            if card.has_keyword("Indestructible", game=game):
                return f"🛡️ **{card.name}** is indestructible!"
            # June 10 audit (V13): honor destroy-replacement saves on the
            # single-target path too (CR 614.6 / 702.154) — the May 30 save
            # chain went into destroy_all_creatures only, while this path
            # moved the card straight to the graveyard (and the dies-template
            # text claimed "handled by the SBA engine", which was false here).
            # Order mirrors the SBA path: shield → totem (Umbra) armor →
            # destroy, then undying/persist return below.
            if card.is_creature() and card.counters.get('shield', 0) > 0:
                card.counters['shield'] -= 1
                print(f"[SHIELD-COUNTER] {card.name}: shield removed instead of destroyed")
                return f"🛡️ **{card.name}**'s shield counter is removed instead!"
            if card.is_creature() and rules._has_totem_armor(card, owner):
                _aura = rules._remove_totem_armor(card, owner, game)
                print(f"[TOTEM-ARMOR] {card.name}: {_aura.name if _aura else '?'} destroyed instead")
                return (f"🛡️ **{_aura.name if _aura else 'Umbra armor'}** is destroyed "
                        f"instead of **{card.name}** (umbra armor)!")
            # [LTB] Check for leaves-the-battlefield triggers BEFORE removal
            ltb_msgs = []
            if hasattr(rules, 'engine_ref') and rules.engine_ref:
                ltb_msgs = rules.engine_ref._check_ltb_triggers_sync(game, card, owner, "graveyard")
            # [LAYERS] Unregister static effects before removal
            game.unregister_static_effects(card)
            owner.battlefield.remove(card)
            # May 20 audit: clones revert printed characteristics when leaving
            # battlefield (CR 706.10).
            _revert_copy_if_leaving_battlefield(card)
            # July 21 batch audit (R3-1, CR 903.9a): the single-target destroy
            # action was the one death path the June 10 C7 sweep missed —
            # Kambal destroyed by Fleshbag Marauder's edict template sat in
            # the graveyard for 10 turns until a Bojuka Bog exile finally
            # bounced him to the command zone (game_1529168824723570750).
            # Dies triggers below still fire (CR 903.9b — they see the death
            # regardless of the redirect).
            _cmd_redirected = False
            from mtg.helpers import commander_declines_graveyard_redirect
            if (getattr(card, 'is_commander', False)
                    and getattr(game, 'format', '') in ('commander', 'edh', 'brawl', 'oathbreaker')
                    and commander_declines_graveyard_redirect(card)):
                print(f"  [CR-903.9] {card.name} declines the command-zone "
                      f"redirect (escape available) — stays in graveyard")
            elif (getattr(card, 'is_commander', False)
                    and getattr(game, 'format', '') in ('commander', 'edh', 'brawl', 'oathbreaker')):
                from mtg.helpers import command_zone_owner
                _zone_owner = command_zone_owner(game, card, owner)
                if not hasattr(_zone_owner, 'command_zone'):
                    _zone_owner.command_zone = []
                _zone_owner.command_zone.append(card)
                _cmd_redirected = True
                print(f"  [CR-903.9] Commander {card.name} redirected from "
                      f"graveyard → command zone (owner={_zone_owner.name})")
            else:
                # CR 404.3: the OWNER's graveyard, not the controller's. The
                # local is named `owner` but find_card_on_battlefield returns
                # whichever player's battlefield held the card — the July 27
                # fanout repro'd a stolen Sun Titan (owner_index=Rick) dying
                # under Claude's control and landing in CLAUDE's graveyard,
                # permanently changing hands. The commander branch above
                # already resolved ownership properly; this one didn't.
                from mtg.helpers import owner_of, unearthed_leaves_to_exile
                _dest_owner = owner_of(game, card, owner)
                # Unearth (CR 702.83a) — see the SBA and sacrifice twins.
                if unearthed_leaves_to_exile(card):
                    _dest_owner.exile.append(card)
                    print(f"[UNEARTH] {card.name} destroyed → exiled "
                          f"(CR 702.83a)")
                else:
                    _dest_owner.graveyard.append(card)
            game.recalculate_granted_keywords()
            game.recalculate_power_toughness()
            rules.log_event(f"{card.name} destroyed")
            is_token = bool(getattr(card, 'is_token', False))
            # [DIES-TRIGGER] Aug 2 (B2, the bus unification): single-target
            # destroy now QUEUES the death via the CREATURE_DIED choke point
            # like every other death path (wipes, SBA, sacrifice) instead of
            # resolving dies triggers inline — this was the last death path
            # invisible to the bus. The dispatcher drains at the next
            # check_state_based_actions (the same deferred semantics the
            # July-24 FP-ledger entry documents for wipes), and its drain
            # pipeline subsumes the old inline scan + July-21 unhandled-tail
            # queue. The subscriber grants Meren/Ezuri experience — the
            # batch-13 direct grant call is gone (queueing + calling would
            # double-grant, and the ==1 pin catches exactly that).
            if card.is_creature():
                _queue_deaths_3a(game, [(card, owner)])
            # June 10 audit (V13): undying / persist return (CR 702.92/702.77).
            # The creature DID die (dies triggers above are correct); it
            # returns as a new object with the appropriate counter.
            save_msgs = []
            # A CZ-redirected commander never reached the graveyard, so the
            # undying/persist graveyard-return below can't apply (it would
            # ValueError on graveyard.remove anyway).
            if card.is_creature() and not is_token and not _cmd_redirected:
                _ret_label = None
                if ((card.has_keyword('Undying') or rules._permanent_grants_undying(game, card, owner))
                        and card.counters.get('+1/+1', 0) == 0):
                    _ret_label = 'UNDYING'
                    _ret_counter = '+1/+1'
                elif card.has_keyword('Persist') and card.counters.get('-1/-1', 0) == 0:
                    _ret_label = 'PERSIST'
                    _ret_counter = '-1/-1'
                if _ret_label:
                    owner.graveyard.remove(card)
                    card.damage_marked = 0
                    card.deathtouch_damage = 0
                    card.summoning_sick = True
                    card.counters[_ret_counter] = card.counters.get(_ret_counter, 0) + 1
                    owner.battlefield.append(card)
                    print(f"[{_ret_label}] {card.name} returned to battlefield with "
                          f"{_ret_counter} counter (single-target destroy)")
                    save_msgs.append(f"♻️ {card.name} returns with {_ret_label.lower()} ({_ret_counter} counter)!")
                    # Re-register statics (they were unregistered above) and run
                    # the new-object cleanup (combat state, enters-tapped, self-ETB).
                    game.register_static_keyword_grants(card, owner.name)
                    game.register_static_pt_effects(card, owner.name)
                    from mtg.sba import _finalize_death_save_return
                    save_msgs.extend(_finalize_death_save_return(rules, game, owner, card, _ret_label))
                    game.recalculate_granted_keywords()
                    game.recalculate_power_toughness()
            # Tokens do enter the graveyard and die, so dies/LTB triggers above
            # must still see the event. They then cease to exist at the next SBA
            # (CR 704.5d). This direct action path is not guaranteed to run a
            # second SBA immediately, so perform that cleanup here and say what
            # happened instead of leaving a token card in the graveyard while
            # reporting only "destroyed" (June 11 smaller queue, Golem token).
            if is_token:
                if card in owner.graveyard:
                    owner.graveyard.remove(card)
                msg = f"💨 Token **{card.name}** is destroyed, then ceases to exist"
                print(f"[TOKEN-SBA] {card.name} destroyed and ceased to exist")
            elif _cmd_redirected:
                msg = (f"💀 **{card.name}** destroyed "
                       f"(👑 returns to the command zone, CR 903.9)")
            else:
                msg = f"💀 **{card.name}** destroyed"
            if ltb_msgs:
                msg += "\n" + "\n".join(ltb_msgs)
            if save_msgs:
                msg += "\n" + "\n".join(save_msgs)
            return msg

    elif action_type == "mill":
        # Mill N cards from a player's library to graveyard
        player_name = action.get("player")
        amount = action.get("amount", 1)
        p = find_player(player_name)
        if p:
            milled = []
            for _ in range(amount):
                if p.library:
                    milled_card = p.library.pop(0)
                    p.graveyard.append(milled_card)
                    milled.append(milled_card.name)
            if milled:
                # May 7 audit fix #3: card names like "Yawgmoth, Thran Physician"
                # contain commas — using ", " as the delimiter makes a 4-card
                # list look like 5 cards when one card has a comma in its name.
                # Use " · " (middle dot) so commas in names don't fragment the
                # count. Mill count uses len(milled) which is always correct
                # (already counts ACTUAL cards moved, not requested amount).
                return f"🪦 {p.name} mills {len(milled)}: {' · '.join(milled)}"
        return None

    elif action_type == "exile_graveyard":
        # Exile all cards from a player's graveyard (Ashiok, Rest in Peace, etc.)
        player_name = action.get("player")
        p = find_player(player_name)
        if p and p.graveyard:
            count = len(p.graveyard)
            exiled_names = [c.name for c in p.graveyard]
            if not hasattr(game, 'exile'):
                game.exile = []
            game.exile.extend(p.graveyard)
            p.graveyard = []
            # May 23 audit (MINOR #27): previously truncated at 5 names with
            # "..." for the rest (game_1507600803190538301:241 dropped 6 of 11
            # card names from a Living Death-style exile). Use the same
            # full-names-with-newlines pattern Living Death uses for relevance.
            if count > 5:
                listed = ', '.join(exiled_names)
                msg = f"⚫ {p.name}'s graveyard exiled ({count} cards):\n  {listed}"
                # Defensive 2000-char clamp so we don't blow past Discord's limit
                # in pathological cases (50+ card graveyards).
                if len(msg) > 1800:
                    msg = msg[:1800] + " …"
                return msg
            return f"⚫ {p.name}'s graveyard exiled ({count} cards: {', '.join(exiled_names)})"
        return None

    elif action_type == "phase_out_all":
        # Phase out all permanents a player controls (Teferi's Protection)
        # Phasing is NOT exile — permanents return at next untap step
        player_name = action.get("player")
        p = find_player(player_name)
        if not p:
            # Fallback to active player (Teferi's Protection is always cast by
            # its controller; if name lookup failed, use who we know is casting)
            p = game.active_player if hasattr(game, 'active_player') else None
            if p:
                print(f"[PHASE-OUT] find_player('{player_name}') failed — falling back to active player {p.name}")
        if p:
            phased = []
            for c in list(p.battlefield):
                # Skip already phased-out permanents (don't double-toggle)
                if getattr(c, '_phased_out', False):
                    continue
                c._phased_out = True
                # Aug 7 confirmation-batch audit (A-3, MAJOR): a permanent
                # that phases out is removed from combat (CR 506.4/702.26e).
                # Teferi's Protection after attackers were declared left the
                # phased-out attackers in game.attackers and a block was
                # declared against a nonexistent Soulherder
                # (game_1535212572960227388; damage was independently
                # skipped, so the leak was display/blocks only — this
                # closes it at the rule's own boundary).
                from mtg.helpers import strip_combat_state
                strip_combat_state(game, c)
                phased.append(c.name)
            # Don't remove from battlefield — just mark as phased out
            # They'll phase back in at the untap step
            if not hasattr(game, '_phased_out_permanents'):
                game._phased_out_permanents = {}
            game._phased_out_permanents[p.name] = [c for c in p.battlefield if getattr(c, '_phased_out', False)]
            # Always return a message so the effect is visible in Discord even
            # if no permanents were on the battlefield (effect still resolves).
            if phased:
                print(f"[PHASE-OUT] {p.name}: phased out {len(phased)} permanents: {phased}")
                return f"🌀 {p.name}'s permanents phase out ({len(phased)} permanents) — they return at next untap"
            else:
                print(f"[PHASE-OUT] {p.name}: no permanents to phase out (battlefield empty or all already phased)")
                return f"🌀 {p.name}'s permanents phase out (0 permanents on battlefield)"
        print(f"[PHASE-OUT] No player resolved from '{player_name}' — effect fizzles")
        return None

    elif action_type == "prevent_all_damage":
        # Prevent all damage to a player until their next turn (Teferi's Protection)
        # Uses the replacement engine for proper interaction with other effects
        # (Furnace of Rath, Dictate of Twin Gods, etc.)
        player_name = action.get("player")
        p = find_player(player_name)
        if p:
            reason = action.get("reason", "damage prevention")
            # Register a replacement effect that prevents all damage to this player
            if HAS_REPLACEMENT_ENGINE and game._replacement_engine is not None:
                # ReplacementEffect and EventType imported at module level (line 140)
                effect_id = f"prevent_all_damage_{player_name}_{game.turn_number}"
                effect = ReplacementEffect(
                    id=effect_id,
                    source_name="Teferi's Protection",
                    source_id=effect_id,
                    controller=player_name,
                    replaces_event=EventType.DAMAGE,
                    condition=lambda e, pn=player_name: e.affected_player == pn,
                    condition_text=f"prevent all damage to {player_name}",
                    replacement_type="prevent_all_damage",
                    prevents=True,
                )
                game._replacement_engine.add_effect(effect)
                # Track the effect ID so we can remove it at untap
                if not hasattr(p, '_temp_replacement_effect_ids'):
                    p._temp_replacement_effect_ids = []
                p._temp_replacement_effect_ids.append(effect_id)
                print(f"[REPLACEMENT] Registered prevent_all_damage for {player_name}")
            # Also set the flag as belt-and-suspenders (replacement engine may
            # not catch all damage paths, e.g., noncombat damage via lose_life)
            p._damage_prevented = True
            # Expires at caster's next untap (1 full turn cycle in multiplayer)
            p._damage_prevented_expires_turn = game.turn_number + len(game.players)
            # Teferi's Protection also locks the life total so non-damage
            # life-change effects (Kambal triggers, Sulfuric Vortex, Exquisite
            # Blood, etc.) can't mutate life while the effect is active.
            if action.get("lock_life_total", False):
                p._life_total_locked = True
                p._life_total_locked_expires_turn = p._damage_prevented_expires_turn
                print(f"[REPLACEMENT] life_total_locked for {player_name} until turn {p._life_total_locked_expires_turn}")
            print(f"[REPLACEMENT] prevent_all_damage expires at turn {p._damage_prevented_expires_turn}")
            return f"🛡️ {p.name}: {reason}"
        return None

    elif action_type == "reanimate":
        # Return a card from any graveyard to controller's battlefield
        player_name = action.get("player")
        card_name = action.get("card", "")
        allow_types = action.get("allow_types", ["creature"])  # Default creature-only; Daretti passes ["artifact"]
        max_cmc = action.get("max_cmc")
        if max_cmc is not None:
            try:
                max_cmc = int(max_cmc)
            except (TypeError, ValueError):
                max_cmc = 0
        # June 10 audit (V7): "your graveyard"-restricted reanimation. Dread
        # Return ("Return target creature card from YOUR graveyard") was
        # taking the opponent's best creature because the search below always
        # spanned every graveyard. own_graveyard=True (or from_player=<name>)
        # restricts the pool; default stays any-graveyard (Animate Dead /
        # Reanimate legitimately reach across).
        own_only = bool(action.get("own_graveyard") or action.get("own_graveyard_only"))
        from_player_name = action.get("from_player")
        p = find_player(player_name)
        if p:
            search_pool = list(game.players)
            if own_only:
                search_pool = [p]
            elif from_player_name:
                _fp = find_player(from_player_name)
                if _fp:
                    search_pool = [_fp]
            # If no specific card named, find best matching card in own graveyard
            if not card_name and p.graveyard:
                for c in sorted(p.graveyard, key=lambda c: int(c.cmc) if c.cmc else 0, reverse=True):
                    type_lower = (c.type_line or "").lower()
                    if (any(t in type_lower for t in allow_types)
                            and (max_cmc is None or int(c.cmc or 0) <= max_cmc)):
                        card_name = c.name
                        break
            if card_name:
                # Search the (possibly restricted) graveyard pool for the card
                for search_player in search_pool:
                    for c in list(search_player.graveyard):
                        if c.name.lower() == card_name.lower():
                            type_lower = (c.type_line or "").lower()
                            if not any(t in type_lower for t in allow_types):
                                print(f"[REANIMATE] Skipping {c.name} — not in allowed types {allow_types}")
                                return f"⚠️ Cannot reanimate {c.name} — not a valid type"
                            if max_cmc is not None and int(c.cmc or 0) > max_cmc:
                                print(f"[REANIMATE] Skipping {c.name} - MV exceeds {max_cmc}")
                                return f"Cannot reanimate {c.name} - mana value exceeds {max_cmc}"
                            search_player.graveyard.remove(c)
                            c.reset_battlefield_state()
                            c.summoning_sick = True
                            c.entered_this_turn = True
                            # July 24 batch-6 (reviewer D1): "It gains haste"
                            # riders (Puppeteer Clique) — opt-in flag.
                            if action.get("haste"):
                                c.summoning_sick = False
                                if 'Haste' not in (c.temp_keywords or []):
                                    c.temp_keywords.append('Haste')
                            p.battlefield.append(c)
                            if c.is_planeswalker():
                                try:
                                    c.loyalty_counters = int(c.loyalty or 0)
                                except (TypeError, ValueError):
                                    c.loyalty_counters = 0
                                from mtg.helpers import loyalty_from_commander_casts
                                c.loyalty_counters += loyalty_from_commander_casts(
                                    game, p, c)
                            game.register_static_keyword_grants(c, p.name)
                            game.register_static_pt_effects(c, p.name)
                            game.register_replacement_effects(c, p.name)
                            # Reanimation-aura LTB binding: Animate Dead /
                            # Dance of the Dead / Necromancy should
                            # sacrifice the reanimated creature when the
                            # aura leaves play. Find the aura on p's
                            # battlefield and attach a two-way binding.
                            src_name = (action.get("_source_card_name") or "").lower()
                            AURA_NAMES = {"animate dead", "dance of the dead", "necromancy"}
                            if src_name in AURA_NAMES:
                                for aura in p.battlefield:
                                    if aura.name.lower() == src_name and not getattr(aura, '_bound_creature_id', None):
                                        aura._bound_creature_id = c.id
                                        c._reanimated_by_aura_id = aura.id
                                        # July 30 batch-9 reviewer audit: the
                                        # bind never set attached_to, so the
                                        # aura's P/T mod (Animate Dead -1/-0)
                                        # stayed wherever the generic ETB
                                        # fallback had pointed it (Thassa).
                                        # Attach to the reanimated creature —
                                        # _get_attached_auras keys on it.
                                        aura.attached_to = c.id
                                        if not hasattr(c, 'attachments') or c.attachments is None:
                                            c.attachments = []
                                        if aura.id not in c.attachments:
                                            c.attachments.append(aura.id)
                                        print(f"[REANIMATE-BIND] {aura.name} bound to {c.name}")
                                        break
                            # Pub/sub slice 2: one PERMANENT_ENTERED per entry.
                            events.emit(events.PERMANENT_ENTERED, game, card=c,
                                        controller=p, via="reanimate", rules=rules)
                            # June 11 audit: the dedicated reanimate action had
                            # the same trigger hole as move_card; Craterhoof
                            # entered as a vanilla 5/5 after reanimation.
                            entry_msgs = _fire_noncast_battlefield_entry(
                                rules, game, p, c)
                            game.recalculate_granted_keywords()
                            game.recalculate_power_toughness()
                            result = f"⬆️ {p.name} reanimates **{c.name}** from {search_player.name}'s graveyard"
                            if entry_msgs:
                                result += "\n" + "\n".join(entry_msgs)
                            return result
        if max_cmc is not None:
            return f"No eligible permanent with mana value {max_cmc} or less to return"
        return None

    elif action_type == "search_library_to_graveyard":
        # Search library for cards and put them into graveyard (Buried Alive, Entomb)
        player_name = action.get("player")
        count = action.get("count", 1)
        card_type = action.get("card_type", "creature")
        p = find_player(player_name)
        if p:
            # [PW-STATIC] Ashiok, Dream Render: opponents can't search their library
            blocker = rules._opponent_prevents_library_search(game, p)
            if blocker:
                print(f"[PW-STATIC] {blocker} prevents {p.name} from searching their library")
                return f"🚫 {p.name} can't search their library ({blocker})"
            found = []
            for c in list(p.library):
                if len(found) >= count:
                    break
                type_line = (c.type_line or '').lower()
                if card_type.lower() in type_line:
                    found.append(c)
            for c in found:
                p.library.remove(c)
                p.graveyard.append(c)
            if found:
                import random as _rng
                _rng.shuffle(p.library)  # Shuffle after searching
                return f"🪦 {p.name} searches library, puts {', '.join(c.name for c in found)} into graveyard"
        return None

    elif action_type == "living_death":
        # Living Death: each player sacrifices all creatures, then returns all creatures from graveyards.
        # Audit fix: previous truncation at 6 items + "+N more" hid the actual
        # returned board state from players. Use a longer list (Discord's 2k
        # character limit is generous enough for typical Living Death payloads)
        # and put each player's events on their own line so the board state
        # is reconstructable from the message.
        messages_parts = []
        ld_died_list = []  # May 30 audit (F-LD1): collect sacrificed creatures for dies-triggers
        ld_returned_list = []  # July 20 batch-3 (M3): returned creatures — ETBs fire after the full wave
        # June 11 audit: triggers dispatch after Living Death finishes, by
        # which point old graveyard creatures have returned. Capture the
        # source set now so those returned permanents cannot see the earlier
        # simultaneous deaths retroactively (game 1514636909593497602).
        ld_source_ids = {
            c.id for source_player in game.players
            for c in source_player.battlefield
            if not getattr(c, '_phased_out', False)
        }
        for p in game.players:
            # Save graveyard creatures first.
            # June 10 deep-dive (B5d, CR 712.4a): a card in a graveyard has
            # FRONT-face characteristics only. Transformed sagas (Kirin-
            # Touched Orochi) sat in the graveyard wearing their creature
            # back face and Living Death returned them — an enchantment Saga
            # is not a legal return. The saga-table transform path can't
            # revert (front data overwritten), so exclude flagged cards;
            # real TDFCs revert via Card.transform() elsewhere.
            gy_creatures = [c for c in p.graveyard
                            if 'creature' in (c.type_line or '').lower()
                            and not getattr(c, '_transformed', False)]
            # Sacrifice all battlefield creatures
            bf_creatures = [c for c in p.battlefield if c.is_creature()]
            for c in bf_creatures:
                game.unregister_static_effects(c)
                p.battlefield.remove(c)
                p.graveyard.append(c)
                ld_died_list.append((c, p))
            if bf_creatures:
                bf_names = ', '.join(c.name for c in bf_creatures)
                messages_parts.append(f"{p.name} sacrifices {len(bf_creatures)}: {bf_names}")
            # Return graveyard creatures to battlefield
            for c in gy_creatures:
                if c in p.graveyard:
                    p.graveyard.remove(c)
                    # July 20 batch-3 audit (reviewer M3): this loop cleared
                    # state by hand (missing counters — CR 400.7) and never
                    # registered statics, emitted PERMANENT_ENTERED, or fired
                    # ETBs — Avenger of Zendikar returned to a 12-land board
                    # with zero Plants (game_1528946220087640286). The sibling
                    # reanimate handler got all of this June 11; mirror it.
                    c.reset_battlefield_state()
                    c.summoning_sick = True
                    c.entered_this_turn = True
                    p.battlefield.append(c)
                    game.register_static_keyword_grants(c, p.name)
                    game.register_static_pt_effects(c, p.name)
                    game.register_replacement_effects(c, p.name)
                    events.emit(events.PERMANENT_ENTERED, game, card=c,
                                controller=p, via="living_death", rules=rules)
                    ld_returned_list.append((c, p))
            if gy_creatures:
                gy_names = ', '.join(c.name for c in gy_creatures)
                messages_parts.append(f"{p.name} returns {len(gy_creatures)} from graveyard: {gy_names}")
        # May 30 audit (F-LD1): queue the SACRIFICED creatures so dies-triggers
        # (Blood Artist, Zulaport, Bastion, Grave Pact, etc.) actually fire.
        # Living Death previously removed creatures inline without queueing
        # _recently_died, so every dies-trigger on a Living Death was silently
        # dropped (Korvold's death was invisible to Bastion in
        # game_1508810609507045508). The graveyard-RETURNED creatures are a
        # separate "enters" event and are not queued here. Mirrors
        # destroy_all_creatures' _recently_died handling.
        if ld_died_list:
            if not hasattr(game, '_recently_died'):
                game._recently_died = []
            _queue_deaths_3a(game, ld_died_list)
            for dead_card, _dead_player in ld_died_list:
                game._dies_source_ids_by_dead_id[dead_card.id] = set(ld_source_ids)
        # July 20 batch-3 (M3): fire entries AFTER all simultaneous returns so
        # watchers that came back with the wave see their fellow arrivals
        # (CR 603.3a). Entry messages ride the same bullet list / truncation.
        for _ret_card, _ret_player in ld_returned_list:
            for _em in _fire_noncast_battlefield_entry(rules, game, _ret_player, _ret_card):
                messages_parts.append(_em)
        if messages_parts:
            # [LAYERS] Recalculate after mass creature entry
            game.recalculate_granted_keywords()
            game.recalculate_power_toughness()
            # Use newlines between per-player events for readability; if the
            # combined message would exceed Discord's 2000-char limit, fall
            # back to truncated names with explicit counts.
            full_msg = "💀 Living Death:\n" + "\n".join(f"  • {p}" for p in messages_parts)
            if len(full_msg) > 1900:
                # Fallback: list lengths only, full names get truncated to fit
                short_parts = []
                for part in messages_parts:
                    if len(part) > 300:
                        short_parts.append(part[:300] + "…")
                    else:
                        short_parts.append(part)
                full_msg = "💀 Living Death:\n" + "\n".join(f"  • {p}" for p in short_parts)
            return full_msg
        return None

    elif action_type == "rise_of_dark_realms":
        # Rise of the Dark Realms: put all creature cards from all graveyards onto your battlefield
        player_name = action.get("player")
        p = find_player(player_name)
        if p:
            returned = []
            for search_player in game.players:
                gy_creatures = [c for c in search_player.graveyard if 'creature' in (c.type_line or '').lower()]
                for c in gy_creatures:
                    search_player.graveyard.remove(c)
                    c.summoning_sick = True
                    c.entered_this_turn = True
                    # Clear damage/modifiers from previous time on battlefield
                    c.damage_marked = 0
                    c.deathtouch_damage = 0
                    c.power_modifier = 0
                    c.toughness_modifier = 0
                    c.tapped = False
                    p.battlefield.append(c)
                    returned.append(c.name)
            if returned:
                # [LAYERS] Recalculate after mass creature entry
                game.recalculate_granted_keywords()
                game.recalculate_power_toughness()
                return f"💀 Rise of the Dark Realms: {p.name} returns {len(returned)} creature(s) to battlefield"
        return None

    elif action_type == "open_the_vaults":
        # Open the Vaults: return all artifact and enchantment cards from ALL graveyards
        # to the battlefield under their OWNERS' control
        returning = []
        for search_player in game.players:
            qualifying = [c for c in search_player.graveyard
                          if ('artifact' in (c.type_line or '').lower()
                              or 'enchantment' in (c.type_line or '').lower())]
            for c in qualifying:
                search_player.graveyard.remove(c)
                c.reset_battlefield_state()
                c.entered_this_turn = True
                c.summoning_sick = c.is_creature()
                c.controller = search_player.name
                search_player.battlefield.append(c)
                returning.append((search_player, c))
        if returning:
            # Everything returns simultaneously. Finish the entire board wave
            # before dispatching any ETB so watchers see the complete state.
            for owner, card in returning:
                if card.is_enchantment() and 'aura' in (card.type_line or '').lower():
                    oracle = (card.oracle_text or '').lower()
                    if 'card in a graveyard' not in oracle:
                        if 'enchant land' in oracle:
                            candidates = [x for p in game.players for x in p.battlefield
                                          if x is not card and x.is_land()]
                        elif 'enchant artifact' in oracle:
                            candidates = [x for p in game.players for x in p.battlefield
                                          if x is not card and x.is_artifact()]
                        elif 'enchant permanent' in oracle:
                            candidates = [x for p in game.players for x in p.battlefield
                                          if x is not card]
                        else:
                            candidates = [x for p in game.players for x in p.battlefield
                                          if x is not card and x.is_creature(game)]
                        if 'you control' in oracle:
                            candidates = [x for x in candidates if x in owner.battlefield]
                        if candidates:
                            target = candidates[0]
                            card.attached_to = target.id
                            if card.id not in target.attachments:
                                target.attachments.append(card.id)
                            print(f"[OPEN-VAULTS-AURA] {card.name} attached to {target.name}")
                if 'saga' in (card.type_line or '').lower():
                    card.counters['lore'] = 1
                    print(f"[OPEN-VAULTS-SAGA] {card.name} enters with lore counter 1")
                game.register_static_keyword_grants(card, owner.name)
                game.register_static_pt_effects(card, owner.name)
                game.register_replacement_effects(card, owner.name)
                events.emit(events.PERMANENT_ENTERED, game, card=card,
                            controller=owner, via="open_the_vaults", rules=rules)

            entry_messages = []
            for owner, card in returning:
                entry_messages.extend(
                    _fire_noncast_battlefield_entry(rules, game, owner, card))
                if 'saga' in (card.type_line or '').lower():
                    engine = getattr(rules, 'engine_ref', None)
                    chapter = engine._get_saga_chapter_text(card, 1) if engine else ''
                    if chapter:
                        try:
                            from rules.effect_templates import get_effect_library, build_game_context
                            opponent = next(p for p in game.players if p is not owner)
                            ctx = build_game_context(game, owner, opponent, card=card)
                            chapter_actions, _ = get_effect_library().resolve_etb(
                                card.name, chapter, owner.name, opponent.name,
                                game_context=ctx)
                            if chapter_actions:
                                for chapter_action in chapter_actions:
                                    msg = rules._execute_action_on_state(game, chapter_action)
                                    if msg:
                                        entry_messages.append(msg)
                            else:
                                engine._queue_async_trigger(
                                    game, card, chapter, "saga_chapter_1", owner.name,
                                    context=f"{card.name} Chapter I (Open the Vaults)")
                        except Exception as exc:
                            print(f"[OPEN-VAULTS-SAGA] chapter I error: {exc}")
                            maybe_reraise(exc)
            game.recalculate_granted_keywords()
            game.recalculate_power_toughness()
            returned = [f"{c.name} ({p.name})" for p, c in returning]
            result = (f"✨ Open the Vaults: {len(returned)} artifact/enchantment "
                      f"card(s) returned — {', '.join(returned[:8])}")
            if len(returned) > 8:
                result += f" and {len(returned) - 8} more"
            if entry_messages:
                result += "\n" + "\n".join(entry_messages)
            return result
        return "✨ Open the Vaults: no qualifying cards in any graveyard"

    elif action_type == "replenish":
        # Replenish: return all enchantment cards from YOUR graveyard to the battlefield
        player_name = action.get("player")
        p = find_player(player_name)
        if p:
            qualifying = [c for c in p.graveyard
                          if 'enchantment' in (c.type_line or '').lower()]
            returned = []
            for c in qualifying:
                p.graveyard.remove(c)
                c.summoning_sick = True
                c.entered_this_turn = True
                c.damage_marked = 0
                c.deathtouch_damage = 0
                c.power_modifier = 0
                c.toughness_modifier = 0
                c.tapped = False
                p.battlefield.append(c)
                returned.append(c.name)
            if returned:
                game.recalculate_granted_keywords()
                game.recalculate_power_toughness()
                return f"✨ Replenish: {p.name} returns {len(returned)} enchantment(s) — {', '.join(returned[:8])}" + (f" and {len(returned) - 8} more" if len(returned) > 8 else "")
            return "✨ Replenish: no enchantments in graveyard"
        return None

    elif action_type == "flicker":
        # Exile a permanent and return it immediately (Ephemerate, Cloudshift, etc.)
        player_name = action.get("player")
        target_name = action.get("target", "")
        source_name = action.get("source", "")  # Optional: card that triggered the flicker
        # Apr 30 audit fix: cap nested flicker depth. Conjurer's Closet +
        # Thassa, Deep-Dwelling + Soulherder + Aminatou stacked in one game
        # produced 4391 nested flicker triggers (Python recursion limit) and
        # ~500 nested "↳ flicker:" prefixes in a single Discord message.
        # End-step + ETB-flicker chains are the worst offenders. We cap at
        # 8 nested flickers per top-level action — enough for legitimate
        # Aminatou/Brago combos but well short of the loops we saw.
        _flicker_depth = getattr(game, '_flicker_depth', 0)
        if _flicker_depth >= 8:
            print(f"[FLICKER-LOOP] Aborting nested flicker (depth={_flicker_depth}) for {target_name or 'auto-target'} from {source_name or 'unknown'}")
            return None
        game._flicker_depth = _flicker_depth + 1
        p = find_player(player_name)
        if p:
            target_card = None
            if target_name:
                for c in p.battlefield:
                    if c.name.lower() == target_name.lower() or target_name.lower() in c.name.lower():
                        target_card = c
                        break
            if not target_card:
                # Auto-select best ETB creature owned by player. Note: this
                # fallback should only fire when the AI didn't specify a
                # target — never override an AI-chosen target with our own
                # heuristic, even if the AI's pick has no ETB. (Bug #5: the
                # template was overriding AI targets with best_own_etb_creature.)
                # July 31 batch-10 reviewer: is_creature(game) — the no-game
                # call bypassed the devotion type-flip, and with no other
                # creatures the SOURCE became its own fallback target: Thassa,
                # Deep-Dwelling (devotion 1/5, not a creature, and printed
                # "one OTHER target creature") flickered HERSELF twice in
                # game_1532409540866212023. Excluding the source by name also
                # covers every real flicker source's "other/another" wording.
                _flicker_src_lower = (source_name or '').lower()
                for c in p.battlefield:
                    if (c.is_creature(game) and c.name.lower() != _flicker_src_lower
                            and c.oracle_text and 'enters' in c.oracle_text.lower()):
                        target_card = c
                        break
                if not target_card:
                    creatures = [c for c in p.battlefield
                                 if c.is_creature(game)
                                 and c.name.lower() != _flicker_src_lower]
                    if creatures:
                        target_card = creatures[0]
            # July 23 audit (#1): Aminatou's -1 is "exile another target
            # permanent YOU OWN" (CR 208: a reanimated/stolen permanent stays
            # owned by its original owner even while another player controls
            # it). The generic flicker handler otherwise resolves targets on
            # p.battlefield by control alone, so honoring an AI-declared target
            # (or the heuristic) could flicker — and permanently keep — an
            # opponent-owned reanimated creature (game_1529666152597426327:
            # Aminatou -1 flickered Rick's Animate-Dead'd Korvold). require_own
            # is Aminatou-only; other flicker sources (Ephemerate, Momentary
            # Blink, Ghostly Flicker) are "you control" and never set it.
            if action.get("require_own") and target_card is not None:
                _pidx = game.players.index(p) if p in game.players else 0
                from mtg.helpers import owns_card as _owns
                if not _owns(target_card, _pidx):
                    _owned = [c for c in p.battlefield
                              if _owns(c, _pidx)
                              and c.name.lower() != (source_name or '').lower()]
                    target_card = _owned[0] if _owned else None
                    if target_card is None:
                        print(f"[FLICKER] require_own: {player_name} owns no legal "
                              f"target — fizzling (was a controlled-not-owned permanent)")
            if target_card and target_card in p.battlefield:
                # Deregister continuous/replacement effects before flicker — re-register
                # below treats this as a fresh ETB. Otherwise stale effects accumulate
                # (e.g. Soulherder + Conjurer's Closet ping-ponging Elesh Norn).
                game.unregister_static_effects(target_card)
                exiled_trigger_msgs = _fire_creature_exiled_watchers(game, target_card)
                p.battlefield.remove(target_card)
                # Reset for re-entry
                target_card.tapped = False
                target_card.damage_marked = 0
                target_card.deathtouch_damage = 0
                target_card.summoning_sick = True
                target_card.entered_this_turn = True
                target_card.power_modifier = 0
                target_card.toughness_modifier = 0
                target_card.temp_keywords = []
                target_card.attachments = []
                # July 23 audit (#3, CR 400.7): clearing the creature's OWN
                # attachments list left the auras/equipment still pointing AT it
                # via attached_to, and the P/T layer walks permanents checking
                # `attached_to == self.id` — so Animate Dead's -1/-0 followed
                # Korvold through every flicker (his lethal swing came in at 9
                # instead of base 4 + 6 counters) and 704.5m never saw those
                # auras as unattached. The returned permanent is a NEW object:
                # auras fall off (then die to the aura SBA), equipment detaches.
                for _att_p in game.players:
                    for _att in _att_p.battlefield:
                        if getattr(_att, 'attached_to', None) == target_card.id:
                            _att.attached_to = None
                # July 23 audit (#2, CR 400.7): a permanent that leaves and
                # returns is a NEW object with no relationship to its previous
                # existence — counters do not carry over, exactly like the
                # tapped/damage/keyword/attachment resets above. Without this,
                # repeated Aminatou/Thassa flickers let +1/+1 counters climb
                # monotonically (game_1529666152597426327: Korvold accrued
                # 1→2→…→6 across flickers and became the lethal threat). The
                # re-ETB scan below re-applies any "enters with N counters".
                if hasattr(target_card, 'counters'):
                    target_card.counters = {}
                # Aug 1 batch-12 (reviewer, partner game): a flickered
                # PLANESWALKER re-enters with its PRINTED loyalty (CR 306.6 /
                # 400.7 — new object). move_card's battlefield-entry branch
                # has had this since June 10 (V8); this sibling entry path
                # never did, so Aminatou kept loyalty 7 through a flicker and
                # climbed to 10 (game_1532756760174268546).
                if target_card.is_planeswalker():
                    try:
                        target_card.loyalty_counters = int(target_card.loyalty)
                    except (TypeError, ValueError):
                        target_card.loyalty_counters = 0
                p.battlefield.append(target_card)
                # [LAYERS] Re-register static effects and recalculate after flicker
                game.register_static_keyword_grants(target_card, p.name)
                game.register_static_pt_effects(target_card, p.name)
                game.register_replacement_effects(target_card, p.name)
                game.recalculate_granted_keywords()
                game.recalculate_power_toughness()
                # CR 603.6a: re-entering creature triggers ETB abilities again.
                # Without this, Meteor Golem's destroy-target ETB, Wall of Omens'
                # draw-a-card ETB, and Charming Prince's modal ETB silently drop
                # on flicker. Three tiers fire:
                #  - Tier 1: resolve_special_effects (hardcoded — Terror of the
                #    Peaks, Panharmonicon, etc.)
                #  - Tier 1.5: own-ETB template via lib.resolve_etb (Charming
                #    Prince modal, Mulldrifter draw, token creators) — this is
                #    the template a normal cast hits in spells.py:1395
                #  - Other-creature triggers via _check_creature_etb_triggers_sync
                #    (Soul Warden, Impact Tremors)
                etb_msgs = list(exiled_trigger_msgs)
                # May 20 audit fix: temporarily reassign game._current_resolution_source
                # to the flickered (re-entering) card for the duration of the
                # re-ETB scan. Without this swap, inner actions like Spell
                # Queller's `exile_from_stack` template inherit the OUTER
                # flicker spell's name (Momentary Blink, Thassa, etc.) via the
                # auto-enrichment at execute_action_on_state line 234-238, so
                # "Spell Queller finds no valid target on the stack" emits as
                # "Momentary Blink finds no valid target..." attributing the
                # Queller's re-ETB to whatever spell flickered it
                # (game_1506604593243492454_discord.log:234,236).
                _saved_resolution_source = getattr(game, '_current_resolution_source', None)
                game._current_resolution_source = (target_card.name, p.name)
                if target_card.is_creature() and hasattr(rules, 'engine_ref') and rules.engine_ref:
                    try:
                        # Tier 1: hardcoded handlers
                        if hasattr(rules.engine_ref, 'resolve_special_effects'):
                            tier1_msgs = rules.engine_ref.resolve_special_effects(game, p, target_card)
                            if tier1_msgs:
                                etb_msgs.extend(tier1_msgs)
                        # Tier 1.5: own-ETB template (Aminatou flicker re-fires
                        # the entering creature's modal/draw/token effect, not
                        # just the other-creature triggers)
                        try:
                            from rules.effect_templates import get_effect_library, build_game_context
                            opp_p = next((pp for pp in game.players if pp != p), p)
                            lib = get_effect_library()
                            ctx = build_game_context(game, p, opp_p, card=target_card)
                            tmpl_actions, tmpl_desc = lib.resolve_etb(
                                card_name=target_card.name,
                                oracle_text=target_card.oracle_text or '',
                                controller=p.name,
                                opponent=opp_p.name,
                                game_context=ctx,
                            )
                            if tmpl_actions:
                                for tmpl_a in tmpl_actions:
                                    if tmpl_a.get('action') == 'no_action':
                                        continue
                                    try:
                                        tmpl_msg = rules._execute_action_on_state(game, tmpl_a)
                                        if tmpl_msg:
                                            etb_msgs.append(tmpl_msg)
                                    except Exception as e:
                                        print(f"[FLICKER-ETB-TEMPLATE] Action failed for {target_card.name}: {e}")
                                if any(a.get('action') != 'no_action' for a in tmpl_actions):
                                    print(f"[FLICKER-ETB-TEMPLATE] Resolved {target_card.name}: {tmpl_desc}")
                                    # May 16 audit: mark this ETB as
                                    # template-resolved so the AI's later
                                    # `resolve` action gets short-circuited
                                    # in engine._execute_action's resolve
                                    # path (Trinket Mage double-queue fix).
                                    if not hasattr(game, '_recently_resolved_etbs'):
                                        game._recently_resolved_etbs = set()
                                    game._recently_resolved_etbs.add(target_card.name)
                        except Exception as e:
                            print(f"[FLICKER-ETB-TEMPLATE] Lookup failed for {target_card.name}: {e}")
                        # Slice 2b (July 21): watcher dispatch ran in the
                        # PERMANENT_ENTERED subscriber (re-entry emit above).
                        from mtg.helpers import drain_pending_messages as _drain_pm2
                        etb_msgs.extend(_drain_pm2(game))
                    except Exception as e:
                        print(f"[FLICKER-ETB] Error firing re-entry triggers for {target_card.name}: {e}")
                        maybe_reraise(e)
                # May 20 audit fix: restore the outer resolution source so
                # subsequent actions inside the flicker spell's own resolution
                # are attributed correctly. Pair with the swap before the
                # re-ETB scan above.
                if _saved_resolution_source is not None:
                    game._current_resolution_source = _saved_resolution_source
                else:
                    try:
                        del game._current_resolution_source
                    except AttributeError:
                        pass
                # Dedup repeated flickers of the same creature in one turn
                # (Conjurer's Closet + Teleportation Circle + Thassa stacked
                # all flicker the same creature each end step → 3 identical
                # lines per turn). Track per-turn flickered names and after
                # the first announcement, suppress the visible line — ETB
                # triggers from re-entry still fire, just silently in Discord.
                if not hasattr(game, '_flicker_announce_seen'):
                    game._flicker_announce_seen = {}
                tname_key = target_card.name.lower()
                seen_count = game._flicker_announce_seen.get(tname_key, 0)
                game._flicker_announce_seen[tname_key] = seen_count + 1
                # Strip nested "↳ flicker:" / "↳ <source>:" breadcrumb prefixes from
                # any etb_msgs that came from a recursive flicker call. Without this,
                # each flicker layer prepends its own "↳" and a stacked chain of
                # 8-level flickers prints 8 prefixes per line — Apr 30 audit found
                # ~500 nested prefixes in a single Discord message.
                etb_msgs = [_strip_flicker_breadcrumb(m) for m in etb_msgs]
                prefix = f"✨ {source_name}: " if source_name else "✨ "
                game._flicker_depth = _flicker_depth  # Restore depth before returning
                if seen_count == 0:
                    base = f"{prefix}**{target_card.name}** flickered (exiled and returned to battlefield)"
                    if etb_msgs:
                        return base + "\n" + "\n".join(etb_msgs)
                    return base
                # Subsequent flickers of same creature: just emit ETB results
                # without the "X flickered" header, and prefix with source so
                # the player still sees what triggered.
                if etb_msgs:
                    return f"  ↳ {source_name or 'flicker'}: " + "; ".join(etb_msgs)
                return None
            else:
                # Suppress the "no creature to flicker" noise when the source
                # is an end-step / upkeep recurring trigger — these fire every
                # turn and shouldn't post a Discord line on empty battlefields.
                # Only the immediate one-shot flickers (Ephemerate, Cloudshift)
                # need the visible "no target" feedback.
                recurring_sources = {"conjurer's closet", "teleportation circle",
                                     "soulherder", "thassa, deep-dwelling",
                                     "yorion, sky nomad", "felidar guardian"}
                game._flicker_depth = _flicker_depth  # Restore depth before returning
                if source_name and source_name.lower() in recurring_sources:
                    return None
                prefix = f"✨ {source_name}: " if source_name else "✨ "
                return f"{prefix}no creature to flicker (empty battlefield)"
        game._flicker_depth = _flicker_depth  # Restore depth before returning
        return None

    elif action_type == "grant_keywords":
        # Grant temporary keywords to permanents until end of turn
        player_name = action.get("player")
        keywords = action.get("keywords", [])
        # Coerce non-list shapes (DeepSeek/Claude sometimes returns string or set or single keyword)
        if isinstance(keywords, str):
            keywords = [k.strip() for k in re.split(r'[,;|]+', keywords) if k.strip()]
        elif isinstance(keywords, set):
            keywords = list(keywords)
        elif keywords is None:
            keywords = []
        elif not isinstance(keywords, list):
            # Fallback: try to wrap in a list
            keywords = list(keywords) if hasattr(keywords, '__iter__') else [str(keywords)]
        # Aug 7 batch audit (G6-1): a Tier-3 grant with keywords=[] produced
        # the garbled "🛡️ Qwen's 13 permanents gain  until end of turn" —
        # an empty grant is a no-op, not a message.
        if not keywords:
            print("[GRANT-KEYWORDS] Refusing empty keyword list (no-op)")
            return None
        target = action.get("target", "all_own_permanents")
        target_card_name = action.get("target_card")
        requires_creature = (
            action.get("target_filter") == "creature")
        p = find_player(player_name)
        if p:
            if target_card_name:
                # Single-target keyword grant (e.g. Vivien +1: "target creature gains vigilance and reach")
                for card in p.battlefield:
                    if card.name.lower() == target_card_name.lower():
                        if requires_creature and not card.is_creature(game):
                            continue
                        for kw in keywords:
                            if kw not in (card.temp_keywords or []):
                                card.temp_keywords = card.temp_keywords or []
                                card.temp_keywords.append(kw)
                        return f"🛡️ {card.name} gains {', '.join(keywords)} until end of turn"
                return None
            # Restrictions the handler is ALREADY being handed but used to
            # ignore. Cavalry Pegasus's template passes
            # filter="attacking_knights_and_other_pegasi"; nothing read it, the
            # default target is all_own_permanents, and so every permanent the
            # controller owned — LANDS included — gained Flying. Observed
            # granting flying to 10, 12, 13 and 15 permanents in single turns,
            # including a Cat Soldier that is not a Human.
            subtype = (action.get("subtype") or "").strip().lower()
            attacking_only = (target == "attacking_subtype"
                              or "attacking" in (action.get("filter") or "").lower())
            unknown_filter = action.get("filter") and not subtype and not attacking_only
            if unknown_filter:
                # Refuse rather than silently widen to the whole battlefield —
                # a fabricated filter string should fail loudly, not buff the
                # board. Same principle as the Woodfall Primus "noncreature"
                # guard: an unrecognized restriction is not "no restriction".
                print(f"[GRANT-KEYWORDS] Refusing '{', '.join(keywords)}' — "
                      f"unrecognized filter {action.get('filter')!r}")
                return None
            count = 0
            granted = []
            for card in p.battlefield:
                if ((target == "all_own_creatures" or requires_creature)
                        and not card.is_creature(game)):
                    continue
                if attacking_only:
                    if not card.is_creature() or not getattr(card, 'attacking', False):
                        continue
                if subtype and subtype not in (card.type_line or "").lower():
                    continue
                for kw in keywords:
                    if kw not in (card.temp_keywords or []):
                        card.temp_keywords = card.temp_keywords or []
                        card.temp_keywords.append(kw)
                count += 1
                granted.append(card.name)
            if not count:
                return None
            # G6-1 (Aug 7): say "creature(s)" when the grant was
            # creature-filtered — "N permanents" for Unbreakable Formation
            # read as if lands were buffed.
            _noun = ("creature" if (target == "all_own_creatures" or requires_creature)
                     else "permanent")
            perm_word = _noun if count == 1 else _noun + "s"
            if subtype or attacking_only:
                who = ", ".join(granted[:4]) + (f" (+{count - 4} more)" if count > 4 else "")
                return f"🛡️ {who} gain {', '.join(keywords)} until end of turn"
            return f"🛡️ {p.name}'s {count} {perm_word} gain {', '.join(keywords)} until end of turn"
        return None

    elif action_type == "remove_keywords":
        # Aug 7 batch audit (G2-1, CRITICAL): "lose <keyword> until end of
        # turn" had NO action vocabulary — Shadowspear's "{1}: Permanents
        # your opponents control lose hexproof and indestructible until end
        # of turn" was a pure no-op on every activation (the Tier-3 judge
        # understood the semantics in prose but could only emit
        # grant_keywords with keywords=[]). Scope shapes: a named card via
        # "card", or every permanent of the named player's OPPONENTS via
        # "player" + target="opponent_permanents" (the Shadowspear shape).
        keywords = action.get("keywords", [])
        if isinstance(keywords, str):
            keywords = [k.strip() for k in re.split(r'[,;|]+', keywords) if k.strip()]
        keywords = [k for k in (keywords or []) if k]
        if not keywords:
            print("[REMOVE-KEYWORDS] Refusing empty keyword list (no-op)")
            return None
        target_card_name = action.get("card") or action.get("target_card")
        player_name = action.get("player")
        if target_card_name:
            for pl in game.players:
                for card in pl.battlefield:
                    if card.name.lower() == str(target_card_name).lower():
                        for kw in keywords:
                            if kw not in card.temp_removed_keywords:
                                card.temp_removed_keywords.append(kw)
                        print(f"[REMOVE-KEYWORDS] {card.name} loses "
                              f"{', '.join(keywords)} until end of turn")
                        return (f"🚫 {card.name} loses {', '.join(keywords)} "
                                f"until end of turn")
            return None
        controller = find_player(player_name)
        if controller:
            count = 0
            for pl in game.players:
                if pl is controller:
                    continue
                for card in pl.battlefield:
                    for kw in keywords:
                        if kw not in card.temp_removed_keywords:
                            card.temp_removed_keywords.append(kw)
                    count += 1
            if count:
                print(f"[REMOVE-KEYWORDS] {count} opponent permanent(s) lose "
                      f"{', '.join(keywords)} until end of turn")
                _pw = "permanent" if count == 1 else "permanents"
                return (f"🚫 Opponents' {count} {_pw} lose "
                        f"{', '.join(keywords)} until end of turn")
        return None

    elif action_type == "prevent_combat_damage":
        # Prevent all combat damage this turn (Fog, Moment's Peace, Constant Mists)
        # Sets a flag that _deal_combat_damage checks
        scope = action.get("scope", "all")  # "all" or "to_you"
        player_name = action.get("player", "")
        if scope == "all":
            # Prevent all combat damage for all players this turn
            for p in game.players:
                p._damage_prevented = True
                # Expires at end of turn (next player's untap)
                p._damage_prevented_expires_turn = game.turn_number + 1
            print(f"[FOG] All combat damage prevented this turn")
            return "🌫️ All combat damage is prevented this turn!"
        elif scope == "to_you" and player_name:
            p = find_player(player_name)
            if p:
                p._damage_prevented = True
                p._damage_prevented_expires_turn = game.turn_number + 1
                print(f"[FOG] Combat damage to {p.name} prevented this turn")
                return f"🌫️ Combat damage to {p.name} is prevented this turn"
        return None

    elif action_type == "grant_hand_cascade":
        # July 31 batch-10: Yidris, Maelstrom Wielder — "Whenever Yidris deals
        # combat damage to a player, as you cast spells from your hand this
        # turn, they gain cascade." Records the grant; the cascade block in
        # mtg/triggers.py consults game._hand_cascade_grants at cast time
        # (hand casts only — _cast_from_graveyard / _from_cascade excluded).
        # Before this, the commander's defining ability was a structural no-op
        # (unhandled combat-damage trigger → Tier-3 combat-shape refusal).
        player_name = action.get("player", "")
        p = find_player(player_name)
        if not p:
            return None
        game._hand_cascade_grants[p.name] = game.turn_number
        print(f"[CASCADE-GRANT] {p.name}'s hand-cast spells gain cascade this turn "
              f"(source: {action.get('source', 'Yidris')})")
        return (f"🌀 **{action.get('source', 'Yidris, Maelstrom Wielder')}**: "
                f"spells {p.name} casts from their hand this turn gain cascade")

    elif action_type == "grant_flashback":
        # Grant flashback to best instant/sorcery in graveyard.
        # May 24 audit fix: source name is parameterized — was hardcoded to
        # "Snapcaster Mage" so every Torrential Gearhulk ETB / Mission
        # Briefing / similar got mis-attributed in Discord (30+ instances
        # in the May 24 batch). Templates should pass `"source": "<card>"`;
        # default keeps backward compat with the Snapcaster template that
        # doesn't pass source.
        player_name = action.get("player")
        source_name = action.get("source", "Snapcaster Mage")
        p = find_player(player_name)
        if p:
            # July 20 audit (Past in Flames): grant_all covers "EACH instant
            # and sorcery card in your graveyard" (the single-best pick was a
            # silent under-grant), and announce_empty makes the empty-
            # graveyard case visible — Discord showed a full "resolves:"
            # oracle dump while the console said silent no-op
            # (game_1527448352298500096). Snapcaster's ETB keeps the silent
            # default (it fires often and a no-op line every time is spam).
            # Aug 1 batch-12 (reviewer, escape game): Yawgmoth's Will grants
            # "cast spells from your graveyard" with NO type restriction, but
            # this handler filtered to instants/sorceries only (correct for
            # its other users: Snapcaster, Gearhulk, Past in Flames). Opt-in
            # card_types="any" widens to every castable nonland; playing
            # LANDS from the graveyard stays unmodeled (play_land has no
            # graveyard branch — breadcrumb, not a silent drop).
            _any_type = action.get("card_types") == "any"
            def _gf_eligible(c):
                if _any_type:
                    return not c.is_land()
                return c.is_instant() or c.is_sorcery()
            if action.get("grant_all"):
                granted = [c for c in p.graveyard if _gf_eligible(c)]
                if granted:
                    for c in granted:
                        p.playable_from_graveyard.append(c.id)
                    names = ', '.join(c.name for c in granted[:6])
                    more = f" +{len(granted) - 6} more" if len(granted) > 6 else ""
                    _what = ("card(s) castable from the graveyard"
                             if _any_type else
                             "instant(s)/sorcery(s) gain flashback")
                    return (f"⚡ {source_name}: {len(granted)} {_what} "
                            f"until end of turn ({names}{more})")
                if action.get("announce_empty"):
                    _empty_what = ("castable cards" if _any_type
                                   else "instants or sorceries")
                    return (f"📜 {source_name}: no {_empty_what} in "
                            f"graveyard — no effect")
                print(f"[GRANT-FLASHBACK] {p.name} ({source_name}): no instant or sorcery in graveyard — silent no-op")
                return None
            # Pick highest-CMC instant/sorcery — most impactful flashback target
            best = None
            for c in p.graveyard:
                if c.is_instant() or c.is_sorcery():
                    if best is None or (c.cmc or 0) > (best.cmc or 0):
                        best = c
            if best:
                p.playable_from_graveyard.append(best.id)
                return f"⚡ {source_name}: **{best.name}** gains flashback until end of turn"
            if action.get("announce_empty"):
                return (f"📜 {source_name}: no instants or sorceries in "
                        f"graveyard — no effect")
            # Empty-graveyard case: silent no-op so Snapcaster ETB doesn't
            # post a useless line every fire (and Panharmonicon doesn't
            # double it). Console-only signal so audits can still grep.
            print(f"[GRANT-FLASHBACK] {p.name} ({source_name}): no instant or sorcery in graveyard — silent no-op")
            return None
        return None

    elif action_type == "queue_free_cast":
        player_name = action.get("player", "")
        p = find_player(player_name)
        if not p:
            return None
        from_zone = (action.get("from_zone") or "library").lower()
        zone = {
            "library": p.library, "graveyard": p.graveyard,
            "exile": p.exile, "hand": p.hand,
        }.get(from_zone)
        if zone is None:
            print(f"[FREE-CAST-QUEUE] Unsupported source zone: {from_zone}")
            return None
        wanted = (action.get("card") or "").strip().lower()
        card = next((c for c in zone if c.name.lower() == wanted), None)
        if card is None:
            print(f"[FREE-CAST-QUEUE] {wanted or '?'} not found in "
                  f"{p.name}'s {from_zone}")
            return None
        game._free_cast_pending.append({
            "card": card,
            "owner_index": game.players.index(p),
            "from_zone": from_zone,
            "fallback_zone": action.get("fallback_zone", "hand"),
            "source": action.get("source", "resolving effect"),
            "is_copy": False,
        })
        print(f"[FREE-CAST-QUEUE] {action.get('source', 'effect')}: "
              f"{card.name} from {from_zone}")
        return None

    elif action_type == "capricious_hellraiser_etb":
        p = find_player(action.get("player", ""))
        if not p:
            return None
        graveyard_cards = list(p.graveyard)
        if not graveyard_cards:
            return ("🔥 Capricious Hellraiser: graveyard is empty — "
                    "no cards are exiled")
        exiled = random.sample(
            graveyard_cards, min(3, len(graveyard_cards)))
        for card in exiled:
            p.graveyard.remove(card)
            p.exile.append(card)
        eligible = [
            card for card in exiled
            if not card.is_creature(game) and not card.is_land()
        ]
        if not eligible:
            names = ", ".join(card.name for card in exiled)
            return (f"🔥 Capricious Hellraiser exiles {names}; "
                    "none can be copied")
        chosen = max(
            eligible, key=lambda c: (int(getattr(c, 'cmc', 0) or 0), c.name))
        import copy as _copy
        import uuid as _uuid
        spell_copy = _copy.deepcopy(chosen)
        spell_copy.id = f"spellcopy_{_uuid.uuid4().hex[:10]}"
        spell_copy.owner_index = game.players.index(p)
        spell_copy._is_spell_copy = True
        spell_copy._is_copy = True
        spell_copy._copy_of = chosen.name
        spell_copy.is_token = False
        spell_copy._spell_resolved = False
        game._free_cast_pending.append({
            "card": spell_copy,
            "owner_index": game.players.index(p),
            "from_zone": "generated",
            "fallback_zone": "cease",
            "source": "Capricious Hellraiser",
            "is_copy": True,
        })
        names = ", ".join(card.name for card in exiled)
        print(f"[HELLRAISER] Exiled at random: {names}; "
              f"queued copy of {chosen.name}")
        return (f"🔥 Capricious Hellraiser exiles {names} and copies "
                f"**{chosen.name}**")
    elif action_type == "schedule_death_trigger":
        # Register a "if this creature dies this turn" watcher (Searing Blood, etc.)
        if not hasattr(game, '_death_watchers'):
            game._death_watchers = []
        game._death_watchers.append({
            "watch_target": action.get("watch_target", ""),
            "watch_target_id": action.get("watch_target_id", ""),
            "on_death_actions": action.get("on_death_actions", []),
            "source": action.get("source", "Unknown"),
            "turn_registered": game.turn_number,
        })
        return None  # Silent registration

    elif action_type == "track_exiled_by":
        # Track that a card was exiled by a specific permanent (for LTB return triggers)
        source_name = action.get("source", "")
        card_name = action.get("card", "")
        owner_name = action.get("owner", "")
        if source_name and card_name:
            # Find the source permanent on the battlefield
            for p in game.players:
                for c in p.battlefield:
                    if c.name.lower() == source_name.lower():
                        owner_idx = 0
                        for i, pl in enumerate(game.players):
                            if pl.name == owner_name:
                                owner_idx = i
                                break
                        exiled_by_key = f"_exiled_by_{c.id}"
                        if not hasattr(game, exiled_by_key):
                            setattr(game, exiled_by_key, [])
                        getattr(game, exiled_by_key).append({
                            'name': card_name, 'owner': owner_idx})
                        print(f"[LTB-TRACK] {card_name} exiled by {c.name}")
                        return None
        return None

    elif action_type == "rebound_cast":
        # Rebound: cast a spell from exile for free (Ephemerate, etc.)
        card_name = action.get("card", "")
        player_name = action.get("player", "")
        p = find_player(player_name)
        if p:
            for c in list(p.exile):
                if c.name.lower() == card_name.lower():
                    p.exile.remove(c)
                    c._from_rebound = True  # Don't trigger rebound again
                    # Put in hand temporarily so cast_spell can find it
                    p.hand.append(c)
                    return f"🔄 {card_name} rebounds from exile! Cast it for free."
        return None

    elif action_type == "register_turn_damage_doubler":
        # Insult // Injury (front half): "Damage can't be prevented this turn.
        # If a source you control would deal damage this turn, it deals double
        # that damage instead."
        # July 23 audit (#15): a turn-scoped CONTINUOUS replacement. Tier 3
        # correctly declined it ("sets up a continuous effect for the turn; no
        # immediate state change") so the doubling simply never happened and
        # that turn's combat damage landed undoubled
        # (game_1529677634588377108). Registered through the real replacement
        # engine rather than a bespoke flag so it inherits CR 616.1
        # controller-choice ordering when it stacks with Gisela / Furnace of
        # Rath. The condition closes over the turn number, so the effect
        # self-expires at end of turn — no cleanup pass or extra state needed.
        player_name = action.get("player")
        src_name = action.get("source") or "Insult // Injury"
        p = find_player(player_name)
        engine_r = getattr(game, 'replacement_engine', None)
        if p is None or engine_r is None:
            return None
        try:
            from rules.replacement import (
                ReplacementEffect as _TurnReplacementEffect,
                EventType as _RepEvent,
            )
        except ImportError:
            return None
        _turn = game.turn_number
        _sid = f"{src_name}_turn{_turn}_doubler"
        engine_r.add_effect(_TurnReplacementEffect(
            id=_sid,
            source_name=src_name,
            source_id=_sid,
            controller=p.name,
            replaces_event=_RepEvent.DAMAGE,
            condition_text=f"{src_name}: damage from your sources doubled this turn",
            replacement_type="double_damage",
            multiply_amount=2.0,
            # Same "source you control" gate Fiery Emancipation uses, plus the
            # turn clamp that makes it expire on its own.
            condition=lambda ev, _g=game, _t=_turn, _ctrl=p.name: (
                _g.turn_number == _t
                and bool(ev.source_controller)
                and ev.source_controller == _ctrl
            ),
        ))
        # Insult's OTHER clause: "Damage can't be prevented this turn." Same
        # self-expiring trick — an exact turn match, checked by
        # helpers.damage_prevention_disabled at every prevention gate, so Fog /
        # Teferi's Protection / Glacial Chasm can't blank the doubled damage.
        game._damage_prevention_off_turn = _turn
        print(f"[REPLACEMENT] {src_name}: registered turn-scoped damage doubler "
              f"for {p.name} + damage prevention off (turn {_turn})")
        return (f"⚡ **{src_name}** — damage from {p.name}'s sources is doubled "
                f"this turn, and damage can't be prevented")

    elif action_type == "schedule_delayed_trigger":
        # Schedule a trigger for a future phase (end_step, upkeep, etc.)
        # "upkeep_of" restricts an upkeep trigger to that player's own upkeep
        # ("your next upkeep" wording); accepts a player name or index.
        upkeep_of = action.get("upkeep_of")
        if isinstance(upkeep_of, str):
            _uo_player = find_player(upkeep_of)
            upkeep_of = (game.players.index(_uo_player)
                         if _uo_player in game.players else None)
        phase_of = action.get("phase_of")
        if isinstance(phase_of, str):
            _po_player = find_player(phase_of)
            phase_of = (game.players.index(_po_player)
                        if _po_player in game.players else None)
        game.delayed_triggers.append({
            "trigger_at": action.get("trigger_at", "end_step"),
            "actions": action.get("actions", []),
            "source": action.get("source", "Unknown"),
            "controller": action.get("controller", 0),
            "once": action.get("once", True),
            "turn_delay": action.get("turn_delay", 0),
            "upkeep_of": upkeep_of,
            "phase_of": phase_of,
        })
        source = action.get("source", "Unknown")
        trigger_at = action.get("trigger_at", "end_step")
        return f"⏰ {source} — delayed trigger scheduled for {trigger_at}"

    elif action_type == "destroy_all_creatures":
        # Board wipe: destroy all creatures on all battlefields
        # Phased-out creatures are not affected (CR 702.26)
        # Optional exclude_types filter (Cast Off: "destroy all non-Giant creatures")
        exclude_types = [t.lower() for t in action.get("exclude_types", []) or []]
        destroyed = []
        died_list = []  # For dies triggers
        save_return_msgs = []  # June 10 (C6): undying/persist return display lines
        for p in game.players:
            creatures_to_destroy = [c for c in p.battlefield
                                    if c.is_creature() and not getattr(c, '_phased_out', False)]
            for creature in creatures_to_destroy:
                # Check for indestructible
                if creature.has_keyword('Indestructible', game=game):
                    continue
                # Exclude-types filter (e.g. Cast Off keeps Giants alive)
                if exclude_types:
                    type_line_lower = (creature.type_line or '').lower()
                    if any(et in type_line_lower for et in exclude_types):
                        continue
                # May 30 audit: board wipes must honor destroy-replacement saves
                # (CR 614.6/702), not just indestructible. Mirror the SBA save order
                # (mtg/sba.py): shield counter -> totem (Umbra) armor -> destroy, then
                # undying/persist on the creatures that do die. Previously only
                # indestructible was checked, so the entire death_replacement mechanic
                # (Umbra armor, Woodfall Primus persist, undying) died permanently to
                # any wrath.
                if creature.counters.get('shield', 0) > 0:
                    creature.counters['shield'] -= 1
                    print(f"[SHIELD-COUNTER] {creature.name}: shield removed instead of destroyed (board wipe)")
                    continue
                if rules._has_totem_armor(creature, p):
                    _aura = rules._remove_totem_armor(creature, p, game)
                    print(f"[TOTEM-ARMOR] {creature.name}: {_aura.name if _aura else '?'} destroyed instead (board wipe)")
                    continue
                game.unregister_static_effects(creature)
                p.battlefield.remove(creature)
                p.graveyard.append(creature)
                destroyed.append(creature.name)
                died_list.append((creature, p))
                # Undying / Persist (CR 614.6): the creature died, but returns with
                # the appropriate counter if it qualifies. (ETB re-fire of the new
                # object is handled by the SBA path for damage deaths; here we return
                # the permanent so it isn't lost to the wrath.)
                if creature.has_keyword('Undying') or rules._permanent_grants_undying(game, creature, p):
                    if creature.counters.get('+1/+1', 0) == 0:
                        p.graveyard.remove(creature)
                        creature.damage_marked = 0
                        creature.deathtouch_damage = 0
                        creature.summoning_sick = True
                        creature.counters['+1/+1'] = creature.counters.get('+1/+1', 0) + 1
                        p.battlefield.append(creature)
                        print(f"[UNDYING] {creature.name} returned to battlefield with +1/+1 counter (board wipe)")
                        # June 10 (C6): re-register statics + new-object cleanup
                        # (combat state, enters-tapped, self-ETB re-fire).
                        game.register_static_keyword_grants(creature, p.name)
                        game.register_static_pt_effects(creature, p.name)
                        from mtg.sba import _finalize_death_save_return
                        save_return_msgs.extend(_finalize_death_save_return(rules, game, p, creature, 'UNDYING'))
                elif creature.has_keyword('Persist') and creature.counters.get('-1/-1', 0) == 0:
                    p.graveyard.remove(creature)
                    creature.damage_marked = 0
                    creature.deathtouch_damage = 0
                    creature.summoning_sick = True
                    creature.counters['-1/-1'] = creature.counters.get('-1/-1', 0) + 1
                    p.battlefield.append(creature)
                    print(f"[PERSIST] {creature.name} returned to battlefield with -1/-1 counter (board wipe)")
                    # June 10 (C6): same new-object cleanup as undying.
                    game.register_static_keyword_grants(creature, p.name)
                    game.register_static_pt_effects(creature, p.name)
                    from mtg.sba import _finalize_death_save_return
                    save_return_msgs.extend(_finalize_death_save_return(rules, game, p, creature, 'PERSIST'))
                else:
                    # Aug 7 batch audit (G3-1, CRITICAL): the mass path did a
                    # bare local-graveyard append, skipping the CR 903.9a
                    # commander redirect + CR 404.3 owner routing + CR 702.83a
                    # unearth exile that the single-target destroy performs —
                    # Damnation put Korvold in the graveyard and Rise of the
                    # Dark Realms handed the opponent their own commander for
                    # the lethal swing (game_1535060120164376726). Re-route
                    # the provisional placement through the shared helper.
                    # Dies triggers (queued above) still fire per CR 903.9b.
                    p.graveyard.remove(creature)
                    _dest = helpers.route_dead_permanent(game, creature, p)
                    if _dest == 'command_zone':
                        destroyed[-1] = f"{creature.name} (→ command zone, CR 903.9)"
        # Fire dies triggers for all creatures that died simultaneously
        # (Blood Artist, Zulaport Cutthroat, etc.)
        if died_list:
            if not hasattr(game, '_recently_died'):
                game._recently_died = []
            _queue_deaths_3a(game, died_list)
        if destroyed:
            game.recalculate_granted_keywords()
            game.recalculate_power_toughness()
            _wipe_msg = f"💥 Board wipe destroys {len(destroyed)} creatures: {_format_destroyed_list(destroyed)}"
            # July 20 audit (Decree of Pain): "Draw a card for each creature
            # destroyed this way" — the template dropped the draw clause
            # entirely (2 cards lost in game_1526071467035459665). Optional
            # param counts ACTUAL destroys (post shield/totem/indestructible
            # saves), so the count is CR-correct by construction.
            _draw_player = action.get("draw_per_destroyed")
            if _draw_player:
                _dp = find_player(_draw_player)
                if _dp:
                    _drawn = 0
                    for _ in range(len(destroyed)):
                        if not _dp.library:
                            break
                        _dp.hand.append(_dp.library.pop(0))
                        _drawn += 1
                    if _drawn:
                        _wipe_msg += f"\n🃏 **{_dp.name}** draws {_drawn} card(s) ({_drawn} creature(s) destroyed)"
            if save_return_msgs:
                _wipe_msg += "\n" + "\n".join(save_return_msgs)
            return _wipe_msg
        return f"💥 Board wipe (no creatures to destroy)"

    elif action_type == "populate":
        # CR 701.34a: create a token that's a copy of a creature token you
        # control; if you control none, populate does NOTHING. July 20 audit
        # (CRITICAL): Song of the Worldsoul escalated to Tier 3, which
        # fabricated a brand-new Human token out of nothing on an empty
        # token board (game_1526071401499328634) — the token then blocked
        # and fed an Athreos misfire. Deterministic handler; never invents.
        player = find_player(action.get("player", ""))
        if player:
            tokens = [c for c in player.battlefield
                      if getattr(c, 'is_token', False) and c.is_creature()
                      and not getattr(c, '_phased_out', False)]
            if not tokens:
                print(f"[POPULATE] {player.name}: no creature tokens — populate does nothing (CR 701.34a)")
                return None
            def _tok_power(c):
                try:
                    return c.get_effective_power(game)
                except (AttributeError, TypeError, ValueError):
                    try:
                        return int(c.power or 0)
                    except (TypeError, ValueError):
                        return 0
            best = max(tokens, key=_tok_power)
            copy_action = {
                "action": "create_token", "player": player.name,
                "name": best.name,
                "power": best.power or 0, "toughness": best.toughness or 0,
                "types": best.type_line or "Creature — Token",
                "count": 1,
                "oracle_text": best.oracle_text or "",
            }
            if getattr(best, 'keywords', None):
                copy_action["keywords"] = list(best.keywords)
            if getattr(best, 'colors', None):
                copy_action["colors"] = list(best.colors)
            msg = execute_action_on_state(rules, game, copy_action)
            print(f"[POPULATE] {player.name} populates: copy of {best.name}")
            return msg or f"🐑 **{player.name}** populates — a copy of {best.name}"
        return None

    elif action_type == "destroy_all_permanents":
        # Worldslayer / Apocalypse-class: destroy ALL permanents (including
        # lands), optionally excepting one named card (Worldslayer: "other
        # than this Equipment"). July 20 audit (CRITICAL): Worldslayer's
        # entire defining ability had no handler — three combat hits, zero
        # wipes (game_1526071467035459665). Honors indestructible + shield
        # counters; creatures that die queue dies-triggers like a board wipe.
        except_name = (action.get("except_card") or "").lower()
        destroyed = []
        died_list = []
        for p in game.players:
            for perm in list(p.battlefield):
                if getattr(perm, '_phased_out', False):
                    continue
                if except_name and perm.name.lower() == except_name:
                    continue
                if perm.has_keyword('Indestructible', game=game):
                    continue
                if perm.counters.get('shield', 0) > 0:
                    perm.counters['shield'] -= 1
                    print(f"[SHIELD-COUNTER] {perm.name}: shield removed instead of destroyed (Worldslayer wipe)")
                    continue
                game.unregister_static_effects(perm)
                p.battlefield.remove(perm)
                if getattr(perm, 'is_token', False):
                    destroyed.append(perm.name)
                    continue
                # G3-1 (Aug 7): shared routing — CR 903.9a redirect,
                # CR 404.3 owner graveyard, CR 702.83a unearth exile.
                helpers.route_dead_permanent(game, perm, p)
                destroyed.append(perm.name)
                if perm.is_creature():
                    died_list.append((perm, p))
        if died_list:
            if not hasattr(game, '_recently_died'):
                game._recently_died = []
            _queue_deaths_3a(game, died_list)
        game.recalculate_granted_keywords()
        game.recalculate_power_toughness()
        if destroyed:
            _shown = ', '.join(destroyed[:10])
            _more = f" +{len(destroyed) - 10} more" if len(destroyed) > 10 else ""
            _exc = f" (except {action.get('except_card')})" if except_name else ""
            return f"🌋 **All permanents destroyed**{_exc}: {_shown}{_more}"
        return None

    elif action_type == "destroy_by_power":
        # Dusk // Dawn style: destroy creatures with power >= threshold
        min_power = action.get("min_power", 3)
        max_power = action.get("max_power", None)
        destroyed = []
        died_list = []
        for p in game.players:
            creatures_to_destroy = [c for c in p.battlefield
                                    if c.is_creature() and not getattr(c, '_phased_out', False)]
            for creature in creatures_to_destroy:
                try:
                    eff_power = game.get_effective_power(creature) if hasattr(game, 'get_effective_power') else int(creature.power or 0)
                except (ValueError, TypeError):
                    eff_power = 0
                if min_power is not None and eff_power < min_power:
                    continue
                if max_power is not None and eff_power > max_power:
                    continue
                if creature.has_keyword('Indestructible', game=game):
                    continue
                game.unregister_static_effects(creature)
                p.battlefield.remove(creature)
                # G3-1 (Aug 7): shared routing (CR 903.9a / 404.3 / 702.83a).
                helpers.route_dead_permanent(game, creature, p)
                destroyed.append(creature.name)
                died_list.append((creature, p))
        if died_list:
            if not hasattr(game, '_recently_died'):
                game._recently_died = []
            _queue_deaths_3a(game, died_list)
        if destroyed:
            game.recalculate_granted_keywords()
            game.recalculate_power_toughness()
            return f"💥 Destroys {len(destroyed)} creatures (power >={min_power}): {_format_destroyed_list(destroyed)}"
        return f"💥 No creatures with power >={min_power} to destroy"

    elif action_type == "exile_all_by_type":
        # Merciless Eviction style: exile all permanents of a type
        perm_type = action.get("type", "creatures").lower()
        exiled = []
        died_list = []
        for p in game.players:
            to_exile = []
            for c in p.battlefield:
                if getattr(c, '_phased_out', False):
                    continue
                tl = (c.type_line or '').lower()
                if perm_type == "creatures" and c.is_creature():
                    to_exile.append(c)
                elif perm_type == "artifacts" and 'artifact' in tl:
                    to_exile.append(c)
                elif perm_type == "enchantments" and 'enchantment' in tl:
                    to_exile.append(c)
                elif perm_type == "planeswalkers" and c.is_planeswalker():
                    to_exile.append(c)
            for c in to_exile:
                game.unregister_static_effects(c)
                p.battlefield.remove(c)
                # G3-1 (Aug 7): CR 903.9a applies to exile too; owner's exile
                # zone, not the battlefield-holder's (CR 406.3 ownership).
                helpers.route_dead_permanent(game, c, p, to_exile=True)
                exiled.append(c.name)
                if c.is_creature():
                    died_list.append((c, p))
        if died_list:
            if not hasattr(game, '_recently_died'):
                game._recently_died = []
            _queue_deaths_3a(game, died_list)
        if exiled:
            game.recalculate_granted_keywords()
            game.recalculate_power_toughness()
            return f"✨ Exiles all {perm_type}: {', '.join(exiled[:8])}{'...' if len(exiled) > 8 else ''}"
        return f"✨ No {perm_type} to exile"

    elif action_type == "single_combat_wipe":
        # Single Combat: each player chooses one creature or planeswalker to keep;
        # all other creatures and planeswalkers are destroyed.
        # Auto-select: keep the highest-power creature (or highest-loyalty PW if no creatures).
        kept_display = []
        destroyed_names = []
        died_list = []
        for p in game.players:
            eligible = [
                c for c in p.battlefield
                if (c.is_creature() or c.is_planeswalker())
                and not getattr(c, '_phased_out', False)
            ]
            if not eligible:
                continue
            # Pick the "champion": best creature by power, falling back to PW by loyalty
            creatures = [c for c in eligible if c.is_creature()]
            pws = [c for c in eligible if c.is_planeswalker() and not c.is_creature()]
            if creatures:
                champion = max(creatures, key=lambda c: (
                    c.get_effective_power(game) if hasattr(c, 'get_effective_power') else int(c.power or 0)
                ))
            else:
                champion = max(pws, key=lambda c: c.loyalty_counters or 0)
            kept_display.append(f"{p.name} keeps {champion.name}")
            # Destroy everything else
            for c in eligible:
                if c is champion:
                    continue
                game.unregister_static_effects(c)
                p.battlefield.remove(c)
                # G3-1 (Aug 7): shared routing (CR 903.9a / 404.3 / 702.83a).
                helpers.route_dead_permanent(game, c, p)
                destroyed_names.append(c.name)
                if c.is_creature():
                    died_list.append((c, p))
        if died_list:
            if not hasattr(game, '_recently_died'):
                game._recently_died = []
            _queue_deaths_3a(game, died_list)
        if destroyed_names or kept_display:
            game.recalculate_granted_keywords()
            game.recalculate_power_toughness()
            kept_str = '; '.join(kept_display) or 'nothing'
            dest_str = ', '.join(destroyed_names[:8]) + ('...' if len(destroyed_names) > 8 else '')
            return (f"⚔️ Single Combat — {kept_str}. "
                    + (f"Destroyed: {dest_str}" if destroyed_names else "No other creatures/PWs."))
        return "⚔️ Single Combat — no creatures or planeswalkers on the battlefield"

    elif action_type == "destroy_all_by_type":
        # Modal board wipes (Austere Command, Akroma's Vengeance) — destroy
        # all permanents of a given type. Mirrors exile_all_by_type but routes
        # to graveyard instead of exile so dies-triggers fire correctly.
        # Optional max_cmc filter for Pernicious Deed-style "with mana value X or less".
        perm_type = action.get("type", "creatures").lower()
        max_cmc = action.get("max_cmc", None)
        destroyed = []
        died_list = []
        for p in game.players:
            to_destroy = []
            for c in p.battlefield:
                if getattr(c, '_phased_out', False):
                    continue
                tl = (c.type_line or '').lower()
                if max_cmc is not None:
                    try:
                        c_cmc = int(c.cmc) if c.cmc else 0
                    except (ValueError, TypeError):
                        c_cmc = 0
                    if c_cmc > max_cmc:
                        continue
                if perm_type == "creatures" and c.is_creature():
                    to_destroy.append(c)
                elif perm_type == "artifacts" and 'artifact' in tl:
                    to_destroy.append(c)
                elif perm_type == "enchantments" and 'enchantment' in tl:
                    to_destroy.append(c)
                elif perm_type == "planeswalkers" and c.is_planeswalker():
                    to_destroy.append(c)
                elif perm_type == "lands" and c.is_land():
                    to_destroy.append(c)
            for c in to_destroy:
                if c.has_keyword('Indestructible', game=game):
                    continue
                if c.counters.get('shield', 0) > 0:
                    c.counters['shield'] -= 1
                    print(f"[SHIELD-COUNTER] {c.name}: shield removed instead of destroyed")
                    continue
                if rules._has_totem_armor(c, p):
                    rules._remove_totem_armor(c, p, game)
                    continue
                game.unregister_static_effects(c)
                _revert_copy_if_leaving_battlefield(c)
                p.battlefield.remove(c)
                # G3-1 (Aug 7): shared routing (CR 903.9a / 404.3 / 702.83a).
                helpers.route_dead_permanent(game, c, p)
                destroyed.append(c.name)
                if c.is_creature():
                    died_list.append((c, p))
        if died_list:
            if not hasattr(game, '_recently_died'):
                game._recently_died = []
            _queue_deaths_3a(game, died_list)
        if destroyed:
            game.recalculate_granted_keywords()
            game.recalculate_power_toughness()
            # May 30 audit: show full names (Austere Command etc.) instead of a
            # bare "..." that hid which permanents died — only fall back to a
            # counted "+N more" when the line would get unwieldy. Mirrors the
            # May 17 Living Death full-names fix.
            _dnames = ', '.join(destroyed)
            if len(_dnames) > 300:
                _dnames = ', '.join(destroyed[:8]) + f" +{len(destroyed) - 8} more"
            return f"💥 Destroys all {perm_type}: {_dnames}"
        return f"💥 No {perm_type} to destroy"

    elif action_type == "destroy_creatures_by_cmc":
        # Austere Command modes ("creatures with mana value 3 or less" /
        # "creatures with mana value 4 or greater"). Filter on CMC then destroy.
        min_cmc = action.get("min_cmc", None)
        max_cmc = action.get("max_cmc", None)
        destroyed = []
        died_list = []
        for p in game.players:
            for c in list(p.battlefield):
                if not c.is_creature() or getattr(c, '_phased_out', False):
                    continue
                if c.has_keyword('Indestructible', game=game):
                    continue
                cmc = getattr(c, 'cmc', 0) or 0
                if min_cmc is not None and cmc < min_cmc:
                    continue
                if max_cmc is not None and cmc > max_cmc:
                    continue
                game.unregister_static_effects(c)
                p.battlefield.remove(c)
                # G3-1 (Aug 7): shared routing (CR 903.9a / 404.3 / 702.83a).
                helpers.route_dead_permanent(game, c, p)
                destroyed.append(c.name)
                died_list.append((c, p))
        if died_list:
            if not hasattr(game, '_recently_died'):
                game._recently_died = []
            _queue_deaths_3a(game, died_list)
        if destroyed:
            game.recalculate_granted_keywords()
            game.recalculate_power_toughness()
            range_str = (f"CMC {min_cmc}-{max_cmc}" if min_cmc is not None and max_cmc is not None
                         else f"CMC ≥{min_cmc}" if min_cmc is not None
                         else f"CMC ≤{max_cmc}" if max_cmc is not None
                         else "creatures")
            return f"💥 Destroys {len(destroyed)} creatures ({range_str}): {_format_destroyed_list(destroyed)}"
        return f"💥 No creatures matching CMC filter"

    elif action_type == "exile_all_graveyards":
        # Rest in Peace ETB: exile all cards from all graveyards
        total = 0
        for p in game.players:
            total += len(p.graveyard)
            p.exile.extend(p.graveyard)
            p.graveyard.clear()
        return f"✨ Rest in Peace: exiled {total} cards from all graveyards"

    elif action_type == "tuck_all_creatures":
        # Terminus-style: put all creatures on bottom of library (bypasses indestructible)
        # Phased-out creatures are not affected (CR 702.26)
        tucked = []
        import random as _rng
        for p in game.players:
            creatures_to_tuck = [c for c in p.battlefield
                                 if c.is_creature() and not getattr(c, '_phased_out', False)]
            for creature in creatures_to_tuck:
                game.unregister_static_effects(creature)
                p.battlefield.remove(creature)
                p.library.append(creature)  # Bottom of library
                tucked.append(creature.name)
            if creatures_to_tuck:
                _rng.shuffle(p.library)  # Randomize bottom portion
        if tucked:
            return f"📚 {len(tucked)} creatures put on bottom of libraries: {', '.join(tucked[:8])}{'...' if len(tucked) > 8 else ''}"
        return f"📚 No creatures to tuck"

    elif action_type == "bounce_all_opponents":
        # Cyclonic Rift overload: return all nonland permanents opponents control
        player_name = action.get("player")
        p = find_player(player_name)
        bounced = []
        if p:
            for sp in game.players:
                if sp.name == p.name:
                    continue
                to_bounce = [c for c in sp.battlefield if not c.is_land()]
                for card in to_bounce:
                    game.unregister_static_effects(card)
                    sp.battlefield.remove(card)
                    if getattr(card, 'is_token', False):
                        # Tokens cease to exist when they leave the battlefield (CR 111.8)
                        print(f"[TOKEN-SBA] Token {card.name} ceased to exist (bounced)")
                    else:
                        sp.hand.append(card)
                    bounced.append(card.name)
        if bounced:
            return f"🌊 Bounced {len(bounced)} nonland permanents: {', '.join(bounced[:8])}{'...' if len(bounced) > 8 else ''}"
        return None


    elif action_type == "steal_permanent":
        # Move a permanent from one player's battlefield to another's (Agent of Treachery, etc.)
        player_name = action.get("player")
        from_player_name = action.get("from_player")
        target_name = action.get("card", "")
        p = find_player(player_name)
        fp = find_player(from_player_name)
        if p and fp and target_name:
            target_card = None
            for c in fp.battlefield:
                if c.name.lower() == target_name.lower():
                    target_card = c
                    break
            if not target_card:
                # Fuzzy match
                for c in fp.battlefield:
                    if target_name.lower() in c.name.lower():
                        target_card = c
                        break
            if target_card and target_card in fp.battlefield:
                # Steal effects don't physically leave the battlefield (CR 800.4),
                # but the new controller's static effects need to re-evaluate.
                # Unregister + re-register handles ownership-tied effects cleanly.
                game.unregister_static_effects(target_card)
                fp.battlefield.remove(target_card)
                # Track original controller. Only temporary effects such as
                # Roil Elemental link the control change to the source leaving;
                # Agent of Treachery's control change has no such duration.
                if target_card.original_controller_index is None:
                    target_card.original_controller_index = game.players.index(fp)
                target_card.control_gained_by = (
                    action.get("source", "steal effect")
                    if action.get("until_source_leaves", False) else None)
                p.battlefield.append(target_card)
                # May 25 audit (F27): re-register the stolen permanent's OWN
                # static abilities under the new controller. Painter's Servant
                # was previously dropping its Layer 5 color-add effect on
                # control change — unregister_static_effects above wipes
                # Painter's "all permanents are also U" effect, but only the
                # control-change Layer 2 was re-registered below; Painter's
                # native static abilities went silent until it left the
                # battlefield. Per CR 611.3, continuous static abilities
                # apply regardless of controller as long as the source is on
                # the battlefield. The unregister-without-reregister pattern
                # also affected anthems, Mana Crypt's upkeep, etc.
                try:
                    game.register_static_keyword_grants(target_card, p.name)
                    game.register_static_pt_effects(target_card, p.name)
                    game.register_replacement_effects(target_card, p.name)
                except Exception as e:
                    print(f"[STEAL] Failed to re-register static effects for {target_card.name}: {e}")
                    maybe_reraise(e)
                # [LAYERS] Register Layer 2 control-change effect
                if HAS_LAYERS_ENGINE and game.layers_engine:
                    _ctrl_eff = create_control_effect(
                        source_name=action.get("source", "steal effect"),
                        source_id=f"steal_{target_card.id}_{p.name}",
                        controller=p.name, target_id=target_card.id,
                        new_controller=p.name)
                    game.layers_engine.add_effect(_ctrl_eff)
                    print(f"[LAYERS-L2] Registered control change: {target_card.name} -> {p.name}")
                game.recalculate_granted_keywords()
                game.recalculate_power_toughness()
                print(f"[STEAL] {p.name} gains control of {target_card.name} from {fp.name} "
                      f"(source: {action.get('source', 'steal effect')})")
                return f"🎭 {p.name} gains control of {target_card.name}!"
            else:
                print(f"[STEAL] Could not find '{target_name}' on {fp.name}'s battlefield")
                return None
        return None

    elif action_type == "search_library_land":
        # Search library for a land and put it onto battlefield (Path to Exile, Sakura-Tribe Elder)
        player_name = action.get("player")
        basic_only = action.get("basic_only", False)
        land_types = {str(t).strip().lower()
                      for t in (action.get("land_types") or []) if str(t).strip()}
        enters_tapped = action.get("enters_tapped", False)
        p = find_player(player_name)
        if p:
            # [PW-STATIC] Ashiok, Dream Render: opponents can't search their library
            blocker = rules._opponent_prevents_library_search(game, p)
            if blocker:
                print(f"[PW-STATIC] {blocker} prevents {p.name} from searching their library")
                return f"🚫 {p.name} can't search their library ({blocker})"
            basic_land_names = ['Plains', 'Island', 'Swamp', 'Mountain', 'Forest']
            import random as _rng
            for lib_card in p.library:
                found_land = False
                if basic_only and ('basic' in (lib_card.type_line or '').lower()
                                   or lib_card.name in basic_land_names):
                    if land_types:
                        _subtypes = (lib_card.type_line or '').lower()
                        found_land = any(t in _subtypes for t in land_types)
                    else:
                        found_land = True
                elif not basic_only and lib_card.is_land():
                    found_land = True
                if found_land:
                    p.library.remove(lib_card)
                    lib_card.entered_this_turn = True
                    # Apply shockland / checkland / fastland ETB-tapped logic.
                    # Without this, Wood Elves / Cultivate searching a shockland
                    # silently took the life loss (console-only) without echoing
                    # to Discord — the agent flagged this as a transparency gap.
                    shock_etb_msg = ""
                    if hasattr(rules, '_check_enters_tapped'):
                        try:
                            cond_tapped, shock_etb_msg = rules._check_enters_tapped(game, lib_card, p)
                            lib_card.tapped = enters_tapped or cond_tapped
                        except Exception as e:
                            print(f"[SEARCH-LAND] _check_enters_tapped error: {e}")
                            lib_card.tapped = enters_tapped
                    else:
                        lib_card.tapped = enters_tapped
                    p.battlefield.append(lib_card)
                    _rng.shuffle(p.library)
                    base_msg = f"🌍 {p.name} searches for {lib_card.name}{shock_etb_msg}{' (tapped)' if (enters_tapped and not shock_etb_msg) else ''}"
                    # Fire landfall triggers (Omnath, Courser of Kruphix, etc.)
                    if hasattr(rules, '_handle_land_etb'):
                        try:
                            land_msgs = rules._handle_land_etb(game, p, lib_card)
                            if land_msgs:
                                return base_msg + "\n" + "\n".join(land_msgs)
                        except Exception as e:
                            print(f"[LANDFALL] Error in search_library_land ETB: {e}")
                    return base_msg
        return None

    elif action_type == "shuffle_graveyard_into_library":
        player = find_player(action.get("player", ""))
        if player is None:
            return None
        import random as _rng
        moved = len(player.graveyard)
        player.library.extend(player.graveyard)
        player.graveyard.clear()
        _rng.shuffle(player.library)
        return (f"?? {player.name} shuffles {moved} card(s) from graveyard "
                f"into their library")

    elif action_type == "search_library":
        # Search library for one or more cards matching criteria (Recruiter,
        # Stoneforge, Trinket Mage, Tooth and Nail, Collected Company, etc.)
        #
        # Aug 8 audit (#3, CRITICAL): the interpreter chain briefly carried
        # TWO search_library branches — this one (reachable) read "card_type"
        # / "max_mv", while the JSON templates (Jarad's Orders, Gravebreaker
        # Lamia) and two effect_templates emitters (the generic "ETB: search
        # your library for a [X] card" PATTERN FAMILY, and Vivien, Monsters'
        # Advocate -2) send "filter_type" / "max_cmc" — the keys of a DEAD
        # duplicate branch shadowed by first-match if/elif. Result: those
        # tutors ran UNFILTERED and UNCAPPED (Jarad's Orders tutored a Swamp
        # to hand; Vivien -2 could put the highest-CMC creature onto the
        # battlefield with no cap). The duplicate is deleted; this branch
        # accepts both key families. A structural pin now forbids duplicate
        # action_type comparisons in this chain.
        player_name = action.get("player")
        card_type = action.get("card_type") or action.get("filter_type") or ""
        requested_name = (action.get("card_name") or action.get("tutor_card")
                          or "").strip().lower()
        max_toughness = action.get("max_toughness")
        max_mv = action.get("max_mv")
        if max_mv is None:
            max_mv = action.get("max_cmc")  # the duplicate's key (Vivien -2)
        exact_mv = action.get("exact_mv")
        # Accept both "to_zone" and "destination" (some templates use
        # destination — Tooth and Nail is one of them).
        to_zone = action.get("to_zone") or action.get("destination") or "hand"
        tapped = bool(action.get("tapped", False))
        do_shuffle = action.get("shuffle", True)
        try:
            count = max(1, int(action.get("count", 1) or 1))
        except (TypeError, ValueError):
            count = 1
        p = find_player(player_name)
        if p and p.library:
            # [PW-STATIC] Ashiok, Dream Render: opponents can't search their library
            blocker = rules._opponent_prevents_library_search(game, p)
            if blocker:
                print(f"[PW-STATIC] {blocker} prevents {p.name} from searching their library")
                return f"🚫 {p.name} can't search their library ({blocker})"
            import random as _rng
            # Aug 8 (#3): underscore compound sentinels ("aura_or_equipment",
            # Open the Armory) never split on " or " and matched no type line
            # — that tutor found NOTHING, ever. Normalize before splitting.
            _ct_normalized = card_type.replace("_", " ")
            type_parts = ([t.strip().lower() for t in _ct_normalized.split(" or ")]
                          if _ct_normalized else [])
            # Aug 2 batch-13 (escape/graveyard reviewer): the JSON templates
            # use "card_type": "Any" as an unrestricted-tutor sentinel
            # (Demonic Tutor, Entomb, Vile Entomber, Vampiric Tutor), but
            # this filter matched it as a literal type-line SUBSTRING — the
            # word "any" appears in no type line, so every candidate was
            # rejected and the searches "found nothing matching", twice live
            # in game_1533284299858514130. Sentinel words mean NO filter.
            type_parts = [t for t in type_parts
                          if t not in ('any', 'all', 'card', 'any card')]

            def _passes_filters(lib_card):
                """Type/MV/toughness restriction check — the printed search
                restriction, independent of any requested name."""
                type_line = (lib_card.type_line or "").lower()
                if type_parts and not any(t in type_line for t in type_parts):
                    return None
                cmc = int(lib_card.cmc) if lib_card.cmc else 0
                if max_toughness is not None:
                    try:
                        if lib_card.toughness and str(lib_card.toughness).lstrip('-').isdigit():
                            tt = int(lib_card.toughness)
                        else:
                            tt = 0
                    except (ValueError, TypeError):
                        tt = 0
                    if tt > max_toughness:
                        return None
                if max_mv is not None and cmc > max_mv:
                    return None
                if exact_mv is not None and cmc != exact_mv:
                    return None
                return cmc

            # Aug 8 (#3d): a requested name (the AI's typed tutor choice, or
            # a template's card_name) must STILL satisfy the printed search
            # restriction — the live defect was the model requesting a Swamp
            # from a creature-only search and the engine honoring it. A
            # rejected request falls back to auto-pick, with a visible line
            # so the AI feedback loop sees the rejection.
            named_pick = None
            if requested_name:
                for lib_card in p.library:
                    if lib_card.name.lower() == requested_name:
                        if _passes_filters(lib_card) is not None:
                            named_pick = lib_card
                        else:
                            print(f"[SEARCH-LIBRARY] rejected requested "
                                  f"'{lib_card.name}' — doesn't match the "
                                  f"printed search restriction "
                                  f"({card_type or 'any'}); auto-picking")
                        break

            # Pick the top-N candidates by CMC (highest first — mimic
            # the strategic "best creature" heuristic). A valid named pick
            # takes the first slot; auto-picks fill the rest.
            scored = []
            for lib_card in p.library:
                if lib_card is named_pick:
                    continue
                score = _passes_filters(lib_card)
                if score is not None:
                    scored.append((score, lib_card))
            scored.sort(key=lambda t: t[0], reverse=True)
            chosen = ([named_pick] if named_pick else []) + [c for _, c in scored]
            chosen = chosen[:count]

            if not chosen:
                return f"🔍 {p.name} searches library but finds nothing matching"

            found_names = []
            entry_msgs = []
            for chosen_card in chosen:
                p.library.remove(chosen_card)
                if to_zone == "battlefield":
                    chosen_card.entered_this_turn = True
                    chosen_card.summoning_sick = True
                    chosen_card.tapped = tapped
                    p.battlefield.append(chosen_card)
                    # The deleted duplicate registered statics on battlefield
                    # entry — keep that intent, and complete it with the
                    # mutation-site entry conventions every other
                    # put-onto-battlefield action follows (PERMANENT_ENTERED
                    # + the noncast entry funnel; the Pattern-of-Rebirth /
                    # Dread Return class).
                    try:
                        game.register_static_keyword_grants(chosen_card, p.name)
                        game.register_static_pt_effects(chosen_card, p.name)
                        game.register_replacement_effects(chosen_card, p.name)
                    except Exception as e:
                        maybe_reraise(e)
                        print(f"[SEARCH-LIBRARY] re-register failed for {chosen_card.name}: {e}")
                    events.emit(events.PERMANENT_ENTERED, game, card=chosen_card,
                                controller=p, via="search_library", rules=rules)
                    try:
                        entry_msgs.extend(
                            _fire_noncast_battlefield_entry(rules, game, p, chosen_card))
                    except Exception as e:
                        maybe_reraise(e)
                        print(f"[SEARCH-LIBRARY] entry triggers failed for {chosen_card.name}: {e}")
                elif to_zone == "library_top":
                    p.library.insert(0, chosen_card)
                elif to_zone == "graveyard":
                    # Aug 2 batch-13: this branch didn't exist — Entomb-class
                    # searches printed "→ graveyard" while appending to HAND.
                    # Invisible until the "Any" filter fix made these searches
                    # find anything at all (the pin caught it on first run).
                    p.graveyard.append(chosen_card)
                elif to_zone == "exile":
                    p.exile.append(chosen_card)
                else:
                    p.hand.append(chosen_card)
                found_names.append(chosen_card.name)
                print(f"[SEARCH-LIBRARY] {p.name} found {chosen_card.name} ({card_type}) → {to_zone}")

            if do_shuffle and to_zone != "library_top":
                _rng.shuffle(p.library)

            names_str = ", ".join(f"**{n}**" for n in found_names)
            if to_zone == "battlefield":
                base = (f"🔍 {p.name} searches library and puts {names_str} "
                        f"onto the battlefield{' tapped' if tapped else ''}")
                if entry_msgs:
                    return base + "\n" + "\n".join(entry_msgs)
                return base
            if to_zone == "library_top":
                return f"🔍 {p.name} searches library and places {names_str} on top"
            return f"🔍 {p.name} searches library and finds {names_str}"
        return None

    elif action_type == "edict_sacrifice":
        # Each player sacrifices a creature (Plaguecrafter, Fleshbag Marauder)
        types = action.get("types", "creature")
        fallback = action.get("fallback")  # "discard" for Plaguecrafter
        opponents_only = action.get("opponents_only", False)
        source = action.get("source", "edict effect")
        msgs = []
        for p in game.players:
            if opponents_only and p == game.active_player:
                continue
            candidates = [c for c in p.creatures() if not getattr(c, 'is_commander', False)]
            if "planeswalker" in types:
                candidates += [c for c in p.active_battlefield() if c.is_planeswalker() and not getattr(c, 'is_commander', False)]
            if candidates:
                worst = min(candidates, key=lambda c: int(c.cmc) if c.cmc else 0)
                game.unregister_static_effects(worst)
                p.battlefield.remove(worst)
                p.graveyard.append(worst)
                msgs.append(f"💀 {p.name} sacrifices **{worst.name}**")
                print(f"[EDICT] {p.name} sacrifices {worst.name}")
            elif fallback == "discard" and p.hand:
                discard = p.hand.pop()
                p.graveyard.append(discard)
                msgs.append(f"🃏 {p.name} discards **{discard.name}** (no creature to sacrifice)")
        return "\n".join(msgs) if msgs else None

    elif action_type == "sacrifice_land":
        # Sacrifice a land (Shard Volley, Harrow additional cost)
        player_name = action.get("player")
        p = find_player(player_name)
        if p:
            # Find worst land to sacrifice (lowest value — prefer basic over non-basic)
            lands = [c for c in p.battlefield if c.is_land()]
            if lands:
                # Prefer tapped basics, then tapped non-basics, then untapped basics
                def land_sacrifice_priority(l):
                    is_basic = "basic" in (l.type_line or "").lower()
                    return (0 if l.tapped else 1, 0 if is_basic else 1)
                lands.sort(key=land_sacrifice_priority)
                victim = lands[0]
                game.unregister_static_effects(victim)
                p.battlefield.remove(victim)
                p.graveyard.append(victim)
                print(f"[SACRIFICE-LAND] {p.name} sacrifices {victim.name}")
                return f"💀 {p.name} sacrifices **{victim.name}**"
            else:
                return f"⚠️ {p.name} has no land to sacrifice"
        return None

    elif action_type == "sacrifice_permanent":
        # Sacrifice a single permanent (Korvold ETB, Daretti -2, Goblin Bombardment, etc.)
        player_name = action.get("player")
        exclude_name = (action.get("exclude") or "").lower()
        type_filter = (action.get("type_filter") or "").lower()  # e.g. "artifact"
        reason = action.get("reason", "sacrifice a permanent")
        preferred_card = (action.get("preferred_card") or "").lower()
        only_preferred = bool(action.get("only_preferred", False))
        allow_commander = bool(action.get("allow_commander", False))
        p = find_player(player_name)
        if p:
            base = [c for c in p.battlefield if c.name.lower() != exclude_name]
            if type_filter:
                # July 23 audit (#6): a "sacrifice a creature" edict (Grave
                # Pact, Dictate of Erebos) must use the devotion-aware
                # is_creature(game), not a substring match on the printed
                # type_line — otherwise a devotion-gated god (Erebos at
                # devotion < 5) counts as a creature and gets sacrificed even
                # though CR 205.4b / 704 make it not a creature right then
                # (game_1529674672545988631: Erebos sacrificed to Dictate at
                # devotion 2).
                if type_filter == "creature":
                    base = [c for c in base if c.is_creature(game)]
                else:
                    base = [c for c in base if type_filter in (c.type_line or '').lower()]
            if only_preferred:
                base = [c for c in base if c.name.lower() == preferred_card]
            # July 23 audit (#10): prefer non-commanders, but a MANDATORY edict
            # ("each other player sacrifices a creature of their choice") can
            # force a commander when it's the only legal choice — CR 903.9a only
            # grants the OPTION to redirect it to the command zone afterward, it
            # doesn't exempt the commander from being chosen. The old hard
            # exclusion reported "no permanent to sacrifice" while Gisela (a
            # plain commander creature) sat untouched on the battlefield
            # (game_1529677634588377108: Grave Pact + Dictate both whiffed).
            if allow_commander:
                candidates = base
            else:
                non_cmd = [c for c in base if not getattr(c, 'is_commander', False)]
                candidates = non_cmd or base
            if not candidates:
                # July 24 batch-6 (reviewer A1, MINOR): name the actual
                # restriction — "no permanent" for a creature edict misled
                # audits (Butcher of Malakir's parallel path already said
                # "no creatures").
                _noun = type_filter if type_filter else "permanent"
                return f"⚠️ {p.name} has no {_noun} to sacrifice"
            # Prefer tokens → lowest CMC non-land → lowest CMC land
            def sac_priority(c):
                is_preferred = c.name.lower() == preferred_card
                is_token = getattr(c, 'is_token', False)
                is_land = c.is_land()
                cmc = int(c.cmc) if c.cmc else 0
                return (0 if is_preferred else 1,
                        0 if is_token else 1,
                        0 if not is_land else 1, cmc)
            candidates.sort(key=sac_priority)
            victim = candidates[0]
            game.unregister_static_effects(victim)
            p.battlefield.remove(victim)
            # Aug 7 batch audit (C-3): a declared attacker sacrificed
            # mid-combat (Korvold's attack trigger eating a fellow attacker)
            # kept its .attacking flag into the graveyard — the per-combat
            # clear can't find an object that left the battlefield, and only
            # the end-turn [COMBAT-SWEEP] net caught it
            # (game_1535060130167521392). Strip at the leave chokepoint.
            helpers.strip_combat_state(game, victim)
            moved_to_command_zone = False
            # Commanders go to command zone — the OWNER's, not the
            # battlefield-holder's (June 10 audit C7, CR 903.9a).
            from mtg.helpers import commander_declines_graveyard_redirect
            if (getattr(victim, 'is_commander', False) and hasattr(game, 'format')
                    and game.format in ('commander', 'edh', 'brawl', 'oathbreaker')
                    and commander_declines_graveyard_redirect(victim)):
                p.graveyard.append(victim)
                print(f"[SACRIFICE] {p.name} sacrifices {victim.name} — "
                      f"declines the command-zone redirect (escape available), "
                      f"stays in graveyard ({reason})")
            elif getattr(victim, 'is_commander', False) and hasattr(game, 'format') and game.format in ('commander', 'edh', 'brawl', 'oathbreaker'):
                from mtg.helpers import command_zone_owner
                _zone_owner = command_zone_owner(game, victim, p)
                if not hasattr(_zone_owner, 'command_zone'):
                    _zone_owner.command_zone = []
                _zone_owner.command_zone.append(victim)
                moved_to_command_zone = True
                print(f"[SACRIFICE] {p.name} sacrifices {victim.name} → "
                      f"{_zone_owner.name}'s command zone ({reason})")
            else:
                # Unearth (CR 702.83a): exiled if it would LEAVE the
                # battlefield, sacrifice included — otherwise it can be
                # unearthed again out of the graveyard.
                from mtg.helpers import unearthed_leaves_to_exile
                if unearthed_leaves_to_exile(victim):
                    p.exile.append(victim)
                    print(f"[UNEARTH] {p.name} sacrifices {victim.name} → exiled "
                          f"(CR 702.83a) ({reason})")
                else:
                    p.graveyard.append(victim)
                    print(f"[SACRIFICE] {p.name} sacrifices {victim.name} ({reason})")

            # A sacrificed commander dies before the engine's direct
            # command-zone approximation (CR 903.9a is an SBA choice), so it
            # must still feed dies-trigger dispatch.
            if victim.is_creature():
                if not hasattr(game, '_recently_died'):
                    game._recently_died = []
                _queue_deaths_3a(game, [(victim, p)])

            # [SACRIFICE-TRIGGER] Scan controller's battlefield for "Whenever you
            # sacrifice a permanent" triggers (Korvold, Mayhem Devil, etc.)
            sac_msgs = _fire_sacrifice_triggers(rules, game, p, victim)
            # July 23 audit (#4): undying / persist return on SACRIFICE
            # (CR 702.92 / 702.77). The SBA, single-target destroy, and board
            # wipe paths all had this save-chain; sacrifice did NOT, so a
            # sacrificed Kitchen Finks could only come back via the Tier-3 dies
            # escalation — which has no counter gate and re-returned it on every
            # death (game_1529674672545988631, free-life loop). Mirrors the
            # destroy block, counter gates included. A CZ-redirected commander
            # never reached the graveyard, so the save can't apply.
            # Aug 10: extracted to mtg.sba.apply_death_save_on_sacrifice so the
            # Phyrexian Tower tap-sacrifice path (which had NO save chain at
            # all) shares one implementation instead of a fourth hand copy.
            save_msgs = []
            if not moved_to_command_zone:
                from mtg.sba import apply_death_save_on_sacrifice
                save_msgs = apply_death_save_on_sacrifice(rules, game, p, victim)
            # July 24 batch-6 anomaly (reviewer A1 #7): the Butcher of Malakir
            # template's edict lines had no source prefix while the hardcoded
            # Grave Pact/Dictate path wraps its own — a 3-edict cascade showed
            # "💀 Claude sacrifices X" with no attribution for exactly one of
            # the three. Opt-in `source` field; paths that already wrap a
            # prefix don't set it, so no double prefix.
            _sac_src = (action.get("source") or "").strip()
            _who_sacrifices = f"{p.name} sacrifices **{victim.name}**"
            base_msg = (f"💀 {_sac_src}: 💀 {_who_sacrifices}" if _sac_src
                        else f"💀 {_who_sacrifices}")
            if moved_to_command_zone:
                base_msg += " → command zone"
            followup_msgs = []
            # Victimize-style "If you do" effects belong here: follow-up
            # actions execute only after a legal permanent was sacrificed.
            for followup in action.get("then_actions", []):
                result = rules._execute_action_on_state(game, followup)
                if result:
                    followup_msgs.append(result)
            all_msgs = list(sac_msgs or []) + save_msgs + followup_msgs
            if all_msgs:
                return base_msg + "\n" + "\n".join(all_msgs)
            return base_msg
        return None

    elif action_type == "exile_top_play_or_damage":
        # Chandra +1: exile the exact object. A nonland may be cast this turn;
        # if that object was not cast, deferred cleanup damages each opponent.
        player_name = action.get("player")
        damage = int(action.get("damage", 2) or 2)
        p = find_player(player_name)
        if p and p.library:
            card = p.library.pop(0)
            p.exile.append(card)
            game.conditional_exile_casts[card.id] = {
                "card_id": card.id,
                "controller_index": game.players.index(p),
                "expires_turn": game.turn_number,
                "source_name": action.get("source") or "Chandra, Torch of Defiance",
                "damage": damage,
            }
            cast_note = ("You may cast it this turn."
                         if not card.is_land()
                         else "It is a land, so it cannot be cast.")
            print(f"[CHANDRA-EXILE] {p.name} exiles {card.name}; conditional window opened")
            return f"🔥 Exiles **{card.name}**. {cast_note}"
        return None

    elif action_type == "exile_top_of_library":
        # July 29 batch audit: Ragavan's combat-damage trigger ("create a
        # Treasure token and exile the top card of that player's library")
        # had no vocabulary for the exile half — the whole trigger queued to
        # Tier 3, whose combat-shaped-resolve guard refused it every time.
        # Exiles face up. The "you may cast that card this turn" rider is
        # NOT modeled (cross-player impulse casting isn't a thing the
        # castable list understands yet) — the denial half still lands.
        player_name = action.get("player")
        count = int(action.get("count", 1) or 1)
        p = find_player(player_name)
        if p is None:
            return None
        # Aug 1 (spectacle package): opt-in "playable": true marks the
        # exiled cards on p.playable_from_exile — SAME-PLAYER impulse
        # (Light Up the Stage exiles your own library and YOU may play
        # them; both executors accept that list as a cast source). The
        # end_turn wipe makes the window approximate "until the end of
        # your next turn" — accepted, documented here. Ragavan's
        # CROSS-player impulse rider stays unmodeled (the denial half
        # still lands).
        _mark_playable = bool(action.get("playable", False))
        exiled_names = []
        for _ in range(count):
            if not p.library:
                break
            top = p.library.pop(0)
            p.exile.append(top)
            if _mark_playable:
                p.playable_from_exile.append(top.id)
                game._impulse_cast_turns[top.id] = game.turn_number
            exiled_names.append(top.name)
        if not exiled_names:
            return f"📚 {p.name}'s library is empty — nothing to exile"
        _play_note = " (playable this turn)" if _mark_playable else ""
        print(f"[EXILE-TOP] {p.name} exiles from top of library: "
              f"{', '.join(exiled_names)}{_play_note}")
        return f"📤 Exiled **{', '.join(exiled_names)}** from the top of {p.name}'s library"

    elif action_type == "exile_from_stack":
        # Exile target spell from the stack (Spell Queller).
        # May 18 audit: the old code did `stack_item.get('card') if isinstance(stack_item, dict) else None`
        # but game.stack contains StackEntry DATACLASSES, not dicts — so the
        # isinstance check was always False and the function ALWAYS returned
        # "no valid target", silently fizzling every Spell Queller cast in
        # the May 17 batch. Also: the exile went to `game.players[0]`
        # unconditionally rather than the owner's exile zone, and there was
        # no tracking that linked the Queller to its exiled card for the LTB
        # return path. Fix all three.
        max_mv = action.get("max_mv", 99)
        source_name = action.get("_source_card_name") or "Spell Queller"
        source_controller_name = action.get("_source_controller") or action.get("controller") or ""
        if game.stack:
            for i in range(len(game.stack) - 1, -1, -1):
                stack_item = game.stack[i]
                # StackEntry exposes `.card`; defensive fallback for legacy dicts.
                stack_card = getattr(stack_item, 'card', None)
                if stack_card is None and isinstance(stack_item, dict):
                    stack_card = stack_item.get('card')
                if stack_card is None or not hasattr(stack_card, 'cmc'):
                    continue
                # Skip the Queller's own ETB trigger object if it somehow
                # appears alongside the spell it should target.
                if getattr(stack_item, 'is_spell', True) is False:
                    continue
                # CR-style MV check (X spells: cmc reads as the X-substituted value).
                if (stack_card.cmc or 0) > max_mv:
                    continue
                # Pop the stack entry, removing from the priority system mirror too.
                game.stack.pop(i)
                ps = getattr(game, '_priority_system', None)
                pid = getattr(stack_item, 'priority_id', None)
                if ps is not None and pid:
                    try:
                        if hasattr(ps, 'remove_stack_entry_by_priority_id'):
                            ps.remove_stack_entry_by_priority_id(pid)
                    except Exception as e:
                        # Phantom-stack-entry class (May 18 audit): a failed
                        # mirror-pop desyncs game.stack vs PrioritySystem.stack.
                        print(f"[QUELLER-EXILE] Priority-mirror pop failed: {e}")
                        maybe_reraise(e)
                # Owner-aware exile zone. Stack entries track controller_name;
                # owner may differ for stolen-then-cast spells but for the
                # common case controller==owner this is right. Fall back to
                # game.players[0] only if everything else fails.
                exile_owner = None
                ctrl_name = getattr(stack_item, 'controller_name', '') or ''
                if ctrl_name:
                    for sp in game.players:
                        if sp.name == ctrl_name:
                            exile_owner = sp
                            break
                if exile_owner is None and stack_card in getattr(game.players[0], 'hand', []):
                    exile_owner = game.players[0]
                if exile_owner is None:
                    exile_owner = game.players[0]
                # Cleanup: ensure the card isn't lingering in hand somewhere.
                for sp in game.players:
                    if stack_card in sp.hand:
                        sp.hand.remove(stack_card)
                exile_owner.exile.append(stack_card)
                # May 18 audit: record the Queller↔exiled-card link so the
                # LTB template can return the right card to hand when the
                # Queller leaves. Keyed by Queller's source card-name to
                # avoid mixing up multiple Quellers' exile zones.
                if not hasattr(game, '_queller_exiles'):
                    game._queller_exiles = {}  # source_card_name → list[(card, owner_name)]
                game._queller_exiles.setdefault(source_name, []).append(
                    (stack_card, exile_owner.name)
                )
                # July 30 batch-9 audit (CRITICAL, latent): without a counter
                # mark, the exiled spell's waiting cast_spell_async coroutine
                # reads its vanished entry as "now at top" and RESOLVES the
                # exiled spell (the Summary Dismissal class, caught live with
                # Song of the Worldsoul before Queller's first real exile).
                # countered_to="already_handled" — the zone move happened here.
                try:
                    stack_item.countered = True
                    stack_item.countered_to = "already_handled"
                except AttributeError:
                    pass  # legacy dict entries carry no coroutine to unwind
                _wake = getattr(stack_item, 'resolution_event', None)
                if _wake is not None:
                    _wake.set()
                print(f"[QUELLER-EXILE] {source_name} exiled {stack_card.name} "
                      f"(owner={exile_owner.name}, cmc={stack_card.cmc})")
                return f"✨ {source_name} exiles **{stack_card.name}** from the stack!"
        # May 25 audit (F2): silently fizzle per CR 603.3c. The original
        # `📜 ... finds no valid target on the stack` message produced 9-line
        # cascades when Thassa, Deep-Dwelling flickered a Spell Queller
        # repeatedly into an empty stack — each re-ETB tried to target a
        # spell, found none, and emitted the noisy line. Per CR 603.3c a
        # triggered ability with no legal target on resolution does nothing
        # (it shouldn't even have gone on the stack at trigger-creation
        # time, but the engine doesn't yet pre-filter at that point). Log
        # to console so audits can still see the empty fizzles, but don't
        # spam Discord.
        print(f"[QUELLER-EXILE-EMPTY] {source_name} fizzled — no valid spell on stack")
        return None

    elif action_type == "release_queller_exile":
        # May 18 audit: pair to exile_from_stack — when Spell Queller leaves
        # the battlefield, return the card it exiled to its owner's hand.
        # Real card says "owner may cast without paying mana", which we
        # approximate as "return to hand" so the owner can re-cast normally.
        source_name = action.get("source") or "Spell Queller"
        bucket = getattr(game, '_queller_exiles', {}).get(source_name, [])
        if not bucket:
            return None  # No exiled cards to release; silent no-op
        # FIFO: return the oldest exile first. Multiple Spell Quellers in
        # play with multiple exiled cards is unusual, but the data structure
        # supports it.
        released_card, owner_name = bucket.pop(0)
        if not bucket:
            del game._queller_exiles[source_name]
        # Locate the owner and move the card from exile back to hand.
        owner = None
        for sp in game.players:
            if sp.name == owner_name:
                owner = sp
                break
        if owner is None:
            print(f"[QUELLER-RELEASE] Owner '{owner_name}' for {released_card.name} not found; "
                  f"sending to game.players[0] hand as fallback")
            owner = game.players[0]
        if released_card in owner.exile:
            owner.exile.remove(released_card)
        else:
            # Defensive: card may have been moved by another effect (Ravenform-
            # style face-down exile, Vanishing-style exile cleanup). Search
            # all players' exile zones before falling back to a no-op.
            for sp in game.players:
                if released_card in sp.exile:
                    sp.exile.remove(released_card)
                    break
        owner.hand.append(released_card)
        print(f"[QUELLER-RELEASE] {source_name} LTB: returned {released_card.name} "
              f"to {owner.name}'s hand")
        return f"🔓 **{released_card.name}** returns to {owner.name}'s hand ({source_name} left the battlefield)"

    elif action_type == "mass_flicker":
        # Flicker multiple nonland permanents (Yorion, Brago, etc.).
        # When require_ownership=True (Yorion default), only flicker permanents
        # the player OWNS, not just permanents they currently control. CR 110.1
        # — control changes don't transfer ownership, so Yorion's "permanents
        # you own" excludes anything stolen via Agent of Treachery, Mind Control,
        # threaten effects, etc.
        player_name = action.get("player")
        count = action.get("count", 5)
        exclude_lands = action.get("exclude_lands", True)
        exclude_self = action.get("exclude_self")  # Card name to skip (e.g. Yorion)
        require_ownership = action.get("require_ownership", True)
        p = find_player(player_name)
        if p:
            try:
                p_idx = game.players.index(p)
            except ValueError:
                p_idx = None
            # Select up to N nonland permanents with ETB abilities
            candidates = []
            for c in p.battlefield:
                if exclude_lands and c.is_land():
                    continue
                # Skip the source card (e.g. Yorion can't flicker itself)
                if exclude_self and c.name == exclude_self:
                    continue
                # Owner check (Yorion: "permanents you own"). Only flicker
                # permanents this player actually owns; skip stolen permanents.
                if require_ownership and p_idx is not None:
                    from mtg.helpers import owns_card as _owns
                    if not _owns(c, p_idx):
                        continue
                # Prefer permanents with ETB effects
                has_etb = c.oracle_text and 'enters' in c.oracle_text.lower() if c.oracle_text else False
                candidates.append((c, has_etb))
            # Sort: ETB permanents first, then by CMC
            candidates.sort(key=lambda x: (not x[1], -(getattr(x[0], 'cmc', 0) or 0)))
            to_flicker = [c for c, _ in candidates[:count]]

            # July 20 audit: Yorion's return is a DELAYED trigger ("Return
            # those cards to the battlefield at the beginning of the next end
            # step", CR 603.7) — resolving it immediately let Yorion ↔ Felidar
            # Guardian re-trigger each other in the same resolution, producing
            # a 204-flicker / 359-mill storm with duplicate mills that decided
            # game_1527451733679149057. With delayed_return the cards sit in
            # exile until the scheduled end-step trigger returns them (same
            # move_card-from-exile shape as the Oath of Teferi template).
            # Brago-class immediate flickers don't pass the flag and keep the
            # instant exile-and-return below.
            if action.get("delayed_return"):
                exiled_names = []
                return_actions = []
                exile_msgs = []
                for card in to_flicker:
                    if card in p.battlefield:
                        game.unregister_static_effects(card)
                        exile_msgs.extend(_fire_creature_exiled_watchers(game, card))
                        p.battlefield.remove(card)
                        card.tapped = False
                        card.damage_marked = 0
                        card.deathtouch_damage = 0
                        card.power_modifier = 0
                        card.toughness_modifier = 0
                        card.temp_keywords = []
                        card.attachments = []
                        p.exile.append(card)
                        exiled_names.append(card.name)
                        return_actions.append({
                            "action": "move_card", "card": card.name,
                            "from_zone": "exile", "to_zone": "battlefield",
                            "player": p.name})
                if exiled_names:
                    game.recalculate_granted_keywords()
                    game.recalculate_power_toughness()
                    game.delayed_triggers.append({
                        "trigger_at": "end_step",
                        "actions": return_actions,
                        "source": exclude_self or "Mass flicker",
                        "controller": p_idx if p_idx is not None else 0,
                        "once": True,
                        "turn_delay": 0,
                        "upkeep_of": None,
                        "phase_of": None,
                    })
                    return (f"✨ Exiled {len(exiled_names)} permanents: "
                            f"{', '.join(exiled_names)} — returning at the "
                            f"beginning of the next end step"
                            + ("\n" + "\n".join(exile_msgs) if exile_msgs else ""))
                return None

            flickered_names = []
            etb_messages = []  # F26: collect ETB-trigger messages for each flickered permanent
            for card in to_flicker:
                if card in p.battlefield:
                    # Deregister continuous/replacement effects before flicker — re-register
                    # below treats this as a fresh ETB. Otherwise stale anthem/replacement
                    # effects accumulate every time the card flickers.
                    game.unregister_static_effects(card)
                    etb_messages.extend(_fire_creature_exiled_watchers(game, card))
                    p.battlefield.remove(card)
                    # Reset for re-entry
                    card.tapped = False
                    card.damage_marked = 0
                    card.deathtouch_damage = 0
                    card.summoning_sick = True
                    card.entered_this_turn = True
                    card.power_modifier = 0
                    card.toughness_modifier = 0
                    card.temp_keywords = []
                    card.attachments = []
                    p.battlefield.append(card)
                    # [LAYERS] Re-register static effects after flicker re-entry
                    game.register_static_keyword_grants(card, p.name)
                    game.register_static_pt_effects(card, p.name)
                    game.register_replacement_effects(card, p.name)
                    flickered_names.append(card.name)
                    # Pub/sub slice 2: each flicker re-entry is a new
                    # physical entry (CR 603.6) — one event per re-entry.
                    events.emit(events.PERMANENT_ENTERED, game, card=card,
                                controller=p, via="mass_flicker", rules=rules)
                    # May 25 audit (F26): fire ETB triggers for the re-entering
                    # permanent. Previously mass_flicker (Brago combat trigger,
                    # Yorion ETB) moved each permanent off and back on the
                    # battlefield without re-firing ETB triggers — so Brago
                    # flickering Agent of Treachery / Felidar Guardian / Omen of
                    # the Sea would re-tap them but skip their value-engine
                    # triggers entirely (25 missed ETBs across one game in the
                    # May 25 batch). Per CR 603.6, each separate enters-the-
                    # battlefield event triggers ETB abilities afresh.
                    try:
                        for msg in _fire_noncast_battlefield_entry(rules, game, p, card):
                            print(f"[MASS-FLICKER-ETB] {msg}")
                            etb_messages.append(msg)
                    except Exception as e:
                        print(f"[MASS-FLICKER-ETB] Error firing ETBs for {card.name}: {e}")
            if flickered_names:
                game.recalculate_granted_keywords()
                game.recalculate_power_toughness()
                base = f"✨ Flickered {len(flickered_names)} permanents: {', '.join(flickered_names)}"
                if etb_messages:
                    return base + "\n" + "\n".join(etb_messages)
                return base
        return None

    elif action_type == "bounce_own_permanent":
        # Return a permanent you control to hand (Dream Stalker, Kor Skyfisher,
        # Usher to Safety, etc.). Optional `card` param picks a specific permanent;
        # otherwise picks cheapest non-land.
        player_name = action.get("player")
        exclude_name = action.get("exclude", "")
        specific_card = (action.get("card") or "").lower()
        # June 11 audit: "return a CREATURE you control" cards (Whitemane
        # Lion) were dispatched here without a type restriction and bounced
        # a Plains (game 1514629231433351168 turn 3). type_filter narrows
        # the candidate pool; an empty pool fizzles rather than bouncing an
        # illegal object.
        type_filter = (action.get("type_filter") or "").lower()
        p = find_player(player_name)
        if p:
            def _type_ok(c):
                return type_filter != "creature" or c.is_creature()
            bounce_target = None
            if specific_card:
                # Honor explicit target if it's on battlefield
                for c in p.battlefield:
                    if c.name.lower() == specific_card and c.name.lower() != exclude_name.lower() and _type_ok(c):
                        bounce_target = c
                        break
            if bounce_target is None:
                # Pick cheapest non-land permanent that isn't the source
                candidates = [c for c in p.battlefield
                              if not c.is_land() and c.name.lower() != exclude_name.lower() and _type_ok(c)]
                if not candidates and not type_filter:
                    # Must bounce something — even rules if no other option
                    candidates = [c for c in p.battlefield if c.name.lower() != exclude_name.lower()]
                if not candidates and type_filter:
                    print(f"[BOUNCE-OWN] No legal {type_filter} to return — fizzles")
                    return None
                if candidates:
                    candidates.sort(key=lambda c: getattr(c, 'cmc', 0) or 0)
                    bounce_target = candidates[0]
            if bounce_target:
                battlefield_name = bounce_target.name
                game.unregister_static_effects(bounce_target)
                p.battlefield.remove(bounce_target)
                # Tokens cease to exist when they leave the battlefield (CR 704.5d)
                if getattr(bounce_target, 'is_token', False):
                    return f"🏠 {bounce_target.name} token returned to hand and ceases to exist"
                # June 11 audit: Dream Stalker bounced a Spark Double that was
                # copying Baral; without reverting the copy effect, the card in
                # hand was illegally recast as Baral (game 1514618379749560442).
                _revert_copy_if_leaving_battlefield(bounce_target)
                p.hand.append(bounce_target)
                if bounce_target.name != battlefield_name:
                    return (f"🏠 {battlefield_name} returned to {p.name}'s hand "
                            f"as {bounce_target.name}")
                return f"🏠 {bounce_target.name} returned to {p.name}'s hand"
        return None

    elif action_type == "cycle":
        # Pay the cycling cost, discard this card, draw a card, and fire any
        # "When you cycle ~" triggers. Apr 30 audit: Shark Typhoon's cycling was
        # being routed as a hardcast (paid {5}{U} instead of cycle cost {X}{1}{U})
        # because cycling had no first-class action support.
        player_name = action.get("player")
        card_name = (action.get("card") or "").lower()
        x_value = action.get("x", 0)
        p = find_player(player_name)
        if not p or not card_name:
            return None
        cycle_card = None
        for c in p.hand:
            if c.name.lower() == card_name or card_name in c.name.lower():
                cycle_card = c
                break
        if not cycle_card or not cycle_card.oracle_text:
            return None
        # Parse cycling cost from oracle text. Examples:
        #   "Cycling {2}"           → fixed cost
        #   "Cycling {X}{1}{U}"     → variable cost, X passed by AI
        #   "Cycling {1}{U}"        → fixed cost
        m = re.search(r'cycling\s*((?:\{[^}]+\})+)', cycle_card.oracle_text, re.IGNORECASE)
        if not m:
            return None
        cycle_cost_str = m.group(1)
        # If oracle has {X}, substitute the AI-supplied X (or 0 if missing).
        # We pre-substitute so the cost-payment code sees a concrete cost.
        x_filled_cost = cycle_cost_str.replace('{X}', '{' + str(int(x_value)) + '}')
        # Tap mana sources for the cycle cost. Prefer the structured ManaCost
        # path when available; fall back to a simple symbol-count tally.
        if not hasattr(p, '_pay_cycling_cost_simple'):
            # Implement payment inline using the same approach as activate-cost.
            try:
                from rules.mana import ManaCost as _ManaCost
                cost = _ManaCost.parse(x_filled_cost)
                if not p.can_pay_mana_cost(game, cost):
                    return f"⚠️ {p.name} cannot pay cycling cost {x_filled_cost} for {cycle_card.name}"
                p.pay_mana_cost(game, cost)
            except Exception:
                # Fallback: sum {N} numerics + count colored pips, compare to total mana.
                generic_match = re.findall(r'\{(\d+)\}', x_filled_cost)
                colored_match = re.findall(r'\{([WUBRGCS])\}', x_filled_cost)
                generic_needed = sum(int(g) for g in generic_match)
                colored_needed = len(colored_match)
                # Simple available-mana tally
                available = sum(1 for land in p.untapped_lands())
                if available < generic_needed + colored_needed:
                    return f"⚠️ {p.name} cannot pay cycling cost {x_filled_cost} for {cycle_card.name}"
                # Tap that many lands (greedy, no color check — autoplay safety net)
                tapped = 0
                for land in list(p.untapped_lands()):
                    if tapped >= generic_needed + colored_needed:
                        break
                    land.tapped = True
                    tapped += 1
        # Discard the card. Madness applies to cost discards too (CR
        # 702.35b — cycling a madness card is the classic interaction:
        # draw AND cast it for the madness cost).
        p.hand.remove(cycle_card)
        from mtg.helpers import madness_discard_to_exile
        if madness_discard_to_exile(game, p, cycle_card) is None:
            p.graveyard.append(cycle_card)
        # Draw a card. Hooked for dredge/miracle like the other draw sites —
        # cycling into a miracle is the classic line, and an unhooked draw
        # also corrupts cards_drawn_this_turn in both directions.
        from mtg.helpers import note_miracle_on_draw as _note_miracle
        from mtg.helpers import try_dredge as _try_dredge
        drawn = None
        if _try_dredge(game, p) is None and p.library:
            drawn = p.library.pop(0)
            p.hand.append(drawn)
            p.cards_drawn_this_turn += 1
            _note_miracle(game, p, drawn)
        # Fire "When you cycle ~" trigger via Tier 1.5 template library
        cycle_msgs = []
        try:
            from rules.effect_templates import get_effect_library, build_game_context
            lib = get_effect_library()
            opp = next((pp for pp in game.players if pp != p), p)
            ctx = build_game_context(game, p, opp, card=cycle_card)
            ctx['_cycle_x'] = int(x_value)  # Templates read X from ctx for X/X token cards
            # Look for "When you cycle <card>" trigger paragraph
            trigger_text = ""
            for paragraph in (cycle_card.oracle_text or '').split('\n'):
                p_lower = paragraph.lower().strip()
                if 'when you cycle' in p_lower:
                    trigger_text = paragraph.strip()
                    break
            if trigger_text:
                actions_list, explanation = lib.resolve_etb(
                    card_name=cycle_card.name,
                    oracle_text=trigger_text,
                    controller=p.name,
                    opponent=opp.name,
                    game_context=ctx,
                )
                if actions_list:
                    for sub in actions_list:
                        if sub.get('action') == 'no_action':
                            continue
                        sub_msg = rules._execute_action_on_state(game, sub)
                        if sub_msg:
                            cycle_msgs.append(sub_msg)
        except Exception as e:
            print(f"[CYCLE] template lookup failed for {cycle_card.name}: {e}")
        base = f"♻️ {p.name} cycles **{cycle_card.name}** (paid {x_filled_cost}, drew a card)"
        if cycle_msgs:
            return base + "\n" + "\n".join(cycle_msgs)
        return base


    return None
