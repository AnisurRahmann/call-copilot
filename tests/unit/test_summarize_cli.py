"""Tests for `rec summarize` (CLI) — dry-run, no-provider, and 401 handling.

All offline: the fake provider is injected through the registry; --dry-run makes
zero provider calls; a no-provider config errors with a one-line message.
"""

from __future__ import annotations

import pytest
from click.testing import CliRunner

from rec import cli, config, session


@pytest.fixture
def session_with_transcript(xdg):
    """A config-less XDG root with one transcribed session."""
    config.save_config(config.default_config())  # minimal config, no summarize block
    sid = "2026-08-10_14-30-00"
    session.create_session_dir(sid)
    session.update_meta(sid, started_at="2026-08-10T14:30:00",
                        status=session.STATUS_TRANSCRIBED, duration=600.0, word_count=500)
    transcript = (
        "# Meeting Transcript\n\n**Date:** 2026-08-10\n\n---\n\n"
        + "\n\n".join(f"[System] [{i:02d}:00] decision number {i}." for i in range(20))
        + "\n"
    )
    session.transcript_path(sid).write_text(transcript, encoding="utf-8")
    return sid


def test_summarize_no_provider_errors_with_setup_hint(session_with_transcript):
    """No summarize block configured → one-line error, non-zero exit, no network."""
    res = CliRunner().invoke(cli.cli, ["summarize", session_with_transcript])
    assert res.exit_code != 0
    assert "provider" in res.output.lower()
    assert "Traceback" not in res.output


def test_dry_run_makes_zero_provider_calls(session_with_transcript, monkeypatch):
    """--dry-run prints estimates and never constructs a provider."""
    call_count = {"n": 0}

    def _bomb(*a, **kw):
        call_count["n"] += 1
        raise AssertionError("dry-run must not construct a provider")

    # If make_provider is reached in dry-run, it's a bug.
    monkeypatch.setattr("rec.providers.make_provider", _bomb)
    res = CliRunner().invoke(cli.cli, ["summarize", session_with_transcript, "--dry-run"])
    assert res.exit_code == 0, res.output
    assert call_count["n"] == 0
    assert "Chunks:" in res.output
    assert "zero network" in res.output.lower()


def test_dry_run_works_without_provider_config(session_with_transcript):
    """--dry-run should work even with no provider set (it sizes only)."""
    res = CliRunner().invoke(cli.cli, ["summarize", session_with_transcript, "--dry-run"])
    assert res.exit_code == 0, res.output
    assert "Chunks:" in res.output


def test_provider_401_renders_one_line_no_traceback(session_with_transcript, monkeypatch):
    """A provider 401 surfaces as one human line, non-zero exit, no traceback."""
    from rec.providers.base import ProviderError
    from rec.providers.openai_compat import OpenAICompatProvider

    def _raising_complete(self, **kw):
        raise ProviderError("provider rejected the API key (HTTP 401)", status_code=401)

    # Bypass key resolution + consent so we reach the provider call.
    monkeypatch.setattr("rec.providers.make_provider", lambda **kw: OpenAICompatProvider(
        name="glm", base_url="https://api.z.ai/api/paas/v4", api_key="fake"))
    monkeypatch.setattr(OpenAICompatProvider, "complete", _raising_complete)

    # Write a config with a provider + confirmed_network so consent is skipped.
    cfg = config.load_config()
    cfg.summarize = {"provider": "glm", "confirmed_network": True, "api_key_env": "ZAI_API_KEY"}
    config.save_config(cfg)
    monkeypatch.setenv("ZAI_API_KEY", "fake-key")

    res = CliRunner().invoke(cli.cli, ["summarize", session_with_transcript, "--yes"])
    assert res.exit_code != 0
    assert "401" in res.output or "API key" in res.output
    assert "Traceback" not in res.output


def test_existing_summary_without_force_errors(session_with_transcript):
    """A real (non-dry-run) run without --force on an existing summary errors."""
    sid = session_with_transcript
    session.summary_path(sid).write_text("old summary", encoding="utf-8")
    # No provider configured → the no-provider check runs first. Add a provider
    # so we reach the existing-summary guard.
    cfg = config.load_config()
    cfg.summarize = {"provider": "glm", "confirmed_network": True}
    config.save_config(cfg)
    res = CliRunner().invoke(cli.cli, ["summarize", sid, "--yes"])
    # The existing-summary guard fires before any network call.
    assert res.exit_code != 0
    assert "--force" in res.output
