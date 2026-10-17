---
search:
  exclude: true
---
# SQLAlchemy セッション

`SQLAlchemySession` は SQLAlchemy を使用して、本番環境に対応したセッション実装を提供します。セッションストレージには、SQLAlchemy がサポートする任意のデータベース（PostgreSQL、MySQL、SQLite など）を使用できます。

## インストール {#installation}

SQLAlchemy セッションには、`sqlalchemy` optional-dependency extra と、データベース URL に対応する非同期データベースドライバーが必要です。

以下の SQLite の例（`sqlite+aiosqlite://`）では、この extra とともに `aiosqlite` をインストールします。

```bash
pip install 'openai-agents[sqlalchemy]' aiosqlite
```

この extra には、`postgresql+asyncpg://` で始まる PostgreSQL URL 用の `asyncpg` がすでに含まれています。`mysql+aiomysql://` で始まる MySQL URL の場合は、この extra とともに `aiomysql` をインストールします。ドライバーの `rsa` extra は、MySQL の SHA-256 認証方式に必要な依存関係を提供します。

```bash
pip install 'openai-agents[sqlalchemy]' 'aiomysql[rsa]'
```

## クイックスタート {#quick-start}

### データベース URL の使用 {#using-database-url}

最も簡単に開始する方法は次のとおりです。

```python
import asyncio
from agents import Agent, Runner
from agents.extensions.memory import SQLAlchemySession


async def main():
    agent = Agent("Assistant")

    # Create session using database URL
    session = SQLAlchemySession.from_url(
        "user-123", url="sqlite+aiosqlite:///:memory:", create_tables=True
    )

    result = await Runner.run(agent, "Hello", session=session)
    print(result.final_output)


if __name__ == "__main__":
    asyncio.run(main())
```

### 既存エンジンの使用 {#using-existing-engine}

既存の SQLAlchemy エンジンを使用するアプリケーションの場合は、次のようにします。

```python
import asyncio
from agents import Agent, Runner
from agents.extensions.memory import SQLAlchemySession
from sqlalchemy.ext.asyncio import create_async_engine


async def main():
    # Create your database engine
    engine = create_async_engine("postgresql+asyncpg://user:pass@localhost/db")

    agent = Agent("Assistant")
    session = SQLAlchemySession("user-456", engine=engine, create_tables=True)

    result = await Runner.run(agent, "Hello", session=session)
    print(result.final_output)

    # Clean up
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
```

## 非 ASCII テキストの保存 {#storing-non-ascii-text}

デフォルトでは、`SQLAlchemySession` はセッション項目を JSON にシリアライズするときに、非 ASCII 文字をエスケープします。これにより、従来のストレージ形式を維持しながら、項目の読み込み時に元のテキストを損失なく復元できます。

保存される JSON 内で多言語テキストを読み取り可能な状態に保つには、`ensure_ascii=False` を設定します。

```python
session = SQLAlchemySession.from_url(
    "user-123",
    url="sqlite+aiosqlite:///conversations.db",
    create_tables=True,
    ensure_ascii=False,
)
```

既存のエンジンを使用する場合は、同じオプションを `SQLAlchemySession(...)` に直接渡すことができます。この設定はデータベースに保存される JSON 表現のみを変更し、セッションメソッドが返す値は変更しません。


## API リファレンス {#api-reference}

- [`SQLAlchemySession`][agents.extensions.memory.sqlalchemy_session.SQLAlchemySession] - メインクラス
- [`Session`][agents.memory.session.Session] - 基本セッションプロトコル