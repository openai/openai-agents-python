---
search:
  exclude: true
---
# 智能体记忆

记忆让未来的沙盒智能体运行能够从先前运行中学习。它与 SDK 的对话式 [`Session`](../sessions/index.md) 记忆分开，后者用于存储消息历史。记忆会将先前运行中的经验提炼为沙盒工作区中的文件。

!!! warning "Beta 功能"

    沙盒智能体处于 Beta 阶段。在正式可用之前，API、默认值和支持能力的细节可能会发生变化，并且随着时间推移会提供更高级的功能。

记忆可以为未来运行降低三类成本：

1. 智能体成本：如果智能体花了很长时间才完成某个工作流，下一次运行应该需要更少探索。这可以减少 token 使用量和完成时间。
2. 用户成本：如果用户纠正了智能体，或表达了偏好，未来运行可以记住这些反馈。这可以减少人工干预。
3. 上下文成本：如果智能体之前完成过一项任务，而用户想在该任务基础上继续推进，用户就不需要找到之前的线程或重新输入全部上下文。这会让任务描述更短。

请参阅 [examples/sandbox/memory.py](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/memory.py)，其中包含一个完整的两次运行代码示例：修复 bug、生成记忆、恢复快照，并在后续验证器运行中使用该记忆。请参阅 [examples/sandbox/memory_multi_agent_multiturn.py](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/memory_multi_agent_multiturn.py)，了解一个多轮、多智能体示例，其中包含独立的记忆布局。

## 记忆启用

将 `Memory()` 作为一项能力添加到沙盒智能体。

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

如果启用了读取，`Memory()` 需要 `Shell()`，这样当注入的摘要不足够时，智能体就可以读取和搜索记忆文件。当启用实时记忆更新时（默认启用），它还需要 `Filesystem()`，这样如果智能体发现记忆已过时，或用户要求它更新记忆，智能体就可以更新 `memories/MEMORY.md`。

默认情况下，记忆产物存储在沙盒工作区的 `memories/` 下。要在后续运行中复用它们，请通过保持同一个实时沙盒会话，或从持久化的会话状态或快照恢复，来保留并复用整个已配置的记忆目录；全新的空沙盒会从空记忆开始。

`Memory()` 同时启用读取记忆和生成记忆。对于应该读取记忆但不应生成新记忆的智能体，请使用 `Memory(generate=None)`：例如，内部智能体、子智能体、检查器，或运行不会增加太多信号的一次性工具智能体。当运行应该为之后生成记忆，但用户不希望该运行受现有记忆影响时，请使用 `Memory(read=None)`。

## 记忆读取

记忆读取采用渐进式披露。在运行开始时，SDK 会将一份小型摘要（`memory_summary.md`）注入到智能体的开发者提示词中，其中包含通常有用的提示、用户偏好和可用记忆。这会为智能体提供足够的上下文，以判断先前工作是否可能相关。

当先前工作看起来相关时，智能体会在已配置的记忆索引（`memories_dir` 下的 `MEMORY.md`）中搜索当前任务的关键词。只有当任务需要更多细节时，它才会打开已配置的 `rollout_summaries/` 目录下对应的先前 rollout 摘要。

记忆可能会过时。智能体会被指示仅将记忆作为指导，并信任当前环境。默认情况下，记忆读取启用 `live_update`，因此如果智能体发现记忆已过时，它可以在同一次运行中更新已配置的 `MEMORY.md`。当智能体应读取记忆但不应在运行期间修改记忆时，请禁用实时更新，例如该运行对延迟敏感时。

## 记忆生成

运行结束后，沙盒运行时会将该运行片段追加到一个对话文件中。累积的对话文件会在沙盒会话关闭时被处理。

记忆生成分为两个阶段：

1. 阶段 1：对话提取。生成记忆的模型会处理一个累积的对话文件，并生成对话摘要。system、developer 和 reasoning 内容会被省略。如果对话过长，它会被截断以适配上下文窗口，同时保留开头和结尾。它还会生成原始记忆摘录：来自对话的紧凑笔记，供阶段 2 进行整合。
2. 阶段 2：布局整合。整合智能体会读取某个记忆布局的原始记忆，在需要更多证据时打开对话摘要，并将模式提取到 `MEMORY.md` 和 `memory_summary.md` 中。

默认工作区布局如下：

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

使用 `extra_prompt` 告诉记忆生成器哪些信号对你的用例最重要，例如 GTM 智能体所需的客户和公司详细信息。

如果最近的原始记忆超过 `max_raw_memories_for_consolidation`（默认为 256），阶段 2 只保留来自最新对话的记忆，并移除较旧的记忆。新近程度基于对话上次更新的时间。这种遗忘机制有助于让记忆反映最新环境。

## 多轮对话

对于多轮沙盒聊天，请将常规 SDK `Session` 与同一个实时沙盒会话一起使用：

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

两次运行都会追加到同一个记忆对话文件，因为它们传入了同一个 SDK 对话会话（`session=conversation_session`），因此共享同一个 `session.session_id`。这不同于沙盒（`sandbox`），后者标识实时工作区，不会被用作记忆对话 ID。阶段 1 会在沙盒会话关闭时看到累积的对话，因此它可以从整个交流中提取记忆，而不是从两个孤立的轮次中提取。

如果你希望多个 `Runner.run(...)` 调用成为同一个记忆对话，请在这些调用之间传入一个稳定标识符。当记忆将某次运行与一个对话关联时，它会按以下顺序解析：

1. `conversation_id`，当你将其传给 `Runner.run(...)` 时
2. `session.session_id`，当你传入 SDK `Session`（例如 `SQLiteSession`）时
3. `RunConfig.group_id`，当上述两者都不存在时
4. 生成的每次运行 ID，当不存在稳定标识符时

## 用于隔离不同智能体记忆的不同布局

记忆隔离基于 `MemoryLayoutConfig`，而不是智能体名称。具有相同布局和相同记忆对话 ID 的智能体会共享一个记忆对话和一份整合后的记忆。具有不同布局的智能体会保留独立的 rollout 文件、原始记忆、`MEMORY.md` 和 `memory_summary.md`，即使它们共享同一个沙盒工作区也是如此。

当多个智能体共享一个沙盒但不应共享记忆时，请使用独立布局：

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

这可以防止 GTM 分析被整合到工程缺陷修复记忆中，反之亦然。