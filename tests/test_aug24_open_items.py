"""The four genuinely-open items from the Aug 24 verification pass."""
import pytest

from mtg.constants import Phase, Zone
from mtg.models import Card, GameState, Player


def _game():
    game = GameState(thread_id=1, format="modern",
                     players=[Player(name="Alice", user_id=1, life=20),
                              Player(name="Bob", user_id=2, life=20)])
    game.turn_number = 3
    game.active_player_index = 0
    return game


# ---------------------------------------------------------------------------
# 1. Protection makes an Aura fall off (CR 702.16 + 704.5m)
# ---------------------------------------------------------------------------

class TestProtectionDetachesAuras:
    """The last letter of DEBA. Damage, Blocking and Targeting were all closed
    on Aug 10; Enchant was not, so a creature could gain protection from white
    and keep wearing a white Aura."""

    @staticmethod
    def _bear_with_aura(game, aura_cost="{W}", oracle=""):
        bear = Card(name="Bear", id="bear", type_line="Creature — Bear",
                    power="2", toughness="2", oracle_text=oracle)
        aura = Card(name="Pacifism", id="aura",
                    type_line="Enchantment — Aura", mana_cost=aura_cost,
                    oracle_text="Enchant creature\nEnchanted creature can't "
                                "attack or block.")
        aura.attached_to = bear.id
        bear.attachments = [aura.id]   # IDs, not Cards (models.py:667)
        game.players[0].battlefield += [bear, aura]
        return bear, aura

    def test_an_aura_falls_off_a_creature_with_protection_from_its_colour(self):
        game = _game()
        bear, aura = self._bear_with_aura(
            game, oracle="Protection from white")

        actions = game._rules_engine.check_state_based_actions(game) \
            if getattr(game, "_rules_engine", None) else None
        # Drive the inline sweep directly -- it is the live path.
        from mtg.sba import check_state_based_actions
        from mtg.rules_engine import RulesEngine
        rules = RulesEngine(None)
        acts = check_state_based_actions(rules, game)

        assert any(a.get("type") == "aura_invalid"
                   and a.get("card_id") == "aura" for a in acts), acts

    def test_an_aura_of_a_safe_colour_stays_attached(self):
        """Adverse control: protection from white must not detach a BLACK
        Aura, or every protection creature becomes unenchantable."""
        game = _game()
        bear, aura = self._bear_with_aura(
            game, aura_cost="{B}", oracle="Protection from white")

        from mtg.sba import check_state_based_actions
        from mtg.rules_engine import RulesEngine
        acts = check_state_based_actions(RulesEngine(None), game)

        assert not any(a.get("type") == "aura_invalid"
                       and a.get("card_id") == "aura" for a in acts), acts

    def test_a_creature_with_no_protection_keeps_its_aura(self):
        game = _game()
        self._bear_with_aura(game, oracle="")

        from mtg.sba import check_state_based_actions
        from mtg.rules_engine import RulesEngine
        acts = check_state_based_actions(RulesEngine(None), game)

        assert not any(a.get("type") == "aura_invalid" for a in acts), acts


# ---------------------------------------------------------------------------
# 3. _validate_plan_mana was blind to cost adjustments
# ---------------------------------------------------------------------------

class TestPlanCostAdjustments:
    """The validator priced every planned cast at its PRINTED cost while
    _compute_alt_costs applied four adjustment helpers, so the two disagreed
    on any board with a Medallion, a Thalia, an affinity card or a
    self-discounter. The directions fail differently and both were live:
    over-pricing rejects a legal cast, under-pricing fails mid-plan."""

    @staticmethod
    def _cost(game, player, card, cost_str, printed):
        from mtg.ai_turn import _adjusted_plan_cost
        return _adjusted_plan_cost(game, player, card, cost_str, printed,
                                   getattr(card, "name", "<unresolved>"))

    def test_a_medallion_lowers_the_planned_cost(self):
        game = _game()
        player = game.players[0]
        # Jet Medallion: "Black spells you cast cost {1} less to cast."
        player.battlefield.append(Card(
            name="Jet Medallion", id="med", type_line="Artifact",
            oracle_text="Black spells you cast cost {1} less to cast."))
        spell = Card(name="Doom Blade", id="s", type_line="Instant",
                     mana_cost="{1}{B}", cmc=2)

        assert self._cost(game, player, spell, "{1}{B}", 2) == 1

    def test_a_reduction_can_never_eat_a_coloured_pip(self):
        """CR 601.2f. Without the generic-only cap a Medallion would appear
        to make {B}{B} free, and the validator would promise a plan the
        payment engine refuses."""
        game = _game()
        player = game.players[0]
        for i in range(4):
            player.battlefield.append(Card(
                name="Jet Medallion", id="med%d" % i, type_line="Artifact",
                oracle_text="Black spells you cast cost {1} less to cast."))
        spell = Card(name="Doom Blade", id="s", type_line="Instant",
                     mana_cost="{B}{B}", cmc=2)

        assert self._cost(game, player, spell, "{B}{B}", 2) == 2

    def test_a_tax_raises_the_planned_cost(self):
        """The other direction: under-pricing makes a LATER entry in the same
        plan fail mid-execution."""
        game = _game()
        game.players[1].battlefield.append(Card(
            name="Thalia, Guardian of Thraben", id="thalia",
            type_line="Legendary Creature — Human Soldier",
            oracle_text="First strike\nNoncreature spells cost {1} more to "
                        "cast."))
        spell = Card(name="Doom Blade", id="s", type_line="Instant",
                     mana_cost="{1}{B}", cmc=2)

        assert self._cost(game, game.players[0], spell, "{1}{B}", 2) == 3

    def test_an_unadjusted_board_leaves_the_printed_cost_alone(self):
        game = _game()
        spell = Card(name="Doom Blade", id="s", type_line="Instant",
                     mana_cost="{1}{B}", cmc=2)

        assert self._cost(game, game.players[0], spell, "{1}{B}", 2) == 2

    def test_a_missing_card_object_is_priced_as_printed(self):
        """The validator must never crash on a name it could not resolve."""
        game = _game()
        assert self._cost(game, game.players[0], None, "{1}{B}", 2) == 2


# ---------------------------------------------------------------------------
# 4. burst_dedup_key discarded the payload it deduped on
# ---------------------------------------------------------------------------

class TestBurstPayloadSurvives:
    """Rule 1 of the dedup key strips a trailing numeric parenthetical so a
    running total does not defeat bucketing. That is right for "(life: 27)"
    and wrong for "(total: 5)", where the number IS the information -- a
    counter total climbed and then vanished.

    The dedup is deliberately NOT narrowed: doing that to this exact function
    caused the V19 regression, and volume control is the whole feature. The
    magnitude is restored in the suppression sentinel instead."""

    def test_the_payload_is_recoverable_from_a_counter_line(self):
        from mtg.helpers import burst_dedup_payload
        line = "⭕ 1 +1/+1 counter(s) on **Bear** (total: 5)"
        assert burst_dedup_payload(line) == "total: 5"

    def test_the_payload_is_recoverable_from_a_life_line(self):
        from mtg.helpers import burst_dedup_payload
        assert burst_dedup_payload(
            "💀 Blood Artist: 1 damage (life: 27)") == "life: 27"

    def test_a_line_with_no_numeric_parenthetical_has_no_payload(self):
        from mtg.helpers import burst_dedup_payload
        assert burst_dedup_payload("Rick draws **Forest**") == ""
        assert burst_dedup_payload("") == ""

    def test_the_dedup_key_is_unchanged(self):
        """The V19 guard: this function's behaviour must not move. Two lines
        differing only in their running total still share a bucket."""
        from mtg.helpers import burst_dedup_key
        a = "⭕ 1 +1/+1 counter(s) on **Bear** (total: 4)"
        b = "⭕ 1 +1/+1 counter(s) on **Bear** (total: 5)"
        assert burst_dedup_key(a) == burst_dedup_key(b)

    def test_distinct_casts_still_keep_distinct_keys(self):
        """The V19 regression itself: an unrestricted bold-strip made every
        cast in a turn share one key, so distinct casts were suppressed."""
        from mtg.helpers import burst_dedup_key
        assert burst_dedup_key("✨ Rick casts **Llanowar Elves**") != \
            burst_dedup_key("✨ Rick casts **Grizzly Bears**")

    def test_the_sentinel_quotes_the_payload(self):
        """Driven directly, not asserted against source text.

        The first version of this pin checked that mtg/cog.py CONTAINED
        "burst_dedup_payload" -- and survived a mutant that blanked the call
        while leaving the import, so the string still matched. The sentinel
        text is a helper now precisely so this can be decisive."""
        from mtg.helpers import burst_suppression_sentinel
        line = burst_suppression_sentinel(
            "⭕ 1 +1/+1 counter(s) on **Bear** (total: 5)")
        assert "total: 5" in line
        assert "suppressing further identical fires" in line

    def test_the_sentinel_is_unadorned_when_there_is_no_payload(self):
        from mtg.helpers import burst_suppression_sentinel
        line = burst_suppression_sentinel("Rick draws **Forest**")
        assert "latest" not in line
        assert "suppressing further identical fires" in line

    def test_the_send_site_uses_the_helper(self):
        """Structural, and narrow: the helper is only worth anything if the
        real send site calls it."""
        import inspect

        import mtg.cog as cog_mod
        assert "burst_suppression_sentinel(content)" in inspect.getsource(cog_mod)


# ---------------------------------------------------------------------------
# 2. Recipient-scoped prevention and fixed-amount shields
# ---------------------------------------------------------------------------

IROAS = ("Indestructible\n"
         "As long as your devotion to red and white is less than seven, "
         "Iroas isn't a creature.\n"
         "Creatures you control have menace.\n"
         "Prevent all damage that would be dealt to attacking creatures "
         "you control.")


class TestRecipientScopedPrevention:
    """A different primitive from the player-level flag: scoped per permanent
    rather than per player, and PARTIAL rather than all-or-nothing."""

    @staticmethod
    def _attacker(game, seat=0, attacking=True):
        c = Card(name="Bear", id="bear", type_line="Creature — Bear",
                 power="2", toughness="2")
        c.attacking = attacking
        game.players[seat].battlefield.append(c)
        return c

    def _iroas(self, game, seat=0):
        god = Card(name="Iroas, God of Victory", id="iroas",
                   type_line="Legendary Enchantment Creature — God",
                   oracle_text=IROAS)
        game.players[seat].battlefield.append(god)
        return god

    def test_iroas_prevents_damage_to_an_attacking_creature(self):
        from mtg.helpers import creature_damage_after_prevention
        game = _game()
        bear = self._attacker(game)
        self._iroas(game)

        after, why = creature_damage_after_prevention(
            game, bear, None, 5, is_combat=True)

        assert after == 0
        assert "Iroas" in why

    def test_iroas_does_not_protect_a_creature_that_is_not_attacking(self):
        """The printed scope is ATTACKING creatures; a blocker takes damage
        normally, and reading it as a blanket shield would be a strictly
        bigger lie than the missing implementation was."""
        from mtg.helpers import creature_damage_after_prevention
        game = _game()
        bear = self._attacker(game, attacking=False)
        self._iroas(game)

        after, _ = creature_damage_after_prevention(
            game, bear, None, 5, is_combat=True)

        assert after == 5

    def test_iroas_does_not_protect_the_opponents_attackers(self):
        from mtg.helpers import creature_damage_after_prevention
        game = _game()
        theirs = self._attacker(game, seat=1)
        self._iroas(game, seat=0)

        after, _ = creature_damage_after_prevention(
            game, theirs, None, 5, is_combat=True)

        assert after == 5

    def test_glacial_chasm_is_not_read_as_a_creature_shield(self):
        """Adverse control for the exact-match scope: "dealt to you" must not
        be read as covering the controller's board, which is what a general
        parser would do."""
        from mtg.helpers import creature_damage_after_prevention
        game = _game()
        bear = self._attacker(game)
        game.players[0].battlefield.append(Card(
            name="Glacial Chasm", id="chasm", type_line="Land",
            oracle_text="Prevent all damage that would be dealt to you."))

        after, _ = creature_damage_after_prevention(
            game, bear, None, 5, is_combat=True)

        assert after == 5


class TestFixedAmountShield:
    def test_a_shield_absorbs_only_its_amount(self):
        from mtg.helpers import creature_damage_after_prevention
        game = _game()
        bear = Card(name="Bear", id="bear", type_line="Creature — Bear",
                    power="2", toughness="2")
        bear._damage_shield = 2
        game.players[0].battlefield.append(bear)

        after, why = creature_damage_after_prevention(
            game, bear, None, 5, is_combat=True)

        assert after == 3, "a shield is partial, not all-or-nothing"
        assert bear._damage_shield == 0, "and it is consumed"
        assert "absorbed 2" in why

    def test_a_shield_is_spent_across_separate_damage_events(self):
        from mtg.helpers import creature_damage_after_prevention
        game = _game()
        bear = Card(name="Bear", id="bear", type_line="Creature — Bear",
                    power="2", toughness="2")
        bear._damage_shield = 3
        game.players[0].battlefield.append(bear)

        first, _ = creature_damage_after_prevention(game, bear, None, 1,
                                                    is_combat=True)
        second, _ = creature_damage_after_prevention(game, bear, None, 5,
                                                     is_combat=True)

        assert first == 0 and second == 3
        assert bear._damage_shield == 0

    def test_the_action_is_the_shield_producer(self):
        """A consumer with no producer is dead code, so they ship together."""
        from mtg.actions import execute_action_on_state
        game = _game()
        bear = Card(name="Bear", id="bear", type_line="Creature — Bear",
                    power="2", toughness="2")
        game.players[0].battlefield.append(bear)

        execute_action_on_state(
            None, game,
            {"action": "prevent_next_damage", "card": "Bear", "amount": 2})

        assert bear._damage_shield == 2

    def test_the_action_is_documented_for_tier_three(self):
        """Tier 3 cannot emit vocabulary it was never shown, and both prompt
        blocks have drifted apart before."""
        import inspect

        import mtg.judge as judge_mod
        src = inspect.getsource(judge_mod)
        assert src.count('"prevent_next_damage"') >= 2
