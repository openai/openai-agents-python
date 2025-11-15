"""Lead engineer orchestrator that coordinates the specialist agents."""

from __future__ import annotations

from agents import Agent, ModelSettings

from ..config import RefineryConfig
from ..context import RefineryContext
from ..work_order_schema import EngineeringWorkOrder

ORCHESTRATOR_INSTRUCTIONS = """
You are the Lead Engineer facilitating a multi-disciplinary design review room. Follow the
architecture B sequence strictly:
1. Intake: Restate the request and create a plan of work.
2. Document discovery: Call the `document_controller` tool to gather document bundles. Capture all
   DocRefs for reuse.
3. Specialist analysis: Sequentially engage process, instrumentation, piping, and standards agents
   (tools) supplying the request context and relevant DocRefs. Summarise each response.
4. Synthesis: Compare the specialist viewpoints, resolve conflicts, and select a recommended design
   option. Explicitly track supporting DocRefs.
5. Drafting: Provide the synthesised findings to the `workorder_drafter` tool to produce an
   EngineeringWorkOrder.
6. Checking: Submit the draft to the `checker` tool. If severity is `major`, update assumptions or
   clarifications once and reissue to the drafter; otherwise accept.
Return the final EngineeringWorkOrder object only. Never invent documents outside the supplied
tools and ensure reasoning is explicit in every step before calling tools.
"""


def create_orchestrator_agent(
    config: RefineryConfig,
    *,
    document_controller: Agent[RefineryContext],
    process_engineer: Agent[RefineryContext],
    instrument_engineer: Agent[RefineryContext],
    piping_engineer: Agent[RefineryContext],
    standards_safety: Agent[RefineryContext],
    workorder_drafter: Agent[RefineryContext],
    checker: Agent[RefineryContext],
) -> Agent[RefineryContext]:
    """Create the orchestrator agent that drives the end-to-end workflow."""

    return Agent(
        name="orchestrator",
        instructions=ORCHESTRATOR_INSTRUCTIONS,
        handoff_description="Leads the full design review workflow to produce the work order.",
        model=config.openai_model_name,
        model_settings=ModelSettings(temperature=0.05),
        output_type=EngineeringWorkOrder,
        tools=[
            document_controller.as_tool(
                tool_name="document_controller",
                tool_description="Collects authorised refinery documents grouped by type.",
            ),
            process_engineer.as_tool(
                tool_name="process_engineer",
                tool_description="Analyses process conditions and constraints using docs.",
            ),
            instrument_engineer.as_tool(
                tool_name="instrument_engineer",
                tool_description="Evaluates instrument technology options and trade-offs.",
            ),
            piping_engineer.as_tool(
                tool_name="piping_engineer",
                tool_description="Reviews pipe class compliance and mechanical feasibility.",
            ),
            standards_safety.as_tool(
                tool_name="standards_safety",
                tool_description="Checks compliance with engineering standards and safety guidance.",
            ),
            workorder_drafter.as_tool(
                tool_name="workorder_drafter",
                tool_description="Drafts the structured EngineeringWorkOrder output.",
            ),
            checker.as_tool(
                tool_name="checker",
                tool_description="Performs independent review and flags outstanding issues.",
            ),
        ],
    )

