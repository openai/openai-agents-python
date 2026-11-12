"""Native child declarations reject unsupported authority before opening it."""

from __future__ import annotations

import asyncio
import importlib
import json
import sys
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from typing import Any, Literal

import pytest

from agents import (
    Agent,
    Environment,
    FunctionTool,
    NativeAgents,
    NativeError,
    NativeRole,
    NativeRoleTool,
    NativeToolCall,
    ToolContext,
    function_tool,
)
from agents.state import SessionState
from tests._asyncio import timeout
from tests.native_executor_fixtures import ExecutorPeer
from tests.native_fixtures import (
    ResponsesFixture,
    assistant_output,
    function_call,
    responses as responses,
)
from tests.test_native_agent import provider
from tests.test_native_child_agents import (
    child_output,
    child_peer,
    collaboration,
    handles,
    metadata,
    trust,
)
from tests.test_native_code_mode import code_call
from tests.test_native_executor_tools import declaration
from tests.test_native_manual_tools import LookupArgs, next_request
from tests.test_native_mcp_tools import declarations
from tests.test_native_profile_model_controls import MODEL as NATIVE_ROLE_FIXTURE_MODEL


def _role() -> NativeRole:
    return NativeRole(
        name="research", instructions="private-role-instructions", description="Research a topic."
    )


def _executor() -> Environment:
    return Environment(
        url="wss://executor.invalid",
        cwd="/workspace",
        tools=["exec_command", "write_stdin", "apply_patch"],
    )


@pytest.fixture
def forbid_native_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    original = importlib.import_module

    def import_without_native(name: str, package: str | None = None) -> Any:
        if name == "agents._native":
            pytest.fail("Unsupported child configuration reached native startup")
        return original(name, package)

    monkeypatch.setattr(importlib, "import_module", import_without_native)


def test_native_child_declarations_capture_roles_and_hide_instructions() -> None:
    settings: dict[Literal["reasoning_effort"], str] = {"reasoning_effort": "low"}
    role = replace(_role(), model_settings=settings)
    supplied = [role]
    children = NativeAgents(additional_roles=supplied, max_children=2)
    supplied.clear()
    assert children.additional_roles == (role,)
    assert children.max_children == 2
    assert role.instructions not in repr(role)
    assert role.instructions not in repr(children)
    settings["reasoning_effort"] = "high"
    assert role.model_settings == {"reasoning_effort": "low"}
    assert role.model_settings is not None
    with pytest.raises(TypeError):
        role.model_settings["reasoning_effort"] = "high"
    for declaration_value, name, value in (
        (role, "instructions", "replacement"),
        (children, "additional_roles", ()),
    ):
        with pytest.raises(FrozenInstanceError):
            setattr(declaration_value, name, value)


@pytest.mark.parametrize("name", ["default", "explorer", "worker", "invalid/name", ""])
def test_native_child_declarations_reject_reserved_or_invalid_names(name: str) -> None:
    with pytest.raises(ValueError, match="Built-in native roles|Native role names"):
        NativeRole(name=name, instructions="Read.", description="Read a topic.")


def test_native_role_tools_capture_validated_role_bindings() -> None:
    tool = NativeRoleTool(name="research_topic", role="research", description="Research a topic.")
    supplied = [tool]
    children = NativeAgents(additional_roles=[_role()], managed_tools=supplied)
    supplied.clear()
    assert children.managed_tools == (tool,)

    with pytest.raises(ValueError, match="Unknown native role 'research'"):
        NativeAgents(managed_tools=[tool])
    with pytest.raises(ValueError, match="Native role tool names must be unique"):
        NativeAgents(additional_roles=[_role()], managed_tools=[tool, tool])
    with pytest.raises(ValueError, match="Built-in collaboration tools"):
        replace(tool, name="spawn_agent")


@pytest.mark.parametrize("allowed", [True, False])
async def test_native_role_tool_returns_its_child_result_after_approval(
    allowed: bool,
    responses: ResponsesFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[NativeToolCall] = []
    ordinary_calls = 0

    def approve(_context: ToolContext[object], call: NativeToolCall) -> bool:
        calls.append(call)
        return allowed

    tool = NativeRoleTool(name="research_topic", role="research", description="Research a topic.")

    @function_tool(name=tool.name)
    def ordinary() -> str:
        nonlocal ordinary_calls
        ordinary_calls += 1
        return "ordinary application result"

    responses.respond(function_call(tool.name, {}, "ordinary"))
    responses.respond(collaboration(tool.name, {"input": "Read the requested record."}, "managed"))
    if allowed:
        responses.respond(assistant_output("managed child result"))
        responses.respond(assistant_output("parent final result"))
    agent = Agent(
        instructions="Call the research tool.",
        model="gpt-5.5",
        provider=provider(responses),
        tools=[
            ordinary,
            NativeAgents(additional_roles=[_role()], managed_tools=[tool], approval=approve),
        ],
    )

    async with timeout(25), ExecutorPeer(tmp_path / "remote").serve() as executor:
        trust(executor, tmp_path, monkeypatch)
        async with agent.session(environment=declaration(executor)) as session:
            root = await session.submit("Research the record.")
            if allowed:
                result = await root.get_result()
                assert result.final_output == "parent final result"
            else:
                with pytest.raises(NativeError, match="interception failed or was denied"):
                    await root.get_result()
            root_turn_id = root.turn_id
            children = [child async for child in session.subagents()]

    assert ordinary_calls == 1
    assert [(call.namespace, call.name) for call in calls] == [("collaboration", tool.name)]
    assert len(children) == int(allowed)
    assert len(responses.requests) == 2 + 2 * int(allowed)
    if not allowed:
        return
    outputs = [
        item
        for item in responses.requests[-1]["input"]
        if item["type"] == "function_call_output" and item["call_id"] == "managed"
    ]
    assert len(outputs) == 1
    assert "managed child result" in json.dumps(outputs[0]["output"])
    child_request = responses.requests[2]
    child_metadata = metadata(child_request, responses.request_headers[2])
    assert child_metadata["agent_name"].startswith("/root/tool_")
    assert child_metadata["root_turn_id"] == root_turn_id
    assert _role().instructions in json.dumps(child_request)


@pytest.mark.parametrize("allowed", [True, False])
async def test_native_role_tool_from_code_mode(
    responses: ResponsesFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, allowed: bool
) -> None:
    calls: list[NativeToolCall] = []
    callback_ids: list[str] = []

    def approve(context: ToolContext[object], call: NativeToolCall) -> bool:
        callback_ids.append(context.call_id)
        calls.append(call)
        return allowed

    role_tool = NativeRoleTool(
        name="research_topic", role="research", description="Research a topic."
    )
    responses.respond(
        code_call(
            'if (typeof tools.collaboration__spawn_agent !== "undefined") {'
            'throw new Error("Built-in collaboration tools must be called directly");'
            "} "
            'text(await tools.collaboration__research_topic({input: "Read the record."}));',
            "managed-code",
        )
    )
    if allowed:
        responses.respond(assistant_output("managed code child result"))
    responses.respond(assistant_output("parent final result"))
    agent = Agent(
        instructions="Use the named research tool from Code Mode.",
        model=NATIVE_ROLE_FIXTURE_MODEL,
        provider=provider(responses),
        programmatic_tool_calling=True,
        tools=[
            NativeAgents(additional_roles=[_role()], managed_tools=[role_tool], approval=approve)
        ],
    )

    async with timeout(25), ExecutorPeer(tmp_path / "remote").serve() as executor:
        trust(executor, tmp_path, monkeypatch)
        async with agent.session(environment=declaration(executor)) as session:
            turn = await session.submit("Research the record.")
            result = await turn.get_result()
            code_outputs = [
                item.get("output")
                for item in responses.requests[-1]["input"]
                if item.get("call_id") == "managed-code" and "output" in item
            ]
            assert result.final_output == "parent final result", (calls, code_outputs)
            assert len([child async for child in session.subagents()]) == int(allowed)

    assert len(calls) == 1
    call = calls[0]
    assert (call.namespace, call.name) == ("collaboration", role_tool.name)
    assert call.route == "programmatic_tool_calling"
    assert call.outer_call_id == "managed-code"
    assert call.call_id is None
    assert call.runtime_tool_call_id
    assert callback_ids == [call.runtime_tool_call_id]
    assert len(responses.requests) == (3 if allowed else 2)
    if allowed:
        assert "managed code child result" in json.dumps(responses.requests[-1]["input"])


async def test_completed_native_role_tool_result_survives_a_root_checkpoint(
    responses: ResponsesFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    role_tool = NativeRoleTool(
        name="research_topic", role="research", description="Research a topic."
    )
    lookup = FunctionTool(
        name="lookup", description="Read a record", input_type=LookupArgs, output_type=str
    )
    responses.respond(collaboration(role_tool.name, {"input": "Read the record."}, "managed"))
    responses.respond(assistant_output("managed result before checkpoint"))
    responses.respond(function_call("lookup", {"key": "A"}, "root-manual"))
    responses.respond(assistant_output("resumed parent result"))
    agent = Agent(
        instructions="Research before requesting the record.",
        model="gpt-5.5",
        provider=provider(responses),
        tools=[NativeAgents(additional_roles=[_role()], managed_tools=[role_tool]), lookup],
    )

    async with timeout(30), ExecutorPeer(tmp_path / "remote").serve() as executor:
        trust(executor, tmp_path, monkeypatch)
        async with agent.session(environment=declaration(executor)) as original:
            root = await original.submit("Read the record.")
            request = await next_request(original.interactions(), lookup)
            assert request.call_id == "root-manual"
            state = SessionState.from_json((await original.suspend()).to_json())
        async with agent.session(state=state, environment=declaration(executor)) as restored:
            pending = await next_request(restored.interactions(), lookup)
            assert pending.call_id == "root-manual"
            await pending.respond("verified")
            continuation = restored.restored_turn
            assert continuation is not None
            assert continuation.turn_id == root.turn_id
            assert (await continuation.get_result()).final_output == "resumed parent result"

    assert len(responses.requests) == 4
    restored_input = json.dumps(responses.requests[-1]["input"])
    assert "managed result before checkpoint" in restored_input
    assert "verified" in restored_input


async def test_cancelling_native_role_tool_retires_its_exact_child(
    responses: ResponsesFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    child_started = asyncio.Event()
    release = asyncio.Event()
    child_output_item = assistant_output("abandoned child output")
    child_output_item["item"]["id"] = "managed-child-held"

    async def hold_child(event: dict[str, Any]) -> None:
        if event.get("item", {}).get("id") == "managed-child-held":
            child_started.set()
            await release.wait()

    responses.before_event = hold_child
    tool = NativeRoleTool(name="research_topic", role="research", description="Research a topic.")
    responses.respond(collaboration(tool.name, {"input": "Wait for the record."}, "managed"))
    responses.respond(child_output_item)
    responses.respond(assistant_output("next root result"))
    agent = Agent(
        instructions="Call the research tool.",
        model="gpt-5.5",
        provider=provider(responses),
        tools=[NativeAgents(additional_roles=[_role()], managed_tools=[tool])],
    )

    async with timeout(25), ExecutorPeer(tmp_path / "remote").serve() as executor:
        trust(executor, tmp_path, monkeypatch)
        async with agent.session(environment=declaration(executor)) as session:
            try:
                root = await session.submit("Research the record.")
                await child_started.wait()
                (child,) = await handles(session, 1)
                await root.cancel()
                with pytest.raises(NativeError, match="interrupted"):
                    await child.get_result()
                following = await (await session.submit("Continue without the tool.")).get_result()
                assert following.final_output == "next root result"
            finally:
                release.set()


@pytest.mark.parametrize("field", ["instructions", "description"])
def test_native_child_declarations_reject_blank_role_text(field: str) -> None:
    values = {"name": "research", "instructions": "Read.", "description": "Read a topic."}
    values[field] = " \n"
    with pytest.raises(ValueError, match=f"Native role {field} must be a nonblank string"):
        NativeRole(**values)


@pytest.mark.parametrize(
    ("value", "error"), [(True, TypeError), (0, ValueError), (sys.maxsize, ValueError)]
)
def test_native_child_declarations_reject_invalid_limits(
    value: Any, error: type[Exception]
) -> None:
    with pytest.raises(error, match="max_children"):
        NativeAgents(additional_roles=[_role()], max_children=value)


@pytest.mark.parametrize(
    ("value", "error"),
    [(True, TypeError), (None, TypeError), (0, ValueError), (sys.maxsize, ValueError)],
)
def test_native_child_depth_requires_a_finite_positive_bound(
    value: Any, error: type[Exception]
) -> None:
    assert NativeAgents(additional_roles=[_role()]).max_depth == 1
    with pytest.raises(error, match="max_depth"):
        NativeAgents(additional_roles=[_role()], max_depth=value)


@pytest.mark.parametrize(
    "options",
    [
        {"interceptor": lambda call: {"action": "continue"}},
        {"interception_timeout": 30},
        {"interception_scope": "thread_tree"},
    ],
)
def test_native_agents_reject_retired_interception_options(options: dict[str, Any]) -> None:
    with pytest.raises(TypeError):
        NativeAgents(**options)


def test_native_agents_approval_requires_a_callable() -> None:
    with pytest.raises(TypeError, match="NativeAgents approval must be callable"):
        NativeAgents(approval="approve")


def test_native_agents_before_step_is_optional_callable_and_private_in_repr() -> None:
    assert NativeAgents().before_step is None
    with pytest.raises(TypeError, match="NativeAgents before_step must be callable"):
        NativeAgents(before_step="select")

    def callback(step):
        return []

    declaration = NativeAgents(before_step=callback)
    assert declaration.before_step is callback
    assert "before_step" not in repr(declaration)


@pytest.mark.parametrize(
    ("settings", "message"),
    [
        ({"reasoning_effort": "  "}, "reasoning_effort must be a nonblank"),
        ({"reasoning_effort": 8}, "reasoning_effort must be a nonblank"),
        ({"reasoning_summary": "concise"}, "supports only reasoning_effort"),
        ({"other": "value"}, "supports only reasoning_effort"),
    ],
)
def test_native_role_settings_reject_unsupported_declarations(settings: Any, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        replace(_role(), model_settings=settings)


@pytest.mark.parametrize("case", ["duplicate", "not-a-role"])
def test_native_child_declarations_require_unique_typed_roles(case: str) -> None:
    if case == "duplicate":
        values = [_role(), replace(_role(), instructions="Other instructions.")]
        error, message = ValueError, "role names must be unique"
    else:
        values = [{"name": "research"}]
        error, message = TypeError, "must contain NativeRole"
    with pytest.raises(error, match=message):
        NativeAgents(additional_roles=values)


@pytest.mark.parametrize("case", ["no-executor", "partial-executor", "two-declarations"])
async def test_native_child_tool_combinations_fail_before_startup(
    case: str, forbid_native_startup: None
) -> None:
    children = NativeAgents()
    tools: list[Any] = [children]
    environment: Environment | None = _executor()
    if case == "no-executor":
        environment = None
        message = "NativeAgents requires an Environment with all three"
    elif case == "partial-executor":
        environment = replace(_executor(), tools=["exec_command"])
        message = "NativeAgents requires an Environment with all three"
    else:
        tools.append(NativeAgents(additional_roles=[replace(_role(), name="other")]))
        message = "exactly one NativeAgents declaration"
    agent = Agent(instructions="Read.", model="gpt-5.5", tools=tools)
    with pytest.raises(ValueError, match=message):
        async with agent.session(environment=environment):
            pytest.fail("Unsupported tool combination opened a Session")


async def test_native_children_still_require_an_explicit_model(
    forbid_native_startup: None,
) -> None:
    agent = Agent(instructions="Read.", tools=[NativeAgents()])
    with pytest.raises(ValueError, match="requires an explicit compatible model"):
        async with agent.session(environment=_executor()):
            pytest.fail("Implicit model opened a native children Session")


async def test_native_child_metadata_rejects_before_remote_io(
    responses: ResponsesFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with timeout(25), ExecutorPeer(tmp_path / "remote").serve() as peer:
        monkeypatch.setenv("SSL_CERT_FILE", str(peer.ca))
        monkeypatch.setenv("SSL_CERT_DIR", str(tmp_path / "empty-cert-directory"))
        agent = Agent(
            instructions="Read.",
            model="unknown-native-child-fixture-model",
            provider=provider(responses),
            tools=[NativeAgents(additional_roles=[_role()])],
        )
        with pytest.raises(RuntimeError, match="selected model metadata is incompatible"):
            async with agent.session(environment=declaration(peer)):
                pytest.fail("Incompatible model opened a Session")
        assert responses.requests == []
        assert peer.attempts == 0
        assert peer.requests == []


async def test_native_defaults_can_spawn_without_an_additional_role(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with (
        timeout(40),
        child_peer(("default",)) as peer,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
    ):
        trust(executor, tmp_path, monkeypatch)
        respond_as_root = peer.response

        def respond() -> list[dict[str, Any]]:
            body = peer.requests[-1]
            if metadata(body, peer.request_headers[-1])["agent_name"] != "/root":
                result = assistant_output("alpha complete")
                result["item"]["id"] = "done-alpha"
                return [result]
            return respond_as_root()

        monkeypatch.setattr(peer, "response", respond)
        agent = Agent(
            instructions="Use a native child for the requested work.",
            model="gpt-5.5",
            provider=peer.provider(websocket=False),
            tools=[NativeAgents()],
        )
        async with agent.session(environment=declaration(executor)) as session:
            root = await session.submit("Read a record.")
            await peer.entered["alpha"].wait()
            assert (await (root).get_result()).final_output == "root complete"
            [child] = await handles(session, 1)
            peer.release["alpha"].set()
            assert await child_output(session, child) == "alpha complete"
            saved = await session.save_state()
            assert len(saved.descendants) == 1


@pytest.mark.requires_native_linux_sandbox
async def test_native_child_updates_reject_without_changing_the_live_session(
    responses: ResponsesFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with timeout(25), ExecutorPeer(tmp_path / "remote").serve() as peer:
        monkeypatch.setenv("SSL_CERT_FILE", str(peer.ca))
        monkeypatch.setenv("SSL_CERT_DIR", str(tmp_path / "empty-cert-directory"))
        executor = declaration(peer)
        children = NativeAgents(additional_roles=[_role()])
        initial_instructions = "retained-native-agent-root-instructions-marker"
        rejected_instructions = "rejected-native-agent-root-instructions-marker"
        agent = Agent(
            instructions=initial_instructions,
            model="gpt-5.5",
            provider=provider(responses),
            tools=[children],
        )
        async with agent.session(environment=executor) as session:
            requests_before = list(peer.requests)
            for update in (
                lambda: session.update(model="unknown-native-child-fixture-model"),
                lambda: session.update(instructions=rejected_instructions),
                lambda: session.update(model_settings={"reasoning_effort": "low"}),
            ):
                with pytest.raises(ValueError, match="settings and instructions cannot change"):
                    await update()
            with pytest.raises(ValueError, match="captured"):
                await session.submit("select", tools=[replace(children, max_children=2)])  # type: ignore[list-item]
            saved = await session.save_state()
            assert saved.source_session_id == session.id
            assert saved.descendants == []
            assert peer.requests == requests_before
            assert responses.requests == []
            responses.respond(assistant_output("no children selected"))
            empty = await session.submit("No children", tools=[])
            assert (await empty.get_result()).final_output == "no children selected"
            assert not declarations(responses.requests[0])
            # An ordinary turn may inspect its display roots without exposing tools.
            # Invalid updates above still had to leave every request unchanged.
            ancestor_markers = {
                (path / ".git").as_uri() for path in (peer.root, *peer.root.parents)
            }
            assert all(
                request["method"] == "fs/getMetadata"
                and request["params"]["path"] in ancestor_markers
                and request["params"] == {"path": request["params"]["path"], "sandbox": None}
                for request in peer.requests[len(requests_before) :]
            )
            assert not [child async for child in session.subagents()]
            responses.respond(assistant_output("still usable"))
            result = await (await session.submit("Read the topic.")).get_result()
            assert result.final_output == "still usable"
            assert len(responses.requests) == 2
            assert ("collaboration", "spawn_agent") in declarations(responses.requests[1])
            encoded = json.dumps(responses.requests[1])
            assert initial_instructions in encoded
            assert rejected_instructions not in encoded
