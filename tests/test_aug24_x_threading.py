"""The x_value threading verification, and what it licensed.

The method, because "verify the threading first" needed one:

  1. find every producer of ctx['x_value'];
  2. determine which producer each card's cast path actually reaches;
  3. DRIVE the real template with and without the stamp and compare.

Step 3 is decisive -- it is the difference between reading the code and
knowing what it does. The answer differed PER CARD, which is why the blanket
"leave all three floors" was too coarse.
"""
import pytest

from mtg.models import Card, GameState, Player
from rules.effect_templates import build_game_context, get_effect_library

BSZ_TEXT = ("Target player draws X cards. Shuffle Blue Sun's Zenith into "
            "its owner's library.")
DELUGE_TEXT = ("As an additional cost to cast this spell, pay X life. "
               "Each creature gets -X/-X until end of turn.")


def _game():
    return GameState(thread_id=1, format="modern",
                     players=[Player(name="Alice", user_id=1, life=20),
                              Player(name="Bob", user_id=2, life=20)])


def _resolve(game, name, oracle, mana_cost, x=None):
    card = Card(name=name, id="c", type_line="Sorcery",
                mana_cost=mana_cost, oracle_text=oracle)
    if x is not None:
        card._x_value = x
    ctx = build_game_context(game, game.players[0], game.players[1], card=card)
    actions, _ = get_effect_library().resolve_spell(
        name, oracle, "Alice", "Bob", ctx)
    return actions or []


class TestTheThreadingItself:
    """Step 3: the stamp reaches the template, or it does not."""

    def test_a_stamped_x_reaches_the_template(self):
        acts = _resolve(_game(), "Blue Sun's Zenith", BSZ_TEXT,
                        "{X}{U}{U}{U}", x=7)
        assert [a["amount"] for a in acts if a["action"] == "draw_cards"] == [7]

    def test_an_unstamped_cast_leaves_x_absent(self):
        """Which is exactly what the floors were standing in for."""
        game = _game()
        card = Card(name="Blue Sun's Zenith", id="c", type_line="Sorcery",
                    mana_cost="{X}{U}{U}{U}", oracle_text=BSZ_TEXT)
        ctx = build_game_context(game, game.players[0], game.players[1],
                                 card=card)
        assert "x_value" not in ctx


class TestBlueSunsZenith:
    """{X}{U}{U}{U} -- X is in the MANA COST, and cast_spell_async stamps
    _x_value unconditionally for that shape. The floor could never fire, so
    it was dead code, and it is gone."""

    def test_x_is_honoured_exactly(self):
        acts = _resolve(_game(), "Blue Sun's Zenith", BSZ_TEXT,
                        "{X}{U}{U}{U}", x=1)
        assert [a["amount"] for a in acts if a["action"] == "draw_cards"] == [1]

    def test_x_zero_draws_nothing_rather_than_one(self):
        """The floor's old behaviour: X=0 drew a card the spell did not."""
        acts = _resolve(_game(), "Blue Sun's Zenith", BSZ_TEXT,
                        "{X}{U}{U}{U}", x=0)
        assert [a["amount"] for a in acts if a["action"] == "draw_cards"] == [0]

    def test_the_cast_path_stamps_any_x_in_the_mana_cost(self):
        """The property that made removal safe. Structural, because the stamp
        lives inside cast_spell_async's X block."""
        import inspect

        import mtg.spells as spells_mod
        src = inspect.getsource(spells_mod)
        assert "card._x_value = x_value_chosen" in src
        assert "if effective_mana_cost and 'X' in effective_mana_cost.upper():" in src


class TestToxicDeluge:
    """{2}{B} -- X is "pay X life", an ADDITIONAL cost, so it never reaches
    that branch. The floor is genuinely load-bearing; a flat 4 was not the
    right stand-in, so X is derived instead."""

    def test_an_explicit_x_still_wins(self):
        acts = _resolve(_game(), "Toxic Deluge", DELUGE_TEXT, "{2}{B}", x=6)
        assert [a["amount"] for a in acts if a["action"] == "lose_life"] == [6]

    def test_x_is_derived_to_kill_the_biggest_opposing_creature(self):
        game = _game()
        game.players[1].battlefield.append(Card(
            name="Big", id="big", type_line="Creature — Beast",
            power="5", toughness="5"))
        game.players[1].battlefield.append(Card(
            name="Small", id="small", type_line="Creature — Bird",
            power="1", toughness="1"))

        acts = _resolve(game, "Toxic Deluge", DELUGE_TEXT, "{2}{B}")

        assert [a["amount"] for a in acts if a["action"] == "lose_life"] == [5]

    def test_it_never_pays_more_life_than_it_has(self):
        """CR 118.4, and never the last point."""
        game = _game()
        game.players[0].life = 3
        game.players[1].battlefield.append(Card(
            name="Huge", id="huge", type_line="Creature — Wurm",
            power="9", toughness="9"))

        acts = _resolve(game, "Toxic Deluge", DELUGE_TEXT, "{2}{B}")

        assert [a["amount"] for a in acts if a["action"] == "lose_life"] == [2]

    def test_an_empty_opposing_board_pays_nothing(self):
        """-0/-0 is the correct resolution here, not a silent failure: there
        is nothing to kill, so a flat 4 was pure self-damage."""
        acts = _resolve(_game(), "Toxic Deluge", DELUGE_TEXT, "{2}{B}")
        assert [a["amount"] for a in acts if a["action"] == "lose_life"] == [0]

    def test_noncreature_permanents_do_not_raise_x(self):
        """'creature' is a substring of 'noncreature' -- the trap this
        codebase has hit six times."""
        game = _game()
        game.players[1].battlefield.append(Card(
            name="Rock", id="rock", type_line="Artifact", toughness="0"))
        acts = _resolve(game, "Toxic Deluge", DELUGE_TEXT, "{2}{B}")
        assert [a["amount"] for a in acts if a["action"] == "lose_life"] == [0]


class TestChandraFlamecallerStaysHeld:
    def test_it_is_in_no_deck_and_on_a_different_path(self):
        """Its floor stays, now recorded as unreachable-and-unverified rather
        than merely unverified: absent from the card cache entirely, and its
        X comes from the planeswalker ACTIVATION path, about which the
        spell-cast verification says nothing."""
        import json

        cache = json.load(open("data/card_data_cache.json", encoding="utf-8"))
        assert "chandra, flamecaller" not in cache

        import inspect

        import rules.effect_templates as tmpl
        src = inspect.getsource(tmpl)
        assert "Verify that path before dropping this floor." in src
