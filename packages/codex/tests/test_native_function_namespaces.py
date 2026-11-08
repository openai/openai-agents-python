"""Public function namespaces use native captured slots and qualified selection."""

from __future__ import annotations

from dataclasses import FrozenInstanceError, replace

import pytest

from agents import (
    Agent,
    FunctionTool,
    HttpMCP,
    NativeAgents,
    NativeError,
    ToolContext,
    ToolRef,
    function_tool,
)
from tests._asyncio import timeout
from tests.native_fixtures import (
    ResponsesFixture,
    assistant_output,
    function_call,
    responses as responses,
    tool_output,
)
from tests.test_native_agent import provider
from tests.test_native_manual_tools import LookupArgs
from tests.test_native_mcp_tools import declarations


def namespaced_call(namespace, name, arguments, call_id):
    event = function_call(name, arguments, call_id)
    event["item"]["namespace"] = namespace
    return event


def lookup_for(namespace, effects, *, name="lookup"):
    @function_tool(name=name, namespace=namespace, namespace_description=f"Read {namespace}.")
    async def lookup(ctx: ToolContext[list[str]], key: str) -> str:
        assert ctx.context is effects
        assert ctx.call.tool == ToolRef(name, namespace)
        effects.append(namespace + ":" + key)
        return namespace + ":" + key

    return lookup


async def test_equal_short_names_dispatch_distinct_captured_namespaces(responses: ResponsesFixture):
    effects = []
    books = lookup_for("books", effects)
    users = lookup_for("users", effects)
    agent = Agent(
        instructions="Use qualified lookups.", tools=[books, users], provider=provider(responses)
    )
    agent.validate()
    assert books.ref == ToolRef("lookup", "books")
    with pytest.raises(FrozenInstanceError):
        books.namespace = "other"  # type: ignore[misc]
    async with timeout(15), agent.session(context=effects) as session:
        # Replacing the source Agent cannot change the already captured roster.
        object.__setattr__(agent, "tools", (replace(books, namespace="other"),))
        responses.respond(
            namespaced_call("books", "lookup", {"key": "A"}, "book"),
            namespaced_call("users", "lookup", {"key": "B"}, "user"),
        )
        responses.respond(assistant_output("complete"))
        assert (await (await session.submit("read")).get_result()).final_output == "complete"
        assert sorted(effects) == ["books:A", "users:B"]
        assert set(declarations(responses.requests[0])) == {
            ("books", "lookup"),
            ("users", "lookup"),
        }
        assert tool_output(responses.requests[1], "book") == "books:A"
        assert tool_output(responses.requests[1], "user") == "users:B"
        published = list(responses.requests[0].get("tools", []))
        for item in responses.requests[0]["input"]:
            if item.get("type") == "additional_tools":
                published.extend(item["tools"])
        namespaces = {tool["name"]: tool for tool in published}
        assert namespaces["books"]["description"] == "Read books."
        assert namespaces["users"]["description"] == "Read users."


async def test_before_step_selects_one_qualified_function(responses: ResponsesFixture):
    effects = []
    books, users = (lookup_for(name, effects) for name in ("books", "users"))
    responses.respond(namespaced_call("books", "lookup", {"key": "A"}, "selected"))
    responses.respond(assistant_output("selected"))
    async with (
        timeout(15),
        Agent(
            instructions="Use selected tools.", tools=[books, users], provider=provider(responses)
        ).session(context=effects) as session,
    ):
        result = await (
            await session.submit("read", before_step=lambda turn, *, step_index: [books.ref])
        ).get_result()
        assert result.final_output == "selected"
        assert effects == ["books:A"]
        assert set(declarations(responses.requests[0])) == {("books", "lookup")}


def test_duplicate_qualified_identity_and_native_names_still_fail():
    effects = []
    first = lookup_for("books", effects)
    with pytest.raises((ValueError, NativeError), match="Duplicate"):
        Agent(instructions="test", tools=[first, replace(first)]).validate()
    for namespace, name in [("functions", "exec"), ("web", "run"), ("image_gen", "imagegen")]:
        with pytest.raises((ValueError, NativeError), match="reserved"):
            Agent(
                instructions="test", tools=[replace(first, namespace=namespace, name=name)]
            ).validate()
    remote = HttpMCP(name="catalog", url="https://example.invalid/mcp", tools=["lookup"])
    with pytest.raises(ValueError, match="shares a ToolRef"):
        Agent(
            instructions="test", tools=[replace(first, namespace="mcp__catalog"), remote]
        ).validate()
    with pytest.raises(ValueError, match="collaboration namespace"):
        Agent(
            instructions="test",
            tools=[replace(first, namespace="collaboration", name="spawn_agent"), NativeAgents()],
        ).validate()
    flattened = replace(first, namespace=None, namespace_description=None, name="books__lookup")
    # Code Mode checks the effective step roster, which can select either member.
    Agent(instructions="test", tools=[first, flattened], programmatic_tool_calling=True).validate()
    assert effects == []


@pytest.mark.parametrize("reverse", [False, True])
async def test_native_agents_reserves_collaboration_before_opening(responses, reverse):
    lookup = lookup_for("collaboration", [])
    native_agents = NativeAgents()
    tools = [native_agents, lookup] if reverse else [lookup, native_agents]
    agent = Agent(instructions="Use lookup.", tools=tools, provider=provider(responses))
    with pytest.raises(ValueError, match="collaboration namespace.*lookup"):
        agent.validate()
    with pytest.raises(ValueError, match="collaboration namespace.*lookup"):
        async with agent.session():
            pytest.fail("A native-owned namespace was admitted")
    with pytest.raises(ValueError, match="collaboration namespace.*lookup"):
        async with Agent(instructions="Overrides.", provider=agent.provider).session(tools=tools):
            pytest.fail("Opening overrides admitted a native-owned namespace")
    assert responses.requests == []


async def test_collaboration_function_without_native_agents_keeps_its_qualified_identity(responses):
    effects = []
    lookup = lookup_for("collaboration", effects)
    agent = Agent(instructions="Use lookup.", tools=[lookup], provider=provider(responses))
    agent.validate()
    responses.respond(namespaced_call("collaboration", "lookup", {"key": "A"}, "lookup"))
    responses.respond(assistant_output("selected"))
    async with timeout(15), agent.session(context=effects) as session:
        result = await (await session.submit("read")).get_result()
    assert result.final_output == "selected"
    assert effects == ["collaboration:A"]


@pytest.mark.parametrize("namespace", ["", " ", 1])
def test_invalid_namespace_fails_at_declaration(namespace):
    with pytest.raises(ValueError, match="namespace"):
        function_tool(namespace=namespace)(lambda: "unused")


def test_namespace_description_and_manual_namespace_are_explicit():
    with pytest.raises(ValueError, match="requires namespace"):
        function_tool(namespace_description="description")(lambda: "unused")
    with pytest.raises(TypeError, match="namespace_description"):
        function_tool(namespace="books", namespace_description=1)(lambda: "unused")
    with pytest.raises(ValueError, match="registered Python callback"):
        FunctionTool(
            name="lookup", description="", input_type=LookupArgs, output_type=str, namespace="books"
        )


async def test_idle_restore_rebinds_only_matching_qualified_declarations(
    responses: ResponsesFixture,
):
    effects = []
    lookup = lookup_for("books", effects)
    agent = Agent(instructions="Use lookup.", tools=[lookup], provider=provider(responses))
    responses.respond(assistant_output("saved"))
    async with timeout(15), agent.session(context=effects) as session:
        await (await session.submit("start")).get_result()
        state = await session.save_state()
    responses.respond(namespaced_call("books", "lookup", {"key": "restored"}, "restored"))
    responses.respond(assistant_output("restored"))
    async with timeout(15), agent.session(state=state, context=effects) as restored:
        assert (await (await restored.submit("continue")).get_result()).final_output == "restored"
    assert effects == ["books:restored"]
    changed = Agent(
        instructions="Use lookup.",
        tools=[replace(lookup, namespace="other")],
        provider=provider(responses),
    )
    with pytest.raises(RuntimeError, match="checkpoint tool mode|managed specs|tool"):
        async with changed.session(state=state, context=effects):
            pytest.fail("A saved namespace cannot grant a missing current declaration")
    assert effects == ["books:restored"]


async def test_two_functions_in_one_namespace_keep_separate_native_slots(responses):
    effects = []
    tools = [lookup_for("books", effects, name=name) for name in ("lookup", "read")]
    responses.respond(
        namespaced_call("books", "lookup", {"key": "A"}, "lookup"),
        namespaced_call("books", "read", {"key": "B"}, "read"),
    )
    responses.respond(assistant_output("two slots"))
    async with (
        timeout(15),
        Agent(instructions="read", tools=tools, provider=provider(responses)).session(
            context=effects
        ) as session,
    ):
        assert (await (await session.submit("read")).get_result()).final_output == "two slots"
        assert sorted(effects) == ["books:A", "books:B"]
        assert set(declarations(responses.requests[0])) == {("books", "lookup"), ("books", "read")}
        assert tool_output(responses.requests[1], "lookup") == "books:A"
        assert tool_output(responses.requests[1], "read") == "books:B"


async def test_explicit_default_namespace_keeps_native_identity_and_duplicate_normalization(
    responses,
):
    calls = []

    @function_tool(namespace="functions")
    async def lookup(ctx: ToolContext[None]) -> str:
        calls.append(ctx.call.tool)
        return "default namespace"

    bare = replace(lookup, namespace=None)
    with pytest.raises((ValueError, NativeError), match="Duplicate"):
        Agent(instructions="test", tools=[bare, lookup]).validate()
    responses.respond(function_call("lookup", {}, "default"))
    responses.respond(assistant_output("complete"))
    async with (
        timeout(15),
        Agent(
            instructions="test", tools=[lookup], provider=provider(responses)
        ).session() as session,
    ):
        assert (await (await session.submit("test")).get_result()).final_output == "complete"
        assert calls == [ToolRef("lookup", "functions")]
