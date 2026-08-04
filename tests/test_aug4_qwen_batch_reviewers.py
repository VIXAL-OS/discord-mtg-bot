"""Reviewer regressions from the Aug 4, 2026 Qwen autoplay batch.

The fixtures use the real action shapes and printed Oracle clauses seen in
the cce6220 corpus.  No test calls a provider or the network.
"""

import asyncio
import inspect

from mtg.engine import GameEngine, _normalize_action_target
from mtg.spells import _compute_alt_costs
from mtg.triggers import _check_cast_triggers


def _engine(game):
    engine = GameEngine(None)
    game._rules_engine = engine.rules
    engine.rules.engine_ref = engine
    return engine


def test_bool_adventure_hint_cannot_crash_split_card_cast(make_game, make_card):
    """Qwen emitted ``adventure: true`` for Insult // Injury.

    The compatibility shim treated every truthy value as a face name and
    called ``.lower()`` on the bool, aborting the game.  An invalid hint may
    fall back to the front face, but it must never crash the executor.
    """
    game = make_game("modern")
    player = game.players[0]
    card = make_card(
        "Insult // Injury",
        type_line="Sorcery",
        oracle_text="Damage can't be prevented this turn.",
        mana_cost="{0}",
        cmc=3,
        power=None,
        toughness=None,
    )
    card.split_names = ["Insult", "Injury"]
    card.split_costs = ["{0}", "{0}"]
    card.split_types = ["Sorcery", "Sorcery"]
    card.split_texts = [card.oracle_text, "Injury deals 2 damage to any target."]
    player.hand.append(card)

    result = asyncio.run(_engine(game)._execute_action(
        game, 0, {"type": "cast", "card": card.name, "adventure": True}))

    assert result is not None
    assert card not in player.hand


def test_cast_target_normalizer_preserves_multiple_named_targets():
    action = {"type": "cast", "card": "Ghostly Flicker",
              "target": ["Mulldrifter", "Island"]}

    assert _normalize_action_target(action) == ["Mulldrifter", "Island"]
    assert action["target"] == ["Mulldrifter", "Island"]


def test_actor_parsers_do_not_flatten_cast_target_arrays():
    """The engine fix is useless if either response parser already discards
    Ghostly Flicker's second target before execution.
    """
    import mtg.claude_player as actor

    src = inspect.getsource(actor)
    assert 'for _k in ("target", "card", "permanent")' not in src
    assert 'step["target"] = tgt[0]' not in src


def test_autoplay_messages_use_the_active_model_seat_name():
    """Qwen games emitted thousands of user-visible ``Claude`` labels."""
    import mtg.ai_turn as ai_turn
    import mtg.autoplay as autoplay

    loop_src = inspect.getsource(autoplay._run_single_autoplay)
    turn_src = inspect.getsource(ai_turn.execute_claude_turn)
    assert "Claude's turn:" not in loop_src
    assert "Claude thinks, then passes" not in loop_src
    assert "Claude attacks with" not in turn_src
    assert "game.active_player.name" in loop_src
    assert "player.name" in turn_src


def test_cascaded_craterhoof_counts_the_live_battlefield(make_game, make_card):
    """Craterhoof was cascaded onto three creatures but the template's
    missing game context used its fallback count of one (+1/+1, not +4/+4).
    """
    game = make_game()
    player = game.players[0]
    bears = [make_card(f"Bear {i}", power="2", toughness="2")
             for i in range(3)]
    player.battlefield.extend(bears)
    craterhoof = make_card(
        "Craterhoof Behemoth",
        type_line="Creature — Beast",
        mana_cost="{5}{G}{G}{G}",
        cmc=8,
        power="5",
        toughness="5",
        oracle_text=("Haste\nWhen Craterhoof Behemoth enters the battlefield, "
                     "creatures you control gain trample and get +X/+X until "
                     "end of turn, where X is the number of creatures you control."),
    )
    player.library = [craterhoof]
    source = make_card("Apex Cascade Test", type_line="Sorcery",
                       mana_cost="{9}", cmc=9, power=None, toughness=None,
                       oracle_text="Cascade")

    asyncio.run(_check_cast_triggers(_engine(game), game, player, source))

    assert craterhoof in player.battlefield
    game.recalculate_power_toughness()
    for creature in bears:
        assert creature.get_effective_power(game) == 6
        assert creature.has_keyword("Trample")


def test_targeted_channel_request_does_not_tap_battlefield_boseiju(
        make_game, make_card):
    """With one Boseiju in play and one in hand, a Channel-shaped action was
    silently clamped to the land's surviving mana ability and tapped it.
    """
    game = make_game()
    player, opponent = game.players
    oracle = ("{T}: Add {G}.\nChannel — {1}{G}, Discard Boseiju, Who Endures: "
              "Destroy target artifact, enchantment, or nonbasic land an "
              "opponent controls.")
    battlefield_copy = make_card("Boseiju, Who Endures",
                                 type_line="Legendary Land",
                                 oracle_text=oracle,
                                 power=None, toughness=None)
    hand_copy = make_card("Boseiju, Who Endures", type_line="Legendary Land",
                          oracle_text=oracle, power=None, toughness=None)
    target = make_card("Sol Ring", type_line="Artifact", power=None,
                       toughness=None)
    player.battlefield.append(battlefield_copy)
    player.hand.append(hand_copy)
    opponent.battlefield.append(target)

    result = asyncio.run(_engine(game)._execute_action(game, 0, {
        "type": "activate",
        "permanent": "Boseiju, Who Endures",
        "ability": 1,
        "target": "Sol Ring",
    }))

    assert result is None
    assert not battlefield_copy.tapped
    assert sum(player.mana_pool.values()) == 0


def test_fierce_guardianship_is_free_with_commander_controlled(
        make_game, make_card):
    oracle = ("If you control a commander, you may cast this spell without "
              "paying its mana cost.\nCounter target noncreature spell.")
    game = make_game()
    player = game.players[0]
    commander = make_card("Thrasios, Triton Hero",
                          type_line="Legendary Creature — Merfolk Wizard")
    commander.is_commander = True
    fierce = make_card("Fierce Guardianship", type_line="Instant",
                       oracle_text=oracle, mana_cost="{2}{U}", cmc=3,
                       power=None, toughness=None)
    player.battlefield.append(commander)
    player.hand.append(fierce)

    assert player.can_pay_printed_alternate_cost(fierce)
    rejection, costs = _compute_alt_costs(
        _engine(game), game, player, fierce, pay_mana=True,
        additional_cost=0)
    assert rejection is None
    assert costs["pay_mana"] is False
