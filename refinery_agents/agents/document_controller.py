"""Agent responsible for discovering relevant refinery documents."""

from __future__ import annotations

from agents import Agent, ModelSettings

from ..config import RefineryConfig
from ..context import RefineryContext
from ..tools import (
    get_engineering_standards,
    get_instrument_datasheet,
    get_isometrics,
    get_jb_cable_schedule,
    get_line_list,
    get_pfds,
    get_pids,
    get_pipe_class,
    get_vendor_manual,
)

INSTRUCTIONS = """
You are the Document Controller for an instrumentation and control engineering change.
Your job is to inventory which authorised refinery documents should be consulted for the
work order. Work in the following order:
1. Analyse the request and list which document categories (P&ID, PFD, line list,
   datasheet, vendor manual, standards, pipe class, isometric, JB/cable schedule) are relevant.
2. Invoke the appropriate retrieval tools to obtain document references. Never invent documents
   and never perform design reasoning – only discovery and short interpretation of scope.
3. Summarise the findings in structured JSON with keys `summary` and `bundles` where `bundles`
   is a mapping from document category to the retrieved list.
"""


def create_document_controller_agent(config: RefineryConfig) -> Agent[RefineryContext]:
    """Create the document controller agent configured with repository tools."""

    return Agent(
        name="document_controller",
        instructions=INSTRUCTIONS,
        handoff_description="Curates allowed refinery documents for downstream engineers.",
        model=config.openai_model_name,
        model_settings=ModelSettings(temperature=0.1),
        tools=[
            get_pids,
            get_pfds,
            get_line_list,
            get_instrument_datasheet,
            get_vendor_manual,
            get_pipe_class,
            get_isometrics,
            get_jb_cable_schedule,
            get_engineering_standards,
        ],
    )

