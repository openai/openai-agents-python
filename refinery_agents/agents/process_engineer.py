"""Process engineering specialist agent."""

from __future__ import annotations

from agents import Agent, ModelSettings

from ..config import RefineryConfig
from ..context import RefineryContext
from ..tools import get_line_list, get_pfds, get_pids

INSTRUCTIONS = """
You are the Process Engineering specialist. Follow a reasoning-first workflow:
1. Restate the process objective and identify the process boundaries (upstream/downstream).
2. Review P&IDs, PFDs, and the line list by calling the provided tools. Cite each document by
   identifier and revision in your reasoning.
3. Determine the service conditions (normal, minimum, maximum), fluid properties, and any red
   flags (two-phase flow, cavitation, noise, flashing, corrosion risks).
4. Provide a structured JSON object with keys `summary`, `constraints`, `risks`, and `doc_refs`.
   `doc_refs` must list the identifiers you relied on. Do not make mechanical or instrument
   selections – focus strictly on process conditions and design basis.
"""


def create_process_engineer_agent(config: RefineryConfig) -> Agent[RefineryContext]:
    """Create the process engineering domain agent."""

    return Agent(
        name="process_engineer",
        instructions=INSTRUCTIONS,
        handoff_description="Interprets process documentation for operating constraints.",
        model=config.openai_model_name,
        model_settings=ModelSettings(temperature=0.15),
        tools=[get_pids, get_pfds, get_line_list],
    )

