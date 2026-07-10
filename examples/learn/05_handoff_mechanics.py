"""Example 05: internal implementation of handoff.

Principle (agents/handoffs/__init__.py:93-280):
  handoff(agent) registers a function tool named transfer_to_<agent_name> from the model's perspective
  - tool_name: default "transfer_to_<agent_name>", can be changed via tool_name_override
  - tool_description: default "<handoff_description>", can be changed via tool_description_override
  - on_handoff(ctx, [input_data]): runs when triggered by model (used for side effects / fetching data)
  - input_type: Pydantic schema, model can pass structured parameters to on_handoff
  - input_filter(HandoffInputData) -> HandoffInputData: controls the context seen by the new agent
  - is_enabled: bool or callable, dynamic switch

Observe four things:
  1) print the schema of the "virtual tool" registered by handoff, see what it looks like
  2) on_handoff actually triggers
  3) how input_type uses Pydantic to let the model pass structured reasons
  4) is_enabled dynamic disable

Usage: python examples/05_handoff_mechanics.py
"""

import asyncio
import sys
from pathlib import Path
from pprint import pprint

sys.path.insert(0, str(Path(__file__).parent))

from config import MODEL  # type: ignore[import-not-found]
from pydantic import BaseModel

from agents import Agent, RunContextWrapper, Runner, handoff

# 1) A basic handoff: only look at on_handoff side effect
on_log = []


def record_handoff(ctx):
    on_log.append(f"on_handoff fired at {ctx.usage.requests} requests used")


basic_agent = Agent(
    name="BasicAgent",
    instructions="I am BasicAgent.",
    model=MODEL,
)
handoff_basic = handoff(
    agent=basic_agent,
    on_handoff=record_handoff,
)


# 2) Handoff with input_type: model must pass reason
class EscalationData(BaseModel):
    reason: str
    severity: int


def record_escalation(ctx: RunContextWrapper, data: EscalationData):
    on_log.append(f"ESCALATE reason={data.reason!r} severity={data.severity}")


escalation_agent = Agent(
    name="EscalationAgent",
    instructions="I am EscalationAgent, handling high priority issues.",
    model=MODEL,
)
handoff_escalate = handoff(
    agent=escalation_agent,
    on_handoff=record_escalation,
    input_type=EscalationData,
)


# 3) is_enabled dynamic switch: judge whether escalation is possible based on context
flag = {"can_escalate": True}


def can_escalate(ctx, _data) -> bool:
    return flag["can_escalate"]


def record_blocked(ctx, _data):
    on_log.append("block: cannot escalate right now")


escalation_agent2 = Agent(
    name="EscalationAgent2",
    instructions="I am EscalationAgent2.",
    model=MODEL,
)
handoff_conditional = handoff(
    agent=escalation_agent2,
    on_handoff=record_blocked,
    input_type=EscalationData,
    is_enabled=can_escalate,
)


#  Main agent: route based on user_role 
triage = Agent(
    name="Triage",
    instructions=(
        "You are Triage.\n"
        "- Normal issues: handoff to BasicAgent\n"
        "- Needs escalation: handoff to EscalationAgent, passing reason and severity (1-5) when calling"
    ),
    model=MODEL,
    handoffs=[handoff_basic, handoff_escalate, handoff_conditional],
)


async def main():
    # First look at what tools each handoff registered
    print("=== Virtual tools registered by handoff ===")
    for h in triage.handoffs:
        name = getattr(h, "tool_name", "unknown")
        description = getattr(h, "tool_description", "")
        agent_name = getattr(h, "agent_name", "unknown")
        print(f"\nname:        {name}")
        print(f"description: {description[:120]}")
        print(f"agent_name:  {agent_name}")
        print("input_json_schema:")
        pprint(getattr(h, "input_json_schema", {}))
        print(f"is_enabled:  {getattr(h, 'is_enabled', True)}")

    print("\n Scenario 1: Normal handoff ")
    on_log.clear()
    flag["can_escalate"] = True
    r1 = await Runner.run(triage, "Hello, just chatting")
    print(f"final agent:    {r1.last_agent.name}")
    print(f"final_output:   {r1.final_output}")
    print(f"on_log:         {on_log}")

    print("\n Scenario 2: input_type escalation ")
    on_log.clear()
    r2 = await Runner.run(triage, "Online payment failed, please escalate to P0 urgent handling")
    print(f"final agent:    {r2.last_agent.name}")
    print(f"final_output:   {r2.final_output}")
    print(f"on_log:         {on_log}")

    print("\n Scenario 3: is_enabled closes escalation ")
    on_log.clear()
    flag["can_escalate"] = False
    r3 = await Runner.run(triage, "Payment failed, must escalate to P0")
    print(f"final agent:    {r3.last_agent.name}")
    print(f"on_log:         {on_log}")


if __name__ == "__main__":
    asyncio.run(main())
