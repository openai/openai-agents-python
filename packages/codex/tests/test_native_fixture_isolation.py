"""The exec-server fixture must not discover the parent's MCP configuration."""

import os
from pathlib import Path

import pytest
from aiohttp import ClientSession

from tests._asyncio import timeout


@pytest.fixture(autouse=True)
def synthetic_parent_config(tmp_path, monkeypatch):
    home = tmp_path / "synthetic-parent-config"
    home.mkdir()
    config = home / "config.toml"
    config.write_text(
        '[mcp_servers.fixture_ambient]\nurl = "http://127.0.0.1:1/mcp"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("CODEX_HOME", str(home))
    return home


async def test_executor_reads_its_own_config_without_parent_mcp(
    native_stdio_environment, native_executor_environment, synthetic_parent_config
):
    # Read only this synthetic key. No model or MCP connection is started.
    async with (
        timeout(10),
        ClientSession() as http,
        http.ws_connect(native_stdio_environment.url) as ws,
    ):
        await ws.send_json(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"clientName": "fixture-isolation"},
            }
        )
        initialized = await ws.receive_json()
        assert initialized["id"] == 1 and "result" in initialized
        await ws.send_json({"jsonrpc": "2.0", "method": "initialized", "params": None})
        await ws.send_json(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "environmentConfig/read",
                "params": {
                    "cwd": Path(native_stdio_environment.cwd).as_uri(),
                    "configPaths": [["mcp_servers", "fixture_ambient"]],
                    "requirementsPaths": [],
                },
            }
        )
        reply = await ws.receive_json()
    assert reply["id"] == 2 and "result" in reply
    result = reply["result"]
    assert result["codexHomeDir"] == Path(native_executor_environment["CODEX_HOME"]).as_uri()
    assert all("fixture_ambient" not in layer["toml"] for layer in result["config"]["layers"])
    assert os.environ["CODEX_HOME"] == str(synthetic_parent_config)
    assert "fixture_ambient" in (synthetic_parent_config / "config.toml").read_text()
