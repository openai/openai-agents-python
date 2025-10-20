---
search:
  exclude: true
---
# 加密会话

`EncryptedSession` 为任意会话实现提供透明加密，通过自动过期机制在 TTL 到期时跳过旧的会话项，从而保护会话数据。

## 功能

- **透明加密**: 使用 Fernet 加密包装任意会话
- **每会话密钥**: 使用 HKDF 密钥派生为每个会话生成唯一加密密钥
- **自动过期**: 当 TTL 到期时，旧项会被静默跳过
- **可直接替换**: 可与任何现有会话实现一起使用

## 安装

加密会话需要 `encrypt` 额外依赖：

```bash
pip install openai-agents[encrypt]
```

## 快速开始

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

## 配置

### 加密密钥

加密密钥可以是 Fernet 密钥或任意字符串：

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

### TTL（存活时间）

设置加密项保持有效的时长：

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

## 在不同会话类型中的用法

### 使用 SQLite 会话

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

### 使用 SQLAlchemy 会话

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

!!! warning "高级会话功能"

    当将 `EncryptedSession` 与诸如 `AdvancedSQLiteSession` 等高级会话实现一起使用时，请注意：

    - 由于消息内容被加密，类似 `find_turns_by_content()` 的方法将难以有效工作
    - 基于内容的搜索会在密文上运行，效果受限



## 密钥派生

EncryptedSession 使用 HKDF（基于 HMAC 的密钥派生函数）为每个会话派生唯一的加密密钥：

- **主密钥**: 你提供的加密密钥
- **会话盐值**: 会话 ID
- **信息字符串**: `"agents.session-store.hkdf.v1"`
- **输出**: 32 字节的 Fernet 密钥

这确保：
- 每个会话都有唯一的加密密钥
- 没有主密钥无法推导出各会话密钥
- 不同会话之间的会话数据无法相互解密

## 自动过期

当项超过 TTL 时，在检索时会被自动跳过：

```python
# Items older than TTL are silently ignored
items = await session.get_items()  # Only returns non-expired items

# Expired items don't affect session behavior
result = await Runner.run(agent, "Continue conversation", session=session)
```

## API 参考

- [`EncryptedSession`][agents.extensions.memory.encrypt_session.EncryptedSession] - 主类
- [`Session`][agents.memory.session.Session] - 基础会话协议