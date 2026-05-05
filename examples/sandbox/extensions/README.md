# Cloud Sandbox Extension Examples

These examples are for manual verification of the cloud sandbox backends that
live under `agents.extensions.sandbox`.

They intentionally keep the flow simple:

1. Build a tiny manifest in memory.
2. Create a `SandboxAgent` that inspects that workspace through one shell tool.
3. Run the agent against E2B, Modal, Daytona, Cloudflare, Runloop, Blaxel, or Vercel.

All of these examples require `OPENAI_API_KEY`, because they call the model through the normal
`Runner` path. Each cloud backend also needs its own provider credentials.

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

- `--sandbox-type e2b_code_interpreter`
- `--template <template-name>`
- `--timeout 300`
- `--pause-on-exit`

The example defaults to `e2b`, which provides a bash-style interface.
Use `e2b_code_interpreter` for a Jupyter-style interface.

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
- `--workspace-persistence snapshot_directory`
- `--sandbox-create-timeout-s 60`
- `--native-cloud-bucket-secret-name my-modal-secret`

`app_name` is required by `ModalSandboxClientOptions`, so the example makes it
an explicit CLI flag instead of hiding it.

Modal sandboxes also support native cloud bucket mounts through
`ModalCloudBucketMountStrategy` on `S3Mount`, `R2Mount`, and HMAC-authenticated
`GCSMount`.

For native cloud bucket testing, you can either export raw credential
environment variables or pass `--native-cloud-bucket-secret-name` to reuse an
existing named Modal Secret instead.

## Cloudflare

### Setup

Install the repo extra:

```bash
uv sync --extra cloudflare
```

Export the required environment variables:

```bash
export OPENAI_API_KEY=...
export CLOUDFLARE_SANDBOX_WORKER_URL=...
```

If your Cloudflare Sandbox Service worker requires bearer auth, also export:

```bash
export CLOUDFLARE_SANDBOX_API_KEY=...
```

### Run

```bash
uv run python examples/sandbox/extensions/cloudflare_runner.py --stream
```

Useful flags:

- `--stream` -- stream model output to the terminal.
- `--demo pty` -- run a PTY demo (interactive Python session with `tty=true`).
- `--skip-snapshot-check` -- skip the stop/resume snapshot round-trip verification.
- `--native-cloud-bucket-name <bucket>` -- mount an R2/S3 bucket via `CloudflareBucketMountStrategy`.
- `--native-cloud-bucket-endpoint-url <url>` -- optional S3 endpoint URL.
- `--api-key <key>` -- bearer token for the worker (or set `CLOUDFLARE_SANDBOX_API_KEY`).


Cloudflare sandboxes support native cloud bucket mounts through
`CloudflareBucketMountStrategy` on `S3Mount`, `R2Mount`, and HMAC-authenticated
`GCSMount`.

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

## Vercel

### Setup

Install the repo extra:

```bash
uv sync --extra vercel
```

Export the required environment variables:

```bash
export OPENAI_API_KEY=...
export VERCEL_OIDC_TOKEN=...
```

Or use explicit token and scope variables:

```bash
export OPENAI_API_KEY=...
export VERCEL_TOKEN=...
export VERCEL_PROJECT_ID=...
export VERCEL_TEAM_ID=...
```

### Run

```bash
uv run python examples/sandbox/extensions/vercel_runner.py --stream
```

Useful flags:

- `--workspace-persistence tar`
- `--workspace-persistence snapshot`
- `--runtime node22`
- `--timeout-ms 120000`

The Vercel example stays on the non-PTY path on purpose. It covers command
execution, workspace materialization, and persistence verification without
depending on interactive websocket support.

## Daytona

### Setup

Install the repo extra:

```bash
uv sync --extra daytona
```

Export the required environment variables:

```bash
export OPENAI_API_KEY=...
export DAYTONA_API_KEY=...
```

### Run

```bash
uv run python examples/sandbox/extensions/daytona/daytona_runner.py --stream
```

## Runloop

### Setup

Install the repo extra:

```bash
uv sync --extra runloop
```

Sign up for Runloop, no credit card required and $50 in credits @ [platform.runloop.ai](https://platform.runloop.ai/).
Export the required environment variables:

```bash
export OPENAI_API_KEY=...
export RUNLOOP_API_KEY=...
```

### Run

```bash
uv run python examples/sandbox/extensions/runloop/runner.py --stream
```

Useful flags:

- `--blueprint-name <name>`
- `--pause-on-exit`
- `--root`

Runloop-specific SDK features are also available directly on
`RunloopSandboxClientOptions` and `RunloopSandboxClient.platform`. Example:

```python
from agents.extensions.sandbox.runloop import (
    RunloopAfterIdle,
    RunloopGatewaySpec,
    RunloopLaunchParameters,
    RunloopMcpSpec,
    RunloopSandboxClient,
    RunloopSandboxClientOptions,
    RunloopTunnelConfig,
)

client = RunloopSandboxClient()
sandbox = await client.create(
    options=RunloopSandboxClientOptions(
        blueprint_name="python-3-12",
        launch_parameters=RunloopLaunchParameters(
            network_policy_id="np_123",
            resource_size_request="MEDIUM",
            after_idle=RunloopAfterIdle(idle_time_seconds=300, on_idle="suspend"),
        ),
        tunnel=RunloopTunnelConfig(auth_mode="authenticated"),
        gateways={
            "OPENAI_GATEWAY": RunloopGatewaySpec(
                gateway="openai",
                secret="OPENAI_GATEWAY_SECRET",
            )
        },
        mcp={
            "GITHUB_MCP": RunloopMcpSpec(
                mcp_config="github-readonly",
                secret="GITHUB_MCP_SECRET",
            )
        },
        managed_secrets={"OPENAI_API_KEY": "..."},
        metadata={"team": "agents"},
    )
)

public_blueprints = await client.platform.blueprints.list_public()
public_benchmarks = await client.platform.benchmarks.list_public()
```

`managed_secrets` are stored as Runloop account secrets and only secret references
are persisted in session state. The platform facade also exposes Runloop-native
helpers for blueprints, benchmarks, secrets, network policies, and axons.

If you enable `--root`, Runloop launches the devbox with
`launch_parameters.user_parameters={"username":"root","uid":0}`. In that mode,
the default home and working directory become `/root`, so the example also uses
`/root` as its manifest workspace root. If you configure root launch in your
own code, either rely on that root-mode default or explicitly choose a
`manifest.root` under `/root`.
## Blaxel

### Setup

Install the repo extra:

```bash
uv sync --extra blaxel
```

Create a Blaxel account and get an API key. The official docs are:

- <https://docs.blaxel.ai>
- <https://app.blaxel.ai>

Export the required environment variables:

```bash
export OPENAI_API_KEY=...
export BL_API_KEY=...
export BL_WORKSPACE=...
```

### Run

```bash
uv run python examples/sandbox/extensions/blaxel_runner.py --stream
```

Useful flags:

- `--image blaxel/py-app`
- `--region us-pdx-1`
- `--memory 4096`
- `--ttl 1h`
- `--pause-on-exit`
- `--skip-snapshot-check`

The runner also includes standalone demos for individual features. Pass
`--demo <name>` to run one:

- `pty` -- agent-driven interactive Python session via PTY
- `drive` -- [Blaxel Drive mount](https://docs.blaxel.ai/Agent-drive/Overview) (persistent storage, requires `--drive-name`)

Blaxel sandboxes support cloud bucket mounts (S3, R2, GCS) through
`BlaxelCloudBucketMountStrategy` and persistent drive mounts through
`BlaxelDriveMountStrategy`. See the
[Blaxel Drive docs](https://docs.blaxel.ai/Agent-drive/Overview) for details.
