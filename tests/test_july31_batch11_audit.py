"""July 31, 2026 batch-11 audit pins (batch game_15325*, sha=0e3fb20).

Inline-sweep findings, all with live batch evidence:

- S1: the AI activation path (mtg/engine.py ~4292) passes
  "<player> activated <source>'s ability: <text>" to resolve_effect, and
  every ^-anchored deterministic guard (self-pump, fog, bounce-attackers)
  silently never matched on that path — Spore Frog's fog was refused AFTER
  its sacrifice cost was paid (×2, the exact batch-10 bug the fog guard was
  built for), and [RESOLVE-SELF-PUMP] has never fired live. The batch-10
  pins tested the BARE text, which the live path never sends (the
  pin-shape-reachability trap, again). resolve_effect now strips the
  prefix once for guard matching.

- S2: X auto-sizing budgeted from available_mana(), which double-counts
  OR-duals (Sacred Foundry = 2) — Volcanic Geyser sized X=6 (total 8) on a
  7-physical-source board, the tap engine correctly refused, and the
  batch's only Geyser cast was lost (game_1532536791742025739). Sizing now
  uses the new Player.one_tap_mana_total() (also deduplicates the can_pay
  gate's inline computation).

- S3: game._last_cast_failure recorded the PARENT card's name for
  adventure-half casts ("Beanstalk Giant") while the retry path compares
  the action's name ("Fertile Footsteps") — the real failure reason was
  dropped and "unknown reason — mana looks sufficient" resurfaced ×2.
  Producers now record the cast-as name.

- S4: the disk cache stores the CREATURE face's type_line (no
  " // Sorcery — Adventure" half), so Card._parse_cmc's adventure gate
  (batch-10 fix) never fires on cache-loaded cards — both halves were
  summed and Flaxen Intruder priced at CMC 8 in plan-validate.
  _extract_adventure_data now recomputes cmc from the creature face
  (CR 715.2b), and _validate_plan_mana prices/simulates only the half
  being cast (both names resolve).

- S5: templates for the batch's refused-trigger tail — Port Razer (combat
  damage: untap own creatures; extra combat phase unmodeled, breadcrumb)
  and Frenzied Trapbreaker (attacks: destroy defending player's best
  artifact/enchantment; fizzles per CR 603.3c when none).

- S6 (AI-quality): Claude cast Aetherize in its own main phase with zero
  attackers (guaranteed-dead cast). _validate_plan_mana now holds
  "return all attacking creatures" casts with a recorded rejection.
"""
import asyncio
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


def _lib():
    from rules.effect_templates import get_effect_library
    return get_effect_library()


# ---------------------------------------------------------------------------
# S1: activation-prefix strip for the deterministic judge guards
# ---------------------------------------------------------------------------

class TestGuardPrefixStrip:
    PREFIXED_FOG = ("Rick activated Spore Frog's ability: Prevent all combat "
                    "damage that would be dealt this turn.")

    def test_fog_fires_through_activation_prefix(self, rules, game):
        # The REACHABLE live shape (batch 15325 refused it twice, cost paid).
        msgs, actions = asyncio.run(rules.resolve_effect(
            game, self.PREFIXED_FOG,
            source_card="Spore Frog", controller="Rick"))
        assert actions == [{"action": "prevent_combat_damage", "scope": "all"}]

    def test_self_pump_fires_through_activation_prefix(self, rules, game):
        # [RESOLVE-SELF-PUMP] had never fired live — same prefix, same anchor.
        msgs, actions = asyncio.run(rules.resolve_effect(
            game, ("Claude activated Inferno Titan's ability: This creature "
                   "gets +1/+0 until end of turn."),
            source_card="Inferno Titan", controller="Claude"))
        assert actions == [{"action": "pump_all_creatures", "player": "Claude",
                            "card": "Inferno Titan", "power": 1,
                            "toughness": 0}]

    def test_prefix_strip_does_not_invent_matches(self, rules, game):
        # A prefixed NON-guard effect must not become a fog/pump/bounce.
        msgs, actions = asyncio.run(rules.resolve_effect(
            game, "Rick activated Arch of Orazca's ability: Draw a card.",
            source_card="Arch of Orazca", controller="Rick"))
        assert actions == [] or all(
            a["action"] not in ("prevent_combat_damage", "pump_all_creatures",
                                "move_card")
            for a in actions)


# ---------------------------------------------------------------------------
# S2: one-tap X budgeting
# ---------------------------------------------------------------------------

def _rw_board(player, make_card, n_basic_plains=3, n_mountains=2):
    """The batch board shape: basics + one OR-dual + an any-color rock."""
    for _ in range(n_basic_plains):
        player.battlefield.append(make_card(
            "Plains", type_line="Basic Land — Plains",
            oracle_text="({T}: Add {W}.)", power=None, toughness=None))
    for _ in range(n_mountains):
        player.battlefield.append(make_card(
            "Mountain", type_line="Basic Land — Mountain",
            oracle_text="({T}: Add {R}.)", power=None, toughness=None))
    player.battlefield.append(make_card(
        "Sacred Foundry", type_line="Land — Mountain Plains",
        oracle_text="({T}: Add {R} or {W}.)", power=None, toughness=None))
    player.battlefield.append(make_card(
        "Arcane Signet", type_line="Artifact",
        oracle_text="{T}: Add one mana of any color in your commander's "
                    "color identity.", power=None, toughness=None))


class TestOneTapXSizing:
    def test_one_tap_total_counts_each_source_once(self, game, make_card):
        rick = game.players[0]
        _rw_board(rick, make_card)  # 7 physical sources, 1 dual
        assert rick.one_tap_mana_total() == 7
        # The advertisement is allowed to be higher (per-color capacity) —
        # the point of the helper is that the TOTAL budget is not.
        assert rick.available_mana() >= rick.one_tap_mana_total()

    def test_x_sizing_uses_one_tap_ceiling_and_cast_pays(self, game, make_card):
        from mtg.engine import GameEngine
        from mtg.spells import _compute_alt_costs
        rick = game.players[0]
        _rw_board(rick, make_card)  # one-tap total 7
        geyser = make_card("Volcanic Geyser", type_line="Instant",
                           mana_cost="{X}{R}{R}", cmc=2,
                           oracle_text="Volcanic Geyser deals X damage to "
                                       "any target.",
                           power=None, toughness=None)
        rick.hand.append(geyser)
        early, costs = _compute_alt_costs(
            GameEngine(None), game, rick, geyser, pay_mana=True,
            additional_cost=0)
        assert early is None
        # 7 one-tap total − 2 fixed pips = X of 5, NOT the inflated 6 the
        # double-counting advertisement produced in the batch.
        assert costs['x_value_chosen'] == 5
        assert costs['total_cost'] == 7
        # And the sized cost must actually be PAYABLE — the whole point.
        assert rick.tap_sources_for_cost(
            "{X}{R}{R}", x_value=costs['x_value_chosen'], game=game)


# ---------------------------------------------------------------------------
# S3: cast-as name in the _last_cast_failure stash (structural pin — the
# producers are inline in the two executors; the consumer's name gate is
# exercised by tests/test_july20 pins)
# ---------------------------------------------------------------------------

class TestStashCastAsName:
    @pytest.mark.parametrize("relpath", ["mtg/autoplay.py", "mtg/engine.py"])
    def test_producers_record_cast_as_name(self, relpath):
        src = (REPO / relpath).read_text(encoding="utf-8")
        # Every _last_cast_failure producer must sit under a cast_as_adventure
        # conditional so adventure-half retries match the stash.
        producers = [m.start() for m in re.finditer(
            r"game\._last_cast_failure = \(game\.turn_number", src)]
        assert producers, f"{relpath}: stash producer disappeared — update this pin"
        for pos in producers:
            window = src[max(0, pos - 700):pos]
            assert "cast_as_adventure" in window, (
                f"{relpath}: a _last_cast_failure producer records card.name "
                f"without the cast-as-adventure name switch — adventure-half "
                f"retries will drop the real failure reason again")


# ---------------------------------------------------------------------------
# S4: adventure CMC through the CACHE shape (front-face type_line)
# ---------------------------------------------------------------------------

def _cache_shape_flaxen(make_card):
    """A Card the way the deck loader ACTUALLY builds it from the disk cache:
    combined mana_cost, creature-face-only type_line."""
    return make_card(
        "Flaxen Intruder", mana_cost="{G} // {5}{G}{G}",
        type_line="Creature — Human Berserker",  # NO adventure half — the cache shape
        oracle_text="Whenever this creature deals combat damage to a player, "
                    "you may sacrifice it.",
        power="1", toughness="2")


class TestAdventureCmcCacheShape:
    SCRYFALL_FACES = {
        "layout": "adventure",
        "card_faces": [
            {"name": "Flaxen Intruder", "mana_cost": "{G}",
             "type_line": "Creature — Human Berserker",
             "oracle_text": "Whenever this creature deals combat damage to a "
                            "player, you may sacrifice it."},
            {"name": "Welcome Home", "mana_cost": "{5}{G}{G}",
             "type_line": "Sorcery — Adventure",
             "oracle_text": "Create three 2/2 green Bear creature tokens."},
        ],
    }

    def _extract(self, card):
        from mtg.deck_loader import DeckLoader
        loader = DeckLoader.__new__(DeckLoader)  # method needs no init state
        loader._extract_adventure_data(card, self.SCRYFALL_FACES)
        return card

    def test_extract_recomputes_cmc_from_creature_face(self, make_card):
        card = _cache_shape_flaxen(make_card)
        # The cache shape defeats _parse_cmc's type_line gate: pre-fix the
        # combined string parsed to 1 + 7 = 8.
        assert card.cmc == 8, "precondition drifted — combined parse changed"
        self._extract(card)
        assert card.adventure_name == "Welcome Home"
        assert card.cmc == 1, "adventure MV is the creature face's (CR 715.2b)"

    def test_plan_validate_prices_creature_face(self, game, make_card):
        from mtg.ai_turn import _validate_plan_mana
        rick = game.players[0]
        card = self._extract(_cache_shape_flaxen(make_card))
        rick.hand.append(card)
        for _ in range(2):
            rick.battlefield.append(make_card(
                "Forest", type_line="Basic Land — Forest",
                oracle_text="({T}: Add {G}.)", power=None, toughness=None))
        plan = [{"type": "cast", "card": "Flaxen Intruder"}]
        validated = _validate_plan_mana(None, game, 0, plan)
        assert any(a.get("card") == "Flaxen Intruder" for a in validated), (
            "a 1-mana adventure creature must survive plan-validate on a "
            "2-land board (was rejected as CMC 8 in batch 15325)")

    def test_plan_validate_prices_adventure_half_by_name(self, game, make_card):
        from mtg.ai_turn import _validate_plan_mana
        rick = game.players[0]
        card = self._extract(_cache_shape_flaxen(make_card))
        rick.hand.append(card)
        for _ in range(2):
            rick.battlefield.append(make_card(
                "Forest", type_line="Basic Land — Forest",
                oracle_text="({T}: Add {G}.)", power=None, toughness=None))
        # The adventure half named directly: {5}{G}{G} = 7 > 2 available →
        # rejected on COST (not "not in hand" — the name must resolve).
        plan = [{"type": "cast", "card": "Welcome Home"}]
        validated = _validate_plan_mana(None, game, 0, plan)
        assert not any(a.get("card") == "Welcome Home" for a in validated)


# ---------------------------------------------------------------------------
# S5: refused-tail templates (Port Razer / Frenzied Trapbreaker)
# ---------------------------------------------------------------------------

class TestRefusedTailTemplates:
    def test_port_razer_untaps_own_creatures_on_connect(self, game, make_card):
        rick = game.players[0]
        tapped = make_card("Goblin Guide", tapped=True)
        untapped = make_card("Monastery Swiftspear", tapped=False)
        noncreature = make_card("Sol Ring", type_line="Artifact", tapped=True)
        rick.battlefield.extend([tapped, untapped, noncreature])
        actions, _desc = _lib().resolve_attack_trigger(
            trigger_card_name="Port Razer",
            trigger_oracle="Whenever this creature deals combat damage to a "
                           "player, untap each creature you control.",
            attacking_creature_name="Port Razer",
            attacking_creature_power=4,
            controller="Rick", opponent="Claude",
            game_context={"damage_dealt": 4, "_controller_player": rick})
        # Aug 1 deferred slate: the additional combat phase is GRANTED now
        # (the Moraug consumption machinery) — the action rides after the
        # untaps.
        assert actions == [{"action": "untap", "card": "Goblin Guide"},
                           {"action": "additional_combat",
                            "source": "Port Razer"}], (
            "only the TAPPED creature untaps (artifacts and untapped "
            "creatures excluded), then the extra combat is granted")

    def test_port_razer_declare_time_no_fire(self, game, make_card):
        rick = game.players[0]
        rick.battlefield.append(make_card("Goblin Guide", tapped=True))
        actions, _desc = _lib().resolve_attack_trigger(
            trigger_card_name="Port Razer",
            trigger_oracle="Whenever this creature deals combat damage to a "
                           "player, untap each creature you control.",
            attacking_creature_name="Port Razer",
            attacking_creature_power=4,
            controller="Rick", opponent="Claude",
            game_context={"_controller_player": rick})  # no damage_dealt
        assert actions == [], "damage-gated — must not fire at declare time"

    def test_frenzied_trapbreaker_destroys_best_defender_artifact(
            self, game, make_card):
        claude = game.players[1]
        claude.battlefield.append(make_card(
            "Sol Ring", type_line="Artifact", cmc=1,
            power=None, toughness=None))
        claude.battlefield.append(make_card(
            "Smothering Tithe", type_line="Enchantment", cmc=4,
            power=None, toughness=None))
        claude.battlefield.append(make_card("Grizzly Bears"))
        actions, _desc = _lib().resolve_attack_trigger(
            trigger_card_name="Frenzied Trapbreaker",
            trigger_oracle="Whenever this creature attacks, destroy target "
                           "artifact or enchantment defending player controls.",
            attacking_creature_name="Frenzied Trapbreaker",
            attacking_creature_power=3,
            controller="Rick", opponent="Claude",
            game_context={"_opponent_player": claude})
        assert actions == [{"action": "destroy", "card": "Smothering Tithe"}], (
            "highest-MV artifact/enchantment of the DEFENDING player")

    def test_frenzied_trapbreaker_fizzles_without_targets_and_skips_damage_step(
            self, game, make_card):
        claude = game.players[1]
        claude.battlefield.append(make_card("Grizzly Bears"))
        lib = _lib()
        # No artifact/enchantment → handled no-op (CR 603.3c), NOT a Tier-3
        # escalation.
        actions, _desc = lib.resolve_attack_trigger(
            trigger_card_name="Frenzied Trapbreaker",
            trigger_oracle="Whenever this creature attacks, destroy target "
                           "artifact or enchantment defending player controls.",
            attacking_creature_name="Frenzied Trapbreaker",
            attacking_creature_power=3,
            controller="Rick", opponent="Claude",
            game_context={"_opponent_player": claude})
        assert actions == []
        # And the combat-damage dispatch sharing the registry must not
        # re-fire the declare-time trigger.
        claude.battlefield.append(make_card(
            "Sol Ring", type_line="Artifact", power=None, toughness=None))
        actions, _desc = lib.resolve_attack_trigger(
            trigger_card_name="Frenzied Trapbreaker",
            trigger_oracle="Whenever this creature attacks, destroy target "
                           "artifact or enchantment defending player controls.",
            attacking_creature_name="Frenzied Trapbreaker",
            attacking_creature_power=3,
            controller="Rick", opponent="Claude",
            game_context={"damage_dealt": 3, "_opponent_player": claude})
        assert actions == []


# ---------------------------------------------------------------------------
# S6: Aetherize-class main-phase hold
# ---------------------------------------------------------------------------

class TestAetherizeMainPhaseHold:
    def test_mass_attacker_bounce_held_in_main_phase(self, game, make_card):
        from mtg.ai_turn import _validate_plan_mana
        claude = game.players[1]
        claude.hand.append(make_card(
            "Aetherize", type_line="Instant", mana_cost="{3}{U}", cmc=4,
            oracle_text="Return all attacking creatures to their owner's "
                        "hand.", power=None, toughness=None))
        for _ in range(4):
            claude.battlefield.append(make_card(
                "Island", type_line="Basic Land — Island",
                oracle_text="({T}: Add {U}.)", power=None, toughness=None))
        plan = [{"type": "cast", "card": "Aetherize"}]
        validated = _validate_plan_mana(None, game, 1, plan)
        assert not any(a.get("card") == "Aetherize" for a in validated), (
            "attacker-mass-bounce in a main phase is a guaranteed-dead cast")
        # Recorded so the AI's rejection feedback loop engages.
        assert any("Aetherize" in str(r) for r in
                   (getattr(game, '_recent_plan_rejections', None) or []))
