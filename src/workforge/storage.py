"""Filesystem storage for WorkForge.

All persistent state lives under ``WORKFORGE_HOME`` (default ``~/.workforge``):

    scripts/<name>.py      # saved scripts
    scripts/<name>.json    # script metadata sidecar (description, size, updated_at)
    jobs/<job_id>/meta.json  # job record (status, timestamps, exit code, output)
    jobs/<job_id>/job.log    # combined stdout+stderr, streamed live

The home directory is resolved on every call (not cached at import time) so
tests can point it at a temp directory via the environment variable.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Script names are slugs: lowercase letter first, then letters/digits/-/_.
# This doubles as path-traversal protection (no dots, no separators).
# fullmatch (not match + $) so trailing newlines/whitespace cannot sneak
# through (`"abc\n"` would otherwise pass and create `scripts/abc\n.py`).
NAME_RE = re.compile(r"[a-z][a-z0-9_-]{0,63}")

# Job ids are uuid4 hex: exactly 32 lowercase hex chars. Validated before any
# path use so a crafted id cannot escape jobs/ (defense in depth). Same
# fullmatch discipline as NAME_RE.
JOB_ID_RE = re.compile(r"[0-9a-f]{32}")


def home() -> Path:
    """Root directory for all WorkForge state."""
    raw = os.environ.get("WORKFORGE_HOME")
    return Path(raw).expanduser() if raw else Path.home() / ".workforge"


def scripts_dir() -> Path:
    return home() / "scripts"


def jobs_dir() -> Path:
    return home() / "jobs"


def job_dir(job_id: str) -> Path:
    return jobs_dir() / job_id


def script_path(name: str) -> Path:
    return scripts_dir() / f"{name}.py"


def script_meta_path(name: str) -> Path:
    return scripts_dir() / f"{name}.json"


def job_meta_path(job_id: str) -> Path:
    return job_dir(job_id) / "meta.json"


def job_log_path(job_id: str) -> Path:
    return job_dir(job_id) / "job.log"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def validate_name(name: str) -> str:
    """Validate and return a script name, or raise ValueError."""
    if not isinstance(name, str) or not NAME_RE.fullmatch(name):
        raise ValueError(
            f"invalid script name {name!r}: must match [a-z][a-z0-9_-]* "
            "(lowercase slug, max 64 chars)"
        )
    return name


def validate_job_id(job_id: str) -> str:
    """Validate and return a job id, or raise ValueError."""
    if not isinstance(job_id, str) or not JOB_ID_RE.fullmatch(job_id):
        raise ValueError(f"invalid job_id {job_id!r}: expected 32-char hex string")
    return job_id


def save_script(name: str, content: str, description: str = "") -> dict[str, Any]:
    """Persist a script (and its metadata sidecar); overwrite allowed."""
    validate_name(name)
    data = content.encode("utf-8")
    sdir = scripts_dir()
    sdir.mkdir(parents=True, exist_ok=True)
    spath = script_path(name)
    # Atomic write: tmp + os.replace. A crash mid-write leaves the previous
    # good copy (or no file) — never a truncated script that later executes.
    # Unique tmp name per call so concurrent save_script(name) calls do not
    # race over a fixed tmp path.
    tmp = sdir / f".{name}.{uuid.uuid4().hex}.tmp"
    try:
        tmp.write_bytes(data)
        os.replace(tmp, spath)
    except Exception:
        # Clean up partial tmp so the scripts/ dir doesn't accumulate junk.
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    meta = {
        "name": name,
        "description": description or "",
        "size": len(data),
        "updated_at": now_iso(),
    }
    _write_json(script_meta_path(name), meta)
    return dict(meta)


def list_scripts() -> list[dict[str, Any]]:
    """All saved scripts, ordered by name."""
    out: list[dict[str, Any]] = []
    sdir = scripts_dir()
    if not sdir.is_dir():
        return out
    for path in sorted(sdir.glob("*.json")):
        try:
            meta = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        name = meta.get("name") or path.stem
        # Re-validate after read: a tampered sidecar ({"name": "../escape"})
        # could otherwise escape scripts/. Drop invalid entries silently.
        try:
            validate_name(name)
        except ValueError:
            continue
        # Trust the file on disk for size (source of truth).
        spath = script_path(name)
        if not spath.exists():
            continue
        meta["size"] = spath.stat().st_size
        out.append(meta)
    return out


def require_script(name: str) -> Path:
    """Return the script path, raising ValueError if unknown/invalid."""
    validate_name(name)
    spath = script_path(name)
    if not spath.exists():
        raise ValueError(f"unknown script {name!r} (save it first with save_script)")
    return spath


# ---------------------------------------------------------------- job records


def read_job_meta(job_id: str) -> dict[str, Any]:
    validate_job_id(job_id)
    path = job_meta_path(job_id)
    if not path.exists():
        raise ValueError(f"unknown job {job_id!r}")
    return json.loads(path.read_text(encoding="utf-8"))


def write_job_meta(meta: dict[str, Any]) -> None:
    """Atomically persist a job record (tmp file + rename)."""
    path = job_meta_path(meta["job_id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    # Unique tmp name per call so concurrent writes cannot interleave bytes.
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def read_job_log(job_id: str, tail: int | None = None) -> str:
    """Combined stdout+stderr so far. Safe to call while the job runs.

    Raises ValueError for unknown jobs; returns "" if nothing logged yet.
    """
    validate_job_id(job_id)
    if not job_meta_path(job_id).exists():
        raise ValueError(f"unknown job {job_id!r}")
    log = job_log_path(job_id)
    if not log.exists():
        return ""
    text = log.read_text(encoding="utf-8", errors="replace")
    if tail is not None:
        if tail < 0:
            raise ValueError("tail must be >= 0")
        # tail=0 must yield empty string: `text.splitlines()[-0:]` is the
        # WHOLE list (because `-0 == 0`); short-circuit here so the contract
        # is "last N lines, 0 = none" instead of "0 = everything".
        if tail == 0:
            return ""
        text = "\n".join(text.splitlines()[-tail:])
    return text


# -------------------------------------------------------------------- helpers


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Unique tmp name per call (same formula as the script-body write above
    # and write_job_meta) so concurrent calls don't race over a fixed tmp path.
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, path)
