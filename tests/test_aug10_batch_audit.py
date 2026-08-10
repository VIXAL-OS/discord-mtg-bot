"""Pins for the Aug 10, 2026 fourth-confirmation-cycle batch audit.

Corpus: 160 games at `strict=1 sha=6a30802` (launcher
autostart_20260809_091830). Each test names the game its finding came from.

Fixture discipline (the standing pin-shape rule): build inputs the way the
LIVE path builds them — real Card objects loaded from the Scryfall disk
cache, real executor entry points, verbatim cached oracle text — never a
hand-written approximation of the shape. Two prior cycles shipped pins that
passed because their fixture was a shape production never sends.
"""
import json
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from conftest import _make_game  # noqa: E402
from mtg.models import Card  # noqa: E402
from mtg.helpers import (  # noqa: E402
    apply_enters_with_counters,
    parse_enters_with_counters,
)

_CACHE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'data', 'card_data_cache.json')


def _cache():
    with open(_CACHE_PATH, encoding='utf-8') as handle:
        return json.load(handle)


def _card_from_cache(name):
    """Build a Card the way the deck loader does — from the disk cache."""
    entry = _cache()[name.lower()]
    return Card(
        name=entry.get('name', name),
        oracle_text=entry.get('oracle_text', ''),
        type_line=entry.get('type_line', ''),
        mana_cost=entry.get('mana_cost', ''),
    )


# ---------------------------------------------------------------------------
# F1 — "enters with N <type> counters on it" had no generic parser.
# game_1536023819976835212: Dark Depths entered with ZERO ice counters
# instead of ten, so a {30} unlock collapsed into one {3} activation and the
# sacrifice trigger then reached Tier 3, which fabricated a 2/2 Vampire in
# place of the printed legendary 20/20 Marit Lage.
# ---------------------------------------------------------------------------

class TestEntersWithCounters:

    def test_dark_depths_enters_with_ten_ice_counters(self):
        card = _card_from_cache('Dark Depths')
        assert 'ten ice counters' in card.oracle_text.lower(), (
            'fixture drifted from the cached oracle')
        apply_enters_with_counters(card)
        assert card.counters.get('ice') == 10

    def test_land_entry_path_applies_the_counters(self):
        """Dark Depths is a LAND, so it never reaches the cast funnel where
        the four specific enters-with parses live. Exercise the land seam."""
        from mtg.triggers import _handle_land_etb

        game = _make_game()
        player = game.players[0]
        card = _card_from_cache('Dark Depths')
        player.battlefield.append(card)

        class _Engine:
            engine_ref = None

        _handle_land_etb(_Engine(), game, player, card)
        assert card.counters.get('ice') == 10, (
            'the land entry seam must apply the clause — the cast funnel '
            'never sees a land')

    def test_for_each_multiplier_is_refused(self):
        """Everflowing Chalice reads "enters with a charge counter on it FOR
        EACH TIME IT WAS KICKED" — the printed number is a multiplier, not a
        total. Flattening it hands an unkicked Chalice a free counter. This
        exact conflict was caught by the Aug-2 multikicker pin on the fix's
        first run."""
        card = _card_from_cache('Everflowing Chalice')
        assert parse_enters_with_counters(card.oracle_text) == []
        apply_enters_with_counters(card)
        assert not card.counters.get('charge')

    def test_specific_parse_wins_when_already_applied(self):
        """Non-stacking: a counter type already present is left alone, so the
        four specific cast-funnel parses keep their own arithmetic and two
        seams firing for one entry cannot double the counters."""
        card = _card_from_cache('Sanctuary Warden')
        card.counters = {'shield': 2}
        assert apply_enters_with_counters(card) == []
        assert card.counters['shield'] == 2

    def test_x_clause_is_cast_only(self):
        """CR 107.3b: X is zero outside the stack, and `_x_value` is NOT
        cleared by reset_battlefield_state — so a REANIMATED Astral
        Cornucopia must not resurrect the X from a previous cast."""
        card = _card_from_cache('Astral Cornucopia')
        card._x_value = 3
        assert apply_enters_with_counters(card) == [], (
            'the noncast seams must not honour a stale _x_value')
        assert not card.counters.get('charge')
        card2 = _card_from_cache('Astral Cornucopia')
        card2._x_value = 3
        apply_enters_with_counters(card2, allow_x=True)
        assert card2.counters.get('charge') == 3


# ---------------------------------------------------------------------------
# F2 — sacrifice-as-cost bypassed the shared zone router.
# game_1536017757303341078: an unearthed Dregscape Zombie fed to Altar of
# Dementia landed in the GRAVEYARD, so it was immediately unearthable again —
# a {B}-per-cycle mill loop the strategist was actively assembling.
# ---------------------------------------------------------------------------

class TestSacrificeAsCostRouting:

    def _game(self):
        game = _make_game()
        return game, game.players[0]

    def test_unearthed_creature_sacrificed_as_cost_is_exiled(self):
        """CR 702.83a — exiled on leaving the battlefield, not just at EOT."""
        from mtg.helpers import route_dead_permanent

        game, player = self._game()
        zombie = _card_from_cache('Dregscape Zombie')
        assert 'unearth' in zombie.oracle_text.lower()
        zombie.owner_index = 0
        zombie._unearthed = True
        player.battlefield.append(zombie)

        player.battlefield.remove(zombie)
        dest = route_dead_permanent(game, zombie, player,
                                    reason='sacrificed as a cost')
        assert dest == 'exile', (
            'an unearthed permanent leaving the battlefield is exiled, not '
            'put into a graveyard where it can be unearthed again')
        assert zombie in player.exile
        assert zombie not in player.graveyard

    def test_all_three_sacrifice_cost_sites_route(self):
        """Structural: the two engine.py activation-cost branches and the
        cog.py manual twin must all call the router. A bare
        `graveyard.append` at any of them silently reopens CR 702.83a /
        404.3 / 903.9a — the documented two-paths divergence."""
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for rel, marker in (
                ('mtg/engine.py', 'Sacrificed {perm.name} as cost'),
                ('mtg/engine.py', 'Sacrificed {sac_target.name} as cost for'),
                ('mtg/cog.py', 'sacrificed {sac_target.name} for'),
        ):
            with open(os.path.join(root, rel), encoding='utf-8') as handle:
                src = handle.read()
            idx = src.find(marker)
            assert idx != -1, f'{rel}: marker moved: {marker}'
            window = src[max(0, idx - 1400):idx + 200]
            assert 'route_dead_permanent' in window, (
                f'{rel}: the sacrifice-as-cost site near {marker!r} no longer '
                f'routes through route_dead_permanent')


# ---------------------------------------------------------------------------
# F3 — action types advertised to the model and dispatched by both executors
# but documented in NEITHER prompt grammar, so the model could never emit
# them. game_1536017876400611338: Lurrus was offered 7 times, every attempt
# came back as `cast`, and it never left the companion zone.
# ---------------------------------------------------------------------------

class TestPromptGrammarCoversAdvertisedActions:

    def test_every_advertised_action_type_is_documented_in_both_blocks(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, 'mtg/legal_actions.py'), encoding='utf-8') as handle:
            advertised = set(re.findall(r'"type":\s*"([a-z_]+)"', handle.read()))
        with open(os.path.join(root, 'mtg/claude_player.py'), encoding='utf-8') as handle:
            prompts = handle.read()
        # The two grammar blocks: the inline decide_action examples and the
        # plan_turn examples. Both must name every type the provider offers,
        # or the offer is decorative.
        documented = set(re.findall(r'\{"type":\s*"([a-z_]+)"', prompts))
        missing = sorted(advertised - documented - {'cast'})
        assert not missing, (
            f'advertised to the model but absent from the prompt grammar: '
            f'{missing} — the model cannot emit a JSON shape it was never '
            f'shown, so the offer, the executor branch and the executor '
            f'consistency pin are all dead weight')

    @pytest.mark.parametrize('action_type', ['companion', 'cycle', 'crew'])
    def test_specific_types_present_in_both_grammar_blocks(self, action_type):
        """`crew` was in the plan block only, so the inline fallback path
        could never crew; `companion` and `cycle` were in neither."""
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, 'mtg/claude_player.py'), encoding='utf-8') as handle:
            src = handle.read()
        occurrences = len(re.findall(r'\{"type":\s*"%s"' % action_type, src))
        assert occurrences >= 2, (
            f'{action_type} appears in {occurrences} grammar block(s); both '
            f'the inline and the plan prompt must document it')


# ---------------------------------------------------------------------------
# F4 — the cube deck builder classified every land with rules text as a
# SPELL. game_1536017666509119572: both drafted decks came out 21 lands / 19
# spells instead of 17/23, and the pool-land branch was dead code.
# ---------------------------------------------------------------------------

class TestCubeDeckBuilderLandClassification:

    def _pool(self, n_spells, n_lands):
        pool = [Card(name=f'Spell{i}', type_line='Creature — Human',
                     mana_cost='{1}{W}', oracle_text='Vanilla.')
                for i in range(n_spells)]
        pool += [Card(name=f'Utility Land {i}', type_line='Land',
                      oracle_text='{T}: Add {W}. This land enters tapped.')
                 for i in range(n_lands)]
        return pool

    def test_utility_lands_count_as_lands(self):
        from cube_draft import auto_build_deck

        deck, _ = auto_build_deck(self._pool(30, 6), 40)
        lands = [c for c in deck if 'land' in (c.type_line or '').lower()]
        assert len(deck) == 40
        assert len(lands) == 17, (
            f'{len(lands)} lands — a land with rules text is still being '
            f'ranked as a spell and then topped up with a full 17 basics')
        assert sum(1 for c in lands if 'Utility' in c.name) == 6

    def test_land_heavy_pool_does_not_overshoot(self):
        """The pool-land branch was dead code before this fix, so it never
        needed a cap. Reachable, it does: without one, basics_needed goes
        negative and the deck overshoots deck_size."""
        from cube_draft import auto_build_deck

        deck, _ = auto_build_deck(self._pool(25, 30), 40)
        lands = [c for c in deck if 'land' in (c.type_line or '').lower()]
        assert len(deck) == 40
        assert len(lands) == 17


# ---------------------------------------------------------------------------
# F5 — the Phyrexian Tower tap-sacrifice had NO undying/persist save chain.
# game_1536023914910588968: a Young Wolf fed to the Tower was permanently
# lost while an identical Butcher Ghoul dying via SBA returned in the same
# game. Fourth member of the "one death path lacks the chain" family.
# ---------------------------------------------------------------------------

class TestPhyrexianTowerDeathSave:

    def test_shared_save_chain_returns_an_undying_creature(self):
        from mtg.sba import apply_death_save_on_sacrifice

        game = _make_game()
        player = game.players[0]
        wolf = _card_from_cache('Young Wolf')
        assert 'undying' in wolf.oracle_text.lower()
        wolf.owner_index = 0
        player.graveyard.append(wolf)

        from mtg.rules_engine import RulesEngine
        rules = RulesEngine(None)

        msgs = apply_death_save_on_sacrifice(rules, game, player, wolf)
        assert msgs, 'undying must return the creature'
        assert wolf in player.battlefield
        assert wolf not in player.graveyard
        assert wolf.counters.get('+1/+1') == 1

    def test_counter_gate_blocks_a_second_return(self):
        """CR 702.92a — undying only returns a creature that had NO +1/+1
        counter. The second Tower sacrifice in the live game was correct by
        accident (the Ghoul already had its counter); make it correct by
        check."""
        from mtg.sba import apply_death_save_on_sacrifice

        game = _make_game()
        player = game.players[0]
        wolf = _card_from_cache('Young Wolf')
        wolf.owner_index = 0
        wolf.counters = {'+1/+1': 1}
        player.graveyard.append(wolf)

        from mtg.rules_engine import RulesEngine
        rules = RulesEngine(None)

        assert apply_death_save_on_sacrifice(rules, game, player, wolf) == []
        assert wolf in player.graveyard

    def test_tower_sacrifice_end_to_end_returns_the_creature(self):
        """BEHAVIOURAL, not structural. The first version of this pin looked
        for the helper's NAME inside Player._apply_sac_cost_at_tap and was
        killed by a mutant that merely aliased a different import to that
        name — a pin a mutant can dodge is a comment. Drive the real tap
        path instead: Phyrexian Tower eats a Young Wolf and the Wolf must
        come back with a +1/+1 counter (game_1536023914910588968 lost it)."""
        from mtg.rules_engine import RulesEngine

        game = _make_game()
        player = game.players[0]
        game._rules_engine = RulesEngine(None)

        tower = _card_from_cache('Phyrexian Tower')
        wolf = _card_from_cache('Young Wolf')
        wolf.owner_index = 0
        wolf.summoning_sick = False
        player.battlefield.extend([tower, wolf])

        victim = player._apply_sac_cost_at_tap(tower, game=game)
        assert victim is wolf, 'the Tower must have eaten the Wolf'
        assert wolf in player.battlefield, (
            'undying must return the sacrificed creature — the Tower path '
            'fired dies and sacrifice triggers but had no save chain')
        assert wolf not in player.graveyard
        assert wolf.counters.get('+1/+1') == 1


# ---------------------------------------------------------------------------
# F6 — one shared target restriction was applied to every action a template
# emits, using the controller from the FIRST target phrase only.
# game_1536023840566546562: Blizzard Brawl fizzled with legal targets on both
# sides. game_1536023731808509984: Kogla's fight dropped the back-damage.
# ---------------------------------------------------------------------------

class TestSharedTargetRestrictionController:

    def _restriction(self, name):
        from rules.targeting_helpers import _parse_target_restriction_from_oracle
        return _parse_target_restriction_from_oracle(_card_from_cache(name))

    def test_disagreeing_phrases_drop_the_controller_constraint(self):
        """CR 601.2c / 701.12a: each target has its own restriction. Blizzard
        Brawl's two clauses disagree, so no single shared controller value is
        sound — and YOU (from phrase one) provably blocked the opponent-side
        target the card requires."""
        from rules.targeting import ControllerRestriction

        brawl = self._restriction('Blizzard Brawl')
        assert brawl is not None
        assert brawl.controller is ControllerRestriction.ANY

    def test_fight_back_damage_target_is_not_rejected(self):
        """Kogla's only printed phrase is "you don't control", but the fight
        legitimately emits a second damage action aimed at the SOURCE."""
        from rules.targeting import ControllerRestriction

        kogla = self._restriction('Kogla, the Titan Ape')
        assert kogla is not None
        assert kogla.controller is ControllerRestriction.ANY

    @pytest.mark.parametrize('name,expected', [
        ('Cyclonic Rift', 'OPPONENT'),
        ('Snakeskin Veil', 'YOU'),
    ])
    def test_single_clause_cards_keep_their_controller(self, name, expected):
        """The relaxation must fire ONLY on disagreement — a card with one
        controller-restricted phrase keeps its constraint, or the July-31
        Cyclonic Rift gate and the Aug-9 Snakeskin Veil fix both regress."""
        restriction = self._restriction(name)
        assert restriction is not None
        assert restriction.controller.name == expected

    def test_nonland_subsumption_still_holds(self):
        """Control for the Aug-9 CO-2 fix living in the same function."""
        from rules.targeting import TargetType

        roil = self._restriction('Into the Roil')
        assert roil is not None
        assert TargetType.NONLAND_PERMANENT in roil.target_types
        assert TargetType.PERMANENT not in roil.target_types


# ---------------------------------------------------------------------------
# F7 — Vivien, Champion of the Wilds' +1 ignored the target the engine chose.
# game_1536000998537953280: [PW-TARGET] announced one creature and the grant
# landed on another, on two separate activations.
# ---------------------------------------------------------------------------

class TestVivienHonoursExplicitTarget:

    def _resolve(self, ctx):
        from rules.effect_templates import get_effect_library

        library = get_effect_library()
        template = library._pw_ability_templates[
            ('vivien, champion of the wilds', 'vigilance and reach')]
        return template.action_generator('Rick', 'Qwen', ctx)

    def test_explicit_target_wins_over_battlefield_order(self):
        actions = self._resolve({
            'explicit_target_name': 'Tidespout Tyrant',
            'best_own_creature': 'Rashmi, Eternities Crafter',
        })
        assert actions[0]['action'] == 'grant_keywords'
        assert actions[0]['target_card'] == 'Tidespout Tyrant'

    def test_falls_back_when_no_explicit_target(self):
        actions = self._resolve({'best_own_creature': 'Rashmi, Eternities Crafter'})
        assert actions[0]['target_card'] == 'Rashmi, Eternities Crafter'

    def test_no_legal_target_is_a_handled_no_op(self):
        actions = self._resolve({})
        assert actions[0]['action'] == 'no_action'
