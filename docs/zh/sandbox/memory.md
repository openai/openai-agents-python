---
search:
  exclude: true
---
# 智能体记忆

记忆让未来的 sandbox-agent 运行能够从先前运行中学习。它与 SDK 的对话 [`Session`](../sessions/index.md) 记忆分离；后者存储消息历史。记忆会将先前运行中的经验提炼为 sandbox 工作区中的文件。

!!! warning "Beta 功能"

    Sandbox 智能体目前处于 beta 阶段。预计 API 细节、默认值和支持能力会在正式可用前发生变化，并且随着时间推移会加入更高级功能。

记忆可以降低未来运行中的三类成本：

1. 智能体成本：如果智能体完成某个工作流花了很长时间，下一次运行应当需要更少探索。这可以减少 token 使用量和完成时间。
2. 用户成本：如果用户纠正了智能体或表达了偏好，未来运行可以记住这些反馈。这可以减少人工干预。
3. 上下文成本：如果智能体之前完成过某项任务，而用户希望在该任务基础上继续，用户不应需要查找之前的线程或重新输入全部上下文。这会让任务描述更简短。

请参阅 [examples/sandbox/memory.py](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/memory.py)，查看一个完整的两次运行示例：修复 bug、生成记忆、恢复快照，并在后续验证器运行中使用该记忆。请参阅 [examples/sandbox/memory_multi_agent_multiturn.py](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/memory_multi_agent_multiturn.py)，查看一个多轮、多智能体且具有独立记忆布局的示例。

## 启用记忆

将 `Memory()` 作为能力添加到 sandbox 智能体中。

```python
from pathlib import Path
import tempfile

from agents.sandbox import LocalSnapshotSpec, SandboxAgent
from agents.sandbox.capabilities import Filesystem, Memory, Shell

agent = SandboxAgent(
    name="Memory-enabled reviewer",
    instructions="Inspect the workspace and preserve useful lessons for follow-up runs.",
    capabilities=[Memory(), Filesystem(), Shell()],
)

with tempfile.TemporaryDirectory(prefix="sandbox-memory-example-") as snapshot_dir:
    sandbox = await client.create(
        manifest=manifest,
        snapshot=LocalSnapshotSpec(base_path=Path(snapshot_dir)),
    )
```

如果启用了读取，`Memory()` 需要 `Shell()`，它允许智能体在注入的摘要不足时读取和检索记忆文件。当启用实时记忆更新（默认）时，还需要 `Filesystem()`，它允许智能体在发现记忆过时或用户要求更新记忆时更新 `memories/MEMORY.md`。

默认情况下，记忆产物存储在 sandbox 工作区的 `memories/` 下。要在后续运行中复用它们，请通过保持相同的 live sandbox 会话，或从持久化的会话状态或快照恢复，来保留并复用整个已配置的记忆目录；全新的空 sandbox 会从空记忆开始。

`Memory()` 同时启用读取和生成记忆。对于应当读取记忆但不应生成新记忆的智能体，请使用 `Memory(generate=None)`：例如内部智能体、子智能体、检查器，或一次性工具智能体（其运行不会增加太多有效信息）。当运行应为后续生成记忆，但用户不希望当前运行受现有记忆影响时，使用 `Memory(read=None)`。

## 读取记忆

记忆读取采用渐进式披露。在运行开始时，SDK 会将一个小型摘要（`memory_summary.md`，包含通用提示、用户偏好和可用记忆）注入到智能体的开发者提示词中。这为智能体提供足够上下文，以判断先前工作是否可能相关。

当先前工作看起来相关时，智能体会在已配置的记忆索引（`memories_dir` 下的 `MEMORY.md`）中按当前任务关键词检索。仅当任务需要更多细节时，它才会打开已配置 `rollout_summaries/` 目录下对应的先前 rollout 摘要。

记忆可能会过时。系统会指示智能体仅将记忆视作指导，并以当前环境为准。默认情况下，记忆读取启用 `live_update`，因此如果智能体发现记忆过时，它可在同一次运行中更新已配置的 `MEMORY.md`。当智能体应读取记忆但不应在运行中修改记忆时（例如运行对延迟敏感），请禁用实时更新。

## 生成记忆

运行结束后，sandbox 运行时会将该次运行片段追加到一个对话文件中。累积的对话文件会在 sandbox 会话关闭时处理。

记忆生成分为两个阶段：

1. 阶段 1：对话提取。一个记忆生成模型处理一个累积对话文件并生成对话摘要。系统、开发者和推理内容会被省略。如果对话过长，会截断以适配上下文窗口，同时保留开头和结尾。它还会生成原始记忆提取：来自对话的紧凑笔记，供阶段 2 进行整合。
2. 阶段 2：布局整合。一个整合智能体读取某个记忆布局下的原始记忆，在需要更多证据时打开对话摘要，并将模式提取到 `MEMORY.md` 和 `memory_summary.md` 中。

默认工作区布局为：

```text
workspace/
├── sessions/
│   └── <rollout-id>.jsonl
└── memories/
    ├── memory_summary.md
    ├── MEMORY.md
    ├── raw_memories.md (intermediate)
    ├── phase_two_selection.json (intermediate)
    ├── raw_memories/ (intermediate)
    │   └── <rollout-id>.md
    ├── rollout_summaries/
    │   └── <rollout-id>_<slug>.md
    └── skills/
```

你可以使用 `MemoryGenerateConfig` 配置记忆生成：

```python
from agents.sandbox import MemoryGenerateConfig
from agents.sandbox.capabilities import Memory

memory = Memory(
    generate=MemoryGenerateConfig(
        max_raw_memories_for_consolidation=128,
        extra_prompt="Pay extra attention to what made the customer more satisfied or annoyed",
    ),
)
```

使用 `extra_prompt` 告诉记忆生成器在你的用例中哪些信号最重要，例如 GTM 智能体的客户与公司信息。

如果近期原始记忆超过 `max_raw_memories_for_consolidation`（默认为 256），阶段 2 仅保留最新对话中的记忆并移除较旧内容。新近性基于对话最近一次更新时间。此遗忘机制有助于让记忆反映最新环境。

## 多轮对话

对于多轮 sandbox 聊天，请将常规 SDK `Session` 与同一个 live sandbox 会话一起使用：

```python
from agents import Runner, SQLiteSession
from agents.run import RunConfig
from agents.sandbox import SandboxRunConfig

conversation_session = SQLiteSession("gtm-q2-pipeline-review")
sandbox = await client.create(manifest=agent.default_manifest)

async with sandbox:
    run_config = RunConfig(
        sandbox=SandboxRunConfig(session=sandbox),
        workflow_name="GTM memory example",
    )
    await Runner.run(
        agent,
        "Analyze data/leads.csv and identify one promising GTM segment.",
        session=conversation_session,
        run_config=run_config,
    )
    await Runner.run(
        agent,
        "Using that analysis, write a short outreach hypothesis.",
        session=conversation_session,
        run_config=run_config,
    )
```

两次运行会追加到同一个记忆对话文件，因为它们传入了同一个 SDK 对话会话（`session=conversation_session`），因此共享相同的 `session.session_id`。这与 sandbox（`sandbox`）不同，后者标识 live 工作区，不用作记忆对话 ID。阶段 1 会在 sandbox 会话关闭时看到累积对话，因此可以从整个交互中提取记忆，而不是两个彼此隔离的轮次。

如果你希望多个 `Runner.run(...)` 调用合并为一个记忆对话，请在这些调用之间传递一个稳定标识符。当记忆将某次运行关联到某个对话时，会按以下顺序解析：

1. `conversation_id`，当你将其传给 `Runner.run(...)` 时
2. `session.session_id`，当你传入 SDK `Session`（如 `SQLiteSession`）时
3. `RunConfig.group_id`，当前两者都不存在时
4. 每次运行生成的 ID，当不存在稳定标识符时

## 使用不同布局为不同智能体隔离记忆

记忆隔离基于 `MemoryLayoutConfig`，而非智能体名称。具有相同布局和相同记忆对话 ID 的智能体会共享同一个记忆对话和同一份整合记忆。布局不同的智能体会保持独立的 rollout 文件、原始记忆、`MEMORY.md` 和 `memory_summary.md`，即使它们共享同一个 sandbox 工作区。

当多个智能体共享一个 sandbox 但不应共享记忆时，请使用独立布局：

```python
from agents import SQLiteSession
from agents.sandbox import MemoryLayoutConfig, SandboxAgent
from agents.sandbox.capabilities import Filesystem, Memory, Shell

gtm_agent = SandboxAgent(
    name="GTM reviewer",
    instructions="Analyze GTM workspace data and write concise recommendations.",
    capabilities=[
        Memory(
            layout=MemoryLayoutConfig(
                memories_dir="memories/gtm",
                sessions_dir="sessions/gtm",
            )
        ),
        Filesystem(),
        Shell(),
    ],
)

engineering_agent = SandboxAgent(
    name="Engineering reviewer",
    instructions="Inspect engineering workspaces and summarize fixes and risks.",
    capabilities=[
        Memory(
            layout=MemoryLayoutConfig(
                memories_dir="memories/engineering",
                sessions_dir="sessions/engineering",
            )
        ),
        Filesystem(),
        Shell(),
    ],
)

gtm_session = SQLiteSession("gtm-q2-pipeline-review")
engineering_session = SQLiteSession("eng-invoice-test-fix")
```

这可以防止将 GTM 分析整合到工程 bug 修复记忆中，反之亦然。