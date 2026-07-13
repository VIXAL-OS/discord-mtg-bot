"""
MTG Replacement Effects System
================================

Handles "If X would happen, instead Y happens" effects.

Key Rules (CR 614):
1. If multiple replacements could apply, the affected player/controller chooses order
2. Each replacement effect can only apply once per event
3. Self-replacement effects apply first
4. "Can't" effects are not replacement effects but prevention effects (always win)

Common patterns:
- "If you would draw a card, instead..." (e.g., Chains of Mephistopheles)
- "If a creature would die, instead..." (e.g., Rest in Peace)  
- "If damage would be dealt, instead..." (e.g., Furnace of Rath)
- "If counters would be placed, instead..." (e.g., Doubling Season)
- "~ enters the battlefield with..." (self-replacement)

The pipeline:
1. Event is about to happen (draw, damage, death, etc.)
2. Check for applicable replacement effects
3. If multiple, affected player chooses order
4. Apply chosen replacement, generating new event
5. Check if more replacements apply to new event
6. Repeat until no more replacements
7. Final event happens
"""

import asyncio
from dataclasses import dataclass, field
from typing import Optional, Dict, List, Any, Tuple, Callable, Set
from enum import Enum, auto
from datetime import datetime


class EventType(Enum):
    """Types of events that can be replaced."""
    DRAW = auto()
    DAMAGE = auto()
    LIFE_GAIN = auto()
    LIFE_LOSS = auto()
    DEATH = auto()  # Creature/planeswalker going to graveyard
    DISCARD = auto()
    MILL = auto()
    COUNTER_PLACED = auto()
    ENTER_BATTLEFIELD = auto()
    LEAVE_BATTLEFIELD = auto()
    SACRIFICE = auto()
    DESTROY = auto()
    EXILE = auto()
    UNTAP = auto()
    COMBAT_DAMAGE = auto()
    TOKEN_CREATED = auto()


@dataclass
class GameEvent:
    """
    Represents an event that's about to happen.
    
    Events can be modified by replacement effects before they resolve.
    """
    event_type: EventType
    
    # Who/what is affected
    affected_player: Optional[str] = None  # Player name
    affected_object: Optional[str] = None  # Card/permanent ID
    affected_object_name: Optional[str] = None
    
    # Event details
    amount: int = 0  # For damage, life, counters, etc.
    source_name: str = ""
    source_id: str = ""
    source_controller: str = ""
    
    # For damage
    is_combat_damage: bool = False
    has_deathtouch: bool = False
    has_lifelink: bool = False
    has_infect: bool = False
    is_prevented: bool = False
    
    # For counters
    counter_type: str = ""
    
    # For zone changes
    from_zone: str = ""
    to_zone: str = ""

    # For ENTER_BATTLEFIELD replacement (Thalia, Heretic Cathar etc.)
    enters_tapped: Optional[bool] = None

    # May 20 audit (CRITICAL #5): the entering permanent's type_line, so
    # narrow-scope replacement effects (Authority of the Consuls = creatures
    # only, Imposing Sovereign = creatures only, Kismet = creatures/artifacts/
    # lands) can filter on the permanent's actual type rather than firing
    # on every ENTER_BATTLEFIELD event. game_1506623255178510416:1493,1533
    # showed Authority of the Consuls forcing Plains and Talisman of Progress
    # to enter tapped — the card text says "creatures your opponents control
    # enter tapped", lands and artifacts are out of scope.
    entering_type_line: str = ""

    # Tracking
    applied_replacements: Set[str] = field(default_factory=set)  # IDs of already-applied effects

    # Result (after all replacements)
    was_replaced: bool = False
    replacement_chain: List[str] = field(default_factory=list)
    
    def copy(self) -> 'GameEvent':
        """Create a copy of this event for modification."""
        return GameEvent(
            event_type=self.event_type,
            affected_player=self.affected_player,
            affected_object=self.affected_object,
            affected_object_name=self.affected_object_name,
            amount=self.amount,
            source_name=self.source_name,
            source_id=self.source_id,
            source_controller=self.source_controller,
            is_combat_damage=self.is_combat_damage,
            has_deathtouch=self.has_deathtouch,
            has_lifelink=self.has_lifelink,
            has_infect=self.has_infect,
            is_prevented=self.is_prevented,
            counter_type=self.counter_type,
            from_zone=self.from_zone,
            to_zone=self.to_zone,
            enters_tapped=self.enters_tapped,
            entering_type_line=self.entering_type_line,
            applied_replacements=set(self.applied_replacements),
            was_replaced=self.was_replaced,
            replacement_chain=list(self.replacement_chain),
        )


@dataclass
class ReplacementEffect:
    """
    A replacement effect that can modify events.
    
    Examples:
        - Rest in Peace: "If a card or token would be put into a graveyard 
          from anywhere, exile it instead."
        - Doubling Season: "If an effect would create one or more tokens 
          under your control, it creates twice that many instead."
        - Furnace of Rath: "If a source would deal damage to a permanent 
          or player, it deals double that damage instead."
    """
    id: str
    source_name: str
    source_id: str
    controller: str
    
    # What events this replaces
    replaces_event: EventType
    
    # Condition for applying (optional filter)
    condition: Optional[Callable[[GameEvent], bool]] = None
    condition_text: str = ""  # Human-readable condition
    
    # What this effect does to the event
    replacement_type: str = ""  # "exile_instead", "double", "prevent", etc.
    
    # For modifying amounts
    multiply_amount: Optional[float] = None  # 2.0 for double
    add_amount: int = 0
    set_amount: Optional[int] = None
    # May 20 audit (CRITICAL #3): Gisela halves damage "rounded up" per oracle.
    # Python int() truncates toward zero, so 1 * 0.5 → 0 instead of 1. Add an
    # explicit round_mode so halving effects can specify "up" (Gisela, Lich's
    # Mirror prevention) vs the default truncation. CR 107.1b: per-card text
    # specifies rounding direction; undefined defaults to truncation.
    round_mode: str = "down"  # "down" (default) | "up"
    
    # For redirecting zone changes
    new_destination: Optional[str] = None  # "exile" instead of "graveyard"
    
    # For preventing
    prevents: bool = False

    # For enters-tapped replacement (Thalia, Heretic Cathar)
    force_tapped: Optional[bool] = None

    # For self-replacement (ETB effects)
    is_self_replacement: bool = False
    
    # Additional effects (e.g., "gains life equal to that creature's toughness")
    additional_effect: Optional[Callable[[GameEvent], List[GameEvent]]] = None
    
    # Timestamp for ordering
    timestamp: datetime = field(default_factory=datetime.now)
    
    def applies_to(self, event: GameEvent) -> bool:
        """Check if this replacement applies to an event."""
        # Already applied?
        if self.id in event.applied_replacements:
            return False
        
        # Wrong event type?
        if event.event_type != self.replaces_event:
            return False
        
        # Check condition
        if self.condition and not self.condition(event):
            return False
        
        return True
    
    def apply(self, event: GameEvent) -> GameEvent:
        """
        Apply this replacement to an event.
        
        Returns the modified event.
        """
        result = event.copy()
        result.applied_replacements.add(self.id)
        result.was_replaced = True
        result.replacement_chain.append(f"{self.source_name}: {self.replacement_type}")
        
        # Prevent
        if self.prevents:
            result.is_prevented = True
            result.amount = 0
            return result
        
        # Modify amount
        if self.set_amount is not None:
            result.amount = self.set_amount
        elif self.multiply_amount is not None:
            # May 20 audit (CRITICAL #3): respect round_mode for halving
            # effects. Gisela's printed text is "deals half that damage...
            # rounded up". `int()` truncates toward zero → 1 → 0 was the bug
            # (game_1506623254943498252:790). Now: ceil for "up", floor for
            # "down" (default), neither shifts integer multipliers.
            import math as _math
            _new = result.amount * self.multiply_amount
            if self.round_mode == "up":
                result.amount = _math.ceil(_new)
            else:
                result.amount = int(_new)  # truncate toward zero
        result.amount += self.add_amount
        
        # Redirect destination
        if self.new_destination:
            result.to_zone = self.new_destination

        # Force enters-tapped state (Thalia, Heretic Cathar)
        if self.force_tapped is not None:
            result.enters_tapped = self.force_tapped

        return result


class ReplacementEngine:
    """
    Processes replacement effects for game events.
    
    Usage:
        engine = ReplacementEngine()
        
        # Register replacement effects
        engine.add_effect(rest_in_peace_effect)
        engine.add_effect(doubling_season_effect)
        
        # Process an event
        final_event = await engine.process_event(draw_event, game_state, 
                                                  choose_callback)
    """
    
    def __init__(self):
        self.effects: List[ReplacementEffect] = []
    
    def add_effect(self, effect: ReplacementEffect):
        """Add a replacement effect."""
        self.effects.append(effect)
    
    def remove_effect(self, effect_id: str):
        """Remove a replacement effect."""
        self.effects = [e for e in self.effects if e.id != effect_id]
    
    def remove_effects_from_source(self, source_id: str):
        """Remove all effects from a source.

        Matches effects whose source_id equals `source_id` OR begins with
        `source_id` followed by an underscore (parallel to layers.py — see
        that module for full rationale).
        """
        prefix = source_id + "_"
        self.effects = [
            e for e in self.effects
            if e.source_id != source_id and not e.source_id.startswith(prefix)
        ]
    
    async def process_event(
        self,
        event: GameEvent,
        game_state: Any,
        choose_callback: Optional[Callable[[str, List[ReplacementEffect]], 
                                           asyncio.Future]] = None
    ) -> GameEvent:
        """
        Process an event through all applicable replacement effects.
        
        Args:
            event: The event to process
            game_state: Current game state
            choose_callback: Async function to let player choose between 
                           multiple applicable effects
        
        Returns:
            The final event after all replacements
        """
        current = event.copy()
        max_iterations = 100  # Prevent infinite loops
        
        for _ in range(max_iterations):
            # Get applicable effects
            applicable = [e for e in self.effects if e.applies_to(current)]
            
            if not applicable:
                break
            
            # Self-replacement effects apply first
            self_replacements = [e for e in applicable if e.is_self_replacement]
            other_replacements = [e for e in applicable if not e.is_self_replacement]
            
            if self_replacements:
                # Apply self-replacements in timestamp order
                self_replacements.sort(key=lambda e: e.timestamp)
                for effect in self_replacements:
                    current = effect.apply(current)
                continue
            
            if not other_replacements:
                break
            
            # If only one effect, apply it
            if len(other_replacements) == 1:
                current = other_replacements[0].apply(current)
                continue
            
            # Multiple effects - affected player/controller chooses
            chosen = await self._choose_effect(
                current, other_replacements, game_state, choose_callback
            )
            
            if chosen:
                current = chosen.apply(current)
            else:
                break
        
        return current
    
    async def _choose_effect(
        self,
        event: GameEvent,
        effects: List[ReplacementEffect],
        game_state: Any,
        choose_callback: Optional[Callable]
    ) -> Optional[ReplacementEffect]:
        """Let the affected player choose which replacement to apply first."""
        
        # Determine who chooses
        chooser = event.affected_player
        if not chooser and event.affected_object:
            # Controller of affected object chooses
            # Would need to look up in game_state
            chooser = event.source_controller
        
        if choose_callback:
            # Use provided callback for player choice
            return await choose_callback(chooser, effects)
        else:
            # Default: timestamp order (earliest first)
            effects.sort(key=lambda e: e.timestamp)
            return effects[0] if effects else None
    
    def get_applicable_effects(self, event: GameEvent) -> List[ReplacementEffect]:
        """Get all effects that could apply to an event (for UI display)."""
        return [e for e in self.effects if e.applies_to(event)]

    def process_event_sync(self, event: GameEvent) -> GameEvent:
        """
        Synchronous version of process_event for use in sync code paths
        (combat damage, SBAs).

        Per CR 616.1 the affected player chooses the order when multiple
        replacement effects apply.  We can't prompt interactively in sync,
        so we use a heuristic:

        1. If ALL applicable effects are purely multiplicative / additive on
           the same field (e.g. two Doubling Seasons both doubling tokens),
           their order doesn't matter — keep timestamp order.
        2. Otherwise pick the ordering that benefits the affected player's
           controller.  Beneficial effects (multiply > 1, add > 0) are
           applied first so they feed into subsequent replacements, and
           harmful effects (prevent, redirect, reduce) are applied last.
           This mirrors how a rational player would choose per CR 616.1.

        TODO: Replace this heuristic with interactive player choice once
        the async path is available everywhere (requires making
        advance_phase() async — see CLAUDE.md "Sync Trigger Gap").
        """
        current = event.copy()
        max_iterations = 100  # Prevent infinite loops

        for _ in range(max_iterations):
            applicable = [e for e in self.effects if e.applies_to(current)]

            if not applicable:
                break

            # Self-replacement effects apply first (CR 614.16)
            self_replacements = [e for e in applicable if e.is_self_replacement]
            other_replacements = [e for e in applicable if not e.is_self_replacement]

            if self_replacements:
                self_replacements.sort(key=lambda e: e.timestamp)
                for effect in self_replacements:
                    current = effect.apply(current)
                continue

            if not other_replacements:
                break

            if len(other_replacements) == 1:
                current = other_replacements[0].apply(current)
                continue

            # Multiple effects — use heuristic ordering for the affected
            # player's benefit (CR 616.1).
            chosen = self._choose_best_for_controller(current, other_replacements)
            current = chosen.apply(current)

        return current

    # ------------------------------------------------------------------
    # Heuristic helper for process_event_sync
    # ------------------------------------------------------------------

    @staticmethod
    def _are_all_commutative(effects: List[ReplacementEffect]) -> bool:
        """
        Return True if all effects are purely multiplicative / additive on
        the event amount and therefore commutative (order doesn't matter).

        Examples of commutative stacks:
          - Two Doubling Seasons (both multiply_amount=2.0)
          - Doubling Season + Parallel Lives (same)
        Counter-examples:
          - Doubling Season + Hardened Scales (multiply vs add — order matters)
          - Furnace of Rath + damage prevention (multiply vs prevent)
        """
        if not effects:
            return True
        first = effects[0]
        for e in effects[1:]:
            # Both purely multiplicative?
            if (first.multiply_amount is not None and e.multiply_amount is not None
                    and first.add_amount == 0 and e.add_amount == 0
                    and first.set_amount is None and e.set_amount is None
                    and not first.prevents and not e.prevents
                    and first.new_destination is None and e.new_destination is None):
                # May 30 audit: two multipliers commute ONLY if they're the SAME
                # direction. A reducer (×0.5, Gisela halve) and an amplifier (×2,
                # Furnace) do NOT commute under integer floor-rounding — N=3 gives
                # halve-first=2 vs double-first=3 — so the affected player must be
                # given the choice (CR 616.1). Mixed-direction multipliers (one >1,
                # one <1) are non-commutative.
                if (first.multiply_amount - 1.0) * (e.multiply_amount - 1.0) < 0:
                    return False
                continue
            # Both purely additive?
            if (first.multiply_amount is None and e.multiply_amount is None
                    and first.set_amount is None and e.set_amount is None
                    and not first.prevents and not e.prevents
                    and first.new_destination is None and e.new_destination is None):
                continue
            return False
        return True

    @staticmethod
    def _effect_benefit_score(effect: ReplacementEffect) -> int:
        """
        Score an effect by how beneficial it is to the affected player.

        Higher score = more beneficial = should be applied FIRST so its
        output feeds into later replacements (maximises the controller's
        advantage per CR 616.1 heuristic).

        Score breakdown:
          - multiply > 1   →  +2  (doubles / triples are very beneficial)
          - add > 0         →  +1  (extra counter / amount)
          - neutral / zone  →   0
          - prevents        →  -2  (prevents remove the event entirely)
          - reduce (mult<1) →  -1
        """
        score = 0
        if effect.prevents:
            return -2
        if effect.multiply_amount is not None:
            if effect.multiply_amount > 1.0:
                score += 2
            elif effect.multiply_amount < 1.0:
                score -= 1
        if effect.add_amount > 0:
            score += 1
        elif effect.add_amount < 0:
            score -= 1
        if effect.new_destination is not None:
            # Redirects (e.g. exile instead of graveyard) — neutral; could
            # be beneficial or harmful depending on context.  Keep at 0 so
            # timestamp breaks ties.
            pass
        return score

    def _choose_best_for_controller(
        self,
        event: GameEvent,
        effects: List[ReplacementEffect],
    ) -> ReplacementEffect:
        """
        Pick the next replacement effect to apply using a benefit
        heuristic when we can't ask the player interactively.

        If all effects are commutative (e.g. two doublers), order
        doesn't matter — fall back to timestamp.  Otherwise, apply
        the most beneficial one first (highest score), with timestamp
        as tie-breaker.
        """
        if self._are_all_commutative(effects):
            effects.sort(key=lambda e: e.timestamp)
            return effects[0]

        # May 30 audit: the benefit score assumes a LARGER amount is good (tokens,
        # counters, life gain) — apply amplifiers first. But for DAMAGE / LIFE_LOSS
        # a larger amount HARMS the affected player, so the order they'd choose
        # (CR 616.1) is the opposite: apply REDUCERS (halve/prevent) first so they
        # feed into the amplifier and minimize the result. Invert the sort for
        # harmful events. (Furnace ×2 + Gisela halve on damage to Gisela's
        # controller: halve-first 3→1→2 beats double-first 3→6→3 for the victim.)
        harmful = event.event_type in (
            EventType.DAMAGE, EventType.COMBAT_DAMAGE, EventType.LIFE_LOSS)
        if harmful:
            # Ascending score → reducer (low score) applied first.
            effects.sort(key=lambda e: (self._effect_benefit_score(e), e.timestamp))
        else:
            # Descending score → amplifier (high score) applied first.
            effects.sort(key=lambda e: (-self._effect_benefit_score(e), e.timestamp))
        return effects[0]


# =============================================================================
# COMMON REPLACEMENT EFFECT FACTORIES
# =============================================================================

def create_rest_in_peace_effect(source_id: str, controller: str) -> ReplacementEffect:
    """
    Rest in Peace: "If a card or token would be put into a graveyard 
    from anywhere, exile it instead."
    """
    return ReplacementEffect(
        id=f"{source_id}_rip",
        source_name="Rest in Peace",
        source_id=source_id,
        controller=controller,
        replaces_event=EventType.DEATH,
        condition=lambda e: e.to_zone == "graveyard",
        condition_text="would be put into a graveyard",
        replacement_type="exile_instead",
        new_destination="exile",
    )


def create_doubling_season_counters(source_id: str, controller: str) -> ReplacementEffect:
    """
    Doubling Season (counters): "If an effect would put one or more counters 
    on a permanent you control, it puts twice that many counters instead."
    """
    return ReplacementEffect(
        id=f"{source_id}_ds_counters",
        source_name="Doubling Season",
        source_id=source_id,
        controller=controller,
        replaces_event=EventType.COUNTER_PLACED,
        condition=lambda e: True,  # Would check controller
        condition_text="counters on a permanent you control",
        replacement_type="double_counters",
        multiply_amount=2.0,
    )


def create_doubling_season_tokens(source_id: str, controller: str) -> ReplacementEffect:
    """
    Doubling Season (tokens): "If an effect would create one or more tokens 
    under your control, it creates twice that many instead."
    """
    return ReplacementEffect(
        id=f"{source_id}_ds_tokens",
        source_name="Doubling Season",
        source_id=source_id,
        controller=controller,
        replaces_event=EventType.TOKEN_CREATED,
        condition=lambda e: True,  # Would check controller
        condition_text="create tokens under your control",
        replacement_type="double_tokens",
        multiply_amount=2.0,
    )


def create_parallel_lives_effect(source_id: str, controller: str) -> ReplacementEffect:
    """
    Parallel Lives: "If an effect would create one or more tokens under
    your control, it creates twice that many of those tokens instead."

    Functionally identical to Doubling Season's token half — separate
    factory for clarity when registering by card name.
    """
    return ReplacementEffect(
        id=f"{source_id}_parallel_lives",
        source_name="Parallel Lives",
        source_id=source_id,
        controller=controller,
        replaces_event=EventType.TOKEN_CREATED,
        condition=lambda e: True,  # Would check controller
        condition_text="create tokens under your control",
        replacement_type="double_tokens",
        multiply_amount=2.0,
    )


def create_furnace_of_rath_effect(source_id: str, controller: str) -> ReplacementEffect:
    """
    Furnace of Rath: "If a source would deal damage to a permanent or player,
    it deals double that damage instead."

    House rule: only doubles damage from sources controlled by the Furnace's
    controller. This prevents opponent-controlled Furnaces from boosting your
    sources (Apr 2026 audit).
    """
    return ReplacementEffect(
        id=f"{source_id}_furnace",
        source_name="Furnace of Rath",
        source_id=source_id,
        controller=controller,
        replaces_event=EventType.DAMAGE,
        condition_text="all damage doubled",
        replacement_type="double_damage",
        multiply_amount=2.0,
        # May 30 audit: Furnace of Rath is SYMMETRIC — "If a SOURCE would deal
        # damage..." doubles ALL damage from ANY source to ANY target, including
        # damage dealt TO its own controller. The Apr 2026 "house rule" gating it
        # to source_controller == controller was CR-incorrect: it stopped Furnace
        # from doubling incoming damage, so it never co-applied with Gisela's halve
        # and the CR-616.1 controller-chooses-order case never fired (confirmed by
        # test_replacement_controller_order.py). No source condition — doubles all.
    )


def create_damage_prevention(source_name: str, source_id: str, 
                             controller: str, amount: int) -> ReplacementEffect:
    """
    Damage prevention effect (e.g., "Prevent the next 3 damage").
    """
    prevented_so_far = [0]  # Mutable to track across calls
    
    def check_remaining(event: GameEvent) -> bool:
        return prevented_so_far[0] < amount
    
    def apply_prevention(effect: ReplacementEffect, event: GameEvent) -> GameEvent:
        result = event.copy()
        prevent_amount = min(event.amount, amount - prevented_so_far[0])
        prevented_so_far[0] += prevent_amount
        result.amount = event.amount - prevent_amount
        if result.amount == 0:
            result.is_prevented = True
        result.was_replaced = True
        result.replacement_chain.append(f"{source_name}: prevent {prevent_amount}")
        return result
    
    effect = ReplacementEffect(
        id=f"{source_id}_prevent",
        source_name=source_name,
        source_id=source_id,
        controller=controller,
        replaces_event=EventType.DAMAGE,
        condition=check_remaining,
        condition_text=f"prevent up to {amount} damage",
        replacement_type="prevent_damage",
    )
    
    # Override apply method
    original_apply = effect.apply
    effect.apply = lambda e: apply_prevention(effect, e)
    
    return effect


def create_swords_to_plowshares_effect(source_id: str, controller: str,
                                        target_id: str) -> ReplacementEffect:
    """
    Swords to Plowshares: "Exile target creature. Its controller gains 
    life equal to its power."
    
    The "gains life" part happens as a result of the replacement.
    """
    return ReplacementEffect(
        id=f"{source_id}_stp",
        source_name="Swords to Plowshares",
        source_id=source_id,
        controller=controller,
        replaces_event=EventType.DEATH,
        condition=lambda e: e.affected_object == target_id,
        condition_text="exiled by Swords to Plowshares",
        replacement_type="exile_instead",
        new_destination="exile",
        # Note: Life gain would be tracked separately
    )


def create_enters_tapped_effect(source_id: str, permanent_name: str) -> ReplacementEffect:
    """
    Self-replacement for "enters the battlefield tapped".
    """
    return ReplacementEffect(
        id=f"{source_id}_etb_tapped",
        source_name=permanent_name,
        source_id=source_id,
        controller="",  # Self
        replaces_event=EventType.ENTER_BATTLEFIELD,
        condition=lambda e: e.affected_object == source_id,
        condition_text="enters the battlefield",
        replacement_type="enters_tapped",
        is_self_replacement=True,
    )


def create_enters_with_counters_effect(source_id: str, permanent_name: str,
                                        counter_type: str, 
                                        amount: int) -> ReplacementEffect:
    """
    Self-replacement for "enters the battlefield with N counters".
    """
    return ReplacementEffect(
        id=f"{source_id}_etb_counters",
        source_name=permanent_name,
        source_id=source_id,
        controller="",  # Self
        replaces_event=EventType.ENTER_BATTLEFIELD,
        condition=lambda e: e.affected_object == source_id,
        condition_text="enters the battlefield",
        replacement_type="enters_with_counters",
        is_self_replacement=True,
        # Would need additional_effect to actually add counters
    )


# =============================================================================
# INTEGRATION WITH GAME ENGINE
# =============================================================================

class ReplacementAwareExecutor:
    """
    Wraps effect execution with replacement effect processing.
    
    Usage:
        executor = ReplacementAwareExecutor(replacement_engine)
        
        # Instead of directly dealing damage:
        event = GameEvent(
            event_type=EventType.DAMAGE,
            affected_player="Bob",
            amount=3,
            source_name="Lightning Bolt"
        )
        final_event = await executor.execute_with_replacements(event, game_state)
        
        # Apply the final event
        if not final_event.is_prevented:
            player.life -= final_event.amount
    """
    
    def __init__(self, replacement_engine: ReplacementEngine):
        self.replacements = replacement_engine
    
    async def deal_damage(
        self,
        amount: int,
        target_player: Optional[str],
        target_permanent: Optional[str],
        source_name: str,
        source_id: str,
        source_controller: str,
        is_combat: bool = False,
        has_deathtouch: bool = False,
        has_lifelink: bool = False,
        has_infect: bool = False,
        game_state: Any = None,
        choose_callback: Any = None
    ) -> GameEvent:
        """Deal damage with replacement effect processing."""
        event = GameEvent(
            event_type=EventType.DAMAGE,
            affected_player=target_player,
            affected_object=target_permanent,
            amount=amount,
            source_name=source_name,
            source_id=source_id,
            source_controller=source_controller,
            is_combat_damage=is_combat,
            has_deathtouch=has_deathtouch,
            has_lifelink=has_lifelink,
            has_infect=has_infect,
        )
        
        return await self.replacements.process_event(event, game_state, choose_callback)
    
    async def draw_cards(
        self,
        player: str,
        amount: int,
        source_name: str,
        game_state: Any = None,
        choose_callback: Any = None
    ) -> GameEvent:
        """Draw cards with replacement effect processing."""
        event = GameEvent(
            event_type=EventType.DRAW,
            affected_player=player,
            amount=amount,
            source_name=source_name,
        )
        
        return await self.replacements.process_event(event, game_state, choose_callback)
    
    async def creature_dies(
        self,
        permanent_id: str,
        permanent_name: str,
        controller: str,
        source_name: str,
        game_state: Any = None,
        choose_callback: Any = None
    ) -> GameEvent:
        """Process creature death with replacements (e.g., Rest in Peace)."""
        event = GameEvent(
            event_type=EventType.DEATH,
            affected_object=permanent_id,
            affected_object_name=permanent_name,
            affected_player=controller,
            source_name=source_name,
            from_zone="battlefield",
            to_zone="graveyard",
        )
        
        return await self.replacements.process_event(event, game_state, choose_callback)
    
    async def place_counters(
        self,
        permanent_id: str,
        permanent_name: str,
        controller: str,
        counter_type: str,
        amount: int,
        source_name: str,
        game_state: Any = None,
        choose_callback: Any = None
    ) -> GameEvent:
        """Place counters with replacement effects (e.g., Doubling Season)."""
        event = GameEvent(
            event_type=EventType.COUNTER_PLACED,
            affected_object=permanent_id,
            affected_object_name=permanent_name,
            affected_player=controller,
            amount=amount,
            counter_type=counter_type,
            source_name=source_name,
        )
        
        return await self.replacements.process_event(event, game_state, choose_callback)


# =============================================================================
# ORACLE TEXT SCANNER — auto-detect replacement effects from card text
# =============================================================================

import re as _re

# Named card templates for cards with non-standard oracle wording
_NAMED_CARD_REPLACEMENTS = {
    "rest in peace": lambda card_id, controller: [
        create_rest_in_peace_effect(card_id, controller)
    ],
    "leyline of the void": lambda card_id, controller: [
        ReplacementEffect(
            id=f"{card_id}_leyline_void",
            source_name="Leyline of the Void",
            source_id=card_id,
            controller=controller,
            replaces_event=EventType.DEATH,
            condition=lambda e: e.to_zone == "graveyard",
            condition_text="opponent's card would go to graveyard",
            replacement_type="exile_instead",
            new_destination="exile",
        )
    ],
    "furnace of rath": lambda card_id, controller: [
        create_furnace_of_rath_effect(card_id, controller)
    ],
    "dictate of the twin gods": lambda card_id, controller: [
        ReplacementEffect(
            id=f"{card_id}_dictate",
            source_name="Dictate of the Twin Gods",
            source_id=card_id,
            controller=controller,
            replaces_event=EventType.DAMAGE,
            condition_text="all damage doubled (symmetric)",
            replacement_type="double_damage",
            multiply_amount=2.0,
            # June 10 audit (V25): the Apr-2026 "your sources only" house rule
            # was wrong for THIS card — Dictate's printed text is fully
            # symmetric ("If a source would deal damage to a permanent or
            # player, it deals double that damage instead"), same wording
            # class as Furnace of Rath, whose condition the May 30 sprint
            # already removed. Kambal's 2 damage to Rick resolved as 1
            # (Gisela halving, Dictate absent) when the CR-correct answer is
            # 2 in either CR 616.1 order. Fiery Emancipation's gate below
            # stays — its oracle really says "a source you control".
        )
    ],
    # June 10 audit (V25 bonus): Curse of Bloodletting wasn't registered at
    # all despite being added to the revised replacement_chain deck (May 30).
    # "Enchant player. If a source would deal damage to enchanted player, it
    # deals double that damage instead." Gated on the RECIPIENT being the
    # enchanted player (read from the aura's attached_to / cursed-player
    # marker at registration time via the affected_player on the event).
    "curse of bloodletting": lambda card_id, controller: [
        ReplacementEffect(
            id=f"{card_id}_curse_bloodletting",
            source_name="Curse of Bloodletting",
            source_id=card_id,
            controller=controller,
            replaces_event=EventType.DAMAGE,
            condition_text="damage to enchanted player doubled",
            replacement_type="double_damage",
            multiply_amount=2.0,
            # The curse doubles damage dealt TO the enchanted player. The
            # registration path doesn't know the attach target here, so use
            # the controller-relative default: a curse is cast on an
            # opponent, so double damage whose affected player is NOT the
            # curse's controller. (Exact attach tracking can replace this
            # when enchant-player auras carry attached_to through
            # registration.)
            condition=lambda ev, _ctrl=controller: (
                bool(getattr(ev, 'affected_player', None))
                and ev.affected_player != _ctrl
            ),
        )
    ],
    # June 10 deep-dive: Twinflame Tyrant — real text is a static doubler
    # ("If a source you control would deal damage to an opponent or a
    # permanent an opponent controls, it deals double that damage instead").
    # It was never registered here (four qualifying events resolved
    # undoubled in game …069616767067), and its Tier-1.5 template was a
    # hallucinated "deal 5 damage" (deleted same day). Both gates: source
    # you control AND opponent-side recipient — same shape as Gisela's
    # double_damage_opp clause.
    "twinflame tyrant": lambda card_id, controller: [
        ReplacementEffect(
            id=f"{card_id}_twinflame",
            source_name="Twinflame Tyrant",
            source_id=card_id,
            controller=controller,
            replaces_event=EventType.DAMAGE,
            condition_text="damage from your sources to opponents is doubled",
            replacement_type="double_damage",
            multiply_amount=2.0,
            condition=lambda e, _ctrl=controller: (
                bool(e.source_controller) and e.source_controller == _ctrl
                and (e.affected_player or "") and e.affected_player != _ctrl
            ),
        )
    ],
    "fiery emancipation": lambda card_id, controller: [
        ReplacementEffect(
            id=f"{card_id}_fiery",
            source_name="Fiery Emancipation",
            source_id=card_id,
            controller=controller,
            replaces_event=EventType.DAMAGE,
            condition_text="damage from your sources tripled",
            replacement_type="triple_damage",
            multiply_amount=3.0,
            # House rule: multiplier only fires for damage from sources you
            # control. This avoids opposing sources getting a "free" boost
            # off your own Fiery Emancipation (audit Apr 2026).
            condition=lambda ev, _ctrl=controller: (
                bool(ev.source_controller) and ev.source_controller == _ctrl
            ),
        )
    ],
    "doubling season": lambda card_id, controller: [
        create_doubling_season_counters(card_id, controller),
        create_doubling_season_tokens(card_id, controller),
    ],
    "parallel lives": lambda card_id, controller: [
        create_parallel_lives_effect(card_id, controller),
    ],
    "anointed procession": lambda card_id, controller: [
        ReplacementEffect(
            id=f"{card_id}_anointed",
            source_name="Anointed Procession",
            source_id=card_id,
            controller=controller,
            replaces_event=EventType.TOKEN_CREATED,
            condition_text="create tokens under your control",
            condition=lambda event: event.affected_player == controller,
            replacement_type="double_tokens",
            multiply_amount=2.0,
        )
    ],
    "branching evolution": lambda card_id, controller: [
        ReplacementEffect(
            id=f"{card_id}_branching",
            source_name="Branching Evolution",
            source_id=card_id,
            controller=controller,
            replaces_event=EventType.COUNTER_PLACED,
            condition_text="+1/+1 counters on a creature you control",
            replacement_type="double_counters",
            multiply_amount=2.0,
        )
    ],
    "hardened scales": lambda card_id, controller: [
        ReplacementEffect(
            id=f"{card_id}_hardened",
            source_name="Hardened Scales",
            source_id=card_id,
            controller=controller,
            replaces_event=EventType.COUNTER_PLACED,
            condition_text="+1/+1 counters on a creature you control",
            replacement_type="extra_counter",
            add_amount=1,
        )
    ],
    # --- DRAW replacement effects ---
    "narset, parter of veils": lambda card_id, controller: [
        ReplacementEffect(
            id=f"{card_id}_narset_draw",
            source_name="Narset, Parter of Veils",
            source_id=card_id,
            controller=controller,
            replaces_event=EventType.DRAW,
            condition=lambda e: e.affected_player != controller,  # opponents only
            condition_text="opponents can't draw more than one card each turn",
            replacement_type="prevent_extra_draw",
            prevents=True,
        )
    ],
    "spirit of the labyrinth": lambda card_id, controller: [
        ReplacementEffect(
            id=f"{card_id}_spirit_draw",
            source_name="Spirit of the Labyrinth",
            source_id=card_id,
            controller=controller,
            replaces_event=EventType.DRAW,
            condition=lambda e: e.affected_player != controller,  # opponents only
            condition_text="each opponent can't draw more than one card each turn",
            replacement_type="prevent_extra_draw",
            prevents=True,
        )
    ],
    # --- LIFE_GAIN replacement effects ---
    "erebos, god of the dead": lambda card_id, controller: [
        ReplacementEffect(
            id=f"{card_id}_erebos_lifegain",
            source_name="Erebos, God of the Dead",
            source_id=card_id,
            controller=controller,
            replaces_event=EventType.LIFE_GAIN,
            condition=lambda e: e.affected_player != controller,  # opponents can't gain life
            condition_text="your opponents can't gain life",
            replacement_type="prevent_lifegain",
            prevents=True,
        )
    ],
    "sulfuric vortex": lambda card_id, controller: [
        ReplacementEffect(
            id=f"{card_id}_sulfuric_lifegain",
            source_name="Sulfuric Vortex",
            source_id=card_id,
            controller=controller,
            replaces_event=EventType.LIFE_GAIN,
            condition_text="players can't gain life",
            replacement_type="prevent_lifegain",
            prevents=True,
        )
    ],
    # --- ENTER_BATTLEFIELD replacement effects ---
    # May 23 audit (MAJOR #8): Thalia, Blind Obedience, and Kismet were
    # missing the type-line filter that Authority of the Consuls + Imposing
    # Sovereign got in the May 20 sprint. The plain `affected_player !=
    # controller` condition fired on EVERY opponent permanent including basic
    # lands (Thalia) and auras (anything). Oracle-text-driven filters:
    #   - Thalia: "Creatures and nonbasic lands"
    #   - Blind Obedience: "Artifacts and creatures"
    #   - Kismet: "Artifacts, creatures, and lands"
    "thalia, heretic cathar": lambda card_id, controller: [
        ReplacementEffect(
            id=f"{card_id}_thalia_tapped",
            source_name="Thalia, Heretic Cathar",
            source_id=card_id,
            controller=controller,
            replaces_event=EventType.ENTER_BATTLEFIELD,
            condition=lambda e, _ctrl=controller: (
                e.affected_player != _ctrl
                and (
                    'creature' in (e.entering_type_line or '').lower()
                    or (
                        'land' in (e.entering_type_line or '').lower()
                        and 'basic' not in (e.entering_type_line or '').lower()
                    )
                )
            ),
            condition_text="creatures and nonbasic lands opponents control enter tapped",
            replacement_type="enters_tapped",
            force_tapped=True,
        )
    ],
    "blind obedience": lambda card_id, controller: [
        ReplacementEffect(
            id=f"{card_id}_blind_tapped",
            source_name="Blind Obedience",
            source_id=card_id,
            controller=controller,
            replaces_event=EventType.ENTER_BATTLEFIELD,
            condition=lambda e, _ctrl=controller: (
                e.affected_player != _ctrl
                and (
                    'artifact' in (e.entering_type_line or '').lower()
                    or 'creature' in (e.entering_type_line or '').lower()
                )
            ),
            condition_text="artifacts and creatures opponents control enter tapped",
            replacement_type="enters_tapped",
            force_tapped=True,
        )
    ],
    "kismet": lambda card_id, controller: [
        ReplacementEffect(
            id=f"{card_id}_kismet_tapped",
            source_name="Kismet",
            source_id=card_id,
            controller=controller,
            replaces_event=EventType.ENTER_BATTLEFIELD,
            condition=lambda e, _ctrl=controller: (
                e.affected_player != _ctrl
                and (
                    'artifact' in (e.entering_type_line or '').lower()
                    or 'creature' in (e.entering_type_line or '').lower()
                    or 'land' in (e.entering_type_line or '').lower()
                )
            ),
            condition_text="artifacts, creatures, and lands opponents control enter tapped",
            replacement_type="enters_tapped",
            force_tapped=True,
        )
    ],
    "authority of the consuls": lambda card_id, controller: [
        ReplacementEffect(
            id=f"{card_id}_authority_tapped",
            source_name="Authority of the Consuls",
            source_id=card_id,
            controller=controller,
            replaces_event=EventType.ENTER_BATTLEFIELD,
            # May 20 audit (CRITICAL #5): authority is creatures-only per oracle
            # text "Creatures your opponents control enter tapped." Previously
            # this fired on every opponent permanent (Plains, Talisman, etc.).
            condition=lambda e, _ctrl=controller: (
                e.affected_player != _ctrl
                and 'creature' in (e.entering_type_line or '').lower()
            ),
            condition_text="creatures opponents control enter tapped",
            replacement_type="enters_tapped",
            force_tapped=True,
        )
    ],
    "imposing sovereign": lambda card_id, controller: [
        ReplacementEffect(
            id=f"{card_id}_imposing_tapped",
            source_name="Imposing Sovereign",
            source_id=card_id,
            controller=controller,
            replaces_event=EventType.ENTER_BATTLEFIELD,
            # May 20 audit (CRITICAL #5): same fix as Authority — Imposing
            # Sovereign is creatures-only per oracle text.
            condition=lambda e, _ctrl=controller: (
                e.affected_player != _ctrl
                and 'creature' in (e.entering_type_line or '').lower()
            ),
            condition_text="creatures opponents control enter tapped",
            replacement_type="enters_tapped",
            force_tapped=True,
        )
    ],
    "frozen aether": lambda card_id, controller: [
        ReplacementEffect(
            id=f"{card_id}_frozen_aether_tapped",
            source_name="Frozen Aether",
            source_id=card_id,
            controller=controller,
            replaces_event=EventType.ENTER_BATTLEFIELD,
            condition=lambda e: e.affected_player != controller,
            condition_text="permanents opponents control enter tapped",
            replacement_type="enters_tapped",
            force_tapped=True,
        )
    ],
    "loxodon gatekeeper": lambda card_id, controller: [
        ReplacementEffect(
            id=f"{card_id}_loxodon_gatekeeper_tapped",
            source_name="Loxodon Gatekeeper",
            source_id=card_id,
            controller=controller,
            replaces_event=EventType.ENTER_BATTLEFIELD,
            condition=lambda e: e.affected_player != controller,
            condition_text="artifacts, creatures, and lands opponents control enter tapped",
            replacement_type="enters_tapped",
            force_tapped=True,
        )
    ],
    # --- Additional LIFE_GAIN prevention ---
    "rampaging ferocidon": lambda card_id, controller: [
        ReplacementEffect(
            id=f"{card_id}_ferocidon_lifegain",
            source_name="Rampaging Ferocidon",
            source_id=card_id,
            controller=controller,
            replaces_event=EventType.LIFE_GAIN,
            condition_text="players can't gain life",
            replacement_type="prevent_lifegain",
            prevents=True,
        )
    ],
    "tibalt, rakish instigator": lambda card_id, controller: [
        ReplacementEffect(
            id=f"{card_id}_tibalt_lifegain",
            source_name="Tibalt, Rakish Instigator",
            source_id=card_id,
            controller=controller,
            replaces_event=EventType.LIFE_GAIN,
            condition=lambda e: e.affected_player != controller,
            condition_text="your opponents can't gain life",
            replacement_type="prevent_lifegain",
            prevents=True,
        )
    ],
    # --- DAMAGE prevention / fog effects ---
    "glacial chasm": lambda card_id, controller: [
        ReplacementEffect(
            id=f"{card_id}_glacial_prevent",
            source_name="Glacial Chasm",
            source_id=card_id,
            controller=controller,
            replaces_event=EventType.DAMAGE,
            condition=lambda e: e.affected_player == controller,
            condition_text="prevent all damage that would be dealt to you",
            replacement_type="prevent_damage",
            prevents=True,
        )
    ],
    # --- DRAW redirection (Notion Thief, Alms Collector) ---
    "notion thief": lambda card_id, controller: [
        ReplacementEffect(
            id=f"{card_id}_notion_thief",
            source_name="Notion Thief",
            source_id=card_id,
            controller=controller,
            replaces_event=EventType.DRAW,
            condition=lambda e, ctrl=controller: e.affected_player != ctrl,
            condition_text="If an opponent would draw a card except the first one each turn, instead that player skips that draw and you draw a card",
            replacement_type="redirect_draw",
            # Custom apply: redirect draw to controller instead of preventing
            # We set prevents=True here and handle the redirect in game engine
            # by checking replacement_type == "redirect_draw"
            prevents=True,
        )
    ],
    "alms collector": lambda card_id, controller: [
        ReplacementEffect(
            id=f"{card_id}_alms_collector",
            source_name="Alms Collector",
            source_id=card_id,
            controller=controller,
            replaces_event=EventType.DRAW,
            condition=lambda e, ctrl=controller: e.affected_player != ctrl and e.amount > 1,
            condition_text="If an opponent would draw 2+ cards, instead they draw one and you draw the rest",
            replacement_type="redirect_draw",
            prevents=True,
        )
    ],
    # --- DEATH replacement (Undying, Persist, Totem Armor) ---
    # These use EventType.DEATH with new_destination="battlefield" to
    # indicate the creature returns instead of going to graveyard
    "mikaeus, the unhallowed": lambda card_id, controller: [
        ReplacementEffect(
            id=f"{card_id}_mikaeus_undying",
            source_name="Mikaeus, the Unhallowed",
            source_id=card_id,
            controller=controller,
            replaces_event=EventType.DEATH,
            # Mikaeus grants undying ONLY to non-Human creatures the same
            # controller (Mikaeus's controller) controls. Without the
            # affected_player check, Claude's Mikaeus was redirecting Rick's
            # dying creatures (e.g., Rick's Mulldrifter post-evoke) back to
            # the battlefield — wrong player got free recursion.
            condition=lambda e, _ctrl=controller: (
                e.to_zone == "graveyard"
                and e.affected_player == _ctrl
                and (e.affected_object_name or "").lower() != "mikaeus, the unhallowed"
            ),
            condition_text="non-Human creatures you control have undying",
            replacement_type="undying",
            new_destination="battlefield",  # Returns to battlefield
        )
    ],
    # --- DAMAGE redirect effects ---
    "gisela, blade of goldnight": lambda card_id, controller: [
        ReplacementEffect(
            id=f"{card_id}_gisela_double",
            source_name="Gisela, Blade of Goldnight",
            source_id=card_id,
            controller=controller,
            replaces_event=EventType.DAMAGE,
            # Double only damage from YOUR sources dealt to opponents /
            # opponent-controlled permanents. Requires source_controller to
            # be set (fail-safe: if unknown, don't fire — prevents false
            # positives on malformed events).
            condition=lambda e, _ctrl=controller: (
                bool(e.source_controller) and e.source_controller == _ctrl
                and (e.affected_player or "") and e.affected_player != _ctrl
            ),
            condition_text="damage from your sources to opponents is doubled",
            replacement_type="double_damage_opp",
            multiply_amount=2.0,
        ),
        ReplacementEffect(
            id=f"{card_id}_gisela_half",
            source_name="Gisela, Blade of Goldnight",
            source_id=card_id,
            controller=controller,
            replaces_event=EventType.DAMAGE,
            # Halve damage dealt to you or permanents you control (any source,
            # per oracle clause 2). Require affected_player to be non-empty
            # and match controller.
            condition=lambda e, _ctrl=controller: (
                bool(e.affected_player) and e.affected_player == _ctrl
            ),
            condition_text="damage dealt to you is halved",
            replacement_type="halve_damage_self",
            multiply_amount=0.5,
            # May 24 audit fix: oracle says "prevent half that damage, rounded up"
            # — the "rounded up" applies to PREVENTION, not to damage dealt.
            # So if D=3: prevented=ceil(3/2)=2, dealt=D-prevented=3-2=1.
            # Equivalently: dealt = floor(D/2) = round_mode="down".
            # The May 20 fix had this inverted (set round_mode="up" on amount
            # dealt), producing D=3→2 instead of D=3→1. Even-D cases were
            # correct by coincidence (D=4 → ceil(2)=2, floor(2)=2).
            round_mode="down",
        ),
    ],
}

# Regex patterns for generic replacement effect detection
_REPLACEMENT_PATTERNS = [
    # "If a card or token would be put into a graveyard ... exile it instead"
    (_re.compile(
        r'if (?:a |any )?(?:card|creature|permanent|token).*would (?:be put into|die|go to).*graveyard.*exile.*instead',
        _re.IGNORECASE
    ), EventType.DEATH, "exile_instead", {"new_destination": "exile"}),

    # "If a source would deal damage ... double/twice"
    (_re.compile(
        r'if (?:a source|damage) would (?:be dealt|deal damage).*(?:double|twice)',
        _re.IGNORECASE
    ), EventType.DAMAGE, "double_damage", {"multiply_amount": 2.0}),

    # "If a source would deal damage ... triple that damage"
    (_re.compile(
        r'(?:a source|it) (?:would deal|deals?) (?:damage|that damage).*triple',
        _re.IGNORECASE
    ), EventType.DAMAGE, "triple_damage", {"multiply_amount": 3.0}),

    # "If an effect would create/put ... token ... twice that many"
    (_re.compile(
        r'if (?:an effect|you) would (?:create|put).*token.*twice that many',
        _re.IGNORECASE
    ), EventType.TOKEN_CREATED, "double_tokens", {"multiply_amount": 2.0}),

    # "If ... would place/put ... counter ... twice that many"
    (_re.compile(
        r'if (?:an effect|you) would (?:place|put).*counter.*twice that many',
        _re.IGNORECASE
    ), EventType.COUNTER_PLACED, "double_counters", {"multiply_amount": 2.0}),

    # "opponents/players can't draw more than one card" (Narset variants)
    (_re.compile(
        r"(?:opponents?|each other player) can.?t draw (?:more than one|additional) card",
        _re.IGNORECASE
    ), EventType.DRAW, "prevent_extra_draw", {"prevents": True}),

    # "players/opponents can't gain life" (Erebos, Sulfuric Vortex variants)
    (_re.compile(
        r"(?:players?|opponents?) can.?t gain life",
        _re.IGNORECASE
    ), EventType.LIFE_GAIN, "prevent_lifegain", {"prevents": True}),

    # "If an opponent would draw ... instead" (Notion Thief / Alms Collector variants)
    (_re.compile(
        r"if (?:an opponent|a player other than you) would draw.*instead",
        _re.IGNORECASE
    ), EventType.DRAW, "redirect_draw", {"prevents": True}),

    # "creatures/permanents/lands [opponents control] enter ... tapped"
    # Must NOT match one-time ETB search effects like "search...put onto the battlefield tapped"
    # (Primeval Titan, Ulvenwald Hydra, Wood Elves, etc.) — those are spell effects, not replacement effects.
    # Real replacement effects: Kismet, Thalia, Blind Obedience, Archon of Emeria, etc.
    (_re.compile(
        r"(?:creatures?|permanents?|artifacts?|lands?|nonland permanents?)(?:\s+(?:your\s+)?opponents?\s+control)?\s+enter\s+the\s+battlefield\s+tapped",
        _re.IGNORECASE
    ), EventType.ENTER_BATTLEFIELD, "enters_tapped", {"force_tapped": True}),
]


def scan_oracle_for_replacements(
    card_id: str,
    card_name: str,
    oracle_text: str,
    controller: str
) -> List[ReplacementEffect]:
    """
    Scan a permanent's oracle text for replacement effects and return
    ReplacementEffect instances to register with the engine.

    Checks named card templates first (exact match), then falls back
    to regex pattern matching on oracle text.
    """
    # Check named card templates first (most reliable) — works even without oracle text
    name_lower = card_name.lower().strip()
    if name_lower in _NAMED_CARD_REPLACEMENTS:
        effects = _NAMED_CARD_REPLACEMENTS[name_lower](card_id, controller)
        return effects

    # Fall back to generic oracle text patterns (need oracle text for regex)
    if not oracle_text:
        return []
    results = []
    oracle_lower = oracle_text.lower()
    for pattern, event_type, replacement_type, kwargs in _REPLACEMENT_PATTERNS:
        match = pattern.search(oracle_text)
        if not match:
            continue
        effect_kwargs = dict(kwargs)
        # Scope the condition correctly when the matched text includes
        # "opponents control" — the replacement should only fire for
        # opposing permanents, not the controller's own. Without this,
        # Authority of the Consuls (matched generically) would tap the
        # controller's own creatures too.
        matched_text = match.group(0).lower()
        if (replacement_type == "enters_tapped"
                and "opponents control" in matched_text
                and "condition" not in effect_kwargs):
            ctrl = controller  # Capture for closure
            effect_kwargs["condition"] = lambda e, _c=ctrl: e.affected_player != _c
        effect = ReplacementEffect(
            id=f"{card_id}_{replacement_type}",
            source_name=card_name,
            source_id=card_id,
            controller=controller,
            replaces_event=event_type,
            condition_text=oracle_text[:80],
            replacement_type=replacement_type,
            **effect_kwargs,
        )
        results.append(effect)

    return results


# =============================================================================
# DEMO / TEST
# =============================================================================

async def demo():
    """Demo the replacement effects system."""
    print("=== Replacement Effects Demo ===\n")
    
    engine = ReplacementEngine()
    
    # Test 1: Furnace of Rath doubles damage
    print("--- Test 1: Furnace of Rath ---")
    furnace = create_furnace_of_rath_effect("furnace_1", "Alice")
    engine.add_effect(furnace)
    
    damage_event = GameEvent(
        event_type=EventType.DAMAGE,
        affected_player="Bob",
        amount=3,
        source_name="Lightning Bolt",
        source_controller="Alice"
    )
    
    result = await engine.process_event(damage_event, None)
    print(f"  Original damage: {damage_event.amount}")
    print(f"  After Furnace: {result.amount}")
    print(f"  Replacement chain: {result.replacement_chain}")
    print(f"  Expected: 6 (doubled)")
    
    # Test 2: Two Furnaces!
    print("\n--- Test 2: Two Furnaces ---")
    furnace2 = create_furnace_of_rath_effect("furnace_2", "Bob")
    engine.add_effect(furnace2)
    
    damage_event2 = GameEvent(
        event_type=EventType.DAMAGE,
        affected_player="Bob",
        amount=3,
        source_name="Lightning Bolt",
        source_controller="Alice"
    )
    
    result2 = await engine.process_event(damage_event2, None)
    print(f"  Original damage: {damage_event2.amount}")
    print(f"  After two Furnaces: {result2.amount}")
    print(f"  Replacement chain: {result2.replacement_chain}")
    print(f"  Expected: 12 (doubled twice)")
    
    # Test 3: Rest in Peace
    print("\n--- Test 3: Rest in Peace ---")
    engine.effects.clear()
    
    rip = create_rest_in_peace_effect("rip_1", "Alice")
    engine.add_effect(rip)
    
    death_event = GameEvent(
        event_type=EventType.DEATH,
        affected_object="creature_1",
        affected_object_name="Grizzly Bears",
        affected_player="Bob",
        from_zone="battlefield",
        to_zone="graveyard",
        source_name="Doom Blade"
    )
    
    result3 = await engine.process_event(death_event, None)
    print(f"  Original destination: {death_event.to_zone}")
    print(f"  After RIP: {result3.to_zone}")
    print(f"  Replacement chain: {result3.replacement_chain}")
    print(f"  Expected: exile")
    
    # Test 4: Doubling Season with counters
    print("\n--- Test 4: Doubling Season ---")
    engine.effects.clear()
    
    ds = create_doubling_season_counters("ds_1", "Alice")
    engine.add_effect(ds)
    
    counter_event = GameEvent(
        event_type=EventType.COUNTER_PLACED,
        affected_object="walker_1",
        affected_object_name="Jace",
        affected_player="Alice",
        amount=3,
        counter_type="loyalty",
        source_name="Jace ETB"
    )
    
    result4 = await engine.process_event(counter_event, None)
    print(f"  Original counters: {counter_event.amount}")
    print(f"  After Doubling Season: {result4.amount}")
    print(f"  Expected: 6")
    
    # Test 5: Using the executor wrapper
    print("\n--- Test 5: ReplacementAwareExecutor ---")
    engine.effects.clear()
    engine.add_effect(create_furnace_of_rath_effect("furnace_3", "Alice"))
    
    executor = ReplacementAwareExecutor(engine)
    
    final_damage = await executor.deal_damage(
        amount=5,
        target_player="Bob",
        target_permanent=None,
        source_name="Fireball",
        source_id="fireball_1",
        source_controller="Alice",
        is_combat=False
    )
    
    print(f"  Fireball for 5 with Furnace:")
    print(f"  Final damage: {final_damage.amount}")
    print(f"  Expected: 10")
    
    print("\n=== Demo Complete ===")


if __name__ == "__main__":
    asyncio.run(demo())
