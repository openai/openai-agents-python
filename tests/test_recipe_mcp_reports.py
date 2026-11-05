"""Exercise the report recipe through real native MCP and caller history."""

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any

import pytest
from aiohttp import web

from agents import NativeError, TurnStoppedError
from examples.recipes.mcp_reports import ReportStore, report_session
from tests._asyncio import timeout
from tests.native_fixtures import (
    ResponsesFixture,
    assistant_output,
    responses as responses,
    tool_output,
)
from tests.test_native_agent import provider
from tests.test_native_mcp_tools import Peer, mcp_call

PRIVATE_TAIL = "PRIVATE_REPORT_TAIL"
REPORT = "Quarterly overview. " + ("資料 📊 detail " * 600) + PRIVATE_TAIL


@dataclass
class ReportPeer(Peer):
    """Keep existing HTTP MCP lifecycle; vary only the tools/call result."""

    names: tuple[str, ...] = ("report",)
    result: dict[str, Any] = field(default_factory=dict)

    async def handle(self, request: web.Request) -> web.Response:
        body = await request.json()
        if body.get("method") != "tools/call":
            return await super().handle(request)
        self.requests.append(body)
        self.calls.append(body["params"])
        self.call_entered.set()
        if self.release_call is not None:
            await self.release_call.wait()
        return web.json_response({"jsonrpc": "2.0", "id": body["id"], "result": self.result})


def original_result() -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": REPORT}],
        "isError": False,
        "_meta": {"source": "synthetic-report"},
    }


async def test_report_replacement_reaches_model_and_caller_before_private_retrieval(
    responses: ResponsesFixture,
) -> None:
    original = original_result()
    release = asyncio.Event()
    peer = ReportPeer(result=original, release_call=release)
    responses.respond(mcp_call("report", "mcp__reports", "report-1"))
    responses.respond(assistant_output("Report ready."))
    owner = object()
    async with timeout(30), peer.serve() as url:
        async with report_session(url, owner, provider=provider(responses)) as (session, reports):
            handle = await session.submit("Read the report")
            await peer.call_entered.wait()
            assert len(responses.requests) == 1
            assert reports.references(owner) == ()
            release.set()
            assert (await handle.get_result()).final_output == "Report ready."
            assert len(peer.calls) == 1
            [reference] = reports.references(owner)
            assert reports.read(owner, reference) == original
            with pytest.raises(PermissionError, match="owner"):
                reports.read(object(), reference)
            with pytest.raises(PermissionError, match="owner"):
                reports.references(object())
            with pytest.raises(KeyError, match="unavailable"):
                reports.read(owner, "missing-reference")
            reports.read(owner, reference)["content"].clear()
            assert reports.read(owner, reference) == original
            history = [item async for item in session.items()]
            for channel in (json.dumps(responses.requests[1]), json.dumps(history)):
                assert reference in channel
                assert "Quarterly" in channel
                assert PRIVATE_TAIL not in channel
            output = tool_output(responses.requests[1], "report-1")
            assert len(output.encode("utf-8")) < 4096
        with pytest.raises(PermissionError, match="unavailable"):
            reports.read(owner, reference)


@pytest.mark.parametrize("is_error", [False, True])
async def test_small_success_and_peer_error_are_not_retained(
    responses: ResponsesFixture, is_error: bool
) -> None:
    peer = ReportPeer(
        result={"content": [{"type": "text", "text": "Inline report"}], "isError": is_error}
    )
    responses.respond(mcp_call("report", "mcp__reports", "inline"))
    responses.respond(assistant_output("Complete"))
    owner = object()
    async with timeout(30), peer.serve() as url:
        async with report_session(url, owner, provider=provider(responses)) as (session, reports):
            await (await session.submit("Read")).get_result()
            assert "Inline report" in tool_output(responses.requests[1], "inline")
            assert reports.references(owner) == ()


async def test_capacity_failure_preserves_earlier_references_until_session_cleanup(
    responses: ResponsesFixture,
) -> None:
    peer = ReportPeer(result=original_result())
    owner = object()
    async with timeout(30), peer.serve() as url:
        async with report_session(url, owner, provider=provider(responses)) as (session, reports):
            for index in range(4):
                responses.respond(mcp_call("report", "mcp__reports", f"report-{index}"))
                responses.respond(assistant_output("Stored"))
                await (await session.submit("Read")).get_result()
            references = reports.references(owner)
            responses.respond(mcp_call("report", "mcp__reports", "too-many"))
            with pytest.raises(NativeError, match="MCP result policy failed"):
                await (await session.submit("Read another")).get_result()
            assert reports.references(owner) == references
            for reference in references:
                assert reports.read(owner, reference) == peer.result
            history = [item async for item in session.items()]
            assert PRIVATE_TAIL not in json.dumps(history)
            assert PRIVATE_TAIL not in json.dumps(responses.requests)
        with pytest.raises(PermissionError, match="unavailable"):
            reports.references(owner)


async def test_body_failure_expires_retained_reports(responses: ResponsesFixture) -> None:
    peer = ReportPeer(result=original_result())
    responses.respond(mcp_call("report", "mcp__reports", "before-failure"))
    responses.respond(assistant_output("Stored"))
    owner = object()
    async with timeout(30), peer.serve() as url:
        with pytest.raises(RuntimeError, match="Application failed"):
            async with report_session(url, owner, provider=provider(responses)) as (
                session,
                reports,
            ):
                await (await session.submit("Read")).get_result()
                [reference] = reports.references(owner)
                raise RuntimeError("Application failed")
        with pytest.raises(PermissionError, match="unavailable"):
            reports.read(owner, reference)


async def test_cancelled_remote_call_publishes_no_reference(responses: ResponsesFixture) -> None:
    release = asyncio.Event()
    peer = ReportPeer(result=original_result(), release_call=release)
    responses.respond(mcp_call("report", "mcp__reports", "cancelled-report"))
    owner = object()
    async with timeout(30), peer.serve() as url:
        try:
            async with report_session(url, owner, provider=provider(responses)) as (
                session,
                reports,
            ):
                handle = await session.submit("Read")
                await peer.call_entered.wait()
                await handle.cancel()
                with pytest.raises(TurnStoppedError):
                    await handle.get_result()
                assert reports.references(owner) == ()
                assert len(responses.requests) == 1
        finally:
            release.set()
        with pytest.raises(PermissionError, match="unavailable"):
            reports.references(owner)


async def test_transform_preserves_report_metadata() -> None:
    # Native model/history projection does not expose every MCP envelope field.
    # Check that separate result-envelope contract directly at the application callback.
    owner = object()
    reports = ReportStore(owner)
    original = original_result()
    original["content"][0]["annotations"] = {"audience": ["assistant"]}
    bounded = await reports.transform(None, original)
    assert bounded["content"][0]["annotations"] == {"audience": ["assistant"]}
    assert bounded["_meta"] == original["_meta"]
    assert bounded["isError"] is False
    assert len(json.dumps(bounded, ensure_ascii=False).encode("utf-8")) <= 4096
    reports.close()


@pytest.mark.parametrize(
    "result",
    [
        {"content": [{"type": "text", "text": "x" * (65 * 1024)}]},
        {"content": [{"type": "text", "text": "small"}] * 2},
        {"content": [{"type": "image", "mimeType": "image/png", "data": "AA=="}]},
        {"content": [], "structuredContent": {"report": REPORT}},
        {"content": [{"type": "text", "text": REPORT}], "_meta": {"detail": "x" * 4096}},
    ],
    ids=["report-size", "content-count", "media", "structured-content", "metadata-size"],
)
async def test_unsupported_reports_fail_without_publishing_a_reference(
    responses: ResponsesFixture, result: dict[str, Any]
) -> None:
    peer = ReportPeer(result=result)
    responses.respond(mcp_call("report", "mcp__reports", "unsupported"))
    owner = object()
    async with timeout(30), peer.serve() as url:
        async with report_session(url, owner, provider=provider(responses)) as (session, reports):
            with pytest.raises(NativeError, match="MCP result policy failed"):
                await (await session.submit("Read")).get_result()
            assert reports.references(owner) == ()
            assert len(responses.requests) == 1
