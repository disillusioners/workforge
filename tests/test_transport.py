"""Transport-layer tests: config resolution + real HTTP/SSE round-trips.

Servers are spawned as real subprocesses on ephemeral ports (bind-then-close)
with isolated WORKFORGE_HOME, then exercised with fastmcp clients over actual
HTTP — the same path a remote agent would take. No fixed ports, no CI races.
"""

from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

import pytest
from fastmcp import Client
from fastmcp.client.transports import SSETransport, StreamableHttpTransport

from workforge.cli import main as cli_main
from workforge.transport import (
    DEFAULT_HOST,
    DEFAULT_PORT,
    DEFAULT_TRANSPORT,
    TransportConfigError,
    is_loopback_host,
    note_stdio_auth_ignored,
    resolve_config,
    warn_unauthenticated_remote,
)

AUTH_TOKEN = "wftest-bearer-0123456789abcdef"
TEST_PG_URL = "postgresql:///workforge_test"  # guarded DB — see history.py

SCRIPT = "print('hello from transport test')\n"


# ---------------------------------------------------------------- helpers


def free_port() -> int:
    """Bind-then-close an ephemeral port (brief-blessed pattern)."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def base_env(home, **extra) -> dict[str, str]:
    """Hermetic env for the server subprocess: isolated home, no stray auth."""
    env = os.environ.copy()
    env["WORKFORGE_HOME"] = str(home)
    for name in (
        "WORKFORGE_AUTH_TOKEN",
        "WORKFORGE_DATABASE_URL",
        "WORKFORGE_TRANSPORT",
        "WORKFORGE_HOST",
        "WORKFORGE_PORT",
    ):
        env.pop(name, None)
    env.update({k: v for k, v in extra.items() if v is not None})
    return env


def spawn_server(home, transport, port, host="127.0.0.1", extra_env=None,
                 capture_stderr=False):
    """Start `workforge --transport ...` as a real subprocess."""
    env = base_env(home, **(extra_env or {}))
    return subprocess.Popen(
        [
            sys.executable,
            "-c",
            "from workforge.cli import main; main()",
            "--transport",
            transport,
            "--host",
            host,
            "--port",
            str(port),
        ],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE if capture_stderr else subprocess.DEVNULL,
    )


def wait_healthy(proc, port, deadline_s: float = 20.0) -> None:
    """Poll /health until the server answers or the process dies."""
    end = time.monotonic() + deadline_s
    url = f"http://127.0.0.1:{port}/health"
    while time.monotonic() < end:
        if proc.poll() is not None:
            stderr = ""
            if proc.stderr is not None:
                stderr = proc.stderr.read().decode(errors="replace")
            pytest.fail(f"server exited early rc={proc.returncode}\n{stderr}")
        try:
            with urllib.request.urlopen(url, timeout=1) as response:
                if response.status == 200:
                    return
        except Exception:
            time.sleep(0.15)
    pytest.fail(f"server did not become healthy within {deadline_s}s")


def stop_server(proc) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def get_status(url: str, headers: dict[str, str] | None = None) -> int | None:
    """HTTP status for a GET, or None on connection error."""
    request = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code


def http_code(method: str, url: str, headers=None):
    request = urllib.request.Request(url, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code


async def call(client: Client, tool: str, **kwargs):
    result = await client.call_tool(tool, kwargs)
    return result.data


async def wait_for_status(
    client: Client, job_id: str, wanted, deadline_s: float = 20.0
):
    wanted = set(wanted)
    end = time.monotonic() + deadline_s
    last = None
    while time.monotonic() < end:
        last = await call(client, "job_status", job_id=job_id)
        if last["status"] in wanted:
            return last
        await asyncio.sleep(0.25)
    pytest.fail(f"job {job_id} never reached {wanted}; last: {last}")


def run_async(coro):
    return asyncio.run(coro)


class Server:
    """A spawned server bound to its coordinates."""

    def __init__(self, proc, port, transport):
        self.proc = proc
        self.port = port
        self.transport = transport

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def mcp_url(self) -> str:
        return f"{self.base_url}/mcp"

    @property
    def sse_url(self) -> str:
        return f"{self.base_url}/sse"


@pytest.fixture(scope="module")
def http_server(tmp_path_factory):
    """Streamable-http server; PG history enabled only if workforge_test answers."""
    pg_url = TEST_PG_URL if _pg_available() else None
    home = tmp_path_factory.mktemp("wf-http-home")
    port = free_port()
    proc = spawn_server(
        home,
        "streamable-http",
        port,
        extra_env={"WORKFORGE_DATABASE_URL": pg_url},
    )
    try:
        wait_healthy(proc, port)
        yield Server(proc, port, "streamable-http")
    finally:
        stop_server(proc)


@pytest.fixture(scope="module")
def sse_server(tmp_path_factory):
    home = tmp_path_factory.mktemp("wf-sse-home")
    port = free_port()
    proc = spawn_server(home, "sse", port)
    try:
        wait_healthy(proc, port)
        yield Server(proc, port, "sse")
    finally:
        stop_server(proc)


@pytest.fixture(scope="module")
def auth_http_server(tmp_path_factory):
    home = tmp_path_factory.mktemp("wf-auth-home")
    port = free_port()
    proc = spawn_server(
        home,
        "streamable-http",
        port,
        extra_env={"WORKFORGE_AUTH_TOKEN": AUTH_TOKEN},
    )
    try:
        wait_healthy(proc, port)
        yield Server(proc, port, "streamable-http")
    finally:
        stop_server(proc)


@pytest.fixture(scope="module")
def auth_sse_server(tmp_path_factory):
    home = tmp_path_factory.mktemp("wf-auth-sse-home")
    port = free_port()
    proc = spawn_server(
        home, "sse", port, extra_env={"WORKFORGE_AUTH_TOKEN": AUTH_TOKEN}
    )
    try:
        wait_healthy(proc, port)
        yield Server(proc, port, "sse")
    finally:
        stop_server(proc)


_PG_STATE: bool | None = None


def _pg_available() -> bool:
    """Probe workforge_test (the guarded test DB only) once per run."""
    global _PG_STATE
    if _PG_STATE is None:
        try:
            import psycopg

            with psycopg.connect(TEST_PG_URL, connect_timeout=3):
                _PG_STATE = True
        except Exception:
            _PG_STATE = False
    return _PG_STATE


# ------------------------------------------------- config resolution


class TestConfigResolution:
    def test_defaults(self, monkeypatch):
        for name in (
            "WORKFORGE_TRANSPORT",
            "WORKFORGE_HOST",
            "WORKFORGE_PORT",
            "WORKFORGE_AUTH_TOKEN",
        ):
            monkeypatch.delenv(name, raising=False)
        config = resolve_config()
        assert config.transport == DEFAULT_TRANSPORT == "stdio"
        assert config.host == DEFAULT_HOST == "127.0.0.1"
        assert config.port == DEFAULT_PORT == 8000
        assert config.auth_token is None

    def test_env_fills_unset_flags(self, monkeypatch):
        monkeypatch.setenv("WORKFORGE_TRANSPORT", "sse")
        monkeypatch.setenv("WORKFORGE_HOST", "0.0.0.0")
        monkeypatch.setenv("WORKFORGE_PORT", "9443")
        monkeypatch.setenv("WORKFORGE_AUTH_TOKEN", "tok")
        config = resolve_config()
        assert (config.transport, config.host, config.port) == ("sse", "0.0.0.0", 9443)
        assert config.auth_token == "tok"

    def test_flags_beat_env(self, monkeypatch):
        monkeypatch.setenv("WORKFORGE_TRANSPORT", "sse")
        monkeypatch.setenv("WORKFORGE_HOST", "0.0.0.0")
        monkeypatch.setenv("WORKFORGE_PORT", "9000")
        config = resolve_config(
            transport="streamable-http", host="127.0.0.1", port=9100
        )
        assert (config.transport, config.host, config.port) == (
            "streamable-http",
            "127.0.0.1",
            9100,
        )

    def test_invalid_flag_is_clean_argparse_error(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", ["workforge", "--transport", "grpc"])
        with pytest.raises(SystemExit) as excinfo:
            cli_main()
        assert excinfo.value.code == 2
        err = capsys.readouterr().err
        assert "invalid choice" in err
        assert "grpc" in err
        assert "Traceback" not in err

    def test_invalid_env_transport_is_clean_error(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", ["workforge"])
        monkeypatch.setenv("WORKFORGE_TRANSPORT", "carrier-pigeon")
        with pytest.raises(SystemExit) as excinfo:
            cli_main()
        assert excinfo.value.code == 2
        err = capsys.readouterr().err
        assert "WORKFORGE_TRANSPORT" in err
        assert "carrier-pigeon" in err
        assert "Traceback" not in err

    def test_invalid_env_port_is_clean_error(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", ["workforge"])
        monkeypatch.setenv("WORKFORGE_PORT", "not-a-number")
        with pytest.raises(SystemExit) as excinfo:
            cli_main()
        assert excinfo.value.code == 2
        assert "WORKFORGE_PORT" in capsys.readouterr().err

    def test_out_of_range_port_rejected(self, monkeypatch):
        monkeypatch.delenv("WORKFORGE_PORT", raising=False)
        with pytest.raises(TransportConfigError):
            resolve_config(port=70000)

    def test_loopback_detection(self):
        assert is_loopback_host("127.0.0.1")
        assert is_loopback_host("127.9.9.9")
        assert is_loopback_host("localhost")
        assert is_loopback_host("::1")
        assert is_loopback_host("[::1]")
        assert not is_loopback_host("0.0.0.0")
        assert not is_loopback_host("::")
        assert not is_loopback_host("192.168.1.10")


# --------------------------------------------- streamable-http serving


class TestStreamableHttp:
    def test_health_endpoint_open(self, http_server):
        assert get_status(f"{http_server.base_url}/health") == 200

    def test_save_and_list_scripts_roundtrip(self, http_server):
        async def scenario():
            transport = StreamableHttpTransport(http_server.mcp_url)
            async with Client(transport) as client:
                saved = await call(
                    client,
                    "save_script",
                    name="http-save",
                    content=SCRIPT,
                    description="over real http",
                )
                assert saved["name"] == "http-save"
                names = {
                    entry["name"] for entry in await call(client, "list_scripts")
                }
                assert "http-save" in names

        run_async(scenario())

    def test_sync_run_roundtrip(self, http_server):
        async def scenario():
            transport = StreamableHttpTransport(http_server.mcp_url)
            async with Client(transport) as client:
                await call(client, "save_script", name="http-sync", content=SCRIPT)
                result = await call(
                    client, "run_script", name="http-sync", timeout_seconds=10
                )
                assert result["status"] == "succeeded"
                assert result["exit_code"] == 0
                assert "hello from transport test" in result["stdout"]

        run_async(scenario())

    def test_async_pipeline_roundtrip(self, http_server):
        async def scenario():
            transport = StreamableHttpTransport(http_server.mcp_url)
            async with Client(transport) as client:
                await call(client, "save_script", name="http-async", content=SCRIPT)
                submitted = await call(
                    client, "run_script_async", name="http-async"
                )
                job_id = submitted["job_id"]
                status = await wait_for_status(client, job_id, {"succeeded"})
                assert status["exit_code"] == 0
                output = await call(client, "get_output", job_id=job_id)
                assert "hello from transport test" in output["stdout"]

        run_async(scenario())

    @pytest.mark.skipif(
        not _pg_available(),
        reason="workforge_test Postgres not reachable; history round-trip skipped",
    )
    def test_history_roundtrip_over_http(self, http_server):
        async def scenario():
            transport = StreamableHttpTransport(http_server.mcp_url)
            async with Client(transport) as client:
                await call(client, "save_script", name="http-history", content=SCRIPT)
                submitted = await call(
                    client, "run_script_async", name="http-history"
                )
                await wait_for_status(client, submitted["job_id"], {"succeeded"})
                rows = await call(
                    client, "list_history", limit=10, script_name="http-history"
                )
                assert rows, "expected at least one history row"
                assert rows[0]["script_name"] == "http-history"

        run_async(scenario())


# --------------------------------------------------------- sse serving


class TestSse:
    def test_health_endpoint_open(self, sse_server):
        assert get_status(f"{sse_server.base_url}/health") == 200

    def test_save_and_sync_run_roundtrip(self, sse_server):
        async def scenario():
            transport = SSETransport(sse_server.sse_url)
            async with Client(transport) as client:
                saved = await call(
                    client, "save_script", name="sse-run", content=SCRIPT
                )
                assert saved["name"] == "sse-run"
                result = await call(
                    client, "run_script", name="sse-run", timeout_seconds=10
                )
                assert result["status"] == "succeeded"
                assert "hello from transport test" in result["stdout"]

        run_async(scenario())


# ---------------------------------------------------------------- auth


class TestAuth:
    def test_missing_token_rejected_http(self, auth_http_server):
        assert http_code("POST", auth_http_server.mcp_url) == 401

    def test_wrong_token_rejected_http(self, auth_http_server):
        assert (
            http_code(
                "POST",
                auth_http_server.mcp_url,
                headers={"Authorization": "Bearer wrong-token"},
            )
            == 401
        )

    def test_missing_token_rejected_sse_handshake(self, auth_sse_server):
        assert get_status(auth_sse_server.sse_url) == 401

    def test_wrong_token_rejected_sse_handshake(self, auth_sse_server):
        assert (
            get_status(
                auth_sse_server.sse_url,
                headers={"Authorization": "Bearer wrong-token"},
            )
            == 401
        )

    def test_correct_token_roundtrip_sse(self, auth_sse_server):
        async def scenario():
            transport = SSETransport(auth_sse_server.sse_url, auth=AUTH_TOKEN)
            async with Client(transport) as client:
                saved = await call(
                    client, "save_script", name="sse-auth-roundtrip", content=SCRIPT
                )
                assert saved["name"] == "sse-auth-roundtrip"

        run_async(scenario())

    def test_correct_token_roundtrip_http(self, auth_http_server):
        async def scenario():
            transport = StreamableHttpTransport(
                auth_http_server.mcp_url, auth=AUTH_TOKEN
            )
            async with Client(transport) as client:
                tools = await client.list_tools()
                names = {tool.name for tool in tools}
                assert {"save_script", "run_script", "job_status"} <= names
                saved = await call(
                    client, "save_script", name="auth-roundtrip", content=SCRIPT
                )
                assert saved["name"] == "auth-roundtrip"

        run_async(scenario())

    def test_health_stays_open_with_auth(self, auth_http_server):
        """HEALTHCHECK contract: /health is unauthenticated even with auth on."""
        assert get_status(f"{auth_http_server.base_url}/health") == 200


# ------------------------------------------------- warnings and notes


class TestWarningsAndNotes:
    def test_unauthenticated_remote_warning_unit(self, capsys):
        from workforge.transport import TransportConfig

        warn_unauthenticated_remote(
            TransportConfig(transport="streamable-http", host="0.0.0.0", port=8123)
        )
        err = capsys.readouterr().err
        assert "WARNING" in err
        assert "NON-localhost" in err
        assert "WORKFORGE_AUTH_TOKEN" in err
        assert err.count("\n") >= 5  # loud = multi-line

    def test_unauthenticated_remote_warning_from_subprocess(self, tmp_path):
        port = free_port()
        proc = spawn_server(
            tmp_path, "streamable-http", port, host="0.0.0.0", capture_stderr=True
        )
        try:
            wait_healthy(proc, port)
        finally:
            stop_server(proc)
        stderr = proc.stderr.read().decode(errors="replace")
        assert "WARNING" in stderr
        assert "NON-localhost" in stderr

    def test_stdio_auth_note(self, capsys):
        note_stdio_auth_ignored()
        err = capsys.readouterr().err
        assert "WORKFORGE_AUTH_TOKEN" in err
        assert "ignored" in err
