---
search:
  exclude: true
---
# 실시간 에이전트 가이드

이 가이드는 OpenAI Agents SDK의 실시간 계층이 OpenAI Realtime API에 어떻게 매핑되는지, 그리고 Python SDK가 그 위에 어떤 추가 동작을 더하는지 설명합니다.

!!! note "시작 안내"

    기본 Python 경로를 원한다면 먼저 [빠른 시작](quickstart.md)을 읽으세요. 앱이 서버 측 WebSocket 또는 SIP를 사용해야 하는지 결정 중이라면 [실시간 전송](transport.md)을 읽으세요. 브라우저 WebRTC 전송은 Python SDK의 일부가 아닙니다.

## 개요

실시간 에이전트는 Realtime API에 대한 장기 연결을 열어 두어 모델이 텍스트와 오디오를 점진적으로 처리하고, 오디오 출력을 스트리밍하고, 도구를 호출하며, 매 턴마다 새 요청을 다시 시작하지 않고 인터럽션(중단 처리)을 처리할 수 있게 합니다.

주요 SDK 구성 요소는 다음과 같습니다:

-   **RealtimeAgent**: 한 실시간 전문 에이전트를 위한 Instructions, tools, 출력 가드레일 및 핸드오프
-   **RealtimeRunner**: 시작 에이전트를 실시간 전송에 연결하는 세션 팩토리
-   **RealtimeSession**: 입력을 보내고, 이벤트를 수신하며, 히스토리를 추적하고, 도구를 실행하는 라이브 세션
-   **RealtimeModel**: 전송 추상화. 기본값은 OpenAI의 서버 측 WebSocket 구현입니다.

## 세션 수명 주기

일반적인 실시간 세션은 다음과 같습니다:

1. 하나 이상의 `RealtimeAgent`를 만듭니다.
2. 시작 에이전트로 `RealtimeRunner`를 만듭니다.
3. `await runner.run()`을 호출해 `RealtimeSession`을 가져옵니다.
4. `async with session:` 또는 `await session.enter()`로 세션에 진입합니다.
5. `send_message()` 또는 `send_audio()`로 사용자 입력을 보냅니다.
6. 대화가 끝날 때까지 세션 이벤트를 반복 처리합니다.

텍스트 전용 실행과 달리 `runner.run()`은 최종 결과를 즉시 생성하지 않습니다. 로컬 히스토리, 백그라운드 도구 실행, 가드레일 상태, 활성 에이전트 구성을 전송 계층과 동기화하여 유지하는 라이브 세션 객체를 반환합니다.

기본적으로 `RealtimeRunner`는 `OpenAIRealtimeWebSocketModel`을 사용하므로, 기본 Python 경로는 Realtime API에 대한 서버 측 WebSocket 연결입니다. 다른 `RealtimeModel`을 전달해도 동일한 세션 수명 주기와 에이전트 기능이 적용되며, 연결 방식은 달라질 수 있습니다.

## 에이전트 및 세션 구성

`RealtimeAgent`는 일반 `Agent` 타입보다 의도적으로 범위가 더 좁습니다:

-   모델 선택은 에이전트별이 아니라 세션 수준에서 구성됩니다.
-   Structured outputs는 지원되지 않습니다.
-   음성은 구성할 수 있지만, 세션이 이미 음성 오디오를 생성한 후에는 변경할 수 없습니다.
-   Instructions, 함수 도구, 핸드오프, 훅, 출력 가드레일은 모두 그대로 작동합니다.

`RealtimeSessionModelSettings`는 더 새로운 중첩 `audio` 구성과 기존 플랫(flat) 별칭을 모두 지원합니다. 새 코드에는 중첩 형태를 권장하며, 새로운 실시간 에이전트에는 `gpt-realtime-2`로 시작하세요:

```python
runner = RealtimeRunner(
    starting_agent=agent,
    config={
        "model_settings": {
            "model_name": "gpt-realtime-2",
            "audio": {
                "input": {
                    "format": "pcm16",
                    "transcription": {"model": "gpt-4o-mini-transcribe"},
                    "turn_detection": {"type": "semantic_vad", "interrupt_response": True},
                },
                "output": {"format": "pcm16", "voice": "ash"},
            },
            "tool_choice": "auto",
        }
    },
)
```

유용한 세션 수준 설정은 다음과 같습니다:

-   `audio.input.format`, `audio.output.format`
-   `audio.input.transcription`
-   `audio.input.noise_reduction`
-   `audio.input.turn_detection`
-   `audio.output.voice`, `audio.output.speed`
-   `output_modalities`
-   `tool_choice`
-   `prompt`
-   `tracing`

`RealtimeRunner(config=...)`에서의 유용한 실행 수준 설정은 다음과 같습니다:

-   `async_tool_calls`
-   `output_guardrails`
-   `guardrails_settings.debounce_text_length`
-   `tool_error_formatter`
-   `tracing_disabled`

타입이 지정된 전체 인터페이스는 [`RealtimeRunConfig`][agents.realtime.config.RealtimeRunConfig] 및 [`RealtimeSessionModelSettings`][agents.realtime.config.RealtimeSessionModelSettings]를 참조하세요.

## 입력 및 출력

### 텍스트 및 구조화된 사용자 메시지

일반 텍스트 또는 구조화된 실시간 메시지에는 [`session.send_message()`][agents.realtime.session.RealtimeSession.send_message]를 사용하세요.

```python
from agents.realtime import RealtimeUserInputMessage

await session.send_message("Summarize what we discussed so far.")

message: RealtimeUserInputMessage = {
    "type": "message",
    "role": "user",
    "content": [
        {"type": "input_text", "text": "Describe this image."},
        {"type": "input_image", "image_url": image_data_url, "detail": "high"},
    ],
}
await session.send_message(message)
```

구조화된 메시지는 실시간 대화에 이미지 입력을 포함하는 주된 방법입니다. [`examples/realtime/app/server.py`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/app/server.py)의 예제 웹 데모는 이런 방식으로 `input_image` 메시지를 전달합니다.

### 오디오 입력

원문 오디오 바이트를 스트리밍하려면 [`session.send_audio()`][agents.realtime.session.RealtimeSession.send_audio]를 사용하세요:

```python
await session.send_audio(audio_bytes)
```

서버 측 턴 감지가 비활성화된 경우 턴 경계를 표시하는 것은 사용자 책임입니다. 상위 수준 편의 기능은 다음과 같습니다:

```python
await session.send_audio(audio_bytes, commit=True)
```

더 낮은 수준의 제어가 필요하면 기반 모델 전송을 통해 `input_audio_buffer.commit` 같은 원문 클라이언트 이벤트를 보낼 수도 있습니다.

### 수동 응답 제어

`session.send_message()`는 상위 수준 경로를 사용해 사용자 입력을 보내고, 응답을 자동으로 시작합니다. 원문 오디오 버퍼링은 모든 구성에서 같은 작업을 **자동으로** 수행하지는 않습니다.

Realtime API 수준에서 수동 턴 제어란 원문 `session.update`로 `turn_detection`을 지운 다음, `input_audio_buffer.commit`과 `response.create`를 직접 보내는 것을 의미합니다.

턴을 수동으로 관리하는 경우 모델 전송을 통해 원문 클라이언트 이벤트를 보낼 수 있습니다:

```python
from agents.realtime.model_inputs import RealtimeModelSendRawMessage

await session.model.send_event(
    RealtimeModelSendRawMessage(
        message={
            "type": "response.create",
        }
    )
)
```

이 패턴은 다음과 같은 경우 유용합니다:

-   `turn_detection`이 비활성화되어 있고 모델이 언제 응답해야 할지 직접 결정하려는 경우
-   응답을 트리거하기 전에 사용자 입력을 검사하거나 통과 여부를 제어하려는 경우
-   대역 외 응답을 위한 커스텀 프롬프트가 필요한 경우

[`examples/realtime/twilio_sip/server.py`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/twilio_sip/server.py)의 SIP 예제는 시작 인사말을 강제하기 위해 원문 `response.create`를 사용합니다.

## 이벤트, 히스토리 및 인터럽션(중단 처리)

`RealtimeSession`은 상위 수준의 SDK 이벤트를 내보내면서, 필요할 때 원문 모델 이벤트도 계속 전달합니다.

중요한 세션 이벤트는 다음과 같습니다:

-   `audio`, `audio_end`, `audio_interrupted`
-   `agent_start`, `agent_end`
-   `tool_start`, `tool_end`, `tool_approval_required`
-   `handoff`
-   `history_added`, `history_updated`
-   `guardrail_tripped`
-   `input_audio_timeout_triggered`
-   `error`
-   `raw_model_event`

UI 상태에 가장 유용한 이벤트는 보통 `history_added`와 `history_updated`입니다. 이 이벤트들은 사용자 메시지, 어시스턴트 메시지, 도구 호출을 포함해 세션의 로컬 히스토리를 `RealtimeItem` 객체로 노출합니다.

### 인터럽션(중단 처리) 및 재생 추적

사용자가 어시스턴트에 대해 인터럽션(중단 처리)을 발생시키면 세션은 `audio_interrupted`를 내보내고 히스토리를 업데이트하여 서버 측 대화가 사용자가 실제로 들은 내용과 계속 정렬되도록 합니다.

지연 시간이 낮은 로컬 재생에서는 기본 재생 추적기로 충분한 경우가 많습니다. 원격 또는 지연 재생 시나리오, 특히 전화 환경에서는 생성된 모든 오디오를 이미 들었다고 가정하는 대신 실제 재생 진행 상황을 기준으로 인터럽션(중단 처리)에 따른 잘라내기가 이루어지도록 [`RealtimePlaybackTracker`][agents.realtime.model.RealtimePlaybackTracker]를 사용하세요.

[`examples/realtime/twilio/twilio_handler.py`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/twilio/twilio_handler.py)의 Twilio 예제는 이 패턴을 보여 줍니다.

## 도구, 승인, 핸드오프 및 가드레일

### 함수 도구

실시간 에이전트는 라이브 대화 중 함수 도구를 지원합니다:

```python
from agents import function_tool


@function_tool
def get_weather(city: str) -> str:
    """Get current weather for a city."""
    return f"The weather in {city} is sunny, 72F."


agent = RealtimeAgent(
    name="Assistant",
    instructions="You can answer weather questions.",
    tools=[get_weather],
)
```

### 도구 승인

함수 도구는 실행 전에 사람의 승인이 필요하도록 설정할 수 있습니다. 이 경우 세션은 `tool_approval_required`를 내보내고, `approve_tool_call()` 또는 `reject_tool_call()`을 호출할 때까지 도구 실행을 일시 중지합니다.

도구에 입력 가드레일도 있는 경우, 승인 후 실행 직전에 해당 가드레일이 실행됩니다. 승인 이벤트가 내보내지기 전에 이를 실행하려면 `RealtimeRunner(..., config={"tool_execution": {"pre_approval_tool_input_guardrails": True}})`로 러너를 만드세요. 이 사전 승인 검사를 통과한 호출도 승인 후 실행 전에 다시 검사됩니다.

```python
async for event in session:
    if event.type == "tool_approval_required":
        await session.approve_tool_call(event.call_id)
```

구체적인 서버 측 승인 루프는 [`examples/realtime/app/server.py`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/app/server.py)를 참조하세요. 휴먼인더루프 (HITL) 문서도 [휴먼인더루프 (HITL)](../human_in_the_loop.md)에서 이 흐름을 다시 안내합니다.

### 핸드오프

실시간 핸드오프를 사용하면 한 에이전트가 라이브 대화를 다른 전문 에이전트에게 넘길 수 있습니다:

```python
from agents.realtime import RealtimeAgent, realtime_handoff

billing_agent = RealtimeAgent(
    name="Billing Support",
    instructions="You specialize in billing issues.",
)

main_agent = RealtimeAgent(
    name="Customer Service",
    instructions="Triage the request and hand off when needed.",
    handoffs=[
        realtime_handoff(
            billing_agent,
            tool_description_override="Transfer to billing support",
        )
    ],
)
```

그대로 전달된 `RealtimeAgent` 핸드오프는 자동으로 래핑되며, `realtime_handoff(...)`를 사용하면 이름, 설명, 검증, 콜백 및 사용 가능 여부를 커스터마이즈할 수 있습니다. 실시간 핸드오프는 일반 핸드오프의 `input_filter`를 지원하지 **않습니다**.

### 가드레일

실시간 에이전트는 에이전트 응답에 대한 출력 가드레일과 함수 도구 호출에 대한 입력 가드레일을 지원합니다. 출력 가드레일은 모든 부분 토큰마다 실행되는 대신 디바운스된 전사 누적분에 대해 실행되며, 예외를 발생시키지 않고 `guardrail_tripped`를 내보냅니다.

```python
from agents.guardrail import GuardrailFunctionOutput, OutputGuardrail


def sensitive_data_check(context, agent, output):
    return GuardrailFunctionOutput(
        tripwire_triggered="password" in output,
        output_info=None,
    )


agent = RealtimeAgent(
    name="Assistant",
    instructions="...",
    output_guardrails=[OutputGuardrail(guardrail_function=sensitive_data_check)],
)
```

실시간 출력 가드레일이 트리거되면 세션은 활성 응답에 인터럽션(중단 처리)을 걸고,
`response.cancel`을 강제하며, `guardrail_tripped`를 내보내고, 트리거된 가드레일의 이름을 명시하는
후속 사용자 메시지를 보내 모델이 대체 응답을 생성할 수 있게 합니다. 그래도 오디오 플레이어는
`audio_interrupted`를 수신하고 로컬 재생을 즉시 중지해야 합니다. 가드레일은
디바운스된 전사 텍스트에서 실행되며 트립와이어가 발동될 때 일부 오디오가 이미 버퍼링되어 있을 수 있기 때문입니다.

## SIP 및 전화

Python SDK에는 [`OpenAIRealtimeSIPModel`][agents.realtime.openai_realtime.OpenAIRealtimeSIPModel]를 통한 정식 지원 SIP attach 흐름이 포함되어 있습니다.

Realtime Calls API를 통해 통화가 들어왔고, 그 결과로 생성된 `call_id`에 에이전트 세션을 연결하려는 경우 사용하세요:

```python
from agents.realtime import RealtimeRunner
from agents.realtime.openai_realtime import OpenAIRealtimeSIPModel

runner = RealtimeRunner(starting_agent=agent, model=OpenAIRealtimeSIPModel())

async with await runner.run(
    model_config={
        "call_id": call_id_from_webhook,
    }
) as session:
    async for event in session:
        ...
```

먼저 통화를 수락해야 하고 수락 페이로드가 에이전트에서 파생된 세션 구성과 일치하기를 원한다면 `OpenAIRealtimeSIPModel.build_initial_session_payload(...)`를 사용하세요. 전체 흐름은 [`examples/realtime/twilio_sip/server.py`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/twilio_sip/server.py)에 나와 있습니다.

## 저수준 접근 및 커스텀 엔드포인트

`session.model`을 통해 기반 전송 객체에 접근할 수 있습니다.

다음이 필요할 때 사용하세요:

-   `session.model.add_listener(...)`를 통한 커스텀 리스너
-   `response.create` 또는 `session.update` 같은 원문 클라이언트 이벤트
-   `model_config`를 통한 커스텀 `url`, `headers` 또는 `api_key` 처리
-   기존 실시간 통화에 `call_id` 연결

`RealtimeModelConfig`는 다음을 지원합니다:

-   `api_key`
-   `url`
-   `headers`
-   `initial_model_settings`
-   `playback_tracker`
-   `call_id`

이 저장소와 함께 제공되는 `call_id` 예제는 SIP입니다. 전반적인 Realtime API에서도 일부 서버 측 제어 흐름에 `call_id`를 사용하지만, 여기에서는 Python 예제로 패키징되어 있지 않습니다.

Azure OpenAI에 연결할 때는 GA Realtime 엔드포인트 URL과 명시적인 헤더를 전달하세요. 예:

```python
session = await runner.run(
    model_config={
        "url": "wss://<your-resource>.openai.azure.com/openai/v1/realtime?model=<deployment-name>",
        "headers": {"api-key": "<your-azure-api-key>"},
    }
)
```

토큰 기반 인증의 경우 `headers`에 Bearer 토큰을 사용하세요:

```python
session = await runner.run(
    model_config={
        "url": "wss://<your-resource>.openai.azure.com/openai/v1/realtime?model=<deployment-name>",
        "headers": {"authorization": f"Bearer {token}"},
    }
)
```

`headers`를 전달하면 SDK는 `Authorization`을 자동으로 추가하지 않습니다. 실시간 에이전트에서는 레거시 베타 경로(`/openai/realtime?api-version=...`)를 피하세요.

## 추가 자료

-   [실시간 전송](transport.md)
-   [빠른 시작](quickstart.md)
-   [OpenAI Realtime 대화](https://developers.openai.com/api/docs/guides/realtime-conversations/)
-   [OpenAI Realtime 서버 측 제어](https://developers.openai.com/api/docs/guides/realtime-server-controls/)
-   [`examples/realtime`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime)