"""July 30, 2026 batch-9 reviewer wave (4 Sonnet reviewers, one game each,
sampled by recency-of-attention: pauper burn mirror, aminatou/voltron,
cascade/mythic, devotion/layers). 16 findings, 0 flat false positives —
the never-recently-examined complement paid out again.

Pinned here (fixed same session):
- R1  Searing Blaze's compound targets collapsed to {PLAYER, PLANESWALKER};
      Comet Storm's "choose any target," read as unplayable all game.
- R4  An exiled Batterskull kept granting its Germ +4/+4/lifelink (CR 301.5).
- R5  Animate Dead's -1/-0 stuck on the WRONG creature (generic aura ETB
      auto-attach ran before the reanimate template; the bind never set
      attached_to).
- R6  Sun Titan returned an INSTANT to the battlefield (CR 110.1).
- R7  The equipment-ETB attach watcher was hardcoded to Hammer of Nazahn by
      name — Sigarda's Aid did nothing for 8 Equipment casts.
- R8  Khalni Heart Expedition's whole activation cost (remove THREE quest
      counters + "sacrifice it") was unenforced — activated at 1 counter,
      free, twice, never sacrificed.
- R9  "{R}: This creature gets +1/+0" via Tier 3 buffed the whole team (the
      documented pump vocabulary was player-scoped only).
- R13 Basri's Lieutenant's "if it had a +1/+1 counter on it" was fabricated
      as true by Tier 3 on all 5 firings (CR 603.4).
- R10 A cascaded Mana Drain granted its counter-contingent mana with nothing
      countered (source pin; the decline now happens before any tier).
"""
import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _engine():
    from mtg.engine import GameEngine
    return GameEngine(None)


SEARING_BLAZE = (
    "Searing Blaze deals 1 damage to target player or planeswalker and 1 "
    "damage to target creature that player or that planeswalker's "
    "controller controls.\n"
    "Landfall — If you had a land enter the battlefield under your control "
    "this turn, Searing Blaze deals 3 damage to that player or planeswalker "
    "and 3 damage to that creature instead.")

COMET_STORM = (
    "Multikicker {1} (You may pay an additional {1} any number of times as "
    "you cast this spell.)\n"
    "Choose any target, then choose another target for each time this "
    "spell was kicked. Comet Storm deals X damage to each of them.")


class TestCompoundTargetClauses:
    def test_searing_blaze_needs_a_creature(self, make_game, make_card):
        from rules.targeting_helpers import _find_any_valid_target
        game = make_game()
        blaze = make_card("Searing Blaze", type_line="Instant",
                          mana_cost="{R}{R}", cmc=2, oracle_text=SEARING_BLAZE)
        assert _find_any_valid_target(game, blaze, "Rick") is False, (
            "both targets are mandatory — with no creature anywhere the "
            "spell is uncastable (CR 601.2c)")
        game.players[1].battlefield.append(make_card("Bear"))
        assert _find_any_valid_target(game, blaze, "Rick") is True

    def test_comet_storm_any_target_is_always_castable(self, make_game, make_card):
        from rules.targeting_helpers import _find_any_valid_target
        game = make_game()
        storm = make_card("Comet Storm", type_line="Instant",
                          mana_cost="{X}{R}{R}", cmc=2, oracle_text=COMET_STORM)
        assert _find_any_valid_target(game, storm, "Rick") is True, (
            "'choose any target,' — a player always exists; the comma "
            "defeated the old phrase capture and it read as unplayable")

    def test_swan_song_whole_phrase_capture_preserved(self, make_game, make_card):
        from mtg.models import StackEntry
        from rules.targeting_helpers import _find_any_valid_target
        game = make_game()
        song = make_card("Swan Song", type_line="Instant", mana_cost="{U}",
                         cmc=1, oracle_text=(
                             "Counter target enchantment, instant, or sorcery "
                             "spell. Its controller creates a 2/2 blue Bird "
                             "creature token with flying."))
        assert _find_any_valid_target(game, song, "Rick") is False
        spell = make_card("Opt", type_line="Instant", cmc=1)
        game.stack.append(StackEntry(card=spell, controller_name="Claude",
                                     controller_index=1))
        assert _find_any_valid_target(game, song, "Rick") is True, (
            "July 21 Swan Song regression check — the clause split must not "
            "re-truncate the type list")


class TestEquipmentBonusRequiresBattlefield:
    def test_exiled_equipment_stops_granting(self, make_game, make_card):
        game = make_game()
        rick = game.players[0]
        skull = make_card("Batterskull",
                          type_line="Artifact — Equipment",
                          power="0", toughness="0",
                          oracle_text=("Living weapon\nEquipped creature gets "
                                       "+4/+4 and has vigilance and lifelink.\n"
                                       "Equip {5}"))
        germ = make_card("Phyrexian Germ",
                         type_line="Token Creature — Phyrexian Germ",
                         power="0", toughness="0")
        germ.attachments = [skull.id]
        skull.attached_to = germ.id
        rick.battlefield.extend([skull, germ])

        assert germ.get_effective_power(game) == 4

        rick.battlefield.remove(skull)
        rick.exile.append(skull)
        # attachments list deliberately left stale — the live bug's shape
        assert germ.get_effective_power(game) == 0, (
            "an EXILED Batterskull kept granting +4/+4 for two combats "
            "(8 phantom damage + 8 phantom lifelink, CR 301.5)")


class TestAnimateDeadAttachment:
    ANIMATE = ("Enchant creature card in a graveyard\n"
               "When this Aura enters, if it's on the battlefield, it loses "
               "\"enchant creature card in a graveyard\" and gains \"enchant "
               "creature put onto the battlefield with this Aura.\" Return "
               "enchanted creature card to the battlefield under your control "
               "and attach this Aura to it. When this Aura leaves the "
               "battlefield, that creature's controller sacrifices it.\n"
               "Enchanted creature gets -1/-0.")

    def test_graveyard_aura_skips_battlefield_auto_attach(
            self, rules, game, make_card, capsys):
        from mtg.actions import execute_action_on_state
        rick = game.players[0]
        thassa = make_card("Thassa, Deep-Dwelling",
                           type_line="Legendary Enchantment Creature — God",
                           power="6", toughness="5")
        rick.battlefield.append(thassa)
        animate = make_card("Animate Dead", type_line="Enchantment — Aura",
                            oracle_text=self.ANIMATE)
        rick.graveyard.append(animate)

        execute_action_on_state(rules, game, {
            "action": "move_card", "card": "Animate Dead",
            "from_zone": "graveyard", "to_zone": "battlefield",
            "player": "Rick"})

        assert animate.attached_to is None, (
            "the generic ETB fallback attached Animate Dead to a LIVE "
            "creature — its -1/-0 stuck on Thassa all game")
        assert animate.id not in (getattr(thassa, 'attachments', None) or [])
        assert "skipping battlefield auto-attach" in capsys.readouterr().out

    def test_reanimate_binds_and_attaches_the_aura(
            self, rules, game, make_card):
        from mtg.actions import execute_action_on_state
        rick = game.players[0]
        animate = make_card("Animate Dead", type_line="Enchantment — Aura",
                            oracle_text=self.ANIMATE)
        rick.battlefield.append(animate)
        angel = make_card("Restoration Angel",
                          type_line="Creature — Angel", power="3",
                          toughness="4", cmc=4)
        rick.graveyard.append(angel)

        execute_action_on_state(rules, game, {
            "action": "reanimate", "player": "Rick",
            "card": "Restoration Angel", "own_graveyard": True,
            "_source_card_name": "Animate Dead"})

        assert angel in rick.battlefield
        assert animate.attached_to == angel.id, (
            "the bind set _bound_creature_id but never attached_to, so the "
            "P/T layer read the aura as attached to whatever the ETB "
            "fallback picked")
        assert animate.id in angel.attachments


class TestSunTitanPermanentFilter:
    def test_instant_is_never_returned(self):
        from rules.effect_templates import get_effect_library
        lib = get_effect_library()
        unmaking = SimpleNamespace(name="Anguished Unmaking",
                                   type_line="Instant", cmc=3,
                                   is_creature=lambda: False)
        bear = SimpleNamespace(name="Grizzly Bears",
                               type_line="Creature — Bear", cmc=2,
                               is_creature=lambda: True)
        ctx = {'controller_graveyard': [unmaking, bear]}
        actions = lib._gen_sun_titan("Rick", "Claude", ctx)
        assert actions[0]["action"] == "move_card"
        assert actions[0]["card"] == "Grizzly Bears"

        ctx = {'controller_graveyard': [unmaking]}
        actions = lib._gen_sun_titan("Rick", "Claude", ctx)
        assert actions[0]["action"] == "no_action", (
            "an Instant returned to the battlefield sat there for 20+ "
            "turns (CR 110.1 — instants are never permanents)")


class TestSigardasAidAttach:
    def test_sigardas_aid_attaches_entering_equipment(
            self, make_game, make_card, rules):
        from mtg.triggers import _check_equipment_etb_watchers
        game = make_game()
        aid = make_card("Sigarda's Aid", type_line="Enchantment",
                        oracle_text=("You may cast Aura and Equipment spells "
                                     "as though they had flash.\n"
                                     "Whenever an Equipment you control "
                                     "enters, you may attach it to target "
                                     "creature you control."))
        sword = make_card("Shadowspear", type_line="Legendary Artifact — Equipment",
                          oracle_text=("Equipped creature gets +1/+1 and has "
                                       "trample and lifelink.\nEquip {3}"))
        bear = make_card("Bear")
        game.players[0].battlefield.extend([aid, sword, bear])
        engine = SimpleNamespace(rules=rules)

        messages = _check_equipment_etb_watchers(
            engine, game, game.players[0], sword)

        assert sword.attached_to == bear.id, (
            "Sigarda's Aid's core ability did nothing for 8 Equipment casts "
            "— the watcher was hardcoded to Hammer of Nazahn by name")
        assert messages and "Sigarda's Aid" in messages[0]


KHALNI = ("Landfall — Whenever a land you control enters, you may put a "
          "quest counter on this enchantment.\n"
          "Remove three quest counters from this enchantment and sacrifice "
          "it: Search your library for up to two basic land cards, put them "
          "onto the battlefield tapped, then shuffle.")


class TestKhalniActivationCost:
    def _board(self, make_game, make_card, counters):
        engine = _engine()
        game = make_game()
        game._rules_engine = engine.rules
        engine.rules.engine_ref = engine
        rick = game.players[0]
        khalni = make_card("Khalni Heart Expedition",
                           type_line="Enchantment", power="0", toughness="0",
                           oracle_text=KHALNI)
        khalni.counters['quest'] = counters
        rick.battlefield.append(khalni)
        rick.library.extend(make_card(f"Forest {i}",
                                      type_line="Basic Land — Forest",
                                      power="0", toughness="0")
                            for i in range(3))
        return engine, game, rick, khalni

    def test_under_three_counters_refuses(self, make_game, make_card):
        engine, game, rick, khalni = self._board(make_game, make_card, 1)
        asyncio.run(engine._execute_action(
            game, 0, {"type": "activate",
                      "permanent": "Khalni Heart Expedition", "ability": 0}))
        assert khalni in rick.battlefield
        assert khalni.counters['quest'] == 1, (
            "activated at 1 counter for free, twice, in "
            "game_1532232990367682571")
        assert len(rick.battlefield) == 1, "no free land search"

    def test_three_counters_pays_and_sacrifices(self, make_game, make_card):
        engine, game, rick, khalni = self._board(make_game, make_card, 3)
        asyncio.run(engine._execute_action(
            game, 0, {"type": "activate",
                      "permanent": "Khalni Heart Expedition", "ability": 0}))
        assert khalni not in rick.battlefield, (
            "'... and sacrifice it' is a COST — the pronoun back-reference "
            "never matched the name/this-only detection")
        assert khalni in rick.graveyard


class TestSelfPumpScopedToSource:
    def test_this_creature_pump_hits_only_the_source(
            self, rules, game, make_card, capsys):
        from mtg.judge import resolve_effect
        rick = game.players[0]
        titan = make_card("Inferno Titan", type_line="Creature — Giant",
                          power="6", toughness="6")
        agent = make_card("Shardless Agent",
                          type_line="Artifact Creature — Human Rogue",
                          power="2", toughness="2")
        rick.battlefield.extend([titan, agent])

        _msgs, actions = asyncio.run(resolve_effect(
            rules, game, "This creature gets +1/+0 until end of turn.",
            source_card="Inferno Titan", controller="Rick"))

        assert "[RESOLVE-SELF-PUMP]" in capsys.readouterr().out
        assert len(actions) == 1 and actions[0]["card"] == "Inferno Titan"

        # Execute the emitted action against the real interpreter — the
        # scoping key must actually be read (process rule: never emit
        # vocabulary the handler doesn't consume). The pump lands as a
        # Layer 7c effect; refresh the cache like every real call site does.
        from mtg.actions import execute_action_on_state
        execute_action_on_state(rules, game, dict(actions[0]))
        game.recalculate_power_toughness()
        assert titan.get_effective_power(game) == 7
        assert agent.get_effective_power(game) == 2, (
            "the Tier-3 pump spread to every creature the controller owned")


class TestBasriInterveningIf:
    BASRI = ("Vigilance, protection from multicolored\n"
             "When this creature enters, put a +1/+1 counter on target "
             "creature you control.\n"
             "Whenever this creature or another creature you control dies, "
             "if it had a +1/+1 counter on it, create a 2/2 white Knight "
             "creature token with vigilance.")

    def _board(self, make_game, make_card):
        engine = _engine()
        game = make_game()
        claude = game.players[1]
        basri = make_card("Basri's Lieutenant",
                          type_line="Creature — Human Knight",
                          power="3", toughness="4", oracle_text=self.BASRI)
        knight = make_card("Knight Token", type_line="Token Creature — Knight",
                           power="2", toughness="2")
        knight.is_token = True
        claude.battlefield.append(basri)
        return engine, game, claude, basri, knight

    def test_counterless_death_is_gated(self, make_game, make_card, capsys):
        from mtg.triggers import _check_dies_triggers_sync
        engine, game, claude, basri, knight = self._board(make_game, make_card)

        _msgs, unhandled = _check_dies_triggers_sync(engine, game, knight, claude)

        out = capsys.readouterr().out
        assert "intervening-if not met" in out, (
            "Tier 3 FABRICATED the counter condition on all 5 firings — "
            "counterless Knights minted replacement Knights in a loop")
        assert not any(c.name == "Basri's Lieutenant" for c, _t in unhandled)

    def test_countered_death_passes_the_gate(self, make_game, make_card, capsys):
        from mtg.triggers import _check_dies_triggers_sync
        engine, game, claude, basri, knight = self._board(make_game, make_card)
        knight.counters['+1/+1'] = 1

        _msgs, unhandled = _check_dies_triggers_sync(engine, game, knight, claude)

        assert "intervening-if not met" not in capsys.readouterr().out


class TestCascadeCounterDecline:
    def test_decline_happens_before_any_tier(self):
        """Source pin: the decline (bottom-of-library, no cast) must sit
        BEFORE the Tier-1.5 resolution — the old action-level block let
        Mana Drain's sibling schedule_delayed_trigger grant the
        counter-contingent {C} with nothing countered."""
        src = (ROOT / "mtg/triggers.py").read_text(encoding="utf-8")
        decline = src.find("Declining to cast")
        tier15 = src.find("Tier 1.5 resolved", decline)
        assert 0 < decline < tier15, "decline must preempt Tier 1.5"
        window = src[decline - 2000:decline + 1200]
        assert "caster.library.append(found_card)" in window, (
            "an uncast cascade hit goes to the library BOTTOM (CR 702.85a), "
            "not the graveyard")

    def test_scry_and_pump_scoping_are_documented(self):
        src = (ROOT / "mtg/judge.py").read_text(encoding="utf-8")
        assert src.count('"action": "scry"') >= 2, (
            "Bontu's Scry 1 was dropped — the vocabulary never offered scry")
        assert src.count('Add "card": "Name" to pump ONLY that one creature') >= 2
