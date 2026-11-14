"""Configured children inherit the issuing profile's current executor authority."""

from __future__ import annotations

import sys

import pytest

from _agents_codex.session import _invocation
from agents import Agent, Environment, ToolContext, function_tool
from tests._asyncio import timeout
from tests.native_executor_fixtures import ExecutorPeer
from tests.native_fixtures import assistant_output, function_call, responses as responses
from tests.test_environment_leases_checklist import trust
from tests.test_native_agent import provider
from tests.test_native_executor_tools import tool_output


@pytest.mark.skipif(
    sys.platform not in ("linux", "darwin"), reason="requires a Linux or macOS strict executor"
)
@pytest.mark.parametrize("root_resource", [True, False], ids=["distinct-root", "model-only-root"])
async def test_configured_child_inherits_issuing_profile_environment(
    tmp_path, monkeypatch, responses, root_resource
):
    native = provider(responses)
    facts = []

    @function_tool
    def inspect_authority(context: ToolContext[None]) -> str:
        invocation = _invocation.get()
        assert invocation is not None
        facts.append((context._environment, invocation.session._writable_roots))
        return "inspected"

    try:
        async with (
            timeout(45),
            ExecutorPeer(
                tmp_path / "executor", synthetic_process_output="destination-resource"
            ).serve() as peer,
        ):
            trust(peer, tmp_path, monkeypatch)
            destination_root = peer.root / "destination"
            original_root = peer.root / "original"
            destination_root.mkdir()
            original_root.mkdir()
            destination_grant = peer.root / "destination-write"
            original_grant = peer.root / "original-write"
            destination_grant.mkdir()
            original_grant.mkdir()
            destination = Environment(
                url=peer.url,
                headers={"Authorization": "Bearer " + peer.token},
                cwd=str(destination_root),
                writable_roots=(destination_grant,),
                tools=("exec_command",),
            )
            original = (
                Environment(
                    url=peer.url,
                    headers={"Authorization": "Bearer " + peer.token},
                    cwd=str(original_root),
                    writable_roots=(original_grant,),
                    tools=("exec_command",),
                )
                if root_resource
                else None
            )
            specialist = Agent(
                name="Specialist",
                instructions="Inspect and use the executor.",
                provider=native,
                tools=[inspect_authority],
            )
            destination_agent = Agent(
                name="Destination",
                instructions="Run the specialist.",
                provider=native,
                environment=destination,
                tools=[specialist.as_tool("specialist")],
            )
            root = Agent(
                name="Root",
                instructions="Transfer to Destination.",
                provider=native,
                handoffs=[destination_agent],
            )
            responses.respond(function_call("transfer_to_destination", {}, "handoff"))
            responses.respond(
                function_call("specialist", {"input": "Inspect the executor."}, "nested")
            )
            responses.respond(function_call("inspect_authority", {}, "inspect"))
            responses.respond(function_call("exec_command", {"cmd": "pwd", "login": False}, "cwd"))
            responses.respond(assistant_output("specialist complete"))
            responses.respond(assistant_output("done"))
            async with root.session(environment=original) as session:
                result = await (
                    await session.submit("Transfer and run the specialist.")
                ).get_result()
                assert result.final_output == "done"
            assert len(facts) == 1
            assert facts[0][0] is destination
            assert facts[0][1] == destination.writable_roots
            starts = [
                request for request in peer.requests if request.get("method") == "process/start"
            ]
            assert len(starts) == 1
            assert starts[0]["params"]["cwd"] == destination_root.as_uri()
            assert "destination-resource" in tool_output(responses.requests[-2], "cwd")
    finally:
        await native.aclose()
