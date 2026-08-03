"""Aug 3, 2026 — SPLICE (CR 702.46), the last non-tail missing mechanic.

Splice is NOT a cost adjustment despite having been filed alongside affinity
and converge. It is a static ability that functions FROM HAND: as you cast a
spell of the named subtype you may reveal the splice card, pay its splice
cost as an additional cost, and add its effects to that spell. The revealed
card never leaves your hand (CR 702.46c) — which is what makes it unlike
every other cost in this family, all of which read the card BEING CAST.

Reachability, checked before a line was written (the handoff's instruction):
Through the Breach is the only splice card in the deck inventory and the only
Arcane spell in either mythic deck is Through the Breach ITSELF, so in a
singleton format it could never splice onto anything. But the sweep over all
38,416 Scryfall cards found the premise was narrower than assumed — there are
97 Arcane cards and 33 splice cards, and Lava Spike (Sorcery — Arcane) was
already sitting at 4x in both burn decks. Adding Glacial Ray (common,
pauper-legal, "Splice onto Arcane {1}{R}") to test_burn_pauper makes the
mechanic live against a splice target the deck already ran.

All oracle text below is the REAL printed text, verbatim from Scryfall bulk.
"""
import asyncio

from mtg import helpers
from mtg.engine import GameEngine

from tests.conftest import _make_card, _make_game


_REMINDER = ("(As you cast an Arcane spell, you may reveal this card from "
             "your hand and pay its splice cost. If you do, add this card's "
             "effects to that spell.)")

GLACIAL_RAY = ("Glacial Ray deals 2 damage to any target.\n"
               "Splice onto Arcane {1}{R} " + _REMINDER)
LAVA_SPIKE = "Lava Spike deals 3 damage to target player or planeswalker."
THROUGH_THE_BREACH = (
    "You may put a creature card from your hand onto the battlefield. That "
    "creature gains haste. Sacrifice that creature at the beginning of the "
    "next end step.\n"
    "Splice onto Arcane {2}{R}{R} " + _REMINDER)
KODAMAS_MIGHT = ("Target creature gets +2/+2 until end of turn.\n"
                 "Splice onto Arcane {G} " + _REMINDER)
# Kamigawa: Neon Dynasty prints the subtype phrase in LOWERCASE.
EVERDREAM = ("Draw a card.\n"
             "Splice onto instant or sorcery {2}{U} (As you cast an instant "
             "or sorcery spell, you may reveal this card from your hand and "
             "pay its splice cost. If you do, add this card's effects to "
             "that spell.)")
# Non-mana em-dash splice costs — five real cards print them. v1 declines.
TORRENT_OF_STONE = ("Torrent of Stone deals 4 damage to target creature.\n"
                    "Splice onto Arcane—Sacrifice two Mountains. " + _REMINDER)
ROAR_OF_JUKAI = ("If you control a Forest, each blocked creature gets +2/+2 "
                 "until end of turn.\n"
                 "Splice onto Arcane—An opponent gains 5 life. " + _REMINDER)
# Rules text that MENTIONS splice without having it.
MINAMOS_MEDDLING = ("Counter target spell. That spell's controller reveals "
                    "their hand, then discards each card with the same name "
                    "as a card spliced onto that spell.")
# "Splice onto Anything" (Nevermind) parses, but matches no real type line.
NEVERMIND = "Counter target spell.\nSplice onto Anything {1}{R} " + _REMINDER


def _engine(game):
    engine = GameEngine(None)
    game._rules_engine = engine.rules
    engine.rules.engine_ref = engine
    return engine


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _mountains(player, n):
    for _ in range(n):
        player.battlefield.append(_make_card(
            "Mountain", type_line="Basic Land — Mountain",
            oracle_text="{T}: Add {R}."))


def _glacial_ray():
    card = _make_card("Glacial Ray", type_line="Instant — Arcane",
                      oracle_text=GLACIAL_RAY, mana_cost="{1}{R}")
    card.cmc = 2
    return card


def _lava_spike():
    card = _make_card("Lava Spike", type_line="Sorcery — Arcane",
                      oracle_text=LAVA_SPIKE, mana_cost="{R}")
    card.cmc = 1
    return card


def _breach():
    card = _make_card("Through the Breach", type_line="Instant — Arcane",
                      oracle_text=THROUGH_THE_BREACH, mana_cost="{4}{R}")
    card.cmc = 5
    return card


class TestSpliceParser:
    def test_reads_the_subtype_and_cost(self):
        assert helpers.parse_splice(GLACIAL_RAY) == ("arcane", "{1}{R}")
        assert helpers.parse_splice(THROUGH_THE_BREACH) == ("arcane", "{2}{R}{R}")

    def test_reads_the_neon_dynasty_lowercase_subtype_phrase(self):
        assert helpers.parse_splice(EVERDREAM) == ("instant or sorcery", "{2}{U}")

    def test_declines_non_mana_em_dash_costs(self):
        """Five real cards print a non-mana splice cost. Paying an unmodeled
        cost for free would be strictly worse than not splicing — the same
        call buyback's life/sacrifice forms got."""
        assert helpers.parse_splice(TORRENT_OF_STONE) is None
        assert helpers.parse_splice(ROAR_OF_JUKAI) is None

    def test_declines_rules_text_that_merely_mentions_splice(self):
        """Minamo's Meddling says "a card spliced onto that spell". The word
        is "spliced", so \\bsplice\\b never matches and no line is even
        considered."""
        assert helpers.parse_splice(MINAMOS_MEDDLING) is None

    def test_declines_a_card_with_no_splice_at_all(self):
        assert helpers.parse_splice(LAVA_SPIKE) is None
        assert helpers.parse_splice("") is None


class TestSubtypeMatching:
    def test_arcane_matches_only_arcane(self):
        assert helpers.splice_matches_spell("arcane", "Instant — Arcane")
        assert helpers.splice_matches_spell("arcane", "Sorcery — Arcane")
        assert not helpers.splice_matches_spell("arcane", "Instant")
        assert not helpers.splice_matches_spell("arcane", "Creature — Goblin")

    def test_instant_or_sorcery_matches_either(self):
        assert helpers.splice_matches_spell("instant or sorcery", "Instant")
        assert helpers.splice_matches_spell("instant or sorcery", "Sorcery — Arcane")
        assert not helpers.splice_matches_spell("instant or sorcery",
                                                "Creature — Human Monk")

    def test_anything_matches_nothing_rather_than_everything(self):
        """Nevermind's "Splice onto Anything" parses, but "anything" appears
        in no type line, so the card simply never splices. Failing safe falls
        out of the general rule instead of needing a special case."""
        assert helpers.parse_splice(NEVERMIND) == ("anything", "{1}{R}")
        assert not helpers.splice_matches_spell("anything", "Instant — Arcane")
        assert not helpers.splice_matches_spell("anything", "Sorcery — Arcane")


class TestStripSpliceLine:
    def test_removes_only_the_splice_line(self):
        assert helpers.strip_splice_line(GLACIAL_RAY) == (
            "Glacial Ray deals 2 damage to any target.")

    def test_leaves_a_card_without_splice_untouched(self):
        assert helpers.strip_splice_line(LAVA_SPIKE) == LAVA_SPIKE


class TestSpliceCandidates:
    def test_a_lone_splice_card_cannot_splice_onto_itself(self):
        """THE load-bearing exclusion. _compute_alt_costs runs BEFORE the card
        leaves hand, so the spell being cast is still in player.hand — without
        the identity check Through the Breach would splice onto ITSELF,
        charging {2}{R}{R} to resolve its own text twice.

        Decisive fixture: exactly one splice card in hand, and it IS the
        spell being cast."""
        game = _make_game("modern")
        rick, _ = game.players
        breach = _breach()
        rick.hand.append(breach)
        assert helpers.splice_candidates(game, rick, breach) == []

    def test_a_second_copy_may_splice_onto_the_first(self):
        """The exclusion is by IDENTITY, not by name. In any 4-of format
        splicing copy B onto copy A is legal, and a name test would forbid
        it — so this is the pin that fails if someone "fixes" the self-splice
        case with a name comparison."""
        game = _make_game("modern")
        rick, _ = game.players
        first, second = _glacial_ray(), _glacial_ray()
        rick.hand.extend([first, second])
        candidates = helpers.splice_candidates(game, rick, first)
        assert [c for c, _ in candidates] == [second]

    def test_subtype_must_match_the_spell_being_cast(self):
        game = _make_game("modern")
        rick, _ = game.players
        ray = _glacial_ray()
        bolt = _make_card("Lightning Bolt", type_line="Instant",
                          oracle_text="Lightning Bolt deals 3 damage to any target.",
                          mana_cost="{R}")
        rick.hand.extend([ray, bolt])
        assert helpers.splice_candidates(game, rick, bolt) == [], (
            "Lightning Bolt is not Arcane")
        spike = _lava_spike()
        rick.hand.append(spike)
        assert [c for c, _ in helpers.splice_candidates(game, rick, spike)] == [ray]

    def test_cr_702_46b_declines_when_the_choices_cannot_be_made(self):
        """You may not splice a card whose required choices can't be made.

        Decisive on exactly that gate: the SAME hand and the SAME spell, once
        with no creature anywhere and once with one on the battlefield."""
        game = _make_game("modern")
        rick, claude = game.players
        might = _make_card("Kodama's Might", type_line="Instant — Arcane",
                           oracle_text=KODAMAS_MIGHT, mana_cost="{G}")
        spike = _lava_spike()
        rick.hand.extend([might, spike])
        assert helpers.splice_candidates(game, rick, spike) == [], (
            "no creature exists to target")
        claude.battlefield.append(_make_card("Bear", type_line="Creature — Bear"))
        assert [c for c, _ in helpers.splice_candidates(game, rick, spike)] == [might]

    def test_a_devotion_gated_god_is_not_a_creature_for_that_gate(self):
        """is_creature(game), not is_creature(). Erebos below five devotion
        is NOT a creature (CR 207.4), so he cannot satisfy a spliced "target
        creature" — the June-10 D4 class, where the bare call counts the god
        and the game-aware call does not.

        Decisive on exactly that argument: Erebos is the ONLY permanent on
        either battlefield, and {1}{B}{B} is devotion 2."""
        game = _make_game("modern")
        rick, claude = game.players
        erebos = _make_card(
            "Erebos, God of the Dead",
            type_line="Legendary Enchantment Creature — God",
            oracle_text="As long as your devotion to black is less than five, "
                        "Erebos isn't a creature.",
            mana_cost="{1}{B}{B}")
        claude.battlefield.append(erebos)
        might = _make_card("Kodama's Might", type_line="Instant — Arcane",
                           oracle_text=KODAMAS_MIGHT, mana_cost="{G}")
        spike = _lava_spike()
        rick.hand.extend([might, spike])
        assert helpers.splice_candidates(game, rick, spike) == [], (
            "a devotion-gated god below threshold is not a legal creature target")

    def test_candidates_are_ordered_cheapest_first_and_deterministically(self):
        game = _make_game("modern")
        rick, _ = game.players
        ray, breach = _glacial_ray(), _breach()
        spike = _lava_spike()
        rick.hand.extend([breach, ray, spike])
        ordered = [c.name for c, _ in helpers.splice_candidates(game, rick, spike)]
        assert ordered == ["Glacial Ray", "Through the Breach"]


class TestSpliceCasting:
    def _burn_game(self, mountains):
        game = _make_game("modern")
        rick, claude = game.players
        ray, spike = _glacial_ray(), _lava_spike()
        rick.hand.extend([ray, spike])
        _mountains(rick, mountains)
        return game, rick, claude, ray, spike

    def test_the_splice_cost_is_charged_on_top_of_the_spell(self):
        """Lava Spike is {R}; splicing Glacial Ray adds {1}{R}. Three sources
        tap, not one.

        Decisive: with only three Mountains the difference between splicing
        and not splicing is the whole board."""
        game, rick, claude, ray, spike = self._burn_game(3)
        engine = _engine(game)
        ok, msg, _ = _run(engine.cast_spell_async(game, rick, spike))
        assert ok, msg
        assert sum(1 for c in rick.battlefield if c.tapped) == 3, (
            "{R} for Lava Spike plus {1}{R} for the spliced Glacial Ray")

    def test_the_spliced_card_stays_in_hand(self):
        """CR 702.46c — it is only REVEALED. Nothing in the implementation
        removes it; this pins that absence."""
        game, rick, claude, ray, spike = self._burn_game(3)
        engine = _engine(game)
        _run(engine.cast_spell_async(game, rick, spike))
        assert ray in rick.hand, "the spliced card is revealed, never played"
        assert ray not in rick.graveyard

    def test_both_effects_resolve(self):
        """3 from Lava Spike plus 2 from the spliced Glacial Ray."""
        game, rick, claude, ray, spike = self._burn_game(3)
        start = claude.life
        engine = _engine(game)
        ok, msg, msgs = _run(engine.cast_spell_async(game, rick, spike))
        assert ok, msg
        assert claude.life == start - 5, (
            f"expected 3 + 2 = 5 damage, saw {start - claude.life}; {msgs}")

    def test_an_unaffordable_splice_is_declined_not_forced(self):
        """The branch is gated on can_pay_mana_cost, so a splice is only added
        when the enlarged total is payable. With one Mountain, Lava Spike
        still casts and simply does not splice.

        Deliberately NOT claiming "splice can never make a cast unpayable" in
        general — that claim was false while the affordability probe ignored
        cost INCREASES, and the tax case is pinned separately below.

        Decisive: the same hand that splices at three Mountains."""
        game, rick, claude, ray, spike = self._burn_game(1)
        start = claude.life
        engine = _engine(game)
        ok, msg, _ = _run(engine.cast_spell_async(game, rick, spike))
        assert ok, msg
        assert sum(1 for c in rick.battlefield if c.tapped) == 1
        assert claude.life == start - 3, "Lava Spike alone"
        assert ray in rick.hand

    def test_splice_is_re_decided_every_cast(self):
        """The stamp is per-cast state (CR 702.46a). A stale list would replay
        the previous cast's spliced effects for free — the same shape as the
        spectacle/kicker/buyback resets it sits beside.

        Decisive only if the SAME card object is cast twice. Casting a fresh
        second copy proves nothing now that _spliced_cards is a declared field
        defaulting to [] — an earlier version of this test did exactly that
        and the mutant that deletes the reset SURVIVED it. Returning the card
        to hand (buyback, a bounce) and recasting is also the real scenario
        the reset protects."""
        game, rick, claude, ray, spike = self._burn_game(3)
        engine = _engine(game)
        _run(engine.cast_spell_async(game, rick, spike))
        assert ray in rick.hand

        # Return the SAME Lava Spike to hand and re-cast it with the board
        # tapped out. The splice must be re-decided from scratch: no mana, so
        # no splice, and the previous cast's Glacial Ray must not resolve again.
        if spike in rick.graveyard:
            rick.graveyard.remove(spike)
        rick.hand.append(spike)
        for land in rick.battlefield:
            land.tapped = True
        mid = claude.life
        _run(engine.cast_spell_async(game, rick, spike))
        assert claude.life >= mid - 3, "no free replay of the spliced effect"


class TestSpliceReviewFindings:
    """The adversarial review wave over the splice diff (Aug 3). Four
    reviewers, zero flat false positives; these pin the fixes."""

    def test_a_cost_increase_is_visible_to_the_affordability_probe(self):
        """CR 601.2f puts increases in the total cost. The probe committed to
        the splice against the UN-taxed cost, so under a tax the tap then
        failed and an advertised-castable spell became a failed cast — the
        doomed-gate asymmetry pointing the wrong way.

        Decisive A/B on exactly the tax: three Mountains pays
        {R} + {1}{R} = 3 with no tax, and cannot pay it with +{1}."""
        for taxed in (False, True):
            game = _make_game("modern")
            rick, claude = game.players
            ray, spike = _glacial_ray(), _lava_spike()
            rick.hand.extend([ray, spike])
            _mountains(rick, 3)
            if taxed:
                claude.battlefield.append(_make_card(
                    "Thorn of Amethyst", type_line="Artifact",
                    oracle_text="Noncreature spells cost {1} more to cast."))
            start = claude.life
            engine = _engine(game)
            ok, msg, _ = _run(engine.cast_spell_async(game, rick, spike))
            # Observable outcomes only: _spliced_cards is cleared after
            # resolution, so reading it here would be vacuously true.
            assert ok, msg
            tapped = sum(1 for c in rick.battlefield if c.tapped)
            if taxed:
                assert (tapped, claude.life) == (2, start - 3), (
                    "under the tax the splice must be DECLINED — {R}+{1} is "
                    "payable, {R}{1}{R}+{1} is not")
            else:
                assert (tapped, claude.life) == (3, start - 5)

    def test_a_free_cast_declines_the_splice_rather_than_granting_it(self):
        """CR 601.2h — casting "without paying its mana cost" does NOT waive
        additional costs, and CR 702.46a makes the splice cost one. The
        splice branch runs while pay_mana is still True and a later branch
        flips it, so the effects were being handed over for nothing.

        v1 declines rather than charging separately (the scope the kicker
        branch documents for free casts); the safe direction is that the
        spell resolves without text it did not pay for."""
        game = _make_game("modern")
        rick, claude = game.players
        ray, spike = _glacial_ray(), _lava_spike()
        rick.hand.extend([ray, spike])
        _mountains(rick, 3)
        game.turn_effects = [{'type': 'free_cast', 'max_mv': 5,
                              'controller': 0, 'source': "Rishkar's Expertise",
                              'used': False}]
        start = claude.life
        engine = _engine(game)
        ok, msg, _ = _run(engine.cast_spell_async(game, rick, spike))
        assert ok, msg
        assert spike._spliced_cards == []
        assert claude.life == start - 3, "Lava Spike alone, no free Glacial Ray"
        assert ray in rick.hand

    def test_the_declared_target_is_forwarded_to_any_target_splice_text(self):
        """CR 601.2c — the caster chooses targets for EVERY instruction as the
        spell is cast, spliced ones included. Deferring to resolution let the
        spliced Glacial Ray auto-pick a CREATURE while the caster had aimed
        Lava Spike at the opponent's face.

        Decisive: a creature is on the board, so auto-targeting has something
        else to choose and the forward is what puts the damage on the face."""
        game = _make_game("modern")
        rick, claude = game.players
        ray, spike = _glacial_ray(), _lava_spike()
        rick.hand.extend([ray, spike])
        _mountains(rick, 3)
        blocker = _make_card("Kiln Fiend", type_line="Creature — Elemental Beast",
                             power="1", toughness="2")
        claude.battlefield.append(blocker)
        start = claude.life
        engine = _engine(game)
        ok, msg, msgs = _run(engine.cast_spell_async(game, rick, spike,
                                                     target=claude))
        assert ok, msg
        assert claude.life == start - 5, (
            f"3 + 2 both to the declared target; saw {start - claude.life}; {msgs}")
        assert blocker in claude.battlefield, "the creature was never targeted"

    def test_at_most_one_card_is_spliced_per_cast(self):
        """v1 policy. CR 702.46 allows any number, but greedy multi-splice is
        unbounded in a way the sibling additive costs are not, and each extra
        increment widens the gap between what the plan simulated and what the
        cast spends.

        Decisive: five Mountains would pay {R} + {1}{R} + {1}{R} = 5, so a
        greedy loop would take both Rays."""
        game = _make_game("modern")
        rick, claude = game.players
        first, second, spike = _glacial_ray(), _glacial_ray(), _lava_spike()
        rick.hand.extend([first, second, spike])
        _mountains(rick, 5)
        start = claude.life
        engine = _engine(game)
        ok, msg, _ = _run(engine.cast_spell_async(game, rick, spike))
        assert ok, msg
        assert sum(1 for c in rick.battlefield if c.tapped) == 3, (
            "one splice ({R} + {1}{R}); a greedy loop would tap 5")
        assert claude.life == start - 5, "3 + 2, not 3 + 2 + 2"
        assert first in rick.hand and second in rick.hand

    def test_cr_702_46b_reads_the_graveyard_for_graveyard_targeting_text(self):
        """Soulless Revival returns "target creature card from your
        GRAVEYARD". Scanning the battlefield answers a different question, so
        it was charged with an empty graveyard and did nothing.

        Decisive: a creature is on the BATTLEFIELD in both halves, so a
        battlefield scan would say yes both times."""
        revival_text = ("Return target creature card from your graveyard to "
                        "your hand.\nSplice onto Arcane {1}{B} " + _REMINDER)
        game = _make_game("modern")
        rick, claude = game.players
        claude.battlefield.append(_make_card("Bear", type_line="Creature — Bear"))
        revival = _make_card("Soulless Revival", type_line="Instant — Arcane",
                             oracle_text=revival_text, mana_cost="{1}{B}")
        spike = _lava_spike()
        rick.hand.extend([revival, spike])
        assert helpers.splice_candidates(game, rick, spike) == [], (
            "no creature card in any graveyard")
        rick.graveyard.append(_make_card("Dead Bear", type_line="Creature — Bear"))
        assert [c.name for c, _ in helpers.splice_candidates(game, rick, spike)] \
            == ["Soulless Revival"]

    def test_a_madness_cast_cannot_inherit_a_stale_splice_list(self):
        """The per-cast reset in _validate_cast is what stops a stale
        _spliced_cards from replaying, and this is the one path where it is
        still load-bearing.

        Everywhere else has grown its own guard: the success path clears after
        resolution, the splice branch assigns unconditionally on every
        pay_mana cast, and the CR 601.2h check clears on free casts. A MADNESS
        cast skips the splice branch (spells.py:797) while pay_mana stays
        True, so it slips past all three — without the reset it resolves
        whatever the previous cast left behind, for free.

        Decisive: the stale entry is planted, and the assertion is that the
        opponent takes Lava Spike's 3 and not Glacial Ray's extra 2."""
        game = _make_game("modern")
        rick, claude = game.players
        ray, spike = _glacial_ray(), _lava_spike()
        rick.hand.extend([ray, spike])
        _mountains(rick, 3)
        # State a failed earlier cast could leave on this object.
        spike._spliced_cards = [ray]
        spike._cast_via_madness = True
        start = claude.life
        engine = _engine(game)
        ok, msg, _ = _run(engine.cast_spell_async(game, rick, spike))
        assert ok, msg
        assert claude.life == start - 3, "the stale Glacial Ray must not resolve"
        assert ray in rick.hand

    def test_subtype_is_read_from_the_split_half_being_cast(self):
        """CR 702.46a names the subtype of the SPELL, and for a split card the
        spell is the HALF being cast. `spell_face_for_gates` is the canonical
        helper ("Every CR 601.2c gate must evaluate the half").

        Synthetic by necessity: no printed split card is Arcane, so this
        cannot be built from real cards — and without a pin the change is one
        a future refactor silently undoes. Adventure halves remain uncovered
        by the helper and err toward DECLINING, which is the safe direction.

        Decisive: face 0 is not Arcane and face 1 is, so reading the whole
        card gives the opposite answer for one of the two halves."""
        game = _make_game("modern")
        rick, _ = game.players
        ray = _glacial_ray()
        split = _make_card("Mundane // Kami", type_line="Instant",
                           oracle_text="Draw a card.", mana_cost="{U}")
        split.split_names = ["Mundane", "Kami"]
        split.split_types = ["Instant", "Instant — Arcane"]
        split.split_texts = ["Draw a card.", "Kami deals 1 damage to any target."]
        split.split_costs = ["{U}", "{R}"]
        rick.hand.extend([ray, split])

        split.cast_as_split_half = 0
        assert helpers.splice_candidates(game, rick, split) == [], (
            "the Mundane half is a plain Instant — nothing to splice onto")
        split.cast_as_split_half = 1
        assert [c.name for c, _ in helpers.splice_candidates(game, rick, split)] \
            == ["Glacial Ray"], "the Kami half IS Arcane"

    def test_spliced_cards_is_a_declared_field_not_an_attribute_staple(self):
        """Both reviewers flagged this independently. Its four cost-family
        siblings are declared transients in mtg/models.py; this one holds
        live Card references to cards that stay in HAND, so it matters more,
        not less."""
        from dataclasses import fields
        from mtg.models import Card
        names = {f.name for f in fields(Card)}
        assert "_spliced_cards" in names
        assert Card(name="X")._spliced_cards == []


class TestSpliceResolution:
    def test_an_unparseable_spliced_effect_escalates_instead_of_placeholding(self):
        """SpellResolver's "complex effect, manual resolution needed" marker
        must NOT count as a result: it is how Tier 2 says it could not parse
        the text, and treating it as output stops the cascade one tier early,
        leaving a splice that was PAID FOR with a placeholder and no effect.

        Decisive, and verified as such by mutation: the fixture's text does
        reach Tier 2 and does produce the marker, so removing the filter puts
        the marker straight into the cast's messages.

        The oracle text here is deliberately synthetic — of the 27 real
        splice cards, every one is handled at Tier 1 or parsed by Tier 2, so
        no printed card reaches this branch. Every other fixture in this file
        uses real printed text."""
        game = _make_game("modern")
        rick, _ = game.players
        unparseable = _make_card(
            "Unparseable Arcana", type_line="Instant — Arcane",
            oracle_text="Each player ponders the nature of the Kami.\n"
                        "Splice onto Arcane {R} " + _REMINDER,
            mana_cost="{R}")
        spike = _lava_spike()
        rick.hand.extend([unparseable, spike])
        _mountains(rick, 3)
        engine = _engine(game)
        ok, msg, msgs = _run(engine.cast_spell_async(game, rick, spike))
        assert ok, msg
        assert unparseable in rick.hand
        assert not any("complex effect" in m.lower() for m in msgs), msgs

    def test_nothing_spliced_resolves_nothing(self):
        game = _make_game("modern")
        rick, _ = game.players
        spike = _lava_spike()
        engine = _engine(game)
        assert _run(helpers_resolve(engine, game, rick, spike)) == []


def helpers_resolve(engine, game, player, card):
    from mtg.spells import _resolve_spliced_effects
    return _resolve_spliced_effects(engine, game, player, card)
