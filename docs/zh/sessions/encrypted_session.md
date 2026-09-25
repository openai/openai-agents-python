---
search:
  exclude: true
---
# 加密会话

`EncryptedSession` 为任何会话实现提供透明加密，通过自动使旧数据项过期来保护对话数据。

## 功能 {#features}

- **透明加密**：使用 Fernet 加密封装任何会话
- **每会话密钥**：使用 HKDF 密钥派生，为每个会话生成唯一的加密密钥
- **自动过期**：TTL 到期后，系统会静默跳过旧数据项
- **直接替换**：可与任何现有会话实现配合使用

## 安装 {#installation}

加密会话需要 `encrypt` extra：

```bash
pip install 'openai-agents[encrypt]'
```

## 快速开始 {#quick-start}

此代码示例使用内存中的 `SQLiteSession`，并为每次执行生成新的加密密钥。内置会话不需要单独的数据库驱动程序。对于持久化存储，请遵循[加密密钥指南](#encryption-key)，以便在进程重启后保留并复用密钥。

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

## 配置 {#configuration}

### 加密密钥 {#encryption-key}

请使用由密码学安全随机方式生成的高熵主密钥，例如使用 [`Fernet.generate_key()`](https://cryptography.io/en/latest/fernet/#cryptography.fernet.Fernet.generate_key) 生成的密钥，或由应用程序的密钥管理系统提供的高熵随机密钥。请勿使用密码、容易记忆的短语或硬编码的示例值作为加密密钥。

```python
from cryptography.fernet import Fernet

# Generate once when provisioning a new key, then store the value securely.
encryption_key = Fernet.generate_key().decode("ascii")
```

对于持久化存储，请仅生成一次密钥，并在使用该密钥加密数据之前，将其值保存在密钥管理器或其他安全存储中。每次启动进程时都应加载同一个密钥。以下代码片段假定部署环境将存储的密钥注入为 `SESSION_ENCRYPTION_KEY`；该环境变量名称是应用程序约定，而非 SDK 设置。

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

请对密钥保密，并将其与加密的会话数据库分开存放。若要在重启后读取现有数据，请使用相同的主密钥和相同的 `session_id`。在启动时生成替代密钥会导致应用程序无法解密现有记录。更改 `encryption_key` 不会重新加密已存储的记录；现有记录仍需要原始密钥，并继续受其 TTL 限制。

为保持向后兼容性，`EncryptedSession` 仍接受原始字符串。但这并不意味着建议使用密码或其他低熵密钥。SDK 使用 HKDF 派生每会话密钥，而非进行密码强化。会话 ID 盐值可分隔不同的会话密钥，但不会增加任何秘密熵。请参阅下文的[密钥派生](#key-derivation)。

### TTL（生存时间） {#ttl-time-to-live}

设置加密数据项保持有效的时长。以下代码片段复用上文加载的 `encryption_key`：

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

## 不同会话类型的用法 {#usage-with-different-session-types}

### SQLite 会话 {#with-sqlite-sessions}

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

### SQLAlchemy 会话 {#with-sqlalchemy-sessions}

对于下面的 PostgreSQL 代码示例，请安装 `encrypt` 和 `sqlalchemy` extras。`sqlalchemy` extra 包含 `postgresql+asyncpg://` URL 所使用的 `asyncpg` 驱动程序。

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

应用程序负责释放由 `SQLAlchemySession.from_url()` 创建的引擎。当使用该引擎的所有 `SQLAlchemySession` 实例都不再需要后，请在应用程序的清理路径中调用 `await underlying.engine.dispose()`，即使运行失败也应如此。

!!! warning "高级会话功能"

    将 `EncryptedSession` 与 `AdvancedSQLiteSession` 等高级会话实现配合使用时，请注意：

    - 由于消息内容已加密，`find_turns_by_content()` 等方法无法有效工作
    - 基于内容的搜索会对加密数据执行操作，因此其有效性会受到限制



## 密钥派生 {#key-derivation}

EncryptedSession 使用 HKDF（基于 HMAC 的密钥派生函数）为每个会话派生唯一的加密密钥：

- **主密钥**：您提供的加密密钥
- **会话盐值**：会话 ID
- **信息字符串**：`"agents.session-store.hkdf.v1"`
- **输出**：32 字节的 Fernet 密钥

使用高熵主密钥时，不同的会话 ID 会生成不同的派生密钥。复用相同的主密钥和会话 ID 会生成相同的派生密钥，使应用程序能够解密此前存储且尚未过期的数据项。

HKDF 无法使弱主密钥抵御密码猜测攻击。会话 ID 是非秘密盐值，而非额外的秘密密钥材料。有关密钥派生与密码强化之间的区别，请参阅 [cryptography HKDF 文档](https://cryptography.io/en/latest/hazmat/primitives/key-derivation-functions/#hkdf)。

## 自动过期 {#automatic-expiration}

当数据项超过 TTL 时，系统会在检索期间自动跳过这些数据项：

```python
# Items older than TTL are silently ignored
items = await session.get_items()  # Only returns non-expired items

# Expired items don't affect session behavior
result = await Runner.run(agent, "Continue conversation", session=session)
```

## API 参考 {#api-reference}

- [`EncryptedSession`][agents.extensions.memory.encrypt_session.EncryptedSession] - 主类
- [`Session`][agents.memory.session.Session] - 基础会话协议