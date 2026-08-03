"""Tests for rec.transcriber + rec.formatter.

A fake Whisper model lets us verify the segment pipeline + markdown shape
without downloading any weights or touching a real audio device.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Iterable

import numpy as np
import pytest

from rec import formatter, transcriber

# Pre-prepared 16kHz mono float32 audio for tests, so they bypass the file read
# in prepare_audio_for_whisper (which would try to open a non-existent path).
_FAKE_AUDIO = (np.zeros(16000 * 5, dtype=np.float32), 16000)


# ---- fakes matching faster-whisper's public surface -----------------------


@dataclass
class _FakeSeg:
    start: float
    end: float
    text: str


@dataclass
class _FakeInfo:
    duration: float
    language: str
    language_probability: float


class _FakeModel:
    """Records kwargs passed to transcribe() and returns a lazy generator."""

    def __init__(self, segments: Iterable[_FakeSeg], info: _FakeInfo):
        self._segments = list(segments)
        self.info = info
        self.kwargs: dict = {}

    def transcribe(self, audio, **kwargs):
        self.kwargs = kwargs
        self.audio_arg = audio

        def _gen():
            for s in self._segments:
                yield s

        return _gen(), self.info


def _make_segments():
    return [
        _FakeSeg(0.0, 12.4, "  Welcome everyone, let's get started.  "),
        _FakeSeg(12.4, 25.0, "So first up, the API migration."),
        _FakeSeg(25.0, 27.0, ""),  # empty text -> should be skipped in markdown
    ]


# ---- transcriber -----------------------------------------------------------


def test_transcribe_returns_segments_and_iterates_generator(capsys):
    fake = _FakeModel(
        _make_segments(),
        _FakeInfo(duration=30.0, language="en", language_probability=0.97),
    )
    result = transcriber.transcribe(
        "/tmp/fake.wav", model_name="base", model=fake, console=None, _audio=_FAKE_AUDIO
    )
    out = capsys.readouterr()

    # The generator was consumed (3 segments collected, empty one kept as "").
    assert len(result.segments) == 3
    assert result.segments[0].start == 0.0
    assert result.segments[1].text == "So first up, the API migration."
    assert result.duration == 30.0
    assert result.language == "en"
    assert result.language_probability == pytest.approx(0.97)
    # Text was stripped.
    assert result.segments[0].text == "Welcome everyone, let's get started."
    # The resampled audio array was forwarded to the model (not the file path).
    assert fake.audio_arg is _FAKE_AUDIO[0]
    # Progress bar wrote something to the captured stream.
    assert "Transcribing" in out.out or out.err


def test_transcribe_passes_anti_hallucination_kwargs():
    fake = _FakeModel(_make_segments(), _FakeInfo(duration=30.0, language="en", language_probability=0.9))
    transcriber.transcribe("/tmp/x.wav", model=fake, console=None, _audio=_FAKE_AUDIO)
    kw = fake.kwargs
    # Per DEPS gotchas: prevent hallucination loops on long silences.
    assert kw["condition_on_previous_text"] is False
    assert kw["hallucination_silence_threshold"] == 2.0
    # VAD defaults to OFF — Silero VAD rejects system-audio capture (different
    # character than close-mic speech), discarding real audio. Whisper's own
    # no_speech_threshold handles silence without the risky pre-filter.
    assert kw["vad_filter"] is False
    assert "vad_parameters" not in kw
    assert kw["language"] == "en"
    assert kw["beam_size"] == 5
    # beam_size_realtime does NOT exist in the real API; we must not pass it.
    assert "beam_size_realtime" not in kw


def test_transcribe_vad_enabled_passes_vad_parameters():
    fake = _FakeModel(_make_segments(), _FakeInfo(duration=30.0, language="en", language_probability=0.9))
    transcriber.transcribe("/tmp/x.wav", model=fake, console=None, vad_filter=True, _audio=_FAKE_AUDIO)
    kw = fake.kwargs
    assert kw["vad_filter"] is True
    assert kw["vad_parameters"] == dict(min_silence_duration_ms=500)


def test_prepare_audio_passthrough_at_16k(tmp_path):
    """A 16kHz mono file is returned unchanged (no resampling needed)."""
    import soundfile as sf
    wav = tmp_path / "in.wav"
    original = np.ones(16000 * 2, dtype=np.float32) * 0.3
    sf.write(str(wav), original, 16000, subtype="FLOAT")
    data, rate = transcriber.prepare_audio_for_whisper(wav)
    assert rate == 16000
    assert len(data) == 16000 * 2
    assert np.allclose(data, original)


def test_prepare_audio_resamples_48k_to_16k(tmp_path):
    """A 48kHz file (audiotap's true native rate) is resampled to 16kHz mono.

    This is the core of the sample-rate bug fix: audiotap ignores the requested
    rate and delivers ~48kHz. Whisper needs 16kHz. Without resampling, Whisper
    reads a 3x-slowed file and hallucinates.
    """
    import soundfile as sf
    wav = tmp_path / "in.wav"
    # 1 second of 48kHz mono audio (48000 samples).
    original = (np.sin(2 * np.pi * 440 * np.arange(48000) / 48000) * 0.5).astype(np.float32)
    sf.write(str(wav), original, 48000, subtype="FLOAT")

    data, rate = transcriber.prepare_audio_for_whisper(wav)
    assert rate == 16000
    # 1s of 48kHz -> ~16000 samples at 16kHz.
    assert abs(len(data) - 16000) <= 100
    assert data.dtype == np.float32


def test_prepare_audio_downmixes_stereo(tmp_path):
    """A stereo 16kHz file is averaged to mono."""
    import soundfile as sf
    wav = tmp_path / "stereo.wav"
    # 0.5s stereo: left=1.0, right=0.0 -> mono should be ~0.5.
    stereo = np.stack([np.ones(8000, dtype=np.float32), np.zeros(8000, dtype=np.float32)], axis=1)
    sf.write(str(wav), stereo, 16000, subtype="FLOAT")
    data, rate = transcriber.prepare_audio_for_whisper(wav)
    assert rate == 16000
    assert data.ndim == 1
    assert np.allclose(data, 0.5, atol=0.01)


def test_load_model_calls_whispermodel(monkeypatch):
    import rec.transcriber as mod

    captured = {}

    class _WM:
        def __init__(self, name, **kw):
            captured["name"] = name
            captured.update(kw)

    monkeypatch.setitem(sys.modules, "faster_whisper", SimpleNamespace(WhisperModel=_WM))
    mod.load_model("medium")
    assert captured["name"] == "medium"
    # Apple Silicon: CPU + int8 (no Metal backend).
    assert captured["device"] == "cpu"
    assert captured["compute_type"] == "int8"


def test_count_words():
    segs = [
        transcriber.Segment(0, 1, "hello world"),  # 2
        transcriber.Segment(1, 2, "third one here"),  # 3
        transcriber.Segment(2, 3, ""),  # 0
    ]
    assert transcriber.count_words(segs) == 5


# ---- formatter (markdown shape) -------------------------------------------


def test_build_markdown_full_shape():
    segs = [
        transcriber.Segment(0.0, 12.0, "Welcome everyone."),
        transcriber.Segment(12.0, 25.0, "First up, the API migration."),
    ]
    md = formatter.build_markdown(
        segs,
        date_str="2026-07-27",
        duration_seconds=47 * 60,
        wav_filename="recording.wav",
    )
    expected_lines = [
        "# Meeting Transcript",
        "",
        "**Date:** 2026-07-27",
        "**Duration:** 47 min",
        "**File:** recording.wav",
        "",
        "---",
        "",
        "[00:00] Welcome everyone.",
        "",
        "[00:12] First up, the API migration.",
        "",
    ]
    assert md == "\n".join(expected_lines).rstrip() + "\n"


def test_build_markdown_skips_empty_segments():
    segs = [
        transcriber.Segment(0.0, 5.0, "real text"),
        transcriber.Segment(5.0, 7.0, ""),
        transcriber.Segment(7.0, 9.0, "   "),
    ]
    md = formatter.build_markdown(segs, date_str="2026-07-27")
    assert "[00:00] real text" in md
    assert "[00:05]" not in md
    assert "[00:07]" not in md


def test_build_markdown_uses_hour_format_over_one_hour():
    segs = [transcriber.Segment(3661.0, 3662.0, "later")]
    md = formatter.build_markdown(segs, date_str="2026-07-27")
    assert "[1:01:01] later" in md


def test_build_markdown_defaults_today_date():
    segs = [transcriber.Segment(0.0, 1.0, "hi")]
    md = formatter.build_markdown(segs)
    # Should contain a **Date:** line with an ISO date.
    assert "**Date:** 20" in md


def test_write_transcript_creates_file(xdg):
    from rec import session

    sid = "2026-07-27_14-30-00"
    session.create_session_dir(sid)
    path = formatter.write_transcript(sid, "# hello\n")
    from pathlib import Path

    p = Path(path)
    assert p.exists()
    assert p.name == "transcript.md"
    assert p.read_text() == "# hello\n"


# ---- merged mic+system formatter ------------------------------------------


def _result(segs, duration=30.0):
    """Build a TranscriptResult from (start, text) tuples."""
    return transcriber.TranscriptResult(
        segments=[transcriber.Segment(s, s + 2, t) for s, t in segs],
        duration=duration, language="en", language_probability=0.9,
    )


def test_build_merged_markdown_interleaves_by_timestamp():
    sys_segs = _result([(0.0, "hello from system"), (20.0, "system again")])
    mic_segs = _result([(10.0, "hello from mic"), (30.0, "mic last")])
    md = formatter.build_merged_markdown(
        system_result=sys_segs, mic_result=mic_segs,
        date_str="2026-07-28",
        wav_filenames=("recording.wav", "recording-mic.wav"),
    )
    # Header declares both sources.
    assert "**Sources:** System (recording.wav) + Microphone (recording-mic.wav)" in md
    # Lines are interleaved by timestamp with source labels.
    assert "[System] [00:00] hello from system" in md
    assert "[Mic] [00:10] hello from mic" in md
    assert "[System] [00:20] system again" in md
    assert "[Mic] [00:30] mic last" in md
    # Order: system@0, mic@10, system@20, mic@30.
    lines = [ln for ln in md.splitlines() if ln.startswith(("[System]", "[Mic]"))]
    assert lines == [
        "[System] [00:00] hello from system",
        "[Mic] [00:10] hello from mic",
        "[System] [00:20] system again",
        "[Mic] [00:30] mic last",
    ]


def test_build_merged_markdown_handles_empty_source():
    """If one source has no speech (VAD/silence), only the other's lines appear."""
    sys_segs = _result([(0.0, "only system spoke")])
    mic_segs = _result([])  # mic captured nothing transcribable
    md = formatter.build_merged_markdown(
        system_result=sys_segs, mic_result=mic_segs, date_str="2026-07-28"
    )
    assert "[System] [00:00] only system spoke" in md
    assert "[Mic]" not in md  # no mic lines


def test_build_markdown_source_label_prefixes_lines():
    """Single-source build_markdown tags lines when source is given."""
    segs = [transcriber.Segment(0.0, 5.0, "hi")]
    md = formatter.build_markdown(segs, date_str="2026-07-28", source="Mic")
    assert "Mic [00:00] hi" in md
