"""Mutation-sensitive regressions from the Aug 8, 2026 second-confirmation
batch audit (sha=f6187ab corpus, 160 games).

Finding numbers (#1..#12) reference the Aug 8 findings ledger in CLAUDE.md.
Fixtures are LIVE-SHAPED (the pin-shape-reachability ledger): actions are
built the way the executors/templates actually emit them, oracle text comes
from data/card_data_cache.json, and state — not echo strings — carries the
assertions.
"""

import asyncio
import io
import json
import re
from pathlib import Path

import pytest

from mtg.constants import Phase, Zone
from mtg.models import Card, GameState, Player

_ROOT = Path(__file__).resolve().parent.parent
_CACHE = json.loads(
    (_ROOT / "data" / "card_data_cache.json").read_text(encoding="utf-8"))


def _cached_card(make_card, name: str, **over):
    entry = _CACHE[name.lower()]
    defaults = dict(
        type_line=entry.get("type_line", ""),
        oracle_text=entry.get("oracle_text", "") or "",
        power=entry.get("power") or "0",
        toughness=entry.get("toughness") or "0",
    )
    defaults.update(over)
    return make_card(entry.get("name", name), **defaults)


# ---------------------------------------------------------------------------
# #1: the phantom {"type":"attack"} plan action never mutates
# ---------------------------------------------------------------------------

class TestPhantomAttackActionNeverMutates:
    def test_attack_action_rejects_without_touching_combat_state(
            self, make_game, make_card):
        # Live shape: game_1535473341308215377 — a MAIN1 plan emitted
        # {"type": "attack", "creatures": ["Frog Lizard"]}; the old handler
        # tapped the creature and set .attacking outside combat (the fifth
        # stale-flag leak source; the tapped creature was illegally
        # unavailable to block the next turn, CR 508).
        from mtg.engine import GameEngine
        game = make_game()
        engine = GameEngine(None)
        game._rules_engine = engine.rules
        engine.rules.engine_ref = engine
        game.phase = Phase.MAIN1
        frog = make_card("Frog Lizard", type_line="Creature — Frog Lizard",
                         power="3", toughness="3")
        game.players[1].battlefield.append(frog)
        result = asyncio.run(engine._execute_action(
            game, 1, {"type": "attack", "creatures": ["Frog Lizard"]}))
        # Assert on the CARD and GAME state, not just the return — a mutant
        # that keeps the mutation while adding the stash must fail here.
        assert result is None
        assert frog.tapped is False
        assert frog.attacking is False
        assert frog.attacks_this_turn == 0
        assert game.attackers == []
        assert getattr(game, "_last_attack_action_failure", None) is not None
        assert "not a plan action" in game._last_attack_action_failure[1]


# ---------------------------------------------------------------------------
# #2: Ascend / the city's blessing (CR 702.131)
# ---------------------------------------------------------------------------

class TestCityBlessing:
    def _swordtooth(self, make_card):
        return _cached_card(make_card, "wayward swordtooth",
                            summoning_sick=False)

    def test_combat_restricted_below_ten_permanents(self, make_game, make_card):
        game = make_game()
        rick = game.players[0]
        tooth = self._swordtooth(make_card)
        rick.battlefield.append(tooth)
        # Six permanents total — the live shape (it blocked and killed Jorn
        # at six, game_1535486721779568700).
        for i in range(5):
            rick.battlefield.append(make_card(f"Forest{i}",
                                              type_line="Basic Land — Forest"))
        assert tooth.can_attack(game=game) is False
        assert tooth.can_block(game=game) is False

    def test_combat_allowed_at_ten_and_sticky_after_dropping(
            self, make_game, make_card):
        game = make_game()
        rick = game.players[0]
        tooth = self._swordtooth(make_card)
        rick.battlefield.append(tooth)
        lands = [make_card(f"Forest{i}", type_line="Basic Land — Forest")
                 for i in range(9)]
        rick.battlefield.extend(lands)  # ten permanents total
        assert tooth.can_attack(game=game) is True
        assert rick.city_blessing is True
        # CR 702.131c: the blessing is for the REST OF THE GAME — dropping
        # below ten must not revoke it.
        for land in lands[:6]:
            rick.battlefield.remove(land)
        assert len(rick.battlefield) < 10
        assert tooth.can_block(game=game) is True

    def test_blessing_serializes_through_player_round_trip(self):
        # The verifier's trap: Player.to_dict/from_dict are HAND-WRITTEN —
        # a declared field does not serialize automatically, and dropping
        # the blessing on save/load or !undo strips game-lifetime state.
        p = Player(name="Rick", life=40)
        p.city_blessing = True
        restored = Player.from_dict(p.to_dict())
        assert restored.city_blessing is True

    def test_tendershoot_anthem_condition_routes_through_blessing(
            self, make_game, make_card):
        # Before #2, Tendershoot's "as long as you have the city's blessing"
        # matched the generic "control N or more <type>" regex, which counts
        # permanents whose TYPE LINE contains "permanent" — never true — so
        # the anthem was permanently OFF.
        game = make_game()
        rick = game.players[0]
        dryad = _cached_card(make_card, "tendershoot dryad")
        rick.battlefield.append(dryad)
        oracle_lower = (dryad.oracle_text or "").lower()
        assert game._static_condition_met(dryad, oracle_lower) is False
        saps = [make_card(f"Saproling{i}", type_line="Creature — Saproling")
                for i in range(9)]
        rick.battlefield.extend(saps)
        assert game._static_condition_met(dryad, oracle_lower) is True
        # DECISIVE for the blessing-vs-live-count distinction: the generic
        # (b) branch would flip back OFF when the count drops; the blessing
        # is sticky (CR 702.131c). A mutant routing this condition through
        # the generic branch must fail HERE, not on the counts above.
        for s in saps[:6]:
            rick.battlefield.remove(s)
        assert len(rick.battlefield) < 10
        assert game._static_condition_met(dryad, oracle_lower) is True

    def test_generic_permanent_kind_counts_whole_battlefield(
            self, make_game, make_card):
        # The (b)-branch fix: "control N or more permanents" counts every
        # battlefield object, not type lines containing "permanent".
        game = make_game()
        rick = game.players[0]
        src = make_card("Synthetic Threshold",
                        type_line="Enchantment",
                        oracle_text="As long as you control three or more "
                                    "permanents, creatures you control get +1/+1.")
        rick.battlefield.append(src)
        assert game._static_condition_met(
            src, src.oracle_text.lower()) is False
        rick.battlefield.append(make_card("A", type_line="Artifact"))
        rick.battlefield.append(make_card("B", type_line="Basic Land — Swamp"))
        assert game._static_condition_met(
            src, src.oracle_text.lower()) is True


# ---------------------------------------------------------------------------
# #3: search_library — merged keys, honored filters, typed-choice rejection
# ---------------------------------------------------------------------------

class TestSearchLibraryMerge:
    def _run(self, rules, game, action):
        return rules._execute_action_on_state(game, action)

    def test_filter_type_enforced_and_wrong_typed_request_rejected(
            self, make_game, make_card, rules, capsys):
        # The live defect: Jarad's Orders (filter_type=creature) honored the
        # model's tutor_to_hand="Swamp" (game_1535473318738788444).
        game = make_game()
        rick = game.players[0]
        creature = make_card("Sheoldred", type_line="Creature — Phyrexian",
                             cmc=4)
        swamp = make_card("Swamp", type_line="Basic Land — Swamp", cmc=0)
        rick.library.extend([swamp, creature])
        msg = self._run(rules, game, {
            "action": "search_library", "player": "Rick",
            "filter_type": "creature", "to_zone": "hand", "count": 1,
            "card_name": "Swamp",
        })
        found_names = [c.name for c in rick.hand]
        assert "Swamp" not in found_names
        assert "Sheoldred" in found_names, msg
        out = capsys.readouterr().out
        assert "rejected requested 'Swamp'" in out

    def test_max_cmc_honored_for_battlefield_placement(
            self, make_game, make_card, rules):
        # Vivien -2 (filter_type creature, max_cmc 3, to_zone battlefield)
        # used to put the HIGHEST-CMC creature onto the battlefield uncapped.
        game = make_game()
        rick = game.players[0]
        small = make_card("Wall of Roots", type_line="Creature — Plant Wall",
                          cmc=2)
        huge = make_card("Craterhoof Behemoth",
                         type_line="Creature — Beast", cmc=8)
        rick.library.extend([huge, small])
        self._run(rules, game, {
            "action": "search_library", "player": "Rick",
            "filter_type": "creature", "max_cmc": 3,
            "to_zone": "battlefield", "count": 1,
        })
        bf_names = [c.name for c in rick.battlefield]
        assert "Wall of Roots" in bf_names
        assert "Craterhoof Behemoth" not in bf_names
        assert "Craterhoof Behemoth" in [c.name for c in rick.library]

    def test_underscore_compound_sentinel_finds_both_types(
            self, make_game, make_card, rules):
        # Open the Armory's card_type "aura_or_equipment" never split on
        # " or " and matched NO type line — that tutor found nothing, ever.
        game = make_game()
        rick = game.players[0]
        aura = make_card("Ethereal Armor", type_line="Enchantment — Aura",
                         cmc=1)
        rick.library.append(aura)
        msg = self._run(rules, game, {
            "action": "search_library", "player": "Rick",
            "card_type": "aura_or_equipment", "to_zone": "hand", "count": 1,
        })
        assert "Ethereal Armor" in [c.name for c in rick.hand], msg

    def test_no_duplicate_action_type_branches_in_interpreter(self):
        # Structural: the dead second search_library branch was shadowed by
        # first-match if/elif and carried the keys the templates actually
        # send. No action_type may be compared twice in the chain.
        src = io.open(_ROOT / "mtg" / "actions.py", encoding="utf-8").read()
        types = re.findall(r'\belif action_type == "([a-z_]+)"', src)
        types += re.findall(r'\bif action_type == "([a-z_]+)"', src)
        dupes = {t for t in types if types.count(t) > 1}
        assert not dupes, f"duplicate action_type branches: {dupes}"


# ---------------------------------------------------------------------------
# #4: Kogla's attack trigger resolves a REAL card (no dead sentinel)
# ---------------------------------------------------------------------------

class TestKoglaAttackTrigger:
    def test_kogla_registered_as_generator_not_constant_json(self, lib):
        tmpl = lib._attack_templates.get("kogla, the titan ape")
        assert tmpl is not None
        assert tmpl.action_generator is not None, \
            "Kogla must be a computed-choice Python generator (the JSON " \
            "sentinel BEST_ARTIFACT_OR_ENCHANTMENT resolved nowhere)"

    def test_kogla_destroys_defenders_best_artifact(self, lib, make_game,
                                                    make_card):
        game = make_game()
        claude = game.players[1]
        signet = make_card("Golgari Signet", type_line="Artifact", cmc=2)
        claude.battlefield.append(signet)
        tmpl = lib._attack_templates["kogla, the titan ape"]
        ctx = {"_game": game, "_opponent_player": claude,
               "_controller_player": game.players[0]}
        actions = tmpl.action_generator("Rick", "Claude", ctx)
        assert any(a.get("action") == "destroy"
                   and a.get("card") == "Golgari Signet" for a in actions), \
            actions

    def test_kogla_fizzles_handled_when_no_target(self, lib, make_game):
        # CR 603.3c: no artifact/enchantment on the defending side — a
        # handled no-op ([]), never a Tier-3 escalation.
        game = make_game()
        tmpl = lib._attack_templates["kogla, the titan ape"]
        ctx = {"_game": game, "_opponent_player": game.players[1],
               "_controller_player": game.players[0]}
        assert tmpl.action_generator("Rick", "Claude", ctx) == []

    def test_no_unresolved_uppercase_sentinels_in_json_templates(self):
        # The class pin: a constant JSON action naming an UPPERCASE_SENTINEL
        # card is a silent no-op (only $controller/$opponent substitute).
        data = json.loads((_ROOT / "data" / "card_templates.json")
                          .read_text(encoding="utf-8"))
        offenders = []

        def walk(obj):
            if isinstance(obj, dict):
                card = obj.get("card")
                if (isinstance(card, str) and card.isupper()
                        and "_" in card):
                    offenders.append(card)
                for v in obj.values():
                    walk(v)
            elif isinstance(obj, list):
                for v in obj:
                    walk(v)

        walk(data)
        assert not offenders, offenders


# ---------------------------------------------------------------------------
# #5: Icebreaker Kraken — skip-untap without force-tapping
# ---------------------------------------------------------------------------

class TestKrakenNoTap:
    def test_no_tap_sets_skip_flag_without_tapping_and_counts_all(
            self, make_game, make_card, rules):
        # Live shape needs BOTH already-tapped and untapped qualifying
        # permanents — a fixture with only untapped ones would pass with
        # the old count bug intact (displayed 4 while 8 were affected).
        game = make_game()
        claude = game.players[1]
        perms = []
        for i, tapped in enumerate([False, False, True, True]):
            c = make_card(f"Perm{i}", type_line="Creature — Bear")
            c.tapped = tapped
            claude.battlefield.append(c)
            perms.append(c)
        msg = rules._execute_action_on_state(game, {
            "action": "tap", "scope": "creatures_and_artifacts",
            "target_player": "Claude", "skip_next_untap": True,
            "no_tap": True, "source": "Icebreaker Kraken",
        })
        assert perms[0].tapped is False and perms[1].tapped is False, \
            "the card has no tap clause — nothing may be force-tapped"
        assert all(getattr(c, "_skip_next_untap", False) for c in perms)
        assert "4" in msg and "won't untap" in msg

    def test_default_bulk_tap_still_taps(self, make_game, make_card, rules):
        # Sleep-class control: without no_tap the handler keeps tapping.
        game = make_game()
        claude = game.players[1]
        c = make_card("Bear", type_line="Creature — Bear")
        claude.battlefield.append(c)
        rules._execute_action_on_state(game, {
            "action": "tap", "scope": "all_creatures",
            "target_player": "Claude",
        })
        assert c.tapped is True

    def test_kraken_template_carries_no_tap(self):
        data = json.loads((_ROOT / "data" / "card_templates.json")
                          .read_text(encoding="utf-8"))
        entry = next(t for t in data["templates"]
                     if t.get("key") == "icebreaker kraken")
        assert entry["actions"][0].get("no_tap") is True


# ---------------------------------------------------------------------------
# #6: Teferi -3 — restriction parse + Tier-3 declared-target threading
# ---------------------------------------------------------------------------

class TestTeferiTier3Target:
    def test_restriction_parse_reads_the_minus3_not_the_emblem(self,
                                                               make_card):
        # The apostrophe in "owner's" aborted the capture, so the parser
        # skipped to the -8 EMBLEM's "target permanent an opponent
        # controls." and blocked a legal own-side -3 as "wrong controller".
        from rules.targeting import ControllerRestriction, TargetType
        from rules.targeting_helpers import _parse_target_restriction_from_oracle
        teferi = _cached_card(make_card, "teferi, hero of dominaria")
        r = _parse_target_restriction_from_oracle(teferi)
        assert r is not None
        assert r.controller == ControllerRestriction.ANY
        assert TargetType.NONLAND_PERMANENT in r.target_types

    def test_move_validation_allows_own_and_opponent_nonland(
            self, make_game, make_card):
        from rules.targeting_helpers import _validate_target_for_action
        game = make_game()
        rick, claude = game.players
        teferi = _cached_card(make_card, "teferi, hero of dominaria")
        rick.battlefield.append(teferi)
        own_shark = make_card("Blue Shark", type_line="Creature — Shark")
        rick.battlefield.append(own_shark)
        opp_perm = make_card("Smothering Tithe",
                             type_line="Enchantment")
        claude.battlefield.append(opp_perm)
        legal_own, why_own = _validate_target_for_action(
            game, own_shark, rick, "Teferi, Hero of Dominaria", "Rick")
        assert legal_own, why_own
        legal_opp, why_opp = _validate_target_for_action(
            game, opp_perm, claude, "Teferi, Hero of Dominaria", "Rick")
        assert legal_opp, why_opp

    def test_tier3_prompt_carries_the_declared_target(self, make_game,
                                                      make_card):
        # The fallback used to build its prompt from ability.text alone —
        # Tier 3 then guessed a DIFFERENT permanent (3 loyalty for nothing,
        # game_1535478621572173844). The declared target rides
        # resolve_effect's context= parameter (never effect_desc, whose
        # substrings feed deterministic guards).
        from rules.planeswalker import (AbilityType, PlaneswalkerManager,
                                        PlaneswalkerAbility)
        game = make_game()
        rick = game.players[0]
        teferi = _cached_card(make_card, "teferi, hero of dominaria")
        rick.battlefield.append(teferi)
        shark = make_card("Blue Shark", type_line="Creature — Shark")
        rick.battlefield.append(shark)

        captured = {}

        class _FakeRules:
            client = object()

            async def resolve_effect(self, game, effect_desc, source_card="",
                                     controller="", context=""):
                captured["desc"] = effect_desc
                captured["context"] = context
                return (["ok"], [])

        game._rules_engine = _FakeRules()
        pw = PlaneswalkerManager.__new__(PlaneswalkerManager)
        ability = PlaneswalkerAbility(
            index=1, loyalty_cost=-3,
            ability_type=AbilityType.LOYALTY_MINUS,
            text="Put target nonland permanent into its owner's library "
                 "third from the top.")
        msgs = asyncio.run(pw._try_tier3_pw_ability(
            game, rick, teferi, ability, targets=[shark]))
        assert msgs == ["ok"]
        assert "Blue Shark" in captured["context"]
        assert "Blue Shark" not in captured["desc"], \
            "declared targets must ride context=, not effect_desc"

    def test_no_target_context_when_none_declared(self, make_game, make_card):
        from rules.planeswalker import (AbilityType, PlaneswalkerManager,
                                        PlaneswalkerAbility)
        game = make_game()
        rick = game.players[0]
        teferi = _cached_card(make_card, "teferi, hero of dominaria")
        captured = {}

        class _FakeRules:
            client = object()

            async def resolve_effect(self, game, effect_desc, source_card="",
                                     controller="", context=""):
                captured["context"] = context
                return (["ok"], [])

        game._rules_engine = _FakeRules()
        pw = PlaneswalkerManager.__new__(PlaneswalkerManager)
        ability = PlaneswalkerAbility(
            index=2, loyalty_cost=-8,
            ability_type=AbilityType.LOYALTY_MINUS,
            text="You get an emblem.")
        asyncio.run(pw._try_tier3_pw_ability(
            game, rick, teferi, ability, targets=[]))
        assert captured["context"] == ""


# ---------------------------------------------------------------------------
# #7: no hardcoded "Claude" in provider-tagged console strings
# ---------------------------------------------------------------------------

class TestProviderAwareConsoleStrings:
    def test_ai_turn_carries_no_hardcoded_claude_pass_strings(self):
        src = io.open(_ROOT / "mtg" / "ai_turn.py", encoding="utf-8").read()
        for literal in ("Claude passes", "Claude chose to pass",
                        "Claude chose not to attack"):
            assert literal not in src, \
                f"hardcoded provider name in console string: {literal!r}"


# ---------------------------------------------------------------------------
# #8 (corrected): the data files carry no double-encoded strings
# ---------------------------------------------------------------------------

class TestNoDoubleEncoding:
    def test_validator_finds_data_files_clean(self):
        import sys
        sys.path.insert(0, str(_ROOT / "tools"))
        from validate_card_names import find_double_encoding
        assert find_double_encoding() == []

    def test_card_templates_codepoints_are_real_punctuation(self):
        raw = (_ROOT / "data" / "card_templates.json").read_text(
            encoding="utf-8")
        # The signature char of the double-encoding (U+00E2 'â') must be
        # entirely absent — 30 strings carried it (em dashes and '≤').
        assert "â" not in raw
        # And the REAL em dash must still be present (the repair must not
        # have stripped legitimate punctuation).
        assert "—" in raw


# ---------------------------------------------------------------------------
# #11: unknown action types are named, with the valid vocabulary
# ---------------------------------------------------------------------------

class TestUnknownActionTypeTeaching:
    def test_unknown_type_is_named(self, make_game):
        from mtg.ai_turn import _get_action_error
        game = make_game()
        msg = _get_action_error(None, game, 1, {"type": "pas"})
        assert "'pas'" in msg
        assert "valid types" in msg

    def test_non_string_type_is_safe(self, make_game):
        from mtg.ai_turn import _get_action_error
        game = make_game()
        msg = _get_action_error(None, game, 1, {"type": True})
        assert msg is not None and "True" in msg


# ---------------------------------------------------------------------------
# #12: "you don't control" restriction + the CR 601.2c gate + overload
# ---------------------------------------------------------------------------

RIFT_ORACLE = (
    "Return target nonland permanent you don't control to its owner's "
    "hand.\nOverload {6}{U} (You may cast this spell for its overload "
    "cost. If you do, change its text by replacing all instances of "
    "\"target\" with \"each.\")")


class TestYouDontControlRestriction:
    def test_parser_maps_you_dont_control_to_opponent(self):
        from rules.targeting import (ControllerRestriction, TargetTextParser)
        r = TargetTextParser.parse(
            "target nonland permanent you don't control")
        assert r.controller == ControllerRestriction.OPPONENT

    def test_finder_rejects_own_permanents_for_rift(self, make_game,
                                                    make_card):
        # The live shape: opponent controlled ONLY lands, the caster's own
        # creature satisfied the old ANY restriction and the cast burned
        # {1}{U} + the card for a guaranteed fizzle
        # (game_1535486721779568700).
        from rules.targeting_helpers import _find_any_valid_target
        game = make_game()
        rick, claude = game.players
        rift = make_card("Cyclonic Rift", type_line="Instant",
                         oracle_text=RIFT_ORACLE, cmc=2)
        claude.battlefield.append(
            make_card("Island", type_line="Basic Land — Island"))
        rick.battlefield.append(
            make_card("Birds of Paradise", type_line="Creature — Bird"))
        assert _find_any_valid_target(game, rift, "Rick") is False

    def test_finder_accepts_opponent_nonland(self, make_game, make_card):
        from rules.targeting_helpers import _find_any_valid_target
        game = make_game()
        claude = game.players[1]
        rift = make_card("Cyclonic Rift", type_line="Instant",
                         oracle_text=RIFT_ORACLE, cmc=2)
        claude.battlefield.append(
            make_card("Smothering Tithe", type_line="Enchantment"))
        assert _find_any_valid_target(game, rift, "Rick") is True


class TestValidateCastOverloadCarveOut:
    def _game_with_engine(self, make_game):
        from mtg.engine import GameEngine
        game = make_game()
        engine = GameEngine(None)
        game._rules_engine = engine.rules
        engine.rules.engine_ref = engine
        return game, engine

    def _rift_in_hand(self, make_card, player, lands):
        rift = make_card("Cyclonic Rift", type_line="Instant",
                         mana_cost="{1}{U}", cmc=2, oracle_text=RIFT_ORACLE)
        player.hand.append(rift)
        for i in range(lands):
            land = make_card(f"Island{i}", type_line="Basic Land — Island",
                             oracle_text="{T}: Add {U}.")
            player.battlefield.append(land)
        return rift

    def test_blocked_when_no_target_and_overload_unaffordable(
            self, make_game, make_card):
        from mtg.spells import _validate_cast
        game, engine = self._game_with_engine(make_game)
        rick, claude = game.players
        claude.battlefield.append(
            make_card("Island", type_line="Basic Land — Island"))
        rift = self._rift_in_hand(make_card, rick, lands=2)
        rejection, _, _ = _validate_cast(engine, game, rick, rift, None)
        assert rejection is not None
        assert rejection[0] is False
        assert "no valid targets" in rejection[1]

    def test_allowed_when_overload_affordable(self, make_game, make_card):
        from mtg.spells import _validate_cast
        game, engine = self._game_with_engine(make_game)
        rick, claude = game.players
        claude.battlefield.append(
            make_card("Island", type_line="Basic Land — Island"))
        rift = self._rift_in_hand(make_card, rick, lands=7)
        rejection, _, _ = _validate_cast(engine, game, rick, rift, None)
        assert rejection is None, rejection
