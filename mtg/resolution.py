"""Durable CR 608 resolution-job coordination.

Q-I moved ownership of stack identity and checkpoint persistence out of the
cast coroutine.  Q-J slice 1 adds the two halves that let a restored
``resolving`` job advance instead of merely being retained: the action plan
is persisted BEFORE any of it runs, and every action carries an idempotency
key so a replay applies only what has not been applied.

CHOSEN SEMANTIC — at-most-once, stated rather than implied.  A key is claimed
and persisted BEFORE its action executes, so a process death between the
claim and the mutation loses that one action rather than repeating it on
recovery.  That direction is deliberate: in Magic a doubled Lightning Bolt is
an illegal game state nobody can unwind, while a dropped one is visible and
fixable at the table (``!resolve`` / ``!fix``).  The alternative ordering
(execute, then mark) is at-least-once and would draw twice, deal damage
twice, or duplicate tokens — precisely what the Q-J requirements forbid.

The plan is also the reason recovery never re-queries Tier 3: a second call
can legitimately return a DIFFERENT plan, and applying half of one plan and
half of another is not a resolution of anything.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Any, Dict, List, Optional, Tuple, Union

from mtg.models import (Card, GameState, ResolutionEvent, ResolutionJob,
                        StackEntry)


RESOLUTION_CHECKPOINTS = (
    "costs_paid",
    "on_stack",
    "priority_open",
    "resolving",
    "effects_applied",
    "card_routed",
    "sbas_and_triggers_done",
    "complete",
)


def action_key(job_id: str, index: int, action: Dict) -> str:
    """Stable idempotency key for one action inside one resolution.

    Position AND content both participate.  Position alone would be enough
    while plans are never re-queried, but including a content digest means a
    plan that somehow differs from the persisted one cannot silently inherit
    the previous plan's "already applied" marks — the keys simply will not
    match, which fails loud instead of half-applying two different plans.
    """
    try:
        payload = json.dumps(action, sort_keys=True, default=str)
    except (TypeError, ValueError):
        payload = repr(action)
    digest = hashlib.sha1(payload.encode('utf-8')).hexdigest()[:12]
    return '%s:%d:%s' % (job_id, index, digest)


class ResolutionCoordinator:
    """Own stable resolution jobs and persist meaningful transitions."""

    def __init__(self, engine, game: GameState):
        self.engine = engine
        self.game = game

    @classmethod
    def attached(cls, game: GameState) -> "ResolutionCoordinator":
        """The coordinator already bound to this game, else a best-effort one.

        Sync producers (the CREATURE_DIED subscriber) have no GameEngine in
        scope. They must NOT call for_game() with whatever engine they can
        reach: that both replaces the cached instance and, because _persist()
        needs an engine with save_game(), silently downgrades every later
        write to a no-op. Reusing the attached instance keeps durability.
        """
        current = getattr(game, "_resolution_coordinator", None)
        if current is not None and current.game is game:
            return current
        return cls.for_game(getattr(game, "_rules_engine", None), game)

    @classmethod
    def for_game(cls, engine, game: GameState) -> "ResolutionCoordinator":
        current = getattr(game, "_resolution_coordinator", None)
        if (current is None or current.engine is not engine
                or current.game is not game):
            current = cls(engine, game)
            game._resolution_coordinator = current
        return current

    def _persist(self) -> None:
        # Autoplay games are disposable diagnostics and would turn every cast
        # into a full-deck disk write. Human games are the recovery contract.
        if (not getattr(self.game, "is_autoplay", False)
                and hasattr(self.engine, "save_game")):
            self.engine.save_game(self.game)

    def register(self, entry: StackEntry, *, additional_cost: int = 0
                 ) -> ResolutionJob:
        entry.target_refs = []
        raw_targets = (list(entry.target)
                       if isinstance(entry.target, (list, tuple))
                       else ([entry.target] if entry.target is not None else []))
        from mtg.models import ResolutionTargetRef
        for target in raw_targets:
            ref = ResolutionTargetRef.capture(self.game, target)
            if ref is not None:
                entry.target_refs.append(ref)
        job = ResolutionJob.capture(
            self.game, entry, checkpoint="on_stack",
            additional_cost=additional_cost)
        entry.resolution_job_id = job.job_id
        self.game.resolution_jobs[job.job_id] = job
        self._persist()
        return job

    def find(self, subject: Union[ResolutionJob, StackEntry, Card, str, None]
             ) -> Optional[ResolutionJob]:
        if isinstance(subject, ResolutionJob):
            return subject
        if isinstance(subject, StackEntry):
            return self.game.resolution_jobs.get(
                str(subject.resolution_job_id or subject.entry_id))
        if isinstance(subject, str):
            return self.game.resolution_jobs.get(subject)
        card_id = getattr(subject, "id", None)
        if not card_id:
            return None
        candidates = [
            job for job in self.game.resolution_jobs.values()
            if str(job.card_snapshot.get("id", "")) == str(card_id)
            and job.checkpoint != "complete"
        ]
        return candidates[-1] if candidates else None

    def transition(self, subject, checkpoint: str, *, persist: bool = True,
                   countered: Optional[bool] = None,
                   recovery_error: Optional[str] = None
                   ) -> Optional[ResolutionJob]:
        if checkpoint not in RESOLUTION_CHECKPOINTS:
            raise ValueError(f"unknown resolution checkpoint: {checkpoint}")
        job = self.find(subject)
        if job is None:
            return None
        old_index = RESOLUTION_CHECKPOINTS.index(job.checkpoint)
        new_index = RESOLUTION_CHECKPOINTS.index(checkpoint)
        if new_index < old_index:
            raise ValueError(
                f"resolution checkpoint regression: {job.checkpoint} -> {checkpoint}")
        job.checkpoint = checkpoint
        if countered is not None:
            job.countered = bool(countered)
        if recovery_error is not None:
            job.recovery_error = str(recovery_error)
        if persist:
            self._persist()
        return job

    def bind_restored_stack(self) -> None:
        """Rebuild runtime events and exact references after load/undo."""
        for entry in self.game.stack:
            entry.bind_persisted_targets(self.game)
            if entry.resolution_event is None:
                entry.resolution_event = asyncio.Event()
            job = self.find(entry)
            if job is not None:
                job.priority_id = entry.priority_id
        self.rebuild_pending_queues()

    async def wait_for_priority(self, entry: StackEntry, timeout: float) -> None:
        """Central owner for runtime priority waits.

        The caller still owns the pre-Q-J effect dispatch, but no cast path
        creates or waits on a private continuation primitive directly.
        """
        if entry.resolution_event is None:
            entry.resolution_event = asyncio.Event()
        await asyncio.wait_for(entry.resolution_event.wait(), timeout=timeout)

    def note_priority_completed(self, entry: StackEntry) -> None:
        """Durably advance a top job before waking any runtime continuation."""
        self.transition(entry, "resolving")

    # ---------------------------------------------------------------- Q-J
    def record_plan(self, subject, actions, *, tier: str = "") -> Optional[
            ResolutionJob]:
        """Persist an action plan BEFORE any of it executes.

        Returns the job, or None when this resolution has no durable job —
        trigger drains and manual activations legitimately have none, and the
        recovery contract only ever covered work that does.

        Re-recording an IDENTICAL plan is a no-op so a retry inside one
        process does not clear the applied ledger.  A genuinely different
        plan replaces the record and resets the ledger, because keys from the
        old plan can no longer describe the new one.
        """
        job = self.find(subject)
        if job is None:
            return None
        planned = [dict(a) for a in (actions or [])]
        if job.planned_actions == planned:
            return job
        job.planned_actions = planned
        job.applied_action_keys = []
        if tier:
            job.cast_facts = dict(job.cast_facts or {})
            job.cast_facts['plan_tier'] = tier
        print("[RESOLVE-PLAN] %s: persisted %d action(s)%s"
              % (job.job_id, len(planned), (" from %s" % tier) if tier else ""))
        self._persist()
        return job

    def claim_action(self, subject, index: int, action: Dict) -> Tuple[bool, str]:
        """Claim one action before executing it.

        Returns ``(should_apply, key)``.  ``should_apply`` is False when the
        key is already in the ledger, which after a restart means "a previous
        process already owned this action" — see the at-most-once note at the
        top of this module.

        With no durable job the answer is always True with an empty key: an
        undurable resolution has nothing to recover from, so gating it would
        only break live play.
        """
        job = self.find(subject)
        if job is None:
            return True, ""
        key = action_key(job.job_id, index, action)
        if key in job.applied_action_keys:
            print("[RESOLVE-REPLAY] %s: action %d already applied — skipping"
                  % (job.job_id, index))
            return False, key
        job.applied_action_keys.append(key)
        self._persist()
        return True, key

    def pending_actions(self, subject) -> List[Tuple[int, Dict]]:
        """The persisted plan's not-yet-claimed actions, in order.

        This is what a recovered ``resolving`` job replays.  It never calls
        out to Tier 3.
        """
        job = self.find(subject)
        if job is None:
            return []
        out = []
        for index, action in enumerate(job.planned_actions or []):
            if action_key(job.job_id, index, action) not in job.applied_action_keys:
                out.append((index, dict(action)))
        return out

    # --------------------------------------------------- Q-J narration
    # Sent entries are kept only as a short dedupe window: the outbox rides
    # in every save, so an unbounded one would grow the snapshot for the
    # length of the game. Unsent entries are NEVER pruned — they are the
    # omission half of the contract.
    NARRATION_SENT_KEEP = 50

    def enqueue_narration(self, content: str, *, job_id: Optional[str] = None
                          ) -> "NarrationEntry":
        """Record a user-visible line BEFORE it is sent.

        The id is deterministic per (scope, position), so a resolution
        replayed after a crash re-derives the same ids and recognises its
        own already-delivered lines instead of minting duplicates.
        """
        from mtg.models import NarrationEntry
        scope = str(job_id or "loose")
        # Derive the next index from the HIGHEST id already issued in this
        # scope, never from the list length: prune_narration() drops old sent
        # entries, and a length-derived index would then re-issue an id that
        # is still live, so mark_narration_sent() would ack the wrong line.
        highest = -1
        for entry in self.game.narration_outbox:
            if entry.scope != scope:
                continue
            _, _, suffix = entry.message_id.rpartition(":n")
            if suffix.isdigit():
                highest = max(highest, int(suffix))
        message_id = "%s:n%d" % (scope, highest + 1)
        # Deliberately NOT deduped by content. Two identical lines in one
        # resolution are two real events (the same trigger firing twice), and
        # collapsing them would DROP narration. Duplication across a restart
        # is prevented by the ack instead: recovery re-sends only
        # unsent_narration(), and a resumed job re-runs only unclaimed
        # actions, so it never re-emits a line it already produced.
        entry = NarrationEntry(message_id=message_id, content=str(content),
                               sent=False, scope=scope)
        self.game.narration_outbox.append(entry)
        self._persist()
        return entry

    def mark_narration_sent(self, message_id: str) -> None:
        for entry in self.game.narration_outbox:
            if entry.message_id == message_id:
                if not entry.sent:
                    entry.sent = True
                    self.prune_narration()
                    self._persist()
                return

    def unsent_narration(self):
        """Lines this process (or a dead one) enqueued but never delivered."""
        return [e for e in self.game.narration_outbox if not e.sent]

    def already_sent(self, content: str, *, job_id: Optional[str] = None
                     ) -> bool:
        scope = str(job_id or "loose")
        return any(e.sent and e.scope == scope and e.content == content
                   for e in self.game.narration_outbox)

    def prune_narration(self) -> None:
        """Bound the outbox: keep every unsent entry, and only a recent
        window of sent ones (the dedupe horizon)."""
        outbox = self.game.narration_outbox
        sent = [e for e in outbox if e.sent]
        if len(sent) <= self.NARRATION_SENT_KEEP:
            return
        drop = set(id(e) for e in sent[:len(sent) - self.NARRATION_SENT_KEEP])
        self.game.narration_outbox = [e for e in outbox if id(e) not in drop]

    def resume_job(self, subject, rules) -> List[str]:
        """Finish a job interrupted mid-resolution, from its persisted plan.

        This is what makes a restored ``resolving`` job advance rather than
        merely be retained. It applies only the actions the ledger says were
        never claimed, in their original order, and it NEVER calls Tier 3 —
        the plan on disk is the plan, by construction.

        Returns the display lines the resumed actions produced. A job with no
        persisted plan is not resumable and returns []; see replayable_jobs()
        for why that is a refusal rather than a re-derivation.
        """
        from mtg.actions import execute_action_on_state
        job = self.find(subject)
        if job is None or not job.planned_actions:
            return []
        if self.is_buried(job):
            # CR 608: something is still above this entry on the stack —
            # most likely the counter that was cast at it. Finishing it now
            # would resolve it out of order, which is the very race the live
            # [STACK-LIFO-GUARD] exists to refuse.
            print("[RESOLVE-RESUME] %s: buried on the stack — not resuming"
                  % job.job_id)
            return []
        blocked = self.unresolved_choices(job)
        if blocked:
            # Same reason as replayable_jobs: finishing the plan now would
            # decide the player's choice for them. Refuse rather than guess.
            print("[RESOLVE-RESUME] %s: blocked on %d unanswered choice(s) — "
                  "not resuming" % (job.job_id, len(blocked)))
            return []
        messages = []
        for index, action in self.pending_actions(job):
            should_apply, _ = self.claim_action(job, index, action)
            if not should_apply:
                continue
            result = execute_action_on_state(rules, self.game, action)
            if result:
                messages.append(result)
        self.transition(job, "effects_applied")
        print("[RESOLVE-RESUME] %s: finished %d remaining action(s)"
              % (job.job_id, len(messages)))
        return messages

    # ------------------------------------------- Q-J slice 4: trigger/SBA events
    # Dispatched records are kept only as a bounded idempotency window, for the
    # same reason the narration outbox bounds its sent entries: the store rides
    # in every save. UNDISPATCHED records are never pruned — they are the
    # omission half of the contract.
    EVENTS_DISPATCHED_KEEP = 50

    def record_event(self, kind: str, source: Card, *, controller_name: str = "",
                     trigger_text: str = "", trigger_type: str = "",
                     context: str = "", occurrence_key: Optional[str] = None
                     ) -> ResolutionEvent:
        """Persist a trigger/SBA event BEFORE it goes on an in-memory queue.

        The id is deterministic per (source, kind, type, occurrence, position),
        so re-recording the SAME event inside one process re-derives the SAME
        id instead of minting a second record — which is what keeps a retried
        enqueue from double-counting after a replay.
        """
        source_id = str(getattr(source, "id", "") or getattr(source, "name", ""))
        source_name = str(getattr(source, "name", "") or source_id)
        job_id = str(getattr(self.game, "_active_resolution_job_id", "") or "")
        seed = "%s|%s|%s|%s" % (source_id, kind, trigger_type,
                                occurrence_key if occurrence_key else "")
        digest = hashlib.sha1(seed.encode("utf-8")).hexdigest()[:12]
        event_id = "%s:%s" % (kind, digest)
        # A same-seed record that is already DISPATCHED must not be reused —
        # a genuinely new occurrence of the same shape would inherit the old
        # record's "already done" mark and never fire. It gets a suffix.
        #
        # The suffix is found by SCANNING for a free one, never derived from
        # len(): the store is pruned, so a length-derived suffix lands back on
        # a live id and overwrites it. That is not hypothetical — it was
        # reproduced here (a reclaimed middle record dropped the length onto a
        # live UNDISPATCHED id, destroying a pending trigger), and it is the
        # same incompatibility between a derived-from-length identifier and a
        # pruning collection that the narration outbox already hit.
        existing = self.game.resolution_events.get(event_id)
        if existing is not None and existing.dispatched:
            suffix = 1
            while ("%s:%s:%d" % (kind, digest, suffix)
                   in self.game.resolution_events):
                suffix += 1
            event_id = "%s:%s:%d" % (kind, digest, suffix)
            existing = None
        if existing is None:
            existing = ResolutionEvent(
                event_id=event_id, kind=kind, source_id=source_id,
                source_name=source_name, controller_name=controller_name,
                trigger_text=trigger_text, trigger_type=trigger_type,
                context=context, occurrence_key=occurrence_key, job_id=job_id)
            self.game.resolution_events[event_id] = existing
        job = self.find(job_id) if job_id else None
        if job is not None and event_id not in job.trigger_event_ids:
            job.trigger_event_ids.append(event_id)
        self._persist()
        return existing

    def undispatched_events(self, kind: Optional[str] = None,
                            subject=None) -> List[ResolutionEvent]:
        """Events nobody has dispatched yet, oldest first.

        Derived from each record's own ``dispatched`` flag rather than from a
        maintained pending list — the slice-3 reasoning: a queue can be
        drained, cleared, or swept by several paths, and one missed removal
        would strand an event forever. An id whose record is GONE counts as
        DISPATCHED (the record outlives the dispatch, so its absence means the
        bounded prune reclaimed it, not that it is still waiting).
        """
        job = self.find(subject) if subject is not None else None
        wanted = set(job.trigger_event_ids) if job is not None else None
        out = []
        for event in self.game.resolution_events.values():
            if event.dispatched:
                continue
            if kind is not None and event.kind != kind:
                continue
            if wanted is not None and event.event_id not in wanted:
                continue
            out.append(event)
        return out

    def mark_event_dispatched(self, event_id: str) -> bool:
        """Claim one event before it resolves.

        Returns False when it was already claimed, which after a restart means
        a previous process owned it — see the at-most-once note on
        ``ResolutionEvent``. An unknown id returns True: an event with no
        durable record has nothing to recover from, and gating it would break
        live play for every undurable path.
        """
        event = self.game.resolution_events.get(str(event_id or ""))
        if event is None:
            return True
        if event.dispatched:
            print("[RESOLVE-EVENT-REPLAY] %s (%s) already dispatched — skipping"
                  % (event.event_id, event.source_name))
            return False
        event.dispatched = True
        self._persist()
        return True

    def rebuild_pending_queues(self) -> int:
        """Repopulate the in-memory trigger/death queues from persisted events.

        This is the half that makes the records worth having: after a load the
        live queues are empty (they hold objects JSON cannot carry), so every
        undispatched event is re-bound to the CURRENT object graph by id and
        pushed back. Returns how many entries were restored.

        An event whose source can no longer be found anywhere is left in the
        store, undispatched, rather than dropped — losing it silently is the
        failure this slice exists to remove, and a stranded record is visible.
        """
        restored = 0
        pending = list(getattr(self.game, "pending_async_triggers", None) or [])
        queued_ids = {entry.get("event_id") for entry in pending}
        died = list(getattr(self.game, "_recently_died", None) or [])
        died_ids = {str(getattr(card, "id", "")) for card, _ in died}
        for event in self.undispatched_events():
            if event.kind == "trigger":
                if event.event_id in queued_ids:
                    continue
                source = self._find_card_anywhere(event.source_id)
                if source is None:
                    print("[RESOLVE-EVENT-ORPHAN] %s: source %s not found — "
                          "left pending" % (event.event_id, event.source_name))
                    continue
                pending.append({
                    "source_card": source,
                    "trigger_text": event.trigger_text,
                    "trigger_type": event.trigger_type,
                    "controller_name": event.controller_name,
                    "context": event.context,
                    "occurrence_key": event.occurrence_key,
                    "event_id": event.event_id,
                })
                restored += 1
            elif event.kind == "death":
                if event.source_id in died_ids:
                    continue
                source = self._find_card_anywhere(event.source_id)
                owner = next((p for p in self.game.players
                              if p.name == event.controller_name), None)
                if source is None or owner is None:
                    print("[RESOLVE-EVENT-ORPHAN] %s: death source %s not found "
                          "— left pending" % (event.event_id, event.source_name))
                    continue
                died.append((source, owner))
                restored += 1
        if restored:
            self.game.pending_async_triggers = pending
            self.game._recently_died = died
            print("[RESOLVE-EVENT-REBUILD] restored %d queued event(s) from disk"
                  % restored)
        return restored

    def _find_card_anywhere(self, card_id: str) -> Optional[Card]:
        """Locate a card by stable id across every zone a source can be in.

        A dies trigger's source may itself be in a graveyard, and an LTB
        source may be in exile, so this deliberately does not stop at the
        battlefield.
        """
        target = str(card_id or "")
        if not target:
            return None
        for player in self.game.players:
            for zone in (player.battlefield, player.graveyard, player.exile,
                         player.hand, player.library):
                for card in zone:
                    if str(getattr(card, "id", "")) == target:
                        return card
        for entry in self.game.stack:
            card = getattr(entry, "card", None)
            if card is not None and str(getattr(card, "id", "")) == target:
                return card
        return None

    def prune_resolution_events(self) -> None:
        """Bound the dispatched window; never touch undispatched records.

        Reclaimed ids are also dropped from every job's link list. An id is
        then free exactly when it is absent from the store, which is what
        makes the suffix scan in record_event() sound: without this, a
        reissued suffix could be re-attributed to a job that once owned a
        since-pruned event, and the dangling link had no audit value anyway
        because the record it names is gone.
        """
        events = self.game.resolution_events
        dispatched = [e for e in events.values() if e.dispatched]
        if len(dispatched) <= self.EVENTS_DISPATCHED_KEEP:
            return
        reclaimed = set()
        for event in dispatched[:len(dispatched) - self.EVENTS_DISPATCHED_KEEP]:
            events.pop(event.event_id, None)
            reclaimed.add(event.event_id)
        if not reclaimed:
            return
        for job in self.game.resolution_jobs.values():
            if any(eid in reclaimed for eid in job.trigger_event_ids):
                job.trigger_event_ids = [eid for eid in job.trigger_event_ids
                                         if eid not in reclaimed]

    def is_buried(self, subject) -> bool:
        """True when this job's stack entry still has something ABOVE it.

        The live cast path pops the entry BEFORE transitioning to
        ``resolving`` ("Pop from stack before resolving", mtg/spells.py), so
        an ordinary in-flight resolution owns no stack entry at all and is
        never buried. A job that IS still on the stack with entries above it
        is the July LIFO-race shape — a buried spell that reached
        ``resolving`` while a counter targeting it was still above — and
        resolving it out of order violates CR 608.

        The live path guards that case with [STACK-LIFO-GUARD]; recovery had
        no equivalent, so a crash in that window turned a guarded race into
        an unguarded one.
        """
        job = self.find(subject)
        if job is None or not self.game.stack:
            return False
        for index, entry in enumerate(self.game.stack):
            if (str(getattr(entry, "resolution_job_id", "") or
                    getattr(entry, "entry_id", "")) == job.job_id):
                return index < len(self.game.stack) - 1
        return False

    def recoverable_jobs(self):
        """Jobs whose priority wait may safely resume without replaying effects."""
        return [
            job for job in self.game.resolution_jobs.values()
            if job.checkpoint in {"on_stack", "priority_open"}
            and not job.recovery_error
        ]

    def unresolved_choices(self, subject) -> List[str]:
        """Choice ids this job opened that nobody has answered yet.

        Derived from each record's own ``complete`` flag rather than from a
        maintained removal list: a choice can finish through submit, timeout,
        cancellation or a seat being eliminated, and a missed removal path
        would strand the job permanently. An id whose record is GONE counts
        as resolved — the record outlives the answer, so its absence means
        the choice was cleaned up, not that it is still waiting.
        """
        job = self.find(subject)
        if job is None:
            return []
        out = []
        for choice_id in job.unresolved_choice_ids:
            record = self.game.pending_choices.get(choice_id)
            if record is not None and not record.get("complete"):
                out.append(choice_id)
        return out

    def replayable_jobs(self):
        """Jobs interrupted mid-resolution that CAN be finished from disk.

        A job only qualifies once its plan was persisted: without one there is
        nothing to finish, and re-deriving the plan would mean re-querying
        Tier 3.  Those stay in ``recovery_error`` territory and are surfaced
        to a human rather than guessed at.
        """
        return [
            job for job in self.game.resolution_jobs.values()
            if job.checkpoint in {"resolving", "effects_applied"}
            and job.planned_actions
            and not job.recovery_error
            # Q-J slice 3: resuming past an unanswered private choice would
            # silently make that choice FOR the player. A job blocked on one
            # is not replayable; it has to wait for the answer.
            and not self.unresolved_choices(job)
            # Q-K nested stack: nor is one with entries still above it.
            and not self.is_buried(job)
        ]
