---
search:
  exclude: true
---
# 概念

!!! warning "Beta 功能"

    Sandbox 智能体目前处于 beta 阶段。在正式可用之前，API 的细节、默认值和支持的能力都可能发生变化，未来也会逐步提供更高级的功能。

现代智能体在能够对文件系统中的真实文件进行操作时效果最佳。**Sandbox 智能体**可以使用专门的工具和 shell 命令，对大型文档集合进行搜索和处理、编辑文件、生成产物以及运行命令。sandbox 为模型提供了一个持久化工作区，智能体可以代表你在其中完成工作。Agents SDK 中的 Sandbox 智能体帮助你轻松运行与 sandbox 环境配对的智能体，使文件进入文件系统变得简单，并通过编排 sandbox，让大规模启动、停止和恢复任务变得容易。

你可以围绕智能体所需的数据来定义工作区。它可以从 GitHub 仓库、本地文件和目录、合成任务文件、远程文件系统（如 S3 或 Azure Blob Storage）以及你提供的其他 sandbox 输入开始。

<div class="sandbox-harness-image" markdown="1">

![带计算能力的 Sandbox 智能体 harness](../assets/images/harness_with_compute.png)

</div>

`SandboxAgent` 仍然是一个 `Agent`。它保留了常规智能体的接口，例如 `instructions`、`prompt`、`tools`、`handoffs`、`mcp_servers`、`model_settings`、`output_type`、安全防护措施和 hooks，并且仍然通过常规的 `Runner` API 运行。变化之处在于执行边界：

- `SandboxAgent` 定义智能体本身：常规的智能体配置，加上 sandbox 专属默认值，例如 `default_manifest`、`base_instructions`、`run_as`，以及文件系统工具、shell 访问、skills、memory 或 compaction 等能力。
- `Manifest` 声明一个全新 sandbox 工作区的期望初始内容和布局，包括文件、仓库、挂载和环境。
- sandbox session 是命令运行和文件发生变化的实时隔离环境。
- [`SandboxRunConfig`][agents.run_config.SandboxRunConfig]决定此次运行如何获得该 sandbox session，例如直接注入一个 session、从序列化的 sandbox session 状态重新连接，或通过 sandbox client 创建一个全新的 sandbox session。
- 保存的 sandbox 状态和快照允许后续运行重新连接到先前的工作，或通过保存的内容为新的 sandbox session 提供初始数据。

`Manifest` 是全新 session 工作区的契约，而不是每个实时 sandbox 的完整事实来源。一次运行的实际工作区也可能来自复用的 sandbox session、序列化的 sandbox session 状态，或在运行时选择的快照。

在本页中，“sandbox session”指由 sandbox client 管理的实时执行环境。它不同于 [Sessions](../sessions/index.md) 中描述的 SDK 对话式 [`Session`][agents.memory.session.Session] 接口。

外层运行时仍然负责审批、追踪、任务转移和恢复记录。sandbox session 负责命令、文件更改和环境隔离。这种划分是该模型的核心部分。

### 组件关系

一次 sandbox 运行会将一个智能体定义与按次运行的 sandbox 配置结合起来。runner 会准备智能体，将其绑定到一个实时 sandbox session，并且可以保存状态供后续运行使用。

```mermaid
flowchart LR
    agent["SandboxAgent<br/><small>full Agent + sandbox defaults</small>"]
    config["SandboxRunConfig<br/><small>client / session / resume inputs</small>"]
    runner["Runner<br/><small>prepare instructions<br/>bind capability tools</small>"]
    sandbox["sandbox session<br/><small>workspace where commands run<br/>and files change</small>"]
    saved["saved state / snapshot<br/><small>for resume or fresh-start later</small>"]

    agent --> runner
    config --> runner
    runner --> sandbox
    sandbox --> saved
```

sandbox 专属默认值保留在 `SandboxAgent` 上。按次运行的 sandbox-session 选择保留在 `SandboxRunConfig` 中。

可以将其生命周期分为三个阶段来理解：

1. 使用 `SandboxAgent`、`Manifest` 和 capabilities 定义智能体以及全新工作区契约。
2. 通过向 `Runner` 提供一个 `SandboxRunConfig` 来执行运行，该配置会注入、恢复或创建 sandbox session。
3. 稍后通过 runner 管理的 `RunState`、显式的 sandbox `session_state`，或保存的工作区快照继续执行。

如果 shell 访问只是一个偶尔用到的工具，请先从[工具指南](../tools.md)中的托管 shell 开始。当工作区隔离、sandbox client 选择或 sandbox-session 恢复行为本身就是设计的一部分时，再使用 sandbox 智能体。

## 适用场景

Sandbox 智能体非常适合以工作区为中心的工作流，例如：

- 编码与调试，例如为 GitHub 仓库中的 issue 报告编排自动修复并运行有针对性的测试
- 文档处理与编辑，例如从用户的财务文档中提取信息并创建已填写的报税表草稿
- 基于文件的审查或分析，例如在回答之前检查入职材料、生成的报告或产物包
- 隔离的多智能体模式，例如为每个审查者或编码子智能体分配各自的工作区
- 多步骤工作区任务，例如一次运行中修复 bug，之后再添加回归测试，或从快照或 sandbox session 状态恢复

如果你不需要访问文件或持续存在的文件系统，继续使用 `Agent` 即可。如果 shell 访问只是偶尔需要的一项能力，可以添加托管 shell；如果工作区边界本身就是功能的一部分，请使用 sandbox 智能体。

## sandbox client 选择

本地开发时先使用 `UnixLocalSandboxClient`。当你需要容器隔离或镜像一致性时，切换到 `DockerSandboxClient`。当你需要由提供方管理执行环境时，切换到托管提供方。

在大多数情况下，`SandboxAgent` 定义保持不变，变化的是 [`SandboxRunConfig`][agents.run_config.SandboxRunConfig] 中的 sandbox client 及其选项。有关本地、Docker、托管和远程挂载选项，请参见[Sandbox clients](clients.md)。

## 核心组件

<div class="sandbox-nowrap-first-column-table" markdown="1">

| 层级 | 主要 SDK 组件 | 它回答的问题 |
| --- | --- | --- |
| 智能体定义 | `SandboxAgent`、`Manifest`、capabilities | 将运行什么智能体，以及它应从什么样的全新 session 工作区契约开始？ |
| Sandbox 执行 | `SandboxRunConfig`、sandbox client 和实时 sandbox session | 这次运行如何获得一个实时 sandbox session，工作又是在何处执行？ |
| 已保存的 sandbox 状态 | `RunState` 的 sandbox 载荷、`session_state` 和 snapshots | 该工作流如何重新连接到先前的 sandbox 工作，或通过已保存内容为新的 sandbox session 提供初始数据？ |

</div>

主要的 SDK 组件与这些层级的映射如下：

<div class="sandbox-nowrap-first-column-table" markdown="1">

| 组件 | 它负责的内容 | 问自己这个问题 |
| --- | --- | --- |
| [`SandboxAgent`][agents.sandbox.sandbox_agent.SandboxAgent] | 智能体定义 | 这个智能体应该做什么，哪些默认值应该随它一起传递？ |
| [`Manifest`][agents.sandbox.manifest.Manifest] | 全新 session 工作区中的文件和文件夹 | 运行开始时，文件系统中应该有哪些文件和文件夹？ |
| [`Capability`][agents.sandbox.capabilities.capability.Capability] | sandbox 原生行为 | 哪些工具、指令片段或运行时行为应附加到这个智能体上？ |
| [`SandboxRunConfig`][agents.run_config.SandboxRunConfig] | 按次运行的 sandbox client 和 sandbox-session 来源 | 这次运行应该注入、恢复还是创建一个 sandbox session？ |
| [`RunState`][agents.run_state.RunState] | 由 runner 管理的已保存 sandbox 状态 | 我是否在恢复一个先前由 runner 管理的工作流，并自动延续其 sandbox 状态？ |
| [`SandboxRunConfig.session_state`][agents.run_config.SandboxRunConfig.session_state] | 显式序列化的 sandbox session 状态 | 我是否想从已经在 `RunState` 之外序列化好的 sandbox 状态恢复？ |
| [`SandboxRunConfig.snapshot`][agents.run_config.SandboxRunConfig.snapshot] | 用于全新 sandbox sessions 的已保存工作区内容 | 一个新的 sandbox session 是否应该从已保存的文件和产物开始？ |

</div>

一个实用的设计顺序是：

1. 用 `Manifest` 定义全新 session 工作区契约。
2. 用 `SandboxAgent` 定义智能体。
3. 添加内置或自定义 capabilities。
4. 在 `RunConfig(sandbox=SandboxRunConfig(...))` 中决定每次运行应如何获取 sandbox session。

## sandbox 运行的准备方式

在运行时，runner 会将该定义转换为一次具体的、由 sandbox 支持的运行：

1. 它从 `SandboxRunConfig` 解析 sandbox session。  
   如果你传入 `session=...`，它会复用该实时 sandbox session。  
   否则它会使用 `client=...` 来创建或恢复一个。
2. 它确定此次运行的实际工作区输入。  
   如果此次运行注入或恢复了一个 sandbox session，则该现有 sandbox 状态优先。  
   否则 runner 会从一次性的 manifest 覆盖项或 `agent.default_manifest` 开始。  
   这就是为什么仅凭 `Manifest` 无法定义每次运行最终的实时工作区。
3. 它让 capabilities 处理生成的 manifest。  
   这样 capabilities 就可以在最终准备智能体之前，添加文件、挂载或其他工作区范围的行为。
4. 它按固定顺序构建最终指令：  
   SDK 的默认 sandbox 提示词，或者如果你显式覆盖则使用 `base_instructions`，然后是 `instructions`，然后是 capability 指令片段，再然后是任何远程挂载策略文本，最后是渲染后的文件系统树。
5. 它将 capability 工具绑定到实时 sandbox session，并通过常规 `Runner` API 运行准备好的智能体。

sandbox 化不会改变一个 turn 的含义。turn 仍然是模型的一步，而不是单条 shell 命令或单个 sandbox 操作。sandbox 侧操作与 turn 之间不存在固定的 1:1 映射：有些工作可能停留在 sandbox 执行层中，而其他动作则会返回工具结果、审批或其他需要再次调用模型的状态。实际规则是，只有当智能体运行时在 sandbox 工作发生后还需要模型再次响应时，才会消耗另一个 turn。

这些准备步骤说明了为什么在设计 `SandboxAgent` 时，`default_manifest`、`instructions`、`base_instructions`、`capabilities` 和 `run_as` 是需要重点考虑的主要 sandbox 专属选项。

## `SandboxAgent` 选项

除了常规 `Agent` 字段外，还有以下 sandbox 专属选项：

<div class="sandbox-nowrap-first-column-table" markdown="1">

| 选项 | 最佳用途 |
| --- | --- |
| `default_manifest` | 由 runner 创建的全新 sandbox sessions 的默认工作区。 |
| `instructions` | 在 SDK sandbox 提示词之后追加的额外角色、工作流和成功标准。 |
| `base_instructions` | 用于替换 SDK sandbox 提示词的高级兜底选项。 |
| `capabilities` | 应随此智能体一起传递的 sandbox 原生工具和行为。 |
| `run_as` | 面向模型的 sandbox 工具（如 shell 命令、文件读取和补丁）所使用的用户身份。 |

</div>

sandbox client 的选择、sandbox-session 的复用、manifest 覆盖以及 snapshot 选择属于 [`SandboxRunConfig`][agents.run_config.SandboxRunConfig]，而不是智能体本身。

### `default_manifest`

`default_manifest` 是当 runner 为该智能体创建全新 sandbox session 时使用的默认 [`Manifest`][agents.sandbox.manifest.Manifest]。用它来放置智能体通常应当具备的文件、仓库、辅助材料、输出目录和挂载。

这只是默认值。运行时可以通过 `SandboxRunConfig(manifest=...)` 覆盖它，而复用或恢复的 sandbox session 会保留其现有工作区状态。

### `instructions` 和 `base_instructions`

将 `instructions` 用于那些应跨不同提示词保留的简短规则。在 `SandboxAgent` 中，这些指令会追加在 SDK 的 sandbox 基础提示词之后，因此你既能保留内置的 sandbox 指导，又能添加自己的角色、工作流和成功标准。

只有在你想替换 SDK sandbox 基础提示词时，才使用 `base_instructions`。大多数智能体都不应设置它。

<div class="sandbox-nowrap-first-column-table" markdown="1">

| 放在...里 | 用途 | 示例 |
| --- | --- | --- |
| `instructions` | 智能体的稳定角色、工作流规则和成功标准。 | “检查入职文档，然后转交。”、“将最终文件写入 `output/`。” |
| `base_instructions` | 完全替代 SDK sandbox 基础提示词。 | 自定义低层 sandbox 包装提示词。 |
| 用户提示词 | 这次运行的一次性请求。 | “总结这个工作区。” |
| manifest 中的工作区文件 | 更长的任务规范、仓库本地说明或有边界的参考材料。 | `repo/task.md`、文档包、样例资料包。 |

</div>

`instructions` 的良好用法包括：

- [examples/sandbox/unix_local_pty.py](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/unix_local_pty.py) 在 PTY 状态重要时，让智能体保持在一个交互式进程中。
- [examples/sandbox/handoffs.py](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/handoffs.py) 禁止 sandbox 审查者在检查后直接回答用户。
- [examples/sandbox/tax_prep.py](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/tax_prep.py) 要求最终填写完成的文件必须实际落入 `output/`。
- [examples/sandbox/docs/coding_task.py](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/docs/coding_task.py) 固定了精确的验证命令，并澄清了相对于工作区根目录的补丁路径。

避免将用户的一次性任务复制到 `instructions` 中，避免嵌入本应属于 manifest 的长篇参考材料，避免重复内置 capabilities 已经注入的工具文档，也不要混入模型在运行时并不需要的本地安装说明。

如果你省略了 `instructions`，SDK 仍会包含默认的 sandbox 提示词。对于低层包装器来说这已经足够，但大多数面向用户的智能体仍应提供显式的 `instructions`。

### `capabilities`

Capabilities 会将 sandbox 原生行为附加到 `SandboxAgent`。它们可以在运行开始前塑造工作区、追加 sandbox 专属指令、暴露绑定到实时 sandbox session 的工具，并调整该智能体的模型行为或输入处理方式。

内置 capabilities 包括：

<div class="sandbox-nowrap-first-column-table" markdown="1">

| Capability | 在何时添加 | 说明 |
| --- | --- | --- |
| `Shell` | 智能体需要 shell 访问时。 | 添加 `exec_command`，并在 sandbox client 支持 PTY 交互时添加 `write_stdin`。 |
| `Filesystem` | 智能体需要编辑文件或检查本地图像时。 | 添加 `apply_patch` 和 `view_image`；补丁路径相对于工作区根目录。 |
| `Skills` | 你希望在 sandbox 中发现并具体化 skills 时。 | 对于 sandbox 本地的 `SKILL.md` skills，优先使用它，而不是手动挂载 `.agents` 或 `.agents/skills`。 |
| `Memory` | 后续运行应读取或生成 memory 产物时。 | 需要 `Shell`；实时更新还需要 `Filesystem`。 |
| `Compaction` | 长时间运行的流程在 compaction 项之后需要裁剪上下文时。 | 会调整模型采样和输入处理。 |

</div>

默认情况下，`SandboxAgent.capabilities` 使用 `Capabilities.default()`，其中包括 `Filesystem()`、`Shell()` 和 `Compaction()`。如果你传入 `capabilities=[...]`，该列表会替换默认值，因此请加入你仍然需要的默认 capabilities。

对于 skills，请根据你希望它们如何被具体化来选择来源：

- `Skills(lazy_from=LocalDirLazySkillSource(...))` 是较大的本地 skill 目录的良好默认选择，因为模型可以先发现索引，只加载所需内容。
- `Skills(from_=LocalDir(src=...))` 更适合你希望预先放入的小型本地打包内容。
- `Skills(from_=GitRepo(repo=..., ref=...))` 适合 skills 本身应来自某个仓库的情况。

如果你的 skills 已经以 `.agents/skills/<name>/SKILL.md` 之类的形式位于磁盘上，请将 `LocalDir(...)` 指向该源根目录，并仍然使用 `Skills(...)` 来暴露它们。除非你已有依赖不同 sandbox 内部布局的工作区契约，否则请保留默认的 `skills_path=".agents"`。

在适用时，优先使用内置 capabilities。只有当你需要内置能力未覆盖的 sandbox 专属工具或指令接口时，才编写自定义 capability。

## 概念

### Manifest

[`Manifest`][agents.sandbox.manifest.Manifest] 描述一个全新 sandbox session 的工作区。它可以设置工作区 `root`，声明文件和目录，复制本地文件，克隆 Git 仓库，附加远程存储挂载，设置环境变量，定义用户或组，并授予对工作区外特定绝对路径的访问权限。

Manifest 条目的路径相对于工作区。它们不能是绝对路径，也不能通过 `..` 逃离工作区，这使工作区契约能够在本地、Docker 和托管 client 之间保持可移植性。

将 manifest 条目用于智能体在开始工作前所需的材料：

<div class="sandbox-nowrap-first-column-table" markdown="1">

| Manifest 条目 | 用途 |
| --- | --- |
| `File`、`Dir` | 小型合成输入、辅助文件或输出目录。 |
| `LocalFile`、`LocalDir` | 应在 sandbox 中具体化的主机文件或目录。 |
| `GitRepo` | 应获取到工作区中的仓库。 |
| 挂载，例如 `S3Mount`、`GCSMount`、`R2Mount`、`AzureBlobMount`、`S3FilesMount` | 应出现在 sandbox 内部的外部存储。 |

</div>

挂载条目描述要暴露哪些存储；挂载策略描述 sandbox 后端如何附加这些存储。有关挂载选项和提供方支持，请参见 [Sandbox clients](clients.md#mounts-and-remote-storage)。

良好的 manifest 设计通常意味着保持工作区契约精简，将较长的任务配方放入工作区文件（例如 `repo/task.md`），并在指令中使用相对工作区路径，例如 `repo/task.md` 或 `output/report.md`。如果智能体使用 `Filesystem` capability 的 `apply_patch` 工具编辑文件，请记住补丁路径是相对于 sandbox 工作区根目录，而不是 shell 的 `workdir`。

仅当智能体需要访问工作区外的具体绝对路径时才使用 `extra_path_grants`，例如用于临时工具输出的 `/tmp`，或用于只读运行时环境的 `/opt/toolchain`。在后端能够执行文件系统策略的情况下，授权同时适用于 SDK 文件 API 和 shell 执行：

```python
from agents.sandbox import Manifest, SandboxPathGrant

manifest = Manifest(
    extra_path_grants=(
        SandboxPathGrant(path="/tmp"),
        SandboxPathGrant(path="/opt/toolchain", read_only=True),
    ),
)
```

Snapshots 和 `persist_workspace()` 仍然只包含工作区根目录。额外授予的路径属于运行时访问权限，而不是持久化的工作区状态。

### 权限

`Permissions` 控制 manifest 条目的文件系统权限。它针对的是 sandbox 具体化出来的文件，而不是模型权限、审批策略或 API 凭证。

默认情况下，manifest 条目对所有者可读/可写/可执行，对组和其他用户可读/可执行。当放入的文件应为私有、只读或可执行时，可以覆盖此默认设置：

```python
from agents.sandbox import FileMode, Permissions
from agents.sandbox.entries import File

private_notes = File(
    text="internal notes",
    permissions=Permissions(
        owner=FileMode.READ | FileMode.WRITE,
        group=FileMode.NONE,
        other=FileMode.NONE,
    ),
)
```

`Permissions` 分别存储 owner、group 和 other 的位，以及该条目是否为目录。你可以直接构建它，也可以使用 `Permissions.from_str(...)` 从 mode 字符串解析，或使用 `Permissions.from_mode(...)` 从操作系统 mode 推导。

用户是可以执行工作的 sandbox 身份。当你希望某个身份存在于 sandbox 中时，可以向 manifest 添加一个 `User`，然后在面向模型的 sandbox 工具（如 shell 命令、文件读取和补丁）应以该用户身份运行时设置 `SandboxAgent.run_as`。如果 `run_as` 指向的用户尚未存在于 manifest 中，runner 会为你将其添加到实际 manifest 中。

```python
from agents import Runner
from agents.run import RunConfig
from agents.sandbox import FileMode, Manifest, Permissions, SandboxAgent, SandboxRunConfig, User
from agents.sandbox.entries import Dir, LocalDir
from agents.sandbox.sandboxes.unix_local import UnixLocalSandboxClient

analyst = User(name="analyst")

agent = SandboxAgent(
    name="Dataroom analyst",
    instructions="Review the files in `dataroom/` and write findings to `output/`.",
    default_manifest=Manifest(
        # Declare the sandbox user so manifest entries can grant access to it.
        users=[analyst],
        entries={
            "dataroom": LocalDir(
                src="./dataroom",
                # Let the analyst traverse and read the mounted dataroom, but not edit it.
                group=analyst,
                permissions=Permissions(
                    owner=FileMode.READ | FileMode.EXEC,
                    group=FileMode.READ | FileMode.EXEC,
                    other=FileMode.NONE,
                ),
            ),
            "output": Dir(
                # Give the analyst a writable scratch/output directory for artifacts.
                group=analyst,
                permissions=Permissions(
                    owner=FileMode.ALL,
                    group=FileMode.ALL,
                    other=FileMode.NONE,
                ),
            ),
        },
    ),
    # Run model-facing sandbox actions as this user, so those permissions apply.
    run_as=analyst,
)

result = await Runner.run(
    agent,
    "Summarize the contracts and call out renewal dates.",
    run_config=RunConfig(
        sandbox=SandboxRunConfig(client=UnixLocalSandboxClient()),
    ),
)
```

如果你还需要文件级共享规则，请将用户与 manifest 组以及条目的 `group` 元数据结合使用。`run_as` 用户控制谁来执行 sandbox 原生操作；`Permissions` 则控制该用户在 sandbox 具体化工作区之后，可以读取、写入或执行哪些文件。

### SnapshotSpec

`SnapshotSpec` 告诉一个全新 sandbox session 应从哪里恢复已保存的工作区内容，并在结束后持久化回哪里。它是 sandbox 工作区的快照策略，而 `session_state` 则是用于恢复特定 sandbox 后端的序列化连接状态。

本地持久快照请使用 `LocalSnapshotSpec`，当你的应用提供远程 snapshot client 时请使用 `RemoteSnapshotSpec`。当本地快照设置不可用时，会回退到一个 no-op snapshot；高级调用方如果不希望工作区快照持久化，也可以显式使用它。

```python
from pathlib import Path

from agents.run import RunConfig
from agents.sandbox import LocalSnapshotSpec, SandboxRunConfig
from agents.sandbox.sandboxes.unix_local import UnixLocalSandboxClient

run_config = RunConfig(
    sandbox=SandboxRunConfig(
        client=UnixLocalSandboxClient(),
        snapshot=LocalSnapshotSpec(base_path=Path("/tmp/my-sandbox-snapshots")),
    )
)
```

当 runner 创建一个全新 sandbox session 时，sandbox client 会为该 session 构建一个 snapshot 实例。启动时，如果 snapshot 可恢复，sandbox 会在运行继续之前恢复已保存的工作区内容。清理时，由 runner 拥有的 sandbox sessions 会归档工作区，并通过 snapshot 将其持久化回去。

如果你省略 `snapshot`，运行时会在可能的情况下尝试使用默认的本地 snapshot 位置。如果无法设置，则会回退到 no-op snapshot。挂载路径和临时路径不会作为持久工作区内容复制到 snapshot 中。

### Sandbox 生命周期

有两种生命周期模式：**SDK-owned** 和 **developer-owned**。

<div class="sandbox-lifecycle-diagram" markdown="1">

```mermaid
sequenceDiagram
    participant App
    participant Runner
    participant Client
    participant Sandbox

    App->>Runner: Runner.run(..., SandboxRunConfig(client=...))
    Runner->>Client: create or resume sandbox
    Client-->>Runner: sandbox session
    Runner->>Sandbox: start, run tools
    Runner->>Sandbox: stop and persist snapshot
    Runner->>Client: delete runner-owned resources

    App->>Client: create(...)
    Client-->>App: sandbox session
    App->>Sandbox: async with sandbox
    App->>Runner: Runner.run(..., SandboxRunConfig(session=sandbox))
    Runner->>Sandbox: run tools
    App->>Sandbox: cleanup on context exit / aclose()
```

</div>

当 sandbox 只需存活一次运行时，请使用 SDK-owned 生命周期。传入 `client`、可选的 `manifest`、可选的 `snapshot` 和 client `options`；runner 会创建或恢复 sandbox，启动它，运行智能体，持久化由 snapshot 支持的工作区状态，关闭 sandbox，并让 client 清理由 runner 拥有的资源。

```python
result = await Runner.run(
    agent,
    "Inspect the workspace and summarize what changed.",
    run_config=RunConfig(
        sandbox=SandboxRunConfig(client=UnixLocalSandboxClient()),
    ),
)
```

当你希望提前创建 sandbox、在多次运行中复用同一个实时 sandbox、在运行后检查文件、对你自己创建的 sandbox 进行流式处理，或精确决定清理时机时，请使用 developer-owned 生命周期。传入 `session=...` 会告诉 runner 使用该实时 sandbox，但不会替你关闭它。

```python
sandbox = await client.create(manifest=agent.default_manifest)

async with sandbox:
    run_config = RunConfig(sandbox=SandboxRunConfig(session=sandbox))
    await Runner.run(agent, "Analyze the files.", run_config=run_config)
    await Runner.run(agent, "Write the final report.", run_config=run_config)
```

上下文管理器是常见形式：进入时启动 sandbox，退出时运行 session 清理生命周期。如果你的应用无法使用上下文管理器，请直接调用生命周期方法：

```python
sandbox = await client.create(
    manifest=agent.default_manifest,
    snapshot=LocalSnapshotSpec(base_path=Path("/tmp/my-sandbox-snapshots")),
)
try:
    await sandbox.start()
    await Runner.run(
        agent,
        "Analyze the files.",
        run_config=RunConfig(sandbox=SandboxRunConfig(session=sandbox)),
    )
    # Persist a checkpoint of the live workspace before doing more work.
    # `aclose()` also calls `stop()`, so this is only needed for an explicit mid-lifecycle save.
    await sandbox.stop()
finally:
    await sandbox.aclose()
```

`stop()` 只会持久化由 snapshot 支持的工作区内容；它不会销毁 sandbox。`aclose()` 是完整的 session 清理路径：它会运行 pre-stop hooks，调用 `stop()`，关闭 sandbox 资源，并关闭 session 范围的依赖项。

## `SandboxRunConfig` 选项

[`SandboxRunConfig`][agents.run_config.SandboxRunConfig] 保存按次运行的选项，用于决定 sandbox session 来自哪里，以及全新 session 应如何初始化。

### Sandbox 来源

这些选项决定 runner 是应复用、恢复还是创建 sandbox session：

<div class="sandbox-nowrap-first-column-table" markdown="1">

| 选项 | 使用时机 | 说明 |
| --- | --- | --- |
| `client` | 你希望 runner 为你创建、恢复并清理 sandbox sessions。 | 除非你提供一个实时 sandbox `session`，否则为必填项。 |
| `session` | 你已经自行创建了一个实时 sandbox session。 | 生命周期由调用方负责；runner 会复用该实时 sandbox session。 |
| `session_state` | 你拥有序列化的 sandbox session 状态，但没有实时 sandbox session 对象。 | 需要 `client`；runner 会从该显式状态恢复为一个自有 session。 |

</div>

在实践中，runner 会按以下顺序解析 sandbox session：

1. 如果你注入 `run_config.sandbox.session`，则直接复用该实时 sandbox session。
2. 否则，如果此次运行是从 `RunState` 恢复，则恢复其中存储的 sandbox session 状态。
3. 否则，如果你传入 `run_config.sandbox.session_state`，runner 会从该显式序列化的 sandbox session 状态恢复。
4. 否则，runner 会创建一个全新的 sandbox session。对于这个全新 session，它会在提供了 `run_config.sandbox.manifest` 时使用它，否则使用 `agent.default_manifest`。

### 全新 session 输入

这些选项仅在 runner 创建全新 sandbox session 时才有意义：

<div class="sandbox-nowrap-first-column-table" markdown="1">

| 选项 | 使用时机 | 说明 |
| --- | --- | --- |
| `manifest` | 你想要一次性的全新 session 工作区覆盖。 | 省略时回退到 `agent.default_manifest`。 |
| `snapshot` | 一个全新的 sandbox session 应从 snapshot 提供初始内容。 | 适用于类似恢复的流程或远程 snapshot clients。 |
| `options` | sandbox client 需要创建时选项。 | 常见于 Docker 镜像、Modal 应用名称、E2B 模板、超时，以及类似的 client 专属设置。 |

</div>

### 具体化控制

`concurrency_limits` 控制有多少 sandbox 具体化工作可以并行运行。当大型 manifest 或本地目录复制需要更严格的资源控制时，请使用 `SandboxConcurrencyLimits(manifest_entries=..., local_dir_files=...)`。将任一值设为 `None` 可禁用对应限制。

有几点值得注意：

- 全新 sessions：`manifest=` 和 `snapshot=` 仅在 runner 创建全新 sandbox session 时生效。
- 恢复与 snapshot：`session_state=` 会重新连接到先前序列化的 sandbox 状态，而 `snapshot=` 则会通过已保存的工作区内容为新的 sandbox session 提供初始数据。
- client 专属选项：`options=` 取决于 sandbox client；Docker 和许多托管 clients 都需要它。
- 注入的实时 sessions：如果你传入一个正在运行的 sandbox `session`，由 capability 驱动的 manifest 更新可以添加兼容的非挂载条目。它们不能更改 `manifest.root`、`manifest.environment`、`manifest.users` 或 `manifest.groups`；不能删除现有条目；不能替换条目类型；也不能添加或更改挂载条目。
- Runner API：`SandboxAgent` 的执行仍使用常规的 `Runner.run()`、`Runner.run_sync()` 和 `Runner.run_streamed()` API。

## 完整示例：编码任务

这个编码风格的示例是一个很好的默认起点：

```python
import asyncio
from pathlib import Path

from agents import ModelSettings, Runner
from agents.run import RunConfig
from agents.sandbox import Manifest, SandboxAgent, SandboxRunConfig
from agents.sandbox.capabilities import (
    Capabilities,
    LocalDirLazySkillSource,
    Skills,
)
from agents.sandbox.entries import LocalDir
from agents.sandbox.sandboxes.unix_local import UnixLocalSandboxClient

EXAMPLE_DIR = Path(__file__).resolve().parent
HOST_REPO_DIR = EXAMPLE_DIR / "repo"
HOST_SKILLS_DIR = EXAMPLE_DIR / "skills"
TARGET_TEST_CMD = "sh tests/test_credit_note.sh"


def build_agent(model: str) -> SandboxAgent[None]:
    return SandboxAgent(
        name="Sandbox engineer",
        model=model,
        instructions=(
            "Inspect the repo, make the smallest correct change, run the most relevant checks, "
            "and summarize the file changes and risks. "
            "Read `repo/task.md` before editing files. Stay grounded in the repository, preserve "
            "existing behavior, and mention the exact verification command you ran. "
            "Use the `$credit-note-fixer` skill before editing files. If the repo lives under "
            "`repo/`, remember that `apply_patch` paths stay relative to the sandbox workspace "
            "root, so edits still target `repo/...`."
        ),
        # Put repos and task files in the manifest.
        default_manifest=Manifest(
            entries={
                "repo": LocalDir(src=HOST_REPO_DIR),
            }
        ),
        capabilities=Capabilities.default() + [
            # Let Skills(...) stage and index sandbox-local skills for you.
            Skills(
                lazy_from=LocalDirLazySkillSource(
                    source=LocalDir(src=HOST_SKILLS_DIR),
                )
            ),
        ],
        model_settings=ModelSettings(tool_choice="required"),
    )


async def main(model: str, prompt: str) -> None:
    result = await Runner.run(
        build_agent(model),
        prompt,
        run_config=RunConfig(
            sandbox=SandboxRunConfig(client=UnixLocalSandboxClient()),
            workflow_name="Sandbox coding example",
        ),
    )
    print(result.final_output)


if __name__ == "__main__":
    asyncio.run(
        main(
            model="gpt-5.4",
            prompt=(
                "Open `repo/task.md`, use the `$credit-note-fixer` skill, fix the bug, "
                f"run `{TARGET_TEST_CMD}`, and summarize the change."
            ),
        )
    )
```

参见 [examples/sandbox/docs/coding_task.py](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/docs/coding_task.py)。它使用了一个基于 shell 的微型仓库，以便该示例能够在 Unix 本地运行中被确定性验证。你的真实任务仓库当然可以是 Python、JavaScript 或其他任何类型。

## 常见模式

从上面的完整示例开始。在许多情况下，同一个 `SandboxAgent` 可以保持不变，只需更改 sandbox client、sandbox-session 来源或工作区来源。

### 切换 sandbox clients

保持智能体定义不变，只更改运行配置。当你想要容器隔离或镜像一致性时使用 Docker；当你想要由提供方管理执行环境时使用托管提供方。示例和提供方选项请参见 [Sandbox clients](clients.md)。

### 覆盖工作区

保持智能体定义不变，仅替换全新 session 的 manifest：

```python
from agents.run import RunConfig
from agents.sandbox import Manifest, SandboxRunConfig
from agents.sandbox.entries import GitRepo
from agents.sandbox.sandboxes.unix_local import UnixLocalSandboxClient

run_config = RunConfig(
    sandbox=SandboxRunConfig(
        client=UnixLocalSandboxClient(),
        manifest=Manifest(
            entries={
                "repo": GitRepo(repo="openai/openai-agents-python", ref="main"),
            }
        ),
    ),
)
```

当同一智能体角色需要针对不同仓库、资料包或任务包运行，而无需重新构建智能体时，可使用此方式。上面的验证型编码示例展示了相同模式，不过使用的是 `default_manifest` 而不是一次性覆盖。

### 注入 sandbox session

当你需要显式生命周期控制、运行后检查或复制输出时，注入一个实时 sandbox session：

```python
from agents import Runner
from agents.run import RunConfig
from agents.sandbox import SandboxRunConfig
from agents.sandbox.sandboxes.unix_local import UnixLocalSandboxClient

client = UnixLocalSandboxClient()
sandbox = await client.create(manifest=agent.default_manifest)

async with sandbox:
    result = await Runner.run(
        agent,
        prompt,
        run_config=RunConfig(
            sandbox=SandboxRunConfig(session=sandbox),
        ),
    )
```

当你想在运行后检查工作区，或对一个已经启动的 sandbox session 进行流式处理时，可使用此方式。参见 [examples/sandbox/docs/coding_task.py](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/docs/coding_task.py) 和 [examples/sandbox/docker/docker_runner.py](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/docker/docker_runner.py)。

### 从 session 状态恢复

如果你已经在 `RunState` 之外序列化了 sandbox 状态，让 runner 从该状态重新连接：

```python
from agents.run import RunConfig
from agents.sandbox import SandboxRunConfig

serialized = load_saved_payload()
restored_state = client.deserialize_session_state(serialized)

run_config = RunConfig(
    sandbox=SandboxRunConfig(
        client=client,
        session_state=restored_state,
    ),
)
```

当 sandbox 状态保存在你自己的存储或作业系统中，并且你希望 `Runner` 直接从中恢复时，可使用此方式。有关序列化/反序列化流程，请参见 [examples/sandbox/extensions/blaxel_runner.py](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/extensions/blaxel_runner.py)。

### 从 snapshot 开始

通过已保存的文件和产物为一个新的 sandbox 提供初始内容：

```python
from pathlib import Path

from agents.run import RunConfig
from agents.sandbox import LocalSnapshotSpec, SandboxRunConfig
from agents.sandbox.sandboxes.unix_local import UnixLocalSandboxClient

run_config = RunConfig(
    sandbox=SandboxRunConfig(
        client=UnixLocalSandboxClient(),
        snapshot=LocalSnapshotSpec(base_path=Path("/tmp/my-sandbox-snapshot")),
    ),
)
```

当一次全新运行应从已保存的工作区内容开始，而不只是 `agent.default_manifest` 时，可使用此方式。有关本地 snapshot 流程，请参见 [examples/sandbox/memory.py](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/memory.py)；有关远程 snapshot client，请参见 [examples/sandbox/sandbox_agent_with_remote_snapshot.py](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/sandbox_agent_with_remote_snapshot.py)。

### 从 Git 加载 skills

将本地 skill 来源替换为基于仓库的来源：

```python
from agents.sandbox.capabilities import Capabilities, Skills
from agents.sandbox.entries import GitRepo

capabilities = Capabilities.default() + [
    Skills(from_=GitRepo(repo="sdcoffey/tax-prep-skills", ref="main")),
]
```

当 skills 包有自己的发布节奏，或应在多个 sandbox 之间共享时，可使用此方式。参见 [examples/sandbox/tax_prep.py](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/tax_prep.py)。

### 作为工具暴露

工具智能体既可以拥有自己的 sandbox 边界，也可以复用父运行中的实时 sandbox。复用对于快速、只读的探索型智能体很有用：它可以检查父级正在使用的确切工作区，而无需付出创建、填充或快照另一个 sandbox 的成本。

```python
from agents import Runner
from agents.run import RunConfig
from agents.sandbox import FileMode, Manifest, Permissions, SandboxAgent, SandboxRunConfig, User
from agents.sandbox.entries import Dir, File
from agents.sandbox.sandboxes.unix_local import UnixLocalSandboxClient

coordinator = User(name="coordinator")
explorer = User(name="explorer")

manifest = Manifest(
    users=[coordinator, explorer],
    entries={
        "pricing_packet": Dir(
            group=coordinator,
            permissions=Permissions(
                owner=FileMode.ALL,
                group=FileMode.ALL,
                other=FileMode.READ | FileMode.EXEC,
                directory=True,
            ),
            children={
                "pricing.md": File(
                    content=b"Pricing packet contents...",
                    group=coordinator,
                    permissions=Permissions(
                        owner=FileMode.ALL,
                        group=FileMode.ALL,
                        other=FileMode.READ,
                    ),
                ),
            },
        ),
        "work": Dir(
            group=coordinator,
            permissions=Permissions(
                owner=FileMode.ALL,
                group=FileMode.ALL,
                other=FileMode.NONE,
                directory=True,
            ),
        ),
    },
)

pricing_explorer = SandboxAgent(
    name="Pricing Explorer",
    instructions="Read `pricing_packet/` and summarize commercial risk. Do not edit files.",
    run_as=explorer,
)

client = UnixLocalSandboxClient()
sandbox = await client.create(manifest=manifest)

async with sandbox:
    shared_run_config = RunConfig(
        sandbox=SandboxRunConfig(session=sandbox),
    )

    orchestrator = SandboxAgent(
        name="Revenue Operations Coordinator",
        instructions="Coordinate the review and write final notes to `work/`.",
        run_as=coordinator,
        tools=[
            pricing_explorer.as_tool(
                tool_name="review_pricing_packet",
                tool_description="Inspect the pricing packet and summarize commercial risk.",
                run_config=shared_run_config,
                max_turns=2,
            ),
        ],
    )

    result = await Runner.run(
        orchestrator,
        "Review the pricing packet, then write final notes to `work/summary.md`.",
        run_config=shared_run_config,
    )
```

这里父智能体以 `coordinator` 身份运行，而探索工具智能体则在同一个实时 sandbox session 中以 `explorer` 身份运行。`pricing_packet/` 条目对 `other` 用户可读，因此 explorer 可以快速检查它们，但它没有写权限。`work/` 目录仅对 coordinator 的用户/组可用，因此父级可以写入最终产物，而 explorer 保持只读。

当工具智能体确实需要真正隔离时，请为它提供自己的 sandbox `RunConfig`：

```python
from docker import from_env as docker_from_env

from agents.run import RunConfig
from agents.sandbox import SandboxRunConfig
from agents.sandbox.sandboxes.docker import DockerSandboxClient, DockerSandboxClientOptions

rollout_agent.as_tool(
    tool_name="review_rollout_risk",
    tool_description="Inspect the rollout packet and summarize implementation risk.",
    run_config=RunConfig(
        sandbox=SandboxRunConfig(
            client=DockerSandboxClient(docker_from_env()),
            options=DockerSandboxClientOptions(image="python:3.14-slim"),
        ),
    ),
)
```

当工具智能体应能自由修改、运行不受信任的命令，或使用不同后端/镜像时，请使用单独的 sandbox。参见 [examples/sandbox/sandbox_agents_as_tools.py](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/sandbox_agents_as_tools.py)。

### 与本地工具和 MCP 结合

在保留 sandbox 工作区的同时，仍在同一个智能体上使用普通工具：

```python
from agents.sandbox import SandboxAgent
from agents.sandbox.capabilities import Shell

agent = SandboxAgent(
    name="Workspace reviewer",
    instructions="Inspect the workspace and call host tools when needed.",
    tools=[get_discount_approval_path],
    mcp_servers=[server],
    capabilities=[Shell()],
)
```

当工作区检查只是智能体工作的一部分时，可使用此方式。参见 [examples/sandbox/sandbox_agent_with_tools.py](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/sandbox_agent_with_tools.py)。

## Memory

当未来的 sandbox-agent 运行应从先前运行中学习时，请使用 `Memory` capability。Memory 与 SDK 的对话式 `Session` memory 是分开的：它会将经验提炼为 sandbox 工作区中的文件，之后的运行就可以读取这些文件。

有关设置、读取/生成行为、多轮对话和布局隔离，请参见[Agent memory](memory.md)。

## 组合模式

当单智能体模式清晰之后，下一个设计问题就是在更大的系统中，sandbox 边界应放在哪里。

Sandbox 智能体仍然可以与 SDK 的其他部分组合：

- [Handoffs](../handoffs.md)：将文档密集型工作从非 sandbox 的接入智能体转交给 sandbox 审查智能体。
- [Agents as tools](../tools.md#agents-as-tools)：将多个 sandbox 智能体作为工具暴露，通常是在每次 `Agent.as_tool(...)` 调用时传入 `run_config=RunConfig(sandbox=SandboxRunConfig(...))`，以便每个工具拥有自己的 sandbox 边界。
- [MCP](../mcp.md) 和常规函数工具：sandbox capabilities 可以与 `mcp_servers` 和普通 Python 工具共存。
- [Running agents](../running_agents.md)：sandbox 运行仍然使用常规 `Runner` API。

有两种模式尤其常见：

- 一个非 sandbox 智能体只在工作流中需要工作区隔离的那一部分转交给 sandbox 智能体
- 一个编排器将多个 sandbox 智能体作为工具暴露，通常每次 `Agent.as_tool(...)` 调用都使用单独的 sandbox `RunConfig`，以便每个工具拥有自己的隔离工作区

### Turns 和 sandbox 运行

将 handoffs 与 agent-as-tool 调用分开说明会更容易理解。

在 handoff 中，仍然只有一个顶层运行和一个顶层 turn 循环。活动智能体会改变，但运行不会变成嵌套。如果一个非 sandbox 的接入智能体转交给一个 sandbox 审查智能体，那么同一次运行中的下一次模型调用就会为该 sandbox 智能体准备，而该 sandbox 智能体会成为执行下一次 turn 的智能体。换句话说，handoff 改变的是同一次运行中由哪个智能体负责下一个 turn。参见 [examples/sandbox/handoffs.py](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/handoffs.py)。

而在 `Agent.as_tool(...)` 中，关系则不同。外层编排器用一个外层 turn 决定调用该工具，而这次工具调用会为 sandbox 智能体启动一次嵌套运行。该嵌套运行有自己的 turn 循环、`max_turns`、审批，以及通常也有自己的 sandbox `RunConfig`。它可能在一个嵌套 turn 中结束，也可能需要多个。从外层编排器的角度看，所有这些工作仍然位于一次工具调用之后，因此嵌套 turn 不会增加外层运行的 turn 计数。参见 [examples/sandbox/sandbox_agents_as_tools.py](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/sandbox_agents_as_tools.py)。

审批行为也遵循同样的划分：

- 在 handoffs 中，审批保留在同一个顶层运行上，因为 sandbox 智能体现在是该运行中的活动智能体
- 在 `Agent.as_tool(...)` 中，sandbox 工具智能体内部触发的审批仍会显示在外层运行上，但它们来自已存储的嵌套运行状态，并会在外层运行恢复时恢复嵌套的 sandbox 运行

## 延伸阅读

- [Quickstart](quickstart.md)：启动一个 sandbox 智能体。
- [Sandbox clients](clients.md)：选择本地、Docker、托管和挂载选项。
- [Agent memory](memory.md)：保留并复用先前 sandbox 运行中的经验。
- [examples/sandbox/](https://github.com/openai/openai-agents-python/tree/main/examples/sandbox)：可运行的本地、编码、memory、handoff 和智能体组合模式。