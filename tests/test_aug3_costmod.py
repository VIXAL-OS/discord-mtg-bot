"""Aug 3, 2026 — the COST-MODIFICATION cluster: affinity and converge.

Both live on the SPELL being cast rather than on another permanent, which is
why neither fits compute_cost_reduction (that scans the battlefield for
permanents saying "spells you cast cost less"). Splice is the third member and
is deliberately not in this wave — see the register.

Oracle text is the REAL printed text (Scryfall-verified this session).
"""
import asyncio

import pytest

from mtg import helpers
from mtg.engine import GameEngine

from tests.conftest import _make_card, _make_game


ICEBREAKER_KRAKEN = (
    "Affinity for snow lands (This spell costs {1} less to cast for each snow "
    "land you control.)\n"
    "When this creature enters, artifacts and creatures target opponent "
    "controls don't untap during that player's next untap step.\n"
    "Return three snow lands you control to their owner's hand: Return this "
    "creature to its owner's hand.")
PRISMATIC_ENDING = (
    "Converge — Exile target nonland permanent if its mana value is less than "
    "or equal to the number of colors of mana spent to cast this spell.")
# Urza GRANTS affinity — it must never be read as the source's own.
URZA_PLUS = ("+1: The next spell you cast this turn has affinity for "
             "artifacts.")


def _engine(game):
    engine = GameEngine(None)
    game._rules_engine = engine.rules
    engine.rules.engine_ref = engine
    return engine


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _kraken():
    card = _make_card("Icebreaker Kraken", type_line="Snow Creature — Kraken",
                      oracle_text=ICEBREAKER_KRAKEN, mana_cost="{10}{U}{U}",
                      power="8", toughness="8")
    card.cmc = 12
    return card


def _snow_islands(player, n):
    for _ in range(n):
        player.battlefield.append(_make_card(
            "Snow-Covered Island", type_line="Basic Snow Land — Island",
            oracle_text="{T}: Add {U}."))


class TestAffinityParser:
    def test_reads_the_for_phrase(self):
        assert helpers.parse_affinity(ICEBREAKER_KRAKEN) == "snow lands"
        assert helpers.parse_affinity("Affinity for artifacts") == "artifacts"
        assert helpers.parse_affinity("Affinity for Equipment") == "equipment"

    def test_a_grant_is_not_the_source_s_own_affinity(self):
        # The same trap as every other keyword in this family: Urza's +1
        # grants affinity to the NEXT spell, and reading it as Urza's own
        # would discount him by his own artifact count.
        assert helpers.parse_affinity(URZA_PLUS) is None

    def test_counts_only_permanents_matching_the_whole_phrase(self):
        game = _make_game()
        rick, _ = game.players
        kraken = _kraken()
        # "snow lands" needs BOTH the snow supertype and the land type.
        for _ in range(3):
            rick.battlefield.append(_make_card("Coldsteel Heart",
                                               type_line="Snow Artifact"))
            rick.battlefield.append(_make_card("Island",
                                               type_line="Basic Land — Island"))
        amount, phrase = helpers.compute_affinity_reduction(rick, kraken)
        assert (amount, phrase) == (0, "snow lands")
        _snow_islands(rick, 4)
        amount, _ = helpers.compute_affinity_reduction(rick, kraken)
        assert amount == 4


class TestAffinityCasting:
    def test_ten_snow_lands_reduce_the_kraken_to_its_two_blue_pips(self):
        game = _make_game()
        rick, _ = game.players
        kraken = _kraken()
        rick.hand.append(kraken)
        _snow_islands(rick, 10)
        engine = _engine(game)
        ok, msg, _ = _run(engine.cast_spell_async(game, rick, kraken))
        assert ok, msg
        assert sum(1 for c in rick.battlefield if c.tapped) == 2, (
            "{10}{U}{U} minus ten snow lands is {U}{U}")

    def test_the_reduction_can_never_eat_a_colored_pip(self):
        """CR 601.2f — twenty snow lands is far more than the generic
        portion, so the Kraken must still tap exactly two blue sources.

        Honest note, the same one the July-26 cost-reduction work recorded
        for this identical clamp: mutation testing shows the CR 601.2f cap in
        _compute_alt_costs is DEFENCE IN DEPTH, not load-bearing. Removing it
        does not change this outcome, because tap_sources_for_cost checks
        `mana_produced[color] < needed` per colour, so a negative
        generic_needed can never buy a coloured pip. The cap stays because
        generic_needed has no zero clamp of its own and that independence is
        a coupling not worth relying on — but this test pins the BEHAVIOUR,
        which is what actually matters, and does not claim the cap is what
        produces it."""
        game = _make_game()
        rick, _ = game.players
        kraken = _kraken()
        rick.hand.append(kraken)
        _snow_islands(rick, 20)
        engine = _engine(game)
        ok, msg, _ = _run(engine.cast_spell_async(game, rick, kraken))
        assert ok, msg
        assert sum(1 for c in rick.battlefield if c.tapped) == 2

    def test_the_pre_gate_knows_about_affinity(self):
        """Without this the payment stage would pay the reduced cost but the
        pre-gate would reject the cast first, so the AI is never offered the
        card — the doomed-gate asymmetry the convoke and cost-reduction
        awareness were both added to fix.

        Decisive on exactly the affinity awareness: ten snow lands can pay
        the reduced {U}{U} and cannot pay the printed {10}{U}{U} (12 mana
        from 10 sources), so the gate's answer differs with and without it."""
        game = _make_game()
        rick, _ = game.players
        kraken = _kraken()
        rick.hand.append(kraken)
        _snow_islands(rick, 10)
        engine = _engine(game)
        can_cast, reason = engine.rules.can_cast_spell(game, rick, kraken)
        assert can_cast, reason


class TestConverge:
    def _converge_game(self, colors):
        game = _make_game()
        rick, claude = game.players
        ending = _make_card("Prismatic Ending", type_line="Sorcery",
                            oracle_text=PRISMATIC_ENDING, mana_cost="{X}{W}")
        ending.cmc = 1
        rick.hand.append(ending)
        names = {"W": "Plains", "U": "Island", "B": "Swamp",
                 "R": "Mountain", "G": "Forest"}
        for sym in colors:
            for _ in range(2):
                rick.battlefield.append(_make_card(
                    names[sym], type_line=f"Basic Land — {names[sym]}",
                    oracle_text="{T}: Add {%s}." % sym))
        return game, rick, claude, ending

    def test_colors_spent_is_recorded_at_payment(self):
        game, rick, claude, ending = self._converge_game("W")
        # Prismatic Ending targets, so it needs something to point at or the
        # CR 601.2c gate blocks the cast before any mana is paid.
        bear = _make_card("Bear", type_line="Creature — Bear",
                          power="1", toughness="1")
        bear.cmc = 1
        claude.battlefield.append(bear)
        engine = _engine(game)
        ending._x_value = 1
        _run(engine.cast_spell_async(game, rick, ending, target=bear))
        assert ending._colors_spent, "the mana engine must record what it spent"
        assert helpers.colors_spent_count(ending) >= 1

    def test_colorless_is_not_a_color(self):
        card = _make_card("X", type_line="Sorcery")
        card._colors_spent = ("W", "C", "U")
        assert helpers.colors_spent_count(card) == 2, "CR 702.100a — {C} is not a color"

    def test_a_six_drop_survives_a_one_color_prismatic_ending(self):
        """The converge CONDITION was ignored entirely: the template exiled
        any nonland permanent whatever its mana value, so a one-colour
        Prismatic Ending answered a 6-drop."""
        game, rick, claude, ending = self._converge_game("W")
        big = _make_card("Big Thing", type_line="Creature — Giant",
                         power="6", toughness="6")
        big.cmc = 6
        claude.battlefield.append(big)
        engine = _engine(game)
        ending._x_value = 1
        ok, msg, msgs = _run(engine.cast_spell_async(game, rick, ending,
                                                     target=big))
        assert ok, msg
        assert big in claude.battlefield, (
            "mana value 6 exceeds the colours of mana spent")
        assert any("mana value" in m for m in msgs), msgs

    def test_a_cheap_permanent_is_exiled(self):
        game, rick, claude, ending = self._converge_game("W")
        bear = _make_card("Bear", type_line="Creature — Bear",
                          power="2", toughness="2")
        bear.cmc = 1
        claude.battlefield.append(bear)
        engine = _engine(game)
        ending._x_value = 1
        ok, msg, _ = _run(engine.cast_spell_async(game, rick, ending,
                                                  target=bear))
        assert ok, msg
        assert bear not in claude.battlefield, "mana value 1 <= 1 colour spent"

    def test_the_context_always_carries_colors_spent(self):
        """Same always-present contract as ctx['kicked'] — a converge
        template must never have to guess from the printed cost."""
        from rules.effect_templates import build_game_context
        game = _make_game()
        rick, claude = game.players
        card = _make_card("Prismatic Ending", type_line="Sorcery",
                          oracle_text=PRISMATIC_ENDING, mana_cost="{X}{W}")
        ctx = build_game_context(game, rick, claude, card=card)
        assert ctx.get("colors_spent") == 0
        card._colors_spent = ("W", "U")
        ctx = build_game_context(game, rick, claude, card=card)
        assert ctx.get("colors_spent") == 2


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
