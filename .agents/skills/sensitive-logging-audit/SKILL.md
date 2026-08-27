---
name: sensitive-logging-audit
description: Audit and fix sensitive-data exposure through Python runtime logging in openai-agents-python. Use when reviewing logging, print, warnings, stderr, traceback, MCP names, model or tool exceptions, redaction flags, or any diagnostic path that may retain user data.
---

# Sensitive Logging Audit

## Objective

Find candidate output sinks, trace their values manually, fix demonstrated leaks at shared runtime boundaries, and prove redaction with adversarial tests.

The collector is only a syntax-based search aid. It does not resolve Python aliases or control flow, certify policy guards, or prove that an absent candidate is safe.

## Workflow

### 1. Establish the review surface

- Work in the current checkout and preserve unrelated changes.
- Read `src/agents/_debug.py`, `src/agents/logger.py`, and the affected callers.
- Treat exception messages, arguments, tracebacks, causes, contexts, notes, names, URLs, and arbitrary values as potentially sensitive.
- Read [the Python redaction validation matrix](references/redaction-validation.md).

Run the collector tests, then collect candidates:

```bash
uv run python .agents/skills/sensitive-logging-audit/scripts/test_inventory.py
uv run python .agents/skills/sensitive-logging-audit/scripts/inventory_logging.py \
  --format json --output /tmp/sensitive-logging-candidates.json
```

The report intentionally contains no `policy`, `safe`, or guard classification.

### 2. Supplement the collector with source search

The collector does not follow assignments such as `emit = logger.error`. Search the source directly and inspect aliases, callbacks, wrappers, and reflective dispatch:

```bash
rg -n '\.(debug|info|warning|warn|error|exception|critical|fatal|log)\b' src/agents
rg -n '\b(print|pprint|pp|warn|warn_explicit|write|writelines|print_exc|print_exception)\b' src/agents
rg -n 'DONT_LOG_(MODEL|TOOL)_DATA|log_(model|tool|model_and_tool)_action' src/agents
```

Do not turn collector coverage or a textual guard into a security conclusion. Trace producers and callers.

### 3. Classify manually

Assign each reviewed path one disposition:

- `model`: model requests, responses, Realtime events, or derived values.
- `tool`: tool arguments, outputs, MCP data, tool events, or derived values.
- `model+tool`: either class may reach the sink.
- `operational`: demonstrated to contain only non-sensitive SDK metadata.
- `intentional-output`: explicitly user-facing output rather than diagnostics.
- `uncertain`: source tracing is incomplete.

Record evidence in the audit report. The script does not validate or inherit dispositions.

### 4. Fix runtime boundaries

Before changing runtime behavior, use `$implementation-strategy`.

- Check the relevant `_debug.DONT_LOG_MODEL_DATA` and `_debug.DONT_LOG_TOOL_DATA` flags before formatting or inspecting sensitive values.
- Redact mixed model/tool values when either flag disables data logging.
- In redacted mode, emit a fixed message and omit sensitive `args`, `extra`, and `exc_info`.
- Build diagnostic-only context lazily so redacted mode never reads it.
- Preserve useful diagnostics when sensitive-data logging is explicitly enabled.
- Keep logging failure from changing fallback, cleanup, event, rejection, or cancellation behavior.
- For MCP URLs, remove credentials, query parameters, and fragments in diagnostic mode; never use sanitized names as a substitute for fixed redacted messages.

### 5. Prove caller behavior

Add tests at every changed caller boundary. Inspect the complete `LogRecord`, not only rendered text. Test both redacted policies, diagnostic mode, hostile objects, exception chains, and the caller's observable fallback or cleanup behavior as applicable.

### 6. Re-run and close out

Re-run the collector, the manual searches, focused tests, and applicable repository gates. Use `$code-change-verification` for runtime or test changes and `$pr-draft-summary` when required.

Report candidate counts as search coverage only. Lead with confirmed leaks fixed, retained intentional output, reviewed uncertainty, and verification results. Never report a clean collector result as proof that no sensitive logging path exists.
