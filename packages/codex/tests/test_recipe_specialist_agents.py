import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from agents import Agent, ToolContext, TurnStoppedError, function_tool, telemetry
from agents.testing import ScriptedResponses, text, tool_call
from examples.codex.recipes.inspect_requests import function_declarations
from examples.codex.recipes.specialist_agents import (
    Application,
    Catalog,
    Summary,
    consult,
    run_example,
)


async def test_specialist_has_only_declared_tools_context_and_typed_return():
    outer, inner = await run_example()
    assert inner.tool_output("catalog") == "Synthetic catalog"
    assert json.loads(outer.tool_output("consult")) == {"title": "Synthetic catalog"}
    assert [tool["name"] for tool in function_declarations(inner.requests[0])] == ["read_catalog"]
    assert [tool["name"] for tool in function_declarations(outer.requests[0])] == ["consult"]


def test_specialist_propagates_explicit_trace_in_isolated_native_worker():
    env = os.environ.copy()
    env.pop("OPENAI_API_KEY", None)
    env.pop("CODEX_API_KEY", None)
    env["PYTHONPATH"] = os.pathsep.join(
        filter(None, (str(Path(__file__).resolve().parents[1]), env.get("PYTHONPATH")))
    )
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import asyncio; from tests.test_recipe_specialist_agents import "
            "specialist_trace_case; asyncio.run(specialist_trace_case())",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr


async def specialist_trace_case():
    class Recorder:
        def on_trace_event(self, event):
            pass

    await telemetry.configure(processors=[Recorder()])
    seen = []

    @function_tool
    def traced_catalog(context: ToolContext[Catalog]) -> str:
        """The nested specialist has been observing its explicit trace ancestry."""
        assert context.trace_context is not None
        seen.append(context.trace_context.traceparent.split("-")[1])
        return context.context.title

    try:
        async with (
            ScriptedResponses(
                [tool_call("consult", {"question": "Read."}, call_id="consult")],
                [text("Done.")],
            ) as outer,
            ScriptedResponses(
                [tool_call("traced_catalog", {}, call_id="catalog")],
                [text('{"title":"Selected"}')],
            ) as inner,
        ):
            specialist = Agent(
                instructions="Read.",
                tools=[traced_catalog],
                provider=inner.provider,
                output_type=Summary,
            )
            await Agent(instructions="Consult.", tools=[consult], provider=outer.provider).run(
                "Consult.",
                context=Application(specialist, Catalog("Selected")),
                parent_trace={
                    "traceparent": "00-11111111111111111111111111111111-2222222222222222-01"
                },
            )
            assert seen == ["1" * 32]
            assert json.loads(outer.tool_output("consult")) == {"title": "Selected"}
    finally:
        await telemetry.shutdown()


@pytest.mark.parametrize("failure", [False, True])
async def test_nested_failure_and_cancellation_join_owned_cleanup(failure):
    entered, cleaning, release, cleaned = (asyncio.Event() for _ in range(4))

    @function_tool
    async def nested(context: ToolContext[Catalog]) -> str:
        assert context.context == Catalog("limited")
        entered.set()
        try:
            if failure:
                raise ValueError("Specialist failed")
            await asyncio.Future()
        finally:
            cleaning.set()
            if not failure:
                await release.wait()
            cleaned.set()
        return "unreachable"

    async with (
        ScriptedResponses(
            [tool_call("consult", {"question": "Read."}, call_id="outer")], [text("Continued.")]
        ) as outer,
        ScriptedResponses([tool_call("nested", {}, call_id="inner")]) as inner,
    ):
        specialist = Agent(
            instructions="Read.", tools=[nested], provider=inner.provider, output_type=Summary
        )
        agent = Agent(instructions="Consult.", tools=[consult], provider=outer.provider)
        async with agent.session(context=Application(specialist, Catalog("limited"))) as session:
            turn = await session.submit("Consult.")
            await asyncio.wait_for(entered.wait(), 10)
            if failure:
                with pytest.raises(ValueError, match="Specialist failed"):
                    await turn.get_result()
            else:
                stopping = asyncio.create_task(turn.cancel())
                await asyncio.wait_for(cleaning.wait(), 10)
                try:
                    assert not stopping.done() and not cleaned.is_set()
                finally:
                    release.set()
                await asyncio.wait_for(stopping, 10)
                with pytest.raises(TurnStoppedError):
                    await turn.get_result()
            assert cleaned.is_set()
            assert outer.tool_output("outer") is None
            assert inner.tool_output("inner") is None
            continued = await session.submit("Continue.")
            assert (await continued.get_result()).final_output == "Continued."
