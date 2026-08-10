"""Pins for the Aug 10, 2026 DEFERRED-QUEUE session.

These cover the "remaining 22" recorded at the end of the Aug 10 card-targeted
wave. Every premise was re-verified against source and against real oracle text
read from data/card_data_cache.json before anything was changed — three of the
recorded mechanisms were incomplete, and the corrections are pinned here rather
than only described.

Fixture discipline (the pin-shape-reachability ledger): oracle text comes from
the disk cache, not from memory, and every pin drives the entry point the live
path actually calls, not a helper in isolation.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from conftest import _make_card, _make_game  # noqa: E402

_CACHE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'data', 'card_data_cache.json')


def _oracle(name):
    with open(_CACHE_PATH, encoding='utf-8') as handle:
        entry = json.load(handle)[name.lower()]
    if entry.get('card_faces'):
        return entry['card_faces'][0].get('oracle_text', '') or ''
    return entry.get('oracle_text', '') or ''


# ===========================================================================
# A — MASS DAMAGE bypassed the replacement engine entirely.
#
# The MASS DAMAGE branch in mtg/spells.py did `c.damage_marked += dmg` raw,
# so Furnace of Rath / Gisela / Torbran / Fiery Emancipation / Insult // Injury
# / Angrath's Marauders were ALL silently inert against every board wipe --
# defeating the replacement_chain deck's entire premise -- while the "and each
# player" half FOUR LINES BELOW already routed through the player funnel.
#
# The obvious fix was wrong and is pinned against: apply_combat_damage_to_creature
# stamps is_combat_damage=True, which would misfile a sorcery's damage as combat
# damage. That flag is load-bearing in BOTH directions -- Solphim's registration
# gates on `not ev.is_combat_damage` and would have started firing wrongly.
# ===========================================================================

class TestMassDamageRoutesThroughReplacements:

    def _wipe(self, dmg_text, doubler_name=None):
        from mtg.spells import resolve_special_effects
        from mtg.rules_engine import RulesEngine

        game = _make_game()
        rick, claude = game.players
        rules = RulesEngine(None)
        game._rules_engine = rules

        class _Eng:
            pass
        engine = _Eng()
        engine.rules = rules

        victim = _make_card("Grizzly Bears", power="4", toughness="4")
        claude.battlefield.append(victim)

        if doubler_name:
            doubler = _make_card(doubler_name,
                                 type_line="Enchantment",
                                 oracle_text=_oracle(doubler_name))
            rick.battlefield.append(doubler)
            game.register_replacement_effects(doubler, rick.name)

        wipe = _make_card("Anger of the Gods", type_line="Sorcery",
                          oracle_text=dmg_text)
        resolve_special_effects(engine, game, rick, wipe)
        return victim

    def test_a_damage_doubler_applies_to_a_board_wipe(self):
        """Furnace of Rath is symmetric and doubles Anger of the Gods."""
        text = _oracle("anger of the gods")
        assert "deals 3 damage to each creature" in text

        plain = self._wipe(text)
        assert plain.damage_marked == 3, (
            f"baseline: unmodified wipe marks 3, got {plain.damage_marked}")

        doubled = self._wipe(text, doubler_name="furnace of rath")
        assert doubled.damage_marked == 6, (
            f"Furnace of Rath must double a board wipe (CR 614). "
            f"got {doubled.damage_marked} -- the raw `damage_marked +=` is back")

    def test_mass_damage_is_not_labelled_combat_damage(self):
        """The naive fix (reusing apply_combat_damage_to_creature) would stamp
        is_combat_damage=True. Solphim registers a NONcombat-only doubler, so
        it must fire here; a combat-labelled event would leave it inert."""
        import inspect
        from mtg import spells
        src = inspect.getsource(spells.resolve_special_effects)
        assert "apply_noncombat_damage_to_creature" in src, (
            "mass damage must route through the NONCOMBAT creature funnel")
        assert "apply_combat_damage_to_creature" not in src, (
            "a spell's damage is not combat damage -- is_combat_damage=True "
            "would wrongly silence Solphim and wrongly wake combat-only "
            "replacements")

    def test_devotion_gated_god_is_not_hit_as_a_creature(self):
        """is_creature() with no `game` bypasses the devotion type-flip gate
        (CR 207.4), so a below-threshold god read as a creature and took the
        wipe. The June-10 D4 class."""
        from mtg.spells import resolve_special_effects
        from mtg.rules_engine import RulesEngine

        game = _make_game()
        rick, claude = game.players
        rules = RulesEngine(None)
        game._rules_engine = rules

        class _Eng:
            pass
        engine = _Eng()
        engine.rules = rules

        erebos = _make_card(
            "Erebos, God of the Dead",
            type_line="Legendary Enchantment Creature — God",
            oracle_text=("Indestructible\nAs long as your devotion to black is "
                         "less than five, Erebos isn't a creature."),
            power="5", toughness="7")
        claude.battlefield.append(erebos)
        assert not erebos.is_creature(game), (
            "fixture precondition: devotion is 0, so Erebos is NOT a creature")

        wipe = _make_card("Anger of the Gods", type_line="Sorcery",
                          oracle_text=_oracle("anger of the gods"))
        resolve_special_effects(engine, game, rick, wipe)
        assert erebos.damage_marked == 0, (
            "a devotion-disabled god is not a creature and takes no "
            "'damage to each creature'")


class TestAngerOfTheGodsExileReplacement:
    """"If a creature dealt damage this way would die this turn, exile it
    instead." CR 700.4 — an exiled creature never DIES, so its dies-triggers
    must not fire. Live evidence was a Hangarback Walker minting Thopters off
    a death that per the rules never happened."""

    def _cast_wipe(self, wipe_name, victim_toughness="2"):
        from mtg.spells import resolve_special_effects
        from mtg.sba import process_state_based_actions
        from mtg.rules_engine import RulesEngine

        game = _make_game()
        rick, claude = game.players
        rules = RulesEngine(None)
        game._rules_engine = rules

        class _Eng:
            pass
        engine = _Eng()
        engine.rules = rules

        victim = _make_card(
            "Hangarback Walker",
            type_line="Artifact Creature — Construct",
            oracle_text=("When this creature dies, create a 1/1 colorless "
                         "Thopter artifact creature token with flying for each "
                         "+1/+1 counter on this creature."),
            power="2", toughness=victim_toughness)
        claude.battlefield.append(victim)

        wipe = _make_card(wipe_name, type_line="Sorcery",
                          oracle_text=_oracle(wipe_name))
        resolve_special_effects(engine, game, rick, wipe)
        process_state_based_actions(rules, game)
        return game, claude, victim

    def test_a_creature_killed_by_anger_is_exiled_not_buried(self):
        assert "would die this turn, exile it instead" in \
            _oracle("anger of the gods"), "fixture precondition"

        game, claude, victim = self._cast_wipe("anger of the gods")
        names_gy = [c.name for c in claude.graveyard]
        names_ex = [c.name for c in claude.exile]
        assert "Hangarback Walker" not in names_gy, (
            f"CR 700.4: exiled instead of dying. graveyard={names_gy}")
        assert "Hangarback Walker" in names_ex, (
            f"it must land in exile. exile={names_ex}")

    def test_the_exiled_creature_fires_no_dies_trigger(self):
        game, claude, _ = self._cast_wipe("anger of the gods")
        thopters = [c for c in claude.battlefield if "Thopter" in c.name]
        assert not thopters, (
            f"a creature that never died has no dies-trigger; got {thopters}")

    def test_a_wipe_without_the_clause_does_not_register(self):
        """Blasphemous Act resolves through the SAME branch and prints no
        exile clause — the gate is the whole printed phrase, not 'exile'."""
        assert "exile it instead" not in _oracle("blasphemous act")
        game, claude, victim = self._cast_wipe("blasphemous act",
                                               victim_toughness="2")
        assert "Hangarback Walker" in [c.name for c in claude.graveyard], (
            "an ordinary wipe still buries its victims")

    def test_the_replacement_expires_at_end_of_turn(self):
        """"...would die THIS TURN". The turn clamp is what makes the effect
        self-expiring with no cleanup pass; without it a creature Anger merely
        damaged would be exiled instead of dying for the rest of the GAME.
        (Pinned because the mutant that removes the clamp survived everything
        else in this class.)"""
        from mtg.spells import resolve_special_effects
        from mtg.sba import process_state_based_actions
        from mtg.rules_engine import RulesEngine

        game = _make_game()
        rick, claude = game.players
        rules = RulesEngine(None)
        game._rules_engine = rules

        class _Eng:
            pass
        engine = _Eng()
        engine.rules = rules

        # 5 toughness: takes Anger's 3 and lives.
        survivor = _make_card("Colossal Dreadmaw", power="6", toughness="5")
        claude.battlefield.append(survivor)

        wipe = _make_card("Anger of the Gods", type_line="Sorcery",
                          oracle_text=_oracle("anger of the gods"))
        resolve_special_effects(engine, game, rick, wipe)
        process_state_based_actions(rules, game)
        assert survivor in claude.battlefield, "fixture: it survives the wipe"

        # A later turn: damage wears off, then it dies to something else.
        game.turn_number += 1
        survivor.damage_marked = 99
        process_state_based_actions(rules, game)

        assert "Colossal Dreadmaw" in [c.name for c in claude.graveyard], (
            "on a LATER turn the creature dies normally — the replacement was "
            "scoped to 'this turn' and must have expired")
        assert "Colossal Dreadmaw" not in [c.name for c in claude.exile], (
            "an expired turn-scoped replacement must not still be exiling")

    def test_the_replacement_is_scoped_to_creatures_it_actually_damaged(self):
        """The condition is an id-set membership test, so a creature that
        entered AFTER the wipe is unaffected."""
        from mtg.sba import process_state_based_actions
        game, claude, _ = self._cast_wipe("anger of the gods")
        latecomer = _make_card("Latecomer", power="1", toughness="1")
        latecomer.damage_marked = 5
        claude.battlefield.append(latecomer)
        process_state_based_actions(game._rules_engine, game)
        assert "Latecomer" in [c.name for c in claude.graveyard], (
            "a creature Anger never damaged dies normally")


class TestAvacynGrantsIndestructibleToPermanents:
    """"Other permanents you control have indestructible." The grant ladder was
    creature-shaped end to end and had no `permanents you control have` pattern
    at all, so Akroma died under Avacyn to a board wipe."""

    def _board(self):
        game = _make_game()
        rick = game.players[0]
        avacyn = _make_card("Avacyn, Angel of Hope",
                            type_line="Legendary Creature — Angel",
                            oracle_text=_oracle("avacyn, angel of hope"),
                            power="8", toughness="8")
        akroma = _make_card("Akroma, Angel of Wrath",
                            type_line="Legendary Creature — Angel",
                            oracle_text=_oracle("akroma, angel of wrath"),
                            power="6", toughness="6")
        rick.battlefield.extend([avacyn, akroma])
        game.register_static_keyword_grants(avacyn, rick.name)
        return game, rick, avacyn, akroma

    def test_the_pattern_is_matched_at_all(self):
        assert "other permanents you control have indestructible" in \
            _oracle("avacyn, angel of hope").lower(), "fixture precondition"
        game, rick, avacyn, akroma = self._board()
        assert any("permanents you control" in e.applies_to.lower()
                   for e in game.layers_engine.effects), (
            "Avacyn must register a permanents-scoped grant; the ladder had no "
            "such pattern and matched nothing")

    def test_another_creature_you_control_gets_indestructible(self):
        game, rick, avacyn, akroma = self._board()
        assert game.has_granted_keyword(akroma, "Indestructible", rick.name), (
            "Akroma is a permanent Rick controls other than Avacyn")

    def test_the_grant_is_controller_scoped_and_excludes_the_source(self):
        game, rick, avacyn, akroma = self._board()
        claude = game.players[1]
        theirs = _make_card("Grizzly Bears", power="2", toughness="2")
        claude.battlefield.append(theirs)
        assert not game.has_granted_keyword(theirs, "Indestructible", claude.name), (
            "an opponent's permanent is not 'you control'")
        assert not game.has_granted_keyword(avacyn, "Indestructible", rick.name), (
            "'Other' excludes the source (CR 109.5) — Avacyn's own "
            "indestructible is printed separately, not granted")

    def test_the_anchor_declines_a_qualified_clause(self):
        """Unanchored, this pattern reaches 'legendary permanents you control
        have...' / 'As long as you're the monarch, permanents you control
        have...' and would grant unconditionally to EVERYTHING."""
        game = _make_game()
        rick = game.players[0]
        src = _make_card("Avacyn's Memorial", type_line="Legendary Artifact",
                         oracle_text="Legendary permanents you control have indestructible.")
        rick.battlefield.append(src)
        game.register_static_keyword_grants(src, rick.name)
        assert not any("permanents you control" in e.applies_to.lower()
                       for e in game.layers_engine.effects), (
            "a qualified clause must DECLINE rather than register an "
            "unconditional board-wide grant")


class TestCastDownNonlegendary:
    """types_excluded has been declared and consumed since it was added and was
    never written by anything — a wired-but-unfed field, the has_coven class
    inverted. Every other piece already existed."""

    def _restriction(self, text):
        from rules.targeting import TargetTextParser
        return TargetTextParser().parse(text)

    def test_nonlegendary_is_parsed_into_types_excluded(self):
        assert _oracle("cast down") == "Destroy target nonlegendary creature."
        r = self._restriction("destroy target nonlegendary creature")
        assert 'legendary' in r.types_excluded, (
            "the restriction must reach types_excluded, which the validator "
            "already iterates")

    def test_a_plain_creature_clause_excludes_nothing(self):
        r = self._restriction("destroy target creature")
        assert 'legendary' not in r.types_excluded

    def test_the_word_is_matched_whole(self):
        """'legendary' is a substring of 'nonlegendary' — the trap this
        codebase has shipped eleven times. A card that says only 'legendary'
        must not be read as excluding legendaries."""
        r = self._restriction("destroy target legendary creature")
        assert 'legendary' not in r.types_excluded, (
            "a POSITIVE legendary restriction must not become an exclusion")


# ===========================================================================
# B — qualifier-restricted anthems.
#
# Two sites had the SAME unanchored pattern: the layers REGISTRATION ladder
# (register_static_pt_effects) and the inline compute-on-read FALLBACK
# (_get_anthem_power/toughness_bonus). They are mutually exclusive per creature,
# so fixing only one leaves the bug reachable.
# ===========================================================================

class TestAnthemQualifierAnchor:

    def _register(self, name, extra=None):
        game = _make_game()
        rick = game.players[0]
        src = _make_card(name, type_line="Enchantment", oracle_text=_oracle(name))
        rick.battlefield.append(src)
        for c in (extra or []):
            rick.battlefield.append(c)
        game.register_static_pt_effects(src, rick.name)
        return game, rick, src

    def test_full_moons_rise_buffs_only_werewolves(self):
        """'Werewolf creatures you control get +1/+0' -- the qualifier was
        swallowed and every creature got the buff."""
        text = _oracle("full moon's rise")
        assert text.lower().startswith("werewolf creatures you control get")

        wolf = _make_card("Howlpack Alpha",
                          type_line="Creature — Werewolf", power="3", toughness="3")
        human = _make_card("Elite Vanguard",
                           type_line="Creature — Human Soldier", power="2", toughness="1")
        game, rick, _ = self._register("full moon's rise", [wolf, human])
        game.recalculate_power_toughness()

        assert wolf.get_effective_power(game) == 4, (
            f"the Werewolf gets +1/+0, got {wolf.get_effective_power(game)}")
        assert human.get_effective_power(game) == 2, (
            f"a Human is NOT a Werewolf and must not be buffed, got "
            f"{human.get_effective_power(game)} -- the anchor regressed")

    def test_nightpack_ambusher_buffs_both_listed_subtypes(self):
        """'Other Wolves and Werewolves you control get +1/+1' matched NEITHER
        the single-subtype branch nor own_all, so it registered nothing."""
        text = _oracle("nightpack ambusher")
        assert "other wolves and werewolves you control get +1/+1" in text.lower()

        wolf = _make_card("Wolf", type_line="Creature — Wolf",
                          power="2", toughness="2")
        werewolf = _make_card("Ulvenwald Tracker",
                              type_line="Creature — Werewolf", power="1", toughness="1")
        bear = _make_card("Grizzly Bears", type_line="Creature — Bear",
                          power="2", toughness="2")

        game = _make_game()
        rick = game.players[0]
        src = _make_card("Nightpack Ambusher",
                         type_line="Creature — Wolf",
                         oracle_text=_oracle("nightpack ambusher"),
                         power="4", toughness="4")
        rick.battlefield.extend([src, wolf, werewolf, bear])
        game.register_static_pt_effects(src, rick.name)
        game.recalculate_power_toughness()

        assert wolf.get_effective_power(game) == 3, "a Wolf is buffed"
        assert werewolf.get_effective_power(game) == 2, "a Werewolf is buffed"
        assert bear.get_effective_power(game) == 2, (
            "a Bear is neither and must not be buffed")
        assert src.get_effective_power(game) == 4, (
            "'Other' excludes the source itself (CR 109.5)")

    def test_narfi_matches_a_supertype_in_the_list(self):
        """'Other snow and Zombie creatures you control' -- `snow` is a
        SUPERTYPE, not a subtype. This one previously leaked to own_all and
        buffed the entire board, which is strictly worse than not registering."""
        assert "other snow and zombie creatures you control get +1/+1" in \
            _oracle("narfi, betrayer king").lower()

        snowy = _make_card("Boreal Druid",
                           type_line="Snow Creature — Elf Druid",
                           power="1", toughness="1")
        zombie = _make_card("Zombie", type_line="Creature — Zombie",
                            power="2", toughness="2")
        plain = _make_card("Grizzly Bears", type_line="Creature — Bear",
                           power="2", toughness="2")

        game = _make_game()
        rick = game.players[0]
        src = _make_card("Narfi, Betrayer King",
                         type_line="Snow Legendary Creature — Zombie Noble",
                         oracle_text=_oracle("narfi, betrayer king"),
                         power="3", toughness="3")
        rick.battlefield.extend([src, snowy, zombie, plain])
        game.register_static_pt_effects(src, rick.name)
        game.recalculate_power_toughness()

        assert snowy.get_effective_power(game) == 2, "snow supertype qualifies"
        assert zombie.get_effective_power(game) == 3, "Zombie subtype qualifies"
        assert plain.get_effective_power(game) == 2, (
            "a plain Bear is neither snow nor Zombie -- the own_all leak is back")

    def test_beastmaster_ascension_still_registers_after_a_comma(self):
        """THE ANCHOR REGRESSION GUARD. Beastmaster Ascension prints
        '...quest counters on it, creatures you control get +5/+5' -- it is
        preceded by ', ', so a `^`/`\\n`/`\\.\\s` anchor kills a card that
        registers correctly today. Only the fixed-width lookbehind passes it."""
        text = _oracle("beastmaster ascension")
        assert ", creatures you control get +5/+5" in text.lower(), (
            "fixture precondition: the clause is comma-preceded")

        bear = _make_card("Grizzly Bears", power="2", toughness="2")
        game = _make_game()
        rick = game.players[0]
        src = _make_card("Beastmaster Ascension", type_line="Enchantment",
                         oracle_text=text)
        src.counters['quest'] = 7          # threshold met
        rick.battlefield.extend([src, bear])
        game.register_static_pt_effects(src, rick.name)
        game.recalculate_power_toughness()

        assert bear.get_effective_power(game) == 7, (
            f"Beastmaster Ascension at 7 quest counters gives +5/+5; got "
            f"{bear.get_effective_power(game)}. If this is 2, the anchor was "
            f"tightened to a line/sentence start and broke a working card.")

    def test_glorious_anthem_still_applies_to_everything(self):
        """The unqualified case must be untouched."""
        bear = _make_card("Grizzly Bears", power="2", toughness="2")
        game, rick, _ = self._register("glorious anthem", [bear])
        game.recalculate_power_toughness()
        assert bear.get_effective_power(game) == 3

    def test_inline_fallback_shares_the_anchor(self):
        """The compute-on-read path had the identical unanchored regex. It runs
        only when the layers engine registered no P/T effect at all -- which is
        exactly what happens now for a declined combat-state anthem, so a fix
        confined to the ladder would restore the bug through this door."""
        from mtg.models import _ANTHEM_INLINE_RE
        import re
        assert re.search(_ANTHEM_INLINE_RE,
                         "werewolf creatures you control get +1/+0") is None, (
            "the inline fallback must not swallow a subtype qualifier")
        assert re.search(_ANTHEM_INLINE_RE,
                         "attacking creatures you control get +1/+0") is None, (
            "nor a combat-state qualifier")
        assert re.search(_ANTHEM_INLINE_RE,
                         "creatures you control get +1/+1") is not None, (
            "the unqualified anthem must still match at string start")
        assert re.search(
            _ANTHEM_INLINE_RE,
            "as long as this has seven or more quest counters on it, "
            "creatures you control get +5/+5") is not None, (
            "and after ', ' -- Beastmaster Ascension")

    def test_own_all_pattern_rejects_a_qualifier(self):
        """The registration ladder's unqualified clause carries the same anchor.

        This is pinned DIRECTLY rather than through a card because, with the
        subtype branch above it generalized, every qualifier shape in the deck
        inventory is now claimed by an earlier branch — so an end-to-end pin
        passes with the anchor reverted and proves nothing (it did; the mutant
        survived). The anchor is defence-in-depth against a future reorder,
        and this asserts the property it actually provides.
        """
        import re
        from mtg.models import _ANTHEM_OWN_ALL_RE
        for qualified in ("werewolf creatures you control get +1/+0",
                          "attacking creatures you control get +1/+0",
                          "other creatures you control get +2/+2"):
            assert re.search(_ANTHEM_OWN_ALL_RE, qualified) is None, (
                f"own_all must not swallow the qualifier in {qualified!r}")
        assert re.search(_ANTHEM_OWN_ALL_RE,
                         "creatures you control get +1/+1") is not None
        assert re.search(
            _ANTHEM_OWN_ALL_RE,
            "...quest counters on it, creatures you control get +5/+5") is not None, (
            "Beastmaster Ascension's comma-preceded clause must still match")

    def test_instigator_gang_declines_rather_than_broadcasting(self):
        """'Attacking creatures you control get +1/+0' cannot be expressed as an
        applies_to string (LayeredPermanent.to_dict carries no attacking key and
        calculate_characteristics is called with game_state=None), so it declines
        with a breadcrumb. Under-applying is the safe direction; the bug being
        fixed is that it buffed the WHOLE BOARD."""
        assert _oracle("instigator gang").lower().startswith(
            "attacking creatures you control get")

        bear = _make_card("Grizzly Bears", power="2", toughness="2")
        game = _make_game()
        rick = game.players[0]
        src = _make_card("Instigator Gang", type_line="Creature — Human Werewolf",
                         oracle_text=_oracle("instigator gang"),
                         power="2", toughness="2")
        rick.battlefield.extend([src, bear])
        game.register_static_pt_effects(src, rick.name)
        game.recalculate_power_toughness()

        assert bear.get_effective_power(game) == 2, (
            f"a non-attacking creature must NOT get the attacking-only buff; "
            f"got {bear.get_effective_power(game)}")
        # The decisive half. Without the decline the clause still reaches the
        # subtype branch and registers applies_to="attackings you control",
        # which matches nobody — so a P/T assertion alone passes either way
        # (it did; the mutant survived). What actually differs is that a junk
        # effect gets added to the engine, where it persists, is iterated on
        # every recalc, and makes the dedup guard skip a later legitimate
        # re-registration of this card.
        assert not [e for e in game.layers_engine.effects
                    if e.id.startswith(f"{src.id}_")], (
            "a combat-state anthem must register NOTHING, not a junk effect "
            "that happens to match no one")
