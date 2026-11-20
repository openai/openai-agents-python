# Local code execution with either provider

This example runs a local Python 3 interpreter through an SDK shell tool, streams tool events, and returns a structured numeric answer. It checks the command output and the final value independently.

## Setup and run

Use the repository's Python setup and install Python 3 on the execution host. The default provider uses `OPENAI_API_KEY`. For `--codex`, complete the [Codex provider setup](../../docs/models/codex.md). Optional `OPENAI_MODEL` and `CODEX_MODEL` variables select the model for each mode.

From the repository root:

```sh
uv run python -m examples.tools.local_code_execution
uv run python -m examples.tools.local_code_execution --codex
```

Both commands ask the model to run Python through the SDK's `exec_command` tool. They print `Verified local computation.` only after observing a tool call, a matching numeric tool result, and a matching structured final answer.

## Execution environment

`UnixLocalSandboxClient` runs commands as your current user. Use this example on a trusted, disposable development host. The example closes and deletes the temporary session on success and failure.

The [hosted code-interpreter example](./code_interpreter.py) uses a Responses API managed container and emits `code_interpreter_call` events. This example emits SDK shell-tool events. It does not provide hosted container IDs, persistence, or the hosted service's isolation.
