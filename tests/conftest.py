"""Shared pytest fixtures.

Two concerns, both applied to EVERY test via autouse fixtures:
  1. Redirect XDG paths to a per-test tmp dir (so tests never touch the real
     ~/.config or ~/.local/share). This also makes global_log_path() point at
     the tmp dir, which is what `rec diagnose` reads from — keeping them
     consistent.
  2. Keep test output clean + isolate logging: configure logging once per test
     to CRITICAL on the console. Also clears the session/command context
     between tests and tears down only our own handlers (not pytest's caplog).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rec import log as log_mod


@pytest.fixture(autouse=True)
def _xdg_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Redirect XDG_CONFIG_HOME + XDG_DATA_HOME to a tmp dir for every test."""
    cfg_home = tmp_path / "config"
    data_home = tmp_path / "data"
    cfg_home.mkdir()
    data_home.mkdir()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(cfg_home))
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))


@pytest.fixture
def xdg(tmp_path: Path) -> Path:
    """Tmp path handle for tests that want to reference the redirected roots.

    The actual redirection is done by the autouse _xdg_tmp fixture; this just
    returns tmp_path for convenience + documents intent.
    """
    return tmp_path


@pytest.fixture(autouse=True)
def isolate_logging(monkeypatch: pytest.MonkeyPatch):
    """Configure logging quietly for every test.

    console=CRITICAL keeps pytest output clean. The global log file still
    captures DEBUG (derived from XDG_DATA_HOME, redirected by _xdg_tmp) so
    `rec diagnose` reads what tests wrote.
    """
    monkeypatch.delenv("REC_LOG_LEVEL", raising=False)
    log_mod.clear_context()
    log_mod.configure_logging("cli", console_level="CRITICAL")
    yield
    # Tear down only OUR handlers so they don't leak into the next test.
    # Leave pytest's caplog handler (and any external handler) intact.
    import logging
    root = logging.getLogger()
    for h in list(root.handlers):
        if getattr(h, "_rec_owned", False):
            root.removeHandler(h)
            try:
                h.close()
            except Exception:
                pass
    log_mod.clear_context()
