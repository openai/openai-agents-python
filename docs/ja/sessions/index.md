---
search:
  exclude: true
---
# セッション

Agents SDK は組み込みのセッションメモリを提供し、複数のエージェント実行にまたがる会話履歴を自動で維持するため、ターン間で `.to_input_list()` を手動で扱う必要がありません。

Sessions は特定のセッションの会話履歴を保存し、明示的な手動メモリ管理なしでエージェントがコンテキストを維持できるようにします。これは、エージェントに過去のやり取りを記憶させたいチャットアプリケーションや複数ターンの会話を構築する際に特に有用です。

SDK にクライアント側メモリを管理させたい場合は、セッションを使用します。すでに `conversation_id` または `previous_response_id` を使って OpenAI のサーバー管理状態を使用している場合、通常は同じ会話に対してセッションを併用する必要はありません。

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

## 同一セッションによる中断実行の再開

実行が承認待ちで一時停止した場合は、同じセッションインスタンス（または同じバックエンドストアを指す別のセッションインスタンス）で再開し、再開ターンが同じ保存済み会話履歴を継続するようにします。

```python
result = await Runner.run(agent, "Delete temporary files that are no longer needed.", session=session)

if result.interruptions:
    state = result.to_state()
    for interruption in result.interruptions:
        state.approve(interruption)
    result = await Runner.run(agent, state, session=session)
```

## セッションのコア動作

セッションメモリが有効な場合:

1. **各実行前**: ランナーはセッションの会話履歴を自動で取得し、入力アイテムの先頭に追加します。
2. **各実行後**: 実行中に生成されたすべての新規アイテム（ユーザー入力、アシスタント応答、ツール呼び出しなど）が自動的にセッションへ保存されます。
3. **コンテキスト維持**: 同じセッションでの後続実行には会話履歴全体が含まれ、エージェントがコンテキストを維持できます。

これにより、 `.to_input_list()` の手動呼び出しや、実行間の会話状態管理が不要になります。

## 履歴と新規入力のマージ制御

セッションを渡すと、ランナーは通常次のようにモデル入力を準備します:

1. セッション履歴（ `session.get_items(...)` から取得）
2. 新しいターン入力

モデル呼び出し前のこのマージ手順をカスタマイズするには、 [`RunConfig.session_input_callback`][agents.run.RunConfig.session_input_callback] を使用します。コールバックは次の 2 つのリストを受け取ります:

-   `history`: 取得されたセッション履歴（すでに入力アイテム形式に正規化済み）
-   `new_input`: 現在ターンの新規入力アイテム

モデルに送信すべき最終的な入力アイテムのリストを返してください。

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

セッションでのアイテム保存方法を変更せずに、履歴のカスタムな間引き、並べ替え、または選択的な含有が必要な場合に使用します。

## 取得履歴の制限

各実行前に取得する履歴量を制御するには、 [`SessionSettings`][agents.memory.SessionSettings] を使用します。

-   `SessionSettings(limit=None)` （デフォルト）: 利用可能なセッションアイテムをすべて取得
-   `SessionSettings(limit=N)`: 直近 `N` 件のアイテムのみ取得

これは [`RunConfig.session_settings`][agents.run.RunConfig.session_settings] により実行ごとに適用できます:

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

セッション実装がデフォルトのセッション設定を公開している場合、 `RunConfig.session_settings` はその実行において `None` でない値を上書きします。これは、セッションのデフォルト動作を変えずに取得サイズを制限したい長い会話で有用です。

## メモリ操作

### 基本操作

Sessions は会話履歴を管理するための複数の操作をサポートします:

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

`pop_item` メソッドは、会話の最後のアイテムを取り消したり変更したい場合に特に有用です:

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

## 組み込みセッション実装

SDK は異なるユースケース向けに複数のセッション実装を提供します:

### 組み込みセッション実装の選択

以下の詳細な例を読む前に、この表を使って開始点を選んでください。

| Session type | Best for | Notes |
| --- | --- | --- |
| `SQLiteSession` | ローカル開発とシンプルなアプリ | 組み込み、軽量、ファイルベースまたはインメモリ |
| `AsyncSQLiteSession` | `aiosqlite` を使う非同期 SQLite | 非同期ドライバー対応の拡張バックエンド |
| `RedisSession` | ワーカー/サービス間で共有するメモリ | 低レイテンシーな分散デプロイに適しています |
| `SQLAlchemySession` | 既存データベースを使う本番アプリ | SQLAlchemy 対応データベースで動作 |
| `DaprSession` | Dapr サイドカーを使うクラウドネイティブデプロイ | 複数のステートストアに加え TTL と整合性制御をサポート |
| `OpenAIConversationsSession` | OpenAI でのサーバー管理ストレージ | OpenAI Conversations API ベースの履歴 |
| `OpenAIResponsesCompactionSession` | 自動圧縮を伴う長い会話 | 別のセッションバックエンドをラップ |
| `AdvancedSQLiteSession` | 分岐/分析機能付き SQLite | 機能が多い実装です。専用ページを参照してください |
| `EncryptedSession` | 別セッション上での暗号化 + TTL | ラッパーです。先に基盤バックエンドを選択してください |

一部の実装には追加詳細を記載した専用ページがあります。各サブセクション内でリンクしています。

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

### OpenAI Responses 圧縮セッション

Responses API （ `responses.compact` ）で保存済み会話履歴を圧縮するには `OpenAIResponsesCompactionSession` を使用します。これは基盤セッションをラップし、 `should_trigger_compaction` に基づいて各ターン後に自動圧縮できます。 `OpenAIConversationsSession` をこれでラップしないでください。両者は異なる方法で履歴を管理します。

#### 典型的な使用法（自動圧縮）

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

デフォルトでは、候補しきい値に達すると各ターン後に圧縮が実行されます。

`compaction_mode="previous_response_id"` は、Responses API の response ID ですでにターン連結している場合に最適です。 `compaction_mode="input"` は代わりに現在のセッションアイテムから圧縮リクエストを再構築するため、応答チェーンが利用できない場合やセッション内容を信頼できる情報源にしたい場合に有用です。デフォルトの `"auto"` は利用可能な中で最も安全なオプションを選択します。

#### 自動圧縮はストリーミングをブロックする可能性

圧縮はセッション履歴をクリアして書き直すため、SDK は圧縮完了まで実行完了とみなしません。ストリーミングモードでは、圧縮が重い場合、最後の出力トークンの後も `run.stream_events()` が数秒開いたままになることがあります。

低レイテンシーなストリーミングや高速なターン切り替えが必要な場合は、自動圧縮を無効にし、ターン間（またはアイドル時間）に `run_compaction()` を手動で呼び出してください。独自の基準に基づいて圧縮を強制するタイミングを決められます。

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

SQLite を使用したデフォルトの軽量セッション実装です:

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

### 非同期 SQLite セッション

`aiosqlite` を基盤とする SQLite 永続化が必要な場合は `AsyncSQLiteSession` を使用します。

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

複数ワーカーまたはサービス間で共有するセッションメモリには `RedisSession` を使用します。

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

SQLAlchemy 対応の任意のデータベースを使う本番対応セッションです:

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

詳細は [SQLAlchemy Sessions](sqlalchemy_session.md) を参照してください。

### Dapr セッション

すでに Dapr サイドカーを運用している場合、またはエージェントコードを変更せずに異なるステートストアバックエンド間で移行可能なセッションストレージが必要な場合は `DaprSession` を使用します。

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

注意:

-   `from_address(...)` は Dapr クライアントを作成して所有します。アプリですでに管理している場合は、 `dapr_client=...` を指定して `DaprSession(...)` を直接構築してください。
-   ストアが TTL をサポートしている場合、 `ttl=...` を渡すと基盤ステートストアが古いセッションデータを自動的に期限切れにします。
-   より強い read-after-write 保証が必要な場合は `consistency=DAPR_CONSISTENCY_STRONG` を渡してください。
-   Dapr Python SDK は HTTP サイドカーエンドポイントも確認します。ローカル開発では、 `dapr_address` で使う gRPC ポートに加え、 `--dapr-http-port 3500` で Dapr を起動してください。
-   ローカルコンポーネントとトラブルシューティングを含む完全なセットアップ手順は [`examples/memory/dapr_session_example.py`](https://github.com/openai/openai-agents-python/tree/main/examples/memory/dapr_session_example.py) を参照してください。


### Advanced SQLite セッション

会話分岐、利用分析、structured outputs クエリを備えた強化 SQLite セッションです:

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

詳細は [Advanced SQLite Sessions](advanced_sqlite_session.md) を参照してください。

### 暗号化セッション

任意のセッション実装向けの透過的な暗号化ラッパーです:

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

詳細は [Encrypted Sessions](encrypted_session.md) を参照してください。

### その他のセッションタイプ

この他にもいくつかの組み込みオプションがあります。 `examples/memory/` と `extensions/memory/` 配下のソースコードを参照してください。

## 運用パターン

### セッション ID 命名

会話を整理しやすい意味のあるセッション ID を使用してください:

-   ユーザーベース: `"user_12345"`
-   スレッドベース: `"thread_abc123"`
-   コンテキストベース: `"support_ticket_456"`

### メモリ永続化

-   一時的な会話にはインメモリ SQLite （ `SQLiteSession("session_id")` ）を使用
-   永続的な会話にはファイルベース SQLite （ `SQLiteSession("session_id", "path/to/db.sqlite")` ）を使用
-   `aiosqlite` ベース実装が必要な場合は非同期 SQLite （ `AsyncSQLiteSession("session_id", db_path="...")` ）を使用
-   共有・低レイテンシーなセッションメモリには Redis ベースセッション（ `RedisSession.from_url("session_id", url="redis://...")` ）を使用
-   SQLAlchemy が対応する既存データベースを持つ本番システムには SQLAlchemy ベースセッション（ `SQLAlchemySession("session_id", engine=engine, create_tables=True)` ）を使用
-   組み込みテレメトリ、トレーシング、データ分離を備え、 30+ のデータベースバックエンドをサポートする本番クラウドネイティブデプロイには Dapr ステートストアセッション（ `DaprSession.from_address("session_id", state_store_name="statestore", dapr_address="localhost:50001")` ）を使用
-   履歴を OpenAI Conversations API に保存したい場合は OpenAI ホスト型ストレージ（ `OpenAIConversationsSession()` ）を使用
-   任意のセッションを透過的暗号化と TTL ベース期限切れでラップするには暗号化セッション（ `EncryptedSession(session_id, underlying_session, encryption_key)` ）を使用
-   より高度なユースケースでは、他の本番システム（例: Django）向けのカスタムセッションバックエンド実装を検討

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

以下はセッションメモリが動作する完全な例です:

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

## コミュニティセッション実装

コミュニティにより追加のセッション実装が開発されています:

| Package | Description |
|---------|-------------|
| [openai-django-sessions](https://pypi.org/project/openai-django-sessions/) | Django がサポートする任意のデータベース（ PostgreSQL 、 MySQL 、 SQLite など）向けの Django ORM ベースセッション |

セッション実装を作成した場合は、ここに追加するためのドキュメント PR をぜひ提出してください。

## API リファレンス

詳細な API ドキュメントは以下を参照してください:

-   [`Session`][agents.memory.session.Session] - プロトコルインターフェース
-   [`OpenAIConversationsSession`][agents.memory.OpenAIConversationsSession] - OpenAI Conversations API 実装
-   [`OpenAIResponsesCompactionSession`][agents.memory.openai_responses_compaction_session.OpenAIResponsesCompactionSession] - Responses API 圧縮ラッパー
-   [`SQLiteSession`][agents.memory.sqlite_session.SQLiteSession] - 基本 SQLite 実装
-   [`AsyncSQLiteSession`][agents.extensions.memory.async_sqlite_session.AsyncSQLiteSession] - `aiosqlite` ベースの非同期 SQLite 実装
-   [`RedisSession`][agents.extensions.memory.redis_session.RedisSession] - Redis ベースのセッション実装
-   [`SQLAlchemySession`][agents.extensions.memory.sqlalchemy_session.SQLAlchemySession] - SQLAlchemy ベース実装
-   [`DaprSession`][agents.extensions.memory.dapr_session.DaprSession] - Dapr ステートストア実装
-   [`AdvancedSQLiteSession`][agents.extensions.memory.advanced_sqlite_session.AdvancedSQLiteSession] - 分岐と分析機能を備えた強化 SQLite
-   [`EncryptedSession`][agents.extensions.memory.encrypt_session.EncryptedSession] - 任意セッション向け暗号化ラッパー