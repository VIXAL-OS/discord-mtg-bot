#!/usr/bin/env python3
"""Card-level audit coverage: which CARDS has a reviewer never actually read?

Why this exists
---------------
Reviewer sampling has been driven by "recency of attention" over ARCHETYPES
since July 27, and by Aug 10 that well ran dry: all 40 matrix archetypes had
been examined at least once, and only 20 of the 62 unexamined matchups
involved a deck read three times or fewer. Meanwhile the defects the Aug 10
cycle actually found were CARD-level — Dark Depths entering with zero ice
counters, Glaive of the Guildpact's discarded multiplier, Goblin
Rabblemaster's inverted trigger, Sword of Light and Shadow's dropped
combat-damage trigger, Glacial Chasm's unenforced attack lock. Every one
surfaced because a game happened to draw the card, not because the matchup
was novel.

So the useful remaining-pool number is not "unexamined matchups". It is
"cards that have appeared in play but never inside a game a reviewer read".
That pool is far larger and depletes far more slowly, and it gives a
selection rule that keeps working after the matchup complement is exhausted:
pick the games densest in never-reviewed cards.

What counts as "in play"
------------------------
Deck membership is NOT evidence — a card sitting in a library all game
exercises nothing. This counts only cards with a live play signal: a spell
put on the stack, a land played, or a template that resolved for it. Every
extracted name is intersected with the Scryfall disk cache, which throws
away parse noise (the alternative, trusting a regex against prose, silently
invents cards).

What counts as "reviewed"
-------------------------
A game recorded in data/reviewed_games.json OR cited by id in CLAUDE.md,
MINUS anything data/card_review_ledger.json puts back. The bare
game-membership inference over-credits three ways — a reviewer does not check
every card in a game, a card can be present with its ability never firing,
and a card fixed in the same session was reviewed against logs that PREDATE
the fix — so the ledger carries per-card verified / unexercised / awaiting
statuses and the last two return to the pool. See that file for the contract.

The JSON is the record and is where a wave's ids belong. CLAUDE.md remains a
source because the historical waves are only recorded there, but it is a
FLOOR: prose citation depends on a write-up happening to list its games, and
the Aug 10 card-targeted wave (written up by finding rather than by matchup)
cited none, which is what the JSON now fixes.

Usage
-----
    python tools/card_coverage.py                  # whole logs/ directory
    python tools/card_coverage.py --sha 6a30802    # one batch
    python tools/card_coverage.py --sha 6a30802 --games 6
        # ...and print the 6 unreviewed games densest in never-seen cards,
        #    which is the reviewer-selection answer.
"""
from __future__ import annotations

import argparse
import collections
import io
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOGS = os.path.join(ROOT, 'logs')
CACHE = os.path.join(ROOT, 'data', 'card_data_cache.json')
DOC = os.path.join(ROOT, 'CLAUDE.md')
LEDGER = os.path.join(ROOT, 'data', 'card_review_ledger.json')
REVIEWED_GAMES = os.path.join(ROOT, 'data', 'reviewed_games.json')

# Live play signals. Each must name a card that DID something, not one that
# was merely legal or merely in a deck list.
_PLAY_PATTERNS = (
    re.compile(r'^\[STACK\] (.+?) goes on the stack'),
    re.compile(r'play_land ([^:]+):'),
    re.compile(r'\[[A-Z0-9-]*TEMPLATE\] Resolved ([A-Za-z][^:,]*?)(?: via| attack| trigger|:|$)'),
    re.compile(r'\[ACTIVATE-[A-Z0-9-]+\] ([A-Za-z][^:,]*?):'),
)


def _load_cache_names():
    with io.open(CACHE, encoding='utf-8') as fh:
        return set(json.load(fh))


def _load_ledger():
    """Reviewer-supplied per-card status; see the file's own _README."""
    try:
        with io.open(LEDGER, encoding='utf-8') as fh:
            return {k.lower(): v for k, v in json.load(fh).get('cards', {}).items()}
    except (OSError, ValueError):
        return {}


def _reviewed_game_ids():
    """Game ids a reviewer read: data/reviewed_games.json UNION CLAUDE.md.

    Aug 10: CLAUDE.md prose was the only source, which worked solely because
    every wave HAPPENED to be written up as a matchup ledger listing each
    game. The card-targeted wave was written up by FINDING and cited zero
    ids, so its twelve games read as unreviewed and this tool began
    recommending them for re-reading — one suggestion listed nine "novel"
    cards, seven of which were that reviewer's own findings.

    Prose citation is a side effect of a formatting convention, not a record.
    The JSON is the record; CLAUDE.md stays a source so the historical games
    keep counting without a backfill. The read is deliberately forgiving (the
    file is optional), which is also why its absence must be pinned — a
    broken read looks exactly like "no games recorded".
    """
    ids = set()
    try:
        with io.open(REVIEWED_GAMES, encoding='utf-8') as fh:
            for key in json.load(fh).get('games', {}):
                found = re.search(r'game_(\d{15,})', key)
                if found:
                    ids.add(found.group(1))
    except (OSError, ValueError):
        pass
    with io.open(DOC, encoding='utf-8') as fh:
        ids.update(re.findall(r'game_(\d{15,})', fh.read()))
    return ids


def _cards_in_play(path, known):
    """Distinct real cards with a live play signal in one console log."""
    found = set()
    with io.open(path, encoding='utf-8', errors='replace') as fh:
        for line in fh:
            for pattern in _PLAY_PATTERNS:
                match = pattern.search(line)
                if not match:
                    continue
                name = match.group(1).strip().strip('*').strip()
                if name.lower() in known:
                    found.add(name.lower())
    return found


def _game_meta(path):
    with io.open(path, encoding='utf-8', errors='replace') as fh:
        # [GAME-INIT] is emitted in the first few lines; cap the scan rather
        # than reading a 200KB log twice just to find it.
        for _ in range(400):
            line = fh.readline()
            if not line:
                break
            if '[GAME-INIT]' in line:
                sha = re.search(r'sha=([0-9a-f]+)', line)
                fmt = re.search(r'format=([a-z]+)', line)
                d0 = re.search(r'deck0=([a-z_0-9]+)', line)
                d1 = re.search(r'deck1=([a-z_0-9]+)', line)
                return (sha.group(1) if sha else '',
                        f"{fmt.group(1) if fmt else '?'}/"
                        f"{d0.group(1) if d0 else '?'}/"
                        f"{d1.group(1) if d1 else '?'}")
    return '', '?'


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--sha', help='restrict to one batch by its [GAME-INIT] sha')
    parser.add_argument('--top', type=int, default=25,
                        help='how many never-reviewed cards to list')
    parser.add_argument('--games', type=int, default=0,
                        help='also rank this many unreviewed games by novel-card count')
    args = parser.parse_args()

    known = _load_cache_names()
    reviewed_ids = _reviewed_game_ids()

    # A reviewed game counts wherever it lives. Restricting the whole scan to
    # one --sha made every card reviewed in an EARLIER batch look novel, which
    # inflated the never-reviewed count and, worse, corrupted the selection
    # ranking it feeds. Always take the reviewed set from the full corpus;
    # only the listing/ranking honours --sha.
    per_game = {}
    reviewed_cards = set()
    for path in sorted(os.listdir(LOGS)):
        if not path.endswith('_console.log') or not path.startswith('game_'):
            continue
        gid = path[len('game_'):-len('_console.log')]
        full = os.path.join(LOGS, path)
        is_reviewed = gid in reviewed_ids
        sha, matchup = _game_meta(full)
        in_scope = (not args.sha) or sha == args.sha
        if not (in_scope or is_reviewed):
            continue
        cards = _cards_in_play(full, known)
        if is_reviewed:
            reviewed_cards |= cards
        if in_scope:
            per_game[gid] = (matchup, cards)

    if not per_game:
        print('no logs matched', file=sys.stderr)
        return 1

    seen_anywhere = set()
    frequency = collections.Counter()
    for gid, (_matchup, cards) in per_game.items():
        seen_anywhere |= cards
        frequency.update(cards)

    # Credit is corpus-wide; only the denominator is scoped.
    #
    # The bare inference "appeared in a game a reviewer read, therefore
    # checked" OVER-CREDITS three ways: a reviewer does not check every card
    # in a game; a card can be present all game with its ability never firing;
    # and a card FIXED in the same session was reviewed against logs that
    # PREDATE the fix, so the corpus shows the broken behaviour. The ledger
    # lets a reviewer put those back — an `unexercised` or `awaiting` card
    # returns to the pool no matter how many reviewed games it appeared in.
    ledger = _load_ledger()
    put_back = {name for name, entry in ledger.items()
                if entry.get('status') in ('unexercised', 'awaiting')}
    seen_in_reviewed = (seen_anywhere & reviewed_cards) - put_back
    never = (seen_anywhere - reviewed_cards) | (seen_anywhere & put_back)
    scope = f' (batch {args.sha})' if args.sha else ''
    reviewed_here = sum(1 for g in per_game if g in reviewed_ids)

    print(f'=== CARD-LEVEL AUDIT COVERAGE{scope} ===')
    print(f'  games scanned                : {len(per_game)} '
          f'({reviewed_here} reviewed, {len(per_game) - reviewed_here} not)')
    print(f'  distinct cards seen IN PLAY  : {len(seen_anywhere)}')
    print(f'  ...inside a REVIEWED game    : {len(seen_in_reviewed)} '
          f'({100.0 * len(seen_in_reviewed) / max(1, len(seen_anywhere)):.1f}%)')
    _unex = sum(1 for n in seen_anywhere
                if ledger.get(n, {}).get('status') == 'unexercised')
    _await = sum(1 for n in seen_anywhere
                 if ledger.get(n, {}).get('status') == 'awaiting')
    print(f'  NEVER under a reviewer       : {len(never)}')
    print(f'    ...of which put back by the ledger:')
    print(f'      unexercised (ability never fired) : {_unex}')
    print(f'      awaiting    (fix postdates corpus): {_await}')
    print()
    print(f'--- top {args.top} never-reviewed cards, by how often they hit play ---')
    for name, count in sorted(((n, frequency[n]) for n in never),
                              key=lambda kv: -kv[1])[:args.top]:
        print(f'  {count:4d}x  {name}')

    if args.games:
        print()
        print(f'--- {args.games} unreviewed games densest in never-reviewed cards ---')
        print('    (this is the selection rule: read these next)')
        ranked = []
        for gid, (matchup, cards) in per_game.items():
            if gid in reviewed_ids:
                continue
            novel = cards & never
            ranked.append((len(novel), gid, matchup, sorted(novel)))
        ranked.sort(reverse=True)
        for count, gid, matchup, novel in ranked[:args.games]:
            print(f'  {count:3d} novel  game_{gid}  {matchup}')
            print(f'            {", ".join(novel[:12])}'
                  f'{" ..." if len(novel) > 12 else ""}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
