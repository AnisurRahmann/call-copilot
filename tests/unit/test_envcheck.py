"""Tests for the runtime environment gate (rec.envcheck).

The gate runs on every real command and aborts cleanly (no traceback) when the
machine can't record: wrong OS, macOS < 14.2, or audiotap's native lib won't
load. `rec --version` / `rec --help` must bypass it so they work everywhere.
"""

from __future__ import annotations

import click
import pytest

from rec import cli, envcheck


def _expect_failure(monkeypatch, *, setup_env, message_contains):
    """Configure the environment then assert check_runtime() aborts cleanly."""
    setup_env(monkeypatch)
    with pytest.raises(click.ClickException) as exc_info:
        envcheck.check_runtime()
    msg = exc_info.value.message
    assert message_contains in msg, f"expected {message_contains!r} in {msg!r}"


def test_non_darwin_fails(monkeypatch):
    """On Linux/Windows the gate refuses with an actionable message."""
    monkeypatch.setattr(envcheck.platform, "system", lambda: "Linux")
    _expect_failure(
        monkeypatch,
        setup_env=lambda mp: None,  # system already patched above
        message_contains="macOS 14.2",
    )


def test_old_macos_fails(monkeypatch):
    """macOS below 14.2 is rejected with the version it needs."""
    monkeypatch.setattr(envcheck.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(envcheck.platform, "mac_ver", lambda: ("13.5.1", "", ""))
    with pytest.raises(click.ClickException) as exc_info:
        envcheck.check_runtime()
    assert "14.2" in exc_info.value.message
    assert "13.5.1" in exc_info.value.message


def test_macos_14_2_boundary_passes(monkeypatch):
    """Exactly 14.2 is supported (inclusive lower bound)."""
    monkeypatch.setattr(envcheck.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(envcheck.platform, "mac_ver", lambda: ("14.2.0", "", ""))
    # audiotap is real + usable on this machine, so the full check passes.
    envcheck.check_runtime()  # no exception


def test_macos_15_passes(monkeypatch):
    """A clearly-supported macOS version passes the version check."""
    monkeypatch.setattr(envcheck.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(envcheck.platform, "mac_ver", lambda: ("15.1.0", "", ""))
    envcheck.check_runtime()


def test_audiotap_unusable_fails(monkeypatch):
    """macOS is fine but audiotap won't load -> actionable reinstall hint."""
    monkeypatch.setattr(envcheck.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(envcheck.platform, "mac_ver", lambda: ("14.4.0", "", ""))

    def fake_usable():
        return False

    monkeypatch.setattr(envcheck, "_audiotap_usable", fake_usable)
    with pytest.raises(click.ClickException) as exc_info:
        envcheck.check_runtime()
    assert "audiotap" in exc_info.value.message
    # The message points the user at a fix, not just the problem.
    assert "reinstall" in exc_info.value.message.lower() or "setup" in exc_info.value.message.lower()


# ---- CLI integration: the gate is wired in, --version/--help bypass it --------


def test_version_bypasses_gate(monkeypatch):
    """`rec --version` runs even when the environment is unsupported."""
    # Sabotage the environment so the gate WOULD fire.
    monkeypatch.setattr(envcheck.platform, "system", lambda: "Linux")
    res = cli.main(["--version"])
    assert res == 0  # did not abort


def test_help_bypasses_gate(monkeypatch):
    """`rec --help` runs even when the environment is unsupported."""
    monkeypatch.setattr(envcheck.platform, "system", lambda: "Linux")
    res = cli.main(["--help"])
    assert res == 0


def test_real_command_aborts_on_unsupported_os(monkeypatch, capsys):
    """A real subcommand on an unsupported OS prints a one-liner, no traceback."""
    monkeypatch.setattr(envcheck.platform, "system", lambda: "Linux")
    rc = cli.main(["list"])
    assert rc != 0
    captured = capsys.readouterr()
    # Clean, human-readable error on stderr — not a Python traceback.
    assert "macOS 14.2" in captured.err
    assert "Traceback" not in captured.err
