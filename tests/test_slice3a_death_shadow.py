"""Pub/sub slice 3 (July 21-24, 2026) — CREATURE_DIED via the bus.

queue_death/queue_deaths (mtg/triggers.py) is the single choke-point for
the _recently_died queue: it emits CREATURE_DIED and the accumulator
subscriber does the append. The parity recorder that shadowed the 3a/3b
migration was retired in slice 3c (the post-3b batch game_15299* returned
[EVENT-PARITY-DIES]=0); the structural no-raw-mutations pin below is the
PERMANENT net, not migration scaffolding.
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
        captured = []

        def _capture(g, card=None, player=None, **_):
            captured.append(card)

        events.subscribe(events.CREATURE_DIED, _capture)
        try:
            queue_death(game, bear, rick)
        finally:
            events.unsubscribe(events.CREATURE_DIED, _capture)
        assert (bear, rick) in game._recently_died, (
            "the accumulator subscriber must feed the queue")
        assert captured and captured[-1] is bear, (
            "queue_death must emit CREATURE_DIED on the bus")

    def test_no_raw_queue_mutations_outside_the_choke_point(self):
        # The call-site-hunt bug class this slice retires: every death goes
        # through the queue_death choke-point (the CREATURE_DIED emit). Slice 3b
        # moved the physical _recently_died append out of queue_death and into
        # the bus subscriber _accumulate_death_subscriber, which is now the ONE
        # sanctioned appender.
        offenders = []
        for pydir in ('mtg', 'rules'):
            for path in sorted((REPO / pydir).glob('*.py')):
                lines = path.read_text(encoding='utf-8').splitlines()
                for i, line in enumerate(lines, 1):
                    if line.strip().startswith('#'):
                        continue
                    if re.search(r'_recently_died\.(append|extend)\(', line):
                        # Robustly identify the enclosing function by scanning
                        # upward for the nearest `def` at a shallower indent
                        # (independent of docstring length — the 3b subscriber's
                        # docstring is long enough to break a char-window check).
                        indent = len(line) - len(line.lstrip())
                        enclosing = None
                        for j in range(i - 2, -1, -1):
                            m = re.match(r'(\s*)def (\w+)', lines[j])
                            if m and len(m.group(1)) < indent:
                                enclosing = m.group(2)
                                break
                        if path.name == 'triggers.py' and enclosing == '_accumulate_death_subscriber':
                            continue  # the sole sanctioned appender (3b bus subscriber)
                        offenders.append(f"{path.name}:{i} (in {enclosing})")
        assert not offenders, (
            "raw _recently_died mutation outside _accumulate_death_subscriber: "
            + "; ".join(offenders))

    # (Slice 3c, July 24, 2026: the three parity-recorder tests were deleted
    # with the recorder they covered. The choke-point emit + the structural
    # pin above are the permanent guarantees.)
