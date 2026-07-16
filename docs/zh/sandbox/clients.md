---
search:
  exclude: true
---
# 沙盒客户端

使用本页选择沙盒任务应在哪里运行。大多数情况下，`SandboxAgent`定义保持不变，而沙盒客户端和客户端特定选项会在[`SandboxRunConfig`][agents.run_config.SandboxRunConfig]中变化。

!!! warning "Beta 功能"

    沙盒智能体处于 Beta 阶段。在正式发布前，API 的细节、默认值和支持的功能可能会发生变化；未来也会陆续提供更高级的功能。

## 决策指南

<div class="sandbox-nowrap-first-column-table" markdown="1">

| 目标 | 起点 | 原因 |
| --- | --- | --- |
| 在 macOS 或 Linux 上进行最快的本地迭代 | `UnixLocalSandboxClient` | 无需额外安装，便于进行简单的本地文件系统开发。 |
| 基本容器隔离 | `DockerSandboxClient` | 使用特定镜像在 Docker 中运行任务。 |
| 托管执行或生产风格隔离 | 一个托管沙盒客户端 | 将工作区边界移动到由提供商管理的环境中。 |

</div>

## 本地客户端

对于大多数用户，请从以下两个沙盒客户端之一开始：

<div class="sandbox-nowrap-first-column-table" markdown="1">

| 客户端 | 安装 | 选择场景 | 代码示例 |
| --- | --- | --- | --- |
| `UnixLocalSandboxClient` | 无 | 在 macOS 或 Linux 上进行最快的本地迭代。适合作为本地开发的默认选择。 | [Unix-local 入门示例](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/unix_local_runner.py) |
| `DockerSandboxClient` | `openai-agents[docker]` | 你需要容器隔离，或需要特定镜像来保持本地环境一致性。 | [Docker 入门示例](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/docker/docker_runner.py) |

</div>

Unix-local 是开始基于本地文件系统进行开发的最简单方式。当你需要更强的环境隔离或生产风格的一致性时，再迁移到 Docker 或托管提供商。

要从 Unix-local 切换到 Docker，请保持智能体定义不变，只更改运行配置：

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

当你需要容器隔离或镜像一致性时使用此方式。参见[examples/sandbox/docker/docker_runner.py](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/docker/docker_runner.py)。

## 挂载与远程存储

挂载条目描述要暴露哪些存储；挂载策略描述沙盒后端如何附加该存储。请从`agents.sandbox.entries`导入内置挂载条目和通用策略。托管提供商策略可从`agents.extensions.sandbox`或提供商特定的扩展包获得。

常见挂载选项：

- `mount_path`: 存储在沙盒中的显示位置。相对路径会在清单根目录下解析；绝对路径按原样使用。
- `read_only`: 默认值为`True`。仅当沙盒需要写回挂载的存储时，才设置为`False`。
- `mount_strategy`: 必填。使用同时匹配挂载条目和沙盒后端的策略。

挂载会被视为临时工作区条目。快照和持久化流程会分离或跳过已挂载路径，而不是将已挂载的远程存储复制到保存的工作区中。

通用本地/容器策略：

<div class="sandbox-nowrap-first-column-table" markdown="1">

| 策略或模式 | 适用场景 | 说明 |
| --- | --- | --- |
| `InContainerMountStrategy(pattern=RcloneMountPattern(...))` | 沙盒镜像可以运行`rclone`。 | 支持 S3、GCS、R2、Azure Blob 和 Box。`RcloneMountPattern`可以在`fuse`模式或`nfs`模式下运行。 |
| `InContainerMountStrategy(pattern=MountpointMountPattern(...))` | 镜像包含`mount-s3`，并且你需要 Mountpoint 风格的 S3 或 S3 兼容访问。 | 支持`S3Mount`和`GCSMount`。 |
| `InContainerMountStrategy(pattern=FuseMountPattern(...))` | 镜像包含`blobfuse2`并支持 FUSE。 | 支持`AzureBlobMount`。 |
| `InContainerMountStrategy(pattern=S3FilesMountPattern(...))` | 镜像包含`mount.s3files`，并且可以访问现有的 S3 Files 挂载目标。 | 支持`S3FilesMount`。 |
| `DockerVolumeMountStrategy(driver=...)` | Docker 应在容器启动前附加由卷驱动支持的挂载。 | 仅限 Docker。S3、GCS、R2、Azure Blob 和 Box 支持`rclone`；S3 和 GCS 还支持`mountpoint`。 |

</div>

## 支持的托管平台

当你需要托管环境时，通常可以沿用相同的`SandboxAgent`定义，只在[`SandboxRunConfig`][agents.run_config.SandboxRunConfig]中更改沙盒客户端。

如果你使用的是已发布的 SDK，而不是此仓库的检出版本，请通过匹配的软件包 extra 安装沙盒客户端依赖。

有关提供商特定的设置说明，以及仓库中已提交的扩展代码示例链接，请参见[examples/sandbox/extensions/README.md](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/extensions/README.md)。

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

托管沙盒客户端会提供特定于提供商的挂载策略。请选择最适合你的存储提供商的后端和挂载策略：

<div class="sandbox-nowrap-first-column-table" markdown="1">

| 后端 | 挂载说明 |
| --- | --- |
| Docker | 支持将`S3Mount`、`GCSMount`、`R2Mount`、`AzureBlobMount`、`BoxMount`和`S3FilesMount`与`InContainerMountStrategy`、`DockerVolumeMountStrategy`等本地策略配合使用。 |
| `ModalSandboxClient` | 支持在`S3Mount`、`R2Mount`和经过 HMAC 认证的`GCSMount`上使用`ModalCloudBucketMountStrategy`进行 Modal 云存储桶挂载。你可以使用内联凭据或命名的 Modal Secret。 |
| `CloudflareSandboxClient` | 支持在`S3Mount`、`R2Mount`和经过 HMAC 认证的`GCSMount`上使用`CloudflareBucketMountStrategy`进行 Cloudflare 存储桶挂载。 |
| `BlaxelSandboxClient` | 支持在`S3Mount`、`R2Mount`和`GCSMount`上使用`BlaxelCloudBucketMountStrategy`进行云存储桶挂载。还支持使用来自`agents.extensions.sandbox.blaxel`的`BlaxelDriveMount`和`BlaxelDriveMountStrategy`实现持久化 Blaxel Drives。 |
| `DaytonaSandboxClient` | 支持通过`DaytonaCloudBucketMountStrategy`进行由 rclone 支持的云存储挂载；可将其与`S3Mount`、`GCSMount`、`R2Mount`、`AzureBlobMount`和`BoxMount`配合使用。 |
| `E2BSandboxClient` | 支持通过`E2BCloudBucketMountStrategy`进行由 rclone 支持的云存储挂载；可将其与`S3Mount`、`GCSMount`、`R2Mount`、`AzureBlobMount`和`BoxMount`配合使用。 |
| `RunloopSandboxClient` | 支持通过`RunloopCloudBucketMountStrategy`进行由 rclone 支持的云存储挂载；可将其与`S3Mount`、`GCSMount`、`R2Mount`、`AzureBlobMount`和`BoxMount`配合使用。 |
| `VercelSandboxClient` | 目前未暴露特定于托管环境的挂载策略。请改用清单文件、仓库或其他工作区输入。 |

</div>

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
| `VercelSandboxClient` | - | - | - | - | - | - |

</div>

如需更多可运行代码示例，请浏览[examples/sandbox/](https://github.com/openai/openai-agents-python/tree/main/examples/sandbox)，了解本地、代码编写、记忆、任务转移和智能体组合模式；并浏览[examples/sandbox/extensions/](https://github.com/openai/openai-agents-python/tree/main/examples/sandbox/extensions)，了解托管沙盒客户端。