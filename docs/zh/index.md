---
search:
  exclude: true
---
# OpenAI Agents SDK

!!! note "重要通知"

    Agents SDK 已**功能完备**。维护、安全修复、关键错误修复和兼容性工作仍将继续，但目前没有开发重大新功能的计划。对于新的智能体应用，我们推荐使用**[Agents API](https://developers.openai.com/api/docs/guides/agents-api/quickstart)**，它运行由托管服务提供的 Codex 执行框架。

    现有应用可以继续使用 Agents SDK。对于需要 Agents API 尚未支持的功能的新应用，SDK 仍可作为短期选择。

[OpenAI Agents SDK](https://github.com/openai/openai-agents-python) 是一个开源框架，用于在应用代码中构建智能体工作流。它基于 [Swarm](https://github.com/openai/swarm/tree/main) 构建，通过安全防护措施和内置追踪，将轻量级多智能体编排引入生产应用。

SDK 会在您的应用中运行智能体循环，协调模型调用和工具执行，包括 MCP 服务器工具。会话可在多次运行之间保留对话上下文，而人工审批机制可让您的应用暂停工具执行以供审核。核心基础组件包括：

-   **智能体**：配备 instructions 和 tools 的 LLM
-   **Agents as tools / 任务转移**：允许智能体针对特定任务将工作委派给其他智能体
-   **安全防护措施**：支持验证智能体的输入和输出

您可以结合使用这些基础组件与 Python 来协调多步骤工作流。内置**追踪**可帮助您可视化、调试和评估这些工作流。SDK 还支持用于低延迟语音交互的[实时智能体](realtime/guide.md)，以及用于处理文件和命令的[沙箱智能体](sandbox_agents.md)。

## 使用 Agents SDK 的理由 {#why-use-the-agents-sdk}

SDK 遵循两项核心设计原则：

1. 提供足够丰富且值得使用的功能，同时将基础组件控制在足够少的数量，以便快速掌握。
2. 开箱即用，同时允许您精确自定义具体行为。

SDK 的主要功能包括：

-   **智能体**：使用 instructions、工具、安全防护措施、任务转移和内置循环来构建智能体；该循环会持续运行，直至任务完成。
-   **沙箱智能体**：在真正隔离的工作区内运行专家智能体。沙箱智能体支持由清单定义的文件、沙箱客户端选择，以及可恢复的沙箱会话。
-   **实时智能体**：使用 `gpt-realtime-2.1`、自动中断检测、上下文管理、安全防护措施等功能构建强大的语音智能体。
-   **语音智能体**：构建结合语音转文本、智能体工作流和文本转语音的语音管线。
-   **Python 优先**：使用内置语言功能编排和串联智能体，而无需学习新的抽象概念。
-   **Agents as tools / 任务转移**：用于在多个智能体之间协调和委派工作的强大机制。
-   **安全防护措施**：与智能体执行并行开展输入验证和安全检查，并在检查未通过时立即终止。
-   **函数工具**：通过自动生成模式和基于 Pydantic 的验证，将任意 Python 函数转换为工具。
-   **MCP 服务器工具调用**：内置集成，可将远程 MCP 工具与函数工具一起提供给智能体。
-   **会话**：用于在智能体循环中维护工作上下文的持久化记忆层。
-   **人在回路**：在智能体运行期间引入人工参与的内置机制。
-   **追踪**：用于可视化、调试和监控工作流的内置追踪功能，并支持 OpenAI 的评估、微调和蒸馏工具套件。

## Agents SDK 与 Responses API 的选择 {#agents-sdk-or-responses-api}

对于新的智能体应用，请从 [Agents API](https://developers.openai.com/api/docs/guides/agents-api/quickstart) 开始。以下比较适用于需要在自己的应用代码中运行智能体工作流的情况。

对于 OpenAI 模型，SDK 默认使用 Responses API，但会通过更高级别的运行时封装模型调用。

以下情况适合直接使用 Responses API：

-   您希望自行掌控循环、工具分发和状态处理
-   您的工作流生命周期较短，并且主要用于返回模型响应

以下情况适合使用 Agents SDK：

-   您希望由运行时管理轮次、工具执行、安全防护措施、任务转移或会话
-   您的智能体需要生成产物，或通过多个协调步骤执行操作
-   您需要真实工作区，或需要通过[沙箱智能体](sandbox_agents.md)实现可恢复执行

您不必在整个应用中只选择其中一种。许多应用会使用 SDK 管理工作流，同时针对更底层的路径直接调用 Responses API。

## 安装 {#installation}

```bash
pip install openai-agents
```

## Hello world 示例 {#hello-world-example}

```python
from agents import Agent, Runner

agent = Agent(name="Assistant", instructions="You are a helpful assistant")

result = Runner.run_sync(agent, "Write a haiku about recursion in programming.")
print(result.final_output)

# Code within the code,
# Functions calling themselves,
# Infinite loop's dance.
```

（_运行此示例时，请确保已设置 `OPENAI_API_KEY` 环境变量_）

```bash
export OPENAI_API_KEY=sk-...
```

## 入门指南 {#start-here}

-   通过[快速入门](quickstart.md)构建您的第一个文本智能体。
-   然后在[运行智能体](running_agents.md#choose-a-memory-strategy)中决定如何跨轮次维护状态。
-   如果任务依赖真实文件、代码仓库或按智能体隔离的工作区状态，请阅读[沙箱智能体快速入门](sandbox_agents.md)。
-   如果您需要在任务转移与管理器式编排之间做出选择，请阅读[智能体编排](multi_agent.md)。

## 路径选择 {#choose-your-path}

如果您清楚自己想完成的工作，但不知道应查看哪个页面，请参考下表。

| 目标 | 入门页面 |
| --- | --- |
| 构建第一个文本智能体并查看一次完整运行 | [快速入门](quickstart.md) |
| 添加函数工具、托管工具或 Agents as tools | [工具](tools.md) |
| 在真正隔离的工作区中运行编码、审核或文档智能体 | [沙箱智能体快速入门](sandbox_agents.md)和[沙箱客户端](sandbox/clients.md) |
| 在任务转移与管理器式编排之间做出选择 | [智能体编排](multi_agent.md) |
| 跨轮次保留记忆 | [运行智能体](running_agents.md#choose-a-memory-strategy)和[会话](sessions/index.md) |
| 使用 OpenAI 模型、WebSocket 传输或非 OpenAI 提供商 | [模型](models/index.md) |
| 审核输出、运行项、中断和恢复状态 | [结果](results.md) |
| 使用 `gpt-realtime-2.1` 构建低延迟语音智能体 | [实时智能体快速入门](realtime/quickstart.md)和[实时传输](realtime/transport.md) |
| 构建语音转文本 / 智能体 / 文本转语音管线 | [语音管线快速入门](voice/quickstart.md) |