# Temporal Sandbox Agent

A conversational coding agent that runs as a durable Temporal workflow with
support for multiple sandbox backends (Daytona, Docker, E2B, local unix).

## Quickstart

**Prerequisites:** Docker (for the Docker backend) and API keys for any
cloud backends you want to use. The local and Docker sandboxes work without
any cloud provider API keys.

1. Install [just](https://just.systems/man/en/packages.html) and the
   [Temporal CLI](https://docs.temporal.io/cli/setup-cli#install-the-cli)
   if you don't have them already.

2. Change into the example directory:

   ```
   cd examples/sandbox/extensions/temporal
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

The `just worker` and `just tui` commands automatically install dependencies
before starting.

## TUI commands

| Command            | Description                                            |
|--------------------|--------------------------------------------------------|
| `/switch`          | Switch the current session to a different sandbox backend |
| `/fork [title]`    | Fork the session onto a (possibly different) backend   |
| `/title <name>`    | Rename the current session                             |
| `/done`            | Exit the TUI                                           |

Both `/switch` and `/fork` open an interactive backend picker. When switching
to the local backend you can specify the workspace root directory.

## How it works

A single Temporal worker registers all sandbox backends via
`SandboxClientProvider`, so every backend's activities are available on one
task queue. The workflow picks which backend to target each turn by calling
`temporal_sandbox_client(name)` in its `RunConfig`.

**Files:**

- `temporal_sandbox_agent.py` -- The `AgentWorkflow` definition and worker
  entrypoint. Each conversation turn calls `Runner.run()` with a
  `SandboxRunConfig` that targets the active backend. The workflow is
  long-lived: it idles between turns and persists indefinitely in Temporal.
- `temporal_session_manager.py` -- A singleton `SessionManagerWorkflow` that
  tracks active sessions and handles create, fork, switch, and destroy
  operations.
- `temporal_sandbox_tui.py` -- A [Textual](https://textual.textualize.io/) TUI
  that connects to the session manager and drives conversations via signals,
  updates, and queries.
- `examples/sandbox/misc/workspace_shell.py` -- A shared `Capability` that
  gives the agent a shell tool for running commands in the sandbox workspace.

**Switching backends** is an in-place operation: the workflow receives a
`switch_backend` update, changes its backend and manifest, clears the
backend-specific session state, and the next turn creates a fresh session on
the new backend. The portable snapshot is preserved so workspace files carry
over.

**Forking** pauses the source workflow, snapshots its state and conversation
history, and starts a new child workflow on the chosen backend. The fork gets
an independent copy of the workspace and conversation.
