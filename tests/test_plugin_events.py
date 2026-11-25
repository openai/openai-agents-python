import asyncio
from typing import Any

import pytest

from agents import Agent, RunConfig, Runner, RunResult, RunResultStreaming, function_tool
from agents.plugins import Plugin, PluginHooks, ToolProgress
from agents.stream_events import ApprovedText, RunCompleted
from agents.testing import ScriptedModel, assistant_message, function_call
from agents.tool_context import ToolContext

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("reader", ["display", "passive", "raw", "ordinary", "withhold"])
async def test_only_the_apps_approved_reader_notifies_plugins(reader: str, caplog: Any):
    seen: list[Any] = []

    async def broken(event: ApprovedText):
        raise RuntimeError("private observer failure")

    agent = Agent(
        name="root",
        model=ScriptedModel([[assistant_message("answer")]]),
        plugins=[
            Plugin(name="broken", hooks=PluginHooks(on_preview=broken)),
            Plugin(
                name="ui",
                hooks=PluginHooks(on_preview=seen.append, on_stream_completed=seen.append),
            ),
        ],
    )
    if reader == "ordinary":
        assert (await Runner.run(agent, "start")).final_output == "answer"
        assert not seen
        return
    run = Runner.run_streamed(agent, "start")
    if reader == "raw":
        async for _ in run.stream_events():
            pass
        assert not seen
        return

    def prefix(text: str):
        return text[:1]

    if reader == "passive":
        received = [event async for event in run.subscribe_approved_output(prefix)]
        async for _ in run.stream_events():
            pass
    else:
        async with run.stream_approved_output(None if reader == "withhold" else prefix) as events:
            received = [event async for event in events]
    assert seen == received and run.final_output == "answer"
    if reader == "withhold":
        assert [type(event) for event in seen] == [RunCompleted]
    else:
        assert [type(event) for event in seen] == [ApprovedText, RunCompleted]
        assert seen[0].text == "a"
        assert "plugin_observer_failed on_preview" in caplog.text
        assert "private observer failure" not in caplog.text


async def test_approval_pause_has_no_completion():
    @function_tool(needs_approval=True)
    def pause():
        return "done"

    seen: list[RunCompleted] = []
    agent = Agent(
        name="root",
        tools=[pause],
        model=ScriptedModel([[function_call("pause", {}, call_id="p")]]),
        plugins=[Plugin(name="ui", hooks=PluginHooks(on_stream_completed=seen.append))],
    )
    run = Runner.run_streamed(agent, "start")
    async with run.stream_approved_output() as reader:
        assert not [event async for event in reader]
    assert run.interruptions and not seen


@pytest.mark.parametrize("stream", [False, True])
async def test_active_call_progress_is_private_and_coalesces(stream: bool):
    entered, release = asyncio.Event(), asyncio.Event()
    seen: list[ToolProgress] = []
    saved: list[ToolContext[Any]] = []

    async def observe(event: ToolProgress):
        seen.append(event)
        entered.set()
        await release.wait()

    @function_tool
    async def fetch(ctx: ToolContext[Any]) -> str:
        saved.append(ctx)
        first = asyncio.create_task(ctx.report_progress("private-1"))
        try:
            await asyncio.wait_for(entered.wait(), 3)
            await ctx.report_progress("private-2")
            await ctx.report_progress("private-3")
        finally:
            release.set()
            await first
        return "data"

    agent = Agent(
        name="root",
        model=ScriptedModel([[function_call("fetch", {}, call_id="f")], [assistant_message("ok")]]),
        plugins=[
            Plugin(name="notes", tools=[fetch]),
            Plugin(name="ui", hooks=PluginHooks(on_tool_progress=observe)),
        ],
    )
    run: RunResult | RunResultStreaming
    if stream:
        run = Runner.run_streamed(agent, "start")
        async with run.stream_approved_output() as reader:
            (receipt,) = [e async for e in reader]
        submission = receipt.submission_id
    else:
        run = await Runner.run(agent, "start")
        submission = seen[0].submission_id
    await saved[0].report_progress("late")
    assert run.final_output == "ok"
    assert "private-" not in str(run.to_input_list())
    assert seen == [
        ToolProgress(submission, "notes", "f", 1, 0, "private-1"),
        ToolProgress(submission, "notes", "f", 3, 1, "private-3"),
    ]


async def test_cancellation_stops_progress_and_receipt():
    seen: list[Any] = []

    @function_tool
    async def cancel(ctx: ToolContext[Any]):
        await ctx.report_progress("before")
        run.cancel("after_turn")
        await ctx.report_progress("after")
        return "done"

    agent = Agent(
        name="root",
        tools=[cancel],
        model=ScriptedModel([[function_call("cancel", {}, call_id="c")]]),
        plugins=[
            Plugin(
                name="ui",
                hooks=PluginHooks(on_tool_progress=seen.append, on_stream_completed=seen.append),
            )
        ],
    )
    run = Runner.run_streamed(agent, "start")
    async with run.stream_approved_output() as reader:
        assert not [e async for e in reader]
    assert len(seen) == 1 and seen[0].message == "before" and seen[0].owner is None


async def test_handoff_only_notifies_the_current_agent():
    before: list[Any] = []
    after: list[Any] = []

    def observer(events: list[Any]) -> Plugin[Any]:
        return Plugin(
            name="ui",
            hooks=PluginHooks(on_preview=events.append, on_stream_completed=events.append),
        )

    child = Agent(
        name="child",
        plugins=[observer(after)],
        model=ScriptedModel([[assistant_message("child answer")]]),
    )
    root = Agent(
        name="root",
        plugins=[observer(before)],
        handoffs=[child],
        model=ScriptedModel([[function_call("transfer_to_child", {}, call_id="h")]]),
    )
    run = Runner.run_streamed(root, "start")
    async with run.stream_approved_output(lambda text: text) as reader:
        received = [e async for e in reader]
    assert not before
    assert received == after
    assert [type(e) for e in after] == [ApprovedText, RunCompleted]
    assert after[0].text == "child answer"


async def test_text_redaction_suppresses_progress_and_previews():
    seen: list[Any] = []

    @function_tool
    async def fetch(ctx: ToolContext[Any]):
        await ctx.report_progress("private")
        return "ok"

    agent = Agent(
        name="root",
        tools=[fetch],
        model=ScriptedModel(
            [[function_call("fetch", {}, call_id="f")], [assistant_message("private")]]
        ),
        plugins=[
            Plugin(
                name="ui",
                hooks=PluginHooks(
                    on_tool_progress=seen.append,
                    on_preview=seen.append,
                    on_stream_completed=seen.append,
                ),
            )
        ],
    )
    run = Runner.run_streamed(
        agent, "start", run_config=RunConfig(redact_text=lambda text: text.replace("private", "X"))
    )
    async with run.stream_approved_output(lambda text: text) as reader:
        received = [e async for e in reader]
    assert run.final_output == "X"
    assert received == seen and [type(e) for e in seen] == [RunCompleted]


async def test_slow_observer_cannot_republish_an_expired_preview():
    seen: list[str] = []

    async def first(event: ApprovedText):
        seen.append(event.text)
        assert run.run_loop_task is not None
        await asyncio.wait_for(asyncio.shield(run.run_loop_task), 3)

    def later(event: ApprovedText):
        seen.append("stale")

    agent = Agent(
        name="root",
        model=ScriptedModel([[assistant_message("draft")]]),
        plugins=[
            Plugin(name="first", hooks=PluginHooks(on_preview=first)),
            Plugin(name="later", hooks=PluginHooks(on_preview=later)),
        ],
    )
    run = Runner.run_streamed(agent, "start")
    async with run.stream_approved_output(lambda text: text) as reader:
        published = [type(event) async for event in reader]
    assert seen == ["draft"] and published == [RunCompleted]


async def test_preview_uses_the_runs_original_plugin_selection():
    allowed: list[str] = []
    unexpected: list[ApprovedText] = []

    def observe(event: ApprovedText):
        allowed.append(event.text)
        agent.plugins = (original,)

    original: Plugin[Any] = Plugin(name="ui", hooks=PluginHooks(on_preview=observe))
    replacement: Plugin[Any] = Plugin(name="ui", hooks=PluginHooks(on_preview=unexpected.append))
    agent = Agent(
        name="root", model=ScriptedModel([[assistant_message("draft")]]), plugins=[original]
    )

    def approve(text: str):
        agent.plugins = (replacement,)
        return text

    run = Runner.run_streamed(agent, "start")
    async with run.stream_approved_output(approve) as reader:
        async for _ in reader:
            pass
    assert allowed == ["draft"] and not unexpected
