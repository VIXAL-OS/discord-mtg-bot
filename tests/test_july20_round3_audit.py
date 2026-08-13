"""July 20 batch-3 audit — regression pins for the game_15289* batch.

Batch vintage: launched 21:52 EDT July 20 on code as of 67e9a45 — includes
the god-function split, JSON template migration, pub/sub slice 2, and the
July 16 verification-batch fixes, but predates 3e3fc64 and the round-2 wave.

Each test names the game that motivated it. The headline: any meld-pair half
entering the battlefield crashed the creature-ETB scan (frozenset has no
.pop()) — the meld path had never once executed successfully since the
monolith era.
"""
import re
from types import SimpleNamespace

import pytest

from mtg.constants import Phase


def _engine():
    from mtg.engine import GameEngine
    return GameEngine(None)


def _etb_scan(engine, game, player, creature):
    from mtg.triggers import _check_creature_etb_triggers_sync
    return _check_creature_etb_triggers_sync(engine, game, player, creature)


class TestMeldPairEntry:
    def test_meld_half_entering_alone_does_not_crash(self, make_game, make_card):
        # game_1528960244212961350: Gisela, the Broken Blade entered and the
        # [MELD] completion check died on (frozenset - set).pop() —
        # AttributeError killed the whole creature-ETB scan and the game.
        game = make_game()
        rick = game.players[0]
        gisela = make_card("Gisela, the Broken Blade",
                           type_line="Legendary Creature — Angel Horror",
                           power="4", toughness="3")
        rick.battlefield.append(gisela)

        msgs, unhandled = _etb_scan(_engine(), game, rick, gisela)

        assert gisela in rick.battlefield  # no partner -> no meld, no crash

    def test_meld_waits_for_controller_end_step(self, make_game, make_card):
        game = make_game()
        rick = game.players[0]
        bruna = make_card("Bruna, the Fading Light",
                          type_line="Legendary Creature — Angel Horror",
                          power="5", toughness="7")
        gisela = make_card("Gisela, the Broken Blade",
                           type_line="Legendary Creature — Angel Horror",
                           power="4", toughness="3")
        bruna.owner_index = gisela.owner_index = 0
        rick.battlefield.extend([bruna, gisela])

        msgs, unhandled = _etb_scan(_engine(), game, rick, gisela)
        assert [c.name for c in rick.battlefield] == [bruna.name, gisela.name]
        assert not any("meld into" in m for m in msgs)

        from mtg.triggers import _check_end_step_triggers_sync
        end_msgs, _ = _check_end_step_triggers_sync(_engine(), game)
        names = [c.name for c in rick.battlefield]
        assert "Brisela, Voice of Nightmares" in names
        assert bruna in rick.exile and gisela in rick.exile
        assert any("meld into" in m for m in end_msgs)


class TestManaDamageLifeClamp:
    def test_console_print_clamps_negative_life(self, make_game, make_card, capsys):
        # game_1528949395989463172: "[MANA-DAMAGE] Ancient Tomb deals 2 damage
        # to Rick Deckard (life: -1)" — the July 20 pain-land emit site missed
        # the May 19 display clamp (the Discord-facing buffered line already
        # clamped). State keeps the true value per CR 119.3; display shows 0.
        game = make_game()
        rick = game.players[0]
        rick.life = 1
        tomb = make_card("Ancient Tomb", type_line="Land",
                         oracle_text="{T}: Add {C}{C}. Ancient Tomb deals 2 damage to you.")
        rick.battlefield.append(tomb)

        rick.tap_lands_for_mana(2)

        out = capsys.readouterr().out
        assert rick.life == -1  # true value preserved
        assert "life: -" not in out
        assert "(life: 0)" in out


class TestCastFailureMessageThreading:
    # game_1528946209857867968: "[EXECUTE] cast Animate Dead: success=False,
    # msg=Animate Dead can't target Charming Prince — it is not a creature
    # card in a graveyard" — the real reason was printed then DISCARDED
    # (executor returns None), and _get_action_error re-derived "unknown
    # reason — mana looks sufficient" because Animate Dead's oracle text has
    # no literal "target". The AI re-proposed the doomed cast (36× batch-wide;
    # 283 of 588 [MANA-DIVERGENCE] lines were non-mana failures).

    def test_real_failure_message_is_returned_and_consumed(self, make_game):
        from mtg.ai_turn import _get_action_error
        game = make_game()
        real = "Animate Dead can't target Charming Prince — it is not a creature card in a graveyard"
        game._last_cast_failure = (game.turn_number, "Animate Dead", real)

        err = _get_action_error(_engine(), game, 0,
                                {"type": "cast", "card": "Animate Dead"})

        assert err == real
        assert game._last_cast_failure is None  # consumed

    def test_stale_turn_stash_is_ignored(self, make_game):
        from mtg.ai_turn import _get_action_error
        game = make_game()
        game._last_cast_failure = (game.turn_number - 1, "Animate Dead", "stale reason")

        err = _get_action_error(_engine(), game, 0,
                                {"type": "cast", "card": "Animate Dead"})

        assert err != "stale reason"

    def test_different_card_stash_is_ignored(self, make_game):
        from mtg.ai_turn import _get_action_error
        game = make_game()
        game._last_cast_failure = (game.turn_number, "Lightning Bolt", "bolt reason")

        err = _get_action_error(_engine(), game, 0,
                                {"type": "cast", "card": "Animate Dead"})

        assert err != "bolt reason"


class TestGatekeeperOfMalakir:
    # game_1528960166047780874 / game_1528960181692534949: [ETB-UNHANDLED]
    # for the kicked-conditional ETB. Kicker payment isn't modeled, so the
    # intervening if (CR 603.4) is never met today — the template must
    # resolve as an explicit no-op, not fall through to UNHANDLED.
    ORACLE = ("Kicker {B}\nWhen this creature enters, if it was kicked, "
              "target player sacrifices a creature of their choice.")

    def test_unkicked_etb_resolves_as_explicit_no_op(self, lib):
        actions, desc = lib.resolve_etb("Gatekeeper of Malakir", self.ORACLE,
                                        "Rick Deckard", "Claude")
        assert actions, "template must claim the ETB (no UNHANDLED fallthrough)"
        assert actions[0]["action"] == "no_action"
        assert "not kicked" in actions[0]["reason"]

    def test_kicked_context_forces_sacrifice(self, lib):
        tpl = lib._card_templates["gatekeeper of malakir"]
        actions = tpl.action_generator(
            "Rick Deckard", "Claude",
            {"kicked": True, "worst_opponent_creature": "Llanowar Elves"})
        assert actions[0]["action"] == "sacrifice_permanent"
        assert actions[0]["player"] == "Claude"


class TestPactResponseGuard:
    def test_pact_filtered_when_upkeep_cost_unpayable(self, make_game, make_card):
        # game_1528942795019255889: Claude countered a turn-3 Sylvan Library
        # with Pact of Negation on 3 mana sources, then auto-lost at his
        # upkeep to the unpayable {3}{U}{U}. Engine correct; the response
        # filter must not offer a pact whose followup cost exceeds what the
        # battlefield could produce even fully untapped.
        import asyncio
        from mtg.claude_player import ClaudePlayer

        game = make_game()
        claude = game.players[1]
        pact = make_card("Pact of Negation", type_line="Instant",
                         mana_cost="{0}", cmc=0,
                         oracle_text="Counter target spell. At the beginning of "
                                     "your next upkeep, pay {3}{U}{U}. If you "
                                     "don't, you lose the game.")
        claude.hand.append(pact)
        for i in range(3):
            isl = make_card(f"Island {i}", type_line="Basic Land — Island",
                            oracle_text="{T}: Add {U}.")
            claude.battlefield.append(isl)

        ai = ClaudePlayer(None)  # no client — must return before any LLM call
        result = asyncio.run(ai.decide_response(game, 1, "Sylvan Library", "Rick Deckard"))

        assert result is None  # pact filtered -> nothing affordable -> pass
