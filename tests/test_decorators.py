import types
from typing import Any

from typing_extensions import assert_type

import agents.decorators as decorators_module
import agents.tool as tool_module
from agents import (
    FunctionTool,
    ToolGuardrailFunctionOutput,
    function_tool,
    input_guardrail,
    output_guardrail,
    tool_input_guardrail,
    tool_output_guardrail,
)
from agents.decorators import function_tool as decorators_function_tool, tool
from agents.tool_guardrails import (
    ToolInputGuardrail,
    ToolInputGuardrailData,
    ToolOutputGuardrail,
    ToolOutputGuardrailData,
)


def test_decorator_module_preserves_existing_imports_and_identities() -> None:
    assert isinstance(decorators_module, types.ModuleType)
    assert isinstance(tool_module, types.ModuleType)
    assert decorators_function_tool is function_tool
    assert tool is function_tool
    assert decorators_module.input_guardrail is input_guardrail
    assert decorators_module.output_guardrail is output_guardrail
    assert decorators_module.tool_input_guardrail is tool_input_guardrail
    assert decorators_module.tool_output_guardrail is tool_output_guardrail
    assert tool_module.function_tool is function_tool


def test_tool_alias_supports_bare_and_configured_decorator_forms() -> None:
    @tool
    def bare_alias() -> str:
        return "bare"

    @tool(name_override="configured_alias")
    async def configured_alias() -> str:
        return "configured"

    assert_type(bare_alias, FunctionTool)
    assert_type(configured_alias, FunctionTool)
    assert bare_alias.name == "bare_alias"
    assert configured_alias.name == "configured_alias"


def test_tool_guardrail_decorators_keep_their_type_in_bare_form() -> None:
    @tool_input_guardrail
    def bare_input(data: ToolInputGuardrailData) -> ToolGuardrailFunctionOutput:
        return ToolGuardrailFunctionOutput.allow()

    @tool_input_guardrail(name="configured_input")
    def configured_input(data: ToolInputGuardrailData) -> ToolGuardrailFunctionOutput:
        return ToolGuardrailFunctionOutput.allow()

    @tool_output_guardrail
    def bare_output(data: ToolOutputGuardrailData) -> ToolGuardrailFunctionOutput:
        return ToolGuardrailFunctionOutput.allow()

    @tool_output_guardrail(name="configured_output")
    def configured_output(data: ToolOutputGuardrailData) -> ToolGuardrailFunctionOutput:
        return ToolGuardrailFunctionOutput.allow()

    assert_type(bare_input, ToolInputGuardrail[Any])
    assert_type(configured_input, ToolInputGuardrail[Any])
    assert_type(bare_output, ToolOutputGuardrail[Any])
    assert_type(configured_output, ToolOutputGuardrail[Any])
    assert bare_input.get_name() == "bare_input"
    assert configured_input.get_name() == "configured_input"
    assert bare_output.get_name() == "bare_output"
    assert configured_output.get_name() == "configured_output"
