"""Q-J slice 4: trigger/SBA events routed from persisted ids, not only queues.

THE GAP, measured before any of this was written.  ``pending_async_triggers``
holds live ``Card`` references and ``_recently_died`` holds ``(Card, Player)``
tuples, so neither can be written to a JSON snapshot — and neither was in
``GameState.to_dict()``.  A save taken while either was non-empty dropped every
entry SILENTLY: a queued Blood Artist trigger round-tripped to ``[]`` rather
than to an error.  Silent omission is precisely what the Q-J requirements
forbid ("route SBAs and triggers from persisted event IDs, not only in-memory
queues"), and the window is live rather than theoretical — the cast path
persists at ``complete`` AFTER effects run, while the drain happens later.

The direction chosen here is at-most-once, matching the action ledger: a record
is claimed BEFORE its trigger resolves, so a process death mid-drain drops that
trigger rather than firing it twice.  That argument is STRONGER for triggers
than for actions — a drained trigger resolves through Tier 3, which mints a
fresh plan on every call, so a re-dispatch applies a second plan's mutations on
top of the first.

WHAT IS DELIBERATELY NOT CLAIMED: the death half restores the CONTENTS of
``_recently_died`` and nothing else.  Wave separation (``_active_dies_batch``)
and APNAP batch ordering stay entirely with the existing dispatcher, because
those are batch-level semantics a per-event record cannot carry.
"""
import json

import pytest

from mtg.models import GameState, ResolutionEvent, StackEntry
from mtg.resolution import ResolutionCoordinator


def _reload(game):
    """The harshest honest in-process crash: bytes in, brand-new object out."""
    return GameState.from_dict(json.loads(json.dumps(game.to_dict(), default=str)))


def _artist(game, make_card):
    card = make_card(
        "Blood Artist", type_line="Creature — Vampire",
        oracle_text=("Whenever Blood Artist or another creature dies, "
                     "target player loses 1 life and you gain 1 life."))
    game.players[0].battlefield.append(card)
    return card


def _queue_trigger(coord, game, source, **over):
    """Mirror what engine._queue_async_trigger writes, record included."""
    fields = dict(controller_name="Rick", trigger_text="target player loses 1 life",
                  trigger_type="dies", context="Grizzly Bears died",
                  occurrence_key=None)
    fields.update(over)
    event = coord.record_event("trigger", source, **fields)
    game.pending_async_triggers.append({
        "source_card": source,
        "trigger_text": fields["trigger_text"],
        "trigger_type": fields["trigger_type"],
        "controller_name": fields["controller_name"],
        "context": fields["context"],
        "occurrence_key": fields["occurrence_key"],
        "event_id": event.event_id,
    })
    return event


# --------------------------------------------------------------------------
# The gap itself
# --------------------------------------------------------------------------

def test_the_live_queue_still_cannot_serialize(game, make_card):
    """The premise, pinned: without the durable record the queue is LOST.

    Kept as a standing control so nobody "simplifies" the event store away on
    the belief that the queue itself round-trips. It does not, and it fails
    silently — an empty list, never an error.
    """
    source = _artist(game, make_card)
    game.pending_async_triggers.append({
        "source_card": source, "trigger_text": "loses 1 life",
        "trigger_type": "dies", "controller_name": "Rick",
        "context": "", "occurrence_key": None,
    })
    assert "pending_async_triggers" not in game.to_dict()
    assert _reload(game).pending_async_triggers == []


def test_a_recorded_trigger_survives_and_rebinds(game, make_card):
    source = _artist(game, make_card)
    coord = ResolutionCoordinator.for_game(None, game)
    _queue_trigger(coord, game, source)

    restored = _reload(game)
    assert restored.pending_async_triggers == [], "queue cannot carry live refs"
    assert len(restored.resolution_events) == 1, "the record must survive"

    rebuilt = ResolutionCoordinator.for_game(None, restored)
    assert rebuilt.rebuild_pending_queues() == 1
    entry = restored.pending_async_triggers[0]
    assert entry["trigger_text"] == "target player loses 1 life"
    assert entry["trigger_type"] == "dies"
    # Identity, not just the name: the queue must point at the CURRENT object
    # graph, or the drain would resolve against a detached copy.
    assert entry["source_card"] is restored.players[0].battlefield[0]


def test_rebuild_is_idempotent(game, make_card):
    """A second rebuild must not double-queue — restore runs more than once."""
    source = _artist(game, make_card)
    coord = ResolutionCoordinator.for_game(None, game)
    _queue_trigger(coord, game, source)

    restored = _reload(game)
    rebuilt = ResolutionCoordinator.for_game(None, restored)
    rebuilt.rebuild_pending_queues()
    rebuilt.rebuild_pending_queues()
    assert len(restored.pending_async_triggers) == 1


def test_a_dispatched_trigger_never_comes_back(game, make_card):
    """The duplication half: claimed work must not be replayed."""
    source = _artist(game, make_card)
    coord = ResolutionCoordinator.for_game(None, game)
    event = _queue_trigger(coord, game, source)
    assert coord.mark_event_dispatched(event.event_id) is True

    restored = _reload(game)
    ResolutionCoordinator.for_game(None, restored).rebuild_pending_queues()
    assert restored.pending_async_triggers == []


def test_claiming_twice_refuses_the_second(game, make_card):
    source = _artist(game, make_card)
    coord = ResolutionCoordinator.for_game(None, game)
    event = _queue_trigger(coord, game, source)
    assert coord.mark_event_dispatched(event.event_id) is True
    assert coord.mark_event_dispatched(event.event_id) is False


def test_an_unknown_event_id_is_allowed_through(game):
    """Undurable paths must keep working: no record means nothing to recover.

    Gating them would break live play for every trigger queued before a game
    had a coordinator.
    """
    coord = ResolutionCoordinator.for_game(None, game)
    assert coord.mark_event_dispatched("no-such-event") is True
    assert coord.mark_event_dispatched("") is True


# --------------------------------------------------------------------------
# Deaths
# --------------------------------------------------------------------------

def test_a_death_survives_through_the_real_choke_point(game, make_card):
    """Driven through queue_death, the ONE sanctioned appender — not by hand.

    A helper pinned only through direct calls is not pinned into production.
    """
    from mtg.triggers import queue_death
    ResolutionCoordinator.for_game(None, game)  # attach before the emit
    dead = make_card("Grizzly Bears")
    game.players[0].graveyard.append(dead)
    queue_death(game, dead, game.players[0])

    assert len(game._recently_died) == 1
    assert len(game.resolution_events) == 1

    restored = _reload(game)
    assert restored._recently_died == []
    ResolutionCoordinator.for_game(None, restored).rebuild_pending_queues()
    assert len(restored._recently_died) == 1
    card, owner = restored._recently_died[0]
    assert card.name == "Grizzly Bears"
    assert owner is restored.players[0]
    # The dead card lives in a graveyard, so a battlefield-only lookup would
    # have failed to rebind it.
    assert card is restored.players[0].graveyard[0]


def test_a_dispatched_death_is_not_restored(game, make_card):
    from mtg.triggers import queue_death
    coord = ResolutionCoordinator.for_game(None, game)
    dead = make_card("Grizzly Bears")
    game.players[0].graveyard.append(dead)
    queue_death(game, dead, game.players[0])
    event = coord.undispatched_events(kind="death")[0]
    coord.mark_event_dispatched(event.event_id)

    restored = _reload(game)
    ResolutionCoordinator.for_game(None, restored).rebuild_pending_queues()
    assert restored._recently_died == []


def test_the_dies_dispatcher_claims_the_wave(game, make_card, rules):
    """The engine's dispatcher sites must claim what they drain.

    Driven through GameEngine._mark_deaths_dispatched rather than the
    coordinator, so a mutant that unhooks the dispatcher is caught.
    """
    from mtg.engine import GameEngine
    coord = ResolutionCoordinator.for_game(None, game)
    dead = make_card("Grizzly Bears")
    game.players[0].graveyard.append(dead)
    from mtg.triggers import queue_death
    queue_death(game, dead, game.players[0])
    assert len(coord.undispatched_events(kind="death")) == 1

    engine = GameEngine.__new__(GameEngine)
    engine._mark_deaths_dispatched(game, [(dead, game.players[0])])
    assert coord.undispatched_events(kind="death") == []


def test_an_unrelated_death_is_not_claimed_by_another_wave(game, make_card):
    """Adverse control: claiming must be per-card, not per-call."""
    from mtg.engine import GameEngine
    from mtg.triggers import queue_death
    coord = ResolutionCoordinator.for_game(None, game)
    bear = make_card("Grizzly Bears")
    elk = make_card("Elk")
    for card in (bear, elk):
        game.players[0].graveyard.append(card)
        queue_death(game, card, game.players[0])
    assert len(coord.undispatched_events(kind="death")) == 2

    engine = GameEngine.__new__(GameEngine)
    engine._mark_deaths_dispatched(game, [(bear, game.players[0])])
    left = coord.undispatched_events(kind="death")
    assert [e.source_name for e in left] == ["Elk"]


# --------------------------------------------------------------------------
# Record identity and derivation
# --------------------------------------------------------------------------

def test_re_recording_the_same_event_does_not_mint_a_second(game, make_card):
    """Deterministic ids: a retried enqueue inside one process is one event."""
    source = _artist(game, make_card)
    coord = ResolutionCoordinator.for_game(None, game)
    first = coord.record_event("trigger", source, trigger_type="dies")
    second = coord.record_event("trigger", source, trigger_type="dies")
    assert first.event_id == second.event_id
    assert len(game.resolution_events) == 1


def test_a_new_occurrence_after_dispatch_gets_its_own_record(game, make_card):
    """The other direction, and the one that would silently swallow a trigger.

    Reusing a DISPATCHED record for a genuinely new occurrence of the same
    shape would hand it an "already done" mark and it would never fire.
    """
    source = _artist(game, make_card)
    coord = ResolutionCoordinator.for_game(None, game)
    first = coord.record_event("trigger", source, trigger_type="dies")
    coord.mark_event_dispatched(first.event_id)
    second = coord.record_event("trigger", source, trigger_type="dies")
    assert second.event_id != first.event_id
    assert second.dispatched is False
    assert len(coord.undispatched_events(kind="trigger")) == 1


def test_distinct_occurrence_keys_are_distinct_events(game, make_card):
    source = _artist(game, make_card)
    coord = ResolutionCoordinator.for_game(None, game)
    a = coord.record_event("trigger", source, trigger_type="cast",
                           occurrence_key="cast-1")
    b = coord.record_event("trigger", source, trigger_type="cast",
                           occurrence_key="cast-2")
    assert a.event_id != b.event_id
    assert len(coord.undispatched_events(kind="trigger")) == 2


def test_a_missing_record_counts_as_dispatched(game, make_card):
    """Slice-3's rule, restated for events: the record OUTLIVES the dispatch.

    Its absence therefore means the bounded prune reclaimed it, not that the
    event is still waiting — treating a gap as pending would strand a job.
    """
    source = _artist(game, make_card)
    coord = ResolutionCoordinator.for_game(None, game)
    event = coord.record_event("trigger", source, trigger_type="dies")
    game.resolution_events.pop(event.event_id)
    assert coord.undispatched_events() == []


def test_events_link_to_the_resolution_that_created_them(game, make_card):
    """The job link is what lets recovery ask "what did THIS resolution owe?"."""
    source = _artist(game, make_card)
    card = make_card("Contrived Bolt", type_line="Instant", power=None,
                     toughness=None)
    entry = StackEntry(card=card, controller_name=game.players[0].name,
                       controller_index=0, target=None)
    coord = ResolutionCoordinator.for_game(None, game)
    job = coord.register(entry)
    game._active_resolution_job_id = job.job_id
    try:
        event = coord.record_event("trigger", source, trigger_type="etb")
    finally:
        game._active_resolution_job_id = None

    assert event.job_id == job.job_id
    assert job.trigger_event_ids == [event.event_id]
    assert [e.event_id for e in coord.undispatched_events(subject=job)] == [
        event.event_id]

    restored = _reload(game)
    assert restored.resolution_jobs[job.job_id].trigger_event_ids == [
        event.event_id]


def test_events_from_another_resolution_are_not_attributed(game, make_card):
    """Adverse control for the subject filter."""
    source = _artist(game, make_card)
    card = make_card("Contrived Bolt", type_line="Instant", power=None,
                     toughness=None)
    entry = StackEntry(card=card, controller_name=game.players[0].name,
                       controller_index=0, target=None)
    coord = ResolutionCoordinator.for_game(None, game)
    job = coord.register(entry)
    loose = coord.record_event("trigger", source, trigger_type="etb")
    assert loose.job_id == ""
    assert coord.undispatched_events(subject=job) == []


# --------------------------------------------------------------------------
# Boundaries
# --------------------------------------------------------------------------

def test_an_orphaned_source_is_left_pending_not_dropped(game, make_card):
    """A source that vanished must stay visible, not be silently discarded.

    Dropping it is the exact failure this slice removes.
    """
    source = _artist(game, make_card)
    coord = ResolutionCoordinator.for_game(None, game)
    _queue_trigger(coord, game, source)
    game.players[0].battlefield.clear()

    restored = _reload(game)
    rebuilt = ResolutionCoordinator.for_game(None, restored)
    assert rebuilt.rebuild_pending_queues() == 0
    assert restored.pending_async_triggers == []
    assert len(rebuilt.undispatched_events(kind="trigger")) == 1


def test_pruning_never_reclaims_undispatched_records(game, make_card):
    """The omission half of the contract, mirrored from the narration outbox."""
    source = _artist(game, make_card)
    coord = ResolutionCoordinator.for_game(None, game)
    keep = coord.EVENTS_DISPATCHED_KEEP
    for i in range(keep + 20):
        event = coord.record_event("trigger", source, trigger_type="etb",
                                   occurrence_key="done-%d" % i)
        coord.mark_event_dispatched(event.event_id)
    pending = coord.record_event("trigger", source, trigger_type="etb",
                                 occurrence_key="still-waiting")
    coord.prune_resolution_events()

    assert pending.event_id in game.resolution_events
    dispatched = [e for e in game.resolution_events.values() if e.dispatched]
    assert len(dispatched) == keep


def test_attached_prefers_the_live_coordinator(game):
    """Sync producers must not downgrade durability.

    for_game() REPLACES the cached instance when the engine differs, and
    _persist() no-ops unless the engine has save_game() — so a subscriber that
    called for_game() with whatever engine it could reach would both clobber
    the real coordinator and silently stop writing to disk.
    """
    class _Engine:
        def save_game(self, _game):
            self.saved = True

    engine = _Engine()
    live = ResolutionCoordinator.for_game(engine, game)
    assert ResolutionCoordinator.attached(game) is live
    assert ResolutionCoordinator.attached(game).engine is engine


def test_attached_falls_back_when_nothing_is_bound(game):
    assert getattr(game, "_resolution_coordinator", None) is None
    coord = ResolutionCoordinator.attached(game)
    assert isinstance(coord, ResolutionCoordinator)
    assert coord.game is game


def test_restore_rebuilds_through_bind_restored_stack(game, make_card):
    """The production restore hook must call the rebuild, not just expose it."""
    source = _artist(game, make_card)
    coord = ResolutionCoordinator.for_game(None, game)
    _queue_trigger(coord, game, source)

    restored = _reload(game)
    assert restored.pending_async_triggers == []
    ResolutionCoordinator.for_game(None, restored).bind_restored_stack()
    assert len(restored.pending_async_triggers) == 1


def test_event_records_round_trip_exactly(game, make_card):
    source = _artist(game, make_card)
    coord = ResolutionCoordinator.for_game(None, game)
    event = coord.record_event(
        "trigger", source, controller_name="Rick", trigger_text="drain 1",
        trigger_type="dies", context="a bear died", occurrence_key="k1")
    clone = ResolutionEvent.from_dict(json.loads(json.dumps(event.to_dict())))
    assert clone.to_dict() == event.to_dict()


def test_a_reclaimed_id_is_never_reissued_over_a_live_record(game, make_card):
    """Regression: a derived-from-length id cannot live in a pruned store.

    Reproduced before the fix — base present and dispatched, a dispatched
    middle suffix reclaimed, len() lands back on a LIVE undispatched id, and
    the next record OVERWRITES it, destroying a pending trigger silently. That
    is the failure this whole slice exists to remove, and it is verbatim the
    incompatibility the narration outbox already hit between an id derived
    from a collection's length and a collection that gets pruned.
    """
    source = _artist(game, make_card)
    coord = ResolutionCoordinator.for_game(None, game)

    base = coord.record_event("trigger", source, trigger_type="dies")
    coord.mark_event_dispatched(base.event_id)
    middle = coord.record_event("trigger", source, trigger_type="dies")
    coord.mark_event_dispatched(middle.event_id)
    pending = coord.record_event("trigger", source, trigger_type="dies")
    assert len({base.event_id, middle.event_id, pending.event_id}) == 3

    # The prune reclaims the dispatched middle record.
    game.resolution_events.pop(middle.event_id)

    fresh = coord.record_event("trigger", source, trigger_type="dies")
    assert fresh.event_id != pending.event_id
    assert game.resolution_events[pending.event_id] is pending
    assert pending.dispatched is False
    assert sorted(e.event_id for e in coord.undispatched_events()) == sorted(
        [pending.event_id, fresh.event_id])


def test_pruning_drops_reclaimed_ids_from_job_links(game, make_card):
    """A dangling link could re-attribute a REISSUED id to the wrong job.

    Once a record is gone its id in a job's list names nothing, so it carries
    no audit value — and leaving it lets a later suffix reuse point an
    unrelated event at that job.
    """
    source = _artist(game, make_card)
    card = make_card("Contrived Bolt", type_line="Instant", power=None,
                     toughness=None)
    entry = StackEntry(card=card, controller_name=game.players[0].name,
                       controller_index=0, target=None)
    coord = ResolutionCoordinator.for_game(None, game)
    job = coord.register(entry)
    game._active_resolution_job_id = job.job_id
    try:
        keep = coord.EVENTS_DISPATCHED_KEEP
        for i in range(keep + 5):
            event = coord.record_event("trigger", source, trigger_type="etb",
                                       occurrence_key="k-%d" % i)
            coord.mark_event_dispatched(event.event_id)
    finally:
        game._active_resolution_job_id = None

    assert len(job.trigger_event_ids) == keep + 5
    coord.prune_resolution_events()
    assert all(eid in game.resolution_events for eid in job.trigger_event_ids)
    assert len(job.trigger_event_ids) == keep


# --------------------------------------------------------------------------
# Production wiring — the helpers must be REACHED, not merely callable
# --------------------------------------------------------------------------

class _StubEngine:
    """Only what drain_pending_triggers and the SBA dispatcher actually touch."""

    def __init__(self, rules):
        self.rules = rules
        self.client = None
        self.saves = 0

    def save_game(self, _game):
        self.saves += 1


def test_the_drain_claims_the_event_it_resolves(game, make_card, rules):
    """Driven through drain_pending_triggers, not through the coordinator.

    A mutant that unhooks the claim from the drain leaves every coordinator
    test passing, so this is the pin that actually holds the wiring.
    """
    import asyncio
    from mtg.triggers import drain_pending_triggers

    source = _artist(game, make_card)
    engine = _StubEngine(rules)
    coord = ResolutionCoordinator.for_game(engine, game)
    event = _queue_trigger(coord, game, source)
    assert event.dispatched is False

    asyncio.run(drain_pending_triggers(engine, game))
    assert event.dispatched is True, "the drain must claim what it resolves"


def test_the_drain_skips_an_already_claimed_event(game, make_card, rules):
    """The recovery case: a previous process owned this trigger.

    Adverse control for the pin above — without the guard the drain would
    resolve it a second time, and a drained trigger goes through Tier 3, which
    mints a fresh plan every call.
    """
    import asyncio
    from mtg.triggers import drain_pending_triggers

    source = _artist(game, make_card)
    engine = _StubEngine(rules)
    coord = ResolutionCoordinator.for_game(engine, game)
    event = _queue_trigger(coord, game, source)
    coord.mark_event_dispatched(event.event_id)

    messages = asyncio.run(drain_pending_triggers(engine, game))
    assert not any("Blood Artist" in m for m in messages), (
        "an already-claimed trigger must not resolve again")


def test_check_state_based_actions_claims_the_death_wave(game, make_card, rules):
    """The dies dispatcher must reach _mark_deaths_dispatched in production.

    A structural pin here would be dodgeable (an import alias, a renamed
    call); this drives the real SBA entry point with a creature that actually
    dies, so the wiring is what is under test.
    """
    from mtg.engine import GameEngine

    engine = GameEngine.__new__(GameEngine)
    engine.rules = rules
    engine.client = None
    coord = ResolutionCoordinator.for_game(None, game)

    doomed = make_card("Doomed Bear", power="2", toughness="2")
    doomed.damage_marked = 5
    game.players[0].battlefield.append(doomed)

    engine.check_state_based_actions(game)

    assert doomed in game.players[0].graveyard, "fixture must actually kill it"
    events = [e for e in game.resolution_events.values() if e.kind == "death"]
    assert events, "the death must have been recorded"
    assert all(e.dispatched for e in events), (
        "the dispatcher must claim the wave it drains")


def test_the_queue_line_carries_the_durable_event_id(game, make_card, rules,
                                                     capsys):
    """A successful record writes no line of its own.

    So without this the only live evidence would be the ABSENCE of an error,
    and a silently-failed record would look identical to a working one. The
    [QUEUE-*] line already fires once per queued trigger, so it carries the id
    — an empty one is then visible in any batch log.

    Found as the single survivor of the mutation sweep: everything else in
    slice 4 was decisive, this was decoration until it was pinned.
    """
    from mtg.engine import GameEngine

    engine = GameEngine.__new__(GameEngine)
    engine.rules = rules
    engine.client = None
    source = _artist(game, make_card)
    ResolutionCoordinator.for_game(engine, game)

    assert engine._queue_async_trigger(
        game, source, "target player loses 1 life", "dies", "Rick") is True

    line = [ln for ln in capsys.readouterr().out.splitlines()
            if ln.startswith("[QUEUE-DIES]")]
    assert line, "the queue line must still be emitted"
    event_id = game.pending_async_triggers[0]["event_id"]
    assert event_id, "the trigger must have a durable record"
    assert ("event=%s" % event_id) in line[0], (
        "the queue line must name the record, so a failed one is visible")
    assert "event=NONE" not in line[0]
