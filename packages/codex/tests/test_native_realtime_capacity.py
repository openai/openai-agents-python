"""A single native voice owner reclaims completed ordinary function calls."""

from contextlib import aclosing

from agents import Agent, NativeProvider, function_tool
from agents.realtime import RealtimeConfig, RealtimeToolCall
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
            for index in range(call_count):
                call_id = f"capacity-call-{index}"
                await peer.function("echo", {"index": index}, call_id)
                async for event in events:
                    if (
                        isinstance(event, RealtimeToolCall)
                        and event.call_id == call_id
                        and event.status in ("completed", "failed")
                    ):
                        assert event.status == "completed"
                        break
                else:
                    raise AssertionError(
                        f"Voice ended after {len(accepted)} accepted function calls"
                    )
                output = await peer.frame(
                    lambda frame, call_id=call_id: (
                        frame.get("item", {}).get("type") == "function_call_output"
                        and frame["item"].get("call_id") == call_id
                    )
                )
                assert output["item"]["output"] == f"accepted-{index}"
                accepted.append(call_id)
            assert executed == list(range(call_count))
            assert len(accepted) == call_count
            assert peer.connections == 1
            assert not peer.responses.requests
        followup = await session.submit("Continue after voice.")
        assert (await followup.get_result()).final_output == "Session remains reusable."
        assert len(peer.responses.requests) == 1
