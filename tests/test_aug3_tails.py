"""Aug 3, 2026 — one-card tails from the missing-mechanics backlog.

These are single-card generators rather than a shared seam, so they are a
rolling backlog. The two here share the wave-2 shape that makes them worth
doing first: a CONDITION that was ignored, so the strong half fired
unconditionally and a generic pattern silently supplied the wrong answer.

PROBE CAVEAT, learned here: tools/probe_mechanics.py funnels everything
through resolve_etb(event_type=...), but production dispatches attack
triggers through resolve_attack_trigger — so an attack template can be
correctly registered and still look unhandled to the probe. These pins use
the production entry points.
"""
import json

import pytest

from rules.effect_templates import build_game_context, get_effect_library

from tests.conftest import _make_card, _make_game

CACHE = json.load(open("data/card_data_cache.json", encoding="utf-8"))
LIB = get_effect_library()


def _cached(name):
    e = CACHE[name]
    card = _make_card(e["name"], type_line=e["type_line"],
                      oracle_text=e["oracle_text"],
                      power=e.get("power") or "1",
                      toughness=e.get("toughness") or "1",
                      mana_cost=e["mana_cost"])
    card.cmc = int(e.get("cmc") or 0)
    return card


class TestPackTactics:
    """Pack tactics (CR 207.2c): "Whenever this creature attacks, IF you
    attacked with creatures with total power 6 or greater this combat, draw a
    card." A generic "whenever this attacks, draw a card" pattern matched and
    drew EVERY combat — free card advantage the card does not have."""

    def _attack(self, buddies):
        game = _make_game()
        rick, claude = game.players
        leader = _cached("werewolf pack leader")
        leader.attacking = True
        rick.battlefield.append(leader)
        for i in range(buddies):
            bear = _make_card(f"Bear {i}", type_line="Creature — Bear",
                              power="3", toughness="3")
            bear.attacking = True
            rick.battlefield.append(bear)
        ctx = build_game_context(game, rick, claude, card=leader,
                                 attacking_creature=leader)
        actions, _desc = LIB.resolve_attack_trigger(
            leader.name, leader.oracle_text, leader.name,
            int(leader.power), rick.name, claude.name, game_context=ctx)
        return actions

    def test_declines_below_six_total_power(self):
        actions = self._attack(0)          # the 3/3 alone
        assert actions and actions[0]["action"] == "no_action"
        assert "pack tactics" in actions[0]["reason"].lower()

    def test_draws_at_exactly_six(self):
        # Decisive on the boundary: 3 + 3 = 6 is "6 or greater".
        actions = self._attack(1)
        assert [a["action"] for a in actions] == ["draw_cards"]
        assert actions[0]["amount"] == 1

    def test_draws_above_six(self):
        actions = self._attack(2)
        assert [a["action"] for a in actions] == ["draw_cards"]

    def test_only_attacking_creatures_count(self):
        """A wide board that is not ATTACKING does not satisfy pack tactics —
        the condition is "you attacked with", not "you control"."""
        game = _make_game()
        rick, claude = game.players
        leader = _cached("werewolf pack leader")
        leader.attacking = True
        rick.battlefield.append(leader)
        for i in range(4):                       # 12 power, none attacking
            rick.battlefield.append(_make_card(
                f"Idle {i}", type_line="Creature — Bear",
                power="3", toughness="3"))
        ctx = build_game_context(game, rick, claude, card=leader,
                                 attacking_creature=leader)
        actions, _ = LIB.resolve_attack_trigger(
            leader.name, leader.oracle_text, leader.name,
            int(leader.power), rick.name, claude.name, game_context=ctx)
        assert actions[0]["action"] == "no_action"


class TestMycolothDevour:
    """Devour 2 (CR 702.81) did not exist, so Mycoloth entered with ZERO
    counters — and his whole payoff is "create a Saproling for EACH +1/+1
    counter", which a generic token pattern resolved as a flat ONE forever.
    He was very nearly a dead card in the deck built around him."""

    def _etb(self, tokens=0, nontokens=0):
        game = _make_game()
        rick, claude = game.players
        myco = _cached("mycoloth")
        rick.battlefield.append(myco)
        for i in range(tokens):
            tok = _make_card(f"Saproling {i}", type_line="Creature — Saproling",
                             power="1", toughness="1")
            tok.is_token = True
            rick.battlefield.append(tok)
        for i in range(nontokens):
            rick.battlefield.append(_make_card(
                f"Real Card {i}", type_line="Creature — Bear",
                power="3", toughness="3"))
        ctx = build_game_context(game, rick, claude, card=myco)
        actions, _desc = LIB.resolve_etb(
            myco.name, myco.oracle_text, rick.name, claude.name,
            game_context=ctx, event_type="etb")
        return actions, myco

    def test_devours_tokens_for_twice_that_many_counters(self):
        actions, _myco = self._etb(tokens=3)
        sacs = [a for a in actions if a["action"] == "sacrifice_permanent"]
        counters = [a for a in actions if a["action"] == "add_counters"]
        assert len(sacs) == 3, "three tokens devoured"
        assert counters and counters[0]["amount"] == 6, "devour 2 = twice that many"

    def test_never_eats_real_cards(self):
        """v1 choice, and the fixture makes it decisive: three real creatures
        and no tokens must produce no sacrifices at all."""
        actions, _ = self._etb(tokens=0, nontokens=3)
        assert actions[0]["action"] == "no_action"
        assert not [a for a in actions if a["action"] == "sacrifice_permanent"]

    def test_upkeep_scales_saprolings_off_the_counters(self):
        game = _make_game()
        rick, claude = game.players
        myco = _cached("mycoloth")
        myco.counters["+1/+1"] = 6
        rick.battlefield.append(myco)
        ctx = build_game_context(game, rick, claude, card=myco)
        actions, _desc = LIB.resolve_etb(
            "mycoloth upkeep", myco.oracle_text, rick.name, claude.name,
            game_context=ctx, event_type="upkeep")
        tokens = [a for a in actions if a["action"] == "create_token"]
        assert tokens and tokens[0]["count"] == 6, (
            "one Saproling per +1/+1 counter, not a flat one")

    def test_no_counters_means_no_saprolings(self):
        game = _make_game()
        rick, claude = game.players
        myco = _cached("mycoloth")
        rick.battlefield.append(myco)
        ctx = build_game_context(game, rick, claude, card=myco)
        actions, _ = LIB.resolve_etb(
            "mycoloth upkeep", myco.oracle_text, rick.name, claude.name,
            game_context=ctx, event_type="upkeep")
        assert actions[0]["action"] == "no_action"


# ---------------------------------------------------------------------------
# Aug 3, 2026 (later the same day) — the rest of the backlog, assessed against
# ground truth rather than the probe. Four entries were struck outright and
# one turned out to be a seam:
#
#   devoid              ALREADY CORRECT — Scryfall reports colors=[] for From
#                       Beyond and Kozilek's Return, so the loader gets it for
#                       free. The "broken" reading came from a probe that
#                       hardcoded the colour instead of reading the loader.
#   addendum            ALREADY CORRECT — pinned below.
#   backup              ALREADY CORRECT — its reminder text is matched by the
#                       existing "ETB +1/+1 Counters" pattern; it only looked
#                       unhandled because the probe used the creature-enters
#                       WATCHER dispatch, which a self-ETB never reaches.
#   choose-a-background NOT A GAME MECHANIC — a deckbuilding rule like partner;
#                       Karlach's actual attack trigger is already implemented.
#   partner-with        INERT — Brallin's partner Shabraz is in no deck and not
#                       in the cache, so the tutor half has nothing to find.
#
# What was real: bolster, and — much bigger than the tail it hid behind —
# "whenever you discard".
# ---------------------------------------------------------------------------
from mtg.constants import Phase                                  # noqa: E402
from mtg.engine import GameEngine                                # noqa: E402
from mtg.triggers import fire_discard_triggers                   # noqa: E402

ANAFENZA = ("Whenever another nontoken creature you control enters, bolster 1. "
            "(Choose a creature with the least toughness among creatures you "
            "control and put a +1/+1 counter on it.)")
BRALLIN = ("Partner with Shabraz, the Skyshark (When this creature enters, "
           "target player may put Shabraz into their hand from their library, "
           "then shuffle.)\n"
           "Whenever you discard a card, put a +1/+1 counter on Brallin and it "
           "deals 1 damage to each opponent.\n"
           "{R}: Target Shark gains trample until end of turn.")
GLINT_HORN = ("Haste\n"
              "Whenever you discard a card, this creature deals 1 damage to "
              "each opponent.\n"
              "{1}{R}, Discard a card: Draw a card. Activate only if this "
              "creature is attacking.")
BONE_MISER = ("Whenever you discard a creature card, create a 2/2 black Zombie "
              "creature token.\n"
              "Whenever you discard a land card, add {B}{B}.\n"
              "Whenever you discard a noncreature, nonland card, draw a card.")
FORMATION = ("Creatures you control gain indestructible until end of turn.\n"
             "Addendum — If you cast this spell during your main phase, put a "
             "+1/+1 counter on each of those creatures and they gain vigilance "
             "until end of turn.")


def _engine(game):
    engine = GameEngine(None)
    game._rules_engine = engine.rules
    engine.rules.engine_ref = engine
    return engine


class TestBolster:
    """CR 701.28 — bolster N puts N +1/+1 counters on the creature with the
    LEAST TOUGHNESS among creatures you control."""

    def _board(self):
        game = _make_game()
        rick, _ = game.players
        ana = _make_card("Anafenza, Kin-Tree Spirit",
                         type_line="Legendary Creature — Spirit Soldier",
                         oracle_text=ANAFENZA, power="2", toughness="2")
        weakling = _make_card("Weakling", type_line="Creature — Rat",
                              power="1", toughness="1")
        rick.battlefield.extend([ana, weakling])
        return game, rick, ana, weakling

    def test_the_trigger_is_detected_at_all(self):
        """The creature-enters DETECTION gate enumerated three literal
        phrasings and had no entry for "whenever another NONTOKEN creature you
        control enters", so Anafenza was never collected and bolster fired
        zero times."""
        game, rick, ana, weakling = self._board()
        engine = _engine(game)
        newcomer = _make_card("Newcomer", type_line="Creature — Bear",
                              power="3", toughness="3")
        rick.battlefield.append(newcomer)
        messages, _ = engine._check_creature_etb_triggers_sync(game, rick, newcomer)
        assert messages, "the watcher must fire at all"

    def test_the_counter_goes_on_the_least_toughness_creature(self):
        """The generic counter pattern matched bolster's REMINDER text ("put a
        +1/+1 counter on it") and read "it" as the SOURCE, so Anafenza grew
        herself every trigger.

        Decisive: Anafenza is 2/2 and Weakling 1/1, so source-targeting and
        least-toughness-targeting name different creatures."""
        game, rick, ana, weakling = self._board()
        engine = _engine(game)
        newcomer = _make_card("Newcomer", type_line="Creature — Bear",
                              power="3", toughness="3")
        rick.battlefield.append(newcomer)
        engine._check_creature_etb_triggers_sync(game, rick, newcomer)
        assert weakling.counters.get('+1/+1') == 1, "least toughness wins"
        assert not ana.counters, "not the source"
        assert not newcomer.counters, "not the creature that entered"


class TestDiscardTriggers:
    """Eight cards across three decks carry "whenever you discard" and only
    Anje Falkenrath was handled. Brallin is a legendary whose entire engine is
    discard payoff and it did nothing — the commander-defining-ability family
    (Baral, Tymna, Thrasios, Anje) one more time."""

    def test_brallin_gets_its_counter_and_pings_each_opponent(self):
        game = _make_game()
        rick, claude = game.players
        brallin = _make_card("Brallin, Skyshark Rider",
                             type_line="Legendary Creature — Human Shaman",
                             oracle_text=BRALLIN, power="3", toughness="3")
        rick.battlefield.append(brallin)
        rick.hand.append(_make_card("Junk", type_line="Creature — Bear"))
        engine = _engine(game)
        start = claude.life
        engine.rules._execute_action_on_state(game, {
            "action": "discard", "player": "Rick", "card": "Junk"})
        assert brallin.counters.get('+1/+1') == 1
        assert claude.life == start - 1

    def test_it_fires_through_the_real_discard_action(self):
        """Hooked at the shared discard choke point, so every discard site
        gets it — the action handler, cycling costs, hand-size discards,
        loots — rather than one caller."""
        game = _make_game()
        rick, claude = game.players
        rick.battlefield.append(_make_card(
            "Glint-Horn Buccaneer", type_line="Creature — Minotaur Pirate",
            oracle_text=GLINT_HORN, power="5", toughness="5"))
        rick.hand.append(_make_card("Junk", type_line="Instant"))
        engine = _engine(game)
        start = claude.life
        engine.rules._execute_action_on_state(game, {
            "action": "discard", "player": "Rick", "card": "Junk"})
        assert claude.life == start - 1

    def test_type_filters_select_the_right_clause(self):
        """Bone Miser has three clauses keyed on the discarded card's type."""
        for type_line, expect in (("Creature — Bear", "token"),
                                  ("Basic Land — Swamp", "mana"),
                                  ("Instant", "draw")):
            game = _make_game()
            rick, _ = game.players
            rick.battlefield.append(_make_card(
                "Bone Miser", type_line="Creature — Zombie Wizard",
                oracle_text=BONE_MISER, power="3", toughness="3"))
            rick.library.append(_make_card("Top", type_line="Instant"))
            _engine(game)
            before_hand, before_bf = len(rick.hand), len(rick.battlefield)
            fire_discard_triggers(game, rick, _make_card("Pitched",
                                                         type_line=type_line))
            if expect == "token":
                assert len(rick.battlefield) == before_bf + 1, "a Zombie token"
            elif expect == "mana":
                assert rick.mana_pool.get('B') == 2, rick.mana_pool
                assert len(rick.battlefield) == before_bf
            else:
                assert len(rick.hand) == before_hand + 1, "drew a card"
                assert len(rick.battlefield) == before_bf

    def test_only_the_discarding_players_own_watchers_fire(self):
        """Every printed form says "whenever YOU discard".

        BOTH life totals are asserted, and that is what makes it decisive: a
        scan over every battlefield would fire the opponent's Glint-Horn on
        Rick's discard, and because "each opponent" is resolved relative to
        the DISCARDER the damage would land on Claude — leaving Rick's life
        untouched and an assertion on Rick alone perfectly happy."""
        game = _make_game()
        rick, claude = game.players
        claude.battlefield.append(_make_card(
            "Glint-Horn Buccaneer", type_line="Creature — Minotaur Pirate",
            oracle_text=GLINT_HORN, power="5", toughness="5"))
        engine = _engine(game)
        rick_start, claude_start = rick.life, claude.life
        rick.hand.append(_make_card("Junk", type_line="Instant"))
        engine.rules._execute_action_on_state(game, {
            "action": "discard", "player": "Rick", "card": "Junk"})
        assert (rick.life, claude.life) == (rick_start, claude_start), (
            "Rick discarding must not fire the OPPONENT's discard watcher")

    def test_the_filter_negation_is_checked_first(self):
        """'creature' is a substring of 'noncreature' — the trap that has now
        bitten this codebase six times. Unit-level, so it is pinned
        independently of any one card."""
        from mtg.triggers import _discard_filter_matches as m
        bear = _make_card("Bear", type_line="Creature — Bear")
        land = _make_card("Swamp", type_line="Basic Land — Swamp")
        bolt = _make_card("Bolt", type_line="Instant")
        assert m("", bear) and m("", land) and m("", bolt)
        assert m("creature ", bear) and not m("creature ", bolt)
        assert m("land ", land) and not m("land ", bear)
        assert m("noncreature, nonland ", bolt)
        assert not m("noncreature, nonland ", bear)
        assert not m("noncreature, nonland ", land)


class TestStruckFromTheBacklog:
    """Pins for the entries assessment struck, so a later change cannot
    quietly break something that was fine and have it re-filed as missing."""

    def test_addendum_checks_the_phase(self):
        """The condition is the PHASE — the axis the earlier probe failed to
        vary, which is the whole reason this was filed "unassessed"."""
        import asyncio
        seen = {}
        for phase in (Phase.MAIN1, Phase.COMBAT_DAMAGE):
            game = _make_game()
            rick, _ = game.players
            game.phase = phase
            bear = _make_card("Bear", type_line="Creature — Bear",
                              power="2", toughness="2")
            rick.battlefield.append(bear)
            for _ in range(3):
                rick.battlefield.append(_make_card(
                    "Plains", type_line="Basic Land — Plains",
                    oracle_text="{T}: Add {W}."))
            form = _make_card("Unbreakable Formation", type_line="Instant",
                              oracle_text=FORMATION, mana_cost="{2}{W}")
            form.cmc = 3
            rick.hand.append(form)
            engine = _engine(game)
            asyncio.new_event_loop().run_until_complete(
                engine.cast_spell_async(game, rick, form))
            seen[phase] = bear.counters.get('+1/+1', 0)
        assert seen[Phase.MAIN1] == 1, "addendum applies in a main phase"
        assert seen[Phase.COMBAT_DAMAGE] == 0, "and not outside one"

    def test_devoid_cards_are_colorless_as_loaded(self):
        """Scryfall already reports colors=[] for a devoid card while keeping
        the mana-cost colour in color_identity, so the loader needs no help."""
        for name, identity in (("from beyond", "G"), ("kozilek's return", "R")):
            entry = CACHE.get(name)
            assert entry, name
            assert "devoid" in (entry["oracle_text"] or "").lower()
            assert entry["colors"] == [], f"{name} is colorless (CR 702.114a)"
            assert identity in entry["color_identity"], "identity is unchanged"

    def test_backup_puts_a_counter_on_the_source(self):
        """Backup's reminder text is already matched by the "ETB +1/+1
        Counters" pattern, and the source is a legal target for it, so a
        dedicated pattern would be unreachable dead code."""
        valkyrie = ("Backup 1 (When this creature enters, put a +1/+1 counter "
                    "on target creature. If that's another creature, it gains "
                    "the following abilities until end of turn.)\n"
                    "Flying, first strike, lifelink")
        actions, _desc = LIB.resolve_etb(
            card_name="Boon-Bringer Valkyrie", oracle_text=valkyrie,
            controller="Rick", opponent="Claude", game_context={})
        assert actions, "backup's ETB must produce an action"
        assert any(a.get("action") == "add_counters"
                   and a.get("counter_type") == "+1/+1" for a in actions), actions


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
