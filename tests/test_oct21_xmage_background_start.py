"""Oct 21, 2026: the XMage bridge starts in the background.

discord.py opens the gateway only after ``setup_hook`` returns, and
``setup_hook`` awaits ``MTGGameCog.cog_load``. Awaiting the bridge there kept
the bot offline for the whole card-database scan: ~13s warm, ~10 minutes
cold off the USB drive the JAR lives on (Oct 21 relaunch, measured).

These pins drive the REAL ``cog_load``/``cog_unload`` with a fake bridge
whose ``start()`` blocks until released, so each property is checked on the
path production takes rather than on a helper called directly:

* cog_load returns while the bridge is still starting;
* no consumer can see the bridge before it reports ready;
* a failed start stops the half-started JVM instead of orphaning it;
* unloading mid-start cancels the start and stops the JVM;
* ``wait_for_xmage`` shares one budget measured from when the start began,
  and timing out never cancels the start;
* autoplay waits on that budget before logging ``[XMAGE-INIT]``;
* ``!xmage`` refuses to start a second scan while the first is running.
"""

import asyncio
import inspect
import io
import logging
import sys
import time
from types import SimpleNamespace

import pytest

import mtg.cog as cog_module
import rules.xmage_bridge as xmage_module
from mtg.cog import MTGGameCog


def _make_fake_bridge_class(fail=None, slow_to_cancel=False):
    """A bridge whose start() blocks until ``release`` is set.

    ``slow_to_cancel`` makes a cancelled start wait for ``shutdown`` before
    it unwinds, so cog_unload is genuinely parked on it.
    """

    class FakeBridge:
        instances = []

        def __init__(self):
            self.process = None
            self.stopped = False
            self.release = asyncio.Event()
            self.shutdown = asyncio.Event()
            FakeBridge.instances.append(self)

        async def start(self):
            self.process = object()  # the JVM exists from here on
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                if slow_to_cancel:
                    await self.shutdown.wait()
                raise
            if fail is not None:
                raise fail

        async def stop(self):
            self.stopped = True
            self.process = None

    return FakeBridge


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    """Contain cog_load's process-wide side effects (tees, root logger)."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    monkeypatch.setattr(sys, "stderr", io.StringIO())
    monkeypatch.delenv("MTG_ACTIVITY_ENABLED", raising=False)
    monkeypatch.setattr(cog_module, "HAS_XMAGE_BRIDGE", True)
    root = logging.getLogger()
    level = root.level
    yield monkeypatch
    root.setLevel(level)


def _make_cog():
    cog = object.__new__(MTGGameCog)
    cog.bot = SimpleNamespace()
    cog.engine = SimpleNamespace(
        games={}, xmage_bridge=None, _xmage_available=False,
        _xmage_init_reason="")
    cog._activity_server = None
    cog._stdout_tee = None
    cog._stderr_tee = None
    cog._stderr_log_handler = None
    cog._xmage_start_task = None
    cog._xmage_pending_bridge = None
    cog._xmage_start_began = None
    return cog


async def _bounded(coro, fake_cls, what):
    """Run ``coro`` for at most 2s; on overrun, unblock every fake and fail.

    asyncio.wait (not wait_for) because wait_for waits for its cancellation
    to finish, and a regression that parks on the bridge would then hang the
    suite instead of failing it.
    """
    task = asyncio.ensure_future(coro)
    done, _ = await asyncio.wait({task}, timeout=2.0)
    if task not in done:
        for bridge in fake_cls.instances:
            bridge.release.set()
            bridge.shutdown.set()
        await asyncio.wait({task}, timeout=2.0)
        pytest.fail(f"{what} did not return within 2s")
    return task.result()


async def _load(cog, fake_cls):
    # A cog_load that awaited the bridge would block until release.
    await _bounded(cog.cog_load(), fake_cls, "cog_load (it must not wait for the bridge)")
    await asyncio.sleep(0)  # let the background start reach its await
    return cog._xmage_pending_bridge


class TestCogLoadDoesNotWaitForTheBridge:

    def test_cog_load_returns_while_the_bridge_is_still_starting(self, isolated):
        fake_cls = _make_fake_bridge_class()
        isolated.setattr(xmage_module, "XMageBridge", fake_cls)

        async def scenario():
            cog = _make_cog()
            try:
                bridge = await _load(cog, fake_cls)
                assert bridge is fake_cls.instances[0]
                assert cog.xmage_starting()
                # Consumers gate on these, so nothing can reach a JVM that
                # is still scanning its card database.
                assert cog.engine.xmage_bridge is None
                assert cog.engine._xmage_available is False
                assert "background" in cog.engine._xmage_init_reason

                bridge.release.set()
                await asyncio.wait_for(cog._xmage_start_task, timeout=2.0)
                assert cog.engine.xmage_bridge is bridge
                assert cog.engine._xmage_available is True
                assert cog.engine._xmage_init_reason == ""
                assert not cog.xmage_starting()
                assert cog._xmage_pending_bridge is None
            finally:
                await _bounded(cog.cog_unload(), fake_cls, "cog_unload")

        asyncio.run(scenario())

    def test_a_failed_start_stops_the_half_started_jvm(self, isolated):
        fake_cls = _make_fake_bridge_class(fail=RuntimeError("JVM died"))
        isolated.setattr(xmage_module, "XMageBridge", fake_cls)

        async def scenario():
            cog = _make_cog()
            try:
                bridge = await _load(cog, fake_cls)
                bridge.release.set()
                await asyncio.wait_for(cog._xmage_start_task, timeout=2.0)
                assert bridge.stopped, "a failed start must not orphan its JVM"
                assert cog.engine.xmage_bridge is None
                assert cog.engine._xmage_available is False
                assert cog.engine._xmage_init_reason.startswith("direct-spawn failed")
                assert cog._xmage_pending_bridge is None
            finally:
                await _bounded(cog.cog_unload(), fake_cls, "cog_unload")

        asyncio.run(scenario())

    def test_unloading_mid_start_cancels_it_and_stops_the_jvm(self, isolated):
        fake_cls = _make_fake_bridge_class()
        isolated.setattr(xmage_module, "XMageBridge", fake_cls)

        async def scenario():
            cog = _make_cog()
            bridge = await _load(cog, fake_cls)
            task = cog._xmage_start_task
            await _bounded(cog.cog_unload(), fake_cls, "cog_unload")
            assert task.cancelled()
            assert bridge.stopped, "a reload must not leave a second scan running"
            assert not cog.xmage_starting()
            assert cog.engine.xmage_bridge is None

        asyncio.run(scenario())

    def test_cancelling_unload_itself_is_not_swallowed(self, isolated):
        # `await task` + `except CancelledError: pass` would also eat a
        # cancellation aimed at cog_unload, which then carries on as if
        # nothing happened. Found by the mutation sweep.
        fake_cls = _make_fake_bridge_class(slow_to_cancel=True)
        isolated.setattr(xmage_module, "XMageBridge", fake_cls)

        async def scenario():
            cog = _make_cog()
            bridge = await _load(cog, fake_cls)
            start_task = cog._xmage_start_task
            unload = asyncio.create_task(cog.cog_unload())
            await asyncio.sleep(0.05)
            assert not unload.done(), "unload should be waiting on the start"
            unload.cancel()
            done, _ = await asyncio.wait({unload}, timeout=2.0)
            assert unload in done
            assert unload.cancelled(), "cog_unload swallowed its own cancellation"
            bridge.shutdown.set()
            await asyncio.wait({start_task}, timeout=2.0)
            assert start_task.done()

        asyncio.run(scenario())


class TestWaitForXmage:

    def test_waits_for_a_start_that_finishes_inside_the_budget(self, isolated):
        fake_cls = _make_fake_bridge_class()
        isolated.setattr(xmage_module, "XMageBridge", fake_cls)

        async def scenario():
            cog = _make_cog()
            try:
                bridge = await _load(cog, fake_cls)
                waiter = asyncio.create_task(cog.wait_for_xmage(60.0))
                await asyncio.sleep(0.05)
                assert not waiter.done()
                bridge.release.set()
                assert await asyncio.wait_for(waiter, timeout=2.0) is True
            finally:
                await _bounded(cog.cog_unload(), fake_cls, "cog_unload")

        asyncio.run(scenario())

    def test_the_budget_is_measured_from_when_the_start_began(self, isolated):
        # A bridge that never becomes ready must stall autoplay by the budget
        # ONCE, not once per game or per wave.
        fake_cls = _make_fake_bridge_class()
        isolated.setattr(xmage_module, "XMageBridge", fake_cls)

        async def scenario():
            cog = _make_cog()
            try:
                await _load(cog, fake_cls)
                cog._xmage_start_began = time.monotonic() - 100.0
                assert await asyncio.wait_for(
                    cog.wait_for_xmage(50.0), timeout=1.0) is False
            finally:
                await _bounded(cog.cog_unload(), fake_cls, "cog_unload")

        asyncio.run(scenario())

    def test_timing_out_never_cancels_the_start(self, isolated):
        fake_cls = _make_fake_bridge_class()
        isolated.setattr(xmage_module, "XMageBridge", fake_cls)

        async def scenario():
            cog = _make_cog()
            try:
                bridge = await _load(cog, fake_cls)
                cog._xmage_start_began = time.monotonic()
                assert await cog.wait_for_xmage(0.05) is False
                assert not cog._xmage_start_task.cancelled()
                bridge.release.set()
                await asyncio.wait_for(cog._xmage_start_task, timeout=2.0)
                assert cog.engine._xmage_available is True
            finally:
                await _bounded(cog.cog_unload(), fake_cls, "cog_unload")

        asyncio.run(scenario())

    def test_no_start_in_flight_returns_availability_immediately(self):
        cog = _make_cog()
        cog.engine._xmage_available = True
        assert asyncio.run(cog.wait_for_xmage(900.0)) is True


class TestCallers:

    def test_autoplay_waits_on_the_shared_budget_before_xmage_init(self):
        # Structural: _run_single_autoplay is too large to drive here. The
        # wait must come BEFORE the [XMAGE-INIT] availability line, or that
        # line reports the pre-wait state and early games skip Tier 2.5.
        import mtg.autoplay as autoplay
        src = inspect.getsource(autoplay._run_single_autoplay)
        wait = src.index("await wait_for_xmage(XMAGE_AUTOPLAY_WAIT_SECONDS)")
        report = src.index("[XMAGE-INIT] Bridge available")
        assert wait < report
        assert autoplay.XMAGE_AUTOPLAY_WAIT_SECONDS >= 600.0

    def _xmage_ctx(self):
        sent = []

        class _Typing:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

        async def send(content=None, **kwargs):
            sent.append(content)

        return SimpleNamespace(typing=lambda: _Typing(), send=send), sent

    def test_xmage_command_refuses_a_second_scan_during_startup(self, monkeypatch):
        from bot import MTGCog

        constructed = []

        class ExplodingBridge:
            def __init__(self, *args, **kwargs):
                constructed.append(True)
                raise AssertionError("a second bridge must not be spawned")

        monkeypatch.setattr(xmage_module, "XMageBridge", ExplodingBridge)
        game_cog = SimpleNamespace(
            xmage_starting=lambda: True,
            engine=SimpleNamespace(_xmage_available=False, xmage_bridge=None))
        cog = object.__new__(MTGCog)
        cog.bot = SimpleNamespace(
            get_cog=lambda name: game_cog if name == "MTG Game" else None)
        ctx, sent = self._xmage_ctx()

        asyncio.run(MTGCog.xmage_lookup.callback(cog, ctx, card_name="Lightning Bolt"))

        assert not constructed
        assert sent and "still loading" in sent[0]

    def test_xmage_command_uses_the_ready_bridge(self, monkeypatch):
        # Control: once ready, the persistent bridge answers as before.
        from bot import MTGCog

        looked_up = []

        class ReadyBridge:
            async def lookup(self, name):
                looked_up.append(name)
                return None

        game_cog = SimpleNamespace(
            xmage_starting=lambda: False,
            engine=SimpleNamespace(_xmage_available=True, xmage_bridge=ReadyBridge()))
        cog = object.__new__(MTGCog)
        cog.bot = SimpleNamespace(
            get_cog=lambda name: game_cog if name == "MTG Game" else None)
        ctx, sent = self._xmage_ctx()

        asyncio.run(MTGCog.xmage_lookup.callback(cog, ctx, card_name="Lightning Bolt"))

        assert looked_up == ["Lightning Bolt"]
        assert not any("still loading" in (m or "") for m in sent)
