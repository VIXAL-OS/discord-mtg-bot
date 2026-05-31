"""
MTG Keyword Abilities
======================

The things MTG players actually call "effects" — Flying, Deathtouch, etc.

Using str(Enum) so Keyword.FLYING == "Flying" is True, which means all
existing string comparisons in the codebase keep working with zero changes.
The enum just provides a canonical list and IDE autocomplete.

Usage:
    from rules.keywords import Keyword

    # String comparison still works
    if "Flying" in card.keywords:  # unchanged
        ...

    # But now you can also do
    if Keyword.FLYING in card.keywords:
        ...

    # And get the full list
    all_keywords = list(Keyword)
"""

from enum import Enum


class Keyword(str, Enum):
    """MTG keyword abilities — the things MTG players actually call 'effects'.

    Inherits from str so Keyword.FLYING == "Flying" is True. This means
    all existing string-based keyword checks work without modification.
    """
    FLYING = "Flying"
    FIRST_STRIKE = "First strike"
    DOUBLE_STRIKE = "Double strike"
    DEATHTOUCH = "Deathtouch"
    HASTE = "Haste"
    HEXPROOF = "Hexproof"
    INDESTRUCTIBLE = "Indestructible"
    LIFELINK = "Lifelink"
    MENACE = "Menace"
    REACH = "Reach"
    TRAMPLE = "Trample"
    VIGILANCE = "Vigilance"
    FLASH = "Flash"
    DEFENDER = "Defender"
    FEAR = "Fear"
    INTIMIDATE = "Intimidate"
    SKULK = "Skulk"
    PROTECTION = "Protection"
    SHROUD = "Shroud"
    UNBLOCKABLE = "Unblockable"
    WITHER = "Wither"
    INFECT = "Infect"
    PERSIST = "Persist"
    UNDYING = "Undying"
    PROWESS = "Prowess"
    EXALTED = "Exalted"
    FLANKING = "Flanking"
