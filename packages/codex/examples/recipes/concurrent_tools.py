"""Share a quota across Sessions on one event loop without losing updates.

Async callbacks run on the Session event loop; synchronous callbacks run in
workers. ToolContext freezes its fields, not the application object it carries.
The immutable identity and mutable quota below have separate owners. Every
writer of this process-local quota must use its lock, including a native child
that shares it. The lock is not a distributed or cross-thread quota service.
"""

import asyncio
from dataclasses import dataclass, field

from agents import Agent, ToolContext, function_tool
from agents.testing import ScriptedResponses, text, tool_call


@dataclass(frozen=True)
class Identity:
    user_id: str


@dataclass
class Quota:
    remaining: int
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    arrived: int = 0
    both_arrived: asyncio.Event = field(default_factory=asyncio.Event)
    ledger: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Application:
    identity: Identity
    quota: Quota


@function_tool
async def reserve(context: ToolContext[Application]) -> str:
    """Reserve one unit for the authenticated user, if capacity remains."""
    quota = context.context.quota
    quota.arrived += 1
    if quota.arrived == 2:
        quota.both_arrived.set()
    await quota.both_arrived.wait()  # Force overlapping callbacks in this demonstration.
    async with quota.lock:
        if quota.remaining == 0:
            return "full"
        quota.remaining -= 1
        quota.ledger.append(context.context.identity.user_id)
        return "reserved"


async def run_example() -> tuple[Quota, list[str | None]]:
    quota = Quota(remaining=1)
    async with (
        ScriptedResponses([tool_call("reserve", {}, call_id="a")], [text("done")]) as first,
        ScriptedResponses([tool_call("reserve", {}, call_id="b")], [text("done")]) as second,
    ):
        async with (
            Agent(
                instructions="Use the declared tools.", tools=[reserve], provider=first.provider
            ).session(context=Application(Identity("user-a"), quota)) as left,
            Agent(
                instructions="Use the declared tools.", tools=[reserve], provider=second.provider
            ).session(context=Application(Identity("user-b"), quota)) as right,
        ):
            turns = await asyncio.gather(left.submit("Reserve."), right.submit("Reserve."))
            await asyncio.wait_for(asyncio.gather(*(turn.get_result() for turn in turns)), 10)
        outputs = [first.tool_output("a"), second.tool_output("b")]
    return quota, outputs


async def main() -> None:
    quota, outputs = await run_example()
    assert quota.remaining == 0 and len(quota.ledger) == 1
    assert sorted(outputs) == ["full", "reserved"]
    print("Two Sessions shared one unit without overspending.")


if __name__ == "__main__":
    asyncio.run(main())
