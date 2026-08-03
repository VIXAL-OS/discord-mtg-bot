"""Aug 2, 2026 — the last three batch-14 open items.

TIER-3 SHRINK. Five templates for the batch's top escalations, each of which
was a real repeated Claude-API call resolving a deterministic effect:
Sire of Insanity ×17, Song of the Worldsoul ×16, Arclight Phoenix ×14,
Glissa the Traitor ×11, The Ozolith ×8.

Arclight needed two things beyond a template. Its condition counts INSTANT
AND SORCERY spells specifically, and the closest existing counter
(noncreature_spells_cast_this_turn) also counts artifacts, enchantments and
planeswalkers — so it got its own. And its trigger functions FROM THE
GRAVEYARD (CR 603.6d), while the beginning-of-combat scan walked the
battlefield only: the ability was unreachable in its own zone, and all 14
escalations were the already-on-battlefield case resolving nothing. The scan
now also walks graveyard cards whose trigger text names that zone.

CUBE DECK-BUILDER. Colorless cards never entered the on-color branch, so
they sat at the base 5.0 while any on-color card started at 10.0 — Sol Ring,
Skullclamp, Mind Stone and both Signets all lost their slots to mediocre
on-color filler, and tied with each other at exactly 5.0 so Python's stable
sort settled it by DRAFT PICK ORDER. The on-color bonus is really a
CASTABILITY bonus, and a colorless card is castable in every deck ever
built. A heuristic power signal (mana positivity, cheap repeatable card
advantage) then breaks the remaining ties on merit rather than pick order.

WASH AWAY was a FALSE POSITIVE and is recorded as one — see the class docs
at the bottom. The message reached Discord all along; async stack resolution
just interleaved it six lines past the cast. It gained a console tag so a
console-based audit can see it, which is what would have prevented the
misfiling.
"""
import re

import pytest


SIRE = "At the beginning of each end step, each player discards their hand."
SONG = "Whenever you cast a spell, populate."
OZOLITH = ("At the beginning of combat on your turn, if The Ozolith has "
           "counters on it, you may move all counters from The Ozolith onto "
           "target creature.")
ARCLIGHT = ("At the beginning of combat on your turn, if you've cast three "
            "or more instant and sorcery spells this turn, return this card "
            "from your graveyard to the battlefield.")
GLISSA = ("Whenever a creature an opponent controls dies, you may return "
          "target artifact card from your graveyard to your hand.")


def _ctx(game):
    from rules.effect_templates import build_game_context
    return build_game_context(game, game.players[0], game.players[1])


class TestTierThreeShrinkTemplates:
    def test_sire_of_insanity_discards_both_hands(self, game, lib, make_card):
        rick, claude = game.players
        rick.hand.append(make_card("A"))
        claude.hand.append(make_card("B"))
        actions, _ = lib.resolve_etb("Sire of Insanity", SIRE, rick.name,
                                     claude.name, game_context=_ctx(game),
                                     event_type="end_step")
        assert actions, "Sire escalated to Tier 3 seventeen times in one batch"
        assert [(a["action"], a["player"], a["card"]) for a in actions] == [
            ("discard", "Rick", "all"), ("discard", "Claude", "all")], actions

    def test_song_of_the_worldsoul_populates(self, game, lib):
        rick, claude = game.players
        actions, _ = lib.resolve_etb("Song of the Worldsoul", SONG, rick.name,
                                     claude.name, game_context=_ctx(game),
                                     event_type="cast_trigger")
        assert actions == [{"action": "populate", "player": "Rick"}], actions

    def test_populate_handler_still_refuses_to_invent_a_token(self, rules,
                                                              game):
        """The template leans on the CR 701.34a-correct handler — pin that it
        does nothing on an empty token board (the July 20 fix, after Tier 3
        fabricated a token from thin air)."""
        msg = rules._execute_action_on_state(
            game, {"action": "populate", "player": "Rick"})
        assert msg is None
        assert not game.players[0].battlefield

    def test_ozolith_moves_every_counter_type(self, game, lib, make_card):
        rick, claude = game.players
        oz = make_card("The Ozolith", type_line="Legendary Artifact")
        oz.counters = {"+1/+1": 3, "shield": 1}
        rick.battlefield.append(oz)
        rick.battlefield.append(make_card("Bear", power="2", toughness="2"))
        actions, _ = lib.resolve_etb("The Ozolith", OZOLITH, rick.name,
                                     claude.name, game_context=_ctx(game),
                                     event_type="beginning_combat")
        moved = {a["counter_type"] for a in actions
                 if a["action"] == "add_counters"}
        assert moved == {"+1/+1", "shield"}, actions
        assert all(a["card"] == "Bear" for a in actions
                   if a["action"] == "add_counters")
        assert all(a["card"] == "The Ozolith" for a in actions
                   if a["action"] == "remove_counters")

    def test_ozolith_with_no_counters_is_a_handled_no_op(self, game, lib,
                                                         make_card):
        """CR 603.4 intervening-if — and a no_action is what keeps this OFF
        the Tier-3 queue."""
        rick, claude = game.players
        rick.battlefield.append(make_card("The Ozolith",
                                          type_line="Legendary Artifact"))
        actions, _ = lib.resolve_etb("The Ozolith", OZOLITH, rick.name,
                                     claude.name, game_context=_ctx(game),
                                     event_type="beginning_combat")
        assert actions and actions[0]["action"] == "no_action"

    def test_arclight_returns_at_three_instants(self, game, lib, make_card):
        rick, claude = game.players
        rick.graveyard.append(make_card("Arclight Phoenix",
                                        type_line="Creature — Phoenix"))
        rick.instant_sorcery_spells_cast_this_turn = 3
        actions, _ = lib.resolve_etb("Arclight Phoenix", ARCLIGHT, rick.name,
                                     claude.name, game_context=_ctx(game),
                                     event_type="beginning_combat")
        assert actions == [{"action": "move_card", "card": "Arclight Phoenix",
                            "from_zone": "graveyard",
                            "to_zone": "battlefield", "player": "Rick"}]

    def test_arclight_declines_below_three(self, game, lib, make_card):
        rick, claude = game.players
        rick.graveyard.append(make_card("Arclight Phoenix",
                                        type_line="Creature — Phoenix"))
        rick.instant_sorcery_spells_cast_this_turn = 2
        actions, _ = lib.resolve_etb("Arclight Phoenix", ARCLIGHT, rick.name,
                                     claude.name, game_context=_ctx(game),
                                     event_type="beginning_combat")
        assert actions and actions[0]["action"] == "no_action"
        assert "2 instant/sorcery" in actions[0]["reason"], actions

    def test_arclight_on_the_battlefield_is_a_no_op(self, game, lib,
                                                    make_card):
        """All 14 live escalations were this case — already returned, so the
        return does nothing. It must not cost an API call."""
        rick, claude = game.players
        rick.battlefield.append(make_card("Arclight Phoenix",
                                          type_line="Creature — Phoenix"))
        rick.instant_sorcery_spells_cast_this_turn = 5
        actions, _ = lib.resolve_etb("Arclight Phoenix", ARCLIGHT, rick.name,
                                     claude.name, game_context=_ctx(game),
                                     event_type="beginning_combat")
        assert actions and actions[0]["action"] == "no_action"

    def test_glissa_returns_the_best_artifact(self, game, lib, make_card):
        rick, claude = game.players
        rick.graveyard.append(make_card("Wurmcoil Engine",
                                        type_line="Artifact Creature", cmc=6))
        rick.graveyard.append(make_card("Bauble", type_line="Artifact", cmc=0))
        actions, _ = lib.resolve_dies_trigger(
            "Glissa, the Traitor", GLISSA, "Bear", 2, 2,
            rick.name, claude.name, game_context=_ctx(game))
        assert actions == [{"action": "move_card", "card": "Wurmcoil Engine",
                            "from_zone": "graveyard", "to_zone": "hand",
                            "player": "Rick"}]

    def test_glissa_with_no_artifact_is_a_handled_no_op(self, game, lib,
                                                        make_card):
        rick, claude = game.players
        rick.graveyard.append(make_card("Forest", type_line="Land"))
        actions, _ = lib.resolve_dies_trigger(
            "Glissa, the Traitor", GLISSA, "Bear", 2, 2,
            rick.name, claude.name, game_context=_ctx(game))
        assert actions and actions[0]["action"] == "no_action"


class TestInstantSorceryCounter:
    """Arclight's condition needs its own counter — the noncreature one is
    too broad (it also counts artifacts, enchantments, planeswalkers)."""

    def test_counter_is_a_declared_field(self):
        from mtg.models import Player
        assert "instant_sorcery_spells_cast_this_turn" in \
            Player.__dataclass_fields__

    def test_only_instants_and_sorceries_increment_it(self):
        import inspect
        import mtg.spells
        src = inspect.getsource(mtg.spells)
        i = src.index("instant_sorcery_spells_cast_this_turn += 1")
        window = src[max(0, i - 300):i]
        assert "'instant' in _tl_cast or 'sorcery' in _tl_cast" in window, (
            "it must gate on the TYPE LINE — reusing the noncreature "
            "counter would let an artifact advance Arclight's condition")

    def test_it_is_reset_each_turn(self):
        import inspect
        import mtg.engine
        src = inspect.getsource(mtg.engine)
        assert "instant_sorcery_spells_cast_this_turn = 0" in src, (
            "an unreset counter would return Arclight every turn forever")


class TestBeginningCombatScansGraveyardTriggers:
    def test_scan_walks_graveyard_cards_that_name_that_zone(self):
        import inspect
        import mtg.triggers
        src = inspect.getsource(
            mtg.triggers._check_beginning_combat_triggers_sync)
        assert "_gy_triggers" in src and "from your graveyard" in src, (
            "Arclight's trigger functions from the graveyard (CR 603.6d); "
            "a battlefield-only scan makes the ability unreachable")

    def test_the_graveyard_filter_is_narrow(self):
        """Scanning the whole graveyard every combat would be a real cost —
        only cards whose trigger names the zone are considered."""
        import inspect
        import mtg.triggers
        src = inspect.getsource(
            mtg.triggers._check_beginning_combat_triggers_sync)
        i = src.index("_gy_triggers")
        window = src[i:i + 500]
        assert "beginning of combat" in window and "from your graveyard" in window

    def _engine(self, game):
        """The scan reaches the action interpreter via engine.rules, so it
        needs a GameEngine — not the bare RulesEngine fixture."""
        from mtg.engine import GameEngine
        eng = GameEngine(None)
        game._rules_engine = eng.rules
        return eng

    def test_a_graveyard_phoenix_actually_returns(self, game, make_card):
        """Behavioral — the structural pin above passes even if the scan
        stops USING _gy_triggers, which is exactly what its mutant did."""
        import mtg.triggers as trig
        rick = game.players[0]
        phoenix = make_card("Arclight Phoenix",
                            type_line="Creature — Phoenix",
                            power="3", toughness="2", oracle_text=ARCLIGHT)
        rick.graveyard.append(phoenix)
        rick.instant_sorcery_spells_cast_this_turn = 3
        game.active_player_index = 0
        trig._check_beginning_combat_triggers_sync(self._engine(game), game)
        assert phoenix in rick.battlefield, (
            "the trigger functions from the graveyard (CR 603.6d) — a "
            "battlefield-only scan never sees it")
        assert phoenix not in rick.graveyard

    def test_a_graveyard_phoenix_stays_put_below_three_casts(self, game,
                                                             make_card):
        import mtg.triggers as trig
        rick = game.players[0]
        phoenix = make_card("Arclight Phoenix",
                            type_line="Creature — Phoenix",
                            power="3", toughness="2", oracle_text=ARCLIGHT)
        rick.graveyard.append(phoenix)
        rick.instant_sorcery_spells_cast_this_turn = 2
        game.active_player_index = 0
        trig._check_beginning_combat_triggers_sync(self._engine(game), game)
        assert phoenix in rick.graveyard and phoenix not in rick.battlefield


class TestCubeDeckBuilderPowerScoring:
    def _score(self, name, main_colors=("U", "B")):
        """Calls the PRODUCTION scorer.

        The first version of this helper reimplemented the scoring inline
        and duly SURVIVED three of its own mutants — the same
        mirrored-predicate trap this session already hit twice. The logic
        now lives in cube_draft.score_card_for_deck and both callers share
        it.
        """
        import json
        import cube_draft
        from mtg.models import Card
        cache = json.load(open("data/card_data_cache.json", encoding="utf-8"))
        e = cache[name.lower()]
        card = Card(name=e.get("name", name), mana_cost=e["mana_cost"],
                    cmc=e.get("cmc", 0), type_line=e["type_line"],
                    oracle_text=e["oracle_text"], power=e.get("power"),
                    toughness=e.get("toughness"))
        return cube_draft.score_card_for_deck(card, list(main_colors))

    def test_the_builder_consumes_the_shared_scorer(self):
        import inspect
        import cube_draft
        src = inspect.getsource(cube_draft.auto_build_deck)
        assert "score_card_for_deck(card, main_colors)" in src, (
            "auto_build_deck must call the shared scorer, not re-inline it")

    def test_colorless_cards_get_the_castability_bonus(self):
        """They are castable in EVERY deck — that is what the on-color bonus
        is actually measuring."""
        assert self._score("Thought Vessel") >= 10.0

    def test_sol_ring_outscores_mediocre_on_color_filler(self):
        """The live symptom: Sol Ring sat at the 5.0 floor and lost its slot
        to on-color filler, then lost the tiebreak to an equally-scored
        colorless card on DRAFT PICK ORDER."""
        sol = self._score("Sol Ring")
        assert sol > 12.0, sol
        assert sol > self._score("Thought Vessel"), (
            "a mana-positive rock must outscore a vanilla colorless one — "
            "otherwise pick order decides again")

    def test_mana_positivity_is_what_separates_them(self):
        """Sol Ring ({1} for two mana) beats a Signet ({2} that costs {1} to
        use) on the produced-vs-cost signal, not on a hardcoded name."""
        assert self._score("Sol Ring") > self._score("Azorius Signet")

    def test_cheap_card_advantage_scores(self):
        assert self._score("Skullclamp") > self._score("Scroll Rack")

    def test_scoring_uses_no_hardcoded_card_names(self):
        """A named-staple list would rot the moment the cube changes, and
        nothing would validate it."""
        import inspect
        import cube_draft
        # Comments legitimately name the cards the fix was diagnosed from;
        # what must not exist is a name-keyed BRANCH. Strip comments first.
        code = "\n".join(
            line.split("#", 1)[0]
            for line in inspect.getsource(cube_draft.auto_build_deck).splitlines()
        ).lower()
        for banned in ("sol ring", "skullclamp", "signet"):
            assert banned not in code, (
                f"{banned!r} is hardcoded in LOGIC — use a heuristic signal "
                f"instead; a named-staple list rots when the cube changes")


class TestWashAwayWasAFalsePositive:
    """The reviewer's "Wash Away's fizzle is invisible" finding.

    It was not invisible. The message reached Discord all along — nine
    fizzles across batch 15334, including the very game the finding came
    from, six lines past the cast. Async stack resolution interleaves a
    response's effect messages with the cast triggers ahead of it, so
    "absent from my search window" is not "absent from the log". That is the
    deferred-drain false-positive pattern wearing new clothes.

    The real (minor) gap it exposed: the message had no CONSOLE tag, so a
    console-based audit genuinely could not see it. Now it does.
    """

    def test_the_restriction_still_fizzles_correctly(self, rules, game,
                                                     make_card):
        from mtg.models import StackEntry
        victim = make_card("Fierce Guardianship", type_line="Instant")
        victim._cast_origin = "hand"
        game.stack.append(StackEntry(card=victim,
                                     controller_name=game.players[1].name,
                                     controller_index=1))
        msg = rules._execute_action_on_state(game, {
            "action": "counter_spell", "player": "Rick", "target": "stack_top",
            "_source_card_name": "Wash Away",
            "_source_oracle": ("Counter target spell that wasn't cast from "
                               "its owner's hand.")})
        assert "fizzles" in (msg or ""), msg
        assert game.stack[-1].countered is False

    def test_the_fizzle_now_has_a_console_tag(self, rules, game, make_card,
                                              capsys):
        from mtg.models import StackEntry
        victim = make_card("Fierce Guardianship", type_line="Instant")
        victim._cast_origin = "hand"
        game.stack.append(StackEntry(card=victim,
                                     controller_name=game.players[1].name,
                                     controller_index=1))
        rules._execute_action_on_state(game, {
            "action": "counter_spell", "player": "Rick", "target": "stack_top",
            "_source_card_name": "Wash Away",
            "_source_oracle": ("Counter target spell that wasn't cast from "
                               "its owner's hand.")})
        out = capsys.readouterr().out
        assert "[COUNTER-FIZZLE] Wash Away" in out, out
