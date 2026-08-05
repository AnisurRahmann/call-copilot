"""Tests for rec.mcp_server — the read-only MCP tools.

Runs offline: no audio device, no model download, no network. Each test gets a
fresh tmp XDG root (via the autouse _xdg_tmp fixture). The tool handlers are
pure functions over the on-disk session layout, so we call them directly and
assert on their dict/list results; the stdio handshake (which needs the `mcp`
SDK) is covered separately, gated on the SDK being importable.
"""

from __future__ import annotations

import inspect
import sys
from pathlib import Path

import pytest

pytest.importorskip("mcp")  # the whole module needs the SDK; skip cleanly if absent

from rec import mcp_server, session  # noqa: E402

# ---- fixture builders ------------------------------------------------------

MERGED = """# Meeting Transcript

**Date:** 2026-07-28
**Duration:** 2 min
**Sources:** System (recording.wav) + Microphone (recording-mic.wav)

---

[System] [00:00] Welcome to the standup everyone.

[Mic] [00:12] The client asked about the volume discount on pricing.

[System] [00:45] They want a quote by Friday.
"""

SINGLE = """# Meeting Transcript

**Date:** 2026-07-29
**Duration:** 1 min
**File:** recording.wav

---

System [00:00] API migration update.

System [00:20] Eighty percent through the cutover.
"""


def _make_session(sid, transcript, *, has_sys=True, has_mic=False, started=None, status=None):
    session.create_session_dir(sid)
    session.update_meta(
        sid,
        started_at=started or f"{sid[:10]}T{sid[11:].replace('-', ':')}:00",
        status=status or session.STATUS_TRANSCRIBED,
        duration=120.0,
        word_count=10,
    )
    if transcript is not None:
        session.transcript_path(sid).write_text(transcript, encoding="utf-8")
    if has_sys:
        session.wav_path(sid).write_bytes(b"system-audio")
    if has_mic:
        session.mic_wav_path(sid).write_bytes(b"mic-audio")


@pytest.fixture
def sessions(xdg):
    """Two sessions: mic+system (merged) + system-only (single-source)."""
    _make_session(
        "2026-07-28_12-25-20", MERGED,
        has_sys=True, has_mic=True, started="2026-07-28T12:25:20",
    )
    _make_session(
        "2026-07-29_09-00-00", SINGLE,
        has_sys=True, has_mic=False, started="2026-07-29T09:00:00",
    )
    return xdg


# ---- list_sessions ---------------------------------------------------------


def test_list_sessions_returns_newest_first(sessions):
    out = mcp_server.list_sessions(limit=10)
    assert [s["id"] for s in out] == ["2026-07-29_09-00-00", "2026-07-28_12-25-20"]


def test_list_sessions_includes_all_documented_fields(sessions):
    out = mcp_server.list_sessions(limit=10)
    s = out[0]
    for key in (
        "id", "started_at", "duration_seconds", "duration_human", "size_bytes",
        "word_count", "has_transcript", "source", "status",
    ):
        assert key in s, f"missing field {key!r}"


def test_list_sessions_infers_source_from_wavs(sessions):
    out = {s["id"]: s for s in mcp_server.list_sessions(limit=10)}
    assert out["2026-07-28_12-25-20"]["source"] == "both"
    assert out["2026-07-29_09-00-00"]["source"] == "system"


def test_list_sessions_filters_by_before(sessions):
    out = mcp_server.list_sessions(before="2026-07-28T23:59:59")
    assert [s["id"] for s in out] == ["2026-07-28_12-25-20"]


def test_list_sessions_filters_by_after(sessions):
    out = mcp_server.list_sessions(after="2026-07-29")
    assert [s["id"] for s in out] == ["2026-07-29_09-00-00"]


def test_list_sessions_date_window_inclusive_same_day(sessions):
    # before/after on the same day as a session includes that session.
    out = mcp_server.list_sessions(after="2026-07-28", before="2026-07-28")
    assert [s["id"] for s in out] == ["2026-07-28_12-25-20"]


def test_list_sessions_datetime_bound_is_exact_not_whole_day(xdg):
    """A datetime bound is an exact instant — NOT a whole-day carve-out.

    Regression: a same-day carve-out used to fire for datetime bounds too, so
    'before noon on the 28th' wrongly included a 14:00 session that day.
    """
    _make_session("2026-07-28_09-00-00", SINGLE, started="2026-07-28T09:00:00")
    _make_session("2026-07-28_14-00-00", SINGLE, started="2026-07-28T14:00:00")
    # before 12:00 same day -> only the 09:00 session.
    out = mcp_server.list_sessions(before="2026-07-28T12:00:00")
    assert [s["id"] for s in out] == ["2026-07-28_09-00-00"]
    # after 12:00 same day -> only the 14:00 session.
    out = mcp_server.list_sessions(after="2026-07-28T12:00:00")
    assert [s["id"] for s in out] == ["2026-07-28_14-00-00"]


def test_list_sessions_respects_limit(sessions):
    out = mcp_server.list_sessions(limit=1)
    assert len(out) == 1


def test_list_sessions_empty_when_no_sessions(xdg):
    assert mcp_server.list_sessions() == []


def test_list_sessions_has_transcript_false_for_recording_only(xdg):
    _make_session("2026-07-30_10-00-00", None, status=session.STATUS_RECORDED)
    out = mcp_server.list_sessions()
    assert out[0]["has_transcript"] is False
    assert out[0]["source"] == "system"  # no WAVs -> defaults to system


# ---- get_session -----------------------------------------------------------


def test_get_session_resolves_prefix(sessions):
    out = mcp_server.get_session("2026-07-28")
    assert out["id"] == "2026-07-28_12-25-20"


def test_get_session_includes_transcript_by_default(sessions):
    out = mcp_server.get_session("2026-07-28")
    assert "transcript" in out
    assert "client asked about" in out["transcript"]


def test_get_session_omits_transcript_when_flag_false(sessions):
    out = mcp_server.get_session("2026-07-28", include_transcript=False)
    assert "transcript" not in out
    assert out["has_transcript"] is True


def test_get_session_missing_raises_value_error(sessions):
    with pytest.raises(ValueError, match="list_sessions"):
        mcp_server.get_session("does-not-exist")


def test_get_session_ambiguous_prefix_raises_with_candidates(xdg):
    """A prefix matching multiple sessions must NOT silently pick the newest.

    Regression: resolve_session_id used to return matches[0] (newest-first),
    so the model could cite the wrong meeting. Now it raises AmbiguousSessionId
    (a ValueError subclass) carrying the candidate ids.
    """
    _make_session("2026-07-28_09-00-00", SINGLE, started="2026-07-28T09:00:00")
    _make_session("2026-07-28_14-00-00", MERGED, started="2026-07-28T14:00:00")
    with pytest.raises(ValueError) as exc_info:
        mcp_server.get_session("2026-07-28")
    msg = str(exc_info.value)
    # The error names both candidates so the model can disambiguate.
    assert "2026-07-28_09-00-00" in msg
    assert "2026-07-28_14-00-00" in msg


def test_get_session_unambiguous_date_prefix_still_resolves(xdg):
    """One session that day -> the date prefix resolves cleanly (no false ambiguity)."""
    _make_session("2026-07-28_09-00-00", SINGLE, started="2026-07-28T09:00:00")
    out = mcp_server.get_session("2026-07-28")
    assert out["id"] == "2026-07-28_09-00-00"


def test_search_transcripts_ambiguous_session_id_raises(xdg):
    """An ambiguous session_id in search_transcripts surfaces the candidates too."""
    _make_session("2026-07-28_09-00-00", SINGLE, started="2026-07-28T09:00:00")
    _make_session("2026-07-28_14-00-00", MERGED, started="2026-07-28T14:00:00")
    with pytest.raises(ValueError, match="2026-07-28_09-00-00"):
        mcp_server.search_transcripts("migration", session_ids=["2026-07-28"])


def test_get_session_without_transcript_raises_when_requested(xdg):
    _make_session("2026-07-30_10-00-00", None, status=session.STATUS_RECORDED)
    with pytest.raises(ValueError, match="no transcript"):
        mcp_server.get_session("2026-07-30", include_transcript=True)
    # ...but works fine without the transcript body.
    out = mcp_server.get_session("2026-07-30", include_transcript=False)
    assert out["has_transcript"] is False


# ---- path traversal (security) ---------------------------------------------
#
# An untrusted `id` (e.g. one a prompt-injected model passes to get_session)
# must NEVER read a transcript.md / session.json outside the sessions root.
# These plant a victim file one+ directories ABOVE the sessions root and assert
# every traversal shape is rejected, and that the leaked text never appears in
# any error message either.


@pytest.fixture
def traversal_target(xdg):
    """A legit session inside the root + a 'stolen' transcript.md above it."""
    from rec import config
    root = config.sessions_root()
    root.mkdir(parents=True, exist_ok=True)
    # Legit session inside the root.
    _make_session("2026-07-28_12-00-00", MERGED, started="2026-07-28T12:00:00")
    # Victim transcript OUTSIDE the root (sibling dir, one level up).
    stolen = root.parent / "stolen-secret"
    stolen.mkdir(parents=True, exist_ok=True)
    (stolen / "transcript.md").write_text("TOPSECRET-marker-outside-sessions-root", encoding="utf-8")
    return root


@pytest.mark.parametrize("bad_id", [
    "../stolen-secret",          # one dir up
    "../../stolen-secret",       # deeper (resolves the same here)
    "../stolen-secret/../stolen-secret",
    "/etc/passwd",               # absolute
    "..", ".",
])
def test_get_session_rejects_path_traversal(traversal_target, bad_id):
    """get_session must not read files outside the sessions root."""
    try:
        out = mcp_server.get_session(bad_id, include_transcript=True)
        # If it returned at all, the id must not be the traversal string and
        # the leaked marker must be absent.
        assert out["id"] != bad_id
        assert "TOPSECRET-marker" not in out.get("transcript", "")
    except ValueError as e:
        # The clean "no session matches" path. The leaked text must not appear
        # in the error message either.
        assert "TOPSECRET-marker" not in str(e)


def test_search_transcripts_session_ids_rejects_traversal(traversal_target):
    """A traversal id in session_ids must not leak data via search."""
    out = mcp_server.search_transcripts("TOPSECRET", session_ids=["../stolen-secret"])
    # Either no hits (the id didn't resolve to a real session) or hits only from
    # legit sessions — never the stolen marker.
    for h in out.get("hits", []):
        assert "TOPSECRET-marker" not in h["line"]
        assert "TOPSECRET-marker" not in h["context"]


def test_session_dir_rejects_traversal_directly(xdg):
    """The low-level session_dir guard (defense-in-depth) blocks traversal."""
    for bad in ("../x", "../../y", "/etc", "a/b", "a\\b", "..", "."):
        with pytest.raises(session.InvalidSessionId):
            session.session_dir(bad)


def test_session_dir_accepts_real_session_ids(xdg):
    """Real session ids (YYYY-MM-DD_HH-MM-SS) pass the guard."""
    # No exception for the canonical shape.
    session.session_dir("2026-07-28_12-25-20")
    session.session_dir("2026-07-28")


# ---- search_transcripts ----------------------------------------------------


def test_search_returns_ranked_hits_with_citation_fields(sessions):
    out = mcp_server.search_transcripts("client pricing discount")
    assert out["count"] >= 1
    h = out["hits"][0]
    assert h["session_id"] == "2026-07-28_12-25-20"
    assert h["started_at"] == "2026-07-28T12:25:20"
    assert h["offset_label"] == "[00:12]"
    assert h["speaker"] == "[Mic]"
    assert "pricing" in h["line"].lower()
    assert h["context"]


def test_search_single_source_speaker_label(sessions):
    out = mcp_server.search_transcripts("migration")
    assert out["count"] == 1
    assert out["hits"][0]["speaker"] == "[System]"
    assert out["hits"][0]["session_id"] == "2026-07-29_09-00-00"


def test_search_no_matches_returns_guidance_not_empty_list(sessions):
    out = mcp_server.search_transcripts("zzznomatchxyz")
    assert out["count"] == 0
    assert out["hits"] == []
    assert "guidance" in out
    assert "list_sessions" in out["guidance"]
    assert "get_session" in out["guidance"]


def test_search_scoped_to_session_ids(sessions):
    out = mcp_server.search_transcripts("the", session_ids=["2026-07-29"])
    assert all(h["session_id"] == "2026-07-29_09-00-00" for h in out["hits"])


def test_search_unresolved_session_ids_returns_guidance(sessions):
    out = mcp_server.search_transcripts("migration", session_ids=["totally-missing"])
    assert out["count"] == 0
    assert "guidance" in out


def test_search_indexes_lazily_on_first_call(sessions):
    # No explicit ensure_indexed() before searching — search_transcripts must
    # build the index itself.
    from rec import index as index_mod
    assert not index_mod.index_path().exists()
    out = mcp_server.search_transcripts("client")
    assert out["count"] >= 1
    assert index_mod.index_path().exists()


def test_search_limit_is_respected(sessions):
    out = mcp_server.search_transcripts("the", limit=1)
    assert len(out["hits"]) <= 1


# ---- server construction + schema-collapse guard (correction #4) -----------
#
# The v2 SDK generates an EMPTY input schema silently if a tool's signature
# collapses to (*args, **kwargs) (e.g. the decorator couldn't read the type
# hints). The server still starts, the client sees a parameterless tool, and
# every call fails opaquely. This guard pins that no registered tool has an
# empty schema.


def _registered_tools():
    """Build the server and return its registered tool definitions."""
    import asyncio
    mcp = mcp_server.build_server()
    return asyncio.run(mcp.list_tools())


def test_server_registers_exactly_three_tools():
    tools = _registered_tools()
    names = sorted(t.name for t in tools)
    assert names == ["get_session", "list_sessions", "search_transcripts"]


def test_all_tools_are_marked_read_only():
    for t in _registered_tools():
        assert t.annotations is not None, f"{t.name} missing annotations"
        assert t.annotations.read_only_hint is True, f"{t.name} not read-only"


def test_no_tool_signature_collapsed_to_empty_schema():
    """Every tool must expose real parameters (correction #4 guard).

    A signature that collapsed to (*args, **kwargs) yields an empty input
    schema; the SDK still starts the server but the tool is uncallable.
    """
    for t in _registered_tools():
        props = (t.input_schema or {}).get("properties", {})
        assert props, (
            f"tool {t.name!r} has an empty input schema — its signature likely "
            f"collapsed to (*args, **kwargs). input_schema={t.input_schema!r}"
        )


def test_tool_descriptions_mention_the_discovery_chain():
    """The docstrings (model-visible) must teach list_sessions → get_session."""
    tools = {t.name: t for t in _registered_tools()}
    # Each description should reference the chain so the model learns the flow.
    ls_desc = tools["list_sessions"].description or ""
    gs_desc = tools["get_session"].description or ""
    st_desc = tools["search_transcripts"].description or ""
    assert "get_session" in ls_desc  # list_sessions points at get_session
    assert "list_sessions" in gs_desc or "search" in gs_desc.lower()
    # search_transcripts must tell the model to pass keywords, not questions.
    assert "keyword" in st_desc.lower()


def test_tool_functions_have_introspectable_signatures():
    """The underlying functions (not just the server bindings) keep real params."""
    for name in ("list_sessions", "get_session", "search_transcripts"):
        fn = getattr(mcp_server, name)
        sig = inspect.signature(fn)
        kinds = {p.kind for p in sig.parameters.values()}
        collapsed = kinds <= {inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD}
        assert not collapsed, f"{name} signature collapsed to *args/**kwargs"
        assert len(sig.parameters) >= 1, f"{name} has no parameters"


# ---- stdout purity (correction #5) ----------------------------------------
#
# MCP stdio puts JSON-RPC on stdout; any stray stdout from inside the server
# corrupts the wire. This audits the module's source + import graph for the
# usual offenders. The real end-to-end check is test_handshake_stdout_is_pure.


def test_mcp_server_module_does_not_import_recorder():
    """The read-only server must not import the recording machinery."""
    src = Path(mcp_server.__file__).read_text(encoding="utf-8")
    # The only modules it should import from rec are read-side ones.
    forbidden = ["recorder", "transcriber", "audio_check"]
    for bad in forbidden:
        assert f"from . import {bad}" not in src, f"mcp_server imports {bad} (not read-only)"
        assert f"import {bad}" not in src, f"mcp_server imports {bad} (not read-only)"


def test_mcp_server_source_has_no_stdout_writes():
    """No print/click.echo/rich-stdout calls in the module — stdout is MCP's.

    We match the call forms (with parens) so docstring mentions like
    "no `print`, no `click.echo`" don't trip a false positive.
    """
    src = Path(mcp_server.__file__).read_text(encoding="utf-8")
    # `print(` writes to stdout; `click.echo(` likewise; a bare `Console(`
    # defaults to stdout (rich's default stream). All forbidden in a stdio MCP
    # server, where stdout carries the JSON-RPC wire protocol.
    assert "print(" not in src, "mcp_server uses print() — stdout is the MCP wire"
    assert "click.echo(" not in src, "mcp_server uses click.echo() — stdout is the MCP wire"
    assert "Console(" not in src, "mcp_server constructs a rich Console — stdout risk"


def test_run_configures_logging_when_invoked_directly(monkeypatch, xdg):
    """`python -m rec.mcp_server` (which skips cli.main) must still set up logging.

    Regression: run() is a documented entry point but used to rely on cli.main
    having called configure_logging first. Invoked directly, no global log file
    was created and no RichHandler was attached.
    """
    import rec.log as log_mod
    from rec import mcp_server as ms

    configured = {}
    monkeypatch.setattr(log_mod, "configure_logging",
                        lambda ctx, **kw: configured.setdefault("ctx", ctx))
    monkeypatch.setattr(log_mod, "set_command_context",
                        lambda cmd: configured.setdefault("cmd", cmd))
    # build_server() imports the SDK (heavy); run() then calls mcp.run() which
    # blocks on the stdio loop. Stub both so we only assert the logging setup.
    fake_mcp = type("FakeMCP", (), {"run": staticmethod(lambda: None)})()
    monkeypatch.setattr(ms, "build_server", lambda: fake_mcp)
    ms.run()
    assert configured.get("ctx") == "cli"
    assert configured.get("cmd") == "mcp"


def test_handshake_stdout_is_pure_jsonrpc(xdg, sessions):
    """End-to-end: stdout of `rec mcp` over a real handshake is ONLY JSON-RPC.

    Spawns `rec mcp` (well, the server module) as a subprocess, runs an
    initialize + tools/list + tools/call cycle using the MCP client SDK, and
    asserts every line on stdout parses as a JSON-RPC message. Any stray print
    or stdout-side log would fail this.
    """
    import asyncio
    import os

    from mcp.client.session import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    async def run():
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "rec.mcp_server"],
            # Inherit the test's redirected XDG roots so the subprocess sees the
            # fixture sessions.
            env={**os.environ},
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as s:
                init = await s.initialize()
                assert init.server_info.name == "call-copilot"
                tools = await s.list_tools()
                names = sorted(t.name for t in tools.tools)
                assert names == ["get_session", "list_sessions", "search_transcripts"]
                # Drive a real search to exercise the read path end to end.
                res = await s.call_tool("search_transcripts", {"query": "client pricing"})
                assert res.is_error is False
                # The result text is JSON the model can read.
                assert res.content

    asyncio.run(run())


# ---- read-only helper coverage --------------------------------------------


def test_session_source_helper(xdg):
    _make_session("s1", "x", has_sys=True, has_mic=True)
    _make_session("s2", "x", has_sys=False, has_mic=True)
    _make_session("s3", "x", has_sys=True, has_mic=False)
    _make_session("s4", None, has_sys=False, has_mic=False)
    assert mcp_server._session_source("s1") == "both"
    assert mcp_server._session_source("s2") == "mic"
    assert mcp_server._session_source("s3") == "system"
    assert mcp_server._session_source("s4") == "system"  # default


def test_offset_label_formats_seconds():
    assert mcp_server._offset_label(0.0) == "[00:00]"
    assert mcp_server._offset_label(63.0) == "[01:03]"
    assert mcp_server._offset_label(3723.0) == "[1:02:03]"
    assert mcp_server._offset_label(None) is None


def test_speaker_label_normalizes():
    assert mcp_server._speaker_label("Mic") == "[Mic]"
    assert mcp_server._speaker_label("System") == "[System]"
    assert mcp_server._speaker_label(None) is None
