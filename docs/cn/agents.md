# 智能体

智能体是应用程序中的核心构建块。一个智能体是一个大型语言模型（LLM），配置了指令和工具。

## 基本配置

智能体最常用到的配置属性包括：

-   `name`: 一个必需的字符串，用于标识你的智能体。
-   `instructions`: 也称为开发者消息或系统提示。
-   `model`: 使用哪个LLM，以及可选的 `model_settings` 来配置模型调优参数如temperature、top_p等。
-   `tools`: 智能体可以用来完成任务的工具。

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

## 上下文

智能体对于其 `context` 类型是通用的。上下文是一种依赖注入工具：它是你创建并传递给 `Runner.run()` 的对象，会传递给每个智能体、工具、交接等，作为智能体运行的依赖项和状态的容器。你可以提供任何Python对象作为上下文。

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

默认情况下，智能体会产生纯文本（即 `str`）输出。如果你想让智能体产生特定类型的输出，可以使用 `output_type` 参数。一个常见的选择是使用 [Pydantic](https://docs.pydantic.dev/) 对象，但我们支持任何可以包装在 Pydantic [TypeAdapter](https://docs.pydantic.dev/latest/api/type_adapter/) 中的类型 - 数据类、列表、TypedDict等。

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

    当你传递一个 `output_type` 时，这告诉模型使用 [结构化输出](https://platform.openai.com/docs/guides/structured-outputs) 而不是常规的纯文本响应。

## 多智能体系统设计模式

设计多智能体系统有很多方法，但我们通常看到两种广泛适用的模式：

1. 管理器（智能体作为工具）：一个中央管理器/编排器调用作为工具公开的专门子智能体，并保持对对话的控制。
2. 交接：对等智能体将控制权委托给一个专门的智能体，该智能体接管对话。这是分散式的。

更多详细信息请参见[我们的智能体构建实用指南](https://cdn.openai.com/business-guides-and-resources/a-practical-guide-to-building-agents.pdf)。

### 管理器（智能体作为工具）

`customer_facing_agent` 处理所有用户交互，并调用作为工具公开的专门子智能体。更多详细信息请参见 [tools](tools.md#agents-as-tools) 文档。

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

### 交接

交接是智能体可以委托的子智能体。当发生交接时，被委托的智能体接收对话历史并接管对话。这种模式使得在单一任务上表现出色的模块化专门智能体成为可能。更多详细信息请参见 [handoffs](handoffs.md) 文档。

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

## 动态指令

在大多数情况下，你可以在创建智能体时提供指令。然而，你也可以通过函数提供动态指令。该函数将接收智能体和上下文，并必须返回提示。既可以使用普通函数，也可以使用 `async` 函数。

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

## 生命周期事件（钩子）

有时候，你可能想要观察智能体的生命周期。例如，你可能想要记录事件，或者在某些事件发生时预取数据。你可以通过 `hooks` 属性在智能体的生命周期中设置钩子。子类化 [`AgentHooks`][agents.lifecycle.AgentHooks] 类，并覆盖你感兴趣的方法。

## 护栏

护栏允许你在智能体运行的同时并行运行对用户输入的检查/验证，并在智能体输出产生后对其进行检查/验证。例如，你可以根据相关性筛选用户输入和智能体输出。更多详细信息请参见 [guardrails](guardrails.md) 文档。

## 克隆/复制智能体

通过使用智能体上的 `clone()` 方法，你可以复制智能体，并选择性地更改任何属性。

```python
pirate_agent = Agent(
    name="Pirate",
    instructions="Write like a pirate",
    model="gpt-4.1",
)

robot_agent = pirate_agent.clone(
    name="Robot",
    instructions="Write like a robot",
)
```

## 强制工具使用

提供工具列表并不总是意味着LLM会使用工具。你可以通过设置 [`ModelSettings.tool_choice`][agents.model_settings.ModelSettings.tool_choice] 来强制工具使用。有效值包括：

1. `auto`，允许LLM决定是否使用工具。
2. `required`，要求LLM使用工具（但它可以智能地决定使用哪个工具）。
3. `none`，要求LLM _不_ 使用工具。
4. 设置特定字符串，例如 `my_tool`，要求LLM使用该特定工具。

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

- `"run_llm_again"`: 默认。工具运行后，LLM处理结果以产生最终响应。
- `"stop_on_first_tool"`: 第一个工具调用的输出用作最终响应，无需进一步的LLM处理。

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

- `StopAtTools(stop_at_tool_names=[...])`: 指定したツールのいずれかが呼び出された場合に停止し、その出力を最終応答として使用します。

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

- `ToolsToFinalOutputFunction`: ツール結果を処理し、停止するか LLM を継続するかを判断するカスタム関数です。

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

    为了防止无限循环，框架会在工具调用后自动将 `tool_choice` 重置为 "auto"。此行为可通过 [`agent.reset_tool_choice`][agents.agent.Agent.reset_tool_choice] 进行配置。无限循环的发生是因为工具结果被发送给LLM，然后由于 `tool_choice` 导致LLM再次生成工具调用，如此循环往复。