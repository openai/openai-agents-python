from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Generic

from typing_extensions import TypeVar

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
        approval_entry = self._approvals.get(tool_name)
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
        tool_name = self._resolve_tool_name(approval_item)
        call_id = self._resolve_call_id(approval_item)

        approval_entry = self._get_or_create_approval_entry(tool_name)
        if always or call_id is None:
            approval_entry.approved = approve
            approval_entry.rejected = [] if approve else True
            if not approve:
                approval_entry.approved = False
            return

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
        self, tool_name: str, call_id: str, *, existing_pending: ToolApprovalItem | None = None
    ) -> bool | None:
        """Return approval status, retrying with pending item's tool name if necessary."""
        status = self.is_tool_approved(tool_name, call_id)
        if status is None and existing_pending:
            fallback_tool_name = self._resolve_tool_name(existing_pending)
            status = self.is_tool_approved(fallback_tool_name, call_id)
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
