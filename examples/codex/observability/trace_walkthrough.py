"""Receive native OTLP JSON locally; use synthetic data in a fresh process."""

from __future__ import annotations

import argparse
import asyncio
import html
import json
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from socketserver import TCPServer
from threading import Lock, Thread
from typing import Any, cast
from uuid import uuid4

from pydantic import BaseModel

from agents import (
    Agent,
    NativeError,
    NativeProvider,
    OutputValidationError,
    PromptedOutput,
    function_tool,
    telemetry,
)
from agents.testing import ScriptedResponses, text, tool_call
from agents.tools import ToolOutput

from .history_reader import read_history


class _LoopbackHTTPServer(ThreadingHTTPServer):
    def server_bind(self) -> None:
        # Numeric loopback needs no reverse DNS lookup during local startup.
        TCPServer.server_bind(self)
        self.server_name, self.server_port = cast(tuple[str, int], self.server_address)


class Collector:
    """A loopback demonstration receiver, not a production collector."""

    def __init__(self) -> None:
        self.batches: list[dict[str, Any]] = []
        self.lock = Lock()
        self.url = ""

    def snapshot(self) -> list[dict[str, Any]]:
        with self.lock:
            return list(self.batches)

    @contextmanager
    def serve(self, port: int = 0) -> Iterator[Collector]:
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:
                pass

            def do_POST(self) -> None:
                if self.path == "/provider/v1/responses":
                    body = b'{"error":{"message":"Synthetic authentication failure","type":"invalid_request_error","code":"invalid_api_key"}}'
                    self.send_response(401)
                elif self.path in {"/v1/traces", "/v1/logs"}:
                    length = int(self.headers.get("Content-Length", "0"))
                    batch = json.loads(self.rfile.read(length))
                    with owner.lock:
                        owner.batches.append(
                            {"signal": self.path.rsplit("/", 1)[-1], "data": batch}
                        )
                    body = b"{}"
                    self.send_response(200)
                else:
                    self.send_error(404)
                    return
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:
                body = (
                    "<!doctype html><title>Synthetic native telemetry</title>"
                    "<h1>Synthetic native telemetry</h1><p>Refresh after the run completes. "
                    "Search for the run ID printed by the command.</p><pre>"
                    + html.escape(json.dumps(owner.snapshot(), indent=2))
                    + "</pre>"
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = _LoopbackHTTPServer(("127.0.0.1", port), Handler)
        server.daemon_threads = False
        thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05})
        thread.start()
        self.url = f"http://127.0.0.1:{server.server_port}"
        try:
            yield self
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


class Report(BaseModel):
    count: int


async def exercise(scenario: str, collector_url: str) -> dict[str, Any]:
    calls: list[str] = []

    @function_tool
    async def lookup(record: str) -> ToolOutput:
        """Look up a synthetic record; this demonstration raises deliberately."""
        calls.append(record)
        if scenario == "tool-error":
            return ToolOutput.error("Synthetic catalog unavailable")
        raise RuntimeError("Synthetic catalog failure")

    receipt: dict[str, Any] = {"scenario": scenario, "tool_calls": calls}

    async def run(agent: Agent[Any, Any]) -> None:
        async with agent.session() as session:
            receipt["session_id"] = session.id
            turn = await session.submit("Look up synthetic record A.")
            try:
                result = await turn.get_result()
                receipt["outcome"] = "completed"
                receipt["final_output"] = str(result.final_output)
            except OutputValidationError:
                receipt["outcome"] = "output_validation_failure"
            except NativeError as error:
                receipt.update(
                    outcome="provider_failure" if error.status_code else "native_failure",
                    status_code=error.status_code,
                    category=error.category,
                    code=error.code,
                )
            except RuntimeError:
                receipt["outcome"] = "tool_callback_exception" if calls else "runtime_failure"
            receipt["turn_id"] = turn.turn_id
            receipt["history"] = await read_history(session)

    if scenario == "provider":
        provider = NativeProvider(
            base_url=collector_url + "/provider/v1",
            api_key="synthetic-provider-key",
            supports_websockets=False,
        )
        try:
            await run(Agent(instructions="Answer briefly.", provider=provider))
        finally:
            await provider.aclose()
    elif scenario == "validation":
        async with ScriptedResponses([text('{"count":"invalid"}')]) as peer:
            await run(
                Agent(
                    instructions="Return a count.",
                    provider=peer.provider,
                    output_type=PromptedOutput(Report),
                )
            )
    else:
        responses = [[tool_call("lookup", {"record": "A"}, call_id="lookup-a")]]
        if scenario == "tool-error":
            responses.append([text("The catalog is unavailable.")])
        async with ScriptedResponses(*responses) as peer:
            await run(Agent(instructions="Use lookup.", tools=[lookup], provider=peer.provider))
    return receipt


async def walkthrough(collector: Collector, scenario: str) -> dict[str, Any]:
    run_id = "synthetic-" + uuid4().hex
    await telemetry.configure(
        traces=telemetry.OtlpHttp(endpoint=collector.url + "/v1/traces", protocol="json"),
        logs=telemetry.OtlpHttp(endpoint=collector.url + "/v1/logs", protocol="json"),
        span_attributes={"example.run_id": run_id},
        environment="synthetic-local",
    )
    try:
        with telemetry.trace(
            "synthetic walkthrough",
            options=telemetry.TraceOptions(
                group_id=run_id,
                metadata={"scenario": scenario},
                # Keep detailed diagnostics for this synthetic, local example.
                include_sensitive_data=True,
            ),
        ):
            with telemetry.span("exercise scenario"):
                receipt = await exercise(scenario, collector.url)
        # Flush completed spans while leaving the SDK usable.
        await telemetry.force_flush()
    finally:
        # Shutdown retires the SDK-owned telemetry provider.
        await telemetry.shutdown()
    return {"run_id": run_id, "receipt": receipt, "batches": collector.snapshot()}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--scenario", choices=["tool", "tool-error", "provider", "validation"], default="tool"
    )
    parser.add_argument("--output", type=Path, default=Path("synthetic-trace.json"))
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument(
        "--view", action="store_true", help="Keep the local viewer open until Enter."
    )
    args = parser.parse_args()
    with Collector().serve(args.port) as collector:
        artifact = asyncio.run(walkthrough(collector, args.scenario))
        args.output.write_text(json.dumps(artifact, indent=2) + "\n")
        print(
            json.dumps(
                {
                    "run_id": artifact["run_id"],
                    "receipt": artifact["receipt"],
                    "batches_received": len(artifact["batches"]),
                    "viewer": collector.url,
                }
            )
        )
        if not artifact["batches"]:
            raise SystemExit("No OTLP receipt: check endpoint, proxy bypass and exporter errors.")
        if args.view:
            input("Open the viewer URL; press Enter to close it. ")


if __name__ == "__main__":
    main()
