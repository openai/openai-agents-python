from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from _agents_codex.session import Session
from agents import GuardrailResult


@pytest.mark.parametrize("info", [RuntimeError("local"), datetime(2026, 1, 1), {"local": object()}])
def test_checkpoint_omits_local_only_diagnostics_without_changing_live_result(info):
    agent = object()
    result = GuardrailResult(False, info)
    record = SimpleNamespace(
        kind="input",
        name="guard",
        agent=agent,
        session_id="session",
        turn_id="turn",
        subagent_id=None,
        result=result,
        tool_call=None,
    )
    owner = SimpleNamespace(
        _profiles={},
        _voice_profiles={},
        _original_agent_for_message=lambda message: agent,
    )
    captured = Session._checkpoint_guardrail_records(
        owner, SimpleNamespace(guardrail_results=[record])
    )
    assert "output_info" not in captured[0]
    assert captured[0]["tripwire_triggered"] is False
    assert record.result.output_info is info


def test_checkpoint_detaches_json_diagnostics_and_omits_cycles():
    agent = object()
    info = {"details": ["original"]}
    record = SimpleNamespace(
        kind="input",
        name="guard",
        agent=agent,
        session_id="session",
        turn_id="turn",
        subagent_id=None,
        result=GuardrailResult(False, info),
        tool_call=None,
    )
    owner = SimpleNamespace(
        _profiles={},
        _voice_profiles={},
        _original_agent_for_message=lambda message: agent,
    )
    state = SimpleNamespace(guardrail_results=[record])
    captured = Session._checkpoint_guardrail_records(owner, state)
    info["details"].append("later")
    assert captured[0]["output_info"] == {"details": ["original"]}
    cyclic = {}
    cyclic["self"] = cyclic
    record.result = GuardrailResult(False, cyclic)
    assert "output_info" not in Session._checkpoint_guardrail_records(owner, state)[0]
    assert record.result.output_info is cyclic


async def test_checkpoint_diagnostic_validation_precedes_irreversible_native_seal():
    failure = ValueError("invalid diagnostic source")
    seal = AsyncMock()
    owner = SimpleNamespace(
        _profiles={},
        _executor=None,
        _strict_executor_writes=False,
        _executor_policy_digest=AsyncMock(return_value=None),
        _manual_ready=AsyncMock(return_value=True),
        _checkpoint_guardrail_records=Mock(side_effect=failure),
        _native=SimpleNamespace(_seal_manual=seal),
    )
    with pytest.raises(ValueError) as caught:
        await Session._capture_checkpoint(
            owner, SimpleNamespace(operation_refs=None, budget=None), None, False
        )
    assert caught.value is failure
    seal.assert_not_called()
