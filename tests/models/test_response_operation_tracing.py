from __future__ import annotations

import asyncio
import json
from typing import Any, Literal

import httpx2
import pytest
from openai import AsyncOpenAI

from agents import Agent, ModelSettings, RunConfig, Runner
from agents.models.openai_responses import OpenAIResponsesModel
from agents.retry import ModelRetrySettings
from agents.tracing.span_data import GenerationSpanData, ResponseSpanData
from tests.model_test_helpers import get_response_obj
from tests.test_responses import get_text_message
from tests.testing_processor import fetch_ordered_spans


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize(
    ("detail", "exhausted", "retry_owner"),
    [
        (None, False, "runner"),
        ("metadata", False, "runner"),
        ("content", False, "runner"),
        ("metadata", True, "runner"),
        (None, False, "transport"),
    ],
)
async def test_runner_retries_preserve_default_attempts_and_coalesce_detailed_operations(
    stream: bool,
    detail: Literal["metadata", "content"] | None,
    exhausted: bool,
    retry_owner: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[dict[str, Any]] = []

    # A wire double preserves provider errors, stream parsing, and actual Runner retry ownership.
    async def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(json.loads(request.content))
        if len(requests) == 1 or exhausted:
            return httpx2.Response(
                503, json={"error": {"message": "retry", "type": "server_error"}}
            )
        payload = get_response_obj([get_text_message("done")]).model_dump(mode="json")
        if stream:
            event = {"type": "response.completed", "response": payload, "sequence_number": 0}
            return httpx2.Response(
                200,
                content=f"event: response.completed\ndata: {json.dumps(event)}\n\n",
                headers={"content-type": "text/event-stream"},
            )
        return httpx2.Response(200, json=payload)

    async with AsyncOpenAI(
        api_key="test",
        base_url="https://provider.example/v1",
        max_retries=1 if retry_owner == "transport" else 0,
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)),
    ) as client:
        monkeypatch.setattr(client, "_calculate_retry_timeout", lambda *args, **kwargs: 0)
        agent = Agent(
            name="retry",
            model=OpenAIResponsesModel(model="fake", openai_client=client),
            model_settings=ModelSettings(
                retry=ModelRetrySettings(
                    max_retries=1,
                    backoff={"initial_delay": 0},
                    policy=lambda _: True,
                )
                if retry_owner == "runner"
                else None
            ),
        )
        config = RunConfig(tracing={} if detail is None else {"operation_detail": detail})
        error: Exception | None = None
        result = None
        try:
            if stream:
                result = Runner.run_streamed(agent, "hello", run_config=config)
                async for _ in result.stream_events():
                    pass
            else:
                result = await Runner.run(agent, "hello", run_config=config)
        except Exception as exc:
            error = exc

    assert len(requests) == 2, repr(error)
    assert (error is not None) is exhausted
    if not exhausted:
        assert result is not None
        assert result.final_output == "done"
    spans = [
        span
        for span in fetch_ordered_spans()
        if isinstance(span.span_data, (GenerationSpanData, ResponseSpanData))
    ]
    assert len(spans) == (2 if detail is None and retry_owner == "runner" else 1)
    assert all(span.started_at is not None and span.ended_at is not None for span in spans)
    assert (spans[-1].error is not None) is exhausted
    assert spans[-1].span_data.operation.outcome == ("failed" if exhausted else "completed")
    if detail is not None:
        assert isinstance(spans[0].span_data, GenerationSpanData)


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["http", "websocket"])
@pytest.mark.parametrize(
    ("detail", "oversized"), [("content", False), ("content", True), ("metadata", False)]
)
async def test_content_uses_prepared_request_overrides(
    transport: str,
    detail: Literal["metadata", "content"],
    oversized: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import AsyncMock

    from agents.models.openai_responses import OpenAIResponsesWSModel
    from tests.models.test_openai_responses import DummyWSConnection

    requests: list[dict[str, Any]] = []
    response = get_response_obj([get_text_message("done")]).model_dump(mode="json")

    # These transport doubles preserve final request assembly, including extra_body overrides.
    async def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(json.loads(request.content))
        return httpx2.Response(200, json=response)

    frame = {"type": "response.completed", "response": response, "sequence_number": 0}
    connection = DummyWSConnection([json.dumps(frame)])
    submitted = [{"role": "user", "content": "☃" * 20000 if oversized else "submitted input"}]
    async with AsyncOpenAI(
        api_key="test",
        base_url="https://provider.example/v1",
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)),
    ) as client:
        model_class = OpenAIResponsesWSModel if transport == "websocket" else OpenAIResponsesModel
        model = model_class(model="initial-model", openai_client=client)
        if transport == "websocket":
            monkeypatch.setattr(
                model, "_open_websocket_connection", AsyncMock(return_value=connection)
            )
        agent = Agent(
            name="prepared",
            instructions="initial instructions",
            model=model,
            model_settings=ModelSettings(
                extra_headers={"Authorization": "transport-secret"},
                extra_body={
                    "input": submitted,
                    "instructions": "submitted instructions",
                    "model": "submitted-model",
                    "previous_response_id": "previous-response",
                    "authorization": "body-secret",
                },
            ),
        )
        try:
            result = await Runner.run(
                agent,
                "initial input",
                run_config=RunConfig(tracing={"operation_detail": detail}),
            )
        finally:
            await model.close()
    if transport == "websocket":
        requests = connection.sent_messages
    assert result.final_output == "done"
    assert len(requests) == 1
    data = next(
        span.span_data
        for span in fetch_ordered_spans()
        if isinstance(span.span_data, GenerationSpanData)
    )
    assert requests[0]["input"] == submitted
    if detail == "metadata":
        assert data.input is None
        assert data.output is None
        assert data.model_config is None
    elif oversized:
        captured = json.dumps(
            {"input": data.input, **(data.model_config or {})}, ensure_ascii=False
        ).encode("utf-8")
        assert len(captured) <= 32 * 1024
        assert b"truncated" in captured
    else:
        assert data.input == submitted
        assert data.model_config == {"instructions": requests[0]["instructions"]}
    assert data.model == "submitted-model"
    assert data.operation.previous_response_id == (
        "previous-response" if detail == "content" else None
    )
    assert "transport-secret" not in repr(data.export())
    assert "body-secret" not in repr(data.export())


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_cancel_during_retry_backoff_finishes_logical_model_span(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agents.run_internal import model_retry

    in_backoff = asyncio.Event()
    release = asyncio.Event()

    async def backoff(_delay: float) -> None:
        in_backoff.set()
        await release.wait()

    async def handler(_request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(503, json={"error": {"message": "retry", "type": "server_error"}})

    monkeypatch.setattr(model_retry, "_sleep_for_retry", backoff)
    async with AsyncOpenAI(
        api_key="test",
        max_retries=0,
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)),
    ) as client:
        agent = Agent(
            name="cancel retry",
            model=OpenAIResponsesModel(model="fake", openai_client=client),
            model_settings=ModelSettings(
                retry=ModelRetrySettings(max_retries=1, policy=lambda _: True)
            ),
        )
        task = asyncio.create_task(
            Runner.run(
                agent, "hello", run_config=RunConfig(tracing={"operation_detail": "metadata"})
            )
        )
        try:
            await asyncio.wait_for(in_backoff.wait(), timeout=1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            release.set()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    spans = [
        span for span in fetch_ordered_spans() if isinstance(span.span_data, GenerationSpanData)
    ]
    assert len(spans) == 1
    assert spans[0].ended_at is not None
    assert spans[0].span_data.operation.outcome == "interrupted"


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", ["failed", "incomplete"])
@pytest.mark.parametrize(
    ("detail", "sensitive", "official"),
    [
        (None, True, False),
        ("content", True, False),
        ("content", False, False),
        ("metadata", False, True),
    ],
)
async def test_failed_terminal_records_outcome_before_exposure_and_preserves_source_end(
    terminal: str,
    detail: Literal["metadata", "content"] | None,
    sensitive: bool,
    official: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agents import ModelTracing, trace
    from agents.models import openai_responses
    from agents.tracing.config import operation_tracing_scope
    from agents.tracing.scope import Scope
    from agents.usage import Usage

    payload = get_response_obj(
        [get_text_message("partial")],
        response_id="terminal-response",
        usage=Usage(requests=1, input_tokens=7, output_tokens=2, total_tokens=9),
    ).model_dump(mode="json")
    payload["status"] = terminal
    if terminal == "incomplete":
        payload["incomplete_details"] = {"reason": "max_output_tokens"}
    else:
        payload["error"] = {"code": "server_error", "message": "synthetic failure"}
    event = {"type": f"response.{terminal}", "response": payload, "sequence_number": 0}
    source_ends: list[str] = []
    original_clock = openai_responses.time_iso

    def terminal_clock() -> str:
        source_ends.append(original_clock())
        return source_ends[-1]

    monkeypatch.setattr(openai_responses, "time_iso", terminal_clock)
    async with AsyncOpenAI(
        api_key="test",
        base_url="https://api.openai.com/v1" if official else "https://provider.example/v1",
        http_client=httpx2.AsyncClient(
            transport=httpx2.MockTransport(
                lambda _: httpx2.Response(
                    200,
                    content=f"event: response.{terminal}\ndata: {json.dumps(event)}\n\n",
                    headers={"content-type": "text/event-stream"},
                )
            )
        ),
    ) as client:
        model = OpenAIResponsesModel(model="requested-model", openai_client=client)
        with (
            trace("terminal"),
            operation_tracing_scope(
                {} if detail is None else {"operation_detail": detail}, sensitive
            ),
        ):
            stream = model.stream_response(
                system_instructions=None,
                input="caller input",
                model_settings=ModelSettings(),
                tools=[],
                output_schema=None,
                handoffs=[],
                tracing=ModelTracing.ENABLED if sensitive else ModelTracing.ENABLED_WITHOUT_DATA,
            )
            try:
                chunk = await stream.__anext__()
                assert chunk.type == f"response.{terminal}"
                live = Scope.get_current_span()
                assert live is not None
                assert live.span_data.operation.outcome == "failed"
                assert live.span_data.operation.response_model == payload["model"]
                assert live.span_data.usage["input_tokens"] == 7
                assert live.span_data.operation.response_id == (
                    "terminal-response" if sensitive or official else None
                )
                if isinstance(live.span_data, ResponseSpanData):
                    assert live.span_data.input is None
                    assert live.span_data.response is None
                    assert live.span_data.export()["response_id"] is None
                elif not sensitive:
                    assert live.span_data.input is None
                    assert live.span_data.output is None
            finally:
                await stream.aclose()
    spans = [
        span
        for span in fetch_ordered_spans()
        if isinstance(span.span_data, (GenerationSpanData, ResponseSpanData))
    ]
    assert len(spans) == 1
    assert spans[0].span_data.operation.outcome == "failed"
    assert spans[0].error is not None
    assert spans[0].ended_at == source_ends[0]


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["http", "websocket"])
@pytest.mark.parametrize("terminal", ["failed", "incomplete"])
@pytest.mark.parametrize(
    ("detail", "sensitive", "official"),
    [
        (None, True, True),
        ("metadata", True, True),
        ("content", True, False),
        ("content", False, False),
    ],
)
async def test_nonstream_terminal_retains_permitted_metadata_and_partial_output(
    transport: str,
    terminal: str,
    detail: Literal["metadata", "content"] | None,
    sensitive: bool,
    official: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import AsyncMock

    from agents import ModelBehaviorError
    from agents.models.openai_responses import OpenAIResponsesWSModel
    from agents.usage import Usage
    from tests.models.test_openai_responses import DummyWSConnection

    response = get_response_obj(
        [get_text_message("visible partial answer")],
        response_id="terminal-response",
        usage=Usage(requests=1, input_tokens=7, output_tokens=2, total_tokens=9),
    ).model_dump(mode="json")
    response["status"] = terminal
    if terminal == "incomplete":
        response["incomplete_details"] = {"reason": "max_output_tokens"}
    else:
        response["error"] = {"code": "server_error", "message": "synthetic failure"}
    frame = {"type": f"response.{terminal}", "response": response, "sequence_number": 0}
    connection = DummyWSConnection([json.dumps(frame)])
    async with AsyncOpenAI(
        api_key="test",
        base_url="https://api.openai.com/v1" if official else "https://provider.example/v1",
        max_retries=0,
        http_client=httpx2.AsyncClient(
            transport=httpx2.MockTransport(lambda _: httpx2.Response(200, json=response))
        ),
    ) as client:
        model_class = OpenAIResponsesWSModel if transport == "websocket" else OpenAIResponsesModel
        model = model_class(model="requested-model", openai_client=client)
        if transport == "websocket":
            monkeypatch.setattr(
                model, "_open_websocket_connection", AsyncMock(return_value=connection)
            )
        try:
            with pytest.raises(ModelBehaviorError, match=f"response.{terminal}"):
                await Runner.run(
                    Agent(name="failed response", model=model),
                    "caller input",
                    run_config=RunConfig(
                        tracing={} if detail is None else {"operation_detail": detail},
                        trace_include_sensitive_data=sensitive,
                    ),
                )
        finally:
            await model.close()
    span = next(
        span
        for span in fetch_ordered_spans()
        if isinstance(span.span_data, (ResponseSpanData, GenerationSpanData))
    )
    data = span.span_data
    assert span.error is not None
    assert data.operation.outcome == "failed"
    assert data.operation.response_model == response["model"]
    assert data.usage["input_tokens"] == 7
    assert data.usage["output_tokens"] == 2
    if isinstance(data, ResponseSpanData):
        assert data.response is None
        assert data.input is None
        assert data.export()["response_id"] is None
    else:
        assert data.operation.response_id == (
            "terminal-response" if sensitive or official else None
        )
        assert data.output == (response["output"] if detail == "content" and sensitive else None)
