import asyncio
import json
from typing import Any

import pytest
from pydantic import BaseModel
from typing_extensions import TypedDict

from src.agents import (
    ImageFunctionTool,
    ModelBehaviorError,
    RunContextWrapper,
    image_function_tool,
)
from src.agents.tool import default_tool_error_function

# A dummy base64 encoded image string (e.g., a tiny 1x1 pixel red PNG)
DUMMY_IMAGE_BASE64 = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="

# =================== Basic Tests for image_function_tool ===================


def argless_image_function() -> str:
    return DUMMY_IMAGE_BASE64


@pytest.mark.asyncio
async def test_argless_image_function():
    tool = image_function_tool(argless_image_function)
    assert tool.name == "argless_image_function"

    result = await tool.on_invoke_tool(RunContextWrapper(None), "")
    assert result == DUMMY_IMAGE_BASE64


def argless_with_context_image(ctx: RunContextWrapper[str]) -> str:
    return DUMMY_IMAGE_BASE64


@pytest.mark.asyncio
async def test_argless_with_context_image():
    tool = image_function_tool(argless_with_context_image)
    assert tool.name == "argless_with_context_image"

    result = await tool.on_invoke_tool(RunContextWrapper(None), "")
    assert result == DUMMY_IMAGE_BASE64

    # Extra JSON should not raise an error
    result = await tool.on_invoke_tool(RunContextWrapper(None), '{"a": 1}')
    assert result == DUMMY_IMAGE_BASE64


def simple_image_function(prompt: str, style: str = "realistic") -> str:
    # In a real scenario, these parameters would affect the generated image
    return DUMMY_IMAGE_BASE64


@pytest.mark.asyncio
async def test_simple_image_function():
    tool = image_function_tool(simple_image_function, failure_error_function=None)
    assert tool.name == "simple_image_function"

    result = await tool.on_invoke_tool(RunContextWrapper(None), '{"prompt": "cat"}')
    assert result == DUMMY_IMAGE_BASE64

    result = await tool.on_invoke_tool(
        RunContextWrapper(None), '{"prompt": "dog", "style": "cartoon"}'
    )
    assert result == DUMMY_IMAGE_BASE64

    # Missing required argument should raise an error
    with pytest.raises(ModelBehaviorError):
        await tool.on_invoke_tool(RunContextWrapper(None), "")


class ImageParams(BaseModel):
    prompt: str
    width: int = 512
    height: int = 512


class StyleOptions(TypedDict):
    style: str
    seed: int


def complex_args_image_function(params: ImageParams, style_options: StyleOptions) -> str:
    # In a real scenario, these parameters would affect the generated image
    return DUMMY_IMAGE_BASE64


@pytest.mark.asyncio
async def test_complex_args_image_function():
    tool = image_function_tool(complex_args_image_function, failure_error_function=None)
    assert tool.name == "complex_args_image_function"

    valid_json = json.dumps(
        {
            "params": ImageParams(prompt="sunset").model_dump(),
            "style_options": StyleOptions(style="realistic", seed=42),
        }
    )
    result = await tool.on_invoke_tool(RunContextWrapper(None), valid_json)
    assert result == DUMMY_IMAGE_BASE64

    valid_json = json.dumps(
        {
            "params": ImageParams(prompt="mountains", width=1024, height=768).model_dump(),
            "style_options": StyleOptions(style="abstract", seed=123),
        }
    )
    result = await tool.on_invoke_tool(RunContextWrapper(None), valid_json)
    assert result == DUMMY_IMAGE_BASE64

    # Missing required argument should raise an error
    with pytest.raises(ModelBehaviorError):
        await tool.on_invoke_tool(RunContextWrapper(None), '{"params": {"prompt": "forest"}}')


def test_image_function_config_overrides():
    tool = image_function_tool(simple_image_function, name_override="custom_image_name")
    assert tool.name == "custom_image_name"

    tool = image_function_tool(simple_image_function, description_override="Generate custom images")
    assert tool.description == "Generate custom images"

    tool = image_function_tool(
        simple_image_function,
        name_override="art_generator",
        description_override="Creates beautiful art images",
    )
    assert tool.name == "art_generator"
    assert tool.description == "Creates beautiful art images"


def test_image_function_schema_is_strict():
    tool = image_function_tool(simple_image_function)
    assert tool.strict_json_schema, "Should be strict by default"
    assert (
        "additionalProperties" in tool.params_json_schema
        and not tool.params_json_schema["additionalProperties"]
    )

    tool = image_function_tool(complex_args_image_function)
    assert tool.strict_json_schema, "Should be strict by default"
    assert (
        "additionalProperties" in tool.params_json_schema
        and not tool.params_json_schema["additionalProperties"]
    )


@pytest.mark.asyncio
async def test_manual_image_function_tool_creation_works():
    def generate_image(prompt: str) -> str:
        return DUMMY_IMAGE_BASE64

    class ImageArgs(BaseModel):
        prompt: str

    async def run_function(ctx: RunContextWrapper[Any], args: str) -> str:
        parsed = ImageArgs.model_validate_json(args)
        return generate_image(prompt=parsed.prompt)

    tool = ImageFunctionTool(
        name="image_creator",
        description="Creates images from text prompts",
        params_json_schema=ImageArgs.model_json_schema(),
        on_invoke_tool=run_function,
    )

    assert tool.name == "image_creator"
    assert tool.description == "Creates images from text prompts"
    for key, value in ImageArgs.model_json_schema().items():
        assert tool.params_json_schema[key] == value
    assert tool.strict_json_schema

    result = await tool.on_invoke_tool(RunContextWrapper(None), '{"prompt": "sunset"}')
    assert result == DUMMY_IMAGE_BASE64

    tool_not_strict = ImageFunctionTool(
        name="image_creator",
        description="Creates images from text prompts",
        params_json_schema=ImageArgs.model_json_schema(),
        on_invoke_tool=run_function,
        strict_json_schema=False,
    )

    assert not tool_not_strict.strict_json_schema
    assert "additionalProperties" not in tool_not_strict.params_json_schema

    result = await tool_not_strict.on_invoke_tool(
        RunContextWrapper(None), '{"prompt": "sunset", "style": "realistic"}'
    )
    assert result == DUMMY_IMAGE_BASE64


@pytest.mark.asyncio
async def test_image_function_tool_default_error_works():
    def failing_image_generator(prompt: str) -> str:
        raise ValueError("Image generation failed")

    tool = image_function_tool(failing_image_generator)
    ctx = RunContextWrapper(None)

    result = await tool.on_invoke_tool(ctx, "")
    assert "Invalid JSON" in str(result)

    result = await tool.on_invoke_tool(ctx, "{}")
    assert "Invalid JSON" in str(result)

    result = await tool.on_invoke_tool(ctx, '{"prompt": "sunset"}')
    assert result == default_tool_error_function(ctx, ValueError("Image generation failed"))


@pytest.mark.asyncio
async def test_sync_custom_error_function_works_for_image_tool():
    def failing_image_generator(prompt: str) -> str:
        raise ValueError("Image generation failed")

    def custom_sync_error_function(ctx: RunContextWrapper[Any], error: Exception) -> str:
        return f"error_{error.__class__.__name__}_image"

    tool = image_function_tool(
        failing_image_generator, failure_error_function=custom_sync_error_function
    )
    ctx = RunContextWrapper(None)

    result = await tool.on_invoke_tool(ctx, "")
    assert result == "error_ModelBehaviorError_image"

    result = await tool.on_invoke_tool(ctx, "{}")
    assert result == "error_ModelBehaviorError_image"

    result = await tool.on_invoke_tool(ctx, '{"prompt": "sunset"}')
    assert result == "error_ValueError_image"


@pytest.mark.asyncio
async def test_async_image_generator():
    async def async_image_generator(prompt: str) -> str:
        # Simulate some async operation
        await asyncio.sleep(0.01)
        return DUMMY_IMAGE_BASE64

    tool = image_function_tool(async_image_generator)

    result = await tool.on_invoke_tool(RunContextWrapper(None), '{"prompt": "sunset"}')
    assert result == DUMMY_IMAGE_BASE64
