import os
import time
from typing import Any, cast
from unittest.mock import MagicMock, patch

import httpx
import pytest

from agents.tracing.processor_interface import TracingProcessor
from agents.tracing.processors import BackendSpanExporter, BatchTraceProcessor
from agents.tracing.span_data import AgentSpanData
from agents.tracing.spans import SpanImpl
from agents.tracing.traces import TraceImpl


def get_span(processor: TracingProcessor) -> SpanImpl[AgentSpanData]:
    """Create a minimal agent span for testing processors."""
    return SpanImpl(
        trace_id="test_trace_id",
        span_id="test_span_id",
        parent_id=None,
        processor=processor,
        span_data=AgentSpanData(name="test_agent"),
        tracing_api_key=None,
    )


def get_trace(processor: TracingProcessor) -> TraceImpl:
    """Create a minimal trace."""
    return TraceImpl(
        name="test_trace",
        trace_id="test_trace_id",
        group_id="test_session_id",
        metadata={},
        processor=processor,
        tracing_api_key=None,
    )


@pytest.fixture
def mocked_exporter():
    exporter = MagicMock()
    exporter.export = MagicMock()
    return exporter


def test_batch_trace_processor_on_trace_start(mocked_exporter):
    processor = BatchTraceProcessor(exporter=mocked_exporter, schedule_delay=0.1)
    test_trace = get_trace(processor)

    processor.on_trace_start(test_trace)
    assert processor._queue.qsize() == 1, "Trace should be added to the queue"

    # Shutdown to clean up the worker thread
    processor.shutdown()


def test_batch_trace_processor_on_span_end(mocked_exporter):
    processor = BatchTraceProcessor(exporter=mocked_exporter, schedule_delay=0.1)
    test_span = get_span(processor)

    processor.on_span_end(test_span)
    assert processor._queue.qsize() == 1, "Span should be added to the queue"

    # Shutdown to clean up the worker thread
    processor.shutdown()


def test_batch_trace_processor_queue_full(mocked_exporter):
    processor = BatchTraceProcessor(exporter=mocked_exporter, max_queue_size=2, schedule_delay=0.1)
    # Fill the queue
    processor.on_trace_start(get_trace(processor))
    processor.on_trace_start(get_trace(processor))
    assert processor._queue.full() is True

    # Next item should not be queued
    processor.on_trace_start(get_trace(processor))
    assert processor._queue.qsize() == 2, "Queue should not exceed max_queue_size"

    processor.on_span_end(get_span(processor))
    assert processor._queue.qsize() == 2, "Queue should not exceed max_queue_size"

    processor.shutdown()


def test_batch_processor_doesnt_enqueue_on_trace_end_or_span_start(mocked_exporter):
    processor = BatchTraceProcessor(exporter=mocked_exporter)

    processor.on_trace_start(get_trace(processor))
    assert processor._queue.qsize() == 1, "Trace should be queued"

    processor.on_span_start(get_span(processor))
    assert processor._queue.qsize() == 1, "Span should not be queued"

    processor.on_span_end(get_span(processor))
    assert processor._queue.qsize() == 2, "Span should be queued"

    processor.on_trace_end(get_trace(processor))
    assert processor._queue.qsize() == 2, "Nothing new should be queued"

    processor.shutdown()


def test_batch_trace_processor_force_flush(mocked_exporter):
    processor = BatchTraceProcessor(exporter=mocked_exporter, max_batch_size=2, schedule_delay=5.0)

    processor.on_trace_start(get_trace(processor))
    processor.on_span_end(get_span(processor))
    processor.on_span_end(get_span(processor))

    processor.force_flush()

    # Ensure exporter.export was called with all items
    # Because max_batch_size=2, it may have been called multiple times
    total_exported = 0
    for call_args in mocked_exporter.export.call_args_list:
        batch = call_args[0][0]  # first positional arg to export() is the items list
        total_exported += len(batch)

    # We pushed 3 items; ensure they all got exported
    assert total_exported == 3

    processor.shutdown()


def test_batch_trace_processor_shutdown_flushes(mocked_exporter):
    processor = BatchTraceProcessor(exporter=mocked_exporter, schedule_delay=5.0)
    processor.on_trace_start(get_trace(processor))
    processor.on_span_end(get_span(processor))
    qsize_before = processor._queue.qsize()
    assert qsize_before == 2

    processor.shutdown()

    # Ensure everything was exported after shutdown
    total_exported = 0
    for call_args in mocked_exporter.export.call_args_list:
        batch = call_args[0][0]
        total_exported += len(batch)

    assert total_exported == 2, "All items in the queue should be exported upon shutdown"


def test_batch_trace_processor_scheduled_export(mocked_exporter):
    """
    Tests that items are automatically exported when the schedule_delay expires.
    We mock time.time() so we can trigger the condition without waiting in real time.
    """
    with patch("time.time") as mock_time:
        base_time = 1000.0
        mock_time.return_value = base_time

        processor = BatchTraceProcessor(exporter=mocked_exporter, schedule_delay=1.0)

        processor.on_span_end(get_span(processor))  # queue size = 1

        # Now artificially advance time beyond the next export time
        mock_time.return_value = base_time + 2.0  # > base_time + schedule_delay
        # Let the background thread run a bit
        time.sleep(0.3)

        # Check that exporter.export was eventually called
        # Because the background thread runs, we might need a small sleep
        processor.shutdown()

    total_exported = 0
    for call_args in mocked_exporter.export.call_args_list:
        batch = call_args[0][0]
        total_exported += len(batch)

    assert total_exported == 1, "Item should be exported after scheduled delay"


@pytest.fixture
def patched_time_sleep():
    """
    Fixture to replace time.sleep with a no-op to speed up tests
    that rely on retry/backoff logic.
    """
    with patch("time.sleep") as mock_sleep:
        yield mock_sleep


def mock_processor():
    processor = MagicMock()
    processor.on_trace_start = MagicMock()
    processor.on_span_end = MagicMock()
    return processor


@patch("httpx.Client")
def test_backend_span_exporter_no_items(mock_client):
    exporter = BackendSpanExporter(api_key="test_key")
    exporter.export([])
    # No calls should be made if there are no items
    mock_client.return_value.post.assert_not_called()
    exporter.close()


@patch("httpx.Client")
def test_backend_span_exporter_no_api_key(mock_client):
    # Ensure that os.environ is empty (sometimes devs have the openai api key set in their env)

    with patch.dict(os.environ, {}, clear=True):
        exporter = BackendSpanExporter(api_key=None)
        exporter.export([get_span(mock_processor())])

        # Should log an error and return without calling post
        mock_client.return_value.post.assert_not_called()
        exporter.close()


@patch("httpx.Client")
def test_backend_span_exporter_2xx_success(mock_client):
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_client.return_value.post.return_value = mock_response

    exporter = BackendSpanExporter(api_key="test_key")
    exporter.export([get_span(mock_processor()), get_trace(mock_processor())])

    # Should have called post exactly once
    mock_client.return_value.post.assert_called_once()
    exporter.close()


@patch("httpx.Client")
def test_backend_span_exporter_4xx_client_error(mock_client):
    mock_response = MagicMock()
    mock_response.status_code = 400
    mock_response.text = "Bad Request"
    mock_client.return_value.post.return_value = mock_response

    exporter = BackendSpanExporter(api_key="test_key")
    exporter.export([get_span(mock_processor())])

    # 4xx should not be retried
    mock_client.return_value.post.assert_called_once()
    exporter.close()


@patch("httpx.Client")
def test_backend_span_exporter_5xx_retry(mock_client, patched_time_sleep):
    mock_response = MagicMock()
    mock_response.status_code = 500

    # Make post() return 500 every time
    mock_client.return_value.post.return_value = mock_response

    exporter = BackendSpanExporter(api_key="test_key", max_retries=3, base_delay=0.1, max_delay=0.2)
    exporter.export([get_span(mock_processor())])

    # Should retry up to max_retries times
    assert mock_client.return_value.post.call_count == 3

    exporter.close()


@patch("httpx.Client")
def test_backend_span_exporter_request_error(mock_client, patched_time_sleep):
    # Make post() raise a RequestError each time
    mock_client.return_value.post.side_effect = httpx.RequestError("Network error")

    exporter = BackendSpanExporter(api_key="test_key", max_retries=2, base_delay=0.1, max_delay=0.2)
    exporter.export([get_span(mock_processor())])

    # Should retry up to max_retries times
    assert mock_client.return_value.post.call_count == 2

    exporter.close()


@patch("httpx.Client")
def test_backend_span_exporter_close(mock_client):
    exporter = BackendSpanExporter(api_key="test_key")
    exporter.close()

    # Ensure underlying http client is closed
    mock_client.return_value.close.assert_called_once()


@patch("httpx.Client")
def test_backend_span_exporter_sanitizes_generation_usage_for_openai_tracing(mock_client):
    """Unsupported usage keys should be stripped before POSTing to OpenAI tracing."""

    class DummyItem:
        tracing_api_key = None

        def __init__(self):
            self.exported_payload: dict[str, Any] = {
                "object": "trace.span",
                "span_data": {
                    "type": "generation",
                    "usage": {
                        "requests": 1,
                        "input_tokens": 10,
                        "output_tokens": 5,
                        "total_tokens": 15,
                        "input_tokens_details": {"cached_tokens": 1},
                        "output_tokens_details": {"reasoning_tokens": 2},
                    },
                },
            }

        def export(self):
            return self.exported_payload

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_client.return_value.post.return_value = mock_response

    exporter = BackendSpanExporter(api_key="test_key")
    item = DummyItem()
    exporter.export([cast(Any, item)])

    sent_payload = mock_client.return_value.post.call_args.kwargs["json"]["data"][0]
    sent_usage = sent_payload["span_data"]["usage"]
    assert "requests" not in sent_usage
    assert sent_usage["input_tokens"] == 10
    assert sent_usage["output_tokens"] == 5
    assert sent_usage["total_tokens"] == 15
    assert sent_usage["input_tokens_details"] == {"cached_tokens": 1}
    assert sent_usage["output_tokens_details"] == {"reasoning_tokens": 2}

    # Ensure the original exported object has not been mutated.
    assert "requests" in item.exported_payload["span_data"]["usage"]
    exporter.close()


@patch("httpx.Client")
def test_backend_span_exporter_keeps_generation_usage_for_custom_endpoint(mock_client):
    class DummyItem:
        tracing_api_key = None

        def __init__(self):
            self.exported_payload = {
                "object": "trace.span",
                "span_data": {
                    "type": "generation",
                    "usage": {
                        "requests": 1,
                        "input_tokens": 10,
                        "output_tokens": 5,
                    },
                },
            }

        def export(self):
            return self.exported_payload

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_client.return_value.post.return_value = mock_response

    exporter = BackendSpanExporter(
        api_key="test_key",
        endpoint="https://example.com/v1/traces/ingest",
    )
    exporter.export([cast(Any, DummyItem())])

    sent_payload = mock_client.return_value.post.call_args.kwargs["json"]["data"][0]
    assert sent_payload["span_data"]["usage"]["requests"] == 1
    assert sent_payload["span_data"]["usage"]["input_tokens"] == 10
    assert sent_payload["span_data"]["usage"]["output_tokens"] == 5
    exporter.close()


@patch("httpx.Client")
def test_backend_span_exporter_does_not_modify_non_generation_usage(mock_client):
    class DummyItem:
        tracing_api_key = None

        def export(self):
            return {
                "object": "trace.span",
                "span_data": {
                    "type": "function",
                    "usage": {"requests": 1},
                },
            }

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_client.return_value.post.return_value = mock_response

    exporter = BackendSpanExporter(api_key="test_key")
    exporter.export([cast(Any, DummyItem())])

    sent_payload = mock_client.return_value.post.call_args.kwargs["json"]["data"][0]
    assert sent_payload["span_data"]["usage"] == {"requests": 1}
    exporter.close()


def test_sanitize_for_openai_tracing_api_keeps_allowed_generation_usage():
    exporter = BackendSpanExporter(api_key="test_key")
    payload = {
        "object": "trace.span",
        "span_data": {
            "type": "generation",
            "usage": {
                "input_tokens": 1,
                "output_tokens": 2,
                "total_tokens": 3,
                "input_tokens_details": {"cached_tokens": 0},
                "output_tokens_details": {"reasoning_tokens": 0},
            },
        },
    }
    assert exporter._sanitize_for_openai_tracing_api(payload) is payload
    exporter.close()


def test_sanitize_for_openai_tracing_api_skips_non_dict_generation_usage():
    exporter = BackendSpanExporter(api_key="test_key")
    payload = {
        "object": "trace.span",
        "span_data": {
            "type": "generation",
            "usage": None,
        },
    }
    assert exporter._sanitize_for_openai_tracing_api(payload) is payload
    exporter.close()
