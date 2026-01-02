---
search:
  exclude: true
---
# OpenAI Agents SDK

[OpenAI Agents SDK](https://github.com/openai/openai-agents-python) 让你以轻量、易用、抽象极少的方式构建智能体 AI 应用。它是我们此前用于智能体实验项目 [Swarm](https://github.com/openai/swarm/tree/main) 的面向生产的升级版。Agents SDK 仅包含一小组基本组件：

-   **智能体（Agents）**：配备 instructions 和 tools 的 LLM
-   **任务转移（Handoffs）**：允许智能体将特定任务委派给其他智能体
-   **安全防护措施（Guardrails）**：支持对智能体输入与输出进行验证
-   **会话（Sessions）**：在多次智能体运行间自动维护对话历史

结合 Python，这些基本组件足以表达工具与智能体之间的复杂关系，让你无需陡峭学习曲线即可构建真实世界应用。此外，SDK 内置 **追踪（tracing）**，可视化与调试智能体流程，对其进行评测，甚至为你的应用微调模型。

## 为什么使用 Agents SDK

该 SDK 遵循两大设计原则：

1. 功能足够有用，但基本组件足够少，便于快速上手。
2. 开箱即用且好用，同时支持你精确自定义行为。

SDK 的主要特性包括：

-   智能体循环：内置循环处理调用工具、将结果回传给 LLM，并循环直至 LLM 完成。
-   Python 优先：利用语言内建特性编排与串联智能体，而无需学习新抽象。
-   任务转移：在多个智能体间进行协调与委派的强大能力。
-   安全防护措施：与智能体并行执行输入校验，校验失败时可提前中断。
-   会话：跨智能体运行自动管理对话历史，免去手动状态管理。
-   工具调用（Function tools）：将任意 Python 函数变为工具，自动生成模式（schema）并通过 Pydantic 驱动进行验证。
-   追踪：内置追踪，可视化、调试与监控工作流，并可使用 OpenAI 的评测、微调与蒸馏工具套件。

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

(_If running this, ensure you set the `OPENAI_API_KEY` environment variable_)

```bash
export OPENAI_API_KEY=sk-...
```