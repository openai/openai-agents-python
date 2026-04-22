from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, Field

from agents import Agent, Runner, trace
from examples.auto_mode import input_with_fallback

"""
This example adapts a strict agent-wrapper audit pattern into the Agents SDK.
It breaks the audit into four structured artifacts:

1. scope
2. evidence pack
3. failure map
4. final report

The goal is to keep the workflow evidence-first and typed end-to-end.
"""


Severity = Literal["critical", "high", "medium", "low"]
AuditPlaybook = Literal[
    "wrapper_regression",
    "memory_contamination",
    "tool_discipline",
    "rendering_transport",
    "hidden_agent_layers",
]


AUDIT_PLAYBOOKS: dict[AuditPlaybook, str] = {
    "wrapper_regression": (
        "Use when the base model looks strong but the wrapped agent behaves worse. "
        "Focus on wrapper layering, duplicated context injection, and orchestration drift."
    ),
    "memory_contamination": (
        "Use when old topics or stale artifacts bleed into current turns. "
        "Focus on memory admission, same-session reentry, and stale retrieval ordering."
    ),
    "tool_discipline": (
        "Use when the agent should have used a tool but skipped it, or used the wrong tool. "
        "Focus on code-enforced tool requirements, preflight probes, and stale evidence binding."
    ),
    "rendering_transport": (
        "Use when the answer seems correct internally but is degraded by delivery or formatting layers. "
        "Focus on payload shape assumptions, fallback rendering, and semantic mutation in transport."
    ),
    "hidden_agent_layers": (
        "Use when hidden retry, recap, summarize, or repair loops mutate results. "
        "Focus on hidden second-pass model calls and undocumented repair behaviors."
    ),
}


AUDIT_RUBRIC = """
Audit rubric:
1. Context cleanliness: detect duplicated context, stale carryover, and same-session artifact reentry.
2. Tool discipline: distinguish prompt-only tool suggestions from code-enforced tool requirements.
3. Failure handling: inspect retries, repair loops, and semantic mutation during fallback.
4. Memory admission: prefer evidence-backed durable facts over assistant self-talk.
5. Answer shaping: check whether final responses are derived from structured evidence or decorative prose.
6. Hidden agent layers: identify recap, repair, summarize, or transport logic behaving like undeclared agents.
7. JSON vs freeform boundary: keep internal orchestration typed and structured whenever possible.

Severity heuristics:
- critical: confidently wrong operational behavior
- high: repeated corruption of otherwise good evidence
- medium: correctness often survives, but the system is fragile or noisy
- low: mostly maintainability or cosmetic concerns
""".strip()


class AuditScope(BaseModel):
    target_name: str
    entrypoints: list[str]
    model_stack: list[str]
    symptoms: list[str]
    time_window: str
    layers_to_audit: list[str]


class EvidenceItem(BaseModel):
    kind: Literal["code", "log", "config", "test", "trace", "session"]
    source: str
    summary: str
    time_scope: Literal["historical", "current", "mixed"]


class EvidencePack(BaseModel):
    evidence_pack: list[EvidenceItem]
    missing_evidence: list[str] = Field(default_factory=list)


class Finding(BaseModel):
    severity: Severity
    title: str
    symptom: str
    source_layer: str
    root_cause: str
    recommended_fix: str


class ConflictEdge(BaseModel):
    from_layer: str
    to_layer: str
    conflict_type: Literal[
        "duplication",
        "contradiction",
        "stale_state",
        "hidden_mutation",
        "freeform_overwrite",
    ]
    note: str


class FailureMap(BaseModel):
    findings: list[Finding]
    conflict_map: list[ConflictEdge] = Field(default_factory=list)


class ExecutiveVerdict(BaseModel):
    overall_health: Literal["critical", "high_risk", "unstable", "acceptable", "strong"]
    primary_failure_mode: str
    most_urgent_fix: str


class OrderedFix(BaseModel):
    order: int
    goal: str
    why_now: str
    expected_effect: str


class AuditReport(BaseModel):
    executive_verdict: ExecutiveVerdict
    findings: list[Finding]
    conflict_map: list[ConflictEdge] = Field(default_factory=list)
    ordered_fix_plan: list[OrderedFix]


@dataclass
class StructuredAuditAgents:
    scope: Agent[AuditScope]
    evidence: Agent[EvidencePack]
    failure_map: Agent[FailureMap]
    report: Agent[AuditReport]


def audit_report_schema() -> dict[str, Any]:
    return AuditReport.model_json_schema()


def build_audit_agents() -> StructuredAuditAgents:
    scope_agent = Agent(
        name="audit_scope_agent",
        instructions=(
            "You define the scope for auditing an agent runtime. "
            "Focus on target system, entrypoints, model stack, symptoms, time window, "
            "and the layers that should be audited."
        ),
        output_type=AuditScope,
    )

    evidence_agent = Agent(
        name="audit_evidence_agent",
        instructions=(
            "You build an evidence pack for an agent-runtime audit. "
            "Prefer direct evidence categories such as code, logs, configs, tests, traces, "
            "or session artifacts. If something important is missing, list it explicitly."
        ),
        output_type=EvidencePack,
    )

    failure_map_agent = Agent(
        name="audit_failure_map_agent",
        instructions=(
            "You diagnose wrapper failures in an agent system. "
            "Look for tool-discipline failures, memory contamination, wrapper regression, "
            "hidden mutation layers, and stale evidence reuse. "
            "Produce severity-ranked findings and a conflict map."
        ),
        output_type=FailureMap,
    )

    report_agent = Agent(
        name="audit_report_agent",
        instructions=(
            "You turn structured audit artifacts into a final report. "
            "Lead with the executive verdict, then severity-ranked findings, then an ordered "
            "fix plan. Prefer code and configuration fixes over prompt-only fixes."
        ),
        output_type=AuditReport,
    )

    return StructuredAuditAgents(
        scope=scope_agent,
        evidence=evidence_agent,
        failure_map=failure_map_agent,
        report=report_agent,
    )


async def run_structured_agent_audit(
    audit_request: str,
    agents: StructuredAuditAgents | None = None,
    playbook: AuditPlaybook = "wrapper_regression",
) -> AuditReport:
    agents = agents or build_audit_agents()
    playbook_guidance = AUDIT_PLAYBOOKS[playbook]
    schema_json = AuditReport.model_json_schema()

    with trace("Structured agent audit"):
        scope_prompt = (
            f"Audit request:\n{audit_request}\n\n"
            f"Playbook: {playbook}\n"
            f"Playbook guidance: {playbook_guidance}"
        )
        scope_result = await Runner.run(agents.scope, scope_prompt)
        scope = scope_result.final_output

        evidence_prompt = (
            "Build an evidence pack for this audit.\n\n"
            f"Audit request:\n{audit_request}\n\n"
            f"Playbook: {playbook}\n"
            f"Playbook guidance: {playbook_guidance}\n\n"
            f"Rubric:\n{AUDIT_RUBRIC}\n\n"
            f"Scope JSON:\n{scope.model_dump_json(indent=2)}"
        )
        evidence_result = await Runner.run(agents.evidence, evidence_prompt)
        evidence = evidence_result.final_output

        failure_prompt = (
            "Build the failure map for this agent audit.\n\n"
            f"Playbook: {playbook}\n"
            f"Playbook guidance: {playbook_guidance}\n\n"
            f"Rubric:\n{AUDIT_RUBRIC}\n\n"
            f"Scope JSON:\n{scope.model_dump_json(indent=2)}\n\n"
            f"Evidence JSON:\n{evidence.model_dump_json(indent=2)}"
        )
        failure_result = await Runner.run(agents.failure_map, failure_prompt)
        failure_map = failure_result.final_output

        report_prompt = (
            "Build the final audit report.\n\n"
            f"Playbook: {playbook}\n"
            f"Playbook guidance: {playbook_guidance}\n\n"
            f"Rubric:\n{AUDIT_RUBRIC}\n\n"
            f"Report JSON schema:\n{json_dumps_pretty(schema_json)}\n\n"
            f"Scope JSON:\n{scope.model_dump_json(indent=2)}\n\n"
            f"Evidence JSON:\n{evidence.model_dump_json(indent=2)}\n\n"
            f"Failure map JSON:\n{failure_map.model_dump_json(indent=2)}"
        )
        report_result = await Runner.run(agents.report, report_prompt)
        return report_result.final_output


def json_dumps_pretty(data: dict[str, Any]) -> str:
    import json

    return json.dumps(data, indent=2, sort_keys=True)


def render_report_summary(report: AuditReport) -> str:
    lines = [
        f"Overall health: {report.executive_verdict.overall_health}",
        f"Primary failure mode: {report.executive_verdict.primary_failure_mode}",
        f"Most urgent fix: {report.executive_verdict.most_urgent_fix}",
        "",
        "Findings:",
    ]
    for finding in report.findings:
        lines.append(f"- [{finding.severity}] {finding.title}: {finding.recommended_fix}")

    lines.append("")
    lines.append("Fix plan:")
    for fix in report.ordered_fix_plan:
        lines.append(f"{fix.order}. {fix.goal} - {fix.expected_effect}")

    return "\n".join(lines)


async def main() -> None:
    audit_request = input_with_fallback(
        "What agent runtime do you want to audit? ",
        (
            "Audit a coding assistant wrapper that became less reliable after adding memory, "
            "tool routing, and answer-shaping layers."
        ),
    )
    report = await run_structured_agent_audit(audit_request)
    print(report.model_dump_json(indent=2))
    print()
    print(render_report_summary(report))


if __name__ == "__main__":
    asyncio.run(main())
