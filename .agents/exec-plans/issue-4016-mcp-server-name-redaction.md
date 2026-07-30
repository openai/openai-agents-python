# Redact endpoint secrets from MCP server names

This ExecPlan is a living document. The sections Progress, Surprises & Discoveries, Decision Log, and Outcomes & Retrospective must stay up to date as work proceeds. It follows [PLANS.md](../../PLANS.md).

## Purpose / Big Picture

MCP servers created from HTTP and SSE endpoint URLs use the URL as their default name. Before this change, that raw name could place credentials, query tokens, and fragments in SDK errors, exported traces, and serialized run items. After the change, those public and diagnostic boundaries display only the endpoint scheme, host, and path, while the server retains its raw name for internal routing and tool identity.

## Progress

- [x] (2026-07-30 00:00Z) Read the issue, repository guidance, lifecycle references, and sensitive-data audit guidance.
- [x] (2026-07-30 00:00Z) Created the local branch `fix-4016-mcp-server-redaction` without committing.
- [x] (2026-07-30 00:00Z) Audited every MCP error, tracing, and persisted tool-origin producer that can receive a URL-derived server name.
- [x] (2026-07-30 00:00Z) Patched confirmed public boundaries and added focused adversarial regression tests.
- [x] (2026-07-30 00:00Z) Ran static checks and attempted focused tests and the mandatory runtime verification stack.
- [x] (2026-07-30 00:00Z) Updated this plan with results and prepared the no-commit handoff.

## Surprises & Discoveries

- Observation: The workspace sandbox can read Git metadata but cannot create a branch ref lock.
  Evidence: Creating the requested branch succeeded only after the Git command received approval to write the local ref.

- Observation: The local verification environment cannot execute the test suite.
  Evidence: `uv` cannot parse this repository's `pyproject.toml` and `uv.lock`; the existing `.venv` has MCP 2.x interfaces although `pyproject.toml` requires `mcp<2`; and the Windows verification wrapper cannot find `make`.

## Decision Log

- Decision: Keep `MCPServer.name` unchanged and sanitize only values that leave the local routing layer.
  Rationale: Raw names are part of MCP tool-prefixing and callback identity; changing them would risk tool-name collisions and behavior changes. The existing `get_mcp_server_log_name` helper already recognizes the SDK's URL-derived name prefixes.
  Date/Author: 2026-07-30 / Codex

- Decision: Limit sanitization to SDK-created `sse:`, `streamable_http:`, and `streamable-http:` names.
  Rationale: Arbitrary custom names are user labels, not reliably parseable endpoints. Callers can provide a safe custom name when that label itself contains a secret.
  Date/Author: 2026-07-30 / Codex

- Decision: Keep `ToolOrigin`'s serialized shape unchanged and pass it a sanitized MCP value at its source.
  Rationale: `ToolOrigin` is metadata, not MCP routing state. Changing its JSON schema or the raw server name used to construct tool callables would add unnecessary compatibility and resume risk.
  Date/Author: 2026-07-30 / Codex

## Outcomes & Retrospective

The implementation uses the existing URL sanitizer for SDK errors, manager fallback errors, MCP list-tools and function span data, cancellation errors, converted function-tool origins, and generic invocation errors. The regression tests use URL user-info, query tokens, and a fragment and assert that none survive in error text, trace export, or the serialized tool origin.

Focused Ruff lint and format checks pass, as does `git diff --check`. The sensitive-logging inventory test passed and produced `C:\\tmp\\issue-4016-sensitive-logging.json`; manual audit retained only raw server-name uses needed for tool prefixing and callback context. The focused tests could not collect because the environment's MCP package is incompatible. The mandatory verification wrapper could not start because `make` is unavailable.

## Context and Orientation

`src/agents/mcp/server.py` owns MCP connection and call failures. `src/agents/mcp/util.py` builds function tools and populates tracing metadata. `src/agents/mcp/_logging.py` provides `get_mcp_server_log_name`, which strips URL user-info, query parameters, and fragments from the SDK's URL-derived server-name formats. `src/agents/tool.py` serializes optional `ToolOrigin` metadata into `RunState` items.

The scope contract is:

- Required behavior: A URL-derived MCP name must be sanitized in raised SDK errors, MCP list-tools/function trace data, and serialized tool-origin metadata.
- Compatibility: `MCPServer.name`, MCP tool names, server callbacks, and legacy state containing raw origins retain their existing behavior. Older saved state remains readable.
- Intentionally unsupported: The SDK will not try to detect secrets in arbitrary caller-provided custom labels; applications should set a non-secret label instead.
- Existing alternative: Every MCP server constructor accepts an explicit `name` so applications can set a safe stable label.

## Plan of Work

First enumerate all uses of `MCPServer.name` that become exceptions, trace payloads, or persisted public metadata. Then use the existing sanitization helper at those producers, leaving local identity consumers untouched. Add tests with hostile URL user-info, query, and fragments to prove the raw markers cannot appear in the affected values. Finally run the focused tests and repository verification stack.

## Concrete Steps

From `C:\\Users\\asus\\Desktop\\os\\openai-agents-python`:

    rg -n "self\\.name|server\\.name" src/agents/mcp src/agents/tool.py
    uv run pytest tests/mcp tests/test_tool_origin.py -q
    powershell -ExecutionPolicy Bypass -File .agents/skills/code-change-verification/scripts/run.ps1

The focused tests should confirm that raw credential, query-token, and fragment markers do not occur in errors, trace exports, or `ToolOrigin.to_json_dict()`.

## Validation and Acceptance

Acceptance is met when an unreachable `MCPServerStreamableHttp` endpoint whose URL contains user-info, query, and fragment causes a meaningful error without any of those values; MCP spans and serialized tool origin similarly exclude them; and ordinary named MCP servers preserve their existing public metadata. Formatting, linting, type checking, and the test suite must pass, or an external environment failure must be recorded exactly.

## Idempotence and Recovery

The edits are additive and can be rerun. If a sanitization change would affect internal tool lookup, revert only that call site and keep it using the raw `MCPServer.name`. No branches, commits, remote state, or user data beyond the requested local branch are modified.

## Artifacts and Notes

The branch is local-only: `fix-4016-mcp-server-redaction`. No commit will be created.

## Interfaces and Dependencies

`get_mcp_server_log_name(name: str) -> str` remains the single name-redaction implementation. Public `MCPServer.name` remains raw for compatibility. Error text, tracing fields, and serialized `ToolOrigin.mcp_server_name` contain the sanitized representation when the underlying name is URL-derived.
