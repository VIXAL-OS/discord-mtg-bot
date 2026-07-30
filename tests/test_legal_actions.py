"""mtg/legal_actions.py — the single legal-actions provider (July 30, 2026).

The castability computation lived TWICE inside claude_player's prompt
builders and had already diverged (the July 29 split-card fix, adventure
halves, cycling, the token skip, and free-cast effects reached only the
decide_action copy — plan_turn could not see Commit // Memory at the
lethal moment). The React frontend would have been the next divergent
copy. One provider now; both builders consume its labels, the frontend
will consume its structured entries.
"""
import pytest

from mtg.legal_actions import castable_entries, castable_labels


def _mana(**kw):
    m = {"W": 0, "U": 0, "B": 0, "R": 0, "G": 0, "C": 0}
    m.update(kw)
    return m


def _labels(game, player, mana, any_color=0):
    total = sum(mana.values()) + any_color
    return castable_labels(game, player, mana, any_color, total)


class TestHandEntries:
    def test_plain_cast_label_shape(self, make_game, make_card):
        game = make_game()
        rick = game.players[0]
        rick.hand.append(make_card("Grizzly Bears", mana_cost="{1}{G}", cmc=2))
        assert _labels(game, rick, _mana(G=1, C=1)) == [
            "Grizzly Bears ({1}{G})"]

    def test_split_card_castable_via_either_half(self, make_game, make_card):
        # July 29: the combined string parsed as one 10-CMC cost. This fix
        # only reached decide_action — plan_turn consuming the provider is
        # what finally closes the divergence.
        game = make_game()
        rick = game.players[0]
        rick.hand.append(make_card(
            "Commit // Memory", type_line="Instant // Sorcery",
            mana_cost="{3}{U} // {4}{U}{U}", cmc=10))
        labels = _labels(game, rick, _mana(U=1, C=3))
        assert any("Commit // Memory" in l for l in labels)

    def test_suspend_only_and_tokens_skipped(self, make_game, make_card):
        game = make_game()
        rick = game.players[0]
        mox = make_card("Mox Tantalite", type_line="Artifact", mana_cost="",
                        cmc=0, oracle_text="Suspend 3—{0}")
        tok = make_card("Zombie", type_line="Token Creature — Zombie",
                        mana_cost="{1}", cmc=1)
        tok.is_token = True
        rick.hand.extend([mox, tok])
        assert _labels(game, rick, _mana(C=5)) == []

    def test_cycling_surfaced(self, make_game, make_card):
        game = make_game()
        rick = game.players[0]
        rick.hand.append(make_card(
            "Shark Typhoon", type_line="Enchantment",
            mana_cost="{5}{U}", cmc=6,
            oracle_text=("Whenever you cast a noncreature spell, create ...\n"
                         "Cycling {X}{1}{U}\n"
                         "When you cycle Shark Typhoon, create an X/X blue "
                         "Shark creature token with flying.")))
        labels = _labels(game, rick, _mana(U=2, C=2))
        assert any("cycle for" in l for l in labels), labels
        # 6-mana hardcast unaffordable at 4 — cycling is the only entry.
        assert not any(l == "Shark Typhoon ({5}{U})" for l in labels)


class TestOtherZones:
    def test_commander_tax_label(self, make_game, make_card):
        game = make_game()
        rick = game.players[0]
        cmdr = make_card("Jorn, God of Winter",
                         type_line="Legendary Snow Creature — God",
                         mana_cost="{1}{G}{U}", cmc=3)
        cmdr.is_commander = True
        cmdr.times_cast_from_command_zone = 1
        rick.command_zone.append(cmdr)
        labels = _labels(game, rick, _mana(G=1, U=1, C=3))
        assert labels == ["Jorn, God of Winter ({1}{G}{U} +{2} tax) [COMMANDER]"]

    def test_structured_entries_carry_action_shapes(self, make_game, make_card):
        game = make_game()
        rick = game.players[0]
        rick.hand.append(make_card("Bear", mana_cost="{1}{G}", cmc=2))
        entries = castable_entries(game, rick, _mana(G=2), 0, 2)
        assert entries[0]["zone"] == "hand"
        assert entries[0]["action"] == {"type": "cast", "card": "Bear"}

    def test_free_cast_dedupe_against_paid_entry(self, make_game, make_card):
        game = make_game()
        rick = game.players[0]
        rick.hand.append(make_card("Bear", mana_cost="{1}{G}", cmc=2))
        game.turn_effects.append({"type": "free_cast", "controller": 0,
                                  "max_mv": 5, "source": "Sneak Attack"})
        labels = _labels(game, rick, _mana(G=2))
        assert labels == ["Bear ({1}{G})"], (
            "already-affordable cards don't get a duplicate FREE entry")
        # With no mana, only the FREE entry appears.
        labels = _labels(game, rick, _mana())
        assert labels == ["Bear (FREE via Sneak Attack)"]


class TestSingleProvider:
    def test_both_prompt_builders_consume_the_provider(self):
        from pathlib import Path
        src = (Path(__file__).resolve().parent.parent
               / "mtg/claude_player.py").read_text(encoding="utf-8")
        assert src.count("castable_labels(") >= 3, (
            "the import line + both builders")
        assert 'castable_cards.append(f"{card.name} ({card.mana_cost})")' not in src, (
            "an inline castable builder crept back into claude_player — "
            "that is the divergence disease this module exists to end")
