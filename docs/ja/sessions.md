---
search:
  exclude: true
---
# セッション

Agents SDK は、複数のエージェント実行にまたがって会話履歴を自動的に保持する組み込みのセッションメモリを提供し、ターン間で手動で `.to_input_list()` を扱う必要をなくします。

Sessions は特定のセッションの会話履歴を保存し、明示的な手動メモリ管理なしでエージェントがコンテキストを維持できるようにします。これは、エージェントに過去のやり取りを覚えさせたいチャットアプリケーションやマルチターン会話の構築に特に有用です。

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

1.  **各実行の前** : ランナーはセッションの会話履歴を自動的に取得し、入力アイテムの先頭に追加します。
2.  **各実行の後** : 実行中に生成された新しいアイテム（ユーザー入力、アシスタントの応答、ツール呼び出しなど）はすべて自動的にセッションに保存されます。
3.  **コンテキストの保持** : 同じセッションでの以降の各実行には完全な会話履歴が含まれ、エージェントがコンテキストを維持できます。

これにより、`.to_input_list()` を手動で呼び出し、実行間の会話状態を管理する必要がなくなります。

## メモリ操作

### 基本操作

Sessions は会話履歴を管理するためのいくつかの操作をサポートします:

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

### 訂正のための `pop_item` の使用

`pop_item` メソッドは、会話の最後のアイテムを元に戻したり修正したい場合に特に有用です:

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

## メモリオプション

### メモリなし（デフォルト）

```python
# Default behavior - no session memory
result = await Runner.run(agent, "Hello")
```

### OpenAI Conversations API メモリ

独自のデータベースを管理せずに [conversation state](https://platform.openai.com/docs/guides/conversation-state?api-mode=responses#using-the-conversations-api) を永続化するには、[OpenAI Conversations API](https://platform.openai.com/docs/api-reference/conversations/create) を使用します。これは、会話履歴の保存に OpenAI ホストのインフラストラクチャにすでに依存している場合に役立ちます。

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

### SQLite メモリ

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

### 複数セッション

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

### SQLAlchemy ベースのセッション

さらに高度なユースケースでは、SQLAlchemy ベースのセッションバックエンドを使用できます。これにより、セッションストレージに SQLAlchemy がサポートする任意のデータベース（PostgreSQL、MySQL、SQLite など）を使用できます。

**例 1: `from_url` とインメモリ SQLite の使用**

これは最も簡単な開始方法で、開発やテストに最適です。

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

**例 2: 既存の SQLAlchemy エンジンの使用**

本番アプリケーションでは、すでに SQLAlchemy の `AsyncEngine` インスタンスがある可能性が高いです。これをセッションに直接渡せます。

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


## カスタムメモリ実装

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

## セッション管理

### セッション ID の命名

会話を整理するのに役立つ意味のあるセッション ID を使用します:

-   ユーザー基準: `"user_12345"`
-   スレッド基準: `"thread_abc123"`
-   コンテキスト基準: `"support_ticket_456"`

### メモリの永続化

-   一時的な会話にはインメモリ SQLite（`SQLiteSession("session_id")`）を使用します
-   永続的な会話にはファイルベースの SQLite（`SQLiteSession("session_id", "path/to/db.sqlite")`）を使用します
-   既存のデータベース（SQLAlchemy がサポート）を持つ本番システムには SQLAlchemy ベースのセッション（`SQLAlchemySession("session_id", engine=engine, create_tables=True)`）を使用します
-   履歴を OpenAI Conversations API に保存したい場合は OpenAI ホスト型ストレージ（`OpenAIConversationsSession()`）を使用します
-   さらに高度なユースケースでは、他の本番システム（Redis、Django など）向けにカスタムセッションバックエンドの実装を検討します

### セッション管理

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

## 完全な例

セッションメモリが動作する完全な例を次に示します:

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

## API リファレンス

詳細な API ドキュメントは次を参照してください:

-   [`Session`][agents.memory.Session] - プロトコルインターフェース
-   [`SQLiteSession`][agents.memory.SQLiteSession] - SQLite 実装
-   [`OpenAIConversationsSession`](ref/memory/openai_conversations_session.md) - OpenAI Conversations API 実装
-   [`SQLAlchemySession`][agents.extensions.memory.sqlalchemy_session.SQLAlchemySession] - SQLAlchemy ベースの実装