"""Aug 23, 2026 cube-FFA audit (game_1538942949243621457, sha=40f7dad).

FOUNDATION finding: the mana payment engine charged pain-land / Talisman tap
damage on EVERY tap, including taps that only ever paid a generic portion the
card's own printed damage-free line covers.

Outcome-affecting in the audited game: Bot-Elspeth's Talisman of Curiosity
("{T}: Add {C}." / "{T}: Add {G} or {U}. This artifact deals 1 damage to
you.") was charged on all six of its taps — turns 12, 16, 20, 24, 28 and 42 —
and every one of those costs ({1}{G}{W}, {1}{W}{W}, {3}{W}, {3}{W}{W},
{2}{W}{W}, {2}) had a legal assignment where the Talisman paid generic. The 6
life were exactly the margin: Bot-Elspeth was eliminated on turn 44 taking 6
from Rampaging Baloths at 6 life, and would have been at 11 without them.

MINOR findings from the same game are fixed here too:

  - `[PW-TARGET] Forwarding explicit target 'None' → Rick Deckard` printed the
    NAME field when the target had actually been supplied as target_player_id.
    The resolution was correct; the log lied about which identifier was used,
    which is exactly what makes a later audit misfile a working path.
  - `💥 3 damage to Emmara, Soul of the Accord` (Inferno Titan's ETB) carried
    no source attribution, unlike every other damage line in the transcript.

FIXTURE DISCIPLINE (the standing pin-shape rule): every payment scenario runs
a REAL cost string through `tap_sources_for_cost`, not through the helper
directly — the Aug 10 lesson is that a helper pinned only by direct calls is
not pinned into production. Oracle text is copied verbatim from
data/card_data_cache.json / Scryfall, never from memory.
"""
import pytest


# --------------------------------------------------------------------------
# Cards, with their real printed text.
# --------------------------------------------------------------------------

def _talisman(make_card):
    """Talisman of Curiosity — free colorless line + damage-bearing color line."""
    return make_card(
        "Talisman of Curiosity",
        type_line="Artifact",
        oracle_text="{T}: Add {C}.\n{T}: Add {G} or {U}. "
                    "This artifact deals 1 damage to you.",
        power=None, toughness=None,
    )


def _pain_land(make_card):
    """Adarkar Wastes — the land half of the same family."""
    return make_card(
        "Adarkar Wastes",
        type_line="Land",
        oracle_text="{T}: Add {C}.\n{T}: Add {W} or {U}. "
                    "Adarkar Wastes deals 1 damage to you.",
        power=None, toughness=None,
    )


def _ancient_tomb(make_card):
    """Ancient Tomb — ONE line, damage-bearing. The adverse control: it has no
    free line to fall back on, so a generic tap must still cost 2."""
    return make_card(
        "Ancient Tomb",
        type_line="Land",
        oracle_text="{T}: Add {C}{C}. Ancient Tomb deals 2 damage to you.",
        power=None, toughness=None,
    )


def _city_of_brass(make_card):
    """City of Brass — the damage is a becomes-tapped TRIGGER, so it fires
    whichever ability was activated. The free-line escape must not reach it."""
    return make_card(
        "City of Brass",
        type_line="Land",
        oracle_text="Whenever City of Brass becomes tapped, it deals 1 damage "
                    "to you.\n{T}: Add one mana of any color.",
        power=None, toughness=None,
    )


def _basic(make_card, name, sym):
    return make_card(name, type_line="Basic Land",
                     oracle_text="{T}: Add {%s}." % sym,
                     power=None, toughness=None)


# --------------------------------------------------------------------------
# The fix: generic taps use the printed free line.
# --------------------------------------------------------------------------

class TestFreeLineTapsTakeNoDamage:

    def test_talisman_paying_pure_generic_deals_no_damage(
            self, make_game, make_card):
        """The turn-42 shape: Mask of Memory for {2}, paid by a Talisman plus
        a basic. The Talisman's "{T}: Add {C}." covers its half."""
        game = make_game()
        p = game.players[0]
        p.battlefield.append(_talisman(make_card))
        p.battlefield.append(_basic(make_card, "Plains", "W"))
        before = p.life

        assert p.tap_sources_for_cost("{2}", game=game)
        assert p.life == before, (
            "a generic-only tap must use the damage-free line "
            "(life %d -> %d)" % (before, p.life))

    def test_talisman_paying_its_colored_pip_still_deals_damage(
            self, make_game, make_card):
        """ADVERSE CONTROL. {G} can only come from the damage-bearing line, so
        the 1 damage stands. This is the half the fix must not erase."""
        game = make_game()
        p = game.players[0]
        p.battlefield.append(_talisman(make_card))
        before = p.life

        assert p.tap_sources_for_cost("{G}", game=game)
        assert p.life == before - 1, (
            "a colored pip must still cost 1 (life %d -> %d)"
            % (before, p.life))

    def test_pain_land_paying_generic_deals_no_damage(
            self, make_game, make_card):
        """The land half of the family. Its committed_color would be a REAL
        colour here ('W' or 'U'), which is why the fix keys off the payment
        PHASE rather than the committed colour."""
        game = make_game()
        p = game.players[0]
        p.battlefield.append(_pain_land(make_card))
        p.battlefield.append(_basic(make_card, "Forest", "G"))
        before = p.life

        assert p.tap_sources_for_cost("{2}", game=game)
        assert p.life == before

    def test_pain_land_paying_its_colored_pip_still_deals_damage(
            self, make_game, make_card):
        """ADVERSE CONTROL for the land half."""
        game = make_game()
        p = game.players[0]
        p.battlefield.append(_pain_land(make_card))
        before = p.life

        assert p.tap_sources_for_cost("{W}", game=game)
        assert p.life == before - 1


class TestCardsWithNoFreeLineStillPay:

    def test_ancient_tomb_generic_tap_still_deals_two(
            self, make_game, make_card):
        """ADVERSE CONTROL: one printed line, damage-bearing. A generic tap has
        no cheaper line to take, so the 2 damage stands."""
        game = make_game()
        p = game.players[0]
        p.battlefield.append(_ancient_tomb(make_card))
        before = p.life

        assert p.tap_sources_for_cost("{2}", game=game)
        assert p.life == before - 2, (
            "Ancient Tomb has no damage-free line (life %d -> %d)"
            % (before, p.life))

    def test_city_of_brass_generic_tap_still_deals_one(
            self, make_game, make_card):
        """ADVERSE CONTROL: becomes-tapped TRIGGER, not an ability rider — it
        fires whichever ability was activated (CR 603.2)."""
        game = make_game()
        p = game.players[0]
        p.battlefield.append(_city_of_brass(make_card))
        p.battlefield.append(_basic(make_card, "Island", "U"))
        before = p.life

        assert p.tap_sources_for_cost("{2}", game=game)
        assert p.life == before - 1, (
            "a becomes-tapped trigger fires on any tap (life %d -> %d)"
            % (before, p.life))


class TestUnchangedPaths:

    def test_helper_defaults_to_charging(self, make_game, make_card):
        """`paid_generic` defaults False, so every existing caller — notably
        the colour-unaware tap_lands_for_mana — keeps its behaviour."""
        game = make_game()
        p = game.players[0]
        talisman = _talisman(make_card)
        assert p._get_mana_tap_damage(talisman) == 1
        assert p._get_mana_tap_damage(talisman, paid_generic=False) == 1
        assert p._get_mana_tap_damage(talisman, paid_generic=True) == 0

    def test_tap_lands_for_mana_still_charges(self, make_game, make_card):
        """The amount-based path cannot establish that only generic was owed,
        so it is deliberately left alone. Pinned so a later change is a
        DECISION rather than an accident."""
        game = make_game()
        p = game.players[0]
        p.battlefield.append(_talisman(make_card))
        p.battlefield.append(_basic(make_card, "Plains", "W"))
        before = p.life

        assert p.tap_lands_for_mana(2, game=game)
        assert p.life == before - 1

    def test_plain_source_never_reports_tap_damage(self, make_game, make_card):
        """Sanity: a card with no damage clause is untouched by any of this."""
        game = make_game()
        p = game.players[0]
        assert p._get_mana_tap_damage(
            _basic(make_card, "Plains", "W"), paid_generic=True) == 0
        assert p._get_mana_tap_damage(
            _basic(make_card, "Plains", "W")) == 0


class TestOutcomeShape:

    def test_six_generic_taps_cost_no_life(self, make_game, make_card):
        """The audited game's shape end to end: six separate payments, each
        with a generic portion the Talisman can cover. Before the fix this
        cost 6 life, which was the exact margin Bot-Elspeth died by."""
        game = make_game()
        p = game.players[0]
        p.battlefield.append(_talisman(make_card))
        for i in range(4):
            p.battlefield.append(_basic(make_card, "Plains", "W"))
        before = p.life

        for _ in range(6):
            for c in p.battlefield:
                c.tapped = False
            p.empty_mana_pool()
            assert p.tap_sources_for_cost("{1}{W}{W}", game=game)

        assert p.life == before, (
            "six generic Talisman taps must be free (life %d -> %d)"
            % (before, p.life))


class TestDamageAttribution:
    """MINOR: noncombat damage to a creature or planeswalker carried no source.

    The player branch already prefixes the source name; the creature and
    planeswalker branches did not, so Inferno Titan's ETB printed a bare
    "3 damage to Emmara, Soul of the Accord" in the audited transcript.
    """

    def _damage(self, rules, game, card, owner, **extra):
        action = {"action": "deal_damage", "amount": 3,
                  "target_card": card.name,
                  "target_controller": owner.name}
        action.update(extra)
        from mtg.actions import execute_action_on_state
        return execute_action_on_state(rules, game, action) or ""

    def test_creature_damage_names_its_source(
            self, rules, make_game, make_card):
        game = make_game()
        victim = make_card("Emmara, Soul of the Accord",
                           type_line="Legendary Creature", power=1, toughness=2)
        game.players[1].battlefield.append(victim)

        msg = self._damage(rules, game, victim, game.players[1],
                           _source_card_name="Inferno Titan")
        assert "Inferno Titan" in msg, msg

    def test_creature_damage_without_a_source_still_reports(
            self, rules, make_game, make_card):
        """ADVERSE CONTROL: no source known -> the old bare line, not a crash
        and not an empty attribution like '** **'."""
        game = make_game()
        victim = make_card("Emmara, Soul of the Accord",
                           type_line="Legendary Creature", power=1, toughness=2)
        game.players[1].battlefield.append(victim)

        msg = self._damage(rules, game, victim, game.players[1])
        assert "Emmara" in msg and "3" in msg, msg
        assert "** **" not in msg, msg
