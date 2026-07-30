"""Pin for the July 29 delayed-trigger drain hotfix.

game_1532203668261044486 (layers vs aminatou, the first strict batch): the
end-step drain iterated the LIVE game.delayed_triggers list. Yorion's return
re-entered Oath of Teferi, whose template flickered Yorion back immediately;
Yorion's ETB scheduled a NEW end-step return that the live iteration picked
up IN THE SAME DRAIN — 9,867 mutual-flicker cycles, a 3.3MB log, and the
batch died at 125/152 games.

CR 603.7: an ability scheduled for "the beginning of the next end step"
during an end step waits for the NEXT one.
"""


def _engine():
    from mtg.engine import GameEngine
    return GameEngine(None)


class TestDelayedTriggerDrainDetachesTheQueue:

    def test_mid_drain_schedules_wait_for_the_next_drain(self, game):
        """A trigger scheduled by a FIRING trigger's own action must not fire
        in the same drain (the infinite-loop shape) and must not be lost by
        the final queue reassignment (the silent-drop shape)."""
        engine = _engine()
        fired = []

        def fake_execute(g, action, _real=engine.rules._execute_action_on_state):
            fired.append(action.get("action"))
            if action.get("action") == "yorion_like":
                # The Yorion shape: firing this trigger schedules ANOTHER
                # end-step trigger (the mutual-flicker re-entry).
                g.delayed_triggers.append({
                    'trigger_at': 'end_step',
                    'source': 'Yorion, Sky Nomad',
                    'turn_delay': 0,
                    'actions': [{"action": "yorion_like"}],
                })
            return None

        engine.rules._execute_action_on_state = fake_execute
        game.delayed_triggers = [{
            'trigger_at': 'end_step',
            'source': 'Yorion, Sky Nomad',
            'turn_delay': 0,
            'actions': [{"action": "yorion_like"}],
        }]

        engine._process_delayed_triggers(game, "end_step")

        assert fired == ["yorion_like"], \
            "the mid-drain schedule fired in the SAME drain — the 9,867-cycle loop"
        assert len(game.delayed_triggers) == 1, \
            "the mid-drain schedule must survive for the NEXT end step"
        assert game.delayed_triggers[0]['source'] == 'Yorion, Sky Nomad'

        # And the next drain fires it exactly once more, scheduling one more —
        # one bounce per end step, bounded by the turn cap.
        fired.clear()
        engine._process_delayed_triggers(game, "end_step")
        assert fired == ["yorion_like"]
        assert len(game.delayed_triggers) == 1

    def test_non_matching_phase_triggers_still_survive(self, game):
        engine = _engine()
        game.delayed_triggers = [{
            'trigger_at': 'upkeep',
            'source': 'Pact of Negation',
            'turn_delay': 0,
            'upkeep_of': 0,
            'actions': [],
        }]
        engine._process_delayed_triggers(game, "end_step")
        assert len(game.delayed_triggers) == 1, \
            "detaching the queue must not drop other-phase triggers"
