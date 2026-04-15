# Sandbox clients

Use this page to choose where sandbox work should run. In most cases, the `SandboxAgent` definition stays the same while the sandbox client and client-specific options change in [`SandboxRunConfig`][agents.run_config.SandboxRunConfig].

!!! warning "Beta feature"

    Sandbox agents are in beta. Expect details of the API, defaults, and supported capabilities to change before general availability, and expect more advanced features over time.

## Decision guide

<div class="sandbox-nowrap-first-column-table" markdown="1">

| Goal | Start with | Why |
| --- | --- | --- |
| Fastest local iteration on macOS or Linux | `UnixLocalSandboxClient` | No extra install, simple local filesystem development. |
| Basic container isolation | `DockerSandboxClient` | Runs work inside Docker with a specific image. |
| Hosted execution or production-style isolation | A hosted sandbox client | Moves the workspace boundary to a provider-managed environment. |

</div>

## Local clients

For most users, start with one of these two sandbox clients:

<div class="sandbox-nowrap-first-column-table" markdown="1">

| Client | Install | Choose it when | Example |
| --- | --- | --- | --- |
| `UnixLocalSandboxClient` | none | Fastest local iteration on macOS or Linux. Good default for local development. | [Unix-local starter](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/unix_local_runner.py) |
| `DockerSandboxClient` | `openai-agents[docker]` | You want container isolation or a specific image for local parity. | [Docker starter](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/docker/docker_runner.py) |

</div>

Unix-local is the easiest way to start developing against a local filesystem. Move to Docker or a hosted provider when you need stronger environment isolation or production-style parity.

To switch from Unix-local to Docker, keep the agent definition the same and change only the run config:

```python
from docker import from_env as docker_from_env

from agents.run import RunConfig
from agents.sandbox import SandboxRunConfig
from agents.sandbox.sandboxes.docker import DockerSandboxClient, DockerSandboxClientOptions

run_config = RunConfig(
    sandbox=SandboxRunConfig(
        client=DockerSandboxClient(docker_from_env()),
        options=DockerSandboxClientOptions(image="python:3.14-slim"),
    ),
)
```

Use this when you want container isolation or image parity. See [examples/sandbox/docker/docker_runner.py](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/docker/docker_runner.py).

## Mounts and remote storage

Mount entries describe what storage to expose; mount strategies describe how a sandbox backend attaches that storage. Import the built-in mount entries and generic strategies from `agents.sandbox.entries`. Hosted-provider strategies are available from `agents.extensions.sandbox` or the provider-specific extension package.

Common mount options:

- `mount_path`: where the storage appears in the sandbox. Relative paths are resolved under the manifest root; absolute paths are used as-is.
- `read_only`: defaults to `True`. Set `False` only when the sandbox should write back to the mounted storage.
- `mount_strategy`: required. Use a strategy that matches both the mount entry and the sandbox backend.

Mounts are treated as ephemeral workspace entries. Snapshot and persistence flows detach or skip mounted paths instead of copying mounted remote storage into the saved workspace.

Generic local/container strategies:

<div class="sandbox-nowrap-first-column-table" markdown="1">

| Strategy or pattern | Use it when | Notes |
| --- | --- | --- |
| `InContainerMountStrategy(pattern=RcloneMountPattern(...))` | The sandbox image can run `rclone`. | Supports S3, GCS, R2, and Azure Blob. `RcloneMountPattern` can run in `fuse` mode or `nfs` mode. |
| `InContainerMountStrategy(pattern=MountpointMountPattern(...))` | The image has `mount-s3` and you want Mountpoint-style S3 or S3-compatible access. | Supports `S3Mount` and `GCSMount`. |
| `InContainerMountStrategy(pattern=FuseMountPattern(...))` | The image has `blobfuse2` and FUSE support. | Supports `AzureBlobMount`. |
| `InContainerMountStrategy(pattern=S3FilesMountPattern(...))` | The image has `mount.s3files` and can reach an existing S3 Files mount target. | Supports `S3FilesMount`. |
| `DockerVolumeMountStrategy(driver=...)` | Docker should attach a volume-driver-backed mount before the container starts. | Docker-only. S3, GCS, R2, and Azure Blob support `rclone`; S3 and GCS also support `mountpoint`. |

</div>

## Supported hosted platforms

When you need a hosted environment, the same `SandboxAgent` definition usually carries over and only the sandbox client changes in [`SandboxRunConfig`][agents.run_config.SandboxRunConfig].

If you are using the published SDK instead of this repository checkout, install sandbox-client dependencies through the matching package extra.

For provider-specific setup notes and links for the checked-in extension examples, see [examples/sandbox/extensions/README.md](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/extensions/README.md).

<div class="sandbox-nowrap-first-column-table" markdown="1">

| Client | Install | Example |
| --- | --- | --- |
| `BlaxelSandboxClient` | `openai-agents[blaxel]` | [Blaxel runner](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/extensions/blaxel_runner.py) |
| `CloudflareSandboxClient` | `openai-agents[cloudflare]` | [Cloudflare runner](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/extensions/cloudflare_runner.py) |
| `DaytonaSandboxClient` | `openai-agents[daytona]` | [Daytona runner](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/extensions/daytona/daytona_runner.py) |
| `E2BSandboxClient` | `openai-agents[e2b]` | [E2B runner](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/extensions/e2b_runner.py) |
| `ModalSandboxClient` | `openai-agents[modal]` | [Modal runner](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/extensions/modal_runner.py) |
| `RunloopSandboxClient` | `openai-agents[runloop]` | [Runloop runner](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/extensions/runloop/runner.py) |
| `VercelSandboxClient` | `openai-agents[vercel]` | [Vercel runner](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/extensions/vercel_runner.py) |

</div>

Hosted sandbox clients expose provider-specific mount strategies. Choose the backend and mount strategy that best fit your storage provider:

<div class="sandbox-nowrap-first-column-table" markdown="1">

| Backend | Mount notes |
| --- | --- |
| Docker | Supports `S3Mount`, `GCSMount`, `R2Mount`, `AzureBlobMount`, and `S3FilesMount` with local strategies such as `InContainerMountStrategy` and `DockerVolumeMountStrategy`. |
| `ModalSandboxClient` | Supports Modal cloud bucket mounts with `ModalCloudBucketMountStrategy` on `S3Mount`, `R2Mount`, and HMAC-authenticated `GCSMount`. You can use inline credentials or a named Modal Secret. |
| `CloudflareSandboxClient` | Supports Cloudflare bucket mounts with `CloudflareBucketMountStrategy` on `S3Mount`, `R2Mount`, and HMAC-authenticated `GCSMount`. |
| `BlaxelSandboxClient` | Supports cloud bucket mounts with `BlaxelCloudBucketMountStrategy` on `S3Mount`, `R2Mount`, and `GCSMount`. Also supports persistent Blaxel Drives with `BlaxelDriveMount` and `BlaxelDriveMountStrategy` from `agents.extensions.sandbox.blaxel`. |
| `DaytonaSandboxClient` | Supports cloud bucket mounts with `DaytonaCloudBucketMountStrategy`; use it with `S3Mount`, `GCSMount`, `R2Mount`, and `AzureBlobMount`. |
| `E2BSandboxClient` | Supports cloud bucket mounts with `E2BCloudBucketMountStrategy`; use it with `S3Mount`, `GCSMount`, `R2Mount`, and `AzureBlobMount`. |
| `RunloopSandboxClient` | Supports cloud bucket mounts with `RunloopCloudBucketMountStrategy`; use it with `S3Mount`, `GCSMount`, `R2Mount`, and `AzureBlobMount`. |
| `VercelSandboxClient` | No hosted-specific mount strategy is currently exposed. Use manifest files, repos, or other workspace inputs instead. |

</div>

The table below summarizes which remote storage entries each backend can mount directly.

<div class="sandbox-nowrap-first-column-table" markdown="1">

| Backend | AWS S3 | Cloudflare R2 | GCS | Azure Blob Storage | S3 Files |
| --- | --- | --- | --- | --- | --- |
| Docker | ✓ | ✓ | ✓ | ✓ | ✓ |
| `ModalSandboxClient` | ✓ | ✓ | ✓ | - | - |
| `CloudflareSandboxClient` | ✓ | ✓ | ✓ | - | - |
| `BlaxelSandboxClient` | ✓ | ✓ | ✓ | - | - |
| `DaytonaSandboxClient` | ✓ | ✓ | ✓ | ✓ | - |
| `E2BSandboxClient` | ✓ | ✓ | ✓ | ✓ | - |
| `RunloopSandboxClient` | ✓ | ✓ | ✓ | ✓ | - |
| `VercelSandboxClient` | - | - | - | - | - |

</div>

For more runnable examples, browse [examples/sandbox/](https://github.com/openai/openai-agents-python/tree/main/examples/sandbox) for local, coding, memory, handoff, and agent-composition patterns, and [examples/sandbox/extensions/](https://github.com/openai/openai-agents-python/tree/main/examples/sandbox/extensions) for hosted sandbox clients.
