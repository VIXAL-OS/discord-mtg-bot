# Contributing

Thanks for looking. This project has two very different contribution paths,
and the common one needs **no API keys and no Discord bot** — you can add
card support with a text editor and `pytest`.

## Setup

```bash
git clone https://github.com/VIXAL-OS/discord-mtg-bot.git
cd discord-mtg-bot
python -m venv venv
source venv/bin/activate    # Windows: venv\Scripts\activate
pip install -r requirements.txt
python -m pytest tests -q   # ~450 tests, a few seconds, no network
```

If that suite passes you have a working development environment. You do
**not** need a Discord token, an Anthropic key, or a DeepSeek key to work on
the rules engine — the whole suite runs offline against fixtures.

---

## Path A — adding support for a card (most contributions)

Most cards resolve through **Tier 1.5**: a table of card-name-keyed
templates that turn an ability into a list of JSON actions. If the card's
effect is *fixed* — it doesn't need to read board state or branch — adding
it is a JSON entry in [`data/card_templates.json`](data/card_templates.json)
and nothing else.

Add an object to the `templates` array:

```json
{
  "key": "soul warden",
  "name": "Soul Warden",
  "event": "etb",
  "description": "Whenever another creature enters, you gain 1 life",
  "actions": [
    { "action": "gain_life", "player": "$controller", "amount": 1 }
  ]
}
```

| Field | Meaning |
|---|---|
| `key` | The card name, lowercased. This is the lookup key — it must match Scryfall exactly (apostrophes included). |
| `name` | Human-readable label used in logs. |
| `event` | `etb`, `dies`, or `attack`. |
| `description` | One line, shown in `[ETB-TEMPLATE] Resolved …` log output. |
| `actions` | List of JSON actions (see the action vocabulary in [ARCHITECTURE.md](ARCHITECTURE.md#json-action-format)). |

`$controller` and `$opponent` are substituted into every string at
resolution time — use them instead of hardcoding player names.

Then run the suite:

```bash
python -m pytest tests -q
```

The loader is deliberately strict, so the tests *are* the schema check:
a malformed entry, a duplicate key, or a key that collides with a Python
template raises at import and fails every test in the suite. CI separately
validates every card name against Scryfall bulk data
(`tools/validate_card_names.py`), so a typo'd name fails there rather than
silently never matching at runtime.

**When JSON isn't enough.** If the effect needs to read game state, branch,
or compute a value ("draw cards equal to the greatest power among creatures
you control", "if you control three or more artifacts, instead…"), it needs
a generator function in [`rules/effect_templates.py`](rules/effect_templates.py)
rather than a JSON entry. Copy the shape of a nearby `_gen_*` function.
Pattern families that catch whole categories of cards also live there.

**Which tier will handle my card?** Run `!coverage <deckname>` in Discord if
you have the bot running, or call `mtg.coverage.supported_at_tier(name,
oracle_text)` directly — it reports `template` / `pattern` / `tier3` without
needing a game.

## Path B — engine changes

Anything touching resolution, combat, the stack, layers, or state-based
actions.

1. **Write a failing test first.** Put it in `tests/` next to the closest
   existing case. Nearly every fix in this repo's history shipped with a
   repro test, and that's the main reason old bugs stay fixed.
2. `python -m pytest tests -q` must be green.
3. Note the debt ratchets in `tests/test_ratchets.py`: the suite fails if
   the count of broad `except Exception:` handlers or undeclared runtime
   attributes *grows*. If your change genuinely needs a new crash barrier,
   keep the log line, add `maybe_reraise(e)` (see `mtg/util.py`), and bump
   the baseline in the same commit with a one-line justification.
4. If you have API keys and the change could affect gameplay broadly, run a
   batch (`!autoplay-all`, or a phase like `!autoplay-batch stress`) with
   `MTG_STRICT=1` exported and check the logs for new `[ETB-UNHANDLED]`,
   `[TRIGGER-UNHANDLED]`, tracebacks, or `[EVENT-PARITY]` lines. This is
   **optional** — maintainers run batches regularly, and a good test is
   worth more than a batch you paid for.

`MTG_STRICT=1` turns swallowed engine exceptions into real crashes. pytest
sets it automatically; export it for batches too.

## Opening the PR

- Say what broke and how you know it's fixed. A log excerpt or a failing-
  then-passing test is ideal.
- If the change is rules-correctness, cite the Comprehensive Rules section
  (e.g. CR 702.19c) — this repo's review culture leans on CR citations, and
  it makes review much faster.
- Card-name claims should be checked against Scryfall, not memory. Several
  past bugs came from confidently-remembered oracle text that no printing
  actually has.

## Reporting bugs

Please include the format and decks, what you expected versus what happened,
and the relevant console log lines if you have them. Logs land in `logs/` as
`game_<id>_console.log` (engine detail) and `game_<id>_discord.log` (what
players saw) — the console log is usually the one that identifies the bug.
