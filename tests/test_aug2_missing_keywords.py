"""Aug 2, 2026 — missing KEYWORD MECHANICS, wave 1.

A sweep of all 1,261 cards in the played decks and cube against the engine
found 33 keyword mechanics with zero or near-zero handling. These are not a
softer category than "bugs": a card that does nothing is a wrong game state,
and the same species has repeatedly been rated CRITICAL when a reviewer
happened to open the right game (madness, suspend initiation, escape,
Torbran all arrived that way).

This wave takes the ones that swing games.

ATTACK KEYWORDS (CR 702). A keyword ability states its trigger in REMINDER
text — or on a bare keyword line with no reminder at all. Emrakul, the Aeons
Torn's entire annihilator clause is the tail of "Flying, protection from
spells that are one or more colors, annihilator 6". The paragraph detector
(_is_self_attack_trigger_paragraph) requires a paragraph that STARTS with
"whenever", so the whole family was unreachable:

  - annihilator 6 on Emrakul — attacks, defending player sacrifices NOTHING,
    in the very deck built around cheating her into play. The only two
    mentions of "annihilator" in the codebase were a docstring example and a
    cost-parsing comment.
  - battle cry on Hero of Bladehold — the token half worked, so the card
    LOOKED handled; the "+1/+0 to each other attacker" half did not exist.
  - melee on Adriana, mentor on Blade Instructor — likewise absent.

ABILITY-WORD CONDITIONS (CR 207.2c). No delirium / morbid / metalcraft /
coven predicate existed anywhere, so every card carrying one used its WEAK
half forever: Tragic Slip was a -1/-1 "removal" spell, Unholy Heat dealt 2
instead of 6, Dragon's Rage Channeler never grew or flew.

Traverse the Ulvenwald was wrong in a THIRD way the sweep did not predict:
its JSON template unconditionally asked for card_type "creature_or_land",
and the handler's filter is a substring match on the type line — so it
matched nothing and the tutor found nothing, ever. The strict JSON loader
caught the Python/JSON key collision the moment the replacement registered,
which is exactly what that check exists for.

LANDWALK (CR 702.14) had no implementation at all — the code comment at the
evasion site literally read "unblockable, fear, intimidate, etc. would go
here". Street Wraith's swampwalk was inert against the only decks it matters
against.
"""
import json

import pytest

import mtg.triggers as trig
from mtg.helpers import (parse_attack_keywords, has_delirium, has_morbid,
                         has_metalcraft, has_coven, has_threshold,
                         graveyard_card_types)
from mtg.models import _parse_landwalk_types


def _cache():
    return json.load(open("data/card_data_cache.json", encoding="utf-8"))


def _real(make_card, name, **kw):
    e = _cache()[name.lower()]
    return make_card(e.get("name", name), type_line=e["type_line"],
                     oracle_text=e["oracle_text"],
                     power=e.get("power") or "1",
                     toughness=e.get("toughness") or "1",
                     mana_cost=e["mana_cost"], **kw)


def _engine():
    from mtg.engine import GameEngine
    return GameEngine(None)


class TestAttackKeywordParsing:
    def test_annihilator_parsed_off_a_bare_keyword_line(self):
        """Emrakul has NO reminder text — the clause is the tail of a
        comma-separated keyword line."""
        e = _cache()["emrakul, the aeons torn"]
        assert parse_attack_keywords(e["oracle_text"]) == {"annihilator": 6}

    @pytest.mark.parametrize("name,expected", [
        ("hero of bladehold", "battle_cry"),
        ("adriana, captain of the guard", "melee"),
        ("blade instructor", "mentor"),
    ])
    def test_reminder_text_keywords_parsed(self, name, expected):
        kws = parse_attack_keywords(_cache()[name]["oracle_text"])
        assert kws.get(expected) is True, kws

    @pytest.mark.parametrize("name", ["sol ring", "monastery swiftspear",
                                      "lightning bolt"])
    def test_unrelated_cards_parse_empty(self, name):
        assert parse_attack_keywords(_cache()[name]["oracle_text"]) == {}

    def test_a_sentence_containing_a_keyword_word_is_not_a_keyword_line(self):
        """The keyword-line test is 'no sentence punctuation outside reminder
        text' — otherwise any prose mentioning melee would false-positive."""
        assert parse_attack_keywords(
            "Whenever this creature attacks, it gains melee until end of "
            "turn if you say so.") == {}

    def test_a_GRANT_line_does_not_give_the_source_the_trigger(self):
        """Found by mutation-testing this parser: "Other creatures you
        control have flying, melee" tokenizes to a bare "melee" and handed
        the attack trigger to the card that merely GRANTS it — the July-21
        Yidris cascade-grant class, in a new subsystem."""
        assert parse_attack_keywords(
            "Other creatures you control have flying, melee.") == {}
        assert parse_attack_keywords(
            "Creatures you control gain battle cry until end of turn.") == {}

    def test_adriana_still_parses_her_own_melee(self):
        """Control — Adriana has BOTH a keyword line and a grant line; the
        grant filter must not eat the real one."""
        kws = parse_attack_keywords(_cache()["adriana, captain of the guard"]["oracle_text"])
        assert kws.get("melee") is True


class TestAnnihilator:
    def test_defending_player_sacrifices_that_many(self, game, make_card):
        rick, claude = game.players
        eng = _engine()
        game._rules_engine = eng.rules
        emrakul = _real(make_card, "Emrakul, the Aeons Torn")
        rick.battlefield.append(emrakul)
        for i in range(8):
            claude.battlefield.append(make_card(f"Perm{i}"))
        emrakul.attacking = True
        game.attackers = [emrakul.id]
        trig._check_attack_triggers_sync(eng, game, emrakul, rick)
        assert len(claude.battlefield) == 2, (
            "annihilator 6 must strip six permanents — this did nothing at "
            "all before, on the mythic deck's top-end bomb")

    def test_it_hits_the_defender_not_the_attacker(self, game, make_card):
        rick, claude = game.players
        eng = _engine()
        game._rules_engine = eng.rules
        emrakul = _real(make_card, "Emrakul, the Aeons Torn")
        rick.battlefield.append(emrakul)
        for i in range(4):
            rick.battlefield.append(make_card(f"Mine{i}"))
            claude.battlefield.append(make_card(f"Theirs{i}"))
        emrakul.attacking = True
        game.attackers = [emrakul.id]
        before_mine = len(rick.battlefield)
        trig._check_attack_triggers_sync(eng, game, emrakul, rick)
        assert len(rick.battlefield) == before_mine
        assert len(claude.battlefield) == 0

    def test_a_creature_without_annihilator_sacrifices_nothing(self, game,
                                                               make_card):
        rick, claude = game.players
        eng = _engine()
        game._rules_engine = eng.rules
        bear = make_card("Bear", oracle_text="Flying, trample")
        rick.battlefield.append(bear)
        for i in range(4):
            claude.battlefield.append(make_card(f"Perm{i}"))
        bear.attacking = True
        game.attackers = [bear.id]
        trig._check_attack_triggers_sync(eng, game, bear, rick)
        assert len(claude.battlefield) == 4


class TestBattleCryMeleeMentor:
    def test_battle_cry_pumps_other_attackers_only(self, game, make_card):
        rick, claude = game.players
        eng = _engine()
        game._rules_engine = eng.rules
        hero = _real(make_card, "Hero of Bladehold")
        ally = make_card("Ally", power="2", toughness="2")
        bench = make_card("Bench", power="2", toughness="2")
        rick.battlefield.extend([hero, ally, bench])
        hero.attacking = ally.attacking = True
        game.attackers = [hero.id, ally.id]
        trig._check_attack_triggers_sync(eng, game, hero, rick)
        game.recalculate_power_toughness()
        assert ally.get_effective_power(game) == 3, "other attacker gets +1/+0"
        assert bench.get_effective_power(game) == 2, (
            "a creature that is NOT attacking must not be pumped")
        assert hero.get_effective_power(game) == 3, (
            "battle cry says each OTHER attacking creature — not itself")

    def test_melee_pumps_the_attacker(self, game, make_card):
        rick, claude = game.players
        eng = _engine()
        game._rules_engine = eng.rules
        adriana = _real(make_card, "Adriana, Captain of the Guard")
        rick.battlefield.append(adriana)
        adriana.attacking = True
        game.attackers = [adriana.id]
        trig._check_attack_triggers_sync(eng, game, adriana, rick)
        game.recalculate_power_toughness()
        # Printed 4/4; two-player melee is exactly +1/+1.
        assert adriana.get_effective_power(game) == 5
        assert adriana.get_effective_toughness(game) == 5

    def test_mentor_counters_a_lesser_power_attacker(self, game, make_card):
        rick, claude = game.players
        eng = _engine()
        game._rules_engine = eng.rules
        instructor = _real(make_card, "Blade Instructor")   # 3/1
        small = make_card("Small", power="1", toughness="1")
        rick.battlefield.extend([instructor, small])
        instructor.attacking = small.attacking = True
        game.attackers = [instructor.id, small.id]
        trig._check_attack_triggers_sync(eng, game, instructor, rick)
        assert small.counters.get("+1/+1") == 1

    def test_mentor_declines_when_nothing_has_lesser_power(self, game,
                                                           make_card,
                                                           capsys):
        """CR 603.3c — no legal target, the trigger does nothing."""
        rick, claude = game.players
        eng = _engine()
        game._rules_engine = eng.rules
        instructor = _real(make_card, "Blade Instructor")   # 3/1
        big = make_card("Big", power="7", toughness="7")
        rick.battlefield.extend([instructor, big])
        instructor.attacking = big.attacking = True
        game.attackers = [instructor.id, big.id]
        trig._check_attack_triggers_sync(eng, game, instructor, rick)
        assert not big.counters.get("+1/+1")
        assert "no attacking creature with lesser power" in capsys.readouterr().out


class TestConditionPredicates:
    def test_delirium_counts_distinct_card_types(self, game, make_card):
        rick = game.players[0]
        for t in ("Artifact", "Creature — Bear", "Enchantment"):
            rick.graveyard.append(make_card("g", type_line=t))
        assert not has_delirium(rick)
        rick.graveyard.append(make_card("g4", type_line="Instant"))
        assert has_delirium(rick)

    def test_delirium_does_not_double_count_one_type(self, game, make_card):
        rick = game.players[0]
        for i in range(9):
            rick.graveyard.append(make_card(f"g{i}", type_line="Creature — Bear"))
        assert graveyard_card_types(rick) == {"creature"}
        assert not has_delirium(rick)

    def test_metalcraft_needs_three_artifacts(self, game, make_card):
        rick = game.players[0]
        for i in range(2):
            rick.battlefield.append(make_card(f"a{i}", type_line="Artifact"))
        assert not has_metalcraft(rick)
        rick.battlefield.append(make_card("a3", type_line="Artifact"))
        assert has_metalcraft(rick)

    def test_threshold_needs_seven_cards(self, game, make_card):
        rick = game.players[0]
        for i in range(6):
            rick.graveyard.append(make_card(f"g{i}"))
        assert not has_threshold(rick)
        rick.graveyard.append(make_card("g7"))
        assert has_threshold(rick)

    def test_coven_needs_three_DIFFERENT_powers(self, game, make_card):
        rick = game.players[0]
        for p in ("2", "2", "2"):
            rick.battlefield.append(make_card("same", power=p, toughness="2"))
        assert not has_coven(rick, game), "three creatures, one distinct power"
        rick.battlefield.append(make_card("x", power="3", toughness="3"))
        rick.battlefield.append(make_card("y", power="4", toughness="4"))
        assert has_coven(rick, game)

    def test_morbid_reads_a_whole_turn_not_a_wave(self, game):
        """The wave-scoped _recently_died list is reset mid-turn by the dies
        dispatcher, so morbid needs its own per-turn flag."""
        assert not has_morbid(game)
        game._creature_died_this_turn = True
        assert has_morbid(game)

    def test_the_morbid_flag_is_stamped_at_the_death_choke_point(self):
        import inspect
        src = inspect.getsource(trig._accumulate_death_subscriber)
        assert "_creature_died_this_turn = True" in src, (
            "stamping anywhere but the one choke point every death path "
            "reaches would miss whole death classes")

    def test_the_morbid_flag_is_a_declared_field_and_is_reset(self):
        import inspect
        from mtg.models import GameState
        import mtg.engine
        assert "_creature_died_this_turn" in GameState.__dataclass_fields__
        assert "_creature_died_this_turn = False" in inspect.getsource(mtg.engine), (
            "an unreset flag makes morbid permanently true from turn 2 on")


class TestConditionCards:
    def _spell(self, game, lib, name):
        from rules.effect_templates import build_game_context
        rick, claude = game.players
        ctx = build_game_context(game, rick, claude)
        return lib.resolve_spell(_cache()[name]["name"],
                                 _cache()[name]["oracle_text"],
                                 rick.name, claude.name, game_context=ctx)[0]

    def test_tragic_slip_is_minus_one_without_morbid(self, game, lib,
                                                     make_card):
        game.players[1].battlefield.append(
            make_card("Victim", power="4", toughness="4"))
        a = self._spell(game, lib, "tragic slip")
        assert a[0]["power"] == -1 and a[0]["toughness"] == -1

    def test_tragic_slip_is_minus_thirteen_with_morbid(self, game, lib,
                                                       make_card):
        game._creature_died_this_turn = True
        game.players[1].battlefield.append(
            make_card("Victim", power="4", toughness="4"))
        a = self._spell(game, lib, "tragic slip")
        assert a[0]["power"] == -13, (
            "without a morbid predicate this was a removal spell that "
            "removed nothing")

    def test_unholy_heat_scales_with_delirium(self, game, lib, make_card):
        rick, claude = game.players
        claude.battlefield.append(make_card("Victim", power="4", toughness="4"))
        assert self._spell(game, lib, "unholy heat")[0]["amount"] == 2
        for t in ("Artifact", "Creature — Bear", "Enchantment", "Instant"):
            rick.graveyard.append(make_card("g", type_line=t))
        assert self._spell(game, lib, "unholy heat")[0]["amount"] == 6

    def test_traverse_searches_a_basic_land_without_delirium(self, game, lib):
        a = self._spell(game, lib, "traverse the ulvenwald")
        assert a[0]["card_type"] == "basic land"

    def test_traverse_upgrades_with_delirium(self, game, lib, make_card):
        rick = game.players[0]
        for t in ("Artifact", "Creature — Bear", "Enchantment", "Instant"):
            rick.graveyard.append(make_card("g", type_line=t))
        a = self._spell(game, lib, "traverse the ulvenwald")
        assert a[0]["card_type"] == "creature"

    def test_traverse_never_asks_for_the_bogus_filter_again(self, game, lib):
        """The old JSON entry asked for card_type "creature_or_land"; the
        handler's filter is a SUBSTRING match on the type line, so it matched
        nothing and the tutor found nothing, ever."""
        a = self._spell(game, lib, "traverse the ulvenwald")
        assert a[0]["card_type"] != "creature_or_land"


class TestLandwalk:
    def test_parses_the_keyword_not_the_reminder(self):
        assert _parse_landwalk_types(
            _cache()["street wraith"]["oracle_text"]) == {"swamp"}
        assert _parse_landwalk_types("Flying, trample") == set()

    def test_unblockable_when_defender_controls_that_land(self, game,
                                                          make_card):
        rick, claude = game.players
        wraith = _real(make_card, "Street Wraith")
        rick.battlefield.append(wraith)
        blocker = make_card("Blocker", power="2", toughness="2")
        claude.battlefield.append(blocker)
        assert blocker.can_block(wraith, game=game), "no swamp yet"
        claude.battlefield.append(make_card(
            "Swamp", type_line="Basic Land — Swamp", power=None,
            toughness=None))
        assert not blocker.can_block(wraith, game=game), (
            "CR 702.14 — swampwalk is the entire reason to run this card")

    def test_the_check_is_on_the_DEFENDER_s_lands(self, game, make_card):
        """The attacker's own Swamps are irrelevant."""
        rick, claude = game.players
        wraith = _real(make_card, "Street Wraith")
        rick.battlefield.append(wraith)
        rick.battlefield.append(make_card("Swamp", type_line="Basic Land — Swamp",
                                          power=None, toughness=None))
        blocker = make_card("Blocker", power="2", toughness="2")
        claude.battlefield.append(blocker)
        assert blocker.can_block(wraith, game=game)

    def test_a_nonmatching_land_type_does_not_grant_evasion(self, game,
                                                            make_card):
        rick, claude = game.players
        wraith = _real(make_card, "Street Wraith")
        rick.battlefield.append(wraith)
        blocker = make_card("Blocker", power="2", toughness="2")
        claude.battlefield.extend([blocker, make_card(
            "Island", type_line="Basic Land — Island", power=None,
            toughness=None)])
        assert blocker.can_block(wraith, game=game)
