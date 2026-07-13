"""execute_action_on_state — the JSON action interpreter (mtg/actions.py).

Every tier (templates, SpellResolver, XMage translator, Tier 3 judge)
ultimately funnels into these handlers, so they're the highest-traffic
deterministic code in the engine.

Setup is a bare GameState + clientless RulesEngine: engine_ref (trigger
fan-out into GameEngine) is absent and every handler hasattr-guards it, so
these tests exercise pure state mutation — triggers are autoplay's job.
"""
import pytest

from mtg.actions import execute_action_on_state
from mtg.models import StackEntry


def run(rules, game, action):
    # dict() copy: the interpreter enriches the action in place
    # (_source_card_name etc.) — don't leak that between parametrized runs.
    return execute_action_on_state(rules, game, dict(action))


class TestDealDamage:
    def test_to_player(self, rules, game):
        msg = run(rules, game, {"action": "deal_damage", "amount": 3,
                                "target_player": "Claude", "source": "Lightning Bolt"})
        assert game.players[1].life == 37
        assert "Lightning Bolt" in msg and "3" in msg

    def test_to_creature_marks_damage(self, rules, game, make_card):
        bear = make_card("Runeclaw Bear")
        game.players[1].battlefield.append(bear)
        run(rules, game, {"action": "deal_damage", "amount": 2,
                          "target_card": "Runeclaw Bear"})
        assert bear.damage_marked == 2

    def test_prevention_flag_blocks_damage(self, rules, game):
        # Apr 4 audit: Teferi's Protection now actually prevents damage.
        claude = game.players[1]
        claude._damage_prevented = True
        msg = run(rules, game, {"action": "deal_damage", "amount": 5,
                                "target_player": "Claude"})
        assert claude.life == 40
        assert "prevent" in msg.lower()

    def test_lethal_sentinel_not_displayed_raw(self, rules, game):
        # May 20 audit (#18): synthetic >=999 sentinel amounts display as
        # "lethal", never the raw number.
        msg = run(rules, game, {"action": "deal_damage", "amount": 999,
                                "target_player": "Claude"})
        assert "lethal" in msg
        assert "999" not in msg

    def test_negative_life_clamped_in_display_only(self, rules, game):
        # May 18 audit: display clamps at 0; true life may go negative
        # internally (CR 119.3) so SBAs still see the real total.
        claude = game.players[1]
        claude.life = 5
        msg = run(rules, game, {"action": "deal_damage", "amount": 12,
                                "target_player": "Claude"})
        assert claude.life == -7
        assert "-7" not in msg
        assert "life: 0" in msg


class TestLifeActions:
    def test_gain_life(self, rules, game):
        run(rules, game, {"action": "gain_life", "player": "Rick", "amount": 4})
        assert game.players[0].life == 44

    def test_lose_life(self, rules, game):
        run(rules, game, {"action": "lose_life", "player": "Rick", "amount": 3})
        assert game.players[0].life == 37


class TestDrawCards:
    def test_draws_from_top_in_order(self, rules, game, make_card):
        rick = game.players[0]
        rick.library = [make_card(n, type_line="Sorcery")
                        for n in ("Alpha", "Beta", "Gamma")]
        msg = run(rules, game, {"action": "draw_cards", "player": "Rick", "amount": 2})
        assert [c.name for c in rick.hand] == ["Alpha", "Beta"]
        assert [c.name for c in rick.library] == ["Gamma"]
        # Discord output is shared by both players, so private hand contents
        # stay hidden even for the non-Claude/autoplay-human player.
        assert "draws 2 card(s)" in msg
        assert "Alpha" not in msg and "Beta" not in msg

    def test_empty_library_reports_instead_of_crashing(self, rules, game):
        msg = run(rules, game, {"action": "draw_cards", "player": "Rick", "amount": 2})
        assert "cannot draw" in msg


class TestCounters:
    def test_add_counters_single_card(self, rules, game, make_card):
        bear = make_card("Runeclaw Bear")
        game.players[0].battlefield.append(bear)
        run(rules, game, {"action": "add_counters", "card": "Runeclaw Bear",
                          "counter_type": "+1/+1", "amount": 2})
        assert bear.counters.get("+1/+1") == 2

    def test_bulk_counters_hit_same_named_tokens_individually(self, rules, game, make_card):
        # May 13 audit: N name-keyed actions collapsed onto the FIRST token
        # of a same-named batch (the power-2694 mega-Soldier). Bulk mode
        # must apply by identity, one counter to EACH token.
        rick = game.players[0]
        plants = [make_card("Plant", type_line="Token Creature — Plant",
                            power="0", toughness="1") for _ in range(3)]
        rick.battlefield.extend(plants)
        run(rules, game, {"action": "add_counters", "player": "Rick",
                          "target": "all_own_creatures",
                          "counter_type": "+1/+1", "amount": 1})
        assert [p.counters.get("+1/+1", 0) for p in plants] == [1, 1, 1]

    def test_remove_counters_floors_at_zero(self, rules, game, make_card):
        bear = make_card("Runeclaw Bear", counters={"+1/+1": 1})
        game.players[0].battlefield.append(bear)
        run(rules, game, {"action": "remove_counters", "card": "Runeclaw Bear",
                          "counter_type": "+1/+1", "amount": 3})
        assert bear.counters["+1/+1"] == 0


class TestProliferate:
    def test_counters_only_never_life(self, rules, game, make_card):
        # May 30 audit: Tier 3 hallucinated phantom life actions while
        # resolving proliferate; the dedicated action touches counters ONLY —
        # own beneficial counters, opponents' detrimental ones.
        rick, claude = game.players
        mine = make_card("Evolution Sage", counters={"+1/+1": 1})
        theirs = make_card("Opposing Bear", counters={"+1/+1": 1})
        rick.battlefield.append(mine)
        claude.battlefield.append(theirs)
        claude.poison = 2
        run(rules, game, {"action": "proliferate", "player": "Rick"})
        assert mine.counters["+1/+1"] == 2      # own beneficial: +1
        assert theirs.counters["+1/+1"] == 1    # opponent beneficial: untouched
        assert claude.poison == 3               # opponent detrimental: +1
        assert rick.life == 40 and claude.life == 40


class TestDestroy:
    def test_destroy_moves_to_graveyard(self, rules, game, make_card):
        bear = make_card("Runeclaw Bear")
        claude = game.players[1]
        claude.battlefield.append(bear)
        msg = run(rules, game, {"action": "destroy", "card": "Runeclaw Bear"})
        assert bear not in claude.battlefield
        assert bear in claude.graveyard
        assert "destroyed" in msg

    def test_indestructible_survives(self, rules, game, make_card):
        myr = make_card("Darksteel Myr", keywords=["Indestructible"])
        claude = game.players[1]
        claude.battlefield.append(myr)
        msg = run(rules, game, {"action": "destroy", "card": "Darksteel Myr"})
        assert myr in claude.battlefield
        assert not claude.graveyard
        assert "indestructible" in msg.lower()


class TestTapUntap:
    def test_tap_then_untap(self, rules, game, make_card):
        bear = make_card("Runeclaw Bear")
        game.players[0].battlefield.append(bear)
        run(rules, game, {"action": "tap", "card": "Runeclaw Bear"})
        assert bear.tapped
        run(rules, game, {"action": "untap", "card": "Runeclaw Bear"})
        assert not bear.tapped


class TestCreateToken:
    def test_tokens_flagged_and_counted(self, rules, game):
        # Apr 4 audit: tokens lacked is_token and were castable from hand.
        rick = game.players[0]
        run(rules, game, {"action": "create_token", "player": "Rick",
                          "name": "Soldier", "power": 1, "toughness": 1,
                          "types": "Creature — Soldier", "count": 2})
        soldiers = [c for c in rick.battlefield if c.name == "Soldier"]
        assert len(soldiers) == 2
        assert all(getattr(t, "is_token", False) for t in soldiers)
        assert all(t.is_creature() for t in soldiers)


class TestBoardWipeSaveChain:
    """destroy_all_creatures — May 26/30 audit: board wipes previously checked
    ONLY indestructible, so the whole death_replacement mechanic family
    (shield counters, Umbra armor, undying, persist) died permanently to any
    wrath. Save order mirrors mtg/sba.py: shield → totem armor → destroy,
    then undying/persist on the creatures that actually died."""

    WIPE = {"action": "destroy_all_creatures"}

    def test_plain_creature_dies_and_queues_dies_triggers(self, rules, game, make_card):
        bear = make_card("Runeclaw Bear")
        claude = game.players[1]
        claude.battlefield.append(bear)
        run(rules, game, self.WIPE)
        assert bear in claude.graveyard
        assert (bear, claude) in getattr(game, "_recently_died", [])

    def test_indestructible_survives(self, rules, game, make_card):
        myr = make_card("Darksteel Myr", keywords=["Indestructible"])
        claude = game.players[1]
        claude.battlefield.append(myr)
        run(rules, game, self.WIPE)
        assert myr in claude.battlefield

    def test_shield_counter_consumed_instead(self, rules, game, make_card):
        warden = make_card("Sanctuary Warden", counters={"shield": 1})
        rick = game.players[0]
        rick.battlefield.append(warden)
        run(rules, game, self.WIPE)
        assert warden in rick.battlefield
        assert warden.counters["shield"] == 0
        assert (warden, rick) not in getattr(game, "_recently_died", [])

    def test_totem_armor_aura_destroyed_instead(self, rules, game, make_card):
        # May 30 (D1): cards print "Umbra armor"; the engine matched only
        # the old "totem armor" wording — the entire save path was dead.
        rick = game.players[0]
        bear = make_card("Runeclaw Bear")
        umbra = make_card(
            "Bear Umbra", type_line="Enchantment — Aura",
            power=None, toughness=None,
            oracle_text="Enchant creature. Umbra armor (If enchanted creature "
                        "would be destroyed, instead remove all damage from it "
                        "and destroy this Aura.)")
        umbra.attached_to = bear.id
        bear.attachments.append(umbra.id)
        rick.battlefield.extend([bear, umbra])
        run(rules, game, self.WIPE)
        assert bear in rick.battlefield
        assert umbra in rick.graveyard

    def test_undying_returns_with_counter(self, rules, game, make_card):
        messenger = make_card("Geralf's Messenger", keywords=["Undying"])
        rick = game.players[0]
        rick.battlefield.append(messenger)
        run(rules, game, self.WIPE)
        assert messenger in rick.battlefield
        assert messenger.counters.get("+1/+1") == 1
        assert messenger.summoning_sick
        # It DID die — dies triggers still fire (CR 603.6e ordering nuance
        # aside, the death event is real).
        assert (messenger, rick) in game._recently_died

    def test_undying_with_existing_counter_stays_dead(self, rules, game, make_card):
        # Undying only returns the creature if it had NO +1/+1 counters.
        messenger = make_card("Geralf's Messenger", keywords=["Undying"],
                              counters={"+1/+1": 1})
        rick = game.players[0]
        rick.battlefield.append(messenger)
        run(rules, game, self.WIPE)
        assert messenger in rick.graveyard

    def test_persist_returns_with_minus_counter(self, rules, game, make_card):
        finks = make_card("Kitchen Finks", keywords=["Persist"])
        rick = game.players[0]
        rick.battlefield.append(finks)
        run(rules, game, self.WIPE)
        assert finks in rick.battlefield
        assert finks.counters.get("-1/-1") == 1


class TestStackExile:
    """exile_from_stack / release_queller_exile — May 18 audit: the old
    isinstance-dict check never matched StackEntry dataclasses, so EVERY
    Spell Queller silently fizzled. Exiles also went to players[0]
    unconditionally, and the LTB return path had no link to follow."""

    def _put_on_stack(self, game, card, controller_idx=1):
        entry = StackEntry(card=card,
                           controller_name=game.players[controller_idx].name,
                           controller_index=controller_idx)
        game.stack.append(entry)
        return entry

    def test_exiles_legal_spell_to_owner_exile(self, rules, game, make_card):
        cs = make_card("Counterspell", type_line="Instant", cmc=2)
        self._put_on_stack(game, cs)
        msg = run(rules, game, {"action": "exile_from_stack",
                                "controller": "Rick", "max_mv": 4})
        claude = game.players[1]
        assert cs in claude.exile          # owner's exile, not players[0]'s
        assert not game.stack
        assert "exiles" in msg
        # Queller↔exile link recorded for the LTB return path
        assert game._queller_exiles["Spell Queller"] == [(cs, "Claude")]

    def test_respects_mv_cap(self, rules, game, make_card):
        big = make_card("Expropriate", type_line="Sorcery", cmc=9)
        self._put_on_stack(game, big)
        msg = run(rules, game, {"action": "exile_from_stack",
                                "controller": "Rick", "max_mv": 4})
        assert msg is None                 # F2: silent fizzle, no Discord spam
        assert len(game.stack) == 1        # spell stays on the stack

    def test_empty_stack_fizzles_silently(self, rules, game):
        # May 25 (F2): CR 603.3c fizzle — console log only, no Discord line.
        msg = run(rules, game, {"action": "exile_from_stack",
                                "controller": "Rick", "max_mv": 4})
        assert msg is None

    def test_release_returns_card_to_owner_hand(self, rules, game, make_card):
        cs = make_card("Counterspell", type_line="Instant", cmc=2)
        self._put_on_stack(game, cs)
        run(rules, game, {"action": "exile_from_stack",
                          "controller": "Rick", "max_mv": 4})
        msg = run(rules, game, {"action": "release_queller_exile",
                                "source": "Spell Queller"})
        claude = game.players[1]
        assert cs in claude.hand
        assert cs not in claude.exile
        assert "returns" in msg


class TestLivingDeath:
    def test_swaps_zones_and_queues_only_the_sacrificed(self, rules, game, make_card):
        # May 30 (F-LD1): Living Death never queued _recently_died, so every
        # dies-trigger on the sacrifice half was silently dropped (Korvold's
        # death invisible to Bastion in game_1508810609507045508).
        rick, claude = game.players
        korvold = make_card("Korvold, Fae-Cursed King")
        mulldrifter = make_card("Mulldrifter")
        bolt = make_card("Lightning Bolt", type_line="Instant",
                         power=None, toughness=None)
        bear = make_card("Runeclaw Bear")
        rick.battlefield.append(korvold)
        rick.graveyard.extend([mulldrifter, bolt])
        claude.battlefield.append(bear)
        run(rules, game, {"action": "living_death"})
        # Battlefield creatures sacrificed into graveyards...
        assert korvold in rick.graveyard
        assert bear in claude.graveyard
        # ...prior graveyard creatures return (summoning sick); spells stay.
        assert mulldrifter in rick.battlefield
        assert mulldrifter.summoning_sick
        assert bolt in rick.graveyard
        # Dies-trigger queue holds the SACRIFICED creatures only — the
        # returned ones are an "enters" event, not a death.
        died = getattr(game, "_recently_died", [])
        assert (korvold, rick) in died
        assert (bear, claude) in died
        assert all(c is not mulldrifter for c, _ in died)
