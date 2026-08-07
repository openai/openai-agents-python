"""Verify that SDK exceptions survive copy and pickle round-trips.

`BaseException.__reduce__` rebuilds an exception with `type(exc)(*exc.args)`, so an
exception whose `__init__` takes more parameters than it forwards to `super().__init__`
cannot be copied or pickled. Anything that moves an exception across a process boundary
(multiprocessing pools, task queues, workflow engines) relies on this working.
"""

from __future__ import annotations

import copy
import pickle
from typing import Any

import pytest

from agents import (
    Agent,
    AgentsException,
    GuardrailFunctionOutput,
    InputGuardrail,
    InputGuardrailResult,
    InputGuardrailTripwireTriggered,
    MaxTurnsExceeded,
    MCPToolCancellationError,
    ModelBehaviorError,
    ModelRefusalError,
    OutputGuardrail,
    OutputGuardrailResult,
    OutputGuardrailTripwireTriggered,
    RunContextWrapper,
    ToolGuardrailFunctionOutput,
    ToolInputGuardrail,
    ToolInputGuardrailTripwireTriggered,
    ToolOutputGuardrail,
    ToolOutputGuardrailTripwireTriggered,
    ToolTimeoutError,
    UserError,
)


def _guardrail_function(
    context: RunContextWrapper[Any], agent: Agent[Any], data: Any
) -> GuardrailFunctionOutput:
    return GuardrailFunctionOutput(output_info=None, tripwire_triggered=True)


def _tool_guardrail_function(data: Any) -> ToolGuardrailFunctionOutput:
    return ToolGuardrailFunctionOutput(output_info=None)


def _input_guardrail_result() -> InputGuardrailResult:
    return InputGuardrailResult(
        guardrail=InputGuardrail(guardrail_function=_guardrail_function),
        output=GuardrailFunctionOutput(output_info="blocked", tripwire_triggered=True),
    )


def _output_guardrail_result() -> OutputGuardrailResult:
    return OutputGuardrailResult(
        guardrail=OutputGuardrail(guardrail_function=_guardrail_function),
        agent_output="final",
        agent=Agent(name="test"),
        output=GuardrailFunctionOutput(output_info="blocked", tripwire_triggered=True),
    )


def _exception_cases() -> list[AgentsException]:
    return [
        UserError("bad usage"),
        MaxTurnsExceeded("too many turns"),
        ModelBehaviorError("bad model output"),
        MCPToolCancellationError("cancelled"),
        ModelRefusalError("cannot help"),
        ToolTimeoutError("my_tool", 1.5),
        InputGuardrailTripwireTriggered(_input_guardrail_result()),
        OutputGuardrailTripwireTriggered(_output_guardrail_result()),
        ToolInputGuardrailTripwireTriggered(
            ToolInputGuardrail(guardrail_function=_tool_guardrail_function),
            ToolGuardrailFunctionOutput(output_info="blocked"),
        ),
        ToolOutputGuardrailTripwireTriggered(
            ToolOutputGuardrail(guardrail_function=_tool_guardrail_function),
            ToolGuardrailFunctionOutput(output_info="blocked"),
        ),
    ]


@pytest.mark.parametrize("error", _exception_cases(), ids=lambda error: type(error).__name__)
def test_sdk_exceptions_can_be_copied(error: AgentsException) -> None:
    copied = copy.copy(error)

    assert type(copied) is type(error)
    assert str(copied) == str(error)
    for name, value in vars(error).items():
        assert getattr(copied, name) is value


def test_tool_timeout_error_survives_copy_with_its_fields() -> None:
    error = ToolTimeoutError("my_tool", 1.5)

    copied = copy.copy(error)

    assert copied.tool_name == "my_tool"
    assert copied.timeout_seconds == 1.5
    assert str(copied) == "Tool 'my_tool' timed out after 1.5 seconds."


def test_tool_timeout_error_survives_pickle() -> None:
    restored = pickle.loads(pickle.dumps(ToolTimeoutError("my_tool", 1.5)))

    assert isinstance(restored, ToolTimeoutError)
    assert restored.tool_name == "my_tool"
    assert restored.timeout_seconds == 1.5
    assert restored.run_data is None
