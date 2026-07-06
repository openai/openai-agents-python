from __future__ import annotations

from typing import Annotated, Any

import pytest
from pydantic import BaseModel, Field

from agents import FunctionTool, RunContextWrapper, function_tool
from agents.tool_context import ToolContext


@function_tool
def test_tool1(
    context: ToolContext,
    required_arg: str,
    optional_arg1: int = 0,
    optional_arg2: str | None = None,
) -> None:
    pass


@function_tool
def test_tool2(
    context: ToolContext,
    required_arg: Annotated[str, Field(..., description="Required argument")],
    optional_arg1: Annotated[int, Field(0, description="Optional argument 1")] = 0,
    optional_arg2: Annotated[str | None, Field(None, description="Optional argument 2")] = None,
) -> None:
    pass


class FunctionArgs(BaseModel):
    required_arg: str = Field(..., description="Required argument")
    optional_arg1: int = Field(default=0, description="Optional argument 1")
    optional_arg2: str | None = Field(None, description="Optional argument 2")


async def run_function(ctx: RunContextWrapper[Any], args: str) -> None:
    pass


test_tool3 = FunctionTool(
    name="process_user",
    description="Processes extracted user data",
    params_json_schema=FunctionArgs.model_json_schema(),
    on_invoke_tool=run_function,
)


@pytest.mark.asyncio
async def test_tool1_params_json_schema():
    params_json_schema = test_tool1.params_json_schema

    assert "required_arg" in params_json_schema["required"], "required_arg should be required"
    assert "optional_arg1" not in params_json_schema["required"], (
        "optional_arg should not be required"
    )
    assert "optional_arg2" not in params_json_schema["required"], (
        "optional_arg should not be required"
    )


@pytest.mark.asyncio
async def test_tool2_params_json_schema():
    params_json_schema = test_tool2.params_json_schema
    print(params_json_schema)
    assert "required_arg" in params_json_schema["required"], "required_arg should be required"
    assert "optional_arg1" not in params_json_schema["required"], (
        "optional_arg should not be required"
    )
    assert "optional_arg2" not in params_json_schema["required"], (
        "optional_arg should not be required"
    )


@pytest.mark.asyncio
async def test_tool3_params_json_schema():
    params_json_schema = test_tool3.params_json_schema
    print(params_json_schema)
    assert "required_arg" in params_json_schema["required"], "required_arg should be required"
    assert "optional_arg1" not in params_json_schema["required"], (
        "optional_arg should not be required"
    )
    assert "optional_arg2" not in params_json_schema["required"], (
        "optional_arg should not be required"
    )
