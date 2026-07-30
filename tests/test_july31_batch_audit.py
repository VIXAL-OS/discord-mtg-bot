"""July 31, 2026 batch-10 audit pins (batch game_15324*, sha=78ef15e).

Inline-sweep findings:
- F-A: the end-of-turn stale-stack sweep destroyed LIVE spells whose
  coroutines were mid-choreography (Mystic Snake / Heroic Intervention /
  Dissipate / Trickbind, all "countered" unresolved). The sweep is now
  live-aware (preserve one boundary, sweep on the second), and the autoplay
  loop drains the stack before end_turn (_await_stack_quiescence).
- F-E: the combat-damage and attack-watcher scans matched trigger phrases
  INSIDE activated-ability lines (Ascendant Spirit's quoted grant; Jaya,
  Fiery Negotiator's loyalty text) — both scans now strip activated lines.
- F-F: the legal-actions provider advertised {"type": "cycle"} entries that
  NEITHER executor dispatched (loaded-gun provider/executor drift).
- Templates for the batch's refused-trigger tail: Predator Ooze, Bloodmad
  Vampire, Underworld Sentinel (linked exile), Yidris cascade grant,
  Soulherder end-step (duplicate-key overwrite killed the old template).
- Deterministic judge guards: fog-shaped activations (Spore Frog paid its
  sacrifice and got nothing) and Aetherize's bounce, both previously eaten
  by the combat-shape guard.

Reviewer findings (oathbreaker mirror / replacement_chain / combat_keywords):
- Ash Zealot's "casts a spell from a graveyard" condition was unchecked —
  fired on every hand cast and decided a game.
- Roiling Vortex's costed "{R}: opponents can't gain life this turn"
  registered as a permanent free replacement (Glen Elendra class in the
  replacement scanner).
- Gisela's damage-doubling was gated on source_controller == controller — a
  house-rule gate her printed text doesn't have (Furnace of Rath class);
  spell damage never doubled.
- Garruk, Primal Hunter -3 floored its draw at 1 off an empty board while
  the correct generator sat dead (live-wrong/dead-right split).
- {X} activation costs paid X=0 while Tier 3 invented its own X
  (CR 118.9 — Pernicious Deed wiped at X≈3 having paid 0).
- Extort was killed by the July 22 parenthetical strip (its whole trigger
  condition is reminder text).
- Ohran Frostfang double-drew on his own connect (hardcoded watcher + the
  July 30 template both firing) — the template is deleted, pinned in
  test_july30_batch_audit.py.
"""
import asyncio
import inspect
import re
import types

import pytest

from mtg.models import StackEntry


def _lib():
    from rules.effect_templates import get_effect_library
    return get_effect_library()


# ---------------------------------------------------------------------------
# F-A: live-aware end-of-turn stack sweep
# ---------------------------------------------------------------------------

class TestLiveStackSweep:
    def _entry(self, make_card, name, event):
        return StackEntry(card=make_card(name), controller_name="Rick",
                          controller_index=0, resolution_event=event)

    def test_live_entry_survives_one_cleanup_then_swept(self, rules, game, make_card):
        from mtg.engine import GameEngine
        engine = rules.engine_ref if getattr(rules, 'engine_ref', None) else None
        # clear_end_of_turn_effects lives on GameEngine; build one directly.
        from mtg.engine import GameEngine as GE
        ge = GE.__new__(GE)  # no __init__ side effects needed for this method
        live = self._entry(make_card, "Heroic Intervention", asyncio.Event())
        game.stack.append(live)
        ge.clear_end_of_turn_effects(game)
        assert live in game.stack, "live entry must survive the first boundary"
        assert live.cleanup_survivals == 1
        ge.clear_end_of_turn_effects(game)
        assert live not in game.stack, "second cleanup sweeps the leak"

    def test_dead_and_resolved_entries_swept_immediately(self, rules, game, make_card):
        from mtg.engine import GameEngine as GE
        ge = GE.__new__(GE)
        dead = self._entry(make_card, "Phantom", None)          # no event at all
        done_ev = asyncio.Event()
        done_ev.set()
        resolved = self._entry(make_card, "Resolved Spell", done_ev)
        game.stack.extend([dead, resolved])
        ge.clear_end_of_turn_effects(game)
        assert game.stack == []

    def test_quiescence_helper_returns_when_stack_drains(self, game, make_card):
        from mtg.autoplay import _await_stack_quiescence
        ev = asyncio.Event()
        ev.set()
        game.stack.append(self._entry(make_card, "Done", ev))
        # Resolved event -> not live -> returns immediately (no timeout burn).
        asyncio.run(_await_stack_quiescence(game, timeout=5.0))

    def test_quiescence_helper_bounded_on_stuck_entry(self, game, make_card):
        from mtg.autoplay import _await_stack_quiescence
        game.stack.append(self._entry(make_card, "Stuck", asyncio.Event()))
        # Live-forever entry -> helper must give up at the timeout, not hang.
        asyncio.run(_await_stack_quiescence(game, timeout=0.3))


# ---------------------------------------------------------------------------
# F-F: provider/executor action-grammar consistency
# ---------------------------------------------------------------------------

class TestProviderExecutorConsistency:
    def test_every_advertised_action_type_is_dispatched_by_both_executors(self):
        import mtg.legal_actions as la
        import mtg.autoplay as ap
        import mtg.engine as eng
        provider_src = inspect.getsource(la)
        emitted = set(re.findall(r'\{"type": "([a-z_]+)"', provider_src))
        assert "cycle" in emitted, "provider stopped advertising cycle?"
        ap_src = inspect.getsource(ap)
        eng_src = inspect.getsource(eng)
        for t in sorted(emitted):
            assert f'action_type == "{t}"' in ap_src, \
                f"autoplay executor cannot dispatch advertised type {t!r}"
            assert f'action_type == "{t}"' in eng_src, \
                f"engine executor cannot dispatch advertised type {t!r}"


# ---------------------------------------------------------------------------
# F-E: activated-ability lines invisible to the combat/attack scans
# ---------------------------------------------------------------------------

ASCENDANT_ORACLE = (
    '{S}{S}: This creature becomes a Spirit Warrior with base power and '
    'toughness 2/3.\n'
    '{S}{S}{S}: If this creature is a Warrior, put a flying counter on it and '
    'it becomes a Spirit Warrior Angel with base power and toughness 4/4.\n'
    '{S}{S}{S}{S}: If this creature is an Angel, put two +1/+1 counters on it '
    'and it gains "Whenever this creature deals combat damage to a player, '
    'draw a card."')

JAYA_ORACLE = (
    '+1: Create a 1/1 red Monk creature token with prowess.\n'
    '−1: Exile the top two cards of your library. Choose one of them. '
    'You may play that card this turn.\n'
    '−2: Choose target creature an opponent controls. Whenever you '
    'attack this turn, Jaya deals damage equal to the number of attacking '
    'creatures to that creature.\n'
    '−8: You get an emblem.')


class TestActivatedLineStrip:
    def test_ascendant_spirit_grant_invisible_to_combat_scan(self):
        from rules.effect_templates import strip_activated_ability_lines
        stripped = strip_activated_ability_lines(ASCENDANT_ORACLE).lower()
        assert 'combat damage to a player' not in stripped

    def test_jaya_loyalty_text_invisible_to_attack_scan(self):
        from rules.effect_templates import strip_activated_ability_lines
        stripped = strip_activated_ability_lines(JAYA_ORACLE).lower()
        assert 'whenever you attack' not in stripped

    def test_real_triggers_survive_the_strip(self):
        from rules.effect_templates import strip_activated_ability_lines
        real = ('Trample\nWhenever Yidris deals combat damage to a player, '
                'as you cast spells from your hand this turn, they gain cascade.')
        assert 'combat damage to a player' in strip_activated_ability_lines(real).lower()

    def test_combat_scan_applies_the_strip(self):
        import mtg.combat as combat
        src = inspect.getsource(combat)
        assert 'strip_activated_ability_lines(attacker.oracle_text)' in src

    def test_attack_watcher_scan_applies_the_strip(self):
        import mtg.triggers as triggers
        src = inspect.getsource(triggers._check_attack_triggers_sync)
        assert 'strip_activated_ability_lines' in src

    def test_queue_extraction_applies_the_strip(self):
        import mtg.triggers as triggers
        src = inspect.getsource(triggers.queue_unhandled_combat_damage)
        assert 'strip_activated_ability_lines' in src


# ---------------------------------------------------------------------------
# Batch-tail templates
# ---------------------------------------------------------------------------

class TestBatchTailTemplates:
    def test_predator_ooze_declare_time_counter(self):
        lib = _lib()
        a = lib._gen_attack_self_counter("Rick", "Claude",
                                         {"attacking_name": "Predator Ooze"})
        assert a == [{"action": "add_counters", "card": "Predator Ooze",
                      "counter_type": "+1/+1", "amount": 1}]
        # The combat-damage dispatch sharing the registry must NOT re-fire it.
        assert lib._gen_attack_self_counter(
            "Rick", "Claude",
            {"attacking_name": "Predator Ooze", "damage_dealt": 3}) == []

    def test_bloodmad_vampire_damage_gated(self):
        lib = _lib()
        assert lib._gen_combat_damage_self_counter(
            "Rick", "Claude", {"attacking_name": "Bloodmad Vampire"}) == []
        a = lib._gen_combat_damage_self_counter(
            "Rick", "Claude",
            {"attacking_name": "Bloodmad Vampire", "damage_dealt": 4})
        assert a == [{"action": "add_counters", "card": "Bloodmad Vampire",
                      "counter_type": "+1/+1", "amount": 1}]

    def test_underworld_sentinel_linkage_round_trip(self, game, make_card):
        lib = _lib()
        rick = game.players[0]
        big = make_card("Woodfall Primus", power="6", toughness="6")
        small = make_card("Sakura-Tribe Elder", power="1", toughness="1")
        rick.graveyard.extend([small, big])
        ctx = {"_game": game, "_controller_player": rick}
        attack = lib._gen_underworld_sentinel_attack("Rick", "Claude", ctx)
        assert attack == [{"action": "move_card", "card": "Woodfall Primus",
                           "from_zone": "graveyard", "to_zone": "exile",
                           "player": "Rick"}], "exiles the BEST creature"
        # Simulate the exile actually happening, then the Sentinel dying.
        rick.graveyard.remove(big)
        rick.exile.append(big)
        dies = lib._gen_underworld_sentinel_dies("Rick", "Claude", ctx)
        assert dies == [{"action": "move_card", "card": "Woodfall Primus",
                         "from_zone": "exile", "to_zone": "battlefield",
                         "player": "Rick"}]
        # Linkage is consumed — a second death returns nothing.
        assert lib._gen_underworld_sentinel_dies("Rick", "Claude", ctx) == []

    def test_underworld_sentinel_dies_verifies_exile(self, game, make_card):
        # A recorded card that never made it to exile is skipped (self-heal).
        lib = _lib()
        rick = game.players[0]
        ctx = {"_game": game, "_controller_player": rick}
        game._linked_exiles["underworld sentinel|Rick"] = ["Ghost Card"]
        assert lib._gen_underworld_sentinel_dies("Rick", "Claude", ctx) == []

    def test_underworld_sentinel_empty_graveyard_fizzles(self, game, make_card):
        lib = _lib()
        ctx = {"_game": game, "_controller_player": game.players[0]}
        assert lib._gen_underworld_sentinel_attack("Rick", "Claude", ctx) == []

    def test_yidris_grant_action_and_flag(self, rules, game):
        from mtg.actions import execute_action_on_state
        lib = _lib()
        a = lib._gen_yidris_cascade_grant("Rick", "Claude", {"damage_dealt": 5})
        assert a == [{"action": "grant_hand_cascade", "player": "Rick",
                      "source": "Yidris, Maelstrom Wielder"}]
        assert lib._gen_yidris_cascade_grant("Rick", "Claude", {}) == []
        msg = execute_action_on_state(rules, game, dict(a[0]))
        assert game._hand_cascade_grants.get("Rick") == game.turn_number
        assert msg and "cascade" in msg.lower()

    def test_cascade_consult_wired_at_cast_site(self):
        import mtg.triggers as triggers
        src = inspect.getsource(triggers._check_cast_triggers)
        assert '_hand_cascade_grants' in src
        assert '_cast_from_graveyard' in src, \
            "the grant must be hand-casts-only"

    def test_soulherder_endstep_dispatch_resolves(self):
        # The duplicate bare-key registration (guard-dodging description)
        # caused 17 Tier-3 drains in batch 15324. End-step dispatch must now
        # resolve via the suffix key.
        lib = _lib()
        assert "soulherder endstep" in lib._card_templates
        actions, desc = lib.resolve_etb(
            card_name="Soulherder",
            oracle_text=("Whenever this or another creature leaves the "
                         "battlefield, put a +1/+1 counter on this creature.\n"
                         "At the beginning of your end step, exile up to one "
                         "target creature you control, then return that card "
                         "to the battlefield under its owner's control."),
            controller="Rick", opponent="Claude",
            game_context={"best_own_etb_creature": "Mulldrifter"},
            event_type="end_step")
        assert actions and actions[0]["action"] == "flicker"
        assert actions[0].get("source") == "Soulherder"


# ---------------------------------------------------------------------------
# Deterministic judge guards (fog / bounce-attackers)
# ---------------------------------------------------------------------------

class TestJudgeDeterministicGuards:
    def test_fog_activation_resolves_deterministically(self, rules, game):
        msgs, actions = asyncio.run(rules.resolve_effect(
            game, "Prevent all combat damage that would be dealt this turn.",
            source_card="Spore Frog", controller="Rick"))
        assert actions == [{"action": "prevent_combat_damage", "scope": "all"}]

    def test_bounce_all_attackers_resolves_deterministically(self, rules, game, make_card):
        rick, claude = game.players
        att1 = make_card("Grizzly Bears", attacking=True)
        att2 = make_card("Stolen Titan", attacking=True)
        att2.owner_index = 1  # controlled by Rick, owned by Claude
        bystander = make_card("Wall of Omens")
        rick.battlefield.extend([att1, att2, bystander])
        msgs, actions = asyncio.run(rules.resolve_effect(
            game, "Return all attacking creatures to their owner's hand.",
            source_card="Aetherize", controller="Claude"))
        by_card = {a["card"]: a for a in actions}
        assert set(by_card) == {"Grizzly Bears", "Stolen Titan"}
        assert by_card["Grizzly Bears"]["player"] == "Rick"
        assert by_card["Stolen Titan"]["player"] == "Claude", \
            "stolen creatures return to their OWNER's hand"
        for a in actions:
            assert a["action"] == "move_card" and a["to_zone"] == "hand"


# ---------------------------------------------------------------------------
# Reviewer: Ash Zealot's graveyard-cast condition
# ---------------------------------------------------------------------------

class TestAshZealotGraveyardGate:
    SENTENCE = ("whenever a player casts a spell from a graveyard, this "
                "creature deals 3 damage to that player")

    def _card(self, from_gy):
        c = types.SimpleNamespace(
            type_line="Sorcery", oracle_text="", cmc=2, adventure_name=None,
            cast_as_adventure=False, _cast_from_graveyard=from_gy)
        c.is_creature = lambda: False
        c.is_instant = lambda: False
        c.is_sorcery = lambda: True
        c.is_artifact = lambda: False
        c.is_enchantment = lambda: False
        return c

    def test_hand_cast_does_not_fire(self):
        from mtg.triggers import _spell_matches_cast_trigger
        assert _spell_matches_cast_trigger(
            None, self.SENTENCE, self._card(False)) is False

    def test_graveyard_cast_fires(self):
        from mtg.triggers import _spell_matches_cast_trigger
        assert _spell_matches_cast_trigger(
            None, self.SENTENCE, self._card(True)) is True

    def test_effect_half_graveyard_mention_not_gated(self):
        # The graveyard phrase in the EFFECT clause must not gate the trigger.
        from mtg.triggers import _spell_matches_cast_trigger
        s = ("whenever you cast a sorcery spell, return target card "
             "from your graveyard to your hand")
        assert _spell_matches_cast_trigger(None, s, self._card(False)) is True


# ---------------------------------------------------------------------------
# Reviewer: Gisela's doubling loses its house-rule source gate
# ---------------------------------------------------------------------------

class TestGiselaDoubleNoSourceGate:
    def _double(self):
        from rules.replacement import _NAMED_CARD_REPLACEMENTS
        effects = _NAMED_CARD_REPLACEMENTS["gisela, blade of goldnight"](
            "gisela_1", "Claude")
        return next(e for e in effects
                    if e.replacement_type == "double_damage_opp")

    def test_spell_damage_with_no_source_controller_doubles(self):
        # A resolved instant is off-battlefield: source_controller is "".
        # Her printed clause 1 has no source qualifier (CR: "If a source
        # would deal damage to an opponent...").
        eff = self._double()
        ev = types.SimpleNamespace(source_controller="", affected_player="Rick")
        assert eff.condition(ev) is True

    def test_damage_to_controller_never_doubles(self):
        eff = self._double()
        ev = types.SimpleNamespace(source_controller="", affected_player="Claude")
        assert eff.condition(ev) is False


# ---------------------------------------------------------------------------
# Reviewer: replacement scanner strips activated-ability lines
# ---------------------------------------------------------------------------

class TestReplacementScanStrip:
    ROILING = ("At the beginning of each player's upkeep, this enchantment "
               "deals 1 damage to them.\n"
               "Whenever a player casts a spell, if no mana was spent to cast "
               "that spell, this enchantment deals 5 damage to that player.\n"
               "{R}: Your opponents can't gain life this turn.")

    def test_roiling_vortex_registers_nothing(self):
        from rules.replacement import scan_oracle_for_replacements
        assert scan_oracle_for_replacements(
            "rv_1", "Roiling Vortex", self.ROILING, "Rick") == []

    def test_genuine_static_still_registers(self):
        # Positive control: an UNCONDITIONAL can't-gain-life line still
        # registers, so the strip didn't blind the scanner.
        from rules.replacement import scan_oracle_for_replacements
        effects = scan_oracle_for_replacements(
            "x_1", "Test Static", "Players can't gain life.", "Rick")
        assert effects, "the generic pattern must still see real statics"


# ---------------------------------------------------------------------------
# Reviewer: Garruk -3 draws zero off an empty board
# ---------------------------------------------------------------------------

class TestGarrukMinusThree:
    ABILITY = "-3: Draw cards equal to the greatest power among creatures you control."

    def test_empty_board_draws_zero(self):
        actions, _ = _lib().resolve_pw_ability(
            "Garruk, Primal Hunter", self.ABILITY, "Rick", "Claude",
            game_context={"greatest_power": 0})
        assert actions and actions[0]["action"] == "no_action"

    def test_board_power_five_draws_five(self):
        actions, _ = _lib().resolve_pw_ability(
            "Garruk, Primal Hunter", self.ABILITY, "Rick", "Claude",
            game_context={"greatest_power": 5})
        assert actions == [{"action": "draw_cards", "player": "Rick",
                            "amount": 5}]


# ---------------------------------------------------------------------------
# Reviewer: extort survives the parenthetical strip
# ---------------------------------------------------------------------------

CRYPT_GHAST = ("Extort (Whenever you cast a spell, you may pay {W/B}. If you "
               "do, each opponent loses 1 life and you gain that much life.)\n"
               "Whenever you tap a Swamp for mana, add an additional {B}.")


class TestExtortDetection:
    def test_strip_alone_kills_extort_condition(self):
        # The regression mechanism: extort's whole trigger is reminder text.
        stripped = re.sub(r'\([^)]*\)', '', CRYPT_GHAST)
        assert 'whenever you cast' not in stripped.lower()

    def test_cast_scan_carves_out_extort(self):
        import mtg.triggers as triggers
        src = inspect.getsource(triggers._check_cast_triggers)
        assert re.search(r"extort", src), \
            "the battlefield cast-trigger scan lost its extort carve-out"
        # The carve-out must rescan the RAW oracle for extort cards.
        assert 'bf_oracle_scan = bf_oracle' in src


# ---------------------------------------------------------------------------
# Reviewer: X-cost activations (CR 118.9) + Tier-3 prose gate
# ---------------------------------------------------------------------------

class TestActivationXAndProseGate:
    def test_x_threaded_through_payment_and_effect(self):
        import mtg.engine as eng
        src = inspect.getsource(eng)
        assert 'x_value=_activation_x or 0' in src, \
            "activation payment lost its auto-sized X"
        assert re.search(r"re\.sub\(r'\\bX\\b', str\(_activation_x\)", src), \
            "the paid X must be substituted into the effect text (CR 118.9)"

    def test_tier3_gate_is_on_actions_not_messages(self):
        import mtg.engine as eng
        src = inspect.getsource(eng)
        assert 'if t3_msgs or t3_actions:' not in src, \
            "zero-action judge prose would leak to Discord again"


# ---------------------------------------------------------------------------
# Reviewer wave 2 (transform/adventure game)
# ---------------------------------------------------------------------------

class TestCounterNotDoubleApplied:
    def test_tier2_counter_writes_dict_only(self, game, make_card):
        # The counters-dict write IS the counter; get_effective_power reads
        # it. The old extra power_modifier bump made a Tier-2-placed +1/+1
        # counter read as +2 until the end-of-turn modifier sweep.
        from rules.effects import Effect, EffectType, ExecutionContext
        from rules.spell_resolver import SpellResolver
        bear = make_card("Grizzly Bears")
        eff = Effect(effect_type=EffectType.ADD_COUNTER, amount=1,
                     counter_type="+1/+1")
        ctx = ExecutionContext(game_state=game, source_card=None,
                               source_controller=None, targets=[bear])
        asyncio.run(SpellResolver._exec_add_counter(None, eff, ctx, game))
        assert bear.counters.get("+1/+1") == 1
        assert bear.power_modifier == 0, "counter must not also bump the modifier"
        assert bear.get_effective_power(game) == 3  # 2 base + 1 counter, not 4


class TestAdventureCmc:
    def test_adventure_cmc_is_creature_face_only(self):
        from mtg.models import Card
        c = Card(id="oak1", name="Oakhame Ranger",
                 type_line="Creature — Elf Knight // Sorcery — Adventure",
                 oracle_text="",
                 mana_cost="{G/W}{G/W}{G/W}{G/W} // {G/W}{G/W}{G/W}{G/W}")
        assert c.cmc == 4, "adventure MV is the creature face's (was 8)"

    def test_split_cmc_stays_combined(self):
        # CR 708.4a: off the stack, a split card's halves are combined.
        from mtg.models import Card
        c = Card(id="cm1", name="Commit // Memory",
                 type_line="Instant // Sorcery",
                 oracle_text="", mana_cost="{3}{U} // {4}{U}{U}")
        assert c.cmc == 10


class TestFrontFaceKeywords:
    SAGE = {
        "layout": "transform",
        "keywords": ["Vigilance", "Transform", "Trample"],
        "card_faces": [
            {"oracle_text": ("Sage of Ancient Lore's power and toughness are "
                             "each equal to the number of cards in your hand.\n"
                             "When this creature enters, draw a card.\n"
                             "At the beginning of each upkeep, if no spells "
                             "were cast last turn, transform this creature.")},
            {"oracle_text": "Vigilance, trample\n..."},
        ],
    }

    def test_back_face_keywords_filtered_at_load(self):
        from mtg.deck_loader import _front_face_keywords
        kws = _front_face_keywords(self.SAGE)
        assert "Vigilance" not in kws and "Trample" not in kws, \
            "CR 712.1 — a face has only its own printed characteristics"

    def test_single_faced_and_adventure_pass_through(self):
        from mtg.deck_loader import _front_face_keywords
        plain = {"keywords": ["Flying"], "card_faces": []}
        assert _front_face_keywords(plain) == ["Flying"]
        adv = {"layout": "adventure", "keywords": ["Vigilance"],
               "card_faces": [{"oracle_text": "a"}, {"oracle_text": "b"}]}
        assert _front_face_keywords(adv) == ["Vigilance"]

    def test_all_loader_sites_use_the_filter(self):
        import mtg.deck_loader as dl
        src = inspect.getsource(dl)
        assert 'keywords=scryfall_data.get("keywords"' not in src, \
            "a loader construction site bypasses _front_face_keywords"


THASSA_ORACLE = ("Indestructible\nAs long as your devotion to blue is less "
                 "than five, Thassa isn't a creature.\nAt the beginning of "
                 "your end step, exile up to one other target creature you "
                 "control, then return that card to the battlefield under "
                 "your control.")


class TestFlickerFallbackGuards:
    def test_source_never_flickers_itself(self, rules, game, make_card):
        # Mutation-hardened: a flickered permanent RETURNS to the battlefield,
        # so membership can't detect the illegal self-flicker — assert on the
        # action's message instead ("**Thassa** flickered" is the bug).
        from mtg.actions import execute_action_on_state
        rick = game.players[0]
        thassa = make_card("Thassa, Deep-Dwelling",
                           type_line="Legendary Enchantment Creature — God",
                           oracle_text=THASSA_ORACLE, power="6", toughness="5")
        rick.battlefield.append(thassa)
        msg = execute_action_on_state(rules, game, {
            "action": "flicker", "player": "Rick",
            "source": "Thassa, Deep-Dwelling"})
        assert 'flickered' not in (msg or ''), \
            f"no legal target — the flicker must fizzle, not self-target: {msg!r}"
        assert thassa in rick.battlefield

    def test_etb_source_never_flickers_itself(self, rules, game, make_card):
        # The FIRST fallback loop prefers creatures whose oracle mentions
        # 'enters' — a flicker SOURCE with its own ETB text (Felidar
        # Guardian) must still be excluded there, or it self-picks (which in
        # real play is an infinite flicker loop).
        from mtg.actions import execute_action_on_state
        rick = game.players[0]
        felidar = make_card(
            "Felidar Guardian",
            oracle_text=("When this creature enters, you may exile another "
                         "target permanent you control, then return that card "
                         "to the battlefield under its owner's control."))
        rick.battlefield.append(felidar)
        msg = execute_action_on_state(rules, game, {
            "action": "flicker", "player": "Rick",
            "source": "Felidar Guardian"})
        assert 'flickered' not in (msg or ''), \
            f"ETB-text source self-picked by the first fallback loop: {msg!r}"

    def test_other_creature_preferred_over_source(self, rules, game, make_card):
        from mtg.actions import execute_action_on_state
        rick = game.players[0]
        thassa = make_card("Thassa, Deep-Dwelling",
                           type_line="Legendary Enchantment Creature — God",
                           oracle_text=THASSA_ORACLE, power="6", toughness="5")
        mull = make_card("Mulldrifter",
                         oracle_text="When this creature enters, draw two cards.")
        rick.battlefield.extend([thassa, mull])
        msg = execute_action_on_state(rules, game, {
            "action": "flicker", "player": "Rick",
            "source": "Thassa, Deep-Dwelling"})
        assert msg and "Mulldrifter" in msg


class TestUpkeepPhase2Label:
    def test_both_phases_gate_resolved_on_real_actions(self):
        import mtg.triggers as triggers
        src = inspect.getsource(triggers._check_upkeep_triggers_sync)
        # The May 7 label fix must exist in BOTH the active-player scan and
        # the Phase-2 (non-active-player "each upkeep") scan. Phase 1 spells
        # it across lines; count the core generator expression, not layout.
        assert src.count('a.get("action") != "no_action" for a in actions') >= 2, \
            "the Phase-2 upkeep scan lost its real-action label gate"
