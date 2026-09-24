"""E2E: async job lifecycle — submit, stream logs while running, finalize.

Salvaged from the phase-1 validation packs
(.agents/tester/packs/e2e_async_lifecycle_test.py), rewritten as a pytest
test against the current tool contract.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime

from _helpers import JOB_ID_RE, call, with_memory_client

# ~3 s total runtime: enough mid-run window to sample the streaming log
# reliably, short enough to keep the suite fast.
LIFECYCLE_SCRIPT = (
    "import time\n"
    "for i in range(10):\n"
    "    print(f'e2e-line-{i}', flush=True)\n"
    "    time.sleep(0.3)\n"
)


def test_async_lifecycle_streams_partial_logs_then_finalizes(client):
    async def scenario():
        saved = await call(
            client, "save_script", name="lifecycle", content=LIFECYCLE_SCRIPT
        )
        assert saved["name"] == "lifecycle"

        started = await call(client, "run_script_async", name="lifecycle")
        job_id = started["job_id"]
        assert JOB_ID_RE.match(job_id)
        assert started["status"] in {"queued", "running"}

        # Poll to a terminal state, keeping the best mid-run log sample.
        deadline = time.monotonic() + 30.0
        saw_non_terminal = False
        best_partial = None
        final = None
        while time.monotonic() < deadline:
            status = await call(client, "job_status", job_id=job_id)
            state = status["status"]
            saw_non_terminal = saw_non_terminal or state in {"queued", "running"}
            if state == "running":
                log = await call(client, "get_log", job_id=job_id)
                lines = [ln for ln in log.splitlines() if ln.startswith("e2e-line-")]
                # The final line only appears once the job is done; a sample
                # without it is guaranteed to be a true mid-run capture.
                if lines and "e2e-line-9" not in log:
                    best_partial = lines
            if state in {"succeeded", "failed"}:
                final = state
                break
            await asyncio.sleep(0.05)

        assert saw_non_terminal, "never observed queued/running before terminal"
        assert final == "succeeded"

        # get_log streamed a partial 1..9 line prefix while the job ran —
        # live streaming, not just the final buffer.
        assert best_partial, "no mid-run log sample captured while running"
        assert 1 <= len(best_partial) <= 9
        assert best_partial == [f"e2e-line-{i}" for i in range(len(best_partial))]

        full_log = await call(client, "get_log", job_id=job_id)
        assert full_log == "".join(f"e2e-line-{i}\n" for i in range(10))

        out = await call(client, "get_output", job_id=job_id)
        assert out["status"] == "succeeded"
        assert out["exit_code"] == 0
        # The script sleeps ~3 s; a shorter duration would mean the engine
        # reported success before the script finished. (Tolerance, not a
        # timing assertion.)
        assert out["duration_ms"] >= 2000
        started_at = datetime.fromisoformat(out["started_at"])
        finished_at = datetime.fromisoformat(out["finished_at"])
        assert finished_at >= started_at
        assert all(f"e2e-line-{i}\n" in out["stdout"] for i in range(10))

    with_memory_client(client, scenario)
