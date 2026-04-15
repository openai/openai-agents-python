# Healthcare support

This example shows how to build a healthcare support workflow with Agents SDK using both
standard agents and a sandbox agent. The scenario is intentionally synthetic and generic: a patient
asks a billing or coverage question, the workflow checks local records, inspects policy documents in
an isolated sandbox workspace, writes support artifacts, and optionally routes one ambiguous case to
a human reviewer.

## What this example demonstrates

- **Standard agent orchestration** with a top-level support orchestrator and a benefits subagent.
- **Sandbox agents** with a mounted workspace, shell commands, a generated output folder, and
  runtime-selected sandbox config.
- **Sandbox capabilities** including `Shell`, `Filesystem`, and lazy-loaded `Skills`.
- **Human-in-the-loop approvals** using an approval-gated queue-routing tool.
- **Persistent memory** with `SQLiteSession`, shared across scenario runs.
- **Structured outputs** for each specialist agent and the final case resolution.
- **Tracing** so you can inspect every model call and tool call in the OpenAI trace viewer.
- **CLI-first workflow** that can be run scenario by scenario from the repository checkout.

## Architecture

The workflow has two execution modes working together:

1. A **standard orchestrator agent** runs in the normal Agents SDK loop, calls the benefits
   subagent first, then calls a sandbox agent tool, and decides whether to request a human handoff.
2. A **sandbox policy agent** runs behind `agents.sandbox`, reads the mounted case files and policy
   documents, uses shell commands plus a lazily loaded skill, writes markdown artifacts into
   `output/`, and returns a structured policy summary.

The local fixture data lives in `data/scenarios/*.json` and `data/fixtures/*.json`. The sandbox
policy library lives in `policies/*.md`. Generated artifacts are copied to
`.cache/healthcare_support/output/<scenario_id>/`.

## Scenarios

The built-in scenarios increase in complexity:

- `eligibility_verification_basic` checks a straightforward benefits question.
- `referral_status_check` adds a referral lookup.
- `blue_cross_pt_benefits` shows a follow-up turn that benefits from the shared SQLite memory.
- `prior_auth_confusion_ct` focuses on prior-authorization and intake-routing confusion.
- `billing_coverage_clarification` combines benefits lookup with sandbox policy search and document
  generation.
- `messy_ambiguous_knee_case` triggers the human approval flow before queueing a handoff.

## Run the CLI demo

From the repository root:

```bash
uv run python examples/sandbox/healthcare_support/main.py
```

Useful options:

```bash
uv run python examples/sandbox/healthcare_support/main.py --list-scenarios
uv run python examples/sandbox/healthcare_support/main.py --scenario blue_cross_pt_benefits
uv run python examples/sandbox/healthcare_support/main.py --scenario messy_ambiguous_knee_case
uv run python examples/sandbox/healthcare_support/main.py --reset-memory
```

For unattended runs, set `EXAMPLES_INTERACTIVE_MODE=auto` to auto-answer prompts:

```bash
EXAMPLES_INTERACTIVE_MODE=auto uv run python examples/sandbox/healthcare_support/main.py --scenario messy_ambiguous_knee_case
```

## Files to read first

- [`main.py`](./main.py) runs the standalone CLI demo.
- [`workflow.py`](./workflow.py) contains the shared workflow execution logic, sandbox setup,
  artifact copying, tracing, and approval resume loop.
- [`support_agents.py`](./support_agents.py) defines the orchestrator, benefits subagent, sandbox
  policy agent, and memory recap agent.
- [`tools.py`](./tools.py) defines the local lookup tools and the approval-gated human handoff tool.
- [`skills/prior-auth-packet-builder/SKILL.md`](./skills/prior-auth-packet-builder/SKILL.md) is the
  sandbox skill loaded at runtime.

## Notes

- This is a demo workflow, not a production healthcare system.
- All patient, payer, and policy data in this example is synthetic.
- The example loads environment defaults from the repository-root `.env` file and from this demo's
  optional local `.env` file.
