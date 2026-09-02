"""Sep 1, 2026 batch audit (sha=8cc5a1a + the 7e0ad86 re-run, 160 games).

Inline-sweep findings, each pinned through the REAL path the live failure
took (the pin-shape-reachability lesson: a pin that exercises a shape the
live caller never sends is a comment).

  I-1  Act of Treason at the caster's OWN creature -> Tier 3 -> refused.
       (a) _gen_temp_control declined a declared own-creature target;
       (b) the judge's combat-shape guard read haste's REMINDER text.
  I-2  Foretell's reminder tripped the optional-payment guard.
  I-3  The equipment combat-damage dispatch read a template's [] as
       unhandled (Sword of Sinew and Steel x6 wasted Tier-3 calls).
  I-4  Quietus Spike had no template (a real lost effect).
  I-5  The inline _get_action_error matched the stash on the PARENT name
       while the producer keyed it on the adventure HALF.
  I-6  scan_damaged_creature lost the owner of a creature that had already
       died to the triggering damage (Negator), and Boros Reckoner's
       reflect went to Tier 3, which fabricated a life gain.

Oracle constants are cache-verified verbatim (data/card_data_cache.json).
"""

import asyncio

import pytest

from mtg.models import Card, GameState, Player
from mtg.rules_engine import RulesEngine
from rules.effect_templates import build_game_context, get_effect_library


ACT_OF_TREASON = ("Gain control of target creature until end of turn. Untap "
                  "that creature. It gains haste until end of turn. (It can "
                  "attack and {T} this turn.)")
BEHOLD_THE_MULTIVERSE = (
    "Scry 2, then draw two cards.\nForetell {1}{U} (During your turn, you may "
    "pay {2} and exile this card from your hand face down. Cast it on a later "
    "turn for its foretell cost.)")
EXTORT_REMINDER = ("extort (whenever you cast a spell, you may pay {w/b}. "
                   "if you do, each opponent loses 1 life.)")
SWORD_OF_SINEW_AND_STEEL = (
    "Equipped creature gets +2/+2 and has protection from black and from red."
    "\nWhenever equipped creature deals combat damage to a player, destroy up "
    "to one target planeswalker and up to one target artifact.\nEquip {2}")
QUIETUS_SPIKE = ("Equipped creature has deathtouch.\nWhenever equipped "
                 "creature deals combat damage to a player, that player loses "
                 "half their life, rounded up.\nEquip {3}")
PHYREXIAN_NEGATOR = ("Trample\nWhenever this creature is dealt damage, "
                     "sacrifice that many permanents.")
BOROS_RECKONER = ("Whenever this creature is dealt damage, it deals that much "
                  "damage to any target.\n{R/W}: This creature gains first "
                  "strike until end of turn.")


def _fake_judge_client(response_text):
    class _Content:
        def __init__(self, t):
            self.text = t
            self.type = "text"

    class _Response:
        def __init__(self, t):
            self.content = [_Content(t)]

    class _Messages:
        def __init__(self, t):
            self._t = t

        def create(self, **kwargs):
            return _Response(self._t)

    class _Client:
        def __init__(self, t):
            self.messages = _Messages(t)

    return _Client(response_text)


# ---------------------------------------------------------------------------
# I-1a: a declared OWN-creature target resolves the riders (no control change)
# ---------------------------------------------------------------------------

class TestTempControlOwnTarget:

    def _aot(self, make_card):
        return make_card("Act of Treason", type_line="Sorcery",
                         oracle_text=ACT_OF_TREASON, mana_cost="{2}{R}")

    def test_own_creature_untaps_and_gains_haste_without_a_steal(
            self, game, rules, make_card):
        rick, claude = game.players
        own = make_card("Midnight Reaper", type_line="Creature — Zombie Knight",
                        power="3", toughness="2")
        own.tapped = True
        rick.battlefield.append(own)
        lib = get_effect_library()
        ctx = build_game_context(game, rick, claude, card=self._aot(make_card),
                                 explicit_target=own)
        actions, _ = lib.resolve_spell(
            card_name="Act of Treason", oracle_text=ACT_OF_TREASON,
            controller=rick.name, opponent=claude.name, game_context=ctx)
        assert actions is not None, (
            "a legal self-target must resolve at Tier 1.5 — live it fell to "
            "Tier 3 and was refused (mana paid, nothing happened, x3)")
        kinds = [a.get("action") for a in actions]
        assert "steal_permanent" not in kinds, "no control change on your own creature"
        assert "untap" in kinds and "grant_keywords" in kinds
        for a in actions:
            rules._execute_action_on_state(game, a)
        assert own.tapped is False, "the printed untap applies"
        assert "haste" in [k.lower() for k in (own.temp_keywords or [])]
        assert own in rick.battlefield and own not in claude.battlefield

    def test_a_name_on_neither_battlefield_still_declines(
            self, game, make_card):
        rick, claude = game.players
        rick.battlefield.append(make_card("Bear", type_line="Creature — Bear",
                                          power="2", toughness="2"))
        ghost = make_card("Ghost", type_line="Creature — Spirit",
                          power="1", toughness="1")
        lib = get_effect_library()
        ctx = build_game_context(game, rick, claude, card=self._aot(make_card),
                                 explicit_target=ghost)
        assert ctx.get("explicit_target_name") == "Ghost"
        actions, _ = lib.resolve_spell(
            card_name="Act of Treason", oracle_text=ACT_OF_TREASON,
            controller=rick.name, opponent=claude.name, game_context=ctx)
        assert actions is None, "never silently retarget (the Abrupt Decay rule)"

    def test_an_opponent_target_is_still_stolen(self, game, make_card):
        """Adverse control: the ordinary steal must be untouched."""
        rick, claude = game.players
        theirs = make_card("Korvold, Fae-Cursed King",
                           type_line="Legendary Creature — Dragon Noble",
                           power="4", toughness="4")
        claude.battlefield.append(theirs)
        lib = get_effect_library()
        ctx = build_game_context(game, rick, claude, card=self._aot(make_card),
                                 explicit_target=theirs)
        actions, _ = lib.resolve_spell(
            card_name="Act of Treason", oracle_text=ACT_OF_TREASON,
            controller=rick.name, opponent=claude.name, game_context=ctx)
        assert actions and actions[0]["action"] == "steal_permanent"
        assert actions[0]["card"] == theirs.name
        assert actions[0]["until_end_of_turn"] is True


# ---------------------------------------------------------------------------
# I-1b: the judge's combat-shape guard ignores reminder text (CR 207.2)
# ---------------------------------------------------------------------------

class TestCombatGuardIgnoresReminderText:

    def test_haste_reminder_is_not_combat_shaped(self):
        from mtg.judge import is_combat_shaped_resolve
        assert is_combat_shaped_resolve(ACT_OF_TREASON) is False, (
            "'(It can attack and {T} this turn.)' is reminder text — the "
            "guard refused every haste-granting resolution on it")

    @pytest.mark.parametrize("text", [
        "Attack for lethal.",
        "Craterhoof enters, pump the team. Attack for lethal.",
        "Deals combat damage to each opponent.",
    ])
    def test_real_combat_claims_are_still_refused(self, text):
        from mtg.judge import is_combat_shaped_resolve
        assert is_combat_shaped_resolve(text) is True

    def test_rules_text_attack_outside_parens_is_still_refused(self):
        """The strip must remove ONLY parentheticals — an 'attack' in rules
        text next to a reminder keeps the guard live."""
        from mtg.judge import is_combat_shaped_resolve
        assert is_combat_shaped_resolve(
            "Attack for lethal. (It can attack and {T} this turn.)") is True


# ---------------------------------------------------------------------------
# I-2: foretell's reminder text is a cast-time payment, not a resolution one
# ---------------------------------------------------------------------------

class TestOptionalPaymentGuardForetell:

    def _run(self, text):
        from mtg import judge as judge_mod
        game = GameState(players=[Player(name="A"), Player(name="B")],
                         thread_id=1, format="commander")
        rules = RulesEngine(game)
        rules.client = _fake_judge_client(
            '{"explanation": "nothing", "actions": '
            '[{"action": "no_action", "reason": "test"}]}')
        out = asyncio.run(judge_mod.resolve_effect(
            rules, game, text, source_card="Test Source", controller="A"))
        messages = out[0] if isinstance(out, tuple) else out
        return " ".join(messages or []).lower()

    def test_foretell_reminder_does_not_trip_the_guard(self):
        assert "optional cost declined" not in self._run(BEHOLD_THE_MULTIVERSE.lower()), (
            "a hand-cast Behold the Multiverse was refused after paying "
            "{3}{U}: the draw-2 was lost")

    def test_extort_reminder_is_still_guarded(self):
        """Adverse control: extort's 'you may pay' IS a resolution-time
        payment and must keep declining with zero mana."""
        assert "optional cost declined" in self._run(EXTORT_REMINDER)


# ---------------------------------------------------------------------------
# I-3: the equipment dispatch honors the [] = handled-no-op contract
# ---------------------------------------------------------------------------

class TestEquipmentNoOpIsNotQueued:

    def _swing(self, game, make_card, rules, defender_artifact=False):
        from mtg.combat import resolve_combat_damage
        rick, claude = game.players
        bear = make_card("Bear", type_line="Creature — Bear", power="2",
                         toughness="2", owner_index=0)
        sword = make_card("Sword of Sinew and Steel", type_line="Artifact — Equipment",
                          oracle_text=SWORD_OF_SINEW_AND_STEEL, owner_index=0)
        sword.attached_to = bear.id
        bear.attachments = [sword.id]
        bear.attacking = True
        bear.attacking_player = 1
        rick.battlefield.extend([bear, sword])
        if defender_artifact:
            claude.battlefield.append(make_card("Sol Ring", type_line="Artifact",
                                                owner_index=1))
        game.attackers = [bear.id]
        game._rules_engine = rules
        rules.engine_ref = None
        resolve_combat_damage(rules, game)
        return rick, claude

    def test_empty_board_no_op_is_handled_not_queued(self, game, make_card,
                                                     rules, capsys):
        self._swing(game, make_card, rules)
        out = capsys.readouterr().out
        assert "[COMBAT-TRIGGER-UNHANDLED] Sword of Sinew and Steel" not in out, (
            "the template returned [] (CR 603.3c: 'up to one' with zero "
            "targets is a legal no-op); it must not be queued for Tier 3")
        assert "handled no-op" in out

    def test_a_real_artifact_is_still_destroyed(self, game, make_card, rules):
        """Adverse control: the non-empty path is untouched."""
        rick, claude = self._swing(game, make_card, rules, defender_artifact=True)
        assert not any(c.name == "Sol Ring" for c in claude.battlefield)


# ---------------------------------------------------------------------------
# I-4: Quietus Spike
# ---------------------------------------------------------------------------

class TestQuietusSpike:

    def _fire(self, game, life, damage=4):
        rick, claude = game.players
        claude.life = life
        lib = get_effect_library()
        actions, _ = lib.resolve_attack_trigger(
            trigger_card_name="Quietus Spike", trigger_oracle=QUIETUS_SPIKE,
            attacking_creature_name="Bear", attacking_creature_power=damage,
            controller=rick.name, opponent=claude.name,
            game_context={"damage_dealt": damage, "_opponent_player": claude})
        return actions

    @pytest.mark.parametrize("life,expected", [(4, 2), (5, 2), (1, 0), (40, 20)])
    def test_loses_half_rounded_up(self, game, rules, life, expected):
        rick, claude = game.players
        actions = self._fire(game, life)
        assert actions, "the trigger must resolve at Tier 1.5 (it was refused at Tier 3)"
        for a in actions:
            rules._execute_action_on_state(game, a)
        assert claude.life == expected
        assert rick.life == 40, "the controller's life is untouched"

    def test_no_damage_means_no_trigger(self, game):
        assert self._fire(game, 40, damage=0) == []


# ---------------------------------------------------------------------------
# I-5: the inline cast-failure stash matches the adventure half's name
# ---------------------------------------------------------------------------

class TestAdventureHalfStashConsumed:

    def _engine(self):
        from mtg.engine import GameEngine
        return GameEngine(None)

    def test_stash_keyed_on_the_half_reaches_a_parent_named_action(
            self, game, make_card):
        from mtg.ai_turn import _get_action_error
        rick, _ = game.players
        carver = make_card("Garenbrig Carver", type_line="Creature — Giant Warrior",
                           mana_cost="{3}{G}", power="4", toughness="3")
        carver.adventure_name = "Shield's Might"
        carver.adventure_cost = "{1}{G}"
        rick.hand.append(carver)
        reason = ("declared target 'Rosethorn Acolyte' is not a legal target "
                  "for Shield's Might (CR 601.2c)")
        game._last_cast_failure = (game.turn_number, "Shield's Might", reason)
        got = _get_action_error(self._engine(), game, 0, {
            "type": "cast", "card": "Garenbrig Carver",
            "adventure": "Shield's Might", "target": "Rosethorn Acolyte"})
        assert got == reason, (
            "live: the real reason was dropped and the AI was told 'unknown "
            "reason — mana looks sufficient' for three retries")
        assert game._last_cast_failure is None, "consumed on read"

    def test_a_different_cards_stash_is_not_consumed(self, game, make_card):
        from mtg.ai_turn import _get_action_error
        rick, _ = game.players
        rick.hand.append(make_card("Bear", type_line="Creature — Bear",
                                   mana_cost="{1}{G}", power="2", toughness="2"))
        game._last_cast_failure = (game.turn_number, "Shield's Might", "stale")
        got = _get_action_error(self._engine(), game, 0,
                                {"type": "cast", "card": "Bear"})
        assert got != "stale"
        assert game._last_cast_failure is not None, "an unrelated stash survives"


# ---------------------------------------------------------------------------
# I-6: the damaged-creature scan after the damaged creature has died
# ---------------------------------------------------------------------------

class TestDamagedCreatureScanAfterDeath:

    def _setup(self, game, make_card, name, oracle, dead=False):
        rules = RulesEngine(None)
        rules.engine_ref = None
        game._rules_engine = rules
        owner, attacker_owner = game.players
        victim = make_card(name, type_line="Creature — Horror", power="5",
                           toughness="5", oracle_text=oracle, owner_index=0)
        if dead:
            owner.graveyard.append(victim)
        else:
            owner.battlefield.append(victim)
        for i in range(4):
            owner.battlefield.append(make_card(f"Own{i}", type_line="Artifact"))
            attacker_owner.battlefield.append(make_card(f"Opp{i}", type_line="Artifact"))
        return rules, victim, owner, attacker_owner

    def test_negator_that_died_to_the_damage_still_sacrifices(
            self, game, make_card, capsys):
        from mtg.triggers import scan_damaged_creature
        rules, victim, owner, attacker_owner = self._setup(
            game, make_card, "Phyrexian Negator", PHYREXIAN_NEGATOR, dead=True)
        scan_damaged_creature(rules, game, victim, 2, attacker_owner)
        out = capsys.readouterr().out
        assert "[DAMAGED-TRIGGER-UNHANDLED] Phyrexian Negator" not in out, (
            "live: the SBA sweep had already moved Negator to the graveyard, "
            "the owner lookup came back None, and the sacrifice fell to Tier 3")
        assert len([c for c in owner.graveyard if c is not victim]) == 2
        assert len(attacker_owner.graveyard) == 0

    def test_boros_reckoner_reflects_to_the_sources_controller(
            self, game, make_card, capsys):
        from mtg.triggers import scan_damaged_creature
        rules, victim, owner, attacker_owner = self._setup(
            game, make_card, "Boros Reckoner", BOROS_RECKONER)
        before_owner, before_src = owner.life, attacker_owner.life
        scan_damaged_creature(rules, game, victim, 3, attacker_owner)
        out = capsys.readouterr().out
        assert "[DAMAGED-TRIGGER] Boros Reckoner: reflects 3" in out
        assert attacker_owner.life == before_src - 3
        assert owner.life == before_owner, (
            "Tier 3 fabricated a 3-life gain here ('gaining life due to the "
            "simultaneous...'); the deterministic branch must not")
        assert "[DAMAGED-TRIGGER-UNHANDLED]" not in out

    def test_unknown_source_still_queues_the_reflect(self, game, make_card, capsys):
        """The reflect needs a target; with no resolvable source it must
        fall to the queue rather than pick one."""
        from mtg.triggers import scan_damaged_creature
        rules, victim, owner, attacker_owner = self._setup(
            game, make_card, "Boros Reckoner", BOROS_RECKONER)
        scan_damaged_creature(rules, game, victim, 3, None)
        assert "[DAMAGED-TRIGGER-UNHANDLED] Boros Reckoner" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# The Tier-3 drain ranking's two deterministic leaders, templated
# ---------------------------------------------------------------------------

ELDRAZI_MONUMENT = ("Creatures you control get +1/+1 and have flying and "
                    "indestructible.\nAt the beginning of your upkeep, "
                    "sacrifice a creature. If you can't, sacrifice this "
                    "artifact.")
HERALD_OF_ANGUISH = (
    "Improvise (Your artifacts can help cast this spell. Each artifact you "
    "tap after you're done activating mana abilities pays for {1}.)\nFlying\n"
    "At the beginning of your end step, each opponent discards a card.\n"
    "{1}{B}, Sacrifice an artifact: Target creature gets -2/-2 until end of "
    "turn.")


class TestDrainRankingTemplates:

    def test_eldrazi_monument_sacrifices_a_creature(self, game, rules, make_card):
        rick, claude = game.players
        monument = make_card("Eldrazi Monument", type_line="Artifact",
                             oracle_text=ELDRAZI_MONUMENT)
        bear = make_card("Bear", type_line="Creature — Bear", power="2", toughness="2")
        rick.battlefield.extend([monument, bear])
        ctx = build_game_context(game, rick, claude, card=monument)
        actions, _ = get_effect_library().resolve_etb(
            "eldrazi monument upkeep", ELDRAZI_MONUMENT, rick.name, claude.name,
            game_context=ctx, event_type="upkeep")
        assert actions and actions[0]["action"] == "sacrifice_permanent"
        for a in actions:
            rules._execute_action_on_state(game, a)
        assert bear not in rick.battlefield and monument in rick.battlefield

    def test_eldrazi_monument_sacrifices_itself_with_no_creature(
            self, game, rules, make_card):
        rick, claude = game.players
        monument = make_card("Eldrazi Monument", type_line="Artifact",
                             oracle_text=ELDRAZI_MONUMENT)
        rick.battlefield.append(monument)
        rick.battlefield.append(make_card("Sol Ring", type_line="Artifact"))
        ctx = build_game_context(game, rick, claude, card=monument)
        actions, _ = get_effect_library().resolve_etb(
            "eldrazi monument upkeep", ELDRAZI_MONUMENT, rick.name, claude.name,
            game_context=ctx, event_type="upkeep")
        for a in actions:
            rules._execute_action_on_state(game, a)
        assert monument not in rick.battlefield, "the printed fallback"
        assert any(c.name == "Sol Ring" for c in rick.battlefield), (
            "only the Monument goes — never another artifact")

    def test_eldrazi_monument_does_not_fire_on_its_own_etb(self, game, make_card):
        """Suffix key: a bare-name lookup at ETB must find nothing."""
        rick, claude = game.players
        monument = make_card("Eldrazi Monument", type_line="Artifact",
                             oracle_text=ELDRAZI_MONUMENT)
        rick.battlefield.append(make_card("Bear", type_line="Creature — Bear",
                                          power="2", toughness="2"))
        ctx = build_game_context(game, rick, claude, card=monument)
        actions, _ = get_effect_library().resolve_etb(
            "Eldrazi Monument", ELDRAZI_MONUMENT, rick.name, claude.name,
            game_context=ctx)
        assert not any(a.get("action") == "sacrifice_permanent"
                       for a in (actions or []))

    def test_herald_of_anguish_end_step_discard(self, game, rules, make_card):
        rick, claude = game.players
        herald = make_card("Herald of Anguish", type_line="Creature — Demon",
                           power="5", toughness="5", oracle_text=HERALD_OF_ANGUISH)
        rick.battlefield.append(herald)
        claude.hand.append(make_card("Their Card"))
        ctx = build_game_context(game, rick, claude, card=herald)
        actions, _ = get_effect_library().resolve_etb(
            "herald of anguish endstep", HERALD_OF_ANGUISH, rick.name, claude.name,
            game_context=ctx, event_type="end_step")
        assert actions and actions[0]["action"] == "discard"
        for a in actions:
            rules._execute_action_on_state(game, a)
        assert len(claude.hand) == 0 and len(claude.graveyard) == 1


# ---------------------------------------------------------------------------
# Reviewer A (F1): "whenever an OPPONENT discards a card" watchers had no scan
# ---------------------------------------------------------------------------

LILIANAS_CARESS = "Whenever an opponent discards a card, that player loses 2 life."
MEGRIM = ("Whenever an opponent discards a card, this enchantment deals 2 "
          "damage to that player.")


class TestOpponentDiscardWatchers:

    def _discard(self, game, rules, discarder, card):
        """The real choke point every discard site calls."""
        from mtg.helpers import madness_discard_to_exile
        game._rules_engine = rules
        rules.engine_ref = None
        discarder.hand.append(card)
        out = madness_discard_to_exile(game, discarder, card)
        if not out:
            discarder.hand.remove(card)
            discarder.graveyard.append(card)

    def test_lilianas_caress_drains_the_discarding_opponent(
            self, game, rules, make_card, capsys):
        rick, claude = game.players
        claude.battlefield.append(make_card("Liliana's Caress", type_line="Enchantment",
                                            oracle_text=LILIANAS_CARESS))
        self._discard(game, rules, rick, make_card("Archon of Sun's Grace"))
        assert rick.life == 38, (
            "live: Rick discarded to Liliana of the Veil under Qwen's Caress "
            "and lost nothing — the punisher class had no scan")
        assert claude.life == 40
        assert "[DISCARD-TRIGGER] Liliana's Caress fired on opponent Rick" in capsys.readouterr().out

    def test_the_watchers_own_discards_do_not_trigger_it(self, game, rules, make_card):
        rick, claude = game.players
        claude.battlefield.append(make_card("Liliana's Caress", type_line="Enchantment",
                                            oracle_text=LILIANAS_CARESS))
        self._discard(game, rules, claude, make_card("Own Card"))
        assert claude.life == 40 and rick.life == 40

    def test_megrim_deals_damage(self, game, rules, make_card):
        rick, claude = game.players
        claude.battlefield.append(make_card("Megrim", type_line="Enchantment",
                                            oracle_text=MEGRIM))
        self._discard(game, rules, rick, make_card("Some Card"))
        assert rick.life == 38

    def test_you_scope_watchers_are_untouched(self, game, rules, make_card):
        """Adverse control: the existing you-scope scan still fires for the
        discarding player's OWN watcher and not for the opponent's."""
        rick, claude = game.players
        rick.battlefield.append(make_card(
            "Glint-Horn Buccaneer", type_line="Creature — Minotaur Pirate",
            power="2", toughness="4",
            oracle_text="Whenever you discard a card, this creature deals 1 damage to each opponent."))
        self._discard(game, rules, rick, make_card("Some Card"))
        assert claude.life == 39 and rick.life == 40


# ---------------------------------------------------------------------------
# Reviewer B (F2): equipment-GRANTED protection was invisible to blocking and
# damage prevention — the two mtg/helpers call sites passed the GameState
# POSITIONALLY into `ctrl_name`, so `game` stayed None and the grant scan
# never ran (game_1544072987039367239: a Sword of Light and Shadow bearer
# blocked by a white Archangel of Thune, CR 702.16c).
# ---------------------------------------------------------------------------

SWORD_OF_LIGHT_AND_SHADOW = (
    "Equipped creature gets +2/+2 and has protection from white and from "
    "black.\nWhenever equipped creature deals combat damage to a player, you "
    "gain 3 life and you may return up to one target creature card from your "
    "graveyard to your hand.\nEquip {2}")


class TestGrantedProtectionReachesCombat:

    def _equipped(self, game, make_card):
        rick, claude = game.players
        bearer = make_card("Gisela, Blade of Goldnight",
                           type_line="Legendary Creature — Angel",
                           power="5", toughness="5", oracle_text="Flying, first strike",
                           mana_cost="{4}{R}{W}", owner_index=0)
        sword = make_card("Sword of Light and Shadow", type_line="Artifact — Equipment",
                          oracle_text=SWORD_OF_LIGHT_AND_SHADOW, owner_index=0)
        sword.attached_to = bearer.id
        bearer.attachments = [sword.id]
        rick.battlefield.extend([bearer, sword])
        white = make_card("Archangel of Thune", type_line="Creature — Angel",
                          power="3", toughness="4", mana_cost="{3}{W}{W}",
                          oracle_text="Flying, lifelink", owner_index=1)
        green = make_card("Bear", type_line="Creature — Bear", power="2",
                          toughness="2", mana_cost="{1}{G}", oracle_text="Reach",
                          owner_index=1)
        claude.battlefield.extend([white, green])
        return bearer, white, green

    def test_a_white_creature_cannot_block_the_bearer(self, game, make_card):
        bearer, white, green = self._equipped(game, make_card)
        bearer.attacking = True
        assert white.can_block(bearer, game=game) is False, (
            "protection from white granted by the Sword (CR 702.16c)")
        assert green.can_block(bearer, game=game) is True, (
            "a non-white flyer/reacher still may")

    def test_white_damage_to_the_bearer_is_prevented(self, game, make_card):
        from mtg.helpers import protection_prevents_damage
        bearer, white, green = self._equipped(game, make_card)
        prevented, _why = protection_prevents_damage(game, bearer, source_card=white)
        assert prevented is True, "CR 702.16e via the granted protection"
        prevented2, _ = protection_prevents_damage(game, bearer, source_card=green)
        assert prevented2 is False
        # The name/id path is the one burn spells take (they have left the
        # stack by the time their damage lands) and it has its OWN call site
        # — a mutant that reverted only that site survived the object path
        # above, so drive both.
        by_name, _ = protection_prevents_damage(
            game, bearer, source_name=white.name, source_id=white.id)
        assert by_name is True, "the name/id call site must also see the grant"
        by_name2, _ = protection_prevents_damage(
            game, bearer, source_name=green.name, source_id=green.id)
        assert by_name2 is False

    def test_printed_protection_still_works(self, game, make_card):
        """Adverse control: the printed path was never broken."""
        rick, claude = game.players
        akroma = make_card("Akroma", type_line="Legendary Creature — Angel",
                           power="6", toughness="6",
                           oracle_text="Flying, protection from black and from red", owner_index=0)
        rick.battlefield.append(akroma)
        black = make_card("Butcher", type_line="Creature — Vampire", power="5",
                          toughness="4", mana_cost="{5}{B}{B}", owner_index=1)
        claude.battlefield.append(black)
        akroma.attacking = True
        assert black.can_block(akroma, game=game) is False


# ---------------------------------------------------------------------------
# Reviewer B (F3): a Tower sacrifice put the victim in the TAPPER's graveyard
# ---------------------------------------------------------------------------

class TestTowerSacrificeGoesToTheOwner:

    def _tower_game(self, game, make_card, *, commander=False):
        rick, claude = game.players
        stolen = make_card("Gisela, the Broken Blade",
                           type_line="Legendary Creature — Angel Horror",
                           power="4", toughness="3", owner_index=0)
        if commander:
            stolen.is_commander = True
        stolen.temp_control_revert_to = 0
        claude.battlefield.append(stolen)           # Act of Treason'd to Claude
        tower = make_card("Phyrexian Tower", type_line="Legendary Land",
                          oracle_text="{T}: Add {C}.\n{T}, Sacrifice a creature: Add {B}{B}.",
                          owner_index=1)
        claude.battlefield.append(tower)
        return rick, claude, stolen, tower

    def test_stolen_creature_returns_to_its_owners_graveyard(self, game, make_card):
        rick, claude, stolen, tower = self._tower_game(game, make_card)
        victim = claude._apply_sac_cost_at_tap(tower, game)
        assert victim is stolen
        assert stolen in rick.graveyard, (
            "CR 404.3 — live, the thief's Eternal Witness then returned her "
            "to the THIEF's hand: a temporary steal made permanent")
        assert stolen not in claude.graveyard

    def test_stolen_commander_goes_to_its_owners_command_zone(self, game, make_card):
        rick, claude, stolen, tower = self._tower_game(game, make_card, commander=True)
        claude._apply_sac_cost_at_tap(tower, game)
        assert stolen in rick.command_zone, "CR 903.9a, the OWNER's command zone"
        assert stolen not in (claude.command_zone or []) and stolen not in claude.graveyard


# ---------------------------------------------------------------------------
# Reviewer B (F1): Tier 3 invented damage from a text that mentions none
# ---------------------------------------------------------------------------

class TestJudgeRefusesFabricatedDamage:

    def _run(self, text, actions_json):
        from mtg import judge as judge_mod
        game = GameState(players=[Player(name="A", life=40), Player(name="B", life=40)],
                         thread_id=1, format="commander")
        rules = RulesEngine(game)
        rules.client = _fake_judge_client(
            '{"explanation": "x", "actions": ' + actions_json + '}')
        out = asyncio.run(judge_mod.resolve_effect(
            rules, game, text, source_card="Test Source", controller="A"))
        return game

    def test_damage_from_a_sacrifice_only_text_is_dropped(self):
        game = self._run(
            "Sacrifice a creature. If you can't, sacrifice this artifact.",
            '[{"action": "deal_damage", "amount": 1, "target_player": "B"}]')
        assert game.players[1].life == 40, (
            "Eldrazi Monument's upkeep dealt 1 fabricated damage per firing")

    def test_damage_from_a_damage_text_still_lands(self):
        game = self._run(
            "This creature deals 1 damage to each opponent.",
            '[{"action": "deal_damage", "amount": 1, "target_player": "B"}]')
        assert game.players[1].life == 39


# ---------------------------------------------------------------------------
# Reviewer B (F4): the reanimate-shape plan check read a TRIGGER's text
# ---------------------------------------------------------------------------

class TestReanimateShapeCheckIgnoresTriggers:

    def _plan(self, game, make_card, card):
        from mtg.engine import GameEngine
        from mtg.ai_turn import _validate_plan_mana
        rick, _ = game.players
        engine = GameEngine(None)
        game._rules_engine = engine.rules
        rick.hand.append(card)
        for i in range(6):
            rick.battlefield.append(make_card(f"Plains {i}", type_line="Basic Land — Plains",
                                              oracle_text="({T}: Add {W}.)",
                                              power=None, toughness=None))
        kept = _validate_plan_mana(engine, game, 0, [{"type": "cast", "card": card.name}])
        return {a.get("card") for a in kept}

    def test_sword_of_light_and_shadow_is_castable_with_an_empty_graveyard(
            self, game, make_card):
        sword = make_card("Sword of Light and Shadow", type_line="Artifact — Equipment",
                          oracle_text=SWORD_OF_LIGHT_AND_SHADOW, mana_cost="{3}",
                          power=None, toughness=None)
        sword.cmc = 3
        assert "Sword of Light and Shadow" in self._plan(game, make_card, sword), (
            "its graveyard clause is a combat-damage TRIGGER, not the spell's "
            "own resolution — the cast has no graveyard precondition")

    def test_a_real_reanimate_spell_is_still_held(self, game, make_card):
        reanimate = make_card("Reanimate", type_line="Sorcery", mana_cost="{B}",
                              oracle_text=("Put target creature card from a graveyard "
                                           "onto the battlefield under your control. "
                                           "You lose life equal to its mana value."),
                              power=None, toughness=None)
        reanimate.cmc = 1
        assert "Reanimate" not in self._plan(game, make_card, reanimate)


# ---------------------------------------------------------------------------
# Reviewer C (F1): a Tier-3 activation applied -1/-1 counters and returned
# without a state-based check — a 0-toughness creature stayed on the
# battlefield until the next spell (game_1544046811151339640).
# ---------------------------------------------------------------------------

class TestActivationRunsStateBasedActions:

    def test_zero_toughness_dies_before_the_next_action(self, game, make_card):
        from mtg.engine import GameEngine
        rick, claude = game.players
        engine = GameEngine(None)
        game._rules_engine = engine.rules
        engine.rules.engine_ref = engine
        engine.rules.client = _fake_judge_client(
            '{"explanation": "weaken", "actions": [{"action": "add_counters", '
            '"card": "Bear", "counter_type": "-1/-1", "amount": 1}]}')
        weakener = make_card("Weakener", type_line="Artifact",
                             oracle_text="{T}: Weaken target creature.",
                             power=None, toughness=None, owner_index=0)
        rick.battlefield.append(weakener)
        bear = make_card("Bear", type_line="Creature — Bear", power="1", toughness="1",
                         owner_index=1)
        claude.battlefield.append(bear)
        asyncio.run(engine._execute_action(game, 0, {
            "type": "activate", "permanent": "Weakener", "ability": 0, "target": "Bear"}))
        assert bear.counters.get("-1/-1") == 1, "the Tier-3 action itself applied"
        assert bear not in claude.battlefield and bear in claude.graveyard, (
            "CR 704.5f: toughness 0 dies at the very next SBA check, which the "
            "activate branch must run before returning")


# ---------------------------------------------------------------------------
# Reviewer C (F2): Sage's Reverie's ETB draw counted "a" as 1
# ---------------------------------------------------------------------------

SAGES_REVERIE = ("Enchant creature\nWhen this Aura enters, draw a card for each "
                 "Aura you control that's attached to a creature.\nEnchanted "
                 "creature gets +1/+1 for each Aura you control that's attached "
                 "to a creature.")


class TestSagesReverieDraw:

    def _resolve(self, game, make_card, other_auras):
        rick, claude = game.players
        halvar = make_card("Halvar", type_line="Legendary Creature — God", power="4",
                           toughness="4")
        rick.battlefield.append(halvar)
        for i in range(other_auras):
            aura = make_card(f"Mantle {i}", type_line="Enchantment — Aura",
                             oracle_text="Enchant creature\nEnchanted creature gets +1/+1.")
            aura.attached_to = halvar.id
            rick.battlefield.append(aura)
        reverie = make_card("Sage's Reverie", type_line="Enchantment — Aura",
                            oracle_text=SAGES_REVERIE)
        ctx = build_game_context(game, rick, claude, card=reverie)
        actions, _ = get_effect_library().resolve_etb(
            "Sage's Reverie", SAGES_REVERIE, rick.name, claude.name, game_context=ctx)
        return [a for a in actions if a["action"] == "draw_cards"]

    def test_counts_every_attached_aura_including_itself(self, game, make_card):
        draws = self._resolve(game, make_card, other_auras=1)
        assert draws and draws[0]["amount"] == 2, (
            "live: Mantle of the Ancients + the Reverie itself = 2, drew 1")

    def test_alone_it_draws_one(self, game, make_card):
        draws = self._resolve(game, make_card, other_auras=0)
        assert draws and draws[0]["amount"] == 1


# ---------------------------------------------------------------------------
# Reviewer D (F1): Anticipate / Impulse resolved as nothing at Tier 3
# ---------------------------------------------------------------------------

ANTICIPATE = ("Look at the top three cards of your library. Put one of them into "
              "your hand and the rest on the bottom of your library in any order.")


class TestImpulseLook:

    def test_one_to_hand_rest_to_bottom(self, game, rules, make_card):
        rick, claude = game.players
        game._rules_engine = rules
        for i in range(4):
            rick.battlefield.append(make_card(f"Forest {i}", type_line="Basic Land — Forest"))
        top = [make_card("Bolt", type_line="Instant", mana_cost="{R}"),
               make_card("Big Guy", type_line="Creature — Giant", mana_cost="{5}{G}"),
               make_card("Forest", type_line="Basic Land — Forest")]
        top[0].cmc, top[1].cmc, top[2].cmc = 1, 6, 0
        deep = [make_card(f"Deep {i}") for i in range(5)]
        rick.library = top + deep
        anticipate = make_card("Anticipate", type_line="Instant", oracle_text=ANTICIPATE,
                               mana_cost="{1}{U}")
        ctx = build_game_context(game, rick, claude, card=anticipate)
        actions, _ = get_effect_library().resolve_spell(
            card_name="Anticipate", oracle_text=ANTICIPATE, controller=rick.name,
            opponent=claude.name, game_context=ctx)
        assert actions, "live: cost paid, nothing happened"
        msgs = [rules._execute_action_on_state(game, a) for a in actions]
        assert len(rick.hand) == 1 and rick.hand[0] in top, "one of the three reaches hand"
        assert len(rick.library) == 7
        assert rick.library[:5] == deep, "the two unchosen cards went UNDER the deep cards"
        assert (sorted(c.name for c in rick.library[5:])
                == sorted(c.name for c in top if c is not rick.hand[0]))
        joined = " ".join(m for m in msgs if m)
        for c in top:
            if c is not rick.hand[0]:
                assert c.name not in joined, "the bottomed cards are hidden information"


# ---------------------------------------------------------------------------
# Reviewer D (F2): "target creature can't block this turn" had no generic
# pattern and no judge vocabulary (Demonic Dread resolved as nothing)
# ---------------------------------------------------------------------------

DEMONIC_DREAD = ("Cascade (When you cast this spell, exile cards from the top of "
                 "your library until you exile a nonland card that costs less. You "
                 "may cast it without paying its mana cost. Put the exiled cards on "
                 "the bottom of your library in a random order.)\nTarget creature "
                 "can't block this turn.")


class TestCantBlockPattern:

    def _resolve(self, game, make_card, explicit=None, text=DEMONIC_DREAD):
        rick, claude = game.players
        dread = make_card("Demonic Dread", type_line="Sorcery", oracle_text=text)
        ctx = build_game_context(game, rick, claude, card=dread, explicit_target=explicit)
        actions, _ = get_effect_library().resolve_spell(
            card_name="Demonic Dread", oracle_text=text, controller=rick.name,
            opponent=claude.name, game_context=ctx)
        return actions

    def test_picks_the_largest_opposing_creature(self, game, rules, make_card):
        rick, claude = game.players
        small = make_card("Small", type_line="Creature — Bear", power="1", toughness="1")
        big = make_card("Big", type_line="Creature — Bear", power="5", toughness="5")
        claude.battlefield.extend([small, big])
        actions = self._resolve(game, make_card)
        assert actions and actions[0]["action"] == "cant_block_this_turn"
        assert actions[0]["target_card"] == "Big"
        rules._execute_action_on_state(game, actions[0])
        assert big.can_block(make_card("Attacker", type_line="Creature", power="2",
                                       toughness="2"), game=game) is False

    def test_the_cascade_keyword_line_is_not_a_compound(self, game, make_card):
        rick, claude = game.players
        claude.battlefield.append(make_card("Big", type_line="Creature — Bear",
                                            power="5", toughness="5"))
        assert self._resolve(game, make_card) is not None

    def test_a_real_compound_declines_to_tier3(self, game, make_card):
        rick, claude = game.players
        claude.battlefield.append(make_card("Big", type_line="Creature — Bear",
                                            power="5", toughness="5"))
        compound = "Target creature can't block this turn. Draw a card."
        assert self._resolve(game, make_card, text=compound) is None

    def test_judge_vocabulary_documents_the_action(self):
        import inspect
        from mtg import judge
        src = inspect.getsource(judge)
        assert src.count('"cant_block_this_turn"') >= 2, (
            "both judge prompt blocks must offer the action; Tier 3 cannot "
            "emit vocabulary it was never shown")


# ---------------------------------------------------------------------------
# Reviewer D (F4): the miracle reveal line posted AFTER the cast resolved
# ---------------------------------------------------------------------------

TERMINUS = ("Put all creatures on the bottom of their owners' libraries.\n"
            "Miracle {W} (You may cast this card for its miracle cost when "
            "you draw it if it's the first card you drew this turn.)")


class TestMiracleAnnouncesBeforeTheCast:

    def test_reveal_line_precedes_the_effect(self, game, make_card):
        from mtg.engine import GameEngine
        from mtg.spells import resolve_pending_miracles
        rick, claude = game.players
        game.active_player_index = 0
        engine = GameEngine(None)
        game._rules_engine = engine.rules
        engine.rules.engine_ref = engine
        rick.battlefield.append(make_card("Plains", type_line="Basic Land — Plains",
                                          oracle_text="{T}: Add {W}."))
        for i in range(10):
            rick.library.append(make_card(f"Lib {i}", type_line="Creature"))
        claude.battlefield.append(make_card("Bear", type_line="Creature — Bear",
                                            power="2", toughness="2"))
        term = make_card("Terminus", type_line="Sorcery", oracle_text=TERMINUS,
                         mana_cost="{4}{W}{W}")
        rick.library.insert(0, term)
        engine.draw_cards(rick, 1, game)
        assert [c.name for c, _ in game._miracle_pending] == ["Terminus"]
        msgs = asyncio.run(resolve_pending_miracles(engine, game))
        assert msgs and "reveals and casts **Terminus**" in msgs[0], (
            "live: Dissolve's counter and scry posted, THEN 'reveals and casts "
            "Entreat the Angels' — as if it were cast again")
