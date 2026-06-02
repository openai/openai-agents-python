---
search:
  exclude: true
---
# 会话

Agents SDK 提供内置的会话内存，可在多个智能体运行之间自动维护对话历史，无需在回合之间手动处理 `.to_input_list()`。

会话为特定会话存储对话历史，使智能体无需显式的手动内存管理即可保持上下文。这对于构建聊天应用或多轮对话尤为有用，你可以让智能体记住之前的交互。

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

## 工作原理

当启用会话内存时：

1. **每次运行前**：运行器会自动检索该会话的对话历史，并将其预置到输入项之前。
2. **每次运行后**：在运行期间生成的所有新条目（用户输入、助手响应、工具调用等）都会自动存储到会话中。
3. **上下文保留**：使用相同会话的后续运行将包含完整对话历史，使智能体能够保持上下文。

这消除了在运行之间手动调用 `.to_input_list()` 并管理对话状态的需要。

## 内存操作

### 基础操作

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

当你想要撤销或修改对话中的最后一个条目时，`pop_item` 方法特别有用：

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

## 内存选项

### 无内存（默认）

```python
# Default behavior - no session memory
result = await Runner.run(agent, "Hello")
```

### OpenAI Conversations API 内存

使用 [OpenAI Conversations API](https://platform.openai.com/docs/api-reference/conversations/create) 来持久化
[conversation state](https://platform.openai.com/docs/guides/conversation-state?api-mode=responses#using-the-conversations-api)，无需管理你自己的数据库。当你已经依赖由 OpenAI 托管的基础设施来存储对话历史时，这将很有帮助。

```python
from agents import OpenAIConversationsSession

session = OpenAIConversationsSession()

# Optionally resume a previous conversation by passing a conversation ID
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

### 多会话

```python
from agents import Agent, Runner, SQLiteSession

agent = Agent(name="Assistant")

# Different sessions maintain separate conversation histories
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

### 由 SQLAlchemy 驱动的会话

对于更高级的用例，你可以使用由 SQLAlchemy 驱动的会话后端。这样就可以使用任何 SQLAlchemy 支持的数据库（PostgreSQL、MySQL、SQLite 等）来进行会话存储。

**示例 1：使用 `from_url` 搭配内存型 SQLite**

这是最简单的入门方式，适合开发和测试。

```python
import asyncio
from agents import Agent, Runner
from agents.extensions.memory.sqlalchemy_session import SQLAlchemySession

async def main():
    agent = Agent("Assistant")
    session = SQLAlchemySession.from_url(
        "user-123",
        url="sqlite+aiosqlite:///:memory:",
        create_tables=True,  # Auto-create tables for the demo
    )

    result = await Runner.run(agent, "Hello", session=session)

if __name__ == "__main__":
    asyncio.run(main())
```

**示例 2：使用现有的 SQLAlchemy 引擎**

在生产应用中，你很可能已经拥有一个 SQLAlchemy 的 `AsyncEngine` 实例。你可以将其直接传递给会话。

```python
import asyncio
from agents import Agent, Runner
from agents.extensions.memory.sqlalchemy_session import SQLAlchemySession
from sqlalchemy.ext.asyncio import create_async_engine

async def main():
    # In your application, you would use your existing engine
    engine = create_async_engine("sqlite+aiosqlite:///conversations.db")

    agent = Agent("Assistant")
    session = SQLAlchemySession(
        "user-456",
        engine=engine,
        create_tables=True,  # Auto-create tables for the demo
    )

    result = await Runner.run(agent, "Hello", session=session)
    print(result.final_output)

    await engine.dispose()

if __name__ == "__main__":
    asyncio.run(main())
```

### 加密会话

对于需要对静态对话数据进行加密的应用，你可以使用 `EncryptedSession` 来包装任意会话后端，实现透明加密和基于 TTL 的自动过期。这需要 `encrypt` 可选依赖：`pip install openai-agents[encrypt]`。

`EncryptedSession` 使用基于每个会话的密钥派生（HKDF）的 Fernet 加密，并支持旧消息的自动过期。当条目超过 TTL 时，它们在检索期间会被静默跳过。

**示例：为 SQLAlchemy 会话数据加密**

```python
import asyncio
from agents import Agent, Runner
from agents.extensions.memory import EncryptedSession, SQLAlchemySession

async def main():
    # Create underlying session (works with any SessionABC implementation)
    underlying_session = SQLAlchemySession.from_url(
        session_id="user-123",
        url="postgresql+asyncpg://app:secret@db.example.com/agents",
        create_tables=True,
    )

    # Wrap with encryption and TTL-based expiration
    session = EncryptedSession(
        session_id="user-123",
        underlying_session=underlying_session,
        encryption_key="your-encryption-key",  # Use a secure key from your secrets management
        ttl=600,  # 10 minutes - items older than this are silently skipped
    )

    agent = Agent("Assistant")
    result = await Runner.run(agent, "Hello", session=session)
    print(result.final_output)

if __name__ == "__main__":
    asyncio.run(main())
```

**关键特性：**

-   **透明加密**：在存储前自动加密所有会话条目，并在检索时解密
-   **按会话派生密钥**：使用会话 ID 作为盐的 HKDF 来派生唯一加密密钥
-   **基于 TTL 的过期**：根据可配置的生存时间（默认：10 分钟）自动使旧消息过期
-   **灵活的密钥输入**：接受 Fernet 密钥或原始字符串作为加密密钥
-   **可包装任意会话**：适用于 SQLite、SQLAlchemy 或自定义会话实现

!!! warning "重要的安全注意事项"

    -   安全存储你的加密密钥（如环境变量、密钥管理服务）
    -   过期令牌根据应用服务的系统时钟被拒绝——请确保所有服务均通过 NTP 同步时间，以避免因时钟漂移导致的误拒
    -   底层会话仍存储加密数据，因此你依然可以掌控你的数据库基础设施


## 自定义内存实现

你可以通过创建遵循 [`Session`][agents.memory.session.Session] 协议的类来实现你自己的会话内存：

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

## 会话管理

### 会话 ID 命名

使用有意义的会话 ID 来帮助组织对话：

-   基于用户：`"user_12345"`
-   基于线程：`"thread_abc123"`
-   基于上下文：`"support_ticket_456"`

### 内存持久化

-   临时会话使用内存型 SQLite（`SQLiteSession("session_id")`）
-   持久化会话使用基于文件的 SQLite（`SQLiteSession("session_id", "path/to/db.sqlite")`）
-   生产系统且已有数据库时，使用由 SQLAlchemy 驱动的会话（`SQLAlchemySession("session_id", engine=engine, create_tables=True)`），支持 SQLAlchemy 支持的数据库
-   当你希望将历史存储在 OpenAI Conversations API 中时，使用 OpenAI 托管的存储（`OpenAIConversationsSession()`）
-   使用加密会话（`EncryptedSession(session_id, underlying_session, encryption_key)`）为任意会话提供透明加密与基于 TTL 的过期
-   针对其他生产系统（Redis、Django 等）考虑实现自定义会话后端，以满足更高级的用例

### 会话管理

```python
# Clear a session when conversation should start fresh
await session.clear_session()

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

以下是展示会话内存实际效果的完整示例：

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

## API 参考

详细的 API 文档请参阅：

-   [`Session`][agents.memory.Session] - 协议接口
-   [`SQLiteSession`][agents.memory.SQLiteSession] - SQLite 实现
-   [`OpenAIConversationsSession`](ref/memory/openai_conversations_session.md) - OpenAI Conversations API 实现
-   [`SQLAlchemySession`][agents.extensions.memory.sqlalchemy_session.SQLAlchemySession] - 由 SQLAlchemy 驱动的实现
-   [`EncryptedSession`][agents.extensions.memory.encrypt_session.EncryptedSession] - 具有 TTL 的加密会话封装器