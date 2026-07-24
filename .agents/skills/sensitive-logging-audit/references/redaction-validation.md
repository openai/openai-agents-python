# Python sensitive logging validation

The collector reports syntactic logging and raw-output candidates. It does not resolve aliases, prove receiver types, evaluate guards, classify payloads, or support a completeness claim. Review candidates together with direct source searches and runtime tests.

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

1. Run the collector against all of `src/agents`.
2. Run the supplemental `rg` searches from `SKILL.md` and inspect aliases and dynamic dispatch.
3. Review raw output and ambiguous receivers first.
4. Review caught values, `logger.exception`, `exc_info`, `extra`, and formatting arguments.
5. Trace model, tool, Realtime, MCP, session, sandbox, voice, tracing, and cleanup values to their producers.
6. Classify intentional output separately from diagnostics; do not silently exempt `print` or warnings.
7. Add focused tests at every changed caller boundary.
8. Re-run the collector and source searches after the fix.

An empty or unchanged collector report is not proof of safety. Assignment aliases, monkey-patched methods, dynamically installed handlers, non-constant reflection, and arbitrary runtime data flow require manual inspection.

## Audit report expectations

For each confirmed or uncertain path, record:

- The source location and value producer.
- The manual disposition: `model`, `tool`, `model+tool`, `operational`, `intentional-output`, or `uncertain`.
- Concrete evidence for the disposition.
- The fix or reason for retaining the path.
- The caller-level regression test, when behavior changed.

Do not reuse a disposition solely because a fingerprint or call text is unchanged.
