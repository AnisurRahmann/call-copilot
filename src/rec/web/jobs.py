"""Background transcription jobs for the web UI.

Transcribing an hour of audio takes minutes; doing it inside the request
handler would hang the browser and time out. Instead, ``POST /api/recording/
stop`` and the re-transcribe endpoint queue a job here and return ``202`` with
a job id the UI polls.

The pool is a single worker (``ThreadPoolExecutor(max_workers=1)``) so two
transcriptions can never fight over CPU or the Whisper model. The registry is
keyed by ``session_id``: a second request for a session that already has a
queued or running job is rejected with 409 (``DuplicateJob``), which is how a
double-clicked Stop avoids producing two transcripts of the same WAV.

The worker calls the existing transcription orchestration in :mod:`rec.cli`
(``_finish_session`` after a stop, ``_transcribe_session`` for a re-transcribe).
That coupling is deliberate and noted: extracting those into a neutral pipeline
module is a worthwhile cleanup but would touch the CLI's orchestration and its
tests, which is out of scope for the web feature. The import is lazy so this
module stays light and the CLI is only pulled in when a job actually runs.
"""

from __future__ import annotations

import threading
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from enum import StrEnum

from .. import config
from ..log import get_logger

log = get_logger(__name__)


class JobState(StrEnum):
    """Lifecycle of a transcription job."""

    queued = "queued"
    running = "running"
    done = "done"
    error = "error"


# What the worker should run once it reaches the head of the queue.
# "finish"  — analyze audio + transcribe (the post-stop path).
# "transcribe" — re-transcribe an existing session at a (maybe different) model.
JOB_FINISH = "finish"
JOB_TRANSCRIBE = "transcribe"


class DuplicateJob(Exception):
    """A job is already queued or running for this session.

    Raised by :meth:`JobRegistry.queue`; the API layer maps it to HTTP 409.
    """


@dataclass
class Job:
    """One transcription job, observable by the UI via GET /api/jobs/{id}."""

    id: str
    session_id: str
    kind: str  # JOB_FINISH | JOB_TRANSCRIBE
    state: JobState = JobState.queued
    message: str = ""
    model_override: str | None = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "session_id": self.session_id,
            "state": self.state.value,
            "kind": self.kind,
            "message": self.message,
            "model": self.model_override,
        }


@dataclass
class JobRegistry:
    """A single-worker transcription queue, keyed by session id.

    One instance per web process (see :data:`registry`). Thread-safe via a
    lock around the job map; the executor serialises the work itself.
    """

    max_workers: int = 1
    _jobs: dict[str, Job] = field(default_factory=dict)
    _futures: dict[str, Future] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _executor: ThreadPoolExecutor | None = None

    def __post_init__(self) -> None:
        # Created lazily so the threads only start when the first job queues;
        # tests that construct a registry never spin up a worker they won't use.
        pass

    # ---- public API ------------------------------------------------------

    def queue(
        self,
        session_id: str,
        *,
        kind: str = JOB_FINISH,
        model_override: str | None = None,
    ) -> Job:
        """Queue a transcription job; raise DuplicateJob if one is active.

        Returns the job (state=queued). The caller returns its id as 202; the
        UI polls :meth:`get` until state is done/error.
        """
        if kind not in (JOB_FINISH, JOB_TRANSCRIBE):
            raise ValueError(f"unknown job kind: {kind!r}")
        with self._lock:
            active = self._active_for_session_locked(session_id)
            if active is not None:
                raise DuplicateJob(session_id)
            job = Job(
                id=uuid.uuid4().hex[:12],
                session_id=session_id,
                kind=kind,
                model_override=model_override,
            )
            self._jobs[job.id] = job
            executor = self._ensure_executor()
            future = executor.submit(self._run, job)
            self._futures[job.id] = future
        log.info("queued %s job %s for session %s", kind, job.id, session_id)
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def active_for_session(self, session_id: str) -> Job | None:
        with self._lock:
            return self._active_for_session_locked(session_id)

    def shutdown(self) -> None:
        """Stop accepting work. Called when the web server shuts down."""
        with self._lock:
            executor = self._executor
            self._executor = None
        if executor is not None:
            executor.shutdown(wait=False, cancel_futures=True)

    # ---- internals ------------------------------------------------------

    def _ensure_executor(self) -> ThreadPoolExecutor:
        if self._executor is None:
            self._executor = ThreadPoolExecutor(
                max_workers=self.max_workers, thread_name_prefix="rec-transcribe"
            )
        return self._executor

    def _active_for_session_locked(self, session_id: str) -> Job | None:
        for job in self._jobs.values():
            if job.session_id == session_id and job.state in (JobState.queued, JobState.running):
                return job
        return None

    def _set_state(self, job: Job, state: JobState, message: str = "") -> None:
        with self._lock:
            job.state = state
            if message:
                job.message = message

    # ---- the worker -----------------------------------------------------

    def _run(self, job: Job) -> None:
        """Run one job to completion. Never raises into the pool."""
        self._set_state(job, JobState.running)
        try:
            cfg = config.load_config()
        except Exception as e:
            # The Start button guards on config, but a re-transcribe of an old
            # session after the user deleted their config should still fail
            # cleanly rather than crash the worker.
            self._set_state(
                job, JobState.error,
                f"No config found. Run `rec setup` in a terminal first. ({e})",
            )
            return

        try:
            if job.kind == JOB_FINISH:
                _run_finish(job.session_id, cfg, job.model_override)
            else:
                _run_transcribe(job.session_id, cfg, job.model_override)
        except Exception as e:
            log.warning("transcribe job %s failed (%r)", job.id, e)
            self._set_state(job, JobState.error, _human_error(e))
            return
        self._set_state(job, JobState.done, "Transcript ready.")


# ---- worker entry points (module-level so they can be monkeypatched) -------


def _run_finish(session_id: str, cfg: config.RecConfig, model_override: str | None) -> None:
    """Post-stop pipeline: silence check + transcription.

    Delegates to cli._finish_session (status=None → no spinner). The CLI
    orchestration already manages STATUS_TRANSCRIBING → terminal.
    """
    from .. import cli

    cli._finish_session(
        cfg, session_id,
        model_override=model_override, vad_filter=False, status=None,
    )


def _run_transcribe(session_id: str, cfg: config.RecConfig, model_override: str | None) -> None:
    """Re-transcribe an existing session at a (maybe different) model."""
    from .. import cli

    cli._transcribe_session(
        session_id, cfg, model_override=model_override, vad_filter=False,
    )


def _human_error(e: Exception) -> str:
    """Turn an exception into one readable sentence for the UI."""
    import click

    if isinstance(e, click.ClickException):
        return e.message
    # Don't leak internal paths/tracebacks; name the exception type.
    return f"Transcription failed ({type(e).__name__}). See the log for detail."


# ---- module-level singleton ------------------------------------------------

# One registry per web process. The single worker means two transcriptions can
# never fight over the Whisper model. Tests construct their own JobRegistry()
# rather than mutating this one.
registry = JobRegistry()
