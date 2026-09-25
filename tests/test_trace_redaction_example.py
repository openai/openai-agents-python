from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from typing import Any

import pytest

from agents import custom_span, set_trace_processors, trace
from agents.tracing import get_trace_provider
from agents.tracing.processors import BatchTraceProcessor
from examples.basic import trace_redaction
from examples.basic.trace_redaction import RedactingExporter, main, redact_payload

from .testing_processor import SPAN_PROCESSOR_TESTING


@pytest.fixture
def sent_batches() -> Iterator[list[list[dict[str, Any]]]]:
    batches: list[list[dict[str, Any]]] = []
    yield batches
    get_trace_provider().shutdown()
    set_trace_processors([SPAN_PROCESSOR_TESTING])


def test_example_emits_only_allowlisted_fields(capsys, sent_batches) -> None:
    main()
    records = [item for line in capsys.readouterr().out.splitlines() for item in json.loads(line)]
    assert len(records) == 2
    trace_record, span_record = records
    assert trace_record == {"object": "trace", "id": trace_record["id"]}
    assert span_record == {
        "object": "trace.span",
        "id": span_record["id"],
        "trace_id": trace_record["id"],
        "parent_id": None,
    }


@pytest.mark.parametrize("failure_object", ["trace", "trace.span"])
def test_redaction_failure_drops_batch_without_logging_payload_and_recovers(
    failure_object: str, sent_batches, caplog
) -> None:
    fail = True

    def redact(payload: dict[str, Any]) -> dict[str, Any]:
        if fail and payload["object"] == failure_object:
            raise ValueError(f"synthetic-secret: {payload}")
        return redact_payload(payload)

    processor = BatchTraceProcessor(
        RedactingExporter(redact, sent_batches.append), schedule_delay=3600
    )
    set_trace_processors([processor])
    with caplog.at_level(logging.WARNING):
        with trace("synthetic-secret", metadata={"private": "synthetic-secret"}):
            with custom_span("synthetic-secret", data={"private": "synthetic-secret"}):
                pass
        processor.force_flush()

    assert sent_batches == []
    assert "Trace redaction failed; dropping batch." in caplog.text
    assert "synthetic-secret" not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)

    fail = False
    with trace("next workflow") as next_trace:
        pass
    processor.force_flush()
    assert sent_batches == [[{"object": "trace", "id": next_trace.trace_id}]]


def test_formatter_failure_does_not_expose_redaction_exception(
    sent_batches, capsys, monkeypatch
) -> None:
    def redact(payload: dict[str, Any]) -> dict[str, Any]:
        raise ValueError(f"synthetic-secret: {payload}")

    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(missing_field)s"))
    isolated_logger = logging.Logger("trace_redaction_formatter_test")
    isolated_logger.addHandler(handler)
    monkeypatch.setattr(trace_redaction, "logger", isolated_logger)
    monkeypatch.setattr(logging, "raiseExceptions", True)

    processor = BatchTraceProcessor(
        RedactingExporter(redact, sent_batches.append), schedule_delay=3600
    )
    set_trace_processors([processor])
    with trace("synthetic-secret", metadata={"private": "synthetic-secret"}):
        pass
    processor.force_flush()

    stderr = capsys.readouterr().err
    assert "--- Logging error ---" in stderr
    assert "Formatting field not found in record" in stderr
    assert "synthetic-secret" not in stderr
    assert sent_batches == []


def test_redactor_mutation_cannot_change_original_trace_or_span(sent_batches) -> None:
    def redact(payload: dict[str, Any]) -> dict[str, Any]:
        if payload["object"] == "trace":
            payload["metadata"]["nested"]["private"] = "changed"
        else:
            payload["span_data"]["data"]["nested"]["private"] = "changed"
        return redact_payload(payload)

    processor = BatchTraceProcessor(
        RedactingExporter(redact, sent_batches.append), schedule_delay=3600
    )
    set_trace_processors([processor])
    with trace("workflow", metadata={"nested": {"private": "original"}}) as recorded_trace:
        with custom_span("operation", data={"nested": {"private": "original"}}) as recorded_span:
            pass
    processor.force_flush()

    assert len(sent_batches[0]) == 2
    original_payload = recorded_trace.export()
    assert original_payload is not None
    assert original_payload["metadata"]["nested"]["private"] == "original"
    assert recorded_span.span_data.data["nested"]["private"] == "original"


def test_shutdown_drops_queued_data_when_redaction_fails(sent_batches) -> None:
    def redact(payload: dict[str, Any]) -> dict[str, Any]:
        raise ValueError("synthetic-secret")

    processor = BatchTraceProcessor(
        RedactingExporter(redact, sent_batches.append), schedule_delay=3600
    )
    set_trace_processors([processor])
    with trace("synthetic-secret"):
        pass
    processor.shutdown()
    assert sent_batches == []
