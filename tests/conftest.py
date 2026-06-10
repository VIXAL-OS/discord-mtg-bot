"""Shared fixtures for the engine test suite.

Design notes:
- No Discord, no Scryfall, no LLM, no network. Everything here builds bare
  dataclasses (Card / Player / GameState) and a clientless RulesEngine,
  which is enough for the deterministic tiers: the template library, the
  JSON action interpreter, replacement effects, SBA, layers, mana.
- Player names follow the autoplay convention: "Rick" is the pretend-human
  (player 0), "Claude" the AI (player 1) — tests read like the log corpus.
- make_card gives every card a unique id. Several engine paths look cards
  up by identity, and same-name tokens are a known historical bug class
  (May 13: thirty-one counters collapsing onto one mega-Soldier).
"""
import itertools
import sys
from pathlib import Path

# Repo root on sys.path so `import mtg` works no matter where pytest runs from.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from mtg.models import Card, Player, GameState  # noqa: E402


_card_ids = itertools.count(1)

_STARTING_LIFE = {"commander": 40, "edh": 40, "brawl": 25}


def _make_card(name: str, **overrides) -> Card:
    defaults = dict(
        id=f"test_card_{next(_card_ids)}",
        type_line="Creature — Bear",
        oracle_text="",
        power="2",
        toughness="2",
        summoning_sick=False,
    )
    defaults.update(overrides)
    return Card(name=name, **defaults)


def _make_game(fmt: str = "commander") -> GameState:
    life = _STARTING_LIFE.get(fmt, 20)
    rick = Player(name="Rick", user_id=99999, life=life)
    claude = Player(name="Claude", user_id=None, is_claude=True, life=life)
    game = GameState(thread_id=0, format=fmt, players=[rick, claude])
    game.turn_number = 1
    return game


@pytest.fixture
def make_card():
    """Factory: make_card("Name", type_line=..., oracle_text=..., ...)."""
    return _make_card


@pytest.fixture
def make_game():
    """Factory: make_game() -> commander game; make_game("modern") -> 20 life."""
    return _make_game


@pytest.fixture
def game():
    """A fresh two-player commander game (Rick vs Claude, 40 life each)."""
    return _make_game()


@pytest.fixture
def rules():
    """A clientless RulesEngine — enough for every deterministic action path.

    Tier 3 / judge paths would need a real Anthropic client; tests must
    never reach them. engine_ref (trigger fan-out into GameEngine) is
    deliberately absent — every handler hasattr-guards it.
    """
    from mtg.rules_engine import RulesEngine
    return RulesEngine(None)


@pytest.fixture(scope="session")
def lib():
    """The Tier 1.5 effect template library singleton (~370 card templates)."""
    from rules.effect_templates import get_effect_library
    return get_effect_library()
