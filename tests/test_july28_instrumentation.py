"""July 28, 2026 — Phase 2, the audit-integrity cluster.

None of these change how a game plays. They change whether the NEXT batch audit
can answer questions about itself, which is why they land before the batch
rather than after it. Three of the four are ordering bugs, so they are pinned
structurally (AST) — a behavioural pin would need a Discord thread.

E1  The cube draft phase reached NEITHER log file. The GameLogger was
    constructed ~150 lines after the last draft message was sent, and both sinks
    are registration-gated: _thread_send only writes to a logger already in
    game_loggers, and StdoutTee only tees once add_game has run. Measured across
    all four cube games in the loose logs: zero [DRAFT-CLAUDE] lines, and every
    console log's first content line was the logger's own confirmation print.
    Pick counts and duplicate-card safety were unauditable by construction.

E2  The same class, but for EVERY autoplay game: create_game ran before the
    logger was registered, and create_game is where deck loading prints
    [COMPANION] / [PARTNER] / [OATHBREAKER] / [ADVENTURE] / [TRANSFORM] /
    [SPLIT] and the "[SCRYFALL] WARNING: Failed to fetch X ... type_line will be
    empty!" line. Across ~600 loose console logs — including 8 companion, 8
    partner, 16 adventure and 16 transform games — every one of those strings
    had zero hits. "Was the companion ever offered?" could not be answered.

E3  Cube [GAME-INIT] hardcoded deck0=draft(40) deck1=draft(40), so the
    format-compliance deck-size check could not fail for cube. Not merely a
    tautology: auto_build_deck's fill loops have no upper trim, so a pool heavy
    in text-less lands can return more than 40 while the line still says 40.

E4  [PW-ACTIVATE] had exactly one emit site, on the Claude path. There are
    THREE planeswalker activation implementations — human (cog), Claude
    (engine), Rick (autoplay) — all converging on PlaneswalkerManager.activate,
    and the other two printed nothing. In game_1529160643909914765 Rick
    activated Daretti five times, each backed by a [PW-TEMPLATE] line, while
    grep -c '[PW-ACTIVATE]' on that log returned 0. That false trail was
    produced during the July 27 audit itself.
"""
import ast
import inspect
from pathlib import Path

import pytest


_ROOT = Path(__file__).resolve().parent.parent


def _function_node(relpath, func_name):
    tree = ast.parse((_ROOT / relpath).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func_name:
            return node
    pytest.fail(f"{func_name} not found in {relpath}")


def _first_call_lineno(func_node, predicate):
    linenos = [n.lineno for n in ast.walk(func_node)
               if isinstance(n, ast.Call) and predicate(n)]
    return min(linenos) if linenos else None


def _callee_name(call):
    fn = call.func
    if isinstance(fn, ast.Name):
        return fn.id
    if isinstance(fn, ast.Attribute):
        return fn.attr
    return ""


class TestCubeDraftIsLogged:

    def test_logger_is_registered_before_any_draft_message(self):
        fn = _function_node("cube_draft.py", "_run_autodraft")
        logger_ln = _first_call_lineno(fn, lambda c: _callee_name(c) == "GameLogger")
        send_ln = _first_call_lineno(fn, lambda c: _callee_name(c) == "_autodraft_send")
        assert logger_ln is not None, "the draft must construct a GameLogger"
        assert send_ln is not None, "the draft must send messages"
        assert logger_ln < send_ln, (
            f"GameLogger at line {logger_ln} is created AFTER the first "
            f"_autodraft_send at line {send_ln} — every message before it is "
            f"invisible in both log files")

    def test_stdout_tee_is_attached_before_the_pick_prints(self):
        fn = _function_node("cube_draft.py", "_run_autodraft")
        tee_ln = _first_call_lineno(fn, lambda c: _callee_name(c) == "add_game")
        assert tee_ln is not None
        # The pod build (and therefore every [DRAFT-CLAUDE] pick print) begins
        # after the thread is created; the tee must beat it.
        send_ln = _first_call_lineno(fn, lambda c: _callee_name(c) == "_autodraft_send")
        assert tee_ln < send_ln


class TestAutoplayDeckSetupIsLogged:

    def test_logger_is_registered_before_create_game(self):
        fn = _function_node("mtg/autoplay.py", "_run_single_autoplay")
        logger_ln = _first_call_lineno(fn, lambda c: _callee_name(c) == "GameLogger")
        create_ln = _first_call_lineno(fn, lambda c: _callee_name(c) == "create_game")
        assert logger_ln is not None and create_ln is not None
        assert logger_ln < create_ln, (
            f"GameLogger at line {logger_ln} is created AFTER create_game at "
            f"line {create_ln} — all deck-loading diagnostics "
            f"([COMPANION]/[PARTNER]/[SCRYFALL] warnings) are dropped")

    def test_only_one_logging_block_survives(self):
        """The fix MOVED the block; a stray copy would double-register."""
        fn = _function_node("mtg/autoplay.py", "_run_single_autoplay")
        count = sum(1 for n in ast.walk(fn)
                    if isinstance(n, ast.Call) and _callee_name(n) == "GameLogger")
        assert count == 1


class TestCubeGameInitReportsRealDeckSizes:

    def test_no_hardcoded_forty(self):
        src = (_ROOT / "cube_draft.py").read_text(encoding="utf-8")
        assert "deck0=draft(40) deck1=draft(40)" not in src, (
            "a literal deck size makes the format-compliance check a no-op")

    def test_the_line_interpolates_something(self):
        src = (_ROOT / "cube_draft.py").read_text(encoding="utf-8")
        assert "deck0=draft({" in src


class TestPlaneswalkerActivationIsTaggedOnEveryPath:

    def test_tag_lives_in_the_shared_manager(self):
        src = inspect.getsource(
            __import__("rules.planeswalker", fromlist=["planeswalker"]))
        assert "[PW-ACTIVATE]" in src, (
            "the tag must print from PlaneswalkerManager.activate, the one "
            "choke point all three activation paths converge on")

    def test_engine_no_longer_prints_its_own_copy(self):
        """Two emits would double-count in every audit grep."""
        src = (_ROOT / "mtg" / "engine.py").read_text(encoding="utf-8")
        assert 'print(f"[PW-ACTIVATE]' not in src

    def test_tag_carries_the_activating_player(self):
        src = (_ROOT / "rules" / "planeswalker.py").read_text(encoding="utf-8")
        line = next(l for l in src.split("\n")
                    if "[PW-ACTIVATE]" in l and "print(" in l)
        assert "player" in line, (
            "Rick-vs-Claude attribution is the whole point — the old tag "
            "omitted the player name")

    def test_it_actually_prints_on_a_successful_activation(self, game, make_card, capsys):
        """Behavioural check on the real manager: no path-specific stubbing."""
        import asyncio

        from rules.planeswalker import PlaneswalkerManager

        rick = game.players[0]
        daretti = make_card(
            "Daretti, Scrap Savant",
            type_line="Legendary Planeswalker — Daretti",
            oracle_text=("+2: Discard a card, then draw a card.\n"
                         "-2: Sacrifice an artifact. If you do, return "
                         "target artifact card from your graveyard to the battlefield."),
            power=None, toughness=None,
        )
        daretti.loyalty_counters = 3
        rick.battlefield.append(daretti)
        rick.hand.append(make_card("Mountain", type_line="Basic Land — Mountain"))
        rick.library.append(make_card("Forest", type_line="Basic Land — Forest"))

        mgr = PlaneswalkerManager(None)
        capsys.readouterr()
        asyncio.run(mgr.activate(game, rick, daretti, 0, None))
        out = capsys.readouterr().out
        if "[PW-ACTIVATE]" not in out:
            # Refunded / rejected activations legitimately print no tag; only
            # assert the tag when the activation actually succeeded.
            assert "[PW-REFUND]" in out or "❌" in out, (
                f"activation neither succeeded nor reported why:\n{out}")
        else:
            assert "Rick" in out


# ---------------------------------------------------------------------------
# Post-game-end message flush (game_1530441479389184000)
# ---------------------------------------------------------------------------

class TestNothingPostsAfterTheGameEnds:
    """July 24 gated trigger DISPATCH on `not game.ended` (CR 104.2a), but
    nothing gated the message FLUSH — so a cast coroutine suspended across the
    end of the game could still post afterwards.

    game_1530441479389184000: Pact of Negation was countered by Frilled Mystic
    at 01:18:13 and Discord reported it then. The Pact's own long-suspended
    cast_spell_async (60s of LIFO extension churn) unwound at 01:18:59 and
    re-posted "❌ Pact of Negation was countered!" AFTER the "🏆 Claude wins!"
    summary. Nothing was wrong with the game — Frilled Mystic's counter is
    legal and the outcome was correct — but a duplicate arriving after the win
    reads as a rules bug.

    Suppressed from Discord, kept on console under [POST-GAME-SUPPRESSED] so
    the record survives for audits.
    """

    class _Thread:
        id = 4242

        def __init__(self):
            self.sent = []

        async def send(self, content=None, embed=None):
            self.sent.append(content)
            return None

    def _cog(self, game):
        from mtg.cog import MTGGameCog
        cog = MTGGameCog.__new__(MTGGameCog)

        class _Engine:
            games = {4242: game}
        cog.engine = _Engine()
        cog.game_loggers = {}
        cog._stdout_tee = None
        return cog

    async def _send(self, cog, thread, content, **kw):
        return await cog._autoplay_send(thread, content, **kw)

    def test_a_stale_cast_message_is_suppressed(self, game, capsys):
        import asyncio
        thread = self._Thread()
        cog = self._cog(game)
        game.ended = True
        capsys.readouterr()
        asyncio.run(self._send(cog, thread,
                               "❌ **Pact of Negation** was countered!"))
        out = capsys.readouterr().out
        assert thread.sent == [], "nothing may reach Discord after the game ends"
        assert "[POST-GAME-SUPPRESSED]" in out, "but the record must survive"
        assert "Pact of Negation" in out

    def test_the_win_summary_itself_still_posts(self, game):
        """The summary is sent AFTER game.ended is set, so it must opt out."""
        import asyncio
        thread = self._Thread()
        cog = self._cog(game)
        game.ended = True
        asyncio.run(self._send(cog, thread, "🏆 **Claude wins!**", final=True))
        assert thread.sent and "wins" in thread.sent[0]

    def test_ordinary_play_is_untouched(self, game):
        import asyncio
        thread = self._Thread()
        cog = self._cog(game)
        game.ended = False
        asyncio.run(self._send(cog, thread, "⚡ Talrand creates a Blue Drake"))
        assert thread.sent and "Talrand" in thread.sent[0]

    def test_it_fails_open_when_the_game_is_unknown(self, game):
        """A thread with no registered game must not lose its messages."""
        import asyncio
        thread = self._Thread()
        thread.id = 999999          # not in cog.engine.games
        cog = self._cog(game)
        game.ended = True
        asyncio.run(self._send(cog, thread, "⚡ some message"))
        assert thread.sent, "unknown game must fail open, not swallow"

    def test_every_terminal_summary_opts_out(self):
        """If a summary forgets final=True it disappears — so pin that each
        'wins!'/'Draw!'/'no winner' send in the autoplay tail carries it."""
        root = Path(__file__).resolve().parent.parent
        src = (root / "mtg" / "autoplay.py").read_text(encoding="utf-8")
        tail = src[src.index("def _run_single_autoplay"):]
        for marker in ("wins!", "Draw!", "no winner"):
            idx = tail.find(marker)
            assert idx != -1, f"{marker} summary not found"
            # the closing paren of that send call must carry final=True
            window = tail[idx:idx + 600]
            assert "final=True" in window, f"{marker} summary is not marked final"
