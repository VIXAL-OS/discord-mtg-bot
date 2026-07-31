"""Pub/sub slice 6b (July 31, 2026) — the MAIN-phase dispatch is bus-fed.

The 6a gate cleared on batch 15325 ([EVENT-PARITY-PHASE]=0 across 151
games), so the flip landed: _main_phase_bus_subscriber (mtg/triggers.py)
runs GameEngine.dispatch_main_phase_triggers on EVERY MAIN1/MAIN2 entry —
advance_phase's walk and all seven combat-path direct sets alike — with no
caller cooperation. Messages buffer into game._pending_messages and the old
call sites drain at their exact old positions (the slice-2b convention).
The 6a recorder/scaffolding is retired; [EVENT-PARITY-PHASE] is a
stale-code tripwire like its 2c/3c/4b/5b siblings.

Scoping: the UPKEEP scan deliberately did NOT flip — UPKEEP has exactly one
entry path (advance_phase's PHASE_ORDER walk, where the scan sits inside a
strictly ordered sequence), so its hook structurally cannot be orphaned.
See the subscriber block's comment.

The structural no-raw-phase-assignment pin below is PERMANENT (the
_recently_died precedent), not scaffolding.
"""
import re
from pathlib import Path

import pytest

import mtg.triggers  # noqa: F401 — registers the subscriber at import
from mtg.constants import Phase


REPO = Path(__file__).resolve().parent.parent


class TestStructuralPin:
    def test_no_raw_phase_assignments_outside_models(self):
        # models.py owns the field (set_phase, the dataclass default,
        # from_dict restoration). Everywhere else in the engine package a
        # raw `X.phase = Phase.Y` assignment is the bug class this slice
        # exists to kill — new sites must go through set_phase(via=...).
        pattern = re.compile(r'\.phase\s*=\s*(Phase\.|PHASE_ORDER)')
        offenders = []
        for py in sorted((REPO / "mtg").glob("*.py")):
            if py.name == "models.py":
                continue
            for lineno, line in enumerate(
                    py.read_text(encoding="utf-8").splitlines(), 1):
                code = line.split('#', 1)[0]
                if pattern.search(code):
                    offenders.append(f"{py.name}:{lineno}: {line.strip()}")
        assert not offenders, (
            "raw game.phase assignments bypass the PHASE_CHANGED spine:\n"
            + "\n".join(offenders))


def _spy_engine(monkeypatch, rules, game, reply=None):
    """Wire a GameEngine whose dispatch is a call-recording spy, reachable
    the way the subscriber reaches it (game._rules_engine.engine_ref)."""
    from mtg.engine import GameEngine
    ge = GameEngine.__new__(GameEngine)
    calls = []

    def _dispatch(g, precombat):
        calls.append(precombat)
        return list(reply or [])

    ge.dispatch_main_phase_triggers = _dispatch
    rules.engine_ref = ge
    game._rules_engine = rules
    return ge, calls


class TestBusDispatch:
    def test_main2_entry_dispatches_and_buffers(self, rules, game, monkeypatch):
        _ge, calls = _spy_engine(monkeypatch, rules, game,
                                 reply=["⚡ Tymna resolves"])
        game.set_phase(Phase.MAIN2, via="cog:_autoplay_resolve_combat:main2")
        assert calls == [False], (
            "a combat-path direct set must run the postcombat dispatch — "
            "the Tymna class this spine exists to kill")
        assert "⚡ Tymna resolves" in game._pending_messages, (
            "dispatch output must buffer for the call-site drain")

    def test_main1_entry_dispatches_precombat(self, rules, game, monkeypatch):
        _ge, calls = _spy_engine(monkeypatch, rules, game)
        game.set_phase(Phase.MAIN1, via="advance_phase")
        assert calls == [True]

    def test_non_main_phases_do_not_dispatch(self, rules, game, monkeypatch):
        _ge, calls = _spy_engine(monkeypatch, rules, game)
        for ph, via in ((Phase.UPKEEP, "advance_phase"),
                        (Phase.COMBAT_DAMAGE, "autoplay:_resolve_combat"),
                        (Phase.END, "advance_phase"),
                        (Phase.UNTAP, "end_turn")):
            game.set_phase(ph, via=via)
        assert calls == []

    def test_game_start_exempt(self, rules, game, monkeypatch):
        _ge, calls = _spy_engine(monkeypatch, rules, game)
        game.set_phase(Phase.MAIN1, via="game_start")
        assert calls == []

    def test_ended_game_skips(self, rules, game, monkeypatch):
        _ge, calls = _spy_engine(monkeypatch, rules, game)
        game.ended = True
        game.set_phase(Phase.MAIN2, via="autoplay:_resolve_combat:main2")
        assert calls == [], "CR 104.2a — no dispatch after the game ends"

    def test_moraug_repeat_entries_each_dispatch(self, rules, game, monkeypatch):
        # CR-correct per-entry semantics: "at the beginning of EACH of your
        # postcombat main phases" fires again on a Moraug extra main.
        _ge, calls = _spy_engine(monkeypatch, rules, game)
        game.set_phase(Phase.MAIN2, via="autoplay:_resolve_combat:main2")
        game.set_phase(Phase.DECLARE_ATTACKERS, via="autoplay:moraug_combat")
        game.set_phase(Phase.MAIN2, via="autoplay:_resolve_combat:main2")
        assert calls == [False, False]

    def test_missing_engine_prints_phase_bus_tag(self, game, capsys):
        game._rules_engine = None
        game.set_phase(Phase.MAIN2, via="orphan-site")
        out = capsys.readouterr().out
        assert "[PHASE-BUS]" in out, (
            "an undispatchable MAIN entry must be loud, not silent")
        assert "orphan-site" in out


class TestNoDirectCallSites:
    def test_dispatch_has_no_remaining_direct_callers(self):
        # The subscriber is the ONE invoker. A direct call surviving (or
        # returning) alongside it would DOUBLE-dispatch — Tymna would pay
        # life twice per combat turn.
        offenders = []
        for py in sorted((REPO / "mtg").glob("*.py")):
            src = py.read_text(encoding="utf-8")
            for lineno, line in enumerate(src.splitlines(), 1):
                code = line.split('#', 1)[0]
                if "dispatch_main_phase_triggers(" in code:
                    if py.name == "triggers.py" and "_main_phase_bus_subscriber" in src:
                        # the subscriber's own invocation
                        continue
                    if "def dispatch_main_phase_triggers" in code:
                        continue
                    offenders.append(f"{py.name}:{lineno}: {line.strip()}")
        assert not offenders, (
            "direct dispatch calls alongside the subscriber double-fire:\n"
            + "\n".join(offenders))

    def test_subscriber_registered(self):
        from mtg import events
        from mtg.triggers import _main_phase_bus_subscriber
        assert _main_phase_bus_subscriber in events._subscribers.get(
            events.PHASE_CHANGED, [])

    def test_shadow_scaffolding_gone(self):
        # The 6a recorder retired at the flip; its tags are stale-code
        # tripwires now.
        import mtg.triggers as trig
        assert not hasattr(trig, "_phase_shadow_recorder")
        assert not hasattr(trig, "report_phase_parity")
        assert not hasattr(trig, "record_phase_hook_run")
        from mtg.models import GameState
        assert "_phase_emissions" not in {
            f.name for f in GameState.__dataclass_fields__.values()}


class TestEndToEndThroughAdvancePhase:
    def test_advance_into_main1_dispatches_exactly_once(self, rules, game,
                                                        monkeypatch):
        # advance_phase's MAIN1 branch used to call the dispatch directly;
        # after the flip the subscriber is the only invoker — entering
        # MAIN1 through the real walk must dispatch exactly ONCE.
        from mtg.engine import GameEngine
        ge = GameEngine(None)
        game._rules_engine = ge.rules
        calls = []
        real = ge.dispatch_main_phase_triggers

        def _counting(g, precombat):
            calls.append(precombat)
            return real(g, precombat)

        monkeypatch.setattr(ge, "dispatch_main_phase_triggers", _counting)
        game.set_phase(Phase.DRAW, via="test-setup")
        calls.clear()  # the setup transition isn't under test
        ge.advance_phase(game)  # DRAW → MAIN1
        assert game.phase == Phase.MAIN1
        assert calls == [True], (
            f"expected exactly one precombat dispatch, got {calls} — more "
            f"than one means a direct call site survived the flip")
