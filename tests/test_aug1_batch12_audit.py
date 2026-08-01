"""Aug 1, 2026 batch-12 audit pins (batch game_15327*, sha=0bfcd57).

Inline-sweep findings, each with live batch evidence:

- B12-1 (explicit-X clamp): the AI supplies explicit X values off the
  advertised per-color availability, which double-counts OR-duals — Genesis
  Wave arrived with x=5 ("mana=10" advertised, one-tap ceiling 5), the
  [X-COST] line printed total=8, and the cast failed at payment with the
  plan's remaining actions skipped (game_1532744074447163402; Secure the
  Wastes hit the same shape). The batch-11 S2 fix budgeted only the
  AUTO-size branch; the explicit `card._x_value` branch had no clamp.
  _compute_alt_costs now clamps AI-requested X to the one-tap budget when
  the clamp leaves X >= 1 (at 0 the cast still fails with a recorded
  reason so the card stays in hand), pay_mana casts only.

- B12-2 (adventure combined cost in _get_action_error): a creature-half
  cast of an adventure card left effective_mana_cost as the cache's
  COMBINED "creature // adventure" string, so the colored-pip re-derivation
  counted both halves (a {6}{G} // {G} card would demand {G}{G}) and the
  [MANA-DIVERGENCE] trace lied about the requirement (Oakhame Ranger
  printed req={'other': 8} for a 4-pip half, game_1532756719195914282 —
  the PAYMENT engine itself was correct: "only 3 source(s) can pay
  {G/W}-compatible pips (4 needed)"). The error path now prices only the
  creature face. Fixture builds the card the way the LOADER does
  (combined mana_cost, face cmc) per the batch-11 reachability lesson.

- B12-3/4 (templates): Stromkirk Occultist and Drana, Liberator of
  Malakir each queued 6x to [COMBAT-TRIGGER-UNHANDLED] in the madness
  games — combat-damage triggers can never resolve via Tier 3 (judge.py's
  combat-shape guard). Both join the attack registry with the standard
  damage_dealt gate: Stromkirk impulse-exiles the top of the controller's
  OWN library (the Light Up the Stage playable-exile machinery); Drana
  puts a +1/+1 counter on each ATTACKING creature her controller controls.
"""
import asyncio
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


def _lib():
    from rules.effect_templates import get_effect_library
    return get_effect_library()


def _board(player, make_card, n_forests=4, n_islands=0, dual=True):
    """Board with basics + one OR-dual: one-tap total = basics + 1."""
    for _ in range(n_forests):
        player.battlefield.append(make_card(
            "Forest", type_line="Basic Land — Forest",
            oracle_text="({T}: Add {G}.)", power=None, toughness=None))
    for _ in range(n_islands):
        player.battlefield.append(make_card(
            "Island", type_line="Basic Land — Island",
            oracle_text="({T}: Add {U}.)", power=None, toughness=None))
    if dual:
        player.battlefield.append(make_card(
            "Temple Garden", type_line="Land — Forest Plains",
            oracle_text="({T}: Add {G} or {W}.)", power=None, toughness=None))


# ---------------------------------------------------------------------------
# B12-1: explicit AI-supplied X is clamped to the one-tap budget
# ---------------------------------------------------------------------------

class TestExplicitXClamp:
    def _wave(self, make_card):
        return make_card(
            "Genesis Wave", type_line="Sorcery",
            mana_cost="{X}{G}{G}{G}", cmc=3,
            oracle_text="Reveal the top X cards of your library. You may put "
                        "any number of permanent cards with mana value X or "
                        "less from among them onto the battlefield.",
            power=None, toughness=None)

    def test_oversized_explicit_x_is_clamped(self, game, make_card):
        from mtg.engine import GameEngine
        from mtg.spells import _compute_alt_costs
        rick = game.players[0]
        _board(rick, make_card, n_forests=4)  # one-tap total 5
        wave = self._wave(make_card)
        wave._x_value = 5  # the AI's ask: total 8 on a 5-source board
        rick.hand.append(wave)
        early, costs = _compute_alt_costs(
            GameEngine(None), game, rick, wave, pay_mana=True,
            additional_cost=0)
        assert early is None
        # 5 one-tap − 3 fixed pips = X of 2; the batch's shape failed at
        # payment instead and the turn's remaining plan was skipped.
        assert costs['x_value_chosen'] == 2
        assert costs['total_cost'] == 5
        assert rick.tap_sources_for_cost(
            "{X}{G}{G}{G}", x_value=costs['x_value_chosen'], game=game)

    def test_explicit_x_within_budget_is_honored(self, game, make_card):
        from mtg.engine import GameEngine
        from mtg.spells import _compute_alt_costs
        rick = game.players[0]
        _board(rick, make_card, n_forests=6)  # one-tap total 7
        wave = self._wave(make_card)
        wave._x_value = 3  # total 6 <= 7 — no clamp
        rick.hand.append(wave)
        early, costs = _compute_alt_costs(
            GameEngine(None), game, rick, wave, pay_mana=True,
            additional_cost=0)
        assert early is None
        assert costs['x_value_chosen'] == 3

    def test_zero_budget_does_not_clamp_to_zero(self, game, make_card):
        # One-tap == fixed pips → budget X of 0. Clamping to 0 would BURN
        # the card (an X=0 Genesis Wave does nothing); the correct outcome
        # is the requested X surviving so the cast fails with a recorded
        # reason and the card stays in hand.
        from mtg.engine import GameEngine
        from mtg.spells import _compute_alt_costs
        rick = game.players[0]
        _board(rick, make_card, n_forests=2, dual=True)  # one-tap total 3
        wave = self._wave(make_card)
        wave._x_value = 4
        rick.hand.append(wave)
        early, costs = _compute_alt_costs(
            GameEngine(None), game, rick, wave, pay_mana=True,
            additional_cost=0)
        assert early is None
        assert costs['x_value_chosen'] == 4  # untouched — cast will fail

    def test_free_cast_explicit_x_untouched(self, game, make_card):
        # pay_mana=False (cascade/free-cast plumbing) must not consult the
        # mana budget at all — behavior-preserving on that path.
        from mtg.engine import GameEngine
        from mtg.spells import _compute_alt_costs
        rick = game.players[0]  # NO board at all
        wave = self._wave(make_card)
        wave._x_value = 3
        rick.hand.append(wave)
        early, costs = _compute_alt_costs(
            GameEngine(None), game, rick, wave, pay_mana=False,
            additional_cost=0)
        assert early is None
        assert costs['x_value_chosen'] == 3


# ---------------------------------------------------------------------------
# B12-2: _get_action_error prices only the creature face of an adventure card
# ---------------------------------------------------------------------------

class TestAdventureErrorPathFaceCost:
    def _giant(self, make_card):
        # The LOADER shape: combined mana_cost string, face cmc, adventure
        # attributes set (the batch-11 pin-reachability lesson — never the
        # full-Scryfall shape the cache doesn't store).
        giant = make_card(
            "Beanstalk Giant", type_line="Creature — Giant",
            mana_cost="{6}{G} // {G}", cmc=7,
            oracle_text="Beanstalk Giant's power and toughness are each "
                        "equal to the number of lands you control.",
            power=0, toughness=0)
        giant.adventure_cost = "{G}"
        giant.adventure_name = "Fertile Footsteps"
        return giant

    def test_creature_half_does_not_double_count_pips(self, game, make_card):
        from mtg.ai_turn import _get_action_error
        from mtg.engine import GameEngine
        rick = game.players[0]
        # 7 mana total but exactly ONE green source: {6}{G} is payable.
        _board(rick, make_card, n_forests=1, n_islands=6, dual=False)
        giant = self._giant(make_card)
        rick.hand.append(giant)
        err = _get_action_error(
            GameEngine(None), game, 0,
            {"type": "cast", "card": "Beanstalk Giant"}) or ""
        # Pre-fix the combined string demanded {G} x2 and returned
        # "Need 2 {G} mana ... only have 1".
        assert "Need 2 {G}" not in err, err

    def test_adventure_half_still_prices_its_own_face(self, game, make_card):
        from mtg.ai_turn import _get_action_error
        from mtg.engine import GameEngine
        rick = game.players[0]
        _board(rick, make_card, n_forests=1, n_islands=6, dual=False)
        giant = self._giant(make_card)
        rick.hand.append(giant)
        err = _get_action_error(
            GameEngine(None), game, 0,
            {"type": "cast", "card": "Beanstalk Giant",
             "adventure": "Fertile Footsteps"}) or ""
        assert "Need 2 {G}" not in err, err


# ---------------------------------------------------------------------------
# B12-3/4: the batch-15327 refused-trigger tail templates
# ---------------------------------------------------------------------------

class TestBatch12AttackTemplates:
    def test_stromkirk_impulse_exiles_own_top(self, rules, game):
        actions = _lib()._gen_stromkirk_occultist(
            "Rick", "Claude", {"damage_dealt": 3})
        assert actions == [{"action": "exile_top_of_library", "player": "Rick",
                            "count": 1, "playable": True}]

    def test_drana_counters_only_attacking_creatures(self, game, make_card):
        attacker1 = make_card("Drana, Liberator of Malakir")
        attacker2 = make_card("Kalastria Highborn")
        bystander = make_card("Wall of Omens")
        attacker1.attacking = True
        attacker2.attacking = True
        bystander.attacking = False
        actions = _lib()._gen_drana_liberator(
            "Rick", "Claude",
            {"damage_dealt": 2,
             "controller_battlefield": [attacker1, attacker2, bystander]})
        named = sorted(a["card"] for a in actions)
        assert named == ["Drana, Liberator of Malakir", "Kalastria Highborn"]
        assert all(a["action"] == "add_counters"
                   and a["counter_type"] == "+1/+1"
                   and a["amount"] == 1 for a in actions)

    def test_damage_gate_holds_for_both(self):
        # The family invariant: a declare-time scan (no damage_dealt in ctx)
        # can never misfire a combat-damage template.
        lib = _lib()
        assert lib._gen_stromkirk_occultist("Rick", "Claude", {}) == []
        assert lib._gen_drana_liberator(
            "Rick", "Claude", {"controller_battlefield": []}) == []

    def test_registry_keys_present_and_unique(self):
        lib = _lib()
        assert "stromkirk occultist" in lib._attack_templates
        assert "drana, liberator of malakir" in lib._attack_templates
        # Bare-name keys must NOT also exist in the ETB registry (the
        # Kokusho/Solemn class): combat-damage triggers live in the attack
        # registry only.
        assert "stromkirk occultist" not in lib._card_templates
        assert "drana, liberator of malakir" not in lib._card_templates


# ---------------------------------------------------------------------------
# Reviewer wave (partner game): commander damage is per-COMMANDER (CR 903.10a)
# ---------------------------------------------------------------------------

class TestCommanderDamagePerCommander:
    def test_two_partners_accumulate_separately(self, rules, game, make_card):
        from mtg.combat import apply_combat_damage_to_player
        rick, claude = game.players
        thrasios = make_card("Thrasios, Triton Hero")
        tymna = make_card("Tymna the Weaver")
        thrasios.is_commander = True
        tymna.is_commander = True
        rick.battlefield.extend([thrasios, tymna])
        apply_combat_damage_to_player(rules, game, claude, 11, thrasios)
        apply_combat_damage_to_player(rules, game, claude, 10, tymna)
        assert claude.commander_damage.get("Thrasios, Triton Hero") == 11
        assert claude.commander_damage.get("Tymna the Weaver") == 10
        # The batch shape: keyed by controller index, this read 21/21 and
        # the SBA would have ruled a loss off two sub-lethal commanders.
        assert all(v < 21 for v in claude.commander_damage.values())

    def test_sba_no_loss_at_eleven_plus_ten(self, rules, game):
        claude = game.players[1]
        claude.commander_damage = {"Thrasios, Triton Hero": 11,
                                   "Tymna the Weaver": 10}
        from mtg.sba import process_state_based_actions
        process_state_based_actions(rules, game)
        assert not game.ended, "11+10 from two different commanders is NOT a loss"

    def test_sba_loss_at_twenty_one_from_one(self, rules, game):
        claude = game.players[1]
        claude.commander_damage = {"Tymna the Weaver": 21}
        from mtg.sba import process_state_based_actions
        msgs = process_state_based_actions(rules, game)
        assert game.ended, "21 from ONE commander is a loss (CR 903.10a)"
        assert any("Tymna the Weaver" in m for m in msgs)


# ---------------------------------------------------------------------------
# Reviewer wave (partner game): flicker resets planeswalker loyalty
# ---------------------------------------------------------------------------

class TestFlickerLoyaltyReset:
    def test_flickered_pw_reenters_at_printed_loyalty(self, rules, game, make_card):
        from mtg.actions import execute_action_on_state
        rick = game.players[0]
        ami = make_card("Aminatou, the Fateshifter",
                        type_line="Legendary Planeswalker - Aminatou",
                        power=None, toughness=None)
        ami.loyalty = 3
        ami.loyalty_counters = 7  # four prior [+1] activations
        rick.battlefield.append(ami)
        execute_action_on_state(rules, game, {
            "action": "flicker", "player": "Rick",
            "target": "Aminatou, the Fateshifter"})
        assert ami in rick.battlefield
        assert ami.loyalty_counters == 3, \
            "CR 306.6/400.7: a flickered planeswalker re-enters at printed loyalty"


# ---------------------------------------------------------------------------
# Reviewer wave (partner game): Restoration Angel never flickers non-creatures
# ---------------------------------------------------------------------------

class TestRestorationAngelCreatureOnly:
    def test_no_creature_means_decline_not_noncreature_fallback(self):
        lib = _lib()
        actions = lib._gen_restoration_angel(
            "Claude", "Rick",
            {"_source_card_name": "Restoration Angel",
             "explicit_target_name": "",
             "best_own_etb_creature": "",
             "best_own_noncreature": "Aminatou, the Fateshifter"})
        assert len(actions) == 1 and actions[0]["action"] == "no_action", \
            "printed ability targets a CREATURE only - the 'you may' declines"


# ---------------------------------------------------------------------------
# Reviewer wave (escape game): Vile Entomber tutors to the GRAVEYARD
# ---------------------------------------------------------------------------

class TestVileEntomberJson:
    def test_json_template_routes_to_graveyard(self):
        lib = _lib()
        actions, desc = lib.resolve_etb(
            "Vile Entomber",
            "When this creature enters, search your library for a card, "
            "put that card into your graveyard, then shuffle.",
            "Rick", "Claude")
        assert actions, "named JSON template must exist"
        assert actions[0]["action"] == "search_library"
        assert actions[0]["to_zone"] == "graveyard", \
            "printed text says graveyard - the generic pattern defaulted to hand"


# ---------------------------------------------------------------------------
# Reviewer wave (escape game): Yawgmoth's Will grants ALL card types
# ---------------------------------------------------------------------------

class TestGrantFlashbackAnyType:
    def test_any_type_includes_creatures_excludes_lands(self, rules, game, make_card):
        from mtg.actions import execute_action_on_state
        rick = game.players[0]
        bear = make_card("Bear Cub")
        bolt = make_card("Lightning Bolt", type_line="Instant",
                         power=None, toughness=None)
        swamp = make_card("Swamp", type_line="Basic Land - Swamp",
                          power=None, toughness=None)
        rick.graveyard.extend([bear, bolt, swamp])
        msg = execute_action_on_state(rules, game, {
            "action": "grant_flashback", "player": "Rick",
            "grant_all": True, "card_types": "any",
            "source": "Yawgmoth's Will"})
        assert bear.id in rick.playable_from_graveyard, \
            "'cast spells from your graveyard' has no type restriction"
        assert bolt.id in rick.playable_from_graveyard
        assert swamp.id not in rick.playable_from_graveyard, \
            "lands are not castable (play-lands half stays unmodeled)"
        assert msg and "2" in msg

    def test_default_stays_instant_sorcery_only(self, rules, game, make_card):
        from mtg.actions import execute_action_on_state
        rick = game.players[0]
        bear = make_card("Bear Cub II")
        bolt = make_card("Shock", type_line="Instant",
                         power=None, toughness=None)
        rick.graveyard.extend([bear, bolt])
        execute_action_on_state(rules, game, {
            "action": "grant_flashback", "player": "Rick",
            "grant_all": True, "source": "Past in Flames"})
        assert bolt.id in rick.playable_from_graveyard
        assert bear.id not in rick.playable_from_graveyard, \
            "Snapcaster/Past in Flames scope must NOT widen"


# ---------------------------------------------------------------------------
# Reviewer wave (partner game): Thrasios reveal-top pattern (activation-reachable)
# ---------------------------------------------------------------------------

class TestThrasiosRevealPattern:
    ORACLE = ("Scry 1, then reveal the top card of your library. If it's a "
              "land card, put it onto the battlefield tapped. Otherwise, "
              "draw a card.")

    def test_land_on_top_goes_to_battlefield_tapped(self, make_card):
        lib = _lib()
        island = make_card("Island", type_line="Basic Land - Island",
                           power=None, toughness=None)
        actions = lib._gen_reveal_top_land_or_draw(
            "Rick", "Claude", {"controller_library": [island]})
        assert actions == [{"action": "move_card", "card": "Island",
                            "from_zone": "library", "to_zone": "battlefield",
                            "player": "Rick", "tapped": True}]

    def test_nonland_on_top_draws(self, make_card):
        lib = _lib()
        spell = make_card("Opt", type_line="Instant", power=None, toughness=None)
        actions = lib._gen_reveal_top_land_or_draw(
            "Rick", "Claude", {"controller_library": [spell]})
        assert actions == [{"action": "draw_cards", "player": "Rick", "amount": 1}]

    def test_pattern_matches_thrasios_oracle_on_activation(self):
        lib = _lib()
        actions, desc = lib.resolve_etb(
            "Thrasios, Triton Hero", self.ORACLE, "Rick", "Claude",
            event_type="activated",
            game_context={"controller_library": []})
        assert actions is not None, \
            "the pattern must be reachable from event_type='activated'"


# ---------------------------------------------------------------------------
# Reviewer wave (escape game): typed sacrifice costs are recognized
# ---------------------------------------------------------------------------

class TestTypedSacrificeCost:
    def test_satisfies_typed_costs(self, game, make_card):
        from mtg.engine import _satisfies_sacrifice_cost
        station = make_card("Grinding Station", type_line="Artifact",
                            power=None, toughness=None)
        bear = make_card("Grizzly Bears")
        assert _satisfies_sacrifice_cost(
            station, "{t}, sacrifice an artifact: target player mills three cards.",
            game)
        assert not _satisfies_sacrifice_cost(
            bear, "{t}, sacrifice an artifact: target player mills three cards.",
            game)
        assert not _satisfies_sacrifice_cost(
            station, "sacrifice a land: add one mana of any color.", game)
