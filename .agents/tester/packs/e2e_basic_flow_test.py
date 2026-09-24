"""E2E pack: e2e_basic_flow_test — SYNC run, FAILURE propagation, EDGE cases.

Covers task scenarios: 1 (SYNC), 3 (FAILURE), 6 (EDGE tail + unknown job_id).
Isolation: unique WORKFORGE_HOME per run (mkdtemp) — parallel-safe, never
touches ~/.workforge. WORKFORGE_HOME is set BEFORE workforge.server import.

Dual-layer timeout (both layers required):
  - Layer 1 (outer guard): caller runs `timeout 300 uv run python <this>`.
  - Layer 2 (inner guard): asyncio.wait_for(main(), timeout=240) below.

Output contract (last line):
  RESULT: PASS                      -> exit 0
  RESULT: FAIL (X/Y checks passed)  -> exit 1
  RESULT: TIMEOUT                   -> exit 124
"""

# --- FIRST LINES: isolate WORKFORGE_HOME before any workforge import --------
import os
import tempfile

os.environ["WORKFORGE_HOME"] = tempfile.mkdtemp(prefix="wf-p1-")
# -----------------------------------------------------------------------------

import asyncio
import sys

from fastmcp import Client
from fastmcp.exceptions import ToolError

from workforge.server import mcp

INNER_TIMEOUT_S = 240

CHECKS_TOTAL = 0
CHECKS_PASSED = 0


def record(n: int, name: str, ok: bool, detail: str = "") -> bool:
    """Record one check. Prints `[CHECK n] <name> ... OK` or `... FAIL (...)`."""
    global CHECKS_TOTAL, CHECKS_PASSED
    CHECKS_TOTAL += 1
    if ok:
        CHECKS_PASSED += 1
        print(f"[CHECK {n}] {name} ... OK", flush=True)
    else:
        print(f"[CHECK {n}] {name} ... FAIL ({detail})", flush=True)
    return ok


def g(rec, key):
    """Field access on a `.data` record (verified pattern: dict)."""
    return rec.get(key) if isinstance(rec, dict) else None


# --- Script payloads ---------------------------------------------------------
SYNC_SCRIPT = (
    "print('p1-line-1', flush=True)\n"
    "print('p1-line-2', flush=True)\n"
    "print('p1-line-3', flush=True)\n"
)

FAIL_SCRIPT = (
    "import sys\n"
    "print('p1-stdout-before-fail', flush=True)\n"
    "print('p1-stderr-boom', file=sys.stderr, flush=True)\n"
    "sys.exit(3)\n"
)

LINES_SCRIPT = "".join(f"print('p1-edge-{i}', flush=True)\n" for i in range(1, 6))

BOGUS_ID = "deadbeefdeadbeefdeadbeefdeadbeef"  # 32 hex: valid format, nonexistent
MALFORMED_ID = "not-a-job-id"


async def scenario_sync(client: Client) -> None:
    """Scenario 1: save -> list -> sync run of a 3-line script."""
    # CHECK 1: save_script returns {name, size>0, updated_at present}
    try:
        saved = (await client.call_tool(
            "save_script",
            {"name": "p1hello", "content": SYNC_SCRIPT,
             "description": "p1 basic sync hello"},
        )).data
        ok = (g(saved, "name") == "p1hello"
              and isinstance(g(saved, "size"), int) and g(saved, "size") > 0
              and bool(g(saved, "updated_at")))
        record(1, "save_script(p1hello) -> name/size>0/updated_at", ok,
               f"expected name='p1hello', size int>0, updated_at truthy | "
               f"actual {saved!r}")
    except Exception as exc:  # noqa: BLE001
        record(1, "save_script(p1hello) -> name/size>0/updated_at", False,
               f"unexpected exception {exc!r}")

    # CHECK 2: list_scripts contains an entry named p1hello
    try:
        scripts = (await client.call_tool("list_scripts", {})).data
        names = [g(s, "name") for s in scripts] if isinstance(scripts, list) else []
        record(2, "list_scripts contains 'p1hello'", "p1hello" in names,
               f"expected 'p1hello' in names | actual names={names!r} data={scripts!r}")
    except Exception as exc:  # noqa: BLE001
        record(2, "list_scripts contains 'p1hello'", False,
               f"unexpected exception {exc!r}")

    # CHECK 3: run_script -> status succeeded, exit_code 0
    try:
        run = (await client.call_tool("run_script", {"name": "p1hello"})).data
        record(3, "run_script(p1hello) -> status='succeeded', exit_code==0",
               g(run, "status") == "succeeded" and g(run, "exit_code") == 0,
               f"expected status='succeeded' exit_code=0 | actual status="
               f"{g(run, 'status')!r} exit_code={g(run, 'exit_code')!r} "
               f"error={g(run, 'error')!r}")
    except Exception as exc:  # noqa: BLE001
        record(3, "run_script(p1hello) -> status='succeeded', exit_code==0", False,
               f"unexpected exception {exc!r}")
        return

    # CHECK 4: stdout has all 3 lines in order; duration_ms present int >= 0
    stdout = g(run, "stdout") or ""
    lines = [ln for ln in stdout.splitlines() if ln.startswith("p1-line-")]
    dur = g(run, "duration_ms")
    ok = lines == ["p1-line-1", "p1-line-2", "p1-line-3"] and isinstance(dur, int) and dur >= 0
    record(4, "run stdout: 3 lines in order; duration_ms int>=0", ok,
           f"expected ['p1-line-1','p1-line-2','p1-line-3'] and duration_ms int>=0 | "
           f"actual stdout={stdout!r} duration_ms={dur!r}")


async def scenario_failure(client: Client) -> None:
    """Scenario 3: non-zero exit -> status failed, exit_code + streams captured."""
    try:
        await client.call_tool(
            "save_script",
            {"name": "p1fail", "content": FAIL_SCRIPT,
             "description": "p1 basic failure propagation"},
        )
    except Exception as exc:  # noqa: BLE001
        for n, nm in ((5, "run_script(p1fail) -> status='failed', exit_code==3"),
                      (6, "p1fail streams: stderr boom, stdout pre-fail, duration_ms"),
                      (7, "job_status(p1fail job) -> status='failed', exit_code==3")):
            record(n, nm, False, f"save_script(p1fail) raised {exc!r}")
        return

    # CHECK 5: run -> status failed, exit_code 3
    try:
        run = (await client.call_tool("run_script", {"name": "p1fail"})).data
        record(5, "run_script(p1fail) -> status='failed', exit_code==3",
               g(run, "status") == "failed" and g(run, "exit_code") == 3,
               f"expected status='failed' exit_code=3 | actual status="
               f"{g(run, 'status')!r} exit_code={g(run, 'exit_code')!r} "
               f"error={g(run, 'error')!r}")
    except Exception as exc:  # noqa: BLE001
        record(5, "run_script(p1fail) -> status='failed', exit_code==3", False,
               f"unexpected exception {exc!r}")
        return

    # CHECK 6: stderr has boom marker, stdout has pre-fail marker, duration_ms present
    dur = g(run, "duration_ms")
    ok = ("p1-stderr-boom" in (g(run, "stderr") or "")
          and "p1-stdout-before-fail" in (g(run, "stdout") or "")
          and isinstance(dur, int))
    record(6, "p1fail streams: stderr boom, stdout pre-fail, duration_ms present", ok,
           f"expected 'p1-stderr-boom' in stderr, 'p1-stdout-before-fail' in stdout, "
           f"duration_ms int | actual stdout={g(run, 'stdout')!r} "
           f"stderr={g(run, 'stderr')!r} duration_ms={dur!r}")

    # CHECK 7: job_status(job_id) -> status failed AND exit_code 3
    try:
        meta = (await client.call_tool("job_status", {"job_id": g(run, "job_id")})).data
        record(7, "job_status(p1fail job) -> status='failed', exit_code==3",
               g(meta, "status") == "failed" and g(meta, "exit_code") == 3,
               f"expected status='failed' exit_code=3 | actual status="
               f"{g(meta, 'status')!r} exit_code={g(meta, 'exit_code')!r}")
    except Exception as exc:  # noqa: BLE001
        record(7, "job_status(p1fail job) -> status='failed', exit_code==3", False,
               f"unexpected exception {exc!r}")


async def scenario_edge_tail(client: Client) -> None:
    """Scenario 6a: get_log tail semantics (\\n join, no trailing newline)."""
    try:
        await client.call_tool(
            "save_script",
            {"name": "p1lines", "content": LINES_SCRIPT,
             "description": "p1 edge tail lines"},
        )
        run = (await client.call_tool("run_script", {"name": "p1lines"})).data
    except Exception as exc:  # noqa: BLE001
        record(8, "get_log(tail=2) == 'p1-edge-4\\np1-edge-5' exact", False,
               f"unexpected exception {exc!r}")
        record(9, "get_log(tail=5) contains all 5 lines", False,
               f"unexpected exception {exc!r}")
        return

    if g(run, "status") != "succeeded":
        detail = (f"precondition failed: run status={g(run, 'status')!r} "
                  f"exit_code={g(run, 'exit_code')!r} stderr={g(run, 'stderr')!r}")
        record(8, "get_log(tail=2) == 'p1-edge-4\\np1-edge-5' exact", False, detail)
        record(9, "get_log(tail=5) contains all 5 lines", False, detail)
        return
    job_id = g(run, "job_id")

    # CHECK 8: tail=2 is exactly the last two lines, \n-joined, no trailing newline
    try:
        tail2 = (await client.call_tool(
            "get_log", {"job_id": job_id, "tail": 2})).data
        expected = "p1-edge-4\np1-edge-5"
        record(8, "get_log(tail=2) == 'p1-edge-4\\np1-edge-5' exact",
               tail2 == expected, f"expected {expected!r} | actual {tail2!r}")
    except Exception as exc:  # noqa: BLE001
        record(8, "get_log(tail=2) == 'p1-edge-4\\np1-edge-5' exact", False,
               f"unexpected exception {exc!r}")

    # CHECK 9: tail=5 contains all five lines
    try:
        tail5 = (await client.call_tool(
            "get_log", {"job_id": job_id, "tail": 5})).data or ""
        missing = [f"p1-edge-{i}" for i in range(1, 6) if f"p1-edge-{i}" not in tail5]
        record(9, "get_log(tail=5) contains all 5 lines", not missing,
               f"expected all 5 lines present | actual={tail5!r} missing={missing!r}")
    except Exception as exc:  # noqa: BLE001
        record(9, "get_log(tail=5) contains all 5 lines", False,
               f"unexpected exception {exc!r}")


async def expect_tool_error(client: Client, n: int, name: str, tool: str,
                            **kwargs) -> None:
    """One check: calling `tool` with kwargs must raise fastmcp ToolError.

    Prints the captured error message VERBATIM on its own line (log evidence).
    """
    try:
        await client.call_tool(tool, kwargs)
        record(n, name, False,
               f"expected ToolError | actual: no exception raised "
               f"(tool={tool!r} kwargs={kwargs!r})")
    except ToolError as exc:
        call_repr = ", ".join(f"{k}={v!r}" for k, v in kwargs.items())
        print(f"    [captured {tool}({call_repr})] ToolError: {exc}", flush=True)
        record(n, name, True)
    except Exception as exc:  # noqa: BLE001
        record(n, name, False,
               f"expected fastmcp ToolError | actual different exception "
               f"{type(exc).__module__}.{type(exc).__name__}: {exc!r}")


async def scenario_edge_unknown_id(client: Client) -> None:
    """Scenario 6b: unknown (valid-format) and malformed job ids -> ToolError."""
    await expect_tool_error(client, 10, "job_status(bogus 32-hex) raises ToolError",
                            "job_status", job_id=BOGUS_ID)
    await expect_tool_error(client, 11, "get_log(bogus 32-hex) raises ToolError",
                            "get_log", job_id=BOGUS_ID)
    await expect_tool_error(client, 12, "get_output(bogus 32-hex) raises ToolError",
                            "get_output", job_id=BOGUS_ID)
    await expect_tool_error(client, 13, "job_status('not-a-job-id') raises ToolError",
                            "job_status", job_id=MALFORMED_ID)

    # CHECK 14: client survived the error path — a normal call still works
    try:
        scripts = (await client.call_tool("list_scripts", {})).data
        names = sorted(g(s, "name") for s in scripts) if isinstance(scripts, list) else []
        record(14, "client alive after errors: list_scripts() succeeds",
               {"p1hello", "p1fail", "p1lines"} <= set(names),
               f"expected names incl. p1hello/p1fail/p1lines | actual names={names!r}")
    except Exception as exc:  # noqa: BLE001
        record(14, "client alive after errors: list_scripts() succeeds", False,
               f"unexpected exception {exc!r}")


async def main() -> None:
    async with Client(mcp) as client:
        await scenario_sync(client)
        await scenario_failure(client)
        await scenario_edge_tail(client)
        await scenario_edge_unknown_id(client)


def run() -> int:
    try:
        asyncio.run(asyncio.wait_for(main(), timeout=INNER_TIMEOUT_S))
    except asyncio.TimeoutError:
        print("RESULT: TIMEOUT", flush=True)
        return 124
    except Exception as exc:  # noqa: BLE001 — pack bug safety net; honest counts
        print(f"[PACK-ERROR] unexpected exception after "
              f"{CHECKS_PASSED}/{CHECKS_TOTAL} checks: {exc!r}", flush=True)
    if CHECKS_TOTAL > 0 and CHECKS_PASSED == CHECKS_TOTAL:
        print("RESULT: PASS", flush=True)
        return 0
    print(f"RESULT: FAIL ({CHECKS_PASSED}/{CHECKS_TOTAL} checks passed)", flush=True)
    return 1


if __name__ == "__main__":
    sys.exit(run())
