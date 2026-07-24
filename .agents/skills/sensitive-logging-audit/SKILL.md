---
name: sensitive-logging-audit
description: Audit and fix sensitive-data exposure through Python runtime logging in openai-agents-python. Use when reviewing logging, print, warnings, stderr, or traceback output; checking OPENAI_AGENTS_DONT_LOG_MODEL_DATA or OPENAI_AGENTS_DONT_LOG_TOOL_DATA coverage; investigating model, tool, Realtime, MCP, session, sandbox, tracing, or arbitrary exception data in logs; or carrying an audit through fixes, adversarial tests, and repository verification.
---

# Sensitive Logging Audit

## Objective

Complete the remediation, not only the scan. Inventory runtime output sinks, classify every dynamic value, fix every demonstrated model/tool-data leak in scope, add adversarial regression tests, and run the applicable repository gates.

Treat the inventory as a conservative review ledger, not automatic taint analysis. Receiver provenance and policy guards improve prioritization but never prove that a dynamic value is safe.

## Workflow

### 1. Establish the baseline

- Work in the current checkout and preserve unrelated changes.
- Record `git status --short --branch` and the current commit.
- Read `src/agents/_debug.py`, `src/agents/logger.py`, and existing policy-aware helpers before judging call sites.
- Treat exception messages, arguments, tracebacks, causes, contexts, notes, and arbitrary values as potentially sensitive.
- Treat URL-derived display names as structured sensitive values. MCP server names may embed endpoint credentials, query parameters, or fragments even when the log does not contain a model/tool payload object.

Run the detector tests before relying on its output:

```bash
uv run python .agents/skills/sensitive-logging-audit/scripts/test_inventory.py
```

Create a baseline from the repository root:

```bash
uv run python .agents/skills/sensitive-logging-audit/scripts/inventory_logging.py \
  --format json --output /tmp/sensitive-logging-before.json
uv run python .agents/skills/sensitive-logging-audit/scripts/inventory_logging.py \
  --summary-only
```

### 2. Classify every dynamic sink

Review the complete JSON ledger. Prioritize raw output, caught exceptions, `logger.exception`, `exc_info`, `extra`, supplemental formatting arguments, and unknown receivers.

Assign one disposition to every dynamic fingerprint group:

- `model`: model requests, responses, Realtime events, or derived values.
- `tool`: tool arguments, outputs, MCP data, tool events, or derived values.
- `model+tool`: either class may reach the sink.
- `operational`: proven to contain only non-sensitive SDK metadata.
- `intentional-output`: explicitly user-facing output rather than diagnostics.
- `uncertain`: source tracing is incomplete; investigate before deciding.

Record the fingerprint, group count, disposition, evidence, and action. Do not classify from a variable name or message text alone. Trace producers, callbacks, formatters, and exception ownership.

### 3. Fix demonstrated leaks

Before changing runtime behavior, use `$implementation-strategy`. Implement the narrowest shared-boundary fix.

- Apply `_debug.DONT_LOG_MODEL_DATA` and `_debug.DONT_LOG_TOOL_DATA` before formatting or inspecting sensitive values.
- Redact `model+tool` values when either relevant flag disables data logging.
- Preserve existing diagnostics when sensitive-data logging is explicitly enabled.
- In redacted mode, emit a fixed message. Do not inspect or pass the sensitive object through `msg`, `args`, `extra`, or `exc_info`.
- In tool-redacted mode, use fixed MCP lifecycle and filter messages without reading `server.name` or including tool names. In diagnostic mode, sanitize URL-derived server names by removing credentials, query parameters, and fragments while retaining ordinary names. Do not mutate the server's public `name` or connection URL.
- Keep logging failure from changing fallback, cleanup, event emission, rejection, or cancellation behavior.
- Leave operational and intentional user-facing values unchanged when their safety is demonstrated.

### 4. Add adversarial regressions

Read [the Python redaction validation matrix](references/redaction-validation.md) and test every changed caller boundary. Helper-only tests do not prove that callers use the helper correctly.

### 5. Re-audit the whole tree

Run the inventory again and compare it to the baseline:

```bash
uv run python .agents/skills/sensitive-logging-audit/scripts/inventory_logging.py \
  --compare /tmp/sensitive-logging-before.json \
  --format json --output /tmp/sensitive-logging-after.json
```

Inspect every new, removed, count-changed, or classification-changed fingerprint group. Classification changes include policy, shape, sink kind, confidence, method, and caught-value status. Duplicate groups deliberately require group-level re-review instead of inheriting an order-dependent disposition.

The completion report must state total and dynamic sink counts, confirmed leaks fixed, retained candidates and evidence, duplicate groups, verification results, and remaining uncertainty.

### 6. Run close-out gates

- Run the detector tests and skill validation after changing the skill.
- For runtime code, tests, examples, or build/test behavior, use `$code-change-verification` after the final fix.
- Use `$pr-draft-summary` when required by the repository instructions.
- Stop after local changes and verification unless the user explicitly requests a remote action.

## Reporting

Lead with whether real leaks were found and fixed. Separate confirmed leaks from conservative candidates and intentional output. Never equate a clean inventory shape or a recognized policy guard with proof that all dynamic values are non-sensitive.
