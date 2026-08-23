"""Q-H production controls shared by human multiplayer and cube FFA."""

import asyncio
from types import SimpleNamespace

from board_visual import _player_area_layout
from mtg.cog import MTGGameCog
from mtg.engine import GameEngine
from mtg.models import GameState, Player
from rules.priority import PriorityAction


def _game(*, duplicate_names=False):
    names = ("Same", "Same", "C", "D") if duplicate_names else (
        "A", "B", "C", "D")
    return GameState(
        thread_id=1608,
        format="limited",
        players=[
            Player(name=name, user_id=1600 + index, seat_id=index, life=20)
            for index, name in enumerate(names)
        ],
        active_player_index=0,
        experimental_ffa=True,
    )


def test_priority_uses_stable_seats_and_unique_presentation_labels():
    game = _game(duplicate_names=True)
    engine = GameEngine(None)
    engine.setup_stack(game, auto_pass_seconds=0)

    priority = game._priority_system
    assert priority.players == ["seat:0", "seat:1", "seat:2", "seat:3"]
    assert priority.priority_holder == "seat:0"
    assert priority.get_state()["priority_holder"] == "Same (seat 1)"
    result = asyncio.run(priority.player_action(
        "seat:0", PriorityAction.pass_priority()))
    assert result["priority_holder"] == "seat:1"
    assert priority.get_state()["priority_holder"] == "Same (seat 2)"


def test_legacy_name_keyed_priority_save_migrates_to_stable_seats():
    game = _game()
    legacy = {
        "version": 1,
        "players": ["A", "B", "C", "D"],
        "active_player": "A",
        "priority_holder": "B",
        "passes_in_succession": ["A"],
        "holds": ["C"],
        "auto_pass_configs": {"A": {"enabled": False}},
        "connected": {"A": True, "B": False},
        "display_names": {"A": "A", "B": "B"},
        "stack": [{"controller": "C", "name": "Test"}],
    }

    migrated = game.normalize_priority_state(legacy)

    assert migrated["version"] == 2
    assert migrated["players"] == ["seat:0", "seat:1", "seat:2", "seat:3"]
    assert migrated["priority_holder"] == "seat:1"
    assert migrated["passes_in_succession"] == ["seat:0"]
    assert migrated["connected"]["seat:1"] is False
    assert migrated["stack"][0]["controller"] == "seat:2"


def test_four_player_visual_areas_are_disjoint_and_seat_zero_is_bottom():
    heights = [310, 420, 330, 390]
    bases, dividers, total = _player_area_layout(heights, 30)

    spans = sorted(
        (base, base + heights[index], index)
        for index, base in bases.items()
    )
    assert [index for _start, _end, index in spans] == [3, 2, 1, 0]
    assert len(dividers) == 3
    assert all(left[1] + 30 == right[0]
               for left, right in zip(spans, spans[1:]))
    assert total == sum(heights) + 90


def test_multiplayer_damage_requires_exact_target_and_rejects_eliminated_actor():
    async def exercise():
        game = _game()
        saved = []
        sent = []
        cog = object.__new__(MTGGameCog)
        cog._get_game = lambda _ctx: game
        cog.engine = SimpleNamespace(
            deal_damage=lambda player, amount, game=None: setattr(
                player, "life", player.life - amount),
            check_state_based_actions=lambda _game: [],
            save_game=lambda saved_game: saved.append(saved_game),
        )

        async def send(message=None, **_kwargs):
            sent.append(message)

        ctx = SimpleNamespace(
            author=SimpleNamespace(id=game.players[0].user_id),
            message=SimpleNamespace(mentions=[]),
            send=send,
        )

        await MTGGameCog.deal_damage.callback(cog, ctx, 3, None)
        assert "explicit target" in sent[-1]
        assert [player.life for player in game.players] == [20, 20, 20, 20]

        await MTGGameCog.deal_damage.callback(cog, ctx, 3, "seat 3")
        assert game.players[2].life == 17
        assert saved == [game]

        game.players[0].eliminated = True
        await MTGGameCog.deal_damage.callback(cog, ctx, 2, "seat 2")
        assert game.players[1].life == 20
        assert "eliminated" in sent[-1]

    asyncio.run(exercise())


def test_thread_membership_repair_unarchives_before_add():
    async def exercise():
        events = []

        class Thread:
            id = 1608
            archived = True

            async def edit(self, **kwargs):
                events.append(("edit", kwargs))
                self.archived = kwargs.get("archived", self.archived)

            async def add_user(self, user):
                events.append(("add", user.id))

        cog = object.__new__(MTGGameCog)
        result = await cog._ensure_thread_member(
            Thread(), SimpleNamespace(id=42))
        assert result is True
        assert events == [("edit", {"archived": False}), ("add", 42)]

    asyncio.run(exercise())
