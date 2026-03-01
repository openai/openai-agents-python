from __future__ import annotations

from typing import Any, cast

import pytest
from openai.types.responses.response_reasoning_item import ResponseReasoningItem

from agents import Agent
from agents.items import ReasoningItem, TResponseInputItem
from agents.models.fake_id import FAKE_RESPONSES_ID
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
