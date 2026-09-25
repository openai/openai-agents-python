---
search:
  exclude: true
---
# 암호화된 세션

`EncryptedSession`은 모든 세션 구현에 투명한 암호화를 제공하며, 오래된 항목을 자동으로 만료시켜 대화 데이터를 보호합니다.

## 기능 {#features}

- **투명한 암호화**: 모든 세션을 Fernet 암호화로 래핑
- **세션별 키**: HKDF 키 파생을 사용하여 세션마다 고유한 암호화 적용
- **자동 만료**: TTL이 만료되면 오래된 항목을 자동으로 건너뜀
- **즉시 교체 가능**: 기존의 모든 세션 구현과 호환

## 설치 {#installation}

암호화된 세션에는 `encrypt` extra가 필요합니다.

```bash
pip install 'openai-agents[encrypt]'
```

## 빠른 시작 {#quick-start}

이 예제에서는 인메모리 `SQLiteSession`을 사용하고 실행할 때마다 새로운 암호화 키를 생성합니다. 기본 제공 세션에는 별도의 데이터베이스 드라이버가 필요하지 않습니다. 영구 저장소를 사용하는 경우 프로세스를 다시 시작해도 키를 보존하고 재사용할 수 있도록 [암호화 키 지침](#encryption-key)을 따르세요.

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

## 구성 {#configuration}

### 암호화 키 {#encryption-key}

[`Fernet.generate_key()`](https://cryptography.io/en/latest/fernet/#cryptography.fernet.Fernet.generate_key)로 생성한 키나 애플리케이션의 비밀 관리 시스템에서 프로비저닝한 엔트로피가 높은 무작위 비밀 값처럼 암호학적으로 안전하고 엔트로피가 높은 마스터 키를 사용하세요. 비밀번호, 기억하기 쉬운 문구 또는 하드 코딩된 예시 값을 암호화 키로 사용하지 마세요.

```python
from cryptography.fernet import Fernet

# Generate once when provisioning a new key, then store the value securely.
encryption_key = Fernet.generate_key().decode("ascii")
```

영구 저장소를 사용하는 경우 키를 한 번 생성하고, 데이터를 암호화하는 데 사용하기 전에 해당 값을 비밀 관리자나 다른 보안 저장소에 저장하세요. 프로세스를 시작할 때마다 동일한 키를 로드하세요. 다음 코드 조각에서는 배포 환경이 저장된 키를 `SESSION_ENCRYPTION_KEY`로 주입한다고 가정합니다. 이 환경 변수 이름은 애플리케이션의 규칙이며 SDK 설정이 아닙니다.

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

키를 비밀로 유지하고 암호화된 세션 데이터베이스와 별도로 보관하세요. 재시작 후 기존 데이터를 읽으려면 동일한 마스터 키와 동일한 `session_id`을 사용하세요. 시작 시 대체 키를 생성하면 애플리케이션에서 기존 레코드를 복호화할 수 없습니다. `encryption_key`을 변경해도 저장된 레코드가 다시 암호화되지는 않습니다. 기존 레코드에는 여전히 원래 키가 필요하며 해당 TTL도 계속 적용됩니다.

`EncryptedSession`은 이전 버전과의 호환성을 위해 계속해서 raw 문자열을 허용합니다. 이는 비밀번호나 엔트로피가 낮은 다른 비밀 값을 사용하라는 의미가 아닙니다. SDK는 세션별 키 파생에 HKDF를 사용하지만, 비밀번호 강화에는 사용하지 않습니다. 세션 ID 솔트는 세션 키를 서로 분리하지만 비밀 엔트로피를 추가하지는 않습니다. 아래의 [키 파생](#key-derivation)을 참고하세요.

### TTL(유효 기간) {#ttl-time-to-live}

암호화된 항목이 유효하게 유지되는 기간을 설정합니다. 다음 코드 조각에서는 위에서 로드한 `encryption_key`을 재사용합니다.

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

## 다양한 세션 유형과 함께 사용 {#usage-with-different-session-types}

### SQLite 세션과 함께 사용 {#with-sqlite-sessions}

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

### SQLAlchemy 세션과 함께 사용 {#with-sqlalchemy-sessions}

아래 PostgreSQL 예제에는 `encrypt` 및 `sqlalchemy` extra를 설치하세요. `sqlalchemy` extra에는 `postgresql+asyncpg://` URL에서 사용하는 `asyncpg` 드라이버가 포함되어 있습니다.

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

애플리케이션은 `SQLAlchemySession.from_url()`에서 생성한 엔진을 해제할 책임이 있습니다. 해당 엔진을 사용하는 모든 `SQLAlchemySession` 인스턴스가 더 이상 필요하지 않으면 실행이 실패한 경우에도 애플리케이션의 정리 경로에서 `await underlying.engine.dispose()`을 호출하세요.

!!! warning "고급 세션 기능"

    `AdvancedSQLiteSession`과 같은 고급 세션 구현에서 `EncryptedSession`을 사용할 때는 다음 사항에 유의하세요.

    - 메시지 콘텐츠가 암호화되므로 `find_turns_by_content()`과 같은 메서드는 효과적으로 작동하지 않음
    - 콘텐츠 기반 검색은 암호화된 데이터에 대해 수행되므로 효과가 제한됨



## 키 파생 {#key-derivation}

EncryptedSession은 HKDF(HMAC 기반 키 파생 함수)를 사용하여 세션마다 고유한 암호화 키를 파생합니다.

- **마스터 키**: 사용자가 제공한 암호화 키
- **세션 솔트**: 세션 ID
- **정보 문자열**: `"agents.session-store.hkdf.v1"`
- **출력**: 32바이트 Fernet 키

엔트로피가 높은 마스터 키를 사용하면 서로 다른 세션 ID에서 서로 다른 파생 키가 생성됩니다. 동일한 마스터 키와 세션 ID를 재사용하면 동일한 파생 키가 생성되므로 애플리케이션에서 이전에 저장한 만료되지 않은 항목을 복호화할 수 있습니다.

HKDF를 사용한다고 해서 약한 마스터 키가 비밀번호 추측 공격에 강해지는 것은 아닙니다. 세션 ID는 비밀이 아닌 솔트이며 추가적인 비밀 키 자료가 아닙니다. 키 파생과 비밀번호 강화의 차이점은 [cryptography HKDF 문서](https://cryptography.io/en/latest/hazmat/primitives/key-derivation-functions/#hkdf)를 참고하세요.

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