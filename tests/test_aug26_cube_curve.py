"""The cube deck-builder curve term (Aug 26, 2026 — the MaRo skeleton import).

auto_build_deck took the top 23 nonlands purely by score — a top-heavy pool
built a deck of six-drops. apply_curve_repair adds SOFT floors/caps (cheap
floor, top-end cap, creature floor); a healthy score-greedy build must pass
through UNCHANGED. Tests call the real module-level function (never a
mirrored copy — the Aug 2 score_card_for_deck lesson).
"""

from cube_draft import (
    CURVE_MAX_TOP, CURVE_MIN_CHEAP, CURVE_MIN_CREATURES, apply_curve_repair,
)
from tests.conftest import _make_card


def _c(name, cmc, creature=True, score=10.0):
    card = _make_card(
        name,
        type_line="Creature — Bear" if creature else "Sorcery",
        cmc=cmc,
        power="2" if creature else None,
        toughness="2" if creature else None,
    )
    return card, score


class TestCurveRepair:
    def test_top_heavy_pool_is_repaired(self):
        # 12 high-scored 6-drops + enough cheaper bench that the constraints
        # ARE satisfiable — the fixture must make the repair the deciding
        # factor (an under-supplied pool legitimately keeps extra 6-drops,
        # since the constraints are soft).
        scored = ([_c(f"Big{i}", 6, score=12.0) for i in range(12)]
                  + [_c(f"Mid{i}", 3, score=9.0) for i in range(8)]
                  + [_c(f"Cheap{i}", 2, score=8.0) for i in range(12)])
        deck, bench = apply_curve_repair(scored, 23)
        assert len(deck) == 23
        assert sum(1 for c in deck if c.cmc >= 6) <= CURVE_MAX_TOP
        assert sum(1 for c in deck if c.cmc <= 2) >= CURVE_MIN_CHEAP

    def test_healthy_build_is_unchanged(self):
        # The no-distortion property: when the score-greedy top 23 already
        # satisfies every constraint, the curve term must not move a card.
        scored = ([_c(f"Cheap{i}", 2, score=12.0) for i in range(8)]
                  + [_c(f"Mid{i}", 3, score=11.0) for i in range(8)]
                  + [_c(f"Four{i}", 4, score=10.0) for i in range(7)]
                  + [_c(f"Bench{i}", 3, score=5.0) for i in range(10)])
        deck, _ = apply_curve_repair(scored, 23)
        greedy = [c for c, _s in scored[:23]]
        assert deck == greedy

    def test_creature_floor(self):
        # A spell-heavy top 23 with benched creatures must pull bodies in.
        scored = ([_c(f"Spell{i}", 3, creature=False, score=12.0)
                   for i in range(23)]
                  + [_c(f"Bear{i}", 2, score=8.0) for i in range(15)])
        deck, _ = apply_curve_repair(scored, 23)
        assert sum(1 for c in deck
                   if "creature" in (c.type_line or "").lower()) >= CURVE_MIN_CREATURES

    def test_soft_constraints_never_break_deck_size(self):
        # An unfixable pool (all 6-drops, empty bench below 6) must still
        # return a full deck — the constraints are SOFT.
        scored = [_c(f"Big{i}", 7, score=10.0) for i in range(23)]
        deck, bench = apply_curve_repair(scored, 23)
        assert len(deck) == 23 and not bench

    def test_swaps_prefer_lowest_scored_offender(self):
        # The repair must sacrifice the WORST offender, not an arbitrary one:
        # with two 6-drops over the cap, the lower-scored one leaves.
        scored = ([_c(f"Big{i}", 6, score=12.0 - i * 0.1) for i in range(6)]
                  + [_c(f"Cheap{i}", 2, score=9.0) for i in range(20)])
        deck, bench = apply_curve_repair(scored, 23)
        kept_bigs = sorted(c.name for c in deck if c.cmc >= 6)
        assert kept_bigs == ["Big0", "Big1", "Big2", "Big3"], (
            "the two LOWEST-scored six-drops (Big4, Big5) must be the ones "
            "swapped out")
