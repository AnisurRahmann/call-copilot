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

import platform
import time
from datetime import datetime

import click
import rich.box
from rich.console import Console
from rich.table import Table

from . import __version__, audio_check, config, envcheck, formatter, recorder, session, transcriber
from . import log as log_mod

console = Console()


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
def setup(default_model: str) -> None:
    """One-time setup: verify macOS + audiotap + capture permission."""
    log = log_mod.get_logger(__name__)
    log.info("running setup (default_model=%s)", default_model)

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
    click.echo("")
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
def start(model: str | None, vad: bool, detach: bool, system_only: bool, mic_only: bool) -> None:
    """Start recording. Shows a live indicator; press Ctrl+C to stop & transcribe."""
    if system_only and mic_only:
        raise click.ClickException("--system-only and --mic-only are mutually exclusive.")
    # Guard: already recording?
    if recorder.active_pid() is not None:
        raise click.ClickException("Already recording. Run 'rec stop' first.")

    cfg = config.load_config()
    capture = _resolve_capture(cfg, system_only, mic_only)

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
    _run_live_recording(cfg, session_id, model_override=model, vad_filter=vad)


def _run_live_recording(
    cfg: config.RecConfig,
    session_id: str,
    *,
    model_override: str | None,
    vad_filter: bool,
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
        with Live(render(), console=console, refresh_per_second=2, transient=True) as live:
            while recorder.active_pid() is not None:
                live.update(render())
                time.sleep(0.5)
    except KeyboardInterrupt:
        # Expected stop path: Ctrl+C. Fall through to finish the session.
        console.print()  # newline after the cleared live display
    else:
        # The daemon died on its own (crash / system sleep). Salvage what we have.
        console.print("\n[yellow](recorder exited unexpectedly — salvaging partial audio.)[/]")

    # Stop the daemon (no-op if already dead) and run silence-check + transcribe.
    recorder.stop_recorder()
    _finish_session(cfg, session_id, model_override=model_override, vad_filter=vad_filter)


# ---- stop ------------------------------------------------------------------


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
def stop(model: str | None, vad: bool) -> None:
    """Stop recording, transcribe, and save the markdown transcript."""
    cfg = config.load_config()

    # Determine the session we're stopping (most recent recording).
    pid = recorder.active_pid()
    session_id = _current_recording_session_id()
    if session_id is None:
        raise click.ClickException("No active recording to stop.")

    was_alive, _ = recorder.stop_recorder()
    if not was_alive and pid is not None:
        click.echo("(recorder process had already exited — salvaging partial audio.)")

    _finish_session(cfg, session_id, model_override=model, vad_filter=vad)


def _finish_session(
    cfg: config.RecConfig,
    session_id: str,
    *,
    model_override: str | None,
    vad_filter: bool,
) -> None:
    """Run the silence check + transcription for a just-stopped session.

    Shared by `rec stop` (finds the active session itself) and `rec start`
    (knows its session id after Ctrl+C). Assumes the recorder daemon has
    already been signaled to stop.
    """
    log_mod.set_session_context(session_id)

    wav = session.wav_path(session_id)
    if not wav.exists():
        # The daemon was stopped before it opened the WAV (very fast Ctrl+C, or
        # it crashed at startup). Nothing to transcribe — say so clearly rather
        # than crashing, and mark the session so `rec list` reflects reality.
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
    levels = audio_check.analyze_wav(wav)
    if levels is not None:
        meta = session.load_meta(session_id)
        if meta is not None:
            meta.extra["audio_peak"] = round(levels.peak, 6)
            meta.extra["audio_rms"] = round(levels.rms, 6)
            meta.extra["audio_silent"] = levels.silent
            session.save_meta(meta)
        if levels.silent:
            click.secho(
                f"⚠  WARNING: recording is silent (peak={levels.peak:.6f}, rms={levels.rms:.6f}).\n"
                f"   The audio tap captured no signal — either nothing was playing,\n"
                f"   or macOS revoked the capture permission. Run `rec setup`, then\n"
                f"   record again while audio is actually playing.",
                fg="yellow", err=True,
            )

    _transcribe_session(session_id, cfg, model_override=model_override, vad_filter=vad_filter)


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
    table.add_column("Model", justify="center", style="dim")

    for m in sessions:
        words = "—" if m.word_count is None else f"{m.word_count:,}"
        table.add_row(
            session.started_at_display(m.id),
            session.format_duration_human(m.duration),
            words,
            m.status,
            m.model or "",
        )

    console.print(table)


# ---- status ----------------------------------------------------------------


@cli.command()
def status() -> None:
    """Show whether a recording is in progress."""
    pid = recorder.active_pid()
    if pid is None:
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
    resolved = _resolve_session_id(session_id)
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


def _resolve_session_id(query: str) -> str | None:
    """Resolve a (possibly partial) session id to a real one.

    Accepts the full id ('2026-07-27_14-30-00') or any unique prefix
    ('2026-07-27'). Returns None if nothing matches.
    """
    # Exact match first.
    if session.session_dir(query).exists():
        return query
    # Prefix match against all session dirs.
    matches = [s.id for s in session.list_sessions() if s.id.startswith(query)]
    return matches[0] if matches else None


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
