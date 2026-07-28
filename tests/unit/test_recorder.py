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


class FakeSystemTap:
    """Mimics audiotap.SystemTap: invokes the callback on a background thread."""

    def __init__(self, callback, sample_rate=16000.0, channels=1, **kwargs):
        self.callback = callback
        self.sample_rate = sample_rate
        self.channels = channels
        self.kwargs = kwargs
        self._thread = None
        self._stop_evt = threading.Event()
        self.started = False
        # Expose what the recorder passed so tests can assert on it.
        FakeSystemTap.last_created = self

    def start(self):
        self.started = True

    def stop(self):
        self._stop_evt.set()

    def destroy(self):
        self._stop_evt.set()

    def feed(self, frames: int):
        """Test helper: deliver one chunk of `frames` samples to the callback."""
        # Interleaved float32 PCM: frames * channels floats.
        data = np.ones(frames * self.channels, dtype=np.float32) * 0.5
        self.callback(data.tobytes(), frames, self.channels, 0)


@pytest.fixture
def fake_audiotap(monkeypatch):
    """Inject the fake SystemTap so the recorder uses it instead of Core Audio."""
    fake_pkg = types.ModuleType("audiotap")
    fake_pkg.SystemTap = FakeSystemTap
    monkeypatch.setitem(sys.modules, "audiotap", fake_pkg)
    return fake_pkg


# ---- helper to run the daemon in-process with a feeder thread --------------


def _run_daemon_with_feeder(session_id, sr, ch, feed_frames_per_call=1600, feed_count=10):
    """Spawn run_detached in a thread; feed audio; then SIGTERM it; join."""
    import threading

    pid = os.getpid()

    def sigterm_self_after_feeding():
        # Wait for the tap to be created + started, then feed audio, then stop.
        for _ in range(50):
            tap = getattr(FakeSystemTap, "last_created", None)
            if tap is not None and tap.started:
                break
            time.sleep(0.01)
        tap = FakeSystemTap.last_created
        for _ in range(feed_count):
            tap.feed(feed_frames_per_call)
            time.sleep(0.005)
        # Now signal stop (the recorder watches stop_event on SIGTERM).
        os.kill(pid, signal.SIGTERM)

    feeder = threading.Thread(target=sigterm_self_after_feeding, daemon=True)
    feeder.start()
    # run_detached installs the SIGTERM handler and blocks until stop.
    return recorder.run_detached(session_id, sr, ch, "system"), feeder


# ---- tests -----------------------------------------------------------------


def test_measure_true_rate_from_queue_frames():
    """_measure_true_rate infers the native rate from delivered frame counts.

    audiotap ignores the requested sample_rate; we detect the true rate by
    counting frames over a wall-clock window. This is the heart of the
    sample-rate-bug fix.
    """
    import queue
    # Simulate ~48kHz delivery: enqueue enough 1ms chunks (48 frames each) to
    # look like 48000 frames/sec. Use a very short window so the test is fast.
    q: queue.Queue = queue.Queue()
    for _ in range(480):  # 480 chunks * 48 frames = 23040 frames
        q.put(np.zeros(48, dtype=np.float32))
    rate, chunks = recorder._measure_true_rate(q, fallback=16000, wait_seconds=0.48)
    # 23040 frames / 0.48s = 48000 Hz -> snaps to the common 48000 rate.
    assert rate == 48000
    # No audio is lost: all drained chunks are returned.
    assert len(chunks) == 480


def test_measure_true_rate_falls_back_when_silent():
    """If no audio arrives (silent source), fall back to the requested rate."""
    import queue
    q: queue.Queue = queue.Queue()
    rate, chunks = recorder._measure_true_rate(q, fallback=16000, wait_seconds=0.1)
    assert rate == 16000
    assert chunks == []


def test_measure_true_rate_snaps_to_common_rates():
    """Jittery measurements (e.g. 47282) snap to the nearest common rate (48000)."""
    import queue
    q: queue.Queue = queue.Queue()
    # 47282 frames/sec-ish: 4728 frames over 0.1s.
    for _ in range(99):
        q.put(np.zeros(48, dtype=np.float32))  # ~4752 frames
    rate, _ = recorder._measure_true_rate(q, fallback=16000, wait_seconds=0.1)
    assert rate == 48000  # snapped, not 47520


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
    code = recorder.run_detached(sid, 16000, 1, "system+mic")
    assert code == 1


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
