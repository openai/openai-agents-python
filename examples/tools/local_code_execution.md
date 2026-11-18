# Local code execution with either provider

This companion has been using a local Python 3 interpreter to solve the same kind of numeric problem as the hosted code-interpreter example. It has been streaming tool events and returning a structured answer. It has been checking the actual command output and final numeric answer independently.

## Setup and run

Use the repository's normal Python setup. The default provider has been using `OPENAI_API_KEY`. Codex mode has been using the stock CLI's authentication and configuration; install Codex and complete its normal authentication first. Optional `OPENAI_MODEL` and `CODEX_MODEL` variables have been selecting the model for each mode.

From the repository root:

```bash
uv run python -m examples.tools.local_code_execution
uv run python -m examples.tools.local_code_execution --codex
```

Both commands have been asking the model to run Python through the SDK's `exec_command` tool. They have been printing `Verified local computation.` only after observing a tool call, a matching numeric tool result, and a matching structured final answer. No extra service account or hosted code-interpreter container has been participating.

## Execution boundary

`UnixLocalSandboxClient` has been running commands directly as your current user. It has not been providing OS isolation. Use this example only on a trusted, disposable development host. The example has been closing and deleting the temporary session on success and failure.

The original [hosted code-interpreter example](./code_interpreter.py) has been using the Responses API's managed container and emitting `code_interpreter_call` events. This companion has been emitting SDK shell-tool events instead; it has not been emulating hosted container IDs or that API's persistence and isolation.
