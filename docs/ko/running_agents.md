---
search:
  exclude: true
---
# 에이전트 실행

[`Runner`][agents.run.Runner] 클래스를 통해 에이전트를 실행할 수 있습니다. 3가지 옵션이 있습니다:

1. [`Runner.run()`][agents.run.Runner.run]: 비동기로 실행되며 [`RunResult`][agents.result.RunResult]를 반환합니다
2. [`Runner.run_sync()`][agents.run.Runner.run_sync]: 동기 메서드이며 내부적으로 `.run()`을 실행합니다
3. [`Runner.run_streamed()`][agents.run.Runner.run_streamed]: 비동기로 실행되며 [`RunResultStreaming`][agents.result.RunResultStreaming]을 반환합니다. LLM을 스트리밍 모드로 호출하고, 수신되는 대로 해당 이벤트를 스트리밍합니다

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

`Runner`에서 run 메서드를 사용할 때 시작 에이전트와 입력을 전달합니다. 입력은 문자열(사용자 메시지로 간주됨)일 수도 있고, OpenAI Responses API의 아이템들로 구성된 입력 아이템 리스트일 수도 있습니다.

그다음 runner는 루프를 실행합니다:

1. 현재 입력으로 현재 에이전트에 대해 LLM을 호출합니다
2. LLM이 출력을 생성합니다
    1. LLM이 `final_output`을 반환하면 루프가 종료되고 결과를 반환합니다
    2. LLM이 핸드오프를 수행하면 현재 에이전트와 입력을 업데이트하고 루프를 다시 실행합니다
    3. LLM이 도구 호출을 생성하면 해당 도구 호출을 실행하고, 결과를 추가한 뒤 루프를 다시 실행합니다
3. 전달된 `max_turns`를 초과하면 [`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded] 예외를 발생시킵니다

!!! note

    LLM 출력이 "최종 출력(final output)"으로 간주되는 규칙은, 원하는 타입의 텍스트 출력을 생성하고 도구 호출이 없어야 한다는 것입니다.

## 스트리밍

스트리밍을 사용하면 LLM이 실행되는 동안 스트리밍 이벤트를 추가로 받을 수 있습니다. 스트림이 완료되면 [`RunResultStreaming`][agents.result.RunResultStreaming]에는 실행에 대한 완전한 정보(생성된 모든 신규 출력 포함)가 담깁니다. 스트리밍 이벤트는 `.stream_events()`를 호출해 받을 수 있습니다. 자세한 내용은 [스트리밍 가이드](streaming.md)를 참고하세요.

## 실행 구성

`run_config` 매개변수로 에이전트 실행에 대한 일부 전역 설정을 구성할 수 있습니다:

-   [`model`][agents.run.RunConfig.model]: 각 Agent가 가진 `model`과 무관하게, 사용할 전역 LLM 모델을 설정할 수 있습니다
-   [`model_provider`][agents.run.RunConfig.model_provider]: 모델 이름을 조회하기 위한 모델 프로바이더로, 기본값은 OpenAI입니다
-   [`model_settings`][agents.run.RunConfig.model_settings]: 에이전트별 설정을 덮어씁니다. 예를 들어 전역 `temperature` 또는 `top_p`를 설정할 수 있습니다
-   [`session_settings`][agents.run.RunConfig.session_settings]: 실행 중 히스토리를 가져올 때 세션 레벨 기본값(예: `SessionSettings(limit=...)`)을 덮어씁니다
-   [`input_guardrails`][agents.run.RunConfig.input_guardrails], [`output_guardrails`][agents.run.RunConfig.output_guardrails]: 모든 실행에 포함할 입력 또는 출력 가드레일 리스트입니다
-   [`handoff_input_filter`][agents.run.RunConfig.handoff_input_filter]: 핸드오프에 이미 설정된 것이 없다면, 모든 핸드오프에 적용되는 전역 입력 필터입니다. 입력 필터를 사용하면 새 에이전트로 전송되는 입력을 편집할 수 있습니다. 자세한 내용은 [`Handoff.input_filter`][agents.handoffs.Handoff.input_filter] 문서를 참고하세요
-   [`nest_handoff_history`][agents.run.RunConfig.nest_handoff_history]: 다음 에이전트를 호출하기 전에 이전 대화를 단일 assistant 메시지로 접어 넣는 옵트인 베타 기능입니다. 중첩 핸드오프를 안정화하는 동안 기본값은 비활성화되어 있으며, 활성화하려면 `True`로 설정하거나 원문(raw) 대화를 그대로 전달하려면 `False`로 두세요. [Runner 메서드][agents.run.Runner]는 `RunConfig`를 전달하지 않으면 자동으로 생성하므로, 퀵스타트와 examples는 기본값(비활성화)을 유지하며, 명시적인 [`Handoff.input_filter`][agents.handoffs.Handoff.input_filter] 콜백은 계속해서 이를 덮어씁니다. 개별 핸드오프는 [`Handoff.nest_handoff_history`][agents.handoffs.Handoff.nest_handoff_history]를 통해 이 설정을 덮어쓸 수 있습니다
-   [`handoff_history_mapper`][agents.run.RunConfig.handoff_history_mapper]: `nest_handoff_history`를 옵트인할 때마다 정규화된 대화 기록(히스토리 + 핸드오프 아이템)을 받는 선택적 callable입니다. 다음 에이전트로 전달할 입력 아이템의 정확한 리스트를 반환해야 하며, 전체 핸드오프 필터를 작성하지 않고도 내장 요약을 대체할 수 있습니다
-   [`tracing_disabled`][agents.run.RunConfig.tracing_disabled]: 전체 실행에 대해 [트레이싱](tracing.md)을 비활성화할 수 있습니다
-   [`tracing`][agents.run.RunConfig.tracing]: 이 실행의 exporter, 프로세서, 또는 트레이싱 메타데이터를 덮어쓰기 위해 [`TracingConfig`][agents.tracing.TracingConfig]를 전달합니다
-   [`trace_include_sensitive_data`][agents.run.RunConfig.trace_include_sensitive_data]: 트레이스에 LLM 및 도구 호출 입력/출력 등 잠재적으로 민감한 데이터가 포함될지 여부를 구성합니다
-   [`workflow_name`][agents.run.RunConfig.workflow_name], [`trace_id`][agents.run.RunConfig.trace_id], [`group_id`][agents.run.RunConfig.group_id]: 실행의 트레이싱 workflow 이름, trace ID, trace group ID를 설정합니다. 최소한 `workflow_name`을 설정하는 것을 권장합니다. group ID는 여러 실행에 걸쳐 트레이스를 연결할 수 있게 해주는 선택적 필드입니다
-   [`trace_metadata`][agents.run.RunConfig.trace_metadata]: 모든 트레이스에 포함할 메타데이터입니다
-   [`session_input_callback`][agents.run.RunConfig.session_input_callback]: Sessions를 사용할 때 각 턴 전에 새 사용자 입력이 세션 히스토리와 병합되는 방식을 커스터마이즈합니다
-   [`call_model_input_filter`][agents.run.RunConfig.call_model_input_filter]: 모델 호출 직전에 완전히 준비된 모델 입력(instructions 및 입력 아이템)을 편집하기 위한 훅입니다. 예: 히스토리 트리밍 또는 시스템 프롬프트 주입
-   [`tool_error_formatter`][agents.run.RunConfig.tool_error_formatter]: 승인 플로우에서 도구 호출이 거부될 때 모델에 보이는 메시지를 커스터마이즈합니다

중첩 핸드오프는 옵트인 베타로 제공됩니다. `RunConfig(nest_handoff_history=True)`를 전달하거나 특정 핸드오프에 대해 `handoff(..., nest_handoff_history=True)`를 설정해 대화 접기 동작을 활성화하세요. 원문(raw) 대화(기본값)를 유지하고 싶다면 플래그를 설정하지 않거나, 대화를 필요한 형태로 정확히 전달하는 `handoff_input_filter`(또는 `handoff_history_mapper`)를 제공하세요. 커스텀 mapper를 작성하지 않고 생성된 요약에 사용되는 래퍼 텍스트를 변경하려면 [`set_conversation_history_wrappers`][agents.handoffs.set_conversation_history_wrappers]를 호출하세요(기본값 복원은 [`reset_conversation_history_wrappers`][agents.handoffs.reset_conversation_history_wrappers]).

## 대화/채팅 스레드

어떤 run 메서드를 호출하든 하나 이상의 에이전트가 실행될 수 있고(따라서 하나 이상의 LLM 호출이 발생), 이는 채팅 대화에서 하나의 논리적 턴을 나타냅니다. 예:

1. 사용자 턴: 사용자가 텍스트 입력
2. Runner 실행: 첫 번째 에이전트가 LLM을 호출하고 도구를 실행한 뒤 두 번째 에이전트로 핸드오프, 두 번째 에이전트가 추가 도구를 실행하고 출력을 생성

에이전트 실행이 끝난 후, 사용자에게 무엇을 보여줄지 선택할 수 있습니다. 예를 들어 에이전트가 생성한 모든 신규 아이템을 사용자에게 보여줄 수도 있고, 최종 출력만 보여줄 수도 있습니다. 어느 쪽이든 사용자가 후속 질문을 할 수 있으며, 그 경우 run 메서드를 다시 호출하면 됩니다.

### 수동 대화 관리

[`RunResultBase.to_input_list()`][agents.result.RunResultBase.to_input_list] 메서드를 사용해 다음 턴 입력을 얻는 방식으로 대화 히스토리를 수동으로 관리할 수 있습니다:

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

더 간단한 접근으로, [Sessions](sessions/index.md)를 사용하면 `.to_input_list()`를 수동으로 호출하지 않고도 대화 히스토리를 자동으로 처리할 수 있습니다:

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

Sessions는 자동으로 다음을 수행합니다:

-   각 실행 전에 대화 히스토리를 조회
-   각 실행 후 신규 메시지를 저장
-   서로 다른 세션 ID에 대해 별도의 대화를 유지

!!! note

    세션 영속성은 server-managed 대화 설정
    (`conversation_id`, `previous_response_id`, 또는 `auto_previous_response_id`)과
    같은 실행에서 함께 사용할 수 없습니다. 호출당 한 가지 방식만 선택하세요.

자세한 내용은 [Sessions 문서](sessions/index.md)를 참고하세요.

### 서버 관리 대화

또한 `to_input_list()` 또는 `Sessions`로 로컬에서 처리하는 대신, OpenAI의 conversation state 기능이 서버 측에서 대화 상태를 관리하도록 할 수 있습니다. 이를 통해 과거 메시지를 모두 수동으로 다시 전송하지 않고도 대화 히스토리를 보존할 수 있습니다. 자세한 내용은 [OpenAI Conversation state 가이드](https://platform.openai.com/docs/guides/conversation-state?api-mode=responses)를 참고하세요.

OpenAI는 턴 간 상태를 추적하는 두 가지 방법을 제공합니다:

#### 1. `conversation_id` 사용

먼저 OpenAI Conversations API로 대화를 생성한 뒤, 이후 모든 호출에서 해당 ID를 재사용합니다:

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

또 다른 옵션은 **response chaining**으로, 각 턴이 이전 턴의 응답 ID에 명시적으로 연결됩니다.

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

## Call model input filter

`call_model_input_filter`를 사용해 모델 호출 직전에 모델 입력을 편집하세요. 이 훅은 현재 에이전트, 컨텍스트, 그리고 결합된 입력 아이템(세션 히스토리가 있는 경우 포함)을 받아 새로운 `ModelInputData`를 반환합니다.

```python
from agents import Agent, Runner, RunConfig
from agents.run import CallModelData, ModelInputData

def drop_old_messages(data: CallModelData[None]) -> ModelInputData:
    # Keep only the last 5 items and preserve existing instructions.
    trimmed = data.model_data.input[-5:]
    return ModelInputData(input=trimmed, instructions=data.model_data.instructions)

agent = Agent(name="Assistant", instructions="Answer concisely.")
result = Runner.run_sync(
    agent,
    "Explain quines",
    run_config=RunConfig(call_model_input_filter=drop_old_messages),
)
```

민감 데이터 마스킹, 긴 히스토리 트리밍, 추가 시스템 가이던스 주입 등을 위해 `run_config`로 실행별 훅을 설정하거나 `Runner`의 기본값으로 설정하세요.

## 오류 핸들러

모든 `Runner` 엔트리 포인트는 오류 종류를 키로 하는 dict인 `error_handlers`를 받습니다. 현재 지원되는 키는 `"max_turns"`입니다. `MaxTurnsExceeded`를 발생시키는 대신 통제된 최종 출력을 반환하고 싶을 때 사용하세요.

```python
from agents import (
    Agent,
    RunErrorHandlerInput,
    RunErrorHandlerResult,
    Runner,
)

agent = Agent(name="Assistant", instructions="Be concise.")


def on_max_turns(_data: RunErrorHandlerInput[None]) -> RunErrorHandlerResult:
    return RunErrorHandlerResult(
        final_output="I couldn't finish within the turn limit. Please narrow the request.",
        include_in_history=False,
    )


result = Runner.run_sync(
    agent,
    "Analyze this long transcript",
    max_turns=3,
    error_handlers={"max_turns": on_max_turns},
)
print(result.final_output)
```

대체 출력이 대화 히스토리에 추가되지 않도록 하려면 `include_in_history=False`로 설정하세요.

## 장시간 실행 에이전트 & 휴먼인더루프

도구 승인 일시정지/재개 패턴은 전용 [Human-in-the-loop 가이드](human_in_the_loop.md)를 참고하세요.

### Temporal

Agents SDK의 [Temporal](https://temporal.io/) 통합을 사용하면, 휴먼인더루프 작업을 포함한 내구성 있는 장시간 워크플로를 실행할 수 있습니다. Temporal과 Agents SDK가 함께 장시간 작업을 완료하는 동작 데모는 [이 비디오](https://www.youtube.com/watch?v=fFBZqzT4DD8)에서 확인할 수 있으며, 문서는 [여기](https://github.com/temporalio/sdk-python/tree/main/temporalio/contrib/openai_agents)에서 볼 수 있습니다. 

### Restate

Agents SDK의 [Restate](https://restate.dev/) 통합을 사용하면, 사람 승인, 핸드오프, 세션 관리를 포함한 경량의 내구성 있는 에이전트를 사용할 수 있습니다. 이 통합은 의존성으로 Restate의 단일 바이너리 런타임이 필요하며, 프로세스/컨테이너 또는 serverless 함수로 에이전트를 실행하는 것을 지원합니다.
자세한 내용은 [개요](https://www.restate.dev/blog/durable-orchestration-for-ai-agents-with-restate-and-openai-sdk)를 읽거나 [문서](https://docs.restate.dev/ai)를 참고하세요.

### DBOS

Agents SDK의 [DBOS](https://dbos.dev/) 통합을 사용하면 장애 및 재시작 전반에 걸쳐 진행 상황을 보존하는 신뢰할 수 있는 에이전트를 실행할 수 있습니다. 장시간 실행 에이전트, 휴먼인더루프 워크플로, 핸드오프를 지원합니다. 동기 및 비동기 메서드 모두를 지원합니다. 이 통합에는 SQLite 또는 Postgres 데이터베이스만 필요합니다. 자세한 내용은 통합 [repo](https://github.com/dbos-inc/dbos-openai-agents)와 [문서](https://docs.dbos.dev/integrations/openai-agents)를 참고하세요.

## 예외

SDK는 특정 경우에 예외를 발생시킵니다. 전체 목록은 [`agents.exceptions`][]에 있습니다. 개요는 다음과 같습니다:

-   [`AgentsException`][agents.exceptions.AgentsException]: SDK 내에서 발생하는 모든 예외의 베이스 클래스입니다. 다른 모든 구체적인 예외가 파생되는 일반 타입 역할을 합니다
-   [`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded]: 에이전트 실행이 `Runner.run`, `Runner.run_sync`, 또는 `Runner.run_streamed` 메서드에 전달된 `max_turns` 제한을 초과하면 발생합니다. 지정된 상호작용 턴 수 내에 에이전트가 작업을 완료하지 못했음을 나타냅니다
-   [`ModelBehaviorError`][agents.exceptions.ModelBehaviorError]: 기반 모델(LLM)이 예상치 않거나 유효하지 않은 출력을 생성할 때 발생합니다. 예:
    -   잘못된 형식의 JSON: 모델이 도구 호출 또는 직접 출력에서 잘못된 JSON 구조를 제공하는 경우(특히 특정 `output_type`이 정의된 경우)
    -   예상치 못한 도구 관련 실패: 모델이 기대된 방식으로 도구를 사용하지 못하는 경우
-   [`ToolTimeoutError`][agents.exceptions.ToolTimeoutError]: 함수 도구 호출이 설정된 타임아웃을 초과하고 도구가 `timeout_behavior="raise_exception"`을 사용하는 경우 발생합니다
-   [`UserError`][agents.exceptions.UserError]: SDK를 사용하는 코드 작성자(사용자)가 SDK 사용 중 오류를 만들었을 때 발생합니다. 이는 보통 잘못된 코드 구현, 유효하지 않은 구성, 또는 SDK API 오용에서 비롯됩니다
-   [`InputGuardrailTripwireTriggered`][agents.exceptions.InputGuardrailTripwireTriggered], [`OutputGuardrailTripwireTriggered`][agents.exceptions.OutputGuardrailTripwireTriggered]: 각각 입력 가드레일 또는 출력 가드레일의 조건이 충족되면 발생합니다. 입력 가드레일은 처리 전 수신 메시지를 검사하고, 출력 가드레일은 전달 전 에이전트의 최종 응답을 검사합니다