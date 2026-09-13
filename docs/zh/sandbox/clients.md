---
search:
  exclude: true
---
# 沙箱客户端

使用本页选择沙箱任务的运行位置。在大多数情况下，`SandboxAgent` 定义保持不变，仅更改 [`SandboxRunConfig`][agents.run_config.SandboxRunConfig] 中的沙箱客户端及客户端专属选项。

!!! warning "Beta 功能"

    沙箱智能体目前处于 Beta 阶段。在正式发布之前，API 细节、默认值和支持的功能可能发生变化，未来还会提供更多高级功能。

## 决策指南 {#decision-guide}

<div class="sandbox-nowrap-first-column-table" markdown="1">

| 目标 | 首选方案 | 原因 |
| --- | --- | --- |
| 在 macOS 或 Linux 上实现最快的本地迭代 | `UnixLocalSandboxClient` | 无需额外安装，适合使用简单的本地文件系统进行开发。 |
| 基本容器隔离 | `DockerSandboxClient` | 使用指定镜像在 Docker 内运行任务。 |
| 托管执行或生产级隔离 | 托管沙箱客户端 | 将工作区边界移至提供商管理的环境。 |

</div>

## 本地客户端 {#local-clients}

对于大多数用户，建议从以下两个沙箱客户端之一开始：

<div class="sandbox-nowrap-first-column-table" markdown="1">

| 客户端 | 安装 | 适用场景 | 示例 |
| --- | --- | --- | --- |
| `UnixLocalSandboxClient` | 无 | 在 macOS 或 Linux 上实现最快的本地迭代。是本地开发的良好默认选项。 | [Unix-local 入门示例](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/unix_local_runner.py) |
| `DockerSandboxClient` | `openai-agents[docker]` | 希望实现容器隔离，或使用特定镜像在本地复现目标环境。 | [Docker 入门示例](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/docker/docker_runner.py) |

</div>

Unix-local 是开始针对本地文件系统进行开发的最简便方式。当需要更强的环境隔离或与生产环境保持一致时，可迁移到 Docker 或托管提供商。

`SandboxPathGrant.host_path` 仅适用于 Docker，可将主机路径映射到容器内的另一个 POSIX 路径。Unix-local 仅支持相同路径授权。有关详细信息，请参阅[清单路径授权](guide.md#manifest)。

### Unix-local 会话的主机环境继承限制 {#limit-host-environment-inheritance-for-unix-local-sessions}

默认情况下，`UnixLocalSandboxClient` 会以完整的主机进程环境作为每个命令环境的起点。设置 `inherit_host_environment=False` 后，则仅传递保守白名单中的主机变量：

```python
from agents.sandbox.sandboxes.unix_local import UnixLocalSandboxClient

client = UnixLocalSandboxClient(
    inherit_host_environment=False,
    host_environment_allowlist={"PATH", "LANG", "SSL_CERT_FILE"},
)
```

当设置 `inherit_host_environment=False` 且省略 `host_environment_allowlist` 时，SDK 允许 `PATH`、`LANG`、`LC_ALL`、`LC_COLLATE`、`LC_CTYPE`、`LC_MESSAGES`、`LC_MONETARY`、`LC_NUMERIC`、`LC_TIME`、`TZ`、`TERM`、`TMPDIR`、`SSL_CERT_FILE`、`SSL_CERT_DIR`、`REQUESTS_CA_BUNDLE`、`NODE_EXTRA_CA_CERTS`、`UV_PYTHON`、`NO_COLOR`、`FORCE_COLOR` 和 `CI`。传入自定义集合可替换此默认白名单。自定义白名单要求设置 `inherit_host_environment=False`。

来自 `Manifest.environment` 的值会在主机环境筛选后应用，并覆盖继承的值。Unix-local 命令始终通过 `HOME` 接收工作区根目录。继承策略属于当前客户端，而不属于序列化的会话状态，因此 `create(...)` 和 `resume(...)` 会应用执行相应操作的客户端所使用的策略。

此选项仅筛选继承的环境变量。Unix-local 命令仍作为本地主机进程运行，并具有本地文件系统和网络访问权限。如果工作负载需要更强的隔离，请使用 Docker 或托管沙箱。

要从 Unix-local 切换到 Docker，请保持智能体定义不变，仅更改运行配置：

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

当需要容器隔离，或希望沙箱镜像与其他环境中使用的镜像保持一致时，请采用此方式。请参阅 [examples/sandbox/docker/docker_runner.py](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/docker/docker_runner.py)。

### Docker 网络禁用 {#disable-docker-networking}

当 Docker 沙箱不得访问网络时，请设置 `network_mode="none"`：

```python
options = DockerSandboxClientOptions(
    image="python:3.14-slim",
    network_mode="none",
)
```

唯一明确支持的网络模式是 `"none"`；省略 `network_mode` 可保留 Docker 的默认行为。禁用网络的沙箱无法暴露端口，因此将 `network_mode="none"` 与非空的 `exposed_ports` 元组结合使用，会在选项验证期间失败。此设置存储在沙箱会话状态中；如果 SDK 在恢复该状态时必须创建替代容器，则会重新应用此设置。

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

SDK 创建容器时会将这些键值对传递给 Docker，并将其存储在 [`DockerSandboxSessionState`][agents.sandbox.sandboxes.docker.DockerSandboxSessionState] 中。恢复的会话重新连接到现有容器时，SDK 会验证每个持久化标签是否仍为预期值；如果标签不匹配，则引发 `ValueError`。SDK 根据已保存状态创建替代容器时，会重新应用持久化标签。

## 挂载与远程存储 {#mounts-and-remote-storage}

挂载条目描述要暴露的存储，挂载策略描述沙箱后端如何连接该存储。从 `agents.sandbox.entries` 导入内置挂载条目和通用策略。托管提供商策略可从 `agents.extensions.sandbox` 或提供商专属扩展包中获取。

常用挂载选项：

- `mount_path`：存储在沙箱中的显示位置。相对路径基于清单根目录解析；绝对路径则按原样使用。
- `read_only`：默认为 `True`。仅当沙箱需要将更改写回已挂载存储时，才设置 `False`。
- `mount_strategy`：必填。请使用同时匹配挂载条目和沙箱后端的策略。

挂载会被视为临时工作区条目。快照和持久化流程会分离或跳过已挂载路径，而不会将已挂载的远程存储复制到保存的工作区中。

通用本地/容器策略：

<div class="sandbox-nowrap-first-column-table" markdown="1">

| 策略或模式 | 适用场景 | 备注 |
| --- | --- | --- |
| `InContainerMountStrategy(pattern=RcloneMountPattern(...))` | 沙箱镜像可以运行 `rclone`。 | 支持 S3、GCS、R2、Azure Blob 和 Box。`RcloneMountPattern` 可以在 `fuse` 模式或 `nfs` 模式下运行。 |
| `InContainerMountStrategy(pattern=MountpointMountPattern(...))` | 镜像包含 `mount-s3`，并且需要 Mountpoint 风格的 S3 或 S3 兼容访问。 | 支持 `S3Mount` 和 `GCSMount`。 |
| `InContainerMountStrategy(pattern=FuseMountPattern(...))` | 镜像包含 `blobfuse2` 并支持 FUSE。 | 支持 `AzureBlobMount`。 |
| `InContainerMountStrategy(pattern=S3FilesMountPattern(...))` | 镜像包含 `mount.s3files`，并且可以访问现有的 S3 Files 挂载目标。 | 支持 `S3FilesMount`。 |
| `DockerVolumeMountStrategy(driver=...)` | Docker 应在容器启动前连接由卷驱动程序支持的挂载。 | 仅适用于 Docker。可通过 `rclone` 挂载 S3、GCS、R2、Azure Blob 和 Box；也可通过 `mountpoint` 挂载 S3 和 GCS。 |

</div>

## 支持的托管平台 {#supported-hosted-platforms}

当需要托管环境时，通常可以沿用相同的 `SandboxAgent` 定义，仅更改 [`SandboxRunConfig`][agents.run_config.SandboxRunConfig] 中的沙箱客户端。

如果使用的是已发布的 SDK，而不是此代码仓库的检出版本，请通过对应的软件包 extra 安装沙箱客户端依赖项。

有关提供商专属的设置说明，以及代码仓库中扩展代码示例的链接，请参阅 [examples/sandbox/extensions/README.md](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/extensions/README.md)。

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

### Modal 沙箱规格 {#size-modal-sandboxes}

使用 `ModalSandboxClientOptions.cpu` 和 `ModalSandboxClientOptions.memory` 为新的 Modal 沙箱申请资源。单个值表示申请该数量的资源。包含两个元素的 `(request, limit)` 元组以第一个元素作为申请值，以第二个元素作为限制值。内存值以 MiB 为单位。

```python
from agents.extensions.sandbox import ModalSandboxClientOptions

options = ModalSandboxClientOptions(
    app_name="agents-sandbox",
    cpu=(1.0, 4.0),
    memory=(2048, 8192),
)
```

将 `cpu`、`memory` 或两者保留为 `None`，即可对每项省略的资源使用 Modal 的默认值。所选值会保留在沙箱会话状态中，因此替代沙箱将使用相同的资源配置。

托管沙箱客户端会提供各提供商专属的挂载策略。请选择最适合所用存储提供商的后端和挂载策略：

<div class="sandbox-nowrap-first-column-table" markdown="1">

| 后端 | 挂载说明 |
| --- | --- |
| Docker | 支持将 `S3Mount`、`GCSMount`、`R2Mount`、`AzureBlobMount`、`BoxMount` 和 `S3FilesMount` 与 `InContainerMountStrategy` 和 `DockerVolumeMountStrategy` 等本地策略配合使用。 |
| `ModalSandboxClient` | 支持使用 `ModalCloudBucketMountStrategy` 挂载云存储桶，并可搭配 `S3Mount`、`R2Mount` 和采用 HMAC 身份验证的 `GCSMount`。可以使用内联凭据或已命名的 Modal Secret。 |
| `CloudflareSandboxClient` | 支持使用 `CloudflareBucketMountStrategy` 挂载存储桶，并可搭配 `S3Mount`、`R2Mount` 和采用 HMAC 身份验证的 `GCSMount`。 |
| `BlaxelSandboxClient` | 支持将 `BlaxelCloudBucketMountStrategy` 与 `S3Mount`、`R2Mount` 或 `GCSMount` 条目配对，以挂载云存储桶。还支持通过 `BlaxelDriveMount` 和 `BlaxelDriveMountStrategy` 使用持久化 Blaxel Drive，这两者均可从 `agents.extensions.sandbox.blaxel` 获取。 |
| `DaytonaSandboxClient` | 支持使用 `DaytonaCloudBucketMountStrategy` 通过 `rclone` 挂载云存储；可将其与 `S3Mount`、`GCSMount`、`R2Mount`、`AzureBlobMount` 和 `BoxMount` 配合使用。 |
| `E2BSandboxClient` | 支持使用 `E2BCloudBucketMountStrategy` 通过 `rclone` 挂载云存储；可将其与 `S3Mount`、`GCSMount`、`R2Mount`、`AzureBlobMount` 和 `BoxMount` 配合使用。 |
| `RunloopSandboxClient` | 支持使用 `RunloopCloudBucketMountStrategy` 通过 `rclone` 挂载云存储；可将其与 `S3Mount`、`GCSMount`、`R2Mount`、`AzureBlobMount` 和 `BoxMount` 配合使用。 |
| `VercelSandboxClient` | 支持将 `VercelCloudBucketMountStrategy` 与 `S3Mount` 条目配对，以挂载仅限创建时使用的 S3 和 S3 兼容存储桶；已挂载的会话无法恢复，且内联凭据要求设置 `allow_s3_credential_exposure=True`。 |

</div>

挂载表描述了每个后端能够执行哪些存储类型。对于在模型控制的沙箱内运行的挂载辅助程序，勾选标记并不会绕过凭据边界，也不表示每种策略都可以在无凭据的情况下运行。只有当所选辅助程序无需受保护权限即可运行时，Agents SDK 才会接受未经确认的容器内挂载。如果挂载需要受保护权限，而受信任的应用程序代码未明确确认要向对应的确切挂载路径暴露该权限，Agents SDK 会在启动沙箱或挂载辅助程序前拒绝该挂载。

无凭据的 `rclone` 挂载仅限 S3、GCS、R2 和 Azure Blob。容器内 Box 挂载需要非交互式身份验证来源，并需要与该来源匹配的确认。`FuseMountPattern` 需要广泛权限确认，因为即使未配置内联凭据，`blobfuse2` 也会发现环境中的 Azure 权限。类似地，`S3FilesMountPattern` 也需要广泛权限确认，因为 `mount.s3files` 会使用环境中的 IAM 权限。这些要求同样适用于以 Docker 作为后端的情况；下表中的勾选标记表示，在满足相应权限边界后，Docker 可以执行该挂载。

对于名为 `"data"` 的挂载条目，请保留由与所配置权限匹配的确认所返回的 `Manifest` 副本：

```python
# Mount-scoped values such as inline access keys.
manifest = manifest.with_in_container_mount_credential_exposure_acknowledged("data")

# Broader authority such as managed or workload identity and external credential files.
manifest = manifest.with_in_container_mount_broad_credential_exposure_acknowledged("data")
```

请传入需要确认的每个确切挂载路径。同时使用两类权限的挂载需要获得两种确认。这些确认仅在运行时有效，不会被序列化，并且允许辅助程序接收凭据，但不会将凭据用途限制在挂载路径内。请优先使用外部策略或提供商原生策略；如果无法使用，则应采用沙箱范围内、短期有效且遵循最小权限原则的凭据。

`VercelSandboxClientOptions(allow_s3_credential_exposure=True)` 仍是一个兼容性选项，适用于创建 Vercel S3 挂载时使用内联的挂载范围凭据。它不会授予广泛凭据权限。

下表汇总了每个后端可以直接挂载的远程存储条目。

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

如需更多可运行的代码示例，请浏览 [examples/sandbox/](https://github.com/openai/openai-agents-python/tree/main/examples/sandbox)，其中包含本地运行、编码、内存、任务转移和智能体组合模式；有关托管沙箱客户端，请浏览 [examples/sandbox/extensions/](https://github.com/openai/openai-agents-python/tree/main/examples/sandbox/extensions)。