"""July 20 semantic audit (July 12-13 unaudited batch) — regression pins.

Each test names the motivating game. Headliners: Athreos firing on the
WRONG owner's creatures (game-deciding), populate fabricating a token on an
empty board, Faith Unbroken permanently eating two creatures, Worldslayer's
defining ability not existing, and Decree of Pain's dropped draw clause.
"""
import pytest

from mtg.actions import execute_action_on_state


def run(rules, game, action):
    return execute_action_on_state(rules, game, dict(action))


class TestAthreosOwnershipGate:
    def _resolve(self, lib, game, make_card, dying, dying_owner_is_ctrl):
        from rules.effect_templates import build_game_context

        rick, claude = game.players
        # Athreos controlled by Claude; ctrl=Claude, opp=Rick. Game setup
        # stamps owner_index per deck (mtg/engine.py:959/965) — mirror it.
        owner = claude if dying_owner_is_ctrl else rick
        dying.owner_index = game.players.index(owner)
        owner.graveyard.append(dying)
        ctx = build_game_context(game, claude, rick, dying_creature=dying)
        return lib.resolve_dies_trigger(
            trigger_card_name="Athreos, God of Passage",
            trigger_oracle="Whenever another creature you own dies, return "
                           "it to your hand unless target opponent pays 3 life.",
            dying_creature_name=dying.name,
            dying_creature_power=2, dying_creature_toughness=2,
            controller="Claude", opponent="Rick",
            game_context=ctx)

    def test_opponents_creature_death_does_not_trigger(self, lib, game, make_card):
        # game_1526071401499328634: Athreos (Claude's) fired on RICK's dying
        # creatures five times, and once drained Claude himself.
        dying = make_card("Mesa Enchantress")
        actions, _ = self._resolve(lib, game, make_card, dying,
                                   dying_owner_is_ctrl=False)
        assert actions is not None
        assert all(a.get("action") == "no_action" for a in actions)

    def test_own_creature_death_drains_opponent_at_high_life(
            self, lib, game, make_card):
        dying = make_card("Nether Traitor")
        actions, _ = self._resolve(lib, game, make_card, dying,
                                   dying_owner_is_ctrl=True)
        assert any(a.get("action") == "lose_life" and a.get("player") == "Rick"
                   and a.get("amount") == 3 for a in actions)


class TestPopulate:
    def test_populate_with_no_tokens_creates_nothing(self, rules, game, make_card):
        # game_1526071401499328634: Tier 3 fabricated a Human token on a
        # zero-token board (CR 701.34a: populate does nothing).
        rick = game.players[0]
        rick.battlefield.append(make_card("Charging Bear"))  # nontoken
        before = len(rick.battlefield)

        result = run(rules, game, {"action": "populate", "player": "Rick"})

        assert result is None
        assert len(rick.battlefield) == before

    def test_populate_copies_best_creature_token(self, rules, game, make_card):
        rick = game.players[0]
        run(rules, game, {"action": "create_token", "player": "Rick",
                          "name": "Knight", "power": 2, "toughness": 2,
                          "types": "Creature — Knight", "count": 1})
        run(rules, game, {"action": "populate", "player": "Rick"})

        knights = [c for c in rick.battlefield if c.name == "Knight"]
        assert len(knights) == 2
        assert all(getattr(k, 'is_token', False) for k in knights)


class TestWorldslayer:
    def test_destroy_all_permanents_spares_the_exception_and_indestructible(
            self, rules, game, make_card):
        # game_1526071467035459665: three combat hits while equipped, zero
        # wipes — the ability had no handler at all.
        rick, claude = game.players
        skull = make_card("Worldslayer", type_line="Artifact — Equipment",
                          power=None, toughness=None)
        land = make_card("Plains", type_line="Basic Land — Plains",
                         power="0", toughness="0")
        bear = make_card("Charging Bear")
        darksteel = make_card("Darksteel Myr",
                              type_line="Artifact Creature — Myr",
                              power="0", toughness="1",
                              oracle_text="Indestructible",
                              keywords=["Indestructible"])
        rick.battlefield.extend([skull, land, bear])
        claude.battlefield.append(darksteel)

        run(rules, game, {"action": "destroy_all_permanents",
                          "except_card": "Worldslayer"})

        assert skull in rick.battlefield
        assert darksteel in claude.battlefield
        assert land not in rick.battlefield and land in rick.graveyard
        assert bear not in rick.battlefield and bear in rick.graveyard
        # The dead creature queues its dies-trigger like any wipe.
        assert any(c is bear for c, _p in game._recently_died)

    def test_worldslayer_attack_template_registered(self, lib):
        tmpl = lib._attack_templates.get("worldslayer")
        assert tmpl is not None
        actions = tmpl.action_generator("Rick", "Claude", {})
        assert actions == [{"action": "destroy_all_permanents",
                            "except_card": "Worldslayer"}]


class TestFaithUnbroken:
    def test_etb_exiles_with_return_tracking(self, lib, game, make_card):
        # game_1526071467035459665: the generic exile pattern had no return
        # tracking — the aura's forced detach re-ran the exile on a SECOND
        # creature instead of returning the first.
        actions, _ = lib.resolve_etb(
            "Faith Unbroken",
            "Enchant creature you control\nWhen this Aura enters, exile "
            "target creature an opponent controls until this Aura leaves "
            "the battlefield.\nEnchanted creature gets +2/+2.",
            "Rick", "Claude",
            game_context={"explicit_target_name": "Strangleroot Geist"})

        assert actions is not None
        kinds = [a.get("action") for a in actions]
        assert "move_card" in kinds and "track_exiled_by" in kinds
        track = next(a for a in actions if a["action"] == "track_exiled_by")
        assert track["card"] == "Strangleroot Geist"
        assert track["source"] == "Faith Unbroken"


class TestJeskaLoyaltyBonus:
    def test_commander_casts_count_toward_enters_loyalty(self, game, make_card):
        # game_1527448352298500096: Jeska (printed loyalty 0) entered with 0
        # and died to SBA instantly — her "enters with a loyalty counter for
        # each time you've cast a commander" was unimplemented.
        from mtg.helpers import loyalty_from_commander_casts

        rick = game.players[0]
        daretti = make_card("Daretti, Scrap Savant",
                            type_line="Legendary Planeswalker — Daretti",
                            power=None, toughness=None, loyalty="3")
        daretti.is_commander = True
        daretti.times_cast_from_command_zone = 2
        rick.battlefield.append(daretti)
        jeska = make_card("Jeska, Thrice Reborn",
                          type_line="Legendary Planeswalker — Jeska",
                          power=None, toughness=None, loyalty="0",
                          oracle_text="Jeska enters with a loyalty counter on "
                                      "her for each time you've cast a "
                                      "commander from the command zone this "
                                      "game.")

        assert loyalty_from_commander_casts(game, rick, jeska) == 2
        # Non-matching walkers get no bonus.
        assert loyalty_from_commander_casts(game, rick, daretti) == 0


class TestArchangelOfThune:
    def test_life_gain_puts_counter_on_each_creature(self, rules, game, make_card):
        # game_1527462198430138448: [GAIN-TRIGGER-UNHANDLED] twice — the
        # core passive did nothing all game.
        claude = game.players[1]
        angel = make_card(
            "Archangel of Thune", power="3", toughness="4",
            keywords=["Flying", "Lifelink"],
            oracle_text="Flying\nLifelink\nWhenever you gain life, put a "
                        "+1/+1 counter on each creature you control.")
        bear = make_card("Charging Bear")
        claude.battlefield.extend([angel, bear])

        rules._apply_life_gain(game, claude, 3, source_name="Lifelink")

        assert angel.counters.get("+1/+1", 0) == 1
        assert bear.counters.get("+1/+1", 0) == 1


class TestSacrificeAdditionalCostGate:
    def test_cast_rejected_without_a_creature_to_sacrifice(
            self, make_game, make_card):
        # game_1526071467035459665: Diabolic Intent slipped through the
        # inline path with no creature, burned mana, and fizzled. The gate
        # now lives in _validate_cast so every path is covered.
        import asyncio
        from mtg.constants import Phase
        from mtg.engine import GameEngine
        from mtg.spells import cast_spell_async

        game = make_game()
        game.phase = Phase.MAIN1
        game.active_player_index = 0
        rick = game.players[0]
        rick.battlefield.extend(
            make_card(f"Swamp {i}", type_line="Basic Land — Swamp",
                      power="0", toughness="0") for i in range(2))
        intent = make_card(
            "Diabolic Intent", type_line="Sorcery", mana_cost="{1}{B}",
            cmc=2, power="0", toughness="0",
            oracle_text="As an additional cost to cast this spell, sacrifice "
                        "a creature.\nSearch your library for a card, put it "
                        "into your hand, then shuffle.")
        rick.hand.append(intent)

        ok, msg, _ = asyncio.run(cast_spell_async(
            GameEngine(None), game, rick, intent))

        assert ok is False
        assert "sacrifice" in msg.lower()
        assert intent in rick.hand
        assert all(not land.tapped for land in rick.battlefield)


class TestDecreeOfPain:
    def test_wipe_draws_per_actual_destroy(self, rules, game, make_card):
        # game_1526071467035459665: the generic wipe template dropped
        # "Draw a card for each creature destroyed this way" (2 cards lost).
        rick, claude = game.players
        claude.battlefield.extend([make_card("Bear A"), make_card("Bear B")])
        rick.library = [make_card(f"Card {i}", type_line="Sorcery",
                                  power="0", toughness="0") for i in range(5)]
        hand_before = len(rick.hand)

        msg = run(rules, game, {"action": "destroy_all_creatures",
                                "draw_per_destroyed": "Rick"})

        assert len(rick.hand) == hand_before + 2
        assert "draws 2" in msg

    def test_decree_json_template_carries_the_draw(self, lib):
        tmpl = lib._card_templates.get("decree of pain")
        assert tmpl is not None
        actions = tmpl.action_generator("Rick", "Claude", {})
        assert actions[0]["action"] == "destroy_all_creatures"
        assert actions[0]["draw_per_destroyed"] == "Rick"
