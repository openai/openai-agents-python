---
search:
  exclude: true
---
# 会话

Agents SDK 提供内置的会话记忆功能，可自动在多次智能体运行之间维护对话历史，无需在轮次之间手动处理 `.to_input_list()`。

会话存储特定会话的对话历史，使智能体能够在无需显式手动内存管理的情况下保持上下文。这对于构建聊天应用程序或智能体需要记住先前交互的多轮对话特别有用。

## 快速开始

```python
from agents import Agent, Runner, SQLiteSession

# 创建智能体
agent = Agent(
    name="Assistant",
    instructions="Reply very concisely.",
)

# 使用会话ID创建会话实例
session = SQLiteSession("conversation_123")

# 第一轮
result = await Runner.run(
    agent,
    "What city is the Golden Gate Bridge in?",
    session=session
)
print(result.final_output)  # "San Francisco"

# 第二轮 - 智能体自动记住之前的上下文
result = await Runner.run(
    agent,
    "What state is it in?",
    session=session
)
print(result.final_output)  # "California"

# 同步运行器也同样适用
result = Runner.run_sync(
    agent,
    "What's the population?",
    session=session
)
print(result.final_output)  # "Approximately 39 million"
```

## 工作原理

启用会话记忆时：

1. **每次运行前**：运行器自动检索会话的对话历史，并将其前置到输入项中。
2. **每次运行后**：运行期间生成的所有新项（用户输入、助手响应、工具调用等）都会自动存储在会话中。
3. **上下文保持**：同一会话的每次后续运行都包含完整的对话历史，使智能体能够保持上下文。

这消除了手动调用 `.to_input_list()` 和管理运行之间对话状态的需要。

## 内存操作

### 基本操作

会话支持用于管理对话历史的多种操作：

```python
from agents import SQLiteSession

session = SQLiteSession("user_123", "conversations.db")

# 获取会话中的所有项
items = await session.get_items()

# 向会话添加新项
new_items = [
    {"role": "user", "content": "Hello"},
    {"role": "assistant", "content": "Hi there!"}
]
await session.add_items(new_items)

# 移除并返回最近的一项
last_item = await session.pop_item()
print(last_item)  # {"role": "assistant", "content": "Hi there!"}

# 清除会话中的所有项
await session.clear_session()
```

### 使用 pop_item 进行更正

`pop_item` 方法在你想要撤消或修改对话中的最后一项时特别有用：

```python
from agents import Agent, Runner, SQLiteSession

agent = Agent(name="Assistant")
session = SQLiteSession("correction_example")

# 初始对话
result = await Runner.run(
    agent,
    "What's 2 + 2?",
    session=session
)
print(f"Agent: {result.final_output}")

# 用户想要更正他们的问题
assistant_item = await session.pop_item()  # 移除智能体的响应
user_item = await session.pop_item()  # 移除用户的问题

# 询问更正后的问题
result = await Runner.run(
    agent,
    "What's 2 + 3?",
    session=session
)
print(f"Agent: {result.final_output}")
```

## 内存选项

### 无内存（默认）

```python
# 默认行为 - 无会话内存
result = await Runner.run(agent, "Hello")
```

### OpenAI 对话 API 内存

使用 [OpenAI 对话 API](https://platform.openai.com/docs/api-reference/conversations/create) 来持久化
[对话状态](https://platform.openai.com/docs/guides/conversation-state?api-mode=responses#using-the-conversations-api)，无需管理自己的数据库。当你已经依赖 OpenAI 托管的基础设施来存储对话历史时，这很有帮助。

```python
from agents import OpenAIConversationsSession

session = OpenAIConversationsSession()

# 可选地通过传递对话 ID 来恢复之前的对话
# session = OpenAIConversationsSession(conversation_id="conv_123")

result = await Runner.run(
    agent,
    "Hello",
    session=session,
)
```

### SQLite 内存

```python
from agents import SQLiteSession

# 内存数据库（进程结束时丢失）
session = SQLiteSession("user_123")

# 基于文件的持久数据库
session = SQLiteSession("user_123", "conversations.db")

# 使用会话
result = await Runner.run(
    agent,
    "Hello",
    session=session
)
```

### 多个会话

```python
from agents import Agent, Runner, SQLiteSession

agent = Agent(name="Assistant")

# 不同的会话维护单独的对话历史
session_1 = SQLiteSession("user_123", "conversations.db")
session_2 = SQLiteSession("user_456", "conversations.db")

result1 = await Runner.run(
    agent,
    "Hello",
    session=session_1
)
result2 = await Runner.run(
    agent,
    "Hello",
    session=session_2
)
```

### SQLAlchemy 驱动的会话

对于更高级的用例，你可以使用 SQLAlchemy 驱动的会话后端。这允许你使用 SQLAlchemy 支持的任何数据库（PostgreSQL、MySQL、SQLite 等）进行会话存储。

**示例 1：使用 `from_url` 和内存 SQLite**

这是最简单的入门方式，非常适合开发和测试。

```python
import asyncio
from agents import Agent, Runner
from agents.extensions.memory.sqlalchemy_session import SQLAlchemySession

async def main():
    agent = Agent("Assistant")
    session = SQLAlchemySession.from_url(
        "user-123",
        url="sqlite+aiosqlite:///:memory:",
        create_tables=True,  # 为演示自动创建表
    )

    result = await Runner.run(agent, "Hello", session=session)

if __name__ == "__main__":
    asyncio.run(main())
```

**示例 2：使用现有的 SQLAlchemy 引擎**

在生产应用中，你可能已经有一个 SQLAlchemy `AsyncEngine` 实例。你可以直接将其传递给会话。

```python
import asyncio
from agents import Agent, Runner
from agents.extensions.memory.sqlalchemy_session import SQLAlchemySession
from sqlalchemy.ext.asyncio import create_async_engine

async def main():
    # 在你的应用中，你会使用现有的引擎
    engine = create_async_engine("sqlite+aiosqlite:///conversations.db")

    agent = Agent("Assistant")
    session = SQLAlchemySession(
        "user-456",
        engine=engine,
        create_tables=True,  # 为演示自动创建表
    )

    result = await Runner.run(agent, "Hello", session=session)
    print(result.final_output)

    await engine.dispose()

if __name__ == "__main__":
    asyncio.run(main())
```


## 自定义内存实现

你可以通过创建遵循 [`Session`][agents.memory.session.Session] 协议的类来实现自己的会话内存：

```python
from agents.memory.session import SessionABC
from agents.items import TResponseInputItem
from typing import List

class MyCustomSession(SessionABC):
    """遵循会话协议的自定义会话实现。"""

    def __init__(self, session_id: str):
        self.session_id = session_id
        # 你的初始化代码在这里

    async def get_items(self, limit: int | None = None) -> List[TResponseInputItem]:
        """检索此会话的对话历史。"""
        # 你的实现代码在这里
        pass

    async def add_items(self, items: List[TResponseInputItem]) -> None:
        """为此会话存储新项。"""
        # 你的实现代码在这里
        pass

    async def pop_item(self) -> TResponseInputItem | None:
        """从此会话中移除并返回最近的一项。"""
        # 你的实现代码在这里
        pass

    async def clear_session(self) -> None:
        """清除此会话的所有项。"""
        # 你的实现代码在这里
        pass

# 使用你的自定义会话
agent = Agent(name="Assistant")
result = await Runner.run(
    agent,
    "Hello",
    session=MyCustomSession("my_session")
)
```

## 会话管理

### 会话ID命名

使用有意义的会话ID来帮助你组织对话：

-   基于用户：`"user_12345"`
-   基于线程：`"thread_abc123"`
-   基于上下文：`"support_ticket_456"`

### 内存持久性

-   使用内存 SQLite (`SQLiteSession("session_id")`) 进行临时对话
-   使用基于文件的 SQLite (`SQLiteSession("session_id", "path/to/db.sqlite")`) 进行持久对话
-   使用 SQLAlchemy 驱动的会话 (`SQLAlchemySession("session_id", engine=engine, create_tables=True)`) 用于具有 SQLAlchemy 支持的现有数据库的生产系统
-   使用 OpenAI 托管存储 (`OpenAIConversationsSession()`) 当你希望将历史记录存储在 OpenAI 对话 API 中时
-   考虑为其他生产系统（Redis、Django 等）实现自定义会话后端，以应对更高级的用例

### 会话管理

```python
# 当对话应该重新开始时清除会话
await session.clear_session()

# 不同的智能体可以共享相同的会话
support_agent = Agent(name="Support")
billing_agent = Agent(name="Billing")
session = SQLiteSession("user_123")

# 两个智能体都将看到相同的对话历史
result1 = await Runner.run(
    support_agent,
    "Help me with my account",
    session=session
)
result2 = await Runner.run(
    billing_agent,
    "What are my charges?",
    session=session
)
```

## 完整示例

以下是展示会话内存实际应用的完整示例：

```python
import asyncio
from agents import Agent, Runner, SQLiteSession


async def main():
    # 创建智能体
    agent = Agent(
        name="Assistant",
        instructions="Reply very concisely.",
    )

    # 创建一个将在运行之间持久的会话实例
    session = SQLiteSession("conversation_123", "conversation_history.db")

    print("=== 会话示例 ===")
    print("智能体将自动记住之前的消息。\n")

    # 第一轮
    print("第一轮:")
    print("User: What city is the Golden Gate Bridge in?")
    result = await Runner.run(
        agent,
        "What city is the Golden Gate Bridge in?",
        session=session
    )
    print(f"Assistant: {result.final_output}")
    print()

    # 第二轮 - 智能体将记住之前的对话
    print("第二轮:")
    print("User: What state is it in?")
    result = await Runner.run(
        agent,
        "What state is it in?",
        session=session
    )
    print(f"Assistant: {result.final_output}")
    print()

    # 第三轮 - 继续对话
    print("第三轮:")
    print("User: What's the population of that state?")
    result = await Runner.run(
        agent,
        "What's the population of that state?",
        session=session
    )
    print(f"Assistant: {result.final_output}")
    print()

    print("=== 对话完成 ===")
    print("注意智能体如何从前几轮记住上下文！")
    print("会话自动处理对话历史。")


if __name__ == "__main__":
    asyncio.run(main())
```

## API 参考

有关详细的 API 文档，请参见：

-   [`Session`][agents.memory.Session] - 协议接口
-   [`SQLiteSession`][agents.memory.SQLiteSession] - SQLite 实现
-   [`OpenAIConversationsSession`](ref/memory/openai_conversations_session.md) - OpenAI 对话 API 实现
-   [`SQLAlchemySession`][agents.extensions.memory.sqlalchemy_session.SQLAlchemySession] - SQLAlchemy 驱动的实现