"""CLI entry point: starts the WorkForge MCP server on stdio.

Also hosts the guarded ``db-init`` bootstrap (``workforge db-init`` or the
``workforge-db-init`` script): creates the WorkForge history database — only
ever ``workforge`` / ``workforge_test`` — and ensures its schema. See
:mod:`workforge.history` for the safety rules.
"""

from __future__ import annotations

import argparse
import sys

from . import __version__


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="workforge",
        description=(
            "WorkForge MCP server — save Python scripts, run them sync/async, "
            "stream logs, fetch outputs. Speaks MCP over stdio; launch it from "
            "your MCP client (Claude Desktop, Cursor, ...)."
        ),
    )
    parser.add_argument("--version", action="version", version=f"workforge {__version__}")
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

    from .server import mcp

    # show_banner=False keeps stderr clean for MCP client logs
    mcp.run(transport="stdio", show_banner=False)


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
