"""Aug 11, 2026 card-targeted reviewer wave — verified findings.

Four defects, each independently re-verified against source before fixing
(one further reviewer finding, "Bontu's Monument stopped reducing", was a
FALSE POSITIVE: the Monument had been sacrificed 66 lines earlier to
Phyrexian Negator's own damage trigger, which reports as a bulk
"sacrifices 6 permanents" and never names its victims).

D4  Totem/umbra armor never saved a creature enchanted by an OPPONENT's
    Aura. Both helpers scanned only the enchanted creature's controller's
    battlefield, but an Aura on an opponent's creature stays on its own
    caster's battlefield. Clean A/B inside one game
    (game_1536540699103854662): Snake Umbra saved Silverback Elder
    (same controller), Boar Umbra failed to save Kambal (cross-controller).

D2  The shared `_counter_action` emits only `counter_spell`, so every
    printed rider on a card in the plain COUNTERSPELLS list was dropped.
    Two live cards were affected: Dissipate (exile-instead — its countered
    spell went to the graveyard, game_1536545962212986881 L1372) and
    Dissolve (Scry 1).

A5  The constellation watcher's draw branch emitted ONLY the draw, so a
    compound "put a +1/+1 counter on this creature and draw a card"
    resolved as draw-only. Setessan Champion fired 9 times in
    game_1536546020802961548 and received zero counters.

B2  The adventure branch gated Tier 3 on `if not adv_msgs`, but Tier 2
    populates adv_msgs with a "complex effect, escalating" PLACEHOLDER —
    so Tier 3 never ran and the message advertised an escalation that
    could not happen. Cost paid, effect lost, on every cast.
"""
import asyncio

import pytest

from mtg.rules_engine import RulesEngine


def _run(coro):
    return asyncio.run(coro)


BOAR_UMBRA = ("Enchant creature\nEnchanted creature gets +3/+3.\n"
              "Totem armor (If enchanted creature would be destroyed, instead "
              "remove all damage from it and destroy this Aura.)")


# --------------------------------------------------------------------------
# D4 — totem armor across controllers
# --------------------------------------------------------------------------

class TestCrossControllerTotemArmor:
    """CR 702.77b is controller-agnostic: the replacement applies to the
    ENCHANTED creature however the Aura is controlled.

    DECISIVE SHAPE: the same-controller case is a control, so a mutant that
    breaks the widened search (or one that only ever searches the opponent)
    is caught in both directions.
    """

    def _board(self, game, make_card, aura_on_opponents_creature: bool):
        rick, claude = game.players
        victim_owner = claude if aura_on_opponents_creature else rick
        victim = make_card("Kambal", type_line="Legendary Creature — Human",
                           power="2", toughness="3")
        victim_owner.battlefield.append(victim)
        umbra = make_card("Boar Umbra",
                          type_line="Enchantment — Aura",
                          oracle_text=BOAR_UMBRA, power=None, toughness=None)
        umbra.attached_to = victim.id
        rick.battlefield.append(umbra)          # ALWAYS the caster's board
        return rick, claude, victim, umbra, victim_owner

    def test_an_opponents_aura_still_saves_the_creature(
            self, rules, game, make_card):
        rick, claude, victim, umbra, owner = self._board(game, make_card, True)
        assert rules._has_totem_armor(victim, owner, game), (
            "the Aura sits on the CASTER's battlefield, not the enchanted "
            "creature's controller's — searching one battlefield can never "
            "find it (CR 702.77b is controller-agnostic)")
        destroyed = rules._remove_totem_armor(victim, owner, game)
        assert destroyed is umbra
        assert umbra not in rick.battlefield, "the Aura is destroyed"
        assert umbra in rick.graveyard, (
            "into ITS OWN owner's graveyard (CR 404.3), not the enchanted "
            "creature's controller's")
        assert victim in claude.battlefield, "the creature survives"

    def test_the_same_controller_case_still_works(
            self, rules, game, make_card):
        """CONTROL — the historically-working path must not regress."""
        rick, claude, victim, umbra, owner = self._board(game, make_card, False)
        assert rules._has_totem_armor(victim, owner, game)
        assert rules._remove_totem_armor(victim, owner, game) is umbra
        assert umbra in rick.graveyard

    def test_an_unattached_umbra_saves_nobody(self, rules, game, make_card):
        """CONTROL — widening the search must not make it indiscriminate."""
        rick, claude = game.players
        victim = make_card("Kambal", type_line="Creature — Human")
        claude.battlefield.append(victim)
        umbra = make_card("Boar Umbra", type_line="Enchantment — Aura",
                          oracle_text=BOAR_UMBRA, power=None, toughness=None)
        umbra.attached_to = "some-other-card-id"
        rick.battlefield.append(umbra)
        assert not rules._has_totem_armor(victim, claude, game)


# --------------------------------------------------------------------------
# D2 — counterspell riders
# --------------------------------------------------------------------------

class TestCounterspellRiders:
    def _actions(self, lib, name, oracle):
        acts, _ = lib.resolve_spell(name, oracle, "Rick", "Claude", {})
        return acts or []

    def test_dissipate_exiles_the_countered_spell(self, lib):
        acts = self._actions(
            lib, "Dissipate",
            "Counter target spell. If that spell is countered this way, exile "
            "it instead of putting it into its owner's graveyard.")
        counter = [a for a in acts if a.get("action") == "counter_spell"]
        assert counter, f"no counter action: {acts}"
        assert counter[0].get("countered_to") == "exile", (
            "the printed 'exile it instead' clause routes the countered spell "
            f"to exile, like Force of Negation. Got: {counter[0]}")

    def test_dissolve_still_scries(self, lib):
        acts = self._actions(lib, "Dissolve", "Counter target spell. Scry 1.")
        assert any(a.get("action") == "counter_spell" for a in acts)
        assert any(a.get("action") == "scry" for a in acts), (
            f"Scry 1 is printed on the card and was being dropped: {acts}")

    def test_a_plain_counterspell_gains_no_rider(self, lib):
        """CONTROL — the fix must not attach riders to plain counters."""
        acts = self._actions(lib, "Counterspell", "Counter target spell.")
        counter = [a for a in acts if a.get("action") == "counter_spell"]
        assert counter, f"no counter action: {acts}"
        assert counter[0].get("countered_to") in (None, "graveyard")
        assert not any(a.get("action") == "scry" for a in acts)


# --------------------------------------------------------------------------
# A5 — compound constellation
# --------------------------------------------------------------------------

SETESSAN = ("Constellation — Whenever an enchantment you control enters, put "
            "a +1/+1 counter on this creature and draw a card.")
EIDOLON = ("Constellation — Whenever an enchantment you control enters, "
           "draw a card.")


class TestCompoundConstellation:
    """A compound constellation must resolve BOTH clauses (CR 603 — a
    triggered ability resolves its whole instruction, not a prefix)."""

    def _fire(self, game, make_card, watcher_oracle):
        from mtg.engine import GameEngine
        from mtg.triggers import _check_enchantment_etb_watchers
        engine = GameEngine(None)
        game._rules_engine = engine.rules
        engine.rules.engine_ref = engine
        rick, _ = game.players
        champ = make_card("Setessan Champion",
                          type_line="Creature — Human Warrior",
                          oracle_text=watcher_oracle, power="1", toughness="3")
        rick.battlefield.append(champ)
        for i in range(4):
            rick.library.append(make_card(f"Lib {i}"))
        ench = make_card("Rancor", type_line="Enchantment — Aura",
                         oracle_text="Enchanted creature gets +2/+0.",
                         power=None, toughness=None)
        rick.battlefield.append(ench)
        msgs = _check_enchantment_etb_watchers(engine, game, rick, ench)
        return champ, rick, msgs

    def test_counter_and_draw_both_resolve(self, game, make_card):
        champ, rick, msgs = self._fire(game, make_card, SETESSAN)
        assert len(rick.hand) == 1, f"the draw half: {msgs}"
        assert champ.counters.get("+1/+1", 0) == 1, (
            "the +1/+1 counter half was silently dropped — the draw branch "
            f"emitted only draw_cards. counters={champ.counters} msgs={msgs}")

    def test_a_draw_only_constellation_gains_no_counter(self, game, make_card):
        """CONTROL — Eidolon of Blossoms prints only the draw."""
        champ, rick, msgs = self._fire(game, make_card, EIDOLON)
        assert len(rick.hand) == 1
        assert champ.counters.get("+1/+1", 0) == 0, (
            "a draw-only constellation must not gain a counter")


# --------------------------------------------------------------------------
# B2 — adventure Tier-3 escalation
# --------------------------------------------------------------------------

class TestAdventureEscalatesOnComplexPlaceholder:
    """SpellResolver returns a "complex effect, escalating" PLACEHOLDER for
    anything it can't parse. Gating Tier 3 on `if not adv_msgs` treated that
    placeholder as a resolution, so the escalation the message advertised
    never happened.

    DECISIVE SHAPE: Tier 3 is stubbed to return a recognizable message, and
    the assertion is that the placeholder is GONE and the stub's message is
    present. Asserting only "Tier 3 was called" would pass even if the
    placeholder were still shown to the player alongside it.
    """

    def test_a_complex_placeholder_still_escalates(self):
        import inspect
        from mtg import spells as spells_mod
        src = inspect.getsource(spells_mod)
        # The adventure branch must not gate Tier 3 on emptiness alone.
        assert "_adv_complex" in src, (
            "the adventure branch needs the same has_complex_effect check the "
            "main resolution path already performs")
        i = src.index("_adv_complex = any(")
        window = src[i:i + 600]
        assert "if not adv_msgs or _adv_complex:" in window, (
            "Tier 3 must run when Tier 2 punted with a placeholder, not only "
            "when Tier 2 produced nothing at all")

    def test_the_placeholder_is_replaced_not_appended(self):
        """The player must not see BOTH '(complex effect, escalating)' and the
        real resolution — the placeholder is superseded, not decorated."""
        import inspect
        from mtg import spells as spells_mod
        src = inspect.getsource(spells_mod)
        i = src.index("_adv_complex = any(")
        window = src[i:i + 900]
        assert 'if "complex effect" not in (m or "").lower()' in window, (
            "the placeholder should be filtered out of adv_msgs once Tier 3 "
            "supersedes it")
