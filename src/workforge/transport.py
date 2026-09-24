"""Transport layer: config resolution, bearer auth, health route, serve loop.

Wraps :mod:`workforge.server` without touching it — the 9 tool contracts are
byte-identical across transports. stdio remains the default so existing MCP
client configs observe zero change.

Config precedence: CLI flag > environment variable > built-in default.

+---------------------+------------------------------+------------------+
| Setting             | Env                          | Default          |
+---------------------+------------------------------+------------------+
| transport           | WORKFORGE_TRANSPORT          | stdio            |
| host                | WORKFORGE_HOST               | 127.0.0.1        |
| port                | WORKFORGE_PORT               | 8000             |
| bearer token (auth) | WORKFORGE_AUTH_TOKEN         | unset (no auth)  |
| unauth opt-in       | WORKFORGE_ALLOW_UNAUTHENTICATED | unset (refuse) |
+---------------------+------------------------------+------------------+

Auth: when ``WORKFORGE_AUTH_TOKEN`` is set and an HTTP transport is selected,
every request to the MCP endpoints must carry ``Authorization: Bearer <token>``
(fastmcp's ``TokenVerifier`` hook; comparison is constant-time). The unauthen-
ticated ``/health`` route stays open so a container HEALTHCHECK can probe it.

DNS-rebinding protection: every HTTP bind installs fastmcp's
``HostOriginGuardMiddleware`` (mode ``"auto"``) so a browser-side page that
tries to reach the MCP endpoint with a spoofed ``Host`` header is rejected
with HTTP 421 — closing the loopback-DNS-rebinding gap even when the operator
trusts the loopback interface. Streamable-HTTP enables it via
``host_origin_protection="auto"``; SSE composes the middleware explicitly
because ``create_sse_app`` ignores that kwarg in fastmcp 4.0.8.

Remote-auth gate: a non-loopback HTTP/SSE bind with NO token refuses to start
(silent RCE if exposed publicly) — the operator must set
``WORKFORGE_AUTH_TOKEN`` or explicitly opt in with
``WORKFORGE_ALLOW_UNAUTHENTICATED=1`` (which still prints the loud warning).
stdio is never gated (no HTTP surface), loopback binds remain allowed without
a token (the DNS-rebinding middleware covers that threat).
"""

from __future__ import annotations

import os
import secrets
import string
import sys
from dataclasses import dataclass, field
from ipaddress import ip_address

from fastmcp.server.auth import AccessToken, TokenVerifier

TRANSPORT_CHOICES = ("stdio", "sse", "streamable-http")
DEFAULT_TRANSPORT = "stdio"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000  # fastmcp's native default; documented in README
HEALTH_PATH = "/health"

ENV_TRANSPORT = "WORKFORGE_TRANSPORT"
ENV_HOST = "WORKFORGE_HOST"
ENV_PORT = "WORKFORGE_PORT"
ENV_AUTH_TOKEN = "WORKFORGE_AUTH_TOKEN"
ENV_ALLOW_UNAUTHENTICATED = "WORKFORGE_ALLOW_UNAUTHENTICATED"


class TransportConfigError(ValueError):
    """Invalid transport configuration; message is user-facing."""


# Conservative allowlist of control chars / whitespace that should never appear
# in a bind host: empty, all-whitespace, embedded spaces, ASCII control chars
# (incl. DEL). Legitimate values include hostnames, IPv4, and bracketed IPv6
# (e.g. ``[::1]``); brackets themselves pass these checks.
_INVALID_HOST_CHARS = frozenset(string.whitespace) | frozenset(
    chr(c) for c in range(0x20)  # C0 control chars
) | frozenset("\x7f")  # DEL


@dataclass(frozen=True)
class TransportConfig:
    """Fully-resolved server settings (flags already beat env vars)."""

    transport: str = DEFAULT_TRANSPORT
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    # repr=False keeps the bearer token out of tracebacks, debug logs, and any
    # place a TransportConfig might be stringified.
    auth_token: str | None = field(default=None, repr=False)


def _env(name: str) -> str | None:
    value = os.environ.get(name)
    value = value.strip() if value else value
    return value or None


def _validate_host(name: str, host: str) -> str:
    """Reject empty / whitespace / control-char hosts at resolve time.

    Conservative on purpose: we only check for syntactic red flags that would
    make uvicorn blow up at bind time with a traceback. Valid forms
    (hostnames, IPv4, bracketed IPv6) all pass. Empty / whitespace / control
    chars are caught early so the CLI produces a clean exit-2 config error
    instead of a uvicorn traceback.
    """
    if not host:
        raise TransportConfigError(
            f"invalid {name} {host!r}: must not be empty"
        )
    if not host.strip():
        raise TransportConfigError(
            f"invalid {name} {host!r}: must not be whitespace-only"
        )
    if any(c in _INVALID_HOST_CHARS for c in host):
        raise TransportConfigError(
            f"invalid {name} {host!r}: contains whitespace or control characters"
        )
    return host


def _validate_token(token: str | None) -> str | None:
    """Reject tokens that can't be encoded to UTF-8 at resolve time.

    Surrogate codes (e.g. ``\\udcff``) are well-formed ``str`` but blow up at
    ``.encode("utf-8")`` time — which is exactly where fastmcp compares the
    incoming ``Authorization: Bearer`` bytes. Valid UTF-8 with non-ASCII
    characters (e.g. ``"héllo"``) is fine and passes through.
    """
    if token is None:
        return None
    try:
        token.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise TransportConfigError(
            f"invalid {ENV_AUTH_TOKEN}: cannot encode as UTF-8 ({exc.reason}); "
            "use ASCII or another valid UTF-8 string"
        ) from None
    return token


def resolve_config(
    *,
    transport: str | None = None,
    host: str | None = None,
    port: int | None = None,
) -> TransportConfig:
    """Resolve transport/host/port/auth from flags, env, then defaults.

    Raises :class:`TransportConfigError` with a clear, actionable message for
    invalid values — the CLI turns that into ``parser.error`` (usage + message,
    exit code 2), never a traceback.
    """
    resolved_transport = transport or _env(ENV_TRANSPORT) or DEFAULT_TRANSPORT
    if resolved_transport not in TRANSPORT_CHOICES:
        raise TransportConfigError(
            f"invalid {ENV_TRANSPORT} {resolved_transport!r}: expected one of "
            f"{', '.join(TRANSPORT_CHOICES)}"
        )

    # ``None`` = "not provided" (fall through to env/default); anything else
    # was explicitly supplied by the operator and must validate.
    if host is not None:
        resolved_host = _validate_host(ENV_HOST, host)
    else:
        env_host = _env(ENV_HOST)
        resolved_host = (
            _validate_host(ENV_HOST, env_host) if env_host is not None else DEFAULT_HOST
        )

    resolved_port = port
    if resolved_port is None:
        raw_port = _env(ENV_PORT)
        if raw_port is not None:
            try:
                resolved_port = int(raw_port)
            except ValueError:
                raise TransportConfigError(
                    f"invalid {ENV_PORT} {raw_port!r}: must be an integer"
                ) from None
    if resolved_port is None:
        resolved_port = DEFAULT_PORT
    if not 0 <= resolved_port <= 65535:
        raise TransportConfigError(
            f"invalid port {resolved_port}: must be 0..65535"
        )

    return TransportConfig(
        transport=resolved_transport,
        host=resolved_host,
        port=resolved_port,
        auth_token=_validate_token(_env(ENV_AUTH_TOKEN)),
    )


class StaticBearerVerifier(TokenVerifier):
    """Bearer-token check for ``Authorization: Bearer <token>``.

    fastmcp 4.0.8's built-in ``StaticTokenVerifier`` does a plain dict lookup,
    which is not constant-time; this verifier uses ``secrets.compare_digest``
    so token checks do not leak timing information. Registered through
    fastmcp's ``auth=`` hook — the SDK middleware rejects missing headers and
    non-matching tokens with ``401`` + ``WWW-Authenticate``.
    """

    def __init__(self, token: str) -> None:
        super().__init__()
        self._expected = token.encode("utf-8")

    async def verify_token(self, token: str) -> AccessToken | None:
        supplied = token.encode("utf-8")
        if secrets.compare_digest(supplied, self._expected):
            return AccessToken(token=token, client_id="workforge", scopes=[])
        return None


def is_loopback_host(host: str) -> bool:
    """True for hosts that only expose the server to this machine.

    Uses :mod:`ipaddress` so unparseable strings (e.g. ``"127.0.0.1.evil.com"``)
    fall through to ``False`` instead of being accepted because of a naive prefix
    match.
    """
    normalized = host.strip("[]").lower()
    if normalized == "localhost":
        return True
    try:
        return ip_address(normalized).is_loopback
    except ValueError:
        return False


def warn_unauthenticated_remote(config: TransportConfig) -> None:
    """Loud stderr banner for no-token non-loopback binds; still starts."""
    lines = [
        "=" * 68,
        "⚠️  WARNING: WorkForge is binding to a NON-localhost address WITHOUT auth.",
        "",
        f"    listening on: http://{config.host}:{config.port}/mcp",
        "    Scripts run with the FULL privileges of this user — there is NO",
        "    sandbox. Anyone who can reach this port can execute arbitrary",
        "    code on this machine.",
        "",
        f"    Set {ENV_AUTH_TOKEN} and connect with an Authorization:",
        "    Bearer header to require a token.",
        "=" * 68,
    ]
    sys.stderr.write("\n".join(lines) + "\n\n")
    sys.stderr.flush()


def note_stdio_auth_ignored() -> None:
    """One-line stderr note when a token is set but stdio ignores auth."""
    sys.stderr.write(
        f"[workforge] note: {ENV_AUTH_TOKEN} is set but the stdio transport "
        "does not use HTTP auth; token ignored.\n"
    )
    sys.stderr.flush()


def register_health_route(mcp) -> None:
    """Add an unauthenticated GET /health route (container HEALTHCHECK).

    fastmcp appends ``custom_route`` registrations OUTSIDE its auth
    middleware, so the probe works with or without ``WORKFORGE_AUTH_TOKEN``.
    stdio transport never serves HTTP routes, so this is inert there.

    The body intentionally returns ONLY ``{"status": "ok"}`` — no version
    fingerprint, so the probe output is stable across releases and can't
    leak build metadata to anyone who can hit the route.
    """
    from starlette.responses import JSONResponse

    @mcp.custom_route(HEALTH_PATH, methods=["GET"])
    async def health(request) -> JSONResponse:  # pragma: no cover - trivial
        return JSONResponse({"status": "ok"})


def enforce_remote_auth_gate(config: TransportConfig) -> None:
    """Refuse to start an HTTP/SSE bind that's both non-loopback and unauthenticated.

    stdio is never gated (no HTTP surface). Loopback HTTP/SSE binds remain
    allowed without a token — the DNS-rebinding middleware covers the
    browser-side spoof risk, and the operator is presumed to know who can
    reach 127.0.0.1. The gate is about *public* binds (silent RCE): a token
    set → starts clean; ``WORKFORGE_ALLOW_UNAUTHENTICATED=1`` → explicit
    opt-in + the loud warning still fires. Refusal raises ``SystemExit(2)``
    so the CLI exits cleanly without a traceback.
    """
    if config.transport == "stdio":
        return
    if config.auth_token:
        return
    if is_loopback_host(config.host):
        return
    if os.environ.get(ENV_ALLOW_UNAUTHENTICATED) == "1":
        return
    sys.stderr.write(
        f"\n[workforge] refusing to start: binding {config.host}:{config.port}"
        f" with no auth token.\n"
        f"  WorkForge scripts run unsandboxed with this user's privileges —\n"
        f"  a public HTTP/SSE endpoint is a silent RCE.\n"
        f"\n  Fix: set {ENV_AUTH_TOKEN} and connect with\n"
        f"  Authorization: Bearer <token>.\n"
        f"\n  Escape hatch (isolated / trusted networks only):\n"
        f"  set {ENV_ALLOW_UNAUTHENTICATED}=1 to opt in to the loud warning + start.\n\n"
    )
    sys.stderr.flush()
    raise SystemExit(2)


def serve(config: TransportConfig, *, show_banner: bool = False) -> None:
    """Start the server with the resolved config. Blocks until interrupted."""
    from .server import mcp

    if config.transport == "stdio":
        # stdio has no HTTP surface, so bearer auth cannot apply. We surface
        # a one-line note instead of failing silently or erroring out.
        if config.auth_token:
            note_stdio_auth_ignored()
        # show_banner=False keeps stderr clean for MCP client logs
        mcp.run(transport="stdio", show_banner=show_banner)
        return

    # gate first: non-loopback + no token → refuse (SystemExit 2)
    enforce_remote_auth_gate(config)

    # order matters: /health route + mcp.auth are both read once at mcp.run() app-build time
    register_health_route(mcp)

    if config.auth_token:
        mcp.auth = StaticBearerVerifier(config.auth_token)
    elif not is_loopback_host(config.host):
        warn_unauthenticated_remote(config)

    # DNS-rebinding protection for both transports. fastmcp 4.0.8's
    # create_streamable_http_app honors `host_origin_protection="auto"` (it
    # wraps HostOriginGuardMiddleware internally, see fastmcp/server/http.py
    # lines 652-661). create_sse_app IGNORES that kwarg in 4.0.8 (its
    # signature has no host_origin_protection parameter), so for SSE we
    # compose the middleware explicitly via the middleware= param (which
    # create_sse_app does forward to the Starlette app builder at lines
    # 522-523 and 531-536).
    run_kwargs: dict = {
        "transport": config.transport,
        "host": config.host,
        "port": config.port,
        "show_banner": show_banner,
    }
    if config.transport == "streamable-http":
        run_kwargs["host_origin_protection"] = "auto"
    elif config.transport == "sse":
        from fastmcp.server.http import HostOriginGuardMiddleware
        from starlette.middleware import Middleware

        run_kwargs["middleware"] = [
            Middleware(HostOriginGuardMiddleware, mode="auto")
        ]
    mcp.run(**run_kwargs)
