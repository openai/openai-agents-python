---
search:
  exclude: true
---
# セッション

Agents SDK は組み込みのセッションメモリーを提供しており、複数回のエージェント実行にわたって会話履歴を自動的に保持します。そのため、ターンごとに `.to_input_list()` を手動で扱う必要がありません。

セッションは特定のセッションに対する会話履歴を保存し、明示的な手動メモリー管理なしでコンテキストを保持できます。これは、エージェントに過去のやり取りを記憶させたいチャットアプリケーションや複数ターンの会話を構築する際に特に便利です。

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

## 動作概要

セッションメモリーが有効な場合:

1. **各実行の前**: runner はセッションの会話履歴を自動的に取得し、入力アイテムの先頭に追加します。  
2. **各実行の後**: 実行中に生成された新しいアイテム（ユーザー入力、アシスタント応答、ツール呼び出しなど）はすべて自動的にセッションに保存されます。  
3. **コンテキストの保持**: 同じセッションで次に実行するときは、完全な会話履歴が含まれるため、エージェントがコンテキストを維持できます。  

これにより、実行間で `.to_input_list()` を手動で呼び出したり会話状態を管理したりする必要がなくなります。

## メモリー操作

### 基本操作

セッションは会話履歴を管理するために、いくつかの操作をサポートしています:

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

### pop_item を使用した修正

会話の最後のアイテムを取り消したり修正したりしたい場合、`pop_item` メソッドが特に便利です:

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

## メモリーオプション

### メモリーなし（デフォルト）

```python
# Default behavior - no session memory
result = await Runner.run(agent, "Hello")
```

### SQLite メモリー

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

## カスタムメモリー実装

独自のセッションメモリーを実装するには、[`Session`][agents.memory.session.Session] プロトコルに従うクラスを作成します:

```python
from agents.memory import Session
from typing import List

class MyCustomSession:
    """Custom session implementation following the Session protocol."""

    def __init__(self, session_id: str):
        self.session_id = session_id
        # Your initialization here

    async def get_items(self, limit: int | None = None) -> List[dict]:
        """Retrieve conversation history for this session."""
        # Your implementation here
        pass

    async def add_items(self, items: List[dict]) -> None:
        """Store new items for this session."""
        # Your implementation here
        pass

    async def pop_item(self) -> dict | None:
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

会話を整理しやすいわかりやすいセッション ID を使用してください:

- ユーザー単位: `"user_12345"`
- スレッド単位: `"thread_abc123"`
- コンテキスト単位: `"support_ticket_456"`

### メモリー永続化

- 一時的な会話にはインメモリー SQLite（`SQLiteSession("session_id")`）を使用します  
- 永続的な会話にはファイルベース SQLite（`SQLiteSession("session_id", "path/to/db.sqlite")`）を使用します  
- 本番環境ではカスタムセッションバックエンド（Redis、PostgreSQL など）の実装を検討してください  

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

セッションメモリーの動作を示す完全な例を以下に示します:

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

## API 参照

詳細な API ドキュメントは次を参照してください:

- [`Session`][agents.memory.Session] - プロトコルインターフェース
- [`SQLiteSession`][agents.memory.SQLiteSession] - SQLite 実装