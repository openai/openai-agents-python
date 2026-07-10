"""Example 03: @function_tool internal mechanism.

Principle (agents/function_schema.py:224-424):
  @function_tool translates a Python function into a FunctionTool, process:
    1) griffe parses docstring (Google/NumPy/Sphinx auto-detected)
    2) typing.get_type_hints gets type hints, strips Annotated to get description
    3) inspect.signature gets parameters; first RunContextWrapper/ToolContext auto-marked as takes_context
    4) pydantic.create_model dynamically builds an "args model"
    5) ensure_strict_json_schema makes schema strict mode (OpenAI requirement)
  On call: JSON args -> pydantic validate -> func(**kwargs)

Observe three things:
  - tool.params_json_schema: actual strict schema sent to LLM (see how description comes from docstring)
  - tool.name / tool.description: how function name and docstring are derived
  - Simulate LLM calling tool: tool.on_invoke_tool(ctx, json_str)

Usage: python examples/03_function_tool_introspection.py
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from tools.calculator import add, divide  # type: ignore[import-not-found]
from tools.weather import get_weather  # type: ignore[import-not-found]

from agents.run_context import RunContextWrapper
from agents.tool_context import ToolContext


def show_schema(tool):
    print(f"  name:        {tool.name}")
    print(f"  description: {tool.description!r}")
    print("  schema.properties:")
    for prop_name, prop in tool.params_json_schema.get("properties", {}).items():
        print(f"    - {prop_name}: {prop.get('type')!r}  desc={prop.get('description')!r}")
    print(f"  required: {tool.params_json_schema.get('required')}")
    print("  Full JSON schema (excerpt):")
    print(json.dumps(tool.params_json_schema, indent=2, ensure_ascii=False)[:600] + "...")


def simulate_invoke(tool, label, json_args):
    print(f"\n  {label}: Tool triggered by LLM, passed JSON args = {json_args!r}")
    # ToolContext has many fields, we mock it
    ctx = ToolContext(
        context=RunContextWrapper(context=None),
        tool_name=tool.name,
        tool_call_id="call_test_001",
        tool_arguments=json_args,
    )
    # on_invoke_tool is async
    import asyncio

    result = asyncio.run(tool.on_invoke_tool(ctx, json_args))
    print(f"  on_invoke_tool() returns: {result!r}")


def main():
    print("\n weather.get_weather")
    show_schema(get_weather)
    simulate_invoke(get_weather, "LLM passes city='Beijing'", '{"city": "Beijing"}')

    print("\n calculator.add")
    show_schema(add)
    simulate_invoke(add, "LLM passes 5+3", '{"a": 5, "b": 3}')

    print("\n calculator.divide (with edge case)")
    show_schema(divide)
    simulate_invoke(divide, "LLM passes 100/4", '{"a": 100, "b": 4}')
    simulate_invoke(divide, "LLM passes 1/0 (boundary)", '{"a": 1, "b": 0}')


if __name__ == "__main__":
    main()
