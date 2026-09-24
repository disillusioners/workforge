"""E2E: the basic agent workflow — save, run synchronously, inspect results.

Covers the happy path end to end, failure propagation, get_log tail
semantics, and error handling for unknown/malformed job ids.

Salvaged from the phase-1 validation packs
(.agents/tester/packs/e2e_basic_flow_test.py), rewritten as pytest tests
against the current tool contract.
"""

from __future__ import annotations

import pytest
from fastmcp.exceptions import ToolError

from _helpers import call, with_memory_client

THREE_LINES_SCRIPT = "".join(f"print('e2e-line-{i}', flush=True)\n" for i in range(1, 4))
FAILING_SCRIPT = (
    "import sys\n"
    "print('e2e-stdout-before-fail', flush=True)\n"
    "print('e2e-stderr-boom', file=sys.stderr, flush=True)\n"
    "sys.exit(3)\n"
)
FIVE_LINES_SCRIPT = "".join(f"print('e2e-edge-{i}', flush=True)\n" for i in range(1, 6))
UNKNOWN_JOB_ID = "deadbeefdeadbeefdeadbeefdeadbeef"  # valid format, nonexistent


def test_sync_roundtrip_save_run_and_status(client):
    async def scenario():
        saved = await call(
            client, "save_script", name="hello3", content=THREE_LINES_SCRIPT
        )
        assert saved["name"] == "hello3"
        assert isinstance(saved["size"], int) and saved["size"] > 0
        assert saved["updated_at"]

        names = [s["name"] for s in await call(client, "list_scripts")]
        assert "hello3" in names

        run = await call(client, "run_script", name="hello3")
        assert run["status"] == "succeeded"
        assert run["exit_code"] == 0
        assert run["stdout"] == "e2e-line-1\ne2e-line-2\ne2e-line-3\n"
        assert isinstance(run["duration_ms"], int) and run["duration_ms"] >= 0

        status = await call(client, "job_status", job_id=run["job_id"])
        assert status["status"] == "succeeded"
        assert status["exit_code"] == 0

    with_memory_client(client, scenario)


def test_failure_propagates_exit_code_streams_and_status(client):
    async def scenario():
        await call(client, "save_script", name="fail3", content=FAILING_SCRIPT)
        run = await call(client, "run_script", name="fail3")
        assert run["status"] == "failed"
        assert run["exit_code"] == 3
        assert "e2e-stderr-boom" in run["stderr"]
        assert "e2e-stdout-before-fail" in run["stdout"]
        assert run["error"] and "3" in run["error"]

        status = await call(client, "job_status", job_id=run["job_id"])
        assert status["status"] == "failed"
        assert status["exit_code"] == 3

    with_memory_client(client, scenario)


def test_get_log_tail_semantics(client):
    async def scenario():
        await call(client, "save_script", name="lines5", content=FIVE_LINES_SCRIPT)
        run = await call(client, "run_script", name="lines5")
        assert run["status"] == "succeeded"
        job_id = run["job_id"]

        # tail=N is the last N lines, "\n"-joined, no trailing newline.
        tail2 = await call(client, "get_log", job_id=job_id, tail=2)
        assert tail2 == "e2e-edge-4\ne2e-edge-5"

        tail5 = await call(client, "get_log", job_id=job_id, tail=5)
        for i in range(1, 6):
            assert f"e2e-edge-{i}" in tail5

    with_memory_client(client, scenario)


def test_unknown_or_malformed_job_id_raises_tool_error(client):
    async def scenario():
        for tool in ("job_status", "get_log", "get_output"):
            with pytest.raises(ToolError):
                await call(client, tool, job_id=UNKNOWN_JOB_ID)
        with pytest.raises(ToolError):
            await call(client, "job_status", job_id="not-a-job-id")

        # The client survives the error paths.
        names = [s["name"] for s in await call(client, "list_scripts")]
        assert isinstance(names, list)

    with_memory_client(client, scenario)
