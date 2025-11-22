from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Generic

from typing_extensions import TypeVar

from .usage import Usage

if TYPE_CHECKING:
    from .items import ToolApprovalItem

TContext = TypeVar("TContext", default=Any)


class ApprovalRecord:
    """Tracks approval/rejection state for a tool."""

    approved: bool | list[str]
    """Either True (always approved), False (never approved), or a list of approved call IDs."""

    rejected: bool | list[str]
    """Either True (always rejected), False (never rejected), or a list of rejected call IDs."""

    def __init__(self):
        self.approved = []
        self.rejected = []


@dataclass
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

    _approvals: dict[str, ApprovalRecord] = field(default_factory=dict)
    """Internal tracking of tool approval/rejection decisions."""

    def is_tool_approved(self, tool_name: str, call_id: str) -> bool | None:
        """Check if a tool call has been approved.

        Args:
            tool_name: The name of the tool being called.
            call_id: The ID of the specific tool call.

        Returns:
            True if approved, False if rejected, None if not yet decided.
        """
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

        # Check for individual call approval/rejection
        individual_approval = (
            call_id in approval_entry.approved
            if isinstance(approval_entry.approved, list)
            else False
        )
        individual_rejection = (
            call_id in approval_entry.rejected
            if isinstance(approval_entry.rejected, list)
            else False
        )

        if individual_approval and individual_rejection:
            # Approval takes precedence
            return True

        if individual_approval:
            return True

        if individual_rejection:
            return False

        return None

    def approve_tool(self, approval_item: ToolApprovalItem, always_approve: bool = False) -> None:
        """Approve a tool call.

        Args:
            approval_item: The tool approval item to approve.
            always_approve: If True, always approve this tool (for all future calls).
        """
        # Extract tool name: use explicit tool_name or fallback to raw_item.name
        tool_name = approval_item.tool_name or (
            getattr(approval_item.raw_item, "name", None)
            if not isinstance(approval_item.raw_item, dict)
            else approval_item.raw_item.get("name")
        )
        if not tool_name:
            raise ValueError("Cannot determine tool name from approval item")

        # Extract call ID: function tools have call_id, hosted tools have id
        call_id: str | None = None
        if isinstance(approval_item.raw_item, dict):
            call_id = (
                approval_item.raw_item.get("callId")
                or approval_item.raw_item.get("call_id")
                or approval_item.raw_item.get("id")
            )
        elif hasattr(approval_item.raw_item, "call_id"):
            call_id = approval_item.raw_item.call_id
        elif hasattr(approval_item.raw_item, "id"):
            call_id = approval_item.raw_item.id

        if not call_id:
            raise ValueError("Cannot determine call ID from approval item")

        if always_approve:
            approval_entry = ApprovalRecord()
            approval_entry.approved = True
            approval_entry.rejected = []
            self._approvals[tool_name] = approval_entry
            return

        if tool_name not in self._approvals:
            self._approvals[tool_name] = ApprovalRecord()

        approval_entry = self._approvals[tool_name]
        if isinstance(approval_entry.approved, list):
            approval_entry.approved.append(call_id)

    def reject_tool(self, approval_item: ToolApprovalItem, always_reject: bool = False) -> None:
        """Reject a tool call.

        Args:
            approval_item: The tool approval item to reject.
            always_reject: If True, always reject this tool (for all future calls).
        """
        # Extract tool name: use explicit tool_name or fallback to raw_item.name
        tool_name = approval_item.tool_name or (
            getattr(approval_item.raw_item, "name", None)
            if not isinstance(approval_item.raw_item, dict)
            else approval_item.raw_item.get("name")
        )
        if not tool_name:
            raise ValueError("Cannot determine tool name from approval item")

        # Extract call ID: function tools have call_id, hosted tools have id
        call_id: str | None = None
        if isinstance(approval_item.raw_item, dict):
            call_id = (
                approval_item.raw_item.get("callId")
                or approval_item.raw_item.get("call_id")
                or approval_item.raw_item.get("id")
            )
        elif hasattr(approval_item.raw_item, "call_id"):
            call_id = approval_item.raw_item.call_id
        elif hasattr(approval_item.raw_item, "id"):
            call_id = approval_item.raw_item.id

        if not call_id:
            raise ValueError("Cannot determine call ID from approval item")

        if always_reject:
            approval_entry = ApprovalRecord()
            approval_entry.approved = False
            approval_entry.rejected = True
            self._approvals[tool_name] = approval_entry
            return

        if tool_name not in self._approvals:
            self._approvals[tool_name] = ApprovalRecord()

        approval_entry = self._approvals[tool_name]
        if isinstance(approval_entry.rejected, list):
            approval_entry.rejected.append(call_id)

    def _rebuild_approvals(self, approvals: dict[str, dict[str, Any]]) -> None:
        """Rebuild approvals from serialized state (for RunState deserialization).

        Args:
            approvals: Dictionary mapping tool names to approval records.
        """
        self._approvals = {}
        for tool_name, record_dict in approvals.items():
            record = ApprovalRecord()
            record.approved = record_dict.get("approved", [])
            record.rejected = record_dict.get("rejected", [])
            self._approvals[tool_name] = record
