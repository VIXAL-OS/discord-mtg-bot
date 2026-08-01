"""Aug 1, 2026 — the batch-12 deferred slate, implemented same day.

- D1 stale-.attacking structural net: combat state never legitimately
  survives a turn boundary, yet flags demonstrably leaked (the Battalion
  over-fire off a surviving Cavalry Pegasus, game_1532756674203619470).
  Two identified sources fixed (the create-token-attacking path never
  joined game.attackers; the attack ACTION branch can mark state with no
  resolution following) plus an end_turn sweep that kills every leak
  shape whatever its origin. The Pegasus game's exact origin was
  unconfirmable from logs — the net covers it regardless.

- D2 escape-commander redirect: CR 903.9a's redirect is a MAY and autoplay
  always took it, so Kroxa could never reach the graveyard he escapes
  from (hardcast tax 2/4/6/8 for one discard each). Escape commanders now
  decline the DEATH->graveyard redirect at the destroy / sacrifice / SBA
  sites; exile/hand/library redirects unchanged.

- D3 Wheel of Misfortune: was a Tier-2 half-capture (caster discards, NO
  draw, no damage anywhere). Deterministic secret-number model (Mana
  Crypt hash convention): caster picks high (5-7), takes it, wheels;
  opponent picks low, keeps hand.

- D4 kicker (CR 702.33): parse + additive cost branch in
  _compute_alt_costs (appends kicker pips to effective_mana_cost so the
  payment tap pays COLORED kicker pips) + declared Card._kicked stamped
  when paid + ctx['kicked'] surfaced as truth. v1 gate: kick whenever
  affordable. Free/madness casts never kick. The Gatekeeper/Rite/Into the
  Roil mana-paid guesses survive only for card-less ctx shapes.
"""
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

KROXA_ORACLE = (
    "When this creature enters or attacks, each opponent discards a card, "
    "then each opponent who didn't discard a nonland card this way loses "
    "3 life.\nWhen this creature enters, sacrifice it unless it escaped.\n"
    "Escape—{B}{B}{R}{R}, Exile five other cards from your graveyard."
)


def _lib():
    from rules.effect_templates import get_effect_library
    return get_effect_library()


# ---------------------------------------------------------------------------
# D1: the stale-flag structural net
# ---------------------------------------------------------------------------

class TestCombatStateSweep:
    def test_end_turn_clears_leaked_attacking_flags(self, rules, game, make_card):
        from mtg.engine import GameEngine
        engine = GameEngine(None)
        engine.rules = rules
        rick = game.players[0]
        pegasus = make_card("Cavalry Pegasus")
        pegasus.attacking = True  # leaked from an earlier combat, any origin
        pegasus.attacking_player = 1
        rick.battlefield.append(pegasus)
        game.attackers = ["stale-id-from-nowhere"]
        engine.end_turn(game)
        assert pegasus.attacking is False
        assert pegasus.attacking_player is None
        assert game.attackers == []

    def test_attacking_token_joins_game_attackers(self, rules, game):
        from mtg.actions import execute_action_on_state
        execute_action_on_state(rules, game, {
            "action": "create_token", "player": "Rick",
            "name": "Soldier", "power": 1, "toughness": 1,
            "types": "Token Creature - Soldier", "count": 1,
            "attacking": True})
        rick = game.players[0]
        token = next(c for c in rick.battlefield if c.name == "Soldier")
        assert token.attacking is True
        assert token.id in game.attackers, \
            "an attacking token must JOIN the combat list — the id-based " \
            "clears and the damage loop both iterate game.attackers"


# ---------------------------------------------------------------------------
# D2: escape commanders decline the graveyard redirect
# ---------------------------------------------------------------------------

class TestEscapeCommanderRedirect:
    def test_helper_distinguishes_escape_commanders(self, make_card):
        from mtg.helpers import commander_declines_graveyard_redirect
        kroxa = make_card("Kroxa, Titan of Death's Hunger",
                          oracle_text=KROXA_ORACLE)
        vanilla = make_card("Surrak Dragonclaw",
                            oracle_text="Flash\nThis creature can't be "
                                        "countered.")
        assert commander_declines_graveyard_redirect(kroxa) is True
        assert commander_declines_graveyard_redirect(vanilla) is False

    def test_sacrificed_escape_commander_stays_in_graveyard(self, rules, game, make_card):
        from mtg.actions import execute_action_on_state
        game.format = "commander"
        rick = game.players[0]
        kroxa = make_card("Kroxa, Titan of Death's Hunger",
                          oracle_text=KROXA_ORACLE)
        kroxa.is_commander = True
        kroxa.owner_index = 0
        rick.battlefield.append(kroxa)
        execute_action_on_state(rules, game, {
            "action": "sacrifice_permanent", "player": "Rick",
            "card": "Kroxa, Titan of Death's Hunger"})
        assert kroxa in rick.graveyard, \
            "escape commander declines the CR 903.9a redirect — the " \
            "graveyard IS the escape-enabling zone"
        assert kroxa not in (rick.command_zone or [])

    def test_sacrificed_vanilla_commander_still_redirects(self, rules, game, make_card):
        from mtg.actions import execute_action_on_state
        game.format = "commander"
        rick = game.players[0]
        surrak = make_card("Surrak Dragonclaw",
                           oracle_text="Flash\nThis creature can't be "
                                       "countered.")
        surrak.is_commander = True
        surrak.owner_index = 0
        rick.battlefield.append(surrak)
        execute_action_on_state(rules, game, {
            "action": "sacrifice_permanent", "player": "Rick",
            "card": "Surrak Dragonclaw"})
        assert surrak in (rick.command_zone or []), \
            "non-escape commanders keep the redirect"


# ---------------------------------------------------------------------------
# D3: Wheel of Misfortune deterministic model
# ---------------------------------------------------------------------------

class TestWheelOfMisfortune:
    def test_caster_takes_high_number_and_wheels(self):
        lib = _lib()
        actions = lib._gen_wheel_of_misfortune("Rick", "Claude",
                                               {"turn_number": 6})
        kinds = [a["action"] for a in actions]
        assert kinds == ["deal_damage", "discard", "draw_cards"]
        dmg = actions[0]
        assert dmg["target_player"] == "Rick" and 5 <= dmg["amount"] <= 7
        assert actions[1] == {"action": "discard", "player": "Rick",
                              "card": "all"}
        assert actions[2] == {"action": "draw_cards", "player": "Rick",
                              "amount": 7}
        # The opponent chose lowest: no damage, keeps their hand.
        assert not any(a.get("player") == "Claude"
                       or a.get("target_player") == "Claude"
                       for a in actions)

    def test_deterministic_per_turn(self):
        lib = _lib()
        a1 = lib._gen_wheel_of_misfortune("Rick", "Claude", {"turn_number": 9})
        a2 = lib._gen_wheel_of_misfortune("Rick", "Claude", {"turn_number": 9})
        assert a1 == a2, "the Mana Crypt hash convention: reproducible"


# ---------------------------------------------------------------------------
# D4: kicker
# ---------------------------------------------------------------------------

class TestKickerParse:
    def test_parse_shapes(self):
        from mtg.helpers import parse_kicker
        assert parse_kicker("Kicker {B} (You may pay an additional {B} as "
                            "you cast this spell.)\nWhen this creature "
                            "enters, if it was kicked, ...") == "{B}"
        assert parse_kicker("Kicker {1}{W}\nSome effect.") == "{1}{W}"
        assert parse_kicker("Kicker {5}") == "{5}"
        # Multikicker is out of v1 scope — must NOT match as plain kicker.
        assert parse_kicker("Multikicker {1} (You may pay an additional "
                            "{1} any number of times...)") is None
        # Condition text alone ("was kicked") never matches.
        assert parse_kicker("If this spell was kicked, draw a card.") is None
        assert parse_kicker("") is None


class TestKickerCostBranch:
    def _gatekeeper(self, make_card):
        return make_card(
            "Gatekeeper of Malakir", type_line="Creature - Vampire Warrior",
            mana_cost="{B}{B}", cmc=2,
            oracle_text="Kicker {B} (You may pay an additional {B} as you "
                        "cast this spell.)\nWhen this creature enters, if "
                        "it was kicked, target player sacrifices a creature.",
            power=2, toughness=2)

    def _swamps(self, player, make_card, n):
        for _ in range(n):
            player.battlefield.append(make_card(
                "Swamp", type_line="Basic Land - Swamp",
                oracle_text="({T}: Add {B}.)", power=None, toughness=None))

    def test_kicks_when_affordable(self, game, make_card):
        from mtg.engine import GameEngine
        from mtg.spells import _compute_alt_costs
        rick = game.players[0]
        self._swamps(rick, make_card, 3)
        gk = self._gatekeeper(make_card)
        rick.hand.append(gk)
        early, costs = _compute_alt_costs(
            GameEngine(None), game, rick, gk, pay_mana=True,
            additional_cost=0)
        assert early is None
        assert gk._kicked is True
        assert costs['effective_mana_cost'] == "{B}{B}{B}"
        assert costs['total_cost'] == 3
        assert rick.tap_sources_for_cost("{B}{B}{B}", game=game), \
            "the kicked string must be PAYABLE — colored kicker pips " \
            "included, not just a bumped generic total"

    def test_does_not_kick_when_unaffordable(self, game, make_card):
        from mtg.engine import GameEngine
        from mtg.spells import _compute_alt_costs
        rick = game.players[0]
        self._swamps(rick, make_card, 2)  # exactly the base cost
        gk = self._gatekeeper(make_card)
        rick.hand.append(gk)
        early, costs = _compute_alt_costs(
            GameEngine(None), game, rick, gk, pay_mana=True,
            additional_cost=0)
        assert early is None
        assert gk._kicked is False
        assert costs['effective_mana_cost'] == "{B}{B}"
        assert costs['total_cost'] == 2

    def test_free_cast_never_kicks(self, game, make_card):
        from mtg.engine import GameEngine
        from mtg.spells import _compute_alt_costs
        rick = game.players[0]
        self._swamps(rick, make_card, 5)
        gk = self._gatekeeper(make_card)
        rick.hand.append(gk)
        early, costs = _compute_alt_costs(
            GameEngine(None), game, rick, gk, pay_mana=False,
            additional_cost=0)
        assert early is None
        assert gk._kicked is False


class TestKickedTruthBeatsGuess:
    def test_gatekeeper_truth_false_overrides_mana_guess(self):
        # The whole point of the stamp: commander tax / cost increases
        # inflate mana_paid, and the old >=3 guess read that as "kicked".
        # ctx INCLUDES a sacrificable creature so the two gate outcomes
        # DIVERGE (mutation lesson: without one, both branches no_action
        # and the pin passes for the wrong reason).
        lib = _lib()
        template = lib._card_templates["gatekeeper of malakir"]
        actions = template.action_generator(
            "Rick", "Claude",
            {"kicked": False, "mana_paid_total": 9,
             "worst_opponent_creature": "Grizzly Bears"})
        assert actions[0]["action"] == "no_action", \
            "stamped truth (not kicked) must beat the mana-paid guess"
        assert "not kicked" in actions[0]["reason"]

    def test_gatekeeper_truth_true_fires(self):
        lib = _lib()
        template = lib._card_templates["gatekeeper of malakir"]
        # ctx shaped the way the live builder feeds _force_sacrifice_creature
        # (it reads worst/best_opponent_creature, not battlefield objects).
        actions = template.action_generator(
            "Rick", "Claude",
            {"kicked": True, "worst_opponent_creature": "Grizzly Bears"})
        assert actions and actions[0]["action"] == "sacrifice_permanent"

    def test_guess_survives_for_cardless_ctx(self):
        lib = _lib()
        template = lib._card_templates["gatekeeper of malakir"]
        actions = template.action_generator(
            "Rick", "Claude", {"mana_paid_total": 1})
        assert actions[0]["action"] == "no_action"


# ---------------------------------------------------------------------------
# D5: opponent-cast triggers get the stack push + priority window (CR 603.3)
# ---------------------------------------------------------------------------

import asyncio


EIDOLON_ORACLE = ("Whenever a player casts a spell with mana value 3 or "
                  "less, this creature deals 2 damage to that player.")
STIFLE_ORACLE = "Counter target activated or triggered ability."


def _engine():
    from mtg.engine import GameEngine
    return GameEngine(None)


class TestOppCastTriggerWindow:
    def _setup(self, make_game, make_card, stifle_in_hand):
        game = make_game()
        rick, claude = game.players
        game.active_player_index = 0
        game.stack_enabled = True

        async def _sink(msg):
            return None
        game._stack_send_func = _sink

        eidolon = make_card("Eidolon of the Great Revel",
                            type_line="Enchantment Creature - Spirit",
                            power=2, toughness=2,
                            oracle_text=EIDOLON_ORACLE)
        claude.battlefield.append(eidolon)
        if stifle_in_hand:
            rick.hand.append(make_card(
                "Stifle", type_line="Instant", mana_cost="{U}", cmc=1,
                power=None, toughness=None, oracle_text=STIFLE_ORACLE))
        spell = make_card("Lava Spike", type_line="Sorcery",
                          mana_cost="{R}", cmc=1, power=None, toughness=None,
                          oracle_text="Lava Spike deals 3 damage to target "
                                      "player or planeswalker.")
        return game, rick, spell

    def test_fast_path_without_stifle_no_push(self, make_game, make_card, capsys):
        from mtg.triggers import _check_cast_triggers
        game, rick, spell = self._setup(make_game, make_card,
                                        stifle_in_hand=False)
        life_before = rick.life
        asyncio.run(_check_cast_triggers(_engine(), game, rick, spell))
        out = capsys.readouterr().out
        assert "[OPP-CAST-TRIGGER]" in out, "the trigger itself must fire"
        assert "[OPP-CAST-TRIGGER-STACK]" not in out, \
            "no Stifle-shape in the caster's hand = the inline fast path " \
            "(zero overhead on the highest-volume Tier-1 path)"
        assert rick.life == life_before - 2, "Eidolon's ping applied inline"

    def test_window_path_with_stifle_pushes_and_still_resolves(
            self, make_game, make_card, capsys):
        from mtg.triggers import _check_cast_triggers
        game, rick, spell = self._setup(make_game, make_card,
                                        stifle_in_hand=True)
        life_before = rick.life
        asyncio.run(_check_cast_triggers(_engine(), game, rick, spell))
        out = capsys.readouterr().out
        assert "[OPP-CAST-TRIGGER-STACK]" in out, \
            "a Stifle-shape in the CASTER's hand must push the trigger on " \
            "the stack with a response window (CR 603.3)"
        assert rick.life == life_before - 2, \
            "uncountered trigger still resolves after the window"
        assert not game.stack, \
            "the trigger entry must be POPPED after inline resolution — " \
            "no phantom entries (the May 19 desync class)"


# ---------------------------------------------------------------------------
# D6: extra combat phases — producers, action, and turn-boundary discipline
# ---------------------------------------------------------------------------

class TestAdditionalCombat:
    def test_action_increments_pending_counter(self, rules, game):
        from mtg.actions import execute_action_on_state
        assert getattr(game, '_additional_combats', 0) == 0
        msg = execute_action_on_state(rules, game, {
            "action": "additional_combat", "source": "Port Razer"})
        assert game._additional_combats == 1
        assert "Port Razer" in msg and "additional combat" in msg

    def test_karlach_grants_the_extra_combat(self):
        lib = _lib()
        template = lib._attack_templates["karlach, fury of avernus"]
        actions = template.action_generator("Rick", "Claude", {})
        kinds = [a["action"] for a in actions]
        assert "additional_combat" in kinds, \
            "Karlach's attack trigger grants the extra combat now " \
            "(was a not-modeled breadcrumb)"
        assert "grant_keywords" in kinds  # the first-strike rider survives

    def test_end_turn_discards_unconsumed_extra_combats(self, rules, game, make_card):
        # The stale-value leak: a Moraug/Port Razer firing on one player's
        # turn must never grant the NEXT player a phantom combat via the
        # autoplay human loop's read.
        from mtg.engine import GameEngine
        engine = GameEngine(None)
        engine.rules = rules
        game._additional_combats = 2
        engine.end_turn(game)
        assert game._additional_combats == 0

    def test_claude_path_breadcrumbs_and_resets(self):
        # Source pin: the Claude turn path must make the drop VISIBLE and
        # reset the counter (audits see the gap; the other player never
        # inherits it). Consumption on that path is the documented gap.
        src = (REPO / "mtg" / "ai_turn.py").read_text(encoding="utf-8")
        assert "[EXTRA-COMBAT]" in src
        idx = src.index("[EXTRA-COMBAT]")
        assert "game._additional_combats = 0" in src[idx:idx + 800], \
            "the breadcrumb must be paired with the reset"
