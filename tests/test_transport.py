"""Transport-layer tests: config resolution + real HTTP/SSE round-trips.

Servers are spawned as real subprocesses on ephemeral ports (bind-then-close)
with isolated WORKFORGE_HOME, then exercised with fastmcp clients over actual
HTTP — the same path a remote agent would take. No fixed ports, no CI races.
"""

from __future__ import annotations

import asyncio
import json
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
    ENV_ALLOW_UNAUTHENTICATED,
    TransportConfig,
    TransportConfigError,
    enforce_remote_auth_gate,
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
    """Poll /health until the server answers or the process dies.

    Bounded by ``deadline_s`` (default 20s). On early exit, captures
    stderr with a tight timeout — never blocks indefinitely.
    """
    end = time.monotonic() + deadline_s
    url = f"http://127.0.0.1:{port}/health"
    while time.monotonic() < end:
        if proc.poll() is not None:
            pytest.fail(
                f"server exited early rc={proc.returncode}\n{_read_stderr(proc)}"
            )
        try:
            with urllib.request.urlopen(url, timeout=1) as response:
                if response.status == 200:
                    return
        except Exception:
            time.sleep(0.15)
    pytest.fail(f"server did not become healthy within {deadline_s}s")


def _read_stderr(proc) -> str:
    """Bounded stderr read — never blocks more than ~3s.

    Plain ``proc.stderr.read()`` can hang indefinitely if the child is
    alive and idle, because POSIX ``subprocess`` does NOT spawn a
    background drainer thread (unlike the Windows path). This helper
    does a bounded wait-then-read in a worker thread so a misbehaving
    subprocess can't pin the test forever.
    """
    if proc.stderr is None:
        return ""
    import threading

    holder: list[str] = []

    def _drain() -> None:
        try:
            data = proc.stderr.read()
            holder.append(data.decode(errors="replace"))
        except Exception:
            holder.append("")

    th = threading.Thread(target=_drain, daemon=True)
    th.start()
    th.join(3.0)
    if th.is_alive():
        return "[stderr read timed out]"
    return holder[0] if holder else ""


def stop_server(proc) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def drain_stderr(proc) -> str:
    """Read stderr after the process exits — bounded.

    The POSIX ``subprocess`` module does NOT spawn a background drainer
    thread for stderr=PIPE (unlike the Windows path), so plain
    ``proc.stderr.read()`` on a still-alive child blocks forever waiting
    for data that may never come. After ``stop_server`` the child is
    dead and the kernel has closed the write-end of the pipe, so a
    bounded read returns EOF promptly. We still wrap it in a thread with
    a hard cap so a misbehaving child (e.g. forked worker holding the
    fd) cannot pin the test forever.
    """
    if proc.poll() is None:
        raise RuntimeError(
            "drain_stderr requires the process to have exited "
            "(call stop_server first)"
        )
    if proc.stderr is None:
        return ""
    import threading

    holder: list[str] = []

    def _drain() -> None:
        try:
            data = proc.stderr.read()
            holder.append(data.decode(errors="replace"))
        except Exception:
            holder.append("")

    th = threading.Thread(target=_drain, daemon=True)
    th.start()
    th.join(3.0)
    if th.is_alive():
        return "[stderr read timed out]"
    return holder[0] if holder else ""


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
        # defense-in-depth: prefix-matching hostnames must NOT be loopback
        assert not is_loopback_host("127.0.0.1.evil.com")
        # case-insensitive hostname allowlist (lowercased before compare)
        assert is_loopback_host("LOCALHOST")
        # hostnames that merely end in loopback substrings must NOT match
        assert not is_loopback_host("localhost.evil.com")


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
            tmp_path,
            "streamable-http",
            port,
            host="0.0.0.0",
            capture_stderr=True,
            extra_env={ENV_ALLOW_UNAUTHENTICATED: "1"},
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


# -------------------------------------- config validation (rider 2/3/4)


class TestConfigValidation:
    """Invalid host / token strings are caught at resolve time, not at bind."""

    def test_valid_hosts_pass(self):
        for good in (
            "127.0.0.1",
            "localhost",
            "0.0.0.0",
            "[::1]",
            "192.168.1.10",
            "host.example.com",
            "host-with-dash",
        ):
            config = resolve_config(host=good)
            assert config.host == good, good

    def test_invalid_host_empty_clean_error(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", ["workforge", "--host", ""])
        with pytest.raises(SystemExit) as excinfo:
            cli_main()
        assert excinfo.value.code == 2
        err = capsys.readouterr().err
        assert "WORKFORGE_HOST" in err
        assert "Traceback" not in err

    def test_invalid_host_whitespace_clean_error(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", ["workforge", "--host", "not a host"])
        with pytest.raises(SystemExit) as excinfo:
            cli_main()
        assert excinfo.value.code == 2
        err = capsys.readouterr().err
        assert "WORKFORGE_HOST" in err
        assert "Traceback" not in err

    def test_invalid_env_host_clean_error(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", ["workforge"])
        monkeypatch.setenv("WORKFORGE_HOST", "bad\tvalue")
        with pytest.raises(SystemExit) as excinfo:
            cli_main()
        assert excinfo.value.code == 2
        assert "WORKFORGE_HOST" in capsys.readouterr().err

    def test_invalid_utf8_token_clean_error(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", ["workforge"])
        # undecodable surrogate: valid str, blows up at .encode("utf-8")
        monkeypatch.setenv("WORKFORGE_AUTH_TOKEN", "\udcff")
        with pytest.raises(SystemExit) as excinfo:
            cli_main()
        assert excinfo.value.code == 2
        err = capsys.readouterr().err
        assert "WORKFORGE_AUTH_TOKEN" in err
        assert "UTF-8" in err
        assert "Traceback" not in err

    def test_non_ascii_but_valid_utf8_token_accepted(self, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["workforge"])
        monkeypatch.setenv("WORKFORGE_AUTH_TOKEN", "héllo-bear-🦀")  # valid UTF-8
        # resolve_config succeeds; serve() is never called from this test
        config = resolve_config()
        assert config.auth_token == "héllo-bear-🦀"

    def test_config_repr_omits_token(self):
        """repr=False keeps the bearer out of tracebacks / logs."""
        config = TransportConfig(transport="streamable-http", auth_token="supersecret")
        rendered = repr(config)
        assert "supersecret" not in rendered
        assert "auth_token" not in rendered  # dataclass repr hides repr=False fields
        # But the field is still accessible programmatically.
        assert config.auth_token == "supersecret"


# ------------------------------------- health body (rider 1)


class TestHealthBody:
    """/health returns exactly {"status": "ok"} — no version fingerprint."""

    def test_health_body_streamable_http(self, http_server):
        with urllib.request.urlopen(
            f"{http_server.base_url}/health", timeout=5
        ) as response:
            body = response.read().decode()
        assert json.loads(body) == {"status": "ok"}

    def test_health_body_sse(self, sse_server):
        with urllib.request.urlopen(
            f"{sse_server.base_url}/health", timeout=5
        ) as response:
            body = response.read().decode()
        assert json.loads(body) == {"status": "ok"}


# ------------------------------------- DNS-rebinding regression (blocker 1)


def _get_with_host(url: str, host_header: str) -> int | None:
    """HTTP GET with an explicit Host header (overrides urllib's default).

    Returns the status code, or None on connection error.
    """
    request = urllib.request.Request(url, headers={"Host": host_header})
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code


class TestDnsRebindingHttp:
    """Loopback-bound streamable-http rejects requests with a foreign Host.

    Without this guard, a browser-side page can fetch
    ``http://localhost:8000/mcp`` and DNS-rebind ``127.0.0.1` to a hostile
    origin — so loopback-only is NOT enough; Host validation closes the gap.
    """

    def test_correct_host_passes_http(self, http_server):
        # No explicit Host override → urllib sends Host: 127.0.0.1:port → allowed
        assert get_status(f"{http_server.base_url}/health") == 200

    def test_spoofed_host_rejected_http(self, http_server):
        # spoofed Host header — a browser-side DNS-rebind would send this
        assert (
            _get_with_host(
                f"{http_server.base_url}/health", "evil.example"
            )
            == 421
        )


class TestDnsRebindingSse:
    """Same regression as TestDnsRebindingHttp but for SSE — wires differently."""

    def test_correct_host_passes_sse(self, sse_server):
        assert get_status(f"{sse_server.base_url}/health") == 200

    def test_spoofed_host_rejected_sse(self, sse_server):
        assert (
            _get_with_host(
                f"{sse_server.base_url}/health", "evil.example"
            )
            == 421
        )


# ------------------------------------- auth edge cases (rider 5)


class TestAuthEdges:
    """Auth middleware must reject every malformed Authorization header."""

    def test_basic_auth_rejected_http(self, auth_http_server):
        assert (
            http_code(
                "POST",
                auth_http_server.mcp_url,
                headers={"Authorization": "Basic x"},
            )
            == 401
        )

    def test_empty_bearer_rejected_http(self, auth_http_server):
        assert (
            http_code(
                "POST",
                auth_http_server.mcp_url,
                headers={"Authorization": "Bearer "},
            )
            == 401
        )

    def test_lowercase_bearer_accepted_with_valid_token_http(
        self, auth_http_server
    ):
        """RFC 7235 says auth scheme is case-insensitive; fastmcp 4.0.8
        accepts ``bearer`` (the upstream ``BearerAuthBackend`` lower-cases
        the header before the startswith check). Documented in README.
        """
        # POST with a lowercase scheme; fastmcp's auth middleware
        # normalizes the scheme, so anything other than 401 proves the
        # lowercase form was accepted.
        code = http_code(
            "POST",
            auth_http_server.mcp_url,
            headers={
                "Authorization": f"bearer {AUTH_TOKEN}",
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
            },
        )
        assert code is not None, "no response (connection error)"
        assert code != 401, (
            f"lowercase bearer was rejected: {code} "
            f"(RFC 7235: scheme is case-insensitive)"
        )

    def test_malformed_garbage_header_rejected_http(self, auth_http_server):
        assert (
            http_code(
                "POST",
                auth_http_server.mcp_url,
                headers={"Authorization": "this is not even close"},
            )
            == 401
        )

    def test_basic_auth_rejected_sse(self, auth_sse_server):
        assert (
            get_status(
                auth_sse_server.sse_url,
                headers={"Authorization": "Basic x"},
            )
            == 401
        )

    def test_empty_bearer_rejected_sse(self, auth_sse_server):
        assert (
            get_status(
                auth_sse_server.sse_url,
                headers={"Authorization": "Bearer "},
            )
            == 401
        )

    def test_malformed_garbage_header_rejected_sse(self, auth_sse_server):
        assert (
            get_status(
                auth_sse_server.sse_url,
                headers={"Authorization": "garbage"},
            )
            == 401
        )


# ------------------------------------- remote-auth gate (blocker 2)


class TestRemoteAuthGate:
    """Non-loopback HTTP/SSE bind with no token REFUSES to start."""

    def test_gate_unit_non_loopback_no_token_raises(self, monkeypatch):
        monkeypatch.delenv(ENV_ALLOW_UNAUTHENTICATED, raising=False)
        config = TransportConfig(
            transport="streamable-http", host="0.0.0.0", port=8123
        )
        with pytest.raises(SystemExit) as excinfo:
            enforce_remote_auth_gate(config)
        assert excinfo.value.code == 2

    def test_gate_unit_loopback_no_token_passes(self, monkeypatch):
        monkeypatch.delenv(ENV_ALLOW_UNAUTHENTICATED, raising=False)
        # Loopback is fine without a token; DNS-rebinding middleware covers it.
        enforce_remote_auth_gate(
            TransportConfig(transport="streamable-http", host="127.0.0.1")
        )
        enforce_remote_auth_gate(
            TransportConfig(transport="streamable-http", host="localhost")
        )

    def test_gate_unit_token_passes(self, monkeypatch):
        monkeypatch.delenv(ENV_ALLOW_UNAUTHENTICATED, raising=False)
        enforce_remote_auth_gate(
            TransportConfig(
                transport="streamable-http", host="0.0.0.0", auth_token="any"
            )
        )

    def test_gate_unit_allow_unauthenticated_passes(self, monkeypatch):
        monkeypatch.setenv(ENV_ALLOW_UNAUTHENTICATED, "1")
        enforce_remote_auth_gate(
            TransportConfig(transport="streamable-http", host="0.0.0.0")
        )

    def test_gate_unit_stdio_never_gated(self, monkeypatch):
        monkeypatch.delenv(ENV_ALLOW_UNAUTHENTICATED, raising=False)
        # stdio has no HTTP surface — gate is a no-op even with no token.
        enforce_remote_auth_gate(TransportConfig(transport="stdio"))

    def test_gate_unit_allow_unauthenticated_other_value_rejected(
        self, monkeypatch
    ):
        monkeypatch.setenv(ENV_ALLOW_UNAUTHENTICATED, "true")
        with pytest.raises(SystemExit):
            enforce_remote_auth_gate(
                TransportConfig(transport="streamable-http", host="0.0.0.0")
            )

    def test_gate_subprocess_no_token_refuses(self, tmp_path):
        port = free_port()
        proc = spawn_server(
            tmp_path,
            "streamable-http",
            port,
            host="0.0.0.0",
            capture_stderr=True,
        )
        # Server should refuse and exit; give it a moment, then verify.
        try:
            rc = proc.wait(timeout=10)
        finally:
            if proc.returncode is None:
                stop_server(proc)
        assert rc != 0, "process should have refused to start"
        stderr = proc.stderr.read().decode(errors="replace")
        assert "refusing to start" in stderr
        assert "WORKFORGE_AUTH_TOKEN" in stderr
        assert ENV_ALLOW_UNAUTHENTICATED in stderr

    def test_gate_subprocess_allow_unauthenticated_starts_with_warning(
        self, tmp_path
    ):
        port = free_port()
        proc = spawn_server(
            tmp_path,
            "streamable-http",
            port,
            host="0.0.0.0",
            capture_stderr=True,
            extra_env={ENV_ALLOW_UNAUTHENTICATED: "1"},
        )
        try:
            wait_healthy(proc, port)
        finally:
            stop_server(proc)
        stderr = drain_stderr(proc)
        assert "WARNING" in stderr  # the loud warning still fires
        assert "NON-localhost" in stderr

    def test_gate_subprocess_token_starts_clean(self, tmp_path):
        port = free_port()
        proc = spawn_server(
            tmp_path,
            "streamable-http",
            port,
            host="0.0.0.0",
            capture_stderr=True,
            extra_env={"WORKFORGE_AUTH_TOKEN": "anytoken"},
        )
        try:
            wait_healthy(proc, port)
        finally:
            stop_server(proc)
        stderr = drain_stderr(proc)
        # No refusal message, no WARNING.
        assert "refusing to start" not in stderr
        assert "NON-localhost" not in stderr

    def test_gate_subprocess_loopback_no_token_allowed(self, tmp_path):
        port = free_port()
        proc = spawn_server(
            tmp_path, "streamable-http", port, host="127.0.0.1",
            capture_stderr=True,
        )
        try:
            wait_healthy(proc, port)
        finally:
            stop_server(proc)
