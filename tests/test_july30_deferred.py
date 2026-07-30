"""July 30, 2026 — the deferred pick-list wave (post-batch-9, maintainer-approved).

Shipped here:
- CR 109.5: the PW forward path type-checks "any target" abilities — an
  unanimated LAND is not a creature/player/planeswalker (Wrenn -1 hit one
  live in batch 15315).
- Draugr Necromancer's death-redirect half (RIP/Leyline family + the ice
  counter rider). The cast-from-exile-with-snow-mana half is UNMODELED
  (noted at the registration).
"""
import pytest

from mtg.models import Card, StackEntry


class TestPwAnyTargetLegal:
    def test_land_is_rejected_for_any_target(self, make_game, make_card):
        from mtg.helpers import pw_any_target_legal
        game = make_game()
        forest = make_card("Forest", type_line="Basic Land — Forest",
                           power="0", toughness="0")
        legal, why = pw_any_target_legal(
            game, "Wrenn and Six deals 1 damage to any target.", forest)
        assert legal is False
        assert "CR 109.5" in why

    def test_creature_player_planeswalker_pass(self, make_game, make_card):
        from mtg.helpers import pw_any_target_legal
        game = make_game()
        text = "Deals 1 damage to any target."
        bear = make_card("Bear")
        jace = make_card("Jace", type_line="Legendary Planeswalker — Jace",
                         power="0", toughness="0")
        assert pw_any_target_legal(game, text, bear)[0] is True
        assert pw_any_target_legal(game, text, jace)[0] is True
        assert pw_any_target_legal(game, text, game.players[1])[0] is True

    def test_non_any_target_abilities_unaffected(self, make_game, make_card):
        from mtg.helpers import pw_any_target_legal
        game = make_game()
        land = make_card("Wastes", type_line="Basic Land",
                         power="0", toughness="0")
        # "Untap two target lands" — its own validation lives elsewhere.
        assert pw_any_target_legal(
            game, "Untap two target lands.", land)[0] is True

    def test_animated_land_is_a_creature_and_passes(self, make_game, make_card):
        from mtg.helpers import pw_any_target_legal
        game = make_game()
        manland = make_card("Celestial Colonnade",
                            type_line="Land Creature — Elemental",
                            power="4", toughness="4")
        assert pw_any_target_legal(
            game, "Deals 1 damage to any target.", manland)[0] is True


DRAUGR_ORACLE = (
    "If a nontoken creature an opponent controls would die, exile that card "
    "with an ice counter on it instead.\n"
    "You may cast spells from among cards in exile your opponents own with "
    "ice counters on them, and you may spend mana from snow sources as "
    "though it were mana of any color to cast those spells.")


class TestDraugrNecromancerRedirect:
    def _register(self, game, controller_name):
        from rules.replacement import scan_oracle_for_replacements
        effects = scan_oracle_for_replacements(
            "draugr_1", "Draugr Necromancer", DRAUGR_ORACLE, controller_name)
        assert effects, "Draugr must register a DEATH replacement"
        for e in effects:
            game.replacement_engine.add_effect(e)  # lazy property wires it
        return effects

    def _kill_by_sba(self, rules, game, creature):
        # Mark lethal damage and run the SBA sweep — the path that consults
        # the replacement engine's DEATH event (the RIP/Leyline family lane).
        creature.damage_marked = 99
        return rules.process_state_based_actions(game)

    def test_opponents_nontoken_creature_exiled_with_ice_counter(
            self, rules, game, make_card):
        rick, claude = game.players
        self._register(game, "Claude")  # Claude controls Draugr
        bear = make_card("Grizzly Bears")
        rick.battlefield.append(bear)

        self._kill_by_sba(rules, game, bear)

        assert bear not in rick.graveyard, (
            "Draugr's controller's OPPONENT's creature must be exiled instead")
        assert bear in rick.exile
        assert bear.counters.get('ice', 0) == 1, (
            "'exile that card with an ice counter on it instead'")

    def test_own_creature_dies_normally(self, rules, game, make_card):
        rick, claude = game.players
        self._register(game, "Claude")
        wall = make_card("Claude Wall")
        claude.battlefield.append(wall)

        self._kill_by_sba(rules, game, wall)

        assert wall in claude.graveyard, (
            "Draugr scopes to OPPONENTS' creatures only")
        assert wall.counters.get('ice', 0) == 0

    def test_token_death_not_redirected(self, rules, game, make_card):
        rick, claude = game.players
        self._register(game, "Claude")
        tok = make_card("Soldier", type_line="Token Creature — Soldier")
        tok.is_token = True
        rick.battlefield.append(tok)

        self._kill_by_sba(rules, game, tok)

        assert tok not in rick.exile, "NONTOKEN creatures only"

    def test_second_half_documented_unmodeled(self):
        from pathlib import Path
        src = (Path(__file__).resolve().parent.parent
               / "rules/replacement.py").read_text(encoding="utf-8")
        assert "UNMODELED" in src.split("draugr necromancer")[0][-900:], (
            "the cast-from-exile half must be noted unmodeled at the "
            "registration site")


RIFT_BOLT = ("Rift Bolt deals 3 damage to any target.\n"
             "Suspend 1—{R} (Rather than cast this card from your hand, "
             "you may pay {R} and exile it with a time counter on it. At the "
             "beginning of your upkeep, remove a time counter. When the last "
             "is removed, you may cast it without paying its mana cost.)")


class TestSuspendInitiation:
    """R2: suspend initiation was structurally unreachable for the
    AI/autoplay — no executor had a "suspend" branch, and the manual
    !suspend command never charged the suspend cost. One shared core now
    serves all three paths."""

    def test_parse_suspend_shapes(self):
        from mtg.helpers import parse_suspend
        assert parse_suspend(RIFT_BOLT) == (1, "{R}")
        assert parse_suspend("Suspend 3—{0}") == (3, "{0}")
        assert parse_suspend("Suspend 4—{1}{U}") == (4, "{1}{U}")
        assert parse_suspend("Flying, haste") is None
        assert parse_suspend("") is None

    def _board(self, make_game, make_card):
        from mtg.engine import GameEngine
        engine = GameEngine(None)
        game = make_game()
        game._rules_engine = engine.rules
        engine.rules.engine_ref = engine
        rick = game.players[0]
        rick.battlefield.append(make_card(
            "Mountain", type_line="Basic Land — Mountain",
            power="0", toughness="0"))
        bolt = make_card("Rift Bolt", type_line="Sorcery", mana_cost="{2}{R}",
                         cmc=3, power="0", toughness="0",
                         oracle_text=RIFT_BOLT)
        rick.hand.append(bolt)
        return engine, game, rick, bolt

    def test_engine_executor_suspends_and_pays(self, make_game, make_card):
        import asyncio
        engine, game, rick, bolt = self._board(make_game, make_card)
        msg = asyncio.run(engine._execute_action(
            game, 0, {"type": "suspend", "card": "Rift Bolt"}))
        assert msg and "suspends" in msg
        assert bolt not in rick.hand
        assert bolt in rick.exile
        assert bolt.suspended is True
        assert bolt.counters.get('time') == 1
        assert all(c.tapped for c in rick.battlefield if c.is_land()), (
            "the {R} suspend cost must actually be paid — the old manual "
            "command charged nothing")

    def test_autoplay_twin_suspends(self, make_game, make_card):
        import asyncio
        from types import SimpleNamespace
        from mtg.autoplay import _autoplay_execute_action
        engine, game, rick, bolt = self._board(make_game, make_card)
        cog = SimpleNamespace(engine=engine)
        msg = asyncio.run(_autoplay_execute_action(
            cog, None, game, 0, {"type": "suspend", "card": "Rift Bolt"}))
        assert msg and "suspends" in msg
        assert bolt in rick.exile and bolt.counters.get('time') == 1

    def test_unpayable_cost_refuses_with_feedback(self, make_game, make_card):
        import asyncio
        from mtg.ai_turn import _get_action_error
        engine, game, rick, bolt = self._board(make_game, make_card)
        rick.battlefield.clear()  # no mana sources
        action = {"type": "suspend", "card": "Rift Bolt"}
        msg = asyncio.run(engine._execute_action(game, 0, action))
        assert msg is None
        assert bolt in rick.hand, "a refused suspend must not move the card"
        err = _get_action_error(engine, game, 0, action)
        assert err and "Can't pay" in err

    def test_ai_surfacing_exists(self):
        from pathlib import Path
        src = (Path(__file__).resolve().parent.parent
               / "mtg/claude_player.py").read_text(encoding="utf-8")
        assert src.count('"type": "suspend"') >= 2, (
            "both action-grammar blocks must document the suspend action")
        assert "SUSPEND available" in src, (
            "the castable section must surface suspendable cards — the "
            "strategist recommended suspending and the actor couldn't")


class TestVisibleState:
    """The per-player serializer foundation (built BEFORE the frontend so
    hidden-info discipline is structural, not a retrofit after the first
    "opponent's hand in the network tab" bug report)."""

    def test_opponent_hand_is_count_only_and_never_leaks(
            self, make_game, make_card):
        import json
        game = make_game()
        rick, claude = game.players
        rick.hand.append(make_card("Secret Plan Xyzzy"))

        state = game.visible_state(1)  # Claude's view

        assert state["players"][0]["hand"] == {"count": 1}
        assert "Secret Plan Xyzzy" not in json.dumps(state), (
            "the opponent's hand contents must be absent from the ENTIRE "
            "serialized payload, not just displayed differently")

    def test_own_hand_is_visible(self, make_game, make_card):
        game = make_game()
        rick = game.players[0]
        rick.hand.append(make_card("My Own Card"))
        state = game.visible_state(0)
        assert any(c.get("name") == "My Own Card"
                   for c in state["players"][0]["hand"])

    def test_libraries_hidden_even_from_their_owner(self, make_game, make_card):
        import json
        game = make_game()
        rick = game.players[0]
        rick.library.append(make_card("Library Topdeck Zzz"))
        state = game.visible_state(0)  # the OWNER's view
        assert state["players"][0]["library_count"] == 1
        assert "Library Topdeck Zzz" not in json.dumps(state), (
            "CR 401.2 — library contents/order are hidden from everyone")

    def test_face_down_exile_masked_for_every_viewer(
            self, make_game, make_card):
        import json
        game = make_game()
        rick = game.players[0]
        hidden = make_card("Gonti Steal Target")
        hidden._face_down = True
        shown = make_card("Path to Exile", type_line="Instant")
        rick.exile.extend([hidden, shown])

        for viewer in (0, 1):
            state = game.visible_state(viewer)
            dump = json.dumps(state)
            assert "Gonti Steal Target" not in dump
            assert "Path to Exile" in dump, "regular exile is public"

    def test_payload_is_json_serializable(self, make_game, make_card):
        import json
        game = make_game()
        game.players[0].battlefield.append(make_card("Bear"))
        game.players[0].commander_damage[1] = 5
        json.dumps(game.visible_state(0))  # must not raise

    def test_move_card_sets_and_clears_face_down(self, rules, game, make_card):
        from mtg.actions import execute_action_on_state
        rick = game.players[0]
        card = make_card("Necro Stash", type_line="Instant")
        rick.hand.append(card)

        execute_action_on_state(rules, game, {
            "action": "move_card", "card": "Necro Stash",
            "from_zone": "hand", "to_zone": "exile", "player": "Rick",
            "hide_card_name": True})
        assert card._face_down is True

        execute_action_on_state(rules, game, {
            "action": "move_card", "card": "Necro Stash",
            "from_zone": "exile", "to_zone": "hand", "player": "Rick"})
        assert card._face_down is False, (
            "returning to a visible zone must clear the mask")


WAKE_ORACLE = ("Creatures you control get +1/+1.\n"
               "Whenever you tap a land for mana, add one mana of any type "
               "that land produced.")
RESURGENT_ORACLE = ("Whenever you tap a land for mana, add one mana of any "
                    "type that land produced.\n"
                    "Whenever you cast a creature spell, draw a card.")


class TestMirarisWakeManaBonus:
    """#12: only the anthem half of Mirari's Wake worked — the tap bonus was
    unmodeled. Scope (the July 21/26 payment-engine lessons): the bonus
    FLOATS into the pool as pure excess (the settle can't consume it toward
    the cost), availability advertisement deliberately does NOT count it,
    and Mana Reflection's replacement wording stays unmodeled."""

    def _forests(self, make_card, n):
        return [make_card(f"Forest {i}", type_line="Basic Land — Forest",
                          power="0", toughness="0") for i in range(n)]

    def test_bonus_floats_on_payment_tap(self, make_game, make_card):
        game = make_game()
        rick = game.players[0]
        wake = make_card("Mirari's Wake", type_line="Enchantment",
                         oracle_text=WAKE_ORACLE, power="0", toughness="0")
        rick.battlefield.append(wake)
        rick.battlefield.extend(self._forests(make_card, 2))

        assert rick.tap_sources_for_cost("{G}", game=game) is True

        assert rick.mana_pool.get('G', 0) == 1, (
            "one land tapped for the cost -> one bonus {G} floats")

    def test_no_wake_no_bonus(self, make_game, make_card):
        game = make_game()
        rick = game.players[0]
        rick.battlefield.extend(self._forests(make_card, 2))
        rick.tap_sources_for_cost("{G}", game=game)
        assert rick.mana_pool.get('G', 0) == 0

    def test_nonland_sources_do_not_trigger(self, make_game, make_card, capsys):
        game = make_game()
        rick = game.players[0]
        wake = make_card("Mirari's Wake", type_line="Enchantment",
                         oracle_text=WAKE_ORACLE, power="0", toughness="0")
        sol = make_card("Sol Ring", type_line="Artifact",
                        oracle_text="{T}: Add {C}{C}.",
                        power="0", toughness="0")
        rick.battlefield.extend([wake, sol])
        rick.tap_sources_for_cost("{1}", game=game)
        assert "[MANA-BONUS]" not in capsys.readouterr().out, (
            "'you tap a LAND for mana' — Sol Ring must not trigger the Wake")

    def test_two_watchers_stack(self, make_game, make_card):
        game = make_game()
        rick = game.players[0]
        rick.battlefield.append(make_card(
            "Mirari's Wake", type_line="Enchantment",
            oracle_text=WAKE_ORACLE, power="0", toughness="0"))
        rick.battlefield.append(make_card(
            "Zendikar Resurgent", type_line="Enchantment",
            oracle_text=RESURGENT_ORACLE, power="0", toughness="0"))
        rick.battlefield.extend(self._forests(make_card, 1))
        rick.tap_sources_for_cost("{G}", game=game)
        assert rick.mana_pool.get('G', 0) == 2

    def test_bonus_is_spendable_on_the_next_cast(self, make_game, make_card):
        # The end-to-end payoff: with a Wake out, one Forest effectively
        # pays for TWO {G} costs this phase — the second via the July 21
        # Phase-0 floating-pool spend.
        game = make_game()
        rick = game.players[0]
        rick.battlefield.append(make_card(
            "Mirari's Wake", type_line="Enchantment",
            oracle_text=WAKE_ORACLE, power="0", toughness="0"))
        rick.battlefield.extend(self._forests(make_card, 1))

        assert rick.tap_sources_for_cost("{G}", game=game) is True
        assert rick.tap_sources_for_cost("{G}", game=game) is True, (
            "the floating bonus {G} must pay the second cost with zero "
            "untapped lands")


class TestCoverageCacheBackfill:
    """#9: deck JSONs carry name+quantity only, so `!coverage <deckname>`
    built Cards with EMPTY oracle text — and empty oracle reads as vanilla,
    so every card classified "no_resolution" (the opposite failure from the
    ~57% tier3 overstatement). supported_at_tier now backfills from the
    Scryfall disk cache; truly-uncached names are "unknown", not vanilla."""

    def test_cached_card_with_no_oracle_is_not_vanilla(self):
        from mtg.coverage import supported_at_tier
        # Tale's End is in the disk cache (stifle deck) — with no oracle
        # supplied it must classify via its REAL text, not as vanilla.
        tier = supported_at_tier("Tale's End")
        assert tier not in ("no_resolution", "unknown"), tier

    def test_uncached_name_is_unknown_not_vanilla(self):
        from mtg.coverage import supported_at_tier
        assert supported_at_tier("Totally Made Up Card Xyzzy") == "unknown"

    def test_basic_land_stays_no_resolution(self):
        from mtg.coverage import supported_at_tier
        assert supported_at_tier("Mountain") == "no_resolution"

    def test_caller_supplied_oracle_still_wins(self):
        from mtg.coverage import supported_at_tier
        # A caller with real text bypasses the backfill entirely.
        tier = supported_at_tier(
            "Some Custom Bear", oracle_text="",
            type_line="Creature — Bear")
        assert tier == "no_resolution"  # type_line given, empty oracle = vanilla

    def test_coverage_command_probes_the_bridge(self):
        from pathlib import Path
        src = (Path(__file__).resolve().parent.parent
               / "mtg/cog.py").read_text(encoding="utf-8")
        i = src.find("classify_deck(cards)")
        assert i > 0
        window = src[i:i + 1500]
        assert "xmage_bridge" in window and "bridge.lookup" in window, (
            "!coverage must run the Tier 2.5 probe pass — the module "
            "supported a probe but no caller ever supplied one")
