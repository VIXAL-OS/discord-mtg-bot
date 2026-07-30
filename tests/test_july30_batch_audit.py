"""July 30, 2026 batch-9 audit (game_15322*, 152 games — the first complete
strict batch) — inline-sweep findings.

F1 (CRITICAL): a spell EXILED from the stack still resolved. Summary
Dismissal exiled Song of the Worldsoul (game_1532236251619528895), but the
exiled spell's cast_spell_async coroutine hit the timeout path, where
`not game.stack or game.stack[-1] is stack_entry` read "entry removed,
stack now empty" as "now at top" — and resolved the exiled enchantment
onto the battlefield, where it triggered for the rest of the game. Discord
showed both events back to back ("Summary Dismissal exiles Song of the
Worldsoul" at 00:08:35; "Song of the Worldsoul triggers" from 00:08:44 on).

The class, not the instance:
  - Summary Dismissal's Tier-1 handler removed entries + moved cards but
    never marked them countered and never woke their resolution_event.
  - exile_from_stack (Spell Queller) had the IDENTICAL latent gap — caught
    here before Queller's first-ever real (non-empty-stack) exile.
  - Sub-bug: Summary Dismissal exiled trigger entries' .card — which is the
    SOURCE PERMANENT, cloning a battlefield object into exile.

Contract after the fix (matches the counterspell convention documented in
_await_stack_window): a remover marks the entry countered=True +
countered_to="already_handled" (zone move done by the remover) and wakes
resolution_event; the caster's coroutine unwinds via the countered branch
without re-routing the card. The wait loop itself gained a
[STACK-ENTRY-VANISHED] net: an entry gone from the stack WITHOUT a counter
mark is treated as countered (graveyard fallback), never resolved.
"""
import asyncio

import pytest

from mtg.models import StackEntry


def _engine():
    from mtg.engine import GameEngine
    return GameEngine(None)


async def _sink(msg):
    return None


class TestSummaryDismissalHandler:
    def _sd(self, make_card):
        return make_card(
            "Summary Dismissal", type_line="Instant", mana_cost="{2}{U}{U}",
            cmc=4, oracle_text="Counter all other spells and abilities.")

    def test_exiled_spell_entry_marked_countered_and_woken(
            self, make_game, make_card):
        from mtg.spells import resolve_special_effects
        engine = _engine()
        game = make_game()
        rick, claude = game.players
        song = make_card("Song of the Worldsoul", type_line="Enchantment",
                         mana_cost="{4}{W}{W}", cmc=6, owner_index=0,
                         oracle_text="Whenever you cast a spell, populate.")
        entry = StackEntry(card=song, controller_name=rick.name,
                           controller_index=0)
        entry.resolution_event = asyncio.Event()
        game.stack.append(entry)

        msgs = resolve_special_effects(engine, game, claude, self._sd(make_card))

        assert entry not in game.stack
        assert song in rick.exile, "the handler still owns the zone move"
        assert entry.countered is True, (
            "an unmarked removal lets the caster's coroutine resolve the "
            "exiled spell (the Song of the Worldsoul bug)")
        assert getattr(entry, 'countered_to', None) == "already_handled"
        assert entry.resolution_event.is_set(), (
            "the waiting coroutine must be woken, not left to burn "
            "extensions against a vanished entry")
        assert any("Song of the Worldsoul" in m for m in msgs)

    def test_trigger_entry_removed_but_source_not_exiled(
            self, make_game, make_card):
        from mtg.spells import resolve_special_effects
        engine = _engine()
        game = make_game()
        rick, claude = game.players
        talrand = make_card("Talrand, Sky Summoner",
                            type_line="Legendary Creature — Merfolk Wizard",
                            power="2", toughness="2", owner_index=0)
        rick.battlefield.append(talrand)
        trig = StackEntry(card=talrand, controller_name=rick.name,
                          controller_index=0, is_spell=False,
                          trigger_source="Talrand, Sky Summoner")
        game.stack.append(trig)

        msgs = resolve_special_effects(engine, game, claude, self._sd(make_card))

        assert trig not in game.stack
        assert talrand in rick.battlefield, (
            "a trigger entry's .card is its SOURCE permanent — 'counter all "
            "abilities' must not move it")
        assert talrand not in rick.exile and talrand not in claude.exile, (
            "exiling the trigger source clones a battlefield object into "
            "two zones")
        assert trig.countered is True
        assert any("triggered abilit" in m for m in msgs)


class TestQuellerExileMarksEntry:
    def test_exile_from_stack_marks_countered_and_wakes(
            self, rules, game, make_card):
        from mtg.actions import execute_action_on_state
        rick = game.players[0]
        spell = make_card("Growth Spiral", type_line="Instant",
                          mana_cost="{G}{U}", cmc=2, owner_index=0)
        entry = StackEntry(card=spell, controller_name=rick.name,
                           controller_index=0)
        entry.resolution_event = asyncio.Event()
        game.stack.append(entry)

        msg = execute_action_on_state(rules, game, {
            "action": "exile_from_stack", "max_mv": 4,
            "_source_card_name": "Spell Queller",
            "_source_controller": "Claude"})

        assert entry not in game.stack
        assert spell in rick.exile
        assert entry.countered is True, (
            "Queller's exile without a counter mark = the Summary Dismissal "
            "class; the quelled spell would resolve from its coroutine")
        assert getattr(entry, 'countered_to', None) == "already_handled"
        assert entry.resolution_event.is_set()
        assert msg and "Growth Spiral" in msg


class TestPhase4DeferredItems:
    """The July 29 deferred pick-list items shipped this session."""

    def test_copy_does_not_inherit_counters(self, game, rules, make_card):
        # CR 706.2 (deferred July 28): a copy effect copies the copiable
        # values, never the counters.
        claude = game.players[1]
        titan = make_card("Grave Titan", power="6", toughness="6")
        titan.counters['+1/+1'] = 3
        claude.battlefield.append(titan)
        rules._execute_action_on_state(game, {
            "action": "create_copy_token", "player": "Claude",
            "target": "best_creature", "filter": "own", "count": 1})
        token = next(c for c in claude.battlefield
                     if getattr(c, "is_token", False))
        assert token.counters.get('+1/+1', 0) == 0

    def test_combat_resolution_drains_pending_gain_messages(self, make_game):
        # Gain-life triggers fired during combat buffered display lines that
        # only drained at the NEXT draw step — a turn late in Discord.
        from mtg.combat import resolve_combat_damage
        game = make_game()
        game._pending_messages = ["💚 Heliod: +1/+1 counter on Test Cleric"]
        msgs = resolve_combat_damage(_engine().rules, game)
        assert "💚 Heliod: +1/+1 counter on Test Cleric" in msgs
        assert not game._pending_messages

    def test_activation_failure_reason_surfaces(self, make_game, make_card):
        # Rhys the Redeemed failed 8 activations across 24 turns with
        # feedback of None/'' — the stash mirrors _last_cast_failure.
        from mtg.ai_turn import _get_action_error
        engine = _engine()
        game = make_game()
        rick = game.players[0]
        rhys = make_card("Rhys the Redeemed",
                         type_line="Legendary Creature — Elf Warrior",
                         power="1", toughness="1",
                         oracle_text=("{2}{G/W}, {T}: Create a 1/1 green and "
                                      "white Elf Warrior creature token."))
        rhys.summoning_sick = True
        rhys.entered_this_turn = True
        rick.battlefield.append(rhys)
        action = {"type": "activate", "permanent": "Rhys the Redeemed",
                  "ability": 0}
        asyncio.run(engine._execute_action(game, 0, action))
        err = _get_action_error(engine, game, 0, action)
        assert err and "summoning sickness" in err, (
            f"the AI feedback loop needs a real reason, got: {err!r}")


class TestCombatDamageTemplates:
    """F2: all 60 [DRAIN-COMBAT_DAMAGE] drains in batch 15322 were refused
    by the Tier-3 combat-shaped-resolve guard (by design) — the queue is an
    audit trail, not a resolution path. Templates per the batch's ranked
    refusal list (the Ragavan precedent); every generator gates on
    ctx['damage_dealt'] so a declare-time scan can't misfire it."""

    def _lib(self):
        from rules.effect_templates import get_effect_library
        return get_effect_library()

    def test_hellkite_tyrant_steals_all_artifacts(self, rules, game, make_card):
        from mtg.actions import execute_action_on_state
        rick, claude = game.players
        sol = make_card("Sol Ring", type_line="Artifact")
        signet = make_card("Azorius Signet", type_line="Artifact")
        bear = make_card("Bear")
        claude.battlefield.extend([sol, signet, bear])
        actions = self._lib()._gen_hellkite_tyrant(
            "Rick", "Claude", {"damage_dealt": 6, "_opponent_player": claude})
        assert len(actions) == 2
        for a in actions:
            execute_action_on_state(rules, game, dict(a))
        assert sol not in claude.battlefield and signet not in claude.battlefield
        assert bear in claude.battlefield, "creatures are not artifacts"

    def test_quartzwood_token_sized_by_damage(self, rules, game, make_card):
        from mtg.actions import execute_action_on_state
        actions = self._lib()._gen_quartzwood_crasher(
            "Rick", "Claude", {"damage_dealt": 5})
        execute_action_on_state(rules, game, dict(actions[0]))
        dino = next(c for c in game.players[0].battlefield
                    if "Dinosaur" in c.name)
        assert dino.power == "5" and dino.toughness == "5"
        assert getattr(dino, 'is_token', False)

    def test_declare_time_scan_cannot_misfire(self):
        # No damage_dealt in ctx (an attack-DECLARE scan) -> handled no-op
        # for every combat-damage generator.
        lib = self._lib()
        for gen in (lib._gen_hellkite_tyrant, lib._gen_quartzwood_crasher,
                    lib._gen_combat_damage_draw_one, lib._gen_neheb_dreadhorde,
                    lib._gen_glissa_sunslayer, lib._gen_ancient_bronze_dragon,
                    lib._gen_flaxen_intruder):
            assert gen("Rick", "Claude", {}) == [], gen.__name__

    def test_registry_reaches_the_dispatcher_lookup(self):
        lib = self._lib()
        actions, _desc = lib.resolve_attack_trigger(
            trigger_card_name="Ohran Frostfang",
            trigger_oracle=("Attacking creatures you control have deathtouch.\n"
                            "Whenever a creature you control deals combat "
                            "damage to a player, draw a card."),
            attacking_creature_name="Ohran Frostfang",
            attacking_creature_power=2,
            controller="Rick", opponent="Claude",
            game_context={"damage_dealt": 2})
        assert actions == [{"action": "draw_cards", "player": "Rick",
                            "amount": 1}]

    def test_queue_sentence_no_keyword_prefix(self, make_game, make_card, capsys):
        from mtg.triggers import queue_unhandled_combat_damage
        game = make_game()
        tyrant = make_card(
            "Test Dragon", type_line="Creature — Dragon",
            oracle_text=("Flying, trample\n"
                         "Whenever this creature deals combat damage to a "
                         "player, gain control of all artifacts that player "
                         "controls."))
        queue_unhandled_combat_damage(game, tyrant, game.players[0], 6)
        out = capsys.readouterr().out
        assert "[COMBAT-TRIGGER-UNHANDLED] Test Dragon: Whenever" in out, out


WRENN_ORACLE = (
    "+1: Return up to one target land card from your graveyard to your hand.\n"
    "−1: Wrenn and Six deals 1 damage to any target.\n"
    "−7: You get an emblem with \"Instant and sorcery cards in your "
    "graveyard have retrace.\"")


class TestUpToNoneChosenKeepsLoyalty:
    """F6: Wrenn and Six [+1] on an empty graveyard — the July 29 gate let
    the activation START, but the no-effect refund heuristic then undid the
    loyalty ([PW-REFUND] loyalty refunded (3 → 3)). CR 601.2c/608.2b: an
    "up to" activation choosing nothing is legal, resolves doing nothing,
    and the loyalty change STANDS with the once-per-turn slot consumed."""

    def test_wrenn_plus_one_empty_graveyard_keeps_loyalty(
            self, make_game, make_card, capsys):
        from rules.planeswalker import PlaneswalkerManager
        game = make_game()
        rick = game.players[0]
        wrenn = make_card("Wrenn and Six",
                          type_line="Legendary Planeswalker — Wrenn",
                          oracle_text=WRENN_ORACLE,
                          power="0", toughness="0")
        wrenn.loyalty_counters = 3
        wrenn.summoning_sick = False
        rick.battlefield.append(wrenn)
        # graveyard is empty — no land to return

        result = asyncio.run(
            PlaneswalkerManager().activate(game, rick, wrenn, 0))

        out = capsys.readouterr().out
        assert "[PW-NONE-CHOSEN]" in out, out
        assert "[PW-REFUND]" not in out, (
            "the refund heuristic pre-empted the CR-correct none-chosen "
            "resolution")
        assert result.success is True
        assert wrenn.loyalty_counters == 4, (
            "+1 loyalty must stand on a legal none-chosen activation")
        assert getattr(wrenn, '_pw_activations_this_turn', 0) == 1, (
            "the once-per-turn slot is consumed — no free retries")


WHIP_EFFECT = (
    "Return target creature card from your graveyard to the battlefield. "
    "It gains haste. Exile it at the beginning of the next end step. If it "
    "would leave the battlefield, exile it instead of putting it anywhere "
    "else.")


class TestGyReturnGuardRiders:
    """F5: the [RESOLVE-GY-RETURN] deterministic pre-guard (built July 24
    for Bruna-class bare returns) emitted ONLY the reanimate — Whip of
    Erebos's "It gains haste. Exile it at the beginning of the next end
    step." riders were dropped, making every Whip activation a PERMANENT
    reanimation (game_1532236167368544388, Phyrexian Obliterator)."""

    def _stock(self, game, make_card):
        rick = game.players[0]
        oblit = make_card("Phyrexian Obliterator",
                          type_line="Creature — Phyrexian Horror",
                          power="5", toughness="5", cmc=4)
        rick.graveyard.append(oblit)
        return rick

    def test_whip_riders_parsed(self, rules, game, make_card):
        from mtg.judge import resolve_effect
        self._stock(game, make_card)
        _msgs, actions = asyncio.run(resolve_effect(
            rules, game, WHIP_EFFECT,
            source_card="Whip of Erebos", controller="Rick"))
        assert actions[0]["action"] == "reanimate"
        assert actions[0]["card"] == "Phyrexian Obliterator"
        assert actions[0].get("haste") is True, "'It gains haste' dropped"
        assert len(actions) == 2, "the delayed-exile rider was dropped"
        sched = actions[1]
        assert sched["action"] == "schedule_delayed_trigger"
        assert sched["trigger_at"] == "end_step"
        assert "phase_of" not in sched, (
            "Whip says THE next end step — ungated (July 23 Necropotence rule)")
        assert sched["actions"][0] == {
            "action": "move_card", "card": "Phyrexian Obliterator",
            "from_zone": "battlefield", "to_zone": "exile", "player": "Rick"}

    def test_your_next_end_step_is_owner_gated(self, rules, game, make_card):
        from mtg.judge import resolve_effect
        self._stock(game, make_card)
        _msgs, actions = asyncio.run(resolve_effect(
            rules, game,
            "Return target creature card from your graveyard to the "
            "battlefield. It gains haste. Exile it at the beginning of "
            "your next end step.",
            source_card="Test Whip", controller="Rick"))
        assert actions[1].get("phase_of") == "Rick"

    def test_bruna_bare_return_unchanged(self, rules, game, make_card):
        from mtg.judge import resolve_effect
        rick = game.players[0]
        human = make_card("Thalia's Lancers",
                          type_line="Creature — Human Knight",
                          power="4", toughness="4", cmc=5)
        rick.graveyard.append(human)
        _msgs, actions = asyncio.run(resolve_effect(
            rules, game,
            "Return target Angel or Human creature card from your "
            "graveyard to the battlefield.",
            source_card="Bruna, the Fading Light", controller="Rick"))
        assert len(actions) == 1
        assert "haste" not in actions[0]


class TestEscapeTargetingGateRollback:
    """F4: the autoplay targeting pre-check (CR 601.2c) sits BETWEEN the
    graveyard extraction (escape exile cost already paid) and the
    hand-append, and returned bare — Cling to Dust was left in NO zone
    with 5 graveyard cards destroyed (game_1532229658215583864). The July
    29 rollback only covered the post-cast failure exit."""

    def test_targeting_gate_exit_restores_graveyard_cast(
            self, make_game, make_card, capsys):
        from types import SimpleNamespace
        from mtg.autoplay import _autoplay_execute_action
        cog = SimpleNamespace(engine=_engine())
        game = make_game()
        rick = game.players[0]
        escaper = make_card(
            "Test Removal", type_line="Sorcery", mana_cost="{B}", cmc=1,
            power="0", toughness="0",
            oracle_text="Destroy target creature.\n"
                        "Escape—{2}{B}, Exile three other cards from your "
                        "graveyard. (You may cast this card from your "
                        "graveyard for its escape cost.)")
        fodder = [make_card(f"Fodder {i}", type_line="Instant",
                            power="0", toughness="0") for i in range(3)]
        rick.graveyard.append(escaper)
        rick.graveyard.extend(fodder)
        rick.playable_from_graveyard.append(escaper.id)
        # No creatures anywhere → "Destroy target creature" has no legal
        # target → the pre-cast gate fires.

        result = asyncio.run(_autoplay_execute_action(
            cog, None, game, 0, {"type": "cast", "card": "Test Removal"}))

        out = capsys.readouterr().out
        assert "no valid targets" in out, out
        assert result is None
        assert escaper in rick.graveyard, (
            "the card must return to the graveyard, not vanish into no zone")
        assert escaper.id in rick.playable_from_graveyard
        assert all(f in rick.graveyard for f in fodder), (
            "the escape exile cost must be refunded")
        assert rick.exile == []
        assert escaper._was_escaped is False


TYMNA_ORACLE = (
    "Lifelink\n"
    "At the beginning of each of your postcombat main phases, you may pay "
    "X life, where X is the number of opponents that were dealt combat "
    "damage this turn. If you do, draw X cards.")


class TestMainPhaseDispatchChokePoint:
    """F3: on any turn with real combat, BOTH post-combat transitions set
    game.phase = Phase.MAIN2 directly (mtg/autoplay.py _resolve_combat,
    mtg/cog.py _autoplay_resolve_combat), bypassing advance_phase's MAIN2
    scan — so Tymna's postcombat trigger only ever ran on NO-combat turns,
    exactly when its condition is guaranteed false
    (game_1532229678751027252: she connected for 2, no scan; next turn no
    combat, scan says "condition not met"). Third iteration of the Tymna
    saga. All four MAIN1/MAIN2 entries now share one dispatcher."""

    def _tymna_board(self, make_game, make_card):
        game = make_game()
        game.active_player_index = 0
        rick, claude = game.players
        tymna = make_card("Tymna the Weaver",
                          type_line="Legendary Creature — Human Cleric",
                          power="2", toughness="2", oracle_text=TYMNA_ORACLE)
        rick.battlefield.append(tymna)
        rick.library.extend(make_card(f"Filler {i}") for i in range(3))
        return game, rick, claude

    def test_dispatcher_fires_tymna_when_opponent_was_hit(
            self, make_game, make_card, capsys):
        game, rick, claude = self._tymna_board(make_game, make_card)
        claude.dealt_combat_damage_this_turn = True
        life0, hand0 = rick.life, len(rick.hand)

        msgs = _engine().dispatch_main_phase_triggers(game, precombat=False)

        assert rick.life == life0 - 1, (
            "Tymna: pay X=1 life (one opponent dealt combat damage)")
        assert len(rick.hand) == hand0 + 1, "…and draw X=1"
        assert "[MAIN2-TRIGGER] Resolved Tymna" in capsys.readouterr().out

    def test_dispatcher_no_op_when_no_opponent_was_hit(
            self, make_game, make_card, capsys):
        game, rick, claude = self._tymna_board(make_game, make_card)
        life0, hand0 = rick.life, len(rick.hand)

        _engine().dispatch_main_phase_triggers(game, precombat=False)

        assert rick.life == life0 and len(rick.hand) == hand0
        assert "condition not met" in capsys.readouterr().out

    def test_both_direct_phase_sets_dispatch_the_scan(self):
        # Source pins (the July 29 style): a future refactor that restores
        # the bare `game.phase = Phase.MAIN2` without the dispatcher
        # reopens the gap silently — the batch can't catch it because the
        # no-combat turns still emit plausible [MAIN2-TRIGGER] lines.
        import pathlib
        root = pathlib.Path(__file__).resolve().parent.parent
        for rel in ("mtg/autoplay.py", "mtg/cog.py"):
            src = (root / rel).read_text(encoding="utf-8")
            for m in __import__("re").finditer(
                    r"game\.phase = Phase\.MAIN2", src):
                tail = src[m.end():m.end() + 400]
                assert "dispatch_main_phase_triggers" in tail, (
                    f"{rel}: a direct MAIN2 phase set no longer runs the "
                    f"main-phase trigger dispatch (the Tymna gap)")


class TestVanishedEntryNeverResolves:
    """End-to-end through cast_spell_async's real wait loop."""

    def _ready(self, make_game, make_card):
        from mtg.constants import Phase
        game = make_game()
        game.phase = Phase.MAIN1
        game.active_player_index = 0
        engine = _engine()
        # Real PrioritySystem so cast_spell_async enters the genuine
        # resolution-wait loop (the code under test). auto_pass generous:
        # a fast auto-pass cycle resolves the spell at ~0.1s, BEFORE the
        # 0.15s remover below — the coroutine's own 0.5s adaptive timeout
        # must be what governs.
        engine.setup_stack(game, auto_pass_seconds=5.0, send_func=_sink)
        # Autoplay flag → the 0.5s adaptive resolution timeout (opponent's
        # hand is empty, so "no interaction possible"). Without it the
        # non-autoplay fallback is auto_pass_seconds * 10 = 50s per cast.
        game.is_autoplay = True
        rick = game.players[0]
        for i in range(3):
            rick.battlefield.append(make_card(
                f"Plains {i}", type_line="Basic Land — Plains",
                power="0", toughness="0"))
        return engine, game, rick

    def test_summary_dismissal_mid_cast_end_to_end(
            self, make_game, make_card, capsys):
        from mtg.spells import cast_spell_async, resolve_special_effects
        engine, game, rick = self._ready(make_game, make_card)
        claude = game.players[1]
        song = make_card("Test Worldsong", type_line="Enchantment",
                         mana_cost="{1}{W}", cmc=2, owner_index=0,
                         oracle_text="")
        rick.hand.append(song)
        sd = make_card("Summary Dismissal", type_line="Instant",
                       mana_cost="{2}{U}{U}", cmc=4,
                       oracle_text="Counter all other spells and abilities.")

        async def _run():
            cast_task = asyncio.create_task(
                cast_spell_async(engine, game, rick, song))
            await asyncio.sleep(0.15)  # let the entry land on the stack
            resolve_special_effects(engine, game, claude, sd)
            return await cast_task

        result = asyncio.run(_run())
        ok, summary, _ = result

        assert song not in rick.battlefield, (
            "the exiled spell RESOLVED onto the battlefield — the Song of "
            "the Worldsoul bug is back")
        assert rick.exile.count(song) == 1, "exactly one zone: exile"
        assert song not in rick.graveyard, (
            "the countered branch re-routed a card the remover already moved")
        assert "removed from stack" in summary

    def test_unmarked_removal_hits_the_vanished_net(
            self, make_game, make_card, capsys):
        # Simulates an UNKNOWN remover that pops the entry without marking
        # it and without moving the card: the net must refuse to resolve
        # and route the card to the graveyard (standard leave-the-stack
        # default), loudly.
        from mtg.spells import cast_spell_async
        engine, game, rick = self._ready(make_game, make_card)
        bear = make_card("Test Bear", type_line="Creature — Bear",
                         mana_cost="{1}{W}", cmc=2, power="2", toughness="2",
                         owner_index=0)
        rick.hand.append(bear)

        async def _run():
            cast_task = asyncio.create_task(
                cast_spell_async(engine, game, rick, bear))
            await asyncio.sleep(0.15)
            for e in list(game.stack):
                if e.card is bear:
                    game.stack.remove(e)
            return await cast_task

        asyncio.run(_run())
        out = capsys.readouterr().out

        assert "[STACK-ENTRY-VANISHED]" in out, out
        assert bear not in rick.battlefield, (
            "a spell whose entry vanished without a counter mark must "
            "never resolve (CR 608)")
        assert bear in rick.graveyard
