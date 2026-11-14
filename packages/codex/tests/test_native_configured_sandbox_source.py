"""Wire probes of captured sandbox contributions in configured children."""

import asyncio
import json
from contextlib import asynccontextmanager

import pytest

from agents import Agent, Environment, NativeProvider, function_tool
from agents.sandbox import Manifest, SandboxCapability, sandbox_environment
from tests._asyncio import timeout
from tests.native_executor_fixtures import ExecutorPeer
from tests.native_fixtures import assistant_output, function_call
from tests.test_environment_leases_checklist import trust
from tests.test_native_configured_agent_tool import peer
from tests.test_sandbox_environment import Client, Options


@pytest.mark.asyncio
@pytest.mark.parametrize("own_source", ["none", "same", "distinct", "lost", "cancel"])
async def test_configured_child_capability_wire(own_source, tmp_path, monkeypatch):
    entered = []
    closed = []
    effects = []
    current = {}
    settings_applied = []
    child_entered = asyncio.Event()
    child_release = asyncio.Event()

    class Capability(SandboxCapability):
        name = "wire-contribution"

        def __init__(self, owner, temperature):
            self.owner = owner
            self.temperature = temperature

        async def bind(self, session, *, user, workspace):
            self.resource = session

        def tools(self):
            @function_tool(name=self.owner + "_capability")
            def record():
                assert self.resource is not None and self.owner not in closed
                effects.append(self.owner)
                return self.owner

            return (record,)

        def sampling_settings(self, settings):
            settings_applied.append(self.owner)
            return {"temperature": self.temperature}

        async def transform_context(self, request):
            return (
                *request.items,
                {
                    "type": "message",
                    "role": "developer",
                    "content": [{"type": "input_text", "text": self.owner + "-capability-marker"}],
                },
            )

    async with (
        timeout(25),
        peer() as root_model,
        peer() as child_model,
        ExecutorPeer(tmp_path / "executor").serve() as executor,
    ):
        trust(executor, tmp_path, monkeypatch)

        def source(owner, temperature):
            directory = executor.root / owner
            directory.mkdir()

            @asynccontextmanager
            async def connect(sandbox):
                entered.append(owner)
                try:
                    yield Environment(
                        url=executor.url,
                        cwd=str(directory),
                        write_policy="legacy",
                        headers={"Authorization": "Bearer " + executor.token},
                    )
                finally:
                    closed.append(owner)

            return sandbox_environment(
                Client([]),
                manifest=Manifest(root=str(directory)),
                options=Options(),
                connect=connect,
                capabilities=[Capability(owner, temperature)],
            )

        root_source = source("root", 0.25)
        child_source = (
            root_source
            if own_source in ("same", "lost", "cancel")
            else source("child", 0.75)
            if own_source == "distinct"
            else None
        )
        async with (
            NativeProvider(base_url=root_model.url, api_key="synthetic-root") as root_provider,
            NativeProvider(base_url=child_model.url, api_key="synthetic-child") as child_provider,
        ):
            child = Agent(
                name="Child",
                instructions="Reply.",
                model="gpt-5.5",
                provider=child_provider,
                model_settings={"temperature": 0.5},
                environment=child_source,
            )

            def after_model(response):
                if own_source == "lost" and response.response_id == "resp_1":
                    current["session"]._environment_lease._interrupted = True

            root = Agent(
                name="Root",
                instructions="Delegate.",
                model="gpt-5.5",
                provider=root_provider,
                after_model=after_model,
                tools=[child.as_tool(tool_name="child", tool_description="Delegate.")],
            )
            if own_source == "cancel":

                async def hold_child(event):
                    if event["type"] == "response.created":
                        child_entered.set()
                        await child_release.wait()

                child_model.before_event = hold_child
            root_model.respond(function_call("child", {"input": "child work"}, "child-call"))
            if own_source == "same":
                child_model.respond(function_call("root_capability", {}, "child-capability-call"))
            child_model.respond(assistant_output("child done"))
            if own_source != "lost":
                root_model.respond(function_call("root_capability", {}, "parent-after-child-call"))
            root_model.respond(assistant_output("root done"))
            async with root.session(environment=root_source) as session:
                current["session"] = session
                turn = await session.submit("Start.")
                if own_source == "lost":
                    from agents import InteractionExpiredError

                    with pytest.raises(InteractionExpiredError, match="sandbox source is retired"):
                        await turn.get_result()
                elif own_source == "cancel":
                    await child_entered.wait()
                    try:
                        await turn.cancel()
                        assert not closed
                    finally:
                        child_release.set()
                    turn = await session.submit("Continue the parent.")
                    assert (await turn.get_result()).final_output == "root done"
                else:
                    assert (await turn.get_result()).final_output == "root done"
                assert "root" not in closed
                assert entered.count("root") == 1
                if own_source != "distinct":
                    assert entered == ["root"]
                assert effects == (
                    ["root", "root"]
                    if own_source == "same"
                    else []
                    if own_source == "lost"
                    else ["root"]
                )
        assert sorted(closed) == sorted(entered)
        if own_source == "lost":
            assert not child_model.requests
            assert settings_applied == ["root"]
            assert len(root_model.requests) == 1
            return
        wire = child_model.requests[0]
        report = {
            "own_source": own_source,
            "acquired_sources": entered,
            "root_temperature": root_model.requests[0].get("temperature"),
            "child_temperature": wire.get("temperature"),
            "child_tools": [row.get("name") for row in wire.get("tools", [])],
            "child_root_marker": "root-capability-marker" in json.dumps(wire["input"]),
            "child_own_marker": "child-capability-marker" in json.dumps(wire["input"]),
        }
        print("CAPABILITY_WIRE=" + json.dumps(report, sort_keys=True))
        if own_source in ("same", "cancel"):
            assert report["acquired_sources"] == ["root"]
            assert report["child_temperature"] == 0.25
            assert "root_capability" in report["child_tools"]
            assert report["child_root_marker"]
        elif own_source == "distinct":
            assert "root_capability" not in report["child_tools"]
            assert not report["child_root_marker"]
        elif own_source == "none":
            assert report["child_temperature"] == 0.5
            assert report["child_tools"] == []
            assert not report["child_root_marker"]
