---
search:
  exclude: true
---
# 에이전트 실행

[`Runner`][agents.run.Runner] 클래스를 통해 에이전트를 실행할 수 있습니다. 옵션은 3가지입니다

1. [`Runner.run()`][agents.run.Runner.run]: 비동기로 실행되며 [`RunResult`][agents.result.RunResult]를 반환합니다
2. [`Runner.run_sync()`][agents.run.Runner.run_sync]: 동기 메서드이며 내부적으로 `.run()`을 실행합니다
3. [`Runner.run_streamed()`][agents.run.Runner.run_streamed]: 비동기로 실행되며 [`RunResultStreaming`][agents.result.RunResultStreaming]을 반환합니다. 스트리밍 모드로 LLM을 호출하고, 이벤트를 수신되는 즉시 스트리밍합니다

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

자세한 내용은 [결과 가이드](results.md)를 참고하세요

## Runner 수명 주기 및 구성

### 에이전트 루프

`Runner`의 run 메서드를 사용할 때 시작 에이전트와 입력을 전달합니다. 입력은 다음 중 하나일 수 있습니다

-   문자열(사용자 메시지로 처리됨)
-   OpenAI Responses API 형식의 입력 항목 목록
-   중단된 실행을 재개할 때의 [`RunState`][agents.run_state.RunState]

Runner는 다음 루프를 실행합니다

1. 현재 입력으로 현재 에이전트의 LLM을 호출합니다
2. LLM이 출력을 생성합니다
    1. LLM이 `final_output`을 반환하면 루프를 종료하고 결과를 반환합니다
    2. LLM이 핸드오프를 수행하면 현재 에이전트와 입력을 업데이트하고 루프를 다시 실행합니다
    3. LLM이 도구 호출을 생성하면 해당 도구 호출을 실행하고 결과를 추가한 뒤 루프를 다시 실행합니다
3. 전달된 `max_turns`를 초과하면 [`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded] 예외를 발생시킵니다

!!! note

    LLM 출력이 "최종 출력"으로 간주되는 기준은 원하는 타입의 텍스트 출력을 생성하고, 도구 호출이 없는 경우입니다

### 스트리밍

스트리밍을 사용하면 LLM 실행 중 스트리밍 이벤트도 추가로 받을 수 있습니다. 스트림이 완료되면 [`RunResultStreaming`][agents.result.RunResultStreaming]에는 실행에 대한 전체 정보(생성된 모든 새 출력 포함)가 담깁니다. 스트리밍 이벤트는 `.stream_events()`를 호출해 받을 수 있습니다. 자세한 내용은 [스트리밍 가이드](streaming.md)를 참고하세요

#### Responses WebSocket 전송(선택적 헬퍼)

OpenAI Responses websocket 전송을 활성화해도 일반 `Runner` API를 계속 사용할 수 있습니다. 연결 재사용에는 websocket session helper를 권장하지만 필수는 아닙니다

이것은 websocket 전송 기반 Responses API이며, [Realtime API](realtime/guide.md)가 아닙니다

구체적인 모델 객체 또는 커스텀 provider 관련 전송 선택 규칙과 주의사항은 [Models](models/index.md#responses-websocket-transport)를 참고하세요

##### 패턴 1: session helper 없이 사용(동작함)

websocket 전송만 원하고 SDK가 공유 provider/session을 관리할 필요가 없을 때 사용합니다

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

이 패턴은 단일 실행에 적합합니다. `Runner.run()` / `Runner.run_streamed()`를 반복 호출하면, 동일한 `RunConfig` / provider 인스턴스를 수동으로 재사용하지 않는 한 실행마다 재연결될 수 있습니다

##### 패턴 2: `responses_websocket_session()` 사용(다중 턴 재사용 권장)

여러 실행 간(동일한 `run_config`를 상속하는 중첩 agent-as-tool 호출 포함) websocket 지원 provider와 `RunConfig`를 공유하려면 [`responses_websocket_session()`][agents.responses_websocket_session]을 사용하세요

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

컨텍스트를 종료하기 전에 스트리밍 결과 소비를 완료하세요. websocket 요청이 진행 중일 때 컨텍스트를 종료하면 공유 연결이 강제로 닫힐 수 있습니다

### Run config

`run_config` 매개변수로 에이전트 실행의 일부 전역 설정을 구성할 수 있습니다

#### 공통 Run config 카테고리

각 에이전트 정의를 변경하지 않고 단일 실행의 동작을 재정의하려면 `RunConfig`를 사용하세요

##### 모델, provider, session 기본값

-   [`model`][agents.run.RunConfig.model]: 각 Agent의 `model` 설정과 무관하게 전역 LLM 모델을 설정할 수 있습니다
-   [`model_provider`][agents.run.RunConfig.model_provider]: 모델 이름 조회용 model provider이며 기본값은 OpenAI입니다
-   [`model_settings`][agents.run.RunConfig.model_settings]: 에이전트별 설정을 재정의합니다. 예를 들어 전역 `temperature` 또는 `top_p`를 설정할 수 있습니다
-   [`session_settings`][agents.run.RunConfig.session_settings]: 실행 중 기록을 조회할 때 session 수준 기본값(예: `SessionSettings(limit=...)`)을 재정의합니다
-   [`session_input_callback`][agents.run.RunConfig.session_input_callback]: Sessions 사용 시 매 턴 전에 새 사용자 입력을 session 기록과 병합하는 방식을 사용자 지정합니다. 콜백은 동기 또는 비동기일 수 있습니다

##### 가드레일, 핸드오프, 모델 입력 형태 조정

-   [`input_guardrails`][agents.run.RunConfig.input_guardrails], [`output_guardrails`][agents.run.RunConfig.output_guardrails]: 모든 실행에 포함할 입력/출력 가드레일 목록입니다
-   [`handoff_input_filter`][agents.run.RunConfig.handoff_input_filter]: 핸드오프에 이미 필터가 없는 경우 모든 핸드오프에 적용할 전역 입력 필터입니다. 새 에이전트로 전송되는 입력을 편집할 수 있습니다. 자세한 내용은 [`Handoff.input_filter`][agents.handoffs.Handoff.input_filter] 문서를 참고하세요
-   [`nest_handoff_history`][agents.run.RunConfig.nest_handoff_history]: 다음 에이전트를 호출하기 전에 이전 transcript를 단일 assistant 메시지로 접는 옵트인 베타 기능입니다. 중첩 핸드오프 안정화 전까지 기본 비활성화되어 있으며, 활성화하려면 `True`, 원문 transcript를 전달하려면 `False`로 두세요. 전달하지 않으면 모든 [Runner methods][agents.run.Runner]는 자동으로 `RunConfig`를 생성하므로 quickstart와 예제는 기본적으로 꺼져 있으며, 명시적 [`Handoff.input_filter`][agents.handoffs.Handoff.input_filter] 콜백은 계속 이를 재정의합니다. 개별 핸드오프는 [`Handoff.nest_handoff_history`][agents.handoffs.Handoff.nest_handoff_history]로 이 설정을 재정의할 수 있습니다
-   [`handoff_history_mapper`][agents.run.RunConfig.handoff_history_mapper]: `nest_handoff_history`를 옵트인했을 때 정규화된 transcript(기록 + 핸드오프 항목)를 받는 선택적 callable입니다. 다음 에이전트로 전달할 정확한 입력 항목 목록을 반환해야 하며, 전체 핸드오프 필터를 작성하지 않고 내장 요약을 대체할 수 있습니다
-   [`call_model_input_filter`][agents.run.RunConfig.call_model_input_filter]: 모델 호출 직전에 완전히 준비된 모델 입력(instructions 및 입력 항목)을 편집하는 훅입니다. 예: 기록 축약, 시스템 프롬프트 주입
-   [`reasoning_item_id_policy`][agents.run.RunConfig.reasoning_item_id_policy]: runner가 이전 출력을 다음 턴 모델 입력으로 변환할 때 reasoning item ID를 유지할지 생략할지 제어합니다

##### 트레이싱 및 관측 가능성

-   [`tracing_disabled`][agents.run.RunConfig.tracing_disabled]: 전체 실행에 대해 [tracing](tracing.md)을 비활성화할 수 있습니다
-   [`tracing`][agents.run.RunConfig.tracing]: [`TracingConfig`][agents.tracing.TracingConfig]를 전달해 이 실행의 exporter, processor, tracing 메타데이터를 재정의합니다
-   [`trace_include_sensitive_data`][agents.run.RunConfig.trace_include_sensitive_data]: trace에 LLM 및 도구 호출 입력/출력 같은 민감할 수 있는 데이터를 포함할지 구성합니다
-   [`workflow_name`][agents.run.RunConfig.workflow_name], [`trace_id`][agents.run.RunConfig.trace_id], [`group_id`][agents.run.RunConfig.group_id]: 실행의 tracing workflow 이름, trace ID, trace group ID를 설정합니다. 최소한 `workflow_name` 설정을 권장합니다. group ID는 여러 실행의 trace를 연결할 수 있는 선택 필드입니다
-   [`trace_metadata`][agents.run.RunConfig.trace_metadata]: 모든 trace에 포함할 메타데이터입니다

##### 도구 승인 및 도구 오류 동작

-   [`tool_error_formatter`][agents.run.RunConfig.tool_error_formatter]: 승인 흐름에서 도구 호출이 거부될 때 모델에 보이는 메시지를 사용자 지정합니다

중첩 핸드오프는 옵트인 베타로 제공됩니다. 축약 transcript 동작을 사용하려면 `RunConfig(nest_handoff_history=True)`를 전달하거나 특정 핸드오프에만 적용하려면 `handoff(..., nest_handoff_history=True)`를 설정하세요. 원문 transcript(기본값)를 유지하려면 플래그를 설정하지 않거나, 필요한 형태로 대화를 그대로 전달하는 `handoff_input_filter`(또는 `handoff_history_mapper`)를 제공하세요. 커스텀 mapper 없이 생성 요약의 래퍼 텍스트를 바꾸려면 [`set_conversation_history_wrappers`][agents.handoffs.set_conversation_history_wrappers]를 호출하세요(기본값 복원은 [`reset_conversation_history_wrappers`][agents.handoffs.reset_conversation_history_wrappers])

#### Run config 세부 정보

##### `tool_error_formatter`

`tool_error_formatter`를 사용하면 승인 흐름에서 도구 호출이 거부될 때 모델에 반환되는 메시지를 사용자 지정할 수 있습니다

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

`reasoning_item_id_policy`는 runner가 기록을 이어갈 때(예: `RunResult.to_input_list()` 또는 session 기반 실행 사용 시) reasoning item을 다음 턴 모델 입력으로 변환하는 방식을 제어합니다

-   `None` 또는 `"preserve"`(기본값): reasoning item ID 유지
-   `"omit"`: 생성되는 다음 턴 입력에서 reasoning item ID 제거

`"omit"`은 주로 Responses API 400 오류의 한 유형에 대한 옵트인 완화책으로 사용합니다. 해당 오류는 reasoning item이 `id`와 함께 전송되었지만 필수 후속 항목이 없을 때 발생합니다(예: `Item 'rs_...' of type 'reasoning' was provided without its required following item.`)

이는 다중 턴 에이전트 실행에서 SDK가 이전 출력으로 후속 입력을 구성할 때(세션 영속성, 서버 관리 대화 델타, 스트리밍/비스트리밍 후속 턴, 재개 경로 포함) reasoning item ID는 유지되지만 provider가 해당 ID를 대응되는 후속 항목과 함께 유지하도록 요구하는 경우 발생할 수 있습니다

`reasoning_item_id_policy="omit"`을 설정하면 reasoning 내용은 유지하되 reasoning item `id`를 제거하므로, SDK가 생성한 후속 입력에서 해당 API 불변 조건 위반을 피할 수 있습니다

적용 범위 참고

-   이는 SDK가 후속 입력을 구성할 때 생성/전달하는 reasoning item에만 영향을 줍니다
-   사용자 제공 초기 입력 항목은 재작성하지 않습니다
-   이 정책 적용 후에도 `call_model_input_filter`로 의도적으로 reasoning ID를 다시 추가할 수 있습니다

## 상태 및 대화 관리

### 메모리 전략 선택

다음 턴으로 상태를 전달하는 일반적인 방법은 네 가지입니다

| 전략 | 상태 저장 위치 | 적합한 용도 | 다음 턴에 전달할 항목 |
| --- | --- | --- | --- |
| `result.to_input_list()` | 앱 메모리 | 소규모 채팅 루프, 완전한 수동 제어, 모든 provider | `result.to_input_list()`의 목록 + 다음 사용자 메시지 |
| `session` | 사용자 저장소 + SDK | 영구 채팅 상태, 재개 가능한 실행, 커스텀 저장소 | 동일한 `session` 인스턴스 또는 같은 저장소를 가리키는 다른 인스턴스 |
| `conversation_id` | OpenAI Conversations API | 워커/서비스 간 공유할 이름 있는 서버 측 대화 | 동일한 `conversation_id` + 새 사용자 턴만 |
| `previous_response_id` | OpenAI Responses API | 대화 리소스 생성 없이 가벼운 서버 관리 연속성 | `result.last_response_id` + 새 사용자 턴만 |

`result.to_input_list()`와 `session`은 클라이언트 관리 방식입니다. `conversation_id`와 `previous_response_id`는 OpenAI 관리 방식이며 OpenAI Responses API 사용 시에만 적용됩니다. 대부분의 애플리케이션에서는 대화당 하나의 영속성 전략만 선택하세요. 클라이언트 관리 기록과 OpenAI 관리 상태를 혼합하면 두 계층을 의도적으로 조정하지 않는 한 컨텍스트가 중복될 수 있습니다

!!! note

    Session 영속성은 서버 관리 대화 설정(`conversation_id`, `previous_response_id`, 또는 `auto_previous_response_id`)과 동일 실행에서 함께 사용할 수 없습니다
    호출마다 한 가지 접근 방식만 선택하세요

### 대화/채팅 스레드

어떤 run 메서드를 호출해도 하나 이상의 에이전트 실행(즉, 하나 이상의 LLM 호출)이 발생할 수 있지만, 채팅 대화에서는 하나의 논리적 턴을 나타냅니다. 예를 들어

1. 사용자 턴: 사용자가 텍스트 입력
2. Runner 실행: 첫 번째 에이전트가 LLM 호출, 도구 실행, 두 번째 에이전트로 핸드오프, 두 번째 에이전트가 추가 도구 실행 후 출력 생성

에이전트 실행이 끝나면 사용자에게 무엇을 보여줄지 선택할 수 있습니다. 예를 들어 에이전트가 생성한 모든 새 항목을 보여주거나 최종 출력만 보여줄 수 있습니다. 이후 사용자가 후속 질문을 하면 run 메서드를 다시 호출할 수 있습니다

#### 수동 대화 관리

다음 턴 입력을 얻기 위해 [`RunResultBase.to_input_list()`][agents.result.RunResultBase.to_input_list] 메서드로 대화 기록을 수동 관리할 수 있습니다

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

#### Sessions를 통한 자동 대화 관리

더 간단한 접근으로, `.to_input_list()`를 수동 호출하지 않고 [Sessions](sessions/index.md)를 사용해 대화 기록을 자동 처리할 수 있습니다

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

-   각 실행 전에 대화 기록 조회
-   각 실행 후 새 메시지 저장
-   서로 다른 session ID에 대해 분리된 대화 유지

자세한 내용은 [Sessions 문서](sessions/index.md)를 참고하세요

#### 서버 관리 대화

`to_input_list()` 또는 `Sessions`로 로컬에서 처리하는 대신, OpenAI 대화 상태 기능으로 서버 측에서 대화 상태를 관리할 수도 있습니다. 이를 통해 과거 메시지를 모두 수동 재전송하지 않고도 대화 기록을 유지할 수 있습니다. 아래 서버 관리 방식에서는 요청마다 새 턴 입력만 전달하고 저장된 ID를 재사용하세요. 자세한 내용은 [OpenAI Conversation state guide](https://platform.openai.com/docs/guides/conversation-state?api-mode=responses)를 참고하세요

OpenAI는 턴 간 상태 추적에 두 가지 방식을 제공합니다

##### 1. `conversation_id` 사용

먼저 OpenAI Conversations API로 대화를 생성한 뒤, 이후 모든 호출에서 해당 ID를 재사용합니다

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

또 다른 옵션은 **response chaining**으로, 각 턴이 이전 턴의 응답 ID에 명시적으로 연결됩니다

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

실행이 승인 대기 중 일시 중지되었다가 [`RunState`][agents.run_state.RunState]에서 재개되면,
SDK는 저장된 `conversation_id` / `previous_response_id` / `auto_previous_response_id`
설정을 유지하므로 재개된 턴도 동일한 서버 관리 대화에서 계속 진행됩니다

`conversation_id`와 `previous_response_id`는 상호 배타적입니다. 시스템 간 공유 가능한 이름 있는 대화 리소스가 필요하면 `conversation_id`를 사용하세요. 턴 간 가장 가벼운 Responses API 연속성 기본 요소가 필요하면 `previous_response_id`를 사용하세요

!!! note

    SDK는 `conversation_locked` 오류를 백오프로 자동 재시도합니다. 서버 관리 대화 실행에서는
    재시도 전에 내부 conversation-tracker 입력을 되감아 동일한 준비 항목을
    깨끗하게 다시 전송할 수 있도록 합니다

    로컬 session 기반 실행(`conversation_id`,
    `previous_response_id`, 또는 `auto_previous_response_id`와 함께 사용할 수 없음)에서도
    SDK는 재시도 후 기록 중복 항목을 줄이기 위해 최근 영속화된 입력 항목을
    최선의 노력으로 롤백합니다

## 훅 및 사용자 지정

### call model input filter

모델 호출 직전에 모델 입력을 편집하려면 `call_model_input_filter`를 사용하세요. 이 훅은 현재 에이전트, 컨텍스트, 결합된 입력 항목(세션 기록이 있으면 포함)을 받아 새 `ModelInputData`를 반환합니다

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

민감 정보 마스킹, 긴 기록 축약, 추가 시스템 가이드 주입 등을 위해 실행별로 `run_config`에서 이 훅을 설정하세요

## 오류 및 복구

### 오류 핸들러

모든 `Runner` 진입점은 오류 종류를 키로 하는 dict 형태의 `error_handlers`를 받습니다. 현재 지원 키는 `"max_turns"`입니다. `MaxTurnsExceeded`를 발생시키는 대신 제어된 최종 출력을 반환하고 싶을 때 사용하세요

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

대체 출력을 대화 기록에 추가하고 싶지 않다면 `include_in_history=False`를 설정하세요

## Durable execution 통합 및 휴먼인더루프 (HITL)

도구 승인 일시 중지/재개 패턴은 전용 [Human-in-the-loop guide](human_in_the_loop.md)부터 시작하세요
아래 통합은 실행이 긴 대기, 재시도 또는 프로세스 재시작에 걸칠 수 있는 durable 오케스트레이션용입니다

### Temporal

Agents SDK [Temporal](https://temporal.io/) 통합을 사용하면 휴먼인더루프 (HITL) 작업을 포함한 durable 장기 실행 워크플로를 실행할 수 있습니다. 장기 실행 작업을 완료하는 Temporal과 Agents SDK의 동작 데모는 [이 영상](https://www.youtube.com/watch?v=fFBZqzT4DD8)에서 확인할 수 있으며, 문서는 [여기](https://github.com/temporalio/sdk-python/tree/main/temporalio/contrib/openai_agents)에서 볼 수 있습니다

### Restate

Agents SDK [Restate](https://restate.dev/) 통합을 사용하면 휴먼 승인, 핸드오프, session 관리를 포함한 경량 durable 에이전트를 실행할 수 있습니다. 이 통합은 Restate의 단일 바이너리 런타임을 의존성으로 필요로 하며, 프로세스/컨테이너 또는 서버리스 함수로 에이전트 실행을 지원합니다
자세한 내용은 [개요](https://www.restate.dev/blog/durable-orchestration-for-ai-agents-with-restate-and-openai-sdk) 또는 [문서](https://docs.restate.dev/ai)를 참고하세요

### DBOS

Agents SDK [DBOS](https://dbos.dev/) 통합을 사용하면 장애 및 재시작 전반에서 진행 상태를 보존하는 신뢰성 높은 에이전트를 실행할 수 있습니다. 장기 실행 에이전트, 휴먼인더루프 (HITL) 워크플로, 핸드오프를 지원합니다. 동기 및 비동기 메서드를 모두 지원합니다. 이 통합은 SQLite 또는 Postgres 데이터베이스만 필요합니다. 자세한 내용은 통합 [repo](https://github.com/dbos-inc/dbos-openai-agents) 및 [문서](https://docs.dbos.dev/integrations/openai-agents)를 참고하세요

## 예외

SDK는 특정 경우 예외를 발생시킵니다. 전체 목록은 [`agents.exceptions`][]에 있습니다. 요약은 다음과 같습니다

-   [`AgentsException`][agents.exceptions.AgentsException]: SDK 내에서 발생하는 모든 예외의 기본 클래스입니다. 다른 모든 구체적 예외가 파생되는 일반 타입 역할을 합니다
-   [`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded]: 에이전트 실행이 `Runner.run`, `Runner.run_sync`, `Runner.run_streamed` 메서드에 전달된 `max_turns` 한도를 초과하면 발생합니다. 지정된 상호작용 턴 수 내에 작업을 완료하지 못했음을 의미합니다
-   [`ModelBehaviorError`][agents.exceptions.ModelBehaviorError]: 기본 모델(LLM)이 예기치 않거나 유효하지 않은 출력을 생성할 때 발생합니다. 예시는 다음과 같습니다
    -   잘못된 JSON: 특히 특정 `output_type`이 정의된 경우, 도구 호출 또는 직접 출력에서 잘못된 JSON 구조를 제공하는 경우
    -   예상치 못한 도구 관련 실패: 모델이 예상된 방식으로 도구를 사용하지 못하는 경우
-   [`ToolTimeoutError`][agents.exceptions.ToolTimeoutError]: 함수 도구 호출이 설정된 타임아웃을 초과하고 도구가 `timeout_behavior="raise_exception"`을 사용할 때 발생합니다
-   [`UserError`][agents.exceptions.UserError]: 사용자가(SDK를 사용해 코드를 작성하는 사람) SDK 사용 중 오류를 범할 때 발생합니다. 일반적으로 잘못된 코드 구현, 유효하지 않은 구성, 또는 SDK API 오용에서 비롯됩니다
-   [`InputGuardrailTripwireTriggered`][agents.exceptions.InputGuardrailTripwireTriggered], [`OutputGuardrailTripwireTriggered`][agents.exceptions.OutputGuardrailTripwireTriggered]: 각각 입력 가드레일 또는 출력 가드레일 조건이 충족될 때 발생합니다. 입력 가드레일은 처리 전에 들어오는 메시지를 검사하고, 출력 가드레일은 전달 전에 에이전트의 최종 응답을 검사합니다