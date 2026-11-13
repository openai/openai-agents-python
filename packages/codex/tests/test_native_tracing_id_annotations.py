"""ID policies and bounded completion data use real native scope ownership."""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Callable
from typing import get_type_hints

import pytest

from agents import Agent, function_tool, telemetry
from agents.testing import ScriptedResponses, text, tool_call
from tests.test_native_trace_exporter import collector, native_trace_child_env


def test_public_tracing_id_generator_type_hints():
    callback = Callable[[], str] | None
    assert get_type_hints(telemetry.TracingIdGenerator) == {
        "generate_trace_id": callback,
        "generate_span_id": callback,
    }


@pytest.mark.parametrize(
    "case", ["ids", "automatic", "collision", "annotations", "restrictions", "invalid"]
)
def test_native_id_and_annotation_policy(case):
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import asyncio,sys; "
            "from tests.test_native_tracing_id_annotations import native_case; "
            "asyncio.run(native_case(sys.argv[1]))",
            case,
        ],
        env=native_trace_child_env(),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"PASS {case}" in result.stdout


async def native_case(case):
    events = []

    class Recorder:
        def on_trace_event(self, event):
            events.append(event)
            if case == "restrictions" and event["name"] == "metadata-only":
                with telemetry.trace("processor-suppressed") as suppressed:
                    assert suppressed.context.disabled is True

    with collector() as (endpoint, received):
        await telemetry.configure(
            processors=[
                Recorder(),
                telemetry.OpenAITraceExporter(
                    api_key="synthetic-id-annotation-key",
                    endpoint=endpoint,
                ),
            ]
        )
        try:
            if case == "ids":
                count = 0
                later_count = 0
                callback_seen = False
                supplied = set()

                class FalsyTracingIdGenerator(telemetry.TracingIdGenerator):
                    def __bool__(self):
                        return False

                def span_b():
                    nonlocal later_count
                    later_count += 1
                    value = f"{later_count + 0xB0:016x}"
                    supplied.add(value)
                    return value

                def span_a():
                    nonlocal count
                    count += 1
                    return f"{count:016x}"

                def trace_a():
                    telemetry.set_tracing_id_generator(
                        telemetry.TracingIdGenerator(
                            generate_trace_id=lambda: "2" * 32,
                            generate_span_id=span_b,
                        )
                    )
                    return "1" * 32

                telemetry.set_tracing_id_generator(
                    FalsyTracingIdGenerator(
                        generate_trace_id=trace_a,
                        generate_span_id=span_a,
                    )
                )
                with telemetry.trace("generated-root") as root:
                    assert root.context.trace_id == "1" * 32
                    assert root.context.span_id == "0000000000000001"
                    with telemetry.span("generated-child") as child:
                        assert child.context.trace_id == root.context.trace_id
                        assert child.context.parent_span_id == root.context.span_id
                        assert child.context.span_id == "00000000000000b1"
                    with telemetry.span(
                        "explicit-child",
                        options=telemetry.TraceOptions(
                            span_id="c" * 16,
                        ),
                    ) as explicit:
                        assert explicit.context.span_id == "c" * 16

                    @function_tool
                    def probe() -> str:
                        nonlocal callback_seen
                        context = telemetry.current_context()
                        assert context is not None
                        assert context.trace_id == root.context.trace_id
                        assert context.span_id in supplied
                        callback_seen = True
                        return "native-policy-answer"

                    async with ScriptedResponses(
                        [tool_call("probe", {}, call_id="policy-tool")],
                        [text("native-policy-answer")],
                    ) as peer:
                        agent = Agent(
                            instructions="Use probe.",
                            model="fixture",
                            provider=peer.provider,
                            tools=[probe],
                        )
                        assert (await agent.run("input")).final_output == "native-policy-answer"
                        assert callback_seen
                        peer.assert_exhausted()
                telemetry.set_tracing_id_generator()
                with telemetry.trace("native-default") as default:
                    assert default.context.trace_id != "2" * 32
                    assert default.context.span_id != "b" * 16
                assert count == 1
            elif case == "automatic":
                count = 0

                def generated_span():
                    nonlocal count
                    count += 1
                    return f"{count:016x}"

                telemetry.set_tracing_id_generator(
                    telemetry.TracingIdGenerator(
                        generate_trace_id=lambda: "7" * 32,
                        generate_span_id=generated_span,
                    )
                )
                async with ScriptedResponses([text("automatic-answer")]) as peer:
                    agent = Agent(instructions="Answer.", model="fixture", provider=peer.provider)
                    result = await agent.run("input", trace_options=telemetry.TraceOptions())
                    assert result.final_output == "automatic-answer"
                    peer.assert_exhausted()
                assert count >= 1
            elif case == "collision":
                options = telemetry.TraceOptions(
                    trace_id="3" * 32,
                    span_id="a" * 16,
                    include_sensitive_data=True,
                    export_credentials=telemetry.TraceExportCredentials(
                        headers={
                            "Authorization": "Bearer synthetic-private-owner",
                        }
                    ),
                )
                with telemetry.trace("identity-owner", options=options) as root:
                    with pytest.raises(RuntimeError, match="identity is already active"):
                        with telemetry.trace(
                            "identity-intruder",
                            options=telemetry.TraceOptions(
                                detached=True,
                                trace_id="3" * 32,
                                span_id="a" * 16,
                            ),
                        ):
                            pytest.fail("Duplicate identity admitted")
                    assert telemetry.current_context() == root.context
                    with telemetry.span(
                        "identity-child",
                        options=telemetry.TraceOptions(
                            span_id="b" * 16,
                        ),
                    ) as child:
                        assert child.context.parent_span_id == root.context.span_id
            elif case == "annotations":
                payload = {"large": "é" * 16000, "nested": {"numbers": list(range(64))}}
                escaped_payload = {"escaped": "\\" * 16_375}
                with telemetry.trace(
                    "large-allowed",
                    options=telemetry.TraceOptions(
                        include_sensitive_data=True,
                    ),
                ) as root:
                    root.set_outcome(status="ok", data=payload)
                    with telemetry.span("escaped-annotation") as escaped:
                        escaped.set_outcome(status="ok", data=escaped_payload)
                with telemetry.trace("large-hidden") as hidden:
                    hidden.set_outcome(status="ok", data=payload)
                with pytest.raises(RuntimeError, match="Native trace span end failed"):
                    with telemetry.trace("over-bound") as over:
                        over.set_outcome(status="ok", data={"large": "x" * 65537})
                assert over._outcome is None
                with pytest.raises(RuntimeError, match="Native trace span end failed") as malformed:
                    with telemetry.trace("malformed-unicode") as invalid:
                        invalid.set_outcome(
                            status="ok", data={"private": "synthetic-private-annotation" + "\ud800"}
                        )
                assert "synthetic-private-annotation" not in str(malformed.value)
                assert invalid._outcome is None
                with pytest.raises(RuntimeError, match="128 KiB"):
                    with telemetry.trace(
                        "metadata-still-bounded",
                        options=telemetry.TraceOptions(metadata={"value": "é" * 65537}),
                    ):
                        pass
            elif case == "restrictions":
                calls = []

                def generated():
                    calls.append(True)
                    return "d" * 16

                telemetry.set_tracing_id_generator(
                    telemetry.TracingIdGenerator(
                        generate_span_id=generated,
                    )
                )
                with telemetry.trace("disabled", options=telemetry.TraceOptions(disabled=True)):
                    with telemetry.span(
                        "cannot-relax", options=telemetry.TraceOptions(disabled=False)
                    ):
                        pass
                assert calls == []
                with telemetry.trace("metadata-only") as root:
                    assert root.context.span_id == "d" * 16
                    assert root.context.include_sensitive_data is False
                telemetry.set_tracing_id_generator()
            elif case == "invalid":

                async def asynchronous():
                    return "e" * 16

                with pytest.raises(TypeError, match="synchronous"):
                    telemetry.set_tracing_id_generator(
                        telemetry.TracingIdGenerator(
                            generate_span_id=asynchronous,
                        )
                    )
                telemetry.set_tracing_id_generator(
                    telemetry.TracingIdGenerator(
                        generate_span_id=lambda: "0" * 16,
                    )
                )
                with pytest.raises(ValueError, match="invalid identifier"):
                    with telemetry.trace("invalid-id"):
                        pass
                assert telemetry.current_context() is None
                telemetry.set_tracing_id_generator()
                with telemetry.trace("recovered"):
                    pass
            assert (await telemetry.force_flush(timeout_ms=3000)).status == "success"
            if case == "restrictions":
                assert len(calls) == 1
            if case == "ids":
                assert any(event["span_id"] == "00000000000000b1" for event in events)
            elif case == "automatic":
                assert any(
                    event["trace_id"] == "7" * 32 and event["span_id"] == "0000000000000001"
                    for event in events
                )
            elif case == "collision":
                assert not any(event["name"] == "identity-intruder" for event in events)
            elif case == "annotations":
                allowed = next(
                    event
                    for event in events
                    if event["name"] == "large-allowed" and event["lifecycle"] == "end"
                )
                hidden = next(
                    event
                    for event in events
                    if event["name"] == "large-hidden" and event["lifecycle"] == "end"
                )
                escaped = next(
                    event
                    for event in events
                    if event["name"] == "escaped-annotation" and event["lifecycle"] == "end"
                )
                assert json.loads(allowed["attributes"]["sdk.outcome.data"]) == payload
                assert json.loads(escaped["attributes"]["sdk.outcome.data"]) == escaped_payload
                assert "sdk.outcome.data" not in hidden["attributes"]
                assert payload["large"] in json.dumps(received, ensure_ascii=False)
        finally:
            telemetry.set_tracing_id_generator()
            await telemetry.shutdown(timeout_ms=3000)
    print(f"PASS {case}")
