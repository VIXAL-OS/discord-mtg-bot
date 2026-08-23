"""Q-J slice 1 — persisted action plans and the idempotent apply ledger.

Q-I made a resolution job durable but stopped at ``resolving``: a job
interrupted mid-effect was retained honestly and could not advance, because
nothing had persisted WHAT it was going to do. Slice 1 supplies that half.

The two properties that matter, and the direction chosen for each:

  - The plan is persisted BEFORE the first action runs, so recovery replays
    the original plan and never re-queries Tier 3. A second call to the model
    can legitimately return a different plan, and applying half of one plan
    and half of another resolves nothing.
  - Every action is CLAIMED before it executes. That is at-most-once: a crash
    between the claim and the mutation loses one action rather than repeating
    it. Deliberate — a doubled Lightning Bolt is an illegal state nobody can
    unwind, while a dropped one is visible and fixable at the table.

Fixture discipline: the round-trip tests go through real
``GameState.to_dict()`` / ``from_dict()`` rather than mutating a job in
place, because "survives a restart" is the whole claim. And the end-to-end
test drives the actual Tier-3 loop in ``mtg.judge.resolve_effect`` with a
stub client — per the standing rule that a helper pinned only through direct
calls is not pinned into production.
"""
import asyncio
import json

import pytest

from mtg.resolution import ResolutionCoordinator, action_key
from mtg.models import StackEntry


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _job_for(game, make_card, name="Lightning Bolt"):
    """Register a real resolution job the way a cast does."""
    card = make_card(name, mana_cost="{R}", type_line="Instant",
                     oracle_text="Lightning Bolt deals 3 damage to any target.",
                     power=None, toughness=None)
    game.players[0].battlefield.append(card)
    entry = StackEntry(card=card, controller_name=game.players[0].name,
                       controller_index=0, target=None)
    coord = ResolutionCoordinator.for_game(None, game)
    return coord, coord.register(entry), card


PLAN = [
    {"action": "deal_damage", "amount": 3, "target_player": "Claude"},
    {"action": "draw_cards", "player": "Rick", "amount": 1},
    {"action": "gain_life", "player": "Rick", "amount": 2},
    {"action": "create_token", "player": "Rick", "name": "Goblin",
     "power": 1, "toughness": 1, "types": "Creature — Goblin", "count": 1},
]


class TestPlanPersistence:

    def test_a_plan_is_recorded_before_anything_runs(self, game, make_card):
        coord, job, _ = _job_for(game, make_card)
        assert job.planned_actions == []

        coord.record_plan(job, PLAN, tier="tier3")

        assert job.planned_actions == PLAN
        assert job.applied_action_keys == []
        assert job.cast_facts.get("plan_tier") == "tier3"

    def test_re_recording_the_same_plan_keeps_the_ledger(self, game, make_card):
        """A retry inside ONE process must not wipe what has been applied."""
        coord, job, _ = _job_for(game, make_card)
        coord.record_plan(job, PLAN)
        coord.claim_action(job, 0, PLAN[0])
        assert len(job.applied_action_keys) == 1

        coord.record_plan(job, PLAN)

        assert len(job.applied_action_keys) == 1, \
            "an identical re-record must be a no-op"

    def test_a_different_plan_resets_the_ledger(self, game, make_card):
        """Keys from the old plan cannot describe the new one, so inheriting
        them would mark unrelated work as done."""
        coord, job, _ = _job_for(game, make_card)
        coord.record_plan(job, PLAN)
        coord.claim_action(job, 0, PLAN[0])

        other = [{"action": "draw_cards", "player": "Claude", "amount": 3}]
        coord.record_plan(job, other)

        assert job.planned_actions == other
        assert job.applied_action_keys == []

    def test_no_durable_job_means_no_plan_and_no_gating(self, game, make_card):
        """Trigger drains and manual activations have no job. They must keep
        working exactly as before, not be gated into silence."""
        coord = ResolutionCoordinator.for_game(None, game)
        assert coord.record_plan("no-such-job", PLAN) is None
        should_apply, key = coord.claim_action("no-such-job", 0, PLAN[0])
        assert should_apply is True and key == ""


class TestIdempotentClaims:

    def test_claiming_twice_refuses_the_second(self, game, make_card):
        coord, job, _ = _job_for(game, make_card)
        coord.record_plan(job, PLAN)

        first, key = coord.claim_action(job, 0, PLAN[0])
        second, same_key = coord.claim_action(job, 0, PLAN[0])

        assert first is True
        assert second is False, "at-most-once: a claimed action never re-runs"
        assert key == same_key

    def test_the_claim_is_persisted_before_the_action_would_run(
            self, game, make_card):
        """The ordering IS the semantic. If the key were recorded after the
        mutation, a crash in between would replay it — at-least-once, which
        is what draws twice."""
        coord, job, _ = _job_for(game, make_card)
        coord.record_plan(job, PLAN)

        should_apply, key = coord.claim_action(job, 1, PLAN[1])

        assert should_apply is True
        assert key in job.applied_action_keys, \
            "the key must be in the ledger by the time the caller acts"

    def test_keys_are_position_and_content_scoped(self, game, make_card):
        coord, job, _ = _job_for(game, make_card)
        same_action_other_slot = action_key(job.job_id, 1, PLAN[0])
        other_action_same_slot = action_key(job.job_id, 0, PLAN[1])
        assert action_key(job.job_id, 0, PLAN[0]) not in (
            same_action_other_slot, other_action_same_slot)

    def test_two_jobs_do_not_share_a_ledger(self, game, make_card):
        """Two copies of one spell on the stack are exactly what Q-I's exact
        identity work exists for."""
        coord, job_a, _ = _job_for(game, make_card)
        _, job_b, _ = _job_for(game, make_card)
        assert job_a.job_id != job_b.job_id

        coord.record_plan(job_a, PLAN)
        coord.record_plan(job_b, PLAN)
        coord.claim_action(job_a, 0, PLAN[0])

        should_apply, _ = coord.claim_action(job_b, 0, PLAN[0])
        assert should_apply is True, "job B's identical action is its own work"


class TestSurvivesRestart:

    def _reload(self, game):
        from mtg.models import GameState
        return GameState.from_dict(json.loads(json.dumps(game.to_dict())))

    def test_replay_after_reload_applies_only_what_is_left(
            self, game, make_card):
        """The core recovery claim, across a real serialize/deserialize."""
        coord, job, _ = _job_for(game, make_card)
        coord.record_plan(job, PLAN, tier="tier3")
        coord.transition(job, "resolving")
        for i in (0, 1):
            assert coord.claim_action(job, i, PLAN[i])[0] is True

        restored = self._reload(game)
        rcoord = ResolutionCoordinator.for_game(None, restored)
        pending = rcoord.pending_actions(job.job_id)

        assert [i for i, _ in pending] == [2, 3], \
            "actions 0 and 1 were already owned by the dead process"
        assert [a["action"] for _, a in pending] == [
            "gain_life", "create_token"]

    def test_a_fully_applied_plan_replays_nothing(self, game, make_card):
        coord, job, _ = _job_for(game, make_card)
        coord.record_plan(job, PLAN)
        for i, action in enumerate(PLAN):
            coord.claim_action(job, i, action)

        restored = self._reload(game)
        rcoord = ResolutionCoordinator.for_game(None, restored)
        assert rcoord.pending_actions(job.job_id) == []

    def test_the_plan_itself_survives_the_round_trip(self, game, make_card):
        coord, job, _ = _job_for(game, make_card)
        coord.record_plan(job, PLAN, tier="tier3")

        restored = self._reload(game)
        rjob = restored.resolution_jobs[job.job_id]

        assert rjob.planned_actions == PLAN, \
            "recovery must replay THIS plan, never re-query the model"
        assert rjob.cast_facts.get("plan_tier") == "tier3"


class TestReplayEligibility:

    def test_a_mid_resolution_job_with_a_plan_is_replayable(
            self, game, make_card):
        coord, job, _ = _job_for(game, make_card)
        coord.record_plan(job, PLAN)
        coord.transition(job, "resolving")
        assert job in coord.replayable_jobs()

    def test_a_mid_resolution_job_without_a_plan_is_not(
            self, game, make_card):
        """ADVERSE CONTROL. With no persisted plan there is nothing to
        finish, and re-deriving it would mean re-querying Tier 3. Those stay
        out of the replay set and surface to a human instead."""
        coord, job, _ = _job_for(game, make_card)
        coord.transition(job, "resolving")
        assert job not in coord.replayable_jobs()

    def test_a_job_still_waiting_for_priority_is_not_replayable(
            self, game, make_card):
        """It belongs to recoverable_jobs — resume the wait, do not replay
        effects that never started."""
        coord, job, _ = _job_for(game, make_card)
        coord.record_plan(job, PLAN)
        assert job not in coord.replayable_jobs()
        assert job in coord.recoverable_jobs()


# --------------------------------------------------------------------------
# End to end, through the real Tier-3 loop.
# --------------------------------------------------------------------------

class _StubBlock:
    def __init__(self, text):
        self.text = text
        self.type = "text"


class _StubResponse:
    def __init__(self, text):
        self.content = [_StubBlock(text)]
        self.usage = None


class _StubMessages:
    """Stands in for rules.client.messages, returning one canned plan."""

    def __init__(self, payload):
        self.payload = payload
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        return _StubResponse(json.dumps(self.payload))


class _StubClient:
    def __init__(self, payload):
        self.messages = _StubMessages(payload)


class TestTier3LoopUsesTheLedger:
    """The wiring, not just the helper.

    Both halves of slice 1 are asserted through mtg.judge.resolve_effect: the
    plan lands on the job before any action runs, and a second pass over the
    same job re-applies nothing.
    """

    PAYLOAD = {
        "explanation": "Draws two cards.",
        "actions": [
            {"action": "draw_cards", "player": "Rick", "amount": 1},
            {"action": "draw_cards", "player": "Rick", "amount": 1},
        ],
    }

    def _run(self, rules, game, card):
        from mtg.judge import resolve_effect
        return asyncio.run(resolve_effect(
            rules, game, "Draw two cards.",
            source_card=card.name, controller=game.players[0].name))

    def test_the_plan_reaches_the_job_and_a_rerun_reapplies_nothing(
            self, rules, game, make_card):
        coord, job, card = _job_for(game, make_card, "Divination")
        game._active_resolution_job_id = job.job_id
        rules.client = _StubClient(self.PAYLOAD)

        for _ in range(6):
            game.players[0].library.append(
                make_card("Forest", type_line="Basic Land — Forest",
                          oracle_text="{T}: Add {G}.",
                          power=None, toughness=None))
        hand_before = len(game.players[0].hand)

        self._run(rules, game, card)

        assert job.planned_actions == self.PAYLOAD["actions"], \
            "the Tier-3 plan must be persisted on the job"
        assert len(job.applied_action_keys) == 2
        drew = len(game.players[0].hand) - hand_before
        assert drew == 2, "first pass draws both cards (got %d)" % drew

        # Second pass: same job, same plan. Every key is already claimed.
        self._run(rules, game, card)

        assert len(game.players[0].hand) - hand_before == 2, \
            "a replay must not draw the same cards again"
        assert len(job.applied_action_keys) == 2

    def test_without_a_job_stamp_the_loop_is_unchanged(
            self, rules, game, make_card):
        """ADVERSE CONTROL: live play with no durable job (a trigger drain)
        must still apply its actions, twice if it is genuinely called twice."""
        card = make_card("Divination", mana_cost="{2}{U}",
                         type_line="Sorcery", oracle_text="Draw two cards.",
                         power=None, toughness=None)
        game._active_resolution_job_id = None
        rules.client = _StubClient(self.PAYLOAD)
        for _ in range(6):
            game.players[0].library.append(
                make_card("Forest", type_line="Basic Land — Forest",
                          oracle_text="{T}: Add {G}.",
                          power=None, toughness=None))
        hand_before = len(game.players[0].hand)

        self._run(rules, game, card)
        self._run(rules, game, card)

        assert len(game.players[0].hand) - hand_before == 4, \
            "an undurable resolution is not gated by the ledger"
