from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Generic

from typing_extensions import TypeVar

from ._tool_identity import (
    FunctionToolLookupKey,
    HostedMCPApprovalKey,
    HostedMCPApprovalRequestIdentity,
    get_function_tool_approval_keys,
    get_function_tool_lookup_key,
    get_hosted_mcp_approval_request_identity,
    is_reserved_synthetic_tool_namespace,
    tool_qualified_name,
)
from ._tool_invocation import (
    is_mcp_approval_invocation,
    is_tool_invocation_digest,
    is_tool_invocation_type,
    tool_invocation_approval_scope,
    tool_invocation_call_id,
    tool_invocation_identity,
    tool_invocation_identity_and_scope,
    tool_output_identity,
)
from .exceptions import ModelBehaviorError, UserError
from .usage import Usage

if TYPE_CHECKING:
    from .items import ToolApprovalItem, TResponseInputItem
else:
    # Keep runtime annotations resolvable for TypeAdapter users (e.g., Temporal's
    # Pydantic data converter) without importing items.py and introducing cycles.
    ToolApprovalItem = Any
    TResponseInputItem = Any

TContext = TypeVar("TContext", default=Any)


@dataclass(eq=False)
class _ToolInvocationRecord:
    """Tracks the canonical identity and lifecycle of one provider tool call ID."""

    invocation_type: str
    approval_scope: str
    fingerprint: str
    executed: bool = False
    completed: bool = False


@dataclass(eq=False)
class _ApprovalRecord:
    """Tracks approval/rejection state for a tool.

    ``approved`` and ``rejected`` are either booleans (permanent allow/deny)
    or lists of call IDs when approval is scoped to specific tool calls.
    """

    approved: bool | list[str] = field(default_factory=list)
    rejected: bool | list[str] = field(default_factory=list)
    rejection_messages: dict[str, str] = field(default_factory=dict)
    sticky_rejection_message: str | None = None
    sticky_scope: str | None = None


@dataclass(eq=False)
class RunContextWrapper(Generic[TContext]):
    """This wraps the context object that you passed to `Runner.run()`. It also contains
    information about the usage of the agent run so far.

    NOTE: Contexts are not passed to the LLM. They're a way to pass dependencies and data to code
    you implement, like tool functions, callbacks, hooks, etc.
    """

    context: TContext
    """The context object (or None), passed by you to `Runner.run()`"""

    usage: Usage = field(default_factory=Usage)
    """The usage of the agent run so far. For streamed responses, the usage will be stale until the
    last chunk of the stream is processed.
    """

    turn_input: list[TResponseInputItem] = field(default_factory=list)
    _approvals: dict[str | HostedMCPApprovalKey, _ApprovalRecord] = field(default_factory=dict)
    _tool_invocations: dict[str, _ToolInvocationRecord] = field(
        default_factory=dict,
        init=False,
        repr=False,
    )
    tool_input: Any | None = None
    """Structured input for the current agent tool run, when available."""
    _allow_legacy_approval_binding_reconstruction: bool = field(
        default=False,
        init=False,
        repr=False,
    )
    _restored_unbound_approval_call_ids: set[str] = field(
        default_factory=set,
        init=False,
        repr=False,
    )

    def _share_tool_state_with(self, target: RunContextWrapper[Any]) -> None:
        """Share tool approval and invocation state with a derived context wrapper."""
        target._approvals = self._approvals
        target._tool_invocations = self._tool_invocations
        target._allow_legacy_approval_binding_reconstruction = (
            self._allow_legacy_approval_binding_reconstruction
        )
        target._restored_unbound_approval_call_ids = self._restored_unbound_approval_call_ids

    @staticmethod
    def _to_str_or_none(value: Any) -> str | None:
        if isinstance(value, str):
            return value
        if value is not None:
            try:
                return str(value)
            except Exception:
                return None
        return None

    @staticmethod
    def _resolve_tool_name(approval_item: ToolApprovalItem) -> str:
        raw = approval_item.raw_item
        if approval_item.tool_name:
            return approval_item.tool_name
        candidate: Any | None
        if isinstance(raw, dict):
            candidate = raw.get("name") or raw.get("type")
        else:
            candidate = getattr(raw, "name", None) or getattr(raw, "type", None)
        return RunContextWrapper._to_str_or_none(candidate) or "unknown_tool"

    @staticmethod
    def _resolve_tool_namespace(approval_item: ToolApprovalItem) -> str | None:
        raw = approval_item.raw_item
        if isinstance(approval_item.tool_namespace, str) and approval_item.tool_namespace:
            return approval_item.tool_namespace
        if isinstance(raw, dict):
            candidate = raw.get("namespace")
        else:
            candidate = getattr(raw, "namespace", None)
        return RunContextWrapper._to_str_or_none(candidate)

    @staticmethod
    def _resolve_approval_key(approval_item: ToolApprovalItem) -> str:
        tool_name = RunContextWrapper._resolve_tool_name(approval_item)
        tool_namespace = RunContextWrapper._resolve_tool_namespace(approval_item)
        lookup_key = RunContextWrapper._resolve_tool_lookup_key(approval_item)
        approval_keys = get_function_tool_approval_keys(
            tool_name=tool_name,
            tool_namespace=tool_namespace,
            tool_lookup_key=lookup_key,
            prefer_legacy_same_name_namespace=lookup_key is None,
        )
        if approval_keys:
            return approval_keys[-1]
        return tool_qualified_name(tool_name, tool_namespace) or tool_name or "unknown_tool"

    @staticmethod
    def _resolve_approval_keys(approval_item: ToolApprovalItem) -> tuple[str, ...]:
        """Return all approval keys that should mirror this approval record."""
        lookup_key = RunContextWrapper._resolve_tool_lookup_key(approval_item)
        return get_function_tool_approval_keys(
            tool_name=RunContextWrapper._resolve_tool_name(approval_item),
            tool_namespace=RunContextWrapper._resolve_tool_namespace(approval_item),
            allow_bare_name_alias=getattr(approval_item, "_allow_bare_name_alias", False),
            tool_lookup_key=lookup_key,
            prefer_legacy_same_name_namespace=lookup_key is None,
        )

    @staticmethod
    def _resolve_tool_lookup_key(approval_item: ToolApprovalItem) -> FunctionToolLookupKey | None:
        candidate = getattr(approval_item, "tool_lookup_key", None)
        if isinstance(candidate, tuple):
            return candidate

        raw = approval_item.raw_item
        if isinstance(raw, dict):
            raw_type = raw.get("type")
        else:
            raw_type = getattr(raw, "type", None)
        if raw_type != "function_call":
            return None

        tool_name = RunContextWrapper._resolve_tool_name(approval_item)
        tool_namespace = RunContextWrapper._resolve_tool_namespace(approval_item)
        if is_reserved_synthetic_tool_namespace(tool_name, tool_namespace):
            return None
        return get_function_tool_lookup_key(tool_name, tool_namespace)

    @staticmethod
    def _resolve_call_id(approval_item: ToolApprovalItem) -> str | None:
        hosted_request = get_hosted_mcp_approval_request_identity(approval_item)
        if hosted_request is not None:
            return hosted_request.request_id

        raw = approval_item.raw_item
        if isinstance(raw, dict):
            raw_type = raw.get("type")
            provider_data = raw.get("provider_data")
            if (
                isinstance(provider_data, dict)
                and provider_data.get("type") == "mcp_approval_request"
            ):
                candidate = provider_data.get("id")
                if isinstance(candidate, str):
                    return candidate
            candidate = raw.get("id") if raw_type == "mcp_approval_request" else raw.get("call_id")
            if candidate is None and raw_type is None:
                candidate = raw.get("id")
        else:
            raw_type = getattr(raw, "type", None)
            provider_data = getattr(raw, "provider_data", None)
            if (
                isinstance(provider_data, dict)
                and provider_data.get("type") == "mcp_approval_request"
            ):
                candidate = provider_data.get("id")
                if isinstance(candidate, str):
                    return candidate
            candidate = (
                getattr(raw, "id", None)
                if raw_type == "mcp_approval_request"
                else getattr(raw, "call_id", None)
            )
            if candidate is None and raw_type is None:
                candidate = getattr(raw, "id", None)
        return RunContextWrapper._to_str_or_none(candidate)

    def _get_or_create_approval_entry(
        self,
        approval_key: str | HostedMCPApprovalKey,
    ) -> _ApprovalRecord:
        approval_entry = self._approvals.get(approval_key)
        if approval_entry is None:
            approval_entry = _ApprovalRecord()
            self._approvals[approval_key] = approval_entry
        return approval_entry

    def _approved_tool_invocation_status(
        self,
        raw_item: Any,
        *,
        tool_lookup_key: FunctionToolLookupKey | None = None,
        tool_name: str | None = None,
        invocation_role: str | None = None,
    ) -> tuple[tuple[str, str], bool, bool] | None:
        """Validate an invocation and return status when an approval decision applies."""
        status = self._tool_invocation_status(
            raw_item,
            tool_lookup_key=tool_lookup_key,
            tool_name=tool_name,
            invocation_role=invocation_role,
        )
        if status is None:
            return None
        identity = tool_invocation_identity_and_scope(
            raw_item,
            tool_lookup_key=tool_lookup_key,
            tool_name=tool_name,
            invocation_role=invocation_role,
        )
        if identity is None:
            return None
        _, call_id, approval_scope, _ = identity
        sticky_approval_keys = self._matching_sticky_approval_keys(
            raw_item,
            tool_lookup_key=tool_lookup_key,
            tool_name=tool_name,
            approval_scope=approval_scope,
        )
        has_per_call_decision = any(
            (isinstance(record.approved, list) and call_id in record.approved)
            or (isinstance(record.rejected, list) and call_id in record.rejected)
            for record in self._approvals.values()
        )
        if not has_per_call_decision and not sticky_approval_keys:
            return None
        return status

    def _tool_invocation_status(
        self,
        raw_item: Any,
        *,
        tool_lookup_key: FunctionToolLookupKey | None = None,
        tool_name: str | None = None,
        invocation_role: str | None = None,
    ) -> tuple[tuple[str, str], bool, bool] | None:
        """Validate and register one canonical invocation for a provider call ID."""
        call_identity = tool_invocation_call_id(raw_item)
        call_id = call_identity[1] if call_identity is not None else None
        is_restored_unbound = (
            call_id is not None and call_id in self._restored_unbound_approval_call_ids
        )
        identity = tool_invocation_identity_and_scope(
            raw_item,
            tool_lookup_key=tool_lookup_key,
            tool_name=tool_name,
            invocation_role=invocation_role,
        )
        if identity is None:
            if is_mcp_approval_invocation(raw_item):
                return None
            if call_id is not None and call_id in self._tool_invocations:
                raise ModelBehaviorError(
                    "Model reused a tool call ID for a different invocation. "
                    "Use a unique call ID for each tool invocation."
                )
            return None
        invocation_type, call_id, approval_scope, fingerprint = identity
        record = self._tool_invocations.get(call_id)
        if record is None:
            if is_restored_unbound:
                return None
            record = _ToolInvocationRecord(
                invocation_type=invocation_type,
                approval_scope=approval_scope,
                fingerprint=fingerprint,
            )
            self._tool_invocations[call_id] = record
        elif (
            record.invocation_type != invocation_type
            or record.approval_scope != approval_scope
            or record.fingerprint != fingerprint
        ):
            raise ModelBehaviorError(
                "Model reused a tool call ID for a different invocation. "
                "Use a unique call ID for each tool invocation."
            )
        if is_restored_unbound:
            return None
        return ((invocation_type, call_id), record.completed, record.executed)

    def _rebind_tool_invocation(
        self,
        raw_item: Any,
        *,
        previous_identity: tuple[str, str, str],
        tool_lookup_key: FunctionToolLookupKey | None = None,
        tool_name: str | None = None,
        invocation_role: str | None = None,
    ) -> tuple[tuple[str, str], bool, bool] | None:
        """Replace an unresolved invocation identity before execution begins."""
        identity = tool_invocation_identity_and_scope(
            raw_item,
            tool_lookup_key=tool_lookup_key,
            tool_name=tool_name,
            invocation_role=invocation_role,
        )
        if identity is None:
            return None
        invocation_type, call_id, approval_scope, fingerprint = identity
        record = self._tool_invocations.get(call_id)
        resolved_identity = (invocation_type, approval_scope, fingerprint)
        if record is None:
            return self._tool_invocation_status(
                raw_item,
                tool_lookup_key=tool_lookup_key,
                tool_name=tool_name,
                invocation_role=invocation_role,
            )
        current_identity = (
            record.invocation_type,
            record.approval_scope,
            record.fingerprint,
        )
        if current_identity == resolved_identity:
            return ((invocation_type, call_id), record.completed, record.executed)
        matches_previous = (
            record.invocation_type == previous_identity[0]
            and call_id == previous_identity[1]
            and record.fingerprint == previous_identity[2]
        )
        if not matches_previous or record.executed or record.completed:
            raise ModelBehaviorError(
                "Model reused a tool call ID for a different invocation. "
                "Use a unique call ID for each tool invocation."
            )
        record.invocation_type = invocation_type
        record.approval_scope = approval_scope
        record.fingerprint = fingerprint
        return ((invocation_type, call_id), False, False)

    def _matching_sticky_approval_keys(
        self,
        raw_item: Any,
        *,
        tool_lookup_key: FunctionToolLookupKey | None,
        tool_name: str | None = None,
        approval_scope: str,
    ) -> frozenset[str | HostedMCPApprovalKey]:
        """Return sticky approval keys that independently authorize this tool identity."""
        if isinstance(raw_item, Mapping):
            mapping = raw_item
        else:
            model_dump = getattr(raw_item, "model_dump", None)
            dumped = (
                model_dump(exclude_none=True, exclude_unset=True) if callable(model_dump) else None
            )
            mapping = dumped if isinstance(dumped, Mapping) else {}
        provider_data = mapping.get("provider_data")
        if (
            mapping.get("type") == "hosted_tool_call"
            and isinstance(provider_data, Mapping)
            and provider_data.get("type") == "mcp_approval_request"
        ):
            merged = dict(mapping)
            merged.update(provider_data)
            mapping = merged

        invocation_type = mapping.get("type")
        if not isinstance(invocation_type, str):
            return frozenset()
        tool_name = tool_name or self._to_str_or_none(mapping.get("name"))
        tool_namespace = self._to_str_or_none(mapping.get("namespace"))
        if invocation_type == "function_call":
            approval_keys: tuple[str | HostedMCPApprovalKey, ...] = get_function_tool_approval_keys(
                tool_name=tool_name,
                tool_namespace=tool_namespace,
                tool_lookup_key=tool_lookup_key,
                include_legacy_deferred_key=True,
            )
        elif invocation_type == "mcp_approval_request":
            server_label = self._to_str_or_none(mapping.get("server_label"))
            approval_keys = (
                (("hosted_mcp", server_label, tool_name),)
                if server_label is not None and tool_name is not None
                else ()
            )
        else:
            if tool_name is None:
                tool_name = {
                    "apply_patch_call": "apply_patch",
                    "computer_call": "computer",
                    "local_shell_call": "local_shell",
                    "shell_call": "shell",
                }.get(invocation_type)
            approval_keys = (tool_name,) if tool_name else ()

        matching_keys: set[str | HostedMCPApprovalKey] = set()
        for approval_key in approval_keys:
            record = self._approvals.get(approval_key)
            if (
                record is not None
                and (isinstance(record.approved, bool) or isinstance(record.rejected, bool))
                and record.sticky_scope == approval_scope
            ):
                matching_keys.add(approval_key)
        return frozenset(matching_keys)

    def _mark_tool_call_completed(
        self,
        raw_item: Any,
    ) -> None:
        """Mark a canonical invocation completed when its output is committed."""
        identity = tool_output_identity(raw_item)
        if identity is None:
            return
        invocation_type, call_id = identity
        record = self._tool_invocations.get(call_id)
        if record is None or record.invocation_type != invocation_type:
            return
        record.executed = True
        record.completed = True

    def _mark_tool_invocation_executed(
        self,
        raw_item: Any,
        *,
        tool_lookup_key: FunctionToolLookupKey | None = None,
        tool_name: str | None = None,
        invocation_role: str | None = None,
    ) -> None:
        """Mark an invocation executed before the first user-code side effect."""
        status = self._tool_invocation_status(
            raw_item,
            tool_lookup_key=tool_lookup_key,
            tool_name=tool_name,
            invocation_role=invocation_role,
        )
        if status is None:
            return
        _, call_id = status[0]
        self._tool_invocations[call_id].executed = True

    def _restore_pending_approval_binding(self, approval_item: ToolApprovalItem) -> None:
        """Rebuild a missing binding from a serialized pending approval item."""
        if not self._allow_legacy_approval_binding_reconstruction:
            return
        approval_keys: list[str | HostedMCPApprovalKey] = list(
            self._resolve_approval_keys(approval_item)
        )
        hosted_request = get_hosted_mcp_approval_request_identity(approval_item)
        if hosted_request is not None and hosted_request.request_id is not None:
            hosted_key: HostedMCPApprovalKey = (
                hosted_request.approval_identity
                if hosted_request.approval_identity is not None
                else ("hosted_mcp_call", hosted_request.request_id)
            )
            approval_keys.append(hosted_key)
        scope_identity = tool_invocation_approval_scope(
            approval_item.raw_item,
            tool_lookup_key=approval_item.tool_lookup_key,
            tool_name=approval_item.tool_name,
        )
        if scope_identity is not None:
            _, approval_scope = scope_identity
            for approval_key in approval_keys:
                record = self._approvals.get(approval_key)
                if record is not None and (
                    isinstance(record.approved, bool) or isinstance(record.rejected, bool)
                ):
                    record.sticky_scope = record.sticky_scope or approval_scope
        call_id = self._resolve_call_id(approval_item)
        if call_id is None:
            return
        identity = tool_invocation_identity_and_scope(
            approval_item.raw_item,
            tool_lookup_key=approval_item.tool_lookup_key,
            tool_name=approval_item.tool_name,
        )
        if identity is None:
            self._restored_unbound_approval_call_ids.add(call_id)
            return
        has_matching_decision = False
        for approval_key in approval_keys:
            record = self._approvals.get(approval_key)
            if record is None:
                continue
            has_per_call_decision = (
                isinstance(record.approved, list) and call_id in record.approved
            ) or (isinstance(record.rejected, list) and call_id in record.rejected)
            has_sticky_decision = (
                record.sticky_scope == approval_scope
                and self._get_approval_status_for_record(record, call_id) is not None
            )
            has_matching_decision = (
                has_matching_decision or has_per_call_decision or has_sticky_decision
            )
        if has_matching_decision:
            self._tool_invocation_status(
                approval_item.raw_item,
                tool_lookup_key=approval_item.tool_lookup_key,
                tool_name=approval_item.tool_name,
            )

    def _mark_restored_unbound_pending_approval(
        self,
        approval_item: ToolApprovalItem,
    ) -> None:
        """Remember a current-schema pending call whose sticky binding was not restored."""
        if self._allow_legacy_approval_binding_reconstruction:
            return
        call_id = self._resolve_call_id(approval_item)
        if call_id is None:
            return
        identity = tool_invocation_identity_and_scope(
            approval_item.raw_item,
            tool_lookup_key=approval_item.tool_lookup_key,
            tool_name=approval_item.tool_name,
        )
        if identity is None:
            self._restored_unbound_approval_call_ids.add(call_id)
            return
        invocation_type, identity_call_id, approval_scope, fingerprint = identity
        if identity_call_id != call_id:
            self._restored_unbound_approval_call_ids.add(call_id)
            return
        sticky_keys = self._matching_sticky_approval_keys(
            approval_item.raw_item,
            tool_lookup_key=approval_item.tool_lookup_key,
            tool_name=approval_item.tool_name,
            approval_scope=approval_scope,
        )
        has_per_call_decision = any(
            (isinstance(record.approved, list) and call_id in record.approved)
            or (isinstance(record.rejected, list) and call_id in record.rejected)
            for record in self._approvals.values()
        )
        if sticky_keys or has_per_call_decision:
            restored_binding = self._tool_invocations.get(call_id)
            if restored_binding is None or (
                restored_binding.invocation_type != invocation_type
                or restored_binding.approval_scope != approval_scope
                or restored_binding.fingerprint != fingerprint
            ):
                self._restored_unbound_approval_call_ids.add(call_id)

    def is_tool_approved(self, tool_name: str, call_id: str) -> bool | None:
        """Return True/False/None for the given tool call."""
        hosted_query_record = self._approvals.get(("hosted_mcp_query", tool_name, call_id))
        hosted_query_status = self._get_per_call_approval_status_for_record(
            hosted_query_record,
            call_id,
        )
        if hosted_query_status is not None:
            return hosted_query_status
        return self._get_approval_status_for_key(tool_name, call_id)

    def _get_approval_status_for_key(self, approval_key: str, call_id: str) -> bool | None:
        """Return True/False/None for a concrete approval key and tool call."""
        approval_entry = self._approvals.get(approval_key)
        return self._get_approval_status_for_record(approval_entry, call_id)

    @staticmethod
    def _get_approval_status_for_record(
        approval_entry: _ApprovalRecord | None,
        call_id: str,
    ) -> bool | None:
        """Return True/False/None for an approval record and tool call."""
        if approval_entry is None:
            return None

        # Check for permanent approval/rejection
        if approval_entry.approved is True and approval_entry.rejected is True:
            # Approval takes precedence
            return True

        if approval_entry.approved is True:
            return True

        if approval_entry.rejected is True:
            return False

        approved_ids = (
            set(approval_entry.approved) if isinstance(approval_entry.approved, list) else set()
        )
        rejected_ids = (
            set(approval_entry.rejected) if isinstance(approval_entry.rejected, list) else set()
        )

        if call_id in approved_ids:
            return True
        if call_id in rejected_ids:
            return False
        # Per-call approvals are scoped to the exact call ID, so other calls require a new decision.
        return None

    def _get_per_call_approval_status_for_key(
        self,
        approval_key: str,
        call_id: str,
    ) -> bool | None:
        """Return only exact-call decisions, ignoring sticky values on the same key."""
        approval_entry = self._approvals.get(approval_key)
        return self._get_per_call_approval_status_for_record(approval_entry, call_id)

    @staticmethod
    def _get_per_call_approval_status_for_record(
        approval_entry: _ApprovalRecord | None,
        call_id: str,
    ) -> bool | None:
        """Return only an exact-call decision from an approval record."""
        if approval_entry is None:
            return None
        if isinstance(approval_entry.approved, list) and call_id in approval_entry.approved:
            return True
        if isinstance(approval_entry.rejected, list) and call_id in approval_entry.rejected:
            return False
        return None

    @staticmethod
    def _clear_rejection_message(record: _ApprovalRecord, call_id: str | None) -> None:
        if call_id is None:
            return
        record.rejection_messages.pop(call_id, None)

    @staticmethod
    def _get_rejection_message_for_key(record: _ApprovalRecord, call_id: str) -> str | None:
        if record.rejected is True:
            if call_id in record.rejection_messages:
                return record.rejection_messages[call_id]
            return record.sticky_rejection_message
        if isinstance(record.rejected, list) and call_id in record.rejected:
            return record.rejection_messages.get(call_id)
        return None

    @staticmethod
    def _restore_approval_value(value: Any) -> bool | list[str]:
        if isinstance(value, bool):
            return value
        if isinstance(value, list):
            return [item for item in value if isinstance(item, str)]
        return []

    @staticmethod
    def _resolve_hosted_mcp_tool_name(
        approval_item: ToolApprovalItem,
        hosted_request: HostedMCPApprovalRequestIdentity,
    ) -> str | None:
        """Resolve a hosted MCP tool name, including persisted legacy item metadata."""
        if hosted_request.tool_name is not None:
            return hosted_request.tool_name
        persisted_tool_name = getattr(approval_item, "tool_name", None)
        if isinstance(persisted_tool_name, str) and persisted_tool_name:
            return persisted_tool_name
        return None

    def _resolve_hosted_mcp_approval_record(
        self,
        approval_item: ToolApprovalItem,
        *,
        allow_legacy_exact: bool,
    ) -> tuple[_ApprovalRecord | None, str | None, bool]:
        """Resolve the authoritative hosted MCP record and whether it is exact-call-only."""
        hosted_request = get_hosted_mcp_approval_request_identity(approval_item)
        if hosted_request is None or hosted_request.request_id is None:
            return None, None, True

        request_id = hosted_request.request_id
        hosted_identity = hosted_request.approval_identity
        if hosted_identity is not None:
            current_record = self._approvals.get(hosted_identity)
            current_status = self._get_approval_status_for_record(current_record, request_id)
            if current_status is not None:
                return current_record, request_id, False
        else:
            current_record = self._approvals.get(("hosted_mcp_call", request_id))
            current_status = self._get_per_call_approval_status_for_record(
                current_record,
                request_id,
            )
            if current_status is not None:
                return current_record, request_id, True

        if not allow_legacy_exact:
            return None, request_id, True

        legacy_key = self._resolve_hosted_mcp_tool_name(approval_item, hosted_request)
        if legacy_key is None:
            return None, request_id, True

        legacy_record = self._approvals.get(legacy_key)
        legacy_status = self._get_per_call_approval_status_for_record(legacy_record, request_id)
        if legacy_status is None:
            return None, request_id, True
        return legacy_record, request_id, True

    def _resolve_hosted_mcp_approval_decision(
        self,
        approval_item: ToolApprovalItem,
        *,
        allow_legacy_exact: bool = True,
    ) -> tuple[bool | None, str | None]:
        """Return a hosted MCP decision and its rejection message from one record."""
        approval_record, request_id, exact_call_only = self._resolve_hosted_mcp_approval_record(
            approval_item,
            allow_legacy_exact=allow_legacy_exact,
        )
        if approval_record is None or request_id is None:
            return None, None

        if exact_call_only:
            status = self._get_per_call_approval_status_for_record(approval_record, request_id)
        else:
            status = self._get_approval_status_for_record(approval_record, request_id)
            if (
                status is not None
                and approval_record.sticky_scope is None
                and self._allow_legacy_approval_binding_reconstruction
            ):
                scope_identity = tool_invocation_approval_scope(
                    approval_item.raw_item,
                    tool_lookup_key=approval_item.tool_lookup_key,
                    tool_name=approval_item.tool_name,
                )
                if scope_identity is not None:
                    approval_record.sticky_scope = scope_identity[1]
        return status, self._get_rejection_message_for_key(approval_record, request_id)

    def get_rejection_message(
        self,
        tool_name: str,
        call_id: str,
        *,
        tool_namespace: str | None = None,
        existing_pending: ToolApprovalItem | None = None,
        tool_lookup_key: FunctionToolLookupKey | None = None,
    ) -> str | None:
        """Return a stored rejection message for a tool call if one exists."""
        if existing_pending is not None:
            hosted_request = get_hosted_mcp_approval_request_identity(existing_pending)
            if hosted_request is not None:
                _, rejection_message = self._resolve_hosted_mcp_approval_decision(existing_pending)
                return rejection_message

        hosted_query_record = self._approvals.get(("hosted_mcp_query", tool_name, call_id))
        hosted_query_status = self._get_per_call_approval_status_for_record(
            hosted_query_record,
            call_id,
        )
        if hosted_query_status is not None:
            assert hosted_query_record is not None
            return self._get_rejection_message_for_key(hosted_query_record, call_id)

        candidates: list[str] = []
        explicit_namespace = (
            tool_namespace if isinstance(tool_namespace, str) and tool_namespace else None
        )
        pending_namespace = (
            self._resolve_tool_namespace(existing_pending) if existing_pending is not None else None
        )
        pending_key = (
            self._resolve_approval_key(existing_pending) if existing_pending is not None else None
        )
        pending_tool_name = (
            self._resolve_tool_name(existing_pending) if existing_pending is not None else None
        )
        pending_keys = (
            list(self._resolve_approval_keys(existing_pending))
            if existing_pending is not None
            else []
        )

        if existing_pending is not None and pending_key is not None:
            candidates.append(pending_key)
        explicit_keys = (
            list(
                get_function_tool_approval_keys(
                    tool_name=tool_name,
                    tool_namespace=explicit_namespace,
                    tool_lookup_key=tool_lookup_key,
                    include_legacy_deferred_key=True,
                )
            )
            if explicit_namespace is not None or tool_lookup_key is not None
            else []
        )
        for explicit_key in explicit_keys:
            if explicit_key not in candidates:
                candidates.append(explicit_key)
        if not explicit_keys and pending_namespace and pending_key is not None:
            if pending_key not in candidates:
                candidates.append(pending_key)
        if (
            explicit_namespace is None
            and tool_lookup_key is None
            and existing_pending is None
            and tool_name not in candidates
        ):
            candidates.append(tool_name)
        if existing_pending is not None:
            for pending_candidate in pending_keys:
                if pending_candidate not in candidates:
                    candidates.append(pending_candidate)
            if (
                pending_namespace is None
                and pending_tool_name is not None
                and pending_tool_name not in candidates
            ):
                candidates.append(pending_tool_name)

        for candidate in candidates:
            approval_entry = self._approvals.get(candidate)
            if not approval_entry:
                continue
            message = self._get_rejection_message_for_key(approval_entry, call_id)
            if message is not None:
                return message
        return None

    def _apply_approval_decision(
        self,
        approval_item: ToolApprovalItem,
        *,
        always: bool,
        approve: bool,
        rejection_message: str | None = None,
    ) -> None:
        """Record an approval or rejection decision."""
        hosted_request = get_hosted_mcp_approval_request_identity(approval_item)
        if hosted_request is not None:
            call_id = hosted_request.request_id
            if call_id is None:
                raise UserError("Hosted MCP approval decisions require a non-empty request id.")
            hosted_identity = hosted_request.approval_identity
            if always and hosted_identity is None:
                raise UserError(
                    "Persistent hosted MCP approval decisions require a non-empty server_label "
                    "and tool name."
                )
        else:
            call_id = self._resolve_call_id(approval_item)
            hosted_identity = None

        call_identity = tool_invocation_call_id(approval_item.raw_item)
        if call_identity is not None and call_identity[1] is None:
            raise ModelBehaviorError(
                "Approval decisions require a non-empty call ID for recognized tool invocations."
            )

        raw_item = approval_item.raw_item
        if isinstance(raw_item, Mapping):
            raw_call_id = raw_item.get("call_id") if "call_id" in raw_item else raw_item.get("id")
        else:
            raw_call_id = (
                getattr(raw_item, "call_id", None)
                if hasattr(raw_item, "call_id")
                else getattr(raw_item, "id", None)
            )
        if raw_call_id == "" and not always:
            raise ModelBehaviorError("Per-call approval decisions require a non-empty call ID.")
        invocation = (
            None
            if call_id is None
            else tool_invocation_identity_and_scope(
                approval_item.raw_item,
                tool_lookup_key=approval_item.tool_lookup_key,
                tool_name=approval_item.tool_name,
            )
        )
        if call_id is not None and invocation is None:
            raise ModelBehaviorError("Approval decisions require a canonical invocation identity.")
        if call_id is None and raw_call_id is not None:
            raise ModelBehaviorError("Approval decisions require a canonical invocation identity.")
        scope_identity = tool_invocation_approval_scope(
            approval_item.raw_item,
            tool_lookup_key=approval_item.tool_lookup_key,
            tool_name=approval_item.tool_name,
        )
        if invocation is not None:
            assert call_id is not None
            if invocation[1] != call_id:
                raise ModelBehaviorError(
                    "Approval decision call ID does not match its canonical invocation ID."
                )
            was_restored_unbound = call_id in self._restored_unbound_approval_call_ids
            if was_restored_unbound:
                self._restored_unbound_approval_call_ids.remove(call_id)
            try:
                self._tool_invocation_status(
                    approval_item.raw_item,
                    tool_lookup_key=approval_item.tool_lookup_key,
                    tool_name=approval_item.tool_name,
                )
            finally:
                if was_restored_unbound:
                    self._restored_unbound_approval_call_ids.add(call_id)
        approval_entries: tuple[tuple[_ApprovalRecord, bool], ...]
        if hosted_request is not None:
            approval_keys: tuple[str, ...] = ()
            assert call_id is not None
            hosted_key: HostedMCPApprovalKey
            if hosted_identity is None:
                hosted_key = ("hosted_mcp_call", call_id)
            else:
                hosted_key = hosted_identity
            approval_entries = ((self._get_or_create_approval_entry(hosted_key), always),)
            hosted_tool_name = self._resolve_hosted_mcp_tool_name(
                approval_item,
                hosted_request,
            )
            if hosted_tool_name is not None:
                # Preserve exact name-based lookup without adding an authorization source.
                approval_entries += (
                    (
                        self._get_or_create_approval_entry(
                            ("hosted_mcp_query", hosted_tool_name, call_id)
                        ),
                        False,
                    ),
                )
        else:
            approval_keys = self._resolve_approval_keys(approval_item) or ("unknown_tool",)
            exact_approval_key = self._resolve_approval_key(approval_item)
            decision_keys = (exact_approval_key,) if always or call_id is None else approval_keys
            approval_entries = tuple(
                (self._get_or_create_approval_entry(approval_key), always)
                for approval_key in decision_keys
            )

        for approval_entry, entry_is_sticky in approval_entries:
            if entry_is_sticky or call_id is None:
                approval_entry.sticky_scope = (
                    scope_identity[1] if scope_identity is not None else None
                )
                approval_entry.approved = approve
                approval_entry.rejected = [] if approve else True
                if not approve:
                    approval_entry.approved = False
                    if rejection_message is not None and call_id is not None:
                        approval_entry.rejection_messages[call_id] = rejection_message
                    elif call_id is not None:
                        self._clear_rejection_message(approval_entry, call_id)
                    approval_entry.sticky_rejection_message = rejection_message
                else:
                    approval_entry.rejection_messages.clear()
                    approval_entry.sticky_rejection_message = None
                continue

            opposite = approval_entry.rejected if approve else approval_entry.approved
            if isinstance(opposite, list) and call_id in opposite:
                opposite.remove(call_id)

            target = approval_entry.approved if approve else approval_entry.rejected
            if isinstance(target, list) and call_id not in target:
                target.append(call_id)
            if approve:
                self._clear_rejection_message(approval_entry, call_id)
            elif call_id is not None:
                if rejection_message is not None:
                    approval_entry.rejection_messages[call_id] = rejection_message
                else:
                    self._clear_rejection_message(approval_entry, call_id)

        if invocation is not None:
            assert call_id is not None
            self._restored_unbound_approval_call_ids.discard(call_id)

    def approve_tool(self, approval_item: ToolApprovalItem, always_approve: bool = False) -> None:
        """Approve a tool call, optionally for all future calls."""
        self._apply_approval_decision(
            approval_item,
            always=always_approve,
            approve=True,
        )

    def reject_tool(
        self,
        approval_item: ToolApprovalItem,
        always_reject: bool = False,
        rejection_message: str | None = None,
    ) -> None:
        """Reject a tool call, optionally for all future calls."""
        self._apply_approval_decision(
            approval_item,
            always=always_reject,
            approve=False,
            rejection_message=rejection_message,
        )

    def get_approval_status(
        self,
        tool_name: str,
        call_id: str,
        *,
        tool_namespace: str | None = None,
        existing_pending: ToolApprovalItem | None = None,
        tool_lookup_key: FunctionToolLookupKey | None = None,
        current_invocation: ToolApprovalItem | None = None,
    ) -> bool | None:
        """Return approval status, retrying with pending item's tool name if necessary."""
        if not isinstance(call_id, str) or not call_id:
            raise ModelBehaviorError("Approval-gated tool calls require a non-empty call ID.")
        if existing_pending is not None:
            self._restore_pending_approval_binding(existing_pending)
            pending_identity = tool_invocation_identity(
                existing_pending.raw_item,
                tool_lookup_key=existing_pending.tool_lookup_key,
                tool_name=existing_pending.tool_name,
            )
            if pending_identity is None:
                pending_call_id = self._resolve_call_id(existing_pending)
                if pending_call_id is not None and (
                    current_invocation is None or pending_call_id not in self._tool_invocations
                ):
                    self._restored_unbound_approval_call_ids.add(pending_call_id)
                if current_invocation is None:
                    return None
            hosted_request = get_hosted_mcp_approval_request_identity(existing_pending)
            if hosted_request is not None:
                hosted_status, _ = self._resolve_hosted_mcp_approval_decision(existing_pending)
                if hosted_status is None:
                    return None
                effective_invocation = (
                    current_invocation if current_invocation is not None else existing_pending
                )
                binding_status = self._approved_tool_invocation_status(
                    effective_invocation.raw_item,
                    tool_lookup_key=effective_invocation.tool_lookup_key,
                    tool_name=effective_invocation.tool_name,
                )
                return hosted_status if binding_status is not None else None

        candidates: list[str] = []
        explicit_namespace = (
            tool_namespace if isinstance(tool_namespace, str) and tool_namespace else None
        )
        pending_namespace = (
            self._resolve_tool_namespace(existing_pending) if existing_pending is not None else None
        )
        pending_key = (
            self._resolve_approval_key(existing_pending) if existing_pending is not None else None
        )
        pending_tool_name = (
            self._resolve_tool_name(existing_pending) if existing_pending is not None else None
        )
        pending_keys = (
            list(self._resolve_approval_keys(existing_pending))
            if existing_pending is not None
            else []
        )

        if existing_pending is not None and pending_key is not None:
            candidates.append(pending_key)
        explicit_keys = (
            list(
                get_function_tool_approval_keys(
                    tool_name=tool_name,
                    tool_namespace=explicit_namespace,
                    tool_lookup_key=tool_lookup_key,
                    include_legacy_deferred_key=True,
                )
            )
            if explicit_namespace is not None or tool_lookup_key is not None
            else []
        )
        for explicit_key in explicit_keys:
            if explicit_key not in candidates:
                candidates.append(explicit_key)
        if not explicit_keys and pending_namespace and pending_key is not None:
            if pending_key not in candidates:
                candidates.append(pending_key)
        if (
            explicit_namespace is None
            and tool_lookup_key is None
            and existing_pending is None
            and tool_name not in candidates
        ):
            candidates.append(tool_name)
        if existing_pending is not None:
            for pending_candidate in pending_keys:
                if pending_candidate not in candidates:
                    candidates.append(pending_candidate)
            if (
                pending_namespace is None
                and pending_tool_name is not None
                and pending_tool_name not in candidates
            ):
                candidates.append(pending_tool_name)

        status: bool | None = None
        matched_record: _ApprovalRecord | None = None
        for candidate in candidates:
            status = self._get_approval_status_for_key(candidate, call_id)
            if status is not None:
                matched_record = self._approvals.get(candidate)
                break
        selected_invocation = (
            current_invocation if current_invocation is not None else existing_pending
        )
        if status is None or matched_record is None or selected_invocation is None:
            return status
        is_sticky = isinstance(matched_record.approved, bool) or isinstance(
            matched_record.rejected, bool
        )
        if is_sticky:
            if (
                matched_record.sticky_scope is None
                and self._allow_legacy_approval_binding_reconstruction
            ):
                scope_identity = tool_invocation_approval_scope(
                    selected_invocation.raw_item,
                    tool_lookup_key=selected_invocation.tool_lookup_key,
                    tool_name=selected_invocation.tool_name,
                )
                if scope_identity is not None:
                    matched_record.sticky_scope = scope_identity[1]
            binding_status = self._approved_tool_invocation_status(
                selected_invocation.raw_item,
                tool_lookup_key=selected_invocation.tool_lookup_key,
                tool_name=selected_invocation.tool_name,
            )
            return status if binding_status is not None else None
        if current_invocation is not None:
            current_identity = tool_invocation_identity(
                current_invocation.raw_item,
                tool_lookup_key=current_invocation.tool_lookup_key,
                tool_name=current_invocation.tool_name,
            )
            if current_identity is None:
                self._approved_tool_invocation_status(
                    current_invocation.raw_item,
                    tool_lookup_key=current_invocation.tool_lookup_key,
                    tool_name=current_invocation.tool_name,
                )
                return None
        binding_status = self._approved_tool_invocation_status(
            selected_invocation.raw_item,
            tool_lookup_key=selected_invocation.tool_lookup_key,
            tool_name=selected_invocation.tool_name,
        )
        if binding_status is None:
            current_identity = tool_invocation_identity(
                selected_invocation.raw_item,
                tool_lookup_key=selected_invocation.tool_lookup_key,
                tool_name=selected_invocation.tool_name,
            )
            if current_identity is not None:
                return None
            if existing_pending is not None:
                pending_identity = tool_invocation_identity(
                    existing_pending.raw_item,
                    tool_lookup_key=existing_pending.tool_lookup_key,
                    tool_name=existing_pending.tool_name,
                )
                if pending_identity is None and is_mcp_approval_invocation(
                    existing_pending.raw_item
                ):
                    return None
            return status
        return status

    def _rebuild_approvals(self, approvals: Any) -> None:
        """Restore approvals from serialized state."""
        self._approvals = {}
        if not isinstance(approvals, Mapping):
            return
        for tool_name, record_dict in approvals.items():
            if not isinstance(tool_name, str) or not isinstance(record_dict, dict):
                continue
            self._approvals[tool_name] = self._restore_approval_record(record_dict)

    @classmethod
    def _restore_approval_record(cls, record_dict: Mapping[str, Any]) -> _ApprovalRecord:
        record = _ApprovalRecord()
        record.approved = cls._restore_approval_value(record_dict.get("approved", []))
        record.rejected = cls._restore_approval_value(record_dict.get("rejected", []))
        rejection_messages = record_dict.get("rejection_messages", {})
        if isinstance(rejection_messages, dict):
            record.rejection_messages = {
                str(call_id): message
                for call_id, message in rejection_messages.items()
                if isinstance(message, str)
            }
        sticky_rejection_message = record_dict.get("sticky_rejection_message")
        if isinstance(sticky_rejection_message, str):
            record.sticky_rejection_message = sticky_rejection_message
        sticky_scope = record_dict.get("sticky_scope")
        if isinstance(sticky_scope, str):
            record.sticky_scope = sticky_scope
        return record

    def _rebuild_tool_invocations(
        self,
        invocations: Any,
        *,
        validation_error_factory: Callable[[str], UserError] = UserError,
    ) -> None:
        """Restore the current-schema canonical tool invocation ledger."""
        self._tool_invocations = {}
        if not isinstance(invocations, Mapping):
            raise validation_error_factory("RunState tool_invocations must be a mapping.")
        for call_id, serialized_invocation in invocations.items():
            if not isinstance(call_id, str) or not call_id:
                raise validation_error_factory(
                    "RunState tool_invocations contains an invalid call ID."
                )
            if not isinstance(serialized_invocation, Mapping):
                raise validation_error_factory("RunState tool invocation must be a mapping.")
            invocation_type = serialized_invocation.get("type")
            approval_scope = serialized_invocation.get("approval_scope")
            fingerprint = serialized_invocation.get("fingerprint")
            executed = serialized_invocation.get("executed")
            completed = serialized_invocation.get("completed")
            if (
                not is_tool_invocation_type(invocation_type)
                or not is_tool_invocation_digest(approval_scope)
                or not is_tool_invocation_digest(fingerprint)
                or not isinstance(executed, bool)
                or not isinstance(completed, bool)
                or (completed and not executed)
            ):
                raise validation_error_factory(
                    "RunState tool invocation contains invalid lifecycle data."
                )
            self._tool_invocations[call_id] = _ToolInvocationRecord(
                invocation_type=invocation_type,
                approval_scope=approval_scope,
                fingerprint=fingerprint,
                executed=executed,
                completed=completed,
            )

    def _mark_restored_unbound_approval_call_ids(self) -> None:
        """Require reapproval for restored per-call decisions without a ledger binding."""
        for record in self._approvals.values():
            for decision in (record.approved, record.rejected):
                if not isinstance(decision, list):
                    continue
                self._restored_unbound_approval_call_ids.update(
                    call_id for call_id in decision if call_id not in self._tool_invocations
                )

    def _rebuild_hosted_mcp_approvals(self, approvals: Any) -> None:
        """Restore typed hosted MCP approval records from serialized state."""
        if not isinstance(approvals, list):
            return
        for entry in approvals:
            if not isinstance(entry, Mapping):
                continue
            identity = entry.get("identity")
            decision = entry.get("decision")
            if not isinstance(identity, Mapping) or not isinstance(decision, Mapping):
                continue
            identity_type = identity.get("type")
            if identity_type == "server_tool":
                server_label = identity.get("server_label")
                tool_name = identity.get("tool_name")
                if not isinstance(server_label, str) or not server_label:
                    continue
                if not isinstance(tool_name, str) or not tool_name:
                    continue
                key: HostedMCPApprovalKey = ("hosted_mcp", server_label, tool_name)
            elif identity_type == "request":
                request_id = identity.get("request_id")
                if not isinstance(request_id, str) or not request_id:
                    continue
                key = ("hosted_mcp_call", request_id)
            elif identity_type == "query":
                tool_name = identity.get("tool_name")
                request_id = identity.get("request_id")
                if not isinstance(tool_name, str) or not tool_name:
                    continue
                if not isinstance(request_id, str) or not request_id:
                    continue
                key = ("hosted_mcp_query", tool_name, request_id)
            else:
                continue
            self._approvals[key] = self._restore_approval_record(decision)

    def _fork_with_tool_input(self, tool_input: Any) -> RunContextWrapper[TContext]:
        """Create a child context that shares approvals and usage with tool input set."""
        fork = RunContextWrapper(context=self.context)
        fork.usage = self.usage
        self._share_tool_state_with(fork)
        fork.turn_input = self.turn_input
        fork.tool_input = tool_input
        return fork

    def _fork_without_tool_input(self) -> RunContextWrapper[TContext]:
        """Create a child context that shares approvals and usage without tool input."""
        fork = RunContextWrapper(context=self.context)
        fork.usage = self.usage
        self._share_tool_state_with(fork)
        fork.turn_input = self.turn_input
        return fork


@dataclass(eq=False)
class AgentHookContext(RunContextWrapper[TContext]):
    """Context passed to agent hooks (on_start, on_end)."""
