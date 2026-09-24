"""WorkForge MCP server: the 7 agent-facing tools.

Tools
-----
save_script, list_scripts, run_script, run_script_async,
job_status, get_log, get_output

Served over stdio by default (``workforge.cli:main`` -> ``mcp.run()``).
"""

from __future__ import annotations

from typing import Any

from fastmcp import FastMCP

from . import engine, storage

mcp = FastMCP(
    "WorkForge",
    instructions=(
        "Save Python scripts once, then run them synchronously or in the "
        "background. Logs stream live to disk while a job runs. "
        "DEMO PHASE: scripts run with full user privileges — no sandbox."
    ),
)


@mcp.tool()
def save_script(name: str, content: str, description: str = "") -> dict[str, Any]:
    """Save a Python script under a stable name for later execution.

    Args:
        name: Slug identifier, must match [a-z][a-z0-9_-]* (max 64 chars).
        content: Full Python source code.
        description: Optional human/agent-readable summary.

    Overwrites an existing script with the same name.
    Returns {name, size, updated_at}.
    """
    return storage.save_script(name, content, description)


@mcp.tool()
def list_scripts() -> list[dict[str, Any]]:
    """List all saved scripts as [{name, description, size, updated_at}]."""
    return storage.list_scripts()


@mcp.tool()
def run_script(
    name: str, args: list[str] | None = None, timeout_seconds: int = 120
) -> dict[str, Any]:
    """Run a saved script SYNCHRONOUSLY and block until it finishes.

    Args:
        name: Name of a saved script (see list_scripts / save_script).
        args: Command-line arguments passed to the script (sys.argv[1:]).
        timeout_seconds: Kill the script (and its children) if it runs longer.
            Must be >= 1 (no implicit "0 = no timeout"; rejected as ToolError).

    Returns {job_id, status, exit_code, stdout, stderr, duration_ms, error}.
    On timeout the job is killed and status is "failed" with a timeout note.
    """
    return engine.run_sync(name, args or [], timeout_seconds)


@mcp.tool()
def run_script_async(
    name: str, args: list[str] | None = None, timeout_seconds: int = 300
) -> dict[str, Any]:
    """Run a saved script in the background; returns immediately.

    Args:
        name: Name of a saved script.
        args: Command-line arguments passed to the script.
        timeout_seconds: Kill the script (and its children) if it runs longer.
            Must be >= 1; the default is 300s (5 min) instead of "no timeout"
            so a hung async job cannot permanently saturate the worker pool.

    Returns {job_id, status} — poll job_status(job_id), tail get_log(job_id)
    while it runs (output streams live), and fetch get_output(job_id) at the end.
    """
    return engine.submit(name, args or [], timeout_seconds)


@mcp.tool()
def job_status(job_id: str) -> dict[str, Any]:
    """Get a job's status: queued | running | succeeded | failed.

    Returns a filtered view of the job record:
    {job_id, script, args, status, exit_code, submitted_at, started_at,
    finished_at, duration_ms, error} — the bulky captured stdout/stderr
    are omitted (use get_output for those). ``script_path`` is intentionally
    excluded; agents don't need an absolute filesystem path.
    """
    return engine.get_status(job_id)


@mcp.tool()
def get_log(job_id: str, tail: int | None = None) -> str:
    """Get a job's combined stdout+stderr. Works WHILE the job is running.

    Args:
        job_id: Job identifier.
        tail: If set, return only the last N lines.

    Output streams to disk as the script executes, so polling this during a
    long run shows progress. Returns "" before the first output appears.
    """
    return storage.read_job_log(job_id, tail)


@mcp.tool()
def get_output(job_id: str) -> dict[str, Any]:
    """Get a job's final structured result.

    Returns a filtered view of the job record:
    {job_id, script, args, status, exit_code, submitted_at, started_at,
    finished_at, duration_ms, stdout, stderr, error}. ``script_path`` is
    intentionally excluded (absolute path is server-internal). Best fetched
    after job_status reports succeeded/failed; for a still-running job the
    captured fields are empty/null.
    """
    return engine.get_output(job_id)
