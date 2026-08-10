"""rec — Click CLI entry point.

Commands:
  rec setup         verify macOS + audiotap + capture permission (one-time)
  rec start         start recording system audio in the background
  rec stop          stop, transcribe, save markdown
  rec list          show past sessions
  rec status        show live recording status
  rec transcribe    re-transcribe an existing recording
  rec diagnose      bundle debug info for one session into a file for an AI agent

Audio capture uses macOS Core Audio taps (via the `audiotap` library). There is
no BlackHole, no Multi-Output Device, and no system-output device switching —
the tap reads system output directly, so `rec start`/`stop` never touch the
user's audio routing.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import signal
import subprocess
import tempfile
import time
from datetime import datetime
from pathlib import Path

import click
import rich.box
from rich.console import Console
from rich.status import Status
from rich.table import Table

from . import __version__, audio_check, config, envcheck, formatter, recorder, session, transcriber
from . import log as log_mod

console = Console()


# ---- interactive prompt seams (Step 3) ------------------------------------
# These are the single points that touch stdin / TTY detection, isolated so
# every prompt test monkeypatches them rather than faking a TTY. Under CliRunner
# sys.stdin.isatty() is always False, which is why _is_interactive is a seam.

DEFAULT_PROMPT_TIMEOUT_S = 60.0


def _is_interactive() -> bool:
    """True if stdin is a TTY (so a prompt won't block forever).

    A seam (not sys.stdin.isatty() inline) so tests can flip it: CliRunner makes
    isatty() always False, so without this the interactive path is untestable.
    """
    import sys
    return bool(sys.stdin.isatty())


def prompt_yes_no(question: str, *, default: bool, timeout_s: float) -> bool:
    """Ask a yes/no question on stdin, returning ``default`` on Enter.

    The single function that touches stdin. It owns the KeyboardInterrupt and
    EOFError conversions internally — a Ctrl+C at the prompt is treated as the
    default (NOT a crash), so "at the prompt → exit 0" is achievable. Every
    prompt test monkeypatches this.

    A timeout (no answer in ``timeout_s`` seconds) returns ``default`` too —
    silence does not consent. The timeout is a seam (not a literal 60s) so tests
    can set it to ~0.
    """
    import select
    import sys
    hint = "[Y/n]" if default else "[y/N]"
    sys.stdout.write(f"{question} {hint}\n")
    sys.stdout.flush()
    try:
        ready, _, _ = select.select([sys.stdin], [], [], timeout_s)
        if not ready:
            return default  # timeout → treat as default (silence ≠ yes)
        line = sys.stdin.readline().strip().lower()
    except (KeyboardInterrupt, EOFError):
        # Ctrl+C / closed stdin at the prompt → default, not a crash.
        return default
    if line == "":
        return default
    return line in ("y", "yes")


def _install_stop_handler() -> None:
    """Install the first-SIGINT-stops-recording handler (Step 3).

    First Ctrl+C sets a flag asking the recording loop to stop normally; the
    handler then restores Python's default disposition so a SECOND Ctrl+C raises
    KeyboardInterrupt (handled by the phase it lands in). Restore
    ``default_int_handler`` (which raises), NOT ``SIG_DFL`` (which hard-kills
    with no finally — reintroducing the Commit A wedge bug).
    """
    global _stop_requested
    _stop_requested = False
    signal.signal(signal.SIGINT, _stop_handler)


def _stop_handler(signum, frame):  # pragma: no cover — signal delivery is manual-test
    """First-SIGINT handler: set the stop flag, restore default disposition."""
    global _stop_requested
    _stop_requested = True
    signal.signal(signal.SIGINT, signal.default_int_handler)


_stop_requested = False


def _setup_logging_for_run(verbose: int, quiet: bool, command: str | None) -> None:
    """Configure logging + stamp command context. Called once per CLI run."""
    level = log_mod.verbosity_to_level(verbose, quiet)
    log_mod.configure_logging("cli", console_level=level)
    log_mod.set_command_context(command)
    from .log import get_logger
    get_logger(__name__).info("rec %s invoked (verbose=%d quiet=%s console_level=%s)",
                              command, verbose, quiet, logging_level_name(level))


def logging_level_name(level: int) -> str:
    import logging as _l
    return _l.getLevelName(level)


@click.group()
@click.version_option(__version__, prog_name="rec")
@click.option("-v", "--verbose", count=True, help="Increase console log verbosity (-v INFO, -vv DEBUG).")
@click.option("--quiet", is_flag=True, help="Suppress console logging (only warnings+ stay).")
@click.pass_context
def cli(ctx: click.Context, verbose: int, quiet: bool) -> None:
    """Call Copilot — silently record meeting audio, then transcribe locally to markdown."""
    # Fail fast before any real work if this machine can't record. Click
    # processes --version/--help during option parsing and never calls this
    # callback, so they still work on unsupported systems (handy for "what
    # version is this broken install?"). cli.main() catches the ClickException
    # and prints a clean one-line error with exit code 1 — no traceback.
    #
    # The read-only commands skip this gate: `rec mcp` / `rec index` only read
    # transcripts on disk and have nothing to do with audiotap/Core Audio, so
    # they must run on any machine that has transcripts to read (including a
    # non-Mac where recordings were copied in, or in CI).
    _READ_ONLY_COMMANDS = {"mcp", "index", "web", "summarize"}
    if ctx.invoked_subcommand not in _READ_ONLY_COMMANDS:
        envcheck.check_runtime()
    _setup_logging_for_run(verbose, quiet, ctx.invoked_subcommand)


def main(argv: list[str] | None = None) -> int:
    """Entry point. Wraps the Click group so EVERY failure is logged.

    Every ClickException (user-facing error) is logged at ERROR without a
    traceback. Any other exception is a real bug — logged at ERROR with the
    full traceback so it's capturable for debugging. Exit code is always
    recorded.
    """
    from .log import get_logger
    log = get_logger(__name__)
    exit_code = 0
    try:
        cli.main(args=argv, standalone_mode=False, prog_name="rec")
    except click.exceptions.Abort:
        log.error("aborted by user (Ctrl-C)")
        click.echo("Aborted.", err=True)
        exit_code = 1
    except click.ClickException as e:
        log.error("command failed: %s (exit=%d)", e.message, e.exit_code)
        click.echo(f"Error: {e.message}", err=True)
        exit_code = e.exit_code
    except SystemExit as e:  # pragma: no cover — defensive
        exit_code = int(e.code) if isinstance(e.code, int) else 1
        log.error("unexpected SystemExit: code=%s", e.code)
    except Exception as e:  # real bug — full traceback to the log
        log.exception("unhandled exception: %r", e)
        click.echo(f"Error: {e}", err=True)
        exit_code = 1
    else:
        log.info("rec command completed (exit=0)")
    return exit_code


# ---- setup -----------------------------------------------------------------


def _macos_version_tuple() -> tuple[int, int]:
    """Parse macOS version (e.g. '14.4.1') into a (major, minor) tuple."""
    try:
        parts = platform.mac_ver()[0].split(".")
        return int(parts[0]), int(parts[1]) if len(parts) > 1 else 0
    except (ValueError, IndexError):  # pragma: no cover — defensive
        return 0, 0


@cli.command()
@click.option(
    "--model",
    "default_model",
    default="base",
    show_default=True,
    help="Default whisper model for transcription (tiny/base/small/medium/large-v3).",
)
@click.option(
    "--selftest/--no-selftest",
    "selftest",
    default=True,
    help="Run a live capture probe (default on) — opens the taps for a couple "
         "of seconds to confirm they actually receive audio. Pass --no-selftest "
         "to skip in CI/automated runs.",
)
@click.option(
    "--selftest-duration",
    type=float,
    default=2.0,
    show_default=True,
    help="Seconds to open each tap during the capture self-test.",
)
def setup(default_model: str, selftest: bool, selftest_duration: float) -> None:
    """One-time setup: verify macOS + audiotap + capture permission + a live tap."""
    log = log_mod.get_logger(__name__)
    log.info("running setup (default_model=%s selftest=%s)", default_model, selftest)

    # 1. macOS version — Core Audio taps require 14.2+.
    major, minor = _macos_version_tuple()
    ok = (major, minor) >= (14, 2)
    click.echo(f"[{'ok' if ok else 'FAIL'}] macOS {platform.mac_ver()[0]} "
               f"({'sufficient — Core Audio taps supported' if ok else 'needs 14.2+ for Core Audio taps'})")
    if not ok:
        raise click.ClickException("macOS 14.2 or later is required for Core Audio taps.")

    # 2. audiotap library loads + the bundled dylib is present.
    try:
        import audiotap  # noqa: F401
        from audiotap import _bindings
        dylib = _bindings._find_library()
        click.echo(f"[ok] audiotap importable (dylib: {dylib})")
        log.info("audiotap OK, dylib=%s", dylib)
    except Exception as e:
        click.echo(f"[FAIL] audiotap not usable: {e}")
        raise click.ClickException(f"audiotap library not usable: {e}") from e

    # 3. Persist config with the chosen defaults.
    cfg = config.RecConfig(whisper_model=default_model)
    path = config.save_config(cfg)
    click.echo(f"[ok] Config saved: {path} (default capture: {cfg.capture})")

    # 4. Microphone permission check (for the default mic+system capture).
    click.echo("")
    try:
        import audiotap
        mic_status = audiotap.mic_permission_status().name
    except Exception:
        mic_status = "UNKNOWN"
    if mic_status == "GRANTED":
        click.echo("[ok] Microphone permission: GRANTED")
    else:
        click.echo(f"[--] Microphone permission: {mic_status} (needed to record YOUR voice)")
        click.echo("  Grant it in System Settings > Privacy & Security > Microphone,")
        click.echo("  for the terminal app you run `rec` from (e.g. Warp). Until granted,")
        click.echo("  `rec start` records system audio only and skips the mic.")

    # 5. System-audio capture permission.
    click.echo("")
    click.echo("System audio capture permission:")
    click.echo("  macOS will prompt for permission the first time you run `rec start`.")
    click.echo("  Grant it under System Settings > Privacy & Security > Screen Recording")
    click.echo("  (system audio capture is grouped with screen recording).")
    click.echo("  After toggling it ON for your terminal app, QUIT and reopen the app.")

    # 6. Live capture self-test — the most important check. A tap that runs
    # but returns all-zero samples is the #1 silent-recording cause, and it's
    # invisible without actually opening the tap. This catches a permission
    # that's checked but not honored (needs an app restart) before a meeting.
    click.echo("")
    if not selftest:
        click.echo("[skipped] live capture self-test (--no-selftest).")
        click.echo("Setup complete. Run `rec start` to begin recording.")
        return

    _run_capture_selftest(cfg, selftest_duration)


def _run_capture_selftest(cfg: config.RecConfig, duration: float) -> None:
    """Open each tap in the configured capture for `duration` and report peak.

    The system probe needs the user to play something during the window; the
    mic probe needs them to speak. We can't force either, so we PRINT the
    instructions, wait the configured duration, then judge the result — a
    near-zero peak gets a clear FAIL with the restart-app guidance.
    """
    sources = sorted(recorder._parse_capture(cfg.capture))
    problems = False
    for src in sources:
        if src == "system":
            click.echo(
                f"[..] Probing SYSTEM audio for {duration:.0f}s — play some audio NOW "
                f"(music, a video, `say hello`) so the tap has something to capture."
            )
        else:
            click.echo(
                f"[..] Probing MICROPHONE for {duration:.0f}s — speak NOW so the tap "
                f"has something to capture."
            )
        probe = recorder.probe_capture(src, duration)
        if not probe.created:
            click.secho(
                f"[FAIL] {src} tap could not be created: {probe.error}",
                fg="red",
            )
            problems = True
            continue
        if probe.peak < audio_check.SILENCE_PEAK_THRESHOLD:
            click.secho(
                f"[FAIL] {src} tap captured only silence "
                f"(peak={probe.peak:.6f}, {probe.frames:,} frames). The permission "
                f"is checked but macOS is handing the running process zero buffers. "
                f"Toggle the permission ON, then FULLY QUIT and reopen this terminal "
                f"app, then run `rec setup` again.",
                fg="red",
            )
            problems = True
        else:
            click.secho(
                f"[ok] {src} tap captured audio (peak={probe.peak:.4f}, "
                f"{probe.frames:,} frames).",
                fg="green",
            )

    click.echo("")
    if problems:
        click.secho(
            "Setup incomplete — at least one capture source is silent. Fix the "
            "permissions above (and restart the terminal app) before recording.",
            fg="yellow",
        )
    else:
        click.echo("Setup complete. Run `rec start` to begin recording.")


# ---- start -----------------------------------------------------------------


@cli.command()
@click.option(
    "--model",
    default=None,
    help="Override whisper model for this transcription (e.g. medium, large-v3).",
)
@click.option(
    "--vad/--no-vad",
    default=False,
    help="Enable voice-activity-detection pre-filter (default off).",
)
@click.option(
    "--detach",
    is_flag=True,
    help="Start in the background and exit immediately (the old behavior). Stop with `rec stop`.",
)
@click.option(
    "--system-only",
    "system_only",
    is_flag=True,
    help="Capture only system audio (what apps play), not the microphone.",
)
@click.option(
    "--mic-only",
    "mic_only",
    is_flag=True,
    help="Capture only the microphone (your voice), not system audio.",
)
@click.option(
    "--summarize/--no-summarize",
    "summarize_flag",
    default=None,
    help="After transcription, summarise (--summarize) or skip (--no-summarize). "
         "Without a flag, the summarize.auto config governs (default: ask).",
)
def start(model: str | None, vad: bool, detach: bool, system_only: bool, mic_only: bool,
          summarize_flag: bool | None) -> None:
    """Start recording. Shows a live indicator; press Ctrl+C to stop & transcribe."""
    if system_only and mic_only:
        raise click.ClickException("--system-only and --mic-only are mutually exclusive.")
    # Guard: already recording?
    if recorder.active_pid() is not None:
        raise click.ClickException("Already recording. Run 'rec stop' first.")

    cfg = config.load_config()
    capture = _resolve_capture(cfg, system_only, mic_only)

    # If the mic is part of the capture but its permission isn't granted, warn
    # up front (in the terminal) — the recorder would otherwise skip it
    # silently and the warning would only land in the daemon log. The user
    # thinks they're recording both sources when they're really only getting
    # system audio.
    if "mic" in _parse_capture_for_cli(capture) and not _mic_permission_granted():
        click.secho(
            "Microphone permission is not granted — the mic will be SKIPPED and "
            "only system audio will be recorded. Grant microphone access for "
            "this terminal app in System Settings > Privacy & Security > "
            "Microphone, then quit + reopen the app. Or record system audio "
            "only with `rec start --system-only`.",
            fg="yellow", err=True,
        )

    session_id = session.new_session_id()
    log_mod.set_session_context(session_id)
    session.create_session_dir(session_id)
    session.update_meta(
        session_id,
        started_at=datetime.now().isoformat(timespec="seconds"),
        status=session.STATUS_RECORDING,
    )

    try:
        recorder.spawn_recorder(session_id, cfg.sample_rate, cfg.channels, capture)
    except Exception as e:
        raise click.ClickException(f"Failed to start recording: {e}") from e

    if detach:
        # Background mode: spawn + exit. Stop later with `rec stop`.
        click.echo(f"Recording started (session {session_id}). Run 'rec stop' when done.")
        return

    # Foreground mode: show a live recording indicator until Ctrl+C, then stop
    # + transcribe in the same command. This is the default UX — you see that
    # recording is in progress and can stop it without a second terminal.
    flag = None
    if summarize_flag is True:
        flag = "yes"
    elif summarize_flag is False:
        flag = "no"
    _run_live_recording(
        cfg, session_id, model_override=model, vad_filter=vad, summarize_flag=flag,
    )


def _run_live_recording(
    cfg: config.RecConfig,
    session_id: str,
    *,
    model_override: str | None,
    vad_filter: bool,
    summarize_flag: str | None = None,
) -> None:
    """Show a live ● REC indicator (elapsed + file size) until Ctrl+C, then finish."""
    from rich.live import Live
    from rich.text import Text

    meta = session.load_meta(session_id)
    started_str = meta.started_at if meta and meta.started_at else datetime.now().isoformat(timespec="seconds")
    try:
        started = datetime.fromisoformat(started_str)
    except ValueError:
        started = datetime.now()
    wav = session.wav_path(session_id)

    def render() -> Text:
        elapsed = datetime.now() - started
        secs = int(elapsed.total_seconds())
        h, rem = divmod(secs, 3600)
        m, s = divmod(rem, 60)
        time_str = f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"
        size_str = _format_bytes(wav.stat().st_size) if wav.exists() else "0 B"
        return Text.from_markup(
            f"[bold red]● REC[/]  [cyan]{session_id}[/]\n"
            f"elapsed [bold]{time_str}[/]   size [bold]{size_str}[/]\n"
            f"[dim]press Ctrl+C to stop & transcribe[/]"
        )

    try:
        # refresh_per_second=2 keeps elapsed/size fresh without burning CPU.
        # transient=True clears the indicator on exit so it doesn't clutter logs.
        # Install the first-SIGINT-stops handler: the first Ctrl+C sets
        # _stop_requested and restores the default disposition; the loop exits
        # cleanly, then transcription/prompt run under the default disposition.
        _install_stop_handler()
        with Live(render(), console=console, refresh_per_second=2, transient=True) as live:
            while recorder.active_pid() is not None:
                live.update(render())
                if _stop_requested:
                    break
                time.sleep(0.5)
        # Always restore the default disposition before leaving the loop, so a
        # Ctrl+C during transcription/prompt raises KeyboardInterrupt normally.
        signal.signal(signal.SIGINT, signal.default_int_handler)
    except KeyboardInterrupt:
        # Expected stop path: Ctrl+C (or the belt-and-braces path). Fall through.
        signal.signal(signal.SIGINT, signal.default_int_handler)
        console.print()  # newline after the cleared live display
    else:
        # The daemon died on its own (crash / system sleep). Salvage what we have.
        signal.signal(signal.SIGINT, signal.default_int_handler)
        console.print("\n[yellow](recorder exited unexpectedly — salvaging partial audio.)[/]")

    # Stop the daemon FIRST (no-op if already dead): it drains + closes the WAV
    # under SIGTERM. The daemon runs in its own session (start_new_session=True),
    # so Ctrl+C did NOT propagate to it — without this call it would keep
    # recording as an orphan and _finish_session would read a half-written file.
    # Then run the silence-check + transcription. Both can take a few seconds,
    # so each shows a spinner so the user knows the process is working.
    _stop_recorder_with_status()
    _finish_session_with_status(
        cfg, session_id, model_override=model_override, vad_filter=vad_filter
    )
    # After a successful transcription, maybe offer to summarise (Step 3). The
    # prompt only fires at the TRANSCRIBED exit — silent/aborted sessions skip it.
    _maybe_prompt_summarize(cfg, session_id, summarize_flag=summarize_flag)


# ---- summarize-on-stop resolution (Step 3) --------------------------------


def _maybe_prompt_summarize(
    cfg: config.RecConfig, session_id: str, *, summarize_flag: str | None
) -> None:
    """Offer to summarise after transcription, per the resolution order.

    Fires only when the session reached STATUS_TRANSCRIBED with a transcript on
    disk. Resolution (see STEP3_SUMMARIES.md §resolution):
      --summarize + provider  -> summarise, no prompt
      --summarize + no provider -> error (explicit intent)
      --no-summarize          -> skip, always
      auto: always/never/ask  -> as named
      no provider, no flag    -> no prompt at all
      non-interactive         -> never prompt; "ask" behaves as "never"
    """
    meta = session.load_meta(session_id)
    if meta is None or meta.status != session.STATUS_TRANSCRIBED:
        return  # silent / aborted / not-yet-transcribed — nothing to summarise
    if not session.transcript_path(session_id).exists():
        return

    summ = dict(cfg.summarize or {})
    auto = summ.get("auto", "ask")
    pname = summ.get("provider")

    # Explicit flag wins.
    if summarize_flag == "no":
        return
    if summarize_flag == "yes":
        if not pname:
            raise click.ClickException(
                "--summarize was given but no provider is configured. "
                "Set summarize.provider in your config and the relevant API-key "
                "env var, then re-run."
            )
        _run_summarize_after_transcription(cfg, session_id)
        return

    # No flag → governed by auto + provider + interactivity.
    if not pname:
        return  # offline by default: no provider, no prompt, no message
    if auto == "never":
        return
    if auto == "always":
        _run_summarize_after_transcription(cfg, session_id)
        return
    # auto == "ask" (default)
    if not _is_interactive():
        return  # non-TTY → never prompt; "ask" behaves as "never"
    timeout_s = float(summ.get("prompt_timeout_s", DEFAULT_PROMPT_TIMEOUT_S))
    words = meta.word_count or 0
    click.echo(f"\nTranscript: {session.transcript_path(session_id)} ({words:,} words)")
    if not prompt_yes_no("Summarise this meeting?", default=True, timeout_s=timeout_s):
        click.echo(f"(skipped — run `rec summarize {session_id}` later to summarise)")
        return
    _run_summarize_after_transcription(cfg, session_id)


def _run_summarize_after_transcription(cfg: config.RecConfig, session_id: str) -> None:
    """Run the summarise pipeline after a transcription, reusing _summarize_command."""
    _summarize_command(
        session_id, template_name="default", template_file=None,
        provider_name=None, tier1_model=None, tier2_model=None, tier3_model=None,
        dry_run=False, force=True, yes=True, api_key_env=None,
    )


@cli.command()
@click.option(
    "--model",
    default=None,
    help="Override whisper model for this transcription (e.g. medium, large-v3).",
)
@click.option(
    "--vad/--no-vad",
    default=False,
    help="Enable voice-activity-detection pre-filter (default off; see transcriber docs).",
)
@click.option(
    "--summarize/--no-summarize",
    "summarize_flag",
    default=None,
    help="After transcription, summarise (--summarize) or skip (--no-summarize). "
         "Without a flag, the summarize.auto config governs (default: ask).",
)
def stop(model: str | None, vad: bool, summarize_flag: bool | None) -> None:
    """Stop recording, transcribe, and save the markdown transcript."""
    cfg = config.load_config()

    # Determine the session we're stopping (most recent recording).
    pid = recorder.active_pid()
    session_id = _current_recording_session_id()
    if session_id is None:
        raise click.ClickException("No active recording to stop.")

    was_alive, _ = _stop_recorder_with_status()
    if not was_alive and pid is not None:
        click.echo("(recorder process had already exited — salvaging partial audio.)")

    _finish_session_with_status(cfg, session_id, model_override=model, vad_filter=vad)
    flag = None
    if summarize_flag is True:
        flag = "yes"
    elif summarize_flag is False:
        flag = "no"
    _maybe_prompt_summarize(cfg, session_id, summarize_flag=flag)


def _stop_recorder_with_status() -> tuple[bool, int | None]:
    """Stop the recorder daemon while showing a spinner.

    Wraps recorder.stop_recorder(): the daemon drains its in-flight audio and
    closes the WAV on SIGTERM, which can take up to STOP_TIMEOUT_S (5s). The
    spinner makes that wait visible instead of looking like a hang.
    """
    with Status("[cyan]Stopping recorder…[/]", console=console, spinner="dots"):
        return recorder.stop_recorder()


def _finish_session_with_status(
    cfg: config.RecConfig,
    session_id: str,
    *,
    model_override: str | None,
    vad_filter: bool,
) -> None:
    """Finish a session (silence check + transcription) with a single spinner.

    Without feedback the terminal is silent for several seconds after Ctrl+C
    while the audio levels are analyzed (soundfile read + numpy) and faster-
    whisper is loaded lazily on first use — which reads as a hang. This shows
    one "working" spinner for the silence-check phase. It is stopped before any
    result/warning text prints, and before transcription, at which point the
    transcriber's own Rich progress bar takes over the display.
    """
    with Status("[cyan]Analyzing captured audio…[/]", console=console, spinner="dots") as status:
        _finish_session(
            cfg, session_id,
            model_override=model_override, vad_filter=vad_filter,
            status=status,
        )


def _finish_session(
    cfg: config.RecConfig,
    session_id: str,
    *,
    model_override: str | None,
    vad_filter: bool,
    status: Status | None = None,
) -> None:
    """Run the silence check + transcription for a just-stopped session.

    Shared by `rec stop` (finds the active session itself) and `rec start`
    (knows its session id after Ctrl+C). Assumes the recorder daemon has
    already been signaled to stop.

    If every captured WAV is silent, transcription is SKIPPED: Whisper on pure
    silence hallucinates text (e.g. a repeated "You"), which is worse than no
    transcript. The session is marked SILENT instead. The WAV is kept on disk
    so `rec diagnose` can inspect it. If only one source of a mic+system
    recording is silent, the other still gets transcribed.

    `status` is an optional rich.status.Status shown by the caller during this
    phase. It's stopped before any user-facing output so messages don't print
    under a still-spinning spinner, and before transcription so the
    transcriber's own progress bar can render cleanly.
    """
    log_mod.set_session_context(session_id)

    wav = session.wav_path(session_id)
    mic_wav = session.mic_wav_path(session_id)
    if not wav.exists() and not mic_wav.exists():
        # The daemon was stopped before it opened either WAV (very fast Ctrl+C,
        # or it crashed at startup). Nothing to transcribe — say so clearly
        # rather than crashing, and mark the session so `rec list` reflects it.
        if status is not None:
            status.stop()
        session.update_meta(session_id, status=session.STATUS_RECORDED, duration=0.0, word_count=0)
        click.secho(
            "No audio was captured (stopped too quickly, or the tap failed to start). "
            "Nothing to transcribe.",
            fg="yellow", err=True,
        )
        return

    # Check audio levels BEFORE transcribing. A silent recording means nothing
    # was playing (or the permission was revoked) — flag it loudly instead of
    # letting Whisper silently produce an empty transcript.
    sys_levels = audio_check.analyze_wav(wav) if wav.exists() else None
    mic_levels = audio_check.analyze_wav(mic_wav) if mic_wav.exists() else None
    meta = session.load_meta(session_id)
    if meta is not None:
        if sys_levels is not None:
            meta.extra["audio_peak"] = round(sys_levels.peak, 6)
            meta.extra["audio_rms"] = round(sys_levels.rms, 6)
            meta.extra["audio_silent"] = sys_levels.silent
        if mic_levels is not None:
            # Store the mic's levels separately so both are diagnosable.
            meta.extra["mic_audio_peak"] = round(mic_levels.peak, 6)
            meta.extra["mic_audio_rms"] = round(mic_levels.rms, 6)
            meta.extra["mic_audio_silent"] = mic_levels.silent
        session.save_meta(meta)

    # Silence warnings + transcription both print to the console; stop the
    # spinner so that output isn't drawn under it.
    if status is not None:
        status.stop()

    _warn_if_silent(sys_levels, mic_levels)

    # Skip transcription only if EVERY captured source is silent. In a
    # mic+system recording where the system tap went silent but the mic heard
    # the user, we still transcribe the mic.
    all_present_silent = all(
        lv.silent for lv in (sys_levels, mic_levels) if lv is not None
    )
    if all_present_silent:
        # Pick a representative duration for the record (longest capture).
        dur = max(
            (lv.duration_seconds for lv in (sys_levels, mic_levels) if lv is not None),
            default=0.0,
        )
        session.update_meta(
            session_id, status=session.STATUS_SILENT, duration=dur, word_count=0
        )
        click.secho(
            "Skipping transcription: the recording is silent. "
            "Whisper would hallucinate text on a zero-signal file, so nothing "
            "was transcribed. The WAV is kept for `rec diagnose`. "
            "Fix the capture (see warning above), then record again.",
            fg="yellow", err=True,
        )
        return

    _transcribe_session(session_id, cfg, model_override=model_override, vad_filter=vad_filter)


def _warn_if_silent(sys_levels, mic_levels) -> None:
    """Print a yellow warning for each silent source with tailored guidance."""
    for label, lv in (("System", sys_levels), ("Microphone", mic_levels)):
        if lv is None or not lv.silent:
            continue
        if lv.frames > 0 and lv.peak == 0.0:
            # The permission-revoked / not-yet-honored signature.
            click.secho(
                f"⚠  WARNING: {label} recording is silent "
                f"(peak={lv.peak:.6f}, rms={lv.rms:.6f}, {lv.frames:,} frames).\n"
                f"   The tap ran but captured literally zero samples. This is the\n"
                f"   macOS capture-permission signature: toggle the relevant\n"
                f"   permission ON for your terminal app (Screen Recording for\n"
                f"   system audio, Microphone for the mic) in System Settings >\n"
                f"   Privacy & Security, then FULLY QUIT and reopen the app. Then\n"
                f"   run `rec setup` to verify.",
                fg="yellow", err=True,
            )
        else:
            click.secho(
                f"⚠  WARNING: {label} recording is silent "
                f"(peak={lv.peak:.6f}, rms={lv.rms:.6f}).\n"
                f"   The tap captured no usable signal — either nothing was\n"
                f"   playing, or the permission was revoked. Run `rec setup`,\n"
                f"   then record again while audio is actually playing.",
                fg="yellow", err=True,
            )


# ---- list ------------------------------------------------------------------


@cli.command(name="list")
@click.option("--limit", type=int, default=50, show_default=True, help="Max rows.")
def list_(limit: int) -> None:
    """Show past sessions."""
    sessions = session.list_sessions()[:limit]
    if not sessions:
        click.echo("No sessions yet. Run 'rec start' to record one.")
        return

    table = Table(title="Sessions", box=rich.box.ROUNDED, show_lines=False)
    table.add_column("Session", style="cyan", no_wrap=True)
    table.add_column("Duration", justify="right", style="magenta")
    table.add_column("Words", justify="right")
    table.add_column("Status", justify="center")
    table.add_column("Health", justify="center")
    table.add_column("Model", justify="center", style="dim")

    for m in sessions:
        words = "—" if m.word_count is None else f"{m.word_count:,}"
        health = session.capture_health(m)
        # suspect/silent carry weight (yellow) — a failed capture is the one
        # thing a CLI user most needs to spot. ok is dim; unknown is muted.
        if health in ("suspect", "silent"):
            health_cell = f"[yellow]{health}[/]"
        elif health == "ok":
            health_cell = f"[dim]{health}[/]"
        else:
            health_cell = f"[dim]{health}[/]"
        table.add_row(
            session.started_at_display(m.id),
            session.format_duration_human(m.duration),
            words,
            m.status,
            health_cell,
            m.model or "",
        )

    console.print(table)


# ---- status ----------------------------------------------------------------


@cli.command()
def status() -> None:
    """Show whether a recording is in progress."""
    pid = recorder.active_pid()
    if pid is None:
        # Not capturing — but a transcription may still be running in the
        # background (web job pool, or a parallel `rec transcribe`). Surface it
        # so the machine doesn't look idle while Whisper is grinding.
        transcribing = _transcribing_session()
        if transcribing is not None:
            click.echo(f"Transcribing (session {transcribing}).")
        else:
            click.echo("Not recording.")
        return

    session_id = _current_recording_session_id()
    started = None
    size = None
    if session_id is not None:
        meta = session.load_meta(session_id)
        if meta and meta.started_at:
            try:
                started = datetime.fromisoformat(meta.started_at)
            except ValueError:
                started = None
        wav = session.wav_path(session_id)
        if wav.exists():
            size = wav.stat().st_size

    elapsed = ""
    if started is not None:
        elapsed = f"  elapsed: {_format_elapsed(started)}"

    size_str = f"  size: {_format_bytes(size)}" if size is not None else ""
    click.echo(f"Recording (pid {pid}, session {session_id}){elapsed}{size_str}")


def _transcribing_session() -> str | None:
    """Return the id of the session currently being transcribed, if any.

    A session stuck in STATUS_TRANSCRIBING while no recorder is running means
    a transcription is in flight (web job pool, or `rec transcribe` in another
    terminal). Used by `rec status` so the user sees that state instead of
    "Not recording". Newest match wins if there's ever more than one.
    """
    for meta in session.list_sessions():
        if meta.status == session.STATUS_TRANSCRIBING:
            return meta.id
    return None


def _format_elapsed(started: datetime) -> str:
    delta = datetime.now() - started
    total = int(delta.total_seconds())
    m, s = divmod(max(0, total), 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}h{m:02d}m"
    return f"{m}m{s:02d}s"


def _format_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}"
        n /= 1024.0
    return f"{n:.1f} TB"


# ---- transcribe ------------------------------------------------------------


@cli.command()
@click.argument("session_id")
@click.option("--model", default=None, help="Override whisper model.")
@click.option(
    "--vad/--no-vad",
    default=False,
    help="Enable voice-activity-detection pre-filter (default off; see transcriber docs).",
)
def transcribe(session_id: str, model: str | None, vad: bool) -> None:
    """Re-transcribe an existing recording."""
    cfg = config.load_config()
    log_mod.set_session_context(session_id)
    wav = session.wav_path(session_id)
    if not wav.exists():
        raise click.ClickException(f"No recording found for session {session_id} at {wav}.")
    _transcribe_session(session_id, cfg, model_override=model, vad_filter=vad)


# ---- diagnose --------------------------------------------------------------


@cli.command()
@click.argument("session_id")
@click.option(
    "--global-log-lines",
    type=int,
    default=500,
    show_default=True,
    help="Max lines to pull from the global log (filtered by session id).",
)
@click.option(
    "--stdout/--no-stdout",
    "to_stdout",
    default=False,
    help="Also print the bundle to stdout instead of only writing the file.",
)
def diagnose(session_id: str, global_log_lines: int, to_stdout: bool) -> None:
    """Bundle everything an AI agent needs to debug one session into one file.

    Produces sessions/<id>/diagnose.md containing:
      - session.json (metadata)
      - the session's recorder.log (daemon activity + raw output)
      - the global log lines tagged with this session id
      - the transcript if it exists
      - the current config

    The session id is the folder name under ~/.local/share/rec/sessions/
    (see `rec list`). A unique prefix is also accepted — e.g. `2026-07-27`
    matches `2026-07-27_14-30-00`.
    """
    try:
        resolved = _resolve_session_id(session_id)
    except session.AmbiguousSessionId as e:
        raise click.ClickException(
            f"{e} Run `rec list` to see session ids."
        ) from e
    if resolved is None:
        raise click.ClickException(
            f"No session matches '{session_id}'. Run `rec list` to see session ids."
        )
    if resolved != session_id:
        click.echo(f"(matched session {resolved})")
    bundle = _build_diagnose_bundle(resolved, global_log_lines)
    out_path = session.session_dir(resolved) / "diagnose.md"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(bundle, encoding="utf-8")
    click.echo(f"Diagnose bundle written: {out_path} ({len(bundle):,} bytes)")
    if to_stdout:
        click.echo(bundle)


# ---- web (local browser UI) ----------------------------------------------


@cli.command()
@click.option(
    "--port",
    type=int,
    default=7717,
    show_default=True,
    help="Port to serve the browser UI on (127.0.0.1 only).",
)
@click.option(
    "--no-open",
    is_flag=True,
    help="Do not open a browser tab automatically.",
)
def web(port: int, no_open: bool) -> None:
    """Open a local browser UI for browsing sessions and recordings.

    Serves a single-page app on 127.0.0.1 that lists sessions, plays audio,
    reads transcripts, searches, and starts/stops a recording — modelled on the
    qBittorrent/Transmission web UI. Bound to loopback only; the host is not
    configurable, by design (a local tool must not become an open transcript
    server). Viewing transcripts has nothing to do with audio capture, so this
    command skips the macOS/audiotap envcheck and runs anywhere transcripts
    exist. The Start button returns a clear error if config is missing.
    """
    from .web import server as web_server

    web_server.serve(port=port, open_browser=not no_open)


# ---- mcp (expose transcripts to Claude Code / other MCP clients) -----------


@cli.group(invoke_without_command=True)
@click.pass_context
def mcp(ctx: click.Context) -> None:
    """Expose your meeting transcripts to Claude Code and other MCP clients.

    \b
    Bare `rec mcp` runs the MCP server on stdio — point an MCP client at it.
    `rec mcp install` writes the server entry into Claude Code's config for you.

    The server is strictly READ-ONLY: it lists, reads, and searches transcripts.
    It never starts, stops, or deletes a recording, and it makes no network calls.
    """
    if ctx.invoked_subcommand is None:
        # Bare `rec mcp` → run the stdio server.
        _run_mcp_server()


def _run_mcp_server() -> None:
    """Launch the read-only MCP stdio server. Errors cleanly if `mcp` is missing."""
    try:
        from . import mcp_server
    except ImportError as e:
        raise click.ClickException(
            "The MCP server needs the `mcp` package, which isn't installed. "
            "Reinstall call-copilot (the dependency should come in automatically), "
            f"or run `pip install mcp`. (detail: {e})"
        ) from e
    mcp_server.run()


@mcp.command()
@click.option(
    "--scope",
    type=click.Choice(["local", "project", "user"], case_sensitive=False),
    default="user",
    show_default=True,
    help="Claude Code scope to install into (used by `claude mcp add`). "
         "user = available everywhere; project = this repo only; local = this dir only.",
)
def install(scope: str) -> None:
    """Register this `rec` as an MCP server in Claude Code (and print config for others).

    If the `claude` CLI is on PATH, this runs `claude mcp add call-copilot --scope <s> -- rec mcp`
    (the supported path — Claude manages its own config). Otherwise it hand-merges the
    entry into `~/.claude.json` under `mcpServers`. Either way it prints the exact JSON
    block added, plus a manual config block for Cursor / Zed / Cline and other clients.

    Refuses to clobber a differing existing entry (prints the diff and exits non-zero).
    Read-only: only writes the MCP client's config file.
    """
    _mcp_install(scope)


def _resolve_rec_command() -> list[str]:
    """Resolve the argv to launch `rec mcp` for an MCP client.

    Prefers a `rec` on PATH (matches how the user runs it); falls back to
    `python -m rec.mcp_server` so the command still works in editable/dev
    installs where `rec` may not be on PATH.
    """
    import sys

    rec_on_path = shutil.which("rec")
    if rec_on_path:
        return [rec_on_path, "mcp"]
    # Dev/editable fallback: launch the module directly with this interpreter.
    return [sys.executable, "-m", "rec.mcp_server"]


def _manual_config_block() -> str:
    """The JSON config block a user can paste into any MCP client config."""
    cmd = _resolve_rec_command()
    entry = {"type": "stdio", "command": cmd[0]}
    if len(cmd) > 1:
        entry["args"] = cmd[1:]
    return json.dumps({"call-copilot": entry}, indent=2)


def _mcp_install(scope: str) -> None:
    """Implementation of `rec mcp install` (split out for testability)."""
    log = log_mod.get_logger(__name__)
    cmd = _resolve_rec_command()
    log.info("mcp install: resolved rec command %s", cmd)

    # 1. Preferred path: the `claude` CLI manages its own config.
    claude_bin = shutil.which("claude")
    if claude_bin:
        # `claude mcp add <name> [--scope <s>] -- <command...>`
        argv = [claude_bin, "mcp", "add", "call-copilot", "--scope", scope, "--", *cmd]
        click.echo(f"Running: {' '.join(argv)}")
        try:
            result = subprocess.run(argv, capture_output=True, text=True, check=False)
        except OSError as e:
            raise click.ClickException(f"Failed to run `claude mcp add`: {e}") from e
        click.echo(result.stdout.rstrip())
        if result.returncode != 0:
            click.secho(result.stderr.rstrip(), fg="red", err=True)
            raise click.ClickException(
                f"`claude mcp add` exited {result.returncode}. "
                "You can fall back to a manual edit — see the config block below."
            )
        click.echo("")
        click.echo("Installed via the Claude Code CLI.")
    else:
        # 2. Fallback: hand-merge ~/.claude.json (top-level mcpServers).
        home = Path.home()
        claude_json = home / ".claude.json"
        click.echo(f"`claude` CLI not found on PATH — merging into {claude_json} directly.")
        try:
            if claude_json.exists():
                data = json.loads(claude_json.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    raise click.ClickException(
                        f"{claude_json} is not a JSON object — refusing to overwrite."
                    )
            else:
                data = {}
        except (json.JSONDecodeError, OSError) as e:
            raise click.ClickException(f"Could not read {claude_json}: {e}") from e

        servers = data.setdefault("mcpServers", {})
        desired = {"type": "stdio", "command": cmd[0]}
        if len(cmd) > 1:
            desired["args"] = cmd[1:]

        existing = servers.get("call-copilot")
        if existing is not None and existing != desired:
            # Refuse to clobber a differing entry.
            click.secho(
                "An existing `call-copilot` MCP entry differs from what `rec mcp install` "
                "would write. Refusing to clobber it.",
                fg="yellow", err=True,
            )
            click.echo("  existing: " + json.dumps(existing, indent=2).replace("\n", "\n  "))
            click.echo("  desired:  " + json.dumps(desired, indent=2).replace("\n", "\n  "))
            click.echo(
                "Remove the existing entry first (or run `claude mcp remove call-copilot`), "
                "then re-run `rec mcp install`."
            )
            raise click.ClickException("Refusing to clobber a differing existing MCP entry.")

        servers["call-copilot"] = desired
        # Atomic + private write: a uniquely-named temp file in the same dir
        # (so the rename is atomic), created with mode 0600 so the merged
        # config — which may carry other servers' command paths / tokens — is
        # never briefly world-readable, then os.replace onto the target.
        payload = json.dumps(data, indent=2) + "\n"
        fd, tmp_name = tempfile.mkstemp(
            prefix=".claude.json.", suffix=".tmp", dir=str(claude_json.parent)
        )
        try:
            os.chmod(tmp_name, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
            os.replace(tmp_name, claude_json)
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
        click.echo(f"Wrote `call-copilot` MCP server entry into {claude_json}.")

    # Always print the JSON block + manual config for other clients.
    click.echo("")
    click.echo("Entry added:")
    click.echo(_manual_config_block())
    click.echo("")
    click.echo(
        "For Cursor / Zed / Cline / other MCP clients, paste the block above into "
        "that client's MCP config (e.g. Cursor: .cursor/mcp.json; Zed: settings.json "
        "under \"mcp_servers\"). The server is read-only — it can't touch recordings."
    )


def mcp_status(*, home: Path | None = None) -> dict:
    """Whether the MCP server is wired into Claude Code.

    Reads ``~/.claude.json`` (the file the fallback install path writes, and
    where ``claude mcp add --scope user`` also lands) and checks the
    ``mcpServers["call-copilot"]`` entry against the command ``rec mcp`` would
    actually launch. Returns ``{wired: bool, path: str, note: str|None}``.

    ``home`` is injectable for tests (defaults to the real ``Path.home()``).

    Note: when the install went through the ``claude`` CLI with a non-user
    scope, Claude manages its own config and this read may not see it — the
    ``note`` field says so rather than reporting a false negative.
    """
    home = home if home is not None else Path.home()
    claude_json = home / ".claude.json"
    expected_cmd = _resolve_rec_command()
    expected = {"type": "stdio", "command": expected_cmd[0]}
    if len(expected_cmd) > 1:
        expected["args"] = expected_cmd[1:]

    if not claude_json.exists():
        return {"wired": False, "path": str(claude_json),
                "note": "Not installed. Run `rec mcp install`."}
    try:
        data = json.loads(claude_json.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"wired": False, "path": str(claude_json),
                "note": f"{claude_json} is unreadable or not valid JSON."}
    servers = data.get("mcpServers", {}) if isinstance(data, dict) else {}
    entry = servers.get("call-copilot")
    if entry is None:
        return {"wired": False, "path": str(claude_json),
                "note": "Not found in mcpServers. Run `rec mcp install`."}
    # Allow entry with/without args; compare command + args.
    if entry.get("command") == expected["command"] and entry.get("args") == expected.get("args"):
        return {"wired": True, "path": str(claude_json),
                "note": "Installed via ~/.claude.json (or `claude mcp add --scope user`)."}
    return {"wired": True, "path": str(claude_json),
            "note": "Wired, but the command differs from what `rec mcp` launches now "
                    "(may be stale from a different install). Run `rec mcp install` to refresh."}


@mcp.command(name="status")
def mcp_status_cmd() -> None:
    """Show whether the MCP server is wired into Claude Code."""
    st = mcp_status()
    click.echo(f"Config: {st['path']}")
    if st["wired"]:
        click.secho("Status: wired", fg="green")
    else:
        click.secho("Status: not wired", fg="yellow")
    if st["note"]:
        click.echo(st["note"])


# ---- index (forced reindex of the FTS5 search cache) -----------------------


@cli.command()
@click.option(
    "--rebuild",
    is_flag=True,
    help="Drop the search index and rebuild it from every transcript on disk.",
)
@click.option(
    "--status",
    "show_status",
    is_flag=True,
    help="Show index health (lines, sessions, last indexed, orphans) and exit.",
)
def index(rebuild: bool, show_status: bool) -> None:
    """Build or refresh the transcript search index (FTS5).

    Search (`rec mcp`'s search_transcripts tool) indexes lazily on first use; this
    command lets you force a refresh — useful after bulk edits, or with `--rebuild`
    to recreate a stale/corrupt index. The index is a disposable cache at
    ~/.local/share/rec/index.db and can be safely deleted at any time.
    """
    from . import index as index_mod

    if show_status:
        st = index_mod.stats()
        click.echo(f"Sessions indexed: {st.sessions}")
        click.echo(f"Lines indexed:    {st.lines}")
        if st.last_indexed_at is not None:
            when = datetime.fromtimestamp(st.last_indexed_at).strftime("%Y-%m-%d %H:%M")
            click.echo(f"Last indexed:     {when}")
        else:
            click.echo("Last indexed:     —")
        orphans = st.orphans
        if orphans:
            click.secho(f"Orphans:          {orphans} indexed session(s) no longer on disk",
                        fg="yellow")
        else:
            click.echo("Orphans:          0")
        click.echo(f"Index:            {index_mod.index_path()}")
        return

    n = index_mod.ensure_indexed(rebuild=rebuild)
    if rebuild:
        click.echo(f"Rebuilt index from scratch: {n} session(s) indexed.")
    elif n:
        click.echo(f"Indexed {n} new/updated session(s).")
    else:
        click.echo("Index is up to date (0 sessions needed reindexing).")
    db = index_mod.index_path()
    click.echo(f"Index: {db}")


# ---- summarize (Step 3) ---------------------------------------------------


@cli.command()
@click.argument("session_id")
@click.option("--template", "template_name", default="default", show_default=True,
              help="Built-in or user prompt template name (default/standup/client-call/...).")
@click.option("--template-file", "template_file", default=None,
              help="Path to a custom .md template file (overrides --template).")
@click.option("--provider", "provider_name", default=None,
              help="Provider preset (glm/glm-anthropic/anthropic/gemini/deepseek/ollama/openai-compat).")
@click.option("--tier1", "tier1_model", default=None, help="Tier 1 (map) model override.")
@click.option("--tier2", "tier2_model", default=None, help="Tier 2 (consolidate) model override.")
@click.option("--tier3", "tier3_model", default=None, help="Tier 3 (reduce) model override.")
@click.option("--dry-run", is_flag=True,
              help="Print chunk count, estimated tokens, and estimated cost — zero network calls.")
@click.option("--force", is_flag=True, help="Overwrite an existing summary.md.")
@click.option("--yes", is_flag=True, help="Skip the one-time network consent prompt.")
@click.option("--api-key-env", "api_key_env", default=None,
              help="Name of the env var holding the API key (overrides summarize.api_key_env).")
def summarize(
    session_id: str, template_name: str, template_file: str | None,
    provider_name: str | None, tier1_model: str | None, tier2_model: str | None,
    tier3_model: str | None, dry_run: bool, force: bool, yes: bool, api_key_env: str | None,
) -> None:
    """Summarise a session's transcript → summary.md using your own API key.

    BYOK and offline by default: with no provider configured this errors with a
    one-line setup instruction and never makes a network call. The first network
    run asks once before sending transcript text; --yes skips that. The summary
    goes to summary.md next to the transcript; transcript.md is never modified.

    --dry-run prints the chunk count, estimated tokens per tier, and estimated
    cost with ZERO network calls — use it to see what a run will cost first.
    """
    _summarize_command(
        session_id, template_name=template_name, template_file=template_file,
        provider_name=provider_name, tier1_model=tier1_model, tier2_model=tier2_model,
        tier3_model=tier3_model, dry_run=dry_run, force=force, yes=yes,
        api_key_env=api_key_env,
    )


def _summarize_command(
    session_id: str, *, template_name: str, template_file: str | None,
    provider_name: str | None, tier1_model: str | None, tier2_model: str | None,
    tier3_model: str | None, dry_run: bool, force: bool, yes: bool,
    api_key_env: str | None,
) -> None:
    """Implementation of `rec summarize` (split out for testability)."""
    from . import summarize as summarize_mod
    from . import templates as templates_mod
    from .providers import NoProviderError, consent_host, is_local_provider, make_provider

    log_mod.set_session_context(session_id)

    # Resolve the session (partial ids allowed).
    try:
        resolved = session.resolve_session_id(session_id)
    except session.AmbiguousSessionId as e:
        raise click.ClickException(f"{e} Run `rec list` to see session ids.") from e
    if resolved is None:
        raise click.ClickException(
            f"No session matches {session_id!r}. Run `rec list` to see session ids."
        )
    if resolved != session_id:
        click.echo(f"(matched session {resolved})")
    sid = resolved

    tpath = session.transcript_path(sid)
    if not tpath.exists():
        raise click.ClickException(
            f"Session {sid} has no transcript (transcribe it first with `rec transcribe {sid}`)."
        )

    # Guard: don't silently overwrite an existing summary.
    if session.summary_path(sid).exists() and not force:
        raise click.ClickException(
            f"Summary already exists for {sid}: {session.summary_path(sid)}. "
            "Pass --force to overwrite."
        )

    # Load template (file override wins).
    try:
        template = (
            templates_mod.load_template_file(template_file)
            if template_file
            else templates_mod.load_template(template_name)
        )
    except templates_mod.TemplateError as e:
        raise click.ClickException(str(e)) from e

    cfg = config.load_config()
    summ = dict(cfg.summarize or {})
    pname = provider_name or summ.get("provider")
    base_url = summ.get("base_url")
    key_env = api_key_env or summ.get("api_key_env")

    # --- dry run: chunking + estimates only, zero network ---
    if dry_run:
        return _run_dry_run(sid, template, pname, tier1_model, tier2_model, tier3_model,
                            summ=summ)

    # No provider configured → one-line error (offline by default).
    if not pname:
        raise click.ClickException(
            "No summarisation provider configured. Set `summarize.provider` (e.g. \"glm\") "
            "and the relevant API-key env var in your config, then re-run. "
            "Run `rec setup` first if you have no config. "
            "(Example: export ZAI_API_KEY=... and add a \"summarize\": {\"provider\": \"glm\"} block.)"
        )

    # Resolve models per tier (config → defaults per provider).
    t1 = tier1_model or summ.get("tier1_model") or "glm-4.7-flash"
    t2 = tier2_model if tier2_model is not None else summ.get("tier2_model")
    t3 = tier3_model or summ.get("tier3_model") or "glm-5"

    # One-time network consent (local providers never prompt).
    local = is_local_provider(pname, base_url)
    if not local and not summ.get("confirmed_network") and not yes:
        host = consent_host(name=pname, base_url=base_url)
        if not _confirm_network(host):
            click.echo("Summarise cancelled.")
            return
        summ["confirmed_network"] = True
        _persist_summarize_block(cfg, summ)

    # Construct the provider (resolves the key from the env var).
    try:
        provider = make_provider(name=pname, api_key_env=key_env, base_url=base_url)
    except NoProviderError as e:
        raise click.ClickException(str(e)) from e

    # The transcript is never modified by summarisation — summary text goes to
    # summary.md only. The byte-identical invariant is pinned by test #13.
    try:
        result = summarize_mod.summarize(
            session_id=sid, provider=provider, template=template,
            tier1_model=t1, tier2_model=t2, tier3_model=t3,
        )
    except summarize_mod.ProviderError as e:
        # Auth/transport error the orchestrator chose to surface (401/403).
        # Render as one human line, non-zero exit — never a traceback.
        raise click.ClickException(e.message) from e
    except KeyboardInterrupt:
        # Abort summarisation only: transcript intact, exit 0.
        _print_cost_line_from_calls(sid, [], elapsed=0.0)
        click.echo("Summarise interrupted. Transcript is intact.")
        return

    # Record summary metadata on the session (no key, no text).
    session.update_meta(sid, summary=result.to_meta(template.name, pname))

    click.echo(f"Summary: {result.out_path}")
    _print_cost_line(result)
    if result.partial:
        click.secho(
            "Note: the reduce pass did not complete; partial map output written to "
            f"{result.out_path.name}.",
            fg="yellow", err=True,
        )


def _run_dry_run(sid, template, pname, t1_override, t2_override, t3_override, *, summ):
    """Print chunk count, estimated tokens, and estimated cost. Zero network."""
    from . import chunking as chunking_mod
    from .providers import pricing

    transcript = session.transcript_path(sid).read_text(encoding="utf-8")
    chunks = chunking_mod.chunk_transcript(transcript)
    if not chunks:
        raise click.ClickException(f"No transcript lines to summarise in {sid}.")

    total_tokens = chunking_mod.total_estimate(chunks)
    t1 = t1_override or summ.get("tier1_model") or "glm-4.7-flash"
    t3 = t3_override or summ.get("tier3_model") or "glm-5"

    # Rough cost: estimate Tier 1 as `total_tokens` in, ~2k out across chunks;
    # Tier 3 as a few k in, ~4k out. This is a planning number, not a promise.
    t1_in = total_tokens
    t1_out = len(chunks) * 1500
    t3_in = min(total_tokens // 3, 12_000)
    t3_out = 3000
    t1_cost = pricing.cost(t1, t1_in, t1_out)
    t3_cost = pricing.cost(t3, t3_in, t3_out)
    est = (t1_cost or 0.0) + (t3_cost or 0.0)

    click.echo(f"Session: {sid}")
    click.echo(f"Chunks: {len(chunks)} (target ~6k tokens each, ceiling 8k)")
    click.echo(f"Estimated tokens: ~{total_tokens:,} transcript → ~{t1_in + t1_out:,} tier-1, ~{t3_in + t3_out:,} tier-3")
    label = f"~${est:.2g}" if est else "$0.00" if est == 0.0 else "cost unknown"
    click.echo(f"Estimated cost: {label}  ({t1} → {t3})")
    if pname:
        click.echo(f"Provider: {pname}")
    else:
        click.echo("Provider: (none configured — set summarize.provider to run for real)")
    click.echo("(dry run — zero network calls)")


def _confirm_network(host: str) -> bool:
    """The one-time network-consent prompt. Local providers never reach here."""
    # `click.confirm` with default=False so a blind Enter does NOT consent.
    return click.confirm(
        f"This sends transcript text to {host}. Continue?",
        default=False,
    )


def _persist_summarize_block(cfg: config.RecConfig, summ: dict) -> None:
    """Persist the (possibly updated) summarize block back to config.json."""
    cfg.summarize = summ
    config.save_config(cfg)


def _print_cost_line(result) -> None:
    """Render the real cost line: `summary: $X — N tier-1 (model), ...`."""
    models = result.models
    calls = result.calls
    toks = result.tokens

    def _cost_str() -> str:
        if result.cost_usd is None:
            return "cost unknown (model not in price table)"
        if result.cost_estimated:
            return f"~${result.cost_usd:.2g} (estimated — provider reported no usage)"
        return f"${result.cost_usd:.4f}"

    t1m = models.get("tier1") or "?"
    t2m = models.get("tier2")
    t3m = models.get("tier3") or "?"
    tier2_part = f", {calls.get('tier2', 0)} tier-2" + (f" ({t2m})" if t2m else "")
    click.echo(
        f"summary: {_cost_str()} — {calls.get('tier1', 0)} tier-1 calls ({t1m})"
        f"{tier2_part}, {calls.get('tier3', 0)} tier-3 call ({t3m}), "
        f"{_fmt_tok(toks.get('in', 0))} in / {_fmt_tok(toks.get('out', 0))} out, "
        f"{result.wall_clock_s:.0f}s"
    )


def _print_cost_line_from_calls(sid, calls, *, elapsed: float) -> None:
    """Print a cost line for a run that didn't complete the normal path."""
    if not calls:
        return
    # Best-effort: aggregate what we have.
    click.echo(f"summary (partial): see summary.partial.md — {elapsed:.0f}s")


def _fmt_tok(n: int) -> str:
    """Render a token count as e.g. `38.2k`."""
    if n >= 1000:
        return f"{n / 1000:.1f}k"
    return str(n)


# ---- shared transcription helper ------------------------------------------


def _transcribe_session(
    session_id: str,
    cfg: config.RecConfig,
    *,
    model_override: str | None,
    vad_filter: bool = False,
) -> None:
    """Transcribe a session's WAV(s) -> transcript.md, updating session.json.

    Handles one or two WAVs. If both system (recording.wav) and mic
    (recording-mic.wav) exist, transcribes each and merges the transcripts
    (labeled [System]/[Mic], interleaved by timestamp). Otherwise transcribes
    the single present WAV.
    """
    model_name = model_override or cfg.whisper_model
    sys_wav = session.wav_path(session_id)
    mic_wav = session.mic_wav_path(session_id)

    has_sys = sys_wav.exists()
    has_mic = mic_wav.exists()
    if not has_sys and not has_mic:
        # _finish_session already handled the no-WAV case, but guard anyway.
        raise click.ClickException(f"No recording found for session {session_id}.")

    # Mark the session as transcribing BEFORE the (slow) model load + decode so
    # `rec status` and the web UI show in-flight state. Cleared to a terminal
    # status below; on error we drop back to RECORDED so the session isn't
    # stuck "transcribing" forever.
    session.update_meta(session_id, status=session.STATUS_TRANSCRIBING)
    try:
        return _transcribe_session_inner(
            session_id, model_name, sys_wav, mic_wav, has_sys, has_mic, vad_filter
        )
    except BaseException:
        # Includes KeyboardInterrupt and SystemExit, which ``except Exception``
        # misses — a Ctrl+C during transcription used to skip this rollback,
        # leaving session.json stuck at STATUS_TRANSCRIBING (which then poisons
        # ``rec status`` via _transcribing_session). Roll back, then re-raise so
        # exit semantics are unchanged. The inner guard ensures a failed rollback
        # never masks the original abort.
        try:
            session.update_meta(session_id, status=session.STATUS_RECORDED)
        except BaseException:
            pass
        raise


def _transcribe_session_inner(
    session_id: str,
    model_name: str,
    sys_wav: Path,
    mic_wav: Path,
    has_sys: bool,
    has_mic: bool,
    vad_filter: bool,
) -> None:
    """Run the Whisper transcodes and write the transcript + terminal status.

    Split out of `_transcribe_session` so the caller can bracket it with the
    STATUS_TRANSCRIBING / error-rollback pair without a deep try/except around
    the whole body. Assumes STATUS_TRANSCRIBING is already set on the session.
    """
    def _t(path):
        return transcriber.transcribe(
            path, model_name=model_name, language="en", console=console, vad_filter=vad_filter
        )

    if has_sys and has_mic:
        sys_result = _t(sys_wav)
        mic_result = _t(mic_wav)
        md = formatter.build_merged_markdown(
            system_result=sys_result,
            mic_result=mic_result,
            wav_filenames=(sys_wav.name, mic_wav.name),
        )
        duration = max(sys_result.duration, mic_result.duration)
        word_count = transcriber.count_words(sys_result.segments) + transcriber.count_words(mic_result.segments)
    elif has_mic:
        # mic-only recording lives in recording-mic.wav
        result = _t(mic_wav)
        md = formatter.build_markdown(result.segments, duration_seconds=result.duration,
                                      wav_filename=mic_wav.name, source="Mic")
        duration = result.duration
        word_count = transcriber.count_words(result.segments)
    else:
        result = _t(sys_wav)
        md = formatter.build_markdown(result.segments, duration_seconds=result.duration,
                                      wav_filename=sys_wav.name, source="System")
        duration = result.duration
        word_count = transcriber.count_words(result.segments)

    out = formatter.write_transcript(session_id, md)
    session.update_meta(
        session_id,
        status=session.STATUS_TRANSCRIBED,
        duration=duration,
        word_count=word_count,
        model=model_name,
    )
    click.echo(
        f"Transcript: {out}\n"
        f"Duration: {session.format_duration_human(duration)}  "
        f"Words: {word_count:,}  "
        f"Language: en"
    )


def _resolve_capture(cfg: config.RecConfig, system_only: bool, mic_only: bool) -> str:
    """Pick the capture mode from flags, falling back to config."""
    if system_only:
        return "system"
    if mic_only:
        return "mic"
    return cfg.capture


def _parse_capture_for_cli(capture: str) -> set[str]:
    """CLI-side view of a capture mode -> set of sources.

    Thin wrapper over recorder._parse_capture so the start command can tell
    whether the mic is in play (to warn if its permission is missing) without
    duplicating the mode-string parsing.
    """
    return recorder._parse_capture(capture)


def _mic_permission_granted() -> bool:
    """True if the mic permission is already granted (not promptable).

    Deliberately more conservative than recorder._mic_available: the start-time
    warning should fire whenever the user would NOT get an auto-prompt, i.e. the
    permission is anything other than already-GRANTED. (UNKNOWN may prompt at
    tap time, so we don't pre-warn for it.)
    """
    try:
        import audiotap

        return audiotap.mic_permission_status() == audiotap.Permission.GRANTED
    except Exception:  # pragma: no cover — audiotap missing/broken
        return False


# ---- helpers ---------------------------------------------------------------


def _current_recording_session_id() -> str | None:
    """The id of the most recent 'recording' session, else the newest overall."""
    sessions = session.list_sessions()
    if not sessions:
        return None
    for m in sessions:
        if m.status == session.STATUS_RECORDING:
            return m.id
    return sessions[0].id


# Re-export of session.resolve_session_id (the implementation lives in
# session.py so the MCP server can use it without importing the CLI). Kept under
# its private CLI name for back-compat with the existing call sites below.
_resolve_session_id = session.resolve_session_id


def _read_if_exists(path, label: str, *, fence: str = "```") -> str:
    """Return a labeled fenced block of a file's contents, or a 'not found' note."""
    from pathlib import Path

    p = Path(path)
    if not p.exists():
        return f"### {label}\n\n_(not present: {p})_\n"
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return f"### {label}\n\n_(could not read {p}: {e})_\n"
    return f"### {label}\n\n{fence}\n{text}\n{fence}\n"


def _filter_global_log_by_session(session_id: str, max_lines: int) -> str:
    """Lines from the global log whose [session_id|...] stamp matches (or is '-')."""
    gpath = log_mod.global_log_path()
    if not gpath.exists():
        return f"_(global log not present: {gpath})_"
    try:
        lines = gpath.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as e:
        return f"_(could not read {gpath}: {e})_"
    # Keep lines stamped with this session, plus any un-attributed ERROR lines.
    needle = f"[{session_id}|"
    matched = [ln for ln in lines if needle in ln or "ERROR" in ln]
    if len(matched) > max_lines:
        head = matched[: max_lines // 2]
        tail = matched[-(max_lines // 2):]
        matched = head + [f"... ({len(matched) - max_lines} lines omitted) ..."] + tail
    return "\n".join(matched) if matched else "_(no log lines matched this session id)_"


def _build_diagnose_bundle(session_id: str, global_log_lines: int) -> str:
    """Assemble the markdown debug bundle for one session."""
    from datetime import datetime

    parts: list[str] = []
    parts.append("# Diagnose bundle")
    parts.append("")
    parts.append(f"**Session:** `{session_id}`")
    parts.append(f"**Generated:** {datetime.now().isoformat(timespec='seconds')}")
    parts.append(f"**rec version:** {__version__}")
    parts.append(f"**Platform:** {platform.platform()}")
    parts.append("")
    parts.append("---")
    parts.append("")

    # 1. Session metadata.
    meta = session.load_meta(session_id)
    if meta is not None:
        parts.append(_read_if_exists(session.session_json_path(session_id), "session.json"))
    else:
        parts.append(
            f"### session.json\n\n"
            f"_(no session directory or metadata found for `{session_id}` — "
            f"check `rec list` for valid session ids.)_\n"
        )

    # 2. Session recorder.log (daemon detail + raw output).
    parts.append(_read_if_exists(
        session.session_dir(session_id) / "recorder.log", "session recorder.log"
    ))

    # 3. Global log lines for this session.
    parts.append("### global log (filtered by session id)")
    parts.append("")
    parts.append("```")
    parts.append(_filter_global_log_by_session(session_id, global_log_lines))
    parts.append("```")
    parts.append("")

    # 4. Transcript (if transcribed).
    tpath = session.transcript_path(session_id)
    if tpath.exists():
        parts.append(_read_if_exists(tpath, "transcript.md"))
    else:
        parts.append("### transcript.md\n\n_(no transcript — session not transcribed)_\n")

    # 5. Config.
    parts.append("### config.json")
    parts.append("")
    cpath = config.config_path()
    if cpath.exists():
        try:
            parts.append("```json")
            parts.append(cpath.read_text(encoding="utf-8").rstrip())
            parts.append("```")
        except OSError as e:
            parts.append(f"_(could not read config: {e})_")
    else:
        parts.append("_(no config — `rec setup` not run)_")
    parts.append("")

    parts.append("---")
    parts.append("")
    parts.append(
        "## Summary for the AI agent\n\n"
        "Investigate why this session did not behave as expected. The metadata "
        "above shows the lifecycle status; the logs show the audio tap lifecycle, "
        "every chunk write, and any ERROR lines. If the transcript is empty but "
        "the recording is non-trivially long, check the `audio_peak`/`audio_rms` "
        "values in session.json — a near-zero peak means the tap captured silence "
        "(nothing was playing, or the capture permission was revoked). If "
        "`recorder.log` ends abruptly, the daemon crashed or was killed (look for "
        "an unhandled exception traceback)."
    )
    parts.append("")

    return "\n".join(parts)


if __name__ == "__main__":
    import sys

    sys.exit(main())
