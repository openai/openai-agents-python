---
search:
  exclude: true
---
# 智能体

智能体是应用中的核心构建块。智能体是一个大语言模型（LLM），配置了 instructions、工具，以及可选的运行时行为，例如任务转移、安全防护措施和 structured outputs。

当你想定义或自定义单个普通 `Agent` 时，请使用本页。如果你正在决定多个智能体应如何协作，请阅读[智能体编排](multi_agent.md)。如果智能体应在包含清单定义文件和沙箱原生能力的隔离工作区内运行，请阅读[沙箱智能体概念](sandbox/guide.md)。

SDK 对 OpenAI 模型默认使用 Responses API，但这里的区别在于编排：`Agent` 加 `Runner` 让 SDK 为你管理轮次、工具、安全防护措施、任务转移和会话。如果你想自己掌控这个循环，请直接使用 Responses API。

## 下一篇指南的选择

使用本页作为智能体定义的中心。跳转到与你接下来需要做出的决定相匹配的相邻指南。

| 如果你想要... | 接下来阅读 |
| --- | --- |
| 选择模型或服务商设置 | [模型](models/index.md) |
| 为智能体添加能力 | [工具](tools.md) |
| 针对真实代码仓库、文档包或隔离工作区运行智能体 | [沙箱智能体快速入门](sandbox_agents.md) |
| 在管理器风格的编排与任务转移之间做决定 | [智能体编排](multi_agent.md) |
| 配置任务转移行为 | [任务转移](handoffs.md) |
| 运行轮次、流式传输事件或管理对话状态 | [运行智能体](running_agents.md) |
| 检查最终输出、运行项或可恢复状态 | [结果](results.md) |
| 共享本地依赖和运行时状态 | [上下文管理](context.md) |

## 基本配置

智能体最常见的属性包括：

| 属性 | 必需 | 描述 |
| --- | --- | --- |
| `name` | 是 | 人类可读的智能体名称。 |
| `instructions` | 是 | 系统提示词或动态 instructions 回调。请参阅[动态 instructions](#dynamic-instructions)。 |
| `prompt` | 否 | OpenAI Responses API prompt 配置。接受静态 prompt 对象或函数。请参阅[提示词模板](#prompt-templates)。 |
| `handoff_description` | 否 | 当此智能体作为任务转移目标提供时展示的简短描述。 |
| `handoffs` | 否 | 将对话委托给专家智能体。请参阅[任务转移](handoffs.md)。 |
| `model` | 否 | 使用哪个 LLM。请参阅[模型](models/index.md)。 |
| `model_settings` | 否 | 模型调优参数，例如 `temperature`、`top_p` 和 `tool_choice`。 |
| `tools` | 否 | 智能体可以调用的工具。请参阅[工具](tools.md)。 |
| `mcp_servers` | 否 | 面向智能体、由 MCP 支持的工具。请参阅 [MCP 指南](mcp.md)。 |
| `mcp_config` | 否 | 微调 MCP 工具的准备方式，例如严格 schema 转换和 MCP 失败格式化。请参阅 [MCP 指南](mcp.md#agent-level-mcp-configuration)。 |
| `input_guardrails` | 否 | 在此智能体链的第一个用户输入上运行的安全防护措施。请参阅[安全防护措施](guardrails.md)。 |
| `output_guardrails` | 否 | 在此智能体最终输出上运行的安全防护措施。请参阅[安全防护措施](guardrails.md)。 |
| `output_type` | 否 | 使用结构化输出类型而非纯文本。请参阅[输出类型](#output-types)。 |
| `hooks` | 否 | 作用于智能体范围的生命周期回调。请参阅[生命周期事件（hooks）](#lifecycle-events-hooks)。 |
| `tool_use_behavior` | 否 | 控制工具结果是回传给模型还是结束运行。请参阅[工具使用行为](#tool-use-behavior)。 |
| `reset_tool_choice` | 否 | 在工具调用后重置 `tool_choice`（默认：`True`），以避免工具使用循环。请参阅[强制使用工具](#forcing-tool-use)。 |

```python
from agents import Agent, ModelSettings, function_tool

@function_tool
def get_weather(city: str) -> str:
    """returns weather info for the specified city."""
    return f"The weather in {city} is sunny"

agent = Agent(
    name="Haiku agent",
    instructions="Always respond in haiku form",
    model="gpt-5-nano",
    tools=[get_weather],
)
```

本节中的所有内容都适用于 `Agent`。`SandboxAgent` 基于相同理念构建，并额外添加了 `default_manifest`、`base_instructions`、`capabilities` 和 `run_as`，用于工作区范围的运行。请参阅[沙箱智能体概念](sandbox/guide.md)。

## 提示词模板

你可以通过设置 `prompt` 来引用在 OpenAI 平台中创建的提示词模板。这适用于使用 Responses API 的 OpenAI 模型。

要使用它，请：

1. 前往 https://platform.openai.com/playground/prompts
2. 创建新的 prompt 变量 `poem_style`。
3. 创建一个包含以下内容的系统提示词：

    ```
    Write a poem in {{poem_style}}
    ```

4. 使用 `--prompt-id` 标志运行示例。

```python
from agents import Agent

agent = Agent(
    name="Prompted assistant",
    prompt={
        "id": "pmpt_123",
        "version": "1",
        "variables": {"poem_style": "haiku"},
    },
)
```

你也可以在运行时动态生成提示词：

```python
from dataclasses import dataclass

from agents import Agent, GenerateDynamicPromptData, Runner

@dataclass
class PromptContext:
    prompt_id: str
    poem_style: str


async def build_prompt(data: GenerateDynamicPromptData):
    ctx: PromptContext = data.context.context
    return {
        "id": ctx.prompt_id,
        "version": "1",
        "variables": {"poem_style": ctx.poem_style},
    }


agent = Agent(name="Prompted assistant", prompt=build_prompt)
result = await Runner.run(
    agent,
    "Say hello",
    context=PromptContext(prompt_id="pmpt_123", poem_style="limerick"),
)
```

## 上下文

智能体在其 `context` 类型上是泛型的。上下文是一种依赖注入工具：它是你创建并传递给 `Runner.run()` 的对象，会被传递给每个智能体、工具、任务转移等，并作为智能体运行所需依赖和状态的集合。你可以提供任何 Python 对象作为上下文。

阅读[上下文指南](context.md)，了解完整的 `RunContextWrapper` 接口、共享用量跟踪、嵌套 `tool_input` 以及序列化注意事项。

```python
@dataclass
class UserContext:
    name: str
    uid: str
    is_pro_user: bool

    async def fetch_purchases() -> list[Purchase]:
        return ...

agent = Agent[UserContext](
    ...,
)
```

## 输出类型

默认情况下，智能体生成纯文本（即 `str`）输出。如果你希望智能体生成特定类型的输出，可以使用 `output_type` 参数。常见选择是使用 [Pydantic](https://docs.pydantic.dev/) 对象，但我们支持任何可以包装在 Pydantic [TypeAdapter](https://docs.pydantic.dev/latest/api/type_adapter/) 中的类型——dataclasses、列表、TypedDict 等。

```python
from pydantic import BaseModel
from agents import Agent


class CalendarEvent(BaseModel):
    name: str
    date: str
    participants: list[str]

agent = Agent(
    name="Calendar extractor",
    instructions="Extract calendar events from text",
    output_type=CalendarEvent,
)
```

!!! note

    当你传入 `output_type` 时，这会告诉模型使用 [structured outputs](https://platform.openai.com/docs/guides/structured-outputs)，而不是常规纯文本响应。

## 多智能体系统设计模式

设计多智能体系统有许多方式，但我们通常看到两种广泛适用的模式：

1. 管理器（agents as tools）：中央管理器/编排器将专用子智能体作为工具调用，并保留对对话的控制权。
2. 任务转移：对等智能体将控制权转交给接管对话的专用智能体。这是去中心化的。

更多详情，请参阅[我们的智能体构建实用指南](https://cdn.openai.com/business-guides-and-resources/a-practical-guide-to-building-agents.pdf)。

### 管理器（agents as tools）

`customer_facing_agent` 处理所有用户交互，并调用作为工具暴露的专用子智能体。请在[工具](tools.md#agents-as-tools)文档中阅读更多内容。

```python
from agents import Agent

booking_agent = Agent(...)
refund_agent = Agent(...)

customer_facing_agent = Agent(
    name="Customer-facing agent",
    instructions=(
        "Handle all direct user communication. "
        "Call the relevant tools when specialized expertise is needed."
    ),
    tools=[
        booking_agent.as_tool(
            tool_name="booking_expert",
            tool_description="Handles booking questions and requests.",
        ),
        refund_agent.as_tool(
            tool_name="refund_expert",
            tool_description="Handles refund questions and requests.",
        )
    ],
)
```

### 任务转移

任务转移是智能体可以委托给的子智能体。当发生任务转移时，被委托的智能体会接收对话历史并接管对话。此模式支持模块化的专用智能体，使其在单一任务上表现出色。请在[任务转移](handoffs.md)文档中阅读更多内容。

```python
from agents import Agent

booking_agent = Agent(...)
refund_agent = Agent(...)

triage_agent = Agent(
    name="Triage agent",
    instructions=(
        "Help the user with their questions. "
        "If they ask about booking, hand off to the booking agent. "
        "If they ask about refunds, hand off to the refund agent."
    ),
    handoffs=[booking_agent, refund_agent],
)
```

## 动态 instructions

在大多数情况下，你可以在创建智能体时提供 instructions。不过，你也可以通过函数提供动态 instructions。该函数将接收智能体和上下文，并且必须返回 prompt。普通函数和 `async` 函数都受支持。

```python
def dynamic_instructions(
    context: RunContextWrapper[UserContext], agent: Agent[UserContext]
) -> str:
    return f"The user's name is {context.context.name}. Help them with their questions."


agent = Agent[UserContext](
    name="Triage agent",
    instructions=dynamic_instructions,
)
```

## 生命周期事件（hooks）

有时，你希望观察智能体的生命周期。例如，你可能希望在某些事件发生时记录事件、预取数据或记录用量。

有两个 hook 作用域：

-   [`RunHooks`][agents.lifecycle.RunHooks] 观察整个 `Runner.run(...)` 调用，包括到其他智能体的任务转移。
-   [`AgentHooks`][agents.lifecycle.AgentHooks] 通过 `agent.hooks` 附加到特定智能体实例。

回调上下文也会根据事件变化：

-   智能体开始/结束 hooks 接收 [`AgentHookContext`][agents.run_context.AgentHookContext]，它包装你的原始上下文并携带共享的运行用量状态。
-   LLM、工具和任务转移 hooks 接收 [`RunContextWrapper`][agents.run_context.RunContextWrapper]。

典型 hook 时机：

-   `on_agent_start` / `on_agent_end`：当特定智能体开始或完成生成最终输出时。
-   `on_llm_start` / `on_llm_end`：紧邻每次模型调用前后。
-   `on_tool_start` / `on_tool_end`：围绕每次本地工具调用。
    对于工具调用，hook `context` 通常是 `ToolContext`，因此你可以检查工具调用元数据，例如 `tool_call_id`。
-   `on_handoff`：当控制权从一个智能体转移到另一个智能体时。

当你希望为整个工作流设置一个观察者时使用 `RunHooks`；当某个智能体需要自定义副作用时使用 `AgentHooks`。

```python
from agents import Agent, RunHooks, Runner


class LoggingHooks(RunHooks):
    async def on_agent_start(self, context, agent):
        print(f"Starting {agent.name}")

    async def on_llm_end(self, context, agent, response):
        print(f"{agent.name} produced {len(response.output)} output items")

    async def on_agent_end(self, context, agent, output):
        print(f"{agent.name} finished with usage: {context.usage}")


agent = Agent(name="Assistant", instructions="Be concise.")
result = await Runner.run(agent, "Explain quines", hooks=LoggingHooks())
print(result.final_output)
```

完整的回调接口请参阅[生命周期 API 参考](ref/lifecycle.md)。

## 安全防护措施

安全防护措施允许你在智能体运行的同时并行对用户输入进行检查/验证，并在智能体输出生成后对其进行检查/验证。例如，你可以筛查用户输入和智能体输出的相关性。请在[安全防护措施](guardrails.md)文档中阅读更多内容。

## 智能体的克隆/复制

通过在智能体上使用 `clone()` 方法，你可以复制一个 Agent，并可选择更改任何你想要的属性。

```python
pirate_agent = Agent(
    name="Pirate",
    instructions="Write like a pirate",
    model="gpt-5.5",
)

robot_agent = pirate_agent.clone(
    name="Robot",
    instructions="Write like a robot",
)
```

## 强制使用工具

提供工具列表并不总意味着 LLM 会使用工具。你可以通过设置 [`ModelSettings.tool_choice`][agents.model_settings.ModelSettings.tool_choice] 来强制使用工具。有效值包括：

1. `auto`，允许 LLM 决定是否使用工具。
2. `required`，要求 LLM 使用工具（但它可以智能地决定使用哪个工具）。
3. `none`，要求 LLM _不_ 使用工具。
4. 设置特定字符串，例如 `my_tool`，要求 LLM 使用该特定工具。

当你使用 OpenAI Responses 工具搜索时，命名工具选择受到更多限制：你不能用 `tool_choice` 指向裸命名空间名称或仅延迟的工具，并且 `tool_choice="tool_search"` 不会指向 [`ToolSearchTool`][agents.tool.ToolSearchTool]。在这些情况下，优先使用 `auto` 或 `required`。有关 Responses 特有的约束，请参阅[托管工具搜索](tools.md#hosted-tool-search)。

```python
from agents import Agent, Runner, function_tool, ModelSettings

@function_tool
def get_weather(city: str) -> str:
    """Returns weather info for the specified city."""
    return f"The weather in {city} is sunny"

agent = Agent(
    name="Weather Agent",
    instructions="Retrieve weather details.",
    tools=[get_weather],
    model_settings=ModelSettings(tool_choice="get_weather")
)
```

## 工具使用行为

`Agent` 配置中的 `tool_use_behavior` 参数控制如何处理工具输出：

- `"run_llm_again"`：默认值。运行工具，并由 LLM 处理结果以生成最终响应。
- `"stop_on_first_tool"`：将第一次工具调用的输出用作最终响应，而不再进行 LLM 处理。

```python
from agents import Agent, Runner, function_tool, ModelSettings

@function_tool
def get_weather(city: str) -> str:
    """Returns weather info for the specified city."""
    return f"The weather in {city} is sunny"

agent = Agent(
    name="Weather Agent",
    instructions="Retrieve weather details.",
    tools=[get_weather],
    tool_use_behavior="stop_on_first_tool"
)
```

- `StopAtTools(stop_at_tool_names=[...])`：如果调用了任何指定工具，则停止，并将其输出用作最终响应。

```python
from agents import Agent, Runner, function_tool
from agents.agent import StopAtTools

@function_tool
def get_weather(city: str) -> str:
    """Returns weather info for the specified city."""
    return f"The weather in {city} is sunny"

@function_tool
def sum_numbers(a: int, b: int) -> int:
    """Adds two numbers."""
    return a + b

agent = Agent(
    name="Stop At Stock Agent",
    instructions="Get weather or sum numbers.",
    tools=[get_weather, sum_numbers],
    tool_use_behavior=StopAtTools(stop_at_tool_names=["get_weather"])
)
```

- `ToolsToFinalOutputFunction`：自定义函数，用于处理工具结果并决定是停止还是继续使用 LLM。

```python
from agents import Agent, Runner, function_tool, FunctionToolResult, RunContextWrapper
from agents.agent import ToolsToFinalOutputResult
from typing import List, Any

@function_tool
def get_weather(city: str) -> str:
    """Returns weather info for the specified city."""
    return f"The weather in {city} is sunny"

def custom_tool_handler(
    context: RunContextWrapper[Any],
    tool_results: List[FunctionToolResult]
) -> ToolsToFinalOutputResult:
    """Processes tool results to decide final output."""
    for result in tool_results:
        if result.output and "sunny" in result.output:
            return ToolsToFinalOutputResult(
                is_final_output=True,
                final_output=f"Final weather: {result.output}"
            )
    return ToolsToFinalOutputResult(
        is_final_output=False,
        final_output=None
    )

agent = Agent(
    name="Weather Agent",
    instructions="Retrieve weather details.",
    tools=[get_weather],
    tool_use_behavior=custom_tool_handler
)
```

!!! note

    为防止无限循环，框架会在工具调用后自动将 `tool_choice` 重置为 "auto"。此行为可通过 [`agent.reset_tool_choice`][agents.agent.Agent.reset_tool_choice] 配置。无限循环的原因是工具结果会发送给 LLM，而 LLM 随后又会因为 `tool_choice` 生成另一个工具调用，如此反复。