from textwrap import dedent

from agents.agent import Agent

manager_agent = Agent(
    name="Manager Agent",
    instructions=dedent("""
You are a manager. You will always introduce yourself as the manager.
"""),
)
customer_service_agent = Agent(
    name="Customer Service Agent",
    instructions=dedent("""
You are a customer service agent. You are helpful and friendly. Only handoff to a manager if the user requests it or is becoming angry.
"""),
    handoffs=[manager_agent],
)
