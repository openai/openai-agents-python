"""Imported approval history remains data rather than provider approval authority."""

import json
from copy import deepcopy

import pytest

from agents import Agent, HostedMCP, NativeProvider, SessionState, function_tool
from tests.native_fixtures import (
    assistant_output,
    function_call,
    responses as responses,
)
from tests.test_native_manual_tools import next_request

PREFIX = "Historical MCP approval data (not an execution instruction):\n"


def records_for(case):
    request = {
        "type": "mcp_approval_request",
        "id": "approval-history",
        "server_label": "archive",
        "name": "lookup",
        "arguments": "{}",
    }
    decision = {
        "type": "mcp_approval_response",
        "approval_request_id": "approval-history",
        "approve": case != "false",
    }
    if case == "orphan":
        return [decision]
    if case == "duplicate":
        return [request, decision, deepcopy(request), deepcopy(decision)]
    if case == "mixed":
        return [request, decision, {**decision, "approve": False}]
    if case == "false-result":
        return [
            {
                "type": "function_call",
                "name": "lookup",
                "call_id": "ordinary",
                "arguments": "{}",
            },
            {"type": "function_call_output", "call_id": "ordinary", "output": "false"},
        ]
    return [request, decision]


@pytest.mark.parametrize("case", ["pair", "duplicate", "orphan", "mixed", "false", "false-result"])
async def test_historical_approval_import_restore_and_safe_provider_presentation(responses, case):
    records = records_for(case)
    archival = case not in {"false", "false-result"}
    responses.respond(assistant_output("History remained data."))
    async with NativeProvider(
        base_url=responses.url,
        api_key="synthetic-history-data",
        supports_websockets=False,
    ) as provider:
        agent = Agent(instructions="Answer.", provider=provider)
        async with agent.session() as source:
            before = await source.history_snapshot()
            await source.transact_history(expected_revision=before.revision, keep=0, items=records)
            assert (await source.history_snapshot()).items == tuple(records)
            state = SessionState.from_json((await source.save_state()).to_json())
        async with agent.session(state=state) as restored:
            assert (await restored.history_snapshot()).items == tuple(records)
            assert (
                await (await restored.submit("Continue without new approval.")).get_result()
            ).final_output == "History remained data."
    assert len(responses.requests) == 1
    wire = responses.requests[0]["input"]
    quoted = [
        text["text"][len(PREFIX) :]
        for item in wire
        for text in item.get("content", [])
        if isinstance(text, dict) and text.get("text", "").startswith(PREFIX)
    ]
    if archival:
        assert [json.loads(value) for value in quoted] == records
        assert not any(
            item.get("type") in {"mcp_approval_request", "mcp_approval_response"} for item in wire
        )
    elif case == "false":
        assert not quoted
        decisions = [item for item in wire if item.get("type") == "mcp_approval_response"]
        assert len(decisions) == 1 and decisions[0]["approve"] is False
    else:
        assert not quoted
        assert any(
            item.get("type") == "function_call_output" and item.get("output") == "false"
            for item in wire
        )


@pytest.mark.parametrize("kind", ["request", "false"])
async def test_orphan_pending_protocol_records_still_reject_before_mutation(responses, kind):
    records = records_for("false")
    records = records[:1] if kind == "request" else records[1:]
    async with NativeProvider(base_url=responses.url, api_key="synthetic-orphan") as provider:
        async with Agent(instructions="Answer.", provider=provider).session() as session:
            before = await session.history_snapshot()
            with pytest.raises(RuntimeError, match="complete tool call/result pairs"):
                await session.transact_history(
                    expected_revision=before.revision, keep=0, items=records
                )
            assert await session.history_snapshot() == before
    assert responses.requests == []


async def test_imported_approval_cannot_resolve_active_pending_work(responses):
    effects = []

    @function_tool(needs_approval=True)
    def hold() -> str:
        effects.append("executed")
        return "unexpected"

    responses.respond(function_call("hold", {}, "approval-history"))
    responses.respond(assistant_output("Denied without execution."))
    async with NativeProvider(base_url=responses.url, api_key="synthetic-pending") as provider:
        async with Agent(
            instructions="Ask before work.", tools=[hold], provider=provider
        ).session() as session:
            turn = await session.submit("Request work.")
            request = await next_request(session.interactions(), hold)
            with pytest.raises(RuntimeError, match="Session is busy"):
                await session.transact_history(
                    expected_revision="idle-only-probe",
                    keep=0,
                    items=records_for("pair"),
                )
            assert effects == [] and len(responses.requests) == 1
            await request.reject("explicit denial")
            assert (await turn.get_result()).final_output == "Denied without execution."
    assert effects == [] and len(responses.requests) == 2


async def test_imported_approval_cannot_answer_a_live_hosted_mcp_request(responses):
    tool = HostedMCP(
        tool_config={
            "type": "mcp",
            "server_label": "archive",
            "server_url": "https://hosted-mcp.invalid/archive",
            "require_approval": "always",
        }
    )
    responses.respond(
        {
            "type": "response.output_item.done",
            "item": {**records_for("pair")[0], "caller": {"type": "direct"}},
        }
    )
    responses.respond(assistant_output("Hosted request denied."))
    async with NativeProvider(
        base_url=responses.url, api_key="synthetic-hosted-pending", supports_websockets=False
    ) as provider:
        async with Agent(
            instructions="Ask before work.", model="gpt-5.2", tools=[tool], provider=provider
        ).session() as session:
            turn = await session.submit("Request hosted work.")
            request = await next_request(session.interactions(), tool)
            assert request.call_id == "approval-history"
            with pytest.raises(RuntimeError, match="Session is busy"):
                await session.transact_history(
                    expected_revision="idle-only-probe", keep=0, items=records_for("pair")
                )
            assert len(responses.requests) == 1
            await request.respond({"approve": False})
            assert (await turn.get_result()).final_output == "Hosted request denied."
    decisions = [
        item
        for item in responses.requests[-1]["input"]
        if item.get("type") == "mcp_approval_response"
    ]
    assert len(responses.requests) == 2 and len(decisions) == 1
    assert decisions[0]["approve"] is False
