"""Delegate one application-selected question to one trusted A2A specialist."""

from collections.abc import Callable
from typing import Protocol
from uuid import uuid4

from a2a.types import Message, Task, TaskState

from agents.decorators import tool
from agents.plugins import Plugin, PluginHooks, RequireApproval

__all__ = ["Client", "specialist"]


class Client(Protocol):
    """The subset of the SDK's A2AClient needed for one owner and one endpoint."""

    @property
    def card_json(self) -> bytes | None: ...

    @property
    def rpc_token_provider(self) -> Callable[[], str] | None: ...

    async def send_message(self, text: str, *, message_id: str) -> Message | Task: ...

    async def get_task(self, *, task_id: str, context_id: str) -> Task: ...


def specialist(
    client: Client, *, question: str, artifact_id: str, verdicts: tuple[str, ...]
) -> Plugin[None]:
    """Create once per owner and question. The token provider must recheck the grant."""
    if client.rpc_token_provider is None or client.card_json is None:
        raise ValueError("Supply a grant-checking token provider and a local Agent Card.")
    allowed = frozenset(verdicts)
    if not question or not artifact_id or not allowed or allowed & {"", "pending", "unknown"}:
        raise ValueError("Supply a question, artifact ID and nonempty, non-reserved verdicts.")

    attempted = False
    receipt: tuple[str, str] | None = None

    def read(task: Task) -> str:
        if task.status.state in (TaskState.TASK_STATE_SUBMITTED, TaskState.TASK_STATE_WORKING):
            return "pending"
        if task.status.state == TaskState.TASK_STATE_COMPLETED and len(task.artifacts) == 1:
            artifact = task.artifacts[0]
            if artifact.artifact_id == artifact_id and len(artifact.parts) == 1:
                verdict = artifact.parts[0].text
                if verdict in allowed:
                    return verdict
        return "unknown"

    @tool
    async def ask_specialist() -> str:
        """Send the application's selected question to the designated specialist once."""
        nonlocal attempted, receipt
        if attempted:
            return "unknown"
        # Reserve before awaiting. A failed or cancelled send might still reach the peer.
        attempted = True
        try:
            reply = await client.send_message(question, message_id=uuid4().hex)
        except Exception:
            # Never put dependency errors or private request details into the model input.
            return "unknown"
        if not isinstance(reply, Task):
            return "unknown"
        result = read(reply)
        if result == "pending":
            receipt = (reply.id, reply.context_id)
        return result

    @tool
    async def refresh_specialist() -> str:
        """Read the status of a previously pending task without sending the question again."""
        if receipt is None:
            return "unknown"
        try:
            task = await client.get_task(task_id=receipt[0], context_id=receipt[1])
        except Exception:
            return "unknown"
        return read(task)

    return Plugin(
        name="specialist",
        tools=[ask_specialist, refresh_specialist],
        hooks=PluginHooks(check_own_tool=lambda _: RequireApproval()),
        instructions=(
            "Ask the specialist once. Report unknown results as unknown. "
            "Refresh a pending task only at the user's request."
        ),
    )
