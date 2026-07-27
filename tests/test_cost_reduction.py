"""Static cost adjustment — reductions AND increases (CR 601.2f).

Implemented July 26, 2026. Before that, `ManaCost.cost_reductions` was declared
at rules/mana.py:434 and **never written by anything** — no code read or filled
it — so every reducer was a blank card. Nine sit in the test decks (the four
Medallions, Bontu's Monument, Herald of the Pantheon, Gravebreaker Lamia,
Danitha Capashen, and **Baral, Chief of Compliance — a COMMANDER whose entire
defining ability did nothing in the deck named after him**). In
game_1530441513702785114 every black spell was cast at full price with Jet
Medallion on the battlefield.

The load-bearing rule is CR 601.2f: a cost reduction applies to the total cost
but can only reduce the GENERIC portion — it can never pay a colored pip. That
matters here beyond pedantry, because `tap_sources_for_cost` computes
`generic_needed = parsed.generic_requirement + additional_generic + snow_count`
with NO clamp at zero, so an uncapped reduction would drive it negative and
silently under-require colored mana. The cap lives in `_compute_alt_costs`.

Both seams are covered, for the reason the July 20 convoke work established:
if only the payment stage knew about the discount, the pre-gate would reject
the cast first and the AI would never be offered the card at all.
"""
import asyncio

import pytest

from mtg.helpers import compute_cost_reduction, spell_colors_from_cost


def _perm(make_card, name, oracle, type_line="Artifact"):
    return make_card(name, type_line=type_line, oracle_text=oracle,
                     power=None, toughness=None)


def _swamp(make_card):
    return make_card("Swamp", type_line="Basic Land — Swamp",
                     oracle_text="", power=None, toughness=None)


JET = "Black spells you cast cost {1} less to cast."
BONTU = ("Black creature spells you cast cost {1} less to cast.\n"
         "Whenever you cast a creature spell, each opponent loses 1 life "
         "and you gain 1 life.")
HERALD = ("Enchantment spells you cast cost {1} less to cast.\n"
          "Whenever you cast an enchantment spell, you gain 1 life.")
DANITHA = ("First strike, vigilance, lifelink\n"
           "Aura and Equipment spells you cast cost {1} less to cast.")
BARAL = ("Instant and sorcery spells you cast cost {1} less to cast.\n"
         "Whenever a spell or ability you control counters a spell, you may "
         "draw a card. If you do, discard a card.")
LAMIA = ("Lifelink\nWhen this creature enters, search your library for a "
         "card, put it into your graveyard, then shuffle.\n"
         "Spells you cast from your graveyard cost {1} less to cast.")


class TestSpellColorsComeFromTheManaCost:
    """CR 202.2 — a spell's color is its mana cost, not its color identity.

    Using color_identity would make a colorless artifact with a {B} activated
    ability count as a "Black spell" for Jet Medallion.
    """

    def test_basic_colors(self):
        assert spell_colors_from_cost("{2}{B}") == {"B"}
        assert spell_colors_from_cost("{W}{U}") == {"W", "U"}

    def test_colorless_has_no_color(self):
        assert spell_colors_from_cost("{2}") == set()
        assert spell_colors_from_cost("") == set()

    def test_hybrid_counts_as_both(self):
        assert spell_colors_from_cost("{W/U}") == {"W", "U"}

    def test_phyrexian_counts_as_its_color(self):
        assert spell_colors_from_cost("{B/P}") == {"B"}

    def test_generic_and_x_contribute_nothing(self):
        assert spell_colors_from_cost("{X}{X}{R}") == {"R"}


class TestRestrictionMatching:
    """Within a category the match is OR; across categories it is AND."""

    def test_color_restriction(self, make_card):
        p = _perm(make_card, "Jet Medallion", JET)
        pl = pytest.importorskip("mtg.models").Player(name="P", life=40)
        pl.battlefield = [p]
        black = make_card("Phyrexian Arena", type_line="Enchantment",
                          mana_cost="{1}{B}{B}", power=None, toughness=None)
        white = make_card("Swords to Plowshares", type_line="Instant",
                          mana_cost="{W}", power=None, toughness=None)
        assert compute_cost_reduction(pl, black)[0] == 1
        assert compute_cost_reduction(pl, white)[0] == 0

    def test_color_and_type_both_required(self, make_card):
        """Bontu's Monument: "Black CREATURE spells" — needs both."""
        from mtg.models import Player
        pl = Player(name="P", life=40)
        pl.battlefield = [_perm(make_card, "Bontu's Monument", BONTU)]
        black_creature = make_card("Gray Merchant of Asphodel",
                                   type_line="Creature — Zombie",
                                   mana_cost="{3}{B}{B}")
        black_noncreature = make_card("Phyrexian Arena", type_line="Enchantment",
                                      mana_cost="{1}{B}{B}",
                                      power=None, toughness=None)
        white_creature = make_card("Serra Angel", type_line="Creature — Angel",
                                   mana_cost="{3}{W}{W}")
        assert compute_cost_reduction(pl, black_creature)[0] == 1
        assert compute_cost_reduction(pl, black_noncreature)[0] == 0, \
            "black but not a creature"
        assert compute_cost_reduction(pl, white_creature)[0] == 0, \
            "creature but not black"

    def test_type_only_restriction(self, make_card):
        from mtg.models import Player
        pl = Player(name="P", life=40)
        pl.battlefield = [_perm(make_card, "Herald of the Pantheon", HERALD,
                                type_line="Creature — Human")]
        ench = make_card("Phyrexian Arena", type_line="Enchantment",
                         mana_cost="{1}{B}{B}", power=None, toughness=None)
        inst = make_card("Counterspell", type_line="Instant",
                         mana_cost="{U}{U}", power=None, toughness=None)
        assert compute_cost_reduction(pl, ench)[0] == 1
        assert compute_cost_reduction(pl, inst)[0] == 0

    def test_subtype_restriction_is_or(self, make_card):
        """Danitha: "Aura and Equipment spells" — those are SUBTYPES."""
        from mtg.models import Player
        pl = Player(name="P", life=40)
        pl.battlefield = [_perm(make_card, "Danitha Capashen, Paragon", DANITHA,
                                type_line="Legendary Creature — Human Knight")]
        equip = make_card("Lightning Greaves", type_line="Artifact — Equipment",
                          mana_cost="{2}", power=None, toughness=None)
        aura = make_card("Pacifism", type_line="Enchantment — Aura",
                         mana_cost="{1}{W}", power=None, toughness=None)
        plain = make_card("Sol Ring", type_line="Artifact", mana_cost="{1}",
                          power=None, toughness=None)
        assert compute_cost_reduction(pl, equip)[0] == 1
        assert compute_cost_reduction(pl, aura)[0] == 1
        assert compute_cost_reduction(pl, plain)[0] == 0, \
            "an artifact that is not an Equipment must not benefit"

    def test_two_types_is_or(self, make_card):
        """Baral: "Instant and sorcery spells" — either qualifies."""
        from mtg.models import Player
        pl = Player(name="P", life=40)
        pl.battlefield = [_perm(make_card, "Baral, Chief of Compliance", BARAL,
                                type_line="Legendary Creature — Human Wizard")]
        for tl, cost in (("Instant", "{U}{U}"), ("Sorcery", "{2}{B}")):
            c = make_card(f"X {tl}", type_line=tl, mana_cost=cost,
                          power=None, toughness=None)
            assert compute_cost_reduction(pl, c)[0] == 1, tl
        creature = make_card("Bear", type_line="Creature — Bear",
                             mana_cost="{1}{G}")
        assert compute_cost_reduction(pl, creature)[0] == 0


class TestZoneRestriction:
    def test_graveyard_clause_only_applies_from_graveyard(self, make_card):
        from mtg.models import Player
        pl = Player(name="P", life=40)
        pl.battlefield = [_perm(make_card, "Gravebreaker Lamia", LAMIA,
                                type_line="Creature — Lamia")]
        spell = make_card("Toxic Deluge", type_line="Sorcery",
                          mana_cost="{2}{B}", power=None, toughness=None)
        assert compute_cost_reduction(pl, spell, from_graveyard=False)[0] == 0
        assert compute_cost_reduction(pl, spell, from_graveyard=True)[0] == 1

    def test_unmodelled_zone_clause_is_refused_not_over_applied(self, make_card):
        """"Spells you cast from exile" must not silently become unconditional."""
        from mtg.models import Player
        pl = Player(name="P", life=40)
        pl.battlefield = [_perm(make_card, "Weird Exile Thing",
                                "Spells you cast from exile cost {2} less to cast.")]
        spell = make_card("Toxic Deluge", type_line="Sorcery",
                          mana_cost="{2}{B}", power=None, toughness=None)
        assert compute_cost_reduction(pl, spell)[0] == 0
        assert compute_cost_reduction(pl, spell, from_graveyard=True)[0] == 0


class TestScopeAndStacking:
    def test_reductions_stack(self, make_card):
        from mtg.models import Player
        pl = Player(name="P", life=40)
        pl.battlefield = [_perm(make_card, "Jet Medallion", JET),
                          _perm(make_card, "Bontu's Monument", BONTU)]
        black_creature = make_card("Gray Merchant of Asphodel",
                                   type_line="Creature — Zombie",
                                   mana_cost="{3}{B}{B}")
        amt, srcs = compute_cost_reduction(pl, black_creature)
        assert amt == 2 and len(srcs) == 2

    def test_only_your_own_permanents_count(self, make_card, make_game):
        """Every printed reducer of this shape says "you cast"."""
        game = make_game()
        rick, claude = game.players
        claude.battlefield.append(_perm(make_card, "Jet Medallion", JET))
        black = make_card("Phyrexian Arena", type_line="Enchantment",
                          mana_cost="{1}{B}{B}", power=None, toughness=None)
        assert compute_cost_reduction(rick, black)[0] == 0, (
            "the opponent's Medallion must not discount Rick's spell")

    def test_non_reducer_permanents_contribute_nothing(self, make_card):
        from mtg.models import Player
        pl = Player(name="P", life=40)
        pl.battlefield = [
            _perm(make_card, "Sol Ring", "{T}: Add {C}{C}."),
            _perm(make_card, "Humility", "All creatures lose all abilities and "
                                         "have base power and toughness 1/1.",
                  type_line="Enchantment"),
        ]
        spell = make_card("Counterspell", type_line="Instant",
                          mana_cost="{U}{U}", power=None, toughness=None)
        assert compute_cost_reduction(pl, spell)[0] == 0

    def test_a_reducer_does_not_discount_itself(self, make_card):
        from mtg.models import Player
        pl = Player(name="P", life=40)
        medallion = _perm(make_card, "Jet Medallion", JET)
        pl.battlefield = [medallion]
        assert compute_cost_reduction(pl, medallion)[0] == 0


class TestCr601_2f_GenericOnly:
    """The rule that makes this safe: reductions never eat a colored pip.

    `tap_sources_for_cost` computes generic_needed WITHOUT clamping at zero,
    so an uncapped reduction would under-require colored mana.
    """

    def _engine_game(self, make_game, make_card, spell_cost, spell_type,
                     n_swamps, with_medallion):
        from mtg.engine import GameEngine
        engine = GameEngine(None)
        game = make_game()
        rick = game.players[0]
        for _ in range(n_swamps):
            rick.battlefield.append(_swamp(make_card))
        if with_medallion:
            rick.battlefield.append(_perm(make_card, "Jet Medallion", JET))
        spell = make_card("Test Spell", type_line=spell_type,
                          mana_cost=spell_cost, cmc=0,
                          oracle_text="", power=None, toughness=None)
        rick.hand.append(spell)
        return engine, game, rick, spell

    def test_generic_is_reduced_end_to_end(self, make_game, make_card):
        """{2}{B} on two Swamps: impossible at full price, fine with -1."""
        from mtg.spells import cast_spell_async
        engine, game, rick, spell = self._engine_game(
            make_game, make_card, "{2}{B}", "Sorcery", 2, with_medallion=True)
        ok, msg, _ = asyncio.run(cast_spell_async(engine, game, rick, spell))
        assert ok, f"Jet Medallion should have made {{2}}{{B}} payable: {msg}"

    def test_same_cast_fails_without_the_reducer(self, make_game, make_card):
        """The control — proves the test above isn't passing for another reason."""
        from mtg.spells import cast_spell_async
        engine, game, rick, spell = self._engine_game(
            make_game, make_card, "{2}{B}", "Sorcery", 2, with_medallion=False)
        ok, msg, _ = asyncio.run(cast_spell_async(engine, game, rick, spell))
        assert not ok, "{2}{B} on two lands must fail without a reducer"

    def test_colored_pips_are_never_reduced(self, make_game, make_card):
        """{B}{B} has no generic — a Medallion cannot make it castable on one Swamp."""
        from mtg.spells import cast_spell_async
        engine, game, rick, spell = self._engine_game(
            make_game, make_card, "{B}{B}", "Sorcery", 1, with_medallion=True)
        ok, msg, _ = asyncio.run(cast_spell_async(engine, game, rick, spell))
        assert not ok, (
            "CR 601.2f: a cost reduction may only reduce the generic portion, "
            "so {B}{B} still needs two black sources")

    def test_raw_reduction_is_uncapped_by_design(self, make_card):
        """The helper reports what's available; the payment seam does the capping."""
        from mtg.models import Player
        pl = Player(name="P", life=40)
        pl.battlefield = [_perm(make_card, "Jet Medallion", JET),
                          _perm(make_card, "Jet Medallion 2", JET)]
        spell = make_card("Test", type_line="Sorcery", mana_cost="{1}{B}",
                          power=None, toughness=None)
        assert compute_cost_reduction(pl, spell)[0] == 2

    def test_cap_expression_is_present_at_the_payment_seam(self):
        """STRUCTURAL on purpose — and the honest reason is worth recording.

        Mutation testing (July 26) showed the cap is currently
        DEFENSE-IN-DEPTH, not the only line of defence: with
        `cost_reduction = raw_reduction` substituted, no behavioural test
        could be made to fail. Two independent downstream checks already
        refuse an over-reduced cost — `tap_sources_for_cost` compares
        `mana_produced[color] < needed` per colour (so a negative
        `generic_needed` can never buy a colored pip), and the July 20
        one-tap physical-total gate catches the source-count case.

        The cap stays anyway: `generic_needed` is genuinely computed without
        a zero clamp, so the invariant depends on those two downstream checks
        continuing to be independent of it. That is a coupling worth not
        relying on. Since no behaviour distinguishes it, this pin asserts the
        expression directly — and asserts the `min(...)`, not just the
        variable name, because a mutant that left `headroom` computed but
        unused slipped past the looser check.
        """
        import inspect
        from mtg import spells
        src = inspect.getsource(spells._compute_alt_costs)
        assert "cost_reduction = min(raw_reduction, headroom)" in src, (
            "the CR 601.2f cap was removed or reworded in _compute_alt_costs; "
            "if that was deliberate, re-verify that tap_sources_for_cost still "
            "rejects an over-reduced cost independently")


class TestPreGateKnowsAboutTheDiscount:
    """If only the payment stage knew, the pre-gate would reject the cast first
    and the AI would never be offered the card (the July 20 convoke lesson)."""

    def test_can_cast_spell_accepts_the_reduced_cost(self, make_game, make_card):
        from mtg.engine import GameEngine
        engine = GameEngine(None)
        game = make_game()
        rick = game.players[0]
        for _ in range(2):
            rick.battlefield.append(_swamp(make_card))
        spell = make_card("Test Spell", type_line="Sorcery",
                          mana_cost="{2}{B}", cmc=3, oracle_text="",
                          power=None, toughness=None)
        rick.hand.append(spell)
        # Sorcery-speed gates: Rick's own main phase, empty stack.
        from mtg.constants import Phase
        game.active_player_index = game.players.index(rick)
        game.phase = Phase.MAIN1

        ok_before, reason = engine.rules.can_cast_spell(game, rick, spell)
        rick.battlefield.append(_perm(make_card, "Jet Medallion", JET))
        ok_after, _ = engine.rules.can_cast_spell(game, rick, spell)
        assert not ok_before, f"expected the pre-gate to reject at full price: {reason}"
        assert ok_after, "the pre-gate must credit the Medallion's discount"


# --------------------------------------------------------------------------- #
# Cost INCREASES (taxes) + X-cost interaction — added July 26, 2026            #
# --------------------------------------------------------------------------- #
# Real Scryfall wording.
THALIA = "First strike\nNoncreature spells cost {1} more to cast."
SPHERE = "Spells cost {1} more to cast."
# VERIFIED against data/scryfall_oracle_cards.json, July 27 2026. The earlier
# constant here said "Spells you cast cost {1} less" — a memory-based
# paraphrase, i.e. exactly the false-positive pattern the audit playbook warns
# about, committed in my own test data. The real card carries TWO separate
# colour-restricted reduction lines, which is a strictly better exercise: a
# {W}{U} spell gets BOTH (the well-known -2 on Sphinx's Revelation).
ARBITER = ("White spells you cast cost {1} less to cast.\n"
           "Blue spells you cast cost {1} less to cast.\n"
           "Spells your opponents cast cost {1} more to cast.")
SAPPHIRE = "Blue spells you cast cost {1} less to cast."


def _spell(make_card, name, type_line, cost):
    return make_card(name, type_line=type_line, mana_cost=cost, cmc=0,
                     oracle_text="", power=None, toughness=None)


class TestTaxRestrictionAndNegation:
    """"Noncreature" is the whole tax family, and it is a substring trap.

    'creature' is a substring of 'noncreature' — the documented Woodfall
    Primus bug from the July 24 audit. Get it wrong and Thalia taxes
    precisely the spells she is supposed to leave alone.
    """

    def _thalia_game(self, make_game, make_card):
        from mtg.helpers import compute_cost_increase
        game = make_game()
        rick, claude = game.players
        rick.battlefield.append(make_card(
            "Thalia, Guardian of Thraben",
            type_line="Legendary Creature — Human Soldier",
            oracle_text=THALIA))
        return game, rick, claude, compute_cost_increase

    def test_noncreature_spells_are_taxed(self, make_game, make_card):
        game, rick, _c, inc = self._thalia_game(make_game, make_card)
        assert inc(game, rick, _spell(make_card, "Counterspell", "Instant", "{U}{U}"))[0] == 1
        assert inc(game, rick, _spell(make_card, "Sol Ring", "Artifact", "{1}"))[0] == 1

    def test_creature_spells_are_NOT_taxed(self, make_game, make_card):
        game, rick, _c, inc = self._thalia_game(make_game, make_card)
        bear = make_card("Grizzly Bears", type_line="Creature — Bear",
                         mana_cost="{1}{G}")
        assert inc(game, rick, bear)[0] == 0, (
            "'creature' is a substring of 'noncreature' — Thalia must not tax "
            "creature spells")

    def test_tax_is_symmetric_by_default(self, make_game, make_card):
        """Thalia taxes her OWN controller too — no 'your opponents' clause."""
        game, rick, claude, inc = self._thalia_game(make_game, make_card)
        spell = _spell(make_card, "Counterspell", "Instant", "{U}{U}")
        assert inc(game, rick, spell)[0] == 1, "controller is taxed too"
        assert inc(game, claude, spell)[0] == 1, "opponent is taxed"


class TestTaxScope:
    def test_grand_arbiter_carries_both_halves_asymmetrically(
            self, make_game, make_card):
        """One card, opposite effects, depending on who is casting."""
        from mtg.helpers import compute_cost_increase, compute_cost_reduction
        game = make_game()
        rick, claude = game.players
        rick.battlefield.append(make_card(
            "Grand Arbiter Augustin IV",
            type_line="Legendary Creature — Human Advisor",
            oracle_text=ARBITER))
        spell = _spell(make_card, "Wrath of God", "Sorcery", "{2}{W}{W}")
        assert compute_cost_increase(game, rick, spell)[0] == 0, \
            "its controller is not taxed"
        assert compute_cost_reduction(rick, spell)[0] == 1, \
            "its controller gets the discount"
        assert compute_cost_increase(game, claude, spell)[0] == 1, \
            "the opponent IS taxed"
        assert compute_cost_reduction(claude, spell)[0] == 0, \
            "the opponent gets no discount"

    def test_grand_arbiter_stacks_both_colour_lines_on_a_wu_spell(
            self, make_game, make_card):
        """Two colour-restricted clauses on ONE card both apply to {W}{U}.

        Sphinx's Revelation off Grand Arbiter costs 2 less, not 1 — a real
        interaction players know, and a good check that finditer keeps
        scanning after the first match.
        """
        from mtg.helpers import compute_cost_reduction
        game = make_game()
        rick = game.players[0]
        rick.battlefield.append(make_card(
            "Grand Arbiter Augustin IV",
            type_line="Legendary Creature — Human Advisor",
            oracle_text=ARBITER))
        wu = _spell(make_card, "Sphinx's Revelation", "Instant", "{X}{W}{U}{U}")
        mono_w = _spell(make_card, "Wrath of God", "Sorcery", "{2}{W}{W}")
        off_colour = _spell(make_card, "Toxic Deluge", "Sorcery", "{2}{B}")
        assert compute_cost_reduction(rick, wu)[0] == 2, "both colour lines apply"
        assert compute_cost_reduction(rick, mono_w)[0] == 1
        assert compute_cost_reduction(rick, off_colour)[0] == 0

    def test_opponent_scoped_tax_skips_its_own_controller(self, make_game, make_card):
        from mtg.helpers import compute_cost_increase
        game = make_game()
        rick, claude = game.players
        rick.battlefield.append(make_card(
            "Tax Thing", type_line="Enchantment", power=None, toughness=None,
            oracle_text="Noncreature spells your opponents cast cost {2} more to cast."))
        spell = _spell(make_card, "Counterspell", "Instant", "{U}{U}")
        assert compute_cost_increase(game, rick, spell)[0] == 0
        assert compute_cost_increase(game, claude, spell)[0] == 2


class TestCr601_2f_Ordering:
    """Increases apply BEFORE reductions, and the two net out exactly."""

    def _cast(self, make_game, make_card, n_swamps, perms, cost):
        from mtg.engine import GameEngine
        from mtg.spells import cast_spell_async
        game = make_game()
        rick = game.players[0]
        for _ in range(n_swamps):
            rick.battlefield.append(_swamp(make_card))
        for p in perms:
            rick.battlefield.append(p)
        spell = _spell(make_card, "Probe", "Sorcery", cost)
        rick.hand.append(spell)
        return asyncio.run(cast_spell_async(GameEngine(None), game, rick, spell))

    def _sphere(self, make_card):
        return _perm(make_card, "Sphere of Resistance", SPHERE)

    def _jet(self, make_card):
        return _perm(make_card, "Jet Medallion", JET)

    def test_tax_makes_a_castable_spell_uncastable(self, make_game, make_card):
        ok_without, _, _ = self._cast(make_game, make_card, 3, [], "{2}{B}")
        ok_with, _, _ = self._cast(make_game, make_card, 3,
                                   [self._sphere(make_card)], "{2}{B}")
        assert ok_without, "3 Swamps pays {2}{B}"
        assert not ok_with, "Sphere of Resistance must make it cost 4"

    def test_tax_and_discount_cancel(self, make_game, make_card):
        ok, msg, _ = self._cast(make_game, make_card, 3,
                                [self._sphere(make_card), self._jet(make_card)],
                                "{2}{B}")
        assert ok, "+1 and -1 must net to zero: " + str(msg)

    def test_cancelling_pair_still_fails_one_land_short(self, make_game, make_card):
        """The control — proves the test above is not passing for free."""
        ok, _, _ = self._cast(make_game, make_card, 2,
                              [self._sphere(make_card), self._jet(make_card)],
                              "{2}{B}")
        assert not ok

    def test_increase_is_inside_the_reduction_headroom(self):
        """A tax raises the generic a reducer may then eat (CR 601.2f order)."""
        import inspect
        from mtg import spells
        src = inspect.getsource(spells._compute_alt_costs)
        assert "printed_generic + additional_cost + cost_increase" in src, (
            "the increase must be inside the reduction headroom — otherwise a "
            "taxed colored-only spell has no generic for the discount to take")


class TestXCostSeesAdjustments:
    """An X spell's {X} becomes generic once X is chosen, so adjustments move X."""

    def _x_cast(self, make_game, make_card, n_islands, perms):
        import contextlib
        import io as _io
        from mtg.engine import GameEngine
        from mtg.spells import cast_spell_async
        game = make_game()
        rick = game.players[0]
        for _ in range(n_islands):
            rick.battlefield.append(make_card(
                "Island", type_line="Basic Land — Island", oracle_text="",
                power=None, toughness=None))
        for p in perms:
            rick.battlefield.append(p)
        spell = make_card("Blue Sun's Zenith", type_line="Instant",
                          mana_cost="{X}{U}{U}{U}", cmc=0,
                          oracle_text="Target player draws X cards.",
                          power=None, toughness=None)
        rick.hand.append(spell)
        buf = _io.StringIO()
        with contextlib.redirect_stdout(buf):
            ok, msg, _ = asyncio.run(cast_spell_async(GameEngine(None), game, rick, spell))
        return ok, getattr(spell, "_x_value", None), buf.getvalue()

    def test_baseline_x(self, make_game, make_card):
        ok, x, _ = self._x_cast(make_game, make_card, 6, [])
        assert ok and x == 3, "6 Islands, fixed 3 -> X=3, got " + str(x)

    def test_reduction_buys_one_more_x(self, make_game, make_card):
        ok, x, out = self._x_cast(
            make_game, make_card, 6,
            [_perm(make_card, "Sapphire Medallion", SAPPHIRE)])
        assert ok, "the cast must still succeed"
        assert x == 4, (
            "a Medallion should buy one MORE card off Blue Sun's Zenith, got X=" + str(x))

    def test_tax_costs_one_x(self, make_game, make_card):
        ok, x, _ = self._x_cast(
            make_game, make_card, 6,
            [_perm(make_card, "Sphere of Resistance", SPHERE)])
        assert ok
        assert x == 2, "a Sphere should cost one card of X, got X=" + str(x)

    def test_x_generic_is_inside_the_reduction_headroom(self):
        """Without this, {X}{U}{U}{U} has zero printed generic and the
        discount would be capped away to nothing."""
        import inspect
        from mtg import spells
        src = inspect.getsource(spells._compute_alt_costs)
        assert "_x_derived_generic" in src and "+ _x_derived_generic" in src
        # July 27: the same X-derived generic must ALSO reach convoke/delve/
        # improvise, which originally scanned only digit-brace symbols and so
        # exiled zero cards for an {X} spell (Logic Knot).
        assert src.count("_x_derived_generic") >= 4, (
            "X-derived generic must feed convoke, delve, improvise AND the "
            "reduction headroom")


class TestPreGateKnowsAboutTheTax:
    def test_pre_gate_rejects_a_taxed_spell(self, make_game, make_card):
        """Otherwise the AI is offered spells it cannot pay for and burns the
        main phase on doomed casts."""
        from mtg.constants import Phase
        from mtg.engine import GameEngine
        engine = GameEngine(None)
        game = make_game()
        rick = game.players[0]
        for _ in range(3):
            rick.battlefield.append(_swamp(make_card))
        spell = _spell(make_card, "Probe", "Sorcery", "{2}{B}")
        spell.cmc = 3
        rick.hand.append(spell)
        game.active_player_index = game.players.index(rick)
        game.phase = Phase.MAIN1

        ok_before, _ = engine.rules.can_cast_spell(game, rick, spell)
        rick.battlefield.append(_perm(make_card, "Sphere of Resistance", SPHERE))
        ok_after, reason = engine.rules.can_cast_spell(game, rick, spell)
        assert ok_before, "payable at three Swamps before the tax"
        assert not ok_after, "the pre-gate must apply the tax: " + str(reason)

    def test_colored_only_cost_gains_a_generic_symbol(self, make_game, make_card):
        """{U}{U} taxed by {1} is {1}{U}{U} — there is no generic symbol to
        grow, so one has to be prepended."""
        from mtg.constants import Phase
        from mtg.engine import GameEngine
        engine = GameEngine(None)
        game = make_game()
        rick = game.players[0]
        for _ in range(2):
            rick.battlefield.append(make_card(
                "Island", type_line="Basic Land — Island", oracle_text="",
                power=None, toughness=None))
        spell = _spell(make_card, "Counterspell", "Instant", "{U}{U}")
        spell.cmc = 2
        rick.hand.append(spell)
        game.active_player_index = game.players.index(rick)
        game.phase = Phase.MAIN1

        ok_before, _ = engine.rules.can_cast_spell(game, rick, spell)
        rick.battlefield.append(_perm(make_card, "Sphere of Resistance", SPHERE))
        ok_after, _ = engine.rules.can_cast_spell(game, rick, spell)
        assert ok_before and not ok_after, (
            "a tax on a colored-only cost must still register")


class TestTaxReachesThePaymentStageToo:
    """Mutation testing (July 26) found the end-to-end tax tests could not see
    the payment stage at all: `cast_spell_async` calls `can_cast_spell` first
    (mtg/spells.py:333), so the PRE-GATE rejects a taxed spell before payment
    is ever attempted, and removing `+ cost_increase` from `additional_generic`
    survived the whole suite.

    Payment still has to charge it — `card._mana_paid` is set from `total_cost`
    and is what X-cost ETBs (Walking Ballista, Hangarback Walker) read for
    their counters, and any future path that reaches casting without the
    pre-gate would otherwise undercharge. These pins hit that stage directly.
    """

    def _cast_taxed(self, make_game, make_card, n_swamps, taxed):
        from mtg.engine import GameEngine
        from mtg.spells import cast_spell_async
        game = make_game()
        rick = game.players[0]
        for _ in range(n_swamps):
            rick.battlefield.append(_swamp(make_card))
        if taxed:
            rick.battlefield.append(_perm(make_card, "Sphere of Resistance", SPHERE))
        spell = _spell(make_card, "Probe", "Sorcery", "{2}{B}")
        spell.cmc = 3
        rick.hand.append(spell)
        ok, msg, _ = asyncio.run(cast_spell_async(GameEngine(None), game, rick, spell))
        return ok, getattr(spell, "_mana_paid", None)

    def test_mana_paid_records_the_taxed_total(self, make_game, make_card):
        """_mana_paid feeds X-cost ETB counters — it must include the tax."""
        ok_plain, paid_plain = self._cast_taxed(make_game, make_card, 3, False)
        ok_taxed, paid_taxed = self._cast_taxed(make_game, make_card, 4, True)
        assert ok_plain and paid_plain == 3, f"untaxed {{2}}{{B}} costs 3, got {paid_plain}"
        assert ok_taxed, "four Swamps must cover the taxed cost"
        assert paid_taxed == 4, (
            f"the Sphere's +1 must be inside _mana_paid, got {paid_taxed}")

    def test_compute_alt_costs_publishes_the_increase(self, make_game, make_card):
        """The payment stage can only apply what the cost stage hands it."""
        from mtg.engine import GameEngine
        from mtg.spells import _compute_alt_costs
        game = make_game()
        rick = game.players[0]
        for _ in range(4):
            rick.battlefield.append(_swamp(make_card))
        rick.battlefield.append(_perm(make_card, "Sphere of Resistance", SPHERE))
        spell = _spell(make_card, "Probe", "Sorcery", "{2}{B}")
        spell.cmc = 3
        rick.hand.append(spell)
        early, costs = _compute_alt_costs(GameEngine(None), game, rick, spell,
                                          pay_mana=True, additional_cost=0)
        assert early is None, "the cost stage should not short-circuit here"
        assert costs["cost_increase"] == 1, (
            "cost_increase must be published for _pay_costs to charge it")
        assert costs["total_cost"] == 4, (
            f"total_cost must fold in the tax, got {costs['total_cost']}")

    def test_payment_applies_the_increase_to_generic(self):
        """Structural backstop for the seam the pre-gate masks end-to-end."""
        import inspect
        from mtg import spells
        src = inspect.getsource(spells._pay_costs)
        assert "additional_cost + cost_increase - total_alt_reduction" in src, (
            "the payment stage stopped charging the tax; the pre-gate hides "
            "this in normal play, so nothing else will catch it")
