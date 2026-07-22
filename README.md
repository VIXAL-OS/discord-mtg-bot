# discord-mtg-bot

A Discord bot that plays **Magic: The Gathering** with you — full Commander / Modern / Legacy / Vintage / Pioneer / Pauper / Limited / Brawl / Oathbreaker / Cube support. Plays both sides if you have nobody else to play with.

Includes:

- A **tiered rules engine** with ~500 card-specific templates (160 of them plain JSON) plus ~90 oracle-text pattern families for the long tail
- An **XMage card-database bridge** (87,000+ cards) for novel-card support
- An **LLM-backed judge** for genuinely complex interactions
- An **`!undo` snapshot stack** so bugs in obscure rules don't ruin your game
- A **`!coverage`** command that classifies every card in a deck by how the engine will handle it
- An **autoplay loop** for batch playtesting (Claude or DeepSeek as both players)
- A **persona layer** for chat flavor in game threads

For a Discord companion bot with distress support / memory / tarot / YouTube transcription, see the sibling [`discord-companion-bot`](https://github.com/VIXAL-OS/discord-companion-bot) repo. That bot can optionally import this MTG engine if you want both in one deployment.

## Quick start

```bash
# 1. Clone + create your config
git clone https://github.com/VIXAL-OS/discord-mtg-bot.git
cd discord-mtg-bot
cp config.json.example config.json
cp .env.example .env
# Edit .env with your Discord token + Anthropic API key

# 2. Install Python deps
python -m venv venv
source venv/bin/activate    # Windows: venv\Scripts\activate
pip install -r requirements.txt

# 3. (Optional) Build the XMage bridge for ~10x more card coverage
cd rules/xmage-bridge && mvn package && cd ../..

# 4. Run it
python bot.py
```

## Configuration

`config.json` has three knobs:

| Setting | What it does |
|---|---|
| `bot_persona` | Character layer (a file under `personas/`). Default: `plain` (no roleplay). Try `ressapanda` for whimsy. |
| `mtg_channel_id` | Discord channel where the bot auto-responds to every message. Set to `null` to require @-mentions in other channels. |
| `excluded_channels` | Channel IDs where the bot never responds. |

That's it — the bot is intentionally minimal in scope.

## How to play

In a Discord channel the bot can see:

```
!game @SomeoneElse commander   # Start a 2-player commander game
!game claude modern            # Play against the bot
!mydeck surrak                 # Load one of the bundled test decks
!deck <archidekt-url>          # Load your own deck from Archidekt
!play Lightning Bolt           # Cast a card from hand
!attack all                    # Declare all available attackers
!block Grizzly Bears with Wall of Omens
!pass                          # Pass priority
!state                         # Show the board
!hand                          # See your hand (DM'd to you)
!undo                          # Roll back the last risky action (depth 5)
!coverage surrak               # See how the engine handles each card in the deck
!judge <question>              # Ask a rules judge — applies state changes if needed
!fix <natural-language>        # Manual state surgery for bug recovery
!card <name>                   # Pretty Scryfall card display
!xmage <name>                  # Raw XMage rules-engine data
!cost                          # Lifetime API usage + costs
```

See `mtg/cog.py` for the full command list. The bot plays as the AI opponent when you `!game claude` or `!game @BotName`.

## Architecture (high-level)

The rules engine resolves effects through a tiered cascade — start fast/cheap, escalate only when needed:

| Tier | What | Cost | Coverage |
|---|---|---|---|
| **Tier 1** | Hardcoded handlers in `mtg/triggers.py` + `mtg/spells.py` | Free, instant | ~15 specific cards |
| **Tier 1.5** | Templates in `data/card_templates.json` + patterns in `rules/effect_templates.py` | Free, instant | ~500 cards + ~90 pattern families |
| **Tier 2** | `SpellResolver` (regex → JSON action) | Free, instant | ~40% of remaining oracle text |
| **Tier 2.5** | XMage bridge (Java subprocess, 87k-card DB) | ~10-50ms | Catches what regex misses |
| **Tier 3** | LLM judge (`mtg/judge.py`) | Tokens + ~2s | Genuinely novel effects |
| **Tier 4** | Manual: `!judge`, `!resolve`, `!fix`, `!undo` | Human | Last resort |

Run `!coverage <deckname>` to see how the engine will classify each card in a deck before you play. See [ARCHITECTURE.md](ARCHITECTURE.md) for the deeper tech overview, the `mtg/` and `rules/` package layouts, the effect-action JSON format, and the per-batch audit playbook contributors use to catch regressions.

## Discord setup checklist

1. Create a Discord application at <https://discord.com/developers/applications>
2. Add a Bot user; grab the **bot token** (goes in `.env` as `DISCORD_TOKEN`)
3. Enable these Privileged Gateway Intents on the Bot tab:
   - **Server Members Intent**
   - **Message Content Intent**
4. Generate an OAuth2 invite URL with the `bot` + `applications.commands` scopes, plus permissions: `Send Messages`, `Read Message History`, `Attach Files`, `Embed Links`, `Add Reactions`, `Use Slash Commands`, `Manage Threads`, `Create Public Threads`, `Send Messages in Threads`.
5. Invite the bot to your server.
6. Get your Anthropic API key from <https://console.anthropic.com>, put it in `.env` as `ANTHROPIC_API_KEY`.
7. (Optional) For autoplay batches, also set `DEEPSEEK_API_KEY` — gives a much cheaper actor model (V4-Flash) while keeping Claude for the strategist. Without DeepSeek configured, autoplay falls back to Claude on both sides (more expensive).
8. Run `python bot.py`.

## Picking and writing personas

`personas/plain.json` is the default — no roleplay, just Claude being friendly and direct. Switch to `ressapanda.json` by setting `"bot_persona": "ressapanda"` in `config.json` if you want a whimsical red-panda character. Write your own by copying either file and editing the fields described in [`personas/README.md`](personas/README.md).

Personas only affect the **voice** the bot uses in chat (during game threads). All MTG capabilities are built into the code and don't change with persona.

## Costs

Roughly per usage pattern:

| Use case | ~Cost |
|---|---|
| Casual chat in game threads (Sonnet) | $0.003 per message round-trip |
| One Commander MTG game (Claude on both sides) | $0.15-$0.30 |
| One Commander game, DeepSeek on both sides (autoplay default when `DEEPSEEK_API_KEY` is set) | ~$0.01 |
| One Modern / Pauper game | $0.10-$0.20 |

`!cost` shows the lifetime running total; persisted in `data/api_costs.json`.

The DeepSeek figure is measured, not projected: a full 143-game regression
batch (every matchup in the autoplay matrix) cost **$1.57** end to end, at a
72.7% prompt-cache hit rate. That's what makes batch playtesting practical —
the same batch on Claude both sides would run $20-40.

## Project status

Pre-1.0. The rules engine is well-exercised — the [post-batch audit playbook](ARCHITECTURE.md#post-batch-audit-playbook) runs Tier 1 + Tier 2 agents against batches of 100+ autoplay games to catch regressions. See [ARCHITECTURE.md](ARCHITECTURE.md) for the open known limitations.

## Contributing

PRs welcome — and **adding support for a card needs no API keys and no
Discord bot.** Most cards resolve from a name-keyed template table, so
contributing one is an entry in [`data/card_templates.json`](data/card_templates.json)
plus `python -m pytest tests -q` (~450 tests, runs offline in a few
seconds). The loader is strict, so the test suite doubles as the schema
check, and CI validates every card name against Scryfall.

Engine changes are the other path: write a failing test first, keep the
suite green, and mind the debt ratchets in `tests/test_ratchets.py`.

Full details, the template schema, and the PR checklist are in
[CONTRIBUTING.md](CONTRIBUTING.md).

## License

MIT — see [LICENSE](LICENSE).
