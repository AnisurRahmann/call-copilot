"""Summarisation orchestration: chunk → map → (consolidate) → reduce → write.

The pipeline across three model tiers:

  **Tier 1 — map** (per chunk, cheap model): extracts decisions, action items,
  open questions, speaker-attributed notes from each chunk. 70–80% of tokens.

  **Tier 2 — consolidate** (optional, mid model): fires only when Tier 1 output
  exceeds the reduce budget (default 12k estimated tokens). Densifies the map
  output into fewer blocks so Tier 3 stays cheap. Below budget → zero calls.

  **Tier 3 — reduce** (exactly one call, expensive model): synthesises the
  Tier 1/2 output into the final summary. A raw full transcript NEVER reaches
  Tier 3 — the reduce input is constructed only from map/consolidate outputs.

The summary is written to ``sessions/{id}/summary.md``; ``transcript.md`` is
never touched. On a failed reduce, ``summary.partial.md`` keeps the map output
so paid tokens aren't lost.

Costs are aggregated from each :class:`~rec.providers.base.Completion` and
returned in :attr:`SummaryResult`. The CLI renders the cost line; nothing here
prints.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import chunking, session
from .log import get_logger
from .providers import Provider, pricing
from .providers.base import ProviderError
from .templates import Template

log = get_logger(__name__)

# When Tier 1 map output exceeds this many estimated tokens, run a Tier 2
# consolidate pass so Tier 3's single reduce call stays bounded.
REDUCE_BUDGET_TOKENS = 12_000
TIER1_MAX_TOKENS = 2000
TIER2_MAX_TOKENS = 4000
TIER3_MAX_TOKENS = 4000


@dataclass
class TierCall:
    """One completed model call, for cost aggregation."""

    tier: str  # "tier1" / "tier2" / "tier3"
    model: str
    tokens_in: int
    tokens_out: int
    cost_usd: float | None
    text: str
    failed: bool = False


@dataclass
class SummaryResult:
    """The outcome of a summarise run — costs, calls, and the text/path."""

    summary_text: str
    out_path: Path
    models: dict[str, str | None]
    calls: dict[str, int]
    tokens: dict[str, int]
    cost_usd: float | None
    cost_estimated: bool
    wall_clock_s: float
    partial: bool = False
    # Per-call detail for debugging (never logged with text content).
    tier_calls: list[TierCall] = field(default_factory=list)

    def to_meta(self, template_name: str, provider_name: str) -> dict:
        """The block stored under ``session.meta.summary`` on success."""
        return {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "template": template_name,
            "provider": provider_name,
            "models": self.models,
            "calls": self.calls,
            "tokens": self.tokens,
            "cost_usd": self.cost_usd,
            "cost_estimated": self.cost_estimated,
            "wall_clock_s": round(self.wall_clock_s, 1),
        }


def summarize(
    *,
    session_id: str,
    provider: Provider,
    template: Template,
    tier1_model: str,
    tier2_model: str | None,
    tier3_model: str,
    reduce_budget_tokens: int = REDUCE_BUDGET_TOKENS,
) -> SummaryResult:
    """Run the map → (consolidate) → reduce pipeline for one session.

    Reads ``transcript.md``, writes ``summary.md`` (or ``summary.partial.md`` on
    a failed reduce). Never throws on a per-chunk failure — a failed chunk is
    marked and the run continues. A failed *reduce* still returns a result with
    ``partial=True``.
    """
    started = time.monotonic()
    transcript_path = session.transcript_path(session_id)
    transcript = transcript_path.read_text(encoding="utf-8")
    chunks = chunking.chunk_transcript(transcript)
    if not chunks:
        raise ValueError(f"No transcript lines to summarise in {session_id}.")

    calls: list[TierCall] = []

    # --- Tier 1: map pass, one call per chunk ---
    map_outputs: list[str] = []
    for chunk in chunks:
        user = template.fill_map(chunk.text)
        tc = _call(
            provider, tier="tier1", model=tier1_model, user=user,
            max_tokens=TIER1_MAX_TOKENS, thinking=False,
            chunk_label=f"chunk {chunk.index + 1}/{len(chunks)}",
        )
        calls.append(tc)
        if tc.failed:
            map_outputs.append(f"[chunk {chunk.index + 1} unavailable]")
        else:
            map_outputs.append(tc.text)

    combined_map = "\n\n---\n\n".join(
        f"## Chunk {i + 1}\n\n{txt}" for i, txt in enumerate(map_outputs)
    )

    # --- Tier 2: consolidate, only if Tier 1 output exceeds the reduce budget ---
    reduce_input = combined_map
    models = {"tier1": tier1_model, "tier2": None, "tier3": tier3_model}
    if tier2_model and pricing.estimate_tokens(combined_map) > reduce_budget_tokens:
        user = template.fill_reduce(combined_map)  # consolidate reuses reduce framing
        # For consolidation we frame it as a densification, not a final summary.
        user = (
            "Consolidate the following per-chunk notes into fewer, denser blocks, "
            "dropping duplicates. Keep all timestamps.\n\n" + combined_map
        )
        tc = _call(
            provider, tier="tier2", model=tier2_model, user=user,
            max_tokens=TIER2_MAX_TOKENS, thinking=True,
            chunk_label="consolidate",
        )
        calls.append(tc)
        models["tier2"] = tier2_model
        reduce_input = tc.text if not tc.failed else combined_map

    # --- Tier 3: reduce, exactly one call, over map/consolidate output ONLY ---
    # The reduce input is NEVER raw chunk text — it's built from Tier 1/2 outputs.
    reduce_user = template.fill_reduce(reduce_input)
    tc = _call(
        provider, tier="tier3", model=tier3_model, user=reduce_user,
        max_tokens=TIER3_MAX_TOKENS, thinking=True,
        chunk_label="reduce",
    )
    calls.append(tc)

    partial = tc.failed
    if partial:
        summary_text = _partial_summary(combined_map, calls)
        out_path = _write_partial(session_id, summary_text)
    else:
        summary_text = tc.text
        out_path = _write_summary(session_id, summary_text)

    elapsed = time.monotonic() - started
    return _build_result(
        summary_text=summary_text, out_path=out_path, models=models,
        calls=calls, elapsed=elapsed, partial=partial,
    )


def _call(
    provider: Provider, *, tier: str, model: str, user: str,
    max_tokens: int, thinking: bool, chunk_label: str,
) -> TierCall:
    """Make one provider call, returning a TierCall (failed=True on error)."""
    system = "You are a meeting summarizer."
    try:
        c = provider.complete(
            system=system, user=user, model=model, max_tokens=max_tokens,
            thinking=thinking,
        )
        log.info(
            "summarize %s ok model=%s tokens_in=%d tokens_out=%d",
            chunk_label, model, c.tokens_in, c.tokens_out,
        )
        return TierCall(
            tier=tier, model=model, tokens_in=c.tokens_in, tokens_out=c.tokens_out,
            cost_usd=c.cost_usd, text=c.text,
        )
    except (ProviderError, Exception) as e:
        # An auth error (401/403) means the key is bad — retrying the remaining
        # chunks with the same key is a waste. Re-raise so the CLI surfaces it as
        # one human line, non-zero exit. Other failures (500/timeout/network) are
        # retried inside the transport; if they still fail after retries, mark the
        # chunk failed and continue (the reduce pass will see the gap).
        if isinstance(e, ProviderError) and e.status_code in (401, 403):
            log.warning("summarize %s AUTH FAILED (status=%s): aborting run", chunk_label, e.status_code)
            raise
        log.warning("summarize %s FAILED: %r", chunk_label, e)
        return TierCall(
            tier=tier, model=model, tokens_in=0, tokens_out=0,
            cost_usd=None, text="", failed=True,
        )


def _partial_summary(combined_map: str, calls: list[TierCall]) -> str:
    """Build a partial-summary file body when the reduce pass failed."""
    failed_tiers = sorted({c.tier for c in calls if c.failed})
    note = (
        "# Partial summary (reduce pass failed)\n\n"
        f"The final reduce pass did not complete (failed tiers: {', '.join(failed_tiers)}). "
        "The per-chunk map output below is preserved so the tokens already spent aren't lost.\n\n"
        "---\n\n"
    )
    return note + combined_map


def _write_summary(session_id: str, text: str) -> Path:
    path = session.session_dir(session_id) / "summary.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    log.info("summary written: %s (%d bytes)", path, len(text))
    return path


def _write_partial(session_id: str, text: str) -> Path:
    path = session.session_dir(session_id) / "summary.partial.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    log.info("partial summary written: %s (%d bytes)", path, len(text))
    return path


def _build_result(
    *, summary_text: str, out_path: Path, models: dict[str, str | None],
    calls: list[TierCall], elapsed: float, partial: bool,
) -> SummaryResult:
    """Aggregate per-call costs into a SummaryResult."""
    call_counts = {"tier1": 0, "tier2": 0, "tier3": 0}
    tokens_in = 0
    tokens_out = 0
    total_cost: float | None = 0.0
    any_estimated = False
    any_unknown = False

    for c in calls:
        call_counts[c.tier] = call_counts.get(c.tier, 0) + 1
        tokens_in += c.tokens_in
        tokens_out += c.tokens_out
        if c.cost_usd is None:
            any_unknown = True
        else:
            total_cost = (total_cost or 0.0) + c.cost_usd

    # If any call had an unknown cost, the aggregate is unknown.
    if any_unknown:
        total_cost = None
    # If any call fell back to an estimated token count (cost_usd computed from
    # an estimate rather than reported usage), mark the line estimated.
    if any(c.tokens_in == 0 and c.tier != "" and not c.failed for c in calls):
        any_estimated = True

    return SummaryResult(
        summary_text=summary_text,
        out_path=out_path,
        models=models,
        calls=call_counts,
        tokens={"in": tokens_in, "out": tokens_out},
        cost_usd=total_cost,
        cost_estimated=any_estimated,
        wall_clock_s=elapsed,
        partial=partial,
        tier_calls=calls,
    )
