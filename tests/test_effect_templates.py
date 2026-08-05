"""Tier 1.5 template library — table-driven tests.

resolve_etb() / tier_for_card() are pure functions (strings in, JSON actions
out): no game object, no Discord, no LLM. This is the cheapest place to pin
behavior the autoplay audits already paid to discover once.

Convention: each test names the audit that motivated it. When a future audit
fix touches a template, add a row here — the repro IS the deliverable.
"""
import pytest


def _actions_of(result):
    actions, desc = result
    assert actions is not None, f"expected a template/pattern hit, got a miss (desc={desc!r})"
    return actions


class TestNameKeyedTemplates:
    def test_mulldrifter_etb_draws_two(self, lib):
        actions = _actions_of(lib.resolve_etb(
            "Mulldrifter", "When Mulldrifter enters, draw two cards.",
            "Rick", "Claude"))
        draws = [a for a in actions if a["action"] == "draw_cards"]
        assert len(draws) == 1
        assert draws[0]["player"] == "Rick"
        assert int(draws[0]["amount"]) == 2

    def test_geralfs_messenger_etb_drains_two(self, lib):
        # May 26 audit: the "vanilla undying" no-action list swallowed the
        # ETB drain on the initial cast AND on every undying return.
        actions = _actions_of(lib.resolve_etb(
            "Geralf's Messenger",
            "When Geralf's Messenger enters the battlefield, target opponent loses 2 life.",
            "Rick", "Claude"))
        assert actions == [{"action": "lose_life", "player": "Claude", "amount": 2}]

    def test_geralfs_messenger_dies_does_not_refire_etb(self, lib):
        # Companion to the above: undying is SBA-handled, so the dies event
        # must NOT re-run the ETB drain (CR 603.6c fires it on re-entry instead).
        actions = _actions_of(lib.resolve_etb(
            "Geralf's Messenger",
            "When Geralf's Messenger enters the battlefield, target opponent loses 2 life.",
            "Rick", "Claude", event_type="dies"))
        assert all(a["action"] == "no_action" for a in actions)

    def test_solemn_simulacrum_dies_draws_one(self, lib):
        # May 17 audit: ETB template and dies template collided on the same
        # _card_templates key; only ONE side of the trigger was discoverable.
        # The dies-templates registry split fixed it.
        actions = _actions_of(lib.resolve_etb(
            "Solemn Simulacrum",
            "When Solemn Simulacrum dies, you may draw a card.",
            "Rick", "Claude", event_type="dies"))
        draws = [a for a in actions if a["action"] == "draw_cards"]
        assert len(draws) == 1
        assert draws[0]["player"] == "Rick"
        assert int(draws[0]["amount"]) == 1

    @pytest.mark.parametrize("key", ["Cathars' Crusade", "Cathar's Crusade"])
    def test_cathars_crusade_both_spellings(self, lib, key):
        # May 17 audit: the real card name has the apostrophe AFTER the s;
        # the template was registered under the wrong key and silently fell
        # through to an over-aggressive regex that emitted a SELF-counter.
        # Both spellings must resolve to the same bulk action.
        ctx = {"_controller_creatures": ["Soldier", "Soldier", "Champion"]}
        actions = _actions_of(lib.resolve_etb(
            key,
            "Whenever a creature you control enters, put a +1/+1 counter "
            "on each creature you control.",
            "Rick", "Claude", game_context=ctx))
        assert len(actions) == 1
        a = actions[0]
        # May 13 audit: must be ONE bulk action (applied by identity), not
        # N name-keyed actions that collapse onto the first same-named token.
        assert a["action"] == "add_counters"
        assert a["target"] == "all_own_creatures"
        assert a["player"] == "Rick"


class TestScheduledVsEtbRouting:
    """May 25 audit (F25): Agent of Treachery's bare-name ETB template was
    firing on END STEP dispatch too — stealing one permanent per turn,
    cascading control-theft across the game. Scheduled events must route to
    the suffix-keyed template, never the ETB one."""

    ORACLE = (
        "When Agent of Treachery enters the battlefield, gain control of "
        "target permanent. At the beginning of your end step, if you control "
        "three or more permanents you don't own, draw three cards."
    )

    def test_etb_event_steals(self, lib):
        actions = _actions_of(lib.resolve_etb(
            "Agent of Treachery", self.ORACLE, "Rick", "Claude"))
        assert actions[0]["action"] == "steal_permanent"

    def test_end_step_event_must_not_steal(self, lib):
        actions, _ = lib.resolve_etb(
            "Agent of Treachery", self.ORACLE, "Rick", "Claude",
            game_context={"controller_stolen_count": 3}, event_type="end_step")
        assert actions is not None
        assert all(a["action"] != "steal_permanent" for a in actions)
        draws = [a for a in actions if a["action"] == "draw_cards"]
        assert draws and int(draws[0]["amount"]) == 3

    def test_end_step_under_three_stolen_is_noop(self, lib):
        actions, _ = lib.resolve_etb(
            "Agent of Treachery", self.ORACLE, "Rick", "Claude",
            game_context={"controller_stolen_count": 1}, event_type="end_step")
        assert actions is not None
        assert all(a["action"] == "no_action" for a in actions)


class TestPatternFamilies:
    """Made-up card names prove the regex families fire for cards that have
    no name-keyed template — the whole point of Tier 1.5 patterns."""

    def test_etb_draw_pattern_on_unregistered_card(self, lib):
        actions = _actions_of(lib.resolve_etb(
            "Testudo Skyfin",
            "When Testudo Skyfin enters, draw three cards.",
            "Rick", "Claude"))
        draws = [a for a in actions if a["action"] == "draw_cards"]
        assert draws
        assert draws[0]["player"] == "Rick"
        assert int(draws[0]["amount"]) == 3

    def test_etb_token_pattern_on_unregistered_card(self, lib):
        actions = _actions_of(lib.resolve_etb(
            "Testudo Marshal",
            "When Testudo Marshal enters, create two 1/1 white Soldier creature tokens.",
            "Rick", "Claude"))
        tokens = [a for a in actions if a["action"] == "create_token"]
        assert tokens
        t = tokens[0]
        assert int(t.get("count", 1)) == 2
        assert int(t["power"]) == 1
        assert int(t["toughness"]) == 1


class TestTierForCard:
    """Pure coverage-classification lookup (feeds mtg/coverage.py reports)."""

    def test_template_hit_both_apostrophe_spellings(self, lib):
        assert lib.tier_for_card("Cathars' Crusade") == "template"
        assert lib.tier_for_card("Cathar's Crusade") == "template"

    def test_pattern_hit(self, lib):
        assert lib.tier_for_card(
            "Testudo Skyfin", "When Testudo Skyfin enters, draw three cards.") == "pattern"

    def test_vanilla_reports_tier3(self, lib):
        assert lib.tier_for_card("Grizzly Bears", "") == "tier3"


class TestMoreBurnedCards:
    """Each row pins a card that already cost an audit cycle."""

    def test_spell_queller_etb_emits_stack_exile(self, lib):
        # May 18 audit: Queller's exile_from_stack action silently fizzled
        # every cast (isinstance-dict check against StackEntry dataclasses).
        # The template side must emit the real action with the MV 4 cap.
        actions = _actions_of(lib.resolve_etb(
            "Spell Queller",
            "When Spell Queller enters the battlefield, exile target spell "
            "with mana value 4 or less until Spell Queller leaves the battlefield.",
            "Rick", "Claude"))
        assert actions == [{"action": "exile_from_stack",
                            "controller": "Rick", "max_mv": 4,
                            "silent_on_no_result": True}]

    def test_bitterblossom_upkeep_fires_both_halves(self, lib):
        # Bug B (May 16): the outer event-type gate bypassed name-keyed
        # templates for upkeep dispatch — Bitterblossom never resolved.
        # F25 regression watch (May 25): scheduled-prefix templates must
        # STILL fire on their scheduled event after the F25 tightening.
        actions = _actions_of(lib.resolve_etb(
            "Bitterblossom",
            "At the beginning of your upkeep, you lose 1 life and create "
            "a 1/1 black Faerie Rogue creature token with flying.",
            "Rick", "Claude", event_type="upkeep"))
        lose = next(a for a in actions if a["action"] == "lose_life")
        assert lose["player"] == "Rick" and int(lose["amount"]) == 1
        tok = next(a for a in actions if a["action"] == "create_token")
        assert tok["name"] == "Faerie Rogue"
        assert "Flying" in tok.get("keywords", "")

    def test_phyrexian_arena_upkeep(self, lib):
        # Same Bug B class — scheduled name-keyed template on upkeep dispatch.
        actions = _actions_of(lib.resolve_etb(
            "Phyrexian Arena",
            "At the beginning of your upkeep, you draw a card and you lose 1 life.",
            "Rick", "Claude", event_type="upkeep"))
        assert {"action": "draw_cards", "player": "Rick", "amount": 1} in actions
        assert {"action": "lose_life", "player": "Rick", "amount": 1} in actions

    def test_kambal_opponent_cast_trigger(self, lib):
        # Bug B (May 16): cast_trigger dispatch must reach name-keyed templates.
        actions = _actions_of(lib.resolve_etb(
            "Kambal, Consul of Allocation",
            "Whenever an opponent casts a noncreature spell, that player "
            "loses 2 life and you gain 2 life.",
            "Rick", "Claude", event_type="cast_trigger"))
        assert {"action": "lose_life", "player": "Claude", "amount": 2} in actions
        assert {"action": "gain_life", "player": "Rick", "amount": 2} in actions

    def test_hornet_queen_tokens_carry_keywords(self, lib):
        # May 17 audit: token actions dropped registry keywords/colors —
        # Hornet Queen's Insects came out as 1/1 vanilla, no flying/deathtouch.
        actions = _actions_of(lib.resolve_etb(
            "Hornet Queen",
            "When Hornet Queen enters the battlefield, create four 1/1 green "
            "Insect creature tokens with flying and deathtouch.",
            "Rick", "Claude"))
        tok = next(a for a in actions if a["action"] == "create_token")
        assert int(tok["count"]) == 4
        assert "Flying" in tok["keywords"] and "Deathtouch" in tok["keywords"]
        assert tok.get("colors") == "G"

    def test_rampaging_ferocidon_hits_entering_controller(self, lib):
        # Apr 6 audit family: the creature-enters trigger damages the
        # ENTERING creature's controller, not a fixed player.
        actions = _actions_of(lib.resolve_etb(
            "Rampaging Ferocidon",
            "Whenever another creature enters the battlefield, Rampaging "
            "Ferocidon deals 1 damage to that creature's controller.",
            "Rick", "Claude",
            game_context={"entering_creature_controller": "Rick"}))
        assert actions == [{"action": "deal_damage", "amount": 1,
                            "target_player": "Rick"}]


class TestFinaleOfDevastation:
    """May 30 audit: no template existed — the 12-mana finisher escalated to
    Tier 3, which returned no actions, and the spell did NOTHING (bare
    "resolves." shown to players)."""

    ORACLE = ("Search your library and/or graveyard for a creature card with "
              "mana value X or less and put it onto the battlefield. If X is 10 "
              "or more, creatures you control get +X/+X and gain trample until "
              "end of turn.")

    def test_small_x_pumps_only(self, lib):
        actions, _ = lib.resolve_spell(
            "Finale of Devastation", self.ORACLE, "Rick", "Claude",
            game_context={"x_value": 5})
        assert actions is not None
        pump = next(a for a in actions if a["action"] == "pump_all_creatures")
        assert pump["power"] == 5 and pump["toughness"] == 5
        assert "Trample" in pump["keywords"]
        assert all(a["action"] != "search_library" for a in actions)

    def test_x_ten_or_more_adds_battlefield_tutor(self, lib):
        actions, _ = lib.resolve_spell(
            "Finale of Devastation", self.ORACLE, "Rick", "Claude",
            game_context={"x_value": 12})
        assert actions is not None
        search = next(a for a in actions if a["action"] == "search_library")
        assert search["to_zone"] == "battlefield"
