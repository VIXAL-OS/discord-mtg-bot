"""July 28, 2026 — Phase 2: adventure exile (A1) and the companion fixture (D4).

A1  ADVENTURE CREATURE HALVES WERE STRANDED IN EXILE. The adventure resolution
    exiled the card but never marked it castable, and every exile-cast gate
    tests membership of `playable_from_exile` — so the creature half, which is
    the entire point of the mechanic and of data/test_adventure_chulane.json,
    could never be cast by anyone. The AI was never even offered it: the
    castable-list builder scans hand, command zone, companion zone, free-cast
    effects and the graveyard, but never exile. (The adventure half it does
    offer is a different thing — the instant/sorcery half of a card still in
    hand.)

    The flag is deliberately NOT `playable_from_exile`: end_turn clears that
    list every turn, while CR 715.3 keeps an adventured card castable for as
    long as it remains exiled.

    Fixing it surfaced a second gap in the same path — the AI executor removed
    the card from exile without putting it anywhere, so the July-20 zone-first
    cast gate rejected it as "Card not in hand". That would have made the fix
    look like it worked while nothing was cast.

D4  test_companion_lurrus.json ran 4x Street Wraith, a mana-value-5 PERMANENT
    card, under Lurrus of the Dream-Den, whose entire clause is "each permanent
    card in your starting deck has mana value 2 or less" — and FormatValidator
    had no companion check at all, so it reported the deck legal. A fixture
    that violates the mechanic it exists to test, with nothing able to say so.
"""
import json
from pathlib import Path

import pytest


_ROOT = Path(__file__).resolve().parent.parent
_CACHE = _ROOT / "data" / "card_data_cache.json"


def _cache():
    if not _CACHE.exists():
        pytest.skip("card_data_cache.json not present")
    with open(_CACHE, encoding="utf-8") as fh:
        return json.load(fh)


def _card_from_cache(name):
    from mtg.models import Card
    entry = _cache().get(name.lower(), {})
    return Card(name=name, id=name, type_line=entry.get("type_line", ""),
                cmc=entry.get("cmc", 0) or 0, mana_cost=entry.get("mana_cost", ""),
                oracle_text=entry.get("oracle_text", ""))


# ---------------------------------------------------------------------------
# A1 — adventure
# ---------------------------------------------------------------------------

def _bonecrusher(make_card):
    return make_card(
        "Bonecrusher Giant", type_line="Creature — Giant", mana_cost="{2}{R}",
        cmc=3, power="4", toughness="3",
        adventure_name="Stomp", adventure_cost="{1}{R}",
        adventure_text="Stomp deals 2 damage to any target.")


class TestAdventureCastableFromExile:

    def test_field_is_declared(self):
        from dataclasses import fields

        from mtg.models import Card
        assert "_adventure_exiled" in {f.name for f in fields(Card)}

    def test_creature_half_can_actually_be_cast(self, make_game, make_card):
        """End-to-end: the whole mechanic, which had never once completed."""
        import asyncio

        from mtg.engine import GameEngine
        engine = GameEngine(None)
        game = make_game()
        game._rules_engine = engine.rules
        engine.rules.engine_ref = engine
        rick = game.players[0]
        giant = _bonecrusher(make_card)
        giant._adventure_exiled = True
        rick.exile.append(giant)
        for i in range(6):
            rick.battlefield.append(
                make_card(f"Mountain{i}", type_line="Basic Land — Mountain"))
        asyncio.run(engine._execute_action(
            game, 0, {"type": "cast", "card": "Bonecrusher Giant"}))
        assert any(c.name == "Bonecrusher Giant" for c in rick.battlefield)
        assert not rick.exile
        assert giant._adventure_exiled is False, "flag must clear when it leaves exile"

    def test_an_ordinary_exiled_card_is_still_not_castable(self, make_game, make_card):
        """The gate must open for adventure cards only — not for everything
        sitting in exile."""
        import asyncio

        from mtg.engine import GameEngine
        engine = GameEngine(None)
        game = make_game()
        game._rules_engine = engine.rules
        engine.rules.engine_ref = engine
        rick = game.players[0]
        exiled = make_card("Grizzly Bears", type_line="Creature — Bear",
                           mana_cost="{1}{G}", cmc=2)
        rick.exile.append(exiled)
        for i in range(6):
            rick.battlefield.append(
                make_card(f"Forest{i}", type_line="Basic Land — Forest"))
        asyncio.run(engine._execute_action(
            game, 0, {"type": "cast", "card": "Grizzly Bears"}))
        assert exiled in rick.exile
        assert not any(c.name == "Grizzly Bears" for c in rick.battlefield)

    def test_the_adventure_resolution_marks_the_card(self):
        """Pin the producer: without this the gates below have nothing to see."""
        src = (_ROOT / "mtg" / "spells.py").read_text(encoding="utf-8")
        assert "_adventure_exiled = True" in src

    def test_castability_survives_end_of_turn(self, make_game, make_card):
        """Why the flag is not playable_from_exile: end_turn wipes that list
        every turn, but CR 715.3 castability persists while it stays exiled."""
        from mtg.engine import GameEngine
        engine = GameEngine(None)
        game = make_game()
        game._rules_engine = engine.rules
        engine.rules.engine_ref = engine
        rick = game.players[0]
        giant = _bonecrusher(make_card)
        giant._adventure_exiled = True
        rick.exile.append(giant)
        rick.playable_from_exile.append("some-impulse-card")
        engine.end_turn(game)
        assert giant._adventure_exiled is True
        assert not rick.playable_from_exile, "the impulse list still clears"

    def test_ai_is_offered_the_creature_half(self, make_game, make_card):
        """Castable-but-invisible is still unplayable: the AI proposes from this
        list, and it never scanned exile. (July 30: the computation moved to
        mtg/legal_actions.py — the single legal-actions provider.)"""
        from mtg.legal_actions import graveyard_castable_entries
        game = make_game()
        rick = game.players[0]
        giant = _bonecrusher(make_card)
        giant._adventure_exiled = True
        rick.exile.append(giant)
        for i in range(6):
            rick.battlefield.append(
                make_card(f"Mountain{i}", type_line="Basic Land — Mountain"))
        listed = [e["label"] for e in graveyard_castable_entries(
            rick, {"W": 0, "U": 0, "B": 0, "R": 6, "G": 0, "C": 0},
            any_color_mana=0, total_mana=6)]
        assert any("Bonecrusher Giant" in entry and "exile" in entry.lower()
                   for entry in listed), f"not offered: {listed}"


# ---------------------------------------------------------------------------
# D4 — companion
# ---------------------------------------------------------------------------

class TestCompanionValidation:

    def _lurrus_deck(self):
        deck = json.loads(
            (_ROOT / "data" / "test_companion_lurrus.json").read_text(encoding="utf-8"))
        return [_card_from_cache(c["name"])
                for c in deck["cards"] for _ in range(c.get("quantity", 1))]

    def test_the_fixture_is_now_legal_under_its_own_companion(self):
        from mtg.models import FormatValidator
        cards = self._lurrus_deck()
        assert len(cards) == 60
        ok, issues = FormatValidator.validate_deck(
            cards, "modern", companion=_card_from_cache("Lurrus of the Dream-Den"))
        assert ok, f"the companion fixture still violates its own mechanic: {issues}"

    def test_street_wraith_is_gone(self):
        raw = (_ROOT / "data" / "test_companion_lurrus.json").read_text(encoding="utf-8")
        assert "Street Wraith" not in raw, (
            "a mana-value-5 permanent cannot be in a Lurrus deck")

    def test_a_violating_deck_is_now_rejected(self):
        """The validator, not just the fixture — this reported LEGAL before."""
        from mtg.models import FormatValidator
        cards = self._lurrus_deck()[:56] + [
            _card_from_cache("Street Wraith") for _ in range(4)]
        ok, issues = FormatValidator.validate_deck(
            cards, "modern", companion=_card_from_cache("Lurrus of the Dream-Den"))
        assert not ok
        assert any("Street Wraith" in i and "companion restriction" in i
                   for i in issues)

    def test_nonpermanents_are_exempt_regardless_of_cost(self):
        """Lurrus restricts PERMANENT cards only — an expensive instant is fine."""
        from mtg.models import FormatValidator
        cards = self._lurrus_deck()[:59] + [_card_from_cache("Cruel Ultimatum")]
        ok, issues = FormatValidator.validate_deck(
            cards, "modern", companion=_card_from_cache("Lurrus of the Dream-Den"))
        companion_issues = [i for i in issues if "companion restriction" in i]
        assert not companion_issues, companion_issues

    def test_no_companion_means_no_companion_issues(self):
        from mtg.models import FormatValidator
        cards = self._lurrus_deck()[:56] + [
            _card_from_cache("Street Wraith") for _ in range(4)]
        ok, issues = FormatValidator.validate_deck(cards, "modern")
        assert not any("companion restriction" in i for i in issues)

    def test_an_unmodelled_restriction_is_reported_not_silently_passed(self, capsys):
        """A companion we cannot check must never look verified."""
        from mtg.models import Card, FormatValidator
        weird = Card(
            name="Obosh, the Preypiercer", id="obosh",
            type_line="Legendary Creature — Hellion Horror",
            oracle_text=("Companion — Your starting deck contains only cards with "
                         "odd mana values and lands.\nIf a source you control would "
                         "deal damage to a permanent or player, it deals double that "
                         "damage instead."))
        capsys.readouterr()
        FormatValidator.validate_deck(self._lurrus_deck(), "modern", companion=weird)
        assert "[COMPANION]" in capsys.readouterr().out
