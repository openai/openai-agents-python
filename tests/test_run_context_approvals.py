from __future__ import annotations

from agents import Agent, RunContextWrapper

from .utils.factories import make_tool_approval_item


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

    assert (
        context_wrapper.get_approval_status(
            "get_weather",
            "call-weather-2",
            tool_namespace="get_weather",
            existing_pending=deferred_item,
        )
        is True
    )


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
