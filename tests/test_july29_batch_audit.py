"""Pins for the July 29, 2026 batch-15315 audit fixes.

Batch 15315 (152 games, sha=e76a1ff, strict=0) was the first live exercise of
the July 27/28 fanout fixes. The audit confirmed most of them and surfaced
seven new findings, fixed and pinned here:

A. The post-game flush gate ate the lethal combat summary in ~150/152 games
   (gate refined to fire only after the final summary posts — the new window
   test lives in tests/test_july28_instrumentation.py next to the original).
B. Into the Roil's template drew a card UNCONDITIONALLY — the kicker was
   never paid, and Chain of Vapor (same generator, no draw on any printing)
   drew too.
C. Tymna the Weaver STILL did nothing: the July 27 main-phase scan queued her
   to Tier 3 (which refused with a hallucinated reason) because no template
   existed AND "main_phase" was missing from _NAME_KEYED_EVENT_TYPES — the
   exact May 16 Bug-B shape (inner gate relaxed, outer gate not).
D. Rick's graveyard-cast branch removed the card from the graveyard and paid
   the escape exile cost but never appended the card to hand — the July 20
   zone-first gate then rejected "Card not in hand", stranding the card in NO
   zone with two graveyard cards destroyed (game_1531564194842017916,
   Sentinel's Eyes). engine.py's twin had the same missing failure rollback.
E. "This creature escapes with two +1/+1 counters on it" was silently dropped
   — batch 15315's first live escape (Woe Strider) entered as a bare 3/2.
F. An open [CAST-TRIGGER-PRIORITY] window (LLM evaluation in flight) burned
   the buried spell's whole LIFO extension/rescue budget — Beast Whisperer
   resolved BENEATH the Arcane Denial targeting it (CR 608), the counter
   fizzled, and the force-path double-resolved the Mystic trigger with its
   Bird token materializing silently.
G. The cube drafter's claude-sonnet-5 picks came back EMPTY ~70% of the time:
   adaptive thinking is on by default on sonnet-5 and max_tokens=50 was
   consumed entirely by the thinking block before any text arrived.
"""
import asyncio
from pathlib import Path

import pytest

from mtg.constants import Phase

ROOT = Path(__file__).resolve().parent.parent


def _engine():
    from mtg.engine import GameEngine
    return GameEngine(None)


def _wire(engine, game):
    game._rules_engine = engine.rules
    engine.rules.engine_ref = engine
    game.phase = Phase.MAIN1
    game.active_player_index = 0
    return game


def _plains(make_card, n):
    return [make_card(f"Plains {i}", type_line="Basic Land — Plains",
                      power="0", toughness="0") for i in range(n)]


# ---------------------------------------------------------------------------
# D — graveyard cast: hand-append, failure rollback, exile-cost refund
# ---------------------------------------------------------------------------

_ESCAPER_ORACLE = (
    "Escape—{1}{W}, Exile two other cards from your graveyard. "
    "(You may cast this card from your graveyard for its escape cost.)\n"
    "This creature escapes with two +1/+1 counters on it."
)


class TestGraveyardCastRollback:

    def _stock_graveyard(self, make_card, rick):
        escaper = make_card("Test Escaper",
                            type_line="Creature — Zombie",
                            oracle_text=_ESCAPER_ORACLE,
                            mana_cost="{W}", cmc=1,
                            power="3", toughness="2")
        fodder = [make_card("Fodder A", type_line="Sorcery",
                            power="0", toughness="0"),
                  make_card("Fodder B", type_line="Instant",
                            power="0", toughness="0")]
        rick.graveyard.append(escaper)
        rick.graveyard.extend(fodder)
        rick.playable_from_graveyard.append(escaper.id)
        return escaper, fodder

    def test_successful_escape_reaches_battlefield_with_counters(
            self, make_game, make_card):
        """The happy path through engine.py's graveyard branch: the card is
        pre-moved to hand (the July 20 zone-first gate demands a home), the
        exile cost sticks, and the CR 702.139e counter rider applies (E)."""
        engine = _engine()
        game = _wire(engine, make_game())
        rick = game.players[0]
        rick.battlefield.extend(_plains(make_card, 2))
        escaper, fodder = self._stock_graveyard(make_card, rick)

        asyncio.run(engine._execute_action(
            game, 0, {"type": "cast", "card": "Test Escaper"}))

        assert escaper in rick.battlefield, "the escaped creature must resolve"
        assert escaper._was_escaped is True
        assert escaper.counters.get('+1/+1') == 2, \
            "CR 702.139e: 'escapes with two +1/+1 counters' must apply (E)"
        assert all(f in rick.exile for f in fodder), \
            "the escape exile cost stays paid on success"

    def test_failed_escape_rolls_the_whole_cast_back(
            self, make_game, make_card):
        """game_1531564194842017916: Sentinel's Eyes was pulled out of the
        graveyard, two cards were exiled as its escape cost, then the cast
        failed and the card was stranded in NO zone. A failed graveyard cast
        must restore the card, the playable marker, AND the exile cost."""
        engine = _engine()
        game = _wire(engine, make_game())
        rick = game.players[0]
        # No mana sources at all — the cast must fail at the mana gate.
        escaper, fodder = self._stock_graveyard(make_card, rick)

        asyncio.run(engine._execute_action(
            game, 0, {"type": "cast", "card": "Test Escaper"}))

        assert escaper in rick.graveyard, \
            "the card must return to the graveyard, not vanish"
        assert escaper not in rick.hand
        assert escaper.id in rick.playable_from_graveyard, \
            "the playable marker must be restored so a retry is possible"
        assert all(f in rick.graveyard for f in fodder), \
            "the escape exile cost must be refunded (cost-paid-no-effect)"
        assert rick.exile == []
        assert escaper._was_escaped is False

    def test_autoplay_twin_carries_the_same_branch(self):
        """The autoplay (Rick) path is the one that failed live. Its fix
        mirrors engine.py; pin both files together — the two-cast-paths
        divergence is a documented recurring bug class."""
        for rel in ("mtg/autoplay.py", "mtg/engine.py"):
            src = (ROOT / rel).read_text(encoding="utf-8")
            assert "_gy_escape_exiled" in src, \
                f"{rel}: escape exile cost is no longer tracked for rollback"
        autoplay = (ROOT / "mtg/autoplay.py").read_text(encoding="utf-8")
        assert "card._cast_from_graveyard = True" in autoplay, \
            "autoplay must stamp the graveyard-origin marker like engine.py"
        assert "if from_graveyard:" in autoplay


# ---------------------------------------------------------------------------
# E — the escapes-with-counters parser
# ---------------------------------------------------------------------------

class TestParseEscapesWithCounters:

    def test_word_numbers(self):
        from mtg.helpers import parse_escapes_with_counters
        assert parse_escapes_with_counters(
            "This creature escapes with two +1/+1 counters on it.") == 2

    def test_article_counts_as_one(self):
        from mtg.helpers import parse_escapes_with_counters
        # Phoenix of Ash / Ox of Agonas print "escapes with a +1/+1 counter".
        assert parse_escapes_with_counters(
            "Phoenix of Ash escapes with a +1/+1 counter on it.") == 1

    def test_no_rider_is_zero(self):
        from mtg.helpers import parse_escapes_with_counters
        assert parse_escapes_with_counters(
            "Escape—{3}{B}{B}, Exile four other cards from your graveyard.") == 0
        assert parse_escapes_with_counters("") == 0
        assert parse_escapes_with_counters(None) == 0


# ---------------------------------------------------------------------------
# B — Into the Roil's draw is kicker-gated
# ---------------------------------------------------------------------------

_ROIL_ORACLE = ("kicker {1}{u} (you may pay an additional {1}{u} as you cast "
                "this spell.)\nreturn target nonland permanent to its owner's "
                "hand. if this spell was kicked, draw a card.")


class TestBounceKickerGate:

    def _lib(self):
        from rules.effect_templates import get_effect_library
        return get_effect_library()

    def test_unkicked_cast_does_not_draw(self):
        """Batch 15315 game_1531552353407340545: Into the Roil was cast for
        {1}{U} (2 sources tapped, kicker never paid) and still drew."""
        actions = self._lib()._gen_bounce_opponent_permanent(
            "Claude", "Rick",
            {'best_opponent_creature': 'Sphinx of Uthuun',
             '_oracle': _ROIL_ORACLE, 'mana_paid_total': 2})
        assert [a['action'] for a in actions] == ['move_card'], \
            "an unkicked Into the Roil bounces but must not draw"

    def test_kicked_cast_draws(self):
        actions = self._lib()._gen_bounce_opponent_permanent(
            "Claude", "Rick",
            {'best_opponent_creature': 'Sphinx of Uthuun',
             '_oracle': _ROIL_ORACLE, 'mana_paid_total': 4})
        assert [a['action'] for a in actions] == ['move_card', 'draw_cards']

    def test_chain_of_vapor_never_draws(self):
        """Chain of Vapor shares the generator and has no draw clause on any
        printing — the old unconditional append gave it a free card too."""
        actions = self._lib()._gen_bounce_opponent_permanent(
            "Claude", "Rick",
            {'best_opponent_creature': 'Sphinx of Uthuun',
             '_oracle': "return target nonland permanent to its owner's hand.",
             'mana_paid_total': 9})
        assert [a['action'] for a in actions] == ['move_card']


# ---------------------------------------------------------------------------
# C — Tymna the Weaver's main-phase template
# ---------------------------------------------------------------------------

_TYMNA_TRIGGER = ("At the beginning of each of your postcombat main phases, "
                  "you may pay X life, where X is the number of opponents "
                  "that were dealt combat damage this turn. If you do, "
                  "draw X cards.")


class TestTymnaMainPhaseTemplate:

    def _resolve(self, ctx, event_type="main_phase"):
        from rules.effect_templates import get_effect_library
        lib = get_effect_library()
        actions, _desc = lib.resolve_etb(
            card_name="Tymna the Weaver", oracle_text=_TYMNA_TRIGGER,
            controller="Claude", opponent="Rick",
            game_context=ctx, event_type=event_type)
        return actions

    def test_pays_x_life_and_draws_x(self):
        """Batch 15315: the scan queued Tymna to Tier 3 every postcombat main
        phase and the judge refused with 'combat actions can't resolve at
        sorcery speed' — her whole card-advantage engine was a no-op. The
        template consumes ctx['_opponents_dealt_combat_damage'] (the July 27
        producer that had no consumer)."""
        actions = self._resolve({'_opponents_dealt_combat_damage': 1,
                                 'controller_life': 40})
        assert actions == [
            {"action": "lose_life", "player": "Claude", "amount": 1},
            {"action": "draw_cards", "player": "Claude", "amount": 1},
        ]

    def test_no_combat_damage_is_a_handled_noop(self):
        # [] is the library's handled-no-op contract — it must NOT escalate
        # to Tier 3 (the July 21 Meren regression class).
        assert self._resolve({'_opponents_dealt_combat_damage': 0,
                              'controller_life': 40}) == []

    def test_declines_when_life_is_low(self):
        assert self._resolve({'_opponents_dealt_combat_damage': 1,
                              'controller_life': 5}) == []

    def test_does_not_fire_on_etb(self):
        """The bare-name template carries a scheduled-prefix description, so
        the ETB scan (Tymna entering the battlefield) must not fire it —
        the Agent of Treachery cascade-steal guard, exercised backwards."""
        actions = self._resolve({'_opponents_dealt_combat_damage': 1,
                                 'controller_life': 40}, event_type="etb")
        assert not any(a.get('action') == 'lose_life'
                       for a in (actions or [])), \
            "Tymna's main-phase payment must not fire when she merely enters"

    def test_main_phase_is_a_name_keyed_event_type(self):
        """The July 27 fix added 'main_phase' to scheduled_event_types (the
        inner gate) but not to _NAME_KEYED_EVENT_TYPES (the outer gate) —
        the exact May 16 Bug-B shape. Pin the outer gate."""
        src = (ROOT / "rules/effect_templates.py").read_text(encoding="utf-8")
        gate = src.split("_NAME_KEYED_EVENT_TYPES = {", 1)[1].split("}", 1)[0]
        assert '"main_phase"' in gate, \
            "a name-keyed main-phase template can never fire without this"


# ---------------------------------------------------------------------------
# F — LIFO wait loop respects an open cast-trigger priority window
# ---------------------------------------------------------------------------

class TestTriggerWindowAwareness:

    def test_gamestate_declares_the_window_depth(self, game):
        assert getattr(game, '_trigger_window_depth') == 0

    def test_force_stack_above_refuses_while_a_window_is_open(
            self, game, capsys):
        """Batch 15315 game_1531566544532799639: the force-path resolved the
        Murmuring Mystic trigger inline while its Stifle-evaluation window
        was still open; the window then resolved it AGAIN via
        [CAST-TRIGGER-VANISHED], and the Bird token materialized silently."""
        from mtg.spells import _force_stack_above
        engine = _engine()
        game._trigger_window_depth = 1
        acted = _force_stack_above(engine, game, object(), [])
        out = capsys.readouterr().out
        assert acted is False
        assert "window open" in out

    def test_wait_loop_does_not_burn_extensions_during_a_window(self):
        """The extension loop is deep inside the async stack machinery, so
        pin its source: the window check must guard the extension counter
        (with its own bounded budget preserving the anti-deadlock cap)."""
        src = (ROOT / "mtg/spells.py").read_text(encoding="utf-8")
        assert "_trigger_window_depth" in src
        assert "window_waits_used" in src
        assert "max_window_waits" in src
        loop = src.split("max_window_waits", 1)[1]
        assert loop.index("window_waits_used") < loop.index(
            "extensions_used += 1"), \
            "the window wait must be checked BEFORE an extension is consumed"

    def test_window_depth_is_balanced_in_triggers(self):
        src = (ROOT / "mtg/triggers.py").read_text(encoding="utf-8")
        # Aug 1 deferred slate: TWO increment sites now — the own-cast
        # [CAST-TRIGGER-PRIORITY] window and the opponent-cast
        # [OPP-CAST-TRIGGER-STACK] window (CR 603.3). Each must pair its
        # increment with a finally-decrement so a window error can't wedge
        # the depth and starve the LIFO wait loop.
        parts = src.split("game._trigger_window_depth = getattr")
        assert len(parts) - 1 == 2, \
            "exactly two increment sites (own-cast + opponent-cast windows)"
        for i, tail in enumerate(parts[1:], 1):
            assert "finally:" in tail[:2000], \
                f"increment site #{i} lacks a nearby finally-decrement"


# ---------------------------------------------------------------------------
# A — the refined post-game gate field
# ---------------------------------------------------------------------------

class TestFinalSummaryField:
    def test_gamestate_declares_the_flag(self, game):
        # The behavioral pins live in tests/test_july28_instrumentation.py
        # (updated for the ended→summary window); this just pins the
        # declared-field contract for the ratchet.
        assert getattr(game, '_final_summary_posted') is False


# ---------------------------------------------------------------------------
# G — cube draft picks must not drown in adaptive thinking
# ---------------------------------------------------------------------------

class TestCubeDraftPickCall:
    def test_thinking_is_disabled_with_headroom(self):
        """claude-sonnet-5 runs adaptive thinking when `thinking` is omitted,
        and max_tokens caps thinking + text TOGETHER — max_tokens=50 was
        all thinking, response_text() returned '', and ~70% of the 15315
        cube game's picks fell back to the heuristic while still billing."""
        import re
        src = (ROOT / "cube_draft.py").read_text(encoding="utf-8")
        assert 'thinking={"type": "disabled"}' in src, \
            "the pick call must disable thinking (a single-number answer)"
        code_lines = [ln for ln in src.splitlines()
                      if not ln.lstrip().startswith("#")]
        assert not any(re.search(r"max_tokens\s*=\s*50\b", ln)
                       for ln in code_lines), \
            "50 tokens cannot fit an answer if thinking ever re-enables"
