"""Token-aware transcript chunking for summarisation.

Splits a ``transcript.md`` body into size-bounded chunks for the Tier 1 map
pass. The chunker preserves the **verbatim** transcript lines (so a summary can
cite exact ``[MM:SS]`` timestamps and so test #6 can reconstruct every original
line), and splits **only on transcript line boundaries** — never mid-line.

Token estimate is ``len(text) / 4`` (no tokenizer dependency) and is for *sizing
only*; real cost always comes from the provider's reported usage.

Both transcript line formats are handled by reusing the header-stripping logic
from :mod:`rec.index` (the parser that already encodes the format contract):
  - merged:      ``[System] [00:12] some text``
  - single:      ``System [00:00] some text``

Overlap: the last ``overlap_lines`` transcript segments of chunk N are carried
into chunk N+1 so a decision spanning a boundary isn't lost.
"""

from __future__ import annotations

from dataclasses import dataclass

from .index import HEADER_SEP_RE
from .providers import pricing

TARGET_TOKENS = 6_000
CEILING_TOKENS = 8_000
DEFAULT_OVERLAP_LINES = 3


@dataclass(frozen=True)
class Chunk:
    """One size-bounded slice of a transcript.

    ``text`` is the verbatim transcript text (header stripped), joined by
    blank lines between segments exactly as in the source. ``segment_count`` is
    the number of transcript segments (timestamped lines) in this chunk,
    including any carried-over overlap.
    """

    index: int
    text: str
    segment_count: int
    est_tokens: int


def strip_header(text: str) -> str:
    """Drop the markdown header (everything up to/including the first ``---``)."""
    splits = HEADER_SEP_RE.split(text, maxsplit=1)
    # splits == [header, body] when a separator is present; else [whole text].
    body = splits[1] if len(splits) == 2 else text
    return body.strip()


def _split_segments(body: str) -> list[str]:
    """Split a header-stripped body into verbatim transcript segments.

    Transcript segments are blank-line-delimited (formatter.py emits one blank
    line between every timestamped line). Each returned segment is a single
    timestamped line, verbatim (not normalized) so the original format and
    timestamp text are preserved exactly.
    """
    # Normalize: split on any run of blank lines, keep the non-empty pieces.
    out: list[str] = []
    for raw in body.split("\n\n"):
        seg = raw.strip()
        if seg:
            out.append(seg)
    return out


def chunk_transcript(
    transcript: str,
    *,
    target_tokens: int = TARGET_TOKENS,
    ceiling_tokens: int = CEILING_TOKENS,
    overlap_lines: int = DEFAULT_OVERLAP_LINES,
) -> list[Chunk]:
    """Split a full transcript.md body into size-bounded chunks.

    Never splits mid-segment. Carries the last ``overlap_lines`` segments of
    chunk N into chunk N+1. The last chunk may be small; a transcript shorter
    than the target yields a single chunk. An empty transcript yields no chunks.

    ``target_tokens`` is the soft target; ``ceiling_tokens`` is the hard ceiling
    a chunk's estimate never exceeds (the only exception: a single segment whose
    own estimate exceeds the ceiling — it becomes its own chunk, since splitting
    it would mean splitting mid-line).
    """
    body = strip_header(transcript)
    segments = _split_segments(body)
    if not segments:
        return []

    chunks: list[Chunk] = []
    current: list[str] = []
    current_tokens = 0
    # The overlap carried into the NEXT chunk once the current one closes.
    carry: list[str] = []

    def _est(text: str) -> int:
        return pricing.estimate_tokens(text)

    def _close_chunk() -> None:
        """Emit the current chunk and set up carry for the next one."""
        nonlocal current, current_tokens
        if not current:
            return
        text = "\n\n".join(current)
        chunks.append(Chunk(
            index=len(chunks),
            text=text,
            segment_count=len(current),
            est_tokens=_est(text),
        ))
        # Carry the last `overlap_lines` segments into the next chunk.
        carry.clear()
        carry.extend(current[-overlap_lines:] if overlap_lines > 0 else [])
        current = []
        current_tokens = 0

    def _start_new_chunk() -> None:
        """Seed a fresh chunk with the carried overlap."""
        nonlocal current_tokens
        current.clear()
        if carry:
            current.extend(carry)
            current_tokens = sum(_est(c) for c in carry)

    for seg in segments:
        seg_tokens = _est(seg)
        # If adding this segment would exceed the ceiling and we already have a
        # non-empty chunk, close it and start a new one (seeded with overlap).
        # A chunk must hold at least one segment — even one over the ceiling —
        # because splitting mid-segment is forbidden.
        if current and (current_tokens + seg_tokens) > ceiling_tokens:
            _close_chunk()
            _start_new_chunk()

        current.append(seg)
        current_tokens += seg_tokens

        # Soft target reached → flush for even sizes. Seed the next chunk with
        # overlap so a decision spanning a boundary isn't lost.
        if current_tokens >= target_tokens:
            _close_chunk()
            _start_new_chunk()

    _close_chunk()
    return chunks


def total_estimate(chunks: list[Chunk]) -> int:
    """Sum of per-chunk token estimates (overlap counted per-chunk, as sent)."""
    return sum(c.est_tokens for c in chunks)
