"""Aug 3, 2026 — one-card tails from the missing-mechanics backlog.

These are single-card generators rather than a shared seam, so they are a
rolling backlog. The two here share the wave-2 shape that makes them worth
doing first: a CONDITION that was ignored, so the strong half fired
unconditionally and a generic pattern silently supplied the wrong answer.

PROBE CAVEAT, learned here: tools/probe_mechanics.py funnels everything
through resolve_etb(event_type=...), but production dispatches attack
triggers through resolve_attack_trigger — so an attack template can be
correctly registered and still look unhandled to the probe. These pins use
the production entry points.
"""
import json

import pytest

from rules.effect_templates import build_game_context, get_effect_library

from tests.conftest import _make_card, _make_game

CACHE = json.load(open("data/card_data_cache.json", encoding="utf-8"))
LIB = get_effect_library()


def _cached(name):
    e = CACHE[name]
    card = _make_card(e["name"], type_line=e["type_line"],
                      oracle_text=e["oracle_text"],
                      power=e.get("power") or "1",
                      toughness=e.get("toughness") or "1",
                      mana_cost=e["mana_cost"])
    card.cmc = int(e.get("cmc") or 0)
    return card


class TestPackTactics:
    """Pack tactics (CR 207.2c): "Whenever this creature attacks, IF you
    attacked with creatures with total power 6 or greater this combat, draw a
    card." A generic "whenever this attacks, draw a card" pattern matched and
    drew EVERY combat — free card advantage the card does not have."""

    def _attack(self, buddies):
        game = _make_game()
        rick, claude = game.players
        leader = _cached("werewolf pack leader")
        leader.attacking = True
        rick.battlefield.append(leader)
        for i in range(buddies):
            bear = _make_card(f"Bear {i}", type_line="Creature — Bear",
                              power="3", toughness="3")
            bear.attacking = True
            rick.battlefield.append(bear)
        ctx = build_game_context(game, rick, claude, card=leader,
                                 attacking_creature=leader)
        actions, _desc = LIB.resolve_attack_trigger(
            leader.name, leader.oracle_text, leader.name,
            int(leader.power), rick.name, claude.name, game_context=ctx)
        return actions

    def test_declines_below_six_total_power(self):
        actions = self._attack(0)          # the 3/3 alone
        assert actions and actions[0]["action"] == "no_action"
        assert "pack tactics" in actions[0]["reason"].lower()

    def test_draws_at_exactly_six(self):
        # Decisive on the boundary: 3 + 3 = 6 is "6 or greater".
        actions = self._attack(1)
        assert [a["action"] for a in actions] == ["draw_cards"]
        assert actions[0]["amount"] == 1

    def test_draws_above_six(self):
        actions = self._attack(2)
        assert [a["action"] for a in actions] == ["draw_cards"]

    def test_only_attacking_creatures_count(self):
        """A wide board that is not ATTACKING does not satisfy pack tactics —
        the condition is "you attacked with", not "you control"."""
        game = _make_game()
        rick, claude = game.players
        leader = _cached("werewolf pack leader")
        leader.attacking = True
        rick.battlefield.append(leader)
        for i in range(4):                       # 12 power, none attacking
            rick.battlefield.append(_make_card(
                f"Idle {i}", type_line="Creature — Bear",
                power="3", toughness="3"))
        ctx = build_game_context(game, rick, claude, card=leader,
                                 attacking_creature=leader)
        actions, _ = LIB.resolve_attack_trigger(
            leader.name, leader.oracle_text, leader.name,
            int(leader.power), rick.name, claude.name, game_context=ctx)
        assert actions[0]["action"] == "no_action"


class TestMycolothDevour:
    """Devour 2 (CR 702.81) did not exist, so Mycoloth entered with ZERO
    counters — and his whole payoff is "create a Saproling for EACH +1/+1
    counter", which a generic token pattern resolved as a flat ONE forever.
    He was very nearly a dead card in the deck built around him."""

    def _etb(self, tokens=0, nontokens=0):
        game = _make_game()
        rick, claude = game.players
        myco = _cached("mycoloth")
        rick.battlefield.append(myco)
        for i in range(tokens):
            tok = _make_card(f"Saproling {i}", type_line="Creature — Saproling",
                             power="1", toughness="1")
            tok.is_token = True
            rick.battlefield.append(tok)
        for i in range(nontokens):
            rick.battlefield.append(_make_card(
                f"Real Card {i}", type_line="Creature — Bear",
                power="3", toughness="3"))
        ctx = build_game_context(game, rick, claude, card=myco)
        actions, _desc = LIB.resolve_etb(
            myco.name, myco.oracle_text, rick.name, claude.name,
            game_context=ctx, event_type="etb")
        return actions, myco

    def test_devours_tokens_for_twice_that_many_counters(self):
        actions, _myco = self._etb(tokens=3)
        sacs = [a for a in actions if a["action"] == "sacrifice_permanent"]
        counters = [a for a in actions if a["action"] == "add_counters"]
        assert len(sacs) == 3, "three tokens devoured"
        assert counters and counters[0]["amount"] == 6, "devour 2 = twice that many"

    def test_never_eats_real_cards(self):
        """v1 choice, and the fixture makes it decisive: three real creatures
        and no tokens must produce no sacrifices at all."""
        actions, _ = self._etb(tokens=0, nontokens=3)
        assert actions[0]["action"] == "no_action"
        assert not [a for a in actions if a["action"] == "sacrifice_permanent"]

    def test_upkeep_scales_saprolings_off_the_counters(self):
        game = _make_game()
        rick, claude = game.players
        myco = _cached("mycoloth")
        myco.counters["+1/+1"] = 6
        rick.battlefield.append(myco)
        ctx = build_game_context(game, rick, claude, card=myco)
        actions, _desc = LIB.resolve_etb(
            "mycoloth upkeep", myco.oracle_text, rick.name, claude.name,
            game_context=ctx, event_type="upkeep")
        tokens = [a for a in actions if a["action"] == "create_token"]
        assert tokens and tokens[0]["count"] == 6, (
            "one Saproling per +1/+1 counter, not a flat one")

    def test_no_counters_means_no_saprolings(self):
        game = _make_game()
        rick, claude = game.players
        myco = _cached("mycoloth")
        rick.battlefield.append(myco)
        ctx = build_game_context(game, rick, claude, card=myco)
        actions, _ = LIB.resolve_etb(
            "mycoloth upkeep", myco.oracle_text, rick.name, claude.name,
            game_context=ctx, event_type="upkeep")
        assert actions[0]["action"] == "no_action"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
