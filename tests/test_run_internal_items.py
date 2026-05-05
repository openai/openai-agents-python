from __future__ import annotations

from typing import Any, cast

import pytest
from openai.types.responses import (
    ResponseFunctionToolCall,
    ResponseToolSearchCall,
    ResponseToolSearchOutputItem,
)
from openai.types.responses.response_reasoning_item import ResponseReasoningItem

from agents import Agent
from agents.exceptions import AgentsException
from agents.items import (
    ReasoningItem,
    ToolCallItem,
    ToolSearchCallItem,
    ToolSearchOutputItem,
    TResponseInputItem,
    coerce_tool_search_output_raw_item,
)
from agents.models.fake_id import FAKE_RESPONSES_ID
from agents.result import RunResult
from agents.run_context import RunContextWrapper
from agents.run_internal import items as run_items


def test_drop_orphan_function_calls_preserves_non_mapping_entries() -> None:
    payload: list[Any] = [
        cast(TResponseInputItem, "plain-text-input"),
        cast(TResponseInputItem, {"type": "message", "role": "user", "content": "hello"}),
        cast(
            TResponseInputItem,
            {
                "type": "function_call",
                "call_id": "orphan_call",
                "name": "orphan",
                "arguments": "{}",
            },
        ),
        cast(
            TResponseInputItem,
            {
                "type": "function_call",
                "call_id": "paired_call",
                "name": "paired",
                "arguments": "{}",
            },
        ),
        cast(
            TResponseInputItem,
            {"type": "function_call_output", "call_id": "paired_call", "output": "ok"},
        ),
        cast(TResponseInputItem, {"call_id": "not-a-tool-call"}),
    ]

    filtered = run_items.drop_orphan_function_calls(cast(list[TResponseInputItem], payload))
    filtered_values = cast(list[Any], filtered)
    assert "plain-text-input" in filtered_values
    assert cast(dict[str, Any], filtered[1])["type"] == "message"
    assert any(
        isinstance(entry, dict)
        and entry.get("type") == "function_call"
        and entry.get("call_id") == "paired_call"
        for entry in filtered
    )
    assert not any(
        isinstance(entry, dict)
        and entry.get("type") == "function_call"
        and entry.get("call_id") == "orphan_call"
        for entry in filtered
    )


def test_drop_orphan_function_calls_handles_tool_search_calls() -> None:
    payload: list[Any] = [
        cast(
            TResponseInputItem,
            {
                "type": "tool_search_call",
                "call_id": "tool_search_orphan",
                "arguments": {"query": "orphan"},
                "execution": "server",
                "status": "completed",
            },
        ),
        cast(
            TResponseInputItem,
            {
                "type": "tool_search_call",
                "call_id": "tool_search_keep",
                "arguments": {"query": "keep"},
                "execution": "server",
                "status": "completed",
            },
        ),
        cast(
            TResponseInputItem,
            {
                "type": "tool_search_output",
                "call_id": "tool_search_keep",
                "execution": "server",
                "status": "completed",
                "tools": [],
            },
        ),
    ]

    filtered = run_items.drop_orphan_function_calls(cast(list[TResponseInputItem], payload))

    assert any(
        isinstance(entry, dict)
        and entry.get("type") == "tool_search_call"
        and entry.get("call_id") == "tool_search_keep"
        for entry in filtered
    )
    assert not any(
        isinstance(entry, dict)
        and entry.get("type") == "tool_search_call"
        and entry.get("call_id") == "tool_search_orphan"
        for entry in filtered
    )


def test_drop_orphan_function_calls_preserves_hosted_tool_search_pairs_without_call_ids() -> None:
    payload: list[Any] = [
        cast(
            TResponseInputItem,
            {
                "type": "tool_search_call",
                "call_id": None,
                "arguments": {"query": "keep"},
                "execution": "server",
                "status": "completed",
            },
        ),
        cast(
            TResponseInputItem,
            {
                "type": "tool_search_output",
                "call_id": None,
                "execution": "server",
                "status": "completed",
                "tools": [],
            },
        ),
    ]

    filtered = run_items.drop_orphan_function_calls(cast(list[TResponseInputItem], payload))

    assert len(filtered) == 2
    assert cast(dict[str, Any], filtered[0])["type"] == "tool_search_call"
    assert cast(dict[str, Any], filtered[1])["type"] == "tool_search_output"


def test_drop_orphan_function_calls_matches_latest_anonymous_tool_search_call() -> None:
    payload: list[Any] = [
        cast(
            TResponseInputItem,
            {
                "type": "tool_search_call",
                "call_id": None,
                "arguments": {"query": "orphan"},
                "execution": "server",
                "status": "completed",
            },
        ),
        cast(
            TResponseInputItem,
            {
                "type": "tool_search_call",
                "call_id": None,
                "arguments": {"query": "paired"},
                "execution": "server",
                "status": "completed",
            },
        ),
        cast(
            TResponseInputItem,
            {
                "type": "tool_search_output",
                "call_id": None,
                "execution": "server",
                "status": "completed",
                "tools": [],
            },
        ),
    ]

    filtered = run_items.drop_orphan_function_calls(cast(list[TResponseInputItem], payload))

    assert [cast(dict[str, Any], item)["type"] for item in filtered] == [
        "tool_search_call",
        "tool_search_output",
    ]
    assert cast(dict[str, Any], filtered[0])["arguments"] == {"query": "paired"}


def test_drop_orphan_function_calls_does_not_pair_named_tool_search_with_anonymous_output() -> None:
    payload: list[Any] = [
        cast(
            TResponseInputItem,
            {
                "type": "tool_search_call",
                "call_id": "orphan_search",
                "arguments": {"query": "keep"},
                "execution": "server",
                "status": "completed",
            },
        ),
        cast(
            TResponseInputItem,
            {
                "type": "tool_search_output",
                "call_id": None,
                "execution": "server",
                "status": "completed",
                "tools": [],
            },
        ),
    ]

    filtered = run_items.drop_orphan_function_calls(cast(list[TResponseInputItem], payload))

    assert [cast(dict[str, Any], item)["type"] for item in filtered] == ["tool_search_output"]


def test_normalize_and_ensure_input_item_format_keep_non_dict_entries() -> None:
    item = cast(TResponseInputItem, "raw-item")
    assert run_items.ensure_input_item_format(item) == item
    assert run_items.normalize_input_items_for_api([item]) == [item]


def test_fingerprint_input_item_handles_edge_cases(monkeypatch: pytest.MonkeyPatch) -> None:
    assert run_items.fingerprint_input_item(None) is None

    fingerprint = run_items.fingerprint_input_item(
        cast(
            TResponseInputItem, {"id": "id-1", "type": "message", "role": "user", "content": "hi"}
        ),
        ignore_ids_for_matching=True,
    )
    assert fingerprint is not None
    assert '"id"' not in fingerprint

    class _BrokenModelDump:
        def model_dump(self, *_args: Any, **kwargs: Any) -> dict[str, Any]:
            if "warnings" in kwargs:
                raise TypeError("warnings arg unsupported")
            raise RuntimeError("still broken")

    assert run_items.fingerprint_input_item(_BrokenModelDump()) is None
    assert run_items._model_dump_without_warnings(object()) is None

    class _Opaque:
        pass

    monkeypatch.setattr(
        run_items,
        "ensure_input_item_format",
        lambda _item: {"id": "internal-id", "type": "message", "role": "user", "content": "x"},
    )
    opaque_fingerprint = run_items.fingerprint_input_item(_Opaque(), ignore_ids_for_matching=True)
    assert opaque_fingerprint is not None
    assert '"id"' not in opaque_fingerprint


def test_deduplicate_input_items_handles_fake_ids_and_approval_request_ids() -> None:
    items: list[Any] = [
        cast(
            TResponseInputItem,
            {
                "type": "function_call_output",
                "id": FAKE_RESPONSES_ID,
                "call_id": "call-1",
                "output": "first",
            },
        ),
        cast(
            TResponseInputItem,
            {
                "type": "function_call_output",
                "id": FAKE_RESPONSES_ID,
                "call_id": "call-1",
                "output": "latest",
            },
        ),
        cast(
            TResponseInputItem,
            {
                "type": "mcp_approval_response",
                "approval_request_id": "req-1",
                "approve": True,
            },
        ),
        cast(
            TResponseInputItem,
            {
                "type": "mcp_approval_response",
                "approval_request_id": "req-1",
                "approve": False,
            },
        ),
        cast(TResponseInputItem, "plain"),
    ]

    deduplicated = run_items.deduplicate_input_items(cast(list[TResponseInputItem], items))
    assert len(deduplicated) == 3
    assert cast(list[Any], deduplicated)[-1] == "plain"

    latest = run_items.deduplicate_input_items_preferring_latest(
        cast(list[TResponseInputItem], items[:2])
    )
    assert len(latest) == 1
    latest_output = cast(dict[str, Any], latest[0])
    assert latest_output["output"] == "latest"


def test_extract_mcp_request_id_supports_dicts_and_objects() -> None:
    assert (
        run_items.extract_mcp_request_id(
            {"provider_data": {"id": "provider-id"}, "id": "fallback-id"}
        )
        == "provider-id"
    )
    assert run_items.extract_mcp_request_id({"call_id": "call-id"}) == "call-id"

    class _WithProviderData:
        provider_data = {"id": "from-provider"}

    assert run_items.extract_mcp_request_id(_WithProviderData()) == "from-provider"

    class _BrokenObject:
        @property
        def provider_data(self) -> dict[str, Any]:
            raise RuntimeError("boom")

        def __getattr__(self, _name: str) -> Any:
            raise RuntimeError("boom")

    assert run_items.extract_mcp_request_id(_BrokenObject()) is None


def test_extract_mcp_request_id_from_run_variants() -> None:
    class _Run:
        def __init__(self, request_item: Any = None, requestItem: Any = None) -> None:
            self.request_item = request_item
            self.requestItem = requestItem

    class _RequestObject:
        provider_data = {"id": "provider-object"}
        id = "object-id"
        call_id = "object-call-id"

    assert (
        run_items.extract_mcp_request_id_from_run(
            _Run(request_item={"provider_data": {"id": "provider-dict"}, "id": "fallback"})
        )
        == "provider-dict"
    )
    assert (
        run_items.extract_mcp_request_id_from_run(_Run(request_item={"id": "dict-id"})) == "dict-id"
    )
    assert (
        run_items.extract_mcp_request_id_from_run(_Run(request_item=_RequestObject()))
        == "provider-object"
    )
    assert (
        run_items.extract_mcp_request_id_from_run(_Run(requestItem={"call_id": "camel-call"}))
        == "camel-call"
    )


def test_run_item_to_input_item_preserves_reasoning_item_ids_by_default() -> None:
    agent = Agent(name="A")
    reasoning = ReasoningItem(
        agent=agent,
        raw_item=ResponseReasoningItem(
            type="reasoning",
            id="rs_123",
            summary=[],
        ),
    )

    result = run_items.run_item_to_input_item(reasoning)

    assert isinstance(result, dict)
    assert result.get("type") == "reasoning"
    assert result.get("id") == "rs_123"


def test_run_item_to_input_item_omits_reasoning_item_ids_when_configured() -> None:
    agent = Agent(name="A")
    reasoning = ReasoningItem(
        agent=agent,
        raw_item=ResponseReasoningItem(
            type="reasoning",
            id="rs_456",
            summary=[],
        ),
    )

    result = run_items.run_item_to_input_item(reasoning, "omit")

    assert isinstance(result, dict)
    assert result.get("type") == "reasoning"
    assert "id" not in result


def test_run_item_to_input_item_preserves_tool_search_items() -> None:
    agent = Agent(name="A")
    tool_search_call = ToolSearchCallItem(
        agent=agent,
        raw_item={"type": "tool_search_call", "queries": [{"search_term": "profile"}]},
    )
    tool_search_output = ToolSearchOutputItem(
        agent=agent,
        raw_item={"type": "tool_search_output", "results": [{"text": "Customer profile"}]},
    )

    converted_call = run_items.run_item_to_input_item(tool_search_call)
    converted_output = run_items.run_item_to_input_item(tool_search_output)

    assert isinstance(converted_call, dict)
    assert converted_call["type"] == "tool_search_call"
    assert isinstance(converted_output, dict)
    assert converted_output["type"] == "tool_search_output"


def test_run_item_to_input_item_strips_tool_search_created_by() -> None:
    agent = Agent(name="A")
    tool_search_call = ToolSearchCallItem(
        agent=agent,
        raw_item=ResponseToolSearchCall(
            id="tsc_123",
            type="tool_search_call",
            arguments={"query": "profile"},
            execution="client",
            status="completed",
            created_by="server",
        ),
    )
    tool_search_output = ToolSearchOutputItem(
        agent=agent,
        raw_item=ResponseToolSearchOutputItem(
            id="tso_123",
            type="tool_search_output",
            execution="client",
            status="completed",
            tools=[],
            created_by="server",
        ),
    )

    converted_call = run_items.run_item_to_input_item(tool_search_call)
    converted_output = run_items.run_item_to_input_item(tool_search_output)

    assert isinstance(converted_call, dict)
    assert converted_call["type"] == "tool_search_call"
    assert "created_by" not in converted_call
    assert isinstance(converted_output, dict)
    assert converted_output["type"] == "tool_search_output"
    assert "created_by" not in converted_output


def test_run_item_to_input_item_omits_tool_call_metadata() -> None:
    agent = Agent(name="A")
    tool_call = ToolCallItem(
        agent=agent,
        raw_item=ResponseFunctionToolCall(
            id="fc_123",
            call_id="call_123",
            name="lookup_account",
            arguments="{}",
            type="function_call",
            status="completed",
        ),
        description="Lookup customer records.",
        title="Lookup Account",
    )

    result = run_items.run_item_to_input_item(tool_call)
    result_dict = cast(dict[str, Any], result)

    assert isinstance(result, dict)
    assert result_dict["type"] == "function_call"
    assert "description" not in result_dict
    assert "title" not in result_dict


def test_normalize_input_items_for_api_strips_internal_tool_call_metadata() -> None:
    item = cast(
        TResponseInputItem,
        {
            "type": "function_call",
            "call_id": "call_123",
            "name": "lookup_account",
            "arguments": "{}",
            run_items.TOOL_CALL_SESSION_DESCRIPTION_KEY: "Lookup customer records.",
            run_items.TOOL_CALL_SESSION_TITLE_KEY: "Lookup Account",
        },
    )

    normalized = run_items.normalize_input_items_for_api([item])
    normalized_item = cast(dict[str, Any], normalized[0])

    assert run_items.TOOL_CALL_SESSION_DESCRIPTION_KEY not in normalized_item
    assert run_items.TOOL_CALL_SESSION_TITLE_KEY not in normalized_item


def test_fingerprint_input_item_ignores_internal_tool_call_metadata() -> None:
    base_item = cast(
        TResponseInputItem,
        {
            "type": "function_call",
            "call_id": "call_123",
            "name": "lookup_account",
            "arguments": "{}",
        },
    )
    with_metadata = cast(
        TResponseInputItem,
        {
            **cast(dict[str, Any], base_item),
            run_items.TOOL_CALL_SESSION_DESCRIPTION_KEY: "Lookup customer records.",
            run_items.TOOL_CALL_SESSION_TITLE_KEY: "Lookup Account",
        },
    )

    assert run_items.fingerprint_input_item(base_item) == run_items.fingerprint_input_item(
        with_metadata
    )


def test_run_result_to_input_list_preserves_tool_search_items() -> None:
    agent = Agent(name="A")
    result = RunResult(
        input="Find CRM tools",
        new_items=[
            ToolSearchCallItem(
                agent=agent,
                raw_item={"type": "tool_search_call", "queries": [{"search_term": "profile"}]},
            ),
            ToolSearchOutputItem(
                agent=agent,
                raw_item={"type": "tool_search_output", "results": [{"text": "Customer profile"}]},
            ),
        ],
        raw_responses=[],
        final_output="done",
        input_guardrail_results=[],
        output_guardrail_results=[],
        tool_input_guardrail_results=[],
        tool_output_guardrail_results=[],
        context_wrapper=RunContextWrapper(context=None),
        _last_agent=agent,
    )

    input_items = result.to_input_list()

    assert len(input_items) == 3
    assert cast(dict[str, Any], input_items[1])["type"] == "tool_search_call"
    assert cast(dict[str, Any], input_items[2])["type"] == "tool_search_output"


def test_coerce_tool_search_output_raw_item_rejects_legacy_type() -> None:
    with pytest.raises(AgentsException, match="Unexpected tool search output item type"):
        coerce_tool_search_output_raw_item({"type": "tool_search_result", "results": []})
