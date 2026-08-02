"""Aug 2, 2026 — the strategist V4-Flash A/B (operator-approved).

The strategist's chronic instability (density nukes, deadman/hard-cap
fires, scaffolding leaks) has been V4-PRO reasoning all along; the 0731
Flash re-post-train reports agent scores passing V4-Pro-Preview at ~6x
lower cost. The A/B: strategist on V4-Flash THINKING mode for a batch.

Pinned contract:
- the reasoner factory builds deepseek-v4-flash with thinking EXPLICITLY
  enabled (flash defaults vary by endpoint — the cube-draft 0-token lesson:
  never rely on an implicit thinking default),
- the per-game degrade only sends reasoning_effort (a PRO knob) to pro
  models,
- the autoplay swap block + STRAT_* pricing follow the model.

Revert path (documented in the factory): five tagged edits.
"""
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


class TestStrategistFlashAB:
    def test_factory_builds_flash_thinking(self):
        pytest.importorskip("openai")
        from rules.llm_adapter import create_deepseek_reasoner_adapter
        adapter = create_deepseek_reasoner_adapter(api_key="test-key-not-used")
        assert adapter is not None
        assert adapter._model == "deepseek-v4-flash"
        assert adapter.messages._thinking_enabled is True, (
            "thinking must be EXPLICIT — flash defaults vary by endpoint")
        assert adapter.messages._reasoning_effort is None, (
            "reasoning_effort is a PRO knob; flash must not send it")

    def test_degrade_is_model_gated(self):
        src = (REPO / "mtg" / "claude_player.py").read_text(encoding="utf-8")
        i = src.index("STRATEGIST-DEGRADE")
        window = src[max(0, i - 800):i]
        assert "'pro' in (self.strategist_model or '')" in window, (
            "the reasoning_effort degrade must not fire on a flash "
            "strategist (flash rejects the knob)")

    def test_swap_block_and_pricing_follow_the_model(self):
        src = (REPO / "mtg" / "autoplay.py").read_text(encoding="utf-8")
        assert 'strategist_model = "deepseek-v4-flash"' in src
        assert "STRAT_INPUT_MISS_RATE = 0.14" in src, (
            "strategist bills at Flash rates now — Pro rates would "
            "over-report ~3x")
