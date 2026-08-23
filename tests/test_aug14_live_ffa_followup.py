"""Pins for the Aug-14 live FFA run at SHA 3745a54."""

from conftest import _make_card
from mtg.actions import _fire_noncast_battlefield_entry
from mtg.engine import GameEngine
from mtg.models import GameState, Player


INFERNO_ORACLE = (
    "{R}: This creature gets +1/+0 until end of turn.\n"
    "Whenever this creature enters or attacks, it deals 3 damage divided "
    "as you choose among one, two, or three targets."
)

PHYLATH_ORACLE = (
    "When Phylath enters, create a 0/1 green Plant creature token for each "
    "basic land you control.\nLandfall — Whenever a land you control "
    "enters, put four +1/+1 counters on target Plant you control."
)


def _ffa_game():
    players = [
        Player(name=name, life=20, seat_id=index, user_id=81400 + index)
        for index, name in enumerate(("A", "B", "C", "D"))
    ]
    return GameState(
        thread_id=1537874009453240366,
        format="limited",
        players=players,
        active_player_index=3,
        experimental_ffa=True,
    )


def _engine(game):
    engine = GameEngine(None)
    game._rules_engine = engine.rules
    engine.rules.engine_ref = engine
    return engine


def test_noncast_etb_uses_living_ffa_opponent_not_eliminated_first_seat():
    game = _ffa_game()
    engine = _engine(game)
    eliminated, low_life, other, controller = game.players
    eliminated.eliminated = True
    eliminated.life = 0
    low_life.life = 5
    other.life = 12
    titan = _make_card(
        "Inferno Titan", owner_index=3, power="6", toughness="6",
        oracle_text=INFERNO_ORACLE)
    controller.battlefield.append(titan)

    messages = _fire_noncast_battlefield_entry(
        engine.rules, game, controller, titan)

    assert eliminated.life == 0
    assert low_life.life == 2
    assert other.life == 12
    assert any("Inferno Titan" in msg and "B" in msg and "3" in msg
               for msg in messages)


def test_noncast_etb_with_no_living_opponent_never_retargets_dead_seat():
    game = _ffa_game()
    engine = _engine(game)
    controller = game.players[3]
    for opponent in game.players[:3]:
        opponent.eliminated = True
        opponent.life = 0
    titan = _make_card(
        "Inferno Titan", owner_index=3, power="6", toughness="6",
        oracle_text=INFERNO_ORACLE)
    controller.battlefield.append(titan)

    messages = _fire_noncast_battlefield_entry(
        engine.rules, game, controller, titan)

    assert [player.life for player in game.players] == [0, 0, 0, 20]
    assert not any("deals 3 damage" in msg for msg in messages)


def _phylath_board(*, shrouded=False, doubling=False):
    game = _ffa_game()
    engine = _engine(game)
    controller = game.players[0]
    phylath = _make_card(
        "Phylath, World Sculptor", owner_index=0, power="5", toughness="5",
        oracle_text=PHYLATH_ORACLE)
    small = _make_card(
        "Plant", owner_index=0, type_line="Creature Token — Plant",
        power="0", toughness="1")
    best = _make_card(
        "Plant", owner_index=0, type_line="Creature Token — Plant",
        power="2", toughness="2", keywords=["Shroud"] if shrouded else [])
    land = _make_card(
        "Forest", owner_index=0, type_line="Basic Land — Forest",
        oracle_text="{T}: Add {G}.", power=None, toughness=None)
    controller.battlefield.extend([phylath, small, best, land])
    if doubling:
        season = _make_card(
            "Doubling Season", owner_index=0, type_line="Enchantment",
            oracle_text=(
                "If an effect would create one or more tokens under your "
                "control, it creates twice that many of those tokens instead. "
                "If an effect would put one or more counters on a permanent "
                "you control, it puts twice that many of those counters on "
                "that permanent instead."),
            power=None, toughness=None)
        controller.battlefield.append(season)
        game.register_replacement_effects(season, controller.name)
    return game, engine, controller, small, best, land


def test_phylath_puts_four_counters_on_one_target_plant_only():
    game, engine, controller, small, best, land = _phylath_board()

    messages = engine._handle_land_etb(game, controller, land)

    assert small.counters.get('+1/+1', 0) == 0
    assert best.counters.get('+1/+1', 0) == 4
    assert any("gets 4 +1/+1 counter" in msg for msg in messages)


def test_phylath_avoids_illegal_shrouded_target_and_honors_doubling():
    game, engine, controller, small, shrouded_best, land = _phylath_board(
        shrouded=True, doubling=True)

    engine._handle_land_etb(game, controller, land)

    assert shrouded_best.counters.get('+1/+1', 0) == 0
    assert small.counters.get('+1/+1', 0) == 8
