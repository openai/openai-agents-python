---
search:
  exclude: true
---
# SQLAlchemy 会话

`SQLAlchemySession` 使用 SQLAlchemy 提供生产就绪的会话实现，让你可以使用 SQLAlchemy 支持的任何数据库（PostgreSQL、MySQL、SQLite 等）存储会话。

## 安装 {#installation}

SQLAlchemy 会话需要 `sqlalchemy` 可选依赖 extra，以及与数据库 URL 匹配的异步数据库驱动程序。

对于下方的 SQLite 示例（`sqlite+aiosqlite://`），请安装该 extra，并同时安装 `aiosqlite`：

```bash
pip install 'openai-agents[sqlalchemy]' aiosqlite
```

该 extra 已包含 `asyncpg`，用于以 `postgresql+asyncpg://` 开头的 PostgreSQL URL。对于以 `mysql+aiomysql://` 开头的 MySQL URL，请安装该 extra，并同时安装 `aiomysql`。该驱动程序的 `rsa` extra 提供 MySQL SHA-256 身份验证方法所需的依赖项：

```bash
pip install 'openai-agents[sqlalchemy]' 'aiomysql[rsa]'
```

## 快速入门 {#quick-start}

### 数据库 URL 的使用 {#using-database-url}

最简单的入门方式：

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

### 现有引擎的使用 {#using-existing-engine}

对于已有 SQLAlchemy 引擎的应用程序：

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

## 非 ASCII 文本存储 {#storing-non-ascii-text}

默认情况下，`SQLAlchemySession` 在将会话项序列化为 JSON 时会转义非 ASCII 字符。这样既能保留原有的存储格式，又能在加载会话项时还原原始文本。

如果希望多语言文本在存储的 JSON 中保持可读，请设置 `ensure_ascii=False`：

```python
session = SQLAlchemySession.from_url(
    "user-123",
    url="sqlite+aiosqlite:///conversations.db",
    create_tables=True,
    ensure_ascii=False,
)
```

使用现有引擎时，可以将相同的选项直接传递给 `SQLAlchemySession(...)`。此设置只会更改数据库中存储的 JSON 表示形式，不会更改会话方法返回的值。


## API 参考 {#api-reference}

- [`SQLAlchemySession`][agents.extensions.memory.sqlalchemy_session.SQLAlchemySession] - 主类
- [`Session`][agents.memory.session.Session] - 基础会话协议