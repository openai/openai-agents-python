from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Generic

from typing_extensions import TypeVar

from ._tool_identity import (
    FunctionToolLookupKey,
    get_function_tool_approval_keys,
    get_function_tool_lookup_key,
    is_reserved_synthetic_tool_namespace,
    tool_qualified_name,
)
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
    _approvals: dict[str, _ApprovalRecord] = field(default_factory=dict)
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

    def _get_or_create_approval_entry(self, tool_name: str) -> _ApprovalRecord:
        approval_entry = self._approvals.get(tool_name)
        if approval_entry is None:
            approval_entry = _ApprovalRecord()
            self._approvals[tool_name] = approval_entry
        return approval_entry

    def is_tool_approved(self, tool_name: str, call_id: str) -> bool | None:
        """Return True/False/None for the given tool call."""
        return self._get_approval_status_for_key(tool_name, call_id)

    def _get_approval_status_for_key(self, approval_key: str, call_id: str) -> bool | None:
        """Return True/False/None for a concrete approval key and tool call."""
        approval_entry = self._approvals.get(approval_key)
        if not approval_entry:
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

    def _apply_approval_decision(
        self, approval_item: ToolApprovalItem, *, always: bool, approve: bool
    ) -> None:
        """Record an approval or rejection decision."""
        approval_keys = self._resolve_approval_keys(approval_item) or ("unknown_tool",)
        exact_approval_key = self._resolve_approval_key(approval_item)
        call_id = self._resolve_call_id(approval_item)
        decision_keys = (exact_approval_key,) if always or call_id is None else approval_keys

        for approval_key in decision_keys:
            approval_entry = self._get_or_create_approval_entry(approval_key)
            if always or call_id is None:
                approval_entry.approved = approve
                approval_entry.rejected = [] if approve else True
                if not approve:
                    approval_entry.approved = False
                continue

            opposite = approval_entry.rejected if approve else approval_entry.approved
            if isinstance(opposite, list) and call_id in opposite:
                opposite.remove(call_id)

            target = approval_entry.approved if approve else approval_entry.rejected
            if isinstance(target, list) and call_id not in target:
                target.append(call_id)

    def approve_tool(self, approval_item: ToolApprovalItem, always_approve: bool = False) -> None:
        """Approve a tool call, optionally for all future calls."""
        self._apply_approval_decision(
            approval_item,
            always=always_approve,
            approve=True,
        )

    def reject_tool(self, approval_item: ToolApprovalItem, always_reject: bool = False) -> None:
        """Reject a tool call, optionally for all future calls."""
        self._apply_approval_decision(
            approval_item,
            always=always_reject,
            approve=False,
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

    def _rebuild_approvals(self, approvals: dict[str, dict[str, Any]]) -> None:
        """Restore approvals from serialized state."""
        self._approvals = {}
        for tool_name, record_dict in approvals.items():
            record = _ApprovalRecord()
            record.approved = record_dict.get("approved", [])
            record.rejected = record_dict.get("rejected", [])
            self._approvals[tool_name] = record

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
