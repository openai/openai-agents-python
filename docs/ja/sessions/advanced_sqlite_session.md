---
search:
  exclude: true
---
# 高度な SQLite セッション

`AdvancedSQLiteSession` は、会話の分岐、詳細な使用状況の分析、構造化された会話クエリなどの高度な会話管理機能を提供する、基本の `SQLiteSession` を強化したバージョンです。

## 機能

- **会話の分岐**: 任意の ユーザー メッセージから代替の会話パスを作成
- **使用状況の追跡**: 各ターンごとのトークン使用量を詳細に分析し、完全な JSON 内訳を提供
- **構造化クエリ**: ターン単位の会話取得、ツール使用統計など
- **ブランチ管理**: 独立したブランチの切り替えと管理
- **メッセージ構造のメタデータ**: メッセージタイプ、ツール使用状況、会話の流れを追跡

## クイックスタート

```python
from agents import Agent, Runner
from agents.extensions.memory import AdvancedSQLiteSession

# Create agent
agent = Agent(
    name="Assistant",
    instructions="Reply very concisely.",
)

# Create an advanced session
session = AdvancedSQLiteSession(
    session_id="conversation_123",
    db_path="conversations.db",
    create_tables=True
)

# First conversation turn
result = await Runner.run(
    agent,
    "What city is the Golden Gate Bridge in?",
    session=session
)
print(result.final_output)  # "San Francisco"

# IMPORTANT: Store usage data
await session.store_run_usage(result)

# Continue conversation
result = await Runner.run(
    agent,
    "What state is it in?",
    session=session
)
print(result.final_output)  # "California"
await session.store_run_usage(result)
```

## 初期化

```python
from agents.extensions.memory import AdvancedSQLiteSession

# Basic initialization
session = AdvancedSQLiteSession(
    session_id="my_conversation",
    create_tables=True  # Auto-create advanced tables
)

# With persistent storage
session = AdvancedSQLiteSession(
    session_id="user_123",
    db_path="path/to/conversations.db",
    create_tables=True
)

# With custom logger
import logging
logger = logging.getLogger("my_app")
session = AdvancedSQLiteSession(
    session_id="session_456",
    create_tables=True,
    logger=logger
)
```

### パラメーター

- `session_id` (str): 会話セッションの一意の識別子
- `db_path` (str | Path): SQLite データベースファイルへのパス。メモリ内ストレージの場合はデフォルトで `:memory:` を使用
- `create_tables` (bool): 高度なテーブルを自動作成するかどうか。デフォルトは `False`
- `logger` (logging.Logger | None): セッション用のカスタムロガー。デフォルトはモジュールのロガー

## 使用状況の追跡

AdvancedSQLiteSession は、会話の各ターンごとにトークン使用データを保存することで、詳細な使用状況分析を提供します。 **これは各エージェントの実行後に `store_run_usage` メソッドが呼び出されることに完全に依存します。**

### 使用データの保存

```python
# After each agent run, store the usage data
result = await Runner.run(agent, "Hello", session=session)
await session.store_run_usage(result)

# This stores:
# - Total tokens used
# - Input/output token breakdown
# - Request count
# - Detailed JSON token information (if available)
```

### 使用統計の取得

```python
# Get session-level usage (all branches)
session_usage = await session.get_session_usage()
if session_usage:
    print(f"Total requests: {session_usage['requests']}")
    print(f"Total tokens: {session_usage['total_tokens']}")
    print(f"Input tokens: {session_usage['input_tokens']}")
    print(f"Output tokens: {session_usage['output_tokens']}")
    print(f"Total turns: {session_usage['total_turns']}")

# Get usage for specific branch
branch_usage = await session.get_session_usage(branch_id="main")

# Get usage by turn
turn_usage = await session.get_turn_usage()
for turn_data in turn_usage:
    print(f"Turn {turn_data['user_turn_number']}: {turn_data['total_tokens']} tokens")
    if turn_data['input_tokens_details']:
        print(f"  Input details: {turn_data['input_tokens_details']}")
    if turn_data['output_tokens_details']:
        print(f"  Output details: {turn_data['output_tokens_details']}")

# Get usage for specific turn
turn_2_usage = await session.get_turn_usage(user_turn_number=2)
```

## 会話の分岐

AdvancedSQLiteSession の主な機能の 1 つは、任意の ユーザー メッセージから会話のブランチを作成でき、代替の会話パスを探索できる点です。

### ブランチの作成

```python
# Get available turns for branching
turns = await session.get_conversation_turns()
for turn in turns:
    print(f"Turn {turn['turn']}: {turn['content']}")
    print(f"Can branch: {turn['can_branch']}")

# Create a branch from turn 2
branch_id = await session.create_branch_from_turn(2)
print(f"Created branch: {branch_id}")

# Create a branch with custom name
branch_id = await session.create_branch_from_turn(
    2, 
    branch_name="alternative_path"
)

# Create branch by searching for content
branch_id = await session.create_branch_from_content(
    "weather", 
    branch_name="weather_focus"
)
```

### ブランチ管理

```python
# List all branches
branches = await session.list_branches()
for branch in branches:
    current = " (current)" if branch["is_current"] else ""
    print(f"{branch['branch_id']}: {branch['user_turns']} turns, {branch['message_count']} messages{current}")

# Switch between branches
await session.switch_to_branch("main")
await session.switch_to_branch(branch_id)

# Delete a branch
await session.delete_branch(branch_id, force=True)  # force=True allows deleting current branch
```

### ブランチのワークフロー例

```python
# Original conversation
result = await Runner.run(agent, "What's the capital of France?", session=session)
await session.store_run_usage(result)

result = await Runner.run(agent, "What's the weather like there?", session=session)
await session.store_run_usage(result)

# Create branch from turn 2 (weather question)
branch_id = await session.create_branch_from_turn(2, "weather_focus")

# Continue in new branch with different question
result = await Runner.run(
    agent, 
    "What are the main tourist attractions in Paris?", 
    session=session
)
await session.store_run_usage(result)

# Switch back to main branch
await session.switch_to_branch("main")

# Continue original conversation
result = await Runner.run(
    agent, 
    "How expensive is it to visit?", 
    session=session
)
await session.store_run_usage(result)
```

## 構造化クエリ

AdvancedSQLiteSession は、会話の構造と内容を分析するための複数のメソッドを提供します。

### 会話分析

```python
# Get conversation organized by turns
conversation_by_turns = await session.get_conversation_by_turns()
for turn_num, items in conversation_by_turns.items():
    print(f"Turn {turn_num}: {len(items)} items")
    for item in items:
        if item["tool_name"]:
            print(f"  - {item['type']} (tool: {item['tool_name']})")
        else:
            print(f"  - {item['type']}")

# Get tool usage statistics
tool_usage = await session.get_tool_usage()
for tool_name, count, turn in tool_usage:
    print(f"{tool_name}: used {count} times in turn {turn}")

# Find turns by content
matching_turns = await session.find_turns_by_content("weather")
for turn in matching_turns:
    print(f"Turn {turn['turn']}: {turn['content']}")
```

### メッセージ構造

セッションはメッセージ構造を自動的に追跡します。内容は次のとおりです。

- メッセージタイプ（user、assistant、tool_call など）
- ツール呼び出しのツール名
- ターン番号とシーケンス番号
- ブランチとの関連付け
- タイムスタンプ

## データベーススキーマ

AdvancedSQLiteSession は、基本の SQLite スキーマを拡張し、次の 2 つの追加テーブルを含みます。

### message_structure テーブル

```sql
CREATE TABLE message_structure (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    message_id INTEGER NOT NULL,
    branch_id TEXT NOT NULL DEFAULT 'main',
    message_type TEXT NOT NULL,
    sequence_number INTEGER NOT NULL,
    user_turn_number INTEGER,
    branch_turn_number INTEGER,
    tool_name TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (session_id) REFERENCES agent_sessions(session_id) ON DELETE CASCADE,
    FOREIGN KEY (message_id) REFERENCES agent_messages(id) ON DELETE CASCADE
);
```

### turn_usage テーブル

```sql
CREATE TABLE turn_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    branch_id TEXT NOT NULL DEFAULT 'main',
    user_turn_number INTEGER NOT NULL,
    requests INTEGER DEFAULT 0,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    total_tokens INTEGER DEFAULT 0,
    input_tokens_details JSON,
    output_tokens_details JSON,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (session_id) REFERENCES agent_sessions(session_id) ON DELETE CASCADE,
    UNIQUE(session_id, branch_id, user_turn_number)
);
```

## 完全な例

すべての機能を網羅する [完全な例](https://github.com/openai/openai-agents-python/tree/main/examples/memory/advanced_sqlite_session_example.py) をご覧ください。


## API リファレンス

- [`AdvancedSQLiteSession`][agents.extensions.memory.advanced_sqlite_session.AdvancedSQLiteSession] - メインクラス
- [`Session`][agents.memory.session.Session] - ベースセッションプロトコル