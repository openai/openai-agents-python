from __future__ import annotations

import gc
import weakref
from types import SimpleNamespace
from typing import Any, cast

import pytest
from openai.types.responses import ResponseFunctionToolCall

import agents.agent_tool_state as tool_state

from .test_responses import get_function_tool_call


@pytest.fixture(autouse=True)
def reset_tool_state_globals(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tool_state, "_agent_tool_run_results_by_obj", {})
    monkeypatch.setattr(tool_state, "_agent_tool_run_results_by_signature", {})
    monkeypatch.setattr(tool_state, "_agent_tool_run_result_signature_by_obj", {})
    monkeypatch.setattr(tool_state, "_agent_tool_call_refs_by_obj", {})


def test_drop_agent_tool_run_result_handles_cleared_globals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tool_state, "_agent_tool_call_refs_by_obj", None)
    monkeypatch.setattr(tool_state, "_agent_tool_run_result_signature_by_obj", None)
    monkeypatch.setattr(tool_state, "_agent_tool_run_results_by_signature", None)

    # Should not raise even if globals are cleared during interpreter shutdown.
    tool_state._drop_agent_tool_run_result(123)


def test_agent_tool_state_scope_helpers_tolerate_missing_or_readonly_contexts() -> None:
    context = SimpleNamespace()

    tool_state.set_agent_tool_state_scope(None, "ignored")
    tool_state.set_agent_tool_state_scope(context, "scope-1")
    assert tool_state.get_agent_tool_state_scope(context) == "scope-1"

    tool_state.set_agent_tool_state_scope(context, None)
    assert tool_state.get_agent_tool_state_scope(context) is None

    readonly_context = object()
    tool_state.set_agent_tool_state_scope(readonly_context, "scope-2")
    assert tool_state.get_agent_tool_state_scope(readonly_context) is None


def _function_tool_call(name: str, arguments: str, *, call_id: str) -> ResponseFunctionToolCall:
    tool_call = get_function_tool_call(name, arguments, call_id=call_id)
    assert isinstance(tool_call, ResponseFunctionToolCall)
    return tool_call


def test_agent_tool_run_result_supports_signature_fallback_across_instances() -> None:
    original_call = _function_tool_call("lookup_account", "{}", call_id="call-1")
    restored_call = _function_tool_call("lookup_account", "{}", call_id="call-1")
    run_result = cast(Any, object())

    tool_state.record_agent_tool_run_result(original_call, run_result, scope_id="scope-1")

    assert tool_state.peek_agent_tool_run_result(restored_call, scope_id="scope-1") is run_result
    assert tool_state.consume_agent_tool_run_result(restored_call, scope_id="scope-1") is run_result
    assert tool_state.peek_agent_tool_run_result(original_call, scope_id="scope-1") is None
    assert tool_state._agent_tool_run_results_by_signature == {}


def test_agent_tool_run_result_returns_none_for_ambiguous_signature_matches() -> None:
    first_call = _function_tool_call("lookup_account", "{}", call_id="call-1")
    second_call = _function_tool_call("lookup_account", "{}", call_id="call-1")
    restored_call = _function_tool_call("lookup_account", "{}", call_id="call-1")
    first_result = cast(Any, object())
    second_result = cast(Any, object())

    tool_state.record_agent_tool_run_result(first_call, first_result, scope_id="scope-1")
    tool_state.record_agent_tool_run_result(second_call, second_result, scope_id="scope-1")

    assert tool_state.peek_agent_tool_run_result(restored_call, scope_id="scope-1") is None
    assert tool_state.consume_agent_tool_run_result(restored_call, scope_id="scope-1") is None

    tool_state.drop_agent_tool_run_result(restored_call, scope_id="scope-1")

    assert tool_state.peek_agent_tool_run_result(first_call, scope_id="scope-1") is first_result
    assert tool_state.peek_agent_tool_run_result(second_call, scope_id="scope-1") is second_result
    assert tool_state.peek_agent_tool_run_result(restored_call, scope_id="other-scope") is None


def test_agent_tool_run_result_is_dropped_when_tool_call_is_collected() -> None:
    tool_call = _function_tool_call("lookup_account", "{}", call_id="call-1")
    tool_call_ref = weakref.ref(tool_call)
    tool_call_obj_id = id(tool_call)

    tool_state.record_agent_tool_run_result(tool_call, cast(Any, object()), scope_id="scope-1")

    del tool_call
    gc.collect()

    assert tool_call_ref() is None
    assert tool_call_obj_id not in tool_state._agent_tool_run_results_by_obj
    assert tool_call_obj_id not in tool_state._agent_tool_run_result_signature_by_obj
    assert tool_call_obj_id not in tool_state._agent_tool_call_refs_by_obj
