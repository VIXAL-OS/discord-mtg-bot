"""
Planeswalker Ability System
============================

Parses planeswalker oracle text into activatable abilities and handles
loyalty ability activation with proper rules enforcement.

MTG Rules for Planeswalker Abilities:
- Loyalty abilities can only be activated at sorcery speed (your main phase, empty stack)
- Each planeswalker can only activate ONE loyalty ability per turn
- You can't activate an ability if it would reduce loyalty below 0
- Abilities go on the stack and can be responded to

Usage:
    from planeswalker import PlaneswalkerManager
    
    pw_manager = PlaneswalkerManager()
    
    # Parse a planeswalker's abilities
    abilities = pw_manager.parse_abilities(chandra_card)
    
    # Check if we can activate
    can_act, reason = pw_manager.can_activate(game, player, chandra_card, ability_index=1)
    
    # Activate an ability
    result = await pw_manager.activate(game, player, chandra_card, ability_index=1, targets=[...])
"""

import re
from dataclasses import dataclass, field
from typing import Optional, Dict, List, Any, Tuple, Set
from enum import Enum, auto


class AbilityType(Enum):
    """Types of planeswalker abilities."""
    LOYALTY_PLUS = auto()   # +N abilities
    LOYALTY_MINUS = auto()  # -N abilities  
    LOYALTY_ZERO = auto()   # 0 abilities
    STATIC = auto()         # Non-loyalty abilities (rare, like Grist)
    TRIGGERED = auto()      # Triggered abilities


@dataclass
class PlaneswalkerAbility:
    """A single ability on a planeswalker."""
    index: int                    # 0, 1, 2, etc. (order on card)
    loyalty_cost: int             # +1, -3, 0, etc.
    ability_type: AbilityType
    text: str                     # Full ability text
    
    # Parsed effect info
    needs_target: bool = False
    target_description: str = ""  # "target creature", "target player", etc.
    effect_keywords: List[str] = field(default_factory=list)  # damage, draw, exile, etc.
    
    # For complex abilities
    is_ultimate: bool = False     # Usually the big minus ability
    
    def __str__(self) -> str:
        if self.loyalty_cost > 0:
            cost_str = f"+{self.loyalty_cost}"
        elif self.loyalty_cost == 0:
            cost_str = "0"
        else:
            cost_str = str(self.loyalty_cost)
        return f"[{cost_str}]: {self.text}"


@dataclass
class ActivationResult:
    """Result of activating a planeswalker ability."""
    success: bool
    messages: List[str] = field(default_factory=list)
    needs_targets: bool = False
    target_prompt: str = ""
    effects_applied: List[str] = field(default_factory=list)
    triggered_abilities: List[str] = field(default_factory=list)


class PlaneswalkerManager:
    """
    Manages planeswalker ability parsing and activation.
    
    Tracks which planeswalkers have activated abilities this turn
    to enforce the once-per-turn rule.
    """
    
    def __init__(self, claude_client=None):
        self.claude = claude_client
        
        # Track activations: game_id -> set of planeswalker card IDs that activated this turn
        self._activations_this_turn: Dict[int, Dict[str, int]] = {}  # game_id -> {card_id: count}
        
        # Effect patterns for parsing
        self._effect_patterns = {
            'damage': [
                r'deals? (\d+) damage to (any target|target [^.]+)',
                r'deals? (\d+) damage to each',
                r'deals? X damage',
            ],
            'draw': [
                r'draw (\d+|a|two|three) cards?',
                r'you may draw',
            ],
            'exile': [
                r'exile (the top card|target|up to)',
                r'exiles? [^.]*from',
            ],
            'destroy': [
                r'destroy (target|all|each)',
            ],
            'create_token': [
                r'create (\d+|a|an|two|three) [^.]+ tokens?',
            ],
            'life': [
                r'(gain|lose) (\d+) life',
                r'gains? life equal to',
            ],
            'discard': [
                r'discard (\d+|a|their hand)',
            ],
            'counter': [
                r'counter target',
            ],
            'tutor': [
                r'search (your|their) library',
            ],
            'pump': [
                r'gets? [+-]\d+/[+-]\d+',
            ],
            'emblem': [
                r'you get an emblem',
            ],
            'copy': [
                r'copy (target|that)',
            ],
        }
        
        # Target patterns
        self._target_patterns = [
            (r'target (player or planeswalker)', 'player or planeswalker'),
            (r'target (creature or planeswalker)', 'creature or planeswalker'),
            (r'target (creature or player)', 'creature or player'),
            (r'any target', 'any target'),
            (r'target (\w+ creature)', r'\1'),
            (r'target (creature)', 'creature'),
            (r'target (player)', 'player'),
            (r'target (planeswalker)', 'planeswalker'),
            # July 29 batch audit: no pattern matched "target [qualifier]
            # permanent" — Teferi, Hero of Dominaria's -3 ("target nonland
            # permanent") parsed as needs_target=False, so the declared
            # target was silently dropped and Tier 3 hallucinated its own
            # (loyalty burned 7→4 for zero effect in
            # game_1531560953928355911). Qualified form must precede the
            # bare one.
            (r'target (non\w+ permanent)', r'\1'),
            (r'target (permanent)', 'permanent'),
            (r'target (artifact)', 'artifact'),
            (r'target (enchantment)', 'enchantment'),
            (r'target (land)', 'land'),
            (r'target (spell)', 'spell'),
            (r'target (opponent)', 'opponent'),
            (r'up to (\w+) target', r'up to \1 targets'),
        ]
    
    # =========================================================================
    # ABILITY PARSING
    # =========================================================================
    
    def parse_abilities(self, card) -> List[PlaneswalkerAbility]:
        """
        Parse a planeswalker's oracle text into individual abilities.
        
        Args:
            card: Card object with oracle_text
            
        Returns:
            List of PlaneswalkerAbility objects
        """
        if not card.oracle_text:
            return []
        
        abilities = []
        text = card.oracle_text
        
        # Normalize Unicode minus signs (U+2212) to ASCII minus (U+002D)
        # Scryfall uses Unicode minus in oracle text like −7
        text = text.replace('−', '-')  # U+2212 → U+002D
        
        # Split by loyalty ability pattern
        # Handles both formats:
        #   [+1]: text  (with brackets - some sources)
        #   +1: text    (without brackets - Scryfall API)
        loyalty_pattern = r'(?:^|\n)\[?([+-]?\d+)\]?:\s*'
        
        # Find all loyalty abilities
        parts = re.split(loyalty_pattern, text)
        
        # parts will be: [preamble, cost1, text1, cost2, text2, ...]
        # Skip preamble (index 0), then pairs of (cost, text)
        idx = 0
        for i in range(1, len(parts), 2):
            if i + 1 >= len(parts):
                break
                
            cost_str = parts[i]
            ability_text = parts[i + 1].strip()
            
            # Clean up ability text - remove the next ability marker if present
            # This handles cases where abilities run together
            ability_text = ability_text.split('\n')[0].strip()
            
            try:
                loyalty_cost = int(cost_str)
            except ValueError:
                continue
            
            # Determine ability type
            if loyalty_cost > 0:
                ability_type = AbilityType.LOYALTY_PLUS
            elif loyalty_cost < 0:
                ability_type = AbilityType.LOYALTY_MINUS
            else:
                ability_type = AbilityType.LOYALTY_ZERO
            
            # Parse targeting
            needs_target, target_desc = self._parse_targeting(ability_text)
            
            # Parse effect keywords
            effect_keywords = self._parse_effect_keywords(ability_text)
            
            # Check if this is the "ultimate" (usually the biggest minus)
            is_ultimate = loyalty_cost <= -6 or 'emblem' in ability_text.lower()
            
            ability = PlaneswalkerAbility(
                index=idx,
                loyalty_cost=loyalty_cost,
                ability_type=ability_type,
                text=ability_text,
                needs_target=needs_target,
                target_description=target_desc,
                effect_keywords=effect_keywords,
                is_ultimate=is_ultimate,
            )
            abilities.append(ability)
            idx += 1
        
        return abilities
    
    def _parse_targeting(self, text: str) -> Tuple[bool, str]:
        """Parse targeting requirements from ability text.

        May 20 audit fix: previously this scanned the FULL ability text and
        matched "target permanent" inside emblem-grant strings like:

            "You get an emblem with 'Whenever you draw a card, exile target
             permanent an opponent controls.'"

        That made Teferi, Hero of Dominaria's [-8] (and similar ult-emblem
        cards) falsely require a target at activation time — the activation
        just creates an emblem; the emblem's TRIGGERED ability is what targets
        (CR 603.3d: targets chosen when the trigger goes on the stack). UW
        Control couldn't ult Teferi at all, contributing to a 46-turn
        stagnation draw in game_1506623255119925278.

        Strip emblem-grant quoted text before scanning, since those targets
        belong to the emblem's future trigger, not the activation.
        """
        # Strip "with '...'" (and "with \"...\"") emblem-grant payloads so
        # targets inside the grant don't get picked up at activation time.
        text_lower = re.sub(
            r"with\s+['\"](.+?)['\"]",
            "",
            text.lower(),
            flags=re.DOTALL,
        )

        for pattern, desc in self._target_patterns:
            match = re.search(pattern, text_lower)
            if match:
                if r'\1' in desc:
                    # Capture group replacement
                    desc = match.group(1) if match.groups() else desc
                return True, desc

        return False, ""
    
    def _parse_effect_keywords(self, text: str) -> List[str]:
        """Extract effect keywords from ability text."""
        keywords = []
        text_lower = text.lower()
        
        for keyword, patterns in self._effect_patterns.items():
            for pattern in patterns:
                if re.search(pattern, text_lower):
                    keywords.append(keyword)
                    break
        
        return keywords
    
    def get_ability_display(self, card) -> str:
        """Get a formatted display of a planeswalker's abilities."""
        abilities = self.parse_abilities(card)
        
        if not abilities:
            return f"{card.name} has no parseable abilities"
        
        lines = [f"**{card.name}** (Loyalty: {card.loyalty_counters})"]
        
        for ability in abilities:
            # Format loyalty cost
            if ability.loyalty_cost > 0:
                cost = f"+{ability.loyalty_cost}"
            else:
                cost = str(ability.loyalty_cost)
            
            # Truncate long abilities — bumped from 97 → 250 chars (Vivien's
            # exile-and-recast clause was being cut mid-sentence at 97 chars).
            # Try to truncate at sentence boundary when possible.
            text = ability.text
            if len(text) > 250:
                # Prefer cut at last period within window
                period_idx = text.rfind('.', 200, 247)
                if period_idx > 0:
                    text = text[:period_idx + 1]
                else:
                    text = text[:247] + "..."

            lines.append(f"  **[{cost}]**: {text}")
        
        # Usage hint
        costs = [f"+{a.loyalty_cost}" if a.loyalty_cost > 0 else str(a.loyalty_cost) for a in abilities]
        lines.append(f"\n*Use `!activate {card.name.split(',')[0]} {costs[0]}` etc.*")
        
        return "\n".join(lines)
    
    def find_abilities_by_cost(self, card, loyalty_cost: int) -> List[PlaneswalkerAbility]:
        """
        Find all abilities with a given loyalty cost.
        
        Args:
            card: Planeswalker card
            loyalty_cost: The cost to search for (+1, -3, 0, etc.)
            
        Returns:
            List of matching abilities (usually 1, but can be multiple like Chandra ToD)
        """
        abilities = self.parse_abilities(card)
        return [a for a in abilities if a.loyalty_cost == loyalty_cost]
    
    def get_ability_by_cost(self, card, loyalty_cost: int, 
                            sub_index: int = 0) -> Tuple[Optional[PlaneswalkerAbility], str]:
        """
        Get a specific ability by loyalty cost, with optional sub-index for duplicates.
        
        Args:
            card: Planeswalker card
            loyalty_cost: The cost (+1, -3, 0, etc.)
            sub_index: For duplicate costs, which one (0 = first, 1 = second)
            
        Returns:
            (ability, error_message) - ability is None if not found
        """
        matching = self.find_abilities_by_cost(card, loyalty_cost)
        
        if not matching:
            return None, f"{card.name} has no [{'+' if loyalty_cost > 0 else ''}{loyalty_cost}] ability"
        
        if len(matching) == 1:
            return matching[0], ""
        
        # Multiple abilities with same cost
        if sub_index >= len(matching):
            return None, f"{card.name} has {len(matching)} [{'+' if loyalty_cost > 0 else ''}{loyalty_cost}] abilities, specify 1-{len(matching)}"
        
        return matching[sub_index], ""
    
    # =========================================================================
    # ACTIVATION VALIDATION
    # =========================================================================
    
    def can_activate(self, game, player, card, ability_index: int) -> Tuple[bool, str]:
        """
        Check if a player can activate a planeswalker's ability.
        
        Validates:
        - It's the player's main phase
        - Stack is empty (sorcery speed)  
        - Player controls the planeswalker
        - Planeswalker hasn't activated an ability this turn
        - Sufficient loyalty for the cost
        
        Returns:
            (can_activate, reason)
        """
        from mtg_game import Phase  # Import here to avoid circular
        
        # Check if card is a planeswalker
        if not card.is_planeswalker():
            return False, f"{card.name} is not a planeswalker"
        
        # Check if player controls it
        if card not in player.battlefield:
            return False, f"You don't control {card.name}"
        
        # Check phase - must be main phase
        if game.phase not in (Phase.MAIN1, Phase.MAIN2):
            return False, "Can only activate loyalty abilities during your main phase"
        
        # Check if it's the player's turn
        player_idx = game.players.index(player)
        if player_idx != game.active_player_index:
            return False, "Can only activate loyalty abilities on your turn"
        
        # Check stack is empty (sorcery speed)
        if game.stack:
            return False, "Stack must be empty to activate loyalty abilities"
        
        # Check if already activated this turn (CR 606.3). June 11 audit: the
        # manager-level dict is shared across all concurrent autoplay games
        # and cleared by end_turn — under `!autoplay-parallel` another game's
        # turn boundary could wipe a live game's record (Ashiok activated
        # twice in one turn, game 1514621737994551457). The card-stamped
        # counter below is immune to cross-game interference; the manager
        # dict remains as a secondary source.
        game_id = game.thread_id
        activation_count = 0
        if game_id in self._activations_this_turn:
            activation_count = self._activations_this_turn[game_id].get(card.id, 0)
        if getattr(card, '_pw_activated_turn', None) == getattr(game, 'turn_number', None):
            activation_count = max(activation_count,
                                   getattr(card, '_pw_activations_this_turn', 0))
        if activation_count > 0:
            # Oath of Teferi: "You may activate loyalty abilities of planeswalkers
            # you control twice each turn rather than only once."
            max_activations = 1
            for p_card in player.battlefield:
                if (p_card.name or '').lower() == 'oath of teferi':
                    max_activations = 2
                    break
            if activation_count >= max_activations:
                return False, f"{card.name} already activated an ability this turn"
        
        # Parse abilities and check index
        abilities = self.parse_abilities(card)
        if ability_index < 0 or ability_index >= len(abilities):
            return False, f"Invalid ability index. {card.name} has {len(abilities)} abilities (0-{len(abilities)-1})"
        
        ability = abilities[ability_index]
        
        # Check loyalty cost
        # Guard: 0 loyalty + any non-positive cost = can't activate (prevents infinite retry loops)
        if card.loyalty_counters == 0 and ability.loyalty_cost <= 0:
            return False, f"{card.name} has 0 loyalty — cannot activate any abilities"
        new_loyalty = card.loyalty_counters + ability.loyalty_cost
        if new_loyalty < 0:
            return False, f"Not enough loyalty ({card.loyalty_counters}) to pay [{ability.loyalty_cost}] cost"
        
        return True, "OK"
    
    # =========================================================================
    # ACTIVATION EXECUTION
    # =========================================================================
    
    async def activate(self, game, player, card, ability_index: int, 
                       targets: List[Any] = None) -> ActivationResult:
        """
        Activate a planeswalker's loyalty ability.
        
        Args:
            game: GameState
            player: Player activating
            card: Planeswalker card
            ability_index: Which ability (0, 1, 2...)
            targets: Pre-selected targets (or None to prompt)
            
        Returns:
            ActivationResult with success status and messages
        """
        # Validate first
        can_act, reason = self.can_activate(game, player, card, ability_index)
        if not can_act:
            return ActivationResult(success=False, messages=[f"❌ {reason}"])
        
        abilities = self.parse_abilities(card)
        ability = abilities[ability_index]
        
        # Check if we need targets
        if ability.needs_target and not targets:
            # CR 601.2c: if the ability requires a target and no legal targets
            # exist in-game, the player can't activate it. Check here BEFORE
            # paying loyalty, so no-target activations don't spend loyalty
            # counters just to fizzle on resolution.
            _optional_targeting = 'up to' in (ability.text or '').lower()
            try:
                legal = get_legal_planeswalker_targets(game, player, ability)
            except Exception:
                legal = None  # fall back to existing prompt path if detection errors
            if legal is not None and len(legal) == 0 and _optional_targeting:
                # July 29 batch audit: "up to one target" abilities are
                # legally activatable choosing ZERO targets (CR 601.2c gates
                # mandatory targets only) — Wrenn and Six's [+1] was refused
                # outright 4× in game_1531560953928355911, and the refusal
                # didn't even consume the once-per-turn activation, granting
                # free retries. Fall through: pay loyalty, record the
                # activation, resolve with none chosen.
                print(f"[PW-TARGET] {card.name} [{ability.loyalty_cost:+d}]: "
                      f"'up to' ability with no legal targets — activating "
                      f"with none chosen (CR 601.2c)")
            elif legal is not None and len(legal) == 0:
                return ActivationResult(
                    success=False,
                    messages=[f"❌ No legal targets for {card.name}'s [{ability.loyalty_cost:+d}] ability — cannot activate"],
                )
            else:
                return ActivationResult(
                    success=False,
                    needs_targets=True,
                    target_prompt=f"Choose {ability.target_description} for {card.name}'s ability",
                    messages=[f"🎯 {card.name}'s [{ability.loyalty_cost:+d}] ability needs a target: {ability.target_description}"]
                )

        # Pay the loyalty cost
        old_loyalty = card.loyalty_counters
        card.loyalty_counters += ability.loyalty_cost

        # Record activation for this turn (counter-based for Oath of Teferi)
        game_id = game.thread_id
        if game_id not in self._activations_this_turn:
            self._activations_this_turn[game_id] = {}
        self._activations_this_turn[game_id][card.id] = self._activations_this_turn[game_id].get(card.id, 0) + 1
        # June 11 audit: card-stamped mirror of the count (see can_activate) —
        # survives cross-game clears of the shared manager dict.
        _turn_now = getattr(game, 'turn_number', None)
        if getattr(card, '_pw_activated_turn', None) == _turn_now:
            card._pw_activations_this_turn = getattr(card, '_pw_activations_this_turn', 0) + 1
        else:
            card._pw_activated_turn = _turn_now
            card._pw_activations_this_turn = 1

        # Inline the ability text so the user sees what [+1] actually does.
        # Outer callers used to prepend their own header with oracle text; now
        # they can trust result.messages[0] to be self-describing.
        # May 18 audit: dedupe oracle text across activations via the same
        # `game._oracle_shown_keys` set the trigger-line dedup uses — Aminatou
        # +1 was printing its full text 5 times in one game's log otherwise.
        ability_text = (getattr(ability, 'text', '') or '').strip()
        # cost_str is used downstream by the no-effect refund path (lines ~527, 531),
        # so it must be defined regardless of which header_line branch wins.
        # May 20 audit: the May 19 oracle-dedup refactor moved the happy path through
        # format_activate_line() which never sets cost_str, and the except-only
        # assignment crashed 6.3% of games when activations refunded.
        cost_str = f"+{ability.loyalty_cost}" if ability.loyalty_cost > 0 else str(ability.loyalty_cost)

        # Execute the ability effect FIRST — the display header is built only
        # on the success path below. July 20 audit: building the header before
        # the refund check recorded the oracle-shown dedup key
        # (format_activate_line side effect) for a header the refund branch
        # then DISCARDED — a refunded first attempt permanently poisoned the
        # "first use shows full text" slot, so Aminatou's -1 text was never
        # shown all game (game_1526059766965604602).
        effects_applied = []
        try:
            effect_messages = await self._execute_ability(game, player, card, ability, targets)
        except Exception as e:
            effect_messages = [f"⚠️ Effect execution error: {e}"]

        # May 7 audit (Bug 1): if the ability resolved with no real game effect
        # (template returned no_action because there were no legal targets, or
        # the template emitted nothing at all), refund the loyalty cost so we
        # don't silently bleed loyalty on illegal activations. Heuristic: a
        # "real" effect message is one that's not a 📍 no-action prefix and
        # not the 🎯 "needs a target" hint. July 20: gated on the execution
        # tracker too — a template can execute real state changes whose
        # actions return no display text (Aminatou -1 flickering Rhystic
        # Study), and those were being refunded as "no effect" — a FREE
        # activation (effect applied + loyalty back) that also lied to the
        # player about a legal target existing.
        if (self._activation_had_no_effect(effect_messages)
                and not getattr(self, '_last_ability_executed_state_change', False)):
            card.loyalty_counters = old_loyalty
            # Roll back the per-turn activation counter too — refunded
            # activations don't count toward Oath of Teferi's "twice per turn".
            try:
                cur = self._activations_this_turn[game_id].get(card.id, 0)
                if cur > 0:
                    self._activations_this_turn[game_id][card.id] = cur - 1
            except KeyError:
                pass
            # July 20 (found by the poisoning regression pin): the per-CARD
            # once-per-turn attributes were NOT rolled back, so a refunded
            # activation still blocked the walker for the rest of the turn
            # ("already activated an ability this turn") even though the
            # manager-side counter said it didn't count.
            if getattr(card, '_pw_activations_this_turn', 0) > 0:
                card._pw_activations_this_turn -= 1
            # July 20: neutral wording — the old "has no legal target" was a
            # guess that contradicted the 📍 reason line that follows when
            # the real cause was e.g. an empty hand (Daretti +2).
            messages = [
                f"📍 {card.name}'s [{cost_str}] ability had no effect — activation refunded (loyalty stays at {old_loyalty})"
            ]
            if effect_messages:
                messages.extend(effect_messages)
            print(f"[PW-REFUND] {card.name} [{cost_str}]: loyalty refunded ({old_loyalty} → {card.loyalty_counters}); no effect")
            return ActivationResult(
                success=False,
                messages=messages,
                effects_applied=[],
            )

        # Success path — NOW build the header (and record the oracle-shown
        # dedup key, since this header is actually displayed).
        try:
            from mtg.helpers import format_activate_line
            header_line = format_activate_line(card.name, ability.loyalty_cost,
                                               ability_text, game=game)
        except Exception:
            header_line = f"⚡ **{card.name}** activates [{cost_str}] ability"
            if ability_text:
                header_line += f": _{ability_text}_"
        messages = [
            header_line,
            f"   Loyalty: {old_loyalty} → {card.loyalty_counters}"
        ]
        messages.extend(effect_messages)
        effects_applied = effect_messages

        # [PW-ACTIVATE] belongs HERE, at the shared choke point, not in one
        # caller. There are three activation implementations — human (cog),
        # Claude (engine) and Rick (autoplay) — all converging on this method,
        # and only the Claude one printed the tag. An auditor grepping
        # [PW-ACTIVATE] saw Rick's five successful Daretti activations in
        # game_1529160643909914765 as ZERO, which manufactured a convincing
        # "ability silently dropped" trail during the July 27 audit itself.
        # The player name is included because that attribution is the whole
        # point of the tag.
        print(f"[PW-ACTIVATE] {getattr(player, 'name', '?')} — {card.name} "
              f"[{cost_str}]: "
              f"{'; '.join(effect_messages[:2]) if effect_messages else 'no effect messages'}")

        return ActivationResult(
            success=True,
            messages=messages,
            effects_applied=effects_applied,
        )

    @staticmethod
    def _activation_had_no_effect(effect_messages) -> bool:
        """Return True when the ability produced no real game-state change.

        Used by activate() to refund loyalty for illegal/empty activations.
        Heuristics:
          - Empty message list → nothing happened
          - All messages are 📍 no-action prefix messages
          - All messages are "🔮 Card [cost]: <oracle summary>" lines (Tier 3
            fallback when the templates+inline+API all missed). These look
            like state changes but are diagnostic-only — the loyalty was
            spent on text that says what should happen, not on the effect.
          - All messages are the 💥 ultimate-activated / ⚠️ manual-resolution
            placeholders, also diagnostic-only.
        """
        if not effect_messages:
            return True
        diagnostic_only_starts = ('📍', '💥', '⚠️')
        for m in effect_messages:
            if not isinstance(m, str):
                # Any non-string thing in here is unexpected; treat as effect.
                return False
            s = m.strip()
            if not s:
                continue
            if s.startswith(diagnostic_only_starts):
                continue
            if s.startswith('🔮') and '[' in s and ']:' in s:
                # Tier 3 "summary only" fallback line — no actual effect
                continue
            # "Use `!judge` for complex effect ruling" — diagnostic hint, no effect
            if s.startswith('Use `!judge`') or 'use `!judge`' in s.lower():
                continue
            # Anything else is a real effect message
            return False
        return True
    
    async def _execute_ability(self, game, player, card, ability: PlaneswalkerAbility,
                               targets: List[Any]) -> List[str]:
        """
        Execute a planeswalker ability's effect.

        Checks effect_templates library FIRST (Tier 1.5), then falls back to
        inline pattern matching, then to Claude for complex abilities.
        """
        messages = []
        text = ability.text.lower()
        # July 20 audit: activate()'s refund heuristic treats an EMPTY message
        # list as "no effect", but a template can execute real state changes
        # whose actions return no display text (Aminatou -1 flickering
        # Rhystic Study — no ETB to narrate). Track actual execution so a
        # silent-but-real effect is never refunded as "no legal target".
        self._last_ability_executed_state_change = False

        # === TIER 1.5: Check effect template library for PW abilities ===
        # This catches Chandra ToD +1, Garruk Beast tokens, Daretti loot, Elspeth soldiers, etc.
        try:
            from rules.effect_templates import get_effect_library
            lib = get_effect_library()
            opponent = None
            for p in game.players:
                if p != player:
                    opponent = p
                    break
            opp_name = opponent.name if opponent else "Opponent"

            # Build game context for template resolution — use full context builder if available
            try:
                from rules.effect_templates import build_game_context
                pw_ctx = build_game_context(game, player, opponent, card=card)
            except (ImportError, Exception):
                pw_ctx = {
                    'controller_hand_size': len(player.hand),
                    'controller_life': player.life,
                }
            pw_ctx['controller_hand_size'] = len(player.hand)
            pw_ctx['controller_life'] = player.life
            pw_ctx['_pw_targets'] = list(targets or [])
            if targets:
                first_target = targets[0]
                pw_ctx['explicit_target_name'] = getattr(first_target, 'name', first_target)
                for target_player in game.players:
                    if first_target in target_player.battlefield:
                        pw_ctx['explicit_target_owner'] = target_player.name
                        break
            # Greatest power among creatures (for Garruk -3, etc.)
            greatest_power = 0
            for c in player.battlefield:
                if c.is_creature() if hasattr(c, 'is_creature') else False:
                    try:
                        p_val = c.get_effective_power(game)
                    except (ValueError, TypeError):
                        p_val = 0
                    greatest_power = max(greatest_power, p_val)
            pw_ctx['greatest_power'] = greatest_power

            actions, explanation = lib.resolve_pw_ability(
                pw_name=card.name,
                ability_text=ability.text,
                controller=player.name,
                opponent=opp_name,
                game_context=pw_ctx,
            )
            if actions is not None and len(actions) > 0:
                # Check it's not a no_action passthrough from generic patterns
                if not (len(actions) == 1 and actions[0].get('action') == 'no_action'
                        and 'use !fix' in actions[0].get('reason', '')):
                    print(f"[PW-TEMPLATE] {card.name} ability resolved via template: {explanation}")
                    # Execute the actions through the game engine
                    # Try game._rules_engine, then self._rules_engine, then game engine ref
                    rules_engine = (getattr(game, '_rules_engine', None)
                                    or getattr(self, '_rules_engine', None))
                    # May 7 audit (Bug 1): if the template's only output is
                    # no_action, surface the reason via 📍 and return early.
                    # activate() detects 📍-only messages and refunds loyalty
                    # rather than charging the cost for an illegal activation.
                    only_no_action = all(a.get('action') == 'no_action' for a in actions)
                    if only_no_action:
                        for act in actions:
                            reason = act.get('reason', '') or explanation
                            if reason:
                                messages.append(f"📍 {reason}")
                        return messages
                    if rules_engine and hasattr(rules_engine, '_execute_action_on_state'):
                        for act in actions:
                            if act.get('action') == 'no_action':
                                reason = act.get('reason', '')
                                if reason:
                                    messages.append(f"📍 {reason}")
                                continue
                            msg = rules_engine._execute_action_on_state(game, act)
                            # Executed a real action — even if it produced no
                            # display text, the game state changed (July 20:
                            # message-less flicker was refunded as no-effect).
                            self._last_ability_executed_state_change = True
                            if msg:
                                messages.append(msg)
                    else:
                        # Fallback: try to execute actions inline for common types
                        for act in actions:
                            act_type = act.get('action', '')
                            if act_type == 'draw_cards':
                                amt = act.get('amount', 1)
                                target_name = act.get('player', player.name)
                                for p in game.players:
                                    if p.name == target_name and p.library:
                                        drawn = p.library[:amt]
                                        p.hand.extend(drawn)
                                        p.library = p.library[amt:]
                                        messages.append(f"🃏 {p.name} draws {amt} card(s)")
                            elif act_type == 'discard':
                                target_name = act.get('player', player.name)
                                for p in game.players:
                                    if p.name == target_name and p.hand:
                                        # Discard worst card (highest CMC non-land)
                                        worst = max(p.hand, key=lambda c: int(getattr(c, 'cmc', 0) or 0))
                                        p.hand.remove(worst)
                                        p.graveyard.append(worst)
                                        messages.append(f"📤 {p.name} discards {worst.name}")
                            elif act_type == 'untap_lands':
                                count = act.get('count', 2)
                                target_name = act.get('player', player.name)
                                for p in game.players:
                                    if p.name == target_name:
                                        untapped = 0
                                        for c in p.battlefield:
                                            if c.is_land() and c.tapped and untapped < count:
                                                c.tapped = False
                                                untapped += 1
                                        if untapped:
                                            messages.append(f"🔓 {p.name} untaps {untapped} land(s)")
                            elif act_type == 'no_action':
                                reason = act.get('reason', '')
                                if reason:
                                    messages.append(f"📍 {reason}")
                            else:
                                messages.append(f"📜 {explanation}")
                    if messages:
                        return messages
        except ImportError:
            pass  # effect_templates not available, continue with inline handlers
        except Exception as e:
            print(f"[PW-TEMPLATE] Error checking templates for {card.name}: {e}")

        # Handle common effect patterns (inline fallback)

        # === TEFERI, TIME RAVELER +1 — grant sorcery flash until next turn ===
        # "Until your next turn, you may cast sorcery spells as though they had flash."
        # This is a turn-based continuous effect, not a simple stat change — the static
        # pattern suppressor at line ~906 would silence it. Emit a message so players
        # know the +1 fired, and record the effect in game.turn_effects for cast validation.
        if 'as though they had flash' in text and 'sorcery' in text and ability.loyalty_cost > 0:
            messages.append(f"⚡ {player.name}'s sorcery spells have flash until their next turn.")
            # Record as a turn effect so cast_spell_async can check it
            if hasattr(game, 'turn_effects') and player is not None:
                player_idx = game.players.index(player) if player in game.players else 0
                # Remove any existing stale Teferi flash effect for this player
                game.turn_effects = [
                    te for te in game.turn_effects
                    if not (te.get('type') == 'sorcery_flash' and te.get('controller') == player_idx)
                ]
                game.turn_effects.append({
                    'type': 'sorcery_flash',
                    'controller': player_idx,
                    'source': card.name,
                    'expires_turn': getattr(game, 'turn_number', 0) + 2,  # expires on player's next turn
                })
            return messages

        # === CHANDRA, PYROMASTER +1 (special two-target handler) ===
        # "deals 1 damage to target player and 1 damage to up to one target creature that player controls"
        card_name_lower = card.name.lower()
        if 'chandra' in card_name_lower and 'pyromaster' in card_name_lower and ability.loyalty_cost > 0:
            if targets:
                # targets[0] = player, targets[1] = creature (optional)
                player_target = targets[0]
                if hasattr(player_target, 'life'):
                    player_target.life -= 1
                    messages.append(f"🔥 {card.name} deals 1 damage to {player_target.name} (Life: {player_target.life})")

                if len(targets) > 1 and targets[1] is not None:
                    creature_target = targets[1]
                    if hasattr(creature_target, 'damage_marked'):
                        creature_target.damage_marked += 1
                        messages.append(f"🔥 {card.name} deals 1 damage to {creature_target.name}")
                        # Mark creature as can't block this turn
                        if not hasattr(creature_target, 'cant_block_this_turn'):
                            creature_target.cant_block_this_turn = False
                        creature_target.cant_block_this_turn = True
                        messages.append(f"🚫 {creature_target.name} can't block this turn")
                        # Check for lethal
                        if hasattr(creature_target, 'toughness') and creature_target.toughness:
                            try:
                                toughness = int(creature_target.toughness) + creature_target.toughness_modifier
                                if creature_target.damage_marked >= toughness:
                                    messages.append(f"💀 {creature_target.name} has lethal damage marked")
                            except ValueError:
                                pass
                else:
                    messages.append(f"   (No creature targeted)")
            return messages

        # === DAMAGE ===
        damage_match = re.search(r'deals? (\d+) damage to (target [^,.]+|any target)', text)
        if damage_match and targets:
            amount = int(damage_match.group(1))
            target = targets[0]

            # Planeswalker check must come BEFORE the creature branch: every
            # Card carries both damage_marked and loyalty_counters fields, so
            # hasattr() dispatch routed PW targets into the creature branch and
            # never deducted loyalty (CR 306.8). July 20 audit: Wrenn and Six's
            # [-1] hit Jace, the Mind Sculptor and Jace kept all 5 loyalty.
            _is_pw_target = ('planeswalker' in
                             (getattr(target, 'type_line', '') or '').lower())
            if hasattr(target, 'life') and not hasattr(target, 'type_line'):
                # It's a player
                target.life -= amount
                messages.append(f"🔥 {card.name} deals {amount} damage to {target.name} (Life: {target.life})")
            elif _is_pw_target:
                # It's a planeswalker — damage removes that many loyalty counters
                target.loyalty_counters = max(0, target.loyalty_counters - amount)
                messages.append(f"🔥 {card.name} deals {amount} damage to {target.name} (Loyalty: {target.loyalty_counters})")
                if target.loyalty_counters <= 0:
                    messages.append(f"💀 {target.name} will be destroyed (0 loyalty)")
            elif hasattr(target, 'damage_marked'):
                # It's a creature
                target.damage_marked += amount
                messages.append(f"🔥 {card.name} deals {amount} damage to {target.name}")

                # Check for lethal
                if hasattr(target, 'toughness') and target.toughness:
                    try:
                        toughness = int(target.toughness) + target.toughness_modifier
                        if target.damage_marked >= toughness:
                            messages.append(f"💀 {target.name} has lethal damage marked")
                    except ValueError:
                        pass
        
        # === DISCARD HAND + EXILE TOP X (Chandra, Heart of Fire +1) ===
        # "Discard your hand, then exile the top three cards of your library. Until end of turn, you may play cards exiled this way."
        if 'discard your hand' in text and 'exile the top' in text:
            # Discard hand first
            discarded_count = len(player.hand)
            player.graveyard.extend(player.hand)
            player.hand.clear()
            messages.append(f"🗑️ Discarded {discarded_count} card(s)")
            
            # Find how many cards to exile
            exile_match = re.search(r'exile the top (\d+|three|two|one) cards?', text)
            if exile_match:
                num_str = exile_match.group(1)
                exile_count = {'one': 1, 'two': 2, 'three': 3, 'four': 4, 'five': 5}.get(num_str, int(num_str) if num_str.isdigit() else 3)
            else:
                exile_count = 3  # Default
            
            # Exile cards and mark as playable
            exiled_names = []
            for _ in range(exile_count):
                if player.library:
                    exiled_card = player.library.pop(0)
                    player.exile.append(exiled_card)
                    if not hasattr(player, 'playable_from_exile'):
                        player.playable_from_exile = []
                    player.playable_from_exile.append(exiled_card.id)
                    exiled_names.append(exiled_card.name)
            
            if exiled_names:
                messages.append(f"📤 Exiled: {', '.join(exiled_names)}")
                messages.append("✨ You may play these cards this turn!")
        
        # === EXILE TOP CARD (Chandra 0 ability style) ===
        if 'exile the top card' in text and 'you may play it' in text:
            if player.library:
                exiled_card = player.library.pop(0)
                player.exile.append(exiled_card)
                # Mark as playable this turn using player's playable_from_exile list
                if not hasattr(player, 'playable_from_exile'):
                    player.playable_from_exile = []
                player.playable_from_exile.append(exiled_card.id)
                messages.append(f"📤 Exiled **{exiled_card.name}** - you may play it this turn")
            else:
                messages.append("📚 Library is empty!")
        
        # === MILL (Jace, Wielder of Mysteries +1, Ashiok, etc.) ===
        # June 11 audit: "Target player mills two cards. Draw a card." matched
        # only the unconditional-draw branch below; the mill sentence was
        # silently dropped across every activation (4x in game
        # 1514621789555265558 — 8 cards that never left the library).
        mill_match = re.search(r'(target player|target opponent|each player|each opponent|you)?\s*mills? (\d+|a|one|two|three|four|five) cards?', text)
        if mill_match:
            _who = (mill_match.group(1) or 'target player').strip()
            _num_str = mill_match.group(2)
            _mill_n = {'a': 1, 'one': 1, 'two': 2, 'three': 3, 'four': 4,
                       'five': 5}.get(_num_str, int(_num_str) if _num_str.isdigit() else 1)
            if _who == 'you':
                _mill_players = [player]
            elif _who == 'each player':
                _mill_players = list(game.players)
            elif _who == 'each opponent':
                _mill_players = [p for p in game.players if p is not player]
            else:
                # target player/opponent: prefer the resolved activation target,
                # else default to the opponent (the overwhelmingly common pick)
                _tp = next((t for t in (targets or [])
                            if hasattr(t, 'life') and hasattr(t, 'hand')), None)
                if _tp is None:
                    _tp = next((p for p in game.players if p is not player), player)
                _mill_players = [_tp]
            for _mp in _mill_players:
                _milled = []
                for _ in range(_mill_n):
                    if _mp.library:
                        _mc = _mp.library.pop(0)
                        _mp.graveyard.append(_mc)
                        _milled.append(_mc.name)
                if _milled:
                    messages.append(f"🪦 {_mp.name} mills {len(_milled)}: {' · '.join(_milled)}")

        # === MAY DISCARD → CONDITIONAL DRAW (Sarkhan, Fireblood +1, etc.) ===
        # Must check BEFORE unconditional draw to avoid false matches
        if 'you may discard a card' in text and 'if you do' in text and 'draw' in text:
            # This is a conditional: discard first, then draw only if you did
            if player.hand:
                # May 23 audit (MAJOR #15): treat autoplay's pretend-human (Rick)
                # the same as Claude for this prompt — the autoplay resolver
                # would auto-handle it anyway, and printing the full hand
                # publicly leaks Rick's hand to spectators.
                auto_pick = getattr(player, 'is_claude', False) or (game and getattr(game, 'is_autoplay', False))
                if auto_pick:
                    # Auto-pick least valuable card to discard
                    discard_candidates = [c for c in player.hand if not c.is_land()]
                    if not discard_candidates:
                        discard_candidates = list(player.hand)
                    if discard_candidates:
                        discard_card = min(discard_candidates, key=lambda c: c.cmc if c.cmc else 0)
                        player.hand.remove(discard_card)
                        player.graveyard.append(discard_card)
                        messages.append(f"🗑️ Discarded **{discard_card.name}**")
                        # Now draw since we discarded
                        if player.library:
                            card_drawn = player.library.pop(0)
                            player.hand.append(card_drawn)
                            messages.append(f"🎴 Drew 1 card")
                else:
                    # Human player: prompt them to choose
                    hand_list = ", ".join(c.name for c in player.hand)
                    messages.append(f"🤔 You may discard a card to draw a card.")
                    messages.append(f"Use `!discard <card name>` to discard and draw, or `!pass` to skip.")
                    messages.append(f"Cards in hand: {hand_list}")
                    # Set pending action so the !discard command knows to draw
                    if game:
                        player_idx = game.players.index(player) if player in game.players else 0
                        game.pending_action = {
                            'type': 'may_discard_draw',
                            'player_idx': player_idx,
                        }
            else:
                messages.append(f"✋ No cards in hand to discard")
        # === DRAW CARDS EQUAL TO GREATEST POWER (Garruk -3, Rishkar's Expertise) ===
        elif 'draw cards equal to' in text and 'greatest power' in text:
            greatest_power = 0
            for c in player.battlefield:
                if c.is_creature():
                    try:
                        p = int(c.power) if c.power and c.power != '*' else 0
                        p += getattr(c, 'power_modifier', 0)
                        p += c.counters.get('+1/+1', 0) if hasattr(c, 'counters') else 0
                    except (ValueError, TypeError):
                        p = 0
                    greatest_power = max(greatest_power, p)
            if greatest_power > 0:
                drawn = []
                for _ in range(greatest_power):
                    if player.library:
                        card_drawn = player.library.pop(0)
                        player.hand.append(card_drawn)
                        drawn.append(card_drawn.name)
                messages.append(f"🎴 Drew {len(drawn)} card(s) (greatest power = {greatest_power})")
            else:
                messages.append(f"🎴 No creatures on battlefield — draw 0 cards")
        # === ADD MANA (Sarkhan, Fireblood +1 style) ===
        elif 'add' in text and ('mana' in text or '{' in ability.text):
            # Parse mana from ability text: "Add two mana in any combination of R and/or G"
            # or "Add {R}{R}" style
            mana_match = re.search(r'add (\w+) mana', text)
            if mana_match:
                count_word = mana_match.group(1)
                count = {'one': 1, 'two': 2, 'three': 3}.get(count_word, 2)
                # Use the first color in the PW's mana cost as a default
                mana_cost = card.mana_cost or ''
                color = 'R'  # Default
                for c in 'RGBUW':
                    if c in mana_cost.upper():
                        color = c
                        break
                player.mana_pool[color] = player.mana_pool.get(color, 0) + count
                messages.append(f"💎 Added {count} {color} mana to pool")
            else:
                # Try to parse {X}{Y} style
                explicit_mana = re.findall(r'\{([WUBRGC])\}', ability.text)
                if explicit_mana:
                    for m in explicit_mana:
                        player.mana_pool[m] = player.mana_pool.get(m, 0) + 1
                    messages.append(f"💎 Added {', '.join(explicit_mana)} mana to pool")
        # === DRAW CARDS (unconditional) ===
        elif re.search(r'draw (\d+|a|two|three) cards?', text):
            draw_match = re.search(r'draw (\d+|a|two|three) cards?', text)
            amount_str = draw_match.group(1)
            amount = {
                'a': 1, 'one': 1, 'two': 2, 'three': 3, 'four': 4, 'five': 5
            }.get(amount_str, int(amount_str) if amount_str.isdigit() else 1)

            drawn = []
            for _ in range(amount):
                if player.library:
                    card_drawn = player.library.pop(0)
                    player.hand.append(card_drawn)
                    drawn.append(card_drawn.name)

            if drawn:
                messages.append(f"🎴 Drew {len(drawn)} card(s)")
            else:
                messages.append(f"📚 Library empty — cannot draw")
        
        # === CREATE TOKENS ===
        token_match = re.search(r'create (\d+|a|an|two|three) ([^.]+) tokens?', text)
        if token_match:
            amount_str = token_match.group(1)
            token_desc = token_match.group(2)
            
            amount = {
                'a': 1, 'an': 1, 'one': 1, 'two': 2, 'three': 3
            }.get(amount_str, int(amount_str) if amount_str.isdigit() else 1)

            # Parse token stats
            stats_match = re.search(r'(\d+)/(\d+)', token_desc)
            power = stats_match.group(1) if stats_match else "1"
            toughness = stats_match.group(2) if stats_match else "1"

            # [REPLACEMENT] Process token doubling (Doubling Season, Anointed Procession, Parallel Lives)
            original_amount = amount
            try:
                from rules.replacement import GameEvent, EventType
                if hasattr(game, '_replacement_engine') and game._replacement_engine and game._replacement_engine.effects:
                    event = GameEvent(
                        event_type=EventType.TOKEN_CREATED,
                        affected_player=player.name,
                        amount=amount,
                        source_name=card.name,
                    )
                    final = game._replacement_engine.process_event_sync(event)
                    if final.amount != amount:
                        print(f"  [REPLACEMENT-APPLY] PW token count modified: {amount} → {final.amount} ({', '.join(final.replacement_chain)})")
                    amount = final.amount
            except (ImportError, Exception) as e:
                print(f"  [PW-TOKEN] Replacement engine not available for token doubling: {e}")

            # Create token cards
            from mtg_game import Card
            # June 10 audit (V31g): derive a real NAME + subtype from the
            # parsed descriptor. The old name was the entire phrase ("1/1
            # White Kor Soldier Creature Token"), which then appeared
            # verbatim in attack/damage lines; subtypes were dropped too.
            _desc_words = re.sub(r'^\d+/\d+\s+', '', (token_desc or '').strip()).split()
            _skip_words = {'white', 'blue', 'black', 'red', 'green', 'colorless',
                           'and', 'creature', 'creatures', 'token', 'tokens',
                           'tapped', 'attacking', 'a', 'an'}
            _name_words = [w for w in _desc_words if w.lower() not in _skip_words]
            _tok_name = ' '.join(w.capitalize() for w in _name_words) or 'Token'
            for i in range(amount):
                token = Card(
                    name=_tok_name,
                    type_line=f"Token Creature — {_tok_name}",
                    power=power,
                    toughness=toughness,
                    owner_index=game.players.index(player),
                    summoning_sick=True,
                    entered_this_turn=True,
                )
                token.is_token = True  # CR 110.5g: tokens cease to exist in non-battlefield zones
                player.battlefield.append(token)

            messages.append(f"🪙 {player.name} creates {amount}x **{_tok_name}** ({power}/{toughness})")
        
        # === GAIN LIFE ===
        life_match = re.search(r'gains? (\d+) life', text)
        if life_match:
            amount = int(life_match.group(1))
            player.life += amount
            messages.append(f"💚 {player.name} gains {amount} life (Life: {player.life})")
        
        # === CAN'T BLOCK (Chandra +1 rider) ===
        if "can't block this turn" in text and targets:
            for target in targets:
                if hasattr(target, 'keywords'):
                    # Add a marker (would need proper continuous effect tracking)
                    messages.append(f"🚫 {target.name} can't block this turn")
        
        # === JAYA'S -2 STYLE: Attack trigger for damage ===
        # "Choose target creature. Whenever you attack this turn, Jaya deals damage equal to the number of attacking creatures to that creature."
        if "whenever you attack this turn" in text and "number of attacking creatures" in text and targets:
            target = targets[0]
            # Store this effect in game.turn_effects
            if hasattr(game, 'turn_effects'):
                player_idx = game.players.index(player) if player in game.players else 0
                game.turn_effects.append({
                    "type": "on_attack_damage",
                    "source": card.name,
                    "target_id": target.id if hasattr(target, 'id') else None,
                    "target_name": target.name if hasattr(target, 'name') else str(target),
                    "calc": "num_attackers",
                    "controller": player_idx
                })
                messages.append(f"🎯 **{target.name}** will take damage equal to the number of attacking creatures when you attack!")
            else:
                messages.append(f"🎯 Effect: {card.name} will deal damage to {target.name} when you attack")
        
        # === COMPLEX/ULTIMATE ABILITIES ===
        if ability.is_ultimate and not messages:
            # For ultimates we haven't handled, try Tier 3 before announcing failure
            resolved = await self._try_tier3_pw_ability(game, player, card, ability)
            if resolved:
                messages.extend(resolved)
            else:
                messages.append(f"💥 **{card.name}** ultimate activated!")
                messages.append("⚠️ Effect needs manual resolution — use `!judge` to resolve")

        # If we didn't handle anything, try Tier 3 then emit a clean fallback
        if not messages:
            ability_lower = ability.text.lower() if ability.text else ""
            # Suppress fallback for static/permission abilities that don't change game state
            # (e.g., "you may cast sorcery spells as though they had flash", "can't be countered")
            static_patterns = [
                "as though they had flash",
                "can't be countered",
                "can't be blocked",
                "protection from",
                "your opponents can't cast spells",
            ]
            is_static_only = any(pat in ability_lower for pat in static_patterns)
            if not is_static_only:
                # Try Tier 3 (Claude API via rules engine) before falling back to text dump
                resolved = await self._try_tier3_pw_ability(game, player, card, ability)
                if resolved:
                    messages.extend(resolved)
                else:
                    # Clean fallback — surface a one-line summary of the
                    # ability so players can see what fired even when we
                    # can't resolve the full effect (CR 117, scry, look-at-top
                    # effects, ultimates with complex outcomes). Strip
                    # parenthetical reminder text so the line stays short.
                    #
                    # May 20 audit fix: route through format_activate_line so
                    # this fallback emit shares the _oracle_shown_keys dedup
                    # set with the rest of the PW activation pipeline. Before:
                    # Calix, Destiny's Hand emitted "🔮 **Calix** [+1]: Look
                    # at the top four cards...Put the..." with `...`-truncation
                    # 10+ times per game (game_1506623303794561024). The
                    # _pw_refund_shown dedup only catches "refund"/"❌"
                    # messages, missing this 🔮 path entirely.
                    from mtg.helpers import format_activate_line
                    summary = (ability.text or "").split('\n')[0]
                    summary = re.sub(r'\([^)]*\)', '', summary).strip()
                    line = format_activate_line(
                        card.name, ability.loyalty_cost, summary,
                        game=game, max_chars=140,
                    )
                    if not summary:
                        # format_activate_line returns "⚡ ... activates [N] ability"
                        # for empty oracle, which is acceptable. Override emoji
                        # to keep the historical 🔮 marker for unresolved-ability
                        # path so existing greps still find it.
                        cost_str = (f"+{ability.loyalty_cost}" if ability.loyalty_cost > 0
                                    else str(ability.loyalty_cost))
                        line = f"🔮 **{card.name}** [{cost_str}]: effect activated"
                    else:
                        # Replace the ⚡ emoji with 🔮 to preserve the historical
                        # unresolved-ability marker that bot.py/!judge surfaces
                        # rely on.
                        line = line.replace('⚡', '🔮', 1)
                    messages.append(line)
                    if self.claude and not getattr(game, 'is_autoplay', False):
                        messages.append("Use `!judge` for complex effect ruling")

        return messages

    async def _try_tier3_pw_ability(self, game, player, card, ability) -> list:
        """Attempt to resolve an unhandled planeswalker ability via Tier 3 (Claude API).

        Returns a list of messages if successful, or empty list if unavailable/failed.
        Called as a last resort from _execute_ability when templates and inline
        handlers both miss (suppresses the raw oracle text dump).
        """
        rules_engine = (getattr(game, '_rules_engine', None)
                        or getattr(self, '_rules_engine', None))
        if not rules_engine or not getattr(rules_engine, 'client', None):
            return []

        opponent = next((p for p in game.players if p != player), None)
        opp_name = opponent.name if opponent else "opponent"
        cost_str = f"+{ability.loyalty_cost}" if ability.loyalty_cost > 0 else str(ability.loyalty_cost)
        effect_desc = (
            f"{card.name} planeswalker [{cost_str}] ability: {ability.text}"
        )

        # Deduplicate Tier 3 calls — once per unique ability per game session.
        # On a dedupe hit we DON'T have a cached result to apply (Tier 3 doesn't
        # cache action lists, just suppresses the API re-call). So emit a clean
        # status line that doesn't pretend a state change happened. If the
        # ability has truly no resolvable effect at lower tiers, this signals
        # the autoplay log to surface it for template-coverage triage.
        hint_key = f"pw_tier3:{card.name}:{ability.text[:80]}"
        if getattr(game, 'is_autoplay', False):
            emitted = getattr(game, '_judge_hints_emitted', set())
            if hint_key in emitted:
                print(f"[PW-TIER3] Skipping duplicate resolve for {card.name} [{cost_str}] (needs template)")
                # Empty list signals to caller that no Tier 3 message was
                # produced — caller falls through to the one-line summary.
                return []
            if hasattr(game, '_judge_hints_emitted'):
                game._judge_hints_emitted.add(hint_key)

        try:
            print(f"[PW-TIER3] Resolving {card.name} [{cost_str}] via Claude API")
            # resolve_effect returns Tuple[List[str], List[Dict]] — msgs already executed
            msgs, _actions = await rules_engine.resolve_effect(
                game, effect_desc,
                source_card=card.name,
                controller=player.name
            )
            if msgs:
                print(f"[PW-TIER3] {card.name} resolved: {msgs[0][:80] if msgs else ''}")
                return msgs
        except Exception as e:
            print(f"[PW-TIER3] Error resolving {card.name} [{cost_str}]: {e}")
        return []
    
    # =========================================================================
    # TURN MANAGEMENT
    # =========================================================================
    
    def on_turn_start(self, game):
        """Reset activation tracking at start of each turn."""
        game_id = game.thread_id
        if game_id in self._activations_this_turn:
            self._activations_this_turn[game_id].clear()
    
    def on_game_end(self, game):
        """Clean up tracking when game ends."""
        game_id = game.thread_id
        if game_id in self._activations_this_turn:
            del self._activations_this_turn[game_id]
    
    def has_activated_this_turn(self, game, card) -> bool:
        """Check if a planeswalker has activated an ability this turn."""
        game_id = game.thread_id
        if game_id not in self._activations_this_turn:
            return False
        return card.id in self._activations_this_turn[game_id]


# =============================================================================
# TARGETING HELPERS
# =============================================================================

def get_legal_planeswalker_targets(game, player, ability: PlaneswalkerAbility) -> List[Tuple[Any, str]]:
    """
    Get legal targets for a planeswalker ability.

    Returns list of (target, description) tuples.
    """
    targets = []
    desc = ability.target_description.lower()
    oracle = (ability.text or "").lower()

    # Get all potential targets
    all_creatures = []
    all_permanents = []
    all_players = list(game.players)
    all_planeswalkers = []

    for p in game.players:
        for card in p.battlefield:
            all_permanents.append((card, p))
            if card.is_creature():
                all_creatures.append((card, p))
            if card.is_planeswalker():
                all_planeswalkers.append((card, p))

    # Filter based on target type
    if 'any target' in desc:
        # Can target creatures, players, or planeswalkers
        for creature, owner in all_creatures:
            targets.append((creature, f"{creature.name} ({owner.name}'s creature)"))
        for p in all_players:
            targets.append((p, f"{p.name} (player)"))
        for pw, owner in all_planeswalkers:
            targets.append((pw, f"{pw.name} ({owner.name}'s planeswalker)"))

    elif 'player or planeswalker' in desc:
        # Can target players or planeswalkers (Chandra +1 style)
        for p in all_players:
            targets.append((p, f"{p.name} (player)"))
        for pw, owner in all_planeswalkers:
            targets.append((pw, f"{pw.name} ({owner.name}'s planeswalker)"))

    elif 'creature or planeswalker' in desc:
        # Can target creatures or planeswalkers
        for creature, owner in all_creatures:
            targets.append((creature, f"{creature.name} ({owner.name}'s creature)"))
        for pw, owner in all_planeswalkers:
            targets.append((pw, f"{pw.name} ({owner.name}'s planeswalker)"))

    elif 'creature or player' in desc:
        # Can target creatures or players
        for creature, owner in all_creatures:
            targets.append((creature, f"{creature.name} ({owner.name}'s creature)"))
        for p in all_players:
            targets.append((p, f"{p.name} (player)"))

    elif 'creature' in desc:
        for creature, owner in all_creatures:
            # Check for restrictions like "opponent's creature"
            if 'opponent' in desc:
                player_idx = game.players.index(player)
                owner_idx = game.players.index(owner)
                if player_idx == owner_idx:
                    continue
            targets.append((creature, f"{creature.name} ({owner.name}'s)"))

    elif 'player' in desc or 'opponent' in desc:
        for p in all_players:
            if 'opponent' in desc:
                if p == player:
                    continue
            targets.append((p, f"{p.name}"))

    elif 'planeswalker' in desc:
        for pw, owner in all_planeswalkers:
            targets.append((pw, f"{pw.name} ({owner.name}'s)"))

    elif 'permanent' in desc:
        # May 7 audit (Bug 1): missing branch — was returning [] for any
        # "target permanent" ability, which made the legality check refuse
        # ALL Aminatou -1 activations. Now we honor "you own"/"you control"
        # and "another" qualifiers (per CR 109.4 — "another" excludes the
        # source).
        wants_own = 'you own' in oracle or 'you control' in oracle
        is_another = 'another' in oracle
        # Optional: PW's own card is the source for the "another" check.
        # The activate() caller already knows the source card; we approximate
        # by trying to find the activating PW via on_battlefield search.
        source_id = None
        for c in player.battlefield:
            if c.is_planeswalker() and (c.oracle_text or '').lower().find(oracle[:60]) >= 0:
                source_id = c.id
                break
        # July 29 batch audit: qualified forms ("nonland permanent" — Teferi,
        # Hero of Dominaria's -3) now parse into desc; honor the negation so
        # the legality check doesn't offer the excluded type.
        _neg_match = re.match(r'non(\w+)\s+permanent', desc)
        _neg = _neg_match.group(1) if _neg_match else None
        for perm, owner in all_permanents:
            if wants_own and owner is not player:
                continue
            if is_another and source_id and getattr(perm, 'id', None) == source_id:
                continue
            if _neg == 'land' and perm.is_land():
                continue
            if _neg == 'creature' and perm.is_creature(game):
                continue
            if _neg == 'token' and getattr(perm, 'is_token', False):
                continue
            targets.append((perm, f"{perm.name} ({owner.name}'s)"))

    return targets


# =============================================================================
# INTEGRATION HELPER
# =============================================================================

def create_planeswalker_manager(claude_client=None) -> PlaneswalkerManager:
    """Factory function to create a configured PlaneswalkerManager."""
    return PlaneswalkerManager(claude_client)
