"""Native span policy receives the model selected for the current logical request."""

from __future__ import annotations

import asyncio
import subprocess
import sys

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from agents import Agent, NativeProvider, telemetry
from tests.native_fixtures import ResponsesFixture, assistant_output
from tests.test_native_trace_exporter import native_trace_child_env
from tests.test_tracing_native import Recorder


@pytest.mark.parametrize("case", ["selected", "generic", "invalid", "disabled"])
def test_native_model_span_selection(case):
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import asyncio,runpy,sys; "
            "asyncio.run(runpy.run_path(sys.argv[1])['run_case'](sys.argv[2]))",
            __file__,
            case,
        ],
        env=native_trace_child_env(),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


async def run_case(case):
    responses = ResponsesFixture()
    app = web.Application()
    app.router.add_post("/v1/responses", responses.handle)
    entered = []
    active_loop = asyncio.get_running_loop()

    class Policy(Recorder):
        def should_create_model_span(self, model):
            assert asyncio.get_running_loop() is active_loop
            assert telemetry.current_context() is not None
            entered.append(model)
            with telemetry.span("nested policy") as nested:
                assert nested.context.disabled
            if case == "invalid":
                return "invalid"
            return model == "gpt-5.5"

    class GenericPolicy(Recorder):
        def should_create_span(self, name, kind):
            return kind != "model"

    policy = Policy()
    processors = [GenericPolicy(), policy] if case == "generic" else [policy]
    await telemetry.configure(processors=processors)
    responses.respond(assistant_output("First."))
    responses.respond(assistant_output("Second."))
    try:
        async with (
            TestServer(app) as server,
            NativeProvider(
                base_url=str(server.make_url("/v1")),
                api_key="synthetic-model-trace-policy",
                supports_websockets=False,
            ) as provider,
        ):
            agent = Agent(model="gpt-5.5", instructions="Answer.", provider=provider)
            with telemetry.trace(
                "model selection", options=telemetry.TraceOptions(disabled=case == "disabled")
            ) as parent:
                trace_id = parent.context.trace_id
                if case == "invalid":
                    with pytest.raises(TypeError, match="creation policies must return bool"):
                        await agent.run("Begin.")
                else:
                    assert (await agent.run("Begin.")).final_output == "First."
                    assert (await agent.run("Again.", model="gpt-4.1")).final_output == "Second."
        assert (await telemetry.force_flush()).status == "success"
        names = [
            row["name"]
            for row in policy.events
            if row["trace_id"] == trace_id and row["lifecycle"] == "start"
        ]
        assert "nested policy" not in names
        if case == "invalid":
            assert entered == ["gpt-5.5"]
            assert not responses.requests
        else:
            assert [request["model"] for request in responses.requests] == ["gpt-5.5", "gpt-4.1"]
            assert entered == (["gpt-5.5", "gpt-4.1"] if case == "selected" else [])
        assert names.count("response") == (1 if case == "selected" else 0)
    finally:
        await telemetry.shutdown()
