---
search:
  exclude: true
---
# 沙盒客户端

使用本页面选择沙盒任务的运行位置。在大多数情况下，`SandboxAgent` 定义保持不变，仅需在 [`SandboxRunConfig`][agents.run_config.SandboxRunConfig] 中更改沙盒客户端和客户端专属选项。

!!! warning "Beta 测试功能"

    沙盒智能体目前处于 Beta 测试阶段。在正式发布之前，API 细节、默认值和支持的功能可能会发生变化，未来还将逐步提供更多高级功能。

## 决策指南

<div class="sandbox-nowrap-first-column-table" markdown="1">

| 目标 | 首选 | 原因 |
| --- | --- | --- |
| 在 macOS 或 Linux 上实现最快的本地迭代 | `UnixLocalSandboxClient` | 无需额外安装，便于在本地文件系统上开发。 |
| 基本的容器隔离 | `DockerSandboxClient` | 使用指定镜像在 Docker 内运行任务。 |
| 托管执行或生产环境级隔离 | 托管沙盒客户端 | 将工作区边界迁移至由服务提供商管理的环境。 |

</div>

## 本地客户端

对于大多数用户，建议从以下两个沙盒客户端之一开始：

<div class="sandbox-nowrap-first-column-table" markdown="1">

| 客户端 | 安装 | 适用场景 | 代码示例 |
| --- | --- | --- | --- |
| `UnixLocalSandboxClient` | 无 | 在 macOS 或 Linux 上实现最快的本地迭代。适合作为本地开发的默认选择。 | [Unix 本地入门代码示例](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/unix_local_runner.py) |
| `DockerSandboxClient` | `openai-agents[docker]` | 需要容器隔离，或使用指定镜像以确保本地环境的一致性。 | [Docker 入门代码示例](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/docker/docker_runner.py) |

</div>

Unix 本地模式是在本地文件系统上开始开发的最简单方式。当需要更强的环境隔离或与生产环境保持一致时，可以迁移到 Docker 或托管服务提供商。

`SandboxPathGrant.host_path` 仅适用于 Docker，用于将主机路径映射到容器内的另一个 POSIX 路径。Unix 本地模式仅支持相同路径的授权。有关详细信息，请参阅[清单路径授权](guide.md#manifest)。

要从 Unix 本地模式切换到 Docker，请保持智能体定义不变，仅更改运行配置：

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

当需要容器隔离或镜像一致性时，请使用此方式。请参阅 [examples/sandbox/docker/docker_runner.py](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/docker/docker_runner.py)。

## 挂载与远程存储

挂载条目用于描述要公开的存储；挂载策略用于描述沙盒后端如何连接该存储。可从 `agents.sandbox.entries` 导入内置挂载条目和通用策略。托管服务提供商的策略可从 `agents.extensions.sandbox` 或服务提供商专属扩展包中获取。

常用挂载选项：

- `mount_path`：存储在沙盒中的显示位置。相对路径基于清单根目录解析；绝对路径则按原样使用。
- `read_only`：默认为 `True`。仅当沙盒需要将内容写回已挂载存储时，才将其设置为 `False`。
- `mount_strategy`：必填。应使用同时兼容挂载条目和沙盒后端的策略。

挂载会被视为临时工作区条目。快照和持久化流程会分离或跳过已挂载路径，而不会将已挂载的远程存储复制到保存的工作区中。

通用本地和容器策略：

<div class="sandbox-nowrap-first-column-table" markdown="1">

| 策略或模式 | 适用场景 | 备注 |
| --- | --- | --- |
| `InContainerMountStrategy(pattern=RcloneMountPattern(...))` | 沙盒镜像能够运行 `rclone`。 | 支持 S3、GCS、R2、Azure Blob 和 Box。`RcloneMountPattern` 可以在 `fuse` 模式或 `nfs` 模式下运行。 |
| `InContainerMountStrategy(pattern=MountpointMountPattern(...))` | 镜像包含 `mount-s3`，并且需要以 Mountpoint 方式访问 S3 或 S3 兼容存储。 | 支持 `S3Mount` 和 `GCSMount`。 |
| `InContainerMountStrategy(pattern=FuseMountPattern(...))` | 镜像包含 `blobfuse2` 并支持 FUSE。 | 支持 `AzureBlobMount`。 |
| `InContainerMountStrategy(pattern=S3FilesMountPattern(...))` | 镜像包含 `mount.s3files`，并且可以访问现有的 S3 Files 挂载目标。 | 支持 `S3FilesMount`。 |
| `DockerVolumeMountStrategy(driver=...)` | Docker 应在容器启动前连接由卷驱动支持的挂载。 | 仅适用于 Docker。S3、GCS、R2、Azure Blob 和 Box 支持 `rclone`；S3 和 GCS 还支持 `mountpoint`。 |

</div>

## 支持的托管平台

当需要托管环境时，通常可以继续使用相同的 `SandboxAgent` 定义，仅需在 [`SandboxRunConfig`][agents.run_config.SandboxRunConfig] 中更改沙盒客户端。

如果使用已发布的 SDK，而不是此代码仓库的检出版本，请通过对应的软件包额外依赖安装沙盒客户端依赖项。

有关特定服务提供商的设置说明，以及代码仓库中扩展代码示例的链接，请参阅 [examples/sandbox/extensions/README.md](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/extensions/README.md)。

<div class="sandbox-nowrap-first-column-table" markdown="1">

| 客户端 | 安装 | 代码示例 |
| --- | --- | --- |
| `BlaxelSandboxClient` | `openai-agents[blaxel]` | [Blaxel 运行器](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/extensions/blaxel_runner.py) |
| `CloudflareSandboxClient` | `openai-agents[cloudflare]` | [Cloudflare 运行器](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/extensions/cloudflare_runner.py) |
| `DaytonaSandboxClient` | `openai-agents[daytona]` | [Daytona 运行器](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/extensions/daytona/daytona_runner.py) |
| `E2BSandboxClient` | `openai-agents[e2b]` | [E2B 运行器](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/extensions/e2b_runner.py) |
| `ModalSandboxClient` | `openai-agents[modal]` | [Modal 运行器](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/extensions/modal_runner.py) |
| `RunloopSandboxClient` | `openai-agents[runloop]` | [Runloop 运行器](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/extensions/runloop/runner.py) |
| `VercelSandboxClient` | `openai-agents[vercel]` | [Vercel 运行器](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/extensions/vercel_runner.py) |

</div>

托管沙盒客户端会提供服务提供商专属的挂载策略。请选择最适合相应存储服务提供商的后端和挂载策略：

<div class="sandbox-nowrap-first-column-table" markdown="1">

| 后端 | 挂载说明 |
| --- | --- |
| Docker | 支持通过 `InContainerMountStrategy` 和 `DockerVolumeMountStrategy` 等本地策略挂载 `S3Mount`、`GCSMount`、`R2Mount`、`AzureBlobMount`、`BoxMount` 和 `S3FilesMount`。 |
| `ModalSandboxClient` | 支持通过 `ModalCloudBucketMountStrategy`，在 `S3Mount`、`R2Mount` 和使用 HMAC 身份验证的 `GCSMount` 上挂载 Modal 云存储桶。可以使用内联凭据或具名 Modal Secret。 |
| `CloudflareSandboxClient` | 支持通过 `CloudflareBucketMountStrategy`，在 `S3Mount`、`R2Mount` 和使用 HMAC 身份验证的 `GCSMount` 上挂载 Cloudflare 存储桶。 |
| `BlaxelSandboxClient` | 支持通过 `BlaxelCloudBucketMountStrategy`，在 `S3Mount`、`R2Mount` 和 `GCSMount` 上挂载云存储桶。还支持使用 `agents.extensions.sandbox.blaxel` 中的 `BlaxelDriveMount` 和 `BlaxelDriveMountStrategy` 挂载持久化 Blaxel Drive。 |
| `DaytonaSandboxClient` | 支持通过 `DaytonaCloudBucketMountStrategy` 挂载由 rclone 支持的云存储；可将其与 `S3Mount`、`GCSMount`、`R2Mount`、`AzureBlobMount` 和 `BoxMount` 配合使用。 |
| `E2BSandboxClient` | 支持通过 `E2BCloudBucketMountStrategy` 挂载由 rclone 支持的云存储；可将其与 `S3Mount`、`GCSMount`、`R2Mount`、`AzureBlobMount` 和 `BoxMount` 配合使用。 |
| `RunloopSandboxClient` | 支持通过 `RunloopCloudBucketMountStrategy` 挂载由 rclone 支持的云存储；可将其与 `S3Mount`、`GCSMount`、`R2Mount`、`AzureBlobMount` 和 `BoxMount` 配合使用。 |
| `VercelSandboxClient` | 支持通过 `VercelCloudBucketMountStrategy`，在 `S3Mount` 上挂载仅能在创建时配置的 S3 和 S3 兼容存储桶；已挂载存储的会话无法恢复，并且使用内联凭据时必须设置 `allow_s3_credential_exposure=True`。 |

</div>

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

如需更多可运行的代码示例，请浏览 [examples/sandbox/](https://github.com/openai/openai-agents-python/tree/main/examples/sandbox)，了解本地运行、编码、记忆、任务转移和智能体组合模式；还可浏览 [examples/sandbox/extensions/](https://github.com/openai/openai-agents-python/tree/main/examples/sandbox/extensions)，查看托管沙盒客户端。