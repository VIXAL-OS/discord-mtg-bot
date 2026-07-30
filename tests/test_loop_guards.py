"""The loop-protection inventory (CLAUDE.md, Post-Batch Bug Audit Playbook)
pinned to source — July 30, 2026.

Loop protection is a deliberate subsystem (the paper proves arbitrary
Magic games can encode a Turing machine; Arena's answer is CR 730 plus
iteration caps plus the rope; ours accreted cap by cap after batch
deaths). This test keeps the documented inventory honest: every cap the
playbook lists must still exist where it says, so an audit can grep the
whole subsystem from one table.
"""
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

# (file, identifier-or-tag) — one row per CLAUDE.md inventory entry.
INVENTORY = [
    ("mtg/engine.py", "_process_delayed_triggers"),
    ("mtg/spells.py", "[STACK-LIFO-FORCE]"),
    ("mtg/spells.py", "LIFO rescue exhausted"),
    ("mtg/spells.py", "_MAX_LIFO_RESCUE_CYCLES"),
    ("mtg/spells.py", "max_window_waits"),
    ("mtg/spells.py", "MAX_STACK_DEPTH"),
    ("mtg/triggers.py", "MAX_DIES_TRIGGERS"),
    ("mtg/autoplay.py", "max_turns"),
    ("mtg/autoplay.py", "[STUCK-GAME] Stagnation draw"),
]


@pytest.mark.parametrize("rel,needle", INVENTORY,
                         ids=[f"{r}:{n[:24]}" for r, n in INVENTORY])
def test_documented_loop_guard_exists(rel, needle):
    src = (ROOT / rel).read_text(encoding="utf-8")
    assert needle in src, (
        f"the loop-protection inventory in CLAUDE.md lists {needle!r} in "
        f"{rel}, but it is gone — update the table (and keep a cap: the "
        f"batch-15321 drain loop is what unbounded iteration costs)")


def test_delayed_trigger_drain_iterates_a_detached_queue():
    """The batch-15321 killer, structurally: the drain must detach the
    queue before iterating so mid-drain schedules land on the NEXT drain
    (one Yorion bounce per end step, CR 603.7)."""
    src = (ROOT / "mtg/engine.py").read_text(encoding="utf-8")
    i = src.find("def _process_delayed_triggers")
    assert i > 0
    body = src[i:i + 3000]
    assert "delayed_triggers" in body
    # The detach: the live list is swapped/emptied before the loop runs.
    assert ("game.delayed_triggers = []" in body
            or "detach" in body.lower()), (
        "the drain no longer detaches the live queue — the Yorion/Oath "
        "mutual-flicker loop (9,867 cycles, 3.3MB log) rides this exact "
        "structure")
