---
search:
  exclude: true
---
# セッション

Agents SDK は、複数のエージェント実行にまたがって会話履歴を自動的に維持する組み込みのセッションメモリを提供し、ターン間で `.to_input_list()` を手動で扱う必要をなくします。

セッションは特定のセッションの会話履歴を保存し、明示的な手動メモリ管理を必要とせずにエージェントがコンテキストを維持できるようにします。これは、チャットアプリケーションや、エージェントに以前のやり取りを記憶させたいマルチターン会話を構築する場合に特に有用です。

SDK にクライアント側メモリを管理させたい場合は、セッションを使用します。セッションは同じ実行内で `conversation_id`、`previous_response_id`、または `auto_previous_response_id` と組み合わせることはできません。代わりに OpenAI サーバー管理の継続を使いたい場合は、セッションを重ねるのではなく、それらの仕組みのいずれかを選択してください。

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

## 同じセッションでの中断された実行の再開

実行が承認待ちで一時停止した場合は、再開後のターンが同じ保存済み会話履歴を引き継ぐように、同じセッションインスタンス（または同じバッキングストアを指す別のセッションインスタンス）で再開してください。

```python
result = await Runner.run(agent, "Delete temporary files that are no longer needed.", session=session)

if result.interruptions:
    state = result.to_state()
    for interruption in result.interruptions:
        state.approve(interruption)
    result = await Runner.run(agent, state, session=session)
```

## コアセッション動作

セッションメモリが有効な場合:

1. **各実行前**: runner はセッションの会話履歴を自動的に取得し、それを入力アイテムの先頭に追加します。
2. **各実行後**: 実行中に生成されたすべての新しいアイテム（ユーザー入力、assistant 応答、ツール呼び出しなど）がセッションに自動的に保存されます。
3. **コンテキスト保持**: 同じセッションでの後続の各実行には完全な会話履歴が含まれるため、エージェントはコンテキストを維持できます。

これにより、`.to_input_list()` を手動で呼び出し、実行間の会話状態を管理する必要がなくなります。

## 履歴と新しい入力のマージ制御

セッションを渡すと、runner は通常、モデル入力を次のように準備します。

1. セッション履歴（`session.get_items(...)` から取得）
2. 新しいターンの入力

モデル呼び出しの前にそのマージ手順をカスタマイズするには、[`RunConfig.session_input_callback`][agents.run.RunConfig.session_input_callback] を使用します。コールバックは 2 つのリストを受け取ります。

-   `history`: 取得されたセッション履歴（すでに入力アイテム形式に正規化済み）
-   `new_input`: 現在のターンの新しい入力アイテム

モデルに送信する最終的な入力アイテムのリストを返します。

コールバックは両方のリストのコピーを受け取るため、安全に変更できます。返されたリストはそのターンのモデル入力を制御しますが、SDK が永続化するのは新しいターンに属するアイテムのみです。そのため、古い履歴を並べ替えたりフィルターしたりしても、古いセッションアイテムが新しい入力として再度保存されることはありません。

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

セッションがアイテムを保存する方法を変えずに、カスタムの刈り込み、並べ替え、または履歴の選択的な取り込みが必要な場合に使用します。モデル呼び出しの直前にさらに最終パスが必要な場合は、[エージェント実行ガイド](../running_agents.md)の [`call_model_input_filter`][agents.run.RunConfig.call_model_input_filter] を使用してください。

## 取得履歴の制限

各実行前に取得する履歴量を制御するには、[`SessionSettings`][agents.memory.SessionSettings] を使用します。

-   `SessionSettings(limit=None)`（デフォルト）: 利用可能なすべてのセッションアイテムを取得します
-   `SessionSettings(limit=N)`: 直近の `N` 個のアイテムのみを取得します

これは [`RunConfig.session_settings`][agents.run.RunConfig.session_settings] を通じて実行ごとに適用できます。

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

セッション実装がデフォルトのセッション設定を公開している場合、`RunConfig.session_settings` はその実行に対して `None` ではない値を上書きします。これは、セッションのデフォルト動作を変更せずに取得サイズに上限を設けたい長い会話で有用です。

## メモリ操作

### 基本操作

セッションは会話履歴を管理するためのいくつかの操作をサポートしています。

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

`pop_item` メソッドは、会話内の最後のアイテムを取り消したり変更したりしたい場合に特に有用です。

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

SDK は、さまざまなユースケース向けに複数のセッション実装を提供しています。

### 組み込みセッション実装の選択

以下の詳細な例を読む前に、開始点を選ぶためにこの表を使用してください。

| セッションタイプ | 最適な用途 | 備考 |
| --- | --- | --- |
| `SQLiteSession` | ローカル開発とシンプルなアプリ | 組み込み、軽量、ファイルバックまたはインメモリ |
| `AsyncSQLiteSession` | `aiosqlite` を使う非同期 SQLite | 非同期ドライバーサポートを備えた拡張バックエンド |
| `RedisSession` | ワーカー/サービス間で共有されるメモリ | 低レイテンシの分散デプロイに適しています |
| `SQLAlchemySession` | 既存データベースを持つ本番アプリ | SQLAlchemy がサポートするデータベースで動作します |
| `MongoDBSession` | すでに MongoDB を使用している、またはマルチプロセスストレージが必要なアプリ | 非同期 pymongo。順序付け用のアトミックシーケンスカウンター |
| `DaprSession` | Dapr サイドカーを使用するクラウドネイティブデプロイ | 複数の状態ストアに加えて TTL と整合性制御をサポート |
| `OpenAIConversationsSession` | OpenAI 内のサーバー管理ストレージ | OpenAI Conversations API ベースの履歴 |
| `OpenAIResponsesCompactionSession` | 自動コンパクションを伴う長い会話 | 別のセッションバックエンドのラッパー |
| `AdvancedSQLiteSession` | SQLite と分岐/分析 | 機能セットはより重めです。専用ページを参照してください |
| `EncryptedSession` | 別のセッション上の暗号化 + TTL | ラッパーです。まず基盤となるバックエンドを選択してください |

一部の実装には追加の詳細を含む専用ページがあり、それぞれの小節内でインラインにリンクされています。

ChatKit 用の Python サーバーを実装している場合は、ChatKit のスレッドおよびアイテム永続化に `chatkit.store.Store` 実装を使用してください。`SQLAlchemySession` などの Agents SDK セッションは SDK 側の会話履歴を管理しますが、ChatKit のストアのドロップイン置換ではありません。[ChatKit データストアの実装に関する `chatkit-python` ガイド](https://github.com/openai/chatkit-python/blob/main/docs/guides/respond-to-user-message.md#implement-your-chatkit-data-store)を参照してください。

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

### OpenAI Responses コンパクションセッション

Responses API（`responses.compact`）で保存済み会話履歴をコンパクト化するには、`OpenAIResponsesCompactionSession` を使用します。これは基盤となるセッションをラップし、`should_trigger_compaction` に基づいて各ターン後に自動的にコンパクションできます。`OpenAIConversationsSession` をこれでラップしないでください。この 2 つの機能は異なる方法で履歴を管理します。

#### 典型的な使用法（自動コンパクション）

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

デフォルトでは、候補しきい値に達すると、各ターン後にコンパクションが実行されます。

`compaction_mode="previous_response_id"` は、Responses API の応答 ID を使ってすでにターンをチェーンしている場合に最適です。`compaction_mode="input"` は代わりに現在のセッションアイテムからコンパクションリクエストを再構築します。これは応答チェーンが利用できない場合や、セッション内容を信頼できる情報源にしたい場合に有用です。デフォルトの `"auto"` は、利用可能な中で最も安全なオプションを選択します。

エージェントが `ModelSettings(store=False)` で実行されている場合、Responses API は後で参照するために最後の応答を保持しません。このステートレスな設定では、デフォルトの `"auto"` モードは `previous_response_id` に依存する代わりに、入力ベースのコンパクションにフォールバックします。完全な例については [`examples/memory/compaction_session_stateless_example.py`](https://github.com/openai/openai-agents-python/tree/main/examples/memory/compaction_session_stateless_example.py) を参照してください。

#### 自動コンパクションがストリーミングをブロックする可能性

コンパクションはセッション履歴をクリアして書き直すため、SDK は実行が完了したとみなす前にコンパクションの終了を待ちます。ストリーミングモードでは、コンパクションが重い場合、最後の出力トークンの後も `run.stream_events()` が数秒間開いたままになる可能性があります。

低レイテンシのストリーミングや高速なターン処理が必要な場合は、自動コンパクションを無効にし、ターン間（またはアイドル時間中）に自分で `run_compaction()` を呼び出してください。独自の基準に基づいて、いつ強制的にコンパクションするかを決定できます。

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

SQLite を使用するデフォルトの軽量セッション実装です。

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

`aiosqlite` による SQLite 永続化を使いたい場合は、`AsyncSQLiteSession` を使用します。

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

複数のワーカーまたはサービス間で共有セッションメモリを使うには、`RedisSession` を使用します。

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

SQLAlchemy がサポートする任意のデータベースを使用した、本番対応の Agents SDK セッション永続化です。

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

詳細なドキュメントについては [SQLAlchemy セッション](sqlalchemy_session.md)を参照してください。

### Dapr セッション

すでに Dapr サイドカーを実行している場合、またはエージェントコードを変更せずに異なる状態ストアバックエンド間を移動できるセッションストレージが必要な場合は、`DaprSession` を使用します。

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

備考:

-   `from_address(...)` は Dapr クライアントを作成し、その所有権を持ちます。アプリがすでにクライアントを管理している場合は、`dapr_client=...` を指定して `DaprSession(...)` を直接構築してください。
-   バッキング状態ストアが TTL をサポートしている場合に、古いセッションデータを自動的に期限切れにするには、`ttl=...` を渡します。
-   より強い read-after-write 保証が必要な場合は、`consistency=DAPR_CONSISTENCY_STRONG` を渡します。
-   Dapr Python SDK は HTTP サイドカーエンドポイントも確認します。ローカル開発では、`dapr_address` で使用する gRPC ポートに加えて、`--dapr-http-port 3500` でも Dapr を起動してください。
-   ローカルコンポーネントやトラブルシューティングを含む完全なセットアップ手順については、[`examples/memory/dapr_session_example.py`](https://github.com/openai/openai-agents-python/tree/main/examples/memory/dapr_session_example.py) を参照してください。


### MongoDB セッション

すでに MongoDB を使用しているアプリケーションや、水平スケーラブルなマルチプロセスセッションストレージが必要なアプリケーションでは、`MongoDBSession` を使用します。

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

備考:

-   `from_uri(...)` は `AsyncMongoClient` を作成して所有し、`session.close()` 時に閉じます。アプリケーションがすでにクライアントを管理している場合は、`client=...` を指定して `MongoDBSession(...)` を直接構築してください。その場合、`session.close()` は no-op になり、ライフサイクルは呼び出し元に残ります。
-   そのほかの変更なしに、`mongodb+srv://user:password@cluster.example.mongodb.net` URI を `from_uri(...)` に渡すことで、[MongoDB Atlas](https://www.mongodb.com/products/platform) に接続できます。
-   2 つのコレクションが使用され、どちらの名前も `sessions_collection=`（デフォルト `agent_sessions`）および `messages_collection=`（デフォルト `agent_messages`）で構成できます。インデックスは初回使用時に自動的に作成されます。各メッセージドキュメントには、同時書き込み元やプロセス間で順序を保持する単調増加の `seq` カウンターが含まれます。
-   最初の実行前に接続性を確認するには、`await session.ping()` を使用します。

### Advanced SQLite セッション

会話の分岐、利用状況分析、構造化クエリを備えた拡張 SQLite セッションです。

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

詳細なドキュメントについては [Advanced SQLite セッション](advanced_sqlite_session.md)を参照してください。

### 暗号化セッション

任意のセッション実装向けの透過的な暗号化ラッパーです。

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

詳細なドキュメントについては [暗号化セッション](encrypted_session.md)を参照してください。

### その他のセッションタイプ

組み込みオプションはさらにいくつかあります。`examples/memory/` および `extensions/memory/` 配下のソースコードを参照してください。

## 運用パターン

### セッション ID の命名

会話を整理しやすくする意味のあるセッション ID を使用してください。

-   ユーザーベース: `"user_12345"`
-   スレッドベース: `"thread_abc123"`
-   コンテキストベース: `"support_ticket_456"`

### メモリ永続化

-   一時的な会話にはインメモリ SQLite（`SQLiteSession("session_id")`）を使用します
-   永続的な会話にはファイルベース SQLite（`SQLiteSession("session_id", "path/to/db.sqlite")`）を使用します
-   `aiosqlite` ベースの実装が必要な場合は非同期 SQLite（`AsyncSQLiteSession("session_id", db_path="...")`）を使用します
-   共有された低レイテンシのセッションメモリには Redis バックのセッション（`RedisSession.from_url("session_id", url="redis://...")`）を使用します
-   SQLAlchemy がサポートする既存データベースを持つ本番システムには、SQLAlchemy によるセッション（`SQLAlchemySession("session_id", engine=engine, create_tables=True)`）を使用します
-   すでに MongoDB を使用している、またはマルチプロセスで水平スケーラブルなセッションストレージが必要なアプリケーションには、MongoDB セッション（`MongoDBSession.from_uri("session_id", uri="mongodb://localhost:27017")`）を使用します
-   組み込みのテレメトリ、トレーシング、データ分離を備えた 30 以上のデータベースバックエンドに対応する本番クラウドネイティブデプロイには、Dapr 状態ストアセッション（`DaprSession.from_address("session_id", state_store_name="statestore", dapr_address="localhost:50001")`）を使用します
-   履歴を OpenAI Conversations API に保存したい場合は、OpenAI がホストするストレージ（`OpenAIConversationsSession()`）を使用します
-   任意のセッションを透過的な暗号化と TTL ベースの有効期限切れでラップするには、暗号化セッション（`EncryptedSession(session_id, underlying_session, encryption_key)`）を使用します
-   より高度なユースケースでは、他の本番システム（たとえば Django）向けのカスタムセッションバックエンドの実装を検討してください

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

セッションメモリの動作を示す完全な例を以下に示します。

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

[`Session`][agents.memory.session.Session] プロトコルに従うクラスを作成することで、独自のセッションメモリを実装できます。

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

コミュニティは追加のセッション実装を開発しています。

| パッケージ | 説明 |
|---------|-------------|
| [openai-django-sessions](https://pypi.org/project/openai-django-sessions/) | Django がサポートする任意のデータベース（PostgreSQL、MySQL、SQLite など）向けの Django ORM ベースのセッション |

セッション実装を構築した場合は、ここに追加するためのドキュメント PR をぜひ提出してください。

## API リファレンス

詳細な API ドキュメントについては、以下を参照してください。

-   [`Session`][agents.memory.session.Session] - プロトコルインターフェイス
-   [`OpenAIConversationsSession`][agents.memory.OpenAIConversationsSession] - OpenAI Conversations API 実装
-   [`OpenAIResponsesCompactionSession`][agents.memory.openai_responses_compaction_session.OpenAIResponsesCompactionSession] - Responses API コンパクションラッパー
-   [`SQLiteSession`][agents.memory.sqlite_session.SQLiteSession] - 基本的な SQLite 実装
-   [`AsyncSQLiteSession`][agents.extensions.memory.async_sqlite_session.AsyncSQLiteSession] - `aiosqlite` に基づく非同期 SQLite 実装
-   [`RedisSession`][agents.extensions.memory.redis_session.RedisSession] - Redis バックのセッション実装
-   [`SQLAlchemySession`][agents.extensions.memory.sqlalchemy_session.SQLAlchemySession] - SQLAlchemy による実装
-   [`MongoDBSession`][agents.extensions.memory.mongodb_session.MongoDBSession] - MongoDB バックのセッション実装
-   [`DaprSession`][agents.extensions.memory.dapr_session.DaprSession] - Dapr 状態ストア実装
-   [`AdvancedSQLiteSession`][agents.extensions.memory.advanced_sqlite_session.AdvancedSQLiteSession] - 分岐と分析を備えた拡張 SQLite
-   [`EncryptedSession`][agents.extensions.memory.encrypt_session.EncryptedSession] - 任意のセッション向けの暗号化ラッパー