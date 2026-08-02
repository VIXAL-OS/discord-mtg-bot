"""Aug 2, 2026 (B1) — Claude-path extra-combat CONSUMPTION.

Batch-13's rashmi/mythic reviewer traced the third combat path (Claude
attacks, Rick blocks, main loop resolves) silently discarding Port Razer's
earned additional combat phase after promising it in Discord. The autoplay
main loop now runs `_claude_extra_combats` — the Moraug-loop twin for
Claude's turns — at the CR-correct position in the human-blocks flow (before
the postcombat main) and as a post-flow catch for the internally-resolved
flow (ai_turn DEFERS instead of discarding). Live play still discards
visibly (interactive blocks would be needed); end_turn remains the backstop.

Conventions pinned: local countdown (mid-loop re-grants discarded VISIBLY at
the tail — the bounded-loop-protection choice), `_in_extra_combat` set during
the loop so Karlach's intervening-if declines (CR 603.4), fresh
attacker/blocker lists per declaration, can_block guard on Rick's blocks.
"""
import asyncio

import pytest

from mtg.autoplay import _claude_extra_combats


class _StubCog:
    """The minimal cog surface _claude_extra_combats touches."""

    def __init__(self, attackers_answer, blocks_answer=None,
                 on_resolve=None):
        self.sent = []
        self.resolved = 0
        self.trigger_flag_seen = []
        outer = self

        class _AI:
            async def decide_attackers(self, g, idx):
                return list(attackers_answer)

            async def decide_blocks(self, g, idx, cards):
                return dict(blocks_answer or {})

        class _Rules:
            def can_attack_with(self, g, p, c):
                return True, ""

            def pay_attack_tax(self, g, p, c):
                return True, ""

        class _Engine:
            claude_ai = _AI()
            rules = _Rules()

            def tap_permanent(self, c):
                c.tapped = True

            def process_attack_triggers(self, g, idx):
                outer.trigger_flag_seen.append(
                    bool(getattr(g, '_in_extra_combat', False)))
                return []

            def check_state_based_actions(self, g):
                return []

        self.engine = _Engine()
        self._on_resolve = on_resolve

    async def _autoplay_send(self, thread, msg):
        self.sent.append(msg)

    async def _autoplay_resolve_combat(self, thread, game):
        self.resolved += 1
        # Mirror the real resolver's per-combat clears
        for p in game.players:
            for c in p.battlefield:
                c.attacking = False
        game.attackers = []
        game.blockers = {}
        if self._on_resolve:
            self._on_resolve(game)


def _claude_active(game):
    """Make players[1] (Claude in the conftest game) the active player."""
    game.active_player_index = 1
    return game.players[1]


class TestClaudeExtraCombats:
    def test_no_attackers_consumes_and_clears(self, game, capsys):
        _claude_active(game)
        game._additional_combats = 1
        cog = _StubCog(attackers_answer=[])
        asyncio.run(_claude_extra_combats(cog, None, game))
        assert game._additional_combats == 0
        assert game._in_extra_combat is False
        assert any("No attackers for the additional combat" in m
                   for m in cog.sent)
        assert cog.resolved == 0

    def test_full_round_declares_and_resolves(self, game, make_card):
        claude = _claude_active(game)
        razer = make_card("Port Razer", type_line="Creature — Orc Pirate",
                          power="4", toughness="4")
        razer.summoning_sick = False
        claude.battlefield.append(razer)
        game._additional_combats = 1
        cog = _StubCog(attackers_answer=["Port Razer"])
        asyncio.run(_claude_extra_combats(cog, None, game))
        assert cog.resolved == 1, "the extra combat must actually resolve"
        assert game._additional_combats == 0
        assert game._in_extra_combat is False
        assert any("attacks with: Port Razer" in m for m in cog.sent)
        # The intervening-if flag was UP while attack triggers processed
        # (Karlach's CR 603.4 gate reads it).
        assert cog.trigger_flag_seen == [True]

    def test_mid_loop_regrant_discarded_visibly(self, game, make_card,
                                                capsys):
        claude = _claude_active(game)
        razer = make_card("Port Razer", type_line="Creature — Orc Pirate",
                          power="4", toughness="4")
        razer.summoning_sick = False
        claude.battlefield.append(razer)
        game._additional_combats = 1

        def _regrant(g):
            # Port Razer connects again during the extra combat
            g._additional_combats = getattr(g, '_additional_combats', 0) + 1

        cog = _StubCog(attackers_answer=["Port Razer"], on_resolve=_regrant)
        asyncio.run(_claude_extra_combats(cog, None, game))
        out = capsys.readouterr().out
        assert cog.resolved == 1, "one consumption pass per turn"
        assert game._additional_combats == 0
        assert "granted mid-extra-combat" in out, (
            "the loop-protection discard must be visible, never silent")
