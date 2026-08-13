"""Focused pins for the Aug-13 next-batch coverage fixes.

Each test exercises the decision which was absent in the clean e004162
batch, together with a control which would regress if the handler simply
matched a card name or nearby precondition.
"""
from pathlib import Path

from conftest import _make_card, _make_game


def _engine(game):
    from mtg.engine import GameEngine

    engine = GameEngine(None)
    game._rules_engine = engine.rules
    engine.rules.engine_ref = engine
    return engine


class TestBlizzardStrix:
    def test_exiles_named_target_then_returns_it_at_next_end_step(self):
        from rules.effect_templates import get_effect_library

        game = _make_game()
        engine = _engine(game)
        controller, opponent = game.players
        controller.battlefield.append(_make_card(
            "Snow-Covered Forest", type_line="Basic Snow Land — Forest"))
        target = _make_card("Opponent's Relic", type_line="Artifact")
        opponent.battlefield.append(target)
        actions, _ = get_effect_library().resolve_etb(
            "Blizzard Strix", "", controller.name, opponent.name,
            {"explicit_target_name": target.name,
             "explicit_target_owner": opponent.name,
             "controller_has_other_snow_permanent": True},
        )

        assert [a["action"] for a in actions] == [
            "move_card", "schedule_delayed_trigger"]
        for action in actions:
            engine.rules._execute_action_on_state(game, action)
        assert target in opponent.exile
        assert target not in opponent.battlefield
        assert len(game.delayed_triggers) == 1

        engine._process_delayed_triggers(game, "end_step")
        assert target in opponent.battlefield
        assert target not in opponent.exile
        assert game.delayed_triggers == []

    def test_no_target_neither_moves_a_card_nor_schedules_a_return(self):
        from rules.effect_templates import get_effect_library

        game = _make_game()
        controller, opponent = game.players
        target = _make_card("Untouched Relic", type_line="Artifact")
        opponent.battlefield.append(target)
        actions, _ = get_effect_library().resolve_etb(
            "Blizzard Strix", "", controller.name, opponent.name, {})

        assert actions == [{"action": "no_action",
                            "reason": "Blizzard Strix: no other permanent target"}]
        assert target in opponent.battlefield
        assert not game.delayed_triggers


class TestKillingWave:
    def test_x_one_pays_life_when_possible_and_sacrifices_at_one_life(self):
        game = _make_game()
        engine = _engine(game)
        payer, cannot_pay = game.players
        payer_creature = _make_card("Payer", type_line="Creature — Bear")
        sacrifice_creature = _make_card("Sacrifice", type_line="Creature — Rat")
        payer.battlefield.append(payer_creature)
        cannot_pay.battlefield.append(sacrifice_creature)
        cannot_pay.life = 1

        engine.rules._execute_action_on_state(
            game, {"action": "killing_wave", "x_value": 1,
                   "source": "Killing Wave"})

        assert payer.life == 39
        assert payer_creature in payer.battlefield
        assert sacrifice_creature not in cannot_pay.battlefield
        assert sacrifice_creature in cannot_pay.graveyard

    def test_x_zero_keeps_every_creature_and_costs_no_life(self):
        game = _make_game()
        engine = _engine(game)
        player, opponent = game.players
        creatures = [_make_card("Zero A"), _make_card("Zero B")]
        player.battlefield.append(creatures[0])
        opponent.battlefield.append(creatures[1])
        before = (player.life, opponent.life)

        engine.rules._execute_action_on_state(
            game, {"action": "killing_wave", "x_value": 0,
                   "source": "Killing Wave"})

        assert all(c in p.battlefield for c, p in zip(creatures, game.players))
        assert (player.life, opponent.life) == before


class TestInscriptionOfAbundance:
    def test_gains_target_players_own_greatest_power_not_casters(self):
        from rules.effect_templates import build_game_context, get_effect_library

        game = _make_game()
        caster, opponent = game.players
        caster.battlefield.append(_make_card("Caster Three", power="3"))
        opponent.battlefield.append(_make_card("Opponent Seven", power="7"))
        ctx = build_game_context(game, caster, opponent)
        ctx.update({"_modes": ["life"], "explicit_target_is_player": True,
                    "explicit_target_player": opponent.name})

        actions, _ = get_effect_library().resolve_spell(
            "Inscription of Abundance", "", caster.name, opponent.name, ctx)
        assert actions == [{"action": "gain_life", "player": opponent.name,
                            "amount": 7}]

    def test_self_target_uses_casters_greatest_power(self):
        from rules.effect_templates import build_game_context, get_effect_library

        game = _make_game()
        caster, opponent = game.players
        caster.battlefield.append(_make_card("Caster Three", power="3"))
        opponent.battlefield.append(_make_card("Opponent Seven", power="7"))
        ctx = build_game_context(game, caster, opponent)
        ctx.update({"_modes": ["life"], "explicit_target_is_player": True,
                    "explicit_target_player": caster.name})

        actions, _ = get_effect_library().resolve_spell(
            "Inscription of Abundance", "", caster.name, opponent.name, ctx)
        assert actions == [{"action": "gain_life", "player": caster.name,
                            "amount": 3}]


class TestAluren:
    def test_mv_three_creature_is_free_but_mv_four_is_not(self):
        from mtg.spells import _compute_alt_costs

        game = _make_game()
        engine = _engine(game)
        caster, aluren_controller = game.players
        aluren_controller.battlefield.append(_make_card(
            "Aluren", type_line="Enchantment",
            oracle_text="Any player may cast creature spells with mana value 3 or less without paying their mana costs and as though they had flash."))
        small = _make_card("Small Creature", mana_cost="{1}{G}", cmc=2)
        large = _make_card("Large Creature", mana_cost="{3}{G}", cmc=4)

        rejection, small_costs = _compute_alt_costs(engine, game, caster, small, True, 0)
        assert rejection is None
        assert small_costs["pay_mana"] is False
        assert small_costs["free_cast_source"] == "Aluren"

        rejection, large_costs = _compute_alt_costs(engine, game, caster, large, True, 0)
        assert rejection is None
        assert large_costs["pay_mana"] is True
        assert large_costs["free_cast_source"] is None

    def test_mv_three_creature_has_aluren_instant_speed_permission(self):
        from mtg.constants import Phase

        game = _make_game()
        engine = _engine(game)
        caster, aluren_controller = game.players
        aluren_controller.battlefield.append(_make_card("Aluren", type_line="Enchantment"))
        game.active_player_index = 1
        game.phase = Phase.UPKEEP
        small = _make_card("Small Creature", mana_cost="{1}{G}", cmc=2)

        legal, reason = engine.rules.can_cast_spell(game, caster, small)
        assert legal, reason


class TestTovolarTransformDispatcher:
    def test_tovolar_dispatches_each_new_face_trigger_exactly_once(self, monkeypatch):
        import mtg.triggers as triggers

        game = _make_game()
        engine = _engine(game)
        controller = game.players[0]
        tovolar = _make_card(
            "Tovolar, Dire Overlord", type_line="Legendary Creature — Human Werewolf",
            oracle_text="At the beginning of your upkeep, if you control three or more Wolves and/or Werewolves, it becomes night.")
        piper = _make_card(
            "Howlpack Piper", type_line="Creature — Human Werewolf",
            oracle_text="Daybound", power="2", toughness="2", has_transform=True,
            back_face_name="Wildsong Howler", back_face_type_line="Creature — Werewolf",
            back_face_oracle_text=("Nightbound\nWhenever this creature transforms "
                                  "into Wildsong Howler, draw a card."),
            back_face_power="5", back_face_toughness="5")
        wolves = [_make_card("Wolf One", type_line="Creature — Wolf"),
                  _make_card("Wolf Two", type_line="Creature — Wolf")]
        controller.battlefield.extend([tovolar, piper, *wolves])
        calls = []

        def record_dispatch(_engine, _game, _controller, card):
            calls.append(card.id)
            return []

        monkeypatch.setattr(triggers, "_fire_transforms_into_triggers", record_dispatch)
        triggers._check_upkeep_triggers_sync(engine, game)

        assert piper.name == "Wildsong Howler"
        assert calls == [piper.id]


class TestStackDecisionPendingPin:
    def test_timeout_waits_while_response_decision_is_pending(self):
        """A compact source pin for the two cooperating async coroutines.

        Full live priority needs a Discord/LLM client; this checks the ordering
        which keeps the entry targetable while that client is awaited.
        """
        root = Path(__file__).resolve().parents[1]
        engine_source = (root / "mtg" / "engine.py").read_text(encoding="utf-8")
        spells_source = (root / "mtg" / "spells.py").read_text(encoding="utf-8")
        pending = "top_stack._stack_ai_decision_pending = True"
        assert pending in engine_source
        assert engine_source.index(pending) < engine_source.index(
            "await engine.claude_ai.decide_response")
        assert "top_stack._stack_ai_decision_pending = False" in engine_source
        assert "getattr(stack_entry, '_stack_ai_decision_pending', False)" in spells_source
        assert "keeping stack entry live" in spells_source
