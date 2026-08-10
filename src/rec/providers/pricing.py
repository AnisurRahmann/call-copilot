"""Static price table + cost math for summarisation.

USD per 1M tokens, input/output. Sizing estimates use ``len(text)/4`` — no
tokenizer dependency — and are for *sizing only*; real cost always comes from
the provider's reported ``usage`` block.

Rules (load-bearing):
  - Unknown model → ``cost(...)`` returns ``None``. The cost line then reads
    "cost unknown (model not in price table)". **Never** return ``0.0`` for a
    model you can't price — that's a lie.
  - Only Ollama / local models get a true ``0.0`` (see :func:`zero_cost`).

``PRICING_UPDATED`` is the date the table was last verified. ``is_stale()``
returns True past 180 days so a caller can print a freshness note.
"""

from __future__ import annotations

from datetime import date

PRICING_UPDATED = "2026-08-10"
_STALE_AFTER_DAYS = 180

# USD per 1M tokens. (input_per_1m, output_per_1m).
# GLM models are the maintainer's path; the others cover the built-in presets.
PRICES: dict[str, tuple[float, float]] = {
    # --- GLM (Z.ai) ---
    "glm-4.7-flash": (0.06, 0.40),
    "glm-4.7-flashx": (0.07, 0.40),
    "glm-4.7": (0.40, 1.75),
    "glm-5": (0.60, 1.92),
    "glm-5.1": (0.95, 2.99),
    "glm-5.2": (1.40, 4.40),
    # --- Anthropic (Claude) ---
    "claude-3-5-haiku-20241022": (0.80, 4.00),
    "claude-3-5-sonnet-20241022": (3.00, 15.00),
    "claude-sonnet-4-20250514": (3.00, 15.00),
    # --- Google Gemini ---
    "gemini-2.0-flash": (0.10, 0.40),
    "gemini-2.5-flash": (0.15, 0.60),
    # --- DeepSeek ---
    "deepseek-chat": (0.27, 1.10),
    "deepseek-reasoner": (0.55, 2.19),
}


def cost(model: str, tokens_in: int, tokens_out: int) -> float | None:
    """USD cost for a call, or ``None`` if the model isn't priced.

    ``None`` is distinct from ``0.0``: a None means "we don't know" and the cost
    line says so; ``0.0`` (via :func:`zero_cost`) means "known free" (Ollama).
    """
    rates = PRICES.get(model)
    if rates is None:
        return None
    in_rate, out_rate = rates
    return (tokens_in / 1_000_000.0) * in_rate + (tokens_out / 1_000_000.0) * out_rate


def zero_cost() -> float:
    """A true $0.0 for known-free providers (Ollama / local)."""
    return 0.0


def is_stale(today: date | None = None) -> bool:
    """True if the price table hasn't been verified in 180+ days."""
    today = today or date.today()
    updated = date.fromisoformat(PRICING_UPDATED)
    return (today - updated).days >= _STALE_AFTER_DAYS


def estimate_tokens(text: str) -> int:
    """Rough token estimate for sizing only (real cost uses reported usage)."""
    return len(text) // 4
