"""Runtime environment gate — fail fast and loudly on unsupported platforms.

`rec` depends on two things pip cannot enforce:

  1. **macOS 14.2+** — Core Audio process taps (the API `audiotap` wraps) only
     landed in macOS 14.2 (Sonoma). On older macOS, or on Linux/Windows, there
     is nothing to tap and every command would fail deep inside the recorder.
  2. **The `audiotap` C extension + its bundled dylib** — a wheel can install
     fine yet carry a dylib that won't load (wrong arch, corrupt download,
     missing system libs). Better to say so before recording than mid-meeting.

So before any real command runs we probe the environment and, if it is
unsupported, raise a `click.ClickException` with a one-line actionable message.
`cli.main()` catches that and prints `Error: <message>` with exit code 1 — no
traceback, no mystery. `rec --version` and `rec --help` skip this gate so they
work everywhere (useful for "what version is this broken install?").
"""

from __future__ import annotations

import platform

import click

# Core Audio process taps require macOS 14.2 (Sonoma). Anything older cannot
# capture system audio the way audiotap needs — we refuse to even try rather
# than hand the user a silent or crashing recorder.
MIN_MACOS: tuple[int, int] = (14, 2)


def _macos_version() -> tuple[int, int]:
    """The running macOS version as a (major, minor) tuple.

    Returns (0, 0) off macOS (platform.mac_ver returns '' on Linux/Windows) or
    if the version string can't be parsed — both cases are treated as unsupported.
    """
    if platform.system() != "Darwin":
        return (0, 0)
    try:
        parts = platform.mac_ver()[0].split(".")
        return int(parts[0]), int(parts[1]) if len(parts) > 1 else 0
    except (ValueError, IndexError):  # pragma: no cover — defensive
        return (0, 0)


def _audiotap_usable() -> bool:
    """True if `audiotap` imports AND its bundled dylib is locatable.

    Mirrors the check `rec setup` performs. A wheel can install yet carry a
    dylib that won't load (wrong arch, corrupt download); `_find_library()`
    returning a falsy value or raising means capture can't work.
    """
    try:
        import audiotap  # noqa: F401
        from audiotap import _bindings

        return bool(_bindings._find_library())
    except Exception:
        return False


def check_runtime() -> None:
    """Abort with an actionable message if this environment can't record.

    Raises `click.ClickException` (caught by `cli.main` → printed cleanly, exit
    code 1, no traceback). Safe to call on every command; each probe is cheap
    and `import audiotap` is cached after the first call.
    """
    # 1. Wrong OS — audiotap is macOS-only.
    if platform.system() != "Darwin":
        raise click.ClickException(
            "rec only runs on macOS 14.2 or later (this is "
            f"{platform.system() or 'an unknown OS'})."
        )

    # 2. macOS too old — Core Audio process taps need 14.2+.
    major, minor = _macos_version()
    if (major, minor) < MIN_MACOS:
        raise click.ClickException(
            f"rec requires macOS 14.2 or later (you have {platform.mac_ver()[0] or 'an unknown version'}). "
            "Core Audio process taps — the API it records through — are unavailable below 14.2."
        )

    # 3. audiotap's C extension / bundled dylib won't load.
    if not _audiotap_usable():
        raise click.ClickException(
            "rec could not load the `audiotap` audio library or its native "
            "component. Try reinstalling with `pip install --force-reinstall audiotap`, "
            "or run `rec setup` for a fuller diagnostic."
        )
