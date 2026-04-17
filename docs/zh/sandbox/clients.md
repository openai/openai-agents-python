---
search:
  exclude: true
---
# Sandbox 客户端

使用本页来选择 sandbox 工作应在何处运行。在大多数情况下，`SandboxAgent` 定义保持不变，而 sandbox 客户端和客户端特定选项会在 [`SandboxRunConfig`][agents.run_config.SandboxRunConfig] 中变化。

!!! warning "Beta 功能"

    sandbox 智能体目前处于 beta 阶段。预计 API 的细节、默认值以及支持的能力会在正式可用前发生变化，并且随着时间推移会提供更高级的功能。

## 决策指南

<div class="sandbox-nowrap-first-column-table" markdown="1">

| 目标 | 起步选择 | 原因 |
| --- | --- | --- |
| 在 macOS 或 Linux 上进行最快的本地迭代 | `UnixLocalSandboxClient` | 无需额外安装，适合简单的本地文件系统开发。 |
| 基本的容器隔离 | `DockerSandboxClient` | 在 Docker 内使用特定镜像运行工作。 |
| 托管执行或生产环境风格的隔离 | 托管 sandbox 客户端 | 将工作区边界转移到由提供方管理的环境中。 |

</div>

## 本地客户端

对大多数用户来说，建议从以下两个 sandbox 客户端之一开始：

<div class="sandbox-nowrap-first-column-table" markdown="1">

| 客户端 | 安装 | 适用场景 | 示例 |
| --- | --- | --- | --- |
| `UnixLocalSandboxClient` | 无 | 在 macOS 或 Linux 上进行最快的本地迭代。是本地开发的良好默认选择。 | [Unix 本地入门](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/unix_local_runner.py) |
| `DockerSandboxClient` | `openai-agents[docker]` | 你希望获得容器隔离，或为本地一致性使用特定镜像。 | [Docker 入门](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/docker/docker_runner.py) |

</div>

Unix 本地方式是在本地文件系统上开始开发的最简单方法。当你需要更强的环境隔离或生产环境风格的一致性时，再迁移到 Docker 或托管提供方。

要从 Unix 本地切换到 Docker，保持智能体定义不变，只修改 run config：

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

当你需要容器隔离或镜像一致性时，请使用这种方式。参见 [examples/sandbox/docker/docker_runner.py](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/docker/docker_runner.py)。

## 挂载与远程存储

挂载条目描述要暴露哪些存储；挂载策略描述 sandbox 后端如何附加这些存储。从 `agents.sandbox.entries` 导入内置挂载条目和通用策略。托管提供方策略可从 `agents.extensions.sandbox` 或提供方特定扩展包中获取。

常见挂载选项：

- `mount_path`：存储在 sandbox 中显示的位置。相对路径会在清单根目录下解析；绝对路径会按原样使用。
- `read_only`：默认为 `True`。仅当 sandbox 应将内容写回挂载存储时才设置为 `False`。
- `mount_strategy`：必填。使用同时与挂载条目和 sandbox 后端匹配的策略。

挂载会被视为临时工作区条目。快照和持久化流程会分离或跳过挂载路径，而不是将挂载的远程存储复制到保存的工作区中。

通用本地/容器策略：

<div class="sandbox-nowrap-first-column-table" markdown="1">

| 策略或模式 | 适用场景 | 说明 |
| --- | --- | --- |
| `InContainerMountStrategy(pattern=RcloneMountPattern(...))` | sandbox 镜像可以运行 `rclone`。 | 支持 S3、GCS、R2 和 Azure Blob。`RcloneMountPattern` 可运行于 `fuse` 模式或 `nfs` 模式。 |
| `InContainerMountStrategy(pattern=MountpointMountPattern(...))` | 镜像内有 `mount-s3`，且你希望使用 Mountpoint 风格的 S3 或 S3 兼容访问。 | 支持 `S3Mount` 和 `GCSMount`。 |
| `InContainerMountStrategy(pattern=FuseMountPattern(...))` | 镜像内有 `blobfuse2` 且支持 FUSE。 | 支持 `AzureBlobMount`。 |
| `InContainerMountStrategy(pattern=S3FilesMountPattern(...))` | 镜像内有 `mount.s3files`，并且可以访问现有的 S3 Files 挂载目标。 | 支持 `S3FilesMount`。 |
| `DockerVolumeMountStrategy(driver=...)` | Docker 应在容器启动前附加一个由 volume driver 支持的挂载。 | 仅适用于 Docker。S3、GCS、R2 和 Azure Blob 支持 `rclone`；S3 和 GCS 还支持 `mountpoint`。 |

</div>

## 支持的托管平台

当你需要托管环境时，通常相同的 `SandboxAgent` 定义可以直接沿用，仅需在 [`SandboxRunConfig`][agents.run_config.SandboxRunConfig] 中更换 sandbox 客户端。

如果你使用的是已发布的 SDK，而不是此仓库的检出版本，请通过对应的软件包 extra 安装 sandbox 客户端依赖。

有关提供方特定的设置说明以及已签入扩展示例的链接，请参见 [examples/sandbox/extensions/README.md](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/extensions/README.md)。

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

托管 sandbox 客户端会公开提供方特定的挂载策略。请选择最适合你的存储提供方的后端与挂载策略：

<div class="sandbox-nowrap-first-column-table" markdown="1">

| 后端 | 挂载说明 |
| --- | --- |
| Docker | 支持将 `S3Mount`、`GCSMount`、`R2Mount`、`AzureBlobMount` 和 `S3FilesMount` 与本地策略（如 `InContainerMountStrategy` 和 `DockerVolumeMountStrategy`）配合使用。 |
| `ModalSandboxClient` | 支持通过 `ModalCloudBucketMountStrategy` 在 `S3Mount`、`R2Mount` 和使用 HMAC 认证的 `GCSMount` 上挂载 Modal cloud bucket。你可以使用内联凭证或已命名的 Modal Secret。 |
| `CloudflareSandboxClient` | 支持通过 `CloudflareBucketMountStrategy` 在 `S3Mount`、`R2Mount` 和使用 HMAC 认证的 `GCSMount` 上挂载 Cloudflare bucket。 |
| `BlaxelSandboxClient` | 支持通过 `BlaxelCloudBucketMountStrategy` 在 `S3Mount`、`R2Mount` 和 `GCSMount` 上挂载 cloud bucket。还支持来自 `agents.extensions.sandbox.blaxel` 的 `BlaxelDriveMount` 和 `BlaxelDriveMountStrategy`，用于持久化 Blaxel Drive。 |
| `DaytonaSandboxClient` | 支持通过 `DaytonaCloudBucketMountStrategy` 挂载 cloud bucket；可与 `S3Mount`、`GCSMount`、`R2Mount` 和 `AzureBlobMount` 搭配使用。 |
| `E2BSandboxClient` | 支持通过 `E2BCloudBucketMountStrategy` 挂载 cloud bucket；可与 `S3Mount`、`GCSMount`、`R2Mount` 和 `AzureBlobMount` 搭配使用。 |
| `RunloopSandboxClient` | 支持通过 `RunloopCloudBucketMountStrategy` 挂载 cloud bucket；可与 `S3Mount`、`GCSMount`、`R2Mount` 和 `AzureBlobMount` 搭配使用。 |
| `VercelSandboxClient` | 当前未公开托管特定的挂载策略。请改用清单文件、仓库或其他工作区输入。 |

</div>

下表总结了每个后端可直接挂载的远程存储条目。

<div class="sandbox-nowrap-first-column-table" markdown="1">

| 后端 | AWS S3 | Cloudflare R2 | GCS | Azure Blob Storage | S3 Files |
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

如需更多可运行的示例，请浏览 [examples/sandbox/](https://github.com/openai/openai-agents-python/tree/main/examples/sandbox)，其中包含本地、编码、内存、任务转移和智能体组合模式；托管 sandbox 客户端示例请参见 [examples/sandbox/extensions/](https://github.com/openai/openai-agents-python/tree/main/examples/sandbox/extensions)。