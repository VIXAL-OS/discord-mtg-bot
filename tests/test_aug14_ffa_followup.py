"""Regressions from the first live four-seat cube FFA smoke."""

import asyncio
from types import SimpleNamespace

from conftest import _make_card
from mtg.combat import resolve_combat_damage
from mtg.constants import Phase
from mtg.engine import GameEngine
from mtg.judge import describe_game_for_judge, resolve_effect
from mtg.models import GameState, Player
from mtg.rules_engine import RulesEngine
from mtg.triggers import has_battlefield_upkeep_trigger


LIGHTNING_RUNNER_TEXT = (
    "Double strike, haste\n"
    "Whenever this creature attacks, you get {E}{E} (two energy counters), "
    "then you may pay eight {E}. If you pay, untap all creatures you "
    "control, and after this phase, there is an additional combat phase."
)

FIREHEART_TEXT = (
    "{1}{R}{R}: Put a blaze counter on target land without a blaze counter "
    "on it. For as long as that land has a blaze counter on it, it has "
    '"At the beginning of your upkeep, this land deals 1 damage to you."'
)


def _game(player_count=4, *, experimental=True):
    players = [
        Player(name=chr(ord("A") + index), life=20,
               seat_id=index, user_id=1000 + index)
        for index in range(player_count)
    ]
    return GameState(
        thread_id=81400 + player_count,
        format="limited",
        players=players,
        active_player_index=0,
        turn_number=1,
        experimental_ffa=experimental,
    )


def _stock_libraries(game, count=12):
    for player_index, player in enumerate(game.players):
        player.library = [
            _make_card(f"{player.name} Library {index}",
                       owner_index=player_index)
            for index in range(count)
        ]


def test_turn_one_draw_skip_is_two_player_only():
    multiplayer = _game()
    multiplayer_engine = GameEngine(None)
    _stock_libraries(multiplayer)
    multiplayer_engine.start_game(multiplayer, 0)
    multiplayer.set_phase(Phase.UPKEEP, via="test")

    _phase, multiplayer_messages = multiplayer_engine.advance_phase(multiplayer)

    assert len(multiplayer.players[0].hand) == 8
    assert len(multiplayer.players[0].library) == 4
    assert any("draws a card" in message for message in multiplayer_messages)
    assert multiplayer.players[0].has_drawn_for_turn is False

    duel = _game(2, experimental=False)
    duel_engine = GameEngine(None)
    _stock_libraries(duel)
    duel_engine.start_game(duel, 0)
    duel.set_phase(Phase.UPKEEP, via="test")

    _phase, duel_messages = duel_engine.advance_phase(duel)

    assert len(duel.players[0].hand) == 7
    assert len(duel.players[0].library) == 5
    assert not any("draws a card" in message for message in duel_messages)
    assert duel.players[0].has_drawn_for_turn is True


def test_eliminated_defender_does_not_redirect_regular_damage():
    game = _game()
    rules = RulesEngine(None)
    attacker = _make_card(
        "Double Striker", owner_index=0, power="5", toughness="5",
        oracle_text="Double strike", attacking=True, attacking_player=1,
    )
    game.players[0].battlefield.append(attacker)
    game.players[1].life = 5
    game.attackers = [attacker.id]

    messages = resolve_combat_damage(rules, game)

    assert game.players[1].eliminated is True
    assert [player.life for player in game.players[2:]] == [20, 20]
    assert sum("Double Striker deals 5 damage" in message
               for message in messages) == 1
    assert attacker.id not in game.attackers
    assert attacker.attacking is False
    assert attacker.attacking_player is None


def test_explicit_dead_defender_never_falls_back_to_another_opponent():
    game = _game()
    attacker = _make_card(
        "Assigned Attacker", owner_index=0,
        attacking=True, attacking_player=1,
    )
    game.players[0].battlefield.append(attacker)
    game.players[1].eliminated = True

    assert game.defender_for(attacker) is None


def test_judge_prompt_lists_all_and_only_living_opponents():
    game = _game()
    game.players[1].eliminated = True
    game.players[1].loss_reason = "zero life"
    rules = RulesEngine(None)
    captured = {}

    class Messages:
        def create(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                content=[SimpleNamespace(
                    type="text",
                    text='{"explanation":"No action.","actions":[]}',
                )],
                usage=None,
            )

    rules.client = SimpleNamespace(messages=Messages())

    asyncio.run(resolve_effect(
        rules, game, "Create a token for each opponent.",
        source_card="Goblin Goliath", controller="A",
    ))

    prompt = captured["messages"][0]["content"]
    assert "Living opponent(s): C, D" in prompt
    assert 'exact living player names as shown: "A", "C", "D"' in prompt
    assert '"Each opponent" means every player in Living opponent(s)' in prompt
    state = describe_game_for_judge(rules, game)
    assert "B (Player 2, ELIMINATED)" in state
    assert "C (Player 3, LIVING)" in state


def test_lightning_runner_accumulates_then_pays_for_extra_combat():
    game = _game()
    engine = GameEngine(None)
    controller = game.players[0]
    controller.energy = 6
    runner = _make_card(
        "Lightning Runner", owner_index=0, power="5", toughness="5",
        oracle_text=LIGHTNING_RUNNER_TEXT, tapped=True,
        attacking=True, attacking_player=1,
    )
    twin_one = _make_card("Twin", owner_index=0, tapped=True)
    twin_two = _make_card("Twin", owner_index=0, tapped=True)
    controller.battlefield.extend([runner, twin_one, twin_two])
    game.attackers = [runner.id]

    messages, unhandled = engine._check_attack_triggers_sync(
        game, runner, controller)

    assert unhandled == []
    assert controller.energy == 0
    assert game._additional_combats == 1
    assert not runner.tapped and not twin_one.tapped and not twin_two.tapped
    assert any("gets 2 energy" in message for message in messages)
    assert any("pays 8 energy" in message for message in messages)


def test_lightning_runner_below_eight_does_not_untap_or_add_combat():
    game = _game()
    engine = GameEngine(None)
    controller = game.players[0]
    controller.energy = 5
    runner = _make_card(
        "Lightning Runner", owner_index=0,
        oracle_text=LIGHTNING_RUNNER_TEXT, tapped=True,
        attacking=True, attacking_player=1,
    )
    controller.battlefield.append(runner)
    game.attackers = [runner.id]

    _messages, unhandled = engine._check_attack_triggers_sync(
        game, runner, controller)

    assert unhandled == []
    assert controller.energy == 7
    assert game._additional_combats == 0
    assert runner.tapped is True


def test_quoted_granted_upkeep_ability_is_not_source_trigger():
    assert has_battlefield_upkeep_trigger(FIREHEART_TEXT) is False
    assert has_battlefield_upkeep_trigger(
        "At the beginning of your upkeep, draw a card.") is True
    assert has_battlefield_upkeep_trigger(
        "Cumulative upkeep {1} (At the beginning of your upkeep, put an age "
        "counter on this permanent, then sacrifice it unless you pay its "
        "upkeep cost for each age counter on it.)") is True

    game = _game()
    engine = GameEngine(None)
    fireheart = _make_card(
        "Obsidian Fireheart", owner_index=0,
        oracle_text=FIREHEART_TEXT,
    )
    game.active_player.battlefield.append(fireheart)

    messages, unhandled = engine._check_upkeep_triggers_sync(game)

    assert messages == []
    assert unhandled == []
