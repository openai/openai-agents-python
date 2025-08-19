---
search:
  exclude: true
---
# 세션

Agents SDK 는 여러 에이전트 실행(run) 간의 대화 이력을 자동으로 유지하는 내장 세션 메모리를 제공합니다. 이를 통해 턴 사이에 `.to_input_list()` 를 수동으로 처리할 필요가 없습니다.

Sessions 는 특정 세션의 대화 이력을 저장하여, 명시적인 수동 메모리 관리 없이도 에이전트가 문맥을 유지할 수 있게 합니다. 이는 에이전트가 이전 상호작용을 기억해야 하는 채팅 애플리케이션이나 멀티 턴 대화에 특히 유용합니다.

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

1. **각 실행 전**: 러너가 세션의 대화 이력을 자동으로 가져와 입력 항목 앞에 추가합니다
2. **각 실행 후**: 실행 중 생성된 모든 새 항목(사용자 입력, 어시스턴트 응답, 도구 호출 등)이 세션에 자동으로 저장됩니다
3. **문맥 보존**: 동일한 세션에서 이후 실행은 전체 대화 이력을 포함하므로 에이전트가 문맥을 유지할 수 있습니다

이로써 `.to_input_list()` 를 수동으로 호출하고 실행 사이의 대화 상태를 관리할 필요가 없어집니다.

## 메모리 작업

### 기본 작업

Sessions 는 대화 이력을 관리하기 위한 여러 작업을 지원합니다:

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

### 교정을 위한 pop_item 사용

`pop_item` 메서드는 대화에서 마지막 항목을 되돌리거나 수정하고 싶을 때 특히 유용합니다:

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

## 메모리 옵션

### 메모리 없음(기본값)

```python
# Default behavior - no session memory
result = await Runner.run(agent, "Hello")
```

### SQLite 메모리

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

### 다중 세션

```python
from agents import Agent, Runner, SQLiteSession

agent = Agent(name="Assistant")

# Different sessions maintain separate conversation histories
session_1 = SQLiteSession("user_123", "conversations.db")
session_2 = SQLiteSession("user_456", "conversations.db")

result1 = await Runner.run(
    agent,
    "Hello",
    session=session_1
)
result2 = await Runner.run(
    agent,
    "Hello",
    session=session_2
)
```

## 사용자 지정 메모리 구현

[`Session`][agents.memory.session.Session] 프로토콜을 따르는 클래스를 만들어 직접 세션 메모리를 구현할 수 있습니다:

```python
from agents.memory import Session
from typing import List

class MyCustomSession:
    """Custom session implementation following the Session protocol."""

    def __init__(self, session_id: str):
        self.session_id = session_id
        # Your initialization here

    async def get_items(self, limit: int | None = None) -> List[dict]:
        """Retrieve conversation history for this session."""
        # Your implementation here
        pass

    async def add_items(self, items: List[dict]) -> None:
        """Store new items for this session."""
        # Your implementation here
        pass

    async def pop_item(self) -> dict | None:
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

## 세션 관리

### 세션 ID 명명

다음과 같이 대화를 체계적으로 정리할 수 있는 의미 있는 세션 ID 를 사용하세요:

-   사용자 기반: `"user_12345"`
-   스레드 기반: `"thread_abc123"`
-   컨텍스트 기반: `"support_ticket_456"`

### 메모리 지속성

-   임시 대화에는 인메모리 SQLite(`SQLiteSession("session_id")`) 사용
-   영구 대화에는 파일 기반 SQLite(`SQLiteSession("session_id", "path/to/db.sqlite")`) 사용
-   프로덕션 시스템에서는 사용자 지정 세션 백엔드(Redis, PostgreSQL 등) 구현 고려

### 세션 관리

```python
# Clear a session when conversation should start fresh
await session.clear_session()

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

세션 메모리가 동작하는 전체 예제:

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

## API 레퍼런스

자세한 API 문서는 다음을 참조하세요:

-   [`Session`][agents.memory.Session] - 프로토콜 인터페이스
-   [`SQLiteSession`][agents.memory.SQLiteSession] - SQLite 구현