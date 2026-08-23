"""Q-H: planeswalkers as combat defenders (CR 508.1a).

THE GAP. `Card.attacking_player` held a defending SEAT and nothing else — the
engine had no concept of attacking a planeswalker at all (grep-confirmed: no
attack-target, no attacking_planeswalker, no loyalty routing in combat). So a
planeswalker could sit on the battlefield untouchable by creatures, which is
not a rules approximation but a whole missing interaction, in a pool where 19
decks run walkers and one runs ten.

BATTLES ARE DELIBERATELY NOT IMPLEMENTED, and the reason is measured rather
than assumed: `data/card_data_cache.json` contains ZERO cards with Battle in
the type line. Building a defender kind for a card type that exists nowhere in
the pool is the speculative-generality shape this register keeps finding as
dead code. When a Battle enters the pool, the seam is `attacked_defender_for`
and the same routing helper.

DESIGN NOTE. The planeswalker's CONTROLLER stays `attacking_player`, because
blocking, attack taxes and defender grouping all key off the seat and are
already correct (CR 508.1a: you attack a planeswalker a defending player
controls). Only the walker itself needs naming.
"""
import pytest

from mtg.combat import resolve_combat_damage
from mtg.models import Card, GameState, Player


def _game(make_card, walker_loyalty=3, attacker_power="2"):
    rick = Player(name="Rick", user_id=1, life=40)
    claude = Player(name="Claude", user_id=2, life=40)
    game = GameState(thread_id=9, format="commander", players=[rick, claude])
    bear = make_card("Grizzly Bears", type_line="Creature — Bear",
                     power=attacker_power, toughness="2", summoning_sick=False)
    walker = make_card("Jace, the Mind Sculptor", power=None, toughness=None,
                       type_line="Legendary Planeswalker — Jace")
    walker.loyalty_counters = walker_loyalty
    rick.battlefield.append(bear)
    claude.battlefield.append(walker)
    return game, bear, walker


def _swing(game, attacker, walker=None):
    attacker.attacking = True
    attacker.attacking_player = 1
    attacker.attacking_planeswalker = walker.id if walker is not None else None
    game.attackers = [attacker.id]


# --------------------------------------------------------------------------
# Damage routing
# --------------------------------------------------------------------------

def test_combat_damage_removes_loyalty_not_life(rules, make_card):
    game, bear, walker = _game(make_card)
    _swing(game, bear, walker)

    messages = resolve_combat_damage(rules, game)

    assert walker.loyalty_counters == 1, "CR 120.3c: damage removes loyalty"
    assert game.players[1].life == 40, "the controller must take none of it"
    assert any("Jace" in m and "loyalty: 1" in m for m in messages)


def test_the_same_swing_without_a_walker_still_hits_the_player(rules, make_card):
    """Adverse control — the ordinary path must be untouched."""
    game, bear, walker = _game(make_card)
    _swing(game, bear, walker=None)

    resolve_combat_damage(rules, game)

    assert game.players[1].life == 38
    assert walker.loyalty_counters == 3


def test_a_walker_that_left_absorbs_nothing_and_redirects_nothing(rules,
                                                                  make_card):
    """CR 506.4 / 508.1: the attacker stays attacking, but its damage is NOT
    redirected to the defending player. Getting this wrong in the other
    direction — quietly falling back to the face — is the tempting bug."""
    game, bear, walker = _game(make_card)
    _swing(game, bear, walker)
    game.players[1].battlefield.remove(walker)

    messages = resolve_combat_damage(rules, game)

    assert game.players[1].life == 40, "damage must not fall through to the face"
    assert any("left the battlefield" in m for m in messages)


def test_lethal_loyalty_damage_kills_the_walker(rules, make_card):
    """The existing zero-loyalty SBA finishes the job once loyalty hits 0."""
    game, bear, walker = _game(make_card, walker_loyalty=2)
    _swing(game, bear, walker)

    resolve_combat_damage(rules, game)
    assert walker.loyalty_counters == 0

    rules.process_state_based_actions(game)
    assert walker not in game.players[1].battlefield
    assert walker in game.players[1].graveyard


def test_lifelink_still_gains_life(rules, make_card):
    """CR 702.15b turns on damage DEALT, not on what received it — attacking a
    planeswalker must not silently switch lifelink off."""
    game, bear, walker = _game(make_card)
    bear.oracle_text = "Lifelink"
    bear.keywords = ["Lifelink"]
    _swing(game, bear, walker)

    resolve_combat_damage(rules, game)

    assert walker.loyalty_counters == 1
    assert game.players[0].life == 42, "lifelink should still have fired"


def test_commander_damage_does_not_accrue_from_a_walker_hit(rules, make_card):
    """CR 903.10a counts damage dealt to a PLAYER. A commander that connects
    with a planeswalker must not tick the 21 clock."""
    game, bear, walker = _game(make_card)
    bear.is_commander = True
    _swing(game, bear, walker)

    resolve_combat_damage(rules, game)

    assert game.players[1].commander_damage == {}
    assert walker.loyalty_counters == 1


def test_trample_over_a_blocker_carries_to_the_walker(rules, make_card):
    """The trample site is a SECOND damage-to-defender path; both route."""
    game, bear, walker = _game(make_card, attacker_power="5")
    bear.oracle_text = "Trample"
    bear.keywords = ["Trample"]
    blocker = make_card("Wall", type_line="Creature — Wall", power="0",
                        toughness="2")
    game.players[1].battlefield.append(blocker)
    _swing(game, bear, walker)
    blocker.blocking = [bear.id]
    bear.blocked_by = [blocker.id]
    game.blockers = {bear.id: [blocker.id]}

    resolve_combat_damage(rules, game)

    # 2 lethal to the Wall, 3 tramples through onto Jace.
    assert walker.loyalty_counters == 0
    assert game.players[1].life == 40


# --------------------------------------------------------------------------
# The resolver is self-invalidating
# --------------------------------------------------------------------------

def test_a_stale_id_on_a_non_attacking_creature_resolves_to_nothing(make_card):
    """Ten sites clear `attacking_player`; adding a parallel clear to each is a
    leak waiting to happen, so the resolver requires the creature to be
    CURRENTLY attacking. A stale id therefore cannot route damage anywhere."""
    game, bear, walker = _game(make_card)
    bear.attacking = False
    bear.attacking_planeswalker = walker.id
    assert game.attacked_planeswalker_for(bear) is None


def test_an_id_naming_a_non_planeswalker_resolves_to_nothing(make_card):
    game, bear, walker = _game(make_card)
    imposter = make_card("Ornithopter", type_line="Artifact Creature — Thopter")
    game.players[1].battlefield.append(imposter)
    _swing(game, bear, imposter)
    assert game.attacked_planeswalker_for(bear) is None


def test_a_walker_on_the_wrong_battlefield_resolves_to_nothing(make_card):
    """The walker must be controlled by the DEFENDING seat — an attacker
    cannot reach past its declared defender."""
    game, bear, walker = _game(make_card)
    game.players[1].battlefield.remove(walker)
    game.players[0].battlefield.append(walker)
    _swing(game, bear, walker)
    assert game.attacked_planeswalker_for(bear) is None


def test_leaving_the_battlefield_clears_the_assignment(make_card):
    """strip_combat_state is the leave chokepoint (Aug 7 C-3)."""
    from mtg.helpers import strip_combat_state

    game, bear, walker = _game(make_card)
    _swing(game, bear, walker)
    strip_combat_state(game, bear)
    assert bear.attacking_planeswalker is None
    assert bear.attacking is False


def test_the_assignment_round_trips(make_card):
    """It rides in save/undo like the seat does, or a reload mid-combat would
    silently turn a walker attack into a face attack."""
    game, bear, walker = _game(make_card)
    _swing(game, bear, walker)

    restored = GameState.from_dict(game.to_dict())
    r_bear = restored.players[0].battlefield[0]
    assert r_bear.attacking_planeswalker == walker.id
    assert restored.attacked_planeswalker_for(r_bear) is not None
