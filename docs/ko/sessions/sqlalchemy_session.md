---
search:
  exclude: true
---
# SQLAlchemy 세션

`SQLAlchemySession`은 SQLAlchemy를 사용하여 프로덕션 환경에 적합한 세션 구현을 제공합니다. 따라서 SQLAlchemy가 지원하는 모든 데이터베이스(PostgreSQL, MySQL, SQLite 등)를 세션 스토리지로 사용할 수 있습니다.

## 설치

SQLAlchemy 세션에는 `sqlalchemy` 추가 의존성이 필요합니다.

```bash
pip install openai-agents[sqlalchemy]
```

## 빠른 시작

### 데이터베이스 URL 사용

가장 간단하게 시작하는 방법은 다음과 같습니다.

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

### 기존 엔진 사용

기존 SQLAlchemy 엔진이 있는 애플리케이션에서는 다음과 같이 사용합니다.

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

## 비 ASCII 텍스트 저장

기본적으로 `SQLAlchemySession`은 세션 항목을 JSON으로 직렬화할 때 비 ASCII 문자를 이스케이프합니다. 이렇게 하면 기존 스토리지 형식을 유지하면서도 항목을 로드할 때 원본 텍스트를 그대로 복원할 수 있습니다.

저장된 JSON에서 다국어 텍스트를 읽을 수 있는 형태로 유지하려면 `ensure_ascii=False`를 설정합니다.

```python
session = SQLAlchemySession.from_url(
    "user-123",
    url="sqlite+aiosqlite:///conversations.db",
    create_tables=True,
    ensure_ascii=False,
)
```

기존 엔진을 사용할 때는 동일한 옵션을 `SQLAlchemySession(...)`에 직접 전달할 수 있습니다. 이 설정은 데이터베이스에 저장되는 JSON 표현만 변경하며, 세션 메서드가 반환하는 값은 변경하지 않습니다.


## API 레퍼런스

- [`SQLAlchemySession`][agents.extensions.memory.sqlalchemy_session.SQLAlchemySession] - 기본 클래스
- [`Session`][agents.memory.session.Session] - 기본 세션 프로토콜