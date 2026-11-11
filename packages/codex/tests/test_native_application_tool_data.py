"""Application tool data follows the accepted native result."""

import json

import pytest
from pydantic import JsonValue

from agents import (
    Agent,
    ApplicationToolOutput,
    FunctionTool,
    HttpMCP,
    InteractionExpiredError,
    MCPResultContext,
    NativeProvider,
    SessionState,
    ToolOutput,
    function_tool,
)
from agents.observation import ToolFinished
from tests._asyncio import timeout
from tests.native_fixtures import (
    ResponsesFixture,
    assistant_output,
    function_call,
    responses as responses,
)
from tests.test_native_code_mode import code_call, code_output
from tests.test_native_manual_tools import LookupArgs, next_request
from tests.test_native_mcp_tools import Peer, mcp_call


@pytest.mark.parametrize("finish_turn", [False, True])
async def test_native_application_data_is_detached_and_survives_checkpoint(
    responses: ResponsesFixture, finish_turn: bool
) -> None:
    effects: list[str] = []
    application_data: dict[str, JsonValue] = {"receipt": "private-application-receipt"}

    @function_tool(finish_turn=finish_turn)
    def lookup() -> ToolOutput:
        effects.append("lookup")
        output = ToolOutput.text("public value")
        output.application_data = application_data.copy()
        return output

    responses.respond(function_call("lookup", {}, "lookup-call"))
    if not finish_turn:
        responses.respond(assistant_output("assistant result"))
    agent = Agent(
        instructions="Use lookup.",
        model="gpt-5.5",
        tools=[lookup],
        provider=NativeProvider(base_url=responses.url, api_key="synthetic-application-tool-data"),
    )
    async with timeout(30), agent.session() as session:
        turn = await session.submit("go")
        completed = await turn.get_result()
        assert completed.final_output == ("public value" if finish_turn else "assistant result")
        (record,) = completed.application_tool_outputs
        assert isinstance(record, ApplicationToolOutput)
        assert (record.submission_id, record.turn_id, record.call_id, record.name) == (
            turn.submission_id,
            turn.turn_id,
            "lookup-call",
            "lookup",
        )
        assert record.application_data == application_data
        record.application_data["receipt"] = "changed"
        assert turn.application_tool_outputs[0].application_data == application_data
        (detailed,) = [
            event
            async for event in turn.events(include_execution_details=True)
            if isinstance(event, ToolFinished)
        ]
        assert detailed.output is not None
        observed_data = detailed.output.application_data
        assert observed_data is not None
        assert observed_data == application_data
        observed_data["receipt"] = "changed in observation"
        assert completed.application_tool_outputs[0].application_data == application_data
        (default,) = [event async for event in turn.events() if isinstance(event, ToolFinished)]
        assert default.output is None
        saved = SessionState.from_json((await session.save_state()).to_json())
        assert saved.application_tool_outputs[0].application_data == application_data
        assert "private-application-receipt" not in json.dumps(saved.history)

    responses.respond(assistant_output("restored"))
    async with timeout(30), agent.session(state=saved) as restored:
        follow_up = await restored.submit("Continue without a tool.")
        restored_result = await follow_up.get_result()
        assert restored_result.final_output == "restored"
        assert restored_result.application_tool_outputs == ()
        exported = await restored.save_state()
        assert exported.application_tool_outputs == saved.application_tool_outputs

    assert effects == ["lookup"]
    assert "private-application-receipt" not in json.dumps(responses.requests)


async def test_programmatic_function_data_uses_the_accepted_nested_result(
    responses: ResponsesFixture,
) -> None:
    application_data: dict[str, JsonValue] = {"receipt": "private-nested-receipt"}

    @function_tool
    def lookup() -> ToolOutput:
        output = ToolOutput.text("value for the code cell")
        output.application_data = application_data
        return output

    responses.respond(code_call("text(await tools.lookup({}));", "cell"))
    responses.respond(assistant_output("complete"))
    agent = Agent(
        instructions="Use lookup from code.",
        programmatic_tool_calling=True,
        tools=[lookup],
        provider=NativeProvider(base_url=responses.url, api_key="synthetic-application-tool-data"),
    )
    async with timeout(45), agent.session() as session:
        turn = await session.submit("go")
        completed = await turn.get_result()
        assert completed.final_output == "complete"
        assert "value for the code cell" in code_output(responses.requests[1], "cell")
        (record,) = completed.application_tool_outputs
        (finished,) = [
            event
            async for event in turn.events(include_execution_details=True)
            if isinstance(event, ToolFinished) and event.name == "lookup"
        ]
        assert (record.submission_id, record.turn_id, record.call_id, record.name) == (
            turn.submission_id,
            turn.turn_id,
            finished.call_id,
            "lookup",
        )
        assert record.application_data == application_data
        saved = SessionState.from_json((await session.save_state()).to_json())
        assert saved.application_tool_outputs == [record]
        assert "private-nested-receipt" not in json.dumps(saved.history)

    assert "private-nested-receipt" not in json.dumps(responses.requests)


async def test_manual_application_data_belongs_to_the_accepted_reply(
    responses: ResponsesFixture,
) -> None:
    lookup = FunctionTool(
        name="lookup", description="Look up a key", input_type=LookupArgs, output_type=ToolOutput
    )
    responses.respond(function_call("lookup", {"key": "blue"}, "manual-call"))
    responses.respond(assistant_output("complete"))
    agent = Agent(
        instructions="Use lookup.",
        model="gpt-5.5",
        tools=[lookup],
        provider=NativeProvider(base_url=responses.url, api_key="synthetic-application-tool-data"),
    )
    async with timeout(30), agent.session() as session:
        turn = await session.submit("go")
        request = await next_request(session.interactions(), lookup)
        output = ToolOutput.text("public manual reply")
        output.application_data = {"receipt": "private-manual-receipt"}
        await request.respond(output)
        output.application_data = {"receipt": "unaccepted-manual-receipt"}
        with pytest.raises(InteractionExpiredError):
            await request.respond(output)
        completed = await turn.get_result()
        (record,) = completed.application_tool_outputs
        assert (record.call_id, record.name) == ("manual-call", "lookup")
        assert record.application_data == {"receipt": "private-manual-receipt"}

    assert "private-manual-receipt" not in json.dumps(responses.requests)


@pytest.mark.parametrize("with_transform", [False, True])
async def test_mcp_extracts_application_data_from_the_effective_result(
    responses: ResponsesFixture, with_transform: bool
) -> None:
    peer = Peer(names=("search",), label="raw MCP reply")
    seen: list[tuple[str, dict]] = []

    def redact(_context, value):
        value["content"][0]["text"] = "redacted MCP reply"
        return value

    async def extract(context: MCPResultContext, value: dict) -> dict:
        assert dict(context.arguments) == {"key": "43"}
        seen.append((context.call_id, value["structuredContent"]))
        assert value["content"][0]["text"] == (
            "redacted MCP reply" if with_transform else "raw MCP reply"
        )
        value["content"][0]["text"] = "private-mcp-receipt"
        return {"receipt": "private-mcp-receipt"}

    responses.respond(mcp_call("search", "mcp__docs", "mcp-call"))
    responses.respond(assistant_output("complete"))
    async with timeout(30), peer.serve() as url:
        agent = Agent(
            instructions="Use search.",
            model="gpt-5.5",
            tools=[
                HttpMCP(
                    name="docs",
                    url=url,
                    tools=["search"],
                    transform_result=redact if with_transform else None,
                    extract_application_data=extract,
                )
            ],
            provider=NativeProvider(
                base_url=responses.url, api_key="synthetic-application-tool-data"
            ),
        )
        async with agent.session() as session:
            turn = await session.submit("go")
            completed = await turn.get_result()
            (record,) = completed.application_tool_outputs
            assert (record.call_id, record.name, record.namespace) == (
                "mcp-call",
                "search",
                "mcp__docs",
            )
            assert record.application_data == {"receipt": "private-mcp-receipt"}
            saved = await session.save_state()
            assert "private-mcp-receipt" not in json.dumps(saved.history)

    assert seen == [("mcp-call", {"receipt": "raw MCP reply"})]
    assert "private-mcp-receipt" not in json.dumps(responses.requests)
