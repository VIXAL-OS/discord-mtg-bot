"""Pub/sub slice 4a (July 24, 2026) — CARD_CAST in shadow mode.

CARD_CAST is emitted at the two live cast funnels — _await_stack_window
(every cast_spell_async cast) and the cascade free-cast — and the only
subscriber is the parity recorder. _check_cast_triggers stays the
directly-called consumer and records what it scanned; report_cast_parity
(end_turn) prints [EVENT-PARITY-CAST] for emissions the scan never saw.
One clean batch gates slice 4b (flip the scan into a subscriber + close
the sync-cast-site gaps: suspend, Etali, free-cast moves — deliberately
NOT emitted in 4a, see mtg/events.py's migration plan).
"""
import asyncio
import inspect

import pytest

from mtg import events


class TestCastShadowRecorder:
    def test_emission_is_recorded_with_via(self, make_game, make_card):
        import mtg.triggers  # noqa: F401 — subscriber registers at import
        game = make_game()
        rick = game.players[0]
        bolt = make_card("Lightning Bolt", type_line="Instant",
                         power="0", toughness="0")
        events.emit(events.CARD_CAST, game, card=bolt, caster=rick,
                    via="cast", engine=None)
        assert game._cast_events
        assert game._cast_events[-1] == (bolt.id, "Lightning Bolt", "cast")

    def test_scanned_cast_produces_no_miss(self, make_game, make_card):
        from mtg.triggers import report_cast_parity
        game = make_game()
        bolt = make_card("Lightning Bolt", type_line="Instant",
                         power="0", toughness="0")
        game._cast_events.append((bolt.id, bolt.name, "cast"))
        game._cast_scanned_ids.append(bolt.id)
        assert report_cast_parity(game) == []
        assert game._cast_events == []
        assert game._cast_scanned_ids == []

    def test_unscanned_cast_is_reported(self, make_game, make_card, capsys):
        from mtg.triggers import report_cast_parity
        game = make_game()
        bolt = make_card("Lightning Bolt", type_line="Instant",
                         power="0", toughness="0")
        game._cast_events.append((bolt.id, bolt.name, "cascade"))
        misses = report_cast_parity(game)
        assert len(misses) == 1
        assert "Lightning Bolt" in misses[0] and "cascade" in misses[0]
        assert "[EVENT-PARITY-CAST]" in capsys.readouterr().out
        # State cleared for the next turn's window.
        assert report_cast_parity(game) == []

    def test_multiplicity_two_casts_need_two_scans(self, make_game, make_card):
        # An adventure card is one Card OBJECT cast twice in a turn
        # (instant half, then creature half) — a set-based diff would mask
        # a missed scan on the second cast.
        from mtg.triggers import report_cast_parity
        game = make_game()
        adv = make_card("Bonecrusher Giant",
                        type_line="Creature — Giant",
                        power="4", toughness="3")
        game._cast_events.append((adv.id, adv.name, "cast"))
        game._cast_events.append((adv.id, adv.name, "cast"))
        game._cast_scanned_ids.append(adv.id)   # only ONE scan record
        misses = report_cast_parity(game)
        assert len(misses) == 1, (
            "two casts of the same card object need two scan records")


class TestCastShadowEndToEnd:
    def test_cast_spell_async_emits_once_and_scan_pairs(
            self, make_game, make_card):
        from mtg.engine import GameEngine
        from mtg.spells import cast_spell_async
        from mtg.triggers import report_cast_parity
        engine = GameEngine(None)
        game = make_game()
        rick = game.players[0]
        rick.battlefield.append(make_card(
            "Mountain", type_line="Basic Land — Mountain",
            power="0", toughness="0"))
        bolt = make_card("Lightning Bolt", type_line="Instant",
                         oracle_text="Lightning Bolt deals 3 damage to any target.",
                         mana_cost="{R}", cmc=1, power="0", toughness="0")
        rick.hand.append(bolt)
        ok, msg, _ = asyncio.run(cast_spell_async(engine, game, rick, bolt))
        assert ok, msg
        casts = [e for e in game._cast_events if e[0] == bolt.id]
        assert len(casts) == 1, (
            f"exactly one CARD_CAST per cast, got {len(casts)}")
        assert casts[0][2] == "cast"
        # The scan ran adjacent to the emit — the diff must be clean.
        assert report_cast_parity(game) == []

    def test_failed_cast_emits_nothing(self, make_game, make_card):
        # A cast rejected by _validate_cast / payment never happened
        # (CR 601.2) — no emission, no scan.
        from mtg.engine import GameEngine
        from mtg.spells import cast_spell_async
        engine = GameEngine(None)
        game = make_game()
        rick = game.players[0]   # zero lands — payment fails
        bolt = make_card("Lightning Bolt", type_line="Instant",
                         oracle_text="Lightning Bolt deals 3 damage to any target.",
                         mana_cost="{R}", cmc=1, power="0", toughness="0")
        rick.hand.append(bolt)
        ok, msg, _ = asyncio.run(cast_spell_async(engine, game, rick, bolt))
        assert not ok
        assert not game._cast_events, "a failed cast is not a cast"


class TestCastShadowStructure:
    def test_cascade_free_cast_site_emits(self):
        # Structural: the cascade free-cast (the second live funnel) must
        # emit CARD_CAST adjacent to its _check_cast_triggers call.
        import mtg.triggers
        src = inspect.getsource(mtg.triggers)
        anchor = src.index("_check_cast_triggers(engine, game, caster, found_card)")
        window = src[max(0, anchor - 800):anchor]
        assert "events.emit(events.CARD_CAST" in window, (
            "the cascade free-cast lost its CARD_CAST shadow emit")

    def test_recorder_is_the_only_card_cast_subscriber(self):
        # 4a is SHADOW mode: _check_cast_triggers must NOT be subscribed
        # yet — the flip is slice 4b, gated on a clean batch.
        import mtg.triggers  # noqa: F401 — registration happens at import
        subs = events._subscribers.get(events.CARD_CAST, [])
        names = [getattr(s, "__name__", "") for s in subs]
        assert names == ["_record_cast_for_parity"], (
            f"shadow mode: the recorder must be the sole subscriber, got {names}")
