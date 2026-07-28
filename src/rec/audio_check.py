"""Audio level analysis — detect silent / near-silent recordings.

A silent recording is the #1 failure mode for system-audio capture: the tap
is running but no app is playing audio (or the permission was revoked mid-
stream), the recorder faithfully writes a file of zeros, and Whisper's VAD
strips everything → an empty transcript.

Catching this immediately after `rec stop` (before Whisper runs) saves the
user a full transcription cycle and points them at the real problem. Analysis
streams the file in chunks so it's constant-memory for multi-hour recordings.

The recorder writes 32-bit float WAV (audiotap delivers float32 PCM), so we
read as float64 for headroom in the sum-of-squares and compare peak against a
normalized threshold (float samples are in the range -1.0..1.0).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .log import get_logger

log = get_logger(__name__)

# A recording is "silent" if peak amplitude is below this. float samples are
# in [-1.0, 1.0]; real speech peaks well above 0.01 even when quiet. Below
# ~0.001 is effectively the digital noise floor.
SILENCE_PEAK_THRESHOLD = 0.001

# Chunk size for streaming analysis (frames). 1M frames ≈ 62s at 16kHz.
_ANALYSIS_CHUNK = 1_000_000


@dataclass
class AudioLevels:
    """Streaming amplitude statistics over a WAV file."""

    peak: float = 0.0          # max absolute sample value (0.0..1.0 for float)
    rms: float = 0.0           # root-mean-square (overall loudness)
    frames: int = 0            # total frames analyzed
    sample_rate: int = 0
    silent: bool = False       # peak below SILENCE_PEAK_THRESHOLD

    @property
    def duration_seconds(self) -> float:
        return self.frames / self.sample_rate if self.sample_rate else 0.0


def analyze_wav(wav_path: str | Path) -> Optional[AudioLevels]:
    """Stream a WAV and compute peak + rms. Returns None if unreadable/empty.

    Reads as float64 (soundfile normalizes PCM/float to double for us). The
    recorder writes float32, so values land in [-1.0, 1.0]. Streams in chunks
    for constant memory on long recordings.
    """
    import numpy as np
    import soundfile as sf

    path = Path(wav_path)
    if not path.exists():
        log.warning("audio check: wav not found %s", path)
        return None

    try:
        with sf.SoundFile(str(path), mode="r") as f:
            sr = f.samplerate
            peak = 0.0
            sum_sq = 0.0
            total = 0
            for block in f.blocks(blocksize=_ANALYSIS_CHUNK, dtype="float64"):
                arr = block.reshape(-1) if block.ndim > 1 else block
                abs_arr = np.abs(arr)
                peak = max(peak, float(abs_arr.max(initial=0.0)))
                sum_sq += float((abs_arr ** 2).sum())
                total += arr.size
    except Exception as e:  # pragma: no cover — defensive
        log.warning("audio check: could not read %s (%r)", path, e)
        return None

    rms = (sum_sq / total) ** 0.5 if total else 0.0
    silent = peak < SILENCE_PEAK_THRESHOLD
    levels = AudioLevels(peak=peak, rms=rms, frames=total, sample_rate=sr, silent=silent)

    if silent:
        log.error(
            "RECORDING IS SILENT: peak=%.6f rms=%.6f over %.1fs — "
            "the audio tap captured no signal. Either nothing was playing, "
            "or macOS revoked the system-audio capture permission. "
            "Run `rec setup` to re-check permissions, then record again while "
            "audio is actually playing.",
            peak, rms, levels.duration_seconds,
        )
    else:
        log.info("audio levels OK: peak=%.4f rms=%.4f over %.1fs", peak, rms, levels.duration_seconds)
    return levels
