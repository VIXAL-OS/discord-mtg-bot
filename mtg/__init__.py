"""mtg/ — Magic: The Gathering game engine package.

This package replaces the old monolithic mtg_game.py. Each module owns a
single concern so a contributor can browse the structure rather than grep
through 26k lines.

Phase 1 + Phase 2 layout (post Apr 26-27, 2026 refactor):

    util.py          — logging utilities (GameLogger, StdoutTee, StderrTee)
    constants.py     — phase/zone enums, format rules, banned lists,
                       MDFC pathways, basic land names, color identity map,
                       keyword list
    helpers.py       — module-level helpers shared between engine + cog
                       (get_mdfc_info, _normalize_pw_ability_idx,
                       _resolve_player_or_card_target, etc.)
    models.py        — Card, Player, GameState, StackEntry, FormatValidator
    deck_loader.py   — DeckLoader (Scryfall + Archidekt + JSON)
    display.py       — GameDisplay (Discord embed/text formatters)
    claude_player.py — ClaudePlayer (AI decision-making, strategist + actor)
    rules_engine.py  — RulesEngine (orchestrator; delegates to sub-modules)
    actions.py       — _execute_action_on_state + 81-action JSON interpreter
    judge.py         — Tier 3 Claude judge (resolve_effect, ask_judge)
    sba.py           — State-based actions (CR 704)
    combat.py        — Combat damage + lifelink + life-gain math
    triggers.py      — ETB/dies/LTB/attack/upkeep/end-step/cast/landfall scans
    spells.py        — cast_spell_async, resolve_special_effects, suspend, sagas
    ai_turn.py       — execute_claude_turn, plan validation
    autoplay.py      — Rick logic, autoplay loop
    engine.py        — GameEngine (turn loop; delegates to spells/triggers/ai_turn)
    cog.py           — MTGGameCog (Discord command handlers) + setup()
    coverage.py      — supported_at_tier() — OSS-readability informative
                       coverage check (no UI side effects)

For backward compatibility, mtg_game.py at the project root is now a
66-line re-export shim — existing imports like `from mtg_game import Card,
MTGGameCog` keep working without changes.

------------------------------------------------------------------------
Rules engine availability check (startup log)
------------------------------------------------------------------------
On first import of any mtg.* submodule we probe the optional rules/
subsystems and print one summary line. Each submodule does its own
try/except for the flags it actually uses; this is purely a human-facing
"is everything loaded?" log line.
"""

# ----------------------------------------------------------------------
# Optional rules/ subsystem probe — emit a single startup summary line.
# ----------------------------------------------------------------------
def _probe_rules_modules():
    flags = []
    try:
        from rules import SpellResolver  # noqa: F401
        flags.append("spell_resolver")
    except ImportError:
        pass
    try:
        from rules.effect_templates import get_effect_library  # noqa: F401
        flags.append("templates")
    except ImportError:
        pass
    try:
        from rules.planeswalker import PlaneswalkerManager  # noqa: F401
        flags.append("planeswalker")
    except ImportError:
        pass
    try:
        from rules.xmage_bridge import XMageBridge  # noqa: F401
        flags.append("xmage")
    except ImportError:
        pass
    try:
        from rules.layers import LayersEngine  # noqa: F401
        flags.append("layers")
    except ImportError:
        pass
    try:
        from rules.replacement import ReplacementEngine  # noqa: F401
        flags.append("replacement")
    except ImportError:
        pass
    try:
        from rules.targeting import TargetValidator  # noqa: F401
        flags.append("targeting")
    except ImportError:
        pass
    try:
        from rules.mana import ManaCost  # noqa: F401
        flags.append("mana")
    except ImportError:
        pass
    try:
        from rules.sba_adapter import compare_with_rules_sba  # noqa: F401
        flags.append("sba")
    except ImportError:
        pass
    print(f"✅ Rules engine: {len(flags)}/9 modules loaded ({', '.join(flags)})")


_probe_rules_modules()
del _probe_rules_modules
