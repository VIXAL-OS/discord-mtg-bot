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

# 3. (Optional) XMage bridge — 87k-card coverage via a prebuilt JAR.
#    One curl, no Java build. See "The XMage bridge" below.
#    Skip it if you like: the engine works fine without it.

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

**The easy way — download the prebuilt JAR** (no Java build required):

```bash
mkdir -p rules/xmage-bridge/target
curl -L -o rules/xmage-bridge/target/xmage-bridge-1.0.0.jar https://github.com/VIXAL-OS/discord-mtg-bot/releases/download/xmage-bridge-v1.0.0/xmage-bridge-1.0.0.jar
```

Restart the bot and you'll see `[XMAGE]` lines in the console. First start
rescans the card DB (~13s) and writes a ~250MB cache under `db/`. You need a
JRE 11+ on PATH — the Docker image already bundles one.

**Building it yourself is a chore**, which is why the JAR is published.
`rules/xmage-bridge/pom.xml` depends on `org.mage:mage`, `mage-server`, and
`mage-sets` at version 1.4.58, and **those artifacts are not published to
Maven Central.** You'd have to build XMage from source first so they land in
your local `~/.m2`:

```bash
git clone --depth 1 --branch xmage_1.4.58 https://github.com/magefree/mage.git
cd mage && mvn install -DskipTests
```

That's a large multi-module Java build — budget real time and a few GB of RAM.
Only then does `cd rules/xmage-bridge && mvn package` work, producing the same
~90MB shaded JAR the release gives you for free.

If you'd rather not: skip it. Run `!coverage <deck>` to see what your decks
actually need — most Commander decks are covered by Tier 1.5 templates.

### Licensing note

XMage is [MIT licensed](https://github.com/magefree/mage/blob/master/LICENSE.txt),
so a JAR that shades it can be redistributed — provided XMage's copyright and
license notice travel with it. The build bundles that notice at
`META-INF/LICENSE-xmage.txt`; if you distribute a built JAR, don't strip it.

Note also that the bundle includes XMage's card implementations (~42,000
classes named after Magic cards). Magic: The Gathering and card names are
Wizards of the Coast property; this project is unaffiliated fan tooling, as is
XMage. Nothing here is legal advice — if you plan to redistribute builds
publicly rather than run your own, that's worth your own look.

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

### Deploying to Fly.io (no VPS to manage)

If you'd rather not run a box, [Fly.io](https://fly.io) works well here and the
repo ships a [`fly.toml`](fly.toml). It builds the same `Dockerfile`, so you get
the same image — you're just swapping who runs it.

```bash
fly launch --no-deploy --copy-config
```

**Decline the HTTP service and health check when it offers them.** This bot
listens on no ports — it dials *out* to the Discord gateway. A health check
against a port nothing serves will fail forever and restart-loop the bot.

Secrets are environment variables (the same ones `.env` holds), so they go in
Fly's secret store rather than a file:

```bash
fly secrets set DISCORD_TOKEN=xxx ANTHROPIC_API_KEY=xxx DEEPSEEK_API_KEY=xxx
```

Saved games need a volume — one gigabyte is plenty, and it must be in the same
region as `primary_region`:

```bash
fly volumes create mtg_games --size 1
```

```bash
fly deploy
```

```bash
fly logs
```

Then run the same Discord sanity checks as the Docker path above.

**Four things worth knowing before you commit to it:**

**You are paying for an always-on machine.** Fly's headline "scale to zero when
idle" is a proxy feature for apps that serve requests. A Discord bot holds a
persistent gateway connection and must never suspend, so those savings don't
apply — price this against a small always-on VPS, not against Fly's idle tier.

**Never scale past one machine.** Each instance opens its own gateway session,
so two machines answer every command twice and run every `!autoplay` twice.
`fly.toml` sets `strategy = "immediate"` for this reason: the default rolling
deploy briefly runs old and new together, which is that same double-answer
state on every deploy. Keep `fly scale count 1`.

**Don't mount a volume at `/app/data`.** That path ships 39 files in git —
decks, card templates, the Scryfall cache — and an empty volume mounted over it
hides all of them, leaving you with a bot that has no decks and an error that
doesn't mention mounts. `fly.toml` mounts `/app/data/games` instead, which is
untracked and is the only path that actually needs to survive a redeploy. The
card cache re-fetches from Scryfall, and logs go to stdout for `fly logs`.

**`config.json` won't be in the image** — it's `.dockerignore`d, so the bot
starts on defaults and responds only to @-mentions. That's a fine first deploy.
To pin it to a channel, delete the `config.json` line from `.dockerignore` and
redeploy so it gets baked in. That's safe *in this fork specifically* because
`config.json` holds no secrets — just `bot_persona`, `mtg_channel_id`, and
`excluded_channels`; every credential lives in an environment variable.

**The XMage bridge (Tier 2.5) is the awkward part.** Its card DB is hundreds of
megabytes that the Dockerfile deliberately keeps out of the image, and on a VPS
you just `scp` it into a bind mount. On Fly you'd have to bake it in (a much
larger image) or seed a second volume. The engine degrades gracefully without
it — you lose one resolution tier, not the bot — so the simplest Fly deploy
skips it. If you want the bridge, a VPS is the easier home.

### Running on a game-server / bot-hosting panel

Plenty of hosts that are best known for Minecraft also sell Discord bot
hosting — PebbleHost, Sparked Host and BisectHosting all do, and
`bot-hosting.net` has a free tier that's popular for small bots. Railway and
Render occupy similar ground as PaaS. Any of them will run this.

Rather than name plans that go stale, here's the checklist that actually
decides fit. **The one that matters most is root:**

| Need | Why | If you don't have it |
|---|---|---|
| Python 3.11+ | Everything in `requirements.txt` is a pure-pip wheel — no compilers needed | — |
| **root or Docker** | The XMage bridge needs a JRE (`apt-get install openjdk`) | **No Tier 2.5.** The engine falls back to templates + LLM. Graceful, not fatal |
| Disk that survives restarts | `data/` holds saved games and the card-image cache | Saves vanish on restart; images re-download |
| Always-on (no idle sleep) | The bot holds a persistent gateway WebSocket | It drops offline and misses commands |
| ~1GB RAM | See sizing below | Rendering spikes can OOM you |

So: a locked-down panel where you upload code and pick a Python version runs
the **whole bot except the XMage bridge**. That's the honest dividing line. If
you want Tier 2.5, you want root — a VPS or their VPS tier.

On a panel you skip Docker entirely: clone, `pip install -r requirements.txt`,
set `DISCORD_TOKEN` / `ANTHROPIC_API_KEY` (and optionally `DEEPSEEK_API_KEY`)
in the panel's environment-variable UI, and run `python bot.py`. Panels are
genuinely *well* shaped for this — they're built around always-on processes
with restart-on-crash, so you avoid the scale-to-zero trap that makes
serverless platforms awkward for a gateway client.

#### Sizing for real use

**Size for concurrent games, not for autoplay batches.** `!autoplay` is a
development harness — the numbers in *Log growth* below (~286MB for a 143-game
batch) describe testing the engine, not people playing on your server. Normal
play produces a tiny fraction of that.

What production actually looks like: games are keyed by Discord thread, so any
number can run at once across any number of servers. Concurrency is cheap on
CPU, because a game spends nearly all its time waiting on the Discord and LLM
APIs. Three things do scale, though:

- **Memory.** Budget a couple hundred MB of baseline (Python, `discord.py`, the
  in-memory card cache) plus a few MB per live game — then leave headroom for
  board rendering, which allocates in bursts. 512MB works for a quiet server;
  1GB is the comfortable number once several games overlap.
- **Disk, via card images.** `data/card_images/` caches every card art the
  renderer has ever fetched. It grows with the *variety* of cards played, not
  the number of games, and it's the one directory that quietly gets large on a
  busy multi-server bot. It's pure cache — safe to delete, it re-downloads.
- **CPU, only for rendering.** Board images are composited with Pillow on the
  event loop, so a heavy `!state` render briefly pauses *every* game in the
  process. On a throttled shared-CPU plan that's the thing you'd notice first.

**Multi-server caveat:** `mtg_channel_id` is a single global setting, not
per-guild. The bot auto-responds without being mentioned in that one channel;
everywhere else — including every other server — it needs an @-mention. That
works fine, it's just worth knowing before you invite it to a second server.

The practical ceiling is almost never the host. It's your LLM API spend and
rate limits, which are identical wherever you run.

### Log growth

Two separate things grow, and they want different treatments.

**The container's stdout** is capped in `docker-compose.yml` (`max-size: 10m`,
`max-file: 5` → a 50MB ceiling). Nothing to do.

**The bot's per-game logs** under `logs/` are not capped, and they add up fast
if you run autoplay batches: two files per game, and one full 143-game batch is
**~286MB**. They're plain text, so they compress about **10x**.

The simplest policy that keeps everything is a nightly job that gzips anything
older than 30 days:

```bash
(crontab -l 2>/dev/null; echo "0 4 * * * find ~/discord-mtg-bot/logs -name 'game_*.log' -mtime +30 -exec gzip {} +") | crontab -
```

That turns a batch's 286MB into ~28MB while keeping every line greppable
(`zgrep` reads them directly). Recent logs stay uncompressed so the audit
workflow's normal `grep` still works on them.

**Archiving whole batches compresses better and matters more than it looks.**
Batch logs are highly similar to each other, so a single `tar.gz` per batch
beats per-file gzip (~12.7x vs ~10.3x measured), and — the bigger win — it
collapses hundreds of files into one. File *count* is the real cost on any
filesystem with large allocation units: on a 1MB-cluster volume, 12,829 log
files averaging 57KB occupied **13GB** for **721MB** of actual content, and
per-file gzip would have saved almost nothing because each compressed file
still burns a full cluster. Archiving those same logs to one tarball per batch
brought it to 429MB.

```bash
cd ~/discord-mtg-bot/logs && for p in $(ls game_*.log | sed -E 's/game_([0-9]{5}).*//' | sort -u); do
  tar -czf "archives/batch_${p}.tar.gz" game_${p}*.log && rm game_${p}*.log
done
```

(Verify the archive before deleting if you're scripting this unattended —
check `tar -tzf` entry count against the original file count first.)

Note that either policy still grows without bound, just far more slowly —
usually the right trade for a personal bot, since old game logs are the raw
material for debugging regressions. For a hard cap, delete instead:

```bash
(crontab -l 2>/dev/null; echo "0 4 * * * find ~/discord-mtg-bot/logs -name 'game_*.log*' -mtime +90 -delete") | crontab -
```

**Why cron rather than logrotate?** logrotate is built for a handful of
*continuously growing* files, and most of its machinery exists to rotate a file
a process is still writing to. These are many small files that are final the
moment a game ends, so none of that applies and a `find` one-liner is a better
fit. Use logrotate anyway if you already run it across all your services and
want one policy everywhere — a `logs/*.log` glob with `copytruncate` works.

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
