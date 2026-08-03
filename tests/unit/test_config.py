"""Tests for rec.config — Pydantic model + XDG paths + load/save + legacy tolerance."""

from __future__ import annotations

import json
from pathlib import Path

import click
import pytest

from rec import config


def test_xdg_paths_default_to_home(xdg):
    # config_path respects XDG_CONFIG_HOME set by the fixture.
    assert config.config_path() == xdg / "config" / "rec" / "config.json"
    assert config.sessions_root() == xdg / "data" / "rec" / "sessions"
    assert config.pid_path() == xdg / "config" / "rec" / "rec.pid"


def test_default_config_uses_recommended_defaults():
    cfg = config.default_config()
    # Core Audio taps default: 16kHz mono, Whisper-native (no resampling).
    assert cfg.sample_rate == 16000
    assert cfg.channels == 1
    assert cfg.capture == "mic+system"
    assert cfg.whisper_model == "base"
    # No audio-routing fields exist anymore (BlackHole/Multi-Output removed).
    assert not hasattr(cfg, "original_output_device")
    assert not hasattr(cfg, "multi_output_device")
    assert not hasattr(cfg, "blackhole_device")


def test_save_and_load_round_trip(xdg):
    cfg = config.default_config()
    cfg.whisper_model = "medium"
    path = config.save_config(cfg)
    assert path.exists()

    loaded = config.load_config()
    assert loaded.whisper_model == "medium"
    assert loaded.sample_rate == 16000
    assert loaded.capture == "mic+system"


def test_save_config_preserves_sessions_dir_as_tilde(xdg, tmp_path):
    # When the sessions dir is under the user's home, it's persisted with a
    # ~-prefix for portability.
    cfg = config.default_config()
    cfg.sessions_dir = Path.home() / "my-sessions"
    path = config.save_config(cfg)
    raw = json.loads(path.read_text())
    assert raw["sessions_dir"].startswith("~")
    assert "my-sessions" in raw["sessions_dir"]

    # Dirs NOT under home stay absolute (no fake ~).
    cfg2 = config.default_config()
    cfg2.sessions_dir = tmp_path / "elsewhere"
    path2 = config.save_config(cfg2)
    raw2 = json.loads(path2.read_text())
    assert not raw2["sessions_dir"].startswith("~")
    assert raw2["sessions_dir"].startswith(str(tmp_path))


def test_load_expands_tilde_in_sessions_dir(xdg):
    cfg = config.default_config()
    cfg.sessions_dir = xdg / "custom_sessions"
    config.save_config(cfg)

    loaded = config.load_config()
    # Re-loaded path must be absolute (tilde / env expanded by the validator).
    assert loaded.sessions_dir.is_absolute()
    assert loaded.sessions_dir == (xdg / "custom_sessions").resolve()


def test_load_config_missing_raises_click_exception(xdg):
    with pytest.raises(click.ClickException) as exc_info:
        config.load_config()
    assert "rec setup" in exc_info.value.message.lower()


def test_load_config_corrupt_raises(xdg):
    config.config_path().parent.mkdir(parents=True, exist_ok=True)
    config.config_path().write_text("{ not json")
    with pytest.raises(click.ClickException):
        config.load_config()


def test_load_config_tolerates_legacy_blackhole_fields(xdg):
    """A config written by the old BlackHole version must still load.

    The legacy fields (original_output_device etc.) are silently dropped so
    users don't have to re-run setup after upgrading.
    """
    config.config_path().parent.mkdir(parents=True, exist_ok=True)
    config.config_path().write_text(json.dumps({
        "original_output_device": "MacBook Pro Speakers",
        "multi_output_device": "Multi-Output Device",
        "blackhole_device": "BlackHole 2ch",
        "sample_rate": 44100,
        "channels": 1,
        "whisper_model": "base",
        "sessions_dir": "~/.local/share/rec/sessions",
    }))
    cfg = config.load_config()
    assert cfg.whisper_model == "base"
    # Legacy rate is honored (we don't silently rewrite it).
    assert cfg.sample_rate == 44100
    assert not hasattr(cfg, "blackhole_device")
