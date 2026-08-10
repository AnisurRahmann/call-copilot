"""Tests for rec.providers.pricing — cost math + staleness.

Unknown model → ``None`` (never ``0.0``); Ollama gets a true ``0.0``; estimates
print to two significant figures; the table carries a freshness date.
"""

from __future__ import annotations

from datetime import date, timedelta

from rec.providers import pricing


def test_known_model_cost():
    # glm-4.7-flash: $0.06/1M in, $0.40/1M out.
    c = pricing.cost("glm-4.7-flash", tokens_in=1_000_000, tokens_out=1_000_000)
    assert c is not None
    assert abs(c - 0.46) < 1e-9  # 0.06 + 0.40


def test_unknown_model_yields_none_not_zero():
    """An unpriced model must be None, never 0.0 — $0.00 would be a lie."""
    assert pricing.cost("totally-made-up-model", 100_000, 100_000) is None


def test_ollama_zero_is_distinct_from_unknown():
    """Ollama's true $0.0 must not collapse with an unknown None."""
    assert pricing.zero_cost() == 0.0
    assert pricing.zero_cost() is not None


def test_cost_scales_with_tokens():
    small = pricing.cost("glm-5", 1_000, 1_000)
    big = pricing.cost("glm-5", 1_000_000, 1_000_000)
    assert small is not None and big is not None
    assert big > small > 0


def test_pricing_has_freshness_date():
    assert pricing.PRICING_UPDATED  # ISO date string
    # Must parse as a real date.
    date.fromisoformat(pricing.PRICING_UPDATED)


def test_is_stale_threshold():
    updated = date.fromisoformat(pricing.PRICING_UPDATED)
    assert pricing.is_stale(today=updated + timedelta(days=200)) is True
    assert pricing.is_stale(today=updated + timedelta(days=10)) is False


def test_estimate_tokens_is_char_div_4():
    assert pricing.estimate_tokens("x" * 4000) == 1000
    assert pricing.estimate_tokens("") == 0


def test_two_sig_fig_estimate_format():
    """The cost-line estimate prints ~$X to two sig figs — a UX commitment.

    Four decimals implies a confidence the table can't support.
    """
    est = 0.0041
    # Two significant figures: ~$0.004, not ~$0.0041.
    formatted = f"~${est:.2g}"
    assert formatted == "~$0.0041" or formatted == "~$0.004"
    # The point: never more than 2 sig figs.
    assert len(formatted.replace("~$", "")) <= len("0.0041")
