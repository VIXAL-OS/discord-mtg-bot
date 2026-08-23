"""Pins for the Aug-16 live FFA follow-up at SHA 9531045."""

import asyncio

from conftest import _make_card
from mtg.autoplay import _coalesce_repeated_x_target_casts
from mtg.constants import Phase
from mtg.engine import GameEngine
from mtg.models import GameState, Player


def _game():
    players = [
        Player(name=name, life=20, seat_id=index, user_id=17000 + index)
        for index, name in enumerate(("Caster", "One", "Two", "Three"))
    ]
    game = GameState(
        thread_id=1538631519546118237,
        format="limited",
        players=players,
        active_player_index=0,
        turn_number=1,
        experimental_ffa=True,
    )
    game.phase = Phase.MAIN1
    return game


def _curse():
    return _make_card(
        "Curse of the Swine", owner_index=0, mana_cost="{X}{U}{U}",
        cmc=2, type_line="Sorcery", power=None, toughness=None,
        oracle_text=(
            "Exile X target creatures. For each creature exiled this way, "
            "its controller creates a 2/2 green Boar creature token."),
    )


def _creature(name, owner_index, power="2"):
    return _make_card(
        name, owner_index=owner_index, type_line="Creature — Test",
        power=power, toughness="2")


def _island(index):
    return _make_card(
        f"Island {index}", owner_index=0, type_line="Basic Land — Island",
        oracle_text="{T}: Add {U}.", power=None, toughness=None)


def _engine(game):
    engine = GameEngine(None)
    game._rules_engine = engine.rules
    engine.rules.engine_ref = engine
    return engine


def test_repeated_x_target_cast_declarations_coalesce_before_execution():
    game = _game()
    game.players[0].hand.append(_curse())
    plan = [
        {"type": "cast", "card": "Curse of the Swine",
         "target": "Oracle of Mul Daya"},
        {"type": "cast", "card": "Curse of the Swine",
         "target": "Wall of Omens"},
        {"type": "pass"},
    ]

    normalized = _coalesce_repeated_x_target_casts(game.players[0], plan)

    assert normalized == [
        {"type": "cast", "card": "Curse of the Swine", "x": 2,
         "target": ["Oracle of Mul Daya", "Wall of Omens"]},
        {"type": "pass"},
    ]


def test_curse_of_swine_routes_each_ffa_target_and_boar_to_its_controller():
    game = _game()
    engine = _engine(game)
    caster = game.players[0]
    caster.battlefield.extend(_island(index) for index in range(4))
    curse = _curse()
    curse._x_value = 2
    caster.hand.append(curse)
    oracle = _creature("Oracle of Mul Daya", 1, power="2")
    wall = _creature("Wall of Omens", 2, power="0")
    unrelated = _creature("Unrelated", 3, power="9")
    game.players[1].battlefield.append(oracle)
    game.players[2].battlefield.append(wall)
    game.players[3].battlefield.append(unrelated)

    ok, _message, _effects = asyncio.run(engine.cast_spell_async(
        game, caster, curse, target=[oracle, wall]))

    assert ok is True
    assert oracle in game.players[1].exile
    assert wall in game.players[2].exile
    assert unrelated in game.players[3].battlefield
    assert [card.name for card in game.players[1].battlefield] == ["Boar"]
    assert [card.name for card in game.players[2].battlefield] == ["Boar"]
    assert not any(card.name == "Boar" for card in game.players[3].battlefield)
    assert len(game.resolution_jobs) == 1
    assert next(iter(game.resolution_jobs.values())).checkpoint == "complete"


def test_curse_x_is_clamped_to_one_declared_target_before_payment():
    game = _game()
    engine = _engine(game)
    caster = game.players[0]
    lands = [_island(index) for index in range(5)]
    caster.battlefield.extend(lands)
    curse = _curse()
    caster.hand.append(curse)
    target = _creature("Only Target", 1)
    decoy = _creature("Must Stay", 2, power="8")
    game.players[1].battlefield.append(target)
    game.players[2].battlefield.append(decoy)

    ok, _message, _effects = asyncio.run(engine.cast_spell_async(
        game, caster, curse, target=target))

    assert ok is True
    assert curse._x_value == 1
    assert sum(card.tapped for card in lands) == 3
    assert target in game.players[1].exile
    assert decoy in game.players[2].battlefield
    assert sum(card.name == "Boar" for card in game.players[1].battlefield) == 1
    assert next(iter(game.resolution_jobs.values())).checkpoint == "complete"


def test_curse_uses_stable_seats_when_display_names_collide():
    game = _game()
    for player in game.players:
        player.name = "Duplicate"
    engine = _engine(game)
    caster = game.players[0]
    caster.battlefield.extend(_island(index) for index in range(4))
    curse = _curse()
    curse._x_value = 2
    caster.hand.append(curse)
    first = _creature("Bear", 1)
    second = _creature("Bear", 2)
    decoy = _creature("Bear", 3)
    game.players[1].battlefield.append(first)
    game.players[2].battlefield.append(second)
    game.players[3].battlefield.append(decoy)

    ok, _message, _effects = asyncio.run(engine.cast_spell_async(
        game, caster, curse, target=[first, second]))

    assert ok is True
    assert first in game.players[1].exile
    assert second in game.players[2].exile
    assert decoy in game.players[3].battlefield
    assert [c.name for c in game.players[1].battlefield] == ["Boar"]
    assert [c.name for c in game.players[2].battlefield] == ["Boar"]
    assert [c.name for c in game.players[3].battlefield] == ["Bear"]
