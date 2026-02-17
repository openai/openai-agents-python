---
search:
  exclude: true
---
# 세션

Agents SDK는 여러 에이전트 실행에 걸쳐 대화 기록을 자동으로 유지하기 위한 내장 세션 메모리를 제공하므로, 턴 사이에 `.to_input_list()`를 수동으로 처리할 필요가 없습니다.

세션은 특정 세션의 대화 기록을 저장하여, 명시적인 수동 메모리 관리 없이도 에이전트가 컨텍스트를 유지할 수 있게 합니다. 이는 에이전트가 이전 상호작용을 기억하길 원하는 채팅 애플리케이션이나 멀티턴 대화를 구축할 때 특히 유용합니다.

## 빠른 시작

```python
from agents import Agent, Runner, SQLiteSession

# Create agent
agent = Agent(
    name="Assistant",
    instructions="Reply very concisely.",
)

# Create a session instance with a session ID
session = SQLiteSession("conversation_123")

# First turn
result = await Runner.run(
    agent,
    "What city is the Golden Gate Bridge in?",
    session=session
)
print(result.final_output)  # "San Francisco"

# Second turn - agent automatically remembers previous context
result = await Runner.run(
    agent,
    "What state is it in?",
    session=session
)
print(result.final_output)  # "California"

# Also works with synchronous runner
result = Runner.run_sync(
    agent,
    "What's the population?",
    session=session
)
print(result.final_output)  # "Approximately 39 million"
```

## 동작 방식

세션 메모리가 활성화되면:

1. **각 실행 전**: 러너가 세션의 대화 기록을 자동으로 가져와 입력 아이템 앞에 추가합니다
2. **각 실행 후**: 실행 중 생성된 모든 새 아이템(사용자 입력, 어시스턴트 응답, 도구 호출 등)이 자동으로 세션에 저장됩니다
3. **컨텍스트 보존**: 동일한 세션으로 이후 실행을 할 때마다 전체 대화 기록이 포함되어, 에이전트가 컨텍스트를 유지할 수 있습니다

이를 통해 `.to_input_list()`를 수동으로 호출하고 실행 사이의 대화 상태를 관리할 필요가 없어집니다.

## 메모리 작업

### 기본 작업

세션은 대화 기록을 관리하기 위한 여러 작업을 지원합니다:

```python
from agents import SQLiteSession

session = SQLiteSession("user_123", "conversations.db")

# Get all items in a session
items = await session.get_items()

# Add new items to a session
new_items = [
    {"role": "user", "content": "Hello"},
    {"role": "assistant", "content": "Hi there!"}
]
await session.add_items(new_items)

# Remove and return the most recent item
last_item = await session.pop_item()
print(last_item)  # {"role": "assistant", "content": "Hi there!"}

# Clear all items from a session
await session.clear_session()
```

### 수정 시 pop_item 사용

`pop_item` 메서드는 대화에서 마지막 아이템을 되돌리거나 수정하려는 경우 특히 유용합니다:

```python
from agents import Agent, Runner, SQLiteSession

agent = Agent(name="Assistant")
session = SQLiteSession("correction_example")

# Initial conversation
result = await Runner.run(
    agent,
    "What's 2 + 2?",
    session=session
)
print(f"Agent: {result.final_output}")

# User wants to correct their question
assistant_item = await session.pop_item()  # Remove agent's response
user_item = await session.pop_item()  # Remove user's question

# Ask a corrected question
result = await Runner.run(
    agent,
    "What's 2 + 3?",
    session=session
)
print(f"Agent: {result.final_output}")
```

## 세션 유형

SDK는 다양한 사용 사례를 위한 여러 세션 구현을 제공합니다:

### OpenAI Conversations API 세션

`OpenAIConversationsSession`을 통해 [OpenAI의 Conversations API](https://platform.openai.com/docs/api-reference/conversations)를 사용합니다.

```python
from agents import Agent, Runner, OpenAIConversationsSession

# Create agent
agent = Agent(
    name="Assistant",
    instructions="Reply very concisely.",
)

# Create a new conversation
session = OpenAIConversationsSession()

# Optionally resume a previous conversation by passing a conversation ID
# session = OpenAIConversationsSession(conversation_id="conv_123")

# Start conversation
result = await Runner.run(
    agent,
    "What city is the Golden Gate Bridge in?",
    session=session
)
print(result.final_output)  # "San Francisco"

# Continue the conversation
result = await Runner.run(
    agent,
    "What state is it in?",
    session=session
)
print(result.final_output)  # "California"
```

### OpenAI Responses 압축 세션

`OpenAIResponsesCompactionSession`을 사용하면 Responses API(`responses.compact`)로 세션 기록을 압축할 수 있습니다. 이는 기본 세션을 감싸는 래퍼이며, `should_trigger_compaction`에 따라 각 턴 이후 자동으로 압축할 수 있습니다.

#### 일반적인 사용(자동 압축)

```python
from agents import Agent, Runner, SQLiteSession
from agents.memory import OpenAIResponsesCompactionSession

underlying = SQLiteSession("conversation_123")
session = OpenAIResponsesCompactionSession(
    session_id="conversation_123",
    underlying_session=underlying,
)

agent = Agent(name="Assistant")
result = await Runner.run(agent, "Hello", session=session)
print(result.final_output)
```

기본적으로 후보 임계값에 도달하면 각 턴 이후 압축이 실행됩니다.

#### 자동 압축은 스트리밍을 차단할 수 있음

압축은 세션 기록을 지우고 다시 쓰므로, SDK는 압축이 끝날 때까지 실행이 완료된 것으로 간주하지 않습니다. 스트리밍 모드에서는 압축이 무거운 경우 마지막 출력 토큰 이후에도 `run.stream_events()`가 몇 초 동안 열린 상태로 유지될 수 있습니다.

지연이 낮은 스트리밍이나 빠른 턴 전환이 필요하다면 자동 압축을 비활성화하고, 턴 사이(또는 유휴 시간)에 직접 `run_compaction()`을 호출하세요. 자체 기준에 따라 언제 압축을 강제할지 결정할 수 있습니다.

```python
from agents import Agent, Runner, SQLiteSession
from agents.memory import OpenAIResponsesCompactionSession

underlying = SQLiteSession("conversation_123")
session = OpenAIResponsesCompactionSession(
    session_id="conversation_123",
    underlying_session=underlying,
    # Disable triggering the auto compaction
    should_trigger_compaction=lambda _: False,
)

agent = Agent(name="Assistant")
result = await Runner.run(agent, "Hello", session=session)

# Decide when to compact (e.g., on idle, every N turns, or size thresholds).
await session.run_compaction({"force": True})
```

### SQLite 세션

SQLite를 사용하는 기본 경량 세션 구현입니다:

```python
from agents import SQLiteSession

# In-memory database (lost when process ends)
session = SQLiteSession("user_123")

# Persistent file-based database
session = SQLiteSession("user_123", "conversations.db")

# Use the session
result = await Runner.run(
    agent,
    "Hello",
    session=session
)
```

### 비동기 SQLite 세션

`aiosqlite` 기반의 SQLite 영속성이 필요하면 `AsyncSQLiteSession`을 사용하세요.

```bash
pip install aiosqlite
```

```python
from agents import Agent, Runner
from agents.extensions.memory import AsyncSQLiteSession

agent = Agent(name="Assistant")
session = AsyncSQLiteSession("user_123", db_path="conversations.db")
result = await Runner.run(agent, "Hello", session=session)
```

### Redis 세션

여러 워커 또는 서비스 간에 공유되는 세션 메모리가 필요하면 `RedisSession`을 사용하세요.

```bash
pip install openai-agents[redis]
```

```python
from agents import Agent, Runner
from agents.extensions.memory import RedisSession

agent = Agent(name="Assistant")
session = RedisSession.from_url(
    "user_123",
    url="redis://localhost:6379/0",
)
result = await Runner.run(agent, "Hello", session=session)
```

### SQLAlchemy 세션

SQLAlchemy가 지원하는 어떤 데이터베이스든 사용할 수 있는 프로덕션 준비 세션입니다:

```python
from agents.extensions.memory import SQLAlchemySession

# Using database URL
session = SQLAlchemySession.from_url(
    "user_123",
    url="postgresql+asyncpg://user:pass@localhost/db",
    create_tables=True
)

# Using existing engine
from sqlalchemy.ext.asyncio import create_async_engine
engine = create_async_engine("postgresql+asyncpg://user:pass@localhost/db")
session = SQLAlchemySession("user_123", engine=engine, create_tables=True)
```

자세한 문서는 [SQLAlchemy Sessions](sqlalchemy_session.md)를 참고하세요.



### 고급 SQLite 세션

대화 분기, 사용량 분석, structured outputs 쿼리를 지원하는 강화된 SQLite 세션입니다:

```python
from agents.extensions.memory import AdvancedSQLiteSession

# Create with advanced features
session = AdvancedSQLiteSession(
    session_id="user_123",
    db_path="conversations.db",
    create_tables=True
)

# Automatic usage tracking
result = await Runner.run(agent, "Hello", session=session)
await session.store_run_usage(result)  # Track token usage

# Conversation branching
await session.create_branch_from_turn(2)  # Branch from turn 2
```

자세한 문서는 [Advanced SQLite Sessions](advanced_sqlite_session.md)를 참고하세요.

### 암호화된 세션

어떤 세션 구현에도 적용 가능한 투명한 암호화 래퍼입니다:

```python
from agents.extensions.memory import EncryptedSession, SQLAlchemySession

# Create underlying session
underlying_session = SQLAlchemySession.from_url(
    "user_123",
    url="sqlite+aiosqlite:///conversations.db",
    create_tables=True
)

# Wrap with encryption and TTL
session = EncryptedSession(
    session_id="user_123",
    underlying_session=underlying_session,
    encryption_key="your-secret-key",
    ttl=600  # 10 minutes
)

result = await Runner.run(agent, "Hello", session=session)
```

자세한 문서는 [Encrypted Sessions](encrypted_session.md)를 참고하세요.

### 기타 세션 유형

몇 가지 내장 옵션이 더 있습니다. `examples/memory/` 및 `extensions/memory/` 아래의 소스 코드를 참고하세요.

## 세션 관리

### 세션 ID 명명

대화를 정리하는 데 도움이 되는 의미 있는 세션 ID를 사용하세요:

-   사용자 기반: `"user_12345"`
-   스레드 기반: `"thread_abc123"`
-   컨텍스트 기반: `"support_ticket_456"`

### 메모리 영속성

-   임시 대화에는 인메모리 SQLite(`SQLiteSession("session_id")`)를 사용하세요
-   영속 대화에는 파일 기반 SQLite(`SQLiteSession("session_id", "path/to/db.sqlite")`)를 사용하세요
-   `aiosqlite` 기반 구현이 필요하면 비동기 SQLite(`AsyncSQLiteSession("session_id", db_path="...")`)를 사용하세요
-   공유되고 지연이 낮은 세션 메모리가 필요하면 Redis 백엔드 세션(`RedisSession.from_url("session_id", url="redis://...")`)을 사용하세요
-   SQLAlchemy가 지원하는 기존 데이터베이스를 사용하는 프로덕션 시스템에는 SQLAlchemy 기반 세션(`SQLAlchemySession("session_id", engine=engine, create_tables=True)`)을 사용하세요
-   내장 텔레메트리, 트레이싱, 데이터 격리를 갖추고 30개 이상의 데이터베이스 백엔드를 지원하는 프로덕션 클라우드 네이티브 배포에는 Dapr 상태 저장소 세션(`DaprSession.from_address("session_id", state_store_name="statestore", dapr_address="localhost:50001")`)을 사용하세요
-   OpenAI Conversations API에 기록을 저장하고 싶다면 OpenAI 호스트하는 스토리지(`OpenAIConversationsSession()`)를 사용하세요
-   투명한 암호화 및 TTL 기반 만료로 어떤 세션이든 감싸려면 암호화된 세션(`EncryptedSession(session_id, underlying_session, encryption_key)`)을 사용하세요
-   더 고급 사용 사례를 위해 다른 프로덕션 시스템(예: Django)을 대상으로 커스텀 세션 백엔드 구현을 고려하세요

### 여러 세션

```python
from agents import Agent, Runner, SQLiteSession

agent = Agent(name="Assistant")

# Different sessions maintain separate conversation histories
session_1 = SQLiteSession("user_123", "conversations.db")
session_2 = SQLiteSession("user_456", "conversations.db")

result1 = await Runner.run(
    agent,
    "Help me with my account",
    session=session_1
)
result2 = await Runner.run(
    agent,
    "What are my charges?",
    session=session_2
)
```

### 세션 공유

```python
# Different agents can share the same session
support_agent = Agent(name="Support")
billing_agent = Agent(name="Billing")
session = SQLiteSession("user_123")

# Both agents will see the same conversation history
result1 = await Runner.run(
    support_agent,
    "Help me with my account",
    session=session
)
result2 = await Runner.run(
    billing_agent,
    "What are my charges?",
    session=session
)
```

## 전체 예제

다음은 세션 메모리가 실제로 동작하는 모습을 보여주는 전체 예제입니다:

```python
import asyncio
from agents import Agent, Runner, SQLiteSession


async def main():
    # Create an agent
    agent = Agent(
        name="Assistant",
        instructions="Reply very concisely.",
    )

    # Create a session instance that will persist across runs
    session = SQLiteSession("conversation_123", "conversation_history.db")

    print("=== Sessions Example ===")
    print("The agent will remember previous messages automatically.\n")

    # First turn
    print("First turn:")
    print("User: What city is the Golden Gate Bridge in?")
    result = await Runner.run(
        agent,
        "What city is the Golden Gate Bridge in?",
        session=session
    )
    print(f"Assistant: {result.final_output}")
    print()

    # Second turn - the agent will remember the previous conversation
    print("Second turn:")
    print("User: What state is it in?")
    result = await Runner.run(
        agent,
        "What state is it in?",
        session=session
    )
    print(f"Assistant: {result.final_output}")
    print()

    # Third turn - continuing the conversation
    print("Third turn:")
    print("User: What's the population of that state?")
    result = await Runner.run(
        agent,
        "What's the population of that state?",
        session=session
    )
    print(f"Assistant: {result.final_output}")
    print()

    print("=== Conversation Complete ===")
    print("Notice how the agent remembered the context from previous turns!")
    print("Sessions automatically handles conversation history.")


if __name__ == "__main__":
    asyncio.run(main())
```

## 커스텀 세션 구현

[`Session`][agents.memory.session.Session] 프로토콜을 따르는 클래스를 만들어 자체 세션 메모리를 구현할 수 있습니다:

```python
from agents.memory.session import SessionABC
from agents.items import TResponseInputItem
from typing import List

class MyCustomSession(SessionABC):
    """Custom session implementation following the Session protocol."""

    def __init__(self, session_id: str):
        self.session_id = session_id
        # Your initialization here

    async def get_items(self, limit: int | None = None) -> List[TResponseInputItem]:
        """Retrieve conversation history for this session."""
        # Your implementation here
        pass

    async def add_items(self, items: List[TResponseInputItem]) -> None:
        """Store new items for this session."""
        # Your implementation here
        pass

    async def pop_item(self) -> TResponseInputItem | None:
        """Remove and return the most recent item from this session."""
        # Your implementation here
        pass

    async def clear_session(self) -> None:
        """Clear all items for this session."""
        # Your implementation here
        pass

# Use your custom session
agent = Agent(name="Assistant")
result = await Runner.run(
    agent,
    "Hello",
    session=MyCustomSession("my_session")
)
```

## 커뮤니티 세션 구현

커뮤니티에서 추가 세션 구현을 개발했습니다:

| Package | Description |
|---------|-------------|
| [openai-django-sessions](https://pypi.org/project/openai-django-sessions/) | Django가 지원하는 어떤 데이터베이스(PostgreSQL, MySQL, SQLite 등)에서도 사용할 수 있는 Django ORM 기반 세션 |

세션 구현을 만들었다면, 여기에 추가할 수 있도록 문서 PR을 제출해 주세요!

## API 레퍼런스

자세한 API 문서는 다음을 참고하세요:

-   [`Session`][agents.memory.session.Session] - 프로토콜 인터페이스
-   [`OpenAIConversationsSession`][agents.memory.OpenAIConversationsSession] - OpenAI Conversations API 구현
-   [`OpenAIResponsesCompactionSession`][agents.memory.openai_responses_compaction_session.OpenAIResponsesCompactionSession] - Responses API 압축 래퍼
-   [`SQLiteSession`][agents.memory.sqlite_session.SQLiteSession] - 기본 SQLite 구현
-   [`AsyncSQLiteSession`][agents.extensions.memory.async_sqlite_session.AsyncSQLiteSession] - `aiosqlite` 기반 비동기 SQLite 구현
-   [`RedisSession`][agents.extensions.memory.redis_session.RedisSession] - Redis 백엔드 세션 구현
-   [`SQLAlchemySession`][agents.extensions.memory.sqlalchemy_session.SQLAlchemySession] - SQLAlchemy 기반 구현
-   [`DaprSession`][agents.extensions.memory.dapr_session.DaprSession] - Dapr 상태 저장소 구현
-   [`AdvancedSQLiteSession`][agents.extensions.memory.advanced_sqlite_session.AdvancedSQLiteSession] - 분기 및 분석 기능을 갖춘 강화된 SQLite
-   [`EncryptedSession`][agents.extensions.memory.encrypt_session.EncryptedSession] - 어떤 세션에도 적용 가능한 암호화 래퍼