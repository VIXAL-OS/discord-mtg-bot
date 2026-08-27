"""Pins for the Aug 27, 2026 f7b74e8 batch audit (15 fixes, 4 reviewers).

Ledger: A1 target-phrase predicate cut; A2 Nighthawk "1+*" CDA; B1 Aluren
vs adventure halves (CR 715.3); B2 Oaken Boon declared target; B3 the
graveyard-spell type gate (CR 601.2c); C1 Robber's silent-no-op sentinel;
C2 the "nontoken" enters qualifier; C3 resolve_cast_target's trigger-clause
graveyard misread; C4 Nevermaker owner-routing; D1 stale attachments across
re-entry (CR 400.7); D3/D4 Extraction Specialist (combat lock + declared
target); D5 Shalai's list-form player hexproof; D6 Chromatic Lantern's land
grant; D7 Zhulodok's static cascade grant.

Oracle constants are bulk-verified verbatim. Fixtures drive the REAL
production paths (the pin-shape lesson): the player-target validator, the
cast gate, the action interpreter, the enters scan, the entry-funnel bus.
"""

import pytest

from mtg.engine import GameEngine
from mtg.helpers import is_aluren_free_cast, resolve_cast_target
from mtg.spells import _validate_cast
from mtg.triggers import (
    _check_creature_etb_triggers_sync, static_cascade_grant_count,
)
from rules.effect_templates import get_effect_library
from rules.targeting_helpers import (
    _parse_target_restriction_from_oracle, _player_to_targetable,
    _validate_player_target_for_action,
)


MIND_SLUDGE = "Target player discards a card for each Swamp you control."
SMITE = "Destroy target creature with power 4 or greater."
NIGHTHAWK = ("Flying, deathtouch, lifelink\nNighthawk Scavenger's power is "
             "equal to 1 plus the number of card types among cards in your "
             "opponents' graveyards.")
SOUL_OF_HARVEST = ("Trample\nWhenever another nontoken creature you control "
                   "enters, you may draw a card.")
SHALAI = ("Flying\nYou, planeswalkers you control, and other creatures you "
          "control have hexproof.\n{4}{G}{G}: Put a +1/+1 counter on each "
          "creature you control.")
LANTERN = ("Lands you control have \"{T}: Add one mana of any color.\"\n"
           "{T}: Add one mana of any color.")
ZHULODOK = ("Colorless spells you cast from your hand with mana value 7 or "
            "greater have \"Cascade, cascade.\"")
IMOTI = "Spells you cast with mana value 6 or greater have cascade."
YIDRIS_TAIL = ("Whenever Yidris, Maelstrom Wielder deals combat damage to a "
               "player, as you cast spells from your hand this turn, they "
               "gain cascade.")
MANTLE = ("Enchant creature you control\nWhen this Aura enters, return any "
          "number of target Aura and/or Equipment cards from your graveyard "
          "to the battlefield attached to enchanted creature.\nEnchanted "
          "creature gets +1/+1 for each Aura and Equipment attached to it.")
REANIMATE = ("Put target creature card from a graveyard onto the "
             "battlefield under your control. You lose life equal to its "
             "mana value.")
EXTRACTION = ("Lifelink\nWhen this creature enters, return target creature "
              "card with mana value 2 or less from your graveyard to the "
              "battlefield. That creature can't attack or block for as long "
              "as you control this creature.")


class TestA1TargetPhrasePredicateCut:
    def test_mind_sludge_no_longer_you_restricted(self, game, make_card):
        sludge = make_card("Mind Sludge", type_line="Sorcery",
                           oracle_text=MIND_SLUDGE, power=None, toughness=None)
        r = _parse_target_restriction_from_oracle(sludge)
        assert r is not None
        from rules.targeting import ControllerRestriction
        assert r.controller != ControllerRestriction.YOU, (
            "the 'for each Swamp you control' scaling tail set a YOU "
            "restriction on the TARGET — the opponent was rejected 'wrong "
            "controller' all game")

    def test_mind_sludge_end_to_end_at_the_opponent(self, game, make_card):
        # Drive the PRODUCTION consumer, not just the parser.
        rick, claude = game.players
        sludge = make_card("Mind Sludge", type_line="Sorcery",
                           oracle_text=MIND_SLUDGE, power=None, toughness=None)
        legal, reason = _validate_player_target_for_action(
            game, claude, sludge, rick.name)
        assert legal, f"the opponent is THE legal target: {reason}"

    def test_legitimate_controller_qualifier_survives(self, game, make_card):
        # "you control" BEFORE any verb must still restrict.
        card = make_card("Probe", type_line="Instant",
                         oracle_text="Untap target creature you control.",
                         power=None, toughness=None)
        r = _parse_target_restriction_from_oracle(card)
        from rules.targeting import ControllerRestriction
        assert r.controller == ControllerRestriction.YOU

    def test_smite_power_qualifier_survives(self, make_card):
        # A with-clause precedes any verb — the cut must not touch it.
        card = make_card("Smite the Monstrous", type_line="Instant",
                         oracle_text=SMITE, power=None, toughness=None)
        r = _parse_target_restriction_from_oracle(card)
        assert getattr(r, 'power_min', None) == 4 or \
            getattr(r, 'min_power', None) == 4 or \
            'power' in str(vars(r)).lower(), (
            "the 'with power 4 or greater' qualifier must survive the cut")


class TestA2NighthawkCDA:
    def _hawk(self, game, make_card):
        rick, claude = game.players
        hawk = make_card("Nighthawk Scavenger",
                         type_line="Creature — Vampire Rogue",
                         oracle_text=NIGHTHAWK, power="1+*", toughness="3")
        rick.battlefield.append(hawk)
        return hawk

    def test_counts_opponent_graveyard_card_types(self, game, make_card):
        hawk = self._hawk(game, make_card)
        claude = game.players[1]
        claude.graveyard.extend([
            make_card("Dead Bear", type_line="Creature — Bear"),
            make_card("Dead Peak", type_line="Land — Mountain", power=None,
                      toughness=None),
            make_card("Dead Bolt", type_line="Instant", power=None,
                      toughness=None),
        ])
        assert hawk.get_effective_power(game) == 4  # 1 + 3 card types

    def test_never_below_the_printed_one(self, game, make_card):
        hawk = self._hawk(game, make_card)
        assert hawk.get_effective_power(game) == 1, (
            "int('1+*') raised, base fell to 0, and the creature was "
            "filtered from every attack for the rest of the game")

    def test_own_graveyard_does_not_count(self, game, make_card):
        hawk = self._hawk(game, make_card)
        game.players[0].graveyard.append(
            make_card("Own Dead Bear", type_line="Creature — Bear"))
        assert hawk.get_effective_power(game) == 1


class TestB1AlurenAdventure:
    def _setup(self, game, make_card):
        rick, claude = game.players
        aluren = make_card("Aluren", type_line="Enchantment",
                           oracle_text="Any player may cast creature spells "
                                       "with mana value 3 or less without "
                                       "paying their mana costs and as "
                                       "though they had flash.",
                           power=None, toughness=None)
        claude.battlefield.append(aluren)
        squire = make_card("Silverflame Squire",
                           type_line="Creature — Human Soldier",
                           mana_cost="{1}{W}", cmc=2, power="2", toughness="1")
        squire.adventure_name = "On Alert"
        squire.adventure_cost = "{2}{W}"
        return squire

    def test_adventure_half_is_not_a_creature_spell(self, game, make_card):
        squire = self._setup(game, make_card)
        squire.cast_as_adventure = True
        assert not is_aluren_free_cast(game, squire), (
            "CR 715.3: the adventure half is an INSTANT — Aluren free-cast "
            "an unpaid tap-down on a lethal turn")

    def test_creature_half_still_free(self, game, make_card):
        squire = self._setup(game, make_card)
        squire.cast_as_adventure = False
        assert is_aluren_free_cast(game, squire)


class TestB2OakenBoonTarget:
    def test_declared_target_wins(self):
        lib = get_effect_library()
        actions, _ = lib.resolve_spell(
            "Oaken Boon", "Put two +1/+1 counters on target creature.",
            "Rick", "Claude",
            game_context={'explicit_target_name': 'Craterhoof Behemoth',
                          'best_own_creature': 'Chulane, Teller of Tales'})
        assert actions and actions[0]['card'] == 'Craterhoof Behemoth', (
            "the counters landed on Chulane while Craterhoof was declared")

    def test_heuristic_fallback_without_declaration(self):
        lib = get_effect_library()
        actions, _ = lib.resolve_spell(
            "Oaken Boon", "Put two +1/+1 counters on target creature.",
            "Rick", "Claude",
            game_context={'best_own_creature': 'Chulane, Teller of Tales'})
        assert actions and actions[0]['card'] == 'Chulane, Teller of Tales'


class TestB3GraveyardTypeGate:
    def _cast_setup(self, game, make_card, declared):
        engine = GameEngine(None)
        rick = game.players[0]
        rean = make_card("Reanimate", type_line="Sorcery",
                         oracle_text=REANIMATE, mana_cost="{B}",
                         power=None, toughness=None)
        rick.hand = [rean]
        swamp = make_card("Swamp", type_line="Basic Land — Swamp",
                          oracle_text="{T}: Add {B}.", power=None,
                          toughness=None)
        rick.battlefield.append(swamp)
        game.active_player_index = 0
        return engine, rick, rean, declared

    def test_land_card_target_rejected_before_payment(self, game, make_card):
        rick = game.players[0]
        # A LEGAL creature target exists (as in the live game — Blazing
        # Rootwalla was available), so the generic no-valid-targets gate
        # stands down and the DECLARED-target type gate is what decides.
        rick.graveyard.append(make_card(
            "Blazing Rootwalla", type_line="Creature — Lizard"))
        rick.graveyard.append(make_card(
            "Graven Cairns", type_line="Land", power=None, toughness=None))
        engine, rick, rean, declared = self._cast_setup(
            game, make_card, "Graven Cairns")
        rejection, _, _ = _validate_cast(engine, game, rick, rean, declared)
        assert rejection is not None and "not a creature card" in rejection[1], (
            "Reanimate accepted a LAND, paid {B}, and fizzled at resolution "
            "— cost paid, effect lost (CR 601.2c)")

    def test_creature_card_target_passes_the_gate(self, game, make_card):
        rick = game.players[0]
        rick.graveyard.append(make_card(
            "Dead Bear", type_line="Creature — Bear"))
        engine, rick, rean, declared = self._cast_setup(
            game, make_card, "Dead Bear")
        rejection, _, _ = _validate_cast(engine, game, rick, rean, declared)
        assert rejection is None or "not a" not in rejection[1]

    def test_unfound_name_keeps_current_behavior(self, game, make_card):
        # A typo/hallucinated name is resolve_cast_target's job, not this
        # gate's — it must not reject what it cannot see.
        engine, rick, rean, declared = self._cast_setup(
            game, make_card, "Nonexistent Horror")
        rejection, _, _ = _validate_cast(engine, game, rick, rean, declared)
        assert rejection is None or "not a" not in rejection[1]


class TestC1RobberOfTheRich:
    def test_emits_the_real_vocabulary(self):
        lib = get_effect_library()
        actions, _ = lib.resolve_attack_trigger(
            "Robber of the Rich",
            "Whenever this creature attacks, if defending player has more "
            "cards in hand than you, exile the top card of their library.",
            "Robber of the Rich", 2, "Rick", "Claude",
            game_context={'opponent_hand': ['a', 'b', 'c'],
                          'controller_hand': ['a']})
        assert actions and actions[0]['action'] == 'exile_top_of_library', (
            "the old move_card carried the sentinel 'top_of_library' — no "
            "card has that name, so the whole ability was a silent no-op")

    def test_condition_still_gates(self):
        lib = get_effect_library()
        actions, _ = lib.resolve_attack_trigger(
            "Robber of the Rich",
            "Whenever this creature attacks, if defending player has more "
            "cards in hand than you, exile the top card of their library.",
            "Robber of the Rich", 2, "Rick", "Claude",
            game_context={'opponent_hand': ['a'], 'controller_hand': ['a', 'b']})
        assert actions and actions[0]['action'] == 'no_action'

    def test_the_action_executes_against_real_state(self, game, rules, make_card):
        # Execute new vocabulary against a real GameState — the lesson that
        # found the sentinel's sibling class twice before.
        claude = game.players[1]
        top = make_card("Library Top", type_line="Instant", power=None,
                        toughness=None)
        claude.library = [top]
        rules._execute_action_on_state(game, {
            "action": "exile_top_of_library", "player": "Claude", "count": 1})
        assert top in claude.exile and top not in claude.library


class TestC2NontokenEntersQualifier:
    def _soul_setup(self, game, make_card):
        rick = game.players[0]
        soul = make_card("Soul of the Harvest",
                         type_line="Creature — Elemental",
                         oracle_text=SOUL_OF_HARVEST, power="6", toughness="6")
        rick.battlefield.append(soul)
        return rick

    def test_token_entry_does_not_draw(self, game, make_card):
        rick = self._soul_setup(game, make_card)
        rick.library = [make_card("Lib1", type_line="Instant", power=None,
                                  toughness=None)]
        token = make_card("Elf Warrior", type_line="Creature — Elf Warrior",
                          power="1", toughness="1")
        token.is_token = True
        rick.battlefield.append(token)
        engine = GameEngine(None)
        hand_before = len(rick.hand)
        _check_creature_etb_triggers_sync(engine, game, rick, token)
        assert len(rick.hand) == hand_before, (
            "Soul of the Harvest drew two cards off two Rhys TOKENS — its "
            "own text says NONTOKEN")

    def test_nontoken_entry_still_draws(self, game, make_card):
        rick = self._soul_setup(game, make_card)
        rick.library = [make_card("Lib1", type_line="Instant", power=None,
                                  toughness=None)]
        bear = make_card("Grizzly Bears", type_line="Creature — Bear")
        rick.battlefield.append(bear)
        engine = GameEngine(None)
        hand_before = len(rick.hand)
        _check_creature_etb_triggers_sync(engine, game, rick, bear)
        assert len(rick.hand) == hand_before + 1, (
            "the qualifier gate must not eat the real trigger")


class TestC3CastTargetTriggerClause:
    def test_aura_with_graveyard_trigger_finds_battlefield_target(
            self, game, make_card):
        rick = game.players[0]
        blademaster = make_card("Heavenly Blademaster",
                                type_line="Creature — Angel Samurai",
                                power="4", toughness="4")
        rick.battlefield.append(blademaster)
        mantle = make_card("Mantle of the Ancients",
                           type_line="Enchantment — Aura",
                           oracle_text=MANTLE, power=None, toughness=None)
        resolved = resolve_cast_target(game, rick, mantle,
                                       "Heavenly Blademaster")
        assert resolved is blademaster, (
            "the ETB clause's 'from your graveyard' made the whole card "
            "read graveyard-only and the declared enchant target was "
            "dropped to auto-select (White Cat)")

    def test_true_graveyard_spell_still_gated(self, game, make_card):
        rick = game.players[0]
        live_bear = make_card("Grizzly Bears", type_line="Creature — Bear")
        rick.battlefield.append(live_bear)
        rean = make_card("Reanimate", type_line="Sorcery",
                         oracle_text=REANIMATE, power=None, toughness=None)
        resolved = resolve_cast_target(game, rick, rean, "Grizzly Bears")
        assert resolved is not live_bear, (
            "a battlefield object must not satisfy a graveyard-only spell "
            "(the Aug-11 FOUNDATION gate)")


class TestC4NevermakerOwnerRouting:
    ORACLE = ("Flying\nWhen this creature leaves the battlefield, put "
              "target nonland permanent on top of its owner's library.")

    def test_own_side_target_routes_to_controller(self):
        lib = get_effect_library()
        own = type('C', (), {'name': 'My Relic'})()
        actions, _ = lib.resolve_etb(
            "Nevermaker", self.ORACLE, "Rick", "Claude",
            game_context={'explicit_target_name': 'My Relic',
                          'controller_battlefield': [own]},
            event_type="ltb")
        assert actions and actions[0]['player'] == 'Rick', (
            "player was hardcoded to opp — an own-side declared target "
            "(legal: 'target nonland permanent') silently failed the "
            "opp-side lookup")

    def test_opponent_target_still_routes_to_opponent(self):
        lib = get_effect_library()
        actions, _ = lib.resolve_etb(
            "Nevermaker", self.ORACLE, "Rick", "Claude",
            game_context={'explicit_target_name': 'Sun Titan',
                          'controller_battlefield': []},
            event_type="ltb")
        assert actions and actions[0]['player'] == 'Claude'


class TestD1StaleAttachments:
    def test_reanimated_creature_sheds_its_old_aura(self, game, rules,
                                                    make_card):
        rick, claude = game.players
        seer = make_card("Viscera Seer", type_line="Creature — Vampire Wizard",
                         power="1", toughness="1")
        arrest = make_card("Arrest", type_line="Enchantment — Aura",
                           oracle_text="Enchanted creature can't attack or "
                                       "block, and its activated abilities "
                                       "can't be activated.",
                           power=None, toughness=None)
        arrest.attached_to = seer.id
        seer.attachments = [arrest.id]
        claude.battlefield.append(arrest)
        rick.graveyard.append(seer)  # the original died; Arrest lingers
        rules._execute_action_on_state(game, {
            "action": "move_card", "card": "Viscera Seer",
            "from_zone": "graveyard", "to_zone": "battlefield",
            "player": "Rick"})
        assert arrest.attached_to is None, (
            "CR 400.7: the returned creature is a NEW object — Arrest kept "
            "a reanimated Viscera Seer locked down for turns")
        assert not seer.attachments

    def test_fresh_animate_dead_bind_survives(self, game, rules, make_card):
        # The exemption: a bind created IN this entry stamps
        # _bound_creature_id before the emit and must not be severed.
        rick = game.players[0]
        dead = make_card("Dead Bear", type_line="Creature — Bear")
        rick.graveyard.append(dead)
        aura = make_card("Animate Dead", type_line="Enchantment — Aura",
                         oracle_text="Enchant creature card in a graveyard",
                         power=None, toughness=None)
        rick.battlefield.append(aura)
        # The bind lives in the REANIMATE action (bind-then-emit), which is
        # exactly the ordering the exemption exists for.
        rules._execute_action_on_state(game, {
            "action": "reanimate", "card": "Dead Bear",
            "player": "Rick", "_source_card_name": "Animate Dead"})
        assert aura.attached_to == dead.id, (
            "the detach subscriber must not clobber the bind made in the "
            "same entry")


class TestD3ExtractionSpecialistLock:
    def _locked_creature(self, game, rules, make_card):
        rick = game.players[0]
        specialist = make_card("Extraction Specialist",
                               type_line="Creature — Human Rogue",
                               oracle_text=EXTRACTION, power="3", toughness="2")
        rick.battlefield.append(specialist)
        seer = make_card("Viscera Seer", type_line="Creature — Vampire Wizard",
                         power="1", toughness="1")
        rick.graveyard.append(seer)
        rules._execute_action_on_state(game, {
            "action": "move_card", "card": "Viscera Seer",
            "from_zone": "graveyard", "to_zone": "battlefield",
            "player": "Rick",
            "combat_lock_while_controlling": "Extraction Specialist"})
        seer.summoning_sick = False
        return rick, specialist, seer

    def test_locked_while_specialist_remains(self, game, rules, make_card):
        rick, specialist, seer = self._locked_creature(game, rules, make_card)
        assert seer.combat_locked_while_controlling == "Extraction Specialist"
        assert not seer.can_attack(game), "the printed rider was unimplemented"
        assert not seer.can_block(game=game)

    def test_lock_ends_when_specialist_leaves(self, game, rules, make_card):
        rick, specialist, seer = self._locked_creature(game, rules, make_card)
        rick.battlefield.remove(specialist)
        assert seer.can_attack(game), (
            "'for as long as you control this creature' — the lock ends "
            "with the Specialist")
        assert seer.combat_locked_while_controlling is None

    def test_declared_legal_candidate_honored(self):
        lib = get_effect_library()
        g1 = type('C', (), {'type_line': 'Creature — Bird', 'cmc': 1,
                            'name': 'Small Bird'})()
        g2 = type('C', (), {'type_line': 'Creature — Wizard', 'cmc': 2,
                            'name': 'Viscera Seer'})()
        actions, _ = lib.resolve_etb(
            "Extraction Specialist", EXTRACTION, "Rick", "Claude",
            game_context={'controller_graveyard': [g1, g2],
                          'explicit_target_name': 'Small Bird'})
        assert actions and actions[0]['card'] == 'Small Bird', (
            "the generator always auto-picked highest CMC")
        assert actions[0].get('combat_lock_while_controlling') == \
            'Extraction Specialist'


class TestD5ShalaiPlayerHexproof:
    def test_list_form_grants_player_hexproof(self, game, make_card):
        rick = game.players[0]
        rick.battlefield.append(make_card(
            "Shalai, Voice of Plenty", type_line="Legendary Creature — Angel",
            oracle_text=SHALAI, power="3", toughness="4"))
        t = _player_to_targetable(rick)
        assert t.has_hexproof_from_opponents, (
            "the comma list defeats 'you have hexproof' adjacency — the "
            "player-hexproof half never registered")

    def test_creature_only_grant_does_not_leak_to_player(self, game,
                                                         make_card):
        rick = game.players[0]
        rick.battlefield.append(make_card(
            "Leafy Guard", type_line="Creature — Elemental",
            oracle_text="Creatures you control have hexproof.",
            power="2", toughness="2"))
        t = _player_to_targetable(rick)
        assert not t.has_hexproof_from_opponents


class TestD6ChromaticLantern:
    def test_plain_land_gains_any_color(self, game, make_card):
        rick = game.players[0]
        rick.battlefield.append(make_card(
            "Chromatic Lantern", type_line="Artifact",
            oracle_text=LANTERN, power=None, toughness=None))
        mountain = make_card("Mountain", type_line="Basic Land — Mountain",
                             oracle_text="{T}: Add {R}.", power=None,
                             toughness=None)
        rick.battlefield.append(mountain)
        assert rick._get_mana_production(mountain).get('any', 0) >= 1, (
            "the headline half — lands gain any-color — was unimplemented")

    def test_without_lantern_the_mountain_is_red(self, game, make_card):
        rick = game.players[0]
        mountain = make_card("Mountain", type_line="Basic Land — Mountain",
                             oracle_text="{T}: Add {R}.", power=None,
                             toughness=None)
        rick.battlefield.append(mountain)
        prod = rick._get_mana_production(mountain)
        assert prod.get('R', 0) == 1 and not prod.get('any', 0)

    def test_ancient_tomb_keeps_its_two(self, game, make_card):
        # The Dryad placement convention: special/dynamic lands return first.
        rick = game.players[0]
        rick.battlefield.append(make_card(
            "Chromatic Lantern", type_line="Artifact",
            oracle_text=LANTERN, power=None, toughness=None))
        tomb = make_card("Ancient Tomb", type_line="Land",
                         oracle_text="{T}: Add {C}{C}. This land deals 2 "
                                     "damage to you.",
                         power=None, toughness=None)
        rick.battlefield.append(tomb)
        assert rick._get_mana_production(tomb).get('C', 0) == 2


class TestD7ZhulodokCascadeGrant:
    def _caster_with(self, game, make_card, oracle):
        rick = game.players[0]
        rick.battlefield.append(make_card(
            "Granter", type_line="Legendary Creature — Eldrazi",
            oracle_text=oracle, power="6", toughness="6"))
        return rick

    def test_colorless_seven_drop_gains_double_cascade(self, game, make_card):
        rick = self._caster_with(game, make_card, ZHULODOK)
        spell = make_card("Kozilek's Fist", type_line="Artifact Creature",
                          cmc=8, power="8", toughness="8")
        spell.colors = []
        assert static_cascade_grant_count(rick, spell) == 2, (
            "the core ability was entirely unimplemented (grep zero)")

    def test_colored_spell_refused(self, game, make_card):
        rick = self._caster_with(game, make_card, ZHULODOK)
        spell = make_card("Green Fatty", type_line="Creature — Wurm",
                          cmc=8, power="8", toughness="8")
        spell.colors = ['G']
        assert static_cascade_grant_count(rick, spell) == 0

    def test_low_mv_refused(self, game, make_card):
        rick = self._caster_with(game, make_card, ZHULODOK)
        spell = make_card("Small Rock", type_line="Artifact", cmc=2,
                          power=None, toughness=None)
        spell.colors = []
        assert static_cascade_grant_count(rick, spell) == 0

    def test_imoti_shape_grants_single_cascade(self, game, make_card):
        rick = self._caster_with(game, make_card, IMOTI)
        spell = make_card("Green Fatty", type_line="Creature — Wurm",
                          cmc=6, power="6", toughness="6")
        spell.colors = ['G']
        assert static_cascade_grant_count(rick, spell) == 1, (
            "Imoti has no colorless clause — colored spells qualify")

    def test_yidris_text_does_not_self_match(self, game, make_card):
        rick = self._caster_with(game, make_card, YIDRIS_TAIL)
        spell = make_card("Kozilek's Fist", type_line="Artifact Creature",
                          cmc=8, power="8", toughness="8")
        spell.colors = []
        assert static_cascade_grant_count(rick, spell) == 0, (
            "Yidris grants via his combat-damage trigger, not this static "
            "scan — the July-21 self-cascade class must not return")
