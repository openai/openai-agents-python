import types

from typing_extensions import assert_type

import agents.decorators as decorators_module
import agents.tool as tool_module
from agents import (
    FunctionTool,
    function_tool,
    input_guardrail,
    output_guardrail,
    tool_input_guardrail,
    tool_output_guardrail,
)
from agents.decorators import function_tool as decorators_function_tool, tool


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
