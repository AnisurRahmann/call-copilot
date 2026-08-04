"""Session directory + metadata management.

Each session lives in {sessions_root}/{id}/ containing:
  recording.wav   — the captured audio
  session.json    — metadata (status, timestamps, duration, word_count)
  transcript.md   — the markdown transcript (after `rec stop`)
"""

from __future__ import annotations

import json
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

# Valid lifecycle statuses.
STATUS_RECORDING = "recording"
STATUS_RECORDED = "recorded"
STATUS_TRANSCRIBED = "transcribed"
# A SILENT session captured only zero samples — nothing to transcribe.
# Whisper on pure silence hallucinates text (e.g. a repeated "You"), so we
# skip transcription entirely and mark the session SILENT instead. The WAV
# stays on disk for `rec diagnose` to inspect.
STATUS_SILENT = "silent"


def new_session_id(now: datetime | None = None) -> str:
    """`YYYY-MM-DD_HH-MM-SS` — filesystem-safe, sorts chronologically."""
    now = now or datetime.now()
    sid = now.strftime("%Y-%m-%d_%H-%M-%S")
    log.debug("generated session id: %s", sid)
    return sid


def session_dir(session_id: str) -> Path:
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


def list_sessions() -> list[SessionMeta]:
    """All sessions newest-first (by id, which sorts chronologically)."""
    root = config.sessions_root()
    if not root.exists():
        return []
    out: list[SessionMeta] = []
    for child in sorted(root.iterdir(), reverse=True):
        if not child.is_dir():
            continue
        meta = load_meta(child.name)
        if meta is not None:
            out.append(meta)
    log.debug("listed %d sessions from %s", len(out), root)
    return out


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
