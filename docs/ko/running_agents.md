---
search:
  exclude: true
---
# 에이전트 실행

[`Runner`][agents.run.Runner] 클래스를 통해 에이전트를 실행할 수 있습니다. 3가지 옵션이 있습니다

1. [`Runner.run()`][agents.run.Runner.run]: 비동기로 실행되며 [`RunResult`][agents.result.RunResult]를 반환합니다
2. [`Runner.run_sync()`][agents.run.Runner.run_sync]: 동기 메서드이며 내부적으로 `.run()`을 실행합니다
3. [`Runner.run_streamed()`][agents.run.Runner.run_streamed]: 비동기로 실행되며 [`RunResultStreaming`][agents.result.RunResultStreaming]을 반환합니다. 스트리밍 모드로 LLM을 호출하고, 수신되는 이벤트를 실시간으로 스트리밍합니다

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

자세한 내용은 [결과 가이드](results.md)에서 확인하세요

## Runner 수명 주기 및 구성

### 에이전트 루프

`Runner`의 run 메서드를 사용할 때 시작 에이전트와 입력을 전달합니다. 입력은 다음 중 하나일 수 있습니다

-   문자열(사용자 메시지로 처리)
-   OpenAI Responses API 형식의 입력 항목 목록
-   중단된 실행을 재개할 때의 [`RunState`][agents.run_state.RunState]

그다음 runner는 루프를 실행합니다

1. 현재 입력으로 현재 에이전트에 대해 LLM을 호출합니다
2. LLM이 출력을 생성합니다
    1. LLM이 `final_output`을 반환하면 루프를 종료하고 결과를 반환합니다
    2. LLM이 핸드오프를 수행하면 현재 에이전트와 입력을 업데이트하고 루프를 다시 실행합니다
    3. LLM이 도구 호출을 생성하면 해당 도구 호출을 실행하고 결과를 추가한 뒤 루프를 다시 실행합니다
3. 전달된 `max_turns`를 초과하면 [`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded] 예외를 발생시킵니다

!!! note

    LLM 출력이 "최종 출력"으로 간주되는 규칙은, 원하는 타입의 텍스트 출력을 생성하고 도구 호출이 없어야 한다는 점입니다

### 스트리밍

스트리밍을 사용하면 LLM 실행 중 스트리밍 이벤트를 추가로 받을 수 있습니다. 스트림이 완료되면 [`RunResultStreaming`][agents.result.RunResultStreaming]에는 실행에 대한 전체 정보(새로 생성된 모든 출력 포함)가 담깁니다. 스트리밍 이벤트는 `.stream_events()`를 호출해 받을 수 있습니다. 자세한 내용은 [스트리밍 가이드](streaming.md)를 참고하세요

#### Responses WebSocket 전송(선택적 헬퍼)

OpenAI Responses websocket 전송을 활성화하면 일반 `Runner` API를 계속 사용할 수 있습니다. websocket 세션 헬퍼는 연결 재사용에 권장되지만 필수는 아닙니다

이는 websocket 전송 기반 Responses API이며 [Realtime API](realtime/guide.md)가 아닙니다

##### 패턴 1: 세션 헬퍼 없음(동작함)

websocket 전송만 필요하고 SDK가 공유 provider/session을 관리할 필요가 없을 때 사용하세요

```python
import asyncio

from agents import Agent, Runner, set_default_openai_responses_transport


async def main():
    set_default_openai_responses_transport("websocket")

    agent = Agent(name="Assistant", instructions="Be concise.")
    result = Runner.run_streamed(agent, "Summarize recursion in one sentence.")

    async for event in result.stream_events():
        if event.type == "raw_response_event":
            continue
        print(event.type)


asyncio.run(main())
```

이 패턴은 단일 실행에는 적합합니다. `Runner.run()` / `Runner.run_streamed()`를 반복 호출하면, 동일한 `RunConfig` / provider 인스턴스를 수동 재사용하지 않는 한 각 실행에서 재연결될 수 있습니다

##### 패턴 2: `responses_websocket_session()` 사용(멀티턴 재사용 권장)

여러 실행에서 websocket 지원 provider와 `RunConfig`를 공유하려면 [`responses_websocket_session()`][agents.responses_websocket_session]을 사용하세요(`run_config`를 상속하는 중첩 agent-as-tool 호출 포함)

```python
import asyncio

from agents import Agent, responses_websocket_session


async def main():
    agent = Agent(name="Assistant", instructions="Be concise.")

    async with responses_websocket_session() as ws:
        first = ws.run_streamed(agent, "Say hello in one short sentence.")
        async for _event in first.stream_events():
            pass

        second = ws.run_streamed(
            agent,
            "Now say goodbye.",
            previous_response_id=first.last_response_id,
        )
        async for _event in second.stream_events():
            pass


asyncio.run(main())
```

컨텍스트를 종료하기 전에 스트리밍된 결과 소비를 마치세요. websocket 요청이 진행 중일 때 컨텍스트를 종료하면 공유 연결이 강제 종료될 수 있습니다

### 실행 구성

`run_config` 매개변수로 에이전트 실행의 전역 설정 일부를 구성할 수 있습니다

#### 공통 실행 구성 카테고리

각 에이전트 정의를 변경하지 않고 단일 실행의 동작을 재정의하려면 `RunConfig`를 사용하세요

##### 모델, provider, 세션 기본값

-   [`model`][agents.run.RunConfig.model]: 각 Agent의 `model`과 관계없이 전역 LLM 모델을 설정할 수 있습니다
-   [`model_provider`][agents.run.RunConfig.model_provider]: 모델 이름 조회를 위한 model provider이며 기본값은 OpenAI입니다
-   [`model_settings`][agents.run.RunConfig.model_settings]: 에이전트별 설정을 재정의합니다. 예를 들어 전역 `temperature` 또는 `top_p`를 설정할 수 있습니다
-   [`session_settings`][agents.run.RunConfig.session_settings]: 실행 중 히스토리를 조회할 때 세션 수준 기본값(예: `SessionSettings(limit=...)`)을 재정의합니다
-   [`session_input_callback`][agents.run.RunConfig.session_input_callback]: Sessions 사용 시 각 턴 전에 새 사용자 입력을 세션 히스토리와 병합하는 방식을 사용자 지정합니다. 콜백은 sync 또는 async일 수 있습니다

##### 가드레일, 핸드오프, 모델 입력 형태 조정

-   [`input_guardrails`][agents.run.RunConfig.input_guardrails], [`output_guardrails`][agents.run.RunConfig.output_guardrails]: 모든 실행에 포함할 입력/출력 가드레일 목록
-   [`handoff_input_filter`][agents.run.RunConfig.handoff_input_filter]: 핸드오프에 이미 필터가 없는 경우 모든 핸드오프에 적용할 전역 입력 필터입니다. 입력 필터를 사용해 새 에이전트로 전송되는 입력을 편집할 수 있습니다. 자세한 내용은 [`Handoff.input_filter`][agents.handoffs.Handoff.input_filter] 문서를 참고하세요
-   [`nest_handoff_history`][agents.run.RunConfig.nest_handoff_history]: 다음 에이전트 호출 전에 이전 대화록을 단일 assistant 메시지로 축약하는 옵트인 베타 기능입니다. 중첩 핸드오프 안정화 중이므로 기본적으로 비활성화되어 있습니다. 활성화하려면 `True`, 원문 대화록을 그대로 전달하려면 `False`로 두세요. [Runner 메서드][agents.run.Runner]는 전달하지 않은 경우 자동으로 `RunConfig`를 생성하므로 quickstart와 예제는 기본 비활성 상태를 유지하며, 명시적 [`Handoff.input_filter`][agents.handoffs.Handoff.input_filter] 콜백은 계속 이를 우선 재정의합니다. 개별 핸드오프는 [`Handoff.nest_handoff_history`][agents.handoffs.Handoff.nest_handoff_history]로 이 설정을 재정의할 수 있습니다
-   [`handoff_history_mapper`][agents.run.RunConfig.handoff_history_mapper]: `nest_handoff_history`를 옵트인했을 때마다 정규화된 대화록(히스토리 + 핸드오프 항목)을 받는 선택적 callable입니다. 다음 에이전트로 전달할 정확한 입력 항목 목록을 반환해야 하며, 전체 핸드오프 필터 작성 없이 내장 요약을 대체할 수 있습니다
-   [`call_model_input_filter`][agents.run.RunConfig.call_model_input_filter]: 모델 호출 직전에 완전히 준비된 모델 입력(instructions 및 입력 항목)을 편집하는 훅입니다. 예: 히스토리 축소, 시스템 프롬프트 주입
-   [`reasoning_item_id_policy`][agents.run.RunConfig.reasoning_item_id_policy]: runner가 이전 출력을 다음 턴 모델 입력으로 변환할 때 reasoning item ID를 유지할지 생략할지 제어합니다

##### 트레이싱 및 관측 가능성

-   [`tracing_disabled`][agents.run.RunConfig.tracing_disabled]: 전체 실행에 대해 [트레이싱](tracing.md)을 비활성화할 수 있습니다
-   [`tracing`][agents.run.RunConfig.tracing]: [`TracingConfig`][agents.tracing.TracingConfig]를 전달해 이번 실행의 exporter, processor, 트레이싱 메타데이터를 재정의합니다
-   [`trace_include_sensitive_data`][agents.run.RunConfig.trace_include_sensitive_data]: LLM 및 도구 호출 입력/출력 같은 잠재적 민감 데이터를 trace에 포함할지 설정합니다
-   [`workflow_name`][agents.run.RunConfig.workflow_name], [`trace_id`][agents.run.RunConfig.trace_id], [`group_id`][agents.run.RunConfig.group_id]: 실행의 트레이싱 workflow 이름, trace ID, trace group ID를 설정합니다. 최소한 `workflow_name` 설정을 권장합니다. group ID는 여러 실행의 trace를 연결하는 선택 필드입니다
-   [`trace_metadata`][agents.run.RunConfig.trace_metadata]: 모든 trace에 포함할 메타데이터

##### 도구 승인 및 도구 오류 동작

-   [`tool_error_formatter`][agents.run.RunConfig.tool_error_formatter]: 승인 플로우에서 도구 호출이 거부될 때 모델에 보이는 메시지를 사용자 지정합니다

중첩 핸드오프는 옵트인 베타로 제공됩니다. 축약 대화록 동작은 `RunConfig(nest_handoff_history=True)`를 전달하거나 특정 핸드오프에 `handoff(..., nest_handoff_history=True)`를 설정해 활성화할 수 있습니다. 원문 대화록(기본값)을 유지하려면 플래그를 설정하지 않거나, 대화를 원하는 형태로 그대로 전달하는 `handoff_input_filter`(또는 `handoff_history_mapper`)를 제공하세요. 커스텀 mapper 작성 없이 생성 요약에 사용되는 래퍼 텍스트를 바꾸려면 [`set_conversation_history_wrappers`][agents.handoffs.set_conversation_history_wrappers]를 호출하세요(기본값 복원은 [`reset_conversation_history_wrappers`][agents.handoffs.reset_conversation_history_wrappers])

#### 실행 구성 세부사항

##### `tool_error_formatter`

승인 플로우에서 도구 호출이 거부될 때 모델에 반환되는 메시지를 사용자 지정하려면 `tool_error_formatter`를 사용하세요

formatter는 다음 필드를 가진 [`ToolErrorFormatterArgs`][agents.run_config.ToolErrorFormatterArgs]를 받습니다

-   `kind`: 오류 카테고리. 현재는 `"approval_rejected"`입니다
-   `tool_type`: 도구 런타임(`"function"`, `"computer"`, `"shell"`, `"apply_patch"`)
-   `tool_name`: 도구 이름
-   `call_id`: 도구 호출 ID
-   `default_message`: SDK 기본 모델 표시 메시지
-   `run_context`: 활성 실행 컨텍스트 래퍼

메시지를 대체할 문자열을 반환하거나, SDK 기본값을 사용하려면 `None`을 반환하세요

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

##### `reasoning_item_id_policy`

`reasoning_item_id_policy`는 runner가 히스토리를 다음 턴으로 전달할 때(예: `RunResult.to_input_list()` 사용 또는 session 기반 실행) reasoning item을 다음 턴 모델 입력으로 변환하는 방식을 제어합니다

-   `None` 또는 `"preserve"`(기본값): reasoning item ID 유지
-   `"omit"`: 생성된 다음 턴 입력에서 reasoning item ID 제거

`"omit"`은 주로 Responses API 400 오류 유형에 대한 옵트인 완화책으로 사용합니다. 이 오류는 reasoning item이 `id`와 함께 전송되지만 필수 후속 항목이 없을 때 발생합니다(예: `Item 'rs_...' of type 'reasoning' was provided without its required following item.`)

이 상황은 SDK가 이전 출력으로 후속 입력을 구성하는 멀티턴 에이전트 실행에서 발생할 수 있습니다(세션 영속성, 서버 관리 대화 델타, 스트리밍/비스트리밍 후속 턴, 재개 경로 포함). reasoning item ID는 유지되지만 provider는 해당 ID가 대응되는 후속 항목과 계속 짝지어지기를 요구할 수 있습니다

`reasoning_item_id_policy="omit"`을 설정하면 reasoning 내용은 유지하면서 reasoning item `id`만 제거하므로, SDK가 생성한 후속 입력에서 이 API 불변 조건을 트리거하지 않게 됩니다

적용 범위 참고

-   이는 SDK가 후속 입력을 구성할 때 생성/전달하는 reasoning item에만 영향을 줍니다
-   사용자가 제공한 초기 입력 항목은 다시 작성하지 않습니다
-   이 정책 적용 후에도 `call_model_input_filter`는 의도적으로 reasoning ID를 다시 도입할 수 있습니다

## 상태 및 대화 관리

### 메모리 전략 선택

다음 턴으로 상태를 전달하는 일반적인 방법은 네 가지입니다

| 전략 | 상태 저장 위치 | 최적 용도 | 다음 턴에 전달할 내용 |
| --- | --- | --- | --- |
| `result.to_input_list()` | 앱 메모리 | 소규모 채팅 루프, 완전 수동 제어, 모든 provider | `result.to_input_list()`의 목록 + 다음 사용자 메시지 |
| `session` | 사용자 저장소 + SDK | 영속 채팅 상태, 재개 가능한 실행, 커스텀 저장소 | 동일한 `session` 인스턴스 또는 같은 저장소를 가리키는 다른 인스턴스 |
| `conversation_id` | OpenAI Conversations API | 워커/서비스 간 공유하려는 이름 있는 서버 측 대화 | 동일한 `conversation_id` + 새 사용자 턴만 |
| `previous_response_id` | OpenAI Responses API | 대화 리소스 생성 없이 경량 서버 관리 연속성 | `result.last_response_id` + 새 사용자 턴만 |

`result.to_input_list()`와 `session`은 클라이언트 관리 방식입니다. `conversation_id`와 `previous_response_id`는 OpenAI 관리 방식이며 OpenAI Responses API 사용 시에만 적용됩니다. 대부분의 애플리케이션에서는 대화당 하나의 영속화 전략을 선택하세요. 클라이언트 관리 히스토리와 OpenAI 관리 상태를 함께 사용하면 의도적으로 두 계층을 조정하지 않는 한 컨텍스트가 중복될 수 있습니다

!!! note

    세션 영속성은 서버 관리 대화 설정
    (`conversation_id`, `previous_response_id`, `auto_previous_response_id`)과
    동일 실행에서 함께 사용할 수 없습니다. 호출당 한 가지 접근법을 선택하세요

### 대화/채팅 스레드

어떤 run 메서드를 호출하더라도 하나 이상의 에이전트가 실행될 수 있고(따라서 하나 이상의 LLM 호출 발생), 논리적으로는 채팅 대화의 단일 턴을 나타냅니다. 예를 들어

1. 사용자 턴: 사용자가 텍스트 입력
2. Runner 실행: 첫 번째 에이전트가 LLM 호출, 도구 실행, 두 번째 에이전트로 핸드오프, 두 번째 에이전트가 추가 도구 실행 후 출력 생성

에이전트 실행이 끝나면 사용자에게 무엇을 보여줄지 선택할 수 있습니다. 예를 들어 에이전트가 생성한 모든 새 항목을 보여주거나 최종 출력만 보여줄 수 있습니다. 어느 쪽이든 이후 사용자가 후속 질문을 하면 run 메서드를 다시 호출하면 됩니다

#### 수동 대화 관리

다음 턴 입력을 얻기 위해 [`RunResultBase.to_input_list()`][agents.result.RunResultBase.to_input_list] 메서드로 대화 히스토리를 수동 관리할 수 있습니다

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

#### Sessions를 사용한 자동 대화 관리

더 간단한 접근으로, `.to_input_list()`를 수동 호출하지 않고 대화 히스토리를 자동 처리하려면 [Sessions](sessions/index.md)를 사용할 수 있습니다

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

Sessions는 자동으로 다음을 수행합니다

-   각 실행 전에 대화 히스토리 조회
-   각 실행 후 새 메시지 저장
-   서로 다른 세션 ID별로 별도 대화 유지

자세한 내용은 [Sessions 문서](sessions/index.md)를 참고하세요

#### 서버 관리 대화

`to_input_list()` 또는 `Sessions`로 로컬 처리하는 대신 OpenAI 대화 상태 기능으로 서버 측 대화 상태를 관리할 수도 있습니다. 이를 통해 과거 메시지를 모두 수동 재전송하지 않고 대화 히스토리를 보존할 수 있습니다. 아래 두 가지 서버 관리 방식 모두에서 요청마다 새 턴 입력만 전달하고 저장된 ID를 재사용하세요. 자세한 내용은 [OpenAI Conversation state 가이드](https://platform.openai.com/docs/guides/conversation-state?api-mode=responses)를 참고하세요

OpenAI는 턴 간 상태를 추적하는 두 가지 방법을 제공합니다

##### 1. `conversation_id` 사용

먼저 OpenAI Conversations API로 대화를 만들고 이후 모든 호출에서 해당 ID를 재사용합니다

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

##### 2. `previous_response_id` 사용

다른 옵션은 **응답 체이닝**으로, 각 턴이 이전 턴의 응답 ID에 명시적으로 연결됩니다

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

실행이 승인 대기 상태로 일시 중지되었다가 [`RunState`][agents.run_state.RunState]에서 재개되면,
SDK는 저장된 `conversation_id` / `previous_response_id` / `auto_previous_response_id`
설정을 유지하므로 재개된 턴이 동일한 서버 관리 대화에서 계속됩니다

`conversation_id`와 `previous_response_id`는 상호 배타적입니다. 시스템 간 공유 가능한 이름 있는 대화 리소스가 필요하면 `conversation_id`를 사용하세요. 턴 간 가장 경량의 Responses API 연속성 프리미티브가 필요하면 `previous_response_id`를 사용하세요

!!! note

    SDK는 `conversation_locked` 오류를 백오프로 자동 재시도합니다. 서버 관리
    대화 실행에서는 재시도 전에 내부 conversation-tracker 입력을 되감아
    동일한 준비 항목을 깔끔하게 재전송할 수 있게 합니다

    로컬 session 기반 실행(`conversation_id`,
    `previous_response_id`, `auto_previous_response_id`와 함께 사용할 수 없음)에서도 SDK는
    재시도 후 중복 히스토리 항목을 줄이기 위해 최근에 저장된 입력 항목을
    최선의 노력으로 롤백합니다

## 훅 및 사용자 지정

### 모델 호출 입력 필터

모델 호출 직전에 모델 입력을 편집하려면 `call_model_input_filter`를 사용하세요. 이 훅은 현재 에이전트, 컨텍스트, 결합된 입력 항목(세션 히스토리 포함 시 포함)을 받아 새로운 `ModelInputData`를 반환합니다

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

민감 데이터 마스킹, 긴 히스토리 축소, 추가 시스템 가이드 주입 등을 위해 `run_config`를 통해 실행별로 훅을 설정하세요

## 오류 및 복구

### 오류 핸들러

모든 `Runner` 진입점은 오류 종류를 키로 하는 dict인 `error_handlers`를 받습니다. 현재 지원 키는 `"max_turns"`입니다. `MaxTurnsExceeded`를 발생시키는 대신 제어된 최종 출력을 반환하려면 사용하세요

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

대체 출력을 대화 히스토리에 추가하고 싶지 않다면 `include_in_history=False`로 설정하세요

## 내구성 실행 통합 및 휴먼인더루프 (HITL)

도구 승인 일시중지/재개 패턴은 전용 [Human-in-the-loop 가이드](human_in_the_loop.md)부터 확인하세요
아래 통합은 실행이 긴 대기, 재시도, 프로세스 재시작에 걸칠 수 있는 내구성 오케스트레이션용입니다

### Temporal

Agents SDK의 [Temporal](https://temporal.io/) 통합을 사용해 휴먼인더루프 작업을 포함한 내구성 있는 장기 실행 워크플로를 실행할 수 있습니다. Temporal과 Agents SDK가 함께 장기 실행 작업을 완료하는 데모는 [이 영상](https://www.youtube.com/watch?v=fFBZqzT4DD8)에서 확인할 수 있으며, [문서는 여기](https://github.com/temporalio/sdk-python/tree/main/temporalio/contrib/openai_agents)에서 볼 수 있습니다

### Restate

Agents SDK의 [Restate](https://restate.dev/) 통합을 사용해 사람 승인, 핸드오프, 세션 관리를 포함한 경량의 내구성 에이전트를 구현할 수 있습니다. 이 통합은 Restate의 단일 바이너리 런타임을 의존성으로 요구하며, 프로세스/컨테이너 또는 serverless 함수로 에이전트를 실행하는 것을 지원합니다
자세한 내용은 [개요](https://www.restate.dev/blog/durable-orchestration-for-ai-agents-with-restate-and-openai-sdk) 또는 [문서](https://docs.restate.dev/ai)를 참고하세요

### DBOS

Agents SDK의 [DBOS](https://dbos.dev/) 통합을 사용해 실패 및 재시작 간에도 진행 상태를 보존하는 신뢰성 높은 에이전트를 실행할 수 있습니다. 장기 실행 에이전트, 휴먼인더루프 워크플로, 핸드오프를 지원합니다. sync와 async 메서드를 모두 지원합니다. 이 통합은 SQLite 또는 Postgres 데이터베이스만 필요합니다. 자세한 내용은 통합 [repo](https://github.com/dbos-inc/dbos-openai-agents)와 [문서](https://docs.dbos.dev/integrations/openai-agents)를 참고하세요

## 예외

SDK는 특정 경우 예외를 발생시킵니다. 전체 목록은 [`agents.exceptions`][]에 있습니다. 개요는 다음과 같습니다

-   [`AgentsException`][agents.exceptions.AgentsException]: SDK 내에서 발생하는 모든 예외의 기본 클래스입니다. 다른 모든 구체적 예외가 파생되는 일반 타입 역할을 합니다
-   [`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded]: 에이전트 실행이 `Runner.run`, `Runner.run_sync`, `Runner.run_streamed` 메서드에 전달된 `max_turns` 한도를 초과할 때 발생합니다. 지정된 상호작용 턴 수 내에서 작업을 완료하지 못했음을 나타냅니다
-   [`ModelBehaviorError`][agents.exceptions.ModelBehaviorError]: 기반 모델(LLM)이 예상치 못하거나 유효하지 않은 출력을 생성할 때 발생합니다. 예시는 다음과 같습니다
    -   형식이 잘못된 JSON: 특히 특정 `output_type`이 정의된 경우, 도구 호출용 또는 직접 출력에서 모델이 잘못된 JSON 구조를 제공한 경우
    -   예상치 못한 도구 관련 실패: 모델이 예상된 방식으로 도구를 사용하지 못한 경우
-   [`ToolTimeoutError`][agents.exceptions.ToolTimeoutError]: 함수 도구 호출이 구성된 타임아웃을 초과하고 도구가 `timeout_behavior="raise_exception"`을 사용할 때 발생합니다
-   [`UserError`][agents.exceptions.UserError]: SDK를 사용하는 코드 작성자(사용자)가 SDK 사용 중 오류를 만들었을 때 발생합니다. 일반적으로 잘못된 코드 구현, 유효하지 않은 구성, SDK API 오용으로 인해 발생합니다
-   [`InputGuardrailTripwireTriggered`][agents.exceptions.InputGuardrailTripwireTriggered], [`OutputGuardrailTripwireTriggered`][agents.exceptions.OutputGuardrailTripwireTriggered]: 각각 입력 가드레일 또는 출력 가드레일 조건이 충족될 때 발생합니다. 입력 가드레일은 처리 전에 들어오는 메시지를 검사하고, 출력 가드레일은 전달 전에 에이전트의 최종 응답을 검사합니다