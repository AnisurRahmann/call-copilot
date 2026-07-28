"""faster-whisper wrapper with a Rich progress bar.

Honors DEPS.md §5 gotchas:
  - segments is a LAZY generator; we must iterate it (the for-loop below
    is what actually runs the transcription).
  - Apple Silicon has no Metal/MPS backend: device='cpu', compute_type='int8'.
  - condition_on_previous_text=False + hallucination_silence_threshold=2.0
    to avoid the "Thank you." repetition loop on long silences.
  - vad_filter=True to skip long silences (faster, cleaner output).
  - The README's beam_size_realtime param does NOT exist — we don't use it.
  - info.duration is known up-front (before iterating), so we can show a
    real determinate progress bar driven by segment end-times.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional, Protocol

from rich.console import Console

from .log import get_logger

log = get_logger(__name__)
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
)


@dataclass
class Segment:
    """One transcribed segment with start/end timestamps and text."""

    start: float
    end: float
    text: str


@dataclass
class TranscriptResult:
    segments: list[Segment]
    duration: float
    language: str
    language_probability: float


class _WhisperLike(Protocol):
    def transcribe(self, audio, **kwargs): ...  # pragma: no cover


def load_model(model_name: str, compute_type: str = "int8") -> _WhisperLike:
    """Load a faster-whisper model. Apple Silicon: CPU + int8 (no Metal backend)."""
    from faster_whisper import WhisperModel  # imported lazily — heavy

    log.info("loading whisper model %r (device=cpu compute_type=%s)", model_name, compute_type)
    return WhisperModel(model_name, device="cpu", compute_type=compute_type)


def prepare_audio_for_whisper(wav_path: str | Path) -> tuple["numpy.ndarray", int]:
    """Load a WAV and return float32 mono 16kHz data for Whisper.

    The recorder captures at the device's native rate (audiotap ignores the
    requested rate — typically ~48kHz). Whisper needs 16kHz mono float32. We
    downmix to mono and linear-resample to 16kHz here so transcription is
    accurate regardless of the capture rate. Returns (data, 16000).
    """
    import numpy as np
    import soundfile as sf

    WHISPER_RATE = 16000
    data, sr = sf.read(str(wav_path), dtype="float32", always_2d=False)
    # Downmix to mono (average channels if stereo).
    if data.ndim > 1:
        data = data.mean(axis=1)
    if int(sr) == WHISPER_RATE:
        return data, WHISPER_RATE
    # Linear-interpolation resample to 16kHz. Speech bandwidth is well under
    # 8kHz, so linear is fine here and avoids a scipy dependency.
    n_out = int(round(len(data) * WHISPER_RATE / sr))
    idx = np.linspace(0, len(data) - 1, n_out)
    resampled = np.interp(idx, np.arange(len(data)), data).astype(np.float32)
    return resampled, WHISPER_RATE


def transcribe(
    wav_path: str | Path,
    model_name: str = "base",
    language: str = "en",
    model: Optional[_WhisperLike] = None,
    console: Optional[Console] = None,
    vad_filter: bool = False,
    _audio: Optional[tuple] = None,
) -> TranscriptResult:
    """Transcribe a WAV file. `model` lets tests inject a fake (no download).

    The WAV is resampled to 16kHz mono float32 first (Whisper's native format)
    — necessary because audiotap captures at the device's native rate (~48kHz),
    not the 16kHz we request. `_audio` (a (ndarray, rate) tuple) bypasses the
    file read and is for tests; production callers omit it.

    VAD (voice activity detection) defaults to OFF. faster-whisper's Silero
    VAD is tuned for close-mic speech and aggressively rejects system-audio
    capture (speakers/headphones via a tap), which has a different character —
    we've seen it discard 100% of a clearly-audible recording, producing an
    empty transcript. Whisper's own `no_speech_threshold` handles silence well
    enough without the pre-filter. Pass vad_filter=True for clean close-mic
    input where skipping long silences is worth the risk.
    """
    console = console or Console()
    wav_str = str(wav_path)
    model = model if model is not None else load_model(model_name)

    # Resample to Whisper's native 16kHz mono before transcribing (unless the
    # caller — typically a test — supplied pre-prepared audio).
    if _audio is not None:
        audio_data, audio_rate = _audio
    else:
        audio_data, audio_rate = prepare_audio_for_whisper(wav_path)
    log.info("transcribing %s (model=%s language=%s vad=%s, %d samples @ %dHz)",
             wav_str, model_name, language, vad_filter, len(audio_data), audio_rate)

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        TimeElapsedColumn(),
        console=console,
        transient=False,
    ) as progress:
        task = progress.add_task("[cyan]Transcribing", total=None)

        transcribe_kwargs = dict(
            language=language,
            beam_size=5,
            vad_filter=vad_filter,
            condition_on_previous_text=False,
            hallucination_silence_threshold=2.0,
        )
        if vad_filter:
            # Only meaningful when vad_filter=True (faster-whisper ignores it otherwise).
            transcribe_kwargs["vad_parameters"] = dict(min_silence_duration_ms=500)

        segments_iter, info = model.transcribe(audio_data, **transcribe_kwargs)
        log.debug("whisper transcribe kwargs applied: %s",
                  {k: v for k, v in transcribe_kwargs.items() if k != "vad_parameters"})

        # info.duration is known up-front -> make the bar determinate.
        total = max(1, int(info.duration or 1))
        progress.update(task, total=total)

        out: list[Segment] = []
        for seg in segments_iter:  # iterating runs the transcription
            text = (seg.text or "").strip()
            out.append(Segment(start=float(seg.start), end=float(seg.end), text=text))
            progress.update(task, completed=min(int(seg.end), total))

        progress.update(task, completed=total)

    log.info("transcription complete: %d segments, %.1fs, language=%s (p=%.2f)",
             len(out), float(info.duration), info.language, float(info.language_probability))
    return TranscriptResult(
        segments=out,
        duration=float(info.duration),
        language=str(info.language),
        language_probability=float(info.language_probability),
    )


def count_words(segments: Iterable[Segment]) -> int:
    """Word count across all segments."""
    return sum(len(s.text.split()) for s in segments)
