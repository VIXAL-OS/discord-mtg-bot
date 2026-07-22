"""Pub/sub slice 3a (July 21, 2026) — CREATURE_DIED in shadow mode.

queue_death/queue_deaths (mtg/triggers.py) is the single choke-point for
the _recently_died queue: it appends exactly as before AND emits
CREATURE_DIED, whose only subscriber is the parity recorder. Consumers
(the dies dispatcher, wave semantics, apnap_order_died) are unchanged by
construction — slice 3b flips them only after 3a's own clean batch.
"""
import re
from pathlib import Path

import pytest

from mtg import events

REPO = Path(__file__).resolve().parent.parent


class TestQueueDeathChokePoint:
    def test_queue_death_appends_and_emits(self, make_game, make_card):
        from mtg.triggers import queue_death
        game = make_game()
        rick = game.players[0]
        bear = make_card("Bear", power="2", toughness="2")
        queue_death(game, bear, rick)
        assert (bear, rick) in game._recently_died, (
            "shadow mode: the legacy queue must be fed exactly as before")
        assert game._death_events, "the CREATURE_DIED shadow emit must record"
        assert game._death_events[-1][1] == "Bear"

    def test_no_raw_queue_mutations_outside_the_choke_point(self):
        # The call-site-hunt bug class this slice retires: every append to
        # _recently_died must go through queue_death (its own body is the
        # one allowed site).
        offenders = []
        for pydir in ('mtg', 'rules'):
            for path in sorted((REPO / pydir).glob('*.py')):
                src = path.read_text(encoding='utf-8')
                for i, line in enumerate(src.splitlines(), 1):
                    if line.strip().startswith('#'):
                        continue
                    if re.search(r'_recently_died\.(append|extend)\(', line):
                        if path.name == 'triggers.py' and 'queue_death' in src[
                                max(0, src.find(line) - 800):src.find(line)]:
                            continue  # the choke-point body itself
                        offenders.append(f"{path.name}:{i}")
        assert not offenders, (
            "raw _recently_died mutation outside queue_death: "
            + "; ".join(offenders))

    def test_parity_reports_undrained_death_after_dispatch_gap(
            self, make_game, make_card, capsys):
        from mtg.triggers import queue_death, report_death_parity
        game = make_game()
        rick = game.players[0]
        bear = make_card("Bear", power="2", toughness="2")
        queue_death(game, bear, rick)
        # Simulate the drain CLAIMING the queue without dispatching (the
        # miss class): empty the queue, never call the dies scan.
        game._recently_died.clear()
        misses = report_death_parity(game)
        assert misses and "Bear" in misses[0]
        assert "[EVENT-PARITY-DIES]" in capsys.readouterr().out

    def test_pending_death_is_not_a_miss(self, make_game, make_card):
        from mtg.triggers import queue_death, report_death_parity
        game = make_game()
        rick = game.players[0]
        bear = make_card("Bear", power="2", toughness="2")
        queue_death(game, bear, rick)
        # Still sitting in the queue at end_turn — queued-not-yet-drained
        # is pending, not a miss.
        assert report_death_parity(game) == []

    def test_dispatched_death_is_clean(self, make_game, make_card, rules):
        from mtg.engine import GameEngine
        from mtg.triggers import queue_death, report_death_parity
        engine = GameEngine(None)
        game = make_game()
        rick = game.players[0]
        bear = make_card("Bear", power="2", toughness="2")
        queue_death(game, bear, rick)
        game._recently_died.clear()
        engine._check_dies_triggers_sync(game, bear, rick)
        assert report_death_parity(game) == []
