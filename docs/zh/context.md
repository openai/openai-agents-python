---
search:
  exclude: true
---
# 上下文管理

上下文是一个含义丰富的术语。你可能关心的上下文主要有两类：

1. 代码本地可用的上下文：这是工具函数运行时、`on_handoff` 等回调中、生命周期钩子中等可能需要的数据和依赖项。
2. LLM 可用的上下文：这是 LLM 在生成响应时看到的数据。

## 本地上下文

这通过 [`RunContextWrapper`][agents.run_context.RunContextWrapper] 类及其中的 [`context`][agents.run_context.RunContextWrapper.context] 属性来表示。它的工作方式如下：

1. 你创建任意所需的 Python 对象。常见模式是使用 dataclass 或 Pydantic 对象。
2. 你将该对象传递给各种 run 方法（例如 `Runner.run(..., context=whatever)`）。
3. 你的所有工具调用、生命周期钩子等都会收到一个包装对象 `RunContextWrapper[T]`，其中 `T` 表示你的上下文对象类型，你可以通过 `wrapper.context` 访问它。

对于一些运行时特定的回调，SDK 可能会传递 `RunContextWrapper[T]` 的更专用子类。例如，工具调用生命周期钩子通常会接收 `ToolContext`，它还会公开工具调用元数据，例如 `tool_call_id`、`tool_name` 和 `tool_arguments`。

需要注意的**最重要**事项：对于给定的智能体运行，其每个智能体、工具函数、生命周期等都必须使用相同的上下文_类型_。

你可以将上下文用于以下用途：

-   运行所需的上下文数据（例如用户名/uid 或关于用户的其他信息）
-   依赖项（例如日志记录器对象、数据获取器等）
-   辅助函数

!!! danger "注意"

    上下文对象**不会**发送给 LLM。它纯粹是一个本地对象，你可以从中读取、向其写入，并调用其方法。

在单次运行中，派生包装器共享相同的底层应用上下文、审批状态和用量跟踪。嵌套的 [`Agent.as_tool()`][agents.agent.Agent.as_tool] 运行可能会附加不同的 `tool_input`，但默认情况下不会获得应用状态的隔离副本。

### `RunContextWrapper` 公开的内容

[`RunContextWrapper`][agents.run_context.RunContextWrapper] 是围绕你应用定义的上下文对象的包装器。实践中你最常使用的是：

-   [`wrapper.context`][agents.run_context.RunContextWrapper.context]：用于你自己的可变应用状态和依赖项。
-   [`wrapper.usage`][agents.run_context.RunContextWrapper.usage]：用于当前运行中聚合的请求和 token 用量。
-   [`wrapper.tool_input`][agents.run_context.RunContextWrapper.tool_input]：用于当前运行在 [`Agent.as_tool()`][agents.agent.Agent.as_tool] 内执行时的结构化输入。
-   [`wrapper.approve_tool(...)`][agents.run_context.RunContextWrapper.approve_tool] / [`wrapper.reject_tool(...)`][agents.run_context.RunContextWrapper.reject_tool]：当你需要以编程方式更新审批状态时使用。

只有 `wrapper.context` 是你应用定义的对象。其他字段都是由 SDK 管理的运行时元数据。

如果你之后为了人工介入或持久化作业工作流而序列化 [`RunState`][agents.run_state.RunState]，这些运行时元数据会随状态一起保存。如果你打算持久化或传输序列化状态，请避免在 [`RunContextWrapper.context`][agents.run_context.RunContextWrapper.context] 中放入密钥。

会话状态是另一个单独的问题。根据你希望如何延续多轮对话，可以使用 `result.to_input_list()`、`session`、`conversation_id` 或 `previous_response_id`。有关该决策，请参见[结果](results.md)、[运行智能体](running_agents.md)和[会话](sessions/index.md)。

```python
import asyncio
from dataclasses import dataclass

from agents import Agent, RunContextWrapper, Runner, function_tool

@dataclass
class UserInfo:  # (1)!
    name: str
    uid: int

@function_tool
async def fetch_user_age(wrapper: RunContextWrapper[UserInfo]) -> str:  # (2)!
    """Fetch the age of the user. Call this function to get user's age information."""
    return f"The user {wrapper.context.name} is 47 years old"

async def main():
    user_info = UserInfo(name="John", uid=123)

    agent = Agent[UserInfo](  # (3)!
        name="Assistant",
        tools=[fetch_user_age],
    )

    result = await Runner.run(  # (4)!
        starting_agent=agent,
        input="What is the age of the user?",
        context=user_info,
    )

    print(result.final_output)  # (5)!
    # The user John is 47 years old.

if __name__ == "__main__":
    asyncio.run(main())
```

1. 这是上下文对象。这里我们使用了 dataclass，但你可以使用任意类型。
2. 这是一个工具。你可以看到它接收 `RunContextWrapper[UserInfo]`。工具实现会从上下文中读取信息。
3. 我们用泛型 `UserInfo` 标记该智能体，这样类型检查器就能捕获错误（例如，如果我们尝试传入一个接收不同上下文类型的工具）。
4. 上下文会传递给 `run` 函数。
5. 智能体会正确调用该工具并获得年龄。

---

### 高级：`ToolContext`

在某些情况下，你可能希望访问有关正在执行的工具的额外元数据，例如其名称、调用 ID 或原始参数字符串。  
为此，你可以使用 [`ToolContext`][agents.tool_context.ToolContext] 类，它扩展了 `RunContextWrapper`。

```python
from typing import Annotated
from pydantic import BaseModel, Field
from agents import Agent, Runner, function_tool
from agents.tool_context import ToolContext

class WeatherContext(BaseModel):
    user_id: str

class Weather(BaseModel):
    city: str = Field(description="The city name")
    temperature_range: str = Field(description="The temperature range in Celsius")
    conditions: str = Field(description="The weather conditions")

@function_tool
def get_weather(ctx: ToolContext[WeatherContext], city: Annotated[str, "The city to get the weather for"]) -> Weather:
    print(f"[debug] Tool context: (name: {ctx.tool_name}, call_id: {ctx.tool_call_id}, args: {ctx.tool_arguments})")
    return Weather(city=city, temperature_range="14-20C", conditions="Sunny with wind.")

agent = Agent(
    name="Weather Agent",
    instructions="You are a helpful agent that can tell the weather of a given city.",
    tools=[get_weather],
)
```

`ToolContext` 提供与 `RunContextWrapper` 相同的 `.context` 属性，  
此外还提供当前工具调用特有的额外字段：

- `tool_name` – 被调用工具的名称  
- `tool_call_id` – 此工具调用的唯一标识符  
- `tool_arguments` – 传递给工具的原始参数字符串  
- `tool_namespace` – 工具调用的 Responses 命名空间，当工具通过 `tool_namespace()` 或其他带命名空间的表面加载时可用  
- `qualified_tool_name` – 当有可用命名空间时，带命名空间限定的工具名称  

当你在执行期间需要工具级元数据时，请使用 `ToolContext`。  
对于智能体与工具之间的一般上下文共享，`RunContextWrapper` 仍然足够。由于 `ToolContext` 扩展了 `RunContextWrapper`，当嵌套的 `Agent.as_tool()` 运行提供了结构化输入时，它也可以公开 `.tool_input`。

---

## 智能体/LLM 上下文

调用 LLM 时，它**唯一**能看到的数据来自对话历史。这意味着，如果你想让一些新数据可供 LLM 使用，就必须以能让这些数据出现在该历史中的方式来提供。有几种方式可以做到这一点：

1. 你可以将其添加到智能体的 `instructions` 中。这也称为“系统提示词”或“开发者消息”。系统提示词可以是静态字符串，也可以是接收上下文并输出字符串的动态函数。对于始终有用的信息（例如用户姓名或当前日期），这是一种常见策略。
2. 在调用 `Runner.run` 函数时将其添加到 `input` 中。这类似于 `instructions` 策略，但允许你使用在[指令链](https://cdn.openai.com/spec/model-spec-2024-05-08.html#follow-the-chain-of-command)中层级较低的消息。
3. 通过工具调用公开它。这对于_按需_上下文很有用——LLM 决定何时需要某些数据，并可以调用工具来获取这些数据。
4. 使用检索或网络检索。这些是特殊工具，能够从文件或数据库中获取相关数据（检索），或从网络获取相关数据（网络检索）。这对于将响应“锚定”在相关上下文数据中很有用。