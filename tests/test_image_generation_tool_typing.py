# pyright: reportCallIssue=true, reportArgumentType=true, reportUnnecessaryTypeIgnoreComment=true, reportAttributeAccessIssue=true, reportGeneralTypeIssues=true
"""Static canaries: invalid config operations must retain their expected diagnostics."""

from dataclasses import MISSING, dataclass, fields, replace
from typing import TYPE_CHECKING

from openai.types.responses.tool_param import ImageGeneration
from pydantic import TypeAdapter
from typing_extensions import assert_type

from agents import ImageGenerationTool, ImageGenerationToolConfig
from agents.tool import ImageGenerationToolConfig as ModuleConfig


@dataclass
class _RequiredImageGenerationTool(ImageGenerationTool):
    extra: int


if TYPE_CHECKING:
    ImageGenerationTool({"type": "image_generation"})
    ImageGenerationTool(
        {
            "type": "image_generation",
            "model": "gpt-image-2.5-sunburst",
            "quality": "max",
            "size": "1536x864",
            "action": "generate",
            "background": "transparent",
            "output_format": "webp",
            "output_compression": 0,
            "partial_images": 2,
            "moderation": "auto",
            "input_fidelity": None,
            "input_image_mask": {"file_id": "file-mask"},
        }
    )
    ImageGenerationTool(
        {"type": "image_generation", "model": "gpt-image-2.5-flare", "quality": "xhigh"}
    )
    legacy: ImageGeneration = {"type": "image_generation", "quality": "high"}
    tool = ImageGenerationTool(legacy)
    tool.tool_config = legacy
    tool.tool_config["quality"] = "max"
    config: ImageGenerationToolConfig = {"type": "image_generation", "quality": "max"}
    _RequiredImageGenerationTool(config, 1)
    _RequiredImageGenerationTool(legacy, 2)
    replace(tool, tool_config=config)
    replace(tool, tool_config=legacy)
    module_config: ModuleConfig = config
    ImageGenerationTool(module_config)
    tool.tool_config = config
    assert_type(tool.tool_config, ImageGenerationToolConfig)
    tool.tool_config = {"type": "image_generation", "quality": "xhigh"}
    tool.tool_config["quality"] = "max"

    tool.tool_config = {"type": "image_generaton"}  # pyright: ignore[reportAttributeAccessIssue]
    tool.tool_config = {"quality": "max"}  # pyright: ignore[reportAttributeAccessIssue]
    tool.tool_config = {"type": "image_generation", "qualty": "high"}  # pyright: ignore[reportAttributeAccessIssue]
    tool.tool_config["quality"] = "maxx"  # pyright: ignore[reportGeneralTypeIssues]
    tool.tool_config["qualty"] = "high"  # pyright: ignore[reportGeneralTypeIssues]
    tool.tool_config["type"] = "image_generaton"  # pyright: ignore[reportGeneralTypeIssues]

    ImageGenerationTool()  # pyright: ignore[reportCallIssue]
    _RequiredImageGenerationTool(extra=1)  # pyright: ignore[reportCallIssue]

    ImageGenerationTool({"type": "image_generaton"})  # pyright: ignore[reportArgumentType]
    ImageGenerationTool({"type": "image_generation", "qualty": "high"})  # pyright: ignore[reportArgumentType]
    ImageGenerationTool({"quality": "high"})  # pyright: ignore[reportArgumentType]
    ImageGenerationTool({"type": "image_generation", "quality": "maxx"})  # pyright: ignore[reportArgumentType]


def test_image_generation_config_public_import() -> None:
    import agents

    assert ImageGenerationToolConfig is ModuleConfig
    assert "ImageGenerationToolConfig" in agents.__all__


def test_image_generation_tool_pydantic_schema() -> None:
    adapter = TypeAdapter(ImageGenerationTool)
    schema = adapter.json_schema()
    config_schema = schema["$defs"]["ImageGenerationToolConfig"]
    assert schema["required"] == ["tool_config"]
    assert config_schema["required"] == ["type"]
    assert config_schema["properties"]["type"]["const"] == "image_generation"
    assert {"xhigh", "max"} <= set(config_schema["properties"]["quality"]["enum"])

    value = adapter.validate_python({"tool_config": {"type": "image_generation", "quality": "max"}})
    assert isinstance(value, ImageGenerationTool)
    assert value.tool_config["quality"] == "max"
    value.tool_config["quality"] = "xhigh"
    assert adapter.dump_python(value) == {
        "tool_config": {"type": "image_generation", "quality": "xhigh"}
    }


def test_image_generation_tool_required_subclass() -> None:
    config: ImageGenerationToolConfig = {"type": "image_generation", "quality": "max"}
    value = _RequiredImageGenerationTool(config, 1)
    assert value.tool_config is config
    assert value.extra == 1
    assert [(field.name, field.default) for field in fields(value)] == [
        ("tool_config", MISSING),
        ("extra", MISSING),
    ]
