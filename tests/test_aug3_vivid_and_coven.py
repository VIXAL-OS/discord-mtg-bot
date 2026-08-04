"""The last two entries on the missing-mechanics backlog (Aug 3, 2026).

VIVID multi-colour mana (Bloom Tender, Faeburrow Elder). The backlog called
this a keyword; it is not — "Vivid" is an ABILITY WORD and the mechanic is a
mana ability whose one tap produces N mana of N DIFFERENT colours. That shape
did not exist in the engine: the only modelled multi-output source (Sol Ring)
is colourless, and every multi-COLOUR production dict was read as an OR-dual
(one mana, choice of colour) because that is what `{'W': 1, 'U': 1}` means for
a dual land. Bloom Tender was absent from `untapped_mana_sources` outright and
contributed ZERO mana, in the four-colour deck it ships in.

There WAS a `_get_mana_production` entry for it returning `{'any': 1}` — dead
code (the card never reached the source list, so the branch was unreachable)
and wrong if it had run: 'any' claims a colour the card cannot make, so a
mono-green board would have advertised blue and the tap then fabricated it.

COVEN's cast-from-top (Augur of Autumn). `has_coven` existed with no consumer
at all, and nothing in the engine could cast from the library — there is no
precedent for library-as-a-cast-source anywhere in the codebase.

The safety property both changes rest on is the same: each is gated behind a
PHRASE predicate that is False for everything else, so no other card's
accounting moves. The Vivid predicate matches exactly two cards in all of
Scryfall; the library-top regex matches 27 and — the trap it exists to avoid
— ZERO of the 32 cascade cards, every one of which contains both "from the
top of your library" and "you may cast" in its reminder text.
"""
import asyncio
import io
import json
from types import SimpleNamespace

import pytest

from mtg.helpers import (colors_among_permanents, library_top_cast_types,
                         is_vivid_mana_line, LIBRARY_TOP_CAST_RE)
from mtg.models import Player
from mtg.legal_actions import castable_entries


def _cache():
    return json.load(io.open("data/card_data_cache.json", encoding="utf-8"))


def _oracle(name):
    """Real printed text. A fixture I author is not evidence about a card."""
    return _cache()[name.lower()]["oracle_text"]


def _engine():
    from mtg.engine import GameEngine
    return GameEngine(None)


def _stub_cog():
    """The autoplay executor announces successful casts through the cog's
    Discord funnel; the failure path returns before it, which is why only
    the success test needs this."""
    async def _send(*_a, **_kw):
        return None
    return SimpleNamespace(engine=_engine(), _autoplay_send=_send)


def _land(make_card, name, produces):
    return make_card(name, type_line=f"Basic Land — {name}",
                     oracle_text=f"{{T}}: Add {{{produces}}}.",
                     power=None, toughness=None)


def _bloom_tender(make_card):
    return make_card("Bloom Tender", type_line="Creature — Elf Druid",
                     oracle_text=_oracle("Bloom Tender"),
                     mana_cost="{1}{G}", power="1", toughness="1")


# ---------------------------------------------------------------------------
# VIVID
# ---------------------------------------------------------------------------

class TestVividIsAManaSource:
    def test_bloom_tender_is_an_untapped_mana_source(self, make_card):
        """It was absent entirely: the ability names no mana SYMBOL, so
        neither the "{t}: add" nor the "add {" check in _can_produce_mana
        could see it."""
        p = Player(name="P")
        bt = _bloom_tender(make_card)
        p.battlefield = [bt]
        assert p._can_produce_mana(bt)
        assert any(c.name == "Bloom Tender" for c in p.untapped_mana_sources())

    def test_faeburrow_elder_matches_without_the_ability_word(self, make_card):
        """Faeburrow Elder prints the SAME ability with no "Vivid —" prefix.
        Matching the phrase rather than the ability word covers both — and is
        why this is not a keyword parser."""
        p = Player(name="P")
        fae = make_card(
            "Faeburrow Elder", type_line="Creature — Treefolk Druid",
            mana_cost="{1}{G}{W}", power="0", toughness="3",
            oracle_text=("Vigilance\nThis creature gets +1/+1 for each color "
                         "among permanents you control.\n{T}: For each color "
                         "among permanents you control, add one mana of that "
                         "color."))
        p.battlefield = [fae]
        assert p._produces_all_colors_at_once(fae)

    def test_counting_the_colours_is_not_the_mana_ability(self, make_card):
        """THE bug the four-lens review found, two reviewers independently.

        "for each color among permanents you control" is a COUNTING phrase
        that nine Scryfall cards share; only two of them add mana with it.
        The first predicate tested the count alone, so Soul of Ravnica (a
        DRAW ability) and Conqueror's Flail (an EQUIPMENT that pumps) became
        mana sources producing one mana of every colour on board — mana from
        nothing (CR 106.1) and an underpaid cast (CR 601.2g). Both halves
        must appear on ONE line."""
        p = Player(name="P")
        soul = make_card("Soul of Ravnica", type_line="Creature — Avatar",
                         mana_cost="{5}{U}{U}", power="6", toughness="6",
                         oracle_text="Flying\n{5}{U}{U}: Draw a card for each "
                                     "color among permanents you control.")
        flail = make_card("Conqueror's Flail", type_line="Artifact — Equipment",
                          mana_cost="{2}", power=None, toughness=None,
                          oracle_text="Equipped creature gets +1/+1 for each "
                                      "color among permanents you control.")
        p.battlefield = [soul, flail]
        assert not p._can_produce_mana(soul)
        assert not p._can_produce_mana(flail)
        assert p.untapped_mana_sources() == []
        assert p.one_tap_mana_total() == 0

    def test_chromatic_orrery_keeps_its_real_mana_ability(self, make_card):
        """The regression the loose predicate caused: Orrery HAS a real
        {T}: Add {C}{C}{C}{C}{C}, and also counts colours on a different
        line. The Vivid branch sits ABOVE the oracle Add-scan, so a loose
        match intercepted a rock that already worked — repricing it to one
        mana per colour, or to NOTHING on a colourless board."""
        p = Player(name="P")
        orrery = make_card(
            "Chromatic Orrery", type_line="Legendary Artifact",
            mana_cost="{7}", power=None, toughness=None,
            oracle_text="You may spend mana as though it were mana of any "
                        "color.\n{T}: Add {C}{C}{C}{C}{C}.\n{5}, {T}: Draw a "
                        "card for each color among permanents you control.")
        p.battlefield = [orrery, make_card("Forest",
                                           type_line="Basic Land — Forest",
                                           oracle_text="{T}: Add {G}.",
                                           power=None, toughness=None)]
        assert not p._produces_all_colors_at_once(orrery)
        # {'C': 1}, not {'C': 5}: the generic Add-line scan credits one per
        # SYMBOL TYPE rather than per repeat. That under-count is
        # PRE-EXISTING and in the safe direction — the point here is only
        # that the Vivid branch does not intercept, so Orrery still reaches
        # that scan and still reports colourless.
        assert p._get_mana_production(orrery) == {"C": 1}

    def test_exactly_two_cards_in_all_of_scryfall(self):
        """The claim the docstring makes, measured rather than asserted —
        the first version of that docstring said 'exactly two' while the
        predicate matched nine."""
        try:
            bulk = json.load(io.open("data/scryfall_oracle_cards.json",
                                     encoding="utf-8"))
        except (OSError, ValueError):
            pytest.skip("Scryfall bulk not present")
        from mtg.helpers import is_vivid_mana_line
        hits = sorted({c["name"] for c in bulk
                       if is_vivid_mana_line(c.get("oracle_text") or "")})
        assert hits == ["Bloom Tender", "Faeburrow Elder"], hits

    def test_predicate_is_false_for_duals_and_signets(self, make_card):
        """The whole safety argument: every other source is untouched."""
        p = Player(name="P")
        dual = make_card("Hallowed Fountain", type_line="Land — Plains Island",
                         oracle_text="{T}: Add {W} or {U}.",
                         power=None, toughness=None)
        signet = make_card("Azorius Signet", type_line="Artifact",
                           oracle_text="{1}, {T}: Add {W}{U}.",
                           power=None, toughness=None)
        assert not p._produces_all_colors_at_once(dual)
        assert not p._produces_all_colors_at_once(signet)


class TestVividProduction:
    def test_basics_are_colourless_so_bloom_tender_sees_only_itself(
            self, make_card):
        """Looks like a bug and is the printed rule. Lands have no mana cost,
        so they are colourless (CR 202.2) and contribute NO colour. Bloom
        Tender beside four basics taps for a single {G} — its own {1}{G}. An
        implementation that counted basic land TYPES would say four."""
        p = Player(name="P")
        bt = _bloom_tender(make_card)
        p.battlefield = [bt, _land(make_card, "Forest", "G"),
                         _land(make_card, "Island", "U"),
                         _land(make_card, "Plains", "W"),
                         _land(make_card, "Swamp", "B")]
        assert p._get_mana_production(bt) == {"G": 1}

    def test_counts_every_colour_among_permanents(self, make_card):
        p = Player(name="P")
        bt = _bloom_tender(make_card)
        p.battlefield = [
            bt,
            make_card("Thrasios", type_line="Legendary Creature — Merfolk",
                      mana_cost="{G}{U}"),
            make_card("Tymna", type_line="Legendary Creature — Human",
                      mana_cost="{1}{W}{B}"),
        ]
        assert p._get_mana_production(bt) == {"B": 1, "G": 1, "U": 1, "W": 1}

    def test_no_coloured_permanents_produces_nothing(self, make_card):
        """Must not fabricate mana. The dead `{'any': 1}` entry would have."""
        p = Player(name="P")
        rock = make_card("Colourless Thing", type_line="Artifact Creature",
                         mana_cost="{2}", power="1", toughness="1",
                         oracle_text=_oracle("Bloom Tender"))
        p.battlefield = [rock]
        assert sum(p._get_mana_production(rock).values()) == 0

    def test_uses_the_scryfall_colors_attribute_production_takes(
            self, make_card):
        """`deck_loader` stamps `card.colors` on EVERY loaded card, so the
        colors branch — not the mana-cost fallback — is what production
        runs. Every other colour test here builds bare Cards and therefore
        exercises only the fallback; without this one the production path
        would be untested. Dryad Arbor is the case where they differ: green
        by colour indicator, with no mana cost at all."""
        p = Player(name="P")
        bt = _bloom_tender(make_card)
        bt.colors = ["G"]
        arbor = make_card("Dryad Arbor",
                          type_line="Land Creature — Forest Dryad",
                          mana_cost="", power="1", toughness="1")
        arbor.colors = ["G"]
        sphinx = make_card("Test Sphinx", type_line="Creature — Sphinx",
                           mana_cost="{4}{U}")
        sphinx.colors = ["U"]
        p.battlefield = [bt, arbor, sphinx]
        assert colors_among_permanents(p) == {"G", "U"}
        assert p._get_mana_production(bt) == {"G": 1, "U": 1}

    def test_colors_among_permanents_skips_phased_out(self, make_card):
        p = Player(name="P")
        gone = make_card("Phased", type_line="Creature — Bear",
                         mana_cost="{U}{U}")
        gone._phased_out = True
        p.battlefield = [make_card("Here", type_line="Creature — Bear",
                                   mana_cost="{G}"), gone]
        assert colors_among_permanents(p) == {"G"}


class TestVividPayment:
    def _four_colour(self, make_card):
        p = Player(name="P")
        bt = _bloom_tender(make_card)
        p.battlefield = [
            bt,
            make_card("Thrasios", type_line="Legendary Creature — Merfolk",
                      mana_cost="{G}{U}"),
            make_card("Tymna", type_line="Legendary Creature — Human",
                      mana_cost="{1}{W}{B}"),
        ]
        return p, bt

    def test_one_tap_total_counts_all_four(self, make_card):
        """`one_tap_mana_total` is the advertisement CEILING. Left at max()
        it reports 1 and suppresses casts that are legal on the real board."""
        p, _ = self._four_colour(make_card)
        assert p.one_tap_mana_total() == 4

    def test_bloom_tender_alone_pays_a_two_colour_cost(self, make_card):
        """The whole card. One tap, {W} and {U} together."""
        p, bt = self._four_colour(make_card)
        assert p.tap_sources_for_cost("{W}{U}") is True
        assert bt.tapped

    def test_unspent_colours_float_to_the_pool(self, make_card):
        """Phase 4's existing settle does this once the production dict is
        not narrowed to a single committed colour."""
        p, _ = self._four_colour(make_card)
        p.tap_sources_for_cost("{W}{U}")
        assert p.mana_pool.get("B", 0) == 1
        assert p.mana_pool.get("G", 0) == 1

    def test_cannot_pay_a_DOUBLE_pip_of_one_colour(self, make_card):
        """Bloom Tender makes ONE {W}. It must not pay {W}{W}.

        This was a real bug introduced by the first version of this feature
        and caught by probing cost shapes rather than by any test: the colour
        check forgives a shortfall whenever total production exceeds total
        cost, an escape hatch meant for 'any' mana. Crediting all four of
        Bloom Tender's colours to the payment ledger made its unused {B} and
        {G} look like flexible surplus. The control that proved it was mine
        and not pre-existing: one Plains plus three off-colour basics
        correctly refuses {W}{W} (see the sibling test)."""
        p, _ = self._four_colour(make_card)
        assert p.tap_sources_for_cost("{W}{W}") is False

    def test_control_ordinary_basics_also_refuse_a_double_pip(self, make_card):
        """The control for the test above. If this ever fails, the escape
        hatch has broken generally and the Vivid finding was a symptom."""
        p = Player(name="P")
        p.battlefield = [_land(make_card, "Plains", "W"),
                         _land(make_card, "Island", "U"),
                         _land(make_card, "Swamp", "B"),
                         _land(make_card, "Forest", "G")]
        assert p.tap_sources_for_cost("{W}{W}") is False

    def test_cannot_pay_more_total_than_it_produces(self, make_card):
        """Four colours means four mana, not unlimited."""
        p, _ = self._four_colour(make_card)
        assert p.tap_sources_for_cost("{W}{U}{B}{G}") is True
        p2, _ = self._four_colour(make_card)
        assert p2.tap_sources_for_cost("{W}{U}{B}{G}{G}") is False

    def test_cannot_pay_a_pip_in_a_colour_it_does_not_make(self, make_card):
        """The decisive case for the Phase 3 generic branch's ledger cap.

        Cost {1}{R} on a W/U/B/G board: Phase 1 skips Bloom Tender (no red
        need it can fill), so the generic loop is what taps it. Crediting all
        four colours there — rather than capping at the shortfall — inflates
        the same wrong-colour surplus, and the red pip gets forgiven by a
        board with no red source at all."""
        p, _ = self._four_colour(make_card)
        assert p.tap_sources_for_cost("{1}{R}") is False

    def test_converge_sees_the_colours_spent(self, make_card):
        """A Vivid source has no committed colour by design, so it would
        otherwise be invisible to converge (CR 702.100a)."""
        p, _ = self._four_colour(make_card)
        p.tap_sources_for_cost("{W}{U}")
        assert set(p._last_colors_spent) >= {"W", "U"}

    def test_or_dual_still_taps_for_exactly_one(self, make_card):
        """CONTROL for the June 10 underpay fix: a dual must NOT gain the
        simultaneous reading. If this ever passes with {W}{U}, spells are
        resolving underpaid again (CR 601.2g)."""
        p = Player(name="P")
        p.battlefield = [make_card("Hallowed Fountain",
                                   type_line="Land — Plains Island",
                                   oracle_text="{T}: Add {W} or {U}.",
                                   power=None, toughness=None)]
        assert p.one_tap_mana_total() == 1
        assert p.tap_sources_for_cost("{W}{U}") is False


class TestVividExplicitActivation:
    def test_activate_mana_floats_every_colour(self, make_card, game):
        """The [ACTIVATE-MANA] path scans for {WUBRG} symbols, and this
        ability names none — so without a branch it fell through every
        check to a generic guess."""
        eng = _engine()
        p = game.players[0]
        bt = _bloom_tender(make_card)
        p.battlefield = [bt,
                         make_card("Thrasios",
                                   type_line="Legendary Creature — Merfolk",
                                   mana_cost="{G}{U}")]
        msg = asyncio.run(eng._execute_action(
            game, 0, {"type": "activate", "permanent": "Bloom Tender"}))
        assert msg and "Bloom Tender" in msg
        assert p.mana_pool.get("G", 0) == 1
        assert p.mana_pool.get("U", 0) == 1
        assert bt.tapped


# ---------------------------------------------------------------------------
# COVEN — cast from the top of the library
# ---------------------------------------------------------------------------

def _augur(make_card):
    return make_card("Augur of Autumn", type_line="Creature — Human Druid",
                     oracle_text=_oracle("Augur of Autumn"),
                     mana_cost="{1}{G}{G}", power="2", toughness="2")


def _coven_board(make_game, make_card, powers=("3", "4")):
    game = make_game()
    p = game.players[0]
    p.battlefield = [_augur(make_card)]
    for i, pw in enumerate(powers):
        p.battlefield.append(make_card(f"Bear {i}", power=pw, toughness=pw))
    for _ in range(3):
        p.battlefield.append(_land(make_card, "Forest", "G"))
    top = make_card("Llanowar Elves", type_line="Creature — Elf Druid",
                    mana_cost="{G}", power="1", toughness="1")
    p.library = [top, _land(make_card, "Forest", "G")]
    return game, p, top


class TestCovenGrant:
    def test_grant_requires_three_DIFFERENT_powers(self, make_game, make_card):
        """Coven counts distinct powers, not creatures. Augur(2) + 3 + 3 is
        three creatures and only two distinct powers."""
        game, p, _ = _coven_board(make_game, make_card, powers=("3", "3"))
        assert library_top_cast_types(p, game) == set()
        p.battlefield[2].power = "4"
        assert library_top_cast_types(p, game) == {"creature"}

    def test_cascade_does_not_grant_library_casting(self, make_game, make_card):
        """THE trap. Every cascade card contains "from the top of your
        library" (exile clause) and "You may cast" (free-cast clause); a
        two-substring test hands library casting to 32 cards."""
        game = make_game()
        p = game.players[0]
        p.battlefield = [make_card(
            "Bloodbraid Elf", type_line="Creature — Elf Berserker",
            oracle_text=("Haste\nCascade (When you cast this spell, exile "
                         "cards from the top of your library until you exile "
                         "a nonland card that costs less. You may cast it "
                         "without paying its mana cost.)"))]
        assert library_top_cast_types(p, game) == set()

    def test_unconditional_grant_needs_no_coven(self, make_game, make_card):
        game = make_game()
        p = game.players[0]
        p.battlefield = [make_card(
            "Vizier of the Menagerie", type_line="Creature — Naga Cleric",
            oracle_text="You may cast creature spells from the top of your "
                        "library.")]
        assert library_top_cast_types(p, game) == {"creature"}

    def test_unmodelled_additional_cost_declines(self, make_game, make_card):
        """Falco Spara charges "by removing a counter". Granting it free is
        worse than not granting it (the buyback/splice convention).

        Note WHY it declines: its grant is untyped ("spells"), so the
        creature-only gate rejects it and the rider check never decides the
        case. That makes this pin real but not decisive for the rider check
        — see the next test, which is.
        """
        game = make_game()
        p = game.players[0]
        p.battlefield = [make_card(
            "Falco Spara, Pactweaver", type_line="Legendary Creature",
            oracle_text="You may cast spells from the top of your library by "
                        "removing a counter from a creature you control in "
                        "addition to paying their other costs.")]
        assert library_top_cast_types(p, game) == set()

    def test_rider_check_decides_a_grant_the_type_gate_would_accept(
            self, make_game, make_card):
        """The rider check must reject on its own, not by luck of the type
        gate. No printed card currently combines a CREATURE grant with an
        unmodelled rider, so this fixture is synthetic on purpose: the check
        is forward-defence for the day the type gate widens to the subtype
        grants (Goblin, Dragon, ...), which the docstring advertises as a
        one-comparison change. Without it, that widening silently grants a
        free version of a card that charges an additional cost."""
        game = make_game()
        p = game.players[0]
        p.battlefield = [make_card(
            "Synthetic Rider Test", type_line="Creature — Test",
            oracle_text="You may cast creature spells from the top of your "
                        "library by removing a counter from a creature you "
                        "control.")]
        assert library_top_cast_types(p, game) == set()

    def test_unmodelled_condition_declines(self, make_game, make_card):
        """Summoning Materia's condition is attachment, which we do not read.
        It must not read as permanently satisfied."""
        game = make_game()
        p = game.players[0]
        p.battlefield = [make_card(
            "Summoning Materia", type_line="Artifact — Equipment",
            oracle_text="As long as this Equipment is attached to a creature, "
                        "you may cast creature spells from the top of your "
                        "library.")]
        assert library_top_cast_types(p, game) == set()

    def test_class_level_gate_declines(self, make_game, make_card):
        """Ranger Class prints the grant under "{3}{G}: Level 3" on its own
        LINE, so neither the ability-word stripper nor the "as long as"
        check can see the gate — and Class levels (CR 716.2) are modelled
        nowhere. It read as unconditional, granting from the turn the Class
        landed for the rest of the game without paying the level cost."""
        game = make_game()
        p = game.players[0]
        p.battlefield = [make_card(
            "Ranger Class", type_line="Enchantment — Class",
            power=None, toughness=None,
            oracle_text="(Gain the next level as a sorcery to add its "
                        "ability.)\nWhen this Class enters, create a 2/2 "
                        "green Wolf creature token.\n{1}{G}: Level 2\n"
                        "{3}{G}: Level 3\nYou may look at the top card of "
                        "your library any time.\nYou may cast creature "
                        "spells from the top of your library.")]
        assert library_top_cast_types(p, game) == set()

    def test_phased_out_creatures_do_not_form_coven(self, make_game, make_card):
        """CR 702.26b — a phased-out permanent is treated as though it does
        not exist. `has_coven` had no consumer until now, so the skip its
        two neighbours already had was missing."""
        game, p, _ = _coven_board(make_game, make_card)
        for c in p.battlefield:
            if c.name.startswith("Bear"):
                c._phased_out = True
        assert library_top_cast_types(p, game) == set()

    def test_regex_rejects_every_cascade_card_in_scryfall(self):
        """Measured, not reasoned. Skips when the bulk file is absent."""
        try:
            bulk = json.load(io.open("data/scryfall_oracle_cards.json",
                                     encoding="utf-8"))
        except (OSError, ValueError):
            pytest.skip("Scryfall bulk not present")
        cascade_hits = [c["name"] for c in bulk
                        if "cascade" in (c.get("oracle_text") or "").lower()
                        and LIBRARY_TOP_CAST_RE.search(c.get("oracle_text") or "")]
        assert cascade_hits == []


class TestCovenOffer:
    def test_top_creature_is_offered(self, make_game, make_card):
        game, p, _ = _coven_board(make_game, make_card)
        labels = [e["label"] for e in castable_entries(
            game, p, {"G": 3}, 0, 3)]
        assert any("Llanowar Elves" in l and "TOP OF LIBRARY" in l
                   for l in labels), labels

    def test_no_offer_without_coven(self, make_game, make_card):
        game, p, _ = _coven_board(make_game, make_card, powers=("3", "3"))
        labels = [e["label"] for e in castable_entries(
            game, p, {"G": 3}, 0, 3)]
        assert not any("TOP OF LIBRARY" in l for l in labels), labels

    def test_land_on_top_is_not_offered(self, make_game, make_card):
        """The coven clause grants CREATURE spells. Augur's separate
        play-lands-from-the-top half is a different mechanic and is not
        modelled (Oracle of Mul Daya / Courser of Kruphix have never been)."""
        game, p, _ = _coven_board(make_game, make_card)
        p.library.insert(0, _land(make_card, "Island", "U"))
        labels = [e["label"] for e in castable_entries(
            game, p, {"G": 3}, 0, 3)]
        assert not any("TOP OF LIBRARY" in l for l in labels), labels

    def test_noncreature_nonland_on_top_is_not_offered(
            self, make_game, make_card):
        """Decisive for the creature-type check specifically. A plain land is
        caught by the is_land guard too, so it cannot tell the two apart; a
        sorcery is caught by the type check ALONE."""
        game, p, _ = _coven_board(make_game, make_card)
        p.library.insert(0, make_card("Lightning Bolt", type_line="Instant",
                                      mana_cost="{R}", power=None,
                                      toughness=None))
        labels = [e["label"] for e in castable_entries(
            game, p, {"G": 3, "R": 1}, 0, 3)]
        assert not any("TOP OF LIBRARY" in l for l in labels), labels

    def test_land_creature_on_top_is_not_offered(self, make_game, make_card):
        """Decisive for the is_land guard: Dryad Arbor is a Land Creature, so
        the type check would accept it. You PLAY a land, you never cast it."""
        game, p, _ = _coven_board(make_game, make_card)
        p.library.insert(0, make_card("Dryad Arbor",
                                      type_line="Land Creature — Forest Dryad",
                                      mana_cost="", power="1", toughness="1"))
        labels = [e["label"] for e in castable_entries(
            game, p, {"G": 3}, 0, 3)]
        assert not any("TOP OF LIBRARY" in l for l in labels), labels

    def test_offer_tracks_the_live_top_card(self, make_game, make_card):
        """Never cached — the top changes on every draw, mill and scry."""
        game, p, _ = _coven_board(make_game, make_card)
        p.library.insert(0, make_card("Grizzly Bears",
                                      type_line="Creature — Bear",
                                      mana_cost="{1}{G}", power="2",
                                      toughness="2"))
        labels = [e["label"] for e in castable_entries(
            game, p, {"G": 3}, 0, 3)]
        top_labels = [l for l in labels if "TOP OF LIBRARY" in l]
        assert len(top_labels) == 1
        assert "Grizzly Bears" in top_labels[0]


class TestCovenReachability:
    def test_plan_validator_accepts_the_top_card(self, make_game, make_card):
        """Without this the offer is decorative: _validate_plan_mana drops
        any cast whose card is not in its name set, and library was absent.
        That gap is what made the alternate-cost mechanics inert until
        July 28."""
        from mtg.ai_turn import _validate_plan_mana
        game, p, _ = _coven_board(make_game, make_card)
        plan = [{"type": "cast", "card": "Llanowar Elves"}]
        out = _validate_plan_mana(_engine(), game, 0, plan)
        assert any(a.get("card") == "Llanowar Elves" for a in out), out

    def test_plan_validator_refuses_a_land_creature_on_top(
            self, make_game, make_card):
        """`hand_names` also admits `play_land`, so a missing is_land guard
        here does more than waste a cast slot: a planned Dryad Arbor land
        drop would pass validation, consume the turn's land drop and credit
        a mana that never arrives, while every executor still refuses it."""
        from mtg.ai_turn import _validate_plan_mana
        game, p, _ = _coven_board(make_game, make_card)
        p.library.insert(0, make_card("Dryad Arbor",
                                      type_line="Land Creature — Forest Dryad",
                                      mana_cost="", power="1", toughness="1"))
        for act in ({"type": "cast", "card": "Dryad Arbor"},
                    {"type": "play_land", "card": "Dryad Arbor"}):
            out = _validate_plan_mana(_engine(), game, 0, [act])
            assert not any(a.get("card") == "Dryad Arbor" for a in out), out

    def test_prompt_mana_is_derived_from_the_payment_engines_source_list(self):
        """A summoning-sick Bloom Tender must not be advertised to the AI.

        Both prompt builders used to re-express "usable mana source" as
        `not tapped and _can_produce_mana(...)`, omitting CR 302.6, which
        `untapped_mana_sources()` enforces and the payment engine obeys. That
        made the advertised mana up to five higher than the payable mana, in
        colours the player might have no other source for.

        This is a STRUCTURAL pin and says so: the loops live inside async
        methods that return before building a prompt when there is no LLM
        client, so there is no cheap behavioural route. It asserts the two
        builders derive from the authoritative list rather than restating the
        rule — which is the property that broke — and it does NOT verify the
        resulting numbers. The rule itself is pinned behaviourally at the
        models level (`untapped_mana_sources` excludes a sick dork)."""
        from pathlib import Path
        src = (Path(__file__).resolve().parent.parent
               / "mtg/claude_player.py").read_text(encoding="utf-8")
        assert src.count("id(perm) in _usable_sources") == 2, (
            "both prompt builders must consult untapped_mana_sources()")
        assert "not perm.tapped and player._can_produce_mana(perm)" not in src, (
            "the re-expressed rule is back; it omits summoning sickness")

    def test_a_summoning_sick_vivid_source_is_not_a_usable_source(
            self, make_card):
        """The behavioural half of the pin above (CR 302.6)."""
        p = Player(name="P")
        bt = _bloom_tender(make_card)
        bt.summoning_sick = True
        p.battlefield = [bt, make_card("Thrasios",
                                       type_line="Legendary Creature — Merfolk",
                                       mana_cost="{G}{U}")]
        assert p.untapped_mana_sources() == []
        assert p.one_tap_mana_total() == 0

    def test_the_prompt_exempts_the_new_tag_from_the_hand_only_rule(self):
        """Offering a card the prompt then forbids is the repo's documented
        'surfaced but suppressed' failure: both prompts say the hand is the
        only place cards exist, and the exemption NOTE listed only
        FLASHBACK / ESCAPE / COMPANION."""
        from pathlib import Path
        src = (Path(__file__).resolve().parent.parent
               / "mtg/claude_player.py").read_text(encoding="utf-8")
        assert src.count("'TOP OF LIBRARY' in c") == 2, (
            "both prompt builders must treat the tag as a not-in-hand offer")
        assert src.count("[TOP OF LIBRARY] are NOT in your hand") == 2

    def test_plan_validator_still_rejects_a_buried_card(
            self, make_game, make_card):
        """Only the TOP card is castable — not the whole library."""
        from mtg.ai_turn import _validate_plan_mana
        game, p, _ = _coven_board(make_game, make_card)
        buried = make_card("Grizzly Bears", type_line="Creature — Bear",
                           mana_cost="{1}{G}", power="2", toughness="2")
        p.library.append(buried)
        plan = [{"type": "cast", "card": "Grizzly Bears"}]
        out = _validate_plan_mana(_engine(), game, 0, plan)
        assert not any(a.get("card") == "Grizzly Bears" for a in out), out


class TestCovenCasting:
    def test_engine_casts_from_the_top(self, make_game, make_card):
        game, p, top = _coven_board(make_game, make_card)
        res = asyncio.run(_engine()._execute_action(
            game, 0, {"type": "cast", "card": "Llanowar Elves"}))
        assert res and "Llanowar Elves" in res
        assert top in p.battlefield
        assert top not in p.library
        assert top not in p.hand

    def test_engine_failure_returns_it_to_the_TOP(self, make_game, make_card):
        """Stranding a card in NO zone is the failure mode this repo has
        shipped twice (escape, wave 3a and batch-9 F4). Index 0 matters: it
        is what the next draw takes and what the offer list re-reads."""
        game, p, top = _coven_board(make_game, make_card)
        for c in p.battlefield:
            if c.name == "Forest":
                c.tapped = True
        before = len(p.library)
        res = asyncio.run(_engine()._execute_action(
            game, 0, {"type": "cast", "card": "Llanowar Elves"}))
        assert res is None
        assert p.library[0] is top
        assert top not in p.hand
        assert len(p.library) == before

    def test_autoplay_casts_from_the_top(self, make_game, make_card):
        """The second of three executors. A fix in one is a fix in one."""
        from mtg.autoplay import _autoplay_execute_action
        game, p, top = _coven_board(make_game, make_card)
        cog = _stub_cog()
        asyncio.run(_autoplay_execute_action(
            cog, None, game, 0, {"type": "cast", "card": "Llanowar Elves"}))
        assert top in p.battlefield
        assert top not in p.library

    def test_autoplay_failure_returns_it_to_the_TOP(self, make_game, make_card):
        from mtg.autoplay import _autoplay_execute_action
        game, p, top = _coven_board(make_game, make_card)
        for c in p.battlefield:
            if c.name == "Forest":
                c.tapped = True
        cog = _stub_cog()
        before = len(p.library)
        asyncio.run(_autoplay_execute_action(
            cog, None, game, 0, {"type": "cast", "card": "Llanowar Elves"}))
        assert p.library[0] is top
        assert top not in p.hand
        assert len(p.library) == before


class TestAllThreeExecutors:
    def test_every_cast_executor_handles_the_library_top(self):
        """cog.py is the human `!play` path and cannot be driven headless,
        so it gets a structural check. engine.py and autoplay.py are pinned
        BEHAVIOURALLY above — this exists so the third path cannot be the
        one that silently diverges, which is a documented recurring bug."""
        from pathlib import Path
        root = Path(__file__).resolve().parent.parent
        for mod in ("mtg/engine.py", "mtg/autoplay.py", "mtg/cog.py"):
            src = (root / mod).read_text(encoding="utf-8")
            assert "library_top_cast_types" in src, (
                f"{mod} never consults the grant — the AI would be offered a "
                f"top-of-library cast this executor cannot perform")
            assert "player.library.insert(0, card)" in src or \
                   "library.insert(0, card)" in src, (
                f"{mod} has no rollback restoring the card to the TOP")
