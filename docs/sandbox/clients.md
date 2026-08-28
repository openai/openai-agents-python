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
| `DockerSandboxClient` | `openai-agents[docker]` | You want container isolation or a specific image to reproduce a target environment locally. | [Docker starter](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/docker/docker_runner.py) |

</div>

Unix-local is the easiest way to start developing against a local filesystem. Move to Docker or a hosted provider when you need stronger environment isolation or production-style parity.

`SandboxPathGrant.host_path` is Docker-only and maps a host path to a different POSIX path inside the container. Unix-local supports only same-path grants. See [Manifest path grants](guide.md#manifest) for details.

### Limit host environment inheritance for Unix-local sessions

By default, `UnixLocalSandboxClient` starts every command environment from the complete host process environment. Set `inherit_host_environment=False` to pass only a conservative allowlist of host variables instead:

```python
from agents.sandbox.sandboxes.unix_local import UnixLocalSandboxClient

client = UnixLocalSandboxClient(
    inherit_host_environment=False,
    host_environment_allowlist={"PATH", "LANG", "SSL_CERT_FILE"},
)
```

When `inherit_host_environment=False` and `host_environment_allowlist` is omitted, the SDK allows `PATH`, `LANG`, `LC_ALL`, `LC_COLLATE`, `LC_CTYPE`, `LC_MESSAGES`, `LC_MONETARY`, `LC_NUMERIC`, `LC_TIME`, `TZ`, `TERM`, `TMPDIR`, `SSL_CERT_FILE`, `SSL_CERT_DIR`, `REQUESTS_CA_BUNDLE`, `NODE_EXTRA_CA_CERTS`, `UV_PYTHON`, `NO_COLOR`, `FORCE_COLOR`, and `CI`. Pass a custom collection to replace that default allowlist. A custom allowlist requires `inherit_host_environment=False`.

Values from `Manifest.environment` are applied after host filtering and override inherited values. Unix-local commands always receive the workspace root as `HOME`. The inheritance policy belongs to the current client rather than serialized session state, so `create(...)` and `resume(...)` apply the policy of the client that performs that operation.

This option filters inherited environment variables only. Unix-local commands still run as local host processes with local filesystem and network access. Use Docker or a hosted sandbox when the workload requires stronger isolation.

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

Use this when you want container isolation or want the sandbox image to match the image used in another environment. See [examples/sandbox/docker/docker_runner.py](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/docker/docker_runner.py).

### Disable Docker networking

Set `network_mode="none"` when a Docker sandbox must not have network access:

```python
options = DockerSandboxClientOptions(
    image="python:3.14-slim",
    network_mode="none",
)
```

The only supported explicit network mode is `"none"`; omit `network_mode` to preserve Docker's default behavior. A network-disabled sandbox cannot expose ports, so combining `network_mode="none"` with a non-empty `exposed_ports` tuple fails during option validation. The setting is stored in sandbox session state and reapplied if the SDK must create a replacement container while resuming that state.

### Label Docker containers

Set `labels` when an application needs to identify or manage the Docker containers created for sandbox sessions:

```python
options = DockerSandboxClientOptions(
    image="python:3.14-slim",
    labels={
        "com.example.owner": "agents-sdk",
        "com.example.environment": "development",
    },
)
```

The SDK passes these key-value pairs to Docker when it creates the container and stores them in [`DockerSandboxSessionState`][agents.sandbox.sandboxes.docker.DockerSandboxSessionState]. When a resumed session reconnects to an existing container, the SDK verifies that every persisted label still has the expected value and raises `ValueError` if the labels do not match. When the SDK creates a replacement container from the saved state, it reapplies the persisted labels.

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
| `InContainerMountStrategy(pattern=RcloneMountPattern(...))` | The sandbox image can run `rclone`. | Supports S3, GCS, R2, Azure Blob, and Box. `RcloneMountPattern` can run in `fuse` mode or `nfs` mode. |
| `InContainerMountStrategy(pattern=MountpointMountPattern(...))` | The image has `mount-s3` and you want Mountpoint-style S3 or S3-compatible access. | Supports `S3Mount` and `GCSMount`. |
| `InContainerMountStrategy(pattern=FuseMountPattern(...))` | The image has `blobfuse2` and FUSE support. | Supports `AzureBlobMount`. |
| `InContainerMountStrategy(pattern=S3FilesMountPattern(...))` | The image has `mount.s3files` and can reach an existing S3 Files mount target. | Supports `S3FilesMount`. |
| `DockerVolumeMountStrategy(driver=...)` | Docker should attach a volume-driver-backed mount before the container starts. | Docker-only. S3, GCS, R2, Azure Blob, and Box can be mounted through `rclone`; S3 and GCS can also be mounted through `mountpoint`. |

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

### Size Modal sandboxes

Use `ModalSandboxClientOptions.cpu` and `ModalSandboxClientOptions.memory` to request resources for a new Modal sandbox. A single value requests that amount. A two-item `(request, limit)` tuple uses the first item as the request and the second item as the limit. Memory values are in MiB.

```python
from agents.extensions.sandbox import ModalSandboxClientOptions

options = ModalSandboxClientOptions(
    app_name="agents-sandbox",
    cpu=(1.0, 4.0),
    memory=(2048, 8192),
)
```

Leave `cpu`, `memory`, or both as `None` to use Modal's default for each omitted resource. The selected values are preserved in sandbox session state so replacement sandboxes use the same resource configuration.

Hosted sandbox clients expose provider-specific mount strategies. Choose the backend and mount strategy that best fit your storage provider:

<div class="sandbox-nowrap-first-column-table" markdown="1">

| Backend | Mount notes |
| --- | --- |
| Docker | Supports `S3Mount`, `GCSMount`, `R2Mount`, `AzureBlobMount`, `BoxMount`, and `S3FilesMount` with local strategies such as `InContainerMountStrategy` and `DockerVolumeMountStrategy`. |
| `ModalSandboxClient` | Supports cloud bucket mounts by using `ModalCloudBucketMountStrategy` with `S3Mount`, `R2Mount`, and HMAC-authenticated `GCSMount`. You can use inline credentials or a named Modal Secret. |
| `CloudflareSandboxClient` | Supports bucket mounts by using `CloudflareBucketMountStrategy` with `S3Mount`, `R2Mount`, and HMAC-authenticated `GCSMount`. |
| `BlaxelSandboxClient` | Supports cloud bucket mounts by pairing `BlaxelCloudBucketMountStrategy` with an `S3Mount`, `R2Mount`, or `GCSMount` entry. Also supports persistent Blaxel Drives with `BlaxelDriveMount` and `BlaxelDriveMountStrategy`, both available from `agents.extensions.sandbox.blaxel`. |
| `DaytonaSandboxClient` | Supports mounting cloud storage through `rclone` by using `DaytonaCloudBucketMountStrategy`; use it with `S3Mount`, `GCSMount`, `R2Mount`, `AzureBlobMount`, and `BoxMount`. |
| `E2BSandboxClient` | Supports mounting cloud storage through `rclone` by using `E2BCloudBucketMountStrategy`; use it with `S3Mount`, `GCSMount`, `R2Mount`, `AzureBlobMount`, and `BoxMount`. |
| `RunloopSandboxClient` | Supports mounting cloud storage through `rclone` by using `RunloopCloudBucketMountStrategy`; use it with `S3Mount`, `GCSMount`, `R2Mount`, `AzureBlobMount`, and `BoxMount`. |
| `VercelSandboxClient` | Supports create-time-only S3 and S3-compatible bucket mounts by pairing `VercelCloudBucketMountStrategy` with an `S3Mount` entry; mounted sessions cannot be resumed, and inline credentials require `allow_s3_credential_exposure=True`. |

</div>

The mount tables describe which storage types each backend can execute. A check mark does not bypass the credential boundary for a mount helper that runs inside a model-controlled sandbox, and it does not mean that every strategy can operate without credentials. The Agents SDK accepts an in-container mount without an acknowledgement only when the selected helper can operate without protected authority. It rejects a mount that requires protected authority before starting the sandbox or mount helper unless trusted application code explicitly acknowledges the exposure for the exact mount path.

Credentialless `rclone` mounts are limited to S3, GCS, R2, and Azure Blob. An in-container Box mount requires a non-interactive authentication source and the acknowledgement that matches that source. `FuseMountPattern` requires broad acknowledgement because `blobfuse2` discovers ambient Azure authority, even when no inline credential is configured. `S3FilesMountPattern` likewise requires broad acknowledgement because `mount.s3files` uses ambient IAM authority. These requirements also apply when Docker is the backend; the check marks below indicate that Docker can execute the mount after the applicable authority boundary is satisfied.

For a mount entry named `"data"`, retain the copied `Manifest` returned by the acknowledgement that matches the configured authority:

```python
# Mount-scoped values such as inline access keys.
manifest = manifest.with_in_container_mount_credential_exposure_acknowledged("data")

# Broader authority such as managed or workload identity and external credential files.
manifest = manifest.with_in_container_mount_broad_credential_exposure_acknowledged("data")
```

Pass every exact mount path that needs the acknowledgement. A mount that uses both authority classes requires both acknowledgements. The acknowledgements are runtime-only, are not serialized, and permit the helper to receive credentials without confining credential use to the mounted path. Prefer an external or provider-native strategy when available, and otherwise use sandbox-scoped, short-lived, least-privilege credentials.

`VercelSandboxClientOptions(allow_s3_credential_exposure=True)` remains a compatibility option for create-time Vercel S3 mounts with inline mount-scoped credentials. It does not authorize broad credential authority.

The table below summarizes which remote storage entries each backend can mount directly.

<div class="sandbox-nowrap-first-column-table" markdown="1">

| Backend | AWS S3 | Cloudflare R2 | GCS | Azure Blob Storage | Box | S3 Files |
| --- | --- | --- | --- | --- | --- | --- |
| Docker | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `ModalSandboxClient` | ✓ | ✓ | ✓ | - | - | - |
| `CloudflareSandboxClient` | ✓ | ✓ | ✓ | - | - | - |
| `BlaxelSandboxClient` | ✓ | ✓ | ✓ | - | - | - |
| `DaytonaSandboxClient` | ✓ | ✓ | ✓ | ✓ | ✓ | - |
| `E2BSandboxClient` | ✓ | ✓ | ✓ | ✓ | ✓ | - |
| `RunloopSandboxClient` | ✓ | ✓ | ✓ | ✓ | ✓ | - |
| `VercelSandboxClient` | ✓ | - | - | - | - | - |

</div>

For more runnable examples, browse [examples/sandbox/](https://github.com/openai/openai-agents-python/tree/main/examples/sandbox) for local, coding, memory, handoff, and agent-composition patterns, and [examples/sandbox/extensions/](https://github.com/openai/openai-agents-python/tree/main/examples/sandbox/extensions) for hosted sandbox clients.
