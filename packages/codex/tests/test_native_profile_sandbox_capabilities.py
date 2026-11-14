"""Native handoff profiles keep each sandbox source's capabilities scoped."""

import json
from contextlib import asynccontextmanager

import pytest

from agents import Agent, CapabilityError, Environment, function_tool
from agents.sandbox import Manifest, SandboxCapability, sandbox_environment
from tests._asyncio import timeout
from tests.native_executor_fixtures import ExecutorPeer
from tests.native_fixtures import assistant_output, function_call, responses as responses
from tests.test_environment_leases_checklist import trust
from tests.test_native_agent import provider
from tests.test_sandbox_environment import Client, Options


@pytest.mark.parametrize(
    ("root_capability", "collision"),
    [(False, False), (True, False), (True, True)],
    ids=["destination-only", "both-sources", "reserved-handoff"],
)
async def test_selected_sandbox_capability_follows_its_agent(
    root_capability, collision, responses, tmp_path, monkeypatch
):
    seen = []
    current = {}
    settings_calls = []

    class Contribution(SandboxCapability):
        name = "profile-contribution"

        def __init__(self, owner, temperature, tool_name="sandbox_record"):
            self.owner = owner
            self.temperature = temperature
            self.resource = None
            self.tool_name = tool_name

        async def bind(self, session, *, user, workspace):
            self.resource = session

        def tools(self):
            @function_tool(name=self.tool_name)
            def sandbox_record():
                assert self.resource is current[self.owner]
                seen.append(self.owner)
                return self.owner + "-tool-result"

            return (sandbox_record,)

        def sampling_settings(self, settings):
            settings_calls.append(self.owner)
            return {"temperature": self.temperature}

        async def transform_context(self, request):
            return (
                *request.items,
                {
                    "type": "message",
                    "role": "developer",
                    "content": [{"type": "input_text", "text": self.owner + "-capability-context"}],
                },
            )

    async with (
        timeout(25),
        provider(responses) as native,
        ExecutorPeer(tmp_path / "executor").serve() as peer,
    ):
        trust(peer, tmp_path, monkeypatch)

        def source(owner, capability):
            directory = peer.root / owner
            directory.mkdir()
            client = Client([])

            @asynccontextmanager
            async def connect(sandbox):
                current[owner] = sandbox
                yield Environment(
                    url=peer.url,
                    cwd=str(directory),
                    headers={"Authorization": "Bearer " + peer.token},
                    write_policy="legacy",
                )

            return sandbox_environment(
                client,
                manifest=Manifest(root=str(directory)),
                options=Options(),
                connect=connect,
                capabilities=[capability] if capability is not None else [],
            )

        root = Contribution("root", 0.25, "transfer_to_target" if collision else "sandbox_record")
        destination = Contribution("destination", 0.75)
        target = Agent(
            name="Target",
            instructions="Use the selected capability.",
            provider=native,
            model="gpt-5.5",
            environment=source("destination", destination),
        )
        parent = Agent(
            name="Root",
            instructions="Transfer.",
            provider=native,
            model="gpt-5.5",
            handoffs=[target],
        )
        if root_capability:
            responses.respond(function_call("sandbox_record", {}, "root-tool"))
        responses.respond(function_call("transfer_to_target", {}, "handoff"))
        responses.respond(function_call("sandbox_record", {}, "destination-tool"))
        responses.respond(assistant_output("done"))
        root_source = source("root", root if root_capability else None)
        if collision:
            with pytest.raises(ValueError, match="handoff name collides"):
                async with parent.session(environment=root_source):
                    pytest.fail("An Agent profile admitted a reserved handoff name")
            assert not seen and not responses.requests
            return
        async with parent.session(environment=root_source) as session:
            assert (await (await session.submit("Run.")).get_result()).final_output == "done"
            with pytest.raises(CapabilityError, match="provider state capture"):
                await session.save_state()

    assert seen == (["root", "destination"] if root_capability else ["destination"])
    assert root.resource is None and destination.resource is None
    assert parent.tools == target.tools == ()
    assert settings_calls == (["root", "destination"] if root_capability else ["destination"])
    target_request = responses.requests[-2]
    assert target_request["temperature"] == 0.75
    assert "destination-capability-context" in json.dumps(target_request["input"])
    assert "root-capability-context" not in json.dumps(target_request["input"])
    assert "destination-tool-result" in json.dumps(responses.requests[-1]["input"])
    if root_capability:
        assert responses.requests[0]["temperature"] == 0.25
        assert "root-capability-context" in json.dumps(responses.requests[0]["input"])
        assert "root-tool-result" in json.dumps(responses.requests[1]["input"])
    assert "destination-capability-context" not in json.dumps(responses.requests[0]["input"])
