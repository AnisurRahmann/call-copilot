"""Tests for rec.session — dir paths, metadata, list ordering, formatters."""

from __future__ import annotations

import json
from datetime import datetime

from rec import config, session


def test_new_session_id_format():
    dt = datetime(2026, 7, 27, 14, 30, 5)
    sid = session.new_session_id(dt)
    assert sid == "2026-07-27_14-30-05"


def test_session_paths(xdg):
    sid = "2026-07-27_14-30-00"
    assert session.session_dir(sid) == config.sessions_root() / sid
    assert session.wav_path(sid).name == "recording.wav"
    assert session.session_json_path(sid).name == "session.json"
    assert session.transcript_path(sid).name == "transcript.md"


def test_create_session_dir(xdg):
    sid = "2026-07-27_14-30-00"
    d = session.create_session_dir(sid)
    assert d.exists() and d.is_dir()
    assert d == session.session_dir(sid)


def test_save_and_load_meta_round_trip(xdg):
    meta = session.SessionMeta(
        id="2026-07-27_14-30-00",
        started_at="2026-07-27T14:30:00",
        status=session.STATUS_RECORDING,
        original_device="MacBook Pro Speakers",
    )
    session.save_meta(meta)
    loaded = session.load_meta(meta.id)
    assert loaded is not None
    assert loaded.id == meta.id
    assert loaded.status == session.STATUS_RECORDING
    assert loaded.original_device == "MacBook Pro Speakers"


def test_update_meta_creates_then_patches(xdg):
    sid = "2026-07-27_14-30-00"
    # First write creates.
    session.update_meta(sid, status=session.STATUS_RECORDING, started_at="2026-07-27T14:30:00")
    # Second write patches existing fields.
    session.update_meta(sid, status=session.STATUS_TRANSCRIBED, duration=120.0, word_count=200)
    loaded = session.load_meta(sid)
    assert loaded is not None
    assert loaded.status == session.STATUS_TRANSCRIBED
    assert loaded.duration == 120.0
    assert loaded.word_count == 200
    assert loaded.started_at == "2026-07-27T14:30:00"  # unchanged by patch


def test_load_meta_missing_returns_none(xdg):
    assert session.load_meta("does-not-exist") is None


def test_load_meta_corrupt_returns_none(xdg):
    sid = "2026-07-27_14-30-00"
    session.create_session_dir(sid)
    session.session_json_path(sid).write_text("{ broken")
    assert session.load_meta(sid) is None


def test_list_sessions_newest_first(xdg):
    # Create three sessions with chronological ids.
    for sid in ["2026-07-24_09-00-00", "2026-07-25_10-00-00", "2026-07-27_14-30-00"]:
        session.update_meta(sid, status=session.STATUS_TRANSCRIBED)
    sessions = session.list_sessions()
    assert [s.id for s in sessions] == [
        "2026-07-27_14-30-00",
        "2026-07-25_10-00-00",
        "2026-07-24_09-00-00",
    ]


def test_list_sessions_empty_when_no_root(xdg):
    assert session.list_sessions() == []


def test_format_duration_human():
    assert session.format_duration_human(None) == "--"
    assert session.format_duration_human(0) == "0 sec"
    assert session.format_duration_human(45) == "45 sec"
    assert session.format_duration_human(60) == "1 min"
    assert session.format_duration_human(47 * 60) == "47 min"
    assert session.format_duration_human(3600) == "1 hr"
    assert session.format_duration_human(3 * 3600 + 5 * 60) == "3 hr 5 min"


def test_format_timestamp():
    assert session.format_timestamp(0) == "[00:00]"
    assert session.format_timestamp(65) == "[01:05]"
    assert session.format_timestamp(3599) == "[59:59]"
    assert session.format_timestamp(3600) == "[1:00:00]"
    assert session.format_timestamp(3661) == "[1:01:01]"


def test_started_at_display():
    assert session.started_at_display("2026-07-27_14-30-00") == "2026-07-27 14:30"
    # Fallback: pass through if unparseable.
    assert session.started_at_display("garbage") == "garbage"


def test_list_sessions_ignores_non_directories(xdg, monkeypatch):
    # A stray file in the sessions root must be skipped, not crash.
    root = config.sessions_root()
    root.mkdir(parents=True, exist_ok=True)
    (root / "stray.txt").write_text("oops")
    session.update_meta("2026-07-27_14-30-00", status=session.STATUS_TRANSCRIBED)
    sessions = session.list_sessions()
    assert len(sessions) == 1
    assert sessions[0].id == "2026-07-27_14-30-00"


def test_list_sessions_ignores_stray_non_timestamp_dirs(xdg):
    """Stray directories with non-conformant names don't surface as sessions.

    Real-world cause: a tap self-test or demo fixture leaves a folder like
    ``TAPTEST_1785841259`` or ``demo`` under the sessions root. Those sort
    above real timestamp ids and bury the newest session, and clicking one in
    the web UI hits the id-format guard. list_sessions filters them out so the
    list, the CLI, the MCP server, and the web UI all agree.
    """
    root = config.sessions_root()
    root.mkdir(parents=True, exist_ok=True)
    # Two strays + one real session that should be the only thing returned.
    for stray in ("TAPTEST_1785841259", "demo"):
        (root / stray).mkdir()
        (root / stray / "session.json").write_text(json.dumps({"id": stray}))
    session.update_meta("2026-08-10_01-42-47", status=session.STATUS_TRANSCRIBED)
    sessions = session.list_sessions()
    assert [s.id for s in sessions] == ["2026-08-10_01-42-47"]


def test_is_valid_session_id():
    assert session.is_valid_session_id("2026-08-10_01-42-47")
    # Rejected: traversal, non-timestamp shapes, empty.
    assert not session.is_valid_session_id("demo")
    assert not session.is_valid_session_id("TAPTEST_1785841259")
    assert not session.is_valid_session_id("../etc/passwd")
    assert not session.is_valid_session_id("")
    assert not session.is_valid_session_id("2026-8-10_1-42-47")  # not zero-padded
