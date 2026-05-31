"""
MTG Rules Engine
================

Enhanced rules enforcement for Magic: The Gathering.

Modules:
    - effects: Effect parsing and execution (ActionType enum, formerly EffectType)
    - keywords: MTG keyword abilities enum (Flying, Deathtouch, etc.)
    - targeting: Target validation with protection/hexproof
    - priority: Stack management and priority passing
    - mana: Mana cost parsing and payment validation
    - state_based_actions: SBA checking and execution
    - spell_resolver: Integrated spell casting and resolution
    - effect_templates: Data-driven ETB/trigger template library (tier 1.5)
                        + TokenDefinition registry, word_to_num utility
    - replacement: Replacement effects ("if would, instead") processing
    - layers: 7-layer continuous effect ordering
    - planeswalker: Planeswalker ability activation
    - xmage_bridge: Python client for XMage Java bridge (tier 2.5)
    - xmage_action_translator: XMage ability text → JSON action translation (tier 2.5)
"""

# Only re-export names that are actually imported via `from rules import X`.
# All other modules are imported directly from submodules (e.g., `from rules.layers import LayersEngine`).
from .spell_resolver import SpellResolver, TargetMode
from .effects import ExecutionContext

__all__ = [
    'SpellResolver',
    'TargetMode',
    'ExecutionContext',
]
