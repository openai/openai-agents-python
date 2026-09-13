---
search:
  exclude: true
---
# 加密会话

`EncryptedSession` 可为任何会话实现提供透明加密，并通过自动使旧条目过期来保护对话数据。

## 功能 {#features}

- **透明加密**：使用 Fernet 加密封装任何会话
- **每会话密钥**：使用 HKDF 密钥派生，为每个会话生成唯一的加密密钥
- **自动过期**：TTL 到期后，会静默跳过旧条目
- **即插即用的替代方案**：适用于任何现有会话实现

## 安装 {#installation}

加密会话需要 `encrypt` extra：

```bash
pip install 'openai-agents[encrypt]'
```

## 快速入门 {#quick-start}

此示例使用内存中的 `SQLiteSession`。内置会话不需要单独的数据库驱动程序。

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

## 配置 {#configuration}

### 加密密钥 {#encryption-key}

加密密钥可以是 Fernet 密钥，也可以是任意字符串：

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

### TTL（存活时间） {#ttl-time-to-live}

设置加密条目的有效时长：

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

## 不同会话类型的用法 {#usage-with-different-session-types}

### SQLite 会话 {#with-sqlite-sessions}

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

### SQLAlchemy 会话 {#with-sqlalchemy-sessions}

对于下面的 PostgreSQL 示例，请安装 `encrypt` 和 `sqlalchemy` extras。`sqlalchemy` extra 包含 `postgresql+asyncpg://` URL 使用的 `asyncpg` 驱动程序。

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

应用程序负责释放由 `SQLAlchemySession.from_url()` 创建的引擎。在不再需要使用该引擎的所有 `SQLAlchemySession` 实例后，请在应用程序的清理流程中调用 `await underlying.engine.dispose()`，即使运行失败也应如此。

!!! warning "高级会话功能"

    将 `EncryptedSession` 与 `AdvancedSQLiteSession` 等高级会话实现配合使用时，请注意：

    - 由于消息内容已加密，`find_turns_by_content()` 等方法无法有效工作
    - 基于内容的搜索会对加密数据执行操作，因此效果有限



## 密钥派生 {#key-derivation}

EncryptedSession 使用 HKDF（基于 HMAC 的密钥派生函数）为每个会话派生唯一的加密密钥：

- **主密钥**：您提供的加密密钥
- **会话盐值**：会话 ID
- **信息字符串**：`"agents.session-store.hkdf.v1"`
- **输出**：32 字节的 Fernet 密钥

这可以确保：
- 每个会话都有唯一的加密密钥
- 没有主密钥便无法派生密钥
- 无法跨不同会话解密会话数据

## 自动过期 {#automatic-expiration}

当条目超过 TTL 时，检索期间会自动跳过这些条目：

```python
# Items older than TTL are silently ignored
items = await session.get_items()  # Only returns non-expired items

# Expired items don't affect session behavior
result = await Runner.run(agent, "Continue conversation", session=session)
```

## API 参考 {#api-reference}

- [`EncryptedSession`][agents.extensions.memory.encrypt_session.EncryptedSession] - 主类
- [`Session`][agents.memory.session.Session] - 基础会话协议