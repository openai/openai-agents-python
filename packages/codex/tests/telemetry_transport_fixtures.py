"""Local OTLP collectors that decode the official protobuf messages and services."""

from __future__ import annotations

import importlib
import os
import ssl
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from agents import telemetry
from tests.native_telemetry_fixtures import mutual_tls_collector

SIGNALS = ("logs", "traces", "metrics")


def require_qualification_dependencies() -> None:
    """Optional locally; fail collection when the CI qualification gate requires them."""
    for name in (
        "grpc",
        "opentelemetry.proto.collector.trace.v1.trace_service_pb2_grpc",
    ):
        reason = "Native tracing qualification requires grpcio and opentelemetry-proto"
        if os.environ.get("AGENTS_REQUIRE_TRACING_QUALIFICATION") == "1":
            try:
                importlib.import_module(name)
            except ImportError:
                pytest.fail(reason, pytrace=False)
        else:
            pytest.importorskip(name, reason=reason, exc_type=ImportError)


@dataclass
class DecodedCollector:
    endpoint: str = ""
    plain_endpoint: str | None = None
    records: list[dict[str, Any]] = field(default_factory=list)

    def record(self, signal: str, message: Any, headers: Any, authenticated: bool) -> None:
        formatter = importlib.import_module("google.protobuf.json_format")
        self.records.append(
            {
                "signal": signal,
                "payload": formatter.MessageToDict(message),
                "headers": {key.lower(): value for key, value in headers},
                "client_authenticated": authenticated,
            }
        )


def _messages(signal: str) -> Any:
    package, name = {
        "logs": ("logs", "logs"),
        "traces": ("trace", "trace"),
        "metrics": ("metrics", "metrics"),
    }[signal]
    return importlib.import_module(f"opentelemetry.proto.collector.{package}.v1.{name}_service_pb2")


def _request_response(signal: str) -> tuple[Any, Any]:
    prefix = {"logs": "Logs", "traces": "Trace", "metrics": "Metrics"}[signal]
    messages = _messages(signal)
    return (
        getattr(messages, f"Export{prefix}ServiceRequest"),
        getattr(messages, f"Export{prefix}ServiceResponse"),
    )


@asynccontextmanager
async def decoded_collector(
    transport: str, security: str, root: Path, *, mixed: bool = False
) -> AsyncIterator[tuple[DecodedCollector, telemetry.OtelTlsConfig | None]]:
    collector = DecodedCollector()
    tls = None
    context = None
    if security != "plain":
        tls, context = await mutual_tls_collector(root)
        if security == "ca":
            context.verify_mode = ssl.CERT_NONE
            tls = telemetry.OtelTlsConfig(ca_certificate=tls.ca_certificate)

    if transport == "http":
        formatter = importlib.import_module("google.protobuf.json_format")

        async def receive(request: web.Request) -> web.Response:
            signal = request.match_info["signal"]
            request_type, response_type = _request_response(signal)
            message = request_type()
            data = await request.read()
            json_protocol = "json" in request.content_type
            if json_protocol:
                formatter.Parse(data.decode(), message)
            else:
                message.ParseFromString(data)
            peer = request.transport
            collector.record(
                signal,
                message,
                request.headers.items(),
                bool(peer and peer.get_extra_info("peercert")),
            )
            return web.Response(
                body=b"{}" if json_protocol else response_type().SerializeToString(),
                content_type="application/json" if json_protocol else "application/x-protobuf",
            )

        def app() -> web.Application:
            application = web.Application()
            application.router.add_post("/v1/{signal:logs|traces|metrics}", receive)
            return application

        server = TestServer(app(), scheme="https" if context else "http")
        plain = TestServer(app()) if mixed else None
        try:
            await server.start_server(ssl=context)
            collector.endpoint = str(server.make_url("")).rstrip("/")
            if plain is not None:
                await plain.start_server()
                collector.plain_endpoint = str(plain.make_url("")).rstrip("/")
            yield collector, tls
        finally:
            await server.close()
            if plain is not None:
                await plain.close()
    else:
        grpc = importlib.import_module("grpc")
        server = grpc.aio.server()

        def service(signal: str) -> Any:
            _, response_type = _request_response(signal)

            class Receiver:
                async def Export(self, request: Any, context: Any) -> Any:
                    collector.record(
                        signal,
                        request,
                        context.invocation_metadata(),
                        bool(context.peer_identities()),
                    )
                    return response_type()

            return Receiver()

        for signal in SIGNALS:
            module = importlib.import_module(_messages(signal).__name__ + "_grpc")
            name = {"logs": "Logs", "traces": "Trace", "metrics": "Metrics"}[signal]
            getattr(module, f"add_{name}ServiceServicer_to_server")(service(signal), server)
        if context is None:
            port = server.add_insecure_port("127.0.0.1:0")
        else:
            credentials = grpc.ssl_server_credentials(
                [((root / "server.key").read_bytes(), (root / "server.pem").read_bytes())],
                root_certificates=(root / "ca.pem").read_bytes(),
                require_client_auth=security == "mtls",
            )
            port = server.add_secure_port("127.0.0.1:0", credentials)
        assert port > 0
        await server.start()
        collector.endpoint = f"{'https' if context else 'http'}://localhost:{port}"
        try:
            yield collector, tls
        finally:
            await server.stop(grace=0)
