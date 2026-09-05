import logging
import os
import subprocess
import sys
import textwrap
import threading
import time
from typing import Any, cast
from unittest.mock import MagicMock, patch

import httpx2
import pytest

import agents._debug as _debug
from agents.tracing import flush_traces, get_trace_provider
from agents.tracing.processor_interface import TracingExporter, TracingProcessor
from agents.tracing.processors import BackendSpanExporter, BatchTraceProcessor, ConsoleSpanExporter
from agents.tracing.provider import DefaultTraceProvider, TraceProvider
from agents.tracing.span_data import AgentSpanData
from agents.tracing.spans import Span, SpanImpl
from agents.tracing.traces import Trace, TraceImpl


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


@pytest.mark.parametrize("redacted", [True, False])
def test_console_span_exporter_respects_data_policy(monkeypatch, capsys, redacted: bool) -> None:
    monkeypatch.setattr(_debug, "DONT_LOG_MODEL_DATA", redacted)
    monkeypatch.setattr(_debug, "DONT_LOG_TOOL_DATA", redacted)
    span = get_span(mock_processor())
    span.span_data.name = "SECRET_CONSOLE_SPAN"

    ConsoleSpanExporter().export([span])

    output = capsys.readouterr().out
    assert ("SECRET_CONSOLE_SPAN" not in output) is redacted
    assert "Export span" in output


@pytest.fixture
def mocked_exporter():
    exporter = MagicMock()
    exporter.export = MagicMock()
    return exporter


def test_batch_trace_processor_on_trace_start(mocked_exporter):
    processor = BatchTraceProcessor(exporter=mocked_exporter, schedule_delay=0.1)
    test_trace = get_trace(processor)

    try:
        with processor._export_lock:
            processor.on_trace_start(test_trace)
            assert processor._queue.qsize() == 1, "Trace should be added to the queue"
    finally:
        processor.shutdown()


def test_batch_trace_processor_on_span_end(mocked_exporter):
    processor = BatchTraceProcessor(exporter=mocked_exporter, schedule_delay=0.1)
    test_span = get_span(processor)

    try:
        with processor._export_lock:
            processor.on_span_end(test_span)
            assert processor._queue.qsize() == 1, "Span should be added to the queue"
    finally:
        processor.shutdown()


def test_batch_trace_processor_queue_full(mocked_exporter):
    processor = BatchTraceProcessor(exporter=mocked_exporter, max_queue_size=2, schedule_delay=0.1)
    try:
        with processor._export_lock:
            # Fill the queue.
            processor.on_trace_start(get_trace(processor))
            processor.on_trace_start(get_trace(processor))
            assert processor._queue.full() is True

            # Next item should not be queued.
            processor.on_trace_start(get_trace(processor))
            assert processor._queue.qsize() == 2, "Queue should not exceed max_queue_size"

            processor.on_span_end(get_span(processor))
            assert processor._queue.qsize() == 2, "Queue should not exceed max_queue_size"
    finally:
        processor.shutdown()


def test_batch_processor_doesnt_enqueue_on_trace_end_or_span_start(mocked_exporter):
    processor = BatchTraceProcessor(exporter=mocked_exporter)

    try:
        with processor._export_lock:
            processor.on_trace_start(get_trace(processor))
            assert processor._queue.qsize() == 1, "Trace should be queued"

            processor.on_span_start(get_span(processor))
            assert processor._queue.qsize() == 1, "Span should not be queued"

            processor.on_span_end(get_span(processor))
            assert processor._queue.qsize() == 2, "Span should be queued"

            processor.on_trace_end(get_trace(processor))
            assert processor._queue.qsize() == 2, "Nothing new should be queued"
    finally:
        processor.shutdown()


def test_batch_trace_processor_force_flush(mocked_exporter):
    processor = BatchTraceProcessor(exporter=mocked_exporter, max_batch_size=2, schedule_delay=5.0)

    try:
        with processor._export_lock:
            processor.on_trace_start(get_trace(processor))
            processor.on_span_end(get_span(processor))
            processor.on_span_end(get_span(processor))

        processor.force_flush()

        # Ensure exporter.export was called with all items in batches respecting max_batch_size=2.
        exported_batches = [call_args[0][0] for call_args in mocked_exporter.export.call_args_list]
        total_exported = sum(len(batch) for batch in exported_batches)

        # We pushed 3 items; ensure they all got exported across 2 batches (sizes 2 and 1).
        assert total_exported == 3
        assert [len(batch) for batch in exported_batches] == [2, 1]
    finally:
        processor.shutdown()


def test_batch_trace_processor_force_flush_waits_for_in_flight_background_export(monkeypatch):
    export_started = threading.Event()
    export_continue = threading.Event()
    export_completed = threading.Event()
    flush_blocked = threading.Event()
    flush_completed = threading.Event()

    class ObservedExportLock:
        def __init__(self) -> None:
            self.lock = threading.Lock()

        def __enter__(self) -> None:
            if not self.lock.acquire(blocking=False):
                flush_blocked.set()
                self.lock.acquire()

        def __exit__(self, *args: object) -> None:
            self.lock.release()

    class BlockingExporter(TracingExporter):
        def export(self, items: list[Trace | Span[Any]]) -> None:
            export_started.set()
            export_continue.wait()
            export_completed.set()

    processor = BatchTraceProcessor(exporter=BlockingExporter(), schedule_delay=0.01)
    monkeypatch.setattr(processor, "_export_lock", ObservedExportLock())

    def flush() -> None:
        processor.force_flush()
        flush_completed.set()

    flush_thread = threading.Thread(target=flush)
    try:
        processor.on_trace_start(get_trace(processor))
        assert export_started.wait(timeout=2.0)

        flush_thread.start()
        assert flush_blocked.wait(timeout=2.0)
        assert not export_completed.is_set()
        assert not flush_completed.is_set(), "force_flush() should wait for an in-flight export"

        export_continue.set()
        assert flush_completed.wait(timeout=2.0)
        assert export_completed.is_set()
    finally:
        export_continue.set()
        if flush_thread.ident is not None:
            flush_thread.join(timeout=2.0)
        processor.shutdown(timeout=2.0)

    assert not flush_thread.is_alive()
    assert processor._worker_thread is not None
    assert not processor._worker_thread.is_alive()


def test_batch_trace_processor_shutdown_flushes(mocked_exporter):
    processor = BatchTraceProcessor(exporter=mocked_exporter, schedule_delay=5.0)
    try:
        with processor._export_lock:
            processor.on_trace_start(get_trace(processor))
            processor.on_span_end(get_span(processor))
            qsize_before = processor._queue.qsize()
            assert qsize_before == 2
    finally:
        processor.shutdown()

    # Ensure everything was exported after shutdown.
    total_exported = 0
    for call_args in mocked_exporter.export.call_args_list:
        batch = call_args[0][0]
        total_exported += len(batch)

    assert total_exported == 2, "All items in the queue should be exported upon shutdown"


def test_batch_trace_processor_shutdown_timeout_returns_when_exporter_blocks(
    caplog: pytest.LogCaptureFixture,
) -> None:
    export_started = threading.Event()
    release_export = threading.Event()

    class BlockingExporter(TracingExporter):
        def export(self, items: list[Trace | Span[Any]]) -> None:
            export_started.set()
            release_export.wait(timeout=5.0)

    processor = BatchTraceProcessor(
        exporter=BlockingExporter(),
        max_queue_size=1,
        schedule_delay=60.0,
        export_trigger_ratio=1.0,
    )
    try:
        processor.on_span_end(get_span(processor))
        assert export_started.wait(timeout=2.0)

        start = time.monotonic()
        with caplog.at_level(logging.WARNING):
            processor.shutdown(timeout=0.05)
        elapsed = time.monotonic() - start

        assert elapsed < 0.5
        assert "shutdown timeout reached" in caplog.text
    finally:
        release_export.set()
        processor.shutdown(timeout=2.0)

    assert processor._worker_thread is not None
    assert not processor._worker_thread.is_alive()


def test_batch_trace_processor_shutdown_passes_deadline_to_exporter() -> None:
    seen_deadlines: list[float | None] = []

    class DeadlineExporter(TracingExporter):
        def export(self, items: list[Trace | Span[Any]]) -> None:
            raise AssertionError("shutdown should use the deadline-aware exporter path")

        def _export_with_deadline(
            self, items: list[Trace | Span[Any]], deadline: float | None
        ) -> None:
            seen_deadlines.append(deadline)

    processor = BatchTraceProcessor(exporter=DeadlineExporter())
    processor._queue.put_nowait(get_span(processor))

    processor.shutdown(timeout=1.0)

    assert len(seen_deadlines) == 1
    assert seen_deadlines[0] is not None


def test_batch_trace_processor_survives_exporter_exception():
    """A failing exporter must not kill the background worker thread.

    Previously, an exception raised inside ``exporter.export`` propagated out of
    ``_export_batches`` and killed the ``_run`` thread, causing all subsequent
    spans to silently accumulate in the queue until it filled up.
    """

    first_export_started = threading.Event()
    recovery_completed = threading.Event()

    class FlakyExporter(TracingExporter):
        def __init__(self) -> None:
            self.call_count = 0
            self.exported: list[Trace | Span[Any]] = []

        def export(self, items: list[Trace | Span[Any]]) -> None:
            self.call_count += 1
            if self.call_count == 1:
                first_export_started.set()
                raise RuntimeError("simulated exporter failure")
            self.exported.extend(items)
            if len(self.exported) == 2:
                recovery_completed.set()

    exporter = FlakyExporter()
    processor = BatchTraceProcessor(exporter, schedule_delay=0.05, max_batch_size=1)
    try:
        processor.on_span_end(get_span(processor))
        assert first_export_started.wait(timeout=2.0)
        worker = processor._worker_thread

        later_spans = [get_span(processor), get_span(processor)]
        for span in later_spans:
            processor.on_span_end(span)

        assert recovery_completed.wait(timeout=2.0)
        assert worker is not None
        assert processor._worker_thread is worker
        assert worker.is_alive(), "Worker thread must survive an exporter exception"

        # Recovery must happen on the worker before shutdown can drain the queue.
        assert exporter.exported == later_spans
        assert exporter.call_count == 3
    finally:
        processor.shutdown(timeout=2.0)

    assert processor._worker_thread is not None
    assert not processor._worker_thread.is_alive()


@pytest.mark.parametrize(
    ("adjusted_wall_time", "adjusted_monotonic_time", "expected_scheduled_exports"),
    [
        (2000.0, 100.5, 0),
        (0.0, 101.5, 1),
    ],
)
def test_batch_trace_processor_schedule_uses_monotonic_clock(
    mocked_exporter,
    monkeypatch,
    adjusted_wall_time: float,
    adjusted_monotonic_time: float,
    expected_scheduled_exports: int,
) -> None:
    class ControlledTime:
        def __init__(self) -> None:
            self.wall_time = 1000.0
            self.monotonic_time = 100.0
            self.sleep_calls = 0

        def time(self) -> float:
            return self.wall_time

        def monotonic(self) -> float:
            return self.monotonic_time

        def sleep(self, _seconds: float) -> None:
            self.sleep_calls += 1
            if self.sleep_calls == 1:
                self.wall_time = adjusted_wall_time
                self.monotonic_time = adjusted_monotonic_time
            else:
                processor._shutdown_event.set()

    controlled_time = ControlledTime()
    monkeypatch.setattr("agents.tracing.processors.time", controlled_time)
    processor = BatchTraceProcessor(
        exporter=mocked_exporter,
        max_queue_size=100,
        schedule_delay=1.0,
        export_trigger_ratio=1.0,
    )
    processor._queue.put_nowait(get_span(processor))
    scheduled_export = object()
    export_deadlines: list[float | None | object] = []

    def record_export(deadline: float | None | object = scheduled_export) -> None:
        export_deadlines.append(deadline)
        if sum(item is scheduled_export for item in export_deadlines) > 1:
            processor._shutdown_event.set()

    monkeypatch.setattr(processor, "_export_batches", record_export)

    processor._run()

    assert sum(deadline is scheduled_export for deadline in export_deadlines) == (
        expected_scheduled_exports
    )
    assert export_deadlines[-1] is None


def test_flush_traces_delegates_to_default_trace_provider():
    provider = DefaultTraceProvider()
    mock_processor = MagicMock()
    provider.register_processor(mock_processor)

    with patch("agents.tracing.setup.GLOBAL_TRACE_PROVIDER", provider):
        flush_traces()

    mock_processor.force_flush.assert_called_once()


def test_flush_traces_is_importable_from_top_level_agents_package():
    from agents import flush_traces as top_level_flush_traces

    assert top_level_flush_traces is flush_traces


def test_default_trace_provider_force_flush_still_flushes_when_disabled():
    provider = DefaultTraceProvider()
    mock_processor = MagicMock()
    provider.register_processor(mock_processor)

    provider.set_disabled(True)
    provider.force_flush()

    mock_processor.force_flush.assert_called_once_with()


def test_trace_provider_force_flush_and_shutdown_default_to_noops():
    class MinimalProvider(TraceProvider):
        def register_processor(self, processor: TracingProcessor) -> None:
            pass

        def set_processors(self, processors: list[TracingProcessor]) -> None:
            pass

        def get_current_trace(self):
            return None

        def get_current_span(self):
            return None

        def set_disabled(self, disabled: bool) -> None:
            pass

        def time_iso(self) -> str:
            return ""

        def gen_trace_id(self) -> str:
            return "trace_123"

        def gen_span_id(self) -> str:
            return "span_123"

        def gen_group_id(self) -> str:
            return "group_123"

        def create_trace(
            self,
            name,
            trace_id=None,
            group_id=None,
            metadata=None,
            disabled=False,
            tracing=None,
        ):
            raise NotImplementedError

        def create_span(self, span_data, span_id=None, parent=None, disabled=False):
            raise NotImplementedError

    provider = MinimalProvider()
    provider.force_flush()
    provider.shutdown()


def test_get_trace_provider_force_flush_flushes_default_processor(mocked_exporter):
    provider = DefaultTraceProvider()
    processor = BatchTraceProcessor(exporter=mocked_exporter, schedule_delay=60.0)
    provider.register_processor(processor)

    try:
        with patch("agents.tracing.setup.GLOBAL_TRACE_PROVIDER", provider):
            processor.on_trace_start(get_trace(processor))
            processor.on_span_end(get_span(processor))

            get_trace_provider().force_flush()

        total_exported = sum(
            len(call_args[0][0]) for call_args in mocked_exporter.export.call_args_list
        )
        assert total_exported == 2
    finally:
        processor.shutdown()


def mock_processor():
    processor = MagicMock()
    processor.on_trace_start = MagicMock()
    processor.on_span_end = MagicMock()
    return processor


@patch("httpx2.Client")
def test_backend_span_exporter_no_items(mock_client):
    exporter = BackendSpanExporter(api_key="test_key")
    exporter.export([])
    # No calls should be made if there are no items
    mock_client.return_value.post.assert_not_called()
    exporter.close()


@patch("httpx2.Client")
def test_backend_span_exporter_no_api_key(mock_client):
    # Ensure that os.environ is empty (sometimes devs have the openai api key set in their env)

    with patch.dict(os.environ, {}, clear=True):
        exporter = BackendSpanExporter(api_key=None)
        exporter.export([get_span(mock_processor())])

        # Should log an error and return without calling post
        mock_client.return_value.post.assert_not_called()
        exporter.close()


@patch("httpx2.Client")
def test_backend_span_exporter_2xx_success(mock_client):
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_client.return_value.post.return_value = mock_response

    exporter = BackendSpanExporter(api_key="test_key")
    exporter.export([get_span(mock_processor()), get_trace(mock_processor())])

    # Should have called post exactly once
    mock_client.return_value.post.assert_called_once()
    exporter.close()


@patch("httpx2.Client")
@pytest.mark.parametrize("redacted", [True, False])
def test_backend_span_exporter_4xx_client_error(mock_client, monkeypatch, caplog, redacted: bool):
    monkeypatch.setattr(_debug, "DONT_LOG_MODEL_DATA", redacted)
    monkeypatch.setattr(_debug, "DONT_LOG_TOOL_DATA", redacted)

    class Response:
        status_code = 400
        text_reads = 0

        @property
        def text(self) -> str:
            self.text_reads += 1
            return "SECRET_TRACE_RESPONSE_BODY"

    mock_response = Response()
    mock_client.return_value.post.return_value = mock_response

    exporter = BackendSpanExporter(api_key="test_key")
    with caplog.at_level(logging.ERROR, logger="openai.agents"):
        exporter.export([get_span(mock_processor())])

    # 4xx should not be retried
    mock_client.return_value.post.assert_called_once()
    assert ("SECRET_TRACE_RESPONSE_BODY" not in caplog.text) is redacted
    assert mock_response.text_reads == (0 if redacted else 1)
    exporter.close()


@patch("httpx2.Client")
def test_backend_span_exporter_5xx_retry(mock_client):
    mock_response = MagicMock()
    mock_response.status_code = 500

    # Make post() return 500 every time
    mock_client.return_value.post.return_value = mock_response

    exporter = BackendSpanExporter(api_key="test_key", max_retries=3, base_delay=0.1, max_delay=0.2)
    with patch.object(exporter._shutdown_event, "wait", return_value=False) as wait_for_retry:
        exporter.export([get_span(mock_processor())])

    # Should retry up to max_retries times
    assert mock_client.return_value.post.call_count == 3
    assert wait_for_retry.call_count == 2

    exporter.close()


@patch("httpx2.Client")
def test_backend_span_exporter_deadline_stops_during_5xx_retry_backoff(mock_client):
    mock_response = MagicMock()
    mock_response.status_code = 504
    mock_client.return_value.post.return_value = mock_response

    exporter = BackendSpanExporter(api_key="test_key", max_retries=3, base_delay=1.0)
    with patch("time.sleep") as sleep_for_retry:
        exporter._export_with_deadline(
            [get_span(mock_processor())], deadline=time.monotonic() + 0.01
        )

    assert mock_client.return_value.post.call_count == 1
    sleep_for_retry.assert_called_once()
    assert sleep_for_retry.call_args.args[0] <= 0.1

    exporter.close()


@patch("httpx2.Client")
def test_batch_trace_processor_shutdown_interrupts_exporter_retry_backoff(mock_client):
    post_called = threading.Event()
    mock_response = MagicMock()
    mock_response.status_code = 504

    def post(**kwargs: Any) -> Any:
        post_called.set()
        return mock_response

    mock_client.return_value.post.side_effect = post

    exporter = BackendSpanExporter(
        api_key="test_key",
        max_retries=100,
        base_delay=10.0,
        max_delay=10.0,
    )
    processor = BatchTraceProcessor(
        exporter=exporter,
        max_queue_size=1,
        max_batch_size=1,
        schedule_delay=60.0,
        export_trigger_ratio=1.0,
    )

    try:
        processor.on_span_end(get_span(processor))
        assert post_called.wait(timeout=2.0)

        start = time.monotonic()
        processor.shutdown(timeout=1.0)
        elapsed = time.monotonic() - start

        assert elapsed < 0.5
        assert processor._worker_thread is not None
        assert not processor._worker_thread.is_alive()
        assert mock_client.return_value.post.call_count == 1

    finally:
        processor.shutdown(timeout=2.0)
        exporter.close()


@patch("httpx2.Client")
def test_batch_trace_processor_shutdown_without_timeout_preserves_export_retries(mock_client):
    mock_response = MagicMock()
    mock_response.status_code = 504
    mock_client.return_value.post.return_value = mock_response

    exporter = BackendSpanExporter(
        api_key="test_key",
        max_retries=3,
        base_delay=0.1,
        max_delay=0.2,
    )
    processor = BatchTraceProcessor(exporter=exporter)
    processor._queue.put_nowait(get_span(processor))

    with patch.object(exporter._shutdown_event, "wait", return_value=False) as wait_for_retry:
        processor.shutdown(timeout=None)

    assert mock_client.return_value.post.call_count == 3
    assert wait_for_retry.call_count == 2

    exporter.close()


@pytest.mark.serial
@pytest.mark.review_optional
def test_tracing_atexit_cleanup_timeout_preserves_process_exit_code_on_504() -> None:
    script = textwrap.dedent(
        """
        import sys
        import threading
        import time
        from unittest.mock import patch

        from agents.tracing import custom_span, trace
        from agents.tracing.processors import BackendSpanExporter, BatchTraceProcessor
        from agents.tracing.provider import DefaultTraceProvider
        from agents.tracing import setup as tracing_setup

        tracing_setup._DEFAULT_SHUTDOWN_TIMEOUT = 0.2

        class Always504Response:
            status_code = 504
            text = "gateway timeout"

        class Always504Client:
            def __init__(self):
                self.request_seen = threading.Event()

            def post(self, **kwargs):
                self.request_seen.set()
                return Always504Response()

            def close(self):
                pass

        client = Always504Client()
        with patch("agents.tracing.processors.httpx2.Client", return_value=client):
            exporter = BackendSpanExporter(
                api_key="test_key",
                max_retries=100,
                base_delay=10.0,
                max_delay=10.0,
            )
        processor = BatchTraceProcessor(
            exporter=exporter,
            max_queue_size=1,
            max_batch_size=1,
            schedule_delay=60.0,
            export_trigger_ratio=1.0,
        )
        provider = DefaultTraceProvider()
        provider.register_processor(processor)
        original_shutdown = provider.shutdown

        def timed_shutdown(*args, **kwargs):
            shutdown_started = time.monotonic()
            try:
                return original_shutdown(*args, **kwargs)
            finally:
                print(
                    f"shutdown_elapsed={time.monotonic() - shutdown_started:.6f}",
                    flush=True,
                )

        provider.shutdown = timed_shutdown
        tracing_setup.set_trace_provider(provider)

        with trace("probe"):
            with custom_span("probe-span"):
                pass

        assert client.request_seen.wait(timeout=5.0)
        sys.exit(7)
        """
    )

    result = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
        timeout=10.0,
    )

    assert result.returncode == 7
    shutdown_elapsed_prefix = "shutdown_elapsed="
    shutdown_elapsed_lines = [
        line for line in result.stdout.splitlines() if line.startswith(shutdown_elapsed_prefix)
    ]
    assert len(shutdown_elapsed_lines) == 1
    assert float(shutdown_elapsed_lines[0][len(shutdown_elapsed_prefix) :]) < 0.5


@patch("httpx2.Client")
def test_backend_span_exporter_request_error(mock_client):
    # Make post() raise a RequestError each time
    mock_client.return_value.post.side_effect = httpx2.RequestError("Network error")

    exporter = BackendSpanExporter(api_key="test_key", max_retries=2, base_delay=0.1, max_delay=0.2)
    with patch.object(exporter._shutdown_event, "wait", return_value=False) as wait_for_retry:
        exporter.export([get_span(mock_processor())])

    # Should retry up to max_retries times
    assert mock_client.return_value.post.call_count == 2
    wait_for_retry.assert_called_once()

    exporter.close()


@patch("httpx2.Client")
def test_backend_span_exporter_close(mock_client):
    exporter = BackendSpanExporter(api_key="test_key")
    exporter.close()

    # Ensure underlying http client is closed
    mock_client.return_value.close.assert_called_once()


@patch("httpx2.Client")
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
    assert "total_tokens" not in sent_usage
    assert "input_tokens_details" not in sent_usage
    assert "output_tokens_details" not in sent_usage
    assert sent_usage["input_tokens"] == 10
    assert sent_usage["output_tokens"] == 5
    assert sent_usage["details"] == {
        "requests": 1,
        "total_tokens": 15,
        "input_tokens_details": {"cached_tokens": 1},
        "output_tokens_details": {"reasoning_tokens": 2},
    }

    # Ensure the original exported object has not been mutated.
    assert "requests" in item.exported_payload["span_data"]["usage"]
    assert item.exported_payload["span_data"]["usage"]["total_tokens"] == 15
    exporter.close()


@patch("httpx2.Client")
def test_backend_span_exporter_truncates_large_input_for_openai_tracing(mock_client):
    class DummyItem:
        tracing_api_key = None

        def __init__(self):
            self.exported_payload: dict[str, Any] = {
                "object": "trace.span",
                "span_data": {
                    "type": "generation",
                    "input": "x" * (BackendSpanExporter._OPENAI_TRACING_MAX_FIELD_BYTES + 5_000),
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
    sent_input = sent_payload["span_data"]["input"]
    assert isinstance(sent_input, str)
    assert sent_input.endswith(exporter._OPENAI_TRACING_STRING_TRUNCATION_SUFFIX)
    assert exporter._value_json_size_bytes(sent_input) <= exporter._OPENAI_TRACING_MAX_FIELD_BYTES
    assert item.exported_payload["span_data"]["input"] != sent_input
    exporter.close()


@patch("httpx2.Client")
def test_backend_span_exporter_truncates_large_structured_input_without_stringifying(mock_client):
    class NoStringifyDict(dict[str, Any]):
        def __str__(self) -> str:
            raise AssertionError("__str__ should not be called for oversized non-string previews")

    class DummyItem:
        tracing_api_key = None

        def __init__(self):
            payload_input = NoStringifyDict(
                blob="x" * (BackendSpanExporter._OPENAI_TRACING_MAX_FIELD_BYTES + 5_000)
            )
            self.exported_payload: dict[str, Any] = {
                "object": "trace.span",
                "span_data": {
                    "type": "generation",
                    "input": payload_input,
                },
            }

        def export(self):
            return self.exported_payload

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_client.return_value.post.return_value = mock_response

    exporter = BackendSpanExporter(api_key="test_key")
    exporter.export([cast(Any, DummyItem())])

    sent_payload = mock_client.return_value.post.call_args.kwargs["json"]["data"][0]
    sent_input = sent_payload["span_data"]["input"]
    assert isinstance(sent_input, dict)
    assert isinstance(sent_input["blob"], str)
    assert sent_input["blob"].endswith(exporter._OPENAI_TRACING_STRING_TRUNCATION_SUFFIX)
    assert exporter._value_json_size_bytes(sent_input) <= exporter._OPENAI_TRACING_MAX_FIELD_BYTES
    exporter.close()


@patch("httpx2.Client")
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


@patch("httpx2.Client")
def test_backend_span_exporter_drops_non_generation_usage_for_openai_endpoint(mock_client):
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
    assert "usage" not in sent_payload["span_data"]
    exporter.close()


@patch("httpx2.Client")
def test_backend_span_exporter_keeps_non_generation_usage_for_custom_endpoint(mock_client):
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

    exporter = BackendSpanExporter(
        api_key="test_key",
        endpoint="https://example.com/v1/traces/ingest",
    )
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
            },
        },
    }
    assert exporter._sanitize_for_openai_tracing_api(payload) is payload
    exporter.close()


@patch("httpx2.Client")
def test_backend_span_exporter_keeps_large_input_for_custom_endpoint(mock_client):
    class DummyItem:
        tracing_api_key = None

        def __init__(self):
            self.exported_payload: dict[str, Any] = {
                "object": "trace.span",
                "span_data": {
                    "type": "generation",
                    "input": "x" * (BackendSpanExporter._OPENAI_TRACING_MAX_FIELD_BYTES + 5_000),
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
    item = DummyItem()
    exporter.export([cast(Any, item)])

    sent_payload: dict[str, Any] = mock_client.return_value.post.call_args.kwargs["json"]["data"][0]
    assert sent_payload["span_data"]["input"] == item.exported_payload["span_data"]["input"]
    exporter.close()


def test_sanitize_for_openai_tracing_api_moves_unsupported_generation_usage_to_details():
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
                "details": {"provider": "litellm"},
            },
        },
    }
    sanitized = exporter._sanitize_for_openai_tracing_api(payload)
    assert sanitized["span_data"]["usage"] == {
        "input_tokens": 1,
        "output_tokens": 2,
        "details": {
            "provider": "litellm",
            "total_tokens": 3,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens_details": {"reasoning_tokens": 0},
        },
    }
    exporter.close()


def test_sanitize_for_openai_tracing_api_filters_non_json_values_in_usage_details():
    exporter = BackendSpanExporter(api_key="test_key")
    non_json = object()
    payload = {
        "object": "trace.span",
        "span_data": {
            "type": "generation",
            "usage": {
                "input_tokens": 1,
                "output_tokens": 2,
                "input_tokens_details": {
                    "cached_tokens": 0,
                    "bad": non_json,
                },
                "output_tokens_details": {"reasoning_tokens": 0},
                "provider_usage": [1, non_json, {"ok": True, "bad": non_json}],
                "details": {
                    "provider": "litellm",
                    "bad": non_json,
                    "nested": {"keep": 1, "bad": non_json},
                },
            },
        },
    }
    sanitized = exporter._sanitize_for_openai_tracing_api(payload)
    assert sanitized["span_data"]["usage"] == {
        "input_tokens": 1,
        "output_tokens": 2,
        "details": {
            "provider": "litellm",
            "nested": {"keep": 1},
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens_details": {"reasoning_tokens": 0},
            "provider_usage": [1, {"ok": True}],
        },
    }
    exporter.close()


def test_sanitize_for_openai_tracing_api_handles_cyclic_usage_values():
    exporter = BackendSpanExporter(api_key="test_key")
    cyclic_dict: dict[str, Any] = {}
    cyclic_dict["self"] = cyclic_dict
    cyclic_list: list[Any] = []
    cyclic_list.append(cyclic_list)

    payload = {
        "object": "trace.span",
        "span_data": {
            "type": "generation",
            "usage": {
                "input_tokens": 1,
                "output_tokens": 2,
                "input_tokens_details": cyclic_dict,
                "details": {
                    "provider": "litellm",
                    "cycle": cyclic_list,
                },
            },
        },
    }

    sanitized = exporter._sanitize_for_openai_tracing_api(payload)
    assert sanitized["span_data"]["usage"] == {
        "input_tokens": 1,
        "output_tokens": 2,
        "details": {
            "provider": "litellm",
            "cycle": [],
            "input_tokens_details": {},
        },
    }
    exporter.close()


def test_sanitize_for_openai_tracing_api_drops_non_dict_generation_usage_details():
    exporter = BackendSpanExporter(api_key="test_key")
    payload = {
        "object": "trace.span",
        "span_data": {
            "type": "generation",
            "usage": {
                "input_tokens": 1,
                "output_tokens": 2,
                "details": "invalid",
            },
        },
    }
    sanitized = exporter._sanitize_for_openai_tracing_api(payload)
    assert sanitized["span_data"]["usage"] == {
        "input_tokens": 1,
        "output_tokens": 2,
    }
    exporter.close()


def test_sanitize_for_openai_tracing_api_drops_generation_usage_missing_required_tokens():
    exporter = BackendSpanExporter(api_key="test_key")
    payload = {
        "object": "trace.span",
        "span_data": {
            "type": "generation",
            "usage": {
                "input_tokens": 1,
                "total_tokens": 3,
                "input_tokens_details": {"cached_tokens": 0},
                "output_tokens_details": {"reasoning_tokens": 0},
            },
        },
    }
    sanitized = exporter._sanitize_for_openai_tracing_api(payload)
    assert sanitized["span_data"] == {
        "type": "generation",
    }
    exporter.close()


def test_sanitize_for_openai_tracing_api_rejects_boolean_token_counts():
    exporter = BackendSpanExporter(api_key="test_key")
    payload = {
        "object": "trace.span",
        "span_data": {
            "type": "generation",
            "usage": {
                "input_tokens": True,
                "output_tokens": False,
                "input_tokens_details": {"cached_tokens": 0},
                "output_tokens_details": {"reasoning_tokens": 0},
            },
        },
    }
    sanitized = exporter._sanitize_for_openai_tracing_api(payload)
    assert sanitized["span_data"] == {
        "type": "generation",
    }
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


def test_sanitize_for_openai_tracing_api_keeps_small_input_without_mutation():
    exporter = BackendSpanExporter(api_key="test_key")
    payload = {
        "object": "trace.span",
        "span_data": {
            "type": "generation",
            "input": "short input",
            "usage": {"input_tokens": 1, "output_tokens": 2},
        },
    }

    assert exporter._sanitize_for_openai_tracing_api(payload) is payload
    exporter.close()


def test_sanitize_for_openai_tracing_api_truncates_oversized_output():
    exporter = BackendSpanExporter(api_key="test_key")
    payload: dict[str, Any] = {
        "object": "trace.span",
        "span_data": {
            "type": "function",
            "output": "x" * (BackendSpanExporter._OPENAI_TRACING_MAX_FIELD_BYTES + 5_000),
        },
    }

    sanitized = exporter._sanitize_for_openai_tracing_api(payload)
    assert sanitized is not payload
    assert sanitized["span_data"]["output"].endswith(
        exporter._OPENAI_TRACING_STRING_TRUNCATION_SUFFIX
    )
    assert (
        exporter._value_json_size_bytes(sanitized["span_data"]["output"])
        <= exporter._OPENAI_TRACING_MAX_FIELD_BYTES
    )
    assert payload["span_data"]["output"] != sanitized["span_data"]["output"]
    exporter.close()


def test_sanitize_for_openai_tracing_api_preserves_generation_input_list_shape():
    exporter = BackendSpanExporter(api_key="test_key")
    payload = {
        "object": "trace.span",
        "span_data": {
            "type": "generation",
            "input": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_audio",
                            "input_audio": {
                                "data": "x"
                                * (BackendSpanExporter._OPENAI_TRACING_MAX_FIELD_BYTES + 5_000),
                                "format": "wav",
                            },
                        }
                    ],
                }
            ],
            "usage": {"input_tokens": 1, "output_tokens": 1},
        },
    }

    sanitized = exporter._sanitize_for_openai_tracing_api(payload)
    sanitized_input = sanitized["span_data"]["input"]
    assert isinstance(sanitized_input, list)
    assert isinstance(sanitized_input[0], dict)
    assert sanitized_input[0]["role"] == "user"
    assert (
        exporter._value_json_size_bytes(sanitized_input) <= exporter._OPENAI_TRACING_MAX_FIELD_BYTES
    )
    exporter.close()


def test_sanitize_for_openai_tracing_api_replaces_unserializable_output():
    exporter = BackendSpanExporter(api_key="test_key")
    payload: dict[str, Any] = {
        "object": "trace.span",
        "span_data": {
            "type": "function",
            "output": b"x" * 10,
        },
    }

    sanitized = exporter._sanitize_for_openai_tracing_api(payload)
    assert sanitized["span_data"]["output"] == {
        "truncated": True,
        "original_type": "bytes",
        "preview": "<bytes bytes=10 truncated>",
    }
    exporter.close()


def test_truncate_json_value_for_limit_terminates_preview_dict_under_zero_budget():
    exporter = BackendSpanExporter(api_key="test_key")
    preview = exporter._truncated_preview(None)

    truncated = exporter._truncate_json_value_for_limit(preview, 0)

    assert truncated == {}
    exporter.close()


def test_sanitize_for_openai_tracing_api_handles_none_content_under_tight_budget():
    exporter = BackendSpanExporter(api_key="test_key")
    payload: dict[str, Any] = {
        "object": "trace.span",
        "span_data": {
            "type": "generation",
            "output": [
                {
                    "role": "assistant",
                    "content": None,
                    "name": "a" * 25_000,
                    "tool_calls": [],
                }
                for _ in range(8)
            ],
            "usage": {"input_tokens": 1, "output_tokens": 1},
        },
    }

    sanitized = exporter._sanitize_for_openai_tracing_api(payload)
    sanitized_output = cast(list[Any], sanitized["span_data"]["output"])

    assert isinstance(sanitized_output, list)
    assert sanitized_output != payload["span_data"]["output"]
    assert (
        exporter._value_json_size_bytes(sanitized_output)
        <= exporter._OPENAI_TRACING_MAX_FIELD_BYTES
    )
    assert any(item == {} for item in sanitized_output)
    exporter.close()


def test_truncate_string_for_json_limit_returns_original_when_within_limit():
    exporter = BackendSpanExporter(api_key="test_key")
    value = "hello"
    max_bytes = exporter._value_json_size_bytes(value)

    assert exporter._truncate_string_for_json_limit(value, max_bytes) == value
    exporter.close()


def test_truncate_string_for_json_limit_returns_suffix_when_limit_equals_suffix():
    exporter = BackendSpanExporter(api_key="test_key")
    max_bytes = exporter._value_json_size_bytes(exporter._OPENAI_TRACING_STRING_TRUNCATION_SUFFIX)

    assert (
        exporter._truncate_string_for_json_limit("x" * 100, max_bytes)
        == exporter._OPENAI_TRACING_STRING_TRUNCATION_SUFFIX
    )
    exporter.close()


def test_truncate_string_for_json_limit_returns_empty_when_suffix_too_large():
    exporter = BackendSpanExporter(api_key="test_key")
    max_bytes = (
        exporter._value_json_size_bytes(exporter._OPENAI_TRACING_STRING_TRUNCATION_SUFFIX) - 1
    )

    assert exporter._truncate_string_for_json_limit("x" * 100, max_bytes) == ""
    exporter.close()


def test_truncate_string_for_json_limit_handles_escape_heavy_input():
    exporter = BackendSpanExporter(api_key="test_key")
    value = ('\\"' * 40_000) + "tail"
    max_bytes = exporter._OPENAI_TRACING_MAX_FIELD_BYTES

    truncated = exporter._truncate_string_for_json_limit(value, max_bytes)

    assert truncated.endswith(exporter._OPENAI_TRACING_STRING_TRUNCATION_SUFFIX)
    assert exporter._value_json_size_bytes(truncated) <= max_bytes
    exporter.close()
