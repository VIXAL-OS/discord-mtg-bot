"""CR 616.1 controller-chooses-order — pytest port of the May 30 scripted
regression (test_replacement_controller_order.py at the repo root).

Furnace of Rath (double ALL damage) and Gisela, Blade of Goldnight (prevent
half the damage dealt to her controller, rounded up) both apply to the SAME
damage event. Mixed-direction multipliers are non-commutative under floor
rounding, so the affected player chooses the order (CR 616.1) — and picks
the one that minimizes the damage they take.

This interaction is intrinsically rare in live autoplay (needs Furnace +
Gisela co-resident at the moment her controller takes damage); the May 26
batch never executed the branch. Forcing it here found 3 real bugs the
first time it ran. The matrix structurally can't cover this — pytest can.
"""
import pytest

from rules.replacement import (
    EventType,
    GameEvent,
    ReplacementEngine,
    scan_oracle_for_replacements,
)

FURNACE_ORACLE = (
    "If a source would deal damage to a permanent or player, "
    "it deals double that damage to that permanent or player instead."
)


def build_engine(controller: str) -> ReplacementEngine:
    """Register Furnace of Rath + Gisela the way the engine does at ETB."""
    eng = ReplacementEngine()
    for eff in scan_oracle_for_replacements(
            "furnace_1", "Furnace of Rath", FURNACE_ORACLE, controller):
        eng.add_effect(eff)
    for eff in scan_oracle_for_replacements(
            "gisela_1", "Gisela, Blade of Goldnight", "", controller):
        eng.add_effect(eff)
    return eng


def damage_to(eng: ReplacementEngine, player: str, amount: int, source_ctrl: str) -> int:
    ev = GameEvent(event_type=EventType.DAMAGE, affected_player=player,
                   amount=amount, source_controller=source_ctrl)
    return eng.process_event_sync(ev).amount


@pytest.mark.parametrize(
    "amount,expected",
    [
        (3, 2),  # halve->1 then double->2 beats double->6 then halve->3
        (5, 4),  # halve->2 then double->4 beats double->10 then halve->5
        (4, 4),  # even amount: order-independent
        (1, 0),  # halve(round up)->0, doubling 0 stays 0
    ],
)
def test_gisela_vs_furnace_non_commutative(amount, expected):
    """Damage TO Gisela's controller: the affected player (her controller)
    orders the replacements to minimize what they take."""
    eng = build_engine("Rick")
    assert damage_to(eng, "Rick", amount, "Claude") == expected


def test_furnace_plus_gisela_opponent_doubler_commutative():
    """Damage TO the opponent: Furnace x2 and Gisela's double-to-opponents
    x2 are same-direction, hence commutative -> x4 either way."""
    eng = build_engine("Rick")
    assert damage_to(eng, "Claude", 3, "Rick") == 12
