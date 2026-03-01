---
search:
  exclude: true
---
# OpenAI Agents SDK

[OpenAI Agents SDK](https://github.com/openai/openai-agents-python) 让你能够以轻量、易用且抽象极少的方式构建智能体 AI 应用。它是我们此前用于智能体实验的 [Swarm](https://github.com/openai/swarm/tree/main) 的生产就绪升级版。Agents SDK 只有一组非常精简的基本组件：

-   **智能体**，即配备了指令和工具的 LLM
-   **Agents as tools / 任务转移**，允许智能体将特定任务委派给其他智能体
-   **安全防护措施**，用于对智能体输入与输出进行校验

结合 Python，这些基本组件足以表达工具与智能体之间的复杂关系，并让你无需陡峭的学习曲线即可构建真实世界应用。此外，SDK 内置了**追踪**，可让你可视化并调试智能体流程，还能对其进行评估，甚至为你的应用微调模型。

## 使用 Agents SDK 的原因

SDK 有两个核心设计原则：

1. 功能足够实用，同时基本组件足够少，学习上手更快。
2. 开箱即用体验优秀，同时你也可以精确自定义每一步行为。

以下是 SDK 的主要特性：

-   **智能体循环**：内置智能体循环，负责处理工具调用、将结果回传给 LLM，并持续执行直到任务完成。
-   **Python 优先**：使用语言内置能力来进行智能体编排与链式调用，而无需学习新的抽象。
-   **Agents as tools / 任务转移**：一种强大的机制，用于在多个智能体之间协调并委派工作。
-   **安全防护措施**：在智能体执行的同时并行运行输入校验和安全检查，并在检查未通过时快速失败。
-   **工具调用**：将任意 Python 函数转换为工具，自动生成 schema，并借助 Pydantic 进行校验。
-   **MCP 服务工具调用**：内置 MCP 服务工具集成，使用方式与工具调用一致。
-   **会话**：持久化记忆层，用于在智能体循环中维护工作上下文。
-   **人类参与回路**：内置机制，支持在智能体运行过程中引入人工参与。
-   **追踪**：内置追踪用于工作流可视化、调试与监控，并支持 OpenAI 的评估、微调与蒸馏工具套件。
-   **Realtime Agents**：构建强大的语音智能体，支持自动打断检测、上下文管理、安全防护措施等功能。

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

-   使用[快速开始](quickstart.md)构建你的第一个文本智能体。
-   使用[Realtime 智能体快速开始](realtime/quickstart.md)构建低延迟语音智能体。
-   如果你想使用语音转文本 / 智能体 / 文本转语音流水线，请参阅[语音流水线快速开始](voice/quickstart.md)。