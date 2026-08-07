from __future__ import annotations

from collections.abc import Mapping
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
from .exceptions import UserError
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
class _ApprovalRecord:
    """Tracks approval/rejection state for a tool.

    ``approved`` and ``rejected`` are either booleans (permanent allow/deny)
    or lists of call IDs when approval is scoped to specific tool calls.
    """

    approved: bool | list[str] = field(default_factory=list)
    rejected: bool | list[str] = field(default_factory=list)
    rejection_messages: dict[str, str] = field(default_factory=dict)
    sticky_rejection_message: str | None = None


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
    tool_input: Any | None = None
    """Structured input for the current agent tool run, when available."""

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
        raw = approval_item.raw_item
        if isinstance(raw, dict):
            provider_data = raw.get("provider_data")
            if (
                isinstance(provider_data, dict)
                and provider_data.get("type") == "mcp_approval_request"
            ):
                candidate = provider_data.get("id")
                if isinstance(candidate, str):
                    return candidate
            candidate = raw.get("call_id") or raw.get("id")
        else:
            provider_data = getattr(raw, "provider_data", None)
            if (
                isinstance(provider_data, dict)
                and provider_data.get("type") == "mcp_approval_request"
            ):
                candidate = provider_data.get("id")
                if isinstance(candidate, str):
                    return candidate
            candidate = getattr(raw, "call_id", None) or getattr(raw, "id", None)
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
        pending_key = self._resolve_approval_key(existing_pending) if existing_pending else None
        pending_tool_name = self._resolve_tool_name(existing_pending) if existing_pending else None
        pending_keys = (
            list(self._resolve_approval_keys(existing_pending))
            if existing_pending is not None
            else []
        )

        if existing_pending and pending_key is not None:
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
        if existing_pending:
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

        approval_entries: tuple[tuple[_ApprovalRecord, bool], ...]
        if hosted_request is not None:
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
    ) -> bool | None:
        """Return approval status, retrying with pending item's tool name if necessary."""
        if existing_pending is not None:
            hosted_request = get_hosted_mcp_approval_request_identity(existing_pending)
            if hosted_request is not None:
                hosted_status, _ = self._resolve_hosted_mcp_approval_decision(existing_pending)
                return hosted_status

        candidates: list[str] = []
        explicit_namespace = (
            tool_namespace if isinstance(tool_namespace, str) and tool_namespace else None
        )
        pending_namespace = (
            self._resolve_tool_namespace(existing_pending) if existing_pending is not None else None
        )
        pending_key = self._resolve_approval_key(existing_pending) if existing_pending else None
        pending_tool_name = self._resolve_tool_name(existing_pending) if existing_pending else None
        pending_keys = (
            list(self._resolve_approval_keys(existing_pending))
            if existing_pending is not None
            else []
        )

        if existing_pending and pending_key is not None:
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
        if existing_pending:
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
        for candidate in candidates:
            status = self._get_approval_status_for_key(candidate, call_id)
            if status is not None:
                break
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
        return record

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
        fork._approvals = self._approvals
        fork.turn_input = self.turn_input
        fork.tool_input = tool_input
        return fork

    def _fork_without_tool_input(self) -> RunContextWrapper[TContext]:
        """Create a child context that shares approvals and usage without tool input."""
        fork = RunContextWrapper(context=self.context)
        fork.usage = self.usage
        fork._approvals = self._approvals
        fork.turn_input = self.turn_input
        return fork


@dataclass(eq=False)
class AgentHookContext(RunContextWrapper[TContext]):
    """Context passed to agent hooks (on_start, on_end)."""
