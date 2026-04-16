---
search:
  exclude: true
---
# 智能体记忆

记忆让未来的 sandbox-agent 运行能够从先前的运行中学习。它独立于 SDK 的对话式[`Session`](../sessions/index.md)记忆，后者存储的是消息历史。记忆会将先前运行中的经验提炼为 sandbox 工作区中的文件。

!!! warning "Beta 功能"

    Sandbox 智能体目前处于 beta 阶段。预计在正式可用之前，API 的细节、默认值和支持的能力都会发生变化，并且功能也会随着时间推移变得更高级。

记忆可以降低未来运行中的三类成本：

1. 智能体成本：如果智能体完成某个工作流花了很长时间，那么下一次运行应当需要更少的探索。这可以减少 token 使用量并缩短完成时间。
2. 用户成本：如果用户纠正了智能体或表达了偏好，未来的运行可以记住这些反馈。这可以减少人工干预。
3. 上下文成本：如果智能体之前完成过某项任务，而用户希望在该任务基础上继续推进，那么用户不需要去查找之前的线程，也不需要重新输入全部上下文。这会让任务描述更简短。

参见[examples/sandbox/memory.py](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/memory.py)，查看一个完整的双次运行示例：修复一个 bug、生成记忆、恢复一个快照，并在后续验证器运行中使用该记忆。另请参见[examples/sandbox/memory_multi_agent_multiturn.py](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/memory_multi_agent_multiturn.py)，查看一个包含独立记忆布局的多轮、多智能体示例。

## 启用记忆

将 `Memory()` 作为一种能力添加到 sandbox 智能体中。

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

如果启用了读取，`Memory()` 需要 `Shell()`，这样智能体就可以在注入的摘要不足时读取和搜索记忆文件。当启用实时记忆更新时（默认启用），它还需要 `Filesystem()`，这样如果智能体发现记忆已过时，或者用户要求它更新记忆，它就可以更新 `memories/MEMORY.md`。

默认情况下，记忆产物存储在 sandbox 工作区的 `memories/` 下。若要在后续运行中复用它们，请通过保持相同的实时 sandbox 会话，或从持久化的会话状态或快照中恢复，来保留并复用整个已配置的记忆目录；一个全新的空 sandbox 会以空记忆启动。

`Memory()` 同时启用记忆读取和记忆生成。对于应当读取记忆但不应生成新记忆的智能体，请使用 `Memory(generate=None)`：例如内部智能体、子智能体、检查器，或一次性工具智能体，因为它们的运行不会增加太多有效信号。当某次运行应为后续生成记忆，但用户不希望该运行受现有记忆影响时，请使用 `Memory(read=None)`。

## 读取记忆

记忆读取采用渐进式披露。在一次运行开始时，SDK 会将一个简短摘要（`memory_summary.md`）注入到智能体的开发者提示词中，其中包含通常有用的提示、用户偏好以及可用记忆。这为智能体提供了足够的上下文，以判断先前工作是否可能相关。

当先前工作看起来相关时，智能体会在已配置的记忆索引（`memories_dir` 下的 `MEMORY.md`）中搜索与当前任务相关的关键词。只有当任务需要更多细节时，它才会打开已配置 `rollout_summaries/` 目录下对应的先前 rollout 摘要。

记忆可能会过时。系统会指示智能体仅将记忆视为参考，并以当前环境为准。默认情况下，记忆读取启用了 `live_update`，因此如果智能体发现记忆已过时，它可以在同一次运行中更新已配置的 `MEMORY.md`。如果某次运行对延迟敏感，而你希望智能体读取记忆但不要在运行期间修改它，请禁用实时更新。

## 生成记忆

一次运行结束后，sandbox 运行时会将该运行片段追加到一个对话文件中。累积的对话文件会在 sandbox 会话关闭时被处理。

记忆生成包含两个阶段：

1. 阶段 1：对话提取。一个生成记忆的模型会处理一个累积的对话文件，并生成对话摘要。系统、开发者和推理内容会被省略。如果对话过长，它会被截断以适应上下文窗口，同时保留开头和结尾。它还会生成原始记忆提取：从对话中提炼出的紧凑笔记，供阶段 2 进行整合。
2. 阶段 2：布局整合。一个整合智能体会读取某个记忆布局下的原始记忆，在需要更多证据时打开对话摘要，并将模式提取到 `MEMORY.md` 和 `memory_summary.md` 中。

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

使用 `extra_prompt` 告诉记忆生成器，哪些信号对你的使用场景最重要，例如 GTM 智能体中的客户和公司细节。

如果最近的原始记忆超过 `max_raw_memories_for_consolidation`（默认为 256），阶段 2 将只保留最新对话中的记忆并移除较旧的记忆。新旧判断基于对话最后一次更新时间。这个遗忘机制有助于让记忆反映最新的环境。

## 多轮对话

对于多轮 sandbox 聊天，请将普通 SDK `Session` 与同一个实时 sandbox 会话一起使用：

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

两次运行都会追加到同一个记忆对话文件中，因为它们传入了同一个 SDK 对话会话（`session=conversation_session`），因此共享同一个 `session.session_id`。这与 sandbox（`sandbox`）不同，后者标识的是实时工作区，不会被用作记忆对话 ID。阶段 1 会在 sandbox 会话关闭时看到累积后的对话，因此它可以从整个交互中提取记忆，而不是从两个彼此孤立的轮次中提取。

如果你希望多次 `Runner.run(...)` 调用成为同一个记忆对话，请在这些调用之间传递一个稳定标识符。当记忆将某次运行关联到某个对话时，会按以下顺序解析：

1. `conversation_id`，当你将其传给 `Runner.run(...)` 时
2. `session.session_id`，当你传入 SDK `Session`（例如 `SQLiteSession`）时
3. `RunConfig.group_id`，当以上两者都不存在时
4. 每次运行生成的 ID，当不存在稳定标识符时

## 使用不同布局隔离不同智能体的记忆

记忆隔离基于 `MemoryLayoutConfig`，而不是智能体名称。具有相同布局且相同记忆对话 ID 的智能体会共享同一个记忆对话和同一份整合后的记忆。布局不同的智能体则会保留各自独立的 rollout 文件、原始记忆、`MEMORY.md` 和 `memory_summary.md`，即使它们共享同一个 sandbox 工作区也是如此。

当多个智能体共享一个 sandbox，但不应共享记忆时，请使用独立布局：

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

这样可以防止 GTM 分析被整合到工程 bug 修复记忆中，反之亦然。