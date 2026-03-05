from typing import Any, cast

import pytest
from mcp.types import (
    Tool as MCPTool,
    ToolAnnotations,
)
from openai._models import construct_type
from openai.types.responses import (
    ResponseApplyPatchToolCall,
    ResponseCompactionItem,
    ResponseFunctionShellToolCall,
    ResponseFunctionShellToolCallOutput,
    ResponseFunctionToolCall,
    ResponseOutputItem,
    ResponseToolSearchCall,
    ResponseToolSearchOutputItem,
)
from openai.types.responses.response_output_item import McpCall

from agents import (
    Agent,
    ApplyPatchTool,
    CompactionItem,
    Handoff,
    ShellTool,
    Tool,
    function_tool,
    handoff,
    tool_namespace,
)
from agents.exceptions import ModelBehaviorError, UserError
from agents.items import (
    HandoffCallItem,
    ModelResponse,
    ToolCallItem,
    ToolCallOutputItem,
    ToolSearchCallItem,
    ToolSearchOutputItem,
)
from agents.run_internal import run_loop
from agents.usage import Usage
from tests.fake_model import FakeModel
from tests.mcp.helpers import CachedToolsMCPServer, NoCacheMCPServer
from tests.test_responses import get_function_tool_call
from tests.utils.hitl import (
    RecordingEditor,
    make_apply_patch_call,
    make_apply_patch_dict,
    make_shell_call,
)


def _response(output: list[object]) -> ModelResponse:
    response = ModelResponse(output=[], usage=Usage(), response_id="resp")
    response.output = output  # type: ignore[assignment]
    return response


def test_process_model_response_shell_call_without_tool_raises() -> None:
    agent = Agent(name="no-shell", model=FakeModel())
    shell_call = make_shell_call("shell-1")

    with pytest.raises(ModelBehaviorError, match="shell tool"):
        run_loop.process_model_response(
            agent=agent,
            all_tools=[],
            response=_response([shell_call]),
            output_schema=None,
            handoffs=[],
        )


def test_process_model_response_skips_local_shell_execution_for_hosted_environment() -> None:
    shell_tool = ShellTool(environment={"type": "container_auto"})
    agent = Agent(name="hosted-shell", model=FakeModel(), tools=[shell_tool])
    shell_call = make_shell_call("shell-hosted-1")

    processed = run_loop.process_model_response(
        agent=agent,
        all_tools=[shell_tool],
        response=_response([shell_call]),
        output_schema=None,
        handoffs=[],
    )

    assert len(processed.new_items) == 1
    assert isinstance(processed.new_items[0], ToolCallItem)
    assert processed.shell_calls == []
    assert processed.tools_used == ["shell"]


def test_process_model_response_sanitizes_shell_call_model_object() -> None:
    shell_call = ResponseFunctionShellToolCall(
        type="shell_call",
        id="sh_call_2",
        call_id="call_shell_2",
        status="completed",
        created_by="server",
        action=cast(Any, {"commands": ["echo hi"], "timeout_ms": 1000}),
    )
    shell_tool = ShellTool(environment={"type": "container_auto"})
    agent = Agent(name="hosted-shell-model", model=FakeModel(), tools=[shell_tool])

    processed = run_loop.process_model_response(
        agent=agent,
        all_tools=[shell_tool],
        response=_response([shell_call]),
        output_schema=None,
        handoffs=[],
    )

    assert len(processed.new_items) == 1
    item = processed.new_items[0]
    assert isinstance(item, ToolCallItem)
    assert isinstance(item.raw_item, dict)
    assert item.raw_item["type"] == "shell_call"
    assert "created_by" not in item.raw_item
    next_input = item.to_input_item()
    assert isinstance(next_input, dict)
    assert next_input["type"] == "shell_call"
    assert "created_by" not in next_input
    assert processed.shell_calls == []
    assert processed.tools_used == ["shell"]


def test_process_model_response_preserves_shell_call_output() -> None:
    shell_output = {
        "type": "shell_call_output",
        "id": "sh_out_1",
        "call_id": "call_shell_1",
        "status": "completed",
        "max_output_length": 1000,
        "output": [
            {
                "stdout": "ok\n",
                "stderr": "",
                "outcome": {"type": "exit", "exit_code": 0},
            }
        ],
    }
    agent = Agent(name="shell-output", model=FakeModel())

    processed = run_loop.process_model_response(
        agent=agent,
        all_tools=[],
        response=_response([shell_output]),
        output_schema=None,
        handoffs=[],
    )

    assert len(processed.new_items) == 1
    assert isinstance(processed.new_items[0], ToolCallOutputItem)
    assert processed.new_items[0].raw_item == shell_output
    assert processed.tools_used == ["shell"]
    assert processed.shell_calls == []


def test_process_model_response_sanitizes_shell_call_output_model_object() -> None:
    shell_output = ResponseFunctionShellToolCallOutput(
        type="shell_call_output",
        id="sh_out_2",
        call_id="call_shell_2",
        status="completed",
        created_by="server",
        output=cast(
            Any,
            [
                {
                    "stdout": "ok\n",
                    "stderr": "",
                    "outcome": {"type": "exit", "exit_code": 0},
                    "created_by": "server",
                }
            ],
        ),
    )
    agent = Agent(name="shell-output-model", model=FakeModel())

    processed = run_loop.process_model_response(
        agent=agent,
        all_tools=[],
        response=_response([shell_output]),
        output_schema=None,
        handoffs=[],
    )

    assert len(processed.new_items) == 1
    item = processed.new_items[0]
    assert isinstance(item, ToolCallOutputItem)
    assert isinstance(item.raw_item, dict)
    assert item.raw_item["type"] == "shell_call_output"
    assert "created_by" not in item.raw_item
    shell_outputs = item.raw_item.get("output")
    assert isinstance(shell_outputs, list)
    assert isinstance(shell_outputs[0], dict)
    assert "created_by" not in shell_outputs[0]

    next_input = item.to_input_item()
    assert isinstance(next_input, dict)
    assert next_input["type"] == "shell_call_output"
    assert "status" not in next_input
    assert "created_by" not in next_input
    next_outputs = next_input.get("output")
    assert isinstance(next_outputs, list)
    assert isinstance(next_outputs[0], dict)
    assert "created_by" not in next_outputs[0]
    assert processed.tools_used == ["shell"]


def test_process_model_response_apply_patch_call_without_tool_raises() -> None:
    agent = Agent(name="no-apply", model=FakeModel())
    apply_patch_call = make_apply_patch_dict("apply-1", diff="-old\n+new\n")

    with pytest.raises(ModelBehaviorError, match="apply_patch tool"):
        run_loop.process_model_response(
            agent=agent,
            all_tools=[],
            response=_response([apply_patch_call]),
            output_schema=None,
            handoffs=[],
        )


def test_process_model_response_sanitizes_apply_patch_call_model_object() -> None:
    editor = RecordingEditor()
    apply_patch_tool = ApplyPatchTool(editor=editor)
    agent = Agent(name="apply-agent-model", model=FakeModel(), tools=[apply_patch_tool])
    apply_patch_call = ResponseApplyPatchToolCall(
        type="apply_patch_call",
        id="ap_call_1",
        call_id="call_apply_1",
        status="completed",
        created_by="server",
        operation=cast(
            Any,
            {"type": "update_file", "path": "test.md", "diff": "-old\n+new\n"},
        ),
    )

    processed = run_loop.process_model_response(
        agent=agent,
        all_tools=[apply_patch_tool],
        response=_response([apply_patch_call]),
        output_schema=None,
        handoffs=[],
    )

    assert len(processed.new_items) == 1
    item = processed.new_items[0]
    assert isinstance(item, ToolCallItem)
    assert isinstance(item.raw_item, dict)
    assert item.raw_item["type"] == "apply_patch_call"
    assert "created_by" not in item.raw_item
    next_input = item.to_input_item()
    assert isinstance(next_input, dict)
    assert next_input["type"] == "apply_patch_call"
    assert "created_by" not in next_input
    assert len(processed.apply_patch_calls) == 1
    queued_call = processed.apply_patch_calls[0].tool_call
    assert isinstance(queued_call, dict)
    assert queued_call["type"] == "apply_patch_call"
    assert "created_by" not in queued_call
    assert processed.tools_used == [apply_patch_tool.name]


def test_process_model_response_converts_custom_apply_patch_call() -> None:
    editor = RecordingEditor()
    apply_patch_tool = ApplyPatchTool(editor=editor)
    agent = Agent(name="apply-agent", model=FakeModel(), tools=[apply_patch_tool])
    custom_call = make_apply_patch_call("custom-apply-1")

    processed = run_loop.process_model_response(
        agent=agent,
        all_tools=[apply_patch_tool],
        response=_response([custom_call]),
        output_schema=None,
        handoffs=[],
    )

    assert processed.apply_patch_calls, "Custom apply_patch call should be converted"
    converted_call = processed.apply_patch_calls[0].tool_call
    assert isinstance(converted_call, dict)
    assert converted_call.get("type") == "apply_patch_call"


def test_process_model_response_prefers_namespaced_function_over_apply_patch_fallback() -> None:
    namespaced_tool = tool_namespace(
        name="billing",
        description="Billing tools",
        tools=[function_tool(lambda payload: payload, name_override="apply_patch_lookup")],
    )[0]
    all_tools: list[Tool] = [namespaced_tool]
    agent = Agent(name="billing-agent", model=FakeModel(), tools=all_tools)

    processed = run_loop.process_model_response(
        agent=agent,
        all_tools=all_tools,
        response=_response(
            [
                get_function_tool_call(
                    "apply_patch_lookup",
                    '{"payload":"value"}',
                    namespace="billing",
                )
            ]
        ),
        output_schema=None,
        handoffs=[],
    )

    assert len(processed.functions) == 1
    assert processed.functions[0].function_tool is namespaced_tool
    assert processed.apply_patch_calls == []


# ---------------------------------------------------------------------------
# Helper for MCP title/description tests
# ---------------------------------------------------------------------------


def _make_mcp_call(server_label: str, tool_name: str) -> McpCall:
    """Build a minimal McpCall output item."""
    return McpCall(
        id="mcp_call_1",
        name=tool_name,
        server_label=server_label,
        arguments="{}",
        type="mcp_call",
    )


def _run_with_mcp_call(agent: Agent, server_label: str, tool_name: str) -> Any:
    """Run process_model_response for a single McpCall and return the ProcessedResponse."""
    response = ModelResponse(output=[], usage=Usage(), response_id="resp")
    response.output = [_make_mcp_call(server_label, tool_name)]
    return run_loop.process_model_response(
        agent=agent,
        all_tools=[],
        response=response,
        output_schema=None,
        handoffs=[],
    )


# ---------------------------------------------------------------------------
# MCP ToolCallItem title/description tests
# ---------------------------------------------------------------------------


def test_mcp_tool_call_item_description_only() -> None:
    """MCP tool with description only: description populated, title is None."""
    mcp_tool = MCPTool(name="my_tool", inputSchema={}, description="Long description", title=None)
    server = CachedToolsMCPServer("my_server", [mcp_tool])
    agent = Agent(name="agent", model=FakeModel(), mcp_servers=[server])

    processed = _run_with_mcp_call(agent, "my_server", "my_tool")

    assert len(processed.new_items) == 1
    item = processed.new_items[0]
    assert isinstance(item, ToolCallItem)
    assert item.description == "Long description"
    assert item.title is None


def test_mcp_tool_call_item_title_only() -> None:
    """MCP tool with title only: title populated, description is None."""
    mcp_tool = MCPTool(name="my_tool", inputSchema={}, description=None, title="Short Label")
    server = CachedToolsMCPServer("my_server", [mcp_tool])
    agent = Agent(name="agent", model=FakeModel(), mcp_servers=[server])

    processed = _run_with_mcp_call(agent, "my_server", "my_tool")

    assert len(processed.new_items) == 1
    item = processed.new_items[0]
    assert isinstance(item, ToolCallItem)
    assert item.description is None
    assert item.title == "Short Label"


def test_mcp_tool_call_item_annotations_title_only() -> None:
    """MCP tool with annotations.title only: title resolved from annotations, description None."""
    mcp_tool = MCPTool(
        name="my_tool",
        inputSchema={},
        description=None,
        title=None,
        annotations=ToolAnnotations(title="Annotation Label"),
    )
    server = CachedToolsMCPServer("my_server", [mcp_tool])
    agent = Agent(name="agent", model=FakeModel(), mcp_servers=[server])

    processed = _run_with_mcp_call(agent, "my_server", "my_tool")

    assert len(processed.new_items) == 1
    item = processed.new_items[0]
    assert isinstance(item, ToolCallItem)
    assert item.description is None
    assert item.title == "Annotation Label"


def test_mcp_tool_call_item_title_takes_precedence_over_annotations_title() -> None:
    """tool.title takes precedence over annotations.title."""
    mcp_tool = MCPTool(
        name="my_tool",
        inputSchema={},
        description=None,
        title="Top-level Title",
        annotations=ToolAnnotations(title="Annotation Title"),
    )
    server = CachedToolsMCPServer("my_server", [mcp_tool])
    agent = Agent(name="agent", model=FakeModel(), mcp_servers=[server])

    processed = _run_with_mcp_call(agent, "my_server", "my_tool")

    assert len(processed.new_items) == 1
    item = processed.new_items[0]
    assert isinstance(item, ToolCallItem)
    assert item.title == "Top-level Title"


def test_mcp_tool_call_item_both_description_and_title() -> None:
    """MCP tool with both description and title: both fields independently populated."""
    mcp_tool = MCPTool(
        name="my_tool",
        inputSchema={},
        description="Long description for model",
        title="Short Label for UI",
    )
    server = CachedToolsMCPServer("my_server", [mcp_tool])
    agent = Agent(name="agent", model=FakeModel(), mcp_servers=[server])

    processed = _run_with_mcp_call(agent, "my_server", "my_tool")

    assert len(processed.new_items) == 1
    item = processed.new_items[0]
    assert isinstance(item, ToolCallItem)
    assert item.description == "Long description for model"
    assert item.title == "Short Label for UI"


def test_mcp_tool_call_item_no_metadata() -> None:
    """MCP tool with no description, title, or annotations: both fields None."""
    mcp_tool = MCPTool(
        name="my_tool",
        inputSchema={},
        description=None,
        title=None,
        annotations=None,
    )
    server = CachedToolsMCPServer("my_server", [mcp_tool])
    agent = Agent(name="agent", model=FakeModel(), mcp_servers=[server])

    processed = _run_with_mcp_call(agent, "my_server", "my_tool")

    assert len(processed.new_items) == 1
    item = processed.new_items[0]
    assert isinstance(item, ToolCallItem)
    assert item.description is None
    assert item.title is None


def test_mcp_tool_call_item_no_cached_tools() -> None:
    """When cached_tools is None (caching disabled) both fields remain None."""
    server = NoCacheMCPServer()
    agent = Agent(name="agent", model=FakeModel(), mcp_servers=[server])

    processed = _run_with_mcp_call(agent, "my_server", "my_tool")

    assert len(processed.new_items) == 1
    item = processed.new_items[0]
    assert isinstance(item, ToolCallItem)
    assert item.description is None
    assert item.title is None


def test_mcp_tool_call_item_no_annotations_attribute() -> None:
    """Tool object with no annotations attribute at all does not raise AttributeError.

    Custom MCP server implementations or future MCP SDK versions may return tool
    objects that lack the annotations attribute entirely.  The title lookup must
    use getattr defensively rather than direct attribute access.
    """
    import types

    # Build a minimal tool-like object that has no annotations attribute.
    bare_tool = types.SimpleNamespace(name="my_tool", description=None, title="Bare Title")
    # Confirm the attribute really is absent (not just None).
    assert not hasattr(bare_tool, "annotations")

    server = CachedToolsMCPServer("my_server", [bare_tool])  # type: ignore[list-item]
    agent = Agent(name="agent", model=FakeModel(), mcp_servers=[server])

    # Should not raise; title resolved from tool.title.
    processed = _run_with_mcp_call(agent, "my_server", "my_tool")

    assert len(processed.new_items) == 1
    item = processed.new_items[0]
    assert isinstance(item, ToolCallItem)
    assert item.title == "Bare Title"
    assert item.description is None


def test_mcp_tool_call_item_no_annotations_attribute_falls_back_to_none() -> None:
    """Tool object with no annotations attribute and no title yields title=None."""
    import types

    bare_tool = types.SimpleNamespace(name="my_tool", description=None, title=None)
    assert not hasattr(bare_tool, "annotations")

    server = CachedToolsMCPServer("my_server", [bare_tool])  # type: ignore[list-item]
    agent = Agent(name="agent", model=FakeModel(), mcp_servers=[server])

    processed = _run_with_mcp_call(agent, "my_server", "my_tool")

    assert len(processed.new_items) == 1
    item = processed.new_items[0]
    assert isinstance(item, ToolCallItem)
    assert item.title is None
    assert item.description is None


# ---------------------------------------------------------------------------
# ResponseFunctionToolCall title tests (MCP tools converted by MCPUtil)
# ---------------------------------------------------------------------------


def _make_function_tool_call(tool_name: str) -> ResponseFunctionToolCall:
    """Build a minimal ResponseFunctionToolCall (what the model emits for converted MCP tools)."""
    return ResponseFunctionToolCall(
        id="call_1",
        call_id="call_1",
        name=tool_name,
        arguments="{}",
        type="function_call",
    )


def _run_with_function_tool_call(agent: Agent, tool_name: str, all_tools: list[Any]) -> Any:
    """Run process_model_response for a ResponseFunctionToolCall and return ProcessedResponse."""
    response = ModelResponse(output=[], usage=Usage(), response_id="resp")
    response.output = [_make_function_tool_call(tool_name)]
    return run_loop.process_model_response(
        agent=agent,
        all_tools=all_tools,
        response=response,
        output_schema=None,
        handoffs=[],
    )


def _make_fake_function_tool(
    name: str, description: str | None, mcp_title: str | None = None
) -> Any:
    """Build a minimal FunctionTool-like stub for use in all_tools."""
    from agents.tool import FunctionTool

    async def _noop(*args: Any, **kwargs: Any) -> str:
        return ""

    return FunctionTool(
        name=name,
        description=description or "",
        params_json_schema={},
        on_invoke_tool=_noop,
        _mcp_title=mcp_title,
    )


def test_function_tool_call_item_title_from_mcp_title() -> None:
    """ResponseFunctionToolCall ToolCallItem gets title from FunctionTool._mcp_title.

    MCPUtil.to_function_tool stores the MCP title directly on FunctionTool._mcp_title,
    so process_model_response simply reads it from the resolved tool.
    """
    func_tool = _make_fake_function_tool(
        "my_tool", "Long description for model", mcp_title="Short Label"
    )
    agent = Agent(name="agent", model=FakeModel())

    processed = _run_with_function_tool_call(agent, "my_tool", [func_tool])

    assert len(processed.new_items) == 1
    item = processed.new_items[0]
    assert isinstance(item, ToolCallItem)
    assert item.description == "Long description for model"
    assert item.title == "Short Label"


def test_function_tool_call_item_title_none_when_no_mcp_title() -> None:
    """ResponseFunctionToolCall for a plain (non-MCP) FunctionTool has title=None."""
    func_tool = _make_fake_function_tool("plain_tool", "A plain function tool")
    agent = Agent(name="agent", model=FakeModel())

    processed = _run_with_function_tool_call(agent, "plain_tool", [func_tool])

    item = processed.new_items[0]
    assert isinstance(item, ToolCallItem)
    assert item.description == "A plain function tool"
    assert item.title is None


def test_function_tool_call_item_local_tool_does_not_inherit_mcp_title() -> None:
    """A local FunctionTool without _mcp_title does not get a title, even if an MCP
    server happens to cache a tool with the same name.

    Since _mcp_title is only set by MCPUtil.to_function_tool, a locally-created
    FunctionTool will never have it, so no cross-contamination is possible.
    """
    local_func_tool = _make_fake_function_tool("shared_tool", "Local description")
    agent = Agent(name="agent", model=FakeModel(), tools=[local_func_tool])

    processed = _run_with_function_tool_call(agent, "shared_tool", [local_func_tool])

    item = processed.new_items[0]
    assert isinstance(item, ToolCallItem)
    assert item.description == "Local description"
    assert item.title is None


def test_process_model_response_handles_compaction_item() -> None:
    agent = Agent(name="compaction-agent", model=FakeModel())
    compaction_item = ResponseCompactionItem(
        id="comp-1",
        encrypted_content="enc",
        type="compaction",
        created_by="server",
    )

    processed = run_loop.process_model_response(
        agent=agent,
        all_tools=[],
        response=_response([compaction_item]),
        output_schema=None,
        handoffs=[],
    )

    assert len(processed.new_items) == 1
    item = processed.new_items[0]
    assert isinstance(item, CompactionItem)
    assert isinstance(item.raw_item, dict)
    assert item.raw_item["type"] == "compaction"
    assert item.raw_item["encrypted_content"] == "enc"
    assert "created_by" not in item.raw_item


def test_process_model_response_classifies_tool_search_items() -> None:
    agent = Agent(name="tool-search-agent", model=FakeModel())
    tool_search_call = construct_type(
        type_=ResponseOutputItem,
        value={
            "id": "tsc_123",
            "type": "tool_search_call",
            "arguments": {"paths": ["crm"], "query": "profile"},
            "execution": "server",
            "status": "completed",
        },
    )
    tool_search_output = construct_type(
        type_=ResponseOutputItem,
        value={
            "id": "tso_123",
            "type": "tool_search_output",
            "execution": "server",
            "status": "completed",
            "tools": [
                {
                    "type": "function",
                    "name": "get_customer_profile",
                    "description": "Fetch a CRM customer profile.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "customer_id": {
                                "type": "string",
                            }
                        },
                        "required": ["customer_id"],
                    },
                    "defer_loading": True,
                }
            ],
        },
    )

    processed = run_loop.process_model_response(
        agent=agent,
        all_tools=[],
        response=_response([tool_search_call, tool_search_output]),
        output_schema=None,
        handoffs=[],
    )

    assert isinstance(processed.new_items[0], ToolSearchCallItem)
    assert isinstance(processed.new_items[0].raw_item, ResponseToolSearchCall)
    assert isinstance(processed.new_items[1], ToolSearchOutputItem)
    assert isinstance(processed.new_items[1].raw_item, ResponseToolSearchOutputItem)
    assert processed.tools_used == ["tool_search", "tool_search"]


def test_process_model_response_uses_namespace_for_duplicate_function_names() -> None:
    crm_tool = function_tool(lambda customer_id: customer_id, name_override="lookup_account")
    billing_tool = function_tool(lambda customer_id: customer_id, name_override="lookup_account")
    crm_namespace = tool_namespace(
        name="crm",
        description="CRM tools",
        tools=[crm_tool],
    )
    billing_namespace = tool_namespace(
        name="billing",
        description="Billing tools",
        tools=[billing_tool],
    )
    all_tools: list[Tool] = [*crm_namespace, *billing_namespace]
    agent = Agent(name="billing-agent", model=FakeModel(), tools=all_tools)

    processed = run_loop.process_model_response(
        agent=agent,
        all_tools=all_tools,
        response=_response(
            [
                get_function_tool_call(
                    "lookup_account",
                    '{"customer_id":"customer_42"}',
                    namespace="billing",
                )
            ]
        ),
        output_schema=None,
        handoffs=[],
    )

    assert len(processed.functions) == 1
    assert processed.functions[0].function_tool is billing_namespace[0]
    assert processed.tools_used == ["billing.lookup_account"]


def test_process_model_response_collapses_synthetic_deferred_namespace_in_tools_used() -> None:
    deferred_tool = function_tool(
        lambda city: city,
        name_override="get_weather",
        defer_loading=True,
    )
    agent = Agent(name="weather-agent", model=FakeModel(), tools=[deferred_tool])

    processed = run_loop.process_model_response(
        agent=agent,
        all_tools=[deferred_tool],
        response=_response(
            [
                get_function_tool_call(
                    "get_weather",
                    '{"city":"Tokyo"}',
                    namespace="get_weather",
                )
            ]
        ),
        output_schema=None,
        handoffs=[],
    )

    assert len(processed.functions) == 1
    assert processed.functions[0].function_tool is deferred_tool
    assert processed.tools_used == ["get_weather"]


def test_process_model_response_rejects_bare_name_for_duplicate_namespaced_functions() -> None:
    crm_tool = function_tool(lambda customer_id: customer_id, name_override="lookup_account")
    billing_tool = function_tool(lambda customer_id: customer_id, name_override="lookup_account")
    crm_namespace = tool_namespace(
        name="crm",
        description="CRM tools",
        tools=[crm_tool],
    )
    billing_namespace = tool_namespace(
        name="billing",
        description="Billing tools",
        tools=[billing_tool],
    )
    all_tools: list[Tool] = [*crm_namespace, *billing_namespace]
    agent = Agent(name="billing-agent", model=FakeModel(), tools=all_tools)

    with pytest.raises(ModelBehaviorError, match="Tool lookup_account not found"):
        run_loop.process_model_response(
            agent=agent,
            all_tools=all_tools,
            response=_response(
                [get_function_tool_call("lookup_account", '{"customer_id":"customer_42"}')]
            ),
            output_schema=None,
            handoffs=[],
        )


def test_process_model_response_uses_last_duplicate_top_level_function() -> None:
    first_tool = function_tool(lambda customer_id: f"first:{customer_id}", name_override="lookup")
    second_tool = function_tool(lambda customer_id: f"second:{customer_id}", name_override="lookup")
    all_tools: list[Tool] = [first_tool, second_tool]
    agent = Agent(name="lookup-agent", model=FakeModel(), tools=all_tools)

    processed = run_loop.process_model_response(
        agent=agent,
        all_tools=all_tools,
        response=_response([get_function_tool_call("lookup", '{"customer_id":"customer_42"}')]),
        output_schema=None,
        handoffs=[],
    )

    assert len(processed.functions) == 1
    assert processed.functions[0].function_tool is second_tool


def test_process_model_response_rejects_reserved_same_name_namespace_shape() -> None:
    invalid_tool = function_tool(lambda customer_id: customer_id, name_override="lookup_account")
    invalid_tool._tool_namespace = "lookup_account"
    invalid_tool._tool_namespace_description = "Same-name namespace"
    all_tools: list[Tool] = [invalid_tool]
    agent = Agent(name="lookup-agent", model=FakeModel(), tools=all_tools)

    with pytest.raises(UserError, match="synthetic namespace `lookup_account.lookup_account`"):
        run_loop.process_model_response(
            agent=agent,
            all_tools=all_tools,
            response=_response(
                [
                    get_function_tool_call(
                        "lookup_account",
                        '{"customer_id":"customer_42"}',
                        namespace="lookup_account",
                    )
                ]
            ),
            output_schema=None,
            handoffs=[],
        )


def test_process_model_response_rejects_qualified_name_collision_with_dotted_top_level_tool() -> (
    None
):
    dotted_top_level_tool = function_tool(
        lambda customer_id: customer_id,
        name_override="crm.lookup_account",
    )
    namespaced_tool = tool_namespace(
        name="crm",
        description="CRM tools",
        tools=[function_tool(lambda customer_id: customer_id, name_override="lookup_account")],
    )[0]
    all_tools: list[Tool] = [dotted_top_level_tool, namespaced_tool]
    agent = Agent(name="lookup-agent", model=FakeModel(), tools=all_tools)

    with pytest.raises(UserError, match="qualified name `crm.lookup_account`"):
        run_loop.process_model_response(
            agent=agent,
            all_tools=all_tools,
            response=_response(
                [
                    get_function_tool_call(
                        "lookup_account",
                        '{"customer_id":"customer_42"}',
                        namespace="crm",
                    )
                ]
            ),
            output_schema=None,
            handoffs=[],
        )


def test_process_model_response_prefers_visible_top_level_function_over_deferred_same_name_tool():
    visible_tool = function_tool(
        lambda customer_id: f"visible:{customer_id}",
        name_override="lookup_account",
    )
    deferred_tool = function_tool(
        lambda customer_id: f"deferred:{customer_id}",
        name_override="lookup_account",
        defer_loading=True,
    )
    all_tools: list[Tool] = [visible_tool, deferred_tool]
    agent = Agent(name="lookup-agent", model=FakeModel(), tools=all_tools)

    processed = run_loop.process_model_response(
        agent=agent,
        all_tools=all_tools,
        response=_response(
            [get_function_tool_call("lookup_account", '{"customer_id":"customer_42"}')]
        ),
        output_schema=None,
        handoffs=[],
    )

    assert len(processed.functions) == 1
    assert processed.functions[0].function_tool is visible_tool
    assert getattr(processed.functions[0].tool_call, "namespace", None) is None
    assert isinstance(processed.new_items[0], ToolCallItem)
    assert getattr(processed.new_items[0].raw_item, "namespace", None) is None


def test_process_model_response_uses_internal_lookup_key_for_deferred_top_level_calls() -> None:
    visible_tool = function_tool(
        lambda customer_id: f"visible:{customer_id}",
        name_override="lookup_account.lookup_account",
    )
    deferred_tool = function_tool(
        lambda customer_id: f"deferred:{customer_id}",
        name_override="lookup_account",
        defer_loading=True,
    )
    all_tools: list[Tool] = [visible_tool, deferred_tool]
    agent = Agent(name="lookup-agent", model=FakeModel(), tools=all_tools)

    processed = run_loop.process_model_response(
        agent=agent,
        all_tools=all_tools,
        response=_response(
            [
                get_function_tool_call(
                    "lookup_account",
                    '{"customer_id":"customer_42"}',
                    namespace="lookup_account",
                )
            ]
        ),
        output_schema=None,
        handoffs=[],
    )

    assert len(processed.functions) == 1
    assert processed.functions[0].function_tool is deferred_tool


def test_process_model_response_preserves_synthetic_namespace_for_deferred_top_level_tools() -> (
    None
):
    deferred_tool = function_tool(
        lambda city: city,
        name_override="get_weather",
        defer_loading=True,
    )
    all_tools: list[Tool] = [deferred_tool]
    agent = Agent(name="weather-agent", model=FakeModel(), tools=all_tools)

    processed = run_loop.process_model_response(
        agent=agent,
        all_tools=all_tools,
        response=_response(
            [get_function_tool_call("get_weather", '{"city":"Tokyo"}', namespace="get_weather")]
        ),
        output_schema=None,
        handoffs=[],
    )

    assert len(processed.functions) == 1
    assert processed.functions[0].function_tool is deferred_tool
    assert getattr(processed.functions[0].tool_call, "namespace", None) == "get_weather"
    assert isinstance(processed.new_items[0], ToolCallItem)
    assert getattr(processed.new_items[0].raw_item, "namespace", None) == "get_weather"


def test_process_model_response_prefers_namespaced_function_over_handoff_name_collision() -> None:
    billing_tool = function_tool(lambda customer_id: customer_id, name_override="lookup_account")
    billing_namespace = tool_namespace(
        name="billing",
        description="Billing tools",
        tools=[billing_tool],
    )
    handoff_target = Agent(name="lookup-agent", model=FakeModel())
    lookup_handoff: Handoff = handoff(handoff_target, tool_name_override="lookup_account")
    all_tools: list[Tool] = [*billing_namespace]
    agent = Agent(name="billing-agent", model=FakeModel(), tools=all_tools)

    processed = run_loop.process_model_response(
        agent=agent,
        all_tools=all_tools,
        response=_response(
            [
                get_function_tool_call(
                    "lookup_account",
                    '{"customer_id":"customer_42"}',
                    namespace="billing",
                )
            ]
        ),
        output_schema=None,
        handoffs=[lookup_handoff],
    )

    assert len(processed.functions) == 1
    assert processed.functions[0].function_tool is billing_namespace[0]
    assert processed.handoffs == []
    assert len(processed.new_items) == 1
    assert isinstance(processed.new_items[0], ToolCallItem)
    assert not isinstance(processed.new_items[0], HandoffCallItem)


def test_process_model_response_rejects_mismatched_function_namespace() -> None:
    bare_tool = function_tool(lambda customer_id: customer_id, name_override="lookup_account")
    all_tools: list[Tool] = [bare_tool]
    agent = Agent(name="bare-agent", model=FakeModel(), tools=all_tools)

    with pytest.raises(ModelBehaviorError, match="crm.lookup_account"):
        run_loop.process_model_response(
            agent=agent,
            all_tools=all_tools,
            response=_response(
                [
                    get_function_tool_call(
                        "lookup_account",
                        '{"customer_id":"customer_42"}',
                        namespace="crm",
                    )
                ]
            ),
            output_schema=None,
            handoffs=[],
        )
