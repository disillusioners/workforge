# F1: Vacuous smoke test — test_stdio_server_startup (2026-09-24)

**Found by:** recon worker 23e569c6, confirmed by suite worker 806772b1, covered-for-real by pack worker 1bc65d46.

**Root cause:** `tests/test_smoke.py::test_stdio_server_startup` defines an inner `async def scenario():` that builds a `StdioTransport`, connects a `Client`, asserts the 7 tool names, and round-trips save/list — but the function body ends without ever invoking the coroutine (no `with_memory_client(...)`, no `asyncio.run(...)`, unlike the other 7 tests in the file). Defining a coroutine discards it; the test returns `None` → pytest passes it.

**Why it matters:** this is the only dev test claiming to validate the real deliverable (the `workforge` console script speaking MCP over stdio — the Claude Desktop / Cursor entry path). A green suite was masking zero coverage there.

**Verification that the product itself is fine:** our scenario-7 pack (`e2e_startup_stdio_test`) executed the identical path for real: spawned the console script via `StdioTransport`, listed all 7 tools, round-tripped save_script/list_scripts/run_script, verified `python -m workforge` answers MCP `initialize` (serverInfo.name=WorkForge), verified WORKFORGE_HOME honored on disk, README quickstart copy-paste correct, zero leftover processes. 7/7 PASS.

**Recommended fix (1 line, test code only):** end `test_stdio_server_startup` with an actual execution of the scenario, e.g. `asyncio.run(scenario())` (the transport-scoped client cannot reuse `with_memory_client(client, ...)` because it wraps its own `Client(transport)` context).

**Classification:** important (🟠) test-quality issue. Not a product bug. Fix before next dev iteration.
