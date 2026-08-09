"""Headless pins for the two lottery rows with thin/zero unit coverage
(Aug 9, 2026 follow-up — the July-21 "unexercised paths are unit-pinned"
playbook).

Verified before writing: every other row on the live-lottery list (DRAUGR,
LIBRARY-TOP, CREW, MELD, DRAW-EMPTY-WIN, SPLICE, FORETELL, BUYBACK,
embalm/eternalize/unearth, the R4 restricted-mana rows) already has
headless pins in earlier test files. The two genuinely thin spots:
- saga chapter III + the CR 704.5s sacrifice: ZERO prior test coverage
- the oathbreaker signature-spell FULL cycle (cast from CZ → resolve →
  return to CZ → tax accumulation): cast-gate pins existed, the cycle
  didn't.

Fixtures are LIVE-SHAPED: real cache oracle text, the real engine
executor / upkeep-advance / SBA paths.
"""

import asyncio
import json
from pathlib import Path

import pytest

from mtg.constants import Phase

_ROOT = Path(__file__).resolve().parent.parent
_CACHE = json.loads(
    (_ROOT / "data" / "card_data_cache.json").read_text(encoding="utf-8"))


def _cached(name: str):
    return _CACHE[name.lower()]


def _engine():
    from mtg.engine import GameEngine
    return GameEngine(None)


# ---------------------------------------------------------------------------
# Saga chapter III + CR 704.5s (the zero-coverage gap)
# ---------------------------------------------------------------------------

class TestSagaFinalChapterAndSacrifice:
    def _saga(self, make_card, name):
        entry = _cached(name)
        return make_card(name, type_line=entry["type_line"],
                         oracle_text=entry.get("oracle_text", "") or "",
                         power=None, toughness=None)

    def test_chapter_three_fires_and_saga_is_sacrificed(
            self, make_game, make_card):
        # History of Benalia at lore 2 → the real post-draw advance adds
        # the third counter and fires chapter III; the next SBA pass must
        # sacrifice the saga (CR 714.4 via 704.5s). Live batches keep
        # ending before any saga reaches III — this is the headless close.
        engine = _engine()
        game = make_game()
        game._rules_engine = engine.rules
        engine.rules.engine_ref = engine
        rick = game.players[0]
        saga = self._saga(make_card, "History of Benalia")
        saga.counters = {"lore": 2}
        rick.battlefield.append(saga)
        msgs = engine._advance_sagas(game, rick)
        assert saga.counters.get("lore") == 3
        assert any("Chapter 3" in m or "III" in m for m in msgs), (
            f"chapter III must fire: {msgs}")
        engine.check_state_based_actions(game)
        assert saga not in rick.battlefield, (
            "CR 704.5s: a Saga with lore >= its final chapter is sacrificed "
            "after the chapter resolves")
        assert saga in rick.graveyard, "a sacrificed Saga goes to the graveyard"

    def test_saga_below_final_chapter_survives_sba(self, make_game, make_card):
        engine = _engine()
        game = make_game()
        game._rules_engine = engine.rules
        engine.rules.engine_ref = engine
        rick = game.players[0]
        saga = self._saga(make_card, "History of Benalia")
        saga.counters = {"lore": 2}
        rick.battlefield.append(saga)
        engine.check_state_based_actions(game)
        assert saga in rick.battlefield, (
            "a Saga below its final chapter must survive the SBA")

    def test_transforming_saga_transforms_instead_of_sacrifice(
            self, make_game, make_card):
        # The 704.5s EXCEPTION branch: The Restoration of Eiganjo's final
        # chapter IS the transform (CR 715.4) — the SBA must flip it to
        # Architect of Restoration via the back-face table, never
        # sacrifice it.
        engine = _engine()
        game = make_game()
        game._rules_engine = engine.rules
        engine.rules.engine_ref = engine
        rick = game.players[0]
        saga = self._saga(make_card, "The Restoration of Eiganjo")
        saga.counters = {"lore": 3}
        rick.battlefield.append(saga)
        engine.check_state_based_actions(game)
        assert saga not in rick.graveyard, (
            "a transforming saga must NOT be sacrificed (CR 715.4)")
        assert any(c.name == "Architect of Restoration"
                   for c in rick.battlefield), (
            "the final chapter IS the transform — the back face must be on "
            "the battlefield")


# ---------------------------------------------------------------------------
# Oathbreaker signature spell: the FULL cycle
# ---------------------------------------------------------------------------

class TestSignatureSpellFullCycle:
    def _setup(self, make_game, make_card, mountains=3):
        engine = _engine()
        game = make_game("oathbreaker")
        game._rules_engine = engine.rules
        engine.rules.engine_ref = engine
        game.active_player_index = 1
        game.phase = Phase.MAIN1
        rick, claude = game.players
        chandra = make_card("Chandra, Torch of Defiance",
                            type_line=_cached("Chandra, Torch of Defiance")["type_line"],
                            oracle_text=_cached("Chandra, Torch of Defiance").get("oracle_text", ""),
                            power=None, toughness=None)
        chandra.is_commander = True
        chandra.loyalty = "4"
        chandra.loyalty_counters = 4
        claude.battlefield.append(chandra)
        bolt = make_card("Lightning Bolt", type_line="Instant",
                         oracle_text=_cached("Lightning Bolt")["oracle_text"],
                         mana_cost="{R}", cmc=1)
        bolt.is_signature_spell = True
        claude.command_zone.append(bolt)
        for i in range(mountains):
            claude.battlefield.append(make_card(
                f"Mountain{i}", type_line="Basic Land — Mountain",
                oracle_text="", power=None, toughness=None))
        return engine, game, rick, claude, chandra, bolt

    def test_cast_resolve_return_to_cz_then_tax_on_recast(
            self, make_game, make_card):
        # The cycle live batches keep missing (the oathbreaker mirror ended
        # turn 15 pre-cast twice running): cast from the command zone →
        # resolve → return to the COMMAND ZONE (never the graveyard) →
        # recast pays {2} tax per prior cast.
        engine, game, rick, claude, chandra, bolt = self._setup(
            make_game, make_card)
        life_before = rick.life
        r1 = asyncio.run(engine._execute_action(
            game, 1, {"type": "cast", "card": "Lightning Bolt"}))
        assert r1, "the signature spell must cast from the command zone"
        assert rick.life == life_before - 3, "Bolt resolves (3 to the face)"
        assert bolt in claude.command_zone, (
            "a resolved signature spell returns to the COMMAND ZONE")
        assert bolt not in claude.graveyard
        assert bolt.times_cast_from_command_zone == 1
        tapped_first = sum(1 for c in claude.battlefield if c.tapped)
        assert tapped_first == 1, "first cast: {R}, no tax"
        # Recast: {R} + {2} tax = 3 sources.
        for c in claude.battlefield:
            c.tapped = False
        r2 = asyncio.run(engine._execute_action(
            game, 1, {"type": "cast", "card": "Lightning Bolt"}))
        assert r2, "the recast must succeed with the tax payable"
        assert rick.life == life_before - 6
        assert bolt in claude.command_zone
        assert bolt.times_cast_from_command_zone == 2
        tapped_second = sum(1 for c in claude.battlefield if c.tapped)
        assert tapped_second == 3, (
            f"the recast pays {{R}} + {{2}} commander tax = 3 sources, "
            f"tapped {tapped_second}")

    def test_offer_gated_on_oathbreaker_presence(self, make_game, make_card):
        # The offer layer is the rule's enforcement point: the signature
        # spell is castable ONLY while the oathbreaker is on the
        # battlefield — with her gone it must not be offered at all.
        from mtg.legal_actions import castable_entries
        engine, game, rick, claude, chandra, bolt = self._setup(
            make_game, make_card)
        entries = castable_entries(game, claude,
                                   {"R": 3, "W": 0, "U": 0, "B": 0, "G": 0,
                                    "C": 0}, 0, 3)
        labels = [e.get("label", "") for e in entries]
        assert any("SIGNATURE_SPELL" in l for l in labels), (
            f"with the oathbreaker on the battlefield the signature spell "
            f"must be offered: {labels}")
        claude.battlefield.remove(chandra)
        entries2 = castable_entries(game, claude,
                                    {"R": 3, "W": 0, "U": 0, "B": 0, "G": 0,
                                     "C": 0}, 0, 3)
        labels2 = [e.get("label", "") for e in entries2]
        assert not any("Lightning Bolt" in l for l in labels2), (
            f"without the oathbreaker the signature spell must be omitted "
            f"entirely: {labels2}")
