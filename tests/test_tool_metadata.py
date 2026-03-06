from __future__ import annotations

from typing import cast

from openai.types.responses.tool_param import CodeInterpreter, ImageGeneration, Mcp

from agents.computer import Computer
from agents.run_context import RunContextWrapper
from agents.tool import (
    ApplyPatchTool,
    CodeInterpreterTool,
    ComputerTool,
    FileSearchTool,
    HostedMCPTool,
    ImageGenerationTool,
    LocalShellTool,
    ShellCallOutcome,
    ShellCommandOutput,
    ShellTool,
    WebSearchTool,
)
from agents.tool_context import ToolContext


class DummyEditor:
    def create_file(self, operation):
        return None

    def update_file(self, operation):
        return None

    def delete_file(self, operation):
        return None


def test_tool_name_properties() -> None:
    dummy_computer = cast(Computer, object())
    dummy_mcp = cast(Mcp, {"type": "mcp", "server_label": "demo"})
    dummy_code = cast(CodeInterpreter, {"type": "code_interpreter", "container": "python"})
    dummy_image = cast(ImageGeneration, {"type": "image_generation", "model": "gpt-image-1"})

    assert FileSearchTool(vector_store_ids=[]).name == "file_search"
    assert WebSearchTool().name == "web_search"
    assert isinstance(ComputerTool(computer=dummy_computer).name, str)
    assert HostedMCPTool(tool_config=dummy_mcp).name == "hosted_mcp"
    assert CodeInterpreterTool(tool_config=dummy_code).name == "code_interpreter"
    assert ImageGenerationTool(tool_config=dummy_image).name == "image_generation"
    assert LocalShellTool(executor=lambda req: "ok").name == "local_shell"
    shell_tool = ShellTool(executor=lambda req: "ok")
    assert shell_tool.type == "shell"
    assert shell_tool.environment == {"type": "local"}
    assert ApplyPatchTool(editor=DummyEditor()).type == "apply_patch"


def test_shell_command_output_status_property() -> None:
    output = ShellCommandOutput(outcome=ShellCallOutcome(type="timeout"))
    assert output.status == "timeout"


def test_tool_context_from_agent_context() -> None:
    ctx = RunContextWrapper(context={"foo": "bar"})
    tool_call = ToolContext.from_agent_context(
        ctx,
        tool_call_id="123",
        tool_call=type(
            "Call",
            (),
            {
                "name": "demo",
                "arguments": "{}",
            },
        )(),
    )
    assert tool_call.tool_name == "demo"
