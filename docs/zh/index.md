---
search:
  exclude: true
---
# OpenAI Agents SDK

[OpenAI Agents SDK](https://github.com/openai/openai-agents-python) 让你以轻量、易用、几乎无抽象的方式构建智能体式 AI 应用。它是我们先前智能体试验项目 [Swarm](https://github.com/openai/swarm/tree/main) 的可用于生产的升级版。Agents SDK 仅包含极少量的基本组件：

-   **智能体（Agents）**：配备了 instructions 和 tools 的 LLM
-   **任务转移（Handoffs）**：允许智能体将特定任务委派给其他智能体
-   **安全防护措施（Guardrails）**：支持对智能体的输入与输出进行校验
-   **会话（Sessions）**：在智能体多次运行间自动维护对话历史

结合 Python，这些基本组件足以表达工具与智能体之间的复杂关系，使你无需陡峭的学习曲线即可构建真实世界的应用。此外，SDK 内置了 **追踪（tracing）**，可帮助你可视化和调试智能体流程，并支持评估、甚至为你的应用微调模型。

## 为什么使用 Agents SDK

该 SDK 的两条核心设计原则：

1. 功能足够丰富值得使用，同时基本组件足够少，便于快速上手。
2. 开箱即用体验优秀，同时你可以精确自定义行为。

SDK 的主要特性包括：

-   智能体循环：内置循环，负责调用工具、将结果发送给 LLM，并循环直至 LLM 完成。
-   Python 优先：使用内置语言特性编排与串联智能体，而无需学习新的抽象。
-   任务转移：强大的能力，用于在多个智能体间协调与委派。
-   安全防护措施：与智能体并行执行输入校验与检查，若检查失败可提前中断。
-   会话：跨智能体运行自动管理对话历史，免去手动管理状态。
-   工具调用（Function tools）：将任意 Python 函数转换为工具，自动生成模式并通过 Pydantic 提供校验。
-   追踪：内置追踪，便于可视化、调试与监控工作流，并可使用 OpenAI 的评估、微调与蒸馏工具套件。

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

_（如果运行此示例，请确保设置 `OPENAI_API_KEY` 环境变量）_

```bash
export OPENAI_API_KEY=sk-...
```