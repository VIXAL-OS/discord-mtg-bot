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

import re
from typing import Any, Dict, List, Optional, Tuple

from mtg.constants import Phase, Zone, COMMAND_ZONE_FORMATS
from mtg.models import Card, Player, GameState
from mtg import events

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
    from rules.effect_templates import (get_effect_library, build_game_context,
                                        strip_activated_ability_lines)
    HAS_EFFECT_TEMPLATES = True
except ImportError:
    HAS_EFFECT_TEMPLATES = False


# Aug 7 confirmation-batch audit (C-1): the self-trigger scan's combat-damage
# phrase, negation-aware — "NONcombat damage to an opponent" (Solphim's
# replacement wording) CONTAINS the bare substring, the substring family's
# 8th instance. Module-level so the pin and both gate sites share ONE
# predicate (the mirrored-predicate rule).
COMBAT_DAMAGE_SELF_PHRASE = re.compile(
    r'(?<!non)combat damage to (?:a player|an opponent)')


def drain_combat_damage_triggers(rules, game: GameState,
                                 messages: List[str]) -> None:
    """Resolve the triggers that watch combat damage, then clear the
    accumulators. Appends player-visible lines to `messages` in place.

    CALLED ONCE PER COMBAT DAMAGE STEP (Aug 11 audit, reviewer C).
    CR 510.4 divides the damage step in two when first strike is
    involved, and normal trigger processing happens after EACH step --
    so a first-striker's own 'deals combat damage' trigger must resolve
    BEFORE the regular step computes its damage. This drain used to run
    once, after both steps, off the accumulated list: Drana, Liberator
    of Malakir hit in the FS step and her '+1/+1 counter on each
    attacking creature you control' landed only after Phyrexian Negator
    and Crypt Ghast had already dealt their UNCOUNTERED base power
    (game_1536545838891794432, reproduced across two separate combats).

    Calling it twice cannot double-fire: both accumulators are cleared
    at the end of this function, so the post-regular call sees only the
    entries the regular step appended. The per-step model is already the
    codebase's own -- `_equip_charge_fired_ids` is reset at the regular
    step so Jitte's charge counters fire once per STEP, not per combat.
    """
    # Fire combat damage triggers (Ancient Bronze Dragon, Quartzwood Crasher, etc.)
    # July 24 batch-6 anomaly (reviewer S2 #5): the SBA call above can END the
    # game (PLAYER_LOSES_ZERO_LIFE) — Brago's combat-damage trigger then ran
    # a full mass-flicker (library search, draws, Altar mills) ~20 lines after
    # the loss fired (game_1529988360263827656). CR 104.2a: once a player has
    # lost, no further game actions occur.
    combat_damage_dealt = getattr(game, '_combat_damage_to_player', [])
    if combat_damage_dealt and HAS_EFFECT_TEMPLATES and not game.ended:
        for entry in combat_damage_dealt:
            attacker, attacker_owner, damage_amount = entry[:3]
            damaged_player = (entry[3] if len(entry) > 3 else
                              game.default_opponent_for(attacker_owner))
            if damaged_player is None:
                continue
            # Slice 5b (July 31, 2026): the old name-gated Ohran Frostfang
            # block generalized to the whole battlefield-watcher family —
            # "Whenever a [qualifier] creature(s)/[subtype(s)] you control
            # deal(s) combat damage to a player, <effect>". Tovolar's "a Wolf
            # or Werewolf you control" now draws on EVERY wolf connect (his
            # own-connect template was the only coverage before). Draw
            # effects resolve inline; anything else queues to the Tier-3
            # audit-trail with [COMBAT-WATCHER-UNHANDLED] (templates are the
            # fix path, per the F2 design).
            for watcher in list(attacker_owner.battlefield):
                w_scan = strip_activated_ability_lines(watcher.oracle_text or '')
                # "a/an <qual>" only — "one or more ... deal" fires once per
                # COMBAT (a batch trigger), and this loop runs per dealer, so
                # matching it here would over-fire. Left to templates.
                _wm = re.search(
                    r'whenever (?:a|an) ([a-z\'\- ]+?) you control '
                    r'deals? combat damage to a player,\s*([^\n.]+)',
                    w_scan.lower())
                if not _wm:
                    continue
                _qual, _weffect = _wm.group(1).strip(), _wm.group(2).strip()
                # Qualifier match: "creature(s)" is unconditional; otherwise
                # each " or "-alternative must appear in the dealer's type
                # line (Tovolar: "wolf or werewolf" vs a Human Werewolf).
                if _qual not in ('creature', 'creatures'):
                    _dealer_types = (getattr(attacker, 'type_line', '') or '').lower()
                    _alts = [a.strip().rstrip('s') for a in _qual.split(' or ')]
                    if not any(a and a in _dealer_types for a in _alts):
                        continue
                if re.match(r'^draw (a|one) cards?', _weffect):
                    msg = rules._execute_action_on_state(game, {
                        "action": "draw_cards", "player": attacker_owner.name,
                        "amount": 1, "_source_card_name": watcher.name,
                    })
                    if msg:
                        messages.append(f"🐍 {watcher.name}: {msg}")
                    print(f"[COMBAT-WATCHER] {watcher.name}: draw 1 "
                          f"({attacker.name} connected)")
                else:
                    print(f"[COMBAT-WATCHER-UNHANDLED] {watcher.name}: "
                          f"{_weffect[:100]}")
                    from mtg.triggers import queue_unhandled_combat_damage
                    queue_unhandled_combat_damage(
                        game, watcher, attacker_owner, damage_amount)
            # July 20 audit (Worldslayer): "Whenever EQUIPPED creature deals
            # combat damage to a player" lives on the EQUIPMENT — the
            # attacker-oracle scan below never saw it (three hits while
            # equipped, zero wipes, game_1526071467035459665). Scan the
            # attacker's attachments and resolve attack-templates keyed on
            # the equipment's name.
            for _att_id in list(getattr(attacker, 'attachments', []) or []):
                _att = next((c for c in attacker_owner.battlefield
                             if c.id == _att_id), None)
                if _att is None or not _att.oracle_text:
                    continue
                if ('equipped creature deals combat damage to a player'
                        not in _att.oracle_text.lower()):
                    continue
                try:
                    _opp = damaged_player
                    _ctx = build_game_context(game, attacker_owner, _opp,
                                              card=_att,
                                              attacking_creature=attacker)
                    _ctx['damage_dealt'] = damage_amount
                    _lib = get_effect_library()
                    _actions, _explanation = _lib.resolve_attack_trigger(
                        trigger_card_name=_att.name,
                        trigger_oracle=_att.oracle_text,
                        attacking_creature_name=attacker.name,
                        attacking_creature_power=damage_amount,
                        controller=attacker_owner.name,
                        opponent=_opp.name,
                        game_context=_ctx,
                    )
                    if _actions and any(a.get("action") != "no_action"
                                        for a in _actions):
                        for _action in _actions:
                            if _action.get("action") == "no_action":
                                continue
                            _msg = rules._execute_action_on_state(game, _action)
                            if _msg:
                                messages.append(f"💥 {_att.name} trigger: {_msg}")
                        print(f"[COMBAT-TRIGGER] {_att.name} (equipment on "
                              f"{attacker.name}): {_explanation}")
                    elif not _equipment_charge_claims(_att):
                        # Aug 10 audit: with no template this branch used to
                        # fall through with NO else — no queue, no tag — so
                        # the whole untemplated equipment combat-damage class
                        # vanished without a trace. Sword of Light and Shadow
                        # connected three times in
                        # game_1536023936116981932 and its 3 life + graveyard
                        # return never happened, invisibly: the sibling
                        # watcher loop twelve lines up has printed
                        # [COMBAT-WATCHER-UNHANDLED] and queued since July.
                        # The silence was worse than the drop — no audit grep
                        # could find it.
                        #
                        # Umezawa's Jitte is excluded because its charge
                        # counters are ALREADY applied by
                        # fire_equipped_combat_damage_counters below; queueing
                        # it would double-fire and burn a Tier-3 call.
                        from mtg.triggers import queue_unhandled_combat_damage
                        queue_unhandled_combat_damage(
                            game, _att, attacker_owner, damage_amount)
                except Exception as e:
                    # Crash barrier mirroring the sibling attacker-loop catch;
                    # visible in strict batches via maybe_reraise.
                    print(f"[COMBAT-TRIGGER] Error for equipment {_att.name}: {e}")
                    from mtg.util import maybe_reraise
                    maybe_reraise(e)

            if not attacker.oracle_text:
                continue
            # July 31 batch-10 audit: strip activated-ability lines before
            # detection. Ascendant Spirit's "{S}{S}{S}{S}: ... it gains
            # 'Whenever this creature deals combat damage to a player, draw a
            # card.'" is an ACTIVATED ability whose quoted grant tripped this
            # scan on every connect (8 queued, all Tier-3 refused, batch
            # 15324). The printed-text scan can't know whether the grant is
            # live (that's Layer-6 state), and scanning the quoted text
            # fabricates a trigger the creature may not have. Same class as
            # the July 21 resolve_etb strip (Glen Elendra).
            oracle_lower = strip_activated_ability_lines(attacker.oracle_text).lower()
            # Aug 7 confirmation-batch audit (C-1, CRITICAL): the bare
            # substring test matched Solphim, Mayhem Dominus — "NONcombat
            # damage to an opponent" CONTAINS "combat damage to an opponent"
            # (the substring family's 8th instance). Its REPLACEMENT effect
            # was routed into this self-trigger scan, and Tier 3 then
            # fabricated a nonexistent "Mayhem Devil" source and dealt 40
            # invented damage that illegitimately ended
            # game_1535236954705240105. The (?<!non) lookbehind rejects the
            # "noncombat" spelling; every real combat-damage trigger in the
            # inventory prints the bare phrase and still passes.
            _combat_phrase = COMBAT_DAMAGE_SELF_PHRASE
            if not _combat_phrase.search(oracle_lower):
                continue
            # Slice 5b: watcher-phrased text ("...you control deals combat
            # damage...") belongs to the battlefield-watcher loop above, not
            # to this SELF-trigger scan — an attacking Ohran/Tovolar would
            # otherwise double-dispatch (their own-connect templates were
            # removed for exactly that). Skip when no non-watcher sentence
            # carries the phrase. Aug 7 (C-1): same negation-aware phrase
            # here, and "you control would deal" (Solphim's replacement
            # wording) counts as watcher-shaped too.
            if not any(
                    _combat_phrase.search(s)
                    and 'you control deal' not in s
                    and 'you control would deal' not in s
                    for para in oracle_lower.split('\n') for s in para.split('.')):
                continue
            try:
                opp = damaged_player
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
                elif actions is None:
                    # Nothing matched. This path is sync, so it has no Tier-3
                    # escalation — and until July 28 2026 it also had no tag and
                    # no queue, so an unmatched combat-damage trigger vanished
                    # without a trace. Ragavan's entire trigger (Treasure +
                    # impulse exile) disappeared on every connect and no audit
                    # grep could see it. Queue it for the async drain, the same
                    # shape as queue_unhandled_dies and the July 24 sync cast
                    # bridge; the `not game.ended` gate above still applies.
                    from mtg.triggers import queue_unhandled_combat_damage
                    queue_unhandled_combat_damage(
                        game, attacker, attacker_owner, damage_amount)
            except Exception as e:
                print(f"[COMBAT-TRIGGER] Error for {attacker.name}: {e}")
        game._combat_damage_to_player = []  # Clear after processing

    # Slice 5b (July 31, 2026): damaged-creature scan — "Whenever a source
    # deals damage to this creature" (Phyrexian Obliterator; CR 603.2). No
    # scan existed anywhere before (July 30 reviewer R14). The creature-kind
    # COMBAT_DAMAGE_DEALT subscriber accumulates; this drain resolves under
    # the same `not game.ended` gate as the player-kind drain above.
    # Aug 1: the scan body moved to triggers.scan_damaged_creature so the
    # NONCOMBAT damage paths (deal_damage action, spell_resolver's damage
    # exec) invoke the same logic — CR 603.2 says A SOURCE, any source.
    # This drain keeps the combat-specific part: resolving the source's
    # controller from the battlefield, with the died-mid-combat fallback.
    damaged_entries = getattr(game, '_combat_damage_to_creature', [])
    if damaged_entries and not game.ended:
        from mtg.triggers import scan_damaged_creature
        for entry in damaged_entries:
            # Aug 2 batch-14 audit (I-1): entries carry the source's
            # controller resolved at ACCUMULATION time (the subscriber),
            # because at drain time both the source and the damaged creature
            # can already be dead — the old drain-time lookups both failed
            # for Obliterator-vs-four-blockers and the deterministic edict
            # never ran. Drain-time resolution kept only as the fallback.
            source_card, damaged, dmg_amount = entry[0], entry[1], entry[2]
            _src_owner = entry[3] if len(entry) > 3 else None
            if _src_owner is None:
                _damaged_owner = next(
                    (p for p in game.players if damaged in p.battlefield),
                    None)
                _src_owner = next(
                    (p for p in game.players
                     if any(c.id == getattr(source_card, 'id', None)
                            for c in p.battlefield)),
                    None)
                if _src_owner is None:
                    _owner_index = getattr(source_card, 'owner_index', None)
                    if (isinstance(_owner_index, int)
                            and 0 <= _owner_index < len(game.players)):
                        _src_owner = game.players[_owner_index]
            messages.extend(scan_damaged_creature(
                rules, game, damaged, dmg_amount, _src_owner))
        game._combat_damage_to_creature = []


def deal_unblocked_damage(rules, game, attacker, amount, defending_player,
                          messages):
    """Land unblocked/trample damage on a planeswalker if one was attacked.

    Returns the damage actually dealt, so callers keep their existing lifelink
    and trigger handling unchanged — lifelink fires either way, since CR
    702.15b turns on damage DEALT and not on what received it.

    Three CR points, all of which differ from the player path:
      - damage to a planeswalker removes that many loyalty counters
        (CR 120.3c), it does not reduce life;
      - commander damage does NOT accrue, because this is damage to a
        permanent rather than to a player (CR 903.10a);
      - if the planeswalker has already left, the damage is simply not dealt
        (CR 506.4 / 508.1) — it is NOT redirected to its controller.
    """
    walker = game.attacked_planeswalker_for(attacker)
    if walker is None:
        if getattr(attacker, 'attacking_planeswalker', None):
            # Declared at a planeswalker that is no longer there.
            messages.append(
                f"⚔️ {attacker.name} was attacking a planeswalker that has "
                f"left the battlefield — no damage is dealt")
            return 0
        return None  # not a planeswalker attack; caller uses the player path

    before = getattr(walker, 'loyalty_counters', 0)
    walker.loyalty_counters = max(0, before - amount)
    messages.append(
        f"⚔️ {attacker.name} deals {amount} damage to **{walker.name}** "
        f"(loyalty: {walker.loyalty_counters})")
    print(f"[PW-COMBAT] {attacker.name} dealt {amount} to {walker.name} "
          f"({before} → {walker.loyalty_counters} loyalty)")
    return amount


def resolve_combat_damage(rules, game: GameState) -> List[str]:
    """
    Resolve combat damage with keyword abilities.
    Returns list of messages describing what happened.
    """
    messages = []
    lifelink_healing = {
        index: 0 for index in range(len(game.players))
    }  # player_index -> life to gain

    # Aug 11 audit (reviewer D, F5): "whenever this creature blocks" had no
    # scan anywhere. Fired here — the one choke point every combat flows
    # through — rather than at the five separate blocker-declaration sites
    # across ai_turn.py and autoplay.py. Guarded so the FS step and the
    # regular step don't both fire it (a creature blocks once per combat).
    try:
        from mtg.triggers import check_block_triggers
        from mtg.util import maybe_reraise as _mr
        messages.extend(check_block_triggers(
            getattr(rules, 'engine_ref', None) or rules, game))
    except Exception as e:                                   # noqa: BLE001
        from mtg.util import maybe_reraise as _mr
        _mr(e)
        print(f"[BLOCK-TRIGGER] scan failed: {e}")

    # Aug 7 (A-2b): each resolve_combat_damage invocation is one damage
    # step (FS and regular are separate calls/events); reset the per-step
    # equipment charge-trigger dedupe so Jitte fires once per step, not
    # once per game and not twice for a trample split.
    game._equip_charge_fired_ids = set()

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
                    messages.append(f"💚 {game.players[idx].name} gains {actual_heal} life (lifelink) (life: {max(0, game.players[idx].life)})")
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

        # CR 510.4: normal SBA *and trigger* processing happens after
        # EACH combat damage step. Without this call a first-striker's
        # own 'deals combat damage to a player' trigger (Drana) landed
        # after the regular step had already dealt un-pumped damage.
        drain_combat_damage_triggers(rules, game, messages)
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
                    messages.append(f"💚 {game.players[idx].name} gains {actual_heal} life (lifelink) (life: {max(0, game.players[idx].life)})")
                    print(f"[LIFELINK] {game.players[idx].name}: +{actual_heal} life → {game.players[idx].life}")
        return messages

    # Regular damage step
    # BUG-10 fix: non-FS blockers always deal retaliatory damage in the regular step,
    # even when blocking first-strikers that already dealt damage in the FS step.
    if regular_attackers or first_strikers:
        print(f"[COMBAT-STEP] REGULAR step: {len(regular_attackers)} regular attacker(s)")
        # Aug 7 (A-2b): the regular step is a SEPARATE damage event from the
        # FS step — a double-striker's Jitte legitimately fires in both
        # (rulings). Reset the per-step dedupe at the boundary.
        game._equip_charge_fired_ids = set()
        _reg_header_idx = None
        if first_strikers:
            messages.append("**Regular Combat Damage:**")
            _reg_header_idx = len(messages) - 1
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
        # June 10 audit: drop the "**Regular Combat Damage:**" header when no
        # regular-step line followed it (all damage landed in the FS step).
        if _reg_header_idx is not None and len(messages) == _reg_header_idx + 1:
            messages.pop(_reg_header_idx)

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
                messages.append(f"💚 {game.players[idx].name} gains {actual_heal} life (lifelink) (life: {max(0, game.players[idx].life)})")
                print(f"[LIFELINK] {game.players[idx].name}: +{actual_heal} life → {game.players[idx].life}")
            else:
                messages.append(f"🚫 Lifelink prevented for {game.players[idx].name} ({', '.join(chain)})")
                print(f"[LIFELINK-PREVENTED] {game.players[idx].name}: heal={heal} chain={','.join(chain)}")
            lifelink_healing[idx] = 0

    # Check SBAs after regular combat damage (kills creatures with lethal damage)
    # Without this, blockers survive combat with lethal damage marked
    sba_messages = rules.process_state_based_actions(game)
    messages.extend(sba_messages)

    # CR 510.4 / 603.3: resolve combat-damage triggers for THIS step
    # before the next one computes damage. See
    # drain_combat_damage_triggers for why calling it twice is safe.
    drain_combat_damage_triggers(rules, game, messages)

    # Gorgon Recluse's trigger names the creature it actually blocked/was
    # blocked by and destroys it at end of combat, after combat damage and
    # the associated SBA sweep but before callers clear combat state.
    from mtg.triggers import drain_end_of_combat_destructions
    messages.extend(drain_end_of_combat_destructions(
        getattr(rules, 'engine_ref', None) or rules, game))

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
            messages.append(f"💚 {game.players[idx].name} gains {actual_heal} life (lifelink) (life: {max(0, game.players[idx].life)})")
            # Apr 30 audit: console mirror so post-batch life-total reconciliation
            # doesn't go missing in the per-game console log (Discord-only events
            # broke the audit's math three times).
            print(f"[LIFELINK] {game.players[idx].name}: +{actual_heal} life → {game.players[idx].life}")

    # July 30 batch-9 audit (deferred July 29 item): gain-life triggers fired
    # during combat (Heliod counters, Vito drains) buffer their display lines
    # in game._pending_messages, whose only drains were the draw step and the
    # cast path — so combat-fired gains showed under the NEXT turn's banner.
    # State was always correct; drain here so the lines ride the combat block.
    from mtg.helpers import drain_pending_messages
    messages.extend(drain_pending_messages(game))

    return messages


def apply_life_gain(rules, game: GameState, player: 'PlayerState', amount: int,
                      source_name: str = "") -> Tuple[bool, int, List[str]]:
    """
    Apply a single life-gain event to a player after processing LIFE_GAIN
    replacement effects (Erebos, Sulfuric Vortex, Rhox Faithmender, etc.).
    Returns (applied, final_amount, replacement_chain). Applied=False means
    the gain was prevented and no life was added.
    This is the single place "whenever you gain life" triggers fire (wired
    June 10 — see _fire_gain_life_triggers below). Any life-gain path that
    bypasses this function also bypasses Vito / Heliod / Pridemate triggers
    AND gain-prevention statics (Erebos), so route gains through here.
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
    # June 10 audit (V26): fire "whenever you gain life" triggers (CR 603.2).
    # June 10 round 3: routed through the event bus (mtg/events.py) as
    # pub/sub migration slice 1 — the gain-life scan is now a SUBSCRIBER,
    # so future gain-adjacent triggers register instead of editing here.
    if game is not None:
        events.emit(events.LIFE_GAINED, game, rules=rules, player=player,
                    amount=final_amount, source_name=source_name)
    return True, final_amount, chain


def _fire_gain_life_triggers(rules, game: GameState, player: 'PlayerState',
                             amount: int, source_name: str = "") -> None:
    """Resolve "whenever you gain life" triggered abilities for one gain event.

    June 10 audit (V26): this trigger class had no implementation anywhere.
    Handles the common shapes inline (free, sync):
      - "...each/target opponent loses that much life"  (Vito, Sanguine Bond)
      - "...put a +1/+1 counter on it/this creature"     (Ajani's Pridemate)
      - "...put a +1/+1 counter on target creature or enchantment you control"
                                                          (Heliod, Sun-Crowned)
    Display lines surface via game._pending_messages (same channel as the
    Phyrexian Tower dies-trigger fix) so every apply_life_gain caller gets
    them without a signature change. Unhandled shapes print a
    [GAIN-TRIGGER-UNHANDLED] breadcrumb instead of silently dropping.
    """
    if amount <= 0:
        return
    # Re-entrancy guard: a gain trigger that itself gains life must not recurse.
    if getattr(game, '_in_gain_life_triggers', False):
        return
    game._in_gain_life_triggers = True
    try:
        import re as _re
        pq = getattr(game, '_pending_messages', None)
        if pq is None:
            pq = []
            game._pending_messages = pq
        for perm in list(player.battlefield):
            oracle = (perm.oracle_text or '').lower()
            if 'whenever you gain life' not in oracle:
                continue
            m = _re.search(r'whenever you gain life[^.]*\.', oracle)
            clause = m.group(0) if m else oracle
            if 'first time each turn' in clause:
                # Frequency-limited variants need per-turn tracking; under-fire
                # (skip) is safer than firing on every event.
                print(f"[GAIN-TRIGGER-UNHANDLED] {perm.name}: first-time-each-turn variant skipped")
                continue
            if 'loses that much life' in clause or 'lose that much life' in clause:
                for opp in game.players:
                    if opp is player:
                        continue
                    opp.life -= amount
                    opp.record_life_loss(amount, game=game)
                    print(f"[GAIN-TRIGGER] {perm.name}: {opp.name} loses {amount} life → {opp.life}")
                    pq.append(f"🩸 **{perm.name}**: {opp.name} loses {amount} life (life: {max(0, opp.life)})")
            elif _re.search(r'put a \+1/\+1 counter on (?:it|this creature)', clause) \
                    or _re.search(r'put a \+1/\+1 counter on ' + _re.escape(perm.name.lower()), clause):
                perm.counters['+1/+1'] = perm.counters.get('+1/+1', 0) + 1
                print(f"[GAIN-TRIGGER] {perm.name}: +1/+1 counter ({perm.counters['+1/+1']} total)")
                pq.append(f"💪 **{perm.name}** gets a +1/+1 counter (life gain trigger)")
            elif 'put a +1/+1 counter on target creature' in clause:
                # Heliod, Sun-Crowned — put it on the controller's biggest creature.
                candidates = [c for c in player.battlefield if c.is_creature(game=game)]
                if candidates:
                    tgt = max(candidates, key=lambda c: c.get_effective_power(game))
                    tgt.counters['+1/+1'] = tgt.counters.get('+1/+1', 0) + 1
                    print(f"[GAIN-TRIGGER] {perm.name}: +1/+1 counter on {tgt.name}")
                    pq.append(f"💪 **{perm.name}**: +1/+1 counter on **{tgt.name}**")
            elif 'put a +1/+1 counter on each creature you control' in clause:
                # Archangel of Thune. July 20 audit: fired
                # [GAIN-TRIGGER-UNHANDLED] twice in game_1527462198430138448
                # — her core passive did nothing all game.
                _boosted = 0
                for c in player.battlefield:
                    if c.is_creature(game=game):
                        c.counters['+1/+1'] = c.counters.get('+1/+1', 0) + 1
                        _boosted += 1
                if _boosted:
                    game.recalculate_power_toughness()
                    print(f"[GAIN-TRIGGER] {perm.name}: +1/+1 counter on {_boosted} creature(s)")
                    pq.append(f"💪 **{perm.name}**: a +1/+1 counter on each of "
                              f"{_boosted} creature(s) (life gain trigger)")
            else:
                print(f"[GAIN-TRIGGER-UNHANDLED] {perm.name}: {clause[:100]}")
    finally:
        game._in_gain_life_triggers = False


def _on_life_gained(game, rules=None, player=None, amount=0, source_name=""):
    """Bus adapter: LIFE_GAINED → the gain-life trigger scan.

    Pub/sub migration slice 1 (June 10) — see mtg/events.py for the plan.
    The scan itself is unchanged; it's now reached by subscription instead
    of a direct call, so future gain-watchers register instead of editing
    apply_life_gain.
    """
    _fire_gain_life_triggers(rules, game, player, amount, source_name)


events.subscribe(events.LIFE_GAINED, _on_life_gained)


def apply_combat_damage_to_player(rules, game: GameState, player: 'PlayerState',
                                     amount: int, source_card: Card, is_combat: bool = True) -> int:
    """Apply damage to a player, processing replacement effects first. Returns final amount."""
    if amount <= 0:
        return 0
    # Damage prevention (Teferi's Protection, Fog, Glacial Chasm, Moonmist's
    # exemption). Aug 10 (A2): the whole gate moved into helpers so the CREATURE
    # funnels consult exactly the same predicate — one gate, no drift.
    # allow_static=True here: "prevent all damage that would be dealt to you"
    # IS about the player.
    from mtg.helpers import damage_prevented_for, damage_source_colors
    _prevented, _why = damage_prevented_for(game, player, source_card,
                                            is_combat=is_combat, allow_static=True)
    if _prevented:
        print(f"  [DAMAGE-PREVENTED] {source_card.name} → {player.name}: {amount} damage prevented")
        return 0
    if _why:
        print(f"  [DAMAGE-PREVENTED] {player.name}: {_why}")
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
            # Aug 2 batch-14: color-gated replacements (Torbran) need the
            # source's colors — CR 202.2, from the mana cost.
            source_colors=damage_source_colors(game, source_card=source_card),
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
        player.record_life_loss(amount, game=game)
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
        if is_combat:
            # Tymna the Weaver counts opponents dealt combat damage this turn.
            # Set even at amount 0? No — CR 119.3: 0 damage isn't dealt.
            if amount > 0:
                player.dealt_combat_damage_this_turn = True

    # Pub/sub slice 5a (July 30, 2026 — SHADOW): one emission per damage
    # APPLICATION, after replacements and the infect/life split (poison IS
    # dealt damage per CR 120.3). The recorder diffs these against the
    # attacker-loop's _combat_damage_to_player appends.
    if is_combat and amount > 0:
        events.emit(events.COMBAT_DAMAGE_DEALT, game, source=source_card,
                    target=player, amount=amount, target_kind="player")
        # Aug 7 (A-2b): Jitte-class equipment triggers fire on combat damage
        # to ANYTHING — the unblocked-connect case flows through this
        # funnel. Deduped per step by _equip_charge_fired_ids, so a
        # trampler that also damaged its blocker fires once.
        fire_equipped_combat_damage_counters(game, source_card)

    # CR 903.10a / 704.5b — Commander damage tracking, PER COMMANDER.
    # Aug 1 batch-12 (reviewer, partner game): the dict was keyed by the
    # source's CONTROLLER index, so Thrasios's 22 and Tymna's 23 summed into
    # one bucket ("total from player 0: 45/21") — under partners a player
    # could be ruled dead off 11+10 from two sub-lethal commanders. CR
    # 903.10a is "by the same commander"; key by the commander's name (two
    # commanders can never share one).
    if (is_combat and getattr(source_card, 'is_commander', False)
            and getattr(game, 'format', '').lower() in ('commander', 'edh')):
        _cd_key = source_card.name
        prior = player.commander_damage.get(_cd_key, 0)
        player.commander_damage[_cd_key] = prior + amount
        total = player.commander_damage[_cd_key]
        print(f"[COMMANDER-DAMAGE] {player.name} takes {amount} commander damage from "
              f"{source_card.name} (total from {_cd_key}: {total}/21)")
    return amount


def apply_combat_damage_to_creature(rules, game: GameState, creature: Card,
                                      amount: int, source_card: Card,
                                      source_has_deathtouch: bool = False) -> int:
    """Apply combat damage to a creature, processing replacement effects. Returns final amount."""
    if amount <= 0:
        return 0
    # Aug 10 deferred (A2): flag-based prevention was consulted ONLY at the two
    # PLAYER funnels, so under a Fog the blockers still died. allow_static=False
    # is load-bearing — Glacial Chasm / Solitary Confinement print "prevent all
    # damage that would be dealt to YOU" and say nothing about creatures.
    from mtg.helpers import damage_prevented_for
    _ctrl = (creature._find_controller(game)
             if hasattr(creature, '_find_controller') else None)
    _prevented, _why = damage_prevented_for(game, _ctrl, source_card,
                                            is_combat=True, allow_static=False)
    if _prevented:
        print(f"  [DAMAGE-PREVENTED] {source_card.name} → {creature.name}: "
              f"{amount} combat damage prevented")
        return 0
    if _why:
        print(f"  [DAMAGE-PREVENTED] {creature.name}: {_why} — damage stands")
    # Aug 10 deferred (E2) — CR 702.16e.
    from mtg.helpers import protection_prevents_damage
    _prot, _prot_why = protection_prevents_damage(game, creature,
                                                  source_card=source_card)
    if _prot:
        print(f"  [PROTECTION] {creature.name} has {_prot_why} — {amount} "
              f"damage from {source_card.name} prevented")
        return 0
    if HAS_REPLACEMENT_ENGINE and game._replacement_engine and game._replacement_engine.effects:
        from mtg.helpers import damage_source_colors
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
            source_colors=damage_source_colors(game, source_card=source_card),
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
    # Pub/sub slice 5a (July 30, 2026 — SHADOW): see the player funnel.
    if amount > 0:
        events.emit(events.COMBAT_DAMAGE_DEALT, game, source=source_card,
                    target=creature, amount=amount, target_kind="creature")
        # Aug 7 confirmation-batch audit (A-2b/B-2): per-turn dealer→damaged
        # attribution. The Predator Ooze dies-watcher premise gate and
        # equipment charge-counter triggers both read this record.
        _dealer_id = getattr(source_card, 'id', None)
        if _dealer_id:
            game._creature_combat_damage_by_dealer.setdefault(
                _dealer_id, set()).add(getattr(creature, 'id', ''))
        fire_equipped_combat_damage_counters(game, source_card)
    return amount


_EQUIP_CHARGE_CLAIM = re.compile(
    r'whenever equipped creature deals combat damage[^.]*?put '
    r'(?:a|one|two|three|four|\d+) charge counters? on', re.IGNORECASE)


def _equipment_charge_claims(equipment) -> bool:
    """Is this equipment's combat-damage trigger already consumed by
    fire_equipped_combat_damage_counters?

    Umezawa's Jitte has no template, so the untemplated-equipment queue added
    Aug 10 would otherwise escalate it to Tier 3 on every connect — on top of
    the charge counters the deterministic path has already applied. Mirrors
    that function's own parse so the two cannot drift.
    """
    return bool(_EQUIP_CHARGE_CLAIM.search(getattr(equipment, 'oracle_text', '') or ''))


def fire_equipped_combat_damage_counters(game: GameState, dealer: Card) -> None:
    """Aug 7 confirmation-batch audit (A-2b): Umezawa's Jitte's "Whenever
    equipped creature deals combat damage, put two charge counters on
    Umezawa's Jitte" — the trigger's printed phrase has NO "to a player"
    (it also fires on damage to blockers), so the Worldslayer equipment scan
    never matched it, and the creature-damage path had no equipment hook at
    all: four unblocked connects in game_1535222945050271766 produced zero
    charge counters.

    Deterministic: parse "put N charge counter(s) on" from the trigger
    sentence and add them to the equipment. Dedupe per damage STEP via
    game._equip_charge_fired_ids (a trampler damaging blocker + player in
    one step is ONE damage event per Jitte rulings; first-strike and
    regular steps are separate events and fire separately). Messages ride
    game._pending_messages — the funnel returns an amount, not text.
    """
    _word_nums = {'a': 1, 'one': 1, 'two': 2, 'three': 3, 'four': 4}
    dealer_owner = None
    for p in game.players:
        if any(c is dealer for c in p.battlefield):
            dealer_owner = p
            break
    if dealer_owner is None:
        return
    for att_id in list(getattr(dealer, 'attachments', []) or []):
        att = next((c for c in dealer_owner.battlefield if c.id == att_id), None)
        if att is None or not att.oracle_text:
            continue
        if 'equipment' not in (att.type_line or '').lower():
            continue
        if att.id in game._equip_charge_fired_ids:
            continue
        ot = att.oracle_text.lower()
        m = re.search(
            r'whenever equipped creature deals combat damage[^.]*?put '
            r'(a|one|two|three|four|\d+) charge counters? on', ot)
        if not m:
            continue
        n = _word_nums.get(m.group(1), None)
        if n is None:
            try:
                n = int(m.group(1))
            except ValueError:
                continue
        game._equip_charge_fired_ids.add(att.id)
        if not hasattr(att, 'counters') or att.counters is None:
            att.counters = {}
        att.counters['charge'] = att.counters.get('charge', 0) + n
        total = att.counters['charge']
        print(f"[EQUIP-CHARGE] {att.name}: +{n} charge counter(s) "
              f"(total {total}) — equipped creature dealt combat damage")
        try:
            game._pending_messages.append(
                f"⚡ **{att.name}**: {n} charge counter(s) added (total {total})")
        except AttributeError:
            pass


def apply_noncombat_damage_to_creature(rules, game: GameState, creature: Card,
                                        amount: int, source_name: str = "",
                                        source_id: str = "",
                                        source_controller: str = "",
                                        source_controller_player=None):
    """Apply NONCOMBAT damage to a creature. Returns (final_amount, messages).

    The creature twin of apply_noncombat_damage_to_player, and deliberately
    NOT apply_combat_damage_to_creature: that one stamps is_combat_damage=True,
    which is wrong for a burn spell or a mass-damage sorcery and is load-bearing
    in both directions — Solphim's registration gates on `not ev.is_combat_damage`
    (it would wrongly fire), and any combat-only replacement would wrongly apply.
    Aug 10 card-targeted wave: the MASS DAMAGE handler in mtg/spells.py did
    `c.damage_marked += dmg` raw, so Furnace of Rath / Gisela / Torbran / Insult
    // Injury / Fiery Emancipation / Angrath's Marauders were ALL silently
    inert against every board wipe — defeating the replacement_chain deck's
    whole premise — while the "and each player" half four lines below already
    routed correctly.

    Does the three things every noncombat creature-damage site needs: run the
    replacement chain, mark the damage, and fire the damaged-creature trigger
    scan (CR 603.2 — "whenever a source deals damage to this creature" is not
    combat-only; Phyrexian Obliterator vs a board wipe).
    """
    if amount <= 0:
        return 0, []
    # Aug 10 deferred (A2): the creature twin of the player gate. is_combat is
    # False here, so a Fog (combat-only) correctly does NOT reach a burn spell
    # while Teferi's Protection still does — the distinction the shared helper
    # exists for. allow_static=False for the same reason as the combat twin.
    from mtg.helpers import damage_prevented_for
    _ctrl = (creature._find_controller(game)
             if hasattr(creature, '_find_controller') else None)
    _prevented, _ = damage_prevented_for(game, _ctrl, None,
                                         is_combat=False, allow_static=False)
    if _prevented:
        print(f"  [DAMAGE-PREVENTED] {source_name or '?'} → {creature.name}: "
              f"{amount} noncombat damage prevented")
        return 0, []
    # Aug 10 deferred (E2) — CR 702.16e. THIS is the funnel the live defect ran
    # through: Akroma (protection from black and from red) was killed by a RED
    # Blasphemous Act. The source has already left the stack by now, which is
    # why colours resolve by name/id across zones rather than from a card ref.
    from mtg.helpers import protection_prevents_damage
    _prot, _prot_why = protection_prevents_damage(
        game, creature, source_name=source_name, source_id=source_id)
    if _prot:
        print(f"  [PROTECTION] {creature.name} has {_prot_why} — {amount} "
              f"damage from {source_name or '?'} prevented")
        return 0, []
    if HAS_REPLACEMENT_ENGINE and game._replacement_engine and game._replacement_engine.effects:
        from mtg.helpers import damage_source_colors
        creature_controller_name = ""
        if hasattr(creature, '_find_controller'):
            ctrl = creature._find_controller(game)
            if ctrl is not None:
                creature_controller_name = getattr(ctrl, 'name', "") or ""
        event = GameEvent(
            event_type=EventType.DAMAGE,
            affected_player=creature_controller_name,
            affected_object=getattr(creature, 'id', ''),
            affected_object_name=creature.name,
            amount=amount,
            source_name=source_name,
            source_id=source_id,
            source_controller=source_controller or "",
            source_colors=damage_source_colors(
                game, source_name=source_name, source_id=source_id),
            is_combat_damage=False,
        )
        final = game._replacement_engine.process_event_sync(event)
        if final.is_prevented:
            print(f"  [REPLACEMENT-APPLY] Noncombat creature damage prevented: "
                  f"{source_name} → {creature.name} ({', '.join(final.replacement_chain)})")
            return 0, []
        if final.amount != amount:
            print(f"  [REPLACEMENT-APPLY] Noncombat creature damage modified: "
                  f"{amount} → {final.amount} ({', '.join(final.replacement_chain)})")
        amount = final.amount
    if amount <= 0:
        return 0, []
    creature.damage_marked = getattr(creature, 'damage_marked', 0) + amount
    from mtg.triggers import scan_damaged_creature
    msgs = scan_damaged_creature(rules, game, creature, amount,
                                 source_controller_player) or []
    return amount, msgs


def apply_noncombat_damage_to_player(rules, game: GameState, player: 'PlayerState',
                                       amount: int, source_name: str = "",
                                       source_id: str = "",
                                       source_controller: str = "") -> int:
    """Apply non-combat damage to a player, processing replacement effects. Returns final amount.

    Aug 7 confirmation-batch audit (B-4): `source_controller` lets a caller
    that KNOWS the caster (Tier-2 SpellResolver) say so directly — the
    battlefield/stack lookups below fail for a spell mid-resolution (the
    stack entry is popped before dispatch), which left Torbran's "a red
    source you control" gate silently unmatched on Tier-2 burn (Skullcrack
    dealt 3 instead of 5, game_1535228623240568872)."""
    if amount <= 0:
        return 0
    # Damage prevention. Aug 10 (A2): shared predicate, is_combat=False — this
    # is where a Fog used to blank a burn spell to the face, because ONE flag
    # served both prevent_combat_damage and prevent_all_damage. Teferi's
    # Protection (all damage) still prevents here; a fog no longer does.
    from mtg.helpers import damage_prevented_for, damage_source_colors
    _prevented, _ = damage_prevented_for(game, player, None,
                                         is_combat=False, allow_static=True)
    if _prevented:
        print(f"  [DAMAGE-PREVENTED] {source_name} → {player.name}: {amount} noncombat damage prevented")
        return 0
    if HAS_REPLACEMENT_ENGINE and game._replacement_engine and game._replacement_engine.effects:
        # Try to find the controller of the source by id or name so the
        # replacement layer can filter by source_controller (Fiery
        # Emancipation, Gisela, etc. — see _apply_combat_damage_to_player).
        # Aug 7 (B-4): an explicitly-passed caster wins; lookups are the
        # fallback for callers that only know a name/id.
        source_controller_name = source_controller or ""
        if not source_controller_name and (source_id or source_name):
            for _p in game.players:
                for _c in _p.battlefield:
                    if (source_id and getattr(_c, 'id', '') == source_id) or \
                       (source_name and _c.name == source_name):
                        source_controller_name = _p.name
                        break
                if source_controller_name:
                    break
        if not source_controller_name:
            # Aug 2 batch-14: a burn SPELL is not a permanent — it is on the
            # stack (or already off it, mid-resolution), so the battlefield
            # scan above leaves the controller blank and any printed
            # "a source YOU control" gate (Torbran) silently never fires.
            for _entry in getattr(game, 'stack', []) or []:
                _c = getattr(_entry, 'card', None)
                if _c is None:
                    continue
                if (source_id and getattr(_c, 'id', '') == source_id) or \
                   (source_name and _c.name == source_name):
                    source_controller_name = getattr(
                        _entry, 'controller_name', '') or ''
                    break
        # (A spell that has already left the stack mid-resolution is not
        # resolvable here at all. That case reaches the replacement layer
        # through mtg/actions.py's deal_damage instead, which carries the
        # caster explicitly as `_source_controller` — the template and
        # Tier-3 burn paths both set it.)
        event = GameEvent(
            event_type=EventType.DAMAGE,
            affected_player=player.name,
            amount=amount,
            source_name=source_name,
            source_id=source_id,
            source_controller=source_controller_name,
            # No card object here — resolve by id/name across battlefields,
            # the stack, and the resolving-spell slot (a burn spell is off
            # the stack by the time its damage applies).
            source_colors=damage_source_colors(
                game, source_name=source_name, source_id=source_id),
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
    player.record_life_loss(amount, game=game)
    if amount > 0:
        # May 18 audit: clamp displayed life at 0 (same rationale as [COMBAT-LIFE]).
        print(f"[NONCOMBAT-LIFE] {player.name} takes {amount} noncombat damage from {source_name} (life: {max(0, player.life)})")
    return amount


def make_replacement_callback(rules, game: GameState, channel=None,
                              private_send=None, timeout_seconds: float = 120.0):
    """Return a private, durable CR 616 replacement-order chooser."""

    async def callback(chooser: str, effects):
        player_idx = next(
            (i for i, player in enumerate(game.players)
             if chooser and player.name.lower() == chooser.lower()),
            None,
        )
        if player_idx is None:
            # Never hand an unknown affected player's choice to seat zero.
            return sorted(effects, key=lambda effect: effect.timestamp)[0]

        from mtg.choices import create_choice, format_choice_prompt, wait_for_choice
        record = create_choice(
            game,
            choice_type="replacement_order",
            chooser_indices=[player_idx],
            options_by_player=[
                {
                    "label": (
                        f"{effect.source_name}: "
                        f"{getattr(effect, 'description', None) or effect.condition_text or effect.replacement_type}"
                    ),
                    "value": effect.id,
                    "payload": effect,
                }
                for effect in effects
            ],
            private=True,
            timeout_seconds=timeout_seconds,
            metadata={"chooser": chooser},
        )
        chooser_id = game.players[player_idx].seat_id
        prompt = format_choice_prompt(record, chooser_id)

        delivered = False
        if private_send:
            await private_send(player_idx, prompt)
            delivered = True
        if channel:
            # The shared thread gets status, never private option text. If no
            # Discord-specific private sender was provided, !choice performs
            # the DM retry through the cog.
            await channel.send(
                f"🔒 **{chooser}** has a private replacement-order choice. "
                + ("Check your DMs." if delivered
                   else "Use `!choice` to retry the private prompt."))

        return await wait_for_choice(game, record['choice_id'])

    return callback


def deal_combat_damage(rules, game: GameState, attackers: List[Tuple[Card, Player]], is_first_strike_step: bool = False, skip_attacker_damage: bool = False) -> Tuple[List[str], Dict[int, int]]:
    """
    Deal combat damage for a set of attackers.
    Returns (messages, lifelink_healing_by_player_index)
    skip_attacker_damage=True: blockers still deal retaliatory damage but attackers don't
    deal damage (used in regular step when all attackers already dealt FS damage).
    """
    messages = []
    lifelink_healing = {index: 0 for index in range(len(game.players))}

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

        # A multiplayer defender can leave between the first-strike and
        # regular damage steps. regular_attackers is a pre-first-strike
        # snapshot, so consult the live combat list again. eliminate_player
        # removes attackers assigned to a departed seat; they do not get to
        # retarget their regular damage to another living opponent.
        if attacker.id not in game.attackers:
            print(f"[COMBAT-DAMAGE] Skipping {attacker.name} - removed from combat")
            continue

        if getattr(attacker, '_phased_out', False):
            print(f"[COMBAT-DAMAGE] Skipping {attacker.name} - phased out")
            continue

        defending_player = game.defender_for(attacker)
        if defending_player is None:
            print(f"[COMBAT] Skipping {attacker.name}: no living defender")
            continue
        defending_player_idx = game.players.index(defending_player)

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

        # A phased-out or departed blocker deals/receives no damage, but the
        # attacking creature remains blocked (CR 509.1h). Keep that historical
        # fact separate from the active damage-assignment list.
        was_blocked = False
        active_blocker_ids = []
        for blocker_id in blocker_ids:
            result = game.find_card_global(blocker_id)
            if not result:
                # A token blocker can cease to exist before damage; the
                # attacker remains blocked even though the object is gone.
                was_blocked = True
                continue
            blocker, _blocker_owner, zone = result
            if _blocker_owner is not defending_player:
                print(
                    f"[COMBAT] Rejecting {blocker.name} as blocker for "
                    f"{attacker.name}: controlled by {_blocker_owner.name}, "
                    f"defender is {defending_player.name}"
                )
                continue
            was_blocked = True
            if zone != Zone.BATTLEFIELD or getattr(blocker, '_phased_out', False):
                print(f"[COMBAT-DAMAGE] Skipping blocker {blocker.name} - inactive")
                continue
            active_blocker_ids.append(blocker_id)
        blocker_ids = active_blocker_ids

        print(f"[COMBAT-DAMAGE] {attacker.name} (id={attacker.id}, power={attacker_power}): blocker_ids={blocker_ids}")

        if not was_blocked:
            # Unblocked - damage to defending player (with replacement effect processing)
            if skip_attacker_damage:
                # Regular step for FS-only board: unblocked FS attackers already dealt damage
                continue
            # CR 510.5: a non-FS/DS attacker deals no damage in the FS step.
            if is_first_strike_step and not (attacker.has_first_strike(game=game) or attacker.has_double_strike(game=game)):
                continue
            _pw_damage = deal_unblocked_damage(
                rules, game, attacker, attacker_power, defending_player,
                messages)
            if _pw_damage is not None:
                # A planeswalker attack: loyalty already adjusted (or the
                # walker is gone and nothing was dealt). Lifelink still
                # applies below; the player-facing life/commander lines do not.
                actual_damage = _pw_damage
            else:
                actual_damage = rules._apply_combat_damage_to_player(game, defending_player, attacker_power, attacker)
            if _pw_damage is None and actual_damage > 0:
                # May 16 audit: append running life total so players can follow
                # the combat math live without scrolling for next !state. Burn
                # spell damage already does this (`(life: 4)` format); combat
                # damage was silent on 121/122 games in the May 15 batch.
                messages.append(
                    f"⚔️ {attacker.name} deals {actual_damage} damage to "
                    f"{defending_player.name} (life: {max(0, defending_player.life)})"
                )
                # June 11 audit: commander-damage running totals were console-
                # only ([COMMANDER-DAMAGE]); players first learned of the 21
                # rule from their own death message in 9/139 games. Surface the
                # tally whenever a commander connects.
                if (getattr(attacker, 'is_commander', False)
                        and getattr(game, 'format', '').lower() in ('commander', 'edh')):
                    _cd_total = defending_player.commander_damage.get(attacker.name)
                    if _cd_total:
                        messages.append(
                            f"👑 Commander damage: {defending_player.name} has taken "
                            f"{_cd_total}/21 from {attacker.name}"
                        )
                # Slice 5b: the combat-damage trigger queue is BUS-FED — the
                # COMBAT_DAMAGE_DEALT emission inside the damage funnel
                # accumulates via _accumulate_combat_damage_subscriber, so
                # this producer site no longer appends directly.
            elif _pw_damage is None:
                # Only the PLAYER path can be "prevented" here; a
                # planeswalker attack already reported its own outcome.
                messages.append(f"🛡️ {attacker.name}'s damage to {defending_player.name} was prevented")

            if attacker.has_lifelink(game=game) and actual_damage > 0:
                owner_idx = game.players.index(attacker_owner)
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
                        # Display attacker's full power (not clamped amount) for
                        # clarity — but ONLY when trample didn't split the
                        # assignment. July 21 batch audit (R4-2): a 5-power
                        # trampler vs a toughness-4 blocker displayed "deals 5"
                        # to the blocker AND "tramples for 1" — 6 damage from a
                        # 5-power creature (game_1529168842905882755). State was
                        # correct; only the display double-counted.
                        # July 26 batch-7 audit: `attacker_power` is captured
                        # BEFORE damage replacement runs, so once a doubler or
                        # halver was live the single-blocker branch reported
                        # the printed power instead of what was actually dealt
                        # — game_1530441531188711565 showed "deals 5 damage"
                        # for a hit that Gisela had doubled to 10 (lifelink
                        # confirmed 10). Whenever a replacement moved the
                        # number, `actual_dmg` is authoritative; the
                        # attacker_power clamp survives only for the unmodified
                        # case it was added for (July 21 trample double-count).
                        if len(blocker_ids) == 1 and not has_trample:
                            display_dmg = (
                                actual_dmg if actual_dmg != damage_to_blocker
                                else min(attacker_power,
                                         remaining_damage + damage_to_blocker))
                        else:
                            display_dmg = actual_dmg
                        # CR 120.8: zero damage isn't dealt — skip the noise line
                        # (June 10 audit: 36 "deals 0 damage" lines per batch).
                        if display_dmg > 0:
                            messages.append(f"⚔️ {_dispname(attacker)} deals {display_dmg} damage to {_dispname(blocker)}")
                
                # Blocker damages attacker (with replacement effect processing)
                # In first strike step, blockers without first strike/double strike don't deal damage
                blocker_deals_damage = blocker_power > 0
                if is_first_strike_step and not (blocker.has_first_strike(game=game) or blocker.has_double_strike(game=game)):
                    blocker_deals_damage = False
                # June 10 audit (C5, CR 510.5): the INVERSE gate was missing —
                # a first-strike (non-double-strike) blocker already dealt its
                # damage in the FS step and must not deal again in the regular
                # step (including the blockers-retaliation pass). Danitha
                # (2/2 FS lifelink) was killing 3/3 attackers with 2+2 and
                # getting lifelink credited twice.
                if (not is_first_strike_step
                        and blocker.has_first_strike(game=game)
                        and not blocker.has_double_strike(game=game)):
                    blocker_deals_damage = False
                if blocker_deals_damage:
                    actual_blocker_dmg = rules._apply_combat_damage_to_creature(
                        game, attacker, blocker_power, blocker,
                        source_has_deathtouch=blocker.has_deathtouch(game=game)
                    )
                    # CR 120.8: skip the display line when nothing was dealt
                    # (zero power or fully prevented — prevention details are
                    # already logged by the replacement engine).
                    if actual_blocker_dmg > 0 and blocker.has_deathtouch(game=game):
                        # deathtouch_damage is set by _apply_combat_damage_to_creature;
                        # SBA checks deathtouch_damage > 0 separately
                        messages.append(f"☠️ {_dispname(blocker)} deals {actual_blocker_dmg} deathtouch damage to {_dispname(attacker)}")
                    elif actual_blocker_dmg > 0:
                        messages.append(f"🛡️ {_dispname(blocker)} deals {actual_blocker_dmg} damage to {_dispname(attacker)}")

                    if blocker.has_lifelink(game=game) and actual_blocker_dmg > 0:
                        blocker_owner_idx = game.players.index(blocker_owner)
                        lifelink_healing[blocker_owner_idx] += actual_blocker_dmg
                        print(f"[LIFELINK] {blocker.name} blocking → {game.players[blocker_owner_idx].name} queued +{actual_blocker_dmg}")

                if attacker.has_lifelink(game=game) and actual_dmg > 0:
                    owner_idx = game.players.index(attacker_owner)
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
                _pw_trample = deal_unblocked_damage(
                    rules, game, attacker, remaining_damage, defending_player,
                    messages)
                actual_trample = (
                    _pw_trample if _pw_trample is not None
                    else rules._apply_combat_damage_to_player(game, defending_player, remaining_damage, attacker))
                if _pw_trample is None and actual_trample > 0:
                    messages.append(
                        f"🦏 {attacker.name} tramples for {actual_trample} damage to "
                        f"{defending_player.name} (life: {max(0, defending_player.life)})"
                    )
                    # June 11 audit: surface commander-damage tally (see the
                    # unblocked path above for rationale).
                    if (getattr(attacker, 'is_commander', False)
                            and getattr(game, 'format', '').lower() in ('commander', 'edh')):
                        _cd_total = defending_player.commander_damage.get(attacker.name)
                        if _cd_total:
                            messages.append(
                                f"👑 Commander damage: {defending_player.name} has taken "
                                f"{_cd_total}/21 from {attacker.name}"
                            )
                    # Slice 5b: bus-fed — see the unblocked producer site.
                elif _pw_trample is None:
                    # See the unblocked path: a planeswalker attack already
                    # reported its own outcome and cannot be "prevented" here.
                    messages.append(f"🛡️ {attacker.name}'s trample damage to {defending_player.name} was prevented")
                if attacker.has_lifelink(game=game) and actual_trample > 0:
                    owner_idx = game.players.index(attacker_owner)
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
