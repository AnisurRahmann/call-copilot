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

from rec import config, recorder, session
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


def _post(host: str, port: int, path: str, body: dict | None = None, *, header: bool = True) -> tuple[int, dict]:
    """POST a JSON body. By default includes the X-Requested-With header the
    CSRF guard requires; pass header=False to test the guard itself."""
    conn = HTTPConnection(host, port, timeout=5)
    try:
        payload = json.dumps(body).encode() if body is not None else b""
        headers = {"Host": f"127.0.0.1:{port}", "Content-Type": "application/json"}
        if header:
            headers["X-Requested-With"] = "rec-web"
        conn.request("POST", path, body=payload, headers=headers)
        resp = conn.getresponse()
        data = resp.read()
        try:
            parsed = json.loads(data) if data else {}
        except json.JSONDecodeError:
            parsed = {"_raw": data.decode("utf-8", "replace")}
        return resp.status, parsed
    finally:
        conn.close()


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


# ---- GET /api/sessions/{id}/audio/{stream} --------------------------------
# A deterministic payload so byte ranges are checkable. 256 bytes, each byte
# equal to its offset, so a slice [s:e+1] reads back as range(s, e+1).
_AUDIO = bytes(range(256))


def _make_session_with_audio(system: bool = True, mic: bool = False) -> str:
    global _seq
    _seq += 1
    now = datetime(2025, 1, 1, 9, 0, 0) + timedelta(minutes=_seq)
    sid = session.new_session_id(now=now)
    session.update_meta(sid, status=session.STATUS_TRANSCRIBED, duration=10.0, word_count=5)
    session.create_session_dir(sid)
    if system:
        session.wav_path(sid).write_bytes(_AUDIO)
    if mic:
        session.mic_wav_path(sid).write_bytes(_AUDIO)
    return sid


def test_audio_no_range_serves_full_body(web_server, xdg):
    sid = _make_session_with_audio()
    host, port = web_server
    status, body, hdrs = _get(host, port, f"/api/sessions/{sid}/audio/system")
    assert status == 200
    assert body == _AUDIO
    assert hdrs["content-type"] == "audio/wav"
    assert hdrs["accept-ranges"] == "bytes"
    assert hdrs["content-length"] == "256"


def test_audio_single_range_returns_206_with_exact_bytes(web_server, xdg):
    """The load-bearing case: Safari's bytes=0-1 first probe."""
    sid = _make_session_with_audio()
    host, port = web_server
    status, body, hdrs = _get(
        host, port, f"/api/sessions/{sid}/audio/system",
        headers={"Host": f"127.0.0.1:{port}", "Range": "bytes=0-1"},
    )
    assert status == 206
    assert body == _AUDIO[0:2]  # bytes(0..1) == b'\x00\x01'
    assert hdrs["content-length"] == "2"
    assert hdrs["content-range"] == "bytes 0-1/256"
    assert hdrs["accept-ranges"] == "bytes"


def test_audio_mid_range(web_server, xdg):
    sid = _make_session_with_audio()
    host, port = web_server
    status, body, hdrs = _get(
        host, port, f"/api/sessions/{sid}/audio/system",
        headers={"Host": f"127.0.0.1:{port}", "Range": "bytes=100-199"},
    )
    assert status == 206
    assert body == _AUDIO[100:200]
    assert hdrs["content-range"] == "bytes 100-199/256"
    assert hdrs["content-length"] == "100"


def test_audio_open_ended_range(web_server, xdg):
    sid = _make_session_with_audio()
    host, port = web_server
    status, body, _ = _get(
        host, port, f"/api/sessions/{sid}/audio/system",
        headers={"Host": f"127.0.0.1:{port}", "Range": "bytes=200-"},
    )
    assert status == 206
    assert body == _AUDIO[200:256]


def test_audio_suffix_range(web_server, xdg):
    sid = _make_session_with_audio()
    host, port = web_server
    status, body, _ = _get(
        host, port, f"/api/sessions/{sid}/audio/system",
        headers={"Host": f"127.0.0.1:{port}", "Range": "bytes=-16"},
    )
    assert status == 206
    assert body == _AUDIO[240:256]


def test_audio_multi_range_falls_back_to_full_body(web_server, xdg):
    """We don't implement multipart/byteranges — fall back to 200 full body."""
    sid = _make_session_with_audio()
    host, port = web_server
    status, body, _ = _get(
        host, port, f"/api/sessions/{sid}/audio/system",
        headers={"Host": f"127.0.0.1:{port}", "Range": "bytes=0-9,20-29"},
    )
    assert status == 200
    assert body == _AUDIO


def test_audio_out_of_bounds_start_is_416(web_server, xdg):
    sid = _make_session_with_audio()
    host, port = web_server
    status, body, hdrs = _get(
        host, port, f"/api/sessions/{sid}/audio/system",
        headers={"Host": f"127.0.0.1:{port}", "Range": "bytes=999-"},
    )
    assert status == 416
    assert hdrs["content-range"] == "bytes */256"


def test_audio_unknown_stream_is_404(web_server, xdg):
    sid = _make_session_with_audio()
    host, port = web_server
    status, body, _ = _get(host, port, f"/api/sessions/{sid}/audio/nope")
    assert status == 404
    assert json.loads(body)["error"]


def test_audio_missing_audio_is_404(web_server, xdg):
    """A session with no system WAV returns 404, not 500 on read."""
    sid = _make_session_with_audio(system=False, mic=True)
    host, port = web_server
    status, body, _ = _get(host, port, f"/api/sessions/{sid}/audio/system")
    assert status == 404
    assert json.loads(body)["error"]


def test_audio_mic_stream_served(web_server, xdg):
    sid = _make_session_with_audio(system=True, mic=True)
    host, port = web_server
    status, body, _ = _get(
        host, port, f"/api/sessions/{sid}/audio/mic",
        headers={"Host": f"127.0.0.1:{port}", "Range": "bytes=0-15"},
    )
    assert status == 206
    assert body == _AUDIO[0:16]


# ---- POST /api/recording/start, /stop, retranscribe, GET /api/jobs --------


def _write_config():
    config.save_config(config.default_config())


# start --------------------------------------------------------------------


def test_start_spawns_recorder_and_returns_session(web_server, monkeypatch, xdg):
    _write_config()
    spawned: list[tuple] = []
    monkeypatch.setattr(recorder, "active_pid", lambda: None)
    monkeypatch.setattr(
        recorder, "spawn_recorder",
        lambda sid, sr, ch, cap: spawned.append((sid, sr, ch, cap)) or 4321,
    )
    host, port = web_server
    status, payload = _post(host, port, "/api/recording/start", {"capture": "system"})
    assert status == 201
    assert payload["session_id"]
    assert payload["capture"] == "system"
    assert len(spawned) == 1
    assert spawned[0][3] == "system"  # capture passed through
    # The session is on disk in RECORDING state.
    assert session.load_meta(payload["session_id"]).status == session.STATUS_RECORDING


def test_start_409_if_already_recording(web_server, monkeypatch, xdg):
    _write_config()
    monkeypatch.setattr(recorder, "active_pid", lambda: 4321)
    host, port = web_server
    status, payload = _post(host, port, "/api/recording/start")
    assert status == 409
    assert payload["error"]


def test_start_without_config_gives_setup_hint(web_server, monkeypatch, xdg):
    # No config written; active_pid None so we reach the config check.
    monkeypatch.setattr(recorder, "active_pid", lambda: None)
    host, port = web_server
    status, payload = _post(host, port, "/api/recording/start")
    assert status == 500
    assert "rec setup" in payload["error"].lower()


def test_start_bad_capture_is_400(web_server, monkeypatch, xdg):
    _write_config()
    monkeypatch.setattr(recorder, "active_pid", lambda: None)
    host, port = web_server
    status, payload = _post(host, port, "/api/recording/start", {"capture": "telepathy"})
    assert status == 400
    assert payload["error"]


# stop ---------------------------------------------------------------------


def test_stop_queues_transcribe_job(web_server, monkeypatch, xdg):
    _write_config()
    sid = _make_session(status=session.STATUS_RECORDING, duration=None, word_count=None)
    monkeypatch.setattr(recorder, "active_pid", lambda: 4321)
    monkeypatch.setattr(recorder, "stop_recorder", lambda: (True, 4321))
    # Stub the worker so no real transcription runs.
    from rec.web import jobs
    monkeypatch.setattr(jobs, "_run_finish", lambda *a, **k: None)

    host, port = web_server
    status, payload = _post(host, port, "/api/recording/stop")
    assert status == 202
    assert payload["session_id"] == sid
    assert payload["job_id"]
    # The job is pollable.
    jstatus, job = _get_job(host, port, payload["job_id"])
    assert jstatus == 200
    assert job["session_id"] == sid
    # Drain the worker so the pool shuts down cleanly for the next test.
    jobs.registry.shutdown()


def test_stop_409_if_not_recording(web_server, monkeypatch, xdg):
    monkeypatch.setattr(recorder, "active_pid", lambda: None)
    host, port = web_server
    status, payload = _post(host, port, "/api/recording/stop")
    assert status == 409
    assert payload["error"]


def _get_job(host, port, job_id):
    return _json(host, port, f"/api/jobs/{job_id}")


def test_get_job_404_for_unknown_id(web_server, xdg):
    host, port = web_server
    status, payload = _json(host, port, "/api/jobs/nope0000nope")
    assert status == 404
    assert payload["error"]


# retranscribe -------------------------------------------------------------


def test_retranscribe_queues_transcribe_job(web_server, monkeypatch, xdg):
    _write_config()
    sid = _make_session(status=session.STATUS_TRANSCRIBED, model="tiny")
    from rec.web import jobs
    monkeypatch.setattr(jobs, "_run_transcribe", lambda *a, **k: None)

    host, port = web_server
    status, payload = _post(host, port, f"/api/sessions/{sid}/transcribe", {"model": "medium"})
    assert status == 202
    assert payload["job_id"]
    jobs.registry.shutdown()


def test_retranscribe_404_for_missing_session(web_server, monkeypatch, xdg):
    _write_config()
    host, port = web_server
    missing = "2020-01-01_00-00-00"
    status, payload = _post(host, port, f"/api/sessions/{missing}/transcribe", {"model": "base"})
    assert status == 404
    assert payload["error"]


def test_retranscribe_409_on_duplicate(web_server, monkeypatch, xdg):
    """A running job for the session blocks a second re-transcribe."""
    _write_config()
    sid = _make_session(status=session.STATUS_TRANSCRIBED)
    from rec.web import jobs
    started = threading.Event()
    release = threading.Event()

    def slow(*a, **k):
        started.set()
        release.wait(timeout=2.0)

    monkeypatch.setattr(jobs, "_run_transcribe", slow)
    host, port = web_server
    status1, payload1 = _post(host, port, f"/api/sessions/{sid}/transcribe", {"model": "base"})
    assert status1 == 202
    assert started.wait(timeout=2.0)
    # Second request while the first runs -> 409.
    status2, payload2 = _post(host, port, f"/api/sessions/{sid}/transcribe", {"model": "base"})
    assert status2 == 409
    release.set()
    jobs.registry.shutdown()


# ---- security hardening: CSRF, body cap, model whitelist -----------------


def test_post_without_csrf_header_is_403(web_server, xdg):
    """A POST lacking the X-Requested-With header is refused — CSRF defence.

    A cross-origin page can't set a custom header without a CORS preflight
    (which this server doesn't grant), so this blocks drive-by Start/Stop."""
    _write_config()
    host, port = web_server
    status, _ = _post(host, port, "/api/recording/start", {"capture": "mic"}, header=False)
    assert status == 403


def test_oversized_post_body_is_413(web_server, xdg):
    """A Content-Length over the cap is rejected before reading the body."""
    _write_config()
    host, port = web_server
    # Build a body just over the 16 KiB cap.
    big = {"x": "a" * (20 * 1024)}
    status, payload = _post(host, port, "/api/recording/start", big)
    assert status == 413
    assert payload["error"]


def test_retranscribe_rejects_unknown_model(web_server, monkeypatch, xdg):
    """An arbitrary model name is rejected server-side (no path/download vector)."""
    _write_config()
    sid = _make_session(status=session.STATUS_TRANSCRIBED)
    host, port = web_server
    status, payload = _post(host, port, f"/api/sessions/{sid}/transcribe", {"model": "../../etc/passwd"})
    assert status == 400
    assert "model" in payload["error"].lower() or payload["error"]

