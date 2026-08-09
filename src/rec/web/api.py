"""HTTP endpoint handlers for ``rec web``.

Each handler takes the :class:`rec.web.server.WebHandler` instance (plus any
path params) and writes a response via the handler's ``_send_json`` /
``_send_bytes`` helpers. The :func:`register` function is called once from
:mod:`rec.web.server` to add the /api/* routes to the routing table as this
module grows; it imports cleanly even before the mutating endpoints (T7) and
the job registry (T6) exist, because those are referenced lazily.

Read-only endpoints live here and are safe to exercise in tests with no audio
device and no model. The mutating endpoints (start/stop/retranscribe) are added
in T7 and call into :mod:`rec.web.jobs`, which is imported lazily so a missing
dependency never breaks the read path.
"""

from __future__ import annotations

import urllib.parse
from http import HTTPStatus
from typing import TYPE_CHECKING

from .. import config, recorder, session
from .ranges import parse_range
from .server import _ApiError

if TYPE_CHECKING:
    from .server import WebHandler


def register(routes: dict[tuple[str, str], tuple]) -> None:
    """Add the /api/* routes to the server's routing table.

    Called once at import time from :mod:`rec.web.server`. Each task (T4 read,
    T5 audio, T7 mutate) appends its routes here as the handlers land.
    """
    routes.update(
        {
            ("GET", "/api/status"): (get_status, []),
            ("GET", "/api/sessions"): (get_sessions, []),
            ("GET", "/api/sessions/{id}"): (get_session_detail, ["id"]),
            ("GET", "/api/sessions/{id}/audio/{stream}"): (get_audio, ["id", "stream"]),
            ("GET", "/api/search"): (get_search, []),
            ("POST", "/api/recording/start"): (post_start, []),
            ("POST", "/api/recording/stop"): (post_stop, []),
            ("GET", "/api/jobs/{job_id}"): (get_job, ["job_id"]),
            ("POST", "/api/sessions/{id}/transcribe"): (post_transcribe, ["id"]),
        }
    )


# ---- helpers --------------------------------------------------------------


def _meta_to_summary(meta: session.SessionMeta) -> dict:
    """One row of the session list: the fields the table renders."""
    sid = meta.id
    return {
        "id": sid,
        "started_at": meta.started_at,
        "status": meta.status,
        "duration": meta.duration,
        "duration_human": session.format_duration_human(meta.duration),
        "word_count": meta.word_count,
        "model": meta.model,
        "has_mic": session.mic_wav_path(sid).exists(),
        "has_system": session.wav_path(sid).exists(),
    }


def _audio_streams(sid: str) -> list[dict]:
    """The playable audio streams for a session, in a stable order."""
    streams = []
    for kind, path in (("system", session.wav_path(sid)), ("mic", session.mic_wav_path(sid))):
        if path.exists():
            try:
                size = path.stat().st_size
            except OSError:
                continue
            streams.append(
                {
                    "kind": kind,
                    "url": f"/api/sessions/{sid}/audio/{kind}",
                    "bytes": size,
                }
            )
    return streams


def _transcribing_session_id() -> str | None:
    """Id of the session currently in STATUS_TRANSCRIBING, if any."""
    for meta in session.list_sessions():
        if meta.status == session.STATUS_TRANSCRIBING:
            return meta.id
    return None


# ---- GET /api/status ------------------------------------------------------


def get_status(h: WebHandler) -> None:
    """What the machine is doing right now.

    ``recording`` is true when the recorder daemon is alive (``active_pid``),
    not when a session.json merely says so — a crashed daemon leaves a stale
    'recording' status that this check corrects.
    """
    pid = recorder.active_pid()
    recording = pid is not None
    payload: dict = {"recording": recording}

    sid = None
    if recording:
        # The active session is the one in STATUS_RECORDING, else the newest.
        for meta in session.list_sessions():
            if meta.status == session.STATUS_RECORDING:
                sid = meta.id
                break
        if sid is None and session.list_sessions():
            sid = session.list_sessions()[0].id
        payload["session_id"] = sid
        meta = session.load_meta(sid) if sid else None
        payload["started_at"] = meta.started_at if meta else None
        payload["elapsed_s"] = _elapsed_seconds(meta) if meta else None
        payload["bytes"] = _session_bytes(sid) if sid else 0
        payload["capture"] = (meta.extra.get("capture") if meta else None) or _default_capture()
    else:
        transcribing = _transcribing_session_id()
        payload["session_id"] = transcribing
        payload["job"] = None  # populated in T6 from the job registry
        payload["started_at"] = None
        payload["elapsed_s"] = None
        payload["bytes"] = 0
        payload["capture"] = None

    h._send_json(HTTPStatus.OK, payload)


def _elapsed_seconds(meta: session.SessionMeta | None) -> float | None:
    """Seconds since the session started, if its started_at parses."""
    from datetime import datetime

    if not meta or not meta.started_at:
        return None
    try:
        started = datetime.fromisoformat(meta.started_at)
    except ValueError:
        return None
    return max(0.0, (datetime.now() - started).total_seconds())


def _session_bytes(sid: str | None) -> int:
    """Total audio bytes for a session (sum of present WAVs). 0 if none/unknown."""
    if not sid:
        return 0
    total = 0
    for p in (session.wav_path(sid), session.mic_wav_path(sid)):
        try:
            if p.exists():
                total += p.stat().st_size
        except OSError:
            pass
    return total


def _default_capture() -> str:
    """The configured default capture mode, or 'mic+system' if no config."""
    try:
        return config.load_config().capture
    except Exception:
        return "mic+system"


# ---- GET /api/sessions ----------------------------------------------------


def get_sessions(h: WebHandler) -> None:
    """The session list, newest-first (list_sessions already sorts)."""
    metas = session.list_sessions()
    # Honour an optional ?limit query; default 50 keeps the payload bounded.
    qs = urllib.parse.parse_qs(urllib.parse.urlsplit(h.path).query)
    limit = 50
    if "limit" in qs and qs["limit"]:
        try:
            limit = max(1, min(int(qs["limit"][0]), 500))
        except ValueError:
            raise _ApiError(HTTPStatus.BAD_REQUEST, "limit must be an integer.")
    rows = [_meta_to_summary(m) for m in metas[:limit]]
    h._send_json(HTTPStatus.OK, {"sessions": rows, "count": len(rows)})


# ---- GET /api/sessions/{id} -----------------------------------------------


def get_session_detail(h: WebHandler, id: str) -> None:
    """Meta + transcript (raw markdown) + audio streams for one session."""
    meta = session.load_meta(id)
    if meta is None:
        raise _ApiError(HTTPStatus.NOT_FOUND, f"No session {id}.")
    transcript = None
    tpath = session.transcript_path(id)
    if tpath.exists():
        try:
            transcript = tpath.read_text(encoding="utf-8")
        except OSError as e:
            raise _ApiError(
                HTTPStatus.INTERNAL_SERVER_ERROR, f"Could not read transcript: {e}."
            ) from e
    h._send_json(
        HTTPStatus.OK,
        {
            "id": meta.id,
            "started_at": meta.started_at,
            "status": meta.status,
            "duration": meta.duration,
            "duration_human": session.format_duration_human(meta.duration),
            "word_count": meta.word_count,
            "model": meta.model,
            "transcript": transcript,
            "audio_streams": _audio_streams(id),
        },
    )


# ---- GET /api/sessions/{id}/audio/{stream} --------------------------------


_AUDIO_KINDS = {"system", "mic"}


def get_audio(h: WebHandler, id: str, stream: str) -> None:
    """Serve a session's WAV, Range-capable so Safari's <audio> plays and seeks.

    ``stream`` is 'system' (recording.wav) or 'mic' (recording-mic.wav). The
    stdlib handler doesn't implement Range; Safari sends ``bytes=0-1`` first
    and refuses to play without a correct 206. We hand-roll it with parse_range.

    - single range  → 206 with Content-Range / Accept-Ranges / Content-Length
    - no Range      → 200 with the full body
    - multi-range   → 200 full body (multipart/byteranges not implemented)
    - invalid/OOB   → 416 with Content-Range: bytes */T
    """
    if stream not in _AUDIO_KINDS:
        raise _ApiError(HTTPStatus.NOT_FOUND, f"Unknown audio stream '{stream}'.")
    path = session.wav_path(id) if stream == "system" else session.mic_wav_path(id)
    if not path.exists():
        raise _ApiError(HTTPStatus.NOT_FOUND, f"No {stream} audio for session {id}.")

    try:
        data = path.read_bytes()
    except OSError as e:
        raise _ApiError(
            HTTPStatus.INTERNAL_SERVER_ERROR, f"Could not read audio: {e}."
        ) from e

    total = len(data)
    spec = parse_range(h.headers.get("Range"), total)

    if spec.kind == "none":
        # Full body. Advertise range support so the client knows it can seek.
        h._send_bytes(
            HTTPStatus.OK,
            data,
            "audio/wav",
            extra_headers=[("Accept-Ranges", "bytes")],
        )
        return

    if spec.kind == "multi":
        # Fall back to the full body rather than attempting multipart/byteranges.
        h._send_bytes(
            HTTPStatus.OK,
            data,
            "audio/wav",
            extra_headers=[("Accept-Ranges", "bytes")],
        )
        return

    if spec.kind == "invalid":
        # Unsatisfiable. Per RFC 7233, include Content-Range: bytes */T.
        h.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
        h.send_header("Content-Range", f"bytes */{total}")
        h.send_header("Content-Length", "0")
        h.end_headers()
        return

    # Single satisfiable range → 206 Partial Content.
    chunk = data[spec.start : spec.end + 1]
    h.send_response(HTTPStatus.PARTIAL_CONTENT)
    h.send_header("Content-Type", "audio/wav")
    h.send_header("Content-Length", str(len(chunk)))
    h.send_header("Content-Range", f"bytes {spec.start}-{spec.end}/{total}")
    h.send_header("Accept-Ranges", "bytes")
    h.end_headers()
    if h.command != "HEAD":
        h.wfile.write(chunk)


# ---- GET /api/search ------------------------------------------------------


def get_search(h: WebHandler) -> None:
    """Full-text search over transcripts via the existing FTS5 layer in index.py."""
    from .. import index  # imported here so the read-only server never carries
                           # the index module at import time unnecessarily.

    qs = urllib.parse.parse_qs(urllib.parse.urlsplit(h.path).query)
    raw_q = qs.get("q", [""])[0]
    query = raw_q.strip()
    if not query:
        raise _ApiError(HTTPStatus.BAD_REQUEST, "Missing 'q' query parameter.")
    limit = 10
    if "limit" in qs and qs["limit"]:
        try:
            limit = max(1, min(int(qs["limit"][0]), 50))
        except ValueError:
            raise _ApiError(HTTPStatus.BAD_REQUEST, "limit must be an integer.")

    try:
        index.ensure_indexed()
        hits = index.search(query, limit=limit)
    except Exception as e:
        # The index self-heals, but never let a search crash the server.
        # Log without the query or transcript text (see log discipline).
        from ..log import get_logger

        get_logger(__name__).warning("search failed (%r); returning guidance", e)
        h._send_json(
            HTTPStatus.OK,
            {
                "hits": [],
                "count": 0,
                "guidance": (
                    "Search is temporarily unavailable. Browse the session list "
                    f"instead. (detail: {e})"
                ),
            },
        )
        return

    if not hits:
        h._send_json(
            HTTPStatus.OK,
            {
                "hits": [],
                "count": 0,
                "guidance": (
                    "No transcript lines matched. Try different keywords, or "
                    "browse the session list to read a whole transcript."
                ),
            },
        )
        return

    # Enrich each hit with started_at (SearchHit doesn't carry it), mirroring
    # the MCP server's hit shape for cross-surface consistency.
    started_by_id: dict[str, str | None] = {}
    rendered: list[dict] = []
    for hit in hits:
        if hit.session_id not in started_by_id:
            m = session.load_meta(hit.session_id)
            started_by_id[hit.session_id] = m.started_at if m else None
        rendered.append(
            {
                "session_id": hit.session_id,
                "started_at": started_by_id[hit.session_id],
                "timestamp_offset": hit.ts_offset,
                "offset_label": _offset_label(hit.ts_offset),
                "speaker": _speaker_label(hit.speaker),
                "line": hit.text,
                "context": hit.context,
            }
        )
    # Note: query text is deliberately NOT echoed back into a log call.
    from ..log import get_logger

    get_logger(__name__).info("search -> %d hit(s)", len(rendered))
    h._send_json(HTTPStatus.OK, {"hits": rendered, "count": len(rendered)})


def _offset_label(seconds: float | None) -> str | None:
    """Render a ts_offset (seconds) as an `[h:]mm:ss` label, or None.

    Duplicated from mcp_server.py rather than imported from it: importing
    mcp_server would pull the `mcp` SDK (an optional dep) into the web path,
    and these two tiny helpers aren't worth a shared module.
    """
    if seconds is None:
        return None
    total = max(0, int(round(seconds)))
    if total >= 3600:
        hgt, rem = divmod(total, 3600)
        mnt, sec = divmod(rem, 60)
        return f"[{hgt}:{mnt:02d}:{sec:02d}]"
    mnt, sec = divmod(total, 60)
    return f"[{mnt:02d}:{sec:02d}]"


def _speaker_label(speaker: str | None) -> str | None:
    """Normalize a parsed speaker ('Mic'/'System'/None) to a bracketed label."""
    if not speaker:
        return None
    return f"[{speaker}]"


# ---- POST /api/recording/start --------------------------------------------
# These mutating endpoints import jobs lazily so the read-only server never
# carries the worker pool at import time.


_VALID_CAPTURES = {"system", "mic", "mic+system"}


def post_start(h: WebHandler) -> None:
    """Start a recording via the detached spawn path (mirrors `rec start --detach`).

    Body ``{"capture": "mic+system" | "mic" | "system"}`` (default: config).
    Returns ``201 {session_id}``. ``409`` if already recording.
    """
    from . import jobs  # noqa: F401 (kept lazy; not used directly here)

    if recorder.active_pid() is not None:
        raise _ApiError(HTTPStatus.CONFLICT, "Already recording. Stop the current one first.")
    try:
        cfg = config.load_config()
    except Exception:
        raise _ApiError(
            HTTPStatus.INTERNAL_SERVER_ERROR,
            "No config found. Run `rec setup` in a terminal first.",
        ) from None
    body = h._read_json()
    capture = (body.get("capture") or cfg.capture).strip().lower()
    if capture not in _VALID_CAPTURES:
        raise _ApiError(
            HTTPStatus.BAD_REQUEST,
            f"capture must be one of: {', '.join(sorted(_VALID_CAPTURES))}.",
        )

    from datetime import datetime

    session_id = session.new_session_id()
    session.create_session_dir(session_id)
    session.update_meta(
        session_id,
        started_at=datetime.now().isoformat(timespec="seconds"),
        status=session.STATUS_RECORDING,
        capture=capture,
    )
    try:
        recorder.spawn_recorder(session_id, cfg.sample_rate, cfg.channels, capture)
    except Exception as e:
        raise _ApiError(
            HTTPStatus.INTERNAL_SERVER_ERROR, f"Failed to start recording: {e}."
        ) from e
    h._send_json(HTTPStatus.CREATED, {"session_id": session_id, "capture": capture})


# ---- POST /api/recording/stop ---------------------------------------------


def post_stop(h: WebHandler) -> None:
    """Stop the active recording, then queue transcription.

    Returns ``202 {session_id, job_id}``. The UI polls /api/jobs/{job_id}.
    ``409`` if not recording, or if a transcription job is already queued/running.
    """
    from . import jobs

    if recorder.active_pid() is None:
        raise _ApiError(HTTPStatus.CONFLICT, "Not recording.")
    session_id = _current_recording_session_id()
    if session_id is None:
        raise _ApiError(
            HTTPStatus.CONFLICT,
            "Recording is active but no recording session was found on disk.",
        )
    recorder.stop_recorder()
    try:
        job = jobs.registry.queue(session_id, kind=jobs.JOB_FINISH)
    except jobs.DuplicateJob:
        raise _ApiError(
            HTTPStatus.CONFLICT,
            "A transcription is already running for this session.",
        ) from None
    h._send_json(HTTPStatus.ACCEPTED, {"session_id": session_id, "job_id": job.id})


def _current_recording_session_id() -> str | None:
    """The id of the session in STATUS_RECORDING, else the newest overall."""
    metas = session.list_sessions()
    if not metas:
        return None
    for m in metas:
        if m.status == session.STATUS_RECORDING:
            return m.id
    return metas[0].id


# ---- GET /api/jobs/{job_id} ------------------------------------------------


def get_job(h: WebHandler, job_id: str) -> None:
    """Poll a transcription job's state. 404 if the id is unknown."""
    from . import jobs

    job = jobs.registry.get(job_id)
    if job is None:
        raise _ApiError(HTTPStatus.NOT_FOUND, f"No job {job_id}.")
    h._send_json(HTTPStatus.OK, job.to_dict())


# ---- POST /api/sessions/{id}/transcribe -----------------------------------


def post_transcribe(h: WebHandler, id: str) -> None:
    """Re-transcribe an existing session at a (maybe different) model.

    Body ``{"model": "base" | "small" | ...}`` (optional; defaults to config).
    Returns ``202 {job_id}``. ``409`` if a job is already queued/running for it.
    """
    from . import jobs

    meta = session.load_meta(id)
    if meta is None:
        raise _ApiError(HTTPStatus.NOT_FOUND, f"No session {id}.")
    body = h._read_json()
    model_override = body.get("model")
    if model_override is not None:
        if not isinstance(model_override, str) or not model_override.strip():
            raise _ApiError(HTTPStatus.BAD_REQUEST, "model must be a non-empty string.")
        model_override = model_override.strip()
    try:
        job = jobs.registry.queue(
            id, kind=jobs.JOB_TRANSCRIBE, model_override=model_override
        )
    except jobs.DuplicateJob:
        raise _ApiError(
            HTTPStatus.CONFLICT,
            "A transcription is already running for this session.",
        ) from None
    h._send_json(HTTPStatus.ACCEPTED, {"job_id": job.id, "session_id": id})
