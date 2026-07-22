"""July 20 audit — regression pins for the July 16 verification batch.

Each test names the game that motivated it. The headline bug: the July 11
declared-target validation (commit 97496b5) had no carve-out for spells ON
THE STACK, so every explicit-target counterspell response in the batch
fizzled at cast with "is not a valid target type" (game_1527451774888317038:
Pact of Negation vs Meren; ~25 failed counter attempts batch-wide across
Counterspell / Mana Drain / Dissolve / Negate / Swan Song / Fierce
Guardianship). The June 11 corpus had zero such failures — pure regression.
"""
import asyncio
from types import SimpleNamespace

import pytest

from mtg.constants import Phase


def _engine():
    from mtg.engine import GameEngine
    return GameEngine(None)


def _ready(game, active_idx=0):
    game.phase = Phase.MAIN1
    game.active_player_index = active_idx
    return game


def _islands(make_card, n):
    return [make_card(f"Island {i}", type_line="Basic Land — Island",
                      power="0", toughness="0") for i in range(n)]


class TestCounterspellStackTarget:
    def test_counterspell_declaring_a_stack_spell_passes_the_cast_gate(
            self, make_game, make_card):
        # game_1527451774888317038: Rick's Pact of Negation response targeting
        # Meren (a spell ON the stack, forwarded by mtg/engine.py's response
        # path) was rejected by the battlefield-oriented declared-target
        # validator: "Meren of Clan Nel Toth is not a valid target type".
        from mtg.models import StackEntry
        from mtg.spells import cast_spell_async

        game = _ready(make_game())
        rick, claude = game.players
        rick.battlefield.extend(_islands(make_card, 3))

        meren = make_card("Meren of Clan Nel Toth",
                          type_line="Legendary Creature — Human Shaman",
                          mana_cost="{2}{B}{G}", cmc=4)
        game.stack.append(StackEntry(card=meren, controller_name=claude.name,
                                     controller_index=1))

        cs = make_card("Counterspell", type_line="Instant",
                       oracle_text="Counter target spell.",
                       mana_cost="{U}{U}", cmc=2, power="0", toughness="0")
        rick.hand.append(cs)

        ok, msg, _ = asyncio.run(cast_spell_async(
            _engine(), game, rick, cs, target=meren))

        assert "not a valid target type" not in (msg or "")
        assert ok is True, msg


class TestPlaneswalkerAbilityDamage:
    def test_pw_ability_damage_to_planeswalker_removes_loyalty(
            self, make_game, make_card):
        # game_1527458364957786138: Wrenn and Six's [-1] hit Jace, the Mind
        # Sculptor; every Card carries both damage_marked and loyalty_counters
        # fields, so the hasattr() dispatch in rules/planeswalker.py routed
        # the PW target into the creature branch — damage was marked, loyalty
        # never deducted (CR 306.8: damage to a planeswalker removes that many
        # loyalty counters).
        from rules.planeswalker import (AbilityType, PlaneswalkerAbility,
                                        PlaneswalkerManager)

        game = make_game()
        rick, claude = game.players
        wrenn = make_card("Wrenn and Six",
                          type_line="Legendary Planeswalker — Wrenn",
                          power=None, toughness=None, loyalty="3",
                          oracle_text="")
        wrenn.loyalty_counters = 3
        rick.battlefield.append(wrenn)
        jace = make_card("Jace, the Mind Sculptor",
                         type_line="Legendary Planeswalker — Jace",
                         power=None, toughness=None, loyalty="3",
                         oracle_text="")
        jace.loyalty_counters = 5
        claude.battlefield.append(jace)

        ability = PlaneswalkerAbility(
            index=1, loyalty_cost=-1,
            ability_type=AbilityType.LOYALTY_MINUS,
            text="Wrenn and Six deals 1 damage to any target.")
        messages = asyncio.run(PlaneswalkerManager()._execute_ability(
            game, rick, wrenn, ability, [jace]))

        assert jace.loyalty_counters == 4, messages
        # The old creature-branch misroute marked damage instead.
        assert jace.damage_marked == 0, messages


class TestYorionDelayedReturn:
    def test_template_requests_delayed_return(self, lib):
        actions, _ = lib.resolve_etb(
            "Yorion, Sky Nomad",
            "When Yorion enters, exile any number of other nonland permanents "
            "you own and control. Return those cards to the battlefield at "
            "the beginning of the next end step.",
            "Rick", "Claude")
        assert actions[0]["action"] == "mass_flicker"
        assert actions[0].get("delayed_return") is True

    def test_mass_flicker_delayed_return_exiles_until_end_step(
            self, rules, game, make_card):
        # game_1527451733679149057: Yorion's return resolved IMMEDIATELY, so
        # Yorion ↔ Felidar Guardian re-triggered each other inside one
        # resolution (204 flickers, 359 mills, duplicate-milled cards in
        # identical order — impossible under CR 121) and decked the opponent.
        # CR 603.7: the return is a delayed trigger at the next end step.
        from mtg.actions import execute_action_on_state

        rick = game.players[0]
        mulldrifter = make_card("Mulldrifter", oracle_text="Flying\nWhen "
                                "Mulldrifter enters, draw two cards.")
        rick.battlefield.append(mulldrifter)

        execute_action_on_state(rules, game, {
            "action": "mass_flicker", "player": "Rick", "count": 5,
            "exclude_lands": True, "exclude_self": "Yorion, Sky Nomad",
            "delayed_return": True})

        assert mulldrifter not in rick.battlefield
        assert mulldrifter in rick.exile
        assert len(game.delayed_triggers) == 1
        delayed = game.delayed_triggers[0]
        assert delayed["trigger_at"] == "end_step"
        assert delayed["actions"][0]["from_zone"] == "exile"
        assert delayed["actions"][0]["to_zone"] == "battlefield"

    def test_mass_flicker_without_flag_still_returns_immediately(
            self, rules, game, make_card):
        # Brago-class flickers ("exile ... then return") keep the old shape.
        from mtg.actions import execute_action_on_state

        rick = game.players[0]
        bear = make_card("Charging Bear")
        rick.battlefield.append(bear)

        execute_action_on_state(rules, game, {
            "action": "mass_flicker", "player": "Rick", "count": 5})

        assert bear in rick.battlefield
        assert bear not in rick.exile


class TestOneTapManaTotal:
    def _esper_manabase(self, make_card):
        # The exact untapped set from game_1527451728084074550 at the Sun
        # Titan failure: 5 physical sources whose per-color sums display as
        # W:3 U:5 B:3 C:1 (12 "available") because every OR-dual counts
        # toward each of its colors.
        return [
            make_card("Test Sanctum", type_line="Land",
                      oracle_text="{T}: Add {W}, {U}, or {B}.",
                      power="0", toughness="0"),
            make_card("Test River", type_line="Land",
                      oracle_text="{T}: Add {C}.\n{T}: Add {U} or {B}.",
                      power="0", toughness="0"),
            make_card("Test Stream", type_line="Land",
                      oracle_text="{T}: Add {W} or {U}.",
                      power="0", toughness="0"),
            make_card("Test Clouds", type_line="Land",
                      oracle_text="{T}: Add {W} or {U}.",
                      power="0", toughness="0"),
            make_card("Test Grave", type_line="Land",
                      oracle_text="{T}: Add {U} or {B}.",
                      power="0", toughness="0"),
        ]

    def test_six_mana_spell_not_payable_off_five_dual_sources(
            self, game, make_card):
        # Sun Titan ({4}{W}{W}) was advertised as castable off this board and
        # the AI burned whole main phases retrying it; tap_sources_for_cost
        # correctly refused (a source taps once), so the availability check
        # must refuse too.
        rick = game.players[0]
        rick.battlefield.extend(self._esper_manabase(make_card))

        can, reason = rick.can_pay_mana_cost("{4}{W}{W}")

        assert can is False
        assert "5" in reason and "6" in reason

    def test_five_mana_spell_still_payable_off_five_dual_sources(
            self, game, make_card):
        rick = game.players[0]
        rick.battlefield.extend(self._esper_manabase(make_card))

        can, reason = rick.can_pay_mana_cost("{3}{W}{W}")

        assert can is True, reason
        assert rick.tap_sources_for_cost("{3}{W}{W}") is True


class TestGraveyardTargetResolution:
    def test_animate_dead_reanimates_the_declared_graveyard_target(
            self, make_game, make_card):
        # game_1527451728084074550: the AI declared Gonti, but the cast-target
        # resolver only searched stack/battlefield/players — the declared
        # target silently dropped to None and the reanimation fallback took
        # the highest-power creature in ANY graveyard (the opponent's Sun
        # Titan) instead. CR 608.2b: a spell affects the object it targeted.
        from mtg.engine import GameEngine

        game = _ready(make_game())
        rick, claude = game.players
        rick.battlefield.extend(
            make_card(f"Swamp {i}", type_line="Basic Land — Swamp",
                      power="0", toughness="0") for i in range(3))
        animate = make_card(
            "Animate Dead", type_line="Enchantment — Aura",
            mana_cost="{1}{B}", cmc=2, power=None, toughness=None,
            oracle_text="Enchant creature card in a graveyard\n"
                        "When Animate Dead enters, ... return enchanted "
                        "creature card to the battlefield under your control")
        rick.hand.append(animate)
        gonti = make_card("Gonti, Lord of Luxury",
                          type_line="Legendary Creature — Aetherborn Rogue",
                          power="2", toughness="3")
        rick.graveyard.append(gonti)
        titan = make_card("Sun Titan", type_line="Creature — Giant",
                          power="6", toughness="6")
        claude.graveyard.append(titan)

        asyncio.run(GameEngine(None)._execute_action(game, 0, {
            "type": "cast", "card": "Animate Dead",
            "target": "Gonti, Lord of Luxury"}))

        assert gonti in rick.battlefield
        assert titan in claude.graveyard
        assert titan not in rick.battlefield


class TestPrintedAlternateCosts:
    def _fow(self, make_card):
        return make_card(
            "Force of Will", type_line="Instant",
            mana_cost="{3}{U}{U}", cmc=5, power="0", toughness="0",
            oracle_text="You may pay 1 life and exile a blue card from your "
                        "hand rather than pay this spell's mana cost.\n"
                        "Counter target spell.")

    def test_fow_predicate_true_with_blue_card_in_hand(self, game, make_card):
        # game_1527458454317563985: FoW sat dead in hand for 51 turns because
        # the response-AI affordability filters checked only the printed cost.
        rick = game.players[0]
        fow = self._fow(make_card)
        blue = make_card("Brainstorm", type_line="Instant", mana_cost="{U}",
                         cmc=1, power="0", toughness="0")
        rick.hand.extend([fow, blue])

        assert rick.can_pay_printed_alternate_cost(fow) is True

    def test_fow_predicate_false_without_blue_card(self, game, make_card):
        rick = game.players[0]
        fow = self._fow(make_card)
        rick.hand.append(fow)
        rick.hand.append(make_card("Shock", type_line="Instant",
                                   mana_cost="{R}", cmc=1,
                                   power="0", toughness="0"))

        assert rick.can_pay_printed_alternate_cost(fow) is False

    def test_fireblast_predicate_needs_two_mountains(self, game, make_card):
        rick = game.players[0]
        fireblast = make_card(
            "Fireblast", type_line="Instant", mana_cost="{4}{R}{R}", cmc=6,
            power="0", toughness="0",
            oracle_text="You may sacrifice two Mountains rather than pay "
                        "this spell's mana cost.\n"
                        "Fireblast deals 4 damage to any target.")
        rick.battlefield.append(make_card(
            "Mountain", type_line="Basic Land — Mountain",
            power="0", toughness="0"))
        assert rick.can_pay_printed_alternate_cost(fireblast) is False

        rick.battlefield.append(make_card(
            "Mountain", type_line="Basic Land — Mountain",
            power="0", toughness="0"))
        assert rick.can_pay_printed_alternate_cost(fireblast) is True

    def test_fow_casts_with_zero_mana_via_alternate_cost(
            self, make_game, make_card):
        # End-to-end: the mana pre-gate must waive for a payable printed
        # alternate cost (it used to reject before _compute_alt_costs could
        # take the alternate-cost branch), and the cast must exile the blue
        # card and charge 1 life.
        from mtg.models import StackEntry
        from mtg.spells import cast_spell_async

        game = _ready(make_game())
        rick, claude = game.players
        threat = make_card("Grave Titan", type_line="Creature — Giant",
                           mana_cost="{4}{B}{B}", cmc=6)
        game.stack.append(StackEntry(card=threat, controller_name=claude.name,
                                     controller_index=1))
        fow = self._fow(make_card)
        blue = make_card("Brainstorm", type_line="Instant", mana_cost="{U}",
                         cmc=1, power="0", toughness="0")
        rick.hand.extend([fow, blue])
        life_before = rick.life

        ok, msg, _ = asyncio.run(cast_spell_async(
            _engine(), game, rick, fow, target=threat))

        assert ok is True, msg
        assert blue in rick.exile
        assert blue not in rick.hand
        assert rick.life == life_before - 1


class TestResponseTextHelper:
    def test_thinking_block_first_content_is_skipped(self):
        # 20 games in the July 16 batch: claude-sonnet-5 responses led with a
        # thinking block, and response.content[0].text raised AttributeError
        # ('ThinkingBlock' object has no attribute 'text') in every actor /
        # strategist / judge parse site.
        from mtg.helpers import response_text

        response = SimpleNamespace(content=[
            SimpleNamespace(type="thinking", thinking="private reasoning"),
            SimpleNamespace(type="text", text='{"type": "pass"}'),
        ])
        assert response_text(response) == '{"type": "pass"}'

    def test_multiple_text_blocks_are_joined(self):
        from mtg.helpers import response_text

        response = SimpleNamespace(content=[
            SimpleNamespace(type="text", text="part one"),
            SimpleNamespace(type="thinking", thinking="ignored"),
            SimpleNamespace(type="text", text=" part two"),
        ])
        assert response_text(response) == "part one part two"

    def test_empty_or_malformed_response_yields_empty_string(self):
        from mtg.helpers import response_text

        assert response_text(SimpleNamespace(content=[])) == ""
        assert response_text(SimpleNamespace()) == ""
        assert response_text(None) == ""
