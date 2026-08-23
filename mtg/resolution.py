"""Durable CR 608 resolution-job coordination.

Q-I moves ownership of stack identity and checkpoint persistence out of the
cast coroutine.  Q-J will extend this coordinator with idempotent effect
execution and the narration outbox; until then a restored ``resolving`` job
is retained honestly instead of re-running a partially applied effect.
"""

from __future__ import annotations

import asyncio
from typing import Optional, Union

from mtg.models import Card, GameState, ResolutionJob, StackEntry


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


class ResolutionCoordinator:
    """Own stable resolution jobs and persist meaningful transitions."""

    def __init__(self, engine, game: GameState):
        self.engine = engine
        self.game = game

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

    def recoverable_jobs(self):
        """Jobs whose priority wait may safely resume without replaying effects."""
        return [
            job for job in self.game.resolution_jobs.values()
            if job.checkpoint in {"on_stack", "priority_open"}
            and not job.recovery_error
        ]
