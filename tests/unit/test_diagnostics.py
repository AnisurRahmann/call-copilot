"""Tests for failure logging (the main() wrapper) + the diagnose command."""

from __future__ import annotations

import logging

import pytest
from click.testing import CliRunner

from rec import cli, config, session
from rec import log as log_mod

# ---- failure logging via main() -------------------------------------------


@pytest.fixture
def cfg_written(xdg):
    """Write a minimal config so commands get past the config gate."""
    config.save_config(config.default_config())
    return xdg


def _run_main(argv: list[str]) -> int:
    """Invoke the full main() wrapper (exercises failure logging)."""
    return cli.main(argv)


def test_main_logs_click_exception_at_error(caplog, cfg_written, xdg):
    # rec stop with no recording -> ClickException inside the command.
    with caplog.at_level(logging.ERROR, logger="rec.cli"):
        code = _run_main(["stop"])
    assert code != 0
    assert any("command failed" in r.getMessage() and "No active recording" in r.getMessage()
               for r in caplog.records)


def test_main_logs_success_exit_code(caplog, cfg_written):
    with caplog.at_level(logging.INFO, logger="rec.cli"):
        code = _run_main(["list"])
    assert code == 0
    assert any("completed (exit=0)" in r.getMessage() for r in caplog.records)


def test_main_logs_unhandled_exception_with_traceback(monkeypatch, caplog, cfg_written):
    # Force an unexpected (non-Click) exception inside a command.
    def boom(limit):
        raise RuntimeError("synthetic internal crash")

    monkeypatch.setattr(cli.session, "list_sessions", boom)
    with caplog.at_level(logging.ERROR, logger="rec.cli"):
        code = _run_main(["list"])
    assert code != 0
    # log.exception => the record carries exception info for traceback.
    err_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert any("unhandled exception" in r.getMessage() for r in err_records)
    assert any(r.exc_info is not None for r in err_records), "expected a traceback attached"


def test_main_propagates_exit_code_from_click_exception(cfg_written):
    # ClickException.exit_code defaults to 1; verify it flows through.
    code = _run_main(["stop"])  # no recording -> ClickException
    assert code == 1


# ---- diagnose command ------------------------------------------------------


def _seed_session(session_id: str, *, status: str = "recorded",
                  recorder_log: str | None = None, transcript: str | None = None,
                  wav: bool = False) -> None:
    session.create_session_dir(session_id)
    session.update_meta(
        session_id,
        started_at="2026-07-27T14:30:00",
        status=status,
        original_device="MacBook Pro Speakers",
    )
    if recorder_log is not None:
        (session.session_dir(session_id) / "recorder.log").write_text(recorder_log)
    if transcript is not None:
        session.transcript_path(session_id).write_text(transcript)
    if wav:
        session.wav_path(session_id).write_bytes(b"RIFF")


def test_diagnose_writes_bundle_file(cfg_written, xdg):
    sid = "2026-07-27_14-30-00"
    _seed_session(sid, recorder_log="2026-07-27 INFO recorder daemon starting\n",
                  transcript="# Meeting Transcript\n\n[00:00] hi\n")
    res = CliRunner().invoke(cli.cli, ["diagnose", sid])
    assert res.exit_code == 0, res.output
    out = session.session_dir(sid) / "diagnose.md"
    assert out.exists()
    text = out.read_text()
    # All five sections present.
    assert "# Diagnose bundle" in text
    assert "**Session:** `2026-07-27_14-30-00`" in text
    assert "### session.json" in text
    assert "### session recorder.log" in text
    assert "### global log" in text
    assert "### transcript.md" in text
    assert "### config.json" in text
    # The recorder.log content was inlined.
    assert "recorder daemon starting" in text
    # The transcript content was inlined.
    assert "[00:00] hi" in text
    # Debugging guidance for the AI agent.
    assert "Summary for the AI agent" in text


def test_diagnose_includes_global_log_lines_for_session(cfg_written, xdg):
    sid = "2026-07-27_14-30-00"
    _seed_session(sid)
    # Emit a log line stamped with this session so the filter catches it.
    log_mod.set_session_context(sid)
    log_mod.set_command_context("start")
    log_mod.get_logger("rec.test").info("session-specific event")
    log_mod.set_session_context("2026-01-01_00-00-00")  # different session
    log_mod.get_logger("rec.test").info("other session event")

    res = CliRunner().invoke(cli.cli, ["diagnose", sid])
    assert res.exit_code == 0, res.output
    text = (session.session_dir(sid) / "diagnose.md").read_text()
    assert "session-specific event" in text
    assert "other session event" not in text


def test_diagnose_includes_error_lines_regardless_of_session(cfg_written, xdg):
    sid = "2026-07-27_14-30-00"
    _seed_session(sid)
    # An ERROR line attributed to a different session should still appear
    # (errors are globally relevant when debugging).
    log_mod.set_session_context("9999-01-01_00-00-00")
    log_mod.set_command_context("stop")
    log_mod.get_logger("rec.test").error("a real failure")

    CliRunner().invoke(cli.cli, ["diagnose", sid])
    text = (session.session_dir(sid) / "diagnose.md").read_text()
    assert "a real failure" in text


def test_diagnose_handles_missing_session_gracefully(cfg_written, xdg):
    # An unknown session id now errors clearly (the old behavior of writing a
    # sparse bundle silently confused users who passed a date instead of an id).
    res = CliRunner().invoke(cli.cli, ["diagnose", "does-not-exist"])
    assert res.exit_code != 0
    assert "No session matches" in res.output


def test_diagnose_stdout_flag_prints_bundle(cfg_written, xdg):
    sid = "2026-07-27_14-30-00"
    _seed_session(sid)
    res = CliRunner().invoke(cli.cli, ["diagnose", sid, "--stdout"])
    assert res.exit_code == 0, res.output
    # The bundle content appears on stdout.
    assert "# Diagnose bundle" in res.output


def test_diagnose_global_log_lines_limit_truncates(cfg_written, xdg):
    sid = "2026-07-27_14-30-00"
    _seed_session(sid)
    # Spam many session-tagged lines.
    log_mod.set_session_context(sid)
    logger = log_mod.get_logger("rec.test")
    for i in range(50):
        logger.info("line %d", i)
    CliRunner().invoke(cli.cli, ["diagnose", sid, "--global-log-lines", "10"])
    text = (session.session_dir(sid) / "diagnose.md").read_text()
    assert "lines omitted" in text  # truncation marker present
