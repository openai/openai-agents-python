---
search:
  exclude: true
---
# 에이전트 실행

에이전트를 [`Runner`][agents.run.Runner] 클래스를 통해 실행할 수 있습니다. 선택지는 3가지입니다:

1. [`Runner.run()`][agents.run.Runner.run]: 비동기로 실행되며 [`RunResult`][agents.result.RunResult]를 반환
2. [`Runner.run_sync()`][agents.run.Runner.run_sync]: 동기 메서드로 내부적으로 `.run()`을 실행
3. [`Runner.run_streamed()`][agents.run.Runner.run_streamed]: 비동기로 실행되며 [`RunResultStreaming`][agents.result.RunResultStreaming]를 반환. LLM을 스트리밍 모드로 호출하고, 수신되는 대로 이벤트를 스트리밍

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

자세한 내용은 [결과 가이드](results.md)를 참조하세요.

## 에이전트 루프

`Runner`의 run 메서드를 사용할 때 시작 에이전트와 입력을 전달합니다. 입력은 문자열(사용자 메시지로 간주됨) 또는 OpenAI Responses API의 입력 아이템 리스트일 수 있습니다.

runner는 다음 루프를 실행합니다:

1. 현재 입력을 가지고 현재 에이전트에 대해 LLM을 호출합니다
2. LLM이 출력을 생성합니다
    1. LLM이 `final_output`을 반환하면 루프가 종료되고 결과를 반환합니다
    2. LLM이 핸드오프를 수행하면 현재 에이전트와 입력을 업데이트하고 루프를 다시 실행합니다
    3. LLM이 도구 호출을 생성하면 해당 도구 호출을 실행하고 결과를 추가한 뒤 루프를 다시 실행합니다
3. 전달된 `max_turns`를 초과하면 [`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded] 예외를 발생시킵니다

!!! note

    LLM 출력이 "최종 출력"으로 간주되는 규칙은, 원하는 타입의 텍스트 출력을 생성하고 도구 호출이 없을 때입니다.

## 스트리밍

스트리밍을 사용하면 LLM 실행 중 스트리밍 이벤트를 추가로 받을 수 있습니다. 스트림이 완료되면 [`RunResultStreaming`][agents.result.RunResultStreaming]에 실행에 대한 전체 정보가 포함되며, 생성된 모든 새 출력도 포함됩니다. 스트리밍 이벤트는 `.stream_events()`를 호출해 수신할 수 있습니다. 자세한 내용은 [스트리밍 가이드](streaming.md)를 참조하세요.

## 실행 구성

`run_config` 매개변수를 사용하면 에이전트 실행에 대한 전역 설정을 구성할 수 있습니다:

-   [`model`][agents.run.RunConfig.model]: 각 Agent의 `model` 설정과 무관하게 사용할 전역 LLM 모델을 설정
-   [`model_provider`][agents.run.RunConfig.model_provider]: 모델 이름을 조회할 모델 공급자, 기본값은 OpenAI
-   [`model_settings`][agents.run.RunConfig.model_settings]: 에이전트별 설정을 오버라이드. 예를 들어 전역 `temperature` 또는 `top_p`를 설정할 수 있음
-   [`input_guardrails`][agents.run.RunConfig.input_guardrails], [`output_guardrails`][agents.run.RunConfig.output_guardrails]: 모든 실행에 포함할 입력 또는 출력 가드레일 리스트
-   [`handoff_input_filter`][agents.run.RunConfig.handoff_input_filter]: 핸드오프에 이미 필터가 없는 경우 모든 핸드오프에 적용할 전역 입력 필터. 입력 필터를 사용하면 새 에이전트로 전송되는 입력을 편집할 수 있음. 자세한 내용은 [`Handoff.input_filter`][agents.handoffs.Handoff.input_filter] 문서를 참조
-   [`nest_handoff_history`][agents.run.RunConfig.nest_handoff_history]: `True`(기본값)일 때, runner는 다음 에이전트를 호출하기 전에 이전 대화 내용을 하나의 assistant 메시지로 압축. 도우미는 내용을 `<CONVERSATION HISTORY>` 블록 안에 배치하며, 이후 핸드오프가 발생하면 새 턴을 계속 추가. 원문 대화(transcript)를 그대로 전달하려면 `False`로 설정하거나 맞춤 handoff 필터를 제공. 모든 [`Runner` 메서드](agents.run.Runner)는 `RunConfig`를 전달하지 않으면 자동으로 생성하므로, 퀵스타트와 예제는 이 기본값을 자동으로 사용하며, 명시적인 [`Handoff.input_filter`][agents.handoffs.Handoff.input_filter] 콜백은 계속해서 이를 오버라이드함. 개별 핸드오프는 [`Handoff.nest_handoff_history`][agents.handoffs.Handoff.nest_handoff_history]를 통해 이 설정을 재정의할 수 있음
-   [`handoff_history_mapper`][agents.run.RunConfig.handoff_history_mapper]: `nest_handoff_history`가 `True`일 때 정규화된 대화 기록(히스토리 + 핸드오프 아이템)을 수신하는 선택적 호출 가능 객체. 다음 에이전트로 전달할 입력 아이템의 정확한 리스트를 반환해야 하며, 전체 handoff 필터를 작성하지 않고도 기본 요약을 교체할 수 있음
-   [`tracing_disabled`][agents.run.RunConfig.tracing_disabled]: 실행 전체에 대해 [트레이싱](tracing.md)을 비활성화
-   [`trace_include_sensitive_data`][agents.run.RunConfig.trace_include_sensitive_data]: LLM 및 도구 호출의 입력/출력과 같은 민감할 수 있는 데이터를 트레이스에 포함할지 구성
-   [`workflow_name`][agents.run.RunConfig.workflow_name], [`trace_id`][agents.run.RunConfig.trace_id], [`group_id`][agents.run.RunConfig.group_id]: 실행에 대한 트레이싱 워크플로 이름, 트레이스 ID 및 트레이스 그룹 ID를 설정. 최소한 `workflow_name` 설정을 권장. 그룹 ID는 선택 필드로 여러 실행에 걸친 트레이스를 연결할 수 있음
-   [`trace_metadata`][agents.run.RunConfig.trace_metadata]: 모든 트레이스에 포함할 메타데이터

기본적으로, SDK는 이제 한 에이전트가 다른 에이전트로 핸드오프할 때 이전 턴들을 단일 assistant 요약 메시지 내부에 중첩합니다. 이는 반복되는 assistant 메시지를 줄이고, 새 에이전트가 빠르게 스캔할 수 있도록 전체 대화를 단일 블록 안에 유지합니다. 레거시 동작으로 돌아가려면 `RunConfig(nest_handoff_history=False)`를 전달하거나, 대화를 필요한 그대로 전달하는 `handoff_input_filter`(또는 `handoff_history_mapper`)를 제공하세요. 특정 핸드오프에 대해 옵트아웃(또는 옵트인)하려면 `handoff(..., nest_handoff_history=False)` 또는 `True`로 설정하면 됩니다. 커스텀 매퍼를 작성하지 않고 생성된 요약에 사용되는 래퍼 텍스트를 변경하려면 [`set_conversation_history_wrappers`][agents.handoffs.set_conversation_history_wrappers]를 호출하세요(기본값 복원은 [`reset_conversation_history_wrappers`][agents.handoffs.reset_conversation_history_wrappers]).

## 대화/채팅 스레드

런 메서드를 호출하면 하나 이상의 에이전트가 실행될 수 있으며(즉, 하나 이상의 LLM 호출), 이는 채팅 대화의 단일 논리적 턴을 의미합니다. 예:

1. 사용자 턴: 사용자가 텍스트 입력
2. Runner 실행: 첫 번째 에이전트가 LLM을 호출하고 도구를 실행하며 두 번째 에이전트로 핸드오프, 두 번째 에이전트가 더 많은 도구를 실행한 후 출력을 생성

에이전트 실행이 끝나면 사용자에게 무엇을 보여줄지 선택할 수 있습니다. 예를 들어, 에이전트가 생성한 모든 새 아이템을 보여주거나 최종 출력만 보여줄 수 있습니다. 어느 쪽이든, 사용자가 후속 질문을 할 수 있으며, 그 경우 run 메서드를 다시 호출하면 됩니다.

### 수동 대화 관리

다음 턴에 대한 입력을 얻기 위해 [`RunResultBase.to_input_list()`][agents.result.RunResultBase.to_input_list] 메서드를 사용하여 대화 기록을 수동으로 관리할 수 있습니다:

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

### 세션을 통한 자동 대화 관리

더 간단한 방법으로는 [세션](sessions/index.md)을 사용해 `.to_input_list()`를 수동으로 호출하지 않고도 대화 기록을 자동으로 처리할 수 있습니다:

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

세션은 다음을 자동으로 수행합니다:

-   각 실행 전에 대화 기록을 조회
-   각 실행 후 새 메시지를 저장
-   서로 다른 세션 ID에 대해 별도의 대화를 유지

자세한 내용은 [세션 문서](sessions/index.md)를 참조하세요.


### 서버 관리형 대화

OpenAI의 conversation state 기능을 사용해 `to_input_list()` 또는 `세션`으로 로컬에서 처리하는 대신 서버 측에서 대화 상태를 관리할 수도 있습니다. 이를 통해 과거 메시지를 모두 수동으로 다시 보내지 않고도 대화 기록을 보존할 수 있습니다. 자세한 내용은 [OpenAI Conversation state 가이드](https://platform.openai.com/docs/guides/conversation-state?api-mode=responses)를 참고하세요.

OpenAI는 턴 간 상태를 추적하는 두 가지 방법을 제공합니다:

#### 1. `conversation_id` 사용

먼저 OpenAI Conversations API를 사용해 대화를 생성한 뒤, 이후 모든 호출에서 해당 ID를 재사용합니다:

```python
from agents import Agent, Runner
from openai import AsyncOpenAI

client = AsyncOpenAI()

async def main():
    agent = Agent(name="Assistant", instructions="Reply very concisely.")

    # Create a server-managed conversation
    conversation = await client.conversations.create()
    conv_id = conversation.id

    while True:
        user_input = input("You: ")
        result = await Runner.run(agent, user_input, conversation_id=conv_id)
        print(f"Assistant: {result.final_output}")
```

#### 2. `previous_response_id` 사용

또 다른 방법은 각 턴이 이전 턴의 response ID에 명시적으로 연결되는 **response chaining**입니다.

```python
from agents import Agent, Runner

async def main():
    agent = Agent(name="Assistant", instructions="Reply very concisely.")

    previous_response_id = None

    while True:
        user_input = input("You: ")

        # Setting auto_previous_response_id=True enables response chaining automatically
        # for the first turn, even when there's no actual previous response ID yet.
        result = await Runner.run(
            agent,
            user_input,
            previous_response_id=previous_response_id,
            auto_previous_response_id=True,
        )
        previous_response_id = result.last_response_id
        print(f"Assistant: {result.final_output}")
```

## 장기 실행 에이전트 및 휴먼인더루프

Agents SDK의 [Temporal](https://temporal.io/) 통합을 사용하여 내구성이 있는 장기 실행 워크플로를 운영할 수 있으며, 휴먼인더루프 작업도 포함할 수 있습니다. Temporal과 Agents SDK가 협업하여 장기 실행 작업을 완료하는 데모는 [이 동영상](https://www.youtube.com/watch?v=fFBZqzT4DD8)에서 확인하고, [문서는 여기](https://github.com/temporalio/sdk-python/tree/main/temporalio/contrib/openai_agents)에서 확인하세요.

## 예외

SDK는 특정 경우에 예외를 발생시킵니다. 전체 목록은 [`agents.exceptions`][]에 있습니다. 개요는 다음과 같습니다:

-   [`AgentsException`][agents.exceptions.AgentsException]: SDK 내에서 발생하는 모든 예외의 기본 클래스. 다른 모든 구체적 예외의 상위 타입으로 사용됨
-   [`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded]: 에이전트 실행이 `max_turns` 제한을 초과할 때 발생. `Runner.run`, `Runner.run_sync`, `Runner.run_streamed` 메서드에서 발생할 수 있으며, 지정된 상호작용 턴 수 내에 에이전트가 작업을 완료하지 못했음을 의미
-   [`ModelBehaviorError`][agents.exceptions.ModelBehaviorError]: 기반 모델(LLM)이 예기치 않거나 유효하지 않은 출력을 생성할 때 발생. 예를 들면:
    -   잘못된 JSON: 특히 특정 `output_type`이 정의된 경우, 도구 호출 또는 직접 출력에 대해 잘못된 JSON 구조를 제공하는 경우
    -   예기치 않은 도구 관련 실패: 모델이 예상한 방식으로 도구를 사용하지 못하는 경우
-   [`UserError`][agents.exceptions.UserError]: SDK를 사용하는 개발자(코드 작성자)가 SDK 사용 중 오류를 범할 때 발생. 잘못된 코드 구현, 잘못된 구성, SDK의 API 오용 등에 의해 발생
-   [`InputGuardrailTripwireTriggered`][agents.exceptions.InputGuardrailTripwireTriggered], [`OutputGuardrailTripwireTriggered`][agents.exceptions.OutputGuardrailTripwireTriggered]: 각각 입력 가드레일 또는 출력 가드레일의 조건이 충족될 때 발생. 입력 가드레일은 처리 전에 들어오는 메시지를 확인하고, 출력 가드레일은 에이전트의 최종 응답을 전달 전에 확인