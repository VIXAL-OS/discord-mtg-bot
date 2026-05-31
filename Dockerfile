# discord-mtg-bot — Magic: The Gathering Discord bot. Bundles a JRE for the
# optional XMage rules bridge (Tier 2.5). No Whisper/ffmpeg in this fork — it
# has no transcription, so the image stays lean.
#
#   docker compose up -d --build      # start (or restart after a git pull)
#   docker compose logs -f            # live logs
#
FROM python:3.11-slim

# openjdk JRE runs the prebuilt XMage bridge JAR (see the COPY note below).
RUN apt-get update && apt-get install -y --no-install-recommends \
        openjdk-17-jre-headless \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Python deps in their own layer so editing code doesn't re-run pip.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Application code. Mutable state + secrets (config.json, .env, data/, logs/)
# are .dockerignore'd and bind-mounted at runtime — see docker-compose.yml.
#
# The XMage bridge JAR (rules/xmage-bridge/target/*.jar) is NOT in git and is
# NOT built here. To bundle the bridge, run `cd rules/xmage-bridge && mvn
# package` on the host BEFORE `docker compose up --build` so the JAR gets baked
# in by this COPY (or add a Maven build stage). Without it the engine falls back
# to template + LLM resolution — no crash, just lighter rules coverage.
COPY . .

ENV PYTHONUNBUFFERED=1 \
    PYTHONUTF8=1 \
    PYTHONIOENCODING=utf-8

CMD ["python", "bot.py"]
