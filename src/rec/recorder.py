"""Background recording daemon — Core Audio taps via the `audiotap` library.

This replaces the old BlackHole + Multi-Output Device approach. `audiotap`
taps system audio output directly through Apple's Core Audio taps API
(macOS 14.2+), so there is:
  - no virtual audio driver to install,
  - no Multi-Output Device to create in Audio MIDI Setup,
  - no system output device to switch + restore,
  - no possibility of the "silent recording" failure the old approach produced
    when BlackHole wasn't actually receiving audio.

Architecture (unchanged from the BlackHole version):
  `rec start` spawns a fully detached child process (`python -m rec.recorder`)
  via subprocess.Popen(start_new_session=True). The child survives the parent
  exiting (the terminal is free for other work), taps audio, streams chunks
  to a WAV on disk, and shuts down cleanly on SIGTERM from `rec stop`.

Honors the audiotap real-time-thread constraint (same as sounddevice's): the
callback runs on Core Audio's real-time thread and must not block. We copy
the incoming bytes and hand them to a queue; a single writer thread drains
the queue and writes to the SoundFile. The callback logs nothing.

Recording format:
  - sample_rate 16000 Hz (Whisper's native rate — no resampling, smallest files)
  - channels 1 (mono — speech doesn't need stereo)
  - 32-bit float WAV (audiotap always delivers float32 PCM; subtype='FLOAT')
"""

from __future__ import annotations

import argparse
import os
import queue
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime

import click

from .log import get_logger

log = get_logger(__name__)

STOP_TIMEOUT_S = 5.0  # grace period after SIGTERM before forcing abort

# The canonical capture modes offered to users (the API and the web select
# render this list; the CLI --help references it). _parse_capture still
# accepts the aliases (both, system+mic, "mic and system") for ergonomics,
# but these three are the canonical names. Single source so the frontend
# never hardcodes a set that can drift from what the recorder accepts.
CAPTURE_MODES: tuple[str, ...] = ("mic+system", "mic", "system")


# ---- daemonization (called from `rec start`) ------------------------------


def spawn_recorder(session_id: str, sample_rate: int, channels: int, capture: str) -> int:
    """Spawn a detached recording process; return its PID.

    Re-invokes this same module via `python -m rec.recorder`. The child is
    started in a new session (start_new_session=True) so it survives the
    parent (`rec start`) exiting. Stdout/stderr are redirected to a log file
    in the session dir so the terminal stays clean.
    """
    from . import config, session

    session.create_session_dir(session_id)
    log_path = session.session_dir(session_id) / "recorder.log"
    log_fp = open(log_path, "ab", buffering=0)  # noqa: SIM115 — child owns it

    argv = [
        sys.executable,
        "-m",
        "rec.recorder",
        "--session-id",
        session_id,
        "--sample-rate",
        str(sample_rate),
        "--channels",
        str(channels),
        "--capture",
        capture,
    ]
    proc = subprocess.Popen(
        argv,
        stdout=log_fp,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        start_new_session=True,  # detach: child survives parent exit
        close_fds=True,
    )
    log_fp.close()

    log.info("spawned recorder daemon: pid=%d argv=%s", proc.pid, argv)

    # Record PID for `rec stop` + update session metadata.
    config.pid_path().parent.mkdir(parents=True, exist_ok=True)
    config.pid_path().write_text(str(proc.pid) + "\n", encoding="utf-8")
    session.update_meta(
        session_id,
        status=session.STATUS_RECORDING,
        started_at=datetime.now().isoformat(timespec="seconds"),
    )
    return proc.pid


# ---- the actual recording loop (runs in the child process) -----------------


def run_detached(session_id: str, sample_rate: int, channels: int, capture: str) -> int:
    """Record one or two audio sources to WAV(s) -> block until SIGTERM.

    `capture` selects the source(s):
      - "system"      -> system audio only (recording.wav)
      - "mic"         -> microphone only (recording-mic.wav)
      - "mic+system"  -> BOTH, in parallel, as two separate WAVs

    audiotap ignores the requested sample_rate and delivers at each device's
    native rate; each source detects + writes at its own true rate. The
    transcriber resamples each WAV to 16kHz for Whisper, and the formatter
    merges the two transcripts (labeled [Mic]/[System]) into one markdown.
    """
    from . import log as log_mod
    from . import session

    # Ensure the session dir + a 'recording' meta exist. The CLI's `start`
    # command does this too, but run_detached must be self-sufficient (tests and
    # `python -m rec.recorder` call it directly without the CLI wrapper).
    session.create_session_dir(session_id)
    if session.load_meta(session_id) is None:
        from datetime import datetime
        session.update_meta(session_id, status=session.STATUS_RECORDING,
                            started_at=datetime.now().isoformat(timespec="seconds"))

    session_log = session.session_dir(session_id) / "recorder.log"
    log_mod.configure_logging("daemon", session_id=session_id, session_log_path=session_log)
    log.info("recorder daemon starting (pid=%d, capture=%s)", os.getpid(), capture)

    stop_event = threading.Event()

    def _on_signal(signum, _frame):
        log.info("received signal %s — stopping", signal.Signals(signum).name)
        stop_event.set()

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    recorders: list[_TapRecorder] = []

    try:
        wanted = _parse_capture(capture)
        # Build recorders. Each source detects + writes at its OWN true rate.
        # NOTE: the mic and system taps do NOT reliably share a delivery rate,
        # so we never use one source's measured rate as another's fallback —
        # a wrong fallback header (e.g. 48k on a 16k mic stream) makes the WAV
        # play back at the wrong speed. Each source falls back to the requested
        # sample_rate, and _measure_true_rate re-measures over longer windows
        # before ever reaching that fallback.
        sys_recorder = None
        if "system" in wanted:
            wav = session.wav_path(session_id)
            wav.parent.mkdir(parents=True, exist_ok=True)
            sys_recorder = _TapRecorder("system", wav, sample_rate, channels,
                                        _create_system_tap, stop_event)
            recorders.append(sys_recorder)
        if "mic" in wanted:
            if not _mic_available():
                log.warning(
                    "microphone permission not granted — skipping mic capture. "
                    "System audio (if any) will still be recorded. Grant mic access "
                    "in System Settings > Privacy & Security > Microphone."
                )
            else:
                mwav = session.mic_wav_path(session_id)
                mwav.parent.mkdir(parents=True, exist_ok=True)
                recorders.append(_TapRecorder("mic", mwav, sample_rate, channels,
                                              _create_mic_tap, stop_event))

        if not recorders:
            raise RuntimeError(
                f"no audio sources available for capture='{capture}' "
                "(no system tap and mic not permitted)"
            )

        # Start the system recorder first (it's the more reliable clock), then
        # the mic. Each falls back to the REQUESTED rate — never the other's.
        if sys_recorder is not None:
            sys_recorder.start()
        for r in recorders:
            if r is sys_recorder:
                continue
            r.start()

        # Block until SIGTERM/SIGINT, then stop them all (drain + close).
        while not stop_event.is_set():
            stop_event.wait(timeout=0.5)
        for r in recorders:
            r.stop()
    except Exception as e:  # pragma: no cover — device/path errors at runtime
        log.exception("recorder error: %r", e)
        for r in recorders:
            try:
                r.stop()
            except Exception:
                pass
        session.update_meta(session_id, status=session.STATUS_RECORDED)
        return 1

    # Persist per-source true rates + sizes + status in ONE write (avoids a
    # load-modify-save race that was wiping extra{}).
    meta = session.load_meta(session_id)
    if meta is not None:
        for r in recorders:
            tag = "mic" if r.source == "mic" else "system"
            meta.extra[f"{tag}_sample_rate"] = r.true_rate
            meta.extra[f"{tag}_bytes"] = r.bytes_written
        meta.extra["requested_sample_rate"] = sample_rate
        meta.extra["capture"] = capture
        meta.status = session.STATUS_RECORDED
        session.save_meta(meta)
    else:
        session.update_meta(session_id, status=session.STATUS_RECORDED)
    log.info("recorder daemon exiting cleanly")
    return 0


class _TapRecorder:
    """One audio source: tap -> rate detection -> WAV writer, until stop_event.

    Encapsulates the single-source logic (formerly run_detached's body) so we
    can run mic + system in parallel. Each instance owns its queue, callback,
    tap, and SoundFile; all share the parent `stop_event`.
    """

    def __init__(self, source, wav_path, sample_rate, channels, tap_factory, stop_event,
                 fallback_rate=None):
        import numpy  # ensure numpy is loaded before the callback thread starts
        assert numpy
        self.source = source  # "system" | "mic"
        self.wav_path = wav_path
        self.sample_rate = sample_rate
        self.channels = channels
        self.tap_factory = tap_factory
        self.stop_event = stop_event
        # Fallback rate if measurement is unreliable. Defaults to the requested
        # rate, but callers can pass a better guess (e.g. the system tap's
        # measured rate, since mic+system share Core Audio's clock domain).
        self.fallback_rate = fallback_rate or sample_rate
        self.q: queue.Queue = queue.Queue()
        self.tap = None
        self.true_rate: int = sample_rate
        self.bytes_written: int = 0

    def _callback(self, samples_bytes: bytes, frame_count: int, n_channels: int, host_time: int) -> None:
        # Real-time-thread callback: copy + enqueue only. No I/O, no blocking.
        import numpy as np
        arr = np.frombuffer(samples_bytes, dtype=np.float32).copy()
        if n_channels > 1:
            arr = arr.reshape(frame_count, n_channels)
        self.q.put(arr)

    def start(self) -> None:
        import soundfile as sf

        log.info("creating %s tap (requested rate=%d ch=%d)", self.source, self.sample_rate, self.channels)
        self.tap = self.tap_factory(self._callback, self.sample_rate, self.channels)
        self.tap.start()
        log.info("%s tap started — detecting true capture rate...", self.source)

        # Detect the true rate over a short window; collect the observed chunks
        # so they're written (no audio lost to measurement).
        self.true_rate, first_chunks = _measure_true_rate(
            self.q, self.fallback_rate, wait_seconds=1.0
        )
        log.info("%s true capture rate: %d Hz (requested %d)", self.source, self.true_rate, self.sample_rate)
        if abs(self.true_rate - self.sample_rate) > 500:
            log.warning(
                "%s: audiotap ignored requested %d Hz, delivering %d Hz. "
                "Writing at true rate; transcription resamples to 16kHz.",
                self.source, self.sample_rate, self.true_rate,
            )

        log.info("opening %s WAV %s (rate=%d ch=%d subtype=FLOAT)",
                 self.source, self.wav_path, self.true_rate, self.channels)
        # Keep the SoundFile open for the recording lifetime; we close in stop().
        self._sf = sf.SoundFile(
            str(self.wav_path), mode="x",
            samplerate=int(self.true_rate), channels=int(self.channels), subtype="FLOAT",
        )
        for chunk in first_chunks:
            self._sf.write(chunk)
        log.info("%s recording started", self.source)

        # Writer thread drains the queue into the SoundFile until stop_event.
        self._writer = threading.Thread(target=self._write_loop, name=f"rec-{self.source}-writer", daemon=True)
        self._writer.start()

    def _write_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                chunk = self.q.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self._sf.write(chunk)
            except Exception as e:  # pragma: no cover — defensive
                log.warning("%s write error: %r", self.source, e)
                return

    def stop(self) -> None:
        # Drain remaining queued audio, close the WAV, stop + destroy the tap.
        try:
            drain_with_timeout(self.q, self._sf, STOP_TIMEOUT_S)
        except Exception:  # pragma: no cover
            pass
        try:
            self._sf.close()
        except Exception:  # pragma: no cover
            pass
        self.bytes_written = self.wav_path.stat().st_size if self.wav_path.exists() else 0
        log.info("%s recording finalized: %s (%d bytes)", self.source, self.wav_path, self.bytes_written)
        if self.tap is not None:
            try:
                self.tap.stop()
            finally:
                self.tap.destroy()
        if getattr(self, "_writer", None) is not None:
            self._writer.join(timeout=1.0)


def _parse_capture(capture: str) -> set[str]:
    """Normalize a capture mode string into a set of sources."""
    c = (capture or "system").strip().lower()
    if c in ("system", "mic"):
        return {c}
    if c in ("mic+system", "system+mic", "both", "mic and system"):
        return {"mic", "system"}
    raise ValueError(f"unknown capture mode: {capture!r}")


def _mic_available() -> bool:
    """True if mic permission is granted (or promptable). Best-effort."""
    try:
        import audiotap
        status = audiotap.mic_permission_status()
        if status == audiotap.Permission.GRANTED:
            return True
        if status == audiotap.Permission.UNKNOWN:
            # UNKNOWN means we can still prompt; the actual tap creation will
            # surface the macOS dialog. Try requesting once.
            return audiotap.request_mic_permission() == audiotap.Permission.GRANTED
        return False  # DENIED
    except Exception:  # pragma: no cover — audiotap missing/broken
        return False


def _measure_true_rate(
    q: queue.Queue, fallback: int, wait_seconds: float = 1.0
) -> tuple[int, list]:
    """Measure a tap's true delivery rate and return (rate, all_chunks_seen).

    audiotap ignores the requested sample_rate and delivers at the device's
    native rate. We measure it empirically, with guards learned from two real
    bugs, both caused by a mic tap delivering BURSTY, irregular frames while
    Core Audio ramps up:

      - 11840 Hz burst (session 2026-07-29): a naive measurement was TRUSTED
        and baked into the header -> 4x-slow playback.
      - 14097 Hz burst (session 2026-08-04): the measurement was correctly
        REJECTED, but the fallback was the SYSTEM tap's 48000 Hz (borrowed
        because mic+system "share a clock domain"). The mic's true rate was
        ~16000, so a 48000 header made it play 3x too fast (chipmunk voice).

    The fix: NEVER fall back to another source's rate. Instead, if the first
    short measurement doesn't snap to a common rate, RE-MEASURE over a longer
    window. Burstiness averages out as the window grows, so the measurement
    converges on the true rate. All observed chunks are collected and returned
    regardless, so NO audio is lost to measurement.

    `wait_seconds` is the INITIAL measurement budget (settling + first window).
    Extra retries add time but only trigger when the source is genuinely bursty.
    """
    SETTLE = 0.4  # discard the first 0.4s of bursty startup frames from the math
    collected: list = []

    def _drain_settle() -> None:
        """Collect (but don't count) the startup-settling frames."""
        settle_deadline = time.monotonic() + SETTLE
        while time.monotonic() < settle_deadline:
            remaining = settle_deadline - time.monotonic()
            try:
                chunk = q.get(timeout=min(0.2, max(0.05, remaining)))
            except queue.Empty:
                continue
            collected.append(chunk)

    def _measure_once(window: float) -> int | None:
        """Measure frames/elapsed over `window` seconds. None if nothing arrived."""
        deadline = time.monotonic() + window
        measured_frames = 0
        start = time.monotonic()
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            try:
                chunk = q.get(timeout=min(0.2, max(0.05, remaining)))
            except queue.Empty:
                continue
            measured_frames += chunk.shape[0]
            collected.append(chunk)
        elapsed = time.monotonic() - start
        if measured_frames == 0 or elapsed <= 0:
            return None
        return int(round(measured_frames / elapsed))

    # Initial settle + first measurement.
    _drain_settle()
    first_window = max(0.3, wait_seconds - SETTLE)
    measured = _measure_once(first_window)

    snapped = _snap_if_common(measured)
    if snapped is not None:
        return snapped, collected

    # First measurement didn't snap (bursty startup, or nothing arrived). Re-measure
    # over progressively longer windows; burstiness averages out. This is the fix
    # for the 14097 Hz mic burst that previously fell back to a wrong 48000 Hz.
    if measured is not None:
        log.warning(
            "true-rate measurement %d Hz not near any common rate; re-measuring "
            "over a longer window (bursty startup).", measured,
        )
    for longer in (2.0, 3.0):
        measured = _measure_once(longer)
        snapped = _snap_if_common(measured)
        if snapped is not None:
            log.info("true-rate converged to %d Hz after longer window.", snapped)
            return snapped, collected

    # Genuinely can't pin it (silent source, or extreme jitter). Fall back to the
    # REQUESTED rate for this source — never another source's rate. 16000 is a
    # safe guess for the mic (matches Whisper's rate; worst case a 3x pitch
    # error, never the multi-hour corruption a wrong 48k header causes).
    if measured is not None:
        import sys
        print(f"WARN: true-rate measurement {measured} Hz stayed unreliable; "
              f"falling back to {fallback} Hz.", file=sys.stderr, flush=True)
    return fallback, collected


def _snap_if_common(measured: int | None) -> int | None:
    """Snap a measured rate to the nearest standard rate, or None if not near one.

    Single source of truth for the tolerance decision: a measurement within
    max(800, 5%) of a standard rate (48000/44100/32000/24000/22050/16000)
    snaps to that rate; anything else returns None (untrustworthy). Returns the
    SNAPPED standard rate, never the raw measurement, so the WAV header always
    carries a clean standard rate.
    """
    if measured is None:
        return None
    common_rates = (48000, 44100, 32000, 24000, 22050, 16000)
    snapped = min(common_rates, key=lambda c: abs(measured - c))
    if abs(measured - snapped) <= max(800, snapped * 0.05):  # 5% tolerance
        return snapped
    return None


def _snap_to_common_rate(measured: int, fallback: int) -> int:
    """Snap a measured sample rate to the nearest common audio rate.

    If `measured` is within tolerance of a standard rate (48000/44100/32000/
    24000/22050/16000), return that rate. Otherwise the measurement is treated
    as unreliable (startup burstiness, silent source, scheduler jitter, etc.)
    and we return `fallback` rather than baking a nonsense rate into the WAV.

    Pure function — kept for the deterministic unit tests of the snap decision.
    Production code uses `_snap_if_common` (which returns None instead of a
    fallback) so the caller can choose to re-measure rather than fall back.
    """
    snapped = _snap_if_common(measured)
    if snapped is not None:
        return snapped
    # Measurement didn't match any common rate — don't trust it.
    import sys

    print(f"WARN: true-rate measurement {measured} Hz is not near any common "
          f"audio rate; falling back to {fallback} Hz.", file=sys.stderr, flush=True)
    return fallback


def _create_system_tap(callback, sample_rate: int, channels: int):
    """Construct a SystemTap. Isolated so tests can monkeypatch it."""
    import audiotap

    return audiotap.SystemTap(
        callback=callback,
        sample_rate=float(sample_rate),
        channels=int(channels),
    )


def _create_mic_tap(callback, sample_rate: int, channels: int):
    """Construct a MicTap. Isolated so tests can monkeypatch it."""
    import audiotap

    return audiotap.MicTap(
        callback=callback,
        sample_rate=float(sample_rate),
        channels=int(channels),
    )


# ---- capture self-test (called from `rec setup`) --------------------------


@dataclass
class CaptureProbe:
    """Result of probing one audio source for `seconds`.

    `created` is False when the tap itself failed to construct (e.g. the mic is
    denied, or macOS lacks the API). `peak` is the max absolute float sample
    captured; near-zero on a live source means the permission isn't being
    honored for the running app — the exact failure this tool exists to catch.
    """
    source: str        # "system" | "mic"
    created: bool      # False if the tap raised at construction
    error: str = ""    # message when created is False
    peak: float = 0.0  # max |sample| over the probe window (0.0..1.0)
    frames: int = 0    # total frames captured


def probe_capture(source: str, seconds: float = 2.0) -> CaptureProbe:
    """Open a tap for `seconds`, measure peak amplitude, then close it.

    Used by `rec setup --selftest` to catch a broken capture permission BEFORE
    a real meeting: macOS grants the Screen Recording checkbox but keeps
    handing a running process zero buffers until the app is fully restarted.
    A live tap that returns peak≈0 on a source that should have signal is the
    signature of that state. The caller decides what counts as "signal" — for
    the mic the user can speak; for system audio we ask them to play something.
    """
    import numpy as np

    factory = _create_mic_tap if source == "mic" else _create_system_tap
    collected: list[np.ndarray] = []

    def _cb(samples_bytes: bytes, frame_count: int, n_channels: int, _host_time: int) -> None:
        arr = np.frombuffer(samples_bytes, dtype=np.float32).copy()
        collected.append(arr)

    try:
        tap = factory(_cb, 48000, 1)
    except Exception as e:  # tap construction failed (denied / unsupported)
        return CaptureProbe(source=source, created=False, error=str(e))

    try:
        tap.start()
    except Exception as e:
        try:
            tap.destroy()
        except Exception:
            pass
        return CaptureProbe(source=source, created=False, error=str(e))

    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        time.sleep(0.05)

    try:
        tap.stop()
    finally:
        try:
            tap.destroy()
        except Exception:
            pass

    if collected:
        allv = np.concatenate(collected)
        return CaptureProbe(
            source=source, created=True,
            peak=float(np.abs(allv).max(initial=0.0)),
            frames=int(allv.size),
        )
    return CaptureProbe(source=source, created=True, peak=0.0, frames=0)


def drain_with_timeout(q: queue.Queue, f, timeout_s: float) -> None:
    """Flush remaining queued chunks to the SoundFile, bounded by timeout."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            f.write(q.get_nowait())
        except queue.Empty:
            break


# ---- stop / salvage (called from `rec stop`) -------------------------------


def stop_recorder() -> tuple[bool, int | None]:
    """Send SIGTERM to the live recorder. Returns (was_alive, pid).

    If the PID file is missing or the process is already dead, returns
    (False, None) — the caller should still salvage the partial WAV.
    """
    from . import config

    pid_file = config.pid_path()
    if not pid_file.exists():
        log.info("no pid file — nothing to stop")
        return False, None

    try:
        pid = int(pid_file.read_text(encoding="utf-8").strip())
    except ValueError:
        log.warning("pid file corrupt — removing")
        pid_file.unlink(missing_ok=True)
        return False, None

    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        # Stale PID — process already gone. Salvage what's on disk.
        log.warning("pid %d already dead (stale) — will salvage partial audio", pid)
        pid_file.unlink(missing_ok=True)
        return False, pid
    except PermissionError:
        log.error("permission denied stopping pid %d", pid)
        raise click.ClickException(
            f"Cannot stop recording (pid {pid}): permission denied."
        )

    log.info("sent SIGTERM to pid %d — waiting up to %.1fs", pid, STOP_TIMEOUT_S)
    # Wait for graceful exit.
    deadline = time.monotonic() + STOP_TIMEOUT_S
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)

    pid_file.unlink(missing_ok=True)
    log.info("pid %d stopped", pid)
    return True, pid


def cleanup_pid_file() -> None:
    """Remove a stale pid file if present (best effort)."""
    from . import config

    config.pid_path().unlink(missing_ok=True)


def active_pid() -> int | None:
    """Return the live recorder PID, or None if no recording is running."""
    from . import config

    pid_file = config.pid_path()
    if not pid_file.exists():
        return None
    try:
        pid = int(pid_file.read_text(encoding="utf-8").strip())
    except ValueError:
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return None
    return pid


# ---- module entry point (`python -m rec.recorder`) ------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="rec.recorder", description="Recording daemon")
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--sample-rate", type=int, default=16000)
    parser.add_argument("--channels", type=int, default=1)
    parser.add_argument("--capture", default="system")
    args = parser.parse_args(argv)
    return run_detached(args.session_id, args.sample_rate, args.channels, args.capture)


if __name__ == "__main__":
    raise SystemExit(main())
