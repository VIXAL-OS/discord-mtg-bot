"""July 21 — scripted forcing of the carry-forward "unexercised" paths.

The batch matrix kept missing these by luck (The Abyss sat castable in hand
for 11 turns; Slumber was never drawn in 4 snow games; Yorion was never
cast; Jace WoM's only cast was countered). Same playbook as the May 30
controller-order test: force the branch the matrix structurally can't
reach, keep the repro as a permanent pin. FoW's alternate-cost payment is
NOT here — it already has an end-to-end pin in test_july20_audit.py.

The Jace WoM test exposed a real gap on first write: the engine had NO
draw-from-empty win replacement (CR 614.12) — Laboratory Maniac / Jace,
Wielder of Mysteries lost their controller the game per the unconditional
CR 104.3c branch. Implemented in engine.draw_cards alongside this suite.
"""
import asyncio

import pytest

from mtg.constants import Phase


def _engine():
    from mtg.engine import GameEngine
    return GameEngine(None)


ABYSS_ORACLE = ("At the beginning of each player's upkeep, destroy target "
                "nonartifact creature that player controls of their choice. "
                "It can't be regenerated.")

SLUMBER_ORACLE = ("When Marit Lage's Slumber enters the battlefield or "
                  "another snow permanent you control enters, scry 1.\n"
                  "At the beginning of your upkeep, if you control ten or "
                  "more snow permanents, sacrifice Marit Lage's Slumber and "
                  "create Marit Lage, a legendary 20/20 black Avatar "
                  "creature token with flying and indestructible.")

JACE_WOM_ORACLE = ("If you would draw a card while your library has no "
                   "cards in it, you win the game instead.\n"
                   "+1: Target player mills two cards. Draw a card.\n"
                   "-8: Draw seven cards. Then if your library has no cards "
                   "in it, you win the game.")


class TestTheAbyssUpkeep:
    # Deferred trace from the July 20 round-2 audit ("two Resolved firings,
    # no visible outcome") — the assigned batch-3 game never cast it, so the
    # branch is forced here instead.

    def _setup(self, make_game, make_card):
        game = make_game()
        rick, claude = game.players
        game.active_player_index = 0  # Rick's upkeep
        abyss = make_card("The Abyss", type_line="World Enchantment",
                          oracle_text=ABYSS_ORACLE)
        claude.battlefield.append(abyss)
        return game, rick, claude

    def test_destroys_active_players_nonartifact_creature(self, make_game, make_card):
        from mtg.triggers import _check_upkeep_triggers_sync
        game, rick, claude = self._setup(make_game, make_card)
        bears = make_card("Grizzly Bears", type_line="Creature — Bear",
                          power="2", toughness="2", cmc=2)
        golem = make_card("Meteor Golem", type_line="Artifact Creature — Golem",
                          power="3", toughness="3", cmc=7)
        rick.battlefield.extend([bears, golem])

        msgs, unhandled = _check_upkeep_triggers_sync(_engine(), game)

        assert bears not in rick.battlefield, "nonartifact creature must be destroyed"
        assert bears in rick.graveyard
        assert golem in rick.battlefield, "artifact creature is not a legal Abyss target"
        assert not any("The Abyss" in getattr(c, 'name', '') for c, _t in unhandled)

    def test_no_nonartifact_creature_is_explicit_no_op(self, make_game, make_card):
        from mtg.triggers import _check_upkeep_triggers_sync
        game, rick, claude = self._setup(make_game, make_card)
        golem = make_card("Meteor Golem", type_line="Artifact Creature — Golem",
                          power="3", toughness="3", cmc=7)
        rick.battlefield.append(golem)

        msgs, unhandled = _check_upkeep_triggers_sync(_engine(), game)

        assert golem in rick.battlefield
        assert not any("The Abyss" in getattr(c, 'name', '') for c, _t in unhandled), \
            "no-creature branch must be a handled no-op, not a Tier-3 escalation"


class TestSlumberSnowWatcher:
    def test_snow_permanent_entering_fires_scry(self, make_game, make_card, capsys):
        # Pub/sub slice 2's motivating deferral: Slumber's "or another snow
        # permanent you control enters, scry 1" half. Never live-exercised —
        # Slumber wasn't drawn in any of the batch's 4 snow games.
        game = make_game()
        rick = game.players[0]
        engine = _engine()
        rick.battlefield.append(make_card("Marit Lage's Slumber",
                                          type_line="Snow Enchantment",
                                          oracle_text=SLUMBER_ORACLE))
        rick.library.extend(make_card(f"Filler {i}", type_line="Sorcery")
                            for i in range(3))
        rick.hand.append(make_card("Snow-Covered Island",
                                   type_line="Basic Snow Land — Island"))

        engine.rules._execute_action_on_state(game, {
            "action": "move_card", "card": "Snow-Covered Island",
            "from_zone": "hand", "to_zone": "battlefield",
            "player": rick.name})

        out = capsys.readouterr().out
        assert "[SNOW-WATCHER]" in out
        assert any("Marit Lage's Slumber" in m
                   for m in (game._pending_messages or []))

    def test_nonsnow_permanent_does_not_fire(self, make_game, make_card, capsys):
        game = make_game()
        rick = game.players[0]
        engine = _engine()
        rick.battlefield.append(make_card("Marit Lage's Slumber",
                                          type_line="Snow Enchantment",
                                          oracle_text=SLUMBER_ORACLE))
        rick.hand.append(make_card("Island", type_line="Basic Land — Island"))

        engine.rules._execute_action_on_state(game, {
            "action": "move_card", "card": "Island",
            "from_zone": "hand", "to_zone": "battlefield",
            "player": rick.name})

        assert "[SNOW-WATCHER]" not in capsys.readouterr().out


class TestYorionDelayedReturn:
    def test_mass_flicker_delayed_exiles_then_returns_at_end_step(
            self, make_game, make_card):
        # July 20 fix, never live-exercised (Yorion sat in hand all game):
        # delayed_return exiles now and schedules the CR 603.7 end-step
        # return instead of the instant re-entry that produced the
        # Yorion ↔ Felidar 204-flicker storm.
        engine = _engine()
        game = make_game()
        rick = game.players[0]
        yorion = make_card("Yorion, Sky Nomad",
                           type_line="Legendary Creature — Bird Serpent",
                           power="4", toughness="5")
        warden = make_card("Soul Warden", type_line="Creature — Human Cleric",
                           power="1", toughness="1",
                           oracle_text="Whenever another creature enters, "
                                       "you gain 1 life.")
        pacifism = make_card("Pacifism", type_line="Enchantment — Aura",
                             oracle_text="Enchant creature")
        rick.battlefield.extend([yorion, warden, pacifism])
        rick.life = 40

        engine.rules._execute_action_on_state(game, {
            "action": "mass_flicker", "player": rick.name, "count": 5,
            "exclude_lands": True, "exclude_self": "Yorion, Sky Nomad",
            "delayed_return": True})

        # Exiled NOW, nothing returned yet, return scheduled for end step.
        assert warden in rick.exile and pacifism in rick.exile
        assert warden not in rick.battlefield
        assert yorion in rick.battlefield  # excludes itself
        assert any(dt.get('trigger_at') == 'end_step'
                   for dt in game.delayed_triggers)

        msgs = engine._process_delayed_triggers(game, "end_step")

        assert warden in rick.battlefield and pacifism in rick.battlefield
        assert warden not in rick.exile
        assert not any(dt.get('source') == 'Yorion, Sky Nomad'
                       for dt in game.delayed_triggers), "one-shot trigger consumed"


class TestDrawEmptyWinReplacement:
    def test_jace_wielder_of_mysteries_wins_instead_of_losing(
            self, make_game, make_card):
        # Forced here for the first time — Jace WoM's only batch cast was
        # countered. Writing the test exposed that the win replacement
        # (CR 614.12, Lab Man / Jace WoM) did not exist at all: the
        # unconditional CR 104.3c branch lost the game for the player.
        engine = _engine()
        game = make_game()
        rick = game.players[0]
        rick.battlefield.append(make_card(
            "Jace, Wielder of Mysteries",
            type_line="Legendary Planeswalker — Jace",
            oracle_text=JACE_WOM_ORACLE, loyalty="4"))
        rick.library.clear()

        drawn = engine.draw_cards(rick, 1, game=game)

        assert drawn == []
        assert game.ended is True
        assert game.winner == 0, "the drawing player WINS with Jace WoM out"
        assert rick.name not in getattr(game, '_library_loss', set())
        assert getattr(rick, 'attempted_draw_from_empty', False) is False

    def test_empty_draw_without_replacement_still_loses(self, make_game, make_card):
        engine = _engine()
        game = make_game()
        rick = game.players[0]
        rick.library.clear()

        engine.draw_cards(rick, 1, game=game)

        assert game.ended is not True  # loss lands via the SBA pipeline
        assert rick.name in getattr(game, '_library_loss', set())
