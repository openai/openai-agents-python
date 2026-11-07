"""MCP argument policy uses normalized detached views on both native routes."""

from __future__ import annotations

import json
from copy import deepcopy

import pytest

from agents import Agent, Allow, HttpMCP, ReplaceArguments
from tests._asyncio import timeout
from tests.native_fixtures import ResponsesFixture, assistant_output, responses as responses
from tests.test_native_agent import provider
from tests.test_native_code_mode import code_call
from tests.test_native_mcp_tools import Peer, mcp_call


class NestedArgumentsPeer(Peer):
    async def handle(self, request):
        response = await super().handle(request)
        if (await request.json()).get("method") == "tools/list":
            assert response.text is not None
            payload = json.loads(response.text)
            for tool in payload["result"]["tools"]:
                tool["inputSchema"]["properties"]["nested"] = {
                    "type": "object",
                    "properties": {"keep": {"type": "boolean"}},
                    "required": ["keep"],
                    "additionalProperties": False,
                }
            response.text = json.dumps(payload)
        return response


@pytest.mark.parametrize("in_code", [False, True], ids=["direct", "code"])
@pytest.mark.parametrize("replace_arguments", [False, True], ids=["allow", "replace"])
async def test_mcp_policies_receive_detached_original_and_effective_arguments(
    responses: ResponsesFixture, in_code, replace_arguments
):
    original = {"key": "original", "nested": {"keep": True}}
    replacement = {"key": "effective", "nested": {"keep": True}}
    expected = deepcopy(replacement if replace_arguments else original)
    policy_order = []
    peer = NestedArgumentsPeer(names=("search",))

    def before(context, call):
        policy_order.append("before")
        assert dict(context.original_arguments) == original
        assert dict(context.effective_arguments) == original
        assert isinstance(call.input, str) is not in_code
        with pytest.raises(TypeError):
            context.effective_arguments["key"] = "changed"
        context.original_arguments["nested"]["keep"] = False
        context.effective_arguments["nested"]["keep"] = False
        return ReplaceArguments(replacement) if replace_arguments else Allow()

    def approval(context, call):
        policy_order.append("approval")
        assert dict(context.original_arguments) == original
        assert dict(context.effective_arguments) == expected
        assert (json.loads(call.input) if isinstance(call.input, str) else call.input) == expected
        context.original_arguments["nested"]["keep"] = False
        context.effective_arguments["nested"]["keep"] = False
        replacement["nested"]["keep"] = False
        return True

    if in_code:
        event = code_call(
            'text(await tools.mcp__records__search({key:"original",nested:{keep:true}}));',
            "normalized-call",
        )
    else:
        event = mcp_call("search", "mcp__records", "normalized-call")
        event["item"]["arguments"] = json.dumps(original)
    responses.respond(event)
    responses.respond(assistant_output("done"))
    async with timeout(20), peer.serve() as url:
        agent = Agent(
            instructions="Search the selected record.",
            provider=provider(responses),
            programmatic_tool_calling=in_code,
            tools=[
                HttpMCP(
                    name="records",
                    url=url,
                    tools=["search"],
                    approval_mode="preapproved",
                    before=before,
                    approval=approval,
                )
            ],
        )
        async with agent.session() as session:
            result = await (await session.submit("Search.")).get_result()
            assert result.final_output == "done"
        assert policy_order == ["before", "approval"]
        assert [call["arguments"] for call in peer.calls] == [expected]
