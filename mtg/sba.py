"""State-based actions — CR 704 (creatures with 0 toughness die, etc.).

Three free functions extracted from RulesEngine during the Phase 2 OSS
refactor. The wrapping pattern is the same as Phase 2A/2B: each function
takes a RulesEngine instance as first arg, RulesEngine keeps thin
delegator methods, callers don't change.

Public free functions:

    process_state_based_actions(rules, game)
        Main SBA loop. Called by the engine each time priority changes.
        Repeatedly applies SBAs until no more apply (CR 704.3).

    check_state_based_actions(rules, game)
        Single SBA pass — collects everything that needs to happen.

    check_sba_inline_fallback(rules, game)
        Inline implementation that runs alongside the rules.state_based_actions
        module for side-by-side validation. Will go away once the rules
        module is fully trusted.

State touched on `rules`:

    rules.engine_ref            — back-reference to GameEngine
    rules.game_log              — game event log
    rules._has_totem_armor, rules._remove_totem_armor
    rules._permanent_grants_undying
    rules._player_cant_lose
    (and the other functions in this module via rules.X)

Extracted from mtg/rules_engine.py during the Phase 2 OSS-readability
refactor (Phase 2C).
"""

import random
from typing import Any, Dict, List, Optional, Tuple

from mtg.constants import Phase, Zone, COMMAND_ZONE_FORMATS
from mtg import events
from mtg.helpers import command_zone_owner, owner_of
from mtg import helpers
from mtg.models import Card, Player, GameState


def _restore_identity_on_battlefield_exit(card: Card) -> None:
    """Share the copy/manifest zone-change inverse with action-driven exits."""
    # Local import avoids making the actions/SBA extraction depend on import
    # order while keeping one implementation of the printed-identity reset.
    from mtg.actions import _revert_copy_if_leaving_battlefield
    _revert_copy_if_leaving_battlefield(card)


# Hardcoded back-face attributes for transforming sagas, used as a fallback
# when Scryfall data only loaded the front face. Sagas in this dict will
# actually return-transformed at chapter completion instead of getting stuck
# in exile.
_TRANSFORMING_SAGA_BACK_FACES: Dict[str, Dict[str, str]] = {
    "the restoration of eiganjo": {
        "name": "Architect of Restoration",
        "type_line": "Enchantment Creature — Fox Monk",
        "oracle_text": "Vigilance\nWhenever this creature attacks or blocks, create a 1/1 colorless Spirit creature token.",
        "power": "3",
        "toughness": "4",
    },
    "the elder dragon war": {
        "name": "The Elder Dragon War",
        "type_line": "Enchantment",
        "oracle_text": "",
        "power": "",
        "toughness": "",
    },
    # Kamigawa: Neon Dynasty modal DFC sagas — front face is a Saga, back
    # face is the matching land. All follow the same shape.
    "the kami war": {
        "name": "O-Kagachi Made Manifest",
        "type_line": "Legendary Creature — Dragon Spirit",
        "oracle_text": "Flying, trample\nWhenever O-Kagachi Made Manifest attacks, gain control of target nonland permanent until end of turn. Untap it. It gains haste until end of turn.",
        "power": "6", "toughness": "6",
    },
    "fable of the mirror-breaker": {
        "name": "Reflection of Kiki-Jiki",
        "type_line": "Legendary Creature — Goblin Shaman",
        "oracle_text": "{T}: Create a token that's a copy of another target creature you control, except it's a 1/1 red Goblin Shaman with 'When this creature enters, tap target creature an opponent controls.' Sacrifice it at the beginning of the next end step.",
        "power": "2", "toughness": "2",
    },
    "the akroan war": {
        "name": "The Akroan War",
        "type_line": "Enchantment",
        "oracle_text": "",
        "power": "", "toughness": "",
    },
    # May 20 audit: missing Kamigawa: Neon Dynasty + March of the Machine
    # transforming sagas surfaced from game_1506623303794561024 ("no back_face
    # attribute available" → saga exiled instead of transformed).
    # Scryfall-verified via data/card_data_cache.json:
    "teachings of the kirin": {
        "name": "Kirin-Touched Orochi",
        "type_line": "Enchantment Creature — Snake Monk",
        "oracle_text": "Whenever this creature attacks, choose one — Exile target creature card from a graveyard. When you do, create a 1/1 colorless Spirit creature token. Exile target noncreature card from a graveyard. When you do, put a +1/+1 counter on target creature you control.",
        "power": "1", "toughness": "1",
    },
    # March of the Machine battles converted to sagas — the Invasion family.
    # These actually transform via a "defeat the battle" mechanic, not chapter
    # completion, but the same back_face fallback lets them flip when SBA
    # fires the transform action.
    "invasion of zendikar": {
        "name": "Awakened Skyclave",
        "type_line": "Land Creature — Vampire Knight",
        "oracle_text": "Awakened Skyclave is also a land.\n{T}: Add {W} or {B}.",
        "power": "3", "toughness": "3",
    },
    "invasion of tarkir": {
        "name": "Defiant Thundermaw",
        "type_line": "Legendary Creature — Dragon",
        "oracle_text": "Flying, haste\nWhen this creature enters, it deals 5 damage to target creature an opponent controls.",
        "power": "5", "toughness": "5",
    },
    # Fall of the Thran is intentionally absent: it sacrifices at chapter III
    # (no transform) and the is_transforming detector in saga handling will
    # correctly skip the back-face lookup for it.
}

# Optional: state-based actions adapter for the side-by-side comparison
try:
    from rules.sba_adapter import compare_with_rules_sba as _sba_compare
    HAS_SBA_CHECKER = True
except ImportError:
    HAS_SBA_CHECKER = False
    _sba_compare = None

# Optional: replacement effects (for SBA-driven death events)
try:
    from rules.replacement import GameEvent, EventType
    HAS_REPLACEMENT_ENGINE = True
except ImportError:
    HAS_REPLACEMENT_ENGINE = False


def check_state_based_actions(rules, game: GameState) -> List[Dict]:
    """Check for state-based actions that need to happen.

    Delegates core checks (CR 704.5a-s) to rules/state_based_actions.py module.
    Validated via 1,398 side-by-side comparisons with 0 mismatches (Apr 5 batch).
    Game-specific checks (commander damage, token cleanup, equipment unattach)
    remain inline as they require direct game state access.

    Returns list of actions to take.
    """
    if game.ended:
        return []

    actions = []

    # === DELEGATED: Core SBA checks via rules module ===
    if HAS_SBA_CHECKER:
        try:
            from rules.sba_adapter import build_sba_state, convert_sba_results
            sba_state = build_sba_state(game)
            from rules.state_based_actions import StateBasedActionChecker
            checker = StateBasedActionChecker()
            results = checker.check(sba_state)
            delegated = convert_sba_results(results, game, rules)
            actions.extend(delegated)
            if results:
                summary = ', '.join(f"{r.sba_type.name}({len(r.affected_objects)})" for r in results[:5])
                print(f"[SBA-RULES] Delegated check found: {summary}")
        except Exception as e:
            print(f"[SBA-RULES] Delegation error, falling back to inline: {e}")
            # Fallback: run inline checks if rules module fails
            actions.extend(rules._check_sba_inline_fallback(game))

    else:
        # No rules module available — use inline checks
        actions.extend(rules._check_sba_inline_fallback(game))

    # === INLINE: Game-specific checks not in rules module ===
    for i, player in enumerate(game.players):
        # Commander damage (21+) — game-specific, not in CR 704.5
        cant_lose_source = rules._player_cant_lose(game, i)
        commander_damage = (player.commander_damage
                            if getattr(game, 'format', '').lower() in ('commander', 'edh')
                            else {})
        for source_key, damage in commander_damage.items():
            if damage >= 21:
                # Aug 1 batch-12: keys are commander NAMES now (CR 903.10a is
                # per-commander — the partner-deck finding). Legacy int /
                # digit-string keys from pre-fix saves still count (their
                # per-player semantics are frozen into the old bucket); map
                # them to a player name for the message when possible.
                if isinstance(source_key, int) or (
                        isinstance(source_key, str) and source_key.isdigit()):
                    _idx = int(source_key)
                    _src_name = (game.players[_idx].name
                                 if 0 <= _idx < len(game.players)
                                 else f'player {source_key}')
                else:
                    _src_name = str(source_key)
                if cant_lose_source:
                    print(f"[SBA-RULES] PLAYER_LOSES_COMMANDER_DAMAGE suppressed: {player.name} can't lose ({cant_lose_source}) — {damage} cmd damage from {_src_name}")
                else:
                    # May 7 audit: this inline check works but never surfaced in
                    # the [SBA-RULES] log because it bypasses the rules module
                    # (rules.Player has no per-source commander_damage). Tag it
                    # explicitly so auditors can grep for it and verify the
                    # loss condition fires even when zero-life doesn't.
                    print(f"[SBA-RULES] PLAYER_LOSES_COMMANDER_DAMAGE({player.name}): {damage} cmd damage from {_src_name}")
                    actions.append({
                        'type': 'player_loses',
                        'player_index': i,
                        'reason': f"received 21+ commander damage from {_src_name}"
                    })

        # 704.5d: Tokens in non-battlefield zones cease to exist (direct mutation)
        for zone_name in ['hand', 'graveyard', 'exile', 'library']:
            zone_list = getattr(player, zone_name, [])
            tokens_to_remove = [c for c in zone_list if getattr(c, 'is_token', False)]
            for token in tokens_to_remove:
                zone_list.remove(token)
                print(f"[TOKEN-SBA] {token.name} ceased to exist in {player.name}'s {zone_name}")

        # 704.5n: Equipment attached to non-creature/nonexistent (direct mutation)
        for card in list(player.battlefield):
            _eq_tl = getattr(card, 'type_line', '') or ''
            if 'equipment' not in _eq_tl.lower() or not card.attached_to:
                continue
            eq_result = game.find_card_global(card.attached_to)
            if not eq_result or eq_result[2] != Zone.BATTLEFIELD:
                print(f"[SBA-EQUIPMENT] {card.name} detached: target gone (CR 704.5n)")
                card.attached_to = None
            elif not eq_result[0].is_creature():
                _eq_target = eq_result[0]
                print(f"[SBA-EQUIPMENT] {card.name} detached from {_eq_target.name}: not creature (CR 704.5n)")
                card.attached_to = None
                if card.id in getattr(_eq_target, 'attachments', []):
                    _eq_target.attachments.remove(card.id)

    return actions


def check_sba_inline_fallback(rules, game: GameState) -> List[Dict]:
    """Inline SBA checks — fallback when rules module is unavailable."""
    actions = []
    for i, player in enumerate(game.players):
        cant_lose_source = rules._player_cant_lose(game, i)
        if player.life <= 0 and not cant_lose_source:
            actions.append({'type': 'player_loses', 'player_index': i, 'reason': 'life total is 0 or less'})
        if player.poison >= 10 and not cant_lose_source:
            actions.append({'type': 'player_loses', 'player_index': i, 'reason': 'has 10 or more poison counters'})
        for creature in player.creatures():
            eff_t = creature.get_effective_toughness(game)
            if eff_t <= 0:
                # 0 toughness — can't be saved by shield counters or indestructible
                actions.append({'type': 'creature_dies', 'card_id': creature.id, 'card_name': creature.name, 'player_index': i, 'reason': 'zero or less toughness'})
            elif creature.deathtouch_damage > 0 and not creature.has_keyword('Indestructible', game=game):
                # [SHIELD-COUNTER] If creature has shield counters, remove one instead of dying
                if creature.counters.get('shield', 0) > 0:
                    creature.counters['shield'] -= 1
                    creature.damage_marked = 0
                    creature.deathtouch_damage = 0
                    actions.append({'type': 'shield_removed', 'card_id': creature.id, 'card_name': creature.name, 'player_index': i, 'reason': 'shield counter removed instead of destruction (deathtouch)'})
                # [TOTEM-ARMOR] If creature has an Aura with totem armor, destroy the Aura instead
                elif rules._has_totem_armor(creature, player):
                    aura = rules._remove_totem_armor(creature, player, game)
                    creature.damage_marked = 0
                    creature.deathtouch_damage = 0
                    actions.append({'type': 'totem_armor', 'card_id': creature.id, 'card_name': creature.name, 'aura_name': aura.name if aura else '?', 'player_index': i, 'reason': f'totem armor ({aura.name if aura else "?"}) destroyed instead'})
                else:
                    actions.append({'type': 'creature_dies', 'card_id': creature.id, 'card_name': creature.name, 'player_index': i, 'reason': 'deathtouch damage'})
            elif creature.damage_marked >= eff_t and not creature.has_keyword('Indestructible', game=game):
                # [SHIELD-COUNTER] If creature has shield counters, remove one instead of dying
                if creature.counters.get('shield', 0) > 0:
                    creature.counters['shield'] -= 1
                    creature.damage_marked = 0
                    actions.append({'type': 'shield_removed', 'card_id': creature.id, 'card_name': creature.name, 'player_index': i, 'reason': 'shield counter removed instead of destruction (lethal damage)'})
                # [TOTEM-ARMOR] If creature has an Aura with totem armor, destroy the Aura instead
                elif rules._has_totem_armor(creature, player):
                    aura = rules._remove_totem_armor(creature, player, game)
                    creature.damage_marked = 0
                    actions.append({'type': 'totem_armor', 'card_id': creature.id, 'card_name': creature.name, 'aura_name': aura.name if aura else '?', 'player_index': i, 'reason': f'totem armor ({aura.name if aura else "?"}) destroyed instead'})
                else:
                    actions.append({'type': 'creature_dies', 'card_id': creature.id, 'card_name': creature.name, 'player_index': i, 'reason': 'lethal damage'})
        for card in list(player.battlefield):
            if card.is_planeswalker() and getattr(card, 'loyalty_counters', 0) <= 0:
                actions.append({'type': 'planeswalker_dies', 'card_id': card.id, 'card_name': card.name, 'player_index': i, 'reason': f'{card.name} has 0 loyalty'})
        for card in player.battlefield:
            plus, minus = card.counters.get('+1/+1', 0), card.counters.get('-1/-1', 0)
            if plus > 0 and minus > 0:
                actions.append({'type': 'counter_cancel', 'card_id': card.id, 'card_name': card.name, 'player_index': i, 'amount': min(plus, minus)})
        # [BATTLE] Battles with 0 defense counters go to graveyard (704.5t)
        for card in player.battlefield:
            if card.is_battle() and card.counters.get('defense', 0) <= 0:
                actions.append({'type': 'battle_defeated', 'card_id': card.id, 'card_name': card.name, 'player_index': i, 'reason': f'{card.name} has no defense counters'})
        for card in list(player.battlefield):
            if not card.is_enchantment() or 'Aura' not in (card.type_line or ''):
                continue
            if not card.attached_to:
                actions.append({'type': 'aura_invalid', 'card_id': card.id, 'card_name': card.name, 'player_index': i, 'reason': f'{card.name} not attached'})
            else:
                r = game.find_card_global(card.attached_to)
                if not r or r[2] != Zone.BATTLEFIELD:
                    actions.append({'type': 'aura_invalid', 'card_id': card.id, 'card_name': card.name, 'player_index': i, 'reason': f'{card.name} target gone'})
        legendary_by_name = {}
        for card in player.battlefield:
            if 'Legendary' in (getattr(card, 'type_line', '') or ''):
                legendary_by_name.setdefault(card.name, []).append(card)
        for name, legends in legendary_by_name.items():
            if len(legends) >= 2:
                for legend in legends[:-1]:
                    actions.append({'type': 'legend_rule', 'card_name': legend.name, 'card_id': getattr(legend, 'id', ''), 'player_index': i, 'reason': f'legend rule — duplicate "{name}"'})
    return actions


def _finalize_death_save_return(rules, game: GameState, player: Player,
                                card: Card, save_label: str) -> List[str]:
    """Shared cleanup when a death save (undying / persist) keeps a creature
    on the battlefield.

    June 10 audit (C6): the returned permanent is a NEW object (CR 702.92a /
    400.7) that was never declared as an attacker (CR 508.1a). The old code
    left `attacking` + game.attackers intact, so a Geralf's Messenger that
    died to first-strike damage "returned" and dealt regular-step combat
    damage, killing the blocker that had already beaten it. Also applies
    "enters tapped" and fires the card's OWN ETB — the watcher-scan helper
    (_handle_etb_triggers) only fires OTHER permanents' creature-enters
    triggers, so Geralf's "target opponent loses 2 life" never re-fired.

    Returns extra display messages.
    """
    msgs: List[str] = []
    # Pub/sub slice 2: the undying/persist return is a NEW battlefield entry
    # (CR 702.92a / 400.7) — one PERMANENT_ENTERED per physical entry.
    events.emit(events.PERMANENT_ENTERED, game, card=card,
                controller=player, via="death_save_return", rules=rules)
    # 1. Strip combat state — the new object is not attacking or blocking.
    was_attacking = bool(getattr(card, 'attacking', False))
    card.attacking = False
    card.attacking_player = None
    card.blocking = []
    card.blocked_by = []
    attackers = getattr(game, 'attackers', None)
    if attackers and card.id in attackers:
        attackers.remove(card.id)
    blockers = getattr(game, 'blockers', None)
    if isinstance(blockers, dict):
        blockers.pop(card.id, None)
        for _atk_id, _blk_list in blockers.items():
            if _blk_list and card.id in _blk_list:
                _blk_list.remove(card.id)
    if was_attacking:
        print(f"[{save_label}] {card.name} removed from combat — the returned "
              f"permanent is a new object (CR 508.1a)")
    # 2. "Enters the battlefield tapped" applies to the re-entry.
    oracle_l = (card.oracle_text or '').lower()
    if 'enters the battlefield tapped' in oracle_l or 'enters tapped' in oracle_l:
        card.tapped = True
        print(f"[{save_label}] {card.name} re-enters tapped")
    # 3. Fire the card's own ETB via the template library (sync, free).
    try:
        from rules.effect_templates import get_effect_library
        lib = get_effect_library()
        opp = next((p for p in game.players if p is not player), None)
        tmpl_actions, tmpl_desc = lib.resolve_etb(
            card.name, card.oracle_text or '', player.name,
            opp.name if opp else '')
        if tmpl_actions:
            for act in tmpl_actions:
                res = rules._execute_action_on_state(game, act)
                if res:
                    msgs.append(res)
            print(f"[{save_label}-ETB] Re-fired self-ETB for {card.name}: {tmpl_desc}")
    except Exception as etb_err:
        # Crash barrier: a bad template must not turn a death save into a
        # game crash. Visible in strict batches via maybe_reraise.
        print(f"[{save_label}-ETB] self-ETB re-fire failed for {card.name}: {etb_err}")
        from mtg.util import maybe_reraise
        maybe_reraise(etb_err)
    # 4. Run the creature-enters WATCHER scan for the re-entry (CR 603.6a —
    # each separate entry triggers "whenever a/another creature enters"
    # abilities afresh; Soul Warden was missing every undying/persist
    # return). July 20, pub/sub slice 2 follow-up: found while wiring the
    # PERMANENT_ENTERED parity recorder. Deliberately the NARROW scan, not
    # _handle_etb_triggers — that helper also re-resolves self-ETB observers,
    # which step 3 above already fired (double-fire risk). Same
    # is_creature(game) gate as the cast path (a devotion-gated god
    # re-entering below threshold isn't a creature, CR 603.2a).
    engine = getattr(rules, 'engine_ref', None)
    if engine is not None:
        try:
            from mtg.triggers import (_check_permanent_etb_watchers,
                                      _check_enchantment_etb_watchers)
            from mtg.helpers import format_trigger_line
            if card.is_creature(game):
                # Slice 2b (July 21): the creature watcher dispatch now runs
                # in the PERMANENT_ENTERED subscriber (fired by the emit at
                # the top of this function). Drain its display lines here —
                # the position the direct scan call used to occupy.
                from mtg.helpers import drain_pending_messages
                msgs.extend(drain_pending_messages(game))
            else:
                msgs.extend(_check_permanent_etb_watchers(
                    engine, game, player, card))
            if card.is_enchantment():
                # Slice 2b (2/2, July 21): constellation dispatch runs in
                # the PERMANENT_ENTERED subscriber (emit at the top of this
                # function). Drain in place.
                from mtg.helpers import drain_pending_messages as _drain_pm_e
                msgs.extend(_drain_pm_e(game))
        except Exception as scan_err:
            print(f"[{save_label}-ETB] watcher scan failed for {card.name}: {scan_err}")
            from mtg.util import maybe_reraise
            maybe_reraise(scan_err)
    return msgs


def process_state_based_actions(rules, game: GameState) -> List[str]:
    """Process all state-based actions. Returns list of messages.

    Also populates game._recently_died with (card, player) tuples for
    dies trigger processing by the caller.
    """
    messages = []
    needs_layers_recalc = False
    recently_died = []  # Collect (card, player) tuples for dies triggers

    # [CR 903.9] Post-action commander-zone redirect sweep. The primary
    # redirect happens inline in _execute_action_on_state, but many paths
    # (Path to Exile, Reality Shift, direct .exile.append, tuck effects,
    # bounce, library manipulation) bypass that hook. This sweep catches
    # commanders that ended up in exile / hand / library and moves them
    # to their owner's command zone. Autoplay always chooses redirect.
    if game.format in COMMAND_ZONE_FORMATS:
        for owner_idx, _p in enumerate(game.players):
            if not hasattr(_p, 'command_zone') or _p.command_zone is None:
                _p.command_zone = []
            for _zone_name, _zone in (('exile', _p.exile),
                                      ('hand', _p.hand),
                                      ('library', _p.library)):
                for _card in list(_zone):
                    if not getattr(_card, 'is_commander', False):
                        continue
                    # Only redirect if the card's owner is this player
                    # (owner_index identifies the original deck owner).
                    if getattr(_card, 'owner_index', owner_idx) != owner_idx:
                        continue
                    _zone.remove(_card)
                    _p.command_zone.append(_card)
                    msg = f"👑 {_card.name} (commander) returned to {_p.name}'s command zone from {_zone_name} (CR 903.9)"
                    messages.append(msg)
                    print(f"[CR-903.9] {msg}")

    # [REANIMATE-AURA] LTB check for Animate Dead / Dance of the Dead /
    # Necromancy: if the binding aura is no longer on any battlefield,
    # sacrifice the creature it brought back (CR 701.16 / oracle text).
    alive_aura_ids = set()
    for _plr in game.players:
        for _c in _plr.battlefield:
            if getattr(_c, '_bound_creature_id', None):
                alive_aura_ids.add(_c.id)
    for _plr in game.players:
        for _c in list(_plr.battlefield):
            bind_id = getattr(_c, '_reanimated_by_aura_id', None)
            if bind_id and bind_id not in alive_aura_ids:
                _restore_identity_on_battlefield_exit(_c)
                _plr.battlefield.remove(_c)
                _plr.graveyard.append(_c)
                _c._reanimated_by_aura_id = None
                messages.append(f"⚰️ **{_c.name}** is sacrificed (reanimating aura left play)")
                print(f"[REANIMATE-AURA] {_c.name} sacrificed — binding aura gone")

    # [LAYERS-PT] Recalculate P/T before SBA check so Humility/anthem changes apply
    game.recalculate_power_toughness()

    while True:
        # Don't process SBAs if game already ended
        if game.ended:
            break

        actions = rules.check_state_based_actions(game)
        if not actions:
            break

        # CR 104.3b — if multiple players lose simultaneously (the same SBA pass
        # sees them all losing), the game is a draw. Pre-scan for player_loses
        # actions before iterating, so we can detect the multi-loss case.
        # Dedupe by player_index: a single player can hit multiple loss
        # conditions in one SBA pass (0 life AND 21 commander damage), and
        # that's still one player losing, not a draw.
        loss_actions = [a for a in actions if a.get('type') == 'player_loses']
        unique_losers = {a['player_index']: a for a in loss_actions}
        if len(unique_losers) >= 2:
            losers = [game.players[idx].name for idx in unique_losers]
            reasons = ', '.join(f"{game.players[idx].name}: {a['reason']}" for idx, a in unique_losers.items())
            messages.append(f"🤝 **Draw!** {len(losers)} players lose simultaneously ({reasons})")
            game.ended = True
            game.winner = None  # Draw — no single winner
            game.loss_reason = f"simultaneous loss: {reasons}"
            break

        # July 31 batch-11 (cube reviewer): CR 704.3 — all SBAs found in ONE
        # check are performed simultaneously. The old order let list position
        # decide: a player_loses early in the batch broke out and discarded
        # the same batch's creature_dies zone changes (Blood Artist stayed on
        # the battlefield in the final snapshot, game_1532532179492536430).
        # Process the loss LAST so same-batch mutations land; the post-end
        # trigger gate (CR 104.2a) still suppresses their triggers.
        for action in sorted(actions, key=lambda a: a.get('type') == 'player_loses'):
            if action['type'] == 'player_loses':
                idx = action['player_index']
                player = game.players[idx]
                # Aug 7 batch audit (G3-2): trigger messages buffered by an
                # EARLIER action (a Mayhem Devil ping from an Altar
                # activation) were still sitting in _pending_messages and
                # flushed AFTER this loss line, so the transcript showed the
                # dead player's life "rising" post-loss
                # (game_1535060120164376726 — state was correct throughout,
                # pure ordering). Drain the buffer ahead of the terminal
                # line so pre-loss events display pre-loss.
                _pending = getattr(game, '_pending_messages', None)
                if _pending:
                    messages.extend(helpers.drain_pending_messages(game))
                messages.append(f"💀 **{player.name}** loses the game! ({action['reason']})")
                game.ended = True
                game.winner = 1 - idx
                break  # Stop processing actions, game is over

            elif action['type'] == 'shield_removed':
                # Shield counter was already removed in SBA detection
                card_name = action.get('card_name', '?')
                messages.append(f"🛡️ {card_name}: shield counter removed instead of being destroyed")
                print(f"[SHIELD-COUNTER] {card_name}: shield counter removed ({action['reason']})")

            elif action['type'] == 'totem_armor':
                # Totem armor Aura was already destroyed in SBA detection
                card_name = action.get('card_name', '?')
                aura_name = action.get('aura_name', '?')
                messages.append(f"🛡️ {card_name}: {aura_name} (totem armor) destroyed instead")
                print(f"[TOTEM-ARMOR] {card_name}: {aura_name} destroyed instead")
                needs_layers_recalc = True

            elif action['type'] == 'creature_dies':
                card_id = action['card_id']
                player = game.players[action['player_index']]
                card = player.find_card(card_id, Zone.BATTLEFIELD)
                if card:
                    # [UNDYING] If creature has undying and no +1/+1 counters, return with a +1/+1 counter
                    has_undying = card.has_keyword('Undying') or rules._permanent_grants_undying(game, card, player)
                    has_persist = card.has_keyword('Persist')
                    no_plus_counters = card.counters.get('+1/+1', 0) == 0
                    no_minus_counters = card.counters.get('-1/-1', 0) == 0
                    # 0-toughness deaths can't be saved by undying/persist (not "dying", state-based)
                    is_zero_toughness = action.get('reason', '') == 'zero or less toughness'

                    if has_undying and no_plus_counters and not is_zero_toughness:
                        # Undying: return to battlefield with a +1/+1 counter.
                        # Per CR 614.6 + 603.6c, the returning permanent is a NEW
                        # object — its ETB triggers (self-ETB + "whenever another
                        # creature enters" on other permanents) MUST fire.
                        card.damage_marked = 0
                        card.deathtouch_damage = 0
                        card.summoning_sick = True
                        card.counters['+1/+1'] = card.counters.get('+1/+1', 0) + 1
                        messages.append(f"♻️ {card.name} returns with undying (+1/+1 counter)!")
                        print(f"[UNDYING] {card.name} returned to battlefield with +1/+1 counter")
                        # May 24 Tier-2 audit fix: _handle_etb_triggers requires the
                        # GameEngine (not RulesEngine) because it calls
                        # `engine._check_creature_etb_triggers_sync` + `engine._queue_async_trigger`,
                        # both of which live on GameEngine. The previous bare `rules`
                        # arg silently threw AttributeError in the except block,
                        # dropping Blood Artist / Mikaeus chain triggers on every
                        # undying return (6+ games in May 24 batch).
                        engine_for_etb = getattr(rules, 'engine_ref', None)
                        if engine_for_etb is not None:
                            try:
                                from mtg.triggers import _handle_etb_triggers
                                etb_msgs = _handle_etb_triggers(engine_for_etb, game, player, card)
                                messages.extend(etb_msgs)
                            except Exception as etb_err:
                                print(f"[UNDYING-ETB] Failed to fire ETB for {card.name}: {etb_err}")
                        else:
                            print(f"[UNDYING-ETB] No engine_ref on rules — ETB triggers skipped for {card.name}")
                        # June 10 audit (C6): the return is a NEW object —
                        # strip combat state, apply enters-tapped, fire its
                        # OWN ETB (the watcher scan above doesn't).
                        messages.extend(_finalize_death_save_return(
                            rules, game, player, card, "UNDYING"))
                        needs_layers_recalc = True
                        continue  # Don't remove from battlefield

                    if has_persist and no_minus_counters and not is_zero_toughness:
                        # Persist: return to battlefield with a -1/-1 counter.
                        # Same CR 614.6 reasoning as undying — fire ETB triggers.
                        card.damage_marked = 0
                        card.deathtouch_damage = 0
                        card.summoning_sick = True
                        card.counters['-1/-1'] = card.counters.get('-1/-1', 0) + 1
                        messages.append(f"♻️ {card.name} returns with persist (-1/-1 counter)!")
                        print(f"[PERSIST] {card.name} returned to battlefield with -1/-1 counter")
                        # See undying fix above — same engine_ref unwrap.
                        engine_for_etb = getattr(rules, 'engine_ref', None)
                        if engine_for_etb is not None:
                            try:
                                from mtg.triggers import _handle_etb_triggers
                                etb_msgs = _handle_etb_triggers(engine_for_etb, game, player, card)
                                messages.extend(etb_msgs)
                            except Exception as etb_err:
                                print(f"[PERSIST-ETB] Failed to fire ETB for {card.name}: {etb_err}")
                        else:
                            print(f"[PERSIST-ETB] No engine_ref on rules — ETB triggers skipped for {card.name}")
                        # June 10 audit (C6): same new-object cleanup as undying.
                        messages.extend(_finalize_death_save_return(
                            rules, game, player, card, "PERSIST"))
                        needs_layers_recalc = True
                        continue  # Don't remove from battlefield

                    # [LAYERS] Unregister static effects before removal
                    game.unregister_static_effects(card)
                    _restore_identity_on_battlefield_exit(card)
                    player.battlefield.remove(card)

                    # Commanders can go to command zone instead of graveyard.
                    # June 10 audit (C7, CR 903.9a): the OWNER's command zone,
                    # not the battlefield-holder's — see command_zone_owner.
                    from mtg.helpers import commander_declines_graveyard_redirect
                    if (card.is_commander and game.format in COMMAND_ZONE_FORMATS
                            and commander_declines_graveyard_redirect(card)):
                        card.reset_battlefield_state()
                        owner_of(game, card, player).graveyard.append(card)
                        messages.append(f"☠️ {card.name} dies → stays in the "
                                        f"graveyard (declines the command-zone "
                                        f"redirect — escape available) ({action['reason']})")
                        print(f"[CR-903.9] {card.name} declines the redirect "
                              f"(escape) — graveyard")
                    elif card.is_commander and game.format in COMMAND_ZONE_FORMATS:
                        card.reset_battlefield_state()  # Clear damage/modifiers so recast starts clean
                        _zone_owner = command_zone_owner(game, card, player)
                        _zone_owner.command_zone.append(card)
                        if _zone_owner is not player:
                            messages.append(f"☠️ {card.name} dies → returns to {_zone_owner.name}'s command zone ({action['reason']})")
                        else:
                            messages.append(f"☠️ {card.name} dies → returns to command zone ({action['reason']})")
                    else:
                        # [REPLACEMENT] Check for death replacement (Rest in Peace → exile instead)
                        destination = "graveyard"
                        # Unearth (CR 702.83a) exiles it "if it would leave the
                        # battlefield" — otherwise the creature dies to the
                        # graveyard and can be unearthed again, the recursion
                        # the printed clause forbids.
                        from mtg.helpers import unearthed_leaves_to_exile
                        if unearthed_leaves_to_exile(card):
                            destination = "exile"
                            print(f"  [UNEARTH] {card.name} left the battlefield "
                                  f"— exiled instead of dying (CR 702.83a)")
                        if HAS_REPLACEMENT_ENGINE and game._replacement_engine and game._replacement_engine.effects:
                            event = GameEvent(
                                event_type=EventType.DEATH,
                                affected_object=getattr(card, 'id', ''),
                                affected_object_name=card.name,
                                affected_player=player.name,
                                from_zone="battlefield",
                                to_zone="graveyard",
                                # July 30: "nontoken creature" scoped
                                # redirects (Draugr Necromancer) filter on it.
                                is_token=bool(getattr(card, 'is_token', False)),
                            )
                            final = game._replacement_engine.process_event_sync(event)
                            if final.to_zone != "graveyard":
                                destination = final.to_zone
                                # July 30: Draugr-class "exile ... with an
                                # ice counter on it instead" — the effect
                                # threads the counter through the event.
                                _rc = getattr(final, 'redirect_counter', '')
                                if _rc and destination == "exile":
                                    card.counters[_rc] = card.counters.get(_rc, 0) + 1
                                    print(f"  [REPLACEMENT-APPLY] {card.name} "
                                          f"exiled with a{'n' if _rc[:1] in 'aeiou' else ''} "
                                          f"{_rc} counter")
                                # May 17 audit: only emit the redirect log AFTER
                                # we know we're actually honoring it below — see
                                # the destination switch. Previously the log
                                # always fired even when the destination wasn't
                                # implemented (e.g. "battlefield" used to fall
                                # through to the graveyard else-branch).
                        # Friendlier SBA death reasons — the raw rules text
                        # "zero or less toughness" is opaque to players.
                        # Phantasmal Image with no target, Omarthis cast
                        # with X=0, and other 0/0 creatures without counters
                        # are the usual suspects.
                        friendly_reason = action['reason']
                        if friendly_reason == 'zero or less toughness':
                            x_paid = getattr(card, '_mana_paid', None)
                            if 'image' in card.name.lower() or (card.oracle_text and 'you may have' in (card.oracle_text or '').lower() and 'enters as a copy' in (card.oracle_text or '').lower()):
                                friendly_reason = "0 toughness — no legal copy target"
                            elif x_paid is not None and x_paid == 0:
                                friendly_reason = "0 toughness — cast with X=0"
                            else:
                                friendly_reason = "0 toughness"
                        if destination == "exile":
                            player.exile.append(card)
                            messages.append(f"☠️ {card.name} dies → exiled ({friendly_reason})")
                            print(f"  [REPLACEMENT-APPLY] SBA death redirected to exile")
                        elif destination == "battlefield":
                            # Replacement engine asked for return-to-battlefield
                            # (Mikaeus-style undying via replacement, not the
                            # SBA hardcoded check at the top of this branch).
                            # Reset combat state and re-add to battlefield with
                            # a +1/+1 counter (undying semantics).
                            card.damage_marked = 0
                            card.deathtouch_damage = 0
                            card.summoning_sick = True
                            card.counters['+1/+1'] = card.counters.get('+1/+1', 0) + 1
                            player.battlefield.append(card)
                            messages.append(f"♻️ {card.name} returns with undying (+1/+1 counter)!")
                            print(f"  [REPLACEMENT-APPLY] SBA death redirected to battlefield ({friendly_reason})")
                            needs_layers_recalc = True
                        else:
                            owner_of(game, card, player).graveyard.append(card)
                            messages.append(f"☠️ {card.name} dies ({friendly_reason})")

                    # [DIES-TRIGGER] Track dead creatures for trigger processing.
                    # May 20 audit (CRITICAL #4): only count this as a "death"
                    # if the creature actually went to a graveyard. CR 700.4
                    # defines "dies" as "is put into a graveyard from the
                    # battlefield". If Rest in Peace / Leyline of the Void
                    # redirected to exile, OR if the commander went to the
                    # command zone, the creature did NOT die — dies-triggers
                    # (Blood Artist, Zulaport, Bastion of Remembrance) must
                    # not fire. game_1506623303748550696:381-384 had Korvold
                    # exiled by Rest in Peace yet Bastion's drain still fired.
                    # June 10 deep-dive: CR 903.9b (2020 rules change) — a
                    # commander DIES into the graveyard (dies-triggers fire),
                    # and the owner then moves it to the command zone as a
                    # SUBSEQUENT state-based action. The old gate treated the
                    # zone choice as a replacement (pre-2020 rules) and
                    # suppressed Blood Artist-class watchers on every
                    # commander death. RIP/Leyline exile redirects remain
                    # correctly suppressed (true replacement — never dies).
                    actually_died = (
                        card.is_creature()
                        and (card in player.graveyard
                             or any(card in p.command_zone for p in game.players))
                    )
                    if actually_died:
                        recently_died.append((card, player))
                    else:
                        print(f"[DIES-TRIGGER-SKIPPED] {card.name} did not enter graveyard "
                              f"(exile redirect / undying return) — "
                              f"dies-triggers suppressed per CR 700.4")

                    # [AURA-DEATH-TRIGGER] Check for "when enchanted creature dies" on
                    # auras attached to this creature (Pattern of Rebirth, Journey to Eternity).
                    # Must be done NOW while the auras are still on the battlefield — the
                    # subsequent aura_invalid SBA will move them to GY before normal dies
                    # trigger processing runs. Gated on actually_died for the same CR 700.4
                    # reason — auras' "when enchanted creature dies" doesn't fire if the
                    # creature was exiled instead of dying.
                    if actually_died:
                        for p in game.players:
                            for aura in list(p.battlefield):
                                if (getattr(aura, 'attached_to', None) == card.id
                                        and aura.oracle_text
                                        and 'when enchanted creature dies' in aura.oracle_text.lower()):
                                    if not hasattr(game, '_queued_aura_death_triggers'):
                                        game._queued_aura_death_triggers = []
                                    game._queued_aura_death_triggers.append(
                                        (aura, p, card, player)
                                    )
                                    print(f"[AURA-DEATH-TRIGGER] Queued {aura.name} trigger (enchanted {card.name} died)")

                    # [LTB-TRIGGER] Check for leaves-the-battlefield triggers
                    if hasattr(rules, 'engine_ref') and rules.engine_ref:
                        ltb_msgs = rules.engine_ref._check_ltb_triggers_sync(game, card, player, "graveyard")
                        messages.extend(ltb_msgs)

                    rules.game_log.append(f"{card.name} died: {action['reason']}")
                    needs_layers_recalc = True

            elif action['type'] == 'legend_rule':
                card_id = action['card_id']
                player = game.players[action['player_index']]
                card = player.find_card(card_id, Zone.BATTLEFIELD)
                if card:
                    # [LAYERS] Unregister static effects before removal
                    game.unregister_static_effects(card)
                    _restore_identity_on_battlefield_exit(card)
                    player.battlefield.remove(card)

                    # Commanders can go to command zone instead of graveyard.
                    # Player-facing message: drop the [SBA-LEGEND] tag and the
                    # raw "(0 controls multiple X)" reason — both are internal
                    # diagnostics that confused players in the Apr 28 audit.
                    if card.is_commander and game.format in COMMAND_ZONE_FORMATS:
                        card.reset_battlefield_state()
                        # CR 903.9a (June 10, C7): owner's zone, not controller's.
                        _zone_owner = command_zone_owner(game, card, player)
                        _zone_owner.command_zone.append(card)
                        messages.append(f"👑 {_zone_owner.name}'s **{card.name}** → command zone (legend rule)")
                    else:
                        owner_of(game, card, player).graveyard.append(card)
                        messages.append(f"👑 {player.name}'s **{card.name}** → graveyard (legend rule)")
                    print(f"[SBA-LEGEND] {card.name} → "
                          f"{'command zone' if card.is_commander else 'graveyard'} "
                          f"({action['reason']})")

                    # Track dies triggers for creatures
                    if card.is_creature():
                        recently_died.append((card, player))

                    # [LTB-TRIGGER] Check for leaves-the-battlefield triggers
                    if hasattr(rules, 'engine_ref') and rules.engine_ref:
                        ltb_msgs = rules.engine_ref._check_ltb_triggers_sync(game, card, player, "graveyard")
                        messages.extend(ltb_msgs)

                    rules.game_log.append(f"{card.name} legend-ruled: {action['reason']}")
                    needs_layers_recalc = True

            elif action['type'] == 'planeswalker_dies':
                card_id = action['card_id']
                player = game.players[action['player_index']]
                card = player.find_card(card_id, Zone.BATTLEFIELD)
                if card:
                    game.unregister_static_effects(card)
                    _restore_identity_on_battlefield_exit(card)
                    player.battlefield.remove(card)
                    if card.is_commander and game.format in COMMAND_ZONE_FORMATS:
                        card.reset_battlefield_state()
                        # CR 903.9a (June 10, C7): owner's zone, not controller's.
                        _zone_owner = command_zone_owner(game, card, player)
                        _zone_owner.command_zone.append(card)
                        messages.append(f"💀 {card.name} goes to command zone (0 loyalty)")
                    else:
                        owner_of(game, card, player).graveyard.append(card)
                        messages.append(f"💀 {card.name} goes to graveyard (0 loyalty)")
                    # [LTB-TRIGGER] Check for leaves-the-battlefield triggers
                    if hasattr(rules, 'engine_ref') and rules.engine_ref:
                        ltb_msgs = rules.engine_ref._check_ltb_triggers_sync(game, card, player, "graveyard")
                        messages.extend(ltb_msgs)
                    rules.game_log.append(f"{card.name} died: {action['reason']}")
                    needs_layers_recalc = True

            elif action['type'] == 'battle_defeated':
                card_id = action['card_id']
                player = game.players[action['player_index']]
                card = player.find_card(card_id, Zone.BATTLEFIELD)
                if card:
                    game.unregister_static_effects(card)
                    _restore_identity_on_battlefield_exit(card)
                    player.battlefield.remove(card)
                    owner_of(game, card, player).graveyard.append(card)
                    messages.append(f"⚔️ {card.name} defeated! (no defense counters remaining)")
                    # [LTB-TRIGGER] Battles may have LTB triggers
                    if hasattr(rules, 'engine_ref') and rules.engine_ref:
                        ltb_msgs = rules.engine_ref._check_ltb_triggers_sync(game, card, player, "graveyard")
                        messages.extend(ltb_msgs)
                    rules.game_log.append(f"{card.name} defeated: {action['reason']}")
                    needs_layers_recalc = True
                    print(f"[BATTLE] {card.name} defeated (0 defense counters)")

            elif action['type'] == 'counter_cancel':
                card_id = action['card_id']
                amount = action.get('amount', 0)
                player = game.players[action['player_index']]
                for c in player.battlefield:
                    if c.id == card_id:
                        c.counters['+1/+1'] = max(0, c.counters.get('+1/+1', 0) - amount)
                        c.counters['-1/-1'] = max(0, c.counters.get('-1/-1', 0) - amount)
                        messages.append(f"🔄 {amount} +1/+1 and -1/-1 counters cancel on {c.name}")
                        needs_layers_recalc = True
                        break

            elif action['type'] == 'aura_invalid':
                card_id = action['card_id']
                player = game.players[action['player_index']]
                card = player.find_card(card_id, Zone.BATTLEFIELD)
                if card:
                    game.unregister_static_effects(card)
                    _restore_identity_on_battlefield_exit(card)
                    player.battlefield.remove(card)
                    owner_of(game, card, player).graveyard.append(card)
                    messages.append(f"💫 {card.name} goes to graveyard ({action['reason']})")
                    if hasattr(rules, 'engine_ref') and rules.engine_ref:
                        ltb_msgs = rules.engine_ref._check_ltb_triggers_sync(game, card, player, "graveyard")
                        messages.extend(ltb_msgs)
                    rules.game_log.append(f"{card.name} aura SBA: {action['reason']}")
                    needs_layers_recalc = True

            elif action['type'] == 'world_rule':
                # 704.5k: If 2+ World permanents, all but newest go to graveyard
                card_id = action['card_id']
                player = game.players[action['player_index']]
                card = player.find_card(card_id, Zone.BATTLEFIELD)
                if card:
                    game.unregister_static_effects(card)
                    _restore_identity_on_battlefield_exit(card)
                    player.battlefield.remove(card)
                    owner_of(game, card, player).graveyard.append(card)
                    messages.append(f"🌍 {card.name} goes to graveyard (World rule — CR 704.5k)")
                    if hasattr(rules, 'engine_ref') and rules.engine_ref:
                        ltb_msgs = rules.engine_ref._check_ltb_triggers_sync(game, card, player, "graveyard")
                        messages.extend(ltb_msgs)
                    rules.game_log.append(f"{card.name} world rule: {action['reason']}")
                    needs_layers_recalc = True
                    print(f"[SBA-WORLD] {card.name} destroyed via World rule (704.5k)")

            elif action['type'] == 'saga_complete':
                # 704.5s: Saga with lore counters >= final chapter is sacrificed.
                # EXCEPTION: transforming sagas (DFC like The Restoration of Eiganjo)
                # whose final chapter says "exile this Saga, then return it to the
                # battlefield transformed" — the chapter effect IS the transform,
                # so the saga shouldn't ALSO be sacrificed. CR 715.4 / 715.3.
                card_id = action['card_id']
                player = game.players[action['player_index']]
                card = player.find_card(card_id, Zone.BATTLEFIELD)
                if card:
                    oracle_lower = (card.oracle_text or '').lower()
                    is_transforming = (
                        'exile this saga' in oracle_lower
                        and ('return it to the battlefield transformed' in oracle_lower
                             or 'transformed under' in oracle_lower)
                    )
                    if is_transforming:
                        # Move to exile, then return transformed (DFC flip).
                        # We approximate transformation by toggling card._transformed
                        # and switching name/type/oracle to the back face (if present).
                        game.unregister_static_effects(card)
                        _restore_identity_on_battlefield_exit(card)
                        player.battlefield.remove(card)
                        player.exile.append(card)
                        # Try to flip the DFC face
                        back_face = getattr(card, 'back_face', None) or getattr(card, 'transform_to', None)
                        if not back_face and getattr(card, 'back_face_name', ''):
                            back_face = {
                                'name': card.back_face_name,
                                'type_line': card.back_face_type_line,
                                'oracle_text': card.back_face_oracle_text,
                                'power': card.back_face_power,
                                'toughness': card.back_face_toughness,
                            }
                        # Scryfall card data routinely loads the front face only
                        # for sagas. Fall back to a small lookup table of common
                        # transforming sagas so they actually come back instead
                        # of staying in exile (Restoration of Eiganjo was
                        # logging "no back_face attribute available" and going
                        # silently to exile in audit batches).
                        if not back_face:
                            back_face = _TRANSFORMING_SAGA_BACK_FACES.get(
                                (card.name or '').lower()
                            )
                        if back_face:
                            player.exile.remove(card)
                            # Apply back-face attributes (name, type, oracle, P/T)
                            if isinstance(back_face, dict):
                                for attr in ('name', 'type_line', 'oracle_text', 'power', 'toughness'):
                                    if attr in back_face:
                                        setattr(card, attr, back_face[attr])
                            card._transformed = True
                            card.tapped = False
                            card.entered_this_turn = True
                            # June 10 deep-dive (B5b, CR 302.6): the flipped
                            # face is a NEW battlefield entry — without this,
                            # Kirin-Touched Orochi attacked the same turn it
                            # transformed (no haste on the card).
                            card.summoning_sick = True
                            # Reset saga-specific state
                            if hasattr(card, 'counters') and card.counters:
                                card.counters.pop('lore', None)
                            player.battlefield.append(card)
                            messages.append(f"📖 {card.name} transforms (chapter complete)")
                            print(f"[SAGA-TRANSFORM] {card.name} exiled and returned transformed")
                        else:
                            # No back-face data — leave in exile (chapter effect already
                            # ran via the chapter-resolution path)
                            messages.append(f"📖 {card.name} exiled (final chapter — no back-face data)")
                            print(f"[SAGA-TRANSFORM] {card.name} exiled, no back_face attribute available")
                        needs_layers_recalc = True
                    else:
                        game.unregister_static_effects(card)
                        player.battlefield.remove(card)
                        owner_of(game, card, player).graveyard.append(card)
                        lore = card.counters.get('lore', 0) if hasattr(card, 'counters') and card.counters else '?'
                        messages.append(f"📖 {card.name} sacrificed (final chapter reached, lore={lore})")
                        if hasattr(rules, 'engine_ref') and rules.engine_ref:
                            ltb_msgs = rules.engine_ref._check_ltb_triggers_sync(game, card, player, "graveyard")
                            messages.extend(ltb_msgs)
                        rules.game_log.append(f"{card.name} saga SBA: sacrificed at final chapter")
                        needs_layers_recalc = True
                        print(f"[SAGA] {card.name} sacrificed via SBA (704.5s)")

            elif action['type'] == 'equipment_invalid':
                # 704.5n: Equipment attached to an illegal permanent (target
                # zoned out, target is no longer a creature via Humility/type
                # loss, target was a token that ceased to exist, target phased
                # out) becomes unattached. It remains on the battlefield.
                # Wires the delegated SBA result — without this,
                # EQUIPMENT_INVALID was detected every cycle (130x in game
                # 1494783305843736588) but never acted on, because the inline
                # scan at ~line 3459 only covers the subset where
                # find_card_global and the rules-module checker agree.
                card_id = action['card_id']
                player = game.players[action['player_index']]
                card = player.find_card(card_id, Zone.BATTLEFIELD)
                if card and card.attached_to:
                    former_target_id = card.attached_to
                    # Clean up the attachee's back-reference if it still exists
                    former = game.find_card_global(former_target_id)
                    if former and card.id in getattr(former[0], 'attachments', []):
                        former[0].attachments.remove(card.id)
                    card.attached_to = None
                    messages.append(f"{card.name} falls off ({action.get('reason', 'invalid attachment')})")
                    print(f"[SBA-UNATTACH] {card.name} unattached via delegated SBA ({action.get('reason', 'invalid target')})")
                    needs_layers_recalc = True

        # Break outer loop if game ended
        if game.ended:
            break

    # [LAYERS] Recalculate granted keywords and P/T if anything left the battlefield
    if needs_layers_recalc:
        game.recalculate_granted_keywords()
        game.recalculate_power_toughness()

    # [AURA-DEATH-TRIGGER] Fire "when enchanted creature dies" triggers collected above.
    # These fire AFTER all creature_dies/aura_invalid SBAs so the game state is clean,
    # but we still have the (aura, controller, dying_card, dying_player) tuples.
    queued_aura_triggers = getattr(game, '_queued_aura_death_triggers', [])
    if queued_aura_triggers:
        game._queued_aura_death_triggers = []
        for aura_card, aura_player, dying_card, dying_player in queued_aura_triggers:
            handled = False
            aura_name_lower = aura_card.name.lower()

            # Pattern of Rebirth: controller may search library for a creature → put on battlefield
            if aura_name_lower == "pattern of rebirth":
                # Find the best creature in the controller's library
                library = dying_player.library
                best = None
                for c in library:
                    if c.is_creature():
                        if best is None or (c.cmc or 0) > (best.cmc or 0):
                            best = c
                if best:
                    library.remove(best)
                    random.shuffle(library)  # Shuffle after search
                    best.summoning_sick = True
                    best.entered_this_turn = True
                    dying_player.battlefield.append(best)
                    messages.append(f"🔮 Pattern of Rebirth: {dying_player.name} searches and puts **{best.name}** onto the battlefield!")
                    print(f"[AURA-DEATH-TRIGGER] Pattern of Rebirth: put {best.name} on battlefield for {dying_player.name}")
                    # July 29 batch audit: this direct append skipped the
                    # whole entry funnel — no self-ETB (a reanimated
                    # Craterhoof pumped nothing), no watcher scan, no
                    # PERMANENT_ENTERED emit. Same class as the July 28
                    # create_copy_token fix; the undying/persist returns a
                    # few hundred lines up have used the funnel all along.
                    from mtg.actions import _fire_noncast_battlefield_entry
                    messages.extend(_fire_noncast_battlefield_entry(
                        rules, game, dying_player, best))
                else:
                    messages.append(f"🔮 Pattern of Rebirth: {dying_player.name} has no creatures in library to find")
                handled = True

            # Journey to Eternity: return enchanted creature to battlefield, transform aura
            elif aura_name_lower == "journey to eternity":
                # Return the dying creature to the battlefield
                if dying_card in dying_player.graveyard:
                    dying_player.graveyard.remove(dying_card)
                dying_card.damage_marked = 0
                dying_card.deathtouch_damage = 0
                dying_card.summoning_sick = True
                dying_card.entered_this_turn = True
                dying_player.battlefield.append(dying_card)
                messages.append(f"🔮 Journey to Eternity: {dying_player.name} returns **{dying_card.name}** to the battlefield!")
                # Transform Journey to Eternity → Atzal, Cave of Eternity (land)
                # In autoplay, approximate by putting the land version in play
                if aura_card in aura_player.graveyard:
                    aura_player.graveyard.remove(aura_card)
                elif aura_card in aura_player.battlefield:
                    aura_player.battlefield.remove(aura_card)
                aura_card.name = "Atzal, Cave of Eternity"
                aura_card.type_line = "Legendary Land"
                aura_card.oracle_text = "{T}: Add one mana of any color. {3}{B}{G}, {T}: Return target creature card from your graveyard to the battlefield."
                aura_card.attached_to = None
                aura_card.tapped = False
                aura_player.battlefield.append(aura_card)
                messages.append(f"🔮 Journey to Eternity transforms into **Atzal, Cave of Eternity**!")
                print(f"[AURA-DEATH-TRIGGER] Journey to Eternity: returned {dying_card.name}, transformed to Atzal")
                handled = True

            # Draconic Destiny (and any aura whose dies-trigger reads "return
            # this [card] to its owner's hand"): bounce the aura to its owner's hand.
            aura_oracle = (aura_card.oracle_text or '').lower()
            if not handled and 'when enchanted creature dies' in aura_oracle and \
               "return this" in aura_oracle and "owner's hand" in aura_oracle:
                # The aura should already be in the graveyard (aura_invalid SBA).
                # Move it to its owner's hand.
                moved = False
                for owner in game.players:
                    if aura_card in owner.graveyard:
                        owner.graveyard.remove(aura_card)
                        # Reset battlefield-only state before moving to hand
                        if hasattr(aura_card, 'reset_battlefield_state'):
                            aura_card.reset_battlefield_state()
                        aura_card.attached_to = None
                        owner.hand.append(aura_card)
                        messages.append(f"🔁 {aura_card.name} returns to {owner.name}'s hand (enchanted creature died)")
                        print(f"[AURA-DEATH-TRIGGER] {aura_card.name} returned to {owner.name}'s hand")
                        moved = True
                        break
                if not moved:
                    print(f"[AURA-DEATH-TRIGGER] {aura_card.name}: return-to-hand trigger fired but aura not found in any graveyard")
                handled = True

            if not handled:
                messages.append(f"📜 {aura_card.name}: triggered (enchanted creature died) — use `!resolve` to handle")
                print(f"[AURA-DEATH-TRIGGER-UNHANDLED] {aura_card.name} trigger not handled")

    # [EXPERIENCE-COUNTERS] July 29 batch audit: the Meren/Ezuri increment
    # MOVED to the CREATURE_DIED bus subscriber
    # (mtg/triggers.py:_accumulate_death_subscriber). Living here it only saw
    # SBA-detected deaths — sacrifice-as-cost deaths (Viscera Seer, Altar of
    # Dementia, Phyrexian Tower) bypass this function entirely, so Meren
    # never gained XP from the most common way players feed her. The
    # queue_deaths call below emits CREATURE_DIED for every death in this
    # batch, so the subscriber covers the SBA class too — exactly once.

    # Store recently died creatures on game state for trigger processing by caller.
    # EXTEND not REPLACE — destroy_all_creatures and other action handlers may have
    # already queued deaths in _recently_died; overwriting would silently drop their
    # dies-triggers (Bastion of Remembrance, Grave Pact, Blood Artist).
    if not hasattr(game, '_recently_died') or game._recently_died is None:
        game._recently_died = []
    from mtg.triggers import queue_deaths
    queue_deaths(game, recently_died)

    return messages

# =========================================================================
# PHASE MANAGEMENT
# =========================================================================
