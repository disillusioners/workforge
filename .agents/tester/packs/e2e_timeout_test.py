"""E2E pack: scenario 4 — TIMEOUT kill semantics.

Covers WorkForge scenario 4 from the test matrix: a synchronous run_script
that exceeds its timeout must be killed promptly, the job record must
report a timeout failure, the captured log must show no late output,
and a negative timeout must surface as a ToolError while the client
remains usable.

Boilerplate from the task brief: set WORKFORGE_HOME to an isolated
tempdir BEFORE importing workforge.server, so concurrent sibling packs
don't trample each other's state.

Dual-layer timeout:
  - outer (caller): `timeout 300 uv run python ...` (caller enforces)
  - inner (this script): asyncio.wait_for(main(), timeout=120) -> exit 124
"""

from __future__ import annotations

import os
import sys
import tempfile
import time

# MUST happen before any workforge import — storage.home() reads this env.
os.environ["WORKFORGE_HOME"] = tempfile.mkdtemp(prefix="wf-p3-")

import asyncio  # noqa: E402  (after env mutation, intentional)

from fastmcp import Client  # noqa: E402
from fastmcp.exceptions import ToolError  # noqa: E402
from workforge.server import mcp  # noqa: E402


HANG_SCRIPT = (
    "import time\n"
    "time.sleep(60)\n"
    'print("never-reached", flush=True)\n'
)


class CheckLog:
    """Accumulates check results and the overall verdict."""

    def __init__(self) -> None:
        self.results: list[tuple[int, str, bool, str]] = []
        self.passed = 0
        self.total = 0

    def record(self, n: int, ok: bool, detail: str = "") -> None:
        self.total += 1
        if ok:
            self.passed += 1
        self.results.append((n, detail, ok, ""))

    def fail(self, n: int, expected: str, actual: object) -> None:
        """Record a check failure with expected vs actual VERBATIM."""
        self.total += 1
        # expected/actual printed exactly as received — no repr() rewrite
        msg = f"FAIL (expected {expected!r} vs actual {actual!r})"
        # No further mutation: keep repr() so the report can quote it raw
        self.results.append((n, "", False, msg))

    def line(self, n: int, name: str, ok: bool, extra: str = "") -> None:
        tag = "OK" if ok else "FAIL"
        suffix = f" — {extra}" if extra else ""
        print(f"[CHECK {n}] {name} ... {tag}{suffix}", flush=True)


def _verbatim(value: object) -> str:
    """Stable string form for VERBATIM reporting (None, strings, anything)."""
    if value is None:
        return "<None>"
    return str(value)


async def main() -> int:
    log = CheckLog()

    async with Client(mcp) as client:

        # ---- CHECK 1: save_script -------------------------------------------------
        n = 1
        try:
            saved = await client.call_tool(
                "save_script",
                {"name": "p3hang", "content": HANG_SCRIPT, "description": "timeout kill e2e"},
            )
            saved_data = saved.data
            ok = isinstance(saved_data, dict) and saved_data.get("name") == "p3hang"
            extra = _verbatim(saved_data) if ok else f"got {saved_data!r}"
        except Exception as exc:
            ok = False
            extra = f"exception {type(exc).__name__}: {exc}"
        log.record(n, ok, extra)
        log.line(n, "save_script('p3hang', hang script)", ok, extra)

        # ---- CHECK 2: run_script timing -------------------------------------------
        n = 2
        try:
            t0 = time.monotonic()
            resp = await client.call_tool(
                "run_script", {"name": "p3hang", "timeout_seconds": 2}
            )
            elapsed = time.monotonic() - t0
            r = resp.data
            ok = (2.0 <= elapsed <= 5.0) and isinstance(r, dict)
            extra = f"elapsed={elapsed:.3f}s"
        except Exception as exc:
            ok = False
            elapsed = -1.0
            r = None
            extra = f"exception {type(exc).__name__}: {exc}"
        log.record(n, ok, extra)
        log.line(n, "run_script returns ~timeout window (2-5s), elapsed recorded", ok, extra)

        # ---- CHECK 3: failed status + exit_code None + 'timed out' in error --------
        n = 3
        if not isinstance(r, dict):
            log.fail(n, "dict result with status/exit_code/error", f"r={r!r}")
            log.line(n, "timeout result shape (failed / None / 'timed out')", False, "no dict r")
        else:
            status_ok = r.get("status") == "failed"
            exit_ok = r.get("exit_code") is None
            err_raw = r.get("error") or ""
            err_ok = "timed out" in err_raw.lower()
            overall = status_ok and exit_ok and err_ok
            log.record(n, overall, _verbatim(err_raw))
            extra = f"status={r.get('status')!r}, exit_code={r.get('exit_code')!r}, error={_verbatim(err_raw)}"
            log.line(n, "timeout result shape (failed / None / 'timed out')", overall, extra)

        # ---- CHECK 4: job_status -> 'failed' ---------------------------------------
        n = 4
        job_id = r.get("job_id") if isinstance(r, dict) else None
        try:
            js_resp = await client.call_tool("job_status", {"job_id": job_id})
            js = js_resp.data
            js_ok = isinstance(js, dict) and js.get("status") == "failed"
            extra = f"job_id={job_id!r}, status={js.get('status') if isinstance(js, dict) else None!r}, exit_code={js.get('exit_code') if isinstance(js, dict) else None!r}, error={_verbatim(js.get('error')) if isinstance(js, dict) else None}"
        except Exception as exc:
            js_ok = False
            js = None
            extra = f"exception {type(exc).__name__}: {exc}"
        log.record(n, js_ok, extra)
        log.line(n, "job_status reports 'failed'", js_ok, extra)

        # ---- CHECK 5: get_log returns no 'never-reached' ---------------------------
        n = 5
        try:
            gl_resp = await client.call_tool("get_log", {"job_id": job_id})
            log_text = gl_resp.data or ""
            log_text_str = log_text if isinstance(log_text, str) else str(log_text)
            no_late = "never-reached" not in log_text_str
            log_ok = no_late  # empty or any content w/o the late marker is OK
            extra = f"len={len(log_text_str)}, contains_late_marker={'never-reached' in log_text_str}, preview={log_text_str[:80]!r}"
        except Exception as exc:
            log_ok = False
            extra = f"exception {type(exc).__name__}: {exc}"
        log.record(n, log_ok, extra)
        log.line(n, "get_log has no 'never-reached' (kill before sleep ends)", log_ok, extra)

        # ---- CHECK 6: negative timeout -> ToolError; client still alive ------------
        n = 6
        tool_err_msg: object = None
        tool_err_ok = False
        try:
            await client.call_tool("run_script", {"name": "p3hang", "timeout_seconds": -1})
            extra6 = "no exception raised"
        except ToolError as exc:
            tool_err_ok = True
            tool_err_msg = str(exc)
            extra6 = f"ToolError({tool_err_msg!r})"
        except Exception as exc:
            extra6 = f"unexpected {type(exc).__name__}: {exc}"

        # Now confirm the client is still alive after the bad call
        alive_ok = False
        alive_extra = "skipped (no prior tool error)"
        try:
            ls_resp = await client.call_tool("list_scripts", {})
            ls = ls_resp.data
            alive_ok = isinstance(ls, list) and any(
                isinstance(item, dict) and item.get("name") == "p3hang" for item in ls
            )
            alive_extra = f"list_scripts returned {len(ls) if isinstance(ls, list) else 'non-list'} item(s)"
        except Exception as exc:
            alive_extra = f"list_scripts raised {type(exc).__name__}: {exc}"

        overall6 = tool_err_ok and alive_ok
        log.record(n, overall6, extra6)
        log.line(
            n,
            "negative timeout raises ToolError AND client still alive (list_scripts works)",
            overall6,
            f"{extra6} | {alive_extra}",
        )

    # ---- VERDICT ---------------------------------------------------------------
    if log.passed == log.total:
        print(f"RESULT: PASS ({log.passed}/{log.total} checks passed)", flush=True)
        return 0
    print(
        f"RESULT: FAIL ({log.passed}/{log.total} checks passed)",
        flush=True,
    )
    return 1


if __name__ == "__main__":
    try:
        rc = asyncio.run(asyncio.wait_for(main(), timeout=120))
    except asyncio.TimeoutError:
        print("RESULT: TIMEOUT", flush=True)
        sys.exit(124)
    except Exception as exc:  # noqa: BLE001 — last-resort net for the test script
        print(f"RESULT: FAIL (script crashed: {type(exc).__name__}: {exc})", flush=True)
        sys.exit(1)
    sys.exit(rc)