"""Pub/sub slice 5b (July 31, 2026) — COMBAT_DAMAGE_DEALT consumers on the bus.

The 5a shadow gate cleared (zero [EVENT-PARITY-CDD] over batch 15324's 134
FS-step combats / 2,496 player-damage events), so the consumers flipped:

- _accumulate_combat_damage_subscriber (mtg/triggers.py) is the SOLE
  sanctioned appender for game._combat_damage_to_player and
  ._combat_damage_to_creature (the slice-3b accumulate-don't-resolve
  pattern; the drains keep their `not game.ended` CR 104.2a gate).
- The old name-gated Ohran Frostfang block generalized to the whole
  "whenever a [qualifier] creature you control deals combat damage to a
  player" battlefield-watcher family (Tovolar's wolves draw on EVERY wolf
  connect now, not just his own).
- The damaged-creature scan exists for the first time ("whenever a source
  deals damage to this creature" — Phyrexian Obliterator, July 30 reviewer
  R14). Combat damage only; the noncombat paths emit no event yet.
- The 5a recorder + scaffolding fields are retired; [EVENT-PARITY-CDD] is a
  stale-code tripwire like its 2c/3c/4b siblings.
"""
import inspect
import re

import pytest

import mtg.triggers  # noqa: F401 — registers the subscriber at import
from mtg import events


OBLITERATOR_ORACLE = ("Trample\n"
                      "Whenever a source deals damage to this creature, that "
                      "source's controller sacrifices that many permanents.")


class TestBusFeedsTheQueues:
    def test_player_funnel_feeds_the_trigger_list(self, rules, game, make_card):
        bear = make_card("Bear")
        game.players[0].battlefield.append(bear)
        rules._apply_combat_damage_to_player(game, game.players[1], 2, bear)
        assert [(e[0].name, e[1].name, e[2])
                for e in game._combat_damage_to_player] == [("Bear", "Rick", 2)]

    def test_prevented_damage_feeds_nothing(self, rules, game, make_card):
        bear = make_card("Bear")
        game.players[0].battlefield.append(bear)
        game.players[1]._damage_prevented = True
        rules._apply_combat_damage_to_player(game, game.players[1], 3, bear)
        assert game._combat_damage_to_player == [], (
            "0 damage isn't dealt (CR 119.3)")

    def test_creature_funnel_feeds_the_creature_list(self, rules, game, make_card):
        att = make_card("Attacker", power="3", toughness="3")
        blk = make_card("Blocker", power="1", toughness="4")
        game.players[0].battlefield.append(att)
        game.players[1].battlefield.append(blk)
        rules._apply_combat_damage_to_creature(game, blk, 3, att)
        assert [(e[0].name, e[1].name, e[2])
                for e in game._combat_damage_to_creature] == [("Attacker", "Blocker", 3)]

    def test_dead_dealer_falls_back_to_non_target_player(self, rules, game, make_card):
        # FS trades: the dealer can be off the battlefield by application
        # time — the subscriber must still attribute a controller.
        ghost = make_card("Ghost Attacker")
        rules._apply_combat_damage_to_player(game, game.players[1], 2, ghost)
        assert [(e[1].name) for e in game._combat_damage_to_player] == ["Rick"]

    def test_no_raw_appends_outside_the_subscriber(self):
        # Structural pin (the slice-3b convention): the subscriber is the
        # only sanctioned appender for both queues.
        import mtg.combat as combat
        import mtg.triggers as triggers
        for mod in (combat,):
            src = inspect.getsource(mod)
            assert '_combat_damage_to_player.append' not in src
            assert '_combat_damage_to_creature.append' not in src
        tsrc = inspect.getsource(triggers)
        sub_src = inspect.getsource(
            triggers._accumulate_combat_damage_subscriber)
        assert tsrc.count('_combat_damage_to_player.append') == \
            sub_src.count('_combat_damage_to_player.append') == 1
        assert tsrc.count('_combat_damage_to_creature.append') == \
            sub_src.count('_combat_damage_to_creature.append') == 1


class TestWatcherFamilyGeneralized:
    def _connect(self, rules, game, make_card, watcher_oracle, dealer_types,
                 watcher_name="Watcher"):
        rick = game.players[0]
        watcher = make_card(watcher_name, oracle_text=watcher_oracle,
                            type_line="Creature — Snake")
        dealer = make_card("Dealer", power="3", toughness="3",
                           type_line=dealer_types)
        rick.battlefield.extend([watcher, dealer])
        rick.library = [make_card(f"L{i}") for i in range(5)]
        hand_before = len(rick.hand)
        game.attackers = [dealer.id]
        game.blockers = {}
        game.active_player_index = 0
        rules.resolve_combat_damage(game)
        return len(rick.hand) - hand_before

    def test_generic_creature_watcher_draws(self, rules, game, make_card):
        drew = self._connect(
            rules, game, make_card,
            "Attacking creatures you control have deathtouch.\n"
            "Whenever a creature you control deals combat damage to a "
            "player, draw a card.",
            "Creature — Snake", watcher_name="Ohran Frostfang")
        assert drew == 1

    def test_subtype_watcher_fires_on_other_wolves(self, rules, game, make_card):
        drew = self._connect(
            rules, game, make_card,
            "Whenever a Wolf or Werewolf you control deals combat damage "
            "to a player, draw a card.",
            "Creature — Human Werewolf", watcher_name="Tovolar, Dire Overlord")
        assert drew == 1, "the watcher must fire on OTHER wolves' connects"

    def test_subtype_watcher_ignores_nonmatching_dealer(self, rules, game, make_card):
        drew = self._connect(
            rules, game, make_card,
            "Whenever a Wolf or Werewolf you control deals combat damage "
            "to a player, draw a card.",
            "Creature — Elf Druid", watcher_name="Tovolar, Dire Overlord")
        assert drew == 0

    def test_own_connect_draws_exactly_once(self, rules, game, make_card, capsys):
        # The July 31 double-draw bug shape: watcher connects itself — one
        # draw total (the watcher loop), never a second from the attacker
        # self-trigger scan (watcher-phrased text is skipped there). With
        # the templates deleted, the residual harm of a missing skip is
        # Tier-3 queue NOISE — assert that's absent too, or the mutant that
        # removes the skip survives on the draw count alone.
        rick = game.players[0]
        ohran = make_card(
            "Ohran Frostfang", power="2", toughness="6",
            type_line="Snow Creature — Snake",
            oracle_text=("Attacking creatures you control have deathtouch.\n"
                         "Whenever a creature you control deals combat damage "
                         "to a player, draw a card."))
        rick.battlefield.append(ohran)
        rick.library = [make_card(f"L{i}") for i in range(5)]
        hand_before = len(rick.hand)
        game.attackers = [ohran.id]
        game.blockers = {}
        game.active_player_index = 0
        rules.resolve_combat_damage(game)
        out = capsys.readouterr().out
        assert len(rick.hand) - hand_before == 1
        assert "[COMBAT-TRIGGER-UNHANDLED]" not in out, \
            "the attacker self-trigger scan must skip watcher-phrased text"


class TestDamagedCreatureScan:
    def test_obliterator_edict_fires(self, rules, game, make_card):
        rick, claude = game.players
        obl = make_card("Phyrexian Obliterator", power="5", toughness="5",
                        type_line="Creature — Phyrexian Horror",
                        oracle_text=OBLITERATOR_ORACLE)
        # Toughness 6 so the Obliterator's 5 retaliation doesn't kill the
        # attacker — the battlefield delta must be the 3 sacrifices alone.
        att = make_card("Reckless Attacker", power="3", toughness="6")
        filler = [make_card(f"Perm{i}", type_line="Artifact") for i in range(4)]
        claude.battlefield.append(obl)
        rick.battlefield.append(att)
        rick.battlefield.extend(filler)
        before = len(rick.battlefield)
        game.attackers = [att.id]
        game.blockers = {att.id: [obl.id]}
        game.active_player_index = 0
        rules.resolve_combat_damage(game)
        assert before - len(rick.battlefield) == 3, (
            "3 damage to the Obliterator = its source's controller "
            "sacrifices 3 permanents")

    def test_lethal_combat_suppresses_the_drain(self, rules, game, make_card):
        # CR 104.2a: once a player has lost, no further game actions. The
        # REACHABLE path for the drain's `not game.ended` gate is the game
        # ending DURING the regular damage step (an unblocked lethal swing in
        # the same combat that damaged the Obliterator) — a pre-set
        # game.ended never reaches the drain (the FS-step early return).
        rick, claude = game.players
        claude.life = 3
        obl = make_card("Phyrexian Obliterator", power="5", toughness="5",
                        oracle_text=OBLITERATOR_ORACLE)
        killer = make_card("Killer", power="5", toughness="5")
        att = make_card("Blocked Attacker", power="3", toughness="6")
        claude.battlefield.append(obl)
        rick.battlefield.extend([killer, att])
        rick.battlefield.extend(
            make_card(f"Perm{i}", type_line="Artifact") for i in range(3))
        before = len(rick.battlefield)
        game.attackers = [killer.id, att.id]
        game.blockers = {att.id: [obl.id]}
        game.active_player_index = 0
        rules.resolve_combat_damage(game)
        assert game.ended, "the unblocked 5 must have been lethal at 3 life"
        assert len(rick.battlefield) == before, \
            "no post-loss sacrifices — the Obliterator edict must not drain"

    def test_vanilla_damaged_creature_is_silent(self, rules, game, make_card, capsys):
        rick, claude = game.players
        blk = make_card("Vanilla Wall", power="0", toughness="8")
        att = make_card("Attacker", power="3", toughness="3")
        claude.battlefield.append(blk)
        rick.battlefield.append(att)
        game.attackers = [att.id]
        game.blockers = {att.id: [blk.id]}
        game.active_player_index = 0
        rules.resolve_combat_damage(game)
        out = capsys.readouterr().out
        assert "[DAMAGED-TRIGGER" not in out


class TestRecorderRetired:
    def test_recorder_and_fields_gone(self):
        import dataclasses
        import mtg.triggers as triggers
        from mtg.models import GameState
        assert not hasattr(triggers, 'report_combat_damage_parity')
        assert not hasattr(triggers, '_cdd_shadow_recorder')
        field_names = {f.name for f in dataclasses.fields(GameState)}
        assert '_cdd_bus_seen' not in field_names
        assert '_cdd_consumer_seen' not in field_names
        assert '_combat_damage_to_player' in field_names, \
            "the queue must be a DECLARED field now (staple removed)"
        assert '_combat_damage_to_creature' in field_names

    def test_parity_tag_is_a_stale_code_tripwire(self):
        # Like [EVENT-PARITY]/[EVENT-PARITY-DIES]/[EVENT-PARITY-CAST]: the
        # tag must never be EMITTED by code (a batch line means stale code
        # is running). Comment mentions are fine — strip them before
        # checking.
        import mtg.combat, mtg.engine, mtg.triggers
        for mod in (mtg.combat, mtg.engine, mtg.triggers):
            for line in inspect.getsource(mod).splitlines():
                code_part = line.split('#', 1)[0]
                assert 'EVENT-PARITY-CDD' not in code_part, line
