"""July 21, 2026 batch audit (game_15291*) — inline-sweep pins.

Pins for the findings the inline audit verified at source level:

1. **events-shadowing UnboundLocalError class** — mtg/engine.py's
   `_execute_action` assigned `events = self.check_state_based_actions(...)`
   in two branches, which made the module-level `from mtg import events`
   unresolvable at the Stoneforge branch's PERMANENT_ENTERED emit
   (UnboundLocalError; 5 games crashed in the July 21 batch). Same class as
   the Apr 6 EventType scoping bug and the July 21 Rancor `import re`.
   Pinned structurally: no function in a module that imports `mtg.events`
   at module level may bind the bare name `events` locally.

2. **Jeska, Thrice Reborn main-cast-path loyalty** — the resolution path's
   predicate ("enters with a number of loyalty counters") matched no
   printing of the card, so the July 20 commander-cast-bonus helper never
   ran on the MAIN cast path (suspend + noncast paths had it) and Jeska
   died to the 0-loyalty SBA even after a commander cast
   (game_1529160614050791549).

3. **Deck-list banned cards** — aura_equipment carried Karakas + Mana Crypt
   (banned in commander); the July 21 identity audit (29a7e35) covered
   color identity but not the banned list. Pinned for every command-zone
   deck JSON in AUTOPLAY_DECKS.

4. **Mana honesty (Bring Back, game_1529165073443197190)** — three cooperating
   bugs: hybrid pips counted as 0 in the adventure/split CMC recompute
   ("needs 0 = 0 total"); the payability advertisement was hybrid-blind
   (hybrid pips classified 'other', checked against nothing) while the
   one-tap gate counted floating pool mana of ANY color; and the pool
   itself held phantom mana because tap-payment production was added to it
   and never deducted — while genuinely-floated mana ([ACTIVATE-MANA],
   rituals) could never actually be spent because the payer ignored the
   pool entirely.
"""
import ast
import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# 1. events-shadowing class pin
# ---------------------------------------------------------------------------

class TestEventsShadowingClass:
    def _local_bindings_of_events(self, tree):
        """Yield (funcname, lineno) for every function that binds the bare
        name `events` locally (assignment, for/with target, comprehension,
        except alias, or parameter)."""
        hits = []

        class _Visitor(ast.NodeVisitor):
            def _check_func(self, node):
                for sub in ast.walk(node):
                    targets = []
                    if isinstance(sub, ast.Assign):
                        targets = sub.targets
                    elif isinstance(sub, (ast.AugAssign, ast.AnnAssign)):
                        targets = [sub.target]
                    elif isinstance(sub, ast.For):
                        targets = [sub.target]
                    elif isinstance(sub, ast.withitem) and sub.optional_vars:
                        targets = [sub.optional_vars]
                    elif isinstance(sub, ast.comprehension):
                        targets = [sub.target]
                    elif isinstance(sub, ast.ExceptHandler) and sub.name == 'events':
                        hits.append((node.name, sub.lineno))
                        continue
                    for t in targets:
                        for n in ast.walk(t):
                            if isinstance(n, ast.Name) and n.id == 'events':
                                hits.append((node.name, n.lineno))
                for arg in getattr(node.args, 'args', []) + getattr(node.args, 'kwonlyargs', []):
                    if arg.arg == 'events':
                        hits.append((node.name, node.lineno))

            def visit_FunctionDef(self, node):
                self._check_func(node)
                self.generic_visit(node)

            def visit_AsyncFunctionDef(self, node):
                self._check_func(node)
                self.generic_visit(node)

        _Visitor().visit(tree)
        return hits

    def test_no_function_shadows_the_events_module(self):
        offenders = []
        for pydir in ('mtg', 'rules'):
            for path in sorted((REPO / pydir).glob('*.py')):
                src = path.read_text(encoding='utf-8')
                if 'from mtg import events' not in src:
                    continue
                tree = ast.parse(src)
                for func, line in self._local_bindings_of_events(tree):
                    offenders.append(f"{path.name}:{line} in {func}()")
        assert not offenders, (
            "Local binding of the name `events` shadows the module-level "
            "`from mtg import events` for the WHOLE function — any earlier "
            "events.emit() line raises UnboundLocalError (5 games crashed "
            "in the July 21 batch). Rename the local: " + "; ".join(offenders))


# ---------------------------------------------------------------------------
# 2. Jeska main-cast-path loyalty
# ---------------------------------------------------------------------------

JESKA_ORACLE = (
    "Jeska enters with a loyalty counter on her for each time you've cast "
    "a commander from the command zone this game.\n"
    "0: Choose target creature. Until your next turn, if that creature would "
    "deal combat damage to one of your opponents, it deals triple that damage "
    "to that player instead."
)


class TestJeskaMainCastLoyalty:
    def _cast(self, engine, game, player, card, **kw):
        import asyncio
        from mtg.spells import cast_spell_async
        return asyncio.run(cast_spell_async(engine, game, player, card, **kw))

    def test_jeska_enters_with_commander_cast_bonus_on_main_path(
            self, make_game, make_card):
        from mtg.constants import Phase
        from mtg.engine import GameEngine
        game = make_game()
        game.phase = Phase.MAIN1
        game.active_player_index = 0
        rick = game.players[0]
        for i in range(3):
            rick.battlefield.append(make_card(
                f"Mountain {i}", type_line="Basic Land — Mountain",
                oracle_text="{T}: Add {R}."))
        commander = make_card("Daretti, Scrap Savant",
                              type_line="Legendary Planeswalker — Daretti")
        commander.is_commander = True
        commander.times_cast_from_command_zone = 1
        commander.loyalty_counters = 3
        rick.battlefield.append(commander)
        jeska = make_card("Jeska, Thrice Reborn", mana_cost="{2}{R}", cmc=3,
                          type_line="Legendary Planeswalker — Jeska",
                          oracle_text=JESKA_ORACLE)
        jeska.loyalty = "0"
        rick.hand.append(jeska)
        ok, msg, _ = self._cast(GameEngine(None), game, rick, jeska)
        assert ok is True, msg
        assert jeska in rick.battlefield
        # Daretti was cast once from the CZ before Jeska → 1 loyalty, not 0
        # (the dead predicate left her at 0 and the SBA killed her).
        assert jeska.loyalty_counters == 1

    def test_dead_predicate_is_gone(self):
        # The old branch keyed on wording that matches NO printing of Jeska.
        src = (REPO / 'mtg' / 'spells.py').read_text(encoding='utf-8')
        assert 'enters with a number of loyalty counters' not in src


# ---------------------------------------------------------------------------
# 3. Deck-list banned cards
# ---------------------------------------------------------------------------

class TestDeckListsBannedCards:
    def test_no_commander_deck_carries_banned_cards(self):
        from mtg.constants import BANNED_CARDS
        banned = {n.lower() for n in BANNED_CARDS.get('commander', [])}
        assert banned, "commander banned list missing from constants"
        from mtg.autoplay import AUTOPLAY_DECKS
        offenders = []
        for short, fname in AUTOPLAY_DECKS.items():
            path = REPO / 'data' / f'{fname}.json'
            if not path.exists():
                continue
            deck = json.loads(path.read_text(encoding='utf-8'))
            fmt = (deck.get('format') or '').lower()
            if fmt not in ('commander', 'edh', 'brawl', 'oathbreaker'):
                continue
            for entry in deck.get('cards', []):
                if entry.get('name', '').lower() in banned:
                    offenders.append(f"{short}: {entry['name']}")
        assert not offenders, (
            "Banned cards in command-zone deck lists (the validator strips "
            "them at load, shrinking the deck): " + "; ".join(offenders))


# ---------------------------------------------------------------------------
# 4. Mana honesty
# ---------------------------------------------------------------------------

def _land(make_card, name, oracle):
    return make_card(name, type_line="Land", oracle_text=oracle)


class TestHybridCmc:
    def test_hybrid_pips_count_toward_mana_value(self):
        from mtg.helpers import cmc_of_cost_string
        assert cmc_of_cost_string('{G/W}{G/W}{G/W}{G/W}') == 4
        assert cmc_of_cost_string('{2}{U}') == 3
        assert cmc_of_cost_string('{X}{G}{G}') == 2      # CR 202.3b
        assert cmc_of_cost_string('{2/W}{2/W}') == 4     # CR 202.3f
        assert cmc_of_cost_string('{W/P}') == 1
        assert cmc_of_cost_string('') == 0


class TestHybridAdvertisementHonesty:
    """The live shape: {G/W}{G/W}{G/W}{G/W} vs 3 G/W-capable duals."""

    def _three_duals(self, make_card, player):
        player.battlefield.append(_land(make_card, "Temple Garden",
                                        "({T}: Add {G} or {W}.)"))
        player.battlefield.append(_land(make_card, "Sunpetal Grove",
                                        "{T}: Add {G} or {W}."))
        player.battlefield.append(_land(make_card, "Hinterland Harbor",
                                        "{T}: Add {G} or {U}."))

    def test_incompatible_floating_mana_is_not_payment_capacity(
            self, make_game, make_card):
        # Floating {U} cannot pay a {G/W} pip: 3 capable sources < 4 pips.
        # Pre-fix this advertised True (one-tap gate counted the U, per-color
        # sums double-counted the duals) and burned AI retries against the
        # tap engine's correct refusal.
        game = make_game()
        rick = game.players[0]
        self._three_duals(make_card, rick)
        rick.mana_pool['U'] = 1
        ok, msg = rick.can_pay_mana_cost('{G/W}{G/W}{G/W}{G/W}')
        assert ok is False
        assert 'Not enough mana' in msg
        assert rick.tap_sources_for_cost('{G/W}{G/W}{G/W}{G/W}') is False

    def test_compatible_floating_mana_actually_pays(self, make_game, make_card):
        # Floating {G} IS the 4th unit — advertisement and payment must both
        # say yes, and the floated mana must be consumed.
        game = make_game()
        rick = game.players[0]
        self._three_duals(make_card, rick)
        rick.mana_pool['G'] = 1
        ok, _ = rick.can_pay_mana_cost('{G/W}{G/W}{G/W}{G/W}')
        assert ok is True
        assert rick.tap_sources_for_cost('{G/W}{G/W}{G/W}{G/W}') is True
        assert rick.mana_pool.get('G', 0) == 0
        assert all(c.tapped for c in rick.battlefield)


class TestPoolHonesty:
    def test_payment_mana_does_not_pollute_the_pool(self, make_game, make_card):
        game = make_game()
        rick = game.players[0]
        rick.battlefield.append(_land(make_card, "Forest", "{T}: Add {G}."))
        assert rick.tap_sources_for_cost('{G}') is True
        assert sum(rick.mana_pool.values()) == 0, (
            "paying {G} with one Forest must leave nothing floating — the "
            "pre-fix pool accumulated everything produced this phase, which "
            "available_mana_detailed() then re-advertised as spendable")

    def test_multi_mana_rock_excess_floats(self, make_game, make_card):
        game = make_game()
        rick = game.players[0]
        sol = make_card("Sol Ring", type_line="Artifact",
                        oracle_text="{T}: Add {C}{C}.")
        rick.battlefield.append(sol)
        assert rick.tap_sources_for_cost('{1}') is True
        # Sol Ring produced 2, the cost consumed 1 — the excess is real
        # floating mana (CR 106.4) and stays available.
        assert sum(rick.mana_pool.values()) == 1

    def test_floated_mana_alone_pays_a_cost_without_tapping(
            self, make_game, make_card):
        game = make_game()
        rick = game.players[0]
        forest = _land(make_card, "Forest", "{T}: Add {G}.")
        rick.battlefield.append(forest)
        rick.mana_pool['G'] = 1
        assert rick.tap_sources_for_cost('{G}') is True
        assert forest.tapped is False, (
            "a floated {G} must be spent before tapping sources")
        assert rick.mana_pool.get('G', 0) == 0
