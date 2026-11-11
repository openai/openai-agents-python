"""A single native voice owner reclaims completed ordinary function calls."""

from contextlib import aclosing

from agents import Agent, NativeProvider, function_tool
from agents.realtime import RealtimeConfig, RealtimeResponse, RealtimeToolCall
from tests._asyncio import timeout
from tests.native_fixtures import text_response
from tests.test_native_realtime import RealtimePeer, realtime_peer as realtime_peer


async def test_sequential_native_realtime_functions_exceed_native_retention_capacities(
    realtime_peer: RealtimePeer,
) -> None:
    peer = realtime_peer
    call_count = 1030
    executed: list[int] = []
    accepted: list[str] = []

    @function_tool
    async def echo(index: int) -> str:
        executed.append(index)
        return f"accepted-{index}"

    peer.responses.respond(*text_response("after-voice-capacity", "Session remains reusable."))
    async with (
        timeout(120),
        NativeProvider(
            base_url=peer.responses.url,
            api_key="synthetic-realtime-capacity-key",
            supports_websockets=False,
        ) as provider,
        Agent(instructions="Use echo.", tools=[echo], provider=provider).session() as session,
    ):
        async with (
            session.realtime(
                config=RealtimeConfig(model="fixture-realtime", tools=(echo,))
            ) as owner,
            aclosing(owner.events()) as events,
        ):
            assert peer.response_count == 0
            for index in range(call_count):
                call_id = f"capacity-call-{index}"
                start = len(peer.frames)
                await peer.function("echo", {"index": index}, call_id)
                tool_done = response_done = False
                async for event in events:
                    if (
                        isinstance(event, RealtimeToolCall)
                        and event.call_id == call_id
                        and event.status in ("completed", "failed")
                    ):
                        assert event.status == "completed"
                        output = await peer.frame(
                            lambda frame, call_id=call_id: (
                                frame.get("item", {}).get("type") == "function_call_output"
                                and frame["item"].get("call_id") == call_id
                            ),
                            after=start,
                        )
                        assert output["item"]["output"] == f"accepted-{index}"
                        item = dict(output["item"])
                        item.setdefault("id", f"capacity-output-{index}")
                        assert peer.socket is not None
                        for kind in ("conversation.item.added", "conversation.item.done"):
                            await peer.socket.send_json({"type": kind, "item": item})
                        tool_done = True
                    elif (
                        isinstance(event, RealtimeResponse)
                        and event.response_id == f"voice-response-{index + 1}"
                        and event.status in ("done", "cancelled")
                    ):
                        assert event.status == "done"
                        response_done = True
                    if tool_done and response_done:
                        break
                else:
                    raise AssertionError(
                        f"Voice ended after {len(accepted)} accepted function calls"
                    )
                assert peer.response_count == index + 1
                accepted.append(call_id)
            assert executed == list(range(call_count))
            assert len(accepted) == call_count
            assert peer.connections == 1
            assert not peer.responses.requests
        followup = await session.submit("Continue after voice.")
        assert (await followup.get_result()).final_output == "Session remains reusable."
        assert len(peer.responses.requests) == 1
