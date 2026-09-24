"""E2E pack: ASYNC CONCURRENCY (scenario 5).

Submits 3 async jobs back-to-back against the WorkForge engine
(in-process thread pool, MAX_WORKERS=4), verifies:
  - submit wall time < 3s, 3 distinct job_ids
  - all 3 jobs reach "succeeded" within 30s
  - per-job exit_code == 0 and output isolation (job-a stdout has no "p4-b")
  - OVERLAP EVIDENCE (INFO only): started_at/finished_at intervals

Run: timeout 300 uv run python .agents/tester/packs/e2e_concurrency_test.py
Output contract: final line RESULT: PASS | RESULT: FAIL (X/Y checks passed) | RESULT: TIMEOUT
"""
import os, tempfile

os.environ["WORKFORGE_HOME"] = tempfile.mkdtemp(prefix="wf-p4-")

import asyncio
import sys
import time
import traceback
from datetime import datetime

from fastmcp import Client
from workforge.server import mcp

TERMINAL = {"succeeded", "failed"}

SCRIPT_CONTENT = '''import sys, time
tag = sys.argv[1] if len(sys.argv) > 1 else "x"
print(f"p4-{tag}-start", flush=True)
time.sleep(1.0)
print(f"p4-{tag}-end", flush=True)
'''

PASSED = 0
TOTAL = 0


def check(n, name, passed, expected="ok", actual=""):
    global PASSED, TOTAL
    TOTAL += 1
    if passed:
        PASSED += 1
        print(f"[CHECK {n}] {name} ... OK", flush=True)
    else:
        print(
            f"[CHECK {n}] {name} ... FAIL (expected: {expected!r} vs actual: {actual!r})",
            flush=True,
        )
    return passed


def field(obj, key, default=None):
    """Read a key from result.data whether it is a dict or an object."""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def parse_iso(value):
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


async def call(client, tool, **kwargs):
    result = await client.call_tool(tool, kwargs)
    return result.data


async def main():
    async with Client(mcp) as client:
        # --- CHECK 1: save_script ---
        try:
            await call(
                client,
                "save_script",
                name="p4job",
                content=SCRIPT_CONTENT,
                description="P4 async concurrency probe (~1s job)",
            )
            check(1, "save_script p4job", True)
        except Exception as exc:
            check(1, "save_script p4job", False,
                  expected="save_script OK", actual=f"{type(exc).__name__}: {exc}")
            raise

        # --- CHECK 2 + 3: 3 back-to-back async submits ---
        t0 = time.monotonic()
        submits = []
        submit_error = None
        try:
            for tag in ("a", "b", "c"):
                submits.append(
                    await call(client, "run_script_async", name="p4job", args=[tag])
                )
        except Exception as exc:
            submit_error = f"{type(exc).__name__}: {exc}"
        submit_elapsed = time.monotonic() - t0
        print(f"[INFO] submit wall time for 3 back-to-back jobs: {submit_elapsed:.3f}s", flush=True)

        if submit_error is not None:
            check(2, "3 back-to-back async submits in < 3s", False,
                  expected="< 3.0s for 3 submits",
                  actual=f"submit failed at {submit_elapsed:.3f}s: {submit_error}")
            check(3, "3 distinct job_ids returned", False,
                  expected="3 distinct non-null job_ids", actual=repr(submits))
            return
        check(2, "3 back-to-back async submits in < 3s", submit_elapsed < 3.0,
              expected="< 3.0s", actual=f"{submit_elapsed:.3f}s")

        job_ids = [field(r, "job_id") for r in submits]
        initial = [field(r, "status") for r in submits]
        for tag, jid, st in zip("abc", job_ids, initial):
            print(f"[INFO] tag={tag} job_id={jid} initial_status={st}", flush=True)
        distinct = (
            len(job_ids) == 3
            and all(j is not None for j in job_ids)
            and len(set(job_ids)) == 3
        )
        check(3, "3 distinct job_ids returned", distinct,
              expected="3 distinct non-null job_ids", actual=repr(job_ids))

        # --- CHECK 4: poll all 3 until terminal (deadline 30s) ---
        statuses = {jid: None for jid in job_ids}
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            for jid in job_ids:
                if statuses[jid] in TERMINAL:
                    continue
                try:
                    meta = await call(client, "job_status", job_id=jid)
                    statuses[jid] = field(meta, "status")
                except Exception as exc:
                    statuses[jid] = f"poll-error: {type(exc).__name__}: {exc}"
            if all(s in TERMINAL for s in statuses.values()):
                break
            await asyncio.sleep(0.1)
        status_by_tag = {t: statuses[j] for t, j in zip("abc", job_ids)}
        print(f"[INFO] final statuses: {status_by_tag}", flush=True)
        final_list = [statuses[j] for j in job_ids]
        check(4, "all 3 jobs reach 'succeeded'",
              all(s == "succeeded" for s in final_list),
              expected="['succeeded', 'succeeded', 'succeeded']",
              actual=repr(final_list))

        # --- CHECK 5..7: outputs, own markers, isolation ---
        outputs = {}
        for tag, jid in zip("abc", job_ids):
            try:
                outputs[tag] = await call(client, "get_output", job_id=jid)
            except Exception as exc:
                outputs[tag] = {"_error": f"{type(exc).__name__}: {exc}"}

        for i, tag in enumerate("abc"):
            out = outputs[tag]
            if isinstance(out, dict) and "_error" in out:
                check(5 + i,
                      f"job-{tag}: exit_code==0 + own start/end in stdout"
                      + (" + no 'p4-b' in stdout" if tag == "a" else ""),
                      False,
                      expected="get_output OK, exit_code == 0, own markers in stdout",
                      actual=out["_error"])
                continue
            exit_code = field(out, "exit_code")
            stdout = field(out, "stdout")
            stdout = stdout if isinstance(stdout, str) else ("" if stdout is None else str(stdout))
            print(f"[INFO] job-{tag} stdout: {stdout!r}", flush=True)
            ok_exit = exit_code == 0
            own = (f"p4-{tag}-start" in stdout) and (f"p4-{tag}-end" in stdout)
            leak = (tag == "a" and "p4-b" in stdout)
            problems = []
            if not ok_exit:
                problems.append(f"exit_code={exit_code!r} (expected 0)")
            if not own:
                problems.append(f"stdout missing own start/end markers: {stdout!r}")
            if leak:
                problems.append(f"job-a stdout contains 'p4-b' (cross-job leak): {stdout!r}")
            check(5 + i,
                  f"job-{tag}: exit_code==0 + own start/end in stdout"
                  + (" + no 'p4-b' in stdout" if tag == "a" else ""),
                  ok_exit and own and not leak,
                  expected=f"exit_code == 0; stdout has p4-{tag}-start and p4-{tag}-end"
                           + ("; no 'p4-b' substring" if tag == "a" else ""),
                  actual="; ".join(problems) if problems else "ok")

        # --- STEP 5 (INFO, not hard-fail): overlap evidence ---
        print("[INFO] overlap analysis:", flush=True)
        starts, ends = [], []
        for tag, jid in zip("abc", job_ids):
            out = outputs[tag]
            st_raw = field(out, "started_at")
            fi_raw = field(out, "finished_at")
            dur_raw = field(out, "duration_ms")
            st, fi = parse_iso(st_raw), parse_iso(fi_raw)
            if st is not None:
                starts.append(st)
            if fi is not None:
                ends.append(fi)
            if dur_raw is not None:
                dur_desc = f"{dur_raw} ms (duration_ms)"
            elif st is not None and fi is not None:
                dur_desc = f"{(fi - st).total_seconds() * 1000:.1f} ms (computed)"
            else:
                dur_desc = "n/a"
            print(f"[INFO]   job-{tag}: started_at={st_raw!r} finished_at={fi_raw!r} duration={dur_desc}",
                  flush=True)
        if len(starts) == 3 and len(ends) == 3:
            max_start, min_end = max(starts), min(ends)
            overlap = max_start < min_end
            print(f"[INFO]   max(started_at)={max_start.isoformat()}  min(finished_at)={min_end.isoformat()}",
                  flush=True)
            print(f"[INFO]   intervals overlapped (true parallelism): {overlap}", flush=True)
        else:
            print("[INFO]   could not parse all 3 timestamp pairs (raw values above); overlap not computed",
                  flush=True)


async def guarded():
    """Internal guard layer: interrupt hung runs at 120s (outer layer = `timeout 300`)."""
    try:
        await asyncio.wait_for(main(), timeout=120)
    except (asyncio.TimeoutError, TimeoutError):
        print("RESULT: TIMEOUT", flush=True)
        return 124
    except Exception:
        print("[FATAL] unexpected exception:", flush=True)
        traceback.print_exc()
        print(f"RESULT: FAIL ({PASSED}/{TOTAL} checks passed)", flush=True)
        return 1
    if TOTAL > 0 and PASSED == TOTAL:
        print("RESULT: PASS", flush=True)
        return 0
    print(f"RESULT: FAIL ({PASSED}/{TOTAL} checks passed)", flush=True)
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(guarded()))
