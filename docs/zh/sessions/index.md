---
search:
  exclude: true
---
# 会话

Agents SDK 提供内置的会话记忆，可在多个智能体运行之间自动维护对话历史，从而无需在多轮之间手动处理 `.to_input_list()`。

会话会为特定会话存储对话历史，使智能体无需显式的手动记忆管理即可保持上下文。这对构建聊天应用或多轮对话尤为有用，因为你希望智能体记住先前的交互。

## 快速开始

```python
from agents import Agent, Runner, SQLiteSession

# Create agent
agent = Agent(
    name="Assistant",
    instructions="Reply very concisely.",
)

# Create a session instance with a session ID
session = SQLiteSession("conversation_123")

# First turn
result = await Runner.run(
    agent,
    "What city is the Golden Gate Bridge in?",
    session=session
)
print(result.final_output)  # "San Francisco"

# Second turn - agent automatically remembers previous context
result = await Runner.run(
    agent,
    "What state is it in?",
    session=session
)
print(result.final_output)  # "California"

# Also works with synchronous runner
result = Runner.run_sync(
    agent,
    "What's the population?",
    session=session
)
print(result.final_output)  # "Approximately 39 million"
```

## 会话核心行为

当启用会话记忆时：

1. **每次运行前**：runner 会自动检索该会话的对话历史，并将其前置到输入项中。
2. **每次运行后**：运行期间生成的所有新项（用户输入、assistant 响应、工具调用等）都会自动存入会话。
3. **上下文保留**：每次使用同一会话进行后续运行时，都会包含完整对话历史，使智能体能够保持上下文。

这消除了在多次运行之间手动调用 `.to_input_list()` 并管理对话状态的需要。

## 控制历史与新输入的合并方式

当你传入一个会话时，runner 通常会按如下方式准备模型输入：

1. 会话历史（从 `session.get_items(...)` 检索）
2. 新一轮输入

使用 [`RunConfig.session_input_callback`][agents.run.RunConfig.session_input_callback] 在调用模型之前自定义该合并步骤。该回调会接收两个列表：

-   `history`：检索到的会话历史（已规范化为 input-item 格式）
-   `new_input`：当前轮次的新输入项

返回应发送给模型的最终输入项列表。

```python
from agents import Agent, RunConfig, Runner, SQLiteSession


def keep_recent_history(history, new_input):
    # Keep only the last 10 history items, then append the new turn.
    return history[-10:] + new_input


agent = Agent(name="Assistant")
session = SQLiteSession("conversation_123")

result = await Runner.run(
    agent,
    "Continue from the latest updates only.",
    session=session,
    run_config=RunConfig(session_input_callback=keep_recent_history),
)
```

当你需要自定义裁剪、重排或选择性纳入历史，而又不想改变会话的存储方式时，可使用此功能。

## 限制检索的历史

使用 [`SessionSettings`][agents.memory.SessionSettings] 控制每次运行前获取的历史量。

-   `SessionSettings(limit=None)`（默认）：检索所有可用的会话项
-   `SessionSettings(limit=N)`：仅检索最近的 `N` 个项

你可以通过 [`RunConfig.session_settings`][agents.run.RunConfig.session_settings] 在每次运行中应用：

```python
from agents import Agent, RunConfig, Runner, SessionSettings, SQLiteSession

agent = Agent(name="Assistant")
session = SQLiteSession("conversation_123")

result = await Runner.run(
    agent,
    "Summarize our recent discussion.",
    session=session,
    run_config=RunConfig(session_settings=SessionSettings(limit=50)),
)
```

如果你的会话实现提供了默认会话设置，`RunConfig.session_settings` 会在该次运行中覆盖任何非 `None` 的值。这对长对话很有用：你可以在不更改会话默认行为的情况下限制检索大小。

## 记忆操作

### 基本操作

会话支持多种用于管理对话历史的操作：

```python
from agents import SQLiteSession

session = SQLiteSession("user_123", "conversations.db")

# Get all items in a session
items = await session.get_items()

# Add new items to a session
new_items = [
    {"role": "user", "content": "Hello"},
    {"role": "assistant", "content": "Hi there!"}
]
await session.add_items(new_items)

# Remove and return the most recent item
last_item = await session.pop_item()
print(last_item)  # {"role": "assistant", "content": "Hi there!"}

# Clear all items from a session
await session.clear_session()
```

### 使用 pop_item 进行更正

当你想撤销或修改对话中的最后一项时，`pop_item` 方法特别有用：

```python
from agents import Agent, Runner, SQLiteSession

agent = Agent(name="Assistant")
session = SQLiteSession("correction_example")

# Initial conversation
result = await Runner.run(
    agent,
    "What's 2 + 2?",
    session=session
)
print(f"Agent: {result.final_output}")

# User wants to correct their question
assistant_item = await session.pop_item()  # Remove agent's response
user_item = await session.pop_item()  # Remove user's question

# Ask a corrected question
result = await Runner.run(
    agent,
    "What's 2 + 3?",
    session=session
)
print(f"Agent: {result.final_output}")
```

## 内置会话实现

SDK 为不同用例提供了多种会话实现：

### 选择内置会话实现

在阅读下面的详细示例前，使用此表选择一个起点。

| 会话类型 | 最适合 | 备注 |
| --- | --- | --- |
| `SQLiteSession` | 本地开发与简单应用 | 内置、轻量、基于文件或内存 |
| `AsyncSQLiteSession` | 结合 `aiosqlite` 的异步 SQLite | 扩展后端，支持异步驱动 |
| `RedisSession` | 跨 worker/服务共享记忆 | 适用于低延迟分布式部署 |
| `SQLAlchemySession` | 使用现有数据库的生产应用 | 适用于 SQLAlchemy 支持的数据库 |
| `OpenAIConversationsSession` | OpenAI 中的服务端托管存储 | 基于 OpenAI Conversations API 的历史 |
| `OpenAIResponsesCompactionSession` | 自动压缩的长对话 | 另一个会话后端的封装器 |
| `AdvancedSQLiteSession` | SQLite + 分支/分析 | 更重的功能集；见专页 |
| `EncryptedSession` | 在另一个会话之上提供加密 + TTL | 封装器；请先选择底层后端 |

部分实现有专门页面提供更多细节；这些会在各自小节中以内联方式链接。

### OpenAI Conversations API 会话

通过 `OpenAIConversationsSession` 使用 [OpenAI's Conversations API](https://platform.openai.com/docs/api-reference/conversations)。

```python
from agents import Agent, Runner, OpenAIConversationsSession

# Create agent
agent = Agent(
    name="Assistant",
    instructions="Reply very concisely.",
)

# Create a new conversation
session = OpenAIConversationsSession()

# Optionally resume a previous conversation by passing a conversation ID
# session = OpenAIConversationsSession(conversation_id="conv_123")

# Start conversation
result = await Runner.run(
    agent,
    "What city is the Golden Gate Bridge in?",
    session=session
)
print(result.final_output)  # "San Francisco"

# Continue the conversation
result = await Runner.run(
    agent,
    "What state is it in?",
    session=session
)
print(result.final_output)  # "California"
```

### OpenAI Responses 压缩会话

使用 `OpenAIResponsesCompactionSession` 通过 Responses API（`responses.compact`）压缩会话历史。它封装一个底层会话，并可基于 `should_trigger_compaction` 在每轮后自动压缩。

#### 典型用法（自动压缩）

```python
from agents import Agent, Runner, SQLiteSession
from agents.memory import OpenAIResponsesCompactionSession

underlying = SQLiteSession("conversation_123")
session = OpenAIResponsesCompactionSession(
    session_id="conversation_123",
    underlying_session=underlying,
)

agent = Agent(name="Assistant")
result = await Runner.run(agent, "Hello", session=session)
print(result.final_output)
```

默认情况下，一旦达到候选阈值，压缩会在每轮后执行。

#### 自动压缩可能阻塞流式传输

压缩会清空并重写会话历史，因此 SDK 会等待压缩完成后才认为运行结束。在流式模式下，这意味着如果压缩很重，`run.stream_events()` 可能会在最后一个输出 token 之后仍保持打开几秒钟。

如果你希望低延迟流式传输或快速轮转，请禁用自动压缩，并在轮次之间（或空闲时间）自行调用 `run_compaction()`。你可以根据自己的标准决定何时强制压缩。

```python
from agents import Agent, Runner, SQLiteSession
from agents.memory import OpenAIResponsesCompactionSession

underlying = SQLiteSession("conversation_123")
session = OpenAIResponsesCompactionSession(
    session_id="conversation_123",
    underlying_session=underlying,
    # Disable triggering the auto compaction
    should_trigger_compaction=lambda _: False,
)

agent = Agent(name="Assistant")
result = await Runner.run(agent, "Hello", session=session)

# Decide when to compact (e.g., on idle, every N turns, or size thresholds).
await session.run_compaction({"force": True})
```

### SQLite 会话

默认的轻量级 SQLite 会话实现：

```python
from agents import SQLiteSession

# In-memory database (lost when process ends)
session = SQLiteSession("user_123")

# Persistent file-based database
session = SQLiteSession("user_123", "conversations.db")

# Use the session
result = await Runner.run(
    agent,
    "Hello",
    session=session
)
```

### 异步 SQLite 会话

当你希望使用 `aiosqlite` 提供的 SQLite 持久化时，使用 `AsyncSQLiteSession`。

```bash
pip install aiosqlite
```

```python
from agents import Agent, Runner
from agents.extensions.memory import AsyncSQLiteSession

agent = Agent(name="Assistant")
session = AsyncSQLiteSession("user_123", db_path="conversations.db")
result = await Runner.run(agent, "Hello", session=session)
```

### Redis 会话

使用 `RedisSession` 在多个 worker 或服务之间共享会话记忆。

```bash
pip install openai-agents[redis]
```

```python
from agents import Agent, Runner
from agents.extensions.memory import RedisSession

agent = Agent(name="Assistant")
session = RedisSession.from_url(
    "user_123",
    url="redis://localhost:6379/0",
)
result = await Runner.run(agent, "Hello", session=session)
```

### SQLAlchemy 会话

使用任意 SQLAlchemy 支持的数据库的生产就绪会话：

```python
from agents.extensions.memory import SQLAlchemySession

# Using database URL
session = SQLAlchemySession.from_url(
    "user_123",
    url="postgresql+asyncpg://user:pass@localhost/db",
    create_tables=True
)

# Using existing engine
from sqlalchemy.ext.asyncio import create_async_engine
engine = create_async_engine("postgresql+asyncpg://user:pass@localhost/db")
session = SQLAlchemySession("user_123", engine=engine, create_tables=True)
```

详见 [SQLAlchemy Sessions](sqlalchemy_session.md) 文档。



### Advanced SQLite 会话

增强型 SQLite 会话，支持对话分支、用量分析和结构化查询：

```python
from agents.extensions.memory import AdvancedSQLiteSession

# Create with advanced features
session = AdvancedSQLiteSession(
    session_id="user_123",
    db_path="conversations.db",
    create_tables=True
)

# Automatic usage tracking
result = await Runner.run(agent, "Hello", session=session)
await session.store_run_usage(result)  # Track token usage

# Conversation branching
await session.create_branch_from_turn(2)  # Branch from turn 2
```

详见 [Advanced SQLite Sessions](advanced_sqlite_session.md) 文档。

### 加密会话

适用于任意会话实现的透明加密封装器：

```python
from agents.extensions.memory import EncryptedSession, SQLAlchemySession

# Create underlying session
underlying_session = SQLAlchemySession.from_url(
    "user_123",
    url="sqlite+aiosqlite:///conversations.db",
    create_tables=True
)

# Wrap with encryption and TTL
session = EncryptedSession(
    session_id="user_123",
    underlying_session=underlying_session,
    encryption_key="your-secret-key",
    ttl=600  # 10 minutes
)

result = await Runner.run(agent, "Hello", session=session)
```

详见 [Encrypted Sessions](encrypted_session.md) 文档。

### 其他会话类型

还有一些内置选项。请参考 `examples/memory/` 以及 `extensions/memory/` 下的源代码。

## 运行模式

### 会话 ID 命名

使用有意义的会话 ID 来帮助你组织对话：

-   基于用户：`"user_12345"`
-   基于线程：`"thread_abc123"`
-   基于上下文：`"support_ticket_456"`

### 记忆持久化

-   临时对话使用内存 SQLite（`SQLiteSession("session_id")`）
-   持久对话使用基于文件的 SQLite（`SQLiteSession("session_id", "path/to/db.sqlite")`）
-   需要基于 `aiosqlite` 的实现时使用异步 SQLite（`AsyncSQLiteSession("session_id", db_path="...")`）
-   共享、低延迟会话记忆使用 Redis 后端会话（`RedisSession.from_url("session_id", url="redis://...")`）
-   生产系统且已使用 SQLAlchemy 支持的现有数据库时，使用 SQLAlchemy 驱动会话（`SQLAlchemySession("session_id", engine=engine, create_tables=True)`）
-   生产级云原生部署使用 Dapr 状态存储会话（`DaprSession.from_address("session_id", state_store_name="statestore", dapr_address="localhost:50001")`），支持 30+ 数据库后端，并内置遥测、追踪与数据隔离
-   希望将历史存储在 OpenAI Conversations API 中时使用由 OpenAI 托管的存储（`OpenAIConversationsSession()`）
-   使用加密会话（`EncryptedSession(session_id, underlying_session, encryption_key)`）为任意会话提供透明加密与基于 TTL 的过期
-   对于更高级用例，可考虑为其他生产系统（例如 Django）实现自定义会话后端

### 多个会话

```python
from agents import Agent, Runner, SQLiteSession

agent = Agent(name="Assistant")

# Different sessions maintain separate conversation histories
session_1 = SQLiteSession("user_123", "conversations.db")
session_2 = SQLiteSession("user_456", "conversations.db")

result1 = await Runner.run(
    agent,
    "Help me with my account",
    session=session_1
)
result2 = await Runner.run(
    agent,
    "What are my charges?",
    session=session_2
)
```

### 会话共享

```python
# Different agents can share the same session
support_agent = Agent(name="Support")
billing_agent = Agent(name="Billing")
session = SQLiteSession("user_123")

# Both agents will see the same conversation history
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

下面是一个展示会话记忆实际效果的完整示例：

```python
import asyncio
from agents import Agent, Runner, SQLiteSession


async def main():
    # Create an agent
    agent = Agent(
        name="Assistant",
        instructions="Reply very concisely.",
    )

    # Create a session instance that will persist across runs
    session = SQLiteSession("conversation_123", "conversation_history.db")

    print("=== Sessions Example ===")
    print("The agent will remember previous messages automatically.\n")

    # First turn
    print("First turn:")
    print("User: What city is the Golden Gate Bridge in?")
    result = await Runner.run(
        agent,
        "What city is the Golden Gate Bridge in?",
        session=session
    )
    print(f"Assistant: {result.final_output}")
    print()

    # Second turn - the agent will remember the previous conversation
    print("Second turn:")
    print("User: What state is it in?")
    result = await Runner.run(
        agent,
        "What state is it in?",
        session=session
    )
    print(f"Assistant: {result.final_output}")
    print()

    # Third turn - continuing the conversation
    print("Third turn:")
    print("User: What's the population of that state?")
    result = await Runner.run(
        agent,
        "What's the population of that state?",
        session=session
    )
    print(f"Assistant: {result.final_output}")
    print()

    print("=== Conversation Complete ===")
    print("Notice how the agent remembered the context from previous turns!")
    print("Sessions automatically handles conversation history.")


if __name__ == "__main__":
    asyncio.run(main())
```

## 自定义会话实现

你可以通过创建一个遵循 [`Session`][agents.memory.session.Session] 协议的类来实现自己的会话记忆：

```python
from agents.memory.session import SessionABC
from agents.items import TResponseInputItem
from typing import List

class MyCustomSession(SessionABC):
    """Custom session implementation following the Session protocol."""

    def __init__(self, session_id: str):
        self.session_id = session_id
        # Your initialization here

    async def get_items(self, limit: int | None = None) -> List[TResponseInputItem]:
        """Retrieve conversation history for this session."""
        # Your implementation here
        pass

    async def add_items(self, items: List[TResponseInputItem]) -> None:
        """Store new items for this session."""
        # Your implementation here
        pass

    async def pop_item(self) -> TResponseInputItem | None:
        """Remove and return the most recent item from this session."""
        # Your implementation here
        pass

    async def clear_session(self) -> None:
        """Clear all items for this session."""
        # Your implementation here
        pass

# Use your custom session
agent = Agent(name="Assistant")
result = await Runner.run(
    agent,
    "Hello",
    session=MyCustomSession("my_session")
)
```

## 社区会话实现

社区开发了额外的会话实现：

| Package | 描述 |
|---------|-------------|
| [openai-django-sessions](https://pypi.org/project/openai-django-sessions/) | 基于 Django ORM 的会话，适用于任何 Django 支持的数据库（PostgreSQL、MySQL、SQLite 等） |

如果你实现了一个会话实现，欢迎提交文档 PR 将其添加到此处！

## API 参考

如需详细 API 文档，请参见：

-   [`Session`][agents.memory.session.Session] - 协议接口
-   [`OpenAIConversationsSession`][agents.memory.OpenAIConversationsSession] - OpenAI Conversations API 实现
-   [`OpenAIResponsesCompactionSession`][agents.memory.openai_responses_compaction_session.OpenAIResponsesCompactionSession] - Responses API 压缩封装器
-   [`SQLiteSession`][agents.memory.sqlite_session.SQLiteSession] - 基本 SQLite 实现
-   [`AsyncSQLiteSession`][agents.extensions.memory.async_sqlite_session.AsyncSQLiteSession] - 基于 `aiosqlite` 的异步 SQLite 实现
-   [`RedisSession`][agents.extensions.memory.redis_session.RedisSession] - 基于 Redis 的会话实现
-   [`SQLAlchemySession`][agents.extensions.memory.sqlalchemy_session.SQLAlchemySession] - 由 SQLAlchemy 驱动的实现
-   [`DaprSession`][agents.extensions.memory.dapr_session.DaprSession] - Dapr 状态存储实现
-   [`AdvancedSQLiteSession`][agents.extensions.memory.advanced_sqlite_session.AdvancedSQLiteSession] - 带分支与分析的增强 SQLite
-   [`EncryptedSession`][agents.extensions.memory.encrypt_session.EncryptedSession] - 适用于任何会话的加密封装器