"""Run model selection without changing the admitted Agent defaults."""

import asyncio
import json
from pathlib import Path

import pytest

from agents import Agent, ModelRequest, ModelResponse, NativeProvider
from tests._asyncio import timeout


def model_names():
    catalog = json.loads(
        (
            Path(__file__).parents[1] / ".upstream/codex/codex-rs/models-manager/models.json"
        ).read_text()
    )
    return [model["slug"] for model in catalog["models"] if model["supported_in_api"]][:3]


def message(text):
    return [
        {
            "type": "message",
            "role": "assistant",
            "content": [
                {"type": "output_text", "text": text},
            ],
        }
    ]


async def test_run_override_survives_handoff_without_changing_next_root():
    initial, forced, destination = model_names()
    calls = []

    class Model:
        def __init__(self, name):
            self.name = name

        def complete(self, request: ModelRequest) -> ModelResponse:
            calls.append((self.name, request.body.get("model")))
            if len(calls) == 1:
                return ModelResponse(
                    [
                        {
                            "type": "function_call",
                            "name": "transfer_to_target",
                            "call_id": "handoff",
                            "arguments": "{}",
                        }
                    ]
                )
            return ModelResponse(message("done"))

    class Provider:
        def get_model(self, name):
            return Model(name)

    async with timeout(20), NativeProvider(model_backend=Provider()) as provider:
        target = Agent(name="Target", model=destination, instructions="Target.")
        source = Agent(model=initial, instructions="Source.", handoffs=[target], provider=provider)
        async with source.session() as session:
            first = await session.submit("transfer", model_override=forced)
            assert (await first.get_result()).final_output == "done"
            assert await session.get_active_agent() is target
            assert (
                await (await session.submit("later default")).get_result()
            ).final_output == "done"
    assert calls == [(forced, forced), (forced, forced), (destination, destination)]


async def test_queued_roots_keep_independent_model_selection():
    initial, forced, _ = model_names()
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = []

    class Model:
        def __init__(self, name):
            self.name = name

        async def complete(self, request):
            calls.append(self.name)
            if len(calls) == 1:
                entered.set()
                await release.wait()
            return ModelResponse(message(self.name))

    class Provider:
        def get_model(self, name):
            return Model(name)

    async with timeout(20), NativeProvider(model_backend=Provider()) as provider:
        async with Agent(
            model=initial, instructions="Answer.", provider=provider
        ).session() as session:
            first = await session.submit("override", model_override=forced)
            await entered.wait()
            second = await session.submit("queued default", if_busy="queue")
            release.set()
            assert (await first.get_result()).final_output == forced
            assert (await second.get_result()).final_output == initial
            assert (
                await (await session.submit("later default")).get_result()
            ).final_output == initial
    assert calls == [forced, initial, initial]


@pytest.mark.parametrize("value", [17, "", "   "])
async def test_invalid_run_override_refuses_before_provider_acquisition(value, monkeypatch):
    def refuse(*args, **kwargs):
        pytest.fail("invalid model override acquired the provider")

    monkeypatch.setattr(NativeProvider, "_acquire_lease", refuse)
    async with NativeProvider() as provider:
        with pytest.raises((TypeError, ValueError), match="model_override"):
            await Agent(instructions="Answer.", provider=provider).run(
                "invalid", model_override=value
            )


def test_run_sync_forwards_model_override_to_native_admission():
    initial, forced, _ = model_names()

    class Provider:
        def get_model(self, name):
            class Model:
                def complete(self, request):
                    return ModelResponse(message(name))

            return Model()

    provider = NativeProvider(model_backend=Provider())
    try:
        result = Agent(model=initial, instructions="Answer.", provider=provider).run_sync(
            "once", model_override=forced
        )
        assert result.final_output == forced
    finally:
        asyncio.run(provider.aclose())
