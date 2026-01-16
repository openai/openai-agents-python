---
search:
  exclude: true
---
# OpenAI Agents SDK

[OpenAI Agents SDK](https://github.com/openai/openai-agents-python) 使你能够以轻量、易用、抽象极少的方式构建智能体式 AI 应用。它是我们此前针对智能体的试验项目 [Swarm](https://github.com/openai/swarm/tree/main) 的可用于生产的升级版。Agents SDK 仅包含一小组基本组件：

-   **智能体（Agents）**，即配备 instructions 和 tools 的 LLM
-   **任务转移（Handoffs）**，允许智能体将特定任务委派给其他智能体
-   **安全防护措施（Guardrails）**，用于验证智能体的输入与输出
-   **会话（Sessions）**，在多次智能体运行间自动维护对话历史

结合 Python，这些基本组件足以表达 tools 与智能体之间的复杂关系，让你无需陡峭学习曲线即可构建真实世界应用。此外，SDK 自带内置的 **追踪（tracing）**，可帮助你可视化与调试智能体流程，对其进行评估，甚至为你的应用微调模型。

## 为何使用 Agents SDK

该 SDK 遵循两条核心设计原则：

1. 功能足够多以值得使用，但基本组件足够少以便快速上手。
2. 开箱即用且效果良好，同时支持你精确定制执行过程。

SDK 的主要特性包括：

-   智能体循环：内置循环，负责调用 tools、将结果发送给 LLM，并循环直至 LLM 完成。
-   Python 优先：使用内置语言特性来编排并串联智能体，而无需学习新的抽象概念。
-   任务转移：在多个智能体间进行协调与委派的强大能力。
-   安全防护措施：与智能体并行运行输入验证与检查，如失败则尽早中止。
-   会话：跨多次智能体运行的对话历史自动管理，省去手动状态处理。
-   工具调用：将任意 Python 函数变为 tool，自动生成 schema，并由 Pydantic 提供验证。
-   追踪：内置追踪，支持可视化、调试与监控工作流，并可使用 OpenAI 的评估、微调与蒸馏工具套件。

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