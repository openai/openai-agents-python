from __future__ import annotations

import asyncio
from typing import Any

from agents import (
    Agent,
    GuardrailFunctionOutput,
    OutputGuardrailTripwireTriggered,
    RunContextWrapper,
    output_guardrail,
    trace,
)
from examples.agent_patterns.structured_agent_audit import (
    AUDIT_PLAYBOOKS,
    AuditPlaybook,
    AuditReport,
    StructuredAuditAgents,
    build_audit_agents,
    render_report_summary,
    run_structured_agent_audit,
)
from examples.auto_mode import input_with_fallback

"""
This example shows how to combine the structured audit workflow with tracing metadata
and an output guardrail on the final audit report.
"""


@output_guardrail
async def actionable_report_guardrail(
    context: RunContextWrapper[Any], agent: Agent[Any], output: AuditReport
) -> GuardrailFunctionOutput:
    severe_findings = [f for f in output.findings if f.severity in {"critical", "high"}]
    missing_fix_plan = len(output.ordered_fix_plan) == 0
    weak_fix_plan = len(output.ordered_fix_plan) < len(severe_findings)

    return GuardrailFunctionOutput(
        output_info={
            "severe_findings": len(severe_findings),
            "fix_plan_steps": len(output.ordered_fix_plan),
            "missing_fix_plan": missing_fix_plan,
            "weak_fix_plan": weak_fix_plan,
        },
        tripwire_triggered=bool(severe_findings) and (missing_fix_plan or weak_fix_plan),
    )


def build_guardrailed_audit_agents() -> StructuredAuditAgents:
    agents = build_audit_agents()
    guarded_report_agent = Agent(
        name=agents.report.name,
        instructions=agents.report.instructions,
        output_type=AuditReport,
        output_guardrails=[actionable_report_guardrail],
    )
    return StructuredAuditAgents(
        scope=agents.scope,
        evidence=agents.evidence,
        failure_map=agents.failure_map,
        report=guarded_report_agent,
    )


async def main(playbook: AuditPlaybook = "tool_discipline") -> None:
    audit_request = input_with_fallback(
        "What agent runtime do you want to audit? ",
        (
            "Audit an agent runtime that sometimes answers operational questions "
            "without using required tools."
        ),
    )
    agents = build_guardrailed_audit_agents()

    with trace(
        "Structured agent audit with guardrails",
        metadata={
            "example": "structured_agent_audit_with_guardrails",
            "playbook": playbook,
            "playbook_guidance": AUDIT_PLAYBOOKS[playbook],
        },
    ):
        try:
            report = await run_structured_agent_audit(
                audit_request,
                agents=agents,
                playbook=playbook,
            )
        except OutputGuardrailTripwireTriggered as exc:
            print("Guardrail tripped on the final audit report.")
            print(exc.guardrail_result.output.output_info)
            return

    print(report.model_dump_json(indent=2))
    print()
    print(render_report_summary(report))


if __name__ == "__main__":
    asyncio.run(main())
