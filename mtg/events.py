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
  Slice 2c (DONE July 24, 2026): the ETB parity recorder retired after a
      SECOND clean batch post-flip (game_15299*, 152 games, strict=1,
      [EVENT-PARITY]=0). [ETB-BUS] remains the emit-side net;
      tests/test_slice2b_bus_dispatch.py pins end-to-end dispatch.
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
      all UNCHANGED. The parity recorder stayed in place with INVERTED meaning
      (a line meant the bus->subscriber->dispatcher path skipped a death).
  Slice 3c (DONE July 24, 2026): the death parity recorder retired — the
      post-3b batch (game_15299*, 152 games, strict=1, vintage verified on
      >= 2d93819) returned [EVENT-PARITY-DIES]=0. The structural pin
      (tests/test_slice3a_death_shadow.py: no raw _recently_died mutations
      outside _accumulate_death_subscriber) remains as the permanent net.
  Slice 4a (SHADOW, July 24, 2026): CARD_CAST emitted at the two live cast
      funnels — _await_stack_window (mtg/spells.py, via="cast": every
      cast_spell_async cast — hand, response, adventure half, flashback,
      commander, signature spell) and the cascade free-cast
      (mtg/triggers.py, via="cascade"). The only consumer is the parity
      recorder ([EVENT-PARITY-CAST], reported from end_turn); the
      _check_cast_triggers scan stays authoritative and records what it
      saw. Emission counts are compared PER CAST (a card object cast twice
      in one turn — adventure half then creature — needs two scan records).
      SYNC CAST SITES (same day, 7ba7ad6): suspend resolution, Etali free
      casts, template "cast ... for free" moves, and the legacy sync cast
      now go through triggers.queue_cast_triggers_sync — battlefield cast
      triggers queue for the async Tier-3 drain ([CAST-TRIGGER-SYNC]; a
      suspended Rift Bolt finally makes Talrand a Drake), and the helper
      emits CARD_CAST with a PAIRED parity record, so the zero gate holds.
      That closes the '4b sync gaps' item early.
  Slice 4b (July 26, 2026): game_15304* returned [EVENT-PARITY-CAST]=0 on
      post-4a code, so the recorder (_record_cast_for_parity,
      report_cast_parity, GameState._cast_events/_cast_scanned_ids, the
      end_turn call, the subscribe line, the scan-side recording) is
      RETIRED. **The consumer deliberately did NOT move onto the bus.**
      _check_cast_triggers is async and needs `await` for Tier-3
      resolve_effect, the cascade free-cast, its own recursion, and —
      decisively — engine._combat_priority_round, the
      [CAST-TRIGGER-PRIORITY] window that lets a Stifle counter a cast
      trigger (19 fires in that batch). Per CONTRACTS below the bus is
      sync-only, so subscribing a queuer would demote EVERY inline cast
      trigger (Talrand tokens, prowess, the whole counter-a-trigger
      interaction) to a Tier-3 drain — a real behaviour downgrade bought
      with nothing but uniformity.
      The migration's goals are met without the flip: CARD_CAST fires at
      every cast path (both async funnels + the sync bridge) — the "one
      spine, no missed call sites" property, which the parity gate proved
      — while CONSUMPTION differs by path: the async funnels consume
      directly (they ARE the funnel), the sync sites consume via
      queue_cast_triggers_sync. Because nothing subscribes to CARD_CAST
      now, tests/test_slice4a_cast_shadow.py is the net that stops the
      emission rotting unnoticed, and it also pins that no async handler
      gets subscribed in violation of the contract.
      Revisit this decision only if _check_cast_triggers loses its need to
      await (the test asserts that need explicitly).
  Slice 5b: COMBAT_DAMAGE_DEALT — LIVE (July 31, 2026; 5a shadow gate
      cleared at zero mismatches over batch 15324's 134 FS-step combats).
      Emitted at both damage-application funnels in mtg/combat.py;
      _accumulate_combat_damage_subscriber (mtg/triggers.py) is the sole
      appender for game._combat_damage_to_player (player-kind → the
      battlefield-watcher family + attacker self-trigger dispatch) and
      game._combat_damage_to_creature (creature-kind → the damaged-creature
      scan, Phyrexian Obliterator class). Both drains stay in
      resolve_combat_damage under the `not game.ended` gate (CR 104.2a) —
      the slice-3b accumulate-don't-resolve pattern. NONCOMBAT damage
      paths (spells, abilities) emit nothing yet; the damaged-creature scan
      therefore sees combat damage only — extending the event to those
      paths is its own future slice.
  Slice 6+: PHASE_CHANGED — shadow first, one slice per audit cycle. The
      React frontend's websocket layer is the intended next CARD_CAST
      subscriber, and it is sync-friendly (it only needs to serialize
      state), so it can attach without reopening 4b.

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
# CONSUMER — the subscriber only accumulates. (Slice 3c, July 24: the parity
# recorder retired after the post-3b batch came back clean.)
CREATURE_DIED = "creature_died"
# CARD_CAST payload: card=<cast Card>, caster=<Player>, via=<"cast" (the
# cast_spell_async funnel: hand/response/adventure/flashback/commander/
# signature) | "cascade" (the cascade free-cast) | "suspend" |
# "etali_free_cast" | "free_cast_move" | "cast_sync" (the four sync sites,
# emitted by triggers.queue_cast_triggers_sync)>, engine=<GameEngine>.
# Emitted ONCE per cast at announcement time (CR 601.2i — before the
# counter window; cast triggers fire even if the spell is countered).
# Slice 4b (July 26, 2026): the parity recorder is retired and CARD_CAST
# currently has NO subscriber. That is deliberate, not an oversight —
# _check_cast_triggers is async (it awaits the [CAST-TRIGGER-PRIORITY]
# Stifle window) and this bus is sync-only, so the async funnels consume
# directly while the sync sites consume via the Tier-3 queue. The emission
# is the migration's real deliverable (one spine, every cast path) and is
# pinned by tests/test_slice4a_cast_shadow.py so it cannot rot unnoticed.
# The React websocket layer is the intended next subscriber.
CARD_CAST = "card_cast"
# Slice 5a (July 30, 2026) — SHADOW. Emitted once per combat-damage
# APPLICATION at the two funnels in mtg/combat.py:
#   apply_combat_damage_to_player   (payload: source, target, amount,
#                                    target_kind="player")
#   apply_combat_damage_to_creature (target_kind="creature")
# The shadow recorder (mtg/triggers.py) diffs player-kind emissions against
# the attacker-loop's game._combat_damage_to_player appends — the list the
# [COMBAT-TRIGGER] dispatch consumes — and prints [EVENT-PARITY-CDD] from
# end_turn for either direction of mismatch. A bus-emission WITHOUT a
# consumer append means a damage path whose combat-damage triggers
# silently never fire (the first-strike step is the suspect); an append
# without an emission means a path bypassing the replacement/poison
# funnel. One clean batch gates 5b (flipping consumers onto the bus: the
# Obliterator class "whenever a source deals damage to THIS creature",
# battlefield-wide Ohran/Tovolar watchers, Player.dealt_combat_damage
# tracking).
COMBAT_DAMAGE_DEALT = "combat_damage_dealt"
# Slice 6a (July 31, 2026 — SHADOW): emitted by GameState.set_phase, the ONE
# sanctioned way to change game.phase (a structural pin forbids raw
# assignments). Payload: old_phase, new_phase, via (the site's name), so the
# 6b flip knows every entry path. The shadow recorder pairs entries into
# HOOKED phases (MAIN1/MAIN2 → dispatch_main_phase_triggers, UPKEEP → the
# upkeep scan) with actual hook runs and prints [EVENT-PARITY-PHASE] from
# end_turn for any entry whose hooks never ran — the direct-phase-set class
# that produced the Tymna bug three times over. One clean batch gates 6b.
PHASE_CHANGED = "phase_changed"

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
