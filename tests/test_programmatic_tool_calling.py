from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Annotated, Any, Literal, cast

import pytest
from openai.types.responses import (
    ResponseApplyPatchToolCall,
    ResponseCustomToolCall,
    ResponseFileSearchToolCall,
    ResponseFunctionShellToolCall,
    ResponseFunctionShellToolCallOutput,
    ResponseFunctionToolCall,
    ResponseFunctionWebSearch,
    ResponseToolSearchCall,
    ResponseToolSearchOutputItem,
)
from openai.types.responses.response_apply_patch_tool_call import OperationCreateFile
from openai.types.responses.response_code_interpreter_tool_call import (
    ResponseCodeInterpreterToolCall,
)
from openai.types.responses.response_function_shell_tool_call import Action
from openai.types.responses.response_function_tool_call import CallerProgram
from openai.types.responses.response_function_web_search import ActionSearch
from openai.types.responses.response_output_item import (
    ImageGenerationCall,
    McpApprovalRequest,
    McpCall,
    McpListTools,
    Program,
    ProgramOutput,
)
from pydantic import BaseModel, ConfigDict, Field
from typing_extensions import TypedDict

from agents import (
    Agent,
    ApplyPatchTool,
    CodeInterpreterTool,
    CustomTool,
    HostedMCPTool,
    ModelResponse,
    ModelSettings,
    ProgrammaticToolCallingTool,
    RunConfig,
    RunItem,
    Runner,
    RunState,
    ShellTool,
    ToolCallItem,
    ToolCallOutputItem,
    ToolExecutionConfig,
    ToolGuardrailFunctionOutput,
    ToolInputGuardrailData,
    ToolOutputGuardrailData,
    ToolSearchTool,
    Usage,
    UserError,
    function_tool,
    tool_input_guardrail,
    tool_output_guardrail,
)
from agents.exceptions import ModelBehaviorError
from agents.items import ItemHelpers
from agents.memory import SQLiteSession
from agents.models.chatcmpl_converter import Converter as ChatCompletionsConverter
from agents.models.openai_responses import Converter as ResponsesConverter
from agents.run_internal.turn_resolution import process_model_response
from agents.tool_context import ToolContext

from .fake_model import FakeModel
from .test_responses import get_handoff_tool_call, get_text_message

PROGRAM_CALL_ID = "call_program"
FUNCTION_CALL_ID = "call_lookup"
PROGRAM_CALLER = {"type": "program", "caller_id": PROGRAM_CALL_ID}


class InventoryOutput(BaseModel):
    sku: str
    available_units: int


class InventoryDict(TypedDict):
    sku: str
    available_units: int


@dataclass
class InventoryData:
    sku: str
    available_units: int


class AliasedInventoryOutput(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    sku: str
    available_units: int = Field(
        validation_alias="inputUnits",
        serialization_alias="availableUnits",
    )


def _program() -> Program:
    return Program(
        id="program_item",
        call_id=PROGRAM_CALL_ID,
        code='lookup_inventory(sku="A-1")',
        fingerprint="fingerprint",
        type="program",
    )


def _function_call() -> ResponseFunctionToolCall:
    return ResponseFunctionToolCall(
        id="function_item",
        call_id=FUNCTION_CALL_ID,
        name="lookup_inventory",
        arguments='{"sku":"A-1"}',
        caller=CallerProgram(type="program", caller_id=PROGRAM_CALL_ID),
        type="function_call",
    )


def _program_output(
    status: Literal["completed", "incomplete"] = "completed",
) -> ProgramOutput:
    return ProgramOutput(
        id="program_output_item",
        call_id=PROGRAM_CALL_ID,
        result='{"sku":"A-1","available_units":42}',
        status=status,
        type="program_output",
    )


def _hosted_program_call_and_tool(
    output_type: str,
    allowed_callers: list[Any] | None,
) -> tuple[Any, Any]:
    if output_type in ("mcp_approval_request", "mcp_call", "mcp_list_tools"):
        mcp_config: dict[str, Any] = {
            "type": "mcp",
            "server_label": "docs_server",
            "server_url": "https://example.com/mcp",
        }
        if allowed_callers is not None:
            mcp_config["allowed_callers"] = allowed_callers
        hosted_tool = HostedMCPTool(tool_config=cast(Any, mcp_config))
        if output_type == "mcp_list_tools":
            return (
                McpListTools.model_construct(
                    id="mcp_list_tools_1",
                    server_label="docs_server",
                    tools=[],
                    type="mcp_list_tools",
                    caller=PROGRAM_CALLER,
                ),
                hosted_tool,
            )
        call_type: Any = McpApprovalRequest if output_type == "mcp_approval_request" else McpCall
        output = call_type.model_construct(
            id=f"{output_type}_1",
            arguments="{}",
            name="search_docs",
            server_label="docs_server",
            type=output_type,
            caller=PROGRAM_CALLER,
        )
        return output, hosted_tool

    code_interpreter_config: dict[str, Any] = {
        "type": "code_interpreter",
        "container": "auto",
    }
    if allowed_callers is not None:
        code_interpreter_config["allowed_callers"] = allowed_callers
    return (
        ResponseCodeInterpreterToolCall.model_construct(
            id="code_interpreter_1",
            container_id="container_1",
            status="completed",
            type="code_interpreter_call",
            caller=PROGRAM_CALLER,
        ),
        CodeInterpreterTool(tool_config=cast(Any, code_interpreter_config)),
    )


def _caller_dict(value: Any) -> dict[str, str]:
    if isinstance(value, dict):
        return cast(dict[str, str], value)
    return cast(dict[str, str], value.model_dump(exclude_none=True))


def _raw_item_type(value: Any) -> str | None:
    if isinstance(value, dict):
        item_type = value.get("type")
        return item_type if isinstance(item_type, str) else None
    item_type = getattr(value, "type", None)
    return item_type if isinstance(item_type, str) else None


def _function_output_raw_items(result: Any) -> list[dict[str, Any]]:
    return [
        cast(dict[str, Any], item.raw_item)
        for item in result.new_items
        if isinstance(item, ToolCallOutputItem)
        and isinstance(item.raw_item, dict)
        and item.raw_item.get("type") == "function_call_output"
    ]


def test_responses_converter_serializes_programmatic_tool_configuration() -> None:
    @function_tool(allowed_callers=["programmatic"])
    def lookup_inventory(sku: str) -> InventoryOutput:
        return InventoryOutput(sku=sku, available_units=42)

    converted = ResponsesConverter.convert_tools(
        tools=[ProgrammaticToolCallingTool(), lookup_inventory],
        handoffs=[],
    )

    assert converted.tools[0] == {"type": "programmatic_tool_calling"}
    function_payload = cast(dict[str, Any], converted.tools[1])
    assert function_payload["allowed_callers"] == ["programmatic"]
    assert function_payload["output_schema"] == lookup_inventory.output_json_schema
    assert function_payload["output_schema"] == {
        "additionalProperties": False,
        "properties": {
            "sku": {"title": "Sku", "type": "string"},
            "available_units": {"title": "Available Units", "type": "integer"},
        },
        "required": ["sku", "available_units"],
        "title": "InventoryOutput",
        "type": "object",
    }
    assert ResponsesConverter.convert_tool_choice("programmatic_tool_calling") == {
        "type": "programmatic_tool_calling"
    }


def test_function_tool_infers_typed_dict_and_dataclass_output_schemas() -> None:
    @function_tool(allowed_callers=["programmatic"])
    def typed_dict_tool() -> InventoryDict:
        return {"sku": "A-1", "available_units": 42}

    @function_tool(allowed_callers=["programmatic"])
    def dataclass_tool() -> InventoryData:
        return InventoryData(sku="A-1", available_units=42)

    assert typed_dict_tool.output_json_schema is not None
    assert typed_dict_tool.output_json_schema["type"] == "object"
    assert typed_dict_tool.output_json_schema["additionalProperties"] is False
    assert dataclass_tool.output_json_schema is not None
    assert dataclass_tool.output_json_schema["type"] == "object"
    assert dataclass_tool.output_json_schema["additionalProperties"] is False


def test_function_tool_treats_annotated_plain_returns_as_untyped() -> None:
    @function_tool(allowed_callers=["programmatic"])
    def string_tool() -> Annotated[str, "plain string"]:
        return "ok"

    @function_tool(allowed_callers=["programmatic"])
    def any_tool() -> Annotated[Any, "untyped value"]:
        return {"status": "ok"}

    @function_tool(allowed_callers=["programmatic"])
    def none_tool() -> Annotated[None, "no value"]:
        return None

    assert string_tool.output_json_schema is None
    assert string_tool._output_type_adapter is None
    assert any_tool.output_json_schema is None
    assert any_tool._output_type_adapter is None
    assert none_tool.output_json_schema is None
    assert none_tool._output_type_adapter is None


def test_function_tool_preserves_annotated_structured_return_metadata() -> None:
    @function_tool(allowed_callers=["programmatic"])
    def lookup_inventory() -> Annotated[
        InventoryOutput,
        Field(description="Inventory result"),
    ]:
        return InventoryOutput(sku="A-1", available_units=42)

    assert lookup_inventory.output_json_schema is not None
    assert lookup_inventory.output_json_schema["description"] == "Inventory result"
    assert lookup_inventory._output_type_adapter is not None


def test_function_tool_output_type_override_and_raw_schema_are_mutually_exclusive() -> None:
    def unannotated_tool() -> Any:
        return {"sku": "A-1", "available_units": 42}

    tool = function_tool(
        unannotated_tool,
        allowed_callers=["programmatic"],
        output_type=InventoryOutput,
    )

    assert tool.output_json_schema is not None
    assert tool.output_json_schema["title"] == "InventoryOutput"

    with pytest.raises(UserError, match="cannot both be provided"):
        function_tool(
            unannotated_tool,
            allowed_callers=["programmatic"],
            output_type=InventoryOutput,
            output_json_schema={"type": "object"},
        )

    with pytest.raises(UserError, match="output_type must define a strict JSON object"):
        function_tool(
            unannotated_tool,
            allowed_callers=["programmatic"],
            output_type=str,
        )


def test_function_tool_rejects_loose_programmatic_output_annotation() -> None:
    def loose_dict_tool() -> dict[str, Any]:
        return {"sku": "A-1", "available_units": 42}

    with pytest.raises(UserError, match="return annotation must define a strict JSON object"):
        function_tool(loose_dict_tool, allowed_callers=["programmatic"])


def test_function_tool_does_not_infer_non_programmatic_output() -> None:
    @function_tool
    def direct_tool() -> InventoryOutput:
        return InventoryOutput(sku="A-1", available_units=42)

    assert direct_tool.output_json_schema is None


@pytest.mark.parametrize(
    "output_json_schema",
    [
        {"type": "string"},
        {"type": "object", "additionalProperties": True},
    ],
)
def test_function_tool_rejects_non_object_or_non_strict_raw_output_schema(
    output_json_schema: dict[str, Any],
) -> None:
    with pytest.raises(UserError, match="output_json_schema must define a.*object schema"):
        function_tool(
            lambda: "ok",
            allowed_callers=["programmatic"],
            output_json_schema=output_json_schema,
        )


@pytest.mark.asyncio
async def test_function_tool_validates_inferred_output_type() -> None:
    @function_tool(allowed_callers=["programmatic"])
    def invalid_tool() -> InventoryOutput:
        return {"sku": "A-1", "available_units": "many"}  # type: ignore[return-value]

    context = ToolContext(
        None,
        tool_name=invalid_tool.name,
        tool_call_id="invalid",
        tool_arguments="{}",
        tool_call=_function_call(),
    )
    with pytest.raises(UserError, match="does not match its declared output type"):
        await invalid_tool.on_invoke_tool(context, "{}")


@pytest.mark.asyncio
async def test_schema_backed_programmatic_tool_bypasses_default_failure_formatter() -> None:
    @function_tool(allowed_callers=["programmatic"])
    def failing_tool() -> InventoryOutput:
        raise RuntimeError("inventory unavailable")

    context = ToolContext(
        None,
        tool_name=failing_tool.name,
        tool_call_id="failing",
        tool_arguments="{}",
        tool_call=_function_call(),
    )
    with pytest.raises(RuntimeError, match="inventory unavailable"):
        await failing_tool.on_invoke_tool(context, "{}")

    @function_tool(
        allowed_callers=["programmatic"],
        output_json_schema={
            "type": "object",
            "properties": {"error": {"type": "string"}},
            "required": ["error"],
            "additionalProperties": False,
        },
    )
    def failing_declared_schema_tool() -> str:
        raise RuntimeError("declared schema unavailable")

    declared_context = ToolContext(
        None,
        tool_name=failing_declared_schema_tool.name,
        tool_call_id="failing-declared",
        tool_arguments="{}",
        tool_call=_function_call(),
    )
    with pytest.raises(RuntimeError, match="declared schema unavailable"):
        await failing_declared_schema_tool.on_invoke_tool(declared_context, "{}")


@pytest.mark.asyncio
async def test_schema_backed_direct_tool_preserves_argument_error_formatter() -> None:
    @function_tool(allowed_callers=["direct", "programmatic"])
    def failing_tool(sku: str) -> InventoryOutput:
        return InventoryOutput(sku=sku, available_units=42)

    direct_call = ResponseFunctionToolCall(
        id="function_item",
        call_id=FUNCTION_CALL_ID,
        name=failing_tool.name,
        arguments="{}",
        type="function_call",
    )
    context = ToolContext(
        None,
        tool_name=failing_tool.name,
        tool_call_id=FUNCTION_CALL_ID,
        tool_arguments="{}",
        tool_call=direct_call,
    )

    result = await failing_tool.on_invoke_tool(context, "{}")

    assert result.startswith("An error occurred while running the tool. Please try again. Error:")
    assert "sku" in result


@pytest.mark.asyncio
async def test_runner_preserves_direct_error_for_schema_backed_tool() -> None:
    model = FakeModel()
    direct_call = ResponseFunctionToolCall(
        id="function_item",
        call_id=FUNCTION_CALL_ID,
        name="lookup_inventory",
        arguments='{"sku":"A-1"}',
        type="function_call",
    )
    model.add_multiple_turn_outputs([[direct_call], [get_text_message("inventory lookup failed")]])

    @function_tool(allowed_callers=["direct", "programmatic"])
    def lookup_inventory(sku: str) -> InventoryOutput:
        raise RuntimeError(f"inventory unavailable for {sku}")

    result = await Runner.run(
        Agent(name="inventory", model=model, tools=[lookup_inventory]),
        "Check inventory",
    )

    function_output = next(
        item for item in result.new_items if isinstance(item, ToolCallOutputItem)
    )
    expected_error = (
        "An error occurred while running the tool. Please try again. "
        "Error: inventory unavailable for A-1"
    )
    assert result.final_output == "inventory lookup failed"
    assert function_output.output == expected_error
    assert cast(dict[str, Any], function_output.raw_item)["output"] == expected_error


@pytest.mark.asyncio
async def test_runner_preserves_direct_default_timeout_for_schema_backed_tool() -> None:
    model = FakeModel()
    direct_call = ResponseFunctionToolCall(
        id="function_item",
        call_id=FUNCTION_CALL_ID,
        name="lookup_inventory",
        arguments='{"sku":"A-1"}',
        type="function_call",
    )
    model.add_multiple_turn_outputs([[direct_call], [get_text_message("timed out")]])

    @function_tool(allowed_callers=["direct", "programmatic"], timeout=0.01)
    async def lookup_inventory(sku: str) -> InventoryOutput:
        await asyncio.sleep(0.2)
        return InventoryOutput(sku=sku, available_units=42)

    result = await Runner.run(
        Agent(name="inventory", model=model, tools=[lookup_inventory]),
        "Check inventory",
    )

    function_output = next(
        item for item in result.new_items if isinstance(item, ToolCallOutputItem)
    )
    assert result.final_output == "timed out"
    assert isinstance(function_output.output, str)
    assert "timed out" in function_output.output.lower()
    assert cast(dict[str, Any], function_output.raw_item)["output"] == function_output.output


@pytest.mark.asyncio
async def test_schema_backed_function_tool_accepts_conforming_custom_error_output() -> None:
    @function_tool(
        allowed_callers=["programmatic"],
        failure_error_function=lambda _context, _error: json.dumps(
            {"sku": "ERROR", "available_units": 0}
        ),
    )
    def failing_tool() -> InventoryOutput:
        raise RuntimeError("inventory unavailable")

    context = ToolContext(
        None,
        tool_name=failing_tool.name,
        tool_call_id="failing",
        tool_arguments="{}",
    )
    result = await failing_tool.on_invoke_tool(context, "{}")
    output = ItemHelpers.tool_call_output_item(
        _function_call(),
        result,
        output_json_schema=failing_tool.output_json_schema,
        output_type_adapter=failing_tool._output_type_adapter,
    )

    assert json.loads(cast(str, output["output"])) == {
        "sku": "ERROR",
        "available_units": 0,
    }


@pytest.mark.asyncio
async def test_schema_backed_programmatic_tool_accepts_conforming_custom_timeout_output() -> None:
    model = FakeModel()
    model.add_multiple_turn_outputs(
        [
            [_program(), _function_call()],
            [_program_output(), get_text_message("timeout handled")],
        ]
    )

    @function_tool(
        allowed_callers=["programmatic"],
        timeout=0.01,
        timeout_error_function=lambda _context, _error: json.dumps(
            {"sku": "TIMEOUT", "available_units": 0}
        ),
    )
    async def lookup_inventory(sku: str) -> InventoryOutput:
        await asyncio.sleep(0.2)
        return InventoryOutput(sku=sku, available_units=42)

    result = await Runner.run(
        Agent(
            name="inventory",
            model=model,
            tools=[ProgrammaticToolCallingTool(), lookup_inventory],
        ),
        "Check inventory",
    )

    function_output = next(
        item for item in result.new_items if isinstance(item, ToolCallOutputItem)
    )
    assert result.final_output == "timeout handled"
    assert json.loads(cast(str, cast(dict[str, Any], function_output.raw_item)["output"])) == {
        "sku": "TIMEOUT",
        "available_units": 0,
    }


def test_schema_backed_function_output_rejects_plain_error_text() -> None:
    @function_tool(allowed_callers=["programmatic"])
    def lookup_inventory() -> InventoryOutput:
        return InventoryOutput(sku="A-1", available_units=42)

    with pytest.raises(UserError, match="does not match its declared output schema"):
        ItemHelpers.tool_call_output_item(
            _function_call(),
            "inventory unavailable",
            output_json_schema=lookup_inventory.output_json_schema,
            output_type_adapter=lookup_inventory._output_type_adapter,
        )

    with pytest.raises(UserError, match="requires a JSON object"):
        ItemHelpers.tool_call_output_item(
            _function_call(),
            "inventory unavailable",
            output_json_schema={"type": "object"},
        )


@pytest.mark.asyncio
async def test_function_tool_serializes_typed_output_with_schema_aliases() -> None:
    @function_tool(allowed_callers=["programmatic"])
    def aliased_tool() -> AliasedInventoryOutput:
        return AliasedInventoryOutput(sku="A-1", available_units=42)

    context = ToolContext(
        None,
        tool_name=aliased_tool.name,
        tool_call_id="aliased",
        tool_arguments="{}",
    )
    result = await aliased_tool.on_invoke_tool(context, "{}")
    output = ItemHelpers.tool_call_output_item(
        _function_call(),
        result,
        output_json_schema=aliased_tool.output_json_schema,
        output_type_adapter=aliased_tool._output_type_adapter,
    )

    assert aliased_tool.output_json_schema is not None
    assert "availableUnits" in aliased_tool.output_json_schema["properties"]
    assert json.loads(cast(str, output["output"])) == {
        "sku": "A-1",
        "availableUnits": 42,
    }


def test_responses_converter_serializes_allowed_callers_for_other_eligible_tools() -> None:
    async def shell_executor(_request: Any) -> str:
        return "ok"

    def custom_executor(_context: Any, _input: str) -> str:
        return "ok"

    class Editor:
        def create_file(self, _operation: Any) -> str:
            return "ok"

        def update_file(self, _operation: Any) -> str:
            return "ok"

        def delete_file(self, _operation: Any) -> str:
            return "ok"

    converted = ResponsesConverter.convert_tools(
        tools=[
            ProgrammaticToolCallingTool(),
            ShellTool(executor=shell_executor, allowed_callers=["programmatic"]),
            ApplyPatchTool(editor=Editor(), allowed_callers=["direct", "programmatic"]),
            CustomTool(
                name="custom",
                description="Custom tool",
                on_invoke_tool=custom_executor,
                allowed_callers=["programmatic"],
            ),
        ],
        handoffs=[],
    )

    tool_payloads = [cast(dict[str, Any], tool) for tool in converted.tools]
    assert tool_payloads[1]["allowed_callers"] == ["programmatic"]
    assert tool_payloads[2]["allowed_callers"] == ["direct", "programmatic"]
    assert tool_payloads[3]["allowed_callers"] == ["programmatic"]


@pytest.mark.parametrize(
    "allowed_callers",
    [
        [],
        ["direct", "direct"],
        ["unsupported"],
    ],
)
def test_tool_construction_rejects_invalid_allowed_callers(
    allowed_callers: list[Any],
) -> None:
    with pytest.raises(UserError, match="allowed_callers"):
        function_tool(lambda: "ok", allowed_callers=allowed_callers)

    with pytest.raises(UserError, match="allowed_callers"):
        ShellTool(executor=lambda _request: "ok", allowed_callers=allowed_callers)

    with pytest.raises(UserError, match="allowed_callers"):
        HostedMCPTool(
            tool_config=cast(
                Any,
                {
                    "type": "mcp",
                    "server_label": "inventory",
                    "server_url": "https://example.com/mcp",
                    "allowed_callers": allowed_callers,
                },
            )
        )

    with pytest.raises(UserError, match="allowed_callers"):
        CodeInterpreterTool(
            tool_config=cast(
                Any,
                {
                    "type": "code_interpreter",
                    "container": "auto",
                    "allowed_callers": allowed_callers,
                },
            )
        )


def test_responses_converter_rejects_incomplete_programmatic_configuration() -> None:
    @function_tool(allowed_callers=["programmatic"])
    def programmatic_only() -> str:
        return "ok"

    with pytest.raises(UserError, match="requires ProgrammaticToolCallingTool"):
        ResponsesConverter.convert_tools(tools=[programmatic_only], handoffs=[])

    with pytest.raises(UserError, match="requires ProgrammaticToolCallingTool"):
        ResponsesConverter.convert_tools(
            tools=[],
            handoffs=[],
            tool_choice="programmatic_tool_calling",
        )

    with pytest.raises(UserError, match="requires at least one tool"):
        ResponsesConverter.convert_tools(
            tools=[ProgrammaticToolCallingTool()],
            handoffs=[],
        )

    with pytest.raises(UserError, match="Only one ProgrammaticToolCallingTool"):
        ResponsesConverter.convert_tools(
            tools=[ProgrammaticToolCallingTool(), ProgrammaticToolCallingTool()],
            handoffs=[],
        )


def test_responses_converter_accepts_mixed_or_tool_search_managed_configuration() -> None:
    @function_tool(allowed_callers=["direct", "programmatic"])
    def mixed_callers() -> str:
        return "ok"

    converted_mixed = ResponsesConverter.convert_tools(tools=[mixed_callers], handoffs=[])
    assert cast(dict[str, Any], converted_mixed.tools[0])["allowed_callers"] == [
        "direct",
        "programmatic",
    ]

    converted_search = ResponsesConverter.convert_tools(
        tools=[ProgrammaticToolCallingTool(), ToolSearchTool()],
        handoffs=[],
        allow_opaque_tool_search_surface=True,
    )
    assert converted_search.tools == [
        {"type": "programmatic_tool_calling"},
        {"type": "tool_search"},
    ]


def test_chat_completions_rejects_programmatic_tool_configuration() -> None:
    @function_tool(allowed_callers=["programmatic"])
    def lookup_inventory() -> InventoryOutput:
        return InventoryOutput(sku="A-1", available_units=42)

    with pytest.raises(UserError, match="only supported with OpenAI Responses models"):
        ChatCompletionsConverter.tool_to_openai(lookup_inventory)

    with pytest.raises(UserError, match="programmatic_tool_calling"):
        ChatCompletionsConverter.convert_tool_choice("programmatic_tool_calling")

    with pytest.raises(UserError, match="Hosted tools are not supported"):
        ChatCompletionsConverter.tool_to_openai(ProgrammaticToolCallingTool())


def test_function_output_preserves_caller_and_uses_declared_json_schema() -> None:
    output = ItemHelpers.tool_call_output_item(
        _function_call(),
        {"sku": "A-1", "available_units": 42},
        output_json_schema={"type": "object"},
    )

    assert json.loads(cast(str, output["output"])) == {
        "sku": "A-1",
        "available_units": 42,
    }
    assert _caller_dict(output["caller"]) == PROGRAM_CALLER

    programmatic_output = ItemHelpers.tool_call_output_item(
        _function_call(),
        {"sku": "A-1", "units": [1, 2]},
    )
    assert json.loads(cast(str, programmatic_output["output"])) == {
        "sku": "A-1",
        "units": [1, 2],
    }
    assert _caller_dict(programmatic_output["caller"]) == PROGRAM_CALLER

    direct_call = ResponseFunctionToolCall(
        id="direct_function_item",
        call_id="direct_call",
        name="lookup_inventory",
        arguments="{}",
        type="function_call",
    )
    legacy_output = ItemHelpers.tool_call_output_item(direct_call, {"sku": "A-1"})
    assert legacy_output["output"] == "{'sku': 'A-1'}"


def test_process_model_response_keeps_program_items_in_order() -> None:
    @function_tool(allowed_callers=["programmatic"])
    def lookup_inventory(sku: str) -> str:
        return sku

    agent = Agent(
        name="inventory",
        tools=[ProgrammaticToolCallingTool(), lookup_inventory],
    )
    response = ModelResponse(
        output=[_program(), _function_call(), _program_output("incomplete")],
        usage=Usage(),
        response_id="response_1",
    )

    processed = process_model_response(
        agent=agent,
        all_tools=agent.tools,
        response=response,
        output_schema=None,
        handoffs=[],
    )

    assert [type(item) for item in processed.new_items] == [
        ToolCallItem,
        ToolCallItem,
        ToolCallOutputItem,
    ]
    assert [_raw_item_type(item.raw_item) for item in processed.new_items] == [
        "program",
        "function_call",
        "program_output",
    ]
    assert processed.tools_used == [
        "programmatic_tool_calling",
        "lookup_inventory",
        "programmatic_tool_calling",
    ]


@pytest.mark.parametrize("call_id", [None, ""])
def test_process_model_response_rejects_program_without_valid_call_id(
    call_id: str | None,
) -> None:
    program: dict[str, Any] = {
        "type": "program",
        "id": "program_item",
        "code": "return 42",
        "fingerprint": "fingerprint",
    }
    if call_id is not None:
        program["call_id"] = call_id

    response = ModelResponse(output=[], usage=Usage(), response_id="response_1")
    response.output = cast(Any, [program])
    agent = Agent(name="inventory", tools=[ProgrammaticToolCallingTool()])

    with pytest.raises(ModelBehaviorError, match="without a valid call_id"):
        process_model_response(
            agent=agent,
            all_tools=agent.tools,
            response=response,
            output_schema=None,
            handoffs=[],
        )


@pytest.mark.parametrize(
    "program_output",
    [_program_output(), _program_output().model_dump(exclude_none=True)],
)
def test_process_model_response_rejects_orphan_program_output(program_output: Any) -> None:
    agent = Agent(name="inventory", tools=[ProgrammaticToolCallingTool()])

    with pytest.raises(ModelBehaviorError, match="does not match a parent program item"):
        process_model_response(
            agent=agent,
            all_tools=agent.tools,
            response=ModelResponse(
                output=[program_output],
                usage=Usage(),
                response_id="response_1",
            ),
            output_schema=None,
            handoffs=[],
        )


def test_process_model_response_accepts_program_output_for_retained_program() -> None:
    agent = Agent(name="inventory", tools=[ProgrammaticToolCallingTool()])
    existing_program = ToolCallItem(raw_item=_program(), agent=agent)

    processed = process_model_response(
        agent=agent,
        all_tools=agent.tools,
        response=ModelResponse(
            output=[_program_output()],
            usage=Usage(),
            response_id="response_1",
        ),
        output_schema=None,
        handoffs=[],
        existing_items=[existing_program],
    )

    assert len(processed.new_items) == 1
    assert isinstance(processed.new_items[0], ToolCallOutputItem)


@pytest.mark.parametrize(
    ("field", "value", "remove_field", "error_match"),
    [
        ("status", None, True, "without a valid status"),
        ("status", "running", False, "without a valid status"),
        ("result", None, True, "without a string result"),
        ("result", 42, False, "without a string result"),
    ],
)
def test_process_model_response_rejects_malformed_program_output(
    field: str,
    value: Any,
    remove_field: bool,
    error_match: str,
) -> None:
    agent = Agent(name="inventory", tools=[ProgrammaticToolCallingTool()])
    program_output = _program_output().model_dump(exclude_none=True)
    if remove_field:
        program_output.pop(field)
    else:
        program_output[field] = value
    response = ModelResponse(output=[], usage=Usage(), response_id="response_1")
    response.output = cast(Any, [_program(), program_output])

    with pytest.raises(ModelBehaviorError, match=error_match):
        process_model_response(
            agent=agent,
            all_tools=agent.tools,
            response=response,
            output_schema=None,
            handoffs=[],
        )


@pytest.mark.parametrize("parent_location", ["existing_items", "current_response"])
@pytest.mark.parametrize("as_mapping", [False, True])
def test_process_model_response_rejects_duplicate_completed_program_output(
    parent_location: str,
    as_mapping: bool,
) -> None:
    agent = Agent(name="inventory", tools=[ProgrammaticToolCallingTool()])
    program: Any = _program()
    completed_output: Any = _program_output()
    duplicate_output: Any = _program_output().model_copy(
        update={"id": "duplicate_program_output_item"}
    )
    if as_mapping:
        program = program.model_dump(exclude_none=True)
        completed_output = completed_output.model_dump(exclude_none=True)
        duplicate_output = duplicate_output.model_dump(exclude_none=True)

    response_output = [duplicate_output]
    existing_items: list[RunItem] = []
    if parent_location == "existing_items":
        existing_items = [
            ToolCallItem(raw_item=program, agent=agent),
            ToolCallOutputItem(
                raw_item=completed_output,
                output=(
                    completed_output["result"]
                    if isinstance(completed_output, dict)
                    else completed_output.result
                ),
                agent=agent,
            ),
        ]
    else:
        response_output = [program, completed_output, duplicate_output]

    with pytest.raises(ModelBehaviorError, match="parent program is already completed"):
        process_model_response(
            agent=agent,
            all_tools=agent.tools,
            response=ModelResponse(
                output=response_output,
                usage=Usage(),
                response_id="response_1",
            ),
            output_schema=None,
            handoffs=[],
            existing_items=existing_items,
        )


@pytest.mark.parametrize("as_mapping", [False, True])
def test_process_model_response_rejects_program_owned_call_for_completed_retained_program(
    as_mapping: bool,
) -> None:
    @function_tool(allowed_callers=["programmatic"])
    def lookup_inventory(sku: str) -> str:
        return sku

    agent = Agent(
        name="inventory",
        tools=[ProgrammaticToolCallingTool(), lookup_inventory],
    )
    program: Any = _program()
    program_output: Any = _program_output()
    tool_call: Any = _function_call()
    if as_mapping:
        program = program.model_dump(exclude_none=True)
        program_output = program_output.model_dump(exclude_none=True)
        tool_call = tool_call.model_dump(exclude_none=True)

    existing_items: list[RunItem] = [
        ToolCallItem(raw_item=program, agent=agent),
        ToolCallOutputItem(
            raw_item=program_output,
            output=getattr(program_output, "result", None)
            or cast(dict[str, Any], program_output)["result"],
            agent=agent,
        ),
    ]

    with pytest.raises(ModelBehaviorError, match="parent program is already completed"):
        process_model_response(
            agent=agent,
            all_tools=agent.tools,
            response=ModelResponse(output=[tool_call], usage=Usage(), response_id="response_1"),
            output_schema=None,
            handoffs=[],
            existing_items=existing_items,
        )


@pytest.mark.parametrize("as_mapping", [False, True])
@pytest.mark.parametrize("call_before_completed_output", [False, True])
def test_process_model_response_rejects_program_owned_call_when_response_completes_program(
    as_mapping: bool,
    call_before_completed_output: bool,
) -> None:
    @function_tool(allowed_callers=["programmatic"])
    def lookup_inventory(sku: str) -> str:
        return sku

    agent = Agent(
        name="inventory",
        tools=[ProgrammaticToolCallingTool(), lookup_inventory],
    )
    child_items = (
        [_function_call(), _program_output()]
        if call_before_completed_output
        else [_program_output(), _function_call()]
    )
    output: list[Any] = [_program(), *child_items]
    if as_mapping:
        output = [item.model_dump(exclude_none=True) for item in output]

    with pytest.raises(ModelBehaviorError, match="parent program is already completed"):
        process_model_response(
            agent=agent,
            all_tools=agent.tools,
            response=ModelResponse(output=output, usage=Usage(), response_id="response_1"),
            output_schema=None,
            handoffs=[],
        )


@pytest.mark.parametrize("as_mapping", [False, True])
def test_process_model_response_accepts_program_owned_call_for_incomplete_retained_program(
    as_mapping: bool,
) -> None:
    @function_tool(allowed_callers=["programmatic"])
    def lookup_inventory(sku: str) -> str:
        return sku

    agent = Agent(
        name="inventory",
        tools=[ProgrammaticToolCallingTool(), lookup_inventory],
    )
    program: Any = _program()
    program_output: Any = _program_output("incomplete")
    tool_call: Any = _function_call()
    if as_mapping:
        program = program.model_dump(exclude_none=True)
        program_output = program_output.model_dump(exclude_none=True)
        tool_call = tool_call.model_dump(exclude_none=True)

    existing_items: list[RunItem] = [
        ToolCallItem(raw_item=program, agent=agent),
        ToolCallOutputItem(
            raw_item=program_output,
            output=getattr(program_output, "result", None)
            or cast(dict[str, Any], program_output)["result"],
            agent=agent,
        ),
    ]
    processed = process_model_response(
        agent=agent,
        all_tools=agent.tools,
        response=ModelResponse(output=[tool_call], usage=Usage(), response_id="response_1"),
        output_schema=None,
        handoffs=[],
        existing_items=existing_items,
    )

    assert len(processed.new_items) == 1
    assert _raw_item_type(processed.new_items[0].raw_item) == "function_call"


@pytest.mark.parametrize(
    "output",
    [
        _program(),
        _program().model_dump(exclude_none=True),
        _program_output(),
        _program_output().model_dump(exclude_none=True),
    ],
)
def test_process_model_response_rejects_program_items_without_programmatic_tool(
    output: Any,
) -> None:
    agent = Agent(name="inventory")

    with pytest.raises(ModelBehaviorError, match="programmatic_tool_calling tool"):
        process_model_response(
            agent=agent,
            all_tools=agent.tools,
            response=ModelResponse(output=[output], usage=Usage(), response_id="response_1"),
            output_schema=None,
            handoffs=[],
        )


@pytest.mark.asyncio
async def test_runner_rejects_program_item_without_programmatic_tool() -> None:
    model = FakeModel()
    model.set_next_output([_program()])
    agent = Agent(name="inventory", model=model)

    with pytest.raises(ModelBehaviorError, match="programmatic_tool_calling tool"):
        await Runner.run(agent, "Check inventory")


def test_process_model_response_rejects_program_owned_calls_without_programmatic_tool() -> None:
    @function_tool(allowed_callers=["programmatic"])
    def lookup_inventory(sku: str) -> str:
        return sku

    async def shell_executor(_request: Any) -> str:
        return "ok"

    shell_tool = ShellTool(executor=shell_executor, allowed_callers=["programmatic"])
    shell_call = ResponseFunctionShellToolCall(
        id="shell_item",
        call_id="call_shell",
        action=Action(commands=["echo ok"]),
        status="completed",
        type="shell_call",
        caller=cast(Any, PROGRAM_CALLER),
    )
    custom_tool = CustomTool(
        name="custom",
        description="Custom tool",
        on_invoke_tool=lambda _context, _input: "ok",
        allowed_callers=["programmatic"],
    )
    custom_call = ResponseCustomToolCall(
        id="custom_item",
        call_id="call_custom",
        input="input",
        name="custom",
        type="custom_tool_call",
        caller=cast(Any, PROGRAM_CALLER),
    )

    cases: list[tuple[Any, Any]] = [
        (lookup_inventory, _function_call()),
        (lookup_inventory, _function_call().model_dump(exclude_none=True)),
        (shell_tool, shell_call),
        (shell_tool, shell_call.model_dump(exclude_none=True)),
        (custom_tool, custom_call),
        (custom_tool, custom_call.model_dump(exclude_none=True)),
    ]
    for tool, tool_call in cases:
        agent = Agent(name="tool agent", tools=[tool])
        with pytest.raises(ModelBehaviorError, match="programmatic_tool_calling tool"):
            process_model_response(
                agent=agent,
                all_tools=agent.tools,
                response=ModelResponse(output=[tool_call], usage=Usage(), response_id="response_1"),
                output_schema=None,
                handoffs=[],
            )


@pytest.mark.asyncio
async def test_runner_does_not_execute_program_owned_call_without_programmatic_tool() -> None:
    model = FakeModel()
    model.set_next_output([_function_call()])
    executed = False

    @function_tool(allowed_callers=["programmatic"])
    def lookup_inventory(sku: str) -> str:
        nonlocal executed
        executed = True
        return sku

    agent = Agent(name="inventory", model=model, tools=[lookup_inventory])

    with pytest.raises(ModelBehaviorError, match="programmatic_tool_calling tool"):
        await Runner.run(agent, "Check inventory")

    assert executed is False


def test_process_model_response_rejects_program_owned_calls_without_parent_program() -> None:
    @function_tool(allowed_callers=["programmatic"])
    def lookup_inventory(sku: str) -> str:
        return sku

    async def shell_executor(_request: Any) -> str:
        return "ok"

    shell_tool = ShellTool(executor=shell_executor, allowed_callers=["programmatic"])
    shell_call = ResponseFunctionShellToolCall(
        id="shell_item",
        call_id="call_shell",
        action=Action(commands=["echo ok"]),
        status="completed",
        type="shell_call",
        caller=cast(Any, PROGRAM_CALLER),
    )
    custom_tool = CustomTool(
        name="custom",
        description="Custom tool",
        on_invoke_tool=lambda _context, _input: "ok",
        allowed_callers=["programmatic"],
    )
    custom_call = ResponseCustomToolCall(
        id="custom_item",
        call_id="call_custom",
        input="input",
        name="custom",
        type="custom_tool_call",
        caller=cast(Any, PROGRAM_CALLER),
    )

    cases: list[tuple[Any, Any]] = [
        (lookup_inventory, _function_call()),
        (lookup_inventory, _function_call().model_dump(exclude_none=True)),
        (shell_tool, shell_call),
        (shell_tool, shell_call.model_dump(exclude_none=True)),
        (custom_tool, custom_call),
        (custom_tool, custom_call.model_dump(exclude_none=True)),
    ]
    for tool, tool_call in cases:
        agent = Agent(name="tool agent", tools=[ProgrammaticToolCallingTool(), tool])
        with pytest.raises(ModelBehaviorError, match="parent program item"):
            process_model_response(
                agent=agent,
                all_tools=agent.tools,
                response=ModelResponse(output=[tool_call], usage=Usage(), response_id="response_1"),
                output_schema=None,
                handoffs=[],
            )


@pytest.mark.parametrize("parent_location", ["current_response", "existing_items"])
@pytest.mark.parametrize("as_mapping", [False, True])
def test_process_model_response_accepts_program_owned_call_with_parent_program(
    parent_location: str,
    as_mapping: bool,
) -> None:
    @function_tool(allowed_callers=["programmatic"])
    def lookup_inventory(sku: str) -> str:
        return sku

    agent = Agent(
        name="inventory",
        tools=[ProgrammaticToolCallingTool(), lookup_inventory],
    )
    tool_call: Any = _function_call()
    parent_program: Any = _program()
    if as_mapping:
        tool_call = tool_call.model_dump(exclude_none=True)
        parent_program = parent_program.model_dump(exclude_none=True)

    response_output: list[Any] = [tool_call]
    existing_items: list[ToolCallItem] = []
    if parent_location == "current_response":
        response_output.insert(0, parent_program)
    else:
        existing_items.append(ToolCallItem(raw_item=parent_program, agent=agent))

    processed = process_model_response(
        agent=agent,
        all_tools=agent.tools,
        response=ModelResponse(output=response_output, usage=Usage(), response_id="response_1"),
        output_schema=None,
        handoffs=[],
        existing_items=existing_items,
    )

    assert "function_call" in {_raw_item_type(item.raw_item) for item in processed.new_items}


@pytest.mark.parametrize("child_type", ["function_call", "program_output"])
@pytest.mark.parametrize("as_mapping", [False, True])
def test_process_model_response_rejects_program_child_before_parent(
    child_type: str,
    as_mapping: bool,
) -> None:
    @function_tool(allowed_callers=["programmatic"])
    def lookup_inventory(sku: str) -> str:
        return sku

    agent = Agent(
        name="inventory",
        tools=[ProgrammaticToolCallingTool(), lookup_inventory],
    )
    child: Any = _function_call() if child_type == "function_call" else _program_output()
    parent: Any = _program()
    if as_mapping:
        child = child.model_dump(exclude_none=True)
        parent = parent.model_dump(exclude_none=True)

    with pytest.raises(ModelBehaviorError, match="parent program item"):
        process_model_response(
            agent=agent,
            all_tools=agent.tools,
            response=ModelResponse(
                output=[child, parent],
                usage=Usage(),
                response_id="response_1",
            ),
            output_schema=None,
            handoffs=[],
        )


@pytest.mark.parametrize("child_type", ["function_call", "program_output"])
@pytest.mark.parametrize("as_mapping", [False, True])
def test_process_model_response_accepts_server_owned_parent_from_submitted_delta(
    child_type: str,
    as_mapping: bool,
) -> None:
    @function_tool(allowed_callers=["programmatic"])
    def lookup_inventory(sku: str) -> str:
        return sku

    agent = Agent(
        name="inventory",
        tools=[ProgrammaticToolCallingTool(), lookup_inventory],
    )
    child: Any = _function_call() if child_type == "function_call" else _program_output()
    if as_mapping:
        child = child.model_dump(exclude_none=True)
    submitted_delta = [
        {
            "type": "function_call_output",
            "call_id": FUNCTION_CALL_ID,
            "output": '{"sku":"A-1","available_units":42}',
            "caller": PROGRAM_CALLER,
        }
    ]

    processed = process_model_response(
        agent=agent,
        all_tools=agent.tools,
        response=ModelResponse(output=[child], usage=Usage(), response_id="response_1"),
        output_schema=None,
        handoffs=[],
        server_manages_conversation=True,
        server_managed_input_items=submitted_delta,
    )

    assert child_type in {_raw_item_type(item.raw_item) for item in processed.new_items}


def test_process_model_response_rejects_server_owned_child_without_parent_evidence() -> None:
    @function_tool(allowed_callers=["programmatic"])
    def lookup_inventory(sku: str) -> str:
        return sku

    agent = Agent(
        name="inventory",
        tools=[ProgrammaticToolCallingTool(), lookup_inventory],
    )

    with pytest.raises(ModelBehaviorError, match="parent program item"):
        process_model_response(
            agent=agent,
            all_tools=agent.tools,
            response=ModelResponse(
                output=[_function_call()],
                usage=Usage(),
                response_id="response_1",
            ),
            output_schema=None,
            handoffs=[],
            server_manages_conversation=True,
        )


@pytest.mark.parametrize("as_mapping", [False, True])
def test_process_model_response_accepts_server_owned_parent_after_incomplete_output(
    as_mapping: bool,
) -> None:
    @function_tool(allowed_callers=["programmatic"])
    def lookup_inventory(sku: str) -> str:
        return sku

    agent = Agent(
        name="inventory",
        tools=[ProgrammaticToolCallingTool(), lookup_inventory],
    )
    prior_output: Any = _program_output("incomplete")
    if as_mapping:
        prior_output = prior_output.model_dump(exclude_none=True)

    processed = process_model_response(
        agent=agent,
        all_tools=agent.tools,
        response=ModelResponse(
            output=[_function_call()],
            usage=Usage(),
            response_id="response_2",
        ),
        output_schema=None,
        handoffs=[],
        existing_items=[
            ToolCallOutputItem(
                raw_item=prior_output,
                output='{"status":"waiting"}',
                agent=agent,
            )
        ],
        server_manages_conversation=True,
    )

    assert _raw_item_type(processed.new_items[0].raw_item) == "function_call"


@pytest.mark.asyncio
async def test_runner_does_not_execute_program_owned_call_without_parent_program() -> None:
    model = FakeModel()
    model.set_next_output([_function_call()])
    executed = False

    @function_tool(allowed_callers=["programmatic"])
    def lookup_inventory(sku: str) -> str:
        nonlocal executed
        executed = True
        return sku

    agent = Agent(
        name="inventory",
        model=model,
        tools=[ProgrammaticToolCallingTool(), lookup_inventory],
    )

    with pytest.raises(ModelBehaviorError, match="parent program item"):
        await Runner.run(agent, "Check inventory")

    assert executed is False


@pytest.mark.parametrize(
    ("allowed_callers", "caller", "expected_caller"),
    [
        (None, CallerProgram(type="program", caller_id=PROGRAM_CALL_ID), "programmatic"),
        (["direct"], CallerProgram(type="program", caller_id=PROGRAM_CALL_ID), "programmatic"),
        (["programmatic"], None, "direct"),
    ],
)
def test_process_model_response_rejects_disallowed_function_callers(
    allowed_callers: list[Any] | None,
    caller: CallerProgram | None,
    expected_caller: str,
) -> None:
    @function_tool(allowed_callers=allowed_callers)
    def lookup_inventory(sku: str) -> str:
        return sku

    agent = Agent(
        name="inventory",
        tools=[ProgrammaticToolCallingTool(), lookup_inventory],
    )
    tool_call = ResponseFunctionToolCall(
        id="function_item",
        call_id=FUNCTION_CALL_ID,
        name="lookup_inventory",
        arguments='{"sku":"A-1"}',
        caller=caller,
        type="function_call",
    )
    response_output: list[Any] = [_program(), tool_call] if caller is not None else [tool_call]

    with pytest.raises(ModelBehaviorError, match=f"caller {expected_caller}"):
        process_model_response(
            agent=agent,
            all_tools=agent.tools,
            response=ModelResponse(output=response_output, usage=Usage(), response_id="response_1"),
            output_schema=None,
            handoffs=[],
        )


def test_process_model_response_rejects_unknown_function_caller_type() -> None:
    @function_tool
    def lookup_inventory(sku: str) -> str:
        return sku

    agent = Agent(name="inventory", tools=[lookup_inventory])
    tool_call = ResponseFunctionToolCall.model_construct(
        id="function_item",
        call_id=FUNCTION_CALL_ID,
        name="lookup_inventory",
        arguments='{"sku":"A-1"}',
        caller={"type": "unknown", "caller_id": PROGRAM_CALL_ID},
        type="function_call",
    )

    with pytest.raises(ModelBehaviorError, match="unsupported caller type 'unknown'"):
        process_model_response(
            agent=agent,
            all_tools=agent.tools,
            response=ModelResponse(output=[tool_call], usage=Usage(), response_id="response_1"),
            output_schema=None,
            handoffs=[],
        )


def test_process_model_response_rejects_disallowed_non_function_callers() -> None:
    caller = cast(Any, PROGRAM_CALLER)

    async def shell_executor(_request: Any) -> str:
        return "ok"

    shell_tool = ShellTool(executor=shell_executor)
    programmatic_tool = ProgrammaticToolCallingTool()
    shell_call = ResponseFunctionShellToolCall(
        id="shell_item",
        call_id="call_shell",
        action=Action(commands=["echo ok"]),
        status="completed",
        type="shell_call",
        caller=caller,
    )
    with pytest.raises(ModelBehaviorError, match="caller programmatic"):
        process_model_response(
            agent=Agent(name="shell", tools=[programmatic_tool, shell_tool]),
            all_tools=[programmatic_tool, shell_tool],
            response=ModelResponse(
                output=[_program(), shell_call], usage=Usage(), response_id="response_1"
            ),
            output_schema=None,
            handoffs=[],
        )

    custom_tool = CustomTool(
        name="custom",
        description="Custom tool",
        on_invoke_tool=lambda _context, _input: "ok",
    )
    custom_call = ResponseCustomToolCall(
        id="custom_item",
        call_id="call_custom",
        input="input",
        name="custom",
        type="custom_tool_call",
        caller=caller,
    )
    with pytest.raises(ModelBehaviorError, match="caller programmatic"):
        process_model_response(
            agent=Agent(name="custom", tools=[programmatic_tool, custom_tool]),
            all_tools=[programmatic_tool, custom_tool],
            response=ModelResponse(
                output=[_program(), custom_call], usage=Usage(), response_id="response_1"
            ),
            output_schema=None,
            handoffs=[],
        )

    class Editor:
        def create_file(self, _operation: Any) -> str:
            return "ok"

        def update_file(self, _operation: Any) -> str:
            return "ok"

        def delete_file(self, _operation: Any) -> str:
            return "ok"

    apply_patch_tool = ApplyPatchTool(editor=Editor(), allowed_callers=["programmatic"])
    apply_patch_call = ResponseApplyPatchToolCall(
        id="apply_patch_item",
        call_id="call_apply_patch",
        operation=OperationCreateFile(type="create_file", path="example.txt", diff="hello"),
        status="completed",
        type="apply_patch_call",
    )
    with pytest.raises(ModelBehaviorError, match="caller direct"):
        process_model_response(
            agent=Agent(name="apply patch", tools=[programmatic_tool, apply_patch_tool]),
            all_tools=[programmatic_tool, apply_patch_tool],
            response=ModelResponse(
                output=[apply_patch_call], usage=Usage(), response_id="response_1"
            ),
            output_schema=None,
            handoffs=[],
        )


@pytest.mark.parametrize("as_mapping", [False, True])
@pytest.mark.parametrize(
    "tool_call",
    [
        ResponseFileSearchToolCall.model_construct(
            id="file_search_1",
            queries=["inventory"],
            status="completed",
            type="file_search_call",
            caller=PROGRAM_CALLER,
        ),
        ResponseFunctionWebSearch.model_construct(
            id="web_search_1",
            action=ActionSearch(type="search", query="inventory"),
            status="completed",
            type="web_search_call",
            caller=PROGRAM_CALLER,
        ),
        ImageGenerationCall.model_construct(
            id="image_generation_1",
            status="completed",
            type="image_generation_call",
            caller=PROGRAM_CALLER,
        ),
    ],
)
def test_process_model_response_rejects_program_owned_direct_only_hosted_calls(
    tool_call: Any,
    as_mapping: bool,
) -> None:
    output = tool_call.model_dump(exclude_none=True) if as_mapping else tool_call
    agent = Agent(name="hosted", tools=[ProgrammaticToolCallingTool()])

    with pytest.raises(ModelBehaviorError, match="caller programmatic"):
        process_model_response(
            agent=agent,
            all_tools=agent.tools,
            response=ModelResponse(
                output=[_program(), output],
                usage=Usage(),
                response_id="response_1",
            ),
            output_schema=None,
            handoffs=[],
        )


@pytest.mark.parametrize(
    "tool_search_item",
    [
        ResponseToolSearchCall.model_construct(
            id="tool_search_call_item",
            arguments={},
            execution="server",
            status="completed",
            type="tool_search_call",
            caller=PROGRAM_CALLER,
        ),
        ResponseToolSearchOutputItem.model_construct(
            id="tool_search_output_item",
            execution="server",
            status="completed",
            tools=[],
            type="tool_search_output",
            caller=PROGRAM_CALLER,
        ),
    ],
)
def test_process_model_response_rejects_program_owned_tool_search_items(
    tool_search_item: Any,
) -> None:
    agent = Agent(
        name="tool search",
        tools=[ProgrammaticToolCallingTool(), ToolSearchTool()],
    )
    with pytest.raises(ModelBehaviorError, match="caller programmatic"):
        process_model_response(
            agent=agent,
            all_tools=agent.tools,
            response=ModelResponse(
                output=[_program(), tool_search_item],
                usage=Usage(),
                response_id="response_1",
            ),
            output_schema=None,
            handoffs=[],
        )


@pytest.mark.parametrize(
    ("output_type", "allowed_callers"),
    [
        ("mcp_approval_request", None),
        ("mcp_approval_request", ["direct"]),
        ("mcp_call", None),
        ("mcp_call", ["direct"]),
        ("mcp_list_tools", None),
        ("mcp_list_tools", ["direct"]),
        ("code_interpreter_call", None),
        ("code_interpreter_call", ["direct"]),
    ],
)
def test_process_model_response_rejects_disallowed_hosted_program_callers(
    output_type: str,
    allowed_callers: list[Any] | None,
) -> None:
    output, hosted_tool = _hosted_program_call_and_tool(output_type, allowed_callers)
    programmatic_tool = ProgrammaticToolCallingTool()
    agent = Agent(name="hosted", tools=[programmatic_tool, hosted_tool])
    with pytest.raises(ModelBehaviorError, match="caller programmatic"):
        process_model_response(
            agent=agent,
            all_tools=agent.tools,
            response=ModelResponse(
                output=[_program(), output],
                usage=Usage(),
                response_id="response_1",
            ),
            output_schema=None,
            handoffs=[],
        )


@pytest.mark.parametrize(
    "output_type",
    ["mcp_approval_request", "mcp_call", "mcp_list_tools", "code_interpreter_call"],
)
def test_process_model_response_accepts_allowed_hosted_program_callers(
    output_type: str,
) -> None:
    output, hosted_tool = _hosted_program_call_and_tool(output_type, ["programmatic"])
    programmatic_tool = ProgrammaticToolCallingTool()
    agent = Agent(name="hosted", tools=[programmatic_tool, hosted_tool])
    processed = process_model_response(
        agent=agent,
        all_tools=agent.tools,
        response=ModelResponse(
            output=[_program(), output],
            usage=Usage(),
            response_id="response_1",
        ),
        output_schema=None,
        handoffs=[],
    )

    assert len(processed.new_items) == 2


@pytest.mark.parametrize("as_mapping", [False, True])
@pytest.mark.parametrize("allowed_callers", [None, ["direct"]])
def test_process_model_response_rejects_disallowed_program_owned_shell_output(
    as_mapping: bool,
    allowed_callers: list[Any] | None,
) -> None:
    shell_output: Any = ResponseFunctionShellToolCallOutput.model_construct(
        id="shell_output_1",
        call_id="shell_call_1",
        status="completed",
        type="shell_call_output",
        output=[],
        caller=PROGRAM_CALLER,
    )
    if as_mapping:
        shell_output = shell_output.model_dump(exclude_none=True)
    shell_tool = ShellTool(executor=lambda _request: "ok", allowed_callers=allowed_callers)
    agent = Agent(
        name="shell",
        tools=[ProgrammaticToolCallingTool(), shell_tool],
    )

    with pytest.raises(ModelBehaviorError, match="caller programmatic"):
        process_model_response(
            agent=agent,
            all_tools=agent.tools,
            response=ModelResponse(
                output=[_program(), shell_output],
                usage=Usage(),
                response_id="response_1",
            ),
            output_schema=None,
            handoffs=[],
        )


@pytest.mark.parametrize("as_mapping", [False, True])
def test_process_model_response_accepts_allowed_program_owned_shell_output(
    as_mapping: bool,
) -> None:
    shell_output: Any = ResponseFunctionShellToolCallOutput.model_construct(
        id="shell_output_1",
        call_id="shell_call_1",
        status="completed",
        type="shell_call_output",
        output=[],
        caller=PROGRAM_CALLER,
    )
    if as_mapping:
        shell_output = shell_output.model_dump(exclude_none=True)
    shell_tool = ShellTool(executor=lambda _request: "ok", allowed_callers=["programmatic"])
    agent = Agent(
        name="shell",
        tools=[ProgrammaticToolCallingTool(), shell_tool],
    )

    processed = process_model_response(
        agent=agent,
        all_tools=agent.tools,
        response=ModelResponse(
            output=[_program(), shell_output],
            usage=Usage(),
            response_id="response_1",
        ),
        output_schema=None,
        handoffs=[],
    )

    assert len(processed.new_items) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
async def test_runner_executes_and_replays_programmatic_function_calls(streamed: bool) -> None:
    model = FakeModel()
    model.add_multiple_turn_outputs(
        [
            [_program(), _function_call()],
            [_program_output(), get_text_message("42 units are available")],
        ]
    )

    @function_tool(allowed_callers=["programmatic"])
    def lookup_inventory(sku: str) -> InventoryOutput:
        return InventoryOutput(sku=sku, available_units=42)

    agent = Agent(
        name="inventory",
        model=model,
        tools=[ProgrammaticToolCallingTool(), lookup_inventory],
        model_settings=ModelSettings(tool_choice="programmatic_tool_calling"),
    )

    result: Any
    if streamed:
        result = Runner.run_streamed(agent, "Check inventory")
        events = [event async for event in result.stream_events()]
        assert any(
            getattr(event, "name", None) == "tool_called"
            and _raw_item_type(getattr(getattr(event, "item", None), "raw_item", None)) == "program"
            for event in events
        )
    else:
        result = await Runner.run(agent, "Check inventory")

    assert result.final_output == "42 units are available"
    assert model.first_turn_args is not None
    assert model.first_turn_args["model_settings"].tool_choice == "programmatic_tool_calling"
    assert model.last_turn_args["model_settings"].tool_choice is None

    function_outputs = [
        item
        for item in result.new_items
        if isinstance(item, ToolCallOutputItem)
        and getattr(item.raw_item, "get", lambda _key: None)("type") == "function_call_output"
    ]
    assert len(function_outputs) == 1
    raw_output = cast(dict[str, Any], function_outputs[0].raw_item)
    assert json.loads(cast(str, raw_output["output"])) == {
        "sku": "A-1",
        "available_units": 42,
    }
    assert _caller_dict(raw_output["caller"]) == PROGRAM_CALLER

    replayed_output = next(
        item
        for item in model.last_turn_args["input"]
        if isinstance(item, dict) and item.get("type") == "function_call_output"
    )
    assert _caller_dict(replayed_output["caller"]) == PROGRAM_CALLER


@pytest.mark.asyncio
async def test_typed_programmatic_tool_preserves_input_guardrail_rejection() -> None:
    model = FakeModel()
    model.add_multiple_turn_outputs(
        [
            [_program(), _function_call()],
            [_program_output(), get_text_message("request rejected")],
        ]
    )
    executed = False

    @tool_input_guardrail
    def reject_tool_input(_data: ToolInputGuardrailData) -> ToolGuardrailFunctionOutput:
        return ToolGuardrailFunctionOutput.reject_content("inventory lookup blocked")

    @function_tool(
        allowed_callers=["programmatic"],
        tool_input_guardrails=[reject_tool_input],
    )
    def lookup_inventory(sku: str) -> InventoryOutput:
        nonlocal executed
        executed = True
        return InventoryOutput(sku=sku, available_units=42)

    agent = Agent(
        name="inventory",
        model=model,
        tools=[ProgrammaticToolCallingTool(), lookup_inventory],
    )

    result = await Runner.run(agent, "Check inventory")

    assert executed is False
    assert result.final_output == "request rejected"
    function_outputs = _function_output_raw_items(result)
    assert len(function_outputs) == 1
    assert function_outputs[0]["output"] == "inventory lookup blocked"
    assert _caller_dict(function_outputs[0]["caller"]) == PROGRAM_CALLER


@pytest.mark.asyncio
async def test_typed_programmatic_tool_preserves_default_timeout_result() -> None:
    model = FakeModel()
    model.add_multiple_turn_outputs(
        [
            [_program(), _function_call()],
            [_program_output(), get_text_message("request timed out")],
        ]
    )

    @function_tool(allowed_callers=["programmatic"], timeout=0.01)
    async def lookup_inventory(sku: str) -> InventoryOutput:
        await asyncio.sleep(0.2)
        return InventoryOutput(sku=sku, available_units=42)

    agent = Agent(
        name="inventory",
        model=model,
        tools=[ProgrammaticToolCallingTool(), lookup_inventory],
    )

    result = await Runner.run(agent, "Check inventory")

    assert result.final_output == "request timed out"
    function_outputs = _function_output_raw_items(result)
    assert len(function_outputs) == 1
    assert isinstance(function_outputs[0]["output"], str)
    assert "timed out" in function_outputs[0]["output"].lower()
    assert _caller_dict(function_outputs[0]["caller"]) == PROGRAM_CALLER


@pytest.mark.asyncio
async def test_typed_programmatic_tool_preserves_output_guardrail_rejection() -> None:
    model = FakeModel()
    model.add_multiple_turn_outputs(
        [
            [_program(), _function_call()],
            [_program_output(), get_text_message("request rejected")],
        ]
    )

    @tool_output_guardrail
    def reject_tool_output(_data: ToolOutputGuardrailData) -> ToolGuardrailFunctionOutput:
        return ToolGuardrailFunctionOutput.reject_content("inventory result blocked")

    @function_tool(
        allowed_callers=["programmatic"],
        tool_output_guardrails=[reject_tool_output],
    )
    def lookup_inventory(sku: str) -> InventoryOutput:
        return InventoryOutput(sku=sku, available_units=42)

    agent = Agent(
        name="inventory",
        model=model,
        tools=[ProgrammaticToolCallingTool(), lookup_inventory],
    )

    result = await Runner.run(agent, "Check inventory")

    assert result.final_output == "request rejected"
    function_outputs = _function_output_raw_items(result)
    assert len(function_outputs) == 1
    assert function_outputs[0]["output"] == "inventory result blocked"
    assert _caller_dict(function_outputs[0]["caller"]) == PROGRAM_CALLER


@pytest.mark.asyncio
async def test_typed_programmatic_tool_preserves_approval_rejection() -> None:
    model = FakeModel()
    model.add_multiple_turn_outputs(
        [
            [_program(), _function_call()],
            [_program_output(), get_text_message("request rejected")],
        ]
    )

    @function_tool(allowed_callers=["programmatic"], needs_approval=True)
    def lookup_inventory(sku: str) -> InventoryOutput:
        return InventoryOutput(sku=sku, available_units=42)

    agent = Agent(
        name="inventory",
        model=model,
        tools=[ProgrammaticToolCallingTool(), lookup_inventory],
    )
    first_result = await Runner.run(agent, "Check inventory")
    assert len(first_result.interruptions) == 1

    state = first_result.to_state()
    state.reject(first_result.interruptions[0])
    result = await Runner.run(agent, state)

    assert result.final_output == "request rejected"
    function_outputs = _function_output_raw_items(result)
    assert len(function_outputs) == 1
    assert function_outputs[0]["output"] == "Tool execution was not approved."
    assert _caller_dict(function_outputs[0]["caller"]) == PROGRAM_CALLER


@pytest.mark.asyncio
async def test_rebuilt_mapping_programmatic_approval_preserves_caller() -> None:
    model = FakeModel()
    model.add_multiple_turn_outputs(
        [
            [_program(), _function_call()],
            [_program_output(), get_text_message("done")],
        ]
    )
    executed = False

    @function_tool(allowed_callers=["programmatic"], needs_approval=True)
    def lookup_inventory(sku: str) -> InventoryOutput:
        nonlocal executed
        executed = True
        return InventoryOutput(sku=sku, available_units=42)

    agent = Agent(
        name="inventory",
        model=model,
        tools=[ProgrammaticToolCallingTool(), lookup_inventory],
    )
    first_result = await Runner.run(agent, "Check inventory")
    state = first_result.to_state()
    approval = state.get_interruptions()[0]
    approval.raw_item = cast(Any, approval.raw_item).model_dump(exclude_none=True)
    assert state._last_processed_response is not None
    state._last_processed_response.functions.clear()
    state.approve(approval)

    result = await Runner.run(agent, state)

    assert executed is True
    assert result.final_output == "done"
    function_outputs = _function_output_raw_items(result)
    assert len(function_outputs) == 1
    assert _caller_dict(function_outputs[0]["caller"]) == PROGRAM_CALLER


@pytest.mark.asyncio
async def test_rebuilt_mapping_programmatic_approval_rechecks_caller_permissions() -> None:
    model = FakeModel()
    model.set_next_output([_program(), _function_call()])
    executed = False

    @function_tool(allowed_callers=["programmatic"], needs_approval=True)
    def lookup_inventory(sku: str) -> InventoryOutput:
        nonlocal executed
        executed = True
        return InventoryOutput(sku=sku, available_units=42)

    agent = Agent(
        name="inventory",
        model=model,
        tools=[ProgrammaticToolCallingTool(), lookup_inventory],
    )
    first_result = await Runner.run(agent, "Check inventory")
    state = first_result.to_state()
    approval = state.get_interruptions()[0]
    approval.raw_item = cast(Any, approval.raw_item).model_dump(exclude_none=True)
    lookup_inventory.allowed_callers = ["direct"]
    assert state._last_processed_response is not None
    state._last_processed_response.functions.clear()
    state.approve(approval)

    with pytest.raises(ModelBehaviorError, match="caller programmatic"):
        await Runner.run(agent, state)

    assert executed is False


@pytest.mark.asyncio
@pytest.mark.parametrize("parent_state", ["missing", "completed"])
async def test_rebuilt_programmatic_approval_requires_active_parent(parent_state: str) -> None:
    model = FakeModel()
    model.add_multiple_turn_outputs(
        [
            [_program(), _function_call()],
            [_program_output(), get_text_message("done")],
        ]
    )
    executed = False

    @function_tool(allowed_callers=["programmatic"], needs_approval=True)
    def lookup_inventory(sku: str) -> InventoryOutput:
        nonlocal executed
        executed = True
        return InventoryOutput(sku=sku, available_units=42)

    agent = Agent(
        name="inventory",
        model=model,
        tools=[ProgrammaticToolCallingTool(), lookup_inventory],
    )
    first_result = await Runner.run(agent, "Check inventory")
    state = first_result.to_state()
    approval = state.get_interruptions()[0]
    approval.raw_item = cast(Any, approval.raw_item).model_dump(exclude_none=True)
    assert state._last_processed_response is not None
    state._last_processed_response.functions.clear()
    assert state._model_responses
    if parent_state == "missing":
        state._generated_items = [
            item for item in state._generated_items if _raw_item_type(item.raw_item) != "program"
        ]
        state._last_processed_response.new_items = [
            item
            for item in state._last_processed_response.new_items
            if _raw_item_type(item.raw_item) != "program"
        ]
        state._model_responses[-1].output = [
            item for item in state._model_responses[-1].output if _raw_item_type(item) != "program"
        ]
        expected_error = "parent program item"
    else:
        state._model_responses[-1].output.append(_program_output())
        expected_error = "already completed"
    state.approve(approval)

    with pytest.raises(ModelBehaviorError, match=expected_error):
        await Runner.run(agent, state)

    assert executed is False


@pytest.mark.asyncio
async def test_typed_programmatic_tool_preserves_pre_approval_guardrail_rejection() -> None:
    model = FakeModel()
    model.add_multiple_turn_outputs(
        [
            [_program(), _function_call()],
            [_program_output(), get_text_message("request rejected")],
        ]
    )

    @tool_input_guardrail
    def reject_tool_input(_data: ToolInputGuardrailData) -> ToolGuardrailFunctionOutput:
        return ToolGuardrailFunctionOutput.reject_content("inventory lookup blocked")

    @function_tool(
        allowed_callers=["programmatic"],
        needs_approval=True,
        tool_input_guardrails=[reject_tool_input],
    )
    def lookup_inventory(sku: str) -> InventoryOutput:
        return InventoryOutput(sku=sku, available_units=42)

    agent = Agent(
        name="inventory",
        model=model,
        tools=[ProgrammaticToolCallingTool(), lookup_inventory],
    )
    run_config = RunConfig(
        tool_execution=ToolExecutionConfig(pre_approval_tool_input_guardrails=True)
    )

    result = await Runner.run(agent, "Check inventory", run_config=run_config)

    assert result.final_output == "request rejected"
    function_outputs = _function_output_raw_items(result)
    assert len(function_outputs) == 1
    assert function_outputs[0]["output"] == "inventory lookup blocked"
    assert _caller_dict(function_outputs[0]["caller"]) == PROGRAM_CALLER


@pytest.mark.asyncio
async def test_runner_handles_multiple_pauses_from_one_program() -> None:
    model = FakeModel()
    second_call = ResponseFunctionToolCall(
        id="function_item_2",
        call_id="call_lookup_2",
        name="lookup_inventory",
        arguments='{"sku":"B-2"}',
        caller=CallerProgram(type="program", caller_id=PROGRAM_CALL_ID),
        type="function_call",
    )
    model.add_multiple_turn_outputs(
        [
            [_program(), _function_call()],
            [_program_output("incomplete"), second_call],
            [
                ProgramOutput(
                    id="program_output_item_2",
                    call_id=PROGRAM_CALL_ID,
                    result='{"total":84}',
                    status="completed",
                    type="program_output",
                ),
                get_text_message("84 units are available"),
            ],
        ]
    )

    calls: list[str] = []

    @function_tool(allowed_callers=["programmatic"])
    def lookup_inventory(sku: str) -> InventoryOutput:
        calls.append(sku)
        return InventoryOutput(sku=sku, available_units=42)

    agent = Agent(
        name="inventory",
        model=model,
        tools=[ProgrammaticToolCallingTool(), lookup_inventory],
        model_settings=ModelSettings(tool_choice="programmatic_tool_calling"),
    )

    result = await Runner.run(agent, "Check two SKUs")

    assert result.final_output == "84 units are available"
    assert calls == ["A-1", "B-2"]
    assert model.last_turn_args["model_settings"].tool_choice is None
    assert len([item for item in result.new_items if isinstance(item, ToolCallOutputItem)]) == 4


@pytest.mark.asyncio
async def test_runner_executes_programmatic_batch_calls_concurrently() -> None:
    model = FakeModel()
    batch_calls = [
        ResponseFunctionToolCall(
            id=f"function_item_{index}",
            call_id=f"call_lookup_{index}",
            name="lookup_inventory",
            arguments=json.dumps({"sku": f"SKU-{index}"}),
            caller=CallerProgram(type="program", caller_id=PROGRAM_CALL_ID),
            type="function_call",
        )
        for index in range(9)
    ]
    model.add_multiple_turn_outputs(
        [
            [_program(), *batch_calls],
            [_program_output(), get_text_message("batch complete")],
        ]
    )

    active_calls = 0
    max_active_calls = 0

    @function_tool(allowed_callers=["programmatic"])
    async def lookup_inventory(sku: str) -> InventoryOutput:
        nonlocal active_calls, max_active_calls
        active_calls += 1
        max_active_calls = max(max_active_calls, active_calls)
        await asyncio.sleep(0.01)
        active_calls -= 1
        return InventoryOutput(sku=sku, available_units=42)

    agent = Agent(
        name="inventory",
        model=model,
        tools=[ProgrammaticToolCallingTool(), lookup_inventory],
    )

    result = await Runner.run(agent, "Check nine SKUs")

    function_outputs = [
        item
        for item in result.new_items
        if isinstance(item, ToolCallOutputItem)
        and isinstance(item.raw_item, dict)
        and item.raw_item.get("type") == "function_call_output"
    ]
    assert result.final_output == "batch complete"
    assert len(result.raw_responses) == 2
    assert len(function_outputs) == 9
    assert max_active_calls == 9
    assert all(
        _caller_dict(cast(dict[str, Any], item.raw_item)["caller"]) == PROGRAM_CALLER
        for item in function_outputs
    )


@pytest.mark.asyncio
async def test_previous_response_id_continuation_sends_only_program_function_output() -> None:
    model = FakeModel()
    model.add_multiple_turn_outputs(
        [
            [_program(), _function_call()],
            [_program_output(), get_text_message("done")],
        ]
    )

    @function_tool(allowed_callers=["programmatic"])
    def lookup_inventory(sku: str) -> InventoryOutput:
        return InventoryOutput(sku=sku, available_units=42)

    agent = Agent(
        name="inventory",
        model=model,
        tools=[ProgrammaticToolCallingTool(), lookup_inventory],
    )

    result = await Runner.run(agent, "Check inventory", auto_previous_response_id=True)

    assert result.final_output == "done"
    assert model.last_turn_args["previous_response_id"] == "resp-789"
    last_input = model.last_turn_args["input"]
    assert isinstance(last_input, list)
    assert len(last_input) == 1
    function_output = cast(dict[str, Any], last_input[0])
    assert function_output["type"] == "function_call_output"
    assert _caller_dict(function_output["caller"]) == PROGRAM_CALLER


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
async def test_previous_response_id_continuation_accepts_server_owned_program_output(
    streamed: bool,
) -> None:
    model = FakeModel()
    model.set_next_output([_program_output(), get_text_message("done")])

    @function_tool(allowed_callers=["programmatic"])
    def lookup_inventory(sku: str) -> InventoryOutput:
        return InventoryOutput(sku=sku, available_units=42)

    agent = Agent(
        name="inventory",
        model=model,
        tools=[ProgrammaticToolCallingTool(), lookup_inventory],
    )
    submitted_delta = [
        {
            "type": "function_call_output",
            "call_id": FUNCTION_CALL_ID,
            "output": '{"sku":"A-1","available_units":42}',
            "caller": PROGRAM_CALLER,
        }
    ]

    result: Any
    if streamed:
        result = Runner.run_streamed(
            agent,
            cast(Any, submitted_delta),
            previous_response_id="response_with_program_parent",
        )
        _events = [event async for event in result.stream_events()]
    else:
        result = await Runner.run(
            agent,
            cast(Any, submitted_delta),
            previous_response_id="response_with_program_parent",
        )

    assert result.final_output == "done"
    assert model.last_turn_args["previous_response_id"] == "response_with_program_parent"


@pytest.mark.asyncio
async def test_previous_response_id_continuation_accepts_repeated_program_pause() -> None:
    model = FakeModel()
    model.add_multiple_turn_outputs(
        [
            [_function_call()],
            [_program_output(), get_text_message("done")],
        ]
    )
    executed = False

    @function_tool(allowed_callers=["programmatic"])
    def lookup_inventory(sku: str) -> InventoryOutput:
        nonlocal executed
        executed = True
        return InventoryOutput(sku=sku, available_units=42)

    agent = Agent(
        name="inventory",
        model=model,
        tools=[ProgrammaticToolCallingTool(), lookup_inventory],
    )
    submitted_delta = [
        {
            "type": "function_call_output",
            "call_id": "call_previous_lookup",
            "output": '{"sku":"A-0","available_units":21}',
            "caller": PROGRAM_CALLER,
        }
    ]

    result = await Runner.run(
        agent,
        cast(Any, submitted_delta),
        previous_response_id="response_with_program_parent",
    )

    assert executed is True
    assert result.final_output == "done"
    assert len(result.raw_responses) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("parent_source", ["caller", "incomplete_program_output"])
async def test_run_state_round_trip_preserves_server_owned_program_parent(
    parent_source: str,
) -> None:
    model = FakeModel()
    model.add_multiple_turn_outputs(
        [
            [_function_call()],
            [_program_output(), get_text_message("done")],
        ]
    )
    executed = False

    @function_tool(allowed_callers=["programmatic"], needs_approval=True)
    def lookup_inventory(sku: str) -> InventoryOutput:
        nonlocal executed
        executed = True
        return InventoryOutput(sku=sku, available_units=42)

    agent = Agent(
        name="inventory",
        model=model,
        tools=[ProgrammaticToolCallingTool(), lookup_inventory],
    )
    if parent_source == "caller":
        submitted_delta = [
            {
                "type": "function_call_output",
                "call_id": "call_previous_lookup",
                "output": '{"sku":"A-0","available_units":21}',
                "caller": PROGRAM_CALLER,
            }
        ]
    else:
        submitted_delta = [_program_output("incomplete").model_dump(exclude_none=True)]

    first_result = await Runner.run(
        agent,
        cast(Any, submitted_delta),
        previous_response_id="response_with_program_parent",
    )
    assert len(first_result.interruptions) == 1

    restored_state = await RunState.from_json(agent, first_result.to_state().to_json())
    approval = restored_state.get_interruptions()[0]
    restored_state.approve(approval)
    result = await Runner.run(agent, restored_state)

    assert executed is True
    assert result.final_output == "done"
    assert model.last_turn_args["previous_response_id"] == "resp-789"


@pytest.mark.asyncio
async def test_sqlite_session_round_trip_preserves_program_history_and_caller() -> None:
    model = FakeModel()
    model.add_multiple_turn_outputs(
        [
            [_program(), _function_call()],
            [_program_output(), get_text_message("done")],
        ]
    )

    @function_tool(allowed_callers=["programmatic"])
    def lookup_inventory(sku: str) -> InventoryOutput:
        return InventoryOutput(sku=sku, available_units=42)

    agent = Agent(
        name="inventory",
        model=model,
        tools=[ProgrammaticToolCallingTool(), lookup_inventory],
    )
    session = SQLiteSession("programmatic-tool-calling")
    try:
        result = await Runner.run(agent, "Check inventory", session=session)
        assert result.final_output == "done"

        session_items = await session.get_items()
        assert [_raw_item_type(item) for item in session_items] == [
            None,
            "program",
            "function_call",
            "function_call_output",
            "program_output",
            "message",
        ]
        function_output = next(
            cast(dict[str, Any], item)
            for item in session_items
            if _raw_item_type(item) == "function_call_output"
        )
        assert _caller_dict(function_output["caller"]) == PROGRAM_CALLER
    finally:
        session.close()


@pytest.mark.asyncio
async def test_nested_handoff_summarizes_complete_programmatic_transcript() -> None:
    model = FakeModel()
    delegate = Agent(name="delegate", model=model)
    model.add_multiple_turn_outputs(
        [
            [_program(), _function_call()],
            [_program_output(), get_handoff_tool_call(delegate)],
            [get_text_message("done")],
        ]
    )

    @function_tool(allowed_callers=["programmatic"])
    def lookup_inventory(sku: str) -> InventoryOutput:
        return InventoryOutput(sku=sku, available_units=42)

    triage = Agent(
        name="triage",
        model=model,
        handoffs=[delegate],
        tools=[ProgrammaticToolCallingTool(), lookup_inventory],
    )
    captured_inputs: list[list[Any]] = []

    def capture_model_input(data: Any) -> Any:
        captured_inputs.append(list(data.model_data.input))
        return data.model_data

    session = SQLiteSession("programmatic-tool-calling-nested-handoff")
    try:
        result = await Runner.run(
            triage,
            "Check inventory and delegate the final response.",
            run_config=RunConfig(
                nest_handoff_history=True,
                call_model_input_filter=capture_model_input,
            ),
            session=session,
        )

        assert result.final_output == "done"
        handoff_input = captured_inputs[-1]
        handoff_types = [_raw_item_type(item) for item in handoff_input]
        assert not {
            "program",
            "function_call",
            "function_call_output",
            "program_output",
        }.intersection(handoff_types)

        summary_text = "\n".join(
            cast(str, item.get("content"))
            for item in handoff_input
            if isinstance(item, dict) and isinstance(item.get("content"), str)
        )
        assert '"type": "program"' in summary_text
        assert '"type": "function_call"' in summary_text
        assert '"type": "function_call_output"' in summary_text
        assert '"type": "program_output"' in summary_text

        session_items = await session.get_items()
        assert [_raw_item_type(item) for item in session_items] == [
            None,
            "program",
            "function_call",
            "function_call_output",
            "program_output",
            "function_call",
            "function_call_output",
            "message",
        ]
    finally:
        session.close()


@pytest.mark.asyncio
async def test_non_function_programmatic_outputs_preserve_caller() -> None:
    caller = cast(Any, PROGRAM_CALLER)

    async def run_tool(tool: Any, tool_call: Any) -> dict[str, Any]:
        model = FakeModel()
        model.add_multiple_turn_outputs([[_program(), tool_call], [get_text_message("done")]])
        agent = Agent(
            name="tool agent",
            model=model,
            tools=[ProgrammaticToolCallingTool(), tool],
        )
        result = await Runner.run(agent, "Run the tool")
        return next(
            cast(dict[str, Any], item.raw_item)
            for item in result.new_items
            if isinstance(item, ToolCallOutputItem)
        )

    async def shell_executor(_request: Any) -> str:
        return "shell done"

    shell_output = await run_tool(
        ShellTool(executor=shell_executor, allowed_callers=["programmatic"]),
        ResponseFunctionShellToolCall(
            id="shell_item",
            call_id="call_shell",
            action=Action(commands=["echo ok"]),
            status="completed",
            type="shell_call",
            caller=caller,
        ),
    )
    assert _caller_dict(shell_output["caller"]) == PROGRAM_CALLER

    custom_output = await run_tool(
        CustomTool(
            name="custom",
            description="Custom tool",
            on_invoke_tool=lambda _context, _input: "custom done",
            allowed_callers=["programmatic"],
        ),
        ResponseCustomToolCall(
            id="custom_item",
            call_id="call_custom",
            input="input",
            name="custom",
            type="custom_tool_call",
            caller=caller,
        ),
    )
    assert _caller_dict(custom_output["caller"]) == PROGRAM_CALLER

    class Editor:
        def create_file(self, _operation: Any) -> str:
            return "patch done"

        def update_file(self, _operation: Any) -> str:
            return "patch done"

        def delete_file(self, _operation: Any) -> str:
            return "patch done"

    apply_patch_output = await run_tool(
        ApplyPatchTool(editor=Editor(), allowed_callers=["programmatic"]),
        ResponseApplyPatchToolCall(
            id="apply_patch_item",
            call_id="call_apply_patch",
            operation=OperationCreateFile(type="create_file", path="example.txt", diff="hello"),
            status="completed",
            type="apply_patch_call",
            caller=caller,
        ),
    )
    assert _caller_dict(apply_patch_output["caller"]) == PROGRAM_CALLER
