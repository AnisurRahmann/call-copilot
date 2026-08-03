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

from collections.abc import Iterable
from datetime import date

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
    source: str | None = None,
) -> str:
    """Render segments into the canonical transcript markdown.

    `source` (e.g. "Mic"/"System"), when set, prefixes each timestamp line so a
    single-source transcript is self-describing. Omit it for the legacy look.
    """
    when = date_str or date.today().isoformat()
    duration_human = session_mod.format_duration_human(duration_seconds)
    label = f"{source} " if source else ""

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
        lines.append(f"{label}{ts} {seg.text}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def build_merged_markdown(
    *,
    system_result,
    mic_result,
    date_str: str | None = None,
    wav_filenames: tuple[str, str] = ("recording.wav", "recording-mic.wav"),
) -> str:
    """Merge a system-audio transcript and a mic transcript into one markdown.

    Segments from both sources are interleaved by start timestamp and labeled
    [System]/[Mic] so the reader can tell who-said-what. The two sources are
    independent WAVs (no real-time mixing), so overlapping speech appears as
    adjacent labeled lines rather than a summed waveform.
    """
    when = date_str or date.today().isoformat()
    duration = max(system_result.duration, mic_result.duration)
    duration_human = session_mod.format_duration_human(duration)

    # Tag each segment with its source, then sort by start time.
    tagged: list[tuple[float, str, str]] = []
    for seg in system_result.segments:
        if seg.text and seg.text.strip():
            tagged.append((seg.start, "System", seg.text))
    for seg in mic_result.segments:
        if seg.text and seg.text.strip():
            tagged.append((seg.start, "Mic", seg.text))
    tagged.sort(key=lambda x: x[0])

    lines: list[str] = [
        "# Meeting Transcript",
        "",
        f"**Date:** {when}",
        f"**Duration:** {duration_human}",
        f"**Sources:** System ({wav_filenames[0]}) + Microphone ({wav_filenames[1]})",
        "",
        "---",
        "",
    ]
    for start, source, text in tagged:
        ts = session_mod.format_timestamp(start)
        lines.append(f"[{source}] {ts} {text}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def write_transcript(session_id: str, markdown: str) -> str:
    """Persist markdown to {session_dir}/transcript.md. Returns the path."""
    path = session_mod.transcript_path(session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(markdown, encoding="utf-8")
    log.info("transcript written: %s (%d bytes)", path, len(markdown))
    return str(path)
