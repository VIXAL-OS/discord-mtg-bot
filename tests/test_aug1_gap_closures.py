"""Aug 1, 2026 — three evidence-backed gap closures (approved as one package).

1. NONCOMBAT damage now reaches the damaged-creature scan. The slice-5b
   scan ("whenever a source deals damage to this creature" — Phyrexian
   Obliterator) heard combat damage only; CR 603.2 says A SOURCE, any
   source, and the batch-10 R14 evidence was exactly Obliterator vs burn.
   The scan body moved to triggers.scan_damaged_creature (the combat drain
   keeps its source-controller resolution + died-mid-combat fallback) and
   is now invoked from the deal_damage action's creature branch, the
   Tier-2 damage exec, and the fight exec.

2. Spectacle (CR 702.137) — see TestSpectacle below.

3. Animate-land duration — see TestAnimateLandDuration below.

Oracle texts are cache-verified; fixtures go through the REAL entry points
(the pin-shape-reachability rule).
"""
import asyncio
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

OBLITERATOR = ("Trample\nWhenever a source deals damage to this creature, "
               "that source's controller sacrifices that many permanents of "
               "their choice.")


def _obliterator(make_card):
    return make_card("Phyrexian Obliterator", oracle_text=OBLITERATOR,
                     type_line="Creature — Phyrexian Horror",
                     power="5", toughness="5")


def _fill_board(player, make_card, n):
    perms = [make_card(f"Perm {i}", type_line="Artifact",
                       power=None, toughness=None) for i in range(n)]
    player.battlefield.extend(perms)
    return perms


# ---------------------------------------------------------------------------
# 1. Noncombat damage → the damaged-creature scan
# ---------------------------------------------------------------------------

class TestNoncombatDamagedCreatureScan:
    def test_bolt_via_action_handler_forces_sacrifices(self, game, rules, make_card):
        rick, claude = game.players
        oblit = _obliterator(make_card)
        claude.battlefield.append(oblit)
        _fill_board(rick, make_card, 4)
        msg = rules._execute_action_on_state(game, {
            "action": "deal_damage", "amount": 3,
            "target_card": "Phyrexian Obliterator",
            "_source_card_name": "Lightning Bolt",
            "_source_controller": "Rick",
        })
        assert oblit.damage_marked == 3
        assert len(rick.battlefield) == 1, (
            "the Bolt's controller sacrifices 3 permanents (CR 603.2 — "
            "ANY source, not just combat)")
        assert "damage to" in (msg or "")

    def test_heuristic_source_controller_when_unthreaded(self, game, rules, make_card):
        # Template/Tier-3 damage without _source_controller: the damaged
        # owner's opponent is the source's controller (2-player heuristic).
        rick, claude = game.players
        oblit = _obliterator(make_card)
        claude.battlefield.append(oblit)
        _fill_board(rick, make_card, 3)
        rules._execute_action_on_state(game, {
            "action": "deal_damage", "amount": 2,
            "target_card": "Phyrexian Obliterator",
        })
        assert len(rick.battlefield) == 1

    def test_tier2_damage_exec_fires_the_scan(self, game, rules, make_card):
        from rules.spell_resolver import SpellResolver
        from rules.effects import Effect, EffectType, ExecutionContext
        rick, claude = game.players
        oblit = _obliterator(make_card)
        claude.battlefield.append(oblit)
        _fill_board(rick, make_card, 3)
        game._rules_engine = rules
        effect = Effect(effect_type=EffectType.DAMAGE, amount=2)
        bolt = make_card("Lightning Bolt", type_line="Instant",
                         power=None, toughness=None)
        ctx = ExecutionContext(game_state=game, source_card=bolt,
                               source_controller=rick, targets=[oblit])
        resolver = SpellResolver.__new__(SpellResolver)
        asyncio.run(resolver._exec_damage(effect, ctx, game))
        assert len(rick.battlefield) == 1, (
            "Tier-2 burn must fire the damaged-creature scan with the "
            "caster as the source's controller")

    def test_no_trigger_no_sacrifices(self, game, rules, make_card):
        rick, claude = game.players
        bear = make_card("Grizzly Bears", power="2", toughness="4")
        claude.battlefield.append(bear)
        _fill_board(rick, make_card, 3)
        rules._execute_action_on_state(game, {
            "action": "deal_damage", "amount": 2,
            "target_card": "Grizzly Bears",
            "_source_controller": "Rick",
        })
        assert len(rick.battlefield) == 3, "no trigger, no edict"


# ---------------------------------------------------------------------------
# 2. Spectacle (CR 702.137)
# ---------------------------------------------------------------------------

LIGHT_UP = ("Spectacle {R} (You may cast this spell for its spectacle cost "
            "rather than its mana cost if an opponent lost life this turn.)\n"
            "Exile the top two cards of your library. Until the end of your "
            "next turn, you may play those cards.")


def _light_up(make_card):
    return make_card("Light Up the Stage", type_line="Sorcery",
                     mana_cost="{2}{R}", cmc=3, oracle_text=LIGHT_UP,
                     power=None, toughness=None)


def _one_mountain(player, make_card):
    player.battlefield.append(make_card(
        "Mountain", type_line="Basic Land — Mountain",
        oracle_text="({T}: Add {R}.)", power=None, toughness=None))


class TestSpectacle:
    def test_parse_and_condition(self, game, make_card):
        from mtg.helpers import parse_spectacle_cost, spectacle_available
        assert parse_spectacle_cost(LIGHT_UP) == "{R}"
        assert parse_spectacle_cost(
            "cast this spell for its spectacle cost rather than") is None
        rick, claude = game.players
        card = _light_up(make_card)
        assert spectacle_available(game, rick, card) is None, (
            "no opponent lost life yet")
        claude.life -= 1
        claude.record_life_loss(1)
        assert spectacle_available(game, rick, card) == "{R}"
        # The CASTER's own life loss must not satisfy their condition.
        assert spectacle_available(game, claude, card) is None

    def test_pre_gate_passes_on_spectacle(self, game, rules, make_card):
        rick, claude = game.players
        _one_mountain(rick, make_card)  # 1 mana — printed {2}{R} unpayable
        card = _light_up(make_card)
        rick.hand.append(card)
        can, _ = rules.can_cast_spell(game, rick, card)
        assert not can, "sanity: condition not met → printed cost gates"
        claude.record_life_loss(2)
        can, reason = rules.can_cast_spell(game, rick, card)
        assert can, f"spectacle {{R}} payable off one Mountain: {reason}"

    def test_compute_alt_costs_takes_spectacle(self, game, make_card):
        from mtg.engine import GameEngine
        from mtg.spells import _compute_alt_costs
        rick, claude = game.players
        _one_mountain(rick, make_card)
        claude.record_life_loss(2)
        card = _light_up(make_card)
        rick.hand.append(card)
        early, costs = _compute_alt_costs(GameEngine(None), game, rick, card,
                                          pay_mana=True, additional_cost=0)
        assert early is None
        assert costs['effective_mana_cost'] == "{R}"
        assert costs['total_cost'] == 1
        assert card._was_spectacled is True

    def test_condition_unmet_charges_printed(self, game, make_card):
        from mtg.engine import GameEngine
        from mtg.spells import _compute_alt_costs
        rick = game.players[0]
        for _ in range(3):
            _one_mountain(rick, make_card)
        card = _light_up(make_card)
        rick.hand.append(card)
        early, costs = _compute_alt_costs(GameEngine(None), game, rick, card,
                                          pay_mana=True, additional_cost=0)
        assert costs['effective_mana_cost'] == "{2}{R}"
        assert card._was_spectacled is False

    def test_castable_list_offers_spectacle(self, game, make_card):
        from mtg.legal_actions import castable_entries
        rick, claude = game.players
        card = _light_up(make_card)
        rick.hand.append(card)
        # Pool: exactly one red — printed {2}{R} fails, spectacle {R} passes.
        # (Real Mountain backing the claim — the provider caps the advertised
        # total at the physical one-tap ceiling, Aug 2 batch-13.)
        _one_mountain(rick, make_card)
        pool = {'W': 0, 'U': 0, 'B': 0, 'R': 1, 'G': 0, 'C': 0}
        entries = castable_entries(game, rick, pool, 0, 1)
        assert not any("Light Up the Stage" in e["label"] for e in entries), (
            "condition unmet → not offered at 1 mana")
        claude.record_life_loss(2)
        entries = castable_entries(game, rick, pool, 0, 1)
        spec = [e for e in entries if "SPECTACLE" in e["label"]]
        assert spec and spec[0]["action"] == {"type": "cast",
                                              "card": "Light Up the Stage"}

    def test_impulse_exile_marks_playable_and_surfaces(self, game, rules, make_card):
        from mtg.legal_actions import graveyard_castable_entries
        rick = game.players[0]
        bolt = make_card("Lightning Bolt", type_line="Instant",
                         mana_cost="{R}", cmc=1, power=None, toughness=None)
        forest = make_card("Forest", type_line="Basic Land — Forest",
                           power=None, toughness=None)
        rick.library = [bolt, forest]
        msg = rules._execute_action_on_state(game, {
            "action": "exile_top_of_library", "player": "Rick",
            "count": 2, "playable": True})
        assert bolt in rick.exile and forest in rick.exile
        assert bolt.id in rick.playable_from_exile
        pool = {'W': 0, 'U': 0, 'B': 0, 'R': 1, 'G': 0, 'C': 0}
        entries = graveyard_castable_entries(rick, pool, 0, 1)
        labels = [e["label"] for e in entries]
        assert any("IMPULSE from exile" in l and "Lightning Bolt" in l
                   for l in labels), labels
        assert not any("Forest" in l for l in labels), (
            "lands are played, not cast — excluded from the impulse offer")


# ---------------------------------------------------------------------------
# 3. Animate-land duration — "until your next turn" (Sylvan Awakening)
# ---------------------------------------------------------------------------

class TestAnimateLandDuration:
    def _animate(self, game, rules, player_name, duration=None):
        action = {"action": "animate_land", "player": player_name,
                  "scope": "all", "power": 2, "toughness": 2,
                  "keywords": "reach,indestructible,haste"}
        if duration:
            action["duration"] = duration
        return rules._execute_action_on_state(game, action)

    def _forest(self, make_card):
        return make_card("Forest", type_line="Basic Land — Forest",
                         oracle_text="({T}: Add {G}.)",
                         power=None, toughness=None)

    def test_until_your_next_turn_survives_end_step_revert(self, game, rules, make_card):
        rick = game.players[0]
        forest = self._forest(make_card)
        rick.battlefield.append(forest)
        msg = self._animate(game, rules, "Rick",
                            duration="until_your_next_turn")
        assert "until your next turn" in (msg or "")
        assert forest._animated_expires_at_turn_of == 0
        assert forest.is_creature(), "animated while the effect lasts"
        # The end-step revert (rules.on_end_step) must SKIP it — the lands
        # block on the opponent's turn (the brawl-mirror evidence: Sylvan
        # Awakening's whole point after the caster's turn).
        rules.on_end_step(game)
        assert getattr(forest, '_animated_until_eot', False), (
            "an until-your-next-turn animation survives the end step")
        assert forest.is_creature()

    def test_reverts_when_controllers_next_turn_begins(self, game, rules, make_card):
        from mtg.engine import GameEngine
        ge = GameEngine(None)
        rules2 = ge.rules
        rick = game.players[0]
        forest = self._forest(make_card)
        rick.battlefield.append(forest)
        rules2._execute_action_on_state(game, {
            "action": "animate_land", "player": "Rick", "scope": "all",
            "power": 2, "toughness": 2,
            "duration": "until_your_next_turn"})
        # Rick's turn ends → Claude's turn: still animated.
        game.active_player_index = 0
        ge.end_turn(game)
        assert game.active_player_index == 1
        assert forest.is_creature(), "survives into the opponent's turn"
        # Claude's turn ends → Rick's next turn begins: reverts.
        ge.end_turn(game)
        assert game.active_player_index == 0
        assert not getattr(forest, '_animated_until_eot', False)
        assert not forest.is_creature(), (
            '"until your next turn" ends as the controller\'s turn begins')
        assert forest._animated_expires_at_turn_of is None

    def test_default_duration_still_reverts_at_end_step(self, game, rules, make_card):
        rick = game.players[0]
        forest = self._forest(make_card)
        rick.battlefield.append(forest)
        msg = self._animate(game, rules, "Rick")  # no duration = EOT
        assert "until end of turn" in (msg or "")
        assert forest._animated_expires_at_turn_of is None
        rules.on_end_step(game)
        assert not getattr(forest, '_animated_until_eot', False), (
            "the plain EOT class (Living Lands, Awaken) is unchanged")
        assert not forest.is_creature()

    def test_sylvan_awakening_json_carries_the_duration(self):
        import json
        data = json.loads((REPO / "data/card_templates.json").read_text(
            encoding="utf-8"))
        entries = data["templates"]
        sylvan = next(e for e in entries
                      if isinstance(e, dict) and e.get("key") == "sylvan awakening")
        assert sylvan["actions"][0].get("duration") == "until_your_next_turn"
