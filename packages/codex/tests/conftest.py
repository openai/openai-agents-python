"""Shared native fixtures with explicit execution-environment ownership."""

import asyncio
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp import ClientConnectorError, ClientSession, ClientTimeout

from tests._asyncio import timeout


@pytest.fixture
def native_executor_environment(tmp_path):
    # Executor-owned MCP discovery has its own config loader. Client runtime
    # loader flags cannot isolate a separately launched exec-server.
    home = tmp_path / "executor-config"
    home.mkdir()
    return {**os.environ, "CODEX_HOME": str(home)}


@pytest.fixture
def native_executor_binary():
    repository = Path(__file__).resolve().parents[1]
    binary = Path(
        os.environ.get(
            "CODEX_EXEC_SERVER_TEST_BINARY",
            repository / "target/debug/examples/exec-server-fixture",
        )
    )
    if not binary.is_file() or not os.access(binary, os.X_OK):
        pytest.fail(
            f"Required native stdio exec-server fixture is unavailable: {binary}. "
            "From the repository run 'cargo build --example exec-server-fixture' "
            "or set CODEX_EXEC_SERVER_TEST_BINARY to the executable built by that command."
        )

    return binary


@pytest.fixture
async def native_stdio_environment(
    tmp_path, unused_tcp_port, native_executor_environment, native_executor_binary
):
    binary = native_executor_binary
    workspace = tmp_path / "mcp"
    workspace.mkdir()
    address = f"127.0.0.1:{unused_tcp_port}"
    log_path = tmp_path / "exec-server.log"
    with log_path.open("wb") as log:
        process = await asyncio.create_subprocess_exec(
            str(binary),
            f"ws://{address}",
            cwd=workspace,
            env=native_executor_environment,
            stdout=log,
            stderr=log,
        )
        try:
            async with (
                timeout(15),
                ClientSession(timeout=ClientTimeout(total=2)) as http,
            ):
                while True:
                    if process.returncode is not None:
                        pytest.fail(
                            f"Exec-server exited before readiness ({process.returncode}): "
                            f"{log_path.read_text()}"
                        )
                    try:
                        async with http.get(f"http://{address}/readyz") as response:
                            if response.status == 200:
                                break
                            pytest.fail(
                                f"Exec-server readiness returned {response.status}: "
                                f"{await response.text()}"
                            )
                    except ClientConnectorError:
                        # Retry only a failed connection while the process binds.
                        await asyncio.sleep(0.05)
            yield SimpleNamespace(url=f"ws://{address}", cwd=str(workspace))
        finally:
            if process.returncode is None:
                process.terminate()
                try:
                    async with timeout(5):
                        await process.wait()
                except TimeoutError:
                    process.kill()
                    await process.wait()
