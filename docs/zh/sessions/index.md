---
search:
  exclude: true
---
# 会话

Agents SDK 提供内置会话记忆功能，可在多次智能体运行之间自动维护对话历史，无需在轮次之间手动处理 `.to_input_list()`。

会话存储特定会话的对话历史，使智能体无需显式的手动记忆管理即可保持上下文。这对于构建聊天应用或多轮对话尤其有用，因为在这些场景中，你希望智能体记住之前的交互。

如果希望 SDK 为你管理客户端记忆，请使用会话。在同一次运行中，会话不能与运行级续接选项 `conversation_id`、`previous_response_id` 或 `auto_previous_response_id` 结合使用。如果希望改用由OpenAI服务器管理的续接，请选择其中一种机制，而不是在其上叠加会话。

## 快速入门 {#quick-start}

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

## 使用同一会话恢复中断的运行 {#resuming-interrupted-runs-with-the-same-session}

如果运行暂停以等待批准，请使用同一个会话实例（或使用相同会话 ID 和相同底层存储后端配置的另一个实例）恢复运行，以便恢复后的轮次继续沿用同一份已存储的对话历史。

```python
result = await Runner.run(agent, "Delete temporary files that are no longer needed.", session=session)

if result.interruptions:
    state = result.to_state()
    for interruption in result.interruptions:
        state.approve(interruption)
    result = await Runner.run(agent, state, session=session)
```

## 核心会话行为 {#core-session-behavior}

启用会话记忆后：

1. **每次运行之前**：运行器会自动检索该会话的对话历史，并将其添加到输入项之前。
2. **每次运行之后**：运行期间生成的所有新项目（用户输入、助手响应、工具调用等）都会自动存储到会话中。
3. **上下文保留**：使用同一会话的每次后续运行都会包含完整的对话历史，使智能体能够保持上下文。

这样便无需手动调用 `.to_input_list()` 并在多次运行之间管理对话状态。

## 历史记录与新输入的合并控制 {#control-how-history-and-new-input-merge}

传入会话时，运行器通常会按以下顺序准备模型输入：

1. 会话历史记录（从 `session.get_items(...)` 检索）
2. 新轮次输入

使用 [`RunConfig.session_input_callback`][agents.run.RunConfig.session_input_callback] 可在调用模型之前自定义此合并步骤。回调接收两个列表：

-   `history`：检索到的会话历史记录（已规范化为输入项格式）
-   `new_input`：当前轮次的新输入项

返回应发送给模型的最终输入项列表。

回调接收的是两个列表的副本，因此你可以安全地修改它们。返回的列表控制该轮次的模型输入，但 SDK 仍只会持久化属于新轮次的项目。因此，对旧历史记录重新排序或进行筛选，不会导致旧会话项目再次作为新输入被保存。

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

如果需要在不改变会话项目存储方式的情况下，对历史记录进行自定义裁剪、重新排序或选择性纳入，请使用此功能。如果需要在模型调用前立即执行后续的最终处理，请使用[运行智能体指南](../running_agents.md)中的 [`call_model_input_filter`][agents.run.RunConfig.call_model_input_filter]。

## 检索历史记录的限制 {#limiting-retrieved-history}

使用 [`SessionSettings`][agents.memory.SessionSettings] 控制每次运行前获取的历史记录量。

-   `SessionSettings(limit=None)`（默认）：检索所有可用的会话项目
-   `SessionSettings(limit=N)`：仅检索最近的 `N` 个项目

你可以通过 [`RunConfig.session_settings`][agents.run.RunConfig.session_settings] 为每次运行应用此设置：

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

如果会话实现提供了默认会话设置，则 `RunConfig.session_settings` 中每个非 `None` 值都会覆盖该次运行对应的默认值。这对于长对话非常有用，可以限制检索数量而不改变会话的默认行为。

## 记忆操作 {#memory-operations}

### 基本操作 {#basic-operations}

会话支持多种对话历史管理操作：

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

### 使用 pop_item 进行更正 {#using-pop_item-for-corrections}

当你希望撤销或修改对话中的最后一个项目时，`pop_item` 方法尤其有用：

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

## 内置会话实现 {#built-in-session-implementations}

SDK 针对不同用例提供了多种会话实现：

### 内置会话实现的选择 {#choose-a-built-in-session-implementation}

在阅读下方详细代码示例之前，请使用此表选择起点。

| 会话类型 | 最适用场景 | 备注 |
| --- | --- | --- |
| `SQLiteSession` | 本地开发和简单应用 | 内置、轻量，可由文件或内存支持 |
| `AsyncSQLiteSession` | 使用 `aiosqlite` 的异步 SQLite | 支持异步驱动程序的扩展后端 |
| `RedisSession` | 在工作进程或服务之间共享记忆 | 适合低延迟分布式部署 |
| `SQLAlchemySession` | 使用现有数据库的生产应用 | 适用于 SQLAlchemy 支持的数据库 |
| `MongoDBSession` | 已使用 MongoDB 或需要多进程存储的应用 | 异步 pymongo；使用原子序列计数器排序 |
| `DaprSession` | 使用 Dapr sidecar 的云原生部署 | 支持多种状态存储以及 TTL 和一致性控制 |
| `OpenAIConversationsSession` | 由OpenAI服务器管理的存储 | 由 OpenAI Conversations API 支持的历史记录 |
| `OpenAIResponsesCompactionSession` | 需要自动压缩的长对话 | 对另一会话后端的封装 |
| `AdvancedSQLiteSession` | SQLite 加分支和分析功能 | 功能集较为丰富；请参阅专属页面 |
| `EncryptedSession` | 在另一会话之上添加加密和 TTL | 封装器；请先选择底层后端 |

一些实现有包含更多详细信息的专属页面，其链接位于各自的小节中。

如果你正在为 ChatKit 实现 Python 服务器，请使用 `chatkit.store.Store` 实现来持久化 ChatKit 的线程和项目。`SQLAlchemySession` 等 Agents SDK 会话用于管理 SDK 侧的对话历史，但不能直接替代 ChatKit 的存储。请参阅[实现 ChatKit 数据存储的 `chatkit-python` 指南](https://github.com/openai/chatkit-python/blob/main/docs/guides/respond-to-user-message.md#implement-your-chatkit-data-store)。

### OpenAI Conversations API 会话 {#openai-conversations-api-sessions}

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

### OpenAI Responses 压缩会话 {#openai-responses-compaction-sessions}

使用 `OpenAIResponsesCompactionSession` 通过 Responses API（`responses.compact`）压缩已存储的对话历史。它封装底层会话，并可根据 `should_trigger_compaction` 在每轮之后自动压缩。请勿用它封装 `OpenAIConversationsSession`；这两项功能以不同方式管理历史记录。

#### 典型用法（自动压缩） {#typical-usage-auto-compaction}

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

默认情况下，每轮之后，SDK 都会检查压缩候选内容是否达到阈值，并且仅在达到阈值时才执行压缩。

自动压缩运行时，SDK 会等待其完成，然后 `Runner.run(...)` 才会返回，或流式事件迭代器才会关闭。压缩请求所报告的用量会计入该次运行的 [`Usage`](../usage.md) 总量。默认情况下，之后手动调用 `run_compaction()` 时没有所属的运行上下文，因此不会更新已完成运行的用量对象。

`compaction_mode="previous_response_id"` 使用压缩会话保留的 Responses API 响应 ID，并且在该响应链仍然可用时效果最佳。`compaction_mode="input"` 则会根据当前会话项目重新构建压缩请求，适用于响应链不可用或希望以会话内容为事实依据的情况。默认的 `"auto"` 会选择最安全的可用选项。

如果智能体使用 `ModelSettings(store=False)` 运行，Responses API 不会保留上一次响应供后续查询。在这种无状态设置中，默认的 `"auto"` 模式会回退到基于输入的压缩，而不是依赖 `previous_response_id`。完整代码示例请参阅 [`examples/memory/compaction_session_stateless_example.py`](https://github.com/openai/openai-agents-python/tree/main/examples/memory/compaction_session_stateless_example.py)。

#### 自动压缩对流式传输的阻塞 {#auto-compaction-can-block-streaming}

压缩会清除并重写会话历史，因此 SDK 会等待压缩完成后才将运行视为已完成。在流式传输模式下，如果压缩任务较重，这意味着最后一个输出 token 发出后，`run.stream_events()` 可能还会保持打开数秒。

`OpenAIResponsesCompactionSession.run_compaction()` 会在封装器边界将清除并重写操作视为可恢复的替换。如果在底层历史记录发生变化后，替换失败或被取消，封装器会尝试恢复之前的历史记录，并等待该恢复操作结束，然后才将原始异常或取消通知给调用方。如果底层后端在恢复期间也发生故障，之前的历史记录可能仍无法恢复，SDK 会记录恢复失败。封装器会将 `add_items()`、`pop_item()` 和 `clear_session()` 与整个压缩操作串行执行，其中包括远程请求以及替换或恢复阶段。并发的封装器修改操作会等待压缩完成，然后应用于最终的历史记录，而不会被覆盖。自动压缩还会记录哪个封装器代次属于该次运行；如果另一次运行在压缩开始前更改了历史记录，SDK 会跳过过时的压缩，而不是替换更新后的历史记录。压缩运行期间，请勿直接修改底层会话，因为直接修改会绕过这些封装器保障。

如果希望获得低延迟流式传输或快速轮次切换，请禁用自动压缩，并在轮次之间（或空闲时）自行调用 `run_compaction()`。你可以根据自己的标准决定何时强制压缩。

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

### SQLite 会话 {#sqlite-sessions}

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

### 异步 SQLite 会话 {#async-sqlite-sessions}

如果希望使用由 `aiosqlite` 支持的 SQLite 持久化，请使用 `AsyncSQLiteSession`。

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

### Redis 会话 {#redis-sessions}

使用 `RedisSession` 可在多个工作进程或服务之间共享会话记忆。

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
await session.close()
```

`from_url(...)` 会创建并拥有 Redis 客户端。执行 `close()` 后，会话进入终止状态，后续会话操作会引发 `RuntimeError`；重复或并发调用 `close()` 是安全的。如果应用已经管理 Redis 客户端，请使用 `redis_client=...` 直接构造 `RedisSession(...)`。在这种情况下，`close()` 不执行任何操作，并且调用方继续拥有客户端所有权，会话也仍然可用。

### SQLAlchemy 会话 {#sqlalchemy-sessions}

使用 SQLAlchemy 支持的任意数据库，为 Agents SDK 提供适用于生产环境的会话持久化：

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

### Dapr 会话 {#dapr-sessions}

如果你已运行 Dapr sidecar，或希望在不更改智能体代码的情况下切换已配置的状态存储后端，请使用 `DaprSession`。

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

注意事项：

-   `from_address(...)` 会为你创建并拥有 Dapr 客户端。如果应用已经管理客户端，请使用 `dapr_client=...` 直接构造 `DaprSession(...)`。
-   退出上下文或调用 `close()` 会使拥有客户端的会话进入终止状态；后续会话操作会引发 `RuntimeError`，而重复或并发调用 `close()` 是安全的。使用注入的客户端时，`close()` 不执行任何操作，并且会话仍然可用。
-   如果底层状态存储支持 TTL，请传入 `ttl=...`，以便自动对会话数据应用 TTL 过期机制。
-   传入 `consistency=DAPR_CONSISTENCY_STRONG` 可请求对状态写入和删除使用强一致性（取决于存储）。请注意，线路级读取一致性要求上游 Dapr Python 客户端支持在 `get_state` 上设置一致性。
-   Dapr Python SDK 还会检查 HTTP sidecar 端点。在本地开发环境中，启动 Dapr 时，除了 `dapr_address` 中使用的 gRPC 端口，还应指定 `--dapr-http-port 3500`。
-   完整的设置演练（包括本地组件和故障排除）请参阅 [`examples/memory/dapr_session_example.py`](https://github.com/openai/openai-agents-python/tree/main/examples/memory/dapr_session_example.py)。


### MongoDB 会话 {#mongodb-sessions}

对于已经使用 MongoDB 或需要可横向扩展的多进程会话存储的应用，请使用 `MongoDBSession`。

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

注意事项：

-   `from_uri(...)` 会创建并拥有 `AsyncMongoClient`，并在调用 `session.close()` 时将其关闭。拥有客户端的会话在执行 `close()` 后进入终止状态，后续会话操作会引发 `RuntimeError`。如果应用已经管理客户端，请使用 `client=...` 直接构造 `MongoDBSession(...)`；在这种情况下，`session.close()` 不执行任何操作，调用方继续负责客户端生命周期，并且会话仍然可用。
-   将 `mongodb+srv://user:password@cluster.example.mongodb.net` URI 传入 `from_uri(...)`，无需其他更改即可连接到 [MongoDB Atlas](https://www.mongodb.com/products/platform)。
-   系统会使用两个集合，且两者名称均可配置：`sessions_collection=`（默认为 `agent_sessions`）和 `messages_collection=`（默认为 `agent_messages`）。首次使用时会自动创建索引。每次非空的 `add_items()` 调用都会写入一个逻辑批次文档，其单调递增的 `seq` 会根据批次中的最后一个项目对该批次进行排序；旧版的逐项目消息文档仍可读取。逻辑批次必须符合 MongoDB 的单文档大小限制；过大的批次会以原子方式失败，不会存储不完整的批次。
-   首次运行前，使用 `await session.ping()` 验证连接。

### 高级 SQLite 会话 {#advanced-sqlite-sessions}

具有对话分支、用量分析和结构化查询功能的增强型 SQLite 会话：

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

### 加密会话 {#encrypted-sessions}

适用于任何会话实现的透明加密封装器：

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

### 其他会话类型 {#other-session-types}

还有一些其他内置选项。请参阅 `examples/memory/` 以及 `extensions/memory/` 下的源代码。

## 运维模式 {#operational-patterns}

### 会话 ID 命名 {#session-id-naming}

使用有意义的会话 ID 来帮助组织对话：

-   基于用户：`"user_12345"`
-   基于线程：`"thread_abc123"`
-   基于上下文：`"support_ticket_456"`

### 记忆持久化 {#memory-persistence}

-   对于临时对话，使用内存 SQLite（`SQLiteSession("session_id")`）
-   对于持久化对话，使用基于文件的 SQLite（`SQLiteSession("session_id", "path/to/db.sqlite")`）
-   需要基于 `aiosqlite` 的实现时，使用异步 SQLite（`AsyncSQLiteSession("session_id", db_path="...")`）
-   对于共享的低延迟会话记忆，使用由 Redis 支持的会话（`RedisSession.from_url("session_id", url="redis://...")`）
-   对于使用 SQLAlchemy 所支持现有数据库的生产系统，使用由 SQLAlchemy 支持的会话（`SQLAlchemySession("session_id", engine=engine, create_tables=True)`）
-   对于已经使用 MongoDB 或需要多进程、可横向扩展会话存储的应用，使用 MongoDB 会话（`MongoDBSession.from_uri("session_id", uri="mongodb://localhost:27017")`）
-   对于需要内置遥测、追踪和数据隔离，并支持 30 多种数据库后端的生产云原生部署，使用 Dapr 状态存储会话（`DaprSession.from_address("session_id", state_store_name="statestore", dapr_address="localhost:50001")`）
-   如果希望将历史记录存储在 OpenAI Conversations API 中，请使用由OpenAI托管的存储（`OpenAIConversationsSession()`）
-   使用加密会话（`EncryptedSession(session_id, underlying_session, encryption_key)`）封装任何会话，以提供透明加密和基于 TTL 的过期机制
-   对于更高级的用例，可考虑为其他生产系统（例如 Django）实现自定义会话后端

### 多会话 {#multiple-sessions}

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

### 会话共享 {#session-sharing}

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

## 完整代码示例 {#complete-example}

以下是展示会话记忆实际运行方式的完整代码示例：

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

## 自定义会话实现 {#custom-session-implementations}

你可以创建一个在结构上遵循 [`Session`][agents.memory.session.Session] 协议的类，实现自己的会话记忆。无需继承 `SessionABC`；定义 `session_id` 和 `session_settings`，并直接实现四个历史记录方法：

```python
from agents import Agent, Runner, SessionSettings
from agents.items import TResponseInputItem


class MyCustomSession:
    """Custom session implementation following the Session protocol."""

    session_settings: SessionSettings | None = None

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.items: list[TResponseInputItem] = []

    async def get_items(self, limit: int | None = None) -> list[TResponseInputItem]:
        if limit is None:
            return list(self.items)
        if limit <= 0:
            return []
        return list(self.items[-limit:])

    async def add_items(self, items: list[TResponseInputItem]) -> None:
        self.items.extend(items)

    async def pop_item(self) -> TResponseInputItem | None:
        return self.items.pop() if self.items else None

    async def clear_session(self) -> None:
        self.items.clear()


# Use your custom session
agent = Agent(name="Assistant")
result = await Runner.run(
    agent,
    "Hello",
    session=MyCustomSession("my_session")
)
```

### 从自定义会话访问运行上下文 {#accessing-run-context-from-a-custom-session}

Agents SDK 可以将活跃的 [`RunContextWrapper`][agents.run_context.RunContextWrapper] 传递给自定义会话，以用于租户路由、授权或其他应用特定的存储决策。要让 Agents SDK 传递该封装器，请为所有四个历史记录方法添加一个具有显式名称且兼容关键字的 `wrapper` 参数：

```python
from typing import Any

from agents import RunContextWrapper
from agents.items import TResponseInputItem


class ContextAwareSession:
    async def get_items(
        self,
        limit: int | None = None,
        *,
        wrapper: RunContextWrapper[Any] | None = None,
    ) -> list[TResponseInputItem]: ...

    async def add_items(
        self,
        items: list[TResponseInputItem],
        *,
        wrapper: RunContextWrapper[Any] | None = None,
    ) -> None: ...

    async def pop_item(
        self,
        *,
        wrapper: RunContextWrapper[Any] | None = None,
    ) -> TResponseInputItem | None: ...

    async def clear_session(
        self,
        *,
        wrapper: RunContextWrapper[Any] | None = None,
    ) -> None: ...
```

仅当 `get_items`、`add_items`、`pop_item` 和 `clear_session` 均声明 `wrapper` 时，Agents SDK 才会启用此集成。通用的 `**kwargs` 参数不满足此签名检查。不包含 `wrapper` 的现有会话实现会保留其已发布的调用形式，并可继续工作，无需更改。

## 社区会话实现 {#community-session-implementations}

社区开发了其他会话实现：

| 软件包 | 说明 |
|---------|-------------|
| [openai-django-sessions](https://pypi.org/project/openai-django-sessions/) | 适用于 Django 所支持任意数据库（PostgreSQL、MySQL、SQLite 等）的基于 Django ORM 的会话 |

如果你构建了会话实现，欢迎提交文档 PR，将其添加到此处！

## API 参考 {#api-reference}

有关详细的 API 文档，请参阅：

-   [`Session`][agents.memory.session.Session] - 协议接口
-   [`OpenAIConversationsSession`][agents.memory.OpenAIConversationsSession] - OpenAI Conversations API 实现
-   [`OpenAIResponsesCompactionSession`][agents.memory.openai_responses_compaction_session.OpenAIResponsesCompactionSession] - Responses API 压缩封装器
-   [`SQLiteSession`][agents.memory.sqlite_session.SQLiteSession] - 基础 SQLite 实现
-   [`AsyncSQLiteSession`][agents.extensions.memory.async_sqlite_session.AsyncSQLiteSession] - 基于 `aiosqlite` 的异步 SQLite 实现
-   [`RedisSession`][agents.extensions.memory.redis_session.RedisSession] - 由 Redis 支持的会话实现
-   [`SQLAlchemySession`][agents.extensions.memory.sqlalchemy_session.SQLAlchemySession] - 由 SQLAlchemy 支持的实现
-   [`MongoDBSession`][agents.extensions.memory.mongodb_session.MongoDBSession] - 由 MongoDB 支持的会话实现
-   [`DaprSession`][agents.extensions.memory.dapr_session.DaprSession] - Dapr 状态存储实现
-   [`AdvancedSQLiteSession`][agents.extensions.memory.advanced_sqlite_session.AdvancedSQLiteSession] - 支持分支和分析功能的增强型 SQLite
-   [`EncryptedSession`][agents.extensions.memory.encrypt_session.EncryptedSession] - 适用于任何会话的加密封装器