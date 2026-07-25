"""Pub/sub slice 2 — PERMANENT_ENTERED (July 20, 2026).

Covers the three deliverables: emit sites fire once per physical entry, the
snow-permanent watcher (Marit Lage's Slumber scry — the June 10 deferral this
slice unlocked), and the parity recorder that shadows the legacy ETB scans
until one clean batch gates slice 2b.
"""
import asyncio

import pytest

from mtg import events
from mtg.constants import Phase


class _Probe:
    """Recording subscriber with unsubscribe-on-teardown."""
    def __init__(self):
        self.received = []

    def __call__(self, game, **payload):
        self.received.append(payload)


@pytest.fixture
def probe():
    p = _Probe()
    events.subscribe(events.PERMANENT_ENTERED, p)
    yield p
    events.unsubscribe(events.PERMANENT_ENTERED, p)


def _engine():
    from mtg.engine import GameEngine
    return GameEngine(None)


def _ready(game, active_idx=0):
    game.phase = Phase.MAIN1
    game.active_player_index = active_idx
    return game


class TestEmitSites:
    def test_cast_path_emits_once(self, make_game, make_card, probe):
        from mtg.spells import cast_spell_async

        game = _ready(make_game())
        rick = game.players[0]
        rick.battlefield.extend(
            make_card(f"Swamp {i}", type_line="Basic Land — Swamp",
                      power="0", toughness="0") for i in range(2))
        bear = make_card("Charging Bear", mana_cost="{1}{B}", cmc=2)
        rick.hand.append(bear)

        ok, msg, _ = asyncio.run(cast_spell_async(_engine(), game, rick, bear))

        assert ok is True, msg
        entries = [p for p in probe.received if p["card"] is bear]
        assert len(entries) == 1
        assert entries[0]["via"] == "cast"
        assert entries[0]["controller"] is rick

    def test_play_land_emits(self, make_game, make_card, probe):
        game = _ready(make_game())
        rick = game.players[0]
        island = make_card("Island", type_line="Basic Land — Island",
                           power="0", toughness="0")
        rick.hand.append(island)

        ok, _ = _engine().play_land(game, rick, island)

        assert ok is True
        entries = [p for p in probe.received if p["card"] is island]
        assert len(entries) == 1
        assert entries[0]["via"] == "land_drop"

    def test_create_token_emits_per_token(self, rules, game, make_card, probe):
        from mtg.actions import execute_action_on_state

        execute_action_on_state(rules, game, {
            "action": "create_token", "player": "Rick", "name": "Soldier",
            "power": 1, "toughness": 1, "types": "Creature — Soldier",
            "count": 3})

        token_entries = [p for p in probe.received
                         if p["via"] == "create_token"]
        assert len(token_entries) == 3
        assert all(p["card"].is_token for p in token_entries)

    def test_move_card_to_battlefield_emits(self, rules, game, make_card, probe):
        from mtg.actions import execute_action_on_state

        rick = game.players[0]
        titan = make_card("Grave Titan", type_line="Creature — Giant",
                          power="6", toughness="6")
        rick.graveyard.append(titan)

        execute_action_on_state(rules, game, {
            "action": "move_card", "card": "Grave Titan",
            "from_zone": "graveyard", "to_zone": "battlefield",
            "player": "Rick"})

        entries = [p for p in probe.received if p["card"] is titan]
        assert len(entries) == 1
        assert entries[0]["via"] == "move_card"


class TestCastEmitTiming:
    def test_fizzled_aura_emits_no_entry(self, make_game, make_card, probe):
        # game_1528942744825757758 (first live slice-2 batch): Draconic
        # Destiny fizzled on resolution (beneficial aura, only opponent
        # creatures) — a fizzled aura never entered (CR 303.4), but the
        # append-time emit fired anyway and produced a false [EVENT-PARITY]
        # line. The emit now fires after resolution settles.
        from mtg.spells import cast_spell_async

        game = _ready(make_game())
        rick, claude = game.players
        rick.battlefield.extend(
            make_card(f"Mountain {i}", type_line="Basic Land — Mountain",
                      power="0", toughness="0") for i in range(3))
        claude.battlefield.append(make_card("Charging Bear"))
        aura = make_card("Test Destiny", type_line="Enchantment — Aura",
                         mana_cost="{1}{R}{R}", cmc=3, power=None,
                         toughness=None,
                         oracle_text="Enchant creature\nEnchanted creature "
                                     "gets +3/+3 and has flying.")
        rick.hand.append(aura)

        ok, msg, _ = asyncio.run(cast_spell_async(_engine(), game, rick, aura))

        assert ok is True, msg
        assert all(p["card"] is not aura for p in probe.received), \
            "fizzled aura must not emit PERMANENT_ENTERED"

    def test_living_weapon_germ_emits_and_feeds_soul_warden(
            self, make_game, make_card, probe):
        # The Living Weapon germ entry had NO watcher scan (Soul Warden
        # never saw a Batterskull germ) and no emit — both added July 20.
        from mtg.spells import cast_spell_async

        game = _ready(make_game())
        rick, claude = game.players
        rick.battlefield.extend(
            make_card(f"Wastes {i}", type_line="Basic Land", power="0",
                      toughness="0") for i in range(5))
        claude.battlefield.append(make_card(
            "Soul Warden", power="1", toughness="1",
            oracle_text="Whenever another creature enters, you gain 1 life."))
        skull = make_card("Batterskull", type_line="Artifact — Equipment",
                          mana_cost="{5}", cmc=5, power=None, toughness=None,
                          oracle_text="Living weapon\nEquipped creature gets "
                                      "+4/+4 and has vigilance and lifelink.\n"
                                      "Equip {3}")
        rick.hand.append(skull)
        claude_life_before = claude.life

        ok, msg, _ = asyncio.run(cast_spell_async(_engine(), game, rick, skull))

        assert ok is True, msg
        germ_events = [p for p in probe.received if p["via"] == "living_weapon"]
        assert len(germ_events) == 1
        assert germ_events[0]["card"].name == "Phyrexian Germ"
        assert claude.life == claude_life_before + 1


class TestSnowWatcher:
    def _slumber(self, make_card):
        return make_card(
            "Marit Lage's Slumber", type_line="Snow Enchantment",
            power=None, toughness=None,
            oracle_text="Whenever Marit Lage's Slumber or another snow "
                        "permanent you control enters, scry 1.")

    def _flooded_board(self, game, make_card):
        # 4+ lands makes the scry heuristic deterministic: a land on top of
        # the library gets bottomed.
        rick = game.players[0]
        rick.battlefield.extend(
            make_card(f"Snow Plains {i}", type_line="Snow Basic Land — Plains",
                      power="0", toughness="0") for i in range(4))
        return rick

    def test_snow_entry_scries_for_slumber_controller(self, rules, game, make_card):
        rick = self._flooded_board(game, make_card)
        rick.battlefield.append(self._slumber(make_card))
        top_land = make_card("Island", type_line="Basic Land — Island",
                             power="0", toughness="0")
        keeper = make_card("Mulldrifter")
        rick.library = [top_land, keeper]

        entering = make_card("Coldsteel Heart", type_line="Snow Artifact",
                             power=None, toughness=None,
                             oracle_text="Coldsteel Heart enters tapped.")
        rick.battlefield.append(entering)
        events.emit(events.PERMANENT_ENTERED, game, card=entering,
                    controller=rick, via="cast", rules=rules)

        # Scry 1 bottomed the flooded land; Mulldrifter now on top.
        assert rick.library[0] is keeper
        assert rick.library[-1] is top_land
        # Display rides pending messages and never names the scried card.
        pending = "".join(getattr(game, '_pending_messages', []) or [])
        assert "Marit Lage's Slumber" in pending
        assert "Island" not in pending

    def test_nonsnow_entry_does_not_scry(self, rules, game, make_card):
        rick = self._flooded_board(game, make_card)
        rick.battlefield.append(self._slumber(make_card))
        top_land = make_card("Island", type_line="Basic Land — Island",
                             power="0", toughness="0")
        rick.library = [top_land]

        entering = make_card("Charging Bear")
        rick.battlefield.append(entering)
        events.emit(events.PERMANENT_ENTERED, game, card=entering,
                    controller=rick, via="cast", rules=rules)

        assert rick.library[0] is top_land

    def test_opponents_snow_entry_does_not_scry(self, rules, game, make_card):
        rick = self._flooded_board(game, make_card)
        rick.battlefield.append(self._slumber(make_card))
        claude = game.players[1]
        top_land = make_card("Island", type_line="Basic Land — Island",
                             power="0", toughness="0")
        rick.library = [top_land]

        entering = make_card("Snow-Covered Swamp",
                             type_line="Snow Basic Land — Swamp",
                             power="0", toughness="0")
        claude.battlefield.append(entering)
        events.emit(events.PERMANENT_ENTERED, game, card=entering,
                    controller=claude, via="land_drop", rules=rules)

        assert rick.library[0] is top_land

    def test_slumbers_own_entry_is_not_double_counted(self, rules, game, make_card):
        # Slumber's OWN entry scry is the ETB pattern's job at cast time —
        # the watcher covers only OTHER snow permanents.
        rick = self._flooded_board(game, make_card)
        top_land = make_card("Island", type_line="Basic Land — Island",
                             power="0", toughness="0")
        rick.library = [top_land]

        slumber = self._slumber(make_card)
        rick.battlefield.append(slumber)
        events.emit(events.PERMANENT_ENTERED, game, card=slumber,
                    controller=rick, via="cast", rules=rules)

        assert rick.library[0] is top_land


# (Slice 2c, July 24, 2026: TestEnteredParity was deleted with the parity
# recorder it covered — two clean batches at [EVENT-PARITY]=0. The dispatch
# behavior itself is pinned in tests/test_slice2b_bus_dispatch.py.)


class TestDeathSaveWatcherScan:
    def test_undying_return_triggers_soul_warden(self, game, make_card):
        # CR 603.6a: the undying return is a NEW entry — Soul Warden must
        # gain life for it. Found via the slice-2 parity recorder; fixed as
        # step 4 of _finalize_death_save_return using the NARROW watcher
        # scan (the broad _handle_etb_triggers would double-fire the
        # self-ETB that step 3 already resolves).
        from mtg.actions import execute_action_on_state

        engine = _engine()
        rules = engine.rules
        claude = game.players[1]
        warden = make_card(
            "Soul Warden", power="1", toughness="1",
            oracle_text="Whenever another creature enters, you gain 1 life.")
        claude.battlefield.append(warden)
        messenger = make_card(
            "Geralf's Messenger", keywords=["Undying"],
            power="3", toughness="2", oracle_text="Undying")
        claude.battlefield.append(messenger)
        life_before = claude.life

        execute_action_on_state(rules, game, {
            "action": "destroy", "card": "Geralf's Messenger"})

        assert messenger in claude.battlefield
        assert messenger.counters.get("+1/+1", 0) == 1
        assert claude.life == life_before + 1

    # (Slice 2c: the parity-recorder companion test was deleted with the
    # recorder; test_undying_return_triggers_soul_warden above pins the
    # CR 603.6a behavior the recorder originally caught.)


class TestNoncastFunnelEnchantmentWatchers:
    def test_flickered_enchantment_reaches_constellation_watcher(
            self, game, make_card):
        # The June 10 B9 fix covered the CAST path only; enchantments
        # entering via the noncast funnel (move_card / mass_flicker) skipped
        # Eidolon of Blossoms. Found while wiring the parity recorder.
        from mtg.triggers import _handle_etb_triggers

        engine = _engine()
        rick = game.players[0]
        eidolon = make_card(
            "Eidolon of Blossoms", type_line="Enchantment Creature — Spirit",
            power="2", toughness="2",
            oracle_text="Constellation — Whenever Eidolon of Blossoms or "
                        "another enchantment you control enters, draw a card.")
        rick.battlefield.append(eidolon)
        rick.library = [make_card("Forest", type_line="Basic Land — Forest",
                                  power="0", toughness="0")]
        hand_before = len(rick.hand)

        entering = make_card("Omen of the Sea", type_line="Enchantment",
                             power=None, toughness=None,
                             oracle_text="Flash")
        rick.battlefield.append(entering)
        # Slice 2b (July 21): the funnel no longer runs the watcher scans
        # itself — every production caller emits PERMANENT_ENTERED first
        # (the subscriber does the dispatch) and the funnel drains the
        # display lines. Mirror that contract here.
        from mtg import events
        events.emit(events.PERMANENT_ENTERED, game, card=entering,
                    controller=rick, via="move_card", rules=engine.rules)
        _handle_etb_triggers(engine, game, rick, entering)

        assert len(rick.hand) == hand_before + 1
