"""Aug-13 sixth-confirmation coverage pins.

Each finding has a positive path and an adverse control.  These intentionally
exercise production templates/actions instead of re-implementing predicates.
"""
import asyncio
from pathlib import Path

from conftest import _make_card, _make_game


def _engine(game):
    from mtg.engine import GameEngine
    engine = GameEngine(None)
    game._rules_engine = engine.rules
    engine.rules.engine_ref = engine
    return engine


def _attach(player, attachment, creature):
    player.battlefield.extend([attachment, creature])
    attachment.attached_to = creature.id
    creature.attachments.append(attachment.id)


class TestHistoryOfBenalia:
    def test_chapter_three_pumps_knights_not_enchantresses(self):
        game = _make_game()
        engine = _engine(game)
        player = game.players[0]
        saga = _make_card("History of Benalia", type_line="Enchantment — Saga",
                          oracle_text=("I, II — Create a 2/2 white Knight creature token with vigilance.\n"
                                       "III — Knights you control get +2/+1 until end of turn."))
        saga.counters["lore"] = 2
        knight = _make_card("Knight", type_line="Creature — Human Knight", power="2", toughness="2")
        sythis = _make_card("Sythis", type_line="Creature — Nymph", power="2", toughness="2")
        player.battlefield.extend([saga, knight, sythis])
        engine._advance_sagas(game, player)
        assert knight.get_effective_power(game) == 4
        assert sythis.get_effective_power(game) == 2


class TestHollowhengeOverlord:
    def test_snapshot_counts_source_and_other_wolf_not_new_token(self):
        game = _make_game()
        player = game.players[0]
        source = _make_card("Hollowhenge Overlord", type_line="Creature — Wolf", power="7", toughness="5")
        wolf = _make_card("Other Wolf", type_line="Creature — Wolf", power="2", toughness="2")
        human = _make_card("Human", type_line="Creature — Human", power="2", toughness="2")
        player.battlefield.extend([source, wolf, human])
        from rules.effect_templates import get_effect_library, build_game_context
        actions, _ = get_effect_library().resolve_upkeep_trigger(
            source.name, "At the beginning of your upkeep, for each creature you control that's a Wolf or a Werewolf, create a 2/2 green Wolf creature token.",
            player.name, game.players[1].name, build_game_context(game, player, game.players[1], card=source))
        assert actions[0]["count"] == 2
        _engine(game).rules._execute_action_on_state(game, actions[0])
        assert len([c for c in player.battlefield if c.name == "Wolf"]) == 2


class TestRestrictiveAttachmentMultipliers:
    def test_mantle_and_reverie_count_only_their_printed_attachment_sets(self):
        game = _make_game()
        player, opponent = game.players
        bearer = _make_card("Germ", type_line="Creature — Germ", power="0", toughness="0")
        mantle = _make_card("Mantle of the Ancients", type_line="Enchantment — Aura",
                            oracle_text="Enchanted creature gets +1/+1 for each Aura and Equipment attached to it.")
        reverie = _make_card("Sage's Reverie", type_line="Enchantment — Aura",
                             oracle_text="Enchanted creature gets +1/+1 for each Aura you control that's attached to a creature.")
        aura = _make_card("Mask", type_line="Enchantment — Aura", oracle_text="Enchanted creature gets +1/+1.")
        sword = _make_card("Sword", type_line="Artifact — Equipment", oracle_text="Equipped creature gets +2/+2.")
        enemy_aura = _make_card("Pacifism", type_line="Enchantment — Aura", oracle_text="Enchanted creature can't attack or block.")
        _attach(player, mantle, bearer)
        _attach(player, reverie, bearer)
        _attach(player, aura, bearer)
        _attach(player, sword, bearer)
        _attach(opponent, enemy_aura, bearer)
        # Mantle sees all five Aura/Equipment attachments (+5); Reverie sees
        # only the controller's three attached Auras (+3); Mask +1, Sword +2.
        assert bearer.get_effective_power(game) == 11

    def test_reverie_ignores_controlled_aura_attached_to_noncreature(self):
        game = _make_game()
        player = game.players[0]
        bearer = _make_card("Bear", type_line="Creature — Bear", power="2", toughness="2")
        reverie = _make_card("Sage's Reverie", type_line="Enchantment — Aura",
                             oracle_text="Enchanted creature gets +1/+1 for each Aura you control that's attached to a creature.")
        land_aura = _make_card("Land Aura", type_line="Enchantment — Aura")
        land = _make_card("Forest", type_line="Basic Land")
        _attach(player, reverie, bearer)
        _attach(player, land_aura, land)
        assert bearer.get_effective_power(game) == 3


class TestGrantedAdventure:
    def test_granted_has_truthful_no_action_and_still_exiles_adventure(self):
        game = _make_game()
        engine = _engine(game)
        player = game.players[0]
        fae = _make_card("Fae of Wishes", type_line="Creature — Faerie Wizard",
                         mana_cost="{1}{U}", oracle_text="Flying", power="1", toughness="4",
                         adventure_name="Granted", adventure_cost="{3}{U}",
                         adventure_text="You may reveal a noncreature card you own from outside the game and put it into your hand.",
                         adventure_type="Sorcery — Adventure")
        player.hand.append(fae)
        player.mana_pool.update({"U": 1, "C": 3})
        fae.cast_as_adventure = True
        ok, _, messages = asyncio.run(engine.cast_spell_async(game, player, fae))
        assert ok and fae in player.exile
        assert any("outside-game cards are not modeled" in message for message in messages)
        assert not any("complex effect" in message.lower() for message in messages)


class TestAutoplayForetellTag:
    def test_tag_is_after_success_gate_not_exile_extraction(self):
        """The production path captures the foretell flag before clearing it,
        but emits the audit tag only after cast_spell_async succeeds."""
        source = (Path(__file__).resolve().parent.parent / "mtg" / "autoplay.py").read_text(encoding="utf-8")
        cast_call = source.index("success, msg, effect_msgs = await cog.engine.cast_spell_async")
        success_gate = source.index("if success:", cast_call)
        tag = source.index("[FORETELL-CAST]", success_gate)
        assert success_gate < tag
        # Pin the live condition too: disabling it leaves the tag string in
        # place and would make a source-order-only assertion vacuous.
        assert "if _was_foretold:" in source[success_gate:tag]
        assert source.count("[FORETELL-CAST]") == 1


class TestToothAndNail:
    def test_entwine_is_library_to_hand_then_hand_to_battlefield_with_two_choices(self):
        game = _make_game()
        engine = _engine(game)
        player = game.players[0]
        a = _make_card("A", type_line="Creature — Beast", cmc=8, power="8", toughness="8")
        b = _make_card("B", type_line="Creature — Beast", cmc=7, power="7", toughness="7")
        hand = _make_card("Hand", type_line="Creature — Beast", cmc=6, power="6", toughness="6")
        player.library.extend([a, b])
        player.hand.append(hand)
        from rules.effect_templates import get_effect_library
        actions = get_effect_library()._gen_tooth_and_nail(player.name, game.players[1].name, {"entwined": True})
        assert [action["action"] for action in actions] == ["search_library", "move_cards_from_hand"]
        actions[0]["card_names"] = ["B", "A"]
        engine.rules._execute_action_on_state(game, actions[0])
        assert {c.name for c in player.hand} == {"A", "B", "Hand"}
        engine.rules._execute_action_on_state(game, actions[1])
        assert len(player.battlefield) == 2
        assert len(player.library) == 0

    def test_base_modes_remain_separate(self):
        from rules.effect_templates import get_effect_library
        library_mode = get_effect_library()._gen_tooth_and_nail("Rick", "Claude", {"entwined": False, "controller_hand": []})
        assert len(library_mode) == 1 and library_mode[0]["to_zone"] == "hand"
        bomb = _make_card("Bomb", type_line="Creature — Beast", cmc=7, power="7", toughness="7")
        hand_mode = get_effect_library()._gen_tooth_and_nail("Rick", "Claude", {"entwined": False, "controller_hand": [bomb]})
        assert len(hand_mode) == 1 and hand_mode[0]["from_zone"] == "hand"
