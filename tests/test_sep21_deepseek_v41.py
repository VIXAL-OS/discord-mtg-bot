"""Sep 21, 2026 — DeepSeek V4.1 Flash repricing, the failover's model, and
DashScope cache hits.

Four mechanical facts motivated it, each verified live on this account:

1. DeepSeek launched V4.1 Flash on Sep 10 and now serves it behind the
   legacy `deepseek-v4-flash` alias (the API reports model=deepseek-flash),
   at new list rates (0.003 hit / 0.15 miss / 0.60 out, off-peak) with the
   long-announced peak surcharge finally published: 2x on every item.
2. Alibaba's `deepseek-v4-flash` is STILL V4, so the "identical model on
   different infrastructure" failover had quietly become a different model.
   Alibaba sells V4.1 separately as `deepseek-v4.1-flash`, busy/idle 2x.
3. DashScope reports cache hits only in the OpenAI-standard
   `prompt_tokens_details.cached_tokens`, which the adapter never read — so
   every Qwen hit was invisible and its implicit-cache rate dead.
4. The lifetime !cost priced V4.1 tokens at V4's calibrated $0.28/M output
   (real: $0.60). Ported from discord-companion-bot d92128d: an exact-priced
   V4.1 bucket, with the calibrated buckets frozen as history.

Behavioral pins drive the real function/method bodies with duck-typed
collaborators; no Discord, no network.
"""
import datetime as dt
from types import SimpleNamespace

import pytest

import rules.llm_adapter as la
from rules.llm_adapter import (MODEL_RATES, _cache_split, _Usage,
                               deepseek_v41_call_cost, rates_for_model)

UTC = dt.timezone.utc
SAT_NOON = dt.datetime(2026, 9, 26, 12, tzinfo=UTC)      # Beijing Sat -> off-peak
MON_0200 = dt.datetime(2026, 9, 21, 2, tzinfo=UTC)       # Beijing Mon 10:00 -> peak


# --- 1. rates -------------------------------------------------------------

class TestV41Rates:
    def test_alias_and_canonical_name_price_identically(self):
        assert MODEL_RATES["deepseek-v4-flash"] == MODEL_RATES["deepseek-flash"] \
            == (0.003, 0.15, 0.60)

    def test_canonical_name_does_not_fall_to_the_pro_catch_all(self):
        """'deepseek-flash' is not a substring of 'deepseek-v4-flash', so
        without its own row it would price at the V4-Pro family catch-all
        — ~4x high on input, ~3x on output."""
        _, miss, out = rates_for_model("deepseek-flash")
        assert (round(miss * 1e6, 3), round(out * 1e6, 2)) == (0.15, 0.6)

    def test_qwen38_flash_is_priced_not_caught_all(self):
        """So QWEN_*_MODEL=qwen3.8-flash reprices [STATS-*] by itself."""
        _, miss, out = rates_for_model("qwen3.8-flash")
        assert (round(miss * 1e6, 3), round(out * 1e6, 3)) == (0.113, 0.382)

    def test_the_multiplier_is_published(self):
        assert la.DEEPSEEK_PEAK_MULTIPLIER == 2.0


# --- 2. the failover is the same model again ------------------------------

class TestFailoverIsV41:
    @pytest.mark.parametrize("factory", [
        "create_dashscope_deepseek_actor_adapter",
        "create_dashscope_deepseek_strategist_adapter",
    ])
    def test_both_roles_run_v41_and_price_as_resale(self, factory, monkeypatch):
        pytest.importorskip("openai")
        monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key-not-used")
        a = getattr(la, factory)()
        assert a._model == "deepseek-v4.1-flash"
        assert a.rate_key == "dashscope:deepseek-v4.1-flash"

    def test_direct_stays_cheaper_than_the_resale_at_peak_too(self, monkeypatch):
        class _R:
            def __init__(self, k):
                self.rate_key = self.model = k
        for window in ("off_peak", "peak"):
            monkeypatch.setattr(la, "deepseek_pricing_window", lambda *a, w=window: w)
            assert la.provider_cost_score(_R("deepseek-v4-flash")) < \
                la.provider_cost_score(_R("dashscope:deepseek-v4.1-flash")), window


# --- 3. DashScope cache hits ----------------------------------------------

class TestCacheSplit:
    @pytest.mark.parametrize("usage, expected", [
        (SimpleNamespace(prompt_tokens=100), None),                          # no cache data
        (SimpleNamespace(prompt_tokens=100, prompt_cache_hit_tokens=70,
                         prompt_cache_miss_tokens=30), (70, 30)),            # DeepSeek
        (SimpleNamespace(prompt_tokens=100,
                         prompt_tokens_details={"cached_tokens": 64}), (64, 36)),   # DashScope
        (SimpleNamespace(prompt_tokens=100,
                         prompt_tokens_details=SimpleNamespace(cached_tokens=64)), (64, 36)),
        (SimpleNamespace(prompt_tokens=100, prompt_cache_hit_tokens=70,
                         prompt_cache_miss_tokens=30,
                         prompt_tokens_details={"cached_tokens": 70}), (70, 30)),   # both: never summed
        (SimpleNamespace(prompt_tokens=100,
                         prompt_tokens_details={"cached_tokens": 500}), (100, 0)),  # clamped
        (SimpleNamespace(prompt_tokens=100,
                         prompt_tokens_details={"cached_tokens": None}), None),
        (SimpleNamespace(prompt_tokens=100,
                         prompt_tokens_details={"cached_tokens": "x"}), None),
    ])
    def test_shapes(self, usage, expected):
        assert _cache_split(usage) == expected

    def test_usage_carries_hits_as_a_subset_of_input(self):
        u = _Usage(1000, 10, 700)
        assert (u.input_tokens, u.output_tokens, u.prompt_cache_hit_tokens) == (1000, 10, 700)
        assert _Usage(5, 1).prompt_cache_hit_tokens == 0


# --- 4. exact per-call V4.1 pricing + the lifetime bucket -----------------

class TestV41CallCost:
    def test_off_peak_and_peak(self):
        cost, peak = deepseek_v41_call_cost("deepseek-v4-flash", 1_000_000, 0,
                                            1_000_000, now_utc=SAT_NOON)
        assert not peak and cost == pytest.approx(0.75)
        cost, peak = deepseek_v41_call_cost("deepseek-v4-flash", 1_000_000, 0,
                                            1_000_000, now_utc=MON_0200)
        assert peak and cost == pytest.approx(1.50)

    def test_hits_bill_at_the_cache_rate_and_clamp(self):
        cost, _ = deepseek_v41_call_cost("deepseek-v4-flash", 1_000_000, 800_000, 0,
                                         now_utc=SAT_NOON)
        assert cost == pytest.approx(0.2 * 0.15 + 0.8 * 0.003)
        cost, _ = deepseek_v41_call_cost("deepseek-v4-flash", 100, 500, 0, now_utc=SAT_NOON)
        assert cost == pytest.approx(100 * 0.003 / 1e6)

    def test_the_resale_prices_at_alibabas_rates(self):
        cost, _ = deepseek_v41_call_cost("deepseek-v4.1-flash", 1_000_000, 0,
                                         1_000_000, now_utc=SAT_NOON)
        assert cost == pytest.approx(0.141 + 0.565)


def _bot_class():
    """The bot's commands.Bot subclass — FOUND rather than named, so this
    file stays wholesale-copyable into the public fork, whose class has a
    different name."""
    import bot
    from discord.ext import commands
    return next(v for v in vars(bot).values()
                if isinstance(v, type) and issubclass(v, commands.Bot)
                and v.__module__ == bot.__name__)


class _Tracker:
    """Duck-typed cost tracker: any counter not yet set reads 0, so the real
    bot methods run unbound against it."""
    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return 0

    def _save_persistent_costs(self):
        pass


def _track(tracker, model, inp, out, hits=0):
    _bot_class().track_mtg_usage(tracker, _Usage(inp, out, hits), model)


class TestLifetimeV41Bucket:
    def test_routing(self):
        t = _Tracker()
        _track(t, "deepseek-v4-flash", 1_000_000, 1_000_000, 800_000)
        _track(t, "deepseek-v4.1-flash", 1000, 10)              # Alibaba failover
        _track(t, "deepseek-v4-pro", 500, 5)
        assert (t.deepseek_v41_calls, t.deepseek_v41_input_tokens,
                t.deepseek_v41_cache_hit_tokens) == (2, 1_001_000, 800_000)
        assert t.deepseek_calls == 0 and t.deepseek_input_tokens == 0   # frozen V4 history
        assert t.deepseek_pro_calls == 1
        assert t.mtg_game_deepseek_v41_cost == pytest.approx(t.deepseek_v41_cost)

    def test_cost_is_exact_at_the_real_clock(self):
        t = _Tracker()
        _track(t, "deepseek-v4-flash", 1_000_000, 1_000_000, 800_000)
        off, _ = deepseek_v41_call_cost("deepseek-v4-flash", 1_000_000, 800_000,
                                        1_000_000, now_utc=SAT_NOON)
        assert t.deepseek_v41_cost in (pytest.approx(off), pytest.approx(2 * off))
        assert t.deepseek_v41_peak_calls in (0, 1)

    def test_persistence_round_trip(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        t = _Tracker()
        _track(t, "deepseek-v4-flash", 1_000_000, 1_000_000, 800_000)
        _bot_class()._save_persistent_costs(t)
        t2 = _Tracker()
        _bot_class()._load_persistent_costs(t2)
        assert (t2.deepseek_v41_calls, t2.deepseek_v41_cache_hit_tokens,
                t2.mtg_game_deepseek_v41_input_tokens) == (1, 800_000, 1_000_000)
        assert t2.deepseek_v41_cost == pytest.approx(t.deepseek_v41_cost)

    def test_cost_summary_counts_v41_once_and_not_as_a_remainder(self):
        """Format-agnostic on purpose (the private and fork !cost layouts
        differ): with ONLY V4.1 usage recorded, every dollar figure in the
        summary must be $0 or exactly the V4.1 cost. A double count, or V4.1
        MTG tokens falling into the Opus/Sonnet remainder (the May-14 class),
        produces some other figure and fails here in either layout."""
        import re
        t = _Tracker()
        _track(t, "deepseek-v4-flash", 1_000_000, 1_000_000, 800_000)
        s = _bot_class().get_cost_summary(t)
        assert "V4.1 Flash:" in s and "800,000 cached" in s
        v41 = f"{t.deepseek_v41_cost:.4f}"
        amounts = set(re.findall(r"\$([0-9]+\.[0-9]{4})", s))
        assert v41 in amounts
        assert amounts <= {v41, "0.0000"}, amounts
