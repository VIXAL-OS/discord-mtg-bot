"""Pins for the SECOND Aug 10, 2026 deferred-queue session.

Covers the architectural items (A1/A2) and the C/D/E/F/H queue recorded at the
end of the first deferred session. Every premise was re-verified against source
before anything changed, and SIX of the recorded mechanisms turned out wrong or
incomplete -- in four cases the recorded fix would have gone to the wrong file,
or would have made things worse. Those corrections are pinned here rather than
only described in a commit message.

Fixture discipline (the pin-shape-reachability ledger): oracle text comes from
the disk cache, not memory, and every pin drives the entry point the live path
actually calls -- not a helper in isolation, which is how three pins in the
previous session passed while the bug was still live.
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
    """Oracle text from the disk cache, FACES INCLUDED.

    An adventure/DFC half is stored inside its parent's `card_faces` list and
    is NOT a top-level key -- searching only the top level makes a real card
    (Usher to Safety, Heart's Desire) look fabricated. That mistake has been
    made in this repo before; the fallback scan is why it isn't made again.
    """
    with open(_CACHE_PATH, encoding='utf-8') as handle:
        cache = json.load(handle)
    entry = cache.get(name.lower())
    if entry is None:
        for value in cache.values():
            if not isinstance(value, dict):
                continue
            for face in value.get('card_faces') or []:
                if face.get('name', '').lower() == name.lower():
                    return face.get('oracle_text', '') or ''
        raise KeyError(name)
    for face in entry.get('card_faces') or []:
        if face.get('name', '').lower() == name.lower():
            return face.get('oracle_text', '') or ''
    if entry.get('card_faces'):
        return entry['card_faces'][0].get('oracle_text', '') or ''
    return entry.get('oracle_text', '') or ''


def _engine_game():
    """A game with a clientless RulesEngine wired the way production wires it."""
    from mtg.rules_engine import RulesEngine
    game = _make_game()
    rules = RulesEngine(None)
    game._rules_engine = rules
    return game, rules


# ===========================================================================
# A2 -- fogs did not prevent combat damage to CREATURES.
#
# Flag-based prevention was consulted only at the two PLAYER funnels, so under
# a Fog the blockers still died. Two scoping decisions correct the recorded
# mechanism rather than following it, and both are pinned:
#   * allow_static=False for creatures  (Glacial Chasm says "dealt to YOU")
#   * is_combat                          (one flag served Fog AND Teferi's)
# ===========================================================================

class TestFogReachesCreatures:

    def _fog(self, game, rules, **extra):
        from mtg.actions import execute_action_on_state
        action = {"action": "prevent_combat_damage", "scope": "all"}
        action.update(extra)
        execute_action_on_state(rules, game, action)

    def test_a_fog_prevents_combat_damage_to_a_creature(self):
        """The headline. Before this, only players were covered."""
        from mtg.combat import apply_combat_damage_to_creature
        game, rules = _engine_game()
        rick, claude = game.players
        blocker = _make_card("Grizzly Bears", power="2", toughness="2")
        claude.battlefield.append(blocker)
        attacker = _make_card("Hill Giant", power="3", toughness="3")
        rick.battlefield.append(attacker)

        self._fog(game, rules)
        dealt = apply_combat_damage_to_creature(rules, game, blocker, 3, attacker)
        assert dealt == 0 and blocker.damage_marked == 0

    def test_a_fog_does_NOT_prevent_NONcombat_damage_to_a_creature(self):
        """The is_combat half. A Fog prevents COMBAT damage; a burn spell must
        still kill the blocker. One flag used to serve both Fog and Teferi's
        Protection, so extending it to creatures without this distinction would
        have propagated the player-side over-prevention."""
        from mtg.combat import apply_noncombat_damage_to_creature
        game, rules = _engine_game()
        rick, claude = game.players
        blocker = _make_card("Grizzly Bears", power="2", toughness="2")
        claude.battlefield.append(blocker)

        self._fog(game, rules)
        dealt, _ = apply_noncombat_damage_to_creature(
            rules, game, blocker, 3, source_name="Lightning Bolt")
        assert dealt == 3 and blocker.damage_marked == 3

    def test_a_fog_does_NOT_prevent_NONcombat_damage_to_a_PLAYER(self):
        """Same distinction on the player side -- this is the pre-existing
        over-prevention the new flag corrects: a Fog used to blank a burn
        spell to the face because the noncombat player funnel gates on the
        same flag Teferi's Protection sets."""
        from mtg.combat import apply_noncombat_damage_to_player
        game, rules = _engine_game()
        rick, claude = game.players
        before = claude.life

        self._fog(game, rules)
        dealt = apply_noncombat_damage_to_player(
            rules, game, claude, 3, source_name="Lightning Bolt")
        assert dealt == 3 and claude.life == before - 3

    def test_teferis_protection_prevents_NONcombat_damage_to_a_CREATURE(self):
        """The decisive pin for the noncombat CREATURE gate. The fog test
        above passes whether or not that gate exists (a fog must not prevent
        noncombat damage either way), so only an ALL-damage prevention makes
        the gate the deciding factor -- mutation testing caught that."""
        from mtg.combat import apply_noncombat_damage_to_creature
        from mtg.actions import execute_action_on_state
        game, rules = _engine_game()
        _rick, claude = game.players
        bear = _make_card("Grizzly Bears", power="2", toughness="2")
        claude.battlefield.append(bear)
        execute_action_on_state(rules, game, {
            "action": "prevent_all_damage", "player": claude.name})

        dealt, _ = apply_noncombat_damage_to_creature(
            rules, game, bear, 3, source_name="Lightning Bolt")
        assert dealt == 0 and bear.damage_marked == 0

    def test_teferis_protection_still_prevents_NONcombat_damage(self):
        """The control for the test above. Teferi's is all-damage, so the
        distinction must not blank it."""
        from mtg.combat import apply_noncombat_damage_to_player
        from mtg.actions import execute_action_on_state
        game, rules = _engine_game()
        rick, claude = game.players
        execute_action_on_state(rules, game, {
            "action": "prevent_all_damage", "player": claude.name})
        before = claude.life
        dealt = apply_noncombat_damage_to_player(
            rules, game, claude, 3, source_name="Lightning Bolt")
        assert dealt == 0 and claude.life == before

    def test_glacial_chasm_does_NOT_protect_its_controllers_creatures(self):
        """allow_static=False. Glacial Chasm and Solitary Confinement print
        "Prevent all damage that would be dealt to YOU" -- cache-verified, they
        say nothing about creatures. The recorded note guessed the text read
        "...to you and creatures you control"; no printing does. Consulting the
        static from a creature funnel would make the whole board immune."""
        from mtg.combat import apply_combat_damage_to_creature
        chasm_oracle = _oracle("Glacial Chasm")
        assert 'creatures you control' not in chasm_oracle.split(
            'Prevent all damage')[-1], (
            "if a printing ever DOES extend the static to creatures, this "
            "pin is the thing that should fail")

        game, rules = _engine_game()
        rick, claude = game.players
        chasm = _make_card("Glacial Chasm", type_line="Land",
                           oracle_text=chasm_oracle)
        creature = _make_card("Grizzly Bears", power="2", toughness="2")
        claude.battlefield.extend([chasm, creature])
        attacker = _make_card("Hill Giant", power="3", toughness="3")
        rick.battlefield.append(attacker)

        dealt = apply_combat_damage_to_creature(rules, game, creature, 3, attacker)
        assert dealt == 3, "Glacial Chasm protects the PLAYER, not the board"

    def test_glacial_chasm_still_protects_the_player(self):
        """The other direction of the same rule -- the static must keep working
        where it does apply."""
        from mtg.combat import apply_combat_damage_to_player
        game, rules = _engine_game()
        rick, claude = game.players
        claude.battlefield.append(_make_card(
            "Glacial Chasm", type_line="Land", oracle_text=_oracle("Glacial Chasm")))
        attacker = _make_card("Hill Giant", power="3", toughness="3")
        rick.battlefield.append(attacker)
        before = claude.life
        assert apply_combat_damage_to_player(rules, game, claude, 3, attacker) == 0
        assert claude.life == before

    def test_moonmists_werewolf_exemption_survives_at_the_creature_funnel(self):
        """The Aug 10 (G5) exemption must ride along into the new gate, not be
        lost by it."""
        from mtg.combat import apply_combat_damage_to_creature
        game, rules = _engine_game()
        rick, claude = game.players
        victim = _make_card("Grizzly Bears", power="2", toughness="2")
        claude.battlefield.append(victim)
        wolf = _make_card("Howlpack Alpha", type_line="Creature — Werewolf",
                          power="3", toughness="3")
        bear = _make_card("Grizzly Bears", type_line="Creature — Bear",
                          power="3", toughness="3")
        rick.battlefield.extend([wolf, bear])

        self._fog(game, rules, except_subtypes=["Werewolf", "Wolf"])
        assert apply_combat_damage_to_creature(rules, game, victim, 3, bear) == 0
        assert apply_combat_damage_to_creature(rules, game, victim, 3, wolf) == 3, (
            "a Werewolf is EXEMPT from Moonmist's own fog")


# ===========================================================================
# C3 -- "becomes untapped" (Mesmeric Orb), and the dead _skip_next_untap.
#
# The recorded fix location was dead on arrival: it proposed using untap_all's
# `was_tapped` local, but RulesEngine.on_untap_step runs immediately BEFORE
# untap_all and blindly untapped everything, so that local reads False for
# every card on every real invocation.
# ===========================================================================

class TestBecomesUntapped:

    def test_mesmeric_orb_mills_on_the_untap_step(self):
        """Drives the REAL untap-step routine, not untap_all."""
        game, rules = _engine_game()
        rick, claude = game.players
        game.active_player_index = 0
        orb = _make_card("Mesmeric Orb", type_line="Artifact",
                         oracle_text=_oracle("Mesmeric Orb"))
        claude.battlefield.append(orb)
        land = _make_card("Forest", type_line="Land", tapped=True)
        rick.battlefield.append(land)
        rick.library.extend(_make_card(f"L{i}") for i in range(5))

        rules.on_untap_step(game)
        assert not land.tapped
        assert len(rick.graveyard) == 1, (
            "the UNTAPPED permanent's controller mills, not the watcher's")

    def test_an_already_untapped_permanent_does_not_become_untapped(self):
        """The transition check is the whole point -- a blind loop cannot tell
        an already-untapped permanent from one that just became untapped."""
        game, rules = _engine_game()
        rick, claude = game.players
        game.active_player_index = 0
        claude.battlefield.append(_make_card(
            "Mesmeric Orb", type_line="Artifact", oracle_text=_oracle("Mesmeric Orb")))
        rick.battlefield.append(_make_card("Forest", type_line="Land", tapped=False))
        rick.library.extend(_make_card(f"L{i}") for i in range(5))

        rules.on_untap_step(game)
        assert rick.graveyard == []

    def test_skip_next_untap_is_honoured_at_the_untap_step(self):
        """Icebreaker Kraken / Frozen Aether. This flag was checked ONLY in
        untap_all, which runs AFTER on_untap_step had already untapped the
        permanent -- so the mechanic was silently dead in the only path that
        runs it."""
        game, rules = _engine_game()
        rick, _ = game.players
        game.active_player_index = 0
        land = _make_card("Forest", type_line="Land", tapped=True)
        land._skip_next_untap = True
        rick.battlefield.append(land)

        rules.on_untap_step(game)
        assert land.tapped, "the skip must survive on_untap_step's own loop"
        assert land._skip_next_untap is False, "and clear so it untaps next turn"

    def test_a_skipped_permanent_does_not_fire_becomes_untapped(self):
        """It never became untapped, so the watcher must not see it."""
        game, rules = _engine_game()
        rick, claude = game.players
        game.active_player_index = 0
        claude.battlefield.append(_make_card(
            "Mesmeric Orb", type_line="Artifact", oracle_text=_oracle("Mesmeric Orb")))
        land = _make_card("Forest", type_line="Land", tapped=True)
        land._skip_next_untap = True
        rick.battlefield.append(land)
        rick.library.extend(_make_card(f"L{i}") for i in range(5))

        rules.on_untap_step(game)
        assert rick.graveyard == []


# ===========================================================================
# C2 -- "whenever <someone> loses life" (Mindcrank).
# ===========================================================================

class TestLosesLifeTriggers:

    def test_mindcrank_mills_on_an_opponents_life_loss(self):
        from mtg.actions import execute_action_on_state
        game, rules = _engine_game()
        rick, claude = game.players
        rick.battlefield.append(_make_card(
            "Mindcrank", type_line="Artifact", oracle_text=_oracle("Mindcrank")))
        claude.library.extend(_make_card(f"L{i}") for i in range(10))

        execute_action_on_state(rules, game, {
            "action": "lose_life", "player": claude.name, "amount": 3})
        assert len(claude.graveyard) == 3, "mills THAT MANY"

    def test_mindcrank_does_not_fire_on_its_own_controllers_life_loss(self):
        """"Whenever an OPPONENT loses life" is scoped relative to the
        watcher's controller, not to whoever lost life."""
        from mtg.actions import execute_action_on_state
        game, rules = _engine_game()
        rick, claude = game.players
        rick.battlefield.append(_make_card(
            "Mindcrank", type_line="Artifact", oracle_text=_oracle("Mindcrank")))
        rick.library.extend(_make_card(f"L{i}") for i in range(10))

        execute_action_on_state(rules, game, {
            "action": "lose_life", "player": rick.name, "amount": 3})
        assert rick.graveyard == []

    def test_damage_causes_loss_of_life_so_mindcrank_sees_it(self):
        """Mindcrank's own printed reminder says so, which is why the emit sits
        at record_life_loss (where damage and non-damage loss converge) rather
        than at a damage funnel."""
        from mtg.combat import apply_combat_damage_to_player
        assert 'Damage causes loss of life' in _oracle("Mindcrank")
        game, rules = _engine_game()
        rick, claude = game.players
        rick.battlefield.append(_make_card(
            "Mindcrank", type_line="Artifact", oracle_text=_oracle("Mindcrank")))
        claude.library.extend(_make_card(f"L{i}") for i in range(10))
        attacker = _make_card("Hill Giant", power="4", toughness="4")
        rick.battlefield.append(attacker)

        apply_combat_damage_to_player(rules, game, claude, 4, attacker)
        assert len(claude.graveyard) == 4


# ===========================================================================
# D -- Tier-3 damage authority + move_card deaths reaching the bus.
# ===========================================================================

class TestTier3DamageAuthority:

    def test_move_card_battlefield_to_graveyard_queues_the_death(self):
        """The class fix. move_card never queued a death, so a creature that
        died through it was INVISIBLE to the CREATURE_DIED bus and every dies
        trigger was skipped."""
        from mtg.actions import execute_action_on_state
        game, rules = _engine_game()
        rick, claude = game.players
        victim = _make_card("Grizzly Bears", power="2", toughness="2")
        claude.battlefield.append(victim)

        execute_action_on_state(rules, game, {
            "action": "move_card", "card": victim.name,
            "from_zone": "battlefield", "to_zone": "graveyard",
            "player": claude.name})
        assert victim in claude.graveyard
        assert any(c is victim for c, _ in game._recently_died), (
            "the death must reach the dies queue")

    def test_a_noncreature_moved_to_the_graveyard_does_not_queue_a_death(self):
        """The is_creature() gate -- Marit Lage's Slumber sacrificing itself is
        the one shipped caller of this shape and must not queue."""
        from mtg.actions import execute_action_on_state
        game, rules = _engine_game()
        rick, claude = game.players
        ench = _make_card("Marit Lage's Slumber", type_line="Enchantment — Snow",
                          power=None, toughness=None)
        claude.battlefield.append(ench)

        execute_action_on_state(rules, game, {
            "action": "move_card", "card": ench.name,
            "from_zone": "battlefield", "to_zone": "graveyard",
            "player": claude.name})
        assert game._recently_died == []

    def test_a_damage_only_effect_may_not_emit_a_direct_kill(self):
        """CR 704.5g -- lethality is state-based actions' call, not the
        resolver's. Drives the production regex object, not a copy of it."""
        from mtg.judge import _DAMAGE_ONLY_EFFECT
        assert _DAMAGE_ONLY_EFFECT.match(
            "Goblin Bombardment deals 1 damage to any target.")
        assert _DAMAGE_ONLY_EFFECT.match("deals 3 damage to target creature")

    def test_a_compound_ability_is_not_refused(self):
        """The deliberate under-refusal. A damage clause PLUS a real destroy
        clause does not full-match, so it passes ungoverned rather than being
        wrongly blocked -- the safe direction."""
        from mtg.judge import _DAMAGE_ONLY_EFFECT
        assert not _DAMAGE_ONLY_EFFECT.match(
            "Deals 2 damage to target creature. Destroy target enchantment.")
        assert not _DAMAGE_ONLY_EFFECT.match("Destroy target creature.")


# ===========================================================================
# A1-adjacent -- the THIRD target-selection path.
#
# Found while verifying A1 and separable from it: mtg/cog.py's `!activate`
# Tier-2 fallback built its own ExecutionContext with a cruder inline regex
# that captured only the target TYPE, discarded any controller qualifier, and
# then scanned the OPPONENT's battlefield unconditionally -- so an activated
# ability reading "target creature YOU CONTROL" always pointed at the
# opponent's creature. 'planeswalker' was captured by that regex but had NO
# branch, so those abilities silently got zero targets.
#
# The full A1 per-clause attribution refactor stays deferred; this is the
# independent live bug inside its blast radius, closed by making the two paths
# share ONE parser instead of three.
# ===========================================================================

class TestSharedTargetRestrictionParser:

    def test_you_control_is_not_flattened_to_the_opponent(self):
        from rules.spell_resolver import target_restrictions_for_text
        from rules.targeting import ControllerRestriction, TargetType
        restrictions = target_restrictions_for_text(
            "Untap target creature you control.")
        assert len(restrictions) == 1
        assert restrictions[0].controller == ControllerRestriction.YOU
        assert TargetType.CREATURE in restrictions[0].target_types

    def test_an_opponent_controls_is_read_as_opponent(self):
        from rules.spell_resolver import target_restrictions_for_text
        from rules.targeting import ControllerRestriction
        restrictions = target_restrictions_for_text(
            "Destroy target creature an opponent controls.")
        assert restrictions[0].controller == ControllerRestriction.OPPONENT

    def test_an_unqualified_target_stays_unrestricted(self):
        """The control -- adding controller handling must not invent a
        restriction the card does not print."""
        from rules.spell_resolver import target_restrictions_for_text
        from rules.targeting import ControllerRestriction
        restrictions = target_restrictions_for_text(
            "Target creature gets +2/+2 until end of turn.")
        assert restrictions[0].controller == ControllerRestriction.ANY

    def test_planeswalker_is_a_recognised_target_type(self):
        """The old inline regex captured 'planeswalker' and then had no branch
        for it, so the ability resolved with zero targets."""
        from rules.spell_resolver import target_restrictions_for_text
        from rules.targeting import TargetType
        restrictions = target_restrictions_for_text(
            "Deal 3 damage to target planeswalker.")
        assert TargetType.PLANESWALKER in restrictions[0].target_types

    def test_one_printed_target_yields_exactly_one_restriction(self):
        """The Aug-9 B-4 overlapping-span dedup rides along with the
        extraction: "target creature you control" matches BOTH the bare and
        the qualified pattern, and resolving it twice put a counter on the
        opponent's same-named creature."""
        from rules.spell_resolver import target_restrictions_for_text
        assert len(target_restrictions_for_text(
            "Put a +1/+1 counter on target creature you control.")) == 1

    def test_you_control_picks_the_ACTIVATORS_creature(self):
        """The decisive pin for the CONSUMER, not the parser. Mutation testing
        showed the parser-level pins above passing while the picking logic was
        still opponent-only -- a helper pinned only through direct calls is not
        pinned into production, so the picker was extracted out of the async
        Discord handler to be drivable at all."""
        from mtg.helpers import pick_targets_for_restrictions
        from rules.spell_resolver import target_restrictions_for_text
        game, _ = _engine_game()
        rick, claude = game.players
        mine = _make_card("My Bear", power="2", toughness="2")
        theirs = _make_card("Their Bear", power="2", toughness="2")
        rick.battlefield.append(mine)
        claude.battlefield.append(theirs)

        picked, missed = pick_targets_for_restrictions(
            game, rick, claude,
            target_restrictions_for_text("Untap target creature you control."))
        assert not missed
        assert [c.name for c in picked] == ["My Bear"]

    def test_an_unqualified_target_still_prefers_the_opponent(self):
        """The control -- honouring "you control" must not flip the default."""
        from mtg.helpers import pick_targets_for_restrictions
        from rules.spell_resolver import target_restrictions_for_text
        game, _ = _engine_game()
        rick, claude = game.players
        rick.battlefield.append(_make_card("My Bear", power="2", toughness="2"))
        claude.battlefield.append(_make_card("Their Bear", power="2", toughness="2"))

        picked, _ = pick_targets_for_restrictions(
            game, rick, claude,
            target_restrictions_for_text("Destroy target creature."))
        assert [c.name for c in picked] == ["Their Bear"]

    def test_a_planeswalker_target_is_actually_picked(self):
        """The old inline regex captured 'planeswalker' with no branch, so the
        ability resolved with ZERO targets.

        A DECOY creature sits FIRST on the same battlefield deliberately: with
        the planeswalker as the only permanent, an unfiltered pick grabs it
        anyway and the pin passes with the type gate deleted. Mutation testing
        caught exactly that."""
        from mtg.helpers import pick_targets_for_restrictions
        from rules.spell_resolver import target_restrictions_for_text
        game, _ = _engine_game()
        rick, claude = game.players
        claude.battlefield.append(
            _make_card("Decoy Bear", power="2", toughness="2"))
        walker = _make_card("Jace, the Mind Sculptor",
                            type_line="Legendary Planeswalker — Jace",
                            power=None, toughness=None)
        claude.battlefield.append(walker)

        picked, missed = pick_targets_for_restrictions(
            game, rick, claude,
            target_restrictions_for_text("Deal 3 damage to target planeswalker."))
        assert not missed and [c.name for c in picked] == ["Jace, the Mind Sculptor"]

    def test_no_legal_target_is_reported_not_silently_dropped(self):
        from mtg.helpers import pick_targets_for_restrictions
        from rules.spell_resolver import target_restrictions_for_text
        game, _ = _engine_game()
        rick, claude = game.players
        picked, missed = pick_targets_for_restrictions(
            game, rick, claude,
            target_restrictions_for_text("Untap target creature you control."))
        assert picked == [] and len(missed) == 1

    def test_the_activate_path_no_longer_has_its_own_target_regex(self):
        """Structural: three independent parses of the same text is how the
        controller filter went missing in the first place. Behavioural pins
        above cover the parser; this covers the de-duplication."""
        import mtg.cog as cog_mod
        src = open(cog_mod.__file__, encoding='utf-8').read()
        assert "target_restrictions_for_text" in src
        assert "r'target (creature|permanent|player|planeswalker" not in src


# ===========================================================================
# H1 -- mana-tap damage, scoped PER ABILITY LINE.
# ===========================================================================

class TestManaTapDamage:

    def _card(self, name):
        entry = json.load(open(_CACHE_PATH, encoding='utf-8'))[name.lower()]
        return _make_card(name, type_line=entry.get('type_line', ''),
                          oracle_text=entry.get('oracle_text', '') or '',
                          power=None, toughness=None)

    def _player(self):
        from mtg.models import Player
        return Player(name="Rick", user_id=99999, life=40)

    def test_the_free_colorless_line_is_not_charged(self):
        """Talisman of Impulse prints "{T}: Add {C}." and "{T}: Add {R} or {G}.
        This artifact deals 1 damage to you." on SEPARATE lines."""
        player = self._player()
        talisman = self._card("Talisman of Impulse")
        assert '{T}: Add {C}.' in talisman.oracle_text
        first_line = talisman.oracle_text.split('\n')[0]
        assert 'damage' not in first_line.lower()
        assert player._get_mana_tap_damage(talisman) == 1

    def test_a_damage_clause_outside_a_mana_ability_is_not_charged(self):
        """The DECISIVE pin for per-line scoping, which the Talisman case is
        not: the Talisman's free line carries no damage text at all, so it
        returns the right answer even under a whole-oracle scan (mutation
        testing caught that the obvious pin passed both ways). What the line
        scoping actually protects against is a damage clause that belongs to
        something OTHER than a mana ability -- here an attack trigger on a
        creature that also taps for mana. Charging it at tap time would be
        damage from nowhere."""
        player = self._player()
        dork = _make_card(
            "Painful Mana Dork", type_line="Creature — Elf Druid",
            power="1", toughness="1",
            oracle_text="{T}: Add {G}.\n"
                        "Whenever this creature attacks, it deals 2 damage to you.")
        assert player._get_mana_tap_damage(dork) == 0

    def test_the_uncovered_pain_lands_and_talismans_now_charge(self):
        """8 pain lands + 8 Talismans tapped for free under the old
        four-name hardcoded list; these are in 8+ decks."""
        player = self._player()
        for name in ("Battlefield Forge", "Adarkar Wastes",
                     "Talisman of Progress"):
            assert player._get_mana_tap_damage(self._card(name)) == 1, name

    def test_ancient_tomb_and_city_of_brass_still_charge(self):
        """Regression guard on the two survivors of the old list. City of
        Brass's damage is a BECOMES-TAPPED trigger rather than part of the
        mana ability, which is why the scan accepts that shape too."""
        player = self._player()
        assert player._get_mana_tap_damage(self._card("Ancient Tomb")) == 2
        assert player._get_mana_tap_damage(self._card("City of Brass")) == 1

    def test_mana_confluence_is_a_LIFE_COST_not_damage(self):
        """The old hardcoded list had this wrong. Mana Confluence prints
        "{T}, Pay 1 life:" -- a cost, which cannot be prevented, doubled or
        redirected. Now that protection and Torbran both read damage, filing a
        payment as damage is a real mislabel, not a wording nit."""
        player = self._player()
        confluence = self._card("Mana Confluence")
        assert 'Pay 1 life' in confluence.oracle_text
        assert 'damage' not in confluence.oracle_text.lower()
        assert player._get_mana_tap_damage(confluence) == 0
        assert player._get_mana_tap_life_cost(confluence) == 1

    def test_an_ordinary_mana_source_is_free(self):
        """The control -- the oracle path must not start taxing Signets."""
        player = self._player()
        for name in ("Boros Signet", "Forest"):
            card = self._card(name)
            assert player._get_mana_tap_damage(card) == 0, name
            assert player._get_mana_tap_life_cost(card) == 0, name


# ===========================================================================
# F2 -- Sunforger's "Unattach this Equipment" cost.
#
# ORDERING IS LOAD-BEARING and is why only the COST shipped: Sunforger today is
# a dead card that burns {R}{W}, and adding the tutor-and-free-cast effect
# WITHOUT this cost would turn it into a repeatable free-instant engine, once
# per turn, forever.
# ===========================================================================

class TestUnattachCost:

    def test_sunforger_prints_unattach_as_part_of_the_cost(self):
        """Read from the cache. The cost is pre-colon, so it is a COST, not an
        effect -- which is what makes dropping it a free-engine bug."""
        oracle = _oracle("Sunforger")
        cost = oracle.split(':', 1)[0]
        assert 'Unattach this Equipment' in cost

    def test_unattaching_clears_both_sides_of_the_link(self):
        """Mirrors the two-field mutation the CR 704.5n SBA detach performs."""
        from mtg.helpers import unattach_equipment
        game, _ = _engine_game()
        rick, _c = game.players
        bearer = _make_card("Grizzly Bears", power="2", toughness="2")
        sunforger = _make_card("Sunforger", type_line="Artifact — Equipment",
                               power=None, toughness=None)
        sunforger.attached_to = bearer.id
        bearer.attachments = [sunforger.id]
        rick.battlefield.extend([bearer, sunforger])

        assert unattach_equipment(game, sunforger) is True
        assert sunforger.attached_to is None
        assert sunforger.id not in bearer.attachments

    def test_unattaching_an_unattached_equipment_is_a_no_op(self):
        """The cost cannot be paid, so nothing may be mutated."""
        from mtg.helpers import unattach_equipment
        game, _ = _engine_game()
        rick, _c = game.players
        sunforger = _make_card("Sunforger", type_line="Artifact — Equipment",
                               power=None, toughness=None)
        rick.battlefield.append(sunforger)
        assert unattach_equipment(game, sunforger) is False

    def test_both_activation_paths_know_the_keyword(self):
        """The two-activation-paths divergence class: 'unattach' was in NEITHER
        path's cost vocabulary, which is why the clause was silently dropped.
        Structural, because the alternative is driving a Discord command."""
        import mtg.engine as engine_mod
        import mtg.cog as cog_mod
        for mod in (engine_mod, cog_mod):
            src = open(mod.__file__, encoding='utf-8').read()
            assert "'unattach' in cost" in src, (
                f"{mod.__name__} must recognise the unattach cost")


# ===========================================================================
# C1 -- the opponent-draw family.
#
# The recorded headline ("no dispatcher at all") was wrong in the way this
# codebase keeps finding: a two-card hardcoded elif chain with NO fallthrough,
# so Smothering Tithe and Consecrated Sphinx worked and every other card in the
# family was silently inert.
# ===========================================================================

class TestOpponentDrawTriggers:

    def _engine(self):
        from mtg.engine import GameEngine
        from mtg.rules_engine import RulesEngine
        game = _make_game()
        rules = RulesEngine(None)
        engine = GameEngine.__new__(GameEngine)
        engine.rules = rules
        rules.engine_ref = engine
        game._rules_engine = rules
        return engine, game

    def test_sheoldred_drains_the_drawing_opponent(self):
        engine, game = self._engine()
        rick, claude = game.players
        claude.battlefield.append(_make_card(
            "Sheoldred, the Apocalypse",
            type_line="Legendary Creature — Phyrexian Praetor",
            power="4", toughness="5",
            oracle_text=_oracle("Sheoldred, the Apocalypse")))
        rick.library.extend(_make_card(f"L{i}") for i in range(5))
        before = rick.life

        engine.draw_cards(rick, 1, game=game)
        assert rick.life == before - 2

    def test_sheoldred_scales_with_the_number_of_cards_drawn(self):
        """The batching risk: the dispatcher fires ONCE per draw_cards call, so
        a multi-card draw must multiply. A three-card draw is 6 life, not 2."""
        engine, game = self._engine()
        rick, claude = game.players
        claude.battlefield.append(_make_card(
            "Sheoldred, the Apocalypse",
            type_line="Legendary Creature — Phyrexian Praetor",
            power="4", toughness="5",
            oracle_text=_oracle("Sheoldred, the Apocalypse")))
        rick.library.extend(_make_card(f"L{i}") for i in range(9))
        before = rick.life

        engine.draw_cards(rick, 3, game=game)
        assert rick.life == before - 6

    def test_sheoldreds_two_clauses_stay_on_their_own_scopes(self):
        """Sheoldred prints BOTH halves of this family: "Whenever you draw a
        card, you gain 2 life" (handled by fire_draw_triggers, the "you" scope)
        and "Whenever an opponent draws a card, they lose 2 life" (the new
        opponent scope). Its controller drawing must take the GAIN and not the
        drain -- which makes this the decisive pin that the two scopes did not
        bleed into each other."""
        engine, game = self._engine()
        rick, claude = game.players
        claude.battlefield.append(_make_card(
            "Sheoldred, the Apocalypse",
            type_line="Legendary Creature — Phyrexian Praetor",
            power="4", toughness="5",
            oracle_text=_oracle("Sheoldred, the Apocalypse")))
        claude.library.extend(_make_card(f"L{i}") for i in range(5))
        before = claude.life

        engine.draw_cards(claude, 1, game=game)
        assert claude.life == before + 2, (
            "the you-scope gain fires; the opponent-scope drain must not")

    def test_the_existing_two_hardcoded_cards_still_work(self):
        """The regression guard on the fallthrough: adding an `else` must not
        steal the branches that already worked."""
        engine, game = self._engine()
        rick, claude = game.players
        claude.battlefield.append(_make_card(
            "Consecrated Sphinx", type_line="Creature — Sphinx",
            power="4", toughness="6",
            oracle_text=_oracle("Consecrated Sphinx")))
        rick.library.extend(_make_card(f"R{i}") for i in range(5))
        claude.library.extend(_make_card(f"C{i}") for i in range(5))

        engine.draw_cards(rick, 1, game=game)
        assert len(claude.hand) == 2, "Sphinx draws two off one opponent draw"


# ===========================================================================
# E2 -- protection beyond TARGETING (CR 702.16c blocking, 702.16e damage).
#
# The recorded item cited CR 509.1b; the operative rule is 702.16c and the
# DIRECTION is the correction: Akroma was ATTACKING and the mono-black Butcher
# was BLOCKING, so it is the ATTACKER's protection tested against the BLOCKER's
# qualities -- the opposite of the natural reading, which would have put the
# check on the wrong card.
# ===========================================================================

class TestProtectionBeyondTargeting:

    def _akroma(self):
        return _make_card(
            "Akroma, Angel of Wrath", type_line="Legendary Creature — Angel",
            power="6", toughness="6", mana_cost="{5}{W}{W}{W}", cmc=8,
            oracle_text=_oracle("Akroma, Angel of Wrath"))

    def test_akromas_printed_protection_is_from_black_and_red(self):
        """Read from the cache, not memory. The Aug-10 A5 fix exists because
        the old regex captured 'white and from' and dropped the second colour;
        this pin is the guard on that parse for the card that motivated it."""
        oracle = _oracle("Akroma, Angel of Wrath").lower()
        assert 'protection from black' in oracle and 'from red' in oracle

    def test_a_black_creature_cannot_block_akroma(self):
        """CR 702.16c. Drives Card.can_block, the real declaration gate."""
        game, _ = _engine_game()
        rick, claude = game.players
        akroma = self._akroma()
        rick.battlefield.append(akroma)
        butcher = _make_card("Butcher of Malakir",
                             type_line="Creature — Vampire Warrior",
                             power="5", toughness="4", mana_cost="{6}{B}{B}")
        claude.battlefield.append(butcher)

        assert butcher.can_block(attacker=akroma, game=game) is False

    def test_a_white_creature_can_still_block_akroma(self):
        """The control. Protection is from black and red only."""
        game, _ = _engine_game()
        rick, claude = game.players
        akroma = self._akroma()
        rick.battlefield.append(akroma)
        knight = _make_card("Serra Angel", type_line="Creature — Angel",
                            power="4", toughness="4", mana_cost="{3}{W}{W}")
        knight.keywords = ["Flying"]
        claude.battlefield.append(knight)

        assert knight.can_block(attacker=akroma, game=game) is True

    def test_the_direction_is_attacker_protection_not_blocker(self):
        """The correction, pinned. A BLACK attacker is not stopped by the
        blocker having protection from black in the other direction -- that
        would be the mistake the recorded CR citation invited."""
        game, _ = _engine_game()
        rick, claude = game.players
        black_attacker = _make_card("Butcher of Malakir",
                                    type_line="Creature — Vampire",
                                    power="5", toughness="4", mana_cost="{6}{B}{B}")
        rick.battlefield.append(black_attacker)
        akroma = self._akroma()
        claude.battlefield.append(akroma)
        # Akroma CAN block a black creature; protection stops her being
        # blocked BY black, not her blocking black.
        assert akroma.can_block(attacker=black_attacker, game=game) is True

    def test_a_red_board_wipe_does_not_damage_akroma(self):
        """CR 702.16e, and the exact live shape: Akroma was killed by a RED
        Blasphemous Act. The source has already left the stack when its damage
        applies, so colours must resolve by name across zones."""
        from mtg.combat import apply_noncombat_damage_to_creature
        game, _ = _engine_game()
        rick, claude = game.players
        akroma = self._akroma()
        claude.battlefield.append(akroma)
        wipe = _make_card("Blasphemous Act", type_line="Sorcery",
                          mana_cost="{8}{R}", cmc=9, power=None, toughness=None)
        rick.graveyard.append(wipe)

        dealt, _ = apply_noncombat_damage_to_creature(
            rules=game._rules_engine, game=game, creature=akroma, amount=13,
            source_name="Blasphemous Act")
        assert dealt == 0 and akroma.damage_marked == 0

    def test_an_unprotected_creature_still_takes_that_damage(self):
        """The control for the test above -- the gate must not blank damage
        generally."""
        from mtg.combat import apply_noncombat_damage_to_creature
        game, _ = _engine_game()
        rick, claude = game.players
        bear = _make_card("Grizzly Bears", power="2", toughness="2")
        claude.battlefield.append(bear)
        rick.graveyard.append(_make_card(
            "Blasphemous Act", type_line="Sorcery", mana_cost="{8}{R}",
            cmc=9, power=None, toughness=None))

        dealt, _ = apply_noncombat_damage_to_creature(
            rules=game._rules_engine, game=game, creature=bear, amount=13,
            source_name="Blasphemous Act")
        assert dealt == 13


# ===========================================================================
# F1 -- SELF cost reduction written out longhand (Blasphemous Act).
#
# The recorded mechanism named a true property that was NOT the cause: the
# battlefield-only scan and the `perm is card` skip are real, but the regex is
# anchored on "spell(s) you cast" and cannot match "this spell costs" from any
# zone -- and a match would have given a flat -1 rather than -N-per-thing.
# ===========================================================================

class TestSelfCostReduction:

    def _card(self, name):
        entry = json.load(open(_CACHE_PATH, encoding='utf-8'))[name.lower()]
        return _make_card(name, type_line=entry.get('type_line', ''),
                          oracle_text=entry.get('oracle_text', '') or '',
                          mana_cost=entry.get('mana_cost', ''),
                          cmc=entry.get('cmc', 0), power=None, toughness=None)

    def test_blasphemous_act_counts_every_creature_on_the_battlefield(self):
        from mtg.helpers import compute_self_cost_reduction
        game, _ = _engine_game()
        rick, claude = game.players
        for i in range(3):
            rick.battlefield.append(_make_card(f"Bear{i}"))
        for i in range(2):
            claude.battlefield.append(_make_card(f"Ogre{i}"))

        amount, domain = compute_self_cost_reduction(
            game, rick, self._card("Blasphemous Act"))
        assert amount == 5, "both battlefields, not just the caster's"
        assert 'creature on the battlefield' in domain

    def test_icebreaker_kraken_does_not_double_apply(self):
        """The trap. Kraken prints "Affinity for snow lands (This spell costs
        {1} less to cast for each snow land you control.)" -- the IDENTICAL
        shape, inside a parenthetical, already fully handled by
        compute_affinity_reduction.

        HONEST SCOPE (mutation testing established this rather than assuming
        it): for the CURRENT registry the strip_reminder_text call is
        defence-in-depth, not the load-bearing guard -- removing it still
        yields (0, None), because the registry independently refuses the
        affinity domains ("snow land you control" is not modelled). What this
        pin actually proves is the PROPERTY -- Kraken never double-applies --
        which is what matters and which holds by either mechanism. The strip
        stays because a future registry entry that collides with an affinity
        domain would double-apply without it, and the guard costs nothing."""
        from mtg.helpers import compute_self_cost_reduction, parse_affinity
        game, _ = _engine_game()
        rick, _c = game.players
        kraken = self._card("Icebreaker Kraken")
        assert parse_affinity(kraken.oracle_text), "affinity owns this card"
        for i in range(4):
            rick.battlefield.append(_make_card(
                f"Snow{i}", type_line="Snow Land — Island", power=None, toughness=None))

        assert compute_self_cost_reduction(game, rick, kraken) == (0, None)

    def test_an_unmodelled_counting_domain_is_refused_not_guessed(self):
        """Over-applying a discount to a cost the player then cannot pay is
        worse than not applying it, so the registry refuses what it does not
        know (the July 26 cost-reduction precedent)."""
        from mtg.helpers import compute_self_cost_reduction
        game, _ = _engine_game()
        rick, _c = game.players
        weird = _make_card(
            "Made Up Spell", type_line="Sorcery", power=None, toughness=None,
            oracle_text="This spell costs {1} less to cast for each Gate you "
                        "control that entered under a full moon.")
        assert compute_self_cost_reduction(game, rick, weird) == (0, None)

    def test_a_devotion_gated_god_below_threshold_is_not_counted(self):
        """is_creature(game=game), not bare is_creature() -- the documented D4
        class. A bare call would count Erebos as a creature at devotion 2."""
        from mtg.helpers import compute_self_cost_reduction
        game, _ = _engine_game()
        rick, _c = game.players
        erebos = _make_card(
            "Erebos, God of the Dead", type_line="Legendary Enchantment Creature — God",
            power="5", toughness="7",
            oracle_text="Indestructible\nAs long as your devotion to black is "
                        "less than five, Erebos isn't a creature.")
        rick.battlefield.append(erebos)
        amount, _ = compute_self_cost_reduction(
            game, rick, self._card("Blasphemous Act"))
        assert amount == 0, "devotion 0 — Erebos is not a creature"


# ===========================================================================
# H3 -- Usher to Safety / Heart's Desire.
# ===========================================================================

class TestUsherToSafety:

    def test_printed_text_is_permanent_not_creature(self):
        """It lives in card_faces (it is an adventure half), which is why a
        top-level name search makes it look absent."""
        assert 'target permanent you control' in _oracle("Usher to Safety").lower()

    def test_usher_returns_a_noncreature_permanent(self):
        """The narrowing was in the TEMPLATE. bounce_own_permanent is already
        permanent-general, so patching the handler would have been a no-op."""
        from rules.effect_templates import get_effect_library, build_game_context
        game, rules = _engine_game()
        rick, claude = game.players
        signet = _make_card("Boros Signet", type_line="Artifact",
                            power=None, toughness=None)
        rick.battlefield.append(signet)

        lib = get_effect_library()
        ctx = build_game_context(game, rick, claude)
        actions, _ = lib.resolve_spell(
            "Usher to Safety", _oracle("Usher to Safety"), rick.name, claude.name, ctx)
        assert actions and actions[0]["action"] == "bounce_own_permanent"
        assert actions[0]["card"] == "Boros Signet"

    def test_hearts_desire_token_is_a_white_human(self):
        """Printed: "Create a 1/1 white Human creature token" -- nameless, and
        white. The template named it after the adventure and set no colour."""
        with open(os.path.join(os.path.dirname(_CACHE_PATH),
                               'card_templates.json'), encoding='utf-8') as fh:
            data = json.load(fh)
        entry = next(e for e in data['templates'] if e['key'] == "heart's desire")
        action = entry['actions'][0]
        assert action['name'] == 'Human'
        assert action['colors'] == 'W'
        assert 'white Human creature token' in _oracle("Heart's Desire")


# ===========================================================================
# H4 -- the planeswalker/activation failure reason.
#
# Of the FOUR unstashed exits (the record said three), only _validate_activation
# lacks a re-derivation. The other three are deliberately left alone, and the
# second pin below is why: a stash is consumed BEFORE the re-derivation and
# WINS, so stashing the PW-index exit would have replaced its richer message.
# ===========================================================================

class TestActivationFailureReason:

    def test_validate_activation_reason_reaches_the_model(self):
        from mtg.ai_turn import _get_action_error

        class _Eng:
            planeswalker_manager = None
        game, rules = _engine_game()
        rick, _ = game.players
        perm = _make_card("Rhys the Redeemed", type_line="Creature — Elf",
                          oracle_text="{2}{G/W}, {T}: Create a 1/1 Elf token.")
        rick.battlefield.append(perm)
        game._last_activation_failure = (
            game.turn_number, perm.name,
            "Can only activate as a sorcery (stack must be empty)")

        msg = _get_action_error(_Eng(), game, 0,
                                {"type": "activate", "permanent": perm.name})
        assert msg == "Can only activate as a sorcery (stack must be empty)"

    def test_the_pw_index_message_is_not_clobbered_by_a_stash(self):
        """The mechanism correction. _get_action_error re-derives the PW
        ability-index refusal with STRICTLY RICHER text (every valid index and
        its loyalty cost -- the June 10 Ashiok retry-deadlock fix). Because a
        stash wins over the re-derivation, stashing that exit would have made
        the message worse, so it is deliberately left unstashed."""
        import re as _re
        import mtg.engine as engine_mod

        src = open(engine_mod.__file__, encoding='utf-8').read()
        block = src.split("# Handle planeswalker abilities", 1)[1][:2000]
        idx_refusal = block.split("out of range", 1)[1].split("return None", 1)[0]
        assert '_last_activation_failure' not in idx_refusal, (
            "stashing here would clobber _get_action_error's richer message")
        assert _re.search(r'loyalty cost', src), (
            "the richer re-derivation must still exist to be worth protecting")
