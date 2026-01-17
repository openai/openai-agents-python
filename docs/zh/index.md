---
search:
  exclude: true
---
# OpenAI Agents SDK

[OpenAI Agents SDK](https://github.com/openai/openai-agents-python) 让你以轻量、易用、低抽象的方式构建具备自主能力的 AI 应用。它是我们此前面向智能体的实验项目 [Swarm](https://github.com/openai/swarm/tree/main) 的生产级升级版。Agents SDK 仅包含一小组基本组件：

- **智能体（Agents）**：配备指令（instructions）与工具（tools）的 LLM
- **任务转移（Handoffs）**：允许智能体将特定任务委派给其他智能体
- **安全防护措施（Guardrails）**：支持对智能体输入与输出进行校验
- **会话（Sessions）**：自动在多次运行间维护对话历史

结合 Python，这些基本组件足以表达工具与智能体之间的复杂关系，让你无需陡峭的学习曲线即可构建真实应用。此外，SDK 内置 **追踪（tracing）**，便于可视化与调试你的智能体流程，并可对其进行评估，甚至为你的应用微调模型。

## 为什么使用 Agents SDK

该 SDK 的设计原则有两点：

1. 功能足够丰富值得使用，但基础组件足够少，便于快速上手。
2. 开箱即用表现优秀，同时可精准自定义行为。

SDK 的主要特性包括：

- 智能体循环（Agent loop）：内置循环处理调用工具、将结果回传给 LLM，并迭代直至 LLM 完成。
- Python 优先（Python-first）：使用语言内置特性编排与串联智能体，无需学习新的抽象。
- 任务转移（Handoffs）：强大的特性，用于在多个智能体间协调与委派。
- 安全防护措施（Guardrails）：与智能体并行运行输入校验与检查，失败时可提前中断。
- 会话（Sessions）：跨多次运行自动管理对话历史，免去手动状态处理。
- 工具调用（Function tools）：将任意 Python 函数变为工具，自动生成模式并通过 Pydantic 提供校验。
- 追踪（Tracing）：内置追踪，便于可视化、调试与监控工作流，并可使用 OpenAI 的评估、微调与蒸馏工具套件。

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

(_如果运行此示例，请确保已设置 `OPENAI_API_KEY` 环境变量_)

```bash
export OPENAI_API_KEY=sk-...
```