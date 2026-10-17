---
search:
  exclude: true
---
# 테스트

SDK는 에이전트 워크플로, 샌드박스 세션, Realtime 세션, Voice 파이프라인을 위한 결정론적이고 공급자 중립적인 테스트 유틸리티를 제공합니다. 이러한 유틸리티는 메모리에서 실행되고 모델, 샌드박스 공급자 또는 Realtime API에 요청하지 않으며, SDK가 담당하는 정규화된 상호작용을 기록합니다. 아래의 실행 가능한 레시피는 OpenAI API 키가 구성된 경우 기본 트레이스 프로세서가 테스트 활동을 업로드하지 않도록 각 실행에서 트레이싱을 비활성화합니다.

애플리케이션과 SDK가 담당하는 오케스트레이션, 즉 도구 실행, 핸드오프, 가드레일, 재시도, 스트리밍, 세션 동작, 샌드박스 기능, Realtime 이벤트 처리, Voice 파이프라인 구성을 테스트하는 데 사용합니다. 외부 모델, 네트워크 프로토콜, 샌드박스 공급자 또는 오디오 시스템이 담당하는 동작에는 실제 공급자 어댑터나 통합 환경을 사용합니다.

## 필요한 레시피 찾기 {#find-the-recipe-you-need}

| 원하는 작업 | 사용 항목 | 이동 위치 |
| --- | --- | --- |
| 고정된 최종 답변 반환 | `ScriptedModel` 및 `assistant_message()` | [고정 응답 반환](#return-a-fixed-response) |
| 여러 턴의 도구 루프 실행 | `function_call()` 다음에 어시스턴트 응답 | [도구 워크플로 테스트](#test-a-tool-workflow) |
| 요청에서 응답 선택 | `ModelStep.respond()` 또는 `responder` 매핑 | [요청에서 응답 도출](#derive-a-response-from-the-request) |
| 실행기가 모델에 보낸 내용 검증 | `calls`, `first_call` 또는 `last_call` | [모델 호출 검사](#inspect-model-calls) |
| 스트리밍 실행 테스트 | 일반 응답 단계 또는 정확한 이벤트를 위한 `ModelStep.stream()` | [스트리밍 테스트](#test-streaming) |
| 오류 또는 재시도 결정 테스트 | `ModelStep.raise_error()` | [모델 실패 주입](#inject-model-failures) |
| 의도치 않은 워크플로 변경 감지 | 정확한 FIFO 단계 및 `assert_complete()` | [워크플로 드리프트 감지](#detect-workflow-drift) |
| 샌드박스를 시작하지 않고 `SandboxAgent` 테스트 | `scripted_sandbox_session()` 및 `ScriptedModel` | [샌드박스 에이전트 워크플로 테스트](#test-a-sandbox-agent-workflow) |
| 샌드박스 호출 매칭 또는 결과 도출 | 샌드박스 단계의 `match` 또는 `responder` | [샌드박스 단계 구성](#configure-sandbox-steps) |
| 연결을 열지 않고 Realtime 세션 테스트 | `ScriptedRealtimeModel` 및 `RealtimeStep` | [Realtime 세션 테스트](#test-a-realtime-session) |
| Realtime 도구 워크플로 테스트 | `RealtimeModelToolCallEvent`을 내보내고 도구 출력 예상 | [Realtime 도구 워크플로 테스트](#test-a-realtime-tool-workflow) |
| 정적 또는 스트리밍 Voice 파이프라인 테스트 | `ScriptedSTTModel`, `ScriptedTTSModel` 및 스크립트된 워크플로나 실제 워크플로 | [Voice 파이프라인 테스트](#test-a-voice-pipeline) |
| 공급자 직렬화 또는 전송 페이로드 테스트 | 제어된 네트워크 전송을 사용하는 실제 공급자 어댑터 | [올바른 경계 선택](#choose-the-correct-boundary) |

## 임포트 {#imports}

테스트 API는 대체 대상인 런타임 경계와 같은 위치에 있습니다.

| 경계 | 임포트 경로 |
| --- | --- |
| 에이전트 모델 및 샌드박스 워크플로 | `agents.testing` |
| Realtime 모델 전송 | `agents.realtime.testing` |
| Voice STT, TTS 및 워크플로 구성 요소 | `agents.voice.testing` |

테스트 심볼은 의도적으로 최상위 `agents` 임포트에서 제외됩니다.

## 에이전트 워크플로 레시피 {#agent-workflow-recipes}

### 고정 응답 반환 {#return-a-fixed-response}

예상되는 각 모델 호출마다 정규화된 출력 항목 시퀀스 하나를 전달합니다. 출력 시퀀스 축약형에는 한 요청에 대해 결정론적인 응답 ID와 사용량이 지정됩니다.

```python
import pytest

from agents import Agent, RunConfig, Runner
from agents.testing import ScriptedModel, assistant_message


@pytest.mark.asyncio
async def test_fixed_response() -> None:
    model = ScriptedModel([[assistant_message("Paris is the capital of France.")]])
    agent = Agent(name="Geography assistant", model=model)

    result = await Runner.run(
        agent,
        "What is the capital of France?",
        run_config=RunConfig(tracing_disabled=True),
    )

    assert result.final_output == "Paris is the capital of France."
    assert len(model.calls) == 1
    model.assert_complete()
```

결정론적 워크플로 테스트는 `model.assert_complete()`로 마무리합니다. 워크플로가 구성된 모든 단계를 소비하기 전에 중지된 경우를 감지합니다.

### 도구 워크플로 테스트 {#test-a-tool-workflow}

도구를 호출하는 모델 응답 하나와 최종 답변을 생성하는 두 번째 응답을 스크립트로 작성합니다. 실제 SDK 도구 파이프라인은 이러한 모델 호출 사이에서 실행됩니다.

```python
import pytest

from agents import Agent, RunConfig, Runner
from agents.decorators import tool
from agents.testing import ScriptedModel, assistant_message, function_call


@tool
def get_weather(city: str) -> str:
    """Return the weather for a city."""
    return f"{city}: sunny"


@pytest.mark.asyncio
async def test_tool_workflow() -> None:
    model = ScriptedModel(
        [
            [function_call("get_weather", {"city": "Tokyo"}, call_id="call_1")],
            [assistant_message("It is sunny in Tokyo.")],
        ]
    )
    agent = Agent(name="Weather assistant", model=model, tools=[get_weather])

    result = await Runner.run(
        agent,
        "What is the weather in Tokyo?",
        run_config=RunConfig(tracing_disabled=True),
    )

    assert result.final_output == "It is sunny in Tokyo."
    assert len(model.calls) == 2
    assert model.last_call is not None
    assert any(item.get("type") == "function_call_output" for item in model.last_call.input)
    model.assert_complete()
```

이 패턴은 도구 입력 검증, 실행, 결과 변환, 훅, 가드레일 및 다음 모델 턴을 다룹니다. Python 함수를 직접 호출하면 이러한 SDK 동작을 우회하게 됩니다.

### 요청에서 응답 도출 {#derive-a-response-from-the-request}

응답이 정규화된 모델 호출에 실제로 의존하거나 검증이 모델 경계에 속하는 경우 `ModelStep.respond()`을 사용합니다. 응답기는 동기식 또는 비동기식일 수 있으며 `ScriptedModel`이 허용하는 모든 단계 형태를 반환할 수 있습니다.

```python
import pytest

from agents import Agent, RunConfig, Runner
from agents.testing import ModelCall, ModelStep, ScriptedModel, assistant_message


def respond(call: ModelCall):
    assert call.streamed is False
    assert call.input == [{"content": "Summarize this", "role": "user"}]
    return {"output": [assistant_message("Handled the normalized request.")]}


@pytest.mark.asyncio
async def test_request_aware_response() -> None:
    model = ScriptedModel([ModelStep.respond(respond)])
    agent = Agent(name="Assistant", model=model)

    result = await Runner.run(
        agent,
        "Summarize this",
        run_config=RunConfig(tracing_disabled=True),
    )

    assert result.final_output == "Handled the normalized request."
    model.assert_complete()
```

`ScriptedModel`은 `ModelStep`, 이에 상응하는 딕셔너리 형식, `ModelResponse`, 정규화된 출력 항목 시퀀스 또는 예외를 허용합니다. 응답이 호출에 의존하지 않는 경우에는 고정 출력 시퀀스를 사용하는 것이 좋습니다. 고정 스크립트를 사용하면 예상치 못한 턴을 더 쉽게 진단할 수 있습니다.

### 모델 호출 검사 {#inspect-model-calls}

`ScriptedModel`은 선택된 단계를 처리하거나 예외를 발생시키기 전에 각 호출을 기록합니다.

| 멤버 | 포함 내용 |
| --- | --- |
| `calls` | 호출 순서에 따른 모든 `ModelCall` |
| `first_call` | 첫 번째 호출 또는 `None` |
| `last_call` | 가장 최근 호출 또는 `None` |
| `remaining_steps` | 아직 소비되지 않은 구성된 단계 수 |

일반적인 검증 대상에는 `call.input`, `call.model_settings`, `call.tools`, `call.handoffs`, `call.streamed`이 포함됩니다. 변경 가능한 요청 데이터는 호출 경계에서 스냅샷으로 저장되며, 각 공개 이력 접근자는 분리된 스냅샷을 반환합니다. 도구, 핸드오프, 출력 스키마 및 트레이싱 객체는 런타임 ID를 유지합니다.

구조화된 `call_index` 및 `input_index` 오류 필드는 0부터 시작하므로 `calls[...]` 또는 제공된 단계 시퀀스를 직접 인덱싱합니다. 사람이 읽을 수 있는 오류 메시지에는 1부터 시작하는 호출 또는 단계 번호가 표시됩니다.

한 테스트에서 모델 단계를 점진적으로 추가해야 하는 경우 `enqueue()` 또는 `extend()`을 사용합니다. 독립적인 시나리오에는 새 `ScriptedModel`를 생성합니다. 이 유틸리티는 소비된 단계나 호출 이력을 초기화하지 않습니다.

### 스트리밍 테스트 {#test-streaming}

일반 응답 단계는 `Runner.run()`과 `Runner.run_streamed()`을 모두 지원합니다. 일반적인 어시스턴트 메시지, 추론 항목, 함수 호출 및 패치 적용 호출의 경우 `ScriptedModel`는 정규화된 시작, 델타, 항목 완료 및 최종 응답 이벤트를 생성합니다. 최종 응답에는 전체 출력과 사용량이 포함됩니다.

정확한 정규화 `TResponseStreamEvent` 시퀀스가 테스트 대상 동작의 일부인 경우에만 `ModelStep.stream()`을 사용합니다.

```python
step = ModelStep.stream(
    events,
    output=[assistant_message("The terminal output used by the runner.")],
)
```

`events`는 고정 시퀀스이거나 기록된 `ModelCall`을 받는 비동기 팩토리일 수 있습니다. 선택적 `output`은 동일한 단계가 비스트리밍 호출에서 사용될 때 반환되는 응답입니다. 정확한 스트림 이벤트는 SDK에서 정규화된 이벤트이며 Responses API 또는 Chat Completions의 전송 청크가 아닙니다.

자동 스트리밍은 증분 수명 주기가 구현되지 않은 정규화된 출력 항목 종류를 거부합니다. 이러한 항목에는 부분적인 이벤트 시퀀스에 의존하지 말고 `ModelStep.stream(...)`을 사용합니다.

### 모델 실패 주입 {#inject-model-failures}

모델 호출 하나를 실패시키려면 `ModelStep.raise_error()`를 사용합니다. 선택적 재시도 지침은 해당 스크립트 오류에만 적용됩니다.

```python
from agents import ModelRetryAdvice
from agents.testing import ModelStep


step = ModelStep.raise_error(
    RuntimeError("temporary failure"),
    retry_advice=ModelRetryAdvice(suggested=True, replay_safety="safe"),
)
```

실행기의 재시도 정책은 지침에 따라 다시 시도할지를 결정합니다. 각 재시도는 또 다른 모델 호출이며 다음 스크립트 단계를 소비합니다. Python 헬퍼는 고정된 `ModelRetryAdvice` 값을 허용합니다. 재시도 지침 자체가 시도마다 동적으로 달라져야 하는 경우 사용자 정의 `Model`을 사용합니다.

### 워크플로 드리프트 감지 {#detect-workflow-drift}

스크립트된 호출을 예상 워크플로 형태로 취급합니다. 추가 모델 요청이 발생하면 `UnexpectedModelCall`가 발생하며, 조기에 종료되면 `assert_complete()`이 보고할 단계가 남습니다.

테스트 프레임워크가 정리 또는 종료 처리기를 지원하는 경우, 다른 검증이 실패한 뒤에도 소비되지 않은 단계를 보고하려면 `assert_complete()`를 해당 위치에 배치합니다. 일반 회귀 테스트에서는 불일치 오류를 포착하지 않습니다.

| 오류 | 구조화된 필드 | 의미 |
| --- | --- | --- |
| `InvalidModelStep` | `reason`, `input_index` | 단계의 형식이 잘못되어 큐에 들어가기 전에 거부됨 |
| `UnexpectedModelCall` | `call`, `call_index` | 스크립트가 끝난 후 워크플로가 추가 모델 호출을 수행함 |
| `UnconsumedModelSteps` | `remaining_steps` | 모든 단계를 사용하기 전에 워크플로가 종료됨 |

## 샌드박스 에이전트 레시피 {#sandbox-agent-recipes}

### 샌드박스 에이전트 워크플로 테스트 {#test-a-sandbox-agent-workflow}

로컬 컨테이너나 원격 샌드박스를 생성하지 않고 실제 `SandboxAgent` 런타임을 실행하려면 `ScriptedModel`과 `scripted_sandbox_session()`를 결합합니다. 모델 스크립트는 기능 도구를 선택하고, 샌드박스 스크립트는 해당 `SandboxSession` 메서드가 반환하는 값을 정의합니다.

```python
import pytest

from agents import RunConfig, Runner
from agents.sandbox import ExecResult, SandboxAgent
from agents.sandbox.capabilities import Shell
from agents.testing import (
    ScriptedModel,
    assistant_message,
    function_call,
    scripted_sandbox_session,
)


@pytest.mark.asyncio
async def test_sandbox_workflow() -> None:
    sandbox = scripted_sandbox_session(
        [
            {
                "method": "exec",
                "match": lambda call: call.args == ("pwd",),
                "result": ExecResult(
                    stdout=b"/workspace\n",
                    stderr=b"",
                    exit_code=0,
                ),
            }
        ]
    )
    model = ScriptedModel(
        [
            [function_call("exec_command", {"cmd": "pwd"}, call_id="call_1")],
            [assistant_message("The workspace is /workspace.")],
        ]
    )
    agent = SandboxAgent(
        name="Workspace assistant",
        model=model,
        capabilities=[Shell()],
    )

    async with sandbox:
        result = await Runner.run(
            agent,
            "Which directory are you in?",
            run_config=RunConfig(
                sandbox={"session": sandbox},
                tracing_disabled=True,
            ),
        )

    assert result.final_output == "The workspace is /workspace."
    assert [call.method for call in sandbox.calls] == ["exec"]
    sandbox.assert_complete()
    model.assert_complete()
```

이 테스트는 두 개의 정규화된 SDK 경계를 통과합니다. 도구 인수 검증, 기능 라우팅, 샌드박스 세션 호출, 다음 모델 턴으로의 도구 결과 전달 및 최종 출력 처리를 다룹니다. 실제 모델이 명령을 선택하는지 또는 실제 샌드박스 공급자가 명령을 어떻게 실행하는지는 테스트하지 않습니다.

### 샌드박스 단계 구성 {#configure-sandbox-steps}

일치하는 각 샌드박스 호출은 하나의 전역 FIFO 시퀀스에서 다음 단계를 소비합니다. 메서드 불일치, 매처 거부 또는 매처 예외가 발생하면 해당 단계는 보류 상태로 남습니다. `method`을 설정하고 결과를 정확히 하나 선택하며, 호출 세부 정보가 중요한 경우에만 `match`을 추가합니다.

| 단계 멤버 | 사용 시점 |
| --- | --- |
| `result` | 메서드가 고정된 타입 값을 반환해야 하는 경우 |
| `responder` | 결과가 분리된 `SandboxCall`에 의존하는 경우 |
| `error` | 메서드가 특정 예외를 발생시켜야 하는 경우 |
| `match` | 매처가 `False` 이외의 값을 반환하지 않는 한 결과를 생성하기 전에 호출을 거부해야 하는 경우 |

지원되는 스크립트 메서드 이름은 `apply_patch`, `exec`, `ls`, `mkdir`, `pty_exec_start`, `pty_write_stdin`, `read`, `rm`, `write`입니다. 구성된 모델 대상 기능만 노출됩니다. 두 PTY 메서드는 하나의 대화형 셸 기능을 구성하므로 둘 중 하나라도 구성되면 함께 노출되지만, 호출은 계속해서 전역 FIFO 스크립트를 소비합니다.

`sandbox.calls`에는 0부터 시작하는 `call_index`, `method`, 위치 인수 `args`, 읽기 전용 `kwargs`이 포함된 분리된 `SandboxCall` 스냅샷이 들어 있습니다. 정적 결과도 스크립트 생성 시 스냅샷으로 저장됩니다. `io.BytesIO` 및 `io.StringIO` 값이 지원됩니다. 그 밖의 라이브 스트림 객체나 수명 주기 동작에는 사용자 정의 샌드박스 세션을 사용합니다.

| 오류 | 구조화된 필드 | 의미 |
| --- | --- | --- |
| `InvalidSandboxStep` | `reason`, `input_index`, `method` | 단계의 형식이 잘못되었거나 지원되지 않는 메서드 이름이 지정됨 |
| `UnexpectedSandboxCall` | `call`, `call_index`, `actual_method`, `expected_method`, `remaining_steps` | 워크플로가 잘못된 메서드를 호출했거나 스크립트가 끝난 후에도 계속됨 |
| `SandboxCallMatcherError` | `call`, `call_index`, `method` | 단계 매처가 `False`을 반환함 |
| `UnconsumedSandboxSteps` | `remaining_steps`, `pending_methods` | 모든 단계를 사용하기 전에 워크플로가 종료됨 |

반환되는 객체가 세션 자체입니다. 이를 `RunConfig(sandbox={"session": sandbox})`에 직접 전달합니다. 래퍼 `.session` 속성은 없습니다.

## Realtime 레시피 {#realtime-recipes}

### Realtime 세션 테스트 {#test-a-realtime-session}

`ScriptedRealtimeModel`는 Python SDK의 정규화된 `RealtimeModel` 경계를 구현합니다. 각 `RealtimeStep`는 하나의 발신 `RealtimeModelSendEvent`와 일치한 다음, 정규화된 수신 `RealtimeModelEvent` 객체를 내보내거나 주입된 오류를 발생시킵니다.

```python
import pytest

from agents.realtime import (
    RealtimeAgent,
    RealtimeModelOutputTextDeltaEvent,
    RealtimeModelSendUserInput,
    RealtimeRawModelEvent,
    RealtimeRunner,
)
from agents.realtime.testing import RealtimeStep, ScriptedRealtimeModel


@pytest.mark.asyncio
async def test_realtime_message() -> None:
    reply = RealtimeModelOutputTextDeltaEvent(
        item_id="item_1",
        delta="Hello!",
        response_id="response_1",
    )
    model = ScriptedRealtimeModel(
        [
            RealtimeStep(
                expect=RealtimeModelSendUserInput(user_input="Hello"),
                emit=[reply],
            )
        ]
    )
    runner = RealtimeRunner(
        RealtimeAgent(name="Assistant"),
        model=model,
        config={"tracing_disabled": True},
    )

    observed_reply = False
    async with await runner.run() as session:
        await session.send_message("Hello")
        async for event in session:
            if isinstance(event, RealtimeRawModelEvent) and event.data == reply:
                observed_reply = True
                break

    assert observed_reply
    assert model.sent_events == (RealtimeModelSendUserInput(user_input="Hello"),)
    assert model.closed is True
    model.assert_complete()
```

기대값은 정확한 이벤트 값, `isinstance`로 일치 여부를 확인하는 이벤트 클래스 또는 발신 이벤트를 받아 일치할 때 `True`을 반환하는 콜러블일 수 있습니다. 엄격 모드는 기본적으로 활성화됩니다. `strict=False`를 사용하면 관련 없는 발신 이벤트가 기록되지만 보류 중인 단계를 소비하지 않습니다. 이는 세션이 테스트 대상 동작과 무관한 부수적 이벤트를 내보낼 때 유용합니다.

연결 중 수신 이벤트를 내보내려면 `connect_events`을 사용합니다. 수명 주기 실패에는 `connect_error` 또는 `close_error`를 사용하고, 일치한 하나의 전송에 연결된 실패에는 `RealtimeStep(error=...)`을 사용합니다. 한 단계에 `emit`와 `error`를 모두 정의할 수는 없습니다.

### Realtime 도구 워크플로 테스트 {#test-a-realtime-tool-workflow}

실제 함수 도구를 `RealtimeAgent`에 연결하고 정규화된 도구 호출을 내보낸 다음, SDK가 모델 경계를 통해 도구 출력을 전송하는지 확인합니다. `async_tool_calls`을 `False`로 설정하면 테스트 전용 대기 장치 없이 연결 중에 이 작은 예제를 완료할 수 있습니다.

```python
import pytest

from agents.decorators import tool
from agents.realtime import (
    RealtimeAgent,
    RealtimeModelSendToolOutput,
    RealtimeModelToolCallEvent,
    RealtimeRunner,
)
from agents.realtime.testing import RealtimeStep, ScriptedRealtimeModel


@tool
def lookup_order(order_id: str) -> str:
    """Look up an order by ID."""
    return f"Order {order_id} has shipped."


@pytest.mark.asyncio
async def test_realtime_tool_workflow() -> None:
    tool_call = RealtimeModelToolCallEvent(
        name="lookup_order",
        call_id="call_1",
        arguments='{"order_id":"order_123"}',
    )

    def matches_tool_output(event) -> bool:
        return (
            isinstance(event, RealtimeModelSendToolOutput)
            and event.tool_call.call_id == "call_1"
            and event.output == "Order order_123 has shipped."
        )

    model = ScriptedRealtimeModel(
        [RealtimeStep(expect=matches_tool_output)],
        connect_events=[tool_call],
    )
    agent = RealtimeAgent(
        name="Order assistant",
        tools=[lookup_order],
    )
    runner = RealtimeRunner(
        agent,
        model=model,
        config={"async_tool_calls": False, "tracing_disabled": True},
    )

    async with await runner.run():
        pass

    model.assert_complete()
```

이는 실제 Realtime 도구 조회, 인수 검증, 실행 및 출력 라우팅을 수행합니다. 실제 모델이 해당 도구를 선택한다는 것은 입증하지 않습니다.

### Realtime 호출 및 수명 주기 검사 {#inspect-realtime-calls-and-lifecycle}

| 멤버 | 포함 내용 |
| --- | --- |
| `connect_calls` | 자격 증명이 없고 분리된 연결 스냅샷 |
| `sent_events` | 호출 순서에 따른 분리된 발신 이벤트 스냅샷 |
| `remaining_steps` | 아직 남아 있는 예상 발신 전송 |
| `listeners` | 현재 등록된 리스너 객체 |
| `connected`, `closed`, `close_calls` | 현재 메모리 내 수명 주기 상태 |

연결 이력에는 API 키 또는 헤더 필드가 제공되었는지만 기록되며 해당 값은 저장되지 않습니다. URL 스냅샷에서는 사용자 정보, 쿼리 매개변수 및 프래그먼트가 제거됩니다. 변경 가능한 이벤트 데이터와 설정은 분리되지만, 도구, 핸드오프, 재생 추적기와 같은 라이브 SDK 객체는 ID를 유지합니다.

`model.assert_complete()`으로 마무리하고 `RealtimeSession` 비동기 컨텍스트 관리자가 모델을 닫도록 합니다. Python 유틸리티는 의도적으로 보류 중인 기대값 프로미스, 암시적 타임아웃 또는 별도의 `assert_closed()` 헬퍼를 제공하지 않습니다.

| 오류 | 구조화된 필드 | 의미 |
| --- | --- | --- |
| `UnexpectedRealtimeSend` | `actual`, `expected` | 엄격 모드의 발신 전송이 다음 단계와 일치하지 않거나 남은 단계가 없음 |
| `UnconsumedRealtimeSteps` | `remaining_steps` | 예상된 모든 전송을 사용하기 전에 세션이 종료됨 |
| `RealtimeScriptError` | 없음 | 연결이 끊긴 상태에서 전송하는 경우처럼 유효하지 않은 수명 주기 상태에서 스크립트가 사용됨 |

## Voice 파이프라인 레시피 {#voice-pipeline-recipes}

### Voice 파이프라인 테스트 {#test-a-voice-pipeline}

스크립트된 STT 및 TTS 모델을 `SingleAgentVoiceWorkflow`, 그리고 `ScriptedModel`을 기반으로 하는 에이전트와 결합하여 공급자 요청 없이 전체 음성-텍스트 변환 -> 에이전트 -> 텍스트-음성 변환 파이프라인을 테스트합니다.

```python
import numpy as np
import pytest

from agents import Agent
from agents.testing import ScriptedModel, assistant_message
from agents.voice import AudioInput, SingleAgentVoiceWorkflow, VoicePipeline
from agents.voice.testing import (
    ScriptedSTTModel,
    ScriptedTTSModel,
    TTSResult,
    pcm16_samples,
)


@pytest.mark.asyncio
async def test_voice_pipeline() -> None:
    model = ScriptedModel([[assistant_message("Hello there.")]])
    stt = ScriptedSTTModel("hello")
    pcm = pcm16_samples([0, 100, -100, 0])
    tts = ScriptedTTSModel([TTSResult([pcm])])
    pipeline = VoicePipeline(
        workflow=SingleAgentVoiceWorkflow(Agent(name="Voice assistant", model=model)),
        stt_model=stt,
        tts_model=tts,
        config={"tracing_disabled": True, "tts_settings": {"buffer_size": 1}},
    )

    result = await pipeline.run(AudioInput(np.zeros(2, dtype=np.int16)))
    events = [event async for event in result.stream()]

    assert events
    assert [call.text for call in tts.calls] == ["Hello there."]
    stt.assert_complete()
    tts.assert_complete()
    model.assert_complete()
```

파이프라인의 STT/TTS 수명 주기는 테스트 대상이지만 에이전트 오케스트레이션은 테스트 대상이 아닌 경우 `ScriptedVoiceWorkflow`을 대신 사용합니다.

```python
from agents.voice.testing import ScriptedVoiceWorkflow


workflow = ScriptedVoiceWorkflow(
    turns=["Hello there."],
    start="Welcome.",
)
```

`start` 단계는 `on_start()`에서 소비됩니다. `VoicePipeline`은 `StreamedAudioInput`에 대해서만 `on_start()`을 호출합니다. 정적 `AudioInput` 실행은 `start`를 소비하지 않습니다. 각 일반 턴은 전사 내용을 기록하고 구성된 결과 하나를 소비합니다. 문자열 하나는 프래그먼트 하나이며, 문자열 시퀀스는 텍스트 분할 및 TTS 이전의 프래그먼트 경계를 제어합니다.

### 스트리밍 전사 테스트 {#test-streamed-transcription}

`ScriptedSTTModel`는 정적 `transcriptions`과 독립적으로 스크립트된 스트리밍 `sessions`을 허용합니다. 세션은 `ScriptedTranscriptionSession`, 전사 턴 시퀀스, 예외 또는 단일 문자열일 수 있습니다.

```python
from agents.voice.testing import ScriptedSTTModel, ScriptedTranscriptionSession


session = ScriptedTranscriptionSession(["first turn", "second turn"])
stt = ScriptedSTTModel(sessions=[session])
```

`ScriptedTranscriptionSession`을 닫으면 반복이 중지되고 건너뛴 턴은 `assert_complete()`이 보고할 수 있도록 남습니다. 마찬가지로 `ScriptedTTSModel`은 호출할 때마다 `TTSResult`, 바이트 청크 시퀀스 또는 예외 하나를 소비합니다.

### Voice 호출 검사 {#inspect-voice-calls}

| 구성 요소 | 기록된 이력 |
| --- | --- |
| `ScriptedSTTModel` | `calls`, `session_calls` 및 라이브 `created_sessions` ID |
| `ScriptedTTSModel` | 텍스트와 분리된 설정을 포함하는 `calls` |
| `ScriptedVoiceWorkflow` | 턴 순서에 따른 `transcriptions` |

정적 오디오 버퍼와 변경 가능한 설정은 호출 시점에 스냅샷으로 저장됩니다. 파이프라인에서 계속 사용하므로 `StreamedAudioInput` 및 생성된 전사 세션 객체는 라이브 ID를 유지합니다.

| 오류 | 구조화된 필드 | 의미 |
| --- | --- | --- |
| `UnexpectedVoiceCall` | `operation` | 정적 전사, 스트리밍 세션, TTS 호출, 워크플로 시작 또는 워크플로 턴에 구성된 단계가 없음 |
| `UnconsumedVoiceSteps` | `remaining_steps` | 구성된 Voice 단계가 하나 이상 남아 있음 |

테스트에서 구성하는 모든 스크립트된 Voice 구성 요소에 `assert_complete()`을 호출합니다. `ScriptedSTTModel.assert_complete()`은 자신이 생성한 전사 세션의 턴도 확인합니다.

## 올바른 경계 선택 {#choose-the-correct-boundary}

모델 공급자에 의존하지 않고 SDK 실행 루프, 도구, 핸드오프, 가드레일, 세션, 재시도 또는 정규화된 스트리밍을 테스트해야 하는 경우 `ScriptedModel`을 사용합니다.

샌드박스 공급자를 시작하지 않고 `SandboxAgent` 기능과 오케스트레이션을 테스트해야 하는 경우 `ScriptedModel`과 함께 `scripted_sandbox_session()`을 사용합니다. 공급자 생성, 프로세스 실행, 파일 시스템 충실도, 영속성, 리소스 제한 및 격리 검사는 실제 샌드박스 공급자를 대상으로 하는 통합 테스트에서 수행합니다.

WebSocket 연결을 열지 않고 `RealtimeSession` 동작 또는 `RealtimeAgent` 도구와 핸드오프 오케스트레이션을 테스트해야 하는 경우 `ScriptedRealtimeModel`를 사용합니다. raw Realtime 클라이언트/서버 이벤트, 인증, 네트워크 복구 및 오디오 전송 동작은 실제 전송 계층 또는 통합 환경에서 테스트합니다. Realtime API 세션은 클라이언트가 입력을 전송하고 이벤트를 수신하는 동안 연결을 열린 상태로 유지하므로, 이러한 네트워크 및 프로토콜 관련 사항은 정규화된 모델 경계 아래에 속합니다. 프로덕션 연결 아키텍처는 [OpenAI Realtime API 가이드](https://developers.openai.com/api/docs/guides/realtime)를 참조하세요.

음성 공급자 없이 STT/TTS 순서, 스트리밍 전사 정리, 워크플로 프래그먼트 전달 또는 전체 Voice 파이프라인 구성을 테스트해야 하는 경우 Voice 테스트 구성 요소를 사용합니다. 전사 품질, 생성된 음성, 인코딩 호환성, 지연 시간 또는 재생이 테스트 대상이라면 실제 오디오 모델과 대표성 있는 오디오를 사용합니다.

Responses API 또는 Chat Completions 요청 직렬화, 인증 헤더, 공급자 기본값, HTTP 페이로드, 공급자 스트림 청크, Realtime 전송 프레임 또는 공급자별 수명 주기 동작을 테스트하는 데 이러한 유틸리티를 사용하지 않습니다. 이러한 테스트에는 실제 어댑터를 유지하고 해당 네트워크 경계를 대체하거나 제어합니다. `openai` v3에서는 OpenAI 어댑터 테스트에 `httpx2` 요청, 응답, 전송 및 예외 타입을 사용해야 합니다. 레거시 `httpx`은 Agents SDK의 핵심 종속성이 아닙니다.

## 최종 체크리스트 {#final-checklist}

- 정규화된 모델, 샌드박스 세션, Realtime 모델 또는 Voice 파이프라인 경계가 담당하는 상호작용만 스크립트로 작성합니다.
- 실행기의 비공개 상태 대신 중요한 공개 요청 또는 호출 필드를 검증합니다.
- 고정 응답 단계를 우선 사용하고, 요청에 의존하는 동작에만 응답기를 사용합니다.
- 자동 모델 스트리밍을 우선 사용하고, 이벤트 수준의 동작이 중요할 때만 정확한 스트림을 사용합니다.
- 각 스크립트 구성 요소 테스트를 해당 `assert_complete()` 메서드로 마무리합니다.
- 주변 테스트가 Realtime 및 샌드박스 수명 주기를 담당하는 경우 수명 주기 정리에 비동기 컨텍스트 관리자를 사용합니다.
- 사람이 읽을 수 있는 메시지를 파싱하는 대신 구조화된 오류 필드를 검증합니다.
- 공급자 전송 테스트에는 제어된 네트워크 전송을 사용하는 실제 어댑터를 사용합니다.

## 범위와 현재 제한 사항 {#scope-and-current-limitations}

테스트 모듈은 의도적으로 다음을 제공하지 않습니다.

- 모든 정규화된 모델 출력 항목을 위한 편의 빌더. 일반적인 경우에는 `assistant_message()` 및 `function_call()`을 사용하고, 그 밖의 정규화된 항목은 직접 전달합니다.
- 공급자 프로토콜 시뮬레이터. 정확한 모델 스트림은 Responses API 또는 Chat Completions 전송 청크가 아니라 정규화된 SDK 이벤트를 사용합니다.
- 고수준의 시뮬레이션된 Realtime 서버. 테스트는 정규화된 발신 전송을 명시적으로 매칭하고 시나리오에 필요한 정규화된 수신 이벤트를 내보냅니다.
- 순서가 지정되지 않은 샌드박스 또는 Realtime 기대값. 두 유틸리티 모두 하나의 전역 순서에 따라 예상 단계를 소비합니다.
- 테스트 실행기별 매처, 픽스처, 암시적 타임아웃 또는 자동 정리.
- 초기화 API. `ScriptedModel`은 점진적 스크립트를 위한 `enqueue()` 및 `extend()`을 지원하지만, 독립적인 시나리오에는 새 스크립트 구성 요소를 생성합니다.

테스트에 잘못된 형식의 스트림, 제어된 일시 중단 또는 동시성, 정확한 취소, 혹은 스크립트 유틸리티가 보존할 수 없는 수명 주기 경계가 필요한 경우 해당 공개 인터페이스의 사용자 정의 구현을 사용합니다. 테스트에 해당 특수 경계를 문서화합니다.

## API 레퍼런스 {#api-reference}

- [`agents.testing`](ref/testing.md)
- [`agents.realtime.testing`](ref/realtime/testing.md)
- [`agents.voice.testing`](ref/voice/testing.md)