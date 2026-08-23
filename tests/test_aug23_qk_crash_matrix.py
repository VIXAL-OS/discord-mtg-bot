"""Q-J slice 2 (narration outbox) and the Q-K crash matrix.

Q-K asks for deterministic process-death injection around every checkpoint,
with the recovered state compared exactly against an uninterrupted run. That
was untestable before Q-J slice 1: with no persisted plan there was nothing
to resume, so "recovery" could only ever mean "reload the display state".

The crash here is deliberately the HARSHEST honest simulation available in
process: serialize the game, throw the live object away entirely, rebuild it
from the bytes, and finish from the persisted plan alone. Nothing in-memory
survives the boundary — which is the whole point, since a real process death
keeps nothing.

WHAT THIS DOES NOT CLAIM: it does not kill an OS process, and it does not
cover a death INSIDE a single action's mutation (that window is what the
at-most-once claim ordering exists to bound, and losing one action there is
the documented, deliberate direction). Those stay for a real out-of-process
harness and the pilot.
"""
import json

import pytest

from mtg.models import GameState, NarrationEntry, StackEntry
from mtg.resolution import RESOLUTION_CHECKPOINTS, ResolutionCoordinator


# --------------------------------------------------------------------------
# Harness
# --------------------------------------------------------------------------

PLAN = [
    {"action": "deal_damage", "amount": 3, "target_player": "Claude"},
    {"action": "gain_life", "player": "Rick", "amount": 2},
    {"action": "deal_damage", "amount": 1, "target_player": "Claude"},
    {"action": "gain_life", "player": "Rick", "amount": 5},
]


def _register(game, make_card, name="Contrived Bolt"):
    card = make_card(name, mana_cost="{R}", type_line="Instant",
                     oracle_text="Deals damage and gains life.",
                     power=None, toughness=None)
    game.players[0].battlefield.append(card)
    entry = StackEntry(card=card, controller_name=game.players[0].name,
                       controller_index=0, target=None)
    coord = ResolutionCoordinator.for_game(None, game)
    return coord, coord.register(entry)


def _snapshot(game):
    """The observable state a player could actually check."""
    return {
        "life": [p.life for p in game.players],
        "hand": [len(p.hand) for p in game.players],
        "battlefield": [sorted(c.name for c in p.battlefield)
                        for p in game.players],
        "graveyard": [sorted(c.name for c in p.graveyard)
                      for p in game.players],
    }


def _reload(game):
    """The crash: nothing in memory survives, only the serialized bytes."""
    return GameState.from_dict(json.loads(json.dumps(game.to_dict())))


# --------------------------------------------------------------------------
# Q-K — crash at each checkpoint, compare against an uninterrupted run
# --------------------------------------------------------------------------

class TestCrashMatrix:

    def _uninterrupted(self, rules, make_game, make_card):
        game = make_game()
        coord, job = _register(game, make_card)
        coord.record_plan(job, PLAN, tier="tier3")
        coord.transition(job, "resolving")
        coord.resume_job(job, rules)
        return _snapshot(game)

    @pytest.mark.parametrize("applied", [0, 1, 2, 3, 4])
    def test_crash_after_n_actions_recovers_identically(
            self, rules, make_game, make_card, applied):
        """Death after each possible number of applied actions. The recovered
        state must equal the uninterrupted one exactly — no action dropped,
        none applied twice."""
        expected = self._uninterrupted(rules, make_game, make_card)

        game = make_game()
        coord, job = _register(game, make_card)
        coord.record_plan(job, PLAN, tier="tier3")
        coord.transition(job, "resolving")

        # Partial progress, exactly as the live loop would make it.
        from mtg.actions import execute_action_on_state
        for i in range(applied):
            should, _ = coord.claim_action(job, i, PLAN[i])
            assert should
            execute_action_on_state(rules, game, PLAN[i])

        # --- process death ---
        restored = _reload(game)
        rcoord = ResolutionCoordinator.for_game(None, restored)
        rcoord.resume_job(job.job_id, rules)

        assert _snapshot(restored) == expected, (
            "crash after %d action(s) did not recover exactly" % applied)

    @pytest.mark.parametrize("checkpoint", list(RESOLUTION_CHECKPOINTS))
    def test_every_checkpoint_survives_a_reload(
            self, make_game, make_card, checkpoint):
        """A job at ANY checkpoint must round-trip with its identity, plan
        and ledger intact — the precondition for resuming from it."""
        game = make_game()
        coord, job = _register(game, make_card)
        coord.record_plan(job, PLAN)
        coord.claim_action(job, 0, PLAN[0])
        if RESOLUTION_CHECKPOINTS.index(checkpoint) >= 1:
            coord.transition(job, checkpoint)

        restored = _reload(game)
        rjob = restored.resolution_jobs[job.job_id]

        assert rjob.checkpoint == job.checkpoint
        assert rjob.planned_actions == PLAN
        assert rjob.applied_action_keys == job.applied_action_keys

    def test_a_double_reload_still_applies_each_action_once(
            self, rules, make_game, make_card):
        """Repeated crashes — the shape a crash loop would actually take."""
        expected = self._uninterrupted(rules, make_game, make_card)

        game = make_game()
        coord, job = _register(game, make_card)
        coord.record_plan(job, PLAN)
        coord.transition(job, "resolving")

        from mtg.actions import execute_action_on_state
        should, _ = coord.claim_action(job, 0, PLAN[0])
        execute_action_on_state(rules, game, PLAN[0])

        first = _reload(game)
        fcoord = ResolutionCoordinator.for_game(None, first)
        # Die again after one more action.
        for index, action in fcoord.pending_actions(job.job_id)[:1]:
            fcoord.claim_action(job.job_id, index, action)
            execute_action_on_state(rules, first, action)

        second = _reload(first)
        scoord = ResolutionCoordinator.for_game(None, second)
        scoord.resume_job(job.job_id, rules)

        assert _snapshot(second) == expected

    def test_resuming_a_finished_job_changes_nothing(
            self, rules, make_game, make_card):
        """ADVERSE CONTROL — the duplication half. Resuming an already
        complete resolution must be inert, not a second application."""
        game = make_game()
        coord, job = _register(game, make_card)
        coord.record_plan(job, PLAN)
        coord.transition(job, "resolving")
        coord.resume_job(job, rules)
        done = _snapshot(game)

        coord.resume_job(job, rules)

        assert _snapshot(game) == done

    def test_a_job_with_no_plan_refuses_to_resume(
            self, rules, make_game, make_card):
        """ADVERSE CONTROL — re-deriving would mean re-querying Tier 3, so a
        planless job must produce NOTHING rather than a guess."""
        game = make_game()
        coord, job = _register(game, make_card)
        coord.transition(job, "resolving")
        before = _snapshot(game)

        assert coord.resume_job(job, rules) == []
        assert _snapshot(game) == before


# --------------------------------------------------------------------------
# Q-J slice 2 — the narration outbox
# --------------------------------------------------------------------------

class TestNarrationOutbox:

    def test_a_line_is_recorded_before_it_is_sent(self, game, make_card):
        coord, job = _register(game, make_card)
        entry = coord.enqueue_narration("Bolt deals 3", job_id=job.job_id)

        assert entry.sent is False, \
            "enqueue must precede the send — that is the omission guard"
        assert entry in game.narration_outbox

    def test_the_ack_marks_it_delivered(self, game, make_card):
        coord, job = _register(game, make_card)
        entry = coord.enqueue_narration("Bolt deals 3", job_id=job.job_id)
        coord.mark_narration_sent(entry.message_id)

        assert game.narration_outbox[0].sent is True
        assert coord.unsent_narration() == []

    def test_an_unsent_line_survives_a_crash(self, game, make_card):
        """The omission half: a line whose send never completed is still on
        disk, so recovery can deliver it."""
        coord, job = _register(game, make_card)
        coord.enqueue_narration("Bolt deals 3", job_id=job.job_id)

        restored = _reload(game)
        rcoord = ResolutionCoordinator.for_game(None, restored)

        pending = rcoord.unsent_narration()
        assert [e.content for e in pending] == ["Bolt deals 3"]

    def test_a_sent_line_is_not_resent_after_a_crash(self, game, make_card):
        """The duplication half."""
        coord, job = _register(game, make_card)
        entry = coord.enqueue_narration("Bolt deals 3", job_id=job.job_id)
        coord.mark_narration_sent(entry.message_id)

        restored = _reload(game)
        rcoord = ResolutionCoordinator.for_game(None, restored)

        assert rcoord.unsent_narration() == []
        assert rcoord.already_sent("Bolt deals 3", job_id=job.job_id)

    def test_ids_are_deterministic_per_scope_and_position(
            self, game, make_card):
        """A replayed resolution re-derives the SAME ids, which is what lets
        it recognise its own delivered lines instead of minting new ones."""
        coord, job = _register(game, make_card)
        first = coord.enqueue_narration("line A", job_id=job.job_id)
        second = coord.enqueue_narration("line B", job_id=job.job_id)

        assert first.message_id == "%s:n0" % job.job_id
        assert second.message_id == "%s:n1" % job.job_id

    def test_two_identical_live_lines_are_two_entries(self, game, make_card):
        """Written first as a dedupe-by-content pin, which was the WRONG
        property: the same trigger firing twice produces the same line twice,
        and collapsing them would drop narration. Duplication across a
        restart is the ack's job, not enqueue's."""
        coord, job = _register(game, make_card)
        a = coord.enqueue_narration("line A", job_id=job.job_id)
        b = coord.enqueue_narration("line A", job_id=job.job_id)

        assert a is not b
        assert a.message_id != b.message_id
        assert len(game.narration_outbox) == 2

    def test_ids_stay_unique_after_the_sent_window_is_pruned(
            self, game, make_card):
        """Pruning shrinks the outbox, so an index derived from its LENGTH
        would re-issue a live id and the next ack would mark the wrong line.
        Ids come from the highest issued index instead."""
        coord, _ = _register(game, make_card)
        keep = ResolutionCoordinator.NARRATION_SENT_KEEP

        for i in range(keep + 10):
            entry = coord.enqueue_narration("sent %d" % i)
            coord.mark_narration_sent(entry.message_id)

        ids = [e.message_id for e in game.narration_outbox]
        assert len(ids) == len(set(ids)), "id collision after pruning"

    def test_two_jobs_keep_separate_scopes(self, game, make_card):
        coord, job_a = _register(game, make_card, "Bolt A")
        _, job_b = _register(game, make_card, "Bolt B")

        a = coord.enqueue_narration("same text", job_id=job_a.job_id)
        b = coord.enqueue_narration("same text", job_id=job_b.job_id)

        assert a.message_id != b.message_id
        assert len(game.narration_outbox) == 2

    def test_loose_lines_get_their_own_scope(self, game, make_card):
        coord, _ = _register(game, make_card)
        entry = coord.enqueue_narration("a turn banner")
        assert entry.message_id.startswith("loose:")

    def test_the_outbox_is_bounded_but_never_drops_unsent(
            self, game, make_card):
        """The outbox rides in every save, so sent entries are only a dedupe
        window. Unsent ones are the omission contract and are never pruned."""
        coord, _ = _register(game, make_card)
        keep = ResolutionCoordinator.NARRATION_SENT_KEEP

        for i in range(keep + 25):
            entry = coord.enqueue_narration("sent %d" % i)
            coord.mark_narration_sent(entry.message_id)
        coord.enqueue_narration("never delivered")

        sent = [e for e in game.narration_outbox if e.sent]
        unsent = [e for e in game.narration_outbox if not e.sent]
        assert len(sent) <= keep, "the sent window must stay bounded"
        assert [e.content for e in unsent] == ["never delivered"]

    def test_entries_round_trip_through_the_game_snapshot(
            self, game, make_card):
        coord, job = _register(game, make_card)
        sent = coord.enqueue_narration("delivered", job_id=job.job_id)
        coord.mark_narration_sent(sent.message_id)
        coord.enqueue_narration("not delivered", job_id=job.job_id)

        restored = _reload(game)

        assert [(e.content, e.sent) for e in restored.narration_outbox] == [
            ("delivered", True), ("not delivered", False)]
        assert all(isinstance(e, NarrationEntry)
                   for e in restored.narration_outbox)


# --------------------------------------------------------------------------
# Q-J slice 3 — a resolution blocked on a private choice
# --------------------------------------------------------------------------

class TestChoiceBlockedJobs:
    """Resuming past an unanswered private choice would silently make that
    choice FOR the player — a worse failure than not resuming at all.

    The job<->choice link is APPEND-ONLY and "unresolved" is derived from the
    record's own `complete` flag. A maintained removal list would have to
    catch submit, timeout, cancellation and seat-elimination, and a single
    missed path would strand the job forever.
    """

    def _open_choice(self, game, job):
        """Create a real choice while `job` is the active resolution."""
        import asyncio
        from mtg.choices import create_choice

        async def go():
            game._active_resolution_job_id = job.job_id
            try:
                return create_choice(
                    game, choice_type="order_replacements",
                    chooser_indices=[0],
                    options_by_player=["first", "second"])
            finally:
                game._active_resolution_job_id = None
        return asyncio.run(go())

    def test_a_choice_records_the_job_that_opened_it(
            self, make_game, make_card):
        game = make_game()
        coord, job = _register(game, make_card)
        record = self._open_choice(game, job)

        assert record["owning_job"] == job.job_id
        assert record["choice_id"] in job.unresolved_choice_ids

    def test_an_open_choice_makes_the_job_unreplayable(
            self, make_game, make_card):
        game = make_game()
        coord, job = _register(game, make_card)
        coord.record_plan(job, PLAN)
        coord.transition(job, "resolving")
        assert job in coord.replayable_jobs()

        self._open_choice(game, job)

        assert coord.unresolved_choices(job)
        assert job not in coord.replayable_jobs()

    def test_resume_refuses_while_a_choice_is_open(
            self, rules, make_game, make_card):
        """The property that matters: no action is applied, so the player's
        choice is still theirs to make."""
        game = make_game()
        coord, job = _register(game, make_card)
        coord.record_plan(job, PLAN)
        coord.transition(job, "resolving")
        self._open_choice(game, job)
        before = _snapshot(game)

        assert coord.resume_job(job, rules) == []
        assert _snapshot(game) == before

    def test_answering_the_choice_unblocks_the_job(
            self, rules, make_game, make_card):
        """ADVERSE CONTROL — the gate must open again, not latch shut."""
        game = make_game()
        coord, job = _register(game, make_card)
        coord.record_plan(job, PLAN)
        coord.transition(job, "resolving")
        record = self._open_choice(game, job)

        record["complete"] = True

        assert coord.unresolved_choices(job) == []
        assert job in coord.replayable_jobs()
        assert coord.resume_job(job, rules)

    def test_a_vanished_record_counts_as_resolved(
            self, make_game, make_card):
        """The record outlives the answer, so its ABSENCE means the choice was
        cleaned up — not that it is still waiting. Treating a missing record
        as unresolved would strand the job permanently."""
        game = make_game()
        coord, job = _register(game, make_card)
        record = self._open_choice(game, job)
        del game.pending_choices[record["choice_id"]]

        assert coord.unresolved_choices(job) == []

    def test_a_choice_with_no_active_resolution_links_to_nothing(
            self, make_game, make_card):
        """Most choices are not opened inside a durable resolution; those must
        keep working untouched."""
        import asyncio
        from mtg.choices import create_choice
        game = make_game()
        coord, job = _register(game, make_card)

        async def go():
            game._active_resolution_job_id = None
            return create_choice(game, choice_type="generic",
                                 chooser_indices=[0],
                                 options_by_player=["a", "b"])
        record = asyncio.run(go())

        assert "owning_job" not in record
        assert job.unresolved_choice_ids == []

    def test_the_link_survives_a_crash(self, make_game, make_card):
        """A job blocked on a choice must STILL be blocked after a restart —
        otherwise recovery is exactly when the choice gets made for them."""
        game = make_game()
        coord, job = _register(game, make_card)
        coord.record_plan(job, PLAN)
        coord.transition(job, "resolving")
        self._open_choice(game, job)

        restored = _reload(game)
        rcoord = ResolutionCoordinator.for_game(None, restored)

        assert rcoord.unresolved_choices(job.job_id)
        assert restored.resolution_jobs[job.job_id] not in rcoord.replayable_jobs()


# --------------------------------------------------------------------------
# Q-K — crash across a NESTED stack (counter-wars), not one job at a time
# --------------------------------------------------------------------------

class TestNestedStackCrash:
    """Counter-wars were pinned per-job; the STACK was not.

    Every test above builds ONE resolution. A real counter-war has several
    jobs in flight at different checkpoints, and the property that matters is
    ordering: recovery must not finish a spell while the counter aimed at it
    is still above it. The live path grew [STACK-LIFO-GUARD] for exactly that
    race in July; the recovery path had no equivalent, which this class found.
    """

    def _war(self, game, make_card):
        """Beast Whisperer, then Arcane Denial cast at it (LIFO: Denial first)."""
        coord = ResolutionCoordinator.for_game(None, game)
        jobs = []
        for name, controller in [("Beast Whisperer", 0), ("Arcane Denial", 1)]:
            card = make_card(name, type_line="Instant", power=None,
                             toughness=None)
            entry = StackEntry(card=card,
                               controller_name=game.players[controller].name,
                               controller_index=controller, target=None)
            game.stack.append(entry)
            jobs.append(coord.register(entry))
        return coord, jobs

    def test_a_buried_job_is_not_offered_for_replay(self, make_game, make_card):
        """THE FINDING. Resuming it resolves a spell out of order (CR 608).

        Reproduced before the guard existed: replayable_jobs() returned the
        buried Beast Whisperer while the Arcane Denial targeting it was still
        above it on the stack.
        """
        game = make_game()
        coord, (buried, _top) = self._war(game, make_card)
        coord.record_plan(buried, PLAN, tier="tier3")
        coord.transition(buried, "resolving")

        restored = _reload(game)
        rcoord = ResolutionCoordinator.for_game(None, restored)
        rcoord.bind_restored_stack()

        assert rcoord.is_buried(buried.job_id) is True
        assert buried.job_id not in [j.job_id for j in rcoord.replayable_jobs()]

    def test_resume_refuses_a_buried_job(self, rules, make_game, make_card):
        """The gate lives in resume_job too, so a direct caller cannot bypass
        replayable_jobs() — the same reasoning as the slice-3 choice gate."""
        game = make_game()
        coord, (buried, _top) = self._war(game, make_card)
        coord.record_plan(buried, PLAN, tier="tier3")
        coord.transition(buried, "resolving")
        before = _snapshot(game)

        assert coord.resume_job(buried, rules) == []
        assert _snapshot(game) == before, "no effect may be applied"

    def test_the_top_of_the_stack_is_not_buried(self, make_game, make_card):
        """Adverse control: the guard must not refuse the job that IS next."""
        game = make_game()
        coord, (_buried, top) = self._war(game, make_card)
        coord.record_plan(top, PLAN, tier="tier3")
        coord.transition(top, "resolving")

        restored = _reload(game)
        rcoord = ResolutionCoordinator.for_game(None, restored)
        rcoord.bind_restored_stack()

        assert rcoord.is_buried(top.job_id) is False
        assert top.job_id in [j.job_id for j in rcoord.replayable_jobs()]

    def test_an_ordinary_resolution_owns_no_stack_entry_and_replays(
            self, rules, make_game, make_card):
        """The control that keeps the guard from breaking all recovery.

        The live cast path pops the entry BEFORE the resolving transition
        ("Pop from stack before resolving", mtg/spells.py), so an ordinary
        in-flight job has no entry at all. If this ever fails, the guard has
        started refusing normal recovery rather than the LIFO race.
        """
        game = make_game()
        coord, job = _register(game, make_card)
        coord.record_plan(job, PLAN, tier="tier3")
        coord.transition(job, "resolving")
        assert coord.is_buried(job) is False

        restored = _reload(game)
        rcoord = ResolutionCoordinator.for_game(None, restored)
        rcoord.resume_job(job.job_id, rules)
        assert _snapshot(restored)["life"] == [40 + 7, 40 - 4]

    def test_the_guard_releases_once_the_entry_above_resolves(
            self, rules, make_game, make_card):
        """A refusal must be temporary, not a permanent strand."""
        game = make_game()
        coord, (buried, top) = self._war(game, make_card)
        coord.record_plan(buried, PLAN, tier="tier3")
        coord.transition(buried, "resolving")
        assert coord.resume_job(buried, rules) == []

        # The counter above resolves and is popped, as the cast path pops it.
        game.stack = [e for e in game.stack
                      if e.resolution_job_id != top.job_id]

        assert coord.is_buried(buried) is False
        assert coord.resume_job(buried, rules) != []
        assert _snapshot(game)["life"] == [40 + 7, 40 - 4]

    def test_the_whole_stack_round_trips_with_order_and_checkpoints(
            self, make_game, make_card):
        """Several jobs in flight at DIFFERENT checkpoints, one crash."""
        game = make_game()
        coord, (buried, top) = self._war(game, make_card)
        coord.record_plan(buried, PLAN, tier="tier3")
        coord.transition(buried, "resolving")
        coord.transition(top, "priority_open")

        restored = _reload(game)
        rcoord = ResolutionCoordinator.for_game(None, restored)
        rcoord.bind_restored_stack()

        assert [e.card.name for e in restored.stack] == [
            "Beast Whisperer", "Arcane Denial"], "LIFO order must survive"
        assert restored.resolution_jobs[buried.job_id].checkpoint == "resolving"
        assert restored.resolution_jobs[top.job_id].checkpoint == "priority_open"
        assert restored.resolution_jobs[buried.job_id].planned_actions == PLAN

    def test_a_countered_job_stays_countered(self, make_game, make_card):
        """The counter-war outcome itself must survive the crash."""
        game = make_game()
        coord, (buried, _top) = self._war(game, make_card)
        coord.transition(buried, "complete", countered=True)

        restored = _reload(game)
        rjob = restored.resolution_jobs[buried.job_id]
        assert rjob.countered is True
        assert rjob.job_id not in [
            j.job_id for j in
            ResolutionCoordinator.for_game(None, restored).replayable_jobs()]

    def test_slice4_events_survive_a_counter_war(self, make_game, make_card):
        """Slice 4 under the shape it was never exercised in.

        A trigger queued while a stack war is in flight must still be there
        after the crash, alongside jobs at different checkpoints.
        """
        game = make_game()
        coord, (buried, _top) = self._war(game, make_card)
        watcher = make_card(
            "Blood Artist", type_line="Creature — Vampire",
            oracle_text=("Whenever Blood Artist or another creature dies, "
                         "target player loses 1 life."))
        game.players[0].battlefield.append(watcher)
        event = coord.record_event(
            "trigger", watcher, controller_name="Rick",
            trigger_text="target player loses 1 life", trigger_type="dies")
        coord.transition(buried, "resolving")

        restored = _reload(game)
        rcoord = ResolutionCoordinator.for_game(None, restored)
        rcoord.bind_restored_stack()

        assert len(restored.pending_async_triggers) == 1
        assert restored.pending_async_triggers[0]["event_id"] == event.event_id
        assert [e.card.name for e in restored.stack] == [
            "Beast Whisperer", "Arcane Denial"]
