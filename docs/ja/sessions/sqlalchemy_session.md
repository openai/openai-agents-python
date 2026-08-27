---
search:
  exclude: true
---
# SQLAlchemy セッション

`SQLAlchemySession` は SQLAlchemy を使用して本番環境対応のセッション実装を提供し、SQLAlchemy がサポートする任意のデータベース（PostgreSQL、MySQL、SQLite など）をセッションストレージとして使用できるようにします。

## インストール {#installation}

SQLAlchemy セッションには、`openai-agents` パッケージの optional-dependency extra `sqlalchemy` が必要です。

```bash
pip install openai-agents[sqlalchemy]
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
        "user-123",
        url="sqlite+aiosqlite:///:memory:",
        create_tables=True
    )
    
    result = await Runner.run(agent, "Hello", session=session)
    print(result.final_output)

if __name__ == "__main__":
    asyncio.run(main())
```

### 既存のエンジンの使用 {#using-existing-engine}

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
    session = SQLAlchemySession(
        "user-456",
        engine=engine,
        create_tables=True
    )
    
    result = await Runner.run(agent, "Hello", session=session)
    print(result.final_output)
    
    # Clean up
    await engine.dispose()

if __name__ == "__main__":
    asyncio.run(main())
```

## 非 ASCII テキストの保存 {#storing-non-ascii-text}

デフォルトでは、`SQLAlchemySession` はセッション項目を JSON にシリアライズする際に、非 ASCII 文字をエスケープします。これにより、従来の保存形式を維持しながら、項目の読み込み時には元のテキストを復元できます。

保存された JSON 内で多言語テキストを読み取り可能な状態に保つには、`ensure_ascii=False` を設定します。

```python
session = SQLAlchemySession.from_url(
    "user-123",
    url="sqlite+aiosqlite:///conversations.db",
    create_tables=True,
    ensure_ascii=False,
)
```

既存のエンジンを使用する場合は、同じオプションを `SQLAlchemySession(...)` に直接渡すことができます。この設定によって変更されるのはデータベースに保存される JSON 表現のみであり、セッションメソッドが返す値は変更されません。


## API リファレンス {#api-reference}

- [`SQLAlchemySession`][agents.extensions.memory.sqlalchemy_session.SQLAlchemySession] - メインクラス
- [`Session`][agents.memory.session.Session] - 基本セッションプロトコル