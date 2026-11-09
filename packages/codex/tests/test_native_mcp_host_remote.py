"""A captured host MCP roster does not select host command execution."""

import json

import pytest

from agents import Agent, Environment, NativeError
from tests._asyncio import timeout
from tests.native_executor_fixtures import ExecutorPeer
from tests.native_fixtures import ResponsesFixture, function_call, responses as responses
from tests.test_native_agent import provider
from tests.test_native_mcp_stdio import assert_stopped, records, stdio
from tests.test_native_mcp_tools import mcp_call


@pytest.mark.parametrize("noise", ["none", "complete", "partial"])
async def test_host_mcp_with_remote_selection_ignores_ambient_noise(
    responses: ResponsesFixture, tmp_path, monkeypatch, noise
):
    for key in (
        "CODEX_EXEC_SERVER_NOISE_REGISTRY_URL",
        "CODEX_EXEC_SERVER_NOISE_ENVIRONMENT_ID",
        "CODEX_EXEC_SERVER_NOISE_AUTH_TOKEN",
        "CODEX_EXEC_SERVER_NOISE_CHATGPT_ACCOUNT_ID",
    ):
        monkeypatch.delenv(key, raising=False)
    if noise != "none":
        monkeypatch.setenv("CODEX_EXEC_SERVER_NOISE_REGISTRY_URL", "http://127.0.0.1:1/unused")
        if noise == "complete":
            monkeypatch.setenv("CODEX_EXEC_SERVER_NOISE_ENVIRONMENT_ID", "unused")
            monkeypatch.setenv("CODEX_EXEC_SERVER_NOISE_AUTH_TOKEN", "synthetic-unused-noise")
    log = tmp_path / "host-mcp"
    responses.respond(mcp_call("search", "mcp__local", "host-request"))
    responses.respond(
        function_call(
            "exec_command",
            {"cmd": "printf HOST_MUST_NOT_RUN", "environment_id": "local"},
            "unselected-local",
        )
    )
    async with timeout(25), ExecutorPeer(tmp_path / "remote").serve() as peer:
        monkeypatch.setenv("SSL_CERT_FILE", str(peer.ca))
        monkeypatch.setenv("SSL_CERT_DIR", str(tmp_path / "empty-cert-directory"))
        selected = Environment(
            url=peer.url,
            cwd=str(peer.root),
            headers={"Authorization": "Bearer " + peer.token},
            tools=["exec_command"],
        )
        async with Agent(
            instructions="Use only current authorities.",
            provider=provider(responses),
            tools=[stdio(log, location="host")],
        ).session(environment=selected) as session:
            turn = await session.submit("Use the selected tools.")
            with pytest.raises(NativeError, match="Native executor rejected invalid arguments"):
                await turn.get_result()
            calls = [r for r in records(log) if r.get("message", {}).get("method") == "tools/call"]
            assert len(calls) == 1
            assert len(responses.requests) == 2
            assert not [r for r in peer.requests if r.get("method") == "process/start"]
            assert "synthetic-unused-noise" not in json.dumps(responses.requests)
        await assert_stopped(log)
