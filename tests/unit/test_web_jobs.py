"""Tests for rec.web.jobs — the single-worker transcription registry.

Offline: the worker entry points (_run_finish / _run_transcribe) are
monkeypatched so no model loads and no real transcription happens. The
single-worker pool is exercised for real (queueing + state transitions), and
the duplicate-session guard (409) is the load-bearing assertion.
"""

from __future__ import annotations

import threading
import time

import pytest

from rec import config, session
from rec.web import jobs

# ---- helpers --------------------------------------------------------------


def _make_recorded_session() -> str:
    """A session with a WAV present, in a pre-transcription state.

    Uses a per-call timestamp so tests creating two sessions get distinct ids
    (new_session_id resolves to the second; a tight loop would collide).
    """
    from datetime import datetime, timedelta

    if not hasattr(_make_recorded_session, "_n"):
        _make_recorded_session._n = 0  # type: ignore[attr-defined]
    _make_recorded_session._n += 1  # type: ignore[attr-defined]
    now = datetime(2025, 1, 1, 9, 0, 0) + timedelta(minutes=_make_recorded_session._n)  # type: ignore[attr-defined]
    sid = session.new_session_id(now=now)
    session.update_meta(sid, status=session.STATUS_RECORDED, duration=None, word_count=None)
    session.create_session_dir(sid)
    session.wav_path(sid).write_bytes(b"RIFF...fake")
    return sid


def _write_config() -> None:
    config.save_config(config.default_config())


def _drain(registry: jobs.JobRegistry, job: jobs.Job, timeout: float = 2.0) -> jobs.Job:
    """Poll the registry until the job reaches a terminal state, return it."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        cur = registry.get(job.id)
        assert cur is not None
        if cur.state in (jobs.JobState.done, jobs.JobState.error):
            return cur
        time.sleep(0.01)
    raise AssertionError(f"job {job.id} never finished; last state={registry.get(job.id)}")


# ---- queue + state transitions -------------------------------------------


def test_queue_finish_runs_to_done(monkeypatch, xdg):
    _write_config()
    sid = _make_recorded_session()
    ran: list[str] = []

    def fake_run_finish(session_id, cfg, model_override):
        ran.append(session_id)

    monkeypatch.setattr(jobs, "_run_finish", fake_run_finish)

    reg = jobs.JobRegistry()
    try:
        job = reg.queue(sid, kind=jobs.JOB_FINISH)
        # Don't assert on the just-queued state — the single worker may already
        # have flipped it to running. _drain pins the terminal state.
        finished = _drain(reg, job)
        assert finished.state == jobs.JobState.done
        assert ran == [sid]
    finally:
        reg.shutdown()


def test_queue_transcribe_runs_to_done(monkeypatch, xdg):
    _write_config()
    sid = _make_recorded_session()
    calls: list[tuple] = []

    def fake_run_transcribe(session_id, cfg, model_override):
        calls.append((session_id, model_override))

    monkeypatch.setattr(jobs, "_run_transcribe", fake_run_transcribe)

    reg = jobs.JobRegistry()
    try:
        job = reg.queue(sid, kind=jobs.JOB_TRANSCRIBE, model_override="medium")
        finished = _drain(reg, job)
        assert finished.state == jobs.JobState.done
        assert calls == [(sid, "medium")]
    finally:
        reg.shutdown()


def test_job_to_dict_has_expected_shape(monkeypatch, xdg):
    _write_config()
    sid = _make_recorded_session()
    monkeypatch.setattr(jobs, "_run_finish", lambda *a, **k: None)
    reg = jobs.JobRegistry()
    try:
        job = reg.queue(sid, kind=jobs.JOB_FINISH)
        d = job.to_dict()
        assert d["session_id"] == sid
        assert d["state"] in ("queued", "running", "done", "error")
        assert d["kind"] == jobs.JOB_FINISH
    finally:
        reg.shutdown()


# ---- the duplicate guard (the load-bearing 409 path) ----------------------


def test_duplicate_for_same_session_is_rejected(monkeypatch, xdg):
    """A second job for a session with one queued/running raises DuplicateJob."""
    _write_config()
    sid = _make_recorded_session()
    started = threading.Event()
    release = threading.Event()

    def slow_finish(session_id, cfg, model_override):
        started.set()
        release.wait(timeout=2.0)  # hold the single worker so the job stays running

    monkeypatch.setattr(jobs, "_run_finish", slow_finish)
    reg = jobs.JobRegistry()
    try:
        first = reg.queue(sid, kind=jobs.JOB_FINISH)
        assert started.wait(timeout=2.0), "worker never started"
        # first is now running; a second for the SAME session must be refused.
        with pytest.raises(jobs.DuplicateJob):
            reg.queue(sid, kind=jobs.JOB_FINISH)
        # get() and active_for_session() still see the running job.
        assert reg.get(first.id).state == jobs.JobState.running
        assert reg.active_for_session(sid).id == first.id
    finally:
        release.set()
        reg.shutdown()


def test_different_sessions_queue_independently(monkeypatch, xdg):
    _write_config()
    sid_a = _make_recorded_session()
    sid_b = _make_recorded_session()
    monkeypatch.setattr(jobs, "_run_finish", lambda *a, **k: None)
    reg = jobs.JobRegistry()
    try:
        ja = reg.queue(sid_a, kind=jobs.JOB_FINISH)
        jb = reg.queue(sid_b, kind=jobs.JOB_FINISH)
        assert ja.id != jb.id
        # Both eventually finish (the single worker serialises them).
        assert _drain(reg, ja).state == jobs.JobState.done
        assert _drain(reg, jb).state == jobs.JobState.done
    finally:
        reg.shutdown()


def test_finished_job_does_not_block_requeue(monkeypatch, xdg):
    """A done job frees the session — a new job for it is accepted."""
    _write_config()
    sid = _make_recorded_session()
    monkeypatch.setattr(jobs, "_run_finish", lambda *a, **k: None)
    reg = jobs.JobRegistry()
    try:
        first = reg.queue(sid, kind=jobs.JOB_FINISH)
        assert _drain(reg, first).state == jobs.JobState.done
        # Now a second is fine — no DuplicateJob.
        second = reg.queue(sid, kind=jobs.JOB_FINISH)
        assert _drain(reg, second).state == jobs.JobState.done
    finally:
        reg.shutdown()


# ---- error handling -------------------------------------------------------


def test_worker_error_marks_job_error_not_crash(monkeypatch, xdg):
    """An exception in the worker sets state=error, doesn't propagate."""
    _write_config()
    sid = _make_recorded_session()

    def boom(*a, **k):
        raise RuntimeError("model load failed")

    monkeypatch.setattr(jobs, "_run_finish", boom)
    reg = jobs.JobRegistry()
    try:
        job = reg.queue(sid, kind=jobs.JOB_FINISH)
        finished = _drain(reg, job)
        assert finished.state == jobs.JobState.error
        assert finished.message  # a human-readable sentence
    finally:
        reg.shutdown()


def test_missing_config_marks_job_error(monkeypatch, xdg):
    """No config.json → the job fails cleanly with a setup hint."""
    sid = _make_recorded_session()
    # _run_finish would also fail, but config.load_config() fails first.
    monkeypatch.setattr(jobs, "_run_finish", lambda *a, **k: None)
    reg = jobs.JobRegistry()
    try:
        job = reg.queue(sid, kind=jobs.JOB_FINISH)
        finished = _drain(reg, job)
        assert finished.state == jobs.JobState.error
        assert "setup" in finished.message.lower()
    finally:
        reg.shutdown()
