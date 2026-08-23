"""Pins for the second Aug-14 live FFA run at SHA af2016e."""

import asyncio

from conftest import _make_card
from mtg.autoplay import _autoplay_execute_action
from mtg.constants import Phase
from mtg.engine import GameEngine
from mtg.models import GameState, Player
from rules.effect_templates import build_game_context, get_effect_library


def _game(player_count=4, *, experimental=True):
    players = [
        Player(name=chr(ord('A') + index), life=20,
               seat_id=index, user_id=81450 + index)
        for index in range(player_count)
    ]
    return GameState(
        thread_id=1537881091866886225,
        format="limited",
        players=players,
        active_player_index=0,
        turn_number=1,
        experimental_ffa=experimental,
    )


def _engine(game):
    engine = GameEngine(None)
    game._rules_engine = engine.rules
    engine.rules.engine_ref = engine
    return engine


def _creature(name, owner_index, power="2", toughness="2", **kwargs):
    return _make_card(
        name, owner_index=owner_index, power=power, toughness=toughness,
        type_line=kwargs.pop("type_line", "Creature — Test"), **kwargs)


def test_fleshbag_named_template_sacrifices_one_creature_for_every_living_seat():
    game = _game()
    engine = _engine(game)
    controller = game.players[0]
    fleshbag = _creature(
        "Fleshbag Marauder", 0, power="3", toughness="1",
        oracle_text=(
            "When this creature enters, each player sacrifices a creature "
            "of their choice."))
    spare = _creature("Controller Spare", 0, power="1")
    victims = [_creature(f"Victim {index}", index, power=str(index + 1))
               for index in range(1, 4)]
    controller.battlefield.extend([fleshbag, spare])
    for index, victim in enumerate(victims, start=1):
        game.players[index].battlefield.append(victim)

    opponent = game.default_opponent_for(controller)
    actions, _ = get_effect_library().resolve_etb(
        fleshbag.name, fleshbag.oracle_text, controller.name, opponent.name,
        build_game_context(game, controller, opponent, card=fleshbag))
    for action in actions:
        engine.rules._execute_action_on_state(game, action)

    assert fleshbag in controller.battlefield
    assert spare in controller.graveyard
    assert all(victim in game.players[index].graveyard
               for index, victim in enumerate(victims, start=1))


def test_fleshbag_ignores_eliminated_seat_and_source_is_only_choice_control():
    game = _game()
    engine = _engine(game)
    controller = game.players[0]
    fleshbag = _creature(
        "Fleshbag Marauder", 0, power="3", toughness="1",
        oracle_text=(
            "When this creature enters, each player sacrifices a creature "
            "of their choice."))
    controller.battlefield.append(fleshbag)
    for index in range(1, 4):
        game.players[index].battlefield.append(
            _creature(f"Victim {index}", index))
    game.players[3].eliminated = True
    eliminated_victim = game.players[3].battlefield[0]

    opponent = game.default_opponent_for(controller)
    actions, _ = get_effect_library().resolve_etb(
        fleshbag.name, fleshbag.oracle_text, controller.name, opponent.name,
        build_game_context(game, controller, opponent, card=fleshbag))
    for action in actions:
        engine.rules._execute_action_on_state(game, action)

    assert fleshbag in controller.graveyard
    assert eliminated_victim in game.players[3].battlefield


SELVALA_ORACLE = (
    "Whenever another creature enters, its controller may draw a card if "
    "its power is greater than each other creature's power.\n"
    "{G}, {T}: Add X mana in any combination of colors, where X is the "
    "greatest power among creatures you control."
)


def test_selvala_compares_global_board_and_includes_herself():
    # Tie with Selvala: no draw.
    game = _game()
    engine = _engine(game)
    selvala = _creature(
        "Selvala, Heart of the Wilds", 0, power="2", toughness="3",
        oracle_text=SELVALA_ORACLE)
    entrant = _creature("Eternal Witness", 2, power="2", toughness="1")
    game.players[0].battlefield.append(selvala)
    game.players[2].battlefield.append(entrant)
    game.players[2].library.append(_make_card("Would-be draw", owner_index=2))

    messages, _ = engine._check_creature_etb_triggers_sync(
        game, game.players[2], entrant)

    assert len(game.players[2].hand) == 0
    assert len(game.players[2].library) == 1
    assert not any("draws a card" in message for message in messages)

    # A larger creature on another seat also blocks the draw.
    game.players[3].battlefield.append(
        _creature("Stonehoof Chieftain", 3, power="8", toughness="8"))
    hellraiser = _creature(
        "Capricious Hellraiser", 2, power="4", toughness="4")
    game.players[2].battlefield.append(hellraiser)
    messages, _ = engine._check_creature_etb_triggers_sync(
        game, game.players[2], hellraiser)
    assert len(game.players[2].hand) == 0
    assert not any("draws a card" in message for message in messages)


def test_selvala_awards_legal_draw_to_entering_creatures_controller():
    game = _game()
    engine = _engine(game)
    selvala = _creature(
        "Selvala, Heart of the Wilds", 0, power="2", toughness="3",
        oracle_text=SELVALA_ORACLE)
    entrant = _creature("Primeval Titan", 2, power="6", toughness="6")
    game.players[0].battlefield.append(selvala)
    game.players[2].battlefield.append(entrant)
    drawn = _make_card("Entrant controller draw", owner_index=2)
    game.players[2].library.append(drawn)

    messages, _ = engine._check_creature_etb_triggers_sync(
        game, game.players[2], entrant)

    assert drawn in game.players[2].hand
    assert game.players[0].hand == []
    assert any("C draws a card" in message for message in messages)


def test_lotus_cobra_adds_needed_colored_mana_and_opponent_control_is_adverse():
    game = _game()
    engine = _engine(game)
    controller = game.players[0]
    cobra = _creature(
        "Lotus Cobra", 0, mana_cost="{1}{G}", power="2", toughness="1",
        oracle_text=(
            "Landfall — Whenever a land enters the battlefield under your "
            "control, add one mana of any color."),
        color_identity=["G"])
    controller.battlefield.append(cobra)
    controller.hand.append(_make_card(
        "White spell", owner_index=0, mana_cost="{W}{W}",
        type_line="Sorcery", power=None, toughness=None))
    land = _make_card(
        "Forest", owner_index=0, type_line="Basic Land — Forest",
        oracle_text="{T}: Add {G}.", power=None, toughness=None)
    controller.battlefield.append(land)

    messages = engine._handle_land_etb(game, controller, land)

    assert controller.mana_pool["W"] == 1
    assert sum(controller.mana_pool.values()) == 1
    assert any("Adds {W}" in message for message in messages)

    # A Cobra controlled by another player does not see this landfall.
    other_cobra = _creature(
        "Lotus Cobra", 1, oracle_text=cobra.oracle_text,
        color_identity=["G"])
    game.players[1].battlefield.append(other_cobra)
    before = dict(game.players[1].mana_pool)
    second_land = _make_card(
        "Plains", owner_index=0, type_line="Basic Land — Plains",
        oracle_text="{T}: Add {W}.", power=None, toughness=None)
    controller.battlefield.remove(cobra)
    controller.battlefield.append(second_land)
    engine._handle_land_etb(game, controller, second_land)
    assert game.players[1].mana_pool == before


def test_autoplay_lowercase_x_is_exact_not_silently_maximized():
    game = _game(2, experimental=False)
    game.phase = Phase.MAIN1
    engine = _engine(game)
    caster, target = game.players
    for index in range(4):
        caster.battlefield.append(_make_card(
            f"Mountain {index}", owner_index=0,
            type_line="Basic Land — Mountain", oracle_text="{T}: Add {R}.",
            power=None, toughness=None))
    storm = _make_card(
        "Comet Storm", owner_index=0, mana_cost="{X}{R}{R}", cmc=2,
        type_line="Instant", power=None, toughness=None,
        oracle_text=(
            "Multikicker {1}\nChoose any target, then choose another target "
            "for each time this spell was kicked. Comet Storm deals X damage "
            "to each of them."))
    caster.hand.append(storm)

    class Cog:
        def __init__(self):
            self.engine = engine
            self.sent = []

        async def _autoplay_send(self, _thread, message):
            self.sent.append(message)

    result = asyncio.run(_autoplay_execute_action(
        Cog(), None, game, 0, {
            "type": "cast", "card": "Comet Storm",
            "target_player_id": 1, "x": 1,
        }))

    assert result is not None
    assert storm._x_value == 1
    assert target.life == 19
