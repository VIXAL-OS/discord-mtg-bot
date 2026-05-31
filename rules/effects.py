"""
MTG Effect Execution Engine
============================

Translates card effects into game state changes.

The pipeline:
1. Card text → Parsed effect (via XMage or pattern matching)
2. Effect + Targets → Execution plan
3. Execution plan → Game state mutations

Effect Categories:
- One-shot effects (damage, draw, destroy, etc.)
- Continuous effects (handled by layers system)
- Triggered abilities (handled by priority system)
- Replacement effects (handled by replacement system)

This module handles ONE-SHOT effects - the "do the thing" part.
"""

import re
import asyncio
from dataclasses import dataclass, field
from typing import Optional, Dict, List, Any, Tuple, Callable, Union
from enum import Enum, auto
from abc import ABC, abstractmethod


class ActionType(Enum):
    """Categories of one-shot game actions (distinct from MTG keyword abilities).

    What MTG players call "effects" (Flying, Deathtouch, etc.) are keyword abilities.
    These enum values describe what a resolved ability *does* — deal damage, draw cards,
    create tokens, etc. Renamed from EffectType to clarify this distinction.
    """
    DAMAGE = auto()
    LIFE_GAIN = auto()
    LIFE_LOSS = auto()
    DRAW = auto()
    DISCARD = auto()
    MILL = auto()
    DESTROY = auto()
    EXILE = auto()
    BOUNCE = auto()  # Return to hand
    SACRIFICE = auto()
    COUNTER = auto()  # Counter a spell
    CREATE_TOKEN = auto()
    ADD_COUNTER = auto()
    REMOVE_COUNTER = auto()
    TAP = auto()
    UNTAP = auto()
    PUMP = auto()  # +X/+X until end of turn
    TUTOR = auto()  # Search library
    REANIMATE = auto()  # Return from graveyard
    COPY = auto()
    FIGHT = auto()
    EXCHANGE = auto()  # Exchange control, life totals, etc.
    TRANSFORM = auto()
    PHASE = auto()  # Phase in/out
    PROTECTION = auto()  # Gain protection
    COMPLEX = auto()  # Needs Claude

# Backward-compatible alias — all existing `from rules.effects import EffectType` still works
EffectType = ActionType


@dataclass
class Effect:
    """Represents a parsed effect ready for execution."""
    effect_type: EffectType
    
    # Common parameters
    amount: int = 0
    target_type: str = ""  # "creature", "player", "permanent", etc.
    
    # For damage/life
    source_name: str = ""
    
    # For tokens
    token_power: int = 0
    token_toughness: int = 0
    token_types: List[str] = field(default_factory=list)
    token_keywords: List[str] = field(default_factory=list)
    token_colors: List[str] = field(default_factory=list)
    
    # For counters
    counter_type: str = "+1/+1"
    
    # For pump effects
    power_mod: int = 0
    toughness_mod: int = 0
    keywords_granted: List[str] = field(default_factory=list)
    duration: str = "end_of_turn"  # or "permanent"
    
    # For complex effects
    raw_text: str = ""
    
    # Conditions
    condition: Optional[str] = None  # "if you control a Goblin"
    
    # Whether this is optional ("you may")
    optional: bool = False
    
    # For multi-part effects
    additional_effects: List['Effect'] = field(default_factory=list)


@dataclass
class ExecutionContext:
    """Context for executing an effect."""
    game_state: Any  # GameState from mtg_game.py
    source_card: Any  # Card causing the effect
    source_controller: Any  # Player who controls the source
    targets: List[Any] = field(default_factory=list)  # Selected targets
    x_value: int = 0  # For X spells
    
    # For tracking during execution
    damage_dealt: int = 0
    life_gained: int = 0
    cards_drawn: int = 0
    
    # Callbacks for game state updates
    on_damage: Optional[Callable] = None
    on_life_change: Optional[Callable] = None
    on_zone_change: Optional[Callable] = None
    on_counter_change: Optional[Callable] = None


class EffectExecutor:
    """
    Executes effects and updates game state.
    
    Usage:
        executor = EffectExecutor()
        
        # Parse effect from card text
        effects = executor.parse_effects("Deal 3 damage to any target")
        
        # Execute with context
        results = await executor.execute(effects, context)
    """
    
    def __init__(self, claude_client=None):
        self.claude_client = claude_client
        self._build_patterns()
    
    def _build_patterns(self):
        """Build regex patterns for effect parsing."""
        self.patterns = [
            # Damage
            (r"deals? (\d+) damage to (any target|target [\w\s]+|each [\w\s]+|all [\w\s]+)",
             self._parse_damage),
            (r"deals? damage equal to (its power|[\w\s]+) to",
             self._parse_variable_damage),
            
            # Life gain/loss
            (r"gains? (\d+) life", self._parse_life_gain),
            (r"loses? (\d+) life", self._parse_life_loss),
            (r"you gain life equal to", self._parse_variable_life_gain),
            
            # Draw/Discard
            (r"draws? (\d+) cards?", self._parse_draw),
            (r"draw a card", lambda m: Effect(EffectType.DRAW, amount=1)),
            (r"discards? (\d+) cards?", self._parse_discard),
            (r"discard a card", lambda m: Effect(EffectType.DISCARD, amount=1)),
            (r"discards? their hand", lambda m: Effect(EffectType.DISCARD, amount=-1, raw_text="all")),
            
            # Mill
            (r"mills? (\d+) cards?", self._parse_mill),
            (r"puts? the top (\d+) cards? .* into .* graveyard", self._parse_mill),
            
            # Destroy
            (r"destroys? (target [\w\s]+|all [\w\s]+|each [\w\s]+)",
             self._parse_destroy),
            
            # Exile
            (r"exiles? (target [\w\s]+|all [\w\s]+|it)", self._parse_exile),
            
            # Bounce
            (r"returns? (target [\w\s]+|it|all [\w\s]+) to (its|their) owners?' hands?",
             self._parse_bounce),
            
            # Sacrifice
            (r"sacrifices? (a|an|target|all) ([\w\s]+)", self._parse_sacrifice),
            
            # Counter spell
            (r"counters? (target [\w\s]+|that spell)", self._parse_counter),
            
            # Tokens
            (r"creates? (\d+|a|an) ([\d/]+)? ?([\w\s]+) (creature |artifact )tokens?",
             self._parse_create_token),
            
            # Counters
            (r"puts? (\d+|a|an) ([\+\-]?\d+/[\+\-]?\d+|[\w]+) counters? on",
             self._parse_add_counter),
            (r"removes? (\d+|a|an|all) ([\+\-]?\d+/[\+\-]?\d+|[\w]+) counters? from",
             self._parse_remove_counter),
            
            # Tap/Untap
            (r"taps? (target [\w\s]+|it|all [\w\s]+)", self._parse_tap),
            (r"untaps? (target [\w\s]+|it|all [\w\s]+)", self._parse_untap),
            
            # Pump
            (r"gets? ([\+\-]\d+)/([\+\-]\d+) until end of turn",
             self._parse_pump),
            (r"([\+\-]\d+)/([\+\-]\d+) and gains? ([\w\s,]+) until end of turn",
             self._parse_pump_with_keywords),
            
            # Tutor
            (r"searchs? (your|their) library for (a|an) ([\w\s]+)",
             self._parse_tutor),
            
            # Reanimate
            (r"returns? (target [\w\s]+|a [\w\s]+) from (a|your|their) graveyard to the battlefield",
             self._parse_reanimate),
            
            # Fight
            (r"fights? (target [\w\s]+|another target creature)",
             self._parse_fight),
        ]
    
    # =========================================================================
    # PARSING
    # =========================================================================
    
    def parse_effects(self, text: str) -> List[Effect]:
        """Parse card text into Effect objects."""
        text = text.lower()
        # May 7 audit fix #9: strip parenthetical reminder text before
        # pattern matching. Dread Return's flashback reminder ("(...Then
        # exile it.)") was matching the exile pattern and causing the
        # reanimation target to be exiled instead of returned to play.
        # Other flashback/cycling/madness reminder text would trigger the
        # same false-positive.
        text = re.sub(r'\([^)]*\)', '', text)
        effects = []

        for pattern, parser in self.patterns:
            for match in re.finditer(pattern, text):
                try:
                    effect = parser(match)
                    if effect:
                        # Check for "you may" (optional)
                        start = max(0, match.start() - 20)
                        prefix = text[start:match.start()]
                        if "you may" in prefix:
                            effect.optional = True
                        
                        effects.append(effect)
                except Exception as e:
                    print(f"Parse error for pattern {pattern}: {e}")
        
        # If no patterns matched, mark as complex
        if not effects and text.strip():
            effects.append(Effect(
                effect_type=EffectType.COMPLEX,
                raw_text=text
            ))
        
        return effects
    
    def _parse_damage(self, match) -> Effect:
        amount = int(match.group(1))
        target_desc = match.group(2)
        return Effect(
            effect_type=EffectType.DAMAGE,
            amount=amount,
            target_type=target_desc
        )
    
    def _parse_variable_damage(self, match) -> Effect:
        return Effect(
            effect_type=EffectType.DAMAGE,
            amount=-1,  # Variable
            raw_text=match.group(0)
        )
    
    def _parse_life_gain(self, match) -> Effect:
        return Effect(effect_type=EffectType.LIFE_GAIN, amount=int(match.group(1)))
    
    def _parse_life_loss(self, match) -> Effect:
        return Effect(effect_type=EffectType.LIFE_LOSS, amount=int(match.group(1)))
    
    def _parse_variable_life_gain(self, match) -> Effect:
        return Effect(effect_type=EffectType.LIFE_GAIN, amount=-1, raw_text=match.group(0))
    
    def _parse_draw(self, match) -> Effect:
        return Effect(effect_type=EffectType.DRAW, amount=int(match.group(1)))
    
    def _parse_discard(self, match) -> Effect:
        return Effect(effect_type=EffectType.DISCARD, amount=int(match.group(1)))
    
    def _parse_mill(self, match) -> Effect:
        return Effect(effect_type=EffectType.MILL, amount=int(match.group(1)))
    
    def _parse_destroy(self, match) -> Effect:
        return Effect(effect_type=EffectType.DESTROY, target_type=match.group(1))
    
    def _parse_exile(self, match) -> Effect:
        return Effect(effect_type=EffectType.EXILE, target_type=match.group(1))
    
    def _parse_bounce(self, match) -> Effect:
        return Effect(effect_type=EffectType.BOUNCE, target_type=match.group(1))
    
    def _parse_sacrifice(self, match) -> Effect:
        return Effect(
            effect_type=EffectType.SACRIFICE,
            amount=1 if match.group(1) in ('a', 'an', 'target') else -1,
            target_type=match.group(2)
        )
    
    def _parse_counter(self, match) -> Effect:
        return Effect(effect_type=EffectType.COUNTER, target_type=match.group(1))
    
    def _parse_create_token(self, match) -> Effect:
        count_str = match.group(1)
        count = 1 if count_str in ('a', 'an') else int(count_str)
        
        pt = match.group(2)
        power, toughness = 0, 0
        if pt and '/' in pt:
            parts = pt.split('/')
            power, toughness = int(parts[0]), int(parts[1])
        
        types = match.group(3).strip().split()
        
        return Effect(
            effect_type=EffectType.CREATE_TOKEN,
            amount=count,
            token_power=power,
            token_toughness=toughness,
            token_types=types
        )
    
    def _parse_add_counter(self, match) -> Effect:
        count_str = match.group(1)
        count = 1 if count_str in ('a', 'an') else int(count_str)
        counter_type = match.group(2)
        
        return Effect(
            effect_type=EffectType.ADD_COUNTER,
            amount=count,
            counter_type=counter_type
        )
    
    def _parse_remove_counter(self, match) -> Effect:
        count_str = match.group(1)
        count = -1 if count_str == 'all' else (1 if count_str in ('a', 'an') else int(count_str))
        counter_type = match.group(2)
        
        return Effect(
            effect_type=EffectType.REMOVE_COUNTER,
            amount=count,
            counter_type=counter_type
        )
    
    def _parse_tap(self, match) -> Effect:
        return Effect(effect_type=EffectType.TAP, target_type=match.group(1))
    
    def _parse_untap(self, match) -> Effect:
        return Effect(effect_type=EffectType.UNTAP, target_type=match.group(1))
    
    def _parse_pump(self, match) -> Effect:
        return Effect(
            effect_type=EffectType.PUMP,
            power_mod=int(match.group(1)),
            toughness_mod=int(match.group(2))
        )
    
    def _parse_pump_with_keywords(self, match) -> Effect:
        keywords = [k.strip() for k in match.group(3).split(',')]
        return Effect(
            effect_type=EffectType.PUMP,
            power_mod=int(match.group(1)),
            toughness_mod=int(match.group(2)),
            keywords_granted=keywords
        )
    
    def _parse_tutor(self, match) -> Effect:
        return Effect(
            effect_type=EffectType.TUTOR,
            target_type=match.group(3)
        )
    
    def _parse_reanimate(self, match) -> Effect:
        return Effect(
            effect_type=EffectType.REANIMATE,
            target_type=match.group(1)
        )
    
    def _parse_fight(self, match) -> Effect:
        return Effect(
            effect_type=EffectType.FIGHT,
            target_type=match.group(1)
        )
    
    # =========================================================================
    # EXECUTION
    # =========================================================================
    
    async def execute(self, effects: List[Effect], ctx: ExecutionContext) -> List[str]:
        """
        Execute a list of effects.
        
        Returns list of messages describing what happened.
        """
        messages = []
        
        for effect in effects:
            if effect.optional:
                # In a real implementation, would prompt the player
                # For now, assume they choose to do it
                pass
            
            result = await self._execute_single(effect, ctx)
            messages.extend(result)
        
        return messages
    
    async def _execute_single(self, effect: Effect, ctx: ExecutionContext) -> List[str]:
        """Execute a single effect."""
        handlers = {
            EffectType.DAMAGE: self._exec_damage,
            EffectType.LIFE_GAIN: self._exec_life_gain,
            EffectType.LIFE_LOSS: self._exec_life_loss,
            EffectType.DRAW: self._exec_draw,
            EffectType.DISCARD: self._exec_discard,
            EffectType.MILL: self._exec_mill,
            EffectType.DESTROY: self._exec_destroy,
            EffectType.EXILE: self._exec_exile,
            EffectType.BOUNCE: self._exec_bounce,
            EffectType.SACRIFICE: self._exec_sacrifice,
            EffectType.COUNTER: self._exec_counter,
            EffectType.CREATE_TOKEN: self._exec_create_token,
            EffectType.ADD_COUNTER: self._exec_add_counter,
            EffectType.REMOVE_COUNTER: self._exec_remove_counter,
            EffectType.TAP: self._exec_tap,
            EffectType.UNTAP: self._exec_untap,
            EffectType.PUMP: self._exec_pump,
            EffectType.TUTOR: self._exec_tutor,
            EffectType.REANIMATE: self._exec_reanimate,
            EffectType.FIGHT: self._exec_fight,
            EffectType.COMPLEX: self._exec_complex,
        }
        
        handler = handlers.get(effect.effect_type, self._exec_complex)
        return await handler(effect, ctx)
    
    async def _exec_damage(self, effect: Effect, ctx: ExecutionContext) -> List[str]:
        """Execute damage effect."""
        messages = []
        amount = effect.amount
        
        # Handle variable damage (X spells, power-based, etc.)
        if amount == -1:
            amount = ctx.x_value if ctx.x_value else 0
        
        for target in ctx.targets:
            if hasattr(target, 'life'):  # Player
                target.life -= amount
                messages.append(f"💥 {ctx.source_card.name} deals {amount} damage to {target.name}")
                ctx.damage_dealt += amount
            elif hasattr(target, 'damage_marked'):  # Creature
                target.damage_marked += amount
                messages.append(f"💥 {ctx.source_card.name} deals {amount} damage to {target.name}")
                ctx.damage_dealt += amount
            elif hasattr(target, 'loyalty_counters'):  # Planeswalker
                target.loyalty_counters -= amount
                messages.append(f"💥 {ctx.source_card.name} deals {amount} damage to {target.name}")
        
        return messages
    
    async def _exec_life_gain(self, effect: Effect, ctx: ExecutionContext) -> List[str]:
        """Execute life gain effect."""
        amount = effect.amount
        if amount == -1:
            # Variable - use context
            amount = ctx.damage_dealt  # Common case: "gain life equal to damage dealt"
        
        ctx.source_controller.life += amount
        ctx.life_gained += amount
        return [f"💚 {ctx.source_controller.name} gains {amount} life"]
    
    async def _exec_life_loss(self, effect: Effect, ctx: ExecutionContext) -> List[str]:
        """Execute life loss effect."""
        messages = []
        for target in ctx.targets:
            if hasattr(target, 'life'):
                target.life -= effect.amount
                messages.append(f"💔 {target.name} loses {effect.amount} life")
        return messages
    
    async def _exec_draw(self, effect: Effect, ctx: ExecutionContext) -> List[str]:
        """Execute draw effect."""
        player = ctx.source_controller
        drawn = []
        
        for _ in range(effect.amount):
            if player.library:
                card = player.library.pop(0)
                player.hand.append(card)
                drawn.append(card.name)
                ctx.cards_drawn += 1
            else:
                # Attempted to draw from empty library - flag for SBA
                player.attempted_draw_from_empty = True
        
        if drawn:
            return [f"🎴 {player.name} draws {len(drawn)} card(s)"]
        return []
    
    async def _exec_discard(self, effect: Effect, ctx: ExecutionContext) -> List[str]:
        """Execute discard effect."""
        messages = []
        
        for target in ctx.targets if ctx.targets else [ctx.source_controller]:
            if not hasattr(target, 'hand'):
                continue
            
            amount = effect.amount
            if amount == -1:  # Discard entire hand
                amount = len(target.hand)
            
            # For now, discard from end of hand (random-ish)
            # In real implementation, player chooses
            discarded = []
            for _ in range(min(amount, len(target.hand))):
                if target.hand:
                    card = target.hand.pop()
                    target.graveyard.append(card)
                    discarded.append(card.name)
            
            if discarded:
                messages.append(f"🗑️ {target.name} discards: {', '.join(discarded)}")
        
        return messages
    
    async def _exec_mill(self, effect: Effect, ctx: ExecutionContext) -> List[str]:
        """Execute mill effect."""
        messages = []
        
        for target in ctx.targets if ctx.targets else [ctx.source_controller]:
            if not hasattr(target, 'library'):
                continue
            
            milled = []
            for _ in range(min(effect.amount, len(target.library))):
                if target.library:
                    card = target.library.pop(0)
                    target.graveyard.append(card)
                    milled.append(card.name)
            
            if milled:
                messages.append(f"📚 {target.name} mills {len(milled)}: {', '.join(milled[:5])}{'...' if len(milled) > 5 else ''}")
        
        return messages
    
    async def _exec_destroy(self, effect: Effect, ctx: ExecutionContext) -> List[str]:
        """Execute destroy effect."""
        messages = []
        
        for target in ctx.targets:
            if not hasattr(target, 'has_indestructible') or not target.has_indestructible:
                # Find owner and move to graveyard
                for player in ctx.game_state.players:
                    if target in player.battlefield:
                        player.battlefield.remove(target)
                        player.graveyard.append(target)
                        messages.append(f"💀 {target.name} is destroyed")
                        break
            else:
                messages.append(f"🛡️ {target.name} is indestructible!")
        
        return messages
    
    async def _exec_exile(self, effect: Effect, ctx: ExecutionContext) -> List[str]:
        """Execute exile effect."""
        messages = []
        
        for target in ctx.targets:
            # Find in any zone and move to exile
            for player in ctx.game_state.players:
                for zone_name in ['battlefield', 'graveyard', 'hand']:
                    zone = getattr(player, zone_name, [])
                    if target in zone:
                        zone.remove(target)
                        player.exile.append(target)
                        messages.append(f"✨ {target.name} is exiled")
                        break
        
        return messages
    
    async def _exec_bounce(self, effect: Effect, ctx: ExecutionContext) -> List[str]:
        """Execute return to hand effect."""
        messages = []
        
        for target in ctx.targets:
            for player in ctx.game_state.players:
                if target in player.battlefield:
                    player.battlefield.remove(target)
                    # Return to owner's hand
                    owner_idx = target.owner_index if hasattr(target, 'owner_index') else 0
                    ctx.game_state.players[owner_idx].hand.append(target)
                    # Reset state
                    target.tapped = False
                    target.summoning_sick = True
                    target.damage_marked = 0
                    target.counters = {}
                    messages.append(f"↩️ {target.name} is returned to hand")
                    break
        
        return messages
    
    async def _exec_sacrifice(self, effect: Effect, ctx: ExecutionContext) -> List[str]:
        """Execute sacrifice effect."""
        messages = []
        
        for target in ctx.targets:
            for player in ctx.game_state.players:
                if target in player.battlefield:
                    player.battlefield.remove(target)
                    player.graveyard.append(target)
                    messages.append(f"🔥 {target.name} is sacrificed")
                    break
        
        return messages
    
    async def _exec_counter(self, effect: Effect, ctx: ExecutionContext) -> List[str]:
        """Execute counter spell effect."""
        messages = []
        
        for target in ctx.targets:
            # Remove from stack
            if hasattr(ctx.game_state, 'stack') and target in ctx.game_state.stack:
                ctx.game_state.stack.remove(target)
                # Move to graveyard
                owner_idx = target.owner_index if hasattr(target, 'owner_index') else 0
                ctx.game_state.players[owner_idx].graveyard.append(target)
                messages.append(f"🚫 {target.name} is countered!")
        
        return messages
    
    async def _exec_create_token(self, effect: Effect, ctx: ExecutionContext) -> List[str]:
        """Execute create token effect."""
        messages = []
        
        for _ in range(effect.amount):
            # Create a token card
            token_name = ' '.join(effect.token_types).title() + " Token"
            
            # This would need to create a Card object
            # For now, just message
            messages.append(
                f"🎭 {ctx.source_controller.name} creates a "
                f"{effect.token_power}/{effect.token_toughness} {token_name}"
            )
        
        return messages
    
    async def _exec_add_counter(self, effect: Effect, ctx: ExecutionContext) -> List[str]:
        """Execute add counter effect."""
        messages = []
        
        for target in ctx.targets:
            if hasattr(target, 'counters'):
                current = target.counters.get(effect.counter_type, 0)
                target.counters[effect.counter_type] = current + effect.amount
                messages.append(f"⭕ {effect.amount} {effect.counter_type} counter(s) added to {target.name}")
        
        return messages
    
    async def _exec_remove_counter(self, effect: Effect, ctx: ExecutionContext) -> List[str]:
        """Execute remove counter effect."""
        messages = []
        
        for target in ctx.targets:
            if hasattr(target, 'counters'):
                current = target.counters.get(effect.counter_type, 0)
                if effect.amount == -1:  # Remove all
                    removed = current
                    target.counters[effect.counter_type] = 0
                else:
                    removed = min(current, effect.amount)
                    target.counters[effect.counter_type] = current - removed
                messages.append(f"⭕ {removed} {effect.counter_type} counter(s) removed from {target.name}")
        
        return messages
    
    async def _exec_tap(self, effect: Effect, ctx: ExecutionContext) -> List[str]:
        """Execute tap effect."""
        messages = []
        
        for target in ctx.targets:
            if hasattr(target, 'tapped'):
                target.tapped = True
                messages.append(f"↪️ {target.name} becomes tapped")
        
        return messages
    
    async def _exec_untap(self, effect: Effect, ctx: ExecutionContext) -> List[str]:
        """Execute untap effect."""
        messages = []
        
        for target in ctx.targets:
            if hasattr(target, 'tapped'):
                target.tapped = False
                messages.append(f"↩️ {target.name} untaps")
        
        return messages
    
    async def _exec_pump(self, effect: Effect, ctx: ExecutionContext) -> List[str]:
        """Execute pump (+X/+X) effect."""
        messages = []
        
        for target in ctx.targets:
            # Add temporary modifiers
            # In real implementation, would track until end of turn
            if hasattr(target, 'power_modifier'):
                target.power_modifier = getattr(target, 'power_modifier', 0) + effect.power_mod
            if hasattr(target, 'toughness_modifier'):
                target.toughness_modifier = getattr(target, 'toughness_modifier', 0) + effect.toughness_mod
            
            pump_str = f"{effect.power_mod:+}/{effect.toughness_mod:+}"
            kw_str = f" and gains {', '.join(effect.keywords_granted)}" if effect.keywords_granted else ""
            messages.append(f"💪 {target.name} gets {pump_str}{kw_str} until end of turn")
        
        return messages
    
    async def _exec_tutor(self, effect: Effect, ctx: ExecutionContext) -> List[str]:
        """Execute tutor (search library) effect."""
        # In real implementation, would show library and let player choose
        return [f"🔍 {ctx.source_controller.name} searches their library for a {effect.target_type}"]
    
    async def _exec_reanimate(self, effect: Effect, ctx: ExecutionContext) -> List[str]:
        """Execute reanimate (return from graveyard) effect."""
        messages = []

        for target in ctx.targets:
            # Bug fix: only reanimate creature cards
            if hasattr(target, 'is_creature') and not target.is_creature():
                messages.append(f"⚠️ Cannot reanimate {target.name} — not a creature card")
                continue
            for player in ctx.game_state.players:
                if target in player.graveyard:
                    player.graveyard.remove(target)
                    ctx.source_controller.battlefield.append(target)
                    target.summoning_sick = True
                    target.tapped = False
                    target.damage_marked = 0
                    messages.append(f"⬆️ {target.name} returns to the battlefield")
                    break
        
        return messages
    
    async def _exec_fight(self, effect: Effect, ctx: ExecutionContext) -> List[str]:
        """Execute fight effect."""
        messages = []
        
        # Need two creatures - source and target
        if len(ctx.targets) >= 1:
            fighter1 = ctx.source_card
            fighter2 = ctx.targets[0]
            
            try:
                p1 = int(fighter1.power) if fighter1.power else 0
                p2 = int(fighter2.power) if fighter2.power else 0
            except (ValueError, TypeError):
                p1, p2 = 0, 0
            
            fighter1.damage_marked += p2
            fighter2.damage_marked += p1
            
            messages.append(f"⚔️ {fighter1.name} and {fighter2.name} fight!")
            messages.append(f"   {fighter1.name} deals {p1} damage to {fighter2.name}")
            messages.append(f"   {fighter2.name} deals {p2} damage to {fighter1.name}")
        
        return messages
    
    async def _exec_complex(self, effect: Effect, ctx: ExecutionContext) -> List[str]:
        """Handle complex effects via Claude."""
        if not self.claude_client:
            return [f"⚠️ {effect.raw_text} _(complex effect, manual resolution needed)_"]
        
        # Ask Claude to interpret and execute
        prompt = f"""You are resolving a Magic: The Gathering effect.

EFFECT TEXT: {effect.raw_text}

SOURCE: {ctx.source_card.name}
CONTROLLER: {ctx.source_controller.name}
TARGETS: {[t.name if hasattr(t, 'name') else str(t) for t in ctx.targets]}

What game state changes should occur? Be specific about:
1. What moves where
2. What values change
3. Any choices players need to make

Respond in a concise, bullet-point format."""

        try:
            response = await asyncio.to_thread(
                self.claude_client.messages.create,
                model="claude-sonnet-4-6",
                max_tokens=500,
                messages=[{"role": "user", "content": prompt}]
            )
            return [f"🧙 Claude interprets: {response.content[0].text}"]
        except Exception as e:
            return [f"⚠️ Error resolving effect: {e}"]


# =============================================================================
# CONVENIENCE FUNCTIONS
# =============================================================================

def parse_card_effects(oracle_text: str) -> List[Effect]:
    """Quick function to parse effects from oracle text."""
    executor = EffectExecutor()
    return executor.parse_effects(oracle_text)


# =============================================================================
# DEMO / TEST
# =============================================================================

def demo():
    """Demo the effect executor."""
    print("=== Effect Executor Demo ===\n")
    
    executor = EffectExecutor()
    
    test_cards = [
        ("Lightning Bolt", "Lightning Bolt deals 3 damage to any target."),
        ("Ancestral Recall", "Target player draws 3 cards."),
        ("Doom Blade", "Destroy target nonblack creature."),
        ("Swords to Plowshares", "Exile target creature. Its controller gains life equal to its power."),
        ("Giant Growth", "Target creature gets +3/+3 until end of turn."),
        ("Counterspell", "Counter target spell."),
        ("Dark Ritual", "Add {B}{B}{B}."),  # Won't parse (mana ability)
        ("Thoughtseize", "Target player reveals their hand. You choose a nonland card from it. That player discards that card. You lose 2 life."),
        ("Wrath of God", "Destroy all creatures. They can't be regenerated."),
        ("Raise Dead", "Return target creature card from your graveyard to your hand."),
        ("Llanowar Elves", "Tap: Add {G}."),  # Won't parse (mana ability)
        ("Soul Warden", "Whenever another creature enters the battlefield, you gain 1 life."),
        ("Murder", "Destroy target creature."),
        ("Opt", "Scry 1. Draw a card."),
        ("Lingering Souls", "Create two 1/1 white Spirit creature tokens with flying."),
    ]
    
    for name, text in test_cards:
        effects = executor.parse_effects(text)
        print(f"**{name}**")
        print(f"  Text: {text}")
        if effects:
            for e in effects:
                if e.effect_type == EffectType.COMPLEX:
                    print(f"  → COMPLEX (needs Claude)")
                else:
                    print(f"  → {e.effect_type.name}: amount={e.amount}, target={e.target_type or 'N/A'}")
        else:
            print(f"  → (no effects parsed)")
        print()
    
    print("=== Demo Complete ===")


if __name__ == "__main__":
    demo()
