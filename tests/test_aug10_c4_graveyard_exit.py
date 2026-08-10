"""C4 -- "whenever cards leave your graveyard" (CR 603.2).

THE RULES ANSWER THESE PINS ENCODE, taken from Gatherer rulings rather than
from the wording (the deferral existed precisely because the wording alone
does not settle it):

  Tormod, the Desecrator, 2020-11-10 -- "You create one Zombie token each time
    Tormod's ability RESOLVES, no matter how many cards left your graveyard."
  Desecrated Tomb, 2018-07-13 -- "You create one Bat token each time
    Desecrated Tomb's ability TRIGGERS, no matter how many cards left your
    graveyard."
  Syr Konrad, the Grim, 2019-10-04 -- "If one or more creatures die at the same
    time as Syr Konrad, ITS FIRST ABILITY TRIGGERS FOR EACH of those
    creatures."  Konrad's three or-joined conditions are ONE ability, and that
    ruling reads it per-object; his leave condition is likewise singular ("a
    creature card") with no "one or more" consolidator.

So "one or more" is a load-bearing consolidator, not decoration, and the two
firing arithmetics have to coexist in one batch. `test_the_two_arithmetics_
coexist_in_one_batch` is the decisive pin: the same three departures must
produce ONE Zombie and THREE damage.

Fixture discipline (the pin-shape-reachability ledger): oracle text is read
from the disk cache for cards the bot has seen, and quoted verbatim from the
Scryfall bulk dump for the four family members it has not -- never from
memory. Every behavioural pin drives `engine.check_state_based_actions`, the
entry point production actually calls, rather than the drain in isolation.
Where a pin necessarily uses invented text (no printed card combines a
restriction with an effect this engine resolves deterministically) it says so.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from conftest import _make_card, _make_game  # noqa: E402

_CACHE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'data', 'card_data_cache.json')

# Verbatim from data/scryfall_oracle_cards.json (the full bulk dump) for the
# family members absent from the bot's cache. Copied, not recalled -- an
# oracle claim from memory has been wrong in this repo before.
_BULK_TEXT = {
    'Oasis of Renewal': (
        'When Oasis of Renewal enters and whenever a land card leaves your '
        'graveyard, seek a land card. This ability triggers only once each '
        'turn.\nWhen Oasis of Renewal enters and whenever a nonland card '
        'leaves your graveyard, seek a nonland card. This ability triggers '
        'only once each turn.'),
    'Kishla Skimmer': (
        'Flying\nWhenever a card leaves your graveyard during your turn, draw '
        'a card. This ability triggers only once each turn.'),
    'Murktide Regent': (
        'Delve (Each card you exile from your graveyard while casting this '
        'spell pays for {1}.)\nFlying\nThis creature enters with a +1/+1 '
        'counter on it for each instant and sorcery card exiled with it.\n'
        'Whenever an instant or sorcery card leaves your graveyard, put a '
        '+1/+1 counter on this creature.'),
    'Thran Vigil': (
        'Whenever one or more artifact and/or creature cards leave your '
        'graveyard during your turn, put a +1/+1 counter on target creature '
        'you control.'),
    # The unearth reminder template -- the dominant false positive for any
    # detector loose enough to accept "leave" and "graveyard" separately. 116
    # of the 158 bulk cards carrying both words are this shape, and Aug 9
    # seeded exactly this card into test_graveyard_meren.
    'Dregscape Zombie': (
        'Unearth {B} ({B}: Return this card from your graveyard to the '
        'battlefield. It gains haste. Exile it at the beginning of the next '
        'end step or if it would leave the battlefield. Unearth only as a '
        'sorcery.)'),
    # A genuine "leaves the graveyard" phrase that is NOT a trigger: duration
    # expiry, inside reminder text.
    'Microscope': 'Whatever. (This effect ends if it leaves the graveyard.)',
    # Scoped to an OPPONENT's graveyard, not "your" -- correctly unmatched.
    "Erebos's Titan": ("Whenever a creature card leaves an opponent's "
                       'graveyard, you may discard a card.'),
}


def _oracle(name):
    """Printed oracle text: disk cache first, bulk-dump quotes second."""
    with open(_CACHE_PATH, encoding='utf-8') as handle:
        cache = json.load(handle)
    entry = cache.get(name.lower())
    if entry is not None:
        return entry.get('oracle_text', '') or ''
    if name in _BULK_TEXT:
        return _BULK_TEXT[name]
    raise KeyError(name)


def _engine_game():
    """A clientless engine wired the way production wires one."""
    from mtg.engine import GameEngine
    engine = GameEngine(None)
    game = _make_game()
    game._rules_engine = engine.rules
    return engine, game


def _watcher(name, **overrides):
    defaults = dict(type_line='Legendary Creature — Human', power='3',
                    toughness='3', oracle_text=_oracle(name))
    defaults.update(overrides)
    return _make_card(name, **defaults)


def _settle(engine, game):
    """Run the production drain path once and return its messages."""
    return engine.check_state_based_actions(game)


def _tokens_named(player, token_name):
    return [c for c in player.battlefield if c.name == token_name]


# ===========================================================================
# The firing arithmetic -- the rulings above, as behaviour.
# ===========================================================================

class TestFiringArithmetic:

    def test_tormod_fires_once_for_a_whole_batch(self):
        """One Zombie for four departures, per the 2020-11-10 ruling.

        This is the misfire the whole design exists to prevent: a {4} delve
        removes four cards in one loop, and a per-card emit would mint four
        Zombies for one event.
        """
        engine, game = _engine_game()
        rick = game.players[0]
        rick.battlefield.append(_watcher('Tormod, the Desecrator'))
        departing = [_make_card(f'Fodder {i}') for i in range(4)]
        rick.graveyard.extend(departing)
        _settle(engine, game)                      # seeds the snapshot

        for card in departing:                     # one event, four cards
            rick.graveyard.remove(card)
        _settle(engine, game)

        assert len(_tokens_named(rick, 'Zombie')) == 1

    def test_syr_konrad_fires_once_per_card(self):
        """Three damage for three creature cards, per the 2019-10-04 ruling."""
        engine, game = _engine_game()
        rick, claude = game.players[0], game.players[1]
        rick.battlefield.append(_watcher('Syr Konrad, the Grim',
                                         power='5', toughness='4'))
        departing = [_make_card(f'Zombie Fodder {i}',
                                type_line='Creature — Zombie')
                     for i in range(3)]
        rick.graveyard.extend(departing)
        _settle(engine, game)

        before = claude.life
        for card in departing:
            rick.graveyard.remove(card)
        _settle(engine, game)

        # Fires once per card, not once for the batch: 3, not 1.
        assert before - claude.life == 3

    def test_the_two_arithmetics_coexist_in_one_batch(self):
        """THE decisive pin: one batch, two different firing counts.

        Tormod ("one or more cards") resolves once; Syr Konrad ("a creature
        card") resolves three times. A single-arithmetic implementation
        cannot satisfy both halves of this assertion at once.
        """
        engine, game = _engine_game()
        rick, claude = game.players[0], game.players[1]
        rick.battlefield.append(_watcher('Tormod, the Desecrator'))
        rick.battlefield.append(_watcher('Syr Konrad, the Grim',
                                         power='5', toughness='4'))
        departing = [_make_card(f'Zombie Fodder {i}',
                                type_line='Creature — Zombie')
                     for i in range(3)]
        rick.graveyard.extend(departing)
        _settle(engine, game)

        before = claude.life
        for card in departing:
            rick.graveyard.remove(card)
        _settle(engine, game)

        assert len(_tokens_named(rick, 'Zombie')) == 1
        assert before - claude.life == 3

    def test_tormod_token_is_the_printed_one(self):
        """Tapped 2/2 black Zombie -- the printed token, not a generic one."""
        engine, game = _engine_game()
        rick = game.players[0]
        rick.battlefield.append(_watcher('Tormod, the Desecrator'))
        card = _make_card('Fodder')
        rick.graveyard.append(card)
        _settle(engine, game)
        rick.graveyard.remove(card)
        _settle(engine, game)

        zombies = _tokens_named(rick, 'Zombie')
        assert len(zombies) == 1
        token = zombies[0]
        assert (token.power, token.toughness) == ('2', '2')
        assert token.tapped is True
        assert token.colors == ['B']
        assert token.is_token is True

    def test_desecrated_tomb_token_is_the_printed_one(self):
        """1/1 black Bat WITH FLYING -- a vanilla Bat blocks differently."""
        engine, game = _engine_game()
        rick = game.players[0]
        rick.battlefield.append(_watcher('Desecrated Tomb',
                                         type_line='Enchantment'))
        card = _make_card('Bear Corpse', type_line='Creature — Bear')
        rick.graveyard.append(card)
        _settle(engine, game)
        rick.graveyard.remove(card)
        _settle(engine, game)

        bats = _tokens_named(rick, 'Bat')
        assert len(bats) == 1
        assert (bats[0].power, bats[0].toughness) == ('1', '1')
        assert 'Flying' in bats[0].keywords


# ===========================================================================
# Type filters -- negation before the positive test.
# ===========================================================================

class TestTypeFilters:

    def test_desecrated_tomb_ignores_noncreature_departures(self):
        """Zero Bats when nothing that left was a creature card.

        The decisive direction: because Desecrated Tomb is CONSOLIDATED, a
        batch containing one creature would produce one Bat whether or not
        the filter worked. Only an all-noncreature batch separates them.
        """
        engine, game = _engine_game()
        rick = game.players[0]
        rick.battlefield.append(_watcher('Desecrated Tomb',
                                         type_line='Enchantment'))
        departing = [_make_card('Shock', type_line='Instant'),
                     _make_card('Wastes', type_line='Land'),
                     _make_card('Sol Ring', type_line='Artifact')]
        rick.graveyard.extend(departing)
        _settle(engine, game)
        for card in departing:
            rick.graveyard.remove(card)
        _settle(engine, game)

        assert _tokens_named(rick, 'Bat') == []

    def test_desecrated_tomb_counts_only_the_creature_cards(self):
        engine, game = _engine_game()
        rick = game.players[0]
        rick.battlefield.append(_watcher('Desecrated Tomb',
                                         type_line='Enchantment'))
        departing = [_make_card('Shock', type_line='Instant'),
                     _make_card('Bear', type_line='Creature — Bear')]
        rick.graveyard.extend(departing)
        _settle(engine, game)
        for card in departing:
            rick.graveyard.remove(card)
        _settle(engine, game)

        assert len(_tokens_named(rick, 'Bat')) == 1

    def test_oasis_land_and_nonland_clauses_do_not_cross_fire(self):
        """Both printed clauses live on ONE card with opposite filters.

        'land' is a substring of 'nonland', so a substring matcher fires both
        off a single land. Parsed from the real printed text, both directions
        asserted.
        """
        from mtg.triggers import (graveyard_leave_watcher_clauses,
                                  graveyard_leave_type_matches)
        oasis = _make_card('Oasis of Renewal', type_line='Enchantment',
                           oracle_text=_oracle('Oasis of Renewal'))
        clauses = graveyard_leave_watcher_clauses(oasis)
        filters = {tuple(c['types']): c for c in clauses}
        assert ('land',) in filters and ('nonland',) in filters

        forest = _make_card('Forest', type_line='Basic Land — Forest')
        bear = _make_card('Bear', type_line='Creature — Bear')

        assert graveyard_leave_type_matches(forest, ['land']) is True
        assert graveyard_leave_type_matches(forest, ['nonland']) is False
        assert graveyard_leave_type_matches(bear, ['nonland']) is True
        assert graveyard_leave_type_matches(bear, ['land']) is False

    def test_a_negated_filter_is_honoured_end_to_end(self):
        """The negation branch through the real drain.

        SYNTHETIC oracle text: no printed card combines a negated filter with
        an effect this engine resolves deterministically (Oasis "seek"s, which
        is unmodeled), so the filter is Oasis's and the effect is Tormod's.
        """
        engine, game = _engine_game()
        rick = game.players[0]
        rick.battlefield.append(_make_card(
            'Nonland Watcher', type_line='Enchantment',
            oracle_text=('Whenever a nonland card leaves your graveyard, '
                         'create a 1/1 white Soldier creature token.')))
        forest = _make_card('Forest', type_line='Basic Land — Forest')
        rick.graveyard.append(forest)
        _settle(engine, game)
        rick.graveyard.remove(forest)
        _settle(engine, game)

        assert _tokens_named(rick, 'Soldier') == []

    def test_murktide_reads_a_two_type_disjunction(self):
        from mtg.triggers import graveyard_leave_watcher_clauses
        regent = _make_card('Murktide Regent', type_line='Creature — Dragon',
                            oracle_text=_oracle('Murktide Regent'))
        clauses = graveyard_leave_watcher_clauses(regent)
        assert len(clauses) == 1
        assert clauses[0]['types'] == ['instant', 'sorcery']
        assert clauses[0]['consolidated'] is False


# ===========================================================================
# Detection -- what must NOT be read as a watcher.
# ===========================================================================

class TestDetection:

    def test_unearth_reminder_text_is_not_a_watcher(self):
        from mtg.triggers import graveyard_leave_watcher_clauses
        zombie = _make_card('Dregscape Zombie', type_line='Creature — Zombie',
                            oracle_text=_oracle('Dregscape Zombie'))
        assert graveyard_leave_watcher_clauses(zombie) == []

    def test_a_leaves_the_graveyard_duration_clause_is_not_a_trigger(self):
        from mtg.triggers import graveyard_leave_watcher_clauses
        scope = _make_card('Microscope', type_line='Artifact',
                           oracle_text=_oracle('Microscope'))
        assert graveyard_leave_watcher_clauses(scope) == []

    def test_an_opponents_graveyard_is_out_of_scope(self):
        from mtg.triggers import graveyard_leave_watcher_clauses
        titan = _make_card("Erebos's Titan", type_line='Creature — God',
                           oracle_text=_oracle("Erebos's Titan"))
        assert graveyard_leave_watcher_clauses(titan) == []

    def test_a_trigger_word_is_required(self):
        """Isolates the trigger-word check.

        Dregscape Zombie and Microscope are each excluded TWICE over (reminder
        stripping and the "your graveyard" adjacency both reject them), which
        is real defence in depth but means neither pin above can be killed by
        a single mutation. These three pins isolate one defence each so the
        properties are individually verifiable.

        The subject must be present for THIS pin to be decisive: "Cards leave
        your graveyard" is rejected for lacking a subject prefix whether or
        not the trigger-word check exists, and the mutation sweep caught that
        first fixture passing for the wrong reason.
        """
        from mtg.triggers import parse_graveyard_leave_clause
        assert parse_graveyard_leave_clause(
            'A creature card leaves your graveyard face down.') is None

    def test_a_trigger_inside_reminder_text_is_not_a_watcher(self):
        """Isolates the reminder strip. SYNTHETIC -- no printed card hides a
        real graveyard-leave trigger in reminder text, which is the point:
        reminder text is never rules text (CR 207.2)."""
        from mtg.triggers import graveyard_leave_watcher_clauses
        card = _make_card('Reminder Only', type_line='Creature — Bird',
                          oracle_text=('Flying (Whenever one or more cards '
                                       'leave your graveyard, this reminder '
                                       'is not an ability.)'))
        assert graveyard_leave_watcher_clauses(card) == []

    def test_leaves_the_battlefield_is_not_leaves_your_graveyard(self):
        """Isolates the anchor's adjacency requirement -- the single biggest
        false-positive vector in the corpus (116 of 158 bulk cards carrying
        both words separately are this shape)."""
        from mtg.triggers import parse_graveyard_leave_clause
        assert parse_graveyard_leave_clause(
            'Whenever a creature card leaves the battlefield, put it into '
            'your graveyard.') is None

    def test_the_plural_leave_verb_is_matched(self):
        """"one or more cards LEAVE" -- 34 family members use the plural verb,
        so a fixed "leaves your graveyard" string misses every one of them."""
        from mtg.triggers import graveyard_leave_watcher_clauses
        vigil = _make_card('Thran Vigil', type_line='Enchantment',
                           oracle_text=_oracle('Thran Vigil'))
        clauses = graveyard_leave_watcher_clauses(vigil)
        assert len(clauses) == 1
        assert clauses[0]['consolidated'] is True
        assert clauses[0]['types'] == ['artifact', 'creature']

    def test_the_cards_own_name_never_becomes_a_type_filter(self):
        """Oasis prints its own name between the trigger word and the subject.

        Without cutting at the last trigger word the filter would read
        ['oasis', 'of', 'renewal', 'enters', 'land'] -- harmless for Oasis,
        but a card named for a creature type would then match real cards.
        """
        from mtg.triggers import graveyard_leave_watcher_clauses
        oasis = _make_card('Oasis of Renewal', type_line='Enchantment',
                           oracle_text=_oracle('Oasis of Renewal'))
        for clause in graveyard_leave_watcher_clauses(oasis):
            assert clause['types'] in (['land'], ['nonland'])


# ===========================================================================
# Zone and object identity.
# ===========================================================================

class TestZoneAndIdentity:

    def test_a_token_leaving_a_graveyard_is_not_a_card(self):
        """CR 111 -- a token is not a card, so it never "leaves" one.

        Driven through `drain_graveyard_exit_triggers` rather than
        `check_state_based_actions` on purpose: CR 704.5d removes a token from
        a graveyard as a state-based action, and SBAs run BEFORE the drain, so
        the full path physically cannot leave a token sitting in a graveyard
        for the observer to snapshot (the engine logs [TOKEN-SBA] and the card
        is gone). The drain is the function production calls; this is the only
        route that can set the state up, and it is decisive -- without the
        is_token skip the token enters the snapshot and its removal reports as
        a departure, minting a Zombie.
        """
        from mtg.triggers import drain_graveyard_exit_triggers
        engine, game = _engine_game()
        rick = game.players[0]
        rick.battlefield.append(_watcher('Tormod, the Desecrator'))
        token = _make_card('Saproling', type_line='Creature — Saproling')
        token.is_token = True
        rick.graveyard.append(token)
        drain_graveyard_exit_triggers(engine, game)

        rick.graveyard.remove(token)
        drain_graveyard_exit_triggers(engine, game)

        assert _tokens_named(rick, 'Zombie') == []

    def test_a_real_card_on_the_same_route_does_fire(self):
        """The control for the pin above: same route, is_token unset."""
        from mtg.triggers import drain_graveyard_exit_triggers
        engine, game = _engine_game()
        rick = game.players[0]
        rick.battlefield.append(_watcher('Tormod, the Desecrator'))
        card = _make_card('Real Card', type_line='Creature — Saproling')
        rick.graveyard.append(card)
        drain_graveyard_exit_triggers(engine, game)

        rick.graveyard.remove(card)
        drain_graveyard_exit_triggers(engine, game)

        assert len(_tokens_named(rick, 'Zombie')) == 1

    def test_a_fresh_game_seeds_without_firing(self):
        """!undo swaps in a restored GameState and save/load rebuilds one.

        A populated graveyard on an object the observer has never seen must
        not read as "the whole graveyard just left".
        """
        engine, game = _engine_game()
        rick = game.players[0]
        rick.battlefield.append(_watcher('Tormod, the Desecrator'))
        rick.graveyard.extend(_make_card(f'Old {i}') for i in range(5))

        _settle(engine, game)

        assert _tokens_named(rick, 'Zombie') == []

    def test_only_the_owners_own_watchers_fire(self):
        """"YOUR graveyard" is the watcher's controller, not any graveyard."""
        engine, game = _engine_game()
        rick, claude = game.players[0], game.players[1]
        claude.battlefield.append(_watcher('Tormod, the Desecrator'))
        card = _make_card('Fodder')
        rick.graveyard.append(card)
        _settle(engine, game)
        rick.graveyard.remove(card)
        _settle(engine, game)

        assert _tokens_named(claude, 'Zombie') == []
        assert _tokens_named(rick, 'Zombie') == []


# ===========================================================================
# Completeness -- the property that chose the design.
# ===========================================================================

class TestCompleteness:

    def test_a_raw_list_mutation_still_fires(self):
        """No engine call, no helper, no emit -- just a card gone.

        ~36-40 code paths across nine files take a card out of a graveyard,
        three independent counts of them disagreed, and six are invisible to
        the obvious `.graveyard.remove(` grep. This pin stands in for every
        one of them, INCLUDING sites added after today: a hand-wired emit at
        each mutation site could not pass it, which is the argument for the
        snapshot-differ detector stated as a test rather than a comment.
        """
        engine, game = _engine_game()
        rick = game.players[0]
        rick.battlefield.append(_watcher('Tormod, the Desecrator'))
        card = _make_card('Unwired Departure')
        rick.graveyard.append(card)
        _settle(engine, game)

        del rick.graveyard[0]          # the crudest possible exit path
        _settle(engine, game)

        assert len(_tokens_named(rick, 'Zombie')) == 1

    def test_a_graveyard_cast_style_move_fires(self):
        """The escape/flashback executor shape: graveyard -> hand."""
        engine, game = _engine_game()
        rick = game.players[0]
        rick.battlefield.append(_watcher('Tormod, the Desecrator'))
        card = _make_card('Cling to Dust', type_line='Instant')
        rick.graveyard.append(card)
        _settle(engine, game)

        rick.graveyard.remove(card)
        rick.hand.append(card)
        _settle(engine, game)

        assert len(_tokens_named(rick, 'Zombie')) == 1


# ===========================================================================
# Restrictions printed alongside the trigger.
# ===========================================================================

class TestPrintedRestrictions:

    def test_kishla_prints_both_restrictions(self):
        from mtg.triggers import graveyard_leave_watcher_clauses
        skimmer = _make_card('Kishla Skimmer', type_line='Creature — Bird',
                             oracle_text=_oracle('Kishla Skimmer'))
        clauses = graveyard_leave_watcher_clauses(skimmer)
        assert len(clauses) == 1
        assert clauses[0]['once_per_turn'] is True
        assert clauses[0]['during_your_turn'] is True

    def test_during_your_turn_does_not_fire_on_the_opponents_turn(self):
        """SYNTHETIC text -- Kishla's own effect ("draw a card") is not one
        this engine resolves deterministically, so the restriction is
        Kishla's and the effect is Tormod's. Unmodeled, this restriction
        over-fires on the opponent's turn, which is the forbidden direction.
        """
        engine, game = _engine_game()
        rick = game.players[0]
        rick.battlefield.append(_make_card(
            'Turn Bound Watcher', type_line='Enchantment',
            oracle_text=('Whenever one or more cards leave your graveyard '
                         'during your turn, create a tapped 2/2 black Zombie '
                         'creature token.')))
        card = _make_card('Fodder')
        rick.graveyard.append(card)
        _settle(engine, game)

        game.active_player_index = 1           # the opponent's turn
        rick.graveyard.remove(card)
        _settle(engine, game)
        assert _tokens_named(rick, 'Zombie') == []

        game.active_player_index = 0           # now the controller's own turn
        second = _make_card('More Fodder')
        rick.graveyard.append(second)
        _settle(engine, game)
        rick.graveyard.remove(second)
        _settle(engine, game)
        assert len(_tokens_named(rick, 'Zombie')) == 1

    def test_the_once_each_turn_cap_holds_within_a_turn(self):
        """SYNTHETIC, same reason as above -- Oasis "seek"s, which is
        unmodeled. Two separate departures in one turn, one token."""
        engine, game = _engine_game()
        rick = game.players[0]
        rick.battlefield.append(_make_card(
            'Capped Watcher', type_line='Enchantment',
            oracle_text=('Whenever a card leaves your graveyard, create a '
                         'tapped 2/2 black Zombie creature token. This '
                         'ability triggers only once each turn.')))
        first, second = _make_card('One'), _make_card('Two')
        rick.graveyard.extend([first, second])
        _settle(engine, game)

        rick.graveyard.remove(first)
        _settle(engine, game)
        rick.graveyard.remove(second)
        _settle(engine, game)

        assert len(_tokens_named(rick, 'Zombie')) == 1
