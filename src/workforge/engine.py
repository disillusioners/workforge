"""Job execution engine.

Every job runs as a subprocess of the WorkForge server process
(``sys.executable script.py args...``) with ``cwd`` set to the job directory.
A dedicated reader thread per pipe pumps combined stdout+stderr into
``jobs/<job_id>/job.log`` line by line, so ``get_log`` works mid-run.

Sync tools call :func:`run_sync` (blocks in the caller's thread); async tools
submit the same execution path to a small in-process ThreadPoolExecutor —
demo-grade, no external queue.

Timeouts: on expiry the entire process group is SIGKILLed and the job is
marked ``failed`` with a timeout note. A job never hangs the server.

Signal handlers (SIGINT/SIGTERM) are installed at import time — see
:func:`_install_signal_handlers` below.
"""

from __future__ import annotations

import atexit
import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any

from . import storage

# Concurrent async jobs; additional submissions stay "queued" until a slot
# frees up. Demo-grade: single process, no persistence across restarts.
MAX_WORKERS = 4

_pool: ThreadPoolExecutor | None = None
_pool_lock = threading.Lock()
_pool_failed = False

# Tracks running subprocesses by job_id so the shutdown hook can SIGKILL
# in-flight children without waiting for the worker thread to return.
# Non-daemon pool workers block interpreter shutdown until each in-flight
# job completes — these hooks break that wait.
_in_flight: dict[str, subprocess.Popen] = {}
_in_flight_lock = threading.Lock()

# job_id -> (meta, future) for jobs submitted to the pool but not yet picked
# up by a worker. Lets shutdown finalize futures dropped by
# cancel_futures=True instead of stranding them on disk as "queued" forever.
_pending: dict[str, tuple[dict[str, Any], Future]] = {}
_pending_lock = threading.Lock()


def _get_pool() -> ThreadPoolExecutor:
    """Return the shared job pool, recreating it if a prior pool broke."""
    global _pool, _pool_failed
    with _pool_lock:
        if _pool is None or _pool_failed:
            _pool = ThreadPoolExecutor(
                max_workers=MAX_WORKERS, thread_name_prefix="workforge-job"
            )
            _pool_failed = False
        return _pool


def _shutdown_pool() -> None:
    """Cancel pending futures and SIGKILL in-flight subprocess children.

    Idempotent. Registered for atexit + SIGINT/SIGTERM so a non-daemon pool
    cannot wedge the process until every worker thread finishes.
    """
    global _pool
    with _pool_lock:
        pool = _pool
        _pool = None
    # Kill children first — otherwise the worker thread can sit in
    # proc.wait() long after the pool has shut down.
    with _in_flight_lock:
        inflight = list(_in_flight.values())
    for proc in inflight:
        # Group kill: a bare proc.kill() would orphan the script's own
        # children. _kill_tree tolerates races with children that already
        # exited (ProcessLookupError/ESRCH) without raising.
        _kill_tree(proc)
    if pool is not None:
        with _pending_lock:
            pending = list(_pending.values())
            _pending.clear()
        # cancel_futures drops queued work; wait=False lets our own cleanup
        # return promptly. The reader threads are daemon, so they vanish.
        pool.shutdown(wait=False, cancel_futures=True)
        # A job still queued here had its future cancelled before _execute
        # ever ran — finalize it so it doesn't sit on disk as "queued".
        for meta, future in pending:
            if not future.cancelled():
                continue  # already started/completed; _execute finalizes it
            meta["status"] = "failed"
            meta["finished_at"] = storage.now_iso()
            meta["error"] = "interrupted: shutdown before start"
            _safe_write_job_meta(meta)
            _safe_record_history(meta)


atexit.register(_shutdown_pool)


def _install_signal_handlers() -> None:
    """Replace SIGINT/SIGTERM so a hung pool cannot block shutdown."""
    def _handler(signum, _frame):
        _shutdown_pool()
        # Re-deliver the signal to this process: after restoring SIG_DFL,
        # os.kill sends the same signal again and the OS default action
        # (terminate) takes over — this Python handler has done its shutdown
        # work and must not run a second time.
        try:
            signal.signal(signum, signal.SIG_DFL)
        except (ValueError, OSError):
            pass
        try:
            os.kill(os.getpid(), signum)
        except OSError:
            pass

    if _IS_WINDOWS:
        # Best-effort on Windows: atexit above still runs.
        return
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _handler)
        except (ValueError, OSError):
            # Not in main thread, or unsupported platform — atexit still
            # provides a safety net.
            pass


_IS_WINDOWS = os.name == "nt"
_install_signal_handlers()


# ----------------------------------------------------------------- public API


# Keys exposed via get_output (full result incl. captured stdout/stderr).
# Notably excludes script_path: an absolute filesystem path leaks server
# internals to the agent and is not actionable from the client side.
OUTPUT_KEYS: tuple[str, ...] = (
    "job_id", "script", "args", "status", "exit_code",
    "submitted_at", "started_at", "finished_at", "duration_ms",
    "stdout", "stderr", "error",
)
# job_status drops the bulky captured output but keeps everything else.
STATUS_KEYS: tuple[str, ...] = tuple(k for k in OUTPUT_KEYS if k not in ("stdout", "stderr"))


def run_sync(name: str, args: list[Any], timeout_seconds: int) -> dict[str, Any]:
    """Run a script and block until it finishes. Returns the run result."""
    meta = _new_meta(name, args, timeout_seconds)
    storage.write_job_meta(meta)
    _execute(meta)
    return _run_result(meta)


def submit(name: str, args: list[Any], timeout_seconds: int) -> dict[str, Any]:
    """Queue a script for background execution. Returns immediately."""
    global _pool_failed
    meta = _new_meta(name, args, timeout_seconds)
    storage.write_job_meta(meta)
    try:
        future = _get_pool().submit(_execute, meta)
        with _pending_lock:
            _pending[meta["job_id"]] = (meta, future)
    except RuntimeError as exc:
        # Pool is broken (e.g. after interpreter shutdown); flag it so the
        # next submission builds a fresh pool instead of reusing a dead
        # executor. The record was already persisted as "queued" — finalize
        # it as "failed" so it isn't stranded on disk forever.
        with _pool_lock:
            _pool_failed = True
        meta["status"] = "failed"
        meta["finished_at"] = storage.now_iso()
        meta["error"] = f"submission failed: {exc!r}"
        _safe_write_job_meta(meta)
        _safe_record_history(meta)
        raise
    return {"job_id": meta["job_id"], "status": meta["status"]}


def get_status(job_id: str) -> dict[str, Any]:
    """Job record without the (potentially large) captured output."""
    meta = storage.read_job_meta(job_id)
    return {k: meta[k] for k in STATUS_KEYS if k in meta}


def get_output(job_id: str) -> dict[str, Any]:
    """Full structured result for a job."""
    meta = storage.read_job_meta(job_id)
    return {k: meta[k] for k in OUTPUT_KEYS if k in meta}


# ------------------------------------------------------------------ execution


def _new_meta(name: str, args: list[Any], timeout_seconds: int) -> dict[str, Any]:
    script = storage.require_script(name)
    timeout_seconds = _validate_timeout(timeout_seconds)
    job_id = uuid.uuid4().hex
    job_d = storage.job_dir(job_id)
    job_d.mkdir(parents=True, exist_ok=True)
    return {
        "job_id": job_id,
        "script": name,
        "script_path": str(script),
        "args": [str(a) for a in args],
        "timeout_seconds": timeout_seconds,
        "status": "queued",
        "exit_code": None,
        "submitted_at": storage.now_iso(),
        "started_at": None,
        "finished_at": None,
        "duration_ms": None,
        "stdout": "",
        "stderr": "",
        "error": None,
    }


def _execute(meta: dict[str, Any]) -> None:
    """Run one job to completion, updating meta.json along the way.

    Any failure in here (e.g. the process cannot be spawned at all) finalizes
    the job as ``failed`` so it never sits on disk stuck in ``running``.
    """
    # No longer queued: deregister so a concurrent shutdown won't finalize
    # this job as cancelled before _execute starts updating its meta.
    with _pending_lock:
        _pending.pop(meta["job_id"], None)
    job_d = storage.job_dir(meta["job_id"])
    # Captures + start time live OUTSIDE the try so the except handler can
    # hoist them even if proc.wait() / reader joins raise mid-flight. Without
    # this, a failure between wait() and the buffer joins would persist an
    # empty stdout/stderr and erase any progress the script already made.
    started = time.monotonic()
    stdout_buf: list[str] = []
    stderr_buf: list[str] = []
    proc: subprocess.Popen | None = None
    try:
        meta["status"] = "running"
        meta["started_at"] = storage.now_iso()
        storage.write_job_meta(meta)
        _safe_record_history(meta)  # INSERT (status=running) when history is on

        cmd = [sys.executable, meta["script_path"], *meta["args"]]
        proc = subprocess.Popen(
            cmd,
            cwd=job_d,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=not _IS_WINDOWS,  # own process group -> group kill
        )
        # Register so a shutdown hook can SIGKILL us without waiting for
        # proc.wait() to return naturally.
        with _in_flight_lock:
            _in_flight[meta["job_id"]] = proc

        # One reader thread per pipe; both append into the shared job.log,
        # serialized by _LOG_LOCK.
        log_path = storage.job_log_path(meta["job_id"])
        readers = [
            threading.Thread(
                target=_pump,
                args=(proc.stdout, stdout_buf, log_path),
                daemon=True,
                name=f"wf-out-{meta['job_id'][:8]}",
            ),
            threading.Thread(
                target=_pump,
                args=(proc.stderr, stderr_buf, log_path),
                daemon=True,
                name=f"wf-err-{meta['job_id'][:8]}",
            ),
        ]
        for t in readers:
            t.start()

        # timeout_seconds is enforced as >= 1 by _validate_timeout, so a
        # plain integer wait is sufficient (no "or None" magic).
        timed_out = False
        try:
            proc.wait(timeout=meta["timeout_seconds"])
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_tree(proc)

        for t in readers:
            t.join(timeout=5)
            if t.is_alive():
                with _LOG_LOCK, open(log_path, "a", encoding="utf-8") as log:
                    log.write(
                        f"[workforge] warning: reader {t.name} did not finish draining\n"
                    )

        meta["finished_at"] = storage.now_iso()
        meta["duration_ms"] = int((time.monotonic() - started) * 1000)
        meta["stdout"] = "".join(stdout_buf)
        meta["stderr"] = "".join(stderr_buf)
        if timed_out:
            meta["status"] = "failed"
            meta["exit_code"] = None
            meta["error"] = (
                f"timed out after {meta['timeout_seconds']}s; process group killed"
            )
        else:
            meta["exit_code"] = proc.returncode
            if proc.returncode == 0:
                meta["status"] = "succeeded"
            else:
                meta["status"] = "failed"
                meta["error"] = f"exited with code {proc.returncode}"
        _safe_write_job_meta(meta)
        _safe_record_history(meta)  # UPDATE to the final status
    except Exception as exc:
        # Spawn/pump failure (e.g. OSError from Popen): finalize the record so
        # an async job never ends up "running" forever with no process.
        # Hoist captures FIRST so anything the script already wrote survives.
        meta["stdout"] = "".join(stdout_buf)
        meta["stderr"] = "".join(stderr_buf)
        meta["status"] = "failed"
        meta["error"] = repr(exc)
        meta["finished_at"] = storage.now_iso()
        if proc is not None:
            try:
                _kill_tree(proc)
            except Exception:
                pass
        _safe_write_job_meta(meta)
        _safe_record_history(meta)
    finally:
        with _in_flight_lock:
            _in_flight.pop(meta["job_id"], None)


# Serializes job.log appends from the two per-pipe reader threads; without it,
# lines longer than PIPE_BUF could interleave byte-wise between the handles.
_LOG_LOCK = threading.Lock()


def _pump(stream, sink: list[str], log_path: Path) -> None:
    """Drain a subprocess pipe into memory and the live job.log."""
    try:
        with open(log_path, "a", encoding="utf-8", buffering=1) as log:
            for raw in iter(stream.readline, b""):
                line = raw.decode("utf-8", errors="replace")
                sink.append(line)
                with _LOG_LOCK:
                    log.write(line)
                    log.flush()
    finally:
        stream.close()


def _kill_tree(proc: subprocess.Popen) -> None:
    """Kill the job's whole process group (the script may have children)."""
    if _IS_WINDOWS:
        _terminate(proc)
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        _terminate(proc)
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass  # reader joins below still bound the wait


def _terminate(proc: subprocess.Popen) -> None:
    try:
        proc.kill()
    except OSError:
        pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


def _safe_record_history(meta: dict[str, Any]) -> None:
    """Best-effort PG history write (phase 2). NEVER fails the job.

    HARD RULES (feature A):
    - No PG contact at all while WORKFORGE_DATABASE_URL is unset — checked
      here first, so a history-less install does zero extra work and job
      metas stay unpolluted.
    - Any failure (connection, timeout, bad URL, ...) is swallowed and
      recorded as ``history_write_error`` in the on-disk job meta, then the
      meta is re-persisted so the error survives restarts.
    """
    if not os.environ.get("WORKFORGE_DATABASE_URL", "").strip():
        return
    try:
        from . import history  # lazy: psycopg never loads unless used

        history.record_job(meta)
    except Exception as exc:
        meta["history_write_error"] = repr(exc)[:500]
        _safe_write_job_meta(meta)


def _safe_write_job_meta(meta: dict[str, Any]) -> None:
    """Persist job meta; on failure, retry once then give up silently.

    A failing final write must not wedge the job as "running" forever; we
    do our best to persist and log any second failure to stderr. The
    double-write covers the common "transient fs hiccup" case (tmp file
    conflict after a fast crash recovery, ENOSPC flush, etc.).
    """
    try:
        storage.write_job_meta(meta)
        return
    except Exception:
        pass
    try:
        storage.write_job_meta(meta)
    except Exception as exc:
        sys.stderr.write(
            f"[workforge] failed to persist final meta for {meta.get('job_id')}: {exc!r}\n"
        )
        sys.stderr.flush()


def _run_result(meta: dict[str, Any]) -> dict[str, Any]:
    return {
        "job_id": meta["job_id"],
        "status": meta["status"],
        "exit_code": meta["exit_code"],
        "stdout": meta["stdout"],
        "stderr": meta["stderr"],
        "duration_ms": meta["duration_ms"],
        "error": meta["error"],
    }


def _validate_timeout(timeout_seconds: int) -> int:
    try:
        timeout = int(timeout_seconds)
    except (TypeError, ValueError):
        raise ValueError("timeout_seconds must be a positive integer") from None
    if timeout < 1:
        # R2: a zero/negative timeout would mean "no timeout", which silently
        # wedges async jobs in the pool. Reject explicitly instead.
        raise ValueError(
            f"timeout_seconds must be >= 1 (got {timeout}); "
            "0/negative means no timeout, which is rejected for safety"
        )
    return timeout
