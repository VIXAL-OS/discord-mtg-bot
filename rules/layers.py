"""
MTG Layers System
==================

The seven-layer system for applying continuous effects.

When multiple continuous effects modify the same thing, the order
matters. MTG uses a strict layering system (CR 613):

Layer 1: Copy effects
Layer 2: Control-changing effects  
Layer 3: Text-changing effects
Layer 4: Type-changing effects
Layer 5: Color-changing effects
Layer 6: Ability-adding/removing effects
Layer 7: Power/toughness-setting effects
    7a: Characteristic-defining abilities (CDA) - "*/*" cards
    7b: Effects that set P/T to specific values
    7c: Effects that modify P/T (+X/+X, -X/-X)
    7d: Effects that switch P/T

Within each layer/sublayer, effects apply in TIMESTAMP order
(when they started applying).

DEPENDENCY: If effect A depends on effect B (A would change what
B applies to), A waits for B regardless of timestamp.

Example:
    Creature with Humble (+1/+1 counter, Glorious Anthem in play)
    Humble: "Enchanted creature loses all abilities and is a 0/1"
    
    Layer 6: Lose abilities (Humble)
    Layer 7b: Set to 0/1 (Humble)
    Layer 7c: +1/+1 from counter
    Layer 7c: +1/+1 from Anthem
    
    Result: 2/3 creature with no abilities
"""

from dataclasses import dataclass, field
from typing import Optional, Dict, List, Any, Tuple, Set, Callable
from enum import Enum, auto
from datetime import datetime
import copy


class Layer(Enum):
    """The seven layers of continuous effects."""
    COPY = 1
    CONTROL = 2
    TEXT = 3
    TYPE = 4
    COLOR = 5
    ABILITY = 6
    POWER_TOUGHNESS = 7


class Sublayer(Enum):
    """Sublayers within Layer 7."""
    PT_CDA = "7a"  # Characteristic-defining abilities
    PT_SET = "7b"  # Set to specific value
    PT_MOD = "7c"  # Modifications (+X/+X)
    PT_SWITCH = "7d"  # Switch power and toughness


@dataclass
class ContinuousEffect:
    """
    Represents a continuous effect that modifies game objects.
    
    Examples:
        - Glorious Anthem: "Creatures you control get +1/+1"
        - Blood Moon: "Nonbasic lands are Mountains"
        - Humility: "All creatures lose all abilities and are 1/1"
    """
    id: str
    source_name: str
    source_id: str
    controller: str
    
    # What layer(s) this effect applies in
    layer: Layer
    sublayer: Optional[Sublayer] = None
    
    # What this effect does
    effect_type: str = ""  # "pump", "set_pt", "add_ability", etc.
    
    # For P/T effects
    power_mod: int = 0
    toughness_mod: int = 0
    set_power: Optional[int] = None
    set_toughness: Optional[int] = None
    switch_pt: bool = False
    
    # For ability effects
    abilities_granted: List[str] = field(default_factory=list)
    abilities_removed: List[str] = field(default_factory=list)
    remove_all_abilities: bool = False
    
    # For type effects
    types_added: List[str] = field(default_factory=list)
    types_removed: List[str] = field(default_factory=list)
    set_types: Optional[List[str]] = None
    
    # For color effects
    colors_added: List[str] = field(default_factory=list)
    colors_removed: List[str] = field(default_factory=list)
    set_colors: Optional[List[str]] = None
    
    # For control effects
    new_controller: Optional[str] = None
    
    # For copy effects
    copy_of: Optional[str] = None  # ID of permanent being copied
    
    # What this effect applies to
    applies_to: str = ""  # "creatures you control", "all creatures", "enchanted creature"
    filter_fn: Optional[Callable] = None  # Custom filter function
    
    # Timing
    timestamp: datetime = field(default_factory=datetime.now)
    duration: str = "permanent"  # "permanent", "end_of_turn", "end_of_combat"
    
    # Dependency tracking
    depends_on: List[str] = field(default_factory=list)  # IDs of effects this depends on
    
    def applies_to_permanent(self, permanent, game_state) -> bool:
        """Check if this effect applies to a permanent."""
        if self.filter_fn:
            return self.filter_fn(permanent, game_state)

        applies_to = self.applies_to.lower()

        # "Other" exclusion (CR 109.5): when the applies_to clause begins with
        # "other ...", the source itself never qualifies. Source is identified
        # by self.source_id matching the permanent's id; also compare by name
        # as a fallback (ids occasionally get rebuilt on save-reload).
        is_other = applies_to.startswith("other ")
        if is_other:
            if permanent.get('id') == self.source_id:
                return False
            perm_name = (permanent.get('name') or '').lower()
            src_name = (self.source_name or '').lower()
            if perm_name and src_name and perm_name == src_name:
                return False

        # Parse common patterns
        if applies_to == "all creatures" or applies_to == "each creature":
            return "creature" in permanent.get('types', [])

        # Subtype-restricted: "other werewolves you control", "wolves you control",
        # "humans you control" etc. — match against subtypes (case-insensitive).
        # Recognised generically: any lowercase word followed by " you control" /
        # " opponents control" that is not already handled by color/creature paths.
        subtype_match = None
        if " you control" in applies_to or " opponents control" in applies_to:
            # Strip leading "other " for subtype detection
            stripped = applies_to[6:] if is_other else applies_to
            # Pattern like "<subtype>s you control" or "<subtype>s opponents control"
            import re as _re
            m = _re.match(r'([a-z]+)s? (you control|opponents? control)$', stripped)
            if m:
                candidate = m.group(1)
                # Exclude card TYPES (not subtypes) so clauses like
                # "other creatures you control" fall through to the dedicated
                # creature-type branch below. The greedy `[a-z]+` swallows the
                # plural "s" on "creatures" / "permanents" / "lands", so both
                # singular and plural forms must be listed.
                if candidate not in ("creature", "creatures",
                                      "white creature", "blue creature",
                                      "black creature", "red creature", "green creature",
                                      "permanent", "permanents",
                                      "land", "lands",
                                      "artifact", "artifacts",
                                      "enchantment", "enchantments"):
                    subtype_match = (candidate, m.group(2))
        if subtype_match:
            sub_word, ctrl_word = subtype_match
            subtypes = [s.lower() for s in permanent.get('subtypes', [])]
            # The upstream regex greedily captures the plural form
            # (e.g. "dragons you control" -> sub_word="dragons"). MTG subtypes are
            # stored singular ("Dragon"), so try several singular candidates before
            # giving up. Covers regular plurals + common irregular -ves plurals
            # (wolves->wolf, elves->elf, werewolves->werewolf, knives->knife).
            candidates = [sub_word]
            if sub_word.endswith('ves'):
                candidates.append(sub_word[:-3] + 'f')
                candidates.append(sub_word[:-3] + 'fe')
            if sub_word.endswith('ies'):
                candidates.append(sub_word[:-3] + 'y')
            if sub_word.endswith('es'):
                candidates.append(sub_word[:-2])
            if sub_word.endswith('s'):
                candidates.append(sub_word[:-1])
            if not any(c in subtypes for c in candidates):
                return False
            if "you control" in ctrl_word:
                return permanent.get('controller') == self.controller
            return permanent.get('controller') != self.controller

        # Token-qualified anthem: "(other )?creature tokens you control" —
        # filters by the `is_token` flag on the permanent. Source-exclusion is
        # handled by the is_other branch above.
        if applies_to.endswith("creature tokens you control"):
            return (
                "creature" in permanent.get('types', [])
                and permanent.get('controller') == self.controller
                and bool(permanent.get('is_token', False))
            )
        if applies_to.endswith("creature tokens opponents control"):
            return (
                "creature" in permanent.get('types', [])
                and permanent.get('controller') != self.controller
                and bool(permanent.get('is_token', False))
            )

        # Color-qualified: "white creatures you control", "red creatures you control", etc.
        color_map = {"white": "W", "blue": "U", "black": "B", "red": "R", "green": "G"}
        for color_word, color_code in color_map.items():
            # Allow optional leading "other " before color qualifier
            head = ("other " + color_word) if is_other else color_word
            if applies_to.startswith(head + " creatures"):
                if "you control" in applies_to:
                    return ("creature" in permanent.get('types', []) and
                            permanent.get('controller') == self.controller and
                            color_code in permanent.get('colors', []))
                elif "opponents control" in applies_to:
                    return ("creature" in permanent.get('types', []) and
                            permanent.get('controller') != self.controller and
                            color_code in permanent.get('colors', []))

        # "non-Human creatures you control" / "non-X creatures you control"
        non_match = None
        import re as _re
        non_re = _re.match(r'(?:other )?non-([a-z]+) creatures (you control|opponents? control)', applies_to)
        if non_re:
            non_match = (non_re.group(1), non_re.group(2))
        if non_match:
            excluded_sub, ctrl_word = non_match
            subtypes = [s.lower() for s in permanent.get('subtypes', [])]
            if excluded_sub in subtypes:
                return False
            if "creature" not in permanent.get('types', []):
                return False
            if "you control" in ctrl_word:
                return permanent.get('controller') == self.controller
            return permanent.get('controller') != self.controller

        if "creatures you control" in applies_to:
            return ("creature" in permanent.get('types', []) and
                    permanent.get('controller') == self.controller)

        if "creatures opponents control" in applies_to:
            return ("creature" in permanent.get('types', []) and
                    permanent.get('controller') != self.controller)
        
        if applies_to == "enchanted creature" or applies_to == "equipped creature":
            return permanent.get('id') == permanent.get('attached_to_id')
        
        if "nonbasic lands" in applies_to:
            return ("land" in permanent.get('types', []) and 
                    "basic" not in permanent.get('supertypes', []))
        
        if "all permanents" in applies_to:
            return True
        
        # Default: doesn't apply
        return False


@dataclass
class LayeredPermanent:
    """
    Intermediate representation of a permanent being processed through layers.
    
    We start with base characteristics and apply effects layer by layer.
    """
    id: str
    name: str
    controller: str
    owner: str
    
    # Base characteristics (from the card itself)
    base_types: List[str] = field(default_factory=list)
    base_supertypes: List[str] = field(default_factory=list)
    base_subtypes: List[str] = field(default_factory=list)
    base_colors: List[str] = field(default_factory=list)
    base_power: Optional[int] = None
    base_toughness: Optional[int] = None
    base_abilities: List[str] = field(default_factory=list)
    
    # Current characteristics (modified by effects)
    types: List[str] = field(default_factory=list)
    supertypes: List[str] = field(default_factory=list)
    subtypes: List[str] = field(default_factory=list)
    colors: List[str] = field(default_factory=list)
    power: Optional[int] = None
    toughness: Optional[int] = None
    abilities: List[str] = field(default_factory=list)
    
    # For copy tracking
    copy_of: Optional[str] = None
    
    # For control tracking
    original_controller: str = ""
    
    # Counters (applied in 7c)
    plus_counters: int = 0
    minus_counters: int = 0
    
    def __post_init__(self):
        # Initialize current from base
        self.types = list(self.base_types)
        self.supertypes = list(self.base_supertypes)
        self.subtypes = list(self.base_subtypes)
        self.colors = list(self.base_colors)
        self.power = self.base_power
        self.toughness = self.base_toughness
        self.abilities = list(self.base_abilities)
        self.original_controller = self.controller
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for use with effect filtering."""
        return {
            'id': self.id,
            'name': self.name,
            'controller': self.controller,
            'owner': self.owner,
            'types': self.types,
            'supertypes': self.supertypes,
            'subtypes': self.subtypes,
            'colors': self.colors,
            'power': self.power,
            'toughness': self.toughness,
            'abilities': self.abilities,
        }


class LayersEngine:
    """
    Processes continuous effects through the layer system.
    
    Usage:
        engine = LayersEngine()
        
        # Add continuous effects
        engine.add_effect(glorious_anthem_effect)
        engine.add_effect(humility_effect)
        
        # Calculate final characteristics for a permanent
        final = engine.calculate_characteristics(permanent, game_state)
    """
    
    def __init__(self):
        self.effects: List[ContinuousEffect] = []
        self._effect_counter = 0
    
    def add_effect(self, effect: ContinuousEffect):
        """Add a continuous effect to track."""
        self._effect_counter += 1
        if not effect.id:
            effect.id = f"effect_{self._effect_counter}"
        self.effects.append(effect)
    
    def remove_effect(self, effect_id: str):
        """Remove an effect (e.g., when source leaves)."""
        self.effects = [e for e in self.effects if e.id != effect_id]
    
    def remove_effects_from_source(self, source_id: str):
        """Remove all effects from a source.

        Matches effects whose source_id equals `source_id` OR begins with
        `source_id` followed by an underscore. The suffix form ("{card.id}_color",
        "{card.id}_subtype", "{card.id}_anthem", "{card.id}_humility_abilities",
        etc.) is how the engine differentiates multiple registered effects from
        the same physical card (e.g. Elesh Norn's two anthem clauses, Humility's
        layer-6 + layer-7 pair). Without prefix matching, a card dying or leaving
        the battlefield does not deregister its effects, and they persist for the
        rest of the game (Apr 30 audit found Elesh Norn's anthem still applying
        ~10 turns after she died).
        """
        prefix = source_id + "_"
        self.effects = [
            e for e in self.effects
            if e.source_id != source_id and not e.source_id.startswith(prefix)
        ]
    
    def clear_temporary_effects(self, duration: str):
        """Remove effects with a specific duration (e.g., "end_of_turn")."""
        self.effects = [e for e in self.effects if e.duration != duration]
    
    def calculate_characteristics(self, permanent: LayeredPermanent, 
                                   game_state: Any = None) -> LayeredPermanent:
        """
        Calculate final characteristics by applying all effects in layer order.
        
        Args:
            permanent: The permanent to calculate
            game_state: Game state for effect filtering
        
        Returns:
            New LayeredPermanent with final characteristics
        """
        # Work on a copy
        result = copy.deepcopy(permanent)
        
        # Get effects that apply to this permanent
        applicable = self._get_applicable_effects(result, game_state)
        
        # Sort by layer, sublayer, then timestamp (with dependency handling)
        ordered = self._order_effects(applicable)
        
        # Apply each effect
        for effect in ordered:
            self._apply_effect(effect, result)
        
        return result
    
    def calculate_all(self, permanents: List[LayeredPermanent],
                      game_state: Any = None) -> List[LayeredPermanent]:
        """Calculate characteristics for all permanents."""
        return [self.calculate_characteristics(p, game_state) for p in permanents]
    
    def _get_applicable_effects(self, permanent: LayeredPermanent,
                                 game_state: Any) -> List[ContinuousEffect]:
        """Get effects that apply to a permanent."""
        perm_dict = permanent.to_dict()
        return [e for e in self.effects if e.applies_to_permanent(perm_dict, game_state)]
    
    def _order_effects(self, effects: List[ContinuousEffect]) -> List[ContinuousEffect]:
        """
        Order effects by layer, sublayer, timestamp, with dependency handling.
        """
        # Group by layer
        by_layer: Dict[Layer, List[ContinuousEffect]] = {layer: [] for layer in Layer}
        for effect in effects:
            by_layer[effect.layer].append(effect)
        
        result = []
        
        # Process each layer in order
        for layer in sorted(Layer, key=lambda l: l.value):
            layer_effects = by_layer[layer]
            
            if layer == Layer.POWER_TOUGHNESS:
                # Handle sublayers
                by_sublayer: Dict[Sublayer, List[ContinuousEffect]] = {
                    Sublayer.PT_CDA: [],
                    Sublayer.PT_SET: [],
                    Sublayer.PT_MOD: [],
                    Sublayer.PT_SWITCH: [],
                }
                for effect in layer_effects:
                    sublayer = effect.sublayer or Sublayer.PT_MOD
                    by_sublayer[sublayer].append(effect)
                
                for sublayer in [Sublayer.PT_CDA, Sublayer.PT_SET, 
                                 Sublayer.PT_MOD, Sublayer.PT_SWITCH]:
                    sublayer_effects = by_sublayer[sublayer]
                    # Sort by timestamp within sublayer
                    sublayer_effects.sort(key=lambda e: e.timestamp)
                    # Handle dependencies
                    sublayer_effects = self._resolve_dependencies(sublayer_effects)
                    result.extend(sublayer_effects)
            else:
                # Sort by timestamp within layer
                layer_effects.sort(key=lambda e: e.timestamp)
                # Handle dependencies
                layer_effects = self._resolve_dependencies(layer_effects)
                result.extend(layer_effects)
        
        return result
    
    def _resolve_dependencies(self, effects: List[ContinuousEffect]) -> List[ContinuousEffect]:
        """
        Resolve dependencies within a layer/sublayer.
        
        If effect A depends on effect B, A must apply after B.
        """
        if not effects:
            return effects
        
        # Build dependency graph
        remaining = list(effects)
        result = []
        
        # Simple topological sort
        while remaining:
            # Find effects with no unresolved dependencies
            ready = []
            for effect in remaining:
                deps_resolved = all(
                    dep not in [e.id for e in remaining]
                    for dep in effect.depends_on
                )
                if deps_resolved:
                    ready.append(effect)
            
            if not ready:
                # Circular dependency - just use timestamp order
                ready = remaining[:1]
            
            # Sort ready effects by timestamp
            ready.sort(key=lambda e: e.timestamp)
            
            # Add first ready effect
            effect = ready[0]
            result.append(effect)
            remaining.remove(effect)
        
        return result
    
    def _apply_effect(self, effect: ContinuousEffect, permanent: LayeredPermanent):
        """Apply a single effect to a permanent."""
        
        if effect.layer == Layer.COPY:
            # Copy effects replace characteristics with those of the copied
            # permanent (CR 707.2).  The copy_data dict, if present, holds
            # the full copiable values snapshotted at the time the copy was
            # created.  If only copy_of (an ID) is set, we just record the
            # reference — the game engine handles the actual attribute copy.
            if effect.copy_of:
                permanent.copy_of = effect.copy_of
            copy_data = getattr(effect, '_copy_data', None)
            if copy_data:
                if 'name' in copy_data:
                    permanent.name = copy_data['name']
                if 'types' in copy_data:
                    permanent.types = list(copy_data['types'])
                if 'supertypes' in copy_data:
                    permanent.supertypes = list(copy_data['supertypes'])
                if 'subtypes' in copy_data:
                    permanent.subtypes = list(copy_data['subtypes'])
                if 'colors' in copy_data:
                    permanent.colors = list(copy_data['colors'])
                if 'power' in copy_data:
                    permanent.power = copy_data['power']
                if 'toughness' in copy_data:
                    permanent.toughness = copy_data['toughness']
                if 'abilities' in copy_data:
                    permanent.abilities = list(copy_data['abilities'])
        
        elif effect.layer == Layer.CONTROL:
            if effect.new_controller:
                permanent.controller = effect.new_controller
        
        elif effect.layer == Layer.TEXT:
            # Text-changing effects are rare and complex
            pass
        
        elif effect.layer == Layer.TYPE:
            if effect.set_types is not None:
                permanent.types = list(effect.set_types)
            else:
                for t in effect.types_added:
                    if t not in permanent.types:
                        permanent.types.append(t)
                for t in effect.types_removed:
                    if t in permanent.types:
                        permanent.types.remove(t)
        
        elif effect.layer == Layer.COLOR:
            if effect.set_colors is not None:
                permanent.colors = list(effect.set_colors)
            else:
                for c in effect.colors_added:
                    if c not in permanent.colors:
                        permanent.colors.append(c)
                for c in effect.colors_removed:
                    if c in permanent.colors:
                        permanent.colors.remove(c)
        
        elif effect.layer == Layer.ABILITY:
            if effect.remove_all_abilities:
                permanent.abilities = []
            else:
                for a in effect.abilities_removed:
                    if a in permanent.abilities:
                        permanent.abilities.remove(a)
            
            for a in effect.abilities_granted:
                if a not in permanent.abilities:
                    permanent.abilities.append(a)
        
        elif effect.layer == Layer.POWER_TOUGHNESS:
            if effect.sublayer == Sublayer.PT_CDA:
                # Characteristic-defining abilities
                # Would calculate based on game state (e.g., Tarmogoyf)
                pass
            
            elif effect.sublayer == Sublayer.PT_SET:
                # Set to specific values
                if effect.set_power is not None:
                    permanent.power = effect.set_power
                if effect.set_toughness is not None:
                    permanent.toughness = effect.set_toughness
            
            elif effect.sublayer == Sublayer.PT_MOD:
                # Modifications
                if permanent.power is not None:
                    permanent.power += effect.power_mod
                if permanent.toughness is not None:
                    permanent.toughness += effect.toughness_mod
            
            elif effect.sublayer == Sublayer.PT_SWITCH:
                # Switch P/T
                if effect.switch_pt:
                    permanent.power, permanent.toughness = permanent.toughness, permanent.power


# =============================================================================
# COMMON EFFECT FACTORIES
# =============================================================================

def create_anthem_effect(source_name: str, source_id: str, controller: str,
                         power_mod: int, toughness_mod: int,
                         applies_to: str = "creatures you control") -> ContinuousEffect:
    """Create a Glorious Anthem-style effect."""
    return ContinuousEffect(
        id=f"{source_id}_anthem",
        source_name=source_name,
        source_id=source_id,
        controller=controller,
        layer=Layer.POWER_TOUGHNESS,
        sublayer=Sublayer.PT_MOD,
        effect_type="pump",
        power_mod=power_mod,
        toughness_mod=toughness_mod,
        applies_to=applies_to,
    )


def create_humility_effect(source_name: str, source_id: str, 
                           controller: str) -> List[ContinuousEffect]:
    """
    Create Humility's effects.
    
    Humility: "All creatures lose all abilities and have base power and toughness 1/1."
    
    This is TWO effects in TWO different layers:
    1. Layer 6: Remove all abilities
    2. Layer 7b: Set P/T to 1/1
    """
    return [
        ContinuousEffect(
            id=f"{source_id}_humility_abilities",
            source_name=source_name,
            source_id=source_id,
            controller=controller,
            layer=Layer.ABILITY,
            effect_type="remove_abilities",
            remove_all_abilities=True,
            applies_to="all creatures",
        ),
        ContinuousEffect(
            id=f"{source_id}_humility_pt",
            source_name=source_name,
            source_id=source_id,
            controller=controller,
            layer=Layer.POWER_TOUGHNESS,
            sublayer=Sublayer.PT_SET,
            effect_type="set_pt",
            set_power=1,
            set_toughness=1,
            applies_to="all creatures",
        ),
    ]


def create_blood_moon_effect(source_name: str, source_id: str,
                              controller: str) -> ContinuousEffect:
    """
    Create Blood Moon's effect.
    
    Blood Moon: "Nonbasic lands are Mountains."
    
    This sets the subtype to Mountain (replacing other land types).
    """
    return ContinuousEffect(
        id=f"{source_id}_blood_moon",
        source_name=source_name,
        source_id=source_id,
        controller=controller,
        layer=Layer.TYPE,
        effect_type="set_type",
        set_types=["land"],  # Keep as land
        types_added=["Mountain"],  # Add Mountain subtype
        applies_to="nonbasic lands",
    )


def create_color_change_effect(source_name: str, source_id: str,
                               controller: str, colors_added: list,
                               applies_to: str = "all permanents and spells") -> ContinuousEffect:
    """Create a color-adding effect (Painter's Servant, etc.).

    Painter's Servant: "All cards that aren't on the battlefield, spells, and
    permanents are the chosen color in addition to their other colors."
    """
    return ContinuousEffect(
        id=f"{source_id}_color_add",
        source_name=source_name,
        source_id=source_id,
        controller=controller,
        layer=Layer.COLOR,
        effect_type="add_colors",
        colors_added=colors_added,
        applies_to=applies_to,
    )


def create_control_effect(source_name: str, source_id: str,
                          controller: str, target_id: str,
                          new_controller: str) -> ContinuousEffect:
    """Create a control-changing effect (Mind Control, etc.)."""
    return ContinuousEffect(
        id=f"{source_id}_control",
        source_name=source_name,
        source_id=source_id,
        controller=controller,
        layer=Layer.CONTROL,
        effect_type="change_control",
        new_controller=new_controller,
        filter_fn=lambda p, g: p.get('id') == target_id,
    )


def create_pump_effect(source_name: str, source_id: str,
                       controller: str, target_id: str,
                       power_mod: int, toughness_mod: int,
                       keywords: List[str] = None,
                       duration: str = "end_of_turn") -> List[ContinuousEffect]:
    """Create a temporary pump effect (Giant Growth, etc.)."""
    effects = [
        ContinuousEffect(
            id=f"{source_id}_pump_pt",
            source_name=source_name,
            source_id=source_id,
            controller=controller,
            layer=Layer.POWER_TOUGHNESS,
            sublayer=Sublayer.PT_MOD,
            effect_type="pump",
            power_mod=power_mod,
            toughness_mod=toughness_mod,
            duration=duration,
            filter_fn=lambda p, g: p.get('id') == target_id,
        ),
    ]
    
    if keywords:
        effects.append(ContinuousEffect(
            id=f"{source_id}_pump_abilities",
            source_name=source_name,
            source_id=source_id,
            controller=controller,
            layer=Layer.ABILITY,
            effect_type="add_abilities",
            abilities_granted=keywords,
            duration=duration,
            filter_fn=lambda p, g: p.get('id') == target_id,
        ))
    
    return effects


def create_copy_effect(source_name: str, source_id: str, controller: str,
                       target_id: str, copy_data: Dict[str, Any]) -> ContinuousEffect:
    """
    Create a Layer 1 copy effect (CR 707).

    Used by Clone, Spark Double, Clever Impersonator, Phyrexian Metamorph,
    and similar "enters as a copy of" permanents.

    Args:
        source_name: Name of the permanent that is becoming a copy.
        source_id:   ID of the permanent that is becoming a copy.
        controller:  Name of the controller.
        target_id:   ID of the permanent being copied.
        copy_data:   Snapshot of the copied permanent's copiable values:
                     {'name', 'types', 'supertypes', 'subtypes', 'colors',
                      'power', 'toughness', 'abilities'}.
                     These are applied in Layer 1 before any other effects.
    """
    effect = ContinuousEffect(
        id=f"{source_id}_copy",
        source_name=source_name,
        source_id=source_id,
        controller=controller,
        layer=Layer.COPY,
        effect_type="copy",
        copy_of=target_id,
        filter_fn=lambda p, g: p.get('id') == source_id,
    )
    # Attach the full copiable values so _apply_effect can use them
    effect._copy_data = copy_data
    return effect


# =============================================================================
# COUNTER-BASED EFFECTS
# =============================================================================

def apply_counters_to_permanent(permanent: LayeredPermanent):
    """
    Apply +1/+1 and -1/-1 counters to a permanent.
    
    This is called during layer 7c processing.
    Counters are modifications, not setting effects.
    """
    if permanent.power is not None:
        permanent.power += permanent.plus_counters
        permanent.power -= permanent.minus_counters
    if permanent.toughness is not None:
        permanent.toughness += permanent.plus_counters
        permanent.toughness -= permanent.minus_counters


# =============================================================================
# DEMO / TEST
# =============================================================================

def demo():
    """Demo the layers system."""
    print("=== Layers System Demo ===\n")
    
    engine = LayersEngine()
    
    # Create a test creature: 3/3 Bear with Flying
    bear = LayeredPermanent(
        id="bear_1",
        name="Flying Bear",
        controller="Alice",
        owner="Alice",
        base_types=["creature"],
        base_subtypes=["Bear"],
        base_colors=["G"],
        base_power=3,
        base_toughness=3,
        base_abilities=["Flying"],
        plus_counters=1,  # Has a +1/+1 counter
    )
    
    print(f"Base creature: {bear.name}")
    print(f"  Base P/T: {bear.base_power}/{bear.base_toughness}")
    print(f"  Base abilities: {bear.base_abilities}")
    print(f"  +1/+1 counters: {bear.plus_counters}")
    
    # Test 1: Just counters
    print("\n--- Test 1: Just counters ---")
    result = engine.calculate_characteristics(bear, None)
    apply_counters_to_permanent(result)
    print(f"  Final P/T: {result.power}/{result.toughness}")
    print(f"  Expected: 4/4 (base 3/3 + 1 counter)")
    
    # Test 2: Add Glorious Anthem
    print("\n--- Test 2: Glorious Anthem ---")
    anthem = create_anthem_effect("Glorious Anthem", "anthem_1", "Alice", 1, 1)
    engine.add_effect(anthem)
    result = engine.calculate_characteristics(bear, None)
    apply_counters_to_permanent(result)
    print(f"  Final P/T: {result.power}/{result.toughness}")
    print(f"  Expected: 5/5 (base 3/3 + 1 counter + 1/1 anthem)")
    
    # Test 3: Add Humility (removes abilities, sets to 1/1)
    print("\n--- Test 3: Humility + Anthem ---")
    humility_effects = create_humility_effect("Humility", "humility_1", "Bob")
    for effect in humility_effects:
        engine.add_effect(effect)
    
    result = engine.calculate_characteristics(bear, None)
    apply_counters_to_permanent(result)
    print(f"  Final P/T: {result.power}/{result.toughness}")
    print(f"  Final abilities: {result.abilities}")
    print(f"  Expected P/T: 3/3 (Humility sets to 1/1, then +1 counter, +1/+1 anthem)")
    print(f"  Expected abilities: [] (Humility removes all)")
    
    # Explanation:
    # Layer 6: Remove all abilities (Humility) - Flying gone
    # Layer 7b: Set to 1/1 (Humility)
    # Layer 7c: +1/+1 from counter = 2/2
    # Layer 7c: +1/+1 from Anthem = 3/3
    
    print("\n--- Layer Order Explanation ---")
    print("  Layer 6 (Abilities): Humility removes Flying")
    print("  Layer 7b (Set P/T): Humility sets to 1/1")
    print("  Layer 7c (Modify): +1/+1 counter → 2/2")
    print("  Layer 7c (Modify): Glorious Anthem → 3/3")
    print("  Final: 3/3 with no abilities")
    
    # Test 4: Timestamp matters
    print("\n--- Test 4: Timestamp Order ---")
    engine.effects.clear()
    
    # Two anthems added at different times
    anthem1 = create_anthem_effect("Anthem A", "anthem_a", "Alice", 2, 2)
    anthem1.timestamp = datetime(2024, 1, 1, 12, 0, 0)
    
    anthem2 = create_anthem_effect("Anthem B", "anthem_b", "Alice", 1, 1)  
    anthem2.timestamp = datetime(2024, 1, 1, 12, 0, 1)  # 1 second later
    
    engine.add_effect(anthem1)
    engine.add_effect(anthem2)
    
    simple_bear = LayeredPermanent(
        id="bear_2",
        name="Simple Bear",
        controller="Alice",
        owner="Alice",
        base_types=["creature"],
        base_power=2,
        base_toughness=2,
    )
    
    result = engine.calculate_characteristics(simple_bear, None)
    print(f"  Base: 2/2")
    print(f"  Anthem A: +2/+2 (timestamp: 12:00:00)")
    print(f"  Anthem B: +1/+1 (timestamp: 12:00:01)")
    print(f"  Final P/T: {result.power}/{result.toughness}")
    print(f"  Expected: 5/5 (both apply, order doesn't matter for additive)")
    
    print("\n=== Demo Complete ===")


if __name__ == "__main__":
    demo()
