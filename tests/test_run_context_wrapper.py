from typing import Any

from agents.items import ToolApprovalItem
from agents.run_context import RunContextWrapper
from tests.utils.hitl import make_agent


class BrokenStr:
    def __str__(self) -> str:
        raise RuntimeError("broken")


def test_run_context_to_str_or_none_handles_errors() -> None:
    assert RunContextWrapper._to_str_or_none("ok") == "ok"
    assert RunContextWrapper._to_str_or_none(123) == "123"
    assert RunContextWrapper._to_str_or_none(BrokenStr()) is None
    assert RunContextWrapper._to_str_or_none(None) is None


def test_run_context_resolve_tool_name_and_call_id_fallbacks() -> None:
    raw: dict[str, Any] = {"name": "raw_tool", "id": "raw-id"}
    item = ToolApprovalItem(agent=make_agent(), raw_item=raw, tool_name=None)

    assert RunContextWrapper._resolve_tool_name(item) == "raw_tool"
    assert RunContextWrapper._resolve_call_id(item) == "raw-id"


def test_run_context_scopes_approvals_to_call_ids() -> None:
    wrapper: RunContextWrapper[dict[str, object]] = RunContextWrapper(context={})
    agent = make_agent()
    approval = ToolApprovalItem(agent=agent, raw_item={"type": "tool_call", "call_id": "call-1"})

    wrapper.approve_tool(approval)
    assert wrapper.is_tool_approved("tool_call", "call-1") is True

    # A different call ID should require a fresh approval.
    assert wrapper.is_tool_approved("tool_call", "call-2") is None


def test_run_context_scopes_rejections_to_call_ids() -> None:
    wrapper: RunContextWrapper[dict[str, object]] = RunContextWrapper(context={})
    agent = make_agent()
    approval = ToolApprovalItem(agent=agent, raw_item={"type": "tool_call", "call_id": "call-1"})

    wrapper.reject_tool(approval)
    assert wrapper.is_tool_approved("tool_call", "call-1") is False

    # A different call ID should require a fresh approval.
    assert wrapper.is_tool_approved("tool_call", "call-2") is None


def test_run_context_honors_global_approval_and_rejection() -> None:
    wrapper: RunContextWrapper[dict[str, object]] = RunContextWrapper(context={})
    agent = make_agent()
    approval = ToolApprovalItem(agent=agent, raw_item={"type": "tool_call", "call_id": "call-1"})

    wrapper.approve_tool(approval, always_approve=True)
    assert wrapper.is_tool_approved("tool_call", "call-2") is True

    wrapper.reject_tool(approval, always_reject=True)
    assert wrapper.is_tool_approved("tool_call", "call-3") is False


def test_run_context_stores_per_call_rejection_messages() -> None:
    wrapper: RunContextWrapper[dict[str, object]] = RunContextWrapper(context={})
    agent = make_agent()
    approval = ToolApprovalItem(agent=agent, raw_item={"type": "tool_call", "call_id": "call-1"})

    wrapper.reject_tool(approval, rejection_message="Denied by policy")

    assert wrapper.get_rejection_message("tool_call", "call-1") == "Denied by policy"
    assert wrapper.get_rejection_message("tool_call", "call-2") is None


def test_run_context_stores_sticky_rejection_messages_for_always_reject() -> None:
    wrapper: RunContextWrapper[dict[str, object]] = RunContextWrapper(context={})
    agent = make_agent()
    approval = ToolApprovalItem(agent=agent, raw_item={"type": "tool_call", "call_id": "call-1"})

    wrapper.reject_tool(approval, always_reject=True, rejection_message="")

    assert wrapper.get_rejection_message("tool_call", "call-1") == ""
    assert wrapper.get_rejection_message("tool_call", "call-2") == ""


def test_run_context_clears_rejection_message_after_approval() -> None:
    wrapper: RunContextWrapper[dict[str, object]] = RunContextWrapper(context={})
    agent = make_agent()
    approval = ToolApprovalItem(agent=agent, raw_item={"type": "tool_call", "call_id": "call-1"})

    wrapper.reject_tool(approval, rejection_message="Denied by policy")
    wrapper.approve_tool(approval)

    assert wrapper.get_rejection_message("tool_call", "call-1") is None


def test_run_context_unknown_tool_name_fallback() -> None:
    agent = make_agent()
    raw: dict[str, Any] = {}
    approval = ToolApprovalItem(agent=agent, raw_item=raw, tool_name=None)

    assert RunContextWrapper._resolve_tool_name(approval) == "unknown_tool"


def test_tool_approval_item_preserves_positional_type_argument() -> None:
    raw: dict[str, Any] = {
        "type": "function_call",
        "name": "lookup_account",
        "call_id": "call-1",
        "namespace": "billing",
    }

    approval = ToolApprovalItem(
        make_agent(),
        raw,
        "lookup_account",
        "tool_approval_item",
    )

    assert approval.type == "tool_approval_item"
    assert approval.tool_name == "lookup_account"
    assert approval.tool_namespace == "billing"
