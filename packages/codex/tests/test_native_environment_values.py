"""Captured environment values reach native executor requests and stay out of state."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agents import Agent
from tests._asyncio import timeout
from tests.native_executor_fixtures import ExecutorPeer
from tests.native_fixtures import (
    ResponsesFixture,
    assistant_output,
    function_call,
    responses as responses,
)
from tests.test_native_agent import provider
from tests.test_native_executor_tools import declaration


@pytest.mark.parametrize("destination", ["replace", "omit", "empty"])
async def test_native_executor_uses_current_environment_after_restore(
    responses: ResponsesFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    destination: str,
) -> None:
    source_secret = "source-private-environment-value"
    destination_secret = "destination-private-environment-value"
    # A protocol reply avoids depending on the host's OS sandbox availability.
    # The assertions inspect the actual native Core process/start requests.
    async with (
        timeout(40),
        ExecutorPeer(tmp_path / "remote", synthetic_process_output="ready\n").serve() as peer,
    ):
        monkeypatch.setenv("SSL_CERT_FILE", str(peer.ca))
        monkeypatch.setenv("SSL_CERT_DIR", str(tmp_path / "empty-cert-directory"))
        values = {"SDK_ENV_VALUE": source_secret, "EMPTY": "", "PATH": "/opt/app:/usr/bin:/bin"}
        original = declaration(peer, env=values)
        values["SDK_ENV_VALUE"] = "mutated-after-capture"
        replacement = (
            {"SDK_ENV_VALUE": destination_secret, "EMPTY": "", "PATH": "/opt/current:/usr/bin:/bin"}
            if destination == "replace"
            else {}
            if destination == "empty"
            else None
        )
        current = declaration(peer, env=replacement)
        agent = Agent(instructions="Run the selected command.", provider=provider(responses))
        saved = None
        for phase, environment in (("source", original), ("destination", current)):
            responses.respond(
                function_call("exec_command", {"cmd": "printf ready", "login": False}, phase)
            )
            responses.respond(assistant_output("completed"))
            async with agent.session(state=saved, environment=environment) as session:
                turn = await session.submit("Run the command")
                assert (await turn.get_result()).final_output == "completed"
                saved = await session.save_state()
                serialized = saved.to_json()
                observations = repr([event async for event in turn.events()])
                for secret in (source_secret, destination_secret):
                    assert secret not in serialized
                    assert secret not in observations
            [request] = [
                item
                for item in peer.requests
                if item.get("method") == "process/start"
                and item["params"].get("metadata", {}).get("toolCallId") == phase
            ]
            actual = request["params"]["env"]
            expected = (
                source_secret
                if phase == "source"
                else (destination_secret if destination == "replace" else None)
            )
            assert actual.get("SDK_ENV_VALUE") == expected
            if expected is not None:
                assert actual["EMPTY"] == ""
                assert actual["PATH"] == (
                    "/opt/app:/usr/bin:/bin" if phase == "source" else "/opt/current:/usr/bin:/bin"
                )
        wire = json.dumps(responses.requests)
        assert source_secret not in wire
        assert destination_secret not in wire
