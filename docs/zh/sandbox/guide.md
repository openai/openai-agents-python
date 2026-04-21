---
search:
  exclude: true
---
# 概念

!!! warning "Beta 功能"

    沙盒智能体目前处于 beta 阶段。API 的细节、默认值和支持的能力在正式可用前都可能发生变化，并且功能会随着时间推移变得更高级。

现代智能体在能够操作文件系统中的真实文件时效果最佳。**Sandbox Agents** 可以使用专门的工具和 shell 命令来搜索和处理大型文档集、编辑文件、生成产物以及运行命令。沙盒为模型提供了一个持久化工作区，智能体可以用它来代你完成工作。Agents SDK 中的沙盒智能体帮助你轻松运行与沙盒环境配对的智能体，使正确的文件进入文件系统变得简单，并对沙盒进行智能体编排，从而便于大规模启动、停止和恢复任务。

你可以围绕智能体所需的数据定义工作区。它可以从 GitHub 仓库、本地文件和目录、合成任务文件、S3 或 Azure Blob Storage 等远程文件系统，以及你提供的其他沙盒输入开始。

<div class="sandbox-harness-image" markdown="1">

![带计算能力的沙盒智能体控制框架](../assets/images/harness_with_compute.png)

</div>

`SandboxAgent` 仍然是一个 `Agent`。它保留了常规智能体接口，例如 `instructions`、`prompt`、`tools`、`handoffs`、`mcp_servers`、`model_settings`、`output_type`、安全防护措施和 hooks，并且仍通过常规的 `Runner` API 运行。变化在于执行边界：

- `SandboxAgent` 定义智能体本身：常规的智能体配置，加上沙盒专属默认值，如 `default_manifest`、`base_instructions`、`run_as`，以及文件系统工具、shell 访问、skills、内存或压缩等能力。
- `Manifest` 声明全新沙盒工作区所需的初始内容和布局，包括文件、仓库、挂载和环境。
- 沙盒会话是命令运行和文件变更发生的实时隔离环境。
- [`SandboxRunConfig`][agents.run_config.SandboxRunConfig] 决定此次运行如何获得该沙盒会话，例如直接注入一个沙盒会话、从序列化的沙盒会话状态重新连接，或通过沙盒客户端创建一个全新的沙盒会话。
- 已保存的沙盒状态和快照使后续运行能够重新连接到先前的工作，或用保存的内容为新的沙盒会话提供初始数据。

`Manifest` 是全新会话工作区的契约，而不是每个实时沙盒的完整事实来源。一次运行的实际工作区也可以来自复用的沙盒会话、序列化的沙盒会话状态，或在运行时选定的快照。

在本页中，“沙盒会话”指由沙盒客户端管理的实时执行环境。它不同于 [Sessions](../sessions/index.md) 中描述的 SDK 会话式 [`Session`][agents.memory.session.Session] 接口。

外层运行时仍负责审批、追踪、任务转移和恢复记录。沙盒会话负责命令、文件变更和环境隔离。这种划分是该模型的核心组成部分。

### 组件配合方式

一次沙盒运行会将智能体定义与每次运行的沙盒配置组合起来。运行器会准备智能体，将其绑定到一个实时沙盒会话，并可为后续运行保存状态。

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

沙盒专属默认值保留在 `SandboxAgent` 上。每次运行的沙盒会话选择保留在 `SandboxRunConfig` 中。

可以将生命周期理解为三个阶段：

1. 使用 `SandboxAgent`、`Manifest` 和能力定义智能体以及全新工作区契约。
2. 通过向 `Runner` 提供一个 `SandboxRunConfig` 来执行运行，该配置用于注入、恢复或创建沙盒会话。
3. 之后可从由运行器管理的 `RunState`、显式的沙盒 `session_state`，或已保存的工作区快照继续。

如果 shell 访问只是偶尔使用的工具，请先从[工具指南](../tools.md)中的托管 shell 开始。只有当工作区隔离、沙盒客户端选择或沙盒会话恢复行为是设计的一部分时，再使用沙盒智能体。

## 适用场景

沙盒智能体非常适合以工作区为中心的工作流，例如：

- 编码与调试，例如在 GitHub 仓库中对 issue 报告进行自动修复编排并运行定向测试
- 文档处理与编辑，例如从用户的财务文件中提取信息并创建已填写的报税表草稿
- 基于文件的审查或分析，例如在回答前检查入职材料包、生成的报告或产物包
- 隔离的多智能体模式，例如为每个审查者或编码子智能体提供其独立工作区
- 多步骤工作区任务，例如一次运行中修复 bug，之后再添加回归测试，或从快照或沙盒会话状态恢复

如果你不需要访问文件或一个活跃的文件系统，请继续使用 `Agent`。如果 shell 访问只是偶尔需要的能力，则添加托管 shell；如果工作区边界本身就是功能的一部分，则使用沙盒智能体。

## 沙盒客户端选择

本地开发时从 `UnixLocalSandboxClient` 开始。需要容器隔离或镜像一致性时，转到 `DockerSandboxClient`。需要由提供方托管执行时，转到托管提供方。

在大多数情况下，`SandboxAgent` 定义保持不变，而沙盒客户端及其选项在 [`SandboxRunConfig`][agents.run_config.SandboxRunConfig] 中变化。有关本地、Docker、托管和远程挂载选项，请参见[沙盒客户端](clients.md)。

## 核心组件

<div class="sandbox-nowrap-first-column-table" markdown="1">

| 层级 | 主要 SDK 组件 | 它回答的问题 |
| --- | --- | --- |
| 智能体定义 | `SandboxAgent`、`Manifest`、能力 | 将运行什么智能体，它应从什么样的全新会话工作区契约开始？ |
| 沙盒执行 | `SandboxRunConfig`、沙盒客户端和实时沙盒会话 | 此次运行如何获得一个实时沙盒会话，工作将在何处执行？ |
| 已保存的沙盒状态 | `RunState` 沙盒负载、`session_state` 和快照 | 此工作流如何重新连接到先前的沙盒工作，或用保存的内容为新的沙盒会话提供初始数据？ |

</div>

主要 SDK 组件与这些层级的对应关系如下：

<div class="sandbox-nowrap-first-column-table" markdown="1">

| 组件 | 它负责的内容 | 请问这个问题 |
| --- | --- | --- |
| [`SandboxAgent`][agents.sandbox.sandbox_agent.SandboxAgent] | 智能体定义 | 这个智能体应执行什么，以及哪些默认值应随其一起传递？ |
| [`Manifest`][agents.sandbox.manifest.Manifest] | 全新会话工作区中的文件和文件夹 | 运行开始时，文件系统中应有哪些文件和文件夹？ |
| [`Capability`][agents.sandbox.capabilities.capability.Capability] | 沙盒原生行为 | 哪些工具、指令片段或运行时行为应附加到这个智能体上？ |
| [`SandboxRunConfig`][agents.run_config.SandboxRunConfig] | 每次运行的沙盒客户端和沙盒会话来源 | 此次运行应注入、恢复还是创建一个沙盒会话？ |
| [`RunState`][agents.run_state.RunState] | 由运行器管理的已保存沙盒状态 | 我是否正在恢复一个先前由运行器管理的工作流，并自动延续其沙盒状态？ |
| [`SandboxRunConfig.session_state`][agents.run_config.SandboxRunConfig.session_state] | 显式序列化的沙盒会话状态 | 我是否希望从已在 `RunState` 之外序列化的沙盒状态恢复？ |
| [`SandboxRunConfig.snapshot`][agents.run_config.SandboxRunConfig.snapshot] | 为新的沙盒会话保存的工作区内容 | 新的沙盒会话是否应从保存的文件和产物开始？ |

</div>

一个实用的设计顺序是：

1. 使用 `Manifest` 定义全新会话工作区契约。
2. 使用 `SandboxAgent` 定义智能体。
3. 添加内置或自定义能力。
4. 在 `RunConfig(sandbox=SandboxRunConfig(...))` 中决定每次运行应如何获取其沙盒会话。

## 沙盒运行准备方式

在运行时，运行器会将该定义转换为一次具体的、由沙盒支持的运行：

1. 它从 `SandboxRunConfig` 解析沙盒会话。
   如果你传入 `session=...`，它会复用该实时沙盒会话。
   否则它会使用 `client=...` 来创建或恢复一个会话。
2. 它确定此次运行的实际工作区输入。
   如果运行注入或恢复了一个沙盒会话，则现有沙盒状态优先。
   否则，运行器从一次性 manifest 覆盖或 `agent.default_manifest` 开始。
   这就是为什么仅有 `Manifest` 并不能定义每次运行最终的实时工作区。
3. 它让能力处理生成的 manifest。
   这样能力就可以在最终智能体准备完成前，添加文件、挂载或其他工作区范围的行为。
4. 它按固定顺序构建最终指令：
   SDK 的默认沙盒提示词，或者如果你显式覆盖了则使用 `base_instructions`，然后是 `instructions`，接着是能力指令片段，然后是任何远程挂载策略文本，最后是渲染后的文件系统树。
5. 它将能力工具绑定到实时沙盒会话，并通过常规 `Runner` API 运行已准备好的智能体。

沙盒化不会改变一个 turn 的含义。turn 仍然是模型的一步，而不是单个 shell 命令或单次沙盒操作。沙盒侧操作与 turn 之间不存在固定的 1:1 映射：某些工作可能停留在沙盒执行层内部，而其他操作会返回工具结果、审批或其他需要模型再次响应的状态。实际规则是，只有当智能体运行时在沙盒工作发生后还需要模型再次响应时，才会消耗另一个 turn。

这些准备步骤也是为什么在设计 `SandboxAgent` 时，`default_manifest`、`instructions`、`base_instructions`、`capabilities` 和 `run_as` 是主要需要考虑的沙盒专属选项。

## `SandboxAgent` 选项

这些是在常规 `Agent` 字段基础上的沙盒专属选项：

<div class="sandbox-nowrap-first-column-table" markdown="1">

| 选项 | 最佳用途 |
| --- | --- |
| `default_manifest` | 由运行器创建的新沙盒会话的默认工作区。 |
| `instructions` | 追加在 SDK 沙盒提示词之后的额外角色、工作流和成功标准。 |
| `base_instructions` | 用于替换 SDK 沙盒提示词的高级逃生口。 |
| `capabilities` | 应随该智能体一起传递的沙盒原生工具和行为。 |
| `run_as` | 面向模型的沙盒工具（如 shell 命令、文件读取和补丁）的用户身份。 |

</div>

沙盒客户端选择、沙盒会话复用、manifest 覆盖和快照选择应放在 [`SandboxRunConfig`][agents.run_config.SandboxRunConfig] 中，而不是放在智能体上。

### `default_manifest`

`default_manifest` 是当运行器为该智能体创建新的沙盒会话时使用的默认 [`Manifest`][agents.sandbox.manifest.Manifest]。用它来定义智能体通常应具备的初始文件、仓库、辅助材料、输出目录和挂载。

这只是默认值。一次运行可以通过 `SandboxRunConfig(manifest=...)` 覆盖它，而复用或恢复的沙盒会话会保留其现有工作区状态。

### `instructions` 和 `base_instructions`

使用 `instructions` 来放置那些应跨不同提示词保留的简短规则。在 `SandboxAgent` 中，这些指令会追加在 SDK 的沙盒基础提示词之后，因此你既保留了内置沙盒指导，也可以添加自己的角色、工作流和成功标准。

仅当你想替换 SDK 沙盒基础提示词时才使用 `base_instructions`。大多数智能体都不应设置它。

<div class="sandbox-nowrap-first-column-table" markdown="1">

| 放在...中 | 用途 | 示例 |
| --- | --- | --- |
| `instructions` | 智能体的稳定角色、工作流规则和成功标准。 | “检查入职文档，然后任务转移。”、“将最终文件写入 `output/`。” |
| `base_instructions` | 对 SDK 沙盒基础提示词的完整替换。 | 自定义的底层沙盒包装提示词。 |
| 用户提示词 | 此次运行的一次性请求。 | “总结这个工作区。” |
| manifest 中的工作区文件 | 更长的任务规范、仓库本地说明或受限参考资料。 | `repo/task.md`、文档包、示例材料包。 |

</div>

`instructions` 的良好用法包括：

- [examples/sandbox/unix_local_pty.py](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/unix_local_pty.py) 在 PTY 状态重要时让智能体保持在一个交互式进程中。
- [examples/sandbox/handoffs.py](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/handoffs.py) 禁止沙盒审查智能体在检查后直接回答用户。
- [examples/sandbox/tax_prep.py](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/tax_prep.py) 要求最终填写好的文件必须实际写入 `output/`。
- [examples/sandbox/docs/coding_task.py](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/docs/coding_task.py) 固定了精确的验证命令，并明确补丁路径相对于工作区根目录。

避免将用户的一次性任务复制到 `instructions` 中，避免嵌入应属于 manifest 的长参考材料，避免重复内置能力已经注入的工具文档，也不要混入模型在运行时并不需要的本地安装说明。

如果你省略 `instructions`，SDK 仍会包含默认的沙盒提示词。对于底层包装器来说这已经足够，但大多数面向用户的智能体仍应提供明确的 `instructions`。

### `capabilities`

能力会将沙盒原生行为附加到 `SandboxAgent`。它们可以在运行开始前塑造工作区，追加沙盒专属说明，暴露绑定到实时沙盒会话的工具，并调整该智能体的模型行为或输入处理。

内置能力包括：

<div class="sandbox-nowrap-first-column-table" markdown="1">

| 能力 | 在何时添加 | 说明 |
| --- | --- | --- |
| `Shell` | 智能体需要 shell 访问时。 | 添加 `exec_command`，当沙盒客户端支持 PTY 交互时还会添加 `write_stdin`。 |
| `Filesystem` | 智能体需要编辑文件或检查本地图像时。 | 添加 `apply_patch` 和 `view_image`；补丁路径相对于工作区根目录。 |
| `Skills` | 你希望在沙盒中进行 skill 发现和实例化时。 | 对于沙盒本地的 `SKILL.md` skills，优先使用它，而不是手动挂载 `.agents` 或 `.agents/skills`。 |
| `Memory` | 后续运行应读取或生成内存产物时。 | 需要 `Shell`；实时更新还需要 `Filesystem`。 |
| `Compaction` | 长时间运行的流程在压缩项之后需要裁剪上下文时。 | 会调整模型采样和输入处理。 |

</div>

默认情况下，`SandboxAgent.capabilities` 使用 `Capabilities.default()`，其中包括 `Filesystem()`、`Shell()` 和 `Compaction()`。如果你传入 `capabilities=[...]`，该列表会替换默认值，因此请包含你仍然需要的默认能力。

对于 skills，请根据你希望它们如何实例化来选择来源：

- `Skills(lazy_from=LocalDirLazySkillSource(...))` 适合作为较大本地 skill 目录的默认选项，因为模型可以先发现索引，只加载其需要的部分。
- `Skills(from_=LocalDir(src=...))` 更适合较小的本地 bundle，并且你希望它们一开始就被准备好。
- `Skills(from_=GitRepo(repo=..., ref=...))` 适用于 skills 本身应来自某个仓库的情况。

如果你的 skills 已经以 `.agents/skills/<name>/SKILL.md` 之类的形式位于磁盘上，请将 `LocalDir(...)` 指向该源根目录，并仍然使用 `Skills(...)` 来暴露它们。除非你已有的工作区契约依赖于不同的沙盒内布局，否则请保留默认的 `skills_path=".agents"`。

当内置能力适用时，优先使用它们。只有当你需要内置能力未覆盖的沙盒专属工具或指令接口时，才编写自定义能力。

## 概念

### Manifest

[`Manifest`][agents.sandbox.manifest.Manifest] 描述一个全新沙盒会话的工作区。它可以设置工作区 `root`，声明文件和目录，复制本地文件，克隆 Git 仓库，附加远程存储挂载，设置环境变量，定义用户或组，并授予对工作区外特定绝对路径的访问权限。

Manifest 条目的路径是相对于工作区的。它们不能是绝对路径，也不能通过 `..` 逃离工作区，这使工作区契约能够在本地、Docker 和托管客户端之间保持可移植性。

对智能体在工作开始前所需的材料，请使用 manifest 条目：

<div class="sandbox-nowrap-first-column-table" markdown="1">

| Manifest 条目 | 用途 |
| --- | --- |
| `File`、`Dir` | 小型合成输入、辅助文件或输出目录。 |
| `LocalFile`、`LocalDir` | 应实例化到沙盒中的主机文件或目录。 |
| `GitRepo` | 应拉取到工作区中的仓库。 |
| 挂载，如 `S3Mount`、`GCSMount`、`R2Mount`、`AzureBlobMount`、`BoxMount`、`S3FilesMount` | 应出现在沙盒中的外部存储。 |

</div>

挂载条目描述要暴露哪些存储；挂载策略描述沙盒后端如何附加这些存储。有关挂载选项和提供方支持，请参见[沙盒客户端](clients.md#mounts-and-remote-storage)。

良好的 manifest 设计通常意味着保持工作区契约精简，将较长的任务说明放在工作区文件中，例如 `repo/task.md`，并在说明中使用相对工作区路径，例如 `repo/task.md` 或 `output/report.md`。如果智能体使用 `Filesystem` 能力的 `apply_patch` 工具编辑文件，请记住补丁路径是相对于沙盒工作区根目录，而不是 shell 的 `workdir`。

仅当智能体需要访问工作区之外的具体绝对路径时才使用 `extra_path_grants`，例如用于临时工具输出的 `/tmp`，或作为只读运行时环境的 `/opt/toolchain`。在后端能够执行文件系统策略的情况下，授权同时适用于 SDK 文件 API 和 shell 执行：

```python
from agents.sandbox import Manifest, SandboxPathGrant

manifest = Manifest(
    extra_path_grants=(
        SandboxPathGrant(path="/tmp"),
        SandboxPathGrant(path="/opt/toolchain", read_only=True),
    ),
)
```

快照和 `persist_workspace()` 仍然只包含工作区根目录。额外授予的路径是运行时访问权限，而不是持久化工作区状态。

### 权限

`Permissions` 控制 manifest 条目的文件系统权限。它针对的是沙盒实例化出来的文件，而不是模型权限、审批策略或 API 凭据。

默认情况下，manifest 条目对所有者可读/可写/可执行，对组和其他用户可读/可执行。当预置的文件应为私有、只读或可执行时，请覆盖此设置：

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

`Permissions` 存储单独的所有者、组和其他用户位，以及该条目是否为目录。你可以直接构建它，用 `Permissions.from_str(...)` 从 mode 字符串解析，或用 `Permissions.from_mode(...)` 从操作系统 mode 派生。

用户是可以执行工作的沙盒身份。当你希望某个身份存在于沙盒中时，请向 manifest 添加一个 `User`，然后在希望 shell 命令、文件读取和补丁等面向模型的沙盒工具以该用户身份运行时设置 `SandboxAgent.run_as`。如果 `run_as` 指向的用户尚未在 manifest 中，运行器会为你将其添加到实际 manifest 中。

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

如果你还需要文件级共享规则，请将用户与 manifest 组以及条目的 `group` 元数据结合使用。`run_as` 用户控制谁执行沙盒原生操作；`Permissions` 控制在沙盒实例化工作区之后，该用户可以读取、写入或执行哪些文件。

### SnapshotSpec

`SnapshotSpec` 告诉一个全新的沙盒会话应从哪里恢复已保存的工作区内容，以及将其持久化回哪里。它是沙盒工作区的快照策略，而 `session_state` 是用于恢复特定沙盒后端的序列化连接状态。

本地持久快照使用 `LocalSnapshotSpec`；当你的应用提供远程快照客户端时使用 `RemoteSnapshotSpec`。当本地快照设置不可用时，会退回使用无操作快照；高级调用方也可以在不希望工作区快照持久化时显式使用它。

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

当运行器创建一个全新的沙盒会话时，沙盒客户端会为该会话构建一个快照实例。启动时，如果快照可恢复，沙盒会在运行继续前恢复已保存的工作区内容。清理时，由运行器拥有的沙盒会话会归档工作区，并通过快照将其持久化回去。

如果你省略 `snapshot`，运行时会在可行时尝试使用默认的本地快照位置。如果无法设置，则退回到无操作快照。挂载路径和临时路径不会作为持久工作区内容复制进快照中。

### 沙盒生命周期

有两种生命周期模式：**SDK 拥有**和**开发者拥有**。

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

当沙盒只需要存活一个运行周期时，使用 SDK 拥有的生命周期。传入 `client`、可选的 `manifest`、可选的 `snapshot` 和客户端 `options`；运行器会创建或恢复沙盒、启动它、运行智能体、持久化由快照支持的工作区状态、关闭沙盒，并让客户端清理由运行器拥有的资源。

```python
result = await Runner.run(
    agent,
    "Inspect the workspace and summarize what changed.",
    run_config=RunConfig(
        sandbox=SandboxRunConfig(client=UnixLocalSandboxClient()),
    ),
)
```

当你希望预先创建沙盒、在多次运行中复用一个实时沙盒、在运行后检查文件、对你自己创建的沙盒进行流式处理，或精确决定何时清理时，请使用开发者拥有的生命周期。传入 `session=...` 会告诉运行器使用该实时沙盒，但不会替你关闭它。

```python
sandbox = await client.create(manifest=agent.default_manifest)

async with sandbox:
    run_config = RunConfig(sandbox=SandboxRunConfig(session=sandbox))
    await Runner.run(agent, "Analyze the files.", run_config=run_config)
    await Runner.run(agent, "Write the final report.", run_config=run_config)
```

上下文管理器是常见形式：进入时启动沙盒，退出时执行会话清理生命周期。如果你的应用无法使用上下文管理器，请直接调用生命周期方法：

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

`stop()` 只持久化由快照支持的工作区内容；它不会拆除沙盒。`aclose()` 是完整的会话清理路径：它会运行停止前 hook，调用 `stop()`，关闭沙盒资源，并关闭会话范围的依赖项。

## `SandboxRunConfig` 选项

[`SandboxRunConfig`][agents.run_config.SandboxRunConfig] 保存每次运行的选项，用于决定沙盒会话来自何处，以及全新会话应如何初始化。

### 沙盒来源

这些选项决定运行器应复用、恢复还是创建沙盒会话：

<div class="sandbox-nowrap-first-column-table" markdown="1">

| 选项 | 适用场景 | 说明 |
| --- | --- | --- |
| `client` | 你希望运行器为你创建、恢复和清理沙盒会话。 | 除非你提供一个实时沙盒 `session`，否则必需。 |
| `session` | 你已经自己创建了一个实时沙盒会话。 | 生命周期由调用方负责；运行器会复用该实时沙盒会话。 |
| `session_state` | 你有已序列化的沙盒会话状态，但没有实时沙盒会话对象。 | 需要 `client`；运行器会从该显式状态恢复，并作为拥有者会话管理它。 |

</div>

在实践中，运行器按以下顺序解析沙盒会话：

1. 如果你注入了 `run_config.sandbox.session`，则直接复用该实时沙盒会话。
2. 否则，如果运行是从 `RunState` 恢复的，则恢复其中存储的沙盒会话状态。
3. 否则，如果你传入了 `run_config.sandbox.session_state`，运行器会从该显式序列化的沙盒会话状态恢复。
4. 否则，运行器会创建一个全新的沙盒会话。对于这个全新会话，如果提供了 `run_config.sandbox.manifest` 则使用它，否则使用 `agent.default_manifest`。

### 全新会话输入

这些选项仅在运行器创建全新沙盒会话时才有意义：

<div class="sandbox-nowrap-first-column-table" markdown="1">

| 选项 | 适用场景 | 说明 |
| --- | --- | --- |
| `manifest` | 你希望一次性覆盖全新会话工作区。 | 省略时回退到 `agent.default_manifest`。 |
| `snapshot` | 全新的沙盒会话应从快照提供初始数据。 | 适用于类似恢复的流程或远程快照客户端。 |
| `options` | 沙盒客户端需要创建时选项。 | 常见于 Docker 镜像、Modal 应用名、E2B 模板、超时等类似的客户端专属设置。 |

</div>

### 实例化控制

`concurrency_limits` 控制有多少沙盒实例化工作可以并行运行。当大型 manifest 或本地目录复制需要更严格的资源控制时，请使用 `SandboxConcurrencyLimits(manifest_entries=..., local_dir_files=...)`。将任一值设为 `None` 可禁用对应的限制。

有几个值得记住的影响：

- 全新会话：`manifest=` 和 `snapshot=` 仅在运行器创建全新沙盒会话时生效。
- 恢复与快照：`session_state=` 重新连接到先前序列化的沙盒状态，而 `snapshot=` 则是从保存的工作区内容为一个新的沙盒会话提供初始数据。
- 客户端专属选项：`options=` 取决于沙盒客户端；Docker 和许多托管客户端都需要它。
- 注入的实时会话：如果你传入一个正在运行的沙盒 `session`，由能力驱动的 manifest 更新可以添加兼容的非挂载条目。它们不能更改 `manifest.root`、`manifest.environment`、`manifest.users` 或 `manifest.groups`；不能删除现有条目；不能替换条目类型；也不能添加或修改挂载条目。
- 运行器 API：`SandboxAgent` 执行仍使用常规的 `Runner.run()`、`Runner.run_sync()` 和 `Runner.run_streamed()` API。

## 完整示例：编码任务

这个编码风格示例是一个很好的默认起点：

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

参见 [examples/sandbox/docs/coding_task.py](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/docs/coding_task.py)。它使用了一个很小的基于 shell 的仓库，以便该示例可以在 Unix 本地运行中被确定性验证。当然，你的真实任务仓库也可以是 Python、JavaScript 或其他任何内容。

## 常见模式

从上面的完整示例开始。在很多情况下，同一个 `SandboxAgent` 可以保持不变，只改变沙盒客户端、沙盒会话来源或工作区来源。

### 切换沙盒客户端

保持智能体定义不变，只修改运行配置。当你需要容器隔离或镜像一致性时使用 Docker；当你需要提供方托管执行时使用托管提供方。示例和提供方选项请参见[沙盒客户端](clients.md)。

### 覆盖工作区

保持智能体定义不变，只替换全新会话 manifest：

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

当同一个智能体角色需要针对不同仓库、材料包或任务包运行，而无需重建智能体时，请使用此方式。上面的已验证编码示例展示了使用 `default_manifest` 而非一次性覆盖的相同模式。

### 注入沙盒会话

当你需要显式生命周期控制、运行后检查或复制输出时，注入一个实时沙盒会话：

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

当你希望在运行后检查工作区，或对一个已启动的沙盒会话进行流式处理时，请使用此方式。参见 [examples/sandbox/docs/coding_task.py](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/docs/coding_task.py) 和 [examples/sandbox/docker/docker_runner.py](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/docker/docker_runner.py)。

### 从会话状态恢复

如果你已经在 `RunState` 之外序列化了沙盒状态，让运行器从该状态重新连接：

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

当沙盒状态位于你自己的存储或任务系统中，并且你希望 `Runner` 直接从中恢复时，请使用此方式。序列化/反序列化流程请参见 [examples/sandbox/extensions/blaxel_runner.py](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/extensions/blaxel_runner.py)。

### 从快照开始

使用已保存的文件和产物为新的沙盒提供初始数据：

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

当一次新的运行应从已保存的工作区内容开始，而不只是 `agent.default_manifest` 时，请使用此方式。本地快照流程请参见 [examples/sandbox/memory.py](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/memory.py)，远程快照客户端请参见 [examples/sandbox/sandbox_agent_with_remote_snapshot.py](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/sandbox_agent_with_remote_snapshot.py)。

### 从 Git 加载 skills

将本地 skill 来源替换为基于仓库的来源：

```python
from agents.sandbox.capabilities import Capabilities, Skills
from agents.sandbox.entries import GitRepo

capabilities = Capabilities.default() + [
    Skills(from_=GitRepo(repo="sdcoffey/tax-prep-skills", ref="main")),
]
```

当 skills bundle 有其自己的发布节奏，或应在多个沙盒之间共享时，请使用此方式。参见 [examples/sandbox/tax_prep.py](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/tax_prep.py)。

### 作为工具暴露

工具智能体既可以拥有自己的沙盒边界，也可以复用父运行中的实时沙盒。复用对于一个快速的只读探索智能体很有用：它可以检查父智能体正在使用的确切工作区，而无需为创建、填充或快照另一个沙盒付出成本。

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

这里父智能体以 `coordinator` 身份运行，而探索工具智能体则以 `explorer` 身份在同一个实时沙盒会话中运行。`pricing_packet/` 条目对 `other` 用户可读，因此探索者可以快速检查它们，但没有写入位。`work/` 目录仅对协调者的用户/组可用，因此父智能体可以写入最终产物，而探索者保持只读。

当工具智能体确实需要真正的隔离时，请给它自己的沙盒 `RunConfig`：

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

当工具智能体应能自由修改、运行不受信任的命令，或使用不同的后端/镜像时，请使用单独的沙盒。参见 [examples/sandbox/sandbox_agents_as_tools.py](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/sandbox_agents_as_tools.py)。

### 与本地工具和 MCP 结合

在保留沙盒工作区的同时，仍在同一个智能体上使用普通工具：

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

当工作区检查只是智能体工作的一部分时，请使用此方式。参见 [examples/sandbox/sandbox_agent_with_tools.py](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/sandbox_agent_with_tools.py)。

## 内存

当未来的沙盒智能体运行应从先前运行中学习时，请使用 `Memory` 能力。内存不同于 SDK 的会话式 `Session` 内存：它将经验提炼为沙盒工作区中的文件，之后的运行便可以读取这些文件。

有关设置、读取/生成行为、多轮对话和布局隔离，请参见[智能体内存](memory.md)。

## 组合模式

当单智能体模式清晰后，下一个设计问题就是在更大的系统中应将沙盒边界放在哪里。

沙盒智能体仍然可以与 SDK 的其他部分组合：

- [Handoffs](../handoffs.md)：将文档密集型工作从非沙盒的接入智能体任务转移给沙盒审查智能体。
- [Agents as tools](../tools.md#agents-as-tools)：将多个沙盒智能体作为工具暴露，通常是在每次 `Agent.as_tool(...)` 调用时传入 `run_config=RunConfig(sandbox=SandboxRunConfig(...))`，以便每个工具都拥有自己的沙盒边界。
- [MCP](../mcp.md) 和普通工具调用：沙盒能力可以与 `mcp_servers` 和普通 Python 工具共存。
- [运行智能体](../running_agents.md)：沙盒运行仍使用常规 `Runner` API。

有两种模式尤其常见：

- 非沙盒智能体仅在工作流中需要工作区隔离的部分任务转移到沙盒智能体
- 一个编排器将多个沙盒智能体作为工具暴露，通常为每次 `Agent.as_tool(...)` 调用提供单独的沙盒 `RunConfig`，以便每个工具都有各自隔离的工作区

### Turns 与沙盒运行

分别解释任务转移和作为工具的智能体调用会更容易理解。

对于任务转移，仍然只有一个顶层运行和一个顶层 turn 循环。活跃智能体会变化，但运行不会变成嵌套。如果一个非沙盒接入智能体将任务转移给一个沙盒审查智能体，那么在同一运行中的下一次模型调用会为该沙盒智能体进行准备，而该沙盒智能体将成为执行下一个 turn 的智能体。换句话说，任务转移改变的是同一运行中由哪个智能体拥有下一个 turn。参见 [examples/sandbox/handoffs.py](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/handoffs.py)。

对于 `Agent.as_tool(...)`，关系则不同。外层编排器使用一个外层 turn 来决定调用工具，而该工具调用会为沙盒智能体启动一次嵌套运行。该嵌套运行有自己的 turn 循环、`max_turns`、审批，通常还有自己的沙盒 `RunConfig`。它可能在一个嵌套 turn 中完成，也可能需要多个。从外层编排器的角度看，这些工作都隐藏在一次工具调用之后，因此嵌套 turn 不会增加外层运行的 turn 计数。参见 [examples/sandbox/sandbox_agents_as_tools.py](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/sandbox_agents_as_tools.py)。

审批行为遵循相同的划分：

- 对于任务转移，审批仍保留在同一个顶层运行中，因为沙盒智能体现在是该运行中的活跃智能体
- 对于 `Agent.as_tool(...)`，在沙盒工具智能体内部触发的审批仍会出现在外层运行上，但它们来自已存储的嵌套运行状态，并会在外层运行恢复时恢复嵌套的沙盒运行

## 延伸阅读

- [快速开始](quickstart.md)：运行一个沙盒智能体。
- [沙盒客户端](clients.md)：选择本地、Docker、托管和挂载选项。
- [智能体内存](memory.md)：保留并复用先前沙盒运行中的经验。
- [examples/sandbox/](https://github.com/openai/openai-agents-python/tree/main/examples/sandbox)：可运行的本地、编码、内存、任务转移和智能体组合模式。