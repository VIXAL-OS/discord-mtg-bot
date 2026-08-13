"""Aug 13 sixth-confirmation FOUNDATION fixes.

These pins deliberately drive the AI activation/cast executor where the
findings occurred; direct helper coverage alone would not protect a payment or
zone-routing bypass above the helper.
"""
import asyncio

from mtg.helpers import resolve_cast_target


def _run(coro):
    return asyncio.run(coro)


def _engine(game):
    from mtg.engine import GameEngine
    engine = GameEngine(None)
    game._rules_engine = engine.rules
    engine.rules.engine_ref = engine
    return engine


class TestActivatedSacrificeDeathSaves:
    def test_self_sacrifice_returns_clean_undying_creature(self, game, make_card):
        """The actual self-sac activation must invoke the shared save chain."""
        rick = game.players[0]
        wolf = make_card(
            "Self-Sacrificing Wolf", type_line="Creature — Wolf",
            oracle_text="Undying\nSacrifice this creature: Add {C}.",
            keywords=["Undying"], power="2", toughness="2")
        rick.battlefield.append(wolf)

        _run(_engine(game)._execute_action(game, 0, {
            "type": "activate", "permanent": wolf.name, "ability": 0,
        }))

        assert wolf in rick.battlefield
        assert wolf not in rick.graveyard
        assert wolf.counters.get('+1/+1') == 1
        assert any("undying" in message.lower()
                   for message in game._pending_messages)

    def test_chosen_sacrifice_returns_clean_undying_creature(self, game, make_card):
        """The chosen-creature cost is a distinct activation route."""
        rick = game.players[0]
        seer = make_card("Audit Seer", type_line="Creature — Vampire",
                         oracle_text="Sacrifice a creature: Scry 1.")
        wolf = make_card("Chosen Wolf", type_line="Creature — Wolf",
                         oracle_text="Undying", keywords=["Undying"],
                         power="2", toughness="2")
        rick.battlefield.extend([seer, wolf])

        _run(_engine(game)._execute_action(game, 0, {
            "type": "activate", "permanent": seer.name, "ability": 0,
            "target": wolf.name,
        }))

        assert wolf in rick.battlefield
        assert wolf not in rick.graveyard
        assert wolf.counters.get('+1/+1') == 1

    def test_counter_and_zone_redirects_do_not_return_the_victim(
            self, game, make_card, make_game):
        """Controls: undying is once-only and no graveyard arrival means no save."""
        rick = game.players[0]
        seer = make_card("Audit Seer", type_line="Creature — Vampire",
                         oracle_text="Sacrifice a creature: Scry 1.")
        spent = make_card("Spent Wolf", type_line="Creature — Wolf",
                          oracle_text="Undying", keywords=["Undying"],
                          power="2", toughness="2")
        spent.counters['+1/+1'] = 1
        rick.battlefield.extend([seer, spent])
        engine = _engine(game)
        _run(engine._execute_action(game, 0, {
            "type": "activate", "permanent": seer.name, "ability": 0,
            "target": spent.name,
        }))
        assert spent in rick.graveyard and spent not in rick.battlefield

        # Re-arm the outlet in a new game: an unearthed creature is redirected
        # to exile and therefore cannot be returned by a graveyard death save.
        redirected_game = make_game()
        rick, _ = redirected_game.players
        seer = make_card("Audit Seer Two", type_line="Creature — Vampire",
                         oracle_text="Sacrifice a creature: Scry 1.")
        unearthed = make_card("Unearthed Wolf", type_line="Creature — Wolf",
                              oracle_text="Undying", keywords=["Undying"],
                              power="2", toughness="2")
        unearthed._unearthed = True
        rick.battlefield.extend([seer, unearthed])
        _run(_engine(redirected_game)._execute_action(redirected_game, 0, {
            "type": "activate", "permanent": seer.name, "ability": 0,
            "target": unearthed.name,
        }))
        assert unearthed in rick.exile and unearthed not in rick.battlefield

        command_game = make_game()
        rick, _ = command_game.players
        seer = make_card("Audit Seer Three", type_line="Creature — Vampire",
                         oracle_text="Sacrifice another creature: Scry 1.")
        commander = make_card("Undying Commander", type_line="Creature — Wolf",
                              oracle_text="Undying", keywords=["Undying"],
                              power="2", toughness="2", is_commander=True)
        commander.owner_index = 0
        rick.battlefield.extend([seer, commander])
        _run(_engine(command_game)._execute_action(command_game, 0, {
            "type": "activate", "permanent": seer.name, "ability": 0,
            "target": commander.name,
        }))
        assert commander in rick.command_zone
        assert commander not in rick.battlefield and commander not in rick.graveyard


class TestEquipTargetBeforePayment:
    def _board(self, game, make_card):
        rick, claude = game.players
        sword = make_card("Audit Sword", type_line="Artifact — Equipment",
                          oracle_text="Equip {2}", power=None, toughness=None)
        own = make_card("Own Bear", type_line="Creature — Bear")
        enemy = make_card("Enemy Bear", type_line="Creature — Bear")
        noncreature = make_card("Own Rock", type_line="Artifact", power=None,
                                toughness=None)
        rick.battlefield.extend([sword, own, noncreature])
        claude.battlefield.append(enemy)
        rick.mana_pool['C'] = 2
        return rick, claude, sword, own, enemy, noncreature

    def test_valid_explicit_target_pays_and_equips(self, game, make_card):
        rick, _, sword, own, _, _ = self._board(game, make_card)
        result = _run(_engine(game)._execute_action(game, 0, {
            "type": "activate", "permanent": sword.name, "ability": 0,
            "target": own.name,
        }))
        assert result and "equips" in result
        assert sword.attached_to == own.id
        assert sum(rick.mana_pool.values()) == 0

    def test_invalid_explicit_targets_pay_nothing_and_never_fall_back(
            self, game, make_card):
        rick, _, sword, own, enemy, noncreature = self._board(game, make_card)
        engine = _engine(game)
        for bad in (enemy.name, noncreature.name):
            before = dict(rick.mana_pool)
            result = _run(engine._execute_action(game, 0, {
                "type": "activate", "permanent": sword.name, "ability": 0,
                "target": bad,
            }))
            assert result is None
            assert rick.mana_pool == before
            assert sword.attached_to is None
            assert sword.id not in own.attachments

    def test_targetless_equip_keeps_auto_selection(self, game, make_card):
        rick, _, sword, own, _, _ = self._board(game, make_card)
        result = _run(_engine(game)._execute_action(game, 0, {
            "type": "activate", "permanent": sword.name, "ability": 0,
        }))
        assert result and "equips" in result
        assert sword.attached_to == own.id
        assert sum(rick.mana_pool.values()) == 0


class TestPlayerCastTargetsNeedOraclePermission:
    def test_bolt_and_a_legal_self_player_spell_keep_their_player_targets(
            self, game, make_card):
        rick, claude = game.players
        bolt = make_card("Lightning Bolt", type_line="Instant",
                         oracle_text="Lightning Bolt deals 3 damage to any target.",
                         power=None, toughness=None)
        salve = make_card("Healing Salve", type_line="Instant",
                          oracle_text="Target player gains 3 life.",
                          power=None, toughness=None)
        assert resolve_cast_target(game, rick, bolt, "opponent") is claude
        assert resolve_cast_target(game, rick, salve, "you") is rick

    def test_reanimate_cannot_resolve_opponent_as_a_target(self, game, make_card):
        rick, claude = game.players
        reanimate = make_card(
            "Reanimate", type_line="Sorcery", power=None, toughness=None,
            oracle_text=("Put target creature card from a graveyard onto the "
                         "battlefield under your control. You lose life equal "
                         "to its mana value."))
        assert resolve_cast_target(game, rick, reanimate, "opponent") is None
        assert resolve_cast_target(game, rick, reanimate, claude.name) is None

    def test_reanimate_rejects_opponent_before_payment_or_card_move(
            self, game, make_card):
        rick = game.players[0]
        reanimate = make_card(
            "Reanimate", type_line="Sorcery", mana_cost="{B}",
            power=None, toughness=None,
            oracle_text=("Put target creature card from a graveyard onto the "
                         "battlefield under your control. You lose life equal "
                         "to its mana value."))
        rick.hand.append(reanimate)
        before_pool = dict(rick.mana_pool)
        result = _run(_engine(game)._execute_action(game, 0, {
            "type": "cast", "card": reanimate.name, "target": "opponent",
        }))
        assert result is None
        assert reanimate in rick.hand
        assert rick.mana_pool == before_pool
