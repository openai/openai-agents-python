"""Piping and mechanical engineering agent."""

from __future__ import annotations

from agents import Agent, ModelSettings

from ..config import RefineryConfig
from ..context import RefineryContext
from ..tools import get_isometrics, get_jb_cable_schedule, get_pipe_class

INSTRUCTIONS = """
You are the Piping/Mechanical Engineer. Work methodically:
1. Summarise the mechanical scope from the request.
2. Consult pipe class specifications, isometrics, and cable/JB schedules using the tools provided.
3. Evaluate flange ratings, materials, face-to-face requirements, available straight runs,
   structural supports, and accessibility/maintenance considerations. Highlight any constructability
   risks and tie them back to document references.
4. Produce JSON with keys `summary`, `constraints`, `installation_notes`, and `doc_refs`.
Avoid instrumentation selection or safety commentary beyond mechanical fit-up implications.
"""


def create_piping_engineer_agent(config: RefineryConfig) -> Agent[RefineryContext]:
    """Create the piping/mechanical engineer agent."""

    return Agent(
        name="piping_engineer",
        instructions=INSTRUCTIONS,
        handoff_description="Checks mechanical feasibility, pipe class compliance, and layout.",
        model=config.openai_model_name,
        model_settings=ModelSettings(temperature=0.15),
        tools=[get_pipe_class, get_isometrics, get_jb_cable_schedule],
    )

