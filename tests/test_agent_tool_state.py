from __future__ import annotations

import pytest

import agents.agent_tool_state as tool_state


def test_drop_agent_tool_run_result_handles_cleared_globals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tool_state, "_agent_tool_call_refs_by_obj", None)
    monkeypatch.setattr(tool_state, "_agent_tool_run_result_signature_by_obj", None)
    monkeypatch.setattr(tool_state, "_agent_tool_run_results_by_signature", None)

    # Should not raise even if globals are cleared during interpreter shutdown.
    tool_state._drop_agent_tool_run_result(123)
