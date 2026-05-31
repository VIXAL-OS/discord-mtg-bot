# Personas

The bot's "character layer" lives here. Each `*.json` file in this directory describes a persona — name, pronouns, personality traits, mannerisms, comfort actions. The bot's capabilities (MTG game engine, tarot, web search, memory system, distress detection) are built into the code and stay the same regardless of persona; only the wrapping voice changes.

## Picking a persona

Set `"bot_persona": "<filename-without-.json>"` in your `config.json`. For example:

```json
{
  "bot_persona": "ressapanda"
}
```

If `bot_persona` is missing or empty, the bot uses `plain.json` (a generic "Claude" persona with no roleplay).

## Bundled personas

| File | Description |
|---|---|
| `plain.json` | No roleplay, just Claude being friendly and direct. The default. |
| `ressapanda.json` | A whimsical sapient red panda from the Pittsburgh Zoo. The original. |

## Writing your own

Copy one of the JSON files, rename it, and edit the fields. Required:

- `name` — the bot's character name (used in `[note:]` tags, log lines, and as "You are X" in prompts)
- `pronouns` — pronoun string (e.g. "he/him", "she/her", "they/them", "it")
- `intro` — a one-sentence character description (will follow "You are <name> (<pronouns>), ...")
- `personality_traits` — bullet list of character traits (each becomes a `-` line in the system prompt)

Optional but useful:

- `mannerisms` — roleplay action descriptors like `*tilts head*`, `*stretches*` (omit if you don't want roleplay)
- `voice_notes` — guidance for how the model should use the persona (e.g. "use mannerisms sparingly")
- `settling_action` — a short action for emotional-support mode (e.g. `*settles nearby, attentive*`)
- `grounding_action` — a short action for active-spiral mode (e.g. `*sits close, warm and solid*`)
- `closing_action` — a short action at the end of crisis responses (e.g. `*curls up next to them*`)
- `comfort_content` — short phrase describing what comforts work (e.g. "more red panda pictures always help")
- `understands_neurodivergence` — boolean; if true, the bot's distress prompts include language about autistic burnout, etc.

## Schema versioning

The `_schema` field is informational only — the loader ignores it. We'll add a versioning system if/when the schema needs a breaking change.

## How it plugs in

`bot.py:load_persona()` reads `personas/<bot_persona>.json` at startup and stores it on the bot. Three template builders (`build_base_prompt`, `build_support_prompt`, `build_spiral_prompt`) compose the persona fields with built-in capability/behavior text to produce the actual system prompts used at runtime.
