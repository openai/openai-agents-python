import asyncio
import json
from contextlib import closing
from typing import Any

import pytest

from agents import (
    Agent,
    AgentsException,
    RunConfig,
    Runner,
    SQLiteSession,
    ToolExecutionConfig,
    UserError,
)
from agents.mcp import MCPToolCustomDataContext
from agents.testing import ScriptedModel, assistant_message, function_call
from agents.tool_context import ToolContext
from examples.mcp.bounded_tool_output import main as recipe
from tests.testing_processor import fetch_ordered_spans

OWNER = "sample-user"


@pytest.mark.asyncio
async def test_sdk_discovery_and_function_results_before_publication():
    observed = {}

    def observe(result: MCPToolCustomDataContext) -> None:
        assert isinstance(result.run_context, ToolContext)
        observed[result.run_context.tool_call_id] = (
            result.run_context.context,
            result.server_name,
            result.tool_name,
            result.tool_output,
            result.structured_content,
            result.result_meta,
            result.is_error,
        )

    name = "mcp_Inspection_reports__read_report"
    model = ScriptedModel(
        [
            [
                function_call("inspect_site", {}, call_id="site"),
                function_call(name, {"kind": "structured"}, call_id="json"),
                function_call(name, {"kind": "illustrated"}, call_id="mixed"),
                function_call(name, {"kind": "combined"}, call_id="combined"),
                function_call(name, {"kind": "ticket"}, call_id="ticket"),
                function_call(name, {"kind": "missing"}, call_id="error"),
                function_call(name, {"kind": "text"}, call_id="text"),
                function_call(name, {"kind": "brief"}, call_id="brief"),
                function_call("mcp_Other__read_report", {"kind": "text"}, call_id="other"),
            ],
            [assistant_message("Done")],
        ]
    )
    with closing(recipe.Reports(OWNER)) as reports, closing(SQLiteSession("reports")) as session:
        async with (
            recipe.ReportServer(reports) as selected,
            recipe.ReportServer(reports, "Other") as other,
        ):
            selected.custom_data_extractor = observe
            agent = Agent(
                name="Inspector",
                model=model,
                tools=[reports.site_tool()],
                mcp_servers=[selected, other],
                mcp_config={"include_server_in_tool_names": True},
            )
            result = Runner.run_streamed(
                agent,
                "Inspect",
                context=OWNER,
                run_config=RunConfig(trace_include_sensitive_data=True),
                session=session,
            )
            events = [event async for event in result.stream_events()]

        detail = "WARN: synthetic detail " * 4
        structured = json.dumps({"report": detail * 2})
        image = "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw=="
        assert observed["json"][:3] == (OWNER, "Inspection reports", "read.report")
        assert observed["json"][4:6] == (None, None)
        assert observed["error"][-1] is True
        assert observed["mixed"][3][1] == {"type": "image", "image_url": image}
        spans = [span.export() for span in fetch_ordered_spans()]
        assert "Inspection report with 8 warning markers" in repr(spans)

        history = result.to_input_list()
        request = model.calls[1].input
        for surface in (
            events,
            spans,
            observed["json"],
            result.new_items,
            history,
            request,
            result.context_wrapper,
            result.to_state().to_json(),
            await session.get_items(),
        ):
            assert "synthetic detail" not in repr(surface)
            assert "synthetic site detail" not in repr(surface)
            assert "Fallback report" not in repr(surface)

        outputs = {
            i["call_id"]: i["output"] for i in request if i.get("type") == "function_call_output"
        }
        assert outputs["error"] == [{"type": "input_text", "text": "Report unavailable. " * 10}]
        assert outputs["brief"] == [{"type": "input_text", "text": "Sample reading. "}]
        assert outputs["other"] == [{"type": "input_text", "text": "Sample reading. " * 12}]
        assert (
            outputs["combined"][1:]
            == outputs["mixed"][1:]
            == [
                {"type": "input_image", "image_url": image},
                {"type": "input_text", "text": ""},
            ]
        )
        for summary, count, original in [
            (outputs["site"], 20, ("WARN: synthetic site detail\n" * 20,)),
            (outputs["json"][0]["text"], 8, (structured,)),
            (outputs["combined"][0]["text"], 12, (json.dumps({"report": detail * 3}),)),
            (
                outputs["ticket"][0]["text"],
                20,
                (json.dumps({"report": "WARN: synthetic site detail\n" * 20}),),
            ),
            (outputs["mixed"][0]["text"], 8, (detail, detail)),
            (outputs["text"][0]["text"], 0, ("Sample reading. " * 12,)),
        ]:
            assert len(summary.encode()) <= 128
            label, ref = summary.split(" Reference: ")
            assert label == f"Inspection report with {count} warning markers."
            assert ref in repr(events) and ref in repr(history) and ref in repr(result.new_items)
            for part, expected in enumerate(original):
                for offset in range(0, len(expected), 64):
                    assert (
                        reports.fetch(OWNER, ref, part=part, offset=offset)
                        == expected[offset : offset + 64]
                    )
            with pytest.raises(PermissionError):
                reports.fetch("other-user", ref)

        with closing(recipe.Reports(OWNER)) as other_session, pytest.raises(PermissionError):
            other_session.fetch(OWNER, ref)
    with pytest.raises(PermissionError):
        reports.fetch(OWNER, ref)


@pytest.mark.asyncio
async def test_local_tool_denies_access_before_reading(monkeypatch):
    async def forbidden():
        pytest.fail("Unauthorized caller reached the report reader")

    monkeypatch.setattr(recipe, "site_report", forbidden)
    model = ScriptedModel([[function_call("inspect_site", {}, call_id="site")]])
    with closing(recipe.Reports(OWNER)) as reports:
        agent = Agent(name="Inspector", model=model, tools=[reports.site_tool()])
        with pytest.raises(UserError, match="Report unavailable"):
            await Runner.run(agent, "Inspect", context="other-user")
    assert len(model.calls) == 1


@pytest.mark.asyncio
async def test_local_tool_rechecks_access_before_returning_a_small_report(monkeypatch):
    model = ScriptedModel([[function_call("inspect_site", {}, call_id="site")]])
    with closing(recipe.Reports(OWNER)) as reports:

        async def revoke():
            reports.close()
            return "Private reading"

        monkeypatch.setattr(recipe, "site_report", revoke)
        agent = Agent(name="Inspector", model=model, tools=[reports.site_tool()])
        with pytest.raises(UserError, match="Report unavailable"):
            await Runner.run(agent, "Inspect", context=OWNER)
    assert len(model.calls) == 1


@pytest.mark.asyncio
async def test_local_output_is_bounded_before_an_independent_editor():
    seen = []

    async def replace(call, output):
        seen.append(output)
        return "Reviewed"

    model = ScriptedModel(
        [[function_call("inspect_site", {}, call_id="site")], [assistant_message("Done")]]
    )
    with closing(recipe.Reports(OWNER)) as reports:
        agent = Agent(name="Inspector", model=model, tools=[reports.site_tool()])
        config = RunConfig(tool_execution=ToolExecutionConfig(prepare_tool_output=replace))
        await Runner.run(agent, "Inspect", context=OWNER, run_config=config)
        (summary,) = seen
        assert len(summary.encode()) <= 128
        label, ref = summary.split(" Reference: ")
        assert label == "Inspection report with 20 warning markers."
        assert reports.fetch(OWNER, ref) == (await recipe.site_report())[:64]
        assert "Reviewed" in repr(model.calls[1].input)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid", [None, {"text": "private"}, "é" * 64], ids=["missing", "object", "oversized"]
)
async def test_invalid_summary_never_reaches_model_or_events(monkeypatch, invalid):
    async def bad_summary(texts: tuple[str, ...]) -> Any:
        return invalid

    monkeypatch.setattr(recipe, "summarize", bad_summary)
    model = ScriptedModel([[function_call("inspect_site", {}, call_id="site")]])
    with closing(recipe.Reports(OWNER)) as reports:
        agent = Agent(name="Inspector", model=model, tools=[reports.site_tool()])
        result = Runner.run_streamed(agent, "Inspect", context=OWNER)
        with pytest.raises(UserError, match="summary"):
            async for event in result.stream_events():
                assert getattr(event, "name", None) != "tool_output"
        assert len(model.calls) == 1
        assert all(item.type != "tool_call_output_item" for item in result.new_items)


@pytest.mark.asyncio
@pytest.mark.parametrize(("kind", "reason"), [("structured", "summary"), ("audio", "media")])
async def test_rejected_mcp_output_withholds_raw_report_from_tracing(monkeypatch, kind, reason):
    async def invalid(texts: tuple[str, ...]) -> str:
        assert texts == (json.dumps({"report": "WARN: synthetic detail " * 8}),)
        return ""

    monkeypatch.setattr(recipe, "summarize", invalid)
    model = ScriptedModel([[function_call("read.report", {"kind": kind}, call_id="report")]])
    with closing(recipe.Reports(OWNER)) as reports:
        async with recipe.ReportServer(reports) as server:
            agent = Agent(name="Inspector", model=model, mcp_servers=[server])
            with pytest.raises(UserError, match=reason):
                await Runner.run(
                    agent,
                    "Inspect",
                    context=OWNER,
                    run_config=RunConfig(trace_include_sensitive_data=True),
                )
    spans = [span.export() for span in fetch_ordered_spans()]
    assert reason in repr(spans)
    assert "QUJD" not in repr(spans)
    assert "synthetic detail" not in repr(spans)
    assert "Fallback report" not in repr(spans)
    assert len(model.calls) == 1


@pytest.mark.asyncio
async def test_cancel_during_mcp_ticket_resolution(monkeypatch):
    started = asyncio.Event()
    stopped = asyncio.Event()

    async def pending_report() -> str:
        started.set()
        try:
            return await asyncio.Future()
        finally:
            stopped.set()

    monkeypatch.setattr(recipe, "site_report", pending_report)
    model = ScriptedModel([[function_call("read.report", {"kind": "ticket"}, call_id="ticket")]])
    with closing(recipe.Reports(OWNER)) as reports:
        async with recipe.ReportServer(reports) as server:
            agent = Agent(name="Inspector", model=model, mcp_servers=[server])
            task = asyncio.create_task(Runner.run(agent, "Inspect", context=OWNER))
            try:
                await asyncio.wait_for(started.wait(), 5)
            finally:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            await asyncio.wait_for(stopped.wait(), 5)
            assert len(model.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "close_at"), [("brief", "peer"), ("ticket", "peer"), ("ticket", "read")]
)
async def test_closed_store_withholds_short_mcp_reports(monkeypatch, kind, close_at):
    model = ScriptedModel(
        [
            [function_call("read.report", {"kind": kind}, call_id="report")],
            [assistant_message("Done")],
        ]
    )
    with closing(recipe.Reports(OWNER)) as reports:
        peer = recipe.MCPServerStdio.call_tool

        async def receive(*args, **kwargs):
            result = await peer(*args, **kwargs)
            if close_at == "peer":
                reports.close()
            return result

        async def read():
            if close_at != "read":
                pytest.fail("Closed store reached the ticket reader")
            reports.close()
            return "Private reading"

        monkeypatch.setattr(recipe.MCPServerStdio, "call_tool", receive)
        monkeypatch.setattr(recipe, "site_report", read)
        async with recipe.ReportServer(reports) as server:
            agent = Agent(name="Inspector", model=model, mcp_servers=[server])
            with pytest.raises(AgentsException, match="Report unavailable"):
                await Runner.run(
                    agent,
                    "Inspect",
                    context=OWNER,
                    run_config=RunConfig(trace_include_sensitive_data=True),
                )
    assert len(model.calls) == 1
    spans = repr([span.export() for span in fetch_ordered_spans()])
    assert "Private reading" not in spans and "Sample reading" not in spans
