---
search:
  exclude: true
---
# 암호화된 세션

`EncryptedSession`은 모든 세션 구현에 투명한 암호화를 제공하며, 대화 데이터를 보호하고 오래된 항목을 자동으로 만료합니다.

## 기능

- **투명한 암호화**: 어떤 세션이든 Fernet 암호화로 감쌉니다
- **세션별 키**: HKDF 키 파생을 사용해 세션마다 고유한 암호화를 적용합니다
- **자동 만료**: TTL이 만료되면 오래된 항목은 자동으로 건너뜁니다
- **대체 가능**: 기존의 어떤 세션 구현과도 함께 동작합니다

## 설치

암호화된 세션을 사용하려면 `encrypt` extra가 필요합니다:

```bash
pip install openai-agents[encrypt]
```

## 빠른 시작

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

## 구성

### 암호화 키

암호화 키는 Fernet 키이거나 임의의 문자열일 수 있습니다:

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

암호화된 항목이 유효한 기간을 설정합니다:

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

## 다양한 세션 유형과의 사용

### SQLite 세션 사용

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

### SQLAlchemy 세션 사용

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

!!! warning "고급 세션 기능"

    `EncryptedSession`을 `AdvancedSQLiteSession` 같은 고급 세션 구현과 함께 사용할 때 다음 사항에 유의하세요:

    - 메시지 내용이 암호화되므로 `find_turns_by_content()` 같은 메서드는 효과적으로 동작하지 않습니다
    - 내용 기반 검색은 암호화된 데이터에서 수행되어 효율이 제한됩니다



## 키 파생

EncryptedSession은 HKDF(HMAC 기반 키 파생 함수)를 사용하여 세션마다 고유한 암호화 키를 파생합니다:

- **마스터 키**: 제공한 암호화 키
- **세션 솔트**: 세션 ID
- **Info 문자열**: `"agents.session-store.hkdf.v1"`
- **출력**: 32바이트 Fernet 키

이는 다음을 보장합니다:
- 각 세션은 고유한 암호화 키를 가집니다
- 마스터 키 없이는 키를 파생할 수 없습니다
- 서로 다른 세션 간에는 세션 데이터를 복호화할 수 없습니다

## 자동 만료

항목이 TTL을 초과하면 검색 중에 자동으로 건너뜁니다:

```python
# Items older than TTL are silently ignored
items = await session.get_items()  # Only returns non-expired items

# Expired items don't affect session behavior
result = await Runner.run(agent, "Continue conversation", session=session)
```

## API 레퍼런스

- [`EncryptedSession`][agents.extensions.memory.encrypt_session.EncryptedSession] - 기본 클래스
- [`Session`][agents.memory.session.Session] - 기본 세션 프로토콜