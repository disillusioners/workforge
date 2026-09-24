"""Transport layer: config resolution, bearer auth, health route, serve loop.

Wraps :mod:`workforge.server` without touching it — the 9 tool contracts are
byte-identical across transports. stdio remains the default so existing MCP
client configs observe zero change.

Config precedence: CLI flag > environment variable > built-in default.

+---------------------+----------------------+------------------+
| Setting             | Env                  | Default          |
+---------------------+----------------------+------------------+
| transport           | WORKFORGE_TRANSPORT  | stdio            |
| host                | WORKFORGE_HOST       | 127.0.0.1        |
| port                | WORKFORGE_PORT       | 8000             |
| bearer token (auth) | WORKFORGE_AUTH_TOKEN | unset (no auth)  |
+---------------------+----------------------+------------------+

Auth: when ``WORKFORGE_AUTH_TOKEN`` is set and an HTTP transport is selected,
every request to the MCP endpoints must carry ``Authorization: Bearer <token>``
(fastmcp's ``TokenVerifier`` hook; comparison is constant-time). The unauthen-
ticated ``/health`` route stays open so a container HEALTHCHECK can probe it.
"""

from __future__ import annotations

import os
import secrets
import sys
from dataclasses import dataclass

from fastmcp.server.auth import AccessToken, TokenVerifier

from . import __version__

TRANSPORT_CHOICES = ("stdio", "sse", "streamable-http")
DEFAULT_TRANSPORT = "stdio"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000  # fastmcp's native default; documented in README
HEALTH_PATH = "/health"

ENV_TRANSPORT = "WORKFORGE_TRANSPORT"
ENV_HOST = "WORKFORGE_HOST"
ENV_PORT = "WORKFORGE_PORT"
ENV_AUTH_TOKEN = "WORKFORGE_AUTH_TOKEN"


class TransportConfigError(ValueError):
    """Invalid transport configuration; message is user-facing."""


@dataclass(frozen=True)
class TransportConfig:
    """Fully-resolved server settings (flags already beat env vars)."""

    transport: str = DEFAULT_TRANSPORT
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    auth_token: str | None = None


def _env(name: str) -> str | None:
    value = os.environ.get(name)
    value = value.strip() if value else value
    return value or None


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

    resolved_host = host or _env(ENV_HOST) or DEFAULT_HOST

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
        auth_token=_env(ENV_AUTH_TOKEN),
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
    """True for hosts that only expose the server to this machine."""
    normalized = host.strip("[]").lower()
    return (
        normalized in {"localhost", "::1"} or normalized.startswith("127.")
    )


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
    """
    from starlette.responses import JSONResponse

    @mcp.custom_route(HEALTH_PATH, methods=["GET"])
    async def health(request) -> JSONResponse:  # pragma: no cover - trivial
        return JSONResponse({"status": "ok", "version": __version__})


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

    register_health_route(mcp)

    if config.auth_token:
        mcp.auth = StaticBearerVerifier(config.auth_token)
    elif not is_loopback_host(config.host):
        warn_unauthenticated_remote(config)

    mcp.run(
        transport=config.transport,
        host=config.host,
        port=config.port,
        show_banner=show_banner,
    )
