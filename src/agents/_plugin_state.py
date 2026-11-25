"""Local identity pins and portable bindings for SDK plugin approvals."""

from __future__ import annotations

import copy
import hashlib
from dataclasses import dataclass, field, fields
from typing import TYPE_CHECKING, Any

from ._tool_identity import (
    FunctionToolLookupKey,
    get_function_tool_lookup_key_for_call,
    get_function_tool_lookup_key_for_tool,
    get_mapping_or_attr,
    get_tool_approval_item_call_id,
    serialize_function_tool_lookup_key,
)
from .exceptions import UserError

if TYPE_CHECKING:
    from .agent import Agent
    from .items import ToolApprovalItem
    from .plugins import Plugin
    from .run_context import RunContextWrapper, TContext
    from .run_state import RunState
    from .tool import FunctionTool


@dataclass(frozen=True)
class PluginCallState:
    agent: Agent[Any]
    plugins: tuple[Plugin[Any], ...]
    tool: FunctionTool
    invoker: object
    digest: str
    key: FunctionToolLookupKey | None = field(kw_only=True)
    local: bool = field(default=True, kw_only=True)
    required: bool = False
    approved: bool = False
    proposal: object = field(default_factory=object, kw_only=True, repr=False)


def bind_plugins(
    agent: Agent[Any],
    context: RunContextWrapper[Any],
    revision: str | None,
    *,
    fresh: bool = False,
) -> None:
    """Pin one selection per public agent, without installing any tool or grant."""
    if fresh or (agent.plugins and not context._plugin_selections):
        from .run_context import RunContextWrapper

        previous = context._tool_invocations
        RunContextWrapper(context=None)._share_tool_state_with(context)
        context._plugin_calls = {}
        if not fresh:
            # A handoff must not replay earlier effects, even if it filters out their history.
            context._tool_invocations.update(
                (key, value) for key, value in previous.items() if value.executed or value.completed
            )
    if not agent.plugins and not context._plugin_selections:
        return
    from ._public_agent import get_public_agent

    agent = get_public_agent(agent)
    if context._plugin_selections and revision != context._plugin_revision:
        raise UserError("The plugin revision changed during the run.")
    for owner, selection in context._plugin_selections:
        if owner is agent:
            if tuple(map(id, selection)) != tuple(map(id, agent.plugins)):
                raise UserError("The plugin selection changed during the run.")
            return
    if agent.plugins:
        agent._tools_with_plugins()
        context._plugin_selections += ((agent, tuple(agent.plugins)),)
        context._plugin_revision = revision


def new_plugin_context(
    agent: Agent[Any], context: RunContextWrapper[TContext], revision: str | None
) -> RunContextWrapper[TContext]:
    if not agent.plugins and not context._plugin_selections:
        # A dynamic handoff may select plugins. Detaching then must leave the caller's
        # grants intact. Until that handoff, ordinary tool state remains shared.
        return copy.copy(context) if agent.handoffs else context
    context = copy.copy(context)
    context._plugin_selections = ()
    context._plugin_revision = None
    bind_plugins(agent, context, revision, fresh=True)
    return context


def _matches(record: PluginCallState, call: Any) -> bool:
    args = get_mapping_or_attr(call, "arguments")
    return (
        record.key is not None
        and get_function_tool_lookup_key_for_call(call)
        == record.key
        == get_function_tool_lookup_key_for_tool(record.tool)
        and isinstance(args, str)
        and hashlib.sha256(args.encode()).hexdigest() == record.digest
    )


def issue_plugin_proposal(context: RunContextWrapper[Any], item: ToolApprovalItem) -> None:
    """Identify a locally issued interruption. Restore issues a new in-memory token."""
    call_id = get_tool_approval_item_call_id(item)
    record = context._plugin_calls.get(call_id) if call_id is not None else None
    if record is not None and record.agent is item.agent:
        item.__dict__["_plugin_proposal"] = record.proposal


def check_plugin_proposal(
    context: RunContextWrapper[Any], item: ToolApprovalItem
) -> PluginCallState | None:
    call_id = get_tool_approval_item_call_id(item)
    record = context._plugin_calls.get(call_id) if call_id is not None else None
    token = getattr(item, "_plugin_proposal", None)
    if record is None and token is None:
        from .models._codex_items import native_approval_checkpoint

        # A native execution receipt can carry a function key without an SDK function.
        if native_approval_checkpoint(item.raw_item) is not None:
            return None
        # A fabricated call ID must not install a standing grant for a pinned agent.
        if context._resolve_tool_lookup_key(item) is None or not any(
            agent is item.agent for agent, _ in context._plugin_selections
        ):
            return None
    invocation = context._tool_invocations.get(call_id) if call_id is not None else None
    if (
        record is None
        or (invocation is not None and invocation.completed)
        or token is not record.proposal
        or item.agent is not record.agent
        or item.tool_lookup_key != record.key
        or item.tool_name != record.tool.name
        or not _matches(record, item.raw_item)
    ):
        raise UserError("Choose the current plugin approval item for this call.")
    return record


def validate_plugin_decision(
    context: RunContextWrapper[Any] | None,
    proposal: ToolApprovalItem,
    current: ToolApprovalItem | None,
) -> None:
    """Check the caller's item before substituting the canonical pending item."""
    if context is None or (
        not context._plugin_selections and getattr(proposal, "_plugin_proposal", None) is None
    ):
        return
    record = check_plugin_proposal(context, proposal)
    if record is None:
        return
    if current is None or check_plugin_proposal(context, current) is not record:
        raise UserError("Choose the current plugin approval item for this call.")


def validate_plugin_resume(
    state: RunState[Any, Any], starting: Agent[Any], revision: str | None
) -> None:
    context, current = state._context, state._current_agent
    if context is None or current is None:
        return
    if not (context._plugin_selections or starting.plugins or current.plugins):
        return
    if starting is not state._starting_agent or revision != context._plugin_revision:
        raise UserError("Resume with the original agent and plugin revision.")
    for agent in (starting, current):
        if agent.plugins and all(a is not agent for a, _ in context._plugin_selections):
            raise UserError("A plugin selection cannot be added to a pending run.")
    for agent, _ in context._plugin_selections:
        bind_plugins(agent, context, revision)
    if not current.plugins or state._last_processed_response is None:
        return
    local = {id(tool) for tool in current._tools_with_plugins()}
    for action in state._last_processed_response.functions:
        record = context._plugin_calls.get(action.tool_call.call_id)
        if (
            record is None
            or record.agent is not current
            or record.tool is not action.function_tool
            or id(record.tool) not in local
            or record.invoker is not record.tool.on_invoke_tool
            or not _matches(record, action.tool_call)
        ):
            raise UserError("Pending plugin call no longer matches the reviewed tool.")


def _selection(agent: Agent[Any]) -> list[dict[str, Any]]:
    return [
        {
            "name": plugin.name,
            "hooks": [
                f.name for f in fields(plugin.hooks) if getattr(plugin.hooks, f.name) is not None
            ]
            if plugin.hooks is not None
            else [],
            "tools": [
                serialize_function_tool_lookup_key(get_function_tool_lookup_key_for_tool(t))
                for t in plugin.tools
            ],
        }
        for plugin in agent.plugins
    ]


def _call_data(record: PluginCallState) -> dict[str, Any]:
    return {
        "key": serialize_function_tool_lookup_key(
            get_function_tool_lookup_key_for_tool(record.tool)
        ),
        "owner": next(
            (p.name for p in record.plugins if any(t is record.tool for t in p.tools)), None
        ),
        "digest": record.digest,
        "required": record.required,
        "approved": record.approved,
    }


def require_plugin_revision(context: RunContextWrapper[Any] | None) -> None:
    if context is not None and context._plugin_selections:
        revision = context._plugin_revision
        if not isinstance(revision, str) or not revision.strip():
            raise UserError("Supply RunConfig.plugin_revision before saving a plugin run.")


def save_plugin_state(
    state: RunState[Any, Any], *, for_resume: bool = False
) -> dict[str, Any] | None:
    context, agent = state._context, state._current_agent
    assert context is not None and agent is not None
    if for_resume:
        if not context._plugin_selections:
            return None
        # Include the pinned bindings in the redaction check. This private shape
        # cannot be restored as a durable, single-agent plugin checkpoint.
        return {
            "revision": context._plugin_revision,
            "selections": [
                {"agent": owner.name, "selection": _selection(owner)}
                for owner, _ in context._plugin_selections
            ],
            "calls": {key: _call_data(record) for key, record in context._plugin_calls.items()},
        }
    # Nested interruptions bubble to the parent. Refuse here before a best-effort
    # child serialization could discard an unsupported grandchild checkpoint.
    for pending in getattr(state._current_step, "interruptions", ()):
        if pending.agent is not agent and getattr(pending.agent, "plugins", None):
            raise UserError("Resume nested plugin approvals in memory.")
    if not context._plugin_selections and not agent.plugins:
        return None
    revision = context._plugin_revision
    if state._starting_agent is not agent or len(context._plugin_selections) != 1:
        raise UserError("Plugin checkpoints require the starting agent. Resume in memory.")
    validate_plugin_resume(state, agent, revision)
    actions = state._last_processed_response.functions if state._last_processed_response else ()
    return {
        "revision": revision,
        "agent": agent.name,
        "selection": _selection(agent),
        "context_required": context.context is not None,
        "calls": {
            action.tool_call.call_id: _call_data(context._plugin_calls[action.tool_call.call_id])
            for action in actions
        },
    }


def restore_plugin_calls(
    initial: Agent[Any],
    agent: Agent[Any],
    snapshot: dict[str, Any],
    revision: str | None,
    has_context: bool,
    plugin_schema: bool,
) -> dict[str, PluginCallState]:
    """Compare with trusted local definitions before any enablement or discovery callbacks."""
    if not plugin_schema:
        if "plugin_state" in snapshot or initial.plugins or agent.plugins or revision is not None:
            raise UserError("A plugin binding requires a plugin checkpoint.")
        return {}

    data = snapshot.get("plugin_state")
    if (
        not agent.plugins
        or not isinstance(data, dict)
        or initial is not agent
        or agent.name != data.get("agent")
        or not isinstance(revision, str)
        or not revision.strip()
        or revision != data.get("revision")
        or _selection(agent) != data.get("selection")
        or type(data.get("context_required")) is not bool
        or (data["context_required"] and not has_context)
        or snapshot["context"].get("context") is not None
    ):
        raise UserError("Restore with the original plugins, trusted revision and fresh context.")

    from .tool import FunctionTool

    tools = [t for t in agent._tools_with_plugins() if isinstance(t, FunctionTool)]
    actions = (snapshot.get("last_processed_response") or {}).get("functions", [])
    records: dict[str, PluginCallState] = {}
    entries = data.get("calls")
    if not isinstance(entries, dict) or len(entries) != len(actions):
        raise UserError("Checkpoint plugin calls do not match the pending actions.")
    for action in actions:
        call = action["tool_call"]
        call_id = call["call_id"]
        entry = entries.get(call_id)
        key = get_function_tool_lookup_key_for_call(call)
        matches = [t for t in tools if get_function_tool_lookup_key_for_tool(t) == key]
        if (
            not isinstance(call_id, str)
            or not call_id
            or call_id in records
            or not isinstance(entry, dict)
            or len(matches) != 1
            or "agent_run_state" in action
            or type(entry.get("required")) is not bool
            or type(entry.get("approved")) is not bool
            or (entry["approved"] and not entry["required"])
            or action["tool"].get("lookupKey") != entry.get("key")
            or action["tool"].get("paramsJsonSchema") != matches[0].params_json_schema
        ):
            raise UserError("Invalid checkpoint plugin call.")
        tool = matches[0]
        record = PluginCallState(
            agent,
            tuple(agent.plugins),
            tool,
            tool.on_invoke_tool,
            entry["digest"],
            entry["required"],
            entry["approved"],
            key=key,
        )
        if not _matches(record, call) or _call_data(record) != entry:
            raise UserError("Plugin checkpoint target or arguments changed.")
        records[call_id] = record
    return records
