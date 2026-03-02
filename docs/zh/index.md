---
search:
  exclude: true
---
# OpenAI Agents SDK

[OpenAI Agents SDK](https://github.com/openai/openai-agents-python) 使你能够以一个轻量、易用且抽象极少的软件包来构建智能体式 AI 应用。它是我们此前面向智能体实验项目 [Swarm](https://github.com/openai/swarm/tree/main) 的生产就绪升级版。Agents SDK 仅包含一组非常精简的基本组件：

-   **智能体**，即配备了指令和工具的 LLM
-   **Agents as tools / 任务转移**，允许智能体将特定任务委派给其他智能体
-   **安全防护措施**，可对智能体输入与输出进行验证

结合 Python，这些基本组件足以表达工具与智能体之间的复杂关系，并让你无需陡峭的学习曲线就能构建真实世界应用。此外，SDK 内置了**追踪**能力，可用于可视化和调试你的智能体流程，也可用于评估流程，甚至为你的应用微调模型。

## 使用 Agents SDK 的原因

SDK 有两个核心设计原则：

1. 功能足够丰富，值得使用，同时基本组件足够精简，上手快速。
2. 开箱即用效果出色，同时你也可以精确自定义每一步行为。

以下是 SDK 的主要特性：

-   **智能体循环**：内置智能体循环，负责处理工具调用、将结果回传给 LLM，并持续执行直到任务完成。
-   **Python 优先**：使用语言内置能力进行智能体编排与链式连接，而无需学习新的抽象。
-   **Agents as tools / 任务转移**：一种强大的机制，用于在多个智能体之间协调并委派工作。
-   **安全防护措施**：与智能体执行并行运行输入验证与安全检查，并在检查不通过时快速失败。
-   **工具调用**：将任意 Python 函数转换为工具，并自动生成 schema，结合 Pydantic 提供验证能力。
-   **MCP 服务工具调用**：内置 MCP 服务工具集成，使用方式与工具调用一致。
-   **会话**：用于在智能体循环中维护工作上下文的持久化记忆层。
-   **人类参与回路**：内置机制，支持在智能体运行过程中引入人工参与。
-   **追踪**：内置追踪能力，用于工作流可视化、调试与监控，并支持 OpenAI 的评估、微调与蒸馏工具套件。
-   **实时智能体**：构建强大的语音智能体，支持自动打断检测、上下文管理、安全防护措施等功能。

## 安装

```bash
pip install openai-agents
```

## Hello World 示例

```python
from agents import Agent, Runner

agent = Agent(name="Assistant", instructions="You are a helpful assistant")

result = Runner.run_sync(agent, "Write a haiku about recursion in programming.")
print(result.final_output)

# Code within the code,
# Functions calling themselves,
# Infinite loop's dance.
```

（_如果要运行此示例，请确保已设置 `OPENAI_API_KEY` 环境变量_）

```bash
export OPENAI_API_KEY=sk-...
```

## 起步

-   使用 [Quickstart](quickstart.md) 构建你的第一个文本智能体。
-   然后在 [Running agents](running_agents.md#choose-a-memory-strategy) 中决定你希望如何在多轮之间传递状态。
-   如果你在任务转移与管理者式编排之间进行选择，请阅读 [智能体编排](multi_agent.md)。

## 路径选择

当你知道想完成的任务，但不确定该看哪一页时，可使用下表。

| 目标 | 从这里开始 |
| --- | --- |
| 构建第一个文本智能体并查看一次完整运行 | [Quickstart](quickstart.md) |
| 添加工具调用、托管工具或 agents as tools | [工具](tools.md) |
| 在任务转移与管理者式编排之间做选择 | [智能体编排](multi_agent.md) |
| 在多轮之间保留记忆 | [Running agents](running_agents.md#choose-a-memory-strategy) 和 [会话](sessions/index.md) |
| 使用 OpenAI 模型、websocket 传输或非 OpenAI 提供商 | [模型](models/index.md) |
| 查看输出、运行项、中断与恢复状态 | [结果](results.md) |
| 构建低延迟语音智能体 | [实时智能体快速入门](realtime/quickstart.md) |
| 构建 speech-to-text / 智能体 / text-to-speech 流水线 | [语音流水线快速入门](voice/quickstart.md) |