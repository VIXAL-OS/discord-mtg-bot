"""July 28, 2026 — Phase 1 of the July 27 12-agent fanout register.

Three findings that were verified against data/card_data_cache.json and the
code before any patch was written. All three come from the deck archetypes the
May 20 sampling fix had starved of attention for 3-4 audit cycles (escape,
graveyard/madness, mythic), which is why they survived so long.

1. KROXA'S LIFE LOSS WAS UNCONDITIONAL. Printed text:

       "Whenever Kroxa enters or attacks, each opponent discards a card, then
        each opponent who didn't discard a nonland card this way loses 3 life."

   `_gen_kroxa_etb` appended `lose_life` with no check of what was discarded.
   Independently confirmed by two reviewers in two different games (escape vs
   Meren, companion vs burn) — the strongest corroboration of the wave; an
   opponent was observed discarding Dictate of Erebos, a nonland, and still
   losing 3. The generator's docstring also had the condition BACKWARDS and
   said "damage" where the card says "life" (a pre-errata wording).

   The attack half of the same printed line ("enters or attacks") had no
   registration at all, so a Kroxa that stuck around was half a card.

2. create_copy_token NEVER SET is_token. The sibling token paths (populate at
   actions.py:1515, create_token at :1728) both set it. Observed consequence: a
   token copy died, did NOT cease to exist (CR 111.7 / 704.5d), sat in a
   graveyard, and was later reanimated by Animate Dead. While fixing it the
   block turned out to have NO entry plumbing at all either — neither the
   copy's own ETB nor PERMANENT_ENTERED — so a Rite of Replication copy of an
   ETB creature drew nothing and was invisible to the slice-2 bus.

3. FELDON OF THE THIRD PATH COPIED FROM THE WRONG ZONE. Printed text:

       "{2}{R}, {T}: Create a token that's a copy of target creature card in
        your graveyard, except it's an artifact in addition to its other types.
        It gains haste. Sacrifice it at the beginning of the next end step."

   The generic BATTLEFIELD copy pattern matched that text (its regex stopped at
   "creature" and never looked at " card in your graveyard"), and
   create_copy_token only scanned battlefields — observed copying a live
   creature with no creature in any graveyard at all.

   Note the resolution path, which dictates the shape of the fix: activated
   abilities reach the library via resolve_etb(event_type="activated"), and
   that event type is NOT in _NAME_KEYED_EVENT_TYPES (mtg/engine.py:3803,
   rules/effect_templates.py:341) — so only PATTERNS run. A name-keyed Feldon
   template could never fire on activation, and would wrongly fire on his ETB.
"""
import json
from pathlib import Path

import pytest


_CACHE = Path(__file__).resolve().parent.parent / "data" / "card_data_cache.json"


def _oracle(card_key):
    """Oracle text from the cache — never from memory (process note #4)."""
    if not _CACHE.exists():
        pytest.skip("card_data_cache.json not present")
    with open(_CACHE, encoding="utf-8") as fh:
        data = json.load(fh)
    entry = data.get(card_key)
    if entry is None:
        pytest.skip(f"{card_key!r} not in the card cache")
    return entry.get("oracle_text") or ""


def _activated_effect(card_key):
    """The post-colon half of an activated ability, exactly as the engine's
    own parser produces it (mtg/engine.py:3128-3145) and hands to
    resolve_etb(event_type="activated"). Deriving it here rather than
    hardcoding keeps the pin honest if the printed text ever changes — and
    matters mechanically, because resolve_etb strips whole activated-ability
    LINES before pattern matching (rules/effect_templates.py:494), so passing
    the full "{2}{R}, {T}: ..." line would test nothing."""
    for line in _oracle(card_key).split("\n"):
        if ":" in line and not line.strip().startswith("("):
            cost, effect = line.split(":", 1)
            if not any(kw in cost.lower()
                       for kw in ("when", "whenever", "at the beginning")):
                return effect.strip()
    pytest.skip(f"no activated ability found on {card_key!r}")


def _kroxa_ctx(hand=None, **extra):
    ctx = dict(extra)
    if hand is not None:
        ctx["opponent_hand"] = hand
    return ctx


def _actions_of(actions, kind):
    return [a for a in actions if a.get("action") == kind]


# ---------------------------------------------------------------------------
# 1. Kroxa
# ---------------------------------------------------------------------------

class TestKroxaOracleIsWhatWeThink:
    """The whole fix hinges on the direction of the condition. Pin it against
    the cache so a future edit can't quietly invert it back."""

    def test_condition_is_didnt_discard_a_nonland(self):
        text = _oracle("kroxa, titan of death's hunger").lower()
        assert "who didn't discard a nonland card this way loses 3 life" in text
        # The pre-errata "deals 3 damage" wording the old docstring described
        # is NOT on any current printing — life loss dodges damage prevention
        # and damage doublers, so the distinction is mechanical, not cosmetic.
        assert "deals 3 damage" not in text

    def test_sacrifice_clause_is_on_entry_not_upkeep(self):
        text = _oracle("kroxa, titan of death's hunger").lower()
        assert "when kroxa enters, sacrifice it unless it escaped" in text
        assert "beginning of your upkeep" not in text


class TestKroxaConditionalLifeLoss:

    def test_nonland_discard_spares_the_opponent(self, lib, make_card):
        hand = [make_card("Dictate of Erebos", type_line="Enchantment", cmc=5),
                make_card("Swamp", type_line="Basic Land — Swamp", cmc=0)]
        actions = lib._kroxa_enters_or_attacks("Claude", "Rick", _kroxa_ctx(hand))
        assert _actions_of(actions, "lose_life") == [], (
            "an opponent who discards a nonland card must NOT lose 3 life")
        discards = _actions_of(actions, "discard")
        assert len(discards) == 1
        assert discards[0]["card"] == "Dictate of Erebos"

    def test_all_lands_hand_takes_the_drain(self, lib, make_card):
        hand = [make_card("Swamp", type_line="Basic Land — Swamp", cmc=0),
                make_card("Mountain", type_line="Basic Land — Mountain", cmc=0)]
        actions = lib._kroxa_enters_or_attacks("Claude", "Rick", _kroxa_ctx(hand))
        loss = _actions_of(actions, "lose_life")
        assert len(loss) == 1 and loss[0]["amount"] == 3 and loss[0]["player"] == "Rick"
        assert len(_actions_of(actions, "discard")) == 1

    def test_empty_hand_discards_nothing_and_still_loses_three(self, lib):
        actions = lib._kroxa_enters_or_attacks("Claude", "Rick", _kroxa_ctx([]))
        assert _actions_of(actions, "discard") == [], (
            "an empty hand discards nothing — emitting a discard would be a lie")
        loss = _actions_of(actions, "lose_life")
        assert len(loss) == 1 and loss[0]["amount"] == 3

    def test_unknown_hand_never_fabricates_a_drain(self, lib):
        """No 'opponent_hand' key at all: under-apply rather than invent life
        loss we can't justify (cf. the Tier-3 fabricated-mana-payment class)."""
        actions = lib._kroxa_enters_or_attacks("Claude", "Rick", {})
        assert _actions_of(actions, "lose_life") == []
        assert _actions_of(actions, "discard")[0]["card"] == "random"

    def test_dict_shaped_hand_is_handled_too(self, lib):
        """Two ctx builders populate opponent_hand differently — Card objects
        (effect_templates.py:8615) and dicts (:9024). A fix that handles only
        one shape silently no-ops on half the call sites."""
        hand = [{"name": "Thoughtseize", "cmc": 1, "is_land": False},
                {"name": "Swamp", "cmc": 0, "is_land": True}]
        actions = lib._kroxa_enters_or_attacks("Claude", "Rick", _kroxa_ctx(hand))
        assert _actions_of(actions, "lose_life") == []
        assert _actions_of(actions, "discard")[0]["card"] == "Thoughtseize"

    def test_dict_shaped_all_lands_takes_the_drain(self, lib):
        hand = [{"name": "Swamp", "cmc": 0, "is_land": True}]
        actions = lib._kroxa_enters_or_attacks("Claude", "Rick", _kroxa_ctx(hand))
        assert len(_actions_of(actions, "lose_life")) == 1

    def test_picks_the_least_castable_nonland(self, lib, make_card):
        """Matches the engine's own 'worst card' convention in the discard
        handler: excess lands first, then the highest CMC."""
        hand = [make_card("Lightning Bolt", type_line="Instant", cmc=1),
                make_card("Emrakul", type_line="Creature — Eldrazi", cmc=15)]
        actions = lib._kroxa_enters_or_attacks("Claude", "Rick", _kroxa_ctx(hand))
        assert _actions_of(actions, "discard")[0]["card"] == "Emrakul"


class TestKroxaEtbAndAttackHalves:

    def test_etb_still_sacrifices_when_not_escaped(self, lib, make_card):
        hand = [make_card("Swamp", type_line="Basic Land — Swamp", cmc=0)]
        actions = lib._gen_kroxa_etb("Claude", "Rick", _kroxa_ctx(hand))
        sacs = _actions_of(actions, "sacrifice_permanent")
        assert len(sacs) == 1
        assert sacs[0]["preferred_card"] == "Kroxa, Titan of Death's Hunger"
        assert len(_actions_of(actions, "lose_life")) == 1

    def test_etb_keeps_kroxa_when_escaped(self, lib, make_card):
        hand = [make_card("Swamp", type_line="Basic Land — Swamp", cmc=0)]
        ctx = _kroxa_ctx(hand, was_escaped=True)
        actions = lib._gen_kroxa_etb("Claude", "Rick", ctx)
        assert _actions_of(actions, "sacrifice_permanent") == []

    def test_attack_template_is_registered(self, lib):
        assert "kroxa, titan of death's hunger" in lib._attack_templates

    def test_attacking_never_sacrifices_kroxa(self, lib, make_card):
        """The sacrifice clause is 'When Kroxa ENTERS' — attacking with him is
        not a downside."""
        hand = [make_card("Swamp", type_line="Basic Land — Swamp", cmc=0)]
        actions, _desc = lib.resolve_attack_trigger(
            trigger_card_name="Kroxa, Titan of Death's Hunger",
            trigger_oracle=_oracle("kroxa, titan of death's hunger"),
            attacking_creature_name="Kroxa, Titan of Death's Hunger",
            attacking_creature_power=6,
            controller="Claude", opponent="Rick",
            game_context=_kroxa_ctx(hand),
        )
        assert actions is not None, "the attack half must resolve, not fall through"
        assert _actions_of(actions, "sacrifice_permanent") == []
        assert len(_actions_of(actions, "lose_life")) == 1


# ---------------------------------------------------------------------------
# 2. create_copy_token
# ---------------------------------------------------------------------------

def _copy_action(**over):
    action = {"action": "create_copy_token", "player": "Claude",
              "target": "best_creature", "filter": "own", "count": 1}
    action.update(over)
    return action


class TestCopyTokenIsAToken:

    def test_copy_is_flagged_as_a_token(self, game, rules, make_card):
        claude = game.players[1]
        claude.battlefield.append(make_card("Grave Titan", power="6", toughness="6"))
        rules._execute_action_on_state(game, _copy_action())
        copies = [c for c in claude.battlefield if c.name == "Grave Titan"]
        assert len(copies) == 2
        assert any(getattr(c, "is_token", False) for c in copies), (
            "the copy must be a token — otherwise it survives death (CR 111.7)")

    def test_destroyed_copy_ceases_to_exist(self, game, rules, make_card):
        """The reported consequence: a copy died, stayed in the graveyard, and
        was later reanimated by Animate Dead."""
        claude = game.players[1]
        claude.battlefield.append(make_card("Grave Titan", power="6", toughness="6"))
        rules._execute_action_on_state(game, _copy_action())
        token = next(c for c in claude.battlefield if getattr(c, "is_token", False))
        rules._execute_action_on_state(
            game, {"action": "destroy", "card": token.name,
                   "target_controller": "Claude"})
        assert not any(getattr(c, "is_token", False) for c in claude.graveyard), (
            "a destroyed token must not linger in the graveyard as a "
            "reanimation target")

    def test_copy_gets_a_distinct_id(self, game, rules, make_card):
        claude = game.players[1]
        original = make_card("Grave Titan", power="6", toughness="6")
        claude.battlefield.append(original)
        rules._execute_action_on_state(game, _copy_action())
        ids = [c.id for c in claude.battlefield if c.name == "Grave Titan"]
        assert len(set(ids)) == 2, "same-name tokens sharing an id is a known bug class"

    def test_entry_is_announced_on_the_bus(self, game, rules, make_card):
        """PERMANENT_ENTERED (pub/sub slice 2) — the block had no emit at all,
        so Soul Warden-class watchers never saw a copy enter."""
        from mtg import events
        seen = []

        def _sub(gs, **payload):
            seen.append(payload)

        events.subscribe(events.PERMANENT_ENTERED, _sub)
        try:
            claude = game.players[1]
            claude.battlefield.append(make_card("Grave Titan", power="6", toughness="6"))
            rules._execute_action_on_state(game, _copy_action())
        finally:
            events.unsubscribe(events.PERMANENT_ENTERED, _sub)
        assert any(p.get("via") == "create_copy_token" for p in seen)


class TestCopyTokenFromGraveyard:

    def test_copies_a_card_out_of_the_controllers_graveyard(self, game, rules, make_card):
        claude = game.players[1]
        claude.graveyard.append(make_card("Grave Titan", power="6", toughness="6", cmc=6))
        rules._execute_action_on_state(
            game, _copy_action(zone="graveyard", target="Grave Titan"))
        assert [c.name for c in claude.battlefield] == ["Grave Titan"]
        assert getattr(claude.battlefield[0], "is_token", False)

    def test_the_original_card_stays_in_the_graveyard(self, game, rules, make_card):
        """It's a COPY — the card itself doesn't move."""
        claude = game.players[1]
        claude.graveyard.append(make_card("Grave Titan", power="6", toughness="6", cmc=6))
        rules._execute_action_on_state(
            game, _copy_action(zone="graveyard", target="Grave Titan"))
        assert [c.name for c in claude.graveyard] == ["Grave Titan"]

    def test_never_reaches_across_to_a_live_creature(self, game, rules, make_card):
        """The exact observed bug: an empty graveyard must produce NOTHING, not
        a copy of somebody's battlefield creature."""
        rick, claude = game.players
        rick.battlefield.append(make_card("Craterhoof Behemoth", power="5", toughness="5"))
        claude.battlefield.append(make_card("Feldon of the Third Path",
                                            power="2", toughness="3"))
        rules._execute_action_on_state(
            game, _copy_action(zone="graveyard", target="best_creature"))
        assert [c.name for c in claude.battlefield] == ["Feldon of the Third Path"]
        assert len(rick.battlefield) == 1

    def test_opponent_graveyard_is_out_of_reach_when_scoped_to_controller(
            self, game, rules, make_card):
        rick, claude = game.players
        rick.graveyard.append(make_card("Grave Titan", power="6", toughness="6", cmc=6))
        rules._execute_action_on_state(
            game, _copy_action(zone="graveyard", zone_owner="controller",
                               target="Grave Titan"))
        assert claude.battlefield == []

    def test_extra_types_and_keywords_are_applied(self, game, rules, make_card):
        """Feldon's "except it's an artifact in addition to its other types"
        plus "It gains haste"."""
        claude = game.players[1]
        claude.graveyard.append(make_card("Grave Titan", power="6", toughness="6", cmc=6))
        rules._execute_action_on_state(
            game, _copy_action(zone="graveyard", target="Grave Titan",
                               extra_types=["Artifact"], keywords=["Haste"]))
        token = claude.battlefield[0]
        assert "artifact" in token.type_line.lower()
        assert "creature" in token.type_line.lower(), "the added type is IN ADDITION"
        assert any(k.lower() == "haste" for k in token.keywords)


# ---------------------------------------------------------------------------
# 3. Feldon / the zone-qualified pattern guard
# ---------------------------------------------------------------------------

class TestGraveyardCopyPattern:

    def test_feldons_ability_no_longer_copies_from_the_battlefield(self, lib, make_card):
        """The regression proper. Resolve Feldon's ability text the way the
        activation path does (event_type='activated', which skips name-keyed
        templates) and assert the produced action targets the GRAVEYARD."""
        rick_gy = [make_card("Grave Titan", type_line="Creature — Giant", cmc=6)]
        ctx = {"controller_graveyard": rick_gy}
        actions, _desc = lib.resolve_etb(
            card_name="Feldon of the Third Path",
            oracle_text=_activated_effect("feldon of the third path"),
            controller="Claude", opponent="Rick",
            game_context=ctx, event_type="activated",
        )
        assert actions is not None, "the ability must resolve at Tier 1.5"
        copies = _actions_of(actions, "create_copy_token")
        assert len(copies) == 1
        assert copies[0]["zone"] == "graveyard"
        assert copies[0]["zone_owner"] == "controller"
        assert copies[0]["target"] == "Grave Titan"

    def test_feldon_adds_artifact_haste_and_the_end_step_sacrifice(self, lib, make_card):
        ctx = {"controller_graveyard": [
            make_card("Grave Titan", type_line="Creature — Giant", cmc=6)]}
        actions, _desc = lib.resolve_etb(
            card_name="Feldon of the Third Path",
            oracle_text=_activated_effect("feldon of the third path"),
            controller="Claude", opponent="Rick",
            game_context=ctx, event_type="activated",
        )
        copy = _actions_of(actions, "create_copy_token")[0]
        assert copy.get("extra_types") == ["Artifact"]
        assert copy.get("keywords") == ["Haste"]
        delayed = _actions_of(actions, "schedule_delayed_trigger")
        assert len(delayed) == 1
        assert delayed[0]["trigger_at"] == "end_step"
        # "the next end step", not "your next end step" — no owner gate.
        assert delayed[0].get("phase_of") is None
        inner = delayed[0]["actions"][0]
        assert inner["action"] == "sacrifice_permanent"
        assert inner["preferred_card"] == "Grave Titan"

    def test_empty_graveyard_is_a_handled_no_op(self, lib):
        actions, _desc = lib.resolve_etb(
            card_name="Feldon of the Third Path",
            oracle_text=_activated_effect("feldon of the third path"),
            controller="Claude", opponent="Rick",
            game_context={"controller_graveyard": []}, event_type="activated",
        )
        assert actions is not None
        assert [a["action"] for a in actions] == ["no_action"]

    def test_declared_target_wins_over_the_heuristic(self, lib, make_card):
        """Templates ignoring explicit_target_name is a recurring class in this
        register — don't add another one."""
        ctx = {"controller_graveyard": [
            make_card("Grave Titan", type_line="Creature — Giant", cmc=6),
            make_card("Llanowar Elves", type_line="Creature — Elf", cmc=1)],
            "explicit_target_name": "Llanowar Elves"}
        actions, _desc = lib.resolve_etb(
            card_name="Feldon of the Third Path",
            oracle_text=_activated_effect("feldon of the third path"),
            controller="Claude", opponent="Rick",
            game_context=ctx, event_type="activated",
        )
        assert _actions_of(actions, "create_copy_token")[0]["target"] == "Llanowar Elves"


class TestBattlefieldCopyPatternStillWorks:
    """The guard must not cost us the cards it isn't about."""

    def test_rite_of_replication_style_text_still_matches(self, lib):
        actions, _desc = lib.resolve_etb(
            card_name="Helm of the Host",
            oracle_text=("At the beginning of combat on your turn, create a token "
                         "that's a copy of equipped creature."),
            controller="Claude", opponent="Rick",
            game_context={}, event_type="activated",
        )
        # The 'equipped creature' wording isn't graveyard-qualified, so if this
        # pattern matches at all it must stay on the battlefield.
        if actions:
            for a in _actions_of(actions, "create_copy_token"):
                assert a.get("zone", "battlefield") == "battlefield"

    def test_plain_target_creature_copy_stays_on_the_battlefield(self, lib):
        actions, _desc = lib.resolve_etb(
            card_name="Thousand-Faced Shadow",
            oracle_text="Create a token that's a copy of target creature you control.",
            controller="Claude", opponent="Rick",
            game_context={}, event_type="activated",
        )
        assert actions is not None
        assert _actions_of(actions, "create_copy_token")[0].get(
            "zone", "battlefield") == "battlefield"

    def test_the_guard_is_about_the_word_card_not_about_feldon(self, lib):
        """`(?!\\s+card\\b)` encodes an MTG templating rule: "permanent" means a
        battlefield object, "card" means one in another zone. Any future card
        with that shape is covered, not just Feldon."""
        import re
        pattern = next(p for p, t in lib._pattern_templates
                       if t.name == "Copy Token")
        assert not re.search(pattern,
                             "create a token that's a copy of target creature card "
                             "in your graveyard")
        assert re.search(pattern,
                         "create a token that's a copy of target creature you control")
