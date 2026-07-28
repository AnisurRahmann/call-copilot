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
from datetime import datetime
from pathlib import Path

import click

from .log import get_logger

log = get_logger(__name__)

STOP_TIMEOUT_S = 5.0  # grace period after SIGTERM before forcing abort


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
    """Tap system audio -> record to WAV -> block until SIGTERM. Returns exit code."""
    from . import log as log_mod
    from . import session

    # Configure the daemon's logging: DEBUG to the global log + the per-session
    # recorder.log. No console handler (the daemon has no TTY).
    session_log = session.session_dir(session_id) / "recorder.log"
    log_mod.configure_logging(
        "daemon",
        session_id=session_id,
        session_log_path=session_log,
    )
    log.info("recorder daemon starting (pid=%d, capture=%s)", os.getpid(), capture)

    import numpy  # used to interpret the float32 PCM bytes
    import soundfile as sf

    wav = session.wav_path(session_id)
    wav.parent.mkdir(parents=True, exist_ok=True)

    stop_event = threading.Event()

    def _on_signal(signum, _frame):
        log.info("received signal %s — stopping", signal.Signals(signum).name)
        stop_event.set()

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    # audiotap's `sample_rate` parameter is NOT honored — the tap always
    # delivers at the device's native rate (measured ~47kHz on most Macs). We
    # therefore can't open the WAV until the FIRST callback tells us the real
    # rate; we defer opening and write the file at whatever rate actually
    # arrives. This is what makes playback the correct speed (the previous bug
    # stamped a 47kHz stream as 16kHz, producing 3x-slow "slow-mo" audio and
    # garbage Whisper output). The transcriber resamples to 16kHz separately.
    q: queue.Queue = queue.Queue()
    first_audio = threading.Event()
    true_rate_holder: list[int] = []  # [samplerate] set by the first callback

    def on_audio(samples_bytes: bytes, frame_count: int, n_channels: int, host_time: int) -> None:
        # audiotap delivers interleaved 32-bit float PCM. Reinterpret as a
        # float32 ndarray and copy it so the writer thread owns its own buffer
        # (the bytes object is transient; the callback runs on a real-time
        # thread and must not block — we only copy + enqueue here).
        arr = numpy.frombuffer(samples_bytes, dtype=numpy.float32).copy()
        if n_channels > 1:
            arr = arr.reshape(frame_count, n_channels)
        q.put(arr)
        if not first_audio.is_set():
            # frame_count is per-callback frames; we need the rate. Compute it
            # once from the first chunk's frame_count vs the (known) sample
            # spacing — but we don't have timing per chunk reliably. Instead we
            # derive the rate from how many frames arrive per second, measured
            # across the first ~0.5s of callbacks below. For now, just signal.
            first_audio.set()

    try:
        if capture != "system":  # pragma: no cover — only "system" wired today
            raise click.ClickException(
                f"capture mode '{capture}' is not supported yet (only 'system')"
            )

        log.info("creating Core Audio system tap (requested rate=%d ch=%d)", sample_rate, channels)
        tap = _create_system_tap(on_audio, sample_rate, channels)
        try:
            tap.start()
            log.info("tap started — detecting true capture rate...")

            # Wait briefly for the first audio chunk, then measure the true rate
            # by counting frames delivered over a short wall-clock window. The
            # chunks observed during measurement are returned so we can write
            # them to the WAV too — no audio is lost to rate detection.
            true_rate, first_chunks = _measure_true_rate(q, sample_rate, wait_seconds=1.0)
            true_rate_holder.append(true_rate)
            log.info("detected true capture rate: %d Hz (requested %d)", true_rate, sample_rate)
            if abs(true_rate - sample_rate) > 500:
                log.warning(
                    "audiotap ignored the requested %d Hz and is delivering %d Hz. "
                    "Recording at the true rate; transcription will resample to 16kHz.",
                    sample_rate, true_rate,
                )

            log.info("opening WAV %s (rate=%d ch=%d subtype=FLOAT)", wav, true_rate, channels)
            with sf.SoundFile(
                str(wav),
                mode="x",
                samplerate=int(true_rate),
                channels=int(channels),
                subtype="FLOAT",
            ) as f:
                # Write the chunks observed during rate detection first — no
                # audio is lost to the measurement step.
                for chunk in first_chunks:
                    f.write(chunk)
                log.info("recording started — writing chunks to disk")
                while not stop_event.is_set():
                    try:
                        chunk = q.get(timeout=0.5)
                    except queue.Empty:
                        continue
                    f.write(chunk)  # single writer thread: safe
                drain_with_timeout(q, f, STOP_TIMEOUT_S)
                log.info("recording loop ended — draining done")
        finally:
            try:
                tap.stop()
            finally:
                tap.destroy()
    except Exception as e:  # pragma: no cover — device/path errors at runtime
        log.exception("recorder error: %r", e)
        session.update_meta(session_id, status=session.STATUS_RECORDED)
        return 1

    wav_size = wav.stat().st_size if wav.exists() else 0
    log.info("recording finalized: %s (%d bytes)", wav, wav_size)
    # Record the true rate so the transcriber resamples to Whisper's 16kHz.
    meta = session.load_meta(session_id)
    if meta is not None:
        meta.extra["capture_sample_rate"] = true_rate_holder[0] if true_rate_holder else sample_rate
        meta.extra["requested_sample_rate"] = sample_rate
        session.save_meta(meta)
    session.update_meta(session_id, status=session.STATUS_RECORDED)
    log.info("recorder daemon exiting cleanly")
    return 0


def _measure_true_rate(
    q: queue.Queue, fallback: int, wait_seconds: float = 1.0
) -> tuple[int, list]:
    """Count frames delivered over a wall-clock window to get the real rate.

    audiotap ignores the requested sample_rate and delivers at the device's
    native rate. We measure it empirically by draining the queue for
    `wait_seconds`, summing the frame counts, and dividing by elapsed time.
    Returns (measured_rate, collected_chunks) — the chunks are handed back so
    the caller can write them to the WAV and NO audio is lost to measurement.
    Falls back to `fallback` (with whatever chunks arrived) if no audio comes.
    """
    import time

    deadline = time.monotonic() + wait_seconds
    total_frames = 0
    collected: list = []
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        try:
            chunk = q.get(timeout=min(0.2, max(0.05, remaining)))
        except queue.Empty:
            continue
        total_frames += chunk.shape[0]
        collected.append(chunk)
    if not collected:
        return fallback, []
    measured = int(round(total_frames / wait_seconds))
    # Snap to a common audio rate if very close (avoids 47282 vs 48000 jitter).
    for common in (48000, 44100, 32000, 24000, 22050, 16000):
        if abs(measured - common) <= 800:
            return common, collected
    return measured, collected


def _create_system_tap(callback, sample_rate: int, channels: int):
    """Construct a SystemTap. Isolated so tests can monkeypatch it."""
    import audiotap

    return audiotap.SystemTap(
        callback=callback,
        sample_rate=float(sample_rate),
        channels=int(channels),
    )


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
