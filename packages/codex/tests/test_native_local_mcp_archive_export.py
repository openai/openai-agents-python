"""Settled hosted approvals export as historical data, without live authority."""

import json

from agents import Agent, HostedMCP, LocalStorage, NativeProvider, SessionState
from tests._asyncio import timeout
from tests.native_fixtures import assistant_output, responses as responses
from tests.test_native_historical_mcp_archive import next_request


async def test_settled_local_hosted_approval_exports_without_replay_authority(responses, tmp_path):
    approval = {
        "type": "mcp_approval_request",
        "id": "settled-approval",
        "server_label": "archive",
        "name": "lookup",
        "arguments": "{}",
        "caller": {"type": "direct"},
    }
    denied = {**approval, "id": "denied-before"}
    for item in (
        denied,
        assistant_output("denied history"),
        approval,
        assistant_output("source complete"),
        approval,
        assistant_output("restored complete"),
    ):
        responses.respond(
            {"type": "response.output_item.done", "item": item}
            if item is approval or item is denied
            else item
        )
    hosted = HostedMCP(
        tool_config={
            "type": "mcp",
            "server_label": "archive",
            "server_url": "https://hosted-mcp.invalid/archive",
            "require_approval": "always",
        }
    )
    directory = tmp_path / "source"
    async with (
        timeout(30),
        NativeProvider(
            base_url=responses.url,
            api_key="synthetic-local-mcp",
            supports_websockets=False,
        ) as provider,
    ):
        agent = Agent(
            instructions="Ask before work.",
            model="gpt-5.2",
            tools=[hosted],
            provider=provider,
        )
        async with agent.session(storage=LocalStorage(directory)) as source:
            denied_turn = await source.submit("Earlier denied hosted work.")
            denied_request = await next_request(source.interactions(), hosted)
            await denied_request.respond({"approve": False})
            assert (await denied_turn.get_result()).final_output == "denied history"
            turn = await source.submit("Request hosted work.")
            request = await next_request(source.interactions(), hosted)
            await request.respond({"approve": True})
            assert (await turn.get_result()).final_output == "source complete"
            before = await source.history_snapshot()
            assert any(
                item.get("type") == "mcp_approval_response" and item.get("approve") is True
                for item in before.items
            )
            saved = SessionState.from_json((await source.save_state()).to_json())
            assert await source.history_snapshot() == before
        directory.rename(tmp_path / "removed-source")
        async with agent.session(state=saved) as restored:
            restored_history = await restored.history_snapshot()
            for item in before.items:
                if item.get("type") == "message":
                    assert item in restored_history.items
            turn = await restored.submit("Continue from history.")
            request = await next_request(restored.interactions(), hosted)
            assert request.call_id == "settled-approval"
            assert len(responses.requests) == 5
            await request.respond({"approve": False})
            assert (await turn.get_result()).final_output == "restored complete"
        incoming = responses.requests[4]["input"]
        decisions = [item for item in incoming if item.get("type") == "mcp_approval_response"]
        assert len(decisions) == 1
        assert decisions[0]["approval_request_id"] == "denied-before"
        assert decisions[0]["approve"] is False
        assert "Historical MCP approval data" in json.dumps(incoming)
        assert "settled-approval" in json.dumps(incoming)
