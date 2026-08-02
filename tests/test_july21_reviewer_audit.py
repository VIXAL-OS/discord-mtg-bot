"""July 21, 2026 batch audit (game_15291*) — reviewer-wave pins.

Four Sonnet reviewers, one game each; every mechanism below was
source-verified before fixing. FP ledger addition from this wave: the
"Dread Return is missing its haste/sacrifice clause" claim was memory-based
— the cache matches Scryfall exactly (no such clause on any printing), so
the permanent reanimation is CORRECT. Verify claimed oracle text against
data/scryfall_oracle_cards.json, not memory, in BOTH directions.

Pinned findings:
- R4-1  Yidris self-cascaded via blind 'cascade' substring (grant clause,
        not a keyword line) — game_1529168842905882755.
- R4-2  Single-blocker trample display showed full power to the blocker on
        top of the trample line (state was correct).
- R4-3  Single-creature pump displayed as a team anthem.
- R2-1  Swan Song: target phrase truncated at the first comma AND the
        type-priority chain collapsed "…spell" phrases to a permanent type
        — every legal counter-counter cast-blocked
        (game_1529172161636597770).
- R2-2  Summary Dismissal silently evaporated at Tier 2 (empty messages
        dodge the Tier 3 gate) while narration claimed it resolved.
- R2-3  "Draw X cards" (Gadwick) defaulted to 1 — never read ctx x_value.
- R1-1  Snap / Vapor Snag ignored the declared cast-time target
        (CR 601.2c/608.2b) — game_1529154418816057364.
- R1-2  A permanent's own "whenever you sacrifice" never saw its own
        sacrifice (Korvold to Viscera Seer drew no card).
- R1-3  Five dies-scan call sites dropped the unhandled tail (no display,
        no Tier 3) — Judith, the Scourge Diva's damage vanished.
- R1-4  Phyrexian mana life payments were console-only.
- R3-1  Single-target destroy sent commanders to the graveyard
        (CR 903.9a) — Kambal stranded 10 turns, game_1529168824723570750.
- R3-2  Dread Return template ignored the declared target.
- R3-4  Anguished Unmaking's "You lose 3 life." clause was dropped.
- R3-5  resolve_etb generic patterns matched ACTIVATED-ability text — Glen
        Elendra Archmage's counter fired free on every ETB resolution.
- (inline) game._rules_engine was only ever assigned in TESTS — the
        spell_resolver SBA routing + Phyrexian Tower dies dispatch were
        dead in every live game. Now stamped at game creation/load.
"""
import asyncio
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

YIDRIS_ORACLE = (
    "Trample\nWhenever Yidris, Maelstrom Wielder deals combat damage to a "
    "player, as you cast spells from your hand this turn, they gain cascade."
)
GLEN_ELENDRA_ORACLE = (
    "Flying\n{U}, Sacrifice this creature: Counter target noncreature spell."
    "\nPersist (When this creature dies, if it had no -1/-1 counters on it, "
    "return it to the battlefield under its owner's control with a -1/-1 "
    "counter on it.)"
)


# ---------------------------------------------------------------------------
# R4-1 — cascade only from keyword lines
# ---------------------------------------------------------------------------

class TestCascadeKeywordLines:
    def _run_cast_triggers(self, make_game, make_card, oracle, library_names):
        from mtg.engine import GameEngine
        from mtg.triggers import _check_cast_triggers
        engine = GameEngine(None)
        game = make_game()
        rick = game.players[0]
        rick.library = [make_card(n, mana_cost="{1}", cmc=1,
                                  type_line="Creature — Bear",
                                  power="2", toughness="2")
                        for n in library_names]
        card = make_card("Caster", mana_cost="{3}{U}", cmc=4,
                         oracle_text=oracle)
        rick.hand.append(card)
        asyncio.run(_check_cast_triggers(engine, game, rick, card))
        return game, rick

    def test_yidris_grant_clause_does_not_self_cascade(self, make_game, make_card):
        game, rick = self._run_cast_triggers(
            make_game, make_card, YIDRIS_ORACLE, ["A", "B", "C"])
        assert len(rick.library) == 3, (
            "Yidris only GRANTS cascade to other spells — casting him must "
            "not exile cards off the top")

    def test_real_cascade_keyword_still_fires(self, make_game, make_card):
        game, rick = self._run_cast_triggers(
            make_game, make_card,
            "Cascade (When you cast this spell, exile cards from the top of "
            "your library until you exile a nonland card that costs less.)"
            "\nHaste", ["A", "B", "C"])
        assert len(rick.library) < 3, "a real Cascade keyword must still exile"


# ---------------------------------------------------------------------------
# R4-2 — trample single-blocker display
# ---------------------------------------------------------------------------

class TestTrampleDisplaySplit:
    def test_display_shows_split_not_full_power(self, make_game, make_card, rules):
        game = make_game()
        rick, claude = game.players
        attacker = make_card("Wielder", power="5", toughness="4",
                             keywords=["Trample"])
        attacker.attacking = True
        rick.battlefield.append(attacker)
        blocker = make_card("Tyrant", power="4", toughness="4")
        claude.battlefield.append(blocker)
        game.attackers = [attacker.id]
        game.blockers = {attacker.id: [blocker.id]}
        game.active_player_index = 0
        msgs = rules.resolve_combat_damage(game) or []
        joined = "\n".join(msgs)
        assert "deals 4 damage" in joined, joined
        assert "deals 5 damage" not in joined, (
            "single-blocker trample must display the 4/1 split, not full "
            "power to the blocker plus a trample line: " + joined)


# ---------------------------------------------------------------------------
# R4-3 — single-creature pump display
# ---------------------------------------------------------------------------

class TestSingleCreaturePumpDisplay:
    def test_self_pump_names_the_creature(self, make_game, make_card, rules):
        from mtg.actions import execute_action_on_state
        game = make_game()
        rick = game.players[0]
        titan = make_card("Inferno Titan", power="6", toughness="6")
        rick.battlefield.append(titan)
        msg = execute_action_on_state(rules, game, {
            "action": "pump_all_creatures", "player": rick.name,
            "power": 1, "toughness": 0})
        assert msg and "Inferno Titan" in msg and "gets +1/+0" in msg, msg
        assert "creatures get" not in (msg or ""), (
            "a one-creature pump must not read like a team anthem: " + str(msg))


# ---------------------------------------------------------------------------
# R2-1 — Swan Song targeting
# ---------------------------------------------------------------------------

class TestCounterspellTargetPhrases:
    def test_multi_type_spell_phrase_parses_as_spell(self):
        from rules.targeting import TargetTextParser, TargetType
        r = TargetTextParser.parse("target enchantment, instant, or sorcery spell")
        assert r.target_types == {TargetType.SPELL}
        r2 = TargetTextParser.parse("target noncreature spell")
        assert r2.target_types == {TargetType.SPELL}

    def test_swan_song_sees_an_instant_on_the_stack(self, make_game, make_card):
        from rules.targeting_helpers import _find_any_valid_target
        from mtg.models import StackEntry
        game = make_game()
        rick = game.players[0]
        swan = make_card(
            "Swan Song", mana_cost="{U}", cmc=1, type_line="Instant",
            oracle_text="Counter target enchantment, instant, or sorcery "
                        "spell. Its controller creates a 2/2 blue Bird "
                        "creature token with flying.")
        inc = make_card("Arcane Denial", mana_cost="{1}{U}", cmc=2,
                        type_line="Instant",
                        oracle_text="Counter target spell.")
        game.stack.append(StackEntry(card=inc, controller_name=game.players[1].name, controller_index=1))
        assert _find_any_valid_target(game, swan, rick.name) is True

    def test_swan_song_rejects_creature_only_board(self, make_game, make_card):
        from rules.targeting_helpers import _find_any_valid_target
        game = make_game()
        rick, claude = game.players
        swan = make_card(
            "Swan Song", mana_cost="{U}", cmc=1, type_line="Instant",
            oracle_text="Counter target enchantment, instant, or sorcery "
                        "spell. Its controller creates a 2/2 blue Bird "
                        "creature token with flying.")
        claude.battlefield.append(make_card("Bear", power="2", toughness="2"))
        assert _find_any_valid_target(game, swan, rick.name) is False, (
            "empty stack + creatures on battlefield is NOT a legal Swan "
            "Song target set")


# ---------------------------------------------------------------------------
# R2-2 — Summary Dismissal actually does something
# ---------------------------------------------------------------------------

class TestSummaryDismissal:
    def test_exiles_other_spells_and_counters_triggers(self, make_game, make_card):
        from mtg.engine import GameEngine
        from mtg.models import StackEntry
        engine = GameEngine(None)
        game = make_game()
        rick, claude = game.players
        dismissal = make_card("Summary Dismissal", mana_cost="{2}{U}{U}",
                              cmc=4, type_line="Instant",
                              oracle_text="Exile all other spells and "
                                          "counter all abilities.")
        avenger = make_card("Avenger of Zendikar", mana_cost="{5}{G}{G}",
                            cmc=7, type_line="Creature — Elemental")
        avenger.owner_index = 1
        game.stack.append(StackEntry(card=avenger, controller_name=claude.name, controller_index=1))
        game.stack.append(StackEntry(card=dismissal, controller_name=rick.name, controller_index=0))
        game.pending_async_triggers = [{'source_card': avenger,
                                        'trigger_text': 'x',
                                        'trigger_type': 'etb',
                                        'controller_name': claude.name,
                                        'context': ''}]
        msgs = engine.resolve_special_effects(game, rick, dismissal)
        assert any("Avenger of Zendikar" in m for m in msgs), msgs
        assert avenger in claude.exile
        assert all(getattr(e, 'card', None) is not avenger for e in game.stack)
        assert game.pending_async_triggers == []


# ---------------------------------------------------------------------------
# R2-3 / R3-5 / R1-1 / R3-2 / R3-4 — template library behavior
# ---------------------------------------------------------------------------

class TestTemplateLibraryFixes:
    def test_gadwick_draws_x(self, lib):
        actions, _ = lib.resolve_etb(
            "Gadwick, the Wizened",
            "When Gadwick, the Wizened enters, draw X cards.",
            "Rick", "Claude", game_context={'x_value': 8})
        assert actions == [{"action": "draw_cards", "player": "Rick", "amount": 8}]

    def test_glen_elendra_activated_ability_is_not_an_etb(self, lib):
        actions, _ = lib.resolve_etb(
            "Glen Elendra Archmage", GLEN_ELENDRA_ORACLE, "Rick", "Claude")
        assert actions is None, (
            "an activated ability ('{U}, Sacrifice...: Counter...') must "
            "never resolve as a free ETB effect")

    def test_snap_honors_declared_target(self, lib):
        actions, _ = lib.resolve_spell(
            "Snap",
            "Return target creature to its owner's hand. Untap up to two lands.",
            "Rick", "Claude",
            game_context={'explicit_target_name': 'Birds of Paradise',
                          'best_opponent_creature': 'Korvold, Fae-Cursed King'})
        assert actions[0]['card'] == 'Birds of Paradise'

    def test_dread_return_honors_declared_target(self, lib):
        actions, _ = lib.resolve_spell(
            "Dread Return",
            "Return target creature card from your graveyard to the "
            "battlefield.\nFlashback—Sacrifice three creatures.",
            "Rick", "Claude",
            game_context={'explicit_target_name': 'Blood Artist',
                          'best_own_graveyard_creature': 'Sakura-Tribe Elder'})
        assert actions[0]['card'] == 'Blood Artist'
        assert actions[0].get('own_graveyard') is True

    def test_anguished_unmaking_carries_the_life_clause(self, lib):
        actions, _ = lib.resolve_spell(
            "Anguished Unmaking",
            "Exile target nonland permanent. You lose 3 life.",
            "Claude", "Rick",
            game_context={'explicit_target_name': 'Dictate of Erebos'})
        assert actions[0]['to_zone'] == 'exile'
        assert {"action": "lose_life", "player": "Claude", "amount": 3} in actions


# ---------------------------------------------------------------------------
# R1-2 — own sacrifice fires "whenever you sacrifice"
# ---------------------------------------------------------------------------

class TestOwnSacrificeTrigger:
    def test_korvold_sees_its_own_sacrifice(self, make_game, make_card, rules):
        from mtg.actions import _fire_sacrifice_triggers
        game = make_game()
        rick = game.players[0]
        korvold = make_card(
            "Korvold, Fae-Cursed King", power="4", toughness="4",
            oracle_text="Flying\nWhenever Korvold, Fae-Cursed King enters "
                        "or attacks, sacrifice another permanent.\nWhenever "
                        "you sacrifice a permanent, put a +1/+1 counter on "
                        "Korvold and draw a card.")
        # Korvold has already been removed from the battlefield (every call
        # site fires the scan after removal) — his own trigger must still
        # see the event (Mayhem Devil rulings; last-known-info).
        hand_before = len(rick.hand)
        msgs = _fire_sacrifice_triggers(rules, game, rick, korvold)
        assert msgs, "Korvold's own sacrifice must fire his draw trigger"
        assert len(rick.hand) > hand_before or any(
            'draw' in m.lower() for m in msgs), msgs


# ---------------------------------------------------------------------------
# R1-3 — unhandled dies triggers are queued, not dropped
# ---------------------------------------------------------------------------

class TestUnhandledDiesQueued:
    def test_queue_unhandled_dies_helper(self, make_game, make_card):
        from mtg.engine import GameEngine
        engine = GameEngine(None)
        game = make_game()
        rick, claude = game.players
        judith = make_card(
            "Judith, the Scourge Diva", power="2", toughness="2",
            oracle_text="Other creatures you control get +1/+0.\nWhenever a "
                        "nontoken creature you control dies, Judith deals 1 "
                        "damage to any target.")
        rick.battlefield.append(judith)
        dead = make_card("Bear", power="2", toughness="2")
        engine.queue_unhandled_dies(game, dead, rick,
                                    [(judith, "Whenever a nontoken creature "
                                              "you control dies...")])
        assert game.pending_async_triggers, "unhandled dies trigger not queued"
        q = game.pending_async_triggers[0]
        assert q['source_card'] is judith
        assert q['trigger_type'] == 'dies'

    def test_single_target_destroy_queues_unhandled(self, make_game, make_card):
        from mtg.engine import GameEngine
        from mtg.actions import execute_action_on_state
        engine = GameEngine(None)
        game = make_game()
        rick = game.players[0]
        judith = make_card(
            "Judith, the Scourge Diva", power="2", toughness="2",
            oracle_text="Whenever a nontoken creature you control dies, "
                        "Judith deals 1 damage to any target.")
        bear = make_card("Bear", power="2", toughness="2")
        rick.battlefield.extend([judith, bear])
        execute_action_on_state(engine.rules, game,
                                {"action": "destroy", "card": "Bear"})
        assert bear in rick.graveyard
        # Aug 2 (B2, the bus unification): the destroy action QUEUES the
        # death via CREATURE_DIED; the dispatcher drains at the next SBA
        # check — the same deferred semantics wipes have had since July.
        # The original guarantee is unchanged: Judith's untemplated trigger
        # reaches the Tier-3 queue, not the void.
        assert any(c is bear for c, _p in (game._recently_died or [])), (
            "the death must sit on the bus-fed queue before the drain")
        engine.check_state_based_actions(game)
        assert any(q.get('source_card') is judith
                   for q in (game.pending_async_triggers or [])), (
            "Judith's untemplated dies trigger must queue for Tier 3, "
            "not vanish")


# ---------------------------------------------------------------------------
# R1-4 — Phyrexian life payment reaches the display buffer
# ---------------------------------------------------------------------------

class TestPhyrexianLifeDisplay:
    def test_life_payment_is_buffered_for_discord(self, make_game, make_card):
        game = make_game()
        rick = game.players[0]
        assert rick.tap_sources_for_cost('{U/P}', pay_phyrexian_with_life=True)
        assert rick.life == 38
        assert any('pays 2 life' in m for m in rick._pending_tap_damage_msgs), (
            "Phyrexian life payment must reach the buffered display drain, "
            "not just the console")


# ---------------------------------------------------------------------------
# R3-1 — destroy action redirects commanders (CR 903.9a)
# ---------------------------------------------------------------------------

class TestDestroyCommanderRedirect:
    def test_destroyed_commander_goes_to_command_zone(self, make_game, make_card, rules):
        from mtg.actions import execute_action_on_state
        game = make_game()
        claude = game.players[1]
        kambal = make_card("Kambal, Consul of Allocation",
                           power="2", toughness="3")
        kambal.is_commander = True
        kambal.owner_index = 1
        claude.battlefield.append(kambal)
        msg = execute_action_on_state(rules, game, {
            "action": "destroy", "card": "Kambal, Consul of Allocation"})
        assert kambal in claude.command_zone, msg
        assert kambal not in claude.graveyard
        assert "command zone" in (msg or "")


# ---------------------------------------------------------------------------
# (inline) game._rules_engine is production-wired
# ---------------------------------------------------------------------------

class TestRulesEngineWiring:
    def test_gamestate_declares_the_field(self, make_game):
        game = make_game()
        assert hasattr(game, '_rules_engine')

    def test_engine_stamps_it_at_registration(self):
        # The attribute was only ever assigned in tests before July 21 —
        # spell_resolver's SBA routing and the Phyrexian Tower dies dispatch
        # were dead in every live game. Source-level pin on the three
        # game-registration sites.
        src = (REPO / 'mtg' / 'engine.py').read_text(encoding='utf-8')
        assert src.count('game._rules_engine = self.rules') >= 3
