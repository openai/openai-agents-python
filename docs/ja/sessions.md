---
search:
  exclude: true
---
# セッション

Agents SDK は、複数のエージェント実行にまたがって会話履歴を自動的に維持するための組み込みセッションメモリを提供し、ターン間で手動で `.to_input_list()` を扱う必要をなくします。

セッションは特定のセッションの会話履歴を保存し、明示的な手動メモリ管理なしにエージェントがコンテキストを維持できるようにします。これは、エージェントに以前のやり取りを記憶させたいチャットアプリケーションやマルチターンの会話を構築する場合に特に有用です。

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

1. **各実行の前**: ランナーはセッションの会話履歴を自動的に取得し、入力アイテムの先頭に付加します。
2. **各実行の後**: 実行中に生成された新しいすべてのアイテム (ユーザー入力、アシスタントの応答、ツール呼び出しなど) は自動的にセッションに保存されます。
3. **コンテキストの保持**: 同じセッションでの後続の各実行には会話履歴全体が含まれるため、エージェントはコンテキストを維持できます。

これにより、`.to_input_list()` を手動で呼び出して実行間の会話状態を管理する必要がなくなります。

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

### 修正のための `pop_item` の使用

`pop_item` メソッドは、会話の最後のアイテムを取り消したり修正したりしたいときに特に便利です:

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

### メモリなし (デフォルト)

```python
# Default behavior - no session memory
result = await Runner.run(agent, "Hello")
```

### OpenAI Conversations API メモリ

自前のデータベースを管理せずに
[conversation state](https://platform.openai.com/docs/guides/conversation-state?api-mode=responses#using-the-conversations-api) を永続化するには、[OpenAI Conversations API](https://platform.openai.com/docs/api-reference/conversations/create) を使用します。これは、会話履歴の保存に OpenAI ホスト型インフラストラクチャにすでに依存している場合に役立ちます。

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

### SQLAlchemy 対応セッション

より高度なユースケース向けに、SQLAlchemy 駆動のセッションバックエンドを使用できます。これにより、SQLAlchemy がサポートする任意のデータベース (PostgreSQL、MySQL、SQLite など) をセッションストレージに使用できます。

**例 1: `from_url` を使用したメモリ内 SQLite**

これは最も簡単な開始方法で、開発およびテストに最適です。

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

本番アプリケーションでは、すでに SQLAlchemy の `AsyncEngine` インスタンスを持っている可能性があります。これをセッションに直接渡せます。

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

### 暗号化セッション

保存時に会話データの暗号化が必要なアプリケーションでは、`EncryptedSession` を使用して任意のセッションバックエンドを透過的な暗号化と自動の TTL ベース有効期限でラップできます。これは `encrypt` エクストラが必要です: `pip install openai-agents[encrypt]`。

`EncryptedSession` は、セッションごとのキー導出 (HKDF) を用いた Fernet 暗号化を使用し、古いメッセージの自動有効期限切れをサポートします。アイテムが TTL を超えた場合、取得時に暗黙的にスキップされます。

**例: SQLAlchemy セッションデータの暗号化**

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

**主な特長:**

- **透過的な暗号化**: 保存前にすべてのセッションアイテムを自動的に暗号化し、取得時に復号
- **セッションごとのキー導出**: セッション ID をソルトとして用いた HKDF により一意の暗号鍵を導出
- **TTL ベースの有効期限**: 設定可能な有効期間に基づいて古いメッセージを自動的に期限切れに (デフォルト: 10 分)
- **柔軟な鍵入力**: Fernet キーまたは生の文字列のいずれも暗号鍵として受け付け
- **任意のセッションをラップ**: SQLite、SQLAlchemy、またはカスタムセッション実装で動作

!!! warning "重要なセキュリティに関する注意"

    - 暗号鍵は安全に保管してください (例: 環境変数、シークレットマネージャー)
    - 期限切れトークンはアプリケーション サーバーのシステムクロックに基づいて拒否されます。クロックスキューにより有効なトークンが拒否されるのを避けるため、すべてのサーバーが NTP で時刻同期されていることを確認してください
    - 基盤となるセッションは引き続き暗号化データを保存するため、データベースインフラストラクチャを制御できます


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

会話を整理しやすい意味のあるセッション ID を使用します:

- ユーザー基準: `"user_12345"`
- スレッド基準: `"thread_abc123"`
- コンテキスト基準: `"support_ticket_456"`

### メモリの永続化

- 一時的な会話にはメモリ内 SQLite (`SQLiteSession("session_id")`) を使用
- 永続的な会話にはファイルベース SQLite (`SQLiteSession("session_id", "path/to/db.sqlite")`) を使用
- 既存のデータベースを持つ本番システムには SQLAlchemy 対応セッション (`SQLAlchemySession("session_id", engine=engine, create_tables=True)`) を使用
- 履歴を OpenAI Conversations API に保存したい場合は OpenAI ホスト型ストレージ (`OpenAIConversationsSession()`) を使用
- 任意のセッションを透過的な暗号化と TTL ベース有効期限でラップするには暗号化セッション (`EncryptedSession(session_id, underlying_session, encryption_key)`) を使用
- より高度なユースケースでは、他の本番システム (Redis、Django など) 向けにカスタムセッションバックエンドの実装を検討

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

- [`Session`][agents.memory.Session] - プロトコルインターフェース
- [`SQLiteSession`][agents.memory.SQLiteSession] - SQLite 実装
- [`OpenAIConversationsSession`](ref/memory/openai_conversations_session.md) - OpenAI Conversations API 実装
- [`SQLAlchemySession`][agents.extensions.memory.sqlalchemy_session.SQLAlchemySession] - SQLAlchemy 対応実装
- [`EncryptedSession`][agents.extensions.memory.encrypt_session.EncryptedSession] - TTL 付き暗号化セッションラッパー