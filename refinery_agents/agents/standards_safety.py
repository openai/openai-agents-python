"""Standards and functional safety review agent."""

from __future__ import annotations

from agents import Agent, ModelSettings

from ..config import RefineryConfig
from ..context import RefineryContext
from ..tools import get_engineering_standards

INSTRUCTIONS = """
You are the Standards & Safety authority. Your mission:
1. Identify applicable standards (API, ISA, company specs) and SIS/SIL considerations. Use only the
   provided standards retrieval tool.
2. Cross-check the proposed design notes (provided in the request) against those standards. Flag any
   compliance gaps, redundancy requirements, proof test intervals, or documentation obligations.
3. Output JSON with `mandatory_requirements`, `recommendations`, `risks`, and `doc_refs`. Each entry
   must reference the supporting standard identifier and clause/page where possible.
Avoid proposing new equipment or re-performing process/mechanical analysis.
"""


def create_standards_safety_agent(config: RefineryConfig) -> Agent[RefineryContext]:
    """Create the standards and safety oversight agent."""

    return Agent(
        name="standards_safety",
        instructions=INSTRUCTIONS,
        handoff_description="Ensures alignment with standards, safety, and compliance obligations.",
        model=config.openai_model_name,
        model_settings=ModelSettings(temperature=0.1),
        tools=[get_engineering_standards],
    )

