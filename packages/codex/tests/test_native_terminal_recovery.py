"""Fresh terminal receipts do not replay or authorize saved tool results."""

from __future__ import annotations

import asyncio
import json

import pytest

from agents import (
    Agent,
    CustomStorage,
    FunctionTool,
    NativeError,
    SessionState,
    SQLiteSessionStore,
    function_tool,
)
from tests._asyncio import timeout
from tests.native_fixtures import (
    ResponsesFixture,
    assistant_output,
    function_call,
    responses as responses,
)
from tests.test_native_manual_tools import LookupArgs, next_request
from tests.test_native_terminal_tools import provider


class TerminalFailureStore(SQLiteSessionStore):
    def __init__(self, path, mode):
        super().__init__(path)
        self.mode = mode
        self.terminal = False
        self.failed = False

    def commit(self, thread_id, token, expected_revision, write_id, payload):
        if "tool_completion" in payload:
            self.terminal = True
            if self.mode == "append":
                self.failed = True
                raise OSError("injected terminal append failure")
        return super().commit(thread_id, token, expected_revision, write_id, payload)

    def flush(self, thread_id, token):
        if self.terminal and self.mode == "flush":
            self.failed = True
            raise OSError("injected terminal flush failure")
        return super().flush(thread_id, token)


@pytest.mark.parametrize("mode", ["append", "flush"])
async def test_root_terminal_persistence_failure_settles_without_completed(
    responses, tmp_path, mode
):
    store = TerminalFailureStore(tmp_path / "terminal.sqlite", mode)

    @function_tool(finish_turn=True)
    def done() -> str:
        return "must not be published as Completed"

    responses.respond(function_call("done", {}, "terminal-failure"))
    async with (
        timeout(30),
        Agent(
            instructions="Use done.", model="gpt-5.5", tools=[done], provider=provider(responses)
        ).session(storage=CustomStorage(store)) as session,
    ):
        handle = await session.submit("finish")
        with pytest.raises((NativeError, RuntimeError), match="[Cc]anonical|persistence failed"):
            await asyncio.wait_for(handle.get_result(), 10)
        assert store.terminal and store.failed
    assert len(responses.requests) == 1


@pytest.mark.parametrize("saved_outcomes", [True, False])
async def test_saved_finisher_receipt_is_data_and_fresh_manual_reply_owns_completion(
    responses: ResponsesFixture, saved_outcomes: bool
) -> None:
    effects: list[str] = []

    @function_tool(finish_turn=True)
    def old_finish() -> str:
        effects.append("old-once")
        return "old result must not win"

    fresh = FunctionTool(
        name="fresh_finish",
        description="Finish with the fresh reply.",
        input_type=LookupArgs,
        output_type=str,
        finish_turn=True,
    )
    responses.respond(
        function_call("old_finish", {}, "old-finish"),
        function_call("fresh_finish", {"key": "A"}, "fresh-finish"),
    )
    agent = Agent(
        instructions="Use both tools.",
        model="gpt-5.5",
        tools=[old_finish, fresh],
        provider=provider(responses),
    )
    async with timeout(30), agent.session() as session:
        await session.submit("finish")
        await next_request(session.interactions(), fresh)
        state = json.loads((await session.suspend()).to_json())
    assert effects == ["old-once"]
    if not saved_outcomes:
        state["native_continuation"].pop("tool_outcomes", None)
        # Missing historical sibling success cannot be invented from its text.
        responses.respond(assistant_output("resumed answer"))
    async with (
        timeout(30),
        agent.session(state=SessionState.from_json(json.dumps(state))) as restored,
    ):
        assert restored.restored_turn is not None
        request = await next_request(restored.interactions(), fresh)
        await request.respond("fresh result")
        result = await restored.restored_turn.get_result()
        if saved_outcomes:
            assert result.final_output == "fresh result"
            assert result.tool_call is not None
            assert result.tool_call.call_id == "fresh-finish"
        else:
            assert result.final_output == "resumed answer"
            assert result.tool_call is None
    assert effects == ["old-once"]
    assert len(responses.requests) == (1 if saved_outcomes else 2)
