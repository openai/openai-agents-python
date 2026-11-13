# Healthcare support

This demo answers synthetic healthcare billing and coverage questions. A Classic orchestrator checks local records, asks a sandbox agent to read policy documents and write support artifacts, and can request a simulated human handoff.

## Run the CLI demo

Complete [sandbox setup](../README.md), including Classic source installation and `OPENAI_API_KEY`. This demo uses Unix-local execution: commands run as host processes, with no OS-level confinement on Linux. Use trusted synthetic inputs in a trusted development environment or provide external isolation.

From the repository root:

```bash
uv run python examples/classic/sandbox/healthcare_support/main.py
```

Useful options:

```bash
uv run python examples/classic/sandbox/healthcare_support/main.py --list-scenarios
uv run python examples/classic/sandbox/healthcare_support/main.py --scenario blue_cross_pt_benefits
uv run python examples/classic/sandbox/healthcare_support/main.py --scenario messy_ambiguous_knee_case
uv run python examples/classic/sandbox/healthcare_support/main.py --reset-memory
```

`--reset-memory` deletes the shared `.cache/healthcare_support/sessions.db` database before starting another scenario. It clears history for all demo scenarios.

For unattended runs, set `EXAMPLES_INTERACTIVE_MODE=auto` to auto-answer prompts, including approval of the simulated human handoff:

```bash
EXAMPLES_INTERACTIVE_MODE=auto uv run python examples/classic/sandbox/healthcare_support/main.py --scenario messy_ambiguous_knee_case
```

After a run, the terminal prints a trace URL, the patient-facing response, internal summary, next step, artifact paths, and memory recap. The next scenario reuses the shared SQLite history unless you reset it.

## Scenarios

The built-in scenarios increase in complexity:

- `eligibility_verification_basic` checks a straightforward benefits question.
- `referral_status_check` adds a referral lookup.
- `blue_cross_pt_benefits` shows a follow-up turn that benefits from the shared SQLite memory.
- `prior_auth_confusion_ct` focuses on prior-authorization and intake-routing confusion.
- `billing_coverage_clarification` combines benefits lookup with sandbox policy search and document generation.
- `messy_ambiguous_knee_case` triggers the human approval flow before queueing a handoff.

<a id="what-this-example-demonstrates"></a>

## Architecture

The workflow combines two agents:

1. A standard orchestrator runs in Classic Runner, calls the benefits subagent, asks the sandbox policy agent for evidence, and decides whether to request a human handoff.
2. A sandbox policy agent uses `agents.classic.sandbox` to read mounted case files and policies. Its `Shell`, `Filesystem`, and lazy-loaded `Skills` capabilities support policy search and Markdown artifact creation. It returns a structured policy summary to the orchestrator.

The orchestrator returns a structured case resolution. A shared `SQLiteSession` preserves history across scenario runs, and SDK tracing records the model and tool calls.

Fixture paths below are relative to this example directory: `data/scenarios/*.json`, `data/fixtures/*.json`, and `policies/*.md`. Generated artifacts are copied to `.cache/healthcare_support/output/<scenario_id>/` under the repository root.

## Files to read first

- [`main.py`](main.py) runs the standalone CLI demo.
- [`workflow.py`](workflow.py) contains the shared workflow execution logic, sandbox setup, artifact copying, tracing, and approval resume loop.
- [`support_agents.py`](support_agents.py) defines the orchestrator, benefits subagent, sandbox policy agent, and memory recap agent.
- [`tools.py`](tools.py) defines the local lookup tools and the approval-gated human handoff tool.
- [`skills/prior-auth-packet-builder/SKILL.md`](skills/prior-auth-packet-builder/SKILL.md) is the sandbox skill loaded at runtime.

## Notes

- This is a demo workflow, not a production healthcare system.
- All patient, payer, and policy data in this example is synthetic.
- The example loads environment defaults from the repository-root `.env` file and from this demo's optional local `.env` file.
