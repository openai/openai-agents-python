---
search:
  exclude: true
---
# 암호화된 세션

`EncryptedSession`는 모든 세션 구현체에 투명한 암호화를 제공하며, 오래된 항목을 자동으로 만료시켜 대화 데이터를 보호합니다.

## 기능 {#features}

- **투명한 암호화**: 모든 세션을 Fernet 암호화로 래핑
- **세션별 키**: HKDF 키 파생을 사용하여 세션마다 고유한 암호화 적용
- **자동 만료**: TTL이 만료되면 오래된 항목을 자동으로 건너뜀
- **즉시 교체 가능**: 기존의 모든 세션 구현체와 호환

## 설치 {#installation}

암호화된 세션에는 `encrypt` extra가 필요합니다.

```bash
pip install 'openai-agents[encrypt]'
```

## 빠른 시작 {#quick-start}

이 예제에서는 인메모리 `SQLiteSession`을 사용합니다. 기본 제공 세션에는 별도의 데이터베이스 드라이버가 필요하지 않습니다.

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

## 구성 {#configuration}

### 암호화 키 {#encryption-key}

암호화 키로 Fernet 키 또는 임의의 문자열을 사용할 수 있습니다.

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

### TTL(유효 기간) {#ttl-time-to-live}

암호화된 항목이 유효하게 유지되는 기간을 설정합니다.

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

## 다양한 세션 유형에서의 사용 {#usage-with-different-session-types}

### SQLite 세션 연동 {#with-sqlite-sessions}

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

### SQLAlchemy 세션 연동 {#with-sqlalchemy-sessions}

아래 PostgreSQL 예제를 사용하려면 `encrypt` 및 `sqlalchemy` extras를 설치합니다. `sqlalchemy` extra에는 `postgresql+asyncpg://` URL에서 사용하는 `asyncpg` 드라이버가 포함되어 있습니다.

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

`SQLAlchemySession.from_url()`에서 생성한 엔진을 폐기하는 작업은 애플리케이션이 담당합니다. 해당 엔진을 사용하는 모든 `SQLAlchemySession` 인스턴스가 더 이상 필요하지 않으면 실행 실패 여부와 관계없이 애플리케이션의 정리 경로에서 `await underlying.engine.dispose()`을 호출합니다.

!!! warning "고급 세션 기능"

    `AdvancedSQLiteSession`와 같은 고급 세션 구현체에서 `EncryptedSession`을 사용할 때는 다음 사항에 유의하세요.

    - 메시지 콘텐츠가 암호화되므로 `find_turns_by_content()`과 같은 메서드는 효과적으로 작동하지 않음
    - 콘텐츠 기반 검색은 암호화된 데이터에서 수행되므로 효과가 제한됨



## 키 파생 {#key-derivation}

EncryptedSession은 HKDF(HMAC 기반 키 파생 함수)를 사용하여 세션마다 고유한 암호화 키를 파생합니다.

- **마스터 키**: 제공한 암호화 키
- **세션 솔트**: 세션 ID
- **정보 문자열**: `"agents.session-store.hkdf.v1"`
- **출력**: 32바이트 Fernet 키

이를 통해 다음을 보장합니다.
- 각 세션에 고유한 암호화 키가 있음
- 마스터 키 없이는 키를 파생할 수 없음
- 서로 다른 세션 간에는 세션 데이터를 복호화할 수 없음

## 자동 만료 {#automatic-expiration}

항목이 TTL을 초과하면 조회 시 자동으로 건너뜁니다.

```python
# Items older than TTL are silently ignored
items = await session.get_items()  # Only returns non-expired items

# Expired items don't affect session behavior
result = await Runner.run(agent, "Continue conversation", session=session)
```

## API 레퍼런스 {#api-reference}

- [`EncryptedSession`][agents.extensions.memory.encrypt_session.EncryptedSession] - 기본 클래스
- [`Session`][agents.memory.session.Session] - 기본 세션 프로토콜