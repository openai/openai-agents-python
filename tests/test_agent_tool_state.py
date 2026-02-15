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
