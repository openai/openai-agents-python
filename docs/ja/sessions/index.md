---
search:
  exclude: true
---
# セッション

Agents SDK には、複数回のエージェント実行にわたって会話履歴を自動的に維持する組み込みのセッションメモリがあり、ターン間で `.to_input_list()` を手動で処理する必要がなくなります。

セッションは特定のセッションの会話履歴を保存するため、明示的にメモリを手動管理しなくても、エージェントはコンテキストを維持できます。これは、エージェントに以前のやり取りを記憶させたいチャットアプリケーションや、複数ターンの会話を構築する場合に特に便利です。

SDK にクライアント側のメモリを管理させたい場合は、セッションを使用します。同じ実行内では、セッションを実行レベルの継続オプション `conversation_id`、`previous_response_id`、`auto_previous_response_id` と組み合わせることはできません。代わりに OpenAI サーバー側で管理される継続を使用する場合は、セッションを重ねて使用せず、これらのメカニズムのいずれかを選択してください。

## クイックスタート {#quick-start}

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
result = await Runner.run(agent, "What city is the Golden Gate Bridge in?", session=session)
print(result.final_output)  # "San Francisco"

# Second turn - agent automatically remembers previous context
result = await Runner.run(agent, "What state is it in?", session=session)
print(result.final_output)  # "California"

# Also works with synchronous runner
result = Runner.run_sync(agent, "What's the population?", session=session)
print(result.final_output)  # "Approximately 39 million"
```

## 同一セッションによる中断された実行の再開 {#resuming-interrupted-runs-with-the-same-session}

承認待ちで実行が一時停止した場合は、同じセッションインスタンス、または同じセッション ID と同じ基盤ストレージバックエンドを設定した別のインスタンスを使用して再開してください。これにより、再開されたターンで、保存済みの同じ会話履歴を引き続き使用できます。

```python
result = await Runner.run(
    agent, "Delete temporary files that are no longer needed.", session=session
)

if result.interruptions:
    state = result.to_state()
    for interruption in result.interruptions:
        state.approve(interruption)
    result = await Runner.run(agent, state, session=session)
```

## セッションの基本動作 {#core-session-behavior}

セッションメモリを有効にすると、次のように動作します。

1. **各実行の前**: ランナーはセッションの会話履歴を自動的に取得し、入力項目の先頭に追加します。
2. **各実行の後**: 実行中に生成されたすべての新しい項目（ユーザー入力、アシスタントの応答、ツール呼び出しなど）が、自動的にセッションへ保存されます。
3. **コンテキストの保持**: 同じセッションを使用する後続の各実行には完全な会話履歴が含まれるため、エージェントはコンテキストを維持できます。

これにより、`.to_input_list()` を手動で呼び出し、実行間の会話状態を管理する必要がなくなります。

## 履歴と新規入力のマージ制御 {#control-how-history-and-new-input-merge}

セッションを渡すと、通常、ランナーは次の順序でモデル入力を準備します。

1. セッション履歴（`session.get_items(...)` から取得）
2. 新しいターンの入力

モデル呼び出し前のマージ処理をカスタマイズするには、[`RunConfig.session_input_callback`][agents.run.RunConfig.session_input_callback] を使用します。コールバックは次の 2 つのリストを受け取ります。

-   `history`: 取得したセッション履歴（すでに入力項目形式へ正規化済み）
-   `new_input`: 現在のターンの新しい入力項目

モデルに送信する最終的な入力項目のリストを返してください。

コールバックは両方のリストのコピーを受け取るため、安全に変更できます。返されたリストによって、そのターンのモデル入力が決まりますが、SDK が永続化するのは新しいターンに属する項目だけです。そのため、古い履歴の並べ替えやフィルタリングを行っても、古いセッション項目が新しい入力として再度保存されることはありません。

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

セッションでの項目の保存方法を変更せずに、履歴を独自に削減、並べ替え、または選択的に含める必要がある場合に使用します。モデル呼び出しの直前に、さらに最終処理を行う必要がある場合は、[エージェント実行ガイド](../running_agents.md)の [`call_model_input_filter`][agents.run.RunConfig.call_model_input_filter] を使用してください。

## 取得する履歴の制限 {#limiting-retrieved-history}

各実行前に取得する履歴の量を制御するには、[`SessionSettings`][agents.memory.SessionSettings] を使用します。

-   `SessionSettings(limit=None)`（デフォルト）: 利用可能なすべてのセッション項目を取得
-   `SessionSettings(limit=N)`: 最新の `N` 件の項目のみを取得

[`RunConfig.session_settings`][agents.run.RunConfig.session_settings] を使用して、実行ごとに適用できます。

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

セッション実装がデフォルトのセッション設定を公開している場合、`RunConfig.session_settings` 内の非 `None` 値は、その実行における対応するデフォルト値を上書きします。これは、セッションのデフォルト動作を変更せずに取得件数を制限したい、長い会話に便利です。

## メモリ操作 {#memory-operations}

### 基本操作 {#basic-operations}

セッションは、会話履歴を管理するための複数の操作をサポートしています。

```python
from agents import SQLiteSession

session = SQLiteSession("user_123", "conversations.db")

# Get all items in a session
items = await session.get_items()

# Add new items to a session
new_items = [{"role": "user", "content": "Hello"}, {"role": "assistant", "content": "Hi there!"}]
await session.add_items(new_items)

# Remove and return the most recent item
last_item = await session.pop_item()
print(last_item)  # {"role": "assistant", "content": "Hi there!"}

# Clear all items from a session
await session.clear_session()
```

### 修正における pop_item の使用 {#using-pop_item-for-corrections}

会話の最後の項目を取り消したり変更したりする場合は、`pop_item` メソッドが特に便利です。

```python
from agents import Agent, Runner, SQLiteSession

agent = Agent(name="Assistant")
session = SQLiteSession("correction_example")

# Initial conversation
result = await Runner.run(agent, "What's 2 + 2?", session=session)
print(f"Agent: {result.final_output}")

# User wants to correct their question
assistant_item = await session.pop_item()  # Remove agent's response
user_item = await session.pop_item()  # Remove user's question

# Ask a corrected question
result = await Runner.run(agent, "What's 2 + 3?", session=session)
print(f"Agent: {result.final_output}")
```

## 組み込みのセッション実装 {#built-in-session-implementations}

SDK は、さまざまなユースケースに対応する複数のセッション実装を提供しています。

### 組み込みセッション実装の選択 {#choose-a-built-in-session-implementation}

以下の詳細な例を読む前に、この表を使用して出発点を選択してください。

| セッションタイプ | 最適な用途 | 備考 |
| --- | --- | --- |
| `SQLiteSession` | ローカル開発とシンプルなアプリ | 組み込みかつ軽量で、ファイルまたはメモリをバックエンドとして使用 |
| `AsyncSQLiteSession` | `aiosqlite` を使用する非同期 SQLite | 非同期ドライバーをサポートする拡張バックエンド |
| `RedisSession` | 複数のワーカーやサービス間で共有するメモリ | 低レイテンシーの分散デプロイに適しています |
| `SQLAlchemySession` | 既存のデータベースを使用する本番アプリ | SQLAlchemy がサポートするデータベースで動作 |
| `MongoDBSession` | MongoDB をすでに使用しているアプリ、またはマルチプロセスストレージが必要なアプリ | 非同期 pymongo。順序付けのためのアトミックなシーケンスカウンター |
| `DaprSession` | Dapr サイドカーを使用するクラウドネイティブなデプロイ | 複数のステートストアに加え、TTL と整合性制御をサポート |
| `OpenAIConversationsSession` | OpenAI 内のサーバー管理ストレージ | OpenAI Conversations API をバックエンドとする履歴 |
| `OpenAIResponsesCompactionSession` | 自動圧縮が必要な長い会話 | 別のセッションバックエンドをラップ |
| `AdvancedSQLiteSession` | SQLite と分岐／分析 | より多機能です。専用ページを参照してください |
| `EncryptedSession` | 別のセッションに追加する暗号化と TTL | ラッパーです。最初に基盤バックエンドを選択してください |

一部の実装には、追加の詳細を記載した専用ページがあります。それぞれのサブセクション内にリンクがあります。

ChatKit 用の Python サーバーを実装している場合は、ChatKit のスレッドと項目を永続化するために `chatkit.store.Store` 実装を使用してください。`SQLAlchemySession` などの Agents SDK セッションは SDK 側の会話履歴を管理しますが、ChatKit のストアをそのまま置き換えるものではありません。[`chatkit-python` による ChatKit データストアの実装ガイド](https://github.com/openai/chatkit-python/blob/main/docs/guides/respond-to-user-message.md#implement-your-chatkit-data-store)を参照してください。

### OpenAI Conversations API セッション {#openai-conversations-api-sessions}

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
result = await Runner.run(agent, "What city is the Golden Gate Bridge in?", session=session)
print(result.final_output)  # "San Francisco"

# Continue the conversation
result = await Runner.run(agent, "What state is it in?", session=session)
print(result.final_output)  # "California"
```

### OpenAI Responses 圧縮セッション {#openai-responses-compaction-sessions}

Responses API（`responses.compact`）を使用して、保存済みの会話履歴を圧縮するには、`OpenAIResponsesCompactionSession` を使用します。これは基盤となるセッションをラップし、`should_trigger_compaction` に基づいて各ターンの後に自動圧縮できます。`OpenAIConversationsSession` をこれでラップしないでください。この 2 つの機能は異なる方法で履歴を管理します。

#### 一般的な使用方法（自動圧縮） {#typical-usage-auto-compaction}

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

デフォルトでは、各ターンの後に SDK が圧縮候補を確認し、しきい値を満たす場合にのみ圧縮します。

自動圧縮の実行時、SDK は `Runner.run(...)` が返るか、ストリーミングイベントのイテレーターが閉じるまでに圧縮の完了を待機します。圧縮リクエストで報告された使用量は、その実行の [`Usage`](../usage.md) の合計に加算されます。デフォルトでは、後から手動で `run_compaction()` を呼び出した場合、対象となる実行コンテキストが存在しないため、完了済み実行の使用量オブジェクトは更新されません。

`compaction_mode="previous_response_id"` は圧縮セッションが保持する Responses API のレスポンス ID を使用し、そのレスポンスチェーンを引き続き利用できる間に最適に動作します。一方、`compaction_mode="input"` は現在のセッション項目から圧縮リクエストを再構築します。これは、レスポンスチェーンを利用できない場合や、セッション内容を信頼できる唯一の情報源にしたい場合に便利です。デフォルトの `"auto"` は、利用可能な最も安全なオプションを選択します。

エージェントが `ModelSettings(store=False)` を指定して実行される場合、Responses API は後から参照できるように最後のレスポンスを保持しません。このステートレス構成では、デフォルトの `"auto"` モードは `previous_response_id` に依存せず、入力ベースの圧縮へフォールバックします。完全な例については、[`examples/memory/compaction_session_stateless_example.py`](https://github.com/openai/openai-agents-python/tree/main/examples/memory/compaction_session_stateless_example.py) を参照してください。

#### 自動圧縮によるストリーミングのブロック {#auto-compaction-can-block-streaming}

圧縮ではセッション履歴を消去して書き直すため、SDK は実行が完了したと判断する前に圧縮の完了を待機します。ストリーミングモードでは、圧縮処理が重い場合、最後の出力トークンの後も `run.stream_events()` が数秒間開いたままになることがあります。

`OpenAIResponsesCompactionSession.run_compaction()` は、消去と再書き込みの操作を、ラッパー境界で復旧可能な置換として扱います。基盤履歴が変更された後に置換が失敗またはキャンセルされた場合、ラッパーは以前の履歴の復元を試み、元の例外またはキャンセルが呼び出し元へ到達する前に、その復旧試行が完了するまで待機します。復旧中に基盤バックエンドでも障害が発生すると、以前の履歴が復元されないままになる可能性があり、SDK は復旧エラーをログに記録します。ラッパーは、リモートリクエストと置換または復旧フェーズを含む圧縮操作全体に対して、`add_items()`、`pop_item()`、`clear_session()` を直列化します。ラッパーに対する同時変更は圧縮の完了を待機し、上書きされることなく、圧縮後の履歴に適用されます。自動圧縮では、どのラッパー世代が実行に属するかも記録されます。その圧縮が開始する前に別の実行が履歴を変更した場合、SDK は新しい履歴を置き換えず、古くなった圧縮をスキップします。圧縮の実行中は、基盤セッションを直接変更しないでください。直接の変更は、こうしたラッパーの保証を迂回するためです。

低レイテンシーのストリーミングや、ターン間の素早い切り替えが必要な場合は、自動圧縮を無効にし、ターン間またはアイドル時に自分で `run_compaction()` を呼び出してください。独自の基準に基づいて、圧縮を強制するタイミングを決定できます。

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

### SQLite セッション {#sqlite-sessions}

SQLite を使用する、デフォルトの軽量なセッション実装です。

```python
from agents import SQLiteSession

# In-memory database (lost when process ends)
session = SQLiteSession("user_123")

# Persistent file-based database
session = SQLiteSession("user_123", "conversations.db")

# Use the session
result = await Runner.run(agent, "Hello", session=session)
```

### 非同期 SQLite セッション {#async-sqlite-sessions}

`aiosqlite` をバックエンドとする SQLite 永続化が必要な場合は、`AsyncSQLiteSession` を使用します。

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

### Redis セッション {#redis-sessions}

複数のワーカーやサービス間でセッションメモリを共有するには、`RedisSession` を使用します。

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

`from_url(...)` は Redis クライアントを作成し、所有します。`close()` の後、セッションは終了状態となり、それ以降のセッション操作では `RuntimeError` が発生します。`close()` は、繰り返し呼び出しても、同時に呼び出しても安全です。アプリケーションですでに Redis クライアントを管理している場合は、`redis_client=...` を指定して `RedisSession(...)` を直接構築してください。その場合、`close()` は何も行わず、クライアントの所有権とセッションの利用可能性は、どちらも呼び出し元に保持されます。

### SQLAlchemy セッション {#sqlalchemy-sessions}

SQLAlchemy がサポートする任意のデータベースを使用した、本番環境対応の Agents SDK セッション永続化です。

```python
from agents.extensions.memory import SQLAlchemySession

# Using database URL
session = SQLAlchemySession.from_url(
    "user_123", url="postgresql+asyncpg://user:pass@localhost/db", create_tables=True
)

# Using existing engine
from sqlalchemy.ext.asyncio import create_async_engine

engine = create_async_engine("postgresql+asyncpg://user:pass@localhost/db")
session = SQLAlchemySession("user_123", engine=engine, create_tables=True)
```

詳細なドキュメントについては、[SQLAlchemy セッション](sqlalchemy_session.md)を参照してください。

### Dapr セッション {#dapr-sessions}

すでに Dapr サイドカーを実行している場合や、エージェントコードを変更せずに設定済みのステートストアバックエンドを切り替えたい場合は、`DaprSession` を使用します。

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

注意事項:

-   `from_address(...)` は Dapr クライアントを作成し、所有します。アプリですでにクライアントを管理している場合は、`dapr_client=...` を指定して `DaprSession(...)` を直接構築してください。
-   コンテキストを終了するか、`close()` を呼び出すと、クライアントを所有するセッションは終了状態になります。それ以降のセッション操作では `RuntimeError` が発生しますが、`close()` は繰り返し呼び出しても、同時に呼び出しても安全です。注入されたクライアントでは、`close()` は何も行わず、セッションは引き続き利用できます。
-   基盤となるステートストアが TTL をサポートしている場合は、`ttl=...` を渡すと、セッションデータに TTL による期限切れが自動的に適用されます。
-   ステートの書き込みと削除で強い整合性を要求するには、`consistency=DAPR_CONSISTENCY_STRONG` を渡します（ストアに依存します）。通信レベルでの読み取り整合性には、`get_state` で整合性を設定できるようにする Dapr Python クライアント側のサポートが必要です。
-   Dapr Python SDK は HTTP サイドカーエンドポイントも確認します。ローカル開発では、`dapr_address` で使用する gRPC ポートに加え、`--dapr-http-port 3500` を指定して Dapr を起動してください。
-   ローカルコンポーネントやトラブルシューティングを含む完全なセットアップ手順については、[`examples/memory/dapr_session_example.py`](https://github.com/openai/openai-agents-python/tree/main/examples/memory/dapr_session_example.py) を参照してください。


### MongoDB セッション {#mongodb-sessions}

MongoDB をすでに使用しているアプリケーションや、水平方向にスケール可能なマルチプロセスのセッションストレージが必要な場合は、`MongoDBSession` を使用します。

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

注意事項:

-   `from_uri(...)` は `AsyncMongoClient` を作成して所有し、`session.close()` で閉じます。クライアントを所有するセッションは `close()` の後に終了状態となり、それ以降のセッション操作では `RuntimeError` が発生します。アプリケーションですでにクライアントを管理している場合は、`client=...` を指定して `MongoDBSession(...)` を直接構築してください。その場合、`session.close()` は何も行わず、クライアントのライフサイクルに関する責任は呼び出し元に残り、セッションも引き続き利用できます。
-   その他の変更を加えずに、`mongodb+srv://user:password@cluster.example.mongodb.net` URI を `from_uri(...)` に渡すことで、[MongoDB Atlas](https://www.mongodb.com/products/platform) に接続できます。
-   2 つのコレクションを使用し、どちらの名前も `sessions_collection=`（デフォルトは `agent_sessions`）と `messages_collection=`（デフォルトは `agent_messages`）で設定できます。インデックスは初回使用時に自動的に作成されます。空でない `add_items()` 呼び出しごとに、論理バッチドキュメントが 1 件書き込まれます。単調増加する `seq` によって、その最後の項目に基づいてバッチの順序が決まります。項目ごとに保存された従来のメッセージドキュメントも引き続き読み取れます。論理バッチは MongoDB の単一ドキュメントのサイズ上限内に収まる必要があります。サイズを超えたバッチは、部分的に保存されることなくアトミックに失敗します。
-   最初の実行前に接続を確認するには、`await session.ping()` を使用します。

### 高度な SQLite セッション {#advanced-sqlite-sessions}

会話の分岐、使用量分析、構造化クエリを備えた拡張 SQLite セッションです。

```python
from agents.extensions.memory import AdvancedSQLiteSession

# Create with advanced features
session = AdvancedSQLiteSession(
    session_id="user_123", db_path="conversations.db", create_tables=True
)

# Automatic usage tracking
result = await Runner.run(agent, "Hello", session=session)
await session.store_run_usage(result)  # Track token usage

# Conversation branching
await session.create_branch_from_turn(2)  # Branch from turn 2
```

詳細なドキュメントについては、[高度な SQLite セッション](advanced_sqlite_session.md)を参照してください。

### 暗号化セッション {#encrypted-sessions}

任意のセッション実装に対応する透過的な暗号化ラッパーです。

```python
from agents.extensions.memory import EncryptedSession, SQLAlchemySession

# Create underlying session
underlying_session = SQLAlchemySession.from_url(
    "user_123", url="sqlite+aiosqlite:///conversations.db", create_tables=True
)

# Wrap with encryption and TTL
session = EncryptedSession(
    session_id="user_123",
    underlying_session=underlying_session,
    encryption_key="your-secret-key",
    ttl=600,  # 10 minutes
)

result = await Runner.run(agent, "Hello", session=session)
```

詳細なドキュメントについては、[暗号化セッション](encrypted_session.md)を参照してください。

### その他のセッションタイプ {#other-session-types}

このほかにも組み込みオプションがいくつかあります。`examples/memory/` と `extensions/memory/` 配下のソースコードを参照してください。

## 運用パターン {#operational-patterns}

### セッション ID の命名 {#session-id-naming}

会話を整理しやすい、意味のあるセッション ID を使用してください。

-   ユーザーベース: `"user_12345"`
-   スレッドベース: `"thread_abc123"`
-   コンテキストベース: `"support_ticket_456"`

### メモリの永続化 {#memory-persistence}

-   一時的な会話には、インメモリ SQLite（`SQLiteSession("session_id")`）を使用します
-   永続的な会話には、ファイルベースの SQLite（`SQLiteSession("session_id", "path/to/db.sqlite")`）を使用します
-   `aiosqlite` ベースの実装が必要な場合は、非同期 SQLite（`AsyncSQLiteSession("session_id", db_path="...")`）を使用します
-   共有された低レイテンシーのセッションメモリには、Redis をバックエンドとするセッション（`RedisSession.from_url("session_id", url="redis://...")`）を使用します
-   SQLAlchemy がサポートする既存のデータベースを備えた本番システムには、SQLAlchemy を利用したセッション（`SQLAlchemySession("session_id", engine=engine, create_tables=True)`）を使用します
-   MongoDB をすでに使用しているアプリケーションや、水平方向にスケール可能なマルチプロセスのセッションストレージが必要な場合は、MongoDB セッション（`MongoDBSession.from_uri("session_id", uri="mongodb://localhost:27017")`）を使用します
-   組み込みのテレメトリ、トレーシング、データ分離に加え、30 種類以上のデータベースバックエンドのサポートが必要な本番環境のクラウドネイティブデプロイには、Dapr ステートストアセッション（`DaprSession.from_address("session_id", state_store_name="statestore", dapr_address="localhost:50001")`）を使用します
-   OpenAI Conversations API に履歴を保存したい場合は、OpenAI がホストするストレージ（`OpenAIConversationsSession()`）を使用します
-   任意のセッションを透過的な暗号化と TTL ベースの期限切れでラップするには、暗号化セッション（`EncryptedSession(session_id, underlying_session, encryption_key)`）を使用します
-   より高度なユースケースでは、その他の本番システム（Django など）向けにカスタムセッションバックエンドを実装することを検討してください

### SQLite ストレージの信頼境界 {#sqlite-storage-trust-boundary}

`SQLiteSession` は、アプリケーションが SQLite データベースとそのストレージを信頼していることを前提とします。このバックエンドは、保存された行を認証せず、外部からの編集、削除、並べ替え、再実行を検出しません。セッション ID は会話履歴を選択するものです。セッション ID は、ユーザーを認証したり、その履歴へのアクセスを認可したりするものではありません。

`get_items()` が履歴を読み取る際、メソッドは `session_id` に基づいて行を選択し、挿入 ID 順に並べ、各行の `message_data` を JSON としてデコードします。そのため、データベースに直接加えられた変更が、アプリケーションへ返される履歴や、次回のエージェント実行で使用される履歴に現れる可能性があります。`message_data` を JSON としてデコードできない行は、整合性エラーを発生させずにスキップされます。行の `created_at` 値を変更しても、返される項目やその順序は変わりません。

アプリケーションは、各セッションへのアクセスを認可し、データベースファイル、その格納先ストレージ、およびバックアップへのアクセスを制限する必要があります。共有ストレージまたは信頼できないストレージで改ざん検出が必要なデプロイでは、必要な整合性保証を実施するストレージ制御またはカスタムの [`Session`][agents.memory.session.Session] 実装を使用してください。[`EncryptedSession`](encrypted_session.md) ラッパーは暗号化を提供しますが、会話履歴の完全性や順序を検証せず、暗号化されていない項目も受け入れます。このラッパーは、こうした整合性制御の代替にはなりません。

### 複数のセッション {#multiple-sessions}

```python
from agents import Agent, Runner, SQLiteSession

agent = Agent(name="Assistant")

# Different sessions maintain separate conversation histories
session_1 = SQLiteSession("user_123", "conversations.db")
session_2 = SQLiteSession("user_456", "conversations.db")

result1 = await Runner.run(agent, "Help me with my account", session=session_1)
result2 = await Runner.run(agent, "What are my charges?", session=session_2)
```

### セッションの共有 {#session-sharing}

```python
# Different agents can share the same session
support_agent = Agent(name="Support")
billing_agent = Agent(name="Billing")
session = SQLiteSession("user_123")

# Both agents will see the same conversation history
result1 = await Runner.run(support_agent, "Help me with my account", session=session)
result2 = await Runner.run(billing_agent, "What are my charges?", session=session)
```

## 完全な例 {#complete-example}

セッションメモリの動作を示す完全な例を次に示します。

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
    result = await Runner.run(agent, "What city is the Golden Gate Bridge in?", session=session)
    print(f"Assistant: {result.final_output}")
    print()

    # Second turn - the agent will remember the previous conversation
    print("Second turn:")
    print("User: What state is it in?")
    result = await Runner.run(agent, "What state is it in?", session=session)
    print(f"Assistant: {result.final_output}")
    print()

    # Third turn - continuing the conversation
    print("Third turn:")
    print("User: What's the population of that state?")
    result = await Runner.run(agent, "What's the population of that state?", session=session)
    print(f"Assistant: {result.final_output}")
    print()

    print("=== Conversation Complete ===")
    print("Notice how the agent remembered the context from previous turns!")
    print("Sessions automatically handles conversation history.")


if __name__ == "__main__":
    asyncio.run(main())
```

## カスタムセッション実装 {#custom-session-implementations}

[`Session`][agents.memory.session.Session] プロトコルに構造的に従うクラスを作成することで、独自のセッションメモリを実装できます。`SessionABC` を継承する必要はありません。`session_id` と `session_settings` を定義し、4 つの履歴メソッドを直接実装してください。

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
result = await Runner.run(agent, "Hello", session=MyCustomSession("my_session"))
```

### カスタムセッションからの実行コンテキストへのアクセス {#accessing-run-context-from-a-custom-session}

Agents SDK は、テナントルーティング、認可、その他のアプリ固有のストレージ判断のために、アクティブな [`RunContextWrapper`][agents.run_context.RunContextWrapper] をカスタムセッションへ渡すことができます。Agents SDK がラッパーを渡せるようにするには、4 つすべての履歴メソッドに、明示的に命名され、キーワード互換性のある `wrapper` パラメーターを追加します。

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

Agents SDK は、`get_items`、`add_items`、`pop_item`、`clear_session` のすべてで `wrapper` が宣言されている場合にのみ、この統合を有効にします。汎用の `**kwargs` パラメーターでは、このシグネチャチェックを満たしません。`wrapper` を省略している既存のセッション実装では、リリース済みの呼び出し形式が維持され、変更なしで引き続き動作します。

## コミュニティのセッション実装 {#community-session-implementations}

コミュニティによって、追加のセッション実装が開発されています。

| パッケージ | 説明 |
|---------|-------------|
| [openai-django-sessions](https://pypi.org/project/openai-django-sessions/) | Django がサポートする任意のデータベース（PostgreSQL、MySQL、SQLite など）に対応する、Django ORM ベースのセッション |

セッション実装を構築した場合は、ここに追加するためのドキュメント PR をぜひ送信してください。

## API リファレンス {#api-reference}

詳細な API ドキュメントについては、以下を参照してください。

-   [`Session`][agents.memory.session.Session] - プロトコルインターフェース
-   [`OpenAIConversationsSession`][agents.memory.OpenAIConversationsSession] - OpenAI Conversations API 実装
-   [`OpenAIResponsesCompactionSession`][agents.memory.openai_responses_compaction_session.OpenAIResponsesCompactionSession] - Responses API 圧縮ラッパー
-   [`SQLiteSession`][agents.memory.sqlite_session.SQLiteSession] - 基本的な SQLite 実装
-   [`AsyncSQLiteSession`][agents.extensions.memory.async_sqlite_session.AsyncSQLiteSession] - `aiosqlite` ベースの非同期 SQLite 実装
-   [`RedisSession`][agents.extensions.memory.redis_session.RedisSession] - Redis をバックエンドとするセッション実装
-   [`SQLAlchemySession`][agents.extensions.memory.sqlalchemy_session.SQLAlchemySession] - SQLAlchemy を利用した実装
-   [`MongoDBSession`][agents.extensions.memory.mongodb_session.MongoDBSession] - MongoDB をバックエンドとするセッション実装
-   [`DaprSession`][agents.extensions.memory.dapr_session.DaprSession] - Dapr ステートストア実装
-   [`AdvancedSQLiteSession`][agents.extensions.memory.advanced_sqlite_session.AdvancedSQLiteSession] - 分岐と分析を備えた拡張 SQLite
-   [`EncryptedSession`][agents.extensions.memory.encrypt_session.EncryptedSession] - 任意のセッションに対応する暗号化ラッパー