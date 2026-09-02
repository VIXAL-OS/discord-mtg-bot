"""Pins for the Reviewer-A half of the Aug 10, 2026 batch audit.

Corpus: 160 games at `strict=1 sha=6a30802`. Each class names the game its
finding came from. Companion file to tests/test_aug10_batch_audit.py.

Three of these fixes would have MISFIRED as first proposed — the independent
verification pass caught each one, and the pins encode the correction:
  * A1 anchored, because a loose "can't attack" check inverts Ghostly Prison.
  * A2 MOVED, not duplicated — add_effect has no dedup, so a second call
    quadruples a damage doubler.
  * A6 needs a life-aware gate, or the engine pays itself to death.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from conftest import _make_card, _make_game  # noqa: E402
from mtg.models import Card  # noqa: E402

_CACHE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'data', 'card_data_cache.json')


def _cache():
    with open(_CACHE_PATH, encoding='utf-8') as handle:
        return json.load(handle)


def _card_from_cache(name, **overrides):
    entry = _cache()[name.lower()]
    fields = dict(
        name=entry.get('name', name),
        oracle_text=entry.get('oracle_text', ''),
        type_line=entry.get('type_line', ''),
        mana_cost=entry.get('mana_cost', ''),
    )
    fields.update(overrides)
    return Card(**fields)


def _src(rel):
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, rel), encoding='utf-8') as handle:
        return handle.read()


# ---------------------------------------------------------------------------
# A1 — game_1536023907918680074. Qwen attacked out from under its own Glacial
# Chasm for 12 commander damage and won. can_attack only ever looked at
# attachments on the creature; can_attack_with only adds taxes and its scan
# skips the attacking player's own battlefield.
# ---------------------------------------------------------------------------

class TestControllerSideAttackLock:

    def _bear(self):
        return _make_card('Grizzly Bears', type_line='Creature - Bear',
                          power='2', toughness='2', summoning_sick=False)

    def test_glacial_chasm_stops_its_controllers_attackers(self):
        game = _make_game()
        player = game.players[0]
        chasm = _card_from_cache('Glacial Chasm')
        assert "creatures you control can't attack" in chasm.oracle_text.lower()
        bear = self._bear()
        player.battlefield.extend([chasm, bear])
        assert bear.can_attack(game) is False

    def test_opponent_taxes_do_not_lock_their_own_controller(self):
        """Ghostly Prison and Sphere of Safety both contain "can't attack" but
        tax the OPPONENT. A substring check would blank their controller's
        attacks instead — the exact inversion this codebase has shipped
        repeatedly (the 'creature' in 'noncreature' family)."""
        for name in ('Ghostly Prison', 'Sphere of Safety'):
            game = _make_game()
            player = game.players[0]
            prison = _card_from_cache(name)
            assert "can't attack" in prison.oracle_text.lower()
            bear = self._bear()
            player.battlefield.extend([prison, bear])
            assert bear.can_attack(game) is True, (
                f'{name} must not lock its own controller')

    def test_the_lock_matches_only_glacial_chasm_in_the_whole_cache(self):
        from mtg.models import _CONTROLLER_ATTACK_LOCK

        matched = sorted(
            key for key, entry in _cache().items()
            if _CONTROLLER_ATTACK_LOCK.search(entry.get('oracle_text') or ''))
        assert matched == ['glacial chasm'], (
            f'the anchored lock must stay narrow; it now matches {matched}')

    def test_an_unrelated_board_still_attacks(self):
        game = _make_game()
        player = game.players[0]
        bear = self._bear()
        player.battlefield.append(bear)
        assert bear.can_attack(game) is True


# ---------------------------------------------------------------------------
# A2 — game_1536023907918680074. Warstorm Surge dealt an UNDOUBLED 5 and
# Purphoros an UNDOUBLED 2 as Gisela entered, because the registration sat 40
# lines BELOW the PERMANENT_ENTERED emit whose subscriber runs those watchers.
# CR 604.3 / 611.2 (a static functions from the moment it is on the
# battlefield) vs CR 603.3d (its triggers resolve strictly later).
# ---------------------------------------------------------------------------

class TestReplacementRegistrationOrdering:

    def test_registration_precedes_the_entry_emit(self):
        src = _src('mtg/spells.py')
        emit = src.find('events.emit(events.PERMANENT_ENTERED, game, card=card,\n'
                        '                        controller=player, via="cast"')
        assert emit != -1, 'the cast-path emit moved'
        window = src[max(0, emit - 1800):emit]
        assert 'game.register_replacement_effects(card, player.name)' in window, (
            "a permanent's replacement effects must be live before its entry "
            'fires other permanents watchers')

    def test_add_effect_has_no_dedup_so_the_guard_is_load_bearing(self):
        """Behavioural half: prove the hazard is real rather than asserting it
        in a comment. Registering the same source twice genuinely doubles the
        stored effects, which is why the fallback MUST be guarded — an
        unguarded second call turns Gisela into a x4 multiplier."""
        game = _make_game()
        player = game.players[0]
        gisela = _card_from_cache('Gisela, Blade of Goldnight')
        player.battlefield.append(gisela)
        # `replacement_engine` is the lazy property production uses; reading
        # the raw field gives None and would make this pin skip, i.e. be a
        # comment. Touch the property so the check actually runs.
        engine = game.replacement_engine
        assert engine is not None, 'the replacement engine must be available'
        game.register_replacement_effects(gisela, player.name)
        once = len(engine.effects)
        assert once, 'Gisela must register at least one replacement effect'
        game.register_replacement_effects(gisela, player.name)
        assert len(engine.effects) > once, (
            'add_effect is expected to be a bare append with no dedup; if this '
            'ever starts deduping, the guard below can be relaxed')

    def test_the_fallback_registration_is_guarded(self):
        """Structural, deliberately: driving the whole cast funnel twice to
        observe a duplicate registration is disproportionate. Anchored on the
        exact condition, so removing the guard fails this."""
        src = _src('mtg/spells.py')
        calls = src.count('game.register_replacement_effects(card, player.name)')
        assert calls == 2, (
            f'expected the moved call plus one guarded fallback, found {calls}')
        assert ("if card in player.battlefield and not getattr("
                "card, '_statics_registered_on_entry', False):") in src, (
            'the surviving fallback must skip a permanent already registered '
            'at entry, or a damage doubler is applied twice')

    def test_flag_clears_on_battlefield_exit(self):
        """Otherwise a flickered permanent never re-registers (CR 400.7)."""
        card = Card(name='Gisela, Blade of Goldnight',
                    type_line='Legendary Creature - Angel')
        card._statics_registered_on_entry = True
        card.reset_battlefield_state()
        assert card._statics_registered_on_entry is False


# ---------------------------------------------------------------------------
# A6 — game_1536023907918680074. "Cumulative upkeep-Pay 2 life" has no mana
# symbols, so both sums came back 0, the `else age` default took over, and the
# cost silently became `age` GENERIC MANA. 12 life owed across three upkeeps,
# 0 paid. CR 702.24a.
# ---------------------------------------------------------------------------

class TestCumulativeUpkeepLifeCost:

    def _run_upkeep(self, life, age=2):
        from mtg.rules_engine import RulesEngine
        from mtg.triggers import _check_upkeep_triggers_sync

        game = _make_game()
        player = game.players[0]
        player.life = life
        game.active_player_index = 0
        chasm = _card_from_cache('Glacial Chasm')
        chasm.counters = {'age': age}
        # A real mana base, so a mana-cost regression would be payable and
        # the test would notice the difference rather than passing because
        # nothing could be paid at all.
        for _ in range(6):
            player.battlefield.append(
                _make_card('Plains', type_line='Basic Land - Plains',
                           oracle_text='{T}: Add {W}.'))
        player.battlefield.append(chasm)
        _check_upkeep_triggers_sync(RulesEngine(None), game)
        return player, chasm

    def test_life_is_actually_paid(self):
        player, chasm = self._run_upkeep(40)
        assert player.life < 40, (
            'a "Pay 2 life" cumulative upkeep must cost LIFE — it used to '
            'degrade to `age` generic mana and tap lands instead')
        assert chasm in player.battlefield, 'a paid upkeep keeps the permanent'

    def test_low_life_declines_instead_of_self_killing(self):
        """pay_threshold is 999 for the Chasm, so bolting a life branch onto
        it without a life-aware gate converts an under-charge into a
        self-inflicted loss: 2, 4, 6, 8, 10 life until the controller dies."""
        player, chasm = self._run_upkeep(5)
        assert player.life == 5, 'must not pay itself to death'
        assert chasm not in player.battlefield, (
            'an unpaid cumulative upkeep sacrifices the permanent')


# ---------------------------------------------------------------------------
# A7 + A8 — game_1536023731808509984. One card, one fix. The attack entry
# minted a phantom tapped-and-attacking token (three attackers resolved from
# two declared) AND pumped every OTHER Goblin +1/+1 instead of the source
# +1/+0 per other attacking Goblin. The token belongs to beginning-of-combat,
# where it also stops costing a Tier-3 call and arrives in time to use haste.
# ---------------------------------------------------------------------------

class TestGoblinRabblemaster:

    def _attack_actions(self, other_goblins):
        from rules.effect_templates import build_game_context, get_effect_library

        game = _make_game()
        player, opponent = game.players[0], game.players[1]
        oracle = _cache()['goblin rabblemaster']['oracle_text']
        rabble = _make_card('Goblin Rabblemaster',
                            type_line='Creature - Goblin',
                            power='2', toughness='2', oracle_text=oracle)
        player.battlefield.append(rabble)
        attackers = [rabble.id]
        for index in range(other_goblins):
            token = _make_card(f'Goblin {index}', type_line='Creature - Goblin',
                               power='1', toughness='1')
            player.battlefield.append(token)
            attackers.append(token.id)
        game.attackers = attackers
        ctx = build_game_context(game, player, opponent, card=rabble)
        ctx.update({'attacking_name': 'Goblin Rabblemaster',
                    '_attacking_creature': rabble, '_game': game})
        actions, _desc = get_effect_library().resolve_attack_trigger(
            'Goblin Rabblemaster', oracle, 'Goblin Rabblemaster', 2,
            player.name, opponent.name, game_context=ctx)
        return actions or []

    def test_attack_trigger_creates_no_token(self):
        actions = self._attack_actions(2)
        assert not any(a.get('action') == 'create_token' for a in actions), (
            'the token belongs to the BEGINNING OF COMBAT ability; minting it '
            'on attack produced a phantom extra attacker every combat')

    def test_pump_scales_with_other_attacking_goblins(self):
        two = self._attack_actions(2)
        one = self._attack_actions(1)
        pump_two = next(a for a in two if a.get('action') == 'pump_all_creatures')
        pump_one = next(a for a in one if a.get('action') == 'pump_all_creatures')
        assert (pump_two['power'], pump_two['toughness']) == (2, 0)
        assert (pump_one['power'], pump_one['toughness']) == (1, 0), (
            'the bonus is +1/+0 for EACH other attacking Goblin, not a flat '
            '+1/+1 — a fixture with one count cannot tell those apart')

    def test_pump_targets_the_source_only(self):
        pump = next(a for a in self._attack_actions(2)
                    if a.get('action') == 'pump_all_creatures')
        assert pump.get('include_name') == 'Goblin Rabblemaster'
        assert 'exclude' not in pump, (
            'the old entry excluded the source and pumped everything else')

    def test_pump_uses_vocabulary_the_handler_actually_reads(self):
        """A plausible invented key is silently ignored, which here would pump
        the WHOLE team. include_name / include_id are the real ones."""
        handler = _src('mtg/actions.py')
        index = handler.find('elif action_type == "pump_all_creatures":')
        assert index != -1
        body = handler[index:index + 2600]
        pump = next(a for a in self._attack_actions(2)
                    if a.get('action') == 'pump_all_creatures')
        for key in pump:
            if key in ('action', 'player', 'power', 'toughness', 'source'):
                continue
            assert f'"{key}"' in body, (
                f'pump_all_creatures never reads {key!r} — the action would be '
                f'a silent no-op')

    def test_no_other_goblins_is_a_handled_no_op(self):
        actions = self._attack_actions(0)
        assert actions and actions[0]['action'] == 'no_action'

    def test_token_half_resolves_at_beginning_of_combat_with_haste(self):
        from rules.effect_templates import get_effect_library

        oracle = _cache()['goblin rabblemaster']['oracle_text']
        actions, _desc = get_effect_library().resolve_etb(
            'Goblin Rabblemaster', oracle, 'Rick', 'Qwen',
            event_type='beginning_combat')
        assert actions, 'the token half must resolve inline, not via Tier 3'
        token = next(a for a in actions if a.get('action') == 'create_token')
        assert 'Haste' in (token.get('keywords') or []), (
            'the printed token has haste precisely so it can attack that combat')
        assert not token.get('attacking') and not token.get('tapped')

    def test_bare_name_key_is_not_registered(self):
        """_NAME_KEYED_EVENT_TYPES covers beginning_combat AND etb, so a bare
        key would also mint a Goblin on Rabblemaster's own ETB — the re-fire
        class the suffix convention exists to prevent."""
        from rules.effect_templates import get_effect_library

        library = get_effect_library()
        assert 'goblin rabblemaster beginningcombat' in library._card_templates
        assert 'goblin rabblemaster' not in library._card_templates


# ---------------------------------------------------------------------------
# A4 / A5 / A9 — silent drops. game_1536023936116981932 (equipment triggers,
# protection) and game_1536023747067252756 (post-loss trigger).
# ---------------------------------------------------------------------------

class TestSilentDropsClosed:

    def test_untemplated_equipment_trigger_reaches_the_queue(self):
        """Sword of Light and Shadow connected three times with zero
        [COMBAT-TRIGGER] and zero [COMBAT-TRIGGER-UNHANDLED] — the branch had
        no else at all, so no audit grep could ever find the loss."""
        from mtg.combat import _equipment_charge_claims

        sword = _card_from_cache('Sword of Light and Shadow')
        assert 'deals combat damage to a player' in sword.oracle_text.lower()
        assert _equipment_charge_claims(sword) is False

    def test_jitte_stays_out_of_the_queue(self):
        """Its charge counters are already applied by the deterministic
        counter path; queueing would double-fire and burn a Tier-3 call."""
        from mtg.combat import _equipment_charge_claims

        assert _equipment_charge_claims(_card_from_cache("Umezawa's Jitte")) is True

    def test_equipment_branch_has_an_else(self):
        src = _src('mtg/combat.py')
        index = src.find("'equipped creature deals combat damage to a player'")
        assert index != -1
        # Sep 1 2026: the window grew past 3000 chars once the [] = handled
        # no-op branch (and its comment) landed between the phrase and the
        # queue call — slice to the enclosing dispatch instead of a fixed
        # window the next comment will outgrow again.
        end = src.find('# Crash barrier mirroring the sibling attacker-loop catch',
                       index)
        assert end != -1, 'the equipment dispatch lost its crash barrier'
        assert 'queue_unhandled_combat_damage' in src[index:end], (
            'the untemplated case must queue like its sibling watcher loop')

    def test_protection_keeps_every_printed_colour(self):
        from rules.targeting_helpers import _card_to_targetable

        sword = _card_from_cache('Sword of Light and Shadow')
        assert 'protection from white and from black' in sword.oracle_text.lower()
        target = _card_to_targetable(sword, 'Rick')
        colours = set()
        for ability in (getattr(target, 'protection', None) or []):
            colours |= set(getattr(ability, 'from_colors', set()) or set())
        assert colours == {'W', 'B'}, (
            f'the "and from X" template dropped the second colour; got {colours}')

    def test_single_colour_protection_still_parses(self):
        from rules.targeting_helpers import _card_to_targetable

        card = Card(name='Test', type_line='Creature - Bear',
                    oracle_text='Protection from red')
        target = _card_to_targetable(card, 'Rick')
        colours = set()
        for ability in (getattr(target, 'protection', None) or []):
            colours |= set(getattr(ability, 'from_colors', set()) or set())
        assert colours == {'R'}

    def test_etb_batch_loop_stops_once_the_game_ends(self):
        """CR 104.2a / 704.3 — Terror of the Peaks took Qwen to 0 and Impact
        Tremors then dealt 1 more and re-announced the loss."""
        src = _src('mtg/triggers.py')
        loop = src.find(
            'for card, _ctrl_player, _ctrl_idx in reversed(_etb_collected):')
        assert loop != -1
        body = src[loop:loop + 900]
        assert "getattr(game, 'ended', False)" in body and 'break' in body
