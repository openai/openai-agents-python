# Structured Agent Audits

This guide shows how to build evidence-first runtime audits with the Agents SDK using typed,
multi-stage workflows instead of a single freeform review prompt.

The reference implementation lives in:

- `examples/agent_patterns/structured_agent_audit.py`
- `examples/agent_patterns/structured_agent_audit_with_guardrails.py`

## Why use this pattern

This pattern is useful when you are not trying to solve the user's domain task, but instead need to
inspect the agent system itself.

Typical cases include:

- wrapper regression, where a wrapped agent behaves worse than the underlying model
- tool-discipline failures, where the agent should have used a tool but skipped it
- stale evidence reuse, where old observations reappear as if they were current
- hidden mutation layers, where retry/repair/rendering logic changes otherwise good answers
- memory contamination, where recalled or distilled content pollutes later turns

## Core idea

Keep the workflow typed from end to end. Instead of letting the model improvise a diagnosis in one
pass, break the audit into structured artifacts:

1. `AuditScope`
2. `EvidencePack`
3. `FailureMap`
4. `AuditReport`

Each stage narrows ambiguity for the next one.

## Included guidance layers

The example also carries three reusable guidance layers:

- **Playbooks**: choose the failure mode you want to focus on
- **Rubric**: define what the audit should inspect
- **Schema**: make the final report shape explicit and machine-readable

## Available playbooks

The example currently includes:

- `wrapper_regression`
- `memory_contamination`
- `tool_discipline`
- `rendering_transport`
- `hidden_agent_layers`

Use playbooks when you already know the dominant failure surface and want the audit to start with a
clear lens.

## Basic usage

```python
from examples.agent_patterns.structured_agent_audit import run_structured_agent_audit

report = await run_structured_agent_audit(
    "Audit an agent runtime that started skipping tools after memory and answer-shaping layers were added.",
    playbook="wrapper_regression",
)

print(report.model_dump_json(indent=2))
```

## Combining with tracing and guardrails

If you want the audit itself to be observable and enforceable, use the guardrailed variant:

```python
from examples.agent_patterns.structured_agent_audit_with_guardrails import (
    build_guardrailed_audit_agents,
)
from examples.agent_patterns.structured_agent_audit import run_structured_agent_audit
from agents import trace

agents = build_guardrailed_audit_agents()

with trace(
    "Structured agent audit with guardrails",
    metadata={"playbook": "tool_discipline"},
):
    report = await run_structured_agent_audit(
        "Audit an agent that answers without fresh tool evidence.",
        agents=agents,
        playbook="tool_discipline",
    )
```

The guardrail in that variant trips when the report contains high-severity findings without a
matching fix plan.

## Example output shape

The final report is intentionally structured for both humans and machines:

```json
{
  "executive_verdict": {
    "overall_health": "high_risk",
    "primary_failure_mode": "Wrapper layers amplify stale evidence and skip fresh probes.",
    "most_urgent_fix": "Move tool-discipline guarantees into code."
  },
  "findings": [
    {
      "severity": "critical",
      "title": "Prompt-only tool enforcement",
      "symptom": "The wrapper answers without fresh tool evidence.",
      "source_layer": "tool_selection",
      "root_cause": "Tool use is encouraged in prompts but not enforced in code.",
      "recommended_fix": "Require tool execution before final answer generation for operational questions."
    }
  ],
  "conflict_map": [
    {
      "from_layer": "active_recall",
      "to_layer": "tool_selection",
      "conflict_type": "stale_state",
      "note": "Recalled stale facts bias tool choice away from fresh probes."
    }
  ],
  "ordered_fix_plan": [
    {
      "order": 1,
      "goal": "Enforce fresh probes in code",
      "why_now": "This removes the highest-confidence failure mode first.",
      "expected_effect": "Operational answers stop bypassing live evidence."
    }
  ]
}
```

## Design guidance

This pattern works best when:

- evidence collection is explicit, not implied
- severity is part of the structured output
- fix ordering is included in the report itself
- code/config controls are preferred over prompt-only controls
- the final prose summary is rendered from typed data rather than generated independently

## Where to extend it

Good next steps include:

- adding domain-specific evidence sources
- attaching trace or run identifiers to each evidence item
- enriching the report schema with confidence scores or contradictory evidence
- connecting the report to downstream remediation workflows
