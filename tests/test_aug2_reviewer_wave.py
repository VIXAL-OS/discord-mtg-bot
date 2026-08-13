"""Aug 2, 2026 batch-13 reviewer-wave pins (batch game_15332*, sha=c042f46).

Four Sonnet reviewers (delve/burn — first delve examination EVER; standard
spot-check #83; rashmi/mythic phase-1 classic; escape/graveyard Kroxa
first-fires). ~14 findings, 0 flat false positives. Each fix pinned with
fixtures shaped like the LIVE path:

- Thought Scour (delve, CRITICAL): the Tier-2 mill pattern was digit-only —
  "Target player mills two cards" matched nothing (word-number class), AND
  the unconditional "Draw a card." was redirected to the opponent because
  ExecutionContext.targets is shared across the whole spell's clauses
  (the auto-target chosen for the DROPPED mill clause hijacked the draw).
  The delve deck's graveyard engine was disabled end to end.

- Searing Blaze feedback (delve): a paid cast that resolution-declines
  returns success=True, so the AI re-cast into the same empty board.
  The decline now feeds game._recent_plan_rejections.

- Impulse window (delve): end_turn cleared playable_from_exile for EVERY
  player EVERY turn — Light Up the Stage's "until the end of your next
  turn" was same-turn-only. Stamped per-exile, expired at the owner's
  next end of turn.

- Post-end phase chain (standard, CRITICAL): UNTAP→UPKEEP→DRAW→MAIN1 ran
  unguarded — a lethal upkeep trigger (suspended Rift Bolt) ended the game
  and the winner still drew a card (CR 104.2a).

- Prismatic Ending (standard): the captured target phrase kept the "if its
  mana value is ... cast this spell" tail, "spell" matched TargetType.SPELL,
  and the card read unplayable on every empty stack all game.

- Advertisement one-tap ceiling (standard): the OR-dual double-count fixed
  July 20 on the PAYMENT side survived in legal_actions' castable list —
  Solitude offered off 3 physical sources, cast failed at payment.

- Comet Storm (rashmi/mythic): "any target" recognized at the CAST gate
  since July 30 but not the RESOLUTION re-check — legally cast, fizzled
  with "players are not creatures".

- Tooth and Nail (rashmi/mythic, CRITICAL): the flat JSON entry resolved
  the ENTWINED result unconditionally. Entwine (CR 702.42) is now modeled
  as kicker's additive-cost twin; the template branches on ctx['entwined'].

- PW token type line (rashmi/mythic, CRITICAL): the generic PW token
  fallback hardcoded "Token Creature" — Chandra, Spark Hunter's VEHICLE
  token attacked without ever being crewed.

- TargetType.CARD (escape/graveyard): no branch in _check_type_match —
  every "target card in/from a graveyard" spell was permanently uncastable
  (Cling to Dust unplayable all game, its escape cast rolled back).

- "card_type": "Any" (escape/graveyard): the JSON tutor sentinel was
  matched as a literal type-line substring — Entomb/Vile Entomber found
  NOTHING twice live (Demonic/Vampiric Tutor same path).

- Victimize (escape/graveyard): the resolution re-check's Card-object
  branch only scanned battlefields — graveyard-targeting spells always
  fizzled ("no longer on the battlefield" for a card sitting untouched in
  the caster's graveyard).

- Meren experience (escape/graveyard): (a) the destroy action resolves
  dies triggers inline and never emits CREATURE_DIED — the choke-point XP
  grant missed every destroy-path death; (b) the SBA sweep removes every
  dying creature BEFORE queueing, so a watcher dying in the same batch
  (Meren in her own Toxic Deluge) missed its batch-mates. Shared
  grant_experience_for_death helper + batch context on the emit.

- Life from the Loam (escape/graveyard): zero-action Tier-3 escalation →
  deterministic template (up to three lands to hand).
"""
import asyncio
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


def _lib():
    from rules.effect_templates import get_effect_library
    return get_effect_library()


# ---------------------------------------------------------------------------
# Thought Scour: word-number mill + per-clause draw scoping
# ---------------------------------------------------------------------------

THOUGHT_SCOUR = "Target player mills two cards.\nDraw a card."


class TestThoughtScourClass:
    def test_word_number_mill_parses(self):
        from rules.effects import EffectExecutor, EffectType
        effects = EffectExecutor().parse_effects(THOUGHT_SCOUR)
        mills = [e for e in effects if e.effect_type == EffectType.MILL]
        assert mills and mills[0].amount == 2
        draws = [e for e in effects if e.effect_type == EffectType.DRAW]
        assert draws and draws[0].amount == 1

    def test_unconditional_draw_goes_to_caster(self, game, make_card):
        """The LIVE shape: the mill clause auto-targeted the opponent; the
        shared ctx.targets list must not hijack the caster's draw."""
        from rules.effects import EffectExecutor, ExecutionContext, EffectType
        from rules.spell_resolver import SpellResolver
        rick, claude = game.players
        for _ in range(3):
            rick.library.append(make_card("Island", type_line="Basic Land",
                                          power=None, toughness=None))
            claude.library.append(make_card("Swamp", type_line="Basic Land",
                                            power=None, toughness=None))
        effects = EffectExecutor().parse_effects(THOUGHT_SCOUR)
        draw = next(e for e in effects if e.effect_type == EffectType.DRAW)
        src = make_card("Thought Scour", type_line="Instant",
                        power=None, toughness=None)
        ctx = ExecutionContext(game_state=game, source_card=src,
                               source_controller=rick, targets=[claude])
        resolver = SpellResolver(None)
        rick_hand = len(rick.hand)
        claude_hand = len(claude.hand)
        asyncio.run(resolver._exec_draw(draw, ctx, game))
        assert len(rick.hand) == rick_hand + 1, "caster draws"
        assert len(claude.hand) == claude_hand, "opponent must NOT draw"

    def test_targeted_draw_still_redirects(self, game, make_card):
        from rules.effects import Effect, EffectType, ExecutionContext
        from rules.spell_resolver import SpellResolver
        rick, claude = game.players
        claude.library.append(make_card("Swamp", type_line="Basic Land",
                                        power=None, toughness=None))
        draw = Effect(effect_type=EffectType.DRAW, amount=1,
                      raw_text="target player draws a card")
        src = make_card("Prosperity-ish", type_line="Sorcery",
                        power=None, toughness=None)
        ctx = ExecutionContext(game_state=game, source_card=src,
                               source_controller=rick, targets=[claude])
        claude_hand = len(claude.hand)
        asyncio.run(SpellResolver(None)._exec_draw(draw, ctx, game))
        assert len(claude.hand) == claude_hand + 1


# ---------------------------------------------------------------------------
# Post-end phase chain (CR 104.2a)
# ---------------------------------------------------------------------------

class TestPostEndPhaseChain:
    def test_draw_step_never_mutates_after_game_end(self, game, make_card):
        from mtg.engine import GameEngine
        from mtg.constants import Phase
        engine = GameEngine(None)
        rick = game.players[0]
        for _ in range(3):
            rick.library.append(make_card("Island", type_line="Basic Land",
                                          power=None, toughness=None))
        game.turn_number = 5
        game.set_phase(Phase.UPKEEP, via="test")
        game.ended = True
        libs = len(rick.library)
        hand = len(rick.hand)
        engine.advance_phase(game)  # UPKEEP → DRAW, gated
        assert len(rick.library) == libs
        assert len(rick.hand) == hand

    def test_autoplay_chain_gates_between_advances(self):
        src = (REPO / "mtg" / "autoplay.py").read_text(encoding="utf-8")
        block = re.search(
            r"if game\.phase == Phase\.UNTAP:.{0,1200}?for _m in _p1 \+ _p2 \+ _p3:",
            src, re.S)
        assert block, "the UNTAP triple-advance block moved — re-anchor this pin"
        assert block.group(0).count("if not game.ended:") >= 2, (
            "an upkeep trigger can END the game mid-chain — each later "
            "advance needs its own gate")


# ---------------------------------------------------------------------------
# Targeting: if-tail strip, any-target at resolution, TargetType.CARD,
# graveyard-zone resolution re-check
# ---------------------------------------------------------------------------

PRISMATIC = ("Converge — Exile target nonland permanent if its mana value is "
             "less than or equal to the number of colors of mana spent to "
             "cast this spell.")


class TestTargetingFixes:
    def test_prismatic_ending_castable_with_creature_on_board(self, game,
                                                              make_card):
        from rules.targeting_helpers import _find_any_valid_target
        rick, claude = game.players
        claude.battlefield.append(make_card(
            "Monastery Swiftspear", type_line="Creature — Human Monk",
            power="1", toughness="2"))
        pe = make_card("Prismatic Ending", type_line="Sorcery",
                       oracle_text=PRISMATIC, power=None, toughness=None)
        assert _find_any_valid_target(game, pe, rick.name) is True

    def test_comet_storm_player_target_survives_resolution_parse(self,
                                                                 make_card):
        from rules.targeting_helpers import _parse_target_restriction_from_oracle
        from rules.targeting import TargetType
        comet = make_card(
            "Comet Storm", type_line="Instant",
            oracle_text="Multikicker {1}\nChoose any target, then choose "
                        "another target for each time this spell was kicked. "
                        "Comet Storm deals X damage to each of them.",
            power=None, toughness=None)
        r = _parse_target_restriction_from_oracle(comet)
        assert r is not None
        assert TargetType.PLAYER in r.target_types

    def test_target_card_in_graveyard_satisfiable(self, game, make_card):
        from rules.targeting_helpers import _find_any_valid_target
        rick, claude = game.players
        cling = make_card(
            "Cling to Dust", type_line="Instant",
            oracle_text="Exile target card from a graveyard. If it was a "
                        "creature card, you gain 3 life. Otherwise, you draw "
                        "a card.\nEscape—{B}{B}{R}{R}, Exile five other cards "
                        "from your graveyard.",
            power=None, toughness=None)
        assert _find_any_valid_target(game, cling, rick.name) is False, (
            "empty graveyards — no legal target")
        claude.graveyard.append(make_card("Massacre Wurm",
                                          type_line="Creature — Phyrexian Wurm",
                                          power="6", toughness="5"))
        assert _find_any_valid_target(game, cling, rick.name) is True

    def test_victimize_graveyard_target_does_not_fizzle(self, game, make_card):
        from rules.targeting_helpers import _check_resolution_targets
        rick, claude = game.players
        gm = make_card("Gray Merchant of Asphodel",
                       type_line="Creature — Zombie", power="2", toughness="4")
        claude.graveyard.append(gm)
        victimize = make_card(
            "Victimize", type_line="Sorcery",
            oracle_text="Choose two target creature cards in your graveyard. "
                        "Sacrifice a creature. If you do, return the chosen "
                        "cards to the battlefield tapped.",
            power=None, toughness=None)

        class _Entry:
            card = victimize
            target = gm
            controller_name = claude.name
        fizzled, why = _check_resolution_targets(game, _Entry())
        assert not fizzled, why


# ---------------------------------------------------------------------------
# "Any" tutor sentinel
# ---------------------------------------------------------------------------

class TestAnyTutorSentinel:
    def test_search_library_any_finds_a_card(self, game, rules, make_card):
        rick = game.players[0]
        rick.library.append(make_card("Massacre Wurm",
                                      type_line="Creature — Phyrexian Wurm",
                                      power="6", toughness="5"))
        msg = rules._execute_action_on_state(game, {
            "action": "search_library", "player": rick.name, "count": 1,
            "card_type": "Any", "to_zone": "graveyard",
            "reason": "Entomb"})
        assert rick.graveyard and rick.graveyard[0].name == "Massacre Wurm", (
            f"'Any' must mean NO filter, not a literal substring — got: {msg}")


# ---------------------------------------------------------------------------
# Meren experience: destroy path + same-batch watcher visibility
# ---------------------------------------------------------------------------

MEREN_ORACLE = ("Whenever another creature you control dies, you get an "
                "experience counter.\nAt the beginning of your end step, "
                "choose target creature card in your graveyard...")


class TestExperienceGaps:
    def _meren(self, make_card):
        return make_card("Meren of Clan Nel Toth",
                         type_line="Legendary Creature — Human Shaman",
                         power="3", toughness="4", oracle_text=MEREN_ORACLE)

    def test_destroy_action_grants_experience(self, game, rules, make_card):
        rick = game.players[0]
        rick.battlefield.append(self._meren(make_card))
        bird = make_card("Birds of Paradise", type_line="Creature — Bird",
                         power="0", toughness="1")
        rick.battlefield.append(bird)
        rules._execute_action_on_state(game, {"action": "destroy",
                                              "card": "Birds of Paradise"})
        assert getattr(rick, '_experience_counters', 0) == 1

    def test_same_batch_watcher_still_sees_batch_mates(self, game, make_card):
        """The Toxic Deluge shape: the SBA sweep removes EVERYONE from the
        battlefield first, then queues the batch — Meren must still see her
        batch-mates die (CR 603.10)."""
        from mtg.triggers import queue_deaths
        rick = game.players[0]
        meren = self._meren(make_card)
        zul = make_card("Zulaport Cutthroat", type_line="Creature — Human",
                        power="1", toughness="1")
        gm = make_card("Gray Merchant of Asphodel",
                       type_line="Creature — Zombie", power="2", toughness="4")
        # All three already OFF the battlefield (the sweep's post-removal
        # state) — order puts the watcher LAST so the battlefield scan alone
        # can never find her.
        queue_deaths(game, [(zul, rick), (gm, rick), (meren, rick)])
        assert getattr(rick, '_experience_counters', 0) == 2, (
            "two 'another creature' deaths in Meren's own death batch")


# ---------------------------------------------------------------------------
# Entwine (CR 702.42)
# ---------------------------------------------------------------------------

TOOTH_ORACLE = ("Choose one —\n• Search your library for up to two creature "
                "cards, reveal them, put them into your hand, then shuffle.\n"
                "• Put up to two creature cards from your hand onto the "
                "battlefield.\nEntwine {2} (Choose both if you pay the "
                "entwine cost.)")


class TestEntwine:
    def test_parse_entwine(self):
        from mtg.helpers import parse_entwine
        assert parse_entwine(TOOTH_ORACLE) == "{2}"
        assert parse_entwine("Flying") is None
        assert parse_entwine("Tooth and Nail (Choose both if you pay the "
                             "entwine cost.)") is None, (
            "reminder text without a brace cost must not match")

    def _tooth(self, make_card):
        return make_card("Tooth and Nail", type_line="Sorcery",
                         mana_cost="{5}{G}{G}", cmc=7,
                         oracle_text=TOOTH_ORACLE, power=None, toughness=None)

    def _forests(self, player, make_card, n):
        for _ in range(n):
            player.battlefield.append(make_card(
                "Forest", type_line="Basic Land — Forest",
                oracle_text="({T}: Add {G}.)", power=None, toughness=None))

    def test_entwine_paid_with_headroom(self, game, make_card):
        from mtg.engine import GameEngine
        from mtg.spells import _compute_alt_costs
        rick = game.players[0]
        self._forests(rick, make_card, 9)
        tooth = self._tooth(make_card)
        rick.hand.append(tooth)
        early, costs = _compute_alt_costs(GameEngine(None), game, rick, tooth,
                                          pay_mana=True, additional_cost=0)
        assert early is None
        assert tooth._entwined is True
        assert costs['effective_mana_cost'] == "{5}{G}{G}{2}"

    def test_no_entwine_at_exact_base_cost(self, game, make_card):
        from mtg.engine import GameEngine
        from mtg.spells import _compute_alt_costs
        rick = game.players[0]
        self._forests(rick, make_card, 7)
        tooth = self._tooth(make_card)
        rick.hand.append(tooth)
        early, costs = _compute_alt_costs(GameEngine(None), game, rick, tooth,
                                          pay_mana=True, additional_cost=0)
        assert early is None
        assert tooth._entwined is False
        assert costs['effective_mana_cost'] == "{5}{G}{G}"

    def test_template_entwined_gets_both_modes(self):
        actions = _lib()._gen_tooth_and_nail("Rick", "Claude",
                                             {"entwined": True})
        assert actions == [
            {"action": "search_library", "player": "Rick", "count": 2,
             "card_type": "creature", "to_zone": "hand",
             "reason": "Tooth and Nail (entwined): search two creatures to hand"},
            {"action": "move_cards_from_hand", "player": "Rick", "count": 2,
             "card_type": "creature",
             "reason": "Tooth and Nail (entwined): put two hand creatures onto battlefield"},
        ]

    def test_template_unentwined_one_mode_only(self, make_card):
        # No creatures in hand → search to HAND, never battlefield.
        actions = _lib()._gen_tooth_and_nail("Rick", "Claude",
                                             {"entwined": False,
                                              "controller_hand": []})
        assert all(a.get("to_zone") != "battlefield" for a in actions)
        # A held creature → put from hand onto the battlefield.
        hoof = make_card("Craterhoof Behemoth", type_line="Creature — Beast",
                         power="5", toughness="5")
        actions = _lib()._gen_tooth_and_nail(
            "Rick", "Claude", {"entwined": False, "controller_hand": [hoof]})
        assert actions == [{"action": "move_card",
                            "card": "Craterhoof Behemoth",
                            "from_zone": "hand", "to_zone": "battlefield",
                            "player": "Rick"}]


# ---------------------------------------------------------------------------
# PW token fallback: type line follows the descriptor
# ---------------------------------------------------------------------------

class TestPwTokenTypeLine:
    def test_vehicle_token_is_not_a_creature(self, game, make_card):
        from rules.planeswalker import PlaneswalkerManager
        rick = game.players[0]
        chandra = make_card(
            "Chandra, Spark Hunter",
            type_line="Legendary Planeswalker — Chandra",
            oracle_text="0: Create a 3/2 colorless Vehicle artifact token "
                        "with crew 1.",
            power=None, toughness=None)
        chandra.loyalty_counters = 4
        chandra.summoning_sick = False
        rick.battlefield.append(chandra)
        result = asyncio.run(
            PlaneswalkerManager().activate(game, rick, chandra, 0))
        assert result.success
        token = rick.battlefield[-1]
        assert "Vehicle" in token.type_line
        assert "Creature" not in token.type_line, token.type_line
        assert not token.is_creature(), (
            "an un-crewed Vehicle attacked in game_1533272987539734779 — "
            "CR 301.6: not a creature until crewed")


# ---------------------------------------------------------------------------
# Life from the Loam template
# ---------------------------------------------------------------------------

class TestLifeFromTheLoam:
    def test_returns_up_to_three_lands(self, make_card):
        gy = [make_card("Swamp", type_line="Basic Land — Swamp",
                        power=None, toughness=None) for _ in range(4)]
        gy.append(make_card("Bolt", type_line="Instant",
                            power=None, toughness=None))
        actions = _lib()._gen_life_from_the_loam(
            "Claude", "Rick", {"controller_graveyard": gy})
        assert len(actions) == 3
        assert all(a["action"] == "move_card" and a["to_zone"] == "hand"
                   for a in actions)

    def test_no_lands_resolves_none_chosen(self):
        actions = _lib()._gen_life_from_the_loam(
            "Claude", "Rick", {"controller_graveyard": []})
        assert actions and actions[0]["action"] == "no_action"


# ---------------------------------------------------------------------------
# Advertisement one-tap ceiling + feedback/message source pins
# ---------------------------------------------------------------------------

class TestAdvertisementCeiling:
    def test_solitude_not_offered_off_three_sources(self, game, make_card):
        """The live shape: two W/U duals + one mono-U — mana_by_color sums
        to 5 but only 3 physical taps exist."""
        from mtg.legal_actions import castable_entries
        rick = game.players[0]
        for name in ("Glacial Fortress", "Celestial Colonnade"):
            rick.battlefield.append(make_card(
                name, type_line="Land", oracle_text="({T}: Add {W} or {U}.)",
                power=None, toughness=None))
        rick.battlefield.append(make_card(
            "Otawara", type_line="Legendary Land",
            oracle_text="({T}: Add {U}.)", power=None, toughness=None))
        rick.hand.append(make_card("Solitude",
                                   type_line="Creature — Elemental Incarnation",
                                   mana_cost="{3}{W}{W}", cmc=5))
        rick.hand.append(make_card("Bear", mana_cost="{1}{W}", cmc=2))
        mana = {'W': 2, 'U': 3, 'B': 0, 'R': 0, 'G': 0, 'C': 0}
        entries = castable_entries(game, rick, mana, 0, 5)
        labels = [e["label"] for e in entries]
        assert not any("Solitude" in l for l in labels), labels
        assert any("Bear" in l for l in labels)


class TestFeedbackAndMessageWiring:
    def test_resolution_decline_feeds_rejection_loop(self):
        src = (REPO / "mtg" / "spells.py").read_text(encoding="utf-8")
        assert "resolved with no effect —" in src, (
            "the conditional-not-met branch must append to "
            "_recent_plan_rejections (the double-Searing-Blaze repeat)")

    def test_pw_double_activation_message_needs_real_enabler(self):
        src = (REPO / "mtg" / "ai_turn.py").read_text(encoding="utf-8")
        assert "Oath of Teferi / similar" not in src, (
            "the confident pre-check lie is back")
        assert "_has_pw_enabler" in src
