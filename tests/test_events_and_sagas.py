"""June 10 round 3: pub/sub bus slice 1 + the Teachings of the Kirin
chapter-dispatching template (closing two of the three deep-dive deferrals).
"""
from types import SimpleNamespace

import pytest

from mtg import events


CH1 = "Mill three cards. Create a 1/1 colorless Spirit creature token."
CH2 = "Put a +1/+1 counter on target creature you control."
CH3 = "Exile this Saga, then return it to the battlefield transformed under your control."


# ---------------------------------------------------------------------------
# Event bus — slice 1 (LIFE_GAINED)
# ---------------------------------------------------------------------------

class TestEventBus:
    def test_subscribe_is_idempotent(self):
        calls = []

        def handler(game, **payload):
            calls.append(payload)

        events.subscribe("test_event_idem", handler)
        events.subscribe("test_event_idem", handler)  # duplicate — ignored
        assert events.subscriber_count("test_event_idem") == 1
        events.emit("test_event_idem", None, x=1)
        assert len(calls) == 1

    def test_emit_unknown_event_is_noop(self):
        events.emit("never_subscribed_event", None, anything=True)  # no raise

    def test_life_gained_has_the_gain_trigger_subscriber(self):
        # combat.py registers the gain-life scan at import time (slice 1).
        import mtg.combat  # noqa: F401 — ensure registration ran
        assert events.subscriber_count(events.LIFE_GAINED) >= 1

    def test_gain_via_bus_fires_vito(self, rules, game, make_card):
        # End-to-end: apply_life_gain → emit(LIFE_GAINED) → subscriber → Vito.
        rick, claude = game.players
        vito = make_card("Vito, Thorn of the Dusk Rose", power="1", toughness="3",
                         oracle_text="Whenever you gain life, each opponent loses that much life.")
        claude.battlefield.append(vito)
        ok, amt, _ = rules._apply_life_gain(game, claude, 2, source_name="test")
        assert ok and amt == 2
        assert rick.life == 38, "bus-routed gain trigger did not fire"


# ---------------------------------------------------------------------------
# Teachings of the Kirin — chapter dispatch (read-first close of the deferral)
# ---------------------------------------------------------------------------

class TestTeachingsOfTheKirin:
    def test_chapter_one_mills_and_makes_spirit(self, lib):
        actions, _ = lib.resolve_etb("Teachings of the Kirin", CH1, "Rick", "Claude")
        kinds = [a["action"] for a in actions]
        assert "mill" in kinds, "chapter I mill missing"
        tok = next(a for a in actions if a["action"] == "create_token")
        assert tok["name"] == "Spirit" and tok["power"] == 1 and tok["toughness"] == 1
        mill = next(a for a in actions if a["action"] == "mill")
        assert mill["amount"] == 3 and mill["player"] == "Rick"

    def test_chapter_two_counters_biggest_own_creature(self, lib):
        actions, _ = lib.resolve_etb(
            "Teachings of the Kirin", CH2, "Rick", "Claude",
            game_context={"_controller_creatures": [
                {"name": "Llanowar Elves", "power": 1},
                {"name": "Verduran Enchantress", "power": 0},
                {"name": "Sythis, Harvest's Hand", "power": 1},
            ]})
        assert actions[0]["action"] == "add_counters"
        assert actions[0]["counter_type"] == "+1/+1"
        # Biggest by power (ties go to first seen — Llanowar at power 1).
        assert actions[0]["card"] in ("Llanowar Elves", "Sythis, Harvest's Hand")

    def test_chapter_two_no_creatures_is_noop(self, lib):
        actions, _ = lib.resolve_etb(
            "Teachings of the Kirin", CH2, "Rick", "Claude",
            game_context={"_controller_creatures": []})
        assert actions[0]["action"] == "no_action"

    def test_chapter_three_defers_to_engine_transform(self, lib):
        # The progression path intercepts transform chapters before the
        # library; this branch is defensive but must never invent actions.
        actions, _ = lib.resolve_etb("Teachings of the Kirin", CH3, "Rick", "Claude")
        assert actions[0]["action"] == "no_action"
        assert "transform" in actions[0]["reason"].lower()
