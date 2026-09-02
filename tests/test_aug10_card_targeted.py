"""Pins for the Aug 10, 2026 CARD-TARGETED reviewer wave.

Selection came from tools/card_coverage.py rather than archetype recency:
142 cards hit play in batch 6a30802 and had never appeared inside a game any
reviewer read, so the twelve densest games were sampled and each reviewer was
handed the explicit card list. That produced 28 findings from 12 games — a
different and denser class than matchup-based sampling was surfacing.

This file covers the six fixed in that session. The rest are recorded with
mechanisms in CLAUDE.md.
"""
import asyncio
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from conftest import _make_card, _make_game  # noqa: E402

_CACHE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'data', 'card_data_cache.json')


def _oracle(name):
    with open(_CACHE_PATH, encoding='utf-8') as handle:
        entry = json.load(handle)[name.lower()]
    if entry.get('card_faces'):
        return entry['card_faces'][0].get('oracle_text', '') or ''
    return entry.get('oracle_text', '') or ''


# ---------------------------------------------------------------------------
# The Tier-3 resolve dedupe key was shared across CONCURRENT GAMES.
# game_1536028980996472842: a fully-paid Killing Wave (X=2, three sources
# tapped) was cancelled because a DIFFERENT game resolved the same card on
# its own turn 18, two seconds earlier.
# ---------------------------------------------------------------------------

class TestResolveDedupeIsPerGame:

    def test_two_games_do_not_share_a_dedupe_entry(self):
        from mtg.judge import resolve_effect
        from mtg.rules_engine import RulesEngine

        rules = RulesEngine(None)          # the singleton every game shares
        game_a, game_b = _make_game(), _make_game()
        game_a.thread_id, game_b.thread_id = 111, 222
        game_a.turn_number = game_b.turn_number = 18

        # A client-less RulesEngine returns before the dedupe write, so stand
        # up a stub that fails at the API call — the key is recorded first,
        # which is the behaviour under test.
        class _Boom:
            class messages:
                @staticmethod
                def create(*_a, **_k):
                    raise RuntimeError('no network in tests')

        rules.client = _Boom()
        for game in (game_a, game_b):
            try:
                asyncio.run(resolve_effect(rules, game, 'Killing Wave',
                                           'sacrifice unless paid'))
            except Exception:
                pass

        keys = list(rules._resolve_dedupe)
        assert keys, 'the guard must record something'
        assert all(len(k) == 4 for k in keys), f'key shape changed: {keys}'
        assert {k[0] for k in keys} == {111, 222}, (
            f'each game must get its OWN entry; shared keys let one game '
            f'cancel another. got {keys}')

    def test_pruner_matches_the_key_shape_and_only_its_own_game(self):
        """The pruner tested `len(k) == 3`; with the game id added that
        matches nothing and would silently wipe every entry. It was also
        cross-game — ageing out other games' keys against THIS game's turn."""
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, 'mtg/engine.py'), encoding='utf-8') as handle:
            src = handle.read()
        index = src.find('_resolve_dedupe.items()')
        assert index != -1
        window = src[index - 400:index + 400]
        assert 'len(k) != 4' in window or 'len(k) == 4' in window
        assert 'k[0] != _tid' in window, 'the pruner must not touch other games'


# ---------------------------------------------------------------------------
# Every subtype-restricted anthem in the layers engine was inert: `Card` has
# no `subtypes` attribute, so getattr(...) always returned [].
# game_1536028774808690810: Captivating Vampire registered correctly and its
# Vampire Nighthawk still dealt 2, not 3, across four combats.
# ---------------------------------------------------------------------------

class TestSubtypeAnthemsReachTheLayersEngine:

    def test_card_has_no_subtypes_attribute(self):
        """The premise: this is why the old read could never work."""
        card = _make_card('X', type_line='Creature - Vampire Shaman')
        assert not hasattr(card, 'subtypes'), (
            'if Card ever gains a real `subtypes` field, revisit the read '
            'in recalculate_power_toughness')

    def test_get_creature_types_is_the_real_accessor(self):
        card = _make_card('Vampire Nighthawk',
                          type_line='Creature - Vampire Shaman')
        types = [t.lower() for t in (card.get_creature_types() or [])]
        assert 'vampire' in types and 'shaman' in types

    def test_subtype_anthem_actually_buffs_a_matching_creature(self):
        game = _make_game()
        player = game.players[0]
        lord = _make_card('Captivating Vampire',
                          type_line='Creature - Vampire',
                          power='2', toughness='2',
                          oracle_text=_oracle('Captivating Vampire'))
        hawk = _make_card('Vampire Nighthawk',
                          type_line='Creature - Vampire Shaman',
                          power='2', toughness='3')
        bystander = _make_card('Grizzly Bears', type_line='Creature - Bear',
                               power='2', toughness='2')
        player.battlefield.extend([lord, hawk, bystander])
        game.register_static_pt_effects(lord, player.name)
        game.recalculate_power_toughness()
        assert hawk.get_effective_power(game) == 3, (
            'a Vampire must receive the Vampire lord anthem')
        assert bystander.get_effective_power(game) == 2, (
            'a non-Vampire must NOT — "Other Vampire creatures you control"')
        assert lord.get_effective_power(game) == 2, (
            'and the lord excludes itself (CR 109.5 "other")')


# ---------------------------------------------------------------------------
# Reanimate was unresolvable whenever a target was declared: the cast gate's
# skip phrase knew only "in a graveyard" (Animate Dead) while Reanimate
# prints "from a graveyard". 45 of 49 failed casts across the batch.
# ---------------------------------------------------------------------------

class TestGraveyardTargetSkipPhrase:

    def test_both_printed_phrasings_skip_the_battlefield_validator(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, 'mtg/spells.py'), encoding='utf-8') as handle:
            src = handle.read()
        index = src.find("_p in _oracle_lower for _p in (")
        assert index != -1, 'the graveyard skip guard moved'
        window = src[index:index + 200]
        assert "'in a graveyard'" in window and "'from a graveyard'" in window

    def test_the_restriction_parser_accepts_both_too(self):
        """The parser always knew both; only the skip guard was narrower —
        which is what routed a declared Reanimate target into the
        battlefield-oriented validator."""
        from rules.targeting import TargetTextParser

        for phrase in ('target creature card from a graveyard',
                       'enchant creature card in a graveyard'):
            restriction = TargetTextParser.parse(phrase)
            assert restriction.zone == 'graveyard', phrase

    def test_reanimate_prints_from_not_in(self):
        assert 'from a graveyard' in _oracle('Reanimate').lower()
        assert 'in a graveyard' in _oracle('Animate Dead').lower()


# ---------------------------------------------------------------------------
# Two planeswalker mana abilities, one function.
# Xenagos's "+1: Add X mana ... where X is the number of creatures you
# control" fell to a hardcoded default of 2 — five activations, all exactly
# 2 red, one of them with ZERO creatures on board.
# Domri's "+1: Add {R} or {G}" added BOTH.
# ---------------------------------------------------------------------------

class TestPlaneswalkerManaAbilities:

    def _activate(self, name, ability_text, creatures=0,
                  want_restricted=False):
        """Drive the REAL activation path — manager.activate is async and
        takes an ability INDEX, parsing the abilities off the oracle itself,
        so a hand-built ability object would test a shape production never
        constructs."""
        from rules.planeswalker import PlaneswalkerManager

        game = _make_game()
        player = game.players[0]
        walker = _make_card(name, type_line='Legendary Planeswalker - Test',
                            mana_cost='{2}{R}{G}', oracle_text=ability_text)
        walker.loyalty_counters = 5
        player.battlefield.append(walker)
        for i in range(creatures):
            player.battlefield.append(
                _make_card(f'Bear{i}', type_line='Creature - Bear',
                           power='2', toughness='2'))
        asyncio.run(PlaneswalkerManager().activate(game, player, walker, 0))
        if want_restricted:
            total = 0
            for entry in (getattr(player, 'restricted_mana_pool', None) or []):
                total += int(entry.get('amount', 0) if isinstance(entry, dict)
                             else getattr(entry, 'amount', 0) or 0)
            return dict(player.mana_pool), total
        return dict(player.mana_pool)

    def test_x_is_computed_from_the_board_not_hardcoded(self):
        text = ('+1: Add X mana in any combination of {R} and/or {G}, where X '
                'is the number of creatures you control.')
        with_three = self._activate('Xenagos, the Reveler', text, creatures=3)
        with_none = self._activate('Xenagos, the Reveler', text, creatures=0)
        assert sum(with_three.values()) == 3, (
            f'X must track the creature count, got {with_three}')
        assert sum(with_none.values()) == 0, (
            f'zero creatures must add NO mana, got {with_none} — the old '
            f'default fabricated 2')

    def test_or_adds_one_mana_not_both(self):
        pool = self._activate(
            'Domri, Anarch of Bolas',
            '+1: Add {R} or {G}. Creature spells you cast this turn '
            "can't be countered.")
        assert sum(pool.values()) == 1, (
            f'a disjunction grants ONE mana of the choice, got {pool}')

    def test_the_disjunction_test_is_anchored_to_the_mana_symbols(self):
        """A bare `' or ' in text` truncated Jaya Ballard's {R}{R}{R} because
        her restriction clause says "instant OR sorcery spells" — the
        substring trap, inside the fix for a substring trap. The existing R4
        pin caught it; this keeps it caught."""
        pool, restricted = self._activate(
            'Jaya Ballard',
            '+1: Add {R}{R}{R}. Spend this mana only to cast instant or '
            'sorcery spells.', want_restricted=True)
        # Jaya's mana is RESTRICTED, so it lands in the restricted pool, not
        # mana_pool — reading the wrong one would make this pin vacuous.
        assert restricted == 3, f'Jaya adds three restricted R, got {restricted}'


# ---------------------------------------------------------------------------
# The damaged-creature scan knew only the ACTIVE-voice templating, so the
# PASSIVE printing matched nothing and returned before even the
# unhandled-queue breadcrumb. Phyrexian Negator was a {2}{B} 5/5 with no
# drawback at all; Ill-Tempered Loner took 6 and dealt nothing back.
# ---------------------------------------------------------------------------

class TestPassiveDamagedTrigger:

    def test_both_printings_are_recognised(self):
        assert 'is dealt damage' in _oracle('Phyrexian Negator').lower()
        assert 'deals damage to' in _oracle('Phyrexian Obliterator').lower()

    def _damaged(self, name, oracle, amount=2):
        from mtg.rules_engine import RulesEngine
        from mtg.triggers import scan_damaged_creature

        game = _make_game()
        owner, attacker_owner = game.players
        victim = _make_card(name, type_line='Creature - Horror',
                            power='5', toughness='5', oracle_text=oracle)
        owner.battlefield.append(victim)
        for i in range(4):
            owner.battlefield.append(
                _make_card(f'Own{i}', type_line='Artifact'))
            attacker_owner.battlefield.append(
                _make_card(f'Opp{i}', type_line='Artifact'))
        rules = RulesEngine(None)
        rules.engine_ref = None
        game._rules_engine = rules
        scan_damaged_creature(rules, game, victim, amount, attacker_owner)
        return owner, attacker_owner

    def test_negator_sacrifices_its_OWN_controllers_permanents(self):
        """Negator prints only "sacrifice that many permanents" — the damaged
        creature's own controller. Resolving it in the source's direction
        would be worse than not firing."""
        owner, opponent = self._damaged(
            'Phyrexian Negator', _oracle('Phyrexian Negator'), amount=2)
        assert len(owner.graveyard) == 2, (
            f"Negator's controller sacrifices, got {len(owner.graveyard)}")
        assert len(opponent.graveyard) == 0

    def test_obliterator_sacrifices_the_SOURCES_controllers_permanents(self):
        owner, opponent = self._damaged(
            'Phyrexian Obliterator', _oracle('Phyrexian Obliterator'), amount=2)
        assert len(opponent.graveyard) == 2, (
            f"Obliterator hits the source's controller, got "
            f'{len(opponent.graveyard)}')
        assert len(owner.graveyard) == 0

    def test_unmatched_passive_shape_still_reaches_the_unhandled_queue(self):
        """Ill-Tempered Loner's "it deals that much damage to any target" has
        no deterministic branch — it must at least leave a breadcrumb rather
        than returning silently."""
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, 'mtg/triggers.py'), encoding='utf-8') as handle:
            src = handle.read()
        index = src.find('def scan_damaged_creature')
        # Sep 1 2026: slice to the enclosing function, not a 4000-char
        # window — the dead-owner fallback and the Reckoner branch pushed
        # the breadcrumb past it (the char-window-outgrown class).
        end = src.find('\ndef ', index + 1)
        body = src[index:end if end != -1 else None]
        assert '_passive = bool(_dm)' in body
        assert 'DAMAGED-TRIGGER-UNHANDLED' in body


# ===========================================================================
# The reviewed-games ledger (Aug 10, follow-up).
#
# "Reviewed" was derived purely from game ids cited in CLAUDE.md prose, which
# worked only because every wave happened to be written up as a matchup ledger
# listing each game. THIS wave was written up by finding and cited zero ids,
# so its twelve games read as unreviewed and the index began recommending them
# for re-reading — one suggestion listed nine "novel" cards, seven of which
# were that reviewer's own findings.
#
# data/reviewed_games.json is now the record. These pins guard the silent-drop
# failure mode: the loader swallows a missing/malformed file by design (so the
# tool still works without it), which means a broken read looks exactly like
# "no games recorded".
#
# NOTE the recovered ids are deliberately NOT re-listed in CLAUDE.md prose.
# Duplicating the record into prose is what broke it, and it would also make
# the first pin below vacuous — CLAUDE.md would supply the ids the JSON is
# being tested for.
# ===========================================================================

class TestReviewedGamesLedger:

    def _tool(self):
        sys.path.insert(0, os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'tools'))
        import card_coverage
        return card_coverage

    def test_ledger_ids_reach_the_reviewed_set(self):
        """Decisive because these ids appear in NO other source."""
        tool = self._tool()
        with open(tool.REVIEWED_GAMES, encoding='utf-8') as handle:
            recorded = list(json.load(handle)['games'])
        assert recorded, 'the ledger is empty — nothing to verify'
        with open(tool.DOC, encoding='utf-8') as handle:
            prose = handle.read()
        reviewed = tool._reviewed_game_ids()
        checked = 0
        for key in recorded:
            digits = key.replace('game_', '')
            if digits in prose:
                continue        # also cited in prose; proves nothing here
            assert digits in reviewed, f'{key} recorded but not counted reviewed'
            checked += 1
        assert checked, ('every recorded id is also in CLAUDE.md, so this pin '
                         'cannot distinguish the two sources')

    def test_claude_md_remains_a_source(self):
        """The historical games must keep counting without a backfill.

        Deliberately a subset assertion with no non-emptiness guard: the
        PUBLIC fork's CLAUDE.md is a different, scrubbed document that cites
        no game ids at all, so requiring some would fail there for a reason
        that has nothing to do with the property. The pin is therefore
        vacuous in the fork and decisive in this repo, where the historical
        149 reviewed games are recorded nowhere else.
        """
        tool = self._tool()
        with open(tool.DOC, encoding='utf-8') as handle:
            prose_ids = set(re.findall(r'game_(\d{15,})', handle.read()))
        assert prose_ids <= tool._reviewed_game_ids()

    def test_every_recorded_key_is_a_game_id(self):
        tool = self._tool()
        with open(tool.REVIEWED_GAMES, encoding='utf-8') as handle:
            data = json.load(handle)
        for key, entry in data['games'].items():
            assert re.fullmatch(r'game_\d{15,}', key), f'malformed key: {key}'
            assert entry.get('wave'), f'{key} has no wave'
            # A recovered id is an inference, not a record — it has to say so
            # and say what the evidence was.
            assert entry.get('confidence') in ('recorded', 'recovered')
            if entry['confidence'] == 'recovered':
                assert entry.get('note'), f'{key} recovered with no evidence'
