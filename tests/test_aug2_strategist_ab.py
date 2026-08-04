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
        """Aug 3: this contract is now enforced by construction rather than
        by two literals matching each other.

        It used to assert `strategist_model = "deepseek-v4-flash"` and
        `STRAT_INPUT_MISS_RATE = 0.14` appeared in autoplay.py — i.e. that
        someone had hand-synced the hardcoded rate to the hardcoded model.
        Both are gone: the swap block reads the model off the adapter and
        the rate is looked up from that string. So the assertion moves from
        "the text says Flash" to "the pricing genuinely follows whatever
        model the adapter carries", which is what the A/B actually needs and
        is what a provider switch would otherwise silently break.
        """
        pytest.importorskip("openai")
        from rules.llm_adapter import (create_deepseek_reasoner_adapter,
                                       rates_for_model)
        src = (REPO / "mtg" / "autoplay.py").read_text(encoding="utf-8")
        assert "rates_for_model" in src, (
            "pricing must be derived from the model that actually ran")
        assert "STRAT_INPUT_MISS_RATE = 0.14" not in src, (
            "a hardcoded strategist rate re-assumes the provider")
        assert 'strategist_model = "deepseek-v4-flash"' not in src, (
            "the swap block must read the model off the adapter")

        adapter = create_deepseek_reasoner_adapter(api_key="test-key-not-used")
        hit, miss, out = rates_for_model(adapter._model)
        assert (round(hit * 1e6, 4), round(miss * 1e6, 3),
                round(out * 1e6, 2)) == (0.0028, 0.14, 0.28), (
            "the strategist bills at Flash rates; Pro would over-report ~3x")

    def test_a_pro_strategist_would_reprice_itself(self):
        """The other half of "follows the model": if the revert path is ever
        taken, the rate must move on its own. Pinning this is what makes the
        indirection worth having rather than just longer."""
        from rules.llm_adapter import rates_for_model
        hit, miss, out = rates_for_model("deepseek-v4-pro")
        assert (round(hit * 1e6, 4), round(miss * 1e6, 3),
                round(out * 1e6, 2)) == (0.0036, 0.435, 0.87)
