#!/usr/bin/env python3
"""E2E STARTUP stdio + README quickstart — scenario 7.

Spawns the real WorkForge MCP server over stdio via the correct
fastmcp transport for the installed version, exercises save/list/run
through the stdio client, then verifies the README quickstart commands
(`uv run workforge` and `uv run python -m workforge`) copy-paste
correctly.

Isolation: every spawned server gets its own WORKFORGE_HOME under
tempfile.gettempdir() so 5 sibling packs can run in parallel without
touching ~/.workforge or each other.

Output contract: final line is `RESULT: PASS` / `RESULT: FAIL (X/Y checks passed)`
/ `RESULT: TIMEOUT` with corresponding exit code (0 / 1 / 124).
"""
from __future__ import annotations

import asyncio
import json
import os
import platform
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

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
}

CHECKS_PASS = 0
CHECKS_TOTAL = 0


# ---------------------------------------------------------------------------
# Check + cleanup helpers
# ---------------------------------------------------------------------------


def check(name: str, ok: bool, detail: str = "") -> None:
    """Emit one [CHECK n] line. Increment counters."""
    global CHECKS_PASS, CHECKS_TOTAL
    CHECKS_TOTAL += 1
    if ok:
        CHECKS_PASS += 1
        print(f"[CHECK {CHECKS_TOTAL}] {name} ... OK", flush=True)
    else:
        print(f"[CHECK {CHECKS_TOTAL}] {name} ... FAIL", flush=True)
        if detail:
            for line in detail.splitlines():
                print(f"    {line}", flush=True)


def find_workforge_console() -> Path | None:
    """Resolve the venv console script `workforge` if installed."""
    candidate = Path(sys.executable).parent / "workforge"
    return candidate if candidate.exists() else None


def _cmdline_of(pid: int) -> str:
    """Best-effort cmdline lookup, cross-platform."""
    try:
        if platform.system() == "Linux":
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                return f.read().decode(errors="replace").replace("\x00", " ")
        out = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True, text=True, timeout=3,
        )
        return out.stdout
    except (FileNotFoundError, ProcessLookupError, PermissionError, subprocess.TimeoutExpired):
        return ""


async def kill_leftover(tag: str) -> None:
    """Verify no leftover workforge procs; nuke any of OUR direct children.

    Rules:
    - We only ever kill processes whose parent is OUR pid (pgrep -P).
    - We never blanket-kill anything matching "workforge" broadly.
    - SIGTERM first, then SIGKILL after a 3 s grace.
    """
    # Informational pgrep -fa (per task spec)
    try:
        v = subprocess.run(
            ["pgrep", "-fa", "workforge"],
            capture_output=True, text=True, timeout=5,
        )
        text = v.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        text = "(pgrep unavailable)"
    print(
        f"  [cleanup:{tag}] pgrep -fa workforge ->\n    "
        + (text if text else "(empty)"),
        flush=True,
    )

    # Target only our direct children
    try:
        c = subprocess.run(
            ["pgrep", "-P", str(os.getpid())],
            capture_output=True, text=True, timeout=5,
        )
        children = [int(x) for x in c.stdout.split() if x.isdigit()]
    except (FileNotFoundError, subprocess.TimeoutExpired):
        children = []

    if not children:
        print(f"  [cleanup:{tag}] no direct children of pid {os.getpid()}", flush=True)
        return

    # SIGTERM round
    for pid in children:
        cmd = _cmdline_of(pid)
        try:
            os.kill(pid, signal.SIGTERM)
            print(f"  [cleanup:{tag}] SIGTERM -> child {pid}: {cmd.strip()[:120]}", flush=True)
        except ProcessLookupError:
            continue
    await asyncio.sleep(3)

    # SIGKILL survivors
    try:
        c2 = subprocess.run(
            ["pgrep", "-P", str(os.getpid())],
            capture_output=True, text=True, timeout=5,
        )
        survivors = [int(x) for x in c2.stdout.split() if x.isdigit()]
    except (FileNotFoundError, subprocess.TimeoutExpired):
        survivors = []

    for pid in survivors:
        cmd = _cmdline_of(pid)
        try:
            os.kill(pid, signal.SIGKILL)
            print(f"  [cleanup:{tag}] SIGKILL -> child {pid}: {cmd.strip()[:120]}", flush=True)
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            pass


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------


async def scenario_1_stdio(home: Path) -> dict:
    """Spawn real workforge over stdio; check 7 tools + round-trip p5stdio."""
    console = find_workforge_console()
    if console is not None:
        command, args = str(console), []
    else:
        command, args = sys.executable, ["-m", "workforge"]

    print(
        f"  [scenario 1] command={command} args={args} WORKFORGE_HOME={home}",
        flush=True,
    )

    transport = StdioTransport(
        command,
        args,
        env={**os.environ, "WORKFORGE_HOME": str(home)},
    )

    observed_names: list[str] = []
    p5_status = ""
    p5_exit: int | None = None
    p5_stdout = ""
    saved_payload = None
    listed_names: list[str] = []

    async with Client(transport) as client:
        tools = await client.list_tools()
        observed_names = [t.name for t in tools]
        check(
            "stdio list_tools returns all 7 expected tools",
            EXPECTED_TOOLS.issubset(set(observed_names)),
            detail=(
                f"expected >= {sorted(EXPECTED_TOOLS)}\n"
                f"     got      {sorted(observed_names)}"
            ),
        )

        saved = await client.call_tool(
            "save_script",
            {
                "name": "p5stdio",
                "content": 'print("p5-alive", flush=True)\n',
                "description": "e2e probe",
            },
        )
        saved_payload = saved.data
        check(
            "stdio save_script('p5stdio', ...) round-trip",
            isinstance(saved_payload, dict) and saved_payload.get("name") == "p5stdio",
            detail=f"expected dict with name=='p5stdio', got {saved_payload!r}",
        )

        listed = await client.call_tool("list_scripts", {})
        listed_data = listed.data
        if isinstance(listed_data, list):
            listed_names = [s.get("name") for s in listed_data if isinstance(s, dict)]
        elif isinstance(listed_data, dict):
            inner = listed_data.get("scripts") or listed_data.get("items") or []
            listed_names = [s.get("name") for s in inner if isinstance(s, dict)]
        check(
            "stdio list_scripts contains 'p5stdio'",
            "p5stdio" in listed_names,
            detail=f"got names={listed_names}",
        )

        ran = await client.call_tool("run_script", {"name": "p5stdio"})
        ran_data = ran.data
        if isinstance(ran_data, dict):
            p5_status = str(ran_data.get("status", ""))
            p5_exit = ran_data.get("exit_code")
            p5_stdout = str(ran_data.get("stdout", ""))
        check(
            "stdio run_script('p5stdio') -> succeeded/exit=0/'p5-alive' in stdout",
            p5_status == "succeeded" and p5_exit == 0 and "p5-alive" in p5_stdout,
            detail=(
                f"status={p5_status!r} exit_code={p5_exit!r} stdout={p5_stdout!r}\n"
                f"     full payload={ran_data!r}"
            ),
        )

    # after async with exits — verify env honored on disk
    p5_path = home / "scripts" / "p5stdio.py"
    check(
        "WORKFORGE_HOME honored on disk: scripts/p5stdio.py exists in spawn-1 home",
        p5_path.exists(),
        detail=f"expected {p5_path} to exist after stdio save_script",
    )

    await kill_leftover("after-scenario-1")

    return {
        "command": command,
        "args": list(args),
        "observed_tool_names": observed_names,
        "saved_payload": saved_payload,
        "listed_names": listed_names,
        "p5_status": p5_status,
        "p5_exit_code": p5_exit,
        "p5_stdout": p5_stdout,
    }


async def scenario_2_python_m(home: Path) -> dict:
    """README quickstart: `uv run python -m workforge` speaks MCP over stdio.

    We bypass StdioTransport (uv is not a flat binary — it sets up the
    venv then execs), spawn the command directly via asyncio subprocess,
    and do a raw MCP `initialize` JSON-RPC handshake on stdin/stdout.
    """
    print(
        f"  [scenario 2] spawning: uv run python -m workforge  WORKFORGE_HOME={home}",
        flush=True,
    )

    proc = await asyncio.create_subprocess_exec(
        "uv",
        "run",
        "python",
        "-m",
        "workforge",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={**os.environ, "WORKFORGE_HOME": str(home)},
    )

    init_req = (
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {
                        "name": "e2e-startup-probe",
                        "version": "0.0.0",
                    },
                },
            }
        )
        + "\n"
    )

    async def drain_stderr() -> list[str]:
        chunks = []
        while True:
            line = await proc.stderr.readline()
            if not line:
                break
            chunks.append(line.decode(errors="replace").rstrip())
        return chunks

    stderr_task = asyncio.create_task(drain_stderr())

    raw_line = ""
    try:
        proc.stdin.write(init_req.encode())
        await proc.stdin.drain()
        line = await asyncio.wait_for(proc.stdout.readline(), timeout=25)
        raw_line = line.decode(errors="replace").strip()
    finally:
        try:
            if proc.stdin is not None and not proc.stdin.is_closing():
                proc.stdin.close()
        except Exception:
            pass
        try:
            proc.terminate()
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except asyncio.TimeoutExpired:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            try:
                await proc.wait()
            except Exception:
                pass

    stderr_chunks = await stderr_task
    if stderr_chunks:
        joined_stderr = " |\n    | ".join(stderr_chunks)
        print(f"  [scenario 2] server stderr (drained):\n    | {joined_stderr}", flush=True)

    parsed: object = None
    server_info = ""
    try:
        msg = json.loads(raw_line)
        if isinstance(msg, dict):
            parsed = msg
            if "result" in msg and isinstance(msg["result"], dict):
                server_info = str(msg["result"].get("serverInfo", {}).get("name", ""))
    except Exception as exc:
        parsed = {"_parse_error": str(exc), "_raw": raw_line}

    ok = (
        isinstance(parsed, dict)
        and "_parse_error" not in parsed
        and ("result" in parsed or "error" in parsed)
    )
    check(
        "`uv run python -m workforge` responds to MCP initialize JSON-RPC",
        ok,
        detail=(
            f"raw_line={raw_line!r}\n"
            f"     parsed={parsed!r}"
            + (f"\n     serverInfo.name={server_info!r}" if server_info else "")
        ),
    )

    await kill_leftover("after-scenario-2")
    return {
        "raw_response": raw_line,
        "parsed": parsed,
        "server_info": server_info,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def main() -> int:
    print("=== Test Pack: e2e_startup_stdio_test ===", flush=True)
    print(f"  workforge console: {find_workforge_console()}", flush=True)
    print(f"  python executable: {sys.executable}", flush=True)

    # record the StdioTransport signature verbatim
    import inspect

    sig = inspect.signature(StdioTransport.__init__)
    print(f"  fastmcp StdioTransport.__init__ signature: {sig}", flush=True)

    home1 = Path(tempfile.mkdtemp(prefix="wf-home-1-"))
    home2 = Path(tempfile.mkdtemp(prefix="wf-home-2-"))
    print(f"  WORKFORGE_HOME (spawn 1): {home1}", flush=True)
    print(f"  WORKFORGE_HOME (spawn 2): {home2}", flush=True)
    print(f"  tempfile.gettempdir():    {tempfile.gettempdir()}", flush=True)

    check(
        "unique WORKFORGE_HOME per spawn (5 sibling packs run in parallel)",
        (
            str(home1) != str(home2)
            and str(home1).startswith(tempfile.gettempdir())
            and str(home2).startswith(tempfile.gettempdir())
        ),
        detail=(
            f"home1={home1}\n"
            f"     home2={home2}\n"
            f"     tmpdir={tempfile.gettempdir()}"
        ),
    )

    s1 = await scenario_1_stdio(home1)
    print(
        f"  [scenario 1] observed_tool_names={s1['observed_tool_names']}",
        flush=True,
    )

    s2 = await scenario_2_python_m(home2)
    print(
        f"  [scenario 2] serverInfo.name={s2['server_info']!r} raw={s2['raw_response']!r}",
        flush=True,
    )

    print()
    print(f"checks passed: {CHECKS_PASS}/{CHECKS_TOTAL}", flush=True)

    await kill_leftover("final")

    if CHECKS_PASS == CHECKS_TOTAL:
        print("RESULT: PASS", flush=True)
        return 0
    print(f"RESULT: FAIL ({CHECKS_PASS}/{CHECKS_TOTAL} checks passed)", flush=True)
    return 1


if __name__ == "__main__":
    try:
        exit_code = asyncio.run(asyncio.wait_for(main(), timeout=240))
    except asyncio.TimeoutError:
        print("RESULT: TIMEOUT", flush=True)
        sys.exit(124)
    sys.exit(exit_code)