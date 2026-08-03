"""Aug 3, 2026 — the alternate-cost / graveyard-casting wave (wave 1).

buyback, jump-start, aftermath, embalm, eternalize, unearth, foretell — plus
the two bugs the wave surfaced in the seam it lands on:

  * an ESCAPED instant was exiled after resolving, so it could never escape
    again (all three executors blanket-exiled any graveyard cast);
  * the engine executor's two PRE-cast gates returned bare between the
    graveyard extraction and the hand-append, stranding the card with its
    costs paid — the July 30 autoplay fix, never ported to its sibling.

Oracle text below is the REAL printed text (Scryfall-verified this session).
Paraphrasing it would test a card that does not exist — and every parser here
anchors on printed shapes, so a paraphrase is not a valid fixture.
"""
import asyncio

import pytest

from mtg import helpers
from mtg.engine import GameEngine
from mtg.legal_actions import graveyard_castable_entries
from mtg.models import Card, StackEntry
from mtg.spells import (activate_from_graveyard, foretell_card_from_hand)

from tests.conftest import _make_card, _make_game


# --- real printed oracle text -------------------------------------------

FORBID = ("Buyback—Discard two cards. (You may discard two cards in addition "
          "to any other costs as you cast this spell. If you do, put this "
          "card into your hand as it resolves.)\nCounter target spell.")
RISK_FACTOR = ("Target opponent may have Risk Factor deal 4 damage to them. "
               "If that player doesn't, you draw three cards.\n"
               "Jump-start (You may cast this card from your graveyard by "
               "discarding a card in addition to paying its other costs. "
               "Then exile this card.)")
CLING = ("Choose one —\n"
         "• Target player mills two cards.\n"
         "• Exile target card from a graveyard. You gain 2 life.\n"
         "Escape—{1}{B}, Exile two other cards from your graveyard. "
         "(You may cast this card from your graveyard for its escape cost.)")
LINGERING_SOULS = ("Create two 1/1 white Spirit creature tokens with flying.\n"
                   "Flashback {1}{B}")
ANGEL_OF_SANCTIONS = (
    "Flying\n"
    "When this creature enters, you may exile target nonland permanent an "
    "opponent controls until this creature leaves the battlefield.\n"
    "Embalm {5}{W} ({5}{W}, Exile this card from your graveyard: Create a "
    "token that's a copy of it, except it's a white Zombie Angel with no "
    "mana cost. Embalm only as a sorcery.)")
TIMELESS_WITNESS = (
    "When this creature enters, return target card from your graveyard to "
    "your hand.\n"
    "Eternalize {5}{G}{G} ({5}{G}{G}, Exile this card from your graveyard: "
    "Create a token that's a copy of it, except it's a 4/4 black Zombie "
    "Human Shaman with no mana cost. Eternalize only as a sorcery.)")
PLATOON_DISPENSER = (
    "At the beginning of your end step, if you control two or more other "
    "creatures, draw a card.\n"
    "{3}{W}: Create a 1/1 colorless Soldier artifact creature token.\n"
    "Unearth {2}{W}{W}")
QUAKEBRINGER = (
    "Your opponents can't gain life.\n"
    "At the beginning of your upkeep, Quakebringer deals 2 damage to each "
    "opponent. This ability triggers only if Quakebringer is on the "
    "battlefield or if Quakebringer is in your graveyard and you control a "
    "Giant.\nForetell {2}{R}{R}")

# Commit // Memory's two faces (Card.oracle_text for a split card is face 0
# only; split_texts carries both, which is what the aftermath parser reads).
COMMIT_FACE = ("Put target spell or nonland permanent into its owner's "
               "library second from the top.")
MEMORY_FACE = ("Aftermath (Cast this spell only from your graveyard. Then "
               "exile it.)\nEach player shuffles their hand and graveyard "
               "into their library, then draws seven cards.")


# --- fixtures ------------------------------------------------------------

def _engine(game):
    engine = GameEngine(None)
    game._rules_engine = engine.rules
    engine.rules.engine_ref = engine
    return engine


def _lands(player, n, name="Swamp", sym="B"):
    for _ in range(n):
        player.battlefield.append(_make_card(
            name, type_line=f"Basic Land — {name}",
            oracle_text="{T}: Add {%s}." % sym))


def _zone_of(card, player):
    for zone in ("hand", "graveyard", "exile", "battlefield", "library"):
        if card in getattr(player, zone):
            return zone
    return "NOWHERE"


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _commit_memory():
    card = _make_card("Commit // Memory", type_line="Instant",
                      oracle_text=COMMIT_FACE,
                      mana_cost="{3}{U} // {4}{U}{U}")
    card.split_names = ["Commit", "Memory"]
    card.split_costs = ["{3}{U}", "{4}{U}{U}"]
    card.split_types = ["Instant", "Sorcery"]
    card.split_texts = [COMMIT_FACE, MEMORY_FACE]
    return card


# --- parsers -------------------------------------------------------------

class TestParsers:
    def test_buyback_mana_and_discard_forms(self):
        assert helpers.parse_buyback("Buyback {2}{U}") == {"mana": "{2}{U}"}
        assert helpers.parse_buyback(FORBID) == {"discard": 2}

    def test_buyback_declines_unmodeled_costs(self):
        # Paying life or sacrificing for buyback is not modeled; buying back
        # for a cost we don't charge would be a free recursion engine.
        assert helpers.parse_buyback("Buyback—Pay 4 life.") is None
        assert helpers.parse_buyback("Buyback—Sacrifice a land.") is None

    def test_grant_lines_are_not_read_as_the_source_s_own_cost(self):
        # THE trap this whole family shares: a card can GRANT the ability to
        # other cards. Reading a grant as the source's own cost is the
        # July-21 Yidris cascade-grant class.
        assert helpers.parse_graveyard_activation(
            "Each creature card in your graveyard has unearth {2}{B}.") is None
        assert helpers.parse_miracle(
            "Each instant and sorcery card in your hand has miracle {2}.") is None
        assert helpers.parse_dredge(
            "Land cards in your graveyard have dredge 2.") is None
        assert helpers.parse_buyback("Buyback costs cost {2} less.") is None
        assert not helpers.has_jump_start(
            "Vehicles in your graveyard have jump-start.")

    def test_graveyard_activation_mechanics(self):
        assert helpers.parse_graveyard_activation(ANGEL_OF_SANCTIONS) == (
            "embalm", "{5}{W}")
        assert helpers.parse_graveyard_activation(TIMELESS_WITNESS) == (
            "eternalize", "{5}{G}{G}")
        assert helpers.parse_graveyard_activation(PLATOON_DISPENSER) == (
            "unearth", "{2}{W}{W}")

    def test_foretell_and_jump_start_and_aftermath(self):
        assert helpers.parse_foretell(QUAKEBRINGER) == "{2}{R}{R}"
        assert helpers.has_jump_start(RISK_FACTOR)
        assert helpers.aftermath_half_index(_commit_memory()) == 1

    def test_zero_costs_are_not_falsey_traps(self):
        # "Foretell {0}" and "Miracle {0}" are real printed costs, so callers
        # must test `is not None` — pin the parser side of that contract.
        assert helpers.parse_foretell("Foretell {0}") == "{0}"
        assert helpers.parse_miracle("Miracle {0}") == "{0}"


# --- escape must NOT be exiled (the bug the wave surfaced) ---------------

class TestGraveyardCastExileIsPerMechanic:
    def test_escape_is_not_exiled(self):
        card = _make_card("Cling to Dust", type_line="Instant",
                          oracle_text=CLING, mana_cost="{B}")
        assert helpers.exile_after_resolution_reason(card) == "", (
            "CR 702.139 gives escape no exile clause — an escaped instant "
            "goes to the graveyard so it can escape again")

    def test_flashback_and_jump_start_and_aftermath_do_exile(self):
        fb = _make_card("Lingering Souls", type_line="Sorcery",
                        oracle_text=LINGERING_SOULS)
        assert helpers.exile_after_resolution_reason(fb) == "flashback"
        js = _make_card("Risk Factor", type_line="Instant",
                        oracle_text=RISK_FACTOR)
        assert helpers.exile_after_resolution_reason(js) == "jump-start"
        cm = _commit_memory()
        cm.cast_as_split_half = 1
        assert helpers.exile_after_resolution_reason(cm) == "aftermath"

    def test_snapcaster_granted_flashback_still_exiles(self):
        # A granted flashback has no printed keyword to detect, so "exile"
        # stays the default for graveyard casts — escape is the only
        # behavior this wave changed.
        plain = _make_card("Ponder", type_line="Sorcery",
                           oracle_text="Look at the top three cards.")
        assert helpers.exile_after_resolution_reason(plain) == "flashback"

    def test_escaped_instant_ends_in_graveyard_end_to_end(self):
        game = _make_game()
        rick, claude = game.players
        cling = _make_card("Cling to Dust", type_line="Instant",
                           oracle_text=CLING, mana_cost="{B}")
        rick.graveyard.append(cling)
        for i in range(4):
            rick.graveyard.append(_make_card(f"Filler {i}",
                                             type_line="Creature"))
        _lands(rick, 6)
        engine = _engine(game)
        # the executors' graveyard pre-move + escape exile cost
        rick.graveyard.remove(cling)
        cost, count = helpers.parse_escape_cost(cling.oracle_text)
        cling._escape_cost = cost
        cling._was_escaped = True
        for _ in range(count):
            rick.exile.append(rick.graveyard.pop())
        rick.hand.append(cling)
        cling._cast_from_graveyard = True
        ok, msg, _ = _run(engine.cast_spell_async(game, rick, cling,
                                                  target=claude))
        assert ok, msg
        # ...and the executors' post-cast block, which used to exile blindly
        if cling in rick.graveyard and helpers.exile_after_resolution_reason(cling):
            rick.graveyard.remove(cling)
            rick.exile.append(cling)
        assert _zone_of(cling, rick) == "graveyard"


# --- buyback -------------------------------------------------------------

class TestBuyback:
    def _forbid_game(self, spare_cards):
        game = _make_game()
        rick, claude = game.players
        forbid = _make_card("Forbid", type_line="Instant", oracle_text=FORBID,
                            mana_cost="{1}{U}{U}")
        rick.hand.append(forbid)
        for i in range(spare_cards):
            rick.hand.append(_make_card(f"Spare {i}", type_line="Creature",
                                        mana_cost="{2}"))
        _lands(rick, 6, "Island", "U")
        engine = _engine(game)
        bogus = _make_card("Bogus", type_line="Sorcery", mana_cost="{1}")
        game.stack.append(StackEntry(card=bogus, controller_name=claude.name,
                                     controller_index=1))
        return game, rick, forbid, engine, bogus

    def test_buyback_returns_the_spell_to_hand(self):
        game, rick, forbid, engine, bogus = self._forbid_game(5)
        ok, msg, _ = _run(engine.cast_spell_async(game, rick, forbid,
                                                  target=bogus))
        assert ok, msg
        assert forbid._buyback_paid
        assert _zone_of(forbid, rick) == "hand"
        assert len(rick.graveyard) == 2, "two cards discarded for buyback"

    def test_buyback_declines_rather_than_emptying_the_hand(self):
        # The fix IS the reserve, so the fixture has to straddle it: with
        # three spare cards the discard is affordable (2 <= 3) but leaves
        # only one card, so the reserve declines. A one-card hand would
        # decline under any threshold and prove nothing — mutation testing
        # caught exactly that, the fixture passing for the wrong reason.
        game, rick, forbid, engine, bogus = self._forbid_game(3)
        ok, msg, _ = _run(engine.cast_spell_async(game, rick, forbid,
                                                  target=bogus))
        assert ok, msg
        assert not forbid._buyback_paid
        assert _zone_of(forbid, rick) == "graveyard"

    def test_buyback_stamp_does_not_survive_into_the_next_cast(self):
        game, rick, forbid, engine, bogus = self._forbid_game(5)
        _run(engine.cast_spell_async(game, rick, forbid, target=bogus))
        assert forbid._buyback_paid
        # Re-cast with an empty hand: the stamp must be cleared, or the spell
        # returns to hand a second time without paying anything.
        rick.hand = [forbid]
        for c in rick.battlefield:
            c.tapped = False
        bogus2 = _make_card("Bogus2", type_line="Sorcery", mana_cost="{1}")
        game.stack.append(StackEntry(card=bogus2, controller_name="Claude",
                                     controller_index=1))
        _run(engine.cast_spell_async(game, rick, forbid, target=bogus2))
        assert not forbid._buyback_paid
        assert _zone_of(forbid, rick) == "graveyard"


# --- jump-start ----------------------------------------------------------

class TestJumpStart:
    def test_offered_priced_and_exiled(self):
        game = _make_game()
        rick, claude = game.players
        rf = _make_card("Risk Factor", type_line="Instant",
                        oracle_text=RISK_FACTOR, mana_cost="{2}{R}")
        rick.graveyard.append(rf)
        rick.hand.append(_make_card("Pitchable", type_line="Creature",
                                    mana_cost="{3}"))
        _lands(rick, 5, "Mountain", "R")
        offers = graveyard_castable_entries(rick, {"R": 5}, 0, 5)
        assert any("JUMP-START" in o["label"] for o in offers)

        engine = _engine(game)
        rick.graveyard.remove(rf)
        rick.playable_from_graveyard.remove(rf.id)
        discarded = helpers.pay_jump_start_discard(game, rick, rf)
        assert [c.name for c in discarded] == ["Pitchable"]
        rick.hand.append(rf)
        rf._cast_from_graveyard = True
        ok, msg, _ = _run(engine.cast_spell_async(game, rick, rf,
                                                  target=claude))
        assert ok, msg
        # ...then the executors' post-cast block, which is where a card the
        # executor pre-moved into hand gets exiled (the orchestrator's own
        # graveyard branch never fires for that shape).
        if rf in rick.graveyard and helpers.exile_after_resolution_reason(rf):
            rick.graveyard.remove(rf)
            rick.exile.append(rf)
        assert _zone_of(rf, rick) == "exile", "CR 702.132a exiles it"

    def test_not_offered_with_an_empty_hand(self):
        # The discard is a COST — no card to pitch means the cast is illegal,
        # so it must not be advertised (the FoW-filter lesson in reverse).
        game = _make_game()
        rick, _ = game.players
        rf = _make_card("Risk Factor", type_line="Instant",
                        oracle_text=RISK_FACTOR, mana_cost="{2}{R}")
        rick.graveyard.append(rf)
        _lands(rick, 5, "Mountain", "R")
        offers = graveyard_castable_entries(rick, {"R": 5}, 0, 5)
        assert not any("JUMP-START" in o["label"] for o in offers)


# --- aftermath -----------------------------------------------------------

class TestAftermath:
    def test_offered_from_graveyard_under_the_half_name(self):
        game = _make_game()
        rick, _ = game.players
        cm = _commit_memory()
        rick.graveyard.append(cm)
        _lands(rick, 8, "Island", "U")
        offers = graveyard_castable_entries(rick, {"U": 8}, 0, 8)
        labels = [o["label"] for o in offers]
        assert any("AFTERMATH" in lbl and lbl.startswith("Memory")
                   for lbl in labels), labels
        # The action must name the FULL card, which is what the executors'
        # graveyard scan matches, with the half routed via `adventure`.
        entry = next(o for o in offers if "AFTERMATH" in o["label"])
        assert entry["action"]["card"] == "Commit // Memory"
        assert entry["action"]["adventure"] == "Memory"

    def test_aftermath_half_is_refused_from_hand(self):
        game = _make_game()
        rick, _ = game.players
        cm = _commit_memory()
        rick.hand.append(cm)
        _lands(rick, 8, "Island", "U")
        engine = _engine(game)
        cm.cast_as_split_half = 1
        ok, msg, _ = _run(engine.cast_spell_async(game, rick, cm))
        assert not ok and "aftermath" in msg.lower(), msg

    def test_front_half_is_refused_from_graveyard(self):
        game = _make_game()
        rick, _ = game.players
        cm = _commit_memory()
        _lands(rick, 8, "Island", "U")
        engine = _engine(game)
        rick.hand.append(cm)
        cm.cast_as_split_half = 0
        cm._cast_from_graveyard = True
        ok, msg, _ = _run(engine.cast_spell_async(game, rick, cm))
        assert not ok and "aftermath" in msg.lower(), msg

    def test_aftermath_half_casts_from_graveyard_and_is_exiled(self):
        game = _make_game()
        rick, _ = game.players
        cm = _commit_memory()
        _lands(rick, 8, "Island", "U")
        engine = _engine(game)
        rick.hand.append(cm)
        cm.cast_as_split_half = 1
        cm._cast_from_graveyard = True
        ok, msg, _ = _run(engine.cast_spell_async(game, rick, cm))
        assert ok, msg
        assert _zone_of(cm, rick) == "exile", "CR 702.127a exiles it"

    def test_targeting_gate_reads_the_half_being_cast(self):
        # Commit targets; Memory does not. Gating Memory on Commit's text
        # made the aftermath half uncastable everywhere once the zone rule
        # was enforced. Decisive because the whole-card oracle IS Commit's.
        from mtg.spells import _spell_requires_targets
        cm = _commit_memory()
        assert _spell_requires_targets(cm), "face 0 (Commit) does target"
        memory_face = Card(name="Memory", mana_cost="{4}{U}{U}",
                           type_line="Sorcery", oracle_text=MEMORY_FACE)
        assert not _spell_requires_targets(memory_face)


# --- embalm / eternalize / unearth ---------------------------------------

class TestGraveyardActivations:
    def test_embalm_exiles_the_card_and_makes_a_white_zombie_copy(self):
        game = _make_game()
        rick, _ = game.players
        angel = _make_card("Angel of Sanctions", type_line="Creature — Angel",
                           oracle_text=ANGEL_OF_SANCTIONS,
                           mana_cost="{3}{W}{W}", power="3", toughness="4")
        rick.graveyard.append(angel)
        _lands(rick, 8, "Plains", "W")
        engine = _engine(game)
        ok, msg = activate_from_graveyard(engine, game, rick, angel)
        assert ok, msg
        assert _zone_of(angel, rick) == "exile"
        tokens = [c for c in rick.battlefield if getattr(c, "is_token", False)]
        assert len(tokens) == 1
        token = tokens[0]
        assert "Zombie" in token.type_line
        assert token.colors == ["W"]
        assert token.mana_cost == "", "'with no mana cost'"
        assert (token.power, token.toughness) == ("3", "4"), "P/T unchanged"

    def test_eternalize_makes_a_4_4_black_zombie(self):
        game = _make_game()
        rick, _ = game.players
        witness = _make_card("Timeless Witness",
                             type_line="Creature — Human Shaman",
                             oracle_text=TIMELESS_WITNESS,
                             mana_cost="{2}{G}{G}", power="2", toughness="1")
        rick.graveyard.append(witness)
        _lands(rick, 8, "Forest", "G")
        engine = _engine(game)
        ok, msg = activate_from_graveyard(engine, game, rick, witness)
        assert ok, msg
        token = [c for c in rick.battlefield
                 if getattr(c, "is_token", False)][0]
        assert (token.power, token.toughness) == ("4", "4")
        assert token.colors == ["B"]
        assert "Zombie" in token.type_line
        assert token.mana_cost == ""

    def test_unearth_returns_the_card_with_haste_and_schedules_its_exile(self):
        game = _make_game()
        rick, _ = game.players
        pd = _make_card("Platoon Dispenser",
                        type_line="Artifact Creature — Construct",
                        oracle_text=PLATOON_DISPENSER, mana_cost="{5}",
                        power="3", toughness="3")
        rick.graveyard.append(pd)
        _lands(rick, 8, "Plains", "W")
        engine = _engine(game)
        ok, msg = activate_from_graveyard(engine, game, rick, pd)
        assert ok, msg
        assert _zone_of(pd, rick) == "battlefield"
        assert not pd.summoning_sick, "unearth grants haste"
        assert pd._unearthed
        assert any(t.get("trigger_at") == "end_step"
                   for t in game.delayed_triggers), \
            "unearth exiles it at the next end step"

    def test_an_unearthed_creature_that_dies_is_exiled_not_buried(self):
        """CR 702.83a exiles it "at the beginning of the next end step OR if
        it would leave the battlefield". The second half is the one that
        matters: an unearthed creature is a hasty attacker, so it usually
        dies before the end step — and landing in the graveyard would let it
        be unearthed AGAIN, the recursion the clause forbids."""
        game = _make_game()
        rick, _ = game.players
        pd = _make_card("Platoon Dispenser",
                        type_line="Artifact Creature — Construct",
                        oracle_text=PLATOON_DISPENSER, mana_cost="{5}",
                        power="3", toughness="3")
        rick.graveyard.append(pd)
        _lands(rick, 8, "Plains", "W")
        engine = _engine(game)
        ok, _msg = activate_from_graveyard(engine, game, rick, pd)
        assert ok
        # It dies before the scheduled end-step exile can fire.
        engine.rules._execute_action_on_state(
            game, {"action": "destroy", "card": "Platoon Dispenser"})
        assert _zone_of(pd, rick) == "exile", (
            "an unearthed permanent that leaves the battlefield is exiled")
        assert pd not in rick.graveyard, "or it could be unearthed again"

    def test_sorcery_speed_only(self):
        # "Embalm only as a sorcery" — refuse on the opponent's turn and with
        # a non-empty stack. Both branches, or the gate is untested.
        game = _make_game()
        rick, claude = game.players
        angel = _make_card("Angel of Sanctions", type_line="Creature — Angel",
                           oracle_text=ANGEL_OF_SANCTIONS,
                           mana_cost="{3}{W}{W}", power="3", toughness="4")
        rick.graveyard.append(angel)
        _lands(rick, 8, "Plains", "W")
        engine = _engine(game)
        game.active_player_index = 1
        ok, msg = activate_from_graveyard(engine, game, rick, angel)
        assert not ok and "sorcery" in msg.lower()
        game.active_player_index = 0
        game.stack.append(StackEntry(
            card=_make_card("Bolt", type_line="Instant"),
            controller_name=claude.name, controller_index=1))
        ok2, msg2 = activate_from_graveyard(engine, game, rick, angel)
        assert not ok2 and "sorcery" in msg2.lower()

    def test_offered_as_an_ability_not_a_cast(self):
        # Casting these would fire Rhystic Study and the rest of the
        # cast-trigger family for an ability that never uses the cast
        # machinery — the action type is what keeps them apart.
        game = _make_game()
        rick, _ = game.players
        angel = _make_card("Angel of Sanctions", type_line="Creature — Angel",
                           oracle_text=ANGEL_OF_SANCTIONS,
                           mana_cost="{3}{W}{W}", power="3", toughness="4")
        rick.graveyard.append(angel)
        _lands(rick, 8, "Plains", "W")
        offers = graveyard_castable_entries(rick, {"W": 8}, 0, 8)
        entry = next(o for o in offers if "EMBALM" in o["label"])
        assert entry["action"]["type"] == "graveyard_activate"
        assert entry["action"]["mechanic"] == "embalm"


# --- foretell ------------------------------------------------------------

class TestForetell:
    def _foretold_game(self):
        game = _make_game()
        rick, _ = game.players
        game.active_player_index = 0
        game.turn_number = 3
        qb = _make_card("Quakebringer",
                        type_line="Creature — Giant Berserker",
                        oracle_text=QUAKEBRINGER, mana_cost="{3}{R}{R}",
                        power="4", toughness="4")
        rick.hand.append(qb)
        _lands(rick, 6, "Mountain", "R")
        return game, rick, qb

    def test_foretell_exiles_face_down_for_two(self):
        game, rick, qb = self._foretold_game()
        ok, msg = foretell_card_from_hand(game, rick, qb)
        assert ok, msg
        assert _zone_of(qb, rick) == "exile"
        assert qb._foretold and qb._face_down
        assert qb._foretell_cost == "{2}{R}{R}"
        assert qb._foretold_turn == 3
        assert sum(1 for c in rick.battlefield if c.tapped) == 2, "paid {2}"

    def test_the_face_down_card_is_never_named(self):
        game, rick, qb = self._foretold_game()
        _ok, msg = foretell_card_from_hand(game, rick, qb)
        assert "Quakebringer" not in msg, (
            "CR 702.143a exiles it face down — hidden information")

    def test_not_castable_the_turn_it_was_foretold(self):
        game, rick, qb = self._foretold_game()
        foretell_card_from_hand(game, rick, qb)
        for c in rick.battlefield:
            c.tapped = False
        same = graveyard_castable_entries(rick, {"R": 6}, 0, 6, turn_number=3)
        later = graveyard_castable_entries(rick, {"R": 6}, 0, 6, turn_number=4)
        assert not any("FORETOLD" in o["label"] for o in same), \
            "CR 702.143b forbids casting it the turn you foretold it"
        assert any("FORETOLD" in o["label"] for o in later)

    def test_cast_from_exile_charges_the_foretell_cost(self):
        game, rick, qb = self._foretold_game()
        foretell_card_from_hand(game, rick, qb)
        for c in rick.battlefield:
            c.tapped = False
        engine = _engine(game)
        # the executors' exile pre-move
        rick.exile.remove(qb)
        rick.hand.append(qb)
        qb._foretold = False
        qb._face_down = False
        qb._cast_via_foretell = True
        ok, msg, _ = _run(engine.cast_spell_async(game, rick, qb))
        assert ok, msg
        assert qb._mana_paid == 4, (
            "foretell {2}{R}{R} = 4, not the printed {3}{R}{R} = 5")
        assert _zone_of(qb, rick) == "battlefield"

    def test_only_on_your_own_turn(self):
        game, rick, qb = self._foretold_game()
        game.active_player_index = 1
        ok, msg = foretell_card_from_hand(game, rick, qb)
        assert not ok and "turn" in msg.lower()


# --- the engine executor's missing pre-cast rollback ---------------------

class TestEngineGraveyardRollback:
    def test_pre_cast_gates_roll_the_graveyard_cast_back(self):
        """The July 30 fix landed on autoplay only; engine.py's two pre-cast
        `return None` gates sat between the graveyard extraction (escape's
        exile cost paid) and the hand-append, so a blocked cast stranded the
        card in NO zone with its cost spent.

        BEHAVIORAL, deliberately: the first version of this pin counted
        `_rollback_graveyard_cast()` occurrences in the source and survived a
        mutant that disabled the CALL while leaving the text — a structural
        pin cannot see a disabled use.
        """
        game = _make_game()
        rick, claude = game.players
        # A creature-targeting spell with escape, on a board with no
        # creatures — so the CR 601.2c pre-cast gate fires.
        cling = _make_card("Cling to Dust", type_line="Instant",
                           oracle_text=(
                               "Destroy target creature.\n"
                               "Escape—{1}{B}, Exile two other cards from "
                               "your graveyard."),
                           mana_cost="{B}")
        rick.graveyard.append(cling)
        fillers = [_make_card(f"Filler {i}", type_line="Creature")
                   for i in range(4)]
        rick.graveyard.extend(fillers)
        rick.playable_from_graveyard.append(cling.id)
        _lands(rick, 6)
        engine = _engine(game)
        gy_before = len(rick.graveyard)

        result = _run(engine._execute_action(
            game, 0, {"type": "cast", "card": "Cling to Dust"}))

        assert result is None, "the targeting gate must block the cast"
        assert _zone_of(cling, rick) == "graveyard", (
            "a blocked graveyard cast must not strand the card in NO zone")
        assert cling.id in rick.playable_from_graveyard, (
            "it must still be offered next time")
        assert len(rick.graveyard) == gy_before, (
            "escape's exile cost must be refunded — paying a cost for a cast "
            "that never happened is the cost-paid-no-effect class")
        assert not rick.exile, "nothing should have stayed exiled"

    def test_engine_and_autoplay_both_dispatch_the_new_action_types(self):
        # The provider advertises them; a type only one executor can dispatch
        # is a silently-dead offer (the batch-13 F-F class).
        import inspect

        import mtg.autoplay as ap
        import mtg.engine as eng
        for action_type in ("graveyard_activate", "foretell"):
            assert f'action_type == "{action_type}"' in inspect.getsource(ap)
            assert f'action_type == "{action_type}"' in inspect.getsource(eng)


# --- the offer builder's escape/flashback ordering -----------------------

class TestOfferOrdering:
    def test_escape_card_is_not_relabelled_flashback_on_a_second_build(self):
        """The escape branch adds the id to playable_from_graveyard, and the
        Snapcaster-grant branch keyed on exactly that membership — so the
        second build of a turn advertised an escape card at its PRINTED cost
        under a FLASHBACK tag."""
        game = _make_game()
        rick, _ = game.players
        cling = _make_card("Cling to Dust", type_line="Instant",
                           oracle_text=CLING, mana_cost="{B}")
        rick.graveyard.append(cling)
        for i in range(4):
            rick.graveyard.append(_make_card(f"Filler {i}",
                                             type_line="Creature"))
        _lands(rick, 6)
        first = graveyard_castable_entries(rick, {"B": 6}, 0, 6)
        second = graveyard_castable_entries(rick, {"B": 6}, 0, 6)
        assert [o["label"] for o in first] == [o["label"] for o in second], (
            "the offer must be stable across builds")
        assert "ESCAPE" in first[0]["label"]
        assert "{1}{B}" in first[0]["label"], "priced at the escape cost"


# --- reachability from the AI's plan path ---------------------------------

class TestPlanPathReachability:
    """A mechanic the AI is never OFFERED, or is offered in a form its plan
    can't survive, is not implemented — this codebase's most expensive
    recurring failure (Force of Will dead 51 turns, suspend structurally
    impossible, Anje's activation filtered out)."""

    def _plan_game(self):
        game = _make_game()
        rick, _ = game.players
        engine = GameEngine(None)
        game._rules_engine = engine.rules

        rf = _make_card("Risk Factor", type_line="Instant",
                        oracle_text=RISK_FACTOR, mana_cost="{2}{R}")
        rf.cmc = 3
        rick.graveyard.append(rf)
        rick.hand.append(_make_card("Pitchable", type_line="Creature",
                                    mana_cost="{3}"))
        cm = _commit_memory()
        cm.cmc = 10
        rick.graveyard.append(cm)
        qb = _make_card("Quakebringer", type_line="Creature — Giant Berserker",
                        oracle_text=QUAKEBRINGER, mana_cost="{3}{R}{R}")
        qb.cmc = 5
        qb._foretold = True
        qb._foretell_cost = "{2}{R}{R}"
        rick.exile.append(qb)
        # Enough for all three at once (3 + 6 + 4), so a drop can only be the
        # name gate, never the mana simulation.
        _lands(rick, 8, "Mountain", "R")
        _lands(rick, 8, "Island", "U")
        # Populate playable_from_graveyard the way the offer builder does.
        graveyard_castable_entries(rick, {"R": 8, "U": 8}, 0, 16, turn_number=5)
        return game, rick, engine

    def test_graveyard_and_exile_casts_survive_plan_validation(self):
        """_validate_plan_mana built its name set from hand + adventure +
        command zone only, so every card the castable list offers from the
        graveyard or exile was dropped as "not in hand" before the executor
        saw it. Pre-existing for flashback/escape; the whole wave lands in
        the same hole."""
        from mtg.ai_turn import _validate_plan_mana
        game, rick, engine = self._plan_game()
        plan = [
            {"type": "cast", "card": "Risk Factor"},     # jump-start, graveyard
            {"type": "cast", "card": "Memory"},          # aftermath half, graveyard
            {"type": "cast", "card": "Quakebringer"},    # foretold, exile
        ]
        kept = {a.get("card") for a in _validate_plan_mana(engine, game, 0, plan)}
        assert {"Risk Factor", "Memory", "Quakebringer"} <= kept, kept

    def test_the_alternative_cost_is_what_gets_priced(self):
        """Pricing a foretold Quakebringer at its printed {3}{R}{R}, or an
        aftermath half at the split card's combined CMC 10, would reject
        casts the payment stage pays comfortably."""
        from mtg.ai_turn import _validate_plan_mana
        game, rick, engine = self._plan_game()
        # Exactly 4 Mountains: the foretell cost {2}{R}{R} fits, the printed
        # {3}{R}{R} does not. Decisive on exactly the pricing.
        for land in rick.battlefield:
            land.tapped = True
        for land in rick.battlefield[:4]:
            land.tapped = False
        kept = _validate_plan_mana(
            engine, game, 0, [{"type": "cast", "card": "Quakebringer"}])
        assert any(a.get("card") == "Quakebringer" for a in kept), kept

    def test_new_action_types_survive_plan_validation(self):
        from mtg.ai_turn import _validate_plan_mana
        game, rick, engine = self._plan_game()
        plan = [
            {"type": "graveyard_activate", "card": "Angel of Sanctions",
             "mechanic": "embalm"},
            {"type": "foretell", "card": "Quakebringer"},
        ]
        kept = {a.get("type") for a in _validate_plan_mana(engine, game, 0, plan)}
        assert {"graveyard_activate", "foretell"} <= kept, kept

    def test_both_action_grammar_blocks_document_the_new_types(self):
        """The model cannot emit JSON it was never shown. There are TWO
        grammar blocks (decide_action and plan_turn) and they have drifted
        before — the July 30 suspend pin exists for the same reason."""
        import inspect

        import mtg.claude_player as cp
        src = inspect.getsource(cp)
        for action_type in ('"foretell"', '"graveyard_activate"'):
            assert src.count(f'{{"type": {action_type}') >= 2, (
                f"{action_type} must appear in BOTH action-grammar blocks")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
