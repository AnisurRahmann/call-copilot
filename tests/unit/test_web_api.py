"""Tests for rec.web.api read-only endpoints — status, sessions, detail, search.

Offline: the autouse xdg/isolate_logging fixtures isolate paths; recorder and
the transcriber are monkeypatched exactly as test_cli.py does. A real web
server runs on an ephemeral port and we hit it with urllib — no new deps.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta
from http.client import HTTPConnection

import pytest

from rec import recorder, session
from rec.web import server

# ---- shared web_server fixture (local; mirrors test_web_server) ------------


@pytest.fixture
def web_server():
    srv = server.make_server(0)
    host, port = srv.server_address[0], srv.server_address[1]
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        yield host, port
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=5)


def _get(host: str, port: int, path: str, *, headers: dict[str, str] | None = None) -> tuple[int, bytes, dict[str, str]]:
    conn = HTTPConnection(host, port, timeout=5)
    try:
        conn.request("GET", path, headers=headers or {"Host": f"127.0.0.1:{port}"})
        resp = conn.getresponse()
        body = resp.read()
        hdrs = {k.lower(): v for k, v in resp.getheaders()}
        return resp.status, body, hdrs
    finally:
        conn.close()


def _json(host, port, path):
    status, body, _ = _get(host, port, path)
    return status, json.loads(body)


# ---- session-tree helpers (inline, like test_cli.py) ----------------------
# new_session_id() resolves to the second, so tests creating multiple sessions
# in a tight loop must pass distinct `now` values or they collide on one id.

_seq = 0


def _make_session(
    *,
    status: str = session.STATUS_TRANSCRIBED,
    duration: float | None = 120.0,
    word_count: int | None = 200,
    model: str | None = "base",
    mic: bool = False,
    system: bool = True,
    transcript: str | None = None,
) -> str:
    # Each call advances one minute from a fixed epoch so ids never collide
    # within a test, and still sort newest-last by wall clock when needed.
    global _seq
    _seq += 1
    now = datetime(2025, 1, 1, 9, 0, 0) + timedelta(minutes=_seq)
    sid = session.new_session_id(now=now)
    session.update_meta(
        sid, status=status, duration=duration, word_count=word_count, model=model
    )
    session.create_session_dir(sid)
    if system:
        session.wav_path(sid).write_bytes(b"RIFF...fake-system")
    if mic:
        session.mic_wav_path(sid).write_bytes(b"RIFF...fake-mic")
    if transcript is not None:
        session.transcript_path(sid).write_text(transcript, encoding="utf-8")
    return sid


# ---- GET /api/status ------------------------------------------------------


def test_status_idle_when_not_recording(web_server, monkeypatch, xdg):
    monkeypatch.setattr(recorder, "active_pid", lambda: None)
    host, port = web_server
    status, payload = _json(host, port, "/api/status")
    assert status == 200
    assert payload["recording"] is False
    assert payload["session_id"] is None
    assert payload["job"] is None


def test_status_recording_reports_active_session(web_server, monkeypatch, xdg):
    sid = _make_session(status=session.STATUS_RECORDING, duration=None, word_count=None)
    monkeypatch.setattr(recorder, "active_pid", lambda: 4321)
    host, port = web_server
    status, payload = _json(host, port, "/api/status")
    assert status == 200
    assert payload["recording"] is True
    assert payload["session_id"] == sid
    assert payload["bytes"] >= len(b"RIFF...fake-system")
    assert payload["capture"] is not None


# ---- GET /api/sessions ----------------------------------------------------


def test_sessions_list_newest_first(web_server, xdg):
    older = _make_session(word_count=10)
    newer = _make_session(word_count=20)
    host, port = web_server
    status, payload = _json(host, port, "/api/sessions")
    assert status == 200
    ids = [row["id"] for row in payload["sessions"]]
    assert ids[0] == newer
    assert ids[1] == older
    assert payload["count"] == 2


def test_sessions_list_has_audio_flags(web_server, xdg):
    _make_session(mic=True, system=True)
    host, port = web_server
    status, payload = _json(host, port, "/api/sessions")
    assert status == 200
    row = payload["sessions"][0]
    assert row["has_mic"] is True
    assert row["has_system"] is True


def test_sessions_limit_query(web_server, xdg):
    for _ in range(5):
        _make_session()
    host, port = web_server
    status, payload = _json(host, port, "/api/sessions?limit=2")
    assert status == 200
    assert payload["count"] == 2


def test_sessions_bad_limit_is_400(web_server, xdg):
    host, port = web_server
    status, body = _json(host, port, "/api/sessions?limit=abc")
    assert status == 400
    assert body["error"]


def test_sessions_empty_state(web_server, xdg):
    host, port = web_server
    status, payload = _json(host, port, "/api/sessions")
    assert status == 200
    assert payload["sessions"] == []
    assert payload["count"] == 0


# ---- GET /api/sessions/{id} -----------------------------------------------


def test_session_detail_with_transcript(web_server, xdg):
    text = "# Transcript\n\nHello world."
    sid = _make_session(transcript=text, mic=True, system=True)
    host, port = web_server
    status, payload = _json(host, port, f"/api/sessions/{sid}")
    assert status == 200
    assert payload["id"] == sid
    assert payload["transcript"] == text
    streams = {s["kind"] for s in payload["audio_streams"]}
    assert streams == {"mic", "system"}


def test_session_detail_without_transcript(web_server, xdg):
    # A recorded-but-not-transcribed session has transcript == null.
    sid = _make_session(status=session.STATUS_RECORDED, transcript=None)
    host, port = web_server
    status, payload = _json(host, port, f"/api/sessions/{sid}")
    assert status == 200
    assert payload["transcript"] is None


def test_session_detail_missing_is_404(web_server, xdg):
    host, port = web_server
    # A valid-format id that doesn't exist -> 404, not 500.
    missing = "2020-01-01_00-00-00"
    status, payload = _json(host, port, f"/api/sessions/{missing}")
    assert status == 404
    assert payload["error"]


def test_session_detail_bad_id_is_400(web_server, xdg):
    """A non-conformant id is rejected before touching session_dir (traversal)."""
    host, port = web_server
    status, payload = _json(host, port, "/api/sessions/..%2Fetc%2fpasswd")
    assert status == 400
    assert payload["error"]


# ---- GET /api/search ------------------------------------------------------


def test_search_missing_query_is_400(web_server, xdg):
    host, port = web_server
    status, payload = _json(host, port, "/api/search")
    assert status == 400
    assert "q" in payload["error"] or payload["error"]


def test_search_empty_query_is_400(web_server, xdg):
    host, port = web_server
    status, payload = _json(host, port, "/api/search?q=")
    assert status == 400


def test_search_returns_hits_with_started_at(web_server, xdg):
    sid = _make_session(
        transcript=(
            "# Transcript\n\n"
            "[00:00] We discussed the quarterly roadmap.\n"
            "[00:12] Action items followed.\n"
        )
    )
    host, port = web_server
    status, payload = _json(host, port, "/api/search?q=roadmap")
    assert status == 200
    assert payload["count"] >= 1
    hit = payload["hits"][0]
    assert hit["session_id"] == sid
    assert hit["started_at"]  # enriched from session meta
    assert "roadmap" in hit["line"].lower() or "roadmap" in hit["context"].lower()


def test_search_no_matches_returns_guidance_not_error(web_server, xdg):
    _make_session(transcript="# Transcript\n\nNothing relevant here.")
    host, port = web_server
    status, payload = _json(host, port, "/api/search?q=zyzzyva")
    assert status == 200  # guidance, not 404
    assert payload["count"] == 0
    assert payload["guidance"]
