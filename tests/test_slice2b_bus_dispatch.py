"""Pub/sub slice 2b (July 21, 2026) — creature-enters dispatch via the bus.

The creature-enters watcher scan is now DRIVEN BY PERMANENT_ENTERED: the
subscriber (mtg/triggers.py:_creature_entered_subscriber) runs the full
sync dispatch (Tier 1/1.5 scan + XMage pass + Tier-3 queueing) and the
former direct-call sites drain game._pending_messages at the position the
old scan call occupied. These tests pin the end-to-end path: a bare emit
must produce watcher effects with no direct scan call anywhere.
"""
import pytest

from mtg import events


def _engine():
    from mtg.engine import GameEngine
    return GameEngine(None)


class TestBusDrivenCreatureWatchers:
    def _soul_warden_board(self, make_game, make_card, engine):
        game = make_game()
        game._rules_engine = engine.rules
        rick, claude = game.players
        warden = make_card(
            "Soul Warden", power="1", toughness="1",
            oracle_text="Whenever another creature enters, you gain 1 life.")
        claude.battlefield.append(warden)
        return game, rick, claude

    def test_bare_emit_fires_soul_warden(self, make_game, make_card):
        engine = _engine()
        game, rick, claude = self._soul_warden_board(make_game, make_card, engine)
        bear = make_card("Bear", power="2", toughness="2")
        rick.battlefield.append(bear)
        events.emit(events.PERMANENT_ENTERED, game, card=bear,
                    controller=rick, via="test", rules=engine.rules)
        assert claude.life == 41, (
            "a bare PERMANENT_ENTERED emit must run the watcher dispatch — "
            "no direct scan call exists anymore")
        # Display lines surfaced via the pending channel for the call site
        # to drain.
        assert getattr(game, '_pending_messages', None), (
            "watcher display lines must be queued for the drain")

    def test_create_token_path_fires_watchers_end_to_end(self, make_game, make_card):
        from mtg.actions import execute_action_on_state
        engine = _engine()
        game, rick, claude = self._soul_warden_board(make_game, make_card, engine)
        msg = execute_action_on_state(engine.rules, game, {
            "action": "create_token", "player": rick.name,
            "name": "Saproling", "power": 1, "toughness": 1,
            "types": "Creature — Saproling", "count": 2})
        assert claude.life == 42, (
            f"two tokens entering must fire Soul Warden twice (life: "
            f"{claude.life}); msg: {msg}")

    def test_noncreature_entry_does_not_fire_creature_watchers(
            self, make_game, make_card):
        engine = _engine()
        game, rick, claude = self._soul_warden_board(make_game, make_card, engine)
        rock = make_card("Mind Stone", type_line="Artifact",
                         oracle_text="{T}: Add {C}.")
        rick.battlefield.append(rock)
        events.emit(events.PERMANENT_ENTERED, game, card=rock,
                    controller=rick, via="test", rules=engine.rules)
        assert claude.life == 40

    def test_bare_emit_fires_constellation(self, make_game, make_card):
        # Slice 2b (2/2): the enchantment watcher scan is bus-driven too.
        engine = _engine()
        game = make_game()
        game._rules_engine = engine.rules
        rick = game.players[0]
        eidolon = make_card(
            "Eidolon of Blossoms", power="2", toughness="2",
            type_line="Enchantment Creature — Spirit",
            oracle_text="Constellation — Whenever this enchantment or "
                        "another enchantment you control enters, draw a card.")
        rick.battlefield.append(eidolon)
        rick.library.append(make_card("Forest", type_line="Basic Land — Forest"))
        aura = make_card("Wild Growth", type_line="Enchantment — Aura",
                         mana_cost="{G}", cmc=1,
                         oracle_text="Enchant land\nWhenever enchanted land "
                                     "is tapped for mana, its controller "
                                     "adds an additional {G}.")
        rick.battlefield.append(aura)
        hand_before = len(rick.hand)
        events.emit(events.PERMANENT_ENTERED, game, card=aura,
                    controller=rick, via="test", rules=engine.rules)
        assert len(rick.hand) == hand_before + 1, (
            "a bare emit for an entering enchantment must fire the "
            "constellation watcher")

    def test_unusable_engine_ref_is_skipped_and_flagged(self, make_game, make_card, capsys):
        game = make_game()
        rick = game.players[0]
        bear = make_card("Bear", power="2", toughness="2")
        rick.battlefield.append(bear)
        events.emit(events.PERMANENT_ENTERED, game, card=bear,
                    controller=rick, via="test", rules=None)
        out = capsys.readouterr().out
        assert "[ETB-BUS]" in out, (
            "a payload without a usable engine must be visibly skipped — "
            "the parity recorder then flags the entry next batch")
