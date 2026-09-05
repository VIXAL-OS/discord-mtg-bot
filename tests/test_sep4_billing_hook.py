"""Billing / balance errors reach a human (Sep 4, 2026).

FORK NOTE: the private repo pins this alongside its distress-detection
changes in one file; the distress half imports that bot's private class, so
only the provider-facing half is carried here — the engine classifier and
registry in mtg.util, the circuit-breaker hook in mtg.claude_player, and
MTGBot's maintainer DM.

The live shape that motivated it: an Anthropic account ran dry mid-session
and the error was an HTTP **400** (``invalid_request_error: Your credit
balance is too low``), so a status code cannot classify it — only the text
can. DeepSeek returns 402 ``Insufficient Balance``; DashScope reports the
account "in arrears". The DM is once per provider per cooldown, to config
``maintainer_user_id`` or else the application owner, and never raises.
"""
import asyncio
from datetime import timedelta

import pytest

from bot import CONFIG, MTGBot, is_billing_error
from mtg.claude_player import ClaudePlayer
from mtg.util import (_BILLING_ALERT_CALLBACKS, looks_like_billing_error,
                      notify_billing_error, register_billing_alert_callback)

LIVE_ANTHROPIC = (
    "Error code: 400 - {'type': 'error', 'error': {'type': "
    "'invalid_request_error', 'message': 'Your credit balance is too low to "
    "access the Anthropic API. Please go to Plans & Billing to upgrade or "
    "purchase credits.'}, 'request_id': 'req_x'}")
LIVE_DEEPSEEK = (
    "Error code: 402 - {'error': {'message': 'Insufficient Balance', "
    "'type': 'unknown_error', 'param': None, 'code': 'invalid_request_error'}}")


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class TestBillingClassifier:
    def test_live_anthropic_400_is_billing(self):
        assert is_billing_error(RuntimeError(LIVE_ANTHROPIC))

    def test_live_deepseek_402_is_billing(self):
        assert is_billing_error(RuntimeError(LIVE_DEEPSEEK))

    def test_402_status_code_is_billing(self):
        class _E(Exception):
            status_code = 402
        assert is_billing_error(_E("Payment Required"))

    def test_dashscope_arrears_is_billing(self):
        assert is_billing_error(RuntimeError(
            "Error code: 400 - {'code': 'Arrearage', 'message': "
            "'Access denied, please make sure your account is in good standing.'}"))

    @pytest.mark.parametrize("text", [
        "Error code: 429 - {'error': {'message': 'Rate limit reached'}}",
        "Request timed out.",
        "Connection reset by peer",
        "Error code: 503 - service is too busy",
    ])
    def test_transients_are_not_billing(self, text):
        assert not is_billing_error(RuntimeError(text))

    def test_bot_uses_the_engine_classifier(self):
        assert is_billing_error is looks_like_billing_error, "one regex, not two that drift"


class _Owner:
    def __init__(self):
        self.sent = []

    async def send(self, text):
        self.sent.append(text)


def _alert_duck(*, maintainer_id=None, cached=None, fetched=None, owner=None):
    class _D:
        def __init__(self):
            self._billing_alert_last = {}
            self.maintainer_user_id = maintainer_id
            self.owner = owner if owner is not None else _Owner()
            self.fetch_calls = []

        def get_user(self, uid):
            return cached

        async def fetch_user(self, uid):
            self.fetch_calls.append(uid)
            return fetched

        async def application_info(self):
            class _Info:
                pass
            info = _Info()
            info.owner = self.owner
            return info

    return _D()


class TestMaintainerAlert:
    def test_dms_the_app_owner_when_no_maintainer_configured(self):
        d = _alert_duck()
        assert _run(MTGBot._maybe_billing_alert(d, "anthropic", RuntimeError(LIVE_ANTHROPIC))) is True
        assert len(d.owner.sent) == 1
        assert "Anthropic" in d.owner.sent[0] and "credit balance" in d.owner.sent[0]

    def test_configured_maintainer_wins_over_owner(self):
        target = _Owner()
        d = _alert_duck(maintainer_id=1234, fetched=target)
        _run(MTGBot._maybe_billing_alert(d, "deepseek-v4-flash", RuntimeError(LIVE_DEEPSEEK)))
        assert len(target.sent) == 1 and d.owner.sent == []
        assert d.fetch_calls == [1234]
        assert "DeepSeek" in target.sent[0]

    def test_cached_user_skips_the_fetch(self):
        target = _Owner()
        d = _alert_duck(maintainer_id=1, cached=target, fetched=_Owner())
        _run(MTGBot._maybe_billing_alert(d, "qwen3.7-flash", RuntimeError(LIVE_DEEPSEEK)))
        assert len(target.sent) == 1 and d.fetch_calls == []
        assert "Qwen" in target.sent[0]

    def test_one_dm_per_provider_per_cooldown(self):
        d = _alert_duck()
        first = _run(MTGBot._maybe_billing_alert(d, "anthropic", RuntimeError("credit balance")))
        second = _run(MTGBot._maybe_billing_alert(d, "claude-haiku-4-5", RuntimeError("credit balance")))
        assert (first, second) == (True, False)
        assert len(d.owner.sent) == 1
        assert _run(MTGBot._maybe_billing_alert(d, "deepseek-v4-flash", RuntimeError(LIVE_DEEPSEEK))) is True
        assert len(d.owner.sent) == 2

    def test_cooldown_expires(self):
        d = _alert_duck()
        _run(MTGBot._maybe_billing_alert(d, "anthropic", RuntimeError("credit balance")))
        d._billing_alert_last["anthropic"] -= timedelta(hours=CONFIG.billing_alert_cooldown_hours + 1)
        assert _run(MTGBot._maybe_billing_alert(d, "anthropic", RuntimeError("credit balance")))
        assert len(d.owner.sent) == 2

    def test_dm_failure_never_raises(self):
        class _Broken:
            async def send(self, text):
                raise AttributeError("no DM channel")
        d = _alert_duck(owner=_Broken())
        assert _run(MTGBot._maybe_billing_alert(d, "anthropic", RuntimeError("credit balance"))) is False


class TestEngineHook:
    @pytest.fixture
    def recorder(self):
        got = []
        register_billing_alert_callback("test-recorder", lambda p, e: got.append((p, e)))
        yield got
        _BILLING_ALERT_CALLBACKS.pop("test-recorder", None)

    def _player(self, model):
        class _P:
            pass
        p = _P()
        p._consecutive_failures = 0
        p._api_disabled = False
        p.model = model
        return p

    def test_circuit_breaker_reports_a_402(self, recorder):
        p = self._player("deepseek-v4-flash")
        err = RuntimeError(LIVE_DEEPSEEK)
        ClaudePlayer._check_circuit_breaker(p, err)
        assert recorder == [("deepseek-v4-flash", err)]
        assert p._consecutive_failures == 1, "the breaker's own bookkeeping is untouched"

    def test_circuit_breaker_ignores_transients(self, recorder):
        p = self._player("qwen3.7-flash")
        ClaudePlayer._check_circuit_breaker(p, RuntimeError("Connection reset by peer"))
        assert recorder == []

    def test_registration_replaces_by_name(self, recorder):
        register_billing_alert_callback("test-recorder", lambda p, e: recorder.append(("v2", e)))
        err = RuntimeError(LIVE_DEEPSEEK)
        notify_billing_error("x", err)
        assert recorder == [("v2", err)], "a re-registered name must not stack"


class TestEngineHookScheduling:
    def _duck(self):
        class _D:
            def __init__(self):
                self.calls = []

            async def _maybe_billing_alert(self, provider, exc):
                self.calls.append((provider, exc))
                return True
        return _D()

    def test_schedules_on_the_running_loop(self):
        d = self._duck()
        err = RuntimeError(LIVE_DEEPSEEK)

        async def main():
            MTGBot._billing_alert_from_engine(d, "deepseek-v4-flash", err)
            await asyncio.sleep(0)
        _run(main())
        assert d.calls == [("deepseek-v4-flash", err)]

    def test_schedules_from_a_worker_thread(self):
        d = self._duck()
        err = RuntimeError(LIVE_DEEPSEEK)

        async def main():
            loop = asyncio.get_running_loop()
            d.loop = loop
            await loop.run_in_executor(
                None, MTGBot._billing_alert_from_engine, d, "qwen3.7-flash", err)
            for _ in range(50):
                if d.calls:
                    break
                await asyncio.sleep(0.01)
        _run(main())
        assert d.calls == [("qwen3.7-flash", err)]

    def test_no_loop_at_all_never_raises(self):
        class _D:
            loop = None

            async def _maybe_billing_alert(self, provider, exc):
                return True
        MTGBot._billing_alert_from_engine(_D(), "deepseek-v4-flash", RuntimeError("x"))
