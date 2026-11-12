"""Retained native contexts bind ancestry and privacy without lifecycle events."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest

from agents import Agent, function_tool, telemetry
from agents.testing import ScriptedResponses, text, tool_call


@pytest.mark.parametrize("case", ["ancestry", "restoration", "privacy", "portable"])
def test_retained_context_binding(case, tmp_path):
    env = dict(os.environ, CODEX_HOME=str(tmp_path / "home"))
    env["PYTHONPATH"] = os.pathsep.join(
        filter(None, (str(Path(__file__).resolve().parents[1]), env.get("PYTHONPATH")))
    )
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import asyncio, sys; "
            "from tests.test_native_trace_context_binding import native_case; "
            "asyncio.run(asyncio.wait_for(native_case(sys.argv[1]), 40))",
            case,
        ],
        env=env,
        text=True,
        capture_output=True,
        timeout=50,
    )
    assert result.returncode == 0, result.stdout + result.stderr


async def native_case(case):
    events = []

    class Recorder:
        def on_trace_event(self, event):
            events.append(event)

    await telemetry.configure(processors=[Recorder()])
    try:
        with telemetry.trace(
            "binding.captured",
            options={
                "include_sensitive_data": True,
                "metadata": {"owner": "captured"},
                "export_credentials": telemetry.TraceExportCredentials(
                    headers={"authorization": "Bearer synthetic-binding-owner"}
                ),
            },
        ) as original:
            saved = original.context
        await telemetry.force_flush()
        baseline = list(events)
        with telemetry.use_context(saved) as bound:
            assert bound.trace_id == saved.trace_id
            assert bound.span_id == saved.span_id
            assert bound.trace_metadata == {"owner": "captured"}
        await telemetry.force_flush()
        assert events == baseline
        assert telemetry.current_context() is None

        if case == "ancestry":
            seen = []

            @function_tool
            def inspect_owner() -> str:
                context = telemetry.current_context()
                assert context is not None
                seen.append((context.trace_id, context.trace_metadata))
                return "owned"

            with telemetry.use_context(saved):
                with telemetry.span("binding.child") as child:
                    assert child.context.parent_span_id == saved.span_id
                    assert child.context.trace_id == saved.trace_id
                async with ScriptedResponses(
                    [tool_call("inspect_owner", {}, call_id="inspect-owner")],
                    [text("done")],
                ) as peer:
                    result = await Agent(
                        instructions="Answer.",
                        model="gpt-5.5",
                        provider=peer.provider,
                        tools=[inspect_owner],
                    ).run("Inspect the owner.")
                    assert result.final_output == "done"
            assert seen == [(saved.trace_id, {"owner": "captured"})]
            await telemetry.force_flush()
            child_events = [event for event in events if event["name"] == "binding.child"]
            assert {event["lifecycle"] for event in child_events} == {"start", "end"}
            assert all(event["parent_span_id"] == saved.span_id for event in child_events)
            assert all(event.get("credential_required") for event in child_events)

        elif case == "restoration":
            gate = asyncio.Event()

            async def copied_task():
                await gate.wait()
                current = telemetry.current_context()
                assert current is not None and current.span_id == saved.span_id
                with telemetry.span("binding.copied") as child:
                    assert child.context.parent_span_id == saved.span_id

            with telemetry.use_context(saved):
                task = asyncio.create_task(copied_task())
            with telemetry.trace("binding.other") as other:
                previous = telemetry.current_context()

                async def independent(context):
                    with telemetry.use_context(context):
                        await asyncio.sleep(0)
                        current = telemetry.current_context()
                        assert current is not None and current.span_id == context.span_id
                    assert telemetry.current_context() is previous

                await asyncio.gather(independent(saved), independent(other.context))
                with pytest.raises(RuntimeError, match="body failure"):
                    with telemetry.use_context(saved):
                        with telemetry.use_context(other.context):
                            raise RuntimeError("body failure")
                assert telemetry.current_context() is previous
                gate.set()
                await task
                assert telemetry.current_context() is previous
            assert telemetry.current_context() is None

        elif case == "privacy":
            with telemetry.trace("binding.disabled", options={"disabled": True}):
                with telemetry.use_context(saved) as disabled:
                    assert disabled.disabled
                    with telemetry.span("binding.must-not-export"):
                        pass
            with telemetry.trace("binding.metadata", options={"include_sensitive_data": False}):
                with telemetry.use_context(saved) as restricted:
                    assert not restricted.include_sensitive_data
                    with telemetry.span("binding.restricted") as child:
                        child.set_outcome(status="ok", data={"private": "BINDING_PRIVATE_CANARY"})
            with telemetry.trace("binding.captured-metadata") as metadata_only:
                limited = metadata_only.context
            with telemetry.use_context(limited):
                with telemetry.span(
                    "binding.cannot-relax", options={"include_sensitive_data": True}
                ) as child:
                    assert not child.context.include_sensitive_data
            telemetry.set_tracing_disabled(True)
            try:
                with telemetry.use_context(saved) as disabled:
                    assert disabled.disabled
            finally:
                telemetry.set_tracing_disabled(False)
            telemetry.set_sensitive_data_logging_enabled(False)
            try:
                with telemetry.use_context(saved) as restricted:
                    assert not restricted.include_sensitive_data
            finally:
                telemetry.set_sensitive_data_logging_enabled(True)
            assert not saved.disabled and saved.include_sensitive_data
            await telemetry.force_flush()
            assert not any(event["name"] == "binding.must-not-export" for event in events)
            assert any(event["name"] == "binding.restricted" for event in events)
            assert "BINDING_PRIVATE_CANARY" not in repr(events)
            with telemetry.use_context(saved) as unchanged:
                assert unchanged.include_sensitive_data and not unchanged.disabled

        else:
            carrier = telemetry.TraceContext(traceparent=f"00-{saved.trace_id}-{saved.span_id}-01")
            for value in (carrier, carrier.to_headers(), saved.trace_id):
                with pytest.raises(TypeError, match="retained SpanContext"):
                    with telemetry.use_context(value):
                        pytest.fail("Portable identity acquired a local context owner")
            await telemetry.force_flush()
            assert events == baseline
    finally:
        await telemetry.shutdown()
