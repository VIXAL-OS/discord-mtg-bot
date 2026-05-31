# CLAUDE.md — discord-mtg-bot (OSS pure-MTG Discord bot)

## What this repo is

This is the **public, persona-agnostic OSS fork** of a larger private bot. It is a
Discord bot that plays Magic: The Gathering (autoplay Claude-vs-Claude, plus
human `!play`/`!attack`/etc.), does Scryfall card lookups, and supports a
configurable persona for light in-thread chat. All the mental-health / support /
memory / tarot / YouTube machinery from the private original was stripped.

- `bot.py` (~748 lines) — entry point, persona layer, cogs.
- `mtg/` — the game engine package (Card, Player, GameState, RulesEngine,
  GameEngine, Discord cog). Tiered effect resolution (see below).
- `rules/` — rules-correctness subsystem (mana, layers, replacement effects,
  targeting, SBA, effect templates, spell resolver, planeswalker).
- `persona/plain.json` + `ressapanda.json` — swappable personas.
- Cost model: Sonnet for chat + DeepSeek buckets for autoplay.

### Relationship to the upstream private repo

The `mtg/` + `rules/` engine is **content-identical** to the private upstream
repo this was forked from. That repo holds the full audit history, the autoplay
test matrix, and the post-batch bug-audit playbook. When porting engine fixes,
the file:line references in the upstream CLAUDE.md's "May 26/30 audit sprint"
section line up here too.

## ⚠️ Known issue: UTF-8 mojibake in the engine files

The `mtg/` + `rules/` files were copied during the fork with a bad encoding
round-trip (UTF-8 read as Latin-1, re-saved as UTF-8). Em-dashes render as `â€"`
and **emoji are corrupted** (e.g. `💀` → garbage), so Discord game messages would
show mojibake. The *content* is byte-for-byte identical to the upstream repo
modulo this corruption. **Fix:** re-copy the engine files from the upstream repo
(which is correctly UTF-8) — this simultaneously applies the audit fixes below AND repairs
the encoding. Verify with: `grep -l 'â€' mtg/*.py rules/*.py` (should be empty
after the fix).

## The tiered effect resolution architecture (same as the original)

Effects resolve through a cascade — always prefer the LOWEST tier:
- **Tier 1** — hardcoded handlers in `mtg/triggers.py` + `mtg/spells.py`.
- **Tier 1.5** — `rules/effect_templates.py` (named-card + regex pattern library).
- **Tier 2** — `rules/spell_resolver.py` (regex → EffectType → execute).
- **Tier 2.5** — XMage bridge (`rules/xmage_*`), optional.
- **Tier 3** — `mtg/judge.py` `resolve_effect()` (Claude API, last resort).
JSON action format consumed by `execute_action_on_state()` in `mtg/actions.py`.

## Porting checklist — May 26/30, 2026 audit (~30 engine fixes)

These are verified, source-checked fixes from the private repo's May 26/30 audit
sprint. The cleanest way to apply them all at once is to **re-copy the listed
files from the upstream repo at commit `c44be72` or later** (which also fixes the
mojibake). If applying by hand, the changes per file:

- **`rules/replacement.py`** — CR-616.1 controller-chooses-order trio: (a) Furnace
  of Rath is SYMMETRIC — remove the "your sources only" house rule in
  `create_furnace_of_rath_effect` so it doubles incoming damage too. (b)
  `_are_all_commutative`: mixed-direction multipliers (×0.5 halve + ×2 double) are
  NON-commutative under floor rounding. (c) `_choose_best_for_controller`: invert
  the benefit direction for DAMAGE/LIFE_LOSS (apply reducers first).
- **`mtg/rules_engine.py`** — `_has_totem_armor`/`_remove_totem_armor` match
  `'umbra armor'` (modern cards print "Umbra armor", not "totem armor") — without
  this the entire wired SBA totem-save path is dead.
- **`rules/spell_resolver.py`** — creature damage marks + runs
  `process_state_based_actions` (via `game._rules_engine`) instead of inline
  removal, using effective toughness — so totem/undying/persist/shield apply.
- **`mtg/actions.py`** — (1) board-wipe `destroy_all_creatures` honors shield →
  totem → undying/persist saves (was indestructible-only). (2) `living_death`
  queues `_recently_died` for sacrificed creatures (was dropping every
  dies-trigger). (3) new `proliferate` action (counters only, never life). (4)
  pump-effect no-op returns `None` (was leaking scaffolding). (5) Austere Command
  full names.
- **`mtg/engine.py`** — (1) `_check_beginning_combat_triggers_sync` delegator +
  COMBAT_BEGIN dispatch (the "at beginning of combat on your turn" class was
  unwired). (2) APNAP NAP-first sort added to the phase-transition dies-trigger
  drain.
- **`mtg/triggers.py`** — (1) `_check_beginning_combat_triggers_sync` scanner. (2)
  `has_dies_trigger` + dying-card self-inclusion recognize "nontoken creature you
  control dies" (Midnight Reaper / Judith / Liliana flip). (3) Warstorm Surge
  `is_creature(game)` (devotion-god ETB). (4) `_log_life_change` + `[LIFE-*]` tags
  on cast-trigger / Blood-Artist-Zulaport-Bastion / Syr Konrad / Phyrexian Arena.
- **`mtg/spells.py`** — Warstorm/creature-ETB-watcher `is_creature(game)`;
  suppress "handled mechanically by the SBA engine" jargon at the `📜 {reason}`
  no_action emit sites.
- **`mtg/models.py`** — (1) conditional static anthems/grants gated on "as long
  as / N or more" (helpers `_has_conditional_static`/`_static_condition_met`/
  `_remove_card_layer_effects` + gates in both `register_static_*` + recalc
  refresh + inline `_get_anthem_*_bonus` gate). (2) Death's Shadow
  `_get_life_total_debuff`. (3) Humility/compute-on-read: `has_keyword` defers to
  the layers engine's Layer-6 result under a remove_all_abilities effect
  (`_remove_all_abilities_active` + `base_abilities`/`_resolved_keywords` in
  `recalculate_power_toughness`). (4) "Other creatures you control have X" grants:
  exclude "other" from subtype capture. (5) Serra Ascendant conditional self-
  keyword (lifelink at 30+ life). (6) Night of Souls' Betrayal `all_debuff` regex
  matches the "All creatures get -1/-1" prefix. (7) summoning-sick creature mana
  dorks excluded from `untapped_mana_sources` (CR 302.6). (8) `[DEVOTION-CHECK]`
  print-on-change dedup. (9) recalc `is_creature(game=self)`.
- **`mtg/cog.py`** — `_autoplay_send` collapses runs of identical lines within one
  multi-line message.
- **`mtg/autoplay.py`** — CRITICAL devotion-block: `can_block(attacker, game=game)`
  guard + empty-name skip on the two unguarded block-application loops (main
  `decide_blocks` path + Moraug additional-combat path). Also stagnation-draw
  line drops the `[AUTOPLAY]` tag.
- **`rules/effect_templates.py`** — Geralf's Messenger ETB (lose_life 2 + dies
  no-op); proliferate generator + clause-final pattern + `ctx['_event_type']`;
  Finale of Devastation generator.
- **Cost (this repo's `bot.py` + `mtg/autoplay.py`)** — use the REAL DeepSeek V4
  rates (verified against a real bill): Flash hit `$0.0028`/miss `$0.14`/out
  `$0.28`; Pro hit `$0.0036`/miss `$0.435`/out `$0.87` (per M). Old list rates
  over-estimated ~38× (cache-hit rate was ~25-39× too high). NOTE: in
  `!autoplay-parallel`, the per-game cost line over-counts ~18× (shared adapter) —
  trust the cumulative line.
- **`test_replacement_controller_order.py`** (new file at repo root) — a permanent
  scripted regression for the Furnace-vs-Gisela controller-choose-order branch.
  Copy it over and run `python test_replacement_controller_order.py`.

## Style notes

- Match the surrounding code; comments explain *why*, not *what*.
- Console logging is tagged with `[BRACKETS]` for grep-ability.
- The bot aims to be as rules-correct as possible; when it's wrong it should be
  fixable on the fly (`!fix`, `!resolve`) rather than game-ending.
