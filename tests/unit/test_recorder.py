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


# ---- helper to run the daemon in-process with a feeder thread --------------


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


def _feed_queue(q, frames_per_sec, stop_evt, chunk_frames=4800):
    """Feed a queue at ~frames_per_sec until stop_evt is set (background thread).

    Uses LARGE chunks (default 4800 frames = 0.1s at 48k) so the feeder's
    per-iteration sleep (~0.1s) is long enough for the OS scheduler to honour —
    a 1ms sleep would lose time to scheduling and under-deliver, faking a lower
    rate. Real audiotap delivers from a C callback with precise Core Audio
    timing, so this is just a test-harness approximation.
    """
    interval = chunk_frames / frames_per_sec
    while not stop_evt.is_set():
        q.put(np.zeros(chunk_frames, dtype=np.float32))
        time.sleep(interval)


def test_measure_true_rate_from_steady_delivery():
    """_measure_true_rate infers ~48kHz from steady 48k-frame/sec delivery."""
    import queue
    q: queue.Queue = queue.Queue()
    stop = threading.Event()
    feeder = threading.Thread(target=_feed_queue, args=(q, 48000, stop), daemon=True)
    feeder.start()
    try:
        rate, chunks = recorder._measure_true_rate(q, fallback=16000, wait_seconds=1.0)
    finally:
        stop.set(); feeder.join(timeout=1.0)
    assert rate == 48000
    assert len(chunks) > 0  # audio was collected (not lost to measurement)


def test_measure_true_rate_ignores_startup_burst():
    """A bursty startup (the real mic bug) must NOT corrupt the measurement.

    Reproduces session 2026-07-29_16-29-23: the mic delivered a bursty ~12k
    frames during its first 0.4s while Core Audio ramped up, and a naive
    measurement baked 11840 Hz into the WAV -> 4x-slow playback. The fix
    discards the settle window and validates against common rates.
    """
    import queue
    q: queue.Queue = queue.Queue()

    def bursty_then_steady():
        # Burst: dump a small number of frames fast (simulates slow startup).
        for _ in range(20):
            q.put(np.zeros(48, dtype=np.float32))
        time.sleep(0.5)  # let the settle window pass
        # Then deliver steady 48k.
        stop_local = threading.Event()
        _feed_queue(q, 48000, stop_local)
    stop = threading.Event()
    feeder = threading.Thread(target=bursty_then_steady, daemon=True)
    feeder.start()
    try:
        rate, _ = recorder._measure_true_rate(q, fallback=48000, wait_seconds=1.5)
    finally:
        stop.set()
    # Must snap to a COMMON audio rate (the steady-state truth), NOT the bursty
    # ~12k that a naive measurement would have baked in. The exact common rate
    # depends on feeder jitter, so accept any standard one near 48k.
    assert rate in (48000, 44100), f"expected a common rate near 48k, got {rate}"


def test_measure_true_rate_falls_back_when_silent():
    """If no audio arrives (silent source), fall back to the requested rate."""
    import queue
    q: queue.Queue = queue.Queue()
    rate, chunks = recorder._measure_true_rate(q, fallback=16000, wait_seconds=0.6)
    assert rate == 16000
    assert chunks == []


def test_measure_true_rate_falls_back_on_nonstandard_measurement():
    """A measurement that's nowhere near a common rate is treated as unreliable."""
    import queue
    q: queue.Queue = queue.Queue()
    stop = threading.Event()
    # Feed at a nonsense 11840 frames/sec (the buggy measured rate).
    feeder = threading.Thread(target=_feed_queue, args=(q, 11840, stop, 118), daemon=True)
    feeder.start()
    try:
        rate, _ = recorder._measure_true_rate(q, fallback=48000, wait_seconds=1.0)
    finally:
        stop.set(); feeder.join(timeout=1.0)
    # 11840 isn't near any common rate -> fall back to the provided fallback.
    assert rate == 48000


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
