"""MCP environment precedence follows the machine that launches the process."""

from __future__ import annotations

import os
import sys
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Literal

import pytest

from agents import Agent, Environment, NativeProvider, StdioMCP
from tests._asyncio import timeout
from tests.native_executor_fixtures import ExecutorPeer
from tests.test_native_child_agents import trust
from tests.test_native_mcp_stdio import SERVER, assert_stopped, records


@pytest.mark.parametrize("location", ["host", "environment"])
@pytest.mark.parametrize("case", ["inherited", "ascii-duplicates", "unicode-duplicates", "path"])
async def test_native_mcp_environment_names(
    location: Literal["host", "environment"],
    case: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inherited: list[str] = []
    if case == "inherited":
        names = ("B04_Case", "B04_CASE")
        monkeypatch.setenv(names[0], "executor-inherited")
        inherited = [names[0]]
        values = {names[1]: "explicit-value"}
    elif case == "path":
        names = ("PATH", "Path")
        values = {names[1]: "synthetic-child-path"}
    else:
        names = ("B04_CASE", "b04_case") if case == "ascii-duplicates" else ("B04_É", "B04_é")
        values = {names[0]: "first-value", names[1]: "second-value"}

    async with timeout(30), AsyncExitStack() as stack:
        environment = None
        root = tmp_path
        if location == "environment":
            peer = await stack.enter_async_context(ExecutorPeer(tmp_path / "remote").serve())
            trust(peer, tmp_path, monkeypatch)
            root = peer.root
            environment = Environment(
                url=peer.url,
                cwd=str(peer.root),
                headers={"Authorization": "Bearer " + peer.token},
                tools=[],
                **({"write_policy": "legacy"} if os.name == "nt" else {}),
            )
            if inherited:
                monkeypatch.setenv(names[0], "host-value-must-not-be-forwarded")
        log = root / "environment-names.jsonl"
        declaration = StdioMCP(
            name="names",
            command=sys.executable,
            args=(
                "-I",
                "-B",
                str(SERVER),
                "--log",
                str(log),
                "--resources",
                "--observe-env",
                names[0],
                "--observe-env",
                names[1],
            ),
            tools=[],
            location=location,
            env_vars=inherited,
            env=values,
        )
        provider = await stack.enter_async_context(
            NativeProvider(base_url="http://127.0.0.1:1/v1", api_key="synthetic-environment-key")
        )
        async with Agent(instructions="Read resources.", tools=[declaration]).session(
            provider=provider, environment=environment
        ) as session:
            if os.name == "nt" and case.endswith("duplicates"):
                with pytest.raises((ValueError, RuntimeError), match="(?i)environment"):
                    await session.list_mcp_resources("names")
                assert not log.exists(), "ambiguous environment launched a process"
                return
            result = await session.list_mcp_resources("names")
            assert result["resources"][0]["uri"] == "fixture://environment"
        if location == "environment" and case == "inherited":
            starts = [
                request for request in peer.requests if request.get("method") == "process/start"
            ]
            assert starts
            for request in starts:
                params = request["params"]
                assert params["env"] == values
                assert params["envPolicy"]["set"] == {}
                assert names[0] in params["envPolicy"]["includeOnly"]
                assert "host-value-must-not-be-forwarded" not in str(params)
        await assert_stopped(log)
        starts = [row for row in records(log) if row["event"] == "start"]
        assert starts
        for row in starts:
            observed = row["observed_env"]
            if case.endswith("duplicates"):
                assert observed == values
            elif case == "inherited":
                assert observed == {
                    names[0]: "explicit-value" if os.name == "nt" else "executor-inherited",
                    names[1]: "explicit-value",
                }
            else:
                assert observed[names[1]] == "synthetic-child-path"
                if os.name == "nt":
                    assert observed[names[0]] == "synthetic-child-path"
                else:
                    assert observed[names[0]] and observed[names[0]] != "synthetic-child-path"
