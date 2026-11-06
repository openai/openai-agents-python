"""Native executor argument replacement reaches the original tool consumer."""

from __future__ import annotations

from typing import Any

import pytest

from agents import Agent, ExecutorContext, ReplaceArguments
from tests._asyncio import timeout
from tests.native_executor_fixtures import ExecutorPeer
from tests.native_fixtures import (
    ResponsesFixture,
    assistant_output,
    function_call,
    responses as responses,
    tool_output,
)
from tests.test_native_agent import provider
from tests.test_native_code_mode import code_call, code_output
from tests.test_native_executor_tools import declaration


@pytest.mark.parametrize("route", ["direct", "code"])
async def test_executor_object_argument_rewrite_reaches_native_handler(
    responses: ResponsesFixture, tmp_path, monkeypatch, route
) -> None:
    contexts: list[ExecutorContext[None]] = []
    original = {"session_id": 731031}
    accepted = {"session_id": 731032}

    async def arguments(ctx: ExecutorContext[None], value: Any) -> ReplaceArguments:
        assert isinstance(value, dict)
        assert value == original
        assert ctx.tool_name == "write_stdin"
        contexts.append(ctx)
        return ReplaceArguments(accepted)

    async with timeout(35), ExecutorPeer(tmp_path / "remote").serve() as peer:
        monkeypatch.setenv("SSL_CERT_FILE", str(peer.ca))
        monkeypatch.setenv("SSL_CERT_DIR", str(tmp_path / "empty"))
        responses.respond(
            code_call(
                "try { text(await tools.write_stdin({session_id:731031})); } "
                "catch (error) { text(String(error)); }",
                "rewrite-code",
            )
            if route == "code"
            else function_call("write_stdin", original, "rewrite-direct")
        )
        responses.respond(assistant_output("done"))
        async with Agent(
            instructions="Use the selected tool once.",
            tools=[],
            provider=provider(responses),
            programmatic_tool_calling=route == "code",
        ).session(environment=declaration(peer, transform_arguments=arguments)) as session:
            handle = await session.submit("Rewrite the tool arguments.")
            assert (await handle.get_result()).final_output == "done"
            assert len(contexts) == 1
            context = contexts[0]
            assert context.session is session
            assert context.submission is handle
            assert context.thread_id == session.id
            assert context.turn_id == handle.turn_id
            if route == "code":
                assert context.outer_call_id == "rewrite-code"
                assert context.cell_id is not None
                assert context.runtime_tool_call_id is not None
            else:
                assert context.call_id == "rewrite-direct"
                assert context.cell_id is None
            assert len(responses.requests) == 2
            output = (
                code_output(responses.requests[1], "rewrite-code")
                if route == "code"
                else tool_output(responses.requests[1], "rewrite-direct")
            )
            # The real handler must consume the replacement object. Its unknown
            # process result distinguishes that consumption from callback entry.
            assert "Unknown process id 731032" in output
            assert "731031" not in output
            assert not any(request.get("method") == "process/start" for request in peer.requests)
