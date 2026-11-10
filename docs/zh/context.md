---
search:
  exclude: true
---
# 上下文管理

上下文是一个含义多样的术语。你可能需要关注两类主要的上下文：

1. 代码本地可用的上下文：这是工具函数运行、执行 `on_handoff` 等回调、调用生命周期钩子时可能需要的数据和依赖项。
2. LLM 可用的上下文：这是 LLM 生成响应时能够看到的数据。

## 本地上下文 {#local-context}

本地上下文通过 [`RunContextWrapper`][agents.run_context.RunContextWrapper] 类及其中的 [`context`][agents.run_context.RunContextWrapper.context] 属性表示。其工作方式如下：

1. 创建任意 Python 对象。常见做法是使用数据类或 Pydantic 对象。
2. 将该对象传给各种运行方法（例如 `Runner.run(..., context=whatever)`）。
3. 所有工具调用、生命周期钩子等都会收到一个包装器对象 `RunContextWrapper[T]`，其中 `T` 表示上下文对象的类型；该对象本身可通过 `wrapper.context` 获取。

对于某些特定于运行时的回调，SDK 可能会传入 `RunContextWrapper[T]` 的更具体子类。例如，`FunctionTool` 实例的生命周期钩子通常会收到 `ToolContext`，后者还会提供 `tool_call_id`、`tool_name` 和 `tool_arguments` 等工具调用元数据。

需要注意的**最重要**一点是：在一次智能体运行中，每个智能体、工具函数和生命周期钩子等都必须使用相同_类型_的上下文。

上下文可用于：

-   本次运行的上下文数据（例如用户名/uid 或其他用户信息）
-   依赖项（例如日志记录器对象、数据获取器等）
-   辅助函数

!!! danger "注意"

    上下文对象**不会**发送给 LLM。它完全是一个本地对象，你可以读取、写入该对象，也可以调用其方法。

在单次运行中，派生包装器共享同一底层应用上下文、审批状态和用量追踪。嵌套的 [`Agent.as_tool()`][agents.agent.Agent.as_tool] 运行可能会附加不同的 `tool_input`，但默认不会获得应用状态的独立副本。

### 能力可见性中的本地上下文用法 {#use-local-context-for-capability-visibility}

当函数工具、MCP 工具和任务转移依赖同一请求策略时，请将策略输入或辅助函数保存在应用上下文中。SDK 的各个接口分别通过各自的回调提供当前运行上下文：

-   [`FunctionTool.is_enabled`][agents.tool.FunctionTool.is_enabled] 接收一个 `RunContextWrapper`。
-   [`Handoff.is_enabled`][agents.handoffs.Handoff.is_enabled] 接收一个 `RunContextWrapper`。
-   MCP [`tool_filter`](mcp.md#dynamic-tool-filtering) 接收一个 [`ToolFilterContext`][agents.mcp.ToolFilterContext]，其 `run_context` 属性包含当前的 `RunContextWrapper`。

请让这些回调适配共享的应用策略，而不是维护单独的能力列表。这些回调用于控制 SDK 在当前运行中公开哪些能力；它们无法授权模型生成的参数或资源选择。对于函数工具，请在工具实现内部强制执行这些决策，或酌情使用[工具输入安全防护措施](guardrails.md#tool-guardrails)和[审批](human_in_the_loop.md)。MCP 服务器必须自行授权其受保护的操作。对于带有 `input_type` 的任务转移，请在 `on_handoff` 开始时、应用产生副作用之前检查解析后的输入，并在授权失败时抛出异常，而不是直接返回。工具输入安全防护措施不会对任务转移运行。有关回调生命周期，请参阅[任务转移输入](handoffs.md#handoff-inputs)。

### `RunContextWrapper` 提供的内容 {#what-runcontextwrapper-exposes}

[`RunContextWrapper`][agents.run_context.RunContextWrapper] 是应用自定义上下文对象的包装器。实际使用中，最常用的是：

-   [`wrapper.context`][agents.run_context.RunContextWrapper.context]：用于应用自身的可变状态和依赖项。
-   [`wrapper.usage`][agents.run_context.RunContextWrapper.usage]：用于当前运行中汇总的请求和 token 用量。
-   [`wrapper.tool_input`][agents.run_context.RunContextWrapper.tool_input]：用于当前运行在 [`Agent.as_tool()`][agents.agent.Agent.as_tool] 内执行时的结构化输入。
-   [`wrapper.approve_tool(...)`][agents.run_context.RunContextWrapper.approve_tool] / [`wrapper.reject_tool(...)`][agents.run_context.RunContextWrapper.reject_tool]：用于以编程方式更新审批状态。

只有 `wrapper.context` 是应用自定义的对象。其他字段均为 SDK 管理的运行时元数据。

如果之后要为人工介入或持久化任务工作流序列化 [`RunState`][agents.run_state.RunState]，这些运行时元数据也会随状态一同保存。如果计划持久化或传输序列化状态，请避免在 [`RunContextWrapper.context`][agents.run_context.RunContextWrapper.context] 中存放机密信息。

对话状态是另一个独立问题。请根据希望如何延续对话轮次，使用 `result.to_input_list()`、`session`、`conversation_id` 或 `previous_response_id`。有关如何选择，请参阅[结果](results.md)、[运行智能体](running_agents.md)和[会话](sessions/index.md)。

```python
import asyncio
from dataclasses import dataclass

from agents import Agent, RunContextWrapper, Runner
from agents.decorators import tool


@dataclass
class UserInfo:  # (1)!
    name: str
    uid: int


@tool
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

1. 这是上下文对象。此处使用了数据类，但你可以使用任意类型。
2. 这是一个工具。可以看到，它接收一个 `RunContextWrapper[UserInfo]`。该工具的实现会读取上下文。
3. 我们为智能体标注泛型 `UserInfo`，以便类型检查器捕获错误（例如，尝试传入使用不同上下文类型的工具时）。
4. 上下文会传给 `run` 函数。
5. 智能体正确调用工具并获取年龄。

---

### 高级用法：`ToolContext` {#advanced-toolcontext}

在某些情况下，你可能希望访问有关正在执行的工具的额外元数据，例如工具名称、调用 ID 或原始参数字符串。  
为此，可以使用 [`ToolContext`][agents.tool_context.ToolContext] 类，它扩展了 `RunContextWrapper`。

```python
from typing import Annotated
from pydantic import BaseModel, Field
from agents import Agent
from agents.decorators import tool
from agents.tool_context import ToolContext


class WeatherContext(BaseModel):
    user_id: str


class Weather(BaseModel):
    city: str = Field(description="The city name")
    temperature_range: str = Field(description="The temperature range in Celsius")
    conditions: str = Field(description="The weather conditions")


@tool
def get_weather(
    ctx: ToolContext[WeatherContext], city: Annotated[str, "The city to get the weather for"]
) -> Weather:
    print(
        f"[debug] Tool context: (name: {ctx.tool_name}, call_id: {ctx.tool_call_id}, args: {ctx.tool_arguments})"
    )
    return Weather(city=city, temperature_range="14-20C", conditions="Sunny with wind.")


agent = Agent(
    name="Weather Agent",
    instructions="You are a helpful agent that can tell the weather of a given city.",
    tools=[get_weather],
)
```

`ToolContext` 提供与 `RunContextWrapper` 相同的 `.context` 属性，  
此外还提供当前工具调用特有的字段：

- `tool_name` – 被调用工具的名称  
- `tool_call_id` – 此工具调用的唯一标识符  
- `tool_arguments` – 传给工具的原始参数字符串  
- `tool_namespace` – 工具调用的 Responses 命名空间，仅当工具通过 `tool_namespace()` 或其他带命名空间的接口加载时提供  
- `qualified_tool_name` – 存在命名空间时，以该命名空间限定的工具名称  

如果执行期间需要工具级元数据，请使用 `ToolContext`。  
对于智能体与工具之间的一般上下文共享，`RunContextWrapper` 仍然足够。由于 `ToolContext` 扩展了 `RunContextWrapper`，因此当嵌套的 `Agent.as_tool()` 运行提供结构化输入时，它也可以提供 `.tool_input`。

---

## 智能体/LLM上下文 {#agentllm-context}

调用 LLM 时，它**唯一**能看到的数据来自对话历史记录。这意味着，如果希望 LLM 能够访问某些新数据，必须以可将其纳入对话历史记录的方式提供这些数据。可通过以下几种方式实现：

1. 可以将数据添加到智能体的 `instructions` 中。这也称为“系统提示词”或“开发者消息”。系统提示词可以是静态字符串，也可以是接收上下文并输出字符串的动态函数。这是提供始终有用的信息（例如用户名或当前日期）的常用方法。
2. 调用 `Runner.run` 函数时，将数据添加到 `input`。这与 `instructions` 方法类似，但可以让消息在[命令链](https://cdn.openai.com/spec/model-spec-2024-05-08.html#follow-the-chain-of-command)中处于更低层级。
3. 通过 `FunctionTool` 实例公开数据。这适用于_按需_上下文：LLM 自行决定何时需要某些数据，并可调用工具获取这些数据。
4. 使用检索或网络检索。这些特殊工具能够从文件或数据库中获取相关数据（检索），或从网络中获取相关数据（网络检索）。这有助于让响应以相关上下文数据为依据。