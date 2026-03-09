from typing import Any, cast

import pytest
from mcp import Tool as MCPTool
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
from openai.types.responses.response_output_item import McpCall, McpListTools, McpListToolsTool

from agents import (
    Agent,
    ApplyPatchTool,
    CompactionItem,
    Handoff,
    HostedMCPTool,
    ShellTool,
    Tool,
    function_tool,
    handoff,
    tool_namespace,
)
from agents.exceptions import ModelBehaviorError, UserError
from agents.items import (
    HandoffCallItem,
    MCPListToolsItem,
    ModelResponse,
    ToolCallItem,
    ToolCallOutputItem,
    ToolSearchCallItem,
    ToolSearchOutputItem,
)
from agents.mcp.util import MCPUtil
from agents.run_internal import run_loop
from agents.usage import Usage
from tests.fake_model import FakeModel
from tests.mcp.helpers import FakeMCPServer
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


def _make_hosted_mcp_list_tools(server_label: str, tool_name: str) -> McpListTools:
    return McpListTools(
        id=f"list_{server_label}",
        server_label=server_label,
        tools=[
            McpListToolsTool(
                name=tool_name,
                input_schema={},
                description="Search the docs.",
                annotations={"title": "Search Docs"},
            )
        ],
        type="mcp_list_tools",
    )


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


def test_process_model_response_sets_title_for_local_mcp_function_tool() -> None:
    agent = Agent(name="local-mcp", model=FakeModel())
    mcp_tool = MCPTool(name="search_docs", inputSchema={}, description=None, title="Search Docs")
    function_tool = MCPUtil.to_function_tool(
        mcp_tool,
        FakeMCPServer(),
        convert_schemas_to_strict=False,
    )
    tool_call = ResponseFunctionToolCall(
        type="function_call",
        name="search_docs",
        call_id="call_search_docs",
        status="completed",
        arguments="{}",
    )

    processed = run_loop.process_model_response(
        agent=agent,
        all_tools=[function_tool],
        response=_response([tool_call]),
        output_schema=None,
        handoffs=[],
    )

    assert len(processed.new_items) == 1
    item = processed.new_items[0]
    assert isinstance(item, ToolCallItem)
    assert item.description == "Search Docs"
    assert item.title == "Search Docs"


def test_process_model_response_uses_mcp_list_tools_metadata_for_hosted_mcp_calls() -> None:
    agent = Agent(name="hosted-mcp", model=FakeModel())
    hosted_tool = HostedMCPTool(
        tool_config=cast(
            Any,
            {
                "type": "mcp",
                "server_label": "docs_server",
                "server_url": "https://example.com/mcp",
            },
        )
    )
    existing_items = [
        MCPListToolsItem(
            agent=agent,
            raw_item=_make_hosted_mcp_list_tools("docs_server", "search_docs"),
        )
    ]
    mcp_call = McpCall(
        id="mcp_call_1",
        arguments="{}",
        name="search_docs",
        server_label="docs_server",
        type="mcp_call",
        status="completed",
    )

    processed = run_loop.process_model_response(
        agent=agent,
        all_tools=[hosted_tool],
        response=_response([mcp_call]),
        output_schema=None,
        handoffs=[],
        existing_items=existing_items,
    )

    assert len(processed.new_items) == 1
    item = processed.new_items[0]
    assert isinstance(item, ToolCallItem)
    assert item.description == "Search the docs."
    assert item.title == "Search Docs"


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
