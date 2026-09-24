# WorkForge

**An MCP server + worker system for AI agents: save Python scripts, run them (sync or async), stream logs live, fetch structured outputs.**

WorkForge turns "can you run this snippet for me?" into a first-class MCP toolset. An AI agent (Claude Desktop, Cursor, or any MCP client) can persist a Python script under a stable name, execute it synchronously and get the result back, or launch it in the background and poll status / tail the live log while it runs. Jobs are durable — every run gets an ID and its output is written to disk, so **completed** logs and results survive restarts. (In-flight async jobs do NOT survive: a process restart kills any job that was still running and leaves no record of the partial run — by design, demo-phase only.)

- **MCP-native** — built on [FastMCP](https://github.com/jlowin/fastmcp), speaks stdio MCP: works with Claude Desktop, Cursor, and any MCP client.
- **Sync *and* async execution** — block until done, or fire-and-forget with live log streaming.
- **Durable by default** — scripts and job records live under one home directory (`WORKFORGE_HOME`, default `~/.workforge`).
- **Persistent run history (optional)** — set `WORKFORGE_DATABASE_URL` and every run is recorded in Postgres, queryable via the `list_history` / `history_detail` tools.
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
| `run_script` | `(name, args=[], timeout_seconds=120)` | `{job_id, status, exit_code, stdout, stderr, duration_ms, error}` | **Blocks** until the run finishes (or times out → killed, `status: "failed"`). `timeout_seconds` must be ≥ 1; values < 1 are rejected. |
| `run_script_async` | `(name, args=[], timeout_seconds=300)` | `{job_id, status}` | Returns immediately (`status` starts at `queued`/`running`). Default 300s = 5 min cap (0 / no-timeout is rejected — a hung job would silently saturate the 4-worker pool). |
| `job_status` | `(job_id)` | `{job_id, script, args, status, exit_code, submitted_at, started_at, finished_at, duration_ms, error}` | `status` ∈ `queued \| running \| succeeded \| failed`. Captured stdout/stderr omitted (use `get_output`); absolute `script_path` intentionally excluded. |
| `get_log` | `(job_id, tail=null)` | combined stdout+stderr (text) | **Works mid-run** — output streams to disk as the script executes. `tail=N` = last N lines; `tail=0` returns the empty string (not the full log). |
| `get_output` | `(job_id)` | `{job_id, script, args, status, exit_code, submitted_at, started_at, finished_at, duration_ms, stdout, stderr, error}` | Final structured result; absolute `script_path` intentionally excluded. |
| `list_history` | `(limit=50, script_name=null, status=null)` | `[{job_id, script_name, status, exit_code, started_at, duration_ms}]` | **Requires Postgres** (see [Run history](#run-history-optional-postgres)). Newest-first rows of past runs; `limit` is 1–200 (anything else is rejected); `status` ∈ `queued \| running \| succeeded \| failed`. Raises a structured error when `WORKFORGE_DATABASE_URL` is unset. |
| `history_detail` | `(job_id)` | `{job_id, input: {script_name, args, timeout_seconds}, output: {status, exit_code, stdout, stderr, duration_ms, started_at, finished_at}}` | **Requires Postgres.** Full record of one past run; unknown/malformed `job_id` raises a structured error. |

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

## Run history (optional Postgres)

Phase 2 feature: with a Postgres URL configured, WorkForge records every job run in a `job_runs` table (inserted as `running` on start, updated to the final status at the end) and exposes two extra tools, `list_history` and `history_detail` (see [Tools](#tools)).

### Setup

```bash
# 1. Point WorkForge at Postgres (no password needed for a default local
#    install using unix-socket peer auth):
export WORKFORGE_DATABASE_URL=postgresql:///workforge   # see .env.example

# 2. Create the database + schema (guarded: only 'workforge' or
#    'workforge_test' can ever be created):
uv run workforge db-init        # or: uv run workforge-db-init
```

The schema (`job_runs` plus `created_at`/`script_name` indexes) is also ensured automatically on first use, so `db-init` is only needed for the database itself.

### Deployment notes

- **The MCP server reads `WORKFORGE_DATABASE_URL` from its environment at launch.** In an MCP client config, pass it in the `env` block, e.g. Claude Desktop: `"env": { "WORKFORGE_DATABASE_URL": "postgresql:///workforge" }` (same place as `WORKFORGE_HOME`).
- **History is optional and fail-safe.** Unset, everything works exactly as before (the history tools raise a structured "not configured" error). With it set, a Postgres outage never fails a running job — the failure is recorded as `history_write_error` in the job's `meta.json` and the job completes normally. All history operations use short connect/query timeouts (~5s), so a dead database can never hang a tool or a job.
- Tests target the `workforge_test` database only (`tests/test_history.py`) and skip automatically when Postgres is unreachable.

## Deployment (Docker)

WorkForge ships as a multi-stage Docker image (`python:3.12-slim-bookworm` base, non-root user, ~no extra OS packages — `psycopg[binary]` bundles libpq). The server still speaks **MCP over stdio**, so "deploying" it means attaching your MCP client to the container's stdin/stdout.

### Build & run

```bash
# From the repo root:
docker build -t workforge:0.1.0 .

# Quick check — the server answers an MCP initialize on stdio:
echo '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"smoke","version":"0.0.0"}}}' \
  | docker run --rm -i workforge:0.1.0
```

State lives under `WORKFORGE_HOME=/data/workforge` inside the container, declared as a `VOLUME` — mount a named volume so saved scripts and job records survive container replacement:

```bash
docker run --rm -i -v workforge-data:/data/workforge workforge:0.1.0
```

### Attaching an MCP client (stdio)

The image's entrypoint is the `workforge` CLI, so MCP clients launch it directly with `docker run -i` (keep `-i` — that's the stdio pipe). Claude Desktop / Cursor config:

```json
{
  "mcpServers": {
    "workforge": {
      "command": "docker",
      "args": ["run", "--rm", "-i", "-v", "workforge-data:/data/workforge", "workforge:0.1.0"]
    }
  }
}
```

### Optional: run history against a remote Postgres

Pass a **TCP DSN** (containers can't reach your host's unix socket by default):

```bash
docker run --rm -i \
  -v workforge-data:/data/workforge \
  -e WORKFORGE_DATABASE_URL=postgresql://user:pass@db-host:5432/workforge \
  workforge:0.1.0
```

One-time bootstrap (`workforge-db-init` is also available via `--entrypoint workforge-db-init`; the `db-init` subcommand is the same guarded path — it can only ever create a database named `workforge` or `workforge_test`, never drop or touch anything else):

```bash
docker run --rm \
  -e WORKFORGE_DATABASE_URL=postgresql://user:pass@db-host:5432/workforge \
  workforge:0.1.0 db-init
```

### CI

`.gitlab-ci.yml` (stages: `test → build → push`) runs the pytest suite from source, validates the Dockerfile builds on MRs, and publishes to `$CI_REGISTRY_IMAGE` with `:$CI_COMMIT_SHORT_SHA` + `:latest` tags on the `latest` branch (plus `:X.Y.Z` on `vX.Y.Z` tags).

> **Phase-3 note:** stdio means the container runs *next to* one MCP client, not as a shared remote service. A `streamable-http` transport mode is planned for phase 3 — that's when this image gains a port, a `HEALTHCHECK`, and true remote serving.

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

- **Phase 2 — run history:** ✅ shipped — persistent run history in Postgres (`list_history` / `history_detail`, guarded `db-init`, fail-safe engine writes).
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
