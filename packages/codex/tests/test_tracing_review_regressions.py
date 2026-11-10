"""Public/native regressions for invalid payloads, cache ownership and fixture retries."""

from __future__ import annotations

import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agents import Agent, telemetry
from agents.testing import ScriptedResponses, text
from tests.test_native_trace_exporter import (
    assert_sanitized_failure,
    collector,
    completed_event,
    native_trace_child_env,
)


async def test_invalid_outcome_drops_private_serializer_frames():
    scope = telemetry.span("private-invalid-outcome")
    secret = "synthetic-private-invalid-outcome"
    with pytest.raises(TypeError) as failure:
        scope.set_outcome(
            status="ok", data={"private": secret, "invalid": datetime.now(timezone.utc)}
        )
    assert_sanitized_failure(failure.value, secret)
    assert scope._outcome is None


async def test_active_descendant_keeps_root_descriptor_during_churn():
    root = {
        **completed_event(),
        "parent_span_id": None,
        "lifecycle": "start",
        "name": "root-workflow",
        "attributes": {"sdk.group_id": "retained-group"},
    }
    child = {
        **completed_event(),
        "lifecycle": "start",
        "span_id": "d" * 16,
        "parent_span_id": root["span_id"],
    }
    with collector() as (endpoint, requests):
        exporter = telemetry.OpenAITraceExporter(api_key="synthetic", endpoint=endpoint)
        await exporter.on_trace_events((root, child, {**root, "lifecycle": "end"}))
        await exporter.on_trace_events(
            tuple(
                {**root, "trace_id": f"{index:032x}", "lifecycle": "end", "name": "completed-churn"}
                for index in range(1100)
            )
        )
        await exporter.on_trace_event({**child, "lifecycle": "end"})
        descriptors = [
            row
            for request in requests
            for row in request["payload"]["data"]
            if row["object"] == "trace" and row["id"] == "trace_" + root["trace_id"]
        ]
        assert descriptors and all(row["workflow_name"] == "root-workflow" for row in descriptors)
        assert len(exporter._traces) <= 1024


async def test_full_active_capacity_rejects_admission_without_fabricating_child_roots():
    root = {**completed_event(), "parent_span_id": None, "lifecycle": "start"}
    with collector() as (endpoint, requests):
        exporter = telemetry.OpenAITraceExporter(api_key="synthetic", endpoint=endpoint)
        roots = tuple({**root, "trace_id": f"{index:032x}"} for index in range(1024))
        await exporter.on_trace_events(roots)
        with pytest.raises(RuntimeError):
            await exporter.on_trace_event({**root, "trace_id": "f" * 32})
        assert len(exporter._traces) == 1024
        with pytest.raises(RuntimeError):
            await exporter.on_trace_event({**completed_event(), "trace_id": "f" * 32})
        assert len(requests) == 1
        await exporter.on_trace_event({**roots[0], "lifecycle": "end"})
        await exporter.on_trace_event({**root, "trace_id": "f" * 32})
        assert len(exporter._traces) == 1024


async def test_native_callback_failure_cannot_consume_the_next_script():
    called = []
    failure = AssertionError("synthetic-private-callback-assertion")

    def broken(request):
        called.append("broken")
        raise failure

    def following(request):
        called.append("following")
        return [text("must remain queued")]

    from tests.test_testing import request

    peer = ScriptedResponses(broken, following)
    with pytest.raises(AssertionError) as raised:
        async with peer:
            agent = Agent(instructions="Respond", model="gpt-5.5", provider=peer.provider)
            try:
                await agent.run("synthetic input")
            except Exception:
                pass
            assert len(peer.requests) == 1  # Native transport did not retry the terminal failure.
            status, _, body = await request(peer.provider, "POST", "/v1/responses", {"input": []})
            assert status == 400
            assert failure.args[0] not in body
    assert raised.value is failure
    assert called == ["broken"]
    assert len(peer.requests) == 2


async def native_churn():
    with collector() as (endpoint, requests):
        exporter = telemetry.OpenAITraceExporter(api_key="synthetic", endpoint=endpoint)
        await telemetry.configure(processors=[exporter])
        with telemetry.trace(
            "retained-root",
            options={"metadata": {"tenant": "original"}, "group_id": "original-group"},
        ):
            for index in range(1100):
                with telemetry.trace(f"completed-{index}", options={"detached": True}):
                    pass
                if index % 32 == 31:
                    intermediate = await telemetry.force_flush()
                    assert intermediate.status == "success", intermediate
            with telemetry.span("retained-child"):
                pass
        result = await telemetry.force_flush()
        assert result.status == "success", result
        records = [row for request in requests for row in request["payload"]["data"]]
        root = next(row for row in records if row.get("workflow_name") == "retained-root")
        same = [row for row in records if row["object"] == "trace" and row["id"] == root["id"]]
        assert all(
            row["workflow_name"] == "retained-root"
            and row["group_id"] == "original-group"
            and row["metadata"]["tenant"] == "original"
            for row in same
        )
        assert len(exporter._traces) <= 1024
        await telemetry.shutdown()
        print("PASS native active-root churn")


@pytest.mark.serial
def test_native_active_root_survives_completed_trace_churn():
    script = (
        "import runpy,asyncio; module=runpy.run_path("
        + repr(str(Path(__file__).resolve()))
        + ");asyncio.run(module['native_churn']())"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        env=native_trace_child_env(),
        text=True,
        capture_output=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASS native active-root churn" in result.stdout
