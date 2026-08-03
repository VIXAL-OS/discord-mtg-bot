"""Aug 2, 2026 — the batch-14 deferred slate, built out.

Three items the batch-14 audit recorded with mechanisms rather than fixes,
plus the minors that came with them.

TORBRAN, THANE OF RED FELL was entirely unimplemented — a vanilla 2/4. Its
"If a RED source you control would deal damage to an opponent or a permanent
an opponent controls, it deals that much damage plus 2 instead" needs
something GameEvent did not carry: the SOURCE's colors. Colors come from the
mana cost (CR 202.2), never from color_identity, which absorbs colors from
oracle text and would make a colorless artifact with a {B} activation a
"black source".

Wiring it exposed a second, larger bug: GameEvent.copy() was a HAND-WRITTEN
field list, and process_event_sync copies the event before its first
applies_to check — so every field added after that list was written became
invisible to every condition. Three had already been lost: source_colors
(this work), and July 30's is_token and redirect_counter, which means Draugr
Necromancer's "NONTOKEN creature an opponent controls" filter was reading a
field that was always False by the time a condition saw it. The copy is now
exhaustive by construction.

COMBUSTIBLE GEARHULK had no template, so the generic "when X enters ... draw
N cards" pattern matched straight across "target opponent MAY have you draw
three cards" and every Gearhulk resolved as an unconditional draw-3 for its
controller — the opponent's choice and the entire mill+burn branch silently
discarded.

MINORS: Liliana of the Veil's [+1] burned a Tier-3 API call whenever both
hands were empty (a legal no-op, CR 701.8a) because the template path's
early return gates on display TEXT rather than on "did it resolve"; the
[DISCARD] sentinel message read as if the engine had searched for a card
named "worst"; and the extra-combat discard line blamed a mid-combat re-grant
even when the game had simply ended first.
"""
import inspect

import pytest

from mtg.helpers import damage_source_colors, spell_colors_from_cost


TORBRAN_ORACLE = ("If a red source you control would deal damage to an "
                  "opponent or a permanent an opponent controls, it deals "
                  "that much damage plus 2 instead.")

GEARHULK_ORACLE = (
    "First strike\n"
    "When this creature enters, target opponent may have you draw three "
    "cards. If the player doesn't, you mill three cards, then this creature "
    "deals damage to that player equal to the total mana value of those "
    "cards.")


def _torbran(make_card):
    return make_card("Torbran, Thane of Red Fell", mana_cost="{1}{R}{R}{R}",
                     type_line="Legendary Creature — Dwarf Noble",
                     power="2", toughness="4", oracle_text=TORBRAN_ORACLE)


class TestGameEventCopyIsExhaustive:
    """The bug that Torbran's wiring uncovered — it outranks Torbran."""

    def test_copy_carries_every_field(self):
        import dataclasses
        from rules.replacement import GameEvent, EventType
        ev = GameEvent(event_type=EventType.DAMAGE, amount=3)
        # Give every field a non-default value so a dropped one is visible.
        for f in dataclasses.fields(GameEvent):
            cur = getattr(ev, f.name)
            if f.name == "event_type":
                continue
            if isinstance(cur, bool):
                setattr(ev, f.name, not cur)
            elif isinstance(cur, int) and not isinstance(cur, bool):
                setattr(ev, f.name, cur + 7)
            elif isinstance(cur, str):
                setattr(ev, f.name, "sentinel-" + f.name)
            elif cur is None:
                setattr(ev, f.name, {"probe"} if "colors" in f.name else "probe")
        dup = ev.copy()
        for f in dataclasses.fields(GameEvent):
            assert getattr(dup, f.name) == getattr(ev, f.name), (
                f"copy() dropped {f.name!r} — process_event_sync copies "
                f"BEFORE its first applies_to, so a dropped field is "
                f"invisible to every condition")

    def test_mutable_fields_are_duplicated_not_shared(self):
        from rules.replacement import GameEvent, EventType
        ev = GameEvent(event_type=EventType.DAMAGE, amount=1,
                       source_colors={"R"})
        ev.applied_replacements.add("x")
        ev.replacement_chain.append("y")
        dup = ev.copy()
        dup.applied_replacements.add("z")
        dup.replacement_chain.append("w")
        dup.source_colors.add("G")
        assert "z" not in ev.applied_replacements
        assert "w" not in ev.replacement_chain
        assert "G" not in ev.source_colors

    def test_is_token_survives_the_copy(self):
        """July 30's field, silently dropped ever since — Draugr
        Necromancer's nontoken filter depends on it."""
        from rules.replacement import GameEvent, EventType
        ev = GameEvent(event_type=EventType.DEATH, is_token=True)
        assert ev.copy().is_token is True


class TestDamageSourceColors:
    def test_colors_come_from_the_mana_cost(self):
        assert spell_colors_from_cost("{1}{R}{R}{R}") == {"R"}
        assert spell_colors_from_cost("{2}") == set()
        assert spell_colors_from_cost("{W/U}") == {"W", "U"}

    def test_card_object_is_preferred(self, game, make_card):
        bolt = make_card("Lightning Bolt", mana_cost="{R}")
        assert damage_source_colors(game, source_card=bolt) == {"R"}

    def test_battlefield_lookup_by_name(self, game, make_card):
        gob = make_card("Goblin", mana_cost="{R}")
        game.players[0].battlefield.append(gob)
        assert damage_source_colors(game, source_name="Goblin") == {"R"}

    def test_graveyard_lookup_finds_a_resolved_burn_spell(self, game,
                                                          make_card):
        """_dispatch_resolution pops the stack entry BEFORE running the
        effect, so a burn spell is in the graveyard when its damage lands."""
        chain = make_card("Chain Lightning", mana_cost="{R}",
                          type_line="Sorcery")
        game.players[0].graveyard.append(chain)
        assert damage_source_colors(game, source_name="Chain Lightning") == {"R"}

    def test_unknown_source_returns_none_not_empty(self, game):
        assert damage_source_colors(game, source_name="Nonexistent") is None, (
            "None means UNKNOWN — a color-gated effect must decline rather "
            "than treat it as colorless and guess")


class TestTorbran:
    def _setup(self, rules, game, make_card):
        rick, claude = game.players
        t = _torbran(make_card)
        rick.battlefield.append(t)
        game.register_replacement_effects(t, rick.name)
        return rick, claude

    def test_red_source_to_opponent_gets_plus_two(self, rules, game, make_card):
        rick, claude = self._setup(rules, game, make_card)
        gob = make_card("Goblin", mana_cost="{R}")
        rick.battlefield.append(gob)
        before = claude.life
        rules._apply_combat_damage_to_player(game, claude, 2, gob,
                                             is_combat=True)
        assert before - claude.life == 4

    def test_colorless_source_is_unaffected(self, rules, game, make_card):
        rick, claude = self._setup(rules, game, make_card)
        orn = make_card("Ornithopter", mana_cost="{0}")
        rick.battlefield.append(orn)
        before = claude.life
        rules._apply_combat_damage_to_player(game, claude, 3, orn,
                                             is_combat=True)
        assert before - claude.life == 3, "'a RED source' is a real gate"

    def test_opponents_red_source_is_unaffected(self, rules, game, make_card):
        rick, claude = self._setup(rules, game, make_card)
        og = make_card("Opp Goblin", mana_cost="{R}")
        claude.battlefield.append(og)
        before = rick.life
        rules._apply_combat_damage_to_player(game, rick, 2, og,
                                             is_combat=True)
        assert before - rick.life == 2, "'you control' is a real gate"

    def test_opponents_red_source_hitting_their_own_creature(
            self, rules, game, make_card):
        """The scenario that isolates the "you control" gate.

        In two-player, "source is mine" and "target is not mine" nearly
        imply each other — EXCEPT here: the opponent's own red source
        damaging the opponent's own creature satisfies "not mine" on the
        target but must still be declined, because the source is theirs.
        A mutant that drops the controller gate survives every other case.
        """
        rick, claude = self._setup(rules, game, make_card)
        their_gob = make_card("Their Goblin", mana_cost="{R}")
        claude.battlefield.append(their_gob)
        their_bear = make_card("Their Bear", power="2", toughness="9")
        claude.battlefield.append(their_bear)
        dealt = rules._apply_combat_damage_to_creature(
            game, their_bear, 2, their_gob)
        assert dealt == 2, (
            "Torbran only boosts sources ITS controller controls")

    def test_opponents_permanent_is_boosted(self, rules, game, make_card):
        """The second half of the printed clause."""
        rick, claude = self._setup(rules, game, make_card)
        gob = make_card("Goblin", mana_cost="{R}")
        rick.battlefield.append(gob)
        blk = make_card("Bear", power="2", toughness="9")
        claude.battlefield.append(blk)
        assert rules._apply_combat_damage_to_creature(game, blk, 2, gob) == 4

    def test_own_permanent_is_not_boosted(self, rules, game, make_card):
        rick, claude = self._setup(rules, game, make_card)
        gob = make_card("Goblin", mana_cost="{R}")
        rick.battlefield.append(gob)
        mine = make_card("My Bear", power="2", toughness="9")
        rick.battlefield.append(mine)
        assert rules._apply_combat_damage_to_creature(game, mine, 2, gob) == 2

    def test_red_burn_spell_from_the_action_path(self, rules, game, make_card):
        """The template / Tier-3 damage path, which carries the caster
        explicitly as _source_controller."""
        rick, claude = self._setup(rules, game, make_card)
        chain = make_card("Chain Lightning", mana_cost="{R}",
                          type_line="Sorcery")
        rick.graveyard.append(chain)
        before = claude.life
        rules._execute_action_on_state(game, {
            "action": "deal_damage", "amount": 3, "target_player": "Claude",
            "source": "Chain Lightning", "_source_controller": "Rick"})
        assert before - claude.life == 5

    def test_white_spell_from_the_action_path_is_unaffected(self, rules, game,
                                                            make_card):
        rick, claude = self._setup(rules, game, make_card)
        sw = make_card("Swords to Plowshares", mana_cost="{W}",
                       type_line="Instant")
        rick.graveyard.append(sw)
        before = claude.life
        rules._execute_action_on_state(game, {
            "action": "deal_damage", "amount": 3, "target_player": "Claude",
            "source": "Swords to Plowshares", "_source_controller": "Rick"})
        assert before - claude.life == 3

    def test_it_is_additive_not_a_multiplier(self, rules, game, make_card):
        """"plus 2", so 5 damage becomes 7 — not 10. The distinction also
        makes Torbran + Furnace of Rath non-commutative, which the existing
        CR 616.1 ordering machinery already handles."""
        rick, claude = self._setup(rules, game, make_card)
        gob = make_card("Big Goblin", mana_cost="{R}", power="5")
        rick.battlefield.append(gob)
        before = claude.life
        rules._apply_combat_damage_to_player(game, claude, 5, gob,
                                             is_combat=True)
        assert before - claude.life == 7


class TestCombustibleGearhulk:
    def _resolve(self, game, lib, make_card, lib_mvs, opp_life):
        from rules.effect_templates import build_game_context
        rick, claude = game.players
        claude.life = opp_life
        rick.library = [make_card(f"L{i}", cmc=mv)
                        for i, mv in enumerate(lib_mvs)]
        gh = make_card("Combustible Gearhulk", mana_cost="{4}{R}",
                       type_line="Artifact Creature — Construct",
                       oracle_text=GEARHULK_ORACLE)
        rick.battlefield.append(gh)
        ctx = build_game_context(game, rick, claude, card=gh)
        actions, _ = lib.resolve_etb("Combustible Gearhulk", GEARHULK_ORACLE,
                                     rick.name, claude.name, game_context=ctx)
        return actions

    def test_cheap_library_means_the_opponent_takes_the_damage(
            self, game, lib, make_card):
        actions = self._resolve(game, lib, make_card, [1] * 10, 40)
        kinds = [a["action"] for a in actions]
        assert kinds == ["mill", "deal_damage"], kinds
        assert actions[1]["amount"] == 3, "sum of the ACTUAL milled MVs"
        assert actions[1]["target_player"] == "Claude"

    def test_expensive_library_means_the_opponent_gives_cards(
            self, game, lib, make_card):
        actions = self._resolve(game, lib, make_card, [8] * 10, 40)
        assert [a["action"] for a in actions] == ["draw_cards"]
        assert actions[0]["player"] == "Rick" and actions[0]["amount"] == 3

    def test_low_life_makes_even_a_cheap_deck_scary(self, game, lib,
                                                    make_card):
        actions = self._resolve(game, lib, make_card, [3] * 10, 8)
        assert [a["action"] for a in actions] == ["draw_cards"]

    def test_short_library_is_always_milled(self, game, lib, make_card):
        """Milling three would deck the controller — free win, always taken."""
        actions = self._resolve(game, lib, make_card, [1, 1], 40)
        assert [a["action"] for a in actions] == ["mill"]

    def test_never_resolves_as_a_bare_unconditional_draw_three(
            self, game, lib, make_card):
        """The live bug: the generic ETB-draw pattern swallowed the whole
        sentence. With a cheap deck and a healthy opponent the printed card
        does NOT hand over three cards."""
        actions = self._resolve(game, lib, make_card, [0] * 10, 40)
        assert not any(a["action"] == "draw_cards" for a in actions), actions

    def test_the_choice_does_not_peek_at_the_top_three(self, game, lib,
                                                       make_card):
        """The opponent decides BEFORE the mill, so the decision may only use
        public information (the library average), never the actual top three.
        Two libraries with the SAME average must produce the same CHOICE even
        when their top three differ wildly."""
        stacked = self._resolve(game, lib, make_card,
                                [9, 9, 9] + [0] * 27, 40)
        flat = self._resolve(game, lib, make_card, [1] * 30, 40)
        assert [a["action"] for a in stacked][0] == \
            [a["action"] for a in flat][0], (
            "a peeking implementation would dodge the stacked top-three")


class TestPlaneswalkerLegalNoOp:
    """Liliana of the Veil [+1] with both hands empty."""

    def test_template_path_does_not_escalate_on_a_legal_no_op(self):
        src = inspect.getsource(
            __import__('rules.planeswalker', fromlist=['x']))
        i = src.index("visible effect (legal no-op)")
        window = src[max(0, i - 700):i + 400]
        assert "_last_ability_legal_noop = True" in window, (
            "a lawful no-op must be flagged, not fall through to Tier 3")

    def test_the_refund_heuristic_honours_the_flag(self):
        src = inspect.getsource(
            __import__('rules.planeswalker', fromlist=['x']))
        i = src.index("_no_effect = (self._activation_had_no_effect")
        window = src[i:i + 700]
        assert "_last_ability_legal_noop" in window, (
            "a lawful no-op keeps its loyalty (CR 606.3) — refunding it "
            "would repeat the July-30 Wrenn-and-Six mistake")

    def test_the_flag_is_reset_per_activation(self):
        src = inspect.getsource(
            __import__('rules.planeswalker', fromlist=['x']))
        assert "self._last_ability_legal_noop = False" in src, (
            "a stale flag would suppress a LATER activation's refund")


class TestDiscardEmptyHandWording:
    def test_empty_hand_says_so(self, rules, game, capsys):
        rules._execute_action_on_state(game, {
            "action": "discard", "player": "Rick", "card": "worst"})
        out = capsys.readouterr().out
        assert "hand is empty" in out, out
        assert "'worst' not in hand" not in out, (
            "the sentinel selectors only reach that branch when the hand is "
            "empty — the old wording read as a search for a card named "
            "'worst' and nearly caused a false-positive audit finding")

    def test_a_real_missing_card_still_reports_as_missing(self, rules, game,
                                                          make_card, capsys):
        game.players[0].hand.append(make_card("Forest"))
        rules._execute_action_on_state(game, {
            "action": "discard", "player": "Rick", "card": "Black Lotus"})
        out = capsys.readouterr().out
        assert "not in hand" in out and "hand is empty" not in out, out


class TestExtraCombatDiscardLabel:
    def test_label_distinguishes_game_over_from_a_mid_combat_regrant(self):
        import mtg.autoplay
        src = inspect.getsource(mtg.autoplay)
        # BOTH consumption loops emit this line; the first pass fixed only
        # one and this pin caught it.
        assert src.count("[EXTRA-COMBAT] Discarding") == 2
        assert src.count("the game ended before it could be taken") == 2, (
            "both extra-combat loops must distinguish the two cases")
        assert src.count("if game.ended else") >= 2, (
            "when the game ended first the loop never ran, so the dropped "
            "phase is the ORIGINAL grant — blaming a mid-combat re-grant "
            "sends the next auditor hunting a Port Razer chain that never "
            "happened")
