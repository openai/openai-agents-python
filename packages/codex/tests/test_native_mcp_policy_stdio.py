"""Selected StdioMCP pre-execution policies against the real spawned peer."""

from __future__ import annotations

import json
from contextlib import AsyncExitStack

import pytest

from agents import Agent, Allow, NativeError, ReplaceArguments, StopTurn, TurnStoppedError
from tests._asyncio import timeout
from tests.native_executor_fixtures import ExecutorPeer
from tests.native_fixtures import ResponsesFixture, assistant_output, responses as responses
from tests.test_native_agent import provider
from tests.test_native_child_agents import trust
from tests.test_native_code_mode import code_call, code_output
from tests.test_native_executor_tools import declaration
from tests.test_native_mcp_stdio import assert_stopped, records, stdio
from tests.test_native_mcp_tools import mcp_call


@pytest.mark.parametrize("in_code", [False, True], ids=["direct", "code"])
@pytest.mark.parametrize("decision", ["allow", "replace", "deny", "stop"])
@pytest.mark.parametrize("location", ["host", "environment"])
async def test_stdio_owned_pre_execution_decisions_and_effects(
    responses: ResponsesFixture, tmp_path, monkeypatch, in_code, decision, location
):
    log = tmp_path / "local-stdio"
    observed = []

    def before(ctx, call):
        assert ctx.submission is turn
        assert (call.namespace, call.name) == ("mcp__local", "search")
        if decision == "replace":
            return ReplaceArguments({"key": "effective"})
        if decision == "stop":
            return StopTurn("private-stdio-stop")
        return Allow()

    def approval(ctx, call):
        assert ctx.submission is turn
        observed.append(json.loads(call.input) if isinstance(call.input, str) else call.input)
        return decision != "deny"

    if in_code:
        call_event = code_call(
            'text(await tools.mcp__local__search({key:"original"}));', "local-owned"
        )
    else:
        call_event = mcp_call("search", "mcp__local", "local-owned")
        call_event["item"]["arguments"] = json.dumps({"key": "original"})
    responses.respond(call_event)
    responses.respond(assistant_output("done"))
    async with timeout(24), AsyncExitStack() as stack:
        environment = None
        if location == "environment":
            executor = await stack.enter_async_context(ExecutorPeer(tmp_path / "remote").serve())
            trust(executor, tmp_path, monkeypatch)
            log = executor.root / "local-stdio"
            environment = declaration(executor)
        agent = Agent(
            instructions="Search",
            provider=provider(responses),
            programmatic_tool_calling=in_code,
            tools=[stdio(log, location=location, before=before, approval=approval)],
        )
        async with agent.session(environment=environment) as session:
            turn = await session.submit("use local search")
            rejects_turn = decision == "stop" or (decision == "deny" and not in_code)
            if rejects_turn:
                with pytest.raises(TurnStoppedError if decision == "stop" else NativeError):
                    await turn.get_result()
                assert len(responses.requests) == 1
            else:
                assert (await turn.get_result()).final_output == "done"
                if in_code:
                    output = code_output(responses.requests[-1], "local-owned")
                    assert ("private-stdio-result" in output) is (decision != "deny")
                else:
                    called = next(
                        i
                        for i in responses.requests[-1]["input"]
                        if i.get("type") == "function_call" and i.get("call_id") == "local-owned"
                    )
                    assert json.loads(called["arguments"]) == {
                        "key": "effective" if decision == "replace" else "original"
                    }
            expected = {"key": "effective" if decision == "replace" else "original"}
            assert observed == ([] if decision == "stop" else [expected])
            calls = [
                item["message"]["params"]
                for item in records(log)
                if item.get("message", {}).get("method") == "tools/call"
            ]
            assert [call["arguments"] for call in calls] == (
                [] if decision in {"deny", "stop"} else [expected]
            )
            if rejects_turn:
                assert (await (await session.submit("later")).get_result()).final_output == "done"
            assert "private-stdio-stop" not in json.dumps(responses.requests)
        await assert_stopped(log)
