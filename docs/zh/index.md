---
search:
  exclude: true
---
# OpenAI Agents SDK

[OpenAI Agents SDK](https://github.com/openai/openai-agents-python) 让你以轻量、易用且极少抽象的方式构建智能体式 AI 应用。它是我们此前面向智能体的试验项目 [Swarm](https://github.com/openai/swarm/tree/main) 的可用于生产的升级版。Agents SDK 仅包含一小组基本组件：

- **智能体（Agents）**：配备了 instructions 和 tools 的 LLM
- **任务转移（Handoffs）**：允许智能体将特定任务委派给其他智能体
- **安全防护措施（Guardrails）**：支持对智能体输入与输出进行校验
- **会话（Sessions）**：在多次运行中自动维护对话历史

结合 Python，这些基本组件足以表达工具与智能体之间的复杂关系，让你无需陡峭的学习曲线即可构建真实应用。此外，SDK 自带 **追踪（tracing）**，可用于可视化和调试你的智能体流程，亦可进行评估，甚至为你的应用微调模型。

## 使用 Agents SDK 的理由

该 SDK 的两条核心设计原则：

1. 功能足够有用，但基本组件足够少，便于快速上手。
2. 开箱即用，同时你可以精确自定义行为。

主要特性包括：

- 智能体循环：内置循环负责调用 tools、将结果反馈给 LLM，并在 LLM 完成前自动迭代。
- Python 优先：使用语言自带能力编排与串联智能体，无需学习新的抽象。
- 任务转移：强大的多智能体协作与委派能力。
- 安全防护措施：与智能体并行执行输入校验与检查，失败即提前终止。
- 会话：跨多次运行自动管理对话历史，免去手动状态处理。
- 工具调用：将任意 Python 函数变为工具，自动生成 schema，并通过 Pydantic 驱动的校验。
- 追踪：内置追踪用于可视化、调试与监控流程，并可使用 OpenAI 的评估、微调与蒸馏工具套件。

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

（运行时，请确保已设置环境变量 `OPENAI_API_KEY`）

```bash
export OPENAI_API_KEY=sk-...
```