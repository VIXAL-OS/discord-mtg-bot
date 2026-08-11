"""Aug 11, 2026 fix wave — the recorded-not-fixed findings from the batch audit.

Four defects, two of them FOUNDATION-class by the recut gate's taxonomy:

F1 (foundation)  Adventure casts bypassed the CR 601.2c target-legality gate
    entirely. `spell_face_for_gates` special-cased split halves and had no
    adventure branch, so every gate judged the CREATURE face:
    `_spell_requires_targets` bails on "'instant' not in tl and 'sorcery' not
    in tl", and Hypnotic Sprite's type line is "Creature — Faerie". Mesmeric
    Glare was cast into an EMPTY STACK for {2}{U} and fizzled
    (game_1536535485969727558).

F2 (foundation)  `resolve_cast_target` walked the battlefield unconditionally
    before the (correctly gated) graveyard scan, so a graveyard-ONLY spell
    accepted a live permanent of the same name and satisfied the cast gate.
    Reanimate declared at a creature on the opponent's battlefield was
    accepted, paid for, and fizzled (game_1536540711770525827).

F5 (coverage)  "Whenever this creature blocks" had NO scan anywhere in the
    engine — every other event has one, and `_is_self_attack_trigger_paragraph`
    matches "attacks or blocks", so the paragraph looked served while being
    dispatched on attack only.

F6 (coverage)  "Whenever an enchantment is put into a graveyard from the
    battlefield" had no watcher; Femeref Enchantress was wholly inert through
    four qualifying events (game_1536546020802961548).
"""
import pytest

from mtg.helpers import spell_face_for_gates, resolve_cast_target
from mtg.triggers import (check_block_triggers,
                          _check_enchantment_to_graveyard_watchers,
                          _is_self_block_trigger_paragraph,
                          _check_ltb_triggers_sync)
from rules.targeting_helpers import _spell_requires_targets


def _engine(game):
    from mtg.engine import GameEngine
    e = GameEngine(None)
    game._rules_engine = e.rules
    e.rules.engine_ref = e
    return e


# --------------------------------------------------------------------------
# F1 — the adventure half must be what every CR 601.2c gate reads
# --------------------------------------------------------------------------

class TestAdventureFaceReachesTheCastGates:
    def _sprite(self, make_card):
        c = make_card("Hypnotic Sprite", type_line="Creature — Faerie",
                      oracle_text="Flying", mana_cost="{U}{U}",
                      power="2", toughness="1")
        c.adventure_name = "Mesmeric Glare"
        c.adventure_cost = "{2}{U}"
        c.adventure_type = "Instant — Adventure"
        c.adventure_text = "Counter target spell with mana value 3 or less."
        return c

    def test_the_gate_face_is_the_adventure_when_casting_it(self, make_card):
        c = self._sprite(make_card)
        c.cast_as_adventure = True
        face = spell_face_for_gates(c)
        assert face.name == "Mesmeric Glare"
        assert "instant" in face.type_line.lower()
        assert "counter target spell" in face.oracle_text.lower()

    def test_the_creature_face_is_used_when_not_casting_the_adventure(
            self, make_card):
        """CONTROL — the branch must be gated on cast_as_adventure."""
        c = self._sprite(make_card)
        c.cast_as_adventure = False
        face = spell_face_for_gates(c)
        assert face is c, "the creature half judges itself"

    def test_the_target_requirement_gate_now_sees_the_adventure(
            self, make_card):
        """The actual consequence: _spell_requires_targets returned False for
        every adventure cast because it read "Creature — Faerie", so the whole
        CR 601.2c block was skipped."""
        c = self._sprite(make_card)
        c.cast_as_adventure = True
        assert _spell_requires_targets(spell_face_for_gates(c)), (
            "Mesmeric Glare requires a target; the gate must see the "
            "adventure face's instant type line, not the creature's")
        c.cast_as_adventure = False
        assert not _spell_requires_targets(spell_face_for_gates(c)), (
            "the creature half has no cast-time target requirement")

    def test_a_missing_adventure_type_still_reads_as_instant_or_sorcery(
            self, make_card):
        """A cache row without the face type line must NOT fall back to the
        creature type — that would silently re-open the gate this closes.
        CR 715.2a: an adventure half is an instant or sorcery by definition."""
        c = self._sprite(make_card)
        c.adventure_type = ""
        c.cast_as_adventure = True
        face = spell_face_for_gates(c)
        tl = face.type_line.lower()
        assert "instant" in tl or "sorcery" in tl
        assert "creature" not in tl


# --------------------------------------------------------------------------
# F2 — graveyard-only spells must not resolve to a battlefield permanent
# --------------------------------------------------------------------------

REANIMATE = ("Put target creature card from a graveyard onto the battlefield "
             "under your control. You lose life equal to its mana value.")


class TestGraveyardTargetingResolvesToTheGraveyard:
    def test_a_live_permanent_does_not_satisfy_a_graveyard_only_spell(
            self, game, make_card):
        rick, claude = game.players
        alive = make_card("Sram, Senior Edificer", type_line="Creature — Dwarf")
        claude.battlefield.append(alive)
        dead = make_card("Sram, Senior Edificer", type_line="Creature — Dwarf")
        rick.graveyard.append(dead)
        spell = make_card("Reanimate", type_line="Sorcery",
                          oracle_text=REANIMATE, power=None, toughness=None)
        got = resolve_cast_target(game, rick, spell, "Sram, Senior Edificer")
        assert got is dead, (
            "a graveyard-only spell must resolve to the graveyard card; "
            "returning the live permanent satisfied the CR 601.2c gate and "
            "the spell fizzled at resolution with the mana spent")

    def test_with_nothing_in_any_graveyard_it_does_not_grab_the_battlefield(
            self, game, make_card):
        rick, claude = game.players
        alive = make_card("Sram, Senior Edificer", type_line="Creature — Dwarf")
        claude.battlefield.append(alive)
        spell = make_card("Reanimate", type_line="Sorcery",
                          oracle_text=REANIMATE, power=None, toughness=None)
        got = resolve_cast_target(game, rick, spell, "Sram, Senior Edificer")
        assert got is not alive, (
            "no legal target exists — the cast should be REJECTED, and the "
            "retry loop then finds a real one")

    def test_an_ordinary_removal_spell_still_finds_the_battlefield(
            self, game, make_card):
        """CONTROL — reordering must not break the common case."""
        rick, claude = game.players
        alive = make_card("Bear", type_line="Creature — Bear")
        claude.battlefield.append(alive)
        bolt = make_card("Doom Blade", type_line="Instant",
                         oracle_text="Destroy target nonblack creature.",
                         power=None, toughness=None)
        assert resolve_cast_target(game, rick, bolt, "Bear") is alive

    def test_a_spell_that_merely_MENTIONS_a_graveyard_keeps_the_battlefield(
            self, game, make_card):
        """THE DECISIVE CONTROL for the gate, not just the reordering.

        Murderous Cut destroys a battlefield creature; its graveyard phrase
        comes from DELVE reminder text, not from a target. Gating the
        battlefield scan on `_reaches_graveyard` ALONE — without the
        no-battlefield-target-phrase half — silently makes every delve spell,
        and every genuinely dual-zone spell, unable to find its target.

        The previous control used Doom Blade, which has no graveyard phrase at
        all, so it never reached this branch and a mutant dodged it.
        """
        rick, claude = game.players
        alive = make_card("Bear", type_line="Creature — Bear")
        claude.battlefield.append(alive)
        cut = make_card(
            "Murderous Cut", type_line="Instant",
            oracle_text=("Destroy target creature. Delve (Each card you exile "
                         "from your graveyard while casting this spell pays "
                         "for {1}.)"),
            power=None, toughness=None)
        assert resolve_cast_target(game, rick, cut, "Bear") is alive, (
            "a delve spell mentions a graveyard but targets the battlefield")

    def test_a_dual_zone_spell_can_still_reach_the_battlefield(
            self, game, make_card):
        """Kolaghan's-Command-shaped wording: one mode targets a permanent,
        another reaches a graveyard. The battlefield must stay reachable."""
        rick, claude = game.players
        alive = make_card("Bear", type_line="Creature — Bear")
        claude.battlefield.append(alive)
        dual = make_card(
            "Dual Mode", type_line="Instant",
            oracle_text=("Destroy target artifact. Return target creature "
                         "card from your graveyard to your hand."),
            power=None, toughness=None)
        assert resolve_cast_target(game, rick, dual, "Bear") is alive


# --------------------------------------------------------------------------
# F5 — block triggers
# --------------------------------------------------------------------------

ARCHITECT = ("Vigilance\nWhenever this creature attacks or blocks, create a "
             "1/1 colorless Spirit creature token.")


class TestBlockTriggers:
    def test_attacks_or_blocks_is_recognized_as_a_block_trigger(
            self, make_card):
        c = make_card("Architect of Restoration")
        assert _is_self_block_trigger_paragraph(
            c, "Whenever this creature attacks or blocks, create a token.")

    def test_an_attack_only_trigger_is_not_a_block_trigger(self, make_card):
        """CONTROL — the shared 'attacks or blocks' wording is exactly why
        this class hid; an attack-only paragraph must stay out."""
        c = make_card("Hellrider")
        assert not _is_self_block_trigger_paragraph(
            c, "Whenever this creature attacks, it deals 1 damage.")

    def _blocked(self, game, make_card):
        rick, claude = game.players
        arch = make_card("Architect of Restoration",
                         type_line="Creature — Spirit",
                         oracle_text=ARCHITECT, power="4", toughness="4")
        rick.battlefield.append(arch)
        atk = make_card("Reckless Wurm", type_line="Creature — Wurm",
                        power="4", toughness="4")
        claude.battlefield.append(atk)
        game.blockers = {atk.id: [arch.id]}
        return _engine(game), rick, arch

    def test_blocking_fires_the_trigger(self, game, make_card):
        engine, rick, arch = self._blocked(game, make_card)
        before = len(rick.battlefield)
        msgs = check_block_triggers(engine, game)
        assert msgs, "the block trigger produced nothing"
        assert len(rick.battlefield) == before + 1, "the Spirit token"

    def test_it_fires_once_per_combat_not_once_per_damage_step(
            self, game, make_card):
        """The first-strike and regular steps are two resolve_combat_damage
        calls but ONE combat — without the dedupe a blocking token-maker
        makes two tokens."""
        engine, rick, arch = self._blocked(game, make_card)
        check_block_triggers(engine, game)
        after_first = len(rick.battlefield)
        assert not check_block_triggers(engine, game)
        assert len(rick.battlefield) == after_first

    def test_a_new_declare_blockers_re_arms_it(self, game, make_card):
        """An extra combat phase re-declares blockers, and the trigger
        legitimately fires again."""
        engine, rick, arch = self._blocked(game, make_card)
        check_block_triggers(engine, game)
        after_first = len(rick.battlefield)
        game._block_triggers_fired_ids = set()      # what declare-blockers does
        check_block_triggers(engine, game)
        assert len(rick.battlefield) == after_first + 1


# --------------------------------------------------------------------------
# F6 — enchantment put into a graveyard from the battlefield
# --------------------------------------------------------------------------

FEMEREF = ("Whenever an enchantment is put into a graveyard from the "
           "battlefield, draw a card.")


class TestEnchantmentToGraveyardWatcher:
    def _board(self, game, make_card):
        rick, _ = game.players
        fem = make_card("Femeref Enchantress",
                        type_line="Creature — Human Druid",
                        oracle_text=FEMEREF, power="1", toughness="2")
        rick.battlefield.append(fem)
        for i in range(4):
            rick.library.append(make_card(f"Lib {i}"))
        return _engine(game), rick

    def test_an_enchantment_hitting_the_graveyard_draws(self, game, make_card):
        engine, rick = self._board(game, make_card)
        rancor = make_card("Rancor", type_line="Enchantment — Aura",
                           power=None, toughness=None)
        _check_enchantment_to_graveyard_watchers(engine, game, rancor,
                                                 "graveyard")
        assert len(rick.hand) == 1

    def test_an_opponents_enchantment_also_counts(self, game, make_card):
        """The printed wording is "an enchantment", not "an enchantment you
        control" — the scope is deliberately unrestricted."""
        engine, rick = self._board(game, make_card)
        theirs = make_card("Their Aura", type_line="Enchantment — Aura",
                           power=None, toughness=None)
        _check_enchantment_to_graveyard_watchers(engine, game, theirs,
                                                 "graveyard")
        assert len(rick.hand) == 1

    def test_a_creature_hitting_the_graveyard_does_not(self, game, make_card):
        engine, rick = self._board(game, make_card)
        bear = make_card("Bear", type_line="Creature — Bear")
        _check_enchantment_to_graveyard_watchers(engine, game, bear,
                                                 "graveyard")
        assert len(rick.hand) == 0

    def test_an_enchantment_going_to_exile_does_not(self, game, make_card):
        """"...put into a GRAVEYARD from the battlefield" — exile is not it."""
        engine, rick = self._board(game, make_card)
        oring = make_card("Oblivion Ring", type_line="Enchantment",
                          power=None, toughness=None)
        _check_enchantment_to_graveyard_watchers(engine, game, oring, "exile")
        assert len(rick.hand) == 0

    def test_it_is_reached_through_the_real_ltb_scan(self, game, make_card):
        """END-TO-END: the watcher is hooked into _check_ltb_triggers_sync,
        which is what production actually calls. A pin that only drove the
        helper would pass with the hook missing."""
        engine, rick = self._board(game, make_card)
        rancor = make_card("Rancor", type_line="Enchantment — Aura",
                           power=None, toughness=None)
        _check_ltb_triggers_sync(engine, game, rancor, rick,
                                 destination="graveyard")
        assert len(rick.hand) == 1
