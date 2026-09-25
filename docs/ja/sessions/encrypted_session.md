---
search:
  exclude: true
---
# 暗号化セッション

`EncryptedSession` は、あらゆるセッション実装に透過的な暗号化を提供し、古い項目を自動的に期限切れにすることで会話データを保護します。

## 機能 {#features}

- **透過的な暗号化**: あらゆるセッションを Fernet 暗号化でラップします
- **セッションごとのキー**: HKDF による鍵導出を使用し、セッションごとに一意の暗号化を行います
- **自動的な期限切れ**: TTL が期限切れになると、古い項目は通知なくスキップされます
- **ドロップイン置換**: 既存のあらゆるセッション実装で機能します

## インストール {#installation}

暗号化セッションには `encrypt` extra が必要です。

```bash
pip install 'openai-agents[encrypt]'
```

## クイックスタート {#quick-start}

この例では、インメモリの `SQLiteSession` を使用し、実行ごとに新しい暗号化キーを生成します。組み込みセッションには、別途データベースドライバーは必要ありません。永続ストレージでは、プロセスの再起動後もキーを保持して再利用できるように、[暗号化キーに関するガイダンス](#encryption-key)に従ってください。

```python
import asyncio
from cryptography.fernet import Fernet
from agents import Agent, Runner, SQLiteSession
from agents.extensions.memory import EncryptedSession

async def main():
    agent = Agent("Assistant")
    encryption_key = Fernet.generate_key().decode("ascii")

    underlying_session = SQLiteSession("user-123")
    try:
        session = EncryptedSession(
            session_id="user-123",
            underlying_session=underlying_session,
            encryption_key=encryption_key,
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

### 暗号化キー {#encryption-key}

[`Fernet.generate_key()`](https://cryptography.io/en/latest/fernet/#cryptography.fernet.Fernet.generate_key) で生成したキーや、アプリケーションのシークレット管理システムによってプロビジョニングされた高エントロピーのランダムシークレットなど、暗号学的に安全なランダム性を持つ高エントロピーのマスターキーを使用してください。パスワード、覚えやすいフレーズ、ハードコードされたサンプル値を暗号化キーとして使用しないでください。

```python
from cryptography.fernet import Fernet

# Generate once when provisioning a new key, then store the value securely.
encryption_key = Fernet.generate_key().decode("ascii")
```

永続ストレージでは、キーを一度だけ生成し、そのキーでデータを暗号化する前に、値をシークレットマネージャーまたはその他の安全なストアに保存してください。プロセスを起動するたびに、同じキーを読み込んでください。以下のスニペットでは、デプロイ環境から保存済みのキーが `SESSION_ENCRYPTION_KEY` として注入されることを前提としています。この環境変数名はアプリケーション側の規約であり、SDK の設定ではありません。

```python
import os
from agents.extensions.memory import EncryptedSession

encryption_key = os.environ["SESSION_ENCRYPTION_KEY"]
session = EncryptedSession(
    session_id="user-123",
    underlying_session=underlying_session,
    encryption_key=encryption_key,
    ttl=600
)
```

キーは秘密に保ち、暗号化されたセッションデータベースとは別に管理してください。再起動後に既存のデータを読み取るには、同じマスターキーと同じ `session_id` を使用してください。起動時に代替キーを生成すると、アプリケーションは既存のレコードを復号できなくなります。`encryption_key` を変更しても、保存済みのレコードは再暗号化されません。既存のレコードには引き続き元のキーが必要であり、TTL も引き続き適用されます。

`EncryptedSession` は、後方互換性のために raw 文字列を引き続き受け付けます。ただし、これはパスワードやその他の低エントロピーのシークレットの使用を推奨するものではありません。SDK はセッションごとの鍵導出に HKDF を使用しますが、パスワード強化には使用しません。セッション ID のソルトはセッションキーを分離しますが、シークレットのエントロピーを追加するものではありません。以下の[鍵導出](#key-derivation)を参照してください。

### TTL （有効期間） {#ttl-time-to-live}

暗号化された項目が有効である期間を設定します。以下のスニペットでは、上で読み込んだ `encryption_key` を再利用します。

```python
# Items expire after 1 hour
session = EncryptedSession(
    session_id="user-123",
    underlying_session=underlying_session,
    encryption_key=encryption_key,
    ttl=3600  # 1 hour in seconds
)

# Items expire after 1 day
session = EncryptedSession(
    session_id="user-123",
    underlying_session=underlying_session,
    encryption_key=encryption_key,
    ttl=86400  # 24 hours in seconds
)
```

## 各種セッションタイプでの使用 {#usage-with-different-session-types}

### SQLite セッションとの併用 {#with-sqlite-sessions}

```python
import os
from agents import SQLiteSession
from agents.extensions.memory import EncryptedSession

# Load the same securely stored key each time this database is opened.
encryption_key = os.environ["SESSION_ENCRYPTION_KEY"]

# Create encrypted SQLite session
underlying = SQLiteSession("user-123", "conversations.db")

session = EncryptedSession(
    session_id="user-123",
    underlying_session=underlying,
    encryption_key=encryption_key
)
```

### SQLAlchemy セッションとの併用 {#with-sqlalchemy-sessions}

以下の PostgreSQL の例では、`encrypt` および `sqlalchemy` extras をインストールしてください。`sqlalchemy` extra には、`postgresql+asyncpg://` URL で使用される `asyncpg` ドライバーが含まれます。

```bash
pip install 'openai-agents[encrypt,sqlalchemy]'
```

```python
import os
from agents.extensions.memory import EncryptedSession, SQLAlchemySession

# Load the same securely stored key each time this database is opened.
encryption_key = os.environ["SESSION_ENCRYPTION_KEY"]

# Create encrypted SQLAlchemy session
underlying = SQLAlchemySession.from_url(
    "user-123",
    url="postgresql+asyncpg://user:pass@localhost/db",
    create_tables=True
)

session = EncryptedSession(
    session_id="user-123",
    underlying_session=underlying,
    encryption_key=encryption_key
)
```

`SQLAlchemySession.from_url()` によって作成されたエンジンを破棄する責任は、アプリケーションにあります。そのエンジンを使用するすべての `SQLAlchemySession` インスタンスが不要になった後、実行が失敗した場合でも、アプリケーションのクリーンアップ処理で `await underlying.engine.dispose()` を呼び出してください。

!!! warning "高度なセッション機能"

    `AdvancedSQLiteSession` のような高度なセッション実装で `EncryptedSession` を使用する場合は、以下の点に注意してください。

    - メッセージの内容が暗号化されるため、`find_turns_by_content()` のようなメソッドは効果的に機能しません
    - 内容に基づく検索は暗号化されたデータを対象とするため、その有効性が制限されます



## 鍵導出 {#key-derivation}

EncryptedSession は、HKDF （HMAC ベースの鍵導出関数）を使用して、セッションごとに一意の暗号化キーを導出します。

- **マスターキー**: 指定した暗号化キー
- **セッションソルト**: セッション ID
- **情報文字列**: `"agents.session-store.hkdf.v1"`
- **出力**: 32 バイトの Fernet キー

高エントロピーのマスターキーを使用すると、異なるセッション ID から異なる派生キーが生成されます。同じマスターキーとセッション ID を再利用すると同じ派生キーが生成されるため、アプリケーションは以前に保存された有効期限内の項目を復号できます。

HKDF を使用しても、脆弱なマスターキーがパスワード推測に耐えられるようになるわけではありません。セッション ID は秘密ではないソルトであり、追加の秘密鍵素材ではありません。鍵導出とパスワード強化の違いについては、[cryptography の HKDF ドキュメント](https://cryptography.io/en/latest/hazmat/primitives/key-derivation-functions/#hkdf)を参照してください。

## 自動的な期限切れ {#automatic-expiration}

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