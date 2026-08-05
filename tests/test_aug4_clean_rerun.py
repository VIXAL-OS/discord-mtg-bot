"""Regression pins from the clean mixed-provider rerun at sha=d70a0b7."""

import inspect

from cube_draft import (_format_ai_turn_message,
                        _format_autodraft_pick_summary,
                        _format_post_combat_message as _format_cube_post_combat_message)
from mtg.autoplay import _format_post_combat_message


def test_post_combat_summary_uses_the_actual_player_name():
    """The d70 corpus emitted 235 user-visible Claude post-combat headers."""
    message = _format_post_combat_message("Qwen", ["casts Opt", "passes"])

    assert message == "**Qwen (post-combat):**\n• casts Opt\n• passes"
    assert "Claude" not in message

    import mtg.autoplay as autoplay
    assert '"**Claude (post-combat):**' not in inspect.getsource(autoplay)


def test_autodraft_summaries_use_the_actual_ai_seat_name():
    """The same corpus exposed 19 remaining Claude labels in the cube path."""
    turn = _format_ai_turn_message("Deepseek", ["plays Island", "passes"])
    post_combat = _format_cube_post_combat_message(
        "Deepseek", ["casts Opt", "passes"])
    pick = _format_autodraft_pick_summary(
        2, 5, "Lightning Bolt", "Deepseek", "Counterspell")

    assert turn == "**Deepseek's turn:**\n• plays Island\n• passes"
    assert post_combat == ("**Deepseek (post-combat):**\n"
                           "• casts Opt\n• passes")
    assert pick == ("R2P5: Rick → **Lightning Bolt** | "
                    "Deepseek → **Counterspell**")
    assert "Claude" not in turn + post_combat + pick

    import cube_draft
    source = inspect.getsource(cube_draft)
    assert "Claude 🤖 = AI picks via API" not in source
    assert "| Claude →" not in source
    assert "**Claude's turn:**" not in source
    assert "**Claude (post-combat):**" not in source
