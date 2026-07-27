"""July 28, 2026 — Phase 2, the bad-templates / swallowed-clauses cluster.

The unifying failure here is a partial handler CLAIMING a whole card. A loose
substring branch, or a single-clause regex, matches the first thing it
recognises, resolves, and thereby prevents every later tier from seeing the
rest of the card — silently, with nothing in the unhandled backlog for an audit
to find.

C1  Felidar Retreat was resolved by the RAMPAGING BALOTHS branch, whose
    "landfall + create + beast" fallback matched on "Cat Beast". It made a 4/4
    green Beast instead of a 2/2 white Cat Beast, and because that branch is an
    elif it also made the Tier 1.5 lookup unreachable, so the modal
    +1/+1-counter half was never offered at any tier. Eight wrong tokens in the
    loose logs.

C2  Cavalry Pegasus's template described a card that does not exist ("each
    attacking Knight and each other attacking Pegasus" — zero hits across all
    38,101 oracle entries), and grant_keywords never read the "filter" key it
    was handed. With the default target of all_own_permanents, every permanent
    the controller owned — LANDS included — gained Flying: 10, 12, 13 and 15
    permanents in single turns, including a Cat Soldier that is not a Human.

C3  Leonin Vanguard had no template, so it escalated to Tier 3 every combat,
    and Tier 3's pump is player-scoped rather than card-scoped: it buffed the
    whole team, and twice emitted +0/+0, which pumps nobody at all. 7 of 7
    executed firings were wrong in one game.

C4  Chulane's "draw a card, THEN you may put a land card from your hand onto
    the battlefield" is one sentence, so the inline draw handler matched it,
    drew, and set executed_trigger — which suppressed BOTH the template lookup
    and the Tier-3 queue. The ramp half never fired in any observed game and
    never appeared in the backlog either.

C5  Three library-look shortcuts swallowed the rest of their cards. The
    reviewer believed all three shared one root cause; verification showed they
    do NOT — only the judge.py site has an exclusion list, and it evaluates
    False for two of the three victims. The two rules/effect_templates.py sites
    are single-clause patterns with no exclusion list at all.
"""
import json
from pathlib import Path

import pytest


_CACHE = Path(__file__).resolve().parent.parent / "data" / "card_data_cache.json"


def _oracle(card_key):
    if not _CACHE.exists():
        pytest.skip("card_data_cache.json not present")
    with open(_CACHE, encoding="utf-8") as fh:
        data = json.load(fh)
    entry = data.get(card_key)
    if entry is None:
        pytest.skip(f"{card_key!r} not in the card cache")
    return entry.get("oracle_text") or ""


def _kinds(actions):
    return [a.get("action") for a in (actions or [])]


# ---------------------------------------------------------------------------
# C1 — Felidar Retreat
# ---------------------------------------------------------------------------

class TestFelidarRetreat:

    def test_oracle_really_is_a_white_cat_beast(self):
        text = _oracle("felidar retreat").lower()
        assert "2/2 white cat beast" in text
        assert "4/4" not in text, "the 4/4 green Beast belongs to Rampaging Baloths"

    def test_token_mode_makes_the_printed_token(self, lib):
        actions, _d = lib.resolve_etb(
            "Felidar Retreat", _oracle("felidar retreat"), "Rick", "Claude",
            {"controller_creature_count": 0})
        tok = next(a for a in actions if a["action"] == "create_token")
        assert tok["power"] == 2 and tok["toughness"] == 2
        assert tok["colors"] == "W"
        assert "Cat Beast" in tok["types"]

    def test_counter_mode_is_reachable_at_all(self, lib):
        """The modal half was previously unreachable at every tier."""
        actions, _d = lib.resolve_etb(
            "Felidar Retreat", _oracle("felidar retreat"), "Rick", "Claude",
            {"controller_creature_count": 3})
        assert "add_counters" in _kinds(actions)
        pump = next(a for a in actions if a["action"] == "pump_all_creatures")
        assert "Vigilance" in pump["keywords"]

    def test_counter_mode_uses_vocabulary_the_handler_has(self, game, rules, make_card):
        """A template emitting keys no handler reads is the same silent-no-op
        class this cluster is about — so execute it, don't just inspect it."""
        from rules.effect_templates import get_effect_library
        rick = game.players[0]
        bears = [make_card(f"Bear {i}") for i in range(2)]
        rick.battlefield.extend(bears)
        actions, _d = get_effect_library().resolve_etb(
            "Felidar Retreat", _oracle("felidar retreat"), rick.name, "Claude",
            {"controller_creature_count": 2})
        for a in actions:
            rules._execute_action_on_state(game, a)
        game.recalculate_power_toughness()
        assert bears[0].counters.get("+1/+1") == 1
        assert bears[0].has_keyword("Vigilance", game=game)

    def test_rampaging_baloths_still_gets_its_green_beast(self, game, rules, make_card):
        from mtg.triggers import _handle_land_etb
        rick = game.players[0]
        rick.battlefield.append(make_card(
            "Rampaging Baloths", power="6", toughness="6",
            oracle_text=("Trample\nLandfall — Whenever a land you control enters, "
                         "you may create a 4/4 green Beast creature token.")))
        land = make_card("Forest", type_line="Basic Land — Forest")
        rick.battlefield.append(land)
        _handle_land_etb(rules, game, rick, land)
        beast = next((c for c in rick.battlefield if c.name == "Beast"), None)
        assert beast is not None and beast.power == "4"


# ---------------------------------------------------------------------------
# C2 — Cavalry Pegasus + grant_keywords filters
# ---------------------------------------------------------------------------

class TestCavalryPegasus:

    def test_template_describes_the_real_card(self):
        path = Path(__file__).resolve().parent.parent / "data" / "card_templates.json"
        raw = json.loads(path.read_text(encoding="utf-8"))
        items = raw if isinstance(raw, list) else raw.get("templates", raw)
        seq = items if isinstance(items, list) else list(items.values())
        entry = next(e for e in seq if e.get("key") == "cavalry pegasus")
        blob = json.dumps(entry).lower()
        assert "human" in blob
        assert "knight" not in blob and "pegasi" not in blob, (
            "that wording matches no printing of any card")

    def test_only_attacking_humans_gain_flying(self, game, rules, lib, make_card):
        rick = game.players[0]
        peg = make_card("Cavalry Pegasus", type_line="Creature — Pegasus")
        human = make_card("Boros Elite", type_line="Creature — Human Soldier")
        cat = make_card("Leonin Vanguard", type_line="Creature — Cat Soldier")
        land = make_card("Plains", type_line="Basic Land — Plains")
        for c in (peg, human, cat, land):
            rick.battlefield.append(c)
        for c in (peg, human, cat):
            c.attacking = True
        actions, _d = lib.resolve_attack_trigger(
            "Cavalry Pegasus", _oracle("cavalry pegasus"),
            "Cavalry Pegasus", 1, rick.name, "Claude", {})
        for a in actions:
            rules._execute_action_on_state(game, a)
        assert "Flying" in (human.temp_keywords or [])
        assert "Flying" not in (cat.temp_keywords or []), "a Cat Soldier is not a Human"
        assert "Flying" not in (land.temp_keywords or []), "lands do not gain flying"

    def test_unknown_filter_refuses_instead_of_buffing_everything(self, game, rules, make_card):
        """The old default silently widened an unrecognized restriction to the
        whole battlefield. A fabricated filter must fail loudly instead."""
        rick = game.players[0]
        land = make_card("Plains", type_line="Basic Land — Plains")
        bear = make_card("Runeclaw Bear")
        rick.battlefield.extend([land, bear])
        msg = rules._execute_action_on_state(game, {
            "action": "grant_keywords", "player": rick.name,
            "keywords": ["Flying"], "filter": "attacking_knights_and_other_pegasi",
        })
        assert msg is None
        assert "Flying" not in (land.temp_keywords or [])
        assert "Flying" not in (bear.temp_keywords or [])

    def test_plain_team_grant_still_works(self, game, rules, make_card):
        rick = game.players[0]
        bear = make_card("Runeclaw Bear")
        rick.battlefield.append(bear)
        rules._execute_action_on_state(game, {
            "action": "grant_keywords", "player": rick.name,
            "keywords": ["Trample"], "target": "all_own_creatures",
        })
        assert "Trample" in (bear.temp_keywords or [])


# ---------------------------------------------------------------------------
# C3 — Leonin Vanguard
# ---------------------------------------------------------------------------

class TestLeoninVanguard:

    def test_pumps_only_itself(self, game, rules, lib, make_card):
        rick = game.players[0]
        lv = make_card("Leonin Vanguard", type_line="Creature — Cat Soldier",
                       power="1", toughness="1")
        other = make_card("Boros Elite", type_line="Creature — Human Soldier",
                          power="1", toughness="1")
        third = make_card("Cavalry Pegasus", type_line="Creature — Pegasus")
        rick.battlefield.extend([lv, other, third])
        actions, _d = lib.resolve_etb(
            "Leonin Vanguard", _oracle("leonin vanguard"), rick.name, "Claude",
            {"controller_creature_count": 3}, event_type="beginning_combat")
        for a in actions:
            rules._execute_action_on_state(game, a)
        game.recalculate_power_toughness()
        assert lv.get_effective_power(game) == 2
        assert other.get_effective_power(game) == 1, "Tier 3 used to pump the whole team"
        assert rick.life == 41

    def test_condition_is_enforced(self, lib):
        actions, _d = lib.resolve_etb(
            "Leonin Vanguard", _oracle("leonin vanguard"), "Rick", "Claude",
            {"controller_creature_count": 2}, event_type="beginning_combat")
        assert _kinds(actions) == ["no_action"]

    def test_it_no_longer_escalates_to_tier_three(self, lib):
        actions, _d = lib.resolve_etb(
            "Leonin Vanguard", _oracle("leonin vanguard"), "Rick", "Claude",
            {"controller_creature_count": 3}, event_type="beginning_combat")
        assert actions is not None, "None here means Tier 3 gets it again"


# ---------------------------------------------------------------------------
# C4 — Chulane's swallowed land drop
# ---------------------------------------------------------------------------

class TestChulaneCompoundTrigger:

    def test_both_halves_resolve(self, lib, make_card):
        hand = [make_card("Forest", type_line="Basic Land — Forest"),
                make_card("Llanowar Elves", type_line="Creature — Elf")]
        actions, _d = lib.resolve_etb(
            "Chulane, Teller of Tales", _oracle("chulane, teller of tales"),
            "Rick", "Claude", {"controller_hand": hand}, event_type="cast_trigger")
        assert "draw_cards" in _kinds(actions)
        move = next(a for a in actions if a["action"] == "move_card")
        assert move["card"] == "Forest"
        assert move["to_zone"] == "battlefield"

    def test_no_land_in_hand_is_still_a_draw(self, lib):
        actions, _d = lib.resolve_etb(
            "Chulane, Teller of Tales", _oracle("chulane, teller of tales"),
            "Rick", "Claude", {"controller_hand": []}, event_type="cast_trigger")
        assert _kinds(actions) == ["draw_cards"]

    def test_compound_trigger_is_not_claimed_by_the_inline_draw(self):
        """The suppression itself: a partial handler must leave the trigger for
        the template/Tier-3 path rather than resolve half of it and stop."""
        import inspect

        from mtg import triggers
        src = inspect.getsource(triggers)
        assert "[CAST-TRIGGER-PARTIAL]" in src, (
            "the compound-trigger fall-through must be greppable")

    def test_a_pure_draw_trigger_still_resolves_inline(self, make_game, make_card):
        """Beast Whisperer must not regress into a Tier-3 escalation: its whole
        trigger IS the draw, so the inline handler should still claim it."""
        import asyncio

        from mtg.engine import GameEngine
        from mtg.triggers import _check_cast_triggers

        engine = GameEngine(None)
        game = make_game()
        rick = game.players[0]
        game.active_player_index = 0
        rick.battlefield.append(make_card(
            "Beast Whisperer", type_line="Creature — Elf Druid",
            oracle_text="Whenever you cast a creature spell, draw a card."))
        rick.library.extend([make_card("Forest", type_line="Basic Land — Forest")
                             for _ in range(3)])
        before = len(rick.hand)
        spell = make_card("Grizzly Bears", type_line="Creature — Bear")
        asyncio.run(_check_cast_triggers(engine, game, rick, spell))
        assert len(rick.hand) == before + 1

    def test_a_compound_trigger_is_not_half_resolved(self, make_game, make_card):
        """The Chulane shape: the draw must NOT happen on its own with the rest
        of the sentence discarded and nothing queued."""
        import asyncio

        from mtg.engine import GameEngine
        from mtg.triggers import _check_cast_triggers

        engine = GameEngine(None)
        game = make_game()
        rick = game.players[0]
        game.active_player_index = 0
        rick.battlefield.append(make_card(
            "Chulane, Teller of Tales", type_line="Legendary Creature — Human Advisor",
            oracle_text=_oracle("chulane, teller of tales")))
        forest = make_card("Forest", type_line="Basic Land — Forest")
        rick.hand.append(forest)
        rick.library.extend([make_card(f"Island {i}", type_line="Basic Land — Island")
                             for i in range(3)])
        spell = make_card("Grizzly Bears", type_line="Creature — Bear")
        asyncio.run(_check_cast_triggers(engine, game, rick, spell))
        # Either the template resolved both halves (land onto the battlefield),
        # or the trigger was queued for Tier 3 — but it must never be "drew a
        # card and silently dropped the land drop".
        land_dropped = any(c.name == "Forest" for c in rick.battlefield)
        queued = bool(getattr(game, "_pending_triggers", None))
        assert land_dropped or queued, (
            "the ramp half was swallowed again — this is exactly the silent "
            "suppression the fix removes")


# ---------------------------------------------------------------------------
# C5 — the library-look shortcuts (three sites, NOT one shared cause)
# ---------------------------------------------------------------------------

class TestLibraryLookShortcutsDoNotSwallowClauses:

    def test_read_the_bones_keeps_all_three_clauses(self, lib):
        actions, _d = lib.resolve_spell(
            "Read the Bones", _oracle("read the bones"), "Rick", "Claude", {})
        assert _kinds(actions) == ["scry", "draw_cards", "lose_life"]
        draw = next(a for a in actions if a["action"] == "draw_cards")
        assert draw["amount"] == 2

    def test_notion_rain_keeps_all_three_clauses(self, lib):
        actions, _d = lib.resolve_spell(
            "Notion Rain", _oracle("notion rain"), "Rick", "Claude", {})
        assert _kinds(actions) == ["surveil", "draw_cards", "deal_damage"]

    def test_thought_erasure_is_not_claimed_as_a_library_look(self, lib):
        """Its surveil REMINDER text contains "look at the top card of your
        library"; the disruption half must still reach a tier that can do it."""
        actions, _d = lib.resolve_spell(
            "Thought Erasure", _oracle("thought erasure"), "Rick", "Claude", {})
        assert actions is None, (
            "returning a library-look no-op here resolved the spell and skipped "
            "the reveal/choose/discard entirely")

    def test_judge_gate_no_longer_swallows_discard_effects(self):
        from mtg import judge
        import inspect
        src = inspect.getsource(judge)
        for verb in ('"discard" not in effect_lower',
                     '"reveals their hand" not in effect_lower'):
            assert verb in src

    def test_pure_library_looks_still_short_circuit(self, lib):
        """The shortcut exists to avoid pointless Tier-3 calls — keep that."""
        actions, _d = lib.resolve_etb(
            "Temple of Mystery",
            "Temple of Mystery enters tapped. When Temple of Mystery enters, scry 1.",
            "Rick", "Claude", {})
        assert _kinds(actions) == ["scry"]

    def test_residual_detector_ignores_reminder_text(self):
        from rules.effect_templates import has_residual_clause_beyond_library_look as f
        # Scry's own reminder talks about putting cards on the bottom — that is
        # not a residual clause.
        assert not f("Scry 2. (To scry 2, look at the top two cards of your "
                     "library, then put any number of them on the bottom and the "
                     "rest on top in any order.)")
        assert f("Scry 2, then draw two cards. You lose 2 life.")

    def test_preordain_is_unaffected(self, lib):
        actions, _d = lib.resolve_spell(
            "Preordain", _oracle("preordain"), "Rick", "Claude", {})
        assert _kinds(actions) == ["scry", "draw_cards"]
