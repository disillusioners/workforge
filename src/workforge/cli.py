"""CLI entry point: starts the WorkForge MCP server (stdio by default).

Also hosts the guarded ``db-init`` bootstrap (``workforge db-init`` or the
``workforge-db-init`` script): creates the WorkForge history database — only
ever ``workforge`` / ``workforge_test`` — and ensures its schema. See
:mod:`workforge.history` for the safety rules.

Transport flags (``--transport/--host/--port`` or the WORKFORGE_* env
equivalents) are resolved in :mod:`workforge.transport`.
"""

from __future__ import annotations

import argparse
import sys

from . import __version__
from .transport import (
    ENV_TRANSPORT,
    TRANSPORT_CHOICES,
    TransportConfigError,
    resolve_config,
    serve,
)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="workforge",
        description=(
            "WorkForge MCP server — save Python scripts, run them sync/async, "
            "stream logs, fetch outputs. Serves MCP over stdio by default; "
            "--transport streamable-http/sse exposes it to remote agents "
            "over HTTP (set WORKFORGE_AUTH_TOKEN for bearer auth)."
        ),
    )
    parser.add_argument("--version", action="version", version=f"workforge {__version__}")
    parser.add_argument(
        "--transport",
        choices=TRANSPORT_CHOICES,
        default=None,
        help=(
            "MCP transport: stdio (default), sse, or streamable-http. "
            f"Env: {ENV_TRANSPORT}."
        ),
    )
    parser.add_argument(
        "--host",
        default=None,
        help="Bind address for HTTP transports (default: 127.0.0.1).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="Port for HTTP transports (default: 8000).",
    )
    sub = parser.add_subparsers(dest="command")
    sub.add_parser(
        "db-init",
        help=(
            "Create the history database (guarded: only 'workforge' or "
            "'workforge_test') and ensure its schema. Requires "
            "WORKFORGE_DATABASE_URL."
        ),
    )
    args = parser.parse_args()

    if args.command == "db-init":
        raise SystemExit(_db_init_command())

    try:
        config = resolve_config(
            transport=args.transport, host=args.host, port=args.port
        )
    except TransportConfigError as exc:
        parser.error(str(exc))  # usage + message on stderr, exit code 2

    serve(config)


def _db_init_command() -> int:
    """Run the guarded history bootstrap; never raises, returns exit code."""
    try:
        from . import history

        result = history.db_init()
    except Exception as exc:
        sys.stderr.write(f"[workforge] db-init failed: {exc}\n")
        return 1
    state = "created" if result["created"] else "already existed"
    print(f"database '{result['database']}' {state}; schema ensured (job_runs)")
    return 0


def db_init_main() -> None:
    """Console-script alias so `workforge-db-init` works alongside
    `workforge db-init`."""
    raise SystemExit(_db_init_command())


if __name__ == "__main__":
    main()
