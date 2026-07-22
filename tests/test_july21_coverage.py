"""July 21 coverage — "for each" aura scaling + the Stifle priority window.

Closes the last two genuinely-unexercised carry-forward items:
- "for each artifact/enchantment" aura scaling (All That Glitters, Ethereal
  Armor) — implemented June 10/11, never batch-exercised (All That Glitters
  was CR 601.2c-gated out of its only live game). Also settles the July 20
  round-2 "+17 vs reconstructed 16" HYPOTHESIS: the aura counts ITSELF (it
  is an enchantment its controller controls), which manual reconstructions
  tend to miss.
- [CAST-TRIGGER-PRIORITY] (APNAP-5, May 20) — the Stifle response window
  had never fired anywhere: no test deck held a Stifle-shape. Pinned here
  headless BEFORE burning batch games on the new test_stifle_talrand deck
  (matchups 140-143).
"""
import asyncio
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


def _engine():
    from mtg.engine import GameEngine
    return GameEngine(None)


ALL_THAT_GLITTERS = ("Enchant creature\nEnchanted creature gets +1/+1 for "
                     "each artifact and/or enchantment you control.")
ETHEREAL_ARMOR = ("Enchant creature\nEnchanted creature gets +1/+1 for each "
                  "enchantment you control and has first strike.")


def _attach(aura, creature):
    aura.attached_to = creature.id
    if aura.id not in creature.attachments:
        creature.attachments.append(aura.id)


class TestForEachAuraScaling:
    def test_all_that_glitters_counts_artifacts_enchantments_and_itself(
            self, make_game, make_card):
        game = make_game()
        rick = game.players[0]
        bears = make_card("Grizzly Bears", type_line="Creature — Bear",
                          power="2", toughness="2")
        glitters = make_card("All That Glitters", type_line="Enchantment — Aura",
                             oracle_text=ALL_THAT_GLITTERS)
        rick.battlefield.extend([bears, glitters])
        _attach(glitters, bears)
        for i in range(3):
            rick.battlefield.append(make_card(f"Relic {i}", type_line="Artifact"))
        rick.battlefield.append(make_card("Rhystic Study", type_line="Enchantment"))

        # 3 artifacts + 1 other enchantment + All That Glitters ITSELF = +5/+5.
        # (The self-count is the whole story behind the July 20 round-2
        # "+17 vs reconstructed 16" hypothesis.)
        assert bears.get_effective_power(game) == 7
        assert bears.get_effective_toughness(game) == 7

    def test_scaling_is_fresh_after_board_change(self, make_game, make_card):
        game = make_game()
        rick = game.players[0]
        bears = make_card("Grizzly Bears", type_line="Creature — Bear",
                          power="2", toughness="2")
        glitters = make_card("All That Glitters", type_line="Enchantment — Aura",
                             oracle_text=ALL_THAT_GLITTERS)
        relic = make_card("Relic", type_line="Artifact")
        rick.battlefield.extend([bears, glitters, relic])
        _attach(glitters, bears)

        assert bears.get_effective_power(game) == 4  # relic + itself

        rick.battlefield.remove(relic)
        rick.graveyard.append(relic)

        assert bears.get_effective_power(game) == 3, \
            "the multiplier must track the live board, not a stale count"

    def test_ethereal_armor_counts_enchantments_only(self, make_game, make_card):
        game = make_game()
        rick = game.players[0]
        bears = make_card("Grizzly Bears", type_line="Creature — Bear",
                          power="2", toughness="2")
        armor = make_card("Ethereal Armor", type_line="Enchantment — Aura",
                          oracle_text=ETHEREAL_ARMOR)
        rick.battlefield.extend([bears, armor])
        _attach(armor, bears)
        rick.battlefield.append(make_card("Rhystic Study", type_line="Enchantment"))
        rick.battlefield.append(make_card("Relic", type_line="Artifact"))

        # armor itself + Rhystic Study = 2; the artifact must NOT count
        assert bears.get_effective_power(game) == 4
        assert bears.get_effective_toughness(game) == 4

    def test_opponents_artifacts_do_not_count(self, make_game, make_card):
        game = make_game()
        rick, claude = game.players
        bears = make_card("Grizzly Bears", type_line="Creature — Bear",
                          power="2", toughness="2")
        glitters = make_card("All That Glitters", type_line="Enchantment — Aura",
                             oracle_text=ALL_THAT_GLITTERS)
        rick.battlefield.extend([bears, glitters])
        _attach(glitters, bears)
        claude.battlefield.append(make_card("Opp Relic", type_line="Artifact"))

        # only the aura itself counts — "you control" is the aura's controller
        assert bears.get_effective_power(game) == 3


class TestStifleWindow:
    def _game_with_talrand_and_stifle(self, make_game, make_card):
        game = make_game()
        rick = game.players[0]
        game.active_player_index = 0
        game.stack_enabled = True

        async def _sink(msg):
            return None
        game._stack_send_func = _sink

        talrand = make_card(
            "Talrand, Sky Summoner",
            type_line="Legendary Creature — Merfolk Wizard",
            power="2", toughness="2",
            oracle_text="Flying\nWhenever you cast an instant or sorcery "
                        "spell, create a 2/2 blue Drake creature token "
                        "with flying.")
        rick.battlefield.append(talrand)
        rick.hand.append(make_card(
            "Stifle", type_line="Instant", mana_cost="{U}", cmc=1,
            oracle_text="Counter target activated or triggered ability."))
        return game, rick

    def test_window_opens_when_caster_holds_stifle_shape(
            self, make_game, make_card, capsys):
        from mtg.triggers import _check_cast_triggers
        engine = _engine()
        game, rick = self._game_with_talrand_and_stifle(make_game, make_card)
        spell = make_card("Opt", type_line="Instant", mana_cost="{U}", cmc=1,
                          oracle_text="Scry 1.\nDraw a card.")

        msgs = asyncio.run(_check_cast_triggers(engine, game, rick, spell))

        out = capsys.readouterr().out
        assert "[CAST-TRIGGER-PRIORITY]" in out, \
            "Stifle in the caster's hand must open the response window"
        # Window passed (nothing countered it) -> trigger resolves inline.
        assert any("Drake" in c.name for c in rick.battlefield), \
            "uncountered trigger must still resolve after the window"

    def test_no_window_without_stifle_shape(self, make_game, make_card, capsys):
        from mtg.triggers import _check_cast_triggers
        engine = _engine()
        game, rick = self._game_with_talrand_and_stifle(make_game, make_card)
        rick.hand.clear()
        spell = make_card("Opt", type_line="Instant", mana_cost="{U}", cmc=1,
                          oracle_text="Scry 1.\nDraw a card.")

        asyncio.run(_check_cast_triggers(engine, game, rick, spell))

        assert "[CAST-TRIGGER-PRIORITY]" not in capsys.readouterr().out


class TestStifleDeckRegistration:
    def test_deck_json_and_registry_are_consistent(self):
        import json
        from mtg.autoplay import AUTOPLAY_DECKS, AUTOPLAY_MATRIX, AUTOPLAY_PHASES

        assert AUTOPLAY_DECKS["stifle"] == "test_stifle_talrand"
        # July 21: was a hardcoded absolute Windows path — passed locally,
        # FileNotFoundError on the Ubuntu CI runner (tests #27-29 all red,
        # one failure email per push).
        d = json.load(open(REPO / "data" / "test_stifle_talrand.json",
                           encoding="utf-8"))
        assert sum(c["quantity"] for c in d["cards"]) == 100
        stifle_shapes = [c["name"] for c in d["cards"]
                         if c["name"] in ("Stifle", "Trickbind", "Tale's End",
                                          "Disallow", "Summary Dismissal",
                                          "Nimble Obstructionist")]
        assert len(stifle_shapes) == 6

        nums = [m[0] for m in AUTOPLAY_MATRIX]
        assert nums == sorted(nums) and len(nums) == len(set(nums)), \
            "matchup numbers must be unique and ordered"
        stifle_matches = [m for m in AUTOPLAY_MATRIX
                          if "stifle" in (m[2], m[3])]
        assert len(stifle_matches) == 6  # 140-143 + the two July 21 reverses
        # the Apr 6 bug class: "all" silently excluding new matchups
        assert AUTOPLAY_PHASES["all"] == (1, max(nums))
        assert AUTOPLAY_PHASES["stifle"] == (140, 145)

    def test_coverage_decks_play_both_seats_against_every_opponent(self):
        # July 21 batch-4 follow-up (user report): deck0 plays the
        # pretend-human code paths, deck1 the AI fast-path — asymmetric-path
        # bugs only surface when a deck's mechanics run through BOTH. The
        # newer coverage decks sat in the human seat for most of their
        # specialty pairings (stifle vs aminatou/sagas, devotion vs
        # aristocrats/layers, etc.), so every distinct opponent must now
        # appear in both orientations.
        from mtg.autoplay import AUTOPLAY_MATRIX
        coverage = {"stifle", "devotion", "combat_keywords", "replacement_chain"}
        pairs = {(m[2], m[3]) for m in AUTOPLAY_MATRIX if m[1] == "commander"}
        missing = []
        for deck in coverage:
            opponents = ({d1 for d0, d1 in pairs if d0 == deck}
                         | {d0 for d0, d1 in pairs if d1 == deck}) - {deck}
            for opp in sorted(opponents):
                if (deck, opp) not in pairs:
                    missing.append(f"{deck} never plays the HUMAN seat vs {opp}")
                if (opp, deck) not in pairs:
                    missing.append(f"{deck} never plays the AI seat vs {opp}")
        assert not missing, "; ".join(missing)
