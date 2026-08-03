"""Tests for rec.recorder — the audiotap-based recording daemon.

A fake `audiotap.SystemTap` invokes the audio callback with synthetic float32
PCM, letting us verify the daemon writes a real WAV and stops cleanly on
SIGTERM — without any real Core Audio tap or audio device.
"""

from __future__ import annotations

import os
import signal
import sys
import threading
import time
import types

import numpy as np
import pytest

from rec import config, recorder, session

# ---- fake audiotap ---------------------------------------------------------


class _FakeTap:
    """Mimics an audiotap tap: records creation + start, and can feed audio."""

    kind = "system"  # overridden by subclass

    def __init__(self, callback, sample_rate=16000.0, channels=1, **kwargs):
        # `callback` must match the audiotap call convention (recorder passes it
        # as a keyword arg: SystemTap(callback=...)).
        self.callback = callback
        self.sample_rate = sample_rate
        self.channels = channels
        self.kwargs = kwargs
        self.started = False
        _FakeTap._created.setdefault(self.kind, []).append(self)

    def start(self):
        self.started = True

    def stop(self):
        pass

    def destroy(self):
        pass

    def feed(self, frames: int):
        """Test helper: deliver one chunk of `frames` samples to the callback."""
        data = np.ones(frames * self.channels, dtype=np.float32) * 0.5
        self.callback(data.tobytes(), frames, self.channels, 0)

    _created: dict = {}


class FakeSystemTap(_FakeTap):
    kind = "system"


class FakeMicTap(_FakeTap):
    kind = "mic"


def _make_fake_pkg(*, mic_granted: bool):
    """Build a fake `audiotap` module with permission stubs."""
    _FakeTap._created = {}
    fake_pkg = types.ModuleType("audiotap")
    fake_pkg.SystemTap = FakeSystemTap
    fake_pkg.MicTap = FakeMicTap

    class _Perm:
        UNKNOWN, GRANTED, DENIED = 0, 1, 2
    fake_pkg.Permission = _Perm
    fake_pkg.mic_permission_status = lambda: _Perm.GRANTED if mic_granted else _Perm.DENIED
    fake_pkg.request_mic_permission = lambda: _Perm.GRANTED if mic_granted else _Perm.DENIED
    return fake_pkg


@pytest.fixture
def fake_audiotap(monkeypatch):
    """Inject fake audiotap (mic permission GRANTED)."""
    monkeypatch.setitem(sys.modules, "audiotap", _make_fake_pkg(mic_granted=True))
    return sys.modules["audiotap"]


@pytest.fixture
def fake_audiotap_mic_denied(monkeypatch):
    """Like fake_audiotap but mic permission is DENIED (system-only fallback)."""
    monkeypatch.setitem(sys.modules, "audiotap", _make_fake_pkg(mic_granted=False))
    return sys.modules["audiotap"]



# ---- tests -----------------------------------------------------------------


def _run_daemon_with_feeder(session_id, sr, ch, capture="system",
                            feed_frames_per_call=1600, feed_count=10):
    """Spawn run_detached in a thread; feed all created taps; then SIGTERM it."""
    pid = os.getpid()

    def sigterm_self_after_feeding():
        # Wait for taps to be created + started, then feed each, then stop.
        for _ in range(50):
            taps = [t for ts in _FakeTap._created.values() for t in ts]
            if taps and all(t.started for t in taps):
                break
            time.sleep(0.01)
        for ts in _FakeTap._created.values():
            for tap in ts:
                for _ in range(feed_count):
                    tap.feed(feed_frames_per_call)
                    time.sleep(0.005)
        os.kill(pid, signal.SIGTERM)

    feeder = threading.Thread(target=sigterm_self_after_feeding, daemon=True)
    feeder.start()
    return recorder.run_detached(session_id, sr, ch, capture), feeder


# ---- tests -----------------------------------------------------------------


def test_measure_true_rate_falls_back_when_silent():
    """If no audio arrives (silent source), fall back to the requested rate."""
    import queue
    q: queue.Queue = queue.Queue()
    rate, chunks = recorder._measure_true_rate(q, fallback=16000, wait_seconds=0.6)
    assert rate == 16000
    assert chunks == []


# The rate-inference *decision* (snap to a common rate, else fall back) is the
# load-bearing logic — it's what prevented the real "11840 Hz mic burst" bug from
# baking a 4x-slow rate into the WAV. We test it directly via the pure helper
# `_snap_to_common_rate` with synthetic measured rates, rather than depending on a
# background feeder thread to hold a steady cadence. A Python thread using
# time.sleep() cannot reliably hold a sample rate on a slow/loaded CI runner (the
# scheduler stretches inter-chunk gaps), which made the old feeder-based tests
# flaky on GitHub Actions macOS runners. The production `_measure_true_rate`
# itself is correct — it divides delivered frames by elapsed wall-time, exactly
# as required for real audiotap (which delivers from a C callback with precise
# Core Audio timing). These tests pin the decision it makes on a given measured rate.


@pytest.mark.parametrize("measured, expected", [
    (48000, 48000),            # exact 48k
    (47800, 48000),            # 48k within 5% tolerance
    (45500, 44100),            # closer to 44.1k
    (32200, 32000),            # 32k
    (15800, 16000),            # 16k within tolerance
    (24000, 24000),            # exact 24k
])
def test_snap_to_common_rate_accepts_near_standard(measured, expected):
    """A measurement near a standard rate snaps to that rate."""
    assert recorder._snap_to_common_rate(measured, fallback=48000) == expected


def test_snap_to_common_rate_rejects_nonstandard_measurement():
    """The real mic bug: a bursty ~11840 Hz measurement must NOT be trusted.

    Reproduces session 2026-07-29_16-29-23: the mic tap measured 11840 Hz during
    a bursty startup while Core Audio ramped up. A naive recorder would bake that
    into the WAV header -> 4x-slow playback. The guard falls back instead.
    """
    # 11840 isn't within tolerance of any common rate -> fall back to fallback.
    assert recorder._snap_to_common_rate(11840, fallback=48000) == 48000


def test_snap_to_common_rate_rejects_partial_drift():
    """A measurement that drifted well off a common rate (e.g. half of 48k due to
    a feeder under-delivering) must fall back, not snap to a wrong rate."""
    # 34302 Hz is exactly what a slow CI runner produced from feeder drift; it is
    # ~15000 Hz away from the nearest common rate (44100) and must fall back.
    assert recorder._snap_to_common_rate(34302, fallback=16000) == 16000


def test_run_detached_writes_float_wav_from_tap(xdg, fake_audiotap):
    """The daemon must convert audiotap's float32 PCM into a real WAV on disk."""
    sid = "2026-07-27_14-30-00"
    session.create_session_dir(sid)

    code, _feeder = _run_daemon_with_feeder(sid, 16000, 1)

    assert code == 0
    wav = session.wav_path(sid)
    assert wav.exists()
    assert wav.stat().st_size > 0

    # The WAV must be readable as float32 by soundfile.
    import soundfile as sf

    with sf.SoundFile(str(wav)) as f:
        assert f.samplerate == 16000
        assert f.channels == 1
        assert f.subtype == "FLOAT"
        data = f.read(dtype="float32")
    # We fed 10 chunks of 1600 frames => 16000 frames total. The written data
    # should be roughly that (allow the drain to miss at most one chunk).
    assert len(data) >= 1600 * 5
    # All fed samples were 0.5 amplitude.
    assert data.max() == pytest.approx(0.5, abs=0.01)

    # session.json reflects a clean stop.
    meta = session.load_meta(sid)
    assert meta.status == session.STATUS_RECORDED


def test_run_detached_marks_recorded_on_error(xdg, monkeypatch):
    """If tap creation fails, the daemon logs + marks the session recorded."""
    sid = "2026-07-27_14-30-00"
    session.create_session_dir(sid)

    def bad_tap(*a, **kw):
        raise RuntimeError("Core Audio refused")

    monkeypatch.setattr(recorder, "_create_system_tap", bad_tap)

    # Run in a thread and SIGTERM to unblock (it raises before the loop).
    pid = os.getpid()
    t = threading.Thread(target=lambda: time.sleep(0.05) or os.kill(pid, signal.SIGTERM), daemon=True)
    t.start()
    code = recorder.run_detached(sid, 16000, 1, "system")
    assert code == 1
    assert session.load_meta(sid).status == session.STATUS_RECORDED


def test_run_detached_rejects_unsupported_capture(xdg, monkeypatch):
    sid = "2026-07-27_14-30-00"
    session.create_session_dir(sid)
    pid = os.getpid()
    t = threading.Thread(target=lambda: time.sleep(0.05) or os.kill(pid, signal.SIGTERM), daemon=True)
    t.start()
    code = recorder.run_detached(sid, 16000, 1, "bogus-mode")
    assert code == 1


def test_parse_capture_modes():
    assert recorder._parse_capture("system") == {"system"}
    assert recorder._parse_capture("mic") == {"mic"}
    assert recorder._parse_capture("mic+system") == {"mic", "system"}
    assert recorder._parse_capture("system+mic") == {"mic", "system"}
    assert recorder._parse_capture("both") == {"mic", "system"}
    with pytest.raises(ValueError):
        recorder._parse_capture("nonsense")


def test_module_entry_point_parses_args(monkeypatch):
    """`python -m rec.recorder` arg parsing wires into run_detached."""
    captured = {}
    monkeypatch.setattr(recorder, "run_detached", lambda sid, sr, ch, cap: captured.update(dict(sid=sid, sr=sr, ch=ch, cap=cap)) or 0)
    code = recorder.main(["--session-id", "X", "--sample-rate", "8000", "--channels", "2", "--capture", "system"])
    assert code == 0
    assert captured == {"sid": "X", "sr": 8000, "ch": 2, "cap": "system"}


# ---- stop / pid management (unchanged logic, still worth covering) --------


def test_stop_recorder_no_pid_file(xdg):
    assert recorder.stop_recorder() == (False, None)


def test_stop_recorder_stale_pid(xdg):
    config.pid_path().parent.mkdir(parents=True, exist_ok=True)
    # A PID that is guaranteed not to exist (very large number).
    config.pid_path().write_text("99999999\n")
    was_alive, pid = recorder.stop_recorder()
    assert was_alive is False
    assert pid == 99999999
    assert not config.pid_path().exists()


def test_stop_recorder_live_then_dies(xdg, monkeypatch):
    """A live-looking pid gets SIGTERM'd; we wait for it to exit."""
    import subprocess

    child = subprocess.Popen(["sleep", "30"], stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        config.pid_path().parent.mkdir(parents=True, exist_ok=True)
        config.pid_path().write_text(f"{child.pid}\n")
        was_alive, pid = recorder.stop_recorder()
        assert was_alive is True
        assert pid == child.pid
        assert not config.pid_path().exists()
        # stop_recorder polls until the process is gone (or timeout), so by here
        # the child has exited. Reap it to be certain, then confirm it's dead.
        child.wait(timeout=5)
        with pytest.raises(ProcessLookupError):
            os.kill(child.pid, 0)
    finally:
        child.kill()
        child.wait(timeout=5)


def test_active_pid_none_when_no_file(xdg):
    assert recorder.active_pid() is None


def test_active_pid_stale_returns_none(xdg):
    config.pid_path().parent.mkdir(parents=True, exist_ok=True)
    config.pid_path().write_text("99999999\n")
    assert recorder.active_pid() is None


# ---- mic + system capture (new) -------------------------------------------


def test_mic_plus_system_writes_two_wavs(xdg, fake_audiotap):
    """Default mic+system capture writes BOTH recording.wav + recording-mic.wav."""
    sid = "2026-07-28_14-30-00"
    session.create_session_dir(sid)

    code, _ = _run_daemon_with_feeder(sid, 16000, 1, capture="mic+system")

    assert code == 0
    sys_wav = session.wav_path(sid)
    mic_wav = session.mic_wav_path(sid)
    assert sys_wav.exists(), "system WAV missing"
    assert mic_wav.exists(), "mic WAV missing"
    # Both taps were created (one each kind).
    assert len(_FakeTap._created.get("system", [])) == 1
    assert len(_FakeTap._created.get("mic", [])) == 1
    # Per-source rates recorded in session.json.
    import json
    meta = json.load(open(session.session_json_path(sid)))
    assert "system_sample_rate" in meta["extra"]
    assert "mic_sample_rate" in meta["extra"]
    assert meta["extra"]["capture"] == "mic+system"


def test_mic_only_writes_single_mic_wav(xdg, fake_audiotap):
    """`--mic-only` (capture='mic') writes recording-mic.wav and no system WAV."""
    sid = "2026-07-28_14-30-00"
    session.create_session_dir(sid)

    code, _ = _run_daemon_with_feeder(sid, 16000, 1, capture="mic")
    assert code == 0
    assert session.mic_wav_path(sid).exists()
    assert not session.wav_path(sid).exists()  # no system recording
    assert len(_FakeTap._created.get("mic", [])) == 1
    assert _FakeTap._created.get("system", []) == []


def test_system_only_writes_single_system_wav(xdg, fake_audiotap):
    """`--system-only` (capture='system') writes recording.wav and no mic WAV."""
    sid = "2026-07-28_14-30-00"
    session.create_session_dir(sid)

    code, _ = _run_daemon_with_feeder(sid, 16000, 1, capture="system")
    assert code == 0
    assert session.wav_path(sid).exists()
    assert not session.mic_wav_path(sid).exists()
    assert _FakeTap._created.get("mic", []) == []


def test_mic_denied_falls_back_to_system_only(xdg, fake_audiotap_mic_denied):
    """If mic permission is DENIED, mic+system records system only (no crash)."""
    sid = "2026-07-28_14-30-00"
    session.create_session_dir(sid)

    code, _ = _run_daemon_with_feeder(sid, 16000, 1, capture="mic+system")
    assert code == 0
    # System WAV present, mic WAV absent (skipped).
    assert session.wav_path(sid).exists()
    assert not session.mic_wav_path(sid).exists()
    assert _FakeTap._created.get("mic", []) == []
    assert len(_FakeTap._created.get("system", [])) == 1
