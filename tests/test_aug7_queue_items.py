"""Pins for the Aug 7 deferred-queue items (Q1 dedup multiplicity,
Q2 choice threading). Each exercises the REAL production functions with
live-shaped inputs (the pin-shape-reachability rule).
"""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from mtg.models import Card, GameState, Player

_CACHE = json.loads(
    (Path(__file__).resolve().parent.parent / "data" / "card_data_cache.json")
    .read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Q1: byte-identical adjacent dedup edits " _(×N)_" onto the POSTED message
# ---------------------------------------------------------------------------

class _FakeMessage:
    def __init__(self, content):
        self.content = content
        self.edits = []

    async def edit(self, content=None, **_):
        self.content = content
        self.edits.append(content)


class _FakeThread:
    def __init__(self):
        self.id = 4242
        self.sent = []

    async def send(self, content=None, **kwargs):
        msg = _FakeMessage(content)
        self.sent.append(msg)
        return msg


def _fake_cog(game):
    from mtg.cog import MTGGameCog
    cog = SimpleNamespace()
    cog.engine = SimpleNamespace(games={4242: game})
    cog.game_loggers = {}
    cog._autoplay_send = MTGGameCog._autoplay_send.__get__(cog)
    cog._thread_send = MTGGameCog._thread_send.__get__(cog)
    return cog


def _q1_game():
    rick = Player(name="Rick", user_id=99999, life=40)
    claude = Player(name="Claude", user_id=None, is_claude=True, life=40)
    game = GameState(thread_id=4242, format="commander",
                     players=[rick, claude])
    game.turn_number = 3
    return game


class TestDedupEditInPlace:
    def test_identical_sends_edit_multiplicity_onto_the_posted_message(self):
        game = _q1_game()
        cog = _fake_cog(game)
        thread = _FakeThread()

        async def run():
            await cog._autoplay_send(thread, "⚡ Prowess: +1/+1 until end of turn")
            await cog._autoplay_send(thread, "⚡ Prowess: +1/+1 until end of turn")
            await cog._autoplay_send(thread, "⚡ Prowess: +1/+1 until end of turn")
        asyncio.run(run())
        # ONE posted message, edited twice — the old behavior silently
        # dropped the 2nd/3rd and two Swiftspears' prowess read as one.
        assert len(thread.sent) == 1
        assert thread.sent[0].content == (
            "⚡ Prowess: +1/+1 until end of turn _(×3)_")
        assert thread.sent[0].edits == [
            "⚡ Prowess: +1/+1 until end of turn _(×2)_",
            "⚡ Prowess: +1/+1 until end of turn _(×3)_",
        ]

    def test_distinct_sends_post_unchanged(self):
        game = _q1_game()
        cog = _fake_cog(game)
        thread = _FakeThread()

        async def run():
            await cog._autoplay_send(thread, "line one")
            await cog._autoplay_send(thread, "line two")
        asyncio.run(run())
        assert [m.content for m in thread.sent] == ["line one", "line two"]
        assert all(not m.edits for m in thread.sent)

    def test_per_turn_burst_sentinel_still_fires_at_three(self):
        game = _q1_game()
        cog = _fake_cog(game)
        thread = _FakeThread()

        async def run():
            # Interleaved identical content (non-adjacent, so Layer 1 never
            # returns early) — the burst layer's sentinel semantics must be
            # UNCHANGED: 3rd identical-per-turn message emits the sentinel
            # then suppresses.
            await cog._autoplay_send(thread, "🃏 Species Specialist draws")
            await cog._autoplay_send(thread, "⚔️ attack happened")
            await cog._autoplay_send(thread, "🃏 Species Specialist draws")
            await cog._autoplay_send(thread, "💥 damage happened")
            await cog._autoplay_send(thread, "🃏 Species Specialist draws")
        asyncio.run(run())
        contents = [m.content for m in thread.sent]
        assert contents.count("🃏 Species Specialist draws") == 2
        assert any("suppressing further identical fires" in c
                   for c in contents), "the sentinel must still fire"


# ---------------------------------------------------------------------------
# Q2a: destination-typed tutor choices (Jarad's Orders class)
# ---------------------------------------------------------------------------

class TestSplitDestinationTutors:
    def test_each_search_consumes_its_own_destination_choice(self):
        from mtg.spells import inject_tutor_choice
        ctx = {"_tutor_to_hand": "Sakura-Tribe Elder",
               "_tutor_to_graveyard": "Kokusho, the Evening Star"}
        hand_search = {"action": "search_library", "to_zone": "hand"}
        gy_search = {"action": "search_library", "to_zone": "graveyard"}
        inject_tutor_choice(hand_search, ctx)
        inject_tutor_choice(gy_search, ctx)
        assert hand_search["card_name"] == "Sakura-Tribe Elder"
        assert gy_search["card_name"] == "Kokusho, the Evening Star", (
            "the model's choice for the SECOND search used to be discarded")

    def test_generic_choice_consumed_once(self):
        from mtg.spells import inject_tutor_choice
        ctx = {"_tutor_card": "Craterhoof Behemoth"}
        s1 = {"action": "search_library", "to_zone": "hand"}
        s2 = {"action": "search_library", "to_zone": "graveyard"}
        inject_tutor_choice(s1, ctx)
        inject_tutor_choice(s2, ctx)
        assert s1["card_name"] == "Craterhoof Behemoth"
        assert "card_name" not in s2, (
            "the C-5 consume-once contract: one name never feeds two searches")

    def test_template_named_card_wins(self):
        from mtg.spells import inject_tutor_choice
        ctx = {"_tutor_to_hand": "Wrong Card"}
        s = {"action": "search_library", "to_zone": "hand",
             "card_name": "Template Choice"}
        inject_tutor_choice(s, ctx)
        assert s["card_name"] == "Template Choice"

    def test_both_executors_stash_the_typed_keys(self):
        root = Path(__file__).resolve().parent.parent
        for fname in ("mtg/autoplay.py", "mtg/engine.py"):
            src = (root / fname).read_text(encoding="utf-8")
            assert 'tutor_to_hand' in src and 'tutor_to_graveyard' in src, (
                f"{fname} must stash the destination-typed tutor keys — "
                f"the two-executor divergence is the most repeated class")

    def test_prompt_grammar_documents_the_keys(self):
        root = Path(__file__).resolve().parent.parent
        src = (root / "mtg" / "claude_player.py").read_text(encoding="utf-8")
        assert src.count("tutor_to_hand") >= 2, (
            "both ACTION-GRAMMAR blocks must show the keys — the model "
            "cannot emit vocabulary it was never shown")


# ---------------------------------------------------------------------------
# Q2b: Cryptic Command honors the AI's mode choice
# ---------------------------------------------------------------------------

class TestCrypticCommandModes:
    @pytest.fixture(scope="class")
    def lib(self):
        from rules.effect_templates import get_effect_library
        return get_effect_library()

    _ORACLE = ("Choose two —\n• Counter target spell.\n• Return target "
               "permanent to its owner's hand.\n• Tap all creatures your "
               "opponents control.\n• Draw a card.")

    def _resolve(self, lib, ctx):
        actions, _ = lib.resolve_spell(
            card_name="Cryptic Command", oracle_text=self._ORACLE,
            controller="Rick", opponent="Claude", game_context=ctx)
        return [a["action"] for a in actions]

    def test_tap_and_draw_modes_honored(self, lib):
        assert self._resolve(lib, {"_modes": [3, 4]}) == ["tap", "draw_cards"]

    def test_mode_names_accepted(self, lib):
        class _C:
            name = "Serra Angel"
            type_line = "Creature — Angel"
        got = self._resolve(lib, {"_modes": ["bounce", "draw"],
                                  "best_opponent_creature": "Serra Angel",
                                  "opponent_battlefield": [_C()]})
        assert got == ["move_card", "draw_cards"]

    def test_default_stays_counter_plus_draw(self, lib):
        assert self._resolve(lib, {}) == ["counter_spell", "draw_cards"]

    def test_dead_def_is_gone_and_python_registration_lives(self, lib):
        root = Path(__file__).resolve().parent.parent
        src = (root / "rules" / "effect_templates.py").read_text(
            encoding="utf-8")
        assert "def _cryptic_command(" not in src, (
            "the dead unregistered def must not return (the "
            "shadowed-duplicate class)")
        assert "cryptic command" in lib._card_templates


# ---------------------------------------------------------------------------
# Q3: Draugr Necromancer's cast half (cross-player exile + snow-as-any)
# ---------------------------------------------------------------------------

# The REAL printed text (cache-verified — the adversarial review caught the
# first draft quoting a text that exists on no printing).
DRAUGR_ORACLE = (
    "If a nontoken creature an opponent controls would die, exile that card "
    "with an ice counter on it instead.\n"
    "You may cast spells from among cards in exile your opponents own with "
    "ice counters on them, and you may spend mana from snow sources as "
    "though it were mana of any color to cast those spells.")


def _draugr_setup(make_game, make_card, register=True):
    """Claude controls a live Draugr; Rick's Bear died and was redirected."""
    from mtg.rules_engine import RulesEngine
    game = make_game()
    rick, claude = game.players
    rules = RulesEngine(None)
    draugr = make_card("Draugr Necromancer",
                       type_line="Snow Creature — Zombie Cleric",
                       power="4", toughness="4", oracle_text=DRAUGR_ORACLE)
    claude.battlefield.append(draugr)
    if register:
        game.register_replacement_effects(draugr, claude.name)
    bear = make_card("Grizzly Bears", type_line="Creature — Bear",
                     oracle_text="", power="2", toughness="2",
                     mana_cost="{1}{G}", cmc=2)
    rick.battlefield.append(bear)
    bear.damage_marked = 99
    rules.process_state_based_actions(game)
    return game, rick, claude, rules, draugr, bear


class TestDraugrCastHalf:
    def test_redirect_stamps_the_cast_permission(self, make_game, make_card):
        game, rick, claude, rules, draugr, bear = _draugr_setup(
            make_game, make_card)
        assert bear in rick.exile, "the July-30 redirect half"
        assert bear.counters.get("ice", 0) == 1
        assert bear._castable_by_player == claude.name, (
            "the Aug-7 cast half: the redirect must stamp WHO may cast it")
        assert bear._snow_as_any_color is True

    def test_permission_dies_with_the_draugr(self, make_game, make_card):
        from mtg.helpers import is_castable_from_exile
        game, rick, claude, rules, draugr, bear = _draugr_setup(
            make_game, make_card)
        assert is_castable_from_exile(game, claude, bear) is True
        claude.battlefield.remove(draugr)
        assert is_castable_from_exile(game, claude, bear) is False, (
            "the permission is re-checked against the Draugr's presence")
        # And it never belongs to the card's owner:
        claude.battlefield.append(draugr)
        assert is_castable_from_exile(game, rick, bear) is False

    def test_finder_locates_the_card_in_the_opponents_exile(
            self, make_game, make_card):
        from mtg.helpers import find_castable_exile_card
        game, rick, claude, rules, draugr, bear = _draugr_setup(
            make_game, make_card)
        found = find_castable_exile_card(game, claude, "Grizzly Bears")
        assert found is not None
        card, holder = found
        assert card is bear
        assert holder is rick, (
            "the HOLDER is whoever physically has the card — removal and "
            "rollback must touch that exile, never assume the caster's")

    def test_snow_source_pays_any_color_only_for_permitted_cards(
            self, make_game, make_card):
        game, rick, claude, rules, draugr, bear = _draugr_setup(
            make_game, make_card)
        snow_plains = make_card("Snow-Covered Plains",
                                type_line="Basic Snow Land — Plains",
                                oracle_text="({T}: Add {W}.)", power=None,
                                toughness=None, cmc=0)
        snow_plains.power = "0"
        snow_plains.toughness = "0"
        claude.battlefield.append(snow_plains)
        forest = make_card("Forest", type_line="Basic Land — Forest",
                           oracle_text="({T}: Add {G}.)", cmc=0)
        claude.battlefield.append(forest)
        # Bear costs {1}{G}: Forest covers {G}, the SNOW Plains covers {1}.
        # Now the decisive shape — a {G}-pip card the snow Plains could
        # never normally pay: remove the Forest and the bear needs the snow
        # source to BE green.
        claude.battlefield.remove(forest)
        bear.mana_cost = "{G}"
        ok, _ = claude.can_pay_mana_cost("{G}", spending_card=bear)
        assert ok is True, (
            "snow mana spends as any color for the permitted card")
        assert claude.tap_sources_for_cost("{G}", spending_card=bear) is True
        # Control: WITHOUT the permission the same shape must fail.
        snow_plains.tapped = False
        plain_card = make_card("Plain Bear", mana_cost="{G}", cmc=1)
        ok2, _ = claude.can_pay_mana_cost("{G}", spending_card=plain_card)
        assert ok2 is False, (
            "a snow Plains is still a Plains for ordinary casts")

    def test_non_snow_sources_gain_nothing(self, make_game, make_card):
        game, rick, claude, rules, draugr, bear = _draugr_setup(
            make_game, make_card)
        plains = make_card("Plains", type_line="Basic Land — Plains",
                           oracle_text="({T}: Add {W}.)", cmc=0)
        claude.battlefield.append(plains)
        bear.mana_cost = "{G}"
        ok, _ = claude.can_pay_mana_cost("{G}", spending_card=bear)
        assert ok is False, (
            "the waiver is SNOW mana only — a plain Plains stays white")

    def test_offer_appears_with_the_draugr_label(self, make_game, make_card):
        from mtg.legal_actions import castable_entries
        game, rick, claude, rules, draugr, bear = _draugr_setup(
            make_game, make_card)
        snow = make_card("Snow-Covered Island",
                         type_line="Basic Snow Land — Island",
                         oracle_text="({T}: Add {U}.)", cmc=0)
        snow2 = make_card("Snow-Covered Swamp",
                          type_line="Basic Snow Land — Swamp",
                          oracle_text="({T}: Add {B}.)", cmc=0)
        claude.battlefield.extend([snow, snow2])
        entries = castable_entries(game, claude, {"W": 0, "U": 1, "B": 1,
                                                  "R": 0, "G": 0, "C": 0},
                                   0, 2)
        labels = [e.get("label", "") for e in entries]
        assert any("DRAUGR" in l and "Grizzly Bears" in l for l in labels), (
            f"the cross-player offer must appear: {labels}")

    def test_holder_aware_moves_in_both_executors(self):
        root = Path(__file__).resolve().parent.parent
        for fname in ("mtg/autoplay.py", "mtg/engine.py"):
            src = (root / fname).read_text(encoding="utf-8")
            assert "find_castable_exile_card" in src, (
                f"{fname} must use the cross-player finder")
            assert "_exile_holder" in src, (
                f"{fname} must remove/rollback against the HOLDER's exile")


# ---------------------------------------------------------------------------
# Q3 adversarial-review fixes (findings #1-#10 of the a8eab98 review)
# ---------------------------------------------------------------------------

class TestDraugrAdversarialFixes:
    def test_waiver_never_leaks_into_advertising(self, make_game, make_card):
        """#1 (CRITICAL): after a can_pay for a permitted card, advertising
        and tap_lands_for_mana must NOT see snow sources as any-color —
        the review reproduced fabricated {G} off a Snow-Covered Plains."""
        game, rick, claude, rules, draugr, bear = _draugr_setup(
            make_game, make_card)
        snow = make_card("Snow-Covered Plains",
                         type_line="Basic Snow Land — Plains",
                         oracle_text="({T}: Add {W}.)", cmc=0)
        claude.battlefield.append(snow)
        bear.mana_cost = "{G}"
        ok, _ = claude.can_pay_mana_cost("{G}", spending_card=bear)
        assert ok is True  # the waiver works during the payment call
        # ...and is DEAD immediately after via any advertising entry:
        detailed = claude.available_mana_detailed()
        assert detailed.get("any", 0) == 0, (
            "the stale waiver leaked into advertising (review finding #1)")
        assert detailed.get("W", 0) >= 1, "the snow Plains is WHITE again"
        # tap_lands_for_mana never had a setter — the fabricated-mana path:
        claude.can_pay_mana_cost("{G}", spending_card=bear)  # re-arm stale
        claude.tap_lands_for_mana(1, "G")
        assert claude.mana_pool.get("G", 0) == 0, (
            "a snow Plains put {G} into the pool — mana from nothing "
            "(CR 106.1, review finding #1)")

    def test_waiver_requires_the_permission_holder(self, make_game, make_card):
        """#6: the card's OWNER (who did not get the permission) must not
        pay with the opponent's waiver if the card drifts into their hand."""
        game, rick, claude, rules, draugr, bear = _draugr_setup(
            make_game, make_card)
        snow = make_card("Snow-Covered Plains",
                         type_line="Basic Snow Land — Plains",
                         oracle_text="({T}: Add {W}.)", cmc=0)
        rick.battlefield.append(snow)
        bear.mana_cost = "{G}"
        # The stamp names Claude; Rick paying for the same card gets nothing.
        ok, _ = rick.can_pay_mana_cost("{G}", spending_card=bear)
        assert ok is False, (
            "the waiver belongs to the stamped caster only (review #6)")

    def test_cross_player_scan_never_exposes_adventure_or_foretold(
            self, make_game, make_card):
        """#2 (CRITICAL): the controller-blind _adventure_exiled/_foretold
        branches were only safe under own-exile scans."""
        from mtg.helpers import find_castable_exile_card
        game = make_game()
        rick, claude = game.players
        adv = make_card("Beanstalk Giant", type_line="Creature — Giant")
        adv._adventure_exiled = True
        rick.exile.append(adv)
        fore = make_card("Doomskar", type_line="Sorcery")
        fore._foretold = True
        fore._foretold_turn = 0
        rick.exile.append(fore)
        assert find_castable_exile_card(game, claude, "Beanstalk Giant") is None, (
            "Claude cast Rick's exiled adventure card (review #2)")
        assert find_castable_exile_card(game, claude, "Doomskar") is None, (
            "Claude cast Rick's foretold card for its foretell cost (#2)")
        # Rick himself still can:
        assert find_castable_exile_card(game, rick, "Beanstalk Giant") is not None

    def test_two_draugr_mirror_stamps_the_opponent(self, make_game, make_card):
        """#4: in a snow mirror the stamp named the DYING player."""
        from mtg.rules_engine import RulesEngine
        game = make_game()
        rick, claude = game.players
        rules = RulesEngine(None)
        for owner in (rick, claude):
            d = make_card("Draugr Necromancer",
                          type_line="Snow Creature — Zombie Cleric",
                          power="4", toughness="4", oracle_text=DRAUGR_ORACLE)
            owner.battlefield.append(d)
            game.register_replacement_effects(d, owner.name)
        bear = make_card("Grizzly Bears", mana_cost="{1}{G}", cmc=2)
        rick.battlefield.append(bear)
        bear.damage_marked = 99
        rules.process_state_based_actions(game)
        assert bear._castable_by_player == claude.name, (
            "the redirect only applies to an OPPONENT's creature — the "
            "stamp must never name the dying player (review #4)")

    def test_phased_out_draugr_grants_nothing(self, make_game, make_card):
        """#5: CR 702.26 — a phased-out permanent's abilities don't exist."""
        from mtg.helpers import is_castable_from_exile
        game, rick, claude, rules, draugr, bear = _draugr_setup(
            make_game, make_card)
        draugr._phased_out = True
        assert is_castable_from_exile(game, claude, bear) is False

    def test_replacement_draugr_cannot_revive_the_permission(
            self, make_game, make_card):
        """#10: CR 607 — the permission is linked to the stamping object."""
        from mtg.helpers import is_castable_from_exile
        game, rick, claude, rules, draugr, bear = _draugr_setup(
            make_game, make_card)
        claude.battlefield.remove(draugr)
        newd = make_card("Draugr Necromancer",
                         type_line="Snow Creature — Zombie Cleric",
                         power="4", toughness="4", oracle_text=DRAUGR_ORACLE)
        claude.battlefield.append(newd)
        assert is_castable_from_exile(game, claude, bear) is False, (
            "a NEW Draugr revived a lapsed permission (review #10)")

    def test_plan_validator_admits_the_cross_player_card(
            self, make_game, make_card):
        """#3 (MAJOR): without the validator loop the offer appeared and the
        plan dropped the cast as 'not in hand' — decorative feature."""
        from mtg.ai_turn import _validate_plan_mana
        game, rick, claude, rules, draugr, bear = _draugr_setup(
            make_game, make_card)
        snow = make_card("Snow-Covered Island",
                         type_line="Basic Snow Land — Island",
                         oracle_text="({T}: Add {U}.)", cmc=0)
        snow2 = make_card("Snow-Covered Swamp",
                          type_line="Basic Snow Land — Swamp",
                          oracle_text="({T}: Add {B}.)", cmc=0)
        claude.battlefield.extend([snow, snow2])

        class _Engine:
            pass
        plan = [{"type": "cast", "card": "Grizzly Bears"}, {"type": "pass"}]
        validated = _validate_plan_mana(
            _Engine(), game, game.players.index(claude), plan)
        kept = [a for a in validated if a.get("card") == "Grizzly Bears"]
        assert kept, (
            "the plan validator dropped the Draugr cast as 'not in hand' "
            "(review #3 — the feature was decorative on Claude's plan path)")
