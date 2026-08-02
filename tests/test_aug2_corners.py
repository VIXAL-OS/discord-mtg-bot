"""Aug 2, 2026 — the corners-of-corners pass (crew, multikicker, entwine
mode choice, hint-scoping, AI-choice nudges, prose-says-pass).

- CREW (CR 702.121): Vehicles became real non-creatures at the batch-13
  PW-token fix; crew is what makes them usable. One shared core
  (spells.crew_vehicle — the suspend pattern), branches in BOTH executors,
  a 🚗 prompt hint, and Chandra, Spark Hunter's beginning-of-combat animate
  template (the other half of her card). Animation rides the established
  _animated machinery (printed P/T via use_printed_pt, EOT revert free).
- MULTIKICKER (CR 702.33c): REGISTRY-gated auto-kick (only cards whose
  kicked mode a template actually consumes — Everflowing Chalice v1;
  auto-kicking Comet Storm would overpay for extra targets nothing
  models). The Chalice's production side has read charge counters since
  forever — the payment→counters half never existed, so it entered dead.
- Entwine mode choice: un-entwined Tooth and Nail takes battlefield mode
  only for a real bomb (power>=4 or MV>=5) — a hand of mana dorks was
  eating the put-onto-battlefield mode.
- Hint-scoping (July-29 carry): "no legal targets — opponent has 0
  creatures" was a CR-false claim whenever the caster had a board; the
  label is honest now, the deterrent stays.
- prose-says-pass: prose_hold_veto — a cast whose card the model's own
  prose says to HOLD is vetoed to pass (decide_action) / dropped
  (plan_turn). Marker must PRECEDE the name within a sentence fragment so
  "cast Bolt and hold Counterspell" never vetoes the Bolt.

Also cleaned en route: the DUPLICATE _gen_life_from_the_loam (an April
2026 template reading a ctx key no builder populates — why the batch-13
reviewer saw Tier-3 escalations — silently shadowed by the batch-13
replacement added without the grep-for-existing-def step). The structural
pin here makes the whole duplicate-def class unshippable.
"""
import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


def _lib():
    from rules.effect_templates import get_effect_library
    return get_effect_library()


CHALICE_ORACLE = ("Multikicker {2} (You may pay an additional {2} any number "
                  "of times as you cast this spell.)\nThis artifact enters "
                  "with a charge counter on it for each time it was kicked.\n"
                  "{T}: Add {C} for each charge counter on this artifact.")

VEHICLE_ORACLE = ("Crew 2 (Tap any number of creatures you control with "
                  "total power 2 or more: This Vehicle becomes an artifact "
                  "creature until end of turn.)")


class TestParsers:
    def test_parse_crew(self):
        from mtg.helpers import parse_crew
        assert parse_crew(VEHICLE_ORACLE) == 2
        assert parse_crew("Crew 1") == 1
        assert parse_crew("Flying") is None

    def test_parse_multikicker(self):
        from mtg.helpers import parse_multikicker, parse_kicker
        assert parse_multikicker(CHALICE_ORACLE) == "{2}"
        assert parse_multikicker("Kicker {1}{W}") is None
        # And the single-kicker parser still excludes multikicker:
        assert parse_kicker(CHALICE_ORACLE) is None


class TestCrew:
    def _board(self, game, make_card):
        rick = game.players[0]
        veh = make_card("Smuggler's Copter", type_line="Artifact — Vehicle",
                        power="3", toughness="3", oracle_text=VEHICLE_ORACLE)
        big = make_card("Bear", type_line="Creature — Bear",
                        power="3", toughness="3")
        small = make_card("Llanowar Elves", type_line="Creature — Elf Druid",
                          power="1", toughness="1")
        big.summoning_sick = False
        small.summoning_sick = True  # sick creatures CAN crew (CR 702.121c)
        rick.battlefield.extend([veh, big, small])
        return rick, veh, big, small

    def test_crew_taps_greedily_and_animates(self, game, make_card):
        from mtg.spells import crew_vehicle
        rick, veh, big, small = self._board(game, make_card)
        ok, msg = crew_vehicle(game, rick, "Smuggler's Copter")
        assert ok, msg
        assert big.tapped and not small.tapped, (
            "greedy largest-first: the 3-power alone covers crew 2")
        assert veh.is_creature(game=game), "crewed Vehicle is a creature"
        assert veh.get_effective_power(game) == 3, "printed P/T"
        assert veh._animated_until_eot

    def test_crew_insufficient_power_refuses(self, game, make_card):
        from mtg.spells import crew_vehicle
        rick = game.players[0]
        veh = make_card("Big Rig", type_line="Artifact — Vehicle",
                        power="6", toughness="6", oracle_text="Crew 5")
        rick.battlefield.extend([veh, make_card(
            "Llanowar Elves", type_line="Creature — Elf", power="1",
            toughness="1")])
        ok, msg = crew_vehicle(game, rick, "Big Rig")
        assert not ok and "need total power 5" in msg
        assert not veh.is_creature(game=game)

    def test_both_executors_dispatch_crew(self):
        for rel in ("mtg/autoplay.py", "mtg/engine.py"):
            src = (REPO / rel).read_text(encoding="utf-8")
            assert 'action_type == "crew"' in src, (
                f"{rel}: the crew branch is missing — the two-activation-"
                f"paths divergence class")

    def test_crew_hint_and_grammar_advertised(self):
        src = (REPO / "mtg" / "claude_player.py").read_text(encoding="utf-8")
        assert "🚗 CREW available" in src
        assert '{"type": "crew", "vehicle"' in src

    def test_chandra_combat_template(self, game, make_card):
        veh = make_card("Vehicle Artifact", type_line="Token Artifact — Vehicle",
                        power="3", toughness="2", oracle_text="Crew 1")
        ctx = {"_controller_player": type("P", (), {"battlefield": [veh]})()}
        actions, _ = _lib().resolve_etb(
            card_name="Chandra, Spark Hunter",
            oracle_text="At the beginning of combat on your turn, choose up "
                        "to one target Vehicle you control. Until end of "
                        "turn, it becomes an artifact creature and gains "
                        "haste.",
            controller="Claude", opponent="Rick",
            game_context=ctx, event_type="beginning_combat")
        assert actions and actions[0]["action"] == "animate_permanent"
        assert actions[0]["use_printed_pt"] is True
        # No vehicle → up-to-one resolves none chosen
        ctx2 = {"_controller_player": type("P", (), {"battlefield": []})()}
        actions2, _ = _lib().resolve_etb(
            card_name="Chandra, Spark Hunter",
            oracle_text="At the beginning of combat on your turn, choose up "
                        "to one target Vehicle you control...",
            controller="Claude", opponent="Rick",
            game_context=ctx2, event_type="beginning_combat")
        assert actions2 and actions2[0]["action"] == "no_action"

    def test_animate_use_printed_pt(self, game, rules, make_card):
        rick = game.players[0]
        veh = make_card("Vehicle Artifact", type_line="Token Artifact — Vehicle",
                        power="3", toughness="2", oracle_text="Crew 1")
        rick.battlefield.append(veh)
        rules._execute_action_on_state(game, {
            "action": "animate_permanent", "player": rick.name,
            "scope": "target", "card": "Vehicle Artifact",
            "required_type": "artifact", "use_printed_pt": True,
            "keywords": "haste"})
        assert veh.is_creature(game=game)
        assert veh.get_effective_power(game) == 3
        assert veh.get_effective_toughness(game) == 2


class TestMultikicker:
    def _chalice(self, make_card):
        return make_card("Everflowing Chalice", type_line="Artifact",
                         mana_cost="{0}", cmc=0, oracle_text=CHALICE_ORACLE,
                         power=None, toughness=None)

    def _lands(self, player, make_card, n):
        for _ in range(n):
            player.battlefield.append(make_card(
                "Wastes", type_line="Basic Land",
                oracle_text="({T}: Add {C}.)", power=None, toughness=None))

    def test_chalice_kicks_to_budget(self, game, make_card):
        from mtg.engine import GameEngine
        from mtg.spells import _compute_alt_costs
        rick = game.players[0]
        self._lands(rick, make_card, 6)
        chal = self._chalice(make_card)
        rick.hand.append(chal)
        early, costs = _compute_alt_costs(GameEngine(None), game, rick, chal,
                                          pay_mana=True, additional_cost=0)
        assert early is None
        assert chal._kicked_times == 3, "6 one-tap / {2} each = kicked 3x"

    def test_unregistered_multikicker_never_auto_kicks(self, game, make_card):
        """Decisive for the REGISTRY gate specifically: a plain-cost
        multikicker card (no X — an X string made the first fixture pass
        for an incidental unpayability reason, the batch-12 gate-divergence
        lesson) that WOULD kick if the gate were oracle-based."""
        from mtg.engine import GameEngine
        from mtg.spells import _compute_alt_costs
        rick = game.players[0]
        self._lands(rick, make_card, 8)
        wolf = make_card(
            "Wolfbriar Elemental", type_line="Creature — Elemental",
            mana_cost="{2}{2}", cmc=4,
            oracle_text="Multikicker {2}\nWhen this creature enters, create "
                        "a 2/2 green Wolf creature token for each time it "
                        "was kicked.",
            power="4", toughness="4")
        rick.hand.append(wolf)
        _compute_alt_costs(GameEngine(None), game, rick, wolf,
                           pay_mana=True, additional_cost=0)
        assert wolf._kicked_times == 0, (
            "auto-kicking a card whose kicked mode nothing consumes "
            "overpays for nothing — the registry gate")

    def test_chalice_template_reads_kicked_times(self):
        actions = _lib()._gen_everflowing_chalice("Rick", "Claude",
                                                  {"kicked_times": 2})
        assert actions == [{"action": "add_counters",
                            "card": "Everflowing Chalice",
                            "counter_type": "charge", "amount": 2}]
        actions0 = _lib()._gen_everflowing_chalice("Rick", "Claude", {})
        assert actions0 and actions0[0]["action"] == "no_action"

    def test_chalice_production_reads_counters(self, game, make_card):
        rick = game.players[0]
        chal = self._chalice(make_card)
        chal.counters["charge"] = 2
        rick.battlefield.append(chal)
        assert rick._get_mana_production(chal).get("C") == 2


class TestSmallFour:
    def test_tooth_unentwined_dorks_do_not_eat_battlefield_mode(self, make_card):
        dork = make_card("Llanowar Elves", type_line="Creature — Elf Druid",
                         power="1", toughness="1", cmc=1)
        actions = _lib()._gen_tooth_and_nail(
            "Rick", "Claude", {"entwined": False, "controller_hand": [dork]})
        assert all(a.get("to_zone") != "battlefield" for a in actions), (
            "a mana dork is not a bomb — search-to-hand is the better mode")
        hoof = make_card("Craterhoof Behemoth", type_line="Creature — Beast",
                         power="5", toughness="5", cmc=8)
        actions2 = _lib()._gen_tooth_and_nail(
            "Rick", "Claude", {"entwined": False,
                               "controller_hand": [dork, hoof]})
        assert actions2 == [{"action": "move_card",
                             "card": "Craterhoof Behemoth",
                             "from_zone": "hand", "to_zone": "battlefield",
                             "player": "Rick"}]

    def test_removal_hint_honest_about_own_targets(self, game, make_card):
        from mtg.claude_player import _card_legality_note
        rick, claude = game.players
        push = make_card("Fatal Push", type_line="Instant", mana_cost="{B}",
                         oracle_text="Destroy target creature if it has mana "
                                     "value 2 or less.",
                         power=None, toughness=None)
        # Opponent (claude) has 0 creatures; the CASTER (rick) has one.
        rick.battlefield.append(make_card(
            "Dragon's Rage Channeler", type_line="Creature — Human Shaman",
            power="1", toughness="1"))
        note = _card_legality_note(push, game, claude)
        assert "no legal targets" not in note, (
            "own creatures ARE legal targets (CR) — the note was a false "
            "claim")
        assert "own creatures" in note
        # Neither side has creatures → the true negative stands.
        rick.battlefield.clear()
        note2 = _card_legality_note(push, game, claude)
        assert "no legal targets" in note2

    def test_prose_hold_veto_semantics(self):
        from mtg.claude_player import prose_hold_veto
        assert prose_hold_veto(
            "I should hold Teferi's Protection for their attack.",
            {"type": "cast", "card": "Teferi's Protection"})
        assert not prose_hold_veto(
            "Cast Lightning Bolt now and hold Counterspell for their turn.",
            {"type": "cast", "card": "Lightning Bolt"}), (
            "the marker must precede THE SAME card — never veto the Bolt")
        assert prose_hold_veto(
            "Cast Lightning Bolt now and hold Counterspell for their turn.",
            {"type": "cast", "card": "Counterspell"})
        assert not prose_hold_veto(
            "Casting Lightning Bolt at the Goblin.",
            {"type": "cast", "card": "Lightning Bolt"})
        assert not prose_hold_veto("hold everything",
                                   {"type": "pass"})
        # SENTENCE-boundary decisiveness: a hold about another card in an
        # EARLIER sentence must not bleed into a later cast (the window
        # stops at .!? and newline — a whole-text window wrongly vetoes).
        assert not prose_hold_veto(
            "Hold Counterspell for their turn. Lightning Bolt the Goblin "
            "Guide now.",
            {"type": "cast", "card": "Lightning Bolt"})

    def test_veto_wired_into_both_paths(self):
        src = (REPO / "mtg" / "claude_player.py").read_text(encoding="utf-8")
        assert src.count("prose_hold_veto(raw_text,") >= 2, (
            "decide_action AND plan_turn must both consult the veto")

    def test_engines_nudge_present(self):
        src = (REPO / "mtg" / "claude_player.py").read_text(encoding="utf-8")
        assert "CARD-ADVANTAGE ENGINES:" in src


class TestNoDuplicateGenerators:
    def test_no_shadowing_generator_defs(self):
        """Structural: a second `def _gen_X` silently SHADOWS the first
        (later def wins) — the class that hid Ancient Bronze Dragon's
        hallucinated template (July 30) and the April Life from the Loam
        (found Aug 2: the batch-13 'fix' was a shadowing duplicate of a
        dead-ctx original). Python allows it; this pin doesn't."""
        import ast
        import collections
        tree = ast.parse((REPO / "rules" / "effect_templates.py").read_text(
            encoding="utf-8"))
        names = collections.Counter()
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name.startswith("_gen_"):
                names[node.name] += 1
        dupes = {k: v for k, v in names.items() if v > 1}
        assert not dupes, f"shadowing duplicate generators: {dupes}"
