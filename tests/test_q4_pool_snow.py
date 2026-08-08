"""Aug 7, 2026 queue item Q4 — snow provenance for FLOATING pool mana.

Before Q4, `_last_payment['snow_spent']` was exact for mana consumed from
sources tapped by the payment engine, but the floating pool stored only
color totals: mana floated BEFORE a payment (Phase-4 settle excess,
[ACTIVATE-MANA], rituals, Tier-3 add_mana) was conservatively counted as
non-snow. Blood on the Snow was the live undercounting consumer.

The fix: a declared `Player._pool_snow` shadow dict tags the KNOWN-snow
portion of `mana_pool` per color. Hard rules pinned here:

- a tag can never exceed the pool's own count for that color;
- Phase-4's pool spend debits PRE-EXISTING tags only — freshly-floated
  snow excess is credited AFTER the debit, so it can never masquerade as
  spent pool snow (the overcount direction is forbidden; undercount is
  the documented safe direction);
- non-snow producers (plain sources, the Mirari's Wake tap bonus) never
  tag;
- the tags die with the pool (`empty_mana_pool`).

Fixture discipline (the Aug-1 Bloom Tender payment-ledger lesson): every
scenario runs REAL cost shapes through `tap_sources_for_cost` /
`tap_lands_for_mana` / the action interpreter — the paths production
takes — and each load-bearing pin has a fixture that diverges on exactly
the gate it names.
"""

import asyncio

from mtg.engine import GameEngine
from mtg.models import Player


def _engine(game):
    engine = GameEngine(None)
    game._rules_engine = engine.rules
    engine.rules.engine_ref = engine
    return engine


def _snow_ring(make_card, name="Snow-Covered Sol Ring"):
    """Two-mana snow rock — overproduces so excess floats."""
    return make_card(name, type_line="Snow Artifact",
                     oracle_text="{T}: Add {C}{C}.",
                     power=None, toughness=None)


def _plain_ring(make_card, name="Sol Ring"):
    return make_card(name, type_line="Artifact",
                     oracle_text="{T}: Add {C}{C}.",
                     power=None, toughness=None)


def _snow_cradle(make_card):
    """Snow Gaea's Cradle — a REAL multi-mana production shape ({G} per
    creature via the name-keyed branch; the generic oracle scan is a flag
    scan, so an invented "{T}: Add {B}{B}." card would produce only 1 and
    the ordering fixture would silently stop floating any excess)."""
    return make_card("Gaea's Cradle", type_line="Snow Land",
                     oracle_text="{T}: Add {G} for each creature you control.",
                     power=None, toughness=None)


def _snow_swamp(make_card):
    """A real snow basic — name is in rules.mana.SNOW_LANDS."""
    return make_card("Snow-Covered Swamp",
                     type_line="Basic Snow Land — Swamp",
                     oracle_text="{T}: Add {B}.",
                     power=None, toughness=None)


class TestSettledExcessProvenance:
    def test_snow_excess_floats_tagged_and_spends_as_snow(
            self, make_game, make_card):
        """End-to-end: a snow rock's excess floats with a tag, and the NEXT
        payment's Phase-0 pool spend counts it as snow."""
        game = make_game()
        rick = game.players[0]
        rick.battlefield.append(_snow_ring(make_card))

        assert rick.tap_sources_for_cost("{1}", game=game)
        # Consumed 1 from the snow rock; its second mana floated, tagged.
        assert rick._last_payment["snow_spent"] == 1
        assert rick.mana_pool["C"] == 1
        assert rick._pool_snow.get("C", 0) == 1

        # The rock is tapped now — this payment is pool-only.
        assert rick.tap_sources_for_cost("{1}", game=game)
        assert rick._last_payment["snow_spent"] == 1
        assert rick.mana_pool["C"] == 0
        assert rick._pool_snow.get("C", 0) == 0

    def test_nonsnow_excess_never_tagged(self, make_game, make_card):
        """The snow gate on the excess credit: a PLAIN overproducer's float
        must spend as non-snow later."""
        game = make_game()
        rick = game.players[0]
        rick.battlefield.append(_plain_ring(make_card))

        assert rick.tap_sources_for_cost("{1}", game=game)
        assert rick.mana_pool["C"] == 1
        assert rick._pool_snow.get("C", 0) == 0

        assert rick.tap_sources_for_cost("{1}", game=game)
        assert rick._last_payment["snow_spent"] == 0

    def test_pool_spend_debits_preexisting_tags_not_fresh_excess(
            self, make_game, make_card):
        """THE ORDERING PIN (debit before credit). Pool holds 1 UNTAGGED
        {B}; the payment spends that pool {B} AND floats 1 fresh snow {B}
        of excess in the same settle. The spent pool mana was the old
        non-snow {B}, so snow_spent must be exactly the 1 consumed from
        the tap — a credit-before-debit implementation reports 2 (the
        forbidden overcount), because the freshly-tagged excess gets
        debited as if it had been spent."""
        game = make_game()
        rick = game.players[0]
        # 1 pre-existing NON-snow G in the pool.
        rick.grant_pool_mana("G", 1, source=_plain_ring(make_card))
        assert rick._pool_snow.get("G", 0) == 0
        # Snow source producing {G}{G} (Cradle + two creatures).
        rick.battlefield.append(_snow_cradle(make_card))
        rick.battlefield.append(make_card("Bear One"))
        rick.battlefield.append(make_card("Bear Two"))

        # {G}{G}: Phase-0 spends the pool G, the tap covers the other pip
        # (consuming 1 of 2 produced), and the leftover snow G floats.
        assert rick.tap_sources_for_cost("{G}{G}", game=game)
        assert rick._last_payment["snow_spent"] == 1
        # The floated snow G is tagged for the NEXT payment.
        assert rick.mana_pool["G"] == 1
        assert rick._pool_snow.get("G", 0) == 1
        assert rick.tap_sources_for_cost("{G}", game=game)
        assert rick._last_payment["snow_spent"] == 1

    def test_wake_bonus_mana_stays_untagged(self, make_game, make_card):
        """The Mirari's Wake tap bonus is from the WAKE, not the land —
        deliberately non-snow even when the tapped land is snow."""
        game = make_game()
        rick = game.players[0]
        rick.battlefield.append(_snow_swamp(make_card))
        rick.battlefield.append(make_card(
            "Mirari's Wake", type_line="Enchantment",
            oracle_text=("Creatures you control get +1/+1. Whenever you "
                         "tap a land for mana, add one mana of any type "
                         "that land produced."),
            power=None, toughness=None))

        # Exact cost — the swamp's own mana is fully consumed (snow_spent
        # 1), and the only float is the Wake bonus.
        assert rick.tap_sources_for_cost("{B}", game=game)
        assert rick._last_payment["snow_spent"] == 1
        assert rick.mana_pool["B"] == 1
        assert rick._pool_snow.get("B", 0) == 0

        assert rick.tap_sources_for_cost("{1}", game=game)
        assert rick._last_payment["snow_spent"] == 0


class TestPoolSnowHelpers:
    def test_credit_clamps_to_pool_count(self):
        """HARD RULE: a tag can never exceed what the pool holds."""
        p = Player(name="P")
        p.mana_pool["C"] = 1
        p.credit_pool_snow("C", 5)
        assert p._pool_snow["C"] == 1

    def test_debit_decrements_the_tag(self, make_game, make_card):
        """Two successive pool payments from a half-snow pool must count
        snow ONCE — a debit that reports without decrementing counts the
        same snow mana on every later spend."""
        game = make_game()
        rick = game.players[0]
        rick.grant_pool_mana("C", 1, source=_snow_ring(make_card))
        rick.grant_pool_mana("C", 1, source=_plain_ring(make_card))
        assert rick.mana_pool["C"] == 2
        assert rick._pool_snow.get("C", 0) == 1

        assert rick.tap_sources_for_cost("{1}", game=game)
        first = rick._last_payment["snow_spent"]
        assert rick.tap_sources_for_cost("{1}", game=game)
        second = rick._last_payment["snow_spent"]
        assert first + second == 1

    def test_empty_mana_pool_clears_tags(self, make_game, make_card):
        """Tags describe pool contents — they die with the pool. A stale
        tag surviving the clear would mark UNRELATED later mana as snow."""
        game = make_game()
        rick = game.players[0]
        rick.grant_pool_mana("C", 1, source=_snow_ring(make_card))
        rick.empty_mana_pool()
        assert rick._pool_snow == {}

        # Refloat NON-snow mana into the emptied pool and spend it: any
        # surviving stale tag turns this 0 into a 1.
        rick.grant_pool_mana("C", 1, source=_plain_ring(make_card))
        assert rick.tap_sources_for_cost("{1}", game=game)
        assert rick._last_payment["snow_spent"] == 0

    def test_grant_pool_mana_source_gating(self, make_card):
        """Snow source tags; non-snow and source-less grants do not."""
        p = Player(name="P")
        p.grant_pool_mana("B", 2, source=_snow_swamp(make_card))
        assert p.mana_pool["B"] == 2
        assert p._pool_snow.get("B", 0) == 2
        p.grant_pool_mana("B", 1, source=_plain_ring(make_card))
        assert p.mana_pool["B"] == 3
        assert p._pool_snow.get("B", 0) == 2
        p.grant_pool_mana("B", 1)
        assert p.mana_pool["B"] == 4
        assert p._pool_snow.get("B", 0) == 2

    def test_is_snow_source_by_name_and_type_line(self, make_card):
        assert Player._is_snow_source(_snow_swamp(make_card))
        assert Player._is_snow_source(_snow_ring(make_card))
        assert not Player._is_snow_source(_plain_ring(make_card))


class TestTapLandsForManaProvenance:
    def test_snow_basic_float_is_tagged(self, make_game, make_card):
        """The color-unaware tap path (rituals/engine float flows) tags a
        snow source's contribution via the pool delta."""
        game = make_game()
        rick = game.players[0]
        rick.battlefield.append(_snow_swamp(make_card))

        assert rick.tap_lands_for_mana(1, game=game)
        assert rick.mana_pool["B"] == 1
        assert rick._pool_snow.get("B", 0) == 1

        assert rick.tap_sources_for_cost("{1}", game=game)
        assert rick._last_payment["snow_spent"] == 1

    def test_plain_basic_float_stays_untagged(self, make_game, make_card):
        game = make_game()
        rick = game.players[0]
        rick.battlefield.append(make_card(
            "Swamp", type_line="Basic Land — Swamp",
            oracle_text="{T}: Add {B}.", power=None, toughness=None))

        assert rick.tap_lands_for_mana(1, game=game)
        assert rick.mana_pool["B"] == 1
        assert rick._pool_snow.get("B", 0) == 0

        assert rick.tap_sources_for_cost("{1}", game=game)
        assert rick._last_payment["snow_spent"] == 0


class TestAddManaActionProvenance:
    def test_add_mana_with_snow_source_tags(self, make_game, make_card):
        """Tier-3 add_mana marks snow only when the emitting source
        resolves to a snow permanent on the player's battlefield."""
        game = make_game()
        rick = game.players[0]
        engine = _engine(game)
        rick.battlefield.append(make_card(
            "Coldsteel Heart", type_line="Snow Artifact",
            oracle_text="{T}: Add one mana of the chosen color.",
            power=None, toughness=None))

        engine.rules._execute_action_on_state(game, {
            "action": "add_mana", "player": "Rick", "color": "C",
            "amount": 2, "source": "Coldsteel Heart"})
        assert rick.mana_pool["C"] == 2
        assert rick._pool_snow.get("C", 0) == 2

        assert rick.tap_sources_for_cost("{2}", game=game)
        assert rick._last_payment["snow_spent"] == 2

    def test_add_mana_without_source_stays_untagged(self, make_game):
        """No resolvable source = non-snow (the undercount direction)."""
        game = make_game()
        rick = game.players[0]
        engine = _engine(game)

        engine.rules._execute_action_on_state(game, {
            "action": "add_mana", "player": "Rick", "color": "R",
            "amount": 1})
        assert rick.mana_pool["R"] == 1
        assert rick._pool_snow.get("R", 0) == 0

        assert rick.tap_sources_for_cost("{1}", game=game)
        assert rick._last_payment["snow_spent"] == 0


class TestActivateManaProvenance:
    def test_engine_activate_snow_rock_tags(self, make_game, make_card):
        """The [ACTIVATE-MANA] symbol-scan branch threads the activated
        permanent as the provenance source."""
        game = make_game()
        rick = game.players[0]
        engine = _engine(game)
        rick.battlefield.append(make_card(
            "Snowfield Signet", type_line="Snow Artifact",
            oracle_text="{T}: Add {B}.", power=None, toughness=None))

        msg = asyncio.run(engine._execute_action(
            game, 0, {"type": "activate", "permanent": "Snowfield Signet"}))
        assert msg
        assert rick.mana_pool["B"] == 1
        assert rick._pool_snow.get("B", 0) == 1

        assert rick.tap_sources_for_cost("{1}", game=game)
        assert rick._last_payment["snow_spent"] == 1

    def test_engine_activate_plain_rock_stays_untagged(
            self, make_game, make_card):
        game = make_game()
        rick = game.players[0]
        engine = _engine(game)
        rick.battlefield.append(make_card(
            "Charcoal Diamond", type_line="Artifact",
            oracle_text="{T}: Add {B}.", power=None, toughness=None))

        msg = asyncio.run(engine._execute_action(
            game, 0, {"type": "activate", "permanent": "Charcoal Diamond"}))
        assert msg
        assert rick.mana_pool["B"] == 1
        assert rick._pool_snow.get("B", 0) == 0

        assert rick.tap_sources_for_cost("{1}", game=game)
        assert rick._last_payment["snow_spent"] == 0


class TestCogManualActivateProvenance:
    def test_manual_activate_snow_rock_tags(self, make_game, make_card):
        """The manual !activate path (the OTHER activation code path —
        the documented two-paths divergence family) threads the activated
        permanent as the provenance source too."""
        from types import SimpleNamespace
        from mtg.cog import MTGGameCog

        async def _record(messages, content):
            messages.append(content)

        game = make_game()
        rick = game.players[0]
        engine = _engine(game)
        engine.save_game = lambda _game: None
        snow_rock = make_card(
            "Snowfield Signet", type_line="Snow Artifact",
            oracle_text="{T}: Add {B}.", power=None, toughness=None)
        plain_rock = make_card(
            "Charcoal Diamond", type_line="Artifact",
            oracle_text="{T}: Add {B}.", power=None, toughness=None)
        rick.battlefield.extend([snow_rock, plain_rock])
        cog = object.__new__(MTGGameCog)
        cog.engine = engine
        sent = []
        ctx = SimpleNamespace(send=lambda content: _record(sent, content))

        asyncio.run(MTGGameCog._activate_permanent(
            cog, ctx, game, rick, 0, snow_rock, "1", None))
        assert rick.mana_pool["B"] == 1
        assert rick._pool_snow.get("B", 0) == 1

        asyncio.run(MTGGameCog._activate_permanent(
            cog, ctx, game, rick, 0, plain_rock, "1", None))
        assert rick.mana_pool["B"] == 2
        assert rick._pool_snow.get("B", 0) == 1


class TestDraugrWaiverNonInterference:
    def test_waivered_snow_payment_still_counts_tap_snow(
            self, make_game, make_card):
        """Q3's snow-as-any waiver and Q4's provenance are independent:
        a waivered payment's consumed snow still counts, and its floated
        snow excess still tags (snow-ness is a property of the SOURCE,
        not of the color the waiver let it produce)."""
        game = make_game()
        rick = game.players[0]
        ring = _snow_ring(make_card)
        rick.battlefield.append(ring)
        spell = make_card("Exiled Spell", mana_cost="{1}",
                          type_line="Sorcery", power=None, toughness=None)
        spell._snow_as_any_color = True
        spell._castable_by_player = rick.name

        assert rick.tap_sources_for_cost(
            "{1}", game=game, spending_card=spell)
        assert rick._last_payment["snow_spent"] == 1
        assert rick._pool_snow.get("C", 0) == rick.mana_pool["C"] == 1
        # The waiver flag is per-entry (Q3 review #1) — a later plain
        # payment must not inherit it, and the tagged float still spends
        # as snow.
        assert rick.tap_sources_for_cost("{1}", game=game)
        assert rick._last_payment["snow_spent"] == 1
