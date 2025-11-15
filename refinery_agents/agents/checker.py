"""Independent reviewer agent for the work order."""

from __future__ import annotations

from agents import Agent, ModelSettings

from ..config import RefineryConfig
from ..context import RefineryContext

INSTRUCTIONS = """
You are the independent Checker. Examine the drafted EngineeringWorkOrder and search for flaws:
- Missing or incorrect document references.
- Gaps in safety, testing, or commissioning steps.
- Unstated assumptions or conflicting recommendations.
Respond in JSON with keys `issues` (list of strings) and `severity` (`none`, `minor`, or `major`).
When no blocking issues exist, set `severity` to `none` and provide an empty list.
"""


def create_checker_agent(config: RefineryConfig) -> Agent[RefineryContext]:
    """Create the checker/challenger agent."""

    return Agent(
        name="checker",
        instructions=INSTRUCTIONS,
        handoff_description="Performs independent review of the drafted work order.",
        model=config.openai_model_name,
        model_settings=ModelSettings(temperature=0.05),
    )

