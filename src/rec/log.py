"""Unified logging for Call Copilot.

Three destinations, split by purpose:

  +-------------------+-----------+----------+----------------------------------+
  | Destination       | Writers   | Level    | Purpose                          |
  +-------------------+-----------+----------+----------------------------------+
  | Console (stderr)  | CLI only  | WARNING* | User-facing; clean by default.   |
  | Global log file   | CLI+daemon| DEBUG    | Monitor surface: tail -f it.     |
  | Session log file  | Daemon    | DEBUG    | Per-session post-mortem detail.  |
  +-------------------+-----------+----------+----------------------------------+
  * WARNING by default; -v -> INFO, -vv/--debug -> DEBUG, --quiet -> CRITICAL.
    REC_LOG_LEVEL env var overrides. The daemon never writes to the console.

Every record is stamped with `session_id` and `command` via a logging.Filter so a
single line in the global log tells you what session + what command produced it.

Idempotent: `configure_logging(...)` may be called many times (per test, per CLI
invocation) — it clears existing handlers each time so no duplicates accumulate.
"""

from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

from rich.console import Console as RichConsole
from rich.logging import RichHandler

# Module-level context, mutated by set_session_context / set_command_context.
# Read by _ContextFilter at emit time so any logger picks it up after it's set.
_SESSION_ID: str | None = None
_COMMAND: str | None = None

# Plain formatter for file output (no ANSI). Console uses RichHandler's format.
_FILE_FORMAT = "%(asctime)s %(levelname)-7s %(name)s [%(session_id)s|%(command)s] %(message)s"
_FILE_DATEFMT = "%Y-%m-%d %H:%M:%S"

# Cap file sizes so a long-running recorder can't fill the disk.
_GLOBAL_MAX_BYTES = 5 * 1024 * 1024  # 5 MB
_GLOBAL_BACKUP_COUNT = 3

_LEVELS = {
    "CRITICAL": logging.CRITICAL,
    "ERROR": logging.ERROR,
    "WARNING": logging.WARNING,
    "INFO": logging.INFO,
    "DEBUG": logging.DEBUG,
}


# ---- context stamping -----------------------------------------------------


class _ContextFilter(logging.Filter):
    """Inject `session_id` and `command` into every record (default to '-').

    Attach to a handler (not a logger) so it runs right before formatting,
    regardless of which logger produced the record.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.session_id = _SESSION_ID or "-"
        record.command = _COMMAND or "-"
        return True


def set_session_context(session_id: str | None) -> None:
    """Stamp subsequent records with the active session id."""
    global _SESSION_ID
    _SESSION_ID = session_id


def set_command_context(command: str | None) -> None:
    """Stamp subsequent records with the active subcommand (start/stop/...)."""
    global _COMMAND
    _COMMAND = command


def clear_context() -> None:
    """Reset both stamps (used between tests)."""
    global _SESSION_ID, _COMMAND
    _SESSION_ID = None
    _COMMAND = None


# ---- paths -----------------------------------------------------------------


def global_log_path(log_dir: Path | None = None) -> Path:
    """Where the global (cross-session) log lives."""
    if log_dir is not None:
        return log_dir / "rec.log"
    base = os.environ.get("XDG_DATA_HOME")
    root = Path(base) if base else Path.home() / ".local" / "share"
    return root / "rec" / "logs" / "rec.log"


# ---- level resolution ------------------------------------------------------


def _resolve_console_level(console_level: str | int) -> int:
    """Accept a level name (DEBUG/INFO/...), an int, or 'NOTSET'.

    NOTSET (the int 0 or the string 'NOTSET') / None / '' means: honour the
    REC_LOG_LEVEL env var, else default WARNING. A concrete int (e.g. 20) or a
    named level is used as-is.
    """
    # NOTSET (0) is the sentinel for "no explicit choice" — resolve via env/default.
    if isinstance(console_level, int):
        if console_level == logging.NOTSET:
            pass  # fall through to env/default resolution
        else:
            return console_level
    elif console_level and console_level != "NOTSET":
        return _LEVELS.get(console_level.upper(), logging.WARNING)
    env = os.environ.get("REC_LOG_LEVEL", "").upper()
    if env in _LEVELS:
        return _LEVELS[env]
    return logging.WARNING


def verbosity_to_level(verbose: int, quiet: bool) -> int:
    """Map CLI flags to a console level. --quiet wins; REC_LOG_LEVEL still wins."""
    if quiet:
        return logging.CRITICAL
    if verbose >= 2:
        return logging.DEBUG
    if verbose == 1:
        return logging.INFO
    return logging.NOTSET  # -> _resolve_console_level (env or WARNING)


# ---- configuration ---------------------------------------------------------


def configure_logging(
    context: str = "cli",
    *,
    session_id: str | None = None,
    console_level: str | int = "NOTSET",
    log_dir: Path | None = None,
    session_log_path: Path | None = None,
) -> logging.Logger:
    """Configure the root logger for either the CLI or the daemon process.

    Idempotent: clears all existing handlers first.

    - context="cli":     RichHandler->stderr + RotatingFileHandler->global log.
    - context="daemon":  RotatingFileHandler->global log + FileHandler->session
                          log (plain); NO console handler (daemon has no TTY).

    `log_dir` overrides the global log's directory (used by tests). The global
    log file is created even in daemon mode so `tail -f` shows both processes.

    `session_id` stamps every record with the active session.
    `session_log_path` (daemon only) is the per-session recorder.log.
    """
    if context not in ("cli", "daemon"):
        raise ValueError(f"unknown logging context: {context!r}")

    if session_id is not None:
        set_session_context(session_id)

    root = logging.getLogger()
    # Idempotency: tear down only OUR prior handlers. We must NOT remove
    # handlers we don't own (e.g. pytest's caplog LogCaptureHandler, or
    # handlers attached by an embedding application) — doing so breaks log
    # capture in tests. Tag every handler we install with `_rec_owned = True`.
    for h in list(root.handlers):
        if getattr(h, "_rec_owned", False):
            root.removeHandler(h)
            try:
                h.close()
            except Exception:  # pragma: no cover — defensive
                pass

    ctx_filter = _ContextFilter()
    file_formatter = logging.Formatter(_FILE_FORMAT, datefmt=_FILE_DATEFMT)

    # Global log: shared by CLI and daemon, always DEBUG.
    gpath = global_log_path(log_dir)
    gpath.parent.mkdir(parents=True, exist_ok=True)
    global_handler = RotatingFileHandler(
        gpath, maxBytes=_GLOBAL_MAX_BYTES, backupCount=_GLOBAL_BACKUP_COUNT, encoding="utf-8"
    )
    global_handler.setLevel(logging.DEBUG)
    global_handler.setFormatter(file_formatter)
    global_handler.addFilter(ctx_filter)

    handlers: list[logging.Handler] = [global_handler]

    if context == "cli":
        level = _resolve_console_level(console_level)
        # RichHandler creates its own Console, which defaults to STDOUT. We want
        # logs on STDERR so they stay separate from the CLI's click.echo() user
        # output on stdout (and so `rec list > file` captures only the table).
        err_console = RichConsole(stderr=True)
        console_handler = RichHandler(
            show_time=True,
            show_level=True,
            show_path=False,
            markup=False,
            rich_tracebacks=True,
            console=err_console,
        )
        console_handler.setLevel(level)
        console_handler.addFilter(ctx_filter)
        handlers.append(console_handler)
        root.setLevel(logging.DEBUG)  # handlers gate what's shown
    else:
        # Daemon: also write to the per-session recorder.log (plain, no ANSI).
        if session_log_path is not None:
            session_log_path.parent.mkdir(parents=True, exist_ok=True)
            session_handler = logging.FileHandler(session_log_path, encoding="utf-8")
            session_handler.setLevel(logging.DEBUG)
            session_handler.setFormatter(file_formatter)
            session_handler.addFilter(ctx_filter)
            handlers.append(session_handler)
        root.setLevel(logging.DEBUG)

    for h in handlers:
        # Mark as owned by rec so a later configure_logging() (or a test's
        # teardown) can find and remove just our handlers, leaving external
        # handlers (pytest's caplog, an embedder's) intact.
        h._rec_owned = True  # type: ignore[attr-defined]
        root.addHandler(h)

    return root


def get_logger(name: str) -> logging.Logger:
    """Get a logger; modules pass __name__ so they appear as rec.<module>."""
    return logging.getLogger(name)
