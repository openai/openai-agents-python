"""Acquired destination resources retain their own live native callback scope."""

import asyncio
from contextlib import asynccontextmanager

import pytest

from agents import Agent, CapabilityError, Environment, ToolContext, function_tool
from tests._asyncio import timeout
from tests.native_executor_fixtures import ExecutorPeer
from tests.native_fixtures import (
    assistant_output,
    function_call,
    responses as responses,
    text_response,
)
from tests.test_environment_leases_checklist import trust
from tests.test_native_agent import provider
from tests.test_native_executor_tools import tool_output


@pytest.mark.parametrize("root_resource", [True, False], ids=["root-and-target", "model-only-root"])
@pytest.mark.parametrize("target_tools", [("exec_command",), ()], ids=["granted", "excluded"])
async def test_profile_sources_reach_the_current_native_executor(
    tmp_path, monkeypatch, responses, root_resource, target_tools
):
    native = provider(responses)
    events = []
    facts = []
    session = None
    try:
        async with (
            timeout(45),
            ExecutorPeer(
                tmp_path / "executor", synthetic_process_output="target-resource"
            ).serve() as peer,
        ):
            trust(peer, tmp_path, monkeypatch)
            target_root = peer.root / "target"
            target_root.mkdir()

            def environment(cwd, tools=("exec_command",)):
                return Environment(
                    url=peer.url,
                    headers={"Authorization": "Bearer " + peer.token},
                    cwd=str(cwd),
                    tools=tools,
                    write_policy="legacy",
                )

            root_environment = environment(peer.root) if root_resource else None
            target_environment = environment(target_root, target_tools)

            @asynccontextmanager
            async def source(name, selected):
                events.append("enter-" + name)
                try:
                    yield selected
                finally:
                    assert session is None or session._resources_retired
                    events.append("exit-" + name)

            root_source = source("root", root_environment) if root_resource else None
            target_source = source("target", target_environment)

            @function_tool
            def inspect_root(context: ToolContext[None]) -> str:
                facts.append(("root", context._environment, context._sandbox_source))
                return "inspected root"

            @function_tool
            def inspect_target(context: ToolContext[None]) -> str:
                facts.append(("target", context._environment, context._sandbox_source))
                return "inspected target"

            specialist = Agent(
                name="Specialist",
                instructions="Inspect the source.",
                provider=native,
                tools=[inspect_target],
            )
            alias = Agent(
                name="Alias", instructions="Alias", provider=native, environment=target_source
            )
            target = Agent(
                name="Target",
                instructions="Inspect and execute.",
                provider=native,
                environment=target_source,
                tools=[specialist.as_tool("specialist")],
                handoffs=[alias],
            )
            root = Agent(
                name="Root",
                instructions="Inspect and transfer.",
                provider=native,
                tools=[inspect_root],
                handoffs=[target],
            )
            responses.respond(function_call("inspect_root", {}, "root-inspect"))
            responses.respond(function_call("transfer_to_target", {}, "transfer"))
            responses.respond(function_call("specialist", {"input": "Inspect."}, "nested"))
            responses.respond(function_call("inspect_target", {}, "target-inspect"))
            responses.respond(assistant_output("inspected"))
            responses.respond(function_call("exec_command", {"cmd": "pwd", "login": False}, "cwd"))
            responses.respond(assistant_output("done"))
            async with root.session(environment=root_source) as session:
                assert events == (
                    ["enter-root", "enter-target"] if root_resource else ["enter-target"]
                )
                result = await (await session.submit("Use the selected source.")).get_result()
                assert result.final_output == "done"
                assert facts == [
                    ("root", root_environment, root_source),
                    ("target", target_environment, target_source),
                ]
                output = tool_output(responses.requests[-1], "cwd")
                starts = [
                    request for request in peer.requests if request.get("method") == "process/start"
                ]
                if target_tools:
                    assert "target-resource" in output
                    assert len(starts) == 1 and starts[0]["params"]["cwd"] == target_root.as_uri()
                else:
                    assert "unsupported call: exec_command" in output
                    assert not starts
                with pytest.raises(CapabilityError, match="provider state capture"):
                    await session.save_state()
            assert events == (
                ["enter-root", "enter-target", "exit-target", "exit-root"]
                if root_resource
                else ["enter-target", "exit-target"]
            )
    finally:
        await native.aclose()


async def test_interrupted_target_source_retires_native_before_exit(
    tmp_path, monkeypatch, responses
):
    native = provider(responses)
    entered = asyncio.Event()
    model_entered = asyncio.Event()
    gate = asyncio.Event()
    holders = []
    events = []
    session = None
    worker = None
    try:
        async with (
            timeout(45),
            ExecutorPeer(tmp_path / "executor", synthetic_process_output="unused").serve() as peer,
        ):
            trust(peer, tmp_path, monkeypatch)
            environment = Environment(
                url=peer.url,
                headers={"Authorization": "Bearer " + peer.token},
                cwd=str(peer.root),
                tools=("exec_command",),
                write_policy="legacy",
            )

            @asynccontextmanager
            async def source():
                holders.append(asyncio.current_task())
                events.append("enter")
                entered.set()
                try:
                    yield environment
                finally:
                    assert session is not None and session._resources_retired
                    events.append("exit")

            target = Agent(
                name="Target",
                instructions="Wait for the model.",
                provider=native,
                environment=source(),
            )
            root = Agent(name="Root", instructions="Transfer.", provider=native, handoffs=[target])
            responses.respond(function_call("transfer_to_target", {}, "transfer"))
            responses.respond(*text_response("held", "held"))
            responses.delta_gate = gate

            async def before_event(event):
                if event.get("type") == "response.output_text.delta":
                    model_entered.set()

            responses.before_event = before_event

            async def run():
                nonlocal session
                async with root.session() as session:
                    await (await session.submit("Transfer and wait.")).get_result()

            worker = asyncio.create_task(run())
            await entered.wait()
            await model_entered.wait()
            assert len(holders) == 1 and holders[0] is not None
            holders[0].cancel()
            with pytest.raises(asyncio.CancelledError):
                await worker
            assert session is not None and session._closed and session._resources_retired
            assert all(holder.done() for holder in holders)
            assert events == ["enter", "exit"]
            assert not peer.synthetic_process_starts
    finally:
        gate.set()
        if worker is not None and not worker.done():
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
        await native.aclose()


async def test_target_source_entry_failure_joins_root_without_native_effects(
    tmp_path, monkeypatch, responses
):
    native = provider(responses)
    events = []
    try:
        async with ExecutorPeer(
            tmp_path / "executor", synthetic_process_output="unused"
        ).serve() as peer:
            trust(peer, tmp_path, monkeypatch)
            environment = Environment(
                url=peer.url,
                headers={"Authorization": "Bearer " + peer.token},
                cwd=str(peer.root),
                tools=(),
                write_policy="legacy",
            )

            @asynccontextmanager
            async def source(name):
                events.append("enter-" + name)
                try:
                    if name == "target":
                        raise RuntimeError("target acquisition failed")
                    yield environment
                finally:
                    events.append("exit-" + name)

            target = Agent(
                name="Target", instructions="Target", provider=native, environment=source("target")
            )
            root = Agent(name="Root", instructions="Root", provider=native, handoffs=[target])
            with pytest.raises(RuntimeError, match="target acquisition failed"):
                async with root.session(environment=source("root")):
                    pytest.fail("Failed source entered a Session")
            assert events == ["enter-root", "enter-target", "exit-target", "exit-root"]
            assert not responses.requests and not peer.synthetic_process_starts
    finally:
        await native.aclose()
