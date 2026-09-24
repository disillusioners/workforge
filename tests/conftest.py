"""Shared fixtures: every test points WORKFORGE_HOME at a temp directory."""

from __future__ import annotations

import pytest
from fastmcp import Client

from workforge.server import mcp


@pytest.fixture()
def wf_home(tmp_path, monkeypatch):
    """Isolated WorkForge home; never touches ~/.workforge."""
    home = tmp_path / "wfhome"
    monkeypatch.setenv("WORKFORGE_HOME", str(home))
    return home


@pytest.fixture()
def client(wf_home):
    """fastmcp in-memory client bound to the WorkForge server."""
    return Client(mcp)
