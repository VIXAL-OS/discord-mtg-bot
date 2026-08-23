"""Behavioral contracts for the bounded four-seat cube FFA foundation."""

import asyncio
from types import MethodType, SimpleNamespace

from conftest import _make_card
from cube_draft import CubeDraftCog, DraftSeat, build_all_drafted_decks
from mtg.actions import execute_action_on_state
from mtg.autoplay import (
    _autoplay_assign_multiplayer_blocks,
    _autoplay_human_turn,
    _format_autoplay_attacker,
)
from mtg.combat import resolve_combat_damage
from mtg.constants import Phase
from mtg.engine import GameEngine
from mtg.helpers import apnap_order_died, resolve_cast_target
from mtg.models import Card, GameState, Player
from mtg.spells import cast_spell_async
from rules.priority import PriorityAction, PrioritySystem


def _ffa_game(life=20):
    players = [
        Player(name=name, life=life, seat_id=index, user_id=1000 + index)
        for index, name in enumerate(("A", "B", "C", "D"))
    ]
    game = GameState(
        thread_id=414, format="limited", players=players,
        active_player_index=0, turn_number=1, experimental_ffa=True,
    )
    return game


def _duel_game(life=20):
    players = [
        Player(name=name, life=life, seat_id=index, user_id=2000 + index)
        for index, name in enumerate(("A", "B"))
    ]
    return GameState(
        thread_id=415, format="limited", players=players,
        active_player_index=0, turn_number=1,
    )


def _seats():
    seats = []
    for seat_index in range(8):
        pool = [
            _make_card(
                f"Seat {seat_index} Card {card_index}",
                mana_cost="{G}", type_line="Creature — Elf",
                power="2", toughness="2",
            )
            for card_index in range(45)
        ]
        seats.append(DraftSeat(
            seat_index=seat_index, name=f"Seat {seat_index + 1}", pool=pool))
    build_all_drafted_decks(seats)
    return seats


def test_stable_seats_cycle_past_eliminated_player_and_round_trip():
    game = _ffa_game()
    game.players[1].eliminated = True
    game.players[1].loss_reason = "test loss"
    game.elimination_order = [1]
    attacker = Card(name="Attacker", id="attacker", type_line="Creature",
                    power="2", toughness="2", owner_index=0,
                    attacking=True, attacking_player=2)
    game.players[0].battlefield.append(attacker)
    game.attackers = [attacker.id]

    assert game.next_living_player_index(0) == 2
    assert [player.name for player in game.living_players_in_turn_order(2)] == [
        "C", "D", "A"]

    restored = GameState.from_dict(game.to_dict())
    assert [player.seat_id for player in restored.players] == [0, 1, 2, 3]
    assert restored.players[1].eliminated is True
    assert restored.elimination_order == [1]
    assert restored.players[0].battlefield[0].attacking_player == 2
    visible = restored.visible_state(0)
    assert visible["players"][1]["eliminated"] is True
    assert visible["elimination_order"] == [1]


def test_cyclic_apnap_placement_and_reverse_resolution_are_four_seat():
    game = _ffa_game()
    game.active_player_index = 2
    pairs = [(f"dead-{player.name}", player) for player in game.players]

    assert game.apnap_player_indices() == [2, 3, 0, 1]
    assert game.apnap_player_indices(resolution_order=True) == [1, 0, 3, 2]
    assert [pair[1].name for pair in apnap_order_died(pairs, game)] == [
        "B", "A", "D", "C"]


def test_priority_rotates_all_seats_and_response_resets_passes():
    async def exercise():
        resolved = []

        async def on_resolve(obj):
            resolved.append(obj.name)

        priority = PrioritySystem(
            ["A", "B", "C", "D"], auto_pass_seconds=0,
            on_stack_resolve=on_resolve)
        assert (await priority.player_action(
            "A", PriorityAction.pass_priority()))["priority_holder"] == "B"
        await priority.player_action("B", PriorityAction.cast("Response"))
        assert priority._passes_in_succession == []
        assert priority.priority_holder == "B"
        for player, next_holder in (("B", "C"), ("C", "D"),
                                    ("D", "A")):
            result = await priority.player_action(
                player, PriorityAction.pass_priority())
            assert result["priority_holder"] == next_holder
        await priority.player_action("A", PriorityAction.pass_priority())
        assert resolved == ["Response"]
        assert priority.priority_holder == "A"
        assert priority._passes_in_succession == []

    asyncio.run(exercise())


def test_start_game_syncs_random_first_seat_into_priority_system():
    engine = GameEngine(None)
    game = _ffa_game()
    for player in game.players:
        player.library = [_make_card(f"{player.name}-{n}") for n in range(10)]
    engine.setup_stack(game, auto_pass_seconds=0)

    engine.start_game(game, first_player_index=2)

    assert game.active_player is game.players[2]
    assert game._priority_system.active_player == "seat:2"
    assert game._priority_system.priority_holder == "seat:2"


def test_elimination_removes_owned_objects_returns_borrowed_and_continues():
    game = _ffa_game()
    owned_by_b = _make_card("B-owned", owner_index=1)
    borrowed_by_b = _make_card("A-owned", owner_index=0)
    b_card_in_c_exile = _make_card("B-exiled", owner_index=1)
    a_card_in_b_exile = _make_card("A-exiled", owner_index=0)
    game.players[2].battlefield.append(owned_by_b)
    game.players[1].battlefield.append(borrowed_by_b)
    game.players[2].exile.append(b_card_in_c_exile)
    game.players[1].exile.append(a_card_in_b_exile)
    game.stack = [{"controller_index": 1, "controller_name": "B"}]
    game.pending_async_triggers = [{"controller_name": "B"}]
    game.turn_effects = [{"controller": "B"}]
    game.delayed_triggers = [{"controller": 1}]
    game.conditional_exile_casts = {
        b_card_in_c_exile.id: {"controller_index": 1}
    }
    game._priority_system = PrioritySystem(["A", "B", "C", "D"])
    game._priority_system.stack = [SimpleNamespace(controller="B")]
    game._priority_system.priority_holder = "B"
    game.attackers = [owned_by_b.id]
    owned_by_b.attacking_player = 3

    messages = game.eliminate_player(1, "zero life")

    assert any("eliminated" in message for message in messages)
    assert game.players[1].eliminated is True
    assert game.elimination_order == [1]
    assert borrowed_by_b in game.players[0].battlefield
    assert owned_by_b not in game.players[2].battlefield
    assert b_card_in_c_exile not in game.players[2].exile
    assert a_card_in_b_exile in game.players[0].exile
    assert game.stack == []
    assert game.pending_async_triggers == []
    assert game.turn_effects == []
    assert game.delayed_triggers == []
    assert game.conditional_exile_casts == {}
    assert game._priority_system.stack == []
    assert game._priority_system.players == ["A", "C", "D"]
    assert game._priority_system.priority_holder == "C"
    assert not game.ended
    assert game.living_player_indices() == [0, 2, 3]


def test_simultaneous_sba_losses_eliminate_both_before_winner_check(rules):
    game = _ffa_game()
    game.players[1].life = 0
    game.players[2].life = -3

    rules.process_state_based_actions(game)

    assert game.players[1].eliminated is True
    assert game.players[2].eliminated is True
    assert set(game.elimination_order) == {1, 2}
    assert game.living_player_indices() == [0, 3]
    assert not game.ended


def test_combat_splits_defenders_and_rejects_wrong_defender_blocker(rules):
    game = _ffa_game()
    attacker_b = _make_card("Attacks B", power="3", toughness="3",
                            owner_index=0, attacking=True,
                            attacking_player=1)
    attacker_c = _make_card("Attacks C", power="3", toughness="3",
                            owner_index=0, attacking=True,
                            attacking_player=2)
    b_blocker = _make_card("B Blocker", power="1", toughness="1",
                           owner_index=1)
    d_wrong_blocker = _make_card("D Wrong Blocker", power="9", toughness="9",
                                 owner_index=3)
    game.players[0].battlefield.extend([attacker_b, attacker_c])
    game.players[1].battlefield.append(b_blocker)
    game.players[3].battlefield.append(d_wrong_blocker)
    game.attackers = [attacker_b.id, attacker_c.id]
    game.blockers = {
        attacker_b.id: [b_blocker.id],
        attacker_c.id: [d_wrong_blocker.id],
    }

    messages = resolve_combat_damage(rules, game)

    assert game.players[1].life == 20
    assert game.players[2].life == 17
    assert game.players[3].life == 20
    assert d_wrong_blocker.damage_marked == 0
    assert any("Attacks C deals 3 damage to C" in message
               for message in messages)


def test_multiplayer_block_planner_is_called_once_per_attacked_defender():
    game = _ffa_game()
    game.active_player_index = 0
    attacker_b = _make_card("Attacks B", owner_index=0, attacking=True,
                            attacking_player=1)
    attacker_c = _make_card("Attacks C", owner_index=0, attacking=True,
                            attacking_player=2)
    blocker_b = _make_card("B Blocker", owner_index=1)
    blocker_c = _make_card("C Blocker", owner_index=2)
    game.players[0].battlefield.extend([attacker_b, attacker_c])
    game.players[1].battlefield.append(blocker_b)
    game.players[2].battlefield.append(blocker_c)
    game.attackers = [attacker_b.id, attacker_c.id]
    calls = []

    class FakeAI:
        async def decide_blocks(self, _game, defender_index, attackers):
            calls.append((defender_index, [attacker.id for attacker in attackers]))
            if defender_index == 1:
                # The cross-defender proposal must be ignored even though it
                # carries a blocker controlled by this defender.
                return {attacker_b.id: [blocker_b.id],
                        attacker_c.id: [blocker_b.id]}
            return {attacker_c.id: [blocker_c.id],
                    attacker_b.id: [blocker_c.id]}

    sent = []

    async def send(_thread, content=None, **_kwargs):
        sent.append(content)

    cog = SimpleNamespace(
        engine=SimpleNamespace(claude_ai=FakeAI()),
        _autoplay_send=send,
    )

    asyncio.run(_autoplay_assign_multiplayer_blocks(cog, object(), game))

    assert calls == [(1, [attacker_b.id]), (2, [attacker_c.id])]
    assert game.blockers == {
        attacker_b.id: [blocker_b.id],
        attacker_c.id: [blocker_c.id],
    }
    assert len(sent) == 2


def test_attack_tax_reads_only_the_assigned_defender(rules):
    game = _ffa_game()
    game.phase = Phase.DECLARE_ATTACKERS
    attacker_b = _make_card("Taxed Attacker", owner_index=0,
                            attacking_player=1)
    attacker_c = _make_card("Untaxed Attacker", owner_index=0,
                            attacking_player=2)
    propaganda = _make_card(
        "Propaganda", owner_index=1, type_line="Enchantment",
        oracle_text=("Creatures can't attack you unless their controller "
                     "pays {2} for each creature they control that's "
                     "attacking you."))
    game.players[0].battlefield.extend([attacker_b, attacker_c])
    game.players[1].battlefield.append(propaganda)

    taxed, taxed_reason = rules.can_attack_with(
        game, game.players[0], attacker_b)
    untaxed, _ = rules.can_attack_with(game, game.players[0], attacker_c)

    assert taxed is False
    assert "requires {2}" in taxed_reason
    assert untaxed is True


def test_each_opponent_fans_out_but_singular_and_named_targets_are_concrete(
        rules):
    game = _ffa_game()
    game._current_resolution_source = ("Group Slug", "A")
    game._default_opponent_index = 2

    message = execute_action_on_state(rules, game, {
        "action": "lose_life", "player": "each opponent", "amount": 2,
    })

    assert [player.life for player in game.players] == [20, 18, 18, 18]
    assert message.count("loses 2 life") == 3
    execute_action_on_state(rules, game, {
        "action": "extort_drain", "player": "A",
        "opponent": "each opponent", "amount": 1,
    })
    assert [player.life for player in game.players] == [23, 17, 17, 17]
    bolt = _make_card(
        "Bolt", type_line="Instant",
        oracle_text="Bolt deals 3 damage to any target.")
    assert resolve_cast_target(game, game.players[0], bolt, "opponent") \
        is game.players[2]
    assert resolve_cast_target(game, game.players[0], bolt, "D") \
        is game.players[3]


def test_each_opponent_edict_template_preserves_1v1_shape_and_fans_out_in_ffa(
        rules):
    from rules.effect_templates import build_game_context, get_effect_library

    lib = get_effect_library()
    game = _ffa_game()
    for index in (1, 2, 3):
        game.players[index].battlefield.append(
            _make_card(f"Victim {index}", owner_index=index))
    ctx = build_game_context(
        game, game.players[0], game.players[1],
        card=_make_card("Butcher of Malakir", owner_index=0))
    actions = lib._force_sacrifice_creature("A", "B", ctx)

    assert actions[0]["action"] == "edict_sacrifice"
    execute_action_on_state(rules, game, actions[0])
    assert [len(player.battlefield) for player in game.players] == [0, 0, 0, 0]


def test_end_turn_skips_dead_seat_without_renumbering():
    engine = GameEngine(None)
    game = _ffa_game()
    game.phase = Phase.MAIN2
    game.players[1].eliminated = True

    engine.end_turn(game)

    assert game.active_player_index == 2
    assert game.players[2].seat_id == 2
    assert len(game.players) == 4


def test_real_autoplay_turn_pipeline_cycles_all_four_idle_seats():
    async def exercise():
        engine = GameEngine(None)
        engine.games.clear()
        game = _ffa_game()
        game.phase = Phase.MAIN1
        game.started = True
        for player in game.players:
            player.hand = [_make_card(f"{player.name} Hand")]
            player.library = [
                _make_card(f"{player.name} Library {index}")
                for index in range(10)
            ]
        visited = []

        async def plan_turn(_game, player_index, call_source=None):
            visited.append((player_index, call_source))
            return [{"type": "pass"}]

        async def decide_attackers(_game, _player_index):
            return []

        async def send(_thread, content=None, **_kwargs):
            return None

        async def pending(_thread, _game):
            return None

        engine.claude_ai.plan_turn = plan_turn
        engine.claude_ai.decide_attackers = decide_attackers
        cog = SimpleNamespace(
            engine=engine,
            _autoplay_send=send,
            _autoplay_resolve_pending_action=pending,
        )
        active_order = []
        for _ in range(4):
            active_order.append(game.active_player_index)
            await _autoplay_human_turn(
                cog, object(), game, game.active_player_index)
            engine.end_turn(game)
        assert active_order == [0, 1, 2, 3]
        assert {player_index for player_index, _source in visited} == {0, 1, 2, 3}
        assert all(player.seat_id == index
                   for index, player in enumerate(game.players))

    asyncio.run(exercise())


def test_active_seat_eliminated_mid_plan_takes_no_more_turn_actions():
    async def exercise():
        engine = GameEngine(None)
        engine.games.clear()
        game = _ffa_game()
        game.phase = Phase.MAIN1
        game.started = True
        game.players[0].hand = [_make_card("Fatal setup")]
        executed = []

        async def plan_turn(_game, _player_index, call_source=None):
            return [
                {"type": "activate", "permanent": "Fatal setup"},
                {"type": "activate", "permanent": "Should never execute"},
            ]

        async def execute(_thread, _game, player_index, action):
            executed.append(action["permanent"])
            _game.eliminate_player(player_index, "lost during own action")
            return "eliminated"

        async def no_op(*_args, **_kwargs):
            return None

        engine.claude_ai.plan_turn = plan_turn
        engine.claude_ai.decide_attackers = no_op
        cog = SimpleNamespace(
            engine=engine,
            _autoplay_send=no_op,
            _autoplay_execute_action=execute,
            _autoplay_resolve_pending_action=no_op,
        )

        await _autoplay_human_turn(cog, object(), game, 0)

        assert executed == ["Fatal setup"]
        assert game.players[0].eliminated
        assert game.living_player_indices() == [1, 2, 3]

    asyncio.run(exercise())


def test_bounded_ffa_runner_uses_one_four_player_game_and_reports_result():
    cog = CubeDraftCog.__new__(CubeDraftCog)
    cog.engine = GameEngine(None)
    observed = []
    sent = []

    async def no_mulligan(_hand, _count):
        return False

    async def fake_turn(_thread, game, player_index):
        observed.append((len(game.players), player_index,
                         game.experimental_ffa,
                         tuple(game.living_player_indices())))
        victims = [index for index in game.living_player_indices()
                   if index != player_index]
        if victims:
            game.eliminate_player(victims[-1], "smoke elimination")

    async def fake_pending(_thread, _game):
        return None

    async def capture_send(_self, _thread, content=None, embed=None,
                           final=False):
        sent.append((content, embed, final))

    cog.engine.claude_ai.decide_mulligan = no_mulligan
    cog.game_cog = SimpleNamespace(
        _autoplay_human_turn=fake_turn,
        _autoplay_resolve_pending_action=fake_pending,
    )
    cog._autodraft_send = MethodType(capture_send, cog)
    thread = SimpleNamespace(id=141414)

    result = asyncio.run(cog._run_cube_ffa(
        thread, "Test Cube", "test", _seats(), max_turns=6))

    assert result["outcome"] == "ffa_win"
    assert result["winner"] is not None
    assert len(result["elimination_order"]) == 3
    assert all(row[0] == 4 and row[2] is True for row in observed)
    assert all(row[1] in row[3] for row in observed)
    assert any(content and "FFA board" in content
               for content, _embed, _final in sent)
    assert sent[-1][2] is True
    assert thread.id not in cog.engine.games


def test_guttersnipe_each_opponent_fans_out_in_ffa_and_preserves_duel():
    oracle = ("Whenever you cast an instant or sorcery spell, this creature "
              "deals 2 damage to each opponent.")
    spell = _make_card(
        "Test Instant", type_line="Instant", oracle_text="Draw a card.")

    game = _ffa_game()
    engine = GameEngine(None)
    game._rules_engine = engine.rules
    engine.rules.engine_ref = engine
    game.players[0].battlefield.append(_make_card(
        "Guttersnipe", type_line="Creature — Goblin Shaman",
        oracle_text=oracle, power="2", toughness="2"))

    asyncio.run(engine._check_cast_triggers(
        game, game.players[0], spell))

    assert [player.life for player in game.players] == [20, 18, 18, 18]

    duel = _duel_game()
    duel_engine = GameEngine(None)
    duel._rules_engine = duel_engine.rules
    duel_engine.rules.engine_ref = duel_engine
    duel.players[0].battlefield.append(_make_card(
        "Guttersnipe", type_line="Creature — Goblin Shaman",
        oracle_text=oracle, power="2", toughness="2"))

    asyncio.run(duel_engine._check_cast_triggers(
        duel, duel.players[0], spell))

    assert [player.life for player in duel.players] == [20, 18]


def test_other_deterministic_each_opponent_trigger_paths_share_fanout():
    game = _ffa_game()
    engine = GameEngine(None)
    game._rules_engine = engine.rules
    engine.rules.engine_ref = engine
    impact = _make_card(
        "Impact Tremors", type_line="Enchantment", power=None,
        toughness=None, oracle_text=(
            "Whenever a creature you control enters, Impact Tremors deals "
            "1 damage to each opponent."))
    entrant = _make_card("Goblin Token", owner_index=0)
    game.players[0].battlefield.extend([impact, entrant])

    engine._check_creature_etb_triggers_sync(
        game, game.players[0], entrant)

    assert [player.life for player in game.players] == [20, 19, 19, 19]

    konrad = _make_card(
        "Syr Konrad, the Grim", owner_index=0,
        type_line="Legendary Creature — Human Knight", power="5",
        toughness="4", oracle_text=(
            "Whenever another creature dies, or a creature card is put into "
            "a graveyard from anywhere other than the battlefield, or a "
            "creature card leaves your graveyard, Syr Konrad, the Grim deals "
            "1 damage to each opponent."))
    game.players[0].battlefield = [konrad]
    dying = _make_card("Dying Bear", owner_index=1)

    engine._check_dies_triggers_sync(game, dying, game.players[1])

    assert [player.life for player in game.players] == [20, 18, 18, 18]


def _cast_torbran_using_ancient_tomb(starting_life):
    game = _ffa_game()
    game.phase = Phase.MAIN1
    engine = GameEngine(None)
    game._rules_engine = engine.rules
    engine.rules.engine_ref = engine
    caster = game.players[0]
    caster.life = starting_life
    tomb = _make_card(
        "Ancient Tomb", type_line="Land",
        oracle_text="{T}: Add {C}{C}. Ancient Tomb deals 2 damage to you.",
        power=None, toughness=None)
    mountains = [
        _make_card(
            f"Mountain {index}", type_line="Basic Land — Mountain",
            oracle_text="{T}: Add {R}.", power=None, toughness=None)
        for index in range(3)
    ]
    caster.battlefield.extend([tomb, *mountains])
    torbran = _make_card(
        "Torbran, Thane of Red Fell", mana_cost="{1}{R}{R}{R}", cmc=4,
        type_line="Legendary Creature — Dwarf Noble", power="2",
        toughness="4", oracle_text=(
            "If a red source you control would deal damage to an opponent "
            "or a permanent an opponent controls, it deals that much plus "
            "2 damage instead."))
    caster.hand.append(torbran)
    result = asyncio.run(cast_spell_async(
        engine, game, caster, torbran))
    return game, caster, torbran, result


def test_mana_ability_self_damage_checks_sba_before_priority_and_resolution():
    game, caster, torbran, result = _cast_torbran_using_ancient_tomb(1)

    assert result[0] is True  # the spell was cast; its caster then lost
    assert caster.eliminated is True
    assert torbran not in caster.battlefield
    assert not any(getattr(entry, 'card', None) is torbran
                   for entry in game.stack)
    assert len(game.living_player_indices()) == 3


def test_mana_ability_self_damage_nonlethal_control_still_resolves_spell():
    game, caster, torbran, result = _cast_torbran_using_ancient_tomb(3)

    assert result[0] is True
    assert caster.eliminated is False
    assert caster.life == 1
    assert torbran in caster.battlefield


def _cast_free_comet_storm(game):
    game.phase = Phase.MAIN1
    engine = GameEngine(None)
    game._rules_engine = engine.rules
    engine.rules.engine_ref = engine
    caster = game.players[0]
    caster.battlefield.extend([
        _make_card(
            f"Storm Mountain {index}", type_line="Basic Land — Mountain",
            oracle_text="{T}: Add {R}.", power=None, toughness=None)
        for index in range(6)
    ])
    storm = _make_card(
        "Comet Storm", type_line="Instant", mana_cost="{X}{R}{R}", cmc=2,
        power=None, toughness=None, oracle_text=(
            "Multikicker {1} (You may pay an additional {1} any number of "
            "times as you cast this spell.)\nChoose any target, then choose "
            "another target for each time this spell was kicked. Comet Storm "
            "deals X damage to each of them."))
    storm._x_value = 4
    caster.hand.append(storm)
    result = asyncio.run(cast_spell_async(
        engine, game, caster, storm, pay_mana=False,
        target=game.players[1]))
    return result


def test_unkicked_comet_storm_honors_one_declared_target_in_ffa_and_duel():
    game = _ffa_game()
    result = _cast_free_comet_storm(game)

    assert result[0] is True
    assert [player.life for player in game.players] == [20, 16, 20, 20]

    duel = _duel_game()
    result = _cast_free_comet_storm(duel)

    assert result[0] is True
    assert [player.life for player in duel.players] == [20, 16]


def test_extra_combat_attacker_labels_defender_only_when_multiplayer():
    phoenix = _make_card("Arclight Phoenix")
    phoenix.attacking_player = 2
    assert _format_autoplay_attacker(
        _ffa_game(), phoenix) == "Arclight Phoenix → C"

    phoenix.attacking_player = 1
    assert _format_autoplay_attacker(
        _duel_game(), phoenix) == "Arclight Phoenix"
