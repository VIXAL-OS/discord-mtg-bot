"""Aug 3, 2026 — a second provider, and a pre-flight that picks between them.

The Aug 3 batch launched at Beijing 11:13, inside DeepSeek's declared
09:00-12:00 peak window, and took a burst of HTTP 503 "service is too busy"
plus a latency tail of 200-365s per call against a healthy ~8s median.
Nothing noticed, because provider choice was a startup constant.

Three things are pinned here:

1. DashScope (Qwen) builds through the SAME OpenAICompatibleAdapter as
   DeepSeek, so it preserves the actor/strategist split — the thing the
   OpenRouter route would have cost us.
2. Cost is derived from the MODEL STRING that actually ran. The rates used to
   be local constants hardcoded per ROLE, which silently assumed the
   provider; the moment a batch ran on anything else, every figure printed
   was wrong.
3. The probe DISQUALIFIES on error rather than averaging. The failure being
   dodged is 503, so an erroring provider is precisely the one not to pick.

The Qwen slugs are VERIFY-flagged: Alibaba's published list names
qwen3.7-max / qwen3.6-plus / qwen3.6-flash and does not confirm
`qwen3.7-flash`. That is exactly why the probe doubles as a validity check —
a wrong slug fails pre-flight and the batch falls back, rather than dying
mid-run.
"""
import asyncio
from types import SimpleNamespace

import pytest

from rules.llm_adapter import (MODEL_RATES, rates_for_model,
                               probe_adapter_latency, choose_fastest_provider)


class _FakeMessages:
    def __init__(self, latency, fail, fail_first):
        self._latency, self._fail = latency, fail
        self._fail_first = fail_first
        self.calls = 0

    def create(self, **kw):
        import time
        self.calls += 1
        if self._fail or self.calls <= self._fail_first:
            raise RuntimeError("Error code: 503 - service_unavailable_error")
        time.sleep(self._latency)
        return object()


class _FakeAdapter:
    def __init__(self, model="fake", latency=0.0, fail=False, fail_first=0):
        """fail_first models the INTERMITTENT case — some samples 503, some
        succeed. That is what DeepSeek was actually doing on Aug 3, and it
        is the only shape that reaches the errors==0 check: an adapter that
        fails every time returns early on an empty latency list, so a
        total-failure fixture cannot tell disqualification from that."""
        self.model = self._model = model
        self.messages = _FakeMessages(latency, fail, fail_first)


class TestRateTable:
    def test_rates_follow_the_model_string(self):
        hit, miss, out = rates_for_model("deepseek-v4-flash")
        assert round(miss * 1e6, 3) == 0.14
        hit, miss, out = rates_for_model("qwen3.7-flash")
        assert round(miss * 1e6, 3) == 0.028

    def test_longest_match_wins(self):
        """The slugs NEST — every specific model contains its family
        catch-all ('qwen3.7-flash' contains 'qwen'). Shortest-first matching
        would price every Qwen model at the premium catch-all rate, making
        Flash read 13x its true cost."""
        _, plus_miss, plus_out = rates_for_model("qwen3.7-plus")
        _, flash_miss, flash_out = rates_for_model("qwen3.7-flash")
        assert plus_miss > flash_miss
        assert round(plus_out * 1e6, 3) == 1.101
        assert round(flash_out * 1e6, 2) == 0.11, (
            "Flash must not inherit the family catch-all rate")
        # Same nesting on the DeepSeek side: the actor must not be priced
        # at the Pro catch-all.
        _, ds_flash_miss, _ = rates_for_model("deepseek-v4-flash")
        assert round(ds_flash_miss * 1e6, 3) == 0.14

    def test_unrecognised_family_member_still_prices_as_that_family(self):
        """The Qwen slugs are VERIFY-flagged. If the real callable name
        differs from the guess, it must still price as Qwen — at the premium
        tier, so an unknown tier over-reports."""
        _, miss, out = rates_for_model("qwen-some-future-slug")
        assert round(miss * 1e6, 3) == 0.276
        assert round(out * 1e6, 3) == 1.101

    def test_unknown_model_over_reports_rather_than_under(self):
        """An unpriced model must not read as cheap. A cost line that reads
        high prompts someone to add a rate; one that reads low is a silent
        lie."""
        hit, miss, out = rates_for_model("some-brand-new-model")
        assert hit == miss, "no phantom cache discount for an unknown model"
        assert round(miss * 1e6, 3) == 0.14

    def test_full_model_string_with_vendor_prefix_still_matches(self):
        """OpenRouter-style 'vendor/model' strings must not fall through."""
        _, miss, _ = rates_for_model("alibaba/qwen3.7-flash")
        assert round(miss * 1e6, 3) == 0.028


class TestLatencyProbe:
    def test_unconfigured_provider_is_not_usable(self):
        r = asyncio.run(probe_adapter_latency(None))
        assert r["ok"] is False
        assert r["median_ms"] is None

    def test_errors_disqualify_even_when_fast(self):
        """A 503 must lose to a slow-but-clean provider. Averaging an error
        into a latency number is how you pick the congested vendor."""
        fast_broken = _FakeAdapter("broken", latency=0.0, fail=True)
        slow_ok = _FakeAdapter("slow", latency=0.05)
        winner, results = asyncio.run(choose_fastest_provider(
            [("broken", fast_broken), ("slow", slow_ok)], samples=2))
        assert winner == "slow"
        assert results["broken"]["ok"] is False

    def test_INTERMITTENT_errors_disqualify_too(self):
        """The decisive case, and the one that actually happened: a provider
        that 503s on some calls and answers others, FAST. It still has a
        median latency, so it reaches the errors==0 check rather than the
        empty-latency early return — and it must still lose to the slower
        clean provider, because intermittent 503s are the whole problem."""
        flaky_fast = _FakeAdapter("flaky", latency=0.0, fail_first=1)
        slow_clean = _FakeAdapter("slow", latency=0.05)
        winner, results = asyncio.run(choose_fastest_provider(
            [("flaky", flaky_fast), ("slow", slow_clean)], samples=2))
        assert results["flaky"]["median_ms"] is not None, (
            "fixture must produce a latency, else it tests the wrong branch")
        assert results["flaky"]["errors"] == 1
        assert results["flaky"]["ok"] is False
        assert winner == "slow"

    def test_fastest_healthy_provider_wins(self):
        slow = _FakeAdapter("slow", latency=0.12)
        quick = _FakeAdapter("quick", latency=0.0)
        winner, _ = asyncio.run(choose_fastest_provider(
            [("slow", slow), ("quick", quick)], samples=2))
        assert winner == "quick"

    def test_all_broken_returns_none_not_a_coin_flip(self):
        """None means "keep whatever default you had". Returning an
        arbitrary pick here would silently move a batch onto a provider we
        just measured as broken."""
        a = _FakeAdapter("a", fail=True)
        b = _FakeAdapter("b", fail=True)
        winner, results = asyncio.run(choose_fastest_provider(
            [("a", a), ("b", b)], samples=1))
        assert winner is None
        assert all(not r["ok"] for r in results.values())

    def test_none_adapters_are_skipped_not_crashed(self):
        """The normal case today: DASHSCOPE_API_KEY unset, so the Qwen
        adapters are None and the probe must simply not see them."""
        ok = _FakeAdapter("ok", latency=0.0)
        winner, results = asyncio.run(choose_fastest_provider(
            [("deepseek", ok), ("qwen", None)], samples=1))
        assert winner == "deepseek"
        assert "qwen" not in results

    def test_probe_is_cheap(self):
        """Pre-flight must cost a handful of tokens, not a real generation."""
        a = _FakeAdapter("a", latency=0.0)
        asyncio.run(probe_adapter_latency(a, samples=3))
        assert a.messages.calls == 3


class TestDashScopeFactories:
    def test_returns_none_without_a_key(self, monkeypatch):
        """Graceful degradation is the whole reason this can ship before the
        key exists — an unconfigured provider must never raise at startup."""
        from rules.llm_adapter import create_dashscope_adapter
        monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
        monkeypatch.delenv("ALIBABA_API_KEY", raising=False)
        assert create_dashscope_adapter("qwen3.7-flash") is None

    def test_actor_and_strategist_are_distinct_models(self, monkeypatch):
        """The point of DashScope over OpenRouter: two slugs, so the Phase-3
        actor/strategist split survives the provider switch."""
        pytest.importorskip("openai")
        monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key-not-used")
        from rules.llm_adapter import (create_qwen_actor_adapter,
                                       create_qwen_strategist_adapter)
        actor = create_qwen_actor_adapter()
        strat = create_qwen_strategist_adapter()
        assert actor is not None and strat is not None
        assert actor._model != strat._model
        assert actor.messages._thinking_enabled is False, (
            "actor emits JSON — thinking must be EXPLICITLY off, never left "
            "to an endpoint default")
        assert strat.messages._thinking_enabled is True

    def test_every_shipped_slug_has_a_rate(self, monkeypatch):
        """A model we can select but cannot price is how cost reporting goes
        quietly wrong."""
        pytest.importorskip("openai")
        monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key-not-used")
        from rules.llm_adapter import (create_qwen_actor_adapter,
                                       create_qwen_strategist_adapter,
                                       create_deepseek_adapter,
                                       create_deepseek_reasoner_adapter)
        monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key-not-used")
        for factory in (create_qwen_actor_adapter,
                        create_qwen_strategist_adapter,
                        create_deepseek_adapter,
                        create_deepseek_reasoner_adapter):
            adapter = factory()
            assert adapter is not None
            model = adapter._model
            assert any(k in model.lower() for k in MODEL_RATES), (
                f"{model} is selectable but has no MODEL_RATES entry")


def _cost_tracker():
    from bot import MTGBot
    counters = (
        "total_input_tokens", "total_output_tokens", "api_calls",
        "mtg_game_input_tokens", "mtg_game_output_tokens", "mtg_game_calls",
        "qwen_input_tokens", "qwen_output_tokens", "qwen_calls",
        "qwen_plus_input_tokens", "qwen_plus_output_tokens",
        "qwen_plus_calls", "qwen_max_input_tokens",
        "qwen_max_output_tokens", "qwen_max_calls",
        "mtg_game_qwen_input_tokens",
        "mtg_game_qwen_output_tokens", "mtg_game_qwen_plus_input_tokens",
        "mtg_game_qwen_plus_output_tokens", "mtg_game_qwen_max_input_tokens",
        "mtg_game_qwen_max_output_tokens", "sonnet_input_tokens",
        "sonnet_output_tokens", "mtg_game_sonnet_input_tokens",
        "mtg_game_sonnet_output_tokens", "deepseek_input_tokens",
        "deepseek_output_tokens", "deepseek_calls",
        "deepseek_pro_input_tokens", "deepseek_pro_output_tokens",
        "deepseek_pro_calls", "mtg_game_deepseek_input_tokens",
        "mtg_game_deepseek_output_tokens",
        "mtg_game_deepseek_pro_input_tokens",
        "mtg_game_deepseek_pro_output_tokens",
    )
    tracker = SimpleNamespace(**{name: 0 for name in counters})
    tracker._save_persistent_costs = lambda: None
    return MTGBot, tracker


class TestCostRoutingInBot:
    def test_qwen_does_not_fall_into_the_sonnet_bucket(self):
        """_track_usage routed on 'deepseek' in model. A Qwen string failed
        that and fell through to the ELSE branch — the Sonnet bucket —
        pricing $0.03/M tokens at Sonnet rates, ~36x high."""
        MTGBot, tracker = _cost_tracker()
        usage = SimpleNamespace(input_tokens=11, output_tokens=7)
        MTGBot.track_mtg_usage(tracker, usage, "qwen3.7-flash")
        assert (tracker.qwen_input_tokens, tracker.qwen_output_tokens,
                tracker.qwen_calls) == (11, 7, 1)
        assert tracker.sonnet_input_tokens == 0
        assert tracker.deepseek_input_tokens == 0
        assert tracker.deepseek_pro_input_tokens == 0

    def test_qwen_premium_tier_is_recognised(self):
        """'plus' does NOT contain '-pro', so without naming it a Qwen
        strategist bills at actor rates — the quiet direction of wrong."""
        MTGBot, tracker = _cost_tracker()
        usage = SimpleNamespace(input_tokens=13, output_tokens=5)
        MTGBot.track_mtg_usage(tracker, usage, "qwen3.7-plus")
        assert (tracker.qwen_plus_input_tokens,
                tracker.qwen_plus_output_tokens,
                tracker.qwen_plus_calls) == (13, 5, 1)
        assert tracker.qwen_input_tokens == 0
        assert tracker.sonnet_input_tokens == 0
        assert tracker.deepseek_pro_input_tokens == 0

    def test_qwen_max_has_its_own_rate_bucket(self):
        MTGBot, tracker = _cost_tracker()
        usage = SimpleNamespace(input_tokens=17, output_tokens=9)
        MTGBot.track_mtg_usage(tracker, usage, "qwen3.7-max")
        assert (tracker.qwen_max_input_tokens,
                tracker.qwen_max_output_tokens,
                tracker.qwen_max_calls) == (17, 9, 1)
        assert tracker.qwen_plus_input_tokens == 0
        assert tracker.mtg_game_qwen_max_input_tokens == 17


class TestResoldModelPricing:
    """Aug 4: DashScope RESELLS deepseek-v4-flash under the identical model
    string at its own rates. Pricing by model alone would bill an
    Alibaba-hosted DeepSeek at DeepSeek's own prices — and the gap is not
    cosmetic, because Alibaba's cache rate is 10x worse."""

    def test_resold_deepseek_is_priced_separately(self):
        own_hit, own_miss, own_out = rates_for_model("deepseek-v4-flash")
        res_hit, res_miss, res_out = rates_for_model(
            "dashscope:deepseek-v4-flash")
        assert res_hit != own_hit, (
            "the resold model must not inherit DeepSeek's cache rate")
        assert round(res_hit * 1e6, 3) == 0.028
        assert round(own_hit * 1e6, 4) == 0.0028

    def test_provider_scoped_key_beats_the_substring_pass(self):
        """'dashscope:deepseek-v4-flash' CONTAINS 'deepseek-v4-flash', so
        without an exact-match pass first the substring walk would price it
        at DeepSeek's own rates."""
        hit, _, _ = rates_for_model("dashscope:deepseek-v4-flash")
        assert round(hit * 1e6, 3) == 0.028

    def test_resold_is_dearer_blended_which_is_the_tradeoff(self):
        """The failover is worth having for ZERO quality risk, not price."""
        HIT = 0.73
        def blended(k):
            h, m, _ = rates_for_model(k)
            return HIT * h + (1 - HIT) * m
        assert blended("dashscope:deepseek-v4-flash") > blended("deepseek-v4-flash")

    def test_rate_key_defaults_to_model(self, monkeypatch):
        pytest.importorskip("openai")
        monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key-not-used")
        from rules.llm_adapter import create_deepseek_adapter
        a = create_deepseek_adapter()
        assert a.rate_key == a._model

    def test_resold_factory_sets_the_scoped_rate_key(self, monkeypatch):
        pytest.importorskip("openai")
        monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key-not-used")
        from rules.llm_adapter import create_dashscope_deepseek_actor_adapter
        a = create_dashscope_deepseek_actor_adapter()
        assert a is not None
        assert a._model == "deepseek-v4-flash", "the API needs the real slug"
        assert a.rate_key == "dashscope:deepseek-v4-flash", (
            "but pricing must know it came from Alibaba")


class TestRegionResolution:
    """Aug 4: a Frankfurt key against the Singapore host returns 401 with a
    message that reads like a bad key. Regions are independent and their
    keys are not interchangeable; Frankfurt/Tokyo/US additionally embed a
    workspace id in the hostname."""

    def _url(self, monkeypatch, **env):
        from rules.llm_adapter import _dashscope_base_url
        for k in ("DASHSCOPE_BASE_URL", "DASHSCOPE_REGION",
                  "DASHSCOPE_WORKSPACE_ID"):
            monkeypatch.delenv(k, raising=False)
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        return _dashscope_base_url()

    def test_default_is_singapore(self, monkeypatch):
        assert "dashscope-intl" in self._url(monkeypatch)

    def test_explicit_override_wins(self, monkeypatch):
        u = self._url(monkeypatch, DASHSCOPE_BASE_URL="https://example/v1",
                      DASHSCOPE_REGION="cn")
        assert u == "https://example/v1"

    def test_workspace_regions_embed_the_workspace_id(self, monkeypatch):
        u = self._url(monkeypatch, DASHSCOPE_REGION="us-east-1",
                      DASHSCOPE_WORKSPACE_ID="ws-abc")
        assert u == ("https://ws-abc.us-east-1.maas.aliyuncs.com"
                     "/compatible-mode/v1")

    def test_region_aliases(self, monkeypatch):
        for alias, canon in (("frankfurt", "eu-central-1"),
                             ("tokyo", "ap-northeast-1"),
                             ("us-virginia", "us-east-1")):
            u = self._url(monkeypatch, DASHSCOPE_REGION=alias,
                          DASHSCOPE_WORKSPACE_ID="w")
            assert f".{canon}.maas.aliyuncs.com" in u

    def test_missing_workspace_id_warns_and_does_not_build_a_broken_host(
            self, monkeypatch, capsys):
        """Silently emitting 'https://.us-east-1...' would fail with a DNS
        error that looks nothing like the real problem."""
        u = self._url(monkeypatch, DASHSCOPE_REGION="us-east-1")
        assert "://." not in u
        assert "DASHSCOPE_WORKSPACE_ID" in capsys.readouterr().out


class TestInterWaveFailover:
    """The launch probe alone is insufficient: on Aug 3 DeepSeek measured
    healthy at 23:00 and degraded minutes later, and provider choice was
    fixed at launch so nothing noticed."""

    def _src(self):
        from pathlib import Path
        return (Path(__file__).resolve().parent.parent
                / "mtg" / "cog.py").read_text(encoding="utf-8")

    def test_the_wave_loop_reprobes(self):
        src = self._src()
        i = src.index("for wave_start in range(0, len(matchups)")
        window = src[i:i + 2200]
        assert "_select_batch_provider()" in window, (
            "no re-probe between waves — a mid-batch degradation is invisible")

    def test_it_skips_the_first_wave(self):
        """The launch probe just ran; re-probing immediately is pure cost."""
        src = self._src()
        i = src.index("for wave_start in range(0, len(matchups)")
        assert "wave_start > 0" in src[i:i + 2200]

    def test_a_probe_failure_cannot_kill_the_batch(self):
        """The probe exists to keep a batch alive; letting it raise would
        give it the power to end one."""
        src = self._src()
        i = src.index("for wave_start in range(0, len(matchups)")
        window = src[i:i + 2200]
        assert "except Exception" in window
        assert "staying on" in window

    def test_the_switch_is_announced(self):
        src = self._src()
        i = src.index("for wave_start in range(0, len(matchups)")
        assert "Provider switched" in src[i:i + 2200]


class TestOperatorPin:
    """MTG_FORCE_PROVIDER. The probe optimises for LATENCY, which is not
    always the goal — a deliberate A/B must not be overruled because the
    other provider answered a ping 200ms quicker."""

    def _src(self):
        from pathlib import Path
        return (Path(__file__).resolve().parent.parent
                / "mtg" / "cog.py").read_text(encoding="utf-8")

    def test_the_pin_exists_and_is_checked_before_the_probe(self):
        src = self._src()
        i = src.index("async def _select_batch_provider")
        window = src[i:i + 3000]
        assert "MTG_FORCE_PROVIDER" in window
        assert window.index("MTG_FORCE_PROVIDER") < window.index(
            "choose_fastest_provider is None"), (
            "the pin must short-circuit before the latency probe")

    def test_a_pinned_provider_is_still_health_checked(self):
        """Pinning a BROKEN provider must fail loudly, not park a batch the
        way Aug 3 did."""
        src = self._src()
        i = src.index("async def _select_batch_provider")
        window = src[i:i + 3000]
        assert "probe_adapter_latency" in window
        assert "failed its" in window

    def test_an_unconfigured_pin_falls_back_rather_than_crashing(self):
        src = self._src()
        i = src.index("async def _select_batch_provider")
        window = src[i:i + 3000]
        assert "is not " in window and "ignoring the pin" in window


class TestSwapUsesTheRealModelString:
    """Aug 4 LIVE BUG. The pin said qwen and the games ran DeepSeek.

    The adapter class only ever exposed `_model`, while the autoplay swap
    block read `getattr(adapter, 'model', 'deepseek-v4-flash')` — so it got
    the FALLBACK for every adapter ever constructed. A Qwen-pinned batch set
    claude_ai.model to 'deepseek-v4-flash': right adapter, wrong model
    string, and DashScope served it happily because it hosts that model too.

    The existing tests could not catch it because their fake adapters set
    `self.model = self._model = model` — defining BOTH names, so the real
    class's missing attribute was invisible. That is the fake-object trap:
    a stub must copy the real field names, not the ones the code wishes for.
    """

    def test_real_adapters_expose_model_publicly(self, monkeypatch):
        pytest.importorskip("openai")
        monkeypatch.setenv("DEEPSEEK_API_KEY", "k")
        monkeypatch.setenv("DASHSCOPE_API_KEY", "k")
        from rules.llm_adapter import (create_deepseek_adapter,
                                       create_deepseek_reasoner_adapter,
                                       create_qwen_actor_adapter,
                                       create_qwen_strategist_adapter,
                                       create_dashscope_deepseek_actor_adapter)
        for factory in (create_deepseek_adapter, create_deepseek_reasoner_adapter,
                        create_qwen_actor_adapter, create_qwen_strategist_adapter,
                        create_dashscope_deepseek_actor_adapter):
            a = factory()
            assert a is not None
            assert a.model == a._model, f"{factory.__name__} model mismatch"
            assert a.model, "a blank model string would be sent to the API"

    def test_qwen_adapters_report_qwen_not_deepseek(self, monkeypatch):
        """The specific assertion whose absence let the bug ship."""
        pytest.importorskip("openai")
        monkeypatch.setenv("DASHSCOPE_API_KEY", "k")
        from rules.llm_adapter import (create_qwen_actor_adapter,
                                       create_qwen_strategist_adapter)
        assert create_qwen_actor_adapter().model.startswith("qwen")
        assert create_qwen_strategist_adapter().model.startswith("qwen")

    def test_swap_block_has_no_model_fallback_string(self):
        """A wrong-but-plausible default is what made this silent. If an
        adapter cannot name its model we want the AttributeError."""
        from pathlib import Path
        src = (Path(__file__).resolve().parent.parent
               / "mtg" / "autoplay.py").read_text(encoding="utf-8")
        assert "getattr(use_alt_adapter, 'model'" not in src
        assert "'model', 'deepseek-v4-flash'" not in src


class TestCostStatsFollowTheProvider:
    """Aug 4, found by reading a live batch: the first Qwen batch reported
    `[STATS-CUMULATIVE] calls=0 est_cost=$0.0000` and per-game deltas of
    calls=-1, prompt_tokens=-22.

    The end-of-game stats were hardcoded to the DEEPSEEK adapters while the
    game-start BASELINE already used the running adapter. So on Qwen the
    delta was idle_deepseek(0) - qwen_start(N), i.e. negative — and the
    cumulative line read zero. Silent zeroes on the exact number a provider
    A/B exists to measure."""

    def _src(self):
        from pathlib import Path
        return (Path(__file__).resolve().parent.parent
                / "mtg" / "autoplay.py").read_text(encoding="utf-8")

    def test_end_stats_read_the_running_adapter(self):
        src = self._src()
        assert "stats = cog._deepseek_adapter.get_stats()" not in src, (
            "hardcoding the DeepSeek adapter reports zero on any other provider")
        assert "use_alt_adapter.get_stats()" in src

    def test_strategist_stats_read_the_running_strategist(self):
        src = self._src()
        assert "strat_stats = cog._deepseek_reasoner_adapter.get_stats()" not in src
        assert "_batch_strategist.get_stats()" in src

    def test_baseline_and_end_use_the_same_adapter(self):
        """The mismatch is what made the deltas NEGATIVE rather than merely
        wrong — a sign error is the tell that two different counters were
        being subtracted."""
        src = self._src()
        assert "_game_start_stats = use_alt_adapter.get_stats().copy()" in src
