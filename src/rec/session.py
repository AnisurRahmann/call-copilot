"""Session directory + metadata management.

Each session lives in {sessions_root}/{id}/ containing:
  recording.wav   — the captured audio
  session.json    — metadata (status, timestamps, duration, word_count)
  transcript.md   — the markdown transcript (after `rec stop`)
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from . import config
from .log import get_logger

log = get_logger(__name__)

RECORDING_FILENAME = "recording.wav"          # system audio
MIC_RECORDING_FILENAME = "recording-mic.wav"  # microphone
SESSION_META_FILENAME = "session.json"
TRANSCRIPT_FILENAME = "transcript.md"


class InvalidSessionId(ValueError):
    """Raised when a session id could escape the sessions root (path traversal).

    Session ids are always a single filesystem-safe segment
    (`YYYY-MM-DD_HH-MM-SS`). Anything containing a path separator or a `..`
    component is rejected before it reaches a `Path` join, so an untrusted id
    (e.g. one supplied to the MCP `get_session` tool by a prompt-injected
    model) cannot read files outside the sessions store.
    """


def validate_session_id(session_id: str) -> str:
    """Return `session_id` if it is safe to join under the sessions root.

    A safe id is a single path segment: it must be non-empty, contain no path
    separator (`/` or `os.sep`), and not be `.` or `..`. This blocks traversal
    (`../stolen`) and absolute paths (`/etc/...`) at the lowest level, before
    any `Path` join. Raises `InvalidSessionId` otherwise.

    Real session ids (`2026-07-28_12-25-20`) always pass.
    """
    if not session_id or session_id in (".", ".."):
        raise InvalidSessionId(f"invalid session id: {session_id!r}")
    # Reject any platform separator, plus the generic `/` and `\`, and any
    # path component that resolves up the tree. `os.altsep` is `\` on Windows.
    seps = {"/", "\\", os.sep} | ({os.altsep} if os.altsep else set())
    if any(ch in session_id for ch in seps):
        raise InvalidSessionId(f"session id must not contain a path separator: {session_id!r}")
    return session_id

# Valid lifecycle statuses.
STATUS_RECORDING = "recording"
STATUS_RECORDED = "recorded"
# Transcription is running in the background (web job pool, or inline via the
# CLI). Set before the model starts, cleared (to TRANSCRIBED/SILENT/RECORDED)
# when it finishes. Surfaces in `rec status` and the web UI so a second process
# doesn't think the machine is idle while Whisper is grinding.
STATUS_TRANSCRIBING = "transcribing"
STATUS_TRANSCRIBED = "transcribed"
# A SILENT session captured only zero samples — nothing to transcribe.
# Whisper on pure silence hallucinates text (e.g. a repeated "You"), so we
# skip transcription entirely and mark the session SILENT instead. The WAV
# stays on disk for `rec diagnose` to inspect.
STATUS_SILENT = "silent"

# The canonical session id shape: YYYY-MM-DD_HH-MM-SS. Single source of truth
# for "is this a real session directory" — used by list_sessions to skip stray
# folders, and by the web router to reject non-conformant ids (path-traversal
# defense) before they reach session_dir.
_SESSION_ID_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}$")


def is_valid_session_id(session_id: str) -> bool:
    """True if ``session_id`` matches the canonical ``YYYY-MM-DD_HH-MM-SS`` shape."""
    return bool(_SESSION_ID_PATTERN.match(session_id))


def new_session_id(now: datetime | None = None) -> str:
    """`YYYY-MM-DD_HH-MM-SS` — filesystem-safe, sorts chronologically."""
    now = now or datetime.now()
    sid = now.strftime("%Y-%m-%d_%H-%M-%S")
    log.debug("generated session id: %s", sid)
    return sid


def session_dir(session_id: str) -> Path:
    """Path to a session's directory. Validates the id to block traversal."""
    validate_session_id(session_id)
    return config.sessions_root() / session_id


def wav_path(session_id: str) -> Path:
    return session_dir(session_id) / RECORDING_FILENAME


def mic_wav_path(session_id: str) -> Path:
    """Path to the microphone recording (used when capturing mic+system)."""
    return session_dir(session_id) / MIC_RECORDING_FILENAME


def session_json_path(session_id: str) -> Path:
    return session_dir(session_id) / SESSION_META_FILENAME


def transcript_path(session_id: str) -> Path:
    return session_dir(session_id) / TRANSCRIPT_FILENAME


def audio_sources(session_id: str) -> tuple[bool, bool]:
    """Which audio sources a session captured, inferred from which WAVs exist.

    Returns ``(has_mic, has_system)``. Source is inferred from disk rather than
    stored in session.json — that's the existing decision (no explicit source
    field is written), so callers that need it derive it here. Centralised so
    the CLI and the web API don't each reimplement the ``.exists()`` check.
    """
    return mic_wav_path(session_id).exists(), wav_path(session_id).exists()


def create_session_dir(session_id: str) -> Path:
    d = session_dir(session_id)
    d.mkdir(parents=True, exist_ok=True)
    log.info("session dir ready: %s", d)
    return d


@dataclass
class SessionMeta:
    id: str
    started_at: str  # ISO 8601
    status: str = STATUS_RECORDING
    original_device: str = ""
    duration: float | None = None  # seconds
    word_count: int | None = None
    model: str | None = None
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


def load_meta(session_id: str) -> SessionMeta | None:
    path = session_json_path(session_id)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        log.warning("session.json corrupt for %s (%r) — ignoring", session_id, e)
        return None
    # Tolerate unknown keys by stashing them under 'extra'.
    known = {f for f in SessionMeta.__dataclass_fields__}
    clean = {k: v for k, v in data.items() if k in known}
    extra = {k: v for k, v in data.items() if k not in known}
    meta = SessionMeta(**clean)
    if extra:
        meta.extra = extra
    return meta


def save_meta(meta: SessionMeta) -> Path:
    path = session_json_path(meta.id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(meta.to_dict(), indent=2) + "\n", encoding="utf-8")
    log.debug("session.json saved: %s status=%s", meta.id, meta.status)
    return path


def update_meta(session_id: str, **changes) -> SessionMeta:
    """Load existing meta (or build a fresh one), apply changes, persist."""
    meta = load_meta(session_id)
    if meta is None:
        meta = SessionMeta(id=session_id, started_at=datetime.now().isoformat(timespec="seconds"))
    for k, v in changes.items():
        if hasattr(meta, k):
            setattr(meta, k, v)
    save_meta(meta)
    log.info("session meta updated: %s %s", session_id,
             ", ".join(f"{k}={v}" for k, v in changes.items()))
    return meta


# The diagnostic shown to the user when a session captured only zero samples.
# One string, defined once here, served to both the CLI (`rec stop`, `rec list`)
# and the web UI — so the wording can't drift between surfaces.
SILENT_DIAGNOSTIC_HINT = (
    "No audio was captured. This usually means the Screen Recording permission "
    "wasn't granted, or nothing was playing. The WAV is kept for `rec diagnose`."
)

# A suspect session: enough duration that real speech should have produced many
# more words. <20 wpm over >30s is well below any real conversation (130–150
# wpm) and above Whisper-on-silence hallucination density (single digits over
# minutes). The duration floor stops short test recordings from tripping it.
SUSPECT_WPM = 20.0
SUSPECT_MIN_DURATION_S = 30.0


def capture_health(meta: SessionMeta) -> str:
    """How usable a session's capture looks: ``ok``/``suspect``/``silent``/``unknown``.

    Pure function over ``SessionMeta`` (not a session id) so it's testable
    without touching disk. The rule lives here — the single source — so the
    CLI (`rec list`) and the web API flag the same sessions.

    - ``silent``  — the recorder already marked it silent (``STATUS_SILENT``).
    - ``unknown`` — duration or word_count isn't set yet (still recording, or
      pre-transcription); must NOT be flagged as a failure.
    - ``suspect`` — duration > 30s and words-per-minute < ``SUSPECT_WPM``: a
      likely failed capture (silent tap, revoked permission, no audio playing).
    - ``ok``      — otherwise.
    """
    if meta.status == STATUS_SILENT:
        return "silent"
    if meta.duration is None or meta.word_count is None:
        return "unknown"
    if meta.duration > SUSPECT_MIN_DURATION_S:
        wpm = meta.word_count / (meta.duration / 60.0)
        if wpm < SUSPECT_WPM:
            return "suspect"
    return "ok"


def list_sessions() -> list[SessionMeta]:
    """All sessions newest-first (by id, which sorts chronologically).

    Only directories whose name is a canonical session id
    (``YYYY-MM-DD_HH-MM-SS``) are returned. Stray directories under the
    sessions root (test artifacts, manually-created folders) are ignored so
    they don't surface as unopenable rows in the list/web UI or push real
    sessions below the fold in the chronological sort.
    """
    root = config.sessions_root()
    if not root.exists():
        return []
    out: list[SessionMeta] = []
    for child in sorted(root.iterdir(), reverse=True):
        if not child.is_dir():
            continue
        if not _SESSION_ID_PATTERN.match(child.name):
            continue
        meta = load_meta(child.name)
        if meta is not None:
            out.append(meta)
    log.debug("listed %d sessions from %s", len(out), root)
    return out


class AmbiguousSessionId(ValueError):
    """Raised by resolve_session_id when a prefix matches more than one session.

    Carries the candidate ids so a caller (the MCP `get_session` tool, surfaced
    to the model as a tool error) can show them and let the user/agent pick,
    rather than silently guessing the newest one.
    """

    def __init__(self, query: str, matches: list[str]):
        self.query = query
        self.matches = matches
        super().__init__(
            f"Session prefix {query!r} matches {len(matches)} sessions: "
            f"{', '.join(matches)}. Use a longer prefix or the full id."
        )


def resolve_session_id(query: str) -> str | None:
    """Resolve a (possibly partial) session id to a real one.

    Accepts the full id ('2026-07-27_14-30-00') or any UNIQUE prefix
    ('2026-07-27' when only one session starts that way). Returns the resolved
    id, or None if nothing matches. Raises `AmbiguousSessionId` (a ValueError
    subclass) if the prefix matches more than one session — callers should let
    that propagate so the user/agent sees the candidates instead of a silent
    newest-first guess.

    Shared by `rec diagnose` and the MCP `get_session` tool so neither reaches
    into the other.
    """
    # Reject traversal up front — an id like '../stolen' must never reach a
    # Path join (see validate_session_id). Treat it as "no match" so the caller
    # surfaces a clean "no session" message rather than a traceback.
    try:
        valid = validate_session_id(query)
    except InvalidSessionId:
        return None
    # Exact match first.
    if session_dir(valid).exists():
        return valid
    # Prefix match against all session dirs (newest-first).
    matches = [m.id for m in list_sessions() if m.id.startswith(query)]
    if not matches:
        return None
    if len(matches) > 1:
        raise AmbiguousSessionId(query, matches)
    return matches[0]


# ---- formatters -----------------------------------------------------------


def format_duration_human(seconds: float | None) -> str:
    """47 minutes / 1 hour 3 minutes / 0 minutes / -- (None)."""
    if seconds is None:
        return "--"
    total = int(round(seconds))
    if total < 60:
        return f"{total} sec"
    mins, _ = divmod(total, 60)
    if mins < 60:
        return f"{mins} min"
    hours, mins = divmod(mins, 60)
    if mins == 0:
        return f"{hours} hr"
    return f"{hours} hr {mins} min"


def format_timestamp(seconds: float) -> str:
    """`[MM:SS]` under an hour, `[H:MM:SS]` at/over an hour."""
    total = max(0, int(seconds))
    if total >= 3600:
        h, rem = divmod(total, 3600)
        m, s = divmod(rem, 60)
        return f"[{h}:{m:02d}:{s:02d}]"
    m, s = divmod(total, 60)
    return f"[{m:02d}:{s:02d}]"


def started_at_display(session_id: str) -> str:
    """Pretty-print the session id as a human date (`2026-07-27 14:30`)."""
    try:
        dt = datetime.strptime(session_id, "%Y-%m-%d_%H-%M-%S")
    except ValueError:
        return session_id
    return dt.strftime("%Y-%m-%d %H:%M")
