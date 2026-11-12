"""Trace a synthetic business workflow through a separate loopback HTTP worker."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Lock
from time import perf_counter_ns
from typing import Any
from urllib.request import Request, urlopen

from agents import (
    Agent,
    NativeError,
    RunBudget,
    ToolContext,
    TurnNeedsAttention,
    TurnStoppedError,
    function_tool,
    telemetry,
)
from agents.testing import ScriptedResponse, ScriptedResponses, text, tool_call

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


@contextmanager
def business_stage(name: str, receipts: list[dict[str, Any]]) -> Iterator[dict[str, Any]]:
    receipt: dict[str, Any] = {"stage": name, "outcome": "completed"}
    started = perf_counter_ns()
    with telemetry.span("business." + name, options={"metadata": {"business.stage": name}}) as span:
        try:
            yield receipt
        except Exception:
            receipt["outcome"] = "failed"
            raise
        finally:
            if "error_code" in receipt:
                span.set_outcome(status="error", category=receipt["error_code"])
            receipt["duration_ns"] = perf_counter_ns() - started
            receipts.append(receipt)


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


async def supervisory_stages(receipts: ReceiptProcessor) -> list[dict[str, Any]]:
    stages: list[dict[str, Any]] = []
    entered, cleaned = asyncio.Event(), asyncio.Event()
    with telemetry.trace("business.supervisory-stops"):
        for scenario in ("cancelled", "run_budget_exceeded"):
            entered.clear()
            cleaned.clear()

            @function_tool
            async def hold() -> str:
                """The synthetic stage has been waiting for cancellation."""
                entered.set()
                try:
                    await asyncio.Future()
                finally:
                    cleaned.set()
                return "unreachable"

            scripts = (
                [[tool_call("hold", {}, call_id="supervisory-hold")]]
                if scenario == "cancelled"
                else []
            )
            async with ScriptedResponses(*scripts) as peer:
                agent = Agent(
                    instructions="Run the synthetic stage.", provider=peer.provider, tools=[hold]
                )
                async with agent.session() as session:
                    with business_stage("supervisory_" + scenario, stages) as stage:
                        turn = await session.submit(
                            "Run the stage.",
                            budget=RunBudget(max_requests=0)
                            if scenario == "run_budget_exceeded"
                            else None,
                        )
                        if scenario == "cancelled":
                            await asyncio.wait_for(entered.wait(), 10)
                            await turn.cancel()
                            assert cleaned.is_set()
                        outcome = await turn.wait_for_attention()
                        assert isinstance(outcome, TurnNeedsAttention)
                        assert outcome.reason == (
                            "interrupted" if scenario == "cancelled" else "budget_exceeded"
                        )
                        if scenario == "cancelled":
                            assert isinstance(outcome.error, TurnStoppedError)
                        else:
                            assert isinstance(outcome.error, NativeError)
                            assert outcome.error.code == "run_budget_exceeded"
                            assert not cleaned.is_set()
                        try:
                            await turn.get_result()
                        except (TurnStoppedError, NativeError) as error:
                            assert error is outcome.error
                        else:
                            raise AssertionError("The stopped stage must not return a result")
                        stage.update(outcome="failed", error_code=scenario)
                        assert len(peer.requests) == (1 if scenario == "cancelled" else 0)
    await telemetry.force_flush()
    for stage in stages:
        assert stage["outcome"] == "failed"
        assert stage["duration_ns"] >= 0
        assert any(
            event["lifecycle"] == "end"
            and event["name"] == "business." + stage["stage"]
            and event["attributes"].get("sdk.outcome.category") == stage["error_code"]
            for event in receipts.snapshot()
        )
    return stages


async def workflow(collector_url: str, worker_url: str, scenario: str) -> dict[str, Any]:
    receipts = await configure(collector_url, "checkout")
    remote: dict[str, Any] = {}
    carrier: dict[str, str] = {}
    stages: list[dict[str, Any]] = []
    delivered: list[dict[str, str]] = []
    supervisory = await supervisory_stages(receipts)
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
            with business_stage("authorized_routing", stages) as route:
                registry = {"fulfillment": worker_url}
                selected_worker = registry["fulfillment"]
                route["specialist"] = "fulfillment"

            @function_tool
            async def dispatch(ctx: ToolContext[None]) -> str:
                """Call the separate synthetic fulfillment service."""
                assert ctx.trace_context is not None
                carrier.update(ctx.trace_context.to_headers())
                with business_stage("specialist_tool", stages) as stage:
                    remote.update(await asyncio.to_thread(post, selected_worker, carrier))
                    outcome = remote.get("outcome")
                    if not isinstance(outcome, str) or outcome not in ("completed", "stopped"):
                        raise ValueError("Unexpected synthetic worker outcome")
                    if outcome == "stopped":
                        stage.update(outcome="failed", error_code="fulfillment_stopped")
                return outcome

            async with ScriptedResponses(
                [tool_call("dispatch", {}, call_id="dispatch-1")],
                ScriptedResponse(
                    [text(SENTINEL)],
                    usage={"input_tokens": 3, "output_tokens": 2, "total_tokens": 5},
                ),
            ) as peer:
                completed = await Agent(
                    instructions="Dispatch the synthetic order.",
                    tools=[dispatch],
                    provider=peer.provider,
                ).run(SENTINEL)
            outcome = remote["outcome"]
            with business_stage("validation", stages) as validation:
                accepted = outcome == "completed" and completed.final_output == SENTINEL
                validation["outcome"] = "completed" if accepted else "rejected"
            with business_stage("in_memory_delivery", stages) as delivery:
                if accepted:
                    delivered.append({"outcome": "completed"})
                else:
                    delivery["outcome"] = "suppressed"
            usage = completed.run_usage
            accounting = {
                "reported_totals": {
                    "input_tokens": usage.input_tokens,
                    "output_tokens": usage.output_tokens,
                    "total_tokens": usage.total_tokens,
                },
                "observed_completions": usage.requests,
                "missing_usage_requests": usage.missing_usage_requests,
                "fixture_estimate": {"tokens": 8, "basis": "synthetic fixture estimate"},
                "worker_tokens": {"status": "unavailable"},
            }
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
        "stages": stages,
        "supervisory": supervisory,
        "delivered": delivered,
        "usage": accounting,
        "flush": asdict(flush),
        "events": receipts.snapshot(),
        "worker": remote,
        "replacement_flush": asdict(replacement_flush),
        "replacement_events": replacement.snapshot(),
    }


def verify(artifact: dict[str, Any]) -> None:
    local, remote = artifact["events"], artifact["worker"]["events"]
    assert local and remote, "Missing processor receipts"
    assert [row["stage"] for row in artifact["stages"]] == [
        "authorized_routing",
        "specialist_tool",
        "validation",
        "in_memory_delivery",
    ]
    assert all(row["duration_ns"] >= 0 for row in artifact["stages"])
    assert artifact["delivered"] == (
        [{"outcome": "completed"}] if artifact["outcome"] == "completed" else []
    )
    assert artifact["stages"][-1]["outcome"] == (
        "completed" if artifact["outcome"] == "completed" else "suppressed"
    )
    failed_stage = artifact["stages"][1]
    assert failed_stage.get("error_code") == (
        "fulfillment_stopped" if artifact["scenario"] == "failure" else None
    )
    assert any(
        event["name"] == "business.specialist_tool"
        and event["attributes"].get("sdk.outcome.category") == "fulfillment_stopped"
        for event in local
    ) == (artifact["scenario"] == "failure")
    assert artifact["usage"]["fixture_estimate"]["basis"] == "synthetic fixture estimate"
    assert artifact["usage"]["reported_totals"] == {
        "input_tokens": 3,
        "output_tokens": 2,
        "total_tokens": 5,
    }
    assert artifact["usage"]["observed_completions"] == 2
    assert artifact["usage"]["missing_usage_requests"] == 1
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
