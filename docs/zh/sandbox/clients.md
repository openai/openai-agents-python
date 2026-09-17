---
search:
  exclude: true
---
# 沙箱客户端

使用本页选择沙箱工作应在何处运行。在大多数情况下，`SandboxAgent` 定义保持不变，而沙箱客户端及其特定选项会在 [`SandboxRunConfig`][agents.run_config.SandboxRunConfig] 中更改。

!!! warning "Beta 功能"

    沙箱智能体目前处于 Beta 阶段。在正式发布前，API 细节、默认值和支持的功能可能会发生变化，并且后续会逐步提供更多高级功能。

## 决策指南 {#decision-guide}

<div class="sandbox-nowrap-first-column-table" markdown="1">

| 目标 | 起始选择 | 原因 |
| --- | --- | --- |
| 在 macOS 或 Linux 上进行可信的本地开发 | `UnixLocalSandboxClient` | 无需额外安装；命令作为本地主机进程运行。 |
| 基础容器隔离 | `DockerSandboxClient` | 使用指定镜像在 Docker 中运行工作。 |
| 托管执行或生产环境级隔离 | 托管沙箱客户端 | 将工作区边界移至由提供商管理的环境。 |

</div>

!!! warning "Unix-local 执行限制"

    `UnixLocalSandboxClient` 将命令作为本地主机进程运行。在 Linux 上，此后端不提供操作系统级别的限制：命令可以访问主机进程和任何外部隔离措施所允许的文件与网络资源。工作区目录、`HOME` 或 `cwd` 均不会限制这种访问。

    在 macOS 上，此后端使用 `sandbox-exec` 应用文件系统限制。这些限制不提供网络隔离，也无法提供与容器相同的边界。

    请将 Unix-local 用于可信的本地开发，或在外部隔离的环境中使用。对于不可信命令，包括受不可信输入影响的命令，请选择经过适当配置的 Docker 或托管沙箱，或提供外部隔离。请根据工作负载检查所选环境的权限、挂载、凭据和网络访问能力。

## 本地客户端 {#local-clients}

对于大多数用户，建议从以下两个沙箱客户端之一开始：

<div class="sandbox-nowrap-first-column-table" markdown="1">

| 客户端 | 安装 | 适用场景 | 示例 |
| --- | --- | --- | --- |
| `UnixLocalSandboxClient` | 无 | 在 macOS 或 Linux 上进行可信的本地开发，或在外部隔离环境中执行。 | [Unix-local 入门示例](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/unix_local_runner.py) |
| `DockerSandboxClient` | `openai-agents[docker]` | 需要容器隔离，或需要使用特定镜像在本地复现目标环境。 | [Docker 入门示例](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/docker/docker_runner.py) |

</div>

Unix-local 无需容器即可提供本地工作区。当需要由后端提供隔离边界，或需要与其他环境匹配的镜像时，请选择 Docker 或托管提供商。

`SandboxPathGrant.host_path` 仅适用于 Docker，它会将主机路径映射到容器内不同的 POSIX 路径。Unix-local 仅支持相同路径的授权。有关详情，请参阅[清单路径授权](guide.md#manifest)。

### Unix-local 会话的主机环境继承限制 {#limit-host-environment-inheritance-for-unix-local-sessions}

默认情况下，`UnixLocalSandboxClient` 会基于完整的主机进程环境启动每个命令环境。设置 `inherit_host_environment=False`，以便仅传递采用保守策略的主机变量允许列表：

```python
from agents.sandbox.sandboxes.unix_local import UnixLocalSandboxClient

client = UnixLocalSandboxClient(
    inherit_host_environment=False,
    host_environment_allowlist={"PATH", "LANG", "SSL_CERT_FILE"},
)
```

当使用 `inherit_host_environment=False` 且省略 `host_environment_allowlist` 时，SDK 允许 `PATH`、`LANG`、`LC_ALL`、`LC_COLLATE`、`LC_CTYPE`、`LC_MESSAGES`、`LC_MONETARY`、`LC_NUMERIC`、`LC_TIME`、`TZ`、`TERM`、`TMPDIR`、`SSL_CERT_FILE`、`SSL_CERT_DIR`、`REQUESTS_CA_BUNDLE`、`NODE_EXTRA_CA_CERTS`、`UV_PYTHON`、`NO_COLOR`、`FORCE_COLOR` 和 `CI`。传入自定义集合可替换该默认允许列表。自定义允许列表需要 `inherit_host_environment=False`。

来自 `Manifest.environment` 的值会在主机环境筛选后应用，并覆盖继承的值。Unix-local 命令始终会收到作为 `HOME` 的工作区根目录。继承策略属于当前客户端，而不是序列化的会话状态，因此 `create(...)` 和 `resume(...)` 会应用执行相应操作的客户端所使用的策略。

此选项只筛选继承的环境变量，不会增加操作系统级别的限制。上述 Unix-local 执行限制仍然适用。

若要从 Unix-local 切换到 Docker，请保持智能体定义不变，只更改运行配置：

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

当需要容器隔离，或希望沙箱镜像与其他环境使用的镜像保持一致时，请使用此方式。请参阅 [examples/sandbox/docker/docker_runner.py](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/docker/docker_runner.py)。

### Docker 网络禁用配置 {#disable-docker-networking}

当 Docker 沙箱不得访问网络时，请设置 `network_mode="none"`：

```python
options = DockerSandboxClientOptions(
    image="python:3.14-slim",
    network_mode="none",
)
```

唯一受支持的显式网络模式是 `"none"`；省略 `network_mode` 可保留 Docker 的默认行为。禁用网络的沙箱无法暴露端口，因此将 `network_mode="none"` 与非空的 `exposed_ports` 元组组合使用时，选项验证会失败。该设置存储在沙箱会话状态中；如果 SDK 在恢复该状态时必须创建替代容器，则会重新应用此设置。

### Docker 容器标签 {#label-docker-containers}

当应用程序需要识别或管理为沙箱会话创建的 Docker 容器时，请设置 `labels`：

```python
options = DockerSandboxClientOptions(
    image="python:3.14-slim",
    labels={
        "com.example.owner": "agents-sdk",
        "com.example.environment": "development",
    },
)
```

SDK 在创建容器时会将这些键值对传递给 Docker，并将它们存储在 [`DockerSandboxSessionState`][agents.sandbox.sandboxes.docker.DockerSandboxSessionState] 中。当恢复的会话重新连接到现有容器时，SDK 会验证每个持久化标签是否仍具有预期值；如果标签不匹配，则引发 `ValueError`。当 SDK 根据保存的状态创建替代容器时，它会重新应用持久化的标签。

## 挂载与远程存储 {#mounts-and-remote-storage}

挂载条目描述要公开哪些存储，挂载策略描述沙箱后端如何附加这些存储。从 `agents.sandbox.entries` 导入内置挂载条目和通用策略。托管提供商策略可从 `agents.extensions.sandbox` 或提供商专用扩展包中获取。

常用挂载选项：

- `mount_path`：存储在沙箱中的显示位置。相对路径基于清单根目录解析；绝对路径按原样使用。
- `read_only`：默认为 `True`。仅当沙箱应将更改写回已挂载的存储时，才设置 `False`。
- `mount_strategy`：必填。请使用同时匹配挂载条目和沙箱后端的策略。

挂载被视为临时工作区条目。快照和持久化流程会分离或跳过已挂载路径，而不会将已挂载的远程存储复制到保存的工作区中。

通用本地/容器策略：

<div class="sandbox-nowrap-first-column-table" markdown="1">

| 策略或模式 | 适用场景 | 说明 |
| --- | --- | --- |
| `InContainerMountStrategy(pattern=RcloneMountPattern(...))` | 沙箱镜像可以运行 `rclone`。 | 支持 S3、GCS、R2、Azure Blob 和 Box。`RcloneMountPattern` 可以在 `fuse` 模式或 `nfs` 模式下运行。 |
| `InContainerMountStrategy(pattern=MountpointMountPattern(...))` | 镜像包含 `mount-s3`，并且需要 Mountpoint 风格的 S3 或 S3 兼容访问。 | 支持 `S3Mount` 和 `GCSMount`。 |
| `InContainerMountStrategy(pattern=FuseMountPattern(...))` | 镜像包含 `blobfuse2` 并支持 FUSE。 | 支持 `AzureBlobMount`。 |
| `InContainerMountStrategy(pattern=S3FilesMountPattern(...))` | 镜像包含 `mount.s3files`，并且可以访问现有的 S3 Files 挂载目标。 | 支持 `S3FilesMount`。 |
| `DockerVolumeMountStrategy(driver=...)` | Docker 应在容器启动前附加由卷驱动程序支持的挂载。 | 仅适用于 Docker。S3、GCS、R2、Azure Blob 和 Box 可以通过 `rclone` 挂载；S3 和 GCS 也可以通过 `mountpoint` 挂载。 |

</div>

## 支持的托管平台 {#supported-hosted-platforms}

当需要托管环境时，通常可以继续使用相同的 `SandboxAgent` 定义，仅在 [`SandboxRunConfig`][agents.run_config.SandboxRunConfig] 中更改沙箱客户端。

如果使用的是已发布的 SDK，而不是此仓库的检出版本，请通过对应的软件包 extra 安装沙箱客户端依赖项。

有关提供商专用的设置说明以及仓库内扩展代码示例的链接，请参阅 [examples/sandbox/extensions/README.md](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/extensions/README.md)。

<div class="sandbox-nowrap-first-column-table" markdown="1">

| 客户端 | 安装 | 示例 |
| --- | --- | --- |
| `BlaxelSandboxClient` | `openai-agents[blaxel]` | [Blaxel 运行器](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/extensions/blaxel_runner.py) |
| `CloudflareSandboxClient` | `openai-agents[cloudflare]` | [Cloudflare 运行器](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/extensions/cloudflare_runner.py) |
| `DaytonaSandboxClient` | `openai-agents[daytona]` | [Daytona 运行器](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/extensions/daytona/daytona_runner.py) |
| `E2BSandboxClient` | `openai-agents[e2b]` | [E2B 运行器](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/extensions/e2b_runner.py) |
| `ModalSandboxClient` | `openai-agents[modal]` | [Modal 运行器](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/extensions/modal_runner.py) |
| `RunloopSandboxClient` | `openai-agents[runloop]` | [Runloop 运行器](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/extensions/runloop/runner.py) |
| `VercelSandboxClient` | `openai-agents[vercel]` | [Vercel 运行器](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/extensions/vercel_runner.py) |

</div>

### Modal 沙箱资源大小 {#size-modal-sandboxes}

使用 `ModalSandboxClientOptions.cpu` 和 `ModalSandboxClientOptions.memory` 为新的 Modal 沙箱请求资源。单个值表示请求该数量。包含两个元素的 `(request, limit)` 元组将第一个元素用作请求值，第二个元素用作上限值。内存值以 MiB 为单位。

```python
from agents.extensions.sandbox import ModalSandboxClientOptions

options = ModalSandboxClientOptions(
    app_name="agents-sandbox",
    cpu=(1.0, 4.0),
    memory=(2048, 8192),
)
```

将 `cpu`、`memory` 或两者保留为 `None`，即可对每个省略的资源使用 Modal 默认值。所选值会保存在沙箱会话状态中，以便替代沙箱使用相同的资源配置。

托管沙箱客户端会提供特定于提供商的挂载策略。请选择最适合存储提供商的后端和挂载策略：

<div class="sandbox-nowrap-first-column-table" markdown="1">

| 后端 | 挂载说明 |
| --- | --- |
| Docker | 支持将 `S3Mount`、`GCSMount`、`R2Mount`、`AzureBlobMount`、`BoxMount` 和 `S3FilesMount` 与 `InContainerMountStrategy`、`DockerVolumeMountStrategy` 等本地策略配合使用。 |
| `ModalSandboxClient` | 支持使用 `ModalCloudBucketMountStrategy` 搭配 `S3Mount`、`R2Mount` 和经 HMAC 身份验证的 `GCSMount` 来挂载云存储桶。可以使用内联凭据或具名 Modal Secret。 |
| `CloudflareSandboxClient` | 支持使用 `CloudflareBucketMountStrategy` 搭配 `S3Mount`、`R2Mount` 和经 HMAC 身份验证的 `GCSMount` 来挂载存储桶。 |
| `BlaxelSandboxClient` | 支持将 `BlaxelCloudBucketMountStrategy` 与 `S3Mount`、`R2Mount` 或 `GCSMount` 条目配对使用来挂载云存储桶。还支持通过 `BlaxelDriveMount` 和 `BlaxelDriveMountStrategy` 使用持久化 Blaxel Drives，两者均可从 `agents.extensions.sandbox.blaxel` 获取。 |
| `DaytonaSandboxClient` | 支持使用 `DaytonaCloudBucketMountStrategy` 通过 `rclone` 挂载云存储；可将其与 `S3Mount`、`GCSMount`、`R2Mount`、`AzureBlobMount` 和 `BoxMount` 配合使用。 |
| `E2BSandboxClient` | 支持使用 `E2BCloudBucketMountStrategy` 通过 `rclone` 挂载云存储；可将其与 `S3Mount`、`GCSMount`、`R2Mount`、`AzureBlobMount` 和 `BoxMount` 配合使用。 |
| `RunloopSandboxClient` | 支持使用 `RunloopCloudBucketMountStrategy` 通过 `rclone` 挂载云存储；可将其与 `S3Mount`、`GCSMount`、`R2Mount`、`AzureBlobMount` 和 `BoxMount` 配合使用。 |
| `VercelSandboxClient` | 支持将 `VercelCloudBucketMountStrategy` 与 `S3Mount` 条目配对，以仅在创建时挂载 S3 和 S3 兼容存储桶；已挂载的会话无法恢复，内联凭据需要 `allow_s3_credential_exposure=True`。 |

</div>

挂载表说明了每个后端能够处理哪些存储类型。如果挂载辅助程序在由模型控制的沙箱内运行，勾选标记并不会绕过其凭据边界，也不表示每种策略都能在没有凭据的情况下运行。只有当所选辅助程序无需受保护的权限即可运行时，Agents SDK 才会在没有确认的情况下接受容器内挂载。如果挂载需要受保护的权限，而可信应用程序代码未明确确认要为该确切挂载路径开放权限，Agents SDK 会在启动沙箱或挂载辅助程序之前拒绝该挂载。

无需凭据的 `rclone` 挂载仅限于 S3、GCS、R2 和 Azure Blob。容器内 Box 挂载需要非交互式身份验证来源，以及与该来源匹配的确认。`FuseMountPattern` 需要广泛权限确认，因为即使未配置内联凭据，`blobfuse2` 也会发现环境中的 Azure 权限。类似地，`S3FilesMountPattern` 也需要广泛权限确认，因为 `mount.s3files` 会使用环境中的 IAM 权限。当 Docker 用作后端时，这些要求同样适用；下方的勾选标记表示，在满足适用的权限边界后，Docker 可以执行该挂载。

对于名为 `"data"` 的挂载条目，请保留由与所配置权限匹配的确认所返回并复制的 `Manifest`：

```python
# Mount-scoped values such as inline access keys.
manifest = manifest.with_in_container_mount_credential_exposure_acknowledged("data")

# Broader authority such as managed or workload identity and external credential files.
manifest = manifest.with_in_container_mount_broad_credential_exposure_acknowledged("data")
```

请传入需要确认的每个确切挂载路径。同时使用两类权限的挂载需要进行两项确认。这些确认仅在运行时有效，不会被序列化，并允许辅助程序接收凭据，但不会将凭据的使用范围限制在挂载路径内。可用时，应优先选择外部策略或提供商原生策略；否则，请使用作用域限定于沙箱、生命周期短且遵循最小权限原则的凭据。

`VercelSandboxClientOptions(allow_s3_credential_exposure=True)` 仍是一个兼容性选项，用于创建时使用内联、作用域限定于挂载的凭据进行 Vercel S3 挂载。它不授权广泛的凭据权限。

下表总结了每个后端可以直接挂载哪些远程存储条目。

<div class="sandbox-nowrap-first-column-table" markdown="1">

| 后端 | AWS S3 | Cloudflare R2 | GCS | Azure Blob Storage | Box | S3 Files |
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

如需更多可运行的代码示例，请浏览 [examples/sandbox/](https://github.com/openai/openai-agents-python/tree/main/examples/sandbox)，其中包含本地、编码、内存、任务转移和智能体组合模式；有关托管沙箱客户端，请浏览 [examples/sandbox/extensions/](https://github.com/openai/openai-agents-python/tree/main/examples/sandbox/extensions)。