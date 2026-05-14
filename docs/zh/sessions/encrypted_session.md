---
search:
  exclude: true
---
# 加密会话

`EncryptedSession` 为任何会话实现提供透明加密，通过自动过期旧条目来保护对话数据。

## 功能

- **透明加密**：使用 Fernet 加密包装任何会话
- **每会话密钥**：使用 HKDF 密钥派生，为每个会话生成唯一加密
- **自动过期**：TTL 过期时会静默跳过旧条目
- **即插即用替代方案**：适用于任何现有会话实现

## 安装

加密会话需要 `encrypt` extra：

```bash
pip install openai-agents[encrypt]
```

## 快速入门

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

### TTL（存活时间）

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

## 与不同会话类型的搭配使用

### 与 SQLite 会话搭配使用

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

### 与 SQLAlchemy 会话搭配使用

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

    将 `EncryptedSession` 与 `AdvancedSQLiteSession` 等高级会话实现一起使用时，请注意：

    - 像 `find_turns_by_content()` 这样的方法无法有效工作，因为消息内容已加密
    - 基于内容的搜索会在加密数据上运行，因此效果受限



## 密钥派生

EncryptedSession 使用 HKDF（基于 HMAC 的密钥派生函数）为每个会话派生唯一的加密密钥：

- **主密钥**：你提供的加密密钥
- **会话盐值**：会话 ID
- **信息字符串**：`"agents.session-store.hkdf.v1"`
- **输出**：32 字节 Fernet 密钥

这可以确保：
- 每个会话都有唯一的加密密钥
- 没有主密钥就无法派生密钥
- 不同会话之间的会话数据无法相互解密

## 自动过期

当条目超过 TTL 时，检索过程中会自动跳过它们：

```python
# Items older than TTL are silently ignored
items = await session.get_items()  # Only returns non-expired items

# Expired items don't affect session behavior
result = await Runner.run(agent, "Continue conversation", session=session)
```

## API 参考

- [`EncryptedSession`][agents.extensions.memory.encrypt_session.EncryptedSession] - 主类
- [`Session`][agents.memory.session.Session] - 基础会话协议