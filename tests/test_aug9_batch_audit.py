"""Mutation-sensitive regressions from the Aug 9, 2026 third-confirmation
batch audit (sha=4a80eb9 corpus, 160 games).

Finding IDs (A-*, B-*, C-F*, CO-*) reference the Aug 9 findings ledger in
CLAUDE.md. Fixtures are LIVE-SHAPED (the pin-shape-reachability ledger):
oracle text comes from data/card_data_cache.json, watcher scans run through
the PERMANENT_ENTERED bus emit, payments run through the real tap engine.
"""

import asyncio
import json
import re
from pathlib import Path

import pytest

from mtg import events
from mtg.constants import Phase
from mtg.models import Card, GameState, Player

_ROOT = Path(__file__).resolve().parent.parent
_CACHE = json.loads(
    (_ROOT / "data" / "card_data_cache.json").read_text(encoding="utf-8"))


def _cached_oracle(name: str) -> str:
    return _CACHE[name.lower()].get("oracle_text", "") or ""


def _engine():
    from mtg.engine import GameEngine
    return GameEngine(None)


# ---------------------------------------------------------------------------
# A-1: Phyrexian mana in activated-ability costs was FREE
# ---------------------------------------------------------------------------

class TestPhyrexianActivationCosts:
    def _player_with(self, make_game, make_card, land_names):
        game = make_game()
        player = game.players[0]
        for name in land_names:
            land = make_card(name, type_line="Basic Land — " + name,
                             oracle_text="", power=None, toughness=None)
            player.battlefield.append(land)
        return game, player

    def test_unpaid_phyrexian_pip_demands_its_color(self, make_game, make_card):
        # Live shape: Hex Parasite {X}{B/P} at X=0 with the flag False
        # (every activation site pre-fix). The pip must cost ONE black
        # mana — the batch showed "Tapped 0 sources".
        game, player = self._player_with(make_game, make_card, ["Swamp"])
        ok = player.tap_sources_for_cost("{X}{B/P}", game=game, x_value=0,
                                        pay_phyrexian_with_life=False)
        assert ok, "a Swamp can pay the {B/P} pip with mana"
        assert sum(1 for c in player.battlefield if c.tapped) == 1, (
            "the unpaid Phyrexian pip must tap exactly one source — "
            "pre-fix it contributed zero to total_cost (free activation)")

    def test_unpaid_phyrexian_pip_rejects_wrong_color(self, make_game, make_card):
        game, player = self._player_with(make_game, make_card, ["Plains"])
        ok = player.tap_sources_for_cost("{X}{B/P}", game=game, x_value=0,
                                        pay_phyrexian_with_life=False)
        assert not ok, (
            "with no black source and no life option the {B/P} pip is "
            "unpayable — pre-fix this returned True for free")

    def test_phyrexian_life_payment_at_high_life(self, make_game, make_card):
        game, player = self._player_with(make_game, make_card, ["Plains"])
        start = player.life
        ok = player.tap_sources_for_cost("{X}{B/P}", game=game, x_value=0,
                                        pay_phyrexian_with_life=True)
        assert ok
        assert player.life == start - 2, "life option pays 2 life"
        assert sum(1 for c in player.battlefield if c.tapped) == 0

    def test_phyrexian_at_low_life_demands_color_not_free(
            self, make_game, make_card):
        # The latent CAST-path hole the verifier found: flag True but
        # life <= 4 declines the life payment — the pip then fell through
        # the same no-else chain and was free.
        game, player = self._player_with(make_game, make_card, ["Swamp"])
        player.life = 3
        ok = player.tap_sources_for_cost("{X}{B/P}", game=game, x_value=0,
                                        pay_phyrexian_with_life=True)
        assert ok
        assert player.life == 3, "life must NOT be paid at life <= 4"
        assert sum(1 for c in player.battlefield if c.tapped) == 1, (
            "declined life payment must fall back to demanding the color")

    def test_low_life_no_color_source_is_unpayable(self, make_game, make_card):
        game, player = self._player_with(make_game, make_card, ["Plains"])
        player.life = 3
        ok = player.tap_sources_for_cost("{X}{B/P}", game=game, x_value=0,
                                        pay_phyrexian_with_life=True)
        assert not ok, (
            "life declined (<=4) + no black source = unpayable, not free")

    def test_advertising_matches_payment_at_low_life(self, make_game, make_card):
        # Adversarial-review residual (A-1): the validator advertised the
        # life option at life 3-4 while the payer declines it below 5 — a
        # doomed-gate window (advertised-castable, unpayable). Thresholds
        # aligned: both refuse at 3-4 with no color source.
        game, player = self._player_with(make_game, make_card, ["Plains"])
        for life in (3, 4):
            player.life = life
            ok, _ = player.can_pay_mana_cost("{U/P}")
            assert not ok, (
                f"at life {life} with no U source the gate must refuse — "
                f"the payer will decline the life option")
        player.life = 5
        ok, _ = player.can_pay_mana_cost("{U/P}")
        assert ok, "at life 5 the life option is genuinely payable"

    def test_activation_sites_pass_the_flag(self):
        # The two-paths-divergence pin: the AI activation payment
        # (engine.py) AND both manual-path twins (cog.py equip + activate)
        # must carry the cast path's '/P}' detection.
        engine_src = (_ROOT / "mtg" / "engine.py").read_text(encoding="utf-8")
        cog_src = (_ROOT / "mtg" / "cog.py").read_text(encoding="utf-8")
        needle = "pay_phyrexian_with_life='/P}' in mana_cost.upper()"
        assert engine_src.count(needle) >= 1, "AI activation payment site"
        assert cog_src.count(needle) >= 2, "manual !activate + equip twins"


# ---------------------------------------------------------------------------
# B-1: cascaded / Chaos-Warped planeswalkers entered at 0 loyalty
# ---------------------------------------------------------------------------

class TestFreeCastPlaneswalkerLoyalty:
    def test_current_loyalty_attribute_is_extinct(self):
        # The phantom attribute had exactly two writers and ZERO readers.
        # Any reappearance re-creates the class (a write nothing reads).
        for rel in ("mtg", "rules"):
            for path in (_ROOT / rel).glob("*.py"):
                src = path.read_text(encoding="utf-8")
                assert not re.search(r"\.current_loyalty\s*=", src), (
                    f"{path.name}: current_loyalty writer reappeared — "
                    "loyalty state lives in loyalty_counters (mtg/sba.py "
                    "reads it; CR 306.8)")

    def test_pw_with_loyalty_counters_survives_sba(self, make_game, make_card):
        # The consequence pin: a free-cast PW written the NEW way survives
        # the SBA that killed the cascaded Liliana.
        engine = _engine()
        game = make_game()
        game._rules_engine = engine.rules
        pw = make_card("Liliana of the Veil",
                       type_line="Legendary Planeswalker — Liliana",
                       oracle_text=_cached_oracle("Liliana of the Veil"),
                       power=None, toughness=None)
        pw.loyalty = "3"
        from mtg.helpers import loyalty_from_commander_casts
        pw.loyalty_counters = int(pw.loyalty) + loyalty_from_commander_casts(
            game, game.players[0], pw)
        game.players[0].battlefield.append(pw)
        engine.check_state_based_actions(game)
        assert pw in game.players[0].battlefield, (
            "a planeswalker entering with printed loyalty must survive the "
            "PLANESWALKER_ZERO_LOYALTY check")

    def test_pw_without_loyalty_counters_dies_to_sba(self, make_game, make_card):
        # The negative control — demonstrates the exact pre-fix death.
        engine = _engine()
        game = make_game()
        game._rules_engine = engine.rules
        pw = make_card("Liliana of the Veil",
                       type_line="Legendary Planeswalker — Liliana",
                       oracle_text="", power=None, toughness=None)
        pw.loyalty = "3"  # printed loyalty present, counters never written
        game.players[0].battlefield.append(pw)
        engine.check_state_based_actions(game)
        assert pw not in game.players[0].battlefield, (
            "sanity: with loyalty_counters unset the SBA kills it — this is "
            "what happened to every cascaded planeswalker pre-fix")


# ---------------------------------------------------------------------------
# A-2: Species Specialist drew on creature-ENTERS (cross-sentence substring)
# ---------------------------------------------------------------------------

class TestCreatureEntersDetectionIsSentenceScoped:
    def _board(self, make_game, make_card, watcher_name, watcher_oracle):
        engine = _engine()
        game = make_game()
        game._rules_engine = engine.rules
        rick, claude = game.players
        watcher = make_card(watcher_name, power="2", toughness="3",
                            oracle_text=watcher_oracle)
        claude.battlefield.append(watcher)
        return engine, game, rick, claude

    def test_species_specialist_not_collected_on_entry(
            self, make_game, make_card):
        # Live shape: game_1535590294320582736 — 4 unearned draws, one per
        # unrelated creature ENTRY (chosen type Dragon, nothing died).
        engine, game, rick, claude = self._board(
            make_game, make_card, "Species Specialist",
            _cached_oracle("Species Specialist"))
        claude.library = [Card(id="lib1", name="L1"), Card(id="lib2", name="L2")]
        hand_before = len(claude.hand)
        bear = make_card("Bear", power="2", toughness="2")
        rick.battlefield.append(bear)
        events.emit(events.PERMANENT_ENTERED, game, card=bear,
                    controller=rick, via="test", rules=engine.rules)
        assert len(claude.hand) == hand_before, (
            "Species Specialist's trigger is on chosen-type DEATHS — a "
            "creature ENTERING must not draw (the cross-sentence substring "
            "match collected it as an enters-watcher)")

    def test_soul_warden_still_fires(self, make_game, make_card):
        # The over-narrowing guard: the period-scoped regex must keep
        # collecting real enters-watchers.
        engine, game, rick, claude = self._board(
            make_game, make_card, "Soul Warden",
            "Whenever another creature enters, you gain 1 life.")
        bear = make_card("Bear", power="2", toughness="2")
        rick.battlefield.append(bear)
        events.emit(events.PERMANENT_ENTERED, game, card=bear,
                    controller=rick, via="test", rules=engine.rules)
        assert claude.life == 41, "Soul Warden must still be collected"

    def test_bastion_of_remembrance_not_an_enters_watcher(
            self, make_game, make_card):
        # Sibling mis-collection the bulk sweep surfaced: Bastion's trigger
        # is "Whenever a creature you control DIES"; its ETB-token sentence
        # supplied the cross-sentence 'enters'.
        engine, game, rick, claude = self._board(
            make_game, make_card, "Bastion of Remembrance",
            _cached_oracle("Bastion of Remembrance"))
        life_before = rick.life
        bear = make_card("Bear", power="2", toughness="2")
        claude.battlefield.append(bear)
        events.emit(events.PERMANENT_ENTERED, game, card=bear,
                    controller=claude, via="test", rules=engine.rules)
        assert rick.life == life_before, (
            "Bastion's dies-drain must not fire on an ENTRY")


# ---------------------------------------------------------------------------
# CO-2: "target nonland permanent" accepted LANDS (union-sweep type leak)
# ---------------------------------------------------------------------------

class TestNonlandPermanentRestriction:
    def _parse(self, card_name):
        from rules.targeting_helpers import _parse_target_restriction_from_oracle
        from types import SimpleNamespace
        oracle = _cached_oracle(card_name)
        assert oracle, f"cache must carry {card_name}"
        return _parse_target_restriction_from_oracle(
            SimpleNamespace(oracle_text=oracle, name=card_name))

    def test_nonland_only_cards_drop_the_broad_permanent_type(self):
        # The union sweep tokenized 'permanent' out of "nonland permanent"
        # and added the broad PERMANENT type — an OR over target_types, so
        # a Land passed (Into the Roil bounced Academy Ruins,
        # game_1535595295201824808). Real cache oracles: the reminder/kicker
        # text is what makes the capture non-trivial.
        from rules.targeting import TargetType
        for name in ("Into the Roil", "Cyclonic Rift", "Detention Sphere"):
            r = self._parse(name)
            assert TargetType.NONLAND_PERMANENT in r.target_types, name
            assert TargetType.PERMANENT not in r.target_types, (
                f"{name}: the broad PERMANENT type must be subsumed — "
                f"it lets a LAND satisfy a nonland restriction")

    def test_separate_unqualified_permanent_phrase_is_kept(self):
        # Controls: a genuinely unqualified "target permanent" phrase
        # elsewhere on the card must keep the broad type (Teferi Hero's -8
        # emblem; Beast Within's only clause is unqualified).
        from rules.targeting import TargetType
        r = self._parse("Teferi, Hero of Dominaria")
        assert TargetType.PERMANENT in r.target_types
        r2 = self._parse("Beast Within")
        assert TargetType.PERMANENT in r2.target_types

    def test_declared_land_rejected_at_cast(self, make_game, make_card):
        # End-to-end: the live shape — Into the Roil with a declared LAND
        # target must be rejected by _validate_cast (CR 601.2c/115.4).
        from mtg.engine import GameEngine
        from mtg.spells import _validate_cast
        game = make_game()
        engine = GameEngine(None)
        game._rules_engine = engine.rules
        engine.rules.engine_ref = engine
        rick, claude = game.players
        ruins = make_card("Academy Ruins",
                          type_line="Legendary Land",
                          oracle_text="", power=None, toughness=None)
        rick.battlefield.append(ruins)
        # A legal nonland target must EXIST (else the zero-target gate fires
        # first and the declared-target branch is never reached).
        claude.battlefield.append(
            make_card("Everflowing Chalice", type_line="Artifact",
                      oracle_text="", power=None, toughness=None))
        roil = make_card("Into the Roil", type_line="Instant",
                         oracle_text=_cached_oracle("Into the Roil"), cmc=2)
        claude.hand.append(roil)
        rejection, _, _ = _validate_cast(engine, game, claude, roil, ruins)
        assert rejection is not None and rejection[0] is False, (
            "a declared LAND must be an illegal target for 'return target "
            "nonland permanent'")

    def test_bounce_template_declines_a_land_name(self, lib, make_game, make_card):
        # Second net: even if a land name reaches the template, it declines.
        game = make_game()
        rick, claude = game.players
        ruins = make_card("Academy Ruins", type_line="Legendary Land",
                          oracle_text="", power=None, toughness=None)
        rick.battlefield.append(ruins)
        from rules.effect_templates import build_game_context
        ctx = build_game_context(game, claude, rick)
        ctx['explicit_target_name'] = "Academy Ruins"
        ctx['_oracle'] = _cached_oracle("Into the Roil").lower()
        actions, _ = lib.resolve_spell(
            "Into the Roil", _cached_oracle("Into the Roil"),
            claude.name, rick.name, game_context=ctx)
        assert actions is not None
        assert all(a.get("action") != "move_card" for a in actions), (
            f"the bounce template must decline a LAND target: {actions}")


# ---------------------------------------------------------------------------
# C-F2-1: Trickbind's Split Second reminder defeated the ability-only gate
# ---------------------------------------------------------------------------

class TestReminderTextStrippedFromCounterGates:
    def _stack_with_creature_spell(self, make_game, make_card):
        from mtg.engine import GameEngine
        from mtg.models import StackEntry
        game = make_game()
        engine = GameEngine(None)
        game._rules_engine = engine.rules
        engine.rules.engine_ref = engine
        rick, claude = game.players
        recruiter = make_card("Recruiter of the Guard",
                              type_line="Creature — Human Soldier",
                              power="1", toughness="1")
        game.stack.append(StackEntry(card=recruiter, controller_name=rick.name,
                                     controller_index=0))
        for i in range(3):
            claude.battlefield.append(make_card(
                f"Island{i}", type_line="Basic Land — Island",
                oracle_text="", power=None, toughness=None))
        return game, engine, rick, claude

    def test_trickbind_rejected_at_spell_only_stack(self, make_game, make_card):
        # Live shape: game_1535590382417612871 — Trickbind (ability-only
        # counter) was cast at a stack holding only Recruiter of the Guard
        # and countered the creature SPELL. Verbatim cache oracle — the
        # Split Second reminder IS the bug (a fixture without it tests
        # nothing).
        from mtg.spells import _validate_cast
        game, engine, rick, claude = self._stack_with_creature_spell(
            make_game, make_card)
        trickbind = make_card("Trickbind", type_line="Instant",
                              oracle_text=_cached_oracle("Trickbind"), cmc=2)
        claude.hand.append(trickbind)
        rejection, _, _ = _validate_cast(engine, game, claude, trickbind, None)
        assert rejection is not None and rejection[0] is False, (
            "Trickbind counters only abilities — with no ability on the "
            "stack the cast must be rejected (CR 601.2c)")
        assert "abilit" in rejection[1].lower()

    def test_disallow_still_casts_at_a_spell(self, make_game, make_card):
        # Control: Disallow genuinely counters spells and must keep
        # skipping the gate.
        from mtg.spells import _validate_cast
        game, engine, rick, claude = self._stack_with_creature_spell(
            make_game, make_card)
        disallow = make_card("Disallow", type_line="Instant",
                             oracle_text=_cached_oracle("Disallow"), cmc=3)
        claude.hand.append(disallow)
        rejection, _, _ = _validate_cast(engine, game, claude, disallow, None)
        assert rejection is None, rejection

    def test_resolution_fallback_fizzles_for_trickbind(self, make_game, make_card):
        # The resolution half: even arriving at counter_ability with only a
        # spell on the stack, Trickbind's fallback must fizzle, not counter.
        from mtg.actions import execute_action_on_state
        from mtg.engine import GameEngine
        from mtg.models import StackEntry
        game = make_game()
        engine = GameEngine(None)
        game._rules_engine = engine.rules
        rick, claude = game.players
        recruiter = make_card("Recruiter of the Guard",
                              type_line="Creature — Human Soldier",
                              power="1", toughness="1")
        entry = StackEntry(card=recruiter, controller_name=rick.name,
                           controller_index=0)
        game.stack.append(entry)
        msg = execute_action_on_state(engine.rules, game, {
            "action": "counter_ability",
            "_source_oracle": _cached_oracle("Trickbind"),
            "_source_controller": claude.name,
        })
        assert not getattr(entry, 'countered', False), (
            f"Trickbind must NOT counter a spell: {msg}")
        assert "fizzles" in (msg or "")

    def test_strip_reminder_text_helper(self):
        from mtg.helpers import strip_reminder_text
        t = _cached_oracle("Trickbind")
        stripped = strip_reminder_text(t).lower()
        assert "spell" not in stripped, (
            "Trickbind's only 'spell' words live in reminder text")
        te = strip_reminder_text(_cached_oracle("Tale's End")).lower()
        assert "legendary spell" in te, (
            "Tale's End's legendary-spell phrase is rules text and must "
            "survive the strip")


# ---------------------------------------------------------------------------
# C-F1-1: type-restricted self-target clauses missed by the literal check
# ---------------------------------------------------------------------------

class TestSelfTargetClauseDetection:
    def test_restriction_shapes(self):
        # Import the PRODUCTION regex — a re-expressed copy is a comment.
        from mtg.engine import _SELF_TARGET_CLAUSE_RE as R
        assert R.search(_cached_oracle("Restoration Angel").lower()), (
            "'target non-Angel creature you control' is a self-target clause")
        assert R.search("exile target creature you control")
        assert not R.search("destroy target creature"), "plain removal"
        assert not R.search("gain control of target creature you don't control"), (
            "a don't-control phrase must not read as self-target")
        assert not R.search(
            "exile target creature that's attacking you or a planeswalker "
            "you control"), (
            "Soul Snare: 'you control' describes the defender, not the target")


# ---------------------------------------------------------------------------
# CO-3a: a model-fabricated `resolve` after play_land executed via Tier 3
# ---------------------------------------------------------------------------

class TestResolveAfterPlayLandIsDropped:
    def test_resolve_following_play_land_dropped_with_reason(
            self, make_game, make_card):
        # Live shape: game_1535595295201824808 turn 26 — after a successful
        # play_land the model emitted {"type": "resolve", "description":
        # "Mystic Sanctuary ETB — exile cards from graveyard..."} (a
        # FABRICATED effect) and Tier 3 executed it: 5 cards exiled from
        # the caster's own graveyard.
        from mtg.engine import GameEngine
        engine = GameEngine(None)
        game = make_game()
        game._rules_engine = engine.rules
        engine.rules.engine_ref = engine
        game.phase = Phase.MAIN2
        game.active_player_index = 1
        player = game.players[1]
        land = make_card("Mystic Sanctuary", type_line="Land — Island",
                         oracle_text=_cached_oracle("Mystic Sanctuary"),
                         power=None, toughness=None)
        player.hand.append(land)
        gy = [make_card(f"Spell{i}", type_line="Instant", oracle_text="")
              for i in range(5)]
        player.graveyard.extend(gy)
        r1 = asyncio.run(engine._execute_action(
            game, 1, {"type": "play_land", "card": "Mystic Sanctuary"}))
        assert r1 and "played" in r1
        gy_before = len(player.graveyard)
        r2 = asyncio.run(engine._execute_action(
            game, 1, {"type": "resolve",
                      "description": "Mystic Sanctuary ETB — exile cards from "
                                     "graveyard to find a card."}))
        assert r2 is None, f"the paired resolve must be dropped: {r2}"
        assert len(player.graveyard) == gy_before, (
            "the fabricated resolve must not touch the graveyard")
        stash = getattr(game, '_last_resolve_drop_reason', None)
        assert stash and "play_land" in stash[1], (
            "the drop must stash a teaching reason naming the land play")

    def test_pending_resolves_hint_channel_untouched(self, make_game, make_card):
        # The verifier's protection: _handle_land_etb's pending_resolves
        # (the human !judge hint flow, e.g. Sejiri Steppe) is a DIFFERENT
        # channel from the model's plan action and must survive.
        from mtg.engine import GameEngine
        engine = GameEngine(None)
        game = make_game()
        game._rules_engine = engine.rules
        engine.rules.engine_ref = engine
        player = game.players[1]
        steppe = make_card("Sejiri Steppe", type_line="Land",
                           oracle_text="Sejiri Steppe enters the battlefield "
                                       "tapped.\nWhen Sejiri Steppe enters, "
                                       "target creature you control gains "
                                       "protection from a color until end of "
                                       "turn.", power=None, toughness=None)
        steppe.tapped = True
        player.battlefield.append(steppe)
        engine._handle_land_etb(game, player, steppe)
        assert game.pending_resolves, (
            "the Sejiri hint must still queue via pending_resolves")


# ---------------------------------------------------------------------------
# CO-3b: Mystic Sanctuary's template was unreachable (checkland exclusion)
# ---------------------------------------------------------------------------

class TestMysticSanctuaryLandEtb:
    def _board(self, make_game, make_card, tapped):
        from mtg.engine import GameEngine
        engine = GameEngine(None)
        game = make_game()
        game._rules_engine = engine.rules
        engine.rules.engine_ref = engine
        player = game.players[1]
        for i in range(3):
            isl = make_card("Island", type_line="Basic Land — Island",
                            oracle_text="", power=None, toughness=None)
            player.battlefield.append(isl)
        opt = make_card("Opt", type_line="Instant",
                        oracle_text="Scry 1.\nDraw a card.")
        player.graveyard.append(opt)
        sanctuary = make_card("Mystic Sanctuary", type_line="Land — Island",
                              oracle_text=_cached_oracle("Mystic Sanctuary"),
                              power=None, toughness=None)
        sanctuary.tapped = tapped
        player.battlefield.append(sanctuary)
        return engine, game, player, sanctuary, opt

    def test_untapped_entry_fires_the_template(self, make_game, make_card):
        engine, game, player, sanctuary, opt = self._board(
            make_game, make_card, tapped=False)
        engine._handle_land_etb(game, player, sanctuary)
        assert opt not in player.graveyard, (
            "Mystic Sanctuary entering untapped must put the graveyard "
            "instant on top of the library (the registered template was "
            "unreachable behind the checkland exclusion)")
        assert player.library and player.library[0].name == "Opt"

    def test_tapped_entry_does_not_fire(self, make_game, make_card):
        # CR 603.4 — the trigger reads "When this land enters UNTAPPED".
        engine, game, player, sanctuary, opt = self._board(
            make_game, make_card, tapped=True)
        engine._handle_land_etb(game, player, sanctuary)
        assert opt in player.graveyard, (
            "a tapped entry must NOT fire the 'enters untapped' trigger")


# ---------------------------------------------------------------------------
# C-F4-1: Huntmaster of the Fells' template implemented RAVAGER's effect
# ---------------------------------------------------------------------------

class TestHuntmasterTemplate:
    def test_wolf_and_life_not_damage(self, lib, make_game, make_card):
        # Real oracle (cache): "...create a 2/2 green Wolf creature token
        # and you gain 2 life." The old template dealt 2 damage to the
        # opponent — the OTHER face's transform trigger, written from
        # memory (the FP-ledger oracle rule, in template form).
        from rules.effect_templates import build_game_context
        game = make_game()
        rick, claude = game.players
        ctx = build_game_context(game, claude, rick)
        actions, _ = lib.resolve_etb(
            "Huntmaster of the Fells",
            _cached_oracle("Huntmaster of the Fells"),
            claude.name, rick.name, game_context=ctx)
        assert actions, "the name-keyed template must match"
        kinds = [a.get("action") for a in actions]
        assert "create_token" in kinds and "gain_life" in kinds
        assert "deal_damage" not in kinds, (
            "the 2-damage half belongs to Ravager of the Fells")
        tok = next(a for a in actions if a["action"] == "create_token")
        assert (tok["power"], tok["toughness"]) == (2, 2)
        assert "Wolf" in tok["types"]
        # Execute against real state — the shipped-vocabulary rule (a
        # template emitting keys nothing consumes is a silent no-op).
        from mtg.rules_engine import RulesEngine
        rules = RulesEngine(None)
        for a in actions:
            rules._execute_action_on_state(game, a)
        assert any(c.name == "Wolf" for c in claude.battlefield)
        assert claude.life == 42


# ---------------------------------------------------------------------------
# CO-4: Daretti -2 reanimated with NO sacrifice ("If you do" violated)
# ---------------------------------------------------------------------------

class TestDarettiWeldConditional:
    def _ctx(self, make_game, make_card, own_artifact, opp_artifact):
        from rules.effect_templates import build_game_context
        game = make_game()
        rick, claude = game.players
        if own_artifact:
            rick.battlefield.append(make_card(
                "Astral Cornucopia", type_line="Artifact",
                oracle_text="", power=None, toughness=None))
        if opp_artifact:
            claude.battlefield.append(make_card(
                "Lightning Greaves", type_line="Artifact — Equipment",
                oracle_text="", power=None, toughness=None))
        rick.graveyard.append(make_card(
            "Blightsteel Colossus",
            type_line="Artifact Creature — Phyrexian Golem",
            power="11", toughness="11"))
        return game, rick, build_game_context(game, rick, claude)

    def _weld(self, lib, ctx):
        return lib._pw_ability_templates[
            ("daretti", "sacrifice an artifact")].action_generator(
            "Rick", "Claude", ctx)

    def test_no_own_artifact_means_no_reanimation(self, lib, make_game, make_card):
        # Live shape: game_1535567121029931081 — "no artifact to sacrifice"
        # AND a free Blightsteel Colossus. The opponent's Lightning Greaves
        # was the auto-target — you can only sacrifice YOUR OWN permanents,
        # so this fixture is the one that must DIVERGE on the gate.
        game, rick, ctx = self._ctx(make_game, make_card,
                                    own_artifact=False, opp_artifact=True)
        actions = self._weld(lib, ctx)
        kinds = [a.get("action") for a in actions]
        assert "reanimate" not in kinds, (
            '"Sacrifice an artifact. If you do, ..." — no sacrifice, no '
            'return (a free 11/11 infect body pre-fix)')
        assert kinds == ["no_action"]

    def test_own_artifact_emits_both_halves(self, lib, make_game, make_card):
        game, rick, ctx = self._ctx(make_game, make_card,
                                    own_artifact=True, opp_artifact=False)
        actions = self._weld(lib, ctx)
        kinds = [a.get("action") for a in actions]
        assert "sacrifice_permanent" in kinds and "reanimate" in kinds


# ---------------------------------------------------------------------------
# CO-1: Inventors' Fair upkeep life-gain could NEVER fire
# ---------------------------------------------------------------------------

class TestInventorsFairUpkeep:
    def _resolve(self, lib, make_game, make_card, artifacts, phased=0):
        # MUST go through build_game_context — a hand-built ctx that sets
        # the old producer-less keys would pass while production fails
        # (which is exactly how this bug survived).
        from rules.effect_templates import build_game_context
        game = make_game()
        rick, claude = game.players
        for i in range(artifacts):
            a = make_card(f"Rock{i}", type_line="Artifact",
                          oracle_text="", power=None, toughness=None)
            if i < phased:
                a._phased_out = True
            rick.battlefield.append(a)
        ctx = build_game_context(game, rick, claude)
        return lib.resolve_upkeep_trigger(
            trigger_card_name="Inventors' Fair",
            trigger_oracle="At the beginning of your upkeep, if you control "
                           "three or more artifacts, you gain 1 life.",
            controller=rick.name, opponent=claude.name, game_context=ctx), rick

    def test_three_artifacts_gain_life(self, lib, make_game, make_card):
        (actions, _), rick = self._resolve(lib, make_game, make_card, 3)
        assert actions and actions[0].get("action") == "gain_life", (
            "3 artifacts must gain 1 life — the old ctx keys had no "
            "producer, so the count was ALWAYS 0")

    def test_two_artifacts_no_op(self, lib, make_game, make_card):
        (actions, _), rick = self._resolve(lib, make_game, make_card, 2)
        assert actions and actions[0].get("action") == "no_action"
        assert "2 artifact" in actions[0].get("reason", "")

    def test_phased_out_artifact_does_not_count(self, lib, make_game, make_card):
        (actions, _), rick = self._resolve(lib, make_game, make_card, 3,
                                           phased=1)
        assert actions and actions[0].get("action") == "no_action", (
            "a phased-out artifact is treated as nonexistent (CR 702.26b)")


# ---------------------------------------------------------------------------
# B-2: Prismatic Ending's converge bound was skipped when mv was unknown
# ---------------------------------------------------------------------------

class TestPrismaticEndingBound:
    def _resolve(self, lib, make_game, make_card, colors_spent,
                 opp_cards, explicit=None):
        from rules.effect_templates import build_game_context
        game = make_game()
        rick, claude = game.players
        for name, tl, cmc in opp_cards:
            c = make_card(name, type_line=tl, power="3", toughness="3")
            c.cmc = cmc
            claude.battlefield.append(c)
        ctx = build_game_context(game, rick, claude)
        ctx['colors_spent'] = colors_spent
        if explicit:
            ctx['explicit_target_name'] = explicit
        return lib.resolve_spell(
            "Prismatic Ending", _cached_oracle("Prismatic Ending"),
            rick.name, claude.name, game_context=ctx)

    def test_auto_pick_respects_the_bound(self, lib, make_game, make_card):
        # Live shape: colors_spent=2 (a two-color deck's structural max),
        # opponent's only nonland is MV 3 — pre-fix it was exiled.
        actions, _ = self._resolve(
            lib, make_game, make_card, 2,
            [("Liliana of the Veil", "Legendary Planeswalker — Liliana", 3)])
        assert actions and actions[0].get("action") == "no_action", (
            f"an MV-3 permanent must not be exiled at bound 2 "
            f"(CR 702.100a): {actions}")

    def test_auto_pick_exiles_within_bound(self, lib, make_game, make_card):
        actions, _ = self._resolve(
            lib, make_game, make_card, 3,
            [("Liliana of the Veil", "Legendary Planeswalker — Liliana", 3)])
        assert actions and actions[0].get("action") == "move_card"
        assert actions[0]["card"] == "Liliana of the Veil"

    def test_declared_over_bound_declines_not_retargets(
            self, lib, make_game, make_card):
        # The Abrupt Decay precedent: an illegal declared target is a
        # decline, never a silent substitute.
        actions, _ = self._resolve(
            lib, make_game, make_card, 1,
            [("Liliana of the Veil", "Legendary Planeswalker — Liliana", 3),
             ("Everflowing Chalice", "Artifact", 0)],
            explicit="Liliana of the Veil")
        assert actions and actions[0].get("action") == "no_action"
        assert "Liliana" in actions[0].get("reason", "")


# ---------------------------------------------------------------------------
# C-F2-2: the trigger-queue dedup silently discarded DISTINCT instances
# ---------------------------------------------------------------------------

class TestTriggerQueueOccurrenceDedup:
    def _setup(self, make_game, make_card):
        engine = _engine()
        game = make_game()
        game._rules_engine = engine.rules
        gadwick = make_card("Gadwick, the Wizened",
                            type_line="Legendary Creature — Human Wizard",
                            power="3", toughness="3")
        return engine, game, gadwick

    def test_distinct_occurrences_both_queue(self, make_game, make_card):
        # Live shape: game_1535590382417612871 — 10 UNHANDLED lines vs 8
        # QUEUE appends: two of Gadwick's cast triggers (two DIFFERENT
        # casts) were silently discarded by the queue-wide (source, type)
        # dedup (CR 603.3b — each instance is separate).
        engine, game, gadwick = self._setup(make_game, make_card)
        r1 = engine._queue_async_trigger(
            game, gadwick, "whenever you cast...", "cast_trigger", "Rick",
            occurrence_key="cast1:5")
        r2 = engine._queue_async_trigger(
            game, gadwick, "whenever you cast...", "cast_trigger", "Rick",
            occurrence_key="cast2:5")
        assert r1 is True and r2 is True
        assert len(game.pending_async_triggers) == 2, (
            "two distinct casting events must queue two instances")

    def test_same_occurrence_dedups_and_reports(self, make_game, make_card):
        # The dedup's legitimate job (same-event re-scan) survives — and
        # the return value now tells the truth.
        engine, game, gadwick = self._setup(make_game, make_card)
        r1 = engine._queue_async_trigger(
            game, gadwick, "whenever you cast...", "cast_trigger", "Rick",
            occurrence_key="cast1:5")
        r2 = engine._queue_async_trigger(
            game, gadwick, "whenever you cast...", "cast_trigger", "Rick",
            occurrence_key="cast1:5")
        assert r1 is True and r2 is False
        assert len(game.pending_async_triggers) == 1

    def test_legacy_callers_keep_old_behavior(self, make_game, make_card):
        # Every pre-existing call site passes no occurrence_key — the
        # (source, type) dedup is unchanged for them by construction.
        engine, game, gadwick = self._setup(make_game, make_card)
        r1 = engine._queue_async_trigger(
            game, gadwick, "at the beginning...", "upkeep", "Rick")
        r2 = engine._queue_async_trigger(
            game, gadwick, "at the beginning...", "upkeep", "Rick")
        assert r1 is True and r2 is False
        assert len(game.pending_async_triggers) == 1


# ---------------------------------------------------------------------------
# C-F4-2: "transforms into <face>" triggers never fired on a transform
# ---------------------------------------------------------------------------

class TestTransformIntoTriggers:
    def _huntmaster(self, make_card):
        hm = make_card(
            "Huntmaster of the Fells",
            type_line="Creature — Human Werewolf",
            oracle_text=_cached_oracle("Huntmaster of the Fells"),
            power="2", toughness="2")
        hm.has_transform = True
        hm.back_face_name = "Ravager of the Fells"
        hm.back_face_type_line = "Creature — Werewolf"
        hm.back_face_oracle_text = (
            "Trample\nWhenever this creature transforms into Ravager of the "
            "Fells, it deals 2 damage to target opponent or planeswalker "
            "and 2 damage to up to one target creature.")
        hm.back_face_power = "4"
        hm.back_face_toughness = "4"
        return hm

    def test_transform_into_huntmaster_makes_the_wolf(
            self, make_game, make_card):
        # The live gap: game_1535582267429101618 — the flip back to
        # Huntmaster produced nothing but a cosmetic line ("enters OR
        # TRANSFORMS INTO" — CR 603.3; transforming is not entering,
        # CR 712.1, so ONLY the trigger sentence is dispatched).
        from mtg.triggers import _fire_transforms_into_triggers
        engine, game = _engine(), None
        game = _mk_game_with(make_game)
        rick = game.players[0]
        game._rules_engine = engine.rules
        hm = self._huntmaster(make_card)
        # Start on the RAVAGER face (as after a night flip)...
        hm.transform()
        assert hm.name == "Ravager of the Fells"
        rick.battlefield.append(hm)
        # ...then flip back to Huntmaster (day) and dispatch.
        hm.transform()
        msgs = _fire_transforms_into_triggers(engine, game, rick, hm)
        assert any(c.name == "Wolf" for c in rick.battlefield), (
            f"the transform-into-Huntmaster trigger must make the Wolf: "
            f"{msgs}")
        assert rick.life == 42

    def test_transform_into_ravager_deals_damage(self, make_game, make_card):
        from mtg.triggers import _fire_transforms_into_triggers
        engine = _engine()
        game = _mk_game_with(make_game)
        rick, claude = game.players
        game._rules_engine = engine.rules
        hm = self._huntmaster(make_card)
        rick.battlefield.append(hm)
        hm.transform()
        assert hm.name == "Ravager of the Fells"
        life_before = claude.life
        _fire_transforms_into_triggers(engine, game, rick, hm)
        assert claude.life == life_before - 2, (
            "Ravager's transform trigger deals 2 to the opponent (the old "
            "JSON entry created a Wolf — both faces' effects were swapped)")

    def test_no_transform_trigger_no_dispatch(self, make_game, make_card):
        # A plain werewolf face with no "transforms into" sentence must
        # dispatch nothing (no Tier-3 queue churn).
        from mtg.triggers import _fire_transforms_into_triggers
        engine = _engine()
        game = _mk_game_with(make_game)
        rick = game.players[0]
        game._rules_engine = engine.rules
        wolf = make_card("Village Ironsmith",
                         type_line="Creature — Human Werewolf",
                         oracle_text="First strike", power="1", toughness="1")
        rick.battlefield.append(wolf)
        msgs = _fire_transforms_into_triggers(engine, game, rick, wolf)
        assert msgs == []
        assert not getattr(game, 'pending_async_triggers', None)


def _mk_game_with(make_game):
    return make_game()


# ---------------------------------------------------------------------------
# B-4: overlapping target restrictions double-resolved one printed target
# ---------------------------------------------------------------------------

class TestOverlappingTargetRestrictions:
    def test_snakeskin_veil_parses_one_restriction(self, make_card):
        # "target creature you control" matched BOTH the bare pattern and
        # the you-control pattern — two restrictions for one printed
        # target; the AUTO branch then picked a target per restriction
        # (the unrestricted one preferring the OPPONENT's copy).
        from rules.spell_resolver import SpellResolver
        sr = SpellResolver(None)
        veil = make_card("Snakeskin Veil", type_line="Instant",
                         oracle_text=_cached_oracle("Snakeskin Veil"))
        needed = sr.get_targets_needed(veil, [])
        assert len(needed) == 1, (
            f"one printed target must yield one restriction, got "
            f"{len(needed)}")

    def test_multi_target_spell_keeps_both(self, make_card):
        # Disjoint spans = genuinely separate targets.
        from rules.spell_resolver import SpellResolver
        sr = SpellResolver(None)
        spell = make_card(
            "Test Bolt", type_line="Instant",
            oracle_text="Test Bolt deals 2 damage to target creature and "
                        "1 damage to target player.")
        needed = sr.get_targets_needed(spell, [])
        assert len(needed) == 2

    def test_auto_cast_hits_only_the_casters_copy(self, make_game, make_card):
        # Live shape: game_1535582056879357993 — BOTH players controlled a
        # Tatyova; Qwen's undeclared-target Snakeskin Veil put a counter on
        # EACH (one on the opponent's — CR 601.2c). The name collision is
        # what makes this fixture decisive.
        from rules.spell_resolver import SpellResolver
        game = make_game()
        rick, claude = game.players
        t_rick = make_card("Tatyova, Benthic Druid",
                           type_line="Legendary Creature — Merfolk Druid",
                           power="3", toughness="3")
        t_claude = make_card("Tatyova, Benthic Druid",
                             type_line="Legendary Creature — Merfolk Druid",
                             power="3", toughness="3")
        rick.battlefield.append(t_rick)
        claude.battlefield.append(t_claude)
        veil = make_card("Snakeskin Veil", type_line="Instant",
                         oracle_text=_cached_oracle("Snakeskin Veil"))
        sr = SpellResolver(None)
        asyncio.run(sr.cast_spell(game, claude, veil))
        assert t_claude.counters.get('+1/+1', 0) == 1, (
            "the caster's copy gets the counter")
        assert t_rick.counters.get('+1/+1', 0) == 0, (
            "the OPPONENT's same-named creature must not be touched")


# ---------------------------------------------------------------------------
# Adversarial-review refutation repairs (CO-2b, B-2b, C-F2-1c)
# ---------------------------------------------------------------------------

class TestNonlandRestrictionSecondaryPhrases:
    """CO-2 refutation: NONLAND_PERMANENT only came from the PRIMARY phrase
    parse, so cards whose FIRST target phrase is something else (Archmage's
    Charm: 'target spell') still accepted lands; and the position-0
    lookahead let 'target spell or nonland permanent' read as unqualified.
    """

    def _parse(self, card_name):
        from rules.targeting_helpers import _parse_target_restriction_from_oracle
        from types import SimpleNamespace
        oracle = _cached_oracle(card_name)
        assert oracle, f"cache must carry {card_name}"
        return _parse_target_restriction_from_oracle(
            SimpleNamespace(oracle_text=oracle, name=card_name))

    def test_secondary_nonland_phrases_carry_the_restriction(self):
        from rules.targeting import TargetType
        for name in ("Archmage's Charm", "Hullbreaker Horror",
                     "Commit // Memory"):
            r = self._parse(name)
            assert TargetType.NONLAND_PERMANENT in r.target_types, name
            assert TargetType.PERMANENT not in r.target_types, (
                f"{name}: the broad type must be subsumed even when the "
                f"nonland phrase is not the PRIMARY target phrase")

    def test_action_layer_blocks_a_land_for_charm_and_hullbreaker(
            self, make_game, make_card):
        # End-to-end at the ACTION-layer validation — the layer the parser
        # fix actually changes for these two cards: the CAST gate and the
        # CR 608.2b re-check both skip MODAL spells by design
        # (_spell_requires_targets, the Victimize precedent), and
        # Hullbreaker's bounce is a TRIGGERED ability (CR 603.3) — but
        # every template move/destroy routes through
        # _validate_target_for_action (with _source_card_name stamped,
        # the Aug-8 convention), and THAT consults the fixed parser.
        from mtg.spells import _validate_target_for_action
        game = make_game()
        rick, claude = game.players
        ruins = make_card("Academy Ruins", type_line="Legendary Land",
                          oracle_text="", power=None, toughness=None)
        rick.battlefield.append(ruins)
        for name, tl in (("Archmage's Charm", "Instant"),
                         ("Hullbreaker Horror", "Creature — Kraken Horror")):
            src = make_card(name, type_line=tl,
                            oracle_text=_cached_oracle(name))
            claude.battlefield.append(src) if tl.startswith("Creature") else None
            legal, reason = _validate_target_for_action(
                game, ruins, rick, src, claude.name)
            assert not legal, (
                f"{name}'s nonland restriction must block a LAND at the "
                f"action layer — pre-fix the broad PERMANENT type leaked "
                f"and the land was accepted ({reason!r})")


class TestFallbackLegalityFilters:
    """B-2 refutation: the now-live opponent_battlefield fallbacks lost the
    _can_target filter their primary keys have — hexproof/phased picks were
    blocked at the action layer (cost paid, effect lost), and Assassin's
    Trophy's UNLINKED search granted a free ramp land on an illegal-only
    board (the CO-4 linkage class).
    """

    def _hexproof_only_board(self, make_game, make_card, source_name,
                             source_oracle):
        from rules.effect_templates import build_game_context
        game = make_game()
        rick, claude = game.players
        bogbonder = make_card("Slippery Bogbonder",
                              type_line="Creature — Human Druid",
                              oracle_text="Flash\nHexproof",
                              power="3", toughness="3")
        bogbonder.keywords = ["Hexproof"]
        claude.battlefield.append(bogbonder)
        src = make_card(source_name, type_line="Instant",
                        oracle_text=source_oracle)
        ctx = build_game_context(game, rick, claude, card=src)
        return game, rick, claude, ctx, src

    def test_trophy_declines_hexproof_only_board_no_free_land(
            self, lib, make_game, make_card):
        game, rick, claude, ctx, src = self._hexproof_only_board(
            make_game, make_card, "Assassin's Trophy",
            _cached_oracle("Assassin's Trophy"))
        actions, _ = lib.resolve_spell(
            "Assassin's Trophy", _cached_oracle("Assassin's Trophy"),
            rick.name, claude.name, game_context=ctx)
        assert actions is not None
        kinds = [a.get("action") for a in actions]
        assert "destroy" not in kinds, (
            f"a hexproof-only board has no legal target: {actions}")
        assert not any("search" in (a.get("action") or "") for a in actions), (
            "the UNLINKED search must not grant a free ramp land when the "
            "destroy half has no legal target")

    def test_prismatic_auto_pick_skips_hexproof(self, lib, make_game, make_card):
        game, rick, claude, ctx, src = self._hexproof_only_board(
            make_game, make_card, "Prismatic Ending",
            _cached_oracle("Prismatic Ending"))
        ctx['colors_spent'] = 3
        actions, _ = lib.resolve_spell(
            "Prismatic Ending", _cached_oracle("Prismatic Ending"),
            rick.name, claude.name, game_context=ctx)
        assert actions and actions[0].get("action") == "no_action", (
            f"the auto-pick must not choose an untargetable creature: "
            f"{actions}")

    def test_prismatic_auto_pick_skips_phased_out(self, lib, make_game, make_card):
        from rules.effect_templates import build_game_context
        game = make_game()
        rick, claude = game.players
        ghost = make_card("Teferi's Protege", type_line="Creature — Human Wizard",
                          power="2", toughness="3")
        ghost.cmc = 2
        ghost._phased_out = True
        claude.battlefield.append(ghost)
        ctx = build_game_context(game, rick, claude)
        ctx['colors_spent'] = 3
        actions, _ = lib.resolve_spell(
            "Prismatic Ending", _cached_oracle("Prismatic Ending"),
            rick.name, claude.name, game_context=ctx)
        assert actions and actions[0].get("action") == "no_action", (
            "a phased-out permanent is treated as nonexistent (CR 702.26b)")


class TestTrickbindEmptyStackWording:
    def test_rejection_names_abilities_not_spells(self, make_game, make_card):
        # C-F2-1 wording nit: Trickbind's Split Second reminder supplied
        # 'spell' to the counter-target-SPELL gate, so its empty-stack
        # rejection claimed it "requires a target spell".
        from mtg.engine import GameEngine
        from mtg.spells import _validate_cast
        game = make_game()
        engine = GameEngine(None)
        game._rules_engine = engine.rules
        engine.rules.engine_ref = engine
        claude = game.players[1]
        for i in range(2):
            claude.battlefield.append(make_card(
                f"Island{i}", type_line="Basic Land — Island",
                oracle_text="", power=None, toughness=None))
        trickbind = make_card("Trickbind", type_line="Instant",
                              oracle_text=_cached_oracle("Trickbind"), cmc=2)
        claude.hand.append(trickbind)
        rejection, _, _ = _validate_cast(engine, game, claude, trickbind, None)
        assert rejection is not None and rejection[0] is False
        assert "abilit" in rejection[1].lower(), (
            f"the empty-stack rejection must name ABILITIES: {rejection[1]}")
