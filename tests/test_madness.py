"""Madness (CR 702.35) — implemented Aug 1, 2026.

The batch-11 madness/graveyard reviewer confirmed the mechanic was entirely
absent (code-absence grep + two live victims in game_1532532252825616466:
Wheel of Fortune discarded Bloodmad Vampire + Avacyn's Judgment, Reforge
the Soul discarded Violent Eruption — all silently to the graveyard).

Architecture (the suspend-initiation precedent — one shared core):
- helpers.parse_madness_cost: the one parser (cache-verified shapes:
  {R}, {1}{R}, {1}{R}{R}, {1}{B}, {B}, {X}{R}, {0}).
- helpers.madness_discard_to_exile: the sync choke point every discard
  site calls INSTEAD of graveyard.append — exiles the card, records
  (card, owner_idx) on game._madness_pending, and runs the Anje-family
  untap scan ("Whenever you discard a card, if it has madness, untap").
- spells.resolve_pending_madness: the async drain (invoked at the front of
  drain_pending_triggers) — cast for the madness cost when affordable
  (pre-move exile→hand, _cast_via_madness stamp, real cast_spell_async),
  else graveyard per CR 702.35d; a failed cast also rolls back to
  graveyard.
- can_cast_spell: _cast_via_madness checks the MADNESS cost (usually
  cheaper than printed — the FoW-waiver class) and waives timing
  (CR 702.35a — the cast happens as the trigger resolves, off-turn included).
- Self-chosen discard selectors ("worst"/loots/hand-size) PREFER a madness
  card — discarding it is pure upside, and it's Anje's whole engine.

Oracle texts below are cache-verified (data/card_data_cache.json,
lowercase keys) — the pin-shape-reachability lesson: fixtures use the REAL
printed text and the REAL action-handler entry points.
"""
import asyncio
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

FIERY_TEMPER = ("Fiery Temper deals 3 damage to any target.\n"
                "Madness {R} (If you discard this card, discard it into "
                "exile. When you do, cast it for its madness cost or put "
                "it into your graveyard.)")
BLOODMAD = ("Whenever this creature deals combat damage to a player, put a "
            "+1/+1 counter on it.\n"
            "Madness {1}{R} (If you discard this card, discard it into "
            "exile. When you do, cast it for its madness cost or put it "
            "into your graveyard.)")
VIOLENT_ERUPTION = ("Violent Eruption deals 4 damage divided as you choose "
                    "among any number of targets.\n"
                    "Madness {1}{R}{R} (If you discard this card, discard "
                    "it into exile. When you do, cast it for its madness "
                    "cost or put it into your graveyard.)")
ANJE = ("Haste\n{T}, Discard a card: Draw a card.\n"
        "Whenever you discard a card, if it has madness, untap Anje "
        "Falkenrath.")


def _fiery_temper(make_card):
    return make_card("Fiery Temper", type_line="Instant", mana_cost="{1}{R}{R}",
                     cmc=3, oracle_text=FIERY_TEMPER,
                     power=None, toughness=None)


def _mountains(player, make_card, n):
    for _ in range(n):
        player.battlefield.append(make_card(
            "Mountain", type_line="Basic Land — Mountain",
            oracle_text="({T}: Add {R}.)", power=None, toughness=None))


# ---------------------------------------------------------------------------
# The parser
# ---------------------------------------------------------------------------

class TestParseMadnessCost:
    def test_cache_verified_shapes(self):
        from mtg.helpers import parse_madness_cost
        assert parse_madness_cost(FIERY_TEMPER) == "{R}"
        assert parse_madness_cost(BLOODMAD) == "{1}{R}"
        assert parse_madness_cost(VIOLENT_ERUPTION) == "{1}{R}{R}"
        assert parse_madness_cost("Madness {X}{R} (If you discard...)") == "{X}{R}"
        assert parse_madness_cost(
            "Return target black creature card from your graveyard to your "
            "hand.\nMadness {0} (If you discard this card...)") == "{0}"

    def test_reminder_and_reference_text_do_not_match(self):
        from mtg.helpers import parse_madness_cost
        # The reminder text's own "madness cost" phrases carry no brace
        # after the word; Anje merely REFERENCES madness.
        assert parse_madness_cost(
            "When you do, cast it for its madness cost or put it into your "
            "graveyard.") is None
        assert parse_madness_cost(ANJE) is None
        assert parse_madness_cost("") is None
        assert parse_madness_cost(None) is None


# ---------------------------------------------------------------------------
# The redirect, exercised through the REAL discard action handler
# ---------------------------------------------------------------------------

class TestDiscardRedirect:
    def test_named_discard_goes_to_exile_with_pending(self, game, rules, make_card):
        rick = game.players[0]
        temper = _fiery_temper(make_card)
        rick.hand = [temper, make_card("Grizzly Bears")]
        msg = rules._execute_action_on_state(
            game, {"action": "discard", "player": "Rick", "card": "Fiery Temper"})
        assert temper in rick.exile, "CR 702.35: discarded into EXILE"
        assert temper not in rick.graveyard
        assert (temper, 0) in game._madness_pending
        assert "into exile" in (msg or "")
        assert temper._madness_cost == "{R}"

    def test_non_madness_discard_unaffected(self, game, rules, make_card):
        rick = game.players[0]
        bears = make_card("Grizzly Bears")
        rick.hand = [bears]
        rules._execute_action_on_state(
            game, {"action": "discard", "player": "Rick", "card": "Grizzly Bears"})
        assert bears in rick.graveyard
        assert game._madness_pending == []

    def test_discard_all_splits_hand(self, game, rules, make_card):
        # The Wheel of Fortune shape — the batch's live victim.
        rick = game.players[0]
        temper = _fiery_temper(make_card)
        bears = make_card("Grizzly Bears")
        rick.hand = [temper, bears]
        msg = rules._execute_action_on_state(
            game, {"action": "discard", "player": "Rick", "card": "all"})
        assert temper in rick.exile and (temper, 0) in game._madness_pending
        assert bears in rick.graveyard
        assert "into exile" in (msg or "")

    def test_self_chosen_selector_prefers_madness(self, game, rules, make_card):
        # "worst" would otherwise take the 6-drop; the madness card is the
        # strictly better discard (it comes back castable).
        rick = game.players[0]
        temper = _fiery_temper(make_card)
        fatty = make_card("Inkwell Leviathan", cmc=9,
                          type_line="Artifact Creature — Leviathan")
        rick.hand = [fatty, temper]
        rules._execute_action_on_state(
            game, {"action": "discard", "player": "Rick", "card": "worst"})
        assert temper in rick.exile, "self-chosen discards prefer madness"
        assert fatty in rick.hand

    def test_anje_untaps_on_madness_discard(self, game, rules, make_card):
        rick = game.players[0]
        anje = make_card("Anje Falkenrath", oracle_text=ANJE,
                         type_line="Legendary Creature — Vampire Noble",
                         power="1", toughness="3", tapped=True)
        rick.battlefield.append(anje)
        temper = _fiery_temper(make_card)
        rick.hand = [temper]
        rules._execute_action_on_state(
            game, {"action": "discard", "player": "Rick", "card": "Fiery Temper"})
        assert anje.tapped is False, (
            "Whenever you discard a card, if it has madness, untap Anje")

    def test_anje_stays_tapped_on_normal_discard(self, game, rules, make_card):
        rick = game.players[0]
        anje = make_card("Anje Falkenrath", oracle_text=ANJE,
                         type_line="Legendary Creature — Vampire Noble",
                         power="1", toughness="3", tapped=True)
        rick.battlefield.append(anje)
        rick.hand = [make_card("Grizzly Bears")]
        rules._execute_action_on_state(
            game, {"action": "discard", "player": "Rick", "card": "Grizzly Bears"})
        assert anje.tapped is True, "the trigger is madness-gated"


# ---------------------------------------------------------------------------
# The drain: cast-or-graveyard (CR 702.35d)
# ---------------------------------------------------------------------------

def _engine_for(game):
    from mtg.engine import GameEngine
    ge = GameEngine(None)
    game._rules_engine = ge.rules
    return ge


class TestResolvePendingMadness:
    def _pend(self, game, rules, rick, card):
        rick.hand.append(card)
        rules._execute_action_on_state(
            game, {"action": "discard", "player": "Rick", "card": card.name})
        assert card in rick.exile

    def test_affordable_madness_card_is_cast(self, game, rules, make_card):
        from mtg.spells import resolve_pending_madness
        rick, claude = game.players
        ge = _engine_for(game)
        _mountains(rick, make_card, 1)  # exactly the madness {R}, NOT the printed {1}{R}{R}
        temper = _fiery_temper(make_card)
        self._pend(game, rules, rick, temper)
        msgs = asyncio.run(resolve_pending_madness(ge, game))
        assert any("casts" in m and "madness cost {R}" in m for m in msgs), msgs
        assert temper not in rick.exile
        assert temper in rick.graveyard, "a resolved instant ends in the graveyard"
        assert temper._mana_paid == 1, (
            "the MADNESS cost {R} must be what was actually paid — printed "
            "is {1}{R}{R}, unpayable off one Mountain")
        assert all(c.tapped for c in rick.battlefield if c.is_land()), (
            "the Mountain paid for it")

    def test_unaffordable_goes_to_graveyard(self, game, rules, make_card):
        from mtg.spells import resolve_pending_madness
        rick = game.players[0]
        ge = _engine_for(game)
        temper = _fiery_temper(make_card)  # no mana sources at all
        self._pend(game, rules, rick, temper)
        msgs = asyncio.run(resolve_pending_madness(ge, game))
        assert temper in rick.graveyard, "CR 702.35d: not cast → graveyard"
        assert temper not in rick.exile
        assert any("not paid" in m for m in msgs)

    def test_failed_cast_rolls_back_to_graveyard(self, game, rules, make_card,
                                                 monkeypatch):
        from mtg.spells import resolve_pending_madness
        rick = game.players[0]
        ge = _engine_for(game)
        _mountains(rick, make_card, 1)
        temper = _fiery_temper(make_card)
        self._pend(game, rules, rick, temper)

        async def _forced_fail(g, p, c, **kw):
            return False, "forced failure", []

        monkeypatch.setattr(ge, "cast_spell_async", _forced_fail)
        asyncio.run(resolve_pending_madness(ge, game))
        assert temper not in rick.hand, "the pre-move must be rolled back"
        assert temper not in rick.exile
        assert temper in rick.graveyard
        assert temper._cast_via_madness is False, "the stamp must be cleared"

    def test_drain_runs_from_drain_pending_triggers(self, game, rules, make_card):
        # The wiring pin: an empty Tier-3 trigger queue must NOT starve the
        # madness drain (the early-return sat above it before the hook).
        from mtg.triggers import drain_pending_triggers
        rick = game.players[0]
        ge = _engine_for(game)
        temper = _fiery_temper(make_card)
        self._pend(game, rules, rick, temper)
        assert not getattr(game, 'pending_async_triggers', None)
        msgs = asyncio.run(drain_pending_triggers(ge, game))
        assert temper in rick.graveyard, (
            "pending madness must resolve through the shared drain even "
            "with zero pending triggers")
        assert any("not paid" in m for m in msgs)


# ---------------------------------------------------------------------------
# The cast gates: cost selection + timing waiver (CR 702.35a)
# ---------------------------------------------------------------------------

class TestMadnessCastGates:
    def test_pre_gate_checks_madness_cost_not_printed(self, game, rules, make_card):
        rick = game.players[0]
        _mountains(rick, make_card, 1)
        temper = _fiery_temper(make_card)  # printed {1}{R}{R}
        temper._madness_cost = "{R}"
        rick.hand.append(temper)
        can, _ = rules.can_cast_spell(game, rick, temper)
        assert not can, "sanity: one Mountain can't pay the printed {1}{R}{R}"
        temper._cast_via_madness = True
        can, reason = rules.can_cast_spell(game, rick, temper)
        assert can, f"the madness cast checks {{R}}: {reason}"

    def test_timing_waived_for_madness_creature_off_turn(self, game, rules, make_card):
        from mtg.constants import Phase
        rick, claude = game.players
        _mountains(claude, make_card, 2)
        vamp = make_card("Bloodmad Vampire", oracle_text=BLOODMAD,
                         mana_cost="{1}{R}", cmc=2,
                         type_line="Creature — Vampire Berserker",
                         power="4", toughness="1")
        vamp._madness_cost = "{1}{R}"
        claude.hand.append(vamp)
        # Rick's combat, Claude (non-active) casting a creature: every
        # sorcery-speed gate would fire without the waiver.
        game.set_phase(Phase.COMBAT_DAMAGE, via="test-setup")
        game.active_player_index = 0
        can, reason = rules.can_cast_spell(game, claude, vamp)
        assert not can, "sanity: no timing waiver without the stamp"
        vamp._cast_via_madness = True
        can, reason = rules.can_cast_spell(game, claude, vamp)
        assert can, f"CR 702.35a — the madness cast ignores timing: {reason}"

    def test_compute_alt_costs_charges_madness_cost(self, game, make_card):
        from mtg.engine import GameEngine
        from mtg.spells import _compute_alt_costs
        rick = game.players[0]
        _mountains(rick, make_card, 3)
        eruption = make_card("Violent Eruption", type_line="Instant",
                             mana_cost="{1}{R}{R}{R}", cmc=4,
                             oracle_text=VIOLENT_ERUPTION,
                             power=None, toughness=None)
        eruption._madness_cost = "{1}{R}{R}"
        eruption._cast_via_madness = True
        rick.hand.append(eruption)
        early, costs = _compute_alt_costs(GameEngine(None), game, rick,
                                          eruption, pay_mana=True,
                                          additional_cost=0)
        assert early is None
        assert costs['effective_mana_cost'] == "{1}{R}{R}"
        assert costs['total_cost'] == 3, "madness {1}{R}{R}, not printed {1}{R}{R}{R}"
