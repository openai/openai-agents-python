# Sandbox examples

These examples show how to run agents with a managed workspace. Start with the small API examples when you want the smallest surface area, or use the tutorial scaffold when you want the shared layout for guided sandbox tutorials.

Unix-local examples run commands as host processes. On Linux, `UnixLocalSandboxClient` adds no OS-level confinement; on macOS, it applies filesystem restrictions through `sandbox-exec` without providing network isolation. Use these examples for trusted local development or within external isolation. For untrusted commands, including commands influenced by untrusted inputs, choose an appropriately configured Docker or hosted backend, or provide external isolation. Separate Unix-local workspaces or sessions do not establish an OS isolation boundary.

Most examples call a model through `Runner`, so set `OPENAI_API_KEY` in the repository-root `.env` file, in the example's `.env` file when it has one, or in your shell environment.

`sandbox_agent_with_tools.py` starts the repository's MCP v2 reference server and is intended to run with the locked development environment. The Agents SDK client itself supports both MCP v1 and v2.

## Small API examples

| Example | Run | What it shows |
| --- | --- | --- |
| [`basic.py`](basic.py) | `uv run python examples/classic/sandbox/basic.py` | Creates a sandbox session from a manifest, runs a `SandboxAgent`, and streams the result. |
| [`handoffs.py`](handoffs.py) | `uv run python examples/classic/sandbox/handoffs.py` | Uses handoffs with sandbox-backed agents. |
| [`sandbox_agent_capabilities.py`](sandbox_agent_capabilities.py) | `uv run python examples/classic/sandbox/sandbox_agent_capabilities.py` | Configures a sandbox agent with workspace capabilities. |
| [`sandbox_agent_with_tools.py`](sandbox_agent_with_tools.py) | `uv run python examples/classic/sandbox/sandbox_agent_with_tools.py` | Combines sandbox capabilities with host-defined tools. |
| [`sandbox_agents_as_tools.py`](sandbox_agents_as_tools.py) | `uv run python examples/classic/sandbox/sandbox_agents_as_tools.py` | Exposes sandbox agents as tools for another agent. |
| [`sandbox_agent_with_remote_snapshot.py`](sandbox_agent_with_remote_snapshot.py) | `uv run python examples/classic/sandbox/sandbox_agent_with_remote_snapshot.py` | Saves a Unix-local workspace to S3 and restores it. |
| [`memory.py`](memory.py) | `uv run python examples/classic/sandbox/memory.py` | Runs one sandbox agent twice across a snapshot resume so it can read and write its own memory. |
| [`memory_s3.py`](memory_s3.py) | `uv run python examples/classic/sandbox/memory_s3.py` | Runs sandbox memory across two fresh Docker sandboxes with S3-backed memory storage. |
| [`memory_multi_agent_multiturn.py`](memory_multi_agent_multiturn.py) | `uv run python examples/classic/sandbox/memory_multi_agent_multiturn.py` | Shows separate memory layouts for two agents sharing one sandbox workspace. |
| [`shared_session_workdirs.py`](shared_session_workdirs.py) | `uv run python examples/classic/sandbox/shared_session_workdirs.py` | Shares one live sandbox between trusted agents while Shell, `view_image`, and `apply_patch` resolve relative paths from each run's `cwd`. This is not confinement; untrusted workloads need a backend and configuration that provide the required isolation. |
| [`unix_local_pty.py`](unix_local_pty.py) | `uv run python examples/classic/sandbox/unix_local_pty.py` | Exercises an interactive pseudo-terminal in a Unix-local sandbox. |
| [`unix_local_runner.py`](unix_local_runner.py) | `uv run python examples/classic/sandbox/unix_local_runner.py` | Runs against the Unix-local sandbox backend directly. |

## Cloud storage examples

Run these examples from the repository root after completing [Classic source setup](../../../CLASSIC.md#install-from-the-matching-source). They make live model requests and write to cloud storage. Use an existing test bucket or container with read/write access, and replace every `your-...` placeholder below. Set `OPENAI_API_KEY` separately as described above.

### Docker mounts and S3 memory

Install the Docker extra in your source environment:

```sh
python -m pip install '.[classic-docker]'
```

The three programs under `docker/mounts/` and `memory_s3.py` require a working Docker daemon and an enabled rclone volume plugin registered as `rclone`. Follow the [official rclone Docker volume plugin setup](https://rclone.org/docker/#getting-started) for your Docker host and CPU architecture. The plugin runs on the Docker daemon's host, where FUSE must be installed and usable. These examples build their mount image automatically; installing tools in that image does not install the host plugin. Check `docker plugin ls` before running them.

For the S3 mount and S3 memory examples, export the bucket, its region and credentials:

```sh
export S3_MOUNT_BUCKET="your-test-bucket"
export AWS_REGION="your-bucket-region"
export AWS_ACCESS_KEY_ID="your-access-key-id"
export AWS_SECRET_ACCESS_KEY="your-secret-access-key"
python examples/classic/sandbox/docker/mounts/s3_mount_read_write.py
python examples/classic/sandbox/memory_s3.py
```

Set `AWS_SESSION_TOKEN` too when using temporary credentials. `S3_MOUNT_PREFIX` selects an optional object prefix. `memory_s3.py` also accepts `S3_BUCKET` as its bucket name and otherwise creates a unique prefix. Set `S3_ENDPOINT_URL` only when using a custom S3 endpoint reachable from the Docker host.

For the GCS mount, this setup uses a Google Cloud Storage HMAC key pair:

```sh
export GCS_MOUNT_BUCKET="your-test-bucket"
export GCS_ACCESS_ID="your-hmac-access-id"
export GCS_SECRET_ACCESS_KEY="your-hmac-secret"
python examples/classic/sandbox/docker/mounts/gcs_mount_read_write.py
```

`GCS_MOUNT_PREFIX` selects an optional object prefix. The example also accepts `GCS_SERVICE_ACCOUNT_FILE`, `GCS_SERVICE_ACCOUNT_CREDENTIALS`, or `GCS_ACCESS_TOKEN` for native GCS authentication instead of the HMAC pair. A service-account file must be accessible to the rclone plugin on the Docker host.

For the Azure Blob mount, this setup uses an account key:

```sh
export AZURE_STORAGE_ACCOUNT="your-storage-account"
export AZURE_STORAGE_CONTAINER="your-test-container"
export AZURE_STORAGE_ACCOUNT_KEY="your-storage-account-key"
python examples/classic/sandbox/docker/mounts/azure_mount_read_write.py
```

`AZURE_STORAGE_ENDPOINT` overrides the storage endpoint when needed. The example also forwards `AZURE_CLIENT_ID` for managed-identity configurations.

Each mount smoke test writes and reads a test object, then prints `docker_volume/rclone: ok`. The S3 memory example runs two fresh sandboxes and prints the memory files retained in its S3 prefix. Cloud objects remain after the examples finish.

### Remote S3 snapshots

`sandbox_agent_with_remote_snapshot.py` uses boto3 and a Unix-local workspace; it does not require Docker, FUSE, or rclone. Install its separate extra:

```sh
python -m pip install '.[classic-s3]'
```

Set `S3_MOUNT_BUCKET` and configure AWS credentials and region for boto3, for example with the AWS variables in the S3 block above. Then run:

```sh
python examples/classic/sandbox/sandbox_agent_with_remote_snapshot.py
```

The program uploads a snapshot under `openai-agents-python/sandbox-snapshots/`, restores its check file and prints `snapshot round-trip ok (s3)`. It then prints the agent's workspace summary. This example uses boto3's endpoint configuration; it does not read `S3_ENDPOINT_URL`.

## Cloud backend examples

Cloud-provider examples live under [`extensions/`](extensions). They cover E2B, Modal, and Daytona sandbox backends and require provider-specific credentials in addition to `OPENAI_API_KEY`.

The locked combination of `blaxel==0.3.5` and `mcp==2.3.0` cannot run the Blaxel example: Blaxel imports `mcp.server.fastmcp.FastMCP`, which MCP v2 removed. Use a separate source environment with MCP 1.30.0, which satisfies both packages' declared requirements. Follow the [source-build prerequisites](../../../CLASSIC.md#install-from-the-matching-source) before installing:

```sh
python3 -m venv .venv-blaxel
. .venv-blaxel/bin/activate
python -m pip install '.[classic-blaxel]' 'blaxel==0.3.5' 'mcp==1.30.0'
```

See the [Blaxel setup](extensions/README.md#blaxel) for provider credentials. Then run with the activated environment's Python:

```sh
python examples/classic/sandbox/extensions/blaxel_runner.py --stream
```

Keep this environment separate from the locked MCP v2 development environment. Plain `uv run` selects the project environment, and `uv sync` restores the locked versions.

## Tutorial scaffold

[`tutorials/`](tutorials) contains the shared helper code, Docker image, and folder conventions for guided sandbox tutorials. Tutorial folders are added in separate focused changes.

## Tutorials

| Example | What it does |
| --- | --- |
| [`sandbox_resume`](tutorials/sandbox_resume) | Edits a workspace app and reuses a sandbox snapshot. |
| [`dataroom_qa`](tutorials/dataroom_qa) | Answers questions over a mounted dataroom with source-backed responses. |
| [`dataroom_metric_extract`](tutorials/dataroom_metric_extract) | Extracts structured financial metrics to CSV/JSONL. |
| [`repo_code_review`](tutorials/repo_code_review) | Reviews a sample repo and writes finding, report, and patch artifacts. |
| [`vision_website_clone`](tutorials/vision_website_clone) | Uses vision and a browser-review loop to clone a reference static website. |

## Workflow examples

| Example | What it does |
| --- | --- |
| [`healthcare_support`](healthcare_support) | Runs a synthetic healthcare support workflow with a standard orchestrator, sandbox policy agent, memory, and human approvals. |

## Shared files

- [`docker/`](docker) contains Docker-specific helper examples.
- [`misc/`](misc) contains reusable support code and tiny reference tools used by several sandbox examples.
