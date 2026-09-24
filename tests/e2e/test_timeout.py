"""E2E: timeout kill semantics on the synchronous run path.

Salvaged from the phase-1 validation packs
(.agents/tester/packs/e2e_timeout_test.py), rewritten as pytest tests
against the current tool contract. The async timeout path and the
timeout_seconds < 1 rejection are already pinned in tests/test_smoke.py.
"""

from __future__ import annotations

import time

import pytest
from fastmcp.exceptions import ToolError

from _helpers import call, with_memory_client

HANG_SCRIPT = "import time\ntime.sleep(60)\nprint('never-reached', flush=True)\n"


def test_sync_timeout_kills_script_and_reports_failure(client):
    async def scenario():
        await call(client, "save_script", name="hang60", content=HANG_SCRIPT)

        t0 = time.monotonic()
        run = await call(client, "run_script", name="hang60", timeout_seconds=2)
        elapsed = time.monotonic() - t0

        # Killed at the 2 s timeout — not immediately, and not after 60 s
        # (tolerances, not exact timings).
        assert elapsed >= 2.0
        assert elapsed < 30.0
        assert run["status"] == "failed"
        assert run["exit_code"] is None
        assert "timed out" in (run["error"] or "").lower()

        status = await call(client, "job_status", job_id=run["job_id"])
        assert status["status"] == "failed"
        assert status["exit_code"] is None

        # The post-timeout line must never appear in the captured log.
        log = await call(client, "get_log", job_id=run["job_id"])
        assert "never-reached" not in log

    with_memory_client(client, scenario)


def test_negative_timeout_rejected_and_client_survives(client):
    async def scenario():
        await call(client, "save_script", name="hang60b", content=HANG_SCRIPT)
        with pytest.raises(ToolError):
            await call(client, "run_script", name="hang60b", timeout_seconds=-1)

        # The client survives the rejected call.
        names = [s["name"] for s in await call(client, "list_scripts")]
        assert "hang60b" in names

    with_memory_client(client, scenario)
