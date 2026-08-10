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
from ..log import get_logger
from .ranges import parse_range
from .server import _ApiError

if TYPE_CHECKING:
    from .server import WebHandler

log = get_logger(__name__)

# Chunk size for streaming audio from disk. 64 KiB is large enough to amortise
# the per-read overhead and small enough that memory stays flat regardless of
# file size or number of concurrent requests.
_STREAM_CHUNK = 64 * 1024


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
            ("GET", "/api/config"): (get_config, []),
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
    has_mic, has_system = session.audio_sources(sid)
    return {
        "id": sid,
        "started_at": meta.started_at,
        "status": meta.status,
        "duration": meta.duration,
        "duration_human": session.format_duration_human(meta.duration),
        "word_count": meta.word_count,
        "model": meta.model,
        "has_mic": has_mic,
        "has_system": has_system,
        "capture_health": session.capture_health(meta),
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
        # One listing covers both checks (the loop falls back to metas[0]).
        metas = session.list_sessions()
        for meta in metas:
            if meta.status == session.STATUS_RECORDING:
                sid = meta.id
                break
        if sid is None and metas:
            sid = metas[0].id
        payload["session_id"] = sid
        meta = session.load_meta(sid) if sid else None
        payload["started_at"] = meta.started_at if meta else None
        payload["elapsed_s"] = _elapsed_seconds(meta) if meta else None
        payload["bytes"] = _session_bytes(sid) if sid else 0
        payload["capture"] = (meta.extra.get("capture") if meta else None) or _default_capture()
    else:
        transcribing = _transcribing_session_id()
        payload["session_id"] = transcribing
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
            log.warning("could not read transcript %s (%r)", tpath, e)
            raise _ApiError(
                HTTPStatus.INTERNAL_SERVER_ERROR, "Could not read the transcript."
            ) from e
    h._send_json(
        HTTPStatus.OK,
        {
            "id": meta.id,
            "started_at": meta.started_at,
            "status": meta.status,
            "capture_health": session.capture_health(meta),
            "duration": meta.duration,
            "duration_human": session.format_duration_human(meta.duration),
            "word_count": meta.word_count,
            "model": meta.model,
            "transcript": transcript,
            "audio_streams": _audio_streams(id),
            # The silent-session hint, served from the one core constant so it
            # can't drift between the CLI and the UI. The frontend renders this
            # for a silent/low-wpm session instead of hardcoding the text.
            "silent_hint": session.SILENT_DIAGNOSTIC_HINT,
        },
    )


# ---- GET /api/sessions/{id}/audio/{stream} --------------------------------


_AUDIO_KINDS = {"system", "mic"}


def _stream_file_range(wfile, path, start: int, length: int) -> None:
    """Stream ``length`` bytes from ``path`` starting at ``start`` in chunks.

    Reads sequentially from disk so the working set stays bounded regardless of
    file size — never buffers the whole file. A read short of the declared
    ``length`` (file shrank mid-stream) is tolerated; the client simply gets a
    truncated body, which an audio player handles gracefully.
    """
    remaining = length
    with path.open("rb") as fh:
        if start:
            fh.seek(start)
        while remaining > 0:
            chunk = fh.read(min(_STREAM_CHUNK, remaining))
            if not chunk:
                break  # file shrank beneath us; serve what we have
            wfile.write(chunk)
            remaining -= len(chunk)


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
        total = path.stat().st_size
    except OSError as e:
        log.warning("could not stat %s (%r)", path, e)
        raise _ApiError(
            HTTPStatus.INTERNAL_SERVER_ERROR, "Could not read the audio file."
        ) from e

    spec = parse_range(h.headers.get("Range"), total)

    if spec.kind == "invalid":
        # Unsatisfiable. Per RFC 7233, include Content-Range: bytes */T.
        h.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
        h.send_header("Content-Range", f"bytes */{total}")
        h.send_header("Content-Length", "0")
        h.send_header("X-Content-Type-Options", "nosniff")
        h.end_headers()
        return

    # Stream directly from disk instead of buffering the whole WAV: a long
    # meeting can be hundreds of MB, and Safari's <audio> issues many Range
    # requests during playback/scrubbing — read_bytes() on each would let one
    # client pin gigabytes of memory.
    if spec.kind in ("none", "multi"):
        # Full body (multi-range falls back to the whole file rather than
        # attempting multipart/byteranges). Advertise range support so the
        # client knows it can seek.
        start, length = 0, total
        status = HTTPStatus.OK
    else:  # single satisfiable range → 206 Partial Content
        start, length = spec.start, spec.end - spec.start + 1
        status = HTTPStatus.PARTIAL_CONTENT

    h.send_response(status)
    h.send_header("Content-Type", "audio/wav")
    h.send_header("Content-Length", str(length))
    h.send_header("Accept-Ranges", "bytes")
    if status == HTTPStatus.PARTIAL_CONTENT:
        h.send_header("Content-Range", f"bytes {spec.start}-{spec.end}/{total}")
    h.send_header("X-Content-Type-Options", "nosniff")
    h.end_headers()
    if h.command == "HEAD":
        return
    _stream_file_range(h.wfile, path, start, length)


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
                    "instead."
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


# ---- GET /api/config -------------------------------------------------------
# Read-only environment + config summary for the Settings view. Never 500s:
# a missing config returns a clear error state (config_present: false) so the
# UI can tell the user to run `rec setup`.


def _collapse_home(path: str | None) -> str | None:
    """Replace a leading $HOME with `~` for display; None passes through."""
    if not path:
        return None
    from pathlib import Path

    home = str(Path.home())
    if path.startswith(home):
        return "~" + path[len(home):]
    return path


def get_config(h: WebHandler) -> None:
    """Loaded config + resolved paths + environment probes, read-only.

    Returns ``{version, config_present, config, config_path, sessions_root,
    index_db, macos_version, macos_supported, audiotap_usable}``. When no
    config.json exists the response is still 200 with ``config_present: false``
    and default-valued config + a setup hint — never a traceback.
    """
    import platform

    from .. import __version__, envcheck, index

    # Config: fall back to defaults + a flag when the user hasn't run setup.
    config_present = config.config_path().exists()
    setup_hint = None
    try:
        cfg = config.load_config()
    except Exception:
        cfg = config.default_config()
        setup_hint = "No config found. Run `rec setup` in a terminal first."

    payload: dict = {
        "version": __version__,
        "config_present": config_present,
        "config": cfg.model_dump_jsonable(),
        "config_path": _collapse_home(str(config.config_path())),
        "sessions_root": _collapse_home(str(config.sessions_root())),
        "index_db": _collapse_home(str(index.index_path())),
        "setup_hint": setup_hint,
        # The valid capture-mode set comes from the recorder (single source);
        # the frontend select renders this list rather than hardcoding it.
        "capture_modes": list(recorder.CAPTURE_MODES),
    }

    # Environment probes. Reuse envcheck's non-raising helpers; the names are
    # underscore-private but the prompt says reuse rather than reimplement. A
    # probe failure (unexpected) shows "unknown" rather than 500ing the page.
    try:
        payload["macos_version"] = platform.mac_ver()[0] or "unknown"
        payload["macos_supported"] = envcheck._macos_version() >= envcheck.MIN_MACOS
    except Exception:
        payload["macos_version"] = "unknown"
        payload["macos_supported"] = None
    try:
        payload["audiotap_usable"] = envcheck._audiotap_usable()
    except Exception:
        payload["audiotap_usable"] = None
    # Permission probes (public, non-raising). Mic is a real audiotap API;
    # screen capture is the best-effort ctypes preflight.
    payload["mic_permission"] = envcheck.mic_permission()
    payload["screen_capture_status"] = envcheck.screen_capture_status()

    # Agent connection: is the MCP server wired into Claude Code? Delegates to
    # the core helper (reads ~/.claude.json), so the Overview and `rec mcp
    # status` agree.
    try:
        from .. import cli as cli_mod
        mcp = cli_mod.mcp_status()
        payload["mcp_wired"] = mcp["wired"]
        payload["mcp_note"] = mcp["note"]
    except Exception:
        payload["mcp_wired"] = None
        payload["mcp_note"] = None

    # Index health: lines/sessions/orphans. Computed in index.py (the rule's
    # single source); the Overview renders what it receives.
    try:
        st = index.stats()
        payload["index_stats"] = {
            "lines": st.lines, "sessions": st.sessions,
            "orphans": st.orphans,
            "last_indexed_at": st.last_indexed_at,
        }
    except Exception:
        payload["index_stats"] = None

    # Session-health summary: counts by capture_health across all sessions.
    # The threshold lives in session.capture_health (core); this just tallies.
    try:
        from collections import Counter
        counts = Counter(session.capture_health(m) for m in session.list_sessions())
        payload["session_health"] = {k: counts.get(k, 0) for k in
                                     ("ok", "suspect", "silent", "unknown")}
    except Exception:
        payload["session_health"] = None

    h._send_json(HTTPStatus.OK, payload)


# ---- POST /api/recording/start --------------------------------------------
# These mutating endpoints import jobs lazily so the read-only server never
# carries the worker pool at import time.


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
    # The valid set comes from recorder.CAPTURE_MODES (single source of truth),
    # not a hardcoded copy in the API layer.
    if capture not in recorder.CAPTURE_MODES:
        raise _ApiError(
            HTTPStatus.BAD_REQUEST,
            f"capture must be one of: {', '.join(recorder.CAPTURE_MODES)}.",
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
        log.warning("spawn_recorder failed (%r)", e)
        raise _ApiError(
            HTTPStatus.INTERNAL_SERVER_ERROR, "Failed to start recording."
        ) from e
    h._send_json(HTTPStatus.CREATED, {"session_id": session_id, "capture": capture})


# ---- POST /api/recording/stop ---------------------------------------------


def post_stop(h: WebHandler) -> None:
    """Stop the active recording, then queue transcription.

    Returns ``202 {session_id, job_id}``. The UI polls /api/jobs/{job_id}.
    ``409`` if not recording, or if a transcription job is already queued/running.
    """
    from . import jobs

    # Always drain the body: with HTTP/1.1 keep-alive, leaving unread bytes in
    # rfile corrupts the next request on the connection. Stop takes no params,
    # but a client (or a CSRF probe) may still send a Content-Length.
    h._read_json()

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
        if not isinstance(model_override, str):
            raise _ApiError(HTTPStatus.BAD_REQUEST, "model must be a string.")
        model_override = model_override.strip()
        # Whitelist: an unknown name would make faster-whisper treat it as a
        # local path or trigger a large HuggingFace download. The SPA offers the
        # same set; enforce it server-side so a stray caller can't.
        allowed = {"tiny", "base", "small", "medium", "large-v3"}
        if model_override not in allowed:
            raise _ApiError(
                HTTPStatus.BAD_REQUEST,
                f"model must be one of: {', '.join(sorted(allowed))}.",
            )
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
