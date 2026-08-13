"""Aug 13 sixth-confirmation audit: combat-trigger coverage fixes.

These pins exercise the production helpers at their real event boundaries,
with a negative control for every new gate.  They intentionally use local
Cards rather than an LLM or a live log.
"""

from mtg.rules_engine import RulesEngine
from mtg.triggers import (
    _attack_keywords_with_attached_grants,
    _check_attack_triggers_sync,
    _check_end_step_triggers_sync,
    _check_meld_completion,
    check_block_triggers,
    drain_end_of_combat_destructions,
)


def _engine(game):
    rules = RulesEngine(None)
    game._rules_engine = rules
    return rules


class TestCombatStateAnthem:
    def test_instigator_gang_buffs_attackers_and_not_nonattackers(
            self, make_game, make_card):
        game = make_game()
        rick, _ = game.players
        gang = make_card(
            'Instigator Gang', power='2', toughness='3',
            oracle_text=('Attacking creatures you control get +1/+0.\n'
                         'At the beginning of each upkeep, if no spells were '
                         'cast last turn, transform this creature.'))
        attacker = make_card('Attacker', power='2', toughness='2')
        bystander = make_card('Bystander', power='2', toughness='2')
        attacker.attacking = True
        rick.battlefield.extend([gang, attacker, bystander])

        assert attacker.get_effective_power(game) == 3
        assert attacker.get_effective_toughness(game) == 2
        assert bystander.get_effective_power(game) == 2

    def test_wildblood_pack_uses_its_larger_attacking_bonus(
            self, make_game, make_card):
        game = make_game()
        rick, _ = game.players
        pack = make_card('Wildblood Pack', power='5', toughness='5',
                         oracle_text='Attacking creatures you control get +3/+0.')
        attacker = make_card('Attacker', power='2', toughness='2')
        attacker.attacking = True
        rick.battlefield.extend([pack, attacker])
        assert attacker.get_effective_power(game) == 5


class TestGiselaMeld:
    def _halves(self, make_game, make_card):
        game = make_game()
        rick, claude = game.players
        gisela = make_card(
            'Gisela, the Broken Blade', power='4', toughness='3', owner_index=0,
            oracle_text=('Flying, first strike, lifelink\nAt the beginning of '
                         'your end step, if you both own and control Gisela and '
                         'a creature named Bruna, the Fading Light, exile them, '
                         'then meld them into Brisela, Voice of Nightmares.'))
        bruna = make_card('Bruna, the Fading Light', power='5', toughness='7',
                          owner_index=0)
        rick.battlefield.extend([gisela, bruna])
        return game, rick, claude, gisela, bruna

    def test_pair_waits_for_controller_end_step_and_has_real_brisela_data(
            self, make_game, make_card):
        game, rick, claude, gisela, bruna = self._halves(make_game, make_card)
        rules = _engine(game)

        assert _check_meld_completion(game, rick, bruna) == []
        assert gisela in rick.battlefield and bruna in rick.battlefield
        game.active_player_index = 1
        _check_end_step_triggers_sync(rules, game)
        assert gisela in rick.battlefield, 'opponent end step must not meld'

        game.active_player_index = 0
        messages, _ = _check_end_step_triggers_sync(rules, game)
        brisela = next(c for c in rick.battlefield
                       if c.name == 'Brisela, Voice of Nightmares')
        assert messages and gisela in rick.exile and bruna in rick.exile
        assert (brisela.type_line, brisela.power, brisela.toughness) == (
            'Legendary Creature — Eldrazi Angel', '9', '10')
        assert 'vigilance' in brisela.oracle_text.lower()
        assert 'mana value 3 or less' in brisela.oracle_text.lower()

    def test_lone_or_not_owned_half_does_not_fabricate_brisela(
            self, make_game, make_card):
        game, rick, _claude, gisela, bruna = self._halves(make_game, make_card)
        rules = _engine(game)
        rick.battlefield.remove(bruna)
        game.active_player_index = 0
        assert _check_end_step_triggers_sync(rules, game)[0] == []
        rick.battlefield.append(bruna)
        bruna.owner_index = 1
        assert _check_end_step_triggers_sync(rules, game)[0] == []
        assert all(c.name != 'Brisela, Voice of Nightmares'
                   for c in rick.battlefield)


class TestGorgonRecluse:
    def test_uses_exact_nonblack_counterpart_and_waits_until_end_of_combat(
            self, make_game, make_card):
        game = make_game()
        rick, claude = game.players
        gorgon = make_card('Gorgon Recluse', power='2', toughness='4',
                           mana_cost='{3}{B}{B}', owner_index=0)
        victim = make_card('Same Name', mana_cost='{2}{G}', owner_index=1)
        unrelated = make_card('Same Name', mana_cost='{2}{G}', owner_index=1)
        rick.battlefield.append(gorgon)
        claude.battlefield.extend([victim, unrelated])
        game.attackers = [victim.id]
        game.blockers = {victim.id: [gorgon.id]}
        rules = _engine(game)

        check_block_triggers(rules, game)
        assert victim in claude.battlefield, 'must not destroy before combat ends'
        drain_end_of_combat_destructions(rules, game)
        assert victim not in claude.battlefield
        assert unrelated in claude.battlefield, 'duplicate names need stable IDs'

    def test_black_or_gone_counterpart_causes_no_destroy(self, make_game, make_card):
        game = make_game()
        rick, claude = game.players
        gorgon = make_card('Gorgon Recluse', mana_cost='{3}{B}{B}', owner_index=0)
        black = make_card('Black Bear', mana_cost='{1}{B}', owner_index=1)
        rick.battlefield.append(gorgon)
        claude.battlefield.append(black)
        game.attackers, game.blockers = [black.id], {black.id: [gorgon.id]}
        rules = _engine(game)
        check_block_triggers(rules, game)
        drain_end_of_combat_destructions(rules, game)
        assert black in claude.battlefield

        pale = make_card('Pale Bear', mana_cost='{1}{G}', owner_index=1)
        claude.battlefield.append(pale)
        game.attackers, game.blockers = [pale.id], {pale.id: [gorgon.id]}
        check_block_triggers(rules, game)
        claude.battlefield.remove(pale)
        drain_end_of_combat_destructions(rules, game)
        assert pale not in claude.graveyard, 'gone target is not retroactively destroyed'


class TestAttachedAttackKeywords:
    def test_attached_aura_grants_annihilator_but_unattached_does_not(
            self, make_game, make_card):
        game = make_game()
        rick, claude = game.players
        attacker = make_card('Vanilla', power='2', toughness='2')
        aura = make_card('Eldrazi Conscription', type_line='Enchantment — Aura',
                         oracle_text=('Enchant creature\nEnchanted creature gets '
                                      '+10/+10 and has trample and annihilator 2.'))
        rick.battlefield.extend([attacker, aura])
        assert _attack_keywords_with_attached_grants(game, attacker) == {}
        sacrifices = [make_card(f'Sacrifice {i}', type_line='Artifact')
                      for i in range(2)]
        claude.battlefield.extend(sacrifices)
        rules = _engine(game)
        _check_attack_triggers_sync(rules, game, attacker, rick)
        assert sacrifices == claude.battlefield, 'unattached Aura grants nothing'
        aura.attached_to = attacker.id
        assert _attack_keywords_with_attached_grants(game, attacker)['annihilator'] == 2
        _check_attack_triggers_sync(rules, game, attacker, rick)
        assert all(card in claude.graveyard for card in sacrifices)

    def test_printed_and_granted_annihilator_resolve_once(self, make_game, make_card):
        game = make_game()
        rick, claude = game.players
        attacker = make_card('Printed Annihilator', oracle_text='Annihilator 1')
        aura = make_card('Eldrazi Conscription', type_line='Enchantment — Aura',
                         oracle_text='Enchanted creature has annihilator 2.',
                         attached_to=attacker.id)
        permanent = make_card('Sacrifice Me', type_line='Artifact')
        rick.battlefield.extend([attacker, aura])
        claude.battlefield.append(permanent)
        rules = _engine(game)
        _check_attack_triggers_sync(rules, game, attacker, rick)
        assert permanent in claude.graveyard
        assert not claude.battlefield, 'one printed keyword must fire once, not once per source'
