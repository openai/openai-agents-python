"""Realtime agent definitions shared by the Twilio SIP example."""

from __future__ import annotations

import asyncio

from agents import function_tool
from agents.extensions.handoff_prompt import RECOMMENDED_PROMPT_PREFIX
from agents.realtime import RealtimeAgent, realtime_handoff

# --- Tools -----------------------------------------------------------------


WELCOME_MESSAGE = "Hello, this is ABC customer service. How can I help you today?"


@function_tool(
    name_override="faq_lookup_tool", description_override="Lookup frequently asked questions."
)
async def faq_lookup_tool(question: str) -> str:
    """Fetch FAQ answers for the caller."""

    await asyncio.sleep(3)

    q = question.lower()
    if "plan" in q or "wifi" in q or "wi-fi" in q:
        return "We provide complimentary Wi-Fi. Join the ABC-Customer network."  # demo data
    if "billing" in q or "invoice" in q:
        return "Your latest invoice is available in the ABC portal under Billing > History."
    if "hours" in q or "support" in q:
        return "Human support agents are available 24/7; transfer to the specialist if needed."
    return "I'm not sure about that. Let me transfer you back to the triage agent."


@function_tool
async def update_customer_record(customer_id: str, note: str) -> str:
    """Record a short note about the caller."""

    await asyncio.sleep(1)
    return f"Recorded note for {customer_id}: {note}"


# --- Agents ----------------------------------------------------------------


faq_agent = RealtimeAgent(
    name="FAQ Agent",
    handoff_description="Handles frequently asked questions and general account inquiries.",
    instructions=f"""{RECOMMENDED_PROMPT_PREFIX}
    You are an FAQ specialist. Always rely on the faq_lookup_tool for answers and keep replies
    concise. If the caller needs hands-on help, transfer back to the triage agent.
    """,
    tools=[faq_lookup_tool],
)

records_agent = RealtimeAgent(
    name="Records Agent",
    handoff_description="Updates customer records with brief notes and confirmation numbers.",
    instructions=f"""{RECOMMENDED_PROMPT_PREFIX}
    You handle structured updates. Confirm the customer's ID, capture their request in a short
    note, and use the update_customer_record tool. For anything outside data updates, return to the
    triage agent.
    """,
    tools=[update_customer_record],
)

triage_agent = RealtimeAgent(
    name="Triage Agent",
    handoff_description="Greets callers and routes them to the most appropriate specialist.",
    instructions=(
        f"{RECOMMENDED_PROMPT_PREFIX} "
        "Always begin the call by saying exactly: '"
        f"{WELCOME_MESSAGE}' "
        "before collecting details. Once the greeting is complete, gather context and hand off to "
        "the FAQ or Records agents when appropriate."
    ),
    handoffs=[faq_agent, realtime_handoff(records_agent)],
)

faq_agent.handoffs.append(triage_agent)
records_agent.handoffs.append(triage_agent)


def get_starting_agent() -> RealtimeAgent:
    """Return the agent used to start each realtime call."""

    return triage_agent
