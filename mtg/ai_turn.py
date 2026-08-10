"""Claude AI turn execution (the engine-side of autoplay).

Five free functions extracted from GameEngine. Together they handle
running a Claude turn end-to-end: build the strategist memo, ask the
actor to plan the turn, validate the plan can pay mana, execute each
action, recover when an action fails, and continue after combat.

Public free functions (each takes a GameEngine instance as `engine`):

    execute_claude_turn(engine, ...)             (async)
        The main AI turn loop. Calls the actor + strategist, validates
        the plan, executes actions, handles failures.

    continue_claude_post_combat(engine, ...)     (async)
        Picks up the AI turn after combat damage resolves.

    _validate_plan_mana(engine, plan, ...)
        Pre-validates that the actor's planned actions can collectively
        be paid for given the available mana sources.

    _get_action_error(engine, action, ...)
        Diagnoses why a given AI action failed, for retry / fallback.

    _validate_activation(engine, ...)            (async)
        Validates that an activated ability can legally be activated
        (mana cost, summoning sickness, tap state, etc.).

Extracted from mtg/engine.py during Phase 2F-engine.
"""

import asyncio, json, random, re
from typing import Any, Dict, List, Optional, Tuple

from mtg.constants import Phase, Zone, COMMAND_ZONE_FORMATS
from mtg.helpers import (_normalize_pw_ability_idx, is_castable_from_exile,
                         library_top_cast_types)
from mtg.models import Card, Player, GameState

# June 10 audit (V31c): failure-indicator phrases for the "Action succeeded:"
# misclassification sniff (Bug E, May 20). Was inlined at the MAIN1 site only;
# now shared so the MAIN2 and post-combat loops apply the same guard.
FAILURE_RESULT_PHRASES = (
    'cannot activate',
    'already used its ability',
    'already activated',
    'no activated abilities',
    'cannot be cast',
    'cannot be activated',
    'no legal target',
    'no valid target',
    'has no mana cost',
)


def _result_looks_like_failure(result) -> bool:
    """True when an action handler returned a truthy string that is actually
    a rejection message (Bug E class).

    Aug 7 batch audit (B-3): a LEGALLY-RESOLVED no-op is not a failure —
    Inquisition of Kozilek cast successfully against a hand with no
    qualifying card and its "📋 No valid targets in opponent's hand"
    resolution line was sniffed as a cast failure, burning a retry on a
    card already in the graveyard (game_1535060193795248229). Template
    no-op RESOLUTION lines carry the 📋 display prefix; Bug-E executor
    rejections never do — exempt those lines from the sniff.
    """
    if not result:
        return False
    for line in str(result).splitlines():
        stripped = line.strip()
        if stripped.startswith("📋"):
            continue
        _ll = stripped.lower()
        if any(p in _ll for p in FAILURE_RESULT_PHRASES):
            return True
    return False

try:
    from rules.mana import ManaCost
    HAS_MANA_ENGINE = True
except ImportError:
    HAS_MANA_ENGINE = False

try:
    from rules.effect_templates import get_effect_library, build_game_context
    HAS_EFFECT_TEMPLATES = True
except ImportError:
    HAS_EFFECT_TEMPLATES = False

try:
    from rules.targeting_helpers import (
        _validate_target_for_action,
        _find_any_valid_target,
        _spell_requires_targets,
    )
    HAS_TARGETING = True
except ImportError:
    HAS_TARGETING = False


def _initial_color_pool(player) -> Tuple[Dict[str, int], int]:
    """Compute the player's current color-mana pool for plan simulation.

    Returns (mana_by_color, any_color_mana). Sums untapped lands AND mana
    rocks. Mirrors the breakdown decide_action / plan_turn already build at
    prompt time, so PLAN-VALIDATE's color awareness matches what the actor saw.
    """
    mana_by_color = {'W': 0, 'U': 0, 'B': 0, 'R': 0, 'G': 0, 'C': 0}
    any_color = 0
    for src in player.battlefield:
        if src.tapped:
            continue
        if not (src.is_land() or player._can_produce_mana(src)):
            continue
        try:
            production = player._get_mana_production(src)
        except Exception:
            continue
        for color, amt in (production or {}).items():
            if color == 'any':
                any_color += amt
            elif color in mana_by_color:
                mana_by_color[color] += amt
    return mana_by_color, any_color


def _simulate_cast_spend(mana_cost_str: str,
                         mana_by_color: Dict[str, int],
                         any_color_mana: int,
                         additional_generic: int = 0
                         ) -> Tuple[bool, Dict[str, int], int]:
    """Try to pay a cost from the simulated pool. If payable, MUTATES the
    pool in-place to reflect the spend and returns (True, mana_by_color,
    any_color_mana). Otherwise returns (False, ...) without changes.
    Conservative: parses {W}/{U}/{B}/{R}/{G}/{C} literally; treats hybrid
    and phyrexian as 1 generic; honors X by ignoring it (planner doesn't
    know X yet, and X-cost rejection is handled upstream).
    """
    cost = (mana_cost_str or '').upper()
    # Try to use the rules.mana parser for proper hybrid/phyrexian handling.
    try:
        if HAS_MANA_ENGINE:
            parsed = ManaCost.parse(mana_cost_str or '')
            color_reqs = {c.value: n for c, n in parsed.color_requirements.items()}
            generic_needed = (parsed.generic_requirement or 0) + additional_generic
        else:
            raise ImportError
    except Exception:
        # Inline fallback — count {color} symbols and digits/X.
        color_reqs = {c: cost.count(f'{{{c}}}') for c in ('W', 'U', 'B', 'R', 'G', 'C')}
        generic_needed = additional_generic
        for sym in re.findall(r'\{([^}]+)\}', cost):
            if sym.isdigit():
                generic_needed += int(sym)
            elif sym in ('X',):
                pass  # X unknown — ignore for simulation
            elif sym not in ('W', 'U', 'B', 'R', 'G', 'C'):
                generic_needed += 1  # hybrid/phyrexian/snow → treat as 1 generic

    # Snapshot for rollback.
    snapshot = dict(mana_by_color)
    flex = any_color_mana

    # Phase 1: pay colored requirements from same-color, then 'any'.
    for color, needed in color_reqs.items():
        if needed <= 0:
            continue
        have = mana_by_color.get(color, 0)
        if have >= needed:
            mana_by_color[color] = have - needed
            continue
        mana_by_color[color] = 0
        shortfall = needed - have
        if shortfall > flex:
            mana_by_color.clear(); mana_by_color.update(snapshot)
            return False, mana_by_color, any_color_mana
        flex -= shortfall

    # Phase 2: pay generic from any available source (prefer C, then 'any',
    # then colored to preserve color flexibility for later casts).
    if generic_needed > 0:
        # Colorless first.
        take_c = min(generic_needed, mana_by_color.get('C', 0))
        mana_by_color['C'] -= take_c
        generic_needed -= take_c
        if generic_needed > 0:
            take_any = min(generic_needed, flex)
            flex -= take_any
            generic_needed -= take_any
        # Colored last.
        for color in ('W', 'U', 'B', 'R', 'G'):
            if generic_needed <= 0:
                break
            take = min(generic_needed, mana_by_color.get(color, 0))
            mana_by_color[color] -= take
            generic_needed -= take
        if generic_needed > 0:
            mana_by_color.clear(); mana_by_color.update(snapshot)
            return False, mana_by_color, any_color_mana

    return True, mana_by_color, flex


_BASIC_LAND_COLORS = {
    'plains': 'W', 'island': 'U', 'swamp': 'B', 'mountain': 'R', 'forest': 'G',
    'wastes': 'C',
    'snow-covered plains': 'W', 'snow-covered island': 'U',
    'snow-covered swamp': 'B', 'snow-covered mountain': 'R',
    'snow-covered forest': 'G',
}


def _validate_plan_mana(engine, game: GameState, player_idx: int, plan: list) -> list:
    """Pre-validate a plan_turn action sequence against available mana.

    Simulates sequential mana spending without modifying game state.
    Strips actions that would exceed available mana or reference cards not in hand.
    Prevents the common DeepSeek failure where it plans 3 spells but only has
    mana for 1, triggering a costly fallback to per-action mode.

    May 13 audit: now color-aware. Previously this checked CMC vs total mana,
    which let plans through that the engine then rejected on color mismatch
    (~50 "need N mana, only have 0" fallbacks per batch when the planner
    committed all available {W} on the first cast and then planned a second
    {W} cast). The simulation tracks per-color mana so the second cast gets
    rejected before the engine has to.
    """
    player = game.players[player_idx]
    # Count actual mana production, not just number of sources.
    # Sol Ring counts as 2, not 1. Tapped sources count as 0.
    available_mana = sum(
        sum(player._get_mana_production(c).values())
        for c in player.battlefield
        if (c.is_land() or player._can_produce_mana(c)) and not c.tapped
    )
    # Color-aware mirror for the new simulation. Kept alongside the colorless
    # `mana_remaining` counter so the existing rejection messages still work.
    mana_by_color, any_color_mana = _initial_color_pool(player)
    hand_names = {c.name.lower() for c in player.hand}
    # July 31 batch-11: the executors accept adventure-half names ("cast
    # Fertile Footsteps" casts Beanstalk Giant's adventure half), so the
    # validator must recognize them too — otherwise half-name plans die
    # here as "not in hand" while the executor would have accepted them.
    for c in player.hand:
        if getattr(c, 'adventure_name', None):
            hand_names.add(c.adventure_name.lower())
    # Also include command zone — commanders can be cast from there.
    # _validate_plan_mana must not reject "cast Commander" actions generated by plan_turn().
    if game.format in COMMAND_ZONE_FORMATS:
        for c in player.command_zone:
            hand_names.add(c.name.lower())
    # Aug 3, 2026 — the graveyard and exile are cast sources too, and this
    # validator knew about neither. Every card the castable list OFFERS from
    # those zones (flashback, escape, jump-start, aftermath, Snapcaster
    # grants, adventure halves in exile, impulse exiles, foretold cards) was
    # dropped here as "not in hand" before the executor ever saw it, so the
    # whole class was reachable only from the per-action fallback path.
    # Pre-existing for flashback/escape; the wave-3 mechanics all land in the
    # same hole, so widening the set is what makes them reachable from
    # plan_turn at all. The pricing below reads `player.hand` for the cost, so
    # `_alt_zone_cards` carries the objects for those names.
    _alt_zone_cards = {}
    for c in player.graveyard:
        if c.id in (getattr(player, 'playable_from_graveyard', None) or []):
            hand_names.add(c.name.lower())
            _alt_zone_cards[c.name.lower()] = c
            # An aftermath half is offered — and cast — under the HALF name.
            for _sname in (getattr(c, 'split_names', None) or []):
                if _sname:
                    hand_names.add(_sname.lower())
                    _alt_zone_cards[_sname.lower()] = c
    for c in (getattr(player, 'exile', None) or []):
        if is_castable_from_exile(game, player, c):
            hand_names.add(c.name.lower())
            _alt_zone_cards[c.name.lower()] = c
    # Aug 7 Q3 adversarial review (#3): Draugr-permitted cards sit in the
    # OPPONENT'S exile — without this loop the offer appeared, the model
    # proposed the cast, and this validator dropped it as "not in hand"
    # before any executor ran (the exact Aug-3 trap the comment above
    # records; the feature was decorative on Claude's primary path).
    for _op in (getattr(game, 'players', None) or []):
        if _op is player:
            continue
        for c in (getattr(_op, 'exile', None) or []):
            if (getattr(c, '_castable_by_player', None) == player.name
                    and is_castable_from_exile(game, player, c)):
                hand_names.add(c.name.lower())
                _alt_zone_cards[c.name.lower()] = c
    # The TOP CARD of the library, when something grants casting from there
    # (Augur of Autumn's coven half). Without this the castable list offers
    # the card and the plan validator immediately drops it as "not in hand"
    # — the exact gap that made the alternate-cost mechanics decorative
    # until July 28.
    try:
        _top_types = library_top_cast_types(player, game)
    except (AttributeError, TypeError):
        _top_types = set()
    if _top_types and getattr(player, 'library', None):
        _top_card = player.library[0]
        # `not is_land()` matches the offer and all three executors. It is
        # NOT cosmetic here: hand_names also admits `play_land`, so without
        # it a Dryad Arbor on top would let a planned land-drop pass
        # validation, consume the turn's land drop and credit a mana that
        # never arrives, while every executor still refuses the action.
        if ('creature' in _top_types
                and 'creature' in (getattr(_top_card, 'type_line', '') or '').lower()
                and not _top_card.is_land()):
            hand_names.add(_top_card.name.lower())
            # setdefault, not assignment: a graveyard/exile copy of the same
            # name was registered above and carries the escape/flashback cost
            # this one does not. Overwriting it would price an alternative-cost
            # cast at the printed cost in any 4-of format.
            _alt_zone_cards.setdefault(_top_card.name.lower(), _top_card)
    land_played = player.lands_played_this_turn >= player.max_lands_per_turn

    validated = []
    mana_remaining = available_mana
    _activated_pws_this_plan = set()  # Track PWs activated in this plan to reject duplicates
    # May 14 audit: AI generated a plan like "cast Meren → activate Altar of
    # Dementia (sacrifice: Meren)" — paying 4 mana to put the just-cast
    # commander back in the command zone for 3 mill. Track commanders cast
    # this plan and reject any same-plan sacrifice targeting them unless the
    # sac unlocks a clearly larger payoff (we just block the immediate sac).
    _commanders_cast_this_plan = set()
    _commander_names_in_zone = set()
    if game.format in COMMAND_ZONE_FORMATS:
        _commander_names_in_zone = {c.name.lower() for c in player.command_zone
                                    if getattr(c, 'is_commander', False)}

    # May 14 audit (A5): collect rejection reasons during validation and surface
    # them in the next plan_turn prompt so the AI learns not to retry the same
    # illegal action. PLAN-VALIDATE rate was 7.2/game (up from ~5 baseline) in
    # May 14 batch; many of those were repeated proposals of the same illegal
    # cast across turns. Recorded reasons are deduped by card_name (most-recent
    # wins), bounded at 10 entries per game, and tagged with turn number so
    # we can prune rejections that are >3 turns old (state may have changed).
    current_turn = getattr(game, 'turn_number', 0)
    # May 16 audit: 3-turn TTL was too lenient for repeat offenders. In the
    # May 15 batch, `cast Walking Ballista X=0` was re-proposed 4+ times in
    # one game despite each rejection — the actor kept "forgetting" the
    # rejection after 3 turns. New tier: once a (card, reason) combo has
    # been rejected 3 times, it becomes a PERMANENT ban for the rest of
    # this game and persists past the 3-turn pruning.
    if not hasattr(game, '_persistent_plan_bans'):
        # Set of (card_name_lower, reason_short) tuples — never pruned.
        game._persistent_plan_bans = set()
    if not hasattr(game, '_plan_rejection_counts'):
        # Map (card_name_lower, reason_short) → count of times rejected.
        game._plan_rejection_counts = {}
    if hasattr(game, '_recent_plan_rejections'):
        # Drop entries older than 3 turns UNLESS the (card, reason) is on
        # the permanent ban list — those should keep surfacing forever.
        game._recent_plan_rejections = [
            entry for entry in game._recent_plan_rejections
            if (current_turn - entry[2] <= 3
                or (entry[0].lower(), entry[1]) in game._persistent_plan_bans)
        ]

    def _record_rejection(card_or_perm: str, reason: str) -> None:
        if not card_or_perm:
            return
        if not hasattr(game, '_recent_plan_rejections'):
            game._recent_plan_rejections = []  # list of (card_name, reason, turn)
        bucket = game._recent_plan_rejections
        # Track count for permanent-ban escalation.
        key = (card_or_perm.lower(), reason)
        counts = game._plan_rejection_counts
        counts[key] = counts.get(key, 0) + 1
        if counts[key] >= 3 and key not in game._persistent_plan_bans:
            game._persistent_plan_bans.add(key)
            print(f"[PLAN-VALIDATE] PERMANENT BAN this game: {card_or_perm} — {reason} "
                  f"(rejected 3x already)")
        # Replace existing entry for this card; otherwise append.
        for i, (cn, _r, _t) in enumerate(bucket):
            if cn.lower() == card_or_perm.lower():
                bucket[i] = (card_or_perm, reason, current_turn)
                return
        bucket.append((card_or_perm, reason, current_turn))
        if len(bucket) > 10:
            # Don't evict permanent bans when bucket overflows — drop oldest
            # non-permanent entry instead.
            for i, (cn, r, _t) in enumerate(bucket):
                if (cn.lower(), r) not in game._persistent_plan_bans:
                    del bucket[i]
                    return
            # All entries are permanent — just drop the oldest.
            del bucket[0]

    def _reject(action_type: str, card_or_perm: str, reason_short: str) -> None:
        """Print a [PLAN-VALIDATE] Rejected line AND record for next-turn feedback."""
        full_msg = f"[PLAN-VALIDATE] Rejected {action_type} {card_or_perm} — {reason_short}"
        print(full_msg)
        _record_rejection(card_or_perm, reason_short)

    # May 17 audit: when a `cast` action is rejected by plan-validate, drop any
    # immediately-following `resolve` action that was paired with it. The AI
    # often plans `[cast Supreme Verdict, resolve: "Destroy all creatures…", …]`
    # and if the cast fails for mana/colored mismatch, the trailing resolve
    # description would otherwise be routed through resolve_effect and apply
    # arbitrary state changes (free board wipes, free damage). Track whether
    # the previous step rejected a cast and skip the orphaned resolve.
    _prev_cast_rejected = False

    # May 20 audit extensions:
    #   (#3) If a cast SUCCEEDS but the card has a tier 1.5 template, the
    #        template will handle the ETB. The AI-planned paired resolve is
    #        then a redundant orphan — `resolve_effect` would interpret the
    #        description AGAIN and double-apply (Craterhoof:
    #        game_1506604518098342018:2290-2327 → +6/+6 template then +10/+10
    #        judge re-resolve then "Attack for lethal" 999-damage hammer).
    #   (#4) Reject resolve actions that have NO immediately-prior cast in
    #        this plan (game_1506614360322080899:431-448 — orphan resolve
    #        "Deal 2 with Lightning Bolt" fired BEFORE the LB cast attempt,
    #        applying free damage; LB cast then failed for insufficient mana).
    _prev_cast_had_template = False
    _prev_cast_was_successful = False  # True only between cast-accept and next action

    # Cheap one-shot lookup of the template library for tier_for_card checks.
    try:
        from rules.effect_templates import get_effect_library
        _template_lib = get_effect_library()
    except Exception:
        _template_lib = None

    for action in plan:
        a_type = action.get("type", "")
        card_name = action.get("card", "")

        if a_type == "resolve":
            desc_preview = (action.get("description") or "")[:80]
            if _prev_cast_rejected:
                print(f"[PLAN-VALIDATE] Dropped orphaned resolve action — "
                      f"paired cast was rejected (would have applied free effects): '{desc_preview}'")
                _prev_cast_rejected = False
                _prev_cast_had_template = False
                _prev_cast_was_successful = False
                continue
            if _prev_cast_had_template:
                print(f"[PLAN-VALIDATE] Dropped redundant resolve — paired cast has tier 1.5 "
                      f"template that already handles the ETB: '{desc_preview}'")
                _prev_cast_had_template = False
                _prev_cast_was_successful = False
                continue
            if not _prev_cast_was_successful:
                print(f"[PLAN-VALIDATE] Dropped orphan resolve — no immediately-prior successful "
                      f"cast in this plan (would apply free effects): '{desc_preview}'")
                continue
            # Accepted — resolve immediately follows a successful cast with
            # no template. Consume the success flag so a SECOND resolve right
            # after the same cast doesn't slip through.
            _prev_cast_was_successful = False
            validated.append(action)
            continue

        # Any non-resolve action breaks the cast-resolve pairing.
        _prev_cast_rejected = False
        _prev_cast_had_template = False
        _prev_cast_was_successful = False
        # Optimistically assume cast will be rejected; the success path below
        # resets this to False right before `validated.append(action)`. Any
        # `continue` that bypasses the success path will leave the flag True,
        # which drops the next resolve action (if any).
        if a_type == "cast":
            _prev_cast_rejected = True

        # Apr 29 audit: DeepSeek-V4-Pro sometimes leaks 800+ char chain-of-thought
        # into the card-name field ("Gray Merchant of Asphodel from graveyard
        # with flashback? Reasoning: ..."). Reject anything that doesn't look
        # like a real card name. MTG cards top out around 35 chars (longest:
        # "Our Market Research Shows That Players Like Really Long Card Names").
        if card_name and (len(card_name) > 60 or any(p in card_name for p in '?:;\n')):
            preview = card_name[:60].replace('\n', ' ') + ('…' if len(card_name) > 60 else '')
            print(f"[PLAN-VALIDATE] Rejected {a_type} — card name looks like prose, not a card: '{preview}'")
            continue

        if a_type == "pass":
            validated.append(action)
            break
        elif a_type == "play_land":
            if land_played:
                # May 18 audit: the May 16 design said plan-stale (already-
                # played-land) rejections are "snapshot mismatches that fix
                # themselves" so DON'T record. But the May 17 batch showed
                # 9+ re-proposals of the same already-played land in one game
                # (`game_1505768865223938089`, Forest re-proposed turns 5/14/
                # 19/22/25/...). The actor isn't reading lands_played_this_turn
                # from the state — surface it via the rejection feedback loop
                # so the next plan_turn prompt explicitly says "you already
                # played a land this turn".
                _reject("play_land", card_name or "<unknown>",
                        "already played a land this turn — pass instead, or hold lands for ramp triggers")
                continue
            if card_name and card_name.lower() not in hand_names:
                # Land-not-in-hand is the SAME root cause as already-played
                # (the land is on the battlefield, not in hand). Record so
                # the actor stops re-proposing.
                _reject("play_land", card_name or "<unknown>",
                        "not in hand (likely already on the battlefield)")
                continue
            validated.append(action)
            land_played = True
            mana_remaining += 1
            # Color-aware: bump the simulated pool by the land's production.
            # Use basic-land table first (fast + works pre-battlefield), then
            # fall back to _get_mana_production on the in-hand Card object —
            # _get_mana_production reads name/type, not zone, so it gives us
            # the right answer for non-basics already in the player's hand.
            try:
                land_card = next((c for c in player.hand if c.name.lower() == (card_name or '').lower()), None)
                if land_card is not None:
                    basic_color = _BASIC_LAND_COLORS.get((card_name or '').lower())
                    if basic_color:
                        if basic_color == 'C':
                            mana_by_color['C'] = mana_by_color.get('C', 0) + 1
                        else:
                            mana_by_color[basic_color] = mana_by_color.get(basic_color, 0) + 1
                    else:
                        production = player._get_mana_production(land_card) or {}
                        for color, amt in production.items():
                            if color == 'any':
                                any_color_mana += amt
                            elif color in mana_by_color:
                                mana_by_color[color] += amt
            except Exception:
                pass
            if card_name:
                hand_names.discard(card_name.lower())
        elif a_type == "cast":
            if card_name and card_name.lower() not in hand_names:
                # PLAN-STALE rejections (card not actually in hand) are
                # snapshot mismatches that fix themselves — don't record.
                print(f"[PLAN-VALIDATE] Rejected cast {card_name} — not in hand")
                continue
            card_obj = next((c for c in player.hand if c.name.lower() == (card_name or '').lower()), None)
            # July 31 batch-11: resolve adventure-half names too — both
            # executors accept "cast Fertile Footsteps" (Beanstalk Giant's
            # adventure), so the validator must price the half being cast,
            # not reject it or price the combined string.
            _adv_half_cost = None
            if card_obj is None and card_name:
                for c in player.hand:
                    if (getattr(c, 'adventure_name', '') or '').lower() == card_name.lower():
                        card_obj = c
                        _adv_half_cost = getattr(c, 'adventure_cost', '') or ''
                        break
            # Graveyard / exile cast sources (see _alt_zone_cards above).
            _alt_cost = None
            if card_obj is None and card_name:
                _alt = _alt_zone_cards.get(card_name.lower())
                if _alt is not None:
                    card_obj = _alt
                    # Price the ALTERNATIVE cost, not the printed one — these
                    # are overwhelmingly cheaper (Terminus {4}{W}{W} → {W}),
                    # so pricing the printed cost would reject casts the
                    # payment stage can comfortably pay.
                    _alt_cost = (getattr(_alt, '_escape_cost', '')
                                 or getattr(_alt, '_flashback_cost', '')
                                 or getattr(_alt, '_foretell_cost', ''))
                    if not _alt_cost:
                        # An aftermath half is named and priced by its half.
                        for _i, _sname in enumerate(getattr(_alt, 'split_names', None) or []):
                            if _sname.lower() == card_name.lower():
                                _costs = getattr(_alt, 'split_costs', None) or []
                                if _i < len(_costs):
                                    _alt_cost = _costs[_i]
                                break
            if card_obj:
                cost = card_obj.cmc or 0
                _sim_cost = card_obj.mana_cost
                if _alt_cost:
                    from mtg.helpers import cmc_of_cost_string as _cmc_of
                    _sim_cost = _alt_cost
                    cost = _cmc_of(_alt_cost)
                # An adventure card's mana_cost is the combined
                # "{creature} // {adventure}" string. A bare-name cast is
                # the creature half (CR 715.2b); a half-name cast is the
                # adventure half. Price and simulate ONLY that half —
                # ManaCost.parse on the combined string charges both
                # (Flaxen Intruder read as CMC 8 in batch 15325).
                if getattr(card_obj, 'adventure_name', None) and ' // ' in (_sim_cost or ''):
                    from mtg.helpers import cmc_of_cost_string
                    _half = (_adv_half_cost if _adv_half_cost
                             else _sim_cost.split(' // ')[0])
                    _sim_cost = _half
                    cost = cmc_of_cost_string(_half)
                if cost > mana_remaining:
                    # Plan-stale (snapshot mismatch). Don't record.
                    print(f"[PLAN-VALIDATE] Rejected cast {card_name} (CMC {cost}) — only {mana_remaining} mana left")
                    continue
                # Color-aware: try to actually pay the cost from the simulated
                # pool. If we can't satisfy the colored requirements (even
                # after spending `any`-color mana to fill gaps), reject — the
                # engine would otherwise fail mid-plan with "Not enough <color>
                # mana" and force a fallback to per-action mode.
                # Aug 7 Q3 adversarial review (#3): a Draugr-permitted card
                # pays with snow-as-any-color, which the per-color pool
                # simulation cannot express — it rejected the cast as a
                # colored mismatch right after the offer appeared. Defer to
                # the REAL spending-aware check and deduct the cost as
                # fungible (exactly right when the payment is all-snow;
                # plan simulation is approximate by design elsewhere too —
                # the splice note).
                _is_draugr_cast = (
                    getattr(card_obj, '_snow_as_any_color', False)
                    and getattr(card_obj, '_castable_by_player', None)
                    == player.name)
                if (_is_draugr_cast and player.can_pay_mana_cost(
                        _sim_cost, spending_card=card_obj)[0]):
                    _rem = cost
                    _take = min(_rem, any_color_mana)
                    any_color_mana -= _take
                    _rem -= _take
                    for _c in list(mana_by_color):
                        if _rem <= 0:
                            break
                        _take = min(_rem, mana_by_color[_c])
                        mana_by_color[_c] -= _take
                        _rem -= _take
                    ok = True
                else:
                    ok, mana_by_color, any_color_mana = _simulate_cast_spend(
                        _sim_cost, mana_by_color, any_color_mana
                    )
                if not ok:
                    # May 17 audit: previously a bare print with the full
                    # mana-pool dict in the reason — that made the
                    # `_record_rejection` key non-canonical (pool dict varied
                    # turn-to-turn) so the permanent-ban counter NEVER
                    # accumulated for colored-mana-mismatch. Now record with
                    # a canonical reason keyed off the COST only.
                    print(f"[PLAN-VALIDATE] Rejected cast {card_name} — colored mana mismatch "
                          f"(cost {card_obj.mana_cost}, pool {mana_by_color} + {any_color_mana} any)")
                    _record_rejection(card_name, f"colored mana mismatch (need {card_obj.mana_cost})")
                    continue
                # July 31 batch-11 AI-quality: a main-phase plan can never
                # see attackers (the plan executes at sorcery speed, before
                # or after combat), so "return all attacking creatures"
                # casts are guaranteed dead — Claude burned Aetherize on an
                # attacker-less board (game_1532536842312876133, 0 bounced).
                # Strategic hold, recorded so the AI stops re-proposing.
                _oracle_hold = (card_obj.oracle_text or '').lower()
                if 'return all attacking creatures' in _oracle_hold:
                    print(f"[PLAN-VALIDATE] Rejected cast {card_name} — "
                          f"attacker-mass-bounce in a main phase (no attackers exist)")
                    _reject("cast", card_name,
                            "hold for the opponent's combat — attackers only exist during their attack")
                    continue
                # Block counterspells when stack is empty (CR 601.2c)
                oracle_lower = (card_obj.oracle_text or '').lower()
                is_counterspell = 'counter target' in oracle_lower and 'spell' in oracle_lower
                # A creature with counter-in-oracle is cast for its body; even if
                # the ETB counter fizzles on empty stack, the creature still enters.
                # Only instants/sorceries should be rejected when stack is empty.
                is_creature_with_counter_etb = card_obj.is_creature()
                # [FIX-3] Modal spells that ALSO have non-counter modes (draw, bounce, tap)
                # should not be blocked just because countering requires a stack target.
                # These cards are still castable for their other modes.
                MODAL_SPELLS_WITH_NON_COUNTER_MODES = {
                    "Mystic Confluence", "Archmage's Charm", "Cryptic Command",
                    "Fuel for the Cause", "Rewind", "Absorb", "Sinister Sabotage",
                    "Sublime Epiphany", "Commit // Memory",
                }
                is_modal_with_other_modes = (
                    card_name in MODAL_SPELLS_WITH_NON_COUNTER_MODES
                    or (
                        # Heuristic: modal bullet list where at least one bullet lacks "counter target spell"
                        '•' in oracle_lower
                        and any(kw in oracle_lower for kw in ['draw a card', 'return target', 'tap target'])
                    )
                )
                if is_counterspell and not is_creature_with_counter_etb and not is_modal_with_other_modes and not game.stack:
                    _reject("cast", card_name, "counterspell with empty stack (cast at instant speed during opponent's turn instead)")
                    continue
                # Block targeted removal when no opposing targets exist.
                # Apr 29 audit: only treat instants/sorceries as targeted
                # removal — planeswalkers (Jace, the Mind Sculptor's [-1]),
                # creatures with ETB triggers, enchantments with situational
                # target abilities are NOT removal spells. Their primary effect
                # is to enter the battlefield; the target text is conditional.
                opp = game.players[1 - player_idx]
                opp_creatures = [c for c in opp.battlefield if c.is_creature()]
                card_type_line = (getattr(card_obj, 'type_line', '') or '').lower()
                is_instant_or_sorcery = (
                    'instant' in card_type_line or 'sorcery' in card_type_line
                )
                is_targeted_removal = (
                    is_instant_or_sorcery
                    and ('destroy target' in oracle_lower or 'exile target' in oracle_lower
                         or 'target creature' in oracle_lower)
                )
                # Exclude spells targeting own graveyard (Victimize, Reanimate, etc.)
                # and spells targeting own creatures (pump spells, etc.)
                targets_own_gy = 'graveyard' in oracle_lower and 'your graveyard' in oracle_lower
                # June 11 audit: self-flicker spells ("Exile target creature
                # you control, then return it" — Ephemerate, Momentary Blink)
                # contain 'exile' and were classified as removal, then
                # rejected for "no opponent creatures" 4x in one game.
                _is_self_flicker = ('target creature you control' in oracle_lower
                                    and 'return' in oracle_lower)
                targets_own_creatures = (
                    'you control' in oracle_lower and 'target creature' in oracle_lower
                    and (('destroy' not in oracle_lower and 'exile' not in oracle_lower)
                         or _is_self_flicker))

                # "Destroy target creature. Its controller creates an X/X" spells
                # (Rapid Hybridization, Pongify, Beast Within). Reject casting
                # these on OWN creatures — you're paying mana to swap a card
                # for a 3/3, which is almost always a misplay unless the
                # creature is about to die to a board wipe (rare enough that
                # it's safer to block it at the planner).
                explicit_tgt_name = action.get("target")
                is_hybridization_effect = (
                    'destroy target' in oracle_lower
                    and 'controller creates' in oracle_lower
                )
                if is_hybridization_effect and explicit_tgt_name:
                    # July 31 batch-11 (brawl mirror): name-only matching
                    # against the caster's OWN battlefield misfired when the
                    # OPPONENT also controlled a same-named permanent — Beast
                    # Within at Rick's Dryad of the Ilysian Grove was
                    # rejected as "targeting own creature" because Claude had
                    # his own Dryad (game_1532532200061403350; the executor's
                    # detrimental-target inference later picked the right
                    # one). Only classify as own-targeting when the caster is
                    # the ONLY controller of that name.
                    _opp_p = game.players[1 - player_idx]
                    _opp_has_name = any(
                        _c.name.lower() == explicit_tgt_name.lower()
                        and _c.is_creature() for _c in _opp_p.battlefield)
                    _own_has_name = any(
                        _c.name.lower() == explicit_tgt_name.lower()
                        and _c.is_creature() for _c in player.battlefield)
                    if _own_has_name and not _opp_has_name:
                        print(f"[PLAN-VALIDATE] Rejected cast {card_name} — "
                              f"hybridize-style spell targeting own creature "
                              f"({explicit_tgt_name})")
                        continue
                    # May 23 audit (MAJOR #10): even when targeting opponent's
                    # creature, Rapid Hybridization / Pongify / Beast Within
                    # upgrade a small creature (1/1 Llanowar Elves) into a
                    # 3/3 token for the opponent — net board-state loss. Reject
                    # when the target's power+toughness is below 4 (the 3/3
                    # token replacement is worth 6; we want to be removing
                    # something worth MORE than the token we give back).
                    _hybridize_reject_small = False
                    if explicit_tgt_name:
                        _tgt_card = None
                        # July 31 batch-11: prefer the OPPONENT's copy when
                        # both sides control the name — detrimental spells
                        # target the opponent, and reading the caster's copy
                        # here would score the wrong creature's P/T.
                        for _p in (_opp_p, player):
                            for _c in _p.battlefield:
                                if _c.name.lower() == explicit_tgt_name.lower() and _c.is_creature():
                                    _tgt_card = _c
                                    break
                            if _tgt_card:
                                break
                        if _tgt_card is not None:
                            def _safe_int(v):
                                try:
                                    return int(v)
                                except (TypeError, ValueError):
                                    return 0
                            tgt_power = _safe_int(getattr(_tgt_card, 'power', 0))
                            tgt_tough = _safe_int(getattr(_tgt_card, 'toughness', 0))
                            if tgt_power + tgt_tough < 4:  # smaller than a 3/3 token
                                _reject("cast", card_name,
                                        f"hybridize-shape removal on small target ({_tgt_card.name}, "
                                        f"{tgt_power}/{tgt_tough}) — opponent gets a 3/3 token "
                                        f"which is net board-state loss")
                                _hybridize_reject_small = True
                    if _hybridize_reject_small:
                        continue

                if is_targeted_removal and not opp_creatures and not targets_own_gy and not targets_own_creatures:
                    # Check if it can also target non-creatures (artifact/enchantment/permanent)
                    targets_artifacts = 'artifact' in oracle_lower
                    targets_enchantments = 'enchantment' in oracle_lower
                    targets_any_permanent = 'permanent' in oracle_lower

                    opp_artifacts = [c for c in opp.battlefield if 'artifact' in getattr(c, 'type_line', '').lower()]
                    opp_enchantments = [c for c in opp.battlefield if 'enchantment' in getattr(c, 'type_line', '').lower()]

                    # Spell targets only creatures — no valid targets
                    if not targets_artifacts and not targets_enchantments and not targets_any_permanent:
                        _reject("cast", card_name, "targeted removal with no opponent creatures on board")
                        continue
                    # Spell targets artifacts/enchantments only — check if any exist
                    if (targets_artifacts or targets_enchantments) and not targets_any_permanent:
                        has_valid_noncreat = (
                            (targets_artifacts and opp_artifacts) or
                            (targets_enchantments and opp_enchantments)
                        )
                        if not has_valid_noncreat:
                            _reject("cast", card_name, "targets artifact/enchantment but opponent controls none")
                            continue
                # NOTE: We do NOT reject devotion-based ETB creatures (Mogis's Marauder, etc.)
                # even when devotion is 0. The non-devotion half of the effect (e.g. intimidate
                # for Mogis) still resolves. Rejecting on devotion would incorrectly block valid plays.

                # Block board wipes on empty boards
                is_board_wipe = ('destroy all creatures' in oracle_lower or 'each creature' in oracle_lower)
                if is_board_wipe and not card_obj.is_creature():
                    total_creatures = sum(len([c for c in p.battlefield if c.is_creature()]) for p in game.players)
                    if total_creatures == 0:
                        _reject("cast", card_name, "board wipe on empty board — hold for after opponent deploys threats")
                        continue
                    # June 10 audit (AI quality): wipes whose ONLY casualties
                    # would be the caster's own creatures (Wrath killing own
                    # commander vs one token; Supreme Verdict killing only own
                    # Snapcaster). Recorded via _reject so the feedback loop
                    # stops the re-propose.
                    _opp_creatures = sum(
                        len([c for c in p.battlefield if c.is_creature()])
                        for p in game.players if p is not player)
                    _own_creatures = total_creatures - _opp_creatures
                    if _opp_creatures == 0 and _own_creatures > 0:
                        _reject("cast", card_name,
                                "board wipe would destroy ONLY your own creatures — "
                                "hold until the opponent has a board")
                        continue

                # June 10 audit (Searing Blaze): burn that REQUIRES a creature
                # target ("deals N damage to target creature") wastes the cast
                # when the opponent has no creatures — the engine legally
                # allows targeting your OWN creature (CR 601.2c), then the
                # template refuses the friendly target and the spell fizzles
                # AFTER mana payment. Hold it at the planner instead.
                if (re.search(r'deals? \d+ damage to target creature', oracle_lower)
                        and 'any target' not in oracle_lower
                        and 'target creature or' not in oracle_lower):
                    _opp_creats2 = sum(
                        len([c for c in p.battlefield if c.is_creature()])
                        for p in game.players if p is not player)
                    if _opp_creats2 == 0:
                        _reject("cast", card_name,
                                "requires a creature target and opponent controls none — "
                                "hold until a creature appears")
                        continue
                # May 23 audit (MAJOR #9): block reanimate-shape spells when
                # own graveyard lacks a legal target. Victimize, Reanimate,
                # Stitch Together, Necromancy, Animate Dead, Beacon of Unrest,
                # etc. require a creature card in a graveyard (own or any).
                # Per CR 601.2c the spell can't even be cast without a legal
                # target; the engine accepts the cast and then fizzles on
                # resolution, wasting the mana payment.
                requires_own_gy_creature = (
                    'target creature card from your graveyard' in oracle_lower
                    or 'target creature card in your graveyard' in oracle_lower
                )
                requires_any_gy_creature = (
                    'target creature card from a graveyard' in oracle_lower
                    or 'target creature card in a graveyard' in oracle_lower
                )
                # Victimize's full pattern: "Sacrifice a creature, then return
                # TWO target creature cards from your graveyard."
                requires_two_own_gy_creatures = (
                    'two target creature cards from your graveyard' in oracle_lower
                )
                if requires_own_gy_creature or requires_two_own_gy_creatures:
                    own_gy_creatures = sum(1 for c in player.graveyard if c.is_creature())
                    need = 2 if requires_two_own_gy_creatures else 1
                    if own_gy_creatures < need:
                        _reject("cast", card_name,
                                f"reanimate-shape spell needs {need}+ creature(s) in own graveyard "
                                f"(have {own_gy_creatures}) — self-mill / dies-trigger setup first")
                        continue
                elif requires_any_gy_creature:
                    any_gy_creatures = sum(
                        1
                        for p in game.players
                        for c in p.graveyard
                        if c.is_creature()
                    )
                    if any_gy_creatures == 0:
                        _reject("cast", card_name,
                                "reanimate-shape spell needs a creature in some graveyard "
                                "(all graveyards empty)")
                        continue
                # Victimize-specific: also requires sacrificeable creature on
                # battlefield (the additional cost is "Sacrifice a creature").
                if 'sacrifice a creature' in oracle_lower and (card_obj.is_instant() or card_obj.is_sorcery()):
                    has_sac_creature = any(
                        c.is_creature() and c is not card_obj
                        for c in player.battlefield
                    )
                    if not has_sac_creature:
                        _reject("cast", card_name,
                                "sacrifice-a-creature additional cost requires a creature on battlefield")
                        continue

                # Block spells with "sacrifice a land" additional cost if player has no lands
                if 'sacrifice a land' in oracle_lower:
                    has_land = any(c.is_land() for c in player.battlefield)
                    if not has_land:
                        print(f"[PLAN-VALIDATE] Rejected cast {card_name} — sacrifice a land cost but no lands")
                        continue
                # Block X=0 casts of X-cost spells (wasted cast — Blue Sun's
                # Zenith X=0 draws 0, Hydroid Krasis X=0 enters as 0/0).
                if card_obj.mana_cost and 'X' in card_obj.mana_cost.upper():
                    x_value = action.get("x_value") or action.get("X") or action.get("x")
                    try:
                        x_int = int(x_value) if x_value is not None else 0
                    except (ValueError, TypeError):
                        x_int = 0
                    if x_int <= 0:
                        _reject("cast", card_name, "X-cost spell with X<=0 (wasted cast — only propose when X>=2)")
                        continue
                # May 24 audit fix (#9): block noncreature spells at low life
                # vs Kambal-shape sources. Game game_1508070855652020275 showed
                # Claude at 3 life cast Anointed Procession into opp's Kambal,
                # Kambal trigger dealt 2 → Claude lost. CR-correct engine, but
                # the AI should never propose a play that's deterministic
                # self-loss. Scan opponent's battlefield for "whenever you cast
                # a noncreature spell, lose N life" patterns (Kambal: 2 life
                # loss) and extort-shaped sources (1 life loss when paid).
                # Threshold: life ≤ 2 × kambal_count + extort_count.
                if not card_obj.is_creature():
                    kambal_drain = 0
                    extort_drain = 0
                    for op in opp.battlefield:
                        op_oracle = (getattr(op, 'oracle_text', '') or '').lower()
                        if not op_oracle:
                            continue
                        # Kambal-shape: "whenever (an opponent / a player you don't
                        # control) casts a noncreature spell, that player loses N life"
                        if ('noncreature spell' in op_oracle
                                and ('opponent' in op_oracle or "don't control" in op_oracle)
                                and ('loses' in op_oracle or 'lose' in op_oracle) and 'life' in op_oracle):
                            # Pull the life-loss number if available, default 2 (Kambal).
                            import re as _re
                            m = _re.search(r'loses?\s+(\d+)\s+life', op_oracle)
                            kambal_drain += int(m.group(1)) if m else 2
                        # Extort-shape: "extort" keyword or "whenever you cast a spell
                        # ... opponent loses 1 life" (Blind Obedience).
                        if 'extort' in op_oracle or (
                            'whenever you cast' in op_oracle
                            and 'opponent' in op_oracle and 'loses 1 life' in op_oracle
                        ):
                            extort_drain += 1
                    total_drain = kambal_drain + extort_drain
                    if total_drain > 0 and player.life <= total_drain:
                        _reject("cast", card_name,
                                f"noncreature spell at {player.life} life vs opp's "
                                f"{total_drain}-life drain on cast (Kambal/extort) — "
                                f"would lose game on this cast")
                        continue
                mana_remaining -= cost
                hand_names.discard(card_name.lower())
                # May 14 audit: track commanders cast THIS plan so we can reject
                # an immediate same-plan sacrifice of them. The plan-EV of
                # "cast Commander → sacrifice Commander to free outlet for 3
                # mill" is strictly negative (pay 4+ mana, return to command
                # zone with +2 tax).
                if (card_obj.name.lower() in _commander_names_in_zone
                        or getattr(card_obj, 'is_commander', False)):
                    _commanders_cast_this_plan.add(card_obj.name.lower())
            validated.append(action)
            _prev_cast_rejected = False  # Cast accepted — paired resolve allowed
            _prev_cast_was_successful = True  # May 20: enables paired-resolve gate
            # May 20 audit (#3): if the just-accepted cast has a tier 1.5
            # template, the template will handle the ETB on resolution. The
            # AI-planned paired resolve action would then be a redundant
            # orphan re-fire — flag so the resolve handler at top of the
            # loop drops it before resolve_effect can over-apply.
            _prev_cast_had_template = False
            if _template_lib is not None and card_obj is not None:
                try:
                    tier = _template_lib.tier_for_card(
                        card_obj.name, card_obj.oracle_text or ""
                    )
                    if tier in ("template", "pattern"):
                        _prev_cast_had_template = True
                except Exception:
                    pass
        elif a_type in ("graveyard_activate", "foretell"):
            # Aug 3: these two DO fall through plan validation untouched (the
            # terminal else appends), but their mana was never simulated —
            # embalm/eternalize cost {5}{W} / {5}{G}{G} and foretell {2}, so a
            # later cast in the same plan validated against mana already
            # spent and then failed for real at the executor.
            if a_type == "foretell":
                _new_cost = "{2}"
            else:
                _gy_card = next(
                    (c for c in player.graveyard
                     if c.name.lower() == str(card_name or '').lower()), None)
                _parsed = None
                if _gy_card is not None:
                    from mtg.helpers import parse_graveyard_activation
                    _parsed = parse_graveyard_activation(_gy_card.oracle_text or '')
                _new_cost = _parsed[1] if _parsed else None
            if _new_cost:
                from mtg.helpers import cmc_of_cost_string as _cmc_of
                _cost_n = _cmc_of(_new_cost)
                if _cost_n > mana_remaining:
                    print(f"[PLAN-VALIDATE] Rejected {a_type} {card_name} "
                          f"(cost {_cost_n}) — only {mana_remaining} mana left")
                    continue
                _ok, mana_by_color, any_color_mana = _simulate_cast_spend(
                    _new_cost, mana_by_color, any_color_mana)
                if not _ok:
                    print(f"[PLAN-VALIDATE] Rejected {a_type} {card_name} — "
                          f"colored mana mismatch for {_new_cost}")
                    continue
                mana_remaining -= _cost_n
            validated.append(action)

        elif a_type == "activate":
            # Reject second activation of the same planeswalker in one turn
            perm_name = action.get("permanent", "").lower()
            perm_obj = next((c for c in player.battlefield if c.name.lower() == perm_name), None)

            # May 14 audit: if the activation sacrifices a commander we just cast
            # this plan, reject — that's a worst-EV play (pay 4+ mana, return
            # to command zone with +2 tax for a minor free-outlet payoff).
            sac_target = (action.get("sacrifice") or action.get("sac")
                          or action.get("target_sacrifice") or "")
            if isinstance(sac_target, str) and sac_target:
                if sac_target.lower() in _commanders_cast_this_plan:
                    _reject(
                        "activate", perm_name,
                        f"sacrifice {sac_target} — that commander was cast THIS turn "
                        f"(net negative EV: pay mana, return to command zone with +2 tax)"
                    )
                    continue

            # Estimate mana cost for the activated ability so the plan validator
            # accounts for equip costs (Trailblazer's Boots: Equip {2}) and
            # mana-plus-other-cost activations (Cathar Commando: {1}, Sacrifice).
            # Without this, later casts in the plan overestimate remaining mana.
            if perm_obj and not perm_obj.is_planeswalker():
                try:
                    ability_idx_val = action.get("ability", 0)
                    try:
                        ability_idx_val = int(ability_idx_val)
                    except (ValueError, TypeError):
                        ability_idx_val = 0
                    ability_cost_mana = 0
                    oracle_lines = (perm_obj.oracle_text or '').split('\n')
                    activatable_idx = 0
                    for line in oracle_lines:
                        ln = line.strip()
                        if not ln:
                            continue
                        equip_m = re.match(r'^Equip\s+((?:\{[^}]+\})+)', ln)
                        has_colon = ':' in ln
                        cost_part = None
                        if equip_m:
                            cost_part = equip_m.group(1)
                        elif has_colon:
                            cand = ln.split(':', 1)[0].strip()
                            if re.match(r'^[+-]?\d+$', cand):
                                continue  # Loyalty ability
                            if '{' in cand or any(k in cand for k in ('Sacrifice', 'Pay', 'Discard', 'Exile', 'Remove')):
                                cost_part = cand
                        if cost_part is None:
                            continue
                        if activatable_idx == ability_idx_val:
                            for sym in re.findall(r'\{([^}]+)\}', cost_part):
                                if sym in ('T', 'Q'):
                                    continue
                                if sym.isdigit():
                                    ability_cost_mana += int(sym)
                                else:
                                    ability_cost_mana += 1  # colored/hybrid/phyrexian ~= 1 mana
                            break
                        activatable_idx += 1
                    if ability_cost_mana > mana_remaining:
                        print(f"[PLAN-VALIDATE] Rejected activate {action.get('permanent')} — ability costs {ability_cost_mana}, only {mana_remaining} mana left")
                        continue
                    mana_remaining -= ability_cost_mana
                except Exception as e:
                    print(f"[PLAN-VALIDATE] Mana estimate for activate {action.get('permanent')} failed: {e}")
            if perm_obj and perm_obj.is_planeswalker():
                # Empty-target ability check: if the PW ability needs a target
                # permanent and none exists, reject. Catches Aminatou [-1]
                # (flicker) on empty battlefield, Daretti [-2] (sacrifice an
                # artifact) with no artifacts, etc.
                try:
                    ability_idx_val = action.get("ability", 0)
                    try:
                        ability_idx_val = int(ability_idx_val)
                    except (ValueError, TypeError):
                        ability_idx_val = 0
                    pw_oracle = (perm_obj.oracle_text or '')
                    pw_lines = [ln.strip() for ln in pw_oracle.split('\n') if ln.strip()]
                    abilities = [ln for ln in pw_lines if re.match(r'^[+−\-]?\d+\s*:', ln)]
                    if 0 <= ability_idx_val < len(abilities):
                        ability_text = abilities[ability_idx_val].lower()
                        # Targets the controller's own permanent (Aminatou flicker)
                        if 'another target permanent you control' in ability_text:
                            owner_perms = [c for c in player.battlefield if c is not perm_obj]
                            if not owner_perms:
                                print(f"[PLAN-VALIDATE] Rejected activate {action.get('permanent')} — no other permanent to target")
                                continue
                        # "target creature" — needs any creature on either side
                        elif 'target creature' in ability_text and 'opponent' not in ability_text:
                            any_creature = any(c.is_creature() for p_ in game.players for c in p_.battlefield)
                            if not any_creature:
                                print(f"[PLAN-VALIDATE] Rejected activate {action.get('permanent')} — no creature target available")
                                continue
                        # "target creature an opponent controls"
                        elif 'target creature an opponent controls' in ability_text:
                            opp_creatures = any(c.is_creature() for p_ in game.players if p_ is not player for c in p_.battlefield)
                            if not opp_creatures:
                                print(f"[PLAN-VALIDATE] Rejected activate {action.get('permanent')} — no opponent creature target")
                                continue
                        # "sacrifice an artifact" — needs an artifact (Daretti -2).
                        # Aug 7 batch audit (A-3): OPTIONAL or OR-alternative
                        # costs must not gate on the artifact half — Chandra,
                        # Spark Hunter's "+2: You MAY sacrifice an artifact OR
                        # DISCARD a card" was rejected with 8 cards in hand
                        # (game_1535051230815064206) while the same action
                        # succeeded via decide_action_inline (two-path
                        # divergence). Daretti's unconditional "-2: Sacrifice
                        # an artifact." carries neither marker.
                        elif ('sacrifice an artifact' in ability_text
                                and 'may sacrifice' not in ability_text
                                and 'or discard' not in ability_text):
                            owner_artifacts = [c for c in player.battlefield if c.is_artifact() and c is not perm_obj]
                            if not owner_artifacts:
                                print(f"[PLAN-VALIDATE] Rejected activate {action.get('permanent')} — no artifact to sacrifice")
                                continue
                except Exception as e:
                    print(f"[PLAN-VALIDATE] PW ability target-check failed: {e}")

                if perm_name in _activated_pws_this_plan:
                    # CR 606.3 normally limits PWs to one ability per turn, but
                    # effects like Oath of Teferi let a PW activate twice. Respect
                    # the engine's can_activate verdict before pruning the second
                    # in-plan activation.
                    # Aug 2 batch-13 (rashmi/mythic reviewer): this pre-check
                    # runs BEFORE any plan action executes, so can_activate is
                    # trivially True for an in-plan second activation (nothing
                    # has consumed the once-per-turn budget yet) — the old
                    # code read that as "an Oath of Teferi-class enabler must
                    # exist" and printed a confident lie. Only trust the
                    # verdict when a real multi-activation enabler is on the
                    # battlefield; otherwise prune the duplicate here (the
                    # execution gate blocked it anyway — pruning saves the
                    # wasted action + feeds the rejection loop).
                    _pw_enablers = ('oath of teferi', 'the chain veil')
                    _has_pw_enabler = any(
                        any(e in (c.name or '').lower() for e in _pw_enablers)
                        for c in player.battlefield)
                    pw_mgr = getattr(engine, 'planeswalker_manager', None)
                    allow_second = False
                    if pw_mgr and _has_pw_enabler:
                        ability_idx_val = action.get("ability", 0)
                        try:
                            ability_idx_val = int(ability_idx_val)
                        except (ValueError, TypeError):
                            ability_idx_val = 0
                        can_act, _why = pw_mgr.can_activate(game, player, perm_obj, ability_idx_val)
                        allow_second = bool(can_act)
                    if not allow_second:
                        _reject("activate", action.get('permanent', '?'),
                                "planeswalker already activated this turn (CR 606.3 — one loyalty ability per turn)")
                        continue
                    print(f"[PLAN-VALIDATE] Allowed 2nd activation of {action.get('permanent')} — multi-activation enabler on battlefield")
                # Bug 2b: also reject if the PW already used its once-per-turn activation
                # before this plan was generated (can happen when plan_turn is called
                # after a prior MAIN1 activation, or across split main phases).
                pw_mgr = getattr(engine, 'planeswalker_manager', None)
                if pw_mgr and pw_mgr.has_activated_this_turn(game, perm_obj):
                    # Respect Oath of Teferi's "twice per turn" exception by consulting
                    # can_activate for the requested ability index.
                    ability_idx_val = action.get("ability", 0)
                    try:
                        ability_idx_val = int(ability_idx_val)
                    except (ValueError, TypeError):
                        ability_idx_val = 0
                    can_act, why = pw_mgr.can_activate(game, player, perm_obj, ability_idx_val)
                    if not can_act and 'already activated' in (why or '').lower():
                        _reject("activate", action.get('permanent', '?'),
                                "planeswalker already activated this turn (cannot use again until next turn)")
                        continue
                _activated_pws_this_plan.add(perm_name)
            validated.append(action)
        else:
            validated.append(action)

    if not validated or validated[-1].get("type") != "pass":
        validated.append({"type": "pass"})

    removed = len(plan) - len(validated)
    if removed > 0:
        print(f"[PLAN-VALIDATE] Removed {removed} invalid action(s) from plan "
              f"({len(validated)} remaining, {mana_remaining} mana left)")
    return validated


async def execute_claude_turn(engine, game: GameState) -> List[str]:
    """Have Claude take its turn with retry on failed actions."""
    # Reset state description caches at turn boundary
    engine.claude_ai._cached_state_desc = None
    engine.claude_ai._cached_state_fingerprint = None
    engine.claude_ai._cached_hand_desc = None
    engine.claude_ai._cached_hand_hash = None

    claude_index = 0 if game.players[0].is_claude else 1
    print(f"[EXECUTE_CLAUDE] Called. claude_index={claude_index}, active_player_index={game.active_player_index}")

    # [STRATEGIST] Phase 2: Fire background strategist for THIS turn's board state.
    # The memo won't be ready for this turn's plan_turn() call (it's async),
    # but it'll be ready for the NEXT turn. One-turn stale is fine.
    import asyncio
    if not engine.claude_ai._api_disabled:
        try:
            from mtg.claude_player import _strategy_call_due
            previous_task = getattr(game, '_strategy_task', None)
            if previous_task is not None and not previous_task.done():
                print("[STRATEGIST] Previous per-game strategy task still running; "
                      "not launching another")
            elif _strategy_call_due(game):
                # Store task on game state (not ClaudePlayer) for
                # parallel-game safety.
                game._strategy_task = asyncio.create_task(
                    engine.claude_ai._update_strategy(game, claude_index)
                )
        except Exception as e:
            print(f"[STRATEGIST] Failed to launch background task: {e}")
    
    if claude_index != game.active_player_index:
        print(f"[EXECUTE_CLAUDE] Not Claude's turn, returning early")
        return []
    
    actions_taken = []
    # The stack path announces casts immediately so responses cannot race
    # ahead. Expose earlier buffered actions so it can flush them first.
    game._active_turn_narration = {
        "turn": game.turn_number,
        "player": game.players[claude_index].name,
        "actions": actions_taken,
        "flushed": False,
    }
    max_actions = 20  # Safety limit
    max_retries = 3   # Retries per failed action
    retry_count = 0
    last_error = None
    last_action_key = None  # Track repeated identical actions
    repeat_count = 0
    mana_failed_cards = set()  # Cards that failed with mana errors this turn (skip retries)
    # Conversation mode: maintain message history per phase
    conversation = []  # Empty list = first call sends full state
    last_action_result = None  # Describes what the last action did

    print(f"[EXECUTE_CLAUDE] Phase={game.phase}, ended={game.ended}")

    # --- Batch planning fast path ---
    # Try plan_turn() first: one API call for the whole phase.
    # Falls back to per-action loop if any action fails.
    use_plan = game.phase in [Phase.MAIN1, Phase.MAIN2] and not game.ended
    plan_failed = False
    if use_plan:
        player_for_check = game.players[claude_index]
        has_hand = len(player_for_check.hand) > 0
        has_activatable = any(
            hasattr(c, 'activated_abilities') and c.activated_abilities and not c.tapped
            for c in player_for_check.battlefield if not c.is_land()
        )
        # Fetchlands count as activatable (sacrifice to search for a land)
        has_fetchlands = any(
            not c.tapped and c.is_land() and 'search your library' in (c.oracle_text or '').lower()
            and 'sacrifice' in (c.oracle_text or '').lower()
            for c in player_for_check.battlefield
        )
        has_pending = bool(getattr(game, 'pending_resolves', None))
        if not has_hand and not has_activatable and not has_fetchlands and not has_pending:
            print(f"[EXECUTE_CLAUDE] Auto-pass: empty hand, no activatable, no pending")
            # July 31 batch-11 (madness reviewer M4): every discarded
            # advance_phase return on this path also discarded the dies-queue
            # drain messages the transition's SBA check produced — three
            # Zulaport Cutthroat drains (a 3-life swing each way) never
            # reached Discord (game_1532532252825616466). Same class as the
            # autoplay L3 sites; capture at every leg.
            _, _ph_msgs = engine.advance_phase(game)
            if _ph_msgs:
                actions_taken.extend(_ph_msgs)
            use_plan = False
        elif not has_pending:
            # Only use batch planning when no pending resolves (those need interactive handling)
            plan = await engine.claude_ai.plan_turn(game, claude_index,
                                                    call_source="ai_turn:main")

            # Pre-validate plan: strip actions that would exceed available mana
            # This prevents the common failure where DeepSeek plans 3 spells
            # but only has mana for 1, causing wasted fallback API calls.
            plan = engine._validate_plan_mana(game, claude_index, plan)

            # May 24 audit fix (#8): 3-strike rule before plan-fallback.
            # Previous behavior was "first failed action → fall back to
            # decide_action_inline loop" — but one mid-plan failure cascades
            # into 5-15+ extra API calls per turn since the inline loop
            # re-evaluates the whole hand. The May 24 batch showed 53.7%
            # of plan_turn calls fell through to decide_action_inline,
            # with 437 `reason=mana` + 215 `reason=other` rejections that
            # were mostly STATE-STALE (cast a card already cast, activate
            # an already-tapped permanent, etc.). The remaining planned
            # actions are usually fine — we just need to SKIP the bad one.
            # Fall back only when 2 consecutive failures suggest the plan
            # is genuinely broken (e.g., AI miscalculated mana for the
            # whole sequence).
            _consecutive_failures = 0
            for action in plan:
                if game.ended:
                    break
                if action.get("type") == "pass":
                    print(f"{engine.claude_ai.turn_tag} [PLAN] {game.players[claude_index].name} passes")
                    _, _ph_msgs = engine.advance_phase(game)
                    if _ph_msgs:
                        actions_taken.extend(_ph_msgs)
                    break
                result = await engine._execute_action(game, claude_index, action)
                if result:
                    print(f"{engine.claude_ai.turn_tag} [PLAN] OK: {result}")
                    actions_taken.append(result)
                    _consecutive_failures = 0  # Reset on success
                else:
                    # Action failed — fall back to per-action loop for remainder
                    error = engine._get_action_error(game, claude_index, action)
                    # May 20 audit (#14): structured [PLAN-REJECTED] tag for
                    # post-batch categorization. May 20 measured median 1355
                    # plan_turn calls/game (May 17 baseline 569-1214) with
                    # decide_action_inline as 43% of source distribution. We
                    # need to know WHICH error categories drive fallback to
                    # the inline path. Categorize by leading phrase of the
                    # error so `grep "[PLAN-REJECTED] reason=" | sort | uniq
                    # -c | sort -rn` summarizes the distribution.
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
                    _consecutive_failures += 1
                    # May 24 audit fix (#8): 3-strike rule. Single-action
                    # failures are usually state-staleness (already cast,
                    # already tapped) — just skip and try the next action.
                    # Two consecutive failures suggests the plan's premise
                    # is genuinely broken (mana miscalc, wrong castable
                    # set, etc.) — fall back to inline mode.
                    if _consecutive_failures >= 2:
                        print(f"{engine.claude_ai.turn_tag} [PLAN] {_consecutive_failures} consecutive failures, falling back to per-action mode")
                        plan_failed = True
                        last_error = error
                        break
                    else:
                        print(f"{engine.claude_ai.turn_tag} [PLAN] Action skipped (failure 1/2), continuing plan: {error}")
                        # continue is implicit — next iteration of for loop
            if not plan_failed:
                use_plan = False  # Plan completed successfully, skip per-action loop

    # --- Per-action fallback loop ---
    # Used when: plan_turn failed mid-execution, or pending resolves need interactive handling
    while (plan_failed or (use_plan and game.phase in [Phase.MAIN1, Phase.MAIN2])) and not game.ended and len(actions_taken) < max_actions:
        player_for_check = game.players[claude_index]
        has_hand = len(player_for_check.hand) > 0
        has_activatable = any(
            hasattr(c, 'activated_abilities') and c.activated_abilities and not c.tapped
            for c in player_for_check.battlefield
            if not c.is_land()
        )
        has_fetchlands = any(
            not c.tapped and c.is_land() and 'search your library' in (c.oracle_text or '').lower()
            and 'sacrifice' in (c.oracle_text or '').lower()
            for c in player_for_check.battlefield
        )
        has_pending = bool(getattr(game, 'pending_resolves', None))
        if not has_hand and not has_activatable and not has_fetchlands and not has_pending:
            print(f"[EXECUTE_CLAUDE] Auto-pass: empty hand, no activatable abilities, no pending resolves")
            _, _ph_msgs = engine.advance_phase(game)
            if _ph_msgs:
                actions_taken.extend(_ph_msgs)
            break

        print(f"[EXECUTE_CLAUDE] Entering while loop, calling decide_action...")

        # Pass error feedback if we're retrying
        action, conversation = await engine.claude_ai.decide_action(
            game, claude_index, last_error=last_error,
            conversation=conversation, action_result=last_action_result
        )

        # Guard against AI returning a list instead of a dict (e.g. target list)
        if not isinstance(action, dict):
            print(f"{engine.claude_ai.turn_tag} AI returned non-dict action ({type(action).__name__}), retrying: {action}")
            last_error = "Invalid action format — please return a JSON object with 'type' field, not a list."
            retry_count += 1
            if retry_count > 5:
                _, _ph_msgs = engine.advance_phase(game)
                if _ph_msgs:
                    actions_taken.extend(_ph_msgs)
                break
            continue

        if action.get("type", "pass") == "pass":
            print(f"{engine.claude_ai.turn_tag} {game.players[claude_index].name} chose to pass")
            _, _ph_msgs = engine.advance_phase(game)
            if _ph_msgs:
                actions_taken.extend(_ph_msgs)
            break

        # Detect repeated identical actions (prevents infinite loops)
        action_key = f"{action.get('type')}:{action.get('card', action.get('permanent', ''))}:{action.get('ability', '')}"
        if action_key == last_action_key:
            repeat_count += 1
            if repeat_count >= 3:
                print(f"{engine.claude_ai.turn_tag} Same action repeated {repeat_count} times, forcing pass: {action_key}")
                # For resolve actions that keep looping, clear the pending resolve and move on
                if action.get('type') == 'resolve':
                    desc = action.get('description', '')
                    if desc:
                        desc_lower = desc.lower()
                        game.pending_resolves = [
                            pr for pr in game.pending_resolves
                            if not any(word in pr.lower() for word in desc_lower.split() if len(word) > 3)
                        ]
                        print(f"{engine.claude_ai.turn_tag} Cleared stale pending resolve for: {desc[:80]}")
                last_error = f"You've tried '{action.get('card', action.get('permanent', ''))}' multiple times with no effect. Try something else or pass."
                if repeat_count >= 5:
                    print(f"{engine.claude_ai.turn_tag} Repeated action limit reached, auto-passing")
                    _, _ph_msgs = engine.advance_phase(game)
                    if _ph_msgs:
                        actions_taken.extend(_ph_msgs)
                    break
                continue  # Skip execution, retry with error feedback
        else:
            repeat_count = 0
        last_action_key = action_key

        # Skip cards that already failed with mana errors this turn
        if action.get('type') == 'cast' and action.get('card', '').lower() in mana_failed_cards:
            print(f"{engine.claude_ai.turn_tag} [MANA-DEDUP] Skipping {action.get('card')} (already failed with mana error)")
            last_error = f"{action.get('card')} already failed to cast this turn (mana issue). Try a different card or pass."
            continue

        # [FIX-2] Per-action counterspell guard — same logic as _validate_plan_mana.
        # The batch validator only runs for plan_turn; this covers the per-action fallback loop.
        if action.get('type') == 'cast':
            _pa_card_name = action.get('card', '')
            _pa_card_obj = next((c for c in game.players[claude_index].hand
                                 if c.name.lower() == _pa_card_name.lower()), None)
            if _pa_card_obj and not game.stack:
                _pa_oracle = (_pa_card_obj.oracle_text or '').lower()
                _pa_is_counter = 'counter target' in _pa_oracle and 'spell' in _pa_oracle
                _pa_is_creature_with_etb = _pa_card_obj.is_creature() and ('enters' in _pa_oracle or 'enter' in _pa_oracle)
                _pa_is_modal = (
                    _pa_card_obj.name in {
                        "Mystic Confluence", "Archmage's Charm", "Cryptic Command",
                        "Fuel for the Cause", "Rewind", "Absorb", "Sinister Sabotage",
                        "Sublime Epiphany", "Commit // Memory",
                    }
                    or ('•' in _pa_oracle and any(kw in _pa_oracle for kw in ['draw a card', 'return target', 'tap target']))
                )
                if _pa_is_counter and not _pa_is_creature_with_etb and not _pa_is_modal:
                    print(f"[PLAN-VALIDATE] Per-action: skipped counterspell {_pa_card_obj.name} with empty stack")
                    last_error = f"{_pa_card_obj.name} requires a spell on the stack to counter. Try a different action or pass."
                    continue

        result = await engine._execute_action(game, claude_index, action)

        # May 20 audit (Bug E): some action handlers return a failure-message
        # string instead of None, which the success branch misreports as
        # `Action succeeded: ...already used its ability...`. game_1506623255119925278
        # showed `[DEEPSEEK TURN] Action succeeded: Teferi, Hero of Dominaria
        # already used its ability this turn — cannot activate again` —
        # actually an activation rejection. Sniff the result string for
        # failure-indicator phrases and reclassify as failed.
        _looks_like_failure = _result_looks_like_failure(result)

        if result and not _looks_like_failure:
            print(f"{engine.claude_ai.turn_tag} Action succeeded: {result}")
            actions_taken.append(result)
            last_action_result = result  # Feed into next conversation delta
            last_error = None
            retry_count = 0
        elif _looks_like_failure:
            # Treat the message as the error; falls through to retry logic.
            print(f"{engine.claude_ai.turn_tag} Action returned failure message: {result}")
            retry_count += 1
            last_error = result
            last_action_result = None
            if retry_count > max_retries:
                print(f"{engine.claude_ai.turn_tag} Action FAILED (retry {max_retries}/{max_retries}): {last_error}")
                retry_count = 0
                last_error = None
                continue
            continue
        else:
            # Action failed - retry with feedback
            _err_preview = engine._get_action_error(game, claude_index, action)
            # [PLAN-STALE] Stale-plan play_land (land already played this turn) — skip silently.
            if _err_preview and _err_preview.startswith("[PLAN-STALE]"):
                print(f"{engine.claude_ai.turn_tag} {_err_preview}")
                last_error = None
                last_action_result = None
                continue
            retry_count += 1
            last_error = _err_preview
            last_action_result = None  # Don't send delta for failed actions
            # Synthesize a fallback reason when the handler returned None
            # (the "FAILED (retry N/3): None" lines from the May 16 batch).
            # 17 occurrences/batch, instrumentation gap — surface what the
            # AI actually tried so audits can see it.
            if not last_error:
                _atype = action.get('type', '?')
                _aname = action.get('card') or action.get('permanent') or '?'
                last_error = f"no error message from action handler ({_atype} {_aname})"
            if retry_count > max_retries:
                print(f"{engine.claude_ai.turn_tag} Action FAILED (retry {max_retries}/{max_retries}): {last_error}")
                # Track mana failures to skip retries on same card
                if last_error and 'mana' in last_error.lower() and 'not enough' in last_error.lower():
                    failed_card = action.get('card', '')
                    if failed_card:
                        mana_failed_cards.add(failed_card.lower())
                        print(f"{engine.claude_ai.turn_tag} [MANA-DEDUP] {failed_card} added to mana-failed set")
                print(f"{engine.claude_ai.turn_tag} Max retries exceeded, passing")
                _, _ph_msgs = engine.advance_phase(game)
                if _ph_msgs:
                    actions_taken.extend(_ph_msgs)
                break
            print(f"{engine.claude_ai.turn_tag} Action FAILED (retry {retry_count}/{max_retries}): {last_error}")

            # Track mana failures to skip retries on same card
            if last_error and 'mana' in last_error.lower() and 'not enough' in last_error.lower():
                failed_card = action.get('card', '')
                if failed_card:
                    mana_failed_cards.add(failed_card.lower())
                    print(f"{engine.claude_ai.turn_tag} [MANA-DEDUP] {failed_card} added to mana-failed set")
            # Continue loop to retry with error feedback

    # [FIX-10] Only log "no actions taken" when the plan had real actions but none executed.
    # Passing immediately is correct behavior (e.g., empty hand) — not worth logging.
    # was: print every time Claude passed, ~109 occurrences per batch.
    if not actions_taken and plan_failed:
        print(f"[EXECUTE_CLAUDE] No actions taken after plan failure — per-action loop also produced nothing.")

    # ================================================================
    # COMBAT PHASE — proper MTG flow:
    # MAIN1 → pass → COMBAT_BEGIN → DECLARE_ATTACKERS → decide attackers
    # Same as human: !pass → COMBAT_BEGIN, !pass → DECLARE_ATTACKERS, !attack
    # ================================================================
    if game.phase == Phase.COMBAT_BEGIN and not game.ended:
        # Advance past COMBAT_BEGIN to DECLARE_ATTACKERS (like human's second !pass)
        # July 20 display audit: the return was dropped, so beginning-of-
        # combat trigger output (Luminarch Aspirant's counter — 8 invisible
        # placements in game_1527462198430138448) never reached Discord.
        _, _cb_msgs = engine.advance_phase(game)  # → DECLARE_ATTACKERS
        if _cb_msgs:
            actions_taken.extend(_cb_msgs)
        # Aug 10 deferred (G6a): drain Tier-3-queued beginning-of-combat
        # triggers HERE, before damage (CR 508.1). Templated ones already
        # resolve inline in advance_phase; the queued tail otherwise landed
        # after combat. See the twin in mtg/autoplay.py — the drain
        # snapshots-and-clears, so later drains find an empty queue.
        actions_taken.extend(await engine.drain_pending_triggers(game))
        print(f"[EXECUTE_CLAUDE] Advanced to DECLARE_ATTACKERS")

    if game.phase == Phase.DECLARE_ATTACKERS and not game.ended:
        # Aug 2 (batch-13): fresh declaration = empty attacker list — the
        # twin of the autoplay-side clear (a stale id from a previous combat
        # inflated an attacker-count gate; see mtg/autoplay.py).
        game.attackers = []
        # Ask Claude which creatures to attack with (separate from MAIN1 decisions)
        attacker_names = await engine.claude_ai.decide_attackers(game, claude_index)

        if attacker_names:
            # Declare the attacks
            player = game.players[claude_index]
            used_ids = set()  # Track already-declared attackers (for duplicate names like "Plant")
            for name in attacker_names:
                # Find a matching creature that hasn't already been declared
                card = None
                for c in player.get_zone(Zone.BATTLEFIELD):
                    # May 25 audit (F24): pass `game` to is_creature so
                    # devotion-gated Theros gods can't attack at devotion < threshold.
                    if (c.name.lower() == name.lower() and c.id not in used_ids
                            and c.is_creature(game=game) and not c.tapped):
                        card = c
                        break
                if card:
                    can_attack, reason = engine.rules.can_attack_with(game, player, card)
                    if not can_attack:
                        print(f"[COMBAT] Rejected attacker {card.name}: {reason}")
                        continue
                    paid, tax_reason = engine.rules.pay_attack_tax(game, player, card)
                    if not paid:
                        print(f"[COMBAT] Rejected attacker {card.name}: {tax_reason}")
                        continue
                    card.attacking = True
                    card.attacks_this_turn += 1  # C-1: Moraug's attack-count static
                    card.attacking_player = 1 - claude_index
                    engine.tap_permanent(card)
                    game.attackers.append(card.id)
                    used_ids.add(card.id)
                else:
                    # June 10 round 3 (A10b follow-up): visibility for silent
                    # drops — see the twin print in mtg/autoplay.py.
                    print(f"[COMBAT] Proposed attacker '{name}' skipped — no untapped, "
                          f"eligible, unclaimed instance on {player.name}'s battlefield")

            if game.attackers:
                declared_names = []
                for a_id in game.attackers:
                    result_card = game.find_card_global(a_id)
                    if result_card:
                        declared_names.append(result_card[0].name)
                actions_taken.append(f"⚔️ {player.name} attacks with {', '.join(declared_names)}")
                print(f"[EXECUTE_CLAUDE] Declared {len(game.attackers)} attacker(s)")
        else:
            print(f"[EXECUTE_CLAUDE] {game.players[claude_index].name} chose not to attack")

    # Process combat if attackers were declared
    if game.attackers and not game.ended:
        print(f"[EXECUTE_CLAUDE] Processing combat with {len(game.attackers)} attacker(s)")

        # Move through combat phases
        while game.phase not in [Phase.MAIN2, Phase.END, Phase.CLEANUP] and not game.ended:
            if game.phase == Phase.DECLARE_ATTACKERS:
                # Process any attack triggers
                trigger_msgs = engine.process_attack_triggers(game, game.active_player_index)
                actions_taken.extend(trigger_msgs)
                _, _ph_msgs = engine.advance_phase(game)  # → DECLARE_BLOCKERS
                if _ph_msgs:
                    actions_taken.extend(_ph_msgs)

            elif game.phase == Phase.DECLARE_BLOCKERS:
                # Check if opponent is human — pause for them to declare blocks
                opponent_idx = 1 - claude_index
                opponent = game.players[opponent_idx]
                if not opponent.is_claude:
                    # Human opponent needs to declare blocks via !block or !noblock
                    game.waiting_for_human_blocks = True
                    print(f"[EXECUTE_CLAUDE] Pausing for human blocks")
                    return actions_taken  # Pause here — caller will prompt human
                else:
                    # AI opponent — let it decide blocks before damage
                    attacker_cards = []
                    for a_id in game.attackers:
                        result = game.find_card_global(a_id)
                        if result:
                            attacker_cards.append(result[0])
                    if attacker_cards:
                        blocks = await engine.claude_ai.decide_blocks(game, opponent_idx, attacker_cards)
                        if blocks:
                            # May 7 audit fix #2: disambiguate same-name creatures
                            # (Plant blocks Plant repeated). Number same-name cards.
                            name_counts4 = {}
                            for a_id4 in game.attackers:
                                ar4 = game.find_card_global(a_id4)
                                if ar4:
                                    name_counts4[ar4[0].name] = name_counts4.get(ar4[0].name, 0) + 1
                            for blocker_ids4 in blocks.values():
                                for b_id4 in blocker_ids4 or []:
                                    br4 = game.find_card_global(b_id4)
                                    if br4:
                                        name_counts4[br4[0].name] = name_counts4.get(br4[0].name, 0) + 1
                            name_index4 = {}
                            name_running4 = {}
                            def _label_for4(card):
                                if card.id in name_index4:
                                    return name_index4[card.id]
                                if name_counts4.get(card.name, 0) > 1:
                                    idx = name_running4.get(card.name, 0) + 1
                                    name_running4[card.name] = idx
                                    label = f"{card.name} #{idx}"
                                else:
                                    label = card.name
                                name_index4[card.id] = label
                                return label

                            block_msgs = []
                            for attacker_id, blocker_ids in blocks.items():
                                if blocker_ids:
                                    atk_result = game.find_card_global(attacker_id)
                                    if not atk_result:
                                        continue
                                    attacker = atk_result[0]
                                    attacker_label = _label_for4(attacker)
                                    blk_names = []
                                    for blocker_id in blocker_ids:
                                        blk_result = game.find_card_global(blocker_id)
                                        if not blk_result:
                                            continue
                                        blocker = blk_result[0]
                                        blocker.blocking.append(attacker.id)
                                        attacker.blocked_by.append(blocker.id)
                                        if attacker.id not in game.blockers:
                                            game.blockers[attacker.id] = []
                                        game.blockers[attacker.id].append(blocker.id)
                                        blk_names.append(_label_for4(blocker))
                                    # May 20 audit fix: skip the append when
                                    # no blocker names resolved (find_card_global
                                    # returned None for every blocker_id) —
                                    # otherwise emits " blocks Attacker" with
                                    # leading empty join.
                                    # May 23 audit (MAJOR #11): also filter
                                    # empty strings — _label_for4 sometimes
                                    # returned "" so [''] slipped past the
                                    # is-empty check.
                                    blk_names = [n for n in blk_names if n and n.strip()]
                                    if not blk_names:
                                        continue
                                    block_msgs.append(f"{', '.join(blk_names)} blocks {attacker_label}")
                            if block_msgs:
                                actions_taken.append(f"🛡️ {opponent.name} blocks: " + "; ".join(block_msgs))
                    _, _ph_msgs = engine.advance_phase(game)  # → COMBAT_DAMAGE
                    if _ph_msgs:
                        actions_taken.extend(_ph_msgs)

            elif game.phase == Phase.COMBAT_DAMAGE:
                # Resolve combat damage!
                combat_messages = engine.rules.resolve_combat_damage(game)
                actions_taken.extend(combat_messages)

                # Check state-based actions after damage
                sba_messages = engine.check_state_based_actions(game)
                actions_taken.extend(sba_messages)

                # Check for game end
                if game.ended:
                    break

                _, _ph_msgs = engine.advance_phase(game)  # → COMBAT_END
                if _ph_msgs:
                    actions_taken.extend(_ph_msgs)

            elif game.phase == Phase.COMBAT_END:
                # Clear combat state
                for player in game.players:
                    for creature in player.creatures():
                        creature.attacking = False
                        creature.attacking_player = None
                        creature.blocking = []
                        creature.blocked_by = []
                game.attackers = []
                game.blockers = {}
                # Aug 2 (batch-13 follow-up): DEFER, don't discard — the
                # autoplay main loop now runs _claude_extra_combats after
                # execute_claude_turn returns, so grants earned in the
                # internally-resolved combat get consumed there. Outside
                # autoplay (live play) the end_turn sweep still discards
                # visibly, so a stale value can never leak into the next
                # player's turn either way.
                if getattr(game, '_additional_combats', 0):
                    print(f"[EXTRA-COMBAT] {game._additional_combats} additional "
                          f"combat phase(s) pending on Claude's turn — "
                          f"deferring to the autoplay consumption loop")
                _, _ph_msgs = engine.advance_phase(game)  # → MAIN2
                if _ph_msgs:
                    actions_taken.extend(_ph_msgs)
            else:
                break

        print(f"[EXECUTE_CLAUDE] Combat resolved, now in phase {game.phase}")
    elif not game.attackers and game.phase == Phase.DECLARE_ATTACKERS and not game.ended:
        # No attackers — skip straight to MAIN2
        print(f"[EXECUTE_CLAUDE] No attackers, skipping to MAIN2")
        while game.phase not in [Phase.MAIN2, Phase.END, Phase.CLEANUP]:
            _, _ph_msgs = engine.advance_phase(game)
            if _ph_msgs:
                actions_taken.extend(_ph_msgs)
    
    # Continue with MAIN2 if applicable
    if game.phase == Phase.MAIN2 and not game.ended:
        print(f"[EXECUTE_CLAUDE] Continuing to MAIN2")
        retry_count = 0
        last_error = None
        last_action_key_m2 = None
        repeat_count_m2 = 0
        # New conversation for MAIN2 (fresh state after combat)
        conversation_m2 = []
        last_action_result_m2 = None

        while game.phase == Phase.MAIN2 and not game.ended and len(actions_taken) < max_actions:
            action, conversation_m2 = await engine.claude_ai.decide_action(
                game, claude_index, last_error=last_error,
                conversation=conversation_m2, action_result=last_action_result_m2
            )

            if action.get("type", "pass") == "pass":
                print(f"{engine.claude_ai.turn_tag} {game.players[claude_index].name} passes in MAIN2")
                break

            # Detect repeated identical actions (prevents infinite resolve loops)
            action_key_m2 = f"{action.get('type')}:{action.get('card', action.get('permanent', ''))}:{action.get('ability', '')}"
            if action_key_m2 == last_action_key_m2:
                repeat_count_m2 += 1
                if repeat_count_m2 >= 3:
                    print(f"{engine.claude_ai.turn_tag} Same action repeated {repeat_count_m2} times in MAIN2: {action_key_m2}")
                    if action.get('type') == 'resolve':
                        desc = action.get('description', '')
                        if desc:
                            desc_lower = desc.lower()
                            game.pending_resolves = [
                                pr for pr in game.pending_resolves
                                if not any(word in pr.lower() for word in desc_lower.split() if len(word) > 3)
                            ]
                            print(f"{engine.claude_ai.turn_tag} Cleared stale pending resolve: {desc[:80]}")
                    if repeat_count_m2 >= 5:
                        print(f"{engine.claude_ai.turn_tag} Repeated action limit in MAIN2, ending turn")
                        break
                    last_error = f"You've tried this multiple times with no effect. Try something else or pass."
                    last_action_result_m2 = None
                    continue
            else:
                repeat_count_m2 = 0
            last_action_key_m2 = action_key_m2

            result = await engine._execute_action(game, claude_index, action)

            # June 10 (V31c): same Bug-E failure-phrase guard as MAIN1.
            if result and _result_looks_like_failure(result):
                print(f"{engine.claude_ai.turn_tag} Action returned failure message: {result}")
                last_error = result
                result = None
            if result:
                print(f"{engine.claude_ai.turn_tag} Action succeeded: {result}")
                actions_taken.append(result)
                last_action_result_m2 = result
                last_error = None
                retry_count = 0
            else:
                _err_preview = engine._get_action_error(game, claude_index, action)
                if _err_preview and _err_preview.startswith("[PLAN-STALE]"):
                    print(f"{engine.claude_ai.turn_tag} {_err_preview}")
                    last_error = None
                    last_action_result_m2 = None
                    continue
                retry_count += 1
                last_error = _err_preview
                last_action_result_m2 = None
                if not last_error:
                    _atype = action.get('type', '?')
                    _aname = action.get('card') or action.get('permanent') or '?'
                    last_error = f"no error message from action handler ({_atype} {_aname})"
                if retry_count > max_retries:
                    print(f"{engine.claude_ai.turn_tag} Action FAILED (retry {max_retries}/{max_retries}): {last_error}")
                    print(f"{engine.claude_ai.turn_tag} Max retries exceeded in MAIN2, ending turn")
                    break
                print(f"{engine.claude_ai.turn_tag} Action FAILED (retry {retry_count}/{max_retries}): {last_error}")
    
    return actions_taken


async def continue_claude_post_combat(engine, game: GameState) -> List[str]:
    """Continue Claude's turn after human has declared blocks and combat resolved.

    Called from !noblock, !block→!combat, and _resolve_combat when it's Claude's turn.
    Handles MAIN2 actions.
    """
    claude_index = 0 if game.players[0].is_claude else 1
    actions_taken = []
    game._active_turn_narration = {
        "turn": game.turn_number,
        "player": game.players[claude_index].name,
        "actions": actions_taken,
        "flushed": False,
    }
    max_actions = 15
    max_retries = 3

    game.waiting_for_human_blocks = False

    if game.phase == Phase.MAIN2 and not game.ended:
        print(f"[EXECUTE_CLAUDE] Continuing to MAIN2 (post-combat)")
        retry_count = 0
        last_error = None
        last_action_key_pc = None
        repeat_count_pc = 0
        # Conversation mode: fresh conversation for post-combat MAIN2
        conversation_pc = []
        last_action_result_pc = None

        while game.phase == Phase.MAIN2 and not game.ended and len(actions_taken) < max_actions:
            action, conversation_pc = await engine.claude_ai.decide_action(
                game, claude_index, last_error=last_error,
                conversation=conversation_pc, action_result=last_action_result_pc
            )

            if action.get("type", "pass") == "pass":
                print(f"{engine.claude_ai.turn_tag} {game.players[claude_index].name} passes in MAIN2 (post-combat)")
                break

            # Detect repeated identical actions (prevents infinite resolve loops)
            action_key_pc = f"{action.get('type')}:{action.get('card', action.get('permanent', ''))}:{action.get('ability', '')}"
            if action_key_pc == last_action_key_pc:
                repeat_count_pc += 1
                if repeat_count_pc >= 3:
                    print(f"{engine.claude_ai.turn_tag} Same action repeated {repeat_count_pc} times (post-combat): {action_key_pc}")
                    if action.get('type') == 'resolve':
                        desc = action.get('description', '')
                        if desc:
                            desc_lower = desc.lower()
                            game.pending_resolves = [
                                pr for pr in game.pending_resolves
                                if not any(word in pr.lower() for word in desc_lower.split() if len(word) > 3)
                            ]
                            print(f"{engine.claude_ai.turn_tag} Cleared stale pending resolve: {desc[:80]}")
                    if repeat_count_pc >= 5:
                        print(f"{engine.claude_ai.turn_tag} Repeated action limit (post-combat), ending turn")
                        break
                    last_error = f"You've tried this multiple times with no effect. Try something else or pass."
                    last_action_result_pc = None
                    continue
            else:
                repeat_count_pc = 0
            last_action_key_pc = action_key_pc

            result = await engine._execute_action(game, claude_index, action)

            # June 10 (V31c): same Bug-E failure-phrase guard as MAIN1.
            if result and _result_looks_like_failure(result):
                print(f"{engine.claude_ai.turn_tag} Action returned failure message: {result}")
                last_error = result
                result = None
            if result:
                print(f"{engine.claude_ai.turn_tag} Action succeeded: {result}")
                actions_taken.append(result)
                last_action_result_pc = result
                last_error = None
                retry_count = 0
            else:
                _err_preview = engine._get_action_error(game, claude_index, action)
                if _err_preview and _err_preview.startswith("[PLAN-STALE]"):
                    print(f"{engine.claude_ai.turn_tag} {_err_preview}")
                    last_error = None
                    last_action_result_pc = None
                    continue
                retry_count += 1
                last_error = _err_preview
                last_action_result_pc = None
                if not last_error:
                    _atype = action.get('type', '?')
                    _aname = action.get('card') or action.get('permanent') or '?'
                    last_error = f"no error message from action handler ({_atype} {_aname})"
                if retry_count > max_retries:
                    print(f"{engine.claude_ai.turn_tag} Action FAILED (retry {max_retries}/{max_retries}): {last_error}")
                    break
                print(f"{engine.claude_ai.turn_tag} Action FAILED (retry {retry_count}/{max_retries}): {last_error}")

    # [STRATEGIST] Phase 2: Await background strategist if it's still running.
    # The memo is now saved for NEXT turn's plan_turn() call.
    # Use per-game task (not ClaudePlayer._strategy_task) for parallel safety.
    #
    # Timeout note: V4-Pro with reasoning_effort=high routinely takes 30-60s
    # to think before returning. The previous 10s ceiling timed out on ~25%
    # of turns, leaving the actor on stale memos. 45s is the practical
    # ceiling — long enough for V4-Pro to finish reasoning, short enough
    # to keep autoplay turns from stalling indefinitely.
    strategy_task = getattr(game, '_strategy_task', None) or engine.claude_ai._strategy_task
    if strategy_task and not strategy_task.done():
        # Apr 29 audit: 45s wasn't enough for V4-Pro reasoning_effort=high to
        # finish on busy turns; ~30% of strategies were timing out and falling
        # back to stale memo. 60s gives the model headroom while still bounding
        # autoplay turn time.
        #
        # May 16 audit: in the May 15 22:00 batch, 52 strategist timeouts at
        # 60s across 18 games = ~52 minutes of pure wallclock idle. Cut the
        # cap to 30s and don't cancel the underlying task — let it finish in
        # the background so the memo lands for a later turn even if it
        # misses THIS turn. Stale-memo fallback handles the gap.
        try:
            await asyncio.wait_for(asyncio.shield(strategy_task), timeout=30.0)
        except asyncio.TimeoutError:
            print(f"[STRATEGIST] Background strategy didn't finish in 30s — "
                  f"continuing with stale memo (task still running)")
        except Exception as e:
            print(f"[STRATEGIST] Background strategy error: {e}")
        # Only clear the handle when the task is actually done; otherwise
        # leave it alive so a future turn can await it.
        if strategy_task.done():
            engine.claude_ai._strategy_task = None

    return actions_taken


# Aug 8 batch audit (#11): the plan-action vocabulary both executors
# dispatch. Used by _get_action_error's unknown-type teaching message; the
# grammar-consistency pin (tests/test_july31_batch_audit.py) asserts every
# provider-advertised type is in this set so the list cannot silently drift.
KNOWN_PLAN_ACTION_TYPES = frozenset({
    "cast", "play_land", "activate", "resolve", "pass", "suspend",
    "foretell", "graveyard_activate", "crew", "cycle", "attack",
    "companion",
})


def _wrong_zone_hint(game, player, card_name) -> str:
    """Teaching message when a `cast` names a card that IS available, but from
    a zone that needs a different action type.

    Returns '' when the card genuinely is not anywhere reachable, so the
    caller falls back to its plain not-in-hand message.
    """
    if not card_name:
        return ''
    from mtg.helpers import names_match, parse_graveyard_activation

    wanted = str(card_name)
    for card in (getattr(player, 'graveyard', None) or []):
        if not names_match(getattr(card, 'name', ''), wanted):
            continue
        parsed = parse_graveyard_activation(getattr(card, 'oracle_text', '') or '')
        if parsed:
            mechanic, cost = parsed
            return (f"'{wanted}' is in your GRAVEYARD, not your hand — {mechanic} "
                    f"is an activated ability, not a cast (CR 702.83a). Use "
                    f'{{"type": "graveyard_activate", "card": "{wanted}", '
                    f'"mechanic": "{mechanic}"}} and pay {cost}.')
        return (f"'{wanted}' is in your GRAVEYARD, not your hand. Only cards with "
                f"flashback / escape / jump-start / aftermath can be CAST from "
                f"there; this one cannot.")
    for card in (getattr(player, 'companion_zone', None) or []):
        if names_match(getattr(card, 'name', ''), wanted):
            return (f"'{wanted}' is your COMPANION, not in your hand — pay {{3}} to "
                    f'move it to hand first with {{"type": "companion", '
                    f'"card": "{wanted}"}}, then cast it on a later action.')
    for card in (getattr(player, 'exile', None) or []):
        if names_match(getattr(card, 'name', ''), wanted):
            if getattr(card, 'id', None) in (getattr(player, 'playable_from_exile', None) or []):
                return ''  # castable from exile; the executor handles it
            if getattr(card, '_foretold', False):
                return ''  # foretold casts are handled by the exile branch
            return (f"'{wanted}' is in EXILE and is not playable from there.")
    return ''


def _get_action_error(engine, game: GameState, player_index: int, action: Dict) -> str:
    """Get human-readable error for why an action failed."""
    player = game.players[player_index]
    action_type = action.get("type")

    # Aug 7 batch audit (C-4): a deliberately-dropped redundant `resolve`
    # stashes its real reason — surface it so the model stops re-proposing
    # the same resolve (it used to see "unknown reason", retry the identical
    # action, and burn a Tier-3 judge call on the second pass).
    if action_type == "resolve":
        _lrd = getattr(game, '_last_resolve_drop_reason', None)
        if _lrd and _lrd[0] == game.turn_number:
            game._last_resolve_drop_reason = None
            return _lrd[1]

    if action_type == "cast":
        card_name = action.get("card")
        # July 20 batch-3 audit: if the executor just ran this exact cast and
        # cast_spell_async returned a real failure reason, surface THAT instead
        # of re-deriving one. The re-derivation below misses whole failure
        # classes (aura targeting has no literal "target" in oracle text;
        # graveyard-zone targets; CR 601.2c gates) and fell through to
        # "unknown reason — mana looks sufficient" — the AI then re-proposed
        # the same doomed cast (Animate Dead ×36 in the 15289 batch).
        _lcf = getattr(game, '_last_cast_failure', None)
        if _lcf:
            _lcf_turn, _lcf_name, _lcf_msg = _lcf
            if (_lcf_turn == game.turn_number and card_name
                    and _lcf_name.lower() == str(card_name).lower()):
                game._last_cast_failure = None
                return _lcf_msg
        adventure_name = action.get("adventure")
        card = player.find_card(card_name, Zone.HAND)
        if not card:
            # Check if card_name is an adventure name
            for c in player.hand:
                if c.adventure_name and c.adventure_name.lower() == card_name.lower():
                    card = c
                    adventure_name = c.adventure_name
                    break
        if not card:
            # Check if card_name is a split-half name (e.g. "Fire" on "Fire // Ice").
            # AI sometimes casts split halves by half-name alone. Route it as the
            # split half, setting adventure_name to the half (downstream treats
            # adventure/split symmetrically when card.split_names is set).
            for c in player.hand:
                split_names = getattr(c, 'split_names', None) or []
                for sname in split_names:
                    if sname.lower() == card_name.lower():
                        card = c
                        adventure_name = sname
                        print(f"[CAST] {card_name} resolved as split-half of {c.name}")
                        break
                if card:
                    break
        if not card:
            # Check command zone — AI often plans commanders as "from hand"
            for c in player.command_zone:
                if c.name.lower() == card_name.lower():
                    card = c
                    print(f"[CAST] {card_name} found in command zone (AI planned as hand)")
                    break
        if not card:
            # Aug 10 deferred (B4): a flat "not found in hand" gave the model
            # no signal that a DIFFERENT action type was required, so it
            # re-proposed `cast` indefinitely — 24 inline failures plus 8
            # plan-validate rejects in one game, trying six distinct wrong
            # JSON shapes for an offered Dregscape Zombie
            # (game_1536017757303341078). The engine's rejection is correct
            # per CR 702.83a; what was missing is the teaching half, exactly
            # the C-4 stash class. Name the zone AND the right action type.
            _hint = _wrong_zone_hint(game, player, card_name)
            if _hint:
                return _hint
            return f"'{card_name}' not found in hand"

        # [FIX] Block AI from casting suspend-only cards (Mox Tantalite, Lotus Bloom, etc.)
        # These have no mana cost in hand — they can only be exiled with suspend.
        SUSPEND_ONLY_CARDS = {
            "Mox Tantalite", "Lotus Bloom", "Ancestral Vision",
            "Living End", "Restore Balance", "Hypergenesis",
        }
        _oracle_lower = (card.oracle_text or '').lower()
        _is_suspend_only = (
            card.name in SUSPEND_ONLY_CARDS
            or (
                (not card.mana_cost or card.mana_cost in ("", "0", "{0}"))
                and 'suspend' in _oracle_lower
                and 'you may cast' not in _oracle_lower
            )
        )
        if _is_suspend_only and card in player.hand:
            print(f"[EXECUTE-CAST] Blocked {card.name} cast — suspend-only card")
            return f"{card.name} can only be suspended, not cast from hand"

        # Use adventure cost if casting adventure half, or split-half cost if split
        is_adventure = (adventure_name and card.adventure_cost)
        effective_cmc = card.cmc
        effective_mana_cost = card.mana_cost or ""
        display_name = card_name
        if is_adventure:
            effective_mana_cost = card.adventure_cost
            adv_upper = card.adventure_cost.upper()
            effective_cmc = sum(int(m) for m in re.findall(r'\{(\d+)\}', adv_upper))
            effective_cmc += sum(adv_upper.count(f'{{{c}}}') for c in ['W', 'U', 'B', 'R', 'G'])
            display_name = adventure_name
        elif adventure_name and getattr(card, 'split_names', None):
            # AI used "adventure" key for a split card half (e.g. Commit // Memory → Memory)
            for i, sname in enumerate(card.split_names):
                if adventure_name.lower() == sname.lower():
                    split_cost = card.split_costs[i] if card.split_costs else ""
                    if split_cost:
                        effective_mana_cost = split_cost
                        half_upper = split_cost.upper()
                        effective_cmc = sum(int(m) for m in re.findall(r'\{(\d+)\}', half_upper))
                        effective_cmc += sum(half_upper.count(f'{{{c}}}') for c in ['W', 'U', 'B', 'R', 'G'])
                        display_name = sname
                    break
        elif card.adventure_cost and ' // ' in effective_mana_cost:
            # Creature-half cast of an adventure card: the cache stores the
            # COMBINED "creature // adventure" cost string, so pricing from it
            # double-counts pips (Beanstalk Giant {6}{G} // {G} would demand
            # {G}{G}) and the [MANA-DIVERGENCE] trace lies about the req
            # (Oakhame Ranger printed req={'other': 8} for a 4-pip half in
            # batch 15327). Price only the creature face; effective_cmc is
            # already the face cmc via the loader's recompute.
            effective_mana_cost = effective_mana_cost.split(' // ')[0]

        # Check total mana
        available = player.available_mana()
        if effective_cmc > available:
            return f"Need {effective_cmc} mana for {display_name}, only have {available} available"

        # Check colored mana specifically
        for color in ['W', 'U', 'B', 'R', 'G']:
            needed = effective_mana_cost.upper().count(f'{{{color}}}')
            if needed > 0:
                # Count available of this color
                have = 0
                for land in player.untapped_lands():
                    mana = player._get_mana_production(land)
                    have += mana.get(color, 0)
                    if mana.get('any', 0) > 0:
                        have += mana.get('any', 0)
                # Also check mana rocks
                for perm in player.battlefield:
                    if not perm.is_land() and not perm.tapped and player._can_produce_mana(perm):
                        mana = player._get_mana_production(perm)
                        have += mana.get(color, 0)
                        if mana.get('any', 0) > 0:
                            have += mana.get('any', 0)
                if have < needed:
                    return f"Need {needed} {{{color}}} mana for {display_name}, only have {have}"
        
        # Mana checks passed — the failure must be a targeting or timing issue
        if card.is_land():
            return f"Lands cannot be cast as spells (use play_land for {card_name})"

        # May 14 audit: color-identity rejections were surfacing to the AI as
        # the generic "unknown reason — mana looks sufficient" message, so the
        # AI retried the same illegal card 5+ times before giving up. Surface
        # the actual rule (CR 903.4) so the planner can drop it.
        if game.format in ('commander', 'edh', 'brawl', 'oathbreaker'):
            try:
                commander_colors = set(player._get_commander_colors())
                card_identity = set(getattr(card, 'color_identity', []) or [])
                if (commander_colors and card_identity
                        and not card_identity.issubset(commander_colors)
                        and not getattr(card, 'is_commander', False)):
                    outside = card_identity - commander_colors
                    return (f"Cannot cast {card_name}: {{{','.join(sorted(card_identity))}}} "
                            f"is outside commander color identity "
                            f"{{{','.join(sorted(commander_colors))}}} — extra: "
                            f"{','.join(sorted(outside))} (CR 903.4)")
            except Exception:
                pass

        oracle_lower = (card.oracle_text or '').lower()
        is_creature_with_counter_etb = card.is_creature() and ('enters' in oracle_lower or 'enter' in oracle_lower)
        # May 14 audit: same modal-spell exemption as the cast-time check in
        # mtg/spells.py — Mystic Confluence, Archmage's Charm, Cryptic Command,
        # etc. have bounce/draw/exile modes that work on an empty stack. Only
        # reject when the spell has NO other modes available.
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
        if ('counter target' in oracle_lower and not game.stack
                and not is_creature_with_counter_etb
                and not is_modal_with_other_modes):
            return f"{card_name} requires a target spell on the stack"
        # Suspend-only cards (no mana cost, has suspend): can't be cast normally
        if not card.mana_cost and 'suspend' in oracle_lower:
            return f"{card_name} has no mana cost and can only be suspended"

        # Bug 2c: name the failing gate rather than dumping "unknown reason"

        # Snow mana gate: spells requiring {S} need a snow-producing source.
        if '{S}' in (effective_mana_cost or '').upper() or '{s}' in (effective_mana_cost or ''):
            has_snow = False
            try:
                for perm in player.battlefield:
                    types = " ".join(perm.types) if hasattr(perm, 'types') and isinstance(perm.types, list) else str(getattr(perm, 'types', ''))
                    if 'snow' in (types or '').lower() or 'snow' in (getattr(perm, 'type_line', '') or '').lower():
                        if not perm.tapped and player._can_produce_mana(perm):
                            has_snow = True
                            break
            except Exception:
                pass
            if not has_snow:
                return f"Cannot cast {card_name}: requires {{S}} (snow mana) which is not available"

        # Timing gate: sorceries require main phase + empty stack + your turn.
        try:
            from rules.mana import Phase as _RMPhase  # optional, may not exist
        except Exception:
            _RMPhase = None
        is_sorcery = 'sorcery' in (getattr(card, 'type_line', '') or '').lower() or \
                     (hasattr(card, 'types') and any('sorcery' in str(t).lower() for t in (card.types or [])))
        has_flash = 'flash' in oracle_lower
        is_instant = 'instant' in (getattr(card, 'type_line', '') or '').lower() or \
                     (hasattr(card, 'types') and any('instant' in str(t).lower() for t in (card.types or [])))
        # Sorcery-speed restriction: most non-instant, non-flash spells
        is_sorcery_speed = not is_instant and not has_flash
        your_turn = game.active_player_index == player_index
        main_phase = game.phase in (Phase.MAIN1, Phase.MAIN2)
        if is_sorcery_speed and (not your_turn or not main_phase or game.stack):
            return f"Cannot cast {card_name}: cannot cast at this timing (sorcery speed)"

        # Zone gate: if we somehow reached here without finding the card in hand,
        # name that explicitly.
        if card not in player.hand and card not in player.command_zone:
            return f"Cannot cast {card_name}: not in a castable zone"

        # Oathbreaker signature spell gate: signature spells can ONLY be
        # cast while the oathbreaker is on the battlefield. Without this
        # specific reason the AI sees the generic "(unknown reason — mana
        # looks sufficient)" and retries, eventually falling back to a
        # different spell. Surface the actual rule so the strategist knows
        # to recast the oathbreaker first.
        if (card in player.command_zone
                and getattr(card, 'is_signature_spell', False)
                and game.format == "oathbreaker"):
            oathbreaker_present = any(
                getattr(c, 'is_commander', False) and not getattr(c, 'is_signature_spell', False)
                for c in player.battlefield
            )
            if not oathbreaker_present:
                return (f"Cannot cast {card_name}: signature spell requires "
                        f"oathbreaker on the battlefield first")

        # Target gate: spell requires a target but none legal.
        # Prefer the full targeting engine if available — it catches "target
        # creature you control" / hexproof / protection that the heuristic misses.
        if 'target' in oracle_lower:
            used_engine = False
            if HAS_TARGETING:
                try:
                    if _spell_requires_targets(card) and not _find_any_valid_target(game, card, player.name):
                        return (f"Cannot cast {card_name}: no legal targets "
                                f"(check 'target creature you control' / hexproof / protection restrictions)")
                    used_engine = True
                except Exception:
                    pass
            if not used_engine:
                opp = game.players[1 - player_index] if len(game.players) > 1 else None
                any_target = False
                try:
                    for pl in game.players:
                        if pl.battlefield:
                            any_target = True
                            break
                    if not any_target and opp and opp.life > 0:
                        if 'target player' in oracle_lower or 'target opponent' in oracle_lower or 'any target' in oracle_lower:
                            any_target = True
                except Exception:
                    any_target = True
                if not any_target:
                    return f"Cannot cast {card_name}: no legal targets"

        # Fall-through: include a coarse state hash AND a detailed mana
        # breakdown so different failure modes can be told apart in logs.
        # May 16 audit: Apex Devastator looped 3+ times with "unknown reason —
        # mana looks sufficient" because the validator's coarse hash didn't
        # surface which specific colored requirement was failing. Including
        # detailed mana per color makes it grep-able.
        # May 20 audit (#15): 202/batch "unknown reason" failures (~1.6/game).
        # Add a per-color shortfall trace that also EXCLUDES summoning-sick
        # mana creatures and ETB-this-turn tap-lands — those are the most
        # likely sources of divergence between available_mana_detailed()
        # (which the diagnostic reads) and cast_spell_async (which actually
        # pays). game_1506604517939220641:223 shows Wood Elves cast → Forest
        # fetched onto battlefield → Surrak cast fails despite pool showing
        # 9 untapped sources: Wood Elves is summoning sick, Forest WAS in
        # the count, so total untapped-this-turn was less than reported.
        try:
            from collections import Counter as _Counter
            req = _Counter()
            for _sym in re.findall(r'\{([^}]+)\}', effective_mana_cost or ''):
                _s = _sym.upper()
                if _s.isdigit():
                    req['generic'] += int(_s)
                elif _s in ('W', 'U', 'B', 'R', 'G'):
                    req[_s] += 1
                elif _s == 'C':
                    req['C'] += 1
                elif _s == 'S':
                    req['snow'] += 1
                elif _s == 'X':
                    pass  # X handled separately
                else:
                    req['other'] += 1
            usable = {'W': 0, 'U': 0, 'B': 0, 'R': 0, 'G': 0, 'C': 0, 'any': 0}
            for _c in player.battlefield:
                # Skip tapped sources outright
                if getattr(_c, 'tapped', False):
                    continue
                # Mana creatures with summoning sickness can't tap this turn
                # (CR 302.1) — they show up in pool counts but can't actually
                # produce mana. This is one suspected divergence source.
                if (_c.is_creature() and getattr(_c, 'summoning_sick', False)
                        and not _c.is_land()):
                    continue
                if not (_c.is_land() or player._can_produce_mana(_c)):
                    continue
                try:
                    prod = player._get_mana_production(_c) or {}
                except Exception:
                    prod = {}
                for color, amt in prod.items():
                    if color in usable:
                        usable[color] += amt
                    elif color == 'any':
                        usable['any'] += amt
            # Compute per-color shortfalls (after burning colored sources for
            # their specific color, the remainder of `any` + colored slack
            # has to cover `generic`).
            shortfalls = {}
            slack = usable.get('any', 0)
            for color in ('W', 'U', 'B', 'R', 'G', 'C'):
                need = req.get(color, 0)
                have = usable.get(color, 0)
                if need > have:
                    shortfalls[color] = need - have
                else:
                    slack += (have - need)
            generic_need = req.get('generic', 0)
            if generic_need > slack:
                shortfalls['generic'] = generic_need - slack
            sick_count = sum(
                1 for _c in player.battlefield
                if _c.is_creature() and getattr(_c, 'summoning_sick', False)
                and player._can_produce_mana(_c)
            )
            # July 20 audit: the per-color `usable` dict double-counts
            # OR-duals (a W/B land adds to both W and B), so shortfalls={}
            # with a failing tap was reading as an engine bug when the real
            # story was "5 physical sources displayed as 12". Emit the
            # physical one-tap total so the log tells the truth about
            # whether the cast was actually payable.
            _one_tap = sum(player.mana_pool.values())
            for _src in player.untapped_mana_sources():
                _prod = player._get_mana_production(_src)
                if _prod:
                    _one_tap += max(_prod.values())
            print(f"[MANA-DIVERGENCE] {card_name} ({effective_mana_cost}): "
                  f"req={dict(req)} usable={usable} shortfalls={shortfalls} "
                  f"one_tap_total={_one_tap} "
                  f"summon_sick_mana_creatures={sick_count}")
        except Exception as _md_e:
            print(f"[MANA-DIVERGENCE] trace failed for {card_name}: {_md_e}")
        try:
            _state_hash = hash((len(game.stack), game.phase.value, player.life,
                                len(player.hand), len(player.battlefield),
                                available, effective_cmc)) & 0xFFFF
        except Exception:
            _state_hash = 0
        try:
            detailed = player.available_mana_detailed()
            mana_summary = ", ".join(
                f"{c}:{detailed.get(c, 0)}" for c in ('W', 'U', 'B', 'R', 'G', 'C', 'any')
            )
        except Exception:
            mana_summary = "unavailable"
        return (f"Cannot cast {card_name} (unknown reason — mana looks sufficient; "
                f"state#{_state_hash:04x}; pool: {mana_summary}; need: "
                f"{effective_mana_cost})")
    
    elif action_type == "play_land":
        if player.lands_played_this_turn >= player.max_lands_per_turn:
            return "Already played a land this turn"
        card_name = action.get("card")
        if not card_name:
            return None
        card = player.find_card(card_name, Zone.HAND)
        if not card:
            # [PLAN-STALE] Stale plan — land was already played earlier in the turn.
            # Return a tagged message so the retry loop can recognize and skip silently.
            return f"[PLAN-STALE] '{card_name}' not in hand (likely already played this turn)"
        if not card.is_land():
            return f"'{card_name}' is not a land"
    
    elif action_type == "suspend":
        # July 30: the suspend branches stash their real failure reason.
        _lsf = getattr(game, '_last_activation_failure', None)
        if _lsf:
            _lsf_turn, _lsf_name, _lsf_msg = _lsf
            _want = str(action.get("card", "")).lower()
            if (_lsf_turn == game.turn_number and _want
                    and _lsf_name.lower() == _want):
                game._last_activation_failure = None
                return _lsf_msg
        return f"Could not suspend {action.get('card', '?')}"

    elif action_type in ("graveyard_activate", "foretell", "suspend_only_marker"):
        # Aug 3: both new action types stash their real reason on
        # _last_activation_failure (the July-30 pattern). Without a branch
        # here the re-derivation below never runs for them and the AI's
        # rejection-feedback loop gets None — the Rhys-the-Redeemed failure
        # mode, where 8 failed activations fed back nothing for 24 turns.
        _laf = getattr(game, '_last_activation_failure', None)
        if _laf:
            _laf_turn, _laf_name, _laf_msg = _laf
            _want = str(action.get("card", "")).lower()
            if (_laf_turn == game.turn_number and _want
                    and _laf_name.lower() == _want):
                game._last_activation_failure = None
                return _laf_msg
        _verb = ("foretell" if action_type == "foretell"
                 else f"{action.get('mechanic', 'activate')} from the graveyard")
        return f"Could not {_verb} {action.get('card', '?')}"

    elif action_type == "attack":
        # Aug 7 confirmation-batch audit (B-5): no branch existed for the
        # (undocumented) {"type": "attack"} action, so its failures fell to
        # the terminal "unknown reason" and the model varied JSON syntax
        # blindly for 3 retries. Surface the stash the engine branch now
        # writes; fall back to the teaching message either way.
        _laa = getattr(game, '_last_attack_action_failure', None)
        if _laa and _laa[0] == game.turn_number:
            game._last_attack_action_failure = None
            return _laa[1]
        return ("attack is not a plan action — attackers are declared during "
                "the Declare Attackers step (decide_attackers)")

    elif action_type == "activate":
        perm_name = action.get("permanent")
        ability_idx = action.get("ability", 0)
        # July 30 batch-9 (deferred July 29 item): surface the REAL failure
        # reason the activate branch just stashed — the re-derivation below
        # has no summoning-sickness or affordability checks, so Rhys the
        # Redeemed's 8 failed activations fed the AI None/'' for 24 turns
        # and the rejection-feedback loop never engaged. Twin of the
        # _last_cast_failure stash (July 20).
        _laf = getattr(game, '_last_activation_failure', None)
        if _laf:
            _laf_turn, _laf_name, _laf_msg = _laf
            if (_laf_turn == game.turn_number and perm_name
                    and _laf_name.lower() == str(perm_name).lower()):
                game._last_activation_failure = None
                return _laf_msg
        # Find the permanent
        perm = player.find_card(perm_name, Zone.BATTLEFIELD)
        if not perm:
            # Fuzzy match (same as _execute_action)
            if perm_name:
                perm_lower = perm_name.lower()
                for c in player.battlefield:
                    if perm_lower in c.name.lower() or c.name.lower().startswith(perm_lower):
                        perm = c
                        break
        if not perm:
            # Check hand for cycling/channel abilities (activated from hand, not battlefield)
            if perm_name:
                perm_lower = perm_name.lower()
                for c in player.hand:
                    if perm_lower in c.name.lower() or c.name.lower().startswith(perm_lower):
                        oracle = (c.oracle_text or '').lower()
                        if 'cycling' in oracle or 'channel' in oracle:
                            perm = c
                            break
            if not perm:
                return f"'{perm_name}' not on your battlefield"
        # Planeswalker: check via planeswalker_manager
        if perm.is_planeswalker() and engine.planeswalker_manager:
            abilities = engine.planeswalker_manager.parse_abilities(perm)
            normalized = _normalize_pw_ability_idx(ability_idx, abilities)
            if normalized is None:
                # June 10 audit (Ashiok retry deadlock): the AI alternated
                # between two wrong formats for 3 retries because neither
                # error taught the expected shape. Spell out valid indices
                # AND the loyalty costs so either addressing mode works.
                _costs = ", ".join(f"index {i} = [{a.loyalty_cost:+d}]"
                                   for i, a in enumerate(abilities))
                return (f"'{perm.name}' ability '{ability_idx}' is not a valid index or "
                        f"loyalty cost (has {len(abilities)} abilities: {_costs}). "
                        f"Use a 0-based \"ability\" index from that list, and if the "
                        f"ability needs a target include \"target\": \"<player or card name>\" "
                        f"in the SAME action.")
            ability_idx = normalized
            can_act, reason = engine.planeswalker_manager.can_activate(game, player, perm, ability_idx)
            if not can_act:
                return f"Cannot activate {perm.name}: {reason}"
            # Bug 2a: if the previous attempt failed with needs_targets=True,
            # surface the target prompt here so last_error is meaningful.
            pw_err = getattr(game, '_last_pw_target_error', None)
            if pw_err:
                game._last_pw_target_error = None
                # June 10 (Ashiok): teach the retry shape — keep the SAME
                # ability index and ADD the target field, instead of
                # switching to a different (invalid) index.
                return (f"{pw_err} — retry with the SAME \"ability\" index plus "
                        f"\"target\": \"<name>\" in the same action")
            return None  # Valid
        # Non-planeswalker: check for activated abilities
        has_activated = False
        if perm.oracle_text:
            for line in perm.oracle_text.split('\n'):
                if ':' in line and not line.strip().startswith('('):
                    parts = line.split(':', 1)
                    if len(parts) == 2:
                        cost = parts[0].strip()
                        if not any(kw in cost.lower() for kw in ['when', 'whenever', 'at the beginning']):
                            if not re.match(r'^[+-]?\d+$', cost):
                                has_activated = True
                                break
        if not has_activated:
            return f"'{perm_name}' has no activated abilities (it may have triggered abilities that fire automatically)"
        if perm.tapped:
            return f"'{perm_name}' is already tapped"
        return None  # Valid - proceed to execution

    # Aug 8 batch audit (#11): an UNRECOGNIZED action type used to fall out
    # as the generic "unknown reason" — the model emitted 'pas' (a typo'd
    # pass) and 'error' this batch and learned nothing from the reply. Name
    # the offending type and the valid vocabulary (the Ashiok retry-teaching
    # pattern). repr() guards non-string types (the Aug-4 Qwen
    # {"adventure": true} lesson). KNOWN_PLAN_ACTION_TYPES is pinned against
    # the provider grammar by TestProviderExecutorConsistency.
    if action_type not in KNOWN_PLAN_ACTION_TYPES:
        return (f"unknown action type {action_type!r} — valid types are: "
                + ", ".join(sorted(KNOWN_PLAN_ACTION_TYPES)))
    return "Action failed (unknown reason)"


async def _validate_activation(engine, game: GameState, player: 'Player',
                               card: 'Card', ability_cost: str = None) -> Tuple[bool, str]:
    """Validate whether an activated ability can be used.

    Cross-checks with XMage bridge when available, plus Python-side fallback
    checks for mana cost, sorcery speed, and summoning sickness.

    Returns:
        (is_legal, reason) — True if legal, False with reason if not.
    """
    oracle = card.oracle_text.lower() if card.oracle_text else ""

    # --- Python-side sorcery speed check ---
    if "activate only as a sorcery" in oracle or \
       "activate this ability only any time you could cast a sorcery" in oracle:
        if game.phase not in (Phase.MAIN1, Phase.MAIN2):
            return False, "Can only activate as a sorcery (not in a main phase)"
        if len(game.stack) > 0:
            return False, "Can only activate as a sorcery (stack must be empty)"

    # --- Python-side mana cost check (from ability cost text) ---
    if ability_cost:
        from mtg.engine import _activation_mana_cost
        mana_cost = _activation_mana_cost(ability_cost)
        if mana_cost:
            can_pay, reason = player.can_pay_mana_cost(mana_cost)
            if not can_pay:
                return False, reason

    # --- XMage bridge cross-check (when available) ---
    if engine._xmage_available and engine.xmage_bridge:
        try:
            xmage_state, _ = engine._serialize_for_xmage(game)
            result = await engine.xmage_bridge.validate_activate(
                card.name, xmage_state
            )
            # Result is nested: {"action": "activate", "card": ..., "legal": {...}}
            legal_data = result.get("legal", {})
            if isinstance(legal_data, dict):
                is_legal = legal_data.get("legal", True)
                reason = legal_data.get("reason", "")
                if not is_legal:
                    print(f"[VALIDATE-ACTIVATE] XMage blocked {card.name}: {reason}")
                    return False, reason
                # Log ability info for debugging
                abilities = legal_data.get("abilities", [])
                if abilities:
                    print(f"[VALIDATE-ACTIVATE] XMage found {len(abilities)} activated abilities for {card.name}")
            elif isinstance(legal_data, bool):
                if not legal_data:
                    print(f"[VALIDATE-ACTIVATE] XMage blocked {card.name}")
                    return False, "Activation not legal (XMage)"
        except Exception as e:
            # Bridge error — graceful degradation, don't block
            print(f"[VALIDATE-ACTIVATE] Bridge error for {card.name}: {e}")

    return True, ""
