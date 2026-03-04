---
search:
  exclude: true
---
# OpenAI Agents SDK

[OpenAI Agents SDK](https://github.com/openai/openai-agents-python) 让你能够以一个轻量、易用且抽象极少的软件包构建智能体式 AI 应用。它是我们此前用于智能体实验项目 [Swarm](https://github.com/openai/swarm/tree/main) 的生产级升级版本。Agents SDK 拥有一组非常小的基本组件：

-   **智能体**，即配备了指令和工具的 LLM
-   **Agents as tools / 任务转移**，允许智能体将特定任务委派给其他智能体
-   **安全防护措施**，可对智能体输入和输出进行验证

结合 Python，这些基本组件足以表达工具与智能体之间的复杂关系，并让你无需陡峭的学习曲线即可构建真实世界应用。此外，SDK 内置了**追踪**功能，可让你可视化并调试智能体流程，还能对其进行评估，甚至为你的应用微调模型。

## 使用 Agents SDK 的原因

SDK 有两个核心设计原则：

1. 功能足够丰富，值得使用；但基本组件足够少，学习速度快。
2. 开箱即用效果出色，同时你也可以精确自定义每一步行为。

以下是 SDK 的主要特性：

-   **智能体循环**：内置智能体循环，可处理工具调用、将结果回传给 LLM，并持续执行直到任务完成。
-   **Python 优先**：使用内置语言特性进行智能体编排与链式调用，而无需学习新的抽象概念。
-   **Agents as tools / 任务转移**：用于在多个智能体之间协调与委派工作的强大机制。
-   **安全防护措施**：与智能体执行并行运行输入验证和安全检查，并在检查未通过时快速失败。
-   **工具调用**：将任意 Python 函数转换为工具，并自动生成 schema 与基于 Pydantic 的验证。
-   **MCP 服务工具调用**：内置 MCP 服务工具集成，使用方式与工具调用相同。
-   **会话**：用于在智能体循环中维护工作上下文的持久化记忆层。
-   **人在回路**：内置机制，可在人机协作中跨智能体运行引入人工参与。
-   **追踪**：内置追踪能力，用于工作流可视化、调试与监控，并支持 OpenAI 全套评估、微调与蒸馏工具。
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

## 从这里开始

-   通过 [Quickstart](quickstart.md) 构建你的第一个基于文本的智能体。
-   然后在 [运行智能体](running_agents.md#choose-a-memory-strategy) 中决定如何在多轮之间保持状态。
-   如果你在任务转移与管理器式编排之间做选择，请阅读 [智能体编排](multi_agent.md)。

## 路径选择

当你知道要完成的工作、但不确定该看哪一页说明时，请使用下表。

| 目标 | 从这里开始 |
| --- | --- |
| 构建第一个文本智能体并查看一次完整运行 | [Quickstart](quickstart.md) |
| 添加工具调用、托管工具或 Agents as tools | [工具](tools.md) |
| 在任务转移与管理器式编排之间做选择 | [智能体编排](multi_agent.md) |
| 在多轮之间保留记忆 | [运行智能体](running_agents.md#choose-a-memory-strategy) 和 [会话](sessions/index.md) |
| 使用 OpenAI 模型、websocket 传输或非 OpenAI 提供方 | [模型](models/index.md) |
| 查看输出、运行项、中断与恢复状态 | [结果](results.md) |
| 构建低延迟语音智能体 | [实时智能体快速开始](realtime/quickstart.md) 和 [实时传输](realtime/transport.md) |
| 构建语音转文本 / 智能体 / 文本转语音流水线 | [语音流水线快速开始](voice/quickstart.md) |