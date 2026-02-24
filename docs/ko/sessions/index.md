---
search:
  exclude: true
---
# 세션

Agents SDK는 여러 에이전트 실행에 걸쳐 대화 기록을 자동으로 유지하기 위한 내장 세션 메모리를 제공하므로, 턴 사이에 `.to_input_list()`를 수동으로 처리할 필요가 없습니다.

세션은 특정 세션의 대화 기록을 저장하여, 에이전트가 명시적인 수동 메모리 관리 없이도 컨텍스트를 유지할 수 있게 합니다. 이는 에이전트가 이전 상호작용을 기억하길 원하는 채팅 애플리케이션이나 멀티턴 대화를 구축할 때 특히 유용합니다.

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

## 핵심 세션 동작

세션 메모리가 활성화되면:

1. **각 실행 전**: 러너가 세션의 대화 기록을 자동으로 가져와 입력 아이템 앞에 덧붙입니다
2. **각 실행 후**: 실행 중 생성된 모든 새 아이템(사용자 입력, 어시스턴트 응답, 도구 호출 등)이 자동으로 세션에 저장됩니다
3. **컨텍스트 보존**: 동일한 세션으로 이후 실행을 수행하면 전체 대화 기록이 포함되어, 에이전트가 컨텍스트를 유지할 수 있습니다

이를 통해 `.to_input_list()`를 수동으로 호출하고 실행 간 대화 상태를 관리할 필요가 없어집니다.

## 기록과 새 입력의 병합 방식 제어

세션을 전달하면 러너는 일반적으로 모델 입력을 다음과 같이 준비합니다:

1. 세션 기록(`session.get_items(...)`에서 가져옴)
2. 새 턴 입력

모델 호출 전에 병합 단계를 커스터마이즈하려면 [`RunConfig.session_input_callback`][agents.run.RunConfig.session_input_callback]을 사용하세요. 콜백은 두 개의 리스트를 받습니다:

-   `history`: 가져온 세션 기록(이미 입력-아이템 형식으로 정규화됨)
-   `new_input`: 현재 턴의 새 입력 아이템

모델에 보내야 하는 최종 입력 아이템 리스트를 반환하세요.

```python
from agents import Agent, RunConfig, Runner, SQLiteSession


def keep_recent_history(history, new_input):
    # Keep only the last 10 history items, then append the new turn.
    return history[-10:] + new_input


agent = Agent(name="Assistant")
session = SQLiteSession("conversation_123")

result = await Runner.run(
    agent,
    "Continue from the latest updates only.",
    session=session,
    run_config=RunConfig(session_input_callback=keep_recent_history),
)
```

세션이 아이템을 저장하는 방식은 바꾸지 않으면서, 기록의 커스텀 가지치기, 재정렬, 선택적 포함이 필요할 때 사용하세요.

## 가져오는 기록의 제한

각 실행 전에 가져오는 기록의 양을 제어하려면 [`SessionSettings`][agents.memory.SessionSettings]를 사용하세요.

-   `SessionSettings(limit=None)` (기본값): 사용 가능한 모든 세션 아이템을 가져옵니다
-   `SessionSettings(limit=N)`: 가장 최근 `N`개 아이템만 가져옵니다

실행별로는 [`RunConfig.session_settings`][agents.run.RunConfig.session_settings]를 통해 적용할 수 있습니다:

```python
from agents import Agent, RunConfig, Runner, SessionSettings, SQLiteSession

agent = Agent(name="Assistant")
session = SQLiteSession("conversation_123")

result = await Runner.run(
    agent,
    "Summarize our recent discussion.",
    session=session,
    run_config=RunConfig(session_settings=SessionSettings(limit=50)),
)
```

세션 구현이 기본 세션 설정을 제공하는 경우, `RunConfig.session_settings`는 해당 실행에서 `None`이 아닌 값에 대해 이를 덮어씁니다. 이는 긴 대화에서 세션의 기본 동작을 바꾸지 않으면서도 가져오기 크기에 상한을 두고 싶을 때 유용합니다.

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

### 수정용 pop_item 사용

`pop_item` 메서드는 대화에서 마지막 아이템을 되돌리거나 수정하고 싶을 때 특히 유용합니다:

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

## 내장 세션 구현

SDK는 다양한 사용 사례를 위한 여러 세션 구현을 제공합니다:

### 내장 세션 구현 선택

아래의 자세한 예제를 읽기 전에, 이 표를 사용해 시작점을 선택하세요.

| 세션 유형 | 적합한 용도 | 비고 |
| --- | --- | --- |
| `SQLiteSession` | 로컬 개발 및 간단한 앱 | 내장, 경량, 파일 기반 또는 인메모리 |
| `AsyncSQLiteSession` | `aiosqlite`를 사용하는 비동기 SQLite | 비동기 드라이버 지원을 위한 확장 백엔드 |
| `RedisSession` | 워커/서비스 간 공유 메모리 | 저지연 분산 배포에 적합 |
| `SQLAlchemySession` | 기존 데이터베이스가 있는 프로덕션 앱 | SQLAlchemy가 지원하는 데이터베이스에서 동작 |
| `OpenAIConversationsSession` | OpenAI에서 서버 관리 스토리지 | OpenAI Conversations API 기반 기록 |
| `OpenAIResponsesCompactionSession` | 자동 컴팩션이 있는 긴 대화 | 다른 세션 백엔드에 대한 래퍼 |
| `AdvancedSQLiteSession` | 분기/분석 기능이 있는 SQLite | 더 무거운 기능 세트; 전용 페이지 참고 |
| `EncryptedSession` | 다른 세션 위에 암호화 + TTL | 래퍼; 먼저 기반 백엔드를 선택 |

일부 구현은 추가 세부 정보가 있는 전용 페이지가 있으며, 해당 구현의 하위 섹션에서 인라인으로 링크됩니다.

### OpenAI Conversations API 세션

`OpenAIConversationsSession`을 통해 [OpenAI's Conversations API](https://platform.openai.com/docs/api-reference/conversations)를 사용하세요.

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

### OpenAI Responses 컴팩션 세션

Responses API(`responses.compact`)로 세션 기록을 컴팩트하려면 `OpenAIResponsesCompactionSession`을 사용하세요. 이는 기반 세션을 감싸며 `should_trigger_compaction`에 따라 각 턴 이후 자동으로 컴팩트할 수 있습니다.

#### 일반적인 사용(자동 컴팩션)

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

기본적으로 후보 임계값에 도달하면 각 턴 이후 컴팩션이 실행됩니다.

#### 자동 컴팩션은 스트리밍을 블로킹할 수 있음

컴팩션은 세션 기록을 지우고 다시 작성하므로, SDK는 컴팩션이 끝나기 전까지 실행이 완료되었다고 보지 않습니다. 스트리밍 모드에서는 컴팩션이 무거운 경우 마지막 출력 토큰 이후에도 `run.stream_events()`가 몇 초 동안 열린 상태로 남을 수 있습니다.

저지연 스트리밍이나 빠른 턴 전환이 필요하다면 자동 컴팩션을 비활성화하고, 턴 사이(또는 유휴 시간)에 `run_compaction()`을 직접 호출하세요. 자체 기준에 따라 언제 컴팩션을 강제할지 결정할 수 있습니다.

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

`aiosqlite` 기반으로 SQLite 영속성을 원한다면 `AsyncSQLiteSession`을 사용하세요.

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

여러 워커 또는 서비스 전반에서 공유되는 세션 메모리가 필요하면 `RedisSession`을 사용하세요.

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

SQLAlchemy가 지원하는 어떤 데이터베이스든 사용하는 프로덕션 준비 세션입니다:

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



### Advanced SQLite 세션

대화 분기, 사용량 분석, structured queries가 포함된 향상된 SQLite 세션입니다:

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

### 암호화 세션

어떤 세션 구현에도 적용할 수 있는 투명한 암호화 래퍼입니다:

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

내장 옵션이 몇 가지 더 있습니다. `examples/memory/` 및 `extensions/memory/` 아래의 소스 코드를 참고하세요.

## 운영 패턴

### 세션 ID 네이밍

대화를 정리하는 데 도움이 되는 의미 있는 세션 ID를 사용하세요:

-   사용자 기반: `"user_12345"`
-   스레드 기반: `"thread_abc123"`
-   컨텍스트 기반: `"support_ticket_456"`

### 메모리 영속성

-   임시 대화에는 인메모리 SQLite(`SQLiteSession("session_id")`)를 사용하세요
-   영속 대화에는 파일 기반 SQLite(`SQLiteSession("session_id", "path/to/db.sqlite")`)를 사용하세요
-   `aiosqlite` 기반 구현이 필요하면 비동기 SQLite(`AsyncSQLiteSession("session_id", db_path="...")`)를 사용하세요
-   공유되고 저지연인 세션 메모리가 필요하면 Redis 백엔드 세션(`RedisSession.from_url("session_id", url="redis://...")`)을 사용하세요
-   SQLAlchemy가 지원하는 기존 데이터베이스가 있는 프로덕션 시스템에는 SQLAlchemy 기반 세션(`SQLAlchemySession("session_id", engine=engine, create_tables=True)`)을 사용하세요
-   내장 텔레메트리, 트레이싱, 데이터 격리를 갖춘 30개 이상의 데이터베이스 백엔드를 지원하는 프로덕션 클라우드 네이티브 배포에는 Dapr state store 세션(`DaprSession.from_address("session_id", state_store_name="statestore", dapr_address="localhost:50001")`)을 사용하세요
-   OpenAI Conversations API에 기록을 저장하고 싶다면 OpenAI 호스트하는 스토리지(`OpenAIConversationsSession()`)를 사용하세요
-   어떤 세션이든 투명한 암호화 및 TTL 기반 만료를 적용하려면 암호화 세션(`EncryptedSession(session_id, underlying_session, encryption_key)`)으로 감싸세요
-   더 고급 사용 사례를 위해 (예: Django) 다른 프로덕션 시스템용 커스텀 세션 백엔드 구현도 고려하세요

### 다중 세션

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

다음은 세션 메모리가 실제로 동작하는 전체 예제입니다:

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

| 패키지 | 설명 |
|---------|-------------|
| [openai-django-sessions](https://pypi.org/project/openai-django-sessions/) | Django ORM 기반 세션으로, Django가 지원하는 모든 데이터베이스(PostgreSQL, MySQL, SQLite 등)에 사용 가능 |

세션 구현을 만들었다면, 여기에 추가할 수 있도록 문서 PR을 제출해 주세요!

## API 레퍼런스

자세한 API 문서는 다음을 참고하세요:

-   [`Session`][agents.memory.session.Session] - 프로토콜 인터페이스
-   [`OpenAIConversationsSession`][agents.memory.OpenAIConversationsSession] - OpenAI Conversations API 구현
-   [`OpenAIResponsesCompactionSession`][agents.memory.openai_responses_compaction_session.OpenAIResponsesCompactionSession] - Responses API 컴팩션 래퍼
-   [`SQLiteSession`][agents.memory.sqlite_session.SQLiteSession] - 기본 SQLite 구현
-   [`AsyncSQLiteSession`][agents.extensions.memory.async_sqlite_session.AsyncSQLiteSession] - `aiosqlite` 기반 비동기 SQLite 구현
-   [`RedisSession`][agents.extensions.memory.redis_session.RedisSession] - Redis 백엔드 세션 구현
-   [`SQLAlchemySession`][agents.extensions.memory.sqlalchemy_session.SQLAlchemySession] - SQLAlchemy 기반 구현
-   [`DaprSession`][agents.extensions.memory.dapr_session.DaprSession] - Dapr state store 구현
-   [`AdvancedSQLiteSession`][agents.extensions.memory.advanced_sqlite_session.AdvancedSQLiteSession] - 분기 및 분석 기능이 있는 향상된 SQLite
-   [`EncryptedSession`][agents.extensions.memory.encrypt_session.EncryptedSession] - 어떤 세션에도 적용 가능한 암호화 래퍼