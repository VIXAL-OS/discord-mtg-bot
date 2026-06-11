"""Engine event bus — the pub/sub seam for trigger dispatch.

WHY THIS EXISTS (June 10, 2026)
-------------------------------
The engine has grown ~12 hand-wired trigger-watcher scans (creature-enters,
enchantment-enters, dies, LTB, attack, upkeep, end-step, cast, landfall,
beginning-combat, gain-life, combat-damage-to-player), each invoked from
specific call sites. The June 10 audits showed the recurring bug class this
produces: a watcher CLASS doesn't exist yet (gain-life before this sprint,
constellation, snow-permanent-enters), or a mutation path forgets to invoke
an existing scan (sacrifice-as-cost skipping dies triggers, undying returns
skipping self-ETBs). Pub/sub inverts that: mutation sites emit semantic
events once; trigger scans subscribe. A new trigger class becomes a
subscription, not a call-site hunt.

This was originally deferred to the React-frontend work (the frontend needs
a state-change -> websocket stream, so one event spine could serve both).
Decision June 10: start now, incrementally — the audit treadmill is paying
the no-bus tax every batch. The frontend can subscribe to the same bus later.

MIGRATION PLAN (one slice per audit cycle; batches are the regression net)
---------------------------------------------------------------------------
  Slice 1 (DONE, this file): LIFE_GAINED — emitted by combat.apply_life_gain
      (already the single centralized gain path), consumed by the gain-life
      trigger scan (Vito / Heliod / Pridemate). Existing pytest coverage of
      apply_life_gain now exercises the bus end-to-end.
  Slice 2: PERMANENT_ENTERED — emit from the cast path, move_card battlefield
      entry, token creation, reanimate, undying/persist returns, and land
      drops; migrate the creature-enters + enchantment-enters scans onto it
      and add the snow-permanent watcher (Marit Lage's Slumber scry — the
      known-open deferral this unlocks). Keep the old scans as parallel
      assertions for one batch before deleting.
  Slice 3: CREATURE_DIED — replace the _recently_died list + drain plumbing.
      APNAP ordering stays with the CONSUMER (helpers.apnap_order_died).
  Slice 4+: CARD_CAST, COMBAT_DAMAGE_DEALT, PHASE_CHANGED — at which point
      the React frontend's websocket layer is just another subscriber.

CONTRACTS
---------
- Synchronous, in-order dispatch (registration order). No async handlers —
  sync emit sites (SBA, combat) must stay sync; an async trigger class
  should subscribe a QUEUER (e.g. engine._queue_async_trigger) not a coro.
- Handlers take (game, **payload) and return None. They surface display
  text via game._pending_messages (the existing cross-system channel) so
  emit sites never need signature changes.
- No exception swallowing here: a broken handler should fail loudly in
  strict batches; production crash barriers live at the call sites that
  already have them.
- Registration is idempotent per (event, handler) so module re-imports
  can't double-fire triggers.
"""
from typing import Any, Callable, Dict, List

# Event types — flat string constants, grep-able in logs and code.
LIFE_GAINED = "life_gained"
# Planned: PERMANENT_ENTERED, CREATURE_DIED, CARD_CAST, COMBAT_DAMAGE_DEALT,
# PHASE_CHANGED (see migration plan above).

_subscribers: Dict[str, List[Callable]] = {}


def subscribe(event_type: str, handler: Callable[..., Any]) -> None:
    """Register a handler for an event type. Idempotent per (event, handler)."""
    handlers = _subscribers.setdefault(event_type, [])
    if handler not in handlers:
        handlers.append(handler)


def emit(event_type: str, game, **payload) -> None:
    """Dispatch an event to all subscribers, synchronously, in order."""
    for handler in list(_subscribers.get(event_type, ())):
        handler(game, **payload)


def subscriber_count(event_type: str) -> int:
    """Test/diagnostic helper."""
    return len(_subscribers.get(event_type, ()))
