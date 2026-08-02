"""Aug 2, 2026 (B2) — the destroy action joins the CREATURE_DIED bus.

Single-target destroy was the LAST death path invisible to the bus: it
resolved dies triggers inline via engine_ref._check_dies_triggers_sync and
never emitted CREATURE_DIED, so bus consumers (the Meren/Ezuri experience
grant, any future subscriber) missed every destroy-action death, and the
same-batch CR 603.10 visibility machinery never saw them. Now it queues via
queue_deaths (the slice-3b choke point) and the dispatcher drains at the
next check_state_based_actions — the same deferred semantics wipes, SBA
deaths, and sacrifices have had since July (the July-24 FP-ledger entry:
"no trigger output near the resolution" is not "trigger never fired").

The no-double-fire guarantees:
- dies triggers fire ONCE (at the drain, not inline+drain),
- experience grants ONCE (the subscriber; the batch-13 direct call is gone
  — tests/test_aug2_reviewer_wave.py's ==1 pin catches a regression).
"""
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


class TestDestroyOnTheBus:
    def test_destroy_death_queues_then_drains_once(self, game, make_card):
        from mtg.engine import GameEngine
        from mtg.actions import execute_action_on_state
        engine = GameEngine(None)
        rick, claude = game.players
        artist = make_card(
            "Blood Artist", type_line="Creature — Vampire",
            power="0", toughness="1",
            oracle_text="Whenever this creature or another creature dies, "
                        "target player loses 1 life and you gain 1 life.")
        bear = make_card("Bear", type_line="Creature — Bear",
                         power="2", toughness="2")
        rick.battlefield.extend([artist, bear])
        execute_action_on_state(engine.rules, game,
                                {"action": "destroy", "card": "Bear"})
        assert bear in rick.graveyard
        assert any(c is bear for c, _p in (game._recently_died or [])), (
            "the destroy-path death must reach the bus-fed queue")
        engine.check_state_based_actions(game)
        assert claude.life == 39, (
            f"Blood Artist must drain exactly once at the drain "
            f"(claude at {claude.life})")
        assert rick.life == 41
        # A second SBA pass must NOT re-fire (the wave was consumed).
        engine.check_state_based_actions(game)
        assert claude.life == 39 and rick.life == 41, "double-fire"

    def test_destroy_handler_no_longer_scans_inline(self):
        src = (REPO / "mtg" / "actions.py").read_text(encoding="utf-8")
        i = src.index("B2, the bus unification")
        block = src[i:i + 1200]
        assert "_queue_deaths_3a(game, [(card, owner)])" in block
        assert "_check_dies_triggers_sync" not in block, (
            "inline resolution is back — queueing + inline = double-fire")
