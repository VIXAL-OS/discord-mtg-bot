"""Aug 2, 2026 — missing KEYWORD MECHANICS, wave 2: the condition consumers.

Wave 1 built has_morbid / has_metalcraft / has_coven predicates and wired
consumers for NONE of them. That is its own bug shape — machinery connected
to nothing, the same species as `game._rules_engine` being assigned only in
tests (July 21). A verified-probe pass over every remaining candidate then
confirmed all three cards resolve NOTHING at any event type:

  Reaper from the Abyss  — a 6-mana 6/6 whose entire payoff is "Morbid — at
    the beginning of each end step, if a creature died this turn, destroy
    target non-Demon creature". It fired never.
  Puresteel Paladin      — neither half worked: no equipment-ETB draw, and
    metalcraft's equip {0} is a SET-TO-ZERO that the existing subtractive
    equip-cost reducer structurally could not express.
  Polukranos, World Eater — monstrosity (CR 701.32) had no handling, so the
    becomes-monstrous trigger could not fire either.

The probe pass is also the methodological point of this wave: grep evidence
is unreliable in BOTH directions (Hero of Bladehold looked unhandled and was
half-handled; Traverse the Ulvenwald looked half-handled and was completely
broken), so each of these was confirmed by asking the engine what the card
actually does before a line was written.
"""
import json

import pytest

from mtg.helpers import has_morbid, has_metalcraft


def _cache():
    return json.load(open("data/card_data_cache.json", encoding="utf-8"))


def _real(make_card, name, **kw):
    e = _cache()[name.lower()]
    return make_card(e.get("name", name), type_line=e["type_line"],
                     oracle_text=e["oracle_text"],
                     power=e.get("power") or "1",
                     toughness=e.get("toughness") or "1",
                     mana_cost=e["mana_cost"], **kw)


def _ctx(game, **kw):
    from rules.effect_templates import build_game_context
    return build_game_context(game, game.players[0], game.players[1], **kw)


class TestReaperFromTheAbyss:
    ORACLE_KEY = "reaper from the abyss"

    def _resolve(self, game, lib):
        return lib.resolve_etb(
            "Reaper from the Abyss", _cache()[self.ORACLE_KEY]["oracle_text"],
            game.players[0].name, game.players[1].name,
            game_context=_ctx(game), event_type="end_step")[0]

    def test_declines_when_nothing_died(self, game, lib, make_card):
        game.players[1].battlefield.append(
            make_card("Victim", power="3", toughness="3"))
        a = self._resolve(game, lib)
        assert a and a[0]["action"] == "no_action"

    def test_destroys_when_a_creature_died_this_turn(self, game, lib,
                                                     make_card):
        game._creature_died_this_turn = True
        game.players[1].battlefield.append(
            make_card("Victim", power="3", toughness="3"))
        a = self._resolve(game, lib)
        assert [x["action"] for x in a] == ["destroy"]
        assert a[0]["card"] == "Victim"

    def test_never_targets_a_demon(self, game, lib, make_card):
        """"target non-Demon creature" — and the Demon here is the biggest
        thing on the board, so a naive best-target pick would take it."""
        game._creature_died_this_turn = True
        claude = game.players[1]
        claude.battlefield.append(make_card(
            "Big Demon", type_line="Creature — Demon", power="9",
            toughness="9"))
        claude.battlefield.append(make_card("Plain Bear", power="2",
                                            toughness="2"))
        a = self._resolve(game, lib)
        assert a[0]["action"] == "destroy" and a[0]["card"] == "Plain Bear"

    def test_declines_when_only_demons_remain(self, game, lib, make_card):
        game._creature_died_this_turn = True
        game.players[1].battlefield.append(make_card(
            "Big Demon", type_line="Creature — Demon", power="9",
            toughness="9"))
        a = self._resolve(game, lib)
        assert a and a[0]["action"] == "no_action"


class TestPolukranosMonstrosity:
    def _resolve(self, game, lib, x):
        p = _real(lambda n, **k: __import__("mtg.models", fromlist=["Card"]).Card(
            name=n, id="polu", **k), "Polukranos, World Eater")
        ctx = _ctx(game, card=p)
        ctx["x_value"] = x
        return lib.resolve_etb(
            "Polukranos, World Eater",
            _cache()["polukranos, world eater"]["oracle_text"],
            game.players[0].name, game.players[1].name, game_context=ctx)[0]

    def test_x_zero_does_nothing(self, game, lib):
        a = self._resolve(game, lib, 0)
        assert a and a[0]["action"] == "no_action"
        # The REASON distinguishes "you paid X=0" from "X could not finish
        # anything" — a player seeing the wrong one is told a different
        # (and untrue) story about their own activation.
        assert "X was 0" in a[0]["reason"], a

    def test_spends_x_on_what_it_can_actually_finish(self, game, lib,
                                                     make_card):
        claude = game.players[1]
        claude.battlefield.append(make_card("Small", power="2", toughness="2"))
        claude.battlefield.append(make_card("Huge", power="8", toughness="8"))
        a = self._resolve(game, lib, 3)
        dmg = [x for x in a if x.get("target_card") == "Small"]
        assert dmg and dmg[0]["amount"] == 2, a
        assert not [x for x in a if x.get("target_card") == "Huge"], (
            "3 damage cannot finish an 8/8 — spending it there is a waste")

    def test_each_damaged_creature_deals_its_power_back(self, game, lib,
                                                        make_card):
        """The half that makes Polukranos a real decision rather than free
        removal."""
        claude = game.players[1]
        claude.battlefield.append(make_card("Small", power="2", toughness="2"))
        a = self._resolve(game, lib, 3)
        back = [x for x in a
                if x.get("target_card") == "Polukranos, World Eater"]
        assert back and back[0]["amount"] == 2, a

    def test_x_too_small_for_anything_declines(self, game, lib, make_card):
        game.players[1].battlefield.append(
            make_card("Wall", power="0", toughness="9"))
        a = self._resolve(game, lib, 1)
        assert a and a[0]["action"] == "no_action"


class TestPuresteelPaladin:
    def test_equipment_etb_draws(self, game, lib):
        a = lib.resolve_etb(
            "Puresteel Paladin", _cache()["puresteel paladin"]["oracle_text"],
            game.players[0].name, game.players[1].name,
            game_context=_ctx(game))[0]
        assert a == [{"action": "draw_cards", "player": "Rick", "amount": 1}]

    def test_metalcraft_sets_equip_to_zero(self):
        """A SET-TO-ZERO, which the subtractive equip-cost reducer could not
        express — so this half had no implementation path at all."""
        import inspect
        import mtg.engine
        src = inspect.getsource(mtg.engine)
        i = src.index("METALCRAFT (CR 702.60)")
        window = src[i:i + 900]
        assert "has_metalcraft(player)" in window, (
            "the equip path must consult the predicate")
        assert 'cost_str = "{0}"' in window, (
            "equip {0} is a set-to-zero, not a reduction")

    def test_the_predicate_gates_it(self, game, make_card):
        rick = game.players[0]
        for i in range(2):
            rick.battlefield.append(make_card(f"a{i}", type_line="Artifact"))
        assert not has_metalcraft(rick)
        rick.battlefield.append(make_card("a3", type_line="Artifact"))
        assert has_metalcraft(rick)


class TestPredicatesHaveConsumers:
    """Wave 1's actual defect: predicates built, consumers wired for none.

    Machinery connected to nothing is the `game._rules_engine` class — it
    looks done, passes its own unit tests, and never executes in a game.
    """

    @pytest.mark.parametrize("predicate,consumer_file", [
        ("has_morbid", "rules/effect_templates.py"),
        ("has_metalcraft", "mtg/engine.py"),
        ("has_delirium", "rules/effect_templates.py"),
    ])
    def test_each_predicate_is_actually_called_in_production(
            self, predicate, consumer_file):
        src = open(consumer_file, encoding="utf-8").read()
        assert f"{predicate}(" in src, (
            f"{predicate} has no production consumer — a predicate nothing "
            f"calls is indistinguishable from an unimplemented mechanic")
