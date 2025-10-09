---
search:
  exclude: true
---
# 上下文管理

"上下文"是一个含义丰富的术语。你可能关心的主要有两类上下文：

1. 代码本地可用的上下文：这是工具函数运行时、在`on_handoff`等回调中、生命周期钩子中等可能需要的数据和依赖项。
2. LLM可用的上下文：这是LLM生成响应时看到的数据。

## 本地上下文

这通过[`RunContextWrapper`][agents.run_context.RunContextWrapper]类和其中的[`context`][agents.run_context.RunContextWrapper.context]属性来表示。其工作方式是：

1. 你创建任何你想要的Python对象。常见模式是使用数据类或Pydantic对象。
2. 你将该对象传递给各种运行方法（例如`Runner.run(..., **context=whatever**)`）。
3. 你的所有工具调用、生命周期钩子等都将传递一个包装器对象`RunContextWrapper[T]`，其中`T`表示你的上下文对象类型，你可以通过`wrapper.context`访问。

**最重要**的事情需要注意：对于给定的智能体运行，每个智能体、工具函数、生命周期等都必须使用相同_类型_的上下文。

你可以将上下文用于：

-   运行的上下文数据（例如用户名/uid或关于用户的其他信息）
-   依赖项（例如日志记录器对象、数据获取器等）
-   辅助函数

!!! danger "注意"

    上下文对象**不会**发送到LLM。它纯粹是一个本地对象，你可以读取、写入和调用其方法。

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

1. 这是上下文对象。我们这里使用了数据类，但你可以使用任何类型。
2. 这是一个工具。你可以看到它接受一个`RunContextWrapper[UserInfo]`。工具实现从上下文中读取。
3. 我们用泛型`UserInfo`标记智能体，这样类型检查器可以捕获错误（例如，如果我们尝试传递一个接受不同上下文类型的工具）。
4. 上下文被传递给`run`函数。
5. 智能体正确调用工具并获取年龄。

## 智能体/LLM上下文

当调用LLM时，它**只能**看到来自对话历史的数据。这意味着如果你想让LLM看到一些新数据，你必须以使其在该历史中可用的方式来实现。有几种方法可以做到这一点：

1. 你可以将其添加到智能体的`instructions`中。这也被称为"系统提示"或"开发者消息"。系统提示可以是静态字符串，也可以是接收上下文并输出字符串的动态函数。这对于始终有用的信息（例如，用户名或当前日期）是常见策略。
2. 在调用`Runner.run`函数时将其添加到`input`中。这类似于`instructions`策略，但允许你在[命令链](https://cdn.openai.com/spec/model-spec-2024-05-08.html#follow-the-chain-of-command)中拥有较低位置的消息。
3. 通过函数工具公开它。这对于_按需_上下文很有用 - LLM决定何时需要某些数据，并可以调用工具来获取该数据。
4. 使用检索或网络搜索。这些是特殊工具，能够从文件或数据库（检索），或从网络（网络搜索）获取相关数据。这对于在相关上下文中"基于"响应很有用。