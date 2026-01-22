---
search:
  exclude: true
---
# セッション

Agents SDK は、複数回の エージェント 実行にまたがって会話履歴を自動的に維持する、組み込みの セッション メモリを提供します。これにより、ターン間で `.to_input_list()` を手動で扱う必要がなくなります。

Sessions は特定の セッション の会話履歴を保存し、明示的な手動のメモリ管理を必要とせずに、エージェント がコンテキストを維持できるようにします。これは、エージェント に過去のやり取りを覚えさせたい チャット アプリケーションや複数ターンの会話を構築する際に特に役立ちます。

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

1. **各実行の前**: runner が セッション の会話履歴を自動的に取得し、入力アイテムの先頭に付加します。
2. **各実行の後**: 実行中に生成されたすべての新しいアイテム (ユーザー 入力、アシスタント 応答、ツール 呼び出しなど) が、自動的に セッション に保存されます。
3. **コンテキストの保持**: 同じ セッション での各後続の実行には会話履歴の全体が含まれ、エージェント がコンテキストを維持できます。

これにより、`.to_input_list()` を手動で呼び出して、実行間の会話状態を管理する必要がなくなります。

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

### 修正で pop_item を使用する

会話の最後のアイテムを取り消したり変更したりしたい場合、`pop_item` メソッドが特に便利です:

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

## セッション 種別

SDK は、さまざまな ユースケース 向けに複数の セッション 実装を提供します:

### OpenAI Conversations API セッション

`OpenAIConversationsSession` を通じて [OpenAI の Conversations API](https://platform.openai.com/docs/api-reference/conversations) を使用します。

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

`OpenAIResponsesCompactionSession` を使用して、Responses API (`responses.compact`) で セッション 履歴を圧縮します。基盤となる セッション をラップし、`should_trigger_compaction` に基づいて各ターン後に自動で圧縮できます。

#### 代表的な使い方 (自動圧縮)

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

デフォルトでは、候補しきい値に到達すると、圧縮は各ターン後に実行されます。

#### 自動圧縮は ストリーミング をブロックする場合があります

圧縮は セッション 履歴をクリアして書き直すため、SDK は圧縮が完了するまで実行完了とみなしません。ストリーミング モードでは、圧縮が重い場合、最後の出力トークンの後でも `run.stream_events()` が数秒間開いたままになることがあります。

低遅延の ストリーミング や高速なターン切り替えを望む場合は、自動圧縮を無効にして、ターン間 (またはアイドル時間中) に自分で `run_compaction()` を呼び出してください。独自の基準に基づいて、いつ圧縮を強制するかを判断できます。

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

SQLite を使用した、デフォルトの軽量な セッション 実装です:

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

SQLAlchemy がサポートする任意のデータベースを使用した、本番対応の セッション です:

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

会話の分岐、使用状況分析、structured queries を備えた拡張 SQLite セッション です:

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

任意の セッション 実装向けの透過的な暗号化ラッパーです:

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

他にもいくつかの組み込みオプションがあります。`examples/memory/` と、`extensions/memory/` 配下のソースコードを参照してください。

## セッション 管理

### セッション ID の命名

会話を整理しやすい、意味のある セッション ID を使用してください:

-   ユーザー ベース: `"user_12345"`
-   スレッド ベース: `"thread_abc123"`
-   コンテキスト ベース: `"support_ticket_456"`

### メモリの永続化

-   一時的な会話には、インメモリ SQLite (`SQLiteSession("session_id")`) を使用します
-   永続的な会話には、ファイル ベースの SQLite (`SQLiteSession("session_id", "path/to/db.sqlite")`) を使用します
-   SQLAlchemy がサポートする既存データベースを用いる本番システムには、SQLAlchemy 駆動の セッション (`SQLAlchemySession("session_id", engine=engine, create_tables=True)`) を使用します
-   本番のクラウドネイティブ デプロイには、Dapr state store セッション (`DaprSession.from_address("session_id", state_store_name="statestore", dapr_address="localhost:50001")`) を使用します。組み込みの telemetry、tracing、データ分離を備え、30+ のデータベース バックエンドをサポートします
-   OpenAI Conversations API に履歴を保存したい場合は、OpenAI がホストするストレージ (`OpenAIConversationsSession()`) を使用します
-   任意の セッション を透過的な暗号化と TTL ベースの有効期限でラップするには、暗号化 セッション (`EncryptedSession(session_id, underlying_session, encryption_key)`) を使用します
-   より高度な ユースケース のために、他の本番システム (Redis、Django など) 向けのカスタム セッション バックエンドの実装も検討してください

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

### セッション の共有

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

以下は、セッション メモリが動作している様子を示す完全な例です:

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

コミュニティが追加の セッション 実装を開発しています:

| Package | 説明 |
|---------|-------------|
| [openai-django-sessions](https://pypi.org/project/openai-django-sessions/) | Django がサポートする任意のデータベース (PostgreSQL、MySQL、SQLite、ほか) 向けの Django ORM ベースの セッション |

セッション 実装を構築した場合は、ぜひドキュメントの PR を提出して、ここに追加してください。

## API リファレンス

詳細な API ドキュメントは次を参照してください:

-   [`Session`][agents.memory.session.Session] - プロトコル インターフェース
-   [`OpenAIConversationsSession`][agents.memory.OpenAIConversationsSession] - OpenAI Conversations API 実装
-   [`OpenAIResponsesCompactionSession`][agents.memory.openai_responses_compaction_session.OpenAIResponsesCompactionSession] - Responses API 圧縮ラッパー
-   [`SQLiteSession`][agents.memory.sqlite_session.SQLiteSession] - 基本 SQLite 実装
-   [`SQLAlchemySession`][agents.extensions.memory.sqlalchemy_session.SQLAlchemySession] - SQLAlchemy 駆動の実装
-   [`DaprSession`][agents.extensions.memory.dapr_session.DaprSession] - Dapr state store 実装
-   [`AdvancedSQLiteSession`][agents.extensions.memory.advanced_sqlite_session.AdvancedSQLiteSession] - 分岐と分析を備えた拡張 SQLite
-   [`EncryptedSession`][agents.extensions.memory.encrypt_session.EncryptedSession] - 任意の セッション 向け暗号化ラッパー