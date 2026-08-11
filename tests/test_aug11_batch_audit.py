"""Aug 11, 2026 batch audit (sha=a59953c, 160 games) — inline-sweep findings.

Two defects, both found by the systematic sweep rather than by a reviewer:

A-1  The FORETELL-CAST log branch was structurally unreachable. The foretell
     cast path clears the persistent `_foretold` marker (it is consumed by
     the cast) ~20 lines before the tag selection reads it, so the ternary
     always took the IMPULSE-DRAW arm. Live: game_1536521598574530593
     foretold Behold the Multiverse at L541 and cast it from exile at L650
     tagged `[IMPULSE-DRAW]`, with L651 confirming the foretell COST was
     applied. Observability only — but it makes `[FORETELL-CAST]` a
     permanently-zero grep, so an audit cannot tell "foretell never cast"
     from "foretell cast fine", which is exactly what the tag exists for.

A-2  The cascade loop had no CR 104.2a gate. A multi-cascade source can kill
     with its first hit and then keep resolving. Live:
     game_1536521698818531428 — Warstorm Surge killed Rick off cascade 1/4
     at L1681, then cascade 2/4 destroyed Altar of Dementia (L1683), 3/4
     searched a Forest onto the battlefield (L1685), 4/4 resolved Garruk
     (L1687) and Beast Whisperer drew (L1688). A whole-corpus scan found
     this was the ONLY genuine post-loss state mutation in 160 games (the
     two other candidates were the executor re-printing the same lethal
     line). Third sibling of the July-24 combat-damage gate and the Aug-10
     A9 ETB-batch-loop gate, both of which were fixed.
"""
import asyncio
import io
import re
from contextlib import redirect_stdout

import pytest

from mtg.engine import GameEngine
from mtg.triggers import _check_cast_triggers


def _run(coro):
    return asyncio.run(coro)


def _engine(game):
    engine = GameEngine(None)
    game._rules_engine = engine.rules
    engine.rules.engine_ref = engine
    return engine


def _lands(make_card, player, n, name="Mountain", sym="R"):
    for _ in range(n):
        player.battlefield.append(make_card(
            name, type_line=f"Basic Land — {name}",
            oracle_text="{T}: Add {%s}." % sym))


QUAKEBRINGER = (
    "Whenever Quakebringer or another creature you control with power 4 or "
    "greater attacks, it deals 1 damage to each opponent.\n"
    "Foretell {2}{R}{R}")


# --------------------------------------------------------------------------
# A-1: the FORETELL-CAST tag
# --------------------------------------------------------------------------

class TestForetellCastTag:
    """The tag must name the mechanic that actually paid for the cast.

    DECISIVE SHAPE: both pins drive the REAL executor
    (`GameEngine._execute_action`) and read the REAL stdout, because the bug
    is a read-after-clear inside that function. A pin that set `_foretold`
    itself and asserted on a helper would pass with the bug present — the
    whole defect is that production clears the flag before reaching the log.
    """

    def _foretold_in_exile(self, make_game, make_card):
        from mtg.spells import foretell_card_from_hand
        game = make_game()
        rick, _ = game.players
        game.active_player_index = 0
        game.turn_number = 3
        qb = make_card("Quakebringer", type_line="Creature — Giant Berserker",
                       oracle_text=QUAKEBRINGER, mana_cost="{3}{R}{R}",
                       power="4", toughness="4")
        rick.hand.append(qb)
        _lands(make_card, rick, 8)
        engine = _engine(game)
        ok, msg = foretell_card_from_hand(game, rick, qb)
        assert ok, msg
        assert qb._foretold, "fixture precondition: the card is foretold"
        for land in rick.battlefield:
            land.tapped = False
        game.turn_number = 4          # CR 702.143b: not the turn foretold
        return game, rick, qb, engine

    def test_a_foretold_cast_logs_foretell_cast_not_impulse(
            self, make_game, make_card):
        game, rick, qb, engine = self._foretold_in_exile(make_game, make_card)
        buf = io.StringIO()
        with redirect_stdout(buf):
            result = _run(engine._execute_action(
                game, 0, {"type": "cast", "card": "Quakebringer"}))
        out = buf.getvalue()
        assert result, "the foretold cast should succeed"
        assert "[FORETELL-CAST]" in out, (
            "a foretold cast must be tagged FORETELL-CAST. The tag selection "
            "reads a flag the foretell branch clears earlier in the same "
            "function, so this branch was unreachable.\n" + out[-800:])
        assert "[IMPULSE-DRAW]" not in out, (
            "foretell is not impulse (CR 702.143 vs Light Up the Stage)")

    def test_a_plain_impulse_cast_still_logs_impulse_draw(
            self, make_game, make_card):
        """NEGATIVE CONTROL. Without this, the fix could hardcode
        FORETELL-CAST and both the pin above and the source pass."""
        game = make_game()
        rick, _ = game.players
        game.active_player_index = 0
        game.turn_number = 4
        bolt = make_card("Lightning Bolt", type_line="Instant",
                         oracle_text="Lightning Bolt deals 3 damage to any target.",
                         mana_cost="{R}", power=None, toughness=None)
        # Impulse: exiled and marked playable, NOT foretold.
        rick.exile.append(bolt)
        rick.playable_from_exile.append(bolt.id)
        _lands(make_card, rick, 4)
        engine = _engine(game)
        buf = io.StringIO()
        with redirect_stdout(buf):
            _run(engine._execute_action(
                game, 0, {"type": "cast", "card": "Lightning Bolt"}))
        out = buf.getvalue()
        assert "[IMPULSE-DRAW]" in out, (
            "a non-foretold exile cast is still impulse\n" + out[-600:])
        assert "[FORETELL-CAST]" not in out


# --------------------------------------------------------------------------
# A-2: CR 104.2a in the cascade loop
# --------------------------------------------------------------------------

APEX = ("When you cast this spell, exile cards from the top of your library "
        "until you exile a nonland card that costs less. You may cast it "
        "without paying its mana cost.\nCascade, cascade, cascade, cascade")


class TestCascadeStopsWhenTheGameEnds:
    """CR 104.2a — a two-player game ends the instant a player loses.

    DECISIVE SHAPE: the assertion is on LIBRARY CONSUMPTION, not on the
    message list. Cascade exiles from the top of the library on every
    iteration, so an ungated loop is visible in the zone state even if the
    messages were suppressed for some other reason.
    """

    def _apex_game(self, make_game, make_card, library_size=8):
        game = make_game()
        rick, claude = game.players
        game.active_player_index = 0
        game.turn_number = 6
        apex = make_card("Apex Devastator",
                         type_line="Creature — Zombie Horror",
                         oracle_text=APEX, mana_cost="{6}{B}{G}{U}{R}",
                         power="10", toughness="10", cmc=10)
        for i in range(library_size):
            rick.library.append(make_card(
                f"Grizzly Bears {i}", type_line="Creature — Bear",
                mana_cost="{1}{G}", cmc=2, power="2", toughness="2"))
        engine = _engine(game)
        return game, rick, apex, engine

    def test_no_cascade_resolves_after_a_player_has_lost(
            self, make_game, make_card):
        game, rick, apex, engine = self._apex_game(make_game, make_card)
        before = len(rick.library)
        game.ended = True                      # the lethal already happened
        buf = io.StringIO()
        with redirect_stdout(buf):
            _run(_check_cast_triggers(engine, game, rick, apex))
        assert len(rick.library) == before, (
            "cascade exiles from the library every iteration; an ungated loop "
            "keeps consuming it after the game is over (CR 104.2a). "
            f"library {before} -> {len(rick.library)}")
        assert "CR 104.2a" in buf.getvalue(), (
            "the stop should be greppable, not silent")

    def test_cascade_still_resolves_normally_when_the_game_is_live(
            self, make_game, make_card):
        """NEGATIVE CONTROL — the gate must not blank a legal cascade.
        Without this a mutant that unconditionally `break`s passes the pin
        above."""
        game, rick, apex, engine = self._apex_game(make_game, make_card)
        before = len(rick.library)
        assert not getattr(game, "ended", False)
        _run(_check_cast_triggers(engine, game, rick, apex))
        assert len(rick.library) < before, (
            "a live game must still cascade — the gate is conditional")

    def test_the_gate_reads_game_ended_at_every_iteration(
            self, make_game, make_card):
        """A player can die to cascade N and the loop must stop before N+1.
        Checking only ONCE before the loop would miss exactly the observed
        case (game_1536521698818531428: lethal landed on cascade 1/4)."""
        game, rick, apex, engine = self._apex_game(make_game, make_card)

        class _KillsOnFirstPop(list):
            """Flips game.ended the moment cascade 1 touches the library —
            i.e. the lethal lands DURING the first iteration, exactly as in
            game_1536521698818531428."""
            def pop(self, idx=-1):
                card = super().pop(idx)
                game.ended = True
                return card

        rick.library = _KillsOnFirstPop(rick.library)
        _run(_check_cast_triggers(engine, game, rick, apex))
        # One cascade's worth of exiling at most: the first iteration was
        # already in flight, iterations 2..4 must not start.
        consumed = 8 - len(rick.library)
        assert consumed <= 2, (
            "the loop must re-check `ended` each iteration, not once before "
            f"it; {consumed} cards were exiled after the game ended")


# --------------------------------------------------------------------------
# A-3: the equipment combat-damage template tail
# --------------------------------------------------------------------------

class TestEquipmentCombatDamageTemplates:
    """The Aug-10 A4 fix made this class VISIBLE; this batch produced the
    ranked backlog it exists to produce (Mask of Memory x5 connecting and
    resolving nothing, plus three Swords). Tier 3 refuses them by design via
    the combat-shape guard, so a template is the prescribed answer.

    EXECUTED, not inspected: every assertion is on real GameState after
    running the emitted actions through the real interpreter. A template
    that emits plausible-looking dicts nothing consumes is this repo's
    documented silent-no-op class, and only execution catches it.
    """

    def _apply(self, rules, game, actions):
        out = []
        for a in actions:
            if a.get("action") == "no_action":
                continue
            out.append(rules._execute_action_on_state(game, a))
        return out

    def _trigger(self, lib, name, ctx):
        actions, _ = lib.resolve_attack_trigger(
            trigger_card_name=name,
            trigger_oracle=("Whenever equipped creature deals combat damage "
                            "to a player, do the thing."),
            attacking_creature_name="Bear", attacking_creature_power=3,
            controller="Rick", opponent="Claude", game_context=ctx)
        return actions

    def test_mask_of_memory_draws_two_and_discards_one(
            self, lib, rules, game, make_card):
        rick, _ = game.players
        for i in range(5):
            rick.library.append(make_card(f"Lib {i}"))
        rick.hand.append(make_card("Held"))
        before_hand, before_lib = len(rick.hand), len(rick.library)
        acts = self._trigger(lib, "Mask of Memory", {"damage_dealt": 3})
        self._apply(rules, game, acts)
        assert len(rick.library) == before_lib - 2, "drew two"
        assert len(rick.hand) == before_hand + 1, (
            "net +1: drew two, discarded one")
        assert len(rick.graveyard) == 1, "the discard reached the graveyard"

    def test_sword_of_fire_and_ice_burns_and_draws(
            self, lib, rules, game, make_card):
        rick, claude = game.players
        rick.library.append(make_card("Lib 0"))
        before_life, before_hand = claude.life, len(rick.hand)
        acts = self._trigger(lib, "Sword of Fire and Ice", {"damage_dealt": 3})
        self._apply(rules, game, acts)
        assert claude.life == before_life - 2, "2 damage to any target"
        assert len(rick.hand) == before_hand + 1, "and you draw a card"

    def test_sword_of_feast_and_famine_discards_and_untaps_all_lands(
            self, lib, rules, game, make_card):
        rick, claude = game.players
        for i in range(4):
            land = make_card(f"Forest {i}", type_line="Basic Land — Forest")
            land.tapped = True
            rick.battlefield.append(land)
        claude.hand.append(make_card("Their Card"))
        ctx = {"damage_dealt": 3,
               "controller_battlefield": list(rick.battlefield)}
        acts = self._trigger(lib, "Sword of Feast and Famine", ctx)
        self._apply(rules, game, acts)
        assert len(claude.hand) == 0, "the damaged player discards"
        assert all(not c.tapped for c in rick.battlefield), (
            "ALL lands untap — the count is derived from the board, so a "
            "hardcoded default would under-untap a big battlefield")

    def test_sword_of_light_and_shadow_gains_and_returns(
            self, lib, rules, game, make_card):
        rick, _ = game.players
        big = make_card("Big Guy", type_line="Creature — Giant", cmc=6)
        rick.graveyard.append(big)
        rick.graveyard.append(make_card("Bolt", type_line="Instant", cmc=1))
        before = rick.life
        ctx = {"damage_dealt": 3,
               "controller_graveyard": list(rick.graveyard)}
        acts = self._trigger(lib, "Sword of Light and Shadow", ctx)
        self._apply(rules, game, acts)
        assert rick.life == before + 3, "gain 3 life"
        assert any(c.name == "Big Guy" for c in rick.hand), (
            "returns a CREATURE card (not the Instant) to hand")
        assert any(c.name == "Bolt" for c in rick.graveyard), (
            "the noncreature stays put")

    def test_sword_of_light_and_shadow_still_gains_with_empty_graveyard(
            self, lib, rules, game):
        """'up to one target' — an empty graveyard is a legal no-op and must
        not swallow the life gain (CR 601.2c does not gate optional targets)."""
        rick, _ = game.players
        before = rick.life
        acts = self._trigger(lib, "Sword of Light and Shadow",
                             {"damage_dealt": 3, "controller_graveyard": []})
        self._apply(rules, game, acts)
        assert rick.life == before + 3

    def test_sword_of_sinew_and_steel_destroys_opponent_permanents_only(
            self, lib, rules, game, make_card):
        rick, claude = game.players
        jace = make_card("Jace", type_line="Legendary Planeswalker — Jace",
                         cmc=4, power=None, toughness=None)
        ring = make_card("Sol Ring", type_line="Artifact", cmc=1,
                         power=None, toughness=None)
        claude.battlefield += [jace, ring]
        mine = make_card("My Ring", type_line="Artifact", cmc=1,
                         power=None, toughness=None)
        rick.battlefield.append(mine)
        ctx = {"damage_dealt": 3,
               "opponent_battlefield": list(claude.battlefield)}
        acts = self._trigger(lib, "Sword of Sinew and Steel", ctx)
        self._apply(rules, game, acts)
        names = {c.name for c in claude.battlefield}
        assert "Jace" not in names and "Sol Ring" not in names, (
            "both halves destroy")
        assert any(c.name == "My Ring" for c in rick.battlefield), (
            "never the controller's own permanents")

    @pytest.mark.parametrize("card", [
        "Mask of Memory", "Sword of Fire and Ice", "Sword of Feast and Famine",
        "Sword of Light and Shadow", "Sword of Sinew and Steel"])
    def test_none_fire_without_combat_damage_gate(self, lib, card):
        """The declare-time attack scan reaches these same templates; the
        Aug-1 F2 convention is that every combat-damage generator gates on
        ctx['damage_dealt'] so it cannot misfire there."""
        acts = self._trigger(lib, card, {"damage_dealt": 0})
        assert not [a for a in (acts or [])
                    if a.get("action") != "no_action"], (
            f"{card} fired with no combat damage dealt")


# --------------------------------------------------------------------------
# A-4 (reviewer C): CR 510.4 — trigger processing after EACH damage step
# --------------------------------------------------------------------------

class TestCombatDamageTriggersDrainPerStep:
    """Drana, Liberator of Malakir has first strike and "whenever Drana deals
    combat damage to a player, put a +1/+1 counter on each attacking creature
    you control". Her entire design is: connect in the FS step, pump the team,
    THEN the regular step swings bigger.

    The drain used to run once, after both steps, off the accumulated
    `_combat_damage_to_player` list — so the counters landed after the regular
    attackers had already dealt their un-pumped base power. Live:
    game_1536545838891794432, reproduced across two separate combats (Phyrexian
    Negator dealt base 5 and Crypt Ghast base 2 in the same combat whose FS
    step had already triggered Drana).

    STRUCTURAL + BEHAVIOURAL: the structural half pins that a call exists in
    the first-strike block (a per-step drain cannot be expressed as a single
    end-of-combat call), the behavioural half pins that calling it twice does
    not double-fire.
    """

    def test_the_drain_is_called_from_the_first_strike_block(self):
        import inspect
        from mtg import combat as combat_mod
        src = inspect.getsource(combat_mod.resolve_combat_damage)
        head = src.split("# Regular damage step")[0]
        assert "drain_combat_damage_triggers(" in head, (
            "CR 510.4: combat-damage triggers must resolve after the FIRST "
            "strike step too, before the regular step computes damage. "
            "Without a call in the FS block a first-striker's own trigger "
            "lands after the regular attackers have already dealt damage.")
        assert src.count("drain_combat_damage_triggers(") >= 2, (
            "one call per damage step")

    def test_draining_twice_does_not_double_fire(self, rules, game, make_card):
        """The safety property that makes the second call legal: the drain
        clears both accumulators, so the post-regular call sees only what the
        regular step appended."""
        from mtg.combat import drain_combat_damage_triggers
        rick, claude = game.players
        watcher = make_card(
            "Ohran Frostfang", type_line="Creature — Snake",
            oracle_text=("Deathtouch\nWhenever a creature you control deals "
                         "combat damage to a player, draw a card."))
        rick.battlefield.append(watcher)
        for i in range(6):
            rick.library.append(make_card(f"Lib {i}"))
        dealer = make_card("Bear")
        rick.battlefield.append(dealer)
        game._combat_damage_to_player = [(dealer, rick, 2)]
        msgs = []
        drain_combat_damage_triggers(rules, game, msgs)
        after_first = len(rick.hand)
        assert after_first == 1, "the watcher drew once for the FS entry"
        assert game._combat_damage_to_player == [], (
            "the accumulator is cleared, which is what makes a second call safe")
        drain_combat_damage_triggers(rules, game, msgs)
        assert len(rick.hand) == after_first, (
            "a second drain with nothing newly accumulated must draw NOTHING "
            "— otherwise the per-step call would double-fire every trigger")
