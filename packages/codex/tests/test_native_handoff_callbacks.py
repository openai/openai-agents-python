import asyncio
import json
from copy import deepcopy

import pytest
from pydantic import ValidationError

from agents import (
    Agent,
    Allow,
    CustomStorage,
    Deny,
    Handoff,
    LocalStorage,
    SessionState,
    ToolContext,
    function_tool,
)
from tests._asyncio import timeout
from tests.native_fixtures import (
    assistant_output,
    function_call,
    responses as responses,
    tool_output,
)
from tests.native_storage_fixtures import MemorySessionStore
from tests.test_native_agent_observation import provider


async def test_canonical_resume_cannot_activate_callback_only_target(responses):
    allow_transfer = False
    effects = []

    def admit(_):
        return Allow() if allow_transfer else Deny("transfer denied")

    @function_tool(name="route", before=admit)
    def route() -> str:
        effects.append("route")
        return "transferred"

    @function_tool
    def privileged() -> str:
        effects.append("privileged")
        return "privileged effect"

    target = Agent(name="Privileged", instructions="privileged agent", tools=[privileged])
    agent = Agent(
        name="Source",
        instructions="source agent",
        provider=provider(responses),
        handoffs=[Handoff(target, route)],
    )
    backend = MemorySessionStore()
    storage = CustomStorage(backend)
    async with agent.session(storage=storage) as session:
        identity = session.id
        responses.respond(assistant_output("source stays"))
        await (await session.submit("stay on source")).get_result()
        root_history = json.loads(backend._payloads[identity])["items"]
        allow_transfer = True
        responses.respond(function_call("route", {}, "accepted"))
        responses.respond(assistant_output("target reached"))
        await (await session.submit("accept control transfer")).get_result()
        assert await session.get_active_agent() is target
        assert effects == ["route"]
    document = json.loads(backend._payloads[identity])
    publication = next(
        row
        for row in reversed(document["items"])
        if row.get("metadata", {}).get("sdk_agent_profile")
    )
    document["items"] = root_history + [publication]
    backend._payloads[identity] = json.dumps(document)
    allow_transfer = False
    effects.clear()
    with pytest.raises(RuntimeError, match="(?i)callback"):
        async with agent.session(storage=storage, resume=identity) as session:
            assert await session.get_active_agent() is target
            responses.respond(function_call("privileged", {}, "effect"))
            responses.respond(assistant_output("forged target ran"))
            await (await session.submit("run privileged tool")).get_result()
    assert effects == []


def request_instructions(request):
    return "\n".join(
        part["text"]
        for item in request["input"]
        if item.get("role") == "developer" and item.get("type") == "message"
        for part in item["content"]
        if part.get("type") == "input_text"
    )


async def test_callback_handoff_rejects_code_only_authority_before_sampling(responses):
    @function_tool(allowed_callers=("code",))
    def route() -> str:
        pytest.fail("The callback must not run for a code-only handoff declaration")

    agent = Agent(
        instructions="Route the request.",
        provider=provider(responses),
        handoffs=[Handoff(Agent(instructions="Resolve the request."), route)],
    )
    async with timeout(20):
        with pytest.raises(ValueError, match="Handoff callback must allow direct model calls"):
            async with agent.session():
                pass
    assert responses.requests == []


async def test_typed_handoff_keeps_callback_schema_context_and_acknowledgement(responses):
    application = {"account": "captured"}
    effects = []

    @function_tool(name="route_issue", description="Issue routing", allowed_callers=("direct",))
    def route(context: ToolContext[dict[str, str]], ticket: int) -> str:
        assert context.context is application
        effects.append(ticket)
        return f"accepted-ticket-{ticket}"

    target = Agent[dict[str, str], str](instructions="TARGET_ONLY resolve the ticket")
    agent = Agent[dict[str, str], str](
        instructions="SOURCE_ONLY route the ticket",
        provider=provider(responses),
        handoffs=[Handoff(target, route)],
    )
    responses.respond(function_call("route_issue", {"ticket": 7}, "route-7"))
    responses.respond(assistant_output("resolved"))
    result = await agent.run("Route ticket 7", context=application)
    assert result.final_output == "resolved"
    assert effects == [7]
    assert len(responses.requests) == 2
    native_tools = responses.requests[0].get("tools")
    if native_tools is None:
        native_tools = [
            tool
            for item in responses.requests[0]["input"]
            if item.get("type") == "additional_tools"
            for namespace in item["tools"]
            for tool in namespace.get("tools", [namespace])
        ]
    spec = next(tool for tool in native_tools if tool["name"] == "route_issue")
    assert spec["description"] == "Issue routing"
    assert spec["parameters"]["properties"]["ticket"]["type"] == "integer"
    assert "SOURCE_ONLY" in request_instructions(responses.requests[0])
    assert "TARGET_ONLY" in request_instructions(responses.requests[1])
    assert tool_output(responses.requests[1], "route-7") == "accepted-ticket-7"


async def test_denied_callback_releases_same_turn_transfer_reservation(responses):
    effects = []

    def admit(context):
        return (
            Deny("Use the next ticket") if context.effective_arguments["ticket"] == 1 else Allow()
        )

    @function_tool(name="route_issue", before=admit)
    def route(ticket: int) -> str:
        effects.append(ticket)
        return f"accepted-ticket-{ticket}"

    agent = Agent(
        instructions="SOURCE_ONLY route the ticket",
        provider=provider(responses),
        handoffs=[Handoff(Agent(instructions="TARGET_ONLY resolve the ticket"), route)],
    )
    responses.respond(function_call("route_issue", {"ticket": 1}, "denied"))
    responses.respond(function_call("route_issue", {"ticket": 2}, "accepted"))
    responses.respond(assistant_output("resolved"))
    assert (await agent.run("Route the ticket")).final_output == "resolved"
    assert effects == [2]
    assert len(responses.requests) == 3
    assert "SOURCE_ONLY" in request_instructions(responses.requests[1])
    assert "TARGET_ONLY" in request_instructions(responses.requests[2])
    assert tool_output(responses.requests[1], "denied") == "Use the next ticket"
    assert tool_output(responses.requests[2], "accepted") == "accepted-ticket-2"


async def test_conflicting_transfers_admit_only_one_callback_effect(responses):
    effects = []

    @function_tool
    def route_a(ticket: int) -> str:
        effects.append("A")
        return f"A-{ticket}"

    @function_tool
    def route_b(ticket: int) -> str:
        effects.append("B")
        return f"B-{ticket}"

    agent = Agent(
        instructions="SOURCE_ONLY route once",
        provider=provider(responses),
        handoffs=[
            Handoff(Agent(instructions="TARGET_A_ONLY resolve"), route_a),
            Handoff(Agent(instructions="TARGET_B_ONLY resolve"), route_b),
        ],
    )
    responses.respond(
        function_call("route_a", {"ticket": 1}, "route-a"),
        function_call("route_b", {"ticket": 2}, "route-b"),
    )
    responses.respond(assistant_output("resolved"))
    assert (await agent.run("Choose one destination")).final_output == "resolved"
    assert len(effects) == 1
    assert len(responses.requests) == 2
    assert f"TARGET_{effects[0]}_ONLY" in request_instructions(responses.requests[1])


async def test_invalid_payload_never_enters_callback_or_target(responses):
    effects = []

    @function_tool
    def route(ticket: int) -> str:
        effects.append(ticket)
        return "accepted"

    agent = Agent(
        instructions="SOURCE_ONLY route",
        provider=provider(responses),
        handoffs=[Handoff(Agent(instructions="TARGET_ONLY resolve"), route)],
    )
    responses.respond(function_call("route", {"ticket": "invalid"}, "invalid"))
    async with timeout(30), agent.session() as session:
        with pytest.raises(ValidationError):
            await (await session.submit("Route")).get_result()
    assert effects == []
    assert len(responses.requests) == 1


async def test_transfer_waits_for_callback_and_its_finally(responses):
    entered = asyncio.Event()
    release = asyncio.Event()
    cleaning = asyncio.Event()
    cleaned = asyncio.Event()

    @function_tool
    async def route(ticket: int) -> str:
        entered.set()
        try:
            await release.wait()
            return f"accepted-{ticket}"
        finally:
            cleaning.set()
            await cleaned.wait()

    target = Agent(instructions="TARGET_ONLY resolve")
    agent = Agent(
        instructions="SOURCE_ONLY route",
        provider=provider(responses),
        handoffs=[Handoff(target, route)],
    )
    responses.respond(function_call("route", {"ticket": 7}, "awaited"))
    responses.respond(assistant_output("resolved"))
    async with timeout(30), agent.session() as session:
        turn = await session.submit("Route")
        waiter = asyncio.create_task(turn.get_result())
        try:
            await entered.wait()
            assert await session.get_active_agent() is agent
            assert len(responses.requests) == 1
            release.set()
            await cleaning.wait()
            assert not waiter.done()
            assert await session.get_active_agent() is agent
            assert len(responses.requests) == 1
            cleaned.set()
            assert (await waiter).final_output == "resolved"
            assert await session.get_active_agent() is target
            assert tool_output(responses.requests[1], "awaited") == "accepted-7"
        finally:
            release.set()
            cleaned.set()
            await asyncio.gather(waiter, return_exceptions=True)


async def test_cancellation_joins_callback_cleanup_without_transfer(responses):
    entered = asyncio.Event()
    cleaning = asyncio.Event()
    cleaned = asyncio.Event()

    @function_tool
    async def route() -> str:
        entered.set()
        try:
            await asyncio.Event().wait()
            return "unreachable"
        finally:
            cleaning.set()
            await cleaned.wait()

    agent = Agent(
        instructions="SOURCE_ONLY route",
        provider=provider(responses),
        handoffs=[Handoff(Agent(instructions="TARGET_ONLY resolve"), route)],
    )
    responses.respond(function_call("route", {}, "cancelled"))
    async with timeout(30), agent.session() as session:
        turn = await session.submit("Route")
        waiter = asyncio.create_task(turn.get_result())
        stopping = None
        try:
            await entered.wait()
            stopping = asyncio.create_task(turn.cancel())
            await cleaning.wait()
            assert not stopping.done()
            assert not waiter.done()
            cleaned.set()
            await stopping
            result = await asyncio.gather(waiter, return_exceptions=True)
            assert isinstance(result[0], BaseException)
            assert await session.get_active_agent() is agent
            assert len(responses.requests) == 1
        finally:
            cleaned.set()
            if stopping is None and not waiter.done():
                stopping = asyncio.create_task(turn.cancel())
            await asyncio.gather(*([stopping] if stopping else []), waiter, return_exceptions=True)


@pytest.mark.parametrize("subset", [False, True])
async def test_reordered_bindings_keep_callback_but_replacement_is_rejected(responses, subset):
    effects = []

    @function_tool
    def helper() -> str:
        return "ordinary"

    @function_tool
    def route() -> str:
        effects.append("original")
        return "original acknowledgement"

    @function_tool(name="route")
    def replacement() -> str:
        effects.append("replacement")
        return "wrong"

    target = Agent(instructions="TARGET_ONLY resolve")
    agent = Agent(
        instructions="SOURCE_ONLY route",
        tools=[helper],
        provider=provider(responses),
        handoffs=[Handoff(target, route)],
    )
    responses.respond(function_call("route", {}, "reordered"))
    responses.respond(assistant_output("resolved"))
    async with timeout(30), agent.session() as session:
        with pytest.raises(ValueError, match="captured handoff callback"):
            await session.update(tools=[replacement, helper])
        assert not effects and not responses.requests
        await session.update(tools=[route, helper])
        if subset:
            await session.update(tools=[route])
        assert (await (await session.submit("Route")).get_result()).final_output == "resolved"
        assert await session.get_active_agent() is target
    assert effects == ["original"]
    assert tool_output(responses.requests[1], "reordered") == "original acknowledgement"


async def test_another_namespace_with_the_same_name_does_not_transfer(responses):
    effects = []

    @function_tool(name="route", namespace="other")
    def ordinary() -> str:
        effects.append("other")
        return "ordinary"

    @function_tool(name="route")
    def transfer() -> str:
        effects.append("handoff")
        return "transferred"

    agent = Agent(
        instructions="SOURCE_ONLY route",
        tools=[ordinary],
        provider=provider(responses),
        handoffs=[Handoff(Agent(instructions="TARGET_ONLY resolve"), transfer)],
    )
    other = function_call("route", {}, "ordinary")
    other["item"]["namespace"] = "other"
    responses.respond(other)
    responses.respond(function_call("route", {}, "transfer"))
    responses.respond(assistant_output("resolved"))
    async with timeout(30):
        assert (await agent.run("Route")).final_output == "resolved"
    assert effects == ["other", "handoff"]
    assert "SOURCE_ONLY" in request_instructions(responses.requests[1])
    assert "TARGET_ONLY" in request_instructions(responses.requests[2])


async def test_portable_settled_state_cannot_activate_callback_only_target(responses):
    allow_transfer = False
    effects = []

    def admit(_):
        return Allow() if allow_transfer else Deny("transfer denied")

    @function_tool(name="route", before=admit)
    def route(ticket: int) -> str:
        effects.append(("route", ticket))
        return "transferred"

    @function_tool
    def privileged() -> str:
        effects.append(("privileged",))
        return "privileged effect"

    target = Agent(name="Privileged", instructions="privileged agent", tools=[privileged])
    agent = Agent(
        name="Source",
        instructions="source agent",
        provider=provider(responses),
        handoffs=[Handoff(target, route)],
    )

    async with agent.session() as session:
        responses.respond(function_call("route", {"ticket": 1}, "denied"))
        responses.respond(assistant_output("source stays"))
        assert (
            await (await session.submit("attempt denied transfer")).get_result()
        ).final_output == "source stays"
        assert await session.get_active_agent() is agent
        assert effects == []
        root_state = (await session.save_state()).model_dump(mode="json")

        allow_transfer = True
        responses.respond(function_call("route", {"ticket": 2}, "accepted"))
        responses.respond(assistant_output("target reached"))
        assert (
            await (await session.submit("accept the control transfer")).get_result()
        ).final_output == "target reached"
        assert await session.get_active_agent() is target
        assert effects == [("route", 2)]
        target_state = (await session.save_state()).model_dump(mode="json")

    allow_transfer = False
    effects.clear()
    async with agent.session(state=SessionState.model_validate(root_state)) as session:
        assert await session.get_active_agent() is agent
    forged = deepcopy(target_state)
    publication = next(
        entry
        for entry in reversed(target_state["history"])
        if entry.get("metadata", {}).get("sdk_agent_profile")
    )
    forged["history"] = deepcopy(root_state["history"]) + [deepcopy(publication)]
    with pytest.raises(RuntimeError, match="(?i)callback"):
        async with agent.session(state=SessionState.model_validate(forged)) as session:
            assert await session.get_active_agent() is target
            responses.respond(function_call("privileged", {}, "effect"))
            responses.respond(assistant_output("forged target ran"))
            assert (
                await (await session.submit("run privileged tool")).get_result()
            ).final_output == "forged target ran"
            assert effects == [("privileged",)]
    assert effects == []


@pytest.mark.parametrize("transport", ["clear", "configured"])
async def test_local_storage_preserves_the_accepted_callback_target(responses, tmp_path, transport):
    calls = []

    @function_tool
    def route() -> str:
        calls.append("accepted")
        return "transfer accepted"

    header = "x-synthetic-profile-route"
    old_route, new_route = "synthetic-old-route", "synthetic-current-route"
    target = Agent(
        name="Target",
        instructions="target agent",
        model_settings={"extra_headers": {header: old_route}},
    )
    agent = Agent(
        name="Source",
        instructions="source agent",
        provider=provider(responses),
        handoffs=[Handoff(target, route)],
    )
    storage = LocalStorage(tmp_path / "sessions")
    responses.respond(function_call("route", {}, "route-accepted"))
    responses.respond(assistant_output("transferred"))
    async with agent.session(storage=storage) as session:
        result = await (await session.submit("run a genuine transfer")).get_result()
        assert result.final_output == "transferred"
        assert await session.get_active_agent() is target
        session_id = session.id
        assert responses.request_headers[-1][header] == old_route

    target.model_settings = (
        {"extra_headers": {header: new_route}} if transport == "configured" else {}
    )
    responses.respond(assistant_output("resumed target"))
    async with agent.session(storage=storage, resume=session_id) as session:
        assert await session.get_active_agent() is target
        result = await (await session.submit("continue the accepted target")).get_result()
        assert result.final_output == "resumed target"
        current_header = responses.request_headers[-1].get(header)
        assert current_header == (new_route if transport == "configured" else None)
    assert calls == ["accepted"]
    journals = tuple(storage.path.rglob("rollout-*.jsonl"))
    assert journals
    for journal in journals:
        persisted = journal.read_text()
        assert old_route not in persisted
        assert new_route not in persisted


async def test_static_handoff_remains_portable(responses):
    target = Agent(name="Target", instructions="target agent")
    agent = Agent(
        name="Source",
        instructions="source agent",
        provider=provider(responses),
        handoffs=[target],
    )
    responses.respond(function_call("transfer_to_target", {}, "direct"))
    responses.respond(assistant_output("transferred"))
    async with agent.session() as session:
        result = await (await session.submit("transfer directly")).get_result()
        assert result.final_output == "transferred"
        assert await session.get_active_agent() is target
        payload = (await session.save_state()).model_dump_json()

    async with agent.session(state=SessionState.model_validate_json(payload)) as session:
        assert await session.get_active_agent() is target
