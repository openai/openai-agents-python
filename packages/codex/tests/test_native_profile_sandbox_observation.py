"""Native sandbox notices retain the resource that issued the operation."""

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from agents import Agent, Environment, TurnStoppedError
from agents.sandbox import CallbackSink, Instrumentation, Manifest, sandbox_environment
from tests._asyncio import timeout
from tests.native_executor_fixtures import ExecutorPeer
from tests.native_fixtures import (
    assistant_output,
    function_call,
    responses as responses,
)
from tests.test_environment_leases_checklist import trust
from tests.test_native_agent import provider
from tests.test_native_sandbox_events import unix


@unix
@pytest.mark.parametrize(
    ("observed", "cancel_child"),
    [
        (("root", "target"), False),
        (("root",), False),
        (("target",), False),
        (("root", "target"), True),
    ],
    ids=["both-observed", "root-only", "target-only", "child-aborted"],
)
async def test_observers_follow_issuing_resource_through_handoff_and_child(
    observed, cancel_child, responses, tmp_path, monkeypatch
):
    from agents.classic.sandbox.sandboxes.unix_local import UnixLocalSandboxClient

    operations = []
    resources = {}
    native = provider(responses)
    process_admitted = asyncio.Event()

    async def before_process_reply(_):
        if cancel_child and len(peer.synthetic_process_starts) == 3:
            peer.synthetic_process_stays_running = True
            process_admitted.set()

    async with (
        native,
        timeout(30),
        ExecutorPeer(
            tmp_path / "executor",
            synthetic_process_output="private-process-output",
            before_synthetic_process_reply=before_process_reply,
        ).serve() as peer,
    ):
        trust(peer, tmp_path, monkeypatch)

        def source(name):
            instrumentation = (
                Instrumentation(
                    sinks=[CallbackSink(lambda event, resource: operations.append((name, event)))]
                )
                if name in observed
                else None
            )
            client = UnixLocalSandboxClient(
                instrumentation=instrumentation, inherit_host_environment=False
            )

            @asynccontextmanager
            async def connect(resource):
                resources[name] = resource
                yield Environment(
                    url=peer.url,
                    cwd=resource.state.manifest.root,
                    headers={"Authorization": "Bearer " + peer.token},
                    tools=["exec_command"],
                )

            return sandbox_environment(client, manifest=Manifest(), options=None, connect=connect)

        child = Agent(name="Child", instructions="Exec once.", provider=native)
        target = Agent(
            name="Target",
            instructions="Exec and call the child.",
            provider=native,
            environment=source("target"),
            tools=[child.as_tool("inspect")],
        )
        root = Agent(
            name="Root", instructions="Exec and transfer.", provider=native, handoffs=[target]
        )
        responses.respond(
            function_call("exec_command", {"cmd": "echo private-root", "login": False}, "root-op")
        )
        responses.respond(function_call("transfer_to_target", {}, "transfer"))
        responses.respond(
            function_call(
                "exec_command", {"cmd": "echo private-target", "login": False}, "target-op"
            )
        )
        responses.respond(function_call("inspect", {"input": "exec"}, "configured-child"))
        responses.respond(
            function_call(
                "exec_command",
                {"cmd": "echo private-child", "login": False, "yield_time_ms": 10000},
                "child-op",
            )
        )
        responses.respond(assistant_output("child done"))
        responses.respond(assistant_output("done"))

        async with root.session(environment=source("root")) as session:
            turn = await session.submit("Run the operations.")
            if cancel_child:
                await process_admitted.wait()
                await turn.cancel(mode="force")
                with pytest.raises((TurnStoppedError, asyncio.CancelledError)):
                    await turn.get_result()
            else:
                assert (await turn.get_result()).final_output == "done"
        physical = [
            request for request in peer.requests if request.get("method") == "process/start"
        ]
        assert [request["params"]["cwd"] for request in physical] == [
            Path(resources[name].state.manifest.root).as_uri()
            for name in ("root", "target", "target")
        ]

    actual = [(name, event) for name, event in operations if event.op == "exec"]
    expected = [
        (name, call, phase)
        for name, call in (("root", "root-op"), ("target", "target-op"), ("target", "child-op"))
        if name in observed
        for phase in ("start", "finish")
    ]
    assert [(name, event.span_id.rsplit(":", 1)[1], event.phase) for name, event in actual] == (
        expected
    )
    assert all(event.session_id == resources[name].state.session_id for name, event in actual)
    for _, event in actual:
        assert event.data == {}
        if event.phase == "finish":
            assert event.duration_ms >= 0
            assert event.ok == (not (cancel_child and event.span_id.endswith(":child-op")))
            assert event.stdout is None and event.stderr is None
    assert "private-" not in "".join(event.model_dump_json() for _, event in actual)
