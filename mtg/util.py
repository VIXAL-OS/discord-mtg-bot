"""Logging utilities for the MTG engine.

GameLogger writes per-game console + Discord transcripts to logs/.
StdoutTee/StderrTee wrap sys.stdout/sys.stderr so that print() output and
tracebacks from concurrent autoplay tasks each route to the right game's
log file via a contextvar.

Extracted from mtg_game.py during the Phase 1 OSS-readability refactor.
Originally lived at lines 266-394 of the monolith.
"""

import contextvars
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional


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
            try:
                with open(self.log_files[active], "a", encoding="utf-8") as f:
                    f.write(text)
            except Exception:
                pass  # Don't let logging errors break the bot

    def flush(self):
        self.original.flush()

    def add_game(self, thread_id: int, path: Path):
        self.log_files[thread_id] = path

    def remove_game(self, thread_id: int):
        self.log_files.pop(thread_id, None)

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
