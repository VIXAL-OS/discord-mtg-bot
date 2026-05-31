# ARCHITECTURE

Contributor-facing technical overview of `discord-mtg-bot`. Read this before working on the engine or the support system. For setup / how-to-use, see [README.md](README.md).

## Project layout

```
discord-mtg-bot/
├── bot.py                    # Discord bot scaffolding, persona loader, card lookups, light chat
├── board_visual.py           # Card image rendering for !board / !state
├── cube_draft.py             # 8-seat cube draft engine + !autodraft test
├── prewarm_cache.py          # Pre-fetch Scryfall card data
├── mtg_game.py               # Backward-compat shim — re-exports from mtg/
├── mtg/                      # MTG game engine package
│   ├── __init__.py
│   ├── util.py               # GameLogger, StdoutTee, StderrTee
│   ├── constants.py          # Phase/Zone enums, format rules, banned lists
│   ├── helpers.py            # Module-level helpers shared by engine + cog
│   ├── models.py             # Card, Player, GameState, StackEntry, FormatValidator
│   ├── deck_loader.py        # Scryfall + Archidekt + JSON deck loaders
│   ├── display.py            # GameDisplay (Discord embed/text formatters)
│   ├── claude_player.py      # ClaudePlayer (strategist + actor)
│   ├── rules_engine.py       # RulesEngine (orchestration)
│   ├── actions.py            # Action interpreter (81 action types)
│   ├── judge.py              # Tier 3 LLM judge
│   ├── sba.py                # State-based actions (CR 704)
│   ├── combat.py             # Combat damage + lifelink + replacement-effect routing
│   ├── triggers.py           # ETB/dies/LTB/attack/upkeep/end-step/cast triggers
│   ├── spells.py             # cast_spell_async, resolve_special_effects, suspend, sagas
│   ├── ai_turn.py            # execute_claude_turn, plan validation
│   ├── autoplay.py           # Pretend-human "Rick" autoplay loop
│   ├── coverage.py           # Tier coverage classifier (!coverage command)
│   ├── engine.py             # GameEngine (turn loop)
│   └── cog.py                # MTGGameCog + Discord command handlers
├── rules/                    # Rules-correctness subsystem
│   ├── effect_templates.py   # ~370 card templates + ~80 pattern families
│   ├── spell_resolver.py     # Spell-cast orchestration
│   ├── targeting.py          # Target validation (hexproof, protection, ward)
│   ├── targeting_helpers.py  # Card → Targetable adapter
│   ├── mana.py               # Mana cost parser (hybrid, phyrexian, snow)
│   ├── state_based_actions.py
│   ├── layers.py             # CR 613 layer system
│   ├── replacement.py        # Replacement effects
│   ├── priority.py           # Async priority + stack + APNAP
│   ├── planeswalker.py       # PW ability parsing + activation
│   ├── xmage_bridge.py       # Python client for XMage Java bridge
│   ├── xmage_action_translator.py  # XMage ability text → JSON actions
│   ├── XMageRulesBridge.java # 978-line JSON-RPC bridge
│   └── llm_adapter.py        # OpenAI-compatible adapter (DeepSeek, OpenRouter)
├── personas/                 # Character-layer JSON files (config-swappable)
│   ├── plain.json
│   ├── ressapanda.json
│   └── README.md
├── data/                     # Decks + card-data cache
│   ├── card_data_cache.json
│   ├── surrak_stompy.json    # The "stompy" example deck
│   ├── test_*.json           # Rules-engine stress-test decks
│   └── ...
├── docs/                     # Long-form docs (audit reports, design notes)
├── tools/                    # Maintenance scripts (cache audit, etc.)
├── config.json.example
└── requirements.txt
```

This repo is the **MTG-only** half of a split. The companion-bot half (distress detection, memory system, tarot, YouTube transcription, persona-driven support) lives at [`discord-companion-bot`](https://github.com/VIXAL-OS/discord-companion-bot). That repo can optionally import this one for combined deployments.

## Effect resolution architecture

Effects resolve through a tiered cascade. Always try to fix bugs at the **lowest** tier possible — Tier 3 costs tokens and adds ~2s latency, so it should be a safety net, not the primary resolution path.

### Tier 1 — Hardcoded handlers
`mtg/triggers.py` (`_check_creature_etb_triggers_sync`) and `mtg/spells.py` (`resolve_special_effects`). Best for specific cards or well-defined patterns where the math has to be exact (e.g., Heartless Hidetsugu rounding, Walking Ballista X-cost calculation).

### Tier 1.5 — Template library
`rules/effect_templates.py`. Two registries:
- `_card_templates` — exact card-name match (~370 cards)
- Pattern families — oracle-text regex (~80 families catching whole categories: "When X enters, draw N cards", "Whenever another creature enters, you gain N life", etc.)

Add a template here for any card whose effect resolves to a clear JSON-action sequence. See [`personas/README.md`](personas/README.md) for the JSON action format reference.

### Tier 2 — SpellResolver
`rules/spell_resolver.py` + `rules/effects.py`. Regex-based oracle-text parsing → EffectType enum → execution. Handles common patterns (damage, draw, destroy, exile, bounce, tokens, counters, pump, mill, sacrifice, fight) not caught by name templates.

### Tier 2.5 — XMage bridge
Java subprocess running XMage's card database (87,000+ cards) via JSON-RPC. `rules/xmage_bridge.py` is the Python client; `rules/XMageRulesBridge.java` is the bridge itself. Catches triggers the Python regex missed; gracefully degrades if Java/JAR is missing.

To rebuild: `cd rules/xmage-bridge && mvn package`

### Tier 3 — LLM judge
`mtg/judge.py:resolve_effect`. Sends game state + effect description to Claude; gets back structured JSON `{"explanation": ..., "actions": [...]}`. The actions go through `mtg/actions.py:execute_action_on_state` for execution.

**Note:** Since the May 25 audit, the LLM's prose explanation is dropped from Discord entirely (preserved in console log as `[RESOLVE-PROSE-DROPPED]` for audit). The bare `⚡ <source> triggers` line plus the downstream action emits (📦 zone, ⚔️ damage, 🩸 life, ⭕ counters) carry every state change.

### Tier 4 — Manual fallback
- `!resolve <description>` — manual Tier 3 trigger
- `!fix <natural-language>` — direct state mutation (move card, set life, etc.)
- `!judge <question>` — tries to execute; falls back to text ruling
- `!undo` — roll back the most recent risky command (depth 5)

## JSON action format

All tiers produce actions in this format. Full list in `mtg/actions.py`.

```json
{"action": "deal_damage", "amount": N, "target_player": "name"}
{"action": "deal_damage", "amount": N, "target_card": "name", "target_controller": "name"}
{"action": "draw_cards", "player": "name", "amount": N}
{"action": "gain_life", "player": "name", "amount": N}
{"action": "lose_life", "player": "name", "amount": N}
{"action": "destroy", "card": "Card Name"}
{"action": "move_card", "card": "X", "from_zone": "zone", "to_zone": "zone", "player": "name"}
{"action": "create_token", "player": "name", "name": "N", "power": P, "toughness": T, "types": "...", "count": N}
{"action": "add_counters", "card": "X", "counter_type": "+1/+1", "amount": N}
{"action": "tap", "card": "X"}
{"action": "untap", "card": "X"}
{"action": "add_mana", "player": "name", "color": "C", "amount": N}
{"action": "discard", "player": "name", "card": "Card Name" or "random"}
{"action": "destroy_by_power", "min_power": N}
{"action": "exile_all_by_type", "type": "creatures|artifacts|enchantments|planeswalkers"}
{"action": "exile_all_graveyards"}
{"action": "steal_permanent", "player": "name", "from_player": "name", "card": "X", "source": "source-name"}
{"action": "mass_flicker", "player": "name", "count": N, "exclude_self": "Card Name"}
{"action": "no_action", "reason": "why"}
```

## Console logging tags

```
[ETB-TEMPLATE]             Resolved by Tier 1.5 template library
[ETB-UNHANDLED]            Self-ETB that nothing handled (needs template)
[TRIGGER-TEMPLATE]         Creature-enters trigger resolved by templates
[TRIGGER-UNHANDLED]        Trigger queued for async auto-resolve
[TRIGGER-AUTO]             Auto-resolved via Claude API (Tier 3)
[SPELL_RESOLVER]           SpellResolver processed a spell
[XMAGE]                    Bridge lifecycle (start/stop/errors)
[XMAGE-ETB]                Self-ETB resolved via XMage + action translator
[XMAGE-TRIGGER]            Creature-enters trigger via XMage translator
[OPP-CAST-TRIGGER]         Opponent-cast trigger detected (Rhystic Study etc.)
[OPP-CAST-TRIGGER-TEMPLATE] Opponent-cast trigger resolved via template
[LANDFALL]                 Landfall trigger resolved
[COMBAT]                   Combat validation (defender, summoning sickness)
[PLAN-VALIDATE]            Plan pre-validation (mana, empty targets, board wipes)
[DAMAGE-PREVENTED]         Damage prevention flag check
[DEVOTION-CHECK]           Devotion-gated god resolved to NOT a creature
[RESOLVE-HIDETSUGU]        Hidetsugu activation took Tier 1 hardcoded path
[RESOLVE-PROSE-DROPPED]    Tier 3 trigger emit prose suppressed (audit-recoverable)
[QUELLER-EXILE-EMPTY]      Spell Queller re-ETB fizzled (empty stack)
[MASS-FLICKER-ETB]         ETB trigger fired during mass_flicker resolution
[DECK-COVERAGE]            Per-deck Tier 1.5/2/3 classification at [GAME-INIT]
[AUTOPLAY]                 Autoplay loop lifecycle
[AUTOPLAY-RESOLVE]         Autoplay manual resolution
[AUTOPLAY-JUDGE]           Tier 3 escalation with no state change (template candidate)
[STRATEGIST]               Background strategist activity
[CACHE-PREFIX]             Per-game cache diagnostic
[CALL-BREAKDOWN]           Per-purpose LLM call counter
[STATS-GAME]               Per-game cost summary
[STATS-GAME-ABORTED]       Per-game cost summary when API balance exhausted
```

## Sync/async split

The engine has a sync/async split that matters:

- `cast_spell_async` — async, can call Claude API, called from Discord command handlers
- `_check_creature_etb_triggers` — async wrapper, calls sync core + auto-resolve
- `_check_creature_etb_triggers_sync` — sync, Tier 1 + Tier 1.5 only
- `advance_phase` — sync, drives the phase system

**Known limitation:** triggers that fire from sync paths (suspend resolution, some land ETBs) can't call the Claude API. If Tiers 1/1.5 don't handle them, the player gets a manual hint (`Use !resolve to handle`) instead of auto-resolution. Workaround: `!resolve` manually. Future fix: make `advance_phase` async (cascades to every caller — medium-effort rewrite).

## Persona system

`personas/<name>.json` files describe the character layer. The bot reads `bot_persona` from `config.json` at startup and composes the three system prompts (`build_base_prompt`, `build_support_prompt`, `build_spiral_prompt`) on `bot.py` from the persona fields plus built-in capability/behavior text. See [`personas/README.md`](personas/README.md) for the schema.

The persona affects only how the bot speaks — name, pronouns, mannerisms, personality traits. The MTG engine itself is built into the code and stays the same regardless of persona.

## Distress detection

Lives in the [companion bot](https://github.com/VIXAL-OS/discord-companion-bot), not this repo. The MTG bot doesn't watch for distress — it just plays Magic and chats during games. If you want both, run two bot instances (or run the companion bot in MTG-enabled mode where it imports this engine).

## Autoplay system

`!autoplay [format] [deck1] [deck2]` runs Claude-vs-Claude games for playtesting. Two AI players run full games at high speed and post updates to a Discord thread.

- **Player 1** ("Rick Deckard") — uses AI decisions but executes through HUMAN code paths (`!play`, `!cast`, `!attack`). This exercises the same paths a real Discord user would.
- **Player 2** ("Claude") — uses the fast-path AI turn loop.

`!autoplay-all` runs the full matchup matrix. Logs land in `logs/game_<id>_console.log` and `game_<id>_discord.log` — those are the input to the post-batch audit playbook.

## Post-batch audit playbook

After running a batch via `!autoplay-all`, the contributor workflow is:

1. **Tier 1 sweep** (4 parallel agents): error/crash scanner, Discord display hunter, mana+life reconciliation, AI decision quality. Each agent operates on the batch logs.
2. **Tier 1.5 verification**: pass Tier 1 findings to a verifier agent that checks each claim against actual code paths / CR sections / Scryfall card text. Filters ~25-30% baseline false-positive rate.
3. **Tier 2 deep dives** (selective): pick 2-3 specific games with the most interesting bugs. Often picks stress-deck matchups (`layers`, `replacement_chain`, `death_replacement`, `combat_keywords`, `devotion`).
4. **Tier 3 periodic** (every few batches): format compliance sweep, deprecation rot sweep, instrumentation sanity check.

Every Tier 1 / Tier 2 prompt MUST include an evidence requirement: cite file:line OR CR section, otherwise downgrade severity. This is the cheapest defense against agent hallucination — it caught a ~22% false-positive rate even on May 25.

## Cost model

Tracked in `data/api_costs.json` and queryable with `!cost`. Headline numbers:

| Model | $/M input | $/M output |
|---|---|---|
| Claude Sonnet 4 | $3 | $15 |
| Claude Opus 4 | $15 | $75 |
| Claude Haiku 3.5 | $0.80 | $4 |
| DeepSeek V4-Flash | $0.27 | $1.10 |
| DeepSeek V4-Pro (reasoning) | $0.56 | $1.68 |

For autoplay specifically, the strategist (V4-Pro) and actor (V4-Flash) are tracked separately so the per-game `[STATS-GAME]` cost reconciles with lifetime `!cost` output.

## Test infrastructure

`data/test_*.json` decks each target a specific rules subsystem:

| Deck | Tests |
|---|---|
| `test_layers_humility.json` | Humility + anthems + counters, CR 613 layer ordering |
| `test_replacement_fog.json` | Damage doublers, Gisela halving, replacement effects |
| `test_replacement_chain_gisela.json` | CR 615.5 controller-chooses-order for replacement chains |
| `test_death_replacement.json` | Undying, persist, shield counters, totem armor |
| `test_devotion_erebos.json` | Theros devotion type-flip (Layer 4 type-changing) |
| `test_combat_keywords_glissa.json` | Trample+deathtouch (CR 702.19c), first strike, multi-blocker DT |
| `test_cascade.json` | Cascade pipeline + per-color mana diagnostics |
| `test_aura_equipment_combo.json` | Equipment + aura double-count bug coverage |
| `test_sagas_enchantress.json` | Saga lore counters, SBA 704.5s |
| `test_snow_jorn.json` | Snow mana ({S}), snow payoff cards |
| `test_aristocrats_korvold.json` | Sacrifice triggers, drain effects, dies loops |
| `test_graveyard_meren.json` | Reanimate, self-mill, dies triggers |

Plus the more conventional matchup decks: `surrak_stompy`, `claude_deck_aminatou`, `claude_deck_baral`, `claude_deck_rashmi`, modern archetypes (burn / uw_control / jund), limited / cube / brawl / oathbreaker variants.

## Common pitfalls

1. **Check `is_creature(game=...)` not `is_creature()`** for devotion-gated Theros gods. Without `game`, the devotion check is silently skipped and the god is treated as a creature regardless of its threshold.
2. **`to_dict() / from_dict()` is the only persistence format** — used by `engine.save_game`, `!resumegame`, and `!undo`'s snapshot stack. If you add a new field to `GameState`, add it to both methods.
3. **The action interpreter (`mtg/actions.py`) is the only path that mutates game state** — well, mostly. There are a few inline mutations in `mtg/spells.py` and `mtg/combat.py` that haven't been routed through it yet, but anything new should use it.
4. **Never read `card.power` directly for effective stats** — use `card.get_effective_power(game)` so layer effects (anthems, Humility, +1/+1 counters) apply. Same for `get_effective_toughness`. Direct reads are correct for cards in zones where layers don't apply (library, graveyard, hand).
5. **`!undo` reuses `to_dict/from_dict`** — anything that round-trips through save/load works for undo. Stack entries are transient (not reconstructed) — undoing a cast restores the pre-cast board with the spell back in hand.

## Open known limitations

- **`priority.py`** is wired but not exposed end-to-end in Discord. Multiplayer priority passing is blocked behind the React frontend work. Adaptive 0.5s/6s stack-resolution timeout in autoplay (`mtg/spells.py`) covers bot-vs-bot.
- **Library order** is not fully modeled — `!resolve` for scry-shaped effects often returns "no state change" since the engine has no oracle for library positioning. Fateseal modeling was added in the May 17 audit but is minimal.
- **Some sync paths** (suspend resolution, sync land ETBs) can't reach Tier 3. See "Sync/async split" above.

## Where to ask questions

- Open a GitHub Discussion for design questions
- Open a GitHub Issue for bug reports
- For audit findings (regressions in an autoplay batch), include the relevant log files
