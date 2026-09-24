"""Optional persistent run history in Postgres (phase 2, feature A).

Enabled ONLY when ``WORKFORGE_DATABASE_URL`` is set — read at call time (never
at import time) so tests can point it at ``workforge_test`` via monkeypatch.
With the variable unset:

- the ``list_history`` / ``history_detail`` tools raise a structured ToolError
  (a ValueError here; fastmcp converts it at the tool boundary),
- the engine records nothing and never touches PG at all.

The engine's writes go through :func:`record_job` (upsert keyed on job_id) and
are wrapped by ``engine._safe_record_history``: a PG failure NEVER fails the
job — it lands as ``history_write_error`` in the on-disk job meta instead.

All PG operations are bounded: ~5s connect timeout plus a short statement
timeout, so a dead/unreachable database can never hang an MCP tool or a job.

Connections come from a tiny ``psycopg_pool`` pool (created lazily per
connection string; no connection is attempted at import time). The schema is
ensured idempotently on the first connection of each pool.

``db_init`` is the guarded CLI helper behind ``workforge db-init`` / the
``workforge-db-init`` script: it reads ``pg_database`` for existence, may
CREATE DATABASE — and only ever for the allow-listed names ``workforge`` /
``workforge_test`` (the localhost cluster may hold real data). Nothing else is
ever executed on a maintenance connection.
"""

from __future__ import annotations

import os
import threading
from datetime import datetime
from typing import Any

NOT_CONFIGURED_MSG = "run history not configured: set WORKFORGE_DATABASE_URL"

# Short timeouts so a dead PG never hangs MCP tools or the job engine.
CONNECT_TIMEOUT_S = 5
STATEMENT_TIMEOUT_MS = 5000
POOL_TIMEOUT_S = 5

DEFAULT_LIST_LIMIT = 50
LIST_LIMIT_CAP = 200

# Statuses a job can legitimately carry in history (same set as job_status).
VALID_STATUSES: tuple[str, ...] = ("queued", "running", "succeeded", "failed")

# db-init may create ONLY these databases (see module docstring: the local
# cluster may contain real, unrelated data).
ALLOWED_INIT_DBS = ("workforge", "workforge_test")
MAINTENANCE_DB = "postgres"

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS job_runs (
    job_id TEXT PRIMARY KEY,
    script_name TEXT NOT NULL,
    args JSONB DEFAULT '[]',
    timeout_seconds INT,
    status TEXT NOT NULL,
    exit_code INT,
    stdout TEXT,
    stderr TEXT,
    duration_ms BIGINT,
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT now()
)
"""

# created_at DESC serves the "newest first" list_history ordering; script_name
# serves the script_name filter.
_INDEX_SQL: tuple[str, ...] = (
    "CREATE INDEX IF NOT EXISTS job_runs_created_at_idx ON job_runs (created_at DESC)",
    "CREATE INDEX IF NOT EXISTS job_runs_script_name_idx ON job_runs (script_name)",
)

_INSERT_SQL = """
INSERT INTO job_runs (
    job_id, script_name, args, timeout_seconds, status,
    exit_code, stdout, stderr, duration_ms, started_at, finished_at
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (job_id) DO UPDATE SET
    script_name = EXCLUDED.script_name,
    args = EXCLUDED.args,
    timeout_seconds = EXCLUDED.timeout_seconds,
    status = EXCLUDED.status,
    exit_code = EXCLUDED.exit_code,
    stdout = EXCLUDED.stdout,
    stderr = EXCLUDED.stderr,
    duration_ms = EXCLUDED.duration_ms,
    started_at = COALESCE(EXCLUDED.started_at, job_runs.started_at),
    finished_at = COALESCE(EXCLUDED.finished_at, job_runs.finished_at)
"""

_LIST_SQL = """
SELECT job_id, script_name, status, exit_code, started_at, duration_ms
FROM job_runs
"""

_DETAIL_SQL = """
SELECT job_id, script_name, args, timeout_seconds, status, exit_code,
       stdout, stderr, duration_ms, started_at, finished_at
FROM job_runs
WHERE job_id = %s
"""


# ------------------------------------------------------------------ plumbing


def database_url() -> str:
    """The configured history URL, read at call time; ValueError if unset."""
    url = os.environ.get("WORKFORGE_DATABASE_URL", "").strip()
    if not url:
        raise ValueError(NOT_CONFIGURED_MSG)
    return url


def is_configured() -> bool:
    """True when a history URL is set (cheap; no PG contact)."""
    return bool(os.environ.get("WORKFORGE_DATABASE_URL", "").strip())


# One small pool per connection string. Keyed by URL so a monkeypatched env in
# tests (or a rotated URL in ops) simply gets a fresh pool instead of reusing
# connections pointed at the wrong database.
_pools: dict[str, Any] = {}
_pools_lock = threading.Lock()

# Connection strings whose schema has been ensured in this process.
_schema_ready: set[str] = set()
_schema_lock = threading.Lock()


def _get_pool(url: str) -> Any:
    """Lazy, per-URL connection pool. No connection is made before first use."""
    with _pools_lock:
        pool = _pools.get(url)
        if pool is None:
            from psycopg_pool import ConnectionPool

            pool = ConnectionPool(
                url,
                min_size=0,  # nothing is attempted until a client asks
                max_size=2,
                timeout=POOL_TIMEOUT_S,
                open=True,
                kwargs={
                    "connect_timeout": CONNECT_TIMEOUT_S,
                    "options": f"-c statement_timeout={STATEMENT_TIMEOUT_MS}",
                    "autocommit": True,
                },
            )
            _pools[url] = pool
        return pool


def reset_pools() -> None:
    """Close and forget all pools (ops/test hook: env or URL rotated)."""
    with _pools_lock:
        pools = list(_pools.values())
        _pools.clear()
        _schema_ready.clear()
    for pool in pools:
        try:
            pool.close()
        except Exception:
            pass


def _ensure_schema(conn: Any, url: str) -> None:
    """Idempotent CREATE TABLE/INDEX IF NOT EXISTS, once per process+URL."""
    with _schema_lock:
        if url in _schema_ready:
            return
        conn.execute(_SCHEMA_SQL)
        for stmt in _INDEX_SQL:
            conn.execute(stmt)
        _schema_ready.add(url)


def _connection(url: str) -> Any:
    """Pool connection with the schema ensured (context-manager use)."""
    pool = _get_pool(url)
    return pool.connection(timeout=POOL_TIMEOUT_S)


# --------------------------------------------------------------- engine path


def record_job(meta: dict[str, Any]) -> None:
    """Upsert one job record (start write: status=running; finalize: final).

    Raises on any PG problem — the engine catches everything and records
    ``history_write_error`` in the job meta instead; a PG failure NEVER fails
    a job.
    """
    from psycopg.types.json import Json

    url = database_url()
    with _connection(url) as conn:
        _ensure_schema(conn, url)
        conn.execute(
            _INSERT_SQL,
            (
                meta["job_id"],
                meta["script"],
                Json(list(meta.get("args") or [])),
                meta.get("timeout_seconds"),
                meta["status"],
                meta.get("exit_code"),
                meta.get("stdout"),
                meta.get("stderr"),
                meta.get("duration_ms"),
                _to_ts(meta.get("started_at")),
                _to_ts(meta.get("finished_at")),
            ),
        )


def _to_ts(value: Any) -> datetime | None:
    """ISO string (storage.now_iso shape) -> aware datetime, or None."""
    if value is None or isinstance(value, datetime):
        return value
    return datetime.fromisoformat(value)


def _to_iso(value: Any) -> str | None:
    """PG timestamptz -> ISO string, matching the on-disk meta style."""
    if value is None or isinstance(value, str):
        return value
    return value.isoformat()


# ---------------------------------------------------------------- tool path


def _validate_limit(limit: Any) -> int:
    """int in [1, 200]; anything else is a ValueError (ToolError at the tool)."""
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise ValueError(f"invalid limit {limit!r}: expected an integer")
    if limit < 1:
        raise ValueError(f"limit must be >= 1 (got {limit})")
    if limit > LIST_LIMIT_CAP:
        raise ValueError(
            f"limit must be <= {LIST_LIMIT_CAP} (got {limit}); "
            "large result sets would be unwieldy"
        )
    return limit


def _validate_status(status: Any) -> str | None:
    if status is None:
        return None
    if status not in VALID_STATUSES:
        raise ValueError(
            f"invalid status {status!r}: expected one of {', '.join(VALID_STATUSES)}"
        )
    return status


def _validate_script_name(script_name: Any) -> str | None:
    if script_name is None:
        return None
    if not isinstance(script_name, str) or not script_name:
        raise ValueError("script_name filter must be a non-empty string")
    return script_name


def list_runs(
    limit: int = DEFAULT_LIST_LIMIT,
    script_name: str | None = None,
    status: str | None = None,
) -> list[dict[str, Any]]:
    """Recent runs, newest first: [{job_id, script_name, status, exit_code,
    started_at, duration_ms}]."""
    limit = _validate_limit(limit)
    script_name = _validate_script_name(script_name)
    status = _validate_status(status)

    clauses: list[str] = []
    params: list[Any] = []
    if script_name is not None:
        clauses.append("script_name = %s")
        params.append(script_name)
    if status is not None:
        clauses.append("status = %s")
        params.append(status)
    query = _LIST_SQL
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY created_at DESC LIMIT %s"
    params.append(limit)

    url = database_url()
    with _connection(url) as conn:
        _ensure_schema(conn, url)
        rows = conn.execute(query, params).fetchall()
    return [
        {
            "job_id": row[0],
            "script_name": row[1],
            "status": row[2],
            "exit_code": row[3],
            "started_at": _to_iso(row[4]),
            "duration_ms": row[5],
        }
        for row in rows
    ]


def get_run_detail(job_id: str) -> dict[str, Any]:
    """Full record for one job: {job_id, input: {...}, output: {...}}.

    Raises ValueError (ToolError at the tool boundary) for a malformed or
    unknown job_id.
    """
    from . import storage

    storage.validate_job_id(job_id)  # same format rule as the file-based tools
    url = database_url()
    with _connection(url) as conn:
        _ensure_schema(conn, url)
        row = conn.execute(_DETAIL_SQL, (job_id,)).fetchone()
    if row is None:
        raise ValueError(f"unknown job {job_id!r}")
    return {
        "job_id": row[0],
        "input": {
            "script_name": row[1],
            "args": row[2],
            "timeout_seconds": row[3],
        },
        "output": {
            "status": row[4],
            "exit_code": row[5],
            "stdout": row[6],
            "stderr": row[7],
            "duration_ms": row[8],
            "started_at": _to_iso(row[9]),
            "finished_at": _to_iso(row[10]),
        },
    }


# ------------------------------------------------------------------- db-init


def db_init() -> dict[str, Any]:
    """Guarded database bootstrap: ensure the target DB exists, then schema.

    Safety (hard project constraint): the maintenance connection does exactly
    two things — read ``pg_database`` for existence, and CREATE DATABASE for
    the target name if missing, where the target MUST be allow-listed as
    ``workforge`` or ``workforge_test``. Any other database is refused before
    connecting. No DROP/ALTER/TRUNCATE, ever.
    """
    from psycopg.conninfo import conninfo_to_dict, make_conninfo
    from psycopg import sql

    url = database_url()
    parsed = conninfo_to_dict(url)
    target = parsed.get("dbname")
    if not target:
        raise ValueError(
            "WORKFORGE_DATABASE_URL must name a database for db-init "
            "(e.g. postgresql:///workforge)"
        )
    if target not in ALLOWED_INIT_DBS:
        raise ValueError(
            f"db-init refuses database {target!r}: only "
            f"{', '.join(ALLOWED_INIT_DBS)} may be created "
            "(the localhost cluster may contain real data)"
        )

    # Maintenance connection: same host/user as the URL, database 'postgres'.
    maintenance = make_conninfo(**{**parsed, "dbname": MAINTENANCE_DB})
    created = False
    with _psycopg().connect(
        maintenance, connect_timeout=CONNECT_TIMEOUT_S, autocommit=True
    ) as conn:
        exists = conn.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (target,)
        ).fetchone()
        if exists is None:
            conn.execute(
                sql.SQL("CREATE DATABASE {}").format(sql.Identifier(target))
            )
            created = True

    with _connection(url) as conn:
        _ensure_schema(conn, url)
    return {"database": target, "created": created, "schema": "ok"}


def _psycopg() -> Any:
    """Lazy driver import: keeps psycopg off the engine's import path."""
    import psycopg

    return psycopg
