"""Public decorators for defining Agents SDK components.

`tool` is an alias for `function_tool`.
"""

from .guardrail import input_guardrail, output_guardrail
from .tool import function_tool
from .tool_guardrails import tool_input_guardrail, tool_output_guardrail

tool = function_tool

__all__ = [
    "function_tool",
    "input_guardrail",
    "output_guardrail",
    "tool",
    "tool_input_guardrail",
    "tool_output_guardrail",
]
