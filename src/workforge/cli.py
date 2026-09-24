"""CLI entry point: starts the WorkForge MCP server on stdio."""

from __future__ import annotations

import argparse

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
    args = parser.parse_args()

    from .server import mcp

    # show_banner=False keeps stderr clean for MCP client logs
    mcp.run(transport="stdio", show_banner=False)


if __name__ == "__main__":
    main()
