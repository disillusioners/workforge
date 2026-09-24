# WorkForge

**An MCP server + worker system for AI agents: save Python scripts, run them (sync or async), stream logs live, fetch structured outputs.**

WorkForge turns "can you run this snippet for me?" into a first-class MCP toolset. An AI agent (Claude Desktop, Cursor, or any MCP client) can persist a Python script under a stable name, execute it synchronously and get the result back, or launch it in the background and poll status / tail the live log while it runs. Jobs are durable — every run gets an ID and its output is written to disk, so logs and results survive restarts.

- **MCP-native** — built on [FastMCP](https://github.com/jlowin/fastmcp), speaks stdio MCP: works with Claude Desktop, Cursor, and any MCP client.
- **Sync *and* async execution** — block until done, or fire-and-forget with live log streaming.
- **Durable by default** — scripts and job records live under one home directory (`WORKFORGE_HOME`, default `~/.workforge`).
- **Tiny** — one dependency, ~a few hundred lines of readable Python.

> ## ⚠️ Demo phase — no sandbox
> **WorkForge is currently a demo: scripts run as plain subprocesses with YOUR full user privileges. There is no sandboxing, no authentication, and no resource limiting.**
> Do not point it at untrusted scripts or expose it to untrusted agents. Treat every `run_script` call as if you typed it into your own terminal.
> Sandboxing, auth, and remote workers are planned for phase 2 (see [Roadmap](#roadmap)).

> **Platform note:** Windows support is best-effort and untested; first-class targets are macOS and Linux (see project classifiers).

## Quickstart

Requires [uv](https://docs.astral.sh/uv/) and Python 3.10+.

```bash
git clone https://github.com/disillusioners/workforge.git
cd workforge
uv sync          # creates .venv, installs workforge + fastmcp
uv run workforge # starts the MCP server on stdio
```

If the `workforge` command isn't on your PATH, `python -m workforge` (inside the project venv, e.g. `uv run python -m workforge`) starts the same server.

The server speaks MCP over stdio and is meant to be launched *by your MCP client*, not by hand.

### Optional: keep state elsewhere

```bash
export WORKFORGE_HOME=~/.workforge   # default; scripts/ and jobs/ live here
```

## MCP client configuration

### Claude Desktop

Add to `claude_desktop_config.json` (Claude Desktop → Settings → Developer → Edit Config):

```json
{
  "mcpServers": {
    "workforge": {
      "command": "uv",
      "args": ["--directory", "/absolute/path/to/workforge", "run", "workforge"]
    }
  }
}
```

### Cursor

Add to `~/.cursor/mcp.json` (or Cursor → Settings → MCP → Add server):

```json
{
  "mcpServers": {
    "workforge": {
      "command": "uv",
      "args": ["--directory", "/absolute/path/to/workforge", "run", "workforge"]
    }
  }
}
```

Replace `/absolute/path/to/workforge` with the real cloned path. To give the server a custom home, add an `env` block: `"env": { "WORKFORGE_HOME": "/path/to/home" }`.

## Tools

| Tool | Signature | Returns | Notes |
|---|---|---|---|
| `save_script` | `(name, content, description="")` | `{name, size, updated_at}` | Persists `scripts/<name>.py`. `name` must be a slug (`[a-z][a-z0-9_-]*`). Overwrites allowed. |
| `list_scripts` | `()` | `[{name, description, size, updated_at}]` | All saved scripts. |
| `run_script` | `(name, args=[], timeout_seconds=120)` | `{job_id, status, exit_code, stdout, stderr, duration_ms, error}` | **Blocks** until the run finishes (or times out → killed, `status: "failed"`). |
| `run_script_async` | `(name, args=[], timeout_seconds=0)` | `{job_id, status}` | Returns immediately (`status` starts at `queued`/`running`). `timeout_seconds=0` = no timeout. |
| `job_status` | `(job_id)` | `{status, exit_code, duration_ms, submitted_at, started_at, finished_at, error}` | `status` ∈ `queued \| running \| succeeded \| failed`. |
| `get_log` | `(job_id, tail=null)` | combined stdout+stderr (text) | **Works mid-run** — output streams to disk as the script executes. `tail=N` = last N lines. |
| `get_output` | `(job_id)` | `{exit_code, stdout, stderr, duration_ms, started_at, finished_at}` | Final structured result. |

Script names are slugs (lowercase letters, digits, `-`, `_`) — no paths, no dots. Scripts run with `sys.executable <script> <args...>` and `cwd` set to the job directory, so a script can write scratch files next to its own `job.log` without cluttering anything else.

## Storage layout

```
$WORKFORGE_HOME (default ~/.workforge)
├── scripts/
│   ├── <name>.py          # saved script
│   └── <name>.json        # metadata (description, size, updated_at)
└── jobs/
    └── <job_id>/          # uuid4 hex
        ├── meta.json      # job record: status, timestamps, exit code, captured output
        └── job.log        # combined stdout+stderr, streamed live during execution
```

## Architecture (demo phase)

```
MCP client (Claude Desktop / Cursor)
   │  stdio (MCP)
   ▼
FastMCP server ──► storage (scripts/, jobs/)
   │
   ├─ run_script        ──► subprocess (sys.executable), blocks, streams job.log
   └─ run_script_async  ──► in-process ThreadPoolExecutor (4 workers) ──► same subprocess path
```

Async jobs share the sync execution path: a small thread pool starts each job as a subprocess, pumps combined stdout+stderr into `jobs/<id>/job.log` line by line, and enforces the timeout by killing the whole process group.

## Roadmap

- **Phase 2 — hardening:** sandboxed execution (containers / restricted privileges), authentication, resource limits (CPU/memory/disk), output truncation policies.
- **Phase 2 — distribution:** remote workers (run jobs on another machine), multi-agent job queues, job cancellation.
- **Phase 3 — ergonomics:** script versioning, cron/scheduled runs, a small web UI for job history.

## Development

```bash
uv sync            # includes the dev group (pytest)
uv run pytest      # smoke tests via the fastmcp in-memory client + a real stdio spawn
```

## License

[MIT](LICENSE) — © 2026 disillusioners / WorkForge contributors
