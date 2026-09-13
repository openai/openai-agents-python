---
search:
  exclude: true
---
# 暗号化セッション

`EncryptedSession` は、あらゆるセッション実装に透過的な暗号化を提供し、古い項目を自動的に期限切れにすることで会話データを保護します。

## 機能 {#features}

- **透過的な暗号化**: あらゆるセッションを Fernet 暗号化でラップします
- **セッションごとの鍵**: HKDF 鍵導出を使用して、セッションごとに一意の暗号化を行います
- **自動期限切れ**: TTL が切れた古い項目は通知なくスキップされます
- **そのまま置き換え可能**: 既存のあらゆるセッション実装で動作します

## インストール {#installation}

暗号化セッションには `encrypt` extra が必要です。

```bash
pip install 'openai-agents[encrypt]'
```

## クイックスタート {#quick-start}

この例では、インメモリの `SQLiteSession` を使用します。組み込みセッションには、別途データベースドライバーは必要ありません。

```python
import asyncio
from agents import Agent, Runner, SQLiteSession
from agents.extensions.memory import EncryptedSession

async def main():
    agent = Agent("Assistant")

    underlying_session = SQLiteSession("user-123")
    try:
        session = EncryptedSession(
            session_id="user-123",
            underlying_session=underlying_session,
            encryption_key="your-secret-key-here",
            ttl=600  # 10 minutes
        )

        result = await Runner.run(agent, "Hello", session=session)
        print(result.final_output)
    finally:
        underlying_session.close()

if __name__ == "__main__":
    asyncio.run(main())
```

## 設定 {#configuration}

### 暗号化鍵 {#encryption-key}

暗号化鍵には、Fernet 鍵または任意の文字列を使用できます。

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

### TTL（有効期間） {#ttl-time-to-live}

暗号化された項目が有効である期間を設定します。

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

## 各種セッションタイプでの使用 {#usage-with-different-session-types}

### SQLite セッション {#with-sqlite-sessions}

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

### SQLAlchemy セッション {#with-sqlalchemy-sessions}

以下の PostgreSQL の例では、`encrypt` および `sqlalchemy` extra をインストールします。`sqlalchemy` extra には、`postgresql+asyncpg://` URL で使用される `asyncpg` ドライバーが含まれています。

```bash
pip install 'openai-agents[encrypt,sqlalchemy]'
```

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

`SQLAlchemySession.from_url()` によって作成されたエンジンを破棄する責任は、アプリケーションにあります。そのエンジンを使用するすべての `SQLAlchemySession` インスタンスが不要になった後、実行が失敗した場合でも、アプリケーションのクリーンアップ処理で `await underlying.engine.dispose()` を呼び出してください。

!!! warning "高度なセッション機能"

    `AdvancedSQLiteSession` のような高度なセッション実装で `EncryptedSession` を使用する場合は、次の点に注意してください。

    - メッセージ内容が暗号化されているため、`find_turns_by_content()` のようなメソッドは効果的に機能しません
    - 内容に基づく検索は暗号化されたデータを対象とするため、その有効性が制限されます



## 鍵導出 {#key-derivation}

EncryptedSession は、HKDF（HMAC ベースの鍵導出関数）を使用して、セッションごとに一意の暗号化鍵を導出します。

- **マスター鍵**: 指定した暗号化鍵
- **セッションソルト**: セッション ID
- **情報文字列**: `"agents.session-store.hkdf.v1"`
- **出力**: 32 バイトの Fernet 鍵

これにより、次のことが保証されます。
- 各セッションに一意の暗号化鍵が割り当てられます
- マスター鍵なしでは鍵を導出できません
- 異なるセッション間でセッションデータを復号できません

## 自動期限切れ {#automatic-expiration}

項目が TTL を超えると、取得時に自動的にスキップされます。

```python
# Items older than TTL are silently ignored
items = await session.get_items()  # Only returns non-expired items

# Expired items don't affect session behavior
result = await Runner.run(agent, "Continue conversation", session=session)
```

## API リファレンス {#api-reference}

- [`EncryptedSession`][agents.extensions.memory.encrypt_session.EncryptedSession] - メインクラス
- [`Session`][agents.memory.session.Session] - 基本セッションプロトコル