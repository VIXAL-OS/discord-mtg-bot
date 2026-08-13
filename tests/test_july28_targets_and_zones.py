"""July 28, 2026 — Phase 2, the "wrong target / wrong zone / wrong player" cluster
plus the two Modern-cluster targeting fixes.

Every finding here was re-verified against the code and data/card_data_cache.json
before patching; one reviewer claim (B1a, "_gen_reanimate ignores the declared
target") was a FALSE-POSITIVE and is pinned as such at the bottom, because a
documented false positive is worth as much as a fix.

D1  TargetTextParser had no "target player or planeswalker" branch, and the
    plain "player" branch below it explicitly excludes any phrase containing
    "planeswalker" — so the phrase fell through to the planeswalker-ONLY branch
    and dropped PLAYER. With no planeswalker on the battlefield the cast gates
    then hard-rejected completely legal burn: 31 blocked casts across the batch,
    and in all six games where Skullcrack was blocked it was never cast at all.

D2  Thoughtseize's JSON template emits `"card": "best_nonland"`, a sentinel the
    discard handler had no branch for. It fell to the name lookup, found no card
    called "best_nonland", and skipped the discard — while the caster still paid
    2 life. Same shape as the June 11 "worst" bug, missed by the July 20 JSON
    migration. Note the polarity is the OPPOSITE of "worst": the caster picks,
    so Thoughtseize takes the opponent's BEST nonland.

B2  A destroyed permanent went to its CONTROLLER's graveyard (CR 404.3), so a
    stolen creature permanently changed owner. The commander branch beside it
    already resolved ownership correctly; the ordinary branch did not.

    Fixing this exposed a trap worth recording: Card.owner_index defaulted to 0,
    which is indistinguishable from "genuinely owned by player 0". Trusting it
    blindly would have sent every UNSTAMPED permanent on player 1's battlefield
    to player 0's graveyard — a brand-new way for cards to change hands, i.e.
    the same bug pointed the other way. The default is now -1 ("unknown"), and
    unknown means "owned by whoever controls it", which is exactly the old
    behaviour. Four existing tests caught this; they are the reason the sentinel
    exists.

B3  The Bloodghast-family graveyard-landfall return never called
    reset_battlefield_state, so damage_marked survived the trip and a 2/1 that
    died to 1 damage returned pre-killed and died again to the next SBA.

B4  The day/night check read the INCOMING active player's own previous turn —
    a turn older still in a two-player game. Daybound's printed reminder,
    quoted from the cache, is "If a player casts no spells during their own
    turn, it becomes night next turn", so the check must read the turn that
    just ended. Independently, only the active player's per-turn spell counter
    was reset, so instants cast during an opponent's turn stayed on the books.

B5/B1c  The two cast-target resolvers had diverged: the autoplay one never
    searched graveyards (so every graveyard-targeting spell Rick cast lost its
    declared target), and NEITHER understood the pronoun "opponent" that the AI
    actually emits for burn — it resolved to None, and get_legal_targets lists
    creatures before players for "any target", so Lightning Bolt aimed at the
    face killed a creature instead. Now one shared resolver.

BONUS  Found while verifying B5: ctx['explicit_target_is_creature'] is READ by
    the Rift Bolt and Volcanic Geyser templates but was written nowhere outside
    tests, so both guards were permanently False and both spells always went to
    the face. The July 23 fix that added those guards had therefore never
    executed in a live game — its pin passed only because the test hand-injected
    the key. That is the mutation-testing lesson in the wild: a test can pass
    for a reason the fix does not provide.
"""
import json
from pathlib import Path

import pytest


_CACHE = Path(__file__).resolve().parent.parent / "data" / "card_data_cache.json"


def _oracle(card_key):
    if not _CACHE.exists():
        pytest.skip("card_data_cache.json not present")
    with open(_CACHE, encoding="utf-8") as fh:
        data = json.load(fh)
    entry = data.get(card_key)
    if entry is None:
        pytest.skip(f"{card_key!r} not in the card cache")
    return entry.get("oracle_text") or ""


# ---------------------------------------------------------------------------
# D1 — "target player or planeswalker"
# ---------------------------------------------------------------------------

class TestPlayerOrPlaneswalkerTargeting:

    def _parse(self, text):
        from rules.targeting import TargetTextParser, TargetType
        return TargetTextParser().parse(text), TargetType

    def test_player_is_not_dropped(self):
        restriction, TargetType = self._parse(
            "Skullcrack deals 3 damage to target player or planeswalker.")
        assert TargetType.PLAYER in restriction.target_types, (
            "dropping PLAYER makes the spell uncastable with no planeswalker out")
        assert TargetType.PLANESWALKER in restriction.target_types

    def test_reversed_wording_too(self):
        restriction, TargetType = self._parse(
            "deals 3 damage to target planeswalker or player")
        assert {TargetType.PLAYER, TargetType.PLANESWALKER} <= restriction.target_types

    @pytest.mark.parametrize("key", ["skullcrack", "lava spike"])
    def test_the_real_printed_cards(self, key):
        restriction, TargetType = self._parse(_oracle(key))
        assert TargetType.PLAYER in restriction.target_types

    def test_plain_player_and_plain_planeswalker_still_work(self):
        r1, TargetType = self._parse("target player draws a card")
        assert r1.target_types == {TargetType.PLAYER}
        r2, _ = self._parse("destroy target planeswalker")
        assert r2.target_types == {TargetType.PLANESWALKER}

    def test_creature_or_player_is_unaffected(self):
        r, TargetType = self._parse("deals 3 damage to target creature or player")
        assert r.target_types == {TargetType.CREATURE, TargetType.PLAYER}


# ---------------------------------------------------------------------------
# D2 — Thoughtseize's best_nonland sentinel
# ---------------------------------------------------------------------------

class TestBestNonlandDiscard:

    def test_sentinel_discards_the_best_nonland(self, game, rules, make_card):
        claude = game.players[1]
        claude.hand = [
            make_card("Swamp", type_line="Basic Land — Swamp", cmc=0),
            make_card("Lightning Bolt", type_line="Instant", cmc=1),
            make_card("Tarmogoyf", type_line="Creature — Lhurgoyf", cmc=2),
        ]
        rules._execute_action_on_state(
            game, {"action": "discard", "player": "Claude", "card": "best_nonland"})
        assert len(claude.hand) == 2, "the discard must actually happen"
        assert [c.name for c in claude.graveyard] == ["Tarmogoyf"]

    def test_never_takes_a_land(self, game, rules, make_card):
        claude = game.players[1]
        claude.hand = [
            make_card("Swamp", type_line="Basic Land — Swamp", cmc=0),
            make_card("Thoughtseize", type_line="Sorcery", cmc=1),
        ]
        rules._execute_action_on_state(
            game, {"action": "discard", "player": "Claude", "card": "best_nonland"})
        assert [c.name for c in claude.graveyard] == ["Thoughtseize"]

    def test_all_lands_hand_discards_nothing(self, game, rules, make_card):
        claude = game.players[1]
        claude.hand = [make_card("Swamp", type_line="Basic Land — Swamp", cmc=0)]
        rules._execute_action_on_state(
            game, {"action": "discard", "player": "Claude", "card": "best_nonland"})
        assert len(claude.hand) == 1
        assert claude.graveyard == []

    def test_end_to_end_through_the_json_template(self, game, rules, lib, make_card):
        """The template and the handler must agree — that is where this broke."""
        rick, claude = game.players
        claude.hand = [make_card("Tarmogoyf", type_line="Creature — Lhurgoyf", cmc=2)]
        actions, _desc = lib.resolve_spell(
            "Thoughtseize", _oracle("thoughtseize"), rick.name, claude.name,
            game_context={})
        assert actions, "Thoughtseize must resolve at Tier 1.5"
        for a in actions:
            rules._execute_action_on_state(game, a)
        assert [c.name for c in claude.graveyard] == ["Tarmogoyf"]
        assert rick.life == 38, "the 2 life is paid either way"


# ---------------------------------------------------------------------------
# B2 — ownership
# ---------------------------------------------------------------------------

class TestDestroyGoesToOwnersGraveyard:

    def test_stolen_creature_returns_to_its_owner(self, game, rules, make_card):
        rick, claude = game.players
        titan = make_card("Sun Titan", power="6", toughness="6")
        titan.owner_index = 0                 # Rick owns it
        claude.battlefield.append(titan)      # Claude has stolen it
        rules._execute_action_on_state(game, {"action": "destroy", "card": "Sun Titan"})
        assert [c.name for c in rick.graveyard] == ["Sun Titan"]
        assert claude.graveyard == [], "the thief must not keep the card"

    def test_unstolen_permanent_is_unaffected(self, game, rules, make_card):
        claude = game.players[1]
        bear = make_card("Runeclaw Bear")
        bear.owner_index = 1
        claude.battlefield.append(bear)
        rules._execute_action_on_state(game, {"action": "destroy", "card": "Runeclaw Bear"})
        assert bear in claude.graveyard

    def test_unstamped_card_stays_with_its_controller(self, game, rules, make_card):
        """The trap: owner_index used to default to 0, which is indistinguishable
        from "player 0 owns this". A card built at runtime must NOT be treated as
        player 0's and teleported across the table."""
        claude = game.players[1]
        bear = make_card("Runeclaw Bear")
        assert bear.owner_index == -1, "unknown ownership must be its own value"
        claude.battlefield.append(bear)
        rules._execute_action_on_state(game, {"action": "destroy", "card": "Runeclaw Bear"})
        assert bear in claude.graveyard
        assert game.players[0].graveyard == []

    def test_owner_of_helper_semantics(self, game, make_card):
        from mtg.helpers import owner_of, owns_card
        rick, claude = game.players
        stamped = make_card("Sun Titan")
        stamped.owner_index = 0
        assert owner_of(game, stamped, claude) is rick
        unknown = make_card("Runeclaw Bear")
        assert owner_of(game, unknown, claude) is claude
        assert owns_card(unknown, 1) and owns_card(unknown, 0), (
            "unknown ownership must not fail an ownership gate either way")
        assert owns_card(stamped, 0) and not owns_card(stamped, 1)


# ---------------------------------------------------------------------------
# B3 — Bloodghast
# ---------------------------------------------------------------------------

class TestGraveyardLandfallReturnIsANewObject:

    def test_damage_does_not_survive_the_graveyard(self, game, rules, make_card):
        from mtg.triggers import _handle_land_etb
        rick = game.players[0]
        ghast = make_card(
            "Bloodghast", power="2", toughness="1",
            oracle_text=("Bloodghast can't block.\nLandfall — Whenever a land you "
                         "control enters, you may return this card from your "
                         "graveyard to the battlefield."))
        ghast.damage_marked = 1          # died to a Bolt earlier
        rick.graveyard.append(ghast)
        land = make_card("Mountain", type_line="Basic Land — Mountain")
        rick.battlefield.append(land)
        _handle_land_etb(rules, game, rick, land)
        assert ghast in rick.battlefield
        assert ghast.damage_marked == 0, (
            "a card returning from the graveyard is a new object (CR 400.7) — "
            "leftover damage re-killed it on the very next SBA")

    def test_stale_counters_and_combat_state_are_cleared(self, game, rules, make_card):
        from mtg.triggers import _handle_land_etb
        rick = game.players[0]
        ghast = make_card("Bloodghast", power="2", toughness="1",
                          oracle_text=("Landfall — Whenever a land you control enters, "
                                       "you may return this card from your graveyard "
                                       "to the battlefield."))
        ghast.counters["+1/+1"] = 3
        ghast.attacking = True
        rick.graveyard.append(ghast)
        land = make_card("Mountain", type_line="Basic Land — Mountain")
        rick.battlefield.append(land)
        _handle_land_etb(rules, game, rick, land)
        assert ghast.counters.get("+1/+1", 0) == 0
        assert not ghast.attacking


# ---------------------------------------------------------------------------
# B4 — day/night
# ---------------------------------------------------------------------------

class TestDayNightReadsTheTurnThatEnded:

    def test_reminder_text_says_their_own_turn(self):
        """Pin the rule we implemented against the printed card, not memory.

        This is the sentence the fix turns on: the condition is about the turn
        a player just took, so the check at the next upkeep must look one turn
        back — not at the incoming active player's own previous turn.
        (Tovolar, Dire Overlord prints a bare "Daybound" with no reminder, so
        pin against a printing that carries the full text.)
        """
        text = _oracle("tovolar's huntmaster").lower()
        assert "casts no spells during their own turn" in text
        assert "night next turn" in text

    def test_flip_uses_the_ending_players_count(self, game, rules, make_card):
        from mtg.triggers import _check_day_night_and_werewolf_transforms
        rick, claude = game.players
        game.day_night_active = True
        game.is_day = True
        # Rick just finished a turn in which he cast 3 spells.
        game._spells_cast_last_turn = 3
        # Claude, the incoming active player, has not had a turn yet.
        claude.spells_cast_prev_turn = 0
        game.active_player_index = 1
        _check_day_night_and_werewolf_transforms(rules, game)
        assert game.is_day is True, (
            "spells WERE cast on the turn that just ended — it must stay day")

    def test_silent_turn_still_brings_night(self, game, rules):
        from mtg.triggers import _check_day_night_and_werewolf_transforms
        game.day_night_active = True
        game.is_day = True
        game._spells_cast_last_turn = 0
        game.active_player_index = 1
        _check_day_night_and_werewolf_transforms(rules, game)
        assert game.is_day is False

    def test_two_spells_brings_day_back(self, game, rules):
        from mtg.triggers import _check_day_night_and_werewolf_transforms
        game.day_night_active = True
        game.is_day = False
        game._spells_cast_last_turn = 2
        _check_day_night_and_werewolf_transforms(rules, game)
        assert game.is_day is True


# ---------------------------------------------------------------------------
# B5 / B1c — one shared cast-target resolver
# ---------------------------------------------------------------------------

class TestSharedCastTargetResolver:

    def test_opponent_pronoun_resolves_to_the_player(self, game, make_card):
        from mtg.helpers import resolve_cast_target
        rick, claude = game.players
        bolt = make_card("Lightning Bolt", type_line="Instant",
                         oracle_text="Lightning Bolt deals 3 damage to any target.")
        claude.battlefield.append(make_card("Goblin Guide"))
        assert resolve_cast_target(game, rick, bolt, "opponent") is claude, (
            "'opponent' resolving to None is why burn hit a creature instead of "
            "the face")

    def test_self_pronoun_resolves_to_the_caster(self, game, make_card):
        from mtg.helpers import resolve_cast_target
        rick = game.players[0]
        card = make_card("Lightning Helix", type_line="Instant",
                         oracle_text="Lightning Helix deals 3 damage to any target.")
        assert resolve_cast_target(game, rick, card, "you") is rick

    def test_graveyard_targets_resolve_for_graveyard_spells(self, game, make_card):
        from mtg.helpers import resolve_cast_target
        rick, claude = game.players
        gonti = make_card("Gonti, Lord of Luxury")
        rick.graveyard.append(gonti)
        claude.graveyard.append(make_card("Sun Titan"))
        animate = make_card("Animate Dead", type_line="Enchantment — Aura",
                            oracle_text="Enchant creature card in a graveyard")
        assert resolve_cast_target(game, rick, animate, "Gonti, Lord of Luxury") is gonti

    def test_non_graveyard_spell_does_not_reach_into_graveyards(self, game, make_card):
        from mtg.helpers import resolve_cast_target
        rick = game.players[0]
        rick.graveyard.append(make_card("Sun Titan"))
        bolt = make_card("Lightning Bolt", type_line="Instant",
                         oracle_text="Lightning Bolt deals 3 damage to any target.")
        assert resolve_cast_target(game, rick, bolt, "Sun Titan") is None

    def test_battlefield_beats_the_pronoun_path(self, game, make_card):
        from mtg.helpers import resolve_cast_target
        rick, claude = game.players
        goyf = make_card("Tarmogoyf")
        claude.battlefield.append(goyf)
        bolt = make_card("Lightning Bolt", type_line="Instant",
                         oracle_text="deals 3 damage to any target")
        assert resolve_cast_target(game, rick, bolt, "Tarmogoyf") is goyf

    def test_a_card_name_starting_with_a_player_name_is_not_that_player(self, game, make_card):
        """Deliberately narrower than _resolve_player_or_card_target: its fuzzy
        substring match would turn an unresolvable card name into a Player."""
        from mtg.helpers import resolve_cast_target
        rick = game.players[0]
        card = make_card("Bolt", type_line="Instant", oracle_text="deals damage")
        assert resolve_cast_target(game, rick, card, "Rick's Signet") is None

    def test_both_cast_paths_use_the_shared_resolver(self):
        """The two copies diverged once; a structural pin keeps them together."""
        import inspect
        from mtg import autoplay, engine
        for mod in (autoplay, engine):
            src = inspect.getsource(mod)
            assert "resolve_cast_target" in src, (
                f"{mod.__name__} must resolve cast targets through the shared helper")


# ---------------------------------------------------------------------------
# BONUS — explicit_target_is_creature
# ---------------------------------------------------------------------------

class TestExplicitTargetIsCreatureIsActuallySet:

    def test_context_marks_a_declared_creature_target(self, game, make_card):
        from rules.effect_templates import build_game_context
        rick, claude = game.players
        savra = make_card("Savra, Queen of the Golgari")
        claude.battlefield.append(savra)
        ctx = build_game_context(game, rick, claude, explicit_target=savra)
        assert ctx.get("explicit_target_is_creature") is True, (
            "the July 23 burn-retarget fix reads this key; nothing ever wrote it")

    def test_a_declared_player_target_is_not_a_creature(self, game, make_card):
        from rules.effect_templates import build_game_context
        rick, claude = game.players
        ctx = build_game_context(game, rick, claude, explicit_target="Claude")
        assert not ctx.get("explicit_target_is_creature")

    def test_burn_honors_the_declared_creature(self, game, lib, make_card):
        from rules.effect_templates import build_game_context
        rick, claude = game.players
        savra = make_card("Savra, Queen of the Golgari")
        claude.battlefield.append(savra)
        ctx = build_game_context(game, rick, claude, explicit_target=savra)
        ctx["x_value"] = 2
        actions, _desc = lib.resolve_spell(
            "Volcanic Geyser",
            "Volcanic Geyser deals X damage to any target.",
            rick.name, claude.name, game_context=ctx)
        assert actions
        dmg = [a for a in actions if a.get("action") == "deal_damage"]
        assert dmg and dmg[0].get("target_card") == "Savra, Queen of the Golgari", (
            f"expected the declared creature, got {dmg}")


# ---------------------------------------------------------------------------
# B1a — the FALSE POSITIVE, pinned so nobody "fixes" it again
# ---------------------------------------------------------------------------

class TestReanimateAlreadyHonorsItsTarget:
    """Reported as "_gen_reanimate ignores explicit_target_name and always takes
    the highest-CMC creature in any graveyard". True of that FUNCTION — and the
    function is dead code: `_add_card` is a plain dict assignment and a later
    registration for the same key overwrites it. The generator that actually
    runs already honors the declared target and charges that card's real mana
    value. Any graveyard is also the correct scope ("from a graveyard")."""

    def test_declared_target_is_honored_and_life_matches_it(self, game, lib, make_card):
        from rules.effect_templates import build_game_context
        rick, claude = game.players
        birds = make_card("Birds of Paradise", cmc=1, type_line="Creature — Bird")
        titan = make_card("Sun Titan", cmc=6, type_line="Creature — Giant")
        rick.graveyard.append(birds)
        claude.graveyard.append(titan)
        ctx = build_game_context(game, rick, claude, explicit_target=birds)
        actions, _desc = lib.resolve_spell(
            "Reanimate",
            ("Put target creature card from a graveyard onto the battlefield "
             "under your control. You lose life equal to its mana value."),
            rick.name, claude.name, game_context=ctx)
        reanimates = [a for a in actions if a.get("action") == "reanimate"]
        assert reanimates and reanimates[0]["card"] == "Birds of Paradise"
        losses = [a for a in actions if a.get("action") == "lose_life"]
        assert losses and losses[0]["amount"] == 1, (
            "the life paid is the tell — 6 would mean it grabbed Sun Titan")


class TestAnimateDeadHonorsItsTarget:
    """B1b — unlike Reanimate, THIS template really did drop the declared target."""

    def test_declared_target_wins(self, game, lib, make_card):
        from rules.effect_templates import build_game_context
        rick, claude = game.players
        birds = make_card("Birds of Paradise", cmc=1, type_line="Creature — Bird")
        titan = make_card("Sun Titan", cmc=6, type_line="Creature — Giant")
        rick.graveyard.append(birds)
        claude.graveyard.append(titan)
        ctx = build_game_context(game, rick, claude, explicit_target=birds)
        actions, _desc = lib.resolve_etb(
            "Animate Dead", "Enchant creature card in a graveyard",
            rick.name, claude.name, game_context=ctx)
        reanimates = [a for a in actions if a.get("action") == "reanimate"]
        assert reanimates and reanimates[0]["card"] == "Birds of Paradise"

    def test_falls_back_when_nothing_was_declared(self, game, lib, make_card):
        from rules.effect_templates import build_game_context
        rick, claude = game.players
        rick.graveyard.append(make_card("Sun Titan", cmc=6, type_line="Creature — Giant"))
        ctx = build_game_context(game, rick, claude)
        actions, _desc = lib.resolve_etb(
            "Animate Dead", "Enchant creature card in a graveyard",
            rick.name, claude.name, game_context=ctx)
        assert [a for a in actions if a.get("action") == "reanimate"]
