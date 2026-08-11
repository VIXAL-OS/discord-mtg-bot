"""Aug 11, 2026 fix wave 2 — the three self-contained coverage findings.

All three are COVERAGE-class by the recut gate's taxonomy (they teach the
engine something it did not know), so none of them moves the gate; they were
done now because each is one file and one mechanism.

F1  Crew N had no special case in the ability parser, so it fell into the
    generic colon-split — and Crew's ONLY colon is inside its own reminder
    text. The split produced an unpayable cost and an effect string of
    "This Vehicle becomes an artifact creature until end of turn.)", which
    was then ANNOUNCED AS SUCCESS with nothing tapped and the Vehicle never
    animated (game_1536549632350625852, with zero creatures to crew with).

F3  The templated "When this Equipment enters, attach it to target creature
    you control" clause had no generic pattern — only two hardcoded
    exceptions. Maul of the Skyclaves declined at every tier and got attached
    only because a Hammer of Nazahn happened to be doing it for its own
    reasons.

F4  Knight of the White Orchid had no handler, so its ETB fell to Tier 3,
    which got the land-count comparison BACKWARDS and searched out a Plains
    while its controller already had MORE lands (CR 603.4).
"""
import asyncio

import pytest

from rules.effect_templates import get_effect_library


def _run(coro):
    return asyncio.run(coro)


def _engine(game):
    from mtg.engine import GameEngine
    e = GameEngine(None)
    game._rules_engine = e.rules
    e.rules.engine_ref = e
    return e


COPTER = ("Flying\n"
          "Whenever this Vehicle attacks or blocks, you may draw a card. "
          "If you do, discard a card.\n"
          "Crew 1 (Tap any number of creatures you control with total power "
          "1 or more: This Vehicle becomes an artifact creature until end of "
          "turn.)")


# --------------------------------------------------------------------------
# F1 — Crew
# --------------------------------------------------------------------------

class TestCrewIsNotColonSplit:
    """DECISIVE SHAPE: both pins drive the REAL executor. The defect is that
    the parser handed an unpayable pseudo-cost to the announce-only fallback,
    which reported success — so only an end-to-end run can tell "crewed" from
    "claimed to crew"."""

    def _game(self, make_game, make_card, with_creature=True):
        game = make_game()
        rick, _ = game.players
        game.active_player_index = 0
        copter = make_card("Smuggler's Copter",
                           type_line="Artifact — Vehicle",
                           oracle_text=COPTER, power="3", toughness="3")
        rick.battlefield.append(copter)
        if with_creature:
            crewer = make_card("Llanowar Elves", type_line="Creature — Elf",
                               power="1", toughness="1")
            rick.battlefield.append(crewer)
        return game, rick, copter, _engine(game)

    def test_crewing_taps_a_creature_and_animates_the_vehicle(
            self, make_game, make_card):
        game, rick, copter, engine = self._game(make_game, make_card)
        result = _run(engine._execute_action(game, 0, {
            "type": "activate", "permanent": "Smuggler's Copter",
            "ability": 0}))
        crewer = next(c for c in rick.battlefield if c.name == "Llanowar Elves")
        assert crewer.tapped, (
            "crewing taps creatures with total power >= N (CR 702.121b); "
            f"nothing was tapped. result={result!r}")
        assert copter.is_creature(game), "the Vehicle animates"

    def test_crewing_with_no_creatures_fails_instead_of_announcing_success(
            self, make_game, make_card):
        """The live bug: zero creatures on the battlefield, and the engine
        still reported the ability as resolved."""
        game, rick, copter, engine = self._game(make_game, make_card,
                                                with_creature=False)
        result = _run(engine._execute_action(game, 0, {
            "type": "activate", "permanent": "Smuggler's Copter",
            "ability": 0}))
        assert not result, (
            "an unpayable crew cost must fail, not announce success — "
            f"got {result!r}")
        assert not copter.is_creature(game), "and the Vehicle must not animate"

    def test_the_garbled_reminder_text_never_reaches_the_player(
            self, make_game, make_card):
        """The colon-split produced an effect string ending in a stray ')'.
        Whatever the outcome, that must not be what gets reported."""
        game, rick, copter, engine = self._game(make_game, make_card)
        result = _run(engine._execute_action(game, 0, {
            "type": "activate", "permanent": "Smuggler's Copter",
            "ability": 0}))
        assert "until end of turn.)" not in (result or ""), (
            "the reminder-text fragment leaked into the player-facing message")


# --------------------------------------------------------------------------
# F3 — generic Equipment ETB-attach
# --------------------------------------------------------------------------

ATTACH = "When this Equipment enters, attach it to target creature you control."


class TestEquipmentEtbAttachPattern:
    def test_an_untemplated_equipment_now_attaches_itself(self):
        acts, _ = get_effect_library().resolve_etb(
            "Maul of the Skyclaves", ATTACH, "Rick", "Claude",
            game_context={"best_own_creature": "Sram",
                          "card_name": "Maul of the Skyclaves"})
        assert acts, "the clause had no generic pattern at all"
        equip = [a for a in acts if a.get("action") == "equip"]
        assert equip and equip[0]["equipment"] == "Maul of the Skyclaves", (
            f"must attach ITSELF, not some other equipment: {acts}")
        assert equip[0]["creature"] == "Sram"

    def test_a_named_template_still_wins(self):
        """CONTROL — Embercleave has its own registration and must keep it;
        a pattern that shadowed name keys would be a regression."""
        acts, desc = get_effect_library().resolve_etb(
            "Embercleave",
            "When Embercleave enters, attach it to target creature you control.",
            "Rick", "Claude",
            game_context={"best_own_creature": "Sram",
                          "card_name": "Embercleave"})
        assert acts and acts[0]["equipment"] == "Embercleave"

    def test_no_legal_creature_is_a_handled_no_op(self):
        """CR 603.3c. [] means handled; None would escalate to Tier 3, which
        is what invents effects."""
        acts, _ = get_effect_library().resolve_etb(
            "Maul of the Skyclaves", ATTACH, "Rick", "Claude",
            game_context={"best_own_creature": "",
                          "card_name": "Maul of the Skyclaves"})
        assert acts == [] or not acts, f"expected a handled no-op, got {acts}"


# --------------------------------------------------------------------------
# F4 — Knight of the White Orchid's intervening-if
# --------------------------------------------------------------------------

KNIGHT = ("When this creature enters, if an opponent controls more lands than "
          "you, you may search your library for a Plains card, put it onto "
          "the battlefield, then shuffle.")


def _lands(n):
    return [{"name": f"L{i}", "type_line": "Basic Land — Plains"}
            for i in range(n)]


class TestKnightOfTheWhiteOrchidCondition:
    def _resolve(self, mine, theirs):
        return get_effect_library().resolve_etb(
            "Knight of the White Orchid", KNIGHT, "Rick", "Claude",
            game_context={"controller_battlefield": _lands(mine),
                          "opponent_battlefield": _lands(theirs)})[0]

    def test_it_searches_when_the_opponent_has_more_lands(self):
        acts = self._resolve(mine=7, theirs=8)
        assert acts and acts[0]["action"] == "search_library"
        assert acts[0]["card_type"] == "Plains"

    def test_it_does_not_search_when_the_controller_has_more(self):
        """The live failure: 8 lands to the opponent's 7, and Tier 3 searched
        anyway."""
        assert not self._resolve(mine=8, theirs=7)

    def test_equal_land_counts_do_not_trigger(self):
        """CR 603.4 — "MORE lands than you" is strictly more. An off-by-one
        here is the difference between a free land and a correct no-op."""
        assert not self._resolve(mine=7, theirs=7)


# --------------------------------------------------------------------------
# Oracle drift — the WotC retemplating that broke a snippet key
# --------------------------------------------------------------------------

class TestPwSnippetKeysSurviveOracleDrift:
    """Aug 11, 2026: WotC's morning Oracle update alphabetized Vivien,
    Champion of the Wilds' +1 from "gains vigilance and reach" to "gains reach
    and vigilance". `_pw_ability_templates` matches its key as a SUBSTRING of
    the ability text, so the key "vigilance and reach" stopped matching and
    her +1 silently fell through to Tier 3. The card-names CI caught it within
    hours of the push.

    This pin reads the CARD CACHE rather than hardcoding the text, so if WotC
    reorders again the test fails here instead of only in CI — and it fails
    for the right reason, naming the key that went stale.
    """

    def _vivien_plus_one(self):
        import json
        from pathlib import Path
        cache = json.loads(
            (Path(__file__).resolve().parent.parent
             / "data" / "card_data_cache.json").read_text(encoding="utf-8"))
        entry = cache.get("vivien, champion of the wilds")
        if not entry:
            pytest.skip("Vivien, Champion of the Wilds not in the card cache")
        for line in (entry.get("oracle_text") or "").split("\n"):
            if line.strip().startswith("+1:"):
                return line.strip()
        pytest.fail("no +1 ability found in the cached oracle text")

    def test_her_plus_one_still_resolves_via_the_template(self):
        ability = self._vivien_plus_one()
        acts, desc = get_effect_library().resolve_pw_ability(
            "Vivien, Champion of the Wilds", ability, "Rick", "Claude",
            game_context={"best_own_creature": "Sram"})
        assert acts, (
            "her +1 resolved to nothing — the snippet key no longer matches "
            f"the printed text. Ability text now reads: {ability!r}")
        assert any(a.get("action") == "grant_keywords" for a in acts), (
            f"expected the keyword grant, got {acts}")

    def test_no_pw_snippet_key_is_a_keyword_LIST(self):
        """The class, not just the instance. WotC alphabetizes keyword lists
        without warning, so a key containing two or more keywords joined by
        "and"/"," is a latent break. Garruk Wildspeaker's
        "get +3/+3 and gain trample" is fine — that "and" joins a pump to a
        keyword, not two keywords."""
        import re
        KW = ("flying", "first strike", "double strike", "deathtouch", "haste",
              "hexproof", "indestructible", "lifelink", "menace", "reach",
              "trample", "vigilance", "ward", "defender", "flash")
        bad = []
        for (pw, snip) in get_effect_library()._pw_ability_templates:
            hits = [k for k in KW if re.search(rf"\b{re.escape(k)}\b", snip)]
            if len(hits) >= 2:
                bad.append((pw, snip, hits))
        assert not bad, (
            "these PW snippet keys embed a keyword LIST and will break the "
            f"next time WotC alphabetizes it: {bad}")
