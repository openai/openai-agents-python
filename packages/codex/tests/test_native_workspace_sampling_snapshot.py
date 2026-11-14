import json
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

import pytest

from _agents_codex.session import _invocation
from agents import Agent, Environment, NativeProvider, Session, ToolContext, function_tool
from agents.classic.sandbox.entries import File
from agents.sandbox import Manifest, sandbox_environment
from tests._asyncio import timeout
from tests.native_executor_fixtures import ExecutorPeer
from tests.native_fixtures import assistant_output, function_call, responses as responses

if sys.platform in ("linux", "darwin"):
    from agents.classic.sandbox.sandboxes.unix_local import (
        UnixLocalSandboxClient,
        UnixLocalSandboxClientOptions,
    )

pytestmark = pytest.mark.skipif(
    sys.platform not in ("linux", "darwin"), reason="UnixLocal resource"
)


def workspace_source(tmp_path, monkeypatch, label):
    directory = (
        tmp_path
        / {
            "RESOURCE_ROOT_ONLY": "root",
            "RESOURCE_DESTINATION_ONLY": "destination",
            "RESOURCE_REVIEWER_ONLY": "reviewer",
            None: "bare",
        }[label]
    )
    directory.mkdir(exist_ok=True)

    @asynccontextmanager
    async def connect(_resource=None):
        async with ExecutorPeer(tmp_path / uuid4().hex).serve() as peer:
            trust = tmp_path / "trusted.pem"
            with trust.open("ab") as output:
                output.write(peer.ca.read_bytes())
            empty = tmp_path / "empty-certs"
            empty.mkdir(exist_ok=True)
            monkeypatch.setenv("SSL_CERT_FILE", str(trust))
            monkeypatch.setenv("SSL_CERT_DIR", str(empty))
            yield Environment(
                url=peer.url,
                headers={"Authorization": "Bearer " + peer.token},
                cwd=str(directory),
                tools=[],
            )

    if label is None:
        return connect()
    return sandbox_environment(
        UnixLocalSandboxClient(),
        manifest=Manifest(
            root=str(directory),
            entries={Path("guide.txt"): File(content=b"guide", description=label)},
        ),
        options=UnixLocalSandboxClientOptions(),
        connect=connect,
    )


def input_text(item):
    return "\n".join(
        part.get("text", "") for part in item.get("content", []) if isinstance(part, dict)
    )


@pytest.mark.parametrize("round_trip", [False, True])
@pytest.mark.parametrize("destination_workspace", [False, True])
async def test_workspace_sampling_has_only_current_resource_and_preserves_policy(
    responses, tmp_path, monkeypatch, round_trip, destination_workspace
):
    root_marker, destination_marker = "RESOURCE_ROOT_ONLY", "RESOURCE_DESTINATION_ONLY"
    provider = NativeProvider(
        base_url=responses.url, api_key="synthetic-workspace", supports_websockets=False
    )
    root = Agent(name="A", instructions="ROOT_APPLICATION", model="gpt-5.5", provider=provider)
    target = Agent(
        name="B",
        instructions="DESTINATION_APPLICATION",
        environment=workspace_source(
            tmp_path, monkeypatch, destination_marker if destination_workspace else None
        ),
    )
    object.__setattr__(root, "handoffs", (target,))
    if round_trip:
        object.__setattr__(target, "handoffs", (root,))
    responses.respond(function_call("transfer_to_b", {}, "to-b"))
    if round_trip:
        responses.respond(function_call("transfer_to_a", {}, "to-a"))
    responses.respond(assistant_output("done"))
    responses.respond(assistant_output("continued"))
    expected = [root_marker, destination_marker if destination_workspace else None]
    if round_trip:
        expected.append(root_marker)
    expected.append(expected[-1])
    async with (
        timeout(30),
        provider,
        root.session(environment=workspace_source(tmp_path, monkeypatch, root_marker)) as session,
    ):
        before = await session.history_snapshot()
        await session.transact_history(
            expected_revision=before.revision,
            keep=before.total,
            items=[
                {
                    "type": "message",
                    "role": "developer",
                    "content": [{"type": "input_text", "text": "CALLER_POLICY_STAYS"}],
                }
            ],
        )
        assert (await (await session.submit("route")).get_result()).final_output == "done"
        await session.update(instructions="UPDATED_APPLICATION")
        assert (await (await session.submit("continue")).get_result()).final_output == "continued"
        audit = json.dumps((await session.history_snapshot()).items)
        assert root_marker not in audit and destination_marker not in audit
        with pytest.raises(
            (ValueError, RuntimeError), match="Per-Agent Environment sources|Scoped workspace"
        ):
            await session.save_state()
    assert len(responses.requests) == len(expected)
    for request, selected in zip(responses.requests, expected, strict=True):
        items = request["input"]
        serialized = json.dumps(items)
        assert "CALLER_POLICY_STAYS" in serialized
        for marker in (root_marker, destination_marker):
            assert serialized.count(marker) == (1 if selected == marker else 0)
        resource = [item for item in items if selected and selected in input_text(item)]
        if selected:
            assert len(resource) == 1 and resource[0]["role"] == "user"


@pytest.mark.parametrize("mode", ["stored", "previous-response", "websocket"])
async def test_scoped_workspace_refuses_stateful_provider_before_request(
    responses, tmp_path, monkeypatch, mode
):
    provider = NativeProvider(
        base_url=responses.url,
        api_key="synthetic-workspace",
        supports_websockets=mode == "websocket",
    )
    target = Agent(
        name="B",
        instructions="destination",
        environment=workspace_source(tmp_path, monkeypatch, "RESOURCE_DESTINATION_ONLY"),
        model_settings={"store": True} if mode == "previous-response" else {},
    )
    root = Agent(
        name="A",
        instructions="root",
        model="gpt-5.5",
        provider=provider,
        handoffs=[target],
        model_settings={"store": True} if mode != "websocket" else {},
    )
    options = (
        {"provider_history": {"type": "previous_response", "id": "resp_previous"}}
        if mode == "previous-response"
        else {}
    )
    with pytest.raises((ValueError, RuntimeError), match="Scoped workspace"):
        async with (
            timeout(30),
            provider,
            root.session(
                environment=workspace_source(tmp_path, monkeypatch, "RESOURCE_ROOT_ONLY"), **options
            ) as session,
        ):
            await (await session.submit("route")).get_result()
    assert responses.requests == []


@pytest.mark.parametrize("child_handoff", [False, True, "new-resource"])
async def test_configured_child_uses_issuing_workspace_guidance(
    responses, tmp_path, monkeypatch, child_handoff
):
    root_marker, destination_marker = "RESOURCE_ROOT_ONLY", "RESOURCE_DESTINATION_ONLY"
    alternate_marker = "RESOURCE_REVIEWER_ONLY"
    provider = NativeProvider(
        base_url=responses.url, api_key="synthetic-workspace", supports_websockets=False
    )
    child_histories = []
    child_sessions = []

    @function_tool
    async def inspect_history(context: ToolContext[None]) -> str:
        invocation = _invocation.get()
        assert invocation is not None
        assert isinstance(invocation.session, Session)
        child_sessions.append(invocation.session)
        return "inspected"

    async def read_history(result):
        assert len(child_sessions) == 1
        child_histories.append((await child_sessions[0].history_snapshot()).items)
        return result.final_output

    child = Agent(
        name="Specialist",
        instructions="Inspect the selected workspace.",
        model="gpt-5.5",
        provider=provider,
        tools=[inspect_history],
        handoffs=[
            Agent(
                name="Reviewer",
                instructions="Review the selected workspace.",
                environment=(
                    workspace_source(tmp_path, monkeypatch, alternate_marker)
                    if child_handoff == "new-resource"
                    else None
                ),
            )
        ]
        if child_handoff
        else [],
    )
    destination = Agent(
        name="Destination",
        instructions="Run the specialist.",
        tools=[child.as_tool("specialist", custom_output_extractor=read_history)],
        environment=workspace_source(tmp_path, monkeypatch, destination_marker),
    )
    root = Agent(
        name="Root",
        instructions="Transfer to the destination.",
        model="gpt-5.5",
        provider=provider,
        handoffs=[destination],
    )
    responses.respond(function_call("transfer_to_destination", {}, "to-destination"))
    responses.respond(function_call("specialist", {"input": "Inspect."}, "inspect"))
    responses.respond(function_call("inspect_history", {}, "inspect-history"))
    if child_handoff:
        responses.respond(function_call("transfer_to_reviewer", {}, "to-reviewer"))
    responses.respond(assistant_output("child done"))
    responses.respond(assistant_output("done"))
    async with (
        timeout(30),
        provider,
        root.session(environment=workspace_source(tmp_path, monkeypatch, root_marker)) as session,
    ):
        assert (await (await session.submit("route")).get_result()).final_output == "done"
    assert len(responses.requests) == (6 if child_handoff else 5)
    assert len(child_histories) == 1
    assert destination_marker not in json.dumps(child_histories[0])
    assert root_marker not in json.dumps(child_histories[0])
    assert alternate_marker not in json.dumps(child_histories[0])
    for index, request in enumerate(responses.requests):
        input_items = json.dumps(request["input"])
        new_resource = child_handoff == "new-resource" and index == 4
        assert input_items.count(root_marker) == (1 if index == 0 else 0)
        assert input_items.count(destination_marker) == (0 if index == 0 or new_resource else 1)
        assert input_items.count(alternate_marker) == (1 if new_resource else 0)


@pytest.mark.parametrize("mode", ["store", "websocket"])
async def test_configured_child_workspace_rejects_stateful_request(
    responses, tmp_path, monkeypatch, mode
):
    provider = NativeProvider(
        base_url=responses.url, api_key="synthetic-workspace", supports_websockets=False
    )
    child_provider = (
        NativeProvider(
            base_url=responses.url, api_key="synthetic-workspace", supports_websockets=True
        )
        if mode == "websocket"
        else provider
    )
    child = Agent(
        name="Specialist",
        instructions="Inspect the workspace.",
        model="gpt-5.5",
        provider=child_provider,
        model_settings={"store": True} if mode == "store" else {},
    )
    destination = Agent(
        name="Destination",
        instructions="Run the specialist.",
        tools=[child.as_tool("specialist")],
        environment=workspace_source(tmp_path, monkeypatch, "RESOURCE_DESTINATION_ONLY"),
    )
    root = Agent(
        name="Root",
        instructions="Transfer to the destination.",
        model="gpt-5.5",
        provider=provider,
        handoffs=[destination],
    )
    responses.respond(function_call("transfer_to_destination", {}, "to-destination"))
    responses.respond(function_call("specialist", {"input": "Inspect."}, "inspect"))
    try:
        async with (
            timeout(20),
            provider,
            root.session(
                environment=workspace_source(tmp_path, monkeypatch, "RESOURCE_ROOT_ONLY")
            ) as session,
        ):
            with pytest.raises((ValueError, RuntimeError), match="Scoped workspace guidance"):
                await (await session.submit("route")).get_result()
    finally:
        if child_provider is not provider:
            await child_provider.aclose()
    assert len(responses.requests) == 2
