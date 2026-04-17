---
search:
  exclude: true
---
# OpenAI Agents SDK

[OpenAI Agents SDK](https://github.com/openai/openai-agents-python)让你能够以一个轻量、易用且几乎没有抽象层的包来构建智能体 AI 应用。它是我们此前用于智能体实验的项目 [Swarm](https://github.com/openai/swarm/tree/main) 的生产就绪升级版。Agents SDK 只有一小组基本组件：

-   **智能体**，即配备了 instructions 和 tools 的 LLM
-   **Agents as tools / 任务转移**，允许智能体将特定任务委派给其他智能体
-   **安全防护措施**，用于验证智能体的输入和输出

结合 Python，这些基本组件足以表达工具与智能体之间的复杂关系，并让你无需陡峭的学习曲线即可构建真实世界应用。此外，SDK 内置了**追踪**功能，可让你可视化并调试智能体工作流，还能对其进行评估，甚至为你的应用微调模型。

## 使用 Agents SDK 的原因

SDK 有两个核心设计原则：

1. 功能足够丰富，值得使用；同时基本组件足够少，能够快速上手。
2. 开箱即用，同时你也可以精确自定义实际发生的行为。

以下是 SDK 的主要特性：

-   **智能体循环**：内置智能体循环，可处理工具调用，将结果发送回 LLM，并持续运行直到任务完成。
-   **Python 优先**：使用内置语言特性来进行智能体编排与链式调用，而无需学习新的抽象。
-   **Agents as tools / 任务转移**：一种强大的机制，用于在多个智能体之间协调和委派工作。
-   **沙箱智能体**：在真实隔离的工作区中运行专用智能体，支持由清单定义的文件、沙箱客户端选择以及可恢复的沙箱会话。
-   **安全防护措施**：与智能体执行并行运行输入验证和安全检查，并在检查未通过时快速失败。
-   **工具调用**：将任意 Python 函数转换为工具，并自动生成 schema 和基于 Pydantic 的验证。
-   **MCP 服务工具调用**：内置 MCP 服务工具集成，其工作方式与工具调用相同。
-   **会话**：一个持久化记忆层，用于在智能体循环中维护工作上下文。
-   **Human in the loop**：内置机制，用于在智能体运行过程中引入人工参与。
-   **追踪**：内置追踪功能，用于可视化、调试和监控工作流，并支持 OpenAI 的评估、微调和蒸馏工具套件。
-   **Realtime Agents**：使用 `gpt-realtime-1.5` 构建强大的语音智能体，支持自动中断检测、上下文管理、安全防护措施等功能。

## Agents SDK 还是 Responses API

对于 OpenAI 模型，SDK 默认使用 Responses API，但它在模型调用之上增加了一层更高层级的运行时。

在以下情况下，直接使用 Responses API：

-   你想自己掌控循环、工具分发和状态处理
-   你的工作流生命周期较短，主要是返回模型响应

在以下情况下，使用 Agents SDK：

-   你希望运行时来管理轮次、工具执行、安全防护措施、任务转移或会话
-   你的智能体需要产出工件，或跨多个协调步骤运行
-   你需要真实工作区或通过[沙箱智能体](sandbox_agents.md)实现可恢复执行

你不需要在全局范围内二选一。很多应用会使用 SDK 来管理工作流，同时在更底层的路径中直接调用 Responses API。

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

## 入门路径

-   通过[快速开始](quickstart.md)构建你的第一个基于文本的智能体。
-   然后在[运行智能体](running_agents.md#choose-a-memory-strategy)中决定如何在多轮之间保留状态。
-   如果任务依赖真实文件、代码仓库或按智能体隔离的工作区状态，请阅读[沙箱智能体快速开始](sandbox_agents.md)。
-   如果你正在权衡任务转移与 manager 风格编排，请阅读[智能体编排](multi_agent.md)。

## 路径选择

当你知道自己想做什么，但不确定该看哪一页时，可使用下表。

| 目标 | 从这里开始 |
| --- | --- |
| 构建第一个文本智能体并查看一次完整运行 | [快速开始](quickstart.md) |
| 添加工具调用、托管工具或 Agents as tools | [工具](tools.md) |
| 在真实隔离工作区中运行编码、审查或文档智能体 | [沙箱智能体快速开始](sandbox_agents.md) 和 [沙箱客户端](sandbox/clients.md) |
| 在任务转移与 manager 风格编排之间做出选择 | [智能体编排](multi_agent.md) |
| 在多轮之间保留记忆 | [运行智能体](running_agents.md#choose-a-memory-strategy) 和 [会话](sessions/index.md) |
| 使用 OpenAI 模型、websocket 传输或非 OpenAI 提供方 | [模型](models/index.md) |
| 查看输出、运行项、中断和恢复状态 | [结果](results.md) |
| 使用 `gpt-realtime-1.5` 构建低延迟语音智能体 | [Realtime agents 快速开始](realtime/quickstart.md) 和 [Realtime transport](realtime/transport.md) |
| 构建 speech-to-text / 智能体 / text-to-speech 流水线 | [语音流水线快速开始](voice/quickstart.md) |