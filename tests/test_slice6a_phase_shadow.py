"""Pub/sub slice 6a (July 31, 2026) — PHASE_CHANGED in SHADOW mode.

GameState.set_phase is the ONE sanctioned phase mutator (the structural pin
below forbids raw assignments in the engine — the direct-phase-set class
produced the Tymna bug three times: July 27 scan unwired, July 29 F3-C,
July 30 F3). It emits PHASE_CHANGED(old, new, via); the shadow recorder
pairs entries into HOOKED phases (MAIN1/MAIN2 → dispatch_main_phase_triggers,
UPKEEP → the upkeep scan) with hook runs recorded AT the hooks, and
report_phase_parity prints [EVENT-PARITY-PHASE] from end_turn for any entry
whose hook never ran that turn. One clean batch gates the 6b flip (hooks
become subscribers; recorder retired like 2c/3c/5b's).
"""
import inspect
import re
from pathlib import Path

import pytest

import mtg.triggers  # noqa: F401 — registers the recorder at import
from mtg.constants import Phase
from mtg.triggers import record_phase_hook_run, report_phase_parity


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


class TestSetPhaseEmits:
    def test_emission_recorded_with_via(self, game):
        game.set_phase(Phase.UPKEEP, via="test-site")
        assert game.phase == Phase.UPKEEP
        assert game._phase_emissions == [
            (game.turn_number, "MAIN1", "UPKEEP", "test-site")]

    def test_untap_is_parity_inert(self, game, capsys):
        # end_turn's own set_phase(UNTAP) lands AFTER the report and carries
        # into the next turn's records — UNTAP has no hook, so it must never
        # produce a parity line.
        game.set_phase(Phase.UNTAP, via="end_turn")
        capsys.readouterr()
        report_phase_parity(game)
        assert "[EVENT-PARITY-PHASE]" not in capsys.readouterr().out


class TestPhaseParity:
    def test_hooked_entry_with_hook_run_is_clean(self, game, capsys):
        game.set_phase(Phase.MAIN2, via="autoplay:_resolve_combat:main2")
        record_phase_hook_run(game, "main2")
        capsys.readouterr()
        report_phase_parity(game)
        assert "[EVENT-PARITY-PHASE]" not in capsys.readouterr().out
        assert game._phase_emissions == [] and game._phase_hook_runs == [], \
            "the report must clear both records"

    def test_hooked_entry_without_hook_run_is_flagged(self, game, capsys):
        game.set_phase(Phase.MAIN2, via="some-new-direct-set")
        capsys.readouterr()
        report_phase_parity(game)
        out = capsys.readouterr().out
        assert "[EVENT-PARITY-PHASE]" in out
        assert "main2 hook never ran" in out
        assert "some-new-direct-set" in out, "via must name the guilty site"

    def test_upkeep_entry_pairs_with_upkeep_scan(self, game, capsys):
        game.set_phase(Phase.UPKEEP, via="advance_phase")
        record_phase_hook_run(game, "upkeep")
        capsys.readouterr()
        report_phase_parity(game)
        assert "[EVENT-PARITY-PHASE]" not in capsys.readouterr().out

    def test_game_start_is_exempt(self, game, capsys):
        # start_game sets MAIN1 with an empty battlefield — no triggers can
        # exist, so no dispatch is demanded.
        game.set_phase(Phase.MAIN1, via="game_start")
        capsys.readouterr()
        report_phase_parity(game)
        assert "[EVENT-PARITY-PHASE]" not in capsys.readouterr().out

    def test_repeat_entries_need_one_dispatch(self, game, capsys):
        # Set semantics per (turn, hook): a Moraug-style second combat's
        # second MAIN2 entry must not demand a second dispatch.
        game.set_phase(Phase.MAIN2, via="cog:_autoplay_resolve_combat:main2")
        game.set_phase(Phase.MAIN2, via="cog:_autoplay_resolve_combat:main2")
        record_phase_hook_run(game, "main2")
        capsys.readouterr()
        report_phase_parity(game)
        assert "[EVENT-PARITY-PHASE]" not in capsys.readouterr().out

    def test_ended_game_skips_the_check(self, game, capsys):
        # CR 104.2a: hooks legitimately don't run after a mid-turn loss.
        game.set_phase(Phase.MAIN2, via="autoplay:_resolve_combat:main2")
        game.ended = True
        capsys.readouterr()
        report_phase_parity(game)
        assert "[EVENT-PARITY-PHASE]" not in capsys.readouterr().out
        assert game._phase_emissions == [], "records still cleared"


class TestInstrumentationWired:
    def test_end_turn_runs_the_report(self):
        from mtg.engine import GameEngine
        src = inspect.getsource(GameEngine.end_turn)
        assert "report_phase_parity" in src, \
            "the shadow report must run every turn or the batch gate is blind"

    def test_main_phase_choke_point_records(self):
        from mtg.engine import GameEngine
        src = inspect.getsource(GameEngine.dispatch_main_phase_triggers)
        assert "record_phase_hook_run" in src
        assert '"main1" if precombat else "main2"' in src

    def test_upkeep_scan_records(self):
        from mtg.triggers import _check_upkeep_triggers_sync
        src = inspect.getsource(_check_upkeep_triggers_sync)
        assert 'record_phase_hook_run(game, "upkeep")' in src

    def test_recorder_subscribed(self):
        from mtg import events
        from mtg.triggers import _phase_shadow_recorder
        assert _phase_shadow_recorder in events._subscribers.get(
            events.PHASE_CHANGED, [])
