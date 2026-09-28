# Incident Report: MCP tool result never delivered → agent run hangs silently

- **Date:** 2026-09-28
- **Discovered via:** stuck chat `f54c03cd-cc24-470b-a03a-04f72a5756d8` on https://fleet.yedda.tech (agent-fleet backend)
- **Diagnosed by:** DevOps agent (live repro against production workforge container)
- **Status:** root cause confirmed by reproduction; **primary fix belongs in `yedda-agent-fleet`**, secondary hardening in workforge
- **Impact:** ANY MCP tool call whose script runs longer than ~5 seconds hangs the whole agent run with no error surfaced to the user. The chat UI spins forever.

---

## 1. Executive summary

`agent-fleet`'s runtime agent builder constructs the httpx client for MCP
streamable-HTTP sessions **without a timeout**, so httpx's default **5-second
read timeout** applies. In the streamable-HTTP protocol the tool-call result
arrives as an SSE event on the POST response stream — and the workforge server
sends **zero body bytes** on that stream until the tool finishes (no event
store → no priming event; sse-starlette ping interval is 15s > 5s).

A script that runs longer than 5s therefore races the read timeout — and loses:

1. httpx raises `ReadTimeout` on the POST response stream at t+5s.
2. The `mcp` SDK client **swallows the exception at DEBUG level** and skips
   reconnection (reconnect is gated on `last_event_id is not None`, which is
   always `None` without an event store).
3. The pending `session.call_tool()` future never resolves. MAF's
   `MCPStreamableHTTPTool` was built with `request_timeout=None` →
   `read_timeout_seconds=None` → **nothing bounds the wait**.
4. agent-fleet's 240s run budget cancels the task. MAF logs only
   `Function duration: 217.97s` (no "succeeded"/"failed" line = CancelledError),
   the cancellation is **not persisted to the chat**, and the UI hangs forever.

The original business-level trigger (kpi_set_target duplicate-key failure,
job ran 5.5s) is already fixed in the deployed scripts (last-wins upsert),
but **the delivery bug is unaffected by that fix**: any future MCP tool call
>5s will hang the run exactly the same way.

---

## 2. Incident timeline (all times UTC, from server logs + DB)

| Time | Source | Event |
|---|---|---|
| 04:01:11 | test_results.kpi_param | Earlier run inserts target `(3718, 2026-09-28, X=111.0)` |
| 10:41:18 | chat | User: "give marks for company 3718 last week" |
| 10:41:50 | backend | Agent asks stored-vs-custom question (`ask_user`) |
| 10:42:17.59 | backend | User answers "Auto"; run resumes |
| 10:42:17.87 | backend | New MCP session to workforge: initialize + tools/list |
| 10:42:22.88 | backend | `GET stream disconnected, reconnecting in 1000ms...` (5.0s after GET opened — same 5s read timeout) |
| 10:42:23.89 | backend | GET reconnect #2 (also dies ~5s later; reader gives up) |
| 10:42:39.74 | backend | `Function name: workforge_run_script` → POST `tools/call` (`run_script kpi_set_target --company 3718 --scope team --mode auto`, timeout_seconds=120) |
| 10:42:39.74 | workforge | uvicorn logs `POST /mcp 200 OK` — **headers only**, SSE body empty |
| **10:42:44.7** | client (invisible) | **httpx ReadTimeout on the POST SSE stream** (logged only at DEBUG) |
| 10:42:45.39 | workforge | Job `2d44cf8d1f40439291f7756e56ed7b6a` finishes: **failed**, exit 1, 5.57s — `UniqueViolation kpi_param_pkey (3718, 2026-09-28)`; engine + history records are correct; response event written to the abandoned stream and dropped |
| 10:46:17.71 | backend | Run budget cancels the hung call: `Function duration: 217.973785s` — **no** `Function ... succeeded.` / `Function failed.` line |
| — | DB | `chat_messages`: nothing after the 10:42:17 answer; session status stays `active` → UI spins forever |

Key forensic detail: in `agent_framework/_tools.py` (~line 740-777), success
logs `Function {name} succeeded.`, an exception logs `Function failed. Error: …`,
and the `finally` always logs `Function duration: …`. We saw **only** the
duration line → the awaiting task was **cancelled** (`CancelledError` bypasses
`except Exception`). 10:46:17.7 − 10:42:17.6 = **240.1s** — a 4-minute run
budget.

---

## 3. Root cause — four contributing layers

### 3.1 PRIMARY: agent-fleet builds the httpx client with no timeout

`yedda-agent-fleet/src/backend/fleet_platform/agent_builder.py:389`
(`_resolve_mcp_servers`):

```python
MCPStreamableHTTPTool(
    name=name,
    url=server["endpoint"],
    description=server.get("description") or None,
    tool_name_prefix=name,
    http_client=httpx.AsyncClient(headers=auth_headers or None),   # ← BUG
    header_provider=...,
)
```

`httpx.AsyncClient()` defaults to `httpx.Timeout(5.0)` — 5s read. The MCP SDK
expects SSE read timeouts of ~300s; its own factory
(`mcp/shared/_httpx_utils.py`) uses:

```python
MCP_DEFAULT_TIMEOUT = 30.0
MCP_DEFAULT_SSE_READ_TIMEOUT = 300.0   # SSE streams — 5 minutes
...
kwargs["timeout"] = httpx.Timeout(MCP_DEFAULT_TIMEOUT, read=MCP_DEFAULT_SSE_READ_TIMEOUT)
```

Because a caller-supplied `http_client` is used as-is, the SDK's 300s default
is **silently overridden** by 5s.

Note `fleet_platform/mcp_connection.py:390` (the probe/verify path) has the
same pattern — worth fixing there too, though it is not on the hot path.

### 3.2 The workforge server sends no bytes until the tool finishes

- MCP `tools/call` POST → server responds `200` with `Content-Type: text/event-stream`
  (see `mcp/server/streamable_http.py::_handle_post_request`, SSE branch).
- With resumability disabled (FastMCP default: **no event store**),
  `_mint_priming_event` returns `None` → **no initial body byte**.
- sse-starlette `EventSourceResponse` sends keepalive pings every **15s**
  (default) — first possible byte at t+15s, long after the 5s read timeout.
- The result event is emitted only when the tool coroutine returns (for
  `run_script`, when the subprocess finishes).

⇒ Any tool call with wall time in the window **(5s, 15s)** loses the race;
calls >15s survive only if a ping landed within the last 5s — i.e. most calls
>5s are effectively broken with a 5s read timeout.

### 3.3 The mcp SDK client silently drops the failed POST stream

`mcp/client/streamable_http.py`, `_handle_sse_response` (~line 400-435):

```python
try:
    event_source = EventSource(response)
    async for sse in event_source.aiter_sse():
        ...
        if is_complete:
            await response.aclose()
            return
except Exception as e:  # pragma: no cover
    logger.debug(f"SSE stream ended: {e}")        # ReadTimeout → invisible at INFO

if last_event_id is not None:                     # None (no event ids without event store)
    logger.info("SSE stream disconnected, reconnecting...")
    await self._handle_reconnection(...)          # NEVER REACHED
```

- The `ReadTimeout` is swallowed at DEBUG.
- Reconnection is gated on `last_event_id`, which stays `None` because the
  server never sent an event id (no event store).
- The pending JSON-RPC request future is **never failed and never resolved**.

This is an upstream bug worth reporting (a POST-stream exception with a
pending request should fail that request, or reconnect regardless of
`last_event_id`).

### 3.4 Nothing bounds the wait, and the cancellation is silent

- `agent_framework/_mcp.py:773-777`: `read_timeout_seconds = timedelta(seconds=self.request_timeout) if self.request_timeout else None`.
  agent-fleet does not set `request_timeout` → `None` → `session.call_tool()` can wait forever.
- agent-fleet's run budget cancels at 240s, but the CancelledError path writes
  **nothing** to `chat_messages` and leaves session status `active` →
  the user sees an eternally spinning tool call.

---

## 4. Evidence / reproduction (done 2026-09-28 ~11:50 UTC, production containers)

All repros ran from inside `agent-fleet-backend-1` with its own `mcp` SDK,
against live `workforge-workforge-1` (token read from DB, never printed).
Client construction mirrored production: `httpx.AsyncClient(headers=...)`
(default 5s timeouts) + `streamable_http_client(endpoint, http_client=...)`.

| # | Scenario | Result |
|---|---|---|
| 1 | Fresh session, fast-failing call (`run_script kpi_run --bogus`, exits in ~0.1s) | ✅ failure result delivered in 0.09s (`isError=False`, dict with status=failed) |
| 2 | Idle session 25s (GET reader dies after 2 reconnects: `GET stream max reconnection attempts (2) exceeded`), then `run_script kpi_set_target 3718 auto` | ✅ delivered in 3.36s (succeeded — **script already fixed** to last-wins upsert: "overwrote existing target (last-wins, Playbook §6)") |
| 3 | `history_detail` of the exact failed incident job (1,349-char payload with full traceback) after idle | ✅ delivered in 0.03s |
| 4 | (observed production) incident call, script runtime **5.5s** | ❌ never delivered; run hung 218s until the 240s budget killed it |

Every sub-5s call delivered; the one >5s call hung. The GET-stream
disconnect/reconnect cycle in the logs (every ~5s across sessions) is the
same missing-timeout symptom on the standalone GET stream.

Conclusion: the transport, the workforge engine, the failure-path history
recording, and even large traceback payloads are all fine. **The delivery
break is exactly the 5s read timeout vs. tool runtime race.**

---

## 5. Fixes

### 5.1 PRIMARY — agent-fleet (kills the bug; one line + optional bound)

`yedda-agent-fleet/src/backend/fleet_platform/agent_builder.py:389`:

```python
# BEFORE
http_client=httpx.AsyncClient(headers=auth_headers or None),

# AFTER — match the MCP SDK factory defaults (mcp/shared/_httpx_utils.py)
http_client=httpx.AsyncClient(
    headers=auth_headers or None,
    timeout=httpx.Timeout(30.0, read=300.0),
),
```

Also apply the same in `fleet_platform/mcp_connection.py:390` (probe path).

**And** bound the request so a lost response surfaces as a tool error instead
of a hang:

```python
MCPStreamableHTTPTool(
    ...,
    request_timeout=290,   # < read timeout; MAF threads this into
                           # ClientSession(read_timeout_seconds=...)
)
```

Note: with `read=300s` the sse-starlette 15s pings keep the stream alive
indefinitely, so 300s is safe for long jobs; jobs longer than 290s then fail
with a clean timeout error the LLM can see and react to.

### 5.2 SECONDARY — workforge hardening (defense in depth, optional)

1. **Shorter SSE ping** so slow readers survive: sse-starlette
   `EventSourceResponse(..., ping=<seconds>)`. FastMCP 4.0.8 wraps the app;
   if the kwarg isn't exposed, skip this — the client-side fix is sufficient.
2. **Consider enabling an event store** (resumability) when wiring FastMCP's
   streamable-HTTP app: with event ids set, the SDK client's reconnect branch
   (`last_event_id is not None`) becomes reachable and a dropped stream can be
   resumed via `Last-Event-ID` instead of losing the response.

### 5.3 OBSERVABILITY — agent-fleet (makes the next one debuggable)

- Persist run-budget cancellations / agent-run aborts as an `error` (or
  assistant) message in `chat_messages` and flip session status out of
  `active`. The user should see "run cancelled/timed out", not an infinite
  spinner.
- The 240s budget itself should be configurable per team/agent, and ideally
  per-tool-call deadlines should compose with it.

### 5.4 UPSTREAM — mcp python-sdk bug report

`mcp/client/streamable_http.py::_handle_sse_response`: a POST-stream
exception with an unresolved pending request should fail that request (or
attempt reconnection regardless of `last_event_id`). Today the future leaks.
Version observed: mcp 2.2.0 (backend) / fastmcp 4.0.8 (server).

---

## 6. Suggested test plan for the fix

1. Unit: assert the constructed `AsyncClient` has `timeout.read >= 300` and
   `request_timeout` is threaded into `MCPStreamableHTTPTool`.
2. Integration (against a real workforge): call `run_script` on a script that
   sleeps 8s (with the old client this hangs; with the fix it returns in ~8s).
3. Regression: fast (<1s) calls still deliver; large traceback payloads still
   deliver; GET-stream reconnect noise in logs should disappear.
4. Failure-path UX: a tool call that exceeds `request_timeout` produces a
   visible error message in the chat, and the session does not stay `active`
   forever after a run-budget cancellation.

---

## 7. Reference: exact code locations

| Layer | File | What |
|---|---|---|
| agent-fleet | `src/backend/fleet_platform/agent_builder.py:389` | `httpx.AsyncClient(headers=...)` without timeout (PRIMARY FIX) |
| agent-fleet | `src/backend/fleet_platform/mcp_connection.py:390` | same pattern in probe path |
| agent-fleet | `src/backend/fleet_platform/tool_registry.py:245-282` | `_resolve_mcp` — no `request_timeout` |
| MAF | `agent_framework/_mcp.py:773-777` | `read_timeout_seconds=None` when `request_timeout` unset |
| MAF | `agent_framework/_tools.py:~740-777` | success/failed/duration logging (duration-only = cancelled) |
| mcp SDK | `mcp/client/streamable_http.py:~400-435` | POST SSE exception swallowed at DEBUG; reconnect gated on `last_event_id` |
| mcp SDK | `mcp/shared/_httpx_utils.py` | correct factory defaults: `Timeout(30.0, read=300.0)` |
| mcp SDK | `mcp/server/streamable_http.py:558-727` | POST → SSE branch, no priming event without event store |
| workforge | `src/workforge/engine.py:230-343` | `_execute` — verified correct on failure (this is NOT the bug) |
| workforge | `src/workforge/server.py:51-66` | `run_script` sync tool contract |
| infra | EC2 agent-fleet `172.31.8.207` (VPN-only), compose: caddy + frontend + backend + postgres + workforge | incident environment |

**Already fixed elsewhere (context, not part of this report's ask):**
`kpi_set_target.py` duplicate-key trigger — deployed scripts now use
last-wins upsert ("overwrote existing target ... (last-wins, Playbook §6)",
verified live 2026-09-28 11:51 UTC).
