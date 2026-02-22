---
search:
  exclude: true
---
# 에이전트 실행

[`Runner`][agents.run.Runner] 클래스를 통해 에이전트를 실행할 수 있습니다. 3가지 옵션이 있습니다:

1. [`Runner.run()`][agents.run.Runner.run]: 비동기로 실행되며 [`RunResult`][agents.result.RunResult]를 반환합니다
2. [`Runner.run_sync()`][agents.run.Runner.run_sync]: 동기 메서드이며 내부적으로 `.run()`을 실행하기만 합니다
3. [`Runner.run_streamed()`][agents.run.Runner.run_streamed]: 비동기로 실행되며 [`RunResultStreaming`][agents.result.RunResultStreaming]를 반환합니다. 스트리밍 모드로 LLM을 호출하고, 수신되는 대로 해당 이벤트를 스트리밍으로 전달합니다

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

`Runner`에서 run 메서드를 사용할 때 시작 에이전트와 입력을 전달합니다. 입력은 문자열(사용자 메시지로 간주)일 수도 있고, OpenAI Responses API의 항목들로 구성된 입력 항목 리스트일 수도 있습니다.

그런 다음 runner가 루프를 실행합니다:

1. 현재 입력으로 현재 에이전트에 대해 LLM을 호출합니다
2. LLM이 출력을 생성합니다
    1. LLM이 `final_output`을 반환하면 루프가 종료되고 결과를 반환합니다
    2. LLM이 핸드오프를 수행하면 현재 에이전트와 입력을 업데이트한 뒤 루프를 다시 실행합니다
    3. LLM이 도구 호출을 생성하면 해당 도구 호출을 실행하고 결과를 추가한 뒤 루프를 다시 실행합니다
3. 전달된 `max_turns`를 초과하면 [`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded] 예외를 발생시킵니다

!!! note

    LLM 출력이 "최종 출력"으로 간주되는 규칙은, 원하는 타입의 텍스트 출력을 생성하고 도구 호출이 없는 경우입니다.

## 스트리밍

스트리밍을 사용하면 LLM이 실행되는 동안 스트리밍 이벤트를 추가로 받을 수 있습니다. 스트림이 끝나면 [`RunResultStreaming`][agents.result.RunResultStreaming]에는 생성된 모든 새 출력을 포함하여 실행에 대한 완전한 정보가 담깁니다. 스트리밍 이벤트는 `.stream_events()`를 호출해 받을 수 있습니다. 자세한 내용은 [스트리밍 가이드](streaming.md)를 참고하세요.

## Run config

`run_config` 매개변수로 에이전트 실행에 대한 일부 전역 설정을 구성할 수 있습니다:

-   [`model`][agents.run.RunConfig.model]: 각 Agent가 가진 `model`과 무관하게 사용할 전역 LLM 모델을 설정할 수 있습니다
-   [`model_provider`][agents.run.RunConfig.model_provider]: 모델 이름 조회를 위한 모델 provider이며, 기본값은 OpenAI입니다
-   [`model_settings`][agents.run.RunConfig.model_settings]: 에이전트별 설정을 재정의합니다. 예를 들어 전역 `temperature` 또는 `top_p`를 설정할 수 있습니다
-   [`session_settings`][agents.run.RunConfig.session_settings]: 실행 중 히스토리를 가져올 때 세션 수준 기본값(예: `SessionSettings(limit=...)`)을 재정의합니다
-   [`input_guardrails`][agents.run.RunConfig.input_guardrails], [`output_guardrails`][agents.run.RunConfig.output_guardrails]: 모든 실행에 포함할 입력/출력 가드레일 목록입니다
-   [`handoff_input_filter`][agents.run.RunConfig.handoff_input_filter]: 핸드오프에 입력 필터가 이미 없는 경우, 모든 핸드오프에 적용할 전역 입력 필터입니다. 입력 필터를 통해 새 에이전트로 보내는 입력을 편집할 수 있습니다. 자세한 내용은 [`Handoff.input_filter`][agents.handoffs.Handoff.input_filter] 문서를 참고하세요
-   [`nest_handoff_history`][agents.run.RunConfig.nest_handoff_history]: 다음 에이전트를 호출하기 전에 이전 대화 기록을 하나의 assistant 메시지로 접어 넣는 옵트인 베타 기능입니다. 중첩 핸드오프를 안정화하는 동안 기본적으로 비활성화되어 있으며, 활성화하려면 `True`로 설정하고 원문 대화 기록을 그대로 전달하려면 `False`로 두세요. 모든 [Runner 메서드][agents.run.Runner]는 `RunConfig`를 전달하지 않으면 자동으로 `RunConfig`를 생성하므로, 퀵스타트와 examples는 기본값(꺼짐)을 유지하며, 명시적으로 지정한 [`Handoff.input_filter`][agents.handoffs.Handoff.input_filter] 콜백은 계속해서 이를 재정의합니다. 개별 핸드오프는 [`Handoff.nest_handoff_history`][agents.handoffs.Handoff.nest_handoff_history]를 통해 이 설정을 재정의할 수 있습니다
-   [`handoff_history_mapper`][agents.run.RunConfig.handoff_history_mapper]: `nest_handoff_history`를 옵트인할 때마다 정규화된 대화 기록(히스토리 + 핸드오프 항목)을 받는 선택적 callable입니다. 다음 에이전트로 전달할 입력 항목의 정확한 리스트를 반환해야 하며, 전체 핸드오프 필터를 작성하지 않고도 내장 요약을 대체할 수 있습니다
-   [`tracing_disabled`][agents.run.RunConfig.tracing_disabled]: 전체 실행에 대해 [tracing](tracing.md)을 비활성화할 수 있습니다
-   [`tracing`][agents.run.RunConfig.tracing]: 이 실행에 대해 exporter, 프로세서, 또는 트레이싱 메타데이터를 재정의하기 위해 [`TracingConfig`][agents.tracing.TracingConfig]를 전달합니다
-   [`trace_include_sensitive_data`][agents.run.RunConfig.trace_include_sensitive_data]: 트레이스에 LLM 및 도구 호출 입력/출력과 같은 잠재적으로 민감한 데이터가 포함될지 여부를 구성합니다
-   [`workflow_name`][agents.run.RunConfig.workflow_name], [`trace_id`][agents.run.RunConfig.trace_id], [`group_id`][agents.run.RunConfig.group_id]: 실행에 대한 트레이싱 워크플로 이름, 트레이스 ID, 트레이스 그룹 ID를 설정합니다. 최소한 `workflow_name`은 설정하는 것을 권장합니다. group ID는 여러 실행에 걸쳐 트레이스를 연결할 수 있는 선택적 필드입니다
-   [`trace_metadata`][agents.run.RunConfig.trace_metadata]: 모든 트레이스에 포함할 메타데이터입니다
-   [`session_input_callback`][agents.run.RunConfig.session_input_callback]: Sessions를 사용할 때 각 턴 전에 새 사용자 입력이 세션 히스토리와 병합되는 방식을 커스터마이즈합니다
-   [`call_model_input_filter`][agents.run.RunConfig.call_model_input_filter]: 모델 호출 직전에 완전히 준비된 모델 입력(instructions 및 입력 항목)을 편집하기 위한 훅입니다. 예: 히스토리 트리밍 또는 시스템 프롬프트 주입
-   [`tool_error_formatter`][agents.run.RunConfig.tool_error_formatter]: 승인 플로우 중 도구 호출이 거부될 때 모델에 보이는 메시지를 커스터마이즈합니다
-   [`reasoning_item_id_policy`][agents.run.RunConfig.reasoning_item_id_policy]: runner가 이전 출력을 다음 턴 모델 입력으로 변환할 때 reasoning 항목 ID를 유지할지 또는 생략할지 제어합니다

중첩 핸드오프는 옵트인 베타로 제공됩니다. `RunConfig(nest_handoff_history=True)`를 전달하거나 특정 핸드오프에 대해 `handoff(..., nest_handoff_history=True)`를 설정해 축약된 대화 기록 동작을 활성화할 수 있습니다. 원문 대화 기록(기본값)을 유지하고 싶다면 플래그를 설정하지 않거나, 원하는 방식 그대로 대화를 전달하는 `handoff_input_filter`(또는 `handoff_history_mapper`)를 제공하세요. 커스텀 mapper를 작성하지 않고 생성된 요약에 사용되는 래퍼 텍스트를 변경하려면 [`set_conversation_history_wrappers`][agents.handoffs.set_conversation_history_wrappers]를 호출하고(기본값으로 복원하려면 [`reset_conversation_history_wrappers`][agents.handoffs.reset_conversation_history_wrappers]를 사용) 설정하세요.

### Run config 상세

#### `tool_error_formatter`

`tool_error_formatter`를 사용하면 승인 플로우에서 도구 호출이 거부될 때 모델에 반환되는 메시지를 커스터마이즈할 수 있습니다.

formatter는 다음을 포함한 [`ToolErrorFormatterArgs`][agents.run_config.ToolErrorFormatterArgs]를 받습니다:

-   `kind`: 오류 카테고리입니다. 현재는 `"approval_rejected"`입니다
-   `tool_type`: 도구 런타임입니다(`"function"`, `"computer"`, `"shell"`, 또는 `"apply_patch"`)
-   `tool_name`: 도구 이름입니다
-   `call_id`: 도구 호출 ID입니다
-   `default_message`: SDK의 기본 모델 표시 메시지입니다
-   `run_context`: 활성 실행 컨텍스트 래퍼입니다

메시지를 대체할 문자열을 반환하거나, SDK 기본값을 사용하려면 `None`을 반환하세요.

```python
from agents import Agent, RunConfig, Runner, ToolErrorFormatterArgs


def format_rejection(args: ToolErrorFormatterArgs[None]) -> str | None:
    if args.kind == "approval_rejected":
        return (
            f"Tool call '{args.tool_name}' was rejected by a human reviewer. "
            "Ask for confirmation or propose a safer alternative."
        )
    return None


agent = Agent(name="Assistant")
result = Runner.run_sync(
    agent,
    "Please delete the production database.",
    run_config=RunConfig(tool_error_formatter=format_rejection),
)
```

#### `reasoning_item_id_policy`

`reasoning_item_id_policy`는 runner가 히스토리를 앞으로 전달할 때(예: `RunResult.to_input_list()`를 사용하거나 세션 기반 실행을 할 때) reasoning 항목이 다음 턴 모델 입력으로 변환되는 방식을 제어합니다.

-   `None` 또는 `"preserve"`(기본값): reasoning 항목 ID를 유지합니다
-   `"omit"`: 생성된 다음 턴 입력에서 reasoning 항목 ID를 제거합니다

`"omit"`은 주로, reasoning 항목이 `id`와 함께 전송되었지만 필수로 뒤따라야 하는 항목이 없는 경우 발생하는 Responses API 400 오류(예: `Item 'rs_...' of type 'reasoning' was provided without its required following item.`) 계열에 대한 옵트인 완화책으로 사용하세요.

이 문제는 SDK가 이전 출력으로부터 후속 입력을 구성할 때(세션 영속성, 서버 관리 대화 델타, 스트리밍/비스트리밍 후속 턴, resume 경로 포함) reasoning 항목 ID는 보존되었지만 provider가 해당 ID가 대응되는 다음 항목과 계속 페어로 남아 있기를 요구하는 경우, 멀티턴 에이전트 실행에서 발생할 수 있습니다.

`reasoning_item_id_policy="omit"`을 설정하면 reasoning 내용은 유지하면서 reasoning 항목 `id`는 제거하므로, SDK가 생성한 후속 입력에서 해당 API 불변 조건을 트리거하는 것을 피할 수 있습니다.

범위 관련 참고 사항:

-   이는 SDK가 후속 입력을 구성할 때 생성/전달하는 reasoning 항목만 변경합니다
-   사용자가 제공한 초기 입력 항목은 재작성하지 않습니다
-   `call_model_input_filter`는 이 정책이 적용된 뒤에도 의도적으로 reasoning ID를 다시 도입할 수 있습니다

## 대화/채팅 스레드

어떤 run 메서드를 호출하더라도 하나 이상의 에이전트가 실행될 수 있으며(따라서 하나 이상의 LLM 호출이 발생), 이는 채팅 대화에서 하나의 논리적 턴을 나타냅니다. 예를 들어:

1. 사용자 턴: 사용자가 텍스트를 입력합니다
2. Runner 실행: 첫 번째 에이전트가 LLM을 호출하고, 도구를 실행하고, 두 번째 에이전트로 핸드오프합니다. 두 번째 에이전트는 더 많은 도구를 실행한 뒤 출력을 생성합니다

에이전트 실행이 끝나면 사용자에게 무엇을 보여줄지 선택할 수 있습니다. 예를 들어 에이전트들이 생성한 모든 새 항목을 사용자에게 보여줄 수도 있고, 최종 출력만 보여줄 수도 있습니다. 어느 쪽이든 사용자가 후속 질문을 할 수 있으며, 그 경우 run 메서드를 다시 호출할 수 있습니다.

### 수동 대화 관리

[`RunResultBase.to_input_list()`][agents.result.RunResultBase.to_input_list] 메서드로 다음 턴의 입력을 가져와 대화 히스토리를 수동으로 관리할 수 있습니다:

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

### Sessions로 자동 대화 관리

더 간단한 접근을 위해 [Sessions](sessions/index.md)를 사용하면 `.to_input_list()`를 수동으로 호출하지 않고도 대화 히스토리를 자동으로 처리할 수 있습니다:

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

-   각 실행 전에 대화 히스토리를 조회합니다
-   각 실행 후 새 메시지를 저장합니다
-   서로 다른 세션 ID에 대해 별도의 대화를 유지합니다

!!! note

    세션 영속성은 서버 관리 대화 설정
    (`conversation_id`, `previous_response_id`, 또는 `auto_previous_response_id`)과
    동일한 실행에서 함께 사용할 수 없습니다. 호출마다 한 가지 접근을 선택하세요.

자세한 내용은 [Sessions 문서](sessions/index.md)를 참고하세요.

### 서버 관리 대화

`to_input_list()` 또는 `Sessions`로 로컬에서 상태를 처리하는 대신, OpenAI conversation state 기능이 서버 측에서 대화 상태를 관리하도록 할 수도 있습니다. 이를 통해 과거 메시지를 모두 다시 전송하지 않고도 대화 히스토리를 보존할 수 있습니다. 자세한 내용은 [OpenAI Conversation state 가이드](https://platform.openai.com/docs/guides/conversation-state?api-mode=responses)를 참고하세요.

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

또 다른 옵션은 **response chaining**으로, 각 턴이 이전 턴의 response ID에 명시적으로 연결됩니다.

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

!!! note

    SDK는 `conversation_locked` 오류를 백오프로 자동 재시도합니다. 서버 관리 대화 실행에서는
    재시도 전에 내부 conversation-tracker 입력을 되감아, 동일하게 준비된 항목들을 깔끔하게
    다시 보낼 수 있게 합니다.

    로컬의 세션 기반 실행(`conversation_id`,
    `previous_response_id`, 또는 `auto_previous_response_id`와 함께 사용할 수 없음)에서는
    SDK가 재시도 후 중복 히스토리 항목을 줄이기 위해 최근에 영속화된 입력 항목을
    최선의 노력으로 롤백합니다.

## Call model input filter

`call_model_input_filter`를 사용해 모델 호출 직전에 모델 입력을 편집할 수 있습니다. 이 훅은 현재 에이전트, 컨텍스트, 그리고 결합된 입력 항목(세션 히스토리가 있는 경우 이를 포함)을 받아 새 `ModelInputData`를 반환합니다.

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

`run_config`로 실행별로 훅을 설정하거나 `Runner`의 기본값으로 설정해, 민감한 데이터 마스킹, 긴 히스토리 트리밍, 추가 시스템 가이드 주입을 수행할 수 있습니다.

## 오류 핸들러

모든 `Runner` 엔트리 포인트는 오류 종류로 키잉된 dict인 `error_handlers`를 받습니다. 현재 지원되는 키는 `"max_turns"`입니다. `MaxTurnsExceeded`를 발생시키는 대신 제어된 최종 출력을 반환하고 싶을 때 사용하세요.

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

fallback 출력이 대화 히스토리에 추가되지 않길 원한다면 `include_in_history=False`로 설정하세요.

## 장기 실행 에이전트 & Human in the loop

도구 승인 일시정지/재개 패턴은 전용 [Human-in-the-loop 가이드](human_in_the_loop.md)를 참고하세요.

### Temporal

Agents SDK의 [Temporal](https://temporal.io/) 통합을 사용하면 휴먼인더루프 (HITL) 작업을 포함해 내구성 있는 장기 실행 워크플로를 실행할 수 있습니다. Temporal과 Agents SDK가 함께 동작해 장기 작업을 완료하는 데모는 [이 영상](https://www.youtube.com/watch?v=fFBZqzT4DD8)에서 확인할 수 있으며, [문서는 여기](https://github.com/temporalio/sdk-python/tree/main/temporalio/contrib/openai_agents)에서 볼 수 있습니다. 

### Restate

Agents SDK의 [Restate](https://restate.dev/) 통합을 사용하면 휴먼 승인, 핸드오프, 세션 관리 등을 포함한 경량의 내구성 있는 에이전트를 사용할 수 있습니다. 이 통합은 의존성으로 Restate의 단일 바이너리 런타임이 필요하며, 프로세스/컨테이너 또는 서버리스 함수로 에이전트를 실행하는 것을 지원합니다.
자세한 내용은 [개요](https://www.restate.dev/blog/durable-orchestration-for-ai-agents-with-restate-and-openai-sdk)를 읽거나 [문서](https://docs.restate.dev/ai)를 확인하세요.

### DBOS

Agents SDK의 [DBOS](https://dbos.dev/) 통합을 사용하면 장애 및 재시작에 걸쳐 진행 상황을 보존하는 신뢰할 수 있는 에이전트를 실행할 수 있습니다. 장기 실행 에이전트, 휴먼인더루프 (HITL) 워크플로, 핸드오프를 지원합니다. 동기 및 비동기 메서드를 모두 지원합니다. 이 통합은 SQLite 또는 Postgres 데이터베이스만 필요합니다. 자세한 내용은 통합 [repo](https://github.com/dbos-inc/dbos-openai-agents)와 [문서](https://docs.dbos.dev/integrations/openai-agents)를 참고하세요.

## 예외

SDK는 특정 경우에 예외를 발생시킵니다. 전체 목록은 [`agents.exceptions`][]에 있습니다. 개요는 다음과 같습니다:

-   [`AgentsException`][agents.exceptions.AgentsException]: SDK 내에서 발생하는 모든 예외의 베이스 클래스입니다. 다른 모든 구체적인 예외가 파생되는 일반 타입으로 사용됩니다
-   [`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded]: 에이전트 실행이 `Runner.run`, `Runner.run_sync`, 또는 `Runner.run_streamed` 메서드에 전달된 `max_turns` 제한을 초과할 때 발생합니다. 지정된 상호작용 턴 수 내에 에이전트가 작업을 완료할 수 없었음을 의미합니다
-   [`ModelBehaviorError`][agents.exceptions.ModelBehaviorError]: 기반 모델(LLM)이 예기치 않거나 유효하지 않은 출력을 생성할 때 발생합니다. 예를 들면 다음을 포함합니다:
    -   잘못된 형식의 JSON: 모델이 도구 호출 또는 직접 출력에 대해 잘못된 JSON 구조를 제공하는 경우, 특히 특정 `output_type`이 정의된 경우
    -   예기치 않은 도구 관련 실패: 모델이 예상된 방식으로 도구를 사용하지 못하는 경우
-   [`ToolTimeoutError`][agents.exceptions.ToolTimeoutError]: 함수 도구 호출이 구성된 타임아웃을 초과하고 도구가 `timeout_behavior="raise_exception"`을 사용하는 경우 발생합니다
-   [`UserError`][agents.exceptions.UserError]: SDK를 사용하는 코드 작성자(사용자)가 SDK를 사용하는 과정에서 오류를 냈을 때 발생합니다. 보통 잘못된 코드 구현, 유효하지 않은 구성, 또는 SDK API의 오용에서 비롯됩니다
-   [`InputGuardrailTripwireTriggered`][agents.exceptions.InputGuardrailTripwireTriggered], [`OutputGuardrailTripwireTriggered`][agents.exceptions.OutputGuardrailTripwireTriggered]: 각각 입력 가드레일 또는 출력 가드레일의 조건이 충족될 때 발생합니다. 입력 가드레일은 처리 전에 들어오는 메시지를 검사하고, 출력 가드레일은 전달 전에 에이전트의 최종 응답을 검사합니다