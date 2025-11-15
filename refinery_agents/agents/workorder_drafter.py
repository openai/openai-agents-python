"""Agent that synthesises the final engineering work order."""

from __future__ import annotations

from agents import Agent, ModelSettings

from ..config import RefineryConfig
from ..context import RefineryContext
from ..work_order_schema import EngineeringWorkOrder

INSTRUCTIONS = """
You are the Work Order Drafter. You will receive consolidated findings from process,
instrumentation, piping, and standards reviewers. Produce a complete EngineeringWorkOrder JSON
object with the following fields: `title`, `scope_summary`, `design_basis`, `design_assumptions`,
`selected_option`, `rejected_options`, `materials_and_parts`, `task_steps`, `open_questions`,
`required_approvals`, and `evidence_sources` (DocRefs). Ensure every assertion references at least
one document identifier. Use concise, actionable language suitable for a refinery work package.
"""


def create_workorder_drafter_agent(config: RefineryConfig) -> Agent[RefineryContext]:
    """Create the drafting agent that outputs the work order schema."""

    return Agent(
        name="workorder_drafter",
        instructions=INSTRUCTIONS,
        handoff_description="Drafts the consolidated engineering work order.",
        model=config.openai_model_name,
        model_settings=ModelSettings(temperature=0.12),
        output_type=EngineeringWorkOrder,
    )

