"""A native handoff selects the captured application model backend."""

from __future__ import annotations

import json

import pytest

from agents import Agent, ModelRequest, ModelResponse, NativeProvider, PromptedOutput
from agents.state import SessionState
from tests._asyncio import timeout
from tests.native_fixtures import assistant_output, function_call


class SourceBackend:
    def __init__(self) -> None:
        self.requests: list[ModelRequest] = []

    def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        return ModelResponse(
            [function_call("transfer_to_destination", {}, "source-handoff")["item"]]
        )


class DestinationBackend:
    def __init__(self) -> None:
        self.requests: list[ModelRequest] = []

    def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        return ModelResponse([assistant_output("destination response")["item"]])


async def test_native_handoff_switches_backend_and_retains_both_leases() -> None:
    source = SourceBackend()
    destination = DestinationBackend()
    async with (
        timeout(20),
        NativeProvider(model_backend=source) as source_provider,
        NativeProvider(model_backend=destination) as destination_provider,
    ):
        target = Agent(
            name="destination", instructions="Destination only.", provider=destination_provider
        )
        agent = Agent(instructions="Source only.", provider=source_provider, handoffs=[target])
        async with agent.session(context={"owner": "workflow"}) as session:
            with pytest.raises(RuntimeError, match="open or opening"):
                await destination_provider.aclose()
            turn = await session.submit("Route.")
            assert (await turn.get_result()).final_output == "destination response"
            saved = await session.save_state()
        async with agent.session(
            state=SessionState.from_json(saved.to_json()), context={"owner": "workflow"}
        ) as restored:
            turn = await restored.submit("Continue.")
            assert (await turn.get_result()).final_output == "destination response"
        assert len(source.requests) == 1
        assert len(destination.requests) == 2
        assert destination.requests[0].context == {"owner": "workflow"}
        assert "Destination only." in json.dumps(destination.requests[0].body["input"])
        assert any(
            item.get("type") == "function_call_output" and item.get("call_id") == "source-handoff"
            for item in destination.requests[0].body["input"]
        )


async def test_native_handoff_repairs_with_destination_backend() -> None:
    source = SourceBackend()

    class RepairBackend(DestinationBackend):
        def complete(self, request: ModelRequest) -> ModelResponse:
            self.requests.append(request)
            text = "invalid JSON" if len(self.requests) == 1 else '{"value": 3}'
            return ModelResponse([assistant_output(text)["item"]])

    destination = RepairBackend()
    async with (
        timeout(20),
        NativeProvider(model_backend=source) as source_provider,
        NativeProvider(model_backend=destination) as destination_provider,
    ):
        target = Agent(
            name="destination",
            instructions="Return a value.",
            provider=destination_provider,
            output_type=PromptedOutput(dict[str, int], max_retries=1),
        )
        agent = Agent(instructions="Route.", provider=source_provider, handoffs=[target])
        async with agent.session() as session:
            turn = await session.submit("Route.")
            assert (await turn.get_result()).final_output == {"value": 3}
        assert len(source.requests) == 1
        assert len(destination.requests) == 2
        assert "Correct the supplied JSON" in json.dumps(destination.requests[1].body["input"])


async def test_failed_handoff_open_releases_earlier_provider_leases() -> None:
    source = NativeProvider(model_backend=SourceBackend())
    first = NativeProvider(model_backend=DestinationBackend())
    closed = NativeProvider(model_backend=DestinationBackend())
    await closed.aclose()
    async with timeout(20), source, first:
        agent = Agent(
            instructions="Route.",
            provider=source,
            handoffs=[
                Agent(name="first", instructions="First.", provider=first),
                Agent(name="closed", instructions="Closed.", provider=closed),
            ],
        )
        with pytest.raises(RuntimeError, match="NativeProvider is closed"):
            async with agent.session():
                pytest.fail("Unexpected session publication")
        await source.aclose()
        await first.aclose()
