"""June 11 audit — regression pins for the batch-log findings.

Each test names the game that motivated it. The headline bug: Pact of
Negation's delayed trigger fired on the OPPONENT'S upkeep one turn late and
never attempted payment — it decided 6 of 139 games in the June 11 batch
(e.g. game_1514621789555265558: caster lost at 26 life with ~10 untapped
blue sources).
"""
from types import SimpleNamespace

import pytest


# (TestTarotThinkingBlocks lives upstream only — the tarot feature is not
# part of this MTG-only fork.)

class TestSmallReviewerObservations:
    def test_oath_of_teferi_returns_at_end_step_not_immediately(self, lib):
        actions, _ = lib.resolve_etb(
            "Oath of Teferi",
            "When Oath of Teferi enters, exile another target permanent you control. "
            "Return it to the battlefield under its owner's control at the beginning "
            "of the next end step.",
            "Rick", "Claude",
            game_context={"_source_card_name": "Oath of Teferi",
                          "explicit_target_name": "Mulldrifter"},
        )

        assert [action["action"] for action in actions] == [
            "move_card", "schedule_delayed_trigger"]
        assert actions[0]["to_zone"] == "exile"
        delayed = actions[1]
        assert delayed["trigger_at"] == "end_step"
        assert delayed["actions"][0]["from_zone"] == "exile"
        assert delayed["actions"][0]["to_zone"] == "battlefield"

    def test_blind_obedience_forces_opponents_creature_tapped(
            self, make_game, make_card, rules):
        game = make_game()
        blind = make_card(
            "Blind Obedience", type_line="Enchantment", power=None, toughness=None,
            oracle_text="Artifacts and creatures your opponents control enter tapped. Extort")
        game.players[0].battlefield.append(blind)
        game.register_replacement_effects(blind, "Rick")
        opponent_creature = make_card("Charging Bear")

        enters_tapped, message = rules._check_enters_tapped(
            game, opponent_creature, game.players[1])

        assert enters_tapped is True
        assert opponent_creature.tapped is True
        assert "Blind Obedience" in message

    def test_spark_double_walker_gets_extra_loyalty_and_own_activation_slot(
            self, make_game, make_card):
        from mtg.constants import Phase
        from mtg.spells import _apply_clone_characteristics
        from rules.planeswalker import PlaneswalkerManager

        game = make_game()
        game.phase = Phase.MAIN1
        game.active_player_index = 0
        original = make_card(
            "Aminatou, the Fateshifter",
            type_line="Legendary Planeswalker — Aminatou", power=None, toughness=None,
            loyalty=3, oracle_text="+1: Draw a card.")
        original.loyalty_counters = 3
        spark = make_card("Spark Double", power="0", toughness="0")
        _apply_clone_characteristics(spark, original)
        game.players[0].battlefield.extend([original, spark])
        manager = PlaneswalkerManager()
        manager._activations_this_turn[game.thread_id] = {original.id: 1}
        original._pw_activated_turn = game.turn_number
        original._pw_activations_this_turn = 1

        can_activate_copy, reason = manager.can_activate(
            game, game.players[0], spark, 0)

        assert spark.loyalty_counters == 4
        assert can_activate_copy is True, reason

    def test_ghostly_flicker_rejects_one_target(self, make_game, make_card):
        from mtg.spells import _ghostly_flicker_targets

        game = make_game()
        one = make_card("Mulldrifter")
        two = make_card("Mana Rock", type_line="Artifact", power=None, toughness=None)
        game.players[0].battlefield.extend([one, two])

        chosen, error = _ghostly_flicker_targets(game, game.players[0], one)

        assert chosen == []
        assert "exactly two" in error

    def test_ghostly_flicker_emits_exactly_two_declared_flickers(
            self, make_game, make_card, lib):
        from rules.effect_templates import build_game_context

        game = make_game()
        one = make_card("Mulldrifter")
        two = make_card("Mana Rock", type_line="Artifact", power=None, toughness=None)
        game.players[0].battlefield.extend([one, two])
        ctx = build_game_context(
            game, game.players[0], game.players[1],
            explicit_target=[one, two])
        actions, _ = lib.resolve_spell(
            "Ghostly Flicker",
            "Exile two target artifacts, creatures, and/or lands you control, "
            "then return those cards to the battlefield under your control.",
            "Rick", "Claude", game_context=ctx)

        assert actions == [
            {"action": "flicker", "player": "Rick", "target": "Mulldrifter"},
            {"action": "flicker", "player": "Rick", "target": "Mana Rock"},
        ]

    def test_ghostly_flicker_cast_accepts_and_resolves_two_targets(
            self, make_game, make_card):
        import asyncio
        from mtg.engine import GameEngine
        from mtg.spells import cast_spell_async

        game = make_game()
        flicker = make_card(
            "Ghostly Flicker", type_line="Instant", mana_cost="{2}{U}", cmc=3,
            oracle_text=("Exile two target artifacts, creatures, and/or lands you "
                         "control, then return those cards to the battlefield under "
                         "your control."))
        one = make_card("Mulldrifter")
        two = make_card("Mana Rock", type_line="Artifact", power=None, toughness=None)
        islands = [make_card(
            f"Island {i}", type_line="Basic Land — Island",
            oracle_text="{T}: Add {U}.", power="0", toughness="0")
            for i in range(3)]
        game.players[0].hand.append(flicker)
        game.players[0].battlefield.extend([one, two, *islands])

        success, cast_message, effect_messages = asyncio.run(cast_spell_async(
            GameEngine(None), game, game.players[0], flicker,
            pay_mana=False, target=[one, two]))

        assert success, (cast_message, effect_messages)
        assert one in game.players[0].battlefield
        assert two in game.players[0].battlefield
        assert flicker in game.players[0].graveyard

    def test_smothering_tithe_treasures_are_doubled_by_procession(
            self, make_game, make_card):
        from mtg.engine import GameEngine

        game = make_game()
        tithe = make_card(
            "Smothering Tithe", type_line="Enchantment", power=None, toughness=None,
            oracle_text=("Whenever an opponent draws a card, that player may pay {2}. "
                         "If the player doesn't, you create a Treasure token."))
        procession = make_card(
            "Anointed Procession", type_line="Enchantment", power=None, toughness=None,
            oracle_text=("If an effect would create one or more tokens under your "
                         "control, it creates twice that many of those tokens instead."))
        game.players[0].battlefield.extend([tithe, procession])
        game.register_replacement_effects(procession, "Rick")
        game.players[1].library.append(make_card("Drawn Spell", type_line="Sorcery"))

        GameEngine(None).draw_cards(game.players[1], 1, game)

        treasures = [card for card in game.players[0].battlefield
                     if card.name == "Treasure"]
        assert len(treasures) == 2
        assert "creates 2 Treasure" in game._pending_messages[-1]


# ---------------------------------------------------------------------------
# Pact of Negation template shape (pure — no game object)
# ---------------------------------------------------------------------------

class TestPactOfNegationTemplate:
    def _pact_actions(self, lib):
        actions, _desc = lib.resolve_spell(
            "Pact of Negation",
            "Counter target spell. At the beginning of your next upkeep, "
            "pay {3}{U}{U}. If you don't, you lose the game.",
            "Rick", "Claude")
        assert actions is not None
        return actions

    def test_schedules_for_casters_own_upkeep(self, lib):
        sched = [a for a in self._pact_actions(lib)
                 if a["action"] == "schedule_delayed_trigger"]
        assert len(sched) == 1
        # turn_delay=1 skipped the caster's upkeep entirely (fired a turn
        # late, on the opponent's upkeep, after the caster tapped out).
        assert sched[0].get("turn_delay", 0) == 0
        assert sched[0].get("upkeep_of") == "Rick"

    def test_attempts_payment_before_losing(self, lib):
        sched = [a for a in self._pact_actions(lib)
                 if a["action"] == "schedule_delayed_trigger"][0]
        inner = sched["actions"]
        assert len(inner) == 1
        # Old shape was an unconditional lose_the_game — no payment attempt.
        assert inner[0]["action"] == "pay_or_lose"
        assert inner[0]["cost"] == "{3}{U}{U}"
        assert inner[0]["player"] == "Rick"


# ---------------------------------------------------------------------------
# Delayed-trigger upkeep_of gating (engine method with stub self/game)
# ---------------------------------------------------------------------------

def _stub_game(active_idx, delayed):
    p0 = SimpleNamespace(name="Rick")
    p1 = SimpleNamespace(name="Claude")
    players = [p0, p1]
    return SimpleNamespace(players=players,
                           active_player=players[active_idx],
                           delayed_triggers=delayed)


def _run_delayed(game, phase_name, executed):
    from mtg.engine import GameEngine
    stub_self = SimpleNamespace(
        rules=SimpleNamespace(
            _execute_action_on_state=lambda g, a: executed.append(a) or ""),
        _handle_etb_triggers=lambda g, p, c: [],
    )
    return GameEngine._process_delayed_triggers(stub_self, game, phase_name)


class TestUpkeepOfGating:
    def _pact_dt(self):
        return {"trigger_at": "upkeep", "turn_delay": 0, "upkeep_of": 0,
                "source": "Pact of Negation", "once": True,
                "actions": [{"action": "pay_or_lose", "player": "Rick",
                             "cost": "{3}{U}{U}", "source": "Pact of Negation",
                             "reason": "failed to pay Pact of Negation cost"}]}

    def test_does_not_fire_on_opponents_upkeep(self):
        executed = []
        game = _stub_game(active_idx=1, delayed=[self._pact_dt()])
        _run_delayed(game, "upkeep", executed)
        assert executed == []                      # nothing ran
        assert len(game.delayed_triggers) == 1     # still queued
        # Non-matching upkeeps must not consume turn_delay either.
        assert game.delayed_triggers[0].get("turn_delay", 0) == 0

    def test_fires_on_owners_upkeep(self):
        executed = []
        game = _stub_game(active_idx=0, delayed=[self._pact_dt()])
        _run_delayed(game, "upkeep", executed)
        assert len(executed) == 1
        assert executed[0]["action"] == "pay_or_lose"
        assert game.delayed_triggers == []         # consumed (once=True)

    def test_ungated_triggers_keep_old_behavior(self):
        executed = []
        dt = {"trigger_at": "upkeep", "turn_delay": 0, "source": "Generic",
              "once": True, "actions": [{"action": "noop"}]}
        game = _stub_game(active_idx=1, delayed=[dt])
        _run_delayed(game, "upkeep", executed)
        assert len(executed) == 1                  # fires on any upkeep


# ---------------------------------------------------------------------------
# coerce_ai_string — the 'dict has no attribute strip/lower' crash family
# (game_1514633334079098891 died on turn 18 to a dict-shaped target)
# ---------------------------------------------------------------------------

class TestCoerceAiString:
    @pytest.mark.parametrize("value,expected", [
        ("Shriekmaw", "Shriekmaw"),
        ({"name": "Shriekmaw"}, "Shriekmaw"),
        ({"player": "opponent"}, "opponent"),
        (["Rick Deckard"], "Rick Deckard"),
        ({"card": {"name": "Fury"}}, "Fury"),
        (None, ""),
        (7, "7"),
        ({}, ""),
        ([], ""),
    ])
    def test_coercion(self, value, expected):
        from mtg.helpers import coerce_ai_string
        assert coerce_ai_string(value) == expected

    def test_resolver_survives_dict_target(self):
        from mtg.helpers import _resolve_player_or_card_target
        p0 = SimpleNamespace(name="Rick", battlefield=[])
        p1 = SimpleNamespace(name="Claude", battlefield=[])
        game = SimpleNamespace(players=[p0, p1])
        # Old code: AttributeError 'dict' object has no attribute 'strip',
        # which killed the game with no winner.
        assert _resolve_player_or_card_target(game, p0, {"player": "opponent"}) is p1


# ---------------------------------------------------------------------------
# PW activation display — repeats show a short reminder, never a bare header
# (June 11 batch: 192/313 activations had no oracle text)
# ---------------------------------------------------------------------------

class TestActivateLineRepeats:
    def test_repeat_activation_keeps_short_text(self):
        from mtg.helpers import format_activate_line
        game = SimpleNamespace()
        text = "Discard up to two cards, then draw that many cards."
        first = format_activate_line("Daretti, Scrap Savant", 2, text, game=game)
        second = format_activate_line("Daretti, Scrap Savant", 2, text, game=game)
        assert "Discard up to two" in first
        # Repeat must still carry (possibly truncated) ability text.
        assert second.rstrip().endswith("_") or "Discard" in second
        assert not second.rstrip().endswith("ability")


# ---------------------------------------------------------------------------
# Tranche 2 — template misexecutions (June 11 audit, second wave)
# ---------------------------------------------------------------------------

class TestTranche2Templates:
    def test_delay_suspends_instead_of_graveyard(self, lib):
        actions, _ = lib.resolve_spell(
            "Delay", "Counter target spell. If the spell is countered this way, "
            "exile it with three time counters on it instead of putting it into "
            "its owner's graveyard.", "Rick", "Claude")
        assert actions is not None
        assert actions[0]["action"] == "counter_spell"
        assert actions[0].get("countered_to") == "exile_suspend"

    def test_mana_leak_offers_payment(self, lib):
        actions, _ = lib.resolve_spell(
            "Mana Leak", "Counter target spell unless its controller pays {3}.",
            "Rick", "Claude")
        assert actions is not None
        assert actions[0]["action"] == "counter_unless_pays"
        assert actions[0]["cost"] == "{3}"

    def test_commit_counters_stack_spell_to_library(self, lib):
        actions, _ = lib.resolve_spell(
            "Commit", "Put target spell or nonland permanent into its owner's "
            "library second from the top.", "Rick", "Claude",
            game_context={"stack_top_spell": "Greater Good"})
        assert actions is not None
        assert actions[0]["action"] == "counter_spell"
        assert actions[0].get("countered_to") == "library"

    def test_curse_of_swine_exiles_x_and_boars_to_owner(self, lib):
        ctx = {"x_value": 2, "_opponent_creatures": [
            {"name": "Judith, the Scourge Diva", "power": 5},
            {"name": "Shriekmaw", "power": 3},
            {"name": "Bloodghast", "power": 2},
        ]}
        actions, _ = lib.resolve_spell(
            "Curse of the Swine",
            "Exile X target creatures. For each creature exiled this way, its "
            "controller creates a 2/2 green Boar creature token.",
            "Claude", "Rick", game_context=ctx)
        assert actions is not None
        exiles = [a for a in actions if a["action"] == "move_card"
                  and a["to_zone"] == "exile"]
        tokens = [a for a in actions if a["action"] == "create_token"]
        assert len(exiles) == 2                      # X targets, not 1
        assert exiles[0]["card"] == "Judith, the Scourge Diva"
        assert len(tokens) == 1
        assert tokens[0]["player"] == "Rick"         # owner, not caster
        assert tokens[0]["count"] == 2

    def test_cruel_ultimatum_hits_the_right_players(self, lib):
        actions, _ = lib.resolve_spell(
            "Cruel Ultimatum",
            "Target opponent sacrifices a creature, discards three cards, then "
            "loses 5 life. You return a creature card from your graveyard to "
            "your hand, draw three cards, then gain 5 life.",
            "Rick", "Claude")
        assert actions is not None
        by_action = {}
        for a in actions:
            by_action.setdefault(a["action"], []).append(a)
        assert by_action["sacrifice_permanent"][0]["player"] == "Claude"
        assert len(by_action["discard"]) == 3
        assert all(a["player"] == "Claude" for a in by_action["discard"])
        assert by_action["lose_life"][0]["player"] == "Claude"
        assert by_action["lose_life"][0]["amount"] == 5
        assert by_action["draw_cards"][0]["player"] == "Rick"
        assert by_action["draw_cards"][0]["amount"] == 3
        assert by_action["gain_life"][0]["player"] == "Rick"


# ---------------------------------------------------------------------------
# Tranche 3 — layers staleness, trigger gates, template dispatch
# ---------------------------------------------------------------------------

class TestTranche3:
    def test_layered_permanent_carries_is_token(self):
        # Intangible Virtue never applied: the filter read is_token from a
        # dict that never contained it (game 1514621737994551457).
        from rules.layers import LayeredPermanent
        lp = LayeredPermanent(id="t1", name="Human", controller="Claude",
                              owner="Claude", is_token=True)
        assert lp.to_dict()["is_token"] is True

    def test_token_anthem_filter_matches_tokens_only(self):
        from rules.layers import create_anthem_effect
        eff = create_anthem_effect("Intangible Virtue", "iv_tokens", "Claude",
                                   1, 1, "creature tokens you control")
        token = {"id": "t1", "name": "Human", "controller": "Claude",
                 "types": ["creature"], "subtypes": [], "is_token": True}
        nontoken = dict(token, id="t2", is_token=False)
        assert eff.applies_to_permanent(token, None) is True
        assert eff.applies_to_permanent(nontoken, None) is False

    def test_whitemane_lion_bounce_is_creature_only_and_may_self_bounce(self, lib):
        actions, _ = lib.resolve_etb(
            "Whitemane Lion",
            "Flash\nWhen Whitemane Lion enters the battlefield, return a "
            "creature you control to its owner's hand.",
            "Rick", "Claude")
        assert actions is not None
        a = actions[0]
        assert a["action"] == "bounce_own_permanent"
        # "a creature" (not "another"): self-bounce legal, land bounce not.
        assert a.get("type_filter") == "creature"
        assert not a.get("exclude")

    def test_lucky_clover_requires_adventure_spell(self):
        from mtg.triggers import _spell_matches_cast_trigger

        class FakeCard:
            def __init__(self, type_line, adventure_name=None):
                self.type_line = type_line
                self.adventure_name = adventure_name
                self.cast_as_adventure = False
                self.oracle_text = ""
            def is_creature(self): return "creature" in self.type_line.lower()
            def is_instant(self): return "instant" in self.type_line.lower()
            def is_sorcery(self): return "sorcery" in self.type_line.lower()
            def is_artifact(self): return "artifact" in self.type_line.lower()
            def is_enchantment(self): return "enchantment" in self.type_line.lower()
            def is_planeswalker(self): return False
            def is_land(self): return False

        clover = "whenever you cast an adventure instant or sorcery spell, copy it."
        farseek = FakeCard("Sorcery")
        # game 1514629231433351168: fired on 10 of 13 plain casts.
        assert _spell_matches_cast_trigger(None, clover, farseek) is False
        adventure_half = FakeCard("Sorcery — Adventure")
        assert _spell_matches_cast_trigger(None, clover, adventure_half) is True


# ---------------------------------------------------------------------------
# Tranche 4 — combined enter/attack triggers, non-cast ETBs, prowess
# ---------------------------------------------------------------------------

class TestCombinedEnterAttackTriggers:
    def test_modern_this_creature_wording_is_both_self_etb_and_attack(self, make_card):
        from mtg.triggers import (
            _is_self_attack_trigger_paragraph,
            _is_self_etb_trigger_paragraph,
        )
        titan = make_card("Inferno Titan")
        text = ("Whenever this creature enters or attacks, it deals 3 damage "
                "divided as you choose among one, two, or three targets.")
        assert _is_self_etb_trigger_paragraph(titan, text) is True
        assert _is_self_attack_trigger_paragraph(titan, text) is True

    def test_ongoing_and_global_attack_triggers_are_not_self_triggers(self, make_card):
        from mtg.triggers import (
            _is_self_attack_trigger_paragraph,
            _is_self_etb_trigger_paragraph,
        )
        adeline = make_card("Adeline, Resplendent Cathar")
        assert _is_self_attack_trigger_paragraph(
            adeline, "Whenever you attack, create a tapped and attacking Human.") is False
        soul_warden = make_card("Soul Warden")
        assert _is_self_etb_trigger_paragraph(
            soul_warden, "Whenever another creature enters, you gain 1 life.") is False

    def test_inferno_titan_combined_attack_trigger_executes(self, make_game, make_card, rules):
        from mtg.triggers import _check_attack_triggers_sync
        game = make_game()
        attacker = make_card(
            "Inferno Titan", power="6", toughness="6",
            oracle_text=("{R}: This creature gets +1/+0 until end of turn.\n"
                         "Whenever this creature enters or attacks, it deals 3 damage "
                         "divided as you choose among one, two, or three targets."),
        )
        game.players[0].battlefield.append(attacker)
        game.attackers = [attacker.id]
        engine = SimpleNamespace(
            rules=rules,
            _should_emit_resolve_prompt=lambda *args: False,
        )
        messages, unhandled = _check_attack_triggers_sync(
            engine, game, attacker, game.players[0])
        # June 11 game 1514633271047225385 missed four attacks because the
        # scanner only recognized the exact phrase "this creature attacks".
        assert game.players[1].life == 37
        assert messages
        assert unhandled == []

    def test_frost_titan_name_template_is_attack_scoped(self, lib):
        actions, _ = lib.resolve_attack_trigger(
            "Frost Titan",
            "Whenever this creature enters or attacks, tap target permanent.",
            "Frost Titan", 6, "Rick", "Claude",
            game_context={"best_opponent_threat": "Sol Ring"},
        )
        assert actions is not None
        assert actions[0]["action"] == "tap"


class TestNoncastEtbAndProwess:
    def test_move_card_to_battlefield_fires_craterhoof_etb(
            self, make_game, make_card, rules):
        game = make_game()
        bear = make_card("Bear", power="2", toughness="2")
        hoof = make_card(
            "Craterhoof Behemoth", power="5", toughness="5",
            oracle_text=("Haste\nWhen this creature enters, creatures you control "
                         "gain trample and get +X/+X until end of turn, where X "
                         "is the number of creatures you control."),
        )
        game.players[0].battlefield.append(bear)
        game.players[0].graveyard.append(hoof)
        rules.engine_ref = SimpleNamespace(
            _handle_etb_triggers=lambda game, player, card: [])

        rules._execute_action_on_state(game, {
            "action": "move_card", "card": "Craterhoof Behemoth",
            "from_zone": "graveyard", "to_zone": "battlefield",
            "player": "Rick",
        })

        # X=2 after Craterhoof enters. Before the fix both creatures remained
        # at printed power in game 1514626089496875188.
        assert hoof.get_effective_power(game) == 7
        assert bear.get_effective_power(game) == 4
        assert hoof.has_keyword("Trample")

    def test_noncreature_cast_applies_one_prowess_pump(
            self, make_game, make_card, rules):
        import asyncio
        from mtg.triggers import _check_cast_triggers

        game = make_game()
        prowess = make_card("Monastery Swiftspear", power="1", toughness="2",
                            oracle_text=("Prowess (Whenever you cast a noncreature spell, "
                                         "this creature gets +1/+1 until end of turn.)"),
                            keywords=["Prowess"])
        spell = make_card("Opt", type_line="Instant", power=None, toughness=None)
        game.players[0].battlefield.append(prowess)
        engine = SimpleNamespace(
            rules=rules, spell_resolver=None,
            _spell_matches_cast_trigger=lambda *args: True,
            _should_emit_resolve_prompt=lambda *args: False,
        )

        messages = asyncio.run(_check_cast_triggers(
            engine, game, game.players[0], spell))

        assert prowess.get_effective_power(game) == 2
        assert prowess.get_effective_toughness(game) == 3
        assert sum("prowess triggers" in m.lower() for m in messages) == 1


# ---------------------------------------------------------------------------
# July 11 audit — deterministic pins from game_1525*
# ---------------------------------------------------------------------------

class TestJuly11TriggerTemplates:
    def test_altar_of_brood_watches_every_permanent_type(
            self, make_game, make_card, rules):
        from mtg.triggers import _check_permanent_etb_watchers
        game = make_game()
        altar = make_card(
            "Altar of the Brood", type_line="Artifact", power=None, toughness=None,
            oracle_text=("Whenever another permanent you control enters, "
                         "each opponent mills a card."))
        creature = make_card("Bear")
        artifact = make_card("Mind Stone", type_line="Artifact", power=None, toughness=None)
        game.players[0].battlefield.extend([altar, creature, artifact])
        game.players[1].library.extend([
            make_card("Top One"), make_card("Top Two")])
        engine = SimpleNamespace(rules=rules)
        assert _check_permanent_etb_watchers(
            engine, game, game.players[0], creature)
        assert _check_permanent_etb_watchers(
            engine, game, game.players[0], artifact)
        assert [card.name for card in game.players[1].graveyard] == ["Top One", "Top Two"]

    def test_elspeth_minus_three_keeps_small_creatures(self, lib):
        actions, _ = lib.resolve_pw_ability(
            "Elspeth, Sun's Champion",
            "−3: Destroy all creatures with power 4 or greater.",
            "Rick", "Claude")
        assert actions == [{"action": "destroy_by_power", "min_power": 4}]

    def test_dragon_rage_channeler_surveils_in_tier_15(self, lib):
        actions, _ = lib.resolve_spell(
            "Dragon's Rage Channeler",
            "Whenever you cast a noncreature spell, surveil 1.",
            "Rick", "Claude")
        assert actions == [{"action": "surveil", "player": "Rick", "amount": 1}]

    def test_hallowed_haunting_token_keeps_dynamic_cda(
            self, lib, make_game, rules):
        game = make_game()
        actions, _ = lib.resolve_spell(
            "Hallowed Haunting",
            ('Whenever you cast an enchantment spell, create a white Spirit '
             'Cleric creature token with "This token\'s power and toughness '
             'are each equal to the number of Spirits you control."'),
            "Rick", "Claude")
        assert actions and actions[0]["power"] == actions[0]["toughness"] == "*"
        rules._execute_action_on_state(game, actions[0])
        first = game.players[0].battlefield[0]
        assert first.oracle_text
        assert first.get_effective_power(game) == 1
        assert first.get_effective_toughness(game) == 1
        rules._execute_action_on_state(game, actions[0])
        assert [c.get_effective_power(game) for c in game.players[0].battlefield] == [2, 2]
        assert rules.process_state_based_actions(game) == []

    @pytest.mark.parametrize("name,oracle", [
        ("Goblin Guide", "Whenever this creature attacks, defending player reveals the top card of their library."),
        ("Sun Titan", "Whenever this creature enters or attacks, you may return target permanent card with mana value 3 or less."),
        ("Etali, Primal Storm", "Whenever this creature attacks, exile the top card of each player's library."),
    ])
    def test_common_attack_triggers_do_not_fall_to_tier3(self, lib, name, oracle):
        actions, _ = lib.resolve_attack_trigger(
            name, oracle, name, 2, "Rick", "Claude",
            game_context={"controller_graveyard": [], "controller_library": [],
                          "opponent_library": []})
        assert actions is not None

    def test_fall_of_the_thran_chapter_one_targets_only_lands(self, lib):
        actions, _ = lib.resolve_spell(
            "Fall of the Thran", "Destroy all lands.", "Rick", "Claude")
        assert actions == [{"action": "destroy_all_by_type", "type": "lands"}]

    def test_ophiomancer_intervening_if_checks_live_board(
            self, lib, make_game, make_card):
        game = make_game()
        snake = make_card("Snake", type_line="Creature — Snake")
        game.players[0].battlefield.append(snake)
        from rules.effect_templates import build_game_context
        ctx = build_game_context(game, game.players[0], game.players[1])
        actions, _ = lib.resolve_upkeep_trigger(
            "Ophiomancer",
            "At the beginning of each upkeep, if you control no Snakes, create a 1/1 black Snake creature token with deathtouch.",
            "Rick", "Claude", game_context=ctx)
        assert actions[0]["action"] == "no_action"


class TestJuly11StateIntegrity:
    def test_empty_library_loss_survives_later_life_gain(self, make_game, rules):
        game = make_game()
        rick = game.players[0]
        rick.library.clear()
        rules._execute_action_on_state(game, {
            "action": "draw_cards", "player": "Rick", "amount": 1})
        rick.life += 1
        messages = rules.process_state_based_actions(game)
        assert game.ended is True and game.winner == 1
        assert any("empty library" in message for message in messages)

    def test_serra_conditional_toughness_reaches_delegated_sba(
            self, make_game, make_card, rules):
        game = make_game()
        serra = make_card(
            "Serra Ascendant", power="1", toughness="1",
            oracle_text=("Lifelink\nAs long as you have 30 or more life, "
                         "this creature gets +5/+5 and has flying."))
        serra.damage_marked = 5
        game.players[0].battlefield.append(serra)
        assert serra.get_effective_toughness(game) == 6
        assert rules.process_state_based_actions(game) == []
        assert serra in game.players[0].battlefield

    def test_restoration_uses_architect_back_face(
            self, make_game, make_card, rules):
        game = make_game()
        saga = make_card(
            "The Restoration of Eiganjo", type_line="Enchantment — Saga",
            power=None, toughness=None,
            oracle_text=("I — Search your library for a basic Plains card.\n"
                         "II — You may discard a card.\n"
                         "III — Exile this Saga, then return it to the battlefield "
                         "transformed under your control."),
            counters={"lore": 3})
        game.players[0].battlefield.append(saga)
        rules.process_state_based_actions(game)
        assert saga in game.players[0].battlefield
        assert saga.name == "Architect of Restoration"
        assert saga.type_line == "Enchantment Creature — Fox Monk"
        assert (saga.power, saga.toughness) == ("3", "4")

    def test_sphere_of_safety_blocks_zero_mana_attacker(
            self, make_game, make_card, rules):
        from mtg.constants import Phase
        game = make_game()
        game.phase = Phase.DECLARE_ATTACKERS
        attacker = make_card("Bear")
        sphere = make_card(
            "Sphere of Safety", type_line="Enchantment", power=None, toughness=None,
            oracle_text=("Creatures can't attack you unless their controller pays "
                         "{X} for each creature they control that's attacking you, "
                         "where X is the number of enchantments you control."))
        game.players[0].battlefield.append(attacker)
        game.players[1].battlefield.append(sphere)
        can_attack, reason = rules.can_attack_with(game, game.players[0], attacker)
        assert can_attack is False
        assert "Sphere of Safety" in reason


class TestSparkDouble:
    def test_can_copy_only_own_creature_or_planeswalker(self, make_game, make_card):
        from mtg.spells import _clone_target_is_legal

        game = make_game()
        spark = make_card("Spark Double")
        own_creature = make_card("Own Creature")
        opposing_creature = make_card("Opposing Creature")
        own_pw = make_card("Own Walker", type_line="Legendary Planeswalker — Test",
                           power=None, toughness=None, loyalty=4)

        assert _clone_target_is_legal(
            spark, own_creature, game.players[0], game.players[0]) is True
        assert _clone_target_is_legal(
            spark, own_pw, game.players[0], game.players[0]) is True
        # June 11 game 1514618379749560442 illegally copied the opponent's Baral.
        assert _clone_target_is_legal(
            spark, opposing_creature, game.players[0], game.players[1]) is False

    def test_copy_of_legend_is_nonlegendary_and_gets_counter(self, make_card):
        from mtg.spells import _apply_clone_characteristics

        spark = make_card("Spark Double", power="0", toughness="0",
                          oracle_text="You may have this creature enter as a copy...")
        baral = make_card("Baral, Chief of Compliance", power="1", toughness="3",
                          type_line="Legendary Creature — Human Wizard")

        original_name = _apply_clone_characteristics(spark, baral)

        assert original_name == "Spark Double"
        assert spark.name == "Baral, Chief of Compliance"
        assert "legendary" not in spark.type_line.lower()
        assert spark.counters["+1/+1"] == 1
        assert spark._pre_copy_snapshot["name"] == "Spark Double"

    def test_bounced_copy_reverts_to_spark_double(self, make_game, make_card, rules):
        from mtg.spells import _apply_clone_characteristics

        game = make_game()
        spark = make_card("Spark Double", power="0", toughness="0", cmc=4,
                          oracle_text="You may have this creature enter as a copy...")
        baral = make_card("Baral, Chief of Compliance", power="1", toughness="3",
                          type_line="Legendary Creature — Human Wizard")
        game.players[0].battlefield.extend([spark, baral])
        _apply_clone_characteristics(spark, baral)

        rules._execute_action_on_state(game, {
            "action": "bounce_own_permanent", "player": "Rick",
            "card": "Baral, Chief of Compliance",
        })

        assert spark in game.players[0].hand
        assert spark.name == "Spark Double"
        assert spark.power == "0"
        assert spark.counters == {}
        assert spark._pre_copy_snapshot is None


class TestSpeciesSpecialistAndDiesWaves:
    def test_etb_template_records_most_common_creature_type(
            self, make_game, make_card, rules, lib):
        game = make_game()
        specialist = make_card(
            "Species Specialist", type_line="Creature — Human Warrior")
        zombie_one = make_card("Zombie One", type_line="Creature — Zombie")
        zombie_two = make_card("Zombie Two", type_line="Creature — Zombie Wizard")
        elf = make_card("Elf", type_line="Creature — Elf Druid")
        game.players[0].battlefield.extend(
            [specialist, zombie_one, zombie_two, elf])

        actions, _ = lib.resolve_etb(
            "Species Specialist",
            "As this creature enters, choose a creature type.",
            "Rick", "Claude")
        assert actions[0]["action"] == "choose_creature_type"
        rules._execute_action_on_state(game, actions[0])
        assert specialist._chosen_creature_type == "Zombie"

    def test_living_death_uses_source_snapshot_and_chosen_type(
            self, make_game, make_card, rules):
        from mtg.triggers import _check_dies_triggers_sync

        game = make_game()
        beast = make_card("Dying Beast", type_line="Creature — Beast")
        returned_human = make_card("Returned Human", type_line="Creature — Human")
        specialist = make_card(
            "Species Specialist", type_line="Creature — Human Warrior",
            oracle_text=("As this creature enters, choose a creature type.\n"
                         "Whenever a creature of the chosen type dies, you may draw a card."))
        specialist._chosen_creature_type = "Human"
        butcher = make_card(
            "Butcher of Malakir", type_line="Creature — Vampire Warrior",
            oracle_text=("Flying\nWhenever this creature or another creature you "
                         "control dies, each opponent sacrifices a creature."))
        game.players[0].battlefield.append(beast)
        game.players[0].graveyard.append(returned_human)
        game.players[1].battlefield.append(specialist)
        game.players[1].graveyard.append(butcher)
        game.players[1].library.append(make_card("Drawn Card", type_line="Sorcery"))

        rules._execute_action_on_state(game, {"action": "living_death"})
        deaths = list(game._recently_died)
        game._recently_died = []
        game._active_dies_batch = deaths
        engine = SimpleNamespace(rules=rules)
        for dead_card, dead_player in deaths:
            _check_dies_triggers_sync(engine, game, dead_card, dead_player)
        game._active_dies_batch = []

        # The Beast is not the chosen type; Specialist sees its own Human
        # death once. Butcher returned only after the deaths and must not
        # retroactively sacrifice Returned Human.
        assert len(game.players[1].hand) == 1
        assert returned_human in game.players[0].battlefield
        assert butcher in game.players[1].battlefield

    def test_pact_family_templates_emit_real_sacrifice(self, lib):
        actions, _ = lib.resolve_dies_trigger(
            "Butcher of Malakir",
            "Whenever a creature you control dies, each opponent sacrifices a creature.",
            "Victim", 2, 2, "Claude", "Rick",
            game_context={"worst_opponent_creature": "Human"},
        )
        assert actions is not None
        assert actions[0]["action"] == "sacrifice_permanent"
        assert actions[0]["player"] == "Rick"
        assert actions[0]["type_filter"] == "creature"


class TestManaDrainTiming:
    def test_template_schedules_mana_instead_of_adding_immediately(self, lib):
        actions, _ = lib.resolve_spell(
            "Mana Drain",
            "Counter target spell. At the beginning of your next main phase, "
            "add an amount of {C} equal to that spell's mana value.",
            "Rick", "Claude",
            game_context={"stack_top_spell": "Craterhoof Behemoth",
                          "stack_top_cmc": 8},
        )
        assert actions is not None
        assert actions[0]["action"] == "counter_spell"
        assert not any(a["action"] == "add_mana" for a in actions)
        delayed = actions[1]
        assert delayed["action"] == "schedule_delayed_trigger"
        assert delayed["trigger_at"] == "main_phase"
        assert delayed["phase_of"] == "Rick"
        assert delayed["actions"] == [{
            "action": "add_mana", "player": "Rick",
            "color": "C", "amount": 8,
        }]

    def test_main_phase_owner_gate(self):
        executed = []
        dt = {"trigger_at": "main_phase", "turn_delay": 0,
              "phase_of": 0, "source": "Mana Drain", "once": True,
              "actions": [{"action": "add_mana", "player": "Rick",
                           "color": "C", "amount": 6}]}
        game = _stub_game(active_idx=1, delayed=[dt])
        _run_delayed(game, "main_phase", executed)
        assert executed == []
        assert len(game.delayed_triggers) == 1

        game.active_player = game.players[0]
        _run_delayed(game, "main_phase", executed)
        assert executed[0]["action"] == "add_mana"
        assert game.delayed_triggers == []

    def test_phase_change_clears_old_mana_before_drain_mana_arrives(self, make_game):
        from mtg.constants import Phase
        from mtg.engine import GameEngine

        game = make_game()
        game.phase = Phase.DRAW
        game.active_player_index = 0
        game.players[0].mana_pool["C"] = 2
        game.delayed_triggers.append({
            "trigger_at": "main_phase", "turn_delay": 0,
            "phase_of": 0, "source": "Mana Drain", "once": True,
            "actions": [{"action": "add_mana", "player": "Rick",
                         "color": "C", "amount": 5}],
        })
        engine = GameEngine(None)

        new_phase, _messages = engine.advance_phase(game)

        assert new_phase == Phase.MAIN1
        # Old C2 emptied at the boundary; delayed C5 remains usable in MAIN1.
        assert game.players[0].mana_pool["C"] == 5


class TestVictimizeAndGreaterGoodCosts:
    def _victimize_actions(self, lib, *, worst="Fodder"):
        actions, _ = lib.resolve_spell(
            "Victimize",
            "Choose two target creature cards in your graveyard. Sacrifice a "
            "creature. If you do, return the chosen cards to the battlefield tapped.",
            "Rick", "Claude",
            game_context={
                "controller_worst_creature": worst,
                "controller_graveyard_creatures": ["Target One", "Target Two"],
            },
        )
        assert actions is not None
        return actions

    def test_victimize_template_nests_tapped_returns_under_sacrifice(self, lib):
        actions = self._victimize_actions(lib)
        assert len(actions) == 1
        sacrifice = actions[0]
        assert sacrifice["action"] == "sacrifice_permanent"
        assert sacrifice["type_filter"] == "creature"
        assert sacrifice["preferred_card"] == "Fodder"
        assert [a["card"] for a in sacrifice["then_actions"]] == [
            "Target One", "Target Two"]
        assert all(a["tapped"] for a in sacrifice["then_actions"])

    def test_victimize_does_not_return_targets_when_sacrifice_fails(
            self, make_game, make_card, rules, lib):
        game = make_game()
        targets = [make_card("Target One"), make_card("Target Two")]
        game.players[0].graveyard.extend(targets)

        result = rules._execute_action_on_state(
            game, self._victimize_actions(lib)[0])

        # July 24: message now names the restriction ("no creature ...")
        assert "no creature to sacrifice" in result
        assert all(card in game.players[0].graveyard for card in targets)
        assert game.players[0].battlefield == []

    def test_victimize_sacrifices_then_returns_both_targets_tapped(
            self, make_game, make_card, rules, lib):
        game = make_game()
        fodder = make_card("Fodder")
        targets = [make_card("Target One"), make_card("Target Two")]
        game.players[0].battlefield.append(fodder)
        game.players[0].graveyard.extend(targets)

        rules._execute_action_on_state(game, self._victimize_actions(lib)[0])

        assert fodder in game.players[0].graveyard
        assert all(card in game.players[0].battlefield for card in targets)
        assert all(card.tapped for card in targets)

    def test_sacrifice_a_creature_cost_rejects_explicit_noncreature(
            self, make_game, make_card):
        from mtg.engine import _satisfies_sacrifice_cost

        game = make_game()
        deed = make_card("Pernicious Deed", type_line="Enchantment")
        creature = make_card("Fodder")
        cost = "Sacrifice a creature: Draw cards equal to its power."

        assert not _satisfies_sacrifice_cost(deed, cost, game)
        assert _satisfies_sacrifice_cost(creature, cost, game)


class TestDiesPlaceholderSuppression:
    def _engine_with_judith_death(self, make_game, make_card, *, has_client):
        from mtg.engine import GameEngine

        game = make_game()
        judith = make_card(
            "Judith, the Scourge Diva",
            oracle_text=("Other creatures you control get +1/+0.\n"
                         "Whenever a nontoken creature you control dies, "
                         "Judith deals 1 damage to any target."),
        )
        victim = make_card("Bloodghast")
        game.players[0].battlefield.append(judith)
        game.players[0].graveyard.append(victim)
        game._recently_died = [(victim, game.players[0])]

        engine = GameEngine(None)
        engine.rules.client = object() if has_client else None
        engine.rules.process_state_based_actions = lambda _game: []
        return engine, game

    def test_tier3_dies_queue_suppresses_queue_time_announcement(
            self, make_game, make_card):
        engine, game = self._engine_with_judith_death(
            make_game, make_card, has_client=True)

        messages = engine.check_state_based_actions(game)

        assert messages == []
        assert len(game.pending_async_triggers) == 1
        assert game.pending_async_triggers[0]["source_card"].name.startswith("Judith")

    def test_no_client_keeps_manual_resolution_hint(self, make_game, make_card):
        engine, game = self._engine_with_judith_death(
            make_game, make_card, has_client=False)

        messages = engine.check_state_based_actions(game)

        assert len(messages) == 1
        assert "Judith" in messages[0]
        assert len(game.pending_async_triggers) == 1


class TestStrategistRejectionBackoff:
    def test_two_rejections_skip_three_turns_then_retry(self, make_game):
        from mtg.claude_player import (
            _record_strategy_memo_result, _strategy_call_due)

        game = make_game()
        _record_strategy_memo_result(game, accepted=False)
        assert game._strategy_rejection_streak == 1
        assert _strategy_call_due(game)

        _record_strategy_memo_result(game, accepted=False)
        assert game._strategy_rejection_streak == 2
        assert [_strategy_call_due(game) for _ in range(3)] == [
            False, False, False]
        assert _strategy_call_due(game)

    def test_accepted_memo_resets_only_its_game(self, make_game):
        from mtg.claude_player import _record_strategy_memo_result

        rejected_game = make_game()
        other_game = make_game()
        for game in (rejected_game, other_game):
            _record_strategy_memo_result(game, accepted=False)
            _record_strategy_memo_result(game, accepted=False)

        _record_strategy_memo_result(rejected_game, accepted=True)

        assert rejected_game._strategy_rejection_streak == 0
        assert rejected_game._strategy_backoff_turns == 0
        assert other_game._strategy_rejection_streak == 2
        assert other_game._strategy_backoff_turns == 3


class TestKroxaCommanderSacrifice:
    def test_template_uses_named_commander_aware_sacrifice(self, lib):
        actions, _ = lib.resolve_etb(
            "Kroxa, Titan of Death's Hunger",
            "When Kroxa enters, sacrifice it unless it escaped.",
            "Claude", "Rick",
            game_context={"was_escaped": False},
        )

        sacrifice = actions[-1]
        assert sacrifice["action"] == "sacrifice_permanent"
        assert sacrifice["preferred_card"] == "Kroxa, Titan of Death's Hunger"
        assert sacrifice["only_preferred"] is True
        assert sacrifice["allow_commander"] is True

    def test_unescaped_commander_kroxa_dies_and_returns_to_command_zone(
            self, make_game, make_card, rules, lib):
        game = make_game()
        kroxa = make_card(
            "Kroxa, Titan of Death's Hunger",
            type_line="Legendary Creature — Elder Giant",
            oracle_text="When Kroxa enters, sacrifice it unless it escaped.",
            power="6", toughness="6", is_commander=True, owner_index=0,
        )
        bystander = make_card("Bystander")
        game.players[0].battlefield.extend([kroxa, bystander])
        actions, _ = lib.resolve_etb(
            kroxa.name, kroxa.oracle_text, "Rick", "Claude",
            game_context={"was_escaped": False},
        )

        result = rules._execute_action_on_state(game, actions[-1])

        assert kroxa in game.players[0].command_zone
        assert kroxa not in game.players[0].graveyard
        assert bystander in game.players[0].battlefield
        assert game._recently_died == [(kroxa, game.players[0])]
        assert "command zone" in result


class TestCastableTargetAnnotations:
    def test_murderous_compulsion_does_not_offer_untapped_creature(
            self, make_game, make_card):
        from mtg.claude_player import _annotate_castable_with_legality

        game = make_game()
        compulsion = make_card(
            "Murderous Compulsion", type_line="Sorcery",
            oracle_text="Destroy target tapped creature.", mana_cost="{1}{B}")
        chainer = make_card("Chainer, Nightmare Adept", tapped=False)
        game.players[0].hand.append(compulsion)
        game.players[1].battlefield.append(chainer)

        labels = _annotate_castable_with_legality(
            ["Murderous Compulsion ({1}{B})"], game.players[0].hand,
            game, game.players[1])

        assert "unplayable: no legal targets" in labels[0]
        assert "target available" not in labels[0]

    def test_murderous_compulsion_offers_tapped_creature(
            self, make_game, make_card):
        from mtg.claude_player import _annotate_castable_with_legality

        game = make_game()
        compulsion = make_card(
            "Murderous Compulsion", type_line="Sorcery",
            oracle_text="Destroy target tapped creature.", mana_cost="{1}{B}")
        chainer = make_card("Chainer, Nightmare Adept", tapped=True)
        game.players[0].hand.append(compulsion)
        game.players[1].battlefield.append(chainer)

        labels = _annotate_castable_with_legality(
            ["Murderous Compulsion ({1}{B})"], game.players[0].hand,
            game, game.players[1])

        assert "target available: Chainer, Nightmare Adept" in labels[0]


class TestSonnet5AndCombatTriggerBoundary:
    def test_all_direct_sonnet_call_sites_use_sonnet_5(self):
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        files = [
            root / "bot.py",
            root / "cube_draft.py",
            root / "mtg" / "claude_player.py",
            root / "mtg" / "rules_engine.py",
            root / "rules" / "effects.py",
        ]
        combined = "\n".join(path.read_text(encoding="utf-8") for path in files)
        assert "claude-sonnet-4-6" not in combined
        assert combined.count("claude-sonnet-5") >= 5

    def test_autoplay_drains_combat_triggers_before_main2(self, make_game):
        import asyncio
        from mtg.constants import Phase
        from mtg.cog import MTGGameCog

        game = make_game()
        sent = []
        phases_seen = []

        async def drain(current_game):
            phases_seen.append(current_game.phase)
            return ["Judith resolves"]

        engine = SimpleNamespace(
            rules=SimpleNamespace(resolve_combat_damage=lambda _game: []),
            check_state_based_actions=lambda _game: [],
            drain_pending_triggers=drain,
        )
        cog = SimpleNamespace(
            engine=engine,
            _autoplay_send=lambda _thread, content: _record_send(sent, content),
        )

        asyncio.run(MTGGameCog._autoplay_resolve_combat(cog, None, game))

        assert phases_seen == [Phase.COMBAT_DAMAGE]
        assert sent == ["Judith resolves"]
        assert game.phase == Phase.MAIN2


async def _record_send(messages, content):
    messages.append(content)


class TestEquipCostLivePaths:
    def _equip_game(self, make_game, make_card):
        game = make_game()
        equipment = make_card(
            "Trailblazer's Boots", type_line="Artifact — Equipment",
            oracle_text=("Equipped creature has nonbasic landwalk.\n"
                         "Equip {2} (Activate only as a sorcery.)"),
            power="0", toughness="0")
        creature = make_card("Bear")
        lands = [
            make_card(f"Swamp {idx}", type_line="Basic Land — Swamp",
                      power="0", toughness="0")
            for idx in range(2)
        ]
        game.players[0].battlefield.extend([equipment, creature, *lands])
        return game, equipment, creature, lands

    def test_autoplay_equip_pays_two_mana_and_attaches(
            self, make_game, make_card):
        import asyncio
        from mtg.engine import GameEngine, _activation_mana_cost

        game, equipment, creature, lands = self._equip_game(
            make_game, make_card)
        engine = GameEngine(None)
        parsed_cost = next(
            line for line in equipment.oracle_text.splitlines()
            if line.lower().startswith("equip "))
        assert _activation_mana_cost(parsed_cost) == "{2}", repr(parsed_cost)

        result = asyncio.run(engine._execute_action(game, 0, {
            "type": "activate", "permanent": equipment.name,
            "ability": 0, "target": creature.name,
        }))

        assert all(land.tapped for land in lands)
        assert equipment.attached_to == creature.id
        assert equipment.id in creature.attachments
        assert "equips" in result

    def test_manual_equip_pays_two_mana_without_tier3(
            self, make_game, make_card):
        import asyncio
        from mtg.cog import MTGGameCog
        from mtg.engine import GameEngine

        game, equipment, creature, lands = self._equip_game(
            make_game, make_card)
        engine = GameEngine(None)
        engine.save_game = lambda _game: None
        sent = []
        cog = object.__new__(MTGGameCog)
        cog.engine = engine
        ctx = SimpleNamespace(send=lambda content: _record_send(sent, content))

        asyncio.run(MTGGameCog._activate_permanent(
            cog, ctx, game, game.players[0], 0,
            equipment, "1", creature.name))

        assert all(land.tapped for land in lands)
        assert equipment.attached_to == creature.id
        assert sent == [f"⚔️ **{equipment.name}** equipped to **{creature.name}**"]


class TestScryfallOracleCacheValidator:
    def test_detects_stale_top_level_and_face_oracle_text(self):
        from tools.validate_card_names import (
            build_oracle_index, find_oracle_mismatches)

        bulk = [{
            "name": "Front // Back", "oracle_text": "",
            "card_faces": [
                {"name": "Front", "oracle_text": "Current front text"},
                {"name": "Back", "oracle_text": "Current back text"},
            ],
        }, {
            "name": "Cloudblazer", "oracle_text": "Draw two cards.",
        }]
        cache = {
            "cloudblazer": {
                "name": "Cloudblazer", "oracle_text": "Draw a card."},
            "front // back": {
                "name": "Front // Back", "oracle_text": "",
                "card_faces": [
                    {"name": "Front", "oracle_text": "Old front text"},
                    {"name": "Back", "oracle_text": "Current back text"},
                ],
            },
        }

        mismatches = find_oracle_mismatches(
            cache, build_oracle_index(bulk))

        assert [name for name, _cached, _current in mismatches] == [
            "Cloudblazer", "Front"]

    def test_ignores_line_endings_and_surrounding_whitespace(self):
        from tools.validate_card_names import (
            build_oracle_index, find_oracle_mismatches)

        bulk = [{"name": "Card", "oracle_text": "First\nSecond"}]
        cache = {"card": {
            "name": "Card", "oracle_text": " First\r\nSecond \n"}}

        assert find_oracle_mismatches(
            cache, build_oracle_index(bulk)) == []

    def test_empty_duplicate_cannot_overwrite_canonical_oracle(self):
        from tools.validate_card_names import build_oracle_index

        index = build_oracle_index([
            {"name": "Arcane Signet", "oracle_text": "{T}: Add mana."},
            {"name": "Arcane Signet", "oracle_text": ""},
        ])

        assert index["arcane signet"]["oracle_text"] == "{T}: Add mana."

    def test_standalone_card_outranks_same_name_face_and_token(self):
        from tools.validate_card_names import build_oracle_index

        index = build_oracle_index([
            {"name": "Prepared Card", "layout": "prepare",
             "oracle_text": "", "card_faces": [
                 {"name": "Replenish", "oracle_text": "Prepared face"}]},
            {"name": "Replenish", "layout": "normal",
             "oracle_text": "Standalone oracle"},
            {"name": "Replenish", "layout": "token",
             "oracle_text": "Token reminder"},
        ])

        assert index["replenish"]["oracle_text"] == "Standalone oracle"


class TestPrivateDrawDisplay:
    def test_effect_draw_does_not_reveal_non_claude_players_card(
            self, make_game, make_card, rules):
        """June 11 smaller queue: public effect text leaked Rick's hand."""
        game = make_game()
        secret = make_card("Secret Topdeck", type_line="Instant")
        game.players[0].library.append(secret)

        message = rules._execute_action_on_state(game, {
            "action": "draw_cards", "player": "Rick", "amount": 1})

        assert secret in game.players[0].hand
        assert "draws 1 card(s)" in message
        assert secret.name not in message

    def test_trigger_source_has_no_public_card_name_branch(self):
        """Pin the four hardcoded trigger sites as well as the action path."""
        from pathlib import Path

        source = (Path(__file__).resolve().parent.parent / "mtg" / "triggers.py").read_text(
            encoding="utf-8")
        assert "draws **{drawn_cards[0].name}**" not in source
        assert "draws **{drawn[0].name}**" not in source


class TestHammerOfNazahnAttach:
    def test_self_etb_is_real_attach_not_internal_placeholder(
            self, lib, make_game, make_card, rules):
        game = make_game()
        hammer = make_card(
            "Hammer of Nazahn", type_line="Legendary Artifact — Equipment",
            oracle_text=("Whenever Hammer of Nazahn or another Equipment you control "
                         "enters, you may attach that Equipment to target creature you "
                         "control.\nEquipped creature gets +2/+0 and has "
                         "indestructible.\nEquip {4}"))
        bear = make_card("Bear")
        game.players[0].battlefield.extend([hammer, bear])
        from rules.effect_templates import build_game_context
        ctx = build_game_context(
            game, game.players[0], game.players[1], card=hammer)

        actions, description = lib.resolve_etb(
            hammer.name, hammer.oracle_text.splitlines()[0],
            "Rick", "Claude", game_context=ctx)
        message = rules._execute_action_on_state(game, actions[0])

        assert actions[0]["action"] == "equip"
        assert hammer.attached_to == bear.id
        assert "another Equipment you control enters" in description
        assert "handled by equipment system" not in message

    def test_later_equipment_enters_and_attaches_for_free(
            self, make_game, make_card, rules):
        from mtg.triggers import _check_equipment_etb_watchers

        game = make_game()
        hammer = make_card(
            "Hammer of Nazahn", type_line="Legendary Artifact — Equipment")
        boots = make_card(
            "Trailblazer's Boots", type_line="Artifact — Equipment")
        bear = make_card("Bear")
        game.players[0].battlefield.extend([hammer, boots, bear])
        engine = SimpleNamespace(rules=rules)

        messages = _check_equipment_etb_watchers(
            engine, game, game.players[0], boots)

        assert boots.attached_to == bear.id
        assert messages and "Hammer of Nazahn" in messages[0]


class TestTokenDeathWording:
    def test_destroyed_token_dies_then_ceases_in_direct_action(
            self, make_game, make_card, rules):
        """June 11 game ...1143: a Golem token was left/reported as a card."""
        game = make_game()
        golem = make_card(
            "Golem", type_line="Token Artifact Creature — Golem")
        golem.is_token = True
        game.players[0].battlefield.append(golem)

        message = rules._execute_action_on_state(
            game, {"action": "destroy", "card": "Golem"})

        assert golem not in game.players[0].battlefield
        assert golem not in game.players[0].graveyard
        assert "destroyed, then ceases to exist" in message


class TestBloodchiefAscensionEndStep:
    ORACLE = (
        "At the beginning of each end step, if an opponent lost 2 or more life "
        "this turn, you may put a quest counter on this enchantment. "
        "(Damage causes loss of life.)\nWhenever a card is put into an "
        "opponent's graveyard from anywhere, if this enchantment has three or "
        "more quest counters on it, you may have that player lose 2 life. If "
        "you do, you gain 2 life.")

    def test_life_loss_is_tracked_and_end_step_adds_counter_not_drain(
            self, make_game, make_card, rules, lib):
        from rules.effect_templates import build_game_context

        game = make_game()
        ascension = make_card(
            "Bloodchief Ascension", type_line="Enchantment",
            oracle_text=self.ORACLE)
        game.players[0].battlefield.append(ascension)
        rules._execute_action_on_state(game, {
            "action": "lose_life", "player": "Claude", "amount": 2})
        ctx = build_game_context(
            game, game.players[0], game.players[1], card=ascension)

        actions, description = lib.resolve_etb(
            ascension.name, self.ORACLE.splitlines()[0], "Rick", "Claude",
            game_context=ctx, event_type="end_step")
        before_life = [player.life for player in game.players]
        message = rules._execute_action_on_state(game, actions[0])

        assert actions == [{
            "action": "add_counters", "card": "Bloodchief Ascension",
            "player": "Rick", "counter_type": "quest", "amount": 1,
            "source": "Bloodchief Ascension"}]
        assert ascension.counters["quest"] == 1
        assert [player.life for player in game.players] == before_life
        assert "quest counter" in description
        assert "quest" in message

    def test_condition_does_not_fire_below_two_life(self, lib):
        actions, _ = lib.resolve_etb(
            "Bloodchief Ascension", self.ORACLE.splitlines()[0],
            "Rick", "Claude",
            game_context={"opponent_life_lost_this_turn": 1},
            event_type="end_step")

        assert actions[0]["action"] == "no_action"
        assert all(action["action"] not in ("lose_life", "gain_life")
                   for action in actions)

    def test_live_end_turn_fires_once_then_resets_life_loss_ledger(
            self, make_game, make_card):
        from mtg.engine import GameEngine

        game = make_game()
        ascension = make_card(
            "Bloodchief Ascension", type_line="Enchantment",
            oracle_text=self.ORACLE)
        game.players[0].battlefield.append(ascension)
        game.players[1].life -= 2
        game.players[1].record_life_loss(2)
        engine = GameEngine(None)

        engine.end_turn(game)

        assert ascension.counters["quest"] == 1
        assert [player.life_lost_this_turn for player in game.players] == [0, 0]


class TestJuly12SpellKillGameEnding:
    """Game #84 (burn vs jund): spell kills emitted a custom '💀 X has been
    defeated!' as its own Discord send, which raced the autoplay main loop's
    🏆 summary (trophy landed between damage line and defeat line) and didn't
    match the standard SBA loss wording. Spell deaths now route through SBA
    and the loss line rides the damage/life-loss message atomically."""

    @staticmethod
    def _resolver():
        from rules.spell_resolver import SpellResolver
        return object.__new__(SpellResolver)

    def test_burn_kill_uses_standard_sba_loss_line(
            self, make_game, make_card, rules):
        import asyncio

        game = make_game("legacy")
        game._rules_engine = rules
        game.players[1].life = 3
        effect = SimpleNamespace(amount=3, raw_text="deal 3 damage")
        ctx = SimpleNamespace(
            targets=[game.players[1]],
            source_card=make_card("Skullcrack", type_line="Instant"),
            source_controller=game.players[0])

        messages = asyncio.run(self._resolver()._exec_damage(effect, ctx, game))

        assert game.ended is True and game.winner == 0
        joined = "\n".join(messages)
        assert "has been defeated" not in joined
        assert "loses the game" in joined
        # Atomic: the loss line rides the damage message in ONE send, so the
        # racing 🏆 summary can never post between them.
        assert any("deals 3 damage" in m and "loses the game" in m
                   for m in messages)

    def test_life_loss_kill_uses_standard_sba_loss_line(
            self, make_game, make_card, rules):
        import asyncio

        game = make_game("legacy")
        game._rules_engine = rules
        game.players[1].life = 2
        effect = SimpleNamespace(amount=2, raw_text="lose 2 life")
        ctx = SimpleNamespace(
            targets=[game.players[1]],
            source_card=make_card("Bump in the Night", type_line="Sorcery"),
            source_controller=game.players[0])

        messages = asyncio.run(self._resolver()._exec_life_loss(effect, ctx, game))

        assert game.ended is True and game.winner == 0
        joined = "\n".join(messages)
        assert "has been defeated" not in joined
        assert any("loses 2 life" in m and "loses the game" in m
                   for m in messages)

    def test_survivor_does_not_end_game(self, make_game, make_card, rules):
        import asyncio

        game = make_game("legacy")
        game._rules_engine = rules
        effect = SimpleNamespace(amount=3, raw_text="deal 3 damage")
        ctx = SimpleNamespace(
            targets=[game.players[1]],
            source_card=make_card("Skullcrack", type_line="Instant"),
            source_controller=game.players[0])

        messages = asyncio.run(self._resolver()._exec_damage(effect, ctx, game))

        assert game.ended is False
        assert all("loses the game" not in m for m in messages)


class TestJuly11TargetLegality:
    def test_teferi_restricts_flash_and_instants(self, make_game, make_card, rules):
        from mtg.constants import Phase

        game = make_game()
        game.phase = Phase.COMBAT_DAMAGE
        teferi = make_card(
            "Teferi, Time Raveler", type_line="Legendary Planeswalker — Teferi",
            oracle_text="Each opponent can cast spells only any time they could cast a sorcery.")
        flash_spell = make_card(
            "Vendilion Clique", type_line="Legendary Creature — Faerie Wizard",
            oracle_text="Flash")
        game.players[0].battlefield.append(teferi)

        legal, reason = rules.can_cast_spell(game, game.players[1], flash_spell)

        assert not legal
        assert "Teferi" in reason

    def test_phased_out_permanent_is_not_a_target(self, make_game, make_card):
        from rules.targeting_helpers import (
            _find_any_valid_target, _validate_target_for_action)

        game = make_game()
        doomed = make_card("Phased Bear")
        doomed._phased_out = True
        game.players[1].battlefield.append(doomed)
        removal = make_card(
            "Murder", type_line="Instant",
            oracle_text="Destroy target creature.")

        assert not _find_any_valid_target(game, removal, "Rick")
        legal, reason = _validate_target_for_action(
            game, doomed, game.players[1], removal, "Rick")
        assert not legal
        assert "phased out" in reason

    @pytest.mark.parametrize("source_name,source_oracle,target_type,origin", [
        ("Dispel", "Counter target instant spell.", "Creature — Bear", "hand"),
        ("Force of Negation", "Counter target noncreature spell.",
         "Creature — Bear", "hand"),
        ("Wash Away", "Cleave {3}{U}. Counter target spell that wasn't cast "
         "from its owner's hand.", "Sorcery", "hand"),
    ])
    def test_counter_type_restrictions_fizzle_centrally(
            self, make_game, make_card, rules,
            source_name, source_oracle, target_type, origin):
        from mtg.models import StackEntry

        game = make_game()
        target = make_card("Illegal Counter Target", type_line=target_type)
        target._cast_origin = origin
        entry = StackEntry(target, "Claude", 1)
        game.stack.append(entry)

        message = rules._execute_action_on_state(game, {
            "action": "counter_spell", "target": "stack_top",
            "_source_card_name": source_name, "_source_oracle": source_oracle,
        })

        assert not entry.countered
        assert "fizzles" in message

    def test_wash_away_can_counter_command_zone_spell(
            self, make_game, make_card, rules):
        from mtg.models import StackEntry

        game = make_game()
        target = make_card("Commander", type_line="Legendary Creature — Bear")
        target._cast_origin = "command_zone"
        entry = StackEntry(target, "Claude", 1)
        game.stack.append(entry)

        rules._execute_action_on_state(game, {
            "action": "counter_spell", "target": "stack_top",
            "_source_card_name": "Wash Away",
            "_source_oracle": "Counter target spell that wasn't cast from its owner's hand.",
        })

        assert entry.countered

    def test_animate_dead_reanimates_declared_not_highest_power_target(
            self, make_game, make_card):
        import asyncio
        from mtg.engine import GameEngine
        from mtg.spells import cast_spell_async

        game = make_game()
        aura = make_card(
            "Animate Dead", type_line="Enchantment — Aura", mana_cost="{1}{B}", cmc=2,
            oracle_text="Enchant creature card in a graveyard\nWhen this Aura enters, "
            "if it's on the battlefield, it loses 'enchant creature card in a graveyard' "
            "and gains 'enchant creature put onto the battlefield with Animate Dead.' "
            "Return enchanted creature card to the battlefield under your control and attach Animate Dead to it.")
        small = make_card("Small Target", power="1", toughness="1")
        bomb = make_card("Big Target", power="10", toughness="10")
        game.players[0].hand.append(aura)
        game.players[0].graveyard.extend([small, bomb])
        game.players[0].battlefield.extend([
            make_card("Swamp", type_line="Basic Land — Swamp",
                      oracle_text="{T}: Add {B}.", power="0", toughness="0"),
            make_card("Wastes", type_line="Basic Land",
                      oracle_text="{T}: Add {C}.", power="0", toughness="0"),
        ])
        engine = GameEngine(None)

        success, _, _ = asyncio.run(cast_spell_async(
            engine, game, game.players[0], aura, pay_mana=False, target=small))

        assert success
        assert small in game.players[0].battlefield
        assert bomb in game.players[0].graveyard
        assert aura.attached_to == small.id


class TestJuly11PartialEffects:
    def test_aminatou_plus_one_is_draw_then_put_back(self, lib):
        actions, _ = lib.resolve_pw_ability(
            "Aminatou, the Fateshifter",
            "Draw a card, then put a card from your hand on top of your library.",
            "Rick", "Claude", {})

        assert [action["action"] for action in actions] == [
            "draw_cards", "put_back_from_hand"]
        assert actions[1]["count"] == 1

    def test_teferi_minus_three_honors_declared_target(self, lib):
        actions, _ = lib.resolve_pw_ability(
            "Teferi, Time Raveler",
            "Return up to one target artifact, creature, or enchantment to its owner's hand. Draw a card.",
            "Rick", "Claude",
            {"explicit_target_name": "My Mana Rock",
             "explicit_target_owner": "Rick",
             "best_opponent_nonland": "Wrong Target"})

        assert actions[0] == {
            "action": "move_card", "card": "My Mana Rock",
            "from_zone": "battlefield", "to_zone": "hand", "player": "Rick"}
        assert actions[1]["action"] == "draw_cards"

    def test_memory_lapse_routes_countered_spell_to_library_top(self, lib):
        actions, _ = lib.resolve_spell(
            "Memory Lapse", "Counter target spell. If that spell is countered this way, "
            "put it on top of its owner's library instead of into that player's graveyard.",
            "Rick", "Claude", {"stack_top_spell": "Bear"})

        assert actions[0]["countered_to"] == "library_top"

    def test_natures_claim_includes_mandatory_life_rider(self, lib):
        actions, _ = lib.resolve_spell(
            "Nature's Claim",
            "Destroy target artifact or enchantment. Its controller gains 4 life.",
            "Rick", "Claude",
            {"explicit_target_name": "Rick's Clue",
             "explicit_target_owner": "Rick"})

        assert actions == [
            {"action": "destroy", "card": "Rick's Clue"},
            {"action": "gain_life", "player": "Rick", "amount": 4},
        ]

    def test_unbreakable_formation_addendum_and_creature_scope(self, lib):
        actions, _ = lib.resolve_spell(
            "Unbreakable Formation", "Creatures you control gain indestructible until end of turn.\n"
            "Addendum — If you cast this spell during your main phase, put a +1/+1 counter "
            "on each of those creatures and they gain vigilance until end of turn.",
            "Rick", "Claude", {"phase": "main1"})

        assert [a["action"] for a in actions] == [
            "grant_keywords", "add_counters", "grant_keywords"]
        assert all(a["target"] == "all_own_creatures" for a in actions)
        assert actions[1]["counter_type"] == "+1/+1"
        assert actions[2]["keywords"] == ["Vigilance"]

    def test_calix_minus_three_tracks_exile_to_chosen_enchantment(
            self, make_game, make_card, rules, lib):
        game = make_game()
        prison = make_card("Prison Realm", type_line="Enchantment")
        victim = make_card("Victim")
        game.players[0].battlefield.append(prison)
        game.players[1].battlefield.append(victim)
        actions, _ = lib.resolve_pw_ability(
            "Calix, Destiny's Hand",
            "Exile target creature or enchantment you don't control until target enchantment you control leaves the battlefield.",
            "Rick", "Claude",
            {"_pw_targets": [victim, prison], "explicit_target_name": victim.name,
             "explicit_target_owner": "Claude", "controller_battlefield": [prison]})

        for action in actions:
            rules._execute_action_on_state(game, action)
        assert victim in game.players[1].exile
        from mtg.triggers import _check_ltb_triggers_sync
        _check_ltb_triggers_sync(
            SimpleNamespace(), game, prison, game.players[0], "graveyard")
        assert victim in game.players[1].battlefield
        assert victim not in game.players[1].exile

    def test_agent_control_does_not_end_when_agent_leaves(
            self, make_game, make_card, rules):
        game = make_game()
        agent = make_card("Agent of Treachery")
        victim = make_card("Victim")
        game.players[0].battlefield.append(agent)
        game.players[1].battlefield.append(victim)

        rules._execute_action_on_state(game, {
            "action": "steal_permanent", "player": "Rick",
            "from_player": "Claude", "card": "Victim",
            "source": "Agent of Treachery"})
        from mtg.triggers import _check_ltb_triggers_sync
        _check_ltb_triggers_sync(
            SimpleNamespace(), game, agent, game.players[0], "graveyard")

        assert victim in game.players[0].battlefield
        assert victim.control_gained_by is None

    @pytest.mark.parametrize("zenith_name,mana_cost,land_type,land_oracle,oracle", [
        ("Blue Sun's Zenith", "{X}{U}{U}{U}", "Basic Land — Island", "{T}: Add {U}.",
         "Target player draws X cards. Shuffle Blue Sun's Zenith into its owner's library."),
        ("Green Sun's Zenith", "{X}{G}", "Basic Land — Forest", "{T}: Add {G}.",
         "Search your library for a green creature card with mana value X or less, "
         "put it onto the battlefield, then shuffle. Shuffle Green Sun's Zenith into its owner's library."),
    ])
    def test_zenith_resolves_to_library_not_graveyard(
            self, make_game, make_card, zenith_name, mana_cost, land_type,
            land_oracle, oracle):
        import asyncio
        from mtg.engine import GameEngine
        from mtg.spells import cast_spell_async

        game = make_game()
        zenith = make_card(
            zenith_name, type_line="Sorcery", mana_cost=mana_cost,
            oracle_text=oracle)
        game.players[0].hand.append(zenith)
        game.players[0].battlefield.extend([
            make_card(f"Source {i}", type_line=land_type,
                      oracle_text=land_oracle, power="0", toughness="0")
            for i in range(3)
        ])
        game.players[1].library.append(make_card("Opponent Draw", type_line="Sorcery"))
        engine = GameEngine(None)

        success, _, _ = asyncio.run(cast_spell_async(
            engine, game, game.players[0], zenith, pay_mana=False,
            target=game.players[1]))

        assert success
        assert zenith in game.players[0].library
        assert zenith not in game.players[0].graveyard

    def test_thrasios_includes_reveal_and_land_or_draw_rider(
            self, make_game, make_card, lib):
        game = make_game()
        top_land = make_card("Forest", type_line="Basic Land — Forest")
        game.players[0].library.append(top_land)

        actions, _ = lib.resolve_etb(
            "Thrasios, Triton Hero",
            "Scry 1, then reveal the top card of your library. If it's a land card, "
            "put it onto the battlefield tapped. Otherwise, draw a card.",
            "Rick", "Claude", {"controller_library": game.players[0].library})

        assert [a["action"] for a in actions] == ["scry", "move_card", "tap"]
        assert actions[1]["card"] == "Forest"


class TestJuly11MissingTriggers:
    SOULHERDER_ORACLE = (
        "Whenever a creature is exiled from the battlefield, put a +1/+1 counter on Soulherder.\n"
        "At the beginning of your end step, you may exile another target creature you control, "
        "then return that card to the battlefield under its owner's control.")

    def test_soulherder_counts_generic_battlefield_exile(
            self, make_game, make_card, rules):
        game = make_game()
        soulherder = make_card("Soulherder", oracle_text=self.SOULHERDER_ORACLE)
        victim = make_card("Victim")
        game.players[0].battlefield.extend([soulherder, victim])

        rules._execute_action_on_state(game, {
            "action": "move_card", "card": "Victim", "player": "Rick",
            "from_zone": "battlefield", "to_zone": "exile"})

        assert soulherder.counters["+1/+1"] == 1

    def test_mass_flicker_runs_own_etb_and_panharmonicon_once_extra(
            self, make_game, make_card, rules):
        game = make_game()
        panharmonicon = make_card("Panharmonicon", type_line="Artifact")
        cloudblazer = make_card(
            "Cloudblazer", oracle_text="When Cloudblazer enters, you gain 2 life and draw two cards.")
        game.players[0].battlefield.extend([panharmonicon, cloudblazer])
        game.players[0].library.extend([
            make_card(f"Draw {i}", type_line="Sorcery") for i in range(6)])
        rules.engine_ref = SimpleNamespace(
            _handle_etb_triggers=lambda game, player, card: [])

        message = rules._execute_action_on_state(game, {
            "action": "mass_flicker", "player": "Rick", "count": 1,
            "exclude_lands": True, "require_ownership": False})

        assert len(game.players[0].hand) == 4
        assert game.players[0].life == 44
        assert "Panharmonicon doubles" in message

    def test_ohran_frostfang_draws_for_other_creature_damage(
            self, make_game, make_card, rules):
        from mtg.combat import resolve_combat_damage

        game = make_game()
        frostfang = make_card(
            "Ohran Frostfang",
            oracle_text="Whenever a creature you control deals combat damage to a player, draw a card.")
        attacker = make_card("Vanilla Attacker")
        game.players[0].battlefield.extend([frostfang, attacker])
        game.players[0].library.append(make_card("Reward", type_line="Sorcery"))
        game._combat_damage_to_player = [(attacker, game.players[0], 2)]

        resolve_combat_damage(rules, game)

        assert [card.name for card in game.players[0].hand] == ["Reward"]

    def test_the_abyss_uses_active_players_nonartifact_creature(self, make_game, make_card, lib):
        game = make_game()
        game.active_player_index = 1
        artifact_creature = make_card("Myr", type_line="Artifact Creature — Myr", cmc=1)
        legal_choice = make_card("Bear", cmc=2)
        game.players[1].battlefield.extend([artifact_creature, legal_choice])

        actions, _ = lib.resolve_upkeep_trigger(
            "The Abyss",
            "At the beginning of each player's upkeep, destroy target nonartifact creature that player controls of their choice.",
            "Rick", "Claude", {"_game": game})

        assert actions == [{
            "action": "destroy", "card": "Bear", "target_controller": "Claude"}]


class TestJuly11CostsAndLinkedState:
    @staticmethod
    def _land(make_card, name, subtype):
        symbol = {"Plains": "W", "Island": "U", "Swamp": "B",
                  "Mountain": "R", "Forest": "G"}[subtype]
        return make_card(
            name, type_line=f"Basic Land — {subtype}",
            oracle_text=f"{{T}}: Add {{{symbol}}}.", power="0", toughness="0")

    def test_hybrid_activation_cost_is_preserved(self):
        from mtg.engine import _activation_mana_cost

        assert _activation_mana_cost("{2}{G/W}, {T}") == "{2}{G/W}"
        assert _activation_mana_cost("{4}{G/W}{G/W}, {T}") == "{4}{G/W}{G/W}"

    def test_wishclaw_spends_counter_tutors_and_transfers(
            self, make_game, make_card):
        import asyncio
        from mtg.engine import GameEngine

        game = make_game()
        talisman = make_card(
            "Wishclaw Talisman", type_line="Artifact",
            oracle_text="This artifact enters with three wish counters on it.\n"
            "{1}, {T}, Remove a wish counter from this artifact: Search your library "
            "for a card, put it into your hand, then shuffle. An opponent gains control "
            "of this artifact. Activate only during your turn.")
        talisman.counters["wish"] = 3
        land = self._land(make_card, "Swamp", "Swamp")
        prize = make_card("Prize", type_line="Sorcery", cmc=7)
        game.players[0].battlefield.extend([talisman, land])
        game.players[0].library.append(prize)
        engine = GameEngine(None)

        result = asyncio.run(engine._execute_action(game, 0, {
            "type": "activate", "permanent": talisman.name, "ability": 0}))

        assert talisman.counters["wish"] == 2
        assert land.tapped and talisman.tapped
        assert prize in game.players[0].hand
        assert talisman in game.players[1].battlefield
        assert "tutors" in result

    def test_rhys_second_ability_pays_six_and_copies_each_token(
            self, make_game, make_card):
        import asyncio
        from mtg.engine import GameEngine

        game = make_game()
        rhys = make_card(
            "Rhys the Redeemed",
            oracle_text="{2}{G/W}, {T}: Create a 1/1 green and white Elf Warrior creature token.\n"
            "{4}{G/W}{G/W}, {T}: For each creature token you control, create a token "
            "that's a copy of that creature.")
        tokens = [make_card("Elf Warrior") for _ in range(2)]
        for token in tokens:
            token.is_token = True
        lands = [self._land(make_card, f"Forest {i}", "Forest") for i in range(6)]
        game.players[0].battlefield.extend([rhys, *tokens, *lands])
        engine = GameEngine(None)

        result = asyncio.run(engine._execute_action(game, 0, {
            "type": "activate", "permanent": rhys.name, "ability": 1}))

        creature_tokens = [c for c in game.players[0].battlefield
                           if getattr(c, 'is_token', False)]
        assert len(creature_tokens) == 4
        assert all(land.tapped for land in lands)
        assert rhys.tapped
        assert "token copies" in result

    def test_extort_pays_real_hybrid_source_and_gains_only_life_lost(
            self, make_game, make_card, rules, lib):
        from rules.effect_templates import build_game_context

        game = make_game()
        extort = make_card("Blind Obedience", type_line="Enchantment", oracle_text="Extort")
        plains = self._land(make_card, "Plains", "Plains")
        game.players[0].battlefield.extend([extort, plains])
        game.players[1]._life_total_locked = True
        game.players[1]._life_total_locked_expires_turn = 99
        ctx = build_game_context(game, game.players[0], game.players[1], card=extort)

        actions, _ = lib.resolve_etb(
            "Blind Obedience", "Extort", "Rick", "Claude",
            game_context=ctx, event_type="cast_trigger")
        before = game.players[0].life
        message = rules._execute_action_on_state(game, actions[0])

        assert plains.tapped
        assert game.players[1].life == 40
        assert game.players[0].life == before
        assert "can't change" in message

    def test_isochron_keeps_one_linked_exile_across_activations(
            self, make_game, make_card, rules):
        game = make_game()
        scepter = make_card("Isochron Scepter", type_line="Artifact")
        opt = make_card(
            "Opt", type_line="Instant", cmc=1,
            oracle_text="Scry 1. Draw a card.")
        game.players[0].battlefield.append(scepter)
        game.players[0].hand.append(opt)
        game.players[0].library.extend([
            make_card("Draw One", type_line="Sorcery"),
            make_card("Draw Two", type_line="Sorcery")])

        rules._execute_action_on_state(game, {
            "action": "imprint_isochron", "player": "Rick",
            "source": "Isochron Scepter"})
        linked_id = scepter._imprinted_card_id
        rules._execute_action_on_state(game, {
            "action": "isochron_copy", "player": "Rick",
            "source": "Isochron Scepter"})
        rules._execute_action_on_state(game, {
            "action": "isochron_copy", "player": "Rick",
            "source": "Isochron Scepter"})

        assert scepter._imprinted_card_id == linked_id == opt.id
        assert game.players[0].exile == [opt]

    def test_attack_tax_is_actually_deducted_per_attacker(
            self, make_game, make_card, rules):
        from mtg.constants import Phase

        game = make_game()
        game.phase = Phase.DECLARE_ATTACKERS
        prison = make_card(
            "Ghostly Prison", type_line="Enchantment",
            oracle_text="Creatures can't attack you unless their controller pays {2} for each creature they control that's attacking you.")
        game.players[1].battlefield.append(prison)
        attackers = [make_card(f"Attacker {i}") for i in range(3)]
        lands = [self._land(make_card, f"Plains {i}", "Plains") for i in range(4)]
        game.players[0].battlefield.extend([*attackers, *lands])

        assert rules.pay_attack_tax(game, game.players[0], attackers[0])[0]
        assert sum(land.tapped for land in lands) == 2
        assert rules.pay_attack_tax(game, game.players[0], attackers[1])[0]
        assert sum(land.tapped for land in lands) == 4
        assert not rules.pay_attack_tax(game, game.players[0], attackers[2])[0]


class TestJuly11GrowingRites:
    ORACLE = (
        "When Growing Rites of Itlimoc enters, look at the top four cards of your library. "
        "You may reveal a creature card from among them and put it into your hand. "
        "Put the rest on the bottom of your library in any order.\n"
        "At the beginning of your end step, if you control four or more creatures, "
        "transform Growing Rites of Itlimoc.")

    def test_etb_moves_one_creature_and_all_other_looked_cards_to_bottom(
            self, make_game, make_card, rules, lib):
        game = make_game()
        top = [
            make_card("Spell A", type_line="Sorcery"),
            make_card("Found Bear"),
            make_card("Spell B", type_line="Instant"),
            make_card("Spell C", type_line="Sorcery"),
        ]
        untouched = make_card("Untouched", type_line="Sorcery")
        game.players[0].library.extend([*top, untouched])
        actions, _ = lib.resolve_etb(
            "Growing Rites of Itlimoc", self.ORACLE.splitlines()[0],
            "Rick", "Claude", {"controller_library": game.players[0].library})

        rules._execute_action_on_state(game, actions[0])

        assert [c.name for c in game.players[0].hand] == ["Found Bear"]
        assert game.players[0].library[0] is untouched
        assert {c.name for c in game.players[0].library[1:]} == {
            "Spell A", "Spell B", "Spell C"}

    def test_end_step_transforms_only_at_four_creatures(
            self, make_game, make_card, rules):
        from mtg.triggers import _check_end_step_triggers_sync

        game = make_game()
        rites = make_card(
            "Growing Rites of Itlimoc", type_line="Legendary Enchantment",
            oracle_text=self.ORACLE)
        rites.back_face_name = "Itlimoc, Cradle of the Sun"
        rites.has_transform = True
        rites.back_face_type_line = "Legendary Land"
        rites.back_face_oracle_text = (
            "{T}: Add {G}.\n{T}: Add {G} for each creature you control.")
        game.players[0].battlefield.append(rites)
        game.players[0].battlefield.extend(
            make_card(f"Creature {i}") for i in range(4))
        engine = SimpleNamespace(rules=rules)

        messages, unhandled = _check_end_step_triggers_sync(engine, game)

        assert rites.name == "Itlimoc, Cradle of the Sun"
        assert rites.is_land()
        assert not unhandled
        assert any("transforms" in message for message in messages)


class TestJuly11OperationalBackpressure:
    def test_card_cache_save_runs_through_to_thread(self, monkeypatch, tmp_path):
        import asyncio
        import json
        from mtg.deck_loader import DeckLoader

        loader = object.__new__(DeckLoader)
        loader.card_cache = {"bear": {"name": "Bear"}}
        cache_path = tmp_path / "card-cache.json"
        monkeypatch.setattr(DeckLoader, "CARD_DATA_CACHE_PATH", str(cache_path))
        monkeypatch.setattr(DeckLoader, "_disk_cache_lock", None)
        calls = []

        async def fake_to_thread(fn, *args):
            calls.append(fn)
            return fn(*args)

        monkeypatch.setattr(asyncio, "to_thread", fake_to_thread)
        asyncio.run(loader._save_disk_cache_async())

        assert calls == [DeckLoader._write_disk_cache_snapshot]
        assert json.loads(cache_path.read_text(encoding="utf-8"))["bear"]["name"] == "Bear"

    def test_parallel_autoplay_thread_creation_is_serialized(self, monkeypatch):
        import asyncio
        import mtg.autoplay as autoplay

        monkeypatch.setattr(autoplay, "_THREAD_CREATE_LOCK", None)
        monkeypatch.setattr(autoplay, "_THREAD_CREATE_LAST", 0.0)
        sleeps = []

        async def fake_sleep(delay):
            sleeps.append(delay)

        monkeypatch.setattr(autoplay.asyncio, "sleep", fake_sleep)

        class Channel:
            def __init__(self):
                self.calls = 0

            async def create_thread(self, **kwargs):
                self.calls += 1
                return SimpleNamespace(id=self.calls, **kwargs)

        channel = Channel()

        async def run_two():
            await autoplay._create_autoplay_thread(channel, "one")
            await autoplay._create_autoplay_thread(channel, "two")

        asyncio.run(run_two())

        assert channel.calls == 2
        assert sleeps and sleeps[-1] > 0
