# Temporal Sandbox Agent

A conversational coding agent that runs as a durable Temporal workflow with support for multiple sandbox backends (Daytona, Docker, E2B, local unix).

These restored examples cannot currently run with this SDK's `agents.classic` namespace. The pinned `temporalio==1.34.0` plugin imports the root `agents` API. The standalone [`temporalio-openai-agents==1.1.0`](https://pypi.org/project/temporalio-openai-agents/1.1.0/) also imports that API and requires `openai-agents>=0.22,<0.23`. Neither plugin provides an SDK namespace option. Installing `classic-temporal` or changing the plugin import path does not resolve this incompatibility. See [Classic library compatibility](../../../../../CLASSIC.md#stage-the-package-and-import-change).

For a supported setup, use a separate environment with the plugin's declared v0 SDK version range and the [original v0 examples](https://github.com/openai/openai-agents-python/tree/v0.22.3/examples/sandbox/extensions/temporal), which import the root `agents` API. For example, `openai-agents==0.22.3`, `temporalio-openai-agents==1.1.0`, and `temporalio==1.35.0` support that setup. The commands below describe the restored Classic examples; they require a plugin that supports `agents.classic` before they can run in this checkout.

## Quickstart

**Prerequisites:** Docker (for the Docker backend) and API keys for any cloud backends you want to use. The local and Docker sandboxes work without any cloud provider API keys.

## Local smoke test

If you only want to confirm that Temporal workflows run locally, use the minimal
example first:

```
export OPENAI_API_KEY="sk-..."
# Optional: export EXAMPLES_TEMPORAL_MODEL="gpt-5.4-mini"
# Optional: export EXAMPLES_TEMPORAL_TRACE="openai"
uv run --extra classic-temporal python -m examples.classic.sandbox.extensions.temporal.local_hello_workflow
```

This starts the Temporal Python SDK test server, runs one workflow and one model activity, connects the workflow to a local Unix sandbox, and then shuts down. It does not require the Temporal CLI, an already running Temporal dev server, or sandbox backend credentials.

The local smoke test enables OpenAI Agents tracing by default. Set `EXAMPLES_TEMPORAL_TRACE=none` to disable tracing, or `EXAMPLES_TEMPORAL_TRACE=openai_with_temporal_spans` to also ask the Temporal plugin to add Temporal spans. The Temporal span mode depends on Temporal plugin behavior and may omit regular Agents spans with some plugin versions; use the default `openai` mode when you want standard OpenAI trace spans.

1. Install [just](https://just.systems/man/en/packages.html) and the [Temporal CLI](https://docs.temporal.io/cli/setup-cli#install-the-cli) if you don't have them already.

2. Change into the example directory:

   ```
   cd examples/classic/sandbox/extensions/temporal
   ```

3. Create a `.env` file in this directory with your API keys:

   ```
   OPENAI_API_KEY="sk-..."
   DAYTONA_API_KEY="dtn_..."   # optional, for Daytona backend
   E2B_API_KEY="e2b_..."       # optional, for E2B backend
   ```

4. Start the Temporal dev server:

   ```
   just temporal
   ```

5. In a second terminal, start the worker:

   ```
   just worker
   ```

6. In a third terminal, start the TUI:

   ```
   just tui
   ```

The `just worker` and `just tui` commands automatically install dependencies before starting.

## TUI commands

| Command            | Description                                            |
|--------------------|--------------------------------------------------------|
| `/switch`          | Switch the current session to a different sandbox backend |
| `/fork [title]`    | Fork the session onto a (possibly different) backend   |
| `/title <name>`    | Rename the current session                             |
| `/done`            | Exit the TUI                                           |

Both `/switch` and `/fork` open an interactive backend picker. When switching to the local backend you can specify the workspace root directory.

## How it works

A single Temporal worker registers all sandbox backends via `SandboxClientProvider`, so every backend's activities are available on one task queue. The workflow picks which backend to target each turn by calling `temporal_sandbox_client(name)` in its `RunConfig`.

**Files:**

- `temporal_sandbox_agent.py` -- The `AgentWorkflow` definition and worker entrypoint. Each conversation turn calls `Runner.run()` with a `SandboxRunConfig` that targets the active backend. The workflow is
  long-lived: it idles between turns and persists indefinitely in Temporal.
- `temporal_session_manager.py` -- A singleton `SessionManagerWorkflow` that tracks active sessions and handles create, fork, switch, and destroy operations.
- `temporal_sandbox_tui.py` -- A [Textual](https://textual.textualize.io/) TUI that connects to the session manager and drives conversations via signals, updates, and queries.
- `examples/classic/sandbox/misc/workspace_shell.py` -- A shared `Capability` that gives the agent a shell tool for running commands in the sandbox workspace.

**Switching backends** is an in-place operation: the workflow receives a `switch_backend` update, changes its backend and manifest, clears the backend-specific session state, and the next turn creates a fresh session on the new backend. The portable snapshot is preserved so workspace files carry over.

**Forking** pauses the source workflow, snapshots its state and conversation history, and starts a new child workflow on the chosen backend. The fork gets an independent copy of the workspace and conversation.
