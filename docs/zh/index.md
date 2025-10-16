---
search:
  exclude: true
---
# OpenAI Agents SDK

[OpenAI Agents SDK](https://github.com/openai/openai-agents-python) 使你能够以轻量、易用、抽象极少的方式构建具备智能体能力的 AI 应用。它是我们此前针对智能体的实验项目 [Swarm](https://github.com/openai/swarm/tree/main) 的面向生产的升级版本。Agents SDK 仅包含一小组基本组件：

-   **智能体**：配备了 instructions 和 tools 的 LLM
-   **任务转移**：允许智能体将特定任务委派给其他智能体
-   **安全防护措施**：支持对智能体的输入与输出进行验证
-   **会话**：在多次智能体运行中自动维护对话历史

结合 Python，这些基本组件足以表达工具与智能体之间的复杂关系，使你无需陡峭学习曲线即可构建真实世界应用。此外，SDK 内置了 **追踪** 功能，帮助你可视化和调试智能体流程，并对其进行评估，甚至为你的应用微调模型。

## Why use the Agents SDK

该 SDK 的两个核心设计原则：

1. 功能足够丰富以值得使用，但基础组件足够少以便快速上手。
2. 开箱即用且效果出色，同时可精确自定义行为。

以下是 SDK 的主要特性：

-   智能体循环：内置循环处理工具调用、将结果发送给 LLM，并循环直到 LLM 完成。
-   Python 优先：使用内置语言特性来编排和串联智能体，而无需学习新的抽象。
-   任务转移：在多个智能体之间进行协调与委派的强大能力。
-   安全防护措施：与智能体并行执行输入验证与检查，如检查失败则提前中断。
-   会话：在多次智能体运行中自动管理对话历史，免去手动状态处理。
-   工具调用：将任意 Python 函数变为工具，自动生成模式并借助 Pydantic 进行验证。
-   追踪：内置追踪，可用于可视化、调试与监控工作流，并使用 OpenAI 的评估、微调与蒸馏工具套件。

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

(_如果运行此示例，请确保已设置 `OPENAI_API_KEY` 环境变量_)

```bash
export OPENAI_API_KEY=sk-...
```