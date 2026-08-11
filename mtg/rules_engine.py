"""RulesEngine — MTG rules enforcement + Claude-as-judge.

The single biggest class in the engine. Owns:

    - Action interpreter (_execute_action_on_state and 81 action types)
    - State-based actions dispatch (with rules/state_based_actions.py)
    - Combat math (damage assignment, lifelink, deathtouch)
    - Trigger scanning (queue + auto-resolution)
    - Layer effect application (with rules/layers.py)
    - Replacement effects (with rules/replacement.py)
    - Targeting validation (with rules/targeting.py)
    - Tier 3 Claude-judge fallback (resolve_effect → JSON actions)

Phase 2 of the OSS refactor would split this further into actions.py,
triggers.py, combat.py — but that requires breaking up the class itself,
which is a much bigger change than the Phase 1 mechanical extraction.
For now, RulesEngine stays as one large coherent class.

Extracted from mtg_game.py during the Phase 1 OSS-readability refactor.
"""

import asyncio
import json
import random
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import anthropic

from mtg.constants import Phase, Zone, COMMAND_ZONE_FORMATS
from mtg.helpers import response_text
from mtg.models import Card, Player, GameState
from mtg.deck_loader import DeckLoader
from mtg.display import GameDisplay

# Optional: 7-layer continuous effects (CR 613)
try:
    from rules.layers import (
        Layer, create_pump_effect, create_control_effect, create_copy_effect,
    )
    HAS_LAYERS_ENGINE = True
except ImportError:
    HAS_LAYERS_ENGINE = False

# Optional: "if would, instead" replacement effects
try:
    from rules.replacement import (
        ReplacementEngine, ReplacementEffect, GameEvent, EventType,
        scan_oracle_for_replacements,
    )
    HAS_REPLACEMENT_ENGINE = True
except ImportError:
    HAS_REPLACEMENT_ENGINE = False

# Optional: pre-cast target legality
try:
    from rules.targeting_helpers import (
        _validate_target_for_action, _validate_player_target_for_action,
    )
    HAS_TARGETING = True
except ImportError:
    HAS_TARGETING = False

# Optional: state-based actions checker (used via local imports inside methods)
try:
    from rules.sba_adapter import compare_with_rules_sba as _sba_compare
    HAS_SBA_CHECKER = True
except ImportError:
    HAS_SBA_CHECKER = False
    _sba_compare = None

# Optional: card-specific effect templates (Tier 1.5)
try:
    from rules.effect_templates import get_effect_library, build_game_context
    HAS_EFFECT_TEMPLATES = True
except ImportError:
    HAS_EFFECT_TEMPLATES = False


# =============================================================================
# RULES ENGINE
# =============================================================================

class RulesEngine:
    """
    Enforces MTG rules with Claude-assisted judging for complex interactions.
    
    Enforcement Levels:
    - STRICT: Enforced automatically, invalid actions rejected
    - WARNED: Allowed but warns about potential rule issues  
    - JUDGE: Asks Claude to interpret complex interactions
    """
    
    def __init__(self, claude_client: anthropic.Anthropic = None, model: str = "claude-sonnet-5", usage_callback=None):
        self.client = claude_client
        self.model = model
        self.usage_callback = usage_callback  # Track API costs
        self.game_log: List[str] = []  # Track game events for context
        # Judge description cache — avoid recomputing for batched trigger resolution
        self._cached_judge_desc = None
        self._cached_judge_fingerprint = None
        # Per-turn resolve_effect dedupe cache. Keys: (source_card, effect_desc, turn);
        # records whether the same (card, effect) was already resolved this turn so
        # a duplicate call doesn't spam Discord or re-burn API tokens when a trigger
        # fires from two paths (e.g. template miss + fallback).
        self._resolve_dedupe: Dict[Tuple[str, str, int], bool] = {}
    
    # =========================================================================
    # EASY ENFORCEMENT (Automatic)
    # =========================================================================
    
    def _opponent_prevents_library_search(self, game: GameState, player: Player) -> Optional[str]:
        """Check if an opponent's static ability prevents this player from searching their library.

        Returns the preventing card's name if search is blocked, None otherwise.
        Handles: Ashiok, Dream Render; Leonin Arbiter (partial — would need mana payment);
        Opposition Agent (redirects, not prevents — not handled here).
        """
        for opp in game.players:
            if opp == player:
                continue
            for bf_card in opp.battlefield:
                oracle_lower = (bf_card.oracle_text or '').lower()
                # Ashiok, Dream Render: "Spells and abilities your opponents control
                # can't cause their controller to search their library."
                if ("can't" in oracle_lower and "search" in oracle_lower
                        and "library" in oracle_lower
                        and ("opponent" in oracle_lower or "player" in oracle_lower)):
                    return bf_card.name
        return None

    def can_play_land(self, game: GameState, player: Player) -> Tuple[bool, str]:
        """Check if player can play a land this turn."""
        if game.phase not in [Phase.MAIN1, Phase.MAIN2]:
            return False, "Can only play lands during main phases"
        if player != game.active_player:
            return False, "Can only play lands on your turn"
        if player.lands_played_this_turn >= player.max_lands_per_turn:
            return False, f"Already played {player.lands_played_this_turn} land(s) this turn"
        return True, "OK"
    
    def can_cast_spell(self, game: GameState, player: Player, card: Card) -> Tuple[bool, str]:
        """Check if player can cast a spell."""
        # Tokens can't be cast as spells
        if getattr(card, 'is_token', False):
            return False, "Tokens cannot be cast as spells"
        # Lands can't be cast — they're played via play_land action
        if card.is_land():
            return False, "Lands cannot be cast as spells — use play_land action"

        # Oathbreaker: a signature spell can only be cast if its owner's
        # Oathbreaker (a planeswalker commander) is on the battlefield under
        # their control.
        if getattr(card, 'is_signature_spell', False) and game.format == "oathbreaker":
            oathbreaker_on_battlefield = any(
                getattr(perm, 'is_commander', False) and perm.is_planeswalker()
                for perm in player.battlefield
            )
            if not oathbreaker_on_battlefield:
                return False, (f"{card.name} is a signature spell and can only be cast "
                               f"while your Oathbreaker is on the battlefield")

        # Check timing — but Flash creatures/artifacts/etc. bypass sorcery-speed restrictions
        has_flash = card.has_keyword('Flash') or (card.oracle_text and 'flash' in card.oracle_text.lower().split('\n')[0])
        # Madness (CR 702.35a): the cast happens as the madness trigger
        # resolves and ignores timing — a Bloodmad Vampire discarded to the
        # opponent's Wheel is castable mid-resolution, off-turn. Without
        # this, the sorcery-speed gate below blocked every madness cast of
        # a creature/sorcery that wasn't on the owner's own main phase.
        if getattr(card, '_cast_via_madness', False):
            has_flash = True
        # Miracle (CR 702.94a): the cast happens as the miracle trigger
        # resolves, which is during the DRAW STEP — so the sorcery-speed gate
        # would reject every miracle sorcery, i.e. most of them (Terminus,
        # Entreat the Angels, Reforge the Soul are all sorceries).
        if getattr(card, '_cast_via_miracle', False):
            has_flash = True
        if getattr(card, '_cast_via_effect', False):
            has_flash = True

        # Split card: check if the half being cast is an instant (instant-speed)
        if getattr(card, 'cast_as_split_half', -1) >= 0 and card.split_types:
            half_type = card.split_types[card.cast_as_split_half].lower()
            if 'instant' in half_type:
                has_flash = True  # Instant half can be cast at instant speed

        if not has_flash and (card.is_sorcery() or card.is_creature() or card.is_artifact() or card.is_enchantment() or card.is_planeswalker()):
            if game.phase not in [Phase.MAIN1, Phase.MAIN2]:
                return False, "Can only cast sorcery-speed spells during main phases"
            if player != game.active_player:
                return False, "Can only cast sorcery-speed spells on your turn"
            if game.stack:
                return False, "Can only cast sorcery-speed spells when stack is empty"

        # [PW-STATIC] Teferi, Time Raveler: "Each opponent can only cast spells
        # any time they could cast a sorcery."  This restricts ALL spells (even
        # instants) to sorcery speed for opponents. Flash does NOT bypass a
        # restriction on when a spell may be cast (CR 101.2, 307.5).
        for opp in game.players:
            if opp == player:
                continue
            for bf_card in opp.battlefield:
                oracle_lower = (bf_card.oracle_text or '').lower()
                if ('opponent' in oracle_lower and 'cast spells' in oracle_lower
                        and 'only' in oracle_lower and 'sorcery' in oracle_lower):
                    if game.phase not in [Phase.MAIN1, Phase.MAIN2]:
                        return False, f"Can only cast spells at sorcery speed ({bf_card.name})"
                    if player != game.active_player:
                        return False, f"Can only cast spells on your own turn ({bf_card.name})"
                    if game.stack:
                        return False, f"Can only cast spells with empty stack ({bf_card.name})"
                    print(f"[PW-STATIC] {bf_card.name} restricts {player.name} to sorcery speed")
                    break  # One Teferi is enough
        # Rashmi/Hellraiser-style effects grant permission to cast during the
        # resolving ability and waive the mana cost. Keep this after timing
        # restrictions so Teferi can still prohibit the cast, but before the
        # ordinary mana-affordability gate below.
        if getattr(card, '_cast_via_effect', False):
            return True, "OK (cast granted by resolving effect)"


        # Check for free-cast turn effect (Rishkar's Expertise, Cascade, etc.)
        if hasattr(game, 'turn_effects'):
            player_idx = game.players.index(player) if player in game.players else 0
            for te in game.turn_effects:
                if (te.get('type') == 'free_cast'
                        and te.get('controller') == player_idx
                        and not te.get('used', False)):
                    max_mv = te.get('max_mv', 5)
                    if (card.cmc or 0) <= max_mv:
                        return True, "OK (free cast available)"

        # Check mana — use adventure/split-half/flashback cost if casting via
        # one of those alternative cost paths. The graveyard-cast pipeline
        # (Snapcaster, native flashback, escape) stashes the alt cost in
        # `card._flashback_cost` BEFORE calling cast_spell_async, but this
        # check ran against `card.mana_cost` (the regular cost) and rejected
        # Lingering Souls from-graveyard flashback ({1}{B}) with "Not enough
        # white mana" because the regular cost was {2}{W}. Honor the alt
        # cost here too. May 13 audit.
        mana_cost_to_check = card.mana_cost
        if getattr(card, 'cast_as_adventure', False) and card.adventure_cost:
            mana_cost_to_check = card.adventure_cost
        elif getattr(card, 'cast_as_split_half', -1) >= 0 and card.split_costs:
            mana_cost_to_check = card.split_costs[card.cast_as_split_half]
        elif getattr(card, '_flashback_cost', None):
            # Flashback (or Snapcaster-granted flashback). The graveyard-cast
            # pipeline shuffles the card briefly into `player.hand` to use
            # the same cast machinery, so don't gate on zone — the
            # `_flashback_cost` marker is the authoritative signal.
            mana_cost_to_check = card._flashback_cost
        elif getattr(card, '_cast_via_madness', False) and getattr(card, '_madness_cost', None):
            # Madness (CR 702.35): the drain pre-moves the card exile→hand
            # and charges the madness cost — usually CHEAPER than printed
            # (Violent Eruption {1}{R}{R}{R} → {1}{R}{R}), so gating on the
            # printed cost would wrongly reject casts the payment stage can
            # cover (the FoW-waiver class, cost-selection flavor).
            mana_cost_to_check = card._madness_cost
        elif getattr(card, '_cast_via_miracle', False) and getattr(card, '_miracle_cost', None):
            # Miracle (CR 702.94a) — same cost-selection waiver as madness,
            # and a much bigger gap: Terminus is {4}{W}{W} printed and {W}
            # for its miracle cost, so the printed-cost gate would reject
            # nearly every miracle the payment stage could actually pay.
            mana_cost_to_check = card._miracle_cost
        elif getattr(card, '_cast_via_foretell', False) and getattr(card, '_foretell_cost', None):
            # Foretell (CR 702.143b) — likewise cheaper than printed.
            mana_cost_to_check = card._foretell_cost
        # July 20 (queued from the cast-gate characterization pins): the mana
        # pre-gate is convoke/delve/improvise-aware. The payment stage covers
        # part of the GENERIC portion by tapping creatures (convoke), exiling
        # graveyard cards (delve), or tapping noncreature artifacts
        # (improvise), so demand only the remainder here — a spell castable
        # via convoke alone used to be rejected up front (the pin comments in
        # tests/test_cast_gates.py described this; tests updated with the fix).
        # Generic-only reduction, matching what the payment stage models.
        _oracle_l = (card.oracle_text or '').lower()
        if mana_cost_to_check and ('convoke' in _oracle_l or 'delve' in _oracle_l
                                   or 'improvise' in _oracle_l):
            helpers = 0
            if 'convoke' in _oracle_l:
                helpers += sum(1 for c in player.active_battlefield()
                               if c.is_creature() and not c.tapped and c is not card)
            if 'delve' in _oracle_l:
                helpers += len(player.graveyard)
            if 'improvise' in _oracle_l:
                helpers += sum(1 for c in player.active_battlefield()
                               if c.is_artifact() and not c.is_creature()
                               and not c.tapped)
            if helpers > 0:
                reduced = re.sub(
                    r'\{(\d+)\}',
                    lambda m: '{' + str(max(0, int(m.group(1)) - helpers)) + '}',
                    mana_cost_to_check, count=1)
                if reduced != mana_cost_to_check:
                    print(f"[CAST-GATE] {card.name}: convoke/delve/improvise-"
                          f"aware pre-gate checking {reduced} (printed "
                          f"{mana_cost_to_check}, {helpers} helper(s))")
                mana_cost_to_check = reduced

        # July 26: same treatment for static cost reduction ("Black spells you
        # cast cost {1} less"). Without this the payment stage would happily
        # cast the cheaper spell but the pre-gate would reject it first, so the
        # AI would never even be offered it — the same doomed-gate asymmetry
        # the convoke awareness above was added to fix, and the reason Baral's
        # discount would still have been invisible in play.
        if mana_cost_to_check:
            from mtg.helpers import compute_cost_increase, compute_cost_reduction
            _from_gy = card.id in (getattr(player, 'playable_from_graveyard', None) or [])
            # CR 601.2f order: increases first, then reductions. Applying the
            # tax here matters as much as the discount — without it the AI is
            # offered spells a Sphere of Resistance makes unaffordable, and
            # burns the main phase on doomed casts (the retry-storm shape the
            # June 11 affordability filter exists to prevent).
            _inc, _inc_src = compute_cost_increase(game, player, card)
            if _inc > 0:
                _taxed = re.sub(
                    r'\{(\d+)\}',
                    lambda m: '{' + str(int(m.group(1)) + _inc) + '}',
                    mana_cost_to_check, count=1)
                if _taxed == mana_cost_to_check:
                    # No generic symbol to grow — prepend one ({U}{U} -> {2}{U}{U}).
                    _taxed = '{' + str(_inc) + '}' + mana_cost_to_check
                print(f"[CAST-GATE] {card.name}: cost-increase-aware pre-gate "
                      f"checking {_taxed} (printed {mana_cost_to_check}, "
                      f"+{_inc} from {', '.join(_inc_src)})")
                mana_cost_to_check = _taxed
            _red, _red_src = compute_cost_reduction(player, card,
                                                    from_graveyard=_from_gy)
            # Affinity (CR 702.41a) reduces the same generic portion, and the
            # gap it closes is the widest of any reducer: Icebreaker Kraken is
            # printed {10}{U}{U} and costs {U}{U} on a ten-snow-land board.
            # Without the pre-gate knowing that, the payment stage would
            # happily pay it and the AI would never be offered the card.
            from mtg.helpers import compute_affinity_reduction
            _aff, _aff_phrase = compute_affinity_reduction(player, card)
            if _aff > 0:
                _red += _aff
                _red_src = list(_red_src) + [f"affinity for {_aff_phrase}"]
            # Aug 10 (F1): a spell that discounts ITSELF longhand (Blasphemous
            # Act, Embercleave). Joins the SAME budget as affinity and the
            # static reducers, which is what stops them double-applying. Wiring
            # the pre-gate matters as much as the payment stage: without it the
            # AI is never offered the card at all (Blasphemous Act was rejected
            # for mana four turns running, then paid 9 for a 3-mana spell).
            from mtg.helpers import compute_self_cost_reduction
            _self_red, _self_dom = compute_self_cost_reduction(game, player, card)
            if _self_red > 0:
                _red += _self_red
                _red_src = list(_red_src) + [f"self-reduction per {_self_dom}"]
            if _red > 0:
                reduced = re.sub(
                    r'\{(\d+)\}',
                    lambda m: '{' + str(max(0, int(m.group(1)) - _red)) + '}',
                    mana_cost_to_check, count=1)
                if reduced != mana_cost_to_check:
                    print(f"[CAST-GATE] {card.name}: cost-reduction-aware "
                          f"pre-gate checking {reduced} (printed "
                          f"{mana_cost_to_check}, -{_red} from "
                          f"{', '.join(_red_src)})")
                    mana_cost_to_check = reduced

        can_pay, reason = player.can_pay_mana_cost(
            mana_cost_to_check, spending_card=card)
        if not can_pay:
            # July 20: printed alternate costs (Force of Will's life+exile,
            # Fireblast's sacrifice) are payable with no mana at all — the
            # cast stage's _compute_alt_costs takes that path whenever raw
            # mana is short. Without this waiver the response filter could
            # offer FoW but the pre-gate would still reject the cast
            # (doomed-cast retry loop, the exact pattern the June 11
            # affordability filter was built to prevent).
            if player.can_pay_printed_alternate_cost(card):
                print(f"[CAST-GATE] {card.name}: printed alternate cost "
                      f"available — mana pre-gate waived")
                return True, "OK (printed alternate cost available)"
            # Spectacle (CR 702.137, Aug 1): usually CHEAPER than printed
            # (Light Up the Stage {2}{R} → {R}) — when the condition is met
            # and the spectacle cost is payable, _compute_alt_costs will
            # take it, so the pre-gate must not reject the cast first.
            from mtg.helpers import spectacle_available
            _spec = spectacle_available(game, player, card)
            if _spec:
                _spec_ok, _spec_reason = player.can_pay_mana_cost(
                    _spec, spending_card=card)
                if _spec_ok:
                    print(f"[CAST-GATE] {card.name}: spectacle cost {_spec} "
                          f"available — mana pre-gate passes")
                    return True, "OK (spectacle cost available)"
            # Impending (CR 702.166, Aug 3): the same shape — an ALTERNATIVE
            # cost that is cheaper than printed, which _compute_alt_costs will
            # take. Without this waiver the pre-gate rejects first and the AI
            # is never offered the card at its impending cost: Overlord of the
            # Boilerbilges reported "only 4 untapped source(s) for 6 total
            # mana" on a board that could pay its {2}{R}{R} four times over.
            from mtg.helpers import parse_impending
            _imp = parse_impending(card.oracle_text)
            if _imp:
                _imp_ok, _ = player.can_pay_mana_cost(
                    _imp[1], spending_card=card)
                if _imp_ok:
                    print(f"[CAST-GATE] {card.name}: impending cost {_imp[1]} "
                          f"available — mana pre-gate passes")
                    return True, "OK (impending cost available)"
            return False, reason

        return True, "OK"
    
    def can_attack_with(self, game: GameState, player: Player, creature: Card) -> Tuple[bool, str]:
        """Check if a creature can attack."""
        if game.phase != Phase.DECLARE_ATTACKERS:
            return False, "Can only declare attackers during declare attackers step"
        if player != game.active_player:
            return False, "Only active player can attack"
        if not creature.can_attack(game=game):
            if creature.tapped:
                return False, f"{creature.name} is tapped"
            if creature.has_defender():
                return False, f"{creature.name} has Defender and can't attack"
            if creature.summoning_sick and not creature.has_haste():
                return False, f"{creature.name} has summoning sickness"
            # Aura-based restriction (Pacifism, Arrest, etc.)
            try:
                for aura in creature._get_attached_auras(game):
                    oracle = (getattr(aura, 'oracle_text', '') or '').lower()
                    if "can't attack" in oracle:
                        return False, f"{creature.name} can't attack ({aura.name})"
            except Exception:
                pass
            return False, f"{creature.name} can't attack"
        # Cost-to-attack restrictions (Propaganda, Ghostly Prison, Sphere of
        # Safety, War Tax, Crawlspace etc.). May 17 audit: previously not
        # enforced — Claude would happily attack into Propaganda for free.
        # Heuristic: when a defending opponent controls a "creatures can't
        # attack <player> unless their controller pays {N}" effect, require
        # the attacker's controller to have N+ untapped mana per attacker.
        # We don't model the actual mana payment; we just block the attack
        # when the cost can't plausibly be paid.
        try:
            tax_per_attacker = 0
            tax_sources = []
            for opp in game.players:
                if opp is player:
                    continue
                for perm in opp.battlefield:
                    oracle = (perm.oracle_text or '').lower()
                    # Match "creatures can't attack you unless their controller pays {N}"
                    # and a few common variants — Propaganda, Ghostly Prison,
                    # War Tax, Norn's Annex, Windborn Muse.
                    import re as _re
                    m = _re.search(
                        r"creatures can't attack(?: you| target player)?,? "
                        r"unless (?:their controller |the attacker's controller )?"
                        r"pays \{(\d+)\}",
                        oracle,
                    )
                    if m:
                        tax_per_attacker += int(m.group(1))
                        tax_sources.append(perm.name)
                    if ("creatures can't attack you unless their controller pays {x}" in oracle
                            and "x is the number of enchantments you control" in oracle):
                        enchantments = sum(
                            1 for permanent in opp.battlefield
                            if 'enchantment' in (permanent.type_line or '').lower()
                        )
                        tax_per_attacker += enchantments
                        tax_sources.append(perm.name)
            if tax_per_attacker > 0:
                # Count creatures already declared as attackers this combat
                # (excluding the candidate) so the tax compounds.
                already = sum(
                    1 for c in player.battlefield
                    if c is not creature and getattr(c, 'attacking', False)
                )
                # Declarations are paid sequentially; previously this multiplied
                # by already-declared attackers even though earlier payments had
                # already tapped their sources, double-counting the tax.
                required = tax_per_attacker
                # Use the authoritative mana-source scan. Calling
                # _get_mana_production on every permanent treated vanilla
                # creatures as plausible sources and let taxed attacks through.
                available = sum(player.available_mana_detailed().values())
                if available < required:
                    src_str = ", ".join(tax_sources) if tax_sources else "attack-tax effect"
                    return False, (f"{creature.name} can't attack — opponent's {src_str} "
                                   f"requires {{{required}}} but only {available} mana available")
        except Exception:
            # Defensive: don't block attacks on a parsing error
            pass
        return True, "OK"

    def attack_tax_for(self, game: GameState, player: Player) -> Tuple[int, List[str]]:
        """Return the generic mana cost for one creature to attack opponents."""
        import re as _re
        total = 0
        sources = []
        for opponent in game.players:
            if opponent is player:
                continue
            for permanent in opponent.battlefield:
                oracle = (permanent.oracle_text or '').lower()
                match = _re.search(
                    r"creatures can't attack(?: you| target player)?,? "
                    r"unless (?:their controller |the attacker's controller )?"
                    r"pays \{(\d+)\}", oracle)
                if match:
                    total += int(match.group(1))
                    sources.append(permanent.name)
                if ("creatures can't attack you unless their controller pays {x}" in oracle
                        and "x is the number of enchantments you control" in oracle):
                    total += sum(1 for c in opponent.battlefield
                                 if 'enchantment' in (c.type_line or '').lower())
                    sources.append(permanent.name)
        return total, sources

    def pay_attack_tax(self, game: GameState, player: Player,
                       creature: Card) -> Tuple[bool, str]:
        """Actually pay the cost to declare one attacker (CR 508.1h)."""
        amount, sources = self.attack_tax_for(game, player)
        if amount <= 0:
            return True, ""
        cost = f"{{{amount}}}"
        if not player.tap_sources_for_cost(cost, game=game):
            return False, (f"{creature.name} can't attack — unable to pay {cost} "
                           f"for {', '.join(sources) or 'attack tax'}")
        print(f"[ATTACK-TAX] {player.name} pays {cost} for {creature.name} "
              f"({', '.join(sources)})")
        return True, f"Paid {cost} attack tax"

    def can_block_with(self, game: GameState, player: Player, blocker: Card, attacker: Card) -> Tuple[bool, str]:
        """Check if a creature can block a specific attacker."""
        if game.phase != Phase.DECLARE_BLOCKERS:
            return False, "Can only declare blockers during declare blockers step"
        if player == game.active_player:
            return False, "Active player cannot declare blockers"
        if not blocker.can_block(attacker, game=game):
            if blocker.tapped:
                return False, f"{blocker.name} is tapped"
            if attacker.has_flying() and not (blocker.has_flying() or blocker.has_reach()):
                return False, f"{blocker.name} can't block {attacker.name} (flying)"
            return False, f"{blocker.name} can't block {attacker.name}"
        return True, "OK"

    def _calculate_cda_value(self, creature: Card, game: GameState, player_index: int) -> int:
        """
        Calculate characteristic-defining ability (CDA) for */* creatures.
        These are Layer 7a effects that define P/T based on game state.
        Returns the calculated value, or 0 if the CDA can't be determined.
        """
        import re as _re
        oracle = (creature.oracle_text or '').lower()
        player = game.players[player_index]

        # "power and toughness are each equal to the number of lands you control"
        # Ulvenwald Hydra, Molimo, Dakkon Blackblade variants
        if 'number of lands you control' in oracle:
            return len(player.lands())

        # "power and toughness are each equal to the number of creatures you control"
        # Scion of the Wild, etc.
        if 'number of creatures you control' in oracle:
            return len(player.creatures())

        # "power and toughness are each equal to the number of cards in your hand"
        # Maro, Masumaro, Soramaro
        if 'number of cards in your hand' in oracle:
            return len(player.hand)

        # "power and toughness are each equal to your life total"
        # Serra Avatar
        if 'equal to your life total' in oracle:
            return player.life

        # "power and toughness are each equal to the number of creature cards in all graveyards"
        # Lord of Extinction, Mortivore
        if 'creature cards in all graveyards' in oracle:
            count = 0
            for p in game.players:
                count += sum(1 for c in p.graveyard if c.is_creature())
            return count

        # "power and toughness are each equal to the number of cards in your graveyard"
        # Cognivore, etc.
        if 'cards in your graveyard' in oracle:
            return len(player.graveyard)

        # Tarmogoyf: "equal to the number of card types among cards in all graveyards"
        if 'card types among cards in all graveyards' in oracle:
            types_seen = set()
            for p in game.players:
                for c in p.graveyard:
                    if c.is_creature():
                        types_seen.add('creature')
                    if c.is_land():
                        types_seen.add('land')
                    if c.is_instant():
                        types_seen.add('instant')
                    if c.is_sorcery():
                        types_seen.add('sorcery')
                    if c.is_artifact():
                        types_seen.add('artifact')
                    if c.is_enchantment():
                        types_seen.add('enchantment')
                    if c.is_planeswalker():
                        types_seen.add('planeswalker')
            return len(types_seen)

        # Death's Shadow (pre-2020 oracle): "13 minus your life total"
        life_minus = _re.search(r'(\d+) minus your life total', oracle)
        if life_minus:
            base_val = int(life_minus.group(1))
            return max(0, base_val - game.players[player_index].life)
        # Death's Shadow (current Scryfall oracle): "gets -X/-X, where X is your life total"
        # Base P/T is the printed value (13/13); the -X/-X is a Layer 7c continuous
        # effect that subtracts life total. So effective P/T = base - life. The base
        # is sourced from creature.power, not the regex — return base - life directly.
        # At life=16, 13/13 - 16/16 = -3/-3 → 0-toughness → CR 704.5f destruction.
        if _re.search(r'gets -x/-x.*x is your life total', oracle):
            base_pt = int(creature.power) if creature.power and str(creature.power).isdigit() else 13
            return max(0, base_pt - game.players[player_index].life)

        # Fallback: if creature has +1/+1 counters, it was probably an X-cost
        # creature like Walking Ballista / Hangarback Walker that enters as 0/0
        # with counters — the counters are already handled by the caller
        print(f"[CDA] Unknown CDA pattern for {creature.name}: {oracle[:80]}")
        return 0

    def _calculate_cda_toughness(self, creature: Card, game: GameState, player_index: int) -> int:
        """Calculate toughness for */* creatures using characteristic-defining abilities."""
        # Tarmogoyf is special: power = types, toughness = types + 1
        oracle = (creature.oracle_text or '').lower()
        if 'toughness is equal' in oracle and 'plus 1' in oracle:
            return self._calculate_cda_value(creature, game, player_index) + 1
        return self._calculate_cda_value(creature, game, player_index)

    def _calculate_cda_power(self, creature: Card, game: GameState, player_index: int) -> int:
        """Calculate power for */* creatures using characteristic-defining abilities."""
        return self._calculate_cda_value(creature, game, player_index)

    def _player_cant_lose(self, game: GameState, player_index: int) -> Optional[str]:
        """
        Check if a player can't lose the game due to a permanent effect.
        Returns the name of the preventing card, or None if the player can lose normally.
        Checks for: "you can't lose the game" on the player's battlefield,
        and "your opponents can't win the game" on the opponent's battlefield.
        """
        player = game.players[player_index]
        # Check player's own battlefield for "you can't lose the game"
        for card in player.battlefield:
            oracle = (card.oracle_text or "").lower()
            if "you can't lose the game" in oracle:
                return card.name
        # Check opponent's battlefield for "your opponents can't win the game"
        # (rare, but e.g. if opponent controls Platinum Angel, THEY can't lose —
        #  the "opponents can't win" clause is about the controller's opponents)
        # Actually: "your opponents can't win the game" prevents the opponents from winning,
        # not from the controller losing. So we need to check if ANY opponent of this player
        # has "your opponents can't win" — that would prevent THIS player from winning,
        # not from losing. So this check is only relevant for win conditions, not loss.
        # For loss prevention, only "you can't lose" matters.
        return None

    def check_state_based_actions(self, game: GameState) -> List[Dict]:
        """Delegates to mtg.sba.check_state_based_actions (Phase 2C)."""
        from mtg.sba import check_state_based_actions
        return check_state_based_actions(self, game)
    def _check_sba_inline_fallback(self, game: GameState) -> List[Dict]:
        """Delegates to mtg.sba.check_sba_inline_fallback (Phase 2C)."""
        from mtg.sba import check_sba_inline_fallback
        return check_sba_inline_fallback(self, game)
    def _permanent_grants_undying(self, game, card, player) -> bool:
        """Check if another permanent grants undying to this creature (e.g., Mikaeus, the Unhallowed)."""
        oracle_lower = (card.oracle_text or '').lower()
        # Card has undying in its own text
        if 'undying' in oracle_lower and 'other' not in oracle_lower:
            return True
        # Check other permanents that grant undying
        for perm in player.battlefield:
            if perm.id == card.id:
                continue
            perm_oracle = (perm.oracle_text or '').lower()
            # Mikaeus: "Other non-Human creatures you control have undying"
            if 'have undying' in perm_oracle or 'gains undying' in perm_oracle:
                # Check "non-Human" restriction
                if 'non-human' in perm_oracle:
                    card_types = (card.type_line or '').lower()
                    if 'human' in card_types:
                        continue  # Human creatures don't get undying from Mikaeus
                return True
        return False

    @staticmethod
    def _totem_armor_search_zones(player, game):
        """Every battlefield an attached Aura could be sitting on.

        Aug 11 audit (reviewer D, F4): an Aura that enchants an OPPONENT's
        creature stays on its own CASTER's battlefield (cast_spell_async only
        sets `attached_to`, it never moves the Aura across battlefields). Both
        totem-armor helpers scanned `player.battlefield` alone, where `player`
        is the CREATURE's controller — so a cross-controller umbra could never
        be found and the save silently never happened. Live A/B in one game
        (game_1536540699103854662): Snake Umbra saved Silverback Elder
        (same controller, worked), Boar Umbra failed to save Kambal
        (Rick's Aura on Qwen's commander). CR 702.77b is controller-agnostic.
        """
        if game is None or not getattr(game, 'players', None):
            return [player]
        # `player` first so a same-controller Aura keeps winning ties exactly
        # as before — this widens the search, it never reorders the old hits.
        return [player] + [p for p in game.players if p is not player]

    def _has_totem_armor(self, creature: Card, player, game=None) -> bool:
        """Check if creature has an attached Aura with totem armor."""
        for owner in self._totem_armor_search_zones(player, game):
            for card in owner.battlefield:
                if card.is_enchantment() and 'Aura' in (card.type_line or ''):
                    if card.attached_to == creature.id:
                        oracle = (card.oracle_text or '').lower()
                        if 'totem armor' in oracle or 'umbra armor' in oracle:
                            return True
        return False

    def _remove_totem_armor(self, creature: Card, player, game) -> Card:
        """Remove and destroy a totem armor Aura from the creature. Returns the destroyed Aura.

        Aug 11: searches every battlefield (see _totem_armor_search_zones) and
        destroys the Aura into ITS OWN owner's graveyard, not the enchanted
        creature's controller's — CR 404.3. Removing a cross-controller Aura
        from `player.battlefield` would have raised ValueError.
        """
        for owner in self._totem_armor_search_zones(player, game):
            for card in list(owner.battlefield):
                if card.is_enchantment() and 'Aura' in (card.type_line or ''):
                    if card.attached_to == creature.id:
                        oracle = (card.oracle_text or '').lower()
                        if 'totem armor' in oracle or 'umbra armor' in oracle:
                            game.unregister_static_effects(card)
                            owner.battlefield.remove(card)
                            owner.graveyard.append(card)
                            print(f"[TOTEM-ARMOR] Destroyed {card.name} instead of {creature.name}")
                            return card
        return None

    def process_state_based_actions(self, game: GameState) -> List[str]:
        """Delegates to mtg.sba.process_state_based_actions (Phase 2C)."""
        from mtg.sba import process_state_based_actions
        return process_state_based_actions(self, game)
    def on_untap_step(self, game: GameState):
        """Handle untap step - phase in, untap permanents, clear summoning sickness for active player."""
        player = game.active_player

        # Phase in permanents (Teferi's Protection, Teferi's Veil, etc.)
        phased_in = []
        for card in player.battlefield:
            if getattr(card, '_phased_out', False):
                card._phased_out = False
                phased_in.append(card.name)
        if phased_in:
            print(f"[PHASE-IN] {player.name}'s permanents phase back in: {', '.join(phased_in[:5])}{'...' if len(phased_in) > 5 else ''}")
        # Clear phased out tracking
        if hasattr(game, '_phased_out_permanents') and player.name in game._phased_out_permanents:
            del game._phased_out_permanents[player.name]
        # Clear temporary replacement effects (Teferi's Protection, etc. expire at next untap)
        temp_ids = getattr(player, '_temp_replacement_effect_ids', [])
        if temp_ids:
            if HAS_REPLACEMENT_ENGINE and game._replacement_engine is not None:
                for effect_id in temp_ids:
                    game._replacement_engine.remove_effect(effect_id)
                    print(f"[REPLACEMENT] Removed temporary effect: {effect_id}")
            player._temp_replacement_effect_ids = []
        # Also clear legacy flag (in case replacement engine wasn't available)
        if getattr(player, '_damage_prevented', False):
            player._damage_prevented = False
        # Clear the Teferi's Protection life-total lock on the same schedule
        # as the damage-prevented flag.
        if getattr(player, '_life_total_locked', False):
            player._life_total_locked = False

        # Aug 10 deferred (C3): this loop — not GameEngine.untap_all — is where
        # the untap step actually happens. untap_all runs immediately after it
        # from end_turn, and advance_phase calls this WITHOUT untap_all at all,
        # so this is the only site that always runs. Two things follow:
        # `_skip_next_untap` (Icebreaker Kraken) is honoured here for the first
        # time (the blind `tapped = False` below used to untap the permanent
        # before untap_all's skip check could see it), and the tapped ->
        # untapped TRANSITION is captured so "becomes untapped" watchers
        # (Mesmeric Orb) can fire.
        from mtg.helpers import untap_permanent
        _became_untapped = []
        for card in player.battlefield:
            if untap_permanent(card):
                _became_untapped.append((card, player))
            # Clear summoning sickness for creatures that were sick
            if card.is_creature() and card.summoning_sick:
                card.summoning_sick = False
            # Clear "entered this turn" flag
            card.entered_this_turn = False
            # Clear stale combat flags
            if card.attacking:
                card.attacking = False
                card.attacking_player = None
            if card.blocking:
                card.blocking = []
            if card.blocked_by:
                card.blocked_by = []
        
        if _became_untapped:
            from mtg.triggers import fire_becomes_untapped_triggers
            _msgs = fire_becomes_untapped_triggers(game, _became_untapped)
            if _msgs:
                if not hasattr(game, '_pending_messages'):
                    game._pending_messages = []
                game._pending_messages.extend(_msgs)

        # Reset land count and landfall tracking
        player.lands_played_this_turn = 0
        player.landfall_count_this_turn = 0
        player.has_drawn_for_turn = False
        
        # Empty mana pools
        for p in game.players:
            p.empty_mana_pool()
            p._retain_mana_through_turn = None
    
    def on_end_step(self, game: GameState) -> List[str]:
        """Handle end step - clear damage, empty mana pools, discard to hand size, reset temporary modifiers."""
        messages = []

        # Discard to hand size (max 7) for active player
        # Check for "no maximum hand size" effects (Reliquary Tower, Thought Vessel, Spellbook, etc.)
        active = game.active_player
        active_idx = game.active_player_index
        has_no_max = False
        for card in active.battlefield:
            oracle = (card.oracle_text or "").lower()
            if "no maximum hand size" in oracle:
                has_no_max = True
                break
        max_hand = float('inf') if has_no_max else 7
        excess = len(active.hand) - int(max_hand) if max_hand != float('inf') else 0

        if excess > 0:
            if active.is_claude:
                # AI player: auto-discard worst cards (highest CMC non-land, or last drawn)
                for _ in range(excess):
                    if not active.hand:
                        break
                    # Pick the worst card to discard: highest CMC non-land, or if all lands, last card
                    non_lands = [c for c in active.hand if not c.is_land()]
                    if non_lands:
                        worst = max(non_lands, key=lambda c: c.cmc if c.cmc else 0)
                    else:
                        worst = active.hand[-1]
                    active.hand.remove(worst)
                    active.graveyard.append(worst)
                    messages.append(f"📤 {active.name} discards {worst.name} to hand size")
            else:
                # Human player: set pending action so they can choose
                game.pending_action = {
                    'type': 'discard_to_hand_size',
                    'player_idx': active_idx,
                    'cards_to_discard': excess,
                }
                hand_list = ", ".join(c.name for c in active.hand)
                messages.append(f"✋ **{active.name}** has {len(active.hand)} cards in hand (max {int(max_hand)}). Discard {excess} card(s).")
                messages.append(f"Use `!discard <card name>` to choose. Cards: {hand_list}")

        # [LAYERS] Clear end-of-turn temporary effects from layers engine
        if HAS_LAYERS_ENGINE and game.layers_engine:
            game.layers_engine.clear_temporary_effects("end_of_turn")

        # Clear damage and temporary modifiers from creatures
        for player in game.players:
            for card in player.battlefield:
                card.damage_marked = 0
                card.deathtouch_damage = 0
                # Reset "until end of turn" modifiers
                if card.power_modifier != 0 or card.toughness_modifier != 0:
                    card.power_modifier = 0
                    card.toughness_modifier = 0
                # Clear "can't block this turn" (Chandra Pyromaster +1, etc.)
                if getattr(card, 'cant_block_this_turn', False):
                    card.cant_block_this_turn = False
                # Revert animated lands (Living Lands, Awaken — the plain
                # until-end-of-turn class). Aug 1: "until your next turn"
                # animations (Sylvan Awakening) carry
                # _animated_expires_at_turn_of and are SKIPPED here — they
                # survive the opponent's turn as blockers and revert at
                # end_turn's turn-advance point instead.
                if (getattr(card, '_animated_until_eot', False)
                        and getattr(card, '_animated_expires_at_turn_of', None) is None):
                    self.revert_animation(card)
            player.empty_mana_pool()
            player._retain_mana_through_turn = None
        
        return messages
    
    def revert_animation(self, card) -> None:
        """Un-animate a temporarily animated land/artifact (extracted Aug 1
        so the end-step revert and the until-your-next-turn expiry share
        one implementation)."""
        if hasattr(card, '_original_type_line'):
            card.type_line = card._original_type_line
            del card._original_type_line
        for kw in (getattr(card, '_animated_keywords', []) or []):
            if card.keywords and kw in [k.lower() for k in card.keywords]:
                card.keywords = [k for k in card.keywords if k.lower() != kw]
        for attr in ('_animated_until_eot', '_animated_power',
                     '_animated_toughness', '_animated_keywords'):
            if hasattr(card, attr):
                delattr(card, attr)
        card._animated_expires_at_turn_of = None

    def on_phase_change(self, game: GameState, new_phase: Phase):
        """Handle phase transitions."""
        # Empty mana pools on phase change (technically should be each step but simplified)
        for player in game.players:
            retained = None
            if (getattr(player, '_retain_mana_through_turn', None)
                    == game.turn_number):
                # Kessig Naturalist: preserve the still-unspent floating mana.
                retained = dict(player.mana_pool)
            player.empty_mana_pool()
            if retained is not None:
                player.mana_pool.update(retained)
    
    # =========================================================================
    # COMBAT RESOLUTION (Medium - Keyword-aware)
    # =========================================================================
    
    def resolve_combat_damage(self, game: GameState) -> List[str]:
        """Delegates to mtg.combat.resolve_combat_damage (Phase 2D)."""
        from mtg.combat import resolve_combat_damage
        return resolve_combat_damage(self, game)
    def _apply_life_gain(self, game: GameState, player: 'PlayerState', amount: int,
                          source_name: str = "") -> Tuple[bool, int, List[str]]:
        """Delegates to mtg.combat.apply_life_gain (Phase 2D)."""
        from mtg.combat import apply_life_gain
        return apply_life_gain(self, game, player, amount, source_name)
    def _apply_combat_damage_to_player(self, game: GameState, player: 'PlayerState',
                                         amount: int, source_card: Card, is_combat: bool = True) -> int:
        """Delegates to mtg.combat.apply_combat_damage_to_player (Phase 2D)."""
        from mtg.combat import apply_combat_damage_to_player
        return apply_combat_damage_to_player(self, game, player, amount, source_card, is_combat)
    def _apply_combat_damage_to_creature(self, game: GameState, creature: Card,
                                          amount: int, source_card: Card,
                                          source_has_deathtouch: bool = False) -> int:
        """Delegates to mtg.combat.apply_combat_damage_to_creature (Phase 2D)."""
        from mtg.combat import apply_combat_damage_to_creature
        return apply_combat_damage_to_creature(self, game, creature, amount, source_card, source_has_deathtouch)
    def _apply_noncombat_damage_to_player(self, game: GameState, player: 'PlayerState',
                                           amount: int, source_name: str = "",
                                           source_id: str = "",
                                           source_controller: str = "") -> int:
        """Delegates to mtg.combat.apply_noncombat_damage_to_player (Phase 2D).

        Aug 7 (B-4): source_controller threads the caster identity from
        Tier-2 SpellResolver so Torbran-class "source you control" gates
        fire for spells already off the stack."""
        from mtg.combat import apply_noncombat_damage_to_player
        return apply_noncombat_damage_to_player(self, game, player, amount, source_name,
                                                source_id, source_controller)
    def _check_enters_tapped(self, game: GameState, card: Card, player) -> tuple:
        """
        Check if a permanent enters the battlefield tapped.
        Consolidates shockland, checkland, fastland, and unconditional ETB-tapped logic,
        plus ENTER_BATTLEFIELD replacement effects (Thalia, Blind Obedience, Kismet).
        Returns (enters_tapped: bool, etb_message: str).
        """
        enters_tapped = False
        etb_msg = ""
        oracle = (card.oracle_text or "").lower()

        # 1. Shocklands: "you may pay 2 life. If you don't, it enters tapped."
        if "you may pay 2 life" in oracle and "enters tapped" in oracle:
            if player.life > 4:
                player.life -= 2
                player.record_life_loss(2, game=game)
                etb_msg = f" (paid 2 life — life: {player.life})"
                print(f"[SHOCKLAND] {player.name} pays 2 life for {card.name} (life: {player.life})")
            else:
                enters_tapped = True
                etb_msg = " (entered tapped — didn't pay 2 life)"

        # 2. Check lands / fast lands: "enters tapped unless ..."
        elif "enters tapped unless" in oracle or "enters the battlefield tapped unless" in oracle:
            enters_tapped = True  # Default: enters tapped

            if "two or fewer other" in oracle:
                # Fast lands: "enters tapped unless you control two or fewer other lands"
                # (Blackcleave Cliffs, Darkslick Shores, Copperline Gorge, etc.)
                # Enter untapped if you control 0, 1, or 2 other lands
                other_land_count = sum(1 for c in player.lands() if c.id != card.id)
                if other_land_count <= 2:
                    enters_tapped = False
            elif "two or more basic lands" in oracle:
                # BFZ "tango" lands (Canopy Vista, Prairie Stream, etc.):
                # "enters tapped unless you control two or more basic lands".
                # June 11 audit: no branch handled the count-based wording, so
                # the whole cycle was unconditionally tapped regardless of
                # board state (game 1514629231433351168 turns 31/35).
                basic_count = sum(
                    1 for c in player.battlefield
                    if c.is_land() and c.id != card.id
                    and 'basic' in (c.type_line or '').lower())
                if basic_count >= 2:
                    enters_tapped = False
            else:
                # Check lands: "unless you control [land type(s)]"
                controlled_types = set()
                for bf_card in player.battlefield:
                    if bf_card.is_land() and bf_card.id != card.id:
                        bf_type = (bf_card.type_line or "").lower()
                        for basic in ["island", "swamp", "mountain", "forest", "plains"]:
                            if basic in bf_type:
                                controlled_types.add(basic)
                for basic in ["island", "swamp", "mountain", "forest", "plains"]:
                    if basic in oracle and basic in controlled_types:
                        enters_tapped = False
                        break

            if enters_tapped:
                etb_msg = " (entered tapped)"
            else:
                etb_msg = " (entered untapped)"

        # 3. Unconditional ETB-tapped (taplands, bounce lands, etc.)
        elif ("enters the battlefield tapped" in oracle or ("enters tapped" in oracle and "unless" not in oracle)):
            enters_tapped = True
            etb_msg = " (entered tapped)"

        # [REPLACEMENT] Check for enters-tapped replacement effects (Thalia, Kismet, Blind Obedience)
        if HAS_REPLACEMENT_ENGINE and game._replacement_engine and game._replacement_engine.effects:
            event = GameEvent(
                event_type=EventType.ENTER_BATTLEFIELD,
                affected_object=getattr(card, 'id', ''),
                affected_object_name=card.name,
                affected_player=player.name,
                # May 20 audit fix: populate entering_type_line so type-scoped
                # replacements (Authority of the Consuls = creatures only) can
                # filter correctly. Without this, Plains/Talisman were being
                # forced tapped by Authority.
                entering_type_line=getattr(card, 'type_line', '') or '',
            )
            final = game._replacement_engine.process_event_sync(event)
            if final.enters_tapped is not None and final.enters_tapped != enters_tapped:
                enters_tapped = final.enters_tapped
                if enters_tapped:
                    etb_msg = f" (enters tapped — {', '.join(final.replacement_chain)})"
                    print(f"  [REPLACEMENT-APPLY] ETB tapped forced: {card.name} ({', '.join(final.replacement_chain)})")

        if enters_tapped:
            card.tapped = True

        return enters_tapped, etb_msg

    def _make_replacement_callback(self, game: GameState, channel=None):
        """Delegates to mtg.combat.make_replacement_callback (Phase 2D)."""
        from mtg.combat import make_replacement_callback
        return make_replacement_callback(self, game, channel)
    def _deal_combat_damage(self, game: GameState, attackers: List[Tuple[Card, Player]], is_first_strike_step: bool = False, skip_attacker_damage: bool = False) -> Tuple[List[str], Dict[int, int]]:
        """Delegates to mtg.combat.deal_combat_damage (Phase 2D)."""
        from mtg.combat import deal_combat_damage
        return deal_combat_damage(self, game, attackers, is_first_strike_step, skip_attacker_damage)
    async def ask_judge(self, game: GameState, question: str, context: str = "") -> str:
        """Delegates to mtg.judge.ask_judge (Phase 2B)."""
        from mtg.judge import ask_judge
        return await ask_judge(self, game, question, context)
    async def ask_judge_with_fix(self, game: GameState, question: str,
                                  controller: str = "") -> str:
        """Delegates to mtg.judge.ask_judge_with_fix (Phase 2B)."""
        from mtg.judge import ask_judge_with_fix
        return await ask_judge_with_fix(self, game, question, controller)
    async def resolve_effect(self, game: GameState, effect_description: str,
                              source_card: str = "", controller: str = "",
                              context: str = "") -> Tuple[List[str], List[Dict]]:
        """Delegates to mtg.judge.resolve_effect (Phase 2B)."""
        from mtg.judge import resolve_effect
        return await resolve_effect(self, game, effect_description, source_card, controller, context)
    def _aggregate_counter_msgs(self, msgs: List[str]) -> List[str]:
        """Delegates to mtg.combat.aggregate_counter_msgs (Phase 2D)."""
        from mtg.combat import aggregate_counter_msgs
        return aggregate_counter_msgs(self, msgs)
    def _execute_action_on_state(self, game: GameState, action: Dict) -> Optional[str]:
        """Delegates to mtg.actions.execute_action_on_state (Phase 2A)."""
        from mtg.actions import execute_action_on_state
        return execute_action_on_state(self, game, action)
    async def check_triggers(self, game: GameState, event: str, card: Card = None) -> List[Dict]:
        """
        Ask Claude to identify triggered abilities from an event.
        Returns list of triggers to put on stack.
        
        Events: 'enters', 'dies', 'attacks', 'blocks', 'damage_dealt', 'spell_cast', etc.
        """
        if not self.client:
            return []
        
        # Check all cards in play for relevant triggers
        all_cards = []
        for player in game.players:
            for c in player.battlefield:
                if c.oracle_text and any(trigger in c.oracle_text.lower() for trigger in 
                    ['when', 'whenever', 'at the beginning', 'at end of']):
                    all_cards.append(c)
        
        if not all_cards:
            return []
        
        cards_text = "\n".join([f"- {c.name}: {c.oracle_text}" for c in all_cards])
        event_desc = f"Event: {event}"
        if card:
            event_desc += f" involving {card.name} ({card.oracle_text})"
        
        prompt = f"""Identify any triggered abilities that trigger from this event.

CARDS WITH TRIGGER TEXT IN PLAY:
{cards_text}

{event_desc}

List ONLY the triggers that actually trigger from this specific event. For each trigger, provide:
1. Card name
2. Trigger text
3. Whether it's mandatory or optional ("may")

Format as JSON array: [{{"card": "Name", "trigger": "text", "optional": true/false}}]
If no triggers, return empty array: []"""

        try:
            response = await asyncio.to_thread(
                self.client.messages.create,
                model=self.model,
                max_tokens=500,
                messages=[{"role": "user", "content": prompt}]
            )
            if self.usage_callback and hasattr(response, 'usage'):
                self.usage_callback(response.usage, self.model)

            # Parse JSON from response
            text = response_text(response).strip()
            # Extract JSON if wrapped in code blocks
            if "```" in text:
                text = re.search(r'```(?:json)?\s*(.*?)\s*```', text, re.DOTALL)
                text = text.group(1) if text else "[]"
            
            return json.loads(text)
        except Exception as e:
            print(f"Trigger check error: {e}")
            return []
    
    def _describe_game_for_judge(self, game: GameState) -> str:
        """Delegates to mtg.judge.describe_game_for_judge (Phase 2B)."""
        from mtg.judge import describe_game_for_judge
        return describe_game_for_judge(self, game)
    def log_event(self, event: str):
        """Add an event to the game log for context."""
        self.game_log.append(event)
        # Keep log reasonable size
        if len(self.game_log) > 50:
            self.game_log = self.game_log[-50:]
