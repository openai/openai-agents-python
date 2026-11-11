"""Trace a synthetic business workflow through a separate loopback HTTP worker."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Mapping
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Lock
from typing import Any
from urllib.request import Request, urlopen

from agents import Agent, ToolContext, function_tool, telemetry
from agents.testing import ScriptedResponses, text, tool_call

from .trace_walkthrough import Collector

SENTINEL = "SYNTHETIC_PRIVATE_ORDER_4821"


class FulfillmentStopped(RuntimeError):
    """A controlled business-stage failure."""


def plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [plain(item) for item in value]
    return value


class ReceiptProcessor:
    """Keep local synthetic receipts; production sinks need their own policy."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []
        self.lock = Lock()

    def on_trace_event(self, event: telemetry.TraceLifecycleEvent) -> None:
        with self.lock:
            self.events.append(plain(event))

    def snapshot(self) -> list[dict[str, Any]]:
        with self.lock:
            return list(self.events)


class FailingProcessor:
    def on_trace_event(self, event: telemetry.TraceLifecycleEvent) -> None:
        raise RuntimeError("Synthetic sink unavailable")


async def configure(collector_url: str, service: str) -> ReceiptProcessor:
    # Each service owns policy. W3C trace headers carry identity, not privacy.
    telemetry.set_sensitive_data_logging_enabled(False)
    await telemetry.configure(
        traces=telemetry.OtlpHttp(endpoint=collector_url + "/v1/traces", protocol="json"),
        span_attributes={"example.service": service},
        environment="synthetic-local",
        log_user_prompt=False,
        log_agent_responses=False,
        tool_result_max_bytes=0,
    )
    receipts = ReceiptProcessor()
    # Adding preserves the native OTLP destination.
    telemetry.add_trace_processor(receipts)
    return receipts


async def worker_run(collector_url: str, scenario: str, headers: dict[str, str]) -> dict[str, Any]:
    receipts = await configure(collector_url, "fulfillment")
    outcome = "completed"
    try:

        @function_tool
        async def fulfill() -> str:
            """Complete a synthetic fulfillment stage."""
            if scenario == "failure":
                raise FulfillmentStopped("Synthetic fulfillment stopped")
            return SENTINEL

        responses = [[tool_call("fulfill", {}, call_id="fulfill-1")]]
        if scenario == "success":
            responses.append([text(SENTINEL)])
        async with ScriptedResponses(*responses) as peer:
            try:
                await Agent(
                    instructions="Fulfill the synthetic order.",
                    tools=[fulfill],
                    provider=peer.provider,
                ).run(
                    SENTINEL,
                    parent_trace=headers,
                    trace_options=telemetry.TraceOptions(
                        include_sensitive_data=False,
                        metadata={"business.stage": "fulfillment", "business.route": "standard"},
                    ),
                )
            except FulfillmentStopped:
                if scenario != "failure":
                    raise
                outcome = "stopped"
        flush = await telemetry.force_flush()
    finally:
        await telemetry.shutdown()
    return {"outcome": outcome, "flush": asdict(flush), "events": receipts.snapshot()}


def worker(collector_url: str, scenario: str) -> None:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def do_POST(self) -> None:
            # This single-request local worker accepts only the known route.
            if self.path != "/fulfill":
                self.send_error(404)
                return
            headers = {
                key: self.headers[key]
                for key in ("traceparent", "tracestate")
                if key in self.headers
            }
            result = asyncio.run(worker_run(collector_url, scenario, headers))
            body = json.dumps(result).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    with HTTPServer(("127.0.0.1", 0), Handler) as server:
        print(f"http://127.0.0.1:{server.server_port}/fulfill", flush=True)
        server.handle_request()


def post(url: str, headers: dict[str, str]) -> dict[str, Any]:
    with urlopen(Request(url, data=b"", headers=headers, method="POST"), timeout=30) as response:
        receipt = json.load(response)
    if not isinstance(receipt, dict):
        raise ValueError("Expected a synthetic worker receipt")
    return receipt


async def workflow(collector_url: str, worker_url: str, scenario: str) -> dict[str, Any]:
    receipts = await configure(collector_url, "checkout")
    remote: dict[str, Any] = {}
    carrier: dict[str, str] = {}
    if scenario == "failure":
        telemetry.add_trace_processor(FailingProcessor())
    try:
        with telemetry.trace(
            "order workflow",
            options=telemetry.TraceOptions(
                include_sensitive_data=False,
                metadata={"business.stage": "checkout", "business.route": "standard"},
            ),
        ) as root:
            with telemetry.span(
                "validate order", options={"metadata": {"business.stage": "validation"}}
            ):
                # Business metadata comes from constants or allowlisted enums.
                # The synthetic private order stays in provider/tool payloads.
                pass

            @function_tool
            async def dispatch(ctx: ToolContext[None]) -> str:
                """Call the separate synthetic fulfillment service."""
                assert ctx.trace_context is not None
                carrier.update(ctx.trace_context.to_headers())
                remote.update(await asyncio.to_thread(post, worker_url, carrier))
                outcome = remote.get("outcome")
                if not isinstance(outcome, str) or outcome not in ("completed", "stopped"):
                    raise ValueError("Unexpected synthetic worker outcome")
                return outcome

            async with ScriptedResponses(
                [tool_call("dispatch", {}, call_id="dispatch-1")], [text(SENTINEL)]
            ) as peer:
                await Agent(
                    instructions="Dispatch the synthetic order.",
                    tools=[dispatch],
                    provider=peer.provider,
                ).run(SENTINEL)
            outcome = remote["outcome"]
            with telemetry.span(
                "record business outcome",
                options={
                    "metadata": {
                        "business.stage": "completion",
                        "business.outcome": outcome,
                        "business.usage.worker_calls": 1,
                        "business.usage.tokens": "unavailable",
                    }
                },
            ):
                pass
            root.set_outcome(
                status="ok" if outcome == "completed" else "error",
                category="completed" if outcome == "completed" else "fulfillment_stopped",
            )
            root_id = root.context.trace_id
        flush = await telemetry.force_flush()

        # Replacement changes future routes, including native OTLP. Finish and
        # flush the workflow before changing routes so the comparison is bounded.
        replacement = ReceiptProcessor()
        telemetry.set_trace_processors([replacement])
        with telemetry.trace("replacement probe"):
            pass
        replacement_flush = await telemetry.force_flush()
    finally:
        await telemetry.shutdown()
    return {
        "scenario": scenario,
        "outcome": outcome,
        "trace_id": root_id,
        "carrier": carrier,
        "flush": asdict(flush),
        "events": receipts.snapshot(),
        "worker": remote,
        "replacement_flush": asdict(replacement_flush),
        "replacement_events": replacement.snapshot(),
    }


def verify(artifact: dict[str, Any]) -> None:
    local, remote = artifact["events"], artifact["worker"]["events"]
    assert local and remote, "Missing processor receipts"
    assert SENTINEL not in json.dumps(artifact), "Private payload reached an artifact"
    tool_id = artifact["carrier"]["traceparent"][36:52]
    tool = next(event for event in local if event["span_id"] == tool_id)
    assert tool["trace_id"] == artifact["trace_id"]
    assert any(
        event["trace_id"] == tool["trace_id"] and event["parent_span_id"] == tool["span_id"]
        for event in remote
    ), "Missing remote parent link"
    settlement = next(event for event in local if event["name"] == "record business outcome")
    assert settlement["attributes"]["sdk.metadata.business.usage.tokens"] == "unavailable"
    spans = [
        span
        for batch in artifact["batches"]
        for resource in batch["data"].get("resourceSpans", [])
        for scope in resource["scopeSpans"]
        for span in scope["spans"]
    ]
    assert spans, "Missing native OTLP sink receipt"
    assert any(span["spanId"] == tool_id for span in spans), "Missing checkout OTLP receipt"
    remote_ids = {event["span_id"] for event in remote}
    assert any(span["spanId"] in remote_ids for span in spans), "Missing fulfillment OTLP receipt"
    assert not any(span["name"] == "replacement probe" for span in spans)
    assert any(event["name"] == "replacement probe" for event in artifact["replacement_events"])
    failed = artifact["flush"]["failed_deliveries"]
    assert failed > 0 if artifact["scenario"] == "failure" else failed == 0
    assert artifact["worker"]["flush"]["status"] == "success"
    assert artifact["outcome"] == ("completed" if artifact["scenario"] == "success" else "stopped")


async def with_worker(collector_url: str, scenario: str) -> dict[str, Any]:
    child = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "examples.codex.observability.business_workflow",
        "--worker",
        collector_url,
        "--scenario",
        scenario,
        stdout=asyncio.subprocess.PIPE,
    )
    try:
        assert child.stdout is not None
        worker_url = (await asyncio.wait_for(child.stdout.readline(), timeout=15)).decode().strip()
        assert worker_url.startswith("http://127.0.0.1:"), "Worker startup failed"
        artifact = await workflow(collector_url, worker_url, scenario)
        assert await asyncio.wait_for(child.wait(), timeout=30) == 0
        return artifact
    finally:
        if child.returncode is None:
            child.kill()
        await child.wait()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", choices=["success", "failure"], default="success")
    parser.add_argument("--output", type=Path, help="Optional local synthetic receipt artifact")
    parser.add_argument("--worker", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        worker(args.worker, args.scenario)
        return
    with Collector().serve() as collector:
        artifact = asyncio.run(with_worker(collector.url, args.scenario))
        artifact["batches"] = collector.snapshot()
        verify(artifact)
        if args.output:
            args.output.write_text(json.dumps(artifact, indent=2) + "\n")
        print(
            json.dumps({key: artifact[key] for key in ("scenario", "outcome", "trace_id", "flush")})
        )


if __name__ == "__main__":
    main()
