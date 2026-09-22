"""DeepSeek's peak/off-peak billing window in the provider choice.

Announced Aug 22, 2026, effective 00:00 Beijing Sun Aug 23:
  * Weekdays (Mon-Fri Beijing) keep the existing peak/off-peak tiers.
  * Weekends (Sat-Sun Beijing) are charged at the OFF-PEAK rate all day.

The window is published; the peak RATE is not. So these pin the STRUCTURE and
deliberately assert the multiplier defaults to a no-op -- a guessed number
would be worse than none, because provider_cost_score decides which provider
runs a 160-game batch.

Sep 21, 2026: the rate IS published now (peak = 2x off-peak on every item,
official pricing page), so the no-op pin became `test_the_published_multiplier
_is_two`, and Alibaba's V4.1 resale is peak-scored too.
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

    def test_the_published_multiplier_is_two(self):
        """Sep 21, 2026 — supersedes the Aug-24 "no-op until published" pin.
        DeepSeek's official pricing page now states off-peak is HALF of peak
        on every item, and MODEL_RATES moved to the real (off-peak) numbers
        in the same commit, which is the condition that pin was waiting on."""
        assert DEEPSEEK_PEAK_MULTIPLIER == 2.0

    def test_the_default_multiplier_doubles_the_peak_score(self, monkeypatch):
        import rules.llm_adapter as la

        class _A:
            rate_key = "deepseek-v4-flash"
            model = "deepseek-v4-flash"

        monkeypatch.setattr(la, "deepseek_pricing_window", lambda *a: "off_peak")
        base = provider_cost_score(_A())
        assert base > 0
        monkeypatch.setattr(la, "deepseek_pricing_window", lambda *a: "peak")
        assert provider_cost_score(_A()) == pytest.approx(base * 2.0)

    def test_resold_v41_is_peak_priced_resold_v4_and_qwen_are_not(self, monkeypatch):
        """Alibaba bills its deepseek-v4.1-flash resale busy/idle 2x as well
        (Sep 21 model-pricing page) — the failover must scale at peak too, or
        it reads half-price exactly when it is not. The old V4 resale row and
        Qwen stay flat."""
        import rules.llm_adapter as la

        class _R:
            def __init__(self, k):
                self.rate_key = self.model = k

        def ratio(k):
            monkeypatch.setattr(la, "deepseek_pricing_window", lambda *a: "off_peak")
            off = provider_cost_score(_R(k))
            monkeypatch.setattr(la, "deepseek_pricing_window", lambda *a: "peak")
            return provider_cost_score(_R(k)) / off

        assert ratio("dashscope:deepseek-v4.1-flash") == pytest.approx(2.0)
        assert ratio("dashscope:deepseek-v4-flash") == pytest.approx(1.0)
        assert ratio("qwen3.7-flash") == pytest.approx(1.0)

    def test_a_published_multiplier_would_raise_the_peak_score(self,
                                                               monkeypatch):
        """The wiring must actually be load-bearing -- otherwise this is a
        declared constant with no consumer, which is the shape this codebase
        keeps rediscovering as dead code."""
        import rules.llm_adapter as la

        class _A:
            rate_key = "deepseek-v4-flash"
            model = "deepseek-v4-flash"

        # Sep 22, 2026: take the base with the window pinned OFF-PEAK. With
        # the published 2.0 default, a base read at the real clock is already
        # doubled inside a peak window — the fork suite caught this running at
        # 01:40 UTC after both private runs had happened to land off-peak.
        monkeypatch.setattr(la, "deepseek_pricing_window", lambda *a: "off_peak")
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
