"""Tests for rec.audio_check — silence detection + message distinction."""

from __future__ import annotations

import logging
from pathlib import Path

import numpy as np
import soundfile as sf

from rec import audio_check


def _write_wav(path: Path, samples: np.ndarray, sample_rate: int = 16000) -> None:
    """Write a float32 mono WAV using soundfile (matches the recorder's output)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(path), samples.astype(np.float32), sample_rate, subtype="FLOAT")


def test_analyze_wav_healthy_audio(tmp_path: Path):
    wav = tmp_path / "recording.wav"
    samples = (np.random.RandomState(0).randn(16000) * 0.3).astype(np.float32)
    _write_wav(wav, samples)
    levels = audio_check.analyze_wav(wav)
    assert levels is not None
    assert not levels.silent
    assert levels.peak > audio_check.SILENCE_PEAK_THRESHOLD
    assert levels.frames == 16000


def test_analyze_wav_silence_with_frames_logs_permission_signature(tmp_path: Path, caplog):
    """peak==0.0 with frames>0 is the macOS permission signature — log names it."""
    wav = tmp_path / "recording.wav"
    _write_wav(wav, np.zeros(16000, dtype=np.float32))
    with caplog.at_level(logging.ERROR, logger="rec.audio_check"):
        levels = audio_check.analyze_wav(wav)
    assert levels is not None
    assert levels.silent
    assert levels.peak == 0.0
    assert levels.frames == 16000
    # The permission-signature branch fires (frames>0 AND peak==0.0).
    msg = caplog.records[-1].getMessage()
    assert "literally zero samples" in msg
    assert "capture-permission signature" in msg
    assert "Screen Recording" in msg


def test_analyze_wav_missing_file(tmp_path: Path):
    levels = audio_check.analyze_wav(tmp_path / "nope.wav")
    assert levels is None


def test_silence_threshold_constant_unchanged():
    """Guard the documented threshold against accidental drift."""
    assert audio_check.SILENCE_PEAK_THRESHOLD == 0.001
