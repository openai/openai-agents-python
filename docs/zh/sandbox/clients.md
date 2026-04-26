---
search:
  exclude: true
---
# Sandbox 客户端

使用本页来选择 sandbox 工作应在哪运行。在大多数情况下，`SandboxAgent` 定义保持不变，而 sandbox 客户端和特定于客户端的选项会在 [`SandboxRunConfig`][agents.run_config.SandboxRunConfig] 中发生变化。

!!! warning "Beta 功能"

    Sandbox 智能体处于 beta 阶段。预计 API 的细节、默认值和支持的能力会在正式可用前发生变化，并且更多高级功能也会随着时间逐步推出。

## 决策指南

<div class="sandbox-nowrap-first-column-table" markdown="1">

| 目标 | 起步选择 | 原因 |
| --- | --- | --- |
| 在 macOS 或 Linux 上实现最快的本地迭代 | `UnixLocalSandboxClient` | 无需额外安装，适合简单的本地文件系统开发。 |
| 基本的容器隔离 | `DockerSandboxClient` | 在 Docker 中使用特定镜像运行工作负载。 |
| 托管执行或生产风格的隔离 | 托管 sandbox 客户端 | 将工作区边界转移到由提供商管理的环境中。 |

</div>

## 本地客户端

对于大多数用户，请从以下两种 sandbox 客户端之一开始：

<div class="sandbox-nowrap-first-column-table" markdown="1">

| 客户端 | 安装 | 适用场景 | 示例 |
| --- | --- | --- | --- |
| `UnixLocalSandboxClient` | 无 | 在 macOS 或 Linux 上进行最快的本地迭代。适合作为本地开发的默认选择。 | [Unix 本地入门](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/unix_local_runner.py) |
| `DockerSandboxClient` | `openai-agents[docker]` | 你需要容器隔离，或希望使用特定镜像来实现本地一致性。 | [Docker 入门](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/docker/docker_runner.py) |

</div>

Unix 本地方式是开始针对本地文件系统进行开发的最简单方法。当你需要更强的环境隔离或生产风格的一致性时，再迁移到 Docker 或托管提供商。

若要从 Unix 本地切换到 Docker，请保持智能体定义不变，仅修改运行配置：

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

当你需要容器隔离或镜像一致性时，请使用此方式。请参见[examples/sandbox/docker/docker_runner.py](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/docker/docker_runner.py)。

## 挂载与远程存储

挂载条目用于描述要暴露的存储；挂载策略用于描述 sandbox 后端如何附加该存储。从 `agents.sandbox.entries` 导入内置挂载条目和通用策略。托管提供商策略可从 `agents.extensions.sandbox` 或提供商专用扩展包中获取。

常见挂载选项：

- `mount_path`：存储在 sandbox 中显示的位置。相对路径会在清单根目录下解析；绝对路径会按原样使用。
- `read_only`：默认为 `True`。仅当 sandbox 需要将内容写回挂载存储时，才设置为 `False`。
- `mount_strategy`：必填。请使用同时匹配挂载条目和 sandbox 后端的策略。

挂载会被视为临时工作区条目。快照和持久化流程会分离或跳过已挂载路径，而不是将已挂载的远程存储复制到保存的工作区中。

通用本地/容器策略：

<div class="sandbox-nowrap-first-column-table" markdown="1">

| 策略或模式 | 适用场景 | 说明 |
| --- | --- | --- |
| `InContainerMountStrategy(pattern=RcloneMountPattern(...))` | sandbox 镜像可以运行 `rclone`。 | 支持 S3、GCS、R2、Azure Blob 和 Box。`RcloneMountPattern` 可在 `fuse` 模式或 `nfs` 模式下运行。 |
| `InContainerMountStrategy(pattern=MountpointMountPattern(...))` | 镜像中具有 `mount-s3`，且你希望使用 Mountpoint 风格的 S3 或兼容 S3 的访问方式。 | 支持 `S3Mount` 和 `GCSMount`。 |
| `InContainerMountStrategy(pattern=FuseMountPattern(...))` | 镜像中具有 `blobfuse2` 且支持 FUSE。 | 支持 `AzureBlobMount`。 |
| `InContainerMountStrategy(pattern=S3FilesMountPattern(...))` | 镜像中具有 `mount.s3files`，并且能够访问现有的 S3 Files 挂载目标。 | 支持 `S3FilesMount`。 |
| `DockerVolumeMountStrategy(driver=...)` | Docker 应在容器启动前附加由卷驱动支持的挂载。 | 仅适用于 Docker。S3、GCS、R2、Azure Blob 和 Box 支持 `rclone`；S3 和 GCS 还支持 `mountpoint`。 |

</div>

## 支持的托管平台

当你需要托管环境时，通常可以继续使用相同的 `SandboxAgent` 定义，而只需在 [`SandboxRunConfig`][agents.run_config.SandboxRunConfig] 中更换 sandbox 客户端。

如果你使用的是已发布的 SDK，而不是此仓库的检出版本，请通过对应的包 extra 安装 sandbox 客户端依赖。

有关特定提供商的设置说明以及仓库内扩展示例的链接，请参见[examples/sandbox/extensions/README.md](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/extensions/README.md)。

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

托管 sandbox 客户端会暴露提供商特定的挂载策略。请选择最适合你的存储提供商的后端和挂载策略：

<div class="sandbox-nowrap-first-column-table" markdown="1">

| 后端 | 挂载说明 |
| --- | --- |
| Docker | 支持将 `S3Mount`、`GCSMount`、`R2Mount`、`AzureBlobMount`、`BoxMount` 和 `S3FilesMount` 与 `InContainerMountStrategy`、`DockerVolumeMountStrategy` 等本地策略配合使用。 |
| `ModalSandboxClient` | 支持在 `S3Mount`、`R2Mount` 和使用 HMAC 认证的 `GCSMount` 上通过 `ModalCloudBucketMountStrategy` 挂载 Modal cloud bucket。你可以使用内联凭证或命名的 Modal Secret。 |
| `CloudflareSandboxClient` | 支持在 `S3Mount`、`R2Mount` 和使用 HMAC 认证的 `GCSMount` 上通过 `CloudflareBucketMountStrategy` 挂载 Cloudflare bucket。 |
| `BlaxelSandboxClient` | 支持在 `S3Mount`、`R2Mount` 和 `GCSMount` 上通过 `BlaxelCloudBucketMountStrategy` 挂载 cloud bucket。还支持来自 `agents.extensions.sandbox.blaxel` 的 `BlaxelDriveMount` 和 `BlaxelDriveMountStrategy`，用于持久化的 Blaxel Drive。 |
| `DaytonaSandboxClient` | 支持通过 `DaytonaCloudBucketMountStrategy` 挂载基于 rclone 的云存储；可与 `S3Mount`、`GCSMount`、`R2Mount`、`AzureBlobMount` 和 `BoxMount` 搭配使用。 |
| `E2BSandboxClient` | 支持通过 `E2BCloudBucketMountStrategy` 挂载基于 rclone 的云存储；可与 `S3Mount`、`GCSMount`、`R2Mount`、`AzureBlobMount` 和 `BoxMount` 搭配使用。 |
| `RunloopSandboxClient` | 支持通过 `RunloopCloudBucketMountStrategy` 挂载基于 rclone 的云存储；可与 `S3Mount`、`GCSMount`、`R2Mount`、`AzureBlobMount` 和 `BoxMount` 搭配使用。 |
| `VercelSandboxClient` | 当前未暴露托管专用的挂载策略。请改用清单文件、代码仓库或其他工作区输入方式。 |

</div>

下表总结了每个后端可以直接挂载的远程存储条目。

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
| `VercelSandboxClient` | - | - | - | - | - | - |

</div>

如需更多可运行的示例，请浏览[examples/sandbox/](https://github.com/openai/openai-agents-python/tree/main/examples/sandbox)了解本地、编码、内存、任务转移和智能体组合模式，并浏览[examples/sandbox/extensions/](https://github.com/openai/openai-agents-python/tree/main/examples/sandbox/extensions)了解托管 sandbox 客户端。