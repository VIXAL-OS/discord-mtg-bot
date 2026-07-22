"""July 20 — adaptive strategist degrade plumbing.

The July 12-13 batch hit 248 deadman/hard-cap fires (healthy baseline 0-2)
on a bad-DeepSeek day. The degrade: after 2 fires in a game, that game's
remaining strategist calls pass reasoning_effort='low' per-call. These tests
pin the adapter half (per-call override beats the adapter default) and the
declared per-game state.
"""
from types import SimpleNamespace

import pytest


class _StubCompletions:
    def __init__(self):
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        msg = SimpleNamespace(content="ok", reasoning_content=None)
        usage = SimpleNamespace(prompt_tokens=1, completion_tokens=1,
                                prompt_cache_hit_tokens=0,
                                prompt_cache_miss_tokens=1)
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)],
                               usage=usage)


def _adapter(**ns_kwargs):
    from rules.llm_adapter import _MessagesNamespace
    stub = _StubCompletions()
    client = SimpleNamespace(chat=SimpleNamespace(completions=stub))
    ns = _MessagesNamespace(client, **ns_kwargs)
    return ns, stub


class TestPerCallEffortOverride:
    def test_override_beats_adapter_default(self):
        ns, stub = _adapter(reasoning_effort="medium")
        ns.create(messages=[{"role": "user", "content": "hi json"}],
                  json_mode=False, reasoning_effort="low")
        assert stub.calls[-1]["reasoning_effort"] == "low"

    def test_adapter_default_used_without_override(self):
        ns, stub = _adapter(reasoning_effort="medium")
        ns.create(messages=[{"role": "user", "content": "hi json"}],
                  json_mode=False)
        assert stub.calls[-1]["reasoning_effort"] == "medium"

    def test_no_effort_kwarg_when_neither_set(self):
        ns, stub = _adapter()
        ns.create(messages=[{"role": "user", "content": "hi json"}],
                  json_mode=False)
        assert "reasoning_effort" not in stub.calls[-1]


class TestPerGameDegradeState:
    def test_declared_fields_default_clean(self, game):
        assert game._strategist_fires == 0
        assert game._strategist_degraded is False
