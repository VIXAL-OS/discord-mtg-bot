# Human Multiplayer and Crash-Safe Resolution Roadmap

Status: Q-H headless command/display checkpoint, August 16, 2026; live cube
FFA smoke next.

This document separates two projects that share persistence primitives but
have very different risk profiles:

1. a production 3–4-human Discord free-for-all surface; and
2. exact CR 608 recovery after the Python process dies while a spell or ability
   is resolving.

The lobby/manual surface is a moderate feature. Crash-safe resolution is a
larger architectural project because the current continuation lives inside an
`asyncio` coroutine and cannot be reconstructed from a save file.

## Current boundary

The engine already has arbitrary-seat `GameState` construction, stable seat
IDs, elimination, cyclic turns/APNAP, per-attacker defending seats, durable
priority, private choices, and a headless four-seat cube FFA smoke. Before Q-G,
the public `!game` command still constructed only two players.

Q-G now provides a beta human lobby:

- `!game create <format> <3|4>` creates a public-thread lobby;
- `!join`, `!leave`, `!lobby`, `!ready [off]`, `!cancelgame`, and `!startgame`;
- one stable lobby seat per Discord user ID; duplicate display names get a
  unique game-facing seat suffix;
- deck JSON is bound per seat and any deck change clears readiness;
- lobby, seat, deck, and readiness records are atomically persisted beneath
  `data/lobbies/` and reload when the cog starts;
- `!startgame` loads every deck through the normal commander/partner/
  companion/signature-spell path and runs format validation;
- random first player and existing multiplayer starting-life/first-turn-draw
  rules;
- private opening-hand delivery with explicit DM-failure recovery;
- multiplayer gameplay stays locked until every living player has kept;
- mulligan count and keep state now survive save/load;
- `!resumegame` reconstructs the runtime priority system instead of restoring
  only inert serialized priority dictionaries.

The Q-H headless conversions now present are:

- multiplayer `!attack` requires an explicit defender per group, for example
  `!attack Ragavan at @Alice; Goblin Guide, Swiftspear at @Bob`;
- attack tax validation uses that exact defender before payment;
- only the assigned defending seat may block each attacker;
- every attacked seat independently finalizes with `!doneblocking` or
  `!noblock`, and damage waits for all of them;
- `!gg` eliminates one multiplayer seat and lets the game continue until one
  living player remains;
- the operational priority ring uses durable `seat:N` identities, while
  Discord names are presentation labels only; version-1 name-keyed saves are
  migrated on restore;
- `!priority`, `!pass`, `!respond`, F6, hold, reconnect, gateway reconnect,
  cast windows, combat windows, elimination, and turn rollover share that
  stable identity contract;
- the visual board allocates a disjoint vertical area to every 3–4-player
  seat instead of painting seats 2–4 over seat 1, and text/image displays mark
  eliminated seats;
- core manual mutation paths reject eliminated seats, while read-only state,
  board, graveyard, exile, and priority views remain spectator-safe;
- manual damage/life/debug resolution no longer silently assumes
  `1 - player_idx`; ambiguous multiplayer `opponent` damage is rejected before
  mutation and exact mentions, names, or seat numbers are accepted;
- lobby start and `!reconnect` best-effort unarchive/rejoin the Discord thread
  before private prompt recovery.

This is still beta. A normal save, Discord gateway reconnect, or bot restart
outside active CR 608 execution is recoverable. A full process death during the
few seconds in which a cast coroutine is applying a spell remains unsafe.

## Q-G — Lobby, decks, readiness, and private mulligans

Deliverables:

- Durable lobby/thread record, owned by a Discord user ID.
- Three- or four-seat capacity chosen at creation.
- Stable, non-reused seat identifiers while the lobby is open.
- Per-seat deck submission and validation, including commander, partner,
  Background, companion, and Oathbreaker signature-spell data.
- Readiness invalidated whenever that seat changes decks.
- Host-only start/cancel; host cannot silently abandon ownership with `!leave`.
- Random first player, format starting life, and multiplayer first-turn draw.
- Private opening hands and London mulligan prompts.
- DM failure never publishes a hand; the user enables DMs and retries `!hand`.
- Lobby survives process restart and is deleted only on start or cancellation.

Exit gate: direct persistence/authorization/adverse tests plus the full private
suite. After this slice, run one four-seat cube FFA autotest because `Player`
and `GameState` persistence fields changed even though cube does not use the
Discord lobby.

## Q-H — Human multiplayer command surface

Already implemented at this checkpoint: defender-specific `!attack`, exact
block authorization, per-defender completion, continuing multiplayer
concessions, stable-seat priority identity and legacy-save migration,
non-overlapping N-seat board rendering, thread membership repair, explicit
multiplayer damage targets, and eliminated-seat guards on the core mutation
surface.

Remaining work:

- finish the long-tail manual mutation audit (pregame/format-specific helpers
  and emergency commands) and add direct command pins where coverage is thin;
- make `!turn`, results, pending prompts, and consecutive human/AI handoffs
  consistently describe and advance every living seat;
- add planeswalker/battle defenders and commander-damage UX;
- perform a real three- or four-human Discord pilot.

Checkpoint gate: `tests/test_aug16_human_multiplayer_surface.py` plus the
existing lobby/FFA/priority suites are **43 focused tests clean**. The full
private suite is **2480 passed in 160.03s** (`pytest tests -q`). Because
`GameState` priority persistence, stack interaction, elimination, and the
shared board surface changed, the next evidence should be one four-seat cube
FFA autotest before continuing the remaining Q-H UX or starting Q-I.

The 1v1 syntax and behavior must remain unchanged. Teams, Two-Headed Giant,
range of influence, and an eight-player rules game are out of scope.

## Why a resolving spell cannot currently survive process death

The current cast path is coroutine-owned:

```text
cast coroutine
  -> pay costs
  -> create StackEntry
  -> await priority event
  -> dispatch resolution
  -> route the card
```

`StackEntry` currently persists only a card name/ID, controller name/index,
stringified target, basic spell/trigger fields, and counter status. The
`asyncio.Event`, exact object references, local variables, and the coroutine
continuation disappear on process death. Restoring the priority ring can wake
future commands, but there is no task left to continue after the wait.

The unsafe response is to deserialize the current dictionary and claim the
stack recovered. That would restore display state without restoring CR 608
execution.

## Q-I — Serializable stack jobs and restart-safe priority waits

Introduce a durable `ResolutionJob` keyed by a stable stack-entry ID. Persist:

- full card and active-face snapshot;
- controller stable seat ID;
- exact target player/card/stack IDs;
- chosen modes and their order;
- X value, kicker, entwine, alternative/additional costs, and payment facts;
- cast origin, ownership, and final destination policy;
- spell/ability/copy status, counter status, and source trigger data;
- the current resolution checkpoint;
- applicable replacement-effect IDs and unresolved private choice IDs.

Reconstruct real `StackEntry` and target objects from stable IDs on load. A
missing/stale target must remain missing and follow normal resolution legality;
it must never fall back to a display name or a same-named card.

Move the wait owner from the cast coroutine to one central stack coordinator.
The coordinator observes durable priority completion and advances the top job.

### Aug 17 implementation checkpoint

Q-I's persistence half is implemented. `ResolutionJob` now carries a complete
card/active-face snapshot, stable controller seat, exact player/card/stack
target references, cast facts, modes/additional costs, destination policy,
and checkpoint state. `GameState.from_dict()` reconstructs real `StackEntry`
objects in two passes, so counter-war targets can bind only after the entire
stack exists. A stale reference remains `None`; duplicate display/card names
are never recovery fallbacks.

`ResolutionCoordinator` owns new priority waits, records `on_stack`,
`priority_open`, `resolving`, and `complete`, and rebuilds runtime events after
load. Saves publish through a flushed/fsynced sibling plus `os.replace`, so a
process death cannot truncate the only snapshot. Seven Q-I pins cover exact
four-seat/card/stack identity, stale targets, split/X/kicker/entwine facts,
spliced cards, trigger entries, runtime-event rebinding, monotonic checkpoints,
and atomic saves.

This is deliberately **not** a crash-safe CR 608 claim. A restored job at
`resolving` is preserved honestly, but Q-J must persist and idempotently replay
the action plan, choices, trigger/SBA event IDs, and narration before it can
advance. Run a four-seat FFA smoke after this shared stack-path change, then
start Q-J only if that gate is clean.

## Q-J — Idempotent CR 608 state machine and narration outbox

Use explicit checkpoints:

```text
costs_paid
  -> on_stack
  -> priority_open
  -> resolving
  -> effects_applied
  -> card_routed
  -> SBAs_and_triggers_done
  -> complete
```

Each transition must be atomic and idempotent. Re-entering a checkpoint after
a crash may verify prior work or finish it, but it must never draw twice, deal
damage twice, duplicate tokens/counters, route the card twice, or enqueue the
same trigger twice.

Requirements:

- persist a deterministic Tier 1/1.5/2 action plan before applying it;
- persist Tier 3's returned action plan before the first action executes;
- never re-query Tier 3 during recovery, because a second response can differ;
- give every action a resolution-job/idempotency key and persist applied keys;
- persist replacement choices, applicable effect IDs, choice ownership, and
  ordering before waiting;
- add a durable Discord narration outbox with stable message IDs and sent
  acknowledgements so restart neither duplicates nor omits visible events;
- route SBAs and triggers from persisted event IDs, not only in-memory queues.

The durable save boundary should be a small transaction/journal abstraction,
not scattered calls to `save_game()` between mutations. A database is not
strictly required for the first implementation; append-only JSON journal plus
atomic snapshots can prove the protocol, but SQLite is likely the cleaner
single-process production store once outbox and checkpoints exist.

## Q-K — Crash injection and human pilot

Add deterministic process-death injection immediately before and after every
checkpoint. The suite must cover:

- a simple permanent and instant/sorcery;
- targeted and targetless spells;
- fizzles and counters;
- nested responses and a counter-war;
- modal, X, kicked, entwined, copied, and cast-from-other-zone spells;
- Tier 3 resolution using the already-persisted action plan;
- replacement-order and simultaneous/private choices;
- ETB/dies/cast triggers and SBA deaths created during resolution;
- narration delivered before/after the crash boundary;
- repeated restart at the same checkpoint.

For every injected crash, the recovered final state and narration inventory
must be byte-/event-equivalent to the uninterrupted control, with every
idempotency key applied exactly once.

Only after that suite is green should the beta label be removed and a real
three- or four-human Discord pilot test process death during live stack wars.

## Slice order and stop conditions

| Slice | Deliverable | Autotest/pilot checkpoint |
|---|---|---|
| Q-G | Lobby, joins, decks, readiness, private mulligans | Four-seat cube FFA smoke, then private suite |
| Q-H | Full manual multiplayer commands and results | Cube FFA smoke plus 3–4-human Discord pilot |
| Q-I | Serializable stack entries/jobs and restart-safe waits | Headless restart tests; no safety claim yet |
| Q-J | Idempotent resolution, persisted Tier 3 plans, outbox | Full crash matrix and cube FFA smoke |
| Q-K | Crash-injection suite and live pilot | Remove beta only after equivalence gates |

Stop rather than merging a partial recovery state machine if exact targets,
effect idempotency, replacement choices, or narration cannot be recovered. A
smaller honest unsafe window is preferable to a system that appears recovered
while silently duplicating or omitting game actions.
