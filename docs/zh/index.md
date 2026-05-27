---
search:
  exclude: true
---
# OpenAI Agents SDK

[OpenAI Agents SDK](https://github.com/openai/openai-agents-python)让你能够使用轻量、易用且抽象很少的包来构建智能体式 AI 应用。它是我们此前智能体实验项目[Swarm](https://github.com/openai/swarm/tree/main)的生产就绪升级版。Agents SDK包含一组非常小的基本组件：

-   **智能体**，即配备指令和工具的 LLM
-   **Agents as tools / 任务转移**，允许智能体将特定任务委派给其他智能体
-   **安全防护措施**，支持对智能体输入和输出进行验证

结合 Python，这些基本组件足以表达工具与智能体之间的复杂关系，并让你无需陡峭的学习曲线即可构建真实世界的应用。此外，SDK 内置**追踪**，可用于可视化和调试你的智能体式流程，也可用于评估这些流程，甚至为你的应用微调模型。

## 使用 Agents SDK 的理由

SDK 有两个核心设计原则：

1. 功能足够实用，但基本组件足够少，便于快速学习。
2. 开箱即用，同时可以精确自定义运行方式。

以下是 SDK 的主要功能：

-   **智能体循环**：内置智能体循环，可处理工具调用、将结果发送回 LLM，并持续运行直到任务完成。
-   **Python 优先**：使用内置语言特性来编排和串联智能体，而无需学习新的抽象。
-   **Agents as tools / 任务转移**：一种强大的机制，用于在多个智能体之间协调和委派工作。
-   **沙盒智能体**：在真实隔离工作区中运行专家智能体，支持由清单定义的文件、沙盒客户端选择以及可恢复的沙盒会话。
-   **安全防护措施**：与智能体执行并行运行输入验证和安全检查，并在检查未通过时快速失败。
-   **工具调用**：将任何 Python 函数转换为工具，自动生成 schema，并通过 Pydantic 进行验证。
-   **MCP 服务工具调用**：内置 MCP 服务工具集成，其工作方式与工具调用相同。
-   **会话**：用于在智能体循环中维护工作上下文的持久记忆层。
-   **人在回路**：内置机制，用于在智能体运行过程中引入人工参与。
-   **追踪**：内置追踪，用于可视化、调试和监控工作流，并支持 OpenAI 的评估、微调和蒸馏工具套件。
-   **实时智能体**：使用 `gpt-realtime-2`、自动中断检测、上下文管理、安全防护措施等构建强大的语音智能体。

## Agents SDK 与 Responses API 的选择

SDK 默认将 Responses API 用于 OpenAI模型，但它在模型调用之上增加了更高层的运行时。

在以下情况下直接使用 Responses API：

-   你希望自行掌控循环、工具分派和状态处理
-   你的工作流生命周期较短，且主要目标是返回模型响应

在以下情况下使用 Agents SDK：

-   你希望由运行时管理轮次、工具执行、安全防护措施、任务转移或会话
-   你的智能体需要生成产物，或跨多个协调步骤运行
-   你需要通过[沙盒智能体](sandbox_agents.md)获得真实工作区或可恢复执行

你不必在全局范围内二选一。许多应用会使用 SDK 处理托管工作流，并在较底层路径中直接调用 Responses API。

## 安装

```bash
pip install openai-agents
```

## Hello world 示例

```python
from agents import Agent, Runner

agent = Agent(name="Assistant", instructions="You are a helpful assistant")

result = Runner.run_sync(agent, "Write a haiku about recursion in programming.")
print(result.final_output)

# Code within the code,
# Functions calling themselves,
# Infinite loop's dance.
```

_(如果运行此示例，请确保设置 `OPENAI_API_KEY` 环境变量)_

```bash
export OPENAI_API_KEY=sk-...
```

## 入门起点

-   通过[快速入门](quickstart.md)构建你的第一个基于文本的智能体。
-   然后在[运行智能体](running_agents.md#choose-a-memory-strategy)中决定如何在多个轮次之间保留状态。
-   如果任务依赖真实文件、代码仓库或每个智能体隔离的工作区状态，请阅读[沙盒智能体快速入门](sandbox_agents.md)。
-   如果你正在任务转移和管理器式编排之间做选择，请阅读[智能体编排](multi_agent.md)。

## 路径选择

当你知道想完成的工作，但不知道应查看哪个页面时，请使用此表。

| 目标 | 从这里开始 |
| --- | --- |
| 构建第一个文本智能体，并查看一次完整运行 | [快速入门](quickstart.md) |
| 添加工具调用、托管工具或 Agents as tools | [工具](tools.md) |
| 在真实隔离工作区中运行编码、审查或文档智能体 | [沙盒智能体快速入门](sandbox_agents.md)和[沙盒客户端](sandbox/clients.md) |
| 在任务转移和管理器式编排之间做选择 | [智能体编排](multi_agent.md) |
| 在多个轮次之间保留记忆 | [运行智能体](running_agents.md#choose-a-memory-strategy)和[会话](sessions/index.md) |
| 使用 OpenAI模型、websocket 传输或非 OpenAI提供商 | [模型](models/index.md) |
| 查看输出、运行项、中断和恢复状态 | [结果](results.md) |
| 使用 `gpt-realtime-2` 构建低延迟语音智能体 | [实时智能体快速入门](realtime/quickstart.md)和[实时传输](realtime/transport.md) |
| 构建语音转文本 / 智能体 / 文本转语音流水线 | [语音流水线快速入门](voice/quickstart.md) |