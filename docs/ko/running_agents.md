---
search:
  exclude: true
---
# 에이전트 실행

에이전트는 [`Runner`][agents.run.Runner] 클래스를 통해 실행할 수 있습니다. 선택지는 3가지입니다:

1. [`Runner.run()`][agents.run.Runner.run]: 비동기로 실행되며 [`RunResult`][agents.result.RunResult] 를 반환
2. [`Runner.run_sync()`][agents.run.Runner.run_sync]: 동기 메서드로, 내부적으로 `.run()` 을 실행
3. [`Runner.run_streamed()`][agents.run.Runner.run_streamed]: 비동기로 실행되며 [`RunResultStreaming`][agents.result.RunResultStreaming] 를 반환. LLM 을 스트리밍 모드로 호출하며, 수신되는 대로 이벤트를 스트리밍

```python
from agents import Agent, Runner

async def main():
    agent = Agent(name="Assistant", instructions="You are a helpful assistant")

    result = await Runner.run(agent, "Write a haiku about recursion in programming.")
    print(result.final_output)
    # Code within the code,
    # Functions calling themselves,
    # Infinite loop's dance
```

자세한 내용은 [결과 가이드](results.md)에서 확인하세요.

## 에이전트 루프

`Runner` 의 run 메서드를 사용할 때 시작 에이전트와 입력을 전달합니다. 입력은 문자열(사용자 메시지로 간주) 또는 OpenAI Responses API 의 입력 항목 리스트일 수 있습니다.

그 후 러너는 다음 루프를 실행합니다:

1. 현재 에이전트와 현재 입력으로 LLM 을 호출
2. LLM 이 출력을 생성
    1. LLM 이 `final_output` 을 반환하면 루프가 종료되고 결과를 반환
    2. LLM 이 핸드오프를 수행하면 현재 에이전트와 입력을 업데이트하고 루프를 재실행
    3. LLM 이 도구 호출을 생성하면 해당 도구 호출을 실행하고 결과를 덧붙인 뒤 루프를 재실행
3. 전달된 `max_turns` 를 초과하면 [`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded] 예외를 발생

!!! note

    LLM 출력이 "최종 출력" 으로 간주되는 규칙은, 원하는 타입의 텍스트 출력을 생성하고 도구 호출이 없는 경우입니다.

## 스트리밍

스트리밍을 사용하면 LLM 이 실행되는 동안 추가로 스트리밍 이벤트를 수신할 수 있습니다. 스트림이 완료되면 [`RunResultStreaming`][agents.result.RunResultStreaming] 에는 실행에 대한 전체 정보와 새로 생성된 모든 출력이 포함됩니다. 스트리밍 이벤트는 `.stream_events()` 를 호출해 받을 수 있습니다. 자세한 내용은 [스트리밍 가이드](streaming.md)에서 확인하세요.

## 실행 구성

`run_config` 매개변수는 에이전트 실행에 대한 전역 설정을 구성합니다:

- [`model`][agents.run.RunConfig.model]: 각 Agent 의 `model` 설정과 무관하게 사용할 전역 LLM 모델을 지정
- [`model_provider`][agents.run.RunConfig.model_provider]: 모델 이름 조회용 모델 제공자, 기본값은 OpenAI
- [`model_settings`][agents.run.RunConfig.model_settings]: 에이전트별 설정을 재정의. 예를 들어 전역 `temperature` 또는 `top_p` 를 설정 가능
- [`input_guardrails`][agents.run.RunConfig.input_guardrails], [`output_guardrails`][agents.run.RunConfig.output_guardrails]: 모든 실행에 포함할 입력/출력 가드레일 목록
- [`handoff_input_filter`][agents.run.RunConfig.handoff_input_filter]: 핸드오프에 적용할 전역 입력 필터(해당 핸드오프에 이미 없을 경우). 입력 필터를 통해 새 에이전트로 전송되는 입력을 편집할 수 있음. 자세한 내용은 [`Handoff.input_filter`][agents.handoffs.Handoff.input_filter] 문서를 참조
- [`tracing_disabled`][agents.run.RunConfig.tracing_disabled]: 전체 실행에 대해 [트레이싱](tracing.md) 비활성화 여부 설정
- [`trace_include_sensitive_data`][agents.run.RunConfig.trace_include_sensitive_data]: 트레이스에 LLM 및 도구 호출의 입출력 등 민감할 수 있는 데이터를 포함할지 설정
- [`workflow_name`][agents.run.RunConfig.workflow_name], [`trace_id`][agents.run.RunConfig.trace_id], [`group_id`][agents.run.RunConfig.group_id]: 실행에 대한 트레이싱 워크플로 이름, 트레이스 ID, 트레이스 그룹 ID 설정. 최소한 `workflow_name` 설정을 권장. 그룹 ID 는 여러 실행에 걸쳐 트레이스를 연결할 수 있는 선택 필드
- [`trace_metadata`][agents.run.RunConfig.trace_metadata]: 모든 트레이스에 포함할 메타데이터

## 대화/채팅 스레드

어느 run 메서드를 호출하든 하나 이상의 에이전트 실행(따라서 하나 이상의 LLM 호출)로 이어질 수 있지만, 이는 채팅 대화의 단일 논리 턴을 나타냅니다. 예:

1. 사용자 턴: 사용자가 텍스트 입력
2. 러너 실행: 첫 번째 에이전트가 LLM 을 호출하고 도구를 실행하고 두 번째 에이전트로 핸드오프, 두 번째 에이전트가 추가 도구를 실행한 뒤 출력을 생성

에이전트 실행이 끝나면 사용자에게 무엇을 보여줄지 선택할 수 있습니다. 예를 들어, 에이전트가 생성한 모든 새 항목을 보여주거나 최종 출력만 보여줄 수 있습니다. 어느 쪽이든 사용자가 후속 질문을 할 수 있으며, 이때 run 메서드를 다시 호출하면 됩니다.

### 수동 대화 관리

[`RunResultBase.to_input_list()`][agents.result.RunResultBase.to_input_list] 메서드를 사용해 다음 턴의 입력을 받아 대화 기록을 수동으로 관리할 수 있습니다:

```python
async def main():
    agent = Agent(name="Assistant", instructions="Reply very concisely.")

    thread_id = "thread_123"  # Example thread ID
    with trace(workflow_name="Conversation", group_id=thread_id):
        # First turn
        result = await Runner.run(agent, "What city is the Golden Gate Bridge in?")
        print(result.final_output)
        # San Francisco

        # Second turn
        new_input = result.to_input_list() + [{"role": "user", "content": "What state is it in?"}]
        result = await Runner.run(agent, new_input)
        print(result.final_output)
        # California
```

### Sessions 를 통한 자동 대화 관리

더 간단한 접근 방식으로, [Sessions](sessions/index.md) 를 사용하면 `.to_input_list()` 를 수동으로 호출하지 않고도 대화 기록을 자동으로 처리할 수 있습니다:

```python
from agents import Agent, Runner, SQLiteSession

async def main():
    agent = Agent(name="Assistant", instructions="Reply very concisely.")

    # Create session instance
    session = SQLiteSession("conversation_123")

    thread_id = "thread_123"  # Example thread ID
    with trace(workflow_name="Conversation", group_id=thread_id):
        # First turn
        result = await Runner.run(agent, "What city is the Golden Gate Bridge in?", session=session)
        print(result.final_output)
        # San Francisco

        # Second turn - agent automatically remembers previous context
        result = await Runner.run(agent, "What state is it in?", session=session)
        print(result.final_output)
        # California
```

Sessions 는 자동으로 다음을 수행합니다:

- 각 실행 전 대화 기록을 가져옴
- 각 실행 후 새 메시지를 저장
- 서로 다른 세션 ID 별로 대화를 분리 관리

자세한 내용은 [Sessions 문서](sessions/index.md)를 참조하세요.


### 서버 관리 대화

`to_input_list()` 또는 `Sessions` 로 로컬에서 처리하는 대신, OpenAI conversation state 기능에 서버 측 대화 상태 관리를 맡길 수도 있습니다. 이를 통해 과거 메시지를 모두 수동으로 재전송하지 않고도 대화 기록을 보존할 수 있습니다. 자세한 내용은 [OpenAI Conversation state 가이드](https://platform.openai.com/docs/guides/conversation-state?api-mode=responses)에서 확인하세요.

OpenAI 는 턴 간 상태를 추적하는 두 가지 방법을 제공합니다:

#### 1. `conversation_id` 사용

먼저 OpenAI Conversations API 를 사용해 대화를 생성한 뒤, 이후 모든 호출에서 해당 ID 를 재사용합니다:

```python
from agents import Agent, Runner
from openai import AsyncOpenAI

client = AsyncOpenAI()

async def main():
    # Create a server-managed conversation
    conversation = await client.conversations.create()
    conv_id = conversation.id    

    agent = Agent(name="Assistant", instructions="Reply very concisely.")

    # First turn
    result1 = await Runner.run(agent, "What city is the Golden Gate Bridge in?", conversation_id=conv_id)
    print(result1.final_output)
    # San Francisco

    # Second turn reuses the same conversation_id
    result2 = await Runner.run(
        agent,
        "What state is it in?",
        conversation_id=conv_id,
    )
    print(result2.final_output)
    # California
```

#### 2. `previous_response_id` 사용

다른 옵션은 **response chaining** 으로, 각 턴이 이전 턴의 response ID 에 명시적으로 연결됩니다.

```python
from agents import Agent, Runner

async def main():
    agent = Agent(name="Assistant", instructions="Reply very concisely.")

    # First turn
    result1 = await Runner.run(agent, "What city is the Golden Gate Bridge in?")
    print(result1.final_output)
    # San Francisco

    # Second turn, chained to the previous response
    result2 = await Runner.run(
        agent,
        "What state is it in?",
        previous_response_id=result1.last_response_id,
    )
    print(result2.final_output)
    # California
```


## 장기 실행 에이전트 및 휴먼인더루프 (HITL)

Agents SDK 의 [Temporal](https://temporal.io/) 통합을 사용하여 내구성이 있는 장기 실행 워크플로, 휴먼인더루프 작업을 포함해 실행할 수 있습니다. Temporal 과 Agents SDK 가 장기 실행 작업을 완료하는 데 함께 동작하는 데모는 [이 영상](https://www.youtube.com/watch?v=fFBZqzT4DD8)에서 볼 수 있으며, [문서는 여기](https://github.com/temporalio/sdk-python/tree/main/temporalio/contrib/openai_agents)에서 확인하세요.

## 예외

SDK 는 특정 상황에서 예외를 발생시킵니다. 전체 목록은 [`agents.exceptions`][] 에 있습니다. 개요는 다음과 같습니다:

- [`AgentsException`][agents.exceptions.AgentsException]: SDK 내에서 발생하는 모든 예외의 기본 클래스. 다른 모든 구체적 예외의 상위 타입으로 사용
- [`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded]: 에이전트 실행이 `Runner.run`, `Runner.run_sync`, `Runner.run_streamed` 메서드에 전달된 `max_turns` 한도를 초과했을 때 발생. 에이전트가 지정된 상호작용 턴 수 내에 작업을 완료하지 못했음을 의미
- [`ModelBehaviorError`][agents.exceptions.ModelBehaviorError]: 기반 모델(LLM) 이 예상치 못했거나 잘못된 출력을 생성했을 때 발생. 예를 들면:
    - 형식이 잘못된 JSON: 도구 호출 또는 직접 출력에서 잘못된 JSON 구조를 제공하는 경우, 특히 특정 `output_type` 이 정의된 경우
    - 예상치 못한 도구 관련 실패: 모델이 도구를 예상된 방식으로 사용하지 못한 경우
- [`UserError`][agents.exceptions.UserError]: SDK 를 사용하는 개발자(코드를 작성하는 사람) 가 SDK 사용 중 오류를 일으켰을 때 발생. 보통 잘못된 코드 구현, 유효하지 않은 구성, SDK API 오사용에서 기인
- [`InputGuardrailTripwireTriggered`][agents.exceptions.InputGuardrailTripwireTriggered], [`OutputGuardrailTripwireTriggered`][agents.exceptions.OutputGuardrailTripwireTriggered]: 각각 입력 가드레일 또는 출력 가드레일의 조건이 충족될 때 발생. 입력 가드레일은 처리 전에 수신 메시지를 검사하고, 출력 가드레일은 에이전트의 최종 응답을 전달하기 전에 검사