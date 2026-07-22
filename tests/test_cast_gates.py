"""Characterization tests for cast_spell_async's gates (refactor #2, step 1).

These pin the CURRENT behavior of the ~2,100-line cast function before it is
decomposed into _validate_cast / _compute_alt_costs / _pay_costs / resolution
dispatch. Each test targets one gate and asserts what the function DOES
today — not what the CR ideally demands — so the split can be verified
behavior-preserving. Where current behavior is a known simplification, the
test says so in a comment rather than encoding the aspirational rule.

Convention: Rick (player 0) casts on his own turn, MAIN1, empty stack.

Characterization discoveries (current behavior, pinned on purpose):
- The zone-membership gate (hand ∪ marked-graveyard) runs BEFORE the
  mana/timing rules gate. (July 10 fix — originally the mana check ran
  first, so a card not even in hand was rejected as "Not enough black
  mana", sending the autoplay AI into pointless mana-fixing retries.)
- The mana pre-gate is NOT convoke/delve-aware: it demands the full printed
  cost in available sources even though the payment stage would cover part
  of it with creature taps / graveyard exiles. (Latent bug candidate — a
  convoke spell you could legally cast via convoke alone is rejected up
  front. Fix in _validate_cast after the decomposition.)
"""
import asyncio

import pytest

from mtg.constants import Phase
from mtg.spells import cast_spell_async


def _engine():
    from mtg.engine import GameEngine
    return GameEngine(None)


def _ready(game, active_idx=0):
    game.phase = Phase.MAIN1
    game.active_player_index = active_idx
    return game


def _cast(engine, game, player, card, **kw):
    return asyncio.run(cast_spell_async(engine, game, player, card, **kw))


def _swamps(make_card, n):
    return [make_card(f"Swamp {i}", type_line="Basic Land — Swamp",
                      power="0", toughness="0") for i in range(n)]


def _islands(make_card, n):
    return [make_card(f"Island {i}", type_line="Basic Land — Island",
                      power="0", toughness="0") for i in range(n)]


# ---------------------------------------------------------------------------
# Zone gates
# ---------------------------------------------------------------------------

class TestZoneGates:
    def test_card_not_in_hand_is_rejected(self, make_game, make_card):
        game = _ready(make_game())
        rick = game.players[0]
        rick.battlefield.extend(_swamps(make_card, 2))
        bear = make_card("Bear", mana_cost="{1}{B}", cmc=2)
        ok, msg, _ = _cast(_engine(), game, rick, bear)
        assert ok is False
        assert "not in hand" in msg.lower()

    def test_hand_gate_precedes_mana_check(self, make_game, make_card):
        # July 10 fix: with NO mana up either, a card that isn't in hand
        # must still be rejected for zone membership, not for mana — the
        # old ordering reported "Not enough black mana" here, which fed the
        # autoplay AI useless retry guidance for a card it didn't hold.
        game = _ready(make_game())
        rick = game.players[0]
        bear = make_card("Bear", mana_cost="{1}{B}", cmc=2)
        ok, msg, _ = _cast(_engine(), game, rick, bear)
        assert ok is False
        assert "not in hand" in msg.lower()

    def test_flashback_cast_leaves_graveyard_and_exiles(self, make_game, make_card):
        # June 11 fix: marked graveyard casts pass the hand gate, leave the
        # graveyard, and exile on resolution (CR 702.34a).
        game = _ready(make_game())
        rick = game.players[0]
        rick.battlefield.extend(_swamps(make_card, 3))
        rick.battlefield.extend(_islands(make_card, 1))
        spell = make_card("Deep Analysis", type_line="Sorcery",
                          oracle_text="Target player draws two cards.\n"
                                      "Flashback—{1}{U}, Pay 3 life.",
                          mana_cost="{3}{U}", cmc=4, power="0", toughness="0")
        rick.graveyard.append(spell)
        rick.playable_from_graveyard.append(spell.id)
        spell._flashback_cost = "{1}{U}"
        ok, msg, _ = _cast(_engine(), game, rick, spell,
                           target=game.players[1])
        assert ok is True, msg
        assert spell not in rick.graveyard
        assert spell in rick.exile


# ---------------------------------------------------------------------------
# CR 601.2c target gates
# ---------------------------------------------------------------------------

class TestTargetGates:
    def test_aura_with_no_battlefield_creature_is_rejected(self, make_game, make_card):
        game = _ready(make_game())
        rick = game.players[0]
        rick.battlefield.extend(_swamps(make_card, 2))
        aura = make_card("Unholy Strength", type_line="Enchantment — Aura",
                         oracle_text="Enchant creature\nEnchanted creature "
                                     "gets +2/+1.",
                         mana_cost="{B}", cmc=1, power="0", toughness="0")
        rick.hand.append(aura)
        ok, msg, _ = _cast(_engine(), game, rick, aura)
        assert ok is False
        assert "601.2c" in msg

    def test_graveyard_aura_checks_graveyards_not_battlefield(self, make_game, make_card):
        # June 11 fix: Animate Dead scans graveyards; a battlefield full of
        # creatures with empty graveyards must still reject.
        game = _ready(make_game())
        rick = game.players[0]
        rick.battlefield.extend(_swamps(make_card, 2))
        rick.battlefield.append(make_card("Bystander Bear"))
        animate = make_card("Animate Dead", type_line="Enchantment — Aura",
                            oracle_text="Enchant creature card in a graveyard\n"
                                        "When Animate Dead enters the "
                                        "battlefield, return enchanted "
                                        "creature card to the battlefield.",
                            mana_cost="{1}{B}", cmc=2, power="0", toughness="0")
        rick.hand.append(animate)
        ok, msg, _ = _cast(_engine(), game, rick, animate)
        assert ok is False
        assert "graveyard" in msg.lower()

    def test_counterspell_on_empty_stack_is_rejected(self, make_game, make_card):
        game = _ready(make_game())
        rick = game.players[0]
        rick.battlefield.extend(_islands(make_card, 3))
        cs = make_card("Cancel", type_line="Instant",
                       oracle_text="Counter target spell.",
                       mana_cost="{1}{U}{U}", cmc=3, power="0", toughness="0")
        rick.hand.append(cs)
        ok, msg, _ = _cast(_engine(), game, rick, cs)
        assert ok is False
        assert "target spell on the stack" in msg

    def test_modal_counter_is_allowed_on_empty_stack(self, make_game, make_card):
        # "choose one" counters have non-counter modes legal on empty stack.
        game = _ready(make_game())
        rick = game.players[0]
        rick.battlefield.extend(_islands(make_card, 5))
        cmd = make_card("Test Command", type_line="Instant",
                        oracle_text="Choose one —\n• Counter target spell.\n"
                                    "• Draw a card.",
                        mana_cost="{2}{U}", cmc=3, power="0", toughness="0")
        rick.hand.append(cmd)
        ok, msg, _ = _cast(_engine(), game, rick, cmd)
        assert ok is True, msg


# ---------------------------------------------------------------------------
# CR 903.4 color identity gate (commander formats)
# ---------------------------------------------------------------------------

class TestColorIdentityGate:
    def test_offcolor_spell_blocked_and_blocklisted(self, make_game, make_card):
        game = _ready(make_game())
        rick = game.players[0]
        commander = make_card("Baral, Chief of Compliance",
                              type_line="Legendary Creature — Human Wizard",
                              mana_cost="{1}{U}", cmc=2)
        commander.is_commander = True
        commander.color_identity = ["U"]
        rick.command_zone.append(commander)
        rick.battlefield.extend(_swamps(make_card, 3))
        offcolor = make_card("Doom Blade", type_line="Instant",
                             oracle_text="Destroy target creature.",
                             mana_cost="{1}{B}", cmc=2,
                             power="0", toughness="0")
        offcolor.color_identity = ["B"]
        rick.hand.append(offcolor)
        game.players[1].battlefield.append(make_card("Target Bear"))
        ok, msg, _ = _cast(_engine(), game, rick, offcolor)
        assert ok is False
        assert "outside commander identity" in msg
        assert "Doom Blade" in getattr(rick, '_color_id_blocklist', set())


# ---------------------------------------------------------------------------
# Mana payment, tax, and alternate costs
# ---------------------------------------------------------------------------

class TestManaPayment:
    def test_basic_cast_taps_lands_and_enters_battlefield(self, make_game, make_card):
        game = _ready(make_game())
        rick = game.players[0]
        lands = _swamps(make_card, 2)
        rick.battlefield.extend(lands)
        bear = make_card("Gray Bear", mana_cost="{1}{B}", cmc=2)
        rick.hand.append(bear)
        ok, msg, _ = _cast(_engine(), game, rick, bear)
        assert ok is True, msg
        assert bear in rick.battlefield
        assert bear not in rick.hand
        assert bear.summoning_sick is True
        assert all(l.tapped for l in lands)

    def test_insufficient_mana_rejects_without_side_effects(self, make_game, make_card):
        game = _ready(make_game())
        rick = game.players[0]
        lands = _swamps(make_card, 2)
        rick.battlefield.extend(lands)
        big = make_card("Big Bear", mana_cost="{3}{B}", cmc=4)
        rick.hand.append(big)
        ok, msg, _ = _cast(_engine(), game, rick, big)
        assert ok is False
        assert "not enough mana" in msg.lower()
        assert big in rick.hand
        assert not any(l.tapped for l in lands)

    def test_commander_tax_is_added_to_generic(self, make_game, make_card):
        game = _ready(make_game())
        rick = game.players[0]
        rick.battlefield.extend(_swamps(make_card, 3))
        cmdr = make_card("Taxed Bear", mana_cost="{1}{B}", cmc=2)
        rick.hand.append(cmdr)
        ok, msg, _ = _cast(_engine(), game, rick, cmdr, additional_cost=2)
        assert ok is False
        assert "commander tax" in msg
        # With exactly enough sources it goes through.
        rick.battlefield.extend(_swamps(make_card, 1))
        ok2, msg2, _ = _cast(_engine(), game, rick, cmdr, additional_cost=2)
        assert ok2 is True, msg2

    def test_pact_casts_for_free(self, make_game, make_card):
        # cmc==0 'pact' names skip payment (cost due next upkeep).
        game = _ready(make_game())
        rick = game.players[0]
        pact = make_card("Pact of the Titan", type_line="Instant",
                         oracle_text="Create a 4/4 red Giant creature token. "
                                     "At the beginning of your next upkeep, "
                                     "pay {4}{R}. If you don't, you lose the "
                                     "game.",
                         mana_cost="", cmc=0, power="0", toughness="0")
        rick.hand.append(pact)
        ok, msg, _ = _cast(_engine(), game, rick, pact)
        assert ok is True, msg

    def test_convoke_taps_creatures_to_help_pay(self, make_game, make_card):
        # July 20: the pre-gate is now convoke-aware — ONE land is enough
        # when three creatures cover the generic portion. (The original pin
        # documented the latent bug: full printed cost had to be AVAILABLE
        # even though convoke then paid most of it.)
        game = _ready(make_game())
        rick = game.players[0]
        helpers = [make_card(f"Helper {i}") for i in range(3)]
        rick.battlefield.extend(helpers)
        lands = _swamps(make_card, 1)
        rick.battlefield.extend(lands)
        spell = make_card("Convoked Giant",
                          type_line="Creature — Giant",
                          oracle_text="Convoke (Your creatures can help cast "
                                      "this spell.)",
                          mana_cost="{3}{B}", cmc=4, power="5", toughness="5")
        rick.hand.append(spell)
        ok, msg, _ = _cast(_engine(), game, rick, spell)
        assert ok is True, msg
        assert all(h.tapped for h in helpers)
        assert sum(l.tapped for l in lands) == 1

    def test_delve_exiles_graveyard_cards(self, make_game, make_card):
        # July 20: pre-gate is delve-aware — one land suffices when the
        # graveyard covers the generic; payment exiles the 3 fodder cards
        # and taps 1 land. (Original pin documented the latent bug.)
        game = _ready(make_game())
        rick = game.players[0]
        fodder = [make_card(f"Fodder {i}", type_line="Sorcery",
                            power="0", toughness="0") for i in range(3)]
        rick.graveyard.extend(fodder)
        lands = _swamps(make_card, 1)
        rick.battlefield.extend(lands)
        spell = make_card("Delved Horror",
                          type_line="Creature — Horror",
                          oracle_text="Delve (Each card you exile from your "
                                      "graveyard while casting this spell "
                                      "pays for {1}.)",
                          mana_cost="{3}{B}", cmc=4, power="4", toughness="4")
        rick.hand.append(spell)
        ok, msg, _ = _cast(_engine(), game, rick, spell)
        assert ok is True, msg
        assert all(f in rick.exile for f in fodder)
        assert all(f not in rick.graveyard for f in fodder)
        assert sum(l.tapped for l in lands) == 1


class TestXCosts:
    def _x_spell(self, make_card):
        return make_card("Test Geyser", type_line="Sorcery",
                         oracle_text="Target player draws X cards.",
                         mana_cost="{X}{B}", cmc=1, power="0", toughness="0")

    def test_explicit_x_value_sets_total_cost(self, make_game, make_card):
        game = _ready(make_game())
        rick = game.players[0]
        lands = _swamps(make_card, 4)
        rick.battlefield.extend(lands)
        spell = self._x_spell(make_card)
        spell._x_value = 2
        rick.hand.append(spell)
        ok, msg, _ = _cast(_engine(), game, rick, spell,
                           target=game.players[1])
        assert ok is True, msg
        assert spell._x_value == 2
        assert sum(l.tapped for l in lands) == 3  # X=2 + {B}

    def test_auto_x_uses_all_available_mana(self, make_game, make_card):
        # Pinned: with no explicit X, the engine maxes X against available
        # mana (X = available - fixed), tapping everything.
        game = _ready(make_game())
        rick = game.players[0]
        lands = _swamps(make_card, 4)
        rick.battlefield.extend(lands)
        spell = self._x_spell(make_card)
        rick.hand.append(spell)
        ok, msg, _ = _cast(_engine(), game, rick, spell,
                           target=game.players[1])
        assert ok is True, msg
        assert spell._x_value == 3
        assert sum(l.tapped for l in lands) == 4


# ---------------------------------------------------------------------------
# Resolution zone-routing and bookkeeping
# ---------------------------------------------------------------------------

class TestResolutionRouting:
    def test_instant_resolves_to_graveyard(self, make_game, make_card):
        game = _ready(make_game())
        rick = game.players[0]
        rick.battlefield.extend(_swamps(make_card, 1))
        spell = make_card("Test Reflex", type_line="Instant",
                          oracle_text="You gain 3 life.",
                          mana_cost="{B}", cmc=1, power="0", toughness="0")
        rick.hand.append(spell)
        ok, msg, _ = _cast(_engine(), game, rick, spell)
        assert ok is True, msg
        assert spell in rick.graveyard
        assert spell not in rick.battlefield

    def test_artifact_enters_without_summoning_sickness(self, make_game, make_card):
        game = _ready(make_game())
        rick = game.players[0]
        rick.battlefield.extend(_swamps(make_card, 2))
        rock = make_card("Test Rock", type_line="Artifact",
                         oracle_text="{T}: Add {C}.",
                         mana_cost="{2}", cmc=2, power="0", toughness="0")
        rick.hand.append(rock)
        ok, msg, _ = _cast(_engine(), game, rick, rock)
        assert ok is True, msg
        assert rock in rick.battlefield
        assert rock.summoning_sick is False

    def test_spell_counters_increment(self, make_game, make_card):
        game = _ready(make_game())
        rick = game.players[0]
        rick.battlefield.extend(_swamps(make_card, 3))
        before_total = rick.spells_cast_this_turn
        before_noncreat = rick.noncreature_spells_cast_this_turn
        spell = make_card("Counting Trick", type_line="Instant",
                          oracle_text="You gain 1 life.",
                          mana_cost="{B}", cmc=1, power="0", toughness="0")
        rick.hand.append(spell)
        assert _cast(_engine(), game, rick, spell)[0] is True
        assert rick.spells_cast_this_turn == before_total + 1
        assert rick.noncreature_spells_cast_this_turn == before_noncreat + 1
        bear = make_card("Counting Bear", mana_cost="{1}{B}", cmc=2)
        rick.hand.append(bear)
        assert _cast(_engine(), game, rick, bear)[0] is True
        assert rick.spells_cast_this_turn == before_total + 2
        assert rick.noncreature_spells_cast_this_turn == before_noncreat + 1
