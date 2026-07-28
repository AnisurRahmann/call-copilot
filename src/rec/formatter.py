"""Build the markdown transcript from transcribed segments.

Output shape (per spec):

    # Meeting Transcript

    **Date:** 2026-07-27
    **Duration:** 47 minutes
    **File:** recording.wav

    ---

    [00:00] Welcome everyone, let's get started with the standup.

    [00:12] So first up, the API migration — we're about 80% through.

    ...
"""

from __future__ import annotations

from datetime import date
from typing import Iterable

from . import session as session_mod
from .log import get_logger
from .transcriber import Segment

log = get_logger(__name__)


def build_markdown(
    segments: Iterable[Segment],
    *,
    date_str: str | None = None,
    duration_seconds: float | None = None,
    wav_filename: str = "recording.wav",
) -> str:
    """Render segments into the canonical transcript markdown."""
    when = date_str or date.today().isoformat()
    duration_human = session_mod.format_duration_human(duration_seconds)

    lines: list[str] = [
        "# Meeting Transcript",
        "",
        f"**Date:** {when}",
        f"**Duration:** {duration_human}",
        f"**File:** {wav_filename}",
        "",
        "---",
        "",
    ]

    for seg in segments:
        if not seg.text or not seg.text.strip():
            continue
        ts = session_mod.format_timestamp(seg.start)
        lines.append(f"{ts} {seg.text}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def write_transcript(session_id: str, markdown: str) -> str:
    """Persist markdown to {session_dir}/transcript.md. Returns the path."""
    path = session_mod.transcript_path(session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(markdown, encoding="utf-8")
    log.info("transcript written: %s (%d bytes)", path, len(markdown))
    return str(path)
