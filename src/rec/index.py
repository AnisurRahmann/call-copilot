"""SQLite FTS5 full-text index over meeting transcripts.

This is the search backing for the MCP `search_transcripts` tool (and the
`rec index` command). It is a **disposable cache**: it can be deleted at any
time and rebuilt from the `transcript.md` files on disk, and it must NEVER be
the reason `rec` breaks. The whole point of this layer is read-side
acceleration — building it lazily on first search, and rebuilding on demand.

The index lives at `~/.local/share/rec/index.db` (XDG data home), outside the
per-session directories so a `rm -rf sessions/` doesn't strand index rows (and
vice-versa: deleting index.db loses nothing).

Design choices:
  - FTS5 is part of the stdlib `sqlite3` build on macOS/CPython — no new
    dependency.
  - WAL journal mode + busy_timeout so a writer (the indexer) never blocks a
    reader (a search) for long, and so building the index can't stall a
    recording (the recorder only touches WAVs, but the DB sits in the same
    data home — WAL keeps contention negligible regardless).
  - Self-healing: any `sqlite3.DatabaseError` while opening/querying causes the
    DB file to be deleted and rebuilt. It's a cache; a corrupt cache is
    replaced, never propagated to the caller.
  - Transcript text is NEVER logged at INFO or below (privacy constraint shared
    with the rest of the package). Only ids, counts, and mtimes.

Schema:
    sessions(session_id PK, transcript_mtime, indexed_at)
    transcript_fts(FTS5: session_id, line_no, ts_offset, speaker, text)

`transcript_fts` rows are one per *spoken line* (timestamped paragraph), so a
search hit maps back to a precise point in the meeting. `speaker` is the
`Mic`/`System` label parsed from the transcript (or NULL when the transcript
has no label).
"""

from __future__ import annotations

import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from . import config
from .log import get_logger

log = get_logger(__name__)

INDEX_FILENAME = "index.db"
# A small English stopword list. These add noise to a MATCH query and almost
# never carry the meaning the user is searching for. Kept short on purpose —
# we're doing keyword search, not NLP.
_STOPWORDS = frozenset(
    "a an and are as at be but by for if in into is it no not of on or such "
    "that the their then there these they this to was will with we you i he "
    "she so what when where which who why how do does did has have had".split()
)


# ---- transcript parser -----------------------------------------------------


# Matches a transcript line in EITHER format produced by formatter.py:
#   merged:  [System] [00:12] some text
#   single:  System [00:00] some text   (label before the timestamp, no brackets)
# Also tolerates an arbitrary run of whitespace between fields. The timestamp
# is `[h:]mm:ss`. `ts_offset` becomes seconds (None if unparseable).
_LINE_RE = re.compile(
    r"^(?:\[(?P<spk_merged>Mic|System)\]\s+)?"  # optional [System] / [Mic]
    r"(?:(?P<spk_single>Mic|System)\s+)?"        # optional bare System / Mic
    r"\[(?P<ts>\d{1,2}:\d{2}(?::\d{2})?)\]\s*"   # [mm:ss] or [h:mm:ss]
    r"(?P<text>.*)$"
)

# The header above the first `---` is metadata (Date/Duration/File/Sources),
# not speech — skip it. We split on a line that is exactly `---`.
_HEADER_SEP_RE = re.compile(r"(?m)^---\s*$")


@dataclass
class ParsedLine:
    """One timestamped transcript line."""

    line_no: int  # 0-based index among parsed (speech) lines
    ts_offset: float | None  # seconds from the start of the recording
    speaker: str | None  # "Mic" / "System" / None
    text: str


def _ts_to_seconds(ts: str) -> float | None:
    """`mm:ss` or `h:mm:ss` → seconds. None if it doesn't parse."""
    parts = ts.split(":")
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return None
    if len(nums) == 2:
        return float(nums[0] * 60 + nums[1])
    if len(nums) == 3:
        return float(nums[0] * 3600 + nums[1] * 60 + nums[2])
    return None


def parse_transcript(text: str) -> list[ParsedLine]:
    """Parse a transcript.md body into timestamped speech lines.

    Skips the markdown header (everything up to and including the first `---`
    separator) and any blank / non-matching line. The `line_no` field is a
    0-based counter over the *returned* lines, so callers can reference a hit
    back to its position in this list.
    """
    # Drop the header block, if present (formatter.py always emits one).
    splits = _HEADER_SEP_RE.split(text, maxsplit=1)
    body = splits[1] if len(splits) == 2 else text

    out: list[ParsedLine] = []
    for raw in body.splitlines():
        line = raw.strip()
        if not line:
            continue
        m = _LINE_RE.match(line)
        if not m:
            # Non-timestamped line (e.g. a stray note) — skip, don't index.
            continue
        speaker = m.group("spk_merged") or m.group("spk_single")
        out.append(
            ParsedLine(
                line_no=len(out),
                ts_offset=_ts_to_seconds(m.group("ts")),
                speaker=speaker,
                text=m.group("text").strip(),
            )
        )
    return out


# ---- paths + connection ----------------------------------------------------


def index_path() -> Path:
    """Where the FTS5 index DB lives (~/.local/share/rec/index.db)."""
    return config._data_home() / INDEX_FILENAME


def _connect(db_path: Path) -> sqlite3.Connection:
    """Open a connection with WAL + a busy timeout, and ensure the schema.

    WAL lets readers and the (single) writer coexist without long locks;
    `busy_timeout` makes a contended writer wait briefly instead of raising.
    `synchronous=NORMAL` is the WAL-recommended tradeoff (safe against crashes,
    faster than FULL).
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db_path), timeout=5.0)
    con.execute(f"PRAGMA busy_timeout = {int(5.0 * 1000)}")
    con.execute("PRAGMA journal_mode = WAL")
    con.execute("PRAGMA synchronous = NORMAL")
    _init_schema(con)
    return con


def _init_schema(con: sqlite3.Connection) -> None:
    """Create tables if missing. Idempotent."""
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS sessions (
            session_id        TEXT PRIMARY KEY,
            transcript_mtime  REAL NOT NULL,
            indexed_at        REAL NOT NULL
        )
        """
    )
    # `unicode61` is the default tokenizer; `remove_diacritics 1` normalizes
    # accents so é matches e. FTS5 is built into the stdlib sqlite3 on macOS.
    con.execute(
        """
        CREATE VIRTUAL TABLE IF NOT EXISTS transcript_fts USING fts5(
            session_id UNINDEXED,
            line_no    UNINDEXED,
            ts_offset  UNINDEXED,
            speaker    UNINDEXED,
            text,
            tokenize = 'unicode61 remove_diacritics 1'
        )
        """
    )
    con.commit()


# ---- indexing --------------------------------------------------------------


def _on_disk_session_ids() -> set[str]:
    """Session ids that currently exist as directories on disk."""
    root = config.sessions_root()
    if not root.exists():
        return set()
    return {p.name for p in root.iterdir() if p.is_dir()}


def _index_one(con: sqlite3.Connection, session_id: str) -> int:
    """Index (or re-index) one session's transcript. Returns rows indexed.

    Idempotent: deletes any prior rows for this session first, then reinserts.
    Never raises on a missing/corrupt transcript — logs a warning and returns 0
    so one bad file can't abort a batch.
    """
    from . import session as session_mod

    tpath = session_mod.transcript_path(session_id)
    if not tpath.exists():
        # No transcript (recording-only or silent session). Drop any stale rows
        # for it so the index reflects reality, but don't treat it as an error.
        con.execute("DELETE FROM transcript_fts WHERE session_id = ?", (session_id,))
        con.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))
        con.commit()
        return 0

    try:
        text = tpath.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        log.warning("could not read transcript for %s (%r) — skipping", session_id, e)
        return 0

    lines = parse_transcript(text)
    mtime = tpath.stat().st_mtime

    # Replace this session's rows atomically.
    con.execute("BEGIN IMMEDIATE")
    try:
        con.execute("DELETE FROM transcript_fts WHERE session_id = ?", (session_id,))
        if lines:
            con.executemany(
                "INSERT INTO transcript_fts "
                "(session_id, line_no, ts_offset, speaker, text) VALUES (?, ?, ?, ?, ?)",
                [
                    (session_id, pl.line_no, pl.ts_offset, pl.speaker, pl.text)
                    for pl in lines
                ],
            )
        con.execute(
            "INSERT INTO sessions(session_id, transcript_mtime, indexed_at) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(session_id) DO UPDATE SET "
            "  transcript_mtime = excluded.transcript_mtime, "
            "  indexed_at = excluded.indexed_at",
            (session_id, mtime, time.time()),
        )
        con.commit()
    except Exception:
        con.rollback()
        raise
    return len(lines)


def _prune(con: sqlite3.Connection) -> int:
    """Delete index rows for session_ids that no longer exist on disk.

    Returns the count of pruned sessions. Keeps the cache honest when a user
    `rm -rf`s a session directory.
    """
    live = _on_disk_session_ids()
    cur = con.execute("SELECT session_id FROM sessions")
    known = {row[0] for row in cur.fetchall()}
    gone = known - live
    if gone:
        for sid in gone:
            con.execute("DELETE FROM transcript_fts WHERE session_id = ?", (sid,))
            con.execute("DELETE FROM sessions WHERE session_id = ?", (sid,))
        con.commit()
        log.info("pruned %d stale session(s) from the index", len(gone))
    return len(gone)


def _outdated_session_ids(con: sqlite3.Connection) -> list[str]:
    """Session ids whose transcript mtime is newer than its indexed_at (or not indexed)."""
    from . import session as session_mod

    live = sorted(_on_disk_session_ids(), reverse=True)
    if not live:
        return []
    cur = con.execute("SELECT session_id, transcript_mtime FROM sessions")
    known: dict[str, float] = {row[0]: row[1] for row in cur.fetchall()}

    stale: list[str] = []
    for sid in live:
        tpath = session_mod.transcript_path(sid)
        if not tpath.exists():
            continue  # nothing to index; _prune will drop any stale row
        mtime = tpath.stat().st_mtime
        if sid not in known or mtime > known[sid]:
            stale.append(sid)
    return stale


def ensure_indexed(rebuild: bool = False) -> int:
    """Bring the index up to date. Returns the number of sessions indexed.

    - `rebuild=True`: wipe both tables and reindex every on-disk transcript.
    - Otherwise: index only sessions whose transcript mtime is newer than their
      `indexed_at`, and prune rows for deleted sessions.

    Self-healing: on any `sqlite3.DatabaseError` the index DB is deleted and
    rebuilt from scratch (it is a disposable cache). Never raises — callers
    (a recording, an MCP search) depend on this not breaking.
    """
    db_path = index_path()
    try:
        con = _connect(db_path)
    except sqlite3.DatabaseError as e:
        log.warning("index db unreadable (%r) — rebuilding from scratch", e)
        _delete_db(db_path)
        con = _connect(db_path)

    try:
        if rebuild:
            con.execute("DELETE FROM transcript_fts")
            con.execute("DELETE FROM sessions")
            con.commit()
            log.info("index rebuild requested — wiped")
        else:
            _prune(con)

        to_index = _outdated_session_ids(con)
        # On a fresh rebuild, _outdated returns ALL on-disk sessions (none are
        # known), so this single path covers both cases.
        count = 0
        for sid in to_index:
            n = _index_one(con, sid)
            count += 1
            log.debug("indexed %s (%d line(s))", sid, n)
        if count:
            log.info("indexed %d session(s)", count)
        return count
    except sqlite3.DatabaseError as e:
        # The cache is corrupt in a way we can't recover from in-place. Drop it
        # and try one more time so the caller still gets a working index.
        log.warning("index db errored during index (%r) — dropping and rebuilding", e)
        try:
            con.close()
        except Exception:
            pass
        _delete_db(db_path)
        con = _connect(db_path)
        count = 0
        for sid in sorted(_on_disk_session_ids(), reverse=True):
            _index_one(con, sid)
            count += 1
        log.info("rebuilt index from scratch: %d session(s)", count)
        return count
    finally:
        try:
            con.close()
        except Exception:
            pass


def _delete_db(db_path: Path) -> None:
    """Remove the index DB and its WAL/SHM sidecars."""
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(db_path) + suffix)
        try:
            p.unlink(missing_ok=True)
        except OSError:
            pass


def _rebuild_fresh(db_path: Path) -> sqlite3.Connection:
    """Open a fresh DB at `db_path` and index every on-disk transcript into it.

    Used by the self-heal paths in `search()`: after deleting a corrupt cache,
    we reconnect (which creates the schema) and repopulate synchronously so the
    immediate retry has data to search. Returns the open, populated connection.
    """
    con = _connect(db_path)
    count = 0
    for sid in sorted(_on_disk_session_ids(), reverse=True):
        _index_one(con, sid)
        count += 1
    if count:
        log.info("rebuilt index from scratch: %d session(s)", count)
    return con


# ---- query sanitization + search ------------------------------------------


def sanitize_match(query: str) -> str:
    """Turn a raw user query into a safe, useful FTS5 MATCH expression.

    Steps:
      1. Lowercase and keep only alphanumerics + whitespace (FTS5 MATCH has its
         own query syntax — `:`, `*`, `"`, `(`, `)` are operators; a stray one
         raises `sqlite3.OperationalError`). This also defangs injection.
      2. Drop stopwords and short tokens (len < 2).
      3. Wrap each surviving term in double quotes (an FTS5 phrase) and OR-join
         them, so any one keyword is enough to hit. AND would miss relevant
         lines that use a subset of the keywords.

    Returns "" if nothing usable survives (caller falls back to LIKE / guidance).
    """
    cleaned = re.sub(r"[^0-9a-z\s]", " ", query.lower())
    tokens = [t for t in cleaned.split() if len(t) >= 2 and t not in _STOPWORDS]
    if not tokens:
        return ""
    # Dedupe while preserving order.
    seen: set[str] = set()
    uniq = [t for t in tokens if not (t in seen or seen.add(t))]
    return " OR ".join(f'"{t}"' for t in uniq)


@dataclass
class SearchHit:
    """One ranked search result."""

    session_id: str
    line_no: int
    ts_offset: float | None
    speaker: str | None
    text: str
    context: str  # this line + up to 1 neighbor on each side, joined by " | "


def _with_context(lines: list[ParsedLine], idx: int) -> str:
    """Render line `idx` with up to one neighbor on each side for readability."""
    lo = max(0, idx - 1)
    hi = min(len(lines), idx + 2)
    return " | ".join(pl.text for pl in lines[lo:hi])


def _build_context_from_db(con: sqlite3.Connection, session_id: str, line_no: int) -> str:
    """Reconstruct surrounding context for a hit directly from the index."""
    rows = con.execute(
        "SELECT line_no, text FROM transcript_fts "
        "WHERE session_id = ? AND line_no BETWEEN ? AND ? "
        "ORDER BY line_no",
        (session_id, max(0, line_no - 1), line_no + 1),
    ).fetchall()
    return " | ".join(r[1] for r in rows) if rows else ""


def search(
    query: str,
    limit: int = 10,
    session_ids: list[str] | None = None,
) -> list[SearchHit]:
    """Search transcripts for keywords. Ranked by BM25.

    `query` is raw user input; we sanitize it (drop punctuation + stopwords,
    quote + OR-join) before handing it to FTS5. On a `sqlite3.OperationalError`
    (malformed MATCH) or an empty sanitized query, fall back to a plain LIKE
    over indexed text.

    `session_ids`, when given, restricts the search to those sessions. `limit`
    caps the hit count (default 10). Each hit carries one line of surrounding
    context. Returns an empty list when there are no matches — the caller (the
    MCP tool) decides how to present that.
    """
    db_path = index_path()
    try:
        con = _connect(db_path)
    except sqlite3.DatabaseError as e:
        log.warning("index db unreadable (%r) — rebuilding from scratch", e)
        _delete_db(db_path)
        con = _rebuild_fresh(db_path)

    try:
        return _search(con, query, limit, session_ids)
    except sqlite3.DatabaseError as e:
        # Cache corrupt beyond an in-place fix → rebuild and retry once.
        log.warning("search hit db error (%r) — rebuilding and retrying", e)
        try:
            con.close()
        except Exception:
            pass
        con = _rebuild_fresh(db_path)
        return _search(con, query, limit, session_ids)
    finally:
        try:
            con.close()
        except Exception:
            pass


def _search(
    con: sqlite3.Connection,
    query: str,
    limit: int,
    session_ids: list[str] | None,
) -> list[SearchHit]:
    """Inner search: assumes a healthy, open connection. Raises on db error."""
    match_expr = sanitize_match(query)

    # Optional session filter. We can't bind a list into FTS5's MATCH, so we
    # filter via the outer WHERE on the unindexed session_id column.
    session_filter = ""
    params: list[object] = []
    if session_ids:
        placeholders = ",".join("?" for _ in session_ids)
        session_filter = f" AND session_id IN ({placeholders})"
        params.extend(session_ids)

    rows: list[tuple] = []
    if match_expr:
        try:
            # bm25() returns smaller = more relevant, so we sort ascending.
            sql = (
                "SELECT session_id, line_no, ts_offset, speaker, text, bm25(transcript_fts) AS rank "
                "FROM transcript_fts WHERE transcript_fts MATCH ?"
                + session_filter
                + " ORDER BY rank ASC LIMIT ?"
            )
            rows = con.execute(sql, [match_expr, *params, limit]).fetchall()
        except sqlite3.OperationalError as e:
            # NOTE: this catches QUERY-SYNTAX failures (a sanitized query should
            # never hit one, but a tokenizer edge case could) and falls through
            # to LIKE. It is deliberately narrow (OperationalError, not its
            # parent DatabaseError) so genuine DB corruption still propagates to
            # search()'s outer handler, which deletes + rebuilds the cache. Do
            # NOT widen this to DatabaseError — that would mask real corruption
            # and turn every search into a silent LIKE fallback.
            log.info("MATCH failed (%r) — falling back to LIKE", e)
            rows = []

    if not rows:
        # LIKE fallback: case-insensitive substring across every indexed line.
        # Slower, but matches partial words the tokenizer might split oddly and
        # handles the empty-sanitized-query case (e.g. a single stopword search).
        like = "%" + query.replace("%", r"\%").replace("_", r"\_") + "%"
        sql = (
            "SELECT session_id, line_no, ts_offset, speaker, text "
            "FROM transcript_fts WHERE text LIKE ? ESCAPE '\\'"
            + session_filter
            + " ORDER BY session_id, line_no LIMIT ?"
        )
        rows = con.execute(sql, [like, *params, limit]).fetchall()

    hits: list[SearchHit] = []
    for r in rows:
        sid, line_no, ts_offset, speaker, text = r[0], r[1], r[2], r[3], r[4]
        ctx = _build_context_from_db(con, sid, int(line_no)) or text
        hits.append(
            SearchHit(
                session_id=sid,
                line_no=int(line_no),
                ts_offset=float(ts_offset) if ts_offset is not None else None,
                speaker=speaker,
                text=text,
                context=ctx,
            )
        )
    return hits
