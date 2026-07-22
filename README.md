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

# 3. (Optional) XMage bridge — extra card coverage, but it needs XMage
#    itself built from source first. See "The XMage bridge" below.
#    Skip this: the engine works fine without it.

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

## The XMage bridge (optional, Tier 2.5)

The engine can consult [XMage](https://github.com/magefree/mage)'s card
database (87,000+ cards) for effects the templates and regex passes miss. It's
genuinely optional — without it the engine falls back to template + LLM
resolution and nothing crashes, it just leans on Tier 3 slightly more often.

**Building it is more work than the rest of the project**, so here's the honest
version. `rules/xmage-bridge/pom.xml` depends on `org.mage:mage`,
`mage-server`, and `mage-sets` at version 1.4.58. **Those artifacts are not
published to Maven Central.** You have to build XMage from source first so
they land in your local `~/.m2`:

```bash
git clone --depth 1 --branch xmage_1.4.58 https://github.com/magefree/mage.git
cd mage && mvn install -DskipTests
```

That's a large multi-module Java build — budget real time and a few GB of RAM.
Only then does the bridge build work:

```bash
cd rules/xmage-bridge && mvn package
```

That produces `target/xmage-bridge-1.0.0.jar` (~90MB, XMage shaded in). The bot
picks it up automatically on next start; you'll see `[XMAGE]` lines in the
console. First start after building rescans the card DB (~13s) and writes a
~250MB cache under `db/` and `rules/xmage-bridge/db/`.

If you'd rather not: skip it. Run `!coverage <deck>` to see what your decks
actually need — most Commander decks are covered by Tier 1.5 templates.

## Deploying (running it 24/7)

The bot is a normal long-running Python process; `docker compose` is the
turnkey path. A small VPS is plenty — 2GB RAM is fine without the XMage
bridge, 4GB if you want it. These instructions were walked end-to-end on a
fresh Ubuntu 24.04 box; if something here is wrong, that's a bug worth an
issue.

```bash
curl -fsSL https://get.docker.com | sh
```

```bash
git clone https://github.com/VIXAL-OS/discord-mtg-bot.git && cd discord-mtg-bot
```

```bash
cp config.json.example config.json && cp .env.example .env
```

Now edit `.env` (Discord token, Anthropic key, optional DeepSeek key) and
`config.json` (channel IDs).

> **Create those two files before your first `docker compose up`.**
> `docker-compose.yml` bind-mounts `config.json` as a *file*. If it doesn't
> exist yet, Docker helpfully creates a **directory** with that name and the
> bot then fails in a way that doesn't mention the real problem. If you hit it:
> `docker compose down && rm -rf config.json`, then copy the example properly.

```bash
docker compose up -d --build
```

```bash
docker compose logs -f
```

You're waiting for the Discord ready line. Then sanity-check in Discord:
`!card Lightning Bolt` (Scryfall works), `!game claude commander` +
`!mydeck surrak` (deck loading works), `!state` (rendering works). If you set
`DEEPSEEK_API_KEY`, a single `!autoplay commander surrak aminatou` exercises
the engine, both LLM adapters, logging, and Discord rate limiting end-to-end
for about a penny — it's the best one-shot integration test.

State lives in host bind mounts (`data/`, `logs/`, `config.json`), so
`docker compose down` and rebuilds don't lose your decks, saved games, or
lifetime cost tracking. To update: `git pull && docker compose up -d --build`.

### Log growth

Two separate things grow, and they want different treatments.

**The container's stdout** is capped in `docker-compose.yml` (`max-size: 10m`,
`max-file: 5` → 50MB ceiling). Nothing to do.

**The bot's per-game logs** under `logs/` are not. Autoplay writes two files
per game, so a full 143-game batch lands ~286 files. They're small
individually and they never grow after the game ends — which is exactly why
**`logrotate` is the wrong tool here**. logrotate is built for a few
*continuously growing* files (`app.log` → `app.log.1.gz`), with machinery for
signalling a process to reopen its file handles. This bot's logs are many
small *immutable* files, so all that machinery buys nothing and the wildcard
config is fussier than the one-liner it replaces.

A cron job that deletes by age fits the actual shape:

```bash
(crontab -l 2>/dev/null; echo "0 4 * * * find ~/discord-mtg-bot/logs -name 'game_*.log' -mtime +14 -delete") | crontab -
```

If you'd rather keep the history, compress instead of deleting — game logs are
text and shrink hard:

```bash
(crontab -l 2>/dev/null; echo "0 4 * * * find ~/discord-mtg-bot/logs -name 'game_*.log' -mtime +2 -exec gzip {} +") | crontab -
```

Use logrotate anyway if you already run it everywhere and want one policy
across all your services — it'll work with a `logs/*.log` glob and
`copytruncate`. It's just more config for less fit.

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
