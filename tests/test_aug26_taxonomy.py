"""The MaRo spell-category taxonomy audit fixes (Aug 26, 2026).

The design-skeleton pass probed one canonical card per common spell category
through the REAL resolvers and found: both destroy generators ignored printed
qualifiers AND the spell form never honored declared targets (Smite the
Monstrous destroyed a 2/2 against "power 4 or greater" — CR 601.2c); the
frozen-tap family (Frost Lynx) reached Tier 3, whose tap vocabulary had no
frozen flag, silently dropping the printed rider; Mind Rot / Raise Dead were
Tier-3 calls for fully deterministic shapes; and team-pump spells had no
reliable team-scope path (Trumpet Blast risked pumping defenders). Oracle
constants are bulk-verified verbatim.
"""

import pytest

from rules.effect_templates import build_game_context, get_effect_library


SMITE = "Destroy target creature with power 4 or greater."
PLUMMET = "Destroy target creature with flying."
MURDER = "Destroy target creature."
FROST_LYNX = ("Flying? no —\nWhen this creature enters, tap target creature "
              "an opponent controls. That creature doesn't untap during its "
              "controller's next untap step.")
FROST_LYNX_REAL = ("When this creature enters, tap target creature an "
                   "opponent controls. That creature doesn't untap during "
                   "its controller's next untap step.")
FROST_BREATH = ("Tap up to two target creatures. Those creatures don't untap "
                "during their controllers' next untap steps.")
MIND_ROT = "Target player discards two cards."
RAISE_DEAD = "Return target creature card from your graveyard to your hand."
TRUMPET_BLAST = "Attacking creatures get +2/+0 until end of turn."
INSPIRED_CHARGE = "Creatures you control get +2/+1 until end of turn."
OVERRUN = "Creatures you control get +3/+3 and gain trample until end of turn."


@pytest.fixture
def lib():
    return get_effect_library()


def _board(game, make_card, opp_cards):
    rick, claude = game.players
    for c in opp_cards:
        claude.battlefield.append(c)
    return build_game_context(game, rick, claude)


def _creature(make_card, name, power="3", toughness="3", **kw):
    kw.setdefault("type_line", "Creature — Bear")
    return make_card(name, power=power, toughness=toughness, **kw)


class TestQualifiedDestroy:
    def test_power_bound_enforced(self, game, make_card, lib):
        small = _creature(make_card, "Small Bear", power="2")
        big = _creature(make_card, "Big Giant", power="5", toughness="5")
        ctx = _board(game, make_card, [small, big])
        actions, _ = lib.resolve_spell("Smite the Monstrous", SMITE,
                                       "Rick", "Claude", game_context=ctx)
        assert actions and actions[0]["card"] == "Big Giant", (
            "'power 4 or greater' must never destroy the 2-power creature")

    def test_power_bound_with_no_legal_target_is_noop(self, game, make_card, lib):
        small = _creature(make_card, "Small Bear", power="2")
        ctx = _board(game, make_card, [small])
        actions, _ = lib.resolve_spell("Smite the Monstrous", SMITE,
                                       "Rick", "Claude", game_context=ctx)
        assert actions and actions[0]["action"] == "no_action"

    def test_flying_qualifier_enforced(self, game, make_card, lib):
        ground = _creature(make_card, "Hill Giant", power="5", toughness="5")
        flyer = _creature(make_card, "Wind Drake", power="2", toughness="2",
                          type_line="Creature — Drake",
                          oracle_text="Flying", keywords=["Flying"])
        ctx = _board(game, make_card, [ground, flyer])
        actions, _ = lib.resolve_spell("Plummet", PLUMMET,
                                       "Rick", "Claude", game_context=ctx)
        assert actions and actions[0]["card"] == "Wind Drake", (
            "'with flying' must pick the flyer, not the biggest creature")

    def test_unqualified_destroy_picks_best_creature(self, game, make_card, lib):
        small = _creature(make_card, "Small Bear", power="2")
        big = _creature(make_card, "Big Giant", power="5", toughness="5")
        ctx = _board(game, make_card, [small, big])
        actions, _ = lib.resolve_spell("Murder", MURDER,
                                       "Rick", "Claude", game_context=ctx)
        assert actions and actions[0]["card"] == "Big Giant"

    def test_declared_target_honored_and_validated(self, game, make_card, lib):
        rick, claude = game.players
        small = _creature(make_card, "Small Bear", power="2")
        big = _creature(make_card, "Big Giant", power="5", toughness="5")
        claude.battlefield.extend([small, big])
        # Declared legal target beats the auto-pick.
        ctx = build_game_context(game, rick, claude, explicit_target=big)
        actions, _ = lib.resolve_spell("Smite the Monstrous", SMITE,
                                       "Rick", "Claude", game_context=ctx)
        assert actions and actions[0]["card"] == "Big Giant"
        # Declared ILLEGAL target (fails the power bound) declines — never
        # silently retargets (the Abrupt Decay precedent).
        ctx = build_game_context(game, rick, claude, explicit_target=small)
        actions, _ = lib.resolve_spell("Smite the Monstrous", SMITE,
                                       "Rick", "Claude", game_context=ctx)
        assert actions is None

    def test_unparseable_qualifier_declines(self, game, make_card, lib):
        big = _creature(make_card, "Big Giant", power="5", toughness="5")
        ctx = _board(game, make_card, [big])
        # The fixture must be a qualifier the destroy REGEX captures (word
        # characters only — a '+1/+1 counter' qualifier never reaches the
        # predicate because '+' isn't in the capture class, so a mutant
        # disabling the decline survives on it; the sweep caught exactly
        # that) but the predicate cannot parse.
        actions, _ = lib.resolve_spell(
            "Synthetic Doom",
            "Destroy target creature with power less than the number of "
            "cards in your hand.",
            "Rick", "Claude", game_context=ctx)
        assert actions is None, (
            "an unparseable qualifier must decline to the restriction-aware "
            "tiers, never destroy an arbitrary creature")

    def test_noncreature_negation_still_holds(self, game, make_card, lib):
        # The Woodfall 'noncreature' pin's property survives the rewrite.
        cre = _creature(make_card, "Doomed Traveler", power="1", toughness="1")
        art = make_card("Mind Stone", type_line="Artifact",
                        oracle_text="{T}: Add {C}.", power=None,
                        toughness=None)
        ctx = _board(game, make_card, [cre, art])
        actions, _ = lib.resolve_etb(
            "Woodfall Test",
            "When this creature enters, destroy target noncreature permanent.",
            "Rick", "Claude", game_context=ctx)
        assert actions and actions[0].get("card") == "Mind Stone"


class TestFrozenTap:
    def test_frost_lynx_taps_and_freezes(self, game, make_card, lib):
        bear = _creature(make_card, "Grizzly Bears")
        ctx = _board(game, make_card, [bear])
        actions, _ = lib.resolve_etb("Frost Lynx", FROST_LYNX_REAL,
                                     "Rick", "Claude", game_context=ctx)
        assert actions and actions[0]["action"] == "tap"
        assert actions[0]["skip_next_untap"] is True, (
            "the printed frozen rider must reach the action — Tier 3 was "
            "dropping it")

    def test_frost_breath_freezes_up_to_two(self, game, make_card, lib):
        b1 = _creature(make_card, "Bear One")
        b2 = _creature(make_card, "Bear Two")
        b3 = _creature(make_card, "Bear Three")
        ctx = _board(game, make_card, [b1, b2, b3])
        actions, _ = lib.resolve_spell("Frost Breath", FROST_BREATH,
                                       "Rick", "Claude", game_context=ctx)
        assert actions is not None and len(actions) == 2
        assert all(a["skip_next_untap"] for a in actions)

    def test_frozen_tap_action_sets_the_flag_end_to_end(self, game, rules, make_card):
        rick, claude = game.players
        bear = _creature(make_card, "Grizzly Bears")
        claude.battlefield.append(bear)
        rules._execute_action_on_state(game, {
            "action": "tap", "card": "Grizzly Bears",
            "skip_next_untap": True})
        assert bear.tapped and bear._skip_next_untap is True


class TestDeterministicShapes:
    def test_mind_rot_discards_two(self, game, make_card, lib):
        ctx = _board(game, make_card, [])
        actions, _ = lib.resolve_spell("Mind Rot", MIND_ROT,
                                       "Rick", "Claude", game_context=ctx)
        assert actions is not None and len(actions) == 2
        assert all(a["action"] == "discard" and a["player"] == "Claude"
                   for a in actions)

    def test_mind_rot_with_trailing_clause_declines(self, game, make_card, lib):
        ctx = _board(game, make_card, [])
        actions, _ = lib.resolve_spell(
            "Synthetic Rot",
            "Target player discards two cards. You gain 2 life.",
            "Rick", "Claude", game_context=ctx)
        assert actions is None, "the compound-drop class"

    def test_raise_dead_returns_best_creature(self, game, make_card, lib):
        rick, claude = game.players
        small = _creature(make_card, "Small Bear", power="2")
        big = _creature(make_card, "Big Giant", power="5", toughness="5")
        sorcery = make_card("Some Sorcery", type_line="Sorcery",
                            power=None, toughness=None)
        rick.graveyard.extend([small, big, sorcery])
        ctx = build_game_context(game, rick, claude)
        actions, _ = lib.resolve_spell("Raise Dead", RAISE_DEAD,
                                       "Rick", "Claude", game_context=ctx)
        assert actions and actions[0]["action"] == "move_card"
        assert actions[0]["card"] == "Big Giant"
        assert actions[0]["player"] == "Rick"

    def test_raise_dead_empty_graveyard_is_handled_fizzle(self, game, make_card, lib):
        ctx = _board(game, make_card, [])
        actions, _ = lib.resolve_spell("Raise Dead", RAISE_DEAD,
                                       "Rick", "Claude", game_context=ctx)
        assert actions == []

    def test_up_to_two_variant_declines(self, game, make_card, lib):
        ctx = _board(game, make_card, [])
        actions, _ = lib.resolve_spell(
            "Grim Discovery Test",
            "Return up to two target creature cards from your graveyard to "
            "your hand.",
            "Rick", "Claude", game_context=ctx)
        assert actions is None, "full-line anchor: variants go to Tier 3"


class TestTeamPump:
    def test_inspired_charge_pumps_the_team(self, game, make_card, lib):
        ctx = _board(game, make_card, [])
        actions, _ = lib.resolve_spell("Inspired Charge", INSPIRED_CHARGE,
                                       "Rick", "Claude", game_context=ctx)
        assert actions and actions[0]["action"] == "pump_all_creatures"
        assert actions[0]["power"] == 2 and actions[0]["toughness"] == 1
        assert not actions[0].get("only_attacking")

    def test_trumpet_blast_scopes_to_attackers(self, game, make_card, lib):
        ctx = _board(game, make_card, [])
        actions, _ = lib.resolve_spell("Trumpet Blast", TRUMPET_BLAST,
                                       "Rick", "Claude", game_context=ctx)
        assert actions and actions[0]["only_attacking"] is True, (
            "Trumpet Blast must never pump defenders")

    def test_overrun_carries_the_keyword(self, game, make_card, lib):
        ctx = _board(game, make_card, [])
        actions, _ = lib.resolve_spell("Overrun", OVERRUN,
                                       "Rick", "Claude", game_context=ctx)
        assert actions and actions[0]["keywords"] == ["Trample"]

    def test_compound_pump_declines(self, game, make_card, lib):
        ctx = _board(game, make_card, [])
        actions, _ = lib.resolve_spell(
            "Synthetic Charge",
            "Creatures you control get +1/+1 until end of turn and you gain "
            "1 life.",
            "Rick", "Claude", game_context=ctx)
        assert actions is None, "same-sentence compound guard"
        actions, _ = lib.resolve_spell(
            "Synthetic Two-Line",
            "Draw a card.\nCreatures you control get +1/+1 until end of turn.",
            "Rick", "Claude", game_context=ctx)
        assert actions is None, "sibling-line guard (the compound-drop class)"
        # The TRIGGER form is where the same-sentence guard is the ONLY
        # defence — the spell branch's own before/after check double-covers
        # the spell form, so a mutant deleting the guard survives a
        # spell-only fixture (the sweep caught exactly that).
        actions, _ = lib.resolve_attack_trigger(
            "Synthetic Battle Shout",
            "Whenever this creature attacks, creatures you control get "
            "+2/+0 until end of turn and you gain 1 life.",
            "Synthetic Battle Shout", 3, "Rick", "Claude",
            game_context=dict(ctx))
        assert actions is None, (
            "a compound trigger clause must decline, never resolve the pump "
            "and drop the life gain")
