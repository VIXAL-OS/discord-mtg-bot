"""July 21, 2026 batch audit — the stack LIFO race (game_1529172174773157998).

The stifle deck's first live batch caught a three-part async race:
Animate Dead on the stack → Rick responds with Disallow targeting it →
Talrand's cast trigger pushed above Disallow (correct LIFO so far) → the
PrioritySystem — whose mirror stack had diverged from game.stack —
resolved ITS top (Animate Dead) while both the counter and the trigger
window were still pending. The reanimation resolved, Disallow later
fizzled ("no longer on the stack"), and the trigger window's async
timeout then declared the trigger "countered" with Stifle still in
Rick's hand (entry vanished ≠ countered), so the Drake was never made.

Fixes pinned here:
1. on_stack_resolve refuses to resolve a matched entry that is BURIED on
   game.stack (CR 608 LIFO gate) — the caster's _await_stack_window
   timeout + extension loop retries once it's genuinely on top.
2. The cast-trigger window only skips inline resolution when the entry's
   `countered` flag is actually set; a vanished entry resolves inline
   ([CAST-TRIGGER-VANISHED]) instead of silently dropping the trigger.
"""
import asyncio
from types import SimpleNamespace

import pytest


def _engine():
    from mtg.engine import GameEngine
    return GameEngine(None)


class TestLifoGuard:
    def _stacked_game(self, make_game, make_card):
        from mtg.models import StackEntry
        game = make_game()
        game.stack_enabled = True
        rick, claude = game.players
        animate = make_card("Animate Dead", mana_cost="{1}{B}", cmc=2,
                            type_line="Enchantment — Aura")
        disallow = make_card("Disallow", mana_cost="{1}{U}{U}", cmc=3,
                             type_line="Instant",
                             oracle_text="Counter target spell, activated "
                                         "ability, or triggered ability.")
        bottom = StackEntry(card=animate, controller_name=claude.name,
                            controller_index=1)
        bottom.priority_id = "stack_41"
        bottom.resolution_event = asyncio.Event()
        top = StackEntry(card=disallow, controller_name=rick.name,
                         controller_index=0, target=animate)
        top.priority_id = "stack_45"
        top.resolution_event = asyncio.Event()
        game.stack.extend([bottom, top])
        return game, bottom, top

    def test_buried_spell_does_not_resolve(self, make_game, make_card, capsys):
        engine = _engine()
        game, bottom, top = self._stacked_game(make_game, make_card)
        engine.setup_stack(game, auto_pass_seconds=0.05)
        assert game._priority_system is not None
        cb = game._priority_system._on_stack_resolve
        fake_obj = SimpleNamespace(name="Animate Dead", controller="Claude",
                                   id="stack_41")
        asyncio.run(cb(fake_obj))
        out = capsys.readouterr().out
        assert "[STACK-LIFO-GUARD]" in out, out
        assert not bottom.resolution_event.is_set(), (
            "a buried spell must NOT have its resolution event fired while "
            "a response sits above it (CR 608)")

    def test_top_spell_resolves_normally(self, make_game, make_card):
        engine = _engine()
        game, bottom, top = self._stacked_game(make_game, make_card)
        engine.setup_stack(game, auto_pass_seconds=0.05)
        cb = game._priority_system._on_stack_resolve
        fake_obj = SimpleNamespace(name="Disallow", controller="Rick Deckard",
                                   id="stack_45")
        asyncio.run(cb(fake_obj))
        assert top.resolution_event.is_set(), (
            "the genuine top of stack must still resolve")

    def test_bottom_resolves_after_top_is_gone(self, make_game, make_card):
        engine = _engine()
        game, bottom, top = self._stacked_game(make_game, make_card)
        engine.setup_stack(game, auto_pass_seconds=0.05)
        game.stack.remove(top)
        cb = game._priority_system._on_stack_resolve
        fake_obj = SimpleNamespace(name="Animate Dead", controller="Claude",
                                   id="stack_41")
        asyncio.run(cb(fake_obj))
        assert bottom.resolution_event.is_set()


class TestVanishedTriggerEntry:
    def _game_with_talrand_and_stifle(self, make_game, make_card):
        game = make_game()
        rick = game.players[0]
        game.active_player_index = 0
        game.stack_enabled = True

        async def _sink(msg):
            return None
        game._stack_send_func = _sink

        talrand = make_card(
            "Talrand, Sky Summoner",
            type_line="Legendary Creature — Merfolk Wizard",
            power="2", toughness="2",
            oracle_text="Flying\nWhenever you cast an instant or sorcery "
                        "spell, create a 2/2 blue Drake creature token "
                        "with flying.")
        rick.battlefield.append(talrand)
        rick.hand.append(make_card(
            "Stifle", type_line="Instant", mana_cost="{U}", cmc=1,
            oracle_text="Counter target activated or triggered ability."))
        return game, rick

    def test_vanished_entry_still_resolves_inline(self, make_game, make_card, capsys):
        # Simulate the live shape: the window's churn removes the trigger
        # entry from game.stack WITHOUT any counter being cast. The trigger
        # must still resolve (the batch's misfire dropped the Drake).
        from mtg.triggers import _check_cast_triggers
        engine = _engine()
        game, rick = self._game_with_talrand_and_stifle(make_game, make_card)

        async def _churn_window(g, send_fn, window_name):
            for e in list(g.stack):
                if not e.is_spell:
                    g.stack.remove(e)
        engine._combat_priority_round = _churn_window

        spell = make_card("Opt", type_line="Instant", mana_cost="{U}", cmc=1,
                          oracle_text="Scry 1.\nDraw a card.")
        asyncio.run(_check_cast_triggers(engine, game, rick, spell))
        out = capsys.readouterr().out
        assert "[CAST-TRIGGER-VANISHED]" in out, out
        assert "[CAST-TRIGGER-COUNTERED]" not in out, (
            "a vanished entry must not be declared countered — no counter "
            "was cast (Stifle never left hand)")
        assert any("Drake" in c.name for c in rick.battlefield), (
            "the uncountered trigger must still make its Drake")

    def test_countered_flag_skips_inline_resolution(self, make_game, make_card, capsys):
        from mtg.triggers import _check_cast_triggers
        engine = _engine()
        game, rick = self._game_with_talrand_and_stifle(make_game, make_card)

        async def _counter_window(g, send_fn, window_name):
            for e in list(g.stack):
                if not e.is_spell:
                    e.countered = True
        engine._combat_priority_round = _counter_window

        spell = make_card("Opt", type_line="Instant", mana_cost="{U}", cmc=1,
                          oracle_text="Scry 1.\nDraw a card.")
        asyncio.run(_check_cast_triggers(engine, game, rick, spell))
        out = capsys.readouterr().out
        assert "[CAST-TRIGGER-COUNTERED]" in out, out
        assert not any("Drake" in c.name for c in rick.battlefield), (
            "a genuinely countered trigger must not resolve")
