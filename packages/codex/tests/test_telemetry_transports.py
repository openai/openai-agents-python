"""REQ450: decoded native OTLP delivery to synthetic HTTP and gRPC collectors.

Each case runs in a fresh process because the native subscriber has one process
lifetime. These tests require the built native extension, openssl, grpcio and
opentelemetry-proto. No model credentials or external services are used.

Required qualification (after the native SDK build), with optional test dependencies:
    AGENTS_REQUIRE_TRACING_QUALIFICATION=1 uv run --with grpcio==1.85.0 \\
      --with opentelemetry-proto==1.45.1 python packages/codex/scripts/test_native.py \\
      --sdk -m pytest -q packages/codex/tests/test_tracing_native.py \\
      packages/codex/tests/test_telemetry_transports.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Literal

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from agents import Agent, NativeProvider, telemetry
from tests.native_fixtures import ResponsesFixture, assistant_output
from tests.telemetry_transport_fixtures import (
    SIGNALS,
    decoded_collector,
    require_qualification_dependencies,
)

require_qualification_dependencies()

CASES = [
    ("http", "json", "plain", "all", "delivery"),
    *[
        (transport, "binary", security, "all", "delivery")
        for transport in ("http", "grpc")
        for security in ("plain", "ca", "mtls")
    ],
    *[
        (transport, "binary", "plain", signal, "delivery")
        for transport in ("http", "grpc")
        for signal in SIGNALS
    ],
    ("http", "binary", "mtls", "all", "mixed"),
    *[
        (transport, "binary", "mtls", "all", scenario)
        for transport in ("http", "grpc")
        for scenario in ("invalid_identity", "incomplete_identity", "untrusted_server")
    ],
]


@pytest.mark.parametrize("case", CASES, ids=lambda case: "-".join(case))
def test_decoded_native_otlp_transport(case: tuple[str, str, str, str, str]) -> None:
    if case[2] != "plain" and shutil.which("openssl") is None:
        if os.environ.get("AGENTS_REQUIRE_TRACING_QUALIFICATION") == "1":
            pytest.fail("Required REQ450 TLS qualification needs openssl")
        pytest.skip("REQ450 TLS qualification requires openssl")
    root = str(Path(__file__).resolve().parents[1])
    env = dict(os.environ)
    env.pop("OPENAI_API_KEY", None)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, (root, env.get("PYTHONPATH"))))
    with tempfile.TemporaryDirectory() as home:
        env["CODEX_HOME"] = home
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import asyncio, json, sys; "
                "from tests.test_telemetry_transports import _qualify_transport; "
                "asyncio.run(asyncio.wait_for(_qualify_transport(*json.loads(sys.argv[1])), 40))",
                json.dumps(case),
            ],
            env=env,
            text=True,
            capture_output=True,
            timeout=50,
        )
    assert result.returncode == 0, result.stdout + result.stderr


async def _qualify_transport(
    transport: str,
    protocol: Literal["binary", "json"],
    security: str,
    selected: str,
    scenario: str,
) -> None:
    fixture = ResponsesFixture()
    application = web.Application()
    application.router.add_post("/v1/responses", fixture.handle)
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        async with (
            decoded_collector(transport, security, root, mixed=scenario == "mixed") as (peer, tls),
            TestServer(application) as model,
        ):
            if scenario == "invalid_identity":
                (root / "client.key").write_text("synthetic invalid private key")
            elif scenario == "incomplete_identity":
                assert tls is not None
                tls = telemetry.OtelTlsConfig(client_certificate=tls.client_certificate)
            elif scenario == "untrusted_server":
                assert tls is not None
                tls = telemetry.OtelTlsConfig(
                    ca_certificate=tls.client_certificate,
                    client_certificate=tls.client_certificate,
                    client_private_key=tls.client_private_key,
                )
            signals = SIGNALS if selected == "all" else (selected,)
            settings: dict[str, Any] = {}
            for signal in signals:
                plain = scenario == "mixed" and signal == "metrics"
                endpoint = peer.plain_endpoint if plain else peer.endpoint
                assert endpoint is not None
                settings[signal] = (
                    telemetry.OtlpHttp(
                        endpoint=f"{endpoint}/v1/{signal}",
                        protocol=protocol,
                        headers={"x-otlp-fixture": signal},
                        tls=None if plain else tls,
                    )
                    if transport == "http"
                    else telemetry.OtlpGrpc(
                        endpoint=endpoint,
                        headers={"x-otlp-fixture": signal},
                        tls=tls,
                    )
                )
            if scenario in ("invalid_identity", "incomplete_identity"):
                with pytest.raises(RuntimeError, match="Native telemetry configuration failed"):
                    await telemetry.configure(**settings)
                assert not peer.records
                return
            await telemetry.configure(
                **settings,
                environment="transport-qualification",
                span_attributes={"fixture.transport": transport},
                log_user_prompt=True,
                log_agent_responses=True,
            )
            provider = NativeProvider(
                base_url=str(model.make_url("/v1")), api_key="synthetic-transport-key"
            )
            response = assistant_output("SYNTHETIC_TRANSPORT_ANSWER")
            response["item"]["phase"] = "final_answer"
            fixture.respond(response)
            try:
                agent = Agent(model="gpt-5.1-codex", provider=provider, instructions="Answer.")
                result = await agent.run("SYNTHETIC_TRANSPORT_PROMPT")
                assert result.final_output == "SYNTHETIC_TRANSPORT_ANSWER"
                assert len(fixture.requests) == 1
            finally:
                await provider.aclose()
                await telemetry.shutdown()
            if scenario == "untrusted_server":
                assert not peer.records
                return
            assert {record["signal"] for record in peer.records} == set(signals)
            resources = {
                "logs": "resourceLogs",
                "traces": "resourceSpans",
                "metrics": "resourceMetrics",
            }
            for record in peer.records:
                signal = record["signal"]
                assert record["headers"]["x-otlp-fixture"] == signal
                if transport == "http":
                    content_type = record["headers"]["content-type"]
                    assert ("json" if protocol == "json" else "x-protobuf") in content_type
                if security == "mtls":
                    assert record["client_authenticated"] == (
                        not (scenario == "mixed" and signal == "metrics")
                    )
                batches = record["payload"][resources[signal]]
                assert batches
                for batch in batches:
                    attributes = batch["resource"]["attributes"]
                    assert {
                        "key": "env",
                        "value": {"stringValue": "transport-qualification"},
                    } in attributes
                    assert {
                        "key": "service.name",
                        "value": {"stringValue": "codex_agents_python"},
                    } in attributes
            payloads = {
                signal: [record["payload"] for record in peer.records if record["signal"] == signal]
                for signal in signals
            }
            if "logs" in signals:
                logs = json.dumps(payloads["logs"])
                assert "codex.conversation_starts" in logs
                assert "SYNTHETIC_TRANSPORT_PROMPT" in logs
                assert "SYNTHETIC_TRANSPORT_ANSWER" in logs
            if "traces" in signals:
                spans = [
                    span
                    for payload in payloads["traces"]
                    for resource in payload["resourceSpans"]
                    for scope in resource["scopeSpans"]
                    for span in scope["spans"]
                ]
                assert any(
                    span.get("name")
                    and span.get("traceId")
                    and span.get("spanId")
                    and {"key": "fixture.transport", "value": {"stringValue": transport}}
                    in span.get("attributes", [])
                    for span in spans
                )
            if "metrics" in signals:
                metrics = [
                    metric
                    for payload in payloads["metrics"]
                    for resource in payload["resourceMetrics"]
                    for scope in resource["scopeMetrics"]
                    for metric in scope["metrics"]
                ]
                assert any(
                    metric["name"] == "codex.process.start"
                    and any(point.get("asInt") == "1" for point in metric["sum"]["dataPoints"])
                    for metric in metrics
                )
