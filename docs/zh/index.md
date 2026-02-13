---
search:
  exclude: true
---
# OpenAI Agents SDK

[OpenAI Agents SDK](https://github.com/openai/openai-agents-python) 使你能够以轻量、易用、抽象极少的方式构建智能体 AI 应用。这是我们此前用于智能体实验的项目 [Swarm](https://github.com/openai/swarm/tree/main) 的生产级升级版本。Agents SDK 只有一小组 basic components：

-   **智能体**：配备 instructions 和 tools 的 LLM
-   **Agents as tools / 任务转移**：允许智能体将特定任务委派给其他智能体
-   **安全防护措施**：用于对智能体输入与输出进行验证

与 Python 结合时，这些 basic components 足以表达工具与智能体之间的复杂关系，并让你无需陡峭的学习曲线就能构建真实世界的应用。此外，SDK 内置 **追踪**，可用于可视化与调试你的智能体流程，并对其进行评估，甚至为你的应用微调模型。

## 使用 Agents SDK 的原因

SDK 有两个核心设计原则：

1. 功能足够多，值得使用；但 basic components 足够少，便于快速上手。
2. 开箱即用，同时也允许你精确自定义行为。

以下是 SDK 的主要特性：

-   **智能体循环**：内置智能体循环，负责处理工具调用、将结果回传给 LLM，并持续运行直到任务完成。
-   **Python 优先**：使用内置语言特性来编排与串联智能体，而无需学习新的抽象。
-   **Agents as tools / 任务转移**：用于跨多个智能体协调与委派工作的强大机制。
-   **安全防护措施**：在智能体执行的同时并行运行输入验证与安全检查，检查不通过时快速失败。
-   **工具调用**：将任意 Python 函数转换为工具，自动生成 schema，并提供由 Pydantic 驱动的验证。
-   **MCP server 工具调用**：内置 MCP server 工具集成，使用方式与工具调用相同。
-   **会话**：用于在智能体循环中维护工作上下文的持久化记忆层。
-   **人在回路（Human in the loop）**：在智能体运行过程中引入人类参与的内置机制。
-   **追踪**：用于可视化、调试与监控工作流的内置追踪，并支持 OpenAI 的评估、微调与蒸馏工具套件。
-   **Realtime 智能体**：构建强大的语音智能体，具备自动打断检测、上下文管理、安全防护措施等能力。

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

(_如果要运行此示例，请确保已设置 `OPENAI_API_KEY` 环境变量_)

```bash
export OPENAI_API_KEY=sk-...
```