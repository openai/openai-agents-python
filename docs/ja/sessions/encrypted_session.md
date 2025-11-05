---
search:
  exclude: true
---
# 暗号化セッション

`EncryptedSession` は、あらゆるセッション実装に対して透過的な暗号化を提供し、会話データを保護しつつ古い項目を自動的に期限切れにします。

## 機能

-  **透過的な暗号化**: 任意のセッションを  Fernet  暗号化でラップします
-  **セッションごとの鍵**:  HKDF  による鍵導出でセッションごとに固有の暗号鍵を使用します
-  **自動有効期限**:  TTL  超過時は古い項目を黙ってスキップします
-  **置き換え可能**:  既存の任意のセッション実装で動作します

## インストール

暗号化セッションには `encrypt` 追加が必要です:

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

暗号化キーは  Fernet  キーまたは任意の文字列にできます:

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

### TTL (Time To Live)

暗号化された項目が有効な期間を設定します:

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

    `EncryptedSession` を `AdvancedSQLiteSession` のような高度なセッション実装と併用する場合、次にご注意ください:

    - `find_turns_by_content()` のようなメソッドは、メッセージ内容が暗号化されているため効果的に動作しません
    - 内容に基づく検索は暗号化データに対して行われるため、その有効性は制限されます



## 鍵導出

EncryptedSession は  HKDF  (HMAC-based Key Derivation Function) を使用して、セッションごとに固有の暗号鍵を導出します:

-  **マスターキー**: 提供された暗号化キー
-  **セッションソルト**: セッション  ID
-  **情報文字列**: `"agents.session-store.hkdf.v1"`
-  **出力**: 32 バイトの  Fernet  キー

これにより、次が保証されます:
-  各セッションは固有の暗号鍵を持ちます
-  マスターキーなしに鍵を導出できません
-  異なるセッション間でセッションデータを復号できません

## 自動有効期限

項目が  TTL  を超えた場合、取得時に自動的にスキップされます:

```python
# Items older than TTL are silently ignored
items = await session.get_items()  # Only returns non-expired items

# Expired items don't affect session behavior
result = await Runner.run(agent, "Continue conversation", session=session)
```

## API 参照

- [`EncryptedSession`][agents.extensions.memory.encrypt_session.EncryptedSession] - メインクラス
- [`Session`][agents.memory.session.Session] - ベースセッションプロトコル