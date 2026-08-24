"""Sunforger's effect half — held on Aug 10 until its cost existed.

The ordering was the point: 'Unattach this Equipment' was recognised as a
cost keyword by NEITHER activation path, so shipping the effect first would
have turned a dead card into a repeatable free-instant engine (once per turn,
forever, with nothing consuming the activation). The cost landed first.
"""
import pytest

from mtg.constants import Zone
from mtg.helpers import sunforger_search_and_free_cast
from mtg.models import Card, GameState, Player

SUNFORGER = (
    "Equipped creature gets +4/+0.\n"
    "{R}{W}, Unattach this Equipment: Search your library for a red or white "
    "instant card with mana value 4 or less and cast that card without "
    "paying its mana cost. Then shuffle.\n"
    "Equip {3}")


def _game():
    return GameState(thread_id=1, format="modern",
                     players=[Player(name="Alice", user_id=1, life=20),
                              Player(name="Bob", user_id=2, life=20)])


def _sunforger(game, seat=0):
    c = Card(name="Sunforger", id="sf", type_line="Artifact — Equipment",
             oracle_text=SUNFORGER)
    game.players[seat].battlefield.append(c)
    return c


def _lib(game, seat, *cards):
    game.players[seat].library.extend(cards)


class TestSunforgerSearch:
    def test_it_finds_a_legal_instant_and_queues_a_free_cast(self):
        game = _game()
        sf = _sunforger(game)
        bolt = Card(name="Lightning Helix", id="helix", type_line="Instant",
                    mana_cost="{R}{W}", cmc=2)
        _lib(game, 0, bolt)

        msg, handled = sunforger_search_and_free_cast(game, game.players[0], sf)

        assert handled and "Lightning Helix" in msg
        assert len(game._free_cast_pending) == 1
        queued = game._free_cast_pending[0]
        assert queued["card"] is bolt
        assert queued["from_zone"] == "library"
        assert queued["source"] == "Sunforger"

    def test_it_respects_the_printed_mana_value_cap(self):
        """The card says mana value 4 or less. The register said 3 -- the
        cache is the authority, and this is why oracle claims get checked."""
        game = _game()
        sf = _sunforger(game)
        too_big = Card(name="Expensive Instant", id="big", type_line="Instant",
                       mana_cost="{4}{R}", cmc=5)
        _lib(game, 0, too_big)

        msg, handled = sunforger_search_and_free_cast(game, game.players[0], sf)

        assert handled, "a search that finds nothing still resolves"
        assert game._free_cast_pending == []
        assert "no matching" in msg

    def test_a_four_drop_is_inside_the_cap(self):
        game = _game()
        sf = _sunforger(game)
        exactly = Card(name="Four Drop", id="four", type_line="Instant",
                       mana_cost="{3}{R}", cmc=4)
        _lib(game, 0, exactly)

        _, handled = sunforger_search_and_free_cast(game, game.players[0], sf)

        assert handled and len(game._free_cast_pending) == 1

    def test_it_respects_the_printed_colours(self):
        game = _game()
        sf = _sunforger(game)
        blue = Card(name="Counterspell", id="cs", type_line="Instant",
                    mana_cost="{U}{U}", cmc=2)
        _lib(game, 0, blue)

        msg, handled = sunforger_search_and_free_cast(game, game.players[0], sf)

        assert handled and game._free_cast_pending == []
        assert "no matching" in msg

    def test_it_respects_the_printed_card_type(self):
        game = _game()
        sf = _sunforger(game)
        sorcery = Card(name="Wrath of God", id="wog", type_line="Sorcery",
                       mana_cost="{2}{W}{W}", cmc=4)
        _lib(game, 0, sorcery)

        _, handled = sunforger_search_and_free_cast(game, game.players[0], sf)

        assert handled and game._free_cast_pending == []

    def test_it_takes_the_biggest_legal_card(self):
        game = _game()
        sf = _sunforger(game)
        small = Card(name="Shock", id="shock", type_line="Instant",
                     mana_cost="{R}", cmc=1)
        big = Card(name="Boros Charm", id="charm", type_line="Instant",
                   mana_cost="{2}{R}{W}", cmc=4)
        _lib(game, 0, small, big)

        sunforger_search_and_free_cast(game, game.players[0], sf)

        assert game._free_cast_pending[0]["card"] is big

    def test_an_unrelated_permanent_is_not_claimed(self):
        """The helper must decline anything that is not this shape, or it
        becomes a general free-cast pattern -- the worst direction to be
        wrong in."""
        game = _game()
        rock = Card(name="Mind Stone", id="ms", type_line="Artifact",
                    oracle_text="{T}: Add {C}.")
        game.players[0].battlefield.append(rock)
        _lib(game, 0, Card(name="Shock", id="s", type_line="Instant",
                           mana_cost="{R}", cmc=1))

        msg, handled = sunforger_search_and_free_cast(game, game.players[0], rock)

        assert not handled and msg is None
        assert game._free_cast_pending == []


class TestBothActivationPathsAreWired:
    """These two paths have a documented history of diverging, and the whole
    reason the cost half was a bug is that NEITHER of them knew 'unattach'."""

    def test_the_ai_path_calls_the_helper(self):
        import inspect

        import mtg.engine as engine_mod
        assert "sunforger_search_and_free_cast" in inspect.getsource(engine_mod)

    def test_the_manual_path_calls_the_helper(self):
        import inspect

        import mtg.cog as cog_mod
        assert "sunforger_search_and_free_cast" in inspect.getsource(cog_mod)

    def test_the_free_cast_queue_has_a_real_drain(self):
        """A queued free cast with no consumer would be a silent no-op --
        the shape this codebase keeps rediscovering as dead code."""
        import inspect

        import mtg.spells as spells_mod
        assert "_free_cast_pending" in inspect.getsource(spells_mod)
