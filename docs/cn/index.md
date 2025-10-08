---
search:
  exclude: true
---
# OpenAI Agents SDK

[OpenAI Agents SDK](https://github.com/openai/openai-agents-python) 让您能够在轻量级、易于使用的软件包中构建智能体 AI 应用，抽象化程度极低。它是我们之前用于智能体的实验项目 [Swarm](https://github.com/openai/swarm/tree/main) 的生产级升级版本。Agents SDK 只包含极少量原语：

-   **Agents**，即配备指令和工具的 LLM
-   **Handoffs**，允许智能体将特定任务委托给其他智能体
-   **Guardrails**，支持对智能体输入和输出进行验证
-   **Sessions**，在智能体运行之间自动维护对话历史

结合 Python，这些原语足以表达工具和智能体之间的复杂关系，让您无需陡峭的学习曲线即可构建实际应用。此外，SDK 还内置了**追踪**功能，让您能够可视化和调试智能体流程，以及评估它们，甚至为您的应用微调模型。

## 为何使用 Agents SDK

SDK 有两个核心设计原则：

1. 功能足够丰富以值得使用，但原语足够少以便快速学习。
2. 开箱即用表现出色，但您可以精确自定义发生的情况。

以下是 SDK 的主要功能：

-   Agent loop：内置的智能体循环，处理调用工具、将结果发送给 LLM，以及循环直到 LLM 完成。
-   Python-first：使用内置语言功能来编排和链接智能体，而非需要学习新的抽象概念。
-   Handoffs：在多个智能体之间协调和委托的强大功能。
-   Guardrails：与智能体并行运行输入验证和检查，如果检查失败则提前中断。
-   Sessions：跨智能体运行自动管理对话历史，消除手动状态处理。
-   Function tools：将任何 Python 函数转换为工具，具有自动模式生成和 Pydantic 驱动的验证。
-   Tracing：内置追踪功能让您能够可视化、调试和监控工作流程，以及使用 OpenAI 的评估、微调和蒸馏工具套件。

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

（_如果运行此代码，请确保设置 `OPENAI_API_KEY` 环境变量_）

```bash
export OPENAI_API_KEY=sk-...
```