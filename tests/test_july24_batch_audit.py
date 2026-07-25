"""July 24, 2026 audit of the game_15299* batch (152 games, the slice-3c gate
batch — [EVENT-PARITY-DIES] came back zero on verified post-3b code).

Findings pinned here:

  #1  Necropotence's delayed RETURN named the face-down card in Discord —
      "⏰ Necropotence: 📦 **Toxic Deluge** → hand" leaked hidden information
      to the opponent (game_1529988281859571782). The activation-time message
      correctly hid the name (July 23 #7), but the scheduled move_card fired
      through the generic display. move_card now honors a hide_card_name
      flag; the Necropotence branch in mtg/engine.py sets it. The console
      log_event keeps the true name for audits.
  #2  (reviewer L1) Gonti, Lord of Luxury's face-down exile named the stolen
      card too — "📦 **Cathars' Crusade** → exile" — same class. The Gonti
      template now sets hide_card_name on the exile AND the library-bottom
      moves (neither player learns which of the four went where).
  #3  (reviewer L1, CRITICAL) The delegated SBA checker read PRINTED
      indestructible: build_sba_state built has_indestructible via
      has_keyword('Indestructible') with no game=, skipping the Humility
      remove-all-abilities deferral — Athreos (printed Indestructible,
      1/1 under Humility) survived exactly-lethal combat damage for ~30
      turns (game_1529985293946585150, CR 613.6 / 704.5g). Fixed by
      threading game= there and at the 10 sibling indestructible-check
      sites (mtg/sba.py inline fallback, mtg/actions.py destroy handlers,
      rules/spell_resolver.py, rules/sba_adapter.py dies-conversion).
  #4  (reviewer S2, CRITICAL x3 — the stack trio, game_1529988360263827656)
      (a) the LIFO extension cap resolved buried entries anyway, so
      Smothering Tithe resolved BENEATH the An Offer You Can't Refuse
      targeting it (4/4 cap-hits were CR 608 violations); the cap-hit
      branch now forces the stack above to act first (_force_stack_above)
      and resolve-anyway survives only as the true-deadlock last resort.
      (b) counter_unless_pays acted on game.stack[-1] unconditionally —
      a stale Spell Pierce extracted a {2} payment from a spell with no
      unless-clause; it now honors the declared target and fizzles per
      CR 608.2b when that target left the stack.
      (c) counter_ability's no-ability fallback countered SPELLS for all
      five Stifle-family cards; it now gates on the source's own oracle
      (Stifle/Trickbind fizzle, Tale's End requires a legendary spell,
      Voidslime/Disallow unchanged), and _validate_cast rejects
      ability-only counters when no ability is on the stack (CR 601.2c).
"""
import pytest


# --------------------------------------------------------------------------- #
# #1 — face-down exile returns must not name the card in player-facing text
# --------------------------------------------------------------------------- #
class TestFaceDownReturnHidesName:
    def test_move_card_hide_card_name_redacts_display(
            self, rules, make_game, make_card):
        game = make_game()
        rick = game.players[0]
        card = make_card("Toxic Deluge")
        rick.exile.append(card)
        msg = rules._execute_action_on_state(game, {
            "action": "move_card", "card": "Toxic Deluge",
            "from_zone": "exile", "to_zone": "hand",
            "player": "Rick", "hide_card_name": True})
        assert card in rick.hand
        assert "Toxic Deluge" not in msg, (
            "face-down return must not name the card in player-facing text")
        assert "face-down" in msg

    def test_move_card_without_flag_still_names_the_card(
            self, rules, make_game, make_card):
        # The redaction is opt-in — ordinary zone changes keep the name.
        game = make_game()
        rick = game.players[0]
        card = make_card("Mulldrifter")
        rick.exile.append(card)
        msg = rules._execute_action_on_state(game, {
            "action": "move_card", "card": "Mulldrifter",
            "from_zone": "exile", "to_zone": "hand", "player": "Rick"})
        assert "Mulldrifter" in msg

    def test_necropotence_delayed_return_is_redacted_end_to_end(
            self, make_game, make_card):
        # The exact shape the Necropotence branch schedules (mtg/engine.py):
        # fire it through _process_delayed_triggers and assert the ⏰ line
        # never names the card.
        from mtg.engine import GameEngine
        engine = GameEngine(None)
        game = make_game()
        rick = game.players[0]
        card = make_card("Hidden Card")
        rick.exile.append(card)
        engine.rules._execute_action_on_state(game, {
            "action": "schedule_delayed_trigger", "trigger_at": "end_step",
            "turn_delay": 0, "phase_of": "Rick", "source": "Necropotence",
            "actions": [{"action": "move_card", "card": "Hidden Card",
                         "from_zone": "exile", "to_zone": "hand",
                         "hide_card_name": True, "player": "Rick"}]})
        game.active_player_index = game.players.index(rick)
        msgs = engine._process_delayed_triggers(game, "end_step")
        assert card in rick.hand
        joined = "\n".join(msgs)
        assert "Hidden Card" not in joined, (
            "the delayed return leaked the face-down card's name")
        assert "Necropotence" in joined

    def test_gonti_template_hides_every_card_name(self, lib, make_game, make_card):
        # Gonti's ETB: every move_card it emits (the face-down exile AND the
        # bottomed cards) must carry hide_card_name — only Gonti's controller
        # may learn which of the four cards went where.
        game = make_game()
        rick, claude = game.players
        claude.library = [make_card(n, type_line="Sorcery", cmc=i + 2)
                          for i, n in enumerate(
                              ["Alpha", "Beta", "Gamma", "Delta", "Epsilon"])]
        from rules.effect_templates import build_game_context
        ctx = build_game_context(game, rick, claude)
        actions, _ = lib.resolve_etb(
            "Gonti, Lord of Luxury",
            "When Gonti, Lord of Luxury enters the battlefield, look at the "
            "top four cards of target opponent's library, exile one of them "
            "face down, then put the rest on the bottom of that library in a "
            "random order.",
            "Rick", "Claude", ctx)
        moves = [a for a in actions if a.get("action") == "move_card"]
        assert moves, "Gonti template produced no zone moves"
        for a in moves:
            assert a.get("hide_card_name") is True, (
                f"Gonti move leaks a hidden card name: {a}")

    def test_engine_source_passes_hide_flag(self):
        # Structural: the Necropotence branch must schedule its move_card
        # with hide_card_name — losing the flag silently reverts the leak.
        import inspect, mtg.engine
        src = inspect.getsource(mtg.engine)
        anchor = src.index("exile the top card of your library")
        window = src[anchor:anchor + 2000]
        assert '"hide_card_name": True' in window, (
            "the Necropotence delayed-return action lost its "
            "hide_card_name flag")


# --------------------------------------------------------------------------- #
# #3 — Humility strips printed Indestructible in the delegated SBA path
# --------------------------------------------------------------------------- #
HUMILITY_ORACLE = ("All creatures lose all abilities and have base power "
                   "and toughness 1/1.")


class TestHumilityStripsIndestructibleInSBA:
    def _god_under_humility(self, game, make_card):
        rick, claude = game.players
        humility = make_card("Humility", type_line="Enchantment",
                             power=None, toughness=None,
                             oracle_text=HUMILITY_ORACLE)
        god = make_card("Athreos, God of Passage",
                        type_line="Legendary Enchantment Creature — God",
                        power="5", toughness="4",
                        keywords=["Indestructible"])
        claude.battlefield.append(humility)
        rick.battlefield.append(god)
        game.register_static_pt_effects(humility, "Claude")
        game.recalculate_power_toughness()
        return god

    def test_sba_state_sees_stripped_indestructible(self, game, make_card):
        god = self._god_under_humility(game, make_card)
        from rules.sba_adapter import build_sba_state
        state = build_sba_state(game)
        perm = state.battlefield[god.id]
        assert perm.has_indestructible is False, (
            "delegated SBA state read PRINTED indestructible under Humility")

    def test_lethal_damage_kills_the_god_under_humility(
            self, rules, game, make_card):
        god = self._god_under_humility(game, make_card)
        rick = game.players[0]
        god.damage_marked = 1          # lethal against Humility's 1/1 base
        rules.process_state_based_actions(game)
        assert god not in rick.battlefield, (
            "1 damage at effective 1/1 under Humility must destroy it "
            "(CR 704.5g — Humility stripped Indestructible)")

    def test_indestructible_still_respected_without_humility(
            self, rules, game, make_card):
        rick = game.players[0]
        god = make_card("Athreos, God of Passage",
                        type_line="Legendary Enchantment Creature — God",
                        power="5", toughness="4",
                        keywords=["Indestructible"])
        rick.battlefield.append(god)
        game.recalculate_power_toughness()
        god.damage_marked = 99
        rules.process_state_based_actions(game)
        assert god in rick.battlefield, (
            "printed Indestructible must still save it with no Humility")


# --------------------------------------------------------------------------- #
# #4b — counter_unless_pays honors the DECLARED target (CR 608.2b)
# --------------------------------------------------------------------------- #
def _spell_entry(make_card, name, controller="Rick", idx=0, **card_kw):
    import asyncio
    from mtg.models import StackEntry
    card = make_card(name, **card_kw)
    return StackEntry(card=card, controller_name=controller,
                      controller_index=idx, is_spell=True,
                      resolution_event=asyncio.Event())


class TestCounterUnlessPaysDeclaredTarget:
    def test_honors_declared_target_not_stack_top(
            self, rules, make_game, make_card):
        game = make_game()
        declared = _spell_entry(make_card, "Smothering Tithe",
                                controller="Rick", type_line="Enchantment")
        unrelated = _spell_entry(make_card, "An Offer You Can't Refuse",
                                 controller="Claude", idx=1,
                                 type_line="Instant")
        game.stack.extend([declared, unrelated])   # unrelated is on top
        msg = rules._execute_action_on_state(game, {
            "action": "counter_unless_pays", "player": "Claude",
            "cost": "{2}", "target_name": "Smothering Tithe"})
        assert declared.countered, "declared target must be the one countered"
        assert not unrelated.countered, (
            "stack-top spell must be untouched when a target was declared")
        assert "Smothering Tithe" in msg

    def test_fizzles_when_declared_target_left_the_stack(
            self, rules, make_game, make_card):
        game = make_game()
        bystander = _spell_entry(make_card, "An Offer You Can't Refuse",
                                 controller="Claude", idx=1,
                                 type_line="Instant")
        game.stack.append(bystander)
        msg = rules._execute_action_on_state(game, {
            "action": "counter_unless_pays", "player": "Claude",
            "cost": "{2}", "target_name": "Ephemerate"})
        assert "no longer on the stack" in msg, (
            "CR 608.2b — a counter whose declared target is gone fizzles")
        assert not bystander.countered, (
            "the stale counter must not grab whatever is on top")


# --------------------------------------------------------------------------- #
# #4c — counter_ability's spell fallback is gated on the source's oracle
# --------------------------------------------------------------------------- #
STIFLE_ORACLE = "Counter target activated or triggered ability."
TALES_END_ORACLE = ("Counter target activated ability, triggered ability, "
                    "or legendary spell.")
DISALLOW_ORACLE = ("Counter target spell, activated ability, or "
                   "triggered ability.")


class TestCounterAbilitySpellFallbackGating:
    def _spell_on_stack(self, game, make_card, name="Scroll Rack",
                        type_line="Artifact"):
        entry = _spell_entry(make_card, name, type_line=type_line)
        game.stack.append(entry)
        return entry

    def test_stifle_cannot_counter_a_spell(self, rules, make_game, make_card):
        game = make_game()
        spell = self._spell_on_stack(game, make_card)
        msg = rules._execute_action_on_state(game, {
            "action": "counter_ability", "player": "Claude",
            "_source_oracle": STIFLE_ORACLE})
        assert not spell.countered, (
            "Stifle's text has no spell clause — it may not counter a spell")
        assert "fizzles" in msg.lower()

    def test_tales_end_requires_a_legendary_spell(
            self, rules, make_game, make_card):
        game = make_game()
        spell = self._spell_on_stack(game, make_card,
                                     name="Agent of Treachery",
                                     type_line="Creature — Human Rogue")
        msg = rules._execute_action_on_state(game, {
            "action": "counter_ability", "player": "Claude",
            "_source_oracle": TALES_END_ORACLE})
        assert not spell.countered, (
            "Tale's End may only counter LEGENDARY spells")
        assert "legendary" in msg.lower()

    def test_tales_end_counters_a_legendary_spell(
            self, rules, make_game, make_card):
        game = make_game()
        spell = self._spell_on_stack(
            game, make_card, name="Korvold, Fae-Cursed King",
            type_line="Legendary Creature — Dragon Noble")
        rules._execute_action_on_state(game, {
            "action": "counter_ability", "player": "Claude",
            "_source_oracle": TALES_END_ORACLE})
        assert spell.countered

    def test_disallow_still_counters_any_spell(
            self, rules, make_game, make_card):
        game = make_game()
        spell = self._spell_on_stack(game, make_card)
        rules._execute_action_on_state(game, {
            "action": "counter_ability", "player": "Claude",
            "_source_oracle": DISALLOW_ORACLE})
        assert spell.countered


# --------------------------------------------------------------------------- #
# #4c cast gate — ability-only counters need an ability on the stack
# --------------------------------------------------------------------------- #
class TestAbilityOnlyCounterCastGate:
    def _stifle(self, make_card):
        return make_card("Stifle", type_line="Instant",
                         oracle_text=STIFLE_ORACLE, mana_cost="{U}",
                         cmc=1, power="0", toughness="0")

    def test_rejected_with_only_spells_on_stack(self, make_game, make_card):
        import asyncio as _a
        from mtg.engine import GameEngine
        from mtg.spells import cast_spell_async
        engine = GameEngine(None)
        game = make_game()
        rick = game.players[0]
        rick.battlefield.append(make_card(
            "Island", type_line="Basic Land — Island", power="0", toughness="0"))
        stifle = self._stifle(make_card)
        rick.hand.append(stifle)
        game.stack.append(_spell_entry(make_card, "Scroll Rack",
                                       controller="Claude", idx=1,
                                       type_line="Artifact"))
        ok, msg, _ = _a.run(cast_spell_async(engine, game, rick, stifle))
        assert ok is False
        assert "activated or triggered ability" in msg

    def test_allowed_with_an_ability_on_stack(self, make_game, make_card):
        import asyncio as _a
        from mtg.models import StackEntry
        from mtg.engine import GameEngine
        from mtg.spells import cast_spell_async
        engine = GameEngine(None)
        game = make_game()
        rick = game.players[0]
        rick.battlefield.append(make_card(
            "Island", type_line="Basic Land — Island", power="0", toughness="0"))
        stifle = self._stifle(make_card)
        rick.hand.append(stifle)
        trig = StackEntry(card=None, controller_name="Claude",
                          controller_index=1, is_spell=False,
                          trigger_source="Talrand, Sky Summoner",
                          trigger_text="create a 2/2 blue Drake creature "
                                       "token with flying.")
        game.stack.append(trig)
        ok, msg, _ = _a.run(cast_spell_async(engine, game, rick, stifle))
        assert ok is True, f"Stifle must cast with an ability on the stack: {msg}"


# --------------------------------------------------------------------------- #
# #4a — the LIFO cap-hit branch acts on the stack ABOVE instead of
#        resolving the buried entry out of order (CR 608)
# --------------------------------------------------------------------------- #
class TestLifoCapRescue:
    def test_force_stack_above_removes_stalled_trigger(
            self, make_game, make_card):
        from mtg.engine import GameEngine
        from mtg.spells import _force_stack_above
        engine = GameEngine(None)
        game = make_game()
        ours = _spell_entry(make_card, "Smothering Tithe",
                            type_line="Enchantment")
        from mtg.models import StackEntry
        trig = StackEntry(card=None, controller_name="Claude",
                          controller_index=1, is_spell=False,
                          trigger_source="Talrand, Sky Summoner",
                          trigger_text="create a 2/2 blue Drake creature "
                                       "token with flying.")
        game.stack.extend([ours, trig])
        msgs = []
        acted = _force_stack_above(engine, game, ours, msgs)
        assert acted
        assert trig not in game.stack, "stalled trigger must be cleared"
        assert game.stack[-1] is ours, "our entry must now be top"

    def test_force_stack_above_wakes_topmost_buried_spell(
            self, make_game, make_card):
        from mtg.engine import GameEngine
        from mtg.spells import _force_stack_above
        engine = GameEngine(None)
        game = make_game()
        ours = _spell_entry(make_card, "Smothering Tithe",
                            type_line="Enchantment")
        counter = _spell_entry(make_card, "An Offer You Can't Refuse",
                               controller="Claude", idx=1,
                               type_line="Instant")
        game.stack.extend([ours, counter])
        acted = _force_stack_above(engine, game, ours, [])
        assert acted
        assert counter.resolution_event.is_set(), (
            "the buried-above spell's coroutine must be woken so IT "
            "resolves (it may counter us) — never resolve beneath it")
        assert not ours.resolution_event.is_set()

    def test_cap_hit_branch_no_longer_resolves_anyway_unconditionally(self):
        # Structural: the cap-hit else-branch must route through the rescue
        # before any resolve-anyway fallback.
        import inspect
        import mtg.spells
        src = inspect.getsource(mtg.spells)
        anchor = src.index("LIFO extension cap")
        window = src[anchor:anchor + 2500]
        assert "_force_stack_above" in window, (
            "the LIFO cap-hit branch lost its CR 608 rescue")


# --------------------------------------------------------------------------- #
# #5 — (reviewer S2, CRITICAL) generic cast-trigger tokens dropped "with
#      flying" — every Talrand Drake was a vanilla 2/2 (CR 702.9b)
# --------------------------------------------------------------------------- #
class TestCastTriggerTokenKeywords:
    def test_talrand_drake_has_flying(self, make_game, make_card):
        import asyncio as _a
        from mtg.engine import GameEngine
        from mtg.triggers import _check_cast_triggers
        engine = GameEngine(None)
        game = make_game()
        claude = game.players[1]
        talrand = make_card(
            "Talrand, Sky Summoner",
            type_line="Legendary Creature — Merfolk Wizard",
            power="2", toughness="2",
            oracle_text="Whenever you cast an instant or sorcery spell, "
                        "create a 2/2 blue Drake creature token with flying.")
        claude.battlefield.append(talrand)
        spell = make_card("Counterspell", type_line="Instant",
                          oracle_text="Counter target spell.",
                          power="0", toughness="0")
        _a.run(_check_cast_triggers(engine, game, claude, spell))
        drakes = [c for c in claude.battlefield
                  if getattr(c, 'is_token', False) and 'Drake' in c.name]
        assert drakes, "Talrand's cast trigger produced no Drake"
        assert drakes[0].has_keyword('Flying'), (
            "the Drake lost its printed 'with flying' clause")

    def test_multi_keyword_clause_is_forwarded(self, make_game, make_card):
        import asyncio as _a
        from mtg.engine import GameEngine
        from mtg.triggers import _check_cast_triggers
        engine = GameEngine(None)
        game = make_game()
        claude = game.players[1]
        src_card = make_card(
            "Test Summoner",
            type_line="Creature — Wizard", power="1", toughness="1",
            oracle_text="Whenever you cast an instant or sorcery spell, "
                        "create a 3/3 green Beast creature token with "
                        "vigilance, trample and haste.")
        claude.battlefield.append(src_card)
        spell = make_card("Shock", type_line="Instant",
                          oracle_text="Shock deals 2 damage to any target.",
                          power="0", toughness="0")
        _a.run(_check_cast_triggers(engine, game, claude, spell))
        beasts = [c for c in claude.battlefield
                  if getattr(c, 'is_token', False) and 'Beast' in c.name]
        assert beasts, "cast trigger produced no Beast"
        for kw in ("Vigilance", "Trample", "Haste"):
            assert beasts[0].has_keyword(kw), f"token lost {kw}"


# --------------------------------------------------------------------------- #
# #6 — (reviewer D1, CRITICAL) Puppeteer Clique: reanimate from the
#      OPPONENT's graveyard, not exile the caster's own creature
# --------------------------------------------------------------------------- #
CLIQUE_ORACLE = ("Flying\nWhen this creature enters, put target creature "
                 "card from an opponent's graveyard onto the battlefield "
                 "under your control. It gains haste. At the beginning of "
                 "your next end step, exile it.\nPersist")


class TestPuppeteerClique:
    def test_template_reanimates_from_opponent_graveyard(
            self, lib, make_game, make_card):
        from rules.effect_templates import build_game_context
        game = make_game()
        rick, claude = game.players
        claude.graveyard.append(make_card(
            "Woodfall Primus", type_line="Creature — Treefolk Shaman",
            power="6", toughness="6", cmc=8))
        ctx = build_game_context(game, rick, claude)
        actions, _ = lib.resolve_etb(
            "Puppeteer Clique", CLIQUE_ORACLE, "Rick", "Claude", ctx)
        kinds = [a["action"] for a in actions]
        assert "reanimate" in kinds, f"expected a reanimate action, got {kinds}"
        rea = next(a for a in actions if a["action"] == "reanimate")
        assert rea["from_player"] == "Claude", "must take from the OPPONENT's graveyard"
        assert rea["haste"] is True
        assert "exile" not in kinds, (
            "the old Tier-2 misparse exiled the caster's own creature")
        sched = next(a for a in actions
                     if a["action"] == "schedule_delayed_trigger")
        assert sched["trigger_at"] == "end_step"
        assert sched["phase_of"] == "Rick", "'your next end step' is owner-gated"

    def test_template_noop_with_empty_opponent_graveyard(
            self, lib, make_game):
        from rules.effect_templates import build_game_context
        game = make_game()
        rick, claude = game.players
        ctx = build_game_context(game, rick, claude)
        actions, _ = lib.resolve_etb(
            "Puppeteer Clique", CLIQUE_ORACLE, "Rick", "Claude", ctx)
        assert actions[0]["action"] == "no_action"

    def test_reanimate_haste_flag_enters_unsick(
            self, rules, make_game, make_card):
        game = make_game()
        rick, claude = game.players
        primus = make_card("Woodfall Primus",
                           type_line="Creature — Treefolk Shaman",
                           power="6", toughness="6", cmc=8)
        claude.graveyard.append(primus)
        rules._execute_action_on_state(game, {
            "action": "reanimate", "player": "Rick",
            "card": "Woodfall Primus", "from_player": "Claude",
            "haste": True})
        assert primus in rick.battlefield
        assert primus not in claude.graveyard
        assert primus.summoning_sick is False, "'It gains haste' — may attack"


# --------------------------------------------------------------------------- #
# #7 — (reviewer D1, CRITICAL) type-restricted graveyard returns resolve
#      deterministically — Tier 3 hallucinated Balan (Cat Knight) for
#      Bruna's "return target Angel or Human creature card"
# --------------------------------------------------------------------------- #
class TestTypeRestrictedGraveyardReturn:
    BRUNA_TRIGGER = ("When you cast this spell, you may return target Angel "
                     "or Human creature card from your graveyard to the "
                     "battlefield.")

    def test_guard_picks_a_legal_type_only(self, rules, make_game, make_card):
        import asyncio as _a
        game = make_game()
        claude = game.players[1]
        claude.graveyard.extend([
            make_card("Balan, Wandering Knight",
                      type_line="Legendary Creature — Cat Knight",
                      power="3", toughness="3", cmc=4),
            make_card("Danitha Capashen, Paragon",
                      type_line="Legendary Creature — Human Knight",
                      power="2", toughness="2", cmc=3),
        ])
        msgs, actions = _a.run(rules.resolve_effect(
            game, self.BRUNA_TRIGGER,
            source_card="Bruna, the Fading Light", controller="Claude"))
        assert actions, "guard should produce a deterministic action"
        rea = actions[0]
        assert rea["action"] == "reanimate"
        assert rea["card"] == "Danitha Capashen, Paragon", (
            "must pick the legal Human, never the Cat Knight (CR 601.2c)")
        assert rea["own_graveyard"] is True

    def test_guard_declines_with_no_legal_target(
            self, rules, make_game, make_card):
        import asyncio as _a
        game = make_game()
        claude = game.players[1]
        claude.graveyard.append(make_card(
            "Balan, Wandering Knight",
            type_line="Legendary Creature — Cat Knight",
            power="3", toughness="3", cmc=4))
        msgs, actions = _a.run(rules.resolve_effect(
            game, self.BRUNA_TRIGGER,
            source_card="Bruna, the Fading Light", controller="Claude"))
        assert not actions, "no legal Angel/Human — must decline, not invent"
        assert msgs and "no angel or human" in msgs[0].lower()


# --------------------------------------------------------------------------- #
# #8 — (reviewer D1, CRITICAL) "destroy target NONcreature permanent" must
#      never pick a creature ('creature' is a substring of 'noncreature')
# --------------------------------------------------------------------------- #
class TestNoncreatureDestroyRestriction:
    def _resolve(self, lib, game, oracle):
        from rules.effect_templates import build_game_context
        rick, claude = game.players
        ctx = build_game_context(game, rick, claude)
        return lib.resolve_etb("Test Smasher", oracle, "Rick", "Claude", ctx)

    def test_noncreature_restriction_skips_creatures(
            self, lib, make_game, make_card):
        game = make_game()
        claude = game.players[1]
        claude.battlefield.extend([
            make_card("Doomed Traveler", type_line="Creature — Human Soldier",
                      power="1", toughness="1"),
            make_card("Sigarda's Aid", type_line="Enchantment",
                      power=None, toughness=None),
        ])
        actions, _ = self._resolve(
            lib, game,
            "When this creature enters, destroy target noncreature permanent.")
        destroys = [a for a in actions if a.get("action") == "destroy"]
        assert destroys, f"expected a destroy action, got {actions}"
        assert destroys[0]["card"] != "Doomed Traveler", (
            "'noncreature permanent' destroyed a CREATURE (substring bug)")

    def test_plain_creature_restriction_still_picks_creatures(
            self, lib, make_game, make_card):
        game = make_game()
        claude = game.players[1]
        claude.battlefield.append(make_card(
            "Doomed Traveler", type_line="Creature — Human Soldier",
            power="1", toughness="1"))
        actions, _ = self._resolve(
            lib, game,
            "When this creature enters, destroy target creature.")
        destroys = [a for a in actions if a.get("action") == "destroy"]
        assert destroys and destroys[0]["card"] == "Doomed Traveler"


# --------------------------------------------------------------------------- #
# #9 — (reviewer D1, MAJOR) Fleshbag-class edicts: the CONTROLLER's own
#      mandatory sacrifice must not be skipped when the opponent also
#      sacrificed (CR 701.20)
# --------------------------------------------------------------------------- #
class TestYawgmothSacrificeAnotherCost:
    # (reviewer A1, CRITICAL) "Pay 1 life, Sacrifice ANOTHER creature:" —
    # the AI activation path matched neither 'sacrifice a creature' nor
    # 'sacrifice a permanent', so the sacrifice cost silently evaporated
    # while the life cost still charged (game_1529979552258855062; the
    # manual !activate path in mtg/cog.py already had the phrase — the
    # documented two-paths divergence).
    def test_satisfies_cost_rejects_the_source_itself(self, make_card, game):
        from mtg.engine import _satisfies_sacrifice_cost
        yawg = make_card("Yawgmoth, Thran Physician",
                         type_line="Legendary Creature — Human Cleric",
                         power="2", toughness="4")
        other = make_card("Bloodghast", type_line="Creature — Vampire Spirit",
                          power="2", toughness="1")
        cost = "pay 1 life, sacrifice another creature"
        assert _satisfies_sacrifice_cost(other, cost, game, source=yawg)
        assert not _satisfies_sacrifice_cost(yawg, cost, game, source=yawg), (
            "'sacrifice ANOTHER creature' — the source itself can't pay")

    def test_engine_branch_matches_the_another_phrasing(self):
        import inspect, mtg.engine
        src = inspect.getsource(mtg.engine)
        anchor = src.index("sacrifice a permanent' in cost_lower")
        window = src[max(0, anchor - 800):anchor]
        assert "sacrifice another creature" in window, (
            "the AI activation cost branch lost the 'sacrifice another "
            "creature' phrasing (Yawgmoth-class costs go free again)")


class TestMayhemDevilAnyPlayerSacrifice:
    # (reviewer A1, MAJOR) "Whenever a PLAYER sacrifices a permanent" never
    # fired — the scan covered only the sacrificer's battlefield and only
    # the "you sacrifice" phrasings.
    DEVIL_ORACLE = ("Whenever a player sacrifices a permanent, this creature "
                    "deals 1 damage to any target.")

    def _devil(self, make_card):
        return make_card("Mayhem Devil", type_line="Creature — Devil",
                         power="3", toughness="3",
                         oracle_text=self.DEVIL_ORACLE)

    def test_fires_on_controllers_own_sacrifice(
            self, rules, make_game, make_card):
        from mtg.actions import _fire_sacrifice_triggers
        game = make_game()
        rick, claude = game.players
        rick.battlefield.append(self._devil(make_card))
        sacked = make_card("Eldrazi Spawn", type_line="Creature — Eldrazi",
                           power="0", toughness="1")
        life_before = claude.life
        msgs = _fire_sacrifice_triggers(rules, game, rick, sacked)
        assert claude.life == life_before - 1, (
            f"Devil should ping on its controller's sacrifice: {msgs}")

    def test_fires_on_opponents_sacrifice_too(
            self, rules, make_game, make_card):
        from mtg.actions import _fire_sacrifice_triggers
        game = make_game()
        rick, claude = game.players
        claude.battlefield.append(self._devil(make_card))   # opponent's Devil
        sacked = make_card("Eldrazi Spawn", type_line="Creature — Eldrazi",
                           power="0", toughness="1")
        life_before = rick.life
        msgs = _fire_sacrifice_triggers(rules, game, rick, sacked)
        assert rick.life == life_before - 1, (
            f"an OPPONENT's Devil must see this sacrifice ('a player "
            f"sacrifices') and ping for ITS controller: {msgs}")


class TestCollapseBurstNamesDistinctSacrifices:
    # (reviewer A1, MAJOR) "💀 Grave Pact: Claude sacrifices Mother of Runes
    # (×2 fires)" made Kor Soldier's sacrifice invisible — distinct objects
    # are now enumerated; cumulative-value runs keep the last-message form.
    def test_distinct_sacrifices_are_enumerated(self):
        from mtg.triggers import collapse_trigger_burst
        out = collapse_trigger_burst([
            "💀 Grave Pact: 💀 Claude sacrifices Kor Soldier",
            "💀 Grave Pact: 💀 Claude sacrifices Mother of Runes",
        ])
        assert len(out) == 1
        assert "Kor Soldier" in out[0], f"hidden sacrifice: {out[0]}"
        assert "Mother of Runes" in out[0]
        assert "×2" in out[0]

    def test_cumulative_value_runs_keep_last_message(self):
        from mtg.triggers import collapse_trigger_burst
        out = collapse_trigger_burst([
            "💀 Syr Konrad: deals 1 damage to Claude (29 life)",
            "💀 Syr Konrad: deals 1 damage to Claude (28 life)",
            "💀 Syr Konrad: deals 1 damage to Claude (27 life)",
        ])
        assert len(out) == 1
        assert "(27 life)" in out[0]
        assert "×3" in out[0]


class TestDetrimentalAuraPrefersCreatures:
    # (reviewer D1, MAJOR) Faith's Fetters auto-targeted a basic Swamp while
    # the two creatures dealing lethal every turn were legal targets in the
    # same list.
    def test_enchant_permanent_lockdown_picks_the_creature(
            self, make_game, make_card):
        import asyncio as _a
        from mtg.engine import GameEngine
        from mtg.spells import cast_spell_async
        engine = GameEngine(None)
        game = make_game()
        rick, claude = game.players
        rick.battlefield.extend(
            make_card(f"Plains {i}", type_line="Basic Land — Plains",
                      power="0", toughness="0") for i in range(4))
        claude.battlefield.extend([
            make_card("Swamp", type_line="Basic Land — Swamp",
                      power="0", toughness="0"),
            make_card("Young Wolf", type_line="Creature — Wolf",
                      power="1", toughness="1"),
        ])
        fetters = make_card(
            "Faith's Fetters", type_line="Enchantment — Aura",
            oracle_text="Enchant permanent\nWhen this enchantment enters, "
                        "you gain 4 life.\nEnchanted permanent can't attack "
                        "or block, and its activated abilities can't be "
                        "activated unless they're mana abilities.",
            mana_cost="{3}{W}", cmc=4, power=None, toughness=None)
        rick.hand.append(fetters)
        ok, msg, _ = _a.run(cast_spell_async(engine, game, rick, fetters))
        assert ok, msg
        wolf = next(c for c in claude.battlefield if c.name == "Young Wolf")
        assert fetters.attached_to == wolf.id, (
            "detrimental aura must prefer the creature over a basic land")


class TestPermanentEtbRebindKeepsPriorMessages:
    # (reviewer A1 #5, root-caused post-wave) Sram's cast-trigger draw
    # message silently vanished when the CAST SPELL was a permanent with
    # self-ETB text: the permanent branch's Tier-1 rebind
    # (effect_messages = resolve_special_effects(...)) discarded everything
    # already appended — the draw HAPPENED (state correct), only the display
    # was lost, and the aura's own "✨ enchants" line went with it. The
    # "intermittency" was deterministic: All That Glitters / the Swords have
    # no ETB paragraph, so they never hit the rebind. The instant/sorcery
    # branch has had its own save/restore since May; this pins the
    # permanent-branch twin.
    def test_sram_draw_message_survives_permanent_etb_rebind(
            self, make_game, make_card):
        import asyncio as _a
        from mtg.engine import GameEngine
        from mtg.spells import cast_spell_async
        engine = GameEngine(None)
        game = make_game()
        claude = game.players[1]
        game.active_player_index = 1
        sram = make_card(
            "Sram, Senior Edificer",
            type_line="Legendary Creature — Dwarf Advisor",
            power="2", toughness="2",
            oracle_text="Whenever you cast an Aura, Equipment, or Vehicle "
                        "spell, draw a card.")
        claude.battlefield.append(sram)
        claude.battlefield.extend(
            make_card(f"Plains {i}", type_line="Basic Land — Plains",
                      power="0", toughness="0") for i in range(5))
        claude.library = [make_card(f"Lib {i}", type_line="Sorcery")
                          for i in range(10)]
        mantle = make_card(
            "Mantle of the Ancients", type_line="Enchantment — Aura",
            oracle_text="Enchant creature you control\nWhen Mantle of the "
                        "Ancients enters the battlefield, return any number "
                        "of Aura and/or Equipment cards from your graveyard "
                        "to the battlefield attached to enchanted creature.\n"
                        "Enchanted creature gets +1/+1 for each Aura and "
                        "Equipment attached to it.",
            mana_cost="{3}{W}{W}", cmc=5, power=None, toughness=None)
        claude.hand.append(mantle)
        hand_before = len(claude.hand)
        ok, msg, effs = _a.run(cast_spell_async(
            engine, game, claude, mantle, target=sram))
        assert ok, msg
        joined = "\n".join(effs)
        assert "draws a card" in joined, (
            f"the cast-trigger draw message was discarded by the ETB "
            f"rebind: {effs}")
        assert "enchants" in joined, (
            f"the aura attach line was discarded by the ETB rebind: {effs}")
        # State was always correct — the draw happened (−1 Mantle, +1 draw).
        assert len(claude.hand) == hand_before


class TestEdictSourcePrefix:
    # (reviewer A1 #7, root-caused) The Butcher of Malakir template's edict
    # lines had no source prefix — "💀 Claude sacrifices X" sat unattributed
    # between prefixed Grave Pact/Dictate neighbors. sacrifice_permanent now
    # honors an opt-in source field; the template threads the trigger source.
    def test_sacrifice_with_source_is_attributed(
            self, rules, make_game, make_card):
        game = make_game()
        claude = game.players[1]
        claude.battlefield.append(make_card(
            "Kor Soldier", type_line="Creature — Kor Soldier",
            power="1", toughness="1"))
        msg = rules._execute_action_on_state(game, {
            "action": "sacrifice_permanent", "player": "Claude",
            "type_filter": "creature", "source": "Butcher of Malakir"})
        assert msg.startswith("💀 Butcher of Malakir: "), (
            f"edict sacrifice lost its source attribution: {msg}")

    def test_sacrifice_without_source_keeps_plain_shape(
            self, rules, make_game, make_card):
        # Paths that wrap their own prefix (hardcoded Grave Pact/Dictate)
        # don't set source — no double prefix.
        game = make_game()
        claude = game.players[1]
        claude.battlefield.append(make_card(
            "Kor Soldier", type_line="Creature — Kor Soldier",
            power="1", toughness="1"))
        msg = rules._execute_action_on_state(game, {
            "action": "sacrifice_permanent", "player": "Claude",
            "type_filter": "creature"})
        assert msg.startswith("💀 Claude sacrifices")

    def test_butcher_template_threads_its_source(self, lib, make_game, make_card):
        from rules.effect_templates import build_game_context
        game = make_game()
        rick, claude = game.players
        claude.battlefield.append(make_card(
            "Kor Soldier", type_line="Creature — Kor Soldier",
            power="1", toughness="1"))
        butcher = make_card(
            "Butcher of Malakir", type_line="Creature — Vampire Warrior",
            power="5", toughness="4",
            oracle_text="Flying\nWhenever this creature or another creature "
                        "you control dies, each opponent sacrifices a creature.")
        ctx = build_game_context(game, rick, claude, card=butcher)
        ctx['_source_card_name'] = "Butcher of Malakir"
        # Route through the generator the dies path uses.
        gen_actions = lib._force_sacrifice_creature("Rick", "Claude", ctx)
        sac = next(a for a in gen_actions
                   if a["action"] == "sacrifice_permanent")
        assert sac.get("source") == "Butcher of Malakir"


class TestPostLossTriggerTail:
    # (reviewer S2 #5) Brago's combat-damage trigger ran a full mass-flicker
    # ~20 lines after PLAYER_LOSES_ZERO_LIFE fired (CR 104.2a). The dispatch
    # in resolve_combat_damage is now gated on game.ended.
    def test_combat_damage_trigger_dispatch_is_gated_on_game_ended(self):
        import inspect
        import mtg.combat
        src = inspect.getsource(mtg.combat)
        anchor = src.index("combat_damage_dealt = getattr(game, '_combat_damage_to_player'")
        window = src[anchor:anchor + 300]
        assert "not game.ended" in window, (
            "the combat-damage trigger dispatch lost its CR 104.2a gate")


class TestSyncCastTriggerBridge:
    # (slice 4b groundwork) Suspend / Etali / free-cast moves are real
    # CR 601 casts that never fired battlefield cast triggers (the sync-gap
    # class). queue_cast_triggers_sync scans both battlefields and queues
    # matching triggers for the async Tier-3 drain, emitting CARD_CAST with
    # a paired parity record so the slice-4a zero gate holds.
    TALRAND_ORACLE = ("Flying\nWhenever you cast an instant or sorcery "
                      "spell, create a 2/2 blue Drake creature token with "
                      "flying.")

    def _setup(self, make_game, make_card):
        from mtg.engine import GameEngine
        engine = GameEngine(None)
        game = make_game()
        rick, claude = game.players
        talrand = make_card("Talrand, Sky Summoner",
                            type_line="Legendary Creature — Merfolk Wizard",
                            power="2", toughness="2",
                            oracle_text=self.TALRAND_ORACLE)
        rick.battlefield.append(talrand)
        return engine, game, rick, claude

    def test_matching_cast_queues_the_trigger_and_pairs_parity(
            self, make_game, make_card):
        from mtg.triggers import queue_cast_triggers_sync, report_cast_parity
        engine, game, rick, claude = self._setup(make_game, make_card)
        bolt = make_card("Rift Bolt", type_line="Sorcery",
                         oracle_text="Rift Bolt deals 3 damage to any target.",
                         power="0", toughness="0")
        queued = queue_cast_triggers_sync(engine, game, rick, bolt,
                                          via="suspend")
        assert queued == 1
        pend = getattr(game, 'pending_async_triggers', [])
        assert any("Talrand" in str(t.get('source_card', ''))
                   or "Talrand" in str(t) for t in pend), (
            f"Talrand's trigger not queued: {pend}")
        assert game._cast_events and game._cast_events[-1][2] == "suspend"
        assert report_cast_parity(game) == [], (
            "sync-bridge casts must be parity-paired (zero gate)")

    def test_nonmatching_cast_type_is_filtered(self, make_game, make_card):
        # Talrand wants instants/sorceries — a suspended CREATURE (Greater
        # Gargadon-class) must not queue his trigger.
        from mtg.triggers import queue_cast_triggers_sync, report_cast_parity
        engine, game, rick, claude = self._setup(make_game, make_card)
        gargadon = make_card("Greater Gargadon",
                             type_line="Creature — Beast",
                             oracle_text="Suspend 10—{R}",
                             power="9", toughness="7")
        queued = queue_cast_triggers_sync(engine, game, rick, gargadon,
                                          via="suspend")
        assert queued == 0
        assert report_cast_parity(game) == []

    def test_opponents_any_player_trigger_sees_the_cast(
            self, make_game, make_card):
        # "Whenever a player casts ..." on the OPPONENT's battlefield fires
        # for this cast; the opponent's "whenever you cast" must not.
        from mtg.triggers import queue_cast_triggers_sync
        from mtg.engine import GameEngine
        engine = GameEngine(None)
        game = make_game()
        rick, claude = game.players
        claude.battlefield.append(make_card(
            "Goblin Spymaster", type_line="Creature — Goblin Rogue",
            power="2", toughness="2",
            oracle_text="Whenever a player casts a spell, this creature "
                        "deals 1 damage to that player."))
        claude.battlefield.append(make_card(
            "Talrand, Sky Summoner",
            type_line="Legendary Creature — Merfolk Wizard",
            power="2", toughness="2",
            oracle_text=self.TALRAND_ORACLE))
        bolt = make_card("Rift Bolt", type_line="Sorcery",
                         oracle_text="Rift Bolt deals 3 damage to any target.",
                         power="0", toughness="0")
        queued = queue_cast_triggers_sync(engine, game, rick, bolt,
                                          via="suspend")
        pend = getattr(game, 'pending_async_triggers', [])
        names = str(pend)
        assert "Talrand" not in names, (
            "an opponent's 'whenever YOU cast' fired for Rick's cast")


class TestEdictControllerSideNotSkipped:
    def test_source_sacrificed_when_it_is_controllers_only_creature(
            self, lib, make_game, make_card):
        from rules.effect_templates import build_game_context
        game = make_game()
        rick, claude = game.players
        fleshbag = make_card("Fleshbag Marauder",
                             type_line="Creature — Zombie Warrior",
                             power="3", toughness="1")
        rick.battlefield.append(fleshbag)          # controller's ONLY creature
        claude.battlefield.append(make_card(
            "Selfless Spirit", type_line="Creature — Spirit Cleric",
            power="2", toughness="1"))
        ctx = build_game_context(game, rick, claude, card=fleshbag)
        ctx['_source_card_name'] = "Fleshbag Marauder"
        actions, _ = lib.resolve_etb(
            "Fleshbag Marauder",
            "When this creature enters, each player sacrifices a creature "
            "of their choice.",
            "Rick", "Claude", ctx)
        destroyed = [a["card"] for a in actions
                     if a.get("action") == "destroy"]
        assert "Selfless Spirit" in destroyed, "opponent's sacrifice missing"
        assert "Fleshbag Marauder" in destroyed, (
            "controller's own mandatory sacrifice was skipped because the "
            "opponent's destroy was already in the action list")
