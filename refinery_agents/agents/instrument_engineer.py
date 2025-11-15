"""Instrumentation engineering specialist agent."""

from __future__ import annotations

from agents import Agent, ModelSettings

from ..config import RefineryConfig
from ..context import RefineryContext
from ..tools import (
    get_engineering_standards,
    get_instrument_datasheet,
    get_jb_cable_schedule,
    get_vendor_manual,
)

INSTRUCTIONS = """
You are the Instrumentation Engineer. Think in three phases:
1. Clarify the measurement need (fluid, accuracy, diagnostics) using the provided briefing.
2. Pull instrument datasheets, vendor manuals, and applicable standards via the tools. Cite
   every reference explicitly by identifier and revision in your reasoning.
3. Produce at least three design options in JSON: `options` (list of objects with `name`,
   `description`, `pros`, `cons`, `suitability`, and `supporting_docs`). Indicate which option you
   recommend and why. Consider power/signal cabling using the JB/cable schedule when relevant.
Avoid mechanical scope decisions; stay focused on instrumentation and control aspects.
"""


def create_instrument_engineer_agent(config: RefineryConfig) -> Agent[RefineryContext]:
    """Create the instrumentation engineering agent."""

    return Agent(
        name="instrument_engineer",
        instructions=INSTRUCTIONS,
        handoff_description="Evaluates instrument technologies and prepares options.",
        model=config.openai_model_name,
        model_settings=ModelSettings(temperature=0.2),
        tools=[
            get_instrument_datasheet,
            get_vendor_manual,
            get_engineering_standards,
            get_jb_cable_schedule,
        ],
    )

