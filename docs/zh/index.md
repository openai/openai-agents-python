---
search:
  exclude: true
---
# OpenAI Agents SDK

[OpenAI Agents SDK](https://github.com/openai/openai-agents-python) 让你以轻量、易用、极少抽象的方式构建智能体式 AI 应用。它是我们此前智能体实验项目 [Swarm](https://github.com/openai/swarm/tree/main) 的可用于生产的升级版。Agents SDK 仅包含一小组基本组件：

-   **智能体**：配备 instructions 和 tools 的 LLMs
-   **任务转移**：允许智能体将特定任务委派给其他智能体
-   **安全防护措施**：用于对智能体输入和输出进行验证
-   **会话**：在多次智能体运行之间自动维护对话历史

结合 Python，这些基本组件足以表达工具与智能体之间的复杂关系，让你无需陡峭学习曲线即可构建真实应用。此外，SDK 内置 **追踪**，可用于可视化与调试智能体流程，并支持评估，甚至为你的应用微调模型。

## 为什么使用 Agents SDK

该 SDK 的两条核心设计原则：

1. 功能足够丰富以值得使用，但基本组件足够少以便快速上手。
2. 开箱即用，同时允许你精细定制具体行为。

主要特性包括：

-   智能体循环：内置循环负责调用工具、将结果发送给 LLM，并持续循环直至 LLM 完成。
-   Python 优先：使用内置语言特性来编排与串联智能体，无需学习新的抽象。
-   任务转移：强大的能力，用于在多个智能体之间协调与委派。
-   安全防护措施：与智能体并行执行输入验证与检查，如检查失败则提前中断。
-   会话：在多次智能体运行之间自动管理对话历史，免去手动状态处理。
-   工具调用：将任意 Python 函数变为工具，自动生成模式并通过 Pydantic 驱动验证。
-   追踪：内置追踪用于可视化、调试与监控工作流，并可使用 OpenAI 的评估、微调与蒸馏工具套件。

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

(_如果运行此示例，请确保设置 `OPENAI_API_KEY` 环境变量_)

```bash
export OPENAI_API_KEY=sk-...
```