from __future__ import annotations

import pytest

from agents import Agent, NativeProvider, function_tool
from tests._asyncio import timeout
from tests.native_fixtures import (
    assistant_output,
    function_call,
    responses as responses,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("hooked", [False, True])
async def test_after_model_lite_http_direct_tool(responses, hooked):
    responses.respond(function_call("lookup", {}, "lite-call"))
    responses.respond(assistant_output("Complete."))
    order = []

    @function_tool
    def lookup() -> str:
        order.append("tool")
        return "value"

    def after_model(event):
        order.append("complete")
        assert event.items

    async with (
        timeout(15),
        NativeProvider(
            base_url=responses.url, api_key="synthetic", supports_websockets=False
        ) as provider,
    ):
        agent = Agent(
            instructions="Use lookup and answer.",
            provider=provider,
            tools=[lookup],
            after_model=after_model if hooked else None,
        )
        assert (await agent.run("Question.")).final_output == "Complete."
    assert len(responses.requests) == 2
    assert any(item["type"] == "additional_tools" for item in responses.requests[0]["input"])
    assert order == (["complete", "tool", "complete"] if hooked else ["tool"])


async def test_after_model_programmatic_websocket_refuses_before_local_tools(responses):
    from agents import HostedProgrammaticToolCalling, NativeError

    effects = []

    @function_tool(allowed_callers=["programmatic"])
    def lookup() -> str:
        effects.append("tool")
        return "value"

    async with NativeProvider(
        base_url=responses.url,
        api_key="synthetic",
        supports_websockets=True,
    ) as provider:
        agent = Agent(
            instructions="Use lookup.",
            provider=provider,
            model="gpt-5.5",
            tools=[lookup, HostedProgrammaticToolCalling()],
            model_settings={"request_transport": "websocket"},
            after_model=lambda _event: None,
        )
        with pytest.raises(
            NativeError,
            match="can finish before local tool execution",
        ):
            async with timeout(15):
                await agent.run("Question.")
    assert effects == []
    assert responses.requests == []


async def test_after_model_standard_http_programmatic_tools_finish_before_dispatch(
    responses,
):
    from agents import HostedProgrammaticToolCalling

    order = []
    program = {
        "type": "program",
        "id": "program_item",
        "call_id": "parent",
        "code": "lookup()",
    }
    call = {
        "type": "function_call",
        "id": "fc_lookup",
        "call_id": "lookup",
        "name": "lookup",
        "arguments": "{}",
        "caller": {"type": "program", "caller_id": "parent"},
    }
    responses.respond(
        *({"type": "response.output_item.done", "item": item} for item in (program, call))
    )
    responses.respond(assistant_output("Complete."))

    @function_tool(allowed_callers=["programmatic"])
    def lookup() -> str:
        order.append("tool")
        return "value"

    def after_model(event):
        order.append("complete")
        assert event.items

    async with NativeProvider(
        base_url=responses.url,
        api_key="synthetic",
        supports_websockets=False,
        request_max_retries=0,
        stream_max_retries=0,
    ) as provider:
        agent = Agent(
            instructions="Use lookup and answer.",
            model="gpt-5.5",
            provider=provider,
            tools=[lookup, HostedProgrammaticToolCalling()],
            after_model=after_model,
        )
        async with timeout(15):
            assert (await agent.run("Question.")).final_output == "Complete."
    assert len(responses.requests) == 2
    assert order == ["complete", "tool", "complete"]
