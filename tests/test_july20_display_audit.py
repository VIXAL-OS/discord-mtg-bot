"""July 20 display-parity audit — regression pins.

Findings from four reviewer passes over the unaudited July 12-13 batch and
the July 16 batch, each source-verified. The headline: a refunded FIRST
planeswalker activation permanently poisoned the oracle-text dedup slot, so
the ability's full text was never shown all game — and the refund itself was
a false positive that gave a free activation (real state change + loyalty
back) while telling the player "no legal target".
"""
import asyncio

import pytest


class TestPWRefundAndDedup:
    def _walker(self, make_card):
        w = make_card("Test Walker",
                      type_line="Legendary Planeswalker — Test",
                      power=None, toughness=None, loyalty="3",
                      oracle_text="+1: Draw a card, then put a card from your "
                                  "hand on top of your library.")
        w.loyalty_counters = 3
        return w

    def test_silent_but_real_effect_is_not_refunded(self, game, make_card):
        # game_1526059766965604602: Aminatou's -1 flickered Rhystic Study
        # (no ETB to narrate → empty message list) and was refunded as
        # "no legal target" — a FREE activation. The execution tracker now
        # blocks the refund when a real action executed.
        from rules.planeswalker import PlaneswalkerManager

        rick = game.players[0]
        walker = self._walker(make_card)
        rick.battlefield.append(walker)
        mgr = PlaneswalkerManager()

        async def fake_exec(g, p, c, ability, targets):
            mgr._last_ability_executed_state_change = True
            return []  # real effect, no display text

        mgr._execute_ability = fake_exec
        result = asyncio.run(mgr.activate(game, rick, walker, 0))

        assert result.success is True
        assert walker.loyalty_counters == 4  # +1 charged, NOT refunded
        assert any("Draw a card" in m for m in result.messages)

    def test_refunded_first_attempt_does_not_poison_oracle_dedup(
            self, game, make_card):
        # The header (and its _oracle_shown_keys side effect) used to be
        # built BEFORE the refund check — a refunded attempt consumed the
        # "first use shows full text" slot, so later real activations only
        # ever showed the truncated form.
        from rules.planeswalker import PlaneswalkerManager

        rick = game.players[0]
        walker = self._walker(make_card)
        rick.battlefield.append(walker)
        mgr = PlaneswalkerManager()

        calls = {"n": 0}

        async def fake_exec(g, p, c, ability, targets):
            calls["n"] += 1
            if calls["n"] == 1:
                mgr._last_ability_executed_state_change = False
                return []  # genuinely nothing happened → refund
            mgr._last_ability_executed_state_change = True
            return []

        mgr._execute_ability = fake_exec
        first = asyncio.run(mgr.activate(game, rick, walker, 0))
        assert first.success is False
        assert walker.loyalty_counters == 3  # refunded
        # July 20 wording fix: the refund no longer guesses "no legal
        # target" (it contradicted the real reason for e.g. Daretti +2
        # with an empty hand).
        assert "no legal target" not in first.messages[0]

        second = asyncio.run(mgr.activate(game, rick, walker, 0))
        assert second.success is True
        # Full oracle text must appear — the refunded attempt must not
        # have consumed the first-use slot.
        assert any("put a card from your hand on top" in m
                   for m in second.messages), second.messages


class TestActivateLineTruncation:
    def test_repeat_activation_truncates_at_word_boundary(self, game):
        from mtg.helpers import format_activate_line

        text = ("Discard your hand, then exile the top three cards of your "
                "library. Until end of turn, you may play cards exiled this "
                "way.")
        first = format_activate_line("Chandra, Test", 1, text, game=game)
        assert "Discard your hand" in first
        repeat = format_activate_line("Chandra, Test", 1, text, game=game)
        # July 16 log showed "…Un…" — a mid-word cut. The truncated form
        # must end on a word boundary before the ellipsis.
        assert repeat.endswith("…_")
        body = repeat.split("_")[1]
        assert not body[:-1].endswith(("U", "Un"))
        last_word = body.rstrip("…").split(" ")[-1]
        assert last_word in text
