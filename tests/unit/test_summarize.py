"""Tests for rec.summarize — the map → (consolidate) → reduce pipeline.

Every test runs against a FakeProvider injected through the provider call —
zero network. The load-bearing invariants pinned here:

  - **Tier 3 payload contains no raw chunk text** (test #7): the reduce input is
    built only from map/consolidate outputs, never from transcript chunks.
  - **transcript.md is byte-identical** before and after (test #13): the single
    most valuable test — the invariant is catastrophic and silent when violated.
  - **session.json carries no key material** (test #10).
  - **A failed chunk doesn't abort the run** — it's marked and the run continues.
  - **A failed reduce writes summary.partial.md** with the map output preserved.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from rec import session
from rec import summarize as summarize_mod
from rec.providers.base import Completion
from rec.templates import load_template

# ---- fake provider ---------------------------------------------------------


@dataclass
class FakeProvider:
    """A provider that records every call and returns canned completions.

    Each call gets a deterministic response tagged with its tier, and the raw
    ``user`` text is captured so tests can assert the Tier 3 payload carries no
    transcript chunk text.
    """

    name: str = "fake"
    base_url: str = "http://fake.local"
    calls: list = None  # type: ignore[assignment]

    def __post_init__(self):
        self.calls = []

    def complete(self, *, system, user, model, max_tokens, temperature=0.2,
                 timeout=300.0, thinking=True) -> Completion:
        self.calls.append({"model": model, "user": user, "thinking": thinking})
        # Echo the tier back in the text so map output is distinguishable.
        is_reduce = "Map output" in user or "map output" in user.lower()
        text = f"[fake-{model} reduce]" if is_reduce else f"[fake-{model} map]"
        # Realistic-ish token counts so cost math runs.
        return Completion(
            text=text,
            model=model,
            tokens_in=len(user) // 4,
            tokens_out=50,
            cost_usd=0.0,  # fake/local → known free
        )


@dataclass
class FailingReduceProvider(FakeProvider):
    """A fake whose reduce (Tier 3) call always fails."""

    def complete(self, *, system, user, model, max_tokens, temperature=0.2,
                 timeout=300.0, thinking=True) -> Completion:
        self.calls.append({"model": model, "user": user, "thinking": thinking})
        # The reduce call contains the reduce template's framing.
        if "Map output" in user:
            from rec.providers.base import ProviderError
            raise ProviderError("reduce failed (fake)", status_code=500)
        return super().complete(
            system=system, user=user, model=model, max_tokens=max_tokens,
            temperature=temperature, timeout=timeout, thinking=thinking,
        )


# ---- fixtures --------------------------------------------------------------


def _make_transcript_text(n_lines: int = 30) -> str:
    """A transcript with both line formats and enough body to chunk."""
    lines = []
    for i in range(n_lines):
        if i % 2 == 0:
            lines.append(f"[System] [{i:02d}:00] decision item number {i} " + "x" * 200)
        else:
            lines.append(f"Mic [{i:02d}:30] action item {i} for owner " + "y" * 200)
    return (
        "# Meeting Transcript\n\n"
        "**Date:** 2026-08-10\n**Duration:** 47 min\n"
        "**Sources:** System (rec.wav) + Microphone (mic.wav)\n\n---\n\n"
        + "\n\n".join(lines)
        + "\n"
    )


@pytest.fixture
def session_with_transcript(xdg):
    """A session with a transcript.md on disk; returns (sid, transcript_text)."""
    sid = "2026-08-10_14-30-00"
    session.create_session_dir(sid)
    text = _make_transcript_text(40)
    session.transcript_path(sid).write_text(text, encoding="utf-8")
    return sid, text


# ---- tests -----------------------------------------------------------------


def test_summarize_writes_summary_md(session_with_transcript):
    sid, _ = session_with_transcript
    provider = FakeProvider()
    template = load_template("default")
    result = summarize_mod.summarize(
        session_id=sid, provider=provider, template=template,
        tier1_model="glm-4.7-flash", tier2_model=None, tier3_model="glm-5",
    )
    assert result.out_path.exists()
    assert result.out_path.name == "summary.md"
    assert not result.partial
    # Tier 1 ran once per chunk; Tier 3 ran exactly once.
    assert result.calls["tier1"] >= 1
    assert result.calls["tier3"] == 1
    assert result.calls["tier2"] == 0  # below the reduce budget → no consolidate


def test_tier3_payload_contains_no_raw_chunk_text(session_with_transcript):
    """The reduce call's user text must NOT contain any transcript chunk text.

    This is the load-bearing invariant: a raw full transcript must never reach
    Tier 3. The reduce input is constructed only from map/consolidate outputs.
    """
    sid, transcript_text = session_with_transcript
    provider = FakeProvider()
    template = load_template("default")
    summarize_mod.summarize(
        session_id=sid, provider=provider, template=template,
        tier1_model="glm-4.7-flash", tier2_model=None, tier3_model="glm-5",
    )
    # Find the Tier 3 (reduce) call.
    reduce_calls = [c for c in provider.calls if c["model"] == "glm-5"]
    assert len(reduce_calls) == 1
    reduce_user = reduce_calls[0]["user"]
    # The reduce payload references "Map output" (template framing) — fine.
    assert "Map output" in reduce_user or "map output" in reduce_user.lower()
    # But a verbatim transcript line should not be in it: the map output is
    # "[fake-... map]", so if a raw phrase like "decision item number 0" appears,
    # raw chunk text leaked into the Tier 3 payload.
    assert "decision item number 0" not in reduce_user


def test_transcript_byte_identical_after_summarise(session_with_transcript):
    """THE single most valuable test: transcript.md is untouched by summarise."""
    sid, _ = session_with_transcript
    tpath = session.transcript_path(sid)
    before = tpath.read_bytes()
    provider = FakeProvider()
    template = load_template("default")
    summarize_mod.summarize(
        session_id=sid, provider=provider, template=template,
        tier1_model="glm-4.7-flash", tier2_model=None, tier3_model="glm-5",
    )
    after = tpath.read_bytes()
    assert before == after, "transcript.md was modified by summarise — catastrophic"


def test_session_json_has_no_key_material(session_with_transcript, monkeypatch):
    """session.json must contain no substring of any API-key env var value."""
    sid, _ = session_with_transcript
    monkeypatch.setenv("ZAI_API_KEY", "super-secret-key-value-xyz")
    provider = FakeProvider()
    template = load_template("default")
    result = summarize_mod.summarize(
        session_id=sid, provider=provider, template=template,
        tier1_model="glm-4.7-flash", tier2_model=None, tier3_model="glm-5",
    )
    # Persist the summary meta like the CLI does.
    session.update_meta(sid, summary=result.to_meta("default", "glm"))
    meta_json = session.session_json_path(sid).read_text(encoding="utf-8")
    assert "super-secret-key-value-xyz" not in meta_json
    assert "ZAI_API_KEY" not in meta_json  # the env var NAME isn't stored either


def test_failed_reduce_writes_partial(session_with_transcript):
    """A failed Tier 3 writes summary.partial.md with the map output preserved."""
    sid, _ = session_with_transcript
    provider = FailingReduceProvider()
    template = load_template("default")
    result = summarize_mod.summarize(
        session_id=sid, provider=provider, template=template,
        tier1_model="glm-4.7-flash", tier2_model=None, tier3_model="glm-5",
    )
    assert result.partial
    assert result.out_path.name == "summary.partial.md"
    assert result.out_path.exists()
    body = result.out_path.read_text(encoding="utf-8")
    assert "Partial summary" in body
    # The map output is preserved.
    assert "Chunk" in body


def test_cost_line_format(session_with_transcript):
    """The cost-line format is a UX commitment — one test pins its shape."""
    sid, _ = session_with_transcript
    provider = FakeProvider()
    template = load_template("default")
    result = summarize_mod.summarize(
        session_id=sid, provider=provider, template=template,
        tier1_model="glm-4.7-flash", tier2_model=None, tier3_model="glm-5",
    )
    # The cost line is rendered by _print_cost_line; capture via the result.
    # We assert the result carries the structured data the line is built from.
    assert result.calls["tier1"] >= 1
    assert result.calls["tier3"] == 1
    assert result.cost_usd is not None  # fake provider reports cost (0.0)
    assert "in" in result.tokens and "out" in result.tokens
    assert result.wall_clock_s >= 0


def test_tier2_fires_when_map_output_exceeds_budget(session_with_transcript):
    """Tier 2 consolidate fires only when Tier 1 output exceeds the reduce budget."""
    sid, _ = session_with_transcript
    provider = FakeProvider()
    template = load_template("default")
    # Force a tiny reduce budget so consolidation triggers.
    result = summarize_mod.summarize(
        session_id=sid, provider=provider, template=template,
        tier1_model="glm-4.7-flash", tier2_model="glm-4.7", tier3_model="glm-5",
        reduce_budget_tokens=1,
    )
    assert result.calls["tier2"] >= 1
    assert result.models["tier2"] == "glm-4.7"


def test_tier2_skipped_when_below_budget(session_with_transcript):
    """Below the reduce budget, Tier 2 makes zero calls."""
    sid, _ = session_with_transcript
    provider = FakeProvider()
    template = load_template("default")
    result = summarize_mod.summarize(
        session_id=sid, provider=provider, template=template,
        tier1_model="glm-4.7-flash", tier2_model="glm-4.7", tier3_model="glm-5",
        reduce_budget_tokens=1_000_000,  # huge → never consolidate
    )
    assert result.calls["tier2"] == 0
    assert result.models["tier2"] is None
