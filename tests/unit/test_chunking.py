"""Tests for rec.chunking — token-aware transcript splitting.

Both transcript line formats (merged [System] [00:12] text and single-source
System [00:00] text) must produce correctly-attributed chunks; the ceiling must
never be exceeded (except for a single oversize segment); overlap must carry
the last segments of chunk N into chunk N+1; and concatenating chunks minus
overlap must reproduce every original line.
"""

from __future__ import annotations

from rec import chunking


def _merged_line(i: int, speaker: str, text: str) -> str:
    """A merged-format line: [System] [00:12] text."""
    mm, ss = divmod(i * 7, 60)
    return f"[{speaker}] [{mm:02d}:{ss:02d}] {text}"


def _single_line(i: int, speaker: str, text: str) -> str:
    """A single-source line: System [00:00] text (label before ts, no brackets)."""
    mm, ss = divmod(i * 7, 60)
    return f"{speaker} [{mm:02d}:{ss:02d}] {text}"


def _make_transcript(lines, *, header="merged") -> str:
    """Wrap speech lines in a transcript.md header + body."""
    if header == "merged":
        head = (
            "# Meeting Transcript\n\n"
            "**Date:** 2026-08-10\n**Duration:** 47 min\n"
            "**Sources:** System (recording.wav) + Microphone (recording-mic.wav)\n\n---\n\n"
        )
    else:
        head = (
            "# Meeting Transcript\n\n"
            "**Date:** 2026-08-10\n**Duration:** 47 min\n"
            "**File:** recording.wav\n\n---\n\n"
        )
    body = "\n\n".join(lines)
    return head + body + "\n"


def test_empty_transcript_yields_no_chunks():
    assert chunking.chunk_transcript("# x\n\n---\n\n") == []
    assert chunking.chunk_transcript("") == []


def test_short_transcript_is_single_chunk():
    lines = [_merged_line(i, "System", f"Line number {i}.") for i in range(3)]
    text = _make_transcript(lines)
    chunks = chunking.chunk_transcript(text)
    assert len(chunks) == 1
    assert chunks[0].index == 0
    # All three lines present.
    for ln in lines:
        assert ln in chunks[0].text


def test_both_line_formats_parse():
    """Merged [System] [00:12] and single System [00:00] must both survive."""
    merged = [_merged_line(i, "Mic", f"merged {i}") for i in range(5)]
    single = [_single_line(i, "System", f"single {i}") for i in range(5)]
    # Two separate transcripts: each format on its own.
    for label, lines in (("merged", merged), ("single", single)):
        text = _make_transcript(lines, header=("merged" if label == "merged" else "single"))
        chunks = chunking.chunk_transcript(text)
        body = "\n\n".join(c.text for c in chunks)
        for ln in lines:
            assert ln in body, f"{label} line {ln!r} lost during chunking"


def test_ceiling_never_exceeded():
    """No chunk's estimate exceeds the ceiling by more than the overlap allowance.

    A chunk holds at least one new segment; carry (overlap from the previous
    chunk) is intentional repetition, not new content, so a chunk may exceed
    the ceiling by up to ``overlap_lines`` segments' worth. With realistic
    proportions (segments much smaller than the ceiling) this never triggers.
    """
    # Lines ~80 tokens each; ceiling 800 fits ~10 new + 3 carry comfortably.
    lines = [_merged_line(i, "System", "word " * 60) for i in range(60)]
    text = _make_transcript(lines)
    chunks = chunking.chunk_transcript(text, target_tokens=500, ceiling_tokens=800)
    assert len(chunks) > 1
    for c in chunks:
        # A single oversize segment is allowed its own chunk (can't split mid-line).
        if c.segment_count == 1:
            continue
        # Otherwise the chunk must respect the ceiling.
        assert c.est_tokens <= 800, (
            f"chunk {c.index} est={c.est_tokens} segs={c.segment_count} exceeds ceiling"
        )


def test_overlap_carries_last_lines():
    """The last 3 segments of chunk N appear in chunk N+1."""
    lines = [_merged_line(i, "System", f"line {i} " + "x" * 300) for i in range(40)]
    text = _make_transcript(lines)
    chunks = chunking.chunk_transcript(text, target_tokens=400, ceiling_tokens=600, overlap_lines=3)
    assert len(chunks) >= 2
    # The last segments of chunk 0 should appear at the start of chunk 1.
    first_body_segs = chunking._split_segments(chunks[0].text)
    second_body_segs = chunking._split_segments(chunks[1].text)
    carried = first_body_segs[-3:]
    for seg in carried:
        assert seg in second_body_segs, f"overlap segment {seg!r} not carried into chunk 1"


def test_reconstruction_minus_overlap_reproduces_every_line():
    """Concatenating chunks minus overlap reproduces every original line."""
    lines = [_merged_line(i, "System", f"decision {i} unique marker {i}") for i in range(50)]
    text = _make_transcript(lines)
    chunks = chunking.chunk_transcript(text, target_tokens=400, ceiling_tokens=600, overlap_lines=3)
    # Every original line must appear in at least one chunk.
    flat = "\n\n".join(c.text for c in chunks)
    for ln in lines:
        assert ln in flat, f"original line {ln!r} lost"


def test_survives_three_hour_transcript():
    """A synthetically large transcript must chunk without error and stay bounded."""
    # ~18000 lines (roughly a 3h meeting at one segment every 0.6s).
    lines = [_merged_line(i, "System", f"speech segment number {i} " + "y" * 40) for i in range(18000)]
    text = _make_transcript(lines)
    chunks = chunking.chunk_transcript(text)
    assert len(chunks) > 1
    # No chunk wildly oversized.
    for c in chunks:
        assert c.est_tokens <= chunking.CEILING_TOKENS or c.segment_count == 1


def test_header_is_stripped():
    """The markdown header must not appear in any chunk."""
    lines = [_merged_line(i, "System", f"body {i}") for i in range(3)]
    text = _make_transcript(lines)
    chunks = chunking.chunk_transcript(text)
    flat = "\n\n".join(c.text for c in chunks)
    assert "Meeting Transcript" not in flat
    assert "**Date:**" not in flat
