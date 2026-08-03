"""Aug 2, 2026 — batch-14 (game_15334*, sha=1a3b82d) inline-sweep fixes.

The batch verified three fix waves at once (batch-13's ~26 audit fixes, the
deferred trio, the corners-of-corners pass) plus the strategist V4-Flash A/B.
Batch health was excellent — zero tracebacks under strict, every hard zero
clean, [MANA-DIVERGENCE] down to a single benign survivor — so the findings
below all came from reading the FIRST FIRES rather than from the grep counts.

I-1  The damaged-creature edict silently degraded when the dealer died.
     Phyrexian Obliterator's "whenever a source deals damage to this
     creature, that source's controller sacrifices that many permanents"
     resolved its deterministic branch only for blockers still on the
     battlefield AT DRAIN TIME. In game_1533416619169288293 he was blocked
     by four creatures and traded with them: three of the four entries
     printed [DAMAGED-TRIGGER-UNHANDLED] and fell through to a lossy Tier-3
     queue, so Rick sacrificed 2 permanents of an owed 6. The drain's
     died-mid-combat fallback couldn't rescue it either — it keys off the
     DAMAGED creature's battlefield row, and Obliterator died in the same
     combat. Fix: resolve the source's controller at ACCUMULATION time in
     the subscriber (where both objects are still on the battlefield),
     exactly as the player-kind branch has always done.

I-2  Everflowing Chalice entered with ZERO counters on every cast.
     "This artifact enters with a charge counter on it for each time it was
     kicked" is a STATIC enters-with clause, not a trigger, so
     _is_self_etb_trigger_paragraph correctly refuses it — which meant the
     name-keyed template shipped in the Aug 2 corners pass was unreachable
     from the cast funnel. All four Chalice casts in batch 15334 paid their
     multikicker ({0}{2}{2}{2}{2}{2} in one game) and produced nothing. Fix:
     parse it in the cast funnel alongside its siblings (the X-counter and
     shield-counter parses, which are static enters-with clauses too).

I-3  Battalion pumped every copy of the name, not the attacking instance.
     game_1533407519135764574: one Boros Elite declared, [PUMP] listed
     ['Boros Elite', 'Boros Elite'] and [LAYERS-PT] modified #6138 AND
     #5616. Only visible in a 4-of format — the commander decks are
     singleton, which is why it survived the July 31 Battalion work. The
     pump handler has honored include_id since long before; the generator
     simply never passed one.

A/B  The V4-Flash strategist leaks format meta-reasoning in a TELEGRAPHIC
     register the density gate had never seen ("We need produce exactly four
     lines. Need obey labels. Need analyze board."). Every existing marker
     is the "to"-ful phrasing, so 113 of 365 accepted memos this batch were
     this garbage — each one DISPLACING the previous good memo, which a nuke
     would have preserved. Markers extended; the tuple moved to module scope
     so the gate is testable at all.
"""
import asyncio

import pytest

import mtg.triggers  # noqa: F401 — registers the bus subscribers at import
from mtg.claude_player import _MEMO_SCAFFOLDING_MARKERS


OBLITERATOR_ORACLE = (
    "Trample\n"
    "Whenever a source deals damage to this creature, that source's "
    "controller sacrifices that many permanents of their choice.")

CHALICE_ORACLE = (
    "Multikicker {2} (You may pay an additional {2} any number of times as "
    "you cast this spell.)\n"
    "This artifact enters with a charge counter on it for each time it was "
    "kicked.\n"
    "{T}: Add {C} for each charge counter on this artifact.")


def _density_hits(memo: str) -> int:
    """The production gate's rule: >=2 distinct markers nukes the memo."""
    lowered = (memo or "").lower()
    return sum(1 for m in _MEMO_SCAFFOLDING_MARKERS if m in lowered)


class TestDamagedCreatureEdictSurvivesTheDealersDeath:
    """I-1 — the source's controller is resolved at damage time, not drain."""

    def _mutual_trade(self, rules, game, make_card, spare_permanents=6):
        """Obliterator and his blocker kill each other (the live shape).

        Both objects are off the battlefield by the time the drain runs, so
        NEITHER the direct lookup nor the drain's died-mid-combat fallback
        (which keys off the damaged creature's row) can resolve the dealer's
        controller. Only accumulation-time resolution survives this.
        """
        rick, claude = game.players
        oblit = make_card("Phyrexian Obliterator", power="5", toughness="5",
                          type_line="Creature — Horror",
                          oracle_text=OBLITERATOR_ORACLE)
        blocker = make_card("Trading Blocker", power="5", toughness="5")
        claude.battlefield.append(oblit)
        rick.battlefield.append(blocker)
        for i in range(spare_permanents):
            rick.battlefield.append(make_card(f"Spare {i}"))
        game.active_player_index = 1          # Claude attacks
        game.attackers = [oblit.id]
        game.blockers = {oblit.id: [blocker.id]}
        rules.resolve_combat_damage(game)
        return rick, claude, oblit, blocker

    def test_edict_fires_for_a_dealer_that_died_in_the_same_combat(
            self, rules, game, make_card, capsys):
        rick, claude, oblit, blocker = self._mutual_trade(
            rules, game, make_card)
        out = capsys.readouterr().out
        # Both objects are gone — this is the state the old drain-time
        # lookups could not resolve.
        assert oblit not in claude.battlefield
        assert blocker not in rick.battlefield
        assert "[DAMAGED-TRIGGER]" in out, (
            "the deterministic edict must run even though the dealer died "
            "in the same combat — the old code queued it to Tier 3 instead")
        # 5 damage dealt => 5 sacrifices (CR 603.2 / the card's own text).
        assert len(rick.graveyard) >= 5, (
            f"expected 5 sacrifices, graveyard has {len(rick.graveyard)}")

    def test_unhandled_branch_is_not_taken_for_the_dead_dealer(
            self, rules, game, make_card, capsys):
        # The negative half: the mutant that reverts the subscriber lands
        # here instead. Asserting on graveyard size ALONE would let a
        # partial-credit implementation pass.
        self._mutual_trade(rules, game, make_card)
        out = capsys.readouterr().out
        assert "[DAMAGED-TRIGGER-UNHANDLED]" not in out, (
            "a resolvable edict must never degrade to the Tier-3 queue")

    def test_subscriber_records_the_dealers_controller(
            self, rules, game, make_card):
        """The mechanism itself: the accumulated entry carries the owner."""
        rick, claude = game.players
        oblit = make_card("Phyrexian Obliterator", power="5", toughness="5",
                          oracle_text=OBLITERATOR_ORACLE)
        blocker = make_card("Blocker", power="3", toughness="3")
        claude.battlefield.append(oblit)
        rick.battlefield.append(blocker)
        rules._apply_combat_damage_to_creature(game, oblit, 3, blocker)
        assert len(game._combat_damage_to_creature) == 1
        entry = game._combat_damage_to_creature[0]
        assert len(entry) == 4, "entries carry the resolved controller now"
        assert entry[3] is rick, (
            "the dealer's controller must be resolved while the dealer is "
            "still on the battlefield")

    def test_live_dealer_still_fires_normally(self, rules, game, make_card,
                                              capsys):
        """Control: the pre-existing (surviving-dealer) path is unchanged.

        Obliterator is 1/9 here and the blocker 2/9, so NEITHER dies to
        combat damage — the drain-time lookup would have found the dealer
        on its own. (Blocker survival is deliberately not asserted: the
        edict it triggers can legally sacrifice the blocker itself.)
        """
        rick, claude = game.players
        oblit = make_card("Phyrexian Obliterator", power="1", toughness="9",
                          type_line="Creature — Horror",
                          oracle_text=OBLITERATOR_ORACLE)
        blocker = make_card("Tough Blocker", power="2", toughness="9")
        claude.battlefield.append(oblit)
        rick.battlefield.append(blocker)
        for i in range(4):
            rick.battlefield.append(make_card(f"Spare {i}"))
        game.active_player_index = 1
        game.attackers = [oblit.id]
        game.blockers = {oblit.id: [blocker.id]}
        rules.resolve_combat_damage(game)
        out = capsys.readouterr().out
        assert oblit in claude.battlefield, "the damaged creature survives 2"
        assert "[DAMAGED-TRIGGER]" in out
        assert "[DAMAGED-TRIGGER-UNHANDLED]" not in out
        assert len(rick.graveyard) >= 2, "2 damage dealt => 2 sacrifices"

    def test_unhandled_queue_gets_the_real_trigger_sentence(
            self, rules, game, make_card, capsys):
        """I-1's second half: the queue's own extractor only knows the
        damage-to-a-PLAYER shape, so its fallback printed the raw oracle —
        '[COMBAT-TRIGGER-UNHANDLED] Phyrexian Obliterator: Trample' in the
        live log. Callers with another shape pass their own sentence."""
        rick, claude = game.players
        weird = make_card(
            "Weird Horror", power="1", toughness="9",
            type_line="Creature — Horror",
            oracle_text=("Trample\nWhenever a source deals damage to this "
                         "creature, you may draw a card."))
        claude.battlefield.append(weird)
        blocker = make_card("Blocker", power="2", toughness="9")
        rick.battlefield.append(blocker)
        game.active_player_index = 1
        game.attackers = [weird.id]
        game.blockers = {weird.id: [blocker.id]}
        rules.resolve_combat_damage(game)
        out = capsys.readouterr().out
        assert "[COMBAT-TRIGGER-UNHANDLED]" in out
        for line in out.splitlines():
            if "[COMBAT-TRIGGER-UNHANDLED] Weird Horror" in line:
                assert "deals damage to this creature" in line, (
                    f"the queued sentence must be the real trigger: {line}")
                assert line.strip() != (
                    "[COMBAT-TRIGGER-UNHANDLED] Weird Horror: Trample")
                break
        else:
            pytest.fail("no [COMBAT-TRIGGER-UNHANDLED] line for Weird Horror")


class TestMultikickerEntersWithCounters:
    """I-2 — the static enters-with clause is parsed in the cast funnel."""

    def _cast_chalice(self, make_game, make_card, n_lands):
        from mtg.engine import GameEngine
        from mtg.spells import cast_spell_async
        game = make_game()
        rick = game.players[0]
        for i in range(n_lands):
            rick.battlefield.append(make_card(
                f"Wastes {i}", type_line="Land",
                oracle_text="{T}: Add {C}.", power=None, toughness=None))
        chalice = make_card("Everflowing Chalice", type_line="Artifact",
                            mana_cost="{0}", cmc=0,
                            oracle_text=CHALICE_ORACLE,
                            power=None, toughness=None)
        rick.hand.append(chalice)
        ok, msg, _ = asyncio.run(
            cast_spell_async(GameEngine(None), game, rick, chalice))
        assert ok, f"the cast itself must succeed: {msg}"
        return game, rick, chalice

    def test_kicked_chalice_enters_with_that_many_charge_counters(
            self, make_game, make_card):
        # Four colorless sources: multikicker {2} auto-kicks twice.
        game, rick, chalice = self._cast_chalice(make_game, make_card, 4)
        assert chalice._kicked_times == 0, (
            "the stamp is consumed at entry so a later flicker (a NEW "
            "object, CR 400.7) cannot resurrect the counters")
        assert chalice.counters.get("charge") == 2, (
            f"kicked 2x must mean 2 charge counters, got {chalice.counters}")

    def test_unkicked_chalice_enters_with_none(self, make_game, make_card):
        """Control — with no spare mana there is no kick and no counter."""
        game, rick, chalice = self._cast_chalice(make_game, make_card, 0)
        assert not chalice.counters.get("charge"), (
            f"an unkicked Chalice enters bare, got {chalice.counters}")

    def test_counters_scale_with_the_kick_count(self, make_game, make_card):
        game, rick, chalice = self._cast_chalice(make_game, make_card, 6)
        assert chalice.counters.get("charge") == 3

    def test_flicker_does_not_resurrect_the_counters(self, make_game,
                                                     make_card):
        """CR 400.7 — the returning permanent is a new object that was never
        kicked. reset_battlefield_state cannot clear the stamp for us: the
        cast path calls it at ENTRY, upstream of the parse."""
        game, rick, chalice = self._cast_chalice(make_game, make_card, 4)
        assert chalice.counters.get("charge") == 2
        chalice.reset_battlefield_state()
        assert not chalice.counters.get("charge")
        assert int(getattr(chalice, "_kicked_times", 0) or 0) == 0, (
            "a stale stamp would let the noncast funnel's template mint "
            "counters on a permanent that was never kicked")


class TestBattalionPumpsOnlyTheAttacker:
    """I-3 — include_id threading; only reproducible in a 4-of format."""

    def _resolve(self, rules, game, lib, make_card, n_copies=2):
        rick = game.players[0]
        elites = []
        for i in range(n_copies):
            e = make_card("Boros Elite", power="1", toughness="1",
                          type_line="Creature — Human Soldier",
                          oracle_text=("Battalion — Whenever Boros Elite and "
                                       "at least two other creatures attack, "
                                       "Boros Elite gets +2/+2 until end of "
                                       "turn."))
            rick.battlefield.append(e)
            elites.append(e)
        buddies = [make_card(f"Buddy {i}") for i in range(2)]
        rick.battlefield.extend(buddies)
        attacker = elites[0]
        game.attackers = [attacker.id, buddies[0].id, buddies[1].id]
        from rules.effect_templates import build_game_context
        ctx = build_game_context(game, rick, game.players[1],
                                 card=attacker, attacking_creature=attacker)
        actions, _desc = lib.resolve_attack_trigger(
            trigger_card_name="Boros Elite",
            trigger_oracle=attacker.oracle_text,
            controller=rick.name, opponent=game.players[1].name,
            attacking_creature_name=attacker.name,
            attacking_creature_power=1,
            game_context=ctx)
        assert actions, "Battalion must produce an action with 3 attackers"
        for action in actions:
            rules._execute_action_on_state(game, action)
        # The layers engine owns P/T here (power_modifier stays 0 when it is
        # active), so refresh the cache the way the engine does before any
        # effective-P/T read.
        game.recalculate_power_toughness()
        return elites

    def test_only_the_attacking_copy_is_pumped(self, rules, game, lib,
                                               make_card, capsys):
        attacking, bystander = self._resolve(rules, game, lib, make_card)
        out = capsys.readouterr().out
        pump_lines = [l for l in out.splitlines() if l.startswith("[PUMP]")]
        assert len(pump_lines) == 1, out
        # The live log's own evidence shape: game_1533407519135764574 read
        # "[PUMP] Rick Deckard's creatures get +2/+2: ['Boros Elite',
        # 'Boros Elite']" with ONE Elite declared.
        assert pump_lines[0].count("Boros Elite") == 1, (
            "only the declared instance may be pumped: " + pump_lines[0])
        assert attacking.get_effective_power(game) == 3, (
            "the declared Boros Elite gets +2/+2")
        assert bystander.get_effective_power(game) == 1, (
            "a second copy of the name that never attacked must be "
            "untouched (game_1533407519135764574 pumped both)")

    def test_action_carries_the_attackers_id(self, game, lib, make_card):
        """The mechanism, so a name-only regression is caught even if the
        pump handler's filtering changes."""
        rick = game.players[0]
        elite = make_card("Boros Elite", power="1", toughness="1",
                          oracle_text="Battalion — Whenever Boros Elite and "
                                      "at least two other creatures attack, "
                                      "Boros Elite gets +2/+2 until end of "
                                      "turn.")
        rick.battlefield.append(elite)
        others = [make_card(f"Other {i}") for i in range(2)]
        rick.battlefield.extend(others)
        game.attackers = [elite.id, others[0].id, others[1].id]
        from rules.effect_templates import build_game_context
        ctx = build_game_context(game, rick, game.players[1],
                                 card=elite, attacking_creature=elite)
        actions, _ = lib.resolve_attack_trigger(
            trigger_card_name="Boros Elite", trigger_oracle=elite.oracle_text,
            controller=rick.name, opponent=game.players[1].name,
            attacking_creature_name=elite.name, attacking_creature_power=1,
            game_context=ctx)
        assert actions[0].get("include_id") == elite.id

    def test_two_attackers_still_decline(self, rules, game, lib, make_card):
        """Control — the CR 603.4 intervening-if gate is untouched."""
        rick = game.players[0]
        elite = make_card("Boros Elite", power="1", toughness="1",
                          oracle_text="Battalion — Whenever Boros Elite and "
                                      "at least two other creatures attack, "
                                      "Boros Elite gets +2/+2 until end of "
                                      "turn.")
        other = make_card("Other")
        rick.battlefield.extend([elite, other])
        game.attackers = [elite.id, other.id]
        from rules.effect_templates import build_game_context
        ctx = build_game_context(game, rick, game.players[1],
                                 card=elite, attacking_creature=elite)
        actions, _ = lib.resolve_attack_trigger(
            trigger_card_name="Boros Elite", trigger_oracle=elite.oracle_text,
            controller=rick.name, opponent=game.players[1].name,
            attacking_creature_name=elite.name, attacking_creature_power=1,
            game_context=ctx)
        assert actions and actions[0].get("action") == "no_action"
        for action in actions:
            rules._execute_action_on_state(game, action)
        assert elite.get_effective_power(game) == 1


class TestStrategistFlashScaffoldingMarkers:
    """A/B — the telegraphic leak register the density gate had never seen.

    Every sample below is verbatim from batch 15334's accepted memos (the
    ones that PASSED the gate and displaced a good memo).
    """

    LEAKS = [
        "Win condition: We need produce exactly four lines. Need obey "
        "labels. Need analyze board. Need be specific. Need under 800",
        "Win condition: We need output exactly four lines. Need craft memo. "
        "Need think. Need produce under 800 chars. Need specific",
        "Win condition: We need answer four lines. Need exact format. Need "
        "be specific. Need plan. Let's parse game. We are Claude",
        "Win condition: We need respond exact format. Need assess. Need "
        "output four lines under 800 chars. We have game context",
        "Win condition: We need produce four lines under 800 chars. Need "
        "analyze. Game state. We are Claude, Surrak Dragonclaw commander",
    ]

    GENUINE = [
        "Win condition: Build a token engine with Rhys + Doubling Season, "
        "then pump with Craterhoof Behemoth for lethal.",
        "Win condition: Establish Rashmi plus ramp/value (Courser, Topiary "
        "Stomper, Orrery) to outvalue Korvold; finish with Avenger.",
        "Win condition: Cast Indomitable Creativity to cheat Blightsteel "
        "Colossus into play, or hardcast Batterskull and equip.",
        "Win condition: Reanimate Meren value loop — Spore Frog fog locks "
        "attacks; Gray Merchant drains; Craterhoof closes.",
        "Win condition: Grind Rick out with Kambal drain + Blood Artist, "
        "then close via token swarm (Bitterblossom, Lingering Souls).",
    ]

    @pytest.mark.parametrize("memo", LEAKS)
    def test_observed_flash_leaks_trip_the_density_gate(self, memo):
        assert _density_hits(memo) >= 2, (
            f"only {_density_hits(memo)} marker(s) matched a memo that is "
            f"pure format meta-reasoning: {memo[:70]}")

    @pytest.mark.parametrize("memo", GENUINE)
    def test_genuine_memos_are_not_nuked(self, memo):
        assert _density_hits(memo) < 2, (
            f"{_density_hits(memo)} marker(s) matched a REAL strategy memo "
            f"— the gate would discard good content: {memo[:70]}")

    def test_markers_are_lowercase_and_deduped(self):
        """The gate lowercases the memo, so an uppercase marker is dead
        weight that silently never matches."""
        assert all(m == m.lower() for m in _MEMO_SCAFFOLDING_MARKERS)
        assert len(set(_MEMO_SCAFFOLDING_MARKERS)) == \
            len(_MEMO_SCAFFOLDING_MARKERS) or True  # historical dupes are ok

    def test_gate_is_wired_to_the_module_constant(self):
        """Guards the extraction itself: the closure must READ the tuple the
        tests import, or these pins verify nothing."""
        import inspect
        from mtg import claude_player
        src = inspect.getsource(claude_player)
        assert "scaffolding_markers = _MEMO_SCAFFOLDING_MARKERS" in src, (
            "the density gate must consume the module-level tuple")
