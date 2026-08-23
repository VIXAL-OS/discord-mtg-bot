"""Behavioral pins for the Aug-14 live cube reports.

FORK NOTE: the private copy of this file also carries pins for the
companion-bot distress detector and the tarot retry helper, which import
symbols this fork's bot.py does not define. Only the engine half lives
here; port changes as hunks, never wholesale.
"""

import asyncio

from mtg.engine import GameEngine
from mtg.spells import _validate_cast


def _bonecrusher(make_card):
    card = make_card(
        "Bonecrusher Giant", mana_cost="{2}{R}",
        type_line="Creature — Giant",
        oracle_text=(
            "Whenever this creature becomes the target of a spell, this "
            "creature deals 2 damage to that spell's controller."),
        power="4", toughness="3")
    card.adventure_name = "Stomp"
    card.adventure_cost = "{1}{R}"
    card.adventure_type = "Instant — Adventure"
    card.adventure_text = (
        "Damage can't be prevented this turn. "
        "Stomp deals 2 damage to any target.")
    card.cast_as_adventure = True
    return card


def _add_stomp_mana(player, make_card):
    player.battlefield.extend([
        make_card("Mountain", type_line="Basic Land — Mountain",
                  oracle_text="{T}: Add {R}.", power=None, toughness=None),
        make_card("Mountain", type_line="Basic Land — Mountain",
                  oracle_text="{T}: Add {R}.", power=None, toughness=None),
    ])


def test_stomp_declared_player_target_uses_the_adventure_face(
        game, make_card):
    """The live cube cast was rejected after the broad target finder passed.

    `_validate_cast` then fed Bonecrusher's creature face into the declared
    player-target validator. Stomp's synthetic face must be used at that last
    gate too, before any mana or card movement occurs.
    """
    rick, opponent = game.players
    card = _bonecrusher(make_card)
    rick.hand.append(card)
    _add_stomp_mana(rick, make_card)

    rejection, _from_graveyard, target = _validate_cast(
        GameEngine(None), game, rick, card, opponent)

    assert rejection is None
    assert target is opponent


def test_stomp_casts_deals_two_and_exiles_the_adventure_card(
        game, make_card):
    """End-to-end production path for the action that failed in the bracket."""
    rick, opponent = game.players
    card = _bonecrusher(make_card)
    rick.hand.append(card)
    _add_stomp_mana(rick, make_card)
    engine = GameEngine(None)
    before_life = opponent.life

    success, _message, _effects = asyncio.run(engine.cast_spell_async(
        game, rick, card, target=opponent))

    assert success
    assert opponent.life == before_life - 2
    assert card in rick.exile
    assert card not in rick.hand


def test_adventure_player_target_still_obeys_its_printed_restriction(
        game, make_card):
    """CONTROL: using the active face must not make every player legal."""
    rick, opponent = game.players
    card = _bonecrusher(make_card)
    card.adventure_name = "Creature Only"
    card.adventure_text = "Destroy target creature."
    rick.hand.append(card)
    _add_stomp_mana(rick, make_card)
    opponent.battlefield.append(make_card(
        "Grizzly Bears", type_line="Creature — Bear",
        power="2", toughness="2"))

    rejection, _from_graveyard, _target = _validate_cast(
        GameEngine(None), game, rick, card, opponent)

    assert rejection is not None
    assert rejection[0] is False
    assert "Creature Only" in rejection[1]
