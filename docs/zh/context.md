---
search:
  exclude: true
---
# 上下文管理

“上下文”一词含义广泛。你可能关心的上下文主要有两类：

1. 代码本地可用的上下文：这是在工具函数运行时、`on_handoff` 等回调、生命周期钩子中可能需要的数据和依赖。
2. LLM 可用的上下文：这是 LLM 在生成回复时可见的数据。

## 本地上下文

这通过 [`RunContextWrapper`][agents.run_context.RunContextWrapper] 类以及其中的 [`context`][agents.run_context.RunContextWrapper.context] 属性来表示。工作方式如下：

1. 创建任意你想要的 Python 对象。常见做法是使用 dataclass 或 Pydantic 对象。
2. 将该对象传给各种运行方法（例如 `Runner.run(..., **context=whatever**)`）。
3. 你所有的工具调用、生命周期钩子等都会接收到一个包装对象 `RunContextWrapper[T]`，其中 `T` 表示你的上下文对象类型，你可以通过 `wrapper.context` 访问。

需要注意的最重要的一点：对于一次给定的智能体运行，所有智能体、工具函数、生命周期等都必须使用相同类型的上下文。

你可以将上下文用于以下场景：

-  该次运行的情境化数据（例如用户名/uid 或关于用户的其他信息）
-  依赖项（例如日志记录器对象、数据获取器等）
-  帮助函数

!!! danger "注意"

    该上下文对象并不会发送给 LLM。它纯粹是一个本地对象，你可以读取、写入并在其上调用方法。

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

1. 这是上下文对象。我们这里使用了 dataclass，但你可以使用任何类型。
2. 这是一个工具。你可以看到它接收 `RunContextWrapper[UserInfo]`。工具实现会从上下文中读取。
3. 我们在智能体上标注了泛型 `UserInfo`，以便类型检查器能捕获错误（例如，如果我们尝试传入一个接收不同上下文类型的工具）。
4. 将上下文传递给 `run` 函数。
5. 智能体正确调用工具并获取年龄。

---

### 高级：`ToolContext`

在某些情况下，你可能希望访问关于正在执行的工具的额外元数据——例如其名称、调用 ID 或原始参数字符串。  
为此，你可以使用扩展自 `RunContextWrapper` 的 [`ToolContext`][agents.tool_context.ToolContext] 类。

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
并额外包含针对当前工具调用的字段：

- `tool_name` – 正在调用的工具名称  
- `tool_call_id` – 此次工具调用的唯一标识符  
- `tool_arguments` – 传给工具的原始参数字符串  

当你在执行期间需要工具级元数据时，请使用 `ToolContext`。  
对于智能体与工具之间的一般上下文共享，`RunContextWrapper` 已经足够。

---

## 智能体/LLM 上下文

当调用 LLM 时，它能看到的唯一数据来自对话历史。这意味着如果你希望让一些新数据对 LLM 可见，你必须以一种方式将其纳入该历史中。常见方式包括：

1. 将其添加到智能体的 `instructions`。这也被称为“系统提示词”或“开发者消息”。系统提示词可以是静态字符串，也可以是接收上下文并输出字符串的动态函数。这对于始终有用的信息很常见（例如用户名或当前日期）。
2. 在调用 `Runner.run` 函数时将其添加到 `input`。这与 `instructions` 策略类似，但允许你放置更处于[指挥链](https://cdn.openai.com/spec/model-spec-2024-05-08.html#follow-the-chain-of-command)更低位置的消息。
3. 通过 工具调用 暴露。这对按需上下文很有用——LLM 决定何时需要某些数据，并可调用工具以获取该数据。
4. 使用 文件检索 或 网络检索。这些是能够从文件或数据库（文件检索）或从网络（网络检索）中获取相关数据的特殊工具。这对于将回复“锚定”在相关上下文数据上很有用。