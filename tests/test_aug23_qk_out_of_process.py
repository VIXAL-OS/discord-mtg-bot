"""Q-K out-of-process death injection — a real process, a real file on disk.

WHAT THIS ADDS OVER THE IN-PROCESS MATRIX. tests/test_aug23_qk_crash_matrix.py
simulates a crash by serializing, discarding the live object and rebuilding
from the bytes. That is the harshest thing available inside one interpreter,
and it is genuinely strong — but three things are structurally out of its
reach, and all three are what "crash-safe" is actually claiming:

  1. The snapshot it recovers from is a Python dict it just built in memory.
     Nothing proves the bytes ever reached a FILE, or that the file is
     loadable by a process that did not create it.
  2. Module-level state, singletons and caches survive an in-process reload.
     A fresh interpreter carries none of it, so anything recovery accidentally
     leaned on shows up here and nowhere else.
  3. It cannot die mid-write. save_game() claims atomicity (temp -> flush ->
     fsync -> os.replace) precisely so a death there cannot truncate the only
     recovery record, and that claim was untested.

HOW THE DEATH IS INJECTED, and why it is not a signal. See the note at the top
of tests/_qk_crash_child.py: an external SIGKILL races the child and lands
somewhere different every run, while os._exit() from a named point reproduces
the same consequences (no atexit, no finally, no flush, no interpreter
shutdown) deterministically. Every test asserts the child exited 137, because
a child that exited 0 has not tested anything.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
CHILD = REPO / "tests" / "_qk_crash_child.py"
CRASH_EXIT = 137
THREAD_ID = 4242

# Uninterrupted truth: Rick 40 +2 +5 = 47, Claude 40 -3 -1 = 36.
FINAL_LIFE = [47, 36]


def run_child(games_dir, crash_at="", thread_id=THREAD_ID):
    env = dict(os.environ)
    env["MTG_QK_GAMES_DIR"] = str(games_dir)
    env["MTG_QK_CRASH_AT"] = crash_at
    env["MTG_QK_THREAD_ID"] = str(thread_id)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    return subprocess.run([sys.executable, str(CHILD)], cwd=str(REPO), env=env,
                          capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=180)


def load_snapshot(games_dir):
    path = Path(games_dir) / ("%d.json" % THREAD_ID)
    assert path.exists(), "no snapshot on disk at %s" % path
    return json.loads(path.read_text(encoding="utf-8"))


def resume_from_disk(games_dir):
    """Recover the way a restarted bot would: read the FILE, rebuild, finish."""
    from mtg.models import GameState
    from mtg.resolution import ResolutionCoordinator
    from mtg.rules_engine import RulesEngine

    game = GameState.from_dict(load_snapshot(games_dir))
    rules = RulesEngine(None)
    game._rules_engine = rules
    coord = ResolutionCoordinator.for_game(None, game)
    coord.bind_restored_stack()
    for job in coord.replayable_jobs():
        coord.resume_job(job, rules)
    return game


# --------------------------------------------------------------------------
# The harness must be able to fail
# --------------------------------------------------------------------------

def test_the_uninterrupted_child_completes(tmp_path):
    """BASELINE. Without this a broken runner would make every crash test
    'pass' by never running anything — the failure mode that invalidated a
    whole mutation sweep earlier in this session."""
    result = run_child(tmp_path)
    assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-2000:]
    assert "COMPLETED" in result.stdout
    assert [p["life"] for p in load_snapshot(tmp_path)["players"]] == FINAL_LIFE


def test_the_injection_actually_kills_the_process(tmp_path):
    """A crash test whose child exits 0 has tested nothing."""
    result = run_child(tmp_path, crash_at="after_action_1")
    assert result.returncode == CRASH_EXIT
    assert "CRASHING AT after_action_1" in result.stdout


# --------------------------------------------------------------------------
# Recovery from a file written by a process that no longer exists
# --------------------------------------------------------------------------

@pytest.mark.parametrize("applied", [0, 1, 2])
def test_death_after_n_actions_recovers_to_the_same_state(tmp_path, applied):
    """The matrix's property, now across a real process boundary.

    The child dies after applying `applied+1` actions and saving. A fresh
    interpreter loads the FILE and finishes from the persisted plan; the end
    state must equal the uninterrupted run exactly.
    """
    result = run_child(tmp_path, crash_at="after_action_%d" % applied)
    assert result.returncode == CRASH_EXIT

    partial = load_snapshot(tmp_path)
    assert [p["life"] for p in partial["players"]] != FINAL_LIFE, (
        "fixture must actually stop early, or this proves nothing")

    game = resume_from_disk(tmp_path)
    assert [p.life for p in game.players] == FINAL_LIFE


def test_death_after_the_last_action_makes_recovery_a_no_op(tmp_path):
    """Every action was applied and saved; only the `complete` transition was
    lost. Recovery must add nothing — this is the double-application case, and
    it is the one a careless "just replay the plan" recovery would fail."""
    result = run_child(tmp_path, crash_at="after_action_3")
    assert result.returncode == CRASH_EXIT

    snapshot = load_snapshot(tmp_path)
    assert [p["life"] for p in snapshot["players"]] == FINAL_LIFE
    job = list(snapshot["resolution_jobs"].values())[0]
    assert len(job["applied_action_keys"]) == 4

    game = resume_from_disk(tmp_path)
    assert [p.life for p in game.players] == FINAL_LIFE, (
        "recovery re-applied actions that were already done")


def test_death_before_any_action_still_recovers(tmp_path):
    """Dying right after the plan is persisted: nothing applied, all replayable."""
    result = run_child(tmp_path, crash_at="after_resolving")
    assert result.returncode == CRASH_EXIT

    snapshot = load_snapshot(tmp_path)
    job = list(snapshot["resolution_jobs"].values())[0]
    assert job["checkpoint"] == "resolving"
    assert len(job["planned_actions"]) == 4
    assert job["applied_action_keys"] == []

    game = resume_from_disk(tmp_path)
    assert [p.life for p in game.players] == FINAL_LIFE


def test_a_claimed_but_unapplied_action_is_dropped_not_doubled(tmp_path):
    """THE at-most-once window, finally observed rather than argued.

    The claim is persisted BEFORE the mutation, so a death in between loses
    that one action. This test pins the DOCUMENTED direction: after recovery
    the claimed action has not been applied and is not re-applied, so the
    final life total is short by exactly that action — never doubled.

    The register has asserted this trade for three slices; nothing had shown
    it happening, because an in-process test cannot die between two adjacent
    statements.
    """
    # Action 0 is 3 damage to Claude. Claim it, then die before it runs.
    result = run_child(tmp_path, crash_at="after_claim_0")
    assert result.returncode == CRASH_EXIT

    game = resume_from_disk(tmp_path)
    rick, claude = game.players
    # Rick still gains both life actions; Claude takes only the SECOND damage.
    assert rick.life == 47
    assert claude.life == 40 - 1, (
        "the claimed-but-unapplied action must be dropped, not replayed")
    assert claude.life != 36, "it must not have been applied after all"
    assert claude.life != 36 - 3, "and certainly not applied twice"


# --------------------------------------------------------------------------
# The atomicity claim, which only a real process death can test
# --------------------------------------------------------------------------

def test_a_death_mid_save_never_publishes_a_truncated_snapshot(tmp_path):
    """save_game()'s whole reason for existing, tested at last.

    The child lays down one complete snapshot, then dies partway through
    writing the next one. os.replace() is the publish step, so the file that
    survives must be the complete OLD snapshot — parseable, not a prefix.
    """
    result = run_child(tmp_path, crash_at="during_save")
    assert result.returncode == CRASH_EXIT
    assert "CRASHING DURING SAVE" in result.stdout

    # Parses at all: a truncated JSON write would raise here.
    snapshot = load_snapshot(tmp_path)
    assert snapshot["thread_id"] == THREAD_ID
    assert len(snapshot["players"]) == 2

    job = list(snapshot["resolution_jobs"].values())[0]
    assert len(job["planned_actions"]) == 4, (
        "the published snapshot must be a COMPLETE one, not a prefix")

    # And it is still recoverable, which is the point of keeping it intact.
    #
    # NOTE the expected total is NOT the uninterrupted one, and that is the
    # at-most-once semantic showing up rather than a defect: claim_action()
    # persists, so the surviving snapshot has action 0 (3 damage) CLAIMED but
    # not yet applied — the child died before the save that would have
    # recorded its effect. Recovery therefore drops exactly that one action.
    # Claude takes only the second damage, and never the first twice.
    game = resume_from_disk(tmp_path)
    rick, claude = game.players
    assert rick.life == 47
    assert claude.life == 39, (
        "expected the claimed-but-unapplied action to be dropped once")


def test_a_death_mid_save_leaves_no_half_written_file_in_place(tmp_path):
    """The temp file must never be mistaken for the snapshot.

    It is written under a dotted `.<thread>.<rand>.tmp` name in the same
    directory, so a loader that globbed *.json would ignore it — this pins
    that the naming actually holds, since a leftover `4242.json.tmp` or a
    bare temp with a .json suffix would be loaded as a game.
    """
    result = run_child(tmp_path, crash_at="during_save")
    assert result.returncode == CRASH_EXIT

    jsons = sorted(p.name for p in Path(tmp_path).glob("*.json"))
    assert jsons == ["%d.json" % THREAD_ID], (
        "only the published snapshot may match *.json; found %s" % jsons)


# --------------------------------------------------------------------------
# Nothing in memory carries over
# --------------------------------------------------------------------------

def test_recovery_needs_nothing_but_the_file(tmp_path):
    """A fresh interpreter shares no singleton, cache or module state with
    the process that died, so anything recovery leaned on implicitly fails
    here and only here."""
    result = run_child(tmp_path, crash_at="after_action_0")
    assert result.returncode == CRASH_EXIT

    # Recover in ANOTHER separate interpreter, reading only the file.
    script = (
        "import json,sys;"
        "sys.path.insert(0, %r);"
        "from mtg.models import GameState;"
        "from mtg.resolution import ResolutionCoordinator;"
        "from mtg.rules_engine import RulesEngine;"
        "d=json.load(open(%r, encoding='utf-8'));"
        "g=GameState.from_dict(d);"
        "r=RulesEngine(None);"
        "g._rules_engine=r;"
        "c=ResolutionCoordinator.for_game(None,g);"
        "c.bind_restored_stack();"
        "[c.resume_job(j,r) for j in c.replayable_jobs()];"
        "print('LIFE', [p.life for p in g.players])"
        % (str(REPO), str(Path(tmp_path) / ("%d.json" % THREAD_ID)))
    )
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    out = subprocess.run([sys.executable, "-c", script], cwd=str(REPO),
                         env=env, capture_output=True, text=True,
                         encoding="utf-8", errors="replace", timeout=180)
    assert out.returncode == 0, out.stdout[-1500:] + out.stderr[-1500:]
    assert "LIFE [47, 36]" in out.stdout
