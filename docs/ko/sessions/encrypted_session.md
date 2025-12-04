---
search:
  exclude: true
---
# 암호화된 세션

`EncryptedSession`은 모든 세션 구현에 대해 투명한 암호화를 제공하며, 자동 만료를 통해 오래된 항목을 안전하게 처리합니다.

## 기능

- **투명한 암호화**: 모든 세션을 Fernet 암호화로 래핑
- **세션별 키**: 각 세션마다 고유한 암호화를 위해 HKDF 키 유도 사용
- **자동 만료**: TTL이 만료되면 오래된 항목을 자동으로 건너뜀
- **바로 교체 가능**: 기존 세션 구현과 함께 사용 가능

## 설치

암호화 세션에는 `encrypt` extra가 필요합니다:

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

암호화된 항목의 유효 기간을 설정합니다:

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

## 세션 타입별 사용법

### SQLite 세션과 함께 사용

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

### SQLAlchemy 세션과 함께 사용

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

    `EncryptedSession`을 `AdvancedSQLiteSession`과 같은 고급 세션 구현과 함께 사용할 때는 다음을 유의하세요:

    - 메시지 콘텐츠가 암호화되므로 `find_turns_by_content()`와 같은 메서드는 효과적으로 작동하지 않음
    - 콘텐츠 기반 검색은 암호화된 데이터에서 수행되므로 효과가 제한됨



## 키 유도

EncryptedSession은 세션별 고유 암호화 키를 유도하기 위해 HKDF (HMAC 기반 Key Derivation Function)를 사용합니다:

- **마스터 키**: 사용자가 제공한 암호화 키
- **세션 솔트**: 세션 ID
- **Info 문자열**: `"agents.session-store.hkdf.v1"`
- **출력**: 32바이트 Fernet 키

이를 통해 다음이 보장됩니다:
- 각 세션은 고유한 암호화 키를 가짐
- 마스터 키 없이는 키를 유도할 수 없음
- 서로 다른 세션 간에 세션 데이터를 복호화할 수 없음

## 자동 만료

항목이 TTL을 초과하면 검색 중에 자동으로 건너뜁니다:

```python
# Items older than TTL are silently ignored
items = await session.get_items()  # Only returns non-expired items

# Expired items don't affect session behavior
result = await Runner.run(agent, "Continue conversation", session=session)
```

## API 레퍼런스

- [`EncryptedSession`][agents.extensions.memory.encrypt_session.EncryptedSession] - 메인 클래스
- [`Session`][agents.memory.session.Session] - 기본 세션 프로토콜