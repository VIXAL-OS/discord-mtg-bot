"""Q-H long-tail command audit — eliminated seats may spectate, not mutate.

In a four-seat game a player who has lost stays in the thread. Every
state-changing command therefore needs `_reject_eliminated_seat`, and the
real long-tail risk is not the commands that have it today — it is the NEXT
command someone adds forgetting it. That is a coverage property, so it is
pinned structurally (the same reasoning as the no-raw-mutation pins): a
per-command behavioural test cannot notice a command that does not exist yet.

The scan is AST-based on purpose. A line-oriented parser mis-attributes the
guard to whichever `@commands.command` most recently preceded it, which made
`!priority` look guarded when it is not (correctly — it is spectator-safe)
and made `!exile` look wholly guarded when only its state-changing "from
hand" branch is. An over-broad parser is no more a finding than an
over-broad grep.
"""
import ast
import inspect
from pathlib import Path

import pytest

from mtg.cog import MTGGameCog
from mtg.models import GameState


GUARD = "_reject_eliminated_seat"

# Commands that CHANGE game state and must therefore refuse an eliminated
# seat. Deliberately an explicit list rather than a heuristic: adding a
# command should be a decision about whether it mutates, and this test is
# where that decision gets recorded.
MUTATING_COMMANDS = {
    "play", "suspend", "activate", "target", "respond", "attack", "block",
    "noblock", "doneblocking", "combat", "transform", "resolve", "damage",
    "life", "pass", "turn", "return", "discard", "fix", "undo",
    # Added when this file's coverage pin flagged them on its first run —
    # which is the pin doing its job rather than a list being wrong twice.
    # All four change state: !choice commits a private choice, !f6 and
    # !holdpriority change how that seat's priority behaves, and !exile has a
    # state-changing "from hand" form.
    "choice", "f6", "holdpriority",
    # !exile is BRANCH-scoped: its read-only form is spectator-safe and
    # ungated, and only the "from hand" branch guards. It belongs here
    # because it can mutate, not because every path does.
    "exile",
}

# Read-only commands an eliminated player keeps, per the guard's own message.
SPECTATOR_COMMANDS = {"state", "board", "graveyard", "priority"}


def _command_functions():
    """{command name: ast.FunctionDef} for every @commands.command in the cog."""
    source = Path(inspect.getfile(MTGGameCog)).read_text(encoding="utf-8")
    tree = ast.parse(source)
    out = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
            continue
        for deco in node.decorator_list:
            if not isinstance(deco, ast.Call):
                continue
            target = deco.func
            if getattr(target, "attr", None) != "command":
                continue
            name = None
            for kw in deco.keywords:
                if kw.arg == "name" and isinstance(kw.value, ast.Constant):
                    name = kw.value.value
            if name is None and deco.args:
                first = deco.args[0]
                if isinstance(first, ast.Constant):
                    name = first.value
            if name:
                out[name] = node
    return out


def _calls_guard(func_node):
    return any(
        getattr(n.func, "attr", None) == GUARD
        for n in ast.walk(func_node) if isinstance(n, ast.Call)
    )


class TestEveryMutatingCommandGuards:

    def test_the_scan_finds_the_cog_commands_at_all(self):
        """Control: a coverage pin whose scanner returns nothing would pass
        vacuously forever."""
        commands = _command_functions()
        assert len(commands) > 30, \
            "AST scan found only %d commands — it is not working" % len(commands)
        assert "play" in commands and "attack" in commands

    @pytest.mark.parametrize("name", sorted(MUTATING_COMMANDS))
    def test_mutating_command_refuses_an_eliminated_seat(self, name):
        commands = _command_functions()
        assert name in commands, \
            "!%s is gone or renamed — update MUTATING_COMMANDS" % name
        assert _calls_guard(commands[name]), (
            "!%s changes game state but never calls %s, so an eliminated "
            "player could still use it" % (name, GUARD))

    def test_spectator_commands_are_not_gated(self):
        """ADVERSE CONTROL. The guard's own message promises these remain
        available; gating them would make it lie."""
        commands = _command_functions()
        for name in sorted(SPECTATOR_COMMANDS):
            if name not in commands:
                continue
            assert not _calls_guard(commands[name]), (
                "!%s is advertised to eliminated players as still usable, "
                "but it calls %s" % (name, GUARD))

    def test_a_new_mutating_command_is_a_deliberate_decision(self):
        """The long-tail catch. Any command that touches the guard but is not
        in either list means somebody added one without recording which kind
        it is."""
        commands = _command_functions()
        guarded = {n for n, f in commands.items() if _calls_guard(f)}
        unclassified = guarded - MUTATING_COMMANDS - SPECTATOR_COMMANDS
        assert not unclassified, (
            "these commands guard against eliminated seats but are in "
            "neither list — classify them: %s" % sorted(unclassified))


class TestTheGuardItself:

    def _ctx(self):
        class Ctx:
            def __init__(self):
                self.sent = []

            async def send(self, content=None, **kwargs):
                self.sent.append(content or "")
        return Ctx()

    def _run(self, cog, ctx, game, idx):
        import asyncio
        return asyncio.run(
            MTGGameCog._reject_eliminated_seat(cog, ctx, game, idx))

    def test_an_eliminated_seat_is_refused_and_told_why(self, game):
        cog = type("CogStub", (), {})()
        game.players[1].eliminated = True
        ctx = self._ctx()

        assert self._run(cog, ctx, game, 1) is True
        assert ctx.sent and "eliminated" in ctx.sent[0].lower()

    def test_a_living_seat_passes_through(self, game):
        """ADVERSE CONTROL — the guard must not block live players."""
        cog = type("CogStub", (), {})()
        ctx = self._ctx()

        assert self._run(cog, ctx, game, 0) is False
        assert ctx.sent == []

    def test_an_out_of_range_or_missing_seat_passes_through(self, game):
        """A spectator with no seat at all is not an eliminated player, and
        must not be handed the elimination message."""
        cog = type("CogStub", (), {})()
        for idx in (None, -1, 99):
            ctx = self._ctx()
            assert self._run(cog, ctx, game, idx) is False
            assert ctx.sent == []

    def test_the_message_names_the_commands_it_promises(self, game):
        """The spectator list in the message and SPECTATOR_COMMANDS above are
        the same promise written twice; this keeps them honest."""
        cog = type("CogStub", (), {})()
        game.players[1].eliminated = True
        ctx = self._ctx()
        self._run(cog, ctx, game, 1)

        text = ctx.sent[0].lower()
        for name in SPECTATOR_COMMANDS:
            assert ("`!%s`" % name) in text, (
                "the refusal promises spectating but never mentions !%s" % name)
