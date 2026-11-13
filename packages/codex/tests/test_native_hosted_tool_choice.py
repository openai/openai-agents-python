"""Explicit hosted selectors through actual native HTTP and portable state."""

import pytest

from agents import (
    Agent,
    FileSearch,
    HostedMCP,
    NativeProvider,
    SessionState,
    ToolRef,
    function_tool,
)
from tests.native_fixtures import assistant_output, function_call, responses as responses


async def test_native_explicit_cache_mode_preserves_ttl_when_cleared(responses):
    for _ in range(3):
        responses.respond(assistant_output("done"))
    async with NativeProvider(
        base_url=responses.url, api_key="synthetic-cache-controls"
    ) as provider:
        agent = Agent(
            model="fixture-cache-controls",
            instructions="Use the captured cache settings.",
            provider=provider,
        )
        async with agent.session() as session:
            await (await session.submit("Provider default.")).get_result()
            await session.update(
                model_settings={"prompt_cache_mode": "explicit", "prompt_cache_ttl": "30m"}
            )
            await (await session.submit("Explicit cache.")).get_result()
            await session.update(model_settings={"prompt_cache_mode": None})
            await (await session.submit("Retain the TTL.")).get_result()
    assert [r.get("prompt_cache_options") for r in responses.requests] == [
        None,
        {"mode": "explicit", "ttl": "30m"},
        {"ttl": "30m"},
    ]


async def test_native_hosted_choice_updates_and_restores(responses):
    for _ in range(4):
        responses.respond(assistant_output("done"))
    async with NativeProvider(
        base_url=responses.url, api_key="synthetic-hosted-choice"
    ) as provider:
        agent = Agent(
            model="fixture-hosted-choice",
            instructions="Inspect the captured synthetic declarations.",
            provider=provider,
            tools=[FileSearch(vector_store_ids=["vs_fixture"])],
            model_settings={"tool_choice": {"type": "file_search"}},
        )
        async with agent.session() as session:
            await (await session.submit("Select the hosted declaration.")).get_result()
            saved = SessionState.from_json((await session.save_state()).to_json())
            await session.update(model_settings={"tool_choice": "auto"})
            await (await session.submit("Clear the preference.")).get_result()
        async with agent.session(state=saved) as restored:
            await (await restored.submit("Restore the captured selector.")).get_result()
        async with agent.session(state=saved) as restored:
            await restored.update(model_settings={"tool_choice": "auto"})
            await (await restored.submit("Use the explicit current override.")).get_result()
    assert [request["tool_choice"] for request in responses.requests] == [
        {"type": "file_search"},
        "auto",
        {"type": "file_search"},
        "auto",
    ]
    assert any(tool["type"] == "file_search" for tool in responses.requests[0]["tools"])


async def test_native_hosted_choice_refuses_same_name_function(responses):
    effects = []

    @function_tool
    def file_search() -> str:
        """Return a synthetic function result."""
        effects.append("called")
        return "function"

    async with NativeProvider(
        base_url=responses.url, api_key="synthetic-hosted-choice"
    ) as provider:
        agent = Agent(
            model="fixture-hosted-choice",
            instructions="Inspect the captured synthetic declarations.",
            provider=provider,
            tools=[file_search],
            model_settings={"tool_choice": {"type": "file_search"}},
        )
        async with agent.session() as session:
            with pytest.raises(Exception, match="(?i)hosted.*catalog"):
                await (await session.submit("Select.")).get_result()
    assert not responses.requests
    assert not effects


async def test_native_hosted_choice_switches_to_captured_function(responses):
    effects = []

    @function_tool
    def lookup() -> str:
        """Return the captured lookup result."""
        effects.append("lookup")
        return "found"

    responses.respond(assistant_output("initial"))
    responses.respond(function_call("lookup", {}, "choice-lookup"))
    responses.respond(assistant_output("done"))
    async with NativeProvider(
        base_url=responses.url, api_key="synthetic-hosted-choice"
    ) as provider:
        agent = Agent(
            model="fixture-hosted-choice",
            instructions="Inspect.",
            provider=provider,
            tools=[FileSearch(vector_store_ids=["vs_fixture"]), lookup],
            model_settings={"tool_choice": {"type": "file_search"}},
        )
        async with agent.session() as session:
            await (await session.submit("Start.")).get_result()
            await session.update(model_settings={"tool_choice": ToolRef("lookup")})
            await (await session.submit("Use lookup.")).get_result()
    assert responses.requests[1]["tool_choice"] == "required"
    assert effects == ["lookup"]


@pytest.mark.parametrize("mode", ["auto", "none"])
async def test_native_managed_profile_clears_hosted_choice(responses, mode):
    responses.respond(assistant_output("initial"))
    responses.respond(assistant_output("updated"))
    async with NativeProvider(
        base_url=responses.url, api_key="synthetic-hosted-choice"
    ) as provider:
        worker = Agent(
            tools=[FileSearch(vector_store_ids=["vs_fixture"])],
            name="Worker",
            model="fixture-hosted-choice",
            instructions="Work.",
            provider=provider,
        )
        agent = Agent(
            name="Root",
            model="fixture-hosted-choice",
            instructions="Inspect.",
            provider=provider,
            tools=[FileSearch(vector_store_ids=["vs_fixture"])],
            handoffs=[worker],
            model_settings={"tool_choice": {"type": "file_search"}},
        )
        async with agent.session() as session:
            await (await session.submit("Start.")).get_result()
            await session.update(model_settings={"tool_choice": mode})
            await (await session.submit("Use the current mode.")).get_result()
    assert responses.requests[1]["tool_choice"] == mode


async def test_native_mcp_choice_keeps_read_only_filter_without_name_list(responses):
    responses.respond(assistant_output("done"))
    async with NativeProvider(
        base_url=responses.url, api_key="synthetic-hosted-choice"
    ) as provider:
        agent = Agent(
            model="fixture-hosted-choice",
            instructions="Inspect.",
            provider=provider,
            tools=[
                HostedMCP(
                    tool_config={
                        "type": "mcp",
                        "server_label": "docs",
                        "server_url": "https://docs.invalid/mcp",
                        "require_approval": "never",
                        "allowed_tools": {"read_only": True},
                    }
                )
            ],
            model_settings={
                "tool_choice": {"type": "mcp", "server_label": "docs", "name": "lookup"}
            },
        )
        async with agent.session() as session:
            await (await session.submit("Choose lookup.")).get_result()
    request = responses.requests[0]
    assert request["tool_choice"] == {"type": "mcp", "server_label": "docs", "name": "lookup"}
    assert next(tool for tool in request["tools"] if tool["type"] == "mcp")["allowed_tools"] == {
        "read_only": True
    }


@pytest.mark.parametrize("declared", [True, False])
async def test_provider_programmatic_selector_requires_its_hosted_declaration(responses, declared):
    from agents import CodeInterpreter, HostedProgrammaticToolCalling, NativeError

    responses.respond(assistant_output("provider completed"))
    async with NativeProvider(
        base_url=responses.url, api_key="synthetic-provider-programmatic"
    ) as provider:
        agent = Agent(
            model="gpt-5.5",
            instructions="Use the configured provider declaration.",
            provider=provider,
            tools=[
                CodeInterpreter(container="cntr_fixture", allowed_callers=["programmatic"]),
                *([HostedProgrammaticToolCalling()] if declared else []),
            ],
            model_settings={"tool_choice": {"type": "programmatic_tool_calling"}},
        )
        if declared:
            assert (await agent.run("Inspect the synthetic program.")).final_output == (
                "provider completed"
            )
        else:
            with pytest.raises(NativeError, match="outside the final offered catalog"):
                await agent.run("Inspect the synthetic program.")
    if not declared:
        assert not responses.requests
        return
    [request] = responses.requests
    assert request["tool_choice"] == {"type": "programmatic_tool_calling"}
    assert {"type": "programmatic_tool_calling"} in request["tools"]
    assert {
        "type": "code_interpreter",
        "container": "cntr_fixture",
        "allowed_callers": ["programmatic"],
    } in request["tools"]


@pytest.mark.parametrize("companion", ["absent", "direct-only", "search", "programmatic-only"])
async def test_provider_programmatic_tool_requires_companion(responses, companion):
    from agents import CodeInterpreter, HostedProgrammaticToolCalling, NativeError, ToolSearch

    responses.respond(assistant_output("provider completed"))
    declaration = HostedProgrammaticToolCalling()
    interpreter = CodeInterpreter(
        container="cntr_fixture",
        allowed_callers=["direct"] if companion == "direct-only" else ["programmatic"],
    )

    @function_tool(defer_loading=True)
    def lookup() -> str:
        return "ok"

    tools = {
        "absent": [declaration],
        "direct-only": [declaration, interpreter],
        "search": [declaration, ToolSearch(execution="server"), lookup],
        "programmatic-only": [interpreter],
    }[companion]
    async with NativeProvider(
        base_url=responses.url, api_key="synthetic-provider-companion"
    ) as provider:
        agent = Agent(
            model="gpt-5.5", instructions="Use a provider tool.", tools=tools, provider=provider
        )
        if companion == "search":
            assert (await agent.run("Use the tool.")).final_output == "provider completed"
        else:
            expected = (
                "Programmatic-only tools require a provider programmatic tool declaration"
                if companion == "programmatic-only"
                else "requires an offered programmatic caller or tool search"
            )
            with pytest.raises(NativeError, match=expected):
                await agent.run("Use the tool.")
    assert len(responses.requests) == (1 if companion == "search" else 0)


async def test_provider_program_and_result_replay_on_the_next_native_turn(responses):
    from agents import CodeInterpreter, HostedProgrammaticToolCalling

    program = {
        "type": "program",
        "id": "program_item",
        "call_id": "program-call",
        "code": "lookup()",
        "fingerprint": "synthetic-fingerprint",
    }
    output = {
        "type": "program_output",
        "id": "program_output_item",
        "call_id": "program-call",
        "status": "completed",
        "result": "native program result",
    }
    responses.respond(
        {"type": "response.output_item.done", "item": program},
        {"type": "response.output_item.done", "item": output},
        assistant_output("provider completed"),
    )
    responses.respond(assistant_output("used previous program"))
    responses.respond(
        {"type": "response.output_item.done", "item": program},
        {"type": "response.output_item.done", "item": output},
        assistant_output("produced program"),
    )
    async with NativeProvider(
        base_url=responses.url, api_key="synthetic-provider-program-replay"
    ) as provider:
        agent = Agent(
            model="gpt-5.5",
            instructions="Use the provider program.",
            provider=provider,
            tools=[
                CodeInterpreter(container="cntr_fixture", allowed_callers=["programmatic"]),
                HostedProgrammaticToolCalling(),
            ],
        )
        async with agent.session() as session:
            assert (await (await session.submit("Run the program.")).get_result()).final_output == (
                "provider completed"
            )
            assert (await (await session.submit("What happened?")).get_result()).final_output == (
                "used previous program"
            )
            history = [
                item
                async for item in session.items()
                if item["type"] in {"program", "program_output"}
            ]
        result = await agent.run("Run again.", return_items=True)
        assert result.items is not None
        produced = [
            item.item for item in result.items if item.item["type"] in {"program", "program_output"}
        ]
    assert [item["provider_data"] for item in history] == [program, output]
    assert [item["provider_data"] for item in produced] == [program, output]
    assert [item["call_id"] for item in history] == ["program-call", "program-call"]
    replay = [
        item
        for item in responses.requests[1]["input"]
        if item["type"] in {"program", "program_output"}
    ]
    assert replay == [program, output]


@pytest.mark.parametrize("explicit", [False, True])
async def test_provider_program_is_admitted_only_by_its_actual_hosted_prompt_catalog(
    responses, explicit
):
    from agents import NativeError

    responses.respond(
        {
            "type": "response.output_item.done",
            "item": {
                "type": "program",
                "id": "program_prompt",
                "call_id": "p",
                "code": "return 1",
            },
        },
        {
            "type": "response.output_item.done",
            "item": {
                "type": "program_output",
                "id": "program_output_prompt",
                "call_id": "p",
                "status": "completed",
                "result": "1",
            },
        },
        assistant_output("provider completed"),
    )
    async with NativeProvider(
        base_url=responses.url, api_key="synthetic-prompt-program"
    ) as provider:
        agent = Agent(
            provider=provider,
            hosted_prompt={"id": "pmpt_programmatic"},
            **({"tools": []} if explicit else {}),
        )
        if explicit:
            with pytest.raises(NativeError, match="without an offered programmatic tool"):
                await agent.run("Use the saved prompt.")
            assert responses.requests[0]["tools"] == []
        else:
            assert (await agent.run("Use the saved prompt.")).final_output == "provider completed"
            assert "tools" not in responses.requests[0]
    assert responses.requests[0]["prompt"] == {"id": "pmpt_programmatic"}
