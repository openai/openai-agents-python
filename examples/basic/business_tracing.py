"""Offline service example. See business_tracing.md for setup and export monitoring."""

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager

from opentelemetry.context import Context
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import Span, StatusCode, Tracer
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from agents import Agent, ModelSettings, Runner
from agents.extensions.tracing import OpenTelemetryTracingProcessor
from agents.testing import ModelStep, ScriptedModel, assistant_message
from agents.tracing import flush_traces, metadata_only_run_tracing, set_trace_processors


@contextmanager
def stage(tracer: Tracer, name: str, parent: Context | None = None) -> Iterator[Span]:
    with tracer.start_as_current_span(
        name, context=parent, record_exception=False, set_status_on_exception=False
    ) as span:
        try:
            yield span
        except BaseException:
            span.set_attribute("app.outcome", "failed")
            span.set_status(StatusCode.ERROR)
            raise
        else:
            span.set_attribute("app.outcome", "ok")


async def route(
    tracer: Tracer, traceparent: str, agent: Agent, request: str, outbox: list[str]
) -> None:
    parent = TraceContextTextMapPropagator().extract({"traceparent": traceparent})
    with stage(tracer, "route", parent), metadata_only_run_tracing():
        with stage(tracer, "specialist") as span:
            result = await Runner.run(agent, request)
            totals = [(r.raw_usage or {}).get("total_tokens") for r in result.raw_responses]
            measured = [n for n in totals if type(n) is int and n >= 0]
            available = bool(totals) and len(measured) == len(totals)
            span.set_attribute("app.direct_usage.available", available)
            if available:
                span.set_attribute("app.direct_usage.total_tokens", sum(measured))
        with stage(tracer, "validate"):
            queue = result.final_output
            if queue not in ("standard", "priority"):
                raise ValueError("Unknown queue")
        with stage(tracer, "deliver"):
            outbox.append(queue)


async def main() -> None:
    # Configure this standalone service once, before accepting requests.
    provider = TracerProvider()
    receiver = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(receiver))
    bridge = OpenTelemetryTracingProcessor(tracer_provider=provider)
    set_trace_processors([bridge])
    tracer = provider.get_tracer(__name__)
    try:
        agent = Agent(
            name="classifier",
            model=ScriptedModel(
                [ModelStep(output=[assistant_message("standard")], raw_usage={"total_tokens": 8})]
            ),
            model_settings=ModelSettings(preserve_raw_usage=True),
        )
        outbox: list[str] = []
        with stage(tracer, "ingress"):
            headers: dict[str, str] = {}
            TraceContextTextMapPropagator().inject(headers)
            await route(tracer, headers.get("traceparent", ""), agent, "Synthetic request", outbox)
        print(f"Delivered {len(outbox)} item(s)")
        flush_traces()
        provider.force_flush()
        for span in receiver.get_finished_spans():
            if span.attributes and "app.outcome" in span.attributes:
                print(span.name, span.attributes["app.outcome"])
        print(f"Spans received in memory: {len(receiver.get_finished_spans())}")
    finally:
        bridge.shutdown()
        provider.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
