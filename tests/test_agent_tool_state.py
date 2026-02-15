from __future__ import annotations

import gc
from typing import Any

import pytest

import agents.agent_tool_state as tool_state
from tests.utils.hitl import make_function_tool_call


def test_drop_agent_tool_run_result_handles_cleared_globals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tool_state, "_agent_tool_run_result_signature_by_obj", None)
    monkeypatch.setattr(tool_state, "_agent_tool_run_results_by_signature", None)

    # Should not raise even if globals are cleared during interpreter shutdown.
    tool_state._drop_agent_tool_run_result(123)


def test_agent_tool_result_survives_tool_call_gc_until_consumed() -> None:
    tool_state._agent_tool_run_results_by_obj.clear()
    tool_state._agent_tool_run_results_by_signature.clear()
    tool_state._agent_tool_run_result_signature_by_obj.clear()

    nested_result: Any = object()
    tool_call = make_function_tool_call(
        "inner_tool",
        call_id="inner-1",
        arguments='{"input":"hello"}',
    )
    tool_state.record_agent_tool_run_result(tool_call, nested_result)

    # Resume uses a reconstructed tool-call object with the same signature.
    resume_tool_call = make_function_tool_call(
        "inner_tool",
        call_id="inner-1",
        arguments='{"input":"hello"}',
    )

    del tool_call
    gc.collect()

    assert tool_state.peek_agent_tool_run_result(resume_tool_call) is nested_result
    assert tool_state.consume_agent_tool_run_result(resume_tool_call) is nested_result
    assert tool_state.peek_agent_tool_run_result(resume_tool_call) is None


def test_record_clears_stale_signature_when_obj_id_is_reused() -> None:
    tool_state._agent_tool_run_results_by_obj.clear()
    tool_state._agent_tool_run_results_by_signature.clear()
    tool_state._agent_tool_run_result_signature_by_obj.clear()

    tool_call = make_function_tool_call(
        "inner_tool",
        call_id="new-call",
        arguments='{"input":"hello"}',
    )
    obj_id = id(tool_call)

    stale_signature = (
        "old-call",
        "inner_tool",
        '{"input":"old"}',
        "function_call",
        "old-id",
        "completed",
    )
    stale_result: Any = object()
    new_result: Any = object()

    tool_state._agent_tool_run_results_by_obj[obj_id] = stale_result
    tool_state._agent_tool_run_result_signature_by_obj[obj_id] = stale_signature
    tool_state._agent_tool_run_results_by_signature[stale_signature] = {obj_id}

    tool_state.record_agent_tool_run_result(tool_call, new_result)

    assert obj_id in tool_state._agent_tool_run_results_by_obj
    assert tool_state._agent_tool_run_results_by_obj[obj_id] is new_result
    assert stale_signature not in tool_state._agent_tool_run_results_by_signature


def test_consume_peek_and_drop_direct_object_path() -> None:
    tool_state._agent_tool_run_results_by_obj.clear()
    tool_state._agent_tool_run_results_by_signature.clear()
    tool_state._agent_tool_run_result_signature_by_obj.clear()

    tool_call = make_function_tool_call(
        "inner_tool",
        call_id="direct-1",
        arguments='{"input":"hello"}',
    )
    nested_result: Any = object()

    tool_state.record_agent_tool_run_result(tool_call, nested_result)
    assert tool_state.peek_agent_tool_run_result(tool_call) is nested_result
    assert tool_state.consume_agent_tool_run_result(tool_call) is nested_result
    assert tool_state.consume_agent_tool_run_result(tool_call) is None

    tool_state.record_agent_tool_run_result(tool_call, nested_result)
    tool_state.drop_agent_tool_run_result(tool_call)
    assert tool_state.peek_agent_tool_run_result(tool_call) is None


def test_signature_fallback_none_and_ambiguous_paths() -> None:
    tool_state._agent_tool_run_results_by_obj.clear()
    tool_state._agent_tool_run_results_by_signature.clear()
    tool_state._agent_tool_run_result_signature_by_obj.clear()

    tool_call = make_function_tool_call(
        "inner_tool",
        call_id="fallback-1",
        arguments='{"input":"hello"}',
    )
    signature = tool_state._tool_call_signature(tool_call)

    # No candidate IDs -> None paths.
    assert tool_state.consume_agent_tool_run_result(tool_call) is None
    assert tool_state.peek_agent_tool_run_result(tool_call) is None
    tool_state.drop_agent_tool_run_result(tool_call)

    # Multiple candidate IDs -> ambiguous, should return/perform no-op.
    tool_state._agent_tool_run_results_by_signature[signature] = {101, 202}
    fake_result_1: Any = object()
    fake_result_2: Any = object()
    tool_state._agent_tool_run_results_by_obj[101] = fake_result_1
    tool_state._agent_tool_run_results_by_obj[202] = fake_result_2
    tool_state._agent_tool_run_result_signature_by_obj[101] = signature
    tool_state._agent_tool_run_result_signature_by_obj[202] = signature

    assert tool_state.consume_agent_tool_run_result(tool_call) is None
    assert tool_state.peek_agent_tool_run_result(tool_call) is None
    tool_state.drop_agent_tool_run_result(tool_call)


def test_drop_index_handles_missing_candidate_collection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signature = ("call", "name", "{}", "function_call", "id", "completed")

    signature_by_obj = {7: signature}
    monkeypatch.setattr(tool_state, "_agent_tool_run_result_signature_by_obj", signature_by_obj)
    monkeypatch.setattr(tool_state, "_agent_tool_run_results_by_signature", None)
    tool_state._drop_agent_tool_run_result(7)

    signature_by_obj = {9: signature}
    monkeypatch.setattr(tool_state, "_agent_tool_run_result_signature_by_obj", signature_by_obj)
    monkeypatch.setattr(tool_state, "_agent_tool_run_results_by_signature", {})
    tool_state._drop_agent_tool_run_result(9)


def test_drop_removes_single_fallback_candidate() -> None:
    tool_state._agent_tool_run_results_by_obj.clear()
    tool_state._agent_tool_run_results_by_signature.clear()
    tool_state._agent_tool_run_result_signature_by_obj.clear()

    stored_call = make_function_tool_call(
        "inner_tool",
        call_id="drop-fallback",
        arguments='{"input":"hello"}',
    )
    probe_call = make_function_tool_call(
        "inner_tool",
        call_id="drop-fallback",
        arguments='{"input":"hello"}',
    )

    stored_id = id(stored_call)
    signature = tool_state._tool_call_signature(stored_call)
    nested_result: Any = object()
    tool_state._agent_tool_run_results_by_obj[stored_id] = nested_result
    tool_state._agent_tool_run_result_signature_by_obj[stored_id] = signature
    tool_state._agent_tool_run_results_by_signature[signature] = {stored_id}

    tool_state.drop_agent_tool_run_result(probe_call)

    assert signature not in tool_state._agent_tool_run_results_by_signature
    assert stored_id not in tool_state._agent_tool_run_result_signature_by_obj
    assert stored_id not in tool_state._agent_tool_run_results_by_obj
