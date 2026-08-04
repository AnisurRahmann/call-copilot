"""Tests for rec.cli — Click CliRunner end-to-end with mocked side effects.

No real audio capture, no model download. The recorder's audiotap dependency
and the transcriber's whisper model are both mocked.
"""

from __future__ import annotations

import pytest
from click.testing import CliRunner

from rec import cli, config, recorder, session
from rec.transcriber import Segment, TranscriptResult

# ---- fixtures --------------------------------------------------------------


@pytest.fixture
def cfg_written(xdg):
    """Write a minimal config so load_config() succeeds."""
    config.save_config(config.default_config())
    return xdg


@pytest.fixture
def fake_transcribe(monkeypatch):
    """Patch the transcribe call inside cli to return canned segments."""
    calls: dict = {}

    def fake(wav, model_name="base", language="en", console=None, vad_filter=False):
        calls["wav"] = str(wav)
        calls["model"] = model_name
        calls["vad"] = vad_filter
        return TranscriptResult(
            segments=[
                Segment(0.0, 12.0, "Hello world."),
                Segment(12.0, 25.0, "Second segment."),
            ],
            duration=25.0,
            language="en",
            language_probability=0.95,
        )

    monkeypatch.setattr(cli.transcriber, "transcribe", fake)
    return calls


@pytest.fixture
def fake_audio_levels(monkeypatch):
    """Patch audio_check to report a healthy (non-silent) recording."""
    from rec.audio_check import AudioLevels

    monkeypatch.setattr(
        cli.audio_check,
        "analyze_wav",
        lambda wav: AudioLevels(peak=0.42, rms=0.08, frames=16000 * 25, sample_rate=16000, silent=False),
    )


# ---- setup -----------------------------------------------------------------


def test_setup_too_old_macos(monkeypatch, xdg):
    monkeypatch.setattr(cli.platform, "mac_ver", lambda: ("13.5.0", "", ""))
    res = CliRunner().invoke(cli.cli, ["setup"])
    assert res.exit_code != 0
    assert "14.2" in res.output


def test_setup_audiotap_missing(monkeypatch, xdg):
    monkeypatch.setattr(cli.platform, "mac_ver", lambda: ("14.4.0", "", ""))

    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "audiotap":
            raise ImportError("no dylib")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    res = CliRunner().invoke(cli.cli, ["setup"])
    assert res.exit_code != 0
    assert "audiotap" in res.output.lower()


def test_setup_success_saves_config(monkeypatch, xdg):
    monkeypatch.setattr(cli.platform, "mac_ver", lambda: ("14.4.0", "", ""))
    res = CliRunner().invoke(cli.cli, ["setup", "--model", "small"])
    assert res.exit_code == 0, res.output
    assert "Config saved" in res.output
    cfg = config.load_config()
    assert cfg.whisper_model == "small"
    assert cfg.sample_rate == 16000


def test_setup_selftest_can_be_skipped(monkeypatch, xdg):
    """--no-selftest short-circuits the live tap probe (for CI / automation)."""
    monkeypatch.setattr(cli.platform, "mac_ver", lambda: ("14.4.0", "", ""))
    probe_calls: list = []
    monkeypatch.setattr(
        recorder, "probe_capture",
        lambda src, seconds: probe_calls.append((src, seconds)) or None,
    )
    res = CliRunner().invoke(cli.cli, ["setup", "--no-selftest"])
    assert res.exit_code == 0, res.output
    assert "skipped" in res.output.lower()
    assert probe_calls == []  # probe never ran


def test_setup_selftest_reports_silent_source_as_failure(monkeypatch, xdg):
    """A tap that returns near-zero peak is flagged FAIL with restart guidance."""
    from rec.recorder import CaptureProbe
    monkeypatch.setattr(cli.platform, "mac_ver", lambda: ("14.4.0", "", ""))
    # Default capture is mic+system; both probes return silence.
    monkeypatch.setattr(
        recorder, "probe_capture",
        lambda src, seconds: CaptureProbe(source=src, created=True, peak=0.0, frames=1000),
    )
    res = CliRunner().invoke(cli.cli, ["setup", "--selftest-duration", "1"])
    assert res.exit_code == 0, res.output
    assert "FAIL" in res.output
    assert "QUIT" in res.output and "reopen" in res.output
    assert "incomplete" in res.output.lower()


def test_setup_selftest_reports_healthy_source_as_ok(monkeypatch, xdg):
    """A tap that captures real audio reports ok and setup completes."""
    from rec.recorder import CaptureProbe
    monkeypatch.setattr(cli.platform, "mac_ver", lambda: ("14.4.0", "", ""))
    monkeypatch.setattr(
        recorder, "probe_capture",
        lambda src, seconds: CaptureProbe(source=src, created=True, peak=0.4, frames=5000),
    )
    res = CliRunner().invoke(cli.cli, ["setup", "--selftest-duration", "1"])
    assert res.exit_code == 0, res.output
    assert "[ok]" in res.output
    assert "Setup complete" in res.output
    assert "incomplete" not in res.output.lower()


def test_setup_selftest_reports_tap_creation_failure(monkeypatch, xdg):
    """If a tap can't be constructed (e.g. denied), it's a FAIL, not a crash."""
    from rec.recorder import CaptureProbe
    monkeypatch.setattr(cli.platform, "mac_ver", lambda: ("14.4.0", "", ""))
    monkeypatch.setattr(
        recorder, "probe_capture",
        lambda src, seconds: CaptureProbe(source=src, created=False, error="permission denied"),
    )
    res = CliRunner().invoke(cli.cli, ["setup", "--selftest-duration", "1"])
    assert res.exit_code == 0, res.output
    assert "FAIL" in res.output
    assert "permission denied" in res.output


# ---- start -----------------------------------------------------------------


def test_start_blocks_when_already_recording(monkeypatch, cfg_written):
    monkeypatch.setattr(recorder, "active_pid", lambda: 99999)
    res = CliRunner().invoke(cli.cli, ["start"])
    assert res.exit_code != 0
    assert "already recording" in res.output.lower()


def test_start_requires_config(xdg):
    # No config -> helpful error pointing to setup.
    res = CliRunner().invoke(cli.cli, ["start"])
    assert res.exit_code != 0
    assert "rec setup" in res.output.lower()


def test_start_detach_spawns_and_exits(monkeypatch, cfg_written):
    """`rec start --detach` spawns the daemon and returns (the old behavior)."""
    monkeypatch.setattr(recorder, "active_pid", lambda: None)
    spawned: dict = {}
    monkeypatch.setattr(
        recorder,
        "spawn_recorder",
        lambda sid, sr, ch, cap: spawned.setdefault("args", (sid, sr, ch, cap)) or 12345,
    )

    res = CliRunner().invoke(cli.cli, ["start", "--detach"])
    assert res.exit_code == 0, res.output
    assert "Recording started" in res.output

    sid, sr, ch, cap = spawned["args"]
    assert sr == 16000 and ch == 1 and cap == "mic+system"
    meta = session.load_meta(sid)
    assert meta is not None
    assert meta.status == session.STATUS_RECORDING


def test_start_default_enters_live_ui_then_finishes(monkeypatch, cfg_written, fake_transcribe, fake_audio_levels):
    """Default `rec start` spawns, runs the live UI, then stops + transcribes."""
    monkeypatch.setattr(recorder, "active_pid", lambda: None)
    monkeypatch.setattr(recorder, "stop_recorder", lambda: (True, 4321))

    spawned: list = []
    monkeypatch.setattr(
        recorder, "spawn_recorder",
        lambda sid, sr, ch, cap: spawned.append((sid, sr, ch, cap)) or 4321,
    )
    # Stub the live UI so the test doesn't actually block — it records that it
    # was called with the right session, then simulates a clean stop.
    live_calls: list = []
    def fake_live(cfg, sid, *, model_override, vad_filter):
        live_calls.append((sid, model_override, vad_filter))
    monkeypatch.setattr(cli, "_run_live_recording", fake_live)

    res = CliRunner().invoke(cli.cli, ["start", "--model", "small", "--vad"])
    assert res.exit_code == 0, res.output
    assert spawned  # daemon was spawned
    # The live UI was entered (it owns the stop+transcribe; we stub it here and
    # test that flow separately in test_run_live_recording_finishes_on_keyboard_interrupt).
    assert len(live_calls) == 1
    assert live_calls[0][1] == "small"   # --model flowed through
    assert live_calls[0][2] is True      # --vad flowed through


def test_run_live_recording_finishes_on_keyboard_interrupt(monkeypatch, cfg_written, fake_transcribe, fake_audio_levels):
    """The live UI catches Ctrl+C (KeyboardInterrupt) and finishes the session."""
    from rich.console import Console as _C
    monkeypatch.setattr(cli, "console", _C(quiet=True))
    # active_pid returns a pid once (loop body), then the Live context raises
    # KeyboardInterrupt to simulate Ctrl+C.
    calls = {"n": 0}
    def active_pid():
        calls["n"] += 1
        if calls["n"] >= 2:
            raise KeyboardInterrupt
        return 4321
    monkeypatch.setattr(recorder, "active_pid", active_pid)
    monkeypatch.setattr(recorder, "stop_recorder", lambda: (True, 4321))

    sid = session.new_session_id()
    session.update_meta(sid, status=session.STATUS_RECORDING, started_at="2026-07-28T14:00:00")
    session.create_session_dir(sid)
    session.wav_path(sid).write_bytes(b"x")

    cfg = config.load_config()
    cli._run_live_recording(cfg, sid, model_override=None, vad_filter=False)

    # After Ctrl+C the session was transcribed.
    assert session.load_meta(sid).status == session.STATUS_TRANSCRIBED


def test_start_reports_spawn_failure(monkeypatch, cfg_written):
    monkeypatch.setattr(recorder, "active_pid", lambda: None)

    def boom(sid, sr, ch, cap):
        raise RuntimeError("tap creation failed")

    monkeypatch.setattr(recorder, "spawn_recorder", boom)
    res = CliRunner().invoke(cli.cli, ["start", "--detach"])
    assert res.exit_code != 0
    assert "Failed to start recording" in res.output


def test_start_warns_when_mic_permission_missing(monkeypatch, cfg_written):
    """When capture includes the mic but permission isn't GRANTED, warn at start."""
    monkeypatch.setattr(recorder, "active_pid", lambda: None)
    monkeypatch.setattr(recorder, "spawn_recorder", lambda sid, sr, ch, cap: 12345)
    # Simulate mic permission NOT granted.
    monkeypatch.setattr(cli, "_mic_permission_granted", lambda: False)

    res = CliRunner().invoke(cli.cli, ["start", "--detach"])
    assert res.exit_code == 0, res.output
    assert "Microphone permission is not granted" in res.output
    assert "SKIPPED" in res.output


def test_start_no_mic_warning_when_system_only(monkeypatch, cfg_written):
    """--system-only never triggers the mic-permission warning."""
    monkeypatch.setattr(recorder, "active_pid", lambda: None)
    monkeypatch.setattr(recorder, "spawn_recorder", lambda sid, sr, ch, cap: 12345)
    monkeypatch.setattr(cli, "_mic_permission_granted", lambda: False)

    res = CliRunner().invoke(cli.cli, ["start", "--detach", "--system-only"])
    assert res.exit_code == 0, res.output
    assert "Microphone permission is not granted" not in res.output


def test_start_no_mic_warning_when_permission_granted(monkeypatch, cfg_written):
    """Default mic+system capture with permission granted -> no warning."""
    monkeypatch.setattr(recorder, "active_pid", lambda: None)
    monkeypatch.setattr(recorder, "spawn_recorder", lambda sid, sr, ch, cap: 12345)
    monkeypatch.setattr(cli, "_mic_permission_granted", lambda: True)

    res = CliRunner().invoke(cli.cli, ["start", "--detach"])
    assert res.exit_code == 0, res.output
    assert "Microphone permission is not granted" not in res.output


# ---- stop ------------------------------------------------------------------


def test_stop_transcribes_and_keeps_no_device_state(monkeypatch, cfg_written, fake_transcribe, fake_audio_levels):
    sid = session.new_session_id()
    session.update_meta(sid, status=session.STATUS_RECORDING)
    session.create_session_dir(sid)
    session.wav_path(sid).write_bytes(b"RIFF...fake")

    monkeypatch.setattr(recorder, "active_pid", lambda: 4321)
    monkeypatch.setattr(recorder, "stop_recorder", lambda: (True, 4321))

    res = CliRunner().invoke(cli.cli, ["stop"])
    assert res.exit_code == 0, res.output
    assert "Transcript:" in res.output
    # No audio-device restore step anymore (BlackHole gone).
    assert "restore" not in res.output.lower()
    meta = session.load_meta(sid)
    assert meta.status == session.STATUS_TRANSCRIBED
    assert meta.duration == 25.0
    assert meta.word_count == 4  # "Hello world." + "Second segment."
    assert meta.extra.get("audio_silent") is False
    assert session.transcript_path(sid).exists()


def test_stop_merges_mic_plus_system_transcript(monkeypatch, cfg_written, fake_transcribe, fake_audio_levels):
    """When both recording.wav and recording-mic.wav exist, the merged transcript is built."""
    sid = session.new_session_id()
    session.update_meta(sid, status=session.STATUS_RECORDING)
    session.create_session_dir(sid)
    # Both WAVs present => mic+system was captured.
    session.wav_path(sid).write_bytes(b"system")
    session.mic_wav_path(sid).write_bytes(b"mic")

    monkeypatch.setattr(recorder, "active_pid", lambda: 4321)
    monkeypatch.setattr(recorder, "stop_recorder", lambda: (True, 4321))

    res = CliRunner().invoke(cli.cli, ["stop"])
    assert res.exit_code == 0, res.output
    # The merged transcript contains both source labels.
    text = session.transcript_path(sid).read_text()
    assert "[System]" in text and "[Mic]" in text
    assert "**Sources:** System" in text
    # word_count is the sum across both sources (4 + 4 from the fake).
    assert session.load_meta(sid).word_count == 8


def test_stop_warns_on_silent_recording(monkeypatch, cfg_written, fake_transcribe):
    """A silent recording is flagged, NOT transcribed (Whisper hallucinates on silence)."""
    from rec.audio_check import AudioLevels
    monkeypatch.setattr(
        cli.audio_check,
        "analyze_wav",
        lambda wav: AudioLevels(peak=0.0, rms=0.0, frames=16000 * 25, sample_rate=16000, silent=True),
    )

    sid = session.new_session_id()
    session.update_meta(sid, status=session.STATUS_RECORDING)
    session.create_session_dir(sid)
    session.wav_path(sid).write_bytes(b"silent")

    monkeypatch.setattr(recorder, "active_pid", lambda: 4321)
    monkeypatch.setattr(recorder, "stop_recorder", lambda: (True, 4321))

    res = CliRunner().invoke(cli.cli, ["stop"])
    assert res.exit_code == 0, res.output
    assert "silent" in res.output.lower()
    assert session.load_meta(sid).extra.get("audio_silent") is True
    # NEW: transcription is skipped on silence, session marked SILENT.
    assert session.load_meta(sid).status == session.STATUS_SILENT
    assert session.load_meta(sid).word_count == 0
    # Whisper was never called.
    assert "wav" not in fake_transcribe
    assert "Skipping transcription" in res.output
    assert not session.transcript_path(sid).exists()


def test_stop_transcribes_when_system_silent_but_mic_has_audio(monkeypatch, cfg_written, fake_transcribe):
    """mic+system: if the mic captured audio, transcribe it even if system was silent."""
    from rec.audio_check import AudioLevels

    # System WAV is silent, mic WAV is healthy. analyze_wav returns based on path.
    def fake_analyze(wav):
        if "recording-mic" in str(wav):
            return AudioLevels(peak=0.5, rms=0.1, frames=16000 * 25, sample_rate=16000, silent=False)
        return AudioLevels(peak=0.0, rms=0.0, frames=16000 * 25, sample_rate=16000, silent=True)

    monkeypatch.setattr(cli.audio_check, "analyze_wav", fake_analyze)

    sid = session.new_session_id()
    session.update_meta(sid, status=session.STATUS_RECORDING)
    session.create_session_dir(sid)
    session.wav_path(sid).write_bytes(b"silent system")
    session.mic_wav_path(sid).write_bytes(b"good mic")

    monkeypatch.setattr(recorder, "active_pid", lambda: 4321)
    monkeypatch.setattr(recorder, "stop_recorder", lambda: (True, 4321))

    res = CliRunner().invoke(cli.cli, ["stop"])
    assert res.exit_code == 0, res.output
    # Transcription ran (mic had audio) — session is TRANSCRIBED, not SILENT.
    assert session.load_meta(sid).status == session.STATUS_TRANSCRIBED
    assert "Transcript:" in res.output


def test_stop_skips_when_both_mic_and_system_silent(monkeypatch, cfg_written, fake_transcribe):
    """mic+system where BOTH sources are silent -> skip transcription, mark SILENT."""
    from rec.audio_check import AudioLevels
    monkeypatch.setattr(
        cli.audio_check,
        "analyze_wav",
        lambda wav: AudioLevels(peak=0.0, rms=0.0, frames=16000 * 25, sample_rate=16000, silent=True),
    )

    sid = session.new_session_id()
    session.update_meta(sid, status=session.STATUS_RECORDING)
    session.create_session_dir(sid)
    session.wav_path(sid).write_bytes(b"silent system")
    session.mic_wav_path(sid).write_bytes(b"silent mic")

    monkeypatch.setattr(recorder, "active_pid", lambda: 4321)
    monkeypatch.setattr(recorder, "stop_recorder", lambda: (True, 4321))

    res = CliRunner().invoke(cli.cli, ["stop"])
    assert res.exit_code == 0, res.output
    assert session.load_meta(sid).status == session.STATUS_SILENT
    assert "wav" not in fake_transcribe  # Whisper not called


def test_stop_silent_warning_mentions_quit_and_reopen(monkeypatch, cfg_written, fake_transcribe):
    """The zero-frames silence warning must name Screen Recording + quit/reopen."""
    from rec.audio_check import AudioLevels
    monkeypatch.setattr(
        cli.audio_check,
        "analyze_wav",
        lambda wav: AudioLevels(peak=0.0, rms=0.0, frames=16000 * 25, sample_rate=16000, silent=True),
    )
    sid = session.new_session_id()
    session.update_meta(sid, status=session.STATUS_RECORDING)
    session.create_session_dir(sid)
    session.wav_path(sid).write_bytes(b"silent")
    monkeypatch.setattr(recorder, "active_pid", lambda: 4321)
    monkeypatch.setattr(recorder, "stop_recorder", lambda: (True, 4321))

    res = CliRunner().invoke(cli.cli, ["stop"])
    out = res.output.lower()
    assert "quit" in out and "reopen" in out
    assert "screen recording" in out


def test_stop_salvages_when_recorder_already_dead(monkeypatch, cfg_written, fake_transcribe, fake_audio_levels):
    sid = session.new_session_id()
    session.update_meta(sid, status=session.STATUS_RECORDING)
    session.create_session_dir(sid)
    session.wav_path(sid).write_bytes(b"partial")

    monkeypatch.setattr(recorder, "active_pid", lambda: 4321)
    monkeypatch.setattr(recorder, "stop_recorder", lambda: (False, 4321))

    res = CliRunner().invoke(cli.cli, ["stop"])
    assert res.exit_code == 0, res.output
    assert "salvaging" in res.output.lower()


def test_stop_with_no_recording_errors(monkeypatch, cfg_written):
    monkeypatch.setattr(recorder, "active_pid", lambda: None)
    monkeypatch.setattr(recorder, "stop_recorder", lambda: (False, None))
    res = CliRunner().invoke(cli.cli, ["stop"])
    assert res.exit_code != 0
    assert "no active recording" in res.output.lower()


def test_stop_respects_model_override(monkeypatch, cfg_written, fake_transcribe, fake_audio_levels):
    sid = session.new_session_id()
    session.update_meta(sid, status=session.STATUS_RECORDING)
    session.create_session_dir(sid)
    session.wav_path(sid).write_bytes(b"x")

    monkeypatch.setattr(recorder, "active_pid", lambda: 4321)
    monkeypatch.setattr(recorder, "stop_recorder", lambda: (True, 4321))

    res = CliRunner().invoke(cli.cli, ["stop", "--model", "medium"])
    assert res.exit_code == 0, res.output
    assert fake_transcribe["model"] == "medium"
    assert session.load_meta(sid).model == "medium"


def test_stop_vad_off_by_default(monkeypatch, cfg_written, fake_transcribe, fake_audio_levels):
    sid = session.new_session_id()
    session.update_meta(sid, status=session.STATUS_RECORDING)
    session.create_session_dir(sid)
    session.wav_path(sid).write_bytes(b"x")
    monkeypatch.setattr(recorder, "active_pid", lambda: 4321)
    monkeypatch.setattr(recorder, "stop_recorder", lambda: (True, 4321))

    res = CliRunner().invoke(cli.cli, ["stop"])
    assert res.exit_code == 0, res.output
    assert fake_transcribe["vad"] is False  # VAD off by default


def test_stop_vad_flag_enables_it(monkeypatch, cfg_written, fake_transcribe, fake_audio_levels):
    sid = session.new_session_id()
    session.update_meta(sid, status=session.STATUS_RECORDING)
    session.create_session_dir(sid)
    session.wav_path(sid).write_bytes(b"x")
    monkeypatch.setattr(recorder, "active_pid", lambda: 4321)
    monkeypatch.setattr(recorder, "stop_recorder", lambda: (True, 4321))

    res = CliRunner().invoke(cli.cli, ["stop", "--vad"])
    assert res.exit_code == 0, res.output
    assert fake_transcribe["vad"] is True


# ---- list ------------------------------------------------------------------


def test_list_shows_sessions(monkeypatch, cfg_written, xdg):
    session.update_meta("2026-07-27_14-30-00", status=session.STATUS_TRANSCRIBED, duration=2820.0, word_count=4231, model="base")
    session.update_meta("2026-07-24_09-00-00", status=session.STATUS_RECORDED, duration=None, word_count=None)

    res = CliRunner().invoke(cli.cli, ["list"])
    assert res.exit_code == 0, res.output
    assert "2026-07-27 14:30" in res.output
    assert "transcribed" in res.output.lower()
    assert "4,231" in res.output


def test_list_empty(monkeypatch, cfg_written, xdg):
    res = CliRunner().invoke(cli.cli, ["list"])
    assert res.exit_code == 0
    assert "No sessions" in res.output


# ---- status ----------------------------------------------------------------


def test_status_not_recording(monkeypatch, cfg_written):
    monkeypatch.setattr(recorder, "active_pid", lambda: None)
    res = CliRunner().invoke(cli.cli, ["status"])
    assert res.exit_code == 0
    assert "Not recording" in res.output


def test_status_recording(monkeypatch, cfg_written):
    sid = session.new_session_id()
    session.update_meta(sid, status=session.STATUS_RECORDING, started_at="2026-07-27T14:30:00")
    session.create_session_dir(sid)
    session.wav_path(sid).write_bytes(b"x" * 2048)

    monkeypatch.setattr(recorder, "active_pid", lambda: 4321)
    res = CliRunner().invoke(cli.cli, ["status"])
    assert res.exit_code == 0, res.output
    assert "Recording" in res.output
    assert "4321" in res.output
    assert "KB" in res.output


# ---- transcribe ------------------------------------------------------------


def test_transcribe_existing_session(monkeypatch, cfg_written, fake_transcribe, xdg):
    sid = "2026-07-27_14-30-00"
    session.create_session_dir(sid)
    session.wav_path(sid).write_bytes(b"audio bytes")
    session.update_meta(sid, status=session.STATUS_RECORDED)

    res = CliRunner().invoke(cli.cli, ["transcribe", sid])
    assert res.exit_code == 0, res.output
    assert "Transcript:" in res.output
    assert session.load_meta(sid).status == session.STATUS_TRANSCRIBED


def test_transcribe_missing_recording(monkeypatch, cfg_written, xdg):
    sid = "2026-07-27_14-30-00"
    session.create_session_dir(sid)
    res = CliRunner().invoke(cli.cli, ["transcribe", sid])
    assert res.exit_code != 0
    assert "No recording found" in res.output


# ---- diagnose --------------------------------------------------------------


def test_diagnose_writes_bundle_and_resolves_prefix(monkeypatch, cfg_written, xdg):
    sid = "2026-07-27_14-30-00"
    session.create_session_dir(sid)
    session.update_meta(sid, status=session.STATUS_RECORDED)

    # Use a date prefix instead of the full id — diagnose should resolve it.
    res = CliRunner().invoke(cli.cli, ["diagnose", "2026-07-27"])
    assert res.exit_code == 0, res.output
    assert "matched session" in res.output
    out = session.session_dir(sid) / "diagnose.md"
    assert out.exists()
    text = out.read_text()
    assert "# Diagnose bundle" in text
    assert "### session.json" in text


def test_diagnose_unknown_session_errors(cfg_written, xdg):
    res = CliRunner().invoke(cli.cli, ["diagnose", "totally-missing"])
    assert res.exit_code != 0
    assert "No session matches" in res.output


# ---- group / help ---------------------------------------------------------


def test_help_lists_all_commands():
    res = CliRunner().invoke(cli.cli, ["--help"])
    assert res.exit_code == 0
    for cmd in ("setup", "start", "stop", "list", "status", "transcribe", "diagnose"):
        assert cmd in res.output


def test_version():
    res = CliRunner().invoke(cli.cli, ["--version"])
    assert res.exit_code == 0
    assert "0.1.0" in res.output
