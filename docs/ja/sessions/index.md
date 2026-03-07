---
search:
  exclude: true
---
# セッション

Agents SDK は、複数のエージェント実行にわたって会話履歴を自動的に維持する組み込みのセッションメモリを提供し、ターン間で `.to_input_list()` を手動で扱う必要をなくします。

Sessions は特定のセッションの会話履歴を保存し、明示的な手動メモリ管理を必要とせずにエージェントがコンテキストを維持できるようにします。これは、エージェントに以前のやり取りを記憶させたいチャットアプリケーションや複数ターンの会話を構築する際に特に有用です。

SDK にクライアント側メモリを管理させたい場合は sessions を使用します。sessions は同一実行内で `conversation_id`、`previous_response_id`、`auto_previous_response_id` と併用できません。代わりに OpenAI のサーバー管理による継続を利用したい場合は、session を重ねるのではなくそれらのメカニズムのいずれかを選択してください。

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

## 同一セッションでの中断実行の再開

実行が承認待ちで一時停止した場合は、同じ session インスタンス（または同じバックエンドストアを指す別の session インスタンス）で再開し、再開ターンが同じ保存済み会話履歴を継続するようにします。

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

1. **各実行前**: runner はセッションの会話履歴を自動取得し、入力アイテムの先頭に追加します。
2. **各実行後**: 実行中に生成されたすべての新規アイテム（ユーザー入力、アシスタント応答、ツール呼び出しなど）が自動的にセッションへ保存されます。
3. **コンテキスト保持**: 同じセッションでの後続実行には毎回完全な会話履歴が含まれ、エージェントがコンテキストを維持できます。

これにより、`.to_input_list()` を手動で呼び出して実行間の会話状態を管理する必要がなくなります。

## 履歴と新規入力のマージ制御

session を渡すと、runner は通常次のようにモデル入力を準備します:

1. セッション履歴（`session.get_items(...)` から取得）
2. 新しいターン入力

モデル呼び出し前のこのマージ手順をカスタマイズするには [`RunConfig.session_input_callback`][agents.run.RunConfig.session_input_callback] を使用します。コールバックは 2 つのリストを受け取ります:

-   `history`: 取得されたセッション履歴（すでに input-item 形式に正規化済み）
-   `new_input`: 現在ターンの新規入力アイテム

モデルに送信すべき最終的な入力アイテムのリストを返してください。

コールバックは両方のリストのコピーを受け取るため、安全に変更できます。返されたリストはそのターンのモデル入力を制御しますが、SDK は引き続き新しいターンに属するアイテムのみを永続化します。したがって、古い履歴を並べ替えたりフィルタしたりしても、古いセッションアイテムが新規入力として再保存されることはありません。

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

これは、session の保存方法を変えずに履歴のカスタム剪定、並べ替え、または選択的な取り込みが必要な場合に使用します。モデル呼び出し直前のさらに後段の最終パスが必要な場合は、[running agents ガイド](../running_agents.md) の [`call_model_input_filter`][agents.run.RunConfig.call_model_input_filter] を使用してください。

## 取得履歴の制限

各実行前にどれだけ履歴を取得するかを制御するには [`SessionSettings`][agents.memory.SessionSettings] を使用します。

-   `SessionSettings(limit=None)`（デフォルト）: 利用可能なすべてのセッションアイテムを取得
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

session 実装がデフォルトの session settings を公開している場合、`RunConfig.session_settings` はその実行で `None` 以外の値を上書きします。これは、session のデフォルト動作を変えずに取得サイズに上限を設けたい長い会話で有用です。

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

### 修正での pop_item の使用

`pop_item` メソッドは、会話内の最後のアイテムを取り消したり変更したりしたい場合に特に有用です:

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

SDK は用途ごとに複数のセッション実装を提供しています:

### 組み込みセッション実装の選択

以下の詳細な例を読む前に、この表で開始点を選んでください。

| Session type | Best for | Notes |
| --- | --- | --- |
| `SQLiteSession` | ローカル開発およびシンプルなアプリ | 組み込み、軽量、ファイルバックエンドまたはインメモリ |
| `AsyncSQLiteSession` | `aiosqlite` を使う非同期 SQLite | 非同期ドライバー対応の拡張バックエンド |
| `RedisSession` | ワーカー/サービス間で共有するメモリ | 低遅延の分散デプロイに適しています |
| `SQLAlchemySession` | 既存データベースを使う本番アプリ | SQLAlchemy 対応データベースで動作 |
| `DaprSession` | Dapr サイドカーを使うクラウドネイティブデプロイ | 複数のステートストアに加え TTL と整合性制御をサポート |
| `OpenAIConversationsSession` | OpenAI 内でのサーバー管理ストレージ | OpenAI Conversations API ベースの履歴 |
| `OpenAIResponsesCompactionSession` | 自動圧縮を伴う長い会話 | 別の session バックエンドをラップ |
| `AdvancedSQLiteSession` | 分岐/分析対応の SQLite | 機能が多い実装。専用ページを参照 |
| `EncryptedSession` | 別 session 上での暗号化 + TTL | ラッパー。先に基盤バックエンドを選択 |

一部の実装には追加詳細を記載した専用ページがあり、各サブセクション内でリンクされています。

ChatKit 向けに Python サーバーを実装する場合は、ChatKit のスレッドおよびアイテム永続化に `chatkit.store.Store` 実装を使用してください。`SQLAlchemySession` などの Agents SDK セッションは SDK 側の会話履歴を管理しますが、ChatKit のストアの置き換えにはなりません。[ChatKit データストア実装に関する `chatkit-python` ガイド](https://github.com/openai/chatkit-python/blob/main/docs/guides/respond-to-user-message.md#implement-your-chatkit-data-store)を参照してください。

### OpenAI Conversations API セッション

`OpenAIConversationsSession` を通じて [OpenAI's Conversations API](https://platform.openai.com/docs/api-reference/conversations) を使用します。

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

保存された会話履歴を Responses API（`responses.compact`）で圧縮するには `OpenAIResponsesCompactionSession` を使用します。これは基盤 session をラップし、`should_trigger_compaction` に基づいて各ターン後に自動圧縮できます。`OpenAIConversationsSession` をこれでラップしないでください。これら 2 つの機能は異なる方法で履歴を管理します。

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

`compaction_mode="previous_response_id"` は、Responses API の response ID ですでにターンを連結している場合に最適です。`compaction_mode="input"` は代わりに現在のセッションアイテムから圧縮リクエストを再構築します。これは response チェーンが利用できない場合や、session 内容を信頼できる唯一の情報源にしたい場合に有用です。デフォルトの `"auto"` は利用可能な中で最も安全なオプションを選択します。

#### 自動圧縮はストリーミングをブロックする可能性

圧縮ではセッション履歴を消去して再書き込みするため、SDK は圧縮完了まで実行を完了扱いにしません。ストリーミングモードでは、圧縮が重い場合、最後の出力トークンの後も `run.stream_events()` が数秒間開いたままになることがあります。

低遅延ストリーミングや高速なターン交代が必要な場合は、自動圧縮を無効にし、ターン間（またはアイドル時間）に `run_compaction()` を自分で呼び出してください。独自の基準に基づいて圧縮を強制するタイミングを決められます。

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

`aiosqlite` をバックエンドにした SQLite 永続化が必要な場合は `AsyncSQLiteSession` を使用します。

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

複数ワーカーまたはサービス間で共有セッションメモリを使うには `RedisSession` を使用します。

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

SQLAlchemy 対応の任意のデータベースを使用した、本番対応の Agents SDK セッション永続化です:

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

すでに Dapr サイドカーを実行している場合、またはエージェントコードを変更せずに異なるステートストアバックエンド間を移行できるセッションストレージが必要な場合は `DaprSession` を使用します。

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

-   `from_address(...)` は Dapr クライアントを作成して管理します。アプリですでに管理している場合は、`dapr_client=...` を指定して `DaprSession(...)` を直接構築してください。
-   `ttl=...` を渡すと、ストアが TTL をサポートしている場合、基盤ステートストアが古いセッションデータを自動期限切れにできます。
-   より強い read-after-write 保証が必要な場合は `consistency=DAPR_CONSISTENCY_STRONG` を渡してください。
-   Dapr Python SDK は HTTP サイドカーエンドポイントも確認します。ローカル開発では、`dapr_address` で使用する gRPC ポートに加えて `--dapr-http-port 3500` で Dapr を起動してください。
-   ローカルコンポーネントやトラブルシューティングを含む完全なセットアップ手順は [`examples/memory/dapr_session_example.py`](https://github.com/openai/openai-agents-python/tree/main/examples/memory/dapr_session_example.py) を参照してください。


### Advanced SQLite セッション

会話分岐、利用分析、構造化クエリを備えた拡張 SQLite セッションです:

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

あらゆるセッション実装向けの透過的暗号化ラッパーです:

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

ほかにも組み込みオプションがいくつかあります。`examples/memory/` および `extensions/memory/` 配下のソースコードを参照してください。

## 運用パターン

### セッション ID 命名

会話を整理しやすい、意味のあるセッション ID を使用してください:

-   ユーザーベース: `"user_12345"`
-   スレッドベース: `"thread_abc123"`
-   コンテキストベース: `"support_ticket_456"`

### メモリ永続化

-   一時的な会話にはインメモリ SQLite（`SQLiteSession("session_id")`）を使用
-   永続的な会話にはファイルベース SQLite（`SQLiteSession("session_id", "path/to/db.sqlite")`）を使用
-   `aiosqlite` ベース実装が必要な場合は非同期 SQLite（`AsyncSQLiteSession("session_id", db_path="...")`）を使用
-   共有・低遅延のセッションメモリには Redis バックエンドセッション（`RedisSession.from_url("session_id", url="redis://...")`）を使用
-   SQLAlchemy がサポートする既存データベースを使う本番システムには SQLAlchemy ベースセッション（`SQLAlchemySession("session_id", engine=engine, create_tables=True)`) を使用
-   組み込みテレメトリー、トレーシング、データ分離を備えた 30+ のデータベースバックエンド対応の本番クラウドネイティブデプロイには Dapr ステートストアセッション（`DaprSession.from_address("session_id", state_store_name="statestore", dapr_address="localhost:50001")`）を使用
-   OpenAI Conversations API に履歴を保存したい場合は OpenAI ホスト型ストレージ（`OpenAIConversationsSession()`）を使用
-   透過的暗号化と TTL ベース有効期限を任意の session に追加するには暗号化セッション（`EncryptedSession(session_id, underlying_session, encryption_key)`）を使用
-   より高度なユースケースでは、他の本番システム（例: Django）向けのカスタム session バックエンド実装も検討してください

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

セッションメモリの動作を示す完全な例です:

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
| [openai-django-sessions](https://pypi.org/project/openai-django-sessions/) | Django がサポートする任意のデータベース（PostgreSQL、MySQL、SQLite など）向けの Django ORM ベースセッション |

セッション実装を作成した場合は、ここに追加するためのドキュメント PR をぜひ提出してください。

## API リファレンス

詳細な API ドキュメントは以下を参照してください:

-   [`Session`][agents.memory.session.Session] - プロトコルインターフェース
-   [`OpenAIConversationsSession`][agents.memory.OpenAIConversationsSession] - OpenAI Conversations API 実装
-   [`OpenAIResponsesCompactionSession`][agents.memory.openai_responses_compaction_session.OpenAIResponsesCompactionSession] - Responses API 圧縮ラッパー
-   [`SQLiteSession`][agents.memory.sqlite_session.SQLiteSession] - 基本 SQLite 実装
-   [`AsyncSQLiteSession`][agents.extensions.memory.async_sqlite_session.AsyncSQLiteSession] - `aiosqlite` ベースの非同期 SQLite 実装
-   [`RedisSession`][agents.extensions.memory.redis_session.RedisSession] - Redis バックエンドセッション実装
-   [`SQLAlchemySession`][agents.extensions.memory.sqlalchemy_session.SQLAlchemySession] - SQLAlchemy ベース実装
-   [`DaprSession`][agents.extensions.memory.dapr_session.DaprSession] - Dapr ステートストア実装
-   [`AdvancedSQLiteSession`][agents.extensions.memory.advanced_sqlite_session.AdvancedSQLiteSession] - 分岐と分析を備えた拡張 SQLite
-   [`EncryptedSession`][agents.extensions.memory.encrypt_session.EncryptedSession] - あらゆる session 向け暗号化ラッパー