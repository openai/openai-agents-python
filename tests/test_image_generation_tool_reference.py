from pathlib import Path

from griffe import visit  # type: ignore[import-untyped]


def test_image_generation_tool_reference_field() -> None:
    source_path = Path(__file__).parents[1] / "src" / "agents" / "tool.py"
    module = visit("agents.tool", source_path, source_path.read_text())
    config_field = module["ImageGenerationTool"]["tool_config"]
    assert str(config_field.annotation) == "ImageGenerationToolConfig"
    assert config_field.value is None
    assert config_field.docstring is not None
    assert 'including `type="image_generation"`' in config_field.docstring.value
    assert "Settings are forwarded unchanged" in config_field.docstring.value
