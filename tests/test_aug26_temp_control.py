"""Temporary control (Act of Treason family) — Aug 26, 2026.

The gap: `steal_permanent` was permanent-only (no duration, no revert), no
Tier 1.5 pattern matched the family, and neither Tier-3 judge vocabulary
block documented ANY steal action — so "gain control ... until end of turn"
(a standard red common category, 87 single-target printings in the bulk) had
no correct resolution path anywhere. Found while grounding a design-skeleton
taxonomy audit; the chip's O-Kagachi reachability premise then dissolved
under re-verification (the sba.py back-face table text was FABRICATED — see
TestSagaBackFaceTable), which is its own finding.

Oracle constants below are bulk-verified verbatim (Aug 26 sweep); synthetic
texts are labeled synthetic.
"""

import pytest

from mtg.engine import GameEngine
from mtg.models import Card
from rules.effect_templates import build_game_context, get_effect_library


# Bulk-verified printed texts (data/scryfall_oracle_cards.json, Aug 26).
ACT_OF_TREASON = ("Gain control of target creature until end of turn. Untap "
                  "that creature. It gains haste until end of turn. (It can "
                  "attack and {T} this turn.)")
THREATEN = ("Untap target creature and gain control of it until end of turn. "
            "That creature gains haste until end of turn. (It can attack and "
            "{T} this turn.)")
CONQUERING_MANTICORE = ("Flying\nWhen this creature enters, gain control of "
                        "target creature an opponent controls until end of "
                        "turn. Untap that creature. It gains haste until end "
                        "of turn.")
WRANGLE = ("Gain control of target creature with power 4 or less until end "
           "of turn. Untap that creature. It gains haste until end of turn.")
CLAIM_THE_FIRSTBORN = ("Gain control of target creature with mana value 3 or "
                       "less until end of turn. Untap that creature. It "
                       "gains haste until end of turn.")
CHAMBER_OF_MANIPULATION = ('Enchant land\nEnchanted land has "{T}, Discard a '
                           'card: Gain control of target creature until end '
                           'of turn."')
KARI_ZEVS_EXPERTISE = ("Gain control of target creature or Vehicle until end "
                       "of turn. Untap it. It gains haste until end of turn."
                       "\nYou may cast a spell with mana value 2 or less "
                       "from your hand without paying its mana cost.")
INSURRECTION = ("Untap all creatures and gain control of them until end of "
                "turn. They gain haste until end of turn.")
TWISTED_FEALTY = ("Gain control of target creature until end of turn. Untap "
                  "that creature. It gains haste until end of turn.\nCreate "
                  "a Wicked Role token attached to up to one target creature. "
                  "(If you control another Role on it, put that one into the "
                  "graveyard. Enchanted creature gets +1/+1. When this token "
                  "is put into a graveyard, each opponent loses 1 life.)")
AGENT_OF_TREACHERY = ("When this creature enters, gain control of target "
                      "permanent.\nAt the beginning of your end step, if you "
                      "control three or more permanents you don't own, draw "
                      "three cards.")
MARK_OF_MUTINY = ("Gain control of target creature until end of turn. Put a "
                  "+1/+1 counter on it and untap it. That creature gains "
                  "haste until end of turn. (It can attack and {T} this "
                  "turn.)")
PORTENT_OF_BETRAYAL = ("Gain control of target creature until end of turn. "
                       "Untap that creature. It gains haste until end of "
                       "turn. Scry 1. (Look at the top card of your library. "
                       "You may put that card on the bottom.)")
# Angrath, the Flame-Chained's -3 effect text (bulk-verified clause; carries
# the sacrifice-at-end-of-turn rider this generator must never drop).
ANGRATH_MINUS3 = ("Gain control of target creature until end of turn. Untap "
                  "it. It gains haste until end of turn. Sacrifice it at the "
                  "beginning of the next end step if it has mana value 3 or "
                  "less.")
# SYNTHETIC: modeled on the swept attack-trigger shape — no inventory card
# carries exactly this; it pins the event-condition guard in both directions.
SYNTH_ATTACK_TRIGGER = ("Whenever this creature attacks, gain control of "
                        "target creature until end of turn. Untap it. It "
                        "gains haste until end of turn.")
# SYNTHETIC: a granted-ability quote that SURVIVES activated-line stripping
# (no cost-colon head) — pins the quote guard decisively.
SYNTH_QUOTED_GRANT = ('Creatures you control have "When this creature '
                      'enters, gain control of target creature until end of '
                      'turn. Untap it. It gains haste until end of turn."')


@pytest.fixture
def lib():
    return get_effect_library()


def _creature(make_card, name, power="3", toughness="3", **kw):
    return make_card(name, type_line=kw.pop("type_line", "Creature — Bear"),
                     power=power, toughness=toughness, **kw)


def _steal(rules, game, card_name, *, until_eot=True, untap=True, haste=True,
           thief="Rick", victim="Claude", source="Act of Treason"):
    return rules._execute_action_on_state(game, {
        "action": "steal_permanent", "player": thief, "from_player": victim,
        "card": card_name, "until_end_of_turn": until_eot,
        "untap": untap, "gain_haste": haste, "source": source,
    })


# ---------------------------------------------------------------------------
# 1. The action + the end-of-turn revert (real interpreter, real end_turn)
# ---------------------------------------------------------------------------

class TestTempStealAction:
    def test_temp_steal_moves_untaps_hastes_and_marks(self, game, rules, make_card):
        rick, claude = game.players
        bear = _creature(make_card, "Grizzly Bears")
        bear.tapped = True
        bear.summoning_sick = False
        claude.battlefield.append(bear)
        msg = _steal(rules, game, "Grizzly Bears")
        assert "until end of turn" in (msg or "")
        assert bear in rick.battlefield and bear not in claude.battlefield
        assert bear.tapped is False, "the printed untap rider"
        assert 'Haste' in bear.temp_keywords, "the printed haste rider"
        assert bear.temp_control_revert_to == 1
        assert bear.summoning_sick is True, (
            "CR 302.6: not under the thief's control since their turn began "
            "— the haste rider is what makes it attackable")

    def test_end_turn_reverts_control(self, game, make_card):
        ge = GameEngine(None)
        rick, claude = game.players
        bear = _creature(make_card, "Grizzly Bears")
        claude.battlefield.append(bear)
        _steal(ge.rules, game, "Grizzly Bears")
        game.active_player_index = 0
        msgs = ge.end_turn(game)
        assert bear in claude.battlefield and bear not in rick.battlefield
        assert bear.temp_control_revert_to is None
        assert any("reverts to" in m for m in (msgs or [])), msgs
        assert 'Haste' not in bear.temp_keywords, (
            "the haste rider expires in the same cleanup window")

    def test_permanent_steal_never_reverts(self, game, make_card):
        ge = GameEngine(None)
        rick, claude = game.players
        bear = _creature(make_card, "Grizzly Bears")
        claude.battlefield.append(bear)
        # Agent of Treachery shape: no until_end_of_turn flag.
        ge.rules._execute_action_on_state(game, {
            "action": "steal_permanent", "player": "Rick",
            "from_player": "Claude", "card": "Grizzly Bears",
            "source": "Agent of Treachery"})
        assert bear.temp_control_revert_to is None
        game.active_player_index = 0
        ge.end_turn(game)
        assert bear in rick.battlefield, "a permanent steal survives cleanup"

    def test_dead_stolen_creature_stays_dead(self, game, make_card):
        ge = GameEngine(None)
        rick, claude = game.players
        bear = _creature(make_card, "Grizzly Bears")
        bear.owner_index = 1
        claude.battlefield.append(bear)
        _steal(ge.rules, game, "Grizzly Bears")
        ge.rules._execute_action_on_state(game, {
            "action": "destroy", "card": "Grizzly Bears"})
        assert bear not in rick.battlefield and bear not in claude.battlefield
        game.active_player_index = 0
        msgs = ge.end_turn(game)
        assert bear not in rick.battlefield and bear not in claude.battlefield
        assert not any("reverts to" in m and "Grizzly" in m
                       for m in (msgs or [])), (
            "a dead permanent must never be resurrected by the revert")

    def test_revert_declined_when_previous_controller_eliminated(self, game, make_card):
        ge = GameEngine(None)
        rick, claude = game.players
        bear = _creature(make_card, "Grizzly Bears")
        claude.battlefield.append(bear)
        _steal(ge.rules, game, "Grizzly Bears")
        claude.eliminated = True
        game.active_player_index = 0
        ge.end_turn(game)
        assert bear in rick.battlefield, (
            "CR 800.4: control cannot revert to a player who left the game")
        assert bear.temp_control_revert_to is None, "marker still consumed"

    def test_layers_control_effect_is_eot_tagged(self, game, rules, make_card):
        """The Layer-2 control effect a TEMP steal registers must carry
        duration "end_of_turn" so the existing clear_temporary_effects pass
        removes the layers half in the same cleanup window; a PERMANENT
        steal's stays permanent."""
        rick, claude = game.players
        bear = _creature(make_card, "Grizzly Bears")
        claude.battlefield.append(bear)
        _steal(rules, game, "Grizzly Bears")
        eff = [e for e in game.layers_engine.effects
               if getattr(e, 'effect_type', '') == 'change_control'
               and getattr(e, 'source_id', '').startswith(f"steal_{bear.id}")]
        assert eff and eff[-1].duration == "end_of_turn"
        bear2 = _creature(make_card, "Second Bear")
        claude.battlefield.append(bear2)
        rules._execute_action_on_state(game, {
            "action": "steal_permanent", "player": "Rick",
            "from_player": "Claude", "card": "Second Bear",
            "source": "Agent of Treachery"})
        eff2 = [e for e in game.layers_engine.effects
                if getattr(e, 'effect_type', '') == 'change_control'
                and getattr(e, 'source_id', '').startswith(f"steal_{bear2.id}")]
        assert eff2 and eff2[-1].duration == "permanent"

    def test_chained_temp_steal_keeps_first_revert_target(self, game, rules, make_card):
        rick, claude = game.players
        bear = _creature(make_card, "Grizzly Bears")
        claude.battlefield.append(bear)
        _steal(rules, game, "Grizzly Bears")           # Rick steals from Claude
        assert bear.temp_control_revert_to == 1
        _steal(rules, game, "Grizzly Bears",           # Claude steals it back
               thief="Claude", victim="Rick")
        assert bear.temp_control_revert_to == 1, (
            "only-if-None: when both effects end simultaneously in cleanup, "
            "control reverts to the FIRST previous controller")

    def test_pattern_to_interpreter_to_end_turn_end_to_end(self, game, make_card):
        """The whole chain the live path takes: pattern resolution → the
        action interpreter → the cleanup revert. A generator pinned only
        through direct calls is not pinned into production."""
        ge = GameEngine(None)
        rick, claude = game.players
        bear = _creature(make_card, "Grizzly Bears")
        bear.tapped = True
        claude.battlefield.append(bear)
        lib = get_effect_library()
        ctx = build_game_context(game, rick, claude)
        actions, desc = lib.resolve_spell(
            "Act of Treason", ACT_OF_TREASON, "Rick", "Claude",
            game_context=ctx)
        assert actions, desc
        for a in actions:
            ge.rules._execute_action_on_state(game, a)
        assert bear in rick.battlefield and not bear.tapped
        assert 'Haste' in bear.temp_keywords
        game.active_player_index = 0
        ge.end_turn(game)
        assert bear in claude.battlefield, "reverts at cleanup"


# ---------------------------------------------------------------------------
# 2. Serialization + CR 400.7 — the marker must survive save/!undo and must
#    NOT survive a zone change
# ---------------------------------------------------------------------------

class TestControlBookkeepingLifecycle:
    def test_serialization_round_trips_control_fields(self, make_card):
        bear = _creature(make_card, "Grizzly Bears")
        bear.temp_control_revert_to = 1
        bear.original_controller_index = 1
        bear.control_gained_by = "Sower of Temptation"
        back = Card.from_dict(bear.to_dict())
        assert back.temp_control_revert_to == 1, (
            "dropping this makes an Act of Treason steal PERMANENT through "
            "an !undo snapshot")
        assert back.original_controller_index == 1
        assert back.control_gained_by == "Sower of Temptation"

    def test_reset_battlefield_state_clears_control_markers(self, make_card):
        bear = _creature(make_card, "Grizzly Bears")
        bear.temp_control_revert_to = 1
        bear.original_controller_index = 0
        bear.control_gained_by = "Sower of Temptation"
        bear.reset_battlefield_state()
        assert bear.temp_control_revert_to is None
        assert bear.original_controller_index is None
        assert bear.control_gained_by is None


# ---------------------------------------------------------------------------
# 3. The Tier 1.5 pattern — fires for the family, declines everything it
#    cannot deliver, never hijacks permanent steals
# ---------------------------------------------------------------------------

class TestTempControlPattern:
    def _ctx(self, game, extra_creatures=()):
        rick, claude = game.players
        for c in extra_creatures:
            claude.battlefield.append(c)
        return build_game_context(game, rick, claude)

    def test_act_of_treason_resolves_with_riders(self, game, make_card, lib):
        bear = _creature(make_card, "Grizzly Bears")
        ctx = self._ctx(game, [bear])
        actions, desc = lib.resolve_spell(
            "Act of Treason", ACT_OF_TREASON, "Rick", "Claude",
            game_context=ctx)
        assert actions and actions[0]["action"] == "steal_permanent"
        a = actions[0]
        assert a["until_end_of_turn"] is True
        assert a["untap"] is True and a["gain_haste"] is True
        assert a["card"] == "Grizzly Bears"

    def test_threaten_wording_matches(self, game, make_card, lib):
        bear = _creature(make_card, "Grizzly Bears")
        ctx = self._ctx(game, [bear])
        actions, _ = lib.resolve_spell(
            "Threaten", THREATEN, "Rick", "Claude", game_context=ctx)
        assert actions and actions[0]["until_end_of_turn"] is True
        assert actions[0]["untap"] is True

    def test_mass_and_permanent_steals_do_not_match(self, game, make_card, lib):
        bear = _creature(make_card, "Grizzly Bears")
        ctx = self._ctx(game, [bear])
        actions, _ = lib.resolve_spell(
            "Insurrection", INSURRECTION, "Rick", "Claude", game_context=ctx)
        assert actions is None, "mass theft is excluded by the target anchor"
        # SYNTHETIC negative: a permanent steal must not gain a phantom EOT.
        actions, _ = lib.resolve_spell(
            "Persuasion Test", "Gain control of target creature.",
            "Rick", "Claude", game_context=ctx)
        assert not actions or not any(
            a.get("until_end_of_turn") for a in actions)
        # Agent of Treachery resolves via its own registration — whatever it
        # emits must never carry the temp flag.
        actions, _ = lib.resolve_etb(
            "Agent of Treachery", AGENT_OF_TREACHERY, "Rick", "Claude",
            game_context=self._ctx(game))
        assert not actions or not any(
            a.get("until_end_of_turn") for a in actions)

    def test_unmodeled_riders_decline_to_tier3(self, game, make_card, lib):
        bear = _creature(make_card, "Grizzly Bears")
        ctx = self._ctx(game, [bear])
        # Angrath -3: sacrifice-at-end rider — resolving without it lets a
        # creature survive that must die.
        actions, _ = lib.resolve_spell(
            "Angrath Minus Three", ANGRATH_MINUS3, "Rick", "Claude",
            game_context=ctx)
        assert actions is None
        # Kari Zev's Expertise: the free-cast rider is a SEPARATE LINE — the
        # multiline safe-list must decline it (the compound-drop class).
        actions, _ = lib.resolve_spell(
            "Kari Zev's Expertise", KARI_ZEVS_EXPERTISE, "Rick", "Claude",
            game_context=ctx)
        assert actions is None
        # Twisted Fealty is the DECISIVE multiline fixture: its steal line is
        # clean and its target class accepted, so ONLY the sibling-line
        # safe-list stands between it and dropping the Role token. (Kari
        # Zev's is double-covered by the class gate — a mutant deleting the
        # multiline check would survive on it alone.)
        actions, _ = lib.resolve_spell(
            "Twisted Fealty", TWISTED_FEALTY, "Rick", "Claude",
            game_context=ctx)
        assert actions is None

    def test_event_condition_guard_both_directions(self, game, make_card, lib):
        bear = _creature(make_card, "Grizzly Bears")
        ctx = self._ctx(game, [bear])
        # An ETB trigger fires on the etb dispatch...
        actions, _ = lib.resolve_etb(
            "Conquering Manticore", CONQUERING_MANTICORE, "Rick", "Claude",
            game_context=dict(ctx))
        assert actions and actions[0]["until_end_of_turn"] is True
        # ...and not on a dies dispatch.
        actions, _ = lib.resolve_etb(
            "Conquering Manticore", CONQUERING_MANTICORE, "Rick", "Claude",
            game_context=dict(ctx), event_type="dies")
        assert actions is None
        # Bare imperative SPELL text must not fire from a dies/upkeep/attacks
        # dispatch either (a dies scan passing a spell-shaped paragraph).
        actions, _ = lib.resolve_etb(
            "Act of Treason", ACT_OF_TREASON, "Rick", "Claude",
            game_context=dict(ctx), event_type="dies")
        assert actions is None
        # An attack trigger fires on the attacks dispatch...
        actions, _ = lib.resolve_attack_trigger(
            "Synthetic Thief", SYNTH_ATTACK_TRIGGER, "Synthetic Thief", 3,
            "Rick", "Claude", game_context=dict(ctx))
        assert actions and actions[0]["action"] == "steal_permanent"
        # ...and never on an ETB scan (the flicker protection).
        actions, _ = lib.resolve_etb(
            "Synthetic Thief", SYNTH_ATTACK_TRIGGER, "Rick", "Claude",
            game_context=dict(ctx))
        assert actions is None

    def test_quote_guard_declines_granted_text(self, game, make_card, lib):
        bear = _creature(make_card, "Grizzly Bears")
        ctx = self._ctx(game, [bear])
        # Chamber of Manipulation: the granted ability is stripped as an
        # activated line AND quote-guarded — belt and suspenders.
        actions, _ = lib.resolve_etb(
            "Chamber of Manipulation", CHAMBER_OF_MANIPULATION,
            "Rick", "Claude", game_context=dict(ctx))
        assert actions is None
        # The synthetic grant survives activated-line stripping (no cost
        # colon), so ONLY the quote guard stands between it and a free steal.
        actions, _ = lib.resolve_etb(
            "Synthetic Granter", SYNTH_QUOTED_GRANT, "Rick", "Claude",
            game_context=dict(ctx))
        assert actions is None

    def test_qualifier_bounds_enforced(self, game, make_card, lib):
        big = _creature(make_card, "Big Bear", power="5", toughness="5")
        small = _creature(make_card, "Small Bear", power="3", toughness="3")
        ctx = self._ctx(game, [big, small])
        actions, _ = lib.resolve_spell(
            "Wrangle", WRANGLE, "Rick", "Claude", game_context=ctx)
        assert actions and actions[0]["card"] == "Small Bear", (
            "power 4 or less: the 5-power creature is not a legal target")
        # Fresh board for the MV half — the leftover bears default to cmc 0,
        # which would be LEGAL under the MV bound and out-rank Cheap Bear
        # for a reason unrelated to the gate under test.
        game.players[1].battlefield.clear()
        cheap = _creature(make_card, "Cheap Bear", cmc=2,
                          mana_cost="{1}{G}")
        pricey = _creature(make_card, "Pricey Bear", power="6",
                           toughness="6", cmc=5, mana_cost="{4}{G}")
        for c in (cheap, pricey):
            game.players[1].battlefield.append(c)
        ctx2 = build_game_context(game, game.players[0], game.players[1])
        actions, _ = lib.resolve_spell(
            "Claim the Firstborn", CLAIM_THE_FIRSTBORN, "Rick", "Claude",
            game_context=ctx2)
        assert actions and actions[0]["card"] == "Cheap Bear", (
            "mana value 3 or less excludes every bigger creature")

    def test_all_targets_exceed_bound_is_handled_noop(self, game, make_card, lib):
        big = _creature(make_card, "Big Bear", power="6", toughness="6")
        ctx = self._ctx(game, [big])
        actions, _ = lib.resolve_spell(
            "Wrangle", WRANGLE, "Rick", "Claude", game_context=ctx)
        assert actions == [], (
            "no legal target: a handled fizzle (CR 603.3c), never an "
            "unhandled escalation and never an illegal steal")

    def test_declared_target_honored_never_retargeted(self, game, make_card, lib):
        rick, claude = game.players
        big = _creature(make_card, "Big Bear", power="6", toughness="6")
        small = _creature(make_card, "Small Bear", power="2", toughness="2")
        claude.battlefield.extend([big, small])
        ctx = build_game_context(game, rick, claude, explicit_target=small)
        actions, _ = lib.resolve_spell(
            "Act of Treason", ACT_OF_TREASON, "Rick", "Claude",
            game_context=ctx)
        assert actions and actions[0]["card"] == "Small Bear", (
            "a declared target beats the auto-pick heuristic (CR 601.2c)")
        # A declared target that is ILLEGAL for the printed class declines —
        # never silently retargets (the Abrupt Decay precedent).
        swamp = make_card("Swamp", type_line="Basic Land — Swamp",
                          power=None, toughness=None)
        claude.battlefield.append(swamp)
        ctx = build_game_context(game, rick, claude, explicit_target=swamp)
        actions, _ = lib.resolve_spell(
            "Act of Treason", ACT_OF_TREASON, "Rick", "Claude",
            game_context=ctx)
        assert actions is None

    def test_counter_and_scry_riders_emitted(self, game, make_card, lib):
        bear = _creature(make_card, "Grizzly Bears")
        ctx = self._ctx(game, [bear])
        actions, _ = lib.resolve_spell(
            "Mark of Mutiny", MARK_OF_MUTINY, "Rick", "Claude",
            game_context=ctx)
        assert actions and any(a["action"] == "add_counters" and
                               a["counter_type"] == "+1/+1"
                               for a in actions)
        actions, _ = lib.resolve_spell(
            "Portent of Betrayal", PORTENT_OF_BETRAYAL, "Rick", "Claude",
            game_context=ctx)
        assert actions and any(a["action"] == "scry" and a["amount"] == 1
                               for a in actions)


# ---------------------------------------------------------------------------
# 4. The saga back-face table carries PRINTED text (the fabrication class)
# ---------------------------------------------------------------------------

class TestSagaBackFaceTable:
    """Aug 26: the Kami War entry carried a fabricated 'gain control of
    target nonland permanent until end of turn' theft trigger that exists on
    no printing (the May 20 Aminatou hallucinated-text class, in a hardcoded
    table). Re-verified every entry against the bulk; four were wrong. These
    are tombstones against the fabrications returning."""

    def _table(self):
        from mtg.sba import _TRANSFORMING_SAGA_BACK_FACES
        return _TRANSFORMING_SAGA_BACK_FACES

    def test_o_kagachi_carries_printed_trigger_not_theft(self):
        e = self._table()["the kami war"]
        assert "defending player chooses" in e["oracle_text"]
        assert "gain control" not in e["oracle_text"].lower()
        assert e["type_line"] == "Enchantment Creature — Dragon Spirit"

    def test_reflection_of_kiki_jiki_is_printed(self):
        e = self._table()["fable of the mirror-breaker"]
        assert "nonlegendary creature you control, except it has haste" in e["oracle_text"]
        assert "1/1 red Goblin Shaman" not in e["oracle_text"]

    def test_invasion_back_faces_are_printed(self):
        t = self._table()
        zen = t["invasion of zendikar"]
        assert "one mana of any color" in zen["oracle_text"]
        assert zen["type_line"] == "Creature — Elemental"
        assert (zen["power"], zen["toughness"]) == ("4", "4")
        tar = t["invasion of tarkir"]
        assert "Whenever a Dragon you control attacks" in tar["oracle_text"]
        assert "deals 5 damage" not in tar["oracle_text"]
        assert (tar["power"], tar["toughness"]) == ("4", "4")


# ---------------------------------------------------------------------------
# 5. Tier-3 vocabulary — both judge prompt blocks document the flag
# ---------------------------------------------------------------------------

class TestJudgeVocabulary:
    def test_both_blocks_document_until_end_of_turn_steal(self):
        import inspect
        import mtg.judge as judge
        src = inspect.getsource(judge)
        assert src.count('"action": "steal_permanent"') >= 2, (
            "both prompt vocabulary blocks must document the steal action")
        assert src.count("until_end_of_turn") >= 2, (
            "without the flag documented, Tier 3 resolves Act of Treason as "
            "a PERMANENT steal — the exact wrong-resolution this shipped "
            "to prevent")
