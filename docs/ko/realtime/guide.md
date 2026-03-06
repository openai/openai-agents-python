---
search:
  exclude: true
---
# 실시간 에이전트 가이드

이 가이드는 OpenAI Agents SDK 의 실시간 레이어가 OpenAI Realtime API 에 어떻게 매핑되는지와, 그 위에 Python SDK 가 추가하는 동작을 설명합니다

!!! warning "베타 기능"

    실시간 에이전트는 베타입니다. 구현을 개선하는 과정에서 일부 호환성이 깨지는 변경이 있을 수 있습니다.

!!! note "여기서 시작"

    기본 Python 경로를 원하시면 먼저 [quickstart](quickstart.md)를 읽어보세요. 앱에서 서버 측 WebSocket 또는 SIP 중 무엇을 사용할지 결정 중이라면 [Realtime transport](transport.md)를 읽어보세요. 브라우저 WebRTC 전송은 Python SDK 범위에 포함되지 않습니다.

## 개요

실시간 에이전트는 Realtime API 와의 장기 연결을 유지하여, 모델이 텍스트와 오디오를 점진적으로 처리하고, 오디오 출력을 스트리밍하고, 도구를 호출하고, 매 턴마다 새 요청을 다시 시작하지 않고 인터럽션(중단 처리)을 처리할 수 있게 합니다.

주요 SDK 구성 요소는 다음과 같습니다:

-   **RealtimeAgent**: 하나의 실시간 전문 에이전트에 대한 instructions, tools, 출력 가드레일, 핸드오프
-   **RealtimeRunner**: 시작 에이전트를 실시간 전송에 연결하는 세션 팩토리
-   **RealtimeSession**: 입력을 전송하고, 이벤트를 수신하고, 히스토리를 추적하고, 도구를 실행하는 라이브 세션
-   **RealtimeModel**: 전송 추상화입니다. 기본값은 OpenAI 의 서버 측 WebSocket 구현입니다.

## 세션 수명 주기

일반적인 실시간 세션은 다음과 같습니다:

1. 하나 이상의 `RealtimeAgent`를 생성합니다.
2. 시작 에이전트로 `RealtimeRunner`를 생성합니다.
3. `await runner.run()`을 호출하여 `RealtimeSession`을 가져옵니다.
4. `async with session:` 또는 `await session.enter()`로 세션에 진입합니다.
5. `send_message()` 또는 `send_audio()`로 사용자 입력을 전송합니다.
6. 대화가 끝날 때까지 세션 이벤트를 순회합니다.

텍스트 전용 실행과 달리 `runner.run()`은 즉시 최종 결과를 생성하지 않습니다. 대신 전송 레이어와 동기화된 로컬 히스토리, 백그라운드 도구 실행, 가드레일 상태, 활성 에이전트 구성을 유지하는 라이브 세션 객체를 반환합니다.

기본적으로 `RealtimeRunner`는 `OpenAIRealtimeWebSocketModel`을 사용하므로, 기본 Python 경로는 Realtime API 에 대한 서버 측 WebSocket 연결입니다. 다른 `RealtimeModel`을 전달해도 동일한 세션 수명 주기와 에이전트 기능이 적용되며, 연결 메커니즘만 달라질 수 있습니다.

## 에이전트 및 세션 구성

`RealtimeAgent`는 일반 `Agent` 타입보다 의도적으로 범위가 좁습니다:

-   모델 선택은 에이전트별이 아니라 세션 수준에서 구성됩니다.
-   structured outputs는 지원되지 않습니다.
-   음성은 구성할 수 있지만, 세션이 이미 음성 오디오를 생성한 이후에는 변경할 수 없습니다.
-   instructions, 함수 도구, 핸드오프, 훅, 출력 가드레일은 모두 계속 동작합니다.

`RealtimeSessionModelSettings`는 더 새로운 중첩형 `audio` 구성과 이전의 평면 별칭을 모두 지원합니다. 새 코드에서는 중첩형을 권장합니다:

```python
runner = RealtimeRunner(
    starting_agent=agent,
    config={
        "model_settings": {
            "model_name": "gpt-realtime",
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

`RealtimeRunner(config=...)`의 유용한 실행 수준 설정은 다음과 같습니다:

-   `async_tool_calls`
-   `output_guardrails`
-   `guardrails_settings.debounce_text_length`
-   `tool_error_formatter`
-   `tracing_disabled`

전체 타입 표면은 [`RealtimeRunConfig`][agents.realtime.config.RealtimeRunConfig] 및 [`RealtimeSessionModelSettings`][agents.realtime.config.RealtimeSessionModelSettings]를 참고하세요.

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

구조화된 메시지는 실시간 대화에 이미지 입력을 포함하는 주요 방법입니다. [`examples/realtime/app/server.py`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/app/server.py)의 예제 웹 데모는 이 방식으로 `input_image` 메시지를 전달합니다.

### 오디오 입력

원시 오디오 바이트를 스트리밍하려면 [`session.send_audio()`][agents.realtime.session.RealtimeSession.send_audio]를 사용하세요:

```python
await session.send_audio(audio_bytes)
```

서버 측 턴 감지가 비활성화된 경우, 턴 경계를 표시하는 책임은 사용자에게 있습니다. 고수준 편의 방식은 다음과 같습니다:

```python
await session.send_audio(audio_bytes, commit=True)
```

더 낮은 수준의 제어가 필요하면, 기본 모델 전송을 통해 `input_audio_buffer.commit` 같은 원시 클라이언트 이벤트를 보낼 수도 있습니다.

### 수동 응답 제어

`session.send_message()`는 고수준 경로를 사용해 사용자 입력을 전송하고 응답을 자동으로 시작합니다. 원시 오디오 버퍼링은 모든 구성에서 동일하게 자동 처리되지는 **않습니다**.

Realtime API 수준에서 수동 턴 제어는 원시 `session.update`로 `turn_detection`을 비운 다음, `input_audio_buffer.commit`과 `response.create`를 직접 보내는 것을 의미합니다.

턴을 수동으로 관리하는 경우, 모델 전송을 통해 원시 클라이언트 이벤트를 전송할 수 있습니다:

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

이 패턴은 다음과 같은 경우에 유용합니다:

-   `turn_detection`이 비활성화되어 있고 모델이 언제 응답할지 직접 결정하고 싶은 경우
-   응답 트리거 전에 사용자 입력을 검사하거나 제한하고 싶은 경우
-   대역 외 응답에 사용자 지정 프롬프트가 필요한 경우

[`examples/realtime/twilio_sip/server.py`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/twilio_sip/server.py)의 SIP 예제는 시작 인사를 강제로 보내기 위해 원시 `response.create`를 사용합니다.

## 이벤트, 히스토리 및 인터럽션(중단 처리)

`RealtimeSession`은 고수준 SDK 이벤트를 내보내면서, 필요할 때 원시 모델 이벤트도 계속 전달합니다.

가치가 높은 세션 이벤트는 다음과 같습니다:

-   `audio`, `audio_end`, `audio_interrupted`
-   `agent_start`, `agent_end`
-   `tool_start`, `tool_end`, `tool_approval_required`
-   `handoff`
-   `history_added`, `history_updated`
-   `guardrail_tripped`
-   `input_audio_timeout_triggered`
-   `error`
-   `raw_model_event`

UI 상태에 가장 유용한 이벤트는 보통 `history_added`와 `history_updated`입니다. 이 이벤트는 사용자 메시지, 어시스턴트 메시지, 도구 호출을 포함한 세션의 로컬 히스토리를 `RealtimeItem` 객체로 노출합니다.

### 인터럽션(중단 처리) 및 재생 추적

사용자가 어시스턴트를 중단하면 세션은 `audio_interrupted`를 내보내고, 사용자가 실제로 들은 내용과 서버 측 대화가 정렬되도록 히스토리를 업데이트합니다.

저지연 로컬 재생에서는 기본 재생 추적기만으로 충분한 경우가 많습니다. 원격 또는 지연 재생 시나리오, 특히 전화 통신에서는 [`RealtimePlaybackTracker`][agents.realtime.model.RealtimePlaybackTracker]를 사용하세요. 이렇게 하면 모든 생성 오디오가 이미 재생되었다고 가정하는 대신 실제 재생 진행 상황을 기준으로 인터럽션 절단이 수행됩니다.

[`examples/realtime/twilio/twilio_handler.py`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/twilio/twilio_handler.py)의 Twilio 예제가 이 패턴을 보여줍니다.

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

함수 도구는 실행 전에 사람의 승인을 요구할 수 있습니다. 이 경우 세션은 `tool_approval_required`를 내보내고, `approve_tool_call()` 또는 `reject_tool_call()`을 호출할 때까지 도구 실행을 일시 중지합니다.

```python
async for event in session:
    if event.type == "tool_approval_required":
        await session.approve_tool_call(event.call_id)
```

구체적인 서버 측 승인 루프는 [`examples/realtime/app/server.py`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/app/server.py)를 참고하세요. 휴먼인더루프 (HITL) 문서도 [Human in the loop](../human_in_the_loop.md)에서 이 흐름을 다시 안내합니다.

### 핸드오프

실시간 핸드오프를 사용하면 한 에이전트가 라이브 대화를 다른 전문 에이전트로 넘길 수 있습니다:

```python
from agents.realtime import RealtimeAgent, realtime_handoff

billing_agent = RealtimeAgent(
    name="Billing Support",
    instructions="You specialize in billing issues.",
)

main_agent = RealtimeAgent(
    name="Customer Service",
    instructions="Triage the request and hand off when needed.",
    handoffs=[realtime_handoff(billing_agent, tool_description="Transfer to billing support")],
)
```

기본 `RealtimeAgent` 핸드오프는 자동 래핑되며, `realtime_handoff(...)`를 사용하면 이름, 설명, 검증, 콜백, 가용성을 사용자 지정할 수 있습니다. 실시간 핸드오프는 일반 핸드오프의 `input_filter`를 지원하지 **않습니다**.

### 가드레일

실시간 에이전트에서는 출력 가드레일만 지원됩니다. 이 가드레일은 매 부분 토큰마다가 아니라 디바운스된 전사 누적 기준으로 실행되며, 예외를 발생시키는 대신 `guardrail_tripped`를 내보냅니다.

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

## SIP 및 전화 통신

Python SDK 는 [`OpenAIRealtimeSIPModel`][agents.realtime.openai_realtime.OpenAIRealtimeSIPModel]을 통한 일급 SIP 연결 플로우를 포함합니다.

Realtime Calls API 를 통해 통화가 들어오고, 결과 `call_id`에 에이전트 세션을 연결하려는 경우 이를 사용하세요:

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

먼저 통화를 수락해야 하고, 수락 페이로드를 에이전트 파생 세션 구성과 일치시키려면 `OpenAIRealtimeSIPModel.build_initial_session_payload(...)`를 사용하세요. 전체 플로우는 [`examples/realtime/twilio_sip/server.py`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/twilio_sip/server.py)에 나와 있습니다.

## 저수준 접근 및 사용자 지정 엔드포인트

`session.model`을 통해 기본 전송 객체에 접근할 수 있습니다.

다음이 필요한 경우 이 방법을 사용하세요:

-   `session.model.add_listener(...)`를 통한 사용자 지정 리스너
-   `response.create` 또는 `session.update` 같은 원시 클라이언트 이벤트
-   `model_config`를 통한 사용자 지정 `url`, `headers`, `api_key` 처리
-   기존 실시간 통화에 `call_id` 연결

`RealtimeModelConfig`는 다음을 지원합니다:

-   `api_key`
-   `url`
-   `headers`
-   `initial_model_settings`
-   `playback_tracker`
-   `call_id`

이 리포지토리에 포함된 `call_id` 예제는 SIP 입니다. 더 넓은 Realtime API 에서도 일부 서버 측 제어 플로우에 `call_id`를 사용하지만, 여기에는 Python 예제로 패키징되어 있지 않습니다.

Azure OpenAI 에 연결할 때는 GA Realtime 엔드포인트 URL 과 명시적 헤더를 전달하세요. 예:

```python
session = await runner.run(
    model_config={
        "url": "wss://<your-resource>.openai.azure.com/openai/v1/realtime?model=<deployment-name>",
        "headers": {"api-key": "<your-azure-api-key>"},
    }
)
```

토큰 기반 인증의 경우 `headers`에 bearer 토큰을 사용하세요:

```python
session = await runner.run(
    model_config={
        "url": "wss://<your-resource>.openai.azure.com/openai/v1/realtime?model=<deployment-name>",
        "headers": {"authorization": f"Bearer {token}"},
    }
)
```

`headers`를 전달하면 SDK 는 `Authorization`을 자동으로 추가하지 않습니다. 실시간 에이전트에서는 레거시 베타 경로(`/openai/realtime?api-version=...`)를 피하세요.

## 추가 읽을거리

-   [Realtime transport](transport.md)
-   [Quickstart](quickstart.md)
-   [OpenAI Realtime conversations](https://developers.openai.com/api/docs/guides/realtime-conversations/)
-   [OpenAI Realtime server-side controls](https://developers.openai.com/api/docs/guides/realtime-server-controls/)
-   [`examples/realtime`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime)