"""
MTG State-Based Actions (SBAs)
==============================

State-based actions are checked continuously and don't use the stack.
They're checked before a player receives priority and after a spell/ability resolves.

SBAs are processed repeatedly until none apply, then triggers go on stack.

Key SBAs (CR 704.5):
- Player at 0 or less life loses (704.5a)
- Player with 10+ poison counters loses (704.5c)
- Player who attempted to draw from empty library loses (704.5b)
- Creature with 0 or less toughness dies (704.5f)
- Creature with lethal damage dies (704.5g)
- Creature with damage from deathtouch source dies (704.5h)
- Planeswalker with 0 loyalty is put into graveyard (704.5i)
- Legend rule (704.5j)
- World rule (704.5k) - rare
- Aura/Equipment not attached correctly (704.5m, 704.5n)
- +1/+1 and -1/-1 counters cancel (704.5q)
- Saga with chapters complete is sacrificed (704.5s)
"""

from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Dict, List, Optional, Set, Tuple, Callable, Any
from collections import defaultdict


class SBAType(Enum):
    """Types of state-based actions."""
    PLAYER_LOSES_ZERO_LIFE = auto()
    PLAYER_LOSES_POISON = auto()
    PLAYER_LOSES_DRAW_EMPTY = auto()
    # Commander damage (21+) — game-specific, lives inline in mtg/sba.py
    # because the rules-module Player dataclass doesn't carry the per-source
    # commander_damage counter. This enum exists so the inline check can
    # tag its dispatch log under a stable name; auditors grep for it.
    PLAYER_LOSES_COMMANDER_DAMAGE = auto()
    CREATURE_ZERO_TOUGHNESS = auto()
    CREATURE_LETHAL_DAMAGE = auto()
    CREATURE_DEATHTOUCH = auto()
    PLANESWALKER_ZERO_LOYALTY = auto()
    LEGEND_RULE = auto()
    WORLD_RULE = auto()
    AURA_INVALID = auto()
    EQUIPMENT_INVALID = auto()
    TOKEN_WRONG_ZONE = auto()
    COPY_WRONG_ZONE = auto()
    COUNTERS_CANCEL = auto()
    SAGA_COMPLETE = auto()
    BATTLE_ZERO_DEFENSE = auto()


@dataclass
class SBAResult:
    """Result of a state-based action."""
    sba_type: SBAType
    description: str
    affected_objects: List[str]  # Object IDs or player names
    choices_required: Optional[Dict[str, List[str]]] = None  # player -> choices
    
    def __str__(self) -> str:
        return f"{self.sba_type.name}: {self.description}"


@dataclass
class Permanent:
    """Represents a permanent on the battlefield."""
    id: str
    name: str
    controller: str
    owner: str
    
    # Types
    is_creature: bool = False
    is_planeswalker: bool = False
    is_artifact: bool = False
    is_enchantment: bool = False
    is_land: bool = False
    is_battle: bool = False
    
    # Supertypes
    is_legendary: bool = False
    is_world: bool = False
    is_snow: bool = False
    is_basic: bool = False
    
    # Subtypes
    subtypes: List[str] = field(default_factory=list)
    is_aura: bool = False
    is_equipment: bool = False
    is_saga: bool = False
    
    # Creature stats
    power: int = 0
    toughness: int = 0
    base_power: int = 0
    base_toughness: int = 0
    
    # Modifiers
    power_modifier: int = 0
    toughness_modifier: int = 0
    
    # Counters
    plus_counters: int = 0  # +1/+1 counters
    minus_counters: int = 0  # -1/-1 counters
    loyalty_counters: int = 0
    lore_counters: int = 0  # For sagas
    defense_counters: int = 0  # For battles
    other_counters: Dict[str, int] = field(default_factory=dict)
    
    # Combat/Damage
    damage_marked: int = 0
    damage_sources: List[str] = field(default_factory=list)  # IDs of sources
    deathtouch_damage: int = 0  # Damage from deathtouch sources
    
    # Attachment
    attached_to: Optional[str] = None  # ID of permanent this is attached to
    attachments: List[str] = field(default_factory=list)  # IDs of attached permanents
    
    # For Auras
    enchanting: Optional[str] = None  # What this aura enchants
    enchant_restriction: Optional[str] = None  # e.g., "creature", "land"
    
    # Token/Copy status
    is_token: bool = False
    is_copy: bool = False
    
    # Saga chapter tracking
    saga_chapters: int = 0  # Total chapters
    
    # Keywords (relevant for SBAs)
    has_indestructible: bool = False
    
    @property
    def effective_power(self) -> int:
        return self.base_power + self.power_modifier + self.plus_counters - self.minus_counters
    
    @property
    def effective_toughness(self) -> int:
        return self.base_toughness + self.toughness_modifier + self.plus_counters - self.minus_counters
    
    @property
    def effective_loyalty(self) -> int:
        return self.loyalty_counters
    
    @property
    def effective_defense(self) -> int:
        return self.defense_counters


@dataclass
class Player:
    """Represents a player in the game."""
    id: str
    name: str
    life: int = 20
    poison_counters: int = 0
    attempted_draw_from_empty: bool = False
    has_lost: bool = False
    has_won: bool = False


@dataclass
class GameState:
    """The complete game state for SBA checking."""
    players: Dict[str, Player] = field(default_factory=dict)
    battlefield: Dict[str, Permanent] = field(default_factory=dict)
    
    # Zone tracking for tokens/copies
    graveyard: Dict[str, List[str]] = field(default_factory=dict)  # player -> card names
    exile: List[str] = field(default_factory=list)
    hand: Dict[str, List[str]] = field(default_factory=dict)
    library: Dict[str, int] = field(default_factory=dict)  # player -> card count
    
    # Tracking for triggers
    died_this_turn: List[str] = field(default_factory=list)
    entered_this_turn: List[str] = field(default_factory=list)
    
    def get_permanents_by_name(self, name: str) -> List[Permanent]:
        """Get all permanents with a given name."""
        return [p for p in self.battlefield.values() if p.name == name]
    
    def get_permanents_by_controller(self, controller: str) -> List[Permanent]:
        """Get all permanents controlled by a player."""
        return [p for p in self.battlefield.values() if p.controller == controller]
    
    def get_legendary_permanents(self, controller: str) -> Dict[str, List[Permanent]]:
        """Get legendary permanents grouped by name for legend rule."""
        legends: Dict[str, List[Permanent]] = defaultdict(list)
        for p in self.battlefield.values():
            if p.is_legendary and p.controller == controller:
                legends[p.name].append(p)
        return {k: v for k, v in legends.items() if len(v) > 1}


class StateBasedActionChecker:
    """
    Checks and processes state-based actions.
    
    Usage:
        checker = StateBasedActionChecker()
        while True:
            results = checker.check(game_state)
            if not results:
                break
            for result in results:
                process_sba(result, game_state)
    """
    
    def __init__(self):
        # Callbacks for when SBAs happen
        self.on_creature_dies: Optional[Callable[[Permanent, str], None]] = None
        self.on_player_loses: Optional[Callable[[Player, str], None]] = None
    
    def check(self, state: GameState) -> List[SBAResult]:
        """
        Check all state-based actions.
        
        Returns list of SBAs that need to be performed.
        """
        results = []
        
        # Player-based SBAs
        results.extend(self._check_player_life(state))
        results.extend(self._check_player_poison(state))
        results.extend(self._check_player_draw_empty(state))
        
        # Creature-based SBAs
        results.extend(self._check_creature_toughness(state))
        results.extend(self._check_creature_damage(state))
        
        # Planeswalker SBAs
        results.extend(self._check_planeswalker_loyalty(state))
        
        # Legend/World rules
        results.extend(self._check_legend_rule(state))
        results.extend(self._check_world_rule(state))
        
        # Attachment SBAs
        results.extend(self._check_auras(state))
        results.extend(self._check_equipment(state))
        
        # Counter SBAs
        results.extend(self._check_counter_cancellation(state))
        
        # Saga SBAs
        results.extend(self._check_sagas(state))
        
        # Battle SBAs
        results.extend(self._check_battles(state))
        
        # Token/Copy in wrong zones
        results.extend(self._check_tokens_and_copies(state))
        
        return results
    
    def check_and_apply(self, state: GameState) -> Tuple[List[SBAResult], List[str]]:
        """
        Check SBAs and apply them to the game state.
        
        Returns (all_results, triggered_abilities).
        """
        all_results = []
        triggered_abilities = []
        
        # Keep checking until no more SBAs
        while True:
            results = self.check(state)
            if not results:
                break
            
            all_results.extend(results)
            
            for result in results:
                triggers = self._apply_sba(result, state)
                triggered_abilities.extend(triggers)
        
        return all_results, triggered_abilities
    
    # =========================================================================
    # Individual SBA Checks
    # =========================================================================
    
    def _check_player_life(self, state: GameState) -> List[SBAResult]:
        """704.5a - Player at 0 or less life loses."""
        results = []
        
        for player in state.players.values():
            if player.life <= 0 and not player.has_lost:
                # Display-clamp: CR 119.3 allows life to go negative, but the
                # loss-reason string is user-facing — show 0 (the loss threshold)
                # rather than e.g. "-13 life". State stays untouched.
                results.append(SBAResult(
                    sba_type=SBAType.PLAYER_LOSES_ZERO_LIFE,
                    description=f"{player.name} has {max(0, player.life)} life",
                    affected_objects=[player.id]
                ))
        
        return results
    
    def _check_player_poison(self, state: GameState) -> List[SBAResult]:
        """704.5c - Player with 10+ poison counters loses."""
        results = []
        
        for player in state.players.values():
            if player.poison_counters >= 10 and not player.has_lost:
                results.append(SBAResult(
                    sba_type=SBAType.PLAYER_LOSES_POISON,
                    description=f"{player.name} has {player.poison_counters} poison counters",
                    affected_objects=[player.id]
                ))
        
        return results
    
    def _check_player_draw_empty(self, state: GameState) -> List[SBAResult]:
        """704.5b - Player who tried to draw from empty library loses."""
        results = []
        
        for player in state.players.values():
            if player.attempted_draw_from_empty and not player.has_lost:
                results.append(SBAResult(
                    sba_type=SBAType.PLAYER_LOSES_DRAW_EMPTY,
                    description=f"{player.name} attempted to draw from empty library",
                    affected_objects=[player.id]
                ))
        
        return results
    
    def _check_creature_toughness(self, state: GameState) -> List[SBAResult]:
        """704.5f - Creature with 0 or less toughness dies.

        Note: Indestructible does NOT prevent this -- a creature with 0 toughness
        is put into the graveyard regardless of indestructible (CR 704.5f).
        Indestructible only prevents destruction from damage (704.5g) and
        "destroy" effects, not zero-toughness SBA.
        """
        results = []

        for perm in state.battlefield.values():
            if perm.is_creature and perm.effective_toughness <= 0:
                # No indestructible check here -- 0 toughness kills even indestructible creatures
                results.append(SBAResult(
                    sba_type=SBAType.CREATURE_ZERO_TOUGHNESS,
                    description=f"{perm.name} has {perm.effective_toughness} toughness",
                    affected_objects=[perm.id]
                ))

        return results
    
    def _check_creature_damage(self, state: GameState) -> List[SBAResult]:
        """704.5g/h - Creature with lethal damage or deathtouch damage dies."""
        results = []
        
        for perm in state.battlefield.values():
            if not perm.is_creature:
                continue
            
            if perm.has_indestructible:
                continue
            
            # Deathtouch - any damage is lethal
            if perm.deathtouch_damage > 0:
                results.append(SBAResult(
                    sba_type=SBAType.CREATURE_DEATHTOUCH,
                    description=f"{perm.name} was dealt damage by a source with deathtouch",
                    affected_objects=[perm.id]
                ))
            # Normal lethal damage
            elif perm.damage_marked >= perm.effective_toughness:
                results.append(SBAResult(
                    sba_type=SBAType.CREATURE_LETHAL_DAMAGE,
                    description=f"{perm.name} has {perm.damage_marked} damage (toughness {perm.effective_toughness})",
                    affected_objects=[perm.id]
                ))
        
        return results
    
    def _check_planeswalker_loyalty(self, state: GameState) -> List[SBAResult]:
        """704.5i - Planeswalker with 0 loyalty is put into graveyard."""
        results = []
        
        for perm in state.battlefield.values():
            if perm.is_planeswalker and perm.effective_loyalty <= 0:
                results.append(SBAResult(
                    sba_type=SBAType.PLANESWALKER_ZERO_LOYALTY,
                    description=f"{perm.name} has {perm.effective_loyalty} loyalty",
                    affected_objects=[perm.id]
                ))
        
        return results
    
    def _check_legend_rule(self, state: GameState) -> List[SBAResult]:
        """704.5j - If a player controls two+ legendaries with same name, choose one to keep."""
        results = []
        
        for player_id in state.players:
            legends = state.get_legendary_permanents(player_id)
            
            for name, permanents in legends.items():
                if len(permanents) > 1:
                    results.append(SBAResult(
                        sba_type=SBAType.LEGEND_RULE,
                        description=f"{player_id} controls multiple {name}",
                        affected_objects=[p.id for p in permanents],
                        choices_required={player_id: [p.id for p in permanents]}
                    ))
        
        return results
    
    def _check_world_rule(self, state: GameState) -> List[SBAResult]:
        """704.5k - If two+ World permanents exist, all but newest are put into graveyard."""
        results = []
        
        worlds = [p for p in state.battlefield.values() if p.is_world]
        
        if len(worlds) > 1:
            # Sort by timestamp (approximated by ID for now)
            worlds.sort(key=lambda p: p.id, reverse=True)
            newest = worlds[0]
            to_destroy = worlds[1:]
            
            results.append(SBAResult(
                sba_type=SBAType.WORLD_RULE,
                description=f"Multiple World permanents exist, keeping {newest.name}",
                affected_objects=[p.id for p in to_destroy]
            ))
        
        return results
    
    def _check_auras(self, state: GameState) -> List[SBAResult]:
        """704.5m - Aura attached to invalid object goes to graveyard."""
        results = []
        
        for perm in state.battlefield.values():
            if not perm.is_aura:
                continue
            
            # Check if attached to something
            if perm.enchanting is None:
                results.append(SBAResult(
                    sba_type=SBAType.AURA_INVALID,
                    description=f"{perm.name} is not attached to anything",
                    affected_objects=[perm.id]
                ))
                continue
            
            # Check if target still exists
            target = state.battlefield.get(perm.enchanting)
            if target is None:
                results.append(SBAResult(
                    sba_type=SBAType.AURA_INVALID,
                    description=f"{perm.name}'s enchanted object no longer exists",
                    affected_objects=[perm.id]
                ))
                continue
            
            # Check if target still matches restriction (simplified)
            if perm.enchant_restriction:
                valid = self._check_enchant_restriction(perm.enchant_restriction, target)
                if not valid:
                    results.append(SBAResult(
                        sba_type=SBAType.AURA_INVALID,
                        description=f"{perm.name} can't enchant {target.name}",
                        affected_objects=[perm.id]
                    ))
        
        return results
    
    def _check_enchant_restriction(self, restriction: str, target: Permanent) -> bool:
        """Check if a target matches an enchant restriction."""
        restriction = restriction.lower()
        
        if restriction == "creature":
            return target.is_creature
        elif restriction == "land":
            return target.is_land
        elif restriction == "artifact":
            return target.is_artifact
        elif restriction == "enchantment":
            return target.is_enchantment
        elif restriction == "planeswalker":
            return target.is_planeswalker
        
        # More complex restrictions would need parsing
        return True
    
    def _check_equipment(self, state: GameState) -> List[SBAResult]:
        """704.5n - Equipment attached to non-creature becomes unattached."""
        results = []
        
        for perm in state.battlefield.values():
            if not perm.is_equipment or perm.attached_to is None:
                continue
            
            target = state.battlefield.get(perm.attached_to)
            
            # Equipment falls off if target doesn't exist or isn't a creature
            if target is None or not target.is_creature:
                results.append(SBAResult(
                    sba_type=SBAType.EQUIPMENT_INVALID,
                    description=f"{perm.name} is attached to an invalid target",
                    affected_objects=[perm.id]
                ))
        
        return results
    
    def _check_counter_cancellation(self, state: GameState) -> List[SBAResult]:
        """704.5q - +1/+1 and -1/-1 counters cancel out."""
        results = []
        
        for perm in state.battlefield.values():
            if perm.plus_counters > 0 and perm.minus_counters > 0:
                cancel_amount = min(perm.plus_counters, perm.minus_counters)
                results.append(SBAResult(
                    sba_type=SBAType.COUNTERS_CANCEL,
                    description=f"{cancel_amount} +1/+1 and -1/-1 counters on {perm.name} cancel",
                    affected_objects=[perm.id]
                ))
        
        return results
    
    def _check_sagas(self, state: GameState) -> List[SBAResult]:
        """704.5s - Saga with final chapter counter is sacrificed."""
        results = []
        
        for perm in state.battlefield.values():
            if perm.is_saga and perm.lore_counters >= perm.saga_chapters:
                results.append(SBAResult(
                    sba_type=SBAType.SAGA_COMPLETE,
                    description=f"{perm.name} has reached its final chapter",
                    affected_objects=[perm.id]
                ))
        
        return results
    
    def _check_battles(self, state: GameState) -> List[SBAResult]:
        """704.5t - Battle with 0 defense counters is put into graveyard."""
        results = []
        
        for perm in state.battlefield.values():
            if perm.is_battle and perm.effective_defense <= 0:
                results.append(SBAResult(
                    sba_type=SBAType.BATTLE_ZERO_DEFENSE,
                    description=f"{perm.name} has no defense counters",
                    affected_objects=[perm.id]
                ))
        
        return results
    
    def _check_tokens_and_copies(self, state: GameState) -> List[SBAResult]:
        """704.5d/e - Tokens/copies in wrong zones cease to exist."""
        results = []
        
        # This would check graveyard, exile, hand, library for tokens/copies
        # Since we're tracking by ID, we'd need additional tracking
        # For now, this is a placeholder
        
        return results
    
    # =========================================================================
    # Apply SBAs
    # =========================================================================
    
    def _apply_sba(self, result: SBAResult, state: GameState) -> List[str]:
        """
        Apply a single SBA to the game state.
        
        Returns list of triggered ability descriptions.
        """
        triggers = []
        
        if result.sba_type in (SBAType.PLAYER_LOSES_ZERO_LIFE, 
                               SBAType.PLAYER_LOSES_POISON,
                               SBAType.PLAYER_LOSES_DRAW_EMPTY):
            for player_id in result.affected_objects:
                player = state.players.get(player_id)
                if player:
                    player.has_lost = True
                    if self.on_player_loses:
                        self.on_player_loses(player, result.description)
        
        elif result.sba_type in (SBAType.CREATURE_ZERO_TOUGHNESS,
                                  SBAType.CREATURE_LETHAL_DAMAGE,
                                  SBAType.CREATURE_DEATHTOUCH):
            for perm_id in result.affected_objects:
                perm = state.battlefield.pop(perm_id, None)
                if perm:
                    # Add to graveyard
                    if perm.owner not in state.graveyard:
                        state.graveyard[perm.owner] = []
                    if not perm.is_token:
                        state.graveyard[perm.owner].append(perm.name)
                    
                    # Track for "died this turn" triggers
                    state.died_this_turn.append(perm.name)
                    
                    # Generate death triggers
                    triggers.append(f"{perm.name} dies")
                    
                    if self.on_creature_dies:
                        self.on_creature_dies(perm, result.description)
        
        elif result.sba_type == SBAType.PLANESWALKER_ZERO_LOYALTY:
            for perm_id in result.affected_objects:
                perm = state.battlefield.pop(perm_id, None)
                if perm:
                    if perm.owner not in state.graveyard:
                        state.graveyard[perm.owner] = []
                    state.graveyard[perm.owner].append(perm.name)
        
        elif result.sba_type == SBAType.LEGEND_RULE:
            # This requires player choice - mark all but don't remove yet
            # The caller should handle the choice
            pass
        
        elif result.sba_type == SBAType.AURA_INVALID:
            for perm_id in result.affected_objects:
                perm = state.battlefield.pop(perm_id, None)
                if perm:
                    if perm.owner not in state.graveyard:
                        state.graveyard[perm.owner] = []
                    state.graveyard[perm.owner].append(perm.name)
        
        elif result.sba_type == SBAType.EQUIPMENT_INVALID:
            for perm_id in result.affected_objects:
                perm = state.battlefield.get(perm_id)
                if perm:
                    perm.attached_to = None
        
        elif result.sba_type == SBAType.COUNTERS_CANCEL:
            for perm_id in result.affected_objects:
                perm = state.battlefield.get(perm_id)
                if perm:
                    cancel = min(perm.plus_counters, perm.minus_counters)
                    perm.plus_counters -= cancel
                    perm.minus_counters -= cancel
        
        elif result.sba_type == SBAType.SAGA_COMPLETE:
            for perm_id in result.affected_objects:
                perm = state.battlefield.pop(perm_id, None)
                if perm:
                    if perm.owner not in state.graveyard:
                        state.graveyard[perm.owner] = []
                    state.graveyard[perm.owner].append(perm.name)
        
        return triggers


# =============================================================================
# Demo / Test
# =============================================================================

def demo():
    """Demo the SBA system."""
    print("=== State-Based Actions Demo ===\n")
    
    checker = StateBasedActionChecker()
    
    # Set up callback
    def on_death(perm: Permanent, reason: str):
        print(f"  💀 {perm.name} died: {reason}")
    
    checker.on_creature_dies = on_death
    
    # Create game state
    state = GameState()
    state.players = {
        "Alice": Player("Alice", "Alice", life=20),
        "Bob": Player("Bob", "Bob", life=3)
    }
    
    # Add some permanents
    state.battlefield = {
        "p1": Permanent("p1", "Grizzly Bears", "Alice", "Alice", 
                       is_creature=True, base_power=2, base_toughness=2),
        "p2": Permanent("p2", "Llanowar Elves", "Alice", "Alice",
                       is_creature=True, base_power=1, base_toughness=1,
                       damage_marked=3),  # Lethal damage!
        "p3": Permanent("p3", "Birds of Paradise", "Bob", "Bob",
                       is_creature=True, base_power=0, base_toughness=1,
                       minus_counters=2),  # 0-1 = -1 toughness!
        "p4": Permanent("p4", "Jace, the Mind Sculptor", "Alice", "Alice",
                       is_planeswalker=True, loyalty_counters=0),  # 0 loyalty!
    }
    
    print("Initial state:")
    print(f"  Alice: {state.players['Alice'].life} life")
    print(f"  Bob: {state.players['Bob'].life} life")
    print(f"  Battlefield: {[p.name for p in state.battlefield.values()]}")
    
    print("\nChecking SBAs...")
    results = checker.check(state)
    
    print(f"\nFound {len(results)} SBAs:")
    for r in results:
        print(f"  - {r}")
    
    print("\nApplying SBAs...")
    all_results, triggers = checker.check_and_apply(state)
    
    print(f"\nAfter SBAs:")
    print(f"  Battlefield: {[p.name for p in state.battlefield.values()]}")
    print(f"  Alice's graveyard: {state.graveyard.get('Alice', [])}")
    print(f"  Bob's graveyard: {state.graveyard.get('Bob', [])}")
    print(f"  Triggers: {triggers}")
    
    # Test legend rule
    print("\n\n--- Legend Rule Test ---")
    state2 = GameState()
    state2.players = {"Alice": Player("Alice", "Alice")}
    state2.battlefield = {
        "l1": Permanent("l1", "Jace Beleren", "Alice", "Alice", 
                       is_planeswalker=True, is_legendary=True, loyalty_counters=3),
        "l2": Permanent("l2", "Jace Beleren", "Alice", "Alice",
                       is_planeswalker=True, is_legendary=True, loyalty_counters=2),
    }
    
    results = checker.check(state2)
    print(f"Legend rule check: {results}")
    if results and results[0].choices_required:
        print(f"  Player must choose from: {results[0].choices_required}")
    
    # Test +1/+1 and -1/-1 cancellation
    print("\n\n--- Counter Cancellation Test ---")
    state3 = GameState()
    state3.players = {"Alice": Player("Alice", "Alice")}
    state3.battlefield = {
        "c1": Permanent("c1", "Kitchen Finks", "Alice", "Alice",
                       is_creature=True, base_power=3, base_toughness=2,
                       plus_counters=2, minus_counters=1),  # Should cancel 1 of each
    }
    
    finks = state3.battlefield["c1"]
    print(f"Before: {finks.name} has {finks.plus_counters} +1/+1 and {finks.minus_counters} -1/-1")
    print(f"  Effective P/T: {finks.effective_power}/{finks.effective_toughness}")
    
    results, _ = checker.check_and_apply(state3)
    
    finks = state3.battlefield["c1"]
    print(f"After: {finks.name} has {finks.plus_counters} +1/+1 and {finks.minus_counters} -1/-1")
    print(f"  Effective P/T: {finks.effective_power}/{finks.effective_toughness}")
    
    print("\n=== Demo Complete ===")


if __name__ == "__main__":
    demo()
