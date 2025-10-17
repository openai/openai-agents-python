---
search:
  exclude: true
---
# OpenAI Agents SDK

[OpenAI Agents SDK](https://github.com/openai/openai-agents-python) 让你以轻量、易用、抽象极少的方式构建智能体化 AI 应用。它是我们此前智能体实验项目 [Swarm](https://github.com/openai/swarm/tree/main) 的面向生产的升级版本。Agents SDK 仅包含一小组基本组件：

-   **智能体**：配备了 instructions 和 tools 的 LLMs
-   **任务转移**：允许智能体将特定任务委派给其他智能体
-   **安全防护措施**：支持对智能体输入与输出进行校验
-   **会话**：在智能体运行之间自动维护对话历史

结合 Python，这些基本组件足以表达工具与智能体之间的复杂关系，帮助你在没有陡峭学习曲线的情况下构建真实世界应用。此外，SDK 内置了 **追踪**，可用于可视化与调试你的智能体流程，并支持对其进行评估，甚至为你的应用微调模型。

## 为什么使用 Agents SDK

该 SDK 的两个核心设计原则：

1. 功能足够有用，但基本组件足够少，便于快速上手。
2. 开箱即用，同时你可以精确自定义执行过程。

SDK 的主要特性如下：

-   智能体循环：内置循环，负责调用工具、将结果发送给 LLM，并循环直至 LLM 完成。
-   Python 优先：使用内置语言特性来编排与串联智能体，而无需学习新的抽象。
-   任务转移：在多个智能体之间进行协调与委派的强大能力。
-   安全防护措施：与智能体并行运行输入校验与检查，若失败可提前中断。
-   会话：在智能体运行之间自动管理对话历史，免去手动状态处理。
-   工具调用：将任意 Python 函数变为工具，自动生成模式，并通过 Pydantic 提供校验。
-   追踪：内置追踪，便于可视化、调试与监控你的工作流，并可使用 OpenAI 的评估、微调与蒸馏工具套件。

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

（如果运行此示例，请确保已设置 `OPENAI_API_KEY` 环境变量）

```bash
export OPENAI_API_KEY=sk-...
```