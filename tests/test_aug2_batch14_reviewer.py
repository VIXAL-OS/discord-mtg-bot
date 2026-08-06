"""Aug 2, 2026 — batch-14 reviewer wave (4 Sonnet, recency-of-attention).

Sampled the longest-unexamined complement: LEGACY #84 (the last
never-examined format spot-check), the cube autodraft (last seen batch-11),
a phase-1 classic (baral/rashmi, untouched since June), and a MYTHIC game
chosen as the epicenter of the newest combat code. 12 findings, 0 flat
false positives — the seventh consecutive clean wave, and the
recency-of-attention rule paid out again: three of the four CRITICALs are
in cards or paths no reviewer had ever looked at.

R-L1 (CRITICAL, game-deciding) Cascade fired "whenever a player casts"
     triggers AFTER resolving the cascaded spell. The opponent-cast scan
     walks a LIVE battlefield, so a cascaded Assassin's Trophy destroyed
     the Eidolon of the Great Revel that should have triggered on it; the
     scan then found nothing and the 2 damage never happened. The caster
     was at 1 life, so the dropped trigger flipped the winner of
     game_1533407568360112128. The main cast path has always fired cast
     triggers before resolution (CR 601.2i / 603.3); only cascade's
     free-cast mini-pipeline was inverted.

R-L2 (MAJOR) Searing Blood reported "no legal target" while its own
     controller had two Monastery Swiftspears out. "Target creature" is
     unrestricted, so the cast was legal and the card + {R}{R} were burned
     for nothing. Declared targets are honored now (and the second clause
     follows the real target's controller); the AUTO-pick stays
     opponent-only on purpose, because blind-targeting your own creature
     with this card is self-harm.

R-B1 (MAJOR) Remand had no template AND there was no "hand" destination in
     the countered-spell dispatch at all — so even a template asking for it
     could not have routed. The countered spell went to the graveyard and
     the draw never happened. Memory Lapse (library_top) is the working
     sibling one destination over.

R-B2 (MAJOR) Tale's End was castable at any spell. The ability-only-counter
     cast gate skips anything whose text contains "spell", and Tale's End
     says "legendary spell" — so it targeted a non-legendary Apex
     Devastator, and the resolution-time legendary check then fizzled it,
     burning the card. CR 601.2c: there was no legal target to begin with.

R-M1 (CRITICAL) Phyrexian Processor charged its life payment TWICE
     (26 → 16 → 6). The ETB prompt queues a pending_action unconditionally
     and the name-keyed template ALSO pays; the drain's _processor_paid
     guard is written nowhere except inside the drain itself, so it never
     saw the template's payment.

R-M2 (CRITICAL) Jeska, Thrice Reborn's [0] was refused on every activation
     in every game. The combat-shaped resolve guard matches the substring
     "combat damage" anywhere, and Jeska's text merely REFERENCES it as the
     condition of a future replacement ("if that creature would deal combat
     damage ... it deals triple that damage instead") — a legal
     sorcery-speed loyalty ability that deals nothing now.

R-M3 (CRITICAL) Moraug's "at the beginning of that combat, untap all
     creatures you control" ran inline at landfall time — during a main
     phase, before anything had attacked — so it untapped 0 creatures every
     time and the extra combats it granted found no eligible attackers. The
     untap is a delayed effect belonging to the START of the granted combat
     (CR 603.7).

I-4  (inline) A battlefield permanent's suspend REMINDER text was read as a
     real upkeep trigger. Mox Tantalite is a plain mana rock once it
     resolves; the suspend clause describes it while EXILED, and the
     suspend machinery owns that. 15 wasted Tier-3 drains, all reporting
     "no state change" — the #3 escalation of the batch.
"""
import asyncio
import inspect

import pytest

import mtg.triggers  # noqa: F401 — registers the bus subscribers at import


class TestCascadeFiresCastTriggersBeforeResolving:
    """R-L1 — see also the structural ordering pin in
    tests/test_slice4a_cast_shadow.py, which guards the source order."""

    def test_opponent_cast_scan_runs_before_the_spell_mutates_the_board(self):
        """The mechanism, stated as source order.

        A behavioral repro needs a real async cascade with a stocked
        library; the ordering IS the bug, so pin it directly and precisely.
        """
        src = inspect.getsource(mtg.triggers)
        fire = src.index("_check_cast_triggers(\n                        engine, game, caster, found_card)")
        announce = src.index("[CASCADE-SPELL] {card.name} cascade found")
        resolve = src.index("[CASCADE-SPELL] Tier 1.5 resolved")
        assert announce < fire < resolve, (
            "cast triggers must fire between the cascade announcement and "
            "the cascaded spell's own resolution (CR 601.2i)")

    def test_only_one_cast_trigger_fire_remains(self):
        """The move must not have left the old post-resolution call behind —
        a duplicate would double every Rhystic Study / prowess trigger."""
        src = inspect.getsource(mtg.triggers)
        assert src.count("_check_cast_triggers(\n                        engine, game, caster, found_card)") == 1
        # Exactly one CARD_CAST emit for the cascade. (PERMANENT_ENTERED also
        # carries via="cascade" for the creature branch — a different event,
        # correctly present; count the CARD_CAST emit specifically.)
        assert src.count("events.emit(events.CARD_CAST, game, card=found_card") == 1


class TestSearingBloodHonorsDeclaredTargets:
    """R-L2."""

    ORACLE = ("Searing Blood deals 2 damage to target creature. When that "
              "creature dies this turn, Searing Blood deals 3 damage to the "
              "creature's controller.")

    def _resolve(self, game, lib, make_card, own_creatures=(), opp_creatures=(),
                 declared=None):
        from rules.effect_templates import build_game_context
        rick, claude = game.players
        for n in own_creatures:
            rick.battlefield.append(make_card(n, power="1", toughness="1"))
        for n in opp_creatures:
            claude.battlefield.append(make_card(n, power="1", toughness="1"))
        ctx = build_game_context(game, rick, claude, explicit_target=declared)
        return lib.resolve_spell("Searing Blood", self.ORACLE, rick.name,
                                 claude.name, game_context=ctx)

    def test_declared_own_creature_is_targeted(self, game, lib, make_card):
        actions, _ = self._resolve(
            game, lib, make_card, own_creatures=["Monastery Swiftspear"],
            declared="Monastery Swiftspear")
        dmg = [a for a in actions if a.get("action") == "deal_damage"]
        assert dmg, f"a declared legal target must resolve, got {actions}"
        assert dmg[0]["target_card"] == "Monastery Swiftspear"
        assert dmg[0]["target_controller"] == "Rick", (
            "the second clause follows the TARGET's controller, not the "
            "opponent — a self-targeted Searing Blood burns its caster")

    def test_declared_own_creature_burns_its_own_controller(self, game, lib,
                                                            make_card):
        actions, _ = self._resolve(
            game, lib, make_card, own_creatures=["Monastery Swiftspear"],
            declared="Monastery Swiftspear")
        # The contingent 3 damage is registered now and fires only if the
        # exact creature actually dies this turn; it is not predicted and
        # applied during spell resolution.
        watchers = [a for a in actions
                    if a.get("action") == "schedule_death_trigger"]
        face = watchers[0]["on_death_actions"]
        assert face[0]["target_player"] == "Rick", (
            "CR: 'the creature's controller' — targeting your own creature "
            "points the 3 damage at YOU")

    def test_auto_pick_still_prefers_the_opponent(self, game, lib, make_card):
        actions, _ = self._resolve(
            game, lib, make_card, own_creatures=["Goblin Guide"],
            opp_creatures=["Tarmogoyf"])
        dmg = [a for a in actions if a.get("action") == "deal_damage"]
        assert dmg[0]["target_card"] == "Tarmogoyf"
        assert dmg[0]["target_controller"] == "Claude"

    def test_auto_pick_declines_rather_than_self_harming(self, game, lib,
                                                         make_card):
        """The deliberate half: with only own creatures and NO declared
        target, declining beats blind self-harm."""
        actions, _ = self._resolve(game, lib, make_card,
                                   own_creatures=["Monastery Swiftspear"])
        assert actions and actions[0].get("action") == "no_action"


class TestRemandReturnsToHandAndDraws:
    """R-B1."""

    def test_hand_destination_exists_in_the_dispatch(self):
        import mtg.spells
        src = inspect.getsource(mtg.spells)
        assert '_countered_to == "hand"' in src, (
            "without a hand branch, no template can express Remand")

    def test_template_is_registered_with_both_clauses(self, lib):
        actions, desc = lib.resolve_spell(
            "Remand",
            "Counter target spell. If that spell is countered this way, put "
            "it into its owner's hand instead of into that player's "
            "graveyard. Draw a card.",
            "Rick", "Claude", game_context={})
        assert actions, "Remand must have a template"
        kinds = [a.get("action") for a in actions]
        assert "counter_spell" in kinds and "draw_cards" in kinds, kinds
        counter = next(a for a in actions if a["action"] == "counter_spell")
        assert counter.get("countered_to") == "hand", counter

    def test_memory_lapse_still_routes_to_library_top(self, lib):
        """Control — the sibling redirect must be untouched."""
        actions, _ = lib.resolve_spell(
            "Memory Lapse",
            "Counter target spell. If that spell is countered this way, put "
            "it on top of its owner's library instead of into that player's "
            "graveyard.", "Rick", "Claude", game_context={})
        counter = next(a for a in actions if a["action"] == "counter_spell")
        assert counter.get("countered_to") == "library_top"


class TestTalesEndNeedsALegendaryTargetOrAnAbility:
    """R-B2 — CR 601.2c at cast time, not a fizzle at resolution."""

    TALES_END = ("Counter target activated ability, triggered ability, or "
                 "legendary spell.")
    DISALLOW = ("Counter target spell, activated ability, or triggered "
                "ability.")
    STIFLE = "Counter target activated or triggered ability."

    def _try_cast(self, make_game, make_card, counter_oracle, stack_card):
        from mtg.engine import GameEngine
        from mtg.spells import cast_spell_async
        from mtg.models import StackEntry
        game = make_game()
        rick = game.players[0]
        for i in range(6):
            rick.battlefield.append(make_card(
                f"Island {i}", type_line="Land",
                oracle_text="{T}: Add {U}.", power=None, toughness=None))
        if stack_card is not None:
            game.stack.append(StackEntry(
                card=stack_card, controller_name=game.players[1].name,
                controller_index=1, is_spell=True))
        spell = make_card("Counter Card", type_line="Instant",
                          mana_cost="{1}{U}", cmc=2,
                          oracle_text=counter_oracle,
                          power=None, toughness=None)
        rick.hand.append(spell)
        return asyncio.run(cast_spell_async(GameEngine(None), game, rick, spell))

    def test_tales_end_rejected_at_a_nonlegendary_spell(self, make_game,
                                                        make_card):
        goyf = make_card("Apex Devastator", type_line="Creature — Chimera Hydra")
        ok, msg, _ = self._try_cast(make_game, make_card, self.TALES_END, goyf)
        assert not ok, "CR 601.2c — no legal target, no cast"
        assert "LEGENDARY" in msg or "legendary" in msg, msg

    def test_tales_end_allowed_at_a_legendary_spell(self, make_game, make_card):
        legend = make_card("Korvold, Fae-Cursed King",
                           type_line="Legendary Creature — Dragon Noble")
        ok, _msg, _ = self._try_cast(make_game, make_card, self.TALES_END, legend)
        assert ok, "a legendary spell IS a legal target"

    def test_disallow_still_unrestricted(self, make_game, make_card):
        """Control — Voidslime/Disallow really do counter 'target spell'."""
        goyf = make_card("Apex Devastator", type_line="Creature — Chimera Hydra")
        ok, _msg, _ = self._try_cast(make_game, make_card, self.DISALLOW, goyf)
        assert ok

    def test_stifle_still_rejected_with_only_a_spell(self, make_game, make_card):
        """Control — the July 24 ability-only gate is untouched."""
        goyf = make_card("Apex Devastator", type_line="Creature — Chimera Hydra")
        ok, _msg, _ = self._try_cast(make_game, make_card, self.STIFLE, goyf)
        assert not ok


class TestPhyrexianProcessorPaysOnce:
    """R-M1 — the template IS the payment; the prompt must retire."""

    ORACLE = ("As this artifact enters, pay any amount of life.\n"
              "{4}, {T}: Create an X/X black Phyrexian Minion creature token, "
              "where X is the life paid as this entered.")

    def test_template_payment_clears_the_pending_prompt(self, make_game,
                                                        make_card):
        from mtg.engine import GameEngine
        from mtg.spells import cast_spell_async
        game = make_game()
        rick = game.players[0]
        for i in range(6):
            rick.battlefield.append(make_card(
                f"Waste {i}", type_line="Land",
                oracle_text="{T}: Add {C}.", power=None, toughness=None))
        proc = make_card("Phyrexian Processor", type_line="Artifact",
                         mana_cost="{4}", cmc=4, oracle_text=self.ORACLE,
                         power=None, toughness=None)
        rick.hand.append(proc)
        life_before = rick.life
        ok, msg, _ = asyncio.run(
            cast_spell_async(GameEngine(None), game, rick, proc))
        assert ok, msg
        pending = getattr(game, 'pending_action', None)
        assert not (isinstance(pending, dict)
                    and pending.get('type') == 'pay_life_etb'), (
            "a stale pay_life_etb prompt is what the autoplay drain paid a "
            "SECOND time (26 -> 16 -> 6)")
        assert rick.life < life_before, "the template's payment still happens"


class TestJeskaStyleSetupAbilitiesAreNotRefused:
    """R-M2 — 'references combat damage' != 'deals combat damage now'."""

    JESKA = ("Jeska, Thrice Reborn planeswalker [0] ability: Choose target "
             "creature. Until your next turn, if that creature would deal "
             "combat damage to one of your opponents, it deals triple that "
             "damage to that player instead.")
    HALLUCINATION = ("Craterhoof enters, pump team for +10/+10 trample. "
                     "Attack for lethal.")

    def _refused(self, text):
        """Calls the PRODUCTION predicate.

        The first version of this pin mirrored the regexes here instead, and
        duly SURVIVED its own mutant — a mirrored copy passes no matter what
        production does. Shared logic, not copied logic.
        """
        from mtg.judge import is_combat_shaped_resolve
        return is_combat_shaped_resolve(text)

    def test_the_guard_consumes_the_shared_predicate(self):
        import mtg.judge
        src = inspect.getsource(mtg.judge.resolve_effect)
        assert "is_combat_shaped_resolve(effect_description)" in src, (
            "resolve_effect must call the shared predicate, not re-inline it")

    def test_jeska_setup_ability_is_allowed(self):
        assert not self._refused(self.JESKA), (
            "Jeska's [0] is a legal sorcery-speed loyalty ability that deals "
            "nothing now — refusing it made the card permanently dead")

    def test_the_original_hallucination_is_still_refused(self):
        """Control — the May 20 guard's actual target must still be caught."""
        assert self._refused(self.HALLUCINATION)

    def test_plain_combat_damage_claim_still_refused(self):
        assert self._refused(
            "Etali deals combat damage to each opponent right now.")


class TestMoraugUntapsAtTheGrantedCombat:
    """R-M3 — CR 603.7, the untap belongs to the granted combat's start."""

    def test_landfall_schedules_instead_of_untapping_inline(self):
        src = inspect.getsource(mtg.triggers)
        i = src.index('"moraug" in perm.name.lower()')
        window = src[i:i + 1400]
        assert "_extra_combat_untaps += 1" in window, (
            "the untap must be scheduled for the granted combat")
        assert "c.tapped = False" not in window, (
            "untapping inline at landfall time untaps nothing — land drops "
            "happen before the turn's real combat")

    def test_both_consumption_loops_apply_the_rider(self):
        import mtg.autoplay
        src = inspect.getsource(mtg.autoplay)
        assert src.count("_extra_combat_untaps") >= 2, (
            "the human/Moraug loop AND the Claude loop must both untap")
        assert src.count("game._extra_combat_untaps -= 1") == 2, (
            "one decrement per consumption loop — a shared counter that is "
            "never consumed would untap every extra combat forever")

    def test_claude_loop_actually_untaps_the_attackers(self, game, make_card):
        """Behavioral, via the existing stub cog.

        The structural counts above pass even if the untap loop is gutted,
        so exercise it: a creature tapped from the turn's REGULAR combat must
        be untapped and available when the granted combat begins — that is
        the whole point of the card, and it untapped 0 creatures every time
        before this fix.
        """
        from mtg.autoplay import _claude_extra_combats
        from tests.test_aug2_claude_extra_combats import (
            _StubCog, _claude_active)
        claude = _claude_active(game)
        razer = make_card("Port Razer", type_line="Creature — Orc Pirate",
                          power="4", toughness="4")
        razer.summoning_sick = False
        razer.tapped = True          # attacked in the regular combat
        claude.battlefield.append(razer)
        game._additional_combats = 1
        game._extra_combat_untaps = 1
        cog = _StubCog(attackers_answer=["Port Razer"])
        asyncio.run(_claude_extra_combats(cog, None, game))
        # The declare loop filters tapped creatures, so "it got to attack" IS
        # the observable that the untap happened at the right moment. (It is
        # tapped again afterwards — by attacking.)
        assert cog.resolved == 1, (
            "the untapped creature must actually get to attack — the live "
            "bug was extra combats reporting 'No attackers'")
        assert not any("No attackers" in m for m in cog.sent), cog.sent
        assert game._extra_combat_untaps == 0, "the rider is consumed once"

    def test_without_the_rider_a_tapped_creature_cannot_attack(
            self, game, make_card):
        """Control — this is the pre-fix behavior, and also the correct
        behavior for an extra combat granted by Port Razer or Karlach, whose
        grants carry no untap. Only Moraug's does."""
        from mtg.autoplay import _claude_extra_combats
        from tests.test_aug2_claude_extra_combats import (
            _StubCog, _claude_active)
        claude = _claude_active(game)
        razer = make_card("Port Razer", type_line="Creature — Orc Pirate",
                          power="4", toughness="4")
        razer.summoning_sick = False
        razer.tapped = True
        claude.battlefield.append(razer)
        game._additional_combats = 1
        game._extra_combat_untaps = 0
        cog = _StubCog(attackers_answer=["Port Razer"])
        asyncio.run(_claude_extra_combats(cog, None, game))
        assert razer.tapped, "no rider, no untap"
        assert cog.resolved == 0
        assert any("No attackers" in m for m in cog.sent), cog.sent

    def test_the_rider_is_a_declared_field_reset_with_its_sibling(self):
        from mtg.models import GameState
        assert any(f.name == "_extra_combat_untaps"
                   for f in GameState.__dataclass_fields__.values()), (
            "declared, not stapled (the ratchet)")
        import mtg.autoplay
        src = inspect.getsource(mtg.autoplay)
        assert src.count("_extra_combat_untaps = 0") == \
            src.count("_additional_combats = 0"), (
            "a stale rider would untap on a LATER turn's extra combat")


class TestSuspendReminderIsNotAnUpkeepTrigger:
    """I-4 — Mox Tantalite is a plain mana rock once it resolves."""

    MOX = ("Suspend 3—{0} (Rather than cast this card from your hand, pay "
           "{0} and exile it with three time counters on it. At the "
           "beginning of your upkeep, remove a time counter. When the last "
           "is removed, you may cast it without paying its mana cost.)\n"
           "{T}: Add one mana of any color.")
    VANISHING = ("Vanishing 3 (This permanent enters with three time "
                 "counters on it. At the beginning of your upkeep, remove a "
                 "time counter from it. When the last is removed, sacrifice "
                 "it.)")

    def _has_upkeep_trigger(self, oracle):
        """Calls the PRODUCTION predicate (see the judge note above — a
        mirrored copy is what let the first mutant survive)."""
        from mtg.triggers import has_battlefield_upkeep_trigger
        return has_battlefield_upkeep_trigger(oracle)

    def test_suspend_reminder_does_not_queue_an_upkeep_trigger(self):
        assert not self._has_upkeep_trigger(self.MOX), (
            "a resolved Mox Tantalite has no upkeep trigger — the suspend "
            "clause describes it while EXILED (15 wasted Tier-3 drains)")

    def test_vanishing_reminder_still_counts(self):
        """Control — vanishing/fading/echo/cumulative-upkeep also state their
        triggers in reminder text and ARE real for a battlefield permanent,
        so a blanket paren-strip would have broken them."""
        assert self._has_upkeep_trigger(self.VANISHING)

    def test_a_real_upkeep_trigger_alongside_suspend_survives(self):
        assert self._has_upkeep_trigger(
            self.MOX + "\nAt the beginning of your upkeep, draw a card.")

    def test_production_scan_uses_the_shared_predicate(self):
        src = inspect.getsource(mtg.triggers._check_upkeep_triggers_sync)
        assert "has_battlefield_upkeep_trigger(card.oracle_text)" in src, (
            "the scan must consume the shared predicate, not re-inline it")
