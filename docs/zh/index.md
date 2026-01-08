---
search:
  exclude: true
---
# OpenAI Agents SDK

[OpenAI Agents SDK](https://github.com/openai/openai-agents-python) 使你能够以轻量、易用、低抽象的方式构建智能体应用。它是我们此前面向智能体的实验项目 [Swarm](https://github.com/openai/swarm/tree/main) 的面向生产级升级版本。Agents SDK 提供了一小组基础组件：

-   **智能体**：基于 LLM，并配备 instructions 和 tools
-   **任务转移**：允许智能体将特定任务委派给其他智能体
-   **安全防护措施**：用于对智能体的输入与输出进行验证
-   **会话**：在多次运行中自动维护对话历史

结合 Python，这些基础组件足以表达工具与智能体之间的复杂关系，让你无需陡峭学习曲线即可构建真实世界应用。此外，SDK 内置 **追踪**，可视化并调试智能体流程，还能对其进行评估，甚至为你的应用微调模型。

## 使用 Agents SDK 的理由

该 SDK 的设计遵循两条原则：

1. 功能足够有用，但基础组件足够少，上手快速。
2. 开箱即用效果佳，同时可精细定制行为。

主要特性包括：

-   智能体循环：内置循环，负责调用工具、将结果返回给 LLM，并在 LLM 完成前持续迭代。
-   Python 优先：使用语言原生特性来编排与串联智能体，而无需学习新的抽象。
-   任务转移：在多个智能体之间进行协调与委派的强大能力。
-   安全防护措施：与智能体并行执行输入校验与检查，失败时提前中断。
-   会话：跨多次运行自动管理对话历史，免去手动管理状态。
-   工具调用：将任意 Python 函数变为工具，自动生成模式，并通过 Pydantic 驱动的校验。
-   追踪：内置追踪，可视化、调试与监控工作流，并可使用 OpenAI 的评估、微调与蒸馏工具套件。

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

(_如果要运行此示例，请确保已设置 `OPENAI_API_KEY` 环境变量_)

```bash
export OPENAI_API_KEY=sk-...
```