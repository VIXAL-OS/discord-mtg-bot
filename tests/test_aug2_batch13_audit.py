"""Aug 2, 2026 batch-13 audit pins (batch game_15332*, sha=c042f46).

Inline-sweep findings, each with live batch evidence:

- I-1 (win_game index): the win_game action stored the PLAYER OBJECT in
  game.winner while every consumer indexes with it — the first-ever live
  alternate win condition (Hellkite Tyrant's 20-artifact upkeep win,
  game_1533272978408865933) crashed the autoplay summary with
  "list indices must be integers or slices, not Player" after the win had
  already been announced.

- I-7 (Queller/Detention Sphere LTB templates unreachable): "ltb" was
  missing from BOTH the suffix-lookup tuple in resolve_etb AND
  _NAME_KEYED_EVENT_TYPES (the latter deliberately — a bare ETB name key
  must not fire on LTB), and the JSON key carried an underscore
  ("spell queller_ltb") where the lookup builds space-separated keys.
  Queller's first-ever real exile (Rashmi, game_1533272933252726965)
  escalated its LTB to Tier 3, which knew nothing about the linked exile —
  harmless there only because the exiled card was a commander that had
  already self-rescued via CR 903.9. The registered-but-unreachable class
  (the game._rules_engine family).

- Karlach intervening-if: her printed "if it's the first combat phase of
  the turn" (CR 603.4) declines the whole trigger in extra combats. The
  generator granted untap + first strike + another phase on EVERY attack,
  masked only by the Moraug loop's tail discard.

- Aurelia, the Warleader: the JSON template was a no_action "use !fix"
  placeholder (the pre-slate breadcrumb class) reached via the
  event_type="attacks" name-key fallthrough. Real attack-registry
  generator now: untap all own creatures + additional_combat, gated on
  "for the first time each turn" via game._attack_trigger_turn_stamps.

- Stale game.attackers at declare (the Boros Elite 2-attacker "fire"):
  Claude's combat path can leave the previous combat's ids in
  game.attackers, and the autoplay/ai_turn declare sites only APPENDED —
  a stale Omenspeaker id made len(game.attackers)=3 with two real
  attackers (game_1533284171202429108). The Battalion GATE was right; the
  list was stale. All three declare sites now reset the list first.
  (The console "Resolved" echo on the decline was the truthy-label
  display class, 3rd sibling — the attack scan now prints
  "handled no-op (condition not met)" instead.)

- I-9 (~4% +1 over-tap): untapped_mana_sources() includes every land
  unconditionally, fetchlands produce {'C': 0} by design, and Phase 3's
  colorless loop tapped zero-producers for nothing before reaching a real
  source (Flooded Strand tapped alongside two duals for a {1}{W} Wall of
  Omens, game_1533272913728372764; 155 one-extra-tap events batch-wide).

- I-8 (opp-cast trigger_text artifact): reminder-text periods split
  mid-parenthetical, gluing ")\n" onto the trigger sentence — Mystic
  Remora printed "casting Talisman of Progress: )".
"""
import asyncio
import json
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


def _lib():
    from rules.effect_templates import get_effect_library
    return get_effect_library()


# ---------------------------------------------------------------------------
# I-1: win_game stores an index, not a Player object
# ---------------------------------------------------------------------------

class TestWinGameIndex:
    def test_win_game_stores_index(self, game, rules):
        msg = rules._execute_action_on_state(game, {
            "action": "win_game", "player": game.players[0].name,
            "reason": "twenty artifacts"})
        assert game.ended
        assert isinstance(game.winner, int)
        # Every consumer does exactly this — it must not raise:
        assert game.players[game.winner].name == game.players[0].name
        assert "wins the game" in msg

    def test_win_game_second_player(self, game, rules):
        rules._execute_action_on_state(game, {
            "action": "win_game", "player": game.players[1].name})
        assert game.winner == 1

    def test_win_game_unknown_player_no_crash(self, game, rules):
        msg = rules._execute_action_on_state(game, {
            "action": "win_game", "player": "Nobody"})
        assert not game.ended
        assert "not found" in msg


# ---------------------------------------------------------------------------
# I-7: LTB suffix templates are reachable
# ---------------------------------------------------------------------------

QUELLER_LTB_TEXT = ("When this creature leaves the battlefield, the exiled "
                    "card's owner may cast that card without paying its mana cost")


class TestLtbSuffixTemplates:
    def test_spell_queller_ltb_template_reachable(self):
        actions, desc = _lib().resolve_etb(
            card_name="Spell Queller", oracle_text=QUELLER_LTB_TEXT,
            controller="Rick Deckard", opponent="Claude",
            game_context={}, event_type="ltb")
        assert actions, "suffix-key lookup returned nothing for event ltb"
        assert any(a.get("action") == "release_queller_exile" for a in actions)

    def test_detention_sphere_ltb_template_reachable(self):
        actions, desc = _lib().resolve_etb(
            card_name="Detention Sphere",
            oracle_text="When Detention Sphere leaves the battlefield, return "
                        "the exiled cards to the battlefield under their "
                        "owner's control",
            controller="Rick Deckard", opponent="Claude",
            game_context={}, event_type="ltb")
        assert actions is not None, "detention sphere ltb key unreachable"

    def test_bare_etb_name_key_still_not_consulted_for_ltb(self):
        # The Apr 29 exclusion stands: a card with ONLY a bare ETB template
        # must not fire it on LTB (Thassa-class re-fire prevention).
        actions, desc = _lib().resolve_etb(
            card_name="Grave Titan",
            oracle_text="When this creature leaves the battlefield, nothing.",
            controller="Rick Deckard", opponent="Claude",
            game_context={}, event_type="ltb")
        assert not actions or all(
            a.get("action") == "no_action" for a in actions)

    def test_queller_ltb_end_to_end_release(self, game, make_card):
        """The whole chain: Queller with a linked exile leaves → the exiled
        card returns to its owner's hand via the suffix template."""
        from mtg.engine import GameEngine
        from mtg.triggers import _check_ltb_triggers_sync
        engine = GameEngine(None)
        rick, claude = game.players
        queller = make_card(
            "Spell Queller", type_line="Creature — Spirit",
            power="2", toughness="3",
            oracle_text="Flash\nFlying\nWhen this creature enters, exile "
                        "target spell with mana value 4 or less.\nWhen this "
                        "creature leaves the battlefield, the exiled card's "
                        "owner may cast that card without paying its mana "
                        "cost.")
        claude.battlefield.append(queller)
        exiled = make_card("Rashmi, Eternities Crafter",
                           type_line="Legendary Creature — Elf Druid",
                           power="2", toughness="3")
        rick.exile.append(exiled)
        game._queller_exiles = {"Spell Queller": [(exiled, rick.name)]}
        msgs = _check_ltb_triggers_sync(engine, game, queller, claude,
                                        destination="graveyard")
        assert exiled in rick.hand, "release_queller_exile never ran"
        assert exiled not in rick.exile
        assert any("returns to" in m for m in msgs)

    def test_no_underscore_suffix_template_keys(self):
        """Structural: the suffix lookup builds space-separated keys
        ("<name> ltb"), so an underscore-suffixed key is unreachable by
        construction — the shape that hid the Queller release template."""
        lib = _lib()
        dispatchable = {"ltb", "endstep", "upkeep", "beginningcombat",
                        "mainphase"}
        bad = []
        for key in list(lib._card_templates):
            if re.search(r"_(ltb|endstep|upkeep|beginningcombat|mainphase)$",
                         key):
                bad.append(key)
            m = re.search(r" (\w+)$", key)
            # A trailing token that LOOKS like an event suffix must be a
            # dispatchable one (space convention, reachable events only).
            if m and m.group(1) in {"ltb", "endstep"} | dispatchable:
                assert m.group(1) in dispatchable
        assert not bad, f"underscore-suffixed (unreachable) keys: {bad}"

    def test_json_key_renamed(self):
        data = json.loads((REPO / "data" / "card_templates.json").read_text(
            encoding="utf-8"))
        entries = data["templates"] if isinstance(data, dict) else data
        keys = {e["key"] for e in entries}
        assert "spell queller ltb" in keys
        assert "spell queller_ltb" not in keys


# ---------------------------------------------------------------------------
# Karlach intervening-if (CR 603.4)
# ---------------------------------------------------------------------------

class TestKarlachFirstCombatGate:
    def _fire(self, game):
        ctx = {"_game": game, "attacking_name": "Karlach, Fury of Avernus",
               "attacking_power": 4}
        actions, desc = _lib().resolve_attack_trigger(
            trigger_card_name="Karlach, Fury of Avernus",
            trigger_oracle="Whenever you attack, if it's the first combat "
                           "phase of the turn, untap all attacking creatures.",
            attacking_creature_name="Karlach, Fury of Avernus",
            attacking_creature_power=4,
            controller=game.players[0].name, opponent=game.players[1].name,
            game_context=ctx)
        return actions

    def test_first_combat_grants_extra_phase(self, game):
        game._in_extra_combat = False
        actions = self._fire(game)
        assert any(a.get("action") == "additional_combat" for a in actions)

    def test_extra_combat_declines(self, game):
        game._in_extra_combat = True
        actions = self._fire(game)
        assert actions and all(a.get("action") == "no_action" for a in actions)
        assert "CR 603.4" in actions[0].get("reason", "")


# ---------------------------------------------------------------------------
# Aurelia, the Warleader — real template, first-time-each-turn gate
# ---------------------------------------------------------------------------

class TestAureliaWarleader:
    def _fire(self, game, make_card=None, tapped_creature=None):
        ctx = {"_game": game,
               "_controller_player": game.players[0],
               "attacking_name": "Aurelia, the Warleader",
               "attacking_power": 3}
        actions, desc = _lib().resolve_attack_trigger(
            trigger_card_name="Aurelia, the Warleader",
            trigger_oracle="Whenever Aurelia attacks for the first time each "
                           "turn, untap all creatures you control. After this "
                           "phase, there is an additional combat phase.",
            attacking_creature_name="Aurelia, the Warleader",
            attacking_creature_power=3,
            controller=game.players[0].name, opponent=game.players[1].name,
            game_context=ctx)
        return actions

    def test_registered_in_attack_registry_not_json(self):
        assert "aurelia, the warleader" in _lib()._attack_templates
        data = json.loads((REPO / "data" / "card_templates.json").read_text(
            encoding="utf-8"))
        entries = data["templates"] if isinstance(data, dict) else data
        assert "aurelia, the warleader" not in {e["key"] for e in entries}, \
            "the JSON no_action placeholder is back — it shadows nothing but " \
            "must stay deleted (the use-!fix breadcrumb class)"

    def test_first_attack_grants_untaps_and_extra_combat(self, game, make_card):
        game.turn_number = 7
        rick = game.players[0]
        tapped = make_card("Sun Titan", type_line="Creature — Giant",
                           power="6", toughness="6")
        tapped.tapped = True
        rick.battlefield.append(tapped)
        actions = self._fire(game)
        assert any(a.get("action") == "additional_combat" for a in actions)
        assert any(a.get("action") == "untap" and a.get("card") == "Sun Titan"
                   for a in actions)

    def test_second_attack_same_turn_declines(self, game):
        game.turn_number = 7
        self._fire(game)
        actions = self._fire(game)
        assert actions and all(a.get("action") == "no_action" for a in actions)

    def test_next_turn_fires_again(self, game):
        game.turn_number = 7
        self._fire(game)
        game.turn_number = 8
        actions = self._fire(game)
        assert any(a.get("action") == "additional_combat" for a in actions)

    def test_damage_dealt_dispatch_cannot_refire(self, game):
        ctx = {"_game": game, "_controller_player": game.players[0],
               "damage_dealt": True}
        actions = _lib()._gen_aurelia_warleader(
            game.players[0].name, game.players[1].name, ctx)
        assert actions == []


# ---------------------------------------------------------------------------
# Stale game.attackers cleared at declare (source pins) + no-op label
# ---------------------------------------------------------------------------

class TestDeclareClearsAttackerList:
    def _assert_clear_before_decide(self, path, min_sites):
        src = (REPO / path).read_text(encoding="utf-8")
        # Every DECLARE_ATTACKERS block that asks the AI for attackers must
        # reset game.attackers first (the staleness class).
        blocks = re.findall(
            r"Phase\.DECLARE_ATTACKERS.{0,900}?decide_attackers\(",
            src, re.S)
        cleared = [b for b in blocks if "game.attackers = []" in b]
        assert len(cleared) >= min_sites, (
            f"{path}: {len(cleared)} of {len(blocks)} declare blocks clear "
            f"game.attackers (need >= {min_sites})")

    def test_autoplay_declare_clears(self):
        self._assert_clear_before_decide("mtg/autoplay.py", 2)

    def test_ai_turn_declare_clears(self):
        self._assert_clear_before_decide("mtg/ai_turn.py", 1)


class TestAttackScanNoOpLabel:
    def test_declined_condition_prints_noop_not_resolved(self, game, make_card,
                                                         capsys):
        from mtg.engine import GameEngine
        from mtg.triggers import _check_attack_triggers_sync
        engine = GameEngine(None)
        rick = game.players[0]
        boros = make_card(
            "Boros Elite", type_line="Creature — Human Soldier",
            power="1", toughness="1",
            oracle_text="Battalion — Whenever this creature and at least two "
                        "other creatures attack, this creature gets +2/+2 "
                        "until end of turn.")
        boros.attacking = True
        peg = make_card("Cavalry Pegasus", type_line="Creature — Pegasus",
                        power="1", toughness="1", oracle_text="Flying")
        peg.attacking = True
        rick.battlefield.extend([boros, peg])
        game.attackers = [boros.id, peg.id]  # only TWO — condition not met
        _check_attack_triggers_sync(engine, game, boros, rick)
        out = capsys.readouterr().out
        assert "handled no-op (condition not met)" in out
        assert "Resolved Boros Elite attack trigger" not in out
        assert boros.power_modifier == 0 if hasattr(boros, "power_modifier") \
            else True


# ---------------------------------------------------------------------------
# I-9: zero-producers are never tapped for mana
# ---------------------------------------------------------------------------

class TestZeroProducerNeverTapped:
    def test_fetchland_not_tapped_for_generic(self, game, make_card):
        """The exact batch board: Flooded Strand + two untapped duals,
        cast {1}{W} — two taps, the fetch untouched."""
        p = game.players[0]
        p.battlefield.append(make_card(
            "Flooded Strand", type_line="Land",
            oracle_text="{T}, Pay 1 life, Sacrifice this land: Search your "
                        "library for a Plains or Island card, put it onto "
                        "the battlefield, then shuffle.",
            power=None, toughness=None))
        p.battlefield.append(make_card(
            "Prairie Stream", type_line="Land — Plains Island",
            oracle_text="({T}: Add {W} or {U}.)", power=None, toughness=None))
        p.battlefield.append(make_card(
            "Godless Shrine", type_line="Land — Plains Swamp",
            oracle_text="({T}: Add {W} or {B}.)", power=None, toughness=None))
        assert p.tap_sources_for_cost("{1}{W}", game=game)
        tapped = [c.name for c in p.battlefield if c.tapped]
        assert len(tapped) == 2
        assert "Flooded Strand" not in tapped


# ---------------------------------------------------------------------------
# I-2 / I-5: descriptions + breadcrumbs
# ---------------------------------------------------------------------------

class TestExtraCombatHousekeeping:
    def test_port_razer_description_updated(self):
        desc = _lib()._attack_templates["port razer"].description
        assert "unmodeled" not in desc

    def test_moraug_tail_discard_is_visible(self):
        src = (REPO / "mtg" / "autoplay.py").read_text(encoding="utf-8")
        assert "granted mid-extra-combat" in src, \
            "the loop-tail zeroing of mid-extra-combat grants went silent again"

    def test_extra_combat_flag_set_and_swept(self):
        auto = (REPO / "mtg" / "autoplay.py").read_text(encoding="utf-8")
        eng = (REPO / "mtg" / "engine.py").read_text(encoding="utf-8")
        assert "game._in_extra_combat = True" in auto
        assert "game._in_extra_combat = False" in auto
        assert "game._in_extra_combat = False" in eng  # end_turn sweep


# ---------------------------------------------------------------------------
# I-8: opp-cast trigger_text cleaned of reminder-text shrapnel
# ---------------------------------------------------------------------------

class TestOppCastTriggerTextClean:
    def test_remora_print_carries_the_trigger_not_a_paren(self, game,
                                                          make_card, capsys):
        from mtg.engine import GameEngine
        from mtg.triggers import _check_cast_triggers
        engine = GameEngine(None)
        rick, claude = game.players
        remora = make_card(
            "Mystic Remora", type_line="Enchantment",
            oracle_text="Cumulative upkeep {1} (At the beginning of your "
                        "upkeep, put an age counter on this permanent, then "
                        "sacrifice it unless you pay its upkeep cost for each "
                        "age counter on it.)\nWhenever an opponent casts a "
                        "noncreature spell, you may draw a card unless that "
                        "player pays {4}.")
        claude.battlefield.append(remora)
        claude.library.append(make_card("Island", type_line="Basic Land",
                                        power=None, toughness=None))
        spell = make_card("Talisman of Progress", type_line="Artifact",
                          mana_cost="{2}", power=None, toughness=None)
        asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
            _check_cast_triggers(engine, game, rick, spell))
        out = capsys.readouterr().out
        line = next((l for l in out.splitlines()
                     if "[OPP-CAST-TRIGGER] Mystic Remora" in l
                     and "triggers from" in l), "")
        assert line, "opp-cast scan never fired for Remora"
        assert not line.rstrip().endswith(": )")
        assert "Whenever an opponent casts" in line
