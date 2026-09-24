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
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from . import storage

# Concurrent async jobs; additional submissions stay "queued" until a slot
# frees up. Demo-grade: single process, no persistence across restarts.
MAX_WORKERS = 4

_pool: ThreadPoolExecutor | None = None
_pool_lock = threading.Lock()
_pool_failed = False


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


# ----------------------------------------------------------------- public API


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
        _get_pool().submit(_execute, meta)
    except RuntimeError:
        # Pool is broken (e.g. after interpreter shutdown); flag it so the next
        # submission builds a fresh pool instead of reusing a dead executor.
        with _pool_lock:
            _pool_failed = True
        raise
    return {"job_id": meta["job_id"], "status": meta["status"]}


def get_status(job_id: str) -> dict[str, Any]:
    """Job record without the (potentially large) captured output."""
    meta = storage.read_job_meta(job_id)
    return {k: v for k, v in meta.items() if k not in ("stdout", "stderr")}


def get_output(job_id: str) -> dict[str, Any]:
    """Full structured result for a job."""
    return storage.read_job_meta(job_id)


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
    job_d = storage.job_dir(meta["job_id"])
    try:
        meta["status"] = "running"
        meta["started_at"] = storage.now_iso()
        storage.write_job_meta(meta)

        cmd = [sys.executable, meta["script_path"], *meta["args"]]
        started = time.monotonic()
        stdout_buf: list[str] = []
        stderr_buf: list[str] = []

        proc = subprocess.Popen(
            cmd,
            cwd=job_d,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=not _IS_WINDOWS,  # own process group -> group kill
        )

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

        timeout = meta["timeout_seconds"] or None
        timed_out = False
        try:
            proc.wait(timeout=timeout)
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
        storage.write_job_meta(meta)
    except Exception as exc:
        # Spawn/pump failure (e.g. OSError from Popen): finalize the record so
        # an async job never ends up "running" forever with no process.
        meta["status"] = "failed"
        meta["error"] = repr(exc)
        meta["finished_at"] = storage.now_iso()
        storage.write_job_meta(meta)


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
        raise ValueError("timeout_seconds must be a non-negative integer") from None
    if timeout < 0:
        raise ValueError("timeout_seconds must be >= 0 (0 = no timeout)")
    return timeout


_IS_WINDOWS = os.name == "nt"
