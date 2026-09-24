"""E2E: concurrent async jobs share the engine pool without interference.

Salvaged from the phase-1 validation packs
(.agents/tester/packs/e2e_concurrency_test.py), rewritten as a pytest
test against the current tool contract.
"""

from __future__ import annotations

import asyncio
import time

from _helpers import call, with_memory_client

TAGGED_SCRIPT = (
    "import sys, time\n"
    "tag = sys.argv[1]\n"
    "print(f'e2e-{tag}-start', flush=True)\n"
    "time.sleep(1.0)\n"
    "print(f'e2e-{tag}-end', flush=True)\n"
)

TERMINAL = {"succeeded", "failed"}


def test_parallel_async_jobs_complete_with_isolated_outputs(client):
    async def scenario():
        await call(client, "save_script", name="tagjob", content=TAGGED_SCRIPT)

        submits = [
            await call(client, "run_script_async", name="tagjob", args=[tag])
            for tag in ("a", "b", "c")
        ]
        job_ids = [s["job_id"] for s in submits]
        assert len(set(job_ids)) == 3, f"expected 3 distinct job ids: {job_ids}"

        deadline = time.monotonic() + 30.0
        statuses = {jid: None for jid in job_ids}
        while time.monotonic() < deadline and any(
            s not in TERMINAL for s in statuses.values()
        ):
            for jid in job_ids:
                if statuses[jid] not in TERMINAL:
                    statuses[jid] = (await call(client, "job_status", job_id=jid))[
                        "status"
                    ]
            if all(s in TERMINAL for s in statuses.values()):
                break
            await asyncio.sleep(0.05)

        assert list(statuses.values()) == ["succeeded"] * 3

        for tag, jid in zip("abc", job_ids):
            out = await call(client, "get_output", job_id=jid)
            assert out["exit_code"] == 0
            assert f"e2e-{tag}-start" in out["stdout"]
            assert f"e2e-{tag}-end" in out["stdout"]
            # No cross-job stdout bleed.
            for other in "abc":
                if other != tag:
                    assert f"e2e-{other}-" not in out["stdout"]

    with_memory_client(client, scenario)
