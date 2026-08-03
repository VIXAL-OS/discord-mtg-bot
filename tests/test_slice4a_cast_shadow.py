"""Pub/sub slice 4 (CARD_CAST) — the emission spine.

Slice 4a (July 24, 2026) emitted CARD_CAST at every cast path and shadowed it
with a parity recorder. Slice 4b (July 26, 2026) retired the recorder after
game_15304* returned [EVENT-PARITY-CAST]=0 on post-4a code.

**The consumer deliberately did NOT move onto the bus, and this file is what
keeps that decision honest.** `_check_cast_triggers` is async and needs
`await` for Tier-3 `resolve_effect`, the cascade free-cast, its own recursion,
and — decisively — `engine._combat_priority_round`, which is the
[CAST-TRIGGER-PRIORITY] window that lets a Stifle counter a cast trigger (19
fires in that batch). The bus contract is sync handlers only, so subscribing a
queuer would demote every inline cast trigger — Talrand tokens, prowess, the
whole counter-a-trigger interaction — to a Tier-3 drain. That is a real
behaviour downgrade bought with nothing but uniformity.

What the migration actually wanted is still delivered: CARD_CAST fires at
EVERY cast path (both async funnels plus the sync bridge), which is the "one
spine, no missed call sites" property the parity gate proved. Consumption
differs by path — the async funnels consume directly (they ARE the funnel),
the sync sites consume via `queue_cast_triggers_sync`.

Because there is now no subscriber watching it, the emission is exactly the
kind of thing that rots silently. These tests are the net: they pin that both
funnels still emit, that a failed cast emits nothing, and that no async
handler has been quietly subscribed in violation of the bus contract.
"""
import asyncio
import inspect

import pytest

from mtg import events


class TestEmissionSpine:
    """Every cast path must still reach the bus."""

    def test_cast_spell_async_emits_exactly_once(self, make_game, make_card):
        from mtg.engine import GameEngine
        from mtg.spells import cast_spell_async
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

        seen = []
        events.subscribe(events.CARD_CAST,
                         lambda g, **kw: seen.append(kw) or None)
        try:
            ok, msg, _ = asyncio.run(cast_spell_async(engine, game, rick, bolt))
        finally:
            events._subscribers[events.CARD_CAST] = [
                s for s in events._subscribers.get(events.CARD_CAST, [])
                if getattr(s, "__name__", "") != "<lambda>"]
        assert ok, msg
        mine = [kw for kw in seen if getattr(kw.get('card'), 'id', None) == bolt.id]
        assert len(mine) == 1, f"exactly one CARD_CAST per cast, got {len(mine)}"
        assert mine[0].get('via') == "cast"
        assert mine[0].get('caster') is rick

    def test_failed_cast_emits_nothing(self, make_game, make_card):
        """A cast rejected by _validate_cast / payment never happened (CR 601.2)."""
        from mtg.engine import GameEngine
        from mtg.spells import cast_spell_async
        engine = GameEngine(None)
        game = make_game()
        rick = game.players[0]   # zero lands — payment fails
        bolt = make_card("Lightning Bolt", type_line="Instant",
                         oracle_text="Lightning Bolt deals 3 damage to any target.",
                         mana_cost="{R}", cmc=1, power="0", toughness="0")
        rick.hand.append(bolt)

        seen = []
        events.subscribe(events.CARD_CAST,
                         lambda g, **kw: seen.append(kw) or None)
        try:
            ok, msg, _ = asyncio.run(cast_spell_async(engine, game, rick, bolt))
        finally:
            events._subscribers[events.CARD_CAST] = [
                s for s in events._subscribers.get(events.CARD_CAST, [])
                if getattr(s, "__name__", "") != "<lambda>"]
        assert not ok
        assert not [kw for kw in seen
                    if getattr(kw.get('card'), 'id', None) == bolt.id], (
            "a failed cast is not a cast")

    def test_cascade_free_cast_site_emits(self):
        """The second async funnel — structural, it needs a real cascade to run."""
        import mtg.triggers
        src = inspect.getsource(mtg.triggers)
        anchor = src.index("_check_cast_triggers(\n                        engine, game, caster, found_card)")
        window = src[max(0, anchor - 900):anchor]
        assert 'events.emit(events.CARD_CAST' in window and 'via="cascade"' in window, (
            "the cascade free-cast lost its CARD_CAST emit")

    def test_cascade_fires_cast_triggers_before_resolving(self):
        """CR 601.2i / 603.3 — Aug 2 batch-14 (R-L1, CRITICAL).

        The cascaded card is CAST, so "whenever a player casts" triggers go
        on the stack above it and resolve FIRST. The fire used to sit after
        the whole resolution block, and the opponent-cast scan walks a LIVE
        battlefield: a cascaded Assassin's Trophy destroyed the Eidolon of
        the Great Revel that should have triggered on it, the scan then
        found nothing, and the dropped 2 damage flipped the winner of
        game_1533407568360112128 (the caster was at 1 life).
        """
        import mtg.triggers
        src = inspect.getsource(mtg.triggers)
        fire = src.index("_check_cast_triggers(\n                        engine, game, caster, found_card)")
        resolve = src.index('[CASCADE-SPELL] Tier 1.5 resolved')
        enters = src.index('→ **{found_card.name}** enters the battlefield')
        assert fire < resolve, (
            "cascade must fire cast-triggers BEFORE resolving the spell's "
            "own effect, or the effect can remove the triggering permanent")
        assert fire < enters, (
            "same for the creature branch — the permanent must not be on "
            "the battlefield before its own cast triggers are collected")

    def test_sync_bridge_emits(self):
        """suspend / Etali / free-cast moves / legacy sync cast (7ba7ad6)."""
        import mtg.triggers
        src = inspect.getsource(mtg.triggers.queue_cast_triggers_sync)
        assert "events.emit(events.CARD_CAST" in src, (
            "the sync cast bridge lost its CARD_CAST emit — suspend/Etali/"
            "free-cast moves would leave the spine again")


class TestConsumerStaysOffTheBus:
    """Slice 4b's decision, pinned so it can't be undone by accident."""

    def test_no_async_handler_is_subscribed(self):
        """The bus contract is sync handlers only (mtg/events.py CONTRACTS)."""
        import mtg.triggers  # noqa: F401 — registration happens at import
        for ev, subs in events._subscribers.items():
            for s in subs:
                assert not inspect.iscoroutinefunction(s), (
                    f"{getattr(s, '__name__', s)} is async but subscribed to "
                    f"{ev}; emit() dispatches synchronously so the coroutine "
                    f"would never be awaited")

    def test_check_cast_triggers_is_not_subscribed(self):
        """Subscribing it would demote inline cast triggers to Tier-3 drains
        and destroy the [CAST-TRIGGER-PRIORITY] Stifle window."""
        import mtg.triggers  # noqa: F401
        names = [getattr(s, "__name__", "")
                 for s in events._subscribers.get(events.CARD_CAST, [])]
        assert "_check_cast_triggers" not in names, (
            "the async cast-trigger scan must not be a subscriber — see this "
            "module's docstring for why the 4b flip was deliberately not made")

    def test_check_cast_triggers_still_needs_async(self):
        """If this ever stops being true, revisit the 4b decision."""
        import mtg.triggers
        assert inspect.iscoroutinefunction(mtg.triggers._check_cast_triggers)
        src = inspect.getsource(mtg.triggers._check_cast_triggers)
        assert "_combat_priority_round" in src, (
            "the [CAST-TRIGGER-PRIORITY] window was the decisive reason the "
            "consumer stayed off the bus — if it moved, re-evaluate slice 4b")

    def test_parity_recorder_is_retired(self):
        """Slice 4b cleanup: recorder, reporter and its two fields are gone."""
        import mtg.triggers
        from mtg.models import GameState
        assert not hasattr(mtg.triggers, "_record_cast_for_parity")
        assert not hasattr(mtg.triggers, "report_cast_parity")
        fields = {f.name for f in GameState.__dataclass_fields__.values()}
        assert "_cast_events" not in fields
        assert "_cast_scanned_ids" not in fields
