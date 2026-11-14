"""Native profiles select their declared executor and tool ceiling."""

from __future__ import annotations

import pytest

from agents import Agent, Environment
from tests._asyncio import timeout
from tests.native_executor_fixtures import ExecutorPeer
from tests.native_fixtures import assistant_output, function_call, responses as responses
from tests.test_environment_leases_checklist import trust
from tests.test_native_agent import provider
from tests.test_native_executor_tools import declarations, tool_output


@pytest.mark.parametrize(
    ("root_tools", "child_tools"),
    [
        (("exec_command",), ("exec_command",)),
        (("exec_command",), ()),
        ((), ("exec_command",)),
        (None, ("exec_command",)),
    ],
    ids=[
        "distinct-child-cwd",
        "child-denies-root-tool",
        "empty-root-to-child",
        "model-only-root-to-child",
    ],
)
async def test_profile_executor_ceiling(root_tools, child_tools, tmp_path, monkeypatch, responses):
    native = provider(responses)
    try:
        async with (
            timeout(30),
            ExecutorPeer(
                tmp_path / "child", synthetic_process_output="child-resource"
            ).serve() as peer,
        ):
            trust(peer, tmp_path, monkeypatch)
            destination = Environment(
                url=peer.url,
                headers={"Authorization": "Bearer " + peer.token},
                cwd=str(peer.root),
                tools=child_tools,
                write_policy="legacy",
            )
            child = Agent(
                name="Child",
                instructions="Use the child executor.",
                provider=native,
                environment=destination,
            )
            root = Agent(
                name="Root", instructions="Transfer to Child.", provider=native, handoffs=[child]
            )
            root_directory = peer.root / "root-default"
            root_directory.mkdir()
            default = (
                None
                if root_tools is None
                else Environment(
                    url=peer.url,
                    headers={"Authorization": "Bearer " + peer.token},
                    cwd=str(root_directory),
                    tools=root_tools,
                    write_policy="legacy",
                )
            )
            if not root_tools:
                responses.respond(function_call("exec_command", {"cmd": "pwd"}, "root-denied"))
            responses.respond(function_call("transfer_to_child", {}, "handoff"))
            responses.respond(
                function_call("exec_command", {"cmd": "pwd", "max_output_tokens": 100}, "cwd")
            )
            responses.respond(assistant_output("done"))
            async with root.session(environment=default) as session:
                result = await (await session.submit("Use the selected executor.")).get_result()
                assert result.final_output == "done"
                output = tool_output(responses.requests[-1], "cwd")
                starts = [
                    request for request in peer.requests if request.get("method") == "process/start"
                ]
                if not root_tools:
                    assert ("functions", "exec_command") not in declarations(responses.requests[0])
                    assert "unsupported call: exec_command" in tool_output(
                        responses.requests[1], "root-denied"
                    )
                if child_tools:
                    assert "child-resource" in output
                    assert len(starts) == 1
                    assert starts[0]["params"]["cwd"] == peer.root.as_uri()
                else:
                    assert "unsupported call: exec_command" in output
                    assert not starts
                saved = (await session.save_state()).to_json()
                assert peer.token not in saved and peer.url not in saved
    finally:
        await native.aclose()


async def test_restored_profile_tool_availability(tmp_path, monkeypatch, responses):
    from agents import SessionState

    native = provider(responses)
    try:
        async with (
            timeout(30),
            ExecutorPeer(
                tmp_path / "child", synthetic_process_output="child-resource"
            ).serve() as peer,
        ):
            trust(peer, tmp_path, monkeypatch)
            destination = Environment(
                url=peer.url,
                headers={"Authorization": "Bearer " + peer.token},
                cwd=str(peer.root),
                tools=("exec_command",),
                write_policy="legacy",
            )
            child = Agent(
                name="Child",
                instructions="Use the child executor.",
                provider=native,
                environment=destination,
            )
            root = Agent(
                name="Root", instructions="Transfer to Child.", provider=native, handoffs=[child]
            )
            responses.respond(function_call("transfer_to_child", {}, "handoff"))
            responses.respond(assistant_output("ready"))
            async with root.session() as session:
                await (await session.submit("Transfer.")).get_result()
                saved = (await session.save_state()).to_json()
            responses.respond(
                function_call(
                    "exec_command", {"cmd": "pwd", "max_output_tokens": 100}, "restored-cwd"
                )
            )
            responses.respond(assistant_output("done"))
            async with root.session(state=SessionState.from_json(saved)) as restored:
                await (await restored.submit("Use the current child executor.")).get_result()
                assert "child-resource" in tool_output(responses.requests[-1], "restored-cwd")
                starts = [
                    request for request in peer.requests if request.get("method") == "process/start"
                ]
                assert len(starts) == 1
                assert starts[0]["params"]["cwd"] == peer.root.as_uri()
    finally:
        await native.aclose()


async def test_next_turn_profile_tool_availability(tmp_path, monkeypatch, responses):
    native = provider(responses)
    try:
        async with (
            timeout(30),
            ExecutorPeer(
                tmp_path / "child", synthetic_process_output="child-resource"
            ).serve() as peer,
        ):
            trust(peer, tmp_path, monkeypatch)
            destination = Environment(
                url=peer.url,
                headers={"Authorization": "Bearer " + peer.token},
                cwd=str(peer.root),
                tools=("exec_command",),
                write_policy="legacy",
            )
            child = Agent(
                name="Child",
                instructions="Use the child executor.",
                provider=native,
                environment=destination,
            )
            root = Agent(
                name="Root", instructions="Transfer to Child.", provider=native, handoffs=[child]
            )
            responses.respond(function_call("transfer_to_child", {}, "handoff"))
            responses.respond(assistant_output("ready"))
            async with root.session() as session:
                await (await session.submit("Transfer.")).get_result()
                responses.respond(
                    function_call(
                        "exec_command", {"cmd": "pwd", "max_output_tokens": 100}, "restored-cwd"
                    )
                )
                responses.respond(assistant_output("done"))
                await (await session.submit("Use the current child executor.")).get_result()
                assert "child-resource" in tool_output(responses.requests[-1], "restored-cwd")
                starts = [
                    request for request in peer.requests if request.get("method") == "process/start"
                ]
                assert len(starts) == 1
                assert starts[0]["params"]["cwd"] == peer.root.as_uri()
    finally:
        await native.aclose()
