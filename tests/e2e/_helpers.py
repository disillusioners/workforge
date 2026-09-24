"""Shared helpers for the e2e suite (same patterns as tests/test_smoke.py)."""

from __future__ import annotations

import asyncio
import re
import time

import pytest
from fastmcp import Client

JOB_ID_RE = re.compile(r"^[0-9a-f]{32}$")


def with_memory_client(client: Client, scenario) -> None:
    """Run an async scenario with the in-memory client connected (same loop)."""

    async def runner():
        async with client:
            await scenario()

    asyncio.run(runner())


async def call(client: Client, tool: str, **kwargs):
    """Call a tool and return its deserialized payload."""
    result = await client.call_tool(tool, kwargs)
    return result.data


async def wait_for_status(client: Client, job_id: str, wanted, deadline_s: float = 15.0):
    """Poll job_status until it reaches one of `wanted` states."""
    wanted = set(wanted)
    end = time.monotonic() + deadline_s
    last = None
    while time.monotonic() < end:
        last = await call(client, "job_status", job_id=job_id)
        if last["status"] in wanted:
            return last
        await asyncio.sleep(0.05)
    pytest.fail(f"job {job_id} never reached {wanted}; last status: {last}")
