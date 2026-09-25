---
search:
  exclude: true
---
# OpenAI Agents SDK

[OpenAI Agents SDK](https://github.com/openai/openai-agents-python)让您能够使用一个轻量、易用且仅包含极少抽象概念的软件包，构建智能体式 AI 应用。它是我们之前智能体实验项目[Swarm](https://github.com/openai/swarm/tree/main)的生产就绪升级版。Agents SDK 仅包含一小组基础组件：

-   **智能体**，即配备了指令和工具的 LLM
-   **Agents as tools / 任务转移**，允许智能体将特定任务委派给其他智能体
-   **安全防护措施**，用于验证智能体的输入和输出

这些基础组件与 Python 结合使用时，足以表达工具与智能体之间的复杂关系，让您无需经历陡峭的学习曲线即可构建实际应用。此外，SDK 还内置了**追踪**功能，让您能够可视化和调试智能体流程、对其进行评估，甚至针对您的应用微调模型。

## Agents SDK 的使用理由 {#why-use-the-agents-sdk}

SDK 遵循两项核心设计原则：

1. 提供足以带来使用价值的功能，同时将基础组件控制在较少数量，以便快速学习。
2. 开箱即用，同时允许您精确自定义具体行为。

以下是 SDK 的主要功能：

-   **智能体**：使用指令、工具、安全防护措施、任务转移以及持续运行直至任务完成的内置循环来构建智能体。
-   **沙箱智能体**：在真正隔离的工作区中运行专项智能体。沙箱智能体支持由清单定义的文件、沙箱客户端选择，以及可恢复的沙箱会话。
-   **实时智能体**：使用`gpt-realtime-2.1`、自动中断检测、上下文管理、安全防护措施等功能构建强大的语音智能体。
-   **语音智能体**：构建结合语音转文本、智能体工作流和文本转语音的语音管线。
-   **Python 优先**：使用内置语言特性编排和串联智能体，无需学习新的抽象概念。
-   **Agents as tools / 任务转移**：一种在多个智能体之间协调和委派工作的强大机制。
-   **安全防护措施**：在执行智能体的同时并行运行输入验证和安全检查，并在检查未通过时快速失败。
-   **函数工具**：通过自动生成模式和由 Pydantic 提供支持的验证，将任意 Python 函数转换为工具。
-   **MCP 服务器工具调用**：内置集成，可同时向智能体提供远程 MCP 工具和函数工具。
-   **会话**：用于在智能体循环中维护工作上下文的持久化记忆层。
-   **人在回路中**：用于在智能体运行期间引入人工参与的内置机制。
-   **追踪**：用于可视化、调试和监控工作流的内置追踪功能，并支持 OpenAI 的评估、微调和蒸馏工具套件。

## Agents SDK 与 Responses API 的选择 {#agents-sdk-or-responses-api}

对于 OpenAI 模型，SDK 默认使用 Responses API，但它会将模型调用封装在更高层级的运行时中。

以下情况适合直接使用 Responses API：

-   您希望自行掌控循环、工具分派和状态处理
-   您的工作流生命周期较短，主要目标是返回模型响应

以下情况适合使用 Agents SDK：

-   您希望由运行时管理轮次、工具执行、安全防护措施、任务转移或会话
-   您的智能体需要生成产物，或通过多个协调步骤完成操作
-   您需要通过[沙箱智能体](sandbox_agents.md)获得真实工作区或可恢复执行能力

您无需在整个应用中只选择一种方式。许多应用会使用 SDK 管理工作流，同时针对较低层级的执行路径直接调用 Responses API。

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

（_运行此代码时，请确保已设置`OPENAI_API_KEY`环境变量_）

```bash
export OPENAI_API_KEY=sk-...
```

## 入门 {#start-here}

-   通过[快速入门](quickstart.md)构建您的第一个文本智能体。
-   然后在[运行智能体](running_agents.md#choose-a-memory-strategy)中决定如何跨轮次传递状态。
-   如果任务依赖真实文件、仓库或每个智能体独立的隔离工作区状态，请阅读[沙箱智能体快速入门](sandbox_agents.md)。
-   如果您正在任务转移与管理器式编排之间进行选择，请阅读[智能体编排](multi_agent.md)。

## 路径选择 {#choose-your-path}

当您明确想完成的工作，但不确定应该参阅哪个页面时，请使用此表。

| 目标 | 入门页面 |
| --- | --- |
| 构建第一个文本智能体并查看一次完整运行 | [快速入门](quickstart.md) |
| 添加函数工具、托管工具或 agents as tools | [工具](tools.md) |
| 在真正隔离的工作区中运行编码、审查或文档智能体 | [沙箱智能体快速入门](sandbox_agents.md)和[沙箱客户端](sandbox/clients.md) |
| 在任务转移与管理器式编排之间进行选择 | [智能体编排](multi_agent.md) |
| 跨轮次保留记忆 | [运行智能体](running_agents.md#choose-a-memory-strategy)和[会话](sessions/index.md) |
| 使用 OpenAI 模型、WebSocket 传输或非 OpenAI 提供商 | [模型](models/index.md) |
| 检查输出、运行项、中断和恢复状态 | [结果](results.md) |
| 使用`gpt-realtime-2.1`构建低延迟语音智能体 | [实时智能体快速入门](realtime/quickstart.md)和[实时传输](realtime/transport.md) |
| 构建语音转文本 / 智能体 / 文本转语音管线 | [语音管线快速入门](voice/quickstart.md) |