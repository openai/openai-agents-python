---
search:
  exclude: true
---
# 세션

Agents SDK는 여러 에이전트 실행에 걸쳐 대화 기록을 자동으로 유지하는 기본 제공 세션 메모리를 지원하므로, 턴 사이에 `.to_input_list()`을 수동으로 처리할 필요가 없습니다.

세션은 특정 세션의 대화 기록을 저장하므로, 명시적으로 메모리를 수동 관리하지 않아도 에이전트가 컨텍스트를 유지할 수 있습니다. 이는 에이전트가 이전 상호작용을 기억해야 하는 채팅 애플리케이션이나 멀티턴 대화를 구축할 때 특히 유용합니다.

SDK가 클라이언트 측 메모리를 대신 관리하도록 하려면 세션을 사용합니다. 동일한 실행에서 세션은 실행 수준 연속 실행 옵션인 `conversation_id`, `previous_response_id`, `auto_previous_response_id`과 함께 사용할 수 없습니다. OpenAI 서버에서 관리하는 연속 실행을 사용하려면 세션과 함께 계층화하지 말고 이러한 메커니즘 중 하나를 선택합니다.

## 빠른 시작 {#quick-start}

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

## 동일한 세션을 사용한 인터럽션된 실행 재개 {#resuming-interrupted-runs-with-the-same-session}

승인을 위해 실행이 일시 중지된 경우 동일한 세션 인스턴스(또는 동일한 세션 ID와 동일한 기본 스토리지 백엔드로 구성된 다른 인스턴스)를 사용하여 재개해야 재개된 턴이 저장된 동일한 대화 기록을 이어서 사용합니다.

```python
result = await Runner.run(agent, "Delete temporary files that are no longer needed.", session=session)

if result.interruptions:
    state = result.to_state()
    for interruption in result.interruptions:
        state.approve(interruption)
    result = await Runner.run(agent, state, session=session)
```

## 세션의 핵심 동작 {#core-session-behavior}

세션 메모리가 활성화된 경우:

1. **각 실행 전**: 러너가 세션의 대화 기록을 자동으로 가져와 입력 항목 앞에 추가합니다.
2. **각 실행 후**: 실행 중에 생성된 모든 새 항목(사용자 입력, 어시스턴트 응답, 도구 호출 등)이 세션에 자동으로 저장됩니다.
3. **컨텍스트 보존**: 동일한 세션을 사용하는 이후의 각 실행에는 전체 대화 기록이 포함되므로 에이전트가 컨텍스트를 유지할 수 있습니다.

따라서 `.to_input_list()`을 수동으로 호출하고 실행 사이의 대화 상태를 관리할 필요가 없습니다.

## 기록과 새 입력의 병합 방식 제어 {#control-how-history-and-new-input-merge}

세션을 전달하면 러너는 일반적으로 다음 순서로 모델 입력을 준비합니다.

1. 세션 기록(`session.get_items(...)`에서 가져옴)
2. 새 턴 입력

모델을 호출하기 전에 이 병합 단계를 사용자 지정하려면 [`RunConfig.session_input_callback`][agents.run.RunConfig.session_input_callback]을 사용합니다. 콜백은 다음 두 목록을 받습니다.

-   `history`: 가져온 세션 기록(이미 입력 항목 형식으로 정규화됨)
-   `new_input`: 현재 턴의 새 입력 항목

모델로 전송할 최종 입력 항목 목록을 반환합니다.

콜백은 두 목록의 복사본을 받으므로 안전하게 변경할 수 있습니다. 반환된 목록은 해당 턴의 모델 입력을 제어하지만, SDK는 여전히 새 턴에 속하는 항목만 유지합니다. 따라서 기존 기록을 재정렬하거나 필터링해도 기존 세션 항목이 새로운 입력으로 다시 저장되지 않습니다.

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

세션의 항목 저장 방식을 변경하지 않으면서 기록을 사용자 지정 방식으로 정리하거나 재정렬하거나 선택적으로 포함해야 할 때 사용합니다. 모델 호출 직전에 나중 단계의 최종 처리가 필요하다면 [에이전트 실행 가이드](../running_agents.md)의 [`call_model_input_filter`][agents.run.RunConfig.call_model_input_filter]을 사용합니다.

## 가져올 기록 제한 {#limiting-retrieved-history}

각 실행 전에 가져올 기록의 양을 제어하려면 [`SessionSettings`][agents.memory.SessionSettings]을 사용합니다.

-   `SessionSettings(limit=None)`(기본값): 사용 가능한 모든 세션 항목을 가져옵니다
-   `SessionSettings(limit=N)`: 가장 최근의 `N`개 항목만 가져옵니다

[`RunConfig.session_settings`][agents.run.RunConfig.session_settings]을 통해 실행별로 적용할 수 있습니다.

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

세션 구현이 기본 세션 설정을 제공하는 경우 `RunConfig.session_settings`에서 `None`이 아닌 각 값은 해당 실행의 대응하는 기본값을 재정의합니다. 이는 세션의 기본 동작을 변경하지 않으면서 긴 대화에서 가져올 기록의 크기를 제한하려는 경우 유용합니다.

## 메모리 작업 {#memory-operations}

### 기본 작업 {#basic-operations}

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

### 수정을 위한 pop_item 사용 {#using-pop_item-for-corrections}

`pop_item` 메서드는 대화의 마지막 항목을 실행 취소하거나 수정하려는 경우 특히 유용합니다.

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

## 기본 제공 세션 구현 {#built-in-session-implementations}

SDK는 다양한 사용 사례를 위한 여러 세션 구현을 제공합니다.

### 기본 제공 세션 구현 선택 {#choose-a-built-in-session-implementation}

아래의 자세한 예제를 읽기 전에 이 표를 사용하여 시작점을 선택합니다.

| 세션 유형 | 적합한 용도 | 참고 |
| --- | --- | --- |
| `SQLiteSession` | 로컬 개발 및 간단한 앱 | 기본 제공되며 가볍고, 파일 기반 또는 인메모리 방식 |
| `AsyncSQLiteSession` | `aiosqlite`을 사용하는 비동기 SQLite | 비동기 드라이버를 지원하는 확장 백엔드 |
| `RedisSession` | 여러 워커/서비스 간 메모리 공유 | 지연 시간이 짧은 분산 배포에 적합 |
| `SQLAlchemySession` | 기존 데이터베이스를 사용하는 프로덕션 앱 | SQLAlchemy가 지원하는 데이터베이스와 함께 작동 |
| `MongoDBSession` | 이미 MongoDB를 사용하거나 멀티프로세스 스토리지가 필요한 앱 | 비동기 pymongo 사용, 순서 지정을 위한 원자적 시퀀스 카운터 제공 |
| `DaprSession` | Dapr 사이드카를 사용하는 클라우드 네이티브 배포 | 여러 상태 저장소와 TTL 및 일관성 제어 지원 |
| `OpenAIConversationsSession` | OpenAI의 서버 관리형 스토리지 | OpenAI Conversations API 기반 기록 |
| `OpenAIResponsesCompactionSession` | 자동 압축이 필요한 긴 대화 | 다른 세션 백엔드를 감싸는 래퍼 |
| `AdvancedSQLiteSession` | SQLite와 분기/분석 기능 | 더 많은 기능을 제공하며 전용 페이지 참조 |
| `EncryptedSession` | 다른 세션에 암호화와 TTL 추가 | 래퍼이므로 먼저 기본 백엔드를 선택 |

일부 구현에는 추가 세부 정보를 제공하는 전용 페이지가 있으며, 해당 하위 섹션에 링크되어 있습니다.

ChatKit용 Python 서버를 구현하는 경우 ChatKit의 스레드 및 항목 영속성을 위해 `chatkit.store.Store` 구현을 사용합니다. `SQLAlchemySession`과 같은 Agents SDK 세션은 SDK 측 대화 기록을 관리하지만 ChatKit 스토어를 바로 대체할 수는 없습니다. [`chatkit-python` ChatKit 데이터 스토어 구현 가이드](https://github.com/openai/chatkit-python/blob/main/docs/guides/respond-to-user-message.md#implement-your-chatkit-data-store)를 참조하세요.

### OpenAI Conversations API 세션 {#openai-conversations-api-sessions}

`OpenAIConversationsSession`을 통해 [OpenAI Conversations API](https://platform.openai.com/docs/api-reference/conversations)를 사용합니다.

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

### OpenAI Responses 압축 세션 {#openai-responses-compaction-sessions}

Responses API(`responses.compact`)를 사용하여 저장된 대화 기록을 압축하려면 `OpenAIResponsesCompactionSession`을 사용합니다. 이 구현은 기본 세션을 감싸며 `should_trigger_compaction`에 따라 각 턴 이후 자동으로 압축할 수 있습니다. `OpenAIConversationsSession`을 이 구현으로 감싸지 마세요. 두 기능은 기록을 서로 다른 방식으로 관리합니다.

#### 일반적인 사용법(자동 압축) {#typical-usage-auto-compaction}

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

기본적으로 SDK는 각 턴 후에 압축 후보가 임계값을 충족하는지 확인하고, 충족하는 경우에만 압축합니다.

자동 압축이 실행되면 SDK는 `Runner.run(...)`이 반환되거나 스트리밍된 이벤트 반복자가 닫히기 전에 압축이 완료될 때까지 기다립니다. 압축 요청에서 보고된 사용량은 해당 실행의 [`Usage`](../usage.md) 합계에 포함됩니다. 기본적으로 나중에 수동으로 수행한 `run_compaction()` 호출에는 이를 포함하는 실행 컨텍스트가 없으므로 완료된 실행의 사용량 객체를 업데이트하지 않습니다.

`compaction_mode="previous_response_id"`은 압축 세션이 유지하는 Responses API 응답 ID를 사용하며, 해당 응답 체인을 계속 사용할 수 있을 때 가장 효과적입니다. 반면 `compaction_mode="input"`은 현재 세션 항목으로 압축 요청을 다시 구성하므로, 응답 체인을 사용할 수 없거나 세션 내용을 단일 진실 공급원으로 사용하려는 경우 유용합니다. 기본값인 `"auto"`는 사용할 수 있는 가장 안전한 옵션을 선택합니다.

에이전트가 `ModelSettings(store=False)`으로 실행되는 경우 Responses API는 나중에 조회할 수 있도록 마지막 응답을 유지하지 않습니다. 이러한 무상태 설정에서는 기본 `"auto"` 모드가 `previous_response_id`에 의존하는 대신 입력 기반 압축으로 대체됩니다. 전체 예제는 [`examples/memory/compaction_session_stateless_example.py`](https://github.com/openai/openai-agents-python/tree/main/examples/memory/compaction_session_stateless_example.py)을 참조하세요.

#### 자동 압축으로 인한 스트리밍 차단 {#auto-compaction-can-block-streaming}

압축은 세션 기록을 지우고 다시 작성하므로 SDK는 실행이 완료된 것으로 간주하기 전에 압축이 끝날 때까지 기다립니다. 스트리밍 모드에서는 압축 작업이 많을 경우 마지막 출력 토큰 이후에도 `run.stream_events()`이 몇 초간 열린 상태로 유지될 수 있습니다.

`OpenAIResponsesCompactionSession.run_compaction()`은 지우고 다시 작성하는 작업을 래퍼 경계에서 복구 가능한 교체 작업으로 취급합니다. 기본 기록이 변경된 후 교체 작업이 실패하거나 취소되면 래퍼는 이전 기록의 복원을 시도하며, 원래 예외나 취소가 호출자에게 전달되기 전에 해당 복구 시도가 완료될 때까지 기다립니다. 복구 중에 기본 백엔드도 실패하면 이전 기록이 복원되지 않은 상태로 남을 수 있으며 SDK는 복구 실패를 로그에 기록합니다. 래퍼는 원격 요청과 교체 또는 복구 단계를 포함한 전체 압축 작업에 걸쳐 `add_items()`, `pop_item()`, `clear_session()`을 직렬화합니다. 동시에 발생한 래퍼 변경 작업은 압축을 기다린 다음 결과 기록에 적용되므로 덮어쓰이지 않습니다. 자동 압축은 어떤 래퍼 세대가 해당 실행에 속하는지도 기록합니다. 다른 실행이 압축 시작 전에 기록을 변경하면 SDK는 최신 기록을 교체하지 않고 오래된 압축 작업을 건너뜁니다. 압축 실행 중에 기본 세션을 직접 변경하지 마세요. 직접 변경하면 이러한 래퍼 보장이 적용되지 않습니다.

지연 시간이 짧은 스트리밍이나 빠른 턴 전환이 필요하다면 자동 압축을 비활성화하고 턴 사이(또는 유휴 시간)에 `run_compaction()`을 직접 호출합니다. 자체 기준에 따라 압축을 강제로 수행할 시점을 결정할 수 있습니다.

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

### SQLite 세션 {#sqlite-sessions}

SQLite를 사용하는 기본 경량 세션 구현입니다.

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

### 비동기 SQLite 세션 {#async-sqlite-sessions}

`aiosqlite`을 기반으로 하는 SQLite 영속성이 필요한 경우 `AsyncSQLiteSession`을 사용합니다.

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

### Redis 세션 {#redis-sessions}

여러 워커 또는 서비스 간에 세션 메모리를 공유하려면 `RedisSession`을 사용합니다.

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
await session.close()
```

`from_url(...)`은 Redis 클라이언트를 생성하고 소유합니다. `close()` 이후에는 세션이 종료 상태가 되며 후속 세션 작업에서 `RuntimeError`이 발생합니다. `close()`을 반복하거나 동시에 호출해도 안전합니다. 애플리케이션에서 이미 Redis 클라이언트를 관리하는 경우 `redis_client=...`을 사용하여 `RedisSession(...)`을 직접 생성합니다. 이 경우 `close()`은 아무 작업도 하지 않으며, 호출자가 클라이언트 소유권을 유지하고 세션도 계속 사용할 수 있습니다.

### SQLAlchemy 세션 {#sqlalchemy-sessions}

SQLAlchemy가 지원하는 모든 데이터베이스를 사용하는 프로덕션 환경용 Agents SDK 세션 영속성입니다.

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

### Dapr 세션 {#dapr-sessions}

이미 Dapr 사이드카를 실행하고 있거나 에이전트 코드를 변경하지 않고 구성된 상태 저장소 백엔드를 전환하려면 `DaprSession`을 사용합니다.

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

-   `from_address(...)`은 Dapr 클라이언트를 생성하고 소유합니다. 앱에서 이미 클라이언트를 관리하는 경우 `dapr_client=...`을 사용하여 `DaprSession(...)`을 직접 생성합니다.
-   컨텍스트를 종료하거나 `close()`을 호출하면 소유 클라이언트 세션이 종료 상태가 됩니다. 이후 세션 작업에서는 `RuntimeError`이 발생하지만 `close()`을 반복하거나 동시에 호출해도 안전합니다. 주입된 클라이언트를 사용하는 경우 `close()`은 아무 작업도 하지 않으며 세션은 계속 사용할 수 있습니다.
-   기본 상태 저장소가 TTL을 지원하는 경우 `ttl=...`을 전달하면 세션 데이터에 TTL 만료가 자동으로 적용됩니다.
-   상태 쓰기 및 삭제 작업에 강력한 일관성을 요청하려면 `consistency=DAPR_CONSISTENCY_STRONG`을 전달합니다(저장소에 따라 다름). 단, 유선 수준의 읽기 일관성을 사용하려면 업스트림 Dapr Python 클라이언트가 `get_state`의 일관성 설정을 지원해야 합니다.
-   Dapr Python SDK는 HTTP 사이드카 엔드포인트도 확인합니다. 로컬 개발 환경에서는 `dapr_address`에 사용되는 gRPC 포트뿐 아니라 `--dapr-http-port 3500`을 사용하여 Dapr를 시작합니다.
-   로컬 구성 요소와 문제 해결을 포함한 전체 설정 안내는 [`examples/memory/dapr_session_example.py`](https://github.com/openai/openai-agents-python/tree/main/examples/memory/dapr_session_example.py)을 참조하세요.


### MongoDB 세션 {#mongodb-sessions}

이미 MongoDB를 사용하거나 수평 확장이 가능한 멀티프로세스 세션 스토리지가 필요한 애플리케이션에서는 `MongoDBSession`을 사용합니다.

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

-   `from_uri(...)`은 `AsyncMongoClient`을 생성하고 소유하며 `session.close()`에서 이를 닫습니다. 소유 클라이언트 세션은 `close()` 이후 종료 상태가 되며, 후속 세션 작업에서 `RuntimeError`가 발생합니다. 애플리케이션에서 이미 클라이언트를 관리하는 경우 `client=...`을 사용하여 `MongoDBSession(...)`을 직접 생성합니다. 이 경우 `session.close()`은 아무 작업도 하지 않고, 호출자가 클라이언트 수명 주기를 관리할 책임을 유지하며, 세션도 계속 사용할 수 있습니다.
-   다른 변경 없이 `mongodb+srv://user:password@cluster.example.mongodb.net` URI를 `from_uri(...)`에 전달하여 [MongoDB Atlas](https://www.mongodb.com/products/platform)에 연결합니다.
-   두 개의 컬렉션이 사용되며 두 이름 모두 `sessions_collection=`(기본값 `agent_sessions`)과 `messages_collection=`(기본값 `agent_messages`)을 통해 구성할 수 있습니다. 인덱스는 처음 사용할 때 자동으로 생성됩니다. 비어 있지 않은 각 `add_items()` 호출은 단조 증가하는 `seq`이 마지막 항목을 기준으로 배치 순서를 지정하는 논리적 배치 문서 하나를 작성합니다. 기존의 항목별 메시지 문서도 계속 읽을 수 있습니다. 논리적 배치는 MongoDB의 단일 문서 크기 제한 이내여야 하며, 크기를 초과한 배치는 일부만 저장되지 않고 원자적으로 실패합니다.
-   첫 실행 전에 연결을 확인하려면 `await session.ping()`을 사용합니다.

### 고급 SQLite 세션 {#advanced-sqlite-sessions}

대화 분기, 사용량 분석, 구조화된 쿼리를 지원하는 향상된 SQLite 세션입니다.

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

### 암호화된 세션 {#encrypted-sessions}

모든 세션 구현을 위한 투명한 암호화 래퍼입니다.

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

### 기타 세션 유형 {#other-session-types}

몇 가지 기본 제공 옵션이 더 있습니다. `examples/memory/` 및 `extensions/memory/` 아래의 소스 코드를 참조하세요.

## 운영 패턴 {#operational-patterns}

### 세션 ID 명명법 {#session-id-naming}

대화를 정리하는 데 도움이 되는 의미 있는 세션 ID를 사용합니다.

-   사용자 기반: `"user_12345"`
-   스레드 기반: `"thread_abc123"`
-   컨텍스트 기반: `"support_ticket_456"`

### 메모리 영속성 {#memory-persistence}

-   임시 대화에는 인메모리 SQLite(`SQLiteSession("session_id")`)를 사용합니다
-   영구 대화에는 파일 기반 SQLite(`SQLiteSession("session_id", "path/to/db.sqlite")`)를 사용합니다
-   `aiosqlite` 기반 구현이 필요한 경우 비동기 SQLite(`AsyncSQLiteSession("session_id", db_path="...")`)를 사용합니다
-   공유되는 저지연 세션 메모리에는 Redis 기반 세션(`RedisSession.from_url("session_id", url="redis://...")`)을 사용합니다
-   SQLAlchemy가 지원하는 기존 데이터베이스를 사용하는 프로덕션 시스템에는 SQLAlchemy 기반 세션(`SQLAlchemySession("session_id", engine=engine, create_tables=True)`)을 사용합니다
-   이미 MongoDB를 사용하거나 수평 확장이 가능한 멀티프로세스 세션 스토리지가 필요한 애플리케이션에는 MongoDB 세션(`MongoDBSession.from_uri("session_id", uri="mongodb://localhost:27017")`)을 사용합니다
-   기본 제공 원격 측정, 트레이싱, 데이터 격리 및 30개 이상의 데이터베이스 백엔드 지원이 필요한 프로덕션 클라우드 네이티브 배포에는 Dapr 상태 저장소 세션(`DaprSession.from_address("session_id", state_store_name="statestore", dapr_address="localhost:50001")`)을 사용합니다
-   OpenAI Conversations API에 기록을 저장하려는 경우 OpenAI 호스트 스토리지(`OpenAIConversationsSession()`)를 사용합니다
-   모든 세션을 투명한 암호화와 TTL 기반 만료로 감싸려면 암호화된 세션(`EncryptedSession(session_id, underlying_session, encryption_key)`)을 사용합니다
-   더 고급 사용 사례에서는 다른 프로덕션 시스템(예: Django)을 위한 사용자 지정 세션 백엔드 구현을 고려합니다

### 여러 세션 {#multiple-sessions}

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

### 세션 공유 {#session-sharing}

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

## 전체 예제 {#complete-example}

다음은 세션 메모리의 실제 동작을 보여주는 전체 예제입니다.

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

## 사용자 지정 세션 구현 {#custom-session-implementations}

[`Session`][agents.memory.session.Session] 프로토콜을 구조적으로 따르는 클래스를 생성하여 자체 세션 메모리를 구현할 수 있습니다. `SessionABC`에서 상속할 필요는 없습니다. `session_id`와 `session_settings`을 정의하고 네 개의 기록 메서드를 직접 구현합니다.

```python
from agents import Agent, Runner, SessionSettings
from agents.items import TResponseInputItem


class MyCustomSession:
    """Custom session implementation following the Session protocol."""

    session_settings: SessionSettings | None = None

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.items: list[TResponseInputItem] = []

    async def get_items(self, limit: int | None = None) -> list[TResponseInputItem]:
        if limit is None:
            return list(self.items)
        if limit <= 0:
            return []
        return list(self.items[-limit:])

    async def add_items(self, items: list[TResponseInputItem]) -> None:
        self.items.extend(items)

    async def pop_item(self) -> TResponseInputItem | None:
        return self.items.pop() if self.items else None

    async def clear_session(self) -> None:
        self.items.clear()


# Use your custom session
agent = Agent(name="Assistant")
result = await Runner.run(
    agent,
    "Hello",
    session=MyCustomSession("my_session")
)
```

### 사용자 지정 세션에서 실행 컨텍스트 접근 {#accessing-run-context-from-a-custom-session}

Agents SDK는 테넌트 라우팅, 권한 부여 또는 기타 앱별 스토리지 결정을 위해 활성 [`RunContextWrapper`][agents.run_context.RunContextWrapper]을 사용자 지정 세션에 전달할 수 있습니다. Agents SDK가 래퍼를 전달하도록 하려면 명시적인 이름을 가지며 키워드와 호환되는 `wrapper` 매개변수를 네 개의 기록 메서드 모두에 추가합니다.

```python
from typing import Any

from agents import RunContextWrapper
from agents.items import TResponseInputItem


class ContextAwareSession:
    async def get_items(
        self,
        limit: int | None = None,
        *,
        wrapper: RunContextWrapper[Any] | None = None,
    ) -> list[TResponseInputItem]: ...

    async def add_items(
        self,
        items: list[TResponseInputItem],
        *,
        wrapper: RunContextWrapper[Any] | None = None,
    ) -> None: ...

    async def pop_item(
        self,
        *,
        wrapper: RunContextWrapper[Any] | None = None,
    ) -> TResponseInputItem | None: ...

    async def clear_session(
        self,
        *,
        wrapper: RunContextWrapper[Any] | None = None,
    ) -> None: ...
```

Agents SDK는 `get_items`, `add_items`, `pop_item`, `clear_session`이 모두 `wrapper`을 선언한 경우에만 이 통합을 활성화합니다. 일반적인 `**kwargs` 매개변수는 이 시그니처 검사를 충족하지 않습니다. `wrapper`을 생략하는 기존 세션 구현은 릴리스된 호출 형태를 유지하며 변경 없이 계속 작동합니다.

## 커뮤니티 세션 구현 {#community-session-implementations}

커뮤니티에서 추가 세션 구현을 개발했습니다.

| 패키지 | 설명 |
|---------|-------------|
| [openai-django-sessions](https://pypi.org/project/openai-django-sessions/) | Django가 지원하는 모든 데이터베이스(PostgreSQL, MySQL, SQLite 등)를 위한 Django ORM 기반 세션 |

세션 구현을 만들었다면 여기에 추가할 수 있도록 문서 PR을 자유롭게 제출해 주세요!

## API 레퍼런스 {#api-reference}

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
-   [`AdvancedSQLiteSession`][agents.extensions.memory.advanced_sqlite_session.AdvancedSQLiteSession] - 분기 및 분석 기능을 갖춘 향상된 SQLite
-   [`EncryptedSession`][agents.extensions.memory.encrypt_session.EncryptedSession] - 모든 세션을 위한 암호화 래퍼