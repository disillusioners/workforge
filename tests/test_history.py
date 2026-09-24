"""Phase 2 feature A — persistent run history (Postgres) integration tests.

Integration against the local Postgres ONLY via the database
``workforge_test`` (created through the guarded init path). Skips cleanly
when Postgres is unreachable — never touches any other database, per the
project's hard PG-safety constraint.

Conventions follow tests/test_smoke.py: fastmcp in-memory client, temp
WORKFORGE_HOME (conftest), structured ToolError assertions.
"""

from __future__ import annotations

import os
import time

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from workforge import engine, history, storage
from workforge.server import mcp

# Unix socket + peer auth (the default local install); no password anywhere.
TEST_DB = "workforge_test"
GOOD_URL = f"postgresql:///{TEST_DB}"
# Refused instantly (nothing listens): proves a PG outage never fails a job.
DEAD_URL = "postgresql://127.0.0.1:59999/workforge_test"

NOT_CONFIGURED = history.NOT_CONFIGURED_MSG

HELLO_SCRIPT = "print('hello history')\n"
SLOW_SCRIPT = "import time\nprint('started', flush=True)\ntime.sleep(0.5)\n"


def with_memory_client(client: Client, scenario) -> None:
    """Same helper shape as test_smoke.py."""

    import asyncio

    async def runner():
        async with client:
            await scenario()

    asyncio.run(runner())


async def call(client: Client, tool: str, **kwargs):
    result = await client.call_tool(tool, kwargs)
    return result.data


# ----------------------------------------------------------------- fixtures


@pytest.fixture(scope="module")
def pg_ready():
    """Probe PG with plain local defaults; guarded-init workforge_test.

    Skips the whole module when Postgres is unreachable — history is an
    optional feature and CI may have no database at all.
    """
    try:
        import psycopg
    except ImportError:  # pragma: no cover - deps are declared, belt & braces
        pytest.skip("psycopg not installed; run history tests skipped")
    try:
        with psycopg.connect("dbname=postgres", connect_timeout=3) as conn:
            conn.execute("SELECT 1")
    except Exception as exc:
        pytest.skip(f"localhost Postgres unreachable via default auth ({exc}); "
                    "run history tests skipped")
    prev = os.environ.get("WORKFORGE_DATABASE_URL")
    os.environ["WORKFORGE_DATABASE_URL"] = GOOD_URL
    try:
        result = history.db_init()  # guarded: workforge_test only, then schema
    except Exception as exc:
        pytest.skip(f"guarded db-init of {TEST_DB} failed ({exc}); "
                    "run history tests skipped")
    finally:
        if prev is None:
            os.environ.pop("WORKFORGE_DATABASE_URL", None)
        else:
            os.environ["WORKFORGE_DATABASE_URL"] = prev
    assert result["database"] == TEST_DB
    yield


@pytest.fixture()
def wf_pg(pg_ready, monkeypatch):
    """Env set + empty job_runs table + clean pool state around each test."""
    monkeypatch.setenv("WORKFORGE_DATABASE_URL", GOOD_URL)
    history.reset_pools()
    import psycopg

    with psycopg.connect(GOOD_URL, autocommit=True) as conn:
        conn.execute("DELETE FROM job_runs")
    yield
    history.reset_pools()


def _rows():
    """All history rows via the public list API (newest first)."""
    return history.list_runs(limit=history.LIST_LIMIT_CAP)


# ------------------------------------------------------ write path / engine


def test_start_inserts_running_then_finalize_updates(wf_pg, wf_home):
    """INSERT on start (status=running) -> UPDATE to the final status."""
    storage.save_script("slowhello", SLOW_SCRIPT)
    res = engine.submit("slowhello", ["a", "b"], 10)
    job_id = res["job_id"]

    # Poll the row until the start write lands (status=running).
    row = None
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        rows = [r for r in _rows() if r["job_id"] == job_id]
        if rows and rows[0]["status"] == "running":
            row = rows[0]
            break
        time.sleep(0.05)
    assert row is not None, "no running row appeared after submit"
    assert row["script_name"] == "slowhello"
    assert row["exit_code"] is None

    # Wait for finalize -> row updated in place (same job_id, final status).
    while time.monotonic() < deadline:
        rows = [r for r in _rows() if r["job_id"] == job_id]
        if rows and rows[0]["status"] in ("succeeded", "failed"):
            row = rows[0]
            break
        time.sleep(0.05)
    assert row["status"] == "succeeded"
    assert row["exit_code"] == 0
    assert row["duration_ms"] is not None
    assert row["started_at"] is not None

    # Exactly ONE row for the job (upsert keyed on job_id).
    assert len([r for r in _rows() if r["job_id"] == job_id]) == 1


def test_finalize_failure_status_recorded(wf_pg, wf_home):
    storage.save_script("boomer", "import sys\nsys.exit(3)\n")
    res = engine.run_sync("boomer", [], 10)
    assert res["status"] == "failed"
    rows = [r for r in _rows() if r["job_id"] == res["job_id"]]
    assert len(rows) == 1
    assert rows[0]["status"] == "failed"
    assert rows[0]["exit_code"] == 3


def test_sync_run_upserts_final_row(wf_pg, wf_home):
    storage.save_script("hello", HELLO_SCRIPT)
    res = engine.run_sync("hello", ["x"], 10)
    assert res["status"] == "succeeded"
    (row,) = [r for r in _rows() if r["job_id"] == res["job_id"]]
    assert row["status"] == "succeeded"


# ------------------------------------------------------- list_history tool


def test_list_history_filters_limit_order_and_cap(wf_pg, wf_home, client):
    storage.save_script("alpha", HELLO_SCRIPT)
    storage.save_script("beta", HELLO_SCRIPT)
    engine.run_sync("alpha", [], 10)
    engine.run_sync("beta", [], 10)
    engine.run_sync("alpha", [], 10)
    storage.save_script("failer", "import sys\nsys.exit(1)\n")
    failed = engine.run_sync("failer", [], 10)

    async def scenario():
        # Newest first: last run first.
        rows = await call(client, "list_history")
        assert len(rows) == 4
        assert rows[0]["job_id"] == failed["job_id"]

        # script_name filter.
        rows = await call(client, "list_history", script_name="alpha")
        assert len(rows) == 2
        assert all(r["script_name"] == "alpha" for r in rows)

        # status filter.
        rows = await call(client, "list_history", status="failed")
        assert len(rows) == 1
        assert rows[0]["job_id"] == failed["job_id"]

        # limit: takes the NEWEST N.
        rows = await call(client, "list_history", limit=1)
        assert len(rows) == 1
        assert rows[0]["job_id"] == failed["job_id"]

        # Row shape.
        row = rows[0]
        assert set(row.keys()) == {
            "job_id", "script_name", "status", "exit_code",
            "started_at", "duration_ms",
        }

        # Validation: cap and invalid values -> ToolError.
        for bad in (0, -1, 201, 1000):
            with pytest.raises(ToolError):
                await call(client, "list_history", limit=bad)

    with_memory_client(client, scenario)


def test_list_history_row_shape_fields_populated(wf_pg, wf_home):
    """Direct history API shape (the tool delegates to it)."""
    storage.save_script("hello", HELLO_SCRIPT)
    res = engine.run_sync("hello", [], 10)
    (row,) = [r for r in _rows() if r["job_id"] == res["job_id"]]
    assert isinstance(row["duration_ms"], int)
    assert "T" in row["started_at"]  # ISO timestamp


def test_list_history_validation_direct(wf_pg):
    for bad in (0, -5, 201, "50", 3.5, None, True):
        with pytest.raises(ValueError):
            history.list_runs(limit=bad)
    with pytest.raises(ValueError):
        history.list_runs(status="bogus")
    with pytest.raises(ValueError):
        history.list_runs(script_name="")


# ---------------------------------------------------- history_detail tool


def test_history_detail_input_and_output_blocks(wf_pg, wf_home, client):
    storage.save_script("detailled", HELLO_SCRIPT)
    res = engine.run_sync("detailled", ["one", "two"], 7)

    async def scenario():
        detail = await call(client, "history_detail", job_id=res["job_id"])
        assert detail["job_id"] == res["job_id"]
        assert detail["input"] == {
            "script_name": "detailled",
            "args": ["one", "two"],
            "timeout_seconds": 7,
        }
        out = detail["output"]
        assert out["status"] == "succeeded"
        assert out["exit_code"] == 0
        assert out["stdout"] == "hello history\n"
        assert out["stderr"] == ""
        assert isinstance(out["duration_ms"], int)
        assert out["started_at"] and out["finished_at"]

        # Unknown id -> structured ToolError.
        with pytest.raises(ToolError, match="unknown job"):
            await call(client, "history_detail", job_id="f" * 32)

        # Malformed id -> structured ToolError (same validator as job_status).
        with pytest.raises(ToolError):
            await call(client, "history_detail", job_id="../escape")

    with_memory_client(client, scenario)


# ------------------------------------------- env unset / PG-down resilience


def test_env_unset_tools_raise_exact_message(pg_ready, wf_home, monkeypatch,
                                              client):
    monkeypatch.delenv("WORKFORGE_DATABASE_URL", raising=False)
    storage.save_script("hello", HELLO_SCRIPT)

    async def scenario():
        with pytest.raises(ToolError) as exc_info:
            await call(client, "list_history")
        assert str(exc_info.value) == NOT_CONFIGURED
        with pytest.raises(ToolError) as exc_info:
            await call(client, "history_detail", job_id="a" * 32)
        assert str(exc_info.value) == NOT_CONFIGURED

    with_memory_client(client, scenario)


def test_env_unset_engine_never_touches_pg_and_meta_stays_clean(
    pg_ready, wf_home, monkeypatch
):
    """HARD RULE: env unset -> zero PG contact, no history_write_error.

    record_job is booby-trapped: if the engine even TRIES a history write
    with the env unset, this test fails loudly.
    """

    def _must_not_be_called(meta):
        raise AssertionError("engine attempted a history write with env unset")

    monkeypatch.setattr(history, "record_job", _must_not_be_called)
    monkeypatch.delenv("WORKFORGE_DATABASE_URL", raising=False)
    storage.save_script("hello", HELLO_SCRIPT)
    res = engine.run_sync("hello", [], 10)
    assert res["status"] == "succeeded"
    meta = storage.read_job_meta(res["job_id"])
    assert "history_write_error" not in meta


def test_pg_down_job_still_succeeds_with_history_write_error(
    wf_pg, wf_home, monkeypatch
):
    """HARD RULE: a PG write failure NEVER fails the job.

    DEAD_URL points at a port nothing listens on; run_sync must still return
    a normal success and the failure must land in the persisted job meta.
    """
    monkeypatch.setenv("WORKFORGE_DATABASE_URL", DEAD_URL)
    storage.save_script("hello", HELLO_SCRIPT)
    res = engine.run_sync("hello", [], 30)
    assert res["status"] == "succeeded"
    assert res["exit_code"] == 0
    # The failure is persisted on disk, not just in the returned dict.
    meta = storage.read_job_meta(res["job_id"])
    assert "history_write_error" in meta


# ------------------------------------------------------------------ db-init


def test_db_init_refuses_any_database_outside_allowlist(pg_ready, monkeypatch):
    monkeypatch.setenv(
        "WORKFORGE_DATABASE_URL", "postgresql:///totally_not_workforge"
    )
    with pytest.raises(ValueError, match="refuses database"):
        history.db_init()


def test_db_init_idempotent_and_guards(pg_ready, wf_pg):
    result = history.db_init()  # created earlier by the pg_ready fixture
    assert result == {"database": TEST_DB, "created": False, "schema": "ok"}
