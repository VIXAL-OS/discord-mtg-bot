"""Behavioral regressions from the Aug. 5 strict 160-game batch audit.

These tests are deterministic and never launch Discord, an LLM, or autoplay.
"""

import asyncio
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from mtg.engine import GameEngine
from mtg.models import Card, FormatValidator, StackEntry
from mtg.spells import _await_stack_window, _pay_costs, cast_spell_async
from mtg.triggers import (
    _check_cast_triggers,
    _check_creature_etb_triggers_sync,
    _check_upkeep_triggers_sync,
)
from rules.effect_templates import build_game_context, get_effect_library
from rules.targeting_helpers import (
    _find_any_valid_target,
    aura_has_legal_target,
)


ROOT = Path(__file__).resolve().parent.parent


def _engine(game):
    engine = GameEngine(None)
    game._rules_engine = engine.rules
    engine.rules.engine_ref = engine
    return engine


def _land(make_card, name, subtype):
    color = {"Plains": "W", "Island": "U", "Swamp": "B",
             "Mountain": "R", "Forest": "G"}[subtype]
    return make_card(
        name, type_line=f"Basic Land - {subtype}",
        oracle_text=f"{{T}}: Add {{{color}}}.",
        power=None, toughness=None)


def test_bastion_etb_creates_soldier_without_false_drain(
        make_game, make_card, rules):
    game = make_game()
    rick, claude = game.players
    bastion = make_card(
        "Bastion of Remembrance", type_line="Enchantment",
        power=None, toughness=None,
        oracle_text=("When Bastion of Remembrance enters the battlefield, "
                     "create a 1/1 white Human Soldier creature token. "
                     "Whenever a creature you control dies, each opponent "
                     "loses 1 life and you gain 1 life."))
    rick.battlefield.append(bastion)

    actions, _ = get_effect_library().resolve_etb(
        bastion.name, bastion.oracle_text, rick.name, claude.name,
        build_game_context(game, rick, claude, card=bastion))

    assert [action["action"] for action in actions] == ["create_token"]
    for action in actions:
        rules._execute_action_on_state(game, action)
    assert (rick.life, claude.life) == (40, 40)
    soldiers = [card for card in rick.battlefield
                if card.name == "Human Soldier"]
    assert len(soldiers) == 1
    assert soldiers[0].power == "1" and soldiers[0].toughness == "1"


def test_sacrificed_creature_fires_bastion_death_trigger_exactly_once(
        make_game, make_card):
    game = make_game()
    engine = _engine(game)
    rick, claude = game.players
    bastion = make_card(
        "Bastion of Remembrance", type_line="Enchantment",
        power=None, toughness=None,
        oracle_text=("Whenever a creature you control dies, each opponent "
                     "loses 1 life and you gain 1 life."))
    fodder = make_card("Doomed Traveler")
    rick.battlefield.extend([bastion, fodder])

    engine.rules._execute_action_on_state(game, {
        "action": "sacrifice_permanent", "player": rick.name,
        "type_filter": "creature", "reason": "regression test",
    })
    messages = engine.check_state_based_actions(game)

    assert fodder in rick.graveyard
    assert (rick.life, claude.life) == (41, 39)
    assert len([message for message in messages
                if "Bastion of Remembrance" in message]) == 1
    assert game._recently_died == []


def test_dreadhorde_upkeep_loses_life_and_grows_one_army(
        make_game, make_card):
    game = make_game()
    engine = _engine(game)
    rick = game.players[0]
    invasion = make_card(
        "Dreadhorde Invasion", type_line="Enchantment",
        power=None, toughness=None,
        oracle_text=("At the beginning of your upkeep, you lose 1 life and "
                     "amass Zombies 1."))
    rick.battlefield.append(invasion)

    _check_upkeep_triggers_sync(engine, game)
    _check_upkeep_triggers_sync(engine, game)

    armies = [card for card in rick.battlefield
              if "army" in (card.type_line or "").lower()]
    assert rick.life == 38
    assert len(armies) == 1
    assert armies[0].counters.get("+1/+1") == 2


@pytest.mark.parametrize(
    "name,oracle,victim_type,forbidden_action",
    [
        (
            "Diabolic Intent",
            "As an additional cost to cast this spell, sacrifice a creature. "
            "Search your library for a card, put that card into your hand, "
            "then shuffle.",
            "creature", "sacrifice_permanent",
        ),
        (
            "Shard Volley",
            "As an additional cost to cast this spell, sacrifice a land. "
            "Shard Volley deals 3 damage to any target.",
            "land", "sacrifice_land",
        ),
    ],
)
def test_mandatory_sacrifice_is_paid_in_cost_stage_not_resolution(
        make_game, make_card, name, oracle, victim_type, forbidden_action):
    game = make_game("modern")
    engine = _engine(game)
    rick, claude = game.players
    card = make_card(
        name, type_line="Sorcery" if name == "Diabolic Intent" else "Instant",
        oracle_text=oracle, mana_cost="", cmc=1,
        power=None, toughness=None)
    if victim_type == "creature":
        victim = make_card("Cost Fodder")
    else:
        victim = _land(make_card, "Cost Mountain", "Mountain")
    rick.battlefield.append(victim)
    costs = {
        "effective_mana_cost": "", "effective_cmc": 0,
        "total_cost": 0, "x_value_chosen": 0,
        "total_alt_reduction": 0, "cost_increase": 0,
        "pay_mana": False,
    }

    assert _pay_costs(engine, game, rick, card, costs, 0) is None
    assert victim not in rick.battlefield
    assert victim in rick.graveyard
    assert costs["additional_cost_messages"]

    actions, _ = get_effect_library().resolve_spell(
        name, oracle, rick.name, claude.name,
        build_game_context(game, rick, claude, card=card))
    assert all(action["action"] not in {
        "sacrifice_permanent", "sacrifice_land"
    } for action in actions)
    assert forbidden_action not in {action["action"] for action in actions}


def test_searing_blaze_requires_opponent_controlled_creature(
        make_game, make_card):
    game = make_game("modern")
    rick, claude = game.players
    blaze = make_card(
        "Searing Blaze", type_line="Instant", power=None, toughness=None,
        oracle_text=("Searing Blaze deals 1 damage to target player or "
                     "planeswalker and 1 damage to target creature that "
                     "player or that planeswalker's controller controls."))
    rick.battlefield.append(make_card("Rick's Bear"))

    assert not _find_any_valid_target(game, blaze, rick.name)
    claude.battlefield.append(make_card("Claude's Bear"))
    assert _find_any_valid_target(game, blaze, rick.name)


def test_already_countered_spell_is_not_a_counterspell_target(
        make_game, make_card):
    game = make_game("modern")
    engine = _engine(game)
    rick, claude = game.players
    counterspell = make_card(
        "Counterspell", type_line="Instant", mana_cost="{U}{U}", cmc=2,
        power=None, toughness=None, oracle_text="Counter target spell.")
    threat = make_card(
        "Divination", type_line="Sorcery", power=None, toughness=None,
        oracle_text="Draw two cards.")
    entry = StackEntry(
        card=threat, controller_name=claude.name, controller_index=1)
    entry.countered = True
    game.stack.append(entry)
    rick.hand.append(counterspell)
    rick.battlefield.extend([
        _land(make_card, "Island A", "Island"),
        _land(make_card, "Island B", "Island"),
    ])

    assert not _find_any_valid_target(game, counterspell, rick.name)
    ok, reason, _ = asyncio.run(
        cast_spell_async(engine, game, rick, counterspell, target=threat))
    assert not ok
    assert "stack" in reason.lower() or "countered" in reason.lower()
    assert counterspell in rick.hand


def test_teferi_damage_prevention_registers_without_import_shadow(
        make_game):
    game = make_game()
    engine = _engine(game)
    rick = game.players[0]
    assert game.replacement_engine is not None

    message = engine.rules._execute_action_on_state(game, {
        "action": "prevent_all_damage", "player": rick.name,
        "reason": "Teferi's Protection", "lock_life_total": True,
    })

    assert message
    assert rick._damage_prevented
    assert rick._life_total_locked
    assert rick._temp_replacement_effect_ids


def test_panharmonicon_does_not_double_devotion_disabled_god(
        make_game, make_card):
    from mtg.actions import _fire_noncast_battlefield_entry

    game = make_game()
    engine = _engine(game)
    rick = game.players[0]
    panharmonicon = make_card(
        "Panharmonicon", type_line="Artifact", power=None, toughness=None)
    erebos = make_card(
        "Erebos, God of the Dead", mana_cost="{3}{B}",
        type_line="Legendary Enchantment Creature - God",
        oracle_text=("Indestructible. As long as your devotion to black is "
                     "less than five, Erebos isn't a creature."))
    rick.battlefield.extend([panharmonicon, erebos])

    messages = _fire_noncast_battlefield_entry(
        engine.rules, game, rick, erebos)

    assert not erebos.is_creature(game)
    assert not any("Panharmonicon doubles" in message for message in messages)


def test_scourge_of_valkas_triggers_on_self_and_later_dragons(
        make_game, make_card):
    game = make_game()
    engine = _engine(game)
    rick, claude = game.players
    scourge = make_card(
        "Scourge of Valkas", type_line="Creature - Dragon",
        oracle_text=("Whenever Scourge of Valkas or another Dragon enters "
                     "the battlefield under your control, it deals X damage "
                     "to any target, where X is the number of Dragons you "
                     "control."))
    rick.battlefield.append(scourge)

    _check_creature_etb_triggers_sync(engine, game, rick, scourge)
    dragon = make_card("Shivan Dragon", type_line="Creature - Dragon")
    rick.battlefield.append(dragon)
    _check_creature_etb_triggers_sync(engine, game, rick, dragon)

    assert claude.life == 37


def test_thermo_alchemist_cast_trigger_untaps_deterministically(
        make_game, make_card):
    game = make_game()
    engine = _engine(game)
    rick = game.players[0]
    thermo = make_card(
        "Thermo-Alchemist", type_line="Creature - Human Shaman",
        oracle_text=("Defender. Whenever you cast an instant or sorcery "
                     "spell, untap this creature."), tapped=True)
    spell = make_card(
        "Shock", type_line="Instant", power=None, toughness=None,
        oracle_text="Shock deals 2 damage to any target.")
    rick.battlefield.append(thermo)

    messages = asyncio.run(_check_cast_triggers(engine, game, rick, spell))

    assert not thermo.tapped
    assert any("Thermo-Alchemist" in message and "untaps" in message
               for message in messages)


def test_aura_target_gate_is_zone_and_controller_aware(
        make_game, make_card):
    game = make_game()
    rick, claude = game.players
    own_aura = make_card(
        "Draconic Destiny", type_line="Enchantment - Aura",
        power=None, toughness=None,
        oracle_text="Enchant creature you control. Enchanted creature gets +1/+1.")
    claude.battlefield.append(make_card("Opponent Bear"))
    assert not aura_has_legal_target(game, own_aura, rick)
    rick.battlefield.append(make_card("Own Bear"))
    assert aura_has_legal_target(game, own_aura, rick)

    grave_aura = make_card(
        "Animate Dead", type_line="Enchantment - Aura",
        power=None, toughness=None,
        oracle_text="Enchant creature card in a graveyard.")
    claude.graveyard.append(make_card(
        "Dead Sorcery", type_line="Sorcery", power=None, toughness=None))
    assert not aura_has_legal_target(game, grave_aura, rick)
    claude.graveyard.append(make_card("Dead Bear"))
    assert aura_has_legal_target(game, grave_aura, rick)


def test_memo_guard_rejects_labeled_task_scaffolding_not_strategy():
    from mtg.claude_player import _memo_has_labeled_task_reference

    leaked = ("Win condition: the user requests output in four lines. "
              "This turn: we need to answer the prompt.")
    strategic = ("Win condition: protect Scourge and attack in two turns. "
                 "This turn: cast Opt, then hold Counterspell.")

    assert _memo_has_labeled_task_reference(leaked)
    assert not _memo_has_labeled_task_reference(strategic)


def test_adapted_response_accepts_missing_usage():
    from rules.llm_adapter import _AdaptedResponse

    response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(
            content='{"type":"pass"}', reasoning_content=None))],
        usage=None,
    )

    adapted = _AdaptedResponse(response)

    assert adapted.content[0].text == '{"type":"pass"}'
    assert adapted.usage.input_tokens == 0
    assert adapted.usage.output_tokens == 0


def test_batch_stats_resolver_stays_with_selected_provider():
    from mtg.cog import MTGGameCog

    qwen_actor, qwen_strat = object(), object()
    deep_actor = object()
    fake = SimpleNamespace(
        _active_provider="qwen",
        _qwen_adapter=qwen_actor,
        _qwen_reasoner_adapter=qwen_strat,
        _ds_via_dashscope_adapter=None,
        _ds_via_dashscope_reasoner_adapter=None,
        _deepseek_adapter=deep_actor,
        _deepseek_reasoner_adapter=None,
    )

    assert MTGGameCog.batch_stats_adapters(fake) == (
        "qwen", qwen_actor, qwen_strat)
    fake._active_provider = "deepseek"
    assert MTGGameCog.batch_stats_adapters(fake) == (
        "deepseek", deep_actor, None)


def test_priority_command_reports_real_holder_and_stack(make_game):
    from mtg.cog import MTGGameCog

    game = make_game()
    game.stack_enabled = True
    game._priority_system = SimpleNamespace(get_state=lambda: {
        "turn": 3, "phase": "MAIN1", "active_player": "Rick",
        "priority_holder": "Claude",
        "stack": [{"name": "Counterspell", "controller": "Claude",
                   "is_spell": True}],
    })
    sent = []

    class Ctx:
        async def send(self, content):
            sent.append(content)

    fake = SimpleNamespace(_get_game=lambda _ctx: game)
    asyncio.run(MTGGameCog.show_priority.callback(fake, Ctx()))

    assert len(sent) == 1
    assert "Priority: Claude" in sent[0]
    assert "Counterspell" in sent[0]
    assert "top first" in sent[0]


def test_stdout_tee_emits_per_game_write_instrumentation(tmp_path):
    from mtg.util import StdoutTee

    original = io.StringIO()
    tee = StdoutTee(original)
    log_path = tmp_path / "game.log"
    tee.add_game(42, log_path)
    tee.active_thread = 42
    tee.write("alpha")
    tee.write("beta")
    tee.remove_game(42)

    assert log_path.read_text(encoding="utf-8") == "alphabeta"
    stats_line = original.getvalue().splitlines()[-1]
    assert "[STDOUT-TEE-STATS] game=42" in stats_line
    assert "writes=2" in stats_line
    assert "bytes=9" in stats_line
    assert "slow_ge_10ms=" in stats_line


def test_early_cast_flushes_prior_turn_actions_before_cast(
        make_game, make_card):
    game = make_game()
    engine = _engine(game)
    rick = game.players[0]
    game.stack_enabled = True
    game._priority_system = None
    sent = []

    async def send(content):
        sent.append(content)

    game._stack_send_func = send
    game._active_turn_narration = {
        "turn": game.turn_number, "player": rick.name,
        "actions": ["plays Mountain", "[DEBUG] hidden"],
        "flushed": False,
    }
    spell = make_card(
        "Opt", type_line="Instant", power=None, toughness=None,
        oracle_text="Scry 1. Draw a card.")

    final, _, _ = asyncio.run(_await_stack_window(
        engine, game, rick, spell, None, []))

    assert final is None
    assert len(sent) >= 2
    assert "plays Mountain" in sent[0]
    assert "hidden" not in sent[0]
    assert "cast **Opt**" in sent[1]
    assert game._active_turn_narration["actions"] == []
    assert game._active_turn_narration["flushed"] is True


def _cached_card(cache, name, suffix=""):
    entry = cache.get(name.lower(), {})
    return Card(
        name=name, id=f"{name}-{suffix}",
        type_line=entry.get("type_line", ""),
        cmc=entry.get("cmc", 0) or 0,
        mana_cost=entry.get("mana_cost", ""),
        oracle_text=entry.get("oracle_text", ""),
    )


def test_strict_matrix_format_specific_fixtures_validate():
    from mtg.autoplay import AUTOPLAY_DECKS

    cache = json.loads(
        (ROOT / "data" / "card_data_cache.json").read_text(encoding="utf-8"))
    expected = {
        "burn_standard": "standard",
        "uw_control_standard": "standard",
        "burn_legacy": "legacy",
        "jund_legacy": "legacy",
        "brawl_tatyova": "brawl",
        "oathbreaker_chandra": "oathbreaker",
        "companion_lurrus_vintage": "vintage",
        "delve": "modern",
    }

    for alias, fmt in expected.items():
        deck = json.loads((ROOT / "data" /
                           f"{AUTOPLAY_DECKS[alias]}.json").read_text(
                               encoding="utf-8"))
        cards = []
        first_by_name = {}
        for item in deck["cards"]:
            copies = [
                _cached_card(cache, item["name"], f"{alias}-{index}")
                for index in range(item.get("quantity", 1))
            ]
            cards.extend(copies)
            first_by_name[item["name"]] = copies[0]

        commanders = []
        if deck.get("commander"):
            commander = first_by_name[deck["commander"]]
            commander.is_commander = True
            commanders.append(commander)
        if deck.get("signature_spell"):
            signature = first_by_name[deck["signature_spell"]]
            signature.is_signature_spell = True
            commanders.append(signature)
        companion = None
        if deck.get("companion"):
            companion = _cached_card(cache, deck["companion"], alias)

        ok, issues = FormatValidator.validate_deck(
            cards, fmt, commander=commanders or None, companion=companion)
        assert ok, f"{alias} must be {fmt}-legal: {issues}"


def test_constructed_legality_allows_cache_misses(make_card):
    cards = [make_card(
        "Future Preview Card", type_line="Instant",
        power=None, toughness=None)]
    cards.extend(make_card(
        "Mountain", type_line="Basic Land - Mountain",
        power=None, toughness=None) for _ in range(59))

    ok, issues = FormatValidator.validate_deck(cards, "standard")

    assert ok, issues


def test_puresteel_zero_equip_requires_exact_global_wording():
    import inspect
    import mtg.engine

    source = inspect.getsource(mtg.engine)
    start = source.index("METALCRAFT (CR 702.60)")
    window = source[start:start + 1000]
    assert r"equipment you control have equip \{0\}" in window
    assert "has_metalcraft(player)" in window


def test_activation_template_actions_receive_source_metadata():
    import inspect
    import mtg.engine

    source = inspect.getsource(mtg.engine)
    start = source.index("Activated-template damage")
    window = source[start:start + 700]
    assert 'act.setdefault("_source_card_name", perm.name)' in window
    assert 'act.setdefault("_source_controller", player.name)' in window
    assert 'act.setdefault("_source_oracle"' in window
    assert 'act.setdefault("source", perm.name)' in window
