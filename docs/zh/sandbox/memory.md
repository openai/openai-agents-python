---
search:
  exclude: true
---
# 智能体记忆

记忆可让未来的沙盒智能体运行从先前的运行中学习。它独立于 SDK 的对话式 [`Session`](../sessions/index.md) 记忆，后者用于存储消息历史记录。记忆会将先前运行中的经验提炼为沙盒工作区中的文件。

!!! warning "Beta 功能"

    沙盒智能体目前处于 Beta 阶段。在正式发布之前，API 细节、默认值和支持的功能可能会发生变化，未来也将提供更高级的功能。

记忆可以降低未来运行中的三类成本：

1. 智能体成本：如果智能体花费很长时间才完成某个工作流，下一次运行所需的探索应该会更少。这可以减少 token 使用量和完成时间。
2. 用户成本：如果用户纠正了智能体或表达了偏好，未来的运行可以记住这些反馈。这可以减少人工干预。
3. 上下文成本：如果智能体之前完成过某项任务，而用户希望在此基础上继续推进，则用户无需查找先前的对话或重新输入所有上下文。这可以缩短任务描述。

有关完整的两次运行代码示例，请参阅 [examples/sandbox/memory.py](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/memory.py)。该示例会修复一个错误、生成记忆、恢复快照，并在后续验证器运行中使用该记忆。有关采用独立记忆布局的多轮、多智能体代码示例，请参阅 [examples/sandbox/memory_multi_agent_multiturn.py](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/memory_multi_agent_multiturn.py)。

## 记忆的启用 {#enable-memory}

将 `Memory()` 作为一项功能添加到沙盒智能体中。

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

如果启用了读取，`Memory()` 需要 `Shell()`，这样当注入的摘要信息不足时，智能体便可以读取和搜索记忆文件。启用实时记忆更新时（默认启用），还需要 `Filesystem()`，这样当智能体发现记忆已过时或用户要求更新记忆时，智能体便可以更新 `memories/MEMORY.md`。

默认情况下，记忆产物存储在沙盒工作区的 `memories/` 下。要在后续运行中复用这些产物，请通过保持使用同一个实时沙盒会话，或从已持久化的会话状态或快照中恢复，来保留并复用整个已配置的记忆目录；全新的空白沙盒最初没有任何记忆。

`Memory()` 会同时启用记忆读取和生成。对于应读取记忆但不应生成新记忆的智能体，请使用 `Memory(generate=None)`——例如，由内部智能体、子智能体、检查器或一次性工具智能体执行的运行通常不会提供太多有价值的信息。如果运行应生成供日后使用的记忆，但用户不希望该运行受现有记忆影响，请使用 `Memory(read=None)`。

## 记忆的读取 {#read-memory}

记忆读取采用渐进式披露方式。在运行开始时，SDK 会将一个简短摘要（`memory_summary.md`）注入智能体的开发者提示词，其中包含普遍有用的技巧、用户偏好以及可用记忆。这可为智能体提供足够的上下文，使其能够判断先前工作是否可能相关。

当先前工作看起来相关时，智能体会使用当前任务中的关键词，在已配置的记忆索引（`memories_dir` 下的 `MEMORY.md`）中进行搜索。只有在任务需要更多细节时，它才会打开已配置的 `rollout_summaries/` 目录下相应的先前运行摘要。

记忆可能会过时。智能体会被要求仅将记忆视为参考，并以当前环境为准。默认情况下，记忆读取会启用 `live_update`，因此如果智能体发现记忆已过时，可以在同一次运行中更新已配置的 `MEMORY.md`。如果智能体应读取记忆但不应在运行期间修改记忆，请禁用实时更新，例如对延迟敏感的运行。

## 记忆的生成 {#generate-memory}

一次运行结束后，沙盒运行时会将该运行片段追加到对话文件中。累积的对话文件会在沙盒会话关闭时进行处理。

记忆生成分为两个阶段：

1. 阶段 1：对话提取。记忆生成模型会处理一个累积的对话文件并生成对话摘要。系统、开发者和推理内容会被省略。如果对话过长，则会截断对话以适应上下文窗口，同时保留开头和结尾。模型还会生成原始记忆提取内容，即从对话中提取的精简笔记，供阶段 2 整合。
2. 阶段 2：布局整合。整合智能体会读取某个记忆布局的原始记忆，在需要更多依据时打开对话摘要，并将其中的模式提取到 `MEMORY.md` 和 `memory_summary.md` 中。

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

使用 `extra_prompt` 告知记忆生成器哪些信息对你的使用场景最为重要，例如面向市场推广（GTM）智能体的客户和公司详细信息。

如果近期原始记忆数量超过 `max_raw_memories_for_consolidation`（默认值为 256），阶段 2 将只保留最新对话中的记忆并删除较旧的记忆。新旧顺序以对话最后更新时间为准。这种遗忘机制有助于让记忆反映最新环境。

## 多轮对话 {#multi-turn-conversations}

对于多轮沙盒聊天，请将常规 SDK `Session` 与同一个实时沙盒会话结合使用：

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

两次运行都会传入同一个 SDK 对话会话（`session=conversation_session`），因此共享同一个 `session.session_id`。所以，两次运行都会追加到同一个记忆对话文件中。这不同于沙盒（`sandbox`），后者用于标识实时工作区，不会用作记忆对话 ID。沙盒会话关闭时，阶段 1 会处理累积的对话，因此可以从整个交流过程而不是两个孤立的轮次中提取记忆。

如果你希望多次 `Runner.run(...)` 调用形成一次记忆对话，请在这些调用中传入一个稳定标识符。当记忆将一次运行与某个对话关联时，会按以下顺序解析：

1. `conversation_id`，当你将其传入 `Runner.run(...)` 时
2. `session.session_id`，当你传入 SDK `Session`（例如 `SQLiteSession`）时
3. `RunConfig.group_id`，当上述两者均不存在时
4. 为每次运行生成的 ID，当不存在稳定标识符时

## 不同智能体的记忆隔离布局 {#use-different-layouts-to-isolate-memory-for-different-agents}

记忆隔离基于 `MemoryLayoutConfig`，而不是智能体名称。具有相同布局和相同记忆对话 ID 的智能体会共享一个记忆对话和一份整合后的记忆。具有不同布局的智能体则会分别保存各自的运行文件、原始记忆、`MEMORY.md` 和 `memory_summary.md`，即使它们共享同一个沙盒工作区也是如此。

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

这样可以防止 GTM 分析被整合到工程错误修复记忆中，反之亦然。