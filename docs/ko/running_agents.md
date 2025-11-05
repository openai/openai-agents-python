---
search:
  exclude: true
---
# 에이전트 실행

에이전트는 [`Runner`][agents.run.Runner] 클래스를 통해 실행할 수 있습니다. 다음 3가지 옵션이 있습니다:

1. [`Runner.run()`][agents.run.Runner.run]: 비동기로 실행되며 [`RunResult`][agents.result.RunResult]를 반환합니다.
2. [`Runner.run_sync()`][agents.run.Runner.run_sync]: 동기 메서드로, 내부적으로 `.run()`을 실행합니다.
3. [`Runner.run_streamed()`][agents.run.Runner.run_streamed]: 비동기로 실행되며 [`RunResultStreaming`][agents.result.RunResultStreaming]을 반환합니다. LLM을 스트리밍 모드로 호출하고, 수신되는 대로 이벤트를 스트림합니다.

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

자세한 내용은 [결과 가이드](results.md)를 참고하세요.

## 에이전트 루프

`Runner`의 run 메서드를 사용할 때 시작 에이전트와 입력을 전달합니다. 입력은 문자열(사용자 메시지로 간주됨) 또는 OpenAI Responses API의 입력 항목 목록일 수 있습니다.

그다음 러너는 다음 루프를 실행합니다:

1. 현재 입력과 함께 현재 에이전트에 대해 LLM을 호출합니다.
2. LLM이 출력을 생성합니다.
    1. LLM이 `final_output`을 반환하면 루프가 종료되고 결과를 반환합니다.
    2. LLM이 핸드오프를 수행하면 현재 에이전트와 입력을 업데이트하고 루프를 다시 실행합니다.
    3. LLM이 도구 호출을 생성하면 해당 도구 호출을 실행하고 결과를 추가한 뒤 루프를 다시 실행합니다.
3. 전달된 `max_turns`를 초과하면 [`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded] 예외를 발생시킵니다.

!!! note

    LLM 출력이 "최종 출력"으로 간주되는 규칙은, 원하는 유형의 텍스트 출력을 생성하고 도구 호출이 없을 때입니다.

## 스트리밍

스트리밍을 사용하면 LLM 실행 중 스트리밍 이벤트를 추가로 수신할 수 있습니다. 스트림이 완료되면 [`RunResultStreaming`][agents.result.RunResultStreaming]에 실행에 대한 모든 최신 출력이 포함된 전체 정보가 담깁니다. 스트리밍 이벤트는 `.stream_events()`를 호출하면 됩니다. 자세한 내용은 [스트리밍 가이드](streaming.md)를 참고하세요.

## 실행 구성

`run_config` 매개변수는 에이전트 실행에 대한 전역 설정을 구성합니다:

-   [`model`][agents.run.RunConfig.model]: 각 Agent의 `model`과 무관하게 사용할 전역 LLM 모델을 설정
-   [`model_provider`][agents.run.RunConfig.model_provider]: 모델 이름 조회용 모델 공급자이며, 기본값은 OpenAI
-   [`model_settings`][agents.run.RunConfig.model_settings]: 에이전트별 설정을 재정의. 예: 전역 `temperature` 또는 `top_p` 설정
-   [`input_guardrails`][agents.run.RunConfig.input_guardrails], [`output_guardrails`][agents.run.RunConfig.output_guardrails]: 모든 실행에 포함할 입력/출력 가드레일 목록
-   [`handoff_input_filter`][agents.run.RunConfig.handoff_input_filter]: 핸드오프에 전역 입력 필터를 적용. 핸드오프에 이미 필터가 없을 때만 적용되며, 새 에이전트로 전달되는 입력을 편집할 수 있음. 자세한 내용은 [`Handoff.input_filter`][agents.handoffs.Handoff.input_filter] 문서를 참고
-   [`tracing_disabled`][agents.run.RunConfig.tracing_disabled]: 전체 실행에 대해 [트레이싱](tracing.md)을 비활성화
-   [`trace_include_sensitive_data`][agents.run.RunConfig.trace_include_sensitive_data]: LLM 및 도구 호출의 입력/출력 등 민감할 수 있는 데이터를 트레이스에 포함할지 설정
-   [`workflow_name`][agents.run.RunConfig.workflow_name], [`trace_id`][agents.run.RunConfig.trace_id], [`group_id`][agents.run.RunConfig.group_id]: 실행의 트레이싱 워크플로 이름, 트레이스 ID, 트레이스 그룹 ID를 설정. 최소한 `workflow_name` 설정을 권장. 그룹 ID는 여러 실행에 걸쳐 트레이스를 연결하는 선택적 필드
-   [`trace_metadata`][agents.run.RunConfig.trace_metadata]: 모든 트레이스에 포함할 메타데이터

## 대화/채팅 스레드

어느 실행 메서드를 호출하든 하나 이상의 에이전트가 실행될 수 있으며(따라서 하나 이상의 LLM 호출), 채팅 대화에서 단일 논리적 턴을 나타냅니다. 예:

1. 사용자 턴: 사용자가 텍스트를 입력
2. Runner 실행: 첫 번째 에이전트가 LLM을 호출하고 도구를 실행한 후 두 번째 에이전트로 핸드오프, 두 번째 에이전트가 추가 도구를 실행하고 출력을 생성

에이전트 실행이 끝나면 사용자에게 무엇을 보여줄지 선택할 수 있습니다. 예를 들어, 에이전트가 생성한 모든 새로운 항목을 보여주거나 최종 출력만 보여줄 수 있습니다. 어느 쪽이든 사용자가 후속 질문을 할 수 있으며, 이 경우 다시 run 메서드를 호출하면 됩니다.

### 수동 대화 관리

다음 턴의 입력을 얻기 위해 [`RunResultBase.to_input_list()`][agents.result.RunResultBase.to_input_list] 메서드를 사용하여 대화 기록을 수동으로 관리할 수 있습니다:

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

### Sessions를 통한 자동 대화 관리

더 간단한 접근으로, [Sessions](sessions/index.md)를 사용하면 `.to_input_list()`를 직접 호출하지 않고도 대화 기록을 자동으로 처리할 수 있습니다:

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

Sessions는 다음을 자동으로 수행합니다:

-   각 실행 전 대화 기록을 가져옴
-   각 실행 후 새 메시지를 저장
-   서로 다른 세션 ID에 대해 별도의 대화를 유지

자세한 내용은 [Sessions 문서](sessions/index.md)를 참고하세요.


### 서버 관리형 대화

OpenAI 대화 상태 기능을 사용하여 `to_input_list()`나 `Sessions`로 로컬에서 처리하는 대신 서버 측에서 대화 상태를 관리할 수도 있습니다. 이를 통해 모든 과거 메시지를 수동으로 다시 보내지 않고도 대화 기록을 보존할 수 있습니다. 자세한 내용은 [OpenAI Conversation state 가이드](https://platform.openai.com/docs/guides/conversation-state?api-mode=responses)를 참고하세요.

OpenAI는 턴 간 상태를 추적하는 두 가지 방법을 제공합니다:

#### 1. `conversation_id` 사용

먼저 OpenAI Conversations API로 대화를 생성한 다음, 이후 모든 호출에서 해당 ID를 재사용합니다:

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

또 다른 옵션은 **response chaining**으로, 각 턴이 이전 턴의 response ID에 명시적으로 연결됩니다.

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


## 장시간 실행 에이전트 및 휴먼인더루프 (HITL)

Agents SDK의 [Temporal](https://temporal.io/) 통합을 사용하면 내구성이 있는 장시간 워크플로(휴먼인더루프 (HITL) 작업 포함)를 실행할 수 있습니다. 장시간 작업을 완료하기 위해 Temporal과 Agents SDK가 함께 작동하는 데모는 [이 동영상](https://www.youtube.com/watch?v=fFBZqzT4DD8)에서 확인하고, [관련 문서](https://github.com/temporalio/sdk-python/tree/main/temporalio/contrib/openai_agents)를 참고하세요.

## 예외

SDK는 특정 경우에 예외를 발생시킵니다. 전체 목록은 [`agents.exceptions`][]에 있습니다. 개요는 다음과 같습니다:

-   [`AgentsException`][agents.exceptions.AgentsException]: SDK 내에서 발생하는 모든 예외의 기본 클래스입니다. 다른 모든 구체적 예외가 파생되는 일반적인 타입입니다.
-   [`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded]: `Runner.run`, `Runner.run_sync`, `Runner.run_streamed` 메서드에 전달된 `max_turns` 한도를 에이전트 실행이 초과할 때 발생합니다. 에이전트가 지정된 상호작용 턴 수 내에 작업을 완료하지 못했음을 나타냅니다.
-   [`ModelBehaviorError`][agents.exceptions.ModelBehaviorError]: 기반 모델(LLM)이 예상치 못한 또는 잘못된 출력을 생성할 때 발생합니다. 다음을 포함할 수 있습니다:
    -   잘못된 JSON: 특히 특정 `output_type`이 정의된 경우, 도구 호출이나 직접 출력에서 잘못된 JSON 구조를 제공하는 경우
    -   예기치 않은 도구 관련 실패: 모델이 기대한 방식으로 도구를 사용하지 못한 경우
-   [`UserError`][agents.exceptions.UserError]: SDK를 사용하는 사람이 SDK 사용 중 오류를 범했을 때 발생합니다. 잘못된 코드 구현, 올바르지 않은 구성, SDK API의 오용 등에서 비롯됩니다.
-   [`InputGuardrailTripwireTriggered`][agents.exceptions.InputGuardrailTripwireTriggered], [`OutputGuardrailTripwireTriggered`][agents.exceptions.OutputGuardrailTripwireTriggered]: 각각 입력 가드레일 또는 출력 가드레일의 조건이 충족될 때 발생합니다. 입력 가드레일은 처리 전에 들어오는 메시지를 확인하고, 출력 가드레일은 에이전트의 최종 응답을 전달하기 전에 확인합니다.