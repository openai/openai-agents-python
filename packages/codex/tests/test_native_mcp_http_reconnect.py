"""Reconnect a native streamable HTTP MCP client within a live turn."""

from aiohttp import web

from agents import Agent, HttpMCP
from tests._asyncio import timeout
from tests.native_fixtures import (
    ResponsesFixture,
    assistant_output,
    function_call,
    responses as responses,
    tool_output,
)
from tests.test_native_agent import provider
from tests.test_native_mcp_tools import Peer


class DisconnectingPeer(Peer):
    disconnect_key = "end-stream"

    async def handle(self, request: web.Request) -> web.Response:
        body = await request.json()
        # Peer records the tool effect before constructing its normal reply.
        reply = await super().handle(request)
        if (
            body["method"] != "tools/call"
            or body["params"]["arguments"]["key"] != self.disconnect_key
        ):
            return reply

        # End the POST response without a final JSON-RPC message or a resumable event ID.
        response = web.Response(body=b": effect recorded\n\n", content_type="text/event-stream")
        response.enable_chunked_encoding()
        return response


async def test_native_http_reconnect_does_not_replay_uncertain_call(
    responses: ResponsesFixture,
) -> None:
    peer = DisconnectingPeer(annotations={"readOnlyHint": False})
    uncertain, later = peer.disconnect_key, "after-reconnect"
    for key in (uncertain, later):
        event = function_call("search", {"key": key}, key)
        event["item"]["namespace"] = "mcp__remote"
        responses.respond(event)
    responses.respond(assistant_output("done"))

    async with timeout(30), peer.serve() as url:
        descriptor = HttpMCP(name="remote", url=url, tools=["search"], approval_mode="preapproved")
        async with Agent(
            instructions="Use the selected tool.", tools=[descriptor], provider=provider(responses)
        ).session() as session:
            turn = await session.submit("Run the two marked calls in sequence.")
            assert (await turn.get_result()).final_output == "done"
            assert len(responses.requests) == 3
            error = tool_output(responses.requests[1], uncertain)
            assert "Transport closed" in error, error
            output = tool_output(responses.requests[2], later)
            assert peer.label in output, output
            assert [call["arguments"]["key"] for call in peer.calls] == [uncertain, later]
            assert [
                request["method"]
                for request in peer.requests
                if request["method"] in {"initialize", "tools/call"}
            ] == ["initialize", "tools/call", "initialize", "tools/call"]
