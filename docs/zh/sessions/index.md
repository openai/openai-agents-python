---
search:
  exclude: true
---
# 会话

Agents SDK 提供内置会话记忆，可在多次智能体运行之间自动维护对话历史，无需在轮次之间手动处理 `.to_input_list()`。

会话会存储特定会话的对话历史，使智能体无需显式手动管理记忆即可保持上下文。这对于构建聊天应用或多轮对话尤其有用，在这些场景中你希望智能体记住之前的交互。

当你希望 SDK 为你管理客户端侧记忆时，请使用会话。会话不能在同一次运行中与 `conversation_id`、`previous_response_id` 或 `auto_previous_response_id` 组合使用。如果你想使用 OpenAI 服务端托管的续接，请选择其中一种机制，而不是在其上叠加会话。

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

## 使用相同会话恢复中断的运行

如果某次运行因等待批准而暂停，请使用相同的会话实例（或指向同一底层存储的另一个会话实例）恢复它，以便恢复后的轮次继续使用同一份已存储的对话历史。

```python
result = await Runner.run(agent, "Delete temporary files that are no longer needed.", session=session)

if result.interruptions:
    state = result.to_state()
    for interruption in result.interruptions:
        state.approve(interruption)
    result = await Runner.run(agent, state, session=session)
```

## 核心会话行为

启用会话记忆后：

1. **每次运行前**：运行器会自动检索该会话的对话历史，并将其前置到输入项中。
2. **每次运行后**：运行期间生成的所有新项（用户输入、助手回复、工具调用等）都会自动存储到会话中。
3. **上下文保留**：使用同一会话的每次后续运行都会包含完整的对话历史，使智能体能够保持上下文。

这消除了在运行之间手动调用 `.to_input_list()` 和管理对话状态的需要。

## 历史记录与新输入的合并控制

当你传入会话时，运行器通常会按如下方式准备模型输入：

1. 会话历史（从 `session.get_items(...)` 检索）
2. 新轮次输入

使用 [`RunConfig.session_input_callback`][agents.run.RunConfig.session_input_callback] 在模型调用前自定义该合并步骤。回调会接收两个列表：

-   `history`：检索到的会话历史（已规范化为输入项格式）
-   `new_input`：当前轮次的新输入项

返回应发送给模型的最终输入项列表。

回调接收的是两个列表的副本，因此你可以安全地修改它们。返回的列表会控制该轮次的模型输入，但 SDK 仍然只持久化属于新轮次的项。因此，重新排序或过滤旧历史不会导致旧会话项被作为新的输入再次保存。

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

当你需要自定义裁剪、重新排序或选择性纳入历史记录，同时不改变会话存储项的方式时，请使用此功能。如果你需要在模型调用前立即进行更靠后的最终处理，请使用[运行智能体指南](../running_agents.md)中的 [`call_model_input_filter`][agents.run.RunConfig.call_model_input_filter]。

## 检索历史记录的限制

使用 [`SessionSettings`][agents.memory.SessionSettings] 控制每次运行前获取多少历史记录。

-   `SessionSettings(limit=None)`（默认）：检索所有可用的会话项
-   `SessionSettings(limit=N)`：仅检索最近的 `N` 个项

你可以通过 [`RunConfig.session_settings`][agents.run.RunConfig.session_settings] 按运行应用此设置：

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

如果你的会话实现暴露默认会话设置，`RunConfig.session_settings` 会覆盖该次运行中任何非 `None` 的值。这对于长对话很有用，你可以限制检索大小，而无需更改会话的默认行为。

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

### 使用 pop_item 进行修正

当你想撤销或修改对话中的最后一项时，`pop_item` 方法尤其有用：

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

### 内置会话实现的选择

在阅读下面的详细示例之前，请使用此表选择一个起点。

| 会话类型 | 最适合 | 备注 |
| --- | --- | --- |
| `SQLiteSession` | 本地开发和简单应用 | 内置、轻量、基于文件或内存 |
| `AsyncSQLiteSession` | 使用 `aiosqlite` 的异步 SQLite | 具有异步驱动支持的扩展后端 |
| `RedisSession` | 跨 worker/服务的共享记忆 | 适用于低延迟分布式部署 |
| `SQLAlchemySession` | 使用现有数据库的生产应用 | 适用于 SQLAlchemy 支持的数据库 |
| `MongoDBSession` | 已使用 MongoDB 或需要多进程存储的应用 | 异步 pymongo；用于排序的原子序列计数器 |
| `DaprSession` | 使用 Dapr sidecar 的云原生部署 | 支持多个状态存储以及 TTL 和一致性控制 |
| `OpenAIConversationsSession` | OpenAI 中的服务端托管存储 | 基于 OpenAI Conversations API 的历史记录 |
| `OpenAIResponsesCompactionSession` | 带自动压缩的长对话 | 围绕另一个会话后端的包装器 |
| `AdvancedSQLiteSession` | SQLite 加分支/分析 | 功能更丰富；请参阅专用页面 |
| `EncryptedSession` | 基于另一个会话的加密 + TTL | 包装器；请先选择底层后端 |

某些实现有包含更多详细信息的专用页面；这些页面会在各自小节中以内联链接给出。

如果你正在为 ChatKit 实现 Python 服务，请使用 `chatkit.store.Store` 实现来持久化 ChatKit 的线程和项。诸如 `SQLAlchemySession` 的 Agents SDK 会话用于管理 SDK 侧的对话历史，但它们不能直接替代 ChatKit 的存储。请参阅 [`chatkit-python` 关于实现 ChatKit 数据存储的指南](https://github.com/openai/chatkit-python/blob/main/docs/guides/respond-to-user-message.md#implement-your-chatkit-data-store)。

### OpenAI Conversations API 会话

通过 `OpenAIConversationsSession` 使用 [OpenAI 的 Conversations API](https://platform.openai.com/docs/api-reference/conversations)。

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

使用 `OpenAIResponsesCompactionSession` 通过 Responses API（`responses.compact`）压缩已存储的对话历史。它包装底层会话，并可根据 `should_trigger_compaction` 在每个轮次后自动压缩。不要用它包装 `OpenAIConversationsSession`；这两个功能以不同方式管理历史记录。

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

默认情况下，一旦达到候选阈值，压缩会在每个轮次后运行。

当你已经使用 Responses API 响应 ID 串联轮次时，`compaction_mode="previous_response_id"` 效果最佳。`compaction_mode="input"` 则会基于当前会话项重新构建压缩请求，这在响应链不可用，或你希望会话内容作为事实来源时很有用。默认的 `"auto"` 会选择最安全的可用选项。

如果你的智能体使用 `ModelSettings(store=False)` 运行，Responses API 不会保留最后一次响应用于后续查找。在这种无状态设置中，默认的 `"auto"` 模式会回退为基于输入的压缩，而不是依赖 `previous_response_id`。完整示例请参阅 [`examples/memory/compaction_session_stateless_example.py`](https://github.com/openai/openai-agents-python/tree/main/examples/memory/compaction_session_stateless_example.py)。

#### 自动压缩可能阻塞流式传输

压缩会清除并重写会话历史，因此 SDK 会等待压缩完成后才认为运行结束。在流式传输模式下，这意味着如果压缩较重，`run.stream_events()` 可能会在最后一个输出 token 之后继续保持打开数秒。

如果你需要低延迟流式传输或快速轮次切换，请禁用自动压缩，并在轮次之间（或空闲时间）自行调用 `run_compaction()`。你可以根据自己的标准决定何时强制压缩。

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

使用 SQLite 的默认轻量级会话实现：

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

当你希望使用由 `aiosqlite` 支持的 SQLite 持久化时，请使用 `AsyncSQLiteSession`。

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

使用任何 SQLAlchemy 支持的数据库实现生产就绪的 Agents SDK 会话持久化：

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

详细文档请参阅 [SQLAlchemy 会话](sqlalchemy_session.md)。

### Dapr 会话

当你已经运行 Dapr sidecar，或希望会话存储能够在不同状态存储后端之间迁移且无需更改智能体代码时，请使用 `DaprSession`。

```bash
pip install openai-agents[dapr]
```

```python
from agents import Agent, Runner
from agents.extensions.memory import DaprSession

agent = Agent(name="Assistant")

async with DaprSession.from_address(
    "user_123",
    state_store_name="statestore",
    dapr_address="localhost:50001",
) as session:
    result = await Runner.run(agent, "Hello", session=session)
    print(result.final_output)
```

备注：

-   `from_address(...)` 会为你创建并拥有 Dapr 客户端。如果你的应用已经管理了一个客户端，请直接使用 `DaprSession(...)` 并传入 `dapr_client=...`。
-   传入 `ttl=...`，可在底层状态存储支持 TTL 时让其自动过期旧会话数据。
-   当你需要更强的写后读保证时，请传入 `consistency=DAPR_CONSISTENCY_STRONG`。
-   Dapr Python SDK 还会检查 HTTP sidecar 端点。在本地开发中，启动 Dapr 时除 `dapr_address` 使用的 gRPC 端口外，还应包含 `--dapr-http-port 3500`。
-   完整设置演练（包括本地组件和故障排查）请参阅 [`examples/memory/dapr_session_example.py`](https://github.com/openai/openai-agents-python/tree/main/examples/memory/dapr_session_example.py)。


### MongoDB 会话

对于已经使用 MongoDB，或需要可水平扩展、多进程会话存储的应用，请使用 `MongoDBSession`。

```bash
pip install openai-agents[mongodb]
```

```python
from agents import Agent, Runner
from agents.extensions.memory import MongoDBSession

agent = Agent(name="Assistant")

# Create from URI — owns the client and closes it when session.close() is called
session = MongoDBSession.from_uri(
    "user-123",
    uri="mongodb://localhost:27017",
    database="agents",
)
result = await Runner.run(agent, "Hello", session=session)
print(result.final_output)
await session.close()
```

备注：

-   `from_uri(...)` 会创建并拥有 `AsyncMongoClient`，并在 `session.close()` 时关闭它。如果你的应用已经管理客户端，请直接使用 `MongoDBSession(...)` 并传入 `client=...`；在这种情况下，`session.close()` 是空操作，生命周期由调用方管理。
-   通过向 `from_uri(...)` 传入 `mongodb+srv://user:password@cluster.example.mongodb.net` URI（无需其他更改）连接到 [MongoDB Atlas](https://www.mongodb.com/products/platform)。
-   使用两个集合，并且这两个名称都可以通过 `sessions_collection=`（默认 `agent_sessions`）和 `messages_collection=`（默认 `agent_messages`）配置。索引会在首次使用时自动创建。每条消息文档都带有一个单调递增的 `seq` 计数器，用于在并发写入者和进程之间保持顺序。
-   在首次运行前，使用 `await session.ping()` 验证连接。

### 高级 SQLite 会话

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

详细文档请参阅[高级 SQLite 会话](advanced_sqlite_session.md)。

### 加密会话

适用于任何会话实现的透明加密包装器：

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

详细文档请参阅[加密会话](encrypted_session.md)。

### 其他会话类型

还有一些其他内置选项。请参考 `examples/memory/` 以及 `extensions/memory/` 下的源代码。

## 运维模式

### 会话 ID 命名

使用有意义的会话 ID，帮助你组织对话：

-   基于用户：`"user_12345"`
-   基于线程：`"thread_abc123"`
-   基于上下文：`"support_ticket_456"`

### 记忆持久化

-   对临时对话使用内存 SQLite（`SQLiteSession("session_id")`）
-   对持久对话使用基于文件的 SQLite（`SQLiteSession("session_id", "path/to/db.sqlite")`）
-   当你需要基于 `aiosqlite` 的实现时，使用异步 SQLite（`AsyncSQLiteSession("session_id", db_path="...")`）
-   使用 Redis 后端会话（`RedisSession.from_url("session_id", url="redis://...")`）实现共享、低延迟的会话记忆
-   对于使用 SQLAlchemy 支持的现有数据库的生产系统，使用 SQLAlchemy 驱动的会话（`SQLAlchemySession("session_id", engine=engine, create_tables=True)`）
-   对于已经使用 MongoDB 或需要多进程、可水平扩展会话存储的应用，使用 MongoDB 会话（`MongoDBSession.from_uri("session_id", uri="mongodb://localhost:27017")`）
-   对于生产级云原生部署，使用 Dapr 状态存储会话（`DaprSession.from_address("session_id", state_store_name="statestore", dapr_address="localhost:50001")`），支持 30+ 数据库后端，并内置遥测、追踪和数据隔离
-   当你倾向于将历史记录存储在 OpenAI Conversations API 中时，使用 OpenAI 托管存储（`OpenAIConversationsSession()`）
-   使用加密会话（`EncryptedSession(session_id, underlying_session, encryption_key)`）为任何会话包装透明加密和基于 TTL 的过期
-   对于更高级的用例，可考虑为其他生产系统（例如 Django）实现自定义会话后端

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

下面是一个展示会话记忆实际工作方式的完整示例：

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

你可以创建一个遵循 [`Session`][agents.memory.session.Session] 协议的类，来实现自己的会话记忆：

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

社区已经开发了额外的会话实现：

| 包 | 描述 |
|---------|-------------|
| [openai-django-sessions](https://pypi.org/project/openai-django-sessions/) | 基于 Django ORM 的会话，适用于任何 Django 支持的数据库（PostgreSQL、MySQL、SQLite 等） |

如果你构建了会话实现，欢迎提交文档 PR 将其添加到这里！

## API 参考

有关详细 API 文档，请参阅：

-   [`Session`][agents.memory.session.Session] - 协议接口
-   [`OpenAIConversationsSession`][agents.memory.OpenAIConversationsSession] - OpenAI Conversations API 实现
-   [`OpenAIResponsesCompactionSession`][agents.memory.openai_responses_compaction_session.OpenAIResponsesCompactionSession] - Responses API 压缩包装器
-   [`SQLiteSession`][agents.memory.sqlite_session.SQLiteSession] - 基础 SQLite 实现
-   [`AsyncSQLiteSession`][agents.extensions.memory.async_sqlite_session.AsyncSQLiteSession] - 基于 `aiosqlite` 的异步 SQLite 实现
-   [`RedisSession`][agents.extensions.memory.redis_session.RedisSession] - Redis 后端会话实现
-   [`SQLAlchemySession`][agents.extensions.memory.sqlalchemy_session.SQLAlchemySession] - SQLAlchemy 驱动的实现
-   [`MongoDBSession`][agents.extensions.memory.mongodb_session.MongoDBSession] - MongoDB 后端会话实现
-   [`DaprSession`][agents.extensions.memory.dapr_session.DaprSession] - Dapr 状态存储实现
-   [`AdvancedSQLiteSession`][agents.extensions.memory.advanced_sqlite_session.AdvancedSQLiteSession] - 带分支和分析的增强型 SQLite
-   [`EncryptedSession`][agents.extensions.memory.encrypt_session.EncryptedSession] - 适用于任何会话的加密包装器