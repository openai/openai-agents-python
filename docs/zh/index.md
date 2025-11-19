---
search:
  exclude: true
---
# OpenAI Agents SDK

[OpenAI Agents SDK](https://github.com/openai/openai-agents-python) 让你以轻量、易用、极少抽象的方式构建智能体式 AI 应用。它是我们此前面向智能体的实验项目 [Swarm](https://github.com/openai/swarm/tree/main) 的面向生产的升级版。Agents SDK 仅包含一小组基本组件：

-   **智能体**：配备了 instructions 和工具的 LLM
-   **任务转移**：允许智能体将特定任务委派给其他智能体
-   **安全防护措施**：支持对智能体输入与输出进行校验
-   **会话**：在多次智能体运行间自动维护对话历史

配合 Python，这些基本组件足以表达工具与智能体之间的复杂关系，让你在没有陡峭学习曲线的情况下构建真实世界应用。此外，SDK 内置 **追踪**，可用于可视化与调试你的智能体流程，并进一步对其进行评估，甚至为你的应用微调模型。

## 为什么使用 Agents SDK

该 SDK 的两项核心设计原则：

1. 功能足够多，值得使用；抽象足够少，易于上手。
2. 开箱即用效果出色，同时你可以精确自定义其行为。

以下是 SDK 的主要特性：

-   智能体循环：内置循环，负责调用工具、将结果返回给 LLM，并循环直至 LLM 完成。
-   Python 优先：使用语言内置特性编排并串联智能体，而无需学习新的抽象。
-   任务转移：在多个智能体之间进行协调与委派的强大功能。
-   安全防护措施：与智能体并行运行输入校验与检查，若检查失败则尽早中断。
-   会话：跨多次智能体运行自动管理对话历史，免去手动状态管理。
-   工具调用：将任意 Python 函数变成工具，自动生成模式并提供基于 Pydantic 的校验。
-   追踪：内置追踪，用于可视化、调试与监控工作流，并可使用 OpenAI 的评估、微调与蒸馏工具套件。

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