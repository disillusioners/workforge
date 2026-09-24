# Test Packs

## Summary
- Total: 6 test packs (5 E2E + 1 existing-suite)
- Unit: 0 | Integration: 0 | E2E: 5 | Suite: 1

## E2E Test Packs (WorkForge demo phase 1 — task matrix scenarios)

| Pack | Location | Scenarios | Last Run | Status |
|------|----------|-----------|----------|--------|
| e2e_basic_flow_test | .agents/tester/packs/e2e_basic_flow_test.py | 1 (sync), 3 (failure), 6 (edge) | 2026-09-24 | PASS (14/14, ~1 s) |
| e2e_async_lifecycle_test | .agents/tester/packs/e2e_async_lifecycle_test.py | 2 (async lifecycle) | 2026-09-24 | PASS (7/7, ~3.2 s) |
| e2e_timeout_test | .agents/tester/packs/e2e_timeout_test.py | 4 (timeout kill) | 2026-09-24 | PASS (6/6, ~3 s) |
| e2e_concurrency_test | .agents/tester/packs/e2e_concurrency_test.py | 5 (3 concurrent async jobs) | 2026-09-24 | PASS (7/7, ~2 s) |
| e2e_startup_stdio_test | .agents/tester/packs/e2e_startup_stdio_test.py | 7 (startup + README quickstart) | 2026-09-24 | PASS (7/7, ~5 s) |

## Existing-suite Packs

| Pack | Location | Scenarios | Last Run | Status |
|------|----------|-----------|----------|--------|
| existing_pytest_suite_test | inline: `timeout 300 uv run pytest -q` (log: packs/logs/existing_pytest_suite.log) | 8 (dev suite) | 2026-09-24 | PASS (8/8 in 2.84 s; note: test_stdio_server_startup passes vacuously — see LESSONS/) |

## Conventions
- Run from repo root: `timeout 300 uv run python .agents/tester/packs/<name>.py` (dual-layer timeout; scripts self-guard via asyncio.wait_for).
- EVERY pack must set a unique `WORKFORGE_HOME` (mkdtemp) before importing `workforge.server` — parallel-safe isolation; default ~/.workforge is shared.
- Unwrap MCP results via `result.data` (fastmcp 4.0.8); errors surface as `ToolError`.
- stdio spawns: `StdioTransport(command, args, env={**os.environ, "WORKFORGE_HOME": ...})`; kill leftover children by PID only (never blanket-kill; no ports involved).
- Output contract: `RESULT: PASS|FAIL|TIMEOUT`, exit 0/1/124; logs to `.agents/tester/packs/logs/`.
