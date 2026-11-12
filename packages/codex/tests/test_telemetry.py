"""Facade serialization and native telemetry in fresh processes.

Native subscriber registration is terminal for a process, so lifecycle tests
must not share the test runner's native owner. All network peers are local and
synthetic. Mapping tests isolate only the native initialization call because
they cover transport/TLS selections without contacting collectors.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import traceback
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from _agents_codex import telemetry as runtime_telemetry
from agents import Agent, NativeProvider, function_tool, telemetry
from tests.native_fixtures import ResponsesFixture, assistant_output, function_call


def _assert_credentials_absent_from_sdk_traceback(error: BaseException) -> None:
    # Application frames own the caller's credentials. Inspect the SDK's frames
    # and its JSON serializer in the complete chain, including native failures.
    captured = traceback.TracebackException.from_exception(error, capture_locals=True)
    sdk = Path(runtime_telemetry.__file__).parent
    encoder = Path(json.__file__).parent
    while captured is not None:
        for frame in captured.stack:
            if (
                Path(frame.filename).is_relative_to(sdk)
                or Path(frame.filename).is_relative_to(encoder)
            ) and frame.locals is not None:
                for credential in (
                    "synthetic-private-header",
                    "synthetic-private-user",
                    "synthetic-private-password",
                    "synthetic-private-query",
                    "synthetic-private-path",
                    "synthetic-private-span-value",
                ):
                    assert credential not in repr(frame.locals), (
                        frame.filename,
                        frame.name,
                        frame.locals,
                    )
        captured = captured.__cause__ or captured.__context__


@pytest.mark.parametrize("source", ["headers", "span_attributes"])
async def test_mapping_iteration_failure_drops_credentials_from_sdk_frames(source: str) -> None:
    failure = RuntimeError("mapping iteration failed")

    class InterruptedMapping(Mapping[str, str]):
        def __iter__(self) -> Iterator[str]:
            yield "synthetic-private-user"
            raise failure

        def __getitem__(self, key: str) -> str:
            if key == "synthetic-private-user":
                return "synthetic-private-header"
            raise KeyError(key)

        def __len__(self) -> int:
            return 2

    mapping = InterruptedMapping()
    with pytest.raises(RuntimeError, match="mapping iteration failed") as raised:
        if source == "headers":
            await telemetry.configure(
                traces=telemetry.OtlpGrpc(endpoint="https://test.invalid:4317", headers=mapping)
            )
        else:
            await telemetry.configure(span_attributes=mapping)
    assert raised.value is failure
    _assert_credentials_absent_from_sdk_traceback(raised.value)
    # The application still owns its mapping; cleanup must not mutate it.
    assert mapping["synthetic-private-user"] == "synthetic-private-header"


async def test_exporter_serialization_hides_headers_and_maps_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agents import _native

    captured: list[dict[str, Any]] = []

    async def capture(config_json: str) -> None:
        captured.append(json.loads(config_json))

    monkeypatch.setattr(_native, "configure_telemetry", capture)
    monkeypatch.setattr(runtime_telemetry, "_engine_options", None)
    monkeypatch.chdir(tmp_path)
    header = {"authorization": "synthetic-secret-header"}
    tls = telemetry.OtelTlsConfig(
        ca_certificate="ca.pem", client_certificate=Path("client.pem"), client_private_key="key.pem"
    )
    http = telemetry.OtlpHttp(endpoint="https://test.invalid/v1/logs", headers=header, tls=tls)
    grpc = telemetry.OtlpGrpc(endpoint="https://test.invalid:4317", headers=header)
    assert "synthetic-secret-header" not in repr(http)
    assert "synthetic-secret-header" not in repr(grpc)
    await telemetry.configure(
        traces=grpc,
        logs=http,
        metrics=grpc,
        environment="test",
        span_attributes={"app.tenant": "synthetic"},
        log_user_prompt=True,
        tool_result_max_bytes=42,
    )
    assert captured == [
        {
            "otel.exporter": {
                "otlp-http": {
                    "endpoint": "https://test.invalid/v1/logs",
                    "protocol": "binary",
                    "headers": header,
                    "tls": {
                        "ca-certificate": str(tmp_path / "ca.pem"),
                        "client-certificate": str(tmp_path / "client.pem"),
                        "client-private-key": str(tmp_path / "key.pem"),
                    },
                }
            },
            "otel.trace_exporter": {
                "otlp-grpc": {"endpoint": "https://test.invalid:4317", "headers": header}
            },
            "otel.metrics_exporter": {
                "otlp-grpc": {"endpoint": "https://test.invalid:4317", "headers": header}
            },
            "analytics.enabled": True,
            "otel.environment": "test",
            "otel.span_attributes": {"app.tenant": "synthetic"},
            "otel.log_user_prompt": True,
            "otel.log_agent_responses": False,
            "otel.tool_result": {"max_bytes": 42},
        }
    ]


@pytest.mark.parametrize("field", ["ca_certificate", "client_certificate", "client_private_key"])
@pytest.mark.parametrize("absolute", [False, True], ids=["relative", "absolute"])
async def test_tls_path_conversion_with_deleted_cwd_preserves_privacy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str, absolute: bool
) -> None:
    from agents import _native

    captured: list[dict[str, Any]] = []

    async def capture(config_json: str) -> None:
        captured.append(json.loads(config_json))

    monkeypatch.setattr(_native, "configure_telemetry", capture)
    monkeypatch.setattr(runtime_telemetry, "_engine_options", None)
    monkeypatch.chdir(tmp_path)
    tls_path = Path("synthetic-private-path") / "credential.pem"
    if absolute:
        tls_path = tmp_path / tls_path
    exporter = telemetry.OtlpGrpc(
        endpoint="https://test.invalid:4317",
        tls=telemetry.OtelTlsConfig(**{field: tls_path}),
    )
    with monkeypatch.context() as cwd:
        if os.name == "nt":
            # Windows holds the process cwd open, so it cannot be removed.
            # Exercise the same OS lookup failure at its boundary instead.
            def vanished_cwd() -> str:
                raise FileNotFoundError("current directory no longer exists")

            cwd.setattr(os, "getcwd", vanished_cwd)
            # Python 3.10's pathlib retains its own reference to os.getcwd.
            accessor = getattr(Path, "_accessor", None)
            if accessor is not None:
                cwd.setattr(accessor, "getcwd", vanished_cwd)
        else:
            missing = tmp_path / "vanished"
            missing.mkdir()
            cwd.chdir(missing)
            missing.rmdir()
        if absolute:
            await telemetry.configure(traces=exporter)
        else:
            with pytest.raises(FileNotFoundError) as raised:
                await telemetry.configure(traces=exporter)
    if absolute:
        assert captured[0]["otel.trace_exporter"]["otlp-grpc"]["tls"] == {
            field.replace("_", "-"): str(tls_path)
        }
    else:
        assert captured == []
        captured_error = traceback.TracebackException.from_exception(
            raised.value, capture_locals=True
        )
        while captured_error is not None:
            in_sdk = False
            for frame in captured_error.stack:
                # The application owns its local path. Everything after the
                # SDK entry, including pathlib, belongs to this conversion.
                in_sdk |= Path(frame.filename).is_relative_to(
                    Path(runtime_telemetry.__file__).parent
                )
                if in_sdk:
                    assert "synthetic-private-path" not in repr(frame.locals)
            captured_error = captured_error.__cause__ or captured_error.__context__


async def test_none_signals_disable_native_exporters(monkeypatch: pytest.MonkeyPatch) -> None:
    from agents import _native

    captured = []

    async def capture(config_json: str) -> None:
        captured.append(json.loads(config_json))

    monkeypatch.setattr(_native, "configure_telemetry", capture)
    monkeypatch.setattr(runtime_telemetry, "_engine_options", None)
    await telemetry.configure()
    assert captured[0]["otel.exporter"] == "none"
    assert captured[0]["otel.trace_exporter"] == "none"
    assert captured[0]["otel.metrics_exporter"] == "none"
    assert captured[0]["analytics.enabled"] is False


@pytest.mark.parametrize(
    "case",
    [
        "default",
        "enabled",
        "cancel",
        "duplicate",
        "failure",
        "cancel_failure",
        "lifecycle",
        "restore",
        "invalid_config",
        "metrics",
        "metrics_restore",
        "metrics_none",
        "skill_metrics",
        "skill_metrics_none",
        "open_failure",
        "validation_traces",
        "validation_metrics",
        "validation_spans",
        "validation_tls_type",
        "validation_tls_path",
        "validation_headers",
        "http_mtls_logs",
        "http_mtls_metrics",
        "http_mtls_all",
        "http_mtls_mixed",
        "http_mtls_invalid_identity",
        "http_mtls_incomplete_identity",
        "http_mtls_untrusted_server",
    ],
)
def test_native_process(case: str) -> None:
    env = dict(os.environ)
    root = str(Path(__file__).resolve().parents[1])
    env["PYTHONPATH"] = os.pathsep.join(filter(None, (root, env.get("PYTHONPATH"))))
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import asyncio, sys; from tests.test_telemetry import _native_case; "
            "asyncio.run(asyncio.wait_for(_native_case(sys.argv[1]), 40))",
            case,
        ],
        env=env,
        text=True,
        capture_output=True,
        timeout=50,
    )
    assert result.returncode == 0, result.stdout + result.stderr


async def _explicit_skill_body(agent: Agent, fixture: ResponsesFixture) -> None:
    from tests.native_executor_fixtures import ExecutorPeer
    from tests.test_environment_leases_checklist import remote, skill, trust

    with tempfile.TemporaryDirectory() as directory, pytest.MonkeyPatch.context() as patch:
        root = Path(directory)
        async with ExecutorPeer(root / "remote").serve() as peer:
            trust(peer, root, patch)
            chosen = peer.root / "chosen"
            selected_body = "SELECTED_SKILL_BODY_ORCHID"
            unselected_body = "UNSELECTED_SKILL_BODY_RUBY"
            for name, description, body in (
                ("wire-selected-skill", "CHOSEN_ORCHID_CATALOG", selected_body),
                ("wire-unselected-skill", "OTHER_RUBY_CATALOG", unselected_body),
            ):
                skill(chosen, name, description)
                path = chosen / "skills" / name / "SKILL.md"
                path.write_text(path.read_text() + body + "\n")
            result = await agent.run("Use $wire-selected-skill", environment=remote(peer, chosen))
            assert result.final_output == "synthetic metric answer"
            await peer.wait_disconnected()
            assert len(fixture.requests) == 1
            wire = json.dumps(fixture.requests[0]["input"])
            # Both candidates were advertised, but only the explicitly named body is injected.
            assert "CHOSEN_ORCHID_CATALOG" in wire
            assert "OTHER_RUBY_CATALOG" in wire
            assert selected_body in wire
            assert unselected_body not in wire
            assert not fixture.replies


async def _native_case(case: str) -> None:
    from agents import _native

    if case.startswith("http_mtls_"):
        await _native_http_mtls(case.removeprefix("http_mtls_"))
        return

    if case.startswith("validation_"):
        header = {"authorization": "synthetic-private-header"}
        logs = telemetry.OtlpHttp(
            endpoint=(
                "https://synthetic-private-user:synthetic-private-password@127.0.0.1:1"
                "/v1/logs?token=synthetic-private-query"
            ),
            headers=header,
        )
        settings: dict[str, Any] = {"logs": logs}
        error_type: type[Exception] = TypeError
        if case == "validation_traces":
            settings["traces"] = telemetry.OtlpHttp(endpoint="")
            error_type = ValueError
            message = "traces endpoint must be a non-empty string"
        elif case == "validation_metrics":
            settings["traces"] = telemetry.OtlpGrpc(endpoint=logs.endpoint, headers=header)
            settings["metrics"] = telemetry.OtlpGrpc(endpoint="")
            error_type = ValueError
            message = "metrics endpoint must be a non-empty string"
        elif case == "validation_spans":
            settings["metrics"] = telemetry.OtlpGrpc(endpoint=logs.endpoint, headers=header)
            settings["span_attributes"] = {"app.tenant": 7}
            message = "span_attributes must be a mapping of strings to strings or None"
        elif case == "validation_tls_type":
            settings["traces"] = telemetry.OtlpGrpc(
                endpoint=logs.endpoint,
                headers=header,
                tls="invalid",  # type: ignore[arg-type]
            )
            message = "traces tls must be OtelTlsConfig or None"
        elif case == "validation_tls_path":
            settings["traces"] = telemetry.OtlpHttp(
                endpoint=logs.endpoint,
                headers=header,
                tls=telemetry.OtelTlsConfig(
                    ca_certificate="/synthetic-private-path/ca.pem",
                    client_certificate=7,  # type: ignore[arg-type]
                ),
            )
            message = "traces tls client_certificate must be a string, Path, or None"
        elif case == "validation_headers":
            settings["traces"] = telemetry.OtlpGrpc(
                endpoint=logs.endpoint,
                headers={"authorization": "synthetic-private-header", "invalid": 7},  # type: ignore[dict-item]
            )
            message = "traces headers must be a mapping of strings to strings or None"
        with pytest.raises(error_type, match=message) as raised:
            await telemetry.configure(**settings)
        _assert_credentials_absent_from_sdk_traceback(raised.value)
        assert header == {"authorization": "synthetic-private-header"}
        assert logs.headers is header
        assert "synthetic-private-query" in logs.endpoint
        with pytest.raises(RuntimeError, match="successfully before shutdown"):
            await telemetry.shutdown()
        # Python validation or encoding failed before native subscriber admission.
        await telemetry.configure()
        await telemetry.shutdown()
        return

    if case == "invalid_config":
        with pytest.raises(RuntimeError) as raised:
            await telemetry.configure(
                logs=telemetry.OtlpHttp(
                    endpoint=(
                        "https://synthetic-private-user:synthetic-private-password@127.0.0.1:1"
                        "/v1/logs?token=synthetic-private-query"
                    ),
                    protocol="synthetic-private-protocol",  # type: ignore[arg-type]
                    headers={"authorization": "synthetic-private-header"},
                )
            )
        assert str(raised.value) == (
            "Native telemetry configuration failed. Check the exporter settings. "
            "If native initialization claimed the subscriber, start a new process."
        )
        assert raised.value.__context__ is None
        assert raised.value.__cause__ is None
        _assert_credentials_absent_from_sdk_traceback(raised.value)
        with pytest.raises(RuntimeError, match="successfully before shutdown"):
            await telemetry.shutdown()
        await telemetry.configure()
        await telemetry.shutdown()
        return

    if case in ("failure", "cancel_failure"):
        with pytest.raises(RuntimeError, match="successfully before shutdown"):
            await telemetry.shutdown()
        logs = telemetry.OtlpHttp(
            endpoint=(
                "https://synthetic-private-user:synthetic-private-password@127.0.0.1:1"
                "/v1/logs?token=synthetic-private-query"
            ),
            headers={"authorization": "synthetic-private-header"},
            tls=telemetry.OtelTlsConfig(ca_certificate="/synthetic-private-path/no-ca.pem"),
        )
        span_attributes = {"app.tenant": "synthetic-private-span-value"}
        if case == "cancel_failure":
            failing = asyncio.Event()
            release = asyncio.Event()
            original = _native.configure_telemetry

            async def delayed(config_json: str) -> None:
                try:
                    await original(config_json)
                except RuntimeError:
                    failing.set()
                    await release.wait()
                    raise

            _native.configure_telemetry = delayed
            try:
                task = asyncio.create_task(
                    telemetry.configure(
                        logs=logs, span_attributes=span_attributes, log_user_prompt=True
                    )
                )
                await failing.wait()
                task.cancel()
                release.set()
                with pytest.raises(asyncio.CancelledError) as cancelled:
                    await task
                context = cancelled.value.__context__
                # Python 3.10 wraps the original cancellation at the Task boundary.
                if isinstance(context, asyncio.CancelledError):
                    context = context.__context__
                assert isinstance(context, RuntimeError)
                assert "Native telemetry configuration failed" in str(context)
                assert "synthetic-private" not in str(context)
                assert context.__context__ is None
                _assert_credentials_absent_from_sdk_traceback(cancelled.value)
            finally:
                _native.configure_telemetry = original
        else:
            with pytest.raises(
                RuntimeError, match="Native telemetry configuration failed"
            ) as error:
                await telemetry.configure(
                    logs=logs, span_attributes=span_attributes, log_user_prompt=True
                )
            assert error.value.__context__ is None
            assert error.value.__cause__ is None
            assert "synthetic-private" not in str(error.value)
            _assert_credentials_absent_from_sdk_traceback(error.value)
        assert logs.headers == {"authorization": "synthetic-private-header"}
        assert span_attributes == {"app.tenant": "synthetic-private-span-value"}
        with pytest.raises(RuntimeError, match="successfully before shutdown"):
            await telemetry.shutdown()
        with pytest.raises(RuntimeError, match="only be configured once"):
            await telemetry.configure()
        return

    fixture = ResponsesFixture()
    collected: list[bytes] = []
    collector_headers: list[str | None] = []
    collector_auth: list[str | None] = []
    collector_query: list[str | None] = []
    metric_batches: list[dict[str, Any]] = []

    async def collect(request: web.Request) -> web.Response:
        collected.append(await request.read())
        collector_headers.append(request.headers.get("x-synthetic-telemetry"))
        collector_auth.append(request.headers.get("authorization"))
        collector_query.append(request.query.get("token"))
        return web.json_response({})

    async def collect_metrics(request: web.Request) -> web.Response:
        metric_batches.append(await request.json())
        return web.json_response({})

    app = web.Application()
    app.router.add_post("/v1/responses", fixture.handle)
    app.router.add_post("/v1/logs", collect)
    app.router.add_post("/v1/metrics", collect_metrics)
    async with TestServer(app) as server:
        provider = NativeProvider(
            base_url=str(server.make_url("/v1")), api_key="synthetic-telemetry-key"
        )
        if case in (
            "metrics",
            "metrics_restore",
            "metrics_none",
            "skill_metrics",
            "skill_metrics_none",
        ):
            metrics = telemetry.OtlpHttp(
                endpoint=str(server.make_url("/v1/metrics")),
                protocol="json",
                headers={"x-synthetic-telemetry": "synthetic-metric-header"},
            )
            await telemetry.configure(metrics=None if case.endswith("_none") else metrics)
            agent = Agent(model="gpt-5.1-codex", provider=provider, instructions="Answer.")
            fixture.respond(assistant_output("synthetic metric answer"))
            if case.startswith("skill_"):
                await _explicit_skill_body(agent, fixture)
            elif case == "metrics_restore":
                async with agent.session() as session:
                    turn = await session.submit("synthetic metric prompt")
                    assert (await turn.get_result()).final_output == "synthetic metric answer"
                    saved = await session.save_state()
                    assert "synthetic-metric-header" not in saved.to_json()
                fixture.respond(assistant_output("synthetic restored answer"))
                async with agent.session(state=saved) as restored:
                    turn = await restored.submit("synthetic restored prompt")
                    assert (await turn.get_result()).final_output == "synthetic restored answer"
            else:
                assert (await agent.run("synthetic metric prompt")).final_output == (
                    "synthetic metric answer"
                )
            await provider.aclose()
            await telemetry.shutdown()
            assert not collected
            if case.endswith("_none"):
                assert not metric_batches
            else:
                records = [
                    metric
                    for batch in metric_batches
                    for resource in batch["resourceMetrics"]
                    for scope in resource["scopeMetrics"]
                    for metric in scope["metrics"]
                ]
                # Native records this histogram only for a completed turn.
                turns = [
                    point
                    for metric in records
                    if metric["name"] == "codex.turn.tool.call"
                    for point in metric["histogram"]["dataPoints"]
                ]
                assert sum(int(point["count"]) for point in turns) == (
                    2 if case == "metrics_restore" else 1
                ), [metric["name"] for metric in records]
            return
        if case == "lifecycle":
            with pytest.raises(RuntimeError, match="successfully before shutdown"):
                await telemetry.shutdown()
            await telemetry.configure()
            fixture.respond(assistant_output("done"))
            agent = Agent(model="gpt-5.1-codex", provider=provider, instructions="Answer.")
            assert (await agent.run("synthetic")).final_output == "done"
            with pytest.raises(RuntimeError, match="only be configured once"):
                await telemetry.configure()
            await telemetry.shutdown()
            with pytest.raises(RuntimeError, match="start a new process"):
                await agent.run("synthetic again")
            await provider.aclose()
            assert not collected
            return

        logs = telemetry.OtlpHttp(
            endpoint=str(
                server.make_url("/v1/logs")
                .with_user("synthetic-private-user")
                .with_password("synthetic-private-password")
                .with_query(token="synthetic-private-query")
            ),
            protocol="json",
            headers={
                "x-synthetic-telemetry": "synthetic-header",
                "authorization": "synthetic-private-header",
            },
        )
        enabled = case in ("enabled", "cancel")
        settings = {
            "logs": logs,
            "log_user_prompt": enabled,
            "log_agent_responses": enabled,
            "tool_result_max_bytes": 512 if enabled else 0,
        }
        if case == "cancel":
            initialized = asyncio.Event()
            release = asyncio.Event()
            original = _native.configure_telemetry

            async def delayed(config_json: str) -> None:
                # Hold the real completed owner before it reports success to
                # the facade; cancellation must still publish the real result.
                await original(config_json)
                initialized.set()
                await release.wait()

            _native.configure_telemetry = delayed
            try:
                task = asyncio.create_task(telemetry.configure(**settings))
                await initialized.wait()
                task.cancel()
                await asyncio.sleep(0)
                assert not task.done()
                release.set()
                with pytest.raises(asyncio.CancelledError):
                    await task
            finally:
                _native.configure_telemetry = original
        else:
            await telemetry.configure(**settings)
        if case == "open_failure":
            await provider.aclose()
            agent = Agent(model="gpt-5.1-codex", provider=provider, instructions="Answer.")
            with pytest.raises(RuntimeError, match="NativeProvider is closed") as error:
                async with agent.session():
                    pytest.fail("A closed provider must not open a Session")
            _assert_credentials_absent_from_sdk_traceback(error.value)
            await telemetry.shutdown()
            assert logs.headers == {
                "x-synthetic-telemetry": "synthetic-header",
                "authorization": "synthetic-private-header",
            }
            return
        if case == "duplicate":
            with pytest.raises(RuntimeError, match="only be configured once"):
                await telemetry.configure(
                    logs=logs,
                    log_user_prompt=True,
                    log_agent_responses=True,
                    tool_result_max_bytes=512,
                )

        @function_tool
        async def lookup(key: str) -> str:
            """Read a synthetic record."""
            return "SYNTHETIC_TOOL_RESULT_CONTENT_9238"

        agent = Agent(
            instructions="Use lookup once.",
            model="gpt-5.1-codex",
            provider=provider,
            tools=[lookup],
            programmatic_tool_calling=False,
        )
        fixture.respond(function_call("lookup", {"key": "unit"}, "lookup-one"))
        response = assistant_output("SYNTHETIC_AGENT_OUTPUT_5491")
        # Core emits the agent response record only for a live final-answer message.
        response["item"]["phase"] = "final_answer"
        fixture.respond(response)
        if case == "restore":
            async with agent.session() as session:
                turn = await session.submit("SYNTHETIC_USER_PROMPT_0712")
                assert (await turn.get_result()).final_output == "SYNTHETIC_AGENT_OUTPUT_5491"
                saved = await session.save_state()
                assert "synthetic-header" not in saved.to_json()
            fixture.respond(function_call("lookup", {"key": "unit"}, "lookup-restored"))
            fixture.respond(response)
            async with agent.session(state=saved) as restored:
                turn = await restored.submit("SYNTHETIC_USER_PROMPT_0712")
                assert (await turn.get_result()).final_output == "SYNTHETIC_AGENT_OUTPUT_5491"
            assert len(fixture.requests) == 4
        else:
            result = await agent.run("SYNTHETIC_USER_PROMPT_0712")
            assert result.final_output == "SYNTHETIC_AGENT_OUTPUT_5491"
            assert len(fixture.requests) == 2
        await provider.aclose()
        await telemetry.shutdown()
        assert logs.headers == {
            "x-synthetic-telemetry": "synthetic-header",
            "authorization": "synthetic-private-header",
        }
        assert collected
        assert set(collector_headers) == {"synthetic-header"}
        assert set(collector_query) == {"synthetic-private-query"}
        assert set(collector_auth) == {"synthetic-private-header"}
        # Bound named native records only, not unrelated spans or logs.
        records = [
            log
            for batch in collected
            for resource in json.loads(batch)["resourceLogs"]
            for scope in resource["scopeLogs"]
            for log in scope["logRecords"]
        ]
        encoded = json.dumps(records)
        assert ("SYNTHETIC_TOOL_RESULT_CONTENT_9238" in encoded) == enabled
        assert ("SYNTHETIC_USER_PROMPT_0712" in encoded) == enabled
        assert ("SYNTHETIC_AGENT_OUTPUT_5491" in encoded) == enabled


async def _native_http_mtls(signal: str) -> None:
    from tests.native_telemetry_fixtures import mutual_tls_collector

    fixture = ResponsesFixture()
    batches: dict[str, list[dict[str, Any]]] = {name: [] for name in ("logs", "metrics", "traces")}
    headers: dict[str, list[str | None]] = {name: [] for name in batches}

    async def collect(request: web.Request) -> web.Response:
        name = request.match_info["signal"]
        # A successful request also proves that the peer presented a trusted client identity.
        transport = request.transport
        assert transport is not None
        if request.secure:
            assert transport.get_extra_info("peercert")
        batches[name].append(await request.json())
        headers[name].append(request.headers.get("x-synthetic-telemetry"))
        return web.json_response({})

    model_app = web.Application()
    model_app.router.add_post("/v1/responses", fixture.handle)
    model_app.router.add_post("/v1/{signal}", collect)
    collector_app = web.Application()
    collector_app.router.add_post("/v1/{signal}", collect)
    with tempfile.TemporaryDirectory() as directory:
        tls, context = await mutual_tls_collector(Path(directory))
        if signal == "invalid_identity":
            Path(directory, "client.key").write_text("synthetic invalid private key")
        elif signal == "incomplete_identity":
            tls = telemetry.OtelTlsConfig(client_certificate=tls.client_certificate)
        elif signal == "untrusted_server":
            # The client leaf is not the collector's issuing CA.
            tls = telemetry.OtelTlsConfig(
                ca_certificate=tls.client_certificate,
                client_certificate=tls.client_certificate,
                client_private_key=tls.client_private_key,
            )
        collector = TestServer(collector_app, scheme="https")
        await collector.start_server(ssl=context)
        async with TestServer(model_app) as model, collector:
            selected = (signal,) if signal in ("logs", "metrics") else tuple(batches)
            settings: dict[str, Any] = {
                name: telemetry.OtlpHttp(
                    endpoint=str(
                        (model if signal == "mixed" and name == "metrics" else collector).make_url(
                            f"/v1/{name}"
                        )
                    ),
                    protocol="json",
                    headers={"x-synthetic-telemetry": "synthetic-mtls-header"},
                    tls=None if signal == "mixed" and name == "metrics" else tls,
                )
                for name in selected
            }
            if signal in ("invalid_identity", "incomplete_identity"):
                with pytest.raises(RuntimeError, match="Native telemetry configuration failed"):
                    await telemetry.configure(**settings)
                assert not any(batches.values())
                return
            await telemetry.configure(**settings, log_user_prompt=True, log_agent_responses=True)
            provider = NativeProvider(
                base_url=str(model.make_url("/v1")), api_key="synthetic-mtls-key"
            )
            response = assistant_output("SYNTHETIC_MTLS_ANSWER")
            response["item"]["phase"] = "final_answer"
            fixture.respond(response)
            agent = Agent(model="gpt-5.1-codex", provider=provider, instructions="Answer.")
            try:
                result = await agent.run("SYNTHETIC_MTLS_PROMPT")
                assert result.final_output == "SYNTHETIC_MTLS_ANSWER"
            finally:
                await provider.aclose()
                await telemetry.shutdown()
            if signal == "untrusted_server":
                assert not any(batches.values())
                return
            for name in selected:
                assert batches[name], name
                assert set(headers[name]) == {"synthetic-mtls-header"}
            if "logs" in selected:
                encoded = json.dumps(batches["logs"])
                assert "SYNTHETIC_MTLS_PROMPT" in encoded
                assert "SYNTHETIC_MTLS_ANSWER" in encoded
            if "metrics" in selected:
                metrics = [
                    metric
                    for batch in batches["metrics"]
                    for resource in batch["resourceMetrics"]
                    for scope in resource["scopeMetrics"]
                    for metric in scope["metrics"]
                ]
                assert (
                    sum(
                        int(point["count"])
                        for metric in metrics
                        if metric["name"] == "codex.turn.tool.call"
                        for point in metric["histogram"]["dataPoints"]
                    )
                    == 1
                )
            if "traces" in selected:
                assert any(
                    scope["spans"]
                    for batch in batches["traces"]
                    for resource in batch["resourceSpans"]
                    for scope in resource["scopeSpans"]
                )
