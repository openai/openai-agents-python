"""Use the recipe's actual subprocess and native MCP client, with no API key."""

import json
import os
from pathlib import Path

import pytest
from examples.recipes.local_mcp import catalog_agent

from agents import NativeError
from tests._asyncio import timeout
from tests.native_fixtures import (
    ResponsesFixture,
    assistant_output,
    function_call,
    responses as responses,
    tool_output,
)
from tests.test_native_agent import provider
from tests.test_native_mcp_stdio import assert_stopped


def audit_rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines()]


async def assert_process_closed(path: Path) -> None:
    async with timeout(10):
        [start] = [row for row in audit_rows(path) if row["event"] == "start"]
        # Windows termination bypasses atexit; require actual OS process exit.
        await assert_stopped(path)
        if os.name == "posix":
            assert any(
                row["event"] == "exit" and row["pid"] == start["pid"] for row in audit_rows(path)
            )


async def test_real_mcp_tool_runs_and_session_closes_process(
    responses: ResponsesFixture, tmp_path: Path
) -> None:
    audit = tmp_path / "mcp.jsonl"
    call = function_call("lookup", {"sku": "PAPER"}, "lookup")
    call["item"]["namespace"] = "mcp__catalog"
    responses.respond(call)
    responses.respond(assistant_output("It costs 5 dollars."))
    async with (
        timeout(20),
        catalog_agent(provider(responses), audit=audit).session() as session,
    ):
        result = await (await session.submit("Price of PAPER?")).get_result()
        assert result.final_output == "It costs 5 dollars."
        assert "PAPER: 5 dollars" in tool_output(responses.requests[1], "lookup")
        assert [row["event"] for row in audit_rows(audit)].count("lookup") == 1
    await assert_process_closed(audit)


@pytest.mark.parametrize(
    ("selected", "fail_start", "diagnosis"),
    [
        ("lookpu", False, "selected tools unavailable: 'lookpu'"),
        ("lookup", True, "connection not ready"),
    ],
)
async def test_bad_selection_and_startup_diagnose_and_close(
    responses: ResponsesFixture,
    tmp_path: Path,
    selected: str,
    fail_start: bool,
    diagnosis: str,
) -> None:
    audit = tmp_path / "mcp.jsonl"
    async with timeout(20):
        with pytest.raises(NativeError, match=diagnosis):
            async with catalog_agent(
                provider(responses),
                selected=selected,
                fail_start=fail_start,
                audit=audit,
            ).session() as session:
                await (await session.submit("Read catalog")).get_result()
        await assert_process_closed(audit)
    assert responses.requests == []
    assert not any(row["event"] == "lookup" for row in audit_rows(audit))


async def test_model_failure_also_closes_live_mcp_process(
    responses: ResponsesFixture, tmp_path: Path
) -> None:
    audit = tmp_path / "mcp.jsonl"
    call = function_call("lookup", {"sku": "PAPER"}, "lookup")
    call["item"]["namespace"] = "mcp__catalog"
    responses.respond(call)
    responses.respond(
        {
            "type": "response.failed",
            "response": {
                "error": {
                    "type": "invalid_request_error",
                    "code": "invalid_prompt",
                    "message": "Synthetic failure after MCP lookup",
                }
            },
        }
    )
    async with timeout(20):
        with pytest.raises(NativeError):
            async with catalog_agent(provider(responses), audit=audit).session() as session:
                await (await session.submit("Read catalog")).get_result()
        await assert_process_closed(audit)
