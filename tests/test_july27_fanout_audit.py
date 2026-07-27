"""July 27, 2026 — 12-agent fanout over the archetypes recent audits never sampled.

The May 20 "audit sampling gap" fix told reviewers to always include the
rules-engine stress decks. It worked, then over-corrected: measured across the
three most recent named waves (12 reviewer-games), coverage was aristocrats x4,
layers x4, aminatou x3, death_replacement x3, stifle x2, replacement_chain x2
and one each of four others. The entire Phase-8 mechanic tier and every
non-Commander format had gone unexamined for 3-4 cycles.

Twelve agents over that complement found a great deal. This file pins the two
fixes completed in that session; the remainder are recorded in CLAUDE.md with
enough detail to act on.
"""
import pytest


class TestCantAttackOrBlockSubstringTrap:
    """Pacifism never stopped anything from BLOCKING.

    The aura checks in `can_attack`/`can_block` were bare substring tests —
    `"can't block" in oracle`. The standard Magic phrasing is "can't attack or
    block", which contains `"can't attack"` but NOT `"can't block"`. So every
    Pacifism/Arrest/Faith's Fetters-class aura correctly stopped attacking and
    silently failed to stop blocking.

    Observed in game_1530434723992834068 (limited): a Pacifism'd Watcher in
    the Mist (3/4) was rejected as an attacker every turn for 19 turns —
    `[COMBAT] Rejected attacker Watcher in the Mist: ... (Pacifism)` — while
    blocking and killing an attacker on seven separate turns.

    Third instance of this family: `'creature' in 'noncreature'` (Woodfall
    Primus, July 24) and the Coldsteel Heart -> Painter's Servant name match
    (May 17). Substring tests over natural-language oracle text need the full
    phrasing enumerated, not the convenient fragment.
    """

    def test_pacifism_phrasing_blocks_both_verbs(self):
        from mtg.models import _restricts_combat
        pacifism = "enchant creature\nenchanted creature can't attack or block."
        assert _restricts_combat(pacifism, 'attack') is True
        assert _restricts_combat(pacifism, 'block') is True, (
            "the bug: \"can't block\" is not a substring of "
            "\"can't attack or block\"")

    def test_reversed_phrasing_also_works(self):
        from mtg.models import _restricts_combat
        assert _restricts_combat("can't block or attack", 'attack') is True
        assert _restricts_combat("can't block or attack", 'block') is True

    def test_single_verb_restrictions_stay_narrow(self):
        """The fix must not over-apply: a can't-attack aura still allows blocking."""
        from mtg.models import _restricts_combat
        assert _restricts_combat("enchanted creature can't attack.", 'attack') is True
        assert _restricts_combat("enchanted creature can't attack.", 'block') is False
        assert _restricts_combat("enchanted creature can't block.", 'block') is True
        assert _restricts_combat("enchanted creature can't block.", 'attack') is False

    def test_unrelated_aura_restricts_nothing(self):
        from mtg.models import _restricts_combat
        assert _restricts_combat("enchanted creature gets +1/+1.", 'attack') is False
        assert _restricts_combat("enchanted creature gets +1/+1.", 'block') is False
        assert _restricts_combat("", 'block') is False
        assert _restricts_combat(None, 'block') is False

    def test_end_to_end_pacifism_stops_a_block(self, make_game, make_card):
        """Through the real can_block path, with a real attached aura."""
        game = make_game()
        rick, claude = game.players
        blocker = make_card("Watcher in the Mist",
                            type_line="Creature — Spirit", power="3", toughness="4")
        claude.battlefield.append(blocker)
        pacifism = make_card("Pacifism", type_line="Enchantment — Aura",
                             power=None, toughness=None,
                             oracle_text="Enchant creature\n"
                                         "Enchanted creature can't attack or block.")
        pacifism.attached_to = blocker.id
        rick.battlefield.append(pacifism)
        attacker = make_card("Blade Instructor",
                             type_line="Creature — Human Soldier",
                             power="1", toughness="1")
        rick.battlefield.append(attacker)
        assert blocker.can_block(attacker, game=game) is False, (
            "a Pacifism'd creature must not be able to block")
        assert blocker.can_attack(game=game) is False


class TestMainPhaseTriggerClassIsWired:
    """"At the beginning of your pre/postcombat main phase" fired for nobody.

    The MAIN1/MAIN2 branches of advance_phase printed a banner and drained
    one-shot DELAYED triggers (`_process_delayed_triggers(game, "main_phase")`
    — Necropotence-style scheduling), but there was no battlefield SCAN, and
    `scheduled_event_types` in rules/effect_templates.py excluded the event.

    Found via Tymna the Weaver — a COMMANDER whose entire card-advantage
    engine is "At the beginning of each of your postcombat main phases, you
    may pay X life ... draw X cards" — doing nothing across a full game. Same
    shape as Baral's cost reduction the day before, and the same shape as the
    May 30 `beginning_combat` finding (template existed, nothing dispatched).
    """

    def test_scan_function_exists(self):
        from mtg import triggers
        assert hasattr(triggers, '_check_main_phase_triggers_sync')

    def test_engine_delegator_exists(self):
        from mtg.engine import GameEngine
        assert hasattr(GameEngine, '_check_main_phase_triggers_sync')

    def test_both_main_phases_dispatch_the_scan(self):
        """MAIN1 and MAIN2 must each call it — precombat and postcombat are
        distinct trigger events."""
        import inspect
        from mtg.engine import GameEngine
        src = inspect.getsource(GameEngine.advance_phase)
        assert src.count("_check_main_phase_triggers_sync") == 2, (
            "expected one dispatch in MAIN1 and one in MAIN2")
        assert "_check_main_phase_triggers_sync(game, True)" in src, "MAIN1 (precombat)"
        assert "_check_main_phase_triggers_sync(game, False)" in src, "MAIN2 (postcombat)"

    def test_template_gate_accepts_main_phase(self):
        """Without this the library refuses to resolve the event type at all."""
        import inspect
        from rules import effect_templates
        src = inspect.getsource(effect_templates)
        assert '"main_phase"' in src and 'scheduled_event_types' in src
        i = src.index('scheduled_event_types = {')
        assert '"main_phase"' in src[i:i + 120], (
            "main_phase must be in scheduled_event_types")

    def test_precombat_and_postcombat_are_distinguished(self, make_game, make_card):
        """A postcombat trigger must not fire in the precombat main phase."""
        from mtg.engine import GameEngine
        from mtg.triggers import _check_main_phase_triggers_sync
        engine = GameEngine(None)
        game = make_game()
        rick = game.players[0]
        rick.battlefield.append(make_card(
            "Postcombat Thing", type_line="Enchantment",
            power=None, toughness=None,
            oracle_text="At the beginning of your postcombat main phase, "
                        "draw a card."))
        _msgs, unhandled_pre = _check_main_phase_triggers_sync(engine, game, True)
        _msgs, unhandled_post = _check_main_phase_triggers_sync(engine, game, False)
        assert not unhandled_pre, "a postcombat trigger must not fire precombat"
        assert unhandled_post, "the postcombat scan must see it"


class TestDealtCombatDamageTracking:
    """Tymna counts opponents dealt combat damage this turn — nothing tracked it."""

    def test_field_is_declared_and_defaults_false(self):
        from mtg.models import Player
        p = Player(name="P", life=40)
        assert p.dealt_combat_damage_this_turn is False
        assert 'dealt_combat_damage_this_turn' in {
            f.name for f in Player.__dataclass_fields__.values()}, (
            "must be a declared field, not an attribute staple (see the ratchet)")

    def test_reset_happens_at_turn_end(self):
        import inspect
        from mtg.engine import GameEngine
        src = inspect.getsource(GameEngine.end_turn)
        assert "dealt_combat_damage_this_turn = False" in src, (
            "the flag must reset per turn alongside life_lost_this_turn")

    def test_combat_damage_sets_it(self):
        import inspect
        from mtg import combat
        src = inspect.getsource(combat)
        assert "dealt_combat_damage_this_turn = True" in src
