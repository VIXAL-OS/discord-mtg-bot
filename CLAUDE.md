# CLAUDE.md â€” discord-mtg-bot (OSS pure-MTG Discord bot)

## What this repo is

This is the **public, persona-agnostic OSS fork** of a larger private bot. It is a
Discord bot that plays Magic: The Gathering (autoplay Claude-vs-Claude, plus
human `!play`/`!attack`/etc.), does Scryfall card lookups, and supports a
configurable persona for light in-thread chat. All the mental-health / support /
memory / tarot / YouTube machinery from the private original was stripped.

- `bot.py` (~748 lines) â€” entry point, persona layer, cogs.
- `mtg/` â€” the game engine package (Card, Player, GameState, RulesEngine,
  GameEngine, Discord cog). Tiered effect resolution (see below).
- `rules/` â€” rules-correctness subsystem (mana, layers, replacement effects,
  targeting, SBA, effect templates, spell resolver, planeswalker).
- `persona/plain.json` + `ressapanda.json` â€” swappable personas.
- Cost model: Sonnet for chat + DeepSeek buckets for autoplay.

### Relationship to the upstream private repo

The `mtg/` + `rules/` engine is **content-identical** to the private upstream
repo this was forked from. That repo holds the full audit history, the autoplay
test matrix, and the post-batch bug-audit playbook. When porting engine fixes,
the file:line references in the upstream CLAUDE.md's "May 26/30 audit sprint"
section line up here too.

## ~~Known issue: UTF-8 mojibake in the engine files~~ â€” RESOLVED June 10, 2026

This warning turned out to be stale: a byte-level scan (Python `read_bytes()`
+ UTF-8 decode, not PowerShell â€” PS 5.1 reads BOM-less files as ANSI and
*displays* phantom mojibake) found zero corruption in any engine file. The
June 10 upstream sync also re-copied `mtg/` + `rules/` from the source repo
with verified UTF-8, applying the May 26/30 + June 10 audit fixes at the same
time. Verify anytime with: `grep -l 'Ã¢â‚¬' mtg/*.py rules/*.py` (empty = clean).

## The tiered effect resolution architecture (same as the original)

Effects resolve through a cascade â€” always prefer the LOWEST tier:
- **Tier 1** â€” hardcoded handlers in `mtg/triggers.py` + `mtg/spells.py`.
- **Tier 1.5** â€” `rules/effect_templates.py` (named-card + regex pattern library).
- **Tier 2** â€” `rules/spell_resolver.py` (regex â†’ EffectType â†’ execute).
- **Tier 2.5** â€” XMage bridge (`rules/xmage_*`), optional.
- **Tier 3** â€” `mtg/judge.py` `resolve_effect()` (Claude API, last resort).
JSON action format consumed by `execute_action_on_state()` in `mtg/actions.py`.

## Porting checklist â€” May 26/30, 2026 audit (~30 engine fixes)

These are verified, source-checked fixes from the private repo's May 26/30 audit
sprint. The cleanest way to apply them all at once is to **re-copy the listed
files from the upstream repo at commit `c44be72` or later** (which also fixes the
mojibake). If applying by hand, the changes per file:

- **`rules/replacement.py`** â€” CR-616.1 controller-chooses-order trio: (a) Furnace
  of Rath is SYMMETRIC â€” remove the "your sources only" house rule in
  `create_furnace_of_rath_effect` so it doubles incoming damage too. (b)
  `_are_all_commutative`: mixed-direction multipliers (Ã—0.5 halve + Ã—2 double) are
  NON-commutative under floor rounding. (c) `_choose_best_for_controller`: invert
  the benefit direction for DAMAGE/LIFE_LOSS (apply reducers first).
- **`mtg/rules_engine.py`** â€” `_has_totem_armor`/`_remove_totem_armor` match
  `'umbra armor'` (modern cards print "Umbra armor", not "totem armor") â€” without
  this the entire wired SBA totem-save path is dead.
- **`rules/spell_resolver.py`** â€” creature damage marks + runs
  `process_state_based_actions` (via `game._rules_engine`) instead of inline
  removal, using effective toughness â€” so totem/undying/persist/shield apply.
- **`mtg/actions.py`** â€” (1) board-wipe `destroy_all_creatures` honors shield â†’
  totem â†’ undying/persist saves (was indestructible-only). (2) `living_death`
  queues `_recently_died` for sacrificed creatures (was dropping every
  dies-trigger). (3) new `proliferate` action (counters only, never life). (4)
  pump-effect no-op returns `None` (was leaking scaffolding). (5) Austere Command
  full names.
- **`mtg/engine.py`** â€” (1) `_check_beginning_combat_triggers_sync` delegator +
  COMBAT_BEGIN dispatch (the "at beginning of combat on your turn" class was
  unwired). (2) APNAP NAP-first sort added to the phase-transition dies-trigger
  drain.
- **`mtg/triggers.py`** â€” (1) `_check_beginning_combat_triggers_sync` scanner. (2)
  `has_dies_trigger` + dying-card self-inclusion recognize "nontoken creature you
  control dies" (Midnight Reaper / Judith / Liliana flip). (3) Warstorm Surge
  `is_creature(game)` (devotion-god ETB). (4) `_log_life_change` + `[LIFE-*]` tags
  on cast-trigger / Blood-Artist-Zulaport-Bastion / Syr Konrad / Phyrexian Arena.
- **`mtg/spells.py`** â€” Warstorm/creature-ETB-watcher `is_creature(game)`;
  suppress "handled mechanically by the SBA engine" jargon at the `ðŸ“œ {reason}`
  no_action emit sites.
- **`mtg/models.py`** â€” (1) conditional static anthems/grants gated on "as long
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
- **`mtg/cog.py`** â€” `_autoplay_send` collapses runs of identical lines within one
  multi-line message.
- **`mtg/autoplay.py`** â€” CRITICAL devotion-block: `can_block(attacker, game=game)`
  guard + empty-name skip on the two unguarded block-application loops (main
  `decide_blocks` path + Moraug additional-combat path). Also stagnation-draw
  line drops the `[AUTOPLAY]` tag.
- **`rules/effect_templates.py`** â€” Geralf's Messenger ETB (lose_life 2 + dies
  no-op); proliferate generator + clause-final pattern + `ctx['_event_type']`;
  Finale of Devastation generator.
- **Cost (this repo's `bot.py` + `mtg/autoplay.py`)** â€” use the REAL DeepSeek V4
  rates (verified against a real bill): Flash hit `$0.0028`/miss `$0.14`/out
  `$0.28`; Pro hit `$0.0036`/miss `$0.435`/out `$0.87` (per M). Old list rates
  over-estimated ~38Ã— (cache-hit rate was ~25-39Ã— too high). NOTE: in
  `!autoplay-parallel`, the per-game cost line over-counts ~18Ã— (shared adapter) â€”
  trust the cumulative line.
- **`test_replacement_controller_order.py`** (new file at repo root) â€” a permanent
  scripted regression for the Furnace-vs-Gisela controller-choose-order branch.
  Copy it over and run `python test_replacement_controller_order.py`.

## Style notes

- Match the surrounding code; comments explain *why*, not *what*.
- Console logging is tagged with `[BRACKETS]` for grep-ability.
- The bot aims to be as rules-correct as possible; when it's wrong it should be
  fixable on the fly (`!fix`, `!resolve`) rather than game-ending.

## June 10, 2026 (evening) upstream sync — full audit fix sprint (~70 fixes, suite 168 tests)

Synced the upstream June 10 triple-wave fix sprint (Tier-1 audit fixes, the
verified deep-dive wave, and the round-3 closures). Engine files (`mtg/`,
`rules/`, `cube_draft.py`, `tests/`) are byte-lockstep again; the six
intentionally-diverged files (`mtg/spells.py`, `mtg/engine.py`,
`mtg/autoplay.py`, `mtg/cog.py`, `rules/effect_templates.py`,
`rules/llm_adapter.py`) took the same fixes as surgical patches with the
fork divergences (deck names, help text, HTTP-Referer) preserved.

Highlights (full detail lives in the upstream repo's June 10 sections):

- **Mana engine**: colored-need decrement (a 1-mana spell tapped the whole
  board since Apr 4), Phyrexian either/or payment, dual-land one-tap
  accounting (underpayment direction), "Add {W} or {U}" or-choice.
- **Combat/death**: first-strike blocker double-dip + the symmetric
  regular-step gate, undying/persist returns as clean new objects (combat
  state stripped, enters-tapped, self-ETB re-fires), single-target destroy
  honors shield/totem/undying, sacrifice-as-cost fires dies triggers +
  unregisters layer effects, CR 903.9b commander deaths fire dies triggers,
  commanders return to the OWNER's command zone.
- **Triggers**: 2026 "this creature" Oracle templating (Blood Artist class
  was dead code), "an opponent controls dies" scope gate (Massacre Wurm
  misfire ended a game illegally), gain-life trigger class wired
  (Vito/Heliod/Pridemate), constellation watchers, cast-trigger self-pumps
  (Kiln Fiend), unhandled cast triggers queue for Tier 3.
- **Templates**: Marit Lage's Slumber condition-checked (the generic
  upkeep-token pattern minted unconditional 20/20s and won a game),
  intervening-if guard on the generic pattern, Land Tax, Drakuseth,
  Teachings of the Kirin chapter dispatcher (+ saga resolution now passes
  real game context), Leyline Tyrant mana-availability decline (+ a judge
  guard against Tier-3 fabricating "you may pay" payments), Toxic Deluge
  player="all" with real X, Reanimate real-MV life cost, named-target
  honoring with legality gates (Krosan Grip / Abrupt Decay), Twinflame
  Tyrant hallucinated template deleted + real doubler registered.
- **Display/pipeline**: burst-dedup no longer suppresses distinct casts,
  trigger source attribution fix, mid-stream LLM error salvage, turn banner
  before upkeep lines, autodraft messages routed through the logged send
  pipeline.
- **New**: `mtg/events.py` — engine event bus, pub/sub migration slice 1
  (LIFE_GAINED) live; migration plan in the module docstring.
- **Tests**: suite is now 168 (three new test files: `test_june10_fixes.py`,
  `test_june10_deepdive.py`, `test_events_and_sagas.py`); ratchet baselines
  updated (sba.py 3->4, engine.py 41->43, both justified crash barriers
  with maybe_reraise).

Run autoplay batches with `MTG_STRICT=1` exported — several swallow sites
now carry `maybe_reraise`, so strict batches detect what production
swallows.
## July 21, 2026 upstream sync — July 16/20/21 audit sprints (suite 309 → 452)

Brings the fork current with ~40 upstream commits spanning four audited
batches. Engine files are byte-identical to upstream except the four
intentional divergences (see "What stays different" below).

**Engine fixes ported** (highlights — see upstream commit messages for the
per-finding stories):
- **Cast path**: `cast_spell_async` decomposed into `_validate_cast` /
  `_compute_alt_costs` / `_pay_costs` / `_await_stack_window` /
  `_dispatch_resolution` with a ~108-line orchestrator; the mana pre-gate
  is now convoke/delve/improvise-aware (`[CAST-GATE]`), and printed
  alternate costs (Force of Will, Fireblast) are visible to the response
  filters instead of reading as dead cards.
- **Stack correctness**: counterspell responses can target spells on the
  stack again (a July regression blocked every explicit-target counter);
  `on_stack_resolve` refuses to resolve an entry buried on `game.stack`
  (`[STACK-LIFO-GUARD]`, CR 608), and a cast-trigger window only counts a
  trigger countered when the `countered` flag is actually set.
- **Mana honesty**: hybrid pips now count toward mana value (CR 202.3);
  payability advertisement does a per-color-pair union-capacity check so
  OR-duals stop being double-counted; the pool no longer accumulates spent
  payment mana, and genuinely-floating mana is actually spendable.
- **Rules**: planeswalker damage deducts loyalty (CR 306.8); commanders
  destroyed by the single-target `destroy` action go to the command zone
  (CR 903.9a); Yorion's return is a delayed end-step trigger (CR 603.7);
  cascade only fires from real keyword lines (grant clauses like Yidris no
  longer self-cascade); a permanent's own "whenever you sacrifice" sees its
  own sacrifice; unhandled dies-triggers queue for Tier 3 instead of being
  dropped at five call sites; generic ETB patterns no longer match
  activated-ability text; draw-from-empty-library WIN replacement
  (CR 614.12) implemented.
- **Wiring**: `game._rules_engine` is stamped at game creation — it had
  only ever been assigned in tests, so the spell-damage SBA routing and the
  Phyrexian Tower dies dispatch were dead in production.
- **Infra**: `mtg/helpers.py:response_text()` at all response-parse sites
  (thinking-block-safe), swap-depth refcount on the shared-client restore
  under parallel batches, adaptive strategist degrade after repeated
  deadman fires.

**Template migration (the contribution path)**: 159 fixed templates now
live in `data/card_templates.json` (144 etb / 6 dies / 9 attack). Adding a
simple card is a JSON entry, not Python. The loader is strict — schema
errors and Python/JSON key collisions raise at import, so every pytest run
is the schema check.

**Pub/sub**: slice 2 (`PERMANENT_ENTERED`) and slice 2b are live — the
creature-enters and enchantment-enters watcher scans are now bus
subscribers rather than ~12 hand-wired call sites; slice 3a
(`CREATURE_DIED`) is in shadow mode behind a `queue_death` choke-point.
Plan in `mtg/events.py`.

**Fork-side fixes found during the sync** (not upstream ports):
- 17 matrix matchups referenced a `mythic` deck that was never ported at
  fork time — they would have failed at runtime. The two Daretti lists now
  ship as `mythic_sanity.json` / `mythic_madness.json` with scrubbed names,
  and every matrix reference resolves against the deck registry.
- `test_aura_equipment_combo.json` carried Mana Crypt + Karakas (banned in
  Commander, silently stripped at load) → Worn Powerstone + Secluded
  Steppe.
- `X-Title` was the scrub artifact `"the bot MTG Bot"` → `"Discord MTG Bot"`.

**What stays different from upstream (by design):**
- `bot.py` — fork-own (no distress/memory/tarot systems).
- `rules/llm_adapter.py` — OpenRouter `HTTP-Referer` / `X-Title` point at
  this repo.
- Deck naming — `surrak`/`surrak_stompy` in place of the upstream persona
  deck; `mythic_sanity` / `mythic_madness` in place of the personal ones.
- Help text and code comments are genericized (`@Player`, "the player").
- `rules/tarot_visuals.py` and its test class are upstream-only.
- Suite is 452 vs upstream 453 — the difference is that tarot test.
