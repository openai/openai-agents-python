# Cloud Sandbox Extension Examples

These examples are for manual verification of the cloud sandbox backends that
live under `agents.extensions.sandbox`.

They intentionally keep the flow simple:

1. Build a tiny manifest in memory.
2. Create a `SandboxAgent` that inspects that workspace through one shell tool.
3. Run the agent against either E2B or Modal.

Both examples require `OPENAI_API_KEY`, because they call the model through the
normal `Runner` path.

## E2B

### Setup

Install the repo extra:

```bash
uv sync --extra e2b
```

Create an E2B account, create an API key, and export it as `E2B_API_KEY`.
The official setup docs are:

- <https://e2b.dev/docs/api-key>
- <https://e2b.dev/docs/quickstart>

Export the required environment variables:

```bash
export OPENAI_API_KEY=...
export E2B_API_KEY=...
```

### Run

```bash
uv run python examples/sandbox/extensions/e2b_runner.py --stream
```

Useful flags:

- `--sandbox-type e2b_code_interpreter_async`
- `--template <template-name>`
- `--timeout 300`
- `--pause-on-exit`

The example defaults to `e2b_code_interpreter_async`, which matches the async
Code Interpreter backend supported by this repo.

## Modal

If you want the same explicit session lifecycle shown in
`examples/sandbox/basic.py`, that example now accepts
`--backend modal` and reuses the same streamed tool-output flow:

```bash
uv run python examples/sandbox/basic.py \
  --backend modal
```

The dedicated script below stays as the smaller extension-specific example.

### Setup

Install the repo extra:

```bash
uv sync --extra modal
```

Authenticate Modal with either CLI token setup or environment variables. The
official references are:

- <https://modal.com/docs/reference/cli/token>
- <https://modal.com/docs/reference/modal.config>
- <https://modal.com/docs/guide/sandbox>

If you want to configure credentials directly from the CLI:

```bash
uv run modal token set --token-id <token-id> --token-secret <token-secret>
```

Or export environment variables for the current shell:

```bash
export OPENAI_API_KEY=...
export MODAL_TOKEN_ID=...
export MODAL_TOKEN_SECRET=...
```

### Run

```bash
uv run python examples/sandbox/extensions/modal_runner.py \
  --app-name openai-agents-python-sandbox-example \
  --stream
```

Useful flags:

- `--workspace-persistence tar`
- `--workspace-persistence snapshot_filesystem`
- `--sandbox-create-timeout-s 60`

`app_name` is required by `ModalSandboxClientOptions`, so the example makes it
an explicit CLI flag instead of hiding it.

## What to expect

Each script asks the model to inspect a small workspace and summarize it. A
successful run should:

1. Start the chosen cloud sandbox backend.
2. Materialize the manifest into the sandbox workspace.
3. Call the shell tool at least once.
4. Print either streamed text or a final short answer about the workspace.

These examples are not live-validated in CI because they depend on external
cloud credentials, but they are shaped so contributors can verify backend
behavior locally with one command per provider.
