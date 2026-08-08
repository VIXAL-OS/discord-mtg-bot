"""Queue item R4 (Aug 8, 2026): Sarkhan-class restricted mana — resolution.

THE FINDING WAS A DOUBLE FALSE POSITIVE (see CLAUDE.md's R4 section): the
core machinery shipped in the Aug-5 checkpoint (add_restricted_mana +
_restricted_mana_allows + Phase-0/Phase-4 wiring, pinned in
test_aug4_games_153_160_fixes.py), and the live "leak" was Ancient Tomb's
double production read as a fourth phantom mana. What the R4 sweep DID
find: two OTHER inventory producers bypassed the machinery —

- Jaya Ballard's "+1: Add {R}{R}{R}. Spend this mana only to cast instant
  or sorcery spells." parses via the PW EXPLICIT-symbol branch, which
  added 3 UNRESTRICTED R (only the word-form branch checked the —
  hardcoded, dragon-only — phrase).
- Castle Garenbrig's "{2}{G}{G}, {T}: Add six {G}. Spend this mana only to
  cast creature spells or activate abilities of creatures." went through
  the [ACTIVATE-MANA] paths, which (a) read "Add six {G}" as ONE {G} and
  (b) added it unrestricted.

Both closed via helpers.parse_mana_spend_restriction (period-anchored
families, swept against all 180 bulk carriers of the phrase — qualified
variants like Unclaimed Territory's "of the chosen type" deliberately fall
to an 'unmodeled:' key whose mana is HELD, never silently unrestricted).
"""

import asyncio
import io
import json
from pathlib import Path

import pytest

from mtg.models import Card, Player, GameState

_ROOT = Path(__file__).resolve().parent.parent
_CACHE = json.loads(
    (_ROOT / "data" / "card_data_cache.json").read_text(encoding="utf-8"))


def _cached_card(make_card, name: str, **over):
    entry = _CACHE[name.lower()]
    defaults = dict(
        type_line=entry.get("type_line", ""),
        oracle_text=entry.get("oracle_text", "") or "",
        power=entry.get("power") or None,
        toughness=entry.get("toughness") or None,
    )
    defaults.update(over)
    return make_card(entry.get("name", name), **defaults)


# ---------------------------------------------------------------------------
# The parse helper: swept families, anchored
# ---------------------------------------------------------------------------

class TestParseManaSpendRestriction:
    def test_the_three_inventory_clauses(self):
        from mtg.helpers import parse_mana_spend_restriction as parse
        assert parse(_CACHE["sarkhan, fireblood"]["oracle_text"]) == "dragon_spell"
        assert parse(_CACHE["jaya ballard"]["oracle_text"]) == "instant_sorcery_spell"
        assert parse(_CACHE["castle garenbrig"]["oracle_text"]) == "creature_spell"

    def test_plain_mana_is_none(self):
        from mtg.helpers import parse_mana_spend_restriction as parse
        assert parse("{T}: Add {G}.") is None
        assert parse("") is None
        assert parse(None) is None

    def test_qualified_variant_falls_to_unmodeled_key(self):
        # Unclaimed Territory: "creature spells OF THE CHOSEN TYPE" — an
        # unanchored family would classify it plain creature_spell and
        # OVER-permit. The unmodeled key means the mana is HELD.
        from mtg.helpers import parse_mana_spend_restriction as parse
        key = parse("{T}: Add one mana of the chosen color. Spend this mana "
                    "only to cast creature spells of the chosen type.")
        assert key is not None and key.startswith("unmodeled:")


# ---------------------------------------------------------------------------
# Jaya Ballard: the PW explicit-symbol producer routes to the restricted pool
# ---------------------------------------------------------------------------

class TestJayaBallardProducer:
    def _activate_plus1(self, game, player, make_card):
        from rules.planeswalker import (AbilityType, PlaneswalkerAbility,
                                        PlaneswalkerManager)
        jaya = _cached_card(make_card, "jaya ballard")
        player.battlefield.append(jaya)
        ability = PlaneswalkerAbility(
            index=0, loyalty_cost=1, ability_type=AbilityType.LOYALTY_PLUS,
            text="Add {R}{R}{R}. Spend this mana only to cast instant or "
                 "sorcery spells.")
        return asyncio.run(PlaneswalkerManager()._execute_ability(
            game, player, jaya, ability, []))

    def test_plus1_lands_in_the_restricted_pool(self, make_game, make_card):
        game = make_game()
        rick = game.players[0]
        msgs = self._activate_plus1(game, rick, make_card)
        assert sum(rick.mana_pool.values()) == 0, \
            "Jaya's mana must NOT enter the unrestricted pool"
        assert sum(b["amount"] for b in rick.restricted_mana_pool) == 3
        assert all(b["restriction"] == "instant_sorcery_spell"
                   for b in rick.restricted_mana_pool)
        assert any("restricted" in m for m in msgs)

    def test_instant_can_pay_with_it_creature_cannot(self, make_game,
                                                     make_card):
        game = make_game()
        rick = game.players[0]
        self._activate_plus1(game, rick, make_card)
        bolt = make_card("Lightning Bolt", type_line="Instant",
                         mana_cost="{R}", oracle_text="")
        bear = make_card("Runeclaw Bear", type_line="Creature — Bear",
                         mana_cost="{1}{G}", oracle_text="")
        assert rick.can_pay_mana_cost("{R}", spending_card=bolt)[0]
        assert not rick.can_pay_mana_cost("{R}", spending_card=bear)[0]
        # Payment: the instant actually consumes it (zero sources needed).
        assert rick.tap_sources_for_cost("{R}", game=game, spending_card=bolt)
        assert sum(b["amount"] for b in rick.restricted_mana_pool) == 2


# ---------------------------------------------------------------------------
# Castle Garenbrig: both activation producers (engine + cog manual twin)
# ---------------------------------------------------------------------------

class TestCastleGarenbrigProducers:
    def test_cog_manual_activate_six_restricted_green(self, make_game,
                                                      make_card):
        # The Q4 harness shape: drive the REAL cog _activate_permanent.
        from types import SimpleNamespace
        from mtg.cog import MTGGameCog
        from mtg.engine import GameEngine

        game = make_game()
        rick = game.players[0]
        engine = GameEngine(None)
        game._rules_engine = engine.rules
        engine.rules.engine_ref = engine
        engine.save_game = lambda _g: None
        castle = _cached_card(make_card, "castle garenbrig")
        rick.battlefield.append(castle)
        for i in range(4):
            rick.battlefield.append(make_card(
                f"Forest{i}", type_line="Basic Land — Forest",
                oracle_text="{T}: Add {G}.", power=None, toughness=None))
        cog = object.__new__(MTGGameCog)
        cog.engine = engine
        sent = []

        async def _send(content):
            sent.append(content)

        ctx = SimpleNamespace(send=_send)
        asyncio.run(MTGGameCog._activate_permanent(
            cog, ctx, game, rick, 0, castle, "2", None))
        restricted_g = sum(b["amount"] for b in rick.restricted_mana_pool
                           if b["color"] == "G")
        assert restricted_g == 6, (rick.restricted_mana_pool, sent)
        assert all(b["restriction"] == "creature_spell"
                   for b in rick.restricted_mana_pool)

    def test_engine_activate_six_restricted_green(self, make_game, make_card):
        # The AI executor twin: engine._execute_action activate, ability 1
        # (the {2}{G}{G}, {T} big ability — index 0 is the plain {T}: Add
        # {G}, which must STAY unrestricted). The cost is genuinely paid
        # (4 Forests tapped) and the word-number "Add six {G}" — which the
        # old branch read as ONE unrestricted {G} — lands as six
        # creature_spell-restricted units.
        from mtg.engine import GameEngine
        game = make_game()
        rick = game.players[0]
        engine = GameEngine(None)
        game._rules_engine = engine.rules
        engine.rules.engine_ref = engine
        engine.save_game = lambda _g: None
        castle = _cached_card(make_card, "castle garenbrig")
        rick.battlefield.append(castle)
        for i in range(4):
            rick.battlefield.append(make_card(
                f"Forest{i}", type_line="Basic Land — Forest",
                oracle_text="{T}: Add {G}.", power=None, toughness=None))
        result = asyncio.run(engine._execute_action(
            game, 0, {"type": "activate", "permanent": "Castle Garenbrig",
                      "ability": 1}))
        assert result is not None and "restricted" in result
        restricted_g = sum(b["amount"] for b in rick.restricted_mana_pool
                           if b["color"] == "G")
        assert restricted_g == 6, rick.restricted_mana_pool
        assert rick.mana_pool.get("G", 0) == 0, \
            "the six {G} must not enter the unrestricted pool"

    def test_creature_pays_with_it_noncreature_held(self, make_game,
                                                    make_card, capsys):
        game = make_game()
        rick = game.players[0]
        rick.add_restricted_mana("G", 6, "creature_spell",
                                 source="Castle Garenbrig")
        hoof = make_card("Craterhoof Behemoth", type_line="Creature — Beast",
                         mana_cost="{5}{G}{G}{G}", oracle_text="")
        # 6 restricted G + 2 real sources pays {5}{G}{G}{G}
        for i in range(2):
            rick.battlefield.append(make_card(
                f"Forest{i}", type_line="Basic Land — Forest",
                oracle_text="{T}: Add {G}.", power=None, toughness=None))
        assert rick.tap_sources_for_cost("{5}{G}{G}{G}", game=game,
                                         spending_card=hoof)
        assert rick.restricted_mana_pool == []
        # And the held case, with the audit line:
        rick.add_restricted_mana("G", 6, "creature_spell",
                                 source="Castle Garenbrig")
        overrun = make_card("Overrun", type_line="Sorcery",
                            mana_cost="{2}{G}{G}{G}", oracle_text="")
        assert not rick.tap_sources_for_cost("{2}{G}{G}{G}", game=game,
                                             spending_card=overrun)
        out = capsys.readouterr().out
        assert "[RESTRICTED-MANA]" in out
        assert "creature_spell only" in out
        assert "Castle Garenbrig" in out
        assert sum(b["amount"] for b in rick.restricted_mana_pool) == 6


# ---------------------------------------------------------------------------
# Adversarial-review fixes (the FIX-FIRST slate)
# ---------------------------------------------------------------------------

class TestAdversarialReviewFixes:
    def test_engine_ability_zero_stays_unrestricted(self, make_game,
                                                    make_card):
        # The mutation-confirmed pin gap: producers must parse the
        # per-ABILITY text, not the whole oracle — a whole-oracle mutant
        # passed all 41 prior tests while making Garenbrig's innocent
        # {T}: Add {G} creature-restricted.
        from mtg.engine import GameEngine
        game = make_game()
        rick = game.players[0]
        engine = GameEngine(None)
        game._rules_engine = engine.rules
        engine.rules.engine_ref = engine
        engine.save_game = lambda _g: None
        castle = _cached_card(make_card, "castle garenbrig")
        rick.battlefield.append(castle)
        asyncio.run(engine._execute_action(
            game, 0, {"type": "activate", "permanent": "Castle Garenbrig",
                      "ability": 0}))
        assert rick.mana_pool.get("G", 0) == 1, \
            "the plain {T}: Add {G} is UNRESTRICTED"
        assert rick.restricted_mana_pool == []

    def test_cog_count_never_reaches_a_conditional_later_clause(
            self, make_game, make_card):
        # Undermountain Adventurer: "Add {G}{G}. If you've completed a
        # dungeon, add six {G} instead." — the unanchored count regex
        # fabricated SIX unconditional mana. Anchored to the matched span,
        # the base clause adds its (pre-existing single-symbol) 1 G.
        from types import SimpleNamespace
        from mtg.cog import MTGGameCog
        from mtg.engine import GameEngine
        game = make_game()
        rick = game.players[0]
        engine = GameEngine(None)
        game._rules_engine = engine.rules
        engine.rules.engine_ref = engine
        engine.save_game = lambda _g: None
        adv = make_card(
            "Undermountain Adventurer",
            type_line="Creature — Dwarf Knight",
            oracle_text="{T}: Add {G}{G}. If you've completed a dungeon, "
                        "add six {G} instead.",
            power="2", toughness="4")
        rick.battlefield.append(adv)
        cog = object.__new__(MTGGameCog)
        cog.engine = engine
        sent = []

        async def _send(content):
            sent.append(content)

        ctx = SimpleNamespace(send=_send)
        # The cog's ability numbering is 1-BASED — "0" is rejected outright,
        # which made this pin's first draft VACUOUS (total 0 passed the
        # upper bound under both the fix and the mutant; the R4b sweep's
        # surviving mutant caught it — the pin-decisiveness discipline).
        asyncio.run(MTGGameCog._activate_permanent(
            cog, ctx, game, rick, 0, adv, "1", None))
        total = rick.mana_pool.get("G", 0) + sum(
            b["amount"] for b in rick.restricted_mana_pool)
        assert total >= 1, (sent, "the base add-clause must actually fire")
        assert total <= 2, (rick.mana_pool, rick.restricted_mana_pool, sent)

    def test_cog_x_add_does_not_hit_the_mana_branch(self, make_game,
                                                    make_card):
        # "add X {R}" (7 bulk cards) must NOT match the broadened trigger —
        # the branch would mark the effect resolved and suppress the tier
        # cascade that actually handles X-adds.
        import re as _re
        pat = _re.compile(
            r'add (?:(?:two|three|four|five|six|seven|eight) )?\{([WUBRGC])\}',
            _re.IGNORECASE)
        assert not pat.search("add x {r} for each charge counter")
        assert pat.search("add {g}{g}")
        assert pat.search("add six {g}")

    def test_response_filters_thread_spending_card(self):
        # The live regression: five response-affordability sites called
        # can_pay_mana_cost with NO spending_card, so Jaya's instants-only
        # restricted mana was invisible to the instant filter (the FoW
        # dead-card class). Structural: no bare-cost call may remain at
        # those filter shapes.
        for path in ("mtg/engine.py", "mtg/claude_player.py",
                     "mtg/spells.py"):
            src = io.open(_ROOT / path, encoding="utf-8").read()
            assert "can_pay_mana_cost(c.mana_cost)[0]" not in src, path
            assert "can_pay_mana_cost(hand_card.mana_cost)\n" not in src, path

    def test_adventure_half_cannot_spend_creature_restricted_mana(
            self, make_card):
        # CR 715.2a: an adventure cast is an instant/sorcery cast, but the
        # Card's type_line is the CREATURE face — Stomp read as a creature
        # spell and Garenbrig's restricted G would have paid for it.
        rick = Player(name="Rick")
        rick.add_restricted_mana("G", 6, "creature_spell",
                                 source="Castle Garenbrig")
        bonecrusher = make_card(
            "Bonecrusher Giant", type_line="Creature — Giant",
            mana_cost="{2}{R}", oracle_text="")
        bonecrusher.cast_as_adventure = True
        assert not rick._restricted_mana_allows(
            rick.restricted_mana_pool[0], bonecrusher)
        bonecrusher.cast_as_adventure = False
        assert rick._restricted_mana_allows(
            rick.restricted_mana_pool[0], bonecrusher)

    def test_chandra_heart_of_fire_adds_six_via_the_pw_parser(
            self, make_game, make_card):
        # The third parser: the word-count fix landed in engine + cog but
        # not rules/planeswalker.py — Chandra, Heart of Fire (live in
        # three decks) still added ONE {R} for "Add six {R}".
        from rules.planeswalker import (AbilityType, PlaneswalkerAbility,
                                        PlaneswalkerManager)
        game = make_game()
        rick = game.players[0]
        chandra = _cached_card(make_card, "chandra, heart of fire")
        rick.battlefield.append(chandra)
        ability = PlaneswalkerAbility(
            index=2, loyalty_cost=-9, ability_type=AbilityType.LOYALTY_MINUS,
            text="Search your graveyard and library for any number of red "
                 "instant and/or sorcery cards, exile them, then shuffle. "
                 "You may cast them this turn. Add six {R}.")
        asyncio.run(PlaneswalkerManager()._execute_ability(
            game, rick, chandra, ability, []))
        assert rick.mana_pool.get("R", 0) == 6


# ---------------------------------------------------------------------------
# Planeswalkers are never auto-tap mana sources (found by this file's
# first run: the Jaya fixture's can_pay check passed for a CREATURE because
# Jaya HERSELF was being counted as an untapped {R} source — her loyalty
# text "Add {R}{R}{R}" matched _can_produce_mana's oracle scan, and the tap
# engine provably tapped her for phantom mana, CR 601.2g)
# ---------------------------------------------------------------------------

class TestPlaneswalkersAreNotManaSources:
    def test_jaya_is_not_an_untapped_source_and_cannot_be_tapped(
            self, make_game, make_card):
        game = make_game()
        rick = game.players[0]
        jaya = _cached_card(make_card, "jaya ballard")
        rick.battlefield.append(jaya)
        assert jaya not in rick.untapped_mana_sources()
        bear = make_card("Runeclaw Bear", type_line="Creature — Bear",
                         mana_cost="{R}", oracle_text="")
        assert not rick.tap_sources_for_cost("{R}", game=game,
                                             spending_card=bear)
        assert jaya.tapped is False, \
            "the payment engine must never tap a planeswalker"

    def test_all_four_live_phantom_sources_are_excluded(self, make_card):
        rick = Player(name="Rick")
        for name in ("jaya ballard", "chandra, torch of defiance",
                     "chandra, flameshaper", "domri, anarch of bolas"):
            pw = _cached_card(make_card, name)
            assert not rick._can_produce_mana(pw), name


# ---------------------------------------------------------------------------
# The unmodeled-clause family holds its mana for everything
# ---------------------------------------------------------------------------

class TestUnmodeledRestrictionHolds:
    def test_unmodeled_key_mana_is_never_spendable(self, make_game,
                                                   make_card):
        from mtg.helpers import parse_mana_spend_restriction as parse
        game = make_game()
        rick = game.players[0]
        key = parse("Add {C}{C}. Spend this mana only on costs that "
                    "contain {X}.")
        assert key.startswith("unmodeled:")
        rick.add_restricted_mana("C", 2, key, source="Shrine")
        anything = make_card("Sol Ring", type_line="Artifact",
                             mana_cost="{1}", oracle_text="")
        assert not rick.can_pay_mana_cost("{2}", spending_card=anything)[0]
        assert not rick.tap_sources_for_cost("{2}", game=game,
                                             spending_card=anything)
        assert sum(b["amount"] for b in rick.restricted_mana_pool) == 2


# ---------------------------------------------------------------------------
# Q4 coexistence: restricted spends never masquerade as snow
# ---------------------------------------------------------------------------

class TestRestrictedAndSnowCoexist:
    def test_snow_counts_only_the_snow_portion(self, make_game, make_card):
        # A dragon cast paying {R}{G} from one restricted R (Sarkhan) and
        # one snow-tagged pool G: snow_spent must be exactly 1 (the G) —
        # restricted-pool spends carry no snow tags (the documented
        # undercount-safe direction in the Phase-4 settle).
        #
        # Mutation note (R4 sweep): the `debit_pool_snow(_k, _ordinary)` →
        # `debit_pool_snow(_k, _v)` mutant SURVIVES this pin and is a
        # DOCUMENTED EQUIVALENT, not a gap — debit clamps its take at the
        # tag count, and the credit clamp guarantees tags ≤ the pool's own
        # count ≥ the ordinary portion whenever restricted units are in
        # play, so the over-debit can never change the result. The clamp
        # invariant that makes it equivalent is itself mutation-pinned by
        # the Q4 sweep (the July-26 defense-in-depth precedent).
        game = make_game()
        rick = game.players[0]
        rick.add_restricted_mana("R", 1, "dragon_spell", source="Sarkhan")
        rick.mana_pool["G"] += 1
        rick.credit_pool_snow("G", 1)
        dragon = make_card("Lathliss, Dragon Queen",
                           type_line="Legendary Creature — Dragon",
                           mana_cost="{4}{R}{R}", oracle_text="")
        assert rick.tap_sources_for_cost("{R}{G}", game=game,
                                         spending_card=dragon)
        assert rick._last_payment["snow_spent"] == 1
        assert rick.restricted_mana_pool == []
        assert rick._pool_snow.get("G", 0) == 0
