"""The Call Copilot MCP server — read-only transcript access over stdio.

Launched by `rec mcp`. Exposes three tools (list_sessions, get_session,
search_transcripts) so Claude Code and any other MCP client can answer
questions about recorded meetings with a cited session id.

INVARIANTS (enforced here and pinned by tests):
  - **Read-only.** This module imports only `session`, `config`, `index`, and
    `log`. It NEVER imports `recorder` and never starts/stops/deletes a
    recording. There is no write path through it.
  - **No network.** Nothing here makes a network call.
  - **stdout is the protocol.** MCP stdio transport puts JSON-RPC on stdout, so
    this module must never write to stdout: no `print`, no `click.echo`, no
    `rich.Console` on the default stream (rich defaults to stdout). All logging
    goes through `log.get_logger`, whose RichHandler is pinned to **stderr**
    (see log.py), plus the SDK's own stderr handler.

Tool design (the docstrings below ARE the prompt the model sees — they're
written to teach the discovery chain: `list_sessions` → `get_session`, with
`search_transcripts` as an accelerator for long transcripts, fed keywords not
questions).
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from pydantic import Field

from . import index, session
from .log import get_logger

log = get_logger(__name__)

# The SDK import is deferred to build_server()/run() so that simply importing
# this module (e.g. for the read-only import-graph test) does not require the
# `mcp` package. `rec mcp` reports a clean error if mcp is missing.


# ---- read-side helpers (pure, no side effects, no network) -----------------


def _session_source(session_id: str) -> str:
    """Infer the audio source(s) of a session from which WAVs exist.

    Returns "both" / "mic" / "system". Falls back to "system" when neither WAV
    is present (a transcript-only or legacy session) so the field is always
    populated and downstream consumers don't have to handle None.
    """
    has_sys = session.wav_path(session_id).exists()
    has_mic = session.mic_wav_path(session_id).exists()
    if has_sys and has_mic:
        return "both"
    if has_mic:
        return "mic"
    return "system"


def _session_size_bytes(session_id: str) -> int:
    """Total bytes of audio in a session dir (sum of any WAVs). 0 if none."""
    total = 0
    for p in (
        session.wav_path(session_id),
        session.mic_wav_path(session_id),
    ):
        try:
            if p.exists():
                total += p.stat().st_size
        except OSError:
            pass
    return total


def _offset_label(seconds: float | None) -> str | None:
    """Render a ts_offset (seconds) as an `[h:]mm:ss` label, or None."""
    if seconds is None:
        return None
    total = max(0, int(round(seconds)))
    if total >= 3600:
        h, rem = divmod(total, 3600)
        m, s = divmod(rem, 60)
        return f"[{h}:{m:02d}:{s:02d}]"
    m, s = divmod(total, 60)
    return f"[{m:02d}:{s:02d}]"


def _speaker_label(speaker: str | None) -> str | None:
    """Normalize a parsed speaker ('Mic'/'System'/None) to a bracketed label."""
    if not speaker:
        return None
    return f"[{speaker}]"


def _parse_started_at(value: str | None) -> datetime | None:
    """Tolerantly parse a session's started_at (ISO datetime or date-only)."""
    if not value:
        return None
    # session.json stores ISO-with-seconds; be lenient about the time part.
    candidates = (
        value,
        value.replace(" ", "T"),
    )
    for cand in candidates:
        try:
            return datetime.fromisoformat(cand)
        except ValueError:
            continue
    # Date-only fallback (YYYY-MM-DD).
    try:
        return datetime.strptime(value[:10], "%Y-%m-%d")
    except ValueError:
        return None


def _is_date_only(bound: str) -> bool:
    """True if `bound` is a date with no time-of-day (e.g. '2026-07-28').

    A date-only bound covers the whole calendar day; a bound carrying a `T`
    (ISO datetime) is an exact instant. We check the raw string because
    `datetime.fromisoformat` fills midnight for date-only input, making the two
    indistinguishable on the parsed value.
    """
    return "t" not in bound.lower() and " " not in bound.strip()


def _within_window(
    started_at: datetime | None, before: str | None, after: str | None
) -> bool:
    """True if `started_at` falls within the [after, before] window.

    `before`/`after` are inclusive bounds. A **date-only** bound (e.g.
    '2026-07-28') covers that whole calendar day; a **datetime** bound (e.g.
    '2026-07-28T12:00:00') is an exact instant. None means unbounded on that
    side. A session with an unparseable started_at is kept (best-effort filter).
    """
    if started_at is None:
        return True
    if before is not None:
        b = _parse_started_at(before)
        if b is not None:
            # Date-only bound -> inclusive whole-day; datetime bound -> exact.
            if _is_date_only(before):
                if started_at.date() > b.date():
                    return False
            elif started_at > b:
                return False
    if after is not None:
        a = _parse_started_at(after)
        if a is not None:
            if _is_date_only(after):
                if started_at.date() < a.date():
                    return False
            elif started_at < a:
                return False
    return True


# ---- the three tools -------------------------------------------------------


def list_sessions(
    limit: Annotated[int, Field(ge=1, le=500, description="Max sessions to return.")] = 50,
    before: Annotated[
        str | None,
        Field(description="Inclusive upper bound on started_at — a date ('2026-07-30') or datetime."),
    ] = None,
    after: Annotated[
        str | None,
        Field(description="Inclusive lower bound on started_at — a date or datetime."),
    ] = None,
) -> list[dict]:
    """List recorded meeting sessions, newest first.

    Start here to find the session you want. Each session is one recorded
    meeting; narrow by date with `before`/`after` (each a 'YYYY-MM-DD' date or
    ISO datetime), then call `get_session` to read a whole transcript.

    Returns, per session: `id` (cite this), `started_at`, `duration_seconds`,
    `duration_human`, `size_bytes` (audio on disk), `word_count`,
    `has_transcript`, and `source` ("mic" / "system" / "both"). Sessions with
    `has_transcript=false` have no searchable text (silent or recording-only).
    """
    sessions = session.list_sessions()  # newest-first already
    out: list[dict] = []
    for m in sessions:
        started = _parse_started_at(m.started_at)
        if not _within_window(started, before, after):
            continue
        has_t = session.transcript_path(m.id).exists()
        out.append(
            {
                "id": m.id,
                "started_at": m.started_at,
                "duration_seconds": m.duration,
                "duration_human": session.format_duration_human(m.duration),
                "size_bytes": _session_size_bytes(m.id),
                "word_count": m.word_count,
                "has_transcript": has_t,
                "source": _session_source(m.id),
                "status": m.status,
            }
        )
        if len(out) >= limit:
            break
    log.info("list_sessions returned %d (limit=%d before=%r after=%r)",
             len(out), limit, before, after)
    return out


def get_session(
    id: Annotated[str, Field(description="Session id or a unique prefix (e.g. '2026-07-27').")],
    include_transcript: Annotated[
        bool, Field(description="If true (default), include the full transcript text.")
    ] = True,
) -> dict:
    """Read the full metadata (and transcript) of one session.

    Use this after `list_sessions` narrows the session, or to pull full context
    around a `search_transcripts` hit. `id` accepts a unique prefix like the
    rest of the CLI does ('2026-07-27' matches '2026-07-27_14-30-00').

    Returns the session's metadata plus, when `include_transcript=true` (the
    default), the complete `transcript` text. Raises a tool error (visible to
    the model) if no session matches or it has no transcript.
    """
    resolved = session.resolve_session_id(id)
    if resolved is None:
        raise ValueError(
            f"No session matches {id!r}. Call list_sessions to see session ids."
        )
    meta = session.load_meta(resolved)
    tpath = session.transcript_path(resolved)
    has_t = tpath.exists()
    transcript_text: str | None = None
    if include_transcript:
        if not has_t:
            raise ValueError(
                f"Session {resolved} has no transcript (status="
                f"{meta.status if meta else 'unknown'}). It may be a silent or "
                f"recording-only session."
            )
        try:
            transcript_text = tpath.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            raise ValueError(f"Could not read transcript for {resolved}: {e}") from e

    log.info("get_session %s -> %s (include_transcript=%s)",
             id, resolved, include_transcript)
    result: dict = {
        "id": resolved,
        "started_at": meta.started_at if meta else None,
        "duration_seconds": meta.duration if meta else None,
        "duration_human": session.format_duration_human(meta.duration) if meta else "--",
        "word_count": meta.word_count if meta else None,
        "model": meta.model if meta else None,
        "source": _session_source(resolved),
        "status": meta.status if meta else None,
        "has_transcript": has_t,
    }
    if transcript_text is not None:
        result["transcript"] = transcript_text
    return result


def search_transcripts(
    query: Annotated[
        str,
        Field(
            description=(
                "KEYWORDS to find in transcripts, not a full question — "
                "e.g. 'pricing discount', not 'what did they say about pricing?'. "
                "Common words (the/and/...) are ignored. Hits are ranked."
            ),
        ),
    ],
    limit: Annotated[int, Field(ge=1, le=100, description="Max hits to return.")] = 10,
    session_ids: Annotated[
        list[str] | None,
        Field(description="Restrict the search to these session ids (or prefixes)."),
    ] = None,
) -> dict:
    """Find transcript lines matching keywords. An accelerator for long meetings.

    Pass KEYWORDS, not a full question: 'client deadline' rather than 'what did
    the client say about the deadline?'. Common stopwords are dropped and the
    rest are OR-joined, so any one keyword is enough to hit.

    For recent or short meetings it is often faster and more reliable to use
    `list_sessions` (narrow by date) then `get_session(id, include_transcript=true)`
    to read the whole thing. Use this tool when a transcript is long and you
    need to locate specific terms across many sessions.

    Each hit includes `session_id` (cite this), `started_at`, `timestamp_offset`
    (seconds from start, when available), `offset_label` (e.g. '[12:03]'),
    `speaker` ('[Mic]' / '[System]' / null), `line` (the matched line), and
    `context` (the matched line plus one neighbor on each side). When there are
    no matches, the response carries `guidance` pointing to list_sessions +
    get_session instead of an empty hits list.
    """
    # Resolve any prefixes in session_ids up front so FTS gets exact ids. An
    # ambiguous prefix raises AmbiguousSessionId (a ValueError) — let it
    # propagate as a tool error so the model sees the candidates and can pick,
    # rather than silently searching the newest match.
    resolved_ids: list[str] | None = None
    if session_ids:
        resolved_ids = []
        for sid in session_ids:
            r = session.resolve_session_id(sid)
            if r is not None:
                resolved_ids.append(r)
        if not resolved_ids:
            # None of the provided ids resolved — return guidance, not an error.
            return {
                "hits": [],
                "count": 0,
                "guidance": (
                    f"None of the provided session_ids {session_ids} matched a "
                    f"recorded session. Call list_sessions to see valid ids."
                ),
            }

    # Lazy indexing: bring the cache up to date on first search, then query.
    try:
        index.ensure_indexed()
        hits = index.search(query, limit=limit, session_ids=resolved_ids)
    except Exception as e:
        # The index layer self-heals, but defend against the unexpected so a
        # search never crashes the server. Log without transcript content.
        log.warning("search_transcripts failed (%r); returning guidance", e)
        return {
            "hits": [],
            "count": 0,
            "guidance": (
                "Search is temporarily unavailable. Use list_sessions to find a "
                "session by date, then get_session(id, include_transcript=true) "
                f"to read it. (detail: {e})"
            ),
        }

    if not hits:
        return {
            "hits": [],
            "count": 0,
            "guidance": (
                f"No transcript lines matched {query!r}. Try different keywords, "
                "or use list_sessions (narrow by date) then "
                "get_session(id, include_transcript=true) to read the whole "
                "transcript."
            ),
        }

    # Enrich each hit with the session's started_at for citation.
    started_by_id: dict[str, str | None] = {}
    rendered: list[dict] = []
    for h in hits:
        if h.session_id not in started_by_id:
            m = session.load_meta(h.session_id)
            started_by_id[h.session_id] = m.started_at if m else None
        rendered.append(
            {
                "session_id": h.session_id,
                "started_at": started_by_id[h.session_id],
                "timestamp_offset": h.ts_offset,
                "offset_label": _offset_label(h.ts_offset),
                "speaker": _speaker_label(h.speaker),
                "line": h.text,
                "context": h.context,
            }
        )
    log.info("search_transcripts %r -> %d hit(s)", query, len(rendered))
    return {"hits": rendered, "count": len(rendered)}


# ---- server construction + entry point -------------------------------------


def build_server():  # -> mcp.server.MCPServer (typed loosely to keep import lazy)
    """Construct and return the MCPServer with all three tools registered.

    `mcp` is imported here (not at module top) so that importing this module
    for introspection/tests does not require the SDK. All three tools carry
    `read_only_hint=True` + `open_world_hint=False` to signal to clients that
    they have no side effects and operate only on local data.
    """
    from mcp.server import MCPServer
    from mcp.types import ToolAnnotations

    read_only = ToolAnnotations(read_only_hint=True, open_world_hint=False)

    mcp = MCPServer(
        "call-copilot",
        title="Call Copilot",
        description=(
            "Read-only access to locally-recorded meeting transcripts. Search "
            "and read meetings that were recorded with `rec`. Never starts, "
            "stops, or deletes a recording."
        ),
        instructions=(
            "Tools to explore recorded meeting transcripts. Typical flow: "
            "list_sessions (narrow by date) → get_session (read a transcript). "
            "search_transcripts is an accelerator for finding specific terms "
            "across long/many transcripts; pass it keywords, not questions. "
            "All tools are read-only."
        ),
    )

    mcp.tool(name="list_sessions", annotations=read_only)(list_sessions)
    mcp.tool(name="get_session", annotations=read_only)(get_session)
    mcp.tool(name="search_transcripts", annotations=read_only)(search_transcripts)
    return mcp


def run() -> None:
    """Entry point for `rec mcp` (and `python -m rec.mcp_server`): run the stdio server.

    Defaults to stdio transport. The SDK owns stdout (the JSON-RPC wire); this
    process must not write to it. Logging goes to stderr via rec.log + the SDK.

    Self-sufficient: configures rec's logging + command context itself (mirrors
    cli.main) so the documented `python -m rec.mcp_server` entry point — which
    skips cli.main — still gets the RichHandler-on-stderr + global log file that
    every other `rec` process has. Safe to call again when invoked via the
    `rec mcp` Click path (configure_logging is idempotent).
    """
    from . import log as log_mod

    log_mod.configure_logging("cli", console_level="WARNING")
    log_mod.set_command_context("mcp")
    mcp = build_server()
    log.info("MCP server starting (stdio transport)")
    mcp.run()  # transport defaults to "stdio"


if __name__ == "__main__":  # pragma: no cover
    run()
