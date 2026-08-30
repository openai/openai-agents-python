from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import pytest
from openai import AsyncOpenAI
from openai.types.responses import ResponseCompletedEvent, ResponseOutputItemDoneEvent
from websockets.asyncio.server import ServerConnection, serve

from agents import (
    Agent,
    RunConfig,
    Runner,
    set_default_openai_client,
    set_default_openai_responses_transport,
)
from agents.models.interface import Model, ModelProvider
from agents.models.multi_provider import MultiProvider
from agents.testing import ModelStep, ScriptedModel
from tests.model_test_helpers import get_response_obj

from .test_responses import get_text_message


def _scripted_agent(*steps: Any) -> Agent[None]:
    return Agent(name="test", model=ScriptedModel(steps))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "run_config",
    [None, {"tracing_disabled": True}],
    ids=["omitted", "dictionary-with-default-provider"],
)
async def test_run_closes_implicitly_created_model_provider(
    monkeypatch: pytest.MonkeyPatch,
    run_config: dict[str, Any] | None,
) -> None:
    closed: list[MultiProvider] = []

    async def record_close(provider: MultiProvider) -> None:
        closed.append(provider)

    monkeypatch.setattr(MultiProvider, "aclose", record_close)
    agent = _scripted_agent([get_text_message("done")])

    result = (
        await Runner.run(agent, "hello")
        if run_config is None
        else await Runner.run(agent, "hello", run_config=run_config)
    )

    assert result.final_output == "done"
    assert len(closed) == 1


def test_run_sync_closes_implicitly_created_model_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closed: list[MultiProvider] = []

    async def record_close(provider: MultiProvider) -> None:
        closed.append(provider)

    monkeypatch.setattr(MultiProvider, "aclose", record_close)
    agent = _scripted_agent([get_text_message("done")])

    result = Runner.run_sync(agent, "hello")

    assert result.final_output == "done"
    assert len(closed) == 1


@pytest.mark.asyncio
async def test_run_preserves_primary_error_when_provider_cleanup_fails(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    run_error = RuntimeError("run failed")
    close_error = RuntimeError("close failed")

    async def fail_close(_provider: MultiProvider) -> None:
        raise close_error

    monkeypatch.setattr(MultiProvider, "aclose", fail_close)
    agent = _scripted_agent(ModelStep.raise_error(run_error))

    with caplog.at_level(logging.WARNING, logger="openai.agents"):
        with pytest.raises(RuntimeError) as exc_info:
            await Runner.run(agent, "hello")

    assert exc_info.value is run_error
    assert "Failed to close model provider created for run" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("dictionary_config", [False, True], ids=["RunConfig", "dictionary"])
async def test_run_keeps_explicit_model_provider_open_for_reuse(dictionary_config: bool) -> None:
    class ReusableProvider(ModelProvider):
        def __init__(self, model: Model) -> None:
            self.model = model
            self.close_calls = 0

        def get_model(self, model_name: str | None) -> Model:
            return self.model

        async def aclose(self) -> None:
            self.close_calls += 1

    model = ScriptedModel(
        [
            [get_text_message("first")],
            [get_text_message("second")],
        ]
    )
    provider = ReusableProvider(model)
    run_config: RunConfig | dict[str, Any] = (
        {"model_provider": provider} if dictionary_config else RunConfig(model_provider=provider)
    )
    agent = Agent(name="test", model="test-model")

    first = await Runner.run(agent, "first", run_config=run_config)
    second = await Runner.run(agent, "second", run_config=run_config)

    assert first.final_output == "first"
    assert second.final_output == "second"
    assert provider.close_calls == 0


@pytest.mark.asyncio
async def test_run_streamed_closes_provider_only_after_run_settles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = asyncio.Event()
    finish = asyncio.Event()
    closed: list[MultiProvider] = []
    output = get_text_message("done")

    async def events(_call: object) -> AsyncIterator[Any]:
        started.set()
        await finish.wait()
        yield ResponseOutputItemDoneEvent(
            type="response.output_item.done",
            item=output,
            output_index=0,
            sequence_number=0,
        )
        yield ResponseCompletedEvent(
            type="response.completed",
            response=get_response_obj([output]),
            sequence_number=1,
        )

    async def record_close(provider: MultiProvider) -> None:
        closed.append(provider)

    monkeypatch.setattr(MultiProvider, "aclose", record_close)
    agent = _scripted_agent(ModelStep.stream(events))
    result = Runner.run_streamed(agent, "hello")

    await asyncio.wait_for(started.wait(), timeout=1)
    assert closed == []

    finish.set()
    async for _event in result.stream_events():
        pass

    assert result.final_output == "done"
    assert len(closed) == 1


@pytest.mark.asyncio
async def test_run_streamed_closes_provider_after_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = asyncio.Event()
    stream_closed = asyncio.Event()
    blocked = asyncio.Event()
    closed: list[MultiProvider] = []

    async def events(_call: object) -> AsyncIterator[Any]:
        started.set()
        try:
            await blocked.wait()
        finally:
            stream_closed.set()
        if False:  # pragma: no cover - makes this an async generator
            yield None

    async def record_close(provider: MultiProvider) -> None:
        closed.append(provider)

    monkeypatch.setattr(MultiProvider, "aclose", record_close)
    agent = _scripted_agent(ModelStep.stream(events))
    result = Runner.run_streamed(agent, "hello")
    await asyncio.wait_for(started.wait(), timeout=1)

    result.cancel()
    async for _event in result.stream_events():
        pass

    assert stream_closed.is_set()
    assert len(closed) == 1


@pytest.mark.asyncio
async def test_run_streamed_repeated_cancellation_waits_for_provider_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream_started = asyncio.Event()
    blocked = asyncio.Event()
    close_started = asyncio.Event()
    close_release = asyncio.Event()
    close_completed = asyncio.Event()

    async def events(_call: object) -> AsyncIterator[Any]:
        stream_started.set()
        await blocked.wait()
        if False:  # pragma: no cover - makes this an async generator
            yield None

    async def slow_close(_provider: MultiProvider) -> None:
        close_started.set()
        await close_release.wait()
        close_completed.set()

    monkeypatch.setattr(MultiProvider, "aclose", slow_close)
    agent = _scripted_agent(ModelStep.stream(events))
    result = Runner.run_streamed(agent, "hello")
    await asyncio.wait_for(stream_started.wait(), timeout=1)

    result.cancel()
    await asyncio.wait_for(close_started.wait(), timeout=1)
    result.cancel()
    close_release.set()
    async for _event in result.stream_events():
        pass

    assert close_completed.is_set()


@pytest.mark.asyncio
async def test_run_streamed_cancel_before_start_propagates_through_cleanup_wrapper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sandbox_cleanup_completed = asyncio.Event()

    async def record_provider_close(_provider: MultiProvider) -> None:
        return None

    async def record_sandbox_cleanup() -> None:
        sandbox_cleanup_completed.set()

    monkeypatch.setattr(MultiProvider, "aclose", record_provider_close)
    result = Runner.run_streamed(Agent(name="test", model=ScriptedModel()), "hello")
    original_task = result.run_loop_task
    assert original_task is not None
    result._sandbox_cleanup = record_sandbox_cleanup
    result.ensure_sandbox_cleanup_on_completion()

    result.cancel()
    async for _event in result.stream_events():
        pass

    assert original_task.cancelled()
    assert sandbox_cleanup_completed.is_set()


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_run_streamed_closes_implicit_responses_websocket_connection() -> None:
    connection_closed = asyncio.Event()

    async def handle(connection: ServerConnection) -> None:
        try:
            async for request_json in connection:
                request = json.loads(request_json)
                assert request["type"] == "response.create"
                response = get_response_obj(
                    [get_text_message("done")],
                    response_id="resp-runner-provider-cleanup",
                )
                await connection.send(
                    json.dumps(
                        {
                            "type": "response.completed",
                            "response": response.model_dump(),
                            "sequence_number": 1,
                        }
                    )
                )
        finally:
            connection_closed.set()

    async with serve(handle, "127.0.0.1", 0) as server:
        server_socket = next(iter(server.sockets))
        host, port = server_socket.getsockname()[:2]
        client = AsyncOpenAI(
            api_key="test-key",
            base_url=f"http://{host}:{port}/v1",
            websocket_base_url=f"ws://{host}:{port}/v1",
            max_retries=0,
        )
        set_default_openai_client(client, use_for_tracing=False)
        set_default_openai_responses_transport("websocket")
        agent = Agent(name="test", model="gpt-4.1-mini")

        try:
            result = Runner.run_streamed(agent, "hello")
            async for _event in result.stream_events():
                pass

            assert result.final_output == "done"
            await asyncio.wait_for(connection_closed.wait(), timeout=1)
        finally:
            await client.close()
