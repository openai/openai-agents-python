---
search:
  exclude: true
---
# セッション

Agents SDK は、複数回の エージェント 実行にわたって会話履歴を自動的に維持するための、組み込みの セッション メモリを提供します。これにより、ターン間で `.to_input_list()` を手動で扱う必要がなくなります。

Sessions は特定の セッション の会話履歴を保存し、明示的な手動のメモリ管理を不要にして エージェント がコンテキストを維持できるようにします。これは、チャット アプリケーションや、エージェント に過去のやり取りを覚えさせたいマルチターン会話の構築に特に有用です。

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

セッション メモリが有効な場合:

1. **各実行の前**: ランナーが セッション の会話履歴を自動的に取得し、入力アイテムの先頭に追加します。
2. **各実行の後**: 実行中に生成された新しいすべてのアイテム（ユーザー 入力、アシスタント 応答、ツール 呼び出しなど）が、自動的に セッション に保存されます。
3. **コンテキスト保持**: 同一の セッション での以降の各実行には会話履歴全体が含まれ、エージェント はコンテキストを維持できます。

これにより、`.to_input_list()` を手動で呼び出して実行間の会話状態を管理する必要がなくなります。

## メモリ操作

### 基本操作

Sessions は、会話履歴を管理するための複数の操作をサポートします:

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

### 修正に pop_item を使用する

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

## セッション種別

SDK は、異なるユースケース向けに複数の セッション 実装を提供します:

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

### OpenAI Responses 圧縮 セッション

`OpenAIResponsesCompactionSession` を使用して、Responses API (`responses.compact`) で セッション 履歴を圧縮します。基盤となる セッション をラップし、`should_trigger_compaction` に基づいて各ターン後に自動的に圧縮できます。

#### 典型的な使用方法（自動圧縮）

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

デフォルトでは、候補のしきい値に達すると、各ターン後に圧縮が実行されます。

#### 自動圧縮は ストリーミング をブロックする場合があります

圧縮は セッション 履歴をクリアして書き直すため、SDK は圧縮の完了を待ってから実行完了と見なします。ストリーミング モードでは、圧縮が重い場合、最後の出力トークンの後も `run.stream_events()` が数秒間開いたままになることがあります。

低レイテンシの ストリーミング や高速なターンテイキングが必要な場合は、自動圧縮を無効化し、ターン間（またはアイドル時間中）に自分で `run_compaction()` を呼び出してください。独自の基準に基づいて、いつ圧縮を強制するかを決められます。

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

### SQLite セッション

SQLite を使用する、デフォルトの軽量な セッション 実装です:

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

### Async SQLite セッション

`aiosqlite` による SQLite 永続化が必要な場合は `AsyncSQLiteSession` を使用します。

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

### Redis セッション

複数のワーカーやサービス間で共有する セッション メモリには `RedisSession` を使用します。

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

### SQLAlchemy セッション

任意の SQLAlchemy 対応データベースを使用する、本番運用向け セッション です:

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

詳細なドキュメントは [SQLAlchemy Sessions](sqlalchemy_session.md) を参照してください。



### 高度な SQLite セッション

会話の分岐、使用状況分析、structured outputs のクエリを備えた拡張 SQLite セッション です:

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

詳細なドキュメントは [Advanced SQLite Sessions](advanced_sqlite_session.md) を参照してください。

### 暗号化 セッション

任意の セッション 実装に対する、透過的な暗号化ラッパーです:

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

詳細なドキュメントは [Encrypted Sessions](encrypted_session.md) を参照してください。

### その他の セッション 種別

組み込みオプションは他にもいくつかあります。`examples/memory/` と、`extensions/memory/` 配下のソースコードを参照してください。

## セッション 管理

### セッション ID の命名

会話を整理しやすい、意味のある セッション ID を使用してください:

-   ユーザー ベース: `"user_12345"`
-   スレッド ベース: `"thread_abc123"`
-   コンテキスト ベース: `"support_ticket_456"`

### メモリ永続化

-   一時的な会話にはインメモリ SQLite (`SQLiteSession("session_id")`) を使用します
-   永続的な会話にはファイル ベース SQLite (`SQLiteSession("session_id", "path/to/db.sqlite")`) を使用します
-   `aiosqlite` ベースの実装が必要な場合は async SQLite (`AsyncSQLiteSession("session_id", db_path="...")`) を使用します
-   共有かつ低レイテンシの セッション メモリには Redis バックエンド セッション (`RedisSession.from_url("session_id", url="redis://...")`) を使用します
-   SQLAlchemy がサポートする既存データベースを用いる本番システムには、SQLAlchemy 搭載 セッション (`SQLAlchemySession("session_id", engine=engine, create_tables=True)`) を使用します
-   30+ のデータベース バックエンドのサポートに加え、組み込みのテレメトリ、トレーシング、データ分離を備えた本番のクラウドネイティブ配備には、Dapr ステートストア セッション (`DaprSession.from_address("session_id", state_store_name="statestore", dapr_address="localhost:50001")`) を使用します
-   OpenAI Conversations API に履歴を保存したい場合は OpenAI がホストするストレージ (`OpenAIConversationsSession()`) を使用します
-   透過的な暗号化と TTL ベースの期限切れを備えて任意の セッション をラップするには、暗号化 セッション (`EncryptedSession(session_id, underlying_session, encryption_key)`) を使用します
-   より高度なユースケース向けに、他の本番システム（例: Django）に対するカスタム セッション バックエンドの実装も検討してください

### 複数 セッション

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

### セッション 共有

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

セッション メモリの動作を示す完全な例です:

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

## カスタム セッション 実装

[`Session`][agents.memory.session.Session] プロトコルに従うクラスを作成することで、独自の セッション メモリを実装できます:

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

## コミュニティの セッション 実装

コミュニティは追加の セッション 実装を開発しています:

| Package | Description |
|---------|-------------|
| [openai-django-sessions](https://pypi.org/project/openai-django-sessions/) | Django がサポートする任意のデータベース（PostgreSQL、MySQL、SQLite など）向けの Django ORM ベース セッション |

セッション 実装を構築した場合は、ここに追加するためのドキュメント PR をぜひ送ってください。

## API リファレンス

詳細な API ドキュメントは次を参照してください:

-   [`Session`][agents.memory.session.Session] - プロトコル インターフェース
-   [`OpenAIConversationsSession`][agents.memory.OpenAIConversationsSession] - OpenAI Conversations API 実装
-   [`OpenAIResponsesCompactionSession`][agents.memory.openai_responses_compaction_session.OpenAIResponsesCompactionSession] - Responses API 圧縮ラッパー
-   [`SQLiteSession`][agents.memory.sqlite_session.SQLiteSession] - 基本 SQLite 実装
-   [`AsyncSQLiteSession`][agents.extensions.memory.async_sqlite_session.AsyncSQLiteSession] - `aiosqlite` に基づく Async SQLite 実装
-   [`RedisSession`][agents.extensions.memory.redis_session.RedisSession] - Redis バックエンドの セッション 実装
-   [`SQLAlchemySession`][agents.extensions.memory.sqlalchemy_session.SQLAlchemySession] - SQLAlchemy 搭載 実装
-   [`DaprSession`][agents.extensions.memory.dapr_session.DaprSession] - Dapr ステートストア 実装
-   [`AdvancedSQLiteSession`][agents.extensions.memory.advanced_sqlite_session.AdvancedSQLiteSession] - 分岐と分析を備えた拡張 SQLite
-   [`EncryptedSession`][agents.extensions.memory.encrypt_session.EncryptedSession] - 任意の セッション 向け暗号化ラッパー