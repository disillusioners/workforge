"""E2E pack: workforge async job lifecycle.

Scenario covered: an async-submitted job must
- submit fast (<1.0 s) and return a valid 32-hex job_id with status queued|running
- transition through queued/running before reaching a terminal status (succeeded)
- stream partial stdout to get_log WHILE running (mid-run sample, <10 lines, no
  p2-line-9 yet, >=1 line)
- complete successfully with exit_code=0 and full stdout (10 lines)
- expose a get_output payload with non-negative duration_ms and ISO-parseable
  started_at/finished_at where finished_at >= started_at
- report status=="succeeded"

Output contract: prints one [CHECK N] line per check, then a final
`RESULT: PASS|FAIL (X/Y checks passed)|TIMEOUT` and exits 0/1/124.

Dual-layer timeout:
  - outer: `timeout 300 uv run python e2e_async_lifecycle_test.py`
  - inner: asyncio.wait_for(main(), timeout=240)  -> TimeoutError -> exit 124

Isolation: a unique WORKFORGE_HOME tempdir is allocated BEFORE importing
workforge.server so this pack can run in parallel with the other 5 sibling
packs without touching ~/.workforge.
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
import tempfile
import time
import traceback
from datetime import datetime

# MUST stay first: isolate WORKFORGE_HOME from sibling packs + real install.
os.environ["WORKFORGE_HOME"] = tempfile.mkdtemp(prefix="wf-p2-")

from fastmcp import Client  # noqa: E402  (imports after env var)

from workforge.server import mcp  # noqa: E402  (imports after env var)


# ---------------------------------------------------------------- helpers


JOB_ID_RE = re.compile(r"^[0-9a-f]{32}$")


async def call(client: Client, tool: str, **kwargs):
    """Call a fastmcp tool and return its deserialized payload."""
    result = await client.call_tool(tool, kwargs)
    return result.data


def p2_lines(text: str) -> list[str]:
    """Return only the p2-line-* lines from a log blob, preserving order."""
    return [ln for ln in text.splitlines() if ln.startswith("p2-line-")]


# ---------------------------------------------------------------- main


async def main() -> int:
    SCRIPT = (
        "import time\n"
        "for i in range(10):\n"
        "    print(f'p2-line-{i}', flush=True)\n"
        "    time.sleep(0.3)\n"
    )

    checks: list[tuple[str, bool, str]] = []

    def record(name: str, ok: bool, detail: str = "") -> None:
        tag = "OK" if ok else "FAIL"
        idx = len(checks) + 1
        print(f"[CHECK {idx}] {name} ... {tag}", flush=True)
        if detail:
            for line in detail.splitlines():
                print(f"    {line}", flush=True)
        checks.append((name, ok, detail))

    observed: list[str] = []
    midrun_samples: list[tuple[str, int]] = []  # (status_at_sample, p2_line_count)
    best: tuple[int, str] | None = None
    final_status: str | None = None

    t_run_start = time.monotonic()
    async with Client(mcp) as client:
        # --- CHECK 1 -------------------------------------------------------
        save = await call(
            client,
            "save_script",
            name="p2long",
            content=SCRIPT,
            description="async lifecycle E2E script (~3 s runtime)",
        )
        c1_ok = (
            save.get("name") == "p2long"
            and isinstance(save.get("size"), int)
            and save.get("size") == len(SCRIPT.encode())
            and bool(save.get("updated_at"))
        )
        record(
            "save_script saves p2long with correct metadata",
            c1_ok,
            f"actual={save!r}",
        )

        # --- CHECK 2 -------------------------------------------------------
        t0 = time.monotonic()
        r = await call(client, "run_script_async", name="p2long")
        t1 = time.monotonic()
        submit_dt = t1 - t0

        job_id = r.get("job_id")
        status_at_submit = r.get("status")

        c2_ok_id = isinstance(job_id, str) and JOB_ID_RE.match(job_id) is not None
        c2_ok_status = status_at_submit in {"queued", "running"}
        c2_ok_fast = submit_dt < 1.0
        c2_ok = c2_ok_id and c2_ok_status and c2_ok_fast
        record(
            "run_script_async returns fast with valid 32-hex job_id and queued|running",
            c2_ok,
            (
                f"submit_dt={submit_dt:.4f}s (<1.0)\n"
                f"job_id={job_id!r} (regex match: {c2_ok_id})\n"
                f"status_at_submit={status_at_submit!r} (in queued|running: {c2_ok_status})"
            ),
        )

        # --- CHECK 3 + 4 (poll loop does both) -----------------------------
        poll_deadline = time.monotonic() + 30.0
        while time.monotonic() < poll_deadline:
            meta = await call(client, "job_status", job_id=job_id)
            status_now = meta.get("status")
            if not observed or observed[-1] != status_now:
                observed.append(status_now)

            if status_now == "running":
                log_text = await call(client, "get_log", job_id=job_id)
                cnt = len(p2_lines(log_text))
                midrun_samples.append((status_now, cnt))
                if "p2-line-9" not in log_text and 1 <= cnt < 10:
                    if best is None or cnt > best[0]:
                        best = (cnt, log_text)

            if status_now in {"succeeded", "failed"}:
                final_status = status_now
                break
            await asyncio.sleep(0.1)

        # --- CHECK 3 -------------------------------------------------------
        non_terminal_seen = any(s in {"queued", "running"} for s in observed)
        c3_ok = final_status in {"succeeded", "failed"} and non_terminal_seen
        record(
            "polling observed a non-terminal status (queued|running) before terminal",
            c3_ok,
            f"observed_sequence={observed!r} final={final_status!r} non_terminal_seen={non_terminal_seen}",
        )

        # --- CHECK 4 -------------------------------------------------------
        if best is None:
            c4_ok = False
            detail4 = (
                f"no qualifying mid-run sample found\n"
                f"midrun_samples (status, line_count)={midrun_samples!r}\n"
                f"observed_sequence={observed!r}"
            )
        else:
            sample_count, sample_log = best
            lines = p2_lines(sample_log)
            c4_ok = (
                1 <= sample_count < 10
                and len(lines) == sample_count
                and "p2-line-9" not in sample_log
            )
            detail4 = (
                f"line_count={sample_count} (expected 1..9)\n"
                f"contains_p2_line_9={'p2-line-9' in sample_log}\n"
                f"raw_sample={sample_log!r}"
            )
        record(
            "mid-run partial get_log captured (<10 p2-lines, no p2-line-9, >=1)",
            c4_ok,
            detail4,
        )

        # --- CHECK 5 -------------------------------------------------------
        if final_status is None:
            # one final poll in case we broke out only by timeout boundary
            meta = await call(client, "job_status", job_id=job_id)
            final_status = meta.get("status")
        c5_ok = final_status == "succeeded"
        record(
            "final job_status reached 'succeeded' within 30 s deadline",
            c5_ok,
            f"final_status={final_status!r}",
        )

        # --- CHECK 6 -------------------------------------------------------
        full_log = await call(client, "get_log", job_id=job_id)
        expected_log = "".join(f"p2-line-{i}\n" for i in range(10))
        c6_ok = full_log == expected_log
        record(
            "get_log returns exactly 10 lines 'p2-line-0' .. 'p2-line-9' (strict equality)",
            c6_ok,
            f"actual={full_log!r}\nexpected={expected_log!r}",
        )

        # --- CHECK 7 -------------------------------------------------------
        out = await call(client, "get_output", job_id=job_id)

        exit_code = out.get("exit_code")
        duration_ms = out.get("duration_ms")
        started_at = out.get("started_at")
        finished_at = out.get("finished_at")
        status_out = out.get("status")
        stderr = out.get("stderr")
        stdout = out.get("stdout", "")

        try:
            t_started = datetime.fromisoformat(started_at) if started_at else None
            t_finished = datetime.fromisoformat(finished_at) if finished_at else None
            ts_parse_err: Exception | None = None
        except (TypeError, ValueError) as exc:
            t_started = t_finished = None
            ts_parse_err = exc

        c7_exit = exit_code == 0
        c7_dur = isinstance(duration_ms, int) and duration_ms >= 2000
        c7_ts_present = bool(started_at) and bool(finished_at)
        c7_ts_parse = t_started is not None and t_finished is not None and ts_parse_err is None
        c7_ts_order = c7_ts_parse and t_finished >= t_started
        c7_status = status_out == "succeeded"
        c7_stdout_complete = all(f"p2-line-{i}\n" in stdout for i in range(10))

        c7_ok = (
            c7_exit
            and c7_dur
            and c7_ts_present
            and c7_ts_parse
            and c7_ts_order
            and c7_status
            and c7_stdout_complete
        )
        record(
            "get_output: exit_code=0, duration_ms>=2000, ISO-parseable started_at/finished_at with finished_at>=started_at, status=succeeded, all 10 lines in stdout",
            c7_ok,
            (
                f"exit_code={exit_code!r} (want 0)\n"
                f"duration_ms={duration_ms!r} (want int>=2000)\n"
                f"started_at={started_at!r}\n"
                f"finished_at={finished_at!r}\n"
                f"parsed_started={t_started!r}\n"
                f"parsed_finished={t_finished!r}\n"
                f"ts_parse_err={ts_parse_err!r}\n"
                f"finished >= started: {c7_ts_order}\n"
                f"status={status_out!r} (want 'succeeded')\n"
                f"stderr={stderr!r}\n"
                f"all_10_lines_in_stdout={c7_stdout_complete}"
            ),
        )

    t_run_end = time.monotonic()

    # ---- summary ----
    passed = sum(1 for _, ok, _ in checks if ok)
    total = len(checks)

    print()
    print("=== Test Pack: e2e_async_lifecycle_test ===")
    print(f"wall_clock_runtime_s={t_run_end - t_run_start:.3f}")
    print(f"observed_status_sequence={observed!r}")
    if best is not None:
        sample_count, sample_log = best
        print(
            "best_midrun_sample "
            f"line_count={sample_count} (p2-lines, no p2-line-9) "
            f"content={sample_log!r}"
        )
    else:
        print("best_midrun_sample=None")
    print(f"checks_passed={passed}/{total}")

    if passed == total:
        print("RESULT: PASS")
        return 0

    failed_names = [name for name, ok, _ in checks if not ok]
    print("failed_checks:")
    for name in failed_names:
        print(f"  - {name}")
    print(f"RESULT: FAIL ({passed}/{total} checks passed)")
    return 1


if __name__ == "__main__":
    try:
        rc = asyncio.run(asyncio.wait_for(main(), timeout=240))
    except asyncio.TimeoutError:
        print("RESULT: TIMEOUT", flush=True)
        sys.exit(124)
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 — surface any unhandled crash
        print(f"UNHANDLED EXCEPTION: {type(exc).__name__}: {exc!r}", flush=True)
        traceback.print_exc()
        print("RESULT: FAIL (unhandled exception)", flush=True)
        sys.exit(1)
    sys.exit(rc)
