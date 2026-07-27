"""July 26, 2026 batch-7 audit (game_15304*, 152 games, sha=280e0ab).

The batch was clean on every hard zero — [EVENT-PARITY-CAST]=0 (the slice-4b
gate), the retired [EVENT-PARITY]/[EVENT-PARITY-DIES] tags absent entirely,
zero tracebacks, 302/302 decks legal — with ONE exception: the LIFO rescue
that shipped in the July 24 sprint reported `LIFO rescue exhausted` 5 times
against a documented healthy baseline of 0-1.

Traced in game_1530441479389184000:

    1594  Claude casting response: Pact of Negation targeting Worldly Tutor
    1605  [STACK-LIFO-GUARD] Worldly Tutor ... buried ... not resolving (CR 608)
    ...   extension cap (5) hit; rescue runs its 3 cycles
    1653  [STACK] LIFO rescue exhausted for Worldly Tutor; resolving anyway
    1654  [SEARCH-LIBRARY] Rick Deckard found Craterhoof Behemoth → library_top

Worldly Tutor resolved while the counter TARGETING it was still on the stack
above it — exactly the CR 608 violation the July 24 `_force_stack_above` work
existed to prevent. (The outcome was accidentally survivable that game because
Frilled Mystic countered the Pact ~85 lines later, but the ordering was wrong
and a Pact that stuck would have been defeated.)

Mechanism: `_force_stack_above` resolves stalled TRIGGER entries inline, but
for a SPELL above it can only wake `resolution_event`. When that event is
ALREADY SET the wake branch is a no-op — the spell's own coroutine owns the
pop and is simply slower than our budget. The old fixed `range(3)` budget then
expired and fell through to resolve-anyway. Batch evidence: the wake branch
fired exactly ONCE across 152 games, and that one cap-hit did NOT exhaust.

Fix pinned here: the rescue budget is progress-aware. It spends a base of 3
cycles unconditionally, then keeps going only while the stack above is
demonstrably moving — either shrinking (`_entries_above`) or holding an awake
spell (`_awake_spell_above`) — up to a finite `_MAX_LIFO_RESCUE_CYCLES` that
preserves the original anti-deadlock guarantee.
"""
import asyncio
import inspect

import pytest


def _stack_entry(make_card, name, controller_name, controller_index,
                 *, event_set=False, is_spell=True, target=None):
    from mtg.models import StackEntry
    card = make_card(name, mana_cost="{1}{U}", cmc=2, type_line="Instant")
    entry = StackEntry(card=card, controller_name=controller_name,
                       controller_index=controller_index, target=target,
                       is_spell=is_spell)
    entry.resolution_event = asyncio.Event()
    if event_set:
        entry.resolution_event.set()
    return entry


class TestEntriesAbove:
    """`_entries_above` is the progress signal — it must be exact."""

    def test_on_top_is_zero(self, make_game, make_card):
        from mtg.spells import _entries_above
        game = make_game()
        e = _stack_entry(make_card, "Worldly Tutor", game.players[0].name, 0)
        game.stack.append(e)
        assert _entries_above(game, e) == 0

    def test_counts_only_entries_above(self, make_game, make_card):
        from mtg.spells import _entries_above
        game = make_game()
        bottom = _stack_entry(make_card, "Worldly Tutor", game.players[0].name, 0)
        mid = _stack_entry(make_card, "Pact of Negation", game.players[1].name, 1)
        top = _stack_entry(make_card, "Frilled Mystic", game.players[0].name, 0)
        game.stack.extend([bottom, mid, top])
        assert _entries_above(game, bottom) == 2
        assert _entries_above(game, mid) == 1
        assert _entries_above(game, top) == 0

    def test_absent_entry_is_negative_one(self, make_game, make_card):
        from mtg.spells import _entries_above
        game = make_game()
        e = _stack_entry(make_card, "Worldly Tutor", game.players[0].name, 0)
        # never pushed
        assert _entries_above(game, e) == -1

    def test_shrinking_stack_reads_as_progress(self, make_game, make_card):
        """The loop's progress test is `0 <= above < prev_above`."""
        from mtg.spells import _entries_above
        game = make_game()
        bottom = _stack_entry(make_card, "Genesis Wave", game.players[0].name, 0)
        t1 = _stack_entry(make_card, "Archmage Emeritus", game.players[1].name, 1,
                          is_spell=False)
        t2 = _stack_entry(make_card, "Talrand, Sky Summoner", game.players[1].name, 1,
                          is_spell=False)
        game.stack.extend([bottom, t1, t2])
        before = _entries_above(game, bottom)
        game.stack.remove(t2)          # a trigger above got resolved
        after = _entries_above(game, bottom)
        assert before == 2 and after == 1
        assert 0 <= after < before, "shrinking stack must read as progress"


class TestAwakeSpellAbove:
    """The exact condition the old budget was blind to."""

    def test_detects_already_woken_spell_above(self, make_game, make_card):
        """The Worldly Tutor / Pact of Negation shape from the batch."""
        from mtg.spells import _awake_spell_above
        game = make_game()
        tutor = _stack_entry(make_card, "Worldly Tutor", game.players[0].name, 0)
        pact = _stack_entry(make_card, "Pact of Negation", game.players[1].name, 1,
                            event_set=True, target=tutor.card)
        game.stack.extend([tutor, pact])
        assert _awake_spell_above(game, tutor) is True, (
            "a spell above whose resolution_event is already set is mid-"
            "resolution, not a deadlock — the rescue must keep waiting")

    def test_unwoken_spell_above_is_not_awake(self, make_game, make_card):
        from mtg.spells import _awake_spell_above
        game = make_game()
        tutor = _stack_entry(make_card, "Worldly Tutor", game.players[0].name, 0)
        pact = _stack_entry(make_card, "Pact of Negation", game.players[1].name, 1,
                            event_set=False)
        game.stack.extend([tutor, pact])
        assert _awake_spell_above(game, tutor) is False

    def test_on_top_is_never_awake_above(self, make_game, make_card):
        from mtg.spells import _awake_spell_above
        game = make_game()
        tutor = _stack_entry(make_card, "Worldly Tutor", game.players[0].name, 0,
                             event_set=True)
        game.stack.append(tutor)
        assert _awake_spell_above(game, tutor) is False, (
            "our OWN set event must not count as an awake spell above us")

    def test_absent_entry_is_not_awake(self, make_game, make_card):
        from mtg.spells import _awake_spell_above
        game = make_game()
        tutor = _stack_entry(make_card, "Worldly Tutor", game.players[0].name, 0)
        assert _awake_spell_above(game, tutor) is False


class TestPhyrexianTowerGameThreading:
    """`_apply_sac_cost_at_tap` did all its real work behind `if game is not None`
    — and BOTH callers omitted `game`, so the whole block was dead in live games.

    Empirical proof at audit time: across four batches (15291/15296/15299/15304)
    the Tower sac path was entered 126 times and
    `[PHYREXIAN-TOWER] Fired N dies-trigger(s)` appeared ZERO times. Losses
    included Blood Artist / Midnight Reaper dies-triggers, Korvold's
    "whenever you sacrifice a permanent", `_recently_died` recording, and the
    CR 903.9a commander -> command-zone redirect (a Tower-sacrificed commander
    landed in the graveyard, where Living Death then picked it up).

    Same "tests green, path dead in production" shape as the July 21
    `game._rules_engine` finding — the July 21 fix that routed this dispatch
    via engine_ref was repairing code that could never run.
    """

    def _tower(self, make_card):
        return make_card("Phyrexian Tower", type_line="Legendary Land",
                         oracle_text="{T}: Add {C}.\n{T}, Sacrifice a creature: "
                                     "Add {B}{B}.",
                         power=None, toughness=None)

    def test_tap_methods_accept_game(self):
        """Both entry points must be able to receive a game at all."""
        import inspect
        from mtg.models import Player
        for meth in (Player.tap_sources_for_cost, Player.tap_lands_for_mana):
            assert "game" in inspect.signature(meth).parameters, (
                f"{meth.__name__} cannot thread a game to _apply_sac_cost_at_tap")

    def test_sac_cost_helper_receives_the_game(self, make_game, make_card,
                                               monkeypatch):
        """The regression itself: callers must PASS the game, not just accept it."""
        from mtg.models import Player
        game = make_game()
        rick = game.players[0]
        rick.battlefield = [self._tower(make_card),
                            make_card("Doomed Traveler", power="1", toughness="1")]

        seen = {}
        real = Player._apply_sac_cost_at_tap

        def spy(self, card, game=None):
            seen["game"] = game
            return real(self, card, game)

        monkeypatch.setattr(Player, "_apply_sac_cost_at_tap", spy)
        rick.tap_sources_for_cost("{B}{B}", game=game)
        assert "game" in seen, "the Tower sac path was never reached by this cost"
        assert seen["game"] is game, (
            "the sac-cost helper got game=None — every dies/sacrifice trigger, "
            "_recently_died record and the CR 903.9a commander redirect is "
            "gated behind `if game is not None` and would be skipped")

    def test_default_still_none_for_unthreaded_callers(self):
        """Backwards compatibility: the param is optional by design."""
        import inspect
        from mtg.models import Player
        for meth in (Player.tap_sources_for_cost, Player.tap_lands_for_mana,
                     Player._apply_sac_cost_at_tap):
            assert inspect.signature(meth).parameters["game"].default is None


class TestAnyColorSourceFillsColoredShortfall:
    """A partially-filled colored requirement double-subtracted its own mana.

    `remaining_needs[color]` is already the OUTSTANDING shortfall (Phase 1
    decrements it), but the allocation loop recomputed `needed - have`, where
    `have` is the mana that decrement already accounted for. Result: shortfall
    read as 0, no 'any' source was ever assigned to the gap, and the final
    verification then rejected a cost the board could pay.

    Found in game_1530441513702785114 — Phyrexian Arena ({1}{B}{B}) rejected
    twice on Swamp + Command Tower + Urborg + Reliquary Tower, then cast fine
    the turn a SECOND Swamp arrived.
    """

    def _player(self, make_card, *lands):
        from mtg.models import Player
        p = Player(name="Claude", life=40)
        p.battlefield = list(lands)
        p.commander_color_identity = ["B"]
        return p

    def _swamp(self, make_card):
        return make_card("Swamp", type_line="Basic Land — Swamp",
                         oracle_text="", power=None, toughness=None)

    def _any_source(self, make_card):
        return make_card("Command Tower", type_line="Land", power=None,
                         toughness=None,
                         oracle_text="{T}: Add one mana of any color in your "
                                     "commander's color identity.")

    def test_one_dedicated_plus_one_any_pays_two_pips(self, make_card):
        """The minimal shape. Two sources, two pips — must be payable."""
        p = self._player(make_card, self._swamp(make_card),
                         self._any_source(make_card))
        assert p.tap_sources_for_cost("{B}{B}") is True, (
            "an any-color source must be allocatable to a colored shortfall")

    def test_the_game_board_that_failed(self, make_card):
        """The literal turn-9 board from game_1530441513702785114."""
        p = self._player(
            make_card, self._swamp(make_card), self._any_source(make_card),
            make_card("Urborg, Tomb of Yawgmoth", type_line="Legendary Land",
                      power=None, toughness=None,
                      oracle_text="Each land is a Swamp in addition to its "
                                  "other land types."),
            make_card("Reliquary Tower", type_line="Land", power=None,
                      toughness=None, oracle_text="{T}: Add {C}."))
        assert p.tap_sources_for_cost("{1}{B}{B}") is True, (
            "Phyrexian Arena was rejected on this exact board")

    def test_still_rejects_a_genuinely_unpayable_cost(self, make_card):
        """The fix must not become a false ACCEPT — one source cannot pay two pips."""
        p = self._player(make_card, self._swamp(make_card))
        assert p.tap_sources_for_cost("{B}{B}") is False

    def test_wrong_color_is_still_rejected(self, make_card):
        """An any-color source restricted to B must not pay for {R}."""
        p = self._player(make_card, self._swamp(make_card))
        assert p.tap_sources_for_cost("{R}") is False


class TestTargetPowerFollowsTheDeclaredTarget:
    """Templates picked an explicit target but read power from a DIFFERENT creature.

    Every site did:
        target       = ctx['explicit_target_name'] or ctx['best_opponent_creature']
        target_power = ctx['best_opponent_creature_power']

    `best_opponent_creature_power` is derived independently as the single
    highest-power opponent creature, so the two decouple the moment the
    declared target isn't that creature. In game_1530445545447886909 Swords to
    Plowshares exiled a 0-power Birds of Paradise and gave its controller 1
    life — Elvish Mystic's power.
    """

    def _ctx(self):
        return {
            '_opponent_creatures': [
                {'name': 'Birds of Paradise', 'power': 0, 'colors': ['G'],
                 'type_line': 'creature — bird'},
                {'name': 'Elvish Mystic', 'power': 1, 'colors': ['G'],
                 'type_line': 'creature — elf druid'},
            ],
            '_controller_creatures': [
                {'name': 'Serra Angel', 'power': 4, 'colors': ['W'],
                 'type_line': 'creature — angel'},
            ],
            'best_opponent_creature': 'Elvish Mystic',
            'best_opponent_creature_power': 1,
        }

    def test_uses_the_declared_targets_power(self):
        from rules.effect_templates import resolve_target_power
        assert resolve_target_power(self._ctx(), 'Birds of Paradise') == 0, (
            "Swords to Plowshares must grant life equal to the EXILED "
            "creature's power, not the best creature's")

    def test_matches_case_insensitively(self):
        from rules.effect_templates import resolve_target_power
        assert resolve_target_power(self._ctx(), 'birds of paradise') == 0

    def test_finds_controller_side_targets_too(self):
        """Fight/pump templates can legally target your own creature."""
        from rules.effect_templates import resolve_target_power
        assert resolve_target_power(self._ctx(), 'Serra Angel') == 4

    def test_falls_back_when_target_is_unknown(self):
        """No explicit target -> previous behaviour, unchanged."""
        from rules.effect_templates import resolve_target_power
        assert resolve_target_power(self._ctx(), '') == 1
        assert resolve_target_power(self._ctx(), None) == 1
        assert resolve_target_power(self._ctx(), 'Card Not On Board') == 1

    def test_empty_context_is_safe(self):
        from rules.effect_templates import resolve_target_power
        assert resolve_target_power({}, 'Anything') == 0

    def test_swords_template_scales_life_to_the_exiled_creature(self, lib):
        """End-to-end through the real template."""
        ctx = self._ctx()
        ctx['explicit_target_name'] = 'Birds of Paradise'
        ctx['explicit_target_owner'] = 'Rick'
        actions, _desc = lib.resolve_spell(
            card_name="Swords to Plowshares",
            oracle_text="Exile target creature. Its controller gains life "
                        "equal to its power.",
            controller="Claude", opponent="Rick", game_context=ctx)
        exiles = [a for a in actions if a.get('action') == 'move_card']
        gains = [a for a in actions if a.get('action') == 'gain_life']
        assert exiles and exiles[0]['card'] == 'Birds of Paradise'
        assert gains and gains[0]['amount'] == 0, (
            f"expected 0 life for a 0-power Bird, got {gains[0]['amount']}")

    def test_no_template_still_reads_the_wrong_power(self):
        """Guard against the helper being bypassed again at any call site."""
        import re
        from pathlib import Path
        src = Path("rules/effect_templates.py").read_text(encoding="utf-8")
        # A site is suspicious when it selects an explicit target and then
        # reads the global best-power in the very next statement.
        bad = re.findall(
            r"explicit_target_name'\) or ctx\.get\('best_opponent_creature'\)\s*\n"
            r"\s*target_power = ctx\.get\('best_opponent_creature_power'",
            src)
        assert not bad, f"{len(bad)} template(s) reverted to the decoupled power read"


class TestAngrathsMaraudersIsRegistered:
    """Oracle-identical to Fiery Emancipation except double-vs-triple, but it
    had no `_NAMED_CARD_REPLACEMENTS` entry, and the generic fallback regex
    requires "a source WOULD deal damage" — this card says "a source YOU
    CONTROL would deal damage", so nothing matched and nothing registered."""

    def test_has_a_named_registration(self):
        from rules.replacement import _NAMED_CARD_REPLACEMENTS
        assert "angrath's marauders" in _NAMED_CARD_REPLACEMENTS

    def test_registers_a_doubling_effect_for_its_controller(self):
        from rules.replacement import _NAMED_CARD_REPLACEMENTS
        effects = _NAMED_CARD_REPLACEMENTS["angrath's marauders"]("m1", "Claude")
        assert len(effects) == 1
        eff = effects[0]
        assert eff.multiply_amount == 2.0, "Marauders doubles, it does not triple"
        assert eff.controller == "Claude"

    def test_only_fires_for_sources_its_controller_owns(self):
        """"a source you control" is printed text here, not a house rule."""
        from types import SimpleNamespace
        from rules.replacement import _NAMED_CARD_REPLACEMENTS
        eff = _NAMED_CARD_REPLACEMENTS["angrath's marauders"]("m1", "Claude")[0]
        assert eff.condition(SimpleNamespace(source_controller="Claude")) is True
        assert eff.condition(SimpleNamespace(source_controller="Rick")) is False
        assert eff.condition(SimpleNamespace(source_controller=None)) is False

    def test_matches_fiery_emancipations_shape(self):
        """The two cards differ only in the multiplier — keep them aligned."""
        from rules.replacement import _NAMED_CARD_REPLACEMENTS
        m = _NAMED_CARD_REPLACEMENTS["angrath's marauders"]("m", "P")[0]
        f = _NAMED_CARD_REPLACEMENTS["fiery emancipation"]("f", "P")[0]
        assert m.replaces_event == f.replaces_event
        assert m.multiply_amount == 2.0 and f.multiply_amount == 3.0


class TestPlaneswalkerLandTargeting:
    """There was no 'land' branch, and the fallback explicitly excludes lands —
    so "target land" abilities were GUARANTEED a non-land target. Garruk
    Wildspeaker's "+1: Untap two target lands" picked the opponent's Sword of
    Fire and Ice in game_1530445545447886909."""

    def test_land_branch_exists_before_the_land_excluding_fallback(self):
        import inspect
        from mtg import autoplay
        src = inspect.getsource(autoplay)
        land_branch = src.find("elif 'land' in target_desc:")
        fallback = src.find("elif ability.needs_target:")
        assert land_branch != -1, "no 'land' target branch"
        assert fallback != -1
        assert land_branch < fallback, (
            "the land branch must precede the fallback, which filters lands out")


class TestTierThreeProseIsNotPostedWhenNothingHappened:
    """`messages.extend(resolve_msgs)` ran unconditionally, ABOVE the console
    line that claims "(suppressed)" — so a no-op Tier 3 resolution still posted
    its raw explanation. game_1530445545447886909 shipped verbatim
    chain-of-thought to Discord: "He chooses to return the Elvish Mystic? No,
    ... So there is no target, ability fizzles"."""

    def test_extend_is_gated_on_actions(self):
        import inspect
        from mtg import triggers
        src = inspect.getsource(triggers.drain_pending_triggers)
        gated = src.find("if actions:\n                messages.extend(resolve_msgs)")
        assert gated != -1, (
            "messages.extend(resolve_msgs) is no longer gated on actions — "
            "no-op Tier 3 prose will reach Discord again")
        assert "[RESOLVE-PROSE-DROPPED]" in src, (
            "dropped prose must still be recoverable from the console")


class TestCombatDisplayReportsPostReplacementDamage:
    """`attacker_power` is captured BEFORE damage replacement runs, so once a
    doubler was live the single-blocker branch printed the printed power
    instead of what was dealt. game_1530441531188711565: "Gisela, the Broken
    Blade deals 5 damage to Korvold" for a hit Gisela, Blade of Goldnight had
    doubled to 10 (the lifelink credit of +10 confirms the real amount).
    Display-only — damage_marked and life totals were already correct."""

    def test_actual_damage_wins_when_a_replacement_moved_it(self):
        import inspect
        from mtg import combat
        src = inspect.getsource(combat.deal_combat_damage)
        assert "actual_dmg if actual_dmg != damage_to_blocker" in src, (
            "the single-blocker display branch no longer prefers the "
            "post-replacement amount — a doubled hit will under-report again")

    def test_attacker_power_clamp_survives_for_the_unmodified_case(self):
        """The clamp exists for the July 21 trample double-count — keep it."""
        import inspect
        from mtg import combat
        src = inspect.getsource(combat.deal_combat_damage)
        assert "min(attacker_power," in src, (
            "the unmodified-damage clamp was removed; the July 21 trample "
            "double-count regression is now unguarded")


class TestRescueBudgetIsProgressAware:
    """Structural pin: don't let the budget silently revert to `range(3)`.

    The behavioural path lives inside `_await_stack_window`'s timeout ladder,
    which needs a full async cast to reach; these assertions pin the decision
    inputs so a regression to a fixed budget fails here first.
    """

    def test_cap_is_finite_and_bounded(self):
        from mtg.spells import _MAX_LIFO_RESCUE_CYCLES
        assert isinstance(_MAX_LIFO_RESCUE_CYCLES, int)
        assert 3 < _MAX_LIFO_RESCUE_CYCLES <= 50, (
            "the cap must exceed the 3-cycle base budget (else the fix is a "
            "no-op) and stay finite (else the anti-deadlock guarantee is gone)")

    def test_rescue_loop_consults_both_progress_signals(self):
        from mtg import spells
        src = inspect.getsource(spells._await_stack_window)
        assert "_MAX_LIFO_RESCUE_CYCLES" in src, (
            "rescue budget no longer bounded by the module cap")
        assert "_awake_spell_above" in src, (
            "rescue budget stopped checking for an awake spell above — this is "
            "the exact blindness that let Worldly Tutor resolve beneath the "
            "Pact of Negation targeting it")
        assert "_entries_above" in src, (
            "rescue budget stopped checking stack-shrink progress")
        assert "for _rescue in range(3)" not in src, (
            "rescue budget reverted to a fixed 3-cycle loop")
