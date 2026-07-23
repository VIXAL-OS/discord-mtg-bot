"""July 23, 2026 audit — the deferred findings, now fixed.

These seven were verified-real in the reviewer wave but held back from the
first fix commit because each needed a mechanism worked out rather than a
one-line change:

  #4  Kitchen Finks' Persist re-triggered on destroy/sacrifice deaths — the
      Persist REMINDER TEXT matched the self-death extraction and was queued
      as an unhandled dies trigger, so Tier 3 returned the creature a second
      time, ungated (a free-life loop). Sacrifice also had no save-chain at
      all, so persist-on-sacrifice depended entirely on that buggy path.
  #7  Necropotence's activated ability was a no-op (no delayed exile->hand).
  #9  Arcane Denial's draws resolved immediately instead of at the next
      turn's upkeep (CR 603.7 delayed triggered abilities).
  #14 Curse of Bloodletting (an "Enchant player" Aura) died to the aura SBA
      because the object-only model saw it as unattached.
  #15 Insult // Injury's turn-long damage doubler was declined by Tier 3.
  #3  Auras kept pointing at a flickered creature (attached_to survived), so
      Animate Dead's -1/-0 followed it through every flicker.
  #16 Savra's two color-qualified sacrifice triggers: only the first clause
      was ever considered and the sacrificed creature's color was ignored.
"""
import pytest


# --------------------------------------------------------------------------- #
# #4 — Persist: gated save-chain on sacrifice, and no Tier-3 escalation
# --------------------------------------------------------------------------- #
PERSIST_ORACLE = (
    "When Kitchen Finks enters, you gain 2 life.\n"
    "Persist (When this creature dies, if it had no -1/-1 counters on it, "
    "return it to the battlefield under its owner's control with a -1/-1 "
    "counter on it.)")


class TestPersistOnSacrifice:
    def _finks(self, make_card):
        c = make_card("Kitchen Finks", oracle_text=PERSIST_ORACLE,
                      type_line="Creature — Ouphe Cleric", power="3", toughness="2")
        c.keywords = ["Persist"]
        return c

    def test_sacrificed_persist_creature_returns_with_counter(
            self, rules, make_game, make_card):
        game = make_game()
        claude = game.players[1]
        finks = self._finks(make_card)
        finks.owner_index = 1
        claude.battlefield.append(finks)
        rules._execute_action_on_state(game, {
            "action": "sacrifice_permanent", "player": "Claude",
            "type_filter": "creature", "reason": "Dictate of Erebos"})
        assert finks in claude.battlefield, "persist must return it (sacrifice had no save-chain)"
        assert finks.counters.get("-1/-1") == 1

    def test_persist_does_not_retrigger_once_it_has_the_counter(
            self, rules, make_game, make_card):
        game = make_game()
        claude = game.players[1]
        finks = self._finks(make_card)
        finks.owner_index = 1
        finks.counters["-1/-1"] = 1  # already came back once
        claude.battlefield.append(finks)
        rules._execute_action_on_state(game, {
            "action": "sacrifice_permanent", "player": "Claude",
            "type_filter": "creature", "reason": "Dictate of Erebos"})
        assert finks not in claude.battlefield, (
            "CR 702.77b: persist does not trigger when it already had a -1/-1 counter")
        assert finks in claude.graveyard

    def test_persist_reminder_text_is_not_escalated_to_tier3(
            self, make_game, make_card):
        from mtg.engine import GameEngine
        engine = GameEngine(None)
        game = make_game()
        rick = game.players[0]
        finks = self._finks(make_card)
        rick.graveyard.append(finks)
        _msgs, unhandled = engine._check_dies_triggers_sync(game, finks, rick)
        assert "Kitchen Finks" not in [c.name for c, _t in unhandled], (
            "Persist is resolved mechanically by the death-save chain; queueing "
            "its reminder text for Tier 3 returned the creature a second time")


# --------------------------------------------------------------------------- #
# #9 — Arcane Denial: both draws are delayed to the next turn's upkeep
# --------------------------------------------------------------------------- #
class TestArcaneDenialDelayedDraws:
    def test_draws_are_scheduled_not_immediate(self, lib):
        actions, _ = lib.resolve_spell(
            "Arcane Denial",
            "Counter target spell. Its controller may draw up to two cards at "
            "the beginning of the next turn's upkeep. You draw a card at the "
            "beginning of the next turn's upkeep.",
            "Claude", "Rick", {"stack_top_spell": "Some Spell"})
        assert actions is not None
        kinds = [a["action"] for a in actions]
        assert "counter_spell" in kinds
        assert "draw_cards" not in kinds, "the draws must not resolve inline"
        sched = next(a for a in actions if a["action"] == "schedule_delayed_trigger")
        assert sched["trigger_at"] == "upkeep"
        # "the NEXT turn's upkeep" — whoever's it is, so no owner gate.
        assert sched["upkeep_of"] is None
        inner = [a["action"] for a in sched["actions"]]
        assert inner.count("draw_cards") == 2

    def test_fizzles_with_no_spell_to_counter(self, lib):
        actions, _ = lib.resolve_spell(
            "Arcane Denial", "Counter target spell.", "Claude", "Rick", {})
        assert actions[0]["action"] == "no_action"


# --------------------------------------------------------------------------- #
# #7 — Necropotence: the delayed exile->hand machinery it now rides
# --------------------------------------------------------------------------- #
class TestDelayedExileToHand:
    def test_exiled_card_returns_to_hand_at_end_step(self, make_game, make_card):
        from mtg.engine import GameEngine
        engine = GameEngine(None)
        game = make_game()
        rick = game.players[0]
        card = make_card("Hidden Card")
        rick.exile.append(card)
        engine.rules._execute_action_on_state(game, {
            "action": "schedule_delayed_trigger", "trigger_at": "end_step",
            "turn_delay": 0, "source": "Necropotence",
            "actions": [{"action": "move_card", "card": "Hidden Card",
                         "from_zone": "exile", "to_zone": "hand",
                         "player": "Rick"}]})
        engine._process_delayed_triggers(game, "end_step")
        assert card in rick.hand
        assert card not in rick.exile

    def test_your_next_end_step_is_owner_gated(self, make_game, make_card):
        # Necropotence says "YOUR next end step" — an instant-speed activation
        # during the opponent's turn must not return at THEIR end step.
        from mtg.engine import GameEngine
        engine = GameEngine(None)
        game = make_game()
        rick, claude = game.players
        card = make_card("Hidden Card")
        claude.exile.append(card)
        engine.rules._execute_action_on_state(game, {
            "action": "schedule_delayed_trigger", "trigger_at": "end_step",
            "turn_delay": 0, "phase_of": "Claude", "source": "Necropotence",
            "actions": [{"action": "move_card", "card": "Hidden Card",
                         "from_zone": "exile", "to_zone": "hand",
                         "player": "Claude"}]})
        game.active_player_index = game.players.index(rick)
        engine._process_delayed_triggers(game, "end_step")
        assert card in claude.exile, "must wait for the caster's own end step"
        game.active_player_index = game.players.index(claude)
        engine._process_delayed_triggers(game, "end_step")
        assert card in claude.hand


# --------------------------------------------------------------------------- #
# #14 — an "Enchant player" Curse survives the aura SBA
# --------------------------------------------------------------------------- #
class TestEnchantPlayerAura:
    def test_curse_is_not_destroyed_for_lacking_an_object(
            self, rules, make_game, make_card):
        game = make_game()
        claude = game.players[1]
        curse = make_card(
            "Curse of Bloodletting",
            type_line="Enchantment — Aura Curse",
            oracle_text="Enchant player\nIf a source would deal damage to "
                        "enchanted player, it deals double that damage to that "
                        "player instead.")
        claude.battlefield.append(curse)
        rules.process_state_based_actions(game)
        assert curse in claude.battlefield, (
            "CR 704.5m: attachment to a PLAYER is legal — the object-only SBA "
            "model must not treat a Curse as unattached")

    def test_ordinary_unattached_aura_still_dies(self, rules, make_game, make_card):
        game = make_game()
        claude = game.players[1]
        aura = make_card("Pacifism", type_line="Enchantment — Aura",
                         oracle_text="Enchant creature\nEnchanted creature "
                                     "can't attack or block.")
        claude.battlefield.append(aura)
        rules.process_state_based_actions(game)
        assert aura not in claude.battlefield, "the 704.5m check must still work"


# --------------------------------------------------------------------------- #
# #15 — Insult // Injury registers a self-expiring turn-scoped doubler
# --------------------------------------------------------------------------- #
class TestInsultTurnDoubler:
    def test_template_emits_the_register_action(self, lib):
        actions, _ = lib.resolve_spell(
            "Insult // Injury",
            "Damage can't be prevented this turn. If a source you control "
            "would deal damage this turn, it deals double that damage instead.",
            "Claude", "Rick", {})
        assert actions is not None
        assert actions[0]["action"] == "register_turn_damage_doubler"
        assert actions[0]["player"] == "Claude"

    def test_action_registers_effect_that_expires_next_turn(self, rules, make_game):
        game = make_game()
        game.turn_number = 5
        msg = rules._execute_action_on_state(game, {
            "action": "register_turn_damage_doubler", "player": "Claude",
            "source": "Insult // Injury"})
        assert msg and "doubled" in msg.lower()
        eff = next(e for e in game.replacement_engine.effects
                   if "Insult" in e.source_name)
        assert eff.multiply_amount == 2.0

        class _Ev:
            source_controller = "Claude"
        assert eff.condition(_Ev()) is True, "applies to the controller's sources this turn"

        class _EvOpp:
            source_controller = "Rick"
        assert eff.condition(_EvOpp()) is False, "not the opponent's sources"

        game.turn_number = 6
        assert eff.condition(_Ev()) is False, "must self-expire after its turn"

    def test_damage_cannot_be_prevented_this_turn(self, rules, make_game):
        # Insult's OTHER clause. Without it, a Fog / Teferi's Protection flag
        # would blank the very damage the doubler just doubled — and the
        # replacement_chain + replacement_fog decks both run Fog effects.
        game = make_game()
        game.turn_number = 5
        rick = game.players[0]
        rick._damage_prevented = True  # Fog / Teferi's Protection
        start = rick.life
        rules._execute_action_on_state(game, {
            "action": "deal_damage", "amount": 3, "target_player": "Rick"})
        assert rick.life == start, "ordinary prevention must still work"

        rules._execute_action_on_state(game, {
            "action": "register_turn_damage_doubler", "player": "Claude",
            "source": "Insult // Injury"})
        rules._execute_action_on_state(game, {
            "action": "deal_damage", "amount": 3, "target_player": "Rick"})
        assert rick.life < start, "damage can't be prevented this turn"

        # ...and the override self-expires with the turn.
        game.turn_number = 6
        rick.life = start
        rules._execute_action_on_state(game, {
            "action": "deal_damage", "amount": 3, "target_player": "Rick"})
        assert rick.life == start, "prevention is back on the next turn"


# --------------------------------------------------------------------------- #
# #3 — flicker detaches the auras that pointed at the creature
# --------------------------------------------------------------------------- #
class TestFlickerDetachesAuras:
    def test_animate_dead_falls_off_on_flicker(self, rules, make_game, make_card):
        game = make_game()
        claude = game.players[1]
        korvold = make_card("Korvold, Fae-Cursed King", oracle_text="",
                            power="4", toughness="4")
        korvold.owner_index = 1
        animate = make_card("Animate Dead", type_line="Enchantment — Aura",
                            oracle_text="Enchanted creature gets -1/-0.")
        animate.owner_index = 1
        animate.attached_to = korvold.id
        korvold.attachments = [animate.id]
        claude.battlefield.extend([korvold, animate])
        rules._execute_action_on_state(game, {
            "action": "flicker", "player": "Claude",
            "target": "Korvold, Fae-Cursed King"})
        assert animate.attached_to is None, (
            "CR 400.7: the returned permanent is a new object — the aura falls off")
        assert korvold.attachments == []


# --------------------------------------------------------------------------- #
# #16 — Savra: color-qualified sacrifice clauses are gated per creature
# --------------------------------------------------------------------------- #
SAVRA_ORACLE = (
    "Whenever you sacrifice a black creature, you may pay 2 life. If you do, "
    "each other player sacrifices a creature of their choice.\n"
    "Whenever you sacrifice a green creature, you may gain 2 life.")


class TestSavraColorQualifiedSacTriggers:
    def test_green_sacrifice_skips_the_black_clause(
            self, rules, make_game, make_card, capsys):
        from mtg.actions import _fire_sacrifice_triggers
        game = make_game()
        claude = game.players[1]
        savra = make_card("Savra, Queen of the Golgari", oracle_text=SAVRA_ORACLE)
        savra.colors = ["B", "G"]
        elder = make_card("Sakura-Tribe Elder")
        elder.colors = ["G"]
        claude.battlefield.append(savra)
        _fire_sacrifice_triggers(rules, game, claude, elder)
        out = capsys.readouterr().out
        assert "skipping B-qualified clause" in out, (
            "a green sacrifice must not resolve Savra's black clause")

    def test_black_sacrifice_skips_the_green_clause(
            self, rules, make_game, make_card, capsys):
        from mtg.actions import _fire_sacrifice_triggers
        game = make_game()
        claude = game.players[1]
        savra = make_card("Savra, Queen of the Golgari", oracle_text=SAVRA_ORACLE)
        savra.colors = ["B", "G"]
        zombie = make_card("Black Zombie")
        zombie.colors = ["B"]
        claude.battlefield.append(savra)
        _fire_sacrifice_triggers(rules, game, claude, zombie)
        out = capsys.readouterr().out
        assert "skipping G-qualified clause" in out
