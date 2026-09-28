# Upstream Bug Report Draft — modelcontextprotocol/python-sdk

> **Status:** DRAFT for review. Do NOT post until reviewed by the project owner.
> **Source:** WorkForge incident 2026-09-28 (`docs/incident-2026-09-28-mcp-result-delivery-hang.md`).
> **Versions reproduced with:** `mcp==2.2.0` (client), `fastmcp==4.0.8` (server),
> streamable-HTTP transport, **resumability OFF** (no event store on the server).

---

## Title

`mcp/client/streamable_http.py::_handle_sse_response`: POST-stream exception silently drops pending JSON-RPC request; `session.call_tool()` hangs forever

---

## Summary

When the POST SSE response stream raises (e.g. `httpx.ReadTimeout` because the
tool runs longer than the client's read timeout), `_handle_sse_response`
swallows the exception at `DEBUG`, never reconnects (because reconnection is
gated on `last_event_id is not None`, which is always `None` without a
server-side event store), and never fails the pending JSON-RPC request
future. The awaiting `session.call_tool()` call hangs indefinitely.

This makes any MCP tool call whose wall time exceeds the client's HTTP read
timeout silently hang the agent loop with no error surfaced to the caller.

---

## Environment

| Component | Version |
|---|---|
| `mcp` (client SDK) | 2.2.0 |
| `fastmcp` (server SDK) | 4.0.8 |
| `httpx` (transport) | 0.27.x (whatever mcp 2.2.0 pulls in) |
| Transport | `streamable-http` |
| Resumability (server-side event store) | **OFF** (FastMCP default) |
| Python | 3.12 |

---

## Steps to reproduce

A self-contained repro using only `mcp` + `fastmcp` (no project infra).

**Server** (`bug_repro_server.py`):

```python
import asyncio
from fastmcp import FastMCP

mcp = FastMCP("repro")

@mcp.tool()
async def slow_tool() -> str:
    # 8 seconds > httpx default read timeout of 5s,
    # > no event store means no priming event on the POST stream.
    await asyncio.sleep(8)
    return "done"

if __name__ == "__main__":
    mcp.run(transport="streamable-http", host="127.0.0.1", port=8765)
```

**Client** (`bug_repro_client.py`):

```python
import asyncio
import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

async def main() -> None:
    # httpx.AsyncClient() defaults to httpx.Timeout(5.0) — 5s read timeout.
    # This mirrors the production caller that built the client without a
    # timeout and therefore inherited the 5s default.
    async with httpx.AsyncClient() as http_client:
        async with streamable_http_client(
            "http://127.0.0.1:8765/mcp", http_client=http_client
        ) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                # This call hangs forever:
                result = await asyncio.wait_for(
                    session.call_tool("slow_tool", {}),
                    timeout=20,
                )
                print(result)  # never reached

asyncio.run(main())
```

**Run:**

```bash
# terminal 1
python bug_repro_server.py

# terminal 2
python bug_repro_client.py
```

**Observed behaviour:**

1. Client `initialize` succeeds.
2. Client `tools/call slow_tool` POST opens.
3. Server holds the request for ~8s; POST SSE response carries **zero bytes**
   for the full 8s (no event store → no priming event; `sse_starlette`
   `ping` default = 15s > 5s read timeout).
4. At t+5s, `httpx.ReadTimeout` fires on the POST response stream.
5. In `mcp/client/streamable_http.py::_handle_sse_response`, the
   `except Exception` branch catches it and emits a single `DEBUG` log line.
6. `last_event_id is None` (no event ids without a server-side event store),
   so the reconnect branch is skipped.
7. `session.call_tool()` future is never resolved. The `asyncio.wait_for`
   eventually times out at 20s with `TimeoutError`; without it, the client
   process hangs indefinitely.

**Expected behaviour:** When the POST SSE stream fails before delivering a
response, the pending request future should resolve (with a JSON-RPC error
or a timeout error) so the caller can react. Reconnect-with-Last-Event-ID
should not be a precondition for failing the request.

---

## Observed log output

From the incident (production containers, `agent-fleet-backend-1` →
`workforge-workforge-1`, 2026-09-28 ~10:42 UTC):

```
# Server side (workforge) — POST /mcp returns 200, but SSE body is empty
# for 5.5 s because the tool script is still running:

2026-09-28 10:42:39.74 INFO  uvicorn.access  POST /mcp 200 OK   (no body bytes yet)

# Client side (agent-fleet) — DEBUG-only swallow of the read timeout:

DEBUG  mcp.client.streamable_http  SSE stream ended    # exc_info=ReadTimeout("timed out")

# No "SSE stream disconnected, reconnecting..." line — the reconnect branch
# is gated on `last_event_id is not None`, which is None.

# The future the LLM is awaiting is never resolved. The LLM call site hangs
# forever (well, until the agent-fleet 240 s run budget cancels the task —
# and even that cancellation is silently dropped; nothing is written to the
# chat and the user sees an eternal spinner).
```

The single `DEBUG` line is the entirety of the signal that anything went
wrong at the transport layer.

---

## Root cause

`mcp/client/streamable_http.py::_handle_sse_response` (≈ lines 452–500):

```python
try:
    event_source = EventSource(response)
    async for sse in event_source:
        if sse.id:
            last_event_id = sse.id
        ...
        if is_complete:
            await response.aclose()
            return  # normal completion, no reconnect needed
except Exception as e:
    logger.debug(f"SSE stream ended: {e}")        # ReadTimeout → invisible at INFO

if last_event_id is not None:                     # None (no event ids without event store)
    logger.info("SSE stream disconnected, reconnecting...")
    await self._handle_reconnection(...)          # NEVER REACHED
# else: future leaks. No _resolve_abandoned_request equivalent.
```

The two-part failure:

1. The exception is logged at `DEBUG`. Operators looking for the cause of a
   hung agent run never see it without raising the client log level.
2. Reconnection is gated on `last_event_id is not None`. Without a server-side
   event store the server never sends event ids, so `last_event_id` stays
   `None`, and the reconnect branch never runs.
3. The pending JSON-RPC request future is **never failed nor resolved**.
   The `await session.call_tool(...)` call site hangs.

This is the entire reason the user's tool call hangs. The combination of
"client read timeout < server tool runtime" is the trigger; the SDK's
failure to surface the error is the bug.

---

## Suggested fix

Any one of the following would resolve the symptom:

**Option A — fail pending requests on stream error (preferred):**

In `_handle_sse_response`, when the `except Exception` branch fires,
resolve the pending JSON-RPC request with a synthetic JSON-RPC error
(similar to `_resolve_abandoned_request` for the GET stream):

```python
except Exception as e:
    logger.warning(f"SSE stream ended without a response: {e!r}")
    # Synthesize a JSON-RPC error so the awaiter wakes up.
    await self._send_error_to_read_stream(
        request_id=original_request_id,
        code=CONNECTION_CLOSED,           # or a new STREAM_INTERRUPTED code
        message="SSE stream interrupted before response arrived",
    )
    return
```

**Option B — attempt reconnection regardless of `last_event_id`:**

Drop the `if last_event_id is not None:` gate. When a POST SSE stream dies
without delivering a response, retry the POST (the server is idempotent
for `tools/call` up to the handler's own idempotency, but at minimum the
*caller* should be told via timeout that the request failed).

**Option C — also raise the default log level:**

At minimum, change `logger.debug(...)` to `logger.warning(...)` so the
swallowed exception is visible at the default client log level.

---

## Impact

Any MCP client that does not set an explicit `httpx.Timeout(read=…)` on the
`AsyncClient` it passes to `streamable_http_client` will silently hang on
any `tools/call` whose wall time exceeds httpx's default 5 s read timeout.
This affects every caller that follows the simplest-possible SDK recipe:

```python
httpx.AsyncClient()  # 5 s default — easy to miss
```

The MCP SDK's own factory (`mcp/shared/_httpx_utils.py`) uses
`httpx.Timeout(30.0, read=300.0)`. Any caller that bypasses the factory
and constructs the client itself inherits the unsafe default — and there
is no warning emitted at construction time.

---

## Related

- Reproducer uses `sse_starlette.EventSourceResponse` `ping` default of 15s
  on the server side. Even if the client sets a longer read timeout, the
  15s keepalive is too long for many workloads. A complementary server-side
  ask is to expose the `ping=` kwarg (or a settings-level `sse_ping_interval`)
  in `mcp.server.streamable_http` and `fastmcp.server.http` so operators
  can shorten it to e.g. 5s.

---

## Severity

**High** — silent hang of any tool call > client read timeout, no error
surfaced, no log at default level. Any production caller that doesn't
explicitly construct `httpx.Timeout(read=…)` is affected.

---

## Workaround (for callers, until fixed upstream)

Always pass an explicit `httpx.Timeout(...)` to `httpx.AsyncClient(...)`:

```python
import httpx
from mcp.shared._httpx_utils import MCP_DEFAULT_TIMEOUT, MCP_DEFAULT_SSE_READ_TIMEOUT

http_client = httpx.AsyncClient(
    timeout=httpx.Timeout(MCP_DEFAULT_TIMEOUT, read=MCP_DEFAULT_SSE_READ_TIMEOUT),
)
```

…or wrap `session.call_tool(...)` in `asyncio.wait_for(..., timeout=...)` so
a lost response surfaces as `TimeoutError` instead of a hang.