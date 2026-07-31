"""July 31, 2026 batch-11 reviewer-wave pins (batch game_15325*).

Four Sonnet reviewers on the longest-unexamined complement (limited, the
brawl_omnath mirror, madness/graveyard, the cube autodraft pipeline) — the
recency-of-attention rule paying out again. Findings pinned here:

Brawl mirror (game_1532532200061403350):
- B1 CRITICAL: the delegated SBA adapter had no animated-land promotion —
  Sylvan Awakening's six 2/2 lands parsed as 0/0 and died to CR 704.5f the
  moment they animated (the May 17 fix patched the inline path only; the
  June 10 Death's Shadow shape, again).
- B2 CRITICAL: Wrenn and Seven was a bare name-keyed template, so EVERY
  ability activation resolved to the same wrong draw-2 via
  resolve_pw_ability's resolve_etb fallthrough. Now three
  _pw_ability_templates entries keyed by ability snippet.
- B3 MAJOR: the hybridize own-target guard name-matched only the caster's
  battlefield — a mirror match (both sides own a Dryad of the Ilysian
  Grove) misclassified an opponent-directed Beast Within as self-targeting.

Cube pipeline (game_1532532179492536430):
- C1: a player_loses early in one SBA batch discarded the same batch's
  creature_dies zone changes (CR 704.3 simultaneity).
- C2: Kokusho's dies drain was registered in the ETB registry — dead code
  for his self-death dispatch; every death was a real Tier 3 call.
- C3: the end-step scan printed "Resolved" even when every action was a
  no_action (the upkeep truthy-label sibling).
- C4: the cube deck builder's flat splash bonus admitted hard off-color
  pips into a two-color manabase (Bloodbraid Elf dead in a BG deck).

Limited (game_1532532194684436573):
- L1 CRITICAL: Tier-2 _exec_pump had no SBA chokepoint — Disfigure's -2/-2
  left a 1/1 alive at effective -1 toughness for three combats (the May 30
  D2 damage-path sibling).
- L2: ability-word prefixes ("Battalion — ") defeated the self-attack scan
  — the whole Battalion class was silently dropped. Scan strips the prefix
  + Boros Elite template.
- L3: autoplay's MAIN1-pass / skip-to-MAIN2 sites discarded advance_phase's
  messages (Leonin Vanguard's beginning-of-combat trigger invisible).

Madness/graveyard (game_1532532252825616466):
- M2: the ACTIVATABLE builder's cycling suppression matched Anje
  Falkenrath's real "{T}, Discard a card: Draw a card." — the madness
  commander was never offered an activation in 25 turns.
- M3 CRITICAL (reviewer-REPRODUCED): whole-oracle "sacrifice"+"end step"
  substring conjunction auto-sacrificed Herald of Anguish every end step
  AND suppressed his real discard trigger (substring family #6).
- M4: execute_claude_turn's discarded advance_phase returns also discarded
  the dies-queue drain messages (three Zulaport drains invisible).
- (Madness the MECHANIC is confirmed absent entirely — deferred to a
  focused session, tracked in CLAUDE.md.)
"""
import asyncio
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


def _lib():
    from rules.effect_templates import get_effect_library
    return get_effect_library()


# ---------------------------------------------------------------------------
# B1: animated lands in the delegated SBA adapter
# ---------------------------------------------------------------------------

class TestAnimatedLandSba:
    def test_animated_land_reads_effective_pt(self, game, make_card):
        from rules.sba_adapter import _compute_pt_for_sba
        land = make_card("Forest", type_line="Basic Land — Forest",
                         oracle_text="({T}: Add {G}.)",
                         power=None, toughness=None)
        land._animated_power = 2
        land._animated_toughness = 2
        land._animated_until_eot = True
        game.players[0].battlefield.append(land)
        kwargs = _compute_pt_for_sba(land, game)
        assert kwargs["base_toughness"] == 2, (
            "an animated 2/2 land must not parse as 0/0 in the delegated "
            "SBA checker (Sylvan Awakening wiped its own mana base)")

    def test_plain_land_still_parses_printed(self, game, make_card):
        from rules.sba_adapter import _compute_pt_for_sba
        land = make_card("Forest", type_line="Basic Land — Forest",
                         power=None, toughness=None)
        kwargs = _compute_pt_for_sba(land, game)
        assert kwargs["base_toughness"] == 0  # unanimated land — unchanged


# ---------------------------------------------------------------------------
# B2: Wrenn and Seven per-ability templates
# ---------------------------------------------------------------------------

class TestWrennAndSeven:
    ZERO_TEXT = "Put any number of land cards from your hand onto the battlefield tapped."
    PLUS_TEXT = ("Reveal the top four cards of your library. Put all land "
                 "cards revealed this way into your hand and the rest into "
                 "your graveyard.")
    MINUS3_TEXT = ('Create a green Treefolk creature token with reach and '
                   '"This token\'s power and toughness are each equal to the '
                   'number of lands you control."')

    def test_bare_name_key_deleted(self):
        assert "wrenn and seven" not in _lib()._card_templates, (
            "the bare key is what made every ability resolve to draw-2")

    def test_zero_ability_puts_lands_not_draws(self, game, make_card):
        rick = game.players[0]
        rick.hand.extend([
            make_card("Forest", type_line="Basic Land — Forest",
                      power=None, toughness=None),
            make_card("Mountain", type_line="Basic Land — Mountain",
                      power=None, toughness=None),
            make_card("Grizzly Bears"),
        ])
        from rules.effect_templates import build_game_context
        ctx = build_game_context(game, rick, game.players[1])
        actions, _desc = _lib().resolve_pw_ability(
            "Wrenn and Seven", self.ZERO_TEXT, "Rick", "Claude",
            game_context=ctx)
        assert actions, "the 0 ability must resolve via its own template"
        assert all(a["action"] != "draw_cards" for a in actions), (
            "the batch bug: [0] drew 2 cards instead of putting lands")
        moves = [a for a in actions if a["action"] == "move_card"]
        assert {a["card"] for a in moves} == {"Forest", "Mountain"}
        assert all(a["to_zone"] == "battlefield" for a in moves)
        taps = [a for a in actions if a["action"] == "tap"]
        assert {a["card"] for a in taps} == {"Forest", "Mountain"}

    def test_plus_one_splits_top_four(self, game, make_card):
        rick = game.players[0]
        rick.library = [
            make_card("Forest", type_line="Basic Land — Forest",
                      power=None, toughness=None),
            make_card("Grizzly Bears"),
            make_card("Island", type_line="Basic Land — Island",
                      power=None, toughness=None),
            make_card("Llanowar Elves"),
            make_card("Plains", type_line="Basic Land — Plains",
                      power=None, toughness=None),  # 5th — untouched
        ]
        from rules.effect_templates import build_game_context
        ctx = build_game_context(game, rick, game.players[1])
        actions, _desc = _lib().resolve_pw_ability(
            "Wrenn and Seven", self.PLUS_TEXT, "Rick", "Claude",
            game_context=ctx)
        assert actions and len(actions) == 4
        dests = {a["card"]: a["to_zone"] for a in actions}
        assert dests == {"Forest": "hand", "Grizzly Bears": "graveyard",
                         "Island": "hand", "Llanowar Elves": "graveyard"}

    def test_minus_three_token_scales_with_lands(self, game, make_card):
        rick = game.players[0]
        for _ in range(4):
            rick.battlefield.append(make_card(
                "Forest", type_line="Basic Land — Forest",
                power=None, toughness=None))
        from rules.effect_templates import build_game_context
        ctx = build_game_context(game, rick, game.players[1])
        actions, _desc = _lib().resolve_pw_ability(
            "Wrenn and Seven", self.MINUS3_TEXT, "Rick", "Claude",
            game_context=ctx)
        assert actions == [{"action": "create_token", "player": "Rick",
                            "name": "Treefolk", "power": 4, "toughness": 4,
                            "types": "Token Creature — Treefolk", "count": 1,
                            "keywords": ["reach"]}]


# ---------------------------------------------------------------------------
# B3: hybridize guard vs cross-controller same names
# ---------------------------------------------------------------------------

class TestHybridizeMirrorGuard:
    BEAST_WITHIN = ("Destroy target permanent. Its controller creates a 3/3 "
                    "green Beast creature token.")

    def _hand_with_beast_within(self, player, make_card):
        player.hand.append(make_card(
            "Beast Within", type_line="Instant", mana_cost="{2}{G}", cmc=3,
            oracle_text="Destroy target permanent. Its controller creates "
                        "a 3/3 green Beast creature token.",
            power=None, toughness=None))
        for _ in range(3):
            player.battlefield.append(make_card(
                "Forest", type_line="Basic Land — Forest",
                oracle_text="({T}: Add {G}.)", power=None, toughness=None))

    def test_mirror_name_not_rejected_as_own_target(self, game, make_card):
        from mtg.ai_turn import _validate_plan_mana
        rick, claude = game.players
        self._hand_with_beast_within(claude, make_card)
        # BOTH sides control a Dryad — the caster's copy must not shadow
        # the opponent's as the intended target. P/T 2/4 (sum 6 ≥ 4) so the
        # small-target heuristic doesn't reject either.
        rick.battlefield.append(make_card(
            "Dryad of the Ilysian Grove", power="2", toughness="4"))
        claude.battlefield.append(make_card(
            "Dryad of the Ilysian Grove", power="2", toughness="4"))
        plan = [{"type": "cast", "card": "Beast Within",
                 "target": "Dryad of the Ilysian Grove"}]
        validated = _validate_plan_mana(None, game, 1, plan)
        assert any(a.get("card") == "Beast Within" for a in validated), (
            "a mirror-named target must not be classified as own-targeting")

    def test_own_only_name_still_rejected(self, game, make_card):
        from mtg.ai_turn import _validate_plan_mana
        rick, claude = game.players
        self._hand_with_beast_within(claude, make_card)
        claude.battlefield.append(make_card(
            "Dryad of the Ilysian Grove", power="2", toughness="4"))
        plan = [{"type": "cast", "card": "Beast Within",
                 "target": "Dryad of the Ilysian Grove"}]
        validated = _validate_plan_mana(None, game, 1, plan)
        assert not any(a.get("card") == "Beast Within" for a in validated)


# ---------------------------------------------------------------------------
# C1: CR 704.3 — same-batch SBA mutations land before the loss break
# ---------------------------------------------------------------------------

class TestSbaBatchSimultaneity:
    def test_creature_dies_in_same_batch_as_player_loss(self, rules, game, make_card):
        rick, claude = game.players
        rick.life = 0
        dying = make_card("Blood Artist", power="0", toughness="1")
        dying.damage_marked = 3
        claude.battlefield.append(dying)
        msgs = rules.process_state_based_actions(game)
        assert game.ended, "the 0-life player must lose"
        assert dying not in claude.battlefield, (
            "CR 704.3: the same batch's lethal-damage death must land even "
            "though a player_loses is in the batch")
        assert dying in claude.graveyard


# ---------------------------------------------------------------------------
# C2: Kokusho lives in the dies registry
# ---------------------------------------------------------------------------

class TestKokushoDiesRegistry:
    ORACLE = ("Flying\nWhen this creature dies, each opponent loses 5 life. "
              "You gain life equal to the life lost this way.")

    def test_registered_in_dies_registry(self):
        lib = _lib()
        assert "kokusho, the evening star" in lib._dies_templates
        assert "kokusho, the evening star" not in lib._card_templates, (
            "the ETB-registry copy is what made the dies dispatch miss him")

    def test_self_death_resolves_at_tier_15(self):
        actions, _desc = _lib().resolve_etb(
            card_name="Kokusho, the Evening Star",
            oracle_text=self.ORACLE,
            controller="Rick", opponent="Claude",
            game_context={}, event_type="dies")
        assert actions == [
            {"action": "lose_life", "player": "Claude", "amount": 5},
            {"action": "gain_life", "player": "Rick", "amount": 5},
        ], "every Kokusho death was a real Tier 3 call before this"


# ---------------------------------------------------------------------------
# M3: end-step self-sacrifice needs ONE sentence, not a whole-oracle
# substring conjunction  +  C3: the truthy "Resolved" label
# ---------------------------------------------------------------------------

class TestEndStepScan:
    HERALD_ORACLE = ("Improvise\nFlying\nAt the beginning of your end step, "
                     "each opponent discards a card.\n{1}{B}, Sacrifice an "
                     "artifact: Target creature gets -2/-2 until end of turn.")
    BALL_LIGHTNING_ORACLE = ("Trample, haste\nAt the beginning of the end "
                             "step, sacrifice this creature.")

    def _run_scan(self, rules, game, card, capsys=None):
        from mtg.engine import GameEngine
        from mtg.triggers import _check_end_step_triggers_sync
        ge = GameEngine.__new__(GameEngine)
        ge.rules = rules
        return _check_end_step_triggers_sync(ge, game)

    def test_herald_not_auto_sacrificed(self, rules, game, make_card):
        herald = make_card("Herald of Anguish", oracle_text=self.HERALD_ORACLE,
                           power="5", toughness="5",
                           type_line="Creature — Phyrexian Demon")
        game.players[0].battlefield.append(herald)
        messages, _unhandled = self._run_scan(rules, game, herald)
        assert herald in game.players[0].battlefield, (
            "unrelated 'sacrifice' (cost) + 'end step' (discard trigger) "
            "clauses must not auto-sacrifice (reviewer-reproduced, x2 live)")
        assert not any("is sacrificed" in m for m in messages)

    def test_ball_lightning_still_sacrificed(self, rules, game, make_card):
        ball = make_card("Ball Lightning",
                         oracle_text=self.BALL_LIGHTNING_ORACLE,
                         power="6", toughness="1",
                         type_line="Creature — Elemental")
        game.players[0].battlefield.append(ball)
        messages, _unhandled = self._run_scan(rules, game, ball)
        assert any("Ball Lightning is sacrificed" in m for m in messages), (
            "the printed self-sacrifice class must keep working")

    def test_all_noop_template_prints_handled_noop(self, rules, game,
                                                   make_card, capsys):
        # Agent of Treachery's end-step template no-ops below 3 stolen
        # permanents — the label must say so, not claim "Resolved".
        agent = make_card(
            "Agent of Treachery",
            oracle_text=("When this creature enters, gain control of target "
                         "permanent.\nAt the beginning of your end step, if "
                         "you control three or more permanents you don't "
                         "own, draw three cards."),
            power="2", toughness="3", type_line="Creature — Human Rogue")
        game.players[0].battlefield.append(agent)
        self._run_scan(rules, game, agent)
        out = capsys.readouterr().out
        if "[ENDSTEP-TRIGGER]" in out and "Agent of Treachery" in out:
            assert "Resolved Agent of Treachery" not in out, (
                "an all-no_action resolution must not print 'Resolved'")


# ---------------------------------------------------------------------------
# L1: negative Tier-2 pump runs SBA
# ---------------------------------------------------------------------------

class TestNegativePumpSba:
    def test_disfigure_kills_one_one(self, rules, game, make_card):
        from rules.spell_resolver import SpellResolver
        from rules.effects import Effect, EffectType, ExecutionContext
        rick, claude = game.players
        hawk = make_card("Healer's Hawk", power="1", toughness="1")
        rick.battlefield.append(hawk)
        game._rules_engine = rules
        effect = Effect(effect_type=EffectType.PUMP,
                        power_mod=-2, toughness_mod=-2)
        ctx = ExecutionContext(game_state=game, source_card=None,
                               source_controller=claude, targets=[hawk])
        resolver = SpellResolver.__new__(SpellResolver)
        asyncio.run(resolver._exec_pump(effect, ctx, game))
        assert hawk not in rick.battlefield, (
            "a 1/1 at -2/-2 has toughness -1 — CR 704.5f, no combat needed "
            "(it survived three combats in the batch)")
        assert hawk in rick.graveyard

    def test_positive_pump_no_sba_needed(self, rules, game, make_card):
        from rules.spell_resolver import SpellResolver
        from rules.effects import Effect, EffectType, ExecutionContext
        rick = game.players[0]
        bear = make_card("Grizzly Bears")
        rick.battlefield.append(bear)
        game._rules_engine = rules
        effect = Effect(effect_type=EffectType.PUMP,
                        power_mod=2, toughness_mod=2)
        ctx = ExecutionContext(game_state=game, source_card=None,
                               source_controller=rick, targets=[bear])
        resolver = SpellResolver.__new__(SpellResolver)
        asyncio.run(resolver._exec_pump(effect, ctx, game))
        assert bear in rick.battlefield


# ---------------------------------------------------------------------------
# L2: Battalion reaches the scan + the Boros Elite template
# ---------------------------------------------------------------------------

class TestBattalion:
    ORACLE = ("Battalion — Whenever this creature and at least two other "
              "creatures attack, this creature gets +2/+2 until end of turn.")

    def test_scan_matches_battalion_paragraph(self, make_card):
        from mtg.triggers import _is_self_attack_trigger_paragraph
        elite = make_card("Boros Elite", oracle_text=self.ORACLE)
        assert _is_self_attack_trigger_paragraph(elite, self.ORACLE), (
            "ability-word prefixes are flavor (CR 207.2c) — the scan must "
            "see through them")

    def test_plain_attack_trigger_still_matches(self, make_card):
        from mtg.triggers import _is_self_attack_trigger_paragraph
        card = make_card("Hellrider")
        assert _is_self_attack_trigger_paragraph(
            card, "Whenever this creature attacks, do a thing.")

    def test_template_fires_at_three_attackers(self, game, make_card):
        rick = game.players[0]
        for name in ("Boros Elite", "Healer's Hawk", "Leonin Vanguard"):
            rick.battlefield.append(make_card(name, attacking=True))
        actions, _desc = _lib().resolve_attack_trigger(
            trigger_card_name="Boros Elite",
            trigger_oracle=self.ORACLE,
            attacking_creature_name="Boros Elite",
            attacking_creature_power=1,
            controller="Rick", opponent="Claude",
            game_context={"_controller_player": rick})
        assert actions == [{"action": "pump_all_creatures", "player": "Rick",
                            "card": "Boros Elite", "power": 2,
                            "toughness": 2}]

    def test_template_noop_below_three(self, game, make_card):
        rick = game.players[0]
        for name in ("Boros Elite", "Healer's Hawk"):
            rick.battlefield.append(make_card(name, attacking=True))
        actions, _desc = _lib().resolve_attack_trigger(
            trigger_card_name="Boros Elite",
            trigger_oracle=self.ORACLE,
            attacking_creature_name="Boros Elite",
            attacking_creature_power=1,
            controller="Rick", opponent="Claude",
            game_context={"_controller_player": rick})
        assert actions == [{"action": "no_action",
                            "reason": "Battalion: fewer than three attackers"}]


# ---------------------------------------------------------------------------
# L3 + M4: no discarded advance_phase returns (structural)
# ---------------------------------------------------------------------------

class TestNoDiscardedPhaseMessages:
    @pytest.mark.parametrize("relpath", ["mtg/ai_turn.py", "mtg/autoplay.py"])
    def test_no_bare_advance_phase_statements(self, relpath):
        src = (REPO / relpath).read_text(encoding="utf-8")
        bare = re.findall(
            r"^\s*(?:cog\.)?engine\.advance_phase\(game\)\s*(?:#.*)?$",
            src, re.M)
        assert not bare, (
            f"{relpath}: {len(bare)} advance_phase call(s) discard the "
            f"returned messages — phase-entry trigger output (Leonin "
            f"Vanguard, dies-queue drains) silently never reaches Discord. "
            f"Capture and send/extend them.")


# ---------------------------------------------------------------------------
# M2: Anje's real ability survives the cycling suppression (structural)
# ---------------------------------------------------------------------------

class TestAnjeActivatableSurfaces:
    def test_cycling_suppression_is_narrow(self):
        src = (REPO / "mtg/claude_player.py").read_text(encoding="utf-8")
        assert "'discard this card' in cost_part" in src, (
            "the cycling suppression must match cycling's exact reminder "
            "cost — a bare 'discard' also matched Anje Falkenrath's real "
            "battlefield ability and hid the madness commander for 25 turns")
        assert re.search(r"^\s*if 'discard' in cost_part and", src, re.M) is None


# ---------------------------------------------------------------------------
# C4: cube deck builder rejects hard off-color pips
# ---------------------------------------------------------------------------

class TestCubeSplashBuild:
    def test_hard_off_pip_excluded(self):
        # Pool shape matters: with more nonlands than deck slots, SCORE
        # decides inclusion. The off-pip card carries removal text so the
        # pre-fix "splash +2" ranked it ABOVE the on-color filler — the
        # mutant (no strict_off_pip) must therefore include it and fail.
        from cube_draft import auto_build_deck
        from mtg.models import Card
        pool = []
        for i in range(20):
            pool.append(Card(name=f"Bear {i}", mana_cost="{1}{B}" if i % 2 else "{1}{G}",
                             type_line="Creature — Bear",
                             oracle_text="", power="2", toughness="2"))
        for i in range(4):
            pool.append(Card(name=f"Ritual {i}", mana_cost="{1}{B}",
                             type_line="Sorcery",
                             oracle_text="", power=None, toughness=None))
        bbe = Card(name="Bloodbraid Elf", mana_cost="{2}{R}{G}",
                   type_line="Creature — Elf Berserker",
                   oracle_text="Destroy target creature.",
                   power="3", toughness="2")
        pool.append(bbe)
        deck, _sideboard = auto_build_deck(pool)
        assert bbe not in deck, (
            "a hard {R} pip in a BG build with no red sources is a "
            "permanently dead card (30 turns in the batch)")
        # Sanity: the pool genuinely overflowed the nonland slots, so score
        # (not pool arithmetic) made the exclusion.
        assert len(pool) > 23
