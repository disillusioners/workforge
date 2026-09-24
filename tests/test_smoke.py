"""WorkForge smoke tests.

Covers the 7 MCP tools via the fastmcp in-memory client, plus one test that
spawns the real server over stdio (the path Claude Desktop / Cursor use).

Every test runs against a temp WORKFORGE_HOME.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

import pytest
from fastmcp import Client
from fastmcp.client.transports import StdioTransport

EXPECTED_TOOLS = {
    "save_script",
    "list_scripts",
    "run_script",
    "run_script_async",
    "job_status",
    "get_log",
    "get_output",
    # phase 2: optional Postgres run history (require WORKFORGE_DATABASE_URL).
    "list_history",
    "history_detail",
}

HELLO_SCRIPT = "print('hello from workforge')\n"
ARGV_SCRIPT = "import sys\nprint('|'.join(sys.argv[1:]))\n"
FAILING_SCRIPT = (
    "import sys\nprint('boom', file=sys.stderr, flush=True)\nsys.exit(3)\n"
)
SLOW_SCRIPT = (
    "import time\n"
    "print('phase-1', flush=True)\n"
    "time.sleep(1.5)\n"
    "print('phase-2', flush=True)\n"
)
HANG_SCRIPT = "import time\ntime.sleep(30)\nprint('never')\n"


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


async def wait_for_log(client: Client, job_id: str, needle: str, deadline_s: float = 5.0) -> str:
    """Poll get_log until it contains `needle` (proves mid-run streaming)."""
    end = time.monotonic() + deadline_s
    log = ""
    while time.monotonic() < end:
        log = await call(client, "get_log", job_id=job_id)
        if needle in log:
            return log
        await asyncio.sleep(0.05)
    pytest.fail(f"log never contained {needle!r}; last log: {log!r}")


# --------------------------------------------------------------------- tools


def test_save_and_list_scripts(client):
    async def scenario():
        saved = await call(
            client,
            "save_script",
            name="hello",
            content=HELLO_SCRIPT,
            description="prints a greeting",
        )
        assert saved["name"] == "hello"
        assert saved["size"] == len(HELLO_SCRIPT.encode())
        assert saved["updated_at"]

        scripts = await call(client, "list_scripts")
        assert len(scripts) == 1
        assert scripts[0]["name"] == "hello"
        assert scripts[0]["description"] == "prints a greeting"
        assert scripts[0]["size"] == saved["size"]

        # overwrite allowed
        await call(client, "save_script", name="hello", content=HELLO_SCRIPT)
        assert len(await call(client, "list_scripts")) == 1

    with_memory_client(client, scenario)


def test_save_script_rejects_bad_names(client):
    async def scenario():
        from fastmcp.exceptions import ToolError

        for bad in ("../escape", "Upper", "has.dot", ""):
            with pytest.raises(ToolError):
                await call(client, "save_script", name=bad, content="pass")

    with_memory_client(client, scenario)


def test_sync_run_stdout_exit_code_and_args(client):
    async def scenario():
        await call(client, "save_script", name="hello", content=HELLO_SCRIPT)
        res = await call(client, "run_script", name="hello")
        assert res["job_id"]
        assert res["status"] == "succeeded"
        assert res["exit_code"] == 0
        assert res["stdout"] == "hello from workforge\n"
        assert isinstance(res["duration_ms"], int)

        await call(client, "save_script", name="argv", content=ARGV_SCRIPT)
        res = await call(client, "run_script", name="argv", args=["one", "two"])
        assert res["exit_code"] == 0
        assert res["stdout"] == "one|two\n"

    with_memory_client(client, scenario)


def test_async_run_transitions_live_log_and_output(client):
    async def scenario():
        await call(client, "save_script", name="slow", content=SLOW_SCRIPT)

        started = await call(client, "run_script_async", name="slow")
        job_id = started["job_id"]
        assert started["status"] in {"queued", "running"}

        status = await wait_for_status(client, job_id, {"running"})
        assert status["script"] == "slow"
        assert status["started_at"]

        # mid-run: log streams to disk while the script sleeps
        log = await wait_for_log(client, job_id, "phase-1")
        status_now = await call(client, "job_status", job_id=job_id)
        assert status_now["status"] == "running"
        assert "phase-2" not in log

        status = await wait_for_status(client, job_id, {"succeeded"})
        assert status["exit_code"] == 0
        assert status["duration_ms"] >= 1000
        assert status["finished_at"]

        log = await call(client, "get_log", job_id=job_id)
        assert log.index("phase-1") < log.index("phase-2")

        out = await call(client, "get_output", job_id=job_id)
        assert out["status"] == "succeeded"
        assert out["exit_code"] == 0
        assert out["stdout"] == "phase-1\nphase-2\n"
        assert out["stderr"] == ""
        assert out["started_at"] and out["finished_at"]

        # tail=N returns only the last N lines
        tail = await call(client, "get_log", job_id=job_id, tail=1)
        assert tail == "phase-2"

    with_memory_client(client, scenario)


def test_failing_script_nonzero_exit_and_stderr(client):
    async def scenario():
        await call(client, "save_script", name="fail", content=FAILING_SCRIPT)
        res = await call(client, "run_script", name="fail")
        assert res["status"] == "failed"
        assert res["exit_code"] == 3
        assert "boom" in res["stderr"]
        assert res["error"] and "3" in res["error"]

        out = await call(client, "get_output", job_id=res["job_id"])
        assert out["status"] == "failed"
        assert out["exit_code"] == 3

    with_memory_client(client, scenario)


def test_timeout_kills_run(client):
    async def scenario():
        await call(client, "save_script", name="hang", content=HANG_SCRIPT)
        started = await call(
            client, "run_script_async", name="hang", timeout_seconds=1
        )
        status = await wait_for_status(
            client, started["job_id"], {"failed"}, deadline_s=10
        )
        assert status["exit_code"] is None
        assert "timed out" in (status["error"] or "").lower()

    with_memory_client(client, scenario)


def test_async_spawn_failure_finalizes_job_as_failed(client, monkeypatch):
    """If Popen raises, the job must end "failed" — never stuck "running"."""
    import workforge.engine as engine

    def raise_oserror(*args, **kwargs):
        raise OSError("spawn blocked by test")

    monkeypatch.setattr(engine.subprocess, "Popen", raise_oserror)

    async def scenario():
        await call(client, "save_script", name="spawnfail", content=HELLO_SCRIPT)
        started = await call(client, "run_script_async", name="spawnfail")
        status = await wait_for_status(
            client, started["job_id"], {"failed"}, deadline_s=10
        )
        assert status["exit_code"] is None
        assert "spawn blocked by test" in (status["error"] or "")
        assert status["finished_at"]

    with_memory_client(client, scenario)


def test_stdio_server_startup(wf_home):
    """The real deliverable: `workforge` starts and serves MCP over stdio."""
    console_script = Path(sys.executable).parent / "workforge"
    if console_script.exists():
        command, args = str(console_script), []
    else:  # fall back to the module entry (same code path)
        command, args = sys.executable, ["-m", "workforge"]

    transport = StdioTransport(
        command,
        args,
        env={**os.environ, "WORKFORGE_HOME": str(wf_home)},
    )

    async def scenario():
        async with Client(transport) as stdio_client:
            tools = await stdio_client.list_tools()
            assert {t.name for t in tools} == EXPECTED_TOOLS
            saved = await call(
                stdio_client, "save_script", name="stdio-check", content=HELLO_SCRIPT
            )
            assert saved["name"] == "stdio-check"
            listing = await call(stdio_client, "list_scripts")
            assert [s["name"] for s in listing] == ["stdio-check"]

    asyncio.run(scenario())


# ----------------------------------------------------- regression: deep review


def test_get_log_tail_zero_returns_empty_via_tool(client):
    """R1 regression — must assert through the MCP tool layer (item 14).

    Without the storage-layer early-return fix, ``get_log(tail=0)`` would
    return the entire log instead of an empty string. Pin both layers:
    pin the tool contract here, pin the storage layer in the unit test
    below.
    """
    async def scenario():
        await call(client, "save_script", name="hello", content=HELLO_SCRIPT)
        res = await call(client, "run_script", name="hello")
        # Confirm there IS log content first, so tail=0 -> "" is meaningful.
        full = await call(client, "get_log", job_id=res["job_id"])
        assert "hello from workforge" in full
        zero = await call(client, "get_log", job_id=res["job_id"], tail=0)
        assert zero == ""

    with_memory_client(client, scenario)


def test_storage_get_log_tail_zero_returns_empty(wf_home):
    """R1 unit-level pin: storage layer also returns "" for tail=0."""
    from workforge import storage

    storage.write_job_meta(
        {
            "job_id": "a" * 32,
            "script": "x",
            "args": [],
            "status": "succeeded",
            "exit_code": 0,
            "submitted_at": storage.now_iso(),
            "started_at": storage.now_iso(),
            "finished_at": storage.now_iso(),
            "duration_ms": 0,
            "stdout": "",
            "stderr": "",
            "error": None,
        }
    )
    storage.job_log_path("a" * 32).write_text("line-1\nline-2\nline-3\n", encoding="utf-8")
    assert storage.read_job_log("a" * 32) == "line-1\nline-2\nline-3\n"
    assert storage.read_job_log("a" * 32, tail=0) == ""
    assert storage.read_job_log("a" * 32, tail=1) == "line-3"
    assert storage.read_job_log("a" * 32, tail=2) == "line-2\nline-3"


def test_timeout_seconds_zero_rejected_on_sync_and_async(client):
    """R2 regression — timeout_seconds < 1 rejected on BOTH paths as ToolError."""
    from fastmcp.exceptions import ToolError

    async def scenario():
        await call(client, "save_script", name="hello", content=HELLO_SCRIPT)
        for bad in (0, -1, -5):
            with pytest.raises(ToolError):
                await call(client, "run_script", name="hello", timeout_seconds=bad)
            with pytest.raises(ToolError):
                await call(
                    client, "run_script_async", name="hello", timeout_seconds=bad
                )

    with_memory_client(client, scenario)


def test_async_default_timeout_is_300_seconds():
    """R2 regression — async default is now 300s (not 0/no-timeout)."""
    import asyncio
    import inspect

    from workforge.server import mcp

    async def _get():
        return await mcp.get_tool("run_script_async")

    tool = asyncio.run(_get())
    sig = inspect.signature(tool.fn)
    assert sig.parameters["timeout_seconds"].default == 300


def test_pool_shutdown_kills_inflight_promptly(client):
    """R3 regression — calling _shutdown_pool with a running job returns fast
    AND kills the in-flight child process group (the reason non-daemon pool
    workers can otherwise wedge the process at shutdown)."""
    import workforge.engine as engine

    async def scenario():
        await call(client, "save_script", name="hang", content=HANG_SCRIPT)
        started = await call(client, "run_script_async", name="hang", timeout_seconds=300)
        # Wait until the job is actually running so _in_flight is populated.
        await wait_for_status(client, started["job_id"], {"running"})
        # Grab the registered Popen so we can verify it gets killed.
        with engine._in_flight_lock:
            inflight = list(engine._in_flight.values())
        assert inflight, "_in_flight should be populated for the running job"
        proc = inflight[0]

        t0 = time.monotonic()
        engine._shutdown_pool()
        elapsed = time.monotonic() - t0
        assert elapsed < 5.0, f"_shutdown_pool took {elapsed:.1f}s, expected <5s"
        # Process must be dead (or at least unwaitable) within the same window.
        proc.wait(timeout=2)
        assert proc.returncode is not None

    with_memory_client(client, scenario)


def test_concurrent_same_name_saves_no_corruption(wf_home):
    """#6 regression — N threads saving the same script concurrently must
    leave a single, complete, valid .py + .json sidecar (no fixed-tmp race
    overwriting each other's bytes)."""
    from concurrent.futures import ThreadPoolExecutor

    from workforge import storage

    N = 16
    contents = [f"print({i!r})\n" for i in range(N)]

    def worker(idx):
        return storage.save_script("race", contents[idx], description=f"writer-{idx}")

    with ThreadPoolExecutor(max_workers=N) as pool:
        results = list(pool.map(worker, range(N)))

    # The script body on disk must equal SOME worker's full write — never
    # a byte-wise interleaving. Read it directly via the storage layer.
    spath = storage.script_path("race")
    assert spath.exists()
    final_body = spath.read_text(encoding="utf-8")
    assert final_body in contents, f"final body {final_body!r} is not a full write"

    # list_scripts must report exactly one entry with the saved metadata.
    listing = storage.list_scripts()
    assert len(listing) == 1
    assert listing[0]["name"] == "race"
    # All N workers must have returned a meta dict with size matching their
    # write — none should have raised (which would have happened with the
    # old fixed-tmp + concurrent os.replace).
    assert len(results) == N
    assert all(r["size"] == len(c.encode()) for r, c in zip(results, contents))


def test_tampered_sidecar_name_is_skipped(wf_home):
    """#11 regression — list_scripts must drop entries whose sidecar ``name``
    fails validation (otherwise ``{"name": "../escape"}`` could escape
    scripts/)."""
    import json

    from workforge import storage

    storage.scripts_dir().mkdir(parents=True, exist_ok=True)
    # Plant a tampered sidecar with a path-traversal name.
    (storage.scripts_dir() / "evil.json").write_text(
        json.dumps({"name": "../escape", "size": 0, "updated_at": "x"}),
        encoding="utf-8",
    )
    # Also plant a valid one — it must still show up.
    storage.save_script("good", "print('ok')\n")

    listing = storage.list_scripts()
    names = [s["name"] for s in listing]
    assert "good" in names
    assert "../escape" not in names
    assert all("/" not in n and "\\" not in n and ".." not in n for n in names)


def test_trailing_newline_name_rejected(client):
    """#12 regression — script names with trailing newline (which the old
    ``$``-anchored regex allowed) must now be rejected by save_script."""
    from fastmcp.exceptions import ToolError

    async def scenario():
        with pytest.raises(ToolError):
            await call(client, "save_script", name="abc\n", content="pass")
        # Same for the storage layer's validator.
        from workforge import storage

        with pytest.raises(ValueError):
            storage.validate_name("abc\n")
        with pytest.raises(ValueError):
            storage.validate_job_id("0123456789abcdef0123456789abcde\n")

    with_memory_client(client, scenario)



