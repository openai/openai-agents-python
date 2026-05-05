---
search:
  exclude: true
---
# 세션

Agents SDK는 여러 에이전트 실행에 걸쳐 대화 기록을 자동으로 유지하는 내장 세션 메모리를 제공하여, 턴 사이에 `.to_input_list()`를 수동으로 처리할 필요를 없애줍니다.

세션은 특정 세션의 대화 기록을 저장하여, 명시적인 수동 메모리 관리 없이도 에이전트가 컨텍스트를 유지할 수 있게 합니다. 이는 특히 에이전트가 이전 상호작용을 기억하길 원하는 채팅 애플리케이션이나 멀티턴 대화를 구축할 때 유용합니다.

SDK가 클라이언트 측 메모리를 관리하도록 하려면 세션을 사용하세요. 세션은 동일한 실행에서 `conversation_id`, `previous_response_id`, `auto_previous_response_id`와 함께 사용할 수 없습니다. 대신 OpenAI 서버 관리형 이어가기를 원한다면, 세션을 그 위에 겹쳐 쓰지 말고 이러한 메커니즘 중 하나를 선택하세요.

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

## 동일한 세션으로 인터럽트된 실행 재개

승인을 위해 실행이 일시 중지되면, 재개된 턴이 동일한 저장된 대화 기록을 이어가도록 같은 세션 인스턴스(또는 동일한 백킹 스토어를 가리키는 다른 세션 인스턴스)로 재개하세요.

```python
result = await Runner.run(agent, "Delete temporary files that are no longer needed.", session=session)

if result.interruptions:
    state = result.to_state()
    for interruption in result.interruptions:
        state.approve(interruption)
    result = await Runner.run(agent, state, session=session)
```

## 핵심 세션 동작

세션 메모리가 활성화되면:

1. **각 실행 전**: 러너가 세션의 대화 기록을 자동으로 가져와 입력 항목 앞에 추가합니다.
2. **각 실행 후**: 실행 중 생성된 모든 새 항목(사용자 입력, 어시스턴트 응답, 도구 호출 등)이 세션에 자동으로 저장됩니다.
3. **컨텍스트 보존**: 동일한 세션으로 이어지는 각 실행에는 전체 대화 기록이 포함되어 에이전트가 컨텍스트를 유지할 수 있습니다.

이를 통해 `.to_input_list()`를 수동으로 호출하고 실행 간 대화 상태를 관리할 필요가 없어집니다.

## 기록과 새 입력 병합 제어

세션을 전달하면, 러너는 일반적으로 모델 입력을 다음과 같이 준비합니다.

1. 세션 기록(`session.get_items(...)`에서 가져옴)
2. 새 턴 입력

모델 호출 전에 이 병합 단계를 사용자 지정하려면 [`RunConfig.session_input_callback`][agents.run.RunConfig.session_input_callback]을 사용하세요. 콜백은 두 개의 목록을 받습니다.

-   `history`: 가져온 세션 기록(이미 입력 항목 형식으로 정규화됨)
-   `new_input`: 현재 턴의 새 입력 항목

모델에 전송할 최종 입력 항목 목록을 반환하세요.

콜백은 두 목록의 복사본을 받으므로 안전하게 변경할 수 있습니다. 반환된 목록은 해당 턴의 모델 입력을 제어하지만, SDK는 여전히 새 턴에 속한 항목만 저장합니다. 따라서 오래된 기록을 재정렬하거나 필터링해도 오래된 세션 항목이 새 입력으로 다시 저장되지는 않습니다.

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

세션이 항목을 저장하는 방식을 바꾸지 않으면서 기록을 사용자 지정 가지치기, 재정렬 또는 선택적으로 포함해야 할 때 사용하세요. 모델 호출 직전에 한 번 더 최종 처리가 필요하다면 [running agents guide](../running_agents.md)의 [`call_model_input_filter`][agents.run.RunConfig.call_model_input_filter]를 사용하세요.

## 가져오는 기록 제한

각 실행 전에 가져올 기록의 양을 제어하려면 [`SessionSettings`][agents.memory.SessionSettings]를 사용하세요.

-   `SessionSettings(limit=None)`(기본값): 사용 가능한 모든 세션 항목 가져오기
-   `SessionSettings(limit=N)`: 가장 최근 `N`개 항목만 가져오기

[`RunConfig.session_settings`][agents.run.RunConfig.session_settings]를 통해 실행별로 이를 적용할 수 있습니다.

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

세션 구현이 기본 세션 설정을 노출하는 경우, `RunConfig.session_settings`는 해당 실행에서 `None`이 아닌 값을 재정의합니다. 이는 긴 대화에서 세션의 기본 동작을 바꾸지 않고 가져오기 크기를 제한하고 싶을 때 유용합니다.

## 메모리 작업

### 기본 작업

세션은 대화 기록을 관리하기 위한 여러 작업을 지원합니다.

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

### 수정을 위한 pop_item 사용

`pop_item` 메서드는 대화의 마지막 항목을 되돌리거나 수정하려는 경우 특히 유용합니다.

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

SDK는 다양한 사용 사례에 맞는 여러 세션 구현을 제공합니다.

### 내장 세션 구현 선택

아래의 상세 예제를 읽기 전에 이 표를 사용해 출발점을 선택하세요.

| 세션 유형 | 적합한 경우 | 참고 |
| --- | --- | --- |
| `SQLiteSession` | 로컬 개발 및 간단한 앱 | 내장형, 경량, 파일 기반 또는 인메모리 |
| `AsyncSQLiteSession` | `aiosqlite`를 사용하는 비동기 SQLite | 비동기 드라이버 지원이 있는 확장 백엔드 |
| `RedisSession` | 워커/서비스 간 공유 메모리 | 저지연 분산 배포에 적합 |
| `SQLAlchemySession` | 기존 데이터베이스를 사용하는 프로덕션 앱 | SQLAlchemy 지원 데이터베이스와 작동 |
| `MongoDBSession` | 이미 MongoDB를 사용하거나 멀티프로세스 스토리지가 필요한 앱 | 비동기 pymongo; 순서 보장을 위한 원자적 시퀀스 카운터 |
| `DaprSession` | Dapr 사이드카를 사용하는 클라우드 네이티브 배포 | 여러 상태 저장소와 TTL 및 일관성 제어 지원 |
| `OpenAIConversationsSession` | OpenAI의 서버 관리형 스토리지 | OpenAI Conversations API 기반 기록 |
| `OpenAIResponsesCompactionSession` | 자동 압축이 필요한 긴 대화 | 다른 세션 백엔드를 감싸는 래퍼 |
| `AdvancedSQLiteSession` | SQLite와 브랜칭/분석 | 더 무거운 기능 세트; 전용 페이지 참조 |
| `EncryptedSession` | 다른 세션 위의 암호화 + TTL | 래퍼; 먼저 기반 백엔드 선택 |

일부 구현에는 추가 세부 정보를 담은 전용 페이지가 있으며, 각 하위 섹션에 인라인으로 링크되어 있습니다.

ChatKit용 파이썬 서버를 구현하는 경우 ChatKit의 스레드 및 항목 영속성을 위해 `chatkit.store.Store` 구현을 사용하세요. `SQLAlchemySession` 같은 Agents SDK 세션은 SDK 측 대화 기록을 관리하지만, ChatKit의 스토어를 대체하는 드롭인 대체품은 아닙니다. [`ChatKit 데이터 스토어 구현에 대한 chatkit-python 가이드`](https://github.com/openai/chatkit-python/blob/main/docs/guides/respond-to-user-message.md#implement-your-chatkit-data-store)를 참조하세요.

### OpenAI Conversations API 세션

`OpenAIConversationsSession`을 통해 [OpenAI의 Conversations API](https://platform.openai.com/docs/api-reference/conversations)를 사용하세요.

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

Responses API(`responses.compact`)로 저장된 대화 기록을 압축하려면 `OpenAIResponsesCompactionSession`을 사용하세요. 이는 기반 세션을 감싸며 `should_trigger_compaction`에 따라 각 턴 후 자동으로 압축할 수 있습니다. `OpenAIConversationsSession`을 이것으로 감싸지 마세요. 두 기능은 서로 다른 방식으로 기록을 관리합니다.

#### 일반적인 사용법(자동 압축)

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

기본적으로, 후보 임계값에 도달하면 각 턴 후 압축이 실행됩니다.

`compaction_mode="previous_response_id"`는 Responses API 응답 ID로 이미 턴을 체이닝하고 있을 때 가장 잘 작동합니다. `compaction_mode="input"`은 대신 현재 세션 항목에서 압축 요청을 다시 구성하므로, 응답 체인을 사용할 수 없거나 세션 내용이 진실의 원천이 되길 원할 때 유용합니다. 기본값 `"auto"`는 사용 가능한 가장 안전한 옵션을 선택합니다.

에이전트가 `ModelSettings(store=False)`로 실행되면 Responses API는 나중 조회를 위해 마지막 응답을 보관하지 않습니다. 이러한 무상태 설정에서는 기본 `"auto"` 모드가 `previous_response_id`에 의존하는 대신 입력 기반 압축으로 폴백합니다. 전체 예제는 [`examples/memory/compaction_session_stateless_example.py`](https://github.com/openai/openai-agents-python/tree/main/examples/memory/compaction_session_stateless_example.py)를 참조하세요.

#### 자동 압축과 스트리밍 차단 가능성

압축은 세션 기록을 지우고 다시 쓰므로, SDK는 실행을 완료로 간주하기 전에 압축이 끝날 때까지 기다립니다. 스트리밍 모드에서는 압축이 무거운 경우 마지막 출력 토큰 이후에도 `run.stream_events()`가 몇 초 동안 열려 있을 수 있음을 의미합니다.

저지연 스트리밍이나 빠른 턴 전환을 원한다면 자동 압축을 비활성화하고 턴 사이(또는 유휴 시간)에 직접 `run_compaction()`을 호출하세요. 자체 기준에 따라 언제 압축을 강제할지 결정할 수 있습니다.

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

SQLite를 사용하는 기본 경량 세션 구현:

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

`aiosqlite` 기반 SQLite 영속성이 필요할 때 `AsyncSQLiteSession`을 사용하세요.

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

여러 워커나 서비스 간 공유 세션 메모리에는 `RedisSession`을 사용하세요.

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

SQLAlchemy가 지원하는 모든 데이터베이스를 사용한 프로덕션 준비 Agents SDK 세션 영속성:

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

자세한 문서는 [SQLAlchemy 세션](sqlalchemy_session.md)을 참조하세요.

### Dapr 세션

이미 Dapr 사이드카를 실행 중이거나 에이전트 코드를 변경하지 않고 다양한 상태 저장소 백엔드 간 이동할 수 있는 세션 스토리지가 필요할 때 `DaprSession`을 사용하세요.

```bash
pip install openai-agents[dapr]
```

```python
from agents import Agent, Runner
from agents.extensions.memory import DaprSession

agent = Agent(name="Assistant")

async with DaprSession.from_address(
    "user_123",
    state_store_name="statestore",
    dapr_address="localhost:50001",
) as session:
    result = await Runner.run(agent, "Hello", session=session)
    print(result.final_output)
```

참고:

-   `from_address(...)`는 Dapr 클라이언트를 생성하고 소유합니다. 앱에서 이미 클라이언트를 관리한다면 `dapr_client=...`로 `DaprSession(...)`을 직접 생성하세요.
-   백킹 상태 저장소가 TTL을 지원하는 경우 오래된 세션 데이터가 자동으로 만료되도록 `ttl=...`을 전달하세요.
-   더 강한 쓰기 후 읽기 보장이 필요할 때는 `consistency=DAPR_CONSISTENCY_STRONG`을 전달하세요.
-   Dapr Python SDK는 HTTP 사이드카 엔드포인트도 확인합니다. 로컬 개발에서는 `dapr_address`에서 사용하는 gRPC 포트와 함께 `--dapr-http-port 3500`으로 Dapr를 시작하세요.
-   로컬 컴포넌트와 문제 해결을 포함한 전체 설정 절차는 [`examples/memory/dapr_session_example.py`](https://github.com/openai/openai-agents-python/tree/main/examples/memory/dapr_session_example.py)를 참조하세요.


### MongoDB 세션

이미 MongoDB를 사용하거나 수평 확장 가능한 멀티프로세스 세션 스토리지가 필요한 애플리케이션에는 `MongoDBSession`을 사용하세요.

```bash
pip install openai-agents[mongodb]
```

```python
from agents import Agent, Runner
from agents.extensions.memory import MongoDBSession

agent = Agent(name="Assistant")

# Create from URI — owns the client and closes it when session.close() is called
session = MongoDBSession.from_uri(
    "user-123",
    uri="mongodb://localhost:27017",
    database="agents",
)
result = await Runner.run(agent, "Hello", session=session)
print(result.final_output)
await session.close()
```

참고:

-   `from_uri(...)`는 `AsyncMongoClient`를 생성하고 소유하며 `session.close()`에서 이를 닫습니다. 애플리케이션에서 이미 클라이언트를 관리한다면 `client=...`로 `MongoDBSession(...)`을 직접 생성하세요. 이 경우 `session.close()`는 아무 작업도 하지 않으며 라이프사이클은 호출자에게 있습니다.
-   다른 변경 없이 `from_uri(...)`에 `mongodb+srv://user:password@cluster.example.mongodb.net` URI를 전달하여 [MongoDB Atlas](https://www.mongodb.com/products/platform)에 연결하세요.
-   두 컬렉션이 사용되며 두 이름 모두 `sessions_collection=`(기본값 `agent_sessions`)과 `messages_collection=`(기본값 `agent_messages`)를 통해 구성할 수 있습니다. 인덱스는 처음 사용할 때 자동으로 생성됩니다. 각 메시지 문서에는 동시 작성자와 프로세스 간 순서를 보존하는 단조 증가 `seq` 카운터가 포함됩니다.
-   첫 실행 전에 연결성을 확인하려면 `await session.ping()`을 사용하세요.

### 고급 SQLite 세션

대화 브랜칭, 사용량 분석, structured queries를 갖춘 향상된 SQLite 세션:

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

자세한 문서는 [고급 SQLite 세션](advanced_sqlite_session.md)을 참조하세요.

### 암호화된 세션

모든 세션 구현을 위한 투명한 암호화 래퍼:

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

자세한 문서는 [암호화된 세션](encrypted_session.md)을 참조하세요.

### 기타 세션 유형

몇 가지 내장 옵션이 더 있습니다. `examples/memory/` 및 `extensions/memory/` 아래의 소스 코드를 참조하세요.

## 운영 패턴

### 세션 ID 명명

대화를 정리하는 데 도움이 되는 의미 있는 세션 ID를 사용하세요.

-   사용자 기반: `"user_12345"`
-   스레드 기반: `"thread_abc123"`
-   컨텍스트 기반: `"support_ticket_456"`

### 메모리 영속성

-   임시 대화에는 인메모리 SQLite(`SQLiteSession("session_id")`) 사용
-   영속 대화에는 파일 기반 SQLite(`SQLiteSession("session_id", "path/to/db.sqlite")`) 사용
-   `aiosqlite` 기반 구현이 필요할 때는 비동기 SQLite(`AsyncSQLiteSession("session_id", db_path="...")`) 사용
-   공유 저지연 세션 메모리에는 Redis 기반 세션(`RedisSession.from_url("session_id", url="redis://...")`) 사용
-   SQLAlchemy가 지원하는 기존 데이터베이스가 있는 프로덕션 시스템에는 SQLAlchemy 기반 세션(`SQLAlchemySession("session_id", engine=engine, create_tables=True)`) 사용
-   이미 MongoDB를 사용하거나 멀티프로세스, 수평 확장 가능한 세션 스토리지가 필요한 애플리케이션에는 MongoDB 세션(`MongoDBSession.from_uri("session_id", uri="mongodb://localhost:27017")`) 사용
-   내장 텔레메트리, 트레이싱 및 데이터 격리를 갖춘 30개 이상의 데이터베이스 백엔드를 지원하는 프로덕션 클라우드 네이티브 배포에는 Dapr 상태 저장소 세션(`DaprSession.from_address("session_id", state_store_name="statestore", dapr_address="localhost:50001")`) 사용
-   OpenAI Conversations API에 기록을 저장하는 것을 선호한다면 OpenAI 호스팅 스토리지(`OpenAIConversationsSession()`) 사용
-   모든 세션을 투명한 암호화 및 TTL 기반 만료로 감싸려면 암호화된 세션(`EncryptedSession(session_id, underlying_session, encryption_key)`) 사용
-   더 고급 사용 사례를 위해 다른 프로덕션 시스템(예: Django)에 대한 사용자 지정 세션 백엔드 구현 고려

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

다음은 세션 메모리가 동작하는 모습을 보여주는 전체 예제입니다.

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

## 사용자 지정 세션 구현

[`Session`][agents.memory.session.Session] 프로토콜을 따르는 클래스를 만들어 자체 세션 메모리를 구현할 수 있습니다.

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

커뮤니티에서 추가 세션 구현을 개발했습니다.

| 패키지 | 설명 |
|---------|-------------|
| [openai-django-sessions](https://pypi.org/project/openai-django-sessions/) | Django가 지원하는 모든 데이터베이스(PostgreSQL, MySQL, SQLite 등)를 위한 Django ORM 기반 세션 |

세션 구현을 구축했다면 여기에 추가할 수 있도록 문서 PR을 자유롭게 제출해 주세요!

## API 참조

자세한 API 문서는 다음을 참조하세요.

-   [`Session`][agents.memory.session.Session] - 프로토콜 인터페이스
-   [`OpenAIConversationsSession`][agents.memory.OpenAIConversationsSession] - OpenAI Conversations API 구현
-   [`OpenAIResponsesCompactionSession`][agents.memory.openai_responses_compaction_session.OpenAIResponsesCompactionSession] - Responses API 압축 래퍼
-   [`SQLiteSession`][agents.memory.sqlite_session.SQLiteSession] - 기본 SQLite 구현
-   [`AsyncSQLiteSession`][agents.extensions.memory.async_sqlite_session.AsyncSQLiteSession] - `aiosqlite` 기반 비동기 SQLite 구현
-   [`RedisSession`][agents.extensions.memory.redis_session.RedisSession] - Redis 기반 세션 구현
-   [`SQLAlchemySession`][agents.extensions.memory.sqlalchemy_session.SQLAlchemySession] - SQLAlchemy 기반 구현
-   [`MongoDBSession`][agents.extensions.memory.mongodb_session.MongoDBSession] - MongoDB 기반 세션 구현
-   [`DaprSession`][agents.extensions.memory.dapr_session.DaprSession] - Dapr 상태 저장소 구현
-   [`AdvancedSQLiteSession`][agents.extensions.memory.advanced_sqlite_session.AdvancedSQLiteSession] - 브랜칭과 분석을 갖춘 향상된 SQLite
-   [`EncryptedSession`][agents.extensions.memory.encrypt_session.EncryptedSession] - 모든 세션을 위한 암호화 래퍼