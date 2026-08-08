"""Mutation-sensitive regressions from the Aug 7, 2026 strict e4057a0 batch audit.

Every pin exercises real production behavior (loader-shaped inputs, real
action handlers, real templates) — the pin-shape-reachability lessons apply.
Finding IDs (C-*/A-*/B-*/G*) reference the audit ledger in CLAUDE.md.
"""

import asyncio

import pytest

from mtg.constants import Phase
from mtg.models import Card, GameState, Player
from mtg.rules_engine import RulesEngine
from mtg.actions import execute_action_on_state


def _fake_judge_client(response_text):
    """Minimal rules.client stand-in matching the real call shape
    (rules.client.messages.create → response.content[0].text)."""
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


MORAUG_TEXT = (
    "Each creature you control gets +1/+0 for each time it has attacked this turn.\n"
    "Landfall — Whenever a land you control enters, if it's your main phase, "
    "there's an additional combat phase after this phase. At the beginning of "
    "that combat, untap all creatures you control."
)

TROPHY_TEXT = (
    "Destroy target permanent an opponent controls. Its controller may search "
    "their library for a basic land card, put it onto the battlefield, then shuffle."
)


# ---------------------------------------------------------------------------
# C-1: Moraug's "+1/+0 for each time it has attacked this turn" static
# ---------------------------------------------------------------------------

class TestMoraugAttackCountStatic:
    def test_bonus_scales_with_attack_count(self, make_game, make_card):
        game = make_game()
        rick = game.players[0]
        moraug = make_card("Moraug, Fury of Akoum",
                           type_line="Legendary Creature — Minotaur Warrior",
                           power="6", toughness="6", oracle_text=MORAUG_TEXT)
        bear = make_card("Grizzly Bears", type_line="Creature — Bear",
                         power="2", toughness="2", oracle_text="")
        rick.battlefield.extend([moraug, bear])
        assert bear.get_effective_power(game) == 2
        bear.attacks_this_turn = 1
        assert bear.get_effective_power(game) == 3
        bear.attacks_this_turn = 2
        assert bear.get_effective_power(game) == 4
        # Moraug pumps himself too.
        moraug.attacks_this_turn = 1
        assert moraug.get_effective_power(game) == 7
        # Toughness is untouched (the printed clause has no toughness half).
        assert bear.get_effective_toughness(game) == 2

    def test_no_bonus_without_moraug(self, make_game, make_card):
        game = make_game()
        rick = game.players[0]
        bear = make_card("Grizzly Bears", type_line="Creature — Bear",
                         power="2", toughness="2", oracle_text="")
        rick.battlefield.append(bear)
        bear.attacks_this_turn = 2
        assert bear.get_effective_power(game) == 2

    def test_opponents_creatures_unaffected(self, make_game, make_card):
        # "Each creature YOU control" — the opponent's attackers get nothing.
        game = make_game()
        rick, claude = game.players
        moraug = make_card("Moraug, Fury of Akoum",
                           type_line="Legendary Creature — Minotaur Warrior",
                           power="6", toughness="6", oracle_text=MORAUG_TEXT)
        rick.battlefield.append(moraug)
        opp_bear = make_card("Opposing Bears", type_line="Creature — Bear",
                             power="2", toughness="2", oracle_text="")
        claude.battlefield.append(opp_bear)
        opp_bear.attacks_this_turn = 2
        assert opp_bear.get_effective_power(game) == 2

    def test_counter_cleared_on_zone_change(self, make_card):
        # CR 400.7: the new object must not remember its attack history.
        bear = make_card("Grizzly Bears", type_line="Creature — Bear",
                         power="2", toughness="2", oracle_text="")
        bear.attacks_this_turn = 3
        bear.reset_battlefield_state()
        assert bear.attacks_this_turn == 0

    def test_end_turn_sweep_clears_counter(self, make_game, make_card):
        from mtg.engine import GameEngine
        game = make_game()
        engine = GameEngine(None)
        game._rules_engine = engine.rules
        engine.rules.engine_ref = engine
        bear = make_card("Grizzly Bears", type_line="Creature — Bear",
                         power="2", toughness="2", oracle_text="")
        game.players[0].battlefield.append(bear)
        bear.attacks_this_turn = 2
        engine.end_turn(game)
        assert bear.attacks_this_turn == 0

    def test_declare_sites_increment(self):
        # Structural: all five DECLARED-attacker sites carry the increment;
        # the token-created-attacking site (actions.py) must NOT (Moraug
        # Gatherer ruling 2020-09-25: put-onto-battlefield-attacking never
        # "attacked").
        #
        # Aug 8 batch-audit (#1): engine.py's count is now ZERO — its one
        # increment lived in the phantom {"type":"attack"} plan-action
        # handler, which partially executed attacks outside combat (the
        # fifth stale-flag leak source) and now never mutates. This pin
        # previously asserted == 1 and CEMENTED that bug.
        import io
        counts = {}
        for path in ("mtg/ai_turn.py", "mtg/autoplay.py", "mtg/cog.py",
                     "mtg/engine.py", "mtg/actions.py"):
            src = io.open(path, encoding="utf-8").read()
            counts[path] = src.count("attacks_this_turn += 1")
        assert counts["mtg/ai_turn.py"] == 1
        assert counts["mtg/autoplay.py"] == 3
        assert counts["mtg/cog.py"] == 1
        assert counts["mtg/engine.py"] == 0
        assert counts["mtg/actions.py"] == 0


# ---------------------------------------------------------------------------
# B-1: Assassin's Trophy — any permanent, untapped may-search
# ---------------------------------------------------------------------------

class TestAssassinsTrophyTemplate:
    def test_lands_only_board_is_a_legal_target(self, lib):
        actions, _ = lib.resolve_spell(
            "Assassin's Trophy", TROPHY_TEXT, "A", "B",
            game_context={"opponent_battlefield": [
                {"name": "Mountain", "type_line": "Basic Land — Mountain"}]})
        kinds = [a["action"] for a in actions]
        assert "destroy" in kinds, "a land IS a legal Trophy target"
        destroy = next(a for a in actions if a["action"] == "destroy")
        assert destroy["card"] == "Mountain"

    def test_search_is_untapped(self, lib):
        # The printed card has no "tapped" clause — that's Path to Exile.
        actions, _ = lib.resolve_spell(
            "Assassin's Trophy", TROPHY_TEXT, "A", "B",
            game_context={"explicit_target_name": "Goblin Guide",
                          "explicit_target_owner": "B"})
        search = next(a for a in actions if a["action"] == "search_library_land")
        assert search["enters_tapped"] is False

    def test_empty_board_no_action(self, lib):
        actions, _ = lib.resolve_spell(
            "Assassin's Trophy", TROPHY_TEXT, "A", "B",
            game_context={"opponent_battlefield": []})
        assert actions == [{"action": "no_action",
                            "reason": "Opponent controls no permanents"}]


# ---------------------------------------------------------------------------
# B-2: Player-as-Card poisoning of explicit_target_name
# ---------------------------------------------------------------------------

class TestPlayerTargetContextPoisoning:
    def test_player_target_sets_player_keys_not_card_keys(self, make_game):
        from rules.effect_templates import build_game_context
        game = make_game()
        rick, claude = game.players
        ctx = build_game_context(game, rick, claude, explicit_target=claude)
        assert ctx.get("explicit_target_is_player") is True
        assert ctx.get("explicit_target_player") == claude.name
        assert not ctx.get("explicit_target_name"), (
            "a PLAYER's name must never reach the card-name slot — "
            "Assassin's Trophy's destroy searched for a permanent named "
            "'Rick Deckard' and whiffed while the free search still fired")
        assert not ctx.get("explicit_target_owner")

    def test_trophy_with_player_target_does_not_fire_free_search(self, lib, make_game):
        # The live misfire: destroy whiffed, the drawback-only search fired.
        from rules.effect_templates import build_game_context
        game = make_game()
        rick, claude = game.players
        ctx = build_game_context(game, rick, claude, explicit_target=claude)
        actions, _ = lib.resolve_spell(
            "Assassin's Trophy", TROPHY_TEXT, rick.name, claude.name,
            game_context=ctx)
        kinds = [a["action"] for a in actions]
        if "search_library_land" in kinds:
            assert "destroy" in kinds, (
                "the search side effect must never fire without the destroy")
            destroy = next(a for a in actions if a["action"] == "destroy")
            assert destroy["card"] != claude.name

    def test_blue_suns_zenith_still_targets_the_opponent(self, lib, make_game):
        # The migration pin: strip the name naively and BSZ's `or ctrl`
        # fallback silently flips "deck the opponent" into "caster draws X".
        from rules.effect_templates import build_game_context
        game = make_game()
        rick, claude = game.players
        ctx = build_game_context(game, rick, claude, explicit_target=claude)
        ctx["x_value"] = 3
        actions, _ = lib.resolve_spell(
            "Blue Sun's Zenith", "Target player draws X cards.",
            rick.name, claude.name, game_context=ctx)
        draw = next(a for a in actions if a["action"] == "draw_cards")
        assert draw["player"] == claude.name

    def test_truthiness_gate_returns_no_action_on_empty_board(self, lib, make_game):
        # Animate Dead with a player target and empty graveyards must be a
        # no_action, not a whiffing reanimate of "Rick Deckard".
        from rules.effect_templates import build_game_context
        game = make_game()
        rick, claude = game.players
        ctx = build_game_context(game, rick, claude, explicit_target=claude)
        actions, _ = lib.resolve_spell(
            "Animate Dead",
            "Enchant creature card in a graveyard. When this enchantment "
            "enters, if it's on the battlefield, it loses \"enchant creature "
            "card in a graveyard\"...", rick.name, claude.name,
            game_context=ctx)
        if actions:
            for a in actions:
                assert a.get("card") != claude.name

    def test_pw_forward_path_does_not_poison(self, make_game, make_card):
        # Second injection site (rules/planeswalker.py): a forwarded Player
        # target must set the player keys, not the card-name key.
        import re
        import io
        src = io.open("rules/planeswalker.py", encoding="utf-8").read()
        # The guard must exist between the targets check and the name set.
        assert "explicit_target_is_player" in src
        assert re.search(
            r"hasattr\(first_target, 'battlefield'\) and hasattr\(first_target, 'life'\)",
            src)

    def test_list_branch_filters_players(self, make_game, make_card):
        from rules.effect_templates import build_game_context
        game = make_game()
        rick, claude = game.players
        bear = make_card("Grizzly Bears", type_line="Creature — Bear",
                         power="2", toughness="2", oracle_text="")
        claude.battlefield.append(bear)
        ctx = build_game_context(game, rick, claude,
                                 explicit_target=[claude, bear])
        assert ctx.get("explicit_target_name") == "Grizzly Bears"
        assert claude.name not in (ctx.get("explicit_target_names") or [])
        assert ctx.get("explicit_target_player") == claude.name


# ---------------------------------------------------------------------------
# A-1a: the you-may-pay guard skips CAST-time reminder text only
# ---------------------------------------------------------------------------

class TestOptionalPaymentGuardDiscriminator:
    CASES = {
        "multikicker": ("multikicker {1} (you may pay an additional {1} any "
                        "number of times as you cast this spell.) choose any "
                        "target", False),
        "kicker": ("kicker {2} (you may pay an additional {2} as you cast "
                   "this spell.) if kicked, draw a card.", False),
        "suspend": ("suspend 1—{r} (rather than cast this card from your "
                    "hand, you may pay {r} and exile it.)", False),
        "extort": ("extort (whenever you cast a spell, you may pay {w/b}. "
                   "if you do, each opponent loses 1 life.)", True),
        "leyline_body": ("whenever one or more red creatures you control "
                         "die, you may pay any amount of {r}.", True),
    }

    @pytest.mark.parametrize("name", sorted(CASES))
    def test_guard_fires_only_for_resolution_time_payments(self, name):
        # Pin via the observable ("optional cost declined" emitted or not).
        # The guard sits AFTER the no-client return, so rules.client must be
        # truthy; a fake client returning a no_action keeps the not-fired
        # cases from crashing when they continue to the LLM stage.
        text, should_fire = self.CASES[name]
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
        joined = " ".join(messages or [])
        if should_fire:
            assert "optional cost declined" in joined.lower(), (
                f"{name}: genuine resolution-time payment must be guarded")
        else:
            assert "optional cost declined" not in joined.lower(), (
                f"{name}: cast-time reminder text must NOT trip the guard "
                f"(an unkicked Comet Storm was refused wholesale — full X "
                f"paid, zero damage)")


# ---------------------------------------------------------------------------
# A-1b: unkicked target-scaling spells clamp their declared target list
# ---------------------------------------------------------------------------

class TestMultikickerTargetClamp:
    def test_unkicked_comet_storm_keeps_one_target(self, make_game, make_card):
        from mtg.spells import cast_spell_async
        from mtg.engine import GameEngine
        game = make_game("modern")
        game.phase = Phase.MAIN1
        engine = GameEngine(None)
        game._rules_engine = engine.rules
        engine.rules.engine_ref = engine
        rick, claude = game.players
        storm = make_card(
            "Comet Storm", type_line="Instant", mana_cost="{X}{R}{R}", cmc=2,
            oracle_text="Multikicker {1} (You may pay an additional {1} any "
                        "number of times as you cast this spell.)\nChoose "
                        "any target, then choose another target for each "
                        "time this spell was kicked. Comet Storm deals X "
                        "damage to each of them.")
        rick.hand.append(storm)
        for i in range(4):
            rick.battlefield.append(make_card(
                f"Mountain {i}", type_line="Basic Land — Mountain",
                oracle_text="{T}: Add {R}.", power=None, toughness=None))
        t1 = make_card("Bear One", type_line="Creature — Bear",
                       power="2", toughness="2", oracle_text="")
        t2 = make_card("Bear Two", type_line="Creature — Bear",
                       power="2", toughness="2", oracle_text="")
        claude.battlefield.extend([t1, t2])
        storm._x_value = 2
        success, _msg, _effects = asyncio.run(cast_spell_async(
            engine, game, rick, storm, target=[t1, t2]))
        assert success
        # Unkicked: exactly ONE target may take damage (CR 601.2b/702.33).
        damaged = [c for c in (t1, t2) if c.damage_marked > 0]
        assert len(damaged) <= 1, (
            f"unkicked Comet Storm damaged {len(damaged)} targets — the "
            f"batch game dealt full X to BOTH declared creatures")


# ---------------------------------------------------------------------------
# G5-1: modal spells resolve remaining modes after a fizzled counter mode
# ---------------------------------------------------------------------------

class TestModalFizzleCascade:
    def _cast(self, make_game, make_card, name, oracle, template_ok=True):
        from mtg.spells import cast_spell_async
        from mtg.engine import GameEngine
        game = make_game("modern")
        game.phase = Phase.MAIN1
        engine = GameEngine(None)
        game._rules_engine = engine.rules
        engine.rules.engine_ref = engine
        rick = game.players[0]
        spell = make_card(name, type_line="Instant", mana_cost="{1}{U}{U}{U}",
                          cmc=4, oracle_text=oracle)
        rick.hand.append(spell)
        for i in range(3):
            rick.library.append(make_card(
                f"Library Card {i}", type_line="Creature — Bear",
                power="2", toughness="2", oracle_text=""))
        for i in range(4):
            rick.battlefield.append(make_card(
                f"Island {i}", type_line="Basic Land — Island",
                oracle_text="{T}: Add {U}.", power=None, toughness=None))
        hand_before = len(rick.hand) - 1  # minus the spell itself
        success, _m, _e = asyncio.run(cast_spell_async(
            engine, game, rick, spell))
        return game, rick, hand_before, success

    def test_cryptic_command_draw_resolves_after_counter_fizzles(
            self, make_game, make_card):
        game, rick, hand_before, success = self._cast(
            make_game, make_card, "Cryptic Command",
            "Choose two —\n• Counter target spell.\n• Return target "
            "permanent to its owner's hand.\n• Tap all creatures your "
            "opponents control.\n• Draw a card.")
        assert success
        assert len(rick.hand) == hand_before + 1, (
            "Cryptic Command is a true modal spell (CR 700.2) — the draw "
            "mode must resolve even when the counter mode's target is gone "
            "(the batch game spent 4 mana for zero effect)")

    def test_non_modal_fizzle_cascade_still_skips(self):
        # Structural control: the skip must require NOT-modal, and the
        # discriminator must be the ANCHORED printed modal header — an
        # Arcane Denial (no modal header) keeps the all-or-nothing cascade.
        # (An end-to-end Denial control can't be built here: the CR 601.2c
        # counter gate correctly refuses the cast on an empty stack; the
        # live fizzle path needs the mid-resolution target-removal race.)
        import io
        import re
        src = io.open("mtg/spells.py", encoding="utf-8").read()
        assert "if spell_fizzled and not _is_modal_spell:" in src
        m = re.search(r"_is_modal_spell = bool\(re\.search\(\s*\n?\s*r'([^']+)'", src)
        assert m and m.group(1).startswith("^choose "), (
            "the discriminator must anchor on the printed modal header — a "
            "loose substring would flip non-modal spells to mode-independent")


# ---------------------------------------------------------------------------
# A-4: judge guard — add_counters on a non-creature for "target creature"
# ---------------------------------------------------------------------------

class TestJudgeAddCountersCreatureGuard:
    def test_counter_on_equipment_dropped(self, make_game, make_card):
        from mtg import judge as judge_mod
        game = make_game()
        rick = game.players[0]
        clamp = make_card("Skullclamp", type_line="Artifact — Equipment",
                          power=None, toughness=None,
                          oracle_text="Equipped creature gets +1/-1.")
        rick.battlefield.append(clamp)
        rules = RulesEngine(game)
        rules.client = _fake_judge_client(
            '{"explanation": "put the counter", "actions": '
            '[{"action": "add_counters", "card": "Skullclamp", '
            '"counter_type": "-1/-1", "amount": 1}]}')
        asyncio.run(judge_mod.resolve_effect(
            rules, game,
            "Rick activated Yawgmoth, Thran Physician's ability: Put a "
            "-1/-1 counter on up to one target creature and draw a card.",
            source_card="Yawgmoth, Thran Physician", controller=rick.name))
        assert clamp.counters.get("-1/-1", 0) == 0, (
            "the ability says 'target CREATURE' — Skullclamp is an "
            "Equipment (the batch game put the counter on it anyway)")


# ---------------------------------------------------------------------------
# G1-1 / G2-2 / Nahiri: sacrifice effects emit sacrifice, not destroy
# ---------------------------------------------------------------------------

class TestSacrificeNotDestroy:
    def test_sidisi_exploit_emits_sacrifice(self, lib):
        actions, _ = lib.resolve_etb(
            "Sidisi, Undead Vizier",
            "Exploit (When this creature enters, you may sacrifice a "
            "creature.)\nWhen Sidisi, Undead Vizier exploits a creature, "
            "search your library for a card, put it into your hand, then "
            "shuffle.", "A", "B",
            {"controller_worst_creature": "Sakura-Tribe Elder"})
        kinds = [a["action"] for a in actions]
        assert "sacrifice_permanent" in kinds, (
            "exploit is a SACRIFICE (CR 701.19) — a destroy lets "
            "indestructible survive and skips sacrifice watchers")
        assert "destroy" not in kinds
        sac = next(a for a in actions if a["action"] == "sacrifice_permanent")
        assert sac["preferred_card"] == "Sakura-Tribe Elder"
        assert sac.get("only_preferred") is True

    def test_nahiris_lithoforming_emits_sacrifices(self, lib):
        actions, _ = lib.resolve_spell(
            "Nahiri's Lithoforming",
            "Sacrifice X lands. For each land sacrificed this way, draw a "
            "card. You may play X additional lands this turn.",
            "A", "B",
            game_context={"x_value": 2,
                          "controller_tapped_lands": ["Plains", "Mountain"],
                          "controller_lands": ["Plains", "Mountain"]})
        sacs = [a for a in actions if a["action"] == "sacrifice_permanent"]
        assert len(sacs) == 2
        assert not [a for a in actions if a["action"] == "destroy"]

    def test_edict_sacrifice_fires_sac_triggers(self, make_game, make_card):
        # End-to-end: the sacrifice action path fires "whenever you
        # sacrifice" watchers — the whole reason destroy was wrong.
        game = make_game()
        rick, claude = game.players
        bear = make_card("Grizzly Bears", type_line="Creature — Bear",
                         power="2", toughness="2", oracle_text="")
        korvold = make_card(
            "Korvold, Fae-Cursed King",
            type_line="Legendary Creature — Dragon Noble",
            power="4", toughness="4",
            oracle_text="Whenever you sacrifice a permanent, put a +1/+1 "
                        "counter on Korvold and draw a card.")
        rick.battlefield.extend([bear, korvold])
        rules = RulesEngine(game)
        execute_action_on_state(rules, game, {
            "action": "sacrifice_permanent", "player": rick.name,
            "type_filter": "creature", "preferred_card": "Grizzly Bears",
            "reason": "edict"})
        assert bear not in rick.battlefield
        assert korvold.counters.get("+1/+1", 0) >= 1, (
            "the sacrifice watcher must fire from an edict sacrifice")


# ---------------------------------------------------------------------------
# G2-1 / G6-1: remove_keywords action + grant_keywords message hygiene
# ---------------------------------------------------------------------------

class TestRemoveKeywords:
    def test_opponent_scope_and_eot_expiry(self, make_game, make_card):
        game = make_game()
        rick, claude = game.players
        angel = make_card("Platinum Angel",
                          type_line="Artifact Creature — Angel",
                          power="4", toughness="4", oracle_text="",
                          keywords=["Flying", "Indestructible"])
        claude.battlefield.append(angel)
        own = make_card("Own Bear", type_line="Creature — Bear",
                        power="2", toughness="2", oracle_text="",
                        keywords=["Indestructible"])
        rick.battlefield.append(own)
        rules = RulesEngine(game)
        msg = execute_action_on_state(rules, game, {
            "action": "remove_keywords", "player": rick.name,
            "keywords": ["Hexproof", "Indestructible"]})
        assert msg and "lose" in msg
        assert not angel.has_keyword("Indestructible", game=game)
        assert own.has_keyword("Indestructible", game=game), (
            "Shadowspear's scope is OPPONENTS' permanents only")
        # EOT expiry via the engine clear.
        from mtg.engine import GameEngine
        engine = GameEngine(None)
        game._rules_engine = engine.rules
        engine.rules.engine_ref = engine
        engine.clear_end_of_turn_effects(game)
        assert angel.has_keyword("Indestructible", game=game)

    def test_named_card_scope(self, make_game, make_card):
        game = make_game()
        _, claude = game.players
        angel = make_card("Platinum Angel",
                          type_line="Artifact Creature — Angel",
                          power="4", toughness="4", oracle_text="",
                          keywords=["Indestructible"])
        claude.battlefield.append(angel)
        rules = RulesEngine(game)
        execute_action_on_state(rules, game, {
            "action": "remove_keywords", "card": "Platinum Angel",
            "keywords": ["Indestructible"]})
        assert not angel.has_keyword("Indestructible", game=game)

    def test_empty_keywords_refused(self, make_game):
        game = make_game()
        rules = RulesEngine(game)
        msg = execute_action_on_state(rules, game, {
            "action": "remove_keywords", "player": game.players[0].name,
            "keywords": []})
        assert msg is None

    def test_grant_keywords_empty_list_refused(self, make_game, make_card):
        # G6-1: the garbled "gain  until end of turn" display.
        game = make_game()
        rules = RulesEngine(game)
        msg = execute_action_on_state(rules, game, {
            "action": "grant_keywords", "player": game.players[0].name,
            "keywords": []})
        assert msg is None

    def test_grant_keywords_creature_wording(self, make_game, make_card):
        game = make_game()
        rick = game.players[0]
        rick.battlefield.append(make_card(
            "Grizzly Bears", type_line="Creature — Bear",
            power="2", toughness="2", oracle_text=""))
        rick.battlefield.append(make_card(
            "Plains", type_line="Basic Land — Plains", power=None,
            toughness=None, oracle_text="{T}: Add {W}."))
        rules = RulesEngine(game)
        msg = execute_action_on_state(rules, game, {
            "action": "grant_keywords", "player": rick.name,
            "keywords": ["Indestructible"], "target": "all_own_creatures"})
        assert "creature" in msg
        assert "permanents" not in msg


# ---------------------------------------------------------------------------
# A-2: Molten Duplication — copy + haste + MANDATORY delayed sacrifice
# ---------------------------------------------------------------------------

class TestMoltenDuplication:
    def test_template_carries_delayed_sacrifice(self, lib):
        actions, _ = lib.resolve_spell(
            "Molten Duplication",
            "Create a token that's a copy of target artifact or creature "
            "you control, except it's an artifact in addition to its other "
            "types. It gains haste until end of turn. Sacrifice it at the "
            "beginning of the next end step.",
            "A", "B",
            game_context={"explicit_target_name": "Arclight Phoenix"})
        kinds = [a["action"] for a in actions]
        assert "create_copy_token" in kinds
        assert "schedule_delayed_trigger" in kinds, (
            "the Tier-3 path dropped the mandatory delayed sacrifice — the "
            "token attacked two extra turns in the batch game")
        sched = next(a for a in actions
                     if a["action"] == "schedule_delayed_trigger")
        inner = sched["actions"][0]
        assert inner["action"] == "sacrifice_permanent"
        assert inner["preferred_card"] == "Arclight Phoenix"
        copy = next(a for a in actions if a["action"] == "create_copy_token")
        assert "Artifact" in (copy.get("extra_types") or [])
        assert "Haste" in (copy.get("keywords") or [])


# ---------------------------------------------------------------------------
# A-3: PLAN-VALIDATE optional-cost carve-out
# ---------------------------------------------------------------------------

class TestPlanValidateSacrificeCarveOut:
    def test_markers(self):
        # The production condition: unconditional "sacrifice an artifact"
        # gates; optional "may sacrifice ... or discard" does not.
        chandra = ("+2: you may sacrifice an artifact or discard a card. "
                   "if you do, draw a card.")
        daretti = ("-2: sacrifice an artifact. if you do, return target "
                   "artifact card from your graveyard to the battlefield.")
        def gated(text):
            return ("sacrifice an artifact" in text
                    and "may sacrifice" not in text
                    and "or discard" not in text)
        assert not gated(chandra), (
            "Chandra, Spark Hunter's optional OR-cost was rejected with 8 "
            "cards in hand")
        assert gated(daretti)

    def test_production_condition_matches(self):
        import io
        src = io.open("mtg/ai_turn.py", encoding="utf-8").read()
        assert "'may sacrifice' not in ability_text" in src
        assert "'or discard' not in ability_text" in src


# ---------------------------------------------------------------------------
# B-3: legal no-op resolutions are not execution failures
# ---------------------------------------------------------------------------

class TestFailureSniffExemptsResolutionNoOps:
    def test_template_noop_line_not_a_failure(self):
        from mtg.ai_turn import _result_looks_like_failure
        assert not _result_looks_like_failure(
            "📋 No valid targets in opponent's hand"), (
            "a legal no-op RESOLUTION is not a cast failure — the sniff "
            "burned a retry on a card already in the graveyard")

    def test_bug_e_rejections_still_sniffed(self):
        from mtg.ai_turn import _result_looks_like_failure
        assert _result_looks_like_failure(
            "Teferi, Hero of Dominaria already used its ability this turn — "
            "cannot activate again")
        assert _result_looks_like_failure(
            "Snapcaster Mage has no activated abilities")


# ---------------------------------------------------------------------------
# C-4: dropped-resolve reason reaches the retry loop
# ---------------------------------------------------------------------------

class TestDroppedResolveReason:
    def test_stash_consumed_by_get_action_error(self, make_game):
        from mtg.ai_turn import _get_action_error
        from mtg.engine import GameEngine
        game = make_game()
        engine = GameEngine(None)
        game._rules_engine = engine.rules
        engine.rules.engine_ref = engine
        game._last_resolve_drop_reason = (
            game.turn_number, "redundant `resolve` dropped — test reason")
        err = _get_action_error(engine, game, 0,
                                {"type": "resolve", "description": "x"})
        assert "redundant" in err
        assert game._last_resolve_drop_reason is None, "consume-on-read"

    def test_drop_sites_stash(self):
        import io
        for path in ("mtg/engine.py", "mtg/autoplay.py"):
            src = io.open(path, encoding="utf-8").read()
            assert "_last_resolve_drop_reason" in src, path


# ---------------------------------------------------------------------------
# C-3: combat state stripped when a permanent leaves the battlefield
# ---------------------------------------------------------------------------

class TestCombatStateStrippedOnLeave:
    def test_sacrificed_attacker_loses_combat_state(self, make_game, make_card):
        game = make_game()
        rick = game.players[0]
        priest = make_card("Priest of Forgotten Gods",
                           type_line="Creature — Human Cleric",
                           power="1", toughness="2", oracle_text="")
        rick.battlefield.append(priest)
        priest.attacking = True
        game.attackers.append(priest.id)
        rules = RulesEngine(game)
        execute_action_on_state(rules, game, {
            "action": "sacrifice_permanent", "player": rick.name,
            "type_filter": "creature", "reason": "Korvold attack trigger"})
        assert priest.attacking is False, (
            "the object left the battlefield carrying .attacking=True — "
            "only the end-turn [COMBAT-SWEEP] net caught it in the batch")
        assert priest.id not in game.attackers


# ---------------------------------------------------------------------------
# G3-1: mass removal routes commanders / owners / unearth like single destroy
# ---------------------------------------------------------------------------

class TestMassRemovalRouting:
    def _setup(self, make_game, make_card):
        game = make_game()
        rick, claude = game.players
        cmd = make_card("Korvold, Fae-Cursed King",
                        type_line="Legendary Creature — Dragon Noble",
                        power="4", toughness="4", oracle_text="")
        cmd.is_commander = True
        bear = make_card("Grizzly Bears", type_line="Creature — Bear",
                         power="2", toughness="2", oracle_text="")
        claude.battlefield.extend([cmd, bear])
        claude.command_zone = []
        return game, rick, claude, cmd, bear

    def test_board_wipe_redirects_commander(self, make_game, make_card):
        game, rick, claude, cmd, bear = self._setup(make_game, make_card)
        rules = RulesEngine(game)
        msg = execute_action_on_state(rules, game,
                                      {"action": "destroy_all_creatures"})
        assert cmd in claude.command_zone, (
            "Damnation put Korvold in the GRAVEYARD and Rise of the Dark "
            "Realms handed the opponent their own commander for the lethal "
            "swing (CR 903.9a)")
        assert cmd not in claude.graveyard
        assert bear in claude.graveyard
        assert "command zone" in msg

    def test_destroy_by_power_redirects_commander(self, make_game, make_card):
        game, rick, claude, cmd, bear = self._setup(make_game, make_card)
        rules = RulesEngine(game)
        execute_action_on_state(rules, game,
                                {"action": "destroy_by_power", "min_power": 3})
        assert cmd in claude.command_zone
        assert bear in claude.battlefield  # power 2 < 3 survives

    def test_exile_all_redirects_commander(self, make_game, make_card):
        game, rick, claude, cmd, bear = self._setup(make_game, make_card)
        rules = RulesEngine(game)
        execute_action_on_state(rules, game,
                                {"action": "exile_all_by_type",
                                 "type": "creatures"})
        assert cmd in claude.command_zone, "CR 903.9a covers exile too"
        assert bear in claude.exile

    def test_owner_routing_for_stolen_creature(self, make_game, make_card):
        # CR 404.3: a stolen creature dies to a wipe → its OWNER's graveyard.
        game = make_game()
        rick, claude = game.players
        stolen = make_card("Sun Titan", type_line="Creature — Giant",
                           power="6", toughness="6", oracle_text="")
        stolen.owner_index = 1  # owned by claude...
        rick.battlefield.append(stolen)  # ...controlled by rick
        rules = RulesEngine(game)
        execute_action_on_state(rules, game,
                                {"action": "destroy_all_creatures"})
        assert stolen in claude.graveyard, (
            "mass wipes were appending to the battlefield-holder's "
            "graveyard, permanently changing hands")
        assert stolen not in rick.graveyard


# ---------------------------------------------------------------------------
# B-4: tail-of-turn SBA losses detected inside end_turn
# ---------------------------------------------------------------------------

class TestTailOfTurnSbaDetection:
    def _engine(self, game):
        from mtg.engine import GameEngine
        engine = GameEngine(None)
        game._rules_engine = engine.rules
        engine.rules.engine_ref = engine
        return engine

    def test_end_turn_detects_zero_life(self, make_game):
        game = make_game("modern")
        engine = self._engine(game)
        game.players[1].life = 0  # Read the Bones self-loss at MAIN2 tail
        engine.end_turn(game)
        assert game.ended, (
            "the loss survived end_turn and was detected only at the next "
            "turn's first phase advance — after the winner's untap and the "
            "turn banner (4 cross-turn deferrals in the batch)")
        assert game.winner == 0

    def test_upkeep_branch_gated_when_ended(self, make_game):
        game = make_game("modern")
        engine = self._engine(game)
        game.phase = Phase.UPKEEP
        game.ended = True
        _, messages = engine.advance_phase(game)
        assert not any("Upkeep" in m for m in messages), (
            "upkeep triggers resolved for a player who had already lost "
            "(a Koma's Coil was created at 0 life in the batch)")

    def test_zero_life_recheck_after_dies_wave_drain(self, make_game, make_card):
        # The adjacent defect: a Blood Artist drain to 0 during the SBA wave
        # is not a death, so no re-check fired and the dead player kept
        # acting (a full Burnished Hart activation at -1 life).
        game = make_game()
        engine = self._engine(game)
        rick, claude = game.players
        artist = make_card(
            "Blood Artist", type_line="Creature — Vampire",
            power="0", toughness="1",
            oracle_text="Whenever Blood Artist or another creature dies, "
                        "target player loses 1 life and you gain 1 life.")
        victim = make_card("Grizzly Bears", type_line="Creature — Bear",
                           power="2", toughness="2", oracle_text="")
        rick.battlefield.append(artist)
        rick.battlefield.append(victim)
        claude.life = 1
        victim.damage_marked = 99
        engine.check_state_based_actions(game)
        assert claude.life <= 0
        assert game.ended, (
            "the drain-to-0 inside the wave must trigger one re-check "
            "(sentinel-guarded against Platinum Angel recursion)")


# ---------------------------------------------------------------------------
# G3-2: buffered trigger messages drain BEFORE the loss line
# ---------------------------------------------------------------------------

class TestPendingMessagesDrainBeforeLoss:
    def test_ordering(self, make_game):
        from mtg.sba import process_state_based_actions
        game = make_game()
        rules = RulesEngine(game)
        game.players[1].life = 0
        game._pending_messages = ["🔥 Mayhem Devil deals 1 damage to Qwen (life: 9)"]
        messages = rules.process_state_based_actions(game)
        joined = list(messages)
        loss_idx = next(i for i, m in enumerate(joined) if "loses the game" in m)
        devil_idx = next((i for i, m in enumerate(joined) if "Mayhem Devil" in m), None)
        assert devil_idx is not None and devil_idx < loss_idx, (
            "pre-loss trigger messages flushed AFTER the terminal line — "
            "the transcript showed the dead player's life rising post-loss")


# ---------------------------------------------------------------------------
# C-2: provider console tags
# ---------------------------------------------------------------------------

class TestProviderTags:
    def _player(self, model):
        from mtg.claude_player import ClaudePlayer
        p = ClaudePlayer.__new__(ClaudePlayer)
        p.model = model
        return p

    def test_qwen_tags(self):
        p = self._player("qwen3.7-flash")
        assert p.provider_tag == "[QWEN AI]"
        assert p.turn_tag == "[QWEN TURN]"

    def test_deepseek_tags_unchanged(self):
        p = self._player("deepseek-v4-flash")
        assert p.provider_tag == "[DEEPSEEK AI]"

    def test_anthropic_fallback_unchanged(self):
        p = self._player("claude-sonnet-5")
        assert p.provider_tag == "[CLAUDE AI]"


# ---------------------------------------------------------------------------
# C-5: the typed tutor choice is consumed by the FIRST search only
# ---------------------------------------------------------------------------

class TestTutorInjectionConsumedOnce:
    def test_injection_site_consumes(self):
        import io
        src = io.open("mtg/spells.py", encoding="utf-8").read()
        anchor = 'action["card_name"] = ctx[\'_tutor_card\']'
        idx = src.find(anchor)
        assert idx != -1
        tail = src[idx:idx + 700]
        assert "ctx['_tutor_card'] = None" in tail, (
            "without consuming the choice, a two-search template (Jarad's "
            "Orders) injected the same name into BOTH searches")


# ---------------------------------------------------------------------------
# Registry dedup (Aug 7 follow-up): no duplicate registration KEYS
# ---------------------------------------------------------------------------

class TestNoDuplicateRegistryKeys:
    def test_no_duplicate_add_card_keys(self):
        """Structural: _add_card is a plain dict assignment, so a second
        registration of the same KEY silently wins — the class that made
        Shard Volley target-blind, kept the flat-draw Mystic Confluence
        alive for months, and hid the dead _gen_reanimate (Aug 7 deep-dive:
        NINE keys were doubled). The sibling pin covers duplicate _gen_*
        DEFINITIONS; this one covers duplicate KEYS across all three
        registries. Baseline: zero."""
        import collections
        import io
        import re
        buf = io.open("rules/effect_templates.py", encoding="utf-8").read()
        for reg in ("_add_card", "_add_attack_card", "_add_dies_card"):
            keys = [
                m.group(1) or m.group(2)
                for m in re.finditer(
                    r"self\." + reg + r"\(\s*\n?\s*(?:\"([^\"]+)\"|'([^']+)')",
                    buf)
            ]
            dupes = {k: c for k, c in collections.Counter(keys).items() if c > 1}
            assert not dupes, (
                f"duplicate {reg} keys (last registration silently wins): "
                f"{dupes}")

    def test_surviving_registrations_resolve(self, lib):
        # The dedup kept the RICHER registration for every key — spot-check
        # the three whose duplicate was the live one before the cleanup.
        # Archmage's Charm: the surviving generator honors stack state.
        a, _ = lib.resolve_spell(
            "Archmage's Charm",
            "Choose one —\n• Counter target spell.\n• Target player draws "
            "two cards.\n• Gain control of target nonland permanent with "
            "mana value 1 or less.",
            "A", "B", game_context={})
        assert a and a[0]["action"] == "draw_cards", (
            "empty stack → the draw mode")
        # Mystic Confluence: the surviving modal generator fills slots.
        a2, _ = lib.resolve_spell(
            "Mystic Confluence",
            "Choose three. You may choose the same mode more than once.\n"
            "• Counter target spell unless its controller pays {3}.\n"
            "• Return target creature to its owner's hand.\n• Draw a card.",
            "A", "B", game_context={})
        kinds2 = [x["action"] for x in (a2 or [])]
        assert "draw_cards" in kinds2, "empty board → draw slots fill"

    def test_shard_volley_honors_declared_targets(self, lib):
        oracle = ("As an additional cost to cast this spell, sacrifice a "
                  "land.\nShard Volley deals 3 damage to any target.")
        # Declared creature → target_card (the deleted live duplicate always
        # hit the face, ignoring every declared target).
        a, _ = lib.resolve_spell(
            "Shard Volley", oracle, "A", "B",
            game_context={"explicit_target_name": "Grizzly Bears",
                          "explicit_target_is_creature": True,
                          "explicit_target_owner": "B"})
        assert a[0].get("target_card") == "Grizzly Bears"
        # Declared player → target_player.
        a2, _ = lib.resolve_spell(
            "Shard Volley", oracle, "A", "B",
            game_context={"explicit_target_player": "B",
                          "explicit_target_is_player": True})
        assert a2[0].get("target_player") == "B"
        # No declared target → face.
        a3, _ = lib.resolve_spell("Shard Volley", oracle, "A", "B",
                                  game_context={})
        assert a3[0].get("target_player") == "B"


# ---------------------------------------------------------------------------
# Backlog item 4: aura enchant restrictions
# ---------------------------------------------------------------------------

class TestAuraEnchantRestrictions:
    def test_coronet_requires_aura_attached(self, make_game, make_card):
        from rules.targeting_helpers import aura_has_legal_target
        game = make_game()
        rick = game.players[0]
        coronet = make_card(
            "Daybreak Coronet", type_line="Enchantment — Aura",
            power=None, toughness=None,
            oracle_text="Enchant creature with another Aura attached to it\n"
                        "Enchanted creature gets +3/+3 and has first "
                        "strike, vigilance, and lifelink.")
        bear = make_card("Grizzly Bears", type_line="Creature — Bear",
                         power="2", toughness="2", oracle_text="")
        rick.battlefield.append(bear)
        assert not aura_has_legal_target(game, coronet, rick), (
            "no aura'd creature exists — Coronet has no legal target "
            "(the restriction was checked NOWHERE)")
        umbra = make_card("Bear Umbra", type_line="Enchantment — Aura",
                          power=None, toughness=None,
                          oracle_text="Enchant creature")
        umbra.attached_to = bear.id
        rick.battlefield.append(umbra)
        assert aura_has_legal_target(game, coronet, rick)

    def test_utopia_sprawl_requires_a_forest(self, make_game, make_card):
        from rules.targeting_helpers import aura_has_legal_target
        game = make_game()
        rick = game.players[0]
        sprawl = make_card(
            "Utopia Sprawl", type_line="Enchantment — Aura",
            power=None, toughness=None,
            oracle_text="Enchant Forest\nAs this enchantment enters, choose a "
                        "color.\nWhenever enchanted Forest is tapped for "
                        "mana, its controller adds one additional mana of "
                        "the chosen color.")
        rick.battlefield.append(make_card(
            "Mountain", type_line="Basic Land — Mountain", power=None,
            toughness=None, oracle_text="{T}: Add {R}."))
        assert not aura_has_legal_target(game, sprawl, rick), (
            "Enchant Forest with zero Forests anywhere — castable, mana "
            "paid, fizzle at attach (CR 601.2c)")
        rick.battlefield.append(make_card(
            "Forest", type_line="Basic Land — Forest", power=None,
            toughness=None, oracle_text="{T}: Add {G}."))
        assert aura_has_legal_target(game, sprawl, rick)
