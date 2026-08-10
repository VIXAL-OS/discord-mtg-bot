"""Pins for the six items deferred out of the Aug 10, 2026 batch audit.

Each was deferred with a recorded mechanism rather than a guess, and in three
cases the recorded mechanism is why the obvious fix would have failed:
  * A3 — the aura helper's kind alternation cannot see "Gate", a land SUBTYPE,
    so lifting it verbatim would have been a literal no-op.
  * A5 — the granted-keyword whitelist holds bare tokens, so 'protection'
    there would carry no colour and would be dropped by the next recalc; the
    load-bearing seam is _card_to_targetable's prot_list.
  * C4 — an Aura with no legal object does NOT enter and get swept by SBA; it
    stays in the library (CR 303.4h).
"""
import json
import os
import re
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from conftest import _make_card, _make_game  # noqa: E402
from mtg.models import Card  # noqa: E402

_CACHE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'data', 'card_data_cache.json')


def _cache():
    with open(_CACHE_PATH, encoding='utf-8') as handle:
        return json.load(handle)


def _oracle(name):
    return _cache()[name.lower()]['oracle_text']


def _attach(game, player, attachment, bearer):
    player.battlefield.extend([attachment, bearer])
    bearer.attachments = [attachment.id]
    attachment.attached_to = bearer.id


# ---------------------------------------------------------------------------
# A3 — Glaive of the Guildpact granted a flat +1/+0; "for each Gate you
# control" was discarded. The equipment P/T reader had NO multiplier handling,
# and the aura sibling's helper alternates on card KINDS only.
# ---------------------------------------------------------------------------

class TestForEachMultiplier:

    def _power_with_gates(self, gates):
        game = _make_game()
        player = game.players[0]
        glaive = _make_card('Glaive of the Guildpact',
                            type_line='Artifact - Equipment',
                            oracle_text=_oracle('Glaive of the Guildpact'))
        bearer = _make_card('Sram', type_line='Creature - Human',
                            power='2', toughness='2')
        _attach(game, player, glaive, bearer)
        for i in range(gates):
            player.battlefield.append(
                _make_card(f'Guildgate{i}', type_line='Land — Gate'))
        player.battlefield.append(
            _make_card('Plains', type_line='Basic Land — Plains'))
        game.recalculate_power_toughness()
        return bearer.get_effective_power(game)

    def test_zero_gates_is_zero_bonus(self):
        """The live case: the only deck running Glaive has no Gates at all, so
        the correct bonus is +0/+0 and the flat +1 was a phantom."""
        assert self._power_with_gates(0) == 2

    def test_bonus_scales_with_gate_count(self):
        """Two counts, because a single fixture cannot tell 'scales' from
        'flat +1' apart."""
        assert self._power_with_gates(1) == 3
        assert self._power_with_gates(3) == 5

    def test_subtype_counting_ignores_non_gates(self):
        """Gate is a land SUBTYPE — matched after the em-dash, so a plain
        Plains must not count even though both are Lands."""
        assert self._power_with_gates(2) == 4

    def test_hyphen_separator_is_accepted_too(self):
        """Real Scryfall data uses the em dash, but hand-built cards use
        " - " — and a subtype scan that silently sees nothing looks exactly
        like "no Gates in play"."""
        from mtg.models import _for_each_you_control_count

        game = _make_game()
        player = game.players[0]
        player.battlefield.append(_make_card('G1', type_line='Land - Gate'))
        player.battlefield.append(_make_card('G2', type_line='Land — Gate'))
        assert _for_each_you_control_count(game, player, 'gate') == 2

    def test_card_kind_multipliers_still_work(self):
        """Ethereal Armor is the aura-side control: the shared helper must not
        regress the four card kinds it already handled."""
        game = _make_game()
        player = game.players[0]
        armor = _make_card('Ethereal Armor', type_line='Enchantment - Aura',
                           oracle_text=_oracle('Ethereal Armor'))
        bearer = _make_card('Bear', type_line='Creature - Bear',
                            power='2', toughness='2')
        _attach(game, player, armor, bearer)
        player.battlefield.append(
            _make_card('Other Enchantment', type_line='Enchantment'))
        game.recalculate_power_toughness()
        # armor itself + the other enchantment = 2
        assert bearer.get_effective_power(game) == 4

    def test_restrictive_clause_declines_rather_than_overcounting(self):
        """Sage's Reverie says "for each Aura you control THAT'S ATTACHED to a
        creature" and Stoneforge Masterwork "that SHARES a creature type".
        Widening to arbitrary words newly reaches both; counting them without
        the restriction is an OVER-count, which is worse than the flat bonus
        they get today, so the parser must refuse."""
        from mtg.models import _FOR_EACH_YOU_CONTROL

        for name in ("Sage's Reverie", 'Stoneforge Masterwork'):
            matches = [m for m in _FOR_EACH_YOU_CONTROL.finditer(_oracle(name))
                       if m.group(3)]
            assert not matches, (
                f'{name} carries an unmodelled restrictive clause and must not '
                f'be multiplied')

    def test_both_readers_share_one_implementation(self):
        """The equipment reader had no multiplier at all while the aura reader
        did; the point of the fix is that they cannot drift again."""
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, 'mtg/models.py'), encoding='utf-8') as handle:
            src = handle.read()
        assert src.count('_for_each_you_control_count(') >= 3, (
            'the helper must be defined once and called from BOTH readers')


# ---------------------------------------------------------------------------
# A5 remainder — equipment/aura GRANTED protection was never modelled, so
# three white Auras (including an opponent's Pacifism) were legally cast onto
# a creature equipped with Sword of Light and Shadow.
# ---------------------------------------------------------------------------

class TestGrantedProtection:

    def _protections(self, target):
        found = set()
        for ability in (getattr(target, 'protection', None) or []):
            found |= set(getattr(ability, 'from_colors', set()) or set())
            found |= set(getattr(ability, 'from_types', set()) or set())
        return found

    def _bearer_with(self, attachment_name, type_line):
        from rules.targeting_helpers import _card_to_targetable

        game = _make_game()
        player = game.players[0]
        attachment = _make_card(attachment_name, type_line=type_line,
                                oracle_text=_oracle(attachment_name))
        bearer = _make_card('Danitha', type_line='Creature - Human Knight',
                            power='2', toughness='2')
        _attach(game, player, attachment, bearer)
        return self._protections(_card_to_targetable(bearer, player.name, game=game))

    def test_equipment_grants_both_colours(self):
        assert self._bearer_with('Sword of Light and Shadow',
                                 'Artifact - Equipment') == {'W', 'B'}

    def test_a_second_sword_grants_its_own_pair(self):
        assert self._bearer_with('Sword of Feast and Famine',
                                 'Artifact - Equipment') == {'B', 'G'}

    def test_aura_grants_type_protection(self):
        assert self._bearer_with('Holy Mantle', 'Enchantment - Aura') == {'creature'}

    def test_equipment_with_its_own_protection_does_not_grant_it(self):
        """Scoped to sentences BEGINNING "equipped/enchanted creature", so an
        Equipment that has protection ITSELF keeps it to itself."""
        from rules.targeting_helpers import _card_to_targetable

        game = _make_game()
        player = game.players[0]
        blade = _make_card('Weird Blade', type_line='Artifact - Equipment',
                           oracle_text=('This Equipment has protection from red.\n'
                                        'Equipped creature gets +1/+1.'))
        bearer = _make_card('Bear', type_line='Creature - Bear',
                            power='2', toughness='2')
        _attach(game, player, blade, bearer)
        assert self._protections(
            _card_to_targetable(bearer, player.name, game=game)) == set()

    def test_printed_protection_keeps_every_colour(self):
        """The regex half: "protection from white and from BLACK" used to
        capture 'white and from' and lose the second colour."""
        from rules.targeting_helpers import _parse_protection

        abilities = _parse_protection(_oracle('Akroma, Angel of Wrath'))
        colours = set()
        for ability in abilities:
            colours |= set(ability.from_colors or set())
        assert colours == {'B', 'R'}

    def test_no_game_means_no_grant_lookup(self):
        """Graveyard/stack callers pass no game; the grant scan must be inert
        rather than raising."""
        from rules.targeting_helpers import _card_to_targetable

        card = Card(name='Bear', type_line='Creature - Bear')
        card.attachments = ['nonexistent']
        assert _card_to_targetable(card, 'Rick') is not None


# ---------------------------------------------------------------------------
# C4 — Chaos Warp put a revealed Aura onto the battlefield unattached and
# called neither the noncast entry funnel nor the bus.
# ---------------------------------------------------------------------------

class TestChaosWarpEntry:

    def test_aura_with_no_legal_object_stays_in_the_library(self):
        """CR 303.4h. Entering unattached and being swept by the CR 704.5m
        AURA_INVALID check is the engine tidying a state it should never have
        produced."""
        from mtg.helpers import find_aura_attach_target

        game = _make_game()
        player = game.players[0]
        umbra = _make_card('Bear Umbra', type_line='Enchantment - Aura',
                           oracle_text=_oracle('Bear Umbra'))
        assert find_aura_attach_target(game, player, umbra) is None

    def test_aura_attaches_when_a_legal_object_exists(self):
        from mtg.helpers import find_aura_attach_target

        game = _make_game()
        player = game.players[0]
        umbra = _make_card('Bear Umbra', type_line='Enchantment - Aura',
                           oracle_text=_oracle('Bear Umbra'))
        small = _make_card('Small', type_line='Creature - Bear',
                           power='1', toughness='1')
        big = _make_card('Big', type_line='Creature - Giant',
                         power='5', toughness='5')
        player.battlefield.extend([small, big])
        assert find_aura_attach_target(game, player, umbra) is big

    def _chaos_warp(self, revealed, victim_owner_creatures=()):
        """Drive the REAL Chaos Warp branch.

        The first version of these two pins searched mtg/spells.py for the
        call's NAME and both SURVIVED their mutants — one kept the string in
        an unused import while neutering the call, the other changed
        behaviour the source text never mentioned. Structural pins a mutant
        can dodge are comments.
        """
        from mtg.rules_engine import RulesEngine
        from mtg.spells import resolve_special_effects

        game = _make_game()
        caster, victim = game.players[0], game.players[1]
        rules = RulesEngine(None)
        rules.engine_ref = None
        game._rules_engine = rules

        class _Engine:
            # engine_ref must be non-None or _fire_noncast_battlefield_entry
            # returns early; _handle_etb_triggers is the downstream WATCHER
            # scan (other permanents seeing the entry), stubbed because these
            # pins are about the entering card's OWN handling.
            rules = None

            def _handle_etb_triggers(self, *_a, **_k):
                return []

        engine = _Engine()
        engine.rules = rules
        rules.engine_ref = engine

        target = _make_card('Doomed Permanent', type_line='Artifact')
        victim.battlefield.append(target)
        for creature in victim_owner_creatures:
            victim.battlefield.append(creature)
        victim.library.insert(0, revealed)

        warp = _make_card('Chaos Warp', type_line='Instant',
                          oracle_text=_oracle('Chaos Warp'))
        # The card genuinely shuffles the target in BEFORE revealing, so the
        # top card is randomised. Neutralise only the shuffle so the reveal is
        # the card under test; everything else runs for real.
        with mock.patch('random.shuffle', lambda seq: None):
            messages = resolve_special_effects(engine, game, caster, warp,
                                               target=target)
        return game, victim, messages

    def test_unattachable_aura_is_not_put_onto_the_battlefield(self):
        """CR 303.4h — with no legal object the Aura stays in the library.
        Entering unattached and being swept by the CR 704.5m AURA_INVALID
        check is the engine tidying a state it should never have made."""
        umbra = _make_card('Bear Umbra', type_line='Enchantment — Aura',
                           oracle_text=_oracle('Bear Umbra'))
        game, victim, messages = self._chaos_warp(umbra)
        assert umbra not in victim.battlefield, (
            'an Aura with no legal object must not enter the battlefield')
        assert umbra in victim.library
        assert any('library' in m for m in messages)

    def test_attachable_aura_enters_attached(self):
        host = _make_card('Host', type_line='Creature — Bear',
                          power='2', toughness='2')
        umbra = _make_card('Bear Umbra', type_line='Enchantment — Aura',
                           oracle_text=_oracle('Bear Umbra'))
        game, victim, _messages = self._chaos_warp(umbra, [host])
        assert umbra in victim.battlefield
        assert umbra.attached_to == host.id
        assert umbra.id in host.attachments

    def test_revealed_creature_resolves_its_own_etb(self):
        """The bus emit and the noncast FUNNEL are separate halves — a mutant
        that neuters only the funnel leaves the bus pin passing. Assert on the
        funnel's own observable output: the entering card's self-ETB."""
        drifter = _make_card('Test Drifter', type_line='Creature — Elemental',
                             power='2', toughness='2',
                             oracle_text='When Test Drifter enters, draw a card.')
        game, victim, _messages = self._chaos_warp(
            drifter, [_make_card('Filler', type_line='Creature — Bear',
                                 power='1', toughness='1')])
        assert drifter in victim.battlefield
        assert len(victim.hand) == 1, (
            "the revealed permanent's own ETB must resolve — the block used "
            'to call neither the noncast funnel nor the bus')

    def test_revealed_creature_fires_the_entry_funnel(self):
        """A Chaos-Warped CREATURE used to enter with no self-ETB, no
        Soul-Warden-class watchers and no PERMANENT_ENTERED. Assert on the
        OBSERVABLE consequence: a watcher already on the battlefield sees it."""
        from mtg import events

        warden = _make_card('Soul Warden', type_line='Creature — Human Cleric',
                            power='1', toughness='1')
        newcomer = _make_card('Grizzly Bears', type_line='Creature — Bear',
                              power='2', toughness='2')
        seen = []

        def _spy(_game, **kw):
            seen.append(kw.get('card'))

        events.subscribe(events.PERMANENT_ENTERED, _spy)
        try:
            game, victim, _messages = self._chaos_warp(newcomer, [warden])
        finally:
            events.unsubscribe(events.PERMANENT_ENTERED, _spy)
        assert newcomer in victim.battlefield
        assert newcomer in seen, (
            'the entry must reach the PERMANENT_ENTERED bus, or every '
            'creature-enters watcher misses it')


# ---------------------------------------------------------------------------
# C5 — Vivien -2 rendered the full impulse-exile text and then drew a card.
# The impulse vocabulary exists, so the real mechanic is modelled now.
# ---------------------------------------------------------------------------

class TestImpulseApproximationsReplaced:

    def _actions(self, key_card, key_snippet):
        from rules.effect_templates import get_effect_library

        template = get_effect_library()._pw_ability_templates[(key_card, key_snippet)]
        return template.action_generator('Rick', 'Qwen', {})

    def test_vivien_minus_two_exiles_face_down_and_playable(self):
        actions = self._actions('vivien, champion of the wilds',
                                'look at the top three')
        assert not any(a.get('action') == 'draw_cards' for a in actions), (
            'the printed ability exiles a card face down; drawing it gives an '
            'unrestricted card with no cast restriction')
        exile = next(a for a in actions if a['action'] == 'exile_top_of_library')
        assert exile.get('playable') is True
        assert exile.get('face_down') is True

    def test_face_down_exile_does_not_name_the_card(self):
        from mtg.rules_engine import RulesEngine

        game = _make_game()
        player = game.players[0]
        secret = _make_card('Craterhoof Behemoth', type_line='Creature - Beast')
        player.library.insert(0, secret)
        msg = RulesEngine(None)._execute_action_on_state(game, {
            'action': 'exile_top_of_library', 'player': player.name,
            'count': 1, 'playable': True, 'face_down': True})
        assert 'Craterhoof' not in (msg or ''), 'face-down exile must not leak the name'
        assert secret in player.exile
        assert secret.id in player.playable_from_exile

    def test_face_up_exile_still_names_the_card(self):
        """Ragavan-class exiles are public; the opt-in must not hide those."""
        from mtg.rules_engine import RulesEngine

        game = _make_game()
        player = game.players[0]
        player.library.insert(0, _make_card('Lightning Bolt', type_line='Instant'))
        msg = RulesEngine(None)._execute_action_on_state(game, {
            'action': 'exile_top_of_library', 'player': player.name, 'count': 1})
        assert 'Lightning Bolt' in (msg or '')


# ---------------------------------------------------------------------------
# C6 — three display defects. B4 — the graveyard-activation retry storm.
# ---------------------------------------------------------------------------

class TestDisplayAndTeaching:

    def test_fizzle_message_names_the_target_once(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, 'mtg/spells.py'), encoding='utf-8') as handle:
            src = handle.read()
        assert "ETB fizzles — {target_name} {reason}" not in src, (
            'the reason string already begins with the target name, so this '
            'printed it twice')
        assert '_friendly_fizzle_reason' in src

    def test_internal_precondition_strings_are_suppressed(self):
        """Blizzard Brawl's gate string reached Discord verbatim because the
        marker list knew only four phrases."""
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, 'mtg/spells.py'), encoding='utf-8') as handle:
            src = handle.read()
        index = src.find('_is_internal = any(marker in _r_lower')
        assert index != -1
        window = src[index:index + 500]
        for marker in ('"need a "', '"requires a "', '"missing "'):
            assert marker in window, f'{marker} must be treated as a diagnostic'

    def test_loyalty_line_is_attached_to_its_header(self):
        """It used to be its own list element, and autoplay sends each element
        as a separate Discord message — so it posted unattributed."""
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, 'rules/planeswalker.py'), encoding='utf-8') as handle:
            src = handle.read()
        assert re.search(r'messages = \[\s*header_line\s*\n\s*\+ f"\\n   Loyalty:', src), (
            'the loyalty line must be folded into the header string')

    def test_graveyard_activation_hint_names_the_action_type(self):
        from mtg.ai_turn import _wrong_zone_hint

        game = _make_game()
        player = game.players[0]
        zombie = _make_card('Dregscape Zombie', type_line='Creature - Zombie',
                            oracle_text=_oracle('Dregscape Zombie'))
        player.graveyard.append(zombie)
        hint = _wrong_zone_hint(game, player, 'Dregscape Zombie')
        assert 'graveyard_activate' in hint and 'unearth' in hint, (
            'the model re-proposed `cast` 24 times because nothing told it '
            'which action type to use')
        assert 'GRAVEYARD' in hint

    def test_companion_hint_names_the_action_type(self):
        from mtg.ai_turn import _wrong_zone_hint

        game = _make_game()
        player = game.players[0]
        player.companion_zone.append(
            _make_card('Lurrus of the Dream-Den',
                       type_line='Legendary Creature - Cat Nightmare',
                       oracle_text=_oracle('Lurrus of the Dream-Den')))
        hint = _wrong_zone_hint(game, player, 'Lurrus of the Dream-Den')
        assert '"type": "companion"' in hint

    def test_absent_card_falls_back_to_the_plain_message(self):
        from mtg.ai_turn import _wrong_zone_hint

        game = _make_game()
        assert _wrong_zone_hint(game, game.players[0], 'Black Lotus') == ''

    def test_the_real_rejection_path_consults_the_hint(self):
        """BEHAVIOURAL. The first version tested _wrong_zone_hint directly and
        SURVIVED a mutant that stopped _get_action_error calling it — a helper
        pinned only through direct calls is not pinned into production."""
        from mtg.ai_turn import _get_action_error
        from mtg.rules_engine import RulesEngine

        game = _make_game()
        player = game.players[0]
        player.graveyard.append(
            _make_card('Dregscape Zombie', type_line='Creature - Zombie',
                       oracle_text=_oracle('Dregscape Zombie')))
        engine = RulesEngine(None)
        message = _get_action_error(
            engine, game, 0, {'type': 'cast', 'card': 'Dregscape Zombie'})
        assert 'graveyard_activate' in message, (
            f'the rejection must teach the right action type, got {message!r}')

    def test_the_real_rejection_path_still_says_not_in_hand_otherwise(self):
        from mtg.ai_turn import _get_action_error
        from mtg.rules_engine import RulesEngine

        game = _make_game()
        message = _get_action_error(
            RulesEngine(None), game, 0, {'type': 'cast', 'card': 'Black Lotus'})
        assert 'not found in hand' in message

    def test_hint_reads_the_real_companion_field(self):
        """`companion_zone` is a declared Player field; a getattr against the
        wrong name would make the hint silently never fire."""
        game = _make_game()
        assert hasattr(game.players[0], 'companion_zone')
