"""Child process for Q-K out-of-process crash injection. NOT a test.

Named with a leading underscore so pytest does not collect it. The parent
(tests/test_aug23_qk_out_of_process.py) runs this in a real, separate
interpreter and kills it at a named point.

WHY os._exit AND NOT AN EXTERNAL SIGNAL. The point of injection is to be
DETERMINISTIC about where the process dies; an external SIGKILL races against
the child's progress and would land somewhere different on every run. From the
dying process's own point of view ``os._exit`` is the same event as SIGKILL —
no ``atexit`` hooks, no ``finally`` blocks, no buffer flush, no interpreter
shutdown — so it reproduces the consequences exactly while staying repeatable.
The one thing it cannot model is the OS tearing down a write mid-syscall, which
is what the fsync in save_game() exists to bound.

The exit code is 137 (128+9, the shell's convention for SIGKILL) so the parent
can assert the injection ACTUALLY FIRED rather than inferring it from a missing
file — a child that exited 0 has not tested anything.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

CRASH_AT = os.environ.get("MTG_QK_CRASH_AT", "")
GAMES_DIR = os.environ["MTG_QK_GAMES_DIR"]
THREAD_ID = int(os.environ.get("MTG_QK_THREAD_ID", "4242"))

CRASH_EXIT = 137

# The same shape the in-process matrix uses, so the two are comparable.
PLAN = [
    {"action": "deal_damage", "amount": 3, "target_player": "Claude"},
    {"action": "gain_life", "player": "Rick", "amount": 2},
    {"action": "deal_damage", "amount": 1, "target_player": "Claude"},
    {"action": "gain_life", "player": "Rick", "amount": 5},
]


def die(point):
    """Vanish, if this is the point the parent asked for."""
    if CRASH_AT == point:
        sys.stdout.write("CRASHING AT %s\n" % point)
        sys.stdout.flush()
        os._exit(CRASH_EXIT)


def build():
    from mtg.engine import GameEngine
    from mtg.models import Card, GameState, Player, StackEntry
    from mtg.resolution import ResolutionCoordinator
    from mtg.rules_engine import RulesEngine

    engine = GameEngine.__new__(GameEngine)
    engine.GAMES_DIR = GAMES_DIR
    engine.rules = RulesEngine(None)
    engine.client = None
    engine.games = {}

    rick = Player(name="Rick", user_id=99999, life=40)
    claude = Player(name="Claude", user_id=None, is_claude=True, life=40)
    game = GameState(thread_id=THREAD_ID, format="commander",
                     players=[rick, claude])
    game.turn_number = 1
    game._rules_engine = engine.rules

    card = Card(name="Contrived Bolt", id="qk_bolt", mana_cost="{R}",
                type_line="Instant", oracle_text="Damage and life.")
    game.players[0].battlefield.append(card)
    entry = StackEntry(card=card, controller_name="Rick", controller_index=0,
                       target=None)
    coord = ResolutionCoordinator.for_game(engine, game)
    job = coord.register(entry)
    return engine, game, coord, job


def main():
    from mtg.actions import execute_action_on_state

    engine, game, coord, job = build()
    die("after_register")

    coord.record_plan(job, PLAN, tier="tier3")
    die("after_plan")

    coord.transition(job, "resolving")
    die("after_resolving")

    for index, action in enumerate(PLAN):
        should_apply, _key = coord.claim_action(job, index, action)
        # The claim is persisted BEFORE the mutation (at-most-once). Dying
        # HERE is the documented window where one action is lost rather than
        # doubled, and the parent asserts exactly that.
        die("after_claim_%d" % index)
        if should_apply:
            execute_action_on_state(engine.rules, game, action)
        engine.save_game(game)
        die("after_action_%d" % index)

    coord.transition(job, "effects_applied")
    die("after_effects")

    coord.transition(job, "complete")
    engine.save_game(game)
    sys.stdout.write("COMPLETED life=%s\n"
                     % json.dumps([p.life for p in game.players]))
    sys.stdout.flush()
    return 0


def main_crash_during_save():
    """Die INSIDE save_game, after the temp file is partly written.

    This is the case an in-process test structurally cannot reach: it asserts
    that the atomic write (temp -> flush -> fsync -> os.replace) publishes
    either the complete OLD snapshot or the complete NEW one, never a
    truncated file. The child first lays down one good snapshot so there IS an
    old one to fall back to.
    """
    from mtg.actions import execute_action_on_state

    engine, game, coord, job = build()
    coord.record_plan(job, PLAN, tier="tier3")
    coord.transition(job, "resolving")
    engine.save_game(game)           # a known-good snapshot on disk

    # Apply one action so the NEXT save would differ from the good one.
    should_apply, _ = coord.claim_action(job, 0, PLAN[0])
    if should_apply:
        execute_action_on_state(engine.rules, game, PLAN[0])

    real_dump = json.dump

    def dying_dump(obj, fp, **kwargs):
        # Write a genuine prefix of the payload, then vanish mid-write.
        blob = json.dumps(obj, **kwargs)
        fp.write(blob[: len(blob) // 3])
        fp.flush()
        sys.stdout.write("CRASHING DURING SAVE\n")
        sys.stdout.flush()
        os._exit(CRASH_EXIT)

    json.dump = dying_dump
    try:
        engine.save_game(game)
    finally:
        json.dump = real_dump
    return 0


if __name__ == "__main__":
    if CRASH_AT == "during_save":
        sys.exit(main_crash_during_save())
    sys.exit(main())
