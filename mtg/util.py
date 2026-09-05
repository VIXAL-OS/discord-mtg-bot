"""Logging utilities for the MTG engine.

GameLogger writes per-game console + Discord transcripts to logs/.
StdoutTee/StderrTee wrap sys.stdout/sys.stderr so that print() output and
tracebacks from concurrent autoplay tasks each route to the right game's
log file via a contextvar.

Extracted from mtg_game.py during the Phase 1 OSS-readability refactor.
Originally lived at lines 266-394 of the monolith.
"""

import contextvars
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional


def strict_mode() -> bool:
    """True when MTG_STRICT=1 (or true/yes) is set in the environment.

    Read at call time, not import time, so the flag works regardless of
    import order and tests can monkeypatch os.environ.
    """
    return os.getenv("MTG_STRICT", "").strip().lower() in ("1", "true", "yes")


def maybe_reraise(exc: BaseException) -> None:
    """Escalate a swallowed exception under MTG_STRICT=1; no-op otherwise.

    Convention (June 10, 2026): pure-engine `except Exception` blocks — the
    ones that convert crashes into silently-wrong game states (actions, SBA,
    models, triggers, layers) — KEEP their existing log line (audit greps
    depend on the tags) and add `maybe_reraise(e)` as the last statement:

        except Exception as e:
            print(f"[TAG] ...: {e}")   # unchanged
            maybe_reraise(e)

    Production behavior is unchanged (log-and-continue, live games
    stay crash-proof). CI and autoplay audit batches run with MTG_STRICT=1 so
    swallowed engine exceptions surface loudly where they're cheap to catch.

    Crash-barrier catches deliberately do NOT get this: LLM/network calls,
    top-level Discord command handlers, and graceful-degradation fallbacks
    that have a real recovery path (e.g. SBA delegation falling back to the
    inline checker).
    """
    if strict_mode():
        raise exc


_GIT_SHA_CACHE: Optional[str] = None


def git_sha() -> str:
    """Short git SHA of the running checkout, cached after first call.

    July 24, 2026: stamped into [GAME-INIT] so batch-vintage checking is one
    grep instead of snowflake-timestamp archaeology (every audit round used
    to re-derive "which commit was the bot running?" from game-ID decode +
    corroborating log tags). Returns "unknown" outside a git checkout.
    """
    global _GIT_SHA_CACHE
    if _GIT_SHA_CACHE is None:
        import subprocess
        try:
            _GIT_SHA_CACHE = subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                cwd=Path(__file__).resolve().parent.parent,
                capture_output=True, text=True, timeout=5,
            ).stdout.strip() or "unknown"
        except (OSError, subprocess.SubprocessError, ValueError):
            _GIT_SHA_CACHE = "unknown"
    return _GIT_SHA_CACHE


class GameLogger:
    """Logs game console output and Discord messages to per-game files."""

    def __init__(self, thread_id: int):
        self.thread_id = thread_id
        self.log_dir = Path("logs")
        self.log_dir.mkdir(exist_ok=True)

        self.console_path = self.log_dir / f"game_{thread_id}_console.log"
        self.discord_path = self.log_dir / f"game_{thread_id}_discord.log"

        # Write headers
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self._write_console(f"=== Game {thread_id} started at {timestamp} ===")
        self._write_discord(f"=== Game {thread_id} Discord log started at {timestamp} ===")

    def _write_console(self, line: str):
        try:
            with open(self.console_path, "a", encoding="utf-8") as f:
                f.write(f"[{datetime.now().strftime('%H:%M:%S')}] {line}\n")
        except Exception:
            pass

    def _write_discord(self, line: str):
        try:
            with open(self.discord_path, "a", encoding="utf-8") as f:
                f.write(f"[{datetime.now().strftime('%H:%M:%S')}] {line}\n")
        except Exception:
            pass

    def log_discord_out(self, content: str, author: str = "Bot"):
        """Log an outgoing Discord message."""
        self._write_discord(f"{author}: {content}")

    def log_discord_in(self, content: str, author: str):
        """Log an incoming player message."""
        self._write_discord(f"{author}: {content}")


class StdoutTee:
    """Wraps sys.stdout to copy all print() output to the correct game log file.

    Uses contextvars so that concurrent asyncio tasks (parallel autoplay)
    each route output to their own game's log file independently.
    """

    def __init__(self, original_stdout):
        self.original = original_stdout
        self.log_files: Dict[int, Path] = {}  # thread_id -> console log path
        self._write_stats: Dict[int, Dict[str, float]] = {}
        self._active_thread_var: contextvars.ContextVar[Optional[int]] = contextvars.ContextVar(
            'stdout_tee_active_thread', default=None
        )

    @property
    def active_thread(self) -> Optional[int]:
        return self._active_thread_var.get()

    @active_thread.setter
    def active_thread(self, value: Optional[int]):
        self._active_thread_var.set(value)

    def write(self, text):
        self.original.write(text)
        active = self._active_thread_var.get()
        if active and active in self.log_files:
            started = time.perf_counter()
            try:
                with open(self.log_files[active], "a", encoding="utf-8") as f:
                    f.write(text)
            except Exception:
                pass  # Don't let logging errors break the bot
            finally:
                elapsed_ms = (time.perf_counter() - started) * 1000.0
                stats = self._write_stats.setdefault(active, {
                    "writes": 0, "bytes": 0, "total_ms": 0.0,
                    "max_ms": 0.0, "slow_writes": 0,
                })
                stats["writes"] += 1
                stats["bytes"] += len(str(text).encode("utf-8"))
                stats["total_ms"] += elapsed_ms
                stats["max_ms"] = max(stats["max_ms"], elapsed_ms)
                if elapsed_ms >= 10.0:
                    stats["slow_writes"] += 1

    def flush(self):
        self.original.flush()

    def add_game(self, thread_id: int, path: Path):
        self.log_files[thread_id] = path

    def remove_game(self, thread_id: int):
        self.log_files.pop(thread_id, None)
        stats = self._write_stats.pop(thread_id, None)
        if stats:
            average_ms = stats["total_ms"] / max(stats["writes"], 1)
            self.original.write(
                f"[STDOUT-TEE-STATS] game={thread_id} "
                f"writes={int(stats['writes'])} bytes={int(stats['bytes'])} "
                f"total_ms={stats['total_ms']:.2f} "
                f"avg_ms={average_ms:.3f} max_ms={stats['max_ms']:.2f} "
                f"slow_ge_10ms={int(stats['slow_writes'])}\n"
            )

    def __getattr__(self, name):
        return getattr(self.original, name)


class StderrTee:
    """Wraps sys.stderr to capture Python tracebacks and discord.py logging
    warnings (heartbeat blocks, reconnects) into the per-game log file.

    Shares routing state (active_thread + log_files) with the StdoutTee
    so existing `_stdout_tee.active_thread = X` calls steer stderr too
    without needing a second setter at every call site.

    Also always writes to a shared fallback log (`logs/stderr.log`) because
    discord.py's heartbeat task doesn't inherit the game command's
    contextvar — its warnings would otherwise land in no game log at all.
    """

    def __init__(self, original_stderr, stdout_tee: 'StdoutTee', fallback_path: Path):
        self.original = original_stderr
        self._stdout_tee = stdout_tee  # share active_thread + log_files
        self.fallback_path = fallback_path

    def write(self, text):
        self.original.write(text)
        # Route to active game's log if a game command set the contextvar
        try:
            active = self._stdout_tee._active_thread_var.get()
        except LookupError:
            active = None
        if active and active in self._stdout_tee.log_files:
            try:
                with open(self._stdout_tee.log_files[active], "a", encoding="utf-8") as f:
                    f.write(text)
            except Exception:
                pass
        # Always also write to the shared fallback log — heartbeat warnings
        # from discord.py's own task don't inherit our contextvar, so this
        # is the only place they'll reliably land.
        try:
            with open(self.fallback_path, "a", encoding="utf-8") as f:
                f.write(text)
        except Exception:
            pass

    def flush(self):
        self.original.flush()

    def __getattr__(self, name):
        return getattr(self.original, name)


# ---------------------------------------------------------------------------
# Billing / balance errors (Sep 4, 2026)
#
# The live shape that motivated this: on Sep 3 at 8:39 PM the Anthropic
# account ran dry mid-conversation. The error is an HTTP **400**
# ``invalid_request_error: Your credit balance is too low to access the
# Anthropic API`` — a status code cannot classify it, only the text can.
# DeepSeek returns 402 ``Insufficient Balance``; DashScope reports the account
# "in arrears"; OpenAI-compatible hosts say "exceeded your current quota ...
# billing". Rate limits (429), timeouts and resets are NOT billing errors.
#
# The registry lets the Discord bot subscribe a DM without the engine knowing
# anything about Discord. Keyed by name so re-registration replaces rather
# than stacks (a second bot object must not produce two DMs).
# ---------------------------------------------------------------------------
import re as _re
from typing import Callable as _Callable

_BILLING_ERROR_RE = _re.compile(
    r"credit balance|insufficient[ _]balance|insufficient[ _]quota|"
    r"payment required|arrear|exceeded your current quota|\bbilling\b",
    _re.IGNORECASE,
)

_BILLING_ALERT_CALLBACKS: Dict[str, "_Callable[[str, BaseException], None]"] = {}


def looks_like_billing_error(exc: BaseException) -> bool:
    """True when an API error is the account running dry, not a transient."""
    if getattr(exc, "status_code", None) == 402:
        return True
    return bool(_BILLING_ERROR_RE.search(str(exc)))


def register_billing_alert_callback(name: str, callback) -> None:
    """Subscribe ``callback(provider_or_model: str, exc)`` to billing errors."""
    _BILLING_ALERT_CALLBACKS[name] = callback


def notify_billing_error(provider: str, exc: BaseException) -> None:
    """Fan a billing error out to every registered callback.

    Callbacks are responsible for their own error handling — a subscriber
    that raises would otherwise replace the exception being reported.
    """
    for callback in list(_BILLING_ALERT_CALLBACKS.values()):
        callback(provider, exc)
