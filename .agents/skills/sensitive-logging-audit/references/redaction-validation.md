# Python sensitive logging validation

The inventory is deliberately broader than a vulnerability detector. It finds confirmed SDK logger calls, raw output, exact policy-aware helpers, and unknown receivers with logging-like method names. Review every dynamic finding; do not treat a recognized receiver, helper, or guard as proof that its values are safe.

## Required validation matrix

Test every changed sensitive caller boundary in both redacted and diagnostic modes. Use a unique sentinel for each source and inspect both rendered output and the complete `LogRecord`.

| Case | Model flag | Tool flag | Value | Required assertion |
| --- | --- | --- | --- | --- |
| Model redaction | on | off | `Exception(secret)` | No sentinel or exception object remains in the record |
| Tool redaction | off | on | `Exception(secret)` | No sentinel or exception object remains in the record |
| Both redacted | on | on | model and tool values | Neither sentinel remains anywhere in the record |
| Diagnostic mode | off | off | ordinary exception | Existing diagnostic detail and traceback behavior remain |
| Hostile string | applicable | applicable | object whose `__str__` raises or returns a secret | Logging does not fail or reveal the secret |
| Hostile repr | applicable | applicable | object whose `__repr__` raises or returns a secret | Logging does not fail or reveal the secret |
| Hostile class access | applicable | applicable | exception overriding `__getattribute__` | Redacted logging does not inspect the exception |
| Exception chain | applicable | applicable | `__cause__`, `__context__`, notes, or `ExceptionGroup` containing secrets | No chained secret is attached or rendered |
| Supplemental arguments | applicable | applicable | fixed message plus secret formatting argument | Formatting arguments are omitted in redacted mode |
| Extra payload | applicable | applicable | `extra={"detail": secret}` | Secret `LogRecord` attributes are omitted |
| Traceback payload | applicable | applicable | `exc_info=True` or an exception tuple | `exc_info` and `exc_text` are absent in redacted mode |
| MCP server or tool name | tool | on | path token or custom-name sentinel | Log uses a fixed message and does not read or attach the name |
| URL-derived MCP name | tool | off | URL credentials, query, and fragment | Log retains only scheme, host, port, and path; the runtime value is unchanged |

Also test the observable caller behavior after logging. Redaction is incorrect if it prevents a fallback result, cleanup, event emission, rejection, or cancellation from completing.

## Inspect the full LogRecord

Do not assert only against `caplog.text` or a mock call converted to a string. In redacted mode, inspect at least:

- `record.msg`
- `record.args`
- `record.exc_info`
- `record.exc_text`
- values added through `record.__dict__`
- the final output of a real `logging.Formatter`

The sensitive object itself must not remain attached even when its string representation is absent. A custom handler or exporter may inspect raw record fields.

## Review procedure

1. Run the inventory against all of `src/agents`.
2. Review raw output and unknown receivers first.
3. Review caught values, `logger.exception`, `exc_info`, `extra`, and formatting arguments.
4. Trace model, tool, Realtime, MCP, session, sandbox, voice, tracing, and cleanup values to their producers.
5. Classify intentional output separately from diagnostics; do not silently exempt `print` or warnings.
6. Add focused tests at every changed caller boundary.
7. Re-run the inventory and compare fingerprint groups to the baseline.
8. Re-review any duplicate group whose count changed.

The detector supports a completeness claim for the repository's direct Python output shapes. It does not prove arbitrary runtime data flow, dynamically installed handlers, monkey-patched logger methods, or reflective calls.

## Review ledger shape

Use a temporary JSON ledger when the audit is large:

```json
{
  "reviews": [
    {
      "group_fingerprint": "0123456789ab",
      "group_count": 1,
      "disposition": "tool",
      "evidence": "The value originates from the function tool input JSON.",
      "action": "Guard formatting and arguments with DONT_LOG_TOOL_DATA."
    }
  ]
}
```

Validate it with:

```bash
uv run python .agents/skills/sensitive-logging-audit/scripts/inventory_logging.py \
  --validate-review /tmp/sensitive-logging-review.json --summary-only
```

Allowed dispositions are `model`, `tool`, `model+tool`, `operational`, `intentional-output`, and `uncertain`.
Both `evidence` and `action` must be non-empty JSON strings; structured values do not count as review rationale.
