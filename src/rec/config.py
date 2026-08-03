"""Configuration: Pydantic model + XDG paths + load/save.

XDG layout:
  ~/.config/rec/config.json   — settings (saved by `rec setup`)
  ~/.config/rec/rec.pid       — PID of the live recording daemon
  ~/.local/share/rec/sessions/{id}/ — per-session wav + metadata + transcript
  ~/.local/share/rec/logs/rec.log   — global log

Audio capture uses macOS Core Audio taps (via the `audiotap` library) — no
BlackHole, no Multi-Output Device, no device switching. The recorder taps
system output directly.
"""

from __future__ import annotations

import os
from pathlib import Path

import click
from pydantic import BaseModel, Field, field_validator

from .log import get_logger

log = get_logger(__name__)


def _config_home() -> Path:
    """XDG config home (~/.config/rec)."""
    base = os.environ.get("XDG_CONFIG_HOME")
    root = Path(base) if base else Path.home() / ".config"
    return root / "rec"


def _data_home() -> Path:
    """XDG data home (~/.local/share/rec)."""
    base = os.environ.get("XDG_DATA_HOME")
    root = Path(base) if base else Path.home() / ".local" / "share"
    return root / "rec"


def config_path() -> Path:
    return _config_home() / "config.json"


def pid_path() -> Path:
    return _config_home() / "rec.pid"


def sessions_root() -> Path:
    return _data_home() / "sessions"


class RecConfig(BaseModel):
    """Persisted recorder settings.

    Audio routing fields (original_output_device, multi_output_device,
    blackhole_device) were removed when we switched from the BlackHole +
    Multi-Output approach to Core Audio taps. There is no device to save or
    restore — the tap reads system output directly.
    """

    # 16000 Hz mono float32 is Whisper's native input format: no resampling,
    # smallest files (~7.5 MB/min), best transcription accuracy.
    sample_rate: int = 16000
    channels: int = 1
    whisper_model: str = "base"
    # Reserved for future "system+mic" capture. Today only "system" is wired.
    capture: str = "mic+system"
    sessions_dir: Path = Field(default_factory=sessions_root)

    @field_validator("sessions_dir", mode="before")
    @classmethod
    def _expand_sessions_dir(cls, v):
        if v is None or v == "":
            return sessions_root()
        p = Path(str(v))
        # Expand a literal '~' or '~user' and any env vars.
        return Path(os.path.expandvars(os.path.expanduser(str(p)))).expanduser()

    def model_dump_jsonable(self) -> dict:
        """JSON-serializable dict (Paths -> str with '~' preserved where possible)."""
        home = str(Path.home())
        sessions = str(self.sessions_dir)
        if sessions.startswith(home):
            sessions = "~" + sessions[len(home):]
        return {
            "sample_rate": self.sample_rate,
            "channels": self.channels,
            "whisper_model": self.whisper_model,
            "capture": self.capture,
            "sessions_dir": sessions,
        }


def default_config() -> RecConfig:
    """A config with the recommended defaults (no device-specific state)."""
    return RecConfig()


def save_config(cfg: RecConfig) -> Path:
    """Write config.json, creating parent dirs. Returns the path written."""
    import json

    path = config_path()
    log.debug("saving config to %s (model=%s rate=%d ch=%d capture=%s)",
              path, cfg.whisper_model, cfg.sample_rate, cfg.channels, cfg.capture)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cfg.model_dump_jsonable(), indent=2) + "\n", encoding="utf-8")
    log.info("config saved: %s", path)
    return path


def load_config() -> RecConfig:
    """Load config; raises click.ClickException if not set up yet."""
    import json

    path = config_path()
    log.debug("loading config from %s", path)
    if not path.exists():
        log.warning("config missing at %s — needs 'rec setup'", path)
        raise click.ClickException(
            "No config found. Run 'rec setup' first to configure audio capture."
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        log.warning("config at %s is corrupt (%r)", path, e)
        raise click.ClickException(f"Could not read config at {path}: {e}") from e
    # Tolerate legacy config files that still carry the old BlackHole fields.
    legacy_fields = {"original_output_device", "multi_output_device", "blackhole_device"}
    dropped = {k: v for k, v in data.items() if k in legacy_fields}
    if dropped:
        log.info("ignoring %d legacy BlackHole config field(s): %s", len(dropped), list(dropped))
    data = {k: v for k, v in data.items() if k not in legacy_fields}
    cfg = RecConfig(**data)
    log.info("config loaded: %s (model=%s rate=%d)", path, cfg.whisper_model, cfg.sample_rate)
    return cfg
