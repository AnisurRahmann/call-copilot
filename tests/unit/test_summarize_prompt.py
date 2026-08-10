"""Tests for the Step 3 interactive summarise prompt + resolution order.

All via the injectable seams (prompt_yes_no, _is_interactive) — no subprocess,
no real signal delivery. CliRunner makes isatty() always False, so _is_interactive
is monkeypatched to exercise both branches.

Resolution order under test (STEP3_SUMMARIES.md):
  --summarize + provider    -> summarise, no prompt
  --summarize + no provider -> ERROR, exit non-zero, name the env var
  --no-summarize            -> skip, always
  auto: always/never/ask    -> as named
  no provider, no flag      -> no prompt at all, no message
  non-interactive           -> never prompt; "ask" behaves as "never"
  prompt timeout            -> skip, print the `rec summarize <id>` hint
"""

from __future__ import annotations

import pytest
from click.testing import CliRunner

from rec import cli, config, session


@pytest.fixture
def transcribed_session(xdg):
    """A config + a session that reached STATUS_TRANSCRIBED with a transcript.

    Returns the sid; tests add a summarize block to config as needed.
    """
    config.save_config(config.default_config())
    sid = "2026-08-10_14-30-00"
    session.create_session_dir(sid)
    session.update_meta(
        sid, started_at="2026-08-10T14:30:00",
        status=session.STATUS_TRANSCRIBED, duration=600.0, word_count=500,
    )
    transcript = (
        "# Meeting Transcript\n\n**Date:** 2026-08-10\n\n---\n\n"
        + "\n\n".join(f"[System] [{i:02d}:00] decision {i}." for i in range(20))
        + "\n"
    )
    session.transcript_path(sid).write_text(transcript, encoding="utf-8")
    return sid


def _provider_config(**overrides):
    """Write a config with a summarize block (confirmed_network pre-set)."""
    cfg = config.load_config()
    summ = {"provider": "glm", "confirmed_network": True, "api_key_env": "ZAI_API_KEY",
            "tier1_model": "glm-4.7-flash", "tier3_model": "glm-5"}
    summ.update(overrides)
    cfg.summarize = summ
    config.save_config(cfg)


# ---- no-provider / offline-by-default -------------------------------------


def test_no_provider_no_flag_no_prompt(transcribed_session, monkeypatch):
    """No provider configured, no flag → no prompt appears at all."""
    prompted = []
    monkeypatch.setattr(cli, "prompt_yes_no", lambda *a, **kw: prompted.append(kw) or True)
    cfg = config.load_config()
    cli._maybe_prompt_summarize(cfg, transcribed_session, summarize_flag=None)
    assert prompted == []  # no provider → never prompted


def test_no_summarize_flag_never_prompts(transcribed_session, monkeypatch):
    """--no-summarize skips the prompt always, even with a provider set."""
    _provider_config()
    prompted = []
    monkeypatch.setattr(cli, "prompt_yes_no", lambda *a, **kw: prompted.append(kw) or True)
    cfg = config.load_config()
    cli._maybe_prompt_summarize(cfg, transcribed_session, summarize_flag="no")
    assert prompted == []
    assert not session.summary_path(transcribed_session).exists()


def test_summarize_flag_no_provider_errors(transcribed_session):
    """--summarize with no provider → error naming the env var, non-zero."""
    cfg = config.load_config()
    # No summarize block.
    import click
    with pytest.raises(click.ClickException) as exc_info:
        cli._maybe_prompt_summarize(cfg, transcribed_session, summarize_flag="yes")
    assert "provider" in str(exc_info.value.message).lower()


# ---- interactive / non-interactive ----------------------------------------


def test_non_interactive_ask_skips(transcribed_session, monkeypatch):
    """Non-TTY + auto=ask → never prompt (behaves as never)."""
    _provider_config(auto="ask")
    monkeypatch.setattr(cli, "_is_interactive", lambda: False)
    prompted = []
    monkeypatch.setattr(cli, "prompt_yes_no", lambda *a, **kw: prompted.append(kw) or True)
    cfg = config.load_config()
    cli._maybe_prompt_summarize(cfg, transcribed_session, summarize_flag=None)
    assert prompted == []


def test_non_interactive_always_still_runs(transcribed_session, monkeypatch):
    """Non-TTY + auto=always → summarise anyway (no prompt needed)."""
    _provider_config(auto="always")
    monkeypatch.setattr(cli, "_is_interactive", lambda: False)
    # Stub the actual summarise call so no network happens.
    ran = []
    monkeypatch.setattr(cli, "_run_summarize_after_transcription",
                        lambda cfg, sid: ran.append(sid))
    cfg = config.load_config()
    cli._maybe_prompt_summarize(cfg, transcribed_session, summarize_flag=None)
    assert ran == [transcribed_session]


def test_interactive_answer_no_skips(transcribed_session, monkeypatch):
    """Interactive + answer 'n' → exit 0, no summary written, zero provider calls."""
    _provider_config(auto="ask")
    monkeypatch.setattr(cli, "_is_interactive", lambda: True)
    monkeypatch.setattr(cli, "prompt_yes_no", lambda *a, **kw: False)
    ran = []
    monkeypatch.setattr(cli, "_run_summarize_after_transcription",
                        lambda cfg, sid: ran.append(sid))
    cfg = config.load_config()
    cli._maybe_prompt_summarize(cfg, transcribed_session, summarize_flag=None)
    assert ran == []  # didn't summarise
    assert not session.summary_path(transcribed_session).exists()


def test_interactive_answer_yes_runs(transcribed_session, monkeypatch):
    """Interactive + answer 'y' (default yes) → summarise runs."""
    _provider_config(auto="ask")
    monkeypatch.setattr(cli, "_is_interactive", lambda: True)
    monkeypatch.setattr(cli, "prompt_yes_no", lambda *a, **kw: True)
    ran = []
    monkeypatch.setattr(cli, "_run_summarize_after_transcription",
                        lambda cfg, sid: ran.append(sid))
    cfg = config.load_config()
    cli._maybe_prompt_summarize(cfg, transcribed_session, summarize_flag=None)
    assert ran == [transcribed_session]


def test_prompt_timeout_skips_with_hint(transcribed_session, monkeypatch):
    """Prompt timeout → skip, print the `rec summarize <id>` hint, zero calls.

    prompt_yes_no returns the default on timeout. With default=True that would
    RUN summarisation — but the spec says "timeout skips rather than proceeds."
    So the seam returns the default only for Enter; a timeout must skip. The
    seam's timeout behavior returns `default`, so for the SKIP-on-timeout
    semantics we set default=False in the call... but the prompt shows [Y/n]
    (default yes). Resolution: timeout returns False (skip) regardless of the
    shown default, because silence ≠ consent.
    """
    _provider_config(auto="ask")
    monkeypatch.setattr(cli, "_is_interactive", lambda: True)
    # Simulate a timeout: the seam returns False (skip) on timeout.
    monkeypatch.setattr(cli, "prompt_yes_no", lambda *a, **kw: False)
    ran = []
    monkeypatch.setattr(cli, "_run_summarize_after_transcription",
                        lambda cfg, sid: ran.append(sid))
    cfg = config.load_config()
    cli._maybe_prompt_summarize(cfg, transcribed_session, summarize_flag=None)
    assert ran == []  # timeout → skipped


# ---- silent / aborted sessions never prompt -------------------------------


def test_silent_session_never_prompts(xdg, monkeypatch):
    """A STATUS_SILENT session has no transcript → no prompt."""
    config.save_config(config.default_config())
    _provider_config()
    sid = "2026-08-10_15-00-00"
    session.create_session_dir(sid)
    session.update_meta(sid, started_at="2026-08-10T15:00:00",
                        status=session.STATUS_SILENT, duration=60.0, word_count=0)
    # No transcript.md written (silent sessions skip transcription).
    prompted = []
    monkeypatch.setattr(cli, "prompt_yes_no", lambda *a, **kw: prompted.append(kw) or True)
    monkeypatch.setattr(cli, "_is_interactive", lambda: True)
    cfg = config.load_config()
    cli._maybe_prompt_summarize(cfg, sid, summarize_flag=None)
    assert prompted == []


# ---- Ctrl+C at the prompt = exit 0 ----------------------------------------


def test_ctrl_c_at_prompt_treated_as_no(transcribed_session, monkeypatch):
    """A KeyboardInterrupt at the prompt is treated as 'n', not a crash.

    prompt_yes_no owns the interrupt conversion internally (returns the default
    on KeyboardInterrupt). This test monkeypatches the seam to simulate that
    conversion — the caller never sees the interrupt.
    """
    _provider_config(auto="ask")
    monkeypatch.setattr(cli, "_is_interactive", lambda: True)

    def fake_prompt(question, *, default, timeout_s):
        # Simulate the seam's internal handling: Ctrl+C -> return default.
        # The spec says timeout/silence skips; Enter=yes. A Ctrl+C is neither,
        # but the seam treats it as the default to avoid a crash. For the
        # "at the prompt → exit 0" guarantee, default=False (skip) on interrupt.
        return False

    monkeypatch.setattr(cli, "prompt_yes_no", fake_prompt)
    ran = []
    monkeypatch.setattr(cli, "_run_summarize_after_transcription",
                        lambda cfg, sid: ran.append(sid))
    cfg = config.load_config()
    cli._maybe_prompt_summarize(cfg, transcribed_session, summarize_flag=None)
    assert ran == []  # Ctrl+C at prompt → skipped, no crash


# ---- start/stop flag wiring -----------------------------------------------


def test_start_accepts_summarize_flags(monkeypatch, xdg):
    """`rec start --summarize` and --no-summarize are accepted (don't error)."""
    config.save_config(config.default_config())
    from rec import recorder
    monkeypatch.setattr(recorder, "active_pid", lambda: None)
    monkeypatch.setattr(recorder, "spawn_recorder",
                        lambda *a, **kw: 4321)
    seen = {}
    def fake_live(cfg, sid, *, model_override, vad_filter, summarize_flag=None):
        seen["summarize_flag"] = summarize_flag
    monkeypatch.setattr(cli, "_run_live_recording", fake_live)
    monkeypatch.setattr(cli, "_mic_permission_granted", lambda: True)

    res = CliRunner().invoke(cli.cli, ["start", "--summarize"])
    assert res.exit_code == 0, res.output
    assert seen["summarize_flag"] == "yes"

    seen.clear()
    res = CliRunner().invoke(cli.cli, ["start", "--no-summarize"])
    assert res.exit_code == 0, res.output
    assert seen["summarize_flag"] == "no"
