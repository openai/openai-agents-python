---
search:
  exclude: true
---
# 실시간 에이전트 가이드

이 가이드는 OpenAI Agents SDK의 실시간 기능을 사용해 음성 지원 AI 에이전트를 구축하는 방법을 심층적으로 다룹니다.

!!! warning "베타 기능"
실시간 에이전트는 베타입니다. 구현을 개선하는 과정에서 일부 호환성이 깨지는 변경이 발생할 수 있습니다.

## 개요

실시간 에이전트는 오디오와 텍스트 입력을 실시간으로 처리하고 실시간 오디오로 응답하는 대화 흐름을 지원합니다. OpenAI의 Realtime API와 지속적인 연결을 유지하여 지연이 낮은 자연스러운 음성 대화를 가능하게 하고, 인터럽션(중단 처리)도 우아하게 처리할 수 있습니다.

## 아키텍처

### 핵심 구성 요소

실시간 시스템은 다음과 같은 주요 구성 요소로 이루어집니다:

-   **RealtimeAgent**: instructions, tools, handoffs로 구성된 에이전트
-   **RealtimeRunner**: 구성을 관리합니다. `runner.run()`을 호출해 세션을 얻을 수 있습니다
-   **RealtimeSession**: 단일 상호작용 세션입니다. 일반적으로 사용자가 대화를 시작할 때마다 하나를 만들고, 대화가 끝날 때까지 유지합니다
-   **RealtimeModel**: 기본 모델 인터페이스(일반적으로 OpenAI의 WebSocket 구현)

### 세션 흐름

일반적인 실시간 세션은 다음 흐름을 따릅니다:

1. instructions, tools, handoffs로 **RealtimeAgent(들)를 생성**합니다
2. 에이전트와 구성 옵션으로 **RealtimeRunner를 설정**합니다
3. `await runner.run()`을 사용해 **세션을 시작**하면 RealtimeSession이 반환됩니다
4. `send_audio()` 또는 `send_message()`를 사용해 세션에 **오디오 또는 텍스트 메시지를 전송**합니다
5. 세션을 순회(iterating)하여 **이벤트를 수신**합니다. 이벤트에는 오디오 출력, 트랜스크립트, 도구 호출, 핸드오프, 오류가 포함됩니다
6. 사용자가 에이전트 위로 말할 때 **인터럽션(중단 처리)을 처리**하며, 이때 현재 오디오 생성이 자동으로 중지됩니다

세션은 대화 기록을 유지하고 실시간 모델과의 지속적인 연결을 관리합니다.

## 에이전트 구성

RealtimeAgent는 일반 Agent 클래스와 유사하게 동작하지만 몇 가지 핵심 차이가 있습니다. 전체 API 세부 사항은 [`RealtimeAgent`][agents.realtime.agent.RealtimeAgent] API 레퍼런스를 참고하세요.

일반 에이전트와의 주요 차이점:

-   모델 선택은 에이전트 레벨이 아니라 세션 레벨에서 구성합니다
-   structured outputs를 지원하지 않습니다(`outputType` 미지원)
-   음성은 에이전트별로 구성할 수 있지만, 첫 번째 에이전트가 말한 뒤에는 변경할 수 없습니다
-   tools, handoffs, instructions 같은 다른 기능은 동일하게 동작합니다

## 세션 구성

### 모델 설정

세션 구성으로 기본 실시간 모델 동작을 제어할 수 있습니다. 모델 이름(예: `gpt-realtime`), 음성 선택(alloy, echo, fable, onyx, nova, shimmer), 지원 모달리티(text 및/또는 audio)를 구성할 수 있습니다. 오디오 형식은 입력과 출력 모두에 설정할 수 있으며 기본값은 PCM16입니다.

### 오디오 구성

오디오 설정은 세션이 음성 입력과 출력을 처리하는 방식을 제어합니다. Whisper 같은 모델을 사용해 입력 오디오 트랜스크립션을 구성하고, 언어 선호를 설정하며, 도메인 특화 용어의 정확도를 높이기 위한 트랜스크립션 프롬프트를 제공할 수 있습니다. 턴 감지 설정은 에이전트가 언제 응답을 시작하고 중지해야 하는지를 제어하며, 음성 활동 감지 임계값, 무음 지속 시간, 감지된 음성 주변의 패딩 같은 옵션이 있습니다.

`RealtimeRunner(config=...)`에서 설정할 수 있는 추가 구성 옵션은 다음과 같습니다:

-   `model_settings.output_modalities`로 출력을 text 및/또는 audio로 제한
-   `model_settings.input_audio_noise_reduction`로 근거리/원거리 오디오에 대한 노이즈 감소를 조정
-   `guardrails_settings.debounce_text_length`로 출력 가드레일 실행 빈도를 제어
-   `async_tool_calls`로 함수 도구를 동시 실행
-   `tool_error_formatter`로 모델에 표시되는 도구 오류 메시지를 커스터마이즈

전체 타입 구성은 [`RealtimeRunConfig`][agents.realtime.config.RealtimeRunConfig] 및 [`RealtimeSessionModelSettings`][agents.realtime.config.RealtimeSessionModelSettings]를 참고하세요.

## 도구와 함수

### 도구 추가

일반 에이전트와 마찬가지로, 실시간 에이전트는 대화 중 실행되는 함수 도구를 지원합니다:

```python
from agents import function_tool

@function_tool
def get_weather(city: str) -> str:
    """Get current weather for a city."""
    # Your weather API logic here
    return f"The weather in {city} is sunny, 72°F"

@function_tool
def book_appointment(date: str, time: str, service: str) -> str:
    """Book an appointment."""
    # Your booking logic here
    return f"Appointment booked for {service} on {date} at {time}"

agent = RealtimeAgent(
    name="Assistant",
    instructions="You can help with weather and appointments.",
    tools=[get_weather, book_appointment],
)
```

## 핸드오프

### 핸드오프 생성

핸드오프를 사용하면 전문화된 에이전트 간에 대화를 전달할 수 있습니다.

```python
from agents.realtime import realtime_handoff

# Specialized agents
billing_agent = RealtimeAgent(
    name="Billing Support",
    instructions="You specialize in billing and payment issues.",
)

technical_agent = RealtimeAgent(
    name="Technical Support",
    instructions="You handle technical troubleshooting.",
)

# Main agent with handoffs
main_agent = RealtimeAgent(
    name="Customer Service",
    instructions="You are the main customer service agent. Hand off to specialists when needed.",
    handoffs=[
        realtime_handoff(billing_agent, tool_description="Transfer to billing support"),
        realtime_handoff(technical_agent, tool_description="Transfer to technical support"),
    ]
)
```

## 런타임 동작 및 세션 처리

### 이벤트 처리

세션은 세션 객체를 순회(iterating)하여 수신할 수 있는 이벤트를 스트리밍합니다. 이벤트에는 오디오 출력 청크, 트랜스크립션 결과, 도구 실행 시작/종료, 에이전트 핸드오프, 오류가 포함됩니다. 처리해야 할 주요 이벤트는 다음과 같습니다:

-   **audio**: 에이전트 응답의 원시 오디오 데이터
-   **audio_end**: 에이전트 발화 종료
-   **audio_interrupted**: 사용자가 에이전트를 인터럽션(중단 처리)함
-   **tool_start/tool_end**: 도구 실행 라이프사이클
-   **handoff**: 에이전트 핸드오프 발생
-   **error**: 처리 중 오류 발생

전체 이벤트 세부 사항은 [`RealtimeSessionEvent`][agents.realtime.events.RealtimeSessionEvent]를 참고하세요.

### 가드레일

실시간 에이전트에서는 출력 가드레일만 지원됩니다. 이러한 가드레일은 실시간 생성 중 성능 문제를 피하기 위해 디바운스 처리되어 주기적으로(매 단어마다가 아니라) 실행됩니다. 기본 디바운스 길이는 100자이지만 구성할 수 있습니다.

가드레일은 `RealtimeAgent`에 직접 부착하거나 세션의 `run_config`를 통해 제공할 수 있습니다. 두 소스의 가드레일은 함께 실행됩니다.

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

가드레일이 트리거되면 `guardrail_tripped` 이벤트가 생성되며 에이전트의 현재 응답을 인터럽션(중단 처리)할 수 있습니다. 디바운스 동작은 안전성과 실시간 성능 요구 사항 사이의 균형을 맞추는 데 도움이 됩니다. 텍스트 에이전트와 달리, 실시간 에이전트는 가드레일이 트리거되더라도 Exception을 발생시키지 **않습니다**.

### 오디오 처리

[`session.send_audio(audio_bytes)`][agents.realtime.session.RealtimeSession.send_audio]를 사용해 세션에 오디오를 전송하거나, [`session.send_message()`][agents.realtime.session.RealtimeSession.send_message]를 사용해 텍스트를 전송하세요.

오디오 출력의 경우 `audio` 이벤트를 수신하고 선호하는 오디오 라이브러리로 오디오 데이터를 재생하세요. 사용자가 에이전트를 인터럽션(중단 처리)할 때 즉시 재생을 멈추고 대기 중인 오디오를 비우기 위해 `audio_interrupted` 이벤트도 반드시 수신해야 합니다.

## 고급 통합 및 로우 레벨 접근

### SIP 통합

[Realtime Calls API](https://platform.openai.com/docs/guides/realtime-sip)로 들어오는 전화 통화에 실시간 에이전트를 연결할 수 있습니다. SDK는 SIP로 미디어를 협상하면서 동일한 에이전트 흐름을 재사용하는 [`OpenAIRealtimeSIPModel`][agents.realtime.openai_realtime.OpenAIRealtimeSIPModel]을 제공합니다.

사용하려면 모델 인스턴스를 runner에 전달하고, 세션 시작 시 SIP `call_id`를 제공하세요. call ID는 수신 전화를 알리는 웹훅에서 전달됩니다.

```python
from agents.realtime import RealtimeAgent, RealtimeRunner
from agents.realtime.openai_realtime import OpenAIRealtimeSIPModel

runner = RealtimeRunner(
    starting_agent=agent,
    model=OpenAIRealtimeSIPModel(),
)

async with await runner.run(
    model_config={
        "call_id": call_id_from_webhook,
        "initial_model_settings": {
            "turn_detection": {"type": "semantic_vad", "interrupt_response": True},
        },
    },
) as session:
    async for event in session:
        ...
```

발신자가 전화를 끊으면 SIP 세션이 종료되고 실시간 연결도 자동으로 닫힙니다. 전체 텔레포니 예제는 [`examples/realtime/twilio_sip`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/twilio_sip)를 참고하세요.

### 모델 직접 접근

기본 모델에 접근해 커스텀 리스너를 추가하거나 고급 작업을 수행할 수 있습니다:

```python
# Add a custom listener to the model
session.model.add_listener(my_custom_listener)
```

이를 통해 연결에 대한 더 로우 레벨 제어가 필요한 고급 사용 사례를 위해 [`RealtimeModel`][agents.realtime.model.RealtimeModel] 인터페이스에 직접 접근할 수 있습니다.

### 예제 및 추가 읽을거리

완전히 동작하는 예제는 UI 구성 요소 포함/미포함 데모가 있는 [examples/realtime directory](https://github.com/openai/openai-agents-python/tree/main/examples/realtime)를 확인하세요.

### Azure OpenAI 엔드포인트 형식

Azure OpenAI에 연결할 때는 GA Realtime 엔드포인트 형식을 사용하고 `model_config`의 헤더로 자격 증명을 전달하세요:

```python
model_config = {
    "url": "wss://<your-resource>.openai.azure.com/openai/v1/realtime?model=<deployment-name>",
    "headers": {"api-key": "<your-azure-api-key>"},
}
```

토큰 기반 인증의 경우 `headers`에 `{"authorization": f"Bearer {token}"}`를 사용하세요.