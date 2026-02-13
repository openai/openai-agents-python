from typing import Optional

import pytest
from inline_snapshot import snapshot
from openai import AsyncOpenAI
from openai.types.responses import ResponseCompletedEvent
from openai.types.responses.response_usage import InputTokensDetails, OutputTokensDetails

from agents import ModelSettings, ModelTracing, OpenAIResponsesModel, trace
from agents.tracing.span_data import ResponseSpanData
from tests import fake_model

from .testing_processor import assert_no_spans, fetch_normalized_spans, fetch_ordered_spans


class DummyTracing:
    def is_disabled(self):
        return False


class DummyUsage:
    def __init__(
        self,
        input_tokens: int = 1,
        input_tokens_details: Optional[InputTokensDetails] = None,
        output_tokens: int = 1,
        output_tokens_details: Optional[OutputTokensDetails] = None,
        total_tokens: int = 2,
    ):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.total_tokens = total_tokens
        self.input_tokens_details = (
            input_tokens_details if input_tokens_details else InputTokensDetails(cached_tokens=0)
        )
        self.output_tokens_details = (
            output_tokens_details
            if output_tokens_details
            else OutputTokensDetails(reasoning_tokens=0)
        )


class DummyResponse:
    def __init__(self):
        self.id = "dummy-id"
        self.output = []
        self.usage = DummyUsage()

    def __aiter__(self):
        yield ResponseCompletedEvent(
            type="response.completed",
            response=fake_model.get_response_obj(self.output),
            sequence_number=0,
        )


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_get_response_creates_trace(monkeypatch):
    with trace(workflow_name="test"):
        # Create an instance of the model
        model = OpenAIResponsesModel(model="test-model", openai_client=AsyncOpenAI(api_key="test"))

        # Mock _fetch_response to return a dummy response with a known id
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
            return DummyResponse()

        monkeypatch.setattr(model, "_fetch_response", dummy_fetch_response)

        # Call get_response
        await model.get_response(
            "instr",
            "input",
            ModelSettings(),
            [],
            None,
            [],
            ModelTracing.ENABLED,
            previous_response_id=None,
        )

    assert fetch_normalized_spans() == snapshot(
        [
            {
                "workflow_name": "test",
                "children": [{"type": "response", "data": {"response_id": "dummy-id"}}],
            }
        ]
    )


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_non_data_tracing_doesnt_set_response_id(monkeypatch):
    with trace(workflow_name="test"):
        # Create an instance of the model
        model = OpenAIResponsesModel(model="test-model", openai_client=AsyncOpenAI(api_key="test"))

        # Mock _fetch_response to return a dummy response with a known id
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
            return DummyResponse()

        monkeypatch.setattr(model, "_fetch_response", dummy_fetch_response)

        # Call get_response
        await model.get_response(
            "instr",
            "input",
            ModelSettings(),
            [],
            None,
            [],
            ModelTracing.ENABLED_WITHOUT_DATA,
            previous_response_id=None,
        )

    assert fetch_normalized_spans() == snapshot(
        [{"workflow_name": "test", "children": [{"type": "response"}]}]
    )

    [span] = fetch_ordered_spans()
    assert span.span_data.response is None


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_disable_tracing_does_not_create_span(monkeypatch):
    with trace(workflow_name="test"):
        # Create an instance of the model
        model = OpenAIResponsesModel(model="test-model", openai_client=AsyncOpenAI(api_key="test"))

        # Mock _fetch_response to return a dummy response with a known id
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
            return DummyResponse()

        monkeypatch.setattr(model, "_fetch_response", dummy_fetch_response)

        # Call get_response
        await model.get_response(
            "instr",
            "input",
            ModelSettings(),
            [],
            None,
            [],
            ModelTracing.DISABLED,
            previous_response_id=None,
        )

    assert fetch_normalized_spans() == snapshot([{"workflow_name": "test"}])

    assert_no_spans()


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_stream_response_creates_trace(monkeypatch):
    with trace(workflow_name="test"):
        # Create an instance of the model
        model = OpenAIResponsesModel(model="test-model", openai_client=AsyncOpenAI(api_key="test"))

        # Define a dummy fetch function that returns an async stream with a dummy response
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
                    yield ResponseCompletedEvent(
                        type="response.completed",
                        response=fake_model.get_response_obj([], "dummy-id-123"),
                        sequence_number=0,
                    )

            return DummyStream()

        monkeypatch.setattr(model, "_fetch_response", dummy_fetch_response)

        # Consume the stream to trigger processing of the final response
        async for _ in model.stream_response(
            "instr",
            "input",
            ModelSettings(),
            [],
            None,
            [],
            ModelTracing.ENABLED,
            previous_response_id=None,
        ):
            pass

    assert fetch_normalized_spans() == snapshot(
        [
            {
                "workflow_name": "test",
                "children": [{"type": "response", "data": {"response_id": "dummy-id-123"}}],
            }
        ]
    )


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_stream_non_data_tracing_doesnt_set_response_id(monkeypatch):
    with trace(workflow_name="test"):
        # Create an instance of the model
        model = OpenAIResponsesModel(model="test-model", openai_client=AsyncOpenAI(api_key="test"))

        # Define a dummy fetch function that returns an async stream with a dummy response
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
                    yield ResponseCompletedEvent(
                        type="response.completed",
                        response=fake_model.get_response_obj([], "dummy-id-123"),
                        sequence_number=0,
                    )

            return DummyStream()

        monkeypatch.setattr(model, "_fetch_response", dummy_fetch_response)

        # Consume the stream to trigger processing of the final response
        async for _ in model.stream_response(
            "instr",
            "input",
            ModelSettings(),
            [],
            None,
            [],
            ModelTracing.ENABLED_WITHOUT_DATA,
            previous_response_id=None,
        ):
            pass

    assert fetch_normalized_spans() == snapshot(
        [{"workflow_name": "test", "children": [{"type": "response"}]}]
    )

    [span] = fetch_ordered_spans()
    assert isinstance(span.span_data, ResponseSpanData)
    assert span.span_data.response is None


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_stream_disabled_tracing_doesnt_create_span(monkeypatch):
    with trace(workflow_name="test"):
        # Create an instance of the model
        model = OpenAIResponsesModel(model="test-model", openai_client=AsyncOpenAI(api_key="test"))

        # Define a dummy fetch function that returns an async stream with a dummy response
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
                    yield ResponseCompletedEvent(
                        type="response.completed",
                        response=fake_model.get_response_obj([], "dummy-id-123"),
                        sequence_number=0,
                    )

            return DummyStream()

        monkeypatch.setattr(model, "_fetch_response", dummy_fetch_response)

        # Consume the stream to trigger processing of the final response
        async for _ in model.stream_response(
            "instr",
            "input",
            ModelSettings(),
            [],
            None,
            [],
            ModelTracing.DISABLED,
            previous_response_id=None,
        ):
            pass

    assert fetch_normalized_spans() == snapshot([{"workflow_name": "test"}])

    assert_no_spans()
