"""
MTG Mana System
================

Handles mana costs, mana pools, and payment validation.

Key insight: Scryfall already parses mana costs for us!
We just need to:
1. Parse their format into structured data
2. Track mana pools
3. Validate and process payments

Mana cost format examples:
    {2}{W}{W}     - 2 generic, 2 white
    {W/U}{W/U}    - 2 hybrid white/blue
    {2/W}         - hybrid 2 generic or 1 white
    {W/P}         - Phyrexian white (pay W or 2 life)
    {X}{R}{R}     - X + 2 red
    {S}           - Snow mana
    {C}           - Colorless (from Oath of the Gatewatch)
"""

import re
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Dict, List, Optional, Set, Tuple, Union
from collections import defaultdict


class ManaColor(Enum):
    """The five colors of mana plus colorless."""
    WHITE = 'W'
    BLUE = 'U'
    BLACK = 'B'
    RED = 'R'
    GREEN = 'G'
    COLORLESS = 'C'  # Specifically colorless (e.g., from Wastes)
    SNOW = 'S'  # Snow mana
    
    @classmethod
    def from_symbol(cls, symbol: str) -> Optional['ManaColor']:
        """Get ManaColor from symbol."""
        symbol = symbol.upper()
        for color in cls:
            if color.value == symbol:
                return color
        return None
    
    @classmethod
    def all_colors(cls) -> List['ManaColor']:
        """Get all five colors (not colorless/snow)."""
        return [cls.WHITE, cls.BLUE, cls.BLACK, cls.RED, cls.GREEN]


@dataclass
class ManaSymbol:
    """Represents a single mana symbol in a cost."""
    # Basic colored mana
    color: Optional[ManaColor] = None
    
    # Generic mana (any color/colorless)
    generic: int = 0
    
    # Hybrid options
    hybrid_colors: Optional[Tuple[ManaColor, ManaColor]] = None  # {W/U}
    hybrid_generic: Optional[Tuple[int, ManaColor]] = None  # {2/W}
    
    # Phyrexian (can pay 2 life instead)
    phyrexian: bool = False
    phyrexian_color: Optional[ManaColor] = None
    
    # X cost
    is_x: bool = False
    
    # Snow
    is_snow: bool = False
    
    def __str__(self) -> str:
        if self.is_x:
            return "{X}"
        if self.generic > 0:
            return f"{{{self.generic}}}"
        if self.color:
            return f"{{{self.color.value}}}"
        if self.hybrid_colors:
            return f"{{{self.hybrid_colors[0].value}/{self.hybrid_colors[1].value}}}"
        if self.hybrid_generic:
            return f"{{{self.hybrid_generic[0]}/{self.hybrid_generic[1].value}}}"
        if self.phyrexian_color:
            return f"{{{self.phyrexian_color.value}/P}}"
        if self.is_snow:
            return "{S}"
        return "{?}"
    
    def can_be_paid_with(self, mana: 'ManaColor', allow_life: bool = True) -> bool:
        """Check if this symbol can be paid with the given mana color."""
        if self.is_x:
            return True  # X can be paid with anything
        
        if self.generic > 0:
            return True  # Generic accepts any mana
        
        if self.color:
            return mana == self.color
        
        if self.hybrid_colors:
            return mana in self.hybrid_colors
        
        if self.hybrid_generic:
            # Can pay with the colored mana OR any mana (counting as generic)
            return mana == self.hybrid_generic[1] or True
        
        if self.phyrexian_color:
            # Can pay with color or life (handled separately)
            return mana == self.phyrexian_color
        
        if self.is_snow:
            # Snow mana must come from snow source (tracked separately)
            return False  # Caller needs to check snow specifically
        
        return False


@dataclass
class ManaCost:
    """Represents a complete mana cost."""
    symbols: List[ManaSymbol] = field(default_factory=list)
    
    # Cached calculations
    _cmc: Optional[int] = field(default=None, repr=False)
    _color_requirements: Optional[Dict[ManaColor, int]] = field(default=None, repr=False)
    
    @classmethod
    def parse(cls, cost_string: str) -> 'ManaCost':
        """
        Parse a mana cost string into a ManaCost object.
        
        Handles formats like:
            {2}{W}{W}
            {W/U}{W/U}
            {X}{R}{R}
            {2/W}
            {W/P}
        """
        if not cost_string:
            return cls()
        
        symbols = []
        
        # Find all symbols in braces
        pattern = r'\{([^}]+)\}'
        matches = re.findall(pattern, cost_string)
        
        for match in matches:
            symbol = cls._parse_symbol(match)
            if symbol:
                symbols.append(symbol)
        
        return cls(symbols=symbols)
    
    @classmethod
    def _parse_symbol(cls, symbol_str: str) -> Optional[ManaSymbol]:
        """Parse a single mana symbol."""
        symbol_str = symbol_str.upper()
        
        # X cost
        if symbol_str == 'X':
            return ManaSymbol(is_x=True)
        
        # Generic mana (number)
        if symbol_str.isdigit():
            return ManaSymbol(generic=int(symbol_str))
        
        # Snow
        if symbol_str == 'S':
            return ManaSymbol(is_snow=True)
        
        # Colorless
        if symbol_str == 'C':
            return ManaSymbol(color=ManaColor.COLORLESS)
        
        # Single color
        color = ManaColor.from_symbol(symbol_str)
        if color:
            return ManaSymbol(color=color)
        
        # Hybrid (W/U, W/B, etc.)
        if '/' in symbol_str:
            parts = symbol_str.split('/')
            
            # Phyrexian (W/P)
            if parts[1] == 'P':
                color = ManaColor.from_symbol(parts[0])
                if color:
                    return ManaSymbol(phyrexian=True, phyrexian_color=color)
            
            # Hybrid generic (2/W)
            if parts[0].isdigit():
                color = ManaColor.from_symbol(parts[1])
                if color:
                    return ManaSymbol(hybrid_generic=(int(parts[0]), color))
            
            # Hybrid colors (W/U)
            color1 = ManaColor.from_symbol(parts[0])
            color2 = ManaColor.from_symbol(parts[1])
            if color1 and color2:
                return ManaSymbol(hybrid_colors=(color1, color2))
        
        return None
    
    @property
    def cmc(self) -> int:
        """Calculate converted mana cost (mana value)."""
        if self._cmc is not None:
            return self._cmc
        
        total = 0
        for sym in self.symbols:
            if sym.is_x:
                continue  # X counts as 0 for CMC calculation
            if sym.generic > 0:
                total += sym.generic
            elif sym.color:
                total += 1
            elif sym.hybrid_colors:
                total += 1
            elif sym.hybrid_generic:
                total += sym.hybrid_generic[0]  # Use generic value
            elif sym.phyrexian_color:
                total += 1
            elif sym.is_snow:
                total += 1
        
        self._cmc = total
        return total
    
    @property
    def color_requirements(self) -> Dict[ManaColor, int]:
        """Get the minimum colored mana requirements."""
        if self._color_requirements is not None:
            return self._color_requirements
        
        reqs: Dict[ManaColor, int] = defaultdict(int)
        
        for sym in self.symbols:
            if sym.color:
                reqs[sym.color] += 1
            # Hybrid doesn't have strict color requirement
            # Phyrexian doesn't either (can pay life)
        
        self._color_requirements = dict(reqs)
        return self._color_requirements
    
    @property 
    def colors(self) -> Set[ManaColor]:
        """Get all colors in this mana cost."""
        colors = set()
        
        for sym in self.symbols:
            if sym.color and sym.color not in (ManaColor.COLORLESS, ManaColor.SNOW):
                colors.add(sym.color)
            if sym.hybrid_colors:
                colors.update(sym.hybrid_colors)
            if sym.hybrid_generic:
                colors.add(sym.hybrid_generic[1])
            if sym.phyrexian_color:
                colors.add(sym.phyrexian_color)
        
        return colors
    
    @property
    def generic_requirement(self) -> int:
        """Get the generic mana requirement."""
        return sum(sym.generic for sym in self.symbols)
    
    @property
    def has_x(self) -> bool:
        """Check if this cost has X in it."""
        return any(sym.is_x for sym in self.symbols)
    
    @property
    def x_count(self) -> int:
        """Count how many X's are in the cost."""
        return sum(1 for sym in self.symbols if sym.is_x)
    
    def __str__(self) -> str:
        return ''.join(str(s) for s in self.symbols)
    
    def __repr__(self) -> str:
        return f"ManaCost({str(self)})"


@dataclass
class ManaPool:
    """Represents a player's mana pool."""
    white: int = 0
    blue: int = 0
    black: int = 0
    red: int = 0
    green: int = 0
    colorless: int = 0
    
    # Track snow mana separately (it's still colored/colorless, but from snow sources)
    snow_white: int = 0
    snow_blue: int = 0
    snow_black: int = 0
    snow_red: int = 0
    snow_green: int = 0
    snow_colorless: int = 0
    
    def add(self, color: ManaColor, amount: int = 1, snow: bool = False):
        """Add mana to the pool."""
        if snow:
            attr = f"snow_{color.name.lower()}"
        else:
            attr = color.name.lower()
        
        current = getattr(self, attr, 0)
        setattr(self, attr, current + amount)
    
    def get(self, color: ManaColor, include_snow: bool = True) -> int:
        """Get amount of a color in pool."""
        base = getattr(self, color.name.lower(), 0)
        if include_snow:
            snow = getattr(self, f"snow_{color.name.lower()}", 0)
            return base + snow
        return base
    
    def get_snow(self, color: ManaColor) -> int:
        """Get amount of snow mana of a color."""
        return getattr(self, f"snow_{color.name.lower()}", 0)
    
    def total(self) -> int:
        """Get total mana in pool."""
        return (self.white + self.blue + self.black + self.red + self.green + self.colorless +
                self.snow_white + self.snow_blue + self.snow_black + 
                self.snow_red + self.snow_green + self.snow_colorless)
    
    def total_snow(self) -> int:
        """Get total snow mana in pool."""
        return (self.snow_white + self.snow_blue + self.snow_black +
                self.snow_red + self.snow_green + self.snow_colorless)
    
    def spend(self, color: ManaColor, amount: int = 1, prefer_snow: bool = False) -> bool:
        """
        Spend mana from the pool.
        
        Returns True if successful, False if not enough mana.
        """
        if prefer_snow:
            # Try snow first
            snow_attr = f"snow_{color.name.lower()}"
            snow_available = getattr(self, snow_attr, 0)
            if snow_available >= amount:
                setattr(self, snow_attr, snow_available - amount)
                return True
            # Use all snow, then regular
            if snow_available > 0:
                setattr(self, snow_attr, 0)
                amount -= snow_available
        
        # Spend from regular pool
        attr = color.name.lower()
        available = getattr(self, attr, 0)
        
        if available >= amount:
            setattr(self, attr, available - amount)
            return True
        
        # Not enough in regular, try snow
        if not prefer_snow:
            snow_attr = f"snow_{color.name.lower()}"
            snow_available = getattr(self, snow_attr, 0)
            total = available + snow_available
            
            if total >= amount:
                # Use all regular first
                setattr(self, attr, 0)
                remaining = amount - available
                setattr(self, snow_attr, snow_available - remaining)
                return True
        
        return False
    
    def clear(self):
        """Empty the mana pool (usually at end of step/phase)."""
        self.white = self.blue = self.black = self.red = self.green = self.colorless = 0
        self.snow_white = self.snow_blue = self.snow_black = 0
        self.snow_red = self.snow_green = self.snow_colorless = 0
    
    def to_dict(self) -> Dict[str, int]:
        """Convert to dictionary for serialization."""
        return {
            'W': self.white + self.snow_white,
            'U': self.blue + self.snow_blue,
            'B': self.black + self.snow_black,
            'R': self.red + self.snow_red,
            'G': self.green + self.snow_green,
            'C': self.colorless + self.snow_colorless,
            'snow': self.total_snow()
        }
    
    def __str__(self) -> str:
        parts = []
        if self.white: parts.append(f"{self.white}W")
        if self.blue: parts.append(f"{self.blue}U")
        if self.black: parts.append(f"{self.black}B")
        if self.red: parts.append(f"{self.red}R")
        if self.green: parts.append(f"{self.green}G")
        if self.colorless: parts.append(f"{self.colorless}C")
        
        snow_total = self.total_snow()
        if snow_total:
            parts.append(f"({snow_total} snow)")
        
        return ' '.join(parts) if parts else "(empty)"


@dataclass
class ManaPayment:
    """Represents how a cost was/will be paid."""
    cost: ManaCost
    
    # What mana is being used for each symbol
    assignments: List[Tuple[ManaSymbol, Union[ManaColor, int, str]]] = field(default_factory=list)
    
    # For X costs
    x_value: int = 0
    
    # Phyrexian life payments
    life_paid: int = 0
    
    # Convoke/Delve/etc. reductions
    cost_reductions: Dict[str, int] = field(default_factory=dict)
    
    def total_mana_spent(self) -> int:
        """Calculate total mana being spent."""
        return sum(
            1 if isinstance(a[1], ManaColor) else a[1] if isinstance(a[1], int) else 0
            for a in self.assignments
        ) + self.x_value * self.cost.x_count
    
    @property
    def is_valid(self) -> bool:
        """Check if this payment covers the entire cost."""
        return len(self.assignments) == len(self.cost.symbols)


class ManaPaymentValidator:
    """Validates and processes mana payments."""
    
    @staticmethod
    def can_pay(pool: ManaPool, cost: ManaCost, x_value: int = 0,
                allow_phyrexian_life: bool = True, life_total: int = 20) -> Tuple[bool, str]:
        """
        Check if a mana pool can pay a cost.

        Handles colored, hybrid, phyrexian, colorless-matters, and generic mana.
        Phyrexian mana can be paid with 2 life (if allow_phyrexian_life=True and
        life_total > 2).

        Returns (can_pay, reason).
        """
        if not cost.symbols:
            return True, "Free spell"

        # Clone pool to test payment
        test_pool = ManaPool(
            white=pool.white, blue=pool.blue, black=pool.black,
            red=pool.red, green=pool.green, colorless=pool.colorless,
            snow_white=pool.snow_white, snow_blue=pool.snow_blue,
            snow_black=pool.snow_black, snow_red=pool.snow_red,
            snow_green=pool.snow_green, snow_colorless=pool.snow_colorless
        )

        generic_needed = cost.generic_requirement
        generic_needed += x_value * cost.x_count  # X costs
        life_to_pay = 0

        # First pass: pay strict colored requirements
        for sym in cost.symbols:
            if sym.is_x:
                continue  # Handled via generic_needed above
            if sym.generic > 0:
                continue  # Handled below

            if sym.color and sym.color not in (ManaColor.COLORLESS, ManaColor.SNOW):
                if not test_pool.spend(sym.color):
                    return False, f"Not enough {sym.color.name.lower()} mana"
            elif sym.color == ManaColor.COLORLESS:
                if not test_pool.spend(ManaColor.COLORLESS):
                    return False, "Not enough colorless mana"
            elif sym.hybrid_colors:
                # Try first color, then second color
                c1, c2 = sym.hybrid_colors
                if test_pool.get(c1) > 0:
                    test_pool.spend(c1)
                elif test_pool.get(c2) > 0:
                    test_pool.spend(c2)
                else:
                    # Need generic mana for this
                    generic_needed += 1
            elif sym.hybrid_generic:
                # Can pay with the color or generic amount
                gen_amount, color = sym.hybrid_generic
                if test_pool.get(color) > 0:
                    test_pool.spend(color)
                else:
                    generic_needed += gen_amount
            elif sym.phyrexian and sym.phyrexian_color:
                # Phyrexian: pay with color OR 2 life
                if test_pool.get(sym.phyrexian_color) > 0:
                    test_pool.spend(sym.phyrexian_color)
                elif allow_phyrexian_life and life_total - life_to_pay > 2:
                    life_to_pay += 2
                else:
                    return False, f"Not enough {sym.phyrexian_color.name.lower()} mana (Phyrexian)"
            elif sym.is_snow:
                if test_pool.total_snow() > 0:
                    # Spend any snow mana
                    for sc in ManaColor.all_colors() + [ManaColor.COLORLESS]:
                        if test_pool.get_snow(sc) > 0:
                            test_pool.spend(sc, prefer_snow=True)
                            break
                else:
                    return False, "Not enough snow mana"

        # Pay generic with remaining mana
        if test_pool.total() < generic_needed:
            return False, f"Not enough mana for generic cost (need {generic_needed}, have {test_pool.total()})"

        return True, "Can pay"
    
    @staticmethod
    def auto_pay(pool: ManaPool, cost: ManaCost, x_value: int = 0) -> Optional[ManaPayment]:
        """
        Automatically pay a mana cost from a pool.
        
        Uses a simple strategy:
        1. Pay colored requirements with matching colors
        2. Pay generic with most abundant color
        
        Returns None if cannot pay.
        """
        can_pay, reason = ManaPaymentValidator.can_pay(pool, cost, x_value)
        if not can_pay:
            return None
        
        payment = ManaPayment(cost=cost, x_value=x_value)
        
        # Pay colored requirements
        for sym in cost.symbols:
            if sym.color and sym.color not in (ManaColor.COLORLESS, ManaColor.SNOW):
                pool.spend(sym.color)
                payment.assignments.append((sym, sym.color))
            elif sym.color == ManaColor.COLORLESS:
                pool.spend(ManaColor.COLORLESS)
                payment.assignments.append((sym, ManaColor.COLORLESS))
            elif sym.is_x:
                # X is paid with generic later
                payment.assignments.append((sym, "X"))
        
        # Pay generic costs
        generic_needed = cost.generic_requirement + (x_value * cost.x_count)
        
        for sym in cost.symbols:
            if sym.generic > 0:
                paid = ManaPaymentValidator._pay_generic(pool, sym.generic)
                payment.assignments.append((sym, paid))
        
        # Pay X value
        if cost.has_x and x_value > 0:
            for _ in range(cost.x_count):
                ManaPaymentValidator._pay_generic(pool, x_value)
        
        return payment
    
    @staticmethod
    def _pay_generic(pool: ManaPool, amount: int) -> int:
        """Pay generic mana cost, preferring least useful colors."""
        # Prefer colorless first
        paid = 0
        
        # Order of preference: Colorless, then alphabetically
        colors = [ManaColor.COLORLESS, ManaColor.BLACK, ManaColor.BLUE, 
                  ManaColor.GREEN, ManaColor.RED, ManaColor.WHITE]
        
        for color in colors:
            while pool.get(color) > 0 and paid < amount:
                pool.spend(color)
                paid += 1
        
        return paid


# =============================================================================
# Land Mana Production
# =============================================================================

# Basic land mana production
BASIC_LAND_MANA = {
    'Plains': ManaColor.WHITE,
    'Island': ManaColor.BLUE,
    'Swamp': ManaColor.BLACK,
    'Mountain': ManaColor.RED,
    'Forest': ManaColor.GREEN,
    'Wastes': ManaColor.COLORLESS,
    # Snow basics
    'Snow-Covered Plains': ManaColor.WHITE,
    'Snow-Covered Island': ManaColor.BLUE,
    'Snow-Covered Swamp': ManaColor.BLACK,
    'Snow-Covered Mountain': ManaColor.RED,
    'Snow-Covered Forest': ManaColor.GREEN,
}

SNOW_LANDS = {
    'Snow-Covered Plains', 'Snow-Covered Island', 'Snow-Covered Swamp',
    'Snow-Covered Mountain', 'Snow-Covered Forest'
}


def get_land_mana(land_name: str) -> List[Tuple[ManaColor, bool]]:
    """
    Get what mana a land can produce.
    
    Returns list of (color, is_snow) tuples.
    """
    is_snow = land_name in SNOW_LANDS or 'Snow' in land_name
    
    if land_name in BASIC_LAND_MANA:
        return [(BASIC_LAND_MANA[land_name], is_snow)]
    
    # For nonbasic lands, would need to look up in database
    # This is where Scryfall's 'produced_mana' field is useful
    return []


# =============================================================================
# Integration with Scryfall
# =============================================================================

def parse_scryfall_mana(scryfall_card: dict) -> ManaCost:
    """Parse mana cost from Scryfall card data."""
    mana_cost = scryfall_card.get('mana_cost', '')
    return ManaCost.parse(mana_cost)


def get_produced_mana(scryfall_card: dict) -> List[ManaColor]:
    """Get what mana a card can produce from Scryfall data."""
    produced = scryfall_card.get('produced_mana', [])
    colors = []
    
    for symbol in produced:
        color = ManaColor.from_symbol(symbol)
        if color:
            colors.append(color)
    
    return colors


# =============================================================================
# Demo / Test
# =============================================================================

def demo():
    """Demo the mana system."""
    print("=== Mana System Demo ===\n")
    
    # Parse various mana costs
    test_costs = [
        "{2}{W}{W}",      # Serra Angel
        "{U}{U}",         # Counterspell  
        "{X}{R}{R}",      # Fireball
        "{W/U}{W/U}",     # Hybrid
        "{2/W}{2/W}",     # Spectral Procession
        "{W/P}",          # Phyrexian
        "{S}{S}{S}",      # Snow cost
        "{C}{C}",         # Colorless (Eldrazi)
        "",               # Free spell
    ]
    
    print("Parsing mana costs:")
    for cost_str in test_costs:
        cost = ManaCost.parse(cost_str)
        print(f"  {cost_str or '(none)':<15} → CMC: {cost.cmc}, Colors: {[c.value for c in cost.colors]}")
    
    print()
    
    # Test mana pools and payment
    print("Mana pool and payment test:")
    
    pool = ManaPool()
    pool.add(ManaColor.WHITE, 3)
    pool.add(ManaColor.BLUE, 2)
    pool.add(ManaColor.RED, 1)
    print(f"  Pool: {pool}")
    
    # Can we cast Counterspell?
    counterspell = ManaCost.parse("{U}{U}")
    can_pay, reason = ManaPaymentValidator.can_pay(pool, counterspell)
    print(f"  Can cast Counterspell ({counterspell})? {can_pay} - {reason}")
    
    # Can we cast Serra Angel?
    serra = ManaCost.parse("{3}{W}{W}")
    can_pay, reason = ManaPaymentValidator.can_pay(pool, serra)
    print(f"  Can cast Serra Angel ({serra})? {can_pay} - {reason}")
    
    # Auto-pay Counterspell
    print("\n  Auto-paying Counterspell...")
    payment = ManaPaymentValidator.auto_pay(pool, counterspell)
    if payment:
        print(f"    Payment valid: {payment.is_valid}")
        print(f"    Pool after: {pool}")
    
    print()
    
    # Test X costs
    print("X cost test:")
    fireball = ManaCost.parse("{X}{R}")
    pool2 = ManaPool(red=3, green=2)
    print(f"  Pool: {pool2}")
    print(f"  Fireball cost: {fireball}")
    
    for x in range(5):
        can_pay, _ = ManaPaymentValidator.can_pay(pool2, fireball, x_value=x)
        print(f"    X={x}: {'✓' if can_pay else '✗'}")
    
    print()
    
    # Test hybrid
    print("Hybrid mana test:")
    hybrid = ManaCost.parse("{W/U}{W/U}{W/U}")
    print(f"  Cost: {hybrid}")
    
    # All white
    pool3 = ManaPool(white=3)
    can_pay, _ = ManaPaymentValidator.can_pay(pool3, hybrid)
    print(f"    3W pool: {'✓' if can_pay else '✗'}")
    
    # All blue
    pool4 = ManaPool(blue=3)
    can_pay, _ = ManaPaymentValidator.can_pay(pool4, hybrid)
    print(f"    3U pool: {'✓' if can_pay else '✗'}")
    
    # Mixed
    pool5 = ManaPool(white=2, blue=1)
    can_pay, _ = ManaPaymentValidator.can_pay(pool5, hybrid)
    print(f"    2W 1U pool: {'✓' if can_pay else '✗'}")
    
    print("\n=== Demo Complete ===")


if __name__ == "__main__":
    demo()
