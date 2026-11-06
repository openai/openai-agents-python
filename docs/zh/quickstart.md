---
search:
  exclude: true
---
# 快速入门

## 项目和虚拟环境的创建 {#create-a-project-and-virtual-environment}

此操作只需执行一次。

```bash
mkdir my_project
cd my_project
python -m venv .venv
```

### 虚拟环境的激活 {#activate-the-virtual-environment}

每次启动新的终端会话时都需要执行此操作。

在 macOS 或 Linux 上：

```bash
source .venv/bin/activate
```

在 Windows 上：

```cmd
.venv\Scripts\activate
```

### Agents SDK 的安装 {#install-the-agents-sdk}

```bash
pip install openai-agents # or `uv add openai-agents`, etc
```

### OpenAI API 密钥的设置 {#set-an-openai-api-key}

如果您还没有密钥，请按照[这些说明](https://platform.openai.com/docs/quickstart#create-and-export-an-api-key)创建 OpenAI API 密钥。

以下命令会为当前终端会话设置密钥。

在 macOS 或 Linux 上：

```bash
export OPENAI_API_KEY=sk-...
```

在 Windows PowerShell 上：

```powershell
$env:OPENAI_API_KEY = "sk-..."
```

在 Windows 命令提示符中：

```cmd
set "OPENAI_API_KEY=sk-..."
```

## 第一个智能体的创建 {#create-your-first-agent}

智能体通过 instructions、名称以及特定模型等可选配置来定义。

```python
from agents import Agent

agent = Agent(
    name="History Tutor",
    instructions="You answer history questions clearly and concisely.",
)
```

## 第一个智能体的运行 {#run-your-first-agent}

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

对于第二轮交互，您可以将 `result.to_input_list()` 传回 `Runner.run(...)`、附加一个[会话](sessions/index.md)，或者使用 `conversation_id` / `previous_response_id` 复用由OpenAI服务器管理的状态。[智能体运行](running_agents.md)指南对这些方法进行了比较。

可以采用以下经验法则：

| 如果您希望…… | 可以从……开始 |
| --- | --- |
| 完全手动控制，并使用与提供商无关的历史记录 | `result.to_input_list()` |
| 让 SDK 为您加载和保存历史记录 | [`session=...`](sessions/index.md) |
| 使用由OpenAI管理的服务器端续接 | `previous_response_id` 或 `conversation_id` |

有关权衡因素和确切行为，请参阅[智能体运行](running_agents.md#choose-a-memory-strategy)。

当任务主要依赖提示词、工具和对话状态时，请使用普通的 `Agent` 搭配 `Runner`。如果智能体需要在隔离的工作区中检查或修改真实文件，请直接参阅[沙盒智能体快速入门](sandbox_agents.md)。

## 智能体工具的添加 {#give-your-agent-tools}

您可以为智能体提供工具，以查询信息或执行操作。

```python
import asyncio
from agents import Agent, Runner
from agents.decorators import tool


@tool
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

## 更多智能体的添加 {#add-a-few-more-agents}

在选择多智能体模式之前，请先确定由谁负责最终答案：

-   **任务转移**：由专家接管该轮交互中相应部分的对话。
-   **Agents as tools**：编排器保持控制权，并将专家作为工具调用。

本快速入门将继续介绍**任务转移**，因为这是最简短的入门示例。有关管理器风格的模式，请参阅[智能体编排](multi_agent.md)和[工具：agents as tools](tools.md#agents-as-tools)。

其他智能体可以用相同方式定义。`handoff_description` 会为路由智能体提供额外上下文，帮助其判断何时进行委派。

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

## 任务转移的定义 {#define-your-handoffs}

您可以在智能体上定义一组传出任务转移选项，供其在解决任务时选择。

```python
triage_agent = Agent(
    name="Triage Agent",
    instructions="Route each homework question to the right specialist.",
    handoffs=[history_tutor_agent, math_tutor_agent],
)
```

## 智能体编排的运行 {#run-the-agent-orchestration}

运行器负责执行各个智能体、处理所有任务转移以及所有工具调用。

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

## 参考代码示例 {#reference-examples}

代码仓库中包含实现相同核心模式的完整脚本：

-   [`examples/basic/hello_world.py`](https://github.com/openai/openai-agents-python/tree/main/examples/basic/hello_world.py)，用于首次运行。
-   [`examples/basic/tools.py`](https://github.com/openai/openai-agents-python/tree/main/examples/basic/tools.py)，用于函数工具。
-   [`examples/agent_patterns/routing.py`](https://github.com/openai/openai-agents-python/tree/main/examples/agent_patterns/routing.py)，用于多智能体路由。

## 追踪记录的查看 {#view-your-traces}

如需回顾智能体运行期间发生的情况，请前往 [OpenAI 控制面板中的追踪查看器](https://platform.openai.com/traces)，查看智能体运行的追踪记录。

## 后续步骤 {#next-steps}

了解如何构建更复杂的智能体工作流：

-   了解如何配置[智能体](agents.md)。
-   了解[智能体运行](running_agents.md)和[会话](sessions/index.md)。
-   如果工作应在真实工作区内进行，请了解[沙盒智能体](sandbox_agents.md)。
-   了解[工具](tools.md)、[安全防护措施](guardrails.md)和[模型](models/index.md)。