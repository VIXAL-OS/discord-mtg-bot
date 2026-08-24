"""DeepSeek's peak/off-peak billing window in the provider choice.

Announced Aug 22, 2026, effective 00:00 Beijing Sun Aug 23:
  * Weekdays (Mon-Fri Beijing) keep the existing peak/off-peak tiers.
  * Weekends (Sat-Sun Beijing) are charged at the OFF-PEAK rate all day.

The window is published; the peak RATE is not. So these pin the STRUCTURE and
deliberately assert the multiplier defaults to a no-op -- a guessed number
would be worse than none, because provider_cost_score decides which provider
runs a 160-game batch.
"""
import datetime as dt

import pytest

from rules.llm_adapter import (DEEPSEEK_PEAK_MULTIPLIER, _is_direct_deepseek,
                               deepseek_pricing_window, provider_cost_score)

UTC = dt.timezone.utc


def _utc(y, m, d, h):
    return dt.datetime(y, m, d, h, tzinfo=UTC)


class TestPricingWindow:
    @pytest.mark.parametrize("utc_hour,expected", [
        (2, "peak"),        # Mon 10:00 Beijing -- inside 09:00-12:00
        (7, "peak"),        # Mon 15:00 Beijing -- inside 14:00-18:00
        (5, "off_peak"),    # Mon 13:00 Beijing -- the gap between windows
        (12, "off_peak"),   # Mon 20:00 Beijing -- after both
        (20, "off_peak"),   # Tue 04:00 Beijing -- overnight
    ])
    def test_weekday_windows(self, utc_hour, expected):
        assert deepseek_pricing_window(_utc(2026, 8, 24, utc_hour)) == expected

    @pytest.mark.parametrize("day", [22, 23])   # Sat, Sun
    @pytest.mark.parametrize("utc_hour", [2, 7])   # both peak windows
    def test_weekends_are_off_peak_even_inside_a_peak_window(self, day,
                                                             utc_hour):
        """The whole point of the Aug 22 change."""
        assert deepseek_pricing_window(_utc(2026, 8, day, utc_hour)) == "off_peak"

    def test_the_weekend_is_decided_in_beijing_not_locally(self):
        """Friday 22:00 EDT is already Saturday in Beijing -- which is exactly
        when an overnight batch launches, so getting this backwards would
        misprice the most common case."""
        friday_evening_edt = _utc(2026, 8, 22, 2)   # Fri 22:00 EDT
        assert friday_evening_edt.astimezone(
            dt.timezone(dt.timedelta(hours=8))).weekday() == 5
        assert deepseek_pricing_window(friday_evening_edt) == "off_peak"

    def test_a_naive_datetime_is_read_as_utc(self):
        assert deepseek_pricing_window(dt.datetime(2026, 8, 24, 2)) == "peak"


class TestSurchargeScope:
    def test_only_direct_deepseek_is_surcharged(self):
        """DashScope resells the same model NAMES at Alibaba's rates, so the
        rate_key prefix is what keeps the two priced apart."""
        assert _is_direct_deepseek("deepseek-v4-flash")
        assert not _is_direct_deepseek("dashscope:deepseek-v4-flash")
        assert not _is_direct_deepseek("qwen3.7-flash")

    def test_the_multiplier_is_a_no_op_until_a_real_rate_is_published(self):
        """DeepSeek announced "a significant increase" with no figure. A
        guessed multiplier would silently steer a 160-game batch, so the
        default must stay 1.0 until MODEL_RATES carries the real number."""
        assert DEEPSEEK_PEAK_MULTIPLIER == 1.0

    def test_scoring_is_unchanged_while_the_multiplier_is_a_no_op(self):
        class _A:
            rate_key = "deepseek-v4-flash"
            model = "deepseek-v4-flash"

        peak = provider_cost_score(_A())
        assert peak > 0
        # Same adapter, same table: with a 1.0 multiplier the window cannot
        # change the number, which is what "no behaviour change" means.
        assert peak == provider_cost_score(_A())

    def test_a_published_multiplier_would_raise_the_peak_score(self,
                                                               monkeypatch):
        """The wiring must actually be load-bearing -- otherwise this is a
        declared constant with no consumer, which is the shape this codebase
        keeps rediscovering as dead code."""
        import rules.llm_adapter as la

        class _A:
            rate_key = "deepseek-v4-flash"
            model = "deepseek-v4-flash"

        base = provider_cost_score(_A())
        monkeypatch.setattr(la, "DEEPSEEK_PEAK_MULTIPLIER", 2.0)
        monkeypatch.setattr(la, "deepseek_pricing_window", lambda *a: "peak")
        assert provider_cost_score(_A()) == pytest.approx(base * 2.0)

        monkeypatch.setattr(la, "deepseek_pricing_window", lambda *a: "off_peak")
        assert provider_cost_score(_A()) == pytest.approx(base)

    def test_qwen_is_never_surcharged_whatever_the_window(self, monkeypatch):
        import rules.llm_adapter as la

        class _Q:
            rate_key = "qwen3.7-flash"
            model = "qwen3.7-flash"

        base = provider_cost_score(_Q())
        monkeypatch.setattr(la, "DEEPSEEK_PEAK_MULTIPLIER", 5.0)
        monkeypatch.setattr(la, "deepseek_pricing_window", lambda *a: "peak")
        assert provider_cost_score(_Q()) == pytest.approx(base)
