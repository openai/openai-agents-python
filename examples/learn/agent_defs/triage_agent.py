"""Triage Agent - Main entry point, hands off conversations to specialists based on intent."""

from config import MODEL  # type: ignore[import-not-found]

from agent_defs.math_agent import math_agent  # type: ignore[import-not-found]
from agent_defs.weather_agent import weather_agent  # type: ignore[import-not-found]
from agents import Agent

triage_agent = Agent(
    name="TriageAgent",
    instructions=(
        "You are the receptionist for a multi-capability assistant.\n"
        "- Arithmetic / math questions -> handoff to MathAgent\n"
        "- Weather / temperature / rain / city weather -> handoff to WeatherAgent\n"
        "- Answer other casual chat briefly yourself (one or two sentences).\n"
        "Do not use tools to do the specialist's job yourself after handoff."
    ),
    model=MODEL,
    handoffs=[math_agent, weather_agent],
)
