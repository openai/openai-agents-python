from __future__ import annotations

import pytest
from openai.types.responses.response_output_item import McpApprovalRequest

from agents import Agent, ModelBehaviorError, RunContextWrapper, ToolApprovalItem, UserError

from .utils.factories import make_tool_approval_item


def _make_hosted_mcp_approval_item(
    agent: Agent[None],
    *,
    request_id: str,
    server_label: str,
    tool_name: str = "lookup_account",
) -> ToolApprovalItem:
    return ToolApprovalItem(
        agent=agent,
        raw_item=McpApprovalRequest(
            id=request_id,
            type="mcp_approval_request",
            arguments="{}",
            name=tool_name,
            server_label=server_label,
        ),
    )


def test_hosted_mcp_permanent_approval_is_scoped_by_server_label() -> None:
    agent = Agent(name="test-agent")
    context_wrapper = RunContextWrapper(context=None)
    server_a = _make_hosted_mcp_approval_item(
        agent,
        request_id="request-a-1",
        server_label="server-a",
    )
    server_a_next = _make_hosted_mcp_approval_item(
        agent,
        request_id="request-a-2",
        server_label="server-a",
    )
    server_b = _make_hosted_mcp_approval_item(
        agent,
        request_id="request-b-1",
        server_label="server-b",
    )

    context_wrapper.approve_tool(server_a, always_approve=True)

    assert (
        context_wrapper.get_approval_status(
            "lookup_account",
            "request-a-2",
            existing_pending=server_a_next,
        )
        is True
    )
    assert (
        context_wrapper.get_approval_status(
            "lookup_account",
            "request-b-1",
            existing_pending=server_b,
        )
        is None
    )
    assert context_wrapper.is_tool_approved("lookup_account", "request-a-1") is True
    assert context_wrapper.is_tool_approved("lookup_account", "request-a-2") is None
    assert "lookup_account" not in context_wrapper._approvals


def test_hosted_mcp_permanent_rejection_message_is_scoped_by_server_label() -> None:
    agent = Agent(name="test-agent")
    context_wrapper = RunContextWrapper(context=None)
    server_a = _make_hosted_mcp_approval_item(
        agent,
        request_id="request-a-1",
        server_label="server-a",
    )
    server_a_next = _make_hosted_mcp_approval_item(
        agent,
        request_id="request-a-2",
        server_label="server-a",
    )
    server_b = _make_hosted_mcp_approval_item(
        agent,
        request_id="request-b-1",
        server_label="server-b",
    )

    context_wrapper.reject_tool(
        server_a,
        always_reject=True,
        rejection_message="server-a denied",
    )

    assert (
        context_wrapper.get_rejection_message(
            "lookup_account",
            "request-a-2",
            existing_pending=server_a_next,
        )
        == "server-a denied"
    )
    assert (
        context_wrapper.get_approval_status(
            "lookup_account",
            "request-b-1",
            existing_pending=server_b,
        )
        is None
    )
    assert (
        context_wrapper.get_rejection_message(
            "lookup_account",
            "request-b-1",
            existing_pending=server_b,
        )
        is None
    )


@pytest.mark.parametrize(
    ("approved", "always"),
    [(True, False), (True, True), (False, False), (False, True)],
)
def test_hosted_mcp_name_based_query_preserves_exact_call_decision(
    approved: bool,
    always: bool,
) -> None:
    agent = Agent(name="test-agent")
    context_wrapper = RunContextWrapper(context=None)
    approval_item = _make_hosted_mcp_approval_item(
        agent,
        request_id="request-a-1",
        server_label="server-a",
    )

    if approved:
        context_wrapper.approve_tool(approval_item, always_approve=always)
    else:
        context_wrapper.reject_tool(
            approval_item,
            always_reject=always,
            rejection_message="server-a denied",
        )

    assert context_wrapper.is_tool_approved("lookup_account", "request-a-1") is approved
    assert context_wrapper.is_tool_approved("lookup_account", "request-a-2") is None
    if not approved:
        assert (
            context_wrapper.get_rejection_message("lookup_account", "request-a-1")
            == "server-a denied"
        )


def test_hosted_mcp_exact_query_precedes_colliding_function_sticky_decision() -> None:
    agent = Agent(name="test-agent")
    context_wrapper = RunContextWrapper(context=None)
    function_item = make_tool_approval_item(
        agent,
        call_id="function-call",
        name="lookup_account",
    )
    hosted_item = _make_hosted_mcp_approval_item(
        agent,
        request_id="hosted-call",
        server_label="server-a",
    )

    context_wrapper.approve_tool(function_item, always_approve=True)
    context_wrapper.reject_tool(hosted_item, rejection_message="hosted denial")

    assert context_wrapper.is_tool_approved("lookup_account", "hosted-call") is False
    assert context_wrapper.is_tool_approved("lookup_account", "function-next") is True
    assert context_wrapper.get_rejection_message("lookup_account", "hosted-call") == "hosted denial"


@pytest.mark.parametrize("hosted_approved", [True, False])
def test_hosted_mcp_exact_query_does_not_inherit_function_rejection_reason(
    hosted_approved: bool,
) -> None:
    agent = Agent(name="test-agent")
    context_wrapper = RunContextWrapper(context=None)
    function_item = make_tool_approval_item(
        agent,
        call_id="function-call",
        name="lookup_account",
    )
    hosted_item = _make_hosted_mcp_approval_item(
        agent,
        request_id="shared-call",
        server_label="server-a",
    )

    context_wrapper.reject_tool(function_item, rejection_message="function denial")
    if hosted_approved:
        context_wrapper.approve_tool(hosted_item)
    else:
        context_wrapper.reject_tool(hosted_item)

    assert context_wrapper.is_tool_approved("lookup_account", "shared-call") is hosted_approved
    assert context_wrapper.get_rejection_message("lookup_account", "shared-call") is None


@pytest.mark.parametrize("approved", [True, False])
def test_hosted_mcp_exact_query_does_not_authorize_other_server(approved: bool) -> None:
    agent = Agent(name="test-agent")
    context_wrapper = RunContextWrapper(context=None)
    server_a = _make_hosted_mcp_approval_item(
        agent,
        request_id="shared-request",
        server_label="server-a",
    )
    server_b = _make_hosted_mcp_approval_item(
        agent,
        request_id="shared-request",
        server_label="server-b",
    )

    if approved:
        context_wrapper.approve_tool(server_a)
    else:
        context_wrapper.reject_tool(server_a, rejection_message="server-a denied")

    assert context_wrapper.is_tool_approved("lookup_account", "shared-request") is approved
    assert (
        context_wrapper.get_approval_status(
            "lookup_account",
            "shared-request",
            existing_pending=server_b,
        )
        is None
    )
    assert (
        context_wrapper.get_rejection_message(
            "lookup_account",
            "shared-request",
            existing_pending=server_b,
        )
        is None
    )


def test_hosted_mcp_legacy_bare_name_approval_does_not_grant_access() -> None:
    agent = Agent(name="test-agent")
    context_wrapper = RunContextWrapper(context=None)
    pending = _make_hosted_mcp_approval_item(
        agent,
        request_id="request-a-1",
        server_label="server-a",
    )
    context_wrapper._rebuild_approvals(  # noqa: SLF001
        {"lookup_account": {"approved": True, "rejected": []}}
    )

    assert (
        context_wrapper.get_approval_status(
            "lookup_account",
            "request-a-1",
            existing_pending=pending,
        )
        is None
    )


def test_hosted_mcp_scoped_identity_cannot_alias_legacy_tool_name() -> None:
    agent = Agent(name="test-agent")
    context_wrapper = RunContextWrapper(context=None)
    pending = _make_hosted_mcp_approval_item(
        agent,
        request_id="request-a-1",
        server_label="server-a",
    )
    colliding_legacy_name = '["hosted_mcp","server-a","lookup_account"]'
    context_wrapper._rebuild_approvals(  # noqa: SLF001
        {colliding_legacy_name: {"approved": True, "rejected": []}}
    )

    assert context_wrapper.is_tool_approved(colliding_legacy_name, "legacy-call") is True
    assert (
        context_wrapper.get_approval_status(
            "lookup_account",
            "request-a-1",
            existing_pending=pending,
        )
        is None
    )


def test_hosted_mcp_legacy_exact_call_decisions_remain_usable() -> None:
    agent = Agent(name="test-agent")
    context_wrapper = RunContextWrapper(context=None)
    approved = _make_hosted_mcp_approval_item(
        agent,
        request_id="request-approved",
        server_label="server-a",
    )
    rejected = _make_hosted_mcp_approval_item(
        agent,
        request_id="request-rejected",
        server_label="server-a",
    )
    rejected_without_raw_name = ToolApprovalItem(
        agent=agent,
        raw_item={
            "type": "hosted_tool_call",
            "provider_data": {
                "type": "mcp_approval_request",
                "id": "request-rejected",
            },
        },
        tool_name="lookup_account",
    )
    context_wrapper._rebuild_approvals(  # noqa: SLF001
        {
            "lookup_account": {
                "approved": ["request-approved"],
                "rejected": ["request-rejected"],
                "rejection_messages": {"request-rejected": "legacy exact denial"},
            }
        }
    )
    context_wrapper._allow_legacy_approval_binding_reconstruction = True  # noqa: SLF001

    assert (
        context_wrapper.get_approval_status(
            "lookup_account",
            "request-approved",
            existing_pending=approved,
        )
        is True
    )
    assert (
        context_wrapper.get_approval_status(
            "lookup_account",
            "request-rejected",
            existing_pending=rejected,
        )
        is False
    )
    assert (
        context_wrapper.get_rejection_message(
            "lookup_account",
            "request-rejected",
            existing_pending=rejected_without_raw_name,
        )
        == "legacy exact denial"
    )
    assert (
        context_wrapper.get_approval_status(
            "lookup_account",
            "request-rejected",
            existing_pending=rejected_without_raw_name,
        )
        is None
    )


def test_hosted_mcp_persistent_decision_requires_complete_identity() -> None:
    agent = Agent(name="test-agent")
    context_wrapper = RunContextWrapper(context=None)
    malformed = ToolApprovalItem(
        agent=agent,
        raw_item={
            "type": "hosted_tool_call",
            "name": "lookup_account",
            "provider_data": {
                "type": "mcp_approval_request",
                "id": "request-a-1",
            },
        },
    )

    with pytest.raises(UserError, match="non-empty server_label and tool name"):
        context_wrapper.approve_tool(malformed, always_approve=True)


def test_incomplete_hosted_mcp_uses_only_exact_call_decisions() -> None:
    agent = Agent(name="test-agent")
    malformed = ToolApprovalItem(
        agent=agent,
        raw_item={
            "type": "hosted_tool_call",
            "name": "lookup_account",
            "id": "request-a-1",
            "provider_data": {
                "type": "mcp_approval_request",
                "id": "request-a-1",
            },
        },
    )
    context_wrapper = RunContextWrapper(context=None)
    context_wrapper._rebuild_approvals(  # noqa: SLF001
        {
            "lookup_account": {
                "approved": True,
                "rejected": True,
                "sticky_rejection_message": "legacy denial",
            }
        }
    )

    assert (
        context_wrapper.get_approval_status(
            "lookup_account",
            "request-a-1",
            existing_pending=malformed,
        )
        is None
    )
    assert (
        context_wrapper.get_rejection_message(
            "lookup_account",
            "request-a-1",
            existing_pending=malformed,
        )
        is None
    )

    with pytest.raises(ModelBehaviorError, match="canonical invocation identity"):
        context_wrapper.approve_tool(malformed)
    with pytest.raises(ModelBehaviorError, match="canonical invocation identity"):
        context_wrapper.reject_tool(malformed, rejection_message="exact denial")


def test_hosted_mcp_decision_requires_request_id() -> None:
    agent = Agent(name="test-agent")
    context_wrapper = RunContextWrapper(context=None)
    malformed = ToolApprovalItem(
        agent=agent,
        raw_item={
            "type": "hosted_tool_call",
            "name": "lookup_account",
            "provider_data": {
                "type": "mcp_approval_request",
                "server_label": "server-a",
            },
        },
    )

    with pytest.raises(UserError, match="non-empty request id"):
        context_wrapper.approve_tool(malformed)

    assert context_wrapper._approvals == {}  # noqa: SLF001


@pytest.mark.parametrize("request_id", ["", 123])
def test_hosted_mcp_invalid_request_id_does_not_mutate_approvals(request_id: object) -> None:
    agent = Agent(name="test-agent")
    context_wrapper = RunContextWrapper(context=None)
    malformed = ToolApprovalItem(
        agent=agent,
        raw_item={
            "type": "mcp_approval_request",
            "id": request_id,
            "arguments": "{}",
            "name": "lookup_account",
            "server_label": "server-a",
        },
    )

    with pytest.raises(UserError, match="non-empty request id"):
        context_wrapper.reject_tool(
            malformed,
            always_reject=True,
            rejection_message="must not persist",
        )

    assert context_wrapper._approvals == {}  # noqa: SLF001


def test_hosted_mcp_provider_invalid_request_id_does_not_fall_back_to_outer_id() -> None:
    agent = Agent(name="test-agent")
    context_wrapper = RunContextWrapper(context=None)
    malformed = ToolApprovalItem(
        agent=agent,
        raw_item={
            "type": "hosted_tool_call",
            "call_id": "outer-id",
            "name": "lookup_account",
            "provider_data": {
                "type": "mcp_approval_request",
                "id": 123,
                "server_label": "server-a",
                "name": "lookup_account",
            },
        },
    )

    with pytest.raises(UserError, match="non-empty request id"):
        context_wrapper.approve_tool(malformed)

    assert context_wrapper._approvals == {}  # noqa: SLF001


def test_hosted_mcp_request_type_is_not_used_as_missing_tool_name() -> None:
    agent = Agent(name="test-agent")
    context_wrapper = RunContextWrapper(context=None)
    malformed = ToolApprovalItem(
        agent=agent,
        raw_item={
            "type": "mcp_approval_request",
            "id": "request-a-1",
            "arguments": "{}",
            "server_label": "server-a",
        },
    )

    with pytest.raises(UserError, match="non-empty server_label and tool name"):
        context_wrapper.approve_tool(malformed, always_approve=True)

    assert context_wrapper._approvals == {}  # noqa: SLF001


def test_latest_approval_decision_wins_for_call_id() -> None:
    agent = Agent(name="test-agent")
    context_wrapper = RunContextWrapper(context=None)
    approval_item = make_tool_approval_item(agent, call_id="call-1", name="test_tool")

    context_wrapper.approve_tool(approval_item)
    assert context_wrapper.is_tool_approved("test_tool", "call-1") is True

    context_wrapper.reject_tool(approval_item)
    assert context_wrapper.is_tool_approved("test_tool", "call-1") is False

    context_wrapper.approve_tool(approval_item)
    assert context_wrapper.is_tool_approved("test_tool", "call-1") is True


def test_namespaced_approval_status_does_not_fall_back_to_bare_tool_decisions() -> None:
    agent = Agent(name="test-agent")
    context_wrapper = RunContextWrapper(context=None)
    bare_item = make_tool_approval_item(agent, call_id="call-bare", name="lookup_account")
    billing_item = make_tool_approval_item(
        agent,
        call_id="call-billing",
        name="lookup_account",
        namespace="billing",
    )

    context_wrapper.approve_tool(bare_item, always_approve=True)

    assert (
        context_wrapper.get_approval_status(
            "lookup_account",
            "call-billing-2",
            tool_namespace="billing",
            existing_pending=billing_item,
        )
        is None
    )
    assert (
        context_wrapper.get_approval_status(
            "lookup_account",
            "call-billing-2",
            existing_pending=billing_item,
        )
        is None
    )


def test_namespaced_rejection_message_does_not_fall_back_to_bare_tool_decisions() -> None:
    agent = Agent(name="test-agent")
    context_wrapper = RunContextWrapper(context=None)
    bare_item = make_tool_approval_item(agent, call_id="call-bare", name="lookup_account")
    billing_item = make_tool_approval_item(
        agent,
        call_id="call-billing",
        name="lookup_account",
        namespace="billing",
    )

    context_wrapper.reject_tool(bare_item, always_reject=True, rejection_message="bare denial")

    assert (
        context_wrapper.get_rejection_message(
            "lookup_account",
            "call-billing-2",
            tool_namespace="billing",
            existing_pending=billing_item,
        )
        is None
    )
    assert context_wrapper.get_rejection_message("lookup_account", "call-bare-2") == "bare denial"


def test_deferred_top_level_per_call_approval_keeps_bare_name_lookup() -> None:
    agent = Agent(name="test-agent")
    context_wrapper = RunContextWrapper(context=None)
    deferred_item = make_tool_approval_item(
        agent,
        call_id="call-weather",
        name="get_weather",
        namespace="get_weather",
        allow_bare_name_alias=True,
    )

    context_wrapper.approve_tool(deferred_item)

    assert context_wrapper.is_tool_approved("get_weather", "call-weather") is True


def test_deferred_top_level_rejection_message_keeps_bare_name_lookup() -> None:
    agent = Agent(name="test-agent")
    context_wrapper = RunContextWrapper(context=None)
    deferred_item = make_tool_approval_item(
        agent,
        call_id="call-weather",
        name="get_weather",
        namespace="get_weather",
        allow_bare_name_alias=True,
    )

    context_wrapper.reject_tool(deferred_item, rejection_message="weather denied")

    assert context_wrapper.get_rejection_message("get_weather", "call-weather") == "weather denied"


def test_deferred_top_level_permanent_approval_does_not_alias_to_bare_name() -> None:
    agent = Agent(name="test-agent")
    context_wrapper = RunContextWrapper(context=None)
    deferred_item = make_tool_approval_item(
        agent,
        call_id="call-weather",
        name="get_weather",
        namespace="get_weather",
        allow_bare_name_alias=True,
    )

    context_wrapper.approve_tool(deferred_item, always_approve=True)

    assert context_wrapper.is_tool_approved("get_weather", "call-weather-2") is None
    assert "deferred_top_level:get_weather" in context_wrapper._approvals
    assert (
        context_wrapper.get_approval_status(
            "get_weather",
            "call-weather-2",
            tool_namespace="get_weather",
            existing_pending=deferred_item,
        )
        is True
    )


def test_deferred_top_level_legacy_permanent_approval_key_still_restores() -> None:
    agent = Agent(name="test-agent")
    context_wrapper = RunContextWrapper(context=None)
    deferred_item = make_tool_approval_item(
        agent,
        call_id="call-weather",
        name="get_weather",
        namespace="get_weather",
        allow_bare_name_alias=True,
    )

    context_wrapper._rebuild_approvals(  # noqa: SLF001
        {"get_weather.get_weather": {"approved": True, "rejected": []}}
    )
    context_wrapper._allow_legacy_approval_binding_reconstruction = True  # noqa: SLF001

    assert (
        context_wrapper.get_approval_status(
            "get_weather",
            "call-weather-2",
            tool_namespace="get_weather",
            existing_pending=deferred_item,
        )
        is True
    )


def test_rebuild_approvals_ignores_malformed_approval_values() -> None:
    context_wrapper = RunContextWrapper(context=None)

    context_wrapper._rebuild_approvals(["not", "a", "mapping"])  # noqa: SLF001
    assert context_wrapper._approvals == {}

    context_wrapper._rebuild_approvals(  # noqa: SLF001
        {
            "get_weather": {
                "approved": {"not": "valid"},
                "rejected": ["call-denied", 123],
                "rejection_messages": {"call-denied": "no"},
            },
            123: {"approved": True},
        }
    )

    assert context_wrapper.is_tool_approved("get_weather", "any-call") is None
    assert context_wrapper.is_tool_approved("get_weather", "call-denied") is False
    assert context_wrapper.get_rejection_message("get_weather", "call-denied") == "no"
    assert context_wrapper.is_tool_approved("123", "any-call") is None


def test_deferred_top_level_approval_does_not_alias_to_visible_bare_sibling() -> None:
    agent = Agent(name="test-agent")
    context_wrapper = RunContextWrapper(context=None)
    deferred_item = make_tool_approval_item(
        agent,
        call_id="call-lookup",
        name="lookup_account",
        namespace="lookup_account",
        allow_bare_name_alias=False,
    )

    context_wrapper.approve_tool(deferred_item, always_approve=True)

    assert context_wrapper.is_tool_approved("lookup_account", "call-visible-2") is None
    assert (
        context_wrapper.get_approval_status(
            "lookup_account",
            "call-deferred-2",
            tool_namespace="lookup_account",
            existing_pending=deferred_item,
        )
        is True
    )


def test_explicit_same_name_namespace_does_not_alias_to_bare_tool() -> None:
    agent = Agent(name="test-agent")
    context_wrapper = RunContextWrapper(context=None)
    explicit_namespaced_item = make_tool_approval_item(
        agent,
        call_id="call-namespaced",
        name="lookup_account",
        namespace="lookup_account",
    )

    context_wrapper.approve_tool(explicit_namespaced_item, always_approve=True)

    assert context_wrapper.is_tool_approved("lookup_account", "call-bare-2") is None
    assert (
        context_wrapper.get_approval_status(
            "lookup_account",
            "call-namespaced-2",
            tool_namespace="lookup_account",
            existing_pending=explicit_namespaced_item,
        )
        is True
    )
