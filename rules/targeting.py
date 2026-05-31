"""
MTG Targeting Validation
========================

Handles target validation for spells and abilities.

Key concepts:
1. Targeting happens when spell/ability is put on stack
2. Targets are checked for legality on resolution
3. If all targets become illegal, spell/ability is countered (fizzles)
4. If some targets become illegal, spell resolves for remaining targets

Target restrictions include:
- Type restrictions ("target creature", "target player")
- Controller restrictions ("target opponent", "target creature you control")
- Characteristic restrictions ("target nonblack creature", "target creature with power 2 or less")
- Protection/Hexproof/Shroud/Ward
- "Can't be targeted" effects

CR 115 covers targeting rules.
"""

from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Dict, List, Optional, Set, Tuple, Callable, Any, Union
import re


class TargetType(Enum):
    """Types of targetable objects."""
    CREATURE = auto()
    PLAYER = auto()
    PLANESWALKER = auto()
    ARTIFACT = auto()
    ENCHANTMENT = auto()
    LAND = auto()
    SPELL = auto()  # On the stack
    ABILITY = auto()  # On the stack
    PERMANENT = auto()  # Any permanent type
    NONLAND_PERMANENT = auto()  # Any permanent except land
    CARD = auto()  # Usually in graveyard
    ANY = auto()  # "any target" = creature, player, or planeswalker


class ControllerRestriction(Enum):
    """Restrictions on who controls the target."""
    ANY = auto()
    YOU = auto()  # Controller of spell/ability
    OPPONENT = auto()
    ANOTHER_PLAYER = auto()


@dataclass
class TargetRestriction:
    """Describes what can be targeted."""
    target_types: Set[TargetType] = field(default_factory=lambda: {TargetType.ANY})
    controller: ControllerRestriction = ControllerRestriction.ANY
    
    # Characteristic restrictions
    colors_allowed: Optional[Set[str]] = None  # None = any, set = only these
    colors_excluded: Set[str] = field(default_factory=set)  # "nonblack"
    
    power_max: Optional[int] = None  # "power 2 or less"
    power_min: Optional[int] = None  # "power 3 or greater"
    toughness_max: Optional[int] = None
    toughness_min: Optional[int] = None
    cmc_max: Optional[int] = None  # "mana value 3 or less"
    cmc_min: Optional[int] = None
    
    types_required: Set[str] = field(default_factory=set)  # "target Goblin"
    types_excluded: Set[str] = field(default_factory=set)  # "non-Zombie creature"
    
    keywords_required: Set[str] = field(default_factory=set)  # "creature with flying"
    keywords_excluded: Set[str] = field(default_factory=set)  # "creature without flying"
    
    must_be_tapped: Optional[bool] = None  # True = must be tapped, False = must be untapped
    must_be_attacking: bool = False
    must_be_blocking: bool = False
    must_be_enchanted: bool = False
    must_be_equipped: bool = False
    
    # Card-in-zone restrictions
    zone: Optional[str] = None  # "graveyard", "library", "hand", etc.
    
    # Other
    other_restrictions: List[str] = field(default_factory=list)  # For complex cases
    
    def describe(self) -> str:
        """Generate a human-readable description."""
        parts = []
        
        # Types
        if TargetType.ANY in self.target_types:
            parts.append("any target")
        else:
            type_names = [t.name.lower() for t in self.target_types]
            parts.append(" or ".join(type_names))
        
        # Controller
        if self.controller == ControllerRestriction.YOU:
            parts.append("you control")
        elif self.controller == ControllerRestriction.OPPONENT:
            parts.append("an opponent controls")
        
        # Colors
        if self.colors_excluded:
            parts.append(f"non{'/'.join(self.colors_excluded)}")
        
        # P/T
        if self.power_max is not None:
            parts.append(f"power {self.power_max} or less")
        if self.power_min is not None:
            parts.append(f"power {self.power_min} or greater")
        
        return "target " + " ".join(parts)


@dataclass
class ProtectionAbility:
    """Represents a protection ability."""
    from_colors: Set[str] = field(default_factory=set)  # "protection from red"
    from_types: Set[str] = field(default_factory=set)  # "protection from artifacts"
    from_qualities: Set[str] = field(default_factory=set)  # "protection from everything"
    from_cmc: Optional[Tuple[str, int]] = None  # ("<=", 3) = CMC 3 or less
    from_players: Set[str] = field(default_factory=set)  # "protection from opponents"
    
    def blocks_targeting_from(self, source_colors: Set[str], source_types: Set[str],
                              source_cmc: int, source_controller: str,
                              target_controller: str) -> Tuple[bool, str]:
        """Check if this protection blocks targeting from a source."""
        # Protection from colors
        for color in source_colors:
            if color in self.from_colors:
                return True, f"protection from {color}"
        
        # Protection from types
        for source_type in source_types:
            if source_type.lower() in {t.lower() for t in self.from_types}:
                return True, f"protection from {source_type}s"
        
        # Protection from CMC
        if self.from_cmc:
            op, value = self.from_cmc
            if op == "<=" and source_cmc <= value:
                return True, f"protection from mana value {value} or less"
            elif op == ">=" and source_cmc >= value:
                return True, f"protection from mana value {value} or greater"
            elif op == "=" and source_cmc == value:
                return True, f"protection from mana value {value}"
        
        # Protection from everything
        if "everything" in self.from_qualities:
            return True, "protection from everything"
        
        # Protection from opponents
        if "opponents" in self.from_players and source_controller != target_controller:
            return True, "protection from opponents"
        
        return False, ""


@dataclass
class Targetable:
    """Something that can be targeted (permanent, player, spell on stack)."""
    id: str
    name: str
    controller: str
    owner: str
    
    # Type information
    types: Set[str] = field(default_factory=set)  # "creature", "instant", etc.
    subtypes: Set[str] = field(default_factory=set)  # "Goblin", "Aura", etc.
    supertypes: Set[str] = field(default_factory=set)  # "legendary", "basic"
    
    # Characteristics
    colors: Set[str] = field(default_factory=set)  # W, U, B, R, G
    power: Optional[int] = None
    toughness: Optional[int] = None
    cmc: int = 0
    
    # Keywords
    keywords: Set[str] = field(default_factory=set)
    
    # Targeting protection
    has_hexproof: bool = False
    has_shroud: bool = False
    has_ward: Optional[int] = None  # Ward cost (simplified to just mana)
    protection: List[ProtectionAbility] = field(default_factory=list)
    cant_be_targeted_by: List[str] = field(default_factory=list)  # Descriptions of restrictions
    
    # State
    is_tapped: bool = False
    is_attacking: bool = False
    is_blocking: bool = False
    is_enchanted: bool = False
    is_equipped: bool = False
    
    # Zone (for cards not on battlefield)
    zone: str = "battlefield"
    
    # For players
    is_player: bool = False
    has_hexproof_from_opponents: bool = False  # Leyline of Sanctity effect
    
    def is_type(self, type_name: str) -> bool:
        """Check if this has a specific type."""
        type_lower = type_name.lower()
        return (type_lower in {t.lower() for t in self.types} or
                type_lower in {t.lower() for t in self.subtypes} or
                type_lower in {t.lower() for t in self.supertypes})


@dataclass
class TargetingSource:
    """The source of targeting (spell or ability)."""
    id: str
    name: str
    controller: str
    colors: Set[str] = field(default_factory=set)
    types: Set[str] = field(default_factory=set)
    cmc: int = 0
    is_spell: bool = True  # vs ability


class TargetValidator:
    """
    Validates targeting for spells and abilities.
    
    Usage:
        validator = TargetValidator()
        
        # When putting spell on stack
        legal, reason = validator.can_target(source, target, restriction)
        
        # On resolution (check if still legal)
        still_legal, reason = validator.validate_on_resolution(source, target, restriction)
    """
    
    def can_target(
        self,
        source: TargetingSource,
        target: Targetable,
        restriction: TargetRestriction,
        targeting_player: str  # Who is choosing the target
    ) -> Tuple[bool, str]:
        """
        Check if a source can target a targetable.
        
        Returns (is_legal, reason).
        """
        # Check basic type match
        if not self._check_type_match(target, restriction):
            return False, f"{target.name} is not a valid target type"
        
        # Check controller restriction
        if not self._check_controller(target, restriction, targeting_player):
            return False, f"{target.name} has wrong controller"
        
        # Check color restrictions
        if not self._check_colors(target, restriction):
            return False, f"{target.name} has excluded color"
        
        # Check P/T restrictions
        if not self._check_power_toughness(target, restriction):
            return False, f"{target.name} has wrong power/toughness"
        
        # Check CMC restrictions
        if not self._check_cmc(target, restriction):
            return False, f"{target.name} has wrong mana value"
        
        # Check type requirements
        if not self._check_type_requirements(target, restriction):
            return False, f"{target.name} doesn't have required type"
        
        # Check keyword requirements
        if not self._check_keyword_requirements(target, restriction):
            return False, f"{target.name} doesn't have required keyword"
        
        # Check state requirements (tapped, attacking, etc.)
        if not self._check_state_requirements(target, restriction):
            return False, f"{target.name} is in wrong state"
        
        # Check zone
        if restriction.zone and target.zone != restriction.zone:
            return False, f"{target.name} is not in {restriction.zone}"
        
        # Now check if targeting is actually blocked
        blocked, reason = self._check_targeting_protection(source, target, targeting_player)
        if blocked:
            return False, reason
        
        return True, "Legal target"
    
    def validate_on_resolution(
        self,
        source: TargetingSource,
        target: Targetable,
        restriction: TargetRestriction,
        targeting_player: str
    ) -> Tuple[bool, str]:
        """
        Validate that a target is still legal on resolution.
        
        Same checks as can_target, but called when spell/ability resolves.
        """
        # Most checks are the same
        return self.can_target(source, target, restriction, targeting_player)
    
    def check_all_targets(
        self,
        source: TargetingSource,
        targets: List[Tuple[Targetable, TargetRestriction]],
        targeting_player: str
    ) -> Tuple[bool, List[str], bool]:
        """
        Check multiple targets for a spell.
        
        Returns (all_legal, reasons, should_fizzle).
        should_fizzle is True if ALL targets are illegal (spell countered).
        """
        results = []
        any_legal = False
        
        for target, restriction in targets:
            legal, reason = self.can_target(source, target, restriction, targeting_player)
            results.append(reason)
            if legal:
                any_legal = True
        
        all_legal = all(r == "Legal target" for r in results)
        should_fizzle = not any_legal and len(targets) > 0
        
        return all_legal, results, should_fizzle
    
    # =========================================================================
    # Individual Checks
    # =========================================================================
    
    def _check_type_match(self, target: Targetable, restriction: TargetRestriction) -> bool:
        """Check if target matches the type restriction."""
        # Empty target_types means no type restriction (permissive default)
        if not restriction.target_types:
            return True

        if TargetType.ANY in restriction.target_types:
            # "any target" = creature, player, planeswalker, or battle
            return (target.is_player or
                    target.is_type("creature") or
                    target.is_type("planeswalker") or
                    target.is_type("battle"))

        for target_type in restriction.target_types:
            if target_type == TargetType.PLAYER and target.is_player:
                return True
            if target_type == TargetType.CREATURE and target.is_type("creature"):
                return True
            if target_type == TargetType.PLANESWALKER and target.is_type("planeswalker"):
                return True
            if target_type == TargetType.ARTIFACT and target.is_type("artifact"):
                return True
            if target_type == TargetType.ENCHANTMENT and target.is_type("enchantment"):
                return True
            if target_type == TargetType.LAND and target.is_type("land"):
                return True
            if target_type == TargetType.PERMANENT and not target.is_player:
                return True
            if target_type == TargetType.NONLAND_PERMANENT and not target.is_player and not target.is_type("land"):
                return True
            if target_type == TargetType.SPELL and "spell" in target.types:
                return True

        return False
    
    def _check_controller(self, target: Targetable, restriction: TargetRestriction,
                          targeting_player: str) -> bool:
        """Check controller restriction."""
        if restriction.controller == ControllerRestriction.ANY:
            return True
        
        if restriction.controller == ControllerRestriction.YOU:
            return target.controller == targeting_player
        
        if restriction.controller == ControllerRestriction.OPPONENT:
            return target.controller != targeting_player
        
        if restriction.controller == ControllerRestriction.ANOTHER_PLAYER:
            # For "target player" with "another" restriction
            return target.id != targeting_player
        
        return True
    
    def _check_colors(self, target: Targetable, restriction: TargetRestriction) -> bool:
        """Check color restrictions."""
        if restriction.colors_allowed is not None:
            # Must have at least one allowed color
            if not target.colors & restriction.colors_allowed:
                # Unless colorless is allowed and target is colorless
                if not (not target.colors and "" in restriction.colors_allowed):
                    return False
        
        if restriction.colors_excluded:
            # Must not have any excluded colors
            if target.colors & restriction.colors_excluded:
                return False
        
        return True
    
    def _check_power_toughness(self, target: Targetable, restriction: TargetRestriction) -> bool:
        """Check power/toughness restrictions."""
        if target.power is None:
            # Not a creature, skip P/T checks
            return True
        
        if restriction.power_max is not None and target.power > restriction.power_max:
            return False
        if restriction.power_min is not None and target.power < restriction.power_min:
            return False
        if restriction.toughness_max is not None and target.toughness > restriction.toughness_max:
            return False
        if restriction.toughness_min is not None and target.toughness < restriction.toughness_min:
            return False
        
        return True
    
    def _check_cmc(self, target: Targetable, restriction: TargetRestriction) -> bool:
        """Check converted mana cost restrictions."""
        if restriction.cmc_max is not None and target.cmc > restriction.cmc_max:
            return False
        if restriction.cmc_min is not None and target.cmc < restriction.cmc_min:
            return False
        return True
    
    def _check_type_requirements(self, target: Targetable, restriction: TargetRestriction) -> bool:
        """Check required/excluded type restrictions."""
        # Required types (all must be present)
        for req_type in restriction.types_required:
            if not target.is_type(req_type):
                return False
        
        # Excluded types (none can be present)
        for exc_type in restriction.types_excluded:
            if target.is_type(exc_type):
                return False
        
        return True
    
    def _check_keyword_requirements(self, target: Targetable, restriction: TargetRestriction) -> bool:
        """Check keyword requirements."""
        target_keywords_lower = {k.lower() for k in target.keywords if isinstance(k, str)}

        for req_kw in restriction.keywords_required:
            if isinstance(req_kw, str) and req_kw.lower() not in target_keywords_lower:
                return False

        for exc_kw in restriction.keywords_excluded:
            if isinstance(exc_kw, str) and exc_kw.lower() in target_keywords_lower:
                return False
        
        return True
    
    def _check_state_requirements(self, target: Targetable, restriction: TargetRestriction) -> bool:
        """Check state requirements (tapped, attacking, etc.)."""
        if restriction.must_be_tapped is True and not target.is_tapped:
            return False
        if restriction.must_be_tapped is False and target.is_tapped:
            return False
        if restriction.must_be_attacking and not target.is_attacking:
            return False
        if restriction.must_be_blocking and not target.is_blocking:
            return False
        if restriction.must_be_enchanted and not target.is_enchanted:
            return False
        if restriction.must_be_equipped and not target.is_equipped:
            return False
        return True
    
    def _check_targeting_protection(
        self,
        source: TargetingSource,
        target: Targetable,
        targeting_player: str
    ) -> Tuple[bool, str]:
        """
        Check if targeting is blocked by hexproof, shroud, protection, etc.
        
        Returns (is_blocked, reason).
        """
        # Shroud blocks everything
        if target.has_shroud:
            return True, f"{target.name} has shroud"
        
        # Hexproof blocks opponents
        if target.has_hexproof and source.controller != target.controller:
            return True, f"{target.name} has hexproof"
        
        # Player-specific hexproof
        if target.is_player and target.has_hexproof_from_opponents:
            if source.controller != target.controller:
                return True, f"{target.name} has hexproof from opponents"
        
        # Protection
        for prot in target.protection:
            blocked, reason = prot.blocks_targeting_from(
                source.colors, source.types, source.cmc,
                source.controller, target.controller
            )
            if blocked:
                return True, f"{target.name} has {reason}"
        
        # "Can't be targeted by" effects
        for restriction_desc in target.cant_be_targeted_by:
            # This would need more sophisticated matching
            # For now, just check if it mentions the source type
            if any(t.lower() in restriction_desc.lower() for t in source.types):
                return True, f"{target.name} can't be targeted by {restriction_desc}"
        
        # Ward — opponent must pay N or spell is countered (CR 702.21)
        # For autoplay: we can't prompt for payment, so we check if the caster
        # has enough mana to pay the ward cost.  If not, targeting is blocked.
        # If yes, we allow targeting (the mana will be deducted by the caller).
        if target.has_ward is not None and source.controller != target.controller:
            # Ward triggers when targeted by opponent's spell/ability
            # The ward_payable flag is set by the caller (targeting_helpers.py)
            # based on whether the caster has enough spare mana.
            # If not set, we conservatively allow targeting (permissive default).
            ward_can_pay = getattr(source, '_ward_payable', True)
            if not ward_can_pay:
                return True, f"{target.name} has Ward {target.has_ward} (can't pay)"

        return False, ""


# =============================================================================
# Parser for Target Text
# =============================================================================

class TargetTextParser:
    """
    Parses target restriction text from card oracle text.
    
    Examples:
        "target creature" -> TargetRestriction(target_types={CREATURE})
        "target nonblack creature" -> ... colors_excluded={"B"}
        "target creature you control" -> ... controller=YOU
        "target creature with flying" -> ... keywords_required={"Flying"}
    """
    
    @staticmethod
    def parse(text: str) -> TargetRestriction:
        """Parse target restriction from text."""
        text = text.lower().strip()
        restriction = TargetRestriction()
        
        # Determine target type(s)
        # Handle compound types first (e.g., "artifact or enchantment")
        if "any target" in text:
            restriction.target_types = {TargetType.ANY}
        elif "nonland permanent" in text:
            restriction.target_types = {TargetType.NONLAND_PERMANENT}
        elif "artifact or enchantment" in text or "enchantment or artifact" in text:
            restriction.target_types = {TargetType.ARTIFACT, TargetType.ENCHANTMENT}
        elif "artifact or creature" in text or "creature or artifact" in text:
            restriction.target_types = {TargetType.ARTIFACT, TargetType.CREATURE}
        elif "creature or planeswalker" in text or "planeswalker or creature" in text:
            restriction.target_types = {TargetType.CREATURE, TargetType.PLANESWALKER}
        elif "creature or player" in text or "player or creature" in text:
            restriction.target_types = {TargetType.CREATURE, TargetType.PLAYER}
        elif "player" in text and "planeswalker" not in text:
            restriction.target_types = {TargetType.PLAYER}
        elif "planeswalker" in text:
            restriction.target_types = {TargetType.PLANESWALKER}
        elif "creature" in text:
            restriction.target_types = {TargetType.CREATURE}
        elif "artifact" in text:
            restriction.target_types = {TargetType.ARTIFACT}
        elif "enchantment" in text:
            restriction.target_types = {TargetType.ENCHANTMENT}
        elif "noncreature" in text and "nonland" in text:
            restriction.target_types = {TargetType.NONLAND_PERMANENT}
        elif "nonland" in text:
            restriction.target_types = {TargetType.NONLAND_PERMANENT}
        elif "land" in text:
            restriction.target_types = {TargetType.LAND}
        elif "permanent" in text:
            restriction.target_types = {TargetType.PERMANENT}
        elif "spell" in text:
            restriction.target_types = {TargetType.SPELL}
        elif "card" in text:
            restriction.target_types = {TargetType.CARD}
        
        # Controller restriction
        if "you control" in text:
            restriction.controller = ControllerRestriction.YOU
        elif "opponent control" in text or "an opponent controls" in text:
            restriction.controller = ControllerRestriction.OPPONENT
        elif "another player" in text:
            restriction.controller = ControllerRestriction.ANOTHER_PLAYER
        
        # Color exclusions
        color_map = {
            "nonwhite": "W", "non-white": "W",
            "nonblue": "U", "non-blue": "U",
            "nonblack": "B", "non-black": "B",
            "nonred": "R", "non-red": "R",
            "nongreen": "G", "non-green": "G"
        }
        for pattern, color in color_map.items():
            if pattern in text:
                restriction.colors_excluded.add(color)
        
        # Power/toughness restrictions
        power_match = re.search(r"power (\d+) or (less|greater)", text)
        if power_match:
            value = int(power_match.group(1))
            if power_match.group(2) == "less":
                restriction.power_max = value
            else:
                restriction.power_min = value
        
        toughness_match = re.search(r"toughness (\d+) or (less|greater)", text)
        if toughness_match:
            value = int(toughness_match.group(1))
            if toughness_match.group(2) == "less":
                restriction.toughness_max = value
            else:
                restriction.toughness_min = value
        
        # CMC restrictions
        cmc_match = re.search(r"mana value (\d+) or (less|greater)", text)
        if cmc_match:
            value = int(cmc_match.group(1))
            if cmc_match.group(2) == "less":
                restriction.cmc_max = value
            else:
                restriction.cmc_min = value
        
        # Keyword requirements
        if "with flying" in text:
            restriction.keywords_required.add("Flying")
        if "without flying" in text:
            restriction.keywords_excluded.add("Flying")
        
        # State requirements
        if "tapped" in text and "untapped" not in text:
            restriction.must_be_tapped = True
        if "untapped" in text:
            restriction.must_be_tapped = False
        if "attacking" in text:
            restriction.must_be_attacking = True
        if "blocking" in text:
            restriction.must_be_blocking = True
        
        # Zone
        if "in a graveyard" in text or "from a graveyard" in text:
            restriction.zone = "graveyard"
        
        return restriction


# =============================================================================
# Demo / Test
# =============================================================================

def demo():
    """Demo the targeting system."""
    print("=== Targeting Validation Demo ===\n")
    
    validator = TargetValidator()
    
    # Create some targetables
    bear = Targetable(
        id="bear1", name="Grizzly Bears", controller="Alice", owner="Alice",
        types={"creature"}, subtypes={"Bear"}, colors={"G"},
        power=2, toughness=2, cmc=2
    )
    
    hexproof_creature = Targetable(
        id="troll1", name="Troll Ascetic", controller="Bob", owner="Bob",
        types={"creature"}, subtypes={"Troll", "Shaman"}, colors={"G"},
        power=3, toughness=2, cmc=3,
        has_hexproof=True
    )
    
    protected_creature = Targetable(
        id="crusader1", name="White Knight", controller="Alice", owner="Alice",
        types={"creature"}, subtypes={"Human", "Knight"}, colors={"W"},
        power=2, toughness=2, cmc=2,
        protection=[ProtectionAbility(from_colors={"B"})]
    )
    
    alice = Targetable(
        id="Alice", name="Alice", controller="Alice", owner="Alice",
        is_player=True
    )
    
    # Create a targeting source (Lightning Bolt)
    bolt = TargetingSource(
        id="bolt1", name="Lightning Bolt", controller="Bob",
        colors={"R"}, types={"instant"}, cmc=1
    )
    
    # Create target restriction for "any target"
    any_target = TargetRestriction(target_types={TargetType.ANY})
    
    print("Lightning Bolt (Bob's) targeting:")
    
    # Test targeting bear
    legal, reason = validator.can_target(bolt, bear, any_target, "Bob")
    print(f"  Can target Grizzly Bears? {legal} - {reason}")
    
    # Test targeting hexproof creature
    legal, reason = validator.can_target(bolt, hexproof_creature, any_target, "Bob")
    print(f"  Can target Troll Ascetic? {legal} - {reason}")
    
    # Test targeting protected creature
    legal, reason = validator.can_target(bolt, protected_creature, any_target, "Bob")
    print(f"  Can target White Knight? {legal} - {reason}")
    
    # Test targeting player
    legal, reason = validator.can_target(bolt, alice, any_target, "Bob")
    print(f"  Can target Alice? {legal} - {reason}")
    
    # Now test with a black spell
    print("\n\nDoom Blade (Bob's) targeting:")
    doom_blade = TargetingSource(
        id="doom1", name="Doom Blade", controller="Bob",
        colors={"B"}, types={"instant"}, cmc=2
    )
    
    # "target nonblack creature"
    doom_restriction = TargetRestriction(
        target_types={TargetType.CREATURE},
        colors_excluded={"B"}
    )
    
    legal, reason = validator.can_target(doom_blade, bear, doom_restriction, "Bob")
    print(f"  Can target Grizzly Bears? {legal} - {reason}")
    
    legal, reason = validator.can_target(doom_blade, protected_creature, doom_restriction, "Bob")
    print(f"  Can target White Knight? {legal} - {reason}")
    
    # Test parser
    print("\n\n=== Target Text Parser Demo ===")
    
    test_texts = [
        "target creature",
        "target nonblack creature",
        "target creature you control",
        "target creature with flying",
        "target creature with power 2 or less",
        "target creature an opponent controls",
        "any target",
        "target tapped creature",
        "target creature card in a graveyard",
    ]
    
    for text in test_texts:
        restriction = TargetTextParser.parse(text)
        print(f"  \"{text}\"")
        print(f"    → types: {[t.name for t in restriction.target_types]}")
        if restriction.controller != ControllerRestriction.ANY:
            print(f"    → controller: {restriction.controller.name}")
        if restriction.colors_excluded:
            print(f"    → colors excluded: {restriction.colors_excluded}")
        if restriction.power_max is not None:
            print(f"    → power max: {restriction.power_max}")
        if restriction.keywords_required:
            print(f"    → keywords required: {restriction.keywords_required}")
        if restriction.must_be_tapped is not None:
            print(f"    → must be tapped: {restriction.must_be_tapped}")
        if restriction.zone:
            print(f"    → zone: {restriction.zone}")
    
    print("\n=== Demo Complete ===")


if __name__ == "__main__":
    demo()
