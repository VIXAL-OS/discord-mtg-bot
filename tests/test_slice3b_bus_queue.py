"""Pub/sub slice 3b (July 23, 2026) — the dies queue is now bus-fed.

queue_death no longer appends to _recently_died directly; it emits
CREATURE_DIED and _accumulate_death_subscriber (the sole sanctioned appender)
does the append synchronously during dispatch. The dispatcher, wave semantics
(_active_dies_batch), and APNAP ordering (helpers.apnap_order_died) are
unchanged — only the queue's INPUT flipped from a direct append to the bus.
"""
from mtg import events
from mtg.triggers import queue_death, _accumulate_death_subscriber


class TestSlice3bBusFedQueue:
    def test_accumulator_is_registered(self):
        # (Slice 3c: the parity recorder this test originally ordered
        # against was retired; the accumulator being subscribed is the
        # remaining registration invariant.)
        import mtg.triggers  # noqa: F401 — registration happens at import
        subs = events._subscribers.get(events.CREATURE_DIED, [])
        names = [getattr(s, "__name__", "") for s in subs]
        assert "_accumulate_death_subscriber" in names, (
            "the sole sanctioned _recently_died appender must be subscribed")

    def test_queue_death_feeds_via_the_bus_not_a_direct_append(self, make_game, make_card):
        game = make_game()
        rick = game.players[0]
        bear = make_card("Bear")
        # Drop the accumulator to prove queue_death itself no longer appends —
        # the append is bus-driven now. (Order after re-subscribe is irrelevant;
        # the parity recorder doesn't read _recently_died.)
        events.unsubscribe(events.CREATURE_DIED, _accumulate_death_subscriber)
        try:
            queue_death(game, bear, rick)
            assert (bear, rick) not in game._recently_died, (
                "queue_death must NOT append directly post-3b")
        finally:
            events.subscribe(events.CREATURE_DIED, _accumulate_death_subscriber)
        game._recently_died.clear()
        queue_death(game, bear, rick)
        assert (bear, rick) in game._recently_died, "the subscriber feeds the queue"

    def test_wave_separation_preserved(self, make_game, make_card):
        # A death emitted after the dispatcher freezes the wave (resets
        # _recently_died to [] and moves it to _active_dies_batch) must land in
        # the FRESH list — becoming the next wave, not joining the frozen batch.
        game = make_game()
        rick = game.players[0]
        wave1 = make_card("Wave1 Creature")
        wave2 = make_card("Wave2 Creature")
        queue_death(game, wave1, rick)
        frozen = game._recently_died
        game._recently_died = []
        game._active_dies_batch = frozen
        queue_death(game, wave2, rick)  # a wave-1 trigger's collateral death
        assert (wave1, rick) in game._active_dies_batch
        assert (wave2, rick) in game._recently_died
        assert (wave2, rick) not in game._active_dies_batch
