"""Aug 4, 2026 — the actor hang bound, and naming the AI seat honestly.

THE HANG. The Aug 3 batch parked completely: all 25 games froze mid
`[PLAN] Planning main1` awaiting a DeepSeek actor call that never returned,
and 53 minutes later not one had finished a turn. Cause: the adapter built
its OpenAI client with NO timeout, inheriting the SDK default of 600s per
request with 2 retries — up to thirty minutes for a single call.

The strategist has had a deadman since May (60s no-chunk / 120s hard cap)
because a 28-minute hang killed a game back then. The ACTOR never got one:
it is non-streaming, so there are no inter-chunk gaps to watch. Bounding it
at the TRANSPORT covers all ~10 `asyncio.to_thread` call sites at once
instead of needing a wrapper around each.

THE NAMING. The AI seat was hardcoded "Claude" regardless of who was
playing, so every log line and Discord message said Claude while DeepSeek
made the decisions. Safe to vary because the AI is identified by
`Player.is_claude`, never by name.
"""
import pytest

from rules.llm_adapter import (create_deepseek_adapter,
                               create_deepseek_reasoner_adapter)


def _adapter(factory, **kw):
    pytest.importorskip("openai")
    return factory(api_key="test-key-not-used", **kw)


class TestActorRequestIsBounded:
    def test_actor_has_a_finite_timeout(self):
        """The whole fix. Without an explicit timeout the SDK default is
        600s x 3 attempts = 30 minutes, which is indistinguishable from a
        hang when 25 games share the endpoint."""
        a = _adapter(create_deepseek_adapter)
        assert a._request_timeout is not None
        assert a._request_timeout <= 120, (
            "an actor call that can outlast a whole turn is a hang")

    def test_worst_case_per_call_is_bounded_well_under_the_old_default(self):
        a = _adapter(create_deepseek_adapter)
        worst = a._request_timeout * (a._max_retries + 1)
        assert worst <= 300, f"worst case {worst}s"
        assert worst < 600 * 3, "must beat the SDK default it replaced"

    def test_timeout_reaches_the_http_client_not_just_the_attribute(self):
        """Storing the number without passing it to OpenAI() would look
        identical from outside and fix nothing."""
        a = _adapter(create_deepseek_adapter)
        client_timeout = a._openai_client.timeout
        assert client_timeout is not None
        secs = getattr(client_timeout, "read", client_timeout)
        assert float(secs) == float(a._request_timeout)

    def test_strategist_is_bounded_too_but_allowed_longer(self):
        """A thinking-mode memo legitimately runs long and has its own
        streaming deadman. The transport cap here is the backstop for what
        the deadman cannot see — a request that never opens a stream, which
        is exactly how the Aug 3 hang presented."""
        actor = _adapter(create_deepseek_adapter)
        strat = _adapter(create_deepseek_reasoner_adapter)
        assert strat._request_timeout > actor._request_timeout
        assert strat._request_timeout <= 300

    def test_qwen_adapters_are_bounded_as_well(self, monkeypatch):
        """A second provider must not reintroduce the unbounded default.

        Asserting `is not None` is NOT enough and the sweep proved it: if
        create_dashscope_adapter stops forwarding request_timeout, the
        adapter silently falls back to the constructor DEFAULT, which is a
        real number — so the bound looks intact while the strategist quietly
        loses its longer window. Assert the value that only forwarding can
        produce.
        """
        pytest.importorskip("openai")
        monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key-not-used")
        from rules.llm_adapter import (create_qwen_actor_adapter,
                                       create_qwen_strategist_adapter)
        actor = create_qwen_actor_adapter()
        strat = create_qwen_strategist_adapter()
        assert actor is not None and strat is not None
        for a in (actor, strat):
            assert a._request_timeout is not None
            assert a._request_timeout <= 300
            secs = getattr(a._openai_client.timeout, "read",
                           a._openai_client.timeout)
            assert float(secs) == float(a._request_timeout), (
                "the timeout must reach the HTTP client, not just the attr")
        assert strat._request_timeout > actor._request_timeout, (
            "the strategist's longer window only exists if the factory "
            "actually forwards request_timeout")

    def test_retries_are_limited(self):
        """max_retries multiplies the timeout. The SDK default of 2 turns a
        90s cap into 270s of blocking."""
        a = _adapter(create_deepseek_adapter)
        assert a._max_retries <= 1


class _StubCog:
    """Just enough of MTGGameCog for the naming helper."""
    def __init__(self, provider="deepseek", deepseek=True, qwen=False):
        self._active_provider = provider
        self._deepseek_adapter = object() if deepseek else None
        self._qwen_adapter = object() if qwen else None

    ai_player_name = None  # bound below


def _cog(**kw):
    from mtg.cog import MTGGameCog
    c = _StubCog(**kw)
    c.ai_player_name = MTGGameCog.ai_player_name.__get__(c, _StubCog)
    c._get_ai_label = MTGGameCog._get_ai_label.__get__(c, _StubCog)
    return c


class TestAIPlayerNaming:
    def test_names_the_provider_actually_playing(self):
        assert _cog().ai_player_name() == "Deepseek"

    def test_follows_a_provider_switch(self):
        assert _cog(provider="qwen", qwen=True).ai_player_name() == "Qwen"

    def test_force_claude_still_says_claude(self):
        assert _cog().ai_player_name(force_claude=True) == "Claude"

    def test_falls_back_to_claude_with_no_alt_provider(self):
        assert _cog(deepseek=False).ai_player_name() == "Claude"

    def test_qwen_selected_but_unconfigured_does_not_claim_qwen(self):
        """_active_provider could be stale relative to the adapters."""
        assert _cog(provider="qwen", qwen=False).ai_player_name() == "Deepseek"

    def test_player_name_is_a_single_token(self):
        """It lands in commander-damage keys, log lines and message
        interpolation, so it must not carry the parentheses and spaces
        _get_ai_label uses for its human-readable label."""
        name = _cog().ai_player_name(openrouter_model="qwen/qwen3.7-flash")
        assert name == "OpenRouter"
        assert " " not in name and "(" not in name
        label = _cog()._get_ai_label(False, openrouter_model="qwen/qwen3.7-flash")
        assert "(" in label, "the LABEL keeps its detail; only the NAME is bare"

    def test_autoplay_no_longer_hardcodes_the_seat_name(self):
        from pathlib import Path
        src = (Path(__file__).resolve().parent.parent
               / "mtg" / "autoplay.py").read_text(encoding="utf-8")
        assert 'player2_name="Claude"' not in src
        assert "ai_player_name(force_claude, openrouter_model)" in src

    def test_cube_draft_follows_the_seat_name(self):
        """The autodraft is part of the same batch and had its own three
        hardcoded 'Claude' strings."""
        from pathlib import Path
        src = (Path(__file__).resolve().parent.parent
               / "cube_draft.py").read_text(encoding="utf-8")
        assert 'player2_name="Claude"' not in src
        assert 'self._format_deck_summary("Claude"' not in src
        assert "player2_name=claude_seat_obj.name" in src

    def test_rick_is_still_rick(self):
        """The pretend-HUMAN seat keeps its name — which model drives him is
        implied by his opponent."""
        from pathlib import Path
        for mod in ("mtg/autoplay.py", "cube_draft.py"):
            src = (Path(__file__).resolve().parent.parent
                   / mod).read_text(encoding="utf-8")
            assert "Rick Deckard" in src
