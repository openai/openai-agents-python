---
search:
  exclude: true
---
# 에이전트 실행

[`Runner`][agents.run.Runner] 클래스를 통해 에이전트를 실행할 수 있습니다. 방법은 3가지입니다:

1. [`Runner.run()`][agents.run.Runner.run]: 비동기로 실행되며 [`RunResult`][agents.result.RunResult] 를 반환
2. [`Runner.run_sync()`][agents.run.Runner.run_sync]: 동기 메서드로, 내부적으로 `.run()` 을 실행
3. [`Runner.run_streamed()`][agents.run.Runner.run_streamed]: 비동기로 실행되며 [`RunResultStreaming`][agents.result.RunResultStreaming] 을 반환. LLM 을 스트리밍 모드로 호출하고 수신되는 대로 이벤트를 스트리밍함

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

`Runner` 의 run 메서드를 사용할 때 시작 에이전트와 입력을 전달합니다. 입력은 문자열(사용자 메시지로 간주) 또는 OpenAI Responses API 의 입력 아이템 목록이 될 수 있습니다.

러너는 다음과 같은 루프를 실행합니다:

1. 현재 에이전트와 현재 입력으로 LLM 을 호출합니다
2. LLM 이 출력을 생성합니다
    1. LLM 이 `final_output` 을 반환하면 루프를 종료하고 결과를 반환합니다
    2. LLM 이 핸드오프를 수행하면 현재 에이전트와 입력을 업데이트하고 루프를 다시 실행합니다
    3. LLM 이 도구 호출을 생성하면 해당 도구 호출을 실행하고 결과를 추가한 뒤 루프를 다시 실행합니다
3. 전달된 `max_turns` 를 초과하면 [`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded] 예외를 발생시킵니다

!!! note

    LLM 출력이 "최종 출력"으로 간주되는 규칙은, 원하는 타입의 텍스트 출력을 생성하고 도구 호출이 없어야 한다는 것입니다.

## 스트리밍

스트리밍을 사용하면 LLM 이 실행되는 동안 스트리밍 이벤트를 추가로 수신할 수 있습니다. 스트림이 완료되면 [`RunResultStreaming`][agents.result.RunResultStreaming] 에는 실행에 대한 모든 새로운 출력이 포함된 전체 정보가 담깁니다. 스트리밍 이벤트는 `.stream_events()` 를 호출하면 됩니다. 자세한 내용은 [스트리밍 가이드](streaming.md)를 참고하세요.

## 실행 구성

`run_config` 매개변수로 에이전트 실행에 대한 전역 설정을 구성할 수 있습니다:

-   [`model`][agents.run.RunConfig.model]: 각 Agent 의 `model` 과 무관하게 사용할 전역 LLM 모델을 설정
-   [`model_provider`][agents.run.RunConfig.model_provider]: 모델 이름 조회를 위한 모델 제공자. 기본값은 OpenAI
-   [`model_settings`][agents.run.RunConfig.model_settings]: 에이전트별 설정 재정의. 예를 들어 전역 `temperature` 또는 `top_p` 를 설정할 수 있음
-   [`input_guardrails`][agents.run.RunConfig.input_guardrails], [`output_guardrails`][agents.run.RunConfig.output_guardrails]: 모든 실행에 포함할 입력/출력 가드레일 목록
-   [`handoff_input_filter`][agents.run.RunConfig.handoff_input_filter]: 핸드오프에 이미 존재하지 않는 경우 모든 핸드오프에 적용할 전역 입력 필터. 입력 필터를 사용하면 새 에이전트로 전송되는 입력을 편집할 수 있습니다. 자세한 내용은 [`Handoff.input_filter`][agents.handoffs.Handoff.input_filter] 문서를 참고하세요
-   [`nest_handoff_history`][agents.run.RunConfig.nest_handoff_history]: `True`(기본값) 일 때, 다음 에이전트를 호출하기 전에 이전 대화 기록을 하나의 assistant 메시지로 접어 넣습니다. 도우미는 이후 핸드오프가 발생할 때마다 새로운 턴을 계속 추가하는 `<CONVERSATION HISTORY>` 블록 안에 내용을 배치합니다. 원문 대화록을 그대로 전달하려면 이를 `False` 로 설정하거나 사용자 지정 핸드오프 필터를 제공하세요. 모든 [`Runner` 메서드](agents.run.Runner)는 `RunConfig` 를 전달하지 않으면 자동으로 생성하므로, 퀵스타트와 code examples 는 이 기본값을 자동으로 사용하며, 명시적인 [`Handoff.input_filter`][agents.handoffs.Handoff.input_filter] 콜백은 계속해서 이를 재정의합니다. 개별 핸드오프는 [`Handoff.nest_handoff_history`][agents.handoffs.Handoff.nest_handoff_history] 로 이 설정을 재정의할 수 있습니다
-   [`handoff_history_mapper`][agents.run.RunConfig.handoff_history_mapper]: `nest_handoff_history` 가 `True` 일 때 정규화된 대화록(히스토리 + 핸드오프 아이템)을 받는 선택적 호출 가능 객체. 다음 에이전트로 전달할 입력 아이템의 정확한 목록을 반환해야 하며, 전체 핸드오프 필터를 작성하지 않고도 내장 요약을 교체할 수 있음
-   [`tracing_disabled`][agents.run.RunConfig.tracing_disabled]: 전체 실행에 대해 [트레이싱](tracing.md) 비활성화
-   [`trace_include_sensitive_data`][agents.run.RunConfig.trace_include_sensitive_data]: LLM 및 도구 호출의 입력/출력 등 민감할 수 있는 데이터를 트레이스에 포함할지 여부를 구성
-   [`workflow_name`][agents.run.RunConfig.workflow_name], [`trace_id`][agents.run.RunConfig.trace_id], [`group_id`][agents.run.RunConfig.group_id]: 실행에 대한 트레이싱 워크플로 이름, 트레이스 ID, 트레이스 그룹 ID 설정. 최소한 `workflow_name` 설정을 권장. 그룹 ID 는 여러 실행에 걸쳐 트레이스를 연결할 수 있는 선택적 필드
-   [`trace_metadata`][agents.run.RunConfig.trace_metadata]: 모든 트레이스에 포함할 메타데이터

기본적으로, SDK 는 한 에이전트가 다른 에이전트로 핸드오프할 때 이전 턴을 단일 assistant 요약 메시지 안에 중첩합니다. 이는 반복되는 assistant 메시지를 줄이고, 새 에이전트가 빠르게 스캔할 수 있도록 전체 대화록을 단일 블록에 유지합니다. 레거시 동작으로 돌아가려면 `RunConfig(nest_handoff_history=False)` 를 전달하거나, 대화를 필요한 그대로 전달하는 `handoff_input_filter`(또는 `handoff_history_mapper`) 를 제공하세요. 특정 핸드오프에 대해 옵트아웃(또는 옵트인)하려면 `handoff(..., nest_handoff_history=False)` 또는 `True` 로 설정하세요. 사용자 지정 매퍼를 작성하지 않고 생성된 요약에 사용되는 래퍼 텍스트를 변경하려면 [`set_conversation_history_wrappers`][agents.handoffs.set_conversation_history_wrappers] 를 호출하세요(기본값 복원은 [`reset_conversation_history_wrappers`][agents.handoffs.reset_conversation_history_wrappers]).

## 대화/채팅 스레드

어떤 run 메서드를 호출해도 하나 이상의 에이전트가 실행될 수 있으며(따라서 하나 이상의 LLM 호출), 이는 채팅 대화에서 단일 논리적 턴을 나타냅니다. 예:

1. 사용자 턴: 사용자가 텍스트 입력
2. 러너 실행: 첫 번째 에이전트가 LLM 을 호출하고 도구를 실행한 뒤 두 번째 에이전트로 핸드오프, 두 번째 에이전트가 더 많은 도구를 실행한 다음 출력을 생성

에이전트 실행이 끝나면 사용자에게 무엇을 보여줄지 선택할 수 있습니다. 예를 들어, 에이전트가 생성한 모든 새 아이템을 보여주거나 최종 출력만 보여줄 수 있습니다. 어느 쪽이든 사용자가 후속 질문을 할 수 있으며, 그 경우 run 메서드를 다시 호출하면 됩니다.

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

### Sessions 를 사용한 자동 대화 관리

더 간단한 접근으로, [Sessions](sessions/index.md) 를 사용하면 `.to_input_list()` 를 수동으로 호출하지 않고도 대화 기록을 자동으로 처리할 수 있습니다:

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

Sessions 는 다음을 자동으로 수행합니다:

-   각 실행 전에 대화 기록을 가져옴
-   각 실행 후 새 메시지를 저장
-   서로 다른 세션 ID 에 대해 별도의 대화를 유지

자세한 내용은 [Sessions 문서](sessions/index.md)를 참고하세요.


### 서버 관리 대화

OpenAI 대화 상태 기능을 사용하여 `to_input_list()` 또는 `Sessions` 로 로컬에서 처리하는 대신 서버 측에서 대화 상태를 관리할 수도 있습니다. 이렇게 하면 과거 모든 메시지를 수동으로 재전송하지 않고도 대화 기록을 보존할 수 있습니다. 자세한 내용은 [OpenAI Conversation state 가이드](https://platform.openai.com/docs/guides/conversation-state?api-mode=responses)를 참고하세요.

OpenAI 는 턴 간 상태를 추적하는 두 가지 방법을 제공합니다:

#### 1. `conversation_id` 사용

먼저 OpenAI Conversations API 로 대화를 생성한 다음 이후 모든 호출에서 해당 ID 를 재사용합니다:

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

또 다른 옵션은 **response chaining** 으로, 각 턴이 이전 턴의 response ID 에 명시적으로 연결됩니다.

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

Agents SDK 의 [Temporal](https://temporal.io/) 통합을 사용하면 휴먼인더루프 작업을 포함한 내구성 있는 장기 실행 워크플로를 운영할 수 있습니다. Temporal 과 Agents SDK 가 장기 실행 작업을 완료하는 데 함께 작동하는 데모는 [이 영상](https://www.youtube.com/watch?v=fFBZqzT4DD8)에서 확인하고, [여기 문서](https://github.com/temporalio/sdk-python/tree/main/temporalio/contrib/openai_agents)를 참고하세요.

## 예외

SDK 는 특정 경우 예외를 발생시킵니다. 전체 목록은 [`agents.exceptions`][] 에 있습니다. 개요는 다음과 같습니다:

-   [`AgentsException`][agents.exceptions.AgentsException]: SDK 내에서 발생하는 모든 예외의 기본 클래스. 다른 모든 구체적 예외의 상위 일반 타입
-   [`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded]: 에이전트 실행이 `Runner.run`, `Runner.run_sync`, `Runner.run_streamed` 메서드에 전달된 `max_turns` 한도를 초과할 때 발생. 지정된 상호작용 턴 수 내에 에이전트가 작업을 완료하지 못했음을 나타냄
-   [`ModelBehaviorError`][agents.exceptions.ModelBehaviorError]: 기본 모델(LLM) 이 예기치 않거나 잘못된 출력을 생성할 때 발생. 다음을 포함할 수 있음:
    -   잘못된 JSON: 특히 특정 `output_type` 이 정의된 경우, 도구 호출 또는 직접 출력에서 모델이 잘못된 JSON 구조를 제공하는 경우
    -   예기치 않은 도구 관련 실패: 모델이 예상 방식으로 도구를 사용하지 못하는 경우
-   [`UserError`][agents.exceptions.UserError]: SDK 를 사용하는 사람이 SDK 사용 중 오류를 발생시킬 때 발생. 보통 잘못된 코드 구현, 잘못된 구성 또는 SDK API 오용으로 인해 발생
-   [`InputGuardrailTripwireTriggered`][agents.exceptions.InputGuardrailTripwireTriggered], [`OutputGuardrailTripwireTriggered`][agents.exceptions.OutputGuardrailTripwireTriggered]: 각각 입력 가드레일 또는 출력 가드레일 조건이 충족될 때 발생. 입력 가드레일은 처리 전에 들어오는 메시지를 검사하고, 출력 가드레일은 전달 전에 에이전트의 최종 응답을 검사함