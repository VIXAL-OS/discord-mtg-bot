"""Regression pins from the first genuine Qwen autoplay batch (sha=cce6220)."""

import asyncio


def test_tier3_optional_payment_guard_does_not_crash(rules, game):
    """A removed local left every nondeterministic Tier-3 resolve crashing.

    The full cce6220 batch aborted 46/160 games at ``effect_lower_guard`` before
    reaching the model.  A truthy sentinel client is enough to enter the
    guard; zero available mana makes it return deterministically before any
    network call.
    """
    rules.client = object()

    messages, actions = asyncio.run(rules.resolve_effect(
        game,
        "When this creature dies, you may pay any amount of {R}.",
        source_card="Leyline Tyrant",
        controller="Rick",
    ))

    assert actions == []
    assert any("optional cost declined" in message for message in messages)
