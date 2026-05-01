---
search:
  exclude: true
---
# 快速入门

## 项目和虚拟环境的创建

你只需执行一次。

```bash
mkdir my_project
cd my_project
python -m venv .venv
```

### 虚拟环境的激活

每次启动新的终端会话时都要执行此操作。

在 macOS 或 Linux 上：

```bash
source .venv/bin/activate
```

在 Windows 上：

```cmd
.venv\Scripts\activate
```

### Agents SDK 的安装

```bash
pip install openai-agents # or `uv add openai-agents`, etc
```

### OpenAI API 密钥的设置

如果你还没有密钥，请按照[这些说明](https://platform.openai.com/docs/quickstart#create-and-export-an-api-key)创建 OpenAI API 密钥。

这些命令会为当前终端会话设置密钥。

在 macOS 或 Linux 上：

```bash
export OPENAI_API_KEY=sk-...
```

在 Windows PowerShell 上：

```powershell
$env:OPENAI_API_KEY = "sk-..."
```

在 Windows 命令提示符上：

```cmd
set "OPENAI_API_KEY=sk-..."
```

## 第一个智能体的创建

智能体通过 instructions、名称以及可选配置（例如特定模型）来定义。

```python
from agents import Agent

agent = Agent(
    name="History Tutor",
    instructions="You answer history questions clearly and concisely.",
)
```

## 第一个智能体的运行

使用 [`Runner`][agents.run.Runner] 执行智能体，并获取返回的 [`RunResult`][agents.result.RunResult]。

```python
import asyncio
from agents import Agent, Runner

agent = Agent(
    name="History Tutor",
    instructions="You answer history questions clearly and concisely.",
)

async def main():
    result = await Runner.run(agent, "When did the Roman Empire fall?")
    print(result.final_output)

if __name__ == "__main__":
    asyncio.run(main())
```

对于第二轮，你可以将 `result.to_input_list()` 传回 `Runner.run(...)`，附加一个 [session](sessions/index.md)，或使用 `conversation_id` / `previous_response_id` 复用 OpenAI 服务管理的状态。[运行智能体](running_agents.md)指南对这些方法进行了比较。

可使用以下经验法则：

| 如果你想要... | 从以下方式开始... |
| --- | --- |
| 完全手动控制且与提供商无关的历史记录 | `result.to_input_list()` |
| 由 SDK 为你加载和保存历史记录 | [`session=...`](sessions/index.md) |
| OpenAI 管理的服务端延续 | `previous_response_id` 或 `conversation_id` |

有关取舍和确切行为，请参阅[运行智能体](running_agents.md#choose-a-memory-strategy)。

当任务主要依赖提示词、工具和对话状态时，请使用普通的 `Agent` 加 `Runner`。如果智能体应在隔离工作区中检查或修改真实文件，请转到 [Sandbox agents 快速入门](sandbox_agents.md)。

## 为智能体提供工具

你可以为智能体提供工具来查找信息或执行操作。

```python
import asyncio
from agents import Agent, Runner, function_tool


@function_tool
def history_fun_fact() -> str:
    """Return a short history fact."""
    return "Sharks are older than trees."


agent = Agent(
    name="History Tutor",
    instructions="Answer history questions clearly. Use history_fun_fact when it helps.",
    tools=[history_fun_fact],
)


async def main():
    result = await Runner.run(
        agent,
        "Tell me something surprising about ancient life on Earth.",
    )
    print(result.final_output)


if __name__ == "__main__":
    asyncio.run(main())
```

## 添加更多智能体

在选择多智能体模式之前，先决定谁应拥有最终答案：

-   **任务转移**：专家接管本轮中该部分的对话。
-   **Agents as tools**：编排器保持控制，并将专家作为工具调用。

本快速入门继续使用**任务转移**，因为它是最简短的第一个示例。有关管理器风格的模式，请参阅[智能体编排](multi_agent.md)和[工具：agents as tools](tools.md#agents-as-tools)。

可以用同样的方式定义其他智能体。`handoff_description` 会为路由智能体提供有关何时委派的额外上下文。

```python
from agents import Agent

history_tutor_agent = Agent(
    name="History Tutor",
    handoff_description="Specialist agent for historical questions",
    instructions="You answer history questions clearly and concisely.",
)

math_tutor_agent = Agent(
    name="Math Tutor",
    handoff_description="Specialist agent for math questions",
    instructions="You explain math step by step and include worked examples.",
)
```

## 任务转移的定义

在智能体上，你可以定义一组可选的传出任务转移选项，供其在解决任务时选择。

```python
triage_agent = Agent(
    name="Triage Agent",
    instructions="Route each homework question to the right specialist.",
    handoffs=[history_tutor_agent, math_tutor_agent],
)
```

## 智能体编排的运行

运行器会处理各个智能体的执行、任何任务转移以及任何工具调用。

```python
import asyncio
from agents import Runner


async def main():
    result = await Runner.run(
        triage_agent,
        "Who was the first president of the United States?",
    )
    print(result.final_output)
    print(f"Answered by: {result.last_agent.name}")


if __name__ == "__main__":
    asyncio.run(main())
```

## 参考代码示例

该仓库包含相同核心模式的完整脚本：

-   [`examples/basic/hello_world.py`](https://github.com/openai/openai-agents-python/tree/main/examples/basic/hello_world.py) 用于首次运行。
-   [`examples/basic/tools.py`](https://github.com/openai/openai-agents-python/tree/main/examples/basic/tools.py) 用于工具调用。
-   [`examples/agent_patterns/routing.py`](https://github.com/openai/openai-agents-python/tree/main/examples/agent_patterns/routing.py) 用于多智能体路由。

## 查看追踪

要回顾智能体运行期间发生的情况，请前往 [OpenAI Dashboard 中的 Trace viewer](https://platform.openai.com/traces)查看智能体运行的追踪。

## 后续步骤

学习如何构建更复杂的智能体式流程：

-   了解如何配置[智能体](agents.md)。
-   了解[运行智能体](running_agents.md)和 [sessions](sessions/index.md)。
-   如果工作应在真实工作区内进行，请了解 [Sandbox agents](sandbox_agents.md)。
-   了解[工具](tools.md)、[安全防护措施](guardrails.md)和[模型](models/index.md)。