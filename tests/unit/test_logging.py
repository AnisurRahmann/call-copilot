"""Tests for rec.log — context stamping, level mapping, destinations, idempotency."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from rec import log as log_mod

# ---- level mapping ---------------------------------------------------------


def test_verbosity_to_level_defaults_to_notset():
    # NOTSET means "honour REC_LOG_LEVEL env, else WARNING".
    assert log_mod.verbosity_to_level(0, quiet=False) == logging.NOTSET


def test_verbosity_to_level_v_is_info():
    assert log_mod.verbosity_to_level(1, quiet=False) == logging.INFO


def test_verbosity_to_level_vv_is_debug():
    assert log_mod.verbosity_to_level(2, quiet=False) == logging.DEBUG
    assert log_mod.verbosity_to_level(5, quiet=False) == logging.DEBUG


def test_verbosity_to_level_quiet_wins():
    assert log_mod.verbosity_to_level(2, quiet=True) == logging.CRITICAL


def test_resolve_console_level_rec_log_level_env(monkeypatch):
    monkeypatch.setenv("REC_LOG_LEVEL", "DEBUG")
    assert log_mod._resolve_console_level("NOTSET") == logging.DEBUG


def test_resolve_console_level_default_warning(monkeypatch):
    monkeypatch.delenv("REC_LOG_LEVEL", raising=False)
    assert log_mod._resolve_console_level("NOTSET") == logging.WARNING


def test_resolve_console_level_explicit_string_wins_over_env(monkeypatch):
    monkeypatch.setenv("REC_LOG_LEVEL", "DEBUG")
    assert log_mod._resolve_console_level("error") == logging.ERROR


# ---- context stamping ------------------------------------------------------


def test_context_filter_stamps_session_and_command():
    log_mod.set_session_context("2026-07-27_14-30-00")
    log_mod.set_command_context("start")
    rec = logging.LogRecord(
        name="x", level=logging.INFO, pathname="", lineno=0,
        msg="hello", args=(), exc_info=None,
    )
    flt = log_mod._ContextFilter()
    assert flt.filter(rec) is True
    assert rec.session_id == "2026-07-27_14-30-00"
    assert rec.command == "start"


def test_context_filter_defaults_to_dash():
    log_mod.clear_context()
    rec = logging.LogRecord(
        name="x", level=logging.INFO, pathname="", lineno=0,
        msg="hello", args=(), exc_info=None,
    )
    log_mod._ContextFilter().filter(rec)
    assert rec.session_id == "-"
    assert rec.command == "-"


# ---- configure_logging destinations ---------------------------------------


def test_cli_config_creates_global_log_file(tmp_path):
    log_mod.configure_logging("cli", console_level="CRITICAL", log_dir=tmp_path)
    logger = log_mod.get_logger("rec.test")
    logger.info("a line")
    glog = tmp_path / "rec.log"
    assert glog.exists()
    text = glog.read_text()
    assert "a line" in text
    # File formatter includes the session/command stamp.
    assert "[-|-]" in text  # no context set -> dashes


def test_daemon_config_writes_session_recorder_log(tmp_path):
    session_log = tmp_path / "session" / "recorder.log"
    log_mod.configure_logging(
        "daemon", session_id="SID", log_dir=tmp_path, session_log_path=session_log
    )
    logger = log_mod.get_logger("rec.recorder")
    logger.info("daemon alive")
    assert session_log.exists()
    assert "daemon alive" in session_log.read_text()
    # Daemon session stamp present.
    assert "[SID" in session_log.read_text()


def test_daemon_has_no_console_handler(tmp_path):
    log_mod.configure_logging("daemon", session_log_path=tmp_path / "s.log", log_dir=tmp_path)
    handlers = logging.getLogger().handlers
    # No RichHandler in daemon mode (it has no TTY).
    from rich.logging import RichHandler
    assert not any(isinstance(h, RichHandler) for h in handlers)


def test_cli_has_console_handler(tmp_path):
    log_mod.configure_logging("cli", console_level="DEBUG", log_dir=tmp_path)
    from rich.logging import RichHandler
    handlers = logging.getLogger().handlers
    assert any(isinstance(h, RichHandler) for h in handlers)


# ---- idempotency -----------------------------------------------------------


def test_configure_logging_idempotent_no_duplicate_handlers(tmp_path):
    log_mod.configure_logging("cli", log_dir=tmp_path)
    log_mod.configure_logging("cli", log_dir=tmp_path)
    log_mod.configure_logging("cli", log_dir=tmp_path)
    # Count only OUR handlers — pytest/caplog inject their own that we must
    # leave alone. CLI installs exactly one file + one console = 2, regardless
    # of how many times configure_logging is called.
    owned = [h for h in logging.getLogger().handlers if getattr(h, "_rec_owned", False)]
    assert len(owned) == 2


def test_configure_logging_rejects_unknown_context(tmp_path):
    with pytest.raises(ValueError):
        log_mod.configure_logging("worker", log_dir=tmp_path)


# ---- integration: a real log line carries the right stamp -----------------


def test_logged_line_carries_session_and_command(tmp_path):
    log_mod.set_session_context("2026-07-27_14-30-00")
    log_mod.set_command_context("stop")
    log_mod.configure_logging("cli", console_level="CRITICAL", log_dir=tmp_path)
    log_mod.get_logger("rec.session").info("session meta updated")
    text = (tmp_path / "rec.log").read_text()
    assert "[2026-07-27_14-30-00|stop]" in text


# ---- global_log_path respects XDG -----------------------------------------


def test_global_log_path_uses_xdg_data_home(monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", "/tmp/fake_xdg_data")
    p = log_mod.global_log_path()
    assert p == Path("/tmp/fake_xdg_data/rec/logs/rec.log")


def test_global_log_path_explicit_log_dir_wins(tmp_path):
    assert log_mod.global_log_path(tmp_path) == tmp_path / "rec.log"
