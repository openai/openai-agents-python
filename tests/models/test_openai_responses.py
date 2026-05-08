from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest
from openai import NOT_GIVEN, APIConnectionError, RateLimitError, omit
from openai.types.responses import ResponseCompletedEvent, ResponseErrorEvent
from openai.types.responses.response_create_params import ContextManagement
from openai.types.shared.reasoning import Reasoning

from agents import (
    Agent,
    AsyncComputer,
    Computer,
    ComputerTool,
    ModelSettings,
    ModelTracing,
    Runner,
    ToolSearchTool,
    __version__,
    trace,
)
from agents.exceptions import ModelBehaviorError, UserError
from agents.models._retry_runtime import (
    provider_managed_retries_disabled,
    websocket_pre_event_retries_disabled,
)
from agents.models.openai_responses import (
    _HEADERS_OVERRIDE as RESP_HEADERS,
    ConvertedTools,
    Converter,
    OpenAIResponsesModel,
    OpenAIResponsesWSModel,
    ResponsesWebSocketError,
    _should_retry_pre_event_websocket_disconnect,
)
from agents.retry import ModelRetryAdviceRequest
from agents.usage import Usage
from tests.fake_model import get_response_obj
from tests.testing_processor import fetch_ordered_spans


async def _run_responses_model_with_custom_base_url(
    model_settings: ModelSettings | None = None,
) -> dict[str, Any]:
    class DummyResponses:
        def __init__(self) -> None:
            self.kwargs: dict[str, Any] = {}

        async def create(self, **kwargs: Any) -> Any:
            self.kwargs = kwargs
            return get_response_obj([])

    class DummyResponsesClient:
        def __init__(self, responses: DummyResponses) -> None:
            self.responses = responses
            self.base_url = httpx.URL("https://custom.example.test/v1/")

    responses = DummyResponses()
    model = OpenAIResponsesModel(
        model="gpt-4",
        openai_client=DummyResponsesClient(responses),  # type: ignore[arg-type]
    )
    agent = Agent(name="test", model=model, model_settings=model_settings or ModelSettings())

    await Runner.run(agent, "hi")

    return responses.kwargs


class DummyWSConnection:
    def __init__(self, frames: list[str]):
        self._frames = frames
        self.sent_messages: list[dict[str, Any]] = []
        self.close_calls = 0
        self.close_code: int | None = None

    async def send(self, payload: str) -> None:
        self.sent_messages.append(json.loads(payload))

    async def recv(self) -> str:
        if not self._frames:
            raise RuntimeError("No more websocket frames configured")
        return self._frames.pop(0)

    async def close(self) -> None:
        self.close_calls += 1
        if self.close_code is None:
            self.close_code = 1000


class DummyWSClient:
    def __init__(self):
        self.base_url = httpx.URL("https://api.openai.com/v1/")
        self.websocket_base_url = None
        self.default_query: dict[str, Any] = {}
        self.default_headers = {
            "Authorization": "Bearer test-key",
            "User-Agent": "AsyncOpenAI/Python test",
        }
        self.timeout: Any = None
        self.refresh_calls = 0

    async def _refresh_api_key(self) -> None:
        self.refresh_calls += 1


def _response_event_frame(event_type: str, response_id: str, sequence_number: int) -> str:
    response = get_response_obj([]).model_dump()
    response["id"] = response_id
    return json.dumps(
        {
            "type": event_type,
            "response": response,
            "sequence_number": sequence_number,
        }
    )


def _response_completed_frame(response_id: str, sequence_number: int) -> str:
    return _response_event_frame("response.completed", response_id, sequence_number)


def _response_error_frame(code: str, message: str, sequence_number: int) -> str:
    return json.dumps(
        {
            "type": "response.error",
            "error": {"code": code, "message": message, "param": None},
            "sequence_number": sequence_number,
        }
    )


def _connection_closed_error(message: str) -> Exception:
    class ConnectionClosedError(Exception):
        pass

    ConnectionClosedError.__module__ = "websockets.client"
    return ConnectionClosedError(message)


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
@pytest.mark.parametrize("override_ua", [None, "test_user_agent"])
async def test_user_agent_header_responses(override_ua: str | None):
    called_kwargs: dict[str, Any] = {}
    expected_ua = override_ua or f"Agents/Python {__version__}"

    class DummyStream:
        def __aiter__(self):
            async def gen():
                yield ResponseCompletedEvent(
                    type="response.completed",
                    response=get_response_obj([]),
                    sequence_number=0,
                )

            return gen()

    class DummyResponses:
        async def create(self, **kwargs):
            nonlocal called_kwargs
            called_kwargs = kwargs
            return DummyStream()

    class DummyResponsesClient:
        def __init__(self):
            self.responses = DummyResponses()

    model = OpenAIResponsesModel(model="gpt-4", openai_client=DummyResponsesClient())  # type: ignore

    if override_ua is not None:
        token = RESP_HEADERS.set({"User-Agent": override_ua})
    else:
        token = None

    try:
        stream = model.stream_response(
            system_instructions=None,
            input="hi",
            model_settings=ModelSettings(),
            tools=[],
            output_schema=None,
            handoffs=[],
            tracing=ModelTracing.DISABLED,
        )
        async for _ in stream:
            pass
    finally:
        if token is not None:
            RESP_HEADERS.reset(token)

    assert "extra_headers" in called_kwargs
    assert called_kwargs["extra_headers"]["User-Agent"] == expected_ua


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_get_response_exposes_request_id():
    class DummyResponses:
        async def create(self, **kwargs):
            response = get_response_obj([], response_id="resp-request-id")
            response._request_id = "req_nonstream_123"
            return response

    class DummyResponsesClient:
        def __init__(self):
            self.responses = DummyResponses()

    model = OpenAIResponsesModel(model="gpt-4", openai_client=DummyResponsesClient())  # type: ignore[arg-type]

    response = await model.get_response(
        system_instructions=None,
        input="hi",
        model_settings=ModelSettings(),
        tools=[],
        output_schema=None,
        handoffs=[],
        tracing=ModelTracing.DISABLED,
    )

    assert response.response_id == "resp-request-id"
    assert response.request_id == "req_nonstream_123"


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_get_response_span_exports_usage():
    class DummyResponses:
        async def create(self, **kwargs):
            return get_response_obj(
                [],
                response_id="resp-usage",
                usage=Usage(requests=1, input_tokens=10, output_tokens=4, total_tokens=14),
            )

    class DummyResponsesClient:
        def __init__(self):
            self.responses = DummyResponses()

    model = OpenAIResponsesModel(model="gpt-4", openai_client=DummyResponsesClient())  # type: ignore[arg-type]

    with trace("test"):
        await model.get_response(
            system_instructions=None,
            input="hi",
            model_settings=ModelSettings(),
            tools=[],
            output_schema=None,
            handoffs=[],
            tracing=ModelTracing.ENABLED,
        )

    response_spans = [
        span.export() for span in fetch_ordered_spans() if span.span_data.type == "response"
    ]
    assert len(response_spans) == 1
    assert response_spans[0]
    assert response_spans[0]["span_data"] == {
        "type": "response",
        "response_id": "resp-usage",
        "usage": {
            "requests": 1,
            "input_tokens": 10,
            "output_tokens": 4,
            "total_tokens": 14,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens_details": {"reasoning_tokens": 0},
        },
    }


def test_get_client_disables_provider_managed_retries_on_runner_retry() -> None:
    class DummyResponsesClient:
        def __init__(self) -> None:
            self.responses = SimpleNamespace()
            self.with_options_calls: list[dict[str, Any]] = []

        def with_options(self, **kwargs):
            self.with_options_calls.append(kwargs)
            return self

    client = DummyResponsesClient()
    model = OpenAIResponsesModel(model="gpt-4", openai_client=client)  # type: ignore[arg-type]

    assert cast(object, model._get_client()) is client
    with provider_managed_retries_disabled(True):
        assert cast(object, model._get_client()) is client

    assert client.with_options_calls == [{"max_retries": 0}]


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_fetch_response_stream_attaches_request_id_to_terminal_response():
    class DummyHTTPStream:
        def __init__(self):
            self._yielded = False

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self._yielded:
                raise StopAsyncIteration
            self._yielded = True
            return ResponseCompletedEvent(
                type="response.completed",
                response=get_response_obj([], response_id="resp-stream-request-id"),
                sequence_number=0,
            )

    inner_stream = DummyHTTPStream()

    class DummyAPIResponse:
        def __init__(self):
            self.request_id = "req_stream_123"
            self.close_calls = 0
            self.parse_calls = 0

        async def parse(self):
            self.parse_calls += 1
            return inner_stream

        async def close(self) -> None:
            self.close_calls += 1

    api_response = DummyAPIResponse()
    aexit_calls: list[tuple[Any, Any, Any]] = []

    class DummyStreamingContextManager:
        async def __aenter__(self):
            return api_response

        async def __aexit__(self, exc_type, exc, tb):
            aexit_calls.append((exc_type, exc, tb))
            await api_response.close()
            return False

    class DummyResponses:
        def __init__(self):
            self.with_streaming_response = SimpleNamespace(create=self.create_streaming)

        def create_streaming(self, **kwargs):
            return DummyStreamingContextManager()

    class DummyResponsesClient:
        def __init__(self):
            self.responses = DummyResponses()

    model = OpenAIResponsesModel(model="gpt-4", openai_client=DummyResponsesClient())  # type: ignore[arg-type]

    stream = await model._fetch_response(
        system_instructions=None,
        input="hi",
        model_settings=ModelSettings(),
        tools=[],
        output_schema=None,
        handoffs=[],
        previous_response_id=None,
        conversation_id=None,
        stream=True,
    )

    stream_agen = cast(Any, stream)
    event = await stream_agen.__anext__()

    assert getattr(stream, "request_id", None) == "req_stream_123"
    assert getattr(event.response, "_request_id", None) == "req_stream_123"

    with pytest.raises(StopAsyncIteration):
        await stream_agen.__anext__()

    assert api_response.parse_calls == 1
    assert api_response.close_calls == 1
    assert aexit_calls == [(None, None, None)]


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_fetch_response_stream_parse_failure_exits_streaming_context():
    parse_error = RuntimeError("parse failed")
    aexit_calls: list[tuple[Any, Any, Any]] = []

    class DummyAPIResponse:
        request_id = "req_stream_123"

        async def parse(self):
            raise parse_error

    api_response = DummyAPIResponse()

    class DummyStreamingContextManager:
        async def __aenter__(self):
            return api_response

        async def __aexit__(self, exc_type, exc, tb):
            aexit_calls.append((exc_type, exc, tb))
            return False

    class DummyResponses:
        def __init__(self):
            self.with_streaming_response = SimpleNamespace(create=self.create_streaming)

        def create_streaming(self, **kwargs):
            return DummyStreamingContextManager()

    class DummyResponsesClient:
        def __init__(self):
            self.responses = DummyResponses()

    model = OpenAIResponsesModel(model="gpt-4", openai_client=DummyResponsesClient())  # type: ignore[arg-type]

    with pytest.raises(RuntimeError, match="parse failed"):
        await model._fetch_response(
            system_instructions=None,
            input="hi",
            model_settings=ModelSettings(),
            tools=[],
            output_schema=None,
            handoffs=[],
            previous_response_id=None,
            conversation_id=None,
            stream=True,
        )

    assert len(aexit_calls) == 1
    exc_type, exc, tb = aexit_calls[0]
    assert exc_type is RuntimeError
    assert exc is parse_error
    assert tb is not None


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_fetch_response_stream_without_request_id_still_returns_events():
    class DummyHTTPStream:
        def __init__(self):
            self._yielded = False

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self._yielded:
                raise StopAsyncIteration
            self._yielded = True
            return ResponseCompletedEvent(
                type="response.completed",
                response=get_response_obj([], response_id="resp-stream-request-id"),
                sequence_number=0,
            )

    inner_stream = DummyHTTPStream()
    aexit_calls: list[tuple[Any, Any, Any]] = []

    class DummyAPIResponse:
        def __init__(self):
            self.close_calls = 0
            self.parse_calls = 0

        async def parse(self):
            self.parse_calls += 1
            return inner_stream

        async def close(self) -> None:
            self.close_calls += 1

    api_response = DummyAPIResponse()

    class DummyStreamingContextManager:
        async def __aenter__(self):
            return api_response

        async def __aexit__(self, exc_type, exc, tb):
            aexit_calls.append((exc_type, exc, tb))
            await api_response.close()
            return False

    class DummyResponses:
        def __init__(self):
            self.with_streaming_response = SimpleNamespace(create=self.create_streaming)

        def create_streaming(self, **kwargs):
            return DummyStreamingContextManager()

    class DummyResponsesClient:
        def __init__(self):
            self.responses = DummyResponses()

    model = OpenAIResponsesModel(model="gpt-4", openai_client=DummyResponsesClient())  # type: ignore[arg-type]

    stream = await model._fetch_response(
        system_instructions=None,
        input="hi",
        model_settings=ModelSettings(),
        tools=[],
        output_schema=None,
        handoffs=[],
        previous_response_id=None,
        conversation_id=None,
        stream=True,
    )

    stream_agen = cast(Any, stream)
    event = await stream_agen.__anext__()

    assert getattr(stream, "request_id", None) is None
    assert getattr(event.response, "_request_id", None) is None

    with pytest.raises(StopAsyncIteration):
        await stream_agen.__anext__()

    assert api_response.parse_calls == 1
    assert api_response.close_calls == 1
    assert aexit_calls == [(None, None, None)]


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_stream_response_ignores_streaming_context_exit_failure_after_terminal_event():
    class DummyHTTPStream:
        def __init__(self):
            self._yielded = False

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self._yielded:
                raise StopAsyncIteration
            self._yielded = True
            return ResponseCompletedEvent(
                type="response.completed",
                response=get_response_obj([], response_id="resp-stream-request-id"),
                sequence_number=0,
            )

    inner_stream = DummyHTTPStream()
    aexit_calls: list[tuple[Any, Any, Any]] = []

    class DummyAPIResponse:
        request_id = "req_stream_123"

        async def parse(self):
            return inner_stream

    api_response = DummyAPIResponse()

    class DummyStreamingContextManager:
        async def __aenter__(self):
            return api_response

        async def __aexit__(self, exc_type, exc, tb):
            aexit_calls.append((exc_type, exc, tb))
            raise RuntimeError("stream context exit failed")

    class DummyResponses:
        def __init__(self):
            self.with_streaming_response = SimpleNamespace(create=self.create_streaming)

        def create_streaming(self, **kwargs):
            return DummyStreamingContextManager()

    class DummyResponsesClient:
        def __init__(self):
            self.responses = DummyResponses()

    model = OpenAIResponsesModel(model="gpt-4", openai_client=DummyResponsesClient())  # type: ignore[arg-type]

    events: list[ResponseCompletedEvent] = []
    async for event in model.stream_response(
        system_instructions=None,
        input="hi",
        model_settings=ModelSettings(),
        tools=[],
        output_schema=None,
        handoffs=[],
        tracing=ModelTracing.DISABLED,
    ):
        assert isinstance(event, ResponseCompletedEvent)
        events.append(event)

    assert len(events) == 1
    assert aexit_calls == [(None, None, None)]


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_stream_response_close_closes_inner_http_stream_with_async_close(monkeypatch):
    client = DummyWSClient()
    model = OpenAIResponsesModel(model="gpt-4", openai_client=client)  # type: ignore[arg-type]

    class DummyHTTPStream:
        def __init__(self):
            self._yielded = False
            self.close_calls = 0

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self._yielded:
                raise StopAsyncIteration
            self._yielded = True
            return ResponseCompletedEvent(
                type="response.completed",
                response=get_response_obj([]),
                sequence_number=0,
            )

        async def close(self) -> None:
            self.close_calls += 1

    inner_stream = DummyHTTPStream()

    async def fake_fetch_response(*args: Any, **kwargs: Any) -> DummyHTTPStream:
        return inner_stream

    monkeypatch.setattr(model, "_fetch_response", fake_fetch_response)

    stream = model.stream_response(
        system_instructions=None,
        input="hi",
        model_settings=ModelSettings(),
        tools=[],
        output_schema=None,
        handoffs=[],
        tracing=ModelTracing.DISABLED,
    )
    stream_agen = cast(Any, stream)

    event = await stream_agen.__anext__()
    assert event.type == "response.completed"

    await stream_agen.aclose()

    assert inner_stream.close_calls == 1


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_stream_response_normal_exhaustion_closes_inner_http_stream(monkeypatch):
    client = DummyWSClient()
    model = OpenAIResponsesModel(model="gpt-4", openai_client=client)  # type: ignore[arg-type]

    class DummyHTTPStream:
        def __init__(self):
            self._yielded = False
            self.close_calls = 0

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self._yielded:
                raise StopAsyncIteration
            self._yielded = True
            return ResponseCompletedEvent(
                type="response.completed",
                response=get_response_obj([]),
                sequence_number=0,
            )

        async def close(self) -> None:
            self.close_calls += 1

    inner_stream = DummyHTTPStream()

    async def fake_fetch_response(*args: Any, **kwargs: Any) -> DummyHTTPStream:
        return inner_stream

    monkeypatch.setattr(model, "_fetch_response", fake_fetch_response)

    events: list[ResponseCompletedEvent] = []
    async for event in model.stream_response(
        system_instructions=None,
        input="hi",
        model_settings=ModelSettings(),
        tools=[],
        output_schema=None,
        handoffs=[],
        tracing=ModelTracing.DISABLED,
    ):
        assert isinstance(event, ResponseCompletedEvent)
        events.append(event)

    assert len(events) == 1
    assert inner_stream.close_calls == 1


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_stream_response_ignores_inner_close_failure_after_terminal_event(monkeypatch):
    client = DummyWSClient()
    model = OpenAIResponsesModel(model="gpt-4", openai_client=client)  # type: ignore[arg-type]

    class DummyHTTPStream:
        def __init__(self):
            self._yielded = False
            self.close_calls = 0

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self._yielded:
                raise StopAsyncIteration
            self._yielded = True
            return ResponseCompletedEvent(
                type="response.completed",
                response=get_response_obj([]),
                sequence_number=0,
            )

        async def close(self) -> None:
            self.close_calls += 1
            raise RuntimeError("stream close failed")

    inner_stream = DummyHTTPStream()

    async def fake_fetch_response(*args: Any, **kwargs: Any) -> DummyHTTPStream:
        return inner_stream

    monkeypatch.setattr(model, "_fetch_response", fake_fetch_response)

    events: list[ResponseCompletedEvent] = []
    async for event in model.stream_response(
        system_instructions=None,
        input="hi",
        model_settings=ModelSettings(),
        tools=[],
        output_schema=None,
        handoffs=[],
        tracing=ModelTracing.DISABLED,
    ):
        assert isinstance(event, ResponseCompletedEvent)
        events.append(event)

    assert len(events) == 1
    assert inner_stream.close_calls == 1


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_stream_response_cancellation_does_not_block_on_inner_stream_close(monkeypatch):
    client = DummyWSClient()
    model = OpenAIResponsesModel(model="gpt-4", openai_client=client)  # type: ignore[arg-type]

    class BlockingHTTPStream:
        def __init__(self):
            self.next_started = asyncio.Event()
            self.close_started = asyncio.Event()
            self.close_release = asyncio.Event()
            self.close_calls = 0

        def __aiter__(self):
            return self

        async def __anext__(self):
            self.next_started.set()
            await asyncio.Event().wait()
            raise StopAsyncIteration

        async def aclose(self) -> None:
            self.close_calls += 1
            self.close_started.set()
            await self.close_release.wait()

    inner_stream = BlockingHTTPStream()

    async def fake_fetch_response(*args: Any, **kwargs: Any) -> BlockingHTTPStream:
        return inner_stream

    monkeypatch.setattr(model, "_fetch_response", fake_fetch_response)

    stream = model.stream_response(
        system_instructions=None,
        input="hi",
        model_settings=ModelSettings(),
        tools=[],
        output_schema=None,
        handoffs=[],
        tracing=ModelTracing.DISABLED,
    )
    stream_agen = cast(Any, stream)
    next_task = asyncio.create_task(stream_agen.__anext__())

    await asyncio.wait_for(inner_stream.next_started.wait(), timeout=1.0)
    next_task.cancel()

    try:
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(next_task, timeout=0.5)
        await asyncio.wait_for(inner_stream.close_started.wait(), timeout=1.0)
        assert inner_stream.close_calls == 1
    finally:
        inner_stream.close_release.set()
        await asyncio.sleep(0)


@pytest.mark.allow_call_model_methods
def test_build_response_create_kwargs_rejects_duplicate_extra_args_keys():
    client = DummyWSClient()
    model = OpenAIResponsesModel(model="gpt-4", openai_client=client)  # type: ignore[arg-type]

    with pytest.raises(TypeError, match="multiple values.*stream"):
        model._build_response_create_kwargs(
            system_instructions=None,
            input="hi",
            model_settings=ModelSettings(extra_args={"stream": False}),
            tools=[],
            output_schema=None,
            handoffs=[],
            previous_response_id=None,
            conversation_id=None,
            stream=True,
            prompt=None,
        )


@pytest.mark.allow_call_model_methods
def test_build_response_create_kwargs_includes_extra_args_prompt_cache_key():
    client = DummyWSClient()
    model = OpenAIResponsesModel(model="gpt-4", openai_client=client)  # type: ignore[arg-type]

    kwargs = model._build_response_create_kwargs(
        system_instructions=None,
        input="hi",
        model_settings=ModelSettings(extra_args={"prompt_cache_key": "cache-key"}),
        tools=[],
        output_schema=None,
        handoffs=[],
        previous_response_id=None,
        conversation_id=None,
        stream=False,
        prompt=None,
    )

    assert kwargs["prompt_cache_key"] == "cache-key"


@pytest.mark.allow_call_model_methods
def test_build_response_create_kwargs_includes_context_management():
    client = DummyWSClient()
    model = OpenAIResponsesModel(model="gpt-4", openai_client=client)  # type: ignore[arg-type]
    context_management: list[ContextManagement] = [
        {"type": "compaction", "compact_threshold": 200000}
    ]

    kwargs = model._build_response_create_kwargs(
        system_instructions=None,
        input="hi",
        model_settings=ModelSettings(context_management=context_management),
        tools=[],
        output_schema=None,
        handoffs=[],
        previous_response_id=None,
        conversation_id=None,
        stream=False,
        prompt=None,
    )

    assert kwargs["context_management"] == context_management


@pytest.mark.allow_call_model_methods
def test_build_response_create_kwargs_allows_extra_arg_when_explicit_arg_is_omitted():
    client = DummyWSClient()
    model = OpenAIResponsesModel(model="gpt-4", openai_client=client)  # type: ignore[arg-type]
    context_management: list[ContextManagement] = [
        {"type": "compaction", "compact_threshold": 200000}
    ]

    kwargs = model._build_response_create_kwargs(
        system_instructions=None,
        input="hi",
        model_settings=ModelSettings(extra_args={"context_management": context_management}),
        tools=[],
        output_schema=None,
        handoffs=[],
        previous_response_id=None,
        conversation_id=None,
        stream=False,
        prompt=None,
    )

    assert kwargs["context_management"] == context_management


@pytest.mark.allow_call_model_methods
def test_build_response_create_kwargs_rejects_duplicate_context_management_extra_args():
    client = DummyWSClient()
    model = OpenAIResponsesModel(model="gpt-4", openai_client=client)  # type: ignore[arg-type]

    with pytest.raises(TypeError, match="multiple values.*context_management"):
        model._build_response_create_kwargs(
            system_instructions=None,
            input="hi",
            model_settings=ModelSettings(
                context_management=[{"type": "compaction", "compact_threshold": 200000}],
                extra_args={"context_management": [{"type": "compaction"}]},
            ),
            tools=[],
            output_schema=None,
            handoffs=[],
            previous_response_id=None,
            conversation_id=None,
            stream=False,
            prompt=None,
        )


@pytest.mark.allow_call_model_methods
def test_build_response_create_kwargs_allows_extra_query_and_body_via_extra_args():
    """Regression: extra_query / extra_body supplied only through extra_args must not
    falsely trigger the duplicate-keyword guard, which previously fired because the
    create_kwargs entries were left as ``None`` (rather than ``omit``) when the matching
    ModelSettings field was unset. Mirrors the fix in #3185 for omitted-kwarg paths."""
    client = DummyWSClient()
    model = OpenAIResponsesModel(model="gpt-4", openai_client=client)  # type: ignore[arg-type]

    kwargs = model._build_response_create_kwargs(
        system_instructions=None,
        input="hi",
        model_settings=ModelSettings(
            extra_args={"extra_query": {"a": "b"}, "extra_body": {"c": "d"}},
        ),
        tools=[],
        output_schema=None,
        handoffs=[],
        previous_response_id=None,
        conversation_id=None,
        stream=False,
        prompt=None,
    )

    assert kwargs["extra_query"] == {"a": "b"}
    assert kwargs["extra_body"] == {"c": "d"}


@pytest.mark.allow_call_model_methods
def test_build_response_create_kwargs_rejects_duplicate_extra_query_extra_args():
    client = DummyWSClient()
    model = OpenAIResponsesModel(model="gpt-4", openai_client=client)  # type: ignore[arg-type]

    with pytest.raises(TypeError, match="multiple values.*extra_query"):
        model._build_response_create_kwargs(
            system_instructions=None,
            input="hi",
            model_settings=ModelSettings(
                extra_query={"a": "b"},
                extra_args={"extra_query": {"a": "z"}},
            ),
            tools=[],
            output_schema=None,
            handoffs=[],
            previous_response_id=None,
            conversation_id=None,
            stream=False,
            prompt=None,
        )


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_custom_base_url_prompt_cache_key_uses_model_settings_only() -> None:
    default_kwargs = await _run_responses_model_with_custom_base_url()
    explicit_kwargs = await _run_responses_model_with_custom_base_url(
        model_settings=ModelSettings(extra_args={"prompt_cache_key": "cache-key"})
    )

    assert "prompt_cache_key" not in default_kwargs
    assert explicit_kwargs["prompt_cache_key"] == "cache-key"


@pytest.mark.allow_call_model_methods
def test_build_response_create_kwargs_preserves_unknown_response_include_values():
    client = DummyWSClient()
    model = OpenAIResponsesModel(model="gpt-4", openai_client=client)  # type: ignore[arg-type]

    kwargs = model._build_response_create_kwargs(
        system_instructions=None,
        input="hi",
        model_settings=ModelSettings(response_include=["response.future_flag"]),
        tools=[],
        output_schema=None,
        handoffs=[],
        previous_response_id=None,
        conversation_id=None,
        stream=False,
        prompt=None,
    )

    assert kwargs["include"] == ["response.future_flag"]


@pytest.mark.allow_call_model_methods
def test_build_response_create_kwargs_preserves_unknown_tool_types(monkeypatch) -> None:
    client = DummyWSClient()
    model = OpenAIResponsesModel(model="gpt-4", openai_client=client)  # type: ignore[arg-type]

    future_tool = cast(Any, {"type": "future_beta_tool", "label": "preview"})

    monkeypatch.setattr(
        Converter,
        "convert_tools",
        classmethod(
            lambda cls, tools, handoffs, **kwargs: ConvertedTools(tools=[future_tool], includes=[])
        ),
    )

    kwargs = model._build_response_create_kwargs(
        system_instructions=None,
        input="hi",
        model_settings=ModelSettings(),
        tools=[],
        output_schema=None,
        handoffs=[],
        previous_response_id=None,
        conversation_id=None,
        stream=False,
        prompt=None,
    )

    assert kwargs["tools"] == [future_tool]


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_prompt_id_omits_model_parameter():
    called_kwargs: dict[str, Any] = {}

    class DummyResponses:
        async def create(self, **kwargs):
            nonlocal called_kwargs
            called_kwargs = kwargs
            return get_response_obj([])

    class DummyResponsesClient:
        def __init__(self):
            self.responses = DummyResponses()

    model = OpenAIResponsesModel(
        model="gpt-4",
        openai_client=DummyResponsesClient(),  # type: ignore[arg-type]
        model_is_explicit=False,
    )

    await model.get_response(
        system_instructions=None,
        input="hi",
        model_settings=ModelSettings(),
        tools=[],
        output_schema=None,
        handoffs=[],
        tracing=ModelTracing.DISABLED,
        prompt={"id": "pmpt_123"},
    )

    assert called_kwargs["prompt"] == {"id": "pmpt_123"}
    assert called_kwargs["model"] is omit


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_prompt_id_omits_tools_parameter_when_no_tools_configured():
    called_kwargs: dict[str, Any] = {}

    class DummyResponses:
        async def create(self, **kwargs):
            nonlocal called_kwargs
            called_kwargs = kwargs
            return get_response_obj([])

    class DummyResponsesClient:
        def __init__(self):
            self.responses = DummyResponses()

    model = OpenAIResponsesModel(
        model="gpt-4",
        openai_client=DummyResponsesClient(),  # type: ignore[arg-type]
        model_is_explicit=False,
    )

    await model.get_response(
        system_instructions=None,
        input="hi",
        model_settings=ModelSettings(),
        tools=[],
        output_schema=None,
        handoffs=[],
        tracing=ModelTracing.DISABLED,
        prompt={"id": "pmpt_123"},
    )

    assert called_kwargs["tools"] is omit


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_prompt_id_omits_tool_choice_when_no_tools_configured():
    called_kwargs: dict[str, Any] = {}

    class DummyResponses:
        async def create(self, **kwargs):
            nonlocal called_kwargs
            called_kwargs = kwargs
            return get_response_obj([])

    class DummyResponsesClient:
        def __init__(self):
            self.responses = DummyResponses()

    model = OpenAIResponsesModel(
        model="gpt-4",
        openai_client=DummyResponsesClient(),  # type: ignore[arg-type]
        model_is_explicit=False,
    )

    await model.get_response(
        system_instructions=None,
        input="hi",
        model_settings=ModelSettings(tool_choice="web_search_preview"),
        tools=[],
        output_schema=None,
        handoffs=[],
        tracing=ModelTracing.DISABLED,
        prompt={"id": "pmpt_123"},
    )

    assert called_kwargs["tools"] is omit
    assert called_kwargs["tool_choice"] is omit


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
@pytest.mark.parametrize("tool_choice", ["none", "required"])
async def test_prompt_id_keeps_literal_tool_choice_without_local_tools(tool_choice: str):
    called_kwargs: dict[str, Any] = {}

    class DummyResponses:
        async def create(self, **kwargs):
            nonlocal called_kwargs
            called_kwargs = kwargs
            return get_response_obj([])

    class DummyResponsesClient:
        def __init__(self):
            self.responses = DummyResponses()

    model = OpenAIResponsesModel(
        model="gpt-4",
        openai_client=DummyResponsesClient(),  # type: ignore[arg-type]
        model_is_explicit=False,
    )

    await model.get_response(
        system_instructions=None,
        input="hi",
        model_settings=ModelSettings(tool_choice=tool_choice),
        tools=[],
        output_schema=None,
        handoffs=[],
        tracing=ModelTracing.DISABLED,
        prompt={"id": "pmpt_123"},
    )

    assert called_kwargs["tools"] is omit
    assert called_kwargs["tool_choice"] == tool_choice


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_prompt_id_keeps_explicit_tool_search_without_local_surface() -> None:
    called_kwargs: dict[str, Any] = {}

    class DummyResponses:
        async def create(self, **kwargs):
            nonlocal called_kwargs
            called_kwargs = kwargs
            return get_response_obj([])

    class DummyResponsesClient:
        def __init__(self):
            self.responses = DummyResponses()

    model = OpenAIResponsesModel(
        model="gpt-4",
        openai_client=DummyResponsesClient(),  # type: ignore[arg-type]
        model_is_explicit=False,
    )

    await model.get_response(
        system_instructions=None,
        input="hi",
        model_settings=ModelSettings(),
        tools=[ToolSearchTool()],
        output_schema=None,
        handoffs=[],
        tracing=ModelTracing.DISABLED,
        prompt={"id": "pmpt_123"},
    )

    assert called_kwargs["prompt"] == {"id": "pmpt_123"}
    assert called_kwargs["tools"] == [{"type": "tool_search"}]


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_ga_computer_tool_does_not_require_preview_metadata() -> None:
    called_kwargs: dict[str, Any] = {}

    class DummyComputer(AsyncComputer):
        async def screenshot(self) -> str:
            return "screenshot"

        async def click(self, x: int, y: int, button: str) -> None:
            pass

        async def double_click(self, x: int, y: int) -> None:
            pass

        async def drag(self, path: list[tuple[int, int]]) -> None:
            pass

        async def keypress(self, keys: list[str]) -> None:
            pass

        async def move(self, x: int, y: int) -> None:
            pass

        async def scroll(self, x: int, y: int, scroll_x: int, scroll_y: int) -> None:
            pass

        async def type(self, text: str) -> None:
            pass

        async def wait(self) -> None:
            pass

    class DummyResponses:
        async def create(self, **kwargs):
            nonlocal called_kwargs
            called_kwargs = kwargs
            return get_response_obj([])

    class DummyResponsesClient:
        def __init__(self):
            self.responses = DummyResponses()

    model = OpenAIResponsesModel(
        model="gpt-5.4",
        openai_client=DummyResponsesClient(),  # type: ignore[arg-type]
        model_is_explicit=True,
    )

    await model.get_response(
        system_instructions=None,
        input="hi",
        model_settings=ModelSettings(),
        tools=[ComputerTool(computer=DummyComputer())],
        output_schema=None,
        handoffs=[],
        tracing=ModelTracing.DISABLED,
        prompt=None,
    )

    assert called_kwargs["tools"] == [{"type": "computer"}]


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_prompt_id_uses_preview_computer_payload_when_prompt_owns_model() -> None:
    called_kwargs: dict[str, Any] = {}

    class DummyComputer(Computer):
        @property
        def environment(self) -> str:  # type: ignore[override]
            return "mac"

        @property
        def dimensions(self) -> tuple[int, int]:
            return (800, 600)

        def screenshot(self) -> str:
            return "screenshot"

        def click(self, x: int, y: int, button: str) -> None:
            pass

        def double_click(self, x: int, y: int) -> None:
            pass

        def drag(self, path: list[tuple[int, int]]) -> None:
            pass

        def keypress(self, keys: list[str]) -> None:
            pass

        def move(self, x: int, y: int) -> None:
            pass

        def scroll(self, x: int, y: int, scroll_x: int, scroll_y: int) -> None:
            pass

        def type(self, text: str) -> None:
            pass

        def wait(self) -> None:
            pass

    class DummyResponses:
        async def create(self, **kwargs):
            nonlocal called_kwargs
            called_kwargs = kwargs
            return get_response_obj([])

    class DummyResponsesClient:
        def __init__(self):
            self.responses = DummyResponses()

    model = OpenAIResponsesModel(
        model="gpt-5.4",
        openai_client=DummyResponsesClient(),  # type: ignore[arg-type]
        model_is_explicit=False,
    )

    await model.get_response(
        system_instructions=None,
        input="hi",
        model_settings=ModelSettings(),
        tools=[ComputerTool(computer=DummyComputer())],
        output_schema=None,
        handoffs=[],
        tracing=ModelTracing.DISABLED,
        prompt={"id": "pmpt_123"},
    )

    assert called_kwargs["model"] is omit
    assert called_kwargs["tool_choice"] is omit
    assert called_kwargs["tools"] == [
        {
            "type": "computer_use_preview",
            "environment": "mac",
            "display_width": 800,
            "display_height": 600,
        }
    ]


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_prompt_id_computer_without_preview_metadata_raises_clear_error() -> None:
    called_kwargs: dict[str, Any] = {}

    class DummyComputer(Computer):
        def screenshot(self) -> str:
            return "screenshot"

        def click(self, x: int, y: int, button: str) -> None:
            pass

        def double_click(self, x: int, y: int) -> None:
            pass

        def drag(self, path: list[tuple[int, int]]) -> None:
            pass

        def keypress(self, keys: list[str]) -> None:
            pass

        def move(self, x: int, y: int) -> None:
            pass

        def scroll(self, x: int, y: int, scroll_x: int, scroll_y: int) -> None:
            pass

        def type(self, text: str) -> None:
            pass

        def wait(self) -> None:
            pass

    class DummyResponses:
        async def create(self, **kwargs):
            nonlocal called_kwargs
            called_kwargs = kwargs
            return get_response_obj([])

    class DummyResponsesClient:
        def __init__(self):
            self.responses = DummyResponses()

    model = OpenAIResponsesModel(
        model="gpt-5.4",
        openai_client=DummyResponsesClient(),  # type: ignore[arg-type]
        model_is_explicit=False,
    )

    with pytest.raises(
        UserError,
        match="Preview computer tool payloads require `environment` and `dimensions`",
    ):
        await model.get_response(
            system_instructions=None,
            input="hi",
            model_settings=ModelSettings(),
            tools=[ComputerTool(computer=DummyComputer())],
            output_schema=None,
            handoffs=[],
            tracing=ModelTracing.DISABLED,
            prompt={"id": "pmpt_123"},
        )

    assert called_kwargs == {}


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_prompt_id_unresolved_computer_uses_preview_payload_shape() -> None:
    called_kwargs: dict[str, Any] = {}

    class DummyComputer(Computer):
        @property
        def environment(self) -> str:  # type: ignore[override]
            return "mac"

        @property
        def dimensions(self) -> tuple[int, int]:
            return (800, 600)

        def screenshot(self) -> str:
            return "screenshot"

        def click(self, x: int, y: int, button: str) -> None:
            pass

        def double_click(self, x: int, y: int) -> None:
            pass

        def drag(self, path: list[tuple[int, int]]) -> None:
            pass

        def keypress(self, keys: list[str]) -> None:
            pass

        def move(self, x: int, y: int) -> None:
            pass

        def scroll(self, x: int, y: int, scroll_x: int, scroll_y: int) -> None:
            pass

        def type(self, text: str) -> None:
            pass

        def wait(self) -> None:
            pass

    class DummyResponses:
        async def create(self, **kwargs):
            nonlocal called_kwargs
            called_kwargs = kwargs
            return get_response_obj([])

    class DummyResponsesClient:
        def __init__(self):
            self.responses = DummyResponses()

    model = OpenAIResponsesModel(
        model="gpt-5.4",
        openai_client=DummyResponsesClient(),  # type: ignore[arg-type]
        model_is_explicit=False,
    )

    with pytest.raises(UserError, match="Computer tool is not initialized for serialization"):
        await model.get_response(
            system_instructions=None,
            input="hi",
            model_settings=ModelSettings(),
            tools=[ComputerTool(computer=lambda **_: DummyComputer())],
            output_schema=None,
            handoffs=[],
            tracing=ModelTracing.DISABLED,
            prompt={"id": "pmpt_123"},
        )

    assert called_kwargs == {}


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
@pytest.mark.parametrize("tool_choice", ["computer", "computer_use"])
async def test_prompt_id_explicit_ga_computer_tool_choice_uses_ga_selector_and_tool(
    tool_choice: str,
) -> None:
    called_kwargs: dict[str, Any] = {}

    class DummyComputer(Computer):
        @property
        def environment(self) -> str:  # type: ignore[override]
            return "mac"

        @property
        def dimensions(self) -> tuple[int, int]:
            return (800, 600)

        def screenshot(self) -> str:
            return "screenshot"

        def click(self, x: int, y: int, button: str) -> None:
            pass

        def double_click(self, x: int, y: int) -> None:
            pass

        def drag(self, path: list[tuple[int, int]]) -> None:
            pass

        def keypress(self, keys: list[str]) -> None:
            pass

        def move(self, x: int, y: int) -> None:
            pass

        def scroll(self, x: int, y: int, scroll_x: int, scroll_y: int) -> None:
            pass

        def type(self, text: str) -> None:
            pass

        def wait(self) -> None:
            pass

    class DummyResponses:
        async def create(self, **kwargs):
            nonlocal called_kwargs
            called_kwargs = kwargs
            return get_response_obj([])

    class DummyResponsesClient:
        def __init__(self):
            self.responses = DummyResponses()

    model = OpenAIResponsesModel(
        model="gpt-5.4",
        openai_client=DummyResponsesClient(),  # type: ignore[arg-type]
        model_is_explicit=False,
    )

    await model.get_response(
        system_instructions=None,
        input="hi",
        model_settings=ModelSettings(tool_choice=tool_choice),
        tools=[ComputerTool(computer=DummyComputer())],
        output_schema=None,
        handoffs=[],
        tracing=ModelTracing.DISABLED,
        prompt={"id": "pmpt_123"},
    )

    assert called_kwargs["model"] is omit
    assert called_kwargs["tool_choice"] == {"type": "computer"}
    assert called_kwargs["tools"] == [{"type": "computer"}]


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
@pytest.mark.parametrize("tool_choice", ["computer", "computer_use"])
async def test_preview_model_forced_computer_tool_choice_uses_preview_selector(
    tool_choice: str,
) -> None:
    called_kwargs: dict[str, Any] = {}

    class DummyComputer(Computer):
        @property
        def environment(self) -> str:  # type: ignore[override]
            return "mac"

        @property
        def dimensions(self) -> tuple[int, int]:
            return (800, 600)

        def screenshot(self) -> str:
            return "screenshot"

        def click(self, x: int, y: int, button: str) -> None:
            pass

        def double_click(self, x: int, y: int) -> None:
            pass

        def drag(self, path: list[tuple[int, int]]) -> None:
            pass

        def keypress(self, keys: list[str]) -> None:
            pass

        def move(self, x: int, y: int) -> None:
            pass

        def scroll(self, x: int, y: int, scroll_x: int, scroll_y: int) -> None:
            pass

        def type(self, text: str) -> None:
            pass

        def wait(self) -> None:
            pass

    class DummyResponses:
        async def create(self, **kwargs):
            nonlocal called_kwargs
            called_kwargs = kwargs
            return get_response_obj([])

    class DummyResponsesClient:
        def __init__(self):
            self.responses = DummyResponses()

    model = OpenAIResponsesModel(
        model="computer-use-preview",
        openai_client=DummyResponsesClient(),  # type: ignore[arg-type]
    )

    await model.get_response(
        system_instructions=None,
        input="hi",
        model_settings=ModelSettings(tool_choice=tool_choice),
        tools=[ComputerTool(computer=DummyComputer())],
        output_schema=None,
        handoffs=[],
        tracing=ModelTracing.DISABLED,
    )

    assert called_kwargs["model"] == "computer-use-preview"
    assert called_kwargs["tool_choice"] == {"type": "computer_use_preview"}
    assert called_kwargs["tools"] == [
        {
            "type": "computer_use_preview",
            "environment": "mac",
            "display_width": 800,
            "display_height": 600,
        }
    ]


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_websocket_model_reuses_connection_and_sends_response_create_frames(monkeypatch):
    client = DummyWSClient()
    ws = DummyWSConnection(
        [
            _response_completed_frame("resp-1", 1),
            _response_completed_frame("resp-2", 2),
        ]
    )
    model = OpenAIResponsesWSModel(model="gpt-4", openai_client=client)  # type: ignore[arg-type]

    opened: list[tuple[str, dict[str, str]]] = []

    async def fake_open(
        ws_url: str, headers: dict[str, str], *, connect_timeout: float | None = None
    ) -> DummyWSConnection:
        opened.append((ws_url, headers))
        return ws

    monkeypatch.setattr(model, "_open_websocket_connection", fake_open)

    first = await model.get_response(
        system_instructions=None,
        input="hi",
        model_settings=ModelSettings(reasoning=Reasoning(effort="medium", summary="detailed")),
        tools=[],
        output_schema=None,
        handoffs=[],
        tracing=ModelTracing.DISABLED,
    )
    second = await model.get_response(
        system_instructions=None,
        input="next",
        model_settings=ModelSettings(),
        tools=[],
        output_schema=None,
        handoffs=[],
        tracing=ModelTracing.DISABLED,
        previous_response_id="resp-1",
    )

    assert first.response_id == "resp-1"
    assert second.response_id == "resp-2"
    assert client.refresh_calls == 2
    assert len(opened) == 1
    assert ws.sent_messages[0]["type"] == "response.create"
    assert ws.sent_messages[0]["stream"] is True
    assert ws.sent_messages[0]["reasoning"] == {"effort": "medium", "summary": "detailed"}
    assert ws.sent_messages[1]["type"] == "response.create"
    assert ws.sent_messages[1]["stream"] is True
    assert ws.sent_messages[1]["previous_response_id"] == "resp-1"


@pytest.mark.asyncio
async def test_websocket_model_passes_keepalive_options_to_connect(monkeypatch):
    import websockets.asyncio.client as websockets_client

    client = DummyWSClient()
    model = OpenAIResponsesWSModel(
        model="gpt-4",
        openai_client=client,  # type: ignore[arg-type]
        websocket_options={"ping_interval": 45.0, "ping_timeout": None},
    )
    ws = DummyWSConnection([])
    captured_kwargs: dict[str, Any] = {}

    async def fake_connect(ws_url: str, **kwargs: Any) -> DummyWSConnection:
        captured_kwargs["ws_url"] = ws_url
        captured_kwargs.update(kwargs)
        return ws

    monkeypatch.setattr(websockets_client, "connect", fake_connect)

    opened = await model._open_websocket_connection(
        "wss://example.test/v1/responses",
        {"Authorization": "Bearer test-key"},
        connect_timeout=10.0,
    )

    assert opened is ws
    assert captured_kwargs["ws_url"] == "wss://example.test/v1/responses"
    assert captured_kwargs["additional_headers"] == {"Authorization": "Bearer test-key"}
    assert captured_kwargs["open_timeout"] == 10.0
    assert captured_kwargs["ping_interval"] == 45.0
    assert captured_kwargs["ping_timeout"] is None


@pytest.mark.allow_call_model_methods
def test_websocket_model_reconnects_when_reused_from_different_event_loop(monkeypatch):
    client = DummyWSClient()
    ws1 = DummyWSConnection([_response_completed_frame("resp-1", 1)])
    ws2 = DummyWSConnection([_response_completed_frame("resp-2", 2)])
    model = OpenAIResponsesWSModel(model="gpt-4", openai_client=client)  # type: ignore[arg-type]

    opened: list[tuple[str, dict[str, str]]] = []
    ws_connections = [ws1, ws2]

    async def fake_open(
        ws_url: str, headers: dict[str, str], *, connect_timeout: float | None = None
    ) -> DummyWSConnection:
        opened.append((ws_url, headers))
        return ws_connections.pop(0)

    monkeypatch.setattr(model, "_open_websocket_connection", fake_open)

    async def get_response(input_text: str, previous_response_id: str | None = None):
        return await model.get_response(
            system_instructions=None,
            input=input_text,
            model_settings=ModelSettings(),
            tools=[],
            output_schema=None,
            handoffs=[],
            tracing=ModelTracing.DISABLED,
            previous_response_id=previous_response_id,
        )

    loop1 = asyncio.new_event_loop()
    loop2 = asyncio.new_event_loop()
    try:
        first = loop1.run_until_complete(get_response("hi"))
        second = loop2.run_until_complete(get_response("next", previous_response_id="resp-1"))
    finally:
        loop1.close()
        loop2.close()
        asyncio.set_event_loop(None)

    assert first.response_id == "resp-1"
    assert second.response_id == "resp-2"
    assert len(opened) == 2
    assert ws1.close_calls == 1
    assert ws2.close_calls == 0


@pytest.mark.allow_call_model_methods
def test_websocket_model_init_lazily_creates_request_lock(monkeypatch):
    client = DummyWSClient()

    def fail_lock(*args, **kwargs):
        raise RuntimeError("asyncio.Lock() should not be called in __init__")

    monkeypatch.setattr("agents.models.openai_responses.asyncio.Lock", fail_lock)

    model = OpenAIResponsesWSModel(model="gpt-4", openai_client=client)  # type: ignore[arg-type]

    assert model._ws_request_lock is None


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_websocket_model_stream_response_yields_typed_events(monkeypatch):
    client = DummyWSClient()
    ws = DummyWSConnection([_response_completed_frame("resp-stream", 1)])
    model = OpenAIResponsesWSModel(model="gpt-4", openai_client=client)  # type: ignore[arg-type]

    async def fake_open(
        ws_url: str, headers: dict[str, str], *, connect_timeout: float | None = None
    ) -> DummyWSConnection:
        return ws

    monkeypatch.setattr(model, "_open_websocket_connection", fake_open)

    events = []
    async for event in model.stream_response(
        system_instructions=None,
        input="hi",
        model_settings=ModelSettings(),
        tools=[],
        output_schema=None,
        handoffs=[],
        tracing=ModelTracing.DISABLED,
    ):
        events.append(event)

    assert len(events) == 1
    assert isinstance(events[0], ResponseCompletedEvent)
    assert events[0].response.id == "resp-stream"


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
@pytest.mark.parametrize("terminal_event_type", ["response.incomplete", "response.failed"])
async def test_websocket_model_get_response_rejects_failed_terminal_response_payload_events(
    monkeypatch, terminal_event_type: str
):
    client = DummyWSClient()
    ws = DummyWSConnection([_response_event_frame(terminal_event_type, "resp-terminal", 1)])
    model = OpenAIResponsesWSModel(model="gpt-4", openai_client=client)  # type: ignore[arg-type]

    async def fake_open(
        ws_url: str, headers: dict[str, str], *, connect_timeout: float | None = None
    ) -> DummyWSConnection:
        return ws

    monkeypatch.setattr(model, "_open_websocket_connection", fake_open)

    with pytest.raises(ModelBehaviorError, match=terminal_event_type):
        await model.get_response(
            system_instructions=None,
            input="hi",
            model_settings=ModelSettings(),
            tools=[],
            output_schema=None,
            handoffs=[],
            tracing=ModelTracing.DISABLED,
        )


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
@pytest.mark.parametrize("terminal_event_type", ["response.incomplete", "response.failed"])
async def test_websocket_model_stream_response_rejects_failed_terminal_response_payload_events(
    monkeypatch, terminal_event_type: str
):
    client = DummyWSClient()
    ws = DummyWSConnection([_response_event_frame(terminal_event_type, "resp-terminal", 1)])
    model = OpenAIResponsesWSModel(model="gpt-4", openai_client=client)  # type: ignore[arg-type]

    async def fake_open(
        ws_url: str, headers: dict[str, str], *, connect_timeout: float | None = None
    ) -> DummyWSConnection:
        return ws

    monkeypatch.setattr(model, "_open_websocket_connection", fake_open)

    events = []
    with pytest.raises(ModelBehaviorError, match=terminal_event_type):
        async for event in model.stream_response(
            system_instructions=None,
            input="hi",
            model_settings=ModelSettings(),
            tools=[],
            output_schema=None,
            handoffs=[],
            tracing=ModelTracing.DISABLED,
        ):
            events.append(event)

    assert len(events) == 1
    assert events[0].type == terminal_event_type
    assert cast(Any, events[0]).response.id == "resp-terminal"


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_stream_response_rejects_response_error_terminal_event(monkeypatch):
    model = OpenAIResponsesModel(model="gpt-4", openai_client=object())  # type: ignore[arg-type]

    async def dummy_fetch_response(
        system_instructions,
        input,
        model_settings,
        tools,
        output_schema,
        handoffs,
        previous_response_id,
        conversation_id,
        stream,
        prompt,
    ):
        class DummyStream:
            async def __aiter__(self):
                yield ResponseErrorEvent(
                    type="error",
                    code="invalid_request_error",
                    message="bad request",
                    param=None,
                    sequence_number=0,
                )

        return DummyStream()

    monkeypatch.setattr(model, "_fetch_response", dummy_fetch_response)

    events = []
    with pytest.raises(ModelBehaviorError, match="invalid_request_error"):
        async for event in model.stream_response(
            system_instructions=None,
            input="hi",
            model_settings=ModelSettings(),
            tools=[],
            output_schema=None,
            handoffs=[],
            tracing=ModelTracing.DISABLED,
        ):
            events.append(event)

    assert len(events) == 1
    assert events[0].type == "error"
    assert events[0].code == "invalid_request_error"
    assert events[0].message == "bad request"


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_websocket_model_get_response_surfaces_response_error_event(monkeypatch):
    client = DummyWSClient()
    ws = DummyWSConnection([_response_error_frame("invalid_request_error", "bad request", 1)])
    model = OpenAIResponsesWSModel(model="gpt-4", openai_client=client)  # type: ignore[arg-type]

    async def fake_open(
        ws_url: str, headers: dict[str, str], *, connect_timeout: float | None = None
    ) -> DummyWSConnection:
        return ws

    monkeypatch.setattr(model, "_open_websocket_connection", fake_open)

    with pytest.raises(ResponsesWebSocketError, match="response\\.error") as exc_info:
        await model.get_response(
            system_instructions=None,
            input="hi",
            model_settings=ModelSettings(),
            tools=[],
            output_schema=None,
            handoffs=[],
            tracing=ModelTracing.DISABLED,
        )

    assert "invalid_request_error" in str(exc_info.value)
    assert "bad request" in str(exc_info.value)
    assert exc_info.value.event_type == "response.error"
    assert exc_info.value.code == "invalid_request_error"
    assert exc_info.value.error_message == "bad request"


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_websocket_model_stream_response_raises_on_response_error_event(monkeypatch):
    client = DummyWSClient()
    ws = DummyWSConnection([_response_error_frame("invalid_request_error", "bad request", 1)])
    model = OpenAIResponsesWSModel(model="gpt-4", openai_client=client)  # type: ignore[arg-type]

    async def fake_open(
        ws_url: str, headers: dict[str, str], *, connect_timeout: float | None = None
    ) -> DummyWSConnection:
        return ws

    monkeypatch.setattr(model, "_open_websocket_connection", fake_open)

    with pytest.raises(ResponsesWebSocketError, match="response\\.error") as exc_info:
        async for _event in model.stream_response(
            system_instructions=None,
            input="hi",
            model_settings=ModelSettings(),
            tools=[],
            output_schema=None,
            handoffs=[],
            tracing=ModelTracing.DISABLED,
        ):
            pass

    assert "invalid_request_error" in str(exc_info.value)
    assert "bad request" in str(exc_info.value)


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_websocket_model_stream_break_drops_persistent_connection(monkeypatch):
    client = DummyWSClient()
    ws = DummyWSConnection(
        [
            _response_event_frame("response.created", "resp-created", 1),
            _response_completed_frame("resp-complete", 2),
        ]
    )
    model = OpenAIResponsesWSModel(model="gpt-4", openai_client=client)  # type: ignore[arg-type]

    async def fake_open(
        ws_url: str, headers: dict[str, str], *, connect_timeout: float | None = None
    ) -> DummyWSConnection:
        return ws

    monkeypatch.setattr(model, "_open_websocket_connection", fake_open)

    stream = await model._fetch_response(
        system_instructions=None,
        input="hi",
        model_settings=ModelSettings(),
        tools=[],
        output_schema=None,
        handoffs=[],
        previous_response_id=None,
        conversation_id=None,
        stream=True,
    )

    stream_agen = cast(Any, stream)
    event = await stream_agen.__anext__()
    assert event.type == "response.created"
    await stream_agen.aclose()

    assert ws.close_calls == 0
    assert model._ws_connection is None


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_websocket_model_stream_close_after_terminal_event_preserves_persistent_connection(
    monkeypatch,
):
    client = DummyWSClient()
    ws = DummyWSConnection(
        [
            _response_completed_frame("resp-complete-1", 1),
            _response_completed_frame("resp-complete-2", 2),
        ]
    )
    model = OpenAIResponsesWSModel(model="gpt-4", openai_client=client)  # type: ignore[arg-type]

    opened: list[DummyWSConnection] = []

    async def fake_open(
        ws_url: str, headers: dict[str, str], *, connect_timeout: float | None = None
    ) -> DummyWSConnection:
        opened.append(ws)
        return ws

    monkeypatch.setattr(model, "_open_websocket_connection", fake_open)

    stream = await model._fetch_response(
        system_instructions=None,
        input="hi",
        model_settings=ModelSettings(),
        tools=[],
        output_schema=None,
        handoffs=[],
        previous_response_id=None,
        conversation_id=None,
        stream=True,
    )

    stream_agen = cast(Any, stream)
    event = await stream_agen.__anext__()
    assert event.type == "response.completed"
    await stream_agen.aclose()

    assert ws.close_calls == 0
    assert model._ws_connection is ws
    assert model._ws_request_lock is not None
    assert model._ws_request_lock.locked() is False

    second = await model.get_response(
        system_instructions=None,
        input="next",
        model_settings=ModelSettings(),
        tools=[],
        output_schema=None,
        handoffs=[],
        tracing=ModelTracing.DISABLED,
    )

    assert second.response_id == "resp-complete-2"
    assert len(opened) == 1


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_websocket_model_stream_response_terminal_close_keeps_connection(
    monkeypatch,
):
    client = DummyWSClient()
    ws = DummyWSConnection(
        [
            _response_completed_frame("resp-complete-1", 1),
            _response_completed_frame("resp-complete-2", 2),
        ]
    )
    model = OpenAIResponsesWSModel(model="gpt-4", openai_client=client)  # type: ignore[arg-type]

    opened: list[DummyWSConnection] = []

    async def fake_open(
        ws_url: str, headers: dict[str, str], *, connect_timeout: float | None = None
    ) -> DummyWSConnection:
        opened.append(ws)
        return ws

    monkeypatch.setattr(model, "_open_websocket_connection", fake_open)

    stream = model.stream_response(
        system_instructions=None,
        input="hi",
        model_settings=ModelSettings(),
        tools=[],
        output_schema=None,
        handoffs=[],
        tracing=ModelTracing.DISABLED,
    )

    stream_agen = cast(Any, stream)
    event = await stream_agen.__anext__()
    assert event.type == "response.completed"
    await stream_agen.aclose()

    assert ws.close_calls == 0
    assert model._ws_connection is ws

    second = await model.get_response(
        system_instructions=None,
        input="next",
        model_settings=ModelSettings(),
        tools=[],
        output_schema=None,
        handoffs=[],
        tracing=ModelTracing.DISABLED,
    )

    assert second.response_id == "resp-complete-2"
    assert len(opened) == 1


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_websocket_model_stream_response_close_releases_inner_iterator(monkeypatch):
    client = DummyWSClient()
    ws = DummyWSConnection(
        [
            _response_event_frame("response.created", "resp-created", 1),
            _response_completed_frame("resp-complete", 2),
        ]
    )
    model = OpenAIResponsesWSModel(model="gpt-4", openai_client=client)  # type: ignore[arg-type]

    async def fake_open(
        ws_url: str, headers: dict[str, str], *, connect_timeout: float | None = None
    ) -> DummyWSConnection:
        return ws

    monkeypatch.setattr(model, "_open_websocket_connection", fake_open)

    stream = model.stream_response(
        system_instructions=None,
        input="hi",
        model_settings=ModelSettings(),
        tools=[],
        output_schema=None,
        handoffs=[],
        tracing=ModelTracing.DISABLED,
    )

    stream_agen = cast(Any, stream)
    event = await stream_agen.__anext__()
    assert event.type == "response.created"
    await stream_agen.aclose()

    assert ws.close_calls == 0
    assert model._ws_connection is None
    assert model._ws_request_lock is not None
    assert model._ws_request_lock.locked() is False


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_websocket_model_stream_response_non_terminal_close_does_not_await_close_handshake(
    monkeypatch,
):
    class BlockingCloseWSConnection(DummyWSConnection):
        def __init__(self):
            super().__init__(
                [
                    _response_event_frame("response.created", "resp-created", 1),
                    _response_completed_frame("resp-complete", 2),
                ]
            )
            self.close_started = asyncio.Event()
            self.close_release = asyncio.Event()

            class DummyTransport:
                def __init__(inner_self, outer: BlockingCloseWSConnection):
                    inner_self.outer = outer
                    inner_self.abort_calls = 0

                def abort(inner_self) -> None:
                    inner_self.abort_calls += 1

            self.transport = DummyTransport(self)

        async def close(self) -> None:
            self.close_calls += 1
            self.close_started.set()
            await self.close_release.wait()

    client = DummyWSClient()
    ws = BlockingCloseWSConnection()
    model = OpenAIResponsesWSModel(model="gpt-4", openai_client=client)  # type: ignore[arg-type]

    async def fake_open(
        ws_url: str, headers: dict[str, str], *, connect_timeout: float | None = None
    ) -> DummyWSConnection:
        return ws

    monkeypatch.setattr(model, "_open_websocket_connection", fake_open)

    stream = model.stream_response(
        system_instructions=None,
        input="hi",
        model_settings=ModelSettings(),
        tools=[],
        output_schema=None,
        handoffs=[],
        tracing=ModelTracing.DISABLED,
    )

    stream_agen = cast(Any, stream)
    event = await stream_agen.__anext__()
    assert event.type == "response.created"

    try:
        await asyncio.wait_for(stream_agen.aclose(), timeout=0.5)
        assert ws.transport.abort_calls == 1
        assert ws.close_calls == 0
        assert model._ws_connection is None
        assert model._ws_request_lock is not None
        assert model._ws_request_lock.locked() is False
    finally:
        ws.close_release.set()
        await asyncio.sleep(0)


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_websocket_model_cancellation_drops_persistent_connection(monkeypatch):
    class CancelOnRecvWSConnection(DummyWSConnection):
        async def recv(self) -> str:
            raise asyncio.CancelledError()

    client = DummyWSClient()
    ws = CancelOnRecvWSConnection([])
    model = OpenAIResponsesWSModel(model="gpt-4", openai_client=client)  # type: ignore[arg-type]

    async def fake_open(
        ws_url: str, headers: dict[str, str], *, connect_timeout: float | None = None
    ) -> DummyWSConnection:
        return ws

    monkeypatch.setattr(model, "_open_websocket_connection", fake_open)

    with pytest.raises(asyncio.CancelledError):
        await model.get_response(
            system_instructions=None,
            input="hi",
            model_settings=ModelSettings(),
            tools=[],
            output_schema=None,
            handoffs=[],
            tracing=ModelTracing.DISABLED,
        )

    assert ws.close_calls == 0
    assert model._ws_connection is None


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_websocket_model_cancellation_does_not_await_close_handshake(monkeypatch):
    class BlockingCloseCancelOnRecvWSConnection(DummyWSConnection):
        def __init__(self):
            super().__init__([])
            self.recv_started = asyncio.Event()
            self.close_started = asyncio.Event()
            self.close_release = asyncio.Event()

            class DummyTransport:
                def __init__(inner_self, outer: BlockingCloseCancelOnRecvWSConnection):
                    inner_self.outer = outer
                    inner_self.abort_calls = 0

                def abort(inner_self) -> None:
                    inner_self.abort_calls += 1

            self.transport = DummyTransport(self)

        async def recv(self) -> str:
            self.recv_started.set()
            await asyncio.Event().wait()
            raise RuntimeError("unreachable")

        async def close(self) -> None:
            self.close_calls += 1
            self.close_started.set()
            await self.close_release.wait()

    client = DummyWSClient()
    ws = BlockingCloseCancelOnRecvWSConnection()
    model = OpenAIResponsesWSModel(model="gpt-4", openai_client=client)  # type: ignore[arg-type]

    async def fake_open(
        ws_url: str, headers: dict[str, str], *, connect_timeout: float | None = None
    ) -> DummyWSConnection:
        return ws

    monkeypatch.setattr(model, "_open_websocket_connection", fake_open)

    request_task = asyncio.create_task(
        model.get_response(
            system_instructions=None,
            input="hi",
            model_settings=ModelSettings(),
            tools=[],
            output_schema=None,
            handoffs=[],
            tracing=ModelTracing.DISABLED,
        )
    )

    await asyncio.wait_for(ws.recv_started.wait(), timeout=1.0)
    request_task.cancel()

    try:
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(request_task, timeout=0.5)
        assert ws.transport.abort_calls == 1
        assert ws.close_calls == 0
        assert model._ws_connection is None
    finally:
        ws.close_release.set()
        await asyncio.sleep(0)


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_websocket_model_preserves_pre_event_usererror(monkeypatch):
    client = DummyWSClient()
    model = OpenAIResponsesWSModel(model="gpt-4", openai_client=client)  # type: ignore[arg-type]

    async def fake_open(
        ws_url: str, headers: dict[str, str], *, connect_timeout: float | None = None
    ) -> DummyWSConnection:
        raise UserError("websockets dependency missing")

    monkeypatch.setattr(model, "_open_websocket_connection", fake_open)

    with pytest.raises(UserError, match="websockets dependency missing"):
        await model.get_response(
            system_instructions=None,
            input="hi",
            model_settings=ModelSettings(),
            tools=[],
            output_schema=None,
            handoffs=[],
            tracing=ModelTracing.DISABLED,
        )


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_websocket_model_preserves_pre_event_server_error_frame_message(monkeypatch):
    client = DummyWSClient()
    ws = DummyWSConnection(
        [
            json.dumps(
                {
                    "type": "error",
                    "error": {"message": "bad auth", "type": "invalid_request_error"},
                }
            )
        ]
    )
    model = OpenAIResponsesWSModel(model="gpt-4", openai_client=client)  # type: ignore[arg-type]

    async def fake_open(
        ws_url: str, headers: dict[str, str], *, connect_timeout: float | None = None
    ) -> DummyWSConnection:
        return ws

    monkeypatch.setattr(model, "_open_websocket_connection", fake_open)

    with pytest.raises(ResponsesWebSocketError, match="Responses websocket error:") as exc_info:
        await model.get_response(
            system_instructions=None,
            input="hi",
            model_settings=ModelSettings(),
            tools=[],
            output_schema=None,
            handoffs=[],
            tracing=ModelTracing.DISABLED,
        )

    assert "feature may not be enabled" not in str(exc_info.value)
    assert "invalid_request_error" in str(exc_info.value)
    assert exc_info.value.event_type == "error"
    assert exc_info.value.error_type == "invalid_request_error"


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_websocket_model_reconnects_if_cached_connection_is_closed(monkeypatch):
    client = DummyWSClient()
    ws1 = DummyWSConnection([_response_completed_frame("resp-1", 1)])
    ws2 = DummyWSConnection([_response_completed_frame("resp-2", 2)])
    model = OpenAIResponsesWSModel(model="gpt-4", openai_client=client)  # type: ignore[arg-type]

    opened: list[DummyWSConnection] = []
    queue = [ws1, ws2]

    async def fake_open(
        ws_url: str, headers: dict[str, str], *, connect_timeout: float | None = None
    ) -> DummyWSConnection:
        next_ws = queue.pop(0)
        opened.append(next_ws)
        return next_ws

    monkeypatch.setattr(model, "_open_websocket_connection", fake_open)

    first = await model.get_response(
        system_instructions=None,
        input="hi",
        model_settings=ModelSettings(),
        tools=[],
        output_schema=None,
        handoffs=[],
        tracing=ModelTracing.DISABLED,
    )
    assert first.response_id == "resp-1"
    assert len(opened) == 1

    # Simulate an idle timeout/server-side close on the cached websocket connection.
    ws1.close_code = 1001

    second = await model.get_response(
        system_instructions=None,
        input="next",
        model_settings=ModelSettings(),
        tools=[],
        output_schema=None,
        handoffs=[],
        tracing=ModelTracing.DISABLED,
    )

    assert second.response_id == "resp-2"
    assert len(opened) == 2
    assert ws1.close_calls == 1
    assert model._ws_connection is ws2


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_websocket_model_does_not_retry_if_send_raises_after_writing_on_reused_connection(
    monkeypatch,
):
    client = DummyWSClient()

    class ConnectionClosedError(Exception):
        pass

    ConnectionClosedError.__module__ = "websockets.client"

    class DropAfterSendWriteOnReuseWSConnection(DummyWSConnection):
        def __init__(self, frames: list[str]):
            super().__init__(frames)
            self.send_calls = 0

        async def send(self, payload: str) -> None:
            self.send_calls += 1
            if self.send_calls > 1:
                await super().send(payload)
                raise ConnectionClosedError("peer closed during send after request write")
            await super().send(payload)

    ws1 = DropAfterSendWriteOnReuseWSConnection([_response_completed_frame("resp-1", 1)])
    model = OpenAIResponsesWSModel(model="gpt-4", openai_client=client)  # type: ignore[arg-type]

    open_calls = 0

    async def fake_open(
        ws_url: str, headers: dict[str, str], *, connect_timeout: float | None = None
    ) -> DummyWSConnection:
        nonlocal open_calls
        open_calls += 1
        if open_calls > 1:
            raise AssertionError("Unexpected websocket retry after send started")
        return ws1

    monkeypatch.setattr(model, "_open_websocket_connection", fake_open)

    first = await model.get_response(
        system_instructions=None,
        input="hi",
        model_settings=ModelSettings(),
        tools=[],
        output_schema=None,
        handoffs=[],
        tracing=ModelTracing.DISABLED,
    )
    with pytest.raises(RuntimeError, match="before any response events were received"):
        await model.get_response(
            system_instructions=None,
            input="next",
            model_settings=ModelSettings(),
            tools=[],
            output_schema=None,
            handoffs=[],
            tracing=ModelTracing.DISABLED,
        )

    assert first.response_id == "resp-1"
    assert open_calls == 1
    assert ws1.send_calls == 2
    assert len(ws1.sent_messages) == 2
    assert ws1.close_calls == 1
    assert model._ws_connection is None


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_websocket_model_does_not_retry_after_pre_event_disconnect_once_request_sent(
    monkeypatch,
):
    client = DummyWSClient()

    class ConnectionClosedError(Exception):
        pass

    ConnectionClosedError.__module__ = "websockets.client"

    class DisconnectAfterSendWSConnection(DummyWSConnection):
        def __init__(self):
            super().__init__([])
            self.send_calls = 0
            self.recv_calls = 0

        async def send(self, payload: str) -> None:
            self.send_calls += 1
            await super().send(payload)

        async def recv(self) -> str:
            self.recv_calls += 1
            raise ConnectionClosedError("peer closed after request send")

    ws = DisconnectAfterSendWSConnection()
    model = OpenAIResponsesWSModel(model="gpt-4", openai_client=client)  # type: ignore[arg-type]
    open_calls = 0

    async def fake_open(
        ws_url: str, headers: dict[str, str], *, connect_timeout: float | None = None
    ) -> DisconnectAfterSendWSConnection:
        nonlocal open_calls
        open_calls += 1
        if open_calls > 1:
            raise AssertionError("Unexpected websocket retry after request frame was sent")
        return ws

    monkeypatch.setattr(model, "_open_websocket_connection", fake_open)

    with pytest.raises(RuntimeError, match="before any response events were received"):
        await model.get_response(
            system_instructions=None,
            input="hi",
            model_settings=ModelSettings(),
            tools=[],
            output_schema=None,
            handoffs=[],
            tracing=ModelTracing.DISABLED,
        )

    assert open_calls == 1
    assert ws.send_calls == 1
    assert ws.recv_calls == 1
    assert ws.close_calls == 1
    assert model._ws_connection is None


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_websocket_model_does_not_retry_after_client_initiated_close(monkeypatch):
    client = DummyWSClient()

    class ConnectionClosedError(Exception):
        pass

    ConnectionClosedError.__module__ = "websockets.client"

    class AbortableRecvWSConnection(DummyWSConnection):
        def __init__(self):
            super().__init__([])
            self.send_calls = 0
            self.recv_started = asyncio.Event()
            self.abort_event = asyncio.Event()

            class DummyTransport:
                def __init__(inner_self, outer: AbortableRecvWSConnection):
                    inner_self.outer = outer
                    inner_self.abort_calls = 0

                def abort(inner_self) -> None:
                    inner_self.abort_calls += 1
                    inner_self.outer.abort_event.set()

            self.transport = DummyTransport(self)

        async def send(self, payload: str) -> None:
            self.send_calls += 1
            await super().send(payload)

        async def recv(self) -> str:
            self.recv_started.set()
            await self.abort_event.wait()
            raise ConnectionClosedError("client closed websocket")

    ws = AbortableRecvWSConnection()
    model = OpenAIResponsesWSModel(model="gpt-4", openai_client=client)  # type: ignore[arg-type]
    open_calls = 0

    async def fake_open(
        ws_url: str, headers: dict[str, str], *, connect_timeout: float | None = None
    ) -> AbortableRecvWSConnection:
        nonlocal open_calls
        open_calls += 1
        if open_calls > 1:
            raise AssertionError("Unexpected websocket reconnect after client close")
        return ws

    monkeypatch.setattr(model, "_open_websocket_connection", fake_open)

    request_task = asyncio.create_task(
        model.get_response(
            system_instructions=None,
            input="hi",
            model_settings=ModelSettings(),
            tools=[],
            output_schema=None,
            handoffs=[],
            tracing=ModelTracing.DISABLED,
        )
    )

    await asyncio.wait_for(ws.recv_started.wait(), timeout=1.0)
    await asyncio.wait_for(model.close(), timeout=1.0)

    with pytest.raises(ConnectionClosedError, match="client closed websocket"):
        await asyncio.wait_for(request_task, timeout=1.0)

    assert open_calls == 1
    assert ws.send_calls == 1
    assert ws.transport.abort_calls == 1
    assert model._ws_connection is None


@pytest.mark.allow_call_model_methods
def test_websocket_model_prepare_websocket_url_preserves_non_tls_scheme_mapping():
    client = DummyWSClient()
    client.base_url = httpx.URL("http://127.0.0.1:8080/v1/")
    model = OpenAIResponsesWSModel(model="gpt-4", openai_client=client)  # type: ignore[arg-type]

    ws_url = model._prepare_websocket_url(extra_query=None)

    assert ws_url == "ws://127.0.0.1:8080/v1/responses"


@pytest.mark.allow_call_model_methods
def test_websocket_model_prepare_websocket_url_appends_path_with_existing_query():
    client = DummyWSClient()
    client.websocket_base_url = "wss://proxy.example.test/v1?token=abc"
    model = OpenAIResponsesWSModel(model="gpt-4", openai_client=client)  # type: ignore[arg-type]

    ws_url = model._prepare_websocket_url(extra_query={"route": "team-a"})
    parsed = httpx.URL(ws_url)

    assert parsed.path == "/v1/responses"
    assert dict(parsed.params) == {"token": "abc", "route": "team-a"}


@pytest.mark.allow_call_model_methods
@pytest.mark.parametrize(
    ("configured_ws_base_url", "expected_scheme"),
    [
        ("http://proxy.example.test/v1?token=abc", "ws"),
        ("https://proxy.example.test/v1?token=abc", "wss"),
    ],
)
def test_websocket_model_prepare_websocket_url_normalizes_explicit_http_schemes(
    configured_ws_base_url: str, expected_scheme: str
):
    client = DummyWSClient()
    client.websocket_base_url = configured_ws_base_url
    model = OpenAIResponsesWSModel(model="gpt-4", openai_client=client)  # type: ignore[arg-type]

    ws_url = model._prepare_websocket_url(extra_query={"route": "team-a"})
    parsed = httpx.URL(ws_url)

    assert parsed.scheme == expected_scheme
    assert parsed.path == "/v1/responses"
    assert dict(parsed.params) == {"token": "abc", "route": "team-a"}


@pytest.mark.allow_call_model_methods
@pytest.mark.parametrize("extra_query", [omit, NOT_GIVEN])
def test_websocket_model_prepare_websocket_url_treats_top_level_omit_sentinels_as_absent(
    extra_query,
):
    client = DummyWSClient()
    client.websocket_base_url = "wss://proxy.example.test/v1?token=abc"
    model = OpenAIResponsesWSModel(model="gpt-4", openai_client=client)  # type: ignore[arg-type]

    ws_url = model._prepare_websocket_url(extra_query=extra_query)
    parsed = httpx.URL(ws_url)

    assert parsed.path == "/v1/responses"
    assert dict(parsed.params) == {"token": "abc"}


@pytest.mark.allow_call_model_methods
def test_websocket_model_prepare_websocket_url_skips_not_given_query_values():
    client = DummyWSClient()
    client.websocket_base_url = "wss://proxy.example.test/v1?token=abc"
    client.default_query = {"api-version": NOT_GIVEN, "route": "team-a"}
    model = OpenAIResponsesWSModel(model="gpt-4", openai_client=client)  # type: ignore[arg-type]

    ws_url = model._prepare_websocket_url(extra_query={"tenant": NOT_GIVEN, "region": "us"})
    parsed = httpx.URL(ws_url)

    assert parsed.path == "/v1/responses"
    assert dict(parsed.params) == {"token": "abc", "route": "team-a", "region": "us"}


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_websocket_model_prepare_websocket_request_filters_omit_from_extra_body():
    client = DummyWSClient()
    model = OpenAIResponsesWSModel(model="gpt-4", openai_client=client)  # type: ignore[arg-type]

    frame, _ws_url, _headers = await model._prepare_websocket_request(
        {
            "model": "gpt-4",
            "input": "hi",
            "stream": True,
            "extra_body": {"keep": "value", "drop": omit},
        }
    )

    assert frame["type"] == "response.create"
    assert frame["keep"] == "value"
    assert "drop" not in frame


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
@pytest.mark.parametrize("extra_body", [omit, NOT_GIVEN])
async def test_websocket_model_prepare_websocket_request_ignores_top_level_extra_body_sentinels(
    extra_body,
):
    client = DummyWSClient()
    model = OpenAIResponsesWSModel(model="gpt-4", openai_client=client)  # type: ignore[arg-type]

    frame, _ws_url, _headers = await model._prepare_websocket_request(
        {
            "model": "gpt-4",
            "input": "hi",
            "stream": True,
            "extra_body": extra_body,
        }
    )

    assert frame["type"] == "response.create"
    assert frame["stream"] is True
    assert frame["model"] == "gpt-4"
    assert frame["input"] == "hi"


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_websocket_model_prepare_websocket_request_preserves_envelope_fields():
    client = DummyWSClient()
    model = OpenAIResponsesWSModel(model="gpt-4", openai_client=client)  # type: ignore[arg-type]

    frame, _ws_url, _headers = await model._prepare_websocket_request(
        {
            "model": "gpt-4",
            "input": "hi",
            "stream": True,
            "extra_body": {
                "type": "not-response-create",
                "stream": False,
                "custom": "value",
            },
        }
    )

    assert frame["type"] == "response.create"
    assert frame["stream"] is True
    assert frame["custom"] == "value"


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_websocket_model_prepare_websocket_request_strips_client_timeout_kwarg():
    client = DummyWSClient()
    model = OpenAIResponsesWSModel(model="gpt-4", openai_client=client)  # type: ignore[arg-type]

    frame, _ws_url, _headers = await model._prepare_websocket_request(
        {
            "model": "gpt-4",
            "input": "hi",
            "stream": True,
            "timeout": 30.0,
            "metadata": {"request_id": "123"},
        }
    )

    assert frame["type"] == "response.create"
    assert frame["metadata"] == {"request_id": "123"}
    assert "timeout" not in frame


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_websocket_model_prepare_websocket_request_skips_not_given_values():
    client = DummyWSClient()
    model = OpenAIResponsesWSModel(model="gpt-4", openai_client=client)  # type: ignore[arg-type]

    frame, _ws_url, _headers = await model._prepare_websocket_request(
        {
            "model": "gpt-4",
            "input": "hi",
            "stream": True,
            "user": NOT_GIVEN,
            "stream_options": NOT_GIVEN,
            "extra_body": {
                "metadata": {"request_id": "123"},
                "optional_field": NOT_GIVEN,
            },
        }
    )

    assert frame["type"] == "response.create"
    assert frame["stream"] is True
    assert frame["metadata"] == {"request_id": "123"}
    assert "user" not in frame
    assert "stream_options" not in frame
    assert "optional_field" not in frame
    json.dumps(frame)


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_websocket_model_get_response_applies_timeout_to_recv(monkeypatch):
    client = DummyWSClient()
    model = OpenAIResponsesWSModel(model="gpt-4", openai_client=client)  # type: ignore[arg-type]

    class SlowRecvWSConnection(DummyWSConnection):
        async def recv(self) -> str:
            await asyncio.sleep(0.2)
            return await super().recv()

    ws = SlowRecvWSConnection([_response_completed_frame("resp-timeout", 1)])

    async def fake_open(
        ws_url: str, headers: dict[str, str], *, connect_timeout: float | None = None
    ) -> DummyWSConnection:
        return ws

    monkeypatch.setattr(model, "_open_websocket_connection", fake_open)

    with pytest.raises(TimeoutError, match="Responses websocket receive timed out"):
        await model.get_response(
            system_instructions=None,
            input="hi",
            model_settings=ModelSettings(extra_args={"timeout": 0.01}),
            tools=[],
            output_schema=None,
            handoffs=[],
            tracing=ModelTracing.DISABLED,
        )

    assert ws.close_calls == 1


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_websocket_model_get_response_marks_partial_receive_timeout_unsafe_to_replay(
    monkeypatch,
):
    client = DummyWSClient()
    model = OpenAIResponsesWSModel(model="gpt-4", openai_client=client)  # type: ignore[arg-type]

    class PartialThenSlowRecvWSConnection(DummyWSConnection):
        def __init__(self) -> None:
            super().__init__([_response_event_frame("response.created", "resp-partial", 1)])
            self.recv_calls = 0

        async def recv(self) -> str:
            self.recv_calls += 1
            if self.recv_calls == 1:
                return await super().recv()
            await asyncio.sleep(0.2)
            return await super().recv()

    ws = PartialThenSlowRecvWSConnection()

    async def fake_open(
        ws_url: str, headers: dict[str, str], *, connect_timeout: float | None = None
    ) -> DummyWSConnection:
        return ws

    monkeypatch.setattr(model, "_open_websocket_connection", fake_open)

    with pytest.raises(TimeoutError, match="Responses websocket receive timed out") as exc_info:
        await model.get_response(
            system_instructions=None,
            input="hi",
            model_settings=ModelSettings(extra_args={"timeout": 0.01}),
            tools=[],
            output_schema=None,
            handoffs=[],
            tracing=ModelTracing.DISABLED,
        )

    error = exc_info.value
    assert getattr(error, "_openai_agents_ws_replay_safety", None) == "unsafe"

    advice = model.get_retry_advice(
        ModelRetryAdviceRequest(
            error=error,
            attempt=1,
            stream=False,
        )
    )

    assert advice is not None
    assert advice.suggested is False
    assert advice.replay_safety == "unsafe"
    assert ws.close_calls == 1


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_websocket_model_get_response_applies_timeout_while_waiting_for_request_lock(
    monkeypatch,
):
    client = DummyWSClient()
    model = OpenAIResponsesWSModel(model="gpt-4", openai_client=client)  # type: ignore[arg-type]
    recv_started = asyncio.Event()
    release_first_request = asyncio.Event()

    class BlockingRecvWSConnection(DummyWSConnection):
        async def recv(self) -> str:
            recv_started.set()
            await release_first_request.wait()
            return await super().recv()

    ws = BlockingRecvWSConnection(
        [
            _response_completed_frame("resp-lock-1", 1),
            _response_completed_frame("resp-lock-2", 2),
        ]
    )

    async def fake_open(
        ws_url: str, headers: dict[str, str], *, connect_timeout: float | None = None
    ) -> DummyWSConnection:
        return ws

    monkeypatch.setattr(model, "_open_websocket_connection", fake_open)

    first_task = asyncio.create_task(
        model.get_response(
            system_instructions=None,
            input="first",
            model_settings=ModelSettings(),
            tools=[],
            output_schema=None,
            handoffs=[],
            tracing=ModelTracing.DISABLED,
        )
    )

    await asyncio.wait_for(recv_started.wait(), timeout=1.0)

    with pytest.raises(TimeoutError, match="request lock wait timed out"):
        await model.get_response(
            system_instructions=None,
            input="second",
            model_settings=ModelSettings(extra_args={"timeout": 0.01}),
            tools=[],
            output_schema=None,
            handoffs=[],
            tracing=ModelTracing.DISABLED,
        )

    release_first_request.set()
    first_response = await first_task

    assert first_response.response_id == "resp-lock-1"
    assert len(ws.sent_messages) == 1


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_websocket_model_get_response_allows_zero_pool_timeout_when_lock_uncontended(
    monkeypatch,
):
    client = DummyWSClient()
    client.timeout = httpx.Timeout(connect=1.0, read=1.0, write=1.0, pool=0.0)
    model = OpenAIResponsesWSModel(model="gpt-4", openai_client=client)  # type: ignore[arg-type]
    ws = DummyWSConnection([_response_completed_frame("resp-zero-pool", 1)])

    async def fake_open(
        ws_url: str, headers: dict[str, str], *, connect_timeout: float | None = None
    ) -> DummyWSConnection:
        return ws

    monkeypatch.setattr(model, "_open_websocket_connection", fake_open)

    response = await model.get_response(
        system_instructions=None,
        input="hi",
        model_settings=ModelSettings(),
        tools=[],
        output_schema=None,
        handoffs=[],
        tracing=ModelTracing.DISABLED,
    )

    assert response.response_id == "resp-zero-pool"
    assert len(ws.sent_messages) == 1


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_websocket_model_get_response_allows_zero_timeout_when_ws_ops_are_immediate(
    monkeypatch,
):
    client = DummyWSClient()
    model = OpenAIResponsesWSModel(model="gpt-4", openai_client=client)  # type: ignore[arg-type]
    ws = DummyWSConnection([_response_completed_frame("resp-zero-timeout", 1)])

    async def fake_open(
        ws_url: str, headers: dict[str, str], *, connect_timeout: float | None = None
    ) -> DummyWSConnection:
        return ws

    monkeypatch.setattr(model, "_open_websocket_connection", fake_open)

    response = await model.get_response(
        system_instructions=None,
        input="hi",
        model_settings=ModelSettings(extra_args={"timeout": 0}),
        tools=[],
        output_schema=None,
        handoffs=[],
        tracing=ModelTracing.DISABLED,
    )

    assert response.response_id == "resp-zero-timeout"
    assert len(ws.sent_messages) == 1


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_websocket_model_get_response_uses_client_default_timeout_when_no_override(
    monkeypatch,
):
    client = DummyWSClient()
    client.timeout = httpx.Timeout(connect=1.0, read=0.01, write=1.0, pool=1.0)
    model = OpenAIResponsesWSModel(model="gpt-4", openai_client=client)  # type: ignore[arg-type]

    class SlowRecvWSConnection(DummyWSConnection):
        async def recv(self) -> str:
            await asyncio.sleep(0.2)
            return await super().recv()

    ws = SlowRecvWSConnection([_response_completed_frame("resp-timeout-default", 1)])

    async def fake_open(
        ws_url: str, headers: dict[str, str], *, connect_timeout: float | None = None
    ) -> DummyWSConnection:
        return ws

    monkeypatch.setattr(model, "_open_websocket_connection", fake_open)

    with pytest.raises(TimeoutError, match="Responses websocket receive timed out"):
        await model.get_response(
            system_instructions=None,
            input="hi",
            model_settings=ModelSettings(),
            tools=[],
            output_schema=None,
            handoffs=[],
            tracing=ModelTracing.DISABLED,
        )

    assert ws.close_calls == 1


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_websocket_model_get_response_uses_client_default_timeout_when_override_is_not_given(
    monkeypatch,
):
    client = DummyWSClient()
    client.timeout = httpx.Timeout(connect=1.0, read=0.01, write=1.0, pool=1.0)
    model = OpenAIResponsesWSModel(model="gpt-4", openai_client=client)  # type: ignore[arg-type]

    class SlowRecvWSConnection(DummyWSConnection):
        async def recv(self) -> str:
            await asyncio.sleep(0.2)
            return await super().recv()

    ws = SlowRecvWSConnection([_response_completed_frame("resp-timeout-not-given", 1)])

    async def fake_open(
        ws_url: str, headers: dict[str, str], *, connect_timeout: float | None = None
    ) -> DummyWSConnection:
        return ws

    monkeypatch.setattr(model, "_open_websocket_connection", fake_open)

    with pytest.raises(TimeoutError, match="Responses websocket receive timed out"):
        await model.get_response(
            system_instructions=None,
            input="hi",
            model_settings=ModelSettings(extra_args={"timeout": NOT_GIVEN}),
            tools=[],
            output_schema=None,
            handoffs=[],
            tracing=ModelTracing.DISABLED,
        )

    assert ws.close_calls == 1


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_websocket_model_prepare_websocket_request_omit_removes_inherited_header():
    client = DummyWSClient()
    model = OpenAIResponsesWSModel(model="gpt-4", openai_client=client)  # type: ignore[arg-type]

    _frame, _ws_url, headers = await model._prepare_websocket_request(
        {
            "model": "gpt-4",
            "input": "hi",
            "stream": True,
            "extra_headers": {"User-Agent": omit},
        }
    )

    assert "Authorization" in headers
    assert "User-Agent" not in headers


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_websocket_model_prepare_websocket_request_replaces_header_case_insensitively():
    client = DummyWSClient()
    model = OpenAIResponsesWSModel(model="gpt-4", openai_client=client)  # type: ignore[arg-type]

    _frame, _ws_url, headers = await model._prepare_websocket_request(
        {
            "model": "gpt-4",
            "input": "hi",
            "stream": True,
            "extra_headers": {
                "authorization": "Bearer override-key",
                "user-agent": "Custom UA",
            },
        }
    )

    assert headers["authorization"] == "Bearer override-key"
    assert headers["user-agent"] == "Custom UA"
    assert "Authorization" not in headers
    assert "User-Agent" not in headers


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_websocket_model_prepare_websocket_request_skips_not_given_header_values():
    client = DummyWSClient()
    model = OpenAIResponsesWSModel(model="gpt-4", openai_client=client)  # type: ignore[arg-type]

    _frame, _ws_url, headers = await model._prepare_websocket_request(
        {
            "model": "gpt-4",
            "input": "hi",
            "stream": True,
            "extra_headers": {
                "Authorization": NOT_GIVEN,
                "X-Optional": NOT_GIVEN,
            },
        }
    )

    assert headers["Authorization"] == "Bearer test-key"
    assert "X-Optional" not in headers
    assert "NOT_GIVEN" not in headers.values()


@pytest.mark.allow_call_model_methods
def test_websocket_model_prepare_websocket_url_includes_client_default_query():
    client = DummyWSClient()
    client.websocket_base_url = "wss://proxy.example.test/v1?token=abc"
    client.default_query = {"api-version": "2025-01-01-preview", "omit_me": omit}
    model = OpenAIResponsesWSModel(model="gpt-4", openai_client=client)  # type: ignore[arg-type]

    ws_url = model._prepare_websocket_url(
        extra_query={"route": "team-a", "api-version": "2026-01-01-preview"}
    )
    parsed = httpx.URL(ws_url)

    assert parsed.path == "/v1/responses"
    assert dict(parsed.params) == {
        "token": "abc",
        "api-version": "2026-01-01-preview",
        "route": "team-a",
    }


@pytest.mark.allow_call_model_methods
def test_websocket_model_prepare_websocket_url_omit_removes_inherited_query_params():
    client = DummyWSClient()
    client.websocket_base_url = "wss://proxy.example.test/v1?token=abc"
    client.default_query = {"route": "team-a", "region": "us"}
    model = OpenAIResponsesWSModel(model="gpt-4", openai_client=client)  # type: ignore[arg-type]

    ws_url = model._prepare_websocket_url(extra_query={"token": omit, "route": omit, "keep": "1"})
    parsed = httpx.URL(ws_url)

    assert parsed.path == "/v1/responses"
    assert dict(parsed.params) == {"region": "us", "keep": "1"}


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_websocket_model_close_closes_persistent_connection(monkeypatch):
    client = DummyWSClient()
    ws = DummyWSConnection([_response_completed_frame("resp-close", 1)])
    model = OpenAIResponsesWSModel(model="gpt-4", openai_client=client)  # type: ignore[arg-type]

    async def fake_open(
        ws_url: str, headers: dict[str, str], *, connect_timeout: float | None = None
    ) -> DummyWSConnection:
        return ws

    monkeypatch.setattr(model, "_open_websocket_connection", fake_open)

    await model.get_response(
        system_instructions=None,
        input="hi",
        model_settings=ModelSettings(),
        tools=[],
        output_schema=None,
        handoffs=[],
        tracing=ModelTracing.DISABLED,
    )

    assert ws.close_calls == 0
    await model.close()
    assert ws.close_calls == 1


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_websocket_model_close_falls_back_to_transport_abort_on_close_error():
    client = DummyWSClient()
    model = OpenAIResponsesWSModel(model="gpt-4", openai_client=client)  # type: ignore[arg-type]

    class DummyTransport:
        def __init__(self):
            self.abort_calls = 0

        def abort(self):
            self.abort_calls += 1

    class FailingWSConnection:
        def __init__(self):
            self.transport = DummyTransport()

        async def close(self):
            raise RuntimeError("attached to a different loop")

    ws = FailingWSConnection()
    model._ws_connection = ws
    model._ws_connection_identity = ("wss://example.test", (("authorization", "x"),))

    await model.close()

    assert ws.transport.abort_calls == 1
    assert model._ws_connection is None
    assert model._ws_connection_identity is None


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_websocket_model_close_does_not_wait_for_held_request_lock():
    client = DummyWSClient()
    model = OpenAIResponsesWSModel(model="gpt-4", openai_client=client)  # type: ignore[arg-type]
    request_lock = model._get_ws_request_lock()
    await request_lock.acquire()

    class DummyTransport:
        def __init__(self):
            self.abort_calls = 0

        def abort(self):
            self.abort_calls += 1

    class HangingCloseWSConnection:
        def __init__(self):
            self.transport = DummyTransport()
            self.close_calls = 0

        async def close(self) -> None:
            self.close_calls += 1
            await asyncio.sleep(3600)

    ws = HangingCloseWSConnection()
    model._ws_connection = ws
    model._ws_connection_identity = ("wss://example.test", (("authorization", "x"),))

    try:
        await asyncio.wait_for(model.close(), timeout=0.1)
    finally:
        if request_lock.locked():
            request_lock.release()

    assert ws.transport.abort_calls == 1
    assert ws.close_calls == 0
    assert model._ws_connection is None
    assert model._ws_connection_identity is None


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_websocket_model_open_websocket_connection_disables_message_size_limit(monkeypatch):
    client = DummyWSClient()
    model = OpenAIResponsesWSModel(model="gpt-4", openai_client=client)  # type: ignore[arg-type]
    captured: dict[str, Any] = {}
    sentinel = object()

    async def fake_connect(*args: Any, **kwargs: Any) -> object:
        captured["args"] = args
        captured["kwargs"] = kwargs
        return sentinel

    monkeypatch.setattr("websockets.asyncio.client.connect", fake_connect)

    result = await model._open_websocket_connection(
        "wss://proxy.example.test/v1/responses",
        {"Authorization": "Bearer test-key"},
        connect_timeout=None,
    )

    assert result is sentinel
    assert captured["args"] == ("wss://proxy.example.test/v1/responses",)
    assert captured["kwargs"]["user_agent_header"] is None
    assert captured["kwargs"]["additional_headers"] == {"Authorization": "Bearer test-key"}
    assert captured["kwargs"]["max_size"] is None
    assert captured["kwargs"]["open_timeout"] is None


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_websocket_model_open_websocket_connection_honors_connect_timeout(monkeypatch):
    client = DummyWSClient()
    model = OpenAIResponsesWSModel(model="gpt-4", openai_client=client)  # type: ignore[arg-type]
    captured: dict[str, Any] = {}
    sentinel = object()

    async def fake_connect(*args: Any, **kwargs: Any) -> object:
        captured["args"] = args
        captured["kwargs"] = kwargs
        return sentinel

    monkeypatch.setattr("websockets.asyncio.client.connect", fake_connect)

    result = await model._open_websocket_connection(
        "wss://proxy.example.test/v1/responses",
        {"Authorization": "Bearer test-key"},
        connect_timeout=42.0,
    )

    assert result is sentinel
    assert captured["kwargs"]["open_timeout"] == 42.0


@pytest.mark.allow_call_model_methods
def test_get_retry_advice_uses_openai_headers() -> None:
    request = httpx.Request("POST", "https://api.openai.com/v1/responses")
    response = httpx.Response(
        429,
        request=request,
        headers={
            "x-should-retry": "true",
            "retry-after-ms": "250",
            "x-request-id": "req_456",
        },
        json={"error": {"code": "rate_limit"}},
    )
    error = RateLimitError(
        "rate limited", response=response, body={"error": {"code": "rate_limit"}}
    )
    model = OpenAIResponsesModel(model="gpt-4", openai_client=cast(Any, object()))

    advice = model.get_retry_advice(
        ModelRetryAdviceRequest(
            error=error,
            attempt=1,
            stream=False,
        )
    )

    assert advice is not None
    assert advice.suggested is True
    assert advice.retry_after == 0.25
    assert advice.replay_safety == "safe"
    assert advice.normalized is not None
    assert advice.normalized.error_code == "rate_limit"
    assert advice.normalized.status_code == 429
    assert advice.normalized.request_id == "req_456"


@pytest.mark.allow_call_model_methods
def test_get_retry_advice_keeps_stateful_transport_failures_ambiguous() -> None:
    model = OpenAIResponsesModel(model="gpt-4", openai_client=cast(Any, object()))
    error = APIConnectionError(
        message="connection error",
        request=httpx.Request("POST", "https://api.openai.com/v1/responses"),
    )

    advice = model.get_retry_advice(
        ModelRetryAdviceRequest(
            error=error,
            attempt=1,
            stream=False,
            previous_response_id="resp_prev",
        )
    )

    assert advice is not None
    assert advice.suggested is True
    assert advice.replay_safety is None
    assert advice.normalized is not None
    assert advice.normalized.is_network_error is True


@pytest.mark.allow_call_model_methods
def test_get_retry_advice_marks_stateful_http_failures_replay_safe() -> None:
    request = httpx.Request("POST", "https://api.openai.com/v1/responses")
    response = httpx.Response(
        429,
        request=request,
        json={"error": {"code": "rate_limit"}},
    )
    error = RateLimitError(
        "rate limited", response=response, body={"error": {"code": "rate_limit"}}
    )
    model = OpenAIResponsesModel(model="gpt-4", openai_client=cast(Any, object()))

    advice = model.get_retry_advice(
        ModelRetryAdviceRequest(
            error=error,
            attempt=1,
            stream=False,
            previous_response_id="resp_prev",
        )
    )

    assert advice is not None
    assert advice.suggested is True
    assert advice.replay_safety == "safe"
    assert advice.normalized is not None
    assert advice.normalized.status_code == 429


@pytest.mark.allow_call_model_methods
def test_get_retry_advice_keeps_stateless_transport_failures_retryable() -> None:
    model = OpenAIResponsesModel(model="gpt-4", openai_client=cast(Any, object()))
    error = APIConnectionError(
        message="connection error",
        request=httpx.Request("POST", "https://api.openai.com/v1/responses"),
    )

    advice = model.get_retry_advice(
        ModelRetryAdviceRequest(
            error=error,
            attempt=1,
            stream=False,
        )
    )

    assert advice is not None
    assert advice.suggested is True
    assert advice.replay_safety is None
    assert advice.normalized is not None
    assert advice.normalized.is_network_error is True


@pytest.mark.allow_call_model_methods
def test_websocket_get_retry_advice_marks_ambiguous_replay_unsafe() -> None:
    model = OpenAIResponsesWSModel(model="gpt-4", openai_client=cast(Any, DummyWSClient()))
    error = RuntimeError("Responses websocket connection closed before a terminal response event.")
    error.__cause__ = _connection_closed_error("peer closed after request send")

    advice = model.get_retry_advice(
        ModelRetryAdviceRequest(
            error=error,
            attempt=1,
            stream=True,
            previous_response_id="resp_prev",
        )
    )

    assert advice is not None
    assert advice.suggested is False
    assert advice.replay_safety == "unsafe"


@pytest.mark.allow_call_model_methods
def test_websocket_get_retry_advice_allows_stateless_ambiguous_disconnect_retry() -> None:
    model = OpenAIResponsesWSModel(model="gpt-4", openai_client=cast(Any, DummyWSClient()))
    error = RuntimeError("Responses websocket connection closed before a terminal response event.")
    error.__cause__ = _connection_closed_error("peer closed after request send")

    advice = model.get_retry_advice(
        ModelRetryAdviceRequest(
            error=error,
            attempt=1,
            stream=True,
        )
    )

    assert advice is not None
    assert advice.suggested is True
    assert advice.replay_safety is None


@pytest.mark.allow_call_model_methods
def test_websocket_get_retry_advice_keeps_wrapped_pre_send_disconnect_safe() -> None:
    model = OpenAIResponsesWSModel(model="gpt-4", openai_client=cast(Any, DummyWSClient()))
    error = RuntimeError(
        "Responses websocket connection closed before any response events were received."
    )
    setattr(error, "_openai_agents_ws_replay_safety", "safe")  # noqa: B010
    error.__cause__ = _connection_closed_error("peer closed before request send")

    advice = model.get_retry_advice(
        ModelRetryAdviceRequest(
            error=error,
            attempt=1,
            stream=True,
            previous_response_id="resp_prev",
        )
    )

    assert advice is not None
    assert advice.suggested is True
    assert advice.replay_safety == "safe"


@pytest.mark.allow_call_model_methods
def test_websocket_get_retry_advice_allows_stateless_wrapped_post_send_disconnect_retry() -> None:
    model = OpenAIResponsesWSModel(model="gpt-4", openai_client=cast(Any, DummyWSClient()))
    error = RuntimeError(
        "Responses websocket connection closed before any response events were received."
    )
    setattr(error, "_openai_agents_ws_replay_safety", "unsafe")  # noqa: B010
    error.__cause__ = _connection_closed_error("peer closed after request send")

    advice = model.get_retry_advice(
        ModelRetryAdviceRequest(
            error=error,
            attempt=1,
            stream=True,
        )
    )

    assert advice is not None
    assert advice.suggested is True
    assert advice.replay_safety is None


@pytest.mark.allow_call_model_methods
def test_websocket_get_retry_advice_allows_stateless_nonstream_post_send_retry() -> None:
    model = OpenAIResponsesWSModel(model="gpt-4", openai_client=cast(Any, DummyWSClient()))
    error = RuntimeError(
        "Responses websocket connection closed before any response events were received."
    )
    setattr(error, "_openai_agents_ws_replay_safety", "unsafe")  # noqa: B010
    error.__cause__ = _connection_closed_error("peer closed after request send")

    advice = model.get_retry_advice(
        ModelRetryAdviceRequest(
            error=error,
            attempt=1,
            stream=False,
        )
    )

    assert advice is not None
    assert advice.suggested is True
    assert advice.replay_safety is None


@pytest.mark.allow_call_model_methods
def test_websocket_get_retry_advice_marks_wrapped_post_send_disconnect_unsafe() -> None:
    model = OpenAIResponsesWSModel(model="gpt-4", openai_client=cast(Any, DummyWSClient()))
    error = RuntimeError(
        "Responses websocket connection closed before any response events were received."
    )
    setattr(error, "_openai_agents_ws_replay_safety", "unsafe")  # noqa: B010
    error.__cause__ = _connection_closed_error("peer closed after request send")

    advice = model.get_retry_advice(
        ModelRetryAdviceRequest(
            error=error,
            attempt=1,
            stream=True,
            previous_response_id="resp_prev",
        )
    )

    assert advice is not None
    assert advice.suggested is False
    assert advice.replay_safety == "unsafe"


@pytest.mark.allow_call_model_methods
def test_websocket_get_retry_advice_marks_partial_nonstream_failure_unsafe() -> None:
    model = OpenAIResponsesWSModel(model="gpt-4", openai_client=cast(Any, DummyWSClient()))
    error = TimeoutError("Responses websocket receive timed out after 5.0 seconds.")
    setattr(error, "_openai_agents_ws_replay_safety", "unsafe")  # noqa: B010
    setattr(error, "_openai_agents_ws_response_started", True)  # noqa: B010

    advice = model.get_retry_advice(
        ModelRetryAdviceRequest(
            error=error,
            attempt=1,
            stream=False,
        )
    )

    assert advice is not None
    assert advice.suggested is False
    assert advice.replay_safety == "unsafe"


@pytest.mark.allow_call_model_methods
def test_websocket_get_retry_advice_marks_connect_timeout_replay_safe() -> None:
    model = OpenAIResponsesWSModel(model="gpt-4", openai_client=cast(Any, DummyWSClient()))
    error = TimeoutError("Responses websocket connect timed out after 5.0 seconds.")

    advice = model.get_retry_advice(
        ModelRetryAdviceRequest(
            error=error,
            attempt=1,
            stream=True,
            previous_response_id="resp_prev",
        )
    )

    assert advice is not None
    assert advice.suggested is True
    assert advice.replay_safety == "safe"


@pytest.mark.allow_call_model_methods
def test_websocket_get_retry_advice_marks_request_lock_timeout_replay_safe() -> None:
    model = OpenAIResponsesWSModel(model="gpt-4", openai_client=cast(Any, DummyWSClient()))
    error = TimeoutError("Responses websocket request lock wait timed out after 5.0 seconds.")

    advice = model.get_retry_advice(
        ModelRetryAdviceRequest(
            error=error,
            attempt=1,
            stream=False,
            previous_response_id="resp_prev",
        )
    )

    assert advice is not None
    assert advice.suggested is True
    assert advice.replay_safety == "safe"


@pytest.mark.allow_call_model_methods
def test_websocket_get_retry_advice_marks_stateful_receive_timeout_unsafe() -> None:
    model = OpenAIResponsesWSModel(model="gpt-4", openai_client=cast(Any, DummyWSClient()))
    error = TimeoutError("Responses websocket receive timed out after 5.0 seconds.")

    advice = model.get_retry_advice(
        ModelRetryAdviceRequest(
            error=error,
            attempt=1,
            stream=True,
            previous_response_id="resp_prev",
        )
    )

    assert advice is not None
    assert advice.suggested is False
    assert advice.replay_safety == "unsafe"


@pytest.mark.allow_call_model_methods
def test_websocket_get_retry_advice_allows_stateless_receive_timeout_retry() -> None:
    model = OpenAIResponsesWSModel(model="gpt-4", openai_client=cast(Any, DummyWSClient()))
    error = TimeoutError("Responses websocket receive timed out after 5.0 seconds.")

    advice = model.get_retry_advice(
        ModelRetryAdviceRequest(
            error=error,
            attempt=1,
            stream=True,
        )
    )

    assert advice is not None
    assert advice.suggested is True
    assert advice.replay_safety is None


def test_get_client_disables_provider_managed_retries_when_requested() -> None:
    class DummyClient:
        def __init__(self):
            self.calls: list[dict[str, int]] = []

        def with_options(self, **kwargs):
            self.calls.append(kwargs)
            return "retry-client"

    client = DummyClient()
    model = OpenAIResponsesModel(model="gpt-4", openai_client=cast(Any, client))

    assert cast(object, model._get_client()) is client

    with provider_managed_retries_disabled(True):
        assert cast(object, model._get_client()) == "retry-client"

    assert client.calls == [{"max_retries": 0}]


def test_websocket_pre_event_disconnect_retry_respects_websocket_retry_disable() -> None:
    assert _should_retry_pre_event_websocket_disconnect() is True

    with websocket_pre_event_retries_disabled(True):
        assert _should_retry_pre_event_websocket_disconnect() is False
