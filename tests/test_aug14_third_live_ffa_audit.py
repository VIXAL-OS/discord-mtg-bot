"""Behavioral pins for live cube FFA game 1537891334034165935."""

from conftest import _make_card, _make_game
from rules.effect_templates import build_game_context, get_effect_library


def _land(name, owner_index, produces):
    return _make_card(
        name, owner_index=owner_index, type_line=f"Basic Land — {name}",
        oracle_text=f"{{T}}: Add {{{produces}}}.", power=None, toughness=None,
    )


def test_bloodthirsty_adversary_pays_filters_and_queues_real_spell_copy(rules):
    game = _make_game("limited")
    player, opponent = game.players
    source = _make_card(
        "Bloodthirsty Adversary", owner_index=0, mana_cost="{1}{R}", cmc=2,
        type_line="Creature — Vampire", power="2", toughness="2",
        oracle_text=(
            "Haste\nWhen Bloodthirsty Adversary enters, you may pay {2}{R} "
            "any number of times. When you pay this cost one or more times, "
            "put that many +1/+1 counters on Bloodthirsty Adversary, then "
            "exile up to that many target instant and/or sorcery cards with "
            "mana value 3 or less from your graveyard and copy them."),
    )
    bolt = _make_card(
        "Lightning Bolt", owner_index=0, mana_cost="{R}", cmc=1,
        type_line="Instant", oracle_text="Lightning Bolt deals 3 damage to any target.",
        power=None, toughness=None,
    )
    ragavan = _make_card(
        "Ragavan, Nimble Pilferer", owner_index=0, mana_cost="{R}", cmc=1,
        type_line="Legendary Creature — Monkey Pirate", power="2", toughness="1",
    )
    player.battlefield.extend([
        source, _land("Mountain", 0, "R"),
        _land("Plains", 0, "W"), _land("Plains", 0, "W"),
    ])
    player.graveyard.extend([ragavan, bolt])

    ctx = build_game_context(game, player, opponent, card=source)
    actions, _ = get_effect_library().resolve_etb(
        source.name, source.oracle_text, player.name, opponent.name, ctx)
    assert [action["action"] for action in actions] == [
        "bloodthirsty_adversary_etb"]

    message = rules._execute_action_on_state(game, actions[0])

    assert "pays {2}{R} 1 time" in message
    assert source.counters.get("+1/+1") == 1
    assert bolt in player.exile
    assert ragavan in player.graveyard
    assert sum(card.tapped for card in player.battlefield if card.is_land()) == 3
    assert len(game._free_cast_pending) == 1
    queued = game._free_cast_pending[0]
    assert queued["is_copy"] is True
    assert queued["card"].name == "Lightning Bolt"
    assert queued["card"]._copy_of == "Lightning Bolt"
    assert queued["card"].is_token is False


def test_bloodthirsty_adversary_does_not_fake_payment_or_copy_creature(rules):
    game = _make_game("limited")
    player, opponent = game.players
    source = _make_card(
        "Bloodthirsty Adversary", owner_index=0,
        oracle_text="When Bloodthirsty Adversary enters, you may pay {2}{R}.",
    )
    creature = _make_card("Ragavan, Nimble Pilferer", owner_index=0)
    player.battlefield.extend([
        source, _land("Plains", 0, "W"),
        _land("Plains", 0, "W"), _land("Plains", 0, "W"),
    ])
    player.graveyard.append(creature)
    ctx = build_game_context(game, player, opponent, card=source)
    actions, _ = get_effect_library().resolve_etb(
        source.name, source.oracle_text, player.name, opponent.name, ctx)

    rules._execute_action_on_state(game, actions[0])

    assert source.counters.get("+1/+1", 0) == 0
    assert creature in player.graveyard
    assert player.exile == []
    assert game._free_cast_pending == []
    assert not any(card.tapped for card in player.battlefield if card.is_land())


def test_nevermaker_ltb_uses_suffix_template_and_tucks_a_legal_nonland(rules):
    game = _make_game("limited")
    player, opponent = game.players
    nevermaker = _make_card(
        "Nevermaker", owner_index=0, type_line="Creature — Elemental",
        oracle_text=(
            "Flying\nWhen this creature leaves the battlefield, put target "
            "nonland permanent on top of its owner's library."),
    )
    target = _make_card("Primeval Titan", owner_index=1)
    opponent.battlefield.extend([_land("Forest", 1, "G"), target])
    ctx = build_game_context(game, player, opponent, card=nevermaker)

    actions, _ = get_effect_library().resolve_etb(
        nevermaker.name, nevermaker.oracle_text, player.name, opponent.name,
        ctx, event_type="ltb")
    assert [action["action"] for action in actions] == ["move_card"]
    rules._execute_action_on_state(game, actions[0])

    assert target not in opponent.battlefield
    assert opponent.library[0] is target


def test_nevermaker_ltb_with_only_lands_is_truthful_no_action():
    game = _make_game("limited")
    player, opponent = game.players
    nevermaker = _make_card(
        "Nevermaker", owner_index=0,
        oracle_text=(
            "When this creature leaves the battlefield, put target nonland "
            "permanent on top of its owner's library."),
    )
    opponent.battlefield.append(_land("Forest", 1, "G"))
    ctx = build_game_context(game, player, opponent, card=nevermaker)
    actions, _ = get_effect_library().resolve_etb(
        nevermaker.name, nevermaker.oracle_text, player.name, opponent.name,
        ctx, event_type="ltb")

    assert actions == [{
        "action": "no_action",
        "reason": "Nevermaker: no legal nonland permanent",
    }]


def test_sphinx_of_uthuun_moves_all_five_revealed_cards_to_exact_zones(rules):
    game = _make_game("limited")
    player, opponent = game.players
    sphinx = _make_card(
        "Sphinx of Uthuun", owner_index=0, type_line="Creature — Sphinx",
        oracle_text=(
            "Flying\nWhen this creature enters, reveal the top five cards of "
            "your library. An opponent separates those cards into two piles. "
            "Put one pile into your hand and the other into your graveyard."),
    )
    revealed = [_make_card(f"Reveal {index}", owner_index=0)
                for index in range(5)]
    remainder = _make_card("Remainder", owner_index=0)
    player.library.extend(revealed + [remainder])
    ctx = build_game_context(game, player, opponent, card=sphinx)

    actions, _ = get_effect_library().resolve_etb(
        sphinx.name, sphinx.oracle_text, player.name, opponent.name, ctx)
    assert len(actions) == 5
    for action in actions:
        rules._execute_action_on_state(game, action)

    assert player.hand == revealed[:3]
    assert player.graveyard == revealed[3:]
    assert player.library == [remainder]
