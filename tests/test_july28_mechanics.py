"""July 28, 2026 — Phase 2, the "whole mechanic appears unimplemented" cluster.

A2  ESCAPE WAS COMPLETELY DEAD, and the decisive cause was NOT the one reported.
    The reviewer's chain was upstream-blind and one of its four sub-claims was
    flatly wrong (the exile payment does exist in all three cast paths). The
    real cause: four separate copies of the detection regex all required
    `(\\d+)` for the exile count, and Scryfall spells it as an English WORD on
    every printing — "Exile five other cards", "three", "four", "eight". The
    pattern matched 0 of the 7 escape cards in the cache, so detection never
    fired and every downstream branch was unreachable dead code.

    `was_escaped` was read in two places and written nowhere in production; the
    two tests that exercised it injected it by hand, so the pair stayed green
    over a dead path — the same trap as game._rules_engine. Escaping Kroxa now
    actually spares him from his own sacrifice clause, which has never once
    happened in this codebase before today.

A3  "Discard a card" as an activation cost was never paid, in BOTH activation
    paths — while the cog's parser explicitly ACCEPTS "Discard" as a cost
    keyword, so the ability was offered and then only its {T} charged. Anje
    Falkenrath, the commander of the madness deck, was a free "{T}: Draw a
    card", and because the discard never happened her own "whenever you discard
    a card, if it has madness, untap" trigger could never fire either.

A4  Heroic triggers are skipped by design (targeting isn't fully tracked), but
    the skip was SILENT: a bare `return False` feeding three bare `continue`s,
    so it never reached the unhandled queue and no audit grep could see it. An
    approximation nobody can see is indistinguishable from a bug.

A5  Unmatched "deals combat damage to a player" triggers were dropped with no
    message, no tag and no queue — resolve_combat_damage is sync, so it has no
    Tier-3 escalation, and nothing queued the tail the way the dies and cast
    scans do. Ragavan's entire trigger vanished on every connect.

D3  Mishra's Bauble's delayed draw was lost — but again not for the reported
    reason. The judge's library-look shortcut provably cannot fire for it
    ("draw" is in the text). A correct handler has existed on the manual
    !activate path since June; the AI/autoplay executor simply never got one.
    Third instance of the two-activation-paths divergence.
"""
import json
from pathlib import Path

import pytest


_CACHE = Path(__file__).resolve().parent.parent / "data" / "card_data_cache.json"


def _cache():
    if not _CACHE.exists():
        pytest.skip("card_data_cache.json not present")
    with open(_CACHE, encoding="utf-8") as fh:
        return json.load(fh)


def _oracle(key):
    entry = _cache().get(key)
    if entry is None:
        pytest.skip(f"{key!r} not in the card cache")
    return entry.get("oracle_text") or ""


# ---------------------------------------------------------------------------
# A2 — escape
# ---------------------------------------------------------------------------

class TestEscapeDetection:

    def test_every_escape_card_in_the_cache_parses(self):
        """The decisive pin. This failed for ALL of them before the fix, because
        the count is printed as a word and the regex demanded digits."""
        from mtg.helpers import parse_escape_cost
        cards = {k: v.get("oracle_text") or "" for k, v in _cache().items()
                 if "escape" in (v.get("oracle_text") or "").lower()}
        # Underworld Breach GRANTS escape to other cards rather than having it,
        # so it legitimately has no escape cost of its own.
        havers = {k: t for k, t in cards.items() if "escape—" in t.lower()
                  or "escape " in t.lower().split("\n")[-1][:8]}
        assert havers, "no escape cards in the cache to test"
        for key, text in havers.items():
            parsed = parse_escape_cost(text)
            assert parsed is not None, f"{key} still does not parse"
            cost, count = parsed
            assert cost.startswith("{") and count >= 1

    def test_word_counts_and_digits_both_work(self):
        from mtg.helpers import parse_escape_cost
        assert parse_escape_cost(
            "Escape—{B}{B}{R}{R}, Exile five other cards from your graveyard.")[1] == 5
        assert parse_escape_cost(
            "Escape—{2}{R}, Exile 3 other cards from your graveyard.")[1] == 3

    def test_kroxas_real_cost_is_recovered(self):
        from mtg.helpers import parse_escape_cost
        cost, count = parse_escape_cost(_oracle("kroxa, titan of death's hunger"))
        assert cost == "{B}{B}{R}{R}" and count == 5

    def test_a_card_that_only_grants_escape_is_not_claimed(self):
        from mtg.helpers import parse_escape_cost
        assert parse_escape_cost(_oracle("underworld breach")) is None


class TestWasEscapedFinallyHasAProducer:

    def test_context_publishes_the_flag(self, game, make_card):
        from rules.effect_templates import build_game_context
        rick, claude = game.players
        kroxa = make_card("Kroxa, Titan of Death's Hunger",
                          oracle_text=_oracle("kroxa, titan of death's hunger"))
        ctx = build_game_context(game, rick, claude, card=kroxa)
        assert ctx.get("was_escaped") is False
        kroxa._was_escaped = True
        ctx2 = build_game_context(game, rick, claude, card=kroxa)
        assert ctx2.get("was_escaped") is True

    def test_hardcast_kroxa_sacrifices_himself(self, game, lib, make_card):
        from rules.effect_templates import build_game_context
        rick, claude = game.players
        claude.hand.append(make_card("Swamp", type_line="Basic Land — Swamp"))
        kroxa = make_card("Kroxa, Titan of Death's Hunger",
                          oracle_text=_oracle("kroxa, titan of death's hunger"))
        ctx = build_game_context(game, rick, claude, card=kroxa)
        actions, _d = lib.resolve_etb(kroxa.name, kroxa.oracle_text,
                                      rick.name, claude.name, ctx)
        assert "sacrifice_permanent" in [a["action"] for a in actions]

    def test_escaped_kroxa_survives(self, game, lib, make_card):
        from rules.effect_templates import build_game_context
        rick, claude = game.players
        claude.hand.append(make_card("Swamp", type_line="Basic Land — Swamp"))
        kroxa = make_card("Kroxa, Titan of Death's Hunger",
                          oracle_text=_oracle("kroxa, titan of death's hunger"))
        kroxa._was_escaped = True
        ctx = build_game_context(game, rick, claude, card=kroxa)
        actions, _d = lib.resolve_etb(kroxa.name, kroxa.oracle_text,
                                      rick.name, claude.name, ctx)
        assert "sacrifice_permanent" not in [a["action"] for a in actions], (
            "the whole point of escaping him")

    def test_escape_cost_is_charged_not_the_printed_cost(self, game, make_card):
        """_compute_alt_costs had no escape branch, so even with detection fixed
        an escaped Kroxa would have been charged his printed {B}{R}."""
        from mtg.engine import GameEngine
        from mtg.spells import _compute_alt_costs
        kroxa = make_card("Kroxa, Titan of Death's Hunger",
                          mana_cost="{B}{R}", cmc=2,
                          oracle_text=_oracle("kroxa, titan of death's hunger"))
        kroxa._escape_cost = "{B}{B}{R}{R}"
        _early, costs = _compute_alt_costs(
            GameEngine(None), game, game.players[0], kroxa,
            pay_mana=True, additional_cost=0)
        assert _early is None, "cost selection should not bail out early here"
        blob = repr(costs)
        assert "{B}{B}{R}{R}" in blob, f"escape cost not selected: {blob}"

    def test_the_marker_survives_payment_for_the_etb_to_read(self, game, make_card):
        """Unlike _flashback_cost, _escape_cost must NOT be cleared at payment:
        the ETB reads was_escaped afterwards."""
        from mtg.engine import GameEngine
        from mtg.spells import _compute_alt_costs
        kroxa = make_card("Kroxa, Titan of Death's Hunger", mana_cost="{B}{R}",
                          oracle_text=_oracle("kroxa, titan of death's hunger"))
        kroxa._escape_cost = "{B}{B}{R}{R}"
        kroxa._was_escaped = True
        _compute_alt_costs(GameEngine(None), game, game.players[0], kroxa,
                           pay_mana=True, additional_cost=0)
        assert kroxa._was_escaped is True

    def test_escape_fields_are_declared_not_stapled(self):
        from dataclasses import fields

        from mtg.models import Card
        names = {f.name for f in fields(Card)}
        assert {"_escape_cost", "_was_escaped"} <= names, (
            "the bug WAS a flag with no writer — declare it so it's findable")


# ---------------------------------------------------------------------------
# A3 — discard as an activation cost
# ---------------------------------------------------------------------------

class TestDiscardActivationCost:

    def _anje(self, make_card):
        return make_card("Anje Falkenrath",
                         type_line="Legendary Creature — Vampire",
                         power="1", toughness="3",
                         oracle_text=_oracle("anje falkenrath"))

    def test_ai_path_pays_the_discard(self, make_game, make_card):
        import asyncio

        from mtg.engine import GameEngine
        engine = GameEngine(None)
        game = make_game()
        game._rules_engine = engine.rules
        engine.rules.engine_ref = engine
        rick = game.players[0]
        anje = self._anje(make_card)
        rick.battlefield.append(anje)
        rick.hand.extend([make_card("Mountain", type_line="Basic Land — Mountain"),
                          make_card("Big Spell", type_line="Sorcery", cmc=7)])
        rick.library.append(make_card("Island", type_line="Basic Land — Island"))
        asyncio.run(engine._execute_action(
            game, 0, {"type": "activate", "permanent": "Anje Falkenrath", "ability": 0}))
        assert len(rick.graveyard) == 1, "the discard cost must actually be paid"
        assert len(rick.hand) == 2, "net: one discarded, one drawn"

    def test_empty_hand_cannot_activate_and_leaves_it_untapped(self, make_game, make_card):
        import asyncio

        from mtg.engine import GameEngine
        engine = GameEngine(None)
        game = make_game()
        game._rules_engine = engine.rules
        engine.rules.engine_ref = engine
        rick = game.players[0]
        anje = self._anje(make_card)
        rick.battlefield.append(anje)
        rick.library.append(make_card("Island", type_line="Basic Land — Island"))
        before = len(rick.hand)
        asyncio.run(engine._execute_action(
            game, 0, {"type": "activate", "permanent": "Anje Falkenrath", "ability": 0}))
        assert len(rick.hand) == before, "no card may be drawn without paying"
        assert not anje.tapped, "a failed activation must roll back the tap"

    def test_both_activation_paths_have_the_branch(self):
        """These two paths have a documented history of diverging; this is the
        third instance, so pin them together."""
        root = Path(__file__).resolve().parent.parent
        for rel in ("mtg/engine.py", "mtg/cog.py"):
            src = (root / rel).read_text(encoding="utf-8")
            assert "discard" in src.lower()
            assert "ACTIVATE-COST" in src, f"{rel} has no cost tag to grep"


# ---------------------------------------------------------------------------
# A4 / A5 — silent drops become greppable
# ---------------------------------------------------------------------------

class TestSilentSkipsAreNowVisible:

    def test_heroic_skip_announces_itself(self, capsys):
        from mtg.triggers import _spell_matches_cast_trigger
        import inspect
        src = inspect.getsource(_spell_matches_cast_trigger)
        assert "[HEROIC-SKIP]" in src, (
            "the skip is a deliberate approximation, but it must be visible")

    def test_combat_damage_trigger_queue_helper_exists(self):
        from mtg.triggers import queue_unhandled_combat_damage
        assert callable(queue_unhandled_combat_damage)

    def test_unmatched_combat_trigger_is_tagged(self, game, make_card, capsys):
        from mtg.triggers import queue_unhandled_combat_damage
        rick = game.players[0]
        ragavan = make_card(
            "Ragavan, Nimble Pilferer", power="2", toughness="1",
            oracle_text=("Whenever Ragavan deals combat damage to a player, create "
                         "a Treasure token and exile the top card of that player's "
                         "library. Until end of turn, you may cast that card."))
        capsys.readouterr()
        queue_unhandled_combat_damage(game, ragavan, rick, 2)
        out = capsys.readouterr().out
        assert "[COMBAT-TRIGGER-UNHANDLED]" in out
        assert "Ragavan" in out

    def test_combat_py_routes_unmatched_triggers_there(self):
        import inspect

        from mtg import combat
        src = inspect.getsource(combat)
        assert "queue_unhandled_combat_damage" in src


# ---------------------------------------------------------------------------
# D3 — Mishra's Bauble
# ---------------------------------------------------------------------------

class TestMishrasBaubleDelayedDraw:

    def test_ai_path_schedules_the_draw(self, make_game, make_card):
        import asyncio

        from mtg.engine import GameEngine
        engine = GameEngine(None)
        game = make_game()
        game._rules_engine = engine.rules
        engine.rules.engine_ref = engine
        rick = game.players[0]
        bauble = make_card("Mishra's Bauble", type_line="Artifact",
                           power=None, toughness=None,
                           oracle_text=_oracle("mishra's bauble"))
        rick.battlefield.append(bauble)
        rick.library.append(make_card("Island", type_line="Basic Land — Island"))
        asyncio.run(engine._execute_action(
            game, 0, {"type": "activate", "permanent": "Mishra's Bauble", "ability": 0}))
        scheduled = [d for d in (game.delayed_triggers or [])
                     if d.get("source") == "Mishra's Bauble"]
        assert scheduled, "the delayed draw was lost on the AI/autoplay path"
        assert scheduled[0]["trigger_at"] == "upkeep"

    def test_no_owner_gate_on_the_next_turns_upkeep(self, make_game, make_card):
        """The card says "the next turn's upkeep", whoever's turn that is —
        unlike Necropotence's "YOUR next end step", which IS gated."""
        import asyncio

        from mtg.engine import GameEngine
        engine = GameEngine(None)
        game = make_game()
        game._rules_engine = engine.rules
        engine.rules.engine_ref = engine
        rick = game.players[0]
        bauble = make_card("Mishra's Bauble", type_line="Artifact",
                           power=None, toughness=None,
                           oracle_text=_oracle("mishra's bauble"))
        rick.battlefield.append(bauble)
        rick.library.append(make_card("Island", type_line="Basic Land — Island"))
        asyncio.run(engine._execute_action(
            game, 0, {"type": "activate", "permanent": "Mishra's Bauble", "ability": 0}))
        sched = next(d for d in game.delayed_triggers
                     if d.get("source") == "Mishra's Bauble")
        assert sched.get("upkeep_of") is None

    def test_the_judge_shortcut_never_could_have_handled_it(self):
        """Pins the corrected mechanism: the reported cause was wrong, because
        "draw" appears in the text and the shortcut excludes that."""
        text = _oracle("mishra's bauble").lower()
        assert "draw" in text
        assert "look at the top" in text


# ---------------------------------------------------------------------------
# A6 — Dryad of the Ilysian Grove's second half
# ---------------------------------------------------------------------------

class TestLandsAreEveryBasicLandType:
    """Only Dryad's extra-land-drop half was implemented. The type-adding half
    existed nowhere, and nothing could have consumed it if it had — mana
    production is derived per card with no static-effect consultation. In a
    four-colour deck that half IS the card.

    Scoped by the controller's own battlefield rather than by threading `game`
    through the mana engine: the effect reads "lands YOU control", and the mana
    engine is this project's most regression-prone area, so the smaller change
    is the safer one.
    """

    def _dryad(self, make_card):
        return make_card(
            "Dryad of the Ilysian Grove", type_line="Creature — Dryad",
            oracle_text=("You may play an additional land on each of your turns.\n"
                         "Lands you control are every basic land type in addition "
                         "to their other types."))

    def test_a_forest_taps_for_any_color(self, game, make_card):
        rick = game.players[0]
        forest = make_card("Forest", type_line="Basic Land — Forest")
        rick.battlefield.append(forest)
        assert rick._get_mana_production(forest) == {"G": 1}
        rick.battlefield.append(self._dryad(make_card))
        assert rick._get_mana_production(forest) == {"any": 1}

    def test_the_player_visible_failure(self, game, make_card):
        """A lone Forest could not pay {U} — the castable list and
        can_pay_mana_cost rejected casts that are legal on the real board."""
        rick = game.players[0]
        rick.battlefield.append(make_card("Forest", type_line="Basic Land — Forest"))
        assert rick.can_pay_mana_cost("{U}")[0] is False
        rick.battlefield.append(self._dryad(make_card))
        assert rick.can_pay_mana_cost("{U}")[0] is True

    def test_an_opponents_dryad_does_not_fix_your_lands(self, game, make_card):
        rick, claude = game.players
        rick.battlefield.append(self._dryad(make_card))
        forest = make_card("Forest", type_line="Basic Land — Forest")
        claude.battlefield.append(forest)
        assert claude._get_mana_production(forest) == {"G": 1}

    def test_unusual_lands_keep_their_own_output(self, game, make_card):
        """A land that taps for something special is not suddenly a one-mana
        rainbow — this is why the branch sits after the special lands."""
        rick = game.players[0]
        tomb = make_card("Ancient Tomb", type_line="Land")
        fetch = make_card("Polluted Delta", type_line="Land")
        rick.battlefield.extend([tomb, fetch])
        rick.battlefield.append(self._dryad(make_card))
        assert rick._get_mana_production(tomb) == {"C": 2}
        assert rick._get_mana_production(fetch) == {"C": 0}

    def test_prismatic_omen_gets_it_free(self, game, make_card):
        """Matched on the printed phrase, not the card name, so any card with
        the same wording is covered."""
        rick = game.players[0]
        forest = make_card("Forest", type_line="Basic Land — Forest")
        rick.battlefield.append(forest)
        rick.battlefield.append(make_card(
            "Prismatic Omen", type_line="Enchantment",
            oracle_text="Lands you control are every basic land type in addition "
                        "to their other types."))
        assert rick._get_mana_production(forest) == {"any": 1}

    def test_nonlands_are_unaffected(self, game, make_card):
        rick = game.players[0]
        rock = make_card("Sol Ring", type_line="Artifact")
        rick.battlefield.extend([rock, self._dryad(make_card)])
        assert rick._get_mana_production(rock) == {"C": 2}
