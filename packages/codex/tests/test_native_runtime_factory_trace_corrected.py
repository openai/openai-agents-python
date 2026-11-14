import runpy
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from agents import telemetry
from tests.test_native_configured_agent_tool import peer
from tests.test_native_trace_exporter import native_trace_child_env
from tests.test_tracing_native import Recorder


def test_runtime_factory_keeps_selected_model_and_agent_span_owner():
    child = subprocess.run(
        [
            sys.executable,
            "-c",
            "import asyncio,runpy,sys; asyncio.run(runpy.run_path(sys.argv[1])['run_case']())",
            __file__,
        ],
        env=native_trace_child_env(),
        capture_output=True,
        text=True,
        timeout=40,
    )
    assert child.returncode == 0, child.stdout + child.stderr


async def run_case():
    models = []

    class Policy(Recorder):
        def should_create_model_span(self, model):
            assert telemetry.current_context() is not None
            models.append(model)
            return True

    policy = Policy()
    await telemetry.configure(processors=[policy])
    case = runpy.run_path(str(Path(__file__).with_name("test_native_runtime_factory_final.py")))
    try:
        with (
            pytest.MonkeyPatch.context() as monkeypatch,
            tempfile.TemporaryDirectory() as temporary,
            telemetry.trace("runtime factory ownership") as workflow,
        ):
            trace_id = workflow.context.trace_id
            async with peer() as responses:
                await case["test_runtime_factory_keeps_native_selection_and_provider_authority"](
                    responses, "fresh", monkeypatch, Path(temporary)
                )
        assert models == ["gpt-5.5"] * 3
        assert (await telemetry.force_flush()).status == "success"
        starts = {
            event["span_id"]: event
            for event in policy.events
            if event["trace_id"] == trace_id and event["lifecycle"] == "start"
        }

        def owner(event):
            while event["attributes"].get("sdk.span.kind") != "agent":
                event = starts[event["parent_span_id"]]
            return event["name"]

        model_spans = [
            event
            for event in starts.values()
            if event["attributes"].get("sdk.span.kind") == "model"
        ]
        assert [owner(event) for event in model_spans] == [
            "Source",
            "Runtime fresh",
            "Runtime fresh",
        ]
        lookup = next(event for event in starts.values() if event["name"] == "fresh_lookup")
        assert owner(lookup) == "Runtime fresh"
    finally:
        await telemetry.shutdown()
