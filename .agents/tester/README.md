# WorkForge — Tester Knowledge Base

Project: WorkForge v0.1 (demo phase 1) — MCP server + worker system for AI agents.
Repo: /Users/nguyenminhkha/All/Code/opensource-projects/workforge
Branch under test: `feature/workforge-demo-phase1` (HEAD aed41aa per task)

## Stack
- Python + FastMCP (stdio MCP server, `uv run workforge`)
- Tests use fastmcp in-memory Client
- Dev smoke suite: `tests/test_smoke.py` (8 tests) — run via `uv sync && uv run pytest`

## Test layout
- E2E pack scripts: `.agents/tester/packs/` (run via `timeout 300 uv run python <pack>` from repo root)
- See PACKS.md for the inventory.

## Conventions
- `uv` only (uv sync first). Always pass repo as explicit workdir.
- Packs use unique script-name prefixes to avoid cross-pack storage collisions.
- Port safety: server is stdio (no ports). Never kill port 8088 (ensemble self-system).

## Known facts (validated 2026-09-24, HEAD aed41aa — full E2E matrix 8/8 PASS, see RESULTS/)
- Branch `feature/workforge-demo-phase1` @ aed41aa; `.agents/` is NOT gitignored.
- fastmcp 4.0.8; `StdioTransport(command, args, env=None, cwd=None, ...)` positional form.
- Storage: `$WORKFORGE_HOME` (default ~/.workforge) with `scripts/` + `jobs/<uuid4hex>/`; `home()` re-reads env every call → set env before/any time before tool calls. Isolation pattern: `os.environ["WORKFORGE_HOME"] = tempfile.mkdtemp(prefix="wf-<pack>-")` before importing `workforge.server`.
- 7 MCP tools: save_script, list_scripts, run_script (sync, timeout_seconds=120 default), run_script_async (timeout_seconds=0 = none), job_status, get_log (combined stdout+stderr; tail=N = last N lines joined "\n", no trailing newline), get_output. Status enum: queued|running|succeeded|failed.
- Timeout finalization: status=failed, exit_code=None, error="timed out after {N}s; process group killed". Verified kill lands in 2.010 s.
- Engine pool MAX_WORKERS=4 → `queued` only observable under saturation; initial async status seen in practice is `running`.
- Unwrap pattern: `result.data` on fastmcp call_tool results; server ValueError → client ToolError.
- Known test-quality issue: dev `test_stdio_server_startup` passes VACUOUSLY (scenario never executed) — see LESSONS/2026-09-24-vacuous-stdio-smoke-test.md. Product stdio path itself verified working by our pack.
