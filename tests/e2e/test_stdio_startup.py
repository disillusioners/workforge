"""E2E: the documented quickstart entrypoint speaks MCP over stdio.

Salvaged from the phase-1 validation packs
(.agents/tester/packs/e2e_startup_stdio_test.py): only the README
quickstart check — `uv run python -m workforge` answering a raw MCP
initialize handshake — is kept. The StdioTransport round-trip is already
covered by tests/test_smoke.py::test_stdio_server_startup.

This is the one test in the suite that spawns a real server process; it
is bounded by an explicit timeout and always reaps the child.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
HANDSHAKE_TIMEOUT_S = 30.0

INITIALIZE_REQUEST = (
    json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "workforge-e2e", "version": "0.0.0"},
            },
        }
    )
    + "\n"
)


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv not on PATH")
def test_readme_quickstart_uv_run_module_answers_mcp_initialize(wf_home):
    async def scenario():
        proc = await asyncio.create_subprocess_exec(
            "uv", "run", "python", "-m", "workforge",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            cwd=str(REPO_ROOT),
            env={**os.environ, "WORKFORGE_HOME": str(wf_home)},
        )
        try:
            proc.stdin.write(INITIALIZE_REQUEST.encode())
            await proc.stdin.drain()
            raw = await asyncio.wait_for(proc.stdout.readline(), timeout=HANDSHAKE_TIMEOUT_S)
        finally:
            # Never leave the spawned server behind.
            if proc.returncode is None:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5)
                except asyncio.TimeoutError:
                    proc.kill()
                    await proc.wait()

        reply = json.loads(raw.decode())
        assert reply["jsonrpc"] == "2.0"
        assert reply["id"] == 1
        assert reply["result"]["serverInfo"]["name"] == "WorkForge"

    asyncio.run(asyncio.wait_for(scenario(), timeout=HANDSHAKE_TIMEOUT_S + 15))
