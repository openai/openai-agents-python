"""Borrowed Chat streams use the existing native producer and cleanup owner."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest

from agents import Agent, ChatCompletionsModel, ModelRequest, NativeProvider, function_tool
from agents.observation import OutputTextDelta, RawProviderEvent
from tests._asyncio import timeout


def chunk(delta=None, finish=None, usage=None):
    return {
        "id": "chat-stream",
        "object": "chat.completion.chunk",
        "choices": [] if delta is None else [{"index": 0, "delta": delta, "finish_reason": finish}],
        **({"usage": usage} if usage is not None else {}),
    }


def client(create):
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


def request(report=None):
    return ModelRequest(
        {"model": "fixture", "input": [], "tools": []},
        None,
        "session",
        "turn",
        _report_usage=report,
    )


@pytest.mark.parametrize("include_usage", [None, True, False])
async def test_chat_stream_option_and_terminal_usage(include_usage):
    bodies, measured = [], []

    async def frames():
        yield chunk({"content": "early"})
        yield chunk({}, "stop")
        yield chunk(
            usage={
                "prompt_tokens": 11,
                "completion_tokens": 3,
                "total_tokens": 14,
                "prompt_tokens_details": {"cached_tokens": 2},
                "completion_tokens_details": {"reasoning_tokens": 1},
            }
        )

    async def create(**body):
        bodies.append(body)
        return frames()

    options = {} if include_usage is None else {"include_usage": include_usage}
    model = ChatCompletionsModel(client(create), stream=True, **options)
    events = [
        event async for event in model.stream(request(lambda value: measured.append(value) or True))
    ]
    assert bodies[0]["stream"] is True
    assert bodies[0].get("stream_options") == (
        None if include_usage is False else {"include_usage": True}
    )
    assert events[-1]["response"]["usage"]["total_tokens"] == 14
    assert measured[0]["cached_input_tokens"] == 2
    assert measured[0]["reasoning_output_tokens"] == 1


async def test_chat_stream_reports_usage_before_iterator_cleanup():
    entered, release = asyncio.Event(), asyncio.Event()
    measured = []

    async def frames():
        try:
            yield chunk({"content": "early"}, "stop")
            yield chunk(usage={"prompt_tokens": 11, "completion_tokens": 3, "total_tokens": 14})
        finally:
            entered.set()
            await release.wait()

    async def create(**body):
        return frames()

    model = ChatCompletionsModel(client(create), stream=True)

    async def consume():
        return [
            event
            async for event in model.stream(request(lambda value: measured.append(value) or True))
        ]

    async with timeout(10):
        pending = asyncio.create_task(consume())
        try:
            await entered.wait()
            assert measured and measured[0]["total_tokens"] == 14
            assert not pending.done()
        finally:
            release.set()
        assert (await pending)[-1]["type"] == "response.completed"


async def test_native_chat_text_arrives_before_provider_finishes():
    entered, release = asyncio.Event(), asyncio.Event()

    async def frames():
        yield chunk({"content": "early"})
        entered.set()
        await release.wait()
        yield chunk({"content": " final"}, "stop")
        yield chunk(
            usage={
                "prompt_tokens": 11,
                "completion_tokens": 3,
                "total_tokens": 14,
                "prompt_tokens_details": {"cached_tokens": 2},
                "completion_tokens_details": {"reasoning_tokens": 1},
            }
        )

    async def create(**body):
        return frames()

    async with (
        timeout(15),
        NativeProvider(model_backend=ChatCompletionsModel(client(create), stream=True)) as provider,
    ):
        async with Agent(
            instructions="Reply.", model="fixture", provider=provider
        ).session() as session:
            turn = await session.submit("Reply.")
            pending = asyncio.create_task(turn.get_result())
            try:
                await entered.wait()
                async for event in turn.events():
                    if isinstance(event, OutputTextDelta):
                        assert event.delta == "early"
                        break
                assert not pending.done()
            finally:
                release.set()
            result = await pending
            assert result.final_output == "early final"
            assert result.run_usage.requests == 1
            assert result.run_usage.input_tokens == 11
            assert result.run_usage.output_tokens == 3
            assert result.run_usage.total_tokens == 14
            assert result.run_usage.cached_input_tokens == 2
            assert result.run_usage.reasoning_output_tokens == 1


async def test_native_chat_tools_wait_for_physical_iterator_cleanup():
    entered, release = asyncio.Event(), asyncio.Event()
    effects = []
    bodies = []

    @function_tool
    def record(value: str) -> str:
        effects.append(value)
        return "recorded"

    async def frames():
        try:
            yield chunk(
                {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "record-call",
                            "type": "function",
                            "function": {"name": "record", "arguments": '{"value":'},
                        }
                    ]
                }
            )
            yield chunk(
                {"tool_calls": [{"index": 0, "function": {"arguments": '"once"}'}}]}, "tool_calls"
            )
        finally:
            entered.set()
            await release.wait()

    async def final():
        yield chunk({"content": "done"}, "stop")

    async def create(**body):
        bodies.append(deepcopy(body))
        return frames() if len(bodies) == 1 else final()

    async with (
        timeout(15),
        NativeProvider(model_backend=ChatCompletionsModel(client(create), stream=True)) as provider,
    ):
        pending = asyncio.create_task(
            Agent(
                instructions="Use record.", model="fixture", provider=provider, tools=[record]
            ).run("Record.")
        )
        try:
            await entered.wait()
            assert effects == []
        finally:
            release.set()
        assert (await pending).final_output == "done"
    assert effects == ["once"]


@pytest.mark.parametrize("capture", [False, True])
async def test_native_chat_stream_observes_original_chunks_only_on_opt_in(capture):
    originals = [
        chunk({"content": "done"}, "stop"),
        chunk(usage={"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3}),
    ]
    originals[0]["provider_extension"] = {"nested": ["synthetic raw chunk"]}

    async def frames():
        for value in originals:
            yield value

    async def create(**body):
        return frames()

    async with (
        timeout(15),
        NativeProvider(model_backend=ChatCompletionsModel(client(create), stream=True)) as provider,
    ):
        async with Agent(instructions="Reply.", model="fixture", provider=provider).session(
            include_raw_provider_events=capture, observation_retention="all"
        ) as session:
            turn = await session.submit("Reply.")
            assert (await turn.get_result()).final_output == "done"
            expected = deepcopy(originals)
            originals[0]["provider_extension"]["nested"][0] = "mutated"
            observed = [
                event
                async for event in turn.events(include_execution_details=True)
                if isinstance(event, RawProviderEvent)
            ]
            assert [event.payload for event in observed] == (expected if capture else [])
            assert all(
                event.source == "chat_completions" and event.transport == "json"
                for event in observed
            )


async def test_chat_stream_joins_the_borrowed_response_close():
    closed = []

    class BorrowedStream:
        async def __aiter__(self):
            yield chunk({"content": "done"}, "stop")

        async def close(self):
            closed.append(True)

    async def create(**body):
        return BorrowedStream()

    model = ChatCompletionsModel(client(create), stream=True)
    events = [event async for event in model.stream(request())]
    assert events[-1]["type"] == "response.completed"
    assert closed == [True]


async def test_chat_stream_bounds_tool_only_assembly():
    async def frames():
        yield chunk(
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "large-call",
                        "type": "function",
                        "function": {"name": "record", "arguments": "x" * (8 * 1024 * 1024)},
                    }
                ]
            },
            "tool_calls",
        )

    async def create(**body):
        return frames()

    model = ChatCompletionsModel(client(create), stream=True)
    with pytest.raises(ValueError, match="buffer limit"):
        _ = [event async for event in model.stream(request())]


async def test_chat_stream_keeps_measured_usage_when_cleanup_fails():
    measured = []

    async def frames():
        try:
            yield chunk({"content": "done"}, "stop")
            yield chunk(usage={"prompt_tokens": 11, "completion_tokens": 3, "total_tokens": 14})
        finally:
            raise RuntimeError("synthetic iterator cleanup failure")

    async def create(**body):
        return frames()

    model = ChatCompletionsModel(client(create), stream=True)
    with pytest.raises(RuntimeError, match="synthetic iterator cleanup failure"):
        _ = [
            event
            async for event in model.stream(request(lambda value: measured.append(value) or True))
        ]
    assert measured == [
        {
            "input_tokens": 11,
            "output_tokens": 3,
            "total_tokens": 14,
            "cached_input_tokens": 0,
            "cache_write_input_tokens": 0,
            "reasoning_output_tokens": 0,
        }
    ]


async def test_native_chat_cancellation_joins_cleanup_and_discards_late_tools():
    entered, cleaning, release, cleaned = (asyncio.Event() for _ in range(4))
    effects = []

    @function_tool
    def record() -> str:
        effects.append(True)
        return "unused"

    async def frames():
        try:
            yield chunk({"role": "assistant"})
            entered.set()
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            yield chunk(
                {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "late-call",
                            "type": "function",
                            "function": {"name": "record", "arguments": "{}"},
                        }
                    ]
                },
                "tool_calls",
            )
        finally:
            cleaning.set()
            await release.wait()
            cleaned.set()

    async def create(**body):
        return frames()

    async with (
        timeout(15),
        NativeProvider(model_backend=ChatCompletionsModel(client(create), stream=True)) as provider,
    ):
        async with Agent(
            instructions="Wait.", model="fixture", provider=provider, tools=[record]
        ).session() as session:
            turn = await session.submit("Wait.")
            await entered.wait()
            pending = asyncio.create_task(turn.cancel())
            try:
                await cleaning.wait()
                assert not pending.done()
                assert effects == []
            finally:
                release.set()
            await pending
            assert cleaned.is_set()
            assert effects == []


@pytest.mark.parametrize("streaming", [False, True])
async def test_native_chat_usage_stays_out_of_responses_originals(streaming):
    usage = {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3}

    async def frames():
        yield chunk({"content": "done"}, "stop")
        yield chunk(usage=usage)

    async def create(**body):
        if body.get("stream"):
            return frames()
        return {
            "id": "chat-completed",
            "object": "chat.completion",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "done"},
                    "finish_reason": "stop",
                }
            ],
            "usage": usage,
        }

    async with (
        timeout(15),
        NativeProvider(
            model_backend=ChatCompletionsModel(client(create), stream=streaming)
        ) as provider,
    ):
        result = await Agent(instructions="Reply.", model="fixture", provider=provider).run(
            "Reply.", return_raw_usage=True
        )
    assert result.raw_usage == ()
    assert result.run_usage.requests == 1
    assert result.run_usage.input_tokens == 2
    assert result.run_usage.output_tokens == 1
    assert result.run_usage.total_tokens == 3
