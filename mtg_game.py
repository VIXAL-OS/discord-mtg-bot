"""mtg_game.py — backward-compatibility shim for the mtg/ package.

The MTG engine used to live as a single 26,677-line file at this path.
It has been split into focused modules under mtg/ during the Phase 1
OSS-readability refactor. This shim re-exports the public API so that
existing imports keep working unchanged:

    from mtg_game import Card, Player, GameState        # → mtg.models
    from mtg_game import Phase, Zone, FORMAT_*           # → mtg.constants
    from mtg_game import GameLogger, StdoutTee, StderrTee  # → mtg.util
    from mtg_game import DeckLoader                     # → mtg.deck_loader
    from mtg_game import GameDisplay                    # → mtg.display
    from mtg_game import ClaudePlayer                   # → mtg.claude_player
    from mtg_game import RulesEngine                    # → mtg.rules_engine
    from mtg_game import GameEngine                     # → mtg.engine
    from mtg_game import MTGGameCog, setup              # → mtg.cog
    from mtg_game import get_mdfc_info, ...              # → mtg.helpers

For new code, prefer importing directly from the mtg.* submodules — the
shim is here to keep bot.py, board_visual.py, cube_draft.py,
prewarm_cache.py, and the rules/ legacy callers working without edits.

`bot.load_extension('mtg_game')` still works: discord.py finds the
re-exported `setup` function below and calls it, which adds MTGGameCog
to the bot.

See mtg/__init__.py for the full module layout and Phase 2 plan.
"""

# Constants + enums
from mtg.constants import (
    Phase, Zone, PHASE_ORDER, PHASE_NAMES,
    FORMAT_STARTING_LIFE, FORMAT_DECK_SIZE,
    SINGLETON_FORMATS, COMMAND_ZONE_FORMATS,
    MELD_PAIRS, BASIC_LAND_NAMES, BANNED_CARDS, MANA_COLOR_IDENTITY,
    MDFC_PATHWAYS,
)

# Utilities
from mtg.util import GameLogger, StdoutTee, StderrTee

# Module-level helpers
from mtg.helpers import (
    get_mdfc_info,
    _normalize_pw_ability_idx,
    _resolve_player_or_card_target,
    _collapse_repeated_life_gain,
    _should_emit_resolve_hint,
)

# Data model
from mtg.models import (
    FormatValidator, Card, Player, StackEntry, GameState,
)

# I/O + display
from mtg.deck_loader import DeckLoader
from mtg.display import GameDisplay

# AI + engines
from mtg.claude_player import ClaudePlayer
from mtg.rules_engine import RulesEngine
from mtg.engine import GameEngine

# Coverage diagnostic (Architectural Readability item #5 — supported_at_tier)
from mtg.coverage import supported_at_tier, classify_deck, format_coverage_report

# Discord cog (also re-exports `setup` so bot.load_extension('mtg_game') works)
from mtg.cog import MTGGameCog, setup
