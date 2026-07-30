"""Pub/sub slice 5a (July 30, 2026) — COMBAT_DAMAGE_DEALT in SHADOW mode.

The two damage-application funnels in mtg/combat.py emit once per
application; the attacker loop's game._combat_damage_to_player appends
(what the [COMBAT-TRIGGER] dispatch consumes) are mirrored, and
report_combat_damage_parity diffs the two from end_turn
([EVENT-PARITY-CDD]). One clean batch gates slice 5b — flipping the
consumers (the Obliterator "whenever a source deals damage to THIS
creature" class, battlefield-wide Ohran/Tovolar watchers) onto the bus.
"""
import pytest

import mtg.triggers  # noqa: F401 — registers the slice-5a recorder at import
                     # (production loads it eagerly via mtg.engine)
from mtg import events


def _engine():
    from mtg.engine import GameEngine
    return GameEngine(None)


class TestFunnelEmissions:
    def test_player_funnel_emits_once_per_application(
            self, rules, game, make_card):
        bear = make_card("Bear")
        game.players[0].battlefield.append(bear)
        rules._apply_combat_damage_to_player(
            game, game.players[1], 2, bear)
        assert game._cdd_bus_seen == [(bear.id, "Claude", 2)]

    def test_prevented_damage_does_not_emit(self, rules, game, make_card):
        bear = make_card("Bear")
        game.players[0].battlefield.append(bear)
        game.players[1]._damage_prevented = True
        rules._apply_combat_damage_to_player(
            game, game.players[1], 3, bear)
        assert game._cdd_bus_seen == [], (
            "0 damage isn't dealt (CR 119.3) — no emission")

    def test_infect_poison_still_emits(self, rules, game, make_card):
        # Poison IS dealt combat damage (CR 120.3) — the infect branch
        # must emit like the life branch.
        blighted = make_card("Blighted Agent",
                             type_line="Creature — Human Rogue",
                             power="1", toughness="1",
                             oracle_text="Infect\nThis creature can't be blocked.")
        game.players[0].battlefield.append(blighted)
        rules._apply_combat_damage_to_player(
            game, game.players[1], 1, blighted)
        assert game.players[1].poison == 1
        assert game._cdd_bus_seen == [(blighted.id, "Claude", 1)]

    def test_creature_funnel_emits_creature_kind(self, rules, game, make_card):
        seen = []

        def _probe(g, source=None, target=None, amount=0, target_kind="",
                   **_):
            seen.append((target_kind, getattr(target, 'name', '?'), amount))

        events.subscribe(events.COMBAT_DAMAGE_DEALT, _probe)
        try:
            att = make_card("Attacker", power="3", toughness="3")
            blk = make_card("Blocker", power="1", toughness="4")
            game.players[0].battlefield.append(att)
            game.players[1].battlefield.append(blk)
            rules._apply_combat_damage_to_creature(game, blk, 3, att)
        finally:
            events._subscribers[events.COMBAT_DAMAGE_DEALT].remove(_probe)
        assert ("creature", "Blocker", 3) in seen
        # The parity recorder deliberately ignores creature-kind: there is
        # no legacy consumer list to diff against yet (that IS slice 5b).
        assert game._cdd_bus_seen == []


class TestShadowParity:
    def test_clean_combat_produces_no_parity_lines(
            self, rules, game, make_card, capsys):
        from mtg.triggers import report_combat_damage_parity
        rick = game.players[0]
        att = make_card("Unblocked Guy", power="4", toughness="4")
        rick.battlefield.append(att)
        game.attackers = [att.id]
        game.blockers = {}
        game.active_player_index = 0

        rules.resolve_combat_damage(game)
        assert game._cdd_bus_seen, "the funnel must have emitted"
        assert game._cdd_consumer_seen, "the loop must have appended"
        capsys.readouterr()
        report_combat_damage_parity(game)

        out = capsys.readouterr().out
        assert "[EVENT-PARITY-CDD]" not in out, out
        assert game._cdd_bus_seen == [] and game._cdd_consumer_seen == [], (
            "the report must clear both records")

    def test_funnel_only_call_is_flagged(self, rules, game, make_card, capsys):
        # A damage path that reaches the funnel but never feeds the
        # combat-trigger list = triggers silently never fire. The net's
        # whole job is to see this in a batch.
        from mtg.triggers import report_combat_damage_parity
        bear = make_card("Bear")
        game.players[0].battlefield.append(bear)
        rules._apply_combat_damage_to_player(game, game.players[1], 2, bear)
        capsys.readouterr()
        report_combat_damage_parity(game)
        out = capsys.readouterr().out
        assert "[EVENT-PARITY-CDD]" in out
        assert "never reached the combat-trigger list" in out

    def test_end_turn_runs_the_report(self):
        import inspect
        from mtg.engine import GameEngine
        src = inspect.getsource(GameEngine.end_turn)
        assert "report_combat_damage_parity" in src, (
            "the shadow report must run every turn or the batch gate is blind")
