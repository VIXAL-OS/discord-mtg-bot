"""July 20 batch-3 audit, reviewer wave — regression pins.

Four Sonnet reviewers (one game each) on the game_15289* batch produced ~10
code-traced findings; every mechanism below was source-verified before its
fix (two reviewer mechanisms were corrected during verification: the
aura_invalid SBA handler DOES run the LTB scan — the Rancor gap was only the
phrasing gate; and the "Kambal stranded in hand" claim was pre-fix vintage,
not a live gap). Finding tags: M* = graveyard-vs-aminatou reviewer,
S* = sagas reviewer, V* = voltron reviewer, A* = Abyss-game reviewer.
"""
import asyncio

import pytest

from mtg.constants import Phase


def _engine():
    from mtg.engine import GameEngine
    return GameEngine(None)


MEREN_ORACLE = (
    "Whenever another creature you control dies, you get an experience counter.\n"
    "At the beginning of your end step, choose target creature card in your "
    "graveyard. If that card's mana value is less than or equal to the number "
    "of experience counters you have, return it to the battlefield. Otherwise, "
    "put it into your hand."
)

VICTIMIZE_ORACLE = (
    "Choose two target creature cards in your graveyard. Sacrifice a creature. "
    "If you do, return the chosen cards to the battlefield tapped."
)

RANCOR_ORACLE = (
    "Enchant creature\nEnchanted creature gets +2/+0 and has trample.\n"
    "When this Aura is put into a graveyard from the battlefield, return it "
    "to its owner's hand."
)

JITTE_ORACLE = (
    "Whenever equipped creature deals combat damage, put two charge counters "
    "on Umezawa's Jitte.\n"
    "Remove a charge counter from Umezawa's Jitte: Choose one —\n"
    "• Equipped creature gets +2/+2 until end of turn.\n"
    "• Target creature gets -1/-1 until end of turn.\n"
    "• You gain 2 life.\nEquip {2}"
)

HAMMER_ORACLE = (
    "Whenever Hammer of Nazahn or another Equipment you control enters, you "
    "may attach that Equipment to target creature you control.\n"
    "Equipped creature gets +2/+0 and has indestructible.\nEquip {4}"
)

BATTERSKULL_ORACLE = (
    "Living weapon (When this Equipment enters, create a 0/0 black Phyrexian "
    "Germ creature token, then attach this to it.)\n"
    "Equipped creature gets +4/+4 and has vigilance and lifelink.\n"
    "{3}: Return this Equipment to its owner's hand.\nEquip {5}"
)


class TestMerenEmptyTemplateNoOp:
    def test_empty_graveyard_meren_is_handled_not_queued(self, make_game, make_card):
        # M1 (game_1528946220087640286): _gen_meren_end_step returns [] as a
        # deliberate no-op when the graveyard has no creature cards, but the
        # dispatch sites treated [] as "unhandled" and queued for Tier 3 —
        # which hallucinated an illegal target (moved the sorcery Victimize
        # to hand, twice, violating "target creature card"). Library
        # contract: None = unhandled; [] = handled no-op.
        from mtg.triggers import _check_end_step_triggers_sync
        game = make_game()
        rick = game.players[0]
        game.active_player_index = 0
        meren = make_card("Meren of Clan Nel Toth",
                          type_line="Legendary Creature — Human Shaman",
                          oracle_text=MEREN_ORACLE)
        rick.battlefield.append(meren)
        rick.graveyard.append(make_card("Victimize", type_line="Sorcery",
                                        oracle_text=VICTIMIZE_ORACLE))

        msgs, unhandled = _check_end_step_triggers_sync(_engine(), game)

        assert not any(c.name == "Meren of Clan Nel Toth" for c, _t in unhandled), \
            "empty-graveyard Meren must be a handled no-op, not a Tier-3 escalation"


class TestVictimizeTargetGate:
    def test_choose_two_target_is_a_targeting_spell(self, make_card):
        # M2 (same game): the modal-spell exclusion matched the substring
        # "choose two" in "Choose two target creature cards in your
        # graveyard", so Victimize skipped the CR 601.2c gate and was cast
        # 3x with zero legal targets (paid, then fizzled at resolution).
        from rules.targeting_helpers import _spell_requires_targets
        victimize = make_card("Victimize", type_line="Sorcery",
                              oracle_text=VICTIMIZE_ORACLE)
        assert _spell_requires_targets(victimize) is True

    def test_true_modal_spell_still_excluded(self, make_card):
        from rules.targeting_helpers import _spell_requires_targets
        modal = make_card("Cryptic Command", type_line="Instant",
                          oracle_text="Choose two —\n• Counter target spell.\n"
                                      "• Return target permanent to its owner's hand.\n"
                                      "• Tap all creatures your opponents control.\n"
                                      "• Draw a card.")
        assert _spell_requires_targets(modal) is False


class TestLivingDeathEntryPlumbing:
    def test_returned_avenger_creates_plants(self, make_game, make_card):
        # M3 (same game): living_death moved 14 creatures to the battlefield
        # with no ETB firing, no static registration, no PERMANENT_ENTERED —
        # Avenger of Zendikar returned to a 12-land board with zero Plants.
        engine = _engine()
        game = make_game()
        rick = game.players[0]
        for i in range(4):
            rick.battlefield.append(make_card(f"Forest {i}",
                                              type_line="Basic Land — Forest"))
        rick.battlefield.append(make_card("Grizzly Bears",
                                          type_line="Creature — Bear",
                                          power="2", toughness="2"))
        rick.graveyard.append(make_card(
            "Avenger of Zendikar", type_line="Creature — Elemental",
            power="5", toughness="5",
            oracle_text="When this creature enters, create a 0/1 green Plant "
                        "creature token for each land you control."))

        engine.rules._execute_action_on_state(game, {"action": "living_death"})

        names = [c.name for c in rick.battlefield]
        assert "Avenger of Zendikar" in names
        assert names.count("Plant") == 4, \
            f"Avenger's ETB must fire on Living Death return (got {names})"

    def test_returned_creature_counters_cleared(self, make_game, make_card):
        # CR 400.7 side of M3: the old manual state-clear missed counters.
        engine = _engine()
        game = make_game()
        rick = game.players[0]
        rick.battlefield.append(make_card("Grizzly Bears",
                                          type_line="Creature — Bear",
                                          power="2", toughness="2"))
        dead = make_card("Walking Ballista", type_line="Artifact Creature — Construct",
                         power="0", toughness="0")
        dead.counters['+1/+1'] = 3
        rick.graveyard.append(dead)

        engine.rules._execute_action_on_state(game, {"action": "living_death"})

        back = next(c for c in rick.battlefield if c.name == "Walking Ballista")
        assert back.counters.get('+1/+1', 0) == 0


class TestBattlefieldMoveAttribution:
    def test_battlefield_move_message_names_the_player(self, make_game, make_card, rules):
        # S1 (game_1528957318224678980): "📦 **Forest** → battlefield" was
        # byte-identical for both players' Fall of the Thran land returns, so
        # _autoplay_send's Layer-1 dedup silently ate 2 of the 4 lines.
        game = make_game()
        rick = game.players[0]
        rick.graveyard.append(make_card("Forest", type_line="Basic Land — Forest"))

        msg = rules._execute_action_on_state(game, {
            "action": "move_card", "card": "Forest",
            "from_zone": "graveyard", "to_zone": "battlefield",
            "player": rick.name})

        assert "battlefield" in msg and rick.name in msg


class TestCastTriggerTokensUseFullPlumbing:
    def test_sigil_angel_is_real_token_and_fires_watchers(self, make_game, make_card):
        # S2 (same game): the fixed-N/N token regex branch appended a bare
        # Card() — no is_token flag, no creature-ETB watcher scan, no
        # PERMANENT_ENTERED emit — so Sigil of the Empty Throne's Angels
        # never triggered Aura Shards (and were invisible to the parity net).
        from mtg.triggers import _check_cast_triggers
        engine = _engine()
        game = make_game()
        rick = game.players[0]
        game.active_player_index = 0
        rick.battlefield.append(make_card(
            "Sigil of the Empty Throne", type_line="Enchantment",
            oracle_text="Whenever you cast an enchantment spell, create a 4/4 "
                        "white Angel creature token with flying."))
        soul_warden = make_card("Soul Warden", type_line="Creature — Human Cleric",
                                power="1", toughness="1",
                                oracle_text="Whenever another creature enters, "
                                            "you gain 1 life.")
        rick.battlefield.append(soul_warden)
        rick.life = 40

        spell = make_card("Pacifism", type_line="Enchantment — Aura",
                          oracle_text="Enchant creature\nEnchanted creature "
                                      "can't attack or block.")
        asyncio.run(_check_cast_triggers(engine, game, rick, spell))

        angels = [c for c in rick.battlefield if "Angel" in c.name]
        assert len(angels) == 1
        assert getattr(angels[0], 'is_token', False) is True
        assert rick.life == 41, "Soul Warden must see the Angel entering"


class TestNoncastLivingWeapon:
    def test_batterskull_noncast_entry_creates_attached_germ(self, make_game, make_card):
        # V1 (game_1528946322995150848): Stoneforge Mystic's cheat-into-play
        # did hand.remove + battlefield.append with no entry plumbing — the
        # Batterskull Germ never existed and the equipment was dead weight.
        # The Living Weapon branch now lives in the shared noncast entry
        # funnel (also covers reanimation / flicker / Living Death entries).
        from mtg.actions import _fire_noncast_battlefield_entry
        engine = _engine()
        game = make_game()
        rick = game.players[0]
        skull = make_card("Batterskull", type_line="Artifact — Equipment",
                          oracle_text=BATTERSKULL_ORACLE)
        rick.battlefield.append(skull)

        msgs = _fire_noncast_battlefield_entry(engine.rules, game, rick, skull)

        germs = [c for c in rick.battlefield if c.name == "Phyrexian Germ"]
        assert len(germs) == 1
        assert skull.attached_to == germs[0].id
        assert skull.id in germs[0].attachments


class TestRashmiSelfCastExclusion:
    def test_own_cast_does_not_fire_own_ongoing_trigger(self, make_game, make_card):
        # V2 (same game): "Whenever you cast your first spell each turn"
        # matched neither the a/an ongoing exclusion nor the self-name check,
        # so the fallback fired Rashmi's battlefield trigger off her OWN
        # casting while she was on the stack (CR 603.3a).
        from mtg.triggers import _check_cast_triggers
        engine = _engine()
        game = make_game()
        rick = game.players[0]
        rashmi = make_card(
            "Rashmi, Eternities Crafter",
            type_line="Legendary Creature — Elf Druid",
            oracle_text="Whenever you cast your first spell each turn, reveal "
                        "the top card of your library. If it's a nonland card "
                        "with mana value less than that spell's, you may cast "
                        "it without paying its mana cost.")

        msgs = asyncio.run(_check_cast_triggers(engine, game, rick, rashmi))

        assert not any("Rashmi" in m and "Cast trigger" in m for m in msgs), \
            "Rashmi's ongoing trigger must not fire off her own casting"

    def test_eldrazi_style_self_cast_trigger_still_fires(self, make_game, make_card):
        from mtg.triggers import _check_cast_triggers
        engine = _engine()
        game = make_game()
        rick = game.players[0]
        rick.library.extend(make_card(f"Filler {i}", type_line="Sorcery")
                            for i in range(3))
        sower = make_card(
            "Oblivion Sower", type_line="Creature — Eldrazi",
            oracle_text="When you cast this spell, target opponent exiles the "
                        "top four cards of their library, then you may put any "
                        "number of land cards that player owns from exile onto "
                        "the battlefield under your control.")

        msgs = asyncio.run(_check_cast_triggers(engine, game, rick, sower))

        assert any("exiles top" in m for m in msgs), \
            "genuine 'when you cast this spell' triggers must still fire"


class TestHammerSelfEtbClassification:
    def test_or_another_wording_classifies_as_self_etb(self, make_card):
        # V3 (same game): "Whenever Hammer of Nazahn or another Equipment you
        # control enters" never matched the subject-adjacent-to-"enters"
        # regex, so the self-attach template never ran on Hammer's own ETB.
        from mtg.triggers import _is_self_etb_trigger_paragraph
        hammer = make_card("Hammer of Nazahn",
                           type_line="Legendary Artifact — Equipment",
                           oracle_text=HAMMER_ORACLE)
        para = HAMMER_ORACLE.split("\n")[0]
        assert _is_self_etb_trigger_paragraph(hammer, para) is True

    def test_plain_watcher_wording_stays_excluded(self, make_card):
        from mtg.triggers import _is_self_etb_trigger_paragraph
        warden = make_card("Soul Warden", type_line="Creature — Human Cleric")
        assert _is_self_etb_trigger_paragraph(
            warden, "Whenever another creature enters, you gain 1 life.") is False


class TestEquipIntentRouting:
    def _jitte_game(self, make_game, make_card, lands=2, with_auriok=False):
        game = make_game()
        rick = game.players[0]
        game.active_player_index = 0
        jitte = make_card("Umezawa's Jitte",
                          type_line="Legendary Artifact — Equipment",
                          oracle_text=JITTE_ORACLE)
        bear = make_card("Grizzly Bears", type_line="Creature — Bear",
                         power="2", toughness="2")
        bear.summoning_sick = False
        rick.battlefield.extend([jitte, bear])
        for i in range(lands):
            isl = make_card(f"Island {i}", type_line="Basic Land — Island",
                            oracle_text="{T}: Add {U}.")
            rick.battlefield.append(isl)
        if with_auriok:
            rick.battlefield.append(make_card(
                "Auriok Steelshaper", type_line="Creature — Human Soldier",
                power="1", toughness="1",
                oracle_text="Equip costs you pay cost {1} less.\nAs long as "
                            "this creature is equipped, each creature you "
                            "control that's a Soldier or a Knight gets +1/+1."))
        return game, rick, jitte, bear

    def test_ability_zero_with_creature_target_reroutes_to_equip(
            self, make_game, make_card):
        # V4 (same game): Jitte's oracle-line order puts the charge-counter
        # modal at abilities[0] and Equip at [1]; the AI's standard equip
        # shape {"ability": 0, "target": <own creature>} died at "no charge
        # counter to remove" for 30+ turns. Own-creature target on an
        # Equipment is unambiguous equip intent.
        engine = _engine()
        game, rick, jitte, bear = self._jitte_game(make_game, make_card)

        result = asyncio.run(engine._execute_action(game, 0, {
            "type": "activate", "permanent": "Umezawa's Jitte",
            "ability": 0, "target": "Grizzly Bears"}))

        assert jitte.attached_to == bear.id, f"equip must happen (got: {result})"

    def test_equip_cost_reduction_applies(self, make_game, make_card):
        # V5 (same game): Auriok Steelshaper's "Equip costs you pay cost {1}
        # less" was never applied anywhere — with one land, Equip {2} is only
        # payable through the reduction.
        engine = _engine()
        game, rick, jitte, bear = self._jitte_game(
            make_game, make_card, lands=1, with_auriok=True)

        result = asyncio.run(engine._execute_action(game, 0, {
            "type": "activate", "permanent": "Umezawa's Jitte",
            "ability": 0, "target": "Grizzly Bears"}))

        assert jitte.attached_to == bear.id, \
            f"reduced equip {{1}} must be payable with one land (got: {result})"


class TestRancorReturnsToHand:
    def test_graveyard_from_battlefield_wording_returns_to_hand(
            self, make_game, make_card):
        # A1 (game_1528957329452830760): Rancor was permanently lost when its
        # creature died — the LTB scan only matched "leaves the battlefield"
        # / "leaves play", never the Aura-family "put into a graveyard from
        # the battlefield" wording. (The aura_invalid SBA handler DOES run
        # this scan — the reviewer's "never calls it" half was wrong; the
        # phrasing gate was the whole gap.)
        from mtg.triggers import _check_ltb_triggers_sync
        engine = _engine()
        game = make_game()
        rick = game.players[0]
        rancor = make_card("Rancor", type_line="Enchantment — Aura",
                           oracle_text=RANCOR_ORACLE)
        rancor.owner_index = 0
        rick.graveyard.append(rancor)  # caller moves it before the scan

        msgs = _check_ltb_triggers_sync(engine, game, rancor, rick,
                                        "graveyard")

        assert rancor in rick.hand
        assert rancor not in rick.graveyard
        assert any("returns to" in m for m in msgs)

    def test_exile_destination_does_not_bounce(self, make_game, make_card):
        from mtg.triggers import _check_ltb_triggers_sync
        engine = _engine()
        game = make_game()
        rick = game.players[0]
        rancor = make_card("Rancor", type_line="Enchantment — Aura",
                           oracle_text=RANCOR_ORACLE)
        rancor.owner_index = 0
        rick.exile.append(rancor)

        _check_ltb_triggers_sync(engine, game, rancor, rick, "exile")

        assert rancor not in rick.hand  # exiled Rancor stays exiled


class TestAnnotatedNameResolution:
    def test_pt_plus_keyword_suffix_resolves(self, make_card):
        # A4 (same game): "[COMBAT] Could not resolve attacker
        # 'Faerie Rogue(1/1)[flying]'" — the end-anchored parenthetical strip
        # failed when a [keywords] group followed the (P/T) group.
        from mtg.claude_player import _resolve_annotated_card_name
        rogue = make_card("Faerie Rogue", type_line="Creature — Faerie Rogue",
                          power="1", toughness="1")
        name_map = {"Faerie Rogue": rogue}
        assert _resolve_annotated_card_name(
            "Faerie Rogue(1/1)[flying]", name_map) is rogue
        assert _resolve_annotated_card_name(
            "Faerie Rogue(1/1)", name_map) is rogue
        assert _resolve_annotated_card_name("Faerie Rogue", name_map) is rogue
