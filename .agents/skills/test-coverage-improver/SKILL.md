---
name: test-coverage-improver
description: Measure Python SDK coverage or address measured coverage gaps. Use for coverage audits and metric regressions, not routine test additions.
---

# Test Coverage Improver

Use current coverage evidence to identify missing caller-visible behavior tests. Select this workflow for coverage measurement, coverage-metric regressions, or finding gaps from coverage artifacts. When the user already specifies the behaviors to test, use the ordinary implementation/review workflow without coverage measurement unless measurement is also requested. Adding tests alone does not trigger this skill or its final `make coverage` step.

## Scope and authorization

For assessment or proposal-only requests, report gaps and suggested tests without editing. When the user requests test improvements or has approved a plan, implement the scoped tests and complete review and verification without asking again. Ask only when a new contract decision, expanded scope, or additional authority is needed. Coverage work never authorizes live API calls or broader sandbox access.

## Workflow

1. Inspect current `.coverage`, `coverage.xml`, and any recorded command/environment evidence. Reuse them only when they represent the relevant source and test state. If missing or stale, run `make coverage` under the repository's verification sandbox and credential policy; this initial measurement precedes implementation review. Respect host-capacity guidance before broad measurement.
2. Identify uncovered behavior within the requested scope. Prioritize public behavior and meaningful error, cancellation, and lifecycle paths over percentage-only targets. Use `uv run coverage report -m` or `coverage.xml` to locate gaps.
3. For an assessment, report evidence and proposed scenarios. For authorized implementation, choose tests with independent expected results at the highest controllable caller boundary. Do not add tests that merely reproduce helper logic or enumerate unsupported permutations.
4. Implement tests, run affected checks, and complete `$implementation-final-review`. Keep iterative verification focused; do not repeatedly run full coverage while review is incomplete.
5. After clean review, run `make coverage` for the final measurement and `$code-change-verification` for the required SDK gates. These have different purposes; reuse an already valid final measurement rather than repeating it. Report coverage changes, verified behaviors, and material gaps that remain.

## Reporting

State the scope and age of coverage evidence, the behavior each new test protects, validation results, and unresolved gaps. Keep comments and code in English. Do not treat coverage percentages alone as proof of correctness.
