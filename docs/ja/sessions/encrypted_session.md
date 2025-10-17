---
search:
  exclude: true
---
# 暗号化セッション

`EncryptedSession` は任意のセッション実装に透過的な暗号化を提供し、会話データを保護するとともに古い項目を自動的に期限切れにします。

## 特長

- **透過的な暗号化**: 任意のセッションを Fernet 暗号化でラップします
- **セッションごとのキー**: 一意の暗号化のために HKDF キー導出を使用します
- **自動期限切れ**: TTL が切れた古い項目は静かにスキップされます
- **ドロップイン代替**: 既存のどのセッション実装でも動作します

## インストール

暗号化セッションには `encrypt` 追加オプションが必要です:

```bash
pip install openai-agents[encrypt]
```

## クイックスタート

```python
import asyncio
from agents import Agent, Runner
from agents.extensions.memory import EncryptedSession, SQLAlchemySession

async def main():
    agent = Agent("Assistant")
    
    # Create underlying session
    underlying_session = SQLAlchemySession.from_url(
        "user-123",
        url="sqlite+aiosqlite:///:memory:",
        create_tables=True
    )
    
    # Wrap with encryption
    session = EncryptedSession(
        session_id="user-123",
        underlying_session=underlying_session,
        encryption_key="your-secret-key-here",
        ttl=600  # 10 minutes
    )
    
    result = await Runner.run(agent, "Hello", session=session)
    print(result.final_output)

if __name__ == "__main__":
    asyncio.run(main())
```

## 設定

### 暗号化キー

暗号化キーは Fernet キーまたは任意の文字列にできます:

```python
from agents.extensions.memory import EncryptedSession

# Using a Fernet key (base64-encoded)
session = EncryptedSession(
    session_id="user-123",
    underlying_session=underlying_session,
    encryption_key="your-fernet-key-here",
    ttl=600
)

# Using a raw string (will be derived to a key)
session = EncryptedSession(
    session_id="user-123", 
    underlying_session=underlying_session,
    encryption_key="my-secret-password",
    ttl=600
)
```

### TTL（有効期間）

暗号化された項目を有効にしておく期間を設定します:

```python
# Items expire after 1 hour
session = EncryptedSession(
    session_id="user-123",
    underlying_session=underlying_session,
    encryption_key="secret",
    ttl=3600  # 1 hour in seconds
)

# Items expire after 1 day
session = EncryptedSession(
    session_id="user-123",
    underlying_session=underlying_session,
    encryption_key="secret", 
    ttl=86400  # 24 hours in seconds
)
```

## さまざまなセッションタイプでの使用

### SQLite セッションでの使用

```python
from agents import SQLiteSession
from agents.extensions.memory import EncryptedSession

# Create encrypted SQLite session
underlying = SQLiteSession("user-123", "conversations.db")

session = EncryptedSession(
    session_id="user-123",
    underlying_session=underlying,
    encryption_key="secret-key"
)
```

### SQLAlchemy セッションでの使用

```python
from agents.extensions.memory import EncryptedSession, SQLAlchemySession

# Create encrypted SQLAlchemy session
underlying = SQLAlchemySession.from_url(
    "user-123",
    url="postgresql+asyncpg://user:pass@localhost/db",
    create_tables=True
)

session = EncryptedSession(
    session_id="user-123",
    underlying_session=underlying,
    encryption_key="secret-key"
)
```

!!! warning "高度なセッション機能"

    `EncryptedSession` を `AdvancedSQLiteSession` のような高度なセッション実装と併用する場合、次に注意してください。

    - メッセージ内容が暗号化されるため、`find_turns_by_content()` のようなメソッドは有効に機能しません
    - 内容に基づく検索は暗号化データ上で実行されるため、その有効性は制限されます



## キー導出

EncryptedSession は HKDF（HMAC ベースの Key Derivation Function）を使用して、セッションごとに一意の暗号化キーを導出します。

- **マスターキー**: 提供された暗号化キー
- **セッションソルト**: セッション ID
- **Info 文字列**: `"agents.session-store.hkdf.v1"`
- **出力**: 32-byte の Fernet キー

これにより、次のことが保証されます。
- 各セッションは一意の暗号化キーを持ちます
- マスターキーなしにキーを導出することはできません
- 異なるセッション間でセッションデータを復号できません

## 自動期限切れ

項目が TTL を超えた場合、取得時に自動的にスキップされます。

```python
# Items older than TTL are silently ignored
items = await session.get_items()  # Only returns non-expired items

# Expired items don't affect session behavior
result = await Runner.run(agent, "Continue conversation", session=session)
```

## API リファレンス

- [`EncryptedSession`][agents.extensions.memory.encrypt_session.EncryptedSession] - メインクラス
- [`Session`][agents.memory.session.Session] - ベースセッションプロトコル