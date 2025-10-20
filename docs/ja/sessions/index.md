---
search:
  exclude: true
---
# セッション

Agents SDK は、複数のエージェント実行にわたって会話履歴を自動的に維持する組み込みのセッションメモリを提供し、ターン間で手動で `.to_input_list()` を扱う必要をなくします。

セッションは特定のセッションの会話履歴を保存し、明示的な手動メモリ管理なしでエージェントがコンテキストを維持できるようにします。これは、エージェントに以前のやり取りを記憶させたいチャットアプリケーションやマルチターンの会話を構築する際に特に有用です。

## クイックスタート

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

## 仕組み

セッションメモリが有効な場合:

1. **各実行前**: ランナーはセッションの会話履歴を自動的に取得し、入力アイテムの先頭に付加します。
2. **各実行後**: 実行中に生成されたすべての新しいアイテム（ユーザー入力、アシスタントの応答、ツール呼び出しなど）が自動的にセッションに保存されます。
3. **コンテキスト保持**: 同じセッションでの後続の各実行には完全な会話履歴が含まれ、エージェントはコンテキストを維持できます。

これにより、実行間で手動で `.to_input_list()` を呼び出して会話状態を管理する必要がなくなります。

## メモリ操作

### 基本操作

セッションは会話履歴を管理するためのいくつかの操作をサポートします:

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

### 修正のための pop_item の使用

`pop_item` メソッドは、会話の最後のアイテムを取り消したり変更したりしたい場合に特に便利です:

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

## セッションタイプ

SDK は、さまざまなユースケース向けにいくつかのセッション実装を提供します:

### OpenAI Conversations API セッション

`OpenAIConversationsSession` を介して [OpenAI の Conversations API](https://platform.openai.com/docs/api-reference/conversations) を使用します。

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

### SQLite セッション

SQLite を使用したデフォルトの軽量セッション実装:

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

### SQLAlchemy セッション

あらゆる SQLAlchemy 対応データベースを使用する本番対応のセッション:

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

詳細なドキュメントは [SQLAlchemy セッション](sqlalchemy_session.md) を参照してください。

### 高度な SQLite セッション

会話の分岐、利用分析、構造化クエリを備えた強化版 SQLite セッション:

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

詳細なドキュメントは [高度な SQLite セッション](advanced_sqlite_session.md) を参照してください。

### 暗号化セッション

あらゆるセッション実装向けの透過的な暗号化ラッパー:

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

詳細なドキュメントは [暗号化セッション](encrypted_session.md) を参照してください。

## セッション管理

### セッション ID の命名

会話を整理しやすい意味のあるセッション ID を使用します:

-   ユーザー単位: `"user_12345"`
-   スレッド単位: `"thread_abc123"`
-   コンテキスト単位: `"support_ticket_456"`

### メモリ永続化

-   一時的な会話にはインメモリ SQLite（`SQLiteSession("session_id")`）を使用します
-   永続的な会話にはファイルベースの SQLite（`SQLiteSession("session_id", "path/to/db.sqlite")`）を使用します
-   既存のデータベースを SQLAlchemy がサポートする本番システムには SQLAlchemy 対応のセッション（`SQLAlchemySession("session_id", engine=engine, create_tables=True)`）を使用します
-   履歴を OpenAI Conversations API に保存したい場合は OpenAI ホストのストレージ（`OpenAIConversationsSession()`）を使用します
-   あらゆるセッションを透過的な暗号化と TTL ベースの有効期限でラップするには暗号化セッション（`EncryptedSession(session_id, underlying_session, encryption_key)`）を使用します
-   より高度なユースケースには、他の本番システム（Redis、Django など）向けのカスタムセッションバックエンドの実装を検討してください

### 複数セッション

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

### セッション共有

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

## 完全な例

セッションメモリがどのように機能するかを示す完全な例です:

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

## カスタムセッション実装

[`Session`][agents.memory.session.Session] プロトコルに従うクラスを作成することで、独自のセッションメモリを実装できます:

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

## API リファレンス

詳細な API ドキュメントは以下を参照してください:

-   [`Session`][agents.memory.session.Session] - プロトコルインターフェース
-   [`OpenAIConversationsSession`][agents.memory.OpenAIConversationsSession] - OpenAI Conversations API 実装
-   [`SQLiteSession`][agents.memory.sqlite_session.SQLiteSession] - 基本的な SQLite 実装
-   [`SQLAlchemySession`][agents.extensions.memory.sqlalchemy_session.SQLAlchemySession] - SQLAlchemy 対応実装
-   [`AdvancedSQLiteSession`][agents.extensions.memory.advanced_sqlite_session.AdvancedSQLiteSession] - 分岐と分析を備えた強化版 SQLite
-   [`EncryptedSession`][agents.extensions.memory.encrypt_session.EncryptedSession] - 任意のセッション向けの暗号化ラッパー