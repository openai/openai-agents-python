from __future__ import annotations

from typing import Any, cast

import pytest
from openai.types.responses import ResponseFunctionToolCall

import agents.agent_tool_state as tool_state
from tests.test_responses import get_function_tool_call


@pytest.fixture(autouse=True)
def reset_tool_state_globals(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tool_state, "_agent_tool_run_results_by_obj", {})
    monkeypatch.setattr(tool_state, "_agent_tool_run_results_by_signature", {})
    monkeypatch.setattr(tool_state, "_agent_tool_run_result_signature_by_obj", {})
    monkeypatch.setattr(tool_state, "_agent_tool_call_refs_by_obj", {})


def _tool_call(arguments: str) -> ResponseFunctionToolCall:
    tool_call = get_function_tool_call("lookup_account", arguments, call_id="call-1")
    assert isinstance(tool_call, ResponseFunctionToolCall)
    return tool_call


def test_record_agent_tool_run_result_reindexes_mutated_call() -> None:
    tool_call = _tool_call("{}")
    old_signature_call = _tool_call("{}")
    first_result = cast(Any, object())
    second_result = cast(Any, object())

    tool_state.record_agent_tool_run_result(tool_call, first_result, scope_id="scope-1")

    tool_call.arguments = '{"account_id":"123"}'
    tool_state.record_agent_tool_run_result(tool_call, second_result, scope_id="scope-1")

    new_signature_call = _tool_call('{"account_id":"123"}')
    assert tool_state.peek_agent_tool_run_result(old_signature_call, scope_id="scope-1") is None
    assert (
        tool_state.peek_agent_tool_run_result(new_signature_call, scope_id="scope-1")
        is second_result
    )
    assert len(tool_state._agent_tool_run_results_by_signature) == 1
