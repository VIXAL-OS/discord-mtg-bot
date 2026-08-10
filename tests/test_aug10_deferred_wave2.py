"""Pins for the Aug 10, 2026 DEFERRED-QUEUE session.

These cover the "remaining 22" recorded at the end of the Aug 10 card-targeted
wave. Every premise was re-verified against source and against real oracle text
read from data/card_data_cache.json before anything was changed — three of the
recorded mechanisms were incomplete, and the corrections are pinned here rather
than only described.

Fixture discipline (the pin-shape-reachability ledger): oracle text comes from
the disk cache, not from memory, and every pin drives the entry point the live
path actually calls, not a helper in isolation.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from conftest import _make_card, _make_game  # noqa: E402

_CACHE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'data', 'card_data_cache.json')


def _oracle(name):
    with open(_CACHE_PATH, encoding='utf-8') as handle:
        entry = json.load(handle)[name.lower()]
    if entry.get('card_faces'):
        return entry['card_faces'][0].get('oracle_text', '') or ''
    return entry.get('oracle_text', '') or ''


# ===========================================================================
# A — MASS DAMAGE bypassed the replacement engine entirely.
#
# The MASS DAMAGE branch in mtg/spells.py did `c.damage_marked += dmg` raw,
# so Furnace of Rath / Gisela / Torbran / Fiery Emancipation / Insult // Injury
# / Angrath's Marauders were ALL silently inert against every board wipe --
# defeating the replacement_chain deck's entire premise -- while the "and each
# player" half FOUR LINES BELOW already routed through the player funnel.
#
# The obvious fix was wrong and is pinned against: apply_combat_damage_to_creature
# stamps is_combat_damage=True, which would misfile a sorcery's damage as combat
# damage. That flag is load-bearing in BOTH directions -- Solphim's registration
# gates on `not ev.is_combat_damage` and would have started firing wrongly.
# ===========================================================================

class TestMassDamageRoutesThroughReplacements:

    def _wipe(self, dmg_text, doubler_name=None):
        from mtg.spells import resolve_special_effects
        from mtg.rules_engine import RulesEngine

        game = _make_game()
        rick, claude = game.players
        rules = RulesEngine(None)
        game._rules_engine = rules

        class _Eng:
            pass
        engine = _Eng()
        engine.rules = rules

        victim = _make_card("Grizzly Bears", power="4", toughness="4")
        claude.battlefield.append(victim)

        if doubler_name:
            doubler = _make_card(doubler_name,
                                 type_line="Enchantment",
                                 oracle_text=_oracle(doubler_name))
            rick.battlefield.append(doubler)
            game.register_replacement_effects(doubler, rick.name)

        wipe = _make_card("Anger of the Gods", type_line="Sorcery",
                          oracle_text=dmg_text)
        resolve_special_effects(engine, game, rick, wipe)
        return victim

    def test_a_damage_doubler_applies_to_a_board_wipe(self):
        """Furnace of Rath is symmetric and doubles Anger of the Gods."""
        text = _oracle("anger of the gods")
        assert "deals 3 damage to each creature" in text

        plain = self._wipe(text)
        assert plain.damage_marked == 3, (
            f"baseline: unmodified wipe marks 3, got {plain.damage_marked}")

        doubled = self._wipe(text, doubler_name="furnace of rath")
        assert doubled.damage_marked == 6, (
            f"Furnace of Rath must double a board wipe (CR 614). "
            f"got {doubled.damage_marked} -- the raw `damage_marked +=` is back")

    def test_mass_damage_is_not_labelled_combat_damage(self):
        """The naive fix (reusing apply_combat_damage_to_creature) would stamp
        is_combat_damage=True. Solphim registers a NONcombat-only doubler, so
        it must fire here; a combat-labelled event would leave it inert."""
        import inspect
        from mtg import spells
        src = inspect.getsource(spells.resolve_special_effects)
        assert "apply_noncombat_damage_to_creature" in src, (
            "mass damage must route through the NONCOMBAT creature funnel")
        assert "apply_combat_damage_to_creature" not in src, (
            "a spell's damage is not combat damage -- is_combat_damage=True "
            "would wrongly silence Solphim and wrongly wake combat-only "
            "replacements")

    def test_devotion_gated_god_is_not_hit_as_a_creature(self):
        """is_creature() with no `game` bypasses the devotion type-flip gate
        (CR 207.4), so a below-threshold god read as a creature and took the
        wipe. The June-10 D4 class."""
        from mtg.spells import resolve_special_effects
        from mtg.rules_engine import RulesEngine

        game = _make_game()
        rick, claude = game.players
        rules = RulesEngine(None)
        game._rules_engine = rules

        class _Eng:
            pass
        engine = _Eng()
        engine.rules = rules

        erebos = _make_card(
            "Erebos, God of the Dead",
            type_line="Legendary Enchantment Creature — God",
            oracle_text=("Indestructible\nAs long as your devotion to black is "
                         "less than five, Erebos isn't a creature."),
            power="5", toughness="7")
        claude.battlefield.append(erebos)
        assert not erebos.is_creature(game), (
            "fixture precondition: devotion is 0, so Erebos is NOT a creature")

        wipe = _make_card("Anger of the Gods", type_line="Sorcery",
                          oracle_text=_oracle("anger of the gods"))
        resolve_special_effects(engine, game, rick, wipe)
        assert erebos.damage_marked == 0, (
            "a devotion-disabled god is not a creature and takes no "
            "'damage to each creature'")


class TestAngerOfTheGodsExileReplacement:
    """"If a creature dealt damage this way would die this turn, exile it
    instead." CR 700.4 — an exiled creature never DIES, so its dies-triggers
    must not fire. Live evidence was a Hangarback Walker minting Thopters off
    a death that per the rules never happened."""

    def _cast_wipe(self, wipe_name, victim_toughness="2"):
        from mtg.spells import resolve_special_effects
        from mtg.sba import process_state_based_actions
        from mtg.rules_engine import RulesEngine

        game = _make_game()
        rick, claude = game.players
        rules = RulesEngine(None)
        game._rules_engine = rules

        class _Eng:
            pass
        engine = _Eng()
        engine.rules = rules

        victim = _make_card(
            "Hangarback Walker",
            type_line="Artifact Creature — Construct",
            oracle_text=("When this creature dies, create a 1/1 colorless "
                         "Thopter artifact creature token with flying for each "
                         "+1/+1 counter on this creature."),
            power="2", toughness=victim_toughness)
        claude.battlefield.append(victim)

        wipe = _make_card(wipe_name, type_line="Sorcery",
                          oracle_text=_oracle(wipe_name))
        resolve_special_effects(engine, game, rick, wipe)
        process_state_based_actions(rules, game)
        return game, claude, victim

    def test_a_creature_killed_by_anger_is_exiled_not_buried(self):
        assert "would die this turn, exile it instead" in \
            _oracle("anger of the gods"), "fixture precondition"

        game, claude, victim = self._cast_wipe("anger of the gods")
        names_gy = [c.name for c in claude.graveyard]
        names_ex = [c.name for c in claude.exile]
        assert "Hangarback Walker" not in names_gy, (
            f"CR 700.4: exiled instead of dying. graveyard={names_gy}")
        assert "Hangarback Walker" in names_ex, (
            f"it must land in exile. exile={names_ex}")

    def test_the_exiled_creature_fires_no_dies_trigger(self):
        game, claude, _ = self._cast_wipe("anger of the gods")
        thopters = [c for c in claude.battlefield if "Thopter" in c.name]
        assert not thopters, (
            f"a creature that never died has no dies-trigger; got {thopters}")

    def test_a_wipe_without_the_clause_does_not_register(self):
        """Blasphemous Act resolves through the SAME branch and prints no
        exile clause — the gate is the whole printed phrase, not 'exile'."""
        assert "exile it instead" not in _oracle("blasphemous act")
        game, claude, victim = self._cast_wipe("blasphemous act",
                                               victim_toughness="2")
        assert "Hangarback Walker" in [c.name for c in claude.graveyard], (
            "an ordinary wipe still buries its victims")

    def test_the_replacement_expires_at_end_of_turn(self):
        """"...would die THIS TURN". The turn clamp is what makes the effect
        self-expiring with no cleanup pass; without it a creature Anger merely
        damaged would be exiled instead of dying for the rest of the GAME.
        (Pinned because the mutant that removes the clamp survived everything
        else in this class.)"""
        from mtg.spells import resolve_special_effects
        from mtg.sba import process_state_based_actions
        from mtg.rules_engine import RulesEngine

        game = _make_game()
        rick, claude = game.players
        rules = RulesEngine(None)
        game._rules_engine = rules

        class _Eng:
            pass
        engine = _Eng()
        engine.rules = rules

        # 5 toughness: takes Anger's 3 and lives.
        survivor = _make_card("Colossal Dreadmaw", power="6", toughness="5")
        claude.battlefield.append(survivor)

        wipe = _make_card("Anger of the Gods", type_line="Sorcery",
                          oracle_text=_oracle("anger of the gods"))
        resolve_special_effects(engine, game, rick, wipe)
        process_state_based_actions(rules, game)
        assert survivor in claude.battlefield, "fixture: it survives the wipe"

        # A later turn: damage wears off, then it dies to something else.
        game.turn_number += 1
        survivor.damage_marked = 99
        process_state_based_actions(rules, game)

        assert "Colossal Dreadmaw" in [c.name for c in claude.graveyard], (
            "on a LATER turn the creature dies normally — the replacement was "
            "scoped to 'this turn' and must have expired")
        assert "Colossal Dreadmaw" not in [c.name for c in claude.exile], (
            "an expired turn-scoped replacement must not still be exiling")

    def test_the_replacement_is_scoped_to_creatures_it_actually_damaged(self):
        """The condition is an id-set membership test, so a creature that
        entered AFTER the wipe is unaffected."""
        from mtg.sba import process_state_based_actions
        game, claude, _ = self._cast_wipe("anger of the gods")
        latecomer = _make_card("Latecomer", power="1", toughness="1")
        latecomer.damage_marked = 5
        claude.battlefield.append(latecomer)
        process_state_based_actions(game._rules_engine, game)
        assert "Latecomer" in [c.name for c in claude.graveyard], (
            "a creature Anger never damaged dies normally")


class TestAdventurePumpTemplates:
    """G3 — two adventure-half templates were written from text no printing has.

    Ground truth from data/scryfall_oracle_cards.json card_faces (a top-level
    name search MISSES adventure halves, which is what made these look like
    fake cards on the first pass):
      On Alert       "Target creature gets +2/+2 until end of turn. Untap it."
      Gift of the Fae "Target creature gets +2/+1 and gains flying until end of turn."
    Both front faces (Silverflame Squire, Faerie Guidemother) are in
    data/test_adventure_chulane.json, so both are live-reachable.
    """

    def _resolve(self, key, ctx):
        from rules.effect_templates import get_effect_library
        tpl = get_effect_library()._card_templates[key]
        return tpl.action_generator("Rick", "Claude", ctx)

    def test_on_alert_is_single_target_and_untaps(self):
        acts = self._resolve("on alert", {"explicit_target_name": "Ambusher"})
        pumps = [a for a in acts if a["action"] == "pump_all_creatures"]
        untaps = [a for a in acts if a["action"] == "untap"]
        assert len(pumps) == 1 and len(untaps) == 1, f"got {acts}"
        assert (pumps[0]["power"], pumps[0]["toughness"]) == (2, 2), (
            f"printed +2/+2, got +{pumps[0]['power']}/+{pumps[0]['toughness']}")
        assert pumps[0].get("card") == "Ambusher", (
            "a single declared target must narrow the pump — an unnarrowed "
            "pump_all_creatures buffs the whole board")
        assert untaps[0].get("card") == "Ambusher", (
            "the printed 'Untap it' was missing entirely")

    def test_gift_of_the_fae_is_single_target_with_flying(self):
        acts = self._resolve("gift of the fae", {"explicit_target_name": "Ambusher"})
        pumps = [a for a in acts if a["action"] == "pump_all_creatures"]
        assert len(pumps) == 1, f"got {acts}"
        assert (pumps[0]["power"], pumps[0]["toughness"]) == (2, 1), (
            f"printed +2/+1, got +{pumps[0]['power']}/+{pumps[0]['toughness']}")
        assert pumps[0].get("card") == "Ambusher"
        assert [k.lower() for k in pumps[0].get("keywords", [])] == ["flying"]
        assert not any(a.get("target") == "all_own_creatures" for a in acts), (
            "flying was being granted TEAM-WIDE by a separate grant_keywords")

    def test_both_decline_cleanly_with_no_target(self):
        for key in ("on alert", "gift of the fae"):
            acts = self._resolve(key, {})
            assert [a["action"] for a in acts] == ["no_action"], (
                f"{key} with no target must be a clean no-op, got {acts}")

    def test_the_json_entry_was_removed_not_duplicated(self):
        """On Alert needs a TARGET, so it cannot live in card_templates.json —
        that file holds only fixed action lists. The loader's strict collision
        check would raise on import if both registrations survived, but assert
        it directly so the reason is recorded."""
        import json
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, 'data', 'card_templates.json'),
                  encoding='utf-8') as fh:
            data = json.load(fh)
        assert not [t for t in data['templates'] if t.get('key') == 'on alert'], (
            "the fixed-action-list entry must be gone; the Python generator owns it")

    def test_the_action_keys_are_ones_the_handler_actually_reads(self):
        """An invented key is silently ignored — that has shipped here. `card`
        is pump_all_creatures' include_name filter and `keywords` is its grant
        list; both are read."""
        import inspect
        from mtg import actions
        src = inspect.getsource(actions.execute_action_on_state)
        assert 'action.get("card", "") or action.get("include_name", "")' in src
        assert 'action.get("keywords", [])' in src


class TestGSeamTemplatesAndTiming:
    """G1/G2/G5/G6/G7 — the seam whose verification agent hit the session limit
    on the first pass. Re-verified; every recorded mechanism in it was wrong or
    incomplete in a way that moved the fix."""

    def _lib(self):
        from rules.effect_templates import get_effect_library
        return get_effect_library()

    # --- G1: Arlinn Kord -------------------------------------------------
    def test_arlinn_front_face_transforms_as_well_as_making_the_wolf(self):
        """The recorded mechanism blamed mtg/triggers.py's day/night routine —
        a fix there would have been dead code. Arlinn has neither
        daybound/nightbound nor an upkeep transform; her transform is an
        instruction inside a LOYALTY ability. Both action vocabulary and the
        execution path already existed; only the emit was missing."""
        class _Src:
            name = 'Arlinn Kord'
        acts, _ = self._lib().resolve_pw_ability(
            'Arlinn Kord',
            '0: Create a 2/2 green Wolf creature token. Transform Arlinn Kord.',
            'Rick', 'Claude', {'_source_card': _Src()})
        kinds = [a['action'] for a in acts]
        assert 'create_token' in kinds and 'transform_permanent' in kinds, kinds
        tp = next(a for a in acts if a['action'] == 'transform_permanent')
        assert tp.get('player') == 'Rick', (
            "the handler returns None without `player` — omitting it makes the "
            "whole transform a silent no-op")
        assert tp.get('card') == 'Arlinn Kord', (
            "must use the live source name, not a literal: the back face prints "
            "the SHORTENED 'Transform Arlinn.' and the handler matches on the "
            "current battlefield name")

    def test_arlinn_back_face_also_transforms(self):
        """The finding named only the [0]. Her [-1] transforms her too — the
        full-Scryfall sweep finds exactly two loyalty abilities printing
        Transform in all of Magic, and both are hers."""
        class _Src:
            name = 'Arlinn, Embraced by the Moon'
        acts, _ = self._lib().resolve_pw_ability(
            'Arlinn, Embraced by the Moon',
            '-1: Arlinn deals 3 damage to any target. Transform Arlinn.',
            'Rick', 'Claude', {'_source_card': _Src(), 'explicit_target_name': 'Bear'})
        assert [a['action'] for a in acts] == ['deal_damage', 'transform_permanent']
        assert acts[1]['card'] == 'Arlinn, Embraced by the Moon'

    def test_the_arlinn_keys_use_full_face_names(self):
        """resolve_pw_ability matches key_pw with a SUBSTRING test, so a bare
        'arlinn' key would also match Arlinn, the Pack's Hope — the DAYBOUND
        walker in the same deck, whose transform is correctly owned by the
        day/night routine. That would hijack a working card."""
        keys = [k for k in self._lib()._pw_ability_templates if 'arlinn' in k[0]]
        assert keys, "Arlinn must be registered"
        assert all(k[0] in ('arlinn kord', 'arlinn, embraced by the moon')
                   for k in keys), f"bare-name key would hijack another Arlinn: {keys}"

    # --- G2: Nightpack Ambusher ------------------------------------------
    def test_nightpack_respects_its_intervening_if(self):
        """CR 603.4. The recorded mechanism was wrong twice: the datum it named
        is not read in the end-step scan at all, and it is the wrong datum
        semantically ("this turn", not "last turn")."""
        lib = self._lib()
        tpl = lib._card_templates["nightpack ambusher"]

        class _P:
            spells_cast_this_turn = 2
        assert tpl.action_generator("Rick", "Claude", {'_controller_player': _P()}) == [], (
            "cast a spell this turn -> handled no-op (an empty list, NOT None: "
            "None means unhandled and escalates to Tier 3)")

        class _Q:
            spells_cast_this_turn = 0
        acts = tpl.action_generator("Rick", "Claude", {'_controller_player': _Q()})
        assert [a['action'] for a in acts] == ['create_token']

    def test_nightpack_reads_the_turn_stamped_snapshot(self):
        """end_turn zeroes spells_cast_this_turn BEFORE end-step triggers
        dispatch on that path, so the live counter always reads 0 there. The
        snapshot is turn-STAMPED because the advance_phase path fires end-step
        triggers while the live counter is still authoritative — an unstamped
        snapshot would be a stale prior-turn value there."""
        tpl = self._lib()._card_templates["nightpack ambusher"]
        game = _make_game()
        game.turn_number = 5
        game._spells_cast_snapshot_turn = 5
        game._spells_cast_snapshot = {"Rick": 3}

        class _P:
            spells_cast_this_turn = 0        # already reset
        assert tpl.action_generator("Rick", "Claude",
                                    {'_game': game, '_controller_player': _P()}) == [], (
            "the matching-turn snapshot must win over the zeroed live counter")

        game._spells_cast_snapshot_turn = 4  # stale
        acts = tpl.action_generator("Rick", "Claude",
                                    {'_game': game, '_controller_player': _P()})
        assert [a['action'] for a in acts] == ['create_token'], (
            "a stale-turn snapshot must be IGNORED and the live counter used")

    def test_nightpack_json_entry_removed(self):
        import json
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, 'data', 'card_templates.json'), encoding='utf-8') as fh:
            data = json.load(fh)
        assert not [t for t in data['templates']
                    if t.get('key') == 'nightpack ambusher'], (
            "a conditional card cannot live in the fixed-action-list registry")

    # --- G5: Moonmist -----------------------------------------------------
    def test_moonmist_resolves_both_halves(self):
        """It was refused WHOLESALE by the combat-shape guard, losing the
        transform as well as the fog. Resolving at Tier 1.5 sidesteps the guard
        (prefer lower tiers)."""
        human = _make_card("Village Ironsmith",
                           type_line="Creature — Human Warrior")
        human.has_transform = True
        # DECISIVE FIXTURE: this non-Human is ALSO transformable, so only the
        # 'human' check can exclude it. With has_transform=False the mutant
        # that drops the type check survives — the fixture has to make the
        # gate under test the deciding one.
        wolf = _make_card("Wolf", type_line="Creature — Wolf")
        wolf.has_transform = True
        acts = self._lib()._card_templates["moonmist"].action_generator(
            "Rick", "Claude",
            {'controller_battlefield': [human, wolf], 'opponent_battlefield': []})
        kinds = [a['action'] for a in acts]
        assert 'transform_permanent' in kinds, "the transform half was being lost"
        assert 'prevent_combat_damage' in kinds, "the fog half too"
        tp = next(a for a in acts if a['action'] == 'transform_permanent')
        assert tp['card'] == "Village Ironsmith" and tp.get('player') == "Rick"
        assert not any(a.get('card') == 'Wolf' for a in acts), (
            "only Humans transform")

    def test_moonmist_transforms_humans_on_both_sides(self):
        """"Transform all Humans" is global, not controller-scoped."""
        mine = _make_card("Mine", type_line="Creature — Human")
        mine.has_transform = True
        theirs = _make_card("Theirs", type_line="Creature — Human")
        theirs.has_transform = True
        acts = self._lib()._card_templates["moonmist"].action_generator(
            "Rick", "Claude",
            {'controller_battlefield': [mine], 'opponent_battlefield': [theirs]})
        owners = {a['card']: a['player'] for a in acts
                  if a['action'] == 'transform_permanent'}
        assert owners == {"Mine": "Rick", "Theirs": "Claude"}, owners

    # --- G6b: Avabruck Caretaker -----------------------------------------
    def test_avabruck_targets_another_creature_you_control(self):
        """The recorded mechanism blamed targeting; the real cause was a MISSING
        TEMPLATE, so the trigger escalated to Tier 3, which picked a
        planeswalker and was then correctly refused — a mandatory trigger
        producing nothing with a legal target on the battlefield."""
        src = _make_card("Avabruck Caretaker", power="4", toughness="4")
        big = _make_card("Big", power="5", toughness="5")
        # DECISIVE FIXTURE: the planeswalker's "power" OUTRANKS the creature, so
        # without the is_creature filter it would win the pick — which is
        # exactly what Tier 3 did live. With power=None it loses the max either
        # way and the mutant survives.
        pw = _make_card("Teferi", type_line="Legendary Planeswalker — Teferi",
                        power="9", toughness="9")
        acts = self._lib()._card_templates["avabruck caretaker"].action_generator(
            "Rick", "Claude",
            {'controller_battlefield': [src, big, pw],
             '_trigger_source': 'avabruck caretaker'})
        assert len(acts) == 1 and acts[0]['action'] == 'add_counters'
        assert acts[0]['card'] == 'Big', (
            "must pick a CREATURE you control, never a planeswalker — even when "
            "the planeswalker's printed numbers are larger")
        assert acts[0]['amount'] == 2

    def test_avabruck_excludes_itself_and_declines_cleanly(self):
        src = _make_card("Avabruck Caretaker", power="4", toughness="4")
        acts = self._lib()._card_templates["avabruck caretaker"].action_generator(
            "Rick", "Claude",
            {'controller_battlefield': [src],
             '_trigger_source': 'avabruck caretaker'})
        assert [a['action'] for a in acts] == ['no_action'], (
            "'another' is CR 109.5 self-exclusion — with no other creature this "
            "is a clean decline, not a self-target")

    # --- G7: Ob Nixilis Reignited ----------------------------------------
    def test_ob_nixilis_plus_one_draws_and_loses(self):
        acts, _ = self._lib().resolve_pw_ability(
            'Ob Nixilis Reignited', 'You draw a card and you lose 1 life.',
            'Rick', 'Claude', {})
        assert [a['action'] for a in acts] == ['draw_cards', 'lose_life'], (
            f"both halves, draw FIRST (empty-library race / lethal life): {acts}")
        assert acts[1]['amount'] == 1

    def test_the_bare_life_loss_partial_still_works(self):
        acts, _ = self._lib().resolve_etb('X', 'You lose 3 life.', 'Rick', 'Claude', {})
        assert [a['action'] for a in acts] == ['lose_life'] and acts[0]['amount'] == 3, (
            "the combined pattern must sit ABOVE the partial without shadowing it")

    def test_the_combined_pattern_is_anchored(self):
        """`^`-anchored so it cannot fire on a card that merely CONTAINS the
        phrase mid-sentence. The control is a SCHEDULED clause (the Phyrexian
        Arena shape) with no more-specific pattern of its own: anchored, the
        combined pattern does not fire and the text falls through to the bare
        life-loss partial. Unanchored, it would match mid-sentence and resolve
        an upkeep trigger as an immediate draw+lose.

        (Dark Prophecy is NOT a valid control here: its own dies-scoped pattern
        legitimately emits draw+lose, because the dies SCAN gates when the
        template runs — the template does not gate itself.)"""
        acts, _ = self._lib().resolve_etb(
            'X',
            'At the beginning of your upkeep, you draw a card and you lose 1 life.',
            'Rick', 'Claude', {})
        assert [a['action'] for a in (acts or [])] != ['draw_cards', 'lose_life'], (
            "an unanchored combined pattern would strip the upkeep schedule "
            "and resolve it immediately")
        pats = [p for p, _t in self._lib()._pattern_templates
                if 'draw a card and you lose' in p and 'dies' not in p]
        assert pats and all(p.startswith('^') for p in pats), pats

    # --- G4: modal spells -------------------------------------------------
    def test_inscription_defaults_to_the_always_legal_mode(self):
        """A KICKED cast paid 5 mana and fizzled because the Tier-2 resolver
        aborts on the FIRST restriction with no legal target — while mode 2
        (target PLAYER) is always legal. Tier 1.5 runs before Tier 2, so a
        name-keyed generator short-circuits it."""
        t = self._lib()._card_templates["inscription of abundance"]
        # DECISIVE FIXTURE: mode 1 is SATISFIABLE here (a creature is present),
        # so the default is actually being chosen rather than reached by
        # fallthrough. Without best_own_creature, a default of mode 1 is
        # unsatisfiable and lands on gain_life anyway — the mutant survived.
        acts = t.action_generator("Rick", "Claude",
                                  {'greatest_power': 4, 'best_own_creature': 'Mine'})
        assert [a['action'] for a in acts] == ['gain_life'], (
            f"with no mode named, default to the ALWAYS-legal one; got {acts}")
        assert acts[0]['amount'] == 4, (
            "X is the greatest power among creatures they control — the real "
            "ctx key is greatest_power (controller-scoped)")

    def test_inscription_honors_kicker_for_choose_any_number(self):
        t = self._lib()._card_templates["inscription of abundance"]
        ctx = {'_modes': [1, 2, 3], 'greatest_power': 4,
               'best_own_creature': 'Mine', 'best_opponent_creature': 'Theirs'}
        unkicked = t.action_generator("Rick", "Claude", dict(ctx))
        kicked = t.action_generator("Rick", "Claude", dict(ctx, kicked=True))
        assert len(unkicked) == 1, (
            f"CR 700.2: unkicked is 'choose ONE', got {unkicked}")
        assert len(kicked) > 1, (
            "kicked is 'choose any number' — gated on the real ctx['kicked'] stamp")

    def test_inscription_uses_the_shared_fight_approximation(self):
        """There is NO 'fight' action type in the interpreter — my first draft
        invented one, which would have been silently ignored. The established
        approximation is mutual damage via the shared helper."""
        import inspect
        from mtg import actions
        assert 'action_type == "fight"' not in inspect.getsource(
            actions.execute_action_on_state), (
            "if a real fight action ever lands, this template should use it")
        t = self._lib()._card_templates["inscription of abundance"]
        acts = t.action_generator("Rick", "Claude", {
            'kicked': True, '_modes': [3], 'greatest_power': 0,
            'best_own_creature': 'Mine', 'best_opponent_creature': 'Theirs'})
        assert [a['action'] for a in acts] == ['deal_damage', 'deal_damage'], (
            f"fight resolves as mutual damage, got {acts}")

    def test_a_failed_tier2_resolution_still_reaches_tier3(self):
        """result.success was ignored entirely, and a Tier-2 failure emits a
        warning carrying no 'complex effect' marker — so the spell counted as
        RESOLVED: mana spent, no effect, no fallback."""
        import inspect
        from mtg import spells
        src = inspect.getsource(spells._dispatch_resolution)
        assert "getattr(result, 'success', True)" in src, (
            "the Tier-3 escalation must also trigger on a failed Tier-2 result")

    # --- G4 follow-up: the Tier-2 loop itself -----------------------------
    def test_a_partially_targetable_spell_resolves_the_rest(self):
        """The loop aborted the WHOLE spell on the first restriction with no
        legal target. CR 601.2c requires legal targets only for the modes
        actually chosen, so a card carrying an always-legal mode alongside a
        creature mode should not fizzle on an empty board."""
        import asyncio
        from rules.spell_resolver import SpellResolver, TargetMode
        from mtg.rules_engine import RulesEngine

        game = _make_game()
        rick, _claude = game.players
        rules = RulesEngine(None)
        game._rules_engine = rules

        # Restriction 1 (artifact) has NO legal target on an empty board;
        # restriction 2 (player) is always legal.
        card = _make_card("Probe", type_line="Instant",
                          oracle_text="Destroy target artifact. "
                                      "Target player gains 3 life.")
        before = rick.life
        res = asyncio.run(SpellResolver(rules).cast_spell(
            game, rick, card, target=None, target_mode=TargetMode.AUTO))

        assert res.success, (
            f"a spell with ONE unsatisfiable restriction must not fizzle "
            f"wholesale: {res.messages}")
        assert rick.life == before + 3, (
            f"the satisfiable half must still resolve; life {before} -> "
            f"{rick.life}")

    def test_a_fully_untargetable_spell_still_fails(self):
        """The other direction: when NOTHING is satisfiable it is a real
        fizzle, and the failure is what routes it to Tier 3."""
        import asyncio
        from rules.spell_resolver import SpellResolver, TargetMode
        from mtg.rules_engine import RulesEngine

        game = _make_game()
        rick, _ = game.players
        rules = RulesEngine(None)
        game._rules_engine = rules
        card = _make_card("Probe2", type_line="Instant",
                          oracle_text="Destroy target artifact.")
        res = asyncio.run(SpellResolver(rules).cast_spell(
            game, rick, card, target=None, target_mode=TargetMode.AUTO))
        assert not res.success, (
            "no legal target for the ONLY restriction is a genuine failure")

    def test_mode_attribution_landed_and_the_consumers_were_updated(self):
        """This pin used to assert that targets were STILL a flat list, and
        said that if per-clause attribution ever landed it should be the thing
        that fails and gets rewritten. It landed on Aug 10, so this is that
        rewrite (deliberately not a deletion — the property it guards is still
        worth guarding, just inverted).

        Attribution lives on Effect.selected_targets rather than on the
        context, because keying by POSITION was unsound: parse_effects returns
        effects in pattern-declaration order while restrictions are
        position-sorted. What must hold now is that no consumer reads the flat
        list directly — every one goes through the accessor that prefers the
        clause's own targets."""
        import dataclasses
        import inspect
        from rules.effects import Effect, ExecutionContext
        from rules import spell_resolver

        assert 'targets' in {f.name for f in dataclasses.fields(ExecutionContext)}
        assert 'selected_targets' in {f.name for f in dataclasses.fields(Effect)}
        assert callable(spell_resolver._targets_for)

        src = inspect.getsource(spell_resolver)
        for line in src.split('\n'):
            if 'ctx.targets' not in line or line.strip().startswith('#'):
                continue
            if 'selected_targets' in line:
                continue   # the accessor itself — the one sanctioned reader
            assert '_targets_for' in line, (
                f"a Tier-2 consumer still reads the flat list directly, which "
                f"is how a clause gets another clause's target: {line.strip()}")

    # --- G5 follow-up: the Moonmist exemption -----------------------------
    def test_moonmist_exempts_werewolves_from_its_own_fog(self):
        """"…by creatures other than Werewolves and Wolves." In a werewolf deck
        that exemption is the point of the card, so a blanket prevention
        inverts it. Drives the REAL combat funnel, not the helper alone."""
        from mtg.combat import apply_combat_damage_to_player
        from mtg.rules_engine import RulesEngine
        from mtg.actions import execute_action_on_state

        game = _make_game()
        rick, claude = game.players
        rules = RulesEngine(None)
        game._rules_engine = rules

        wolf = _make_card("Howlpack Alpha", type_line="Creature — Werewolf",
                          power="3", toughness="3")
        bear = _make_card("Grizzly Bears", type_line="Creature — Bear",
                          power="2", toughness="2")
        rick.battlefield.extend([wolf, bear])

        execute_action_on_state(rules, game, {
            "action": "prevent_combat_damage", "scope": "all",
            "except_subtypes": ["Werewolf", "Wolf"]})

        before = claude.life
        dealt_bear = apply_combat_damage_to_player(rules, game, claude, 2, bear)
        assert dealt_bear == 0 and claude.life == before, (
            "a Bear's combat damage IS prevented")
        dealt_wolf = apply_combat_damage_to_player(rules, game, claude, 3, wolf)
        assert dealt_wolf == 3, (
            f"a Werewolf is EXEMPT and its damage stands; got {dealt_wolf}")

    def test_an_unconditional_fog_still_prevents_everything(self):
        """The regression guard that matters: Fog and Teferi's Protection pass
        no exemption list, and their behaviour must be byte-for-byte unchanged."""
        from mtg.combat import apply_combat_damage_to_player
        from mtg.rules_engine import RulesEngine
        from mtg.actions import execute_action_on_state

        game = _make_game()
        rick, claude = game.players
        rules = RulesEngine(None)
        game._rules_engine = rules
        wolf = _make_card("Howlpack Alpha", type_line="Creature — Werewolf",
                          power="3", toughness="3")
        rick.battlefield.append(wolf)

        execute_action_on_state(rules, game, {
            "action": "prevent_combat_damage", "scope": "all"})
        assert apply_combat_damage_to_player(rules, game, claude, 3, wolf) == 0, (
            "with no exemption list even a Werewolf is prevented")

    def test_moonmist_passes_the_exemption(self):
        acts = self._lib()._card_templates["moonmist"].action_generator(
            "Rick", "Claude", {'controller_battlefield': [], 'opponent_battlefield': []})
        fog = next(a for a in acts if a['action'] == 'prevent_combat_damage')
        assert sorted(s.lower() for s in fog.get('except_subtypes', [])) == \
            ['werewolf', 'wolf']

    # --- G3 prerequisite --------------------------------------------------
    def test_the_adventure_path_forwards_the_declared_target(self):
        """The adventure branch was the ONLY resolution builder not passing
        explicit_target, so explicit_target_name was ALWAYS absent there and no
        template on that path could honor a declared target however it was
        written — the G3 template fix would have been partly dead code."""
        import inspect
        import re
        from mtg import spells
        src = inspect.getsource(spells._dispatch_resolution)
        # Match THIS call specifically, across its line wrapping. A looser check
        # (`'explicit_target=target' in src`) is satisfied by the two SIBLING
        # builders and survives the adventure one losing its argument — which is
        # how the first version of this pin passed with the fix reverted.
        call = re.search(r'build_game_context\(\s*game,\s*player,\s*opp_adv[^)]*\)', src,
                         re.DOTALL)
        assert call, "the adventure ctx builder moved — re-locate it"
        assert 'explicit_target' in call.group(0), (
            "the adventure builder must forward the declared target like both "
            f"of its siblings; got: {call.group(0)}")


class TestAvacynGrantsIndestructibleToPermanents:
    """"Other permanents you control have indestructible." The grant ladder was
    creature-shaped end to end and had no `permanents you control have` pattern
    at all, so Akroma died under Avacyn to a board wipe."""

    def _board(self):
        game = _make_game()
        rick = game.players[0]
        avacyn = _make_card("Avacyn, Angel of Hope",
                            type_line="Legendary Creature — Angel",
                            oracle_text=_oracle("avacyn, angel of hope"),
                            power="8", toughness="8")
        akroma = _make_card("Akroma, Angel of Wrath",
                            type_line="Legendary Creature — Angel",
                            oracle_text=_oracle("akroma, angel of wrath"),
                            power="6", toughness="6")
        rick.battlefield.extend([avacyn, akroma])
        game.register_static_keyword_grants(avacyn, rick.name)
        return game, rick, avacyn, akroma

    def test_the_pattern_is_matched_at_all(self):
        assert "other permanents you control have indestructible" in \
            _oracle("avacyn, angel of hope").lower(), "fixture precondition"
        game, rick, avacyn, akroma = self._board()
        assert any("permanents you control" in e.applies_to.lower()
                   for e in game.layers_engine.effects), (
            "Avacyn must register a permanents-scoped grant; the ladder had no "
            "such pattern and matched nothing")

    def test_another_creature_you_control_gets_indestructible(self):
        game, rick, avacyn, akroma = self._board()
        assert game.has_granted_keyword(akroma, "Indestructible", rick.name), (
            "Akroma is a permanent Rick controls other than Avacyn")

    def test_the_grant_is_controller_scoped_and_excludes_the_source(self):
        game, rick, avacyn, akroma = self._board()
        claude = game.players[1]
        theirs = _make_card("Grizzly Bears", power="2", toughness="2")
        claude.battlefield.append(theirs)
        assert not game.has_granted_keyword(theirs, "Indestructible", claude.name), (
            "an opponent's permanent is not 'you control'")
        assert not game.has_granted_keyword(avacyn, "Indestructible", rick.name), (
            "'Other' excludes the source (CR 109.5) — Avacyn's own "
            "indestructible is printed separately, not granted")

    def test_the_anchor_declines_a_qualified_clause(self):
        """Unanchored, this pattern reaches 'legendary permanents you control
        have...' / 'As long as you're the monarch, permanents you control
        have...' and would grant unconditionally to EVERYTHING."""
        game = _make_game()
        rick = game.players[0]
        src = _make_card("Avacyn's Memorial", type_line="Legendary Artifact",
                         oracle_text="Legendary permanents you control have indestructible.")
        rick.battlefield.append(src)
        game.register_static_keyword_grants(src, rick.name)
        assert not any("permanents you control" in e.applies_to.lower()
                       for e in game.layers_engine.effects), (
            "a qualified clause must DECLINE rather than register an "
            "unconditional board-wide grant")


class TestCastDownNonlegendary:
    """types_excluded has been declared and consumed since it was added and was
    never written by anything — a wired-but-unfed field, the has_coven class
    inverted. Every other piece already existed."""

    def _restriction(self, text):
        from rules.targeting import TargetTextParser
        return TargetTextParser().parse(text)

    def test_nonlegendary_is_parsed_into_types_excluded(self):
        assert _oracle("cast down") == "Destroy target nonlegendary creature."
        r = self._restriction("destroy target nonlegendary creature")
        assert 'legendary' in r.types_excluded, (
            "the restriction must reach types_excluded, which the validator "
            "already iterates")

    def test_a_plain_creature_clause_excludes_nothing(self):
        r = self._restriction("destroy target creature")
        assert 'legendary' not in r.types_excluded

    def test_the_word_is_matched_whole(self):
        """'legendary' is a substring of 'nonlegendary' — the trap this
        codebase has shipped eleven times. A card that says only 'legendary'
        must not be read as excluding legendaries."""
        r = self._restriction("destroy target legendary creature")
        assert 'legendary' not in r.types_excluded, (
            "a POSITIVE legendary restriction must not become an exclusion")


# ===========================================================================
# B — qualifier-restricted anthems.
#
# Two sites had the SAME unanchored pattern: the layers REGISTRATION ladder
# (register_static_pt_effects) and the inline compute-on-read FALLBACK
# (_get_anthem_power/toughness_bonus). They are mutually exclusive per creature,
# so fixing only one leaves the bug reachable.
# ===========================================================================

class TestAnthemQualifierAnchor:

    def _register(self, name, extra=None):
        game = _make_game()
        rick = game.players[0]
        src = _make_card(name, type_line="Enchantment", oracle_text=_oracle(name))
        rick.battlefield.append(src)
        for c in (extra or []):
            rick.battlefield.append(c)
        game.register_static_pt_effects(src, rick.name)
        return game, rick, src

    def test_full_moons_rise_buffs_only_werewolves(self):
        """'Werewolf creatures you control get +1/+0' -- the qualifier was
        swallowed and every creature got the buff."""
        text = _oracle("full moon's rise")
        assert text.lower().startswith("werewolf creatures you control get")

        wolf = _make_card("Howlpack Alpha",
                          type_line="Creature — Werewolf", power="3", toughness="3")
        human = _make_card("Elite Vanguard",
                           type_line="Creature — Human Soldier", power="2", toughness="1")
        game, rick, _ = self._register("full moon's rise", [wolf, human])
        game.recalculate_power_toughness()

        assert wolf.get_effective_power(game) == 4, (
            f"the Werewolf gets +1/+0, got {wolf.get_effective_power(game)}")
        assert human.get_effective_power(game) == 2, (
            f"a Human is NOT a Werewolf and must not be buffed, got "
            f"{human.get_effective_power(game)} -- the anchor regressed")

    def test_nightpack_ambusher_buffs_both_listed_subtypes(self):
        """'Other Wolves and Werewolves you control get +1/+1' matched NEITHER
        the single-subtype branch nor own_all, so it registered nothing."""
        text = _oracle("nightpack ambusher")
        assert "other wolves and werewolves you control get +1/+1" in text.lower()

        wolf = _make_card("Wolf", type_line="Creature — Wolf",
                          power="2", toughness="2")
        werewolf = _make_card("Ulvenwald Tracker",
                              type_line="Creature — Werewolf", power="1", toughness="1")
        bear = _make_card("Grizzly Bears", type_line="Creature — Bear",
                          power="2", toughness="2")

        game = _make_game()
        rick = game.players[0]
        src = _make_card("Nightpack Ambusher",
                         type_line="Creature — Wolf",
                         oracle_text=_oracle("nightpack ambusher"),
                         power="4", toughness="4")
        rick.battlefield.extend([src, wolf, werewolf, bear])
        game.register_static_pt_effects(src, rick.name)
        game.recalculate_power_toughness()

        assert wolf.get_effective_power(game) == 3, "a Wolf is buffed"
        assert werewolf.get_effective_power(game) == 2, "a Werewolf is buffed"
        assert bear.get_effective_power(game) == 2, (
            "a Bear is neither and must not be buffed")
        assert src.get_effective_power(game) == 4, (
            "'Other' excludes the source itself (CR 109.5)")

    def test_narfi_matches_a_supertype_in_the_list(self):
        """'Other snow and Zombie creatures you control' -- `snow` is a
        SUPERTYPE, not a subtype. This one previously leaked to own_all and
        buffed the entire board, which is strictly worse than not registering."""
        assert "other snow and zombie creatures you control get +1/+1" in \
            _oracle("narfi, betrayer king").lower()

        snowy = _make_card("Boreal Druid",
                           type_line="Snow Creature — Elf Druid",
                           power="1", toughness="1")
        zombie = _make_card("Zombie", type_line="Creature — Zombie",
                            power="2", toughness="2")
        plain = _make_card("Grizzly Bears", type_line="Creature — Bear",
                           power="2", toughness="2")

        game = _make_game()
        rick = game.players[0]
        src = _make_card("Narfi, Betrayer King",
                         type_line="Snow Legendary Creature — Zombie Noble",
                         oracle_text=_oracle("narfi, betrayer king"),
                         power="3", toughness="3")
        rick.battlefield.extend([src, snowy, zombie, plain])
        game.register_static_pt_effects(src, rick.name)
        game.recalculate_power_toughness()

        assert snowy.get_effective_power(game) == 2, "snow supertype qualifies"
        assert zombie.get_effective_power(game) == 3, "Zombie subtype qualifies"
        assert plain.get_effective_power(game) == 2, (
            "a plain Bear is neither snow nor Zombie -- the own_all leak is back")

    def test_beastmaster_ascension_still_registers_after_a_comma(self):
        """THE ANCHOR REGRESSION GUARD. Beastmaster Ascension prints
        '...quest counters on it, creatures you control get +5/+5' -- it is
        preceded by ', ', so a `^`/`\\n`/`\\.\\s` anchor kills a card that
        registers correctly today. Only the fixed-width lookbehind passes it."""
        text = _oracle("beastmaster ascension")
        assert ", creatures you control get +5/+5" in text.lower(), (
            "fixture precondition: the clause is comma-preceded")

        bear = _make_card("Grizzly Bears", power="2", toughness="2")
        game = _make_game()
        rick = game.players[0]
        src = _make_card("Beastmaster Ascension", type_line="Enchantment",
                         oracle_text=text)
        src.counters['quest'] = 7          # threshold met
        rick.battlefield.extend([src, bear])
        game.register_static_pt_effects(src, rick.name)
        game.recalculate_power_toughness()

        assert bear.get_effective_power(game) == 7, (
            f"Beastmaster Ascension at 7 quest counters gives +5/+5; got "
            f"{bear.get_effective_power(game)}. If this is 2, the anchor was "
            f"tightened to a line/sentence start and broke a working card.")

    def test_glorious_anthem_still_applies_to_everything(self):
        """The unqualified case must be untouched."""
        bear = _make_card("Grizzly Bears", power="2", toughness="2")
        game, rick, _ = self._register("glorious anthem", [bear])
        game.recalculate_power_toughness()
        assert bear.get_effective_power(game) == 3

    def test_inline_fallback_shares_the_anchor(self):
        """The compute-on-read path had the identical unanchored regex. It runs
        only when the layers engine registered no P/T effect at all -- which is
        exactly what happens now for a declined combat-state anthem, so a fix
        confined to the ladder would restore the bug through this door."""
        from mtg.models import _ANTHEM_INLINE_RE
        import re
        assert re.search(_ANTHEM_INLINE_RE,
                         "werewolf creatures you control get +1/+0") is None, (
            "the inline fallback must not swallow a subtype qualifier")
        assert re.search(_ANTHEM_INLINE_RE,
                         "attacking creatures you control get +1/+0") is None, (
            "nor a combat-state qualifier")
        assert re.search(_ANTHEM_INLINE_RE,
                         "creatures you control get +1/+1") is not None, (
            "the unqualified anthem must still match at string start")
        assert re.search(
            _ANTHEM_INLINE_RE,
            "as long as this has seven or more quest counters on it, "
            "creatures you control get +5/+5") is not None, (
            "and after ', ' -- Beastmaster Ascension")

    def test_own_all_pattern_rejects_a_qualifier(self):
        """The registration ladder's unqualified clause carries the same anchor.

        This is pinned DIRECTLY rather than through a card because, with the
        subtype branch above it generalized, every qualifier shape in the deck
        inventory is now claimed by an earlier branch — so an end-to-end pin
        passes with the anchor reverted and proves nothing (it did; the mutant
        survived). The anchor is defence-in-depth against a future reorder,
        and this asserts the property it actually provides.
        """
        import re
        from mtg.models import _ANTHEM_OWN_ALL_RE
        for qualified in ("werewolf creatures you control get +1/+0",
                          "attacking creatures you control get +1/+0",
                          "other creatures you control get +2/+2"):
            assert re.search(_ANTHEM_OWN_ALL_RE, qualified) is None, (
                f"own_all must not swallow the qualifier in {qualified!r}")
        assert re.search(_ANTHEM_OWN_ALL_RE,
                         "creatures you control get +1/+1") is not None
        assert re.search(
            _ANTHEM_OWN_ALL_RE,
            "...quest counters on it, creatures you control get +5/+5") is not None, (
            "Beastmaster Ascension's comma-preceded clause must still match")

    def test_instigator_gang_declines_rather_than_broadcasting(self):
        """'Attacking creatures you control get +1/+0' cannot be expressed as an
        applies_to string (LayeredPermanent.to_dict carries no attacking key and
        calculate_characteristics is called with game_state=None), so it declines
        with a breadcrumb. Under-applying is the safe direction; the bug being
        fixed is that it buffed the WHOLE BOARD."""
        assert _oracle("instigator gang").lower().startswith(
            "attacking creatures you control get")

        bear = _make_card("Grizzly Bears", power="2", toughness="2")
        game = _make_game()
        rick = game.players[0]
        src = _make_card("Instigator Gang", type_line="Creature — Human Werewolf",
                         oracle_text=_oracle("instigator gang"),
                         power="2", toughness="2")
        rick.battlefield.extend([src, bear])
        game.register_static_pt_effects(src, rick.name)
        game.recalculate_power_toughness()

        assert bear.get_effective_power(game) == 2, (
            f"a non-attacking creature must NOT get the attacking-only buff; "
            f"got {bear.get_effective_power(game)}")
        # The decisive half. Without the decline the clause still reaches the
        # subtype branch and registers applies_to="attackings you control",
        # which matches nobody — so a P/T assertion alone passes either way
        # (it did; the mutant survived). What actually differs is that a junk
        # effect gets added to the engine, where it persists, is iterated on
        # every recalc, and makes the dedup guard skip a later legitimate
        # re-registration of this card.
        assert not [e for e in game.layers_engine.effects
                    if e.id.startswith(f"{src.id}_")], (
            "a combat-state anthem must register NOTHING, not a junk effect "
            "that happens to match no one")
