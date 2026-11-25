from typing import Protocol

from agents import RunContextWrapper
from agents.decorators import tool
from agents.plugins import Plugin


class TicketClient(Protocol):
    async def close_if_allowed(self, user: str, ticket_id: str) -> bool:
        """Check the signed-in user and current grant atomically with the write."""
        ...


def ticket_desk(client: TicketClient, *, owner: str) -> Plugin[str]:
    """Bind the desk to the authenticated owner of one conversation."""
    if not isinstance(owner, str) or not owner.strip():
        raise ValueError("A signed-in ticket owner is required")

    @tool(needs_approval=True, failure_error_function=None)
    async def close_ticket(ctx: RunContextWrapper[str], ticket_id: str) -> str:
        """Close a support ticket by its exact ID."""
        if ctx.context != owner:
            return "denied"
        closed = await client.close_if_allowed(owner, ticket_id)
        return "closed" if closed else "denied"

    return Plugin(name="ticket-desk", tools=[close_ticket])
