"""Tests for rec.index — the FTS5 transcript search index.

These run offline: no audio device, no model download. Each test gets a fresh
tmp XDG root (via the autouse _xdg_tmp fixture in conftest), so index.db lands
in the tmp dir and nothing touches the real ~/.local/share/rec.
"""

from __future__ import annotations

import sqlite3
import time

import pytest

from rec import index, session

# ---- fixture builders ------------------------------------------------------


MERGED_TRANSCRIPT = """# Meeting Transcript

**Date:** 2026-07-28
**Duration:** 2 min
**Sources:** System (recording.wav) + Microphone (recording-mic.wav)

---

[System] [00:00] Welcome to the standup everyone.

[Mic] [00:12] The client asked about the volume discount on pricing.

[System] [00:45] They want a quote by Friday.

[Mic] [01:30] Let's confirm the discount tiers before then.
"""

SINGLE_TRANSCRIPT = """# Meeting Transcript

**Date:** 2026-07-29
**Duration:** 1 min
**File:** recording.wav

---

System [00:00] Quick sync on the API migration.

System [00:20] We are eighty percent through the cutover.
"""


def _make_session(sid: str, transcript: str, *, has_sys=True, has_mic=False, started=None):
    """Write a session dir with metadata + transcript + marker WAVs (no real audio)."""
    session.create_session_dir(sid)
    session.update_meta(
        sid,
        started_at=started or f"{sid[:10]}T{sid[11:].replace('-', ':')}:00",
        status=session.STATUS_TRANSCRIBED,
        duration=120.0,
        word_count=10,
    )
    session.transcript_path(sid).write_text(transcript, encoding="utf-8")
    if has_sys:
        session.wav_path(sid).write_bytes(b"system-audio")
    if has_mic:
        session.mic_wav_path(sid).write_bytes(b"mic-audio")


@pytest.fixture
def two_sessions(xdg):
    """Two sessions: one mic+system (merged), one system-only (single-source)."""
    _make_session(
        "2026-07-28_12-25-20", MERGED_TRANSCRIPT,
        has_sys=True, has_mic=True, started="2026-07-28T12:25:20",
    )
    _make_session(
        "2026-07-29_09-00-00", SINGLE_TRANSCRIPT,
        has_sys=True, has_mic=False, started="2026-07-29T09:00:00",
    )
    return xdg


# ---- transcript parser -----------------------------------------------------


def test_parse_merged_format_labels_speakers_and_offsets():
    lines = index.parse_transcript(MERGED_TRANSCRIPT)
    # 4 speech lines; header + blanks skipped.
    assert len(lines) == 4
    assert lines[0].speaker == "System"
    assert lines[0].ts_offset == 0.0
    assert lines[1].speaker == "Mic"
    assert lines[1].ts_offset == 12.0  # [00:12]
    assert lines[2].speaker == "System"
    assert lines[2].ts_offset == 45.0
    assert lines[3].ts_offset == 90.0  # [01:30]


def test_parse_single_source_format_has_system_label():
    lines = index.parse_transcript(SINGLE_TRANSCRIPT)
    assert len(lines) == 2
    assert all(pl.speaker == "System" for pl in lines)
    assert lines[0].ts_offset == 0.0
    assert lines[1].ts_offset == 20.0


def test_parse_handles_hour_timestamps():
    md = (
        "# Meeting Transcript\n\n**Date:** x\n\n---\n\n"
        "System [1:02:03] A line past the hour mark.\n"
    )
    lines = index.parse_transcript(md)
    assert len(lines) == 1
    assert lines[0].ts_offset == 3723.0  # 1*3600 + 2*60 + 3


def test_parse_skips_non_timestamped_lines():
    md = "# Meeting Transcript\n\n---\n\nSystem [00:00] Real line.\n\nA stray note with no timestamp.\n"
    lines = index.parse_transcript(md)
    assert len(lines) == 1
    assert lines[0].text == "Real line."


def test_parse_handles_transcript_without_header_separator():
    # A transcript missing the `---` separator should still parse its lines.
    md = "System [00:05] No header here.\n"
    lines = index.parse_transcript(md)
    assert len(lines) == 1
    assert lines[0].ts_offset == 5.0


# ---- sanitize_match --------------------------------------------------------


def test_sanitize_lowercases_and_quotes_and_or_joins():
    assert index.sanitize_match("Pricing DISCOUNT") == '"pricing" OR "discount"'


def test_sanitize_strips_punctuation():
    # FTS5 operators like : * " ( ) must not reach MATCH.
    out = index.sanitize_match("client's quote: by friday?")
    # Punctuation is replaced with spaces: "client's" -> "client" + "s" (the "s"
    # fragment is too short and dropped). Stopwords (the/by) are removed.
    assert "client" in out
    assert "quote" in out
    assert "friday" in out
    assert "the" not in out  # stopword
    assert "by" not in out   # stopword
    # No raw FTS5 operators survive to MATCH.
    for ch in (":", "(", ")", "*", '"'):
        # double-quotes ARE expected (they wrap each term); the operators are not.
        pass
    for ch in (":", "(", ")", "*"):
        assert ch not in out


def test_sanitize_drops_stopwords():
    assert index.sanitize_match("the and what") == ""
    assert index.sanitize_match("of to in on") == ""


def test_sanitize_empty_for_pure_punctuation():
    assert index.sanitize_match("??? !!! ...") == ""


def test_sanitize_dedupes_tokens():
    out = index.sanitize_match("pricing pricing client client")
    assert out.count("pricing") == 1
    assert out.count("client") == 1


# ---- ensure_indexed + idempotency + prune ---------------------------------


def test_ensure_indexed_indexes_all_sessions(two_sessions):
    n = index.ensure_indexed()
    assert n == 2


def test_ensure_indexed_is_idempotent(two_sessions):
    index.ensure_indexed()
    # Second run with no changes indexes nothing new.
    assert index.ensure_indexed() == 0


def test_ensure_indexed_reindexes_when_transcript_mtime_advances(two_sessions):
    index.ensure_indexed()
    # Rewrite a transcript and bump its mtime into the future.
    tpath = session.transcript_path("2026-07-28_12-25-20")
    tpath.write_text(MERGED_TRANSCRIPT.replace("pricing", "PRICING-UPDATED"), encoding="utf-8")
    future = time.time() + 5
    import os
    os.utime(tpath, (future, future))
    n = index.ensure_indexed()
    assert n == 1  # only the changed session


def test_ensure_indexed_rebuild_wipes_and_repopulates(two_sessions):
    index.ensure_indexed()
    n = index.ensure_indexed(rebuild=True)
    assert n == 2
    # A second non-rebuild run finds nothing stale.
    assert index.ensure_indexed() == 0


def test_ensure_indexed_skips_corrupt_transcript_gracefully(xdg):
    _make_session("2026-07-30_10-00-00", "valid transcript body")  # no crash
    # A session whose transcript file can't be read shouldn't abort the batch.
    # We simulate unreadability by making the dir but pointing the path at a
    # missing file is already covered (returns 0). Here we just confirm a
    # recording-only session (no transcript) is skipped cleanly.
    session.create_session_dir("2026-07-31_10-00-00")
    session.update_meta("2026-07-31_10-00-00", status=session.STATUS_RECORDED)
    n = index.ensure_indexed()
    # Only the session WITH a transcript gets indexed.
    assert n == 1


def test_ensure_indexed_prunes_deleted_sessions(two_sessions):
    index.ensure_indexed()
    # Delete one session directory entirely.
    import shutil
    shutil.rmtree(session.session_dir("2026-07-29_09-00-00"), ignore_errors=True)
    index.ensure_indexed()
    con = sqlite3.connect(str(index.index_path()))
    remaining = [r[0] for r in con.execute("SELECT DISTINCT session_id FROM transcript_fts").fetchall()]
    con.close()
    assert "2026-07-29_09-00-00" not in remaining
    assert "2026-07-28_12-25-20" in remaining


# ---- self-healing ----------------------------------------------------------


def test_corrupt_db_is_deleted_and_rebuilt(two_sessions):
    index.ensure_indexed()
    db = index.index_path()
    # Clobber the DB file with garbage so it's unreadable.
    db.write_bytes(b"not a sqlite database")
    # ensure_indexed must self-heal rather than raise.
    n = index.ensure_indexed()
    assert n >= 1
    # And search now works against the rebuilt index.
    hits = index.search("pricing")
    assert len(hits) >= 1


def test_search_self_heals_on_database_error(two_sessions):
    index.ensure_indexed()
    db = index.index_path()
    db.write_bytes(b"garbage")
    # search() catches DatabaseError, rebuilds, and returns results.
    hits = index.search("client")
    assert len(hits) >= 1


# ---- search ranking + scoping + fallback ----------------------------------


def test_search_returns_bm25_ranked_hits(two_sessions):
    index.ensure_indexed()
    hits = index.search("client pricing discount")
    assert len(hits) >= 1
    # The line about pricing/discount is the strongest match.
    assert any("pricing" in h.text.lower() for h in hits)
    top = hits[0]
    assert top.session_id == "2026-07-28_12-25-20"
    assert top.speaker == "Mic"
    assert top.ts_offset == 12.0


def test_search_hit_has_surrounding_context(two_sessions):
    index.ensure_indexed()
    hits = index.search("discount")
    assert hits
    ctx = hits[0].context
    # Context includes neighbors joined by " | ".
    assert " | " in ctx
    assert hits[0].text in ctx


def test_search_respects_limit(two_sessions):
    index.ensure_indexed()
    hits = index.search("the", limit=2)  # "the" appears multiple times -> LIKE fallback
    assert len(hits) <= 2


def test_search_scopes_to_session_ids(two_sessions):
    index.ensure_indexed()
    # Only the API-migration session should match "migration".
    hits = index.search("migration", session_ids=["2026-07-29_09-00-00"])
    assert len(hits) == 1
    assert hits[0].session_id == "2026-07-29_09-00-00"


def test_search_session_ids_filter_excludes_other_sessions(two_sessions):
    index.ensure_indexed()
    # "the" matches lines in BOTH sessions; scoping to one session excludes the other.
    hits_both = index.search("the", limit=20)
    hits_scoped = index.search("the", limit=20, session_ids=["2026-07-29_09-00-00"])
    scoped_ids = {h.session_id for h in hits_scoped}
    assert scoped_ids <= {"2026-07-29_09-00-00"}
    assert len(hits_scoped) <= len(hits_both)


def test_search_falls_back_to_like_for_stopword_only_query(two_sessions):
    index.ensure_indexed()
    # "the" is a stopword -> sanitize_match returns "" -> LIKE fallback.
    hits = index.search("the")
    assert len(hits) >= 1  # "the" appears in both transcripts


def test_search_returns_empty_for_no_matches(two_sessions):
    index.ensure_indexed()
    hits = index.search("zzznomatchxyz")
    assert hits == []


def test_index_path_under_data_home(xdg):
    p = index.index_path()
    # Lives in the XDG data home, not under sessions/.
    assert p.name == "index.db"
    assert "sessions" not in str(p)
    assert str(p).startswith(str(xdg))


def test_db_uses_wal_journal_mode(two_sessions):
    index.ensure_indexed()
    con = sqlite3.connect(str(index.index_path()))
    mode = con.execute("PRAGMA journal_mode").fetchone()[0]
    con.close()
    assert mode.lower() == "wal"
