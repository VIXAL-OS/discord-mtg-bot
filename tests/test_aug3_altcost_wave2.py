"""Aug 3, 2026 — the alternate-cost wave, part 2: the DRAW-time mechanics.

Miracle (CR 702.94) and dredge (CR 702.52) shipped together and separately
from wave 1 because both hook the draw, which is genuinely new machinery:
dredge REPLACES the draw (it runs before the card leaves the library),
miracle reacts to it (the card must be in hand and be the turn's first).

Oracle text is the REAL printed text (Scryfall-verified this session).
"""
import asyncio

import pytest

from mtg import helpers
from mtg.engine import GameEngine
from mtg.spells import resolve_pending_miracles

from tests.conftest import _make_card, _make_game


TERMINUS = ("Put all creatures on the bottom of their owners' libraries.\n"
            "Miracle {W} (You may cast this card for its miracle cost when "
            "you draw it if it's the first card you drew this turn.)")
ENTREAT = ("Create X 4/4 white Angel creature tokens with flying.\n"
           "Miracle {X}{W}{W} (You may cast this card for its miracle cost "
           "when you draw it if it's the first card you drew this turn.)")
LOAM = ("Return up to three target land cards from your graveyard to your "
        "hand.\nDredge 3 (If you would draw a card, you may mill three cards "
        "instead. If you do, return this card from your graveyard to your "
        "hand.)")
STINKWEED_IMP = ("Flying\n"
                 "Whenever this creature deals combat damage to a creature, "
                 "destroy that creature.\n"
                 "Dredge 5 (If you would draw a card, you may mill five "
                 "cards instead. If you do, return this card from your "
                 "graveyard to your hand.)")


def _engine(game):
    engine = GameEngine(None)
    game._rules_engine = engine.rules
    engine.rules.engine_ref = engine
    return engine


def _lands(player, n, name="Plains", sym="W"):
    for _ in range(n):
        player.battlefield.append(_make_card(
            name, type_line=f"Basic Land — {name}",
            oracle_text="{T}: Add {%s}." % sym))


def _stock(player, n=40):
    for i in range(n):
        player.library.append(_make_card(f"Lib {i}", type_line="Creature"))


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _terminus():
    return _make_card("Terminus", type_line="Sorcery", oracle_text=TERMINUS,
                      mana_cost="{4}{W}{W}")


def _loam():
    return _make_card("Life from the Loam", type_line="Sorcery",
                      oracle_text=LOAM, mana_cost="{1}{G}")


class TestParsers:
    def test_miracle_and_dredge_costs(self):
        assert helpers.parse_miracle(TERMINUS) == "{W}"
        assert helpers.parse_miracle(ENTREAT) == "{X}{W}{W}"
        assert helpers.parse_dredge(LOAM) == 3
        assert helpers.parse_dredge(STINKWEED_IMP) == 5

    def test_grants_are_not_read_as_the_source_s_own(self):
        assert helpers.parse_miracle(
            "Each instant and sorcery card in your hand has miracle {2}.") is None
        assert helpers.parse_dredge(
            "Land cards in your graveyard have dredge 2.") is None


class TestMiracle:
    def _game_with_terminus_on_top(self, opp_creatures=1, plains=1):
        # ONE Plains by default, deliberately: that pays the miracle cost
        # {W} and cannot pay the printed {4}{W}{W}. Every gate in the miracle
        # path is a cost-SELECTION gate, so a fixture with six lands would
        # pass whichever cost the code picked — mutation testing caught
        # exactly that on the pre-gate.
        game = _make_game()
        rick, claude = game.players
        game.active_player_index = 0
        engine = _engine(game)
        _lands(rick, plains)
        _stock(rick)
        term = _terminus()
        rick.library.insert(0, term)
        for i in range(opp_creatures):
            claude.battlefield.append(_make_card(f"Bear {i}",
                                                 type_line="Creature",
                                                 power="2", toughness="2"))
        return game, rick, claude, engine, term

    def test_first_draw_of_the_turn_makes_it_pending(self):
        game, rick, _c, engine, term = self._game_with_terminus_on_top()
        engine.draw_cards(rick, 1, game)
        assert rick.cards_drawn_this_turn == 1
        assert [c.name for c, _ in game._miracle_pending] == ["Terminus"]

    def test_cast_for_the_miracle_cost_not_the_printed_one(self):
        game, rick, claude, engine, term = self._game_with_terminus_on_top()
        engine.draw_cards(rick, 1, game)
        msgs = _run(resolve_pending_miracles(engine, game))
        assert any("miracle cost" in m for m in msgs), msgs
        assert term._mana_paid == 1, (
            "Terminus is {4}{W}{W} printed and {W} for its miracle cost")
        assert not claude.battlefield, "and it actually resolved"

    def test_not_the_second_card_drawn(self):
        # CR 702.94a is specifically the FIRST card drawn this turn. Decisive:
        # the same card, one slot deeper in the library.
        game = _make_game()
        rick, _ = game.players
        engine = _engine(game)
        _lands(rick, 6)
        _stock(rick)
        rick.library.insert(1, _terminus())
        engine.draw_cards(rick, 2, game)
        assert rick.cards_drawn_this_turn == 2
        assert not game._miracle_pending

    def test_declines_when_the_cost_cannot_be_paid(self, capsys):
        game, rick, _c, engine, term = self._game_with_terminus_on_top()
        for land in rick.battlefield:
            land.tapped = True
        engine.draw_cards(rick, 1, game)
        msgs = _run(resolve_pending_miracles(engine, game))
        assert msgs == []
        assert term in rick.hand
        # Assert the DECLINE, not just the end state: the cast pipeline would
        # reject an unaffordable cast anyway, so without this the guard could
        # be deleted and the test would still pass. The log line is also what
        # a batch audit greps for.
        assert "declines Terminus" in capsys.readouterr().out

    def test_a_stale_pending_entry_is_dropped_without_a_cast(self, capsys):
        game, rick, _c, engine, term = self._game_with_terminus_on_top()
        engine.draw_cards(rick, 1, game)
        rick.hand.remove(term)
        rick.graveyard.append(term)
        assert _run(resolve_pending_miracles(engine, game)) == []
        # Same reasoning as above — no cast may even be ATTEMPTED for a card
        # that has left hand.
        out = capsys.readouterr().out
        assert "[MIRACLE] Rick casts Terminus" not in out
        assert "miracle cast of Terminus" not in out

    def test_declines_a_wipe_that_would_hit_only_your_own_board(self):
        # The one guard on the otherwise "affordable = cast" gate. Decisive
        # on exactly the guard: same card, same mana, opponent board empty
        # and the caster's board not.
        game, rick, claude, engine, term = self._game_with_terminus_on_top(
            opp_creatures=0)
        rick.battlefield.append(_make_card("Mine", type_line="Creature",
                                           power="3", toughness="3"))
        engine.draw_cards(rick, 1, game)
        msgs = _run(resolve_pending_miracles(engine, game))
        assert msgs == []
        assert term in rick.hand
        assert any(c.name == "Mine" for c in rick.battlefield)

    def test_a_sorcery_miracle_is_castable_during_the_draw_step(self):
        # The miracle window opens on the DRAW, so the sorcery-speed gate
        # would otherwise reject Terminus, Entreat and Reforge — i.e. most
        # miracle cards.
        from mtg.constants import Phase
        game, rick, _c, engine, term = self._game_with_terminus_on_top()
        game.phase = Phase.DRAW
        engine.draw_cards(rick, 1, game)
        msgs = _run(resolve_pending_miracles(engine, game))
        assert any("miracle cost" in m for m in msgs), msgs

    def test_the_drain_is_actually_wired_into_drain_pending_triggers(self):
        """Every other miracle pin calls resolve_pending_miracles directly,
        so none of them notice if it is never invoked in production. This one
        goes through the real async choke point."""
        from mtg.triggers import drain_pending_triggers
        game, rick, claude, engine, term = self._game_with_terminus_on_top()
        engine.draw_cards(rick, 1, game)
        assert game._miracle_pending
        msgs = _run(drain_pending_triggers(engine, game))
        assert any("miracle cost" in m for m in msgs), msgs
        assert term._mana_paid == 1


class TestDredge:
    def test_replaces_the_draw_and_returns_the_card(self):
        game = _make_game()
        rick, _ = game.players
        engine = _engine(game)
        _stock(rick, 40)
        loam = _loam()
        rick.graveyard.append(loam)
        lib_before = len(rick.library)
        engine.draw_cards(rick, 1, game)
        assert loam in rick.hand
        assert len(rick.library) == lib_before - 3, "mills exactly N"
        assert rick.cards_drawn_this_turn == 0, (
            "the draw was REPLACED, not taken in addition")

    def test_at_most_once_per_turn(self):
        # Always dredging means never drawing a new card again; the cap is
        # what keeps the mechanic from being strictly worse than drawing.
        # TWO dredge cards, deliberately: with only one, the first dredge
        # moves it to hand and the second draw has no candidate anyway, so
        # the fixture would pass with the cap deleted — mutation testing
        # caught exactly that.
        game = _make_game()
        rick, _ = game.players
        engine = _engine(game)
        _stock(rick, 60)
        rick.graveyard.append(_loam())
        rick.graveyard.append(_make_card(
            "Stinkweed Imp", type_line="Creature — Imp",
            oracle_text=STINKWEED_IMP, mana_cost="{2}{B}"))
        engine.draw_cards(rick, 1, game)
        assert rick.cards_drawn_this_turn == 0, "the first draw was dredged"
        engine.draw_cards(rick, 1, game)
        assert rick.cards_drawn_this_turn == 1, (
            "the second draw of the turn is a real draw even though another "
            "dredge card is still available")

    def test_never_dredges_itself_into_a_deck_out(self):
        game = _make_game()
        rick, _ = game.players
        engine = _engine(game)
        _stock(rick, 5)
        loam = _loam()
        rick.graveyard.append(loam)
        engine.draw_cards(rick, 1, game)
        assert loam in rick.graveyard, "declined — the library is too small"
        assert rick.cards_drawn_this_turn == 1

    def test_takes_the_largest_affordable_dredge(self):
        # Graveyard decks want fuel, so among affordable candidates the
        # larger mill wins. Decisive: both are affordable.
        game = _make_game()
        rick, _ = game.players
        engine = _engine(game)
        _stock(rick, 60)
        rick.graveyard.append(_loam())                       # dredge 3
        imp = _make_card("Stinkweed Imp", type_line="Creature — Imp",
                         oracle_text=STINKWEED_IMP, mana_cost="{2}{B}")
        rick.graveyard.append(imp)                           # dredge 5
        lib_before = len(rick.library)
        engine.draw_cards(rick, 1, game)
        assert imp in rick.hand
        assert len(rick.library) == lib_before - 5

    def test_dredge_candidates_respects_library_size(self):
        game = _make_game()
        rick, _ = game.players
        rick.graveyard.append(_loam())
        _stock(rick, 2)
        assert helpers.dredge_candidates(rick) == []
        _stock(rick, 5)
        assert [n for _c, n in helpers.dredge_candidates(rick)] == [3]


class TestDrawHookCoverage:
    def test_both_draw_choke_points_carry_the_hooks(self):
        """The hooks live in GameEngine.draw_cards and the `draw_cards`
        ACTION handler. That covers the draw step and every template/Tier-3
        draw — NOT the ~8 raw library.pop(0) draws in rules/. This pin makes
        the covered set explicit so the gap stays visible rather than being
        assumed closed."""
        import inspect

        import mtg.actions as actions
        import mtg.engine as engine_mod
        for src in (inspect.getsource(engine_mod), inspect.getsource(actions)):
            assert "try_dredge(game, player)" in src
            assert "note_miracle_on_draw(game, player, card)" in src

    def test_the_uncovered_draw_sites_are_still_uncovered(self):
        """Documents the known gap. If someone routes rules/ draws through
        the choke point, this pin fails and should be DELETED along with the
        caveat in helpers — a passing-because-fixed test is the point."""
        import inspect

        import rules.spell_resolver as sr
        assert "try_dredge" not in inspect.getsource(sr), (
            "rules/spell_resolver now hooks the draw — update the coverage "
            "note in mtg/helpers.py and delete this pin")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
