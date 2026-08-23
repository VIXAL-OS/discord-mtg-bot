"""Pins for the live Aug-13 cube follow-up findings."""

import asyncio
from types import MethodType, SimpleNamespace

from cube_draft import CubeDraftCog, CubeLoader, DraftSeat, claude_make_pick
from mtg.deck_loader import DeckLoader
from mtg.engine import GameEngine
from mtg.models import Card


BONECRUSHER = {
    "name": "Bonecrusher Giant // Stomp",
    "layout": "adventure",
    "mana_cost": "{2}{R} // {1}{R}",
    "type_line": "Creature — Giant",
    "oracle_text": (
        "Whenever this creature becomes the target of a spell, this creature "
        "deals 2 damage to that spell's controller."),
    "power": "4",
    "toughness": "3",
    "card_faces": [
        {
            "name": "Bonecrusher Giant",
            "mana_cost": "{2}{R}",
            "type_line": "Creature — Giant",
            "oracle_text": (
                "Whenever this creature becomes the target of a spell, this "
                "creature deals 2 damage to that spell's controller."),
        },
        {
            "name": "Stomp",
            "mana_cost": "{1}{R}",
            "type_line": "Instant — Adventure",
            "oracle_text": (
                "Damage can't be prevented this turn. Stomp deals 2 damage "
                "to any target."),
        },
    ],
}


def _cube_loader_with(response):
    loader = SimpleNamespace(card_cache={"bonecrusher giant": response})

    async def fetch(_self, _name):
        return response

    loader.fetch_card_data = MethodType(fetch, loader)
    loader._extract_adventure_data = MethodType(
        DeckLoader._extract_adventure_data, loader)
    return CubeLoader(loader)


def test_cube_loader_keeps_adventure_front_face_and_three_mana_value():
    cards = asyncio.run(
        _cube_loader_with(BONECRUSHER)._fetch_cards(["Bonecrusher Giant"]))

    assert len(cards) == 1
    card = cards[0]
    assert card.name == "Bonecrusher Giant"
    assert card.adventure_name == "Stomp"
    assert card.adventure_cost == "{1}{R}"
    assert card.cmc == 3


def test_cube_bonecrusher_bare_name_plan_is_payable_with_three_mana(
        game, make_card):
    from mtg.ai_turn import _validate_plan_mana

    card = asyncio.run(
        _cube_loader_with(BONECRUSHER)._fetch_cards(["Bonecrusher Giant"]))[0]
    player = game.players[0]
    player.hand.append(card)
    player.battlefield.extend([
        make_card(
            "Mountain", type_line="Basic Land — Mountain",
            oracle_text="({T}: Add {R}.)", power=None, toughness=None)
        for _ in range(3)
    ])

    validated = _validate_plan_mana(
        None, game, 0, [{"type": "cast", "card": "Bonecrusher Giant"}])

    assert validated[0] == {"type": "cast", "card": "Bonecrusher Giant"}


def test_cube_loader_preserves_wrong_fuzzy_adventure_guard():
    fuzzy = {
        "name": "Grave Researcher // Reanimate",
        "layout": "adventure",
        "mana_cost": "{1}{B} // {B}",
        "type_line": "Creature — Human Wizard",
        "oracle_text": "When this creature enters, mill a card.",
        "power": "2",
        "toughness": "1",
        "card_faces": [
            {"name": "Grave Researcher", "mana_cost": "{1}{B}"},
            {"name": "Reanimate", "mana_cost": "{B}",
             "type_line": "Sorcery — Adventure"},
        ],
    }
    loader = _cube_loader_with(fuzzy)
    loader.deck_loader.card_cache = {"reanimate": fuzzy}

    card = asyncio.run(loader._fetch_cards(["Reanimate"]))[0]

    assert card.name == "Reanimate"
    assert card.adventure_name == ""
    assert card.adventure_cost == ""


class _FakeMessages:
    def __init__(self, owner):
        self.owner = owner

    def create(self, **kwargs):
        self.owner.requests.append(kwargs)
        self.owner.calls += 1
        self.owner.prompt_tokens += 17
        self.owner.completion_tokens += 1
        return SimpleNamespace(
            content=[SimpleNamespace(text="0")],
            usage=SimpleNamespace(input_tokens=17, output_tokens=1),
        )


class _FakeAdapter:
    def __init__(self, model):
        self.model = model
        self.requests = []
        self.calls = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.messages = _FakeMessages(self)

    def get_stats(self):
        return {
            "calls": self.calls,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "purpose_counts": {},
        }


def _provider_cog(actor, strategist=None):
    engine = GameEngine(None)
    original_actor = object()
    original_strategist = object()
    original_rules = object()
    engine.claude_ai.client = original_actor
    engine.claude_ai.model = "claude-actor"
    engine.claude_ai.strategist_client = original_strategist
    engine.claude_ai.strategist_model = "claude-strategist"
    engine.rules.client = original_rules
    engine.rules.model = "claude-rules"
    fallback_claude = _FakeAdapter("claude-sonnet-5")
    usage = []
    bot = SimpleNamespace(
        claude=fallback_claude,
        track_mtg_usage=lambda value, model: usage.append((value, model)),
    )
    game_cog = SimpleNamespace(
        batch_stats_adapters=lambda: ("deepseek", actor, strategist),
    )
    cog = CubeDraftCog.__new__(CubeDraftCog)
    cog.bot = bot
    cog.engine = engine
    cog.game_cog = game_cog
    return cog, (original_actor, original_strategist, original_rules), usage


def test_direct_cube_installs_selected_provider_for_pick_and_gameplay(monkeypatch):
    import mtg.autoplay as autoplay_module

    monkeypatch.setattr(
        autoplay_module, "_AUTOPLAY_SWAP_DEPTH", 0, raising=False)
    monkeypatch.setattr(
        autoplay_module, "_AUTOPLAY_TRUE_ORIGINALS", None, raising=False)
    actor = _FakeAdapter("deepseek-v4-flash")
    strategist = _FakeAdapter("deepseek-v4-pro")
    cog, originals, usage = _provider_cog(actor, strategist)

    session = cog._begin_autodraft_provider_session()

    assert session["provider"] == "deepseek"
    assert session["ai_name"] == "Deepseek"
    assert session["draft_client"] is actor
    assert cog.engine.claude_ai.client is actor
    assert cog.engine.rules.client is actor
    assert cog.engine.claude_ai.strategist_client is strategist

    seat = DraftSeat(seat_index=1, name=session["ai_name"], is_claude=True)
    picked = asyncio.run(claude_make_pick(
        session["draft_client"], seat,
        [Card(name="Lightning Bolt", mana_cost="{R}", type_line="Instant")],
        1, 1, usage_callback=cog.bot.track_mtg_usage))

    assert picked.name == "Lightning Bolt"
    assert actor.calls == 1
    assert actor.requests[0]["model"] == "deepseek-v4-flash"
    assert usage[0][1] == "deepseek-v4-flash"
    assert cog.bot.claude.calls == 0

    cog._end_autodraft_provider_session(session)
    assert cog.engine.claude_ai.client is originals[0]
    assert cog.engine.claude_ai.strategist_client is originals[1]
    assert cog.engine.rules.client is originals[2]
    assert autoplay_module._AUTOPLAY_SWAP_DEPTH == 0


def test_missing_selected_adapter_stays_claude_and_says_claude(monkeypatch):
    import mtg.autoplay as autoplay_module

    monkeypatch.setattr(
        autoplay_module, "_AUTOPLAY_SWAP_DEPTH", 0, raising=False)
    monkeypatch.setattr(
        autoplay_module, "_AUTOPLAY_TRUE_ORIGINALS", None, raising=False)
    cog, originals, _usage = _provider_cog(None)

    session = cog._begin_autodraft_provider_session()

    assert session["provider"] == "claude"
    assert session["ai_name"] == "Claude"
    assert session["draft_client"] is cog.bot.claude
    assert cog.engine.claude_ai.client is originals[0]
    assert cog.engine.rules.client is originals[2]
    assert not session["joined_swap"]
    assert autoplay_module._AUTOPLAY_SWAP_DEPTH == 0
