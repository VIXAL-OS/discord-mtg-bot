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
  Slice 2 (STARTED July 20, 2026): PERMANENT_ENTERED — emitted from the cast
      paths (async + sync), play_land, suspend resolution, the noncast entry
      funnel (move_card / create_token / reanimate / mass_flicker), and
      undying/persist death-save returns. Consumers so far: the snow-
      permanent watcher (Marit Lage's Slumber scry — the known-open deferral
      this unlocked) and the PARITY RECORDER (mtg/triggers.py): the existing
      creature-enters + enchantment-enters scans stay authoritative and are
      instrumented to record which entries they saw; engine.end_turn reports
      `[EVENT-PARITY]` for any creature/enchantment entry the scans missed.
      One clean batch (zero parity lines) is the gate for flipping the scans
      into subscribers and deleting the direct calls (slice 2b).
      GATE STATUS: CLEARED July 21, 2026 — batch game_15291* (143 games,
      all on >= 6b189ce, strict=1) came back with ZERO [EVENT-PARITY]
      lines. Slice 2b landed the same day: both scans (creature-enters +
      enchantment-enters) are now bus subscribers, the ~12 direct call
      sites drain game._pending_messages in place, and the parity recorder
      is INVERTED (a line now means a subscriber skipped an entry —
      unusable engine ref in the payload, [ETB-BUS] tag).
  Slice 3a (SHADOW, July 21, 2026): CREATURE_DIED emitted by the
      queue_death choke-point (mtg/triggers.py) that wraps every raw
      `_recently_died.append/extend`; the only consumer is the death
      parity recorder ([EVENT-PARITY-DIES], reported from end_turn;
      deaths still pending in the queue are excluded — not yet drained is
      not a miss). No consumer changes by construction: the dies
      dispatcher, wave semantics (_active_dies_batch), and
      helpers.apnap_order_died are untouched.
      GATE STATUS: CLEARED July 23, 2026 — batch game_15296* (152 games,
      all on >= fd86e3d, strict=1) came back with ZERO [EVENT-PARITY-DIES]
      lines.
  Slice 3b (DONE, July 23, 2026): the dies queue is now BUS-FED. queue_death
      dropped its direct `_recently_died.append` and is emit-only; the append
      moved into `_accumulate_death_subscriber` (mtg/triggers.py) — the sole
      sanctioned appender, registered on CREATURE_DIED before the parity
      recorder. The subscriber only ACCUMULATES (never resolves inline)
      because the dies consumer has batch-level semantics a per-event handler
      can't carry: wave separation (the dispatcher resets _recently_died to []
      before draining, so a wave's collateral deaths land in the fresh list as
      the next wave) and APNAP batch ordering. The dispatcher, _active_dies_batch,
      apnap_order_died, and _dies_source_ids_by_dead_id (call-site-populated) are
      all UNCHANGED. The parity recorder stays in place with INVERTED meaning (a
      line now means the bus->subscriber->dispatcher path skipped a death); one
      clean [EVENT-PARITY-DIES]=0 batch gates removing it (slice 3c cleanup).
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
# PERMANENT_ENTERED payload: card=<entering Card>, controller=<Player>,
# via=<entry-path tag: cast/cast_sync/land_drop/suspend/move_card/
# create_token/living_weapon/reanimate/mass_flicker/death_save_return>,
# rules=<RulesEngine or None — handlers that execute actions need it>.
# Emitted ONCE per physical entry (Panharmonicon doubling is a trigger-level
# concept, not a second entry). The cast emit fires AFTER resolution settles
# — post aura-fizzle, post clone-copy — never at the raw battlefield append
# (July 20: a fizzled Draconic Destiny and a Clever Impersonator copying a
# devotion-gated god both produced false parity lines from an append-time
# emit on the first live batch).
PERMANENT_ENTERED = "permanent_entered"
# CREATURE_DIED payload: card=<dead Card>, player=<controller Player>.
# Slice 3b (July 23, 2026): the dies queue is BUS-FED — queue_death
# (mtg/triggers.py) is emit-only and _accumulate_death_subscriber does the
# _recently_died append (synchronously, in registration order, so callers see
# the queue populated the instant queue_death returns). The dies dispatcher,
# wave semantics (_active_dies_batch), and helpers.apnap_order_died remain the
# CONSUMER — the subscriber only accumulates. The parity recorder
# ([EVENT-PARITY-DIES]) stays until slice 3c after a clean batch.
CREATURE_DIED = "creature_died"
# Planned: CARD_CAST, COMBAT_DAMAGE_DEALT, PHASE_CHANGED
# (see migration plan above).

_subscribers: Dict[str, List[Callable]] = {}


def subscribe(event_type: str, handler: Callable[..., Any]) -> None:
    """Register a handler for an event type. Idempotent per (event, handler)."""
    handlers = _subscribers.setdefault(event_type, [])
    if handler not in handlers:
        handlers.append(handler)


def unsubscribe(event_type: str, handler: Callable[..., Any]) -> None:
    """Remove a handler. No-op when absent (test-teardown friendly)."""
    handlers = _subscribers.get(event_type)
    if handlers and handler in handlers:
        handlers.remove(handler)


def emit(event_type: str, game, **payload) -> None:
    """Dispatch an event to all subscribers, synchronously, in order."""
    for handler in list(_subscribers.get(event_type, ())):
        handler(game, **payload)


def subscriber_count(event_type: str) -> int:
    """Test/diagnostic helper."""
    return len(_subscribers.get(event_type, ()))
