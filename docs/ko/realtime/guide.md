---
search:
  exclude: true
---
# 가이드

이 가이드는 OpenAI Agents SDK의 실시간 기능을 사용하여 음성 기반 AI 에이전트를 구축하는 방법을 자세히 설명합니다.

!!! warning "베타 기능"
Realtime agents는 베타 단계입니다. 구현을 개선하는 과정에서 호환성 문제가 발생할 수 있습니다.

## 개요

Realtime agents는 실시간으로 오디오와 텍스트 입력을 처리하고 실시간 오디오로 응답하는 대화형 흐름을 제공합니다. OpenAI의 Realtime API와 지속적인 연결을 유지하여 낮은 지연의 자연스러운 음성 대화와 원활한 인터럽션(중단 처리)을 지원합니다.

## 아키텍처

### 핵심 구성 요소

실시간 시스템은 다음과 같은 주요 구성 요소로 이루어져 있습니다:

-   **RealtimeAgent**: instructions, tools 및 핸드오프로 구성된 에이전트
-   **RealtimeRunner**: 구성을 관리합니다. `runner.run()`을 호출하여 세션을 얻을 수 있습니다.
-   **RealtimeSession**: 단일 상호작용 세션입니다. 일반적으로 사용자 대화가 시작될 때 하나를 만들고, 대화가 끝날 때까지 유지합니다.
-   **RealtimeModel**: 기본 모델 인터페이스(일반적으로 OpenAI의 WebSocket 구현)

### 세션 흐름

일반적인 실시간 세션의 흐름은 다음과 같습니다:

1. instructions, tools 및 핸드오프로 **RealtimeAgent를 생성**합니다
2. 에이전트와 구성 옵션으로 **RealtimeRunner를 설정**합니다
3. `await runner.run()`을 사용해 **세션을 시작**하고 RealtimeSession을 반환받습니다
4. `send_audio()` 또는 `send_message()`를 사용해 **오디오 또는 텍스트 메시지 전송**합니다
5. 세션을 반복(iterate)하면서 **이벤트를 수신**합니다 - 오디오 출력, 전사 결과, 도구 호출, 핸드오프, 오류 등이 포함됩니다
6. 사용자가 에이전트가 말하는 동안 말을 겹쳐 할 때 **인터럽션(중단 처리)** 을 처리합니다. 현재 오디오 생성이 자동으로 중단됩니다

세션은 대화 기록을 유지하고 실시간 모델과의 지속적인 연결을 관리합니다.

## 에이전트 구성

RealtimeAgent는 일반 Agent 클래스와 유사하지만 몇 가지 중요한 차이점이 있습니다. 전체 API 세부 정보는 [`RealtimeAgent`][agents.realtime.agent.RealtimeAgent] API 레퍼런스를 참조하세요.

일반 에이전트와의 주요 차이점:

-   모델 선택은 에이전트 수준이 아닌 세션 수준에서 구성합니다
-   structured output 지원이 없습니다 (`outputType` 미지원)
-   음성은 에이전트별로 설정할 수 있으나 첫 번째 에이전트가 말한 이후에는 변경할 수 없습니다
-   tools, 핸드오프, instructions 등의 다른 기능은 동일하게 동작합니다

## 세션 구성

### 모델 설정

세션 구성에서는 기본 실시간 모델 동작을 제어할 수 있습니다. 모델 이름(예: `gpt-4o-realtime-preview`), 음성 선택(alloy, echo, fable, onyx, nova, shimmer), 지원 모달리티(텍스트 및/또는 오디오)를 설정할 수 있습니다. 오디오 포맷은 입력과 출력 모두에 대해 설정할 수 있으며 기본값은 PCM16입니다.

### 오디오 구성

오디오 설정은 세션이 음성 입력과 출력을 처리하는 방식을 제어합니다. Whisper와 같은 모델을 사용한 입력 오디오 전사, 언어 기본값 설정, 도메인 특화 용어의 정확도를 높이기 위한 전사 프롬프트 제공이 가능합니다. 턴 감지 설정은 에이전트가 언제 응답을 시작하고 종료해야 하는지를 제어하며, 음성 활동 감지 임계값, 무음 지속 시간, 감지된 음성 주변 패딩 등의 옵션을 제공합니다.

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

핸드오프를 통해 전문화된 에이전트 간에 대화를 전환할 수 있습니다.

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

## 이벤트 처리

세션은 세션 객체를 반복(iterate)하여 수신할 수 있는 이벤트를 스트리밍합니다. 이벤트에는 오디오 출력 청크, 전사 결과, 도구 실행 시작 및 종료, 에이전트 핸드오프, 오류 등이 포함됩니다. 처리해야 할 주요 이벤트는 다음과 같습니다:

-   **audio**: 에이전트 응답의 원시 오디오 데이터
-   **audio_end**: 에이전트가 말하기를 완료함
-   **audio_interrupted**: 사용자가 에이전트를 중단함
-   **tool_start/tool_end**: 도구 실행 라이프사이클
-   **handoff**: 에이전트 핸드오프 발생
-   **error**: 처리 중 오류 발생

전체 이벤트 상세는 [`RealtimeSessionEvent`][agents.realtime.events.RealtimeSessionEvent]를 참조하세요.

## 가드레일

실시간 에이전트에서는 출력 가드레일만 지원합니다. 실시간 생성 중 성능 문제를 피하기 위해 가드레일은 디바운스되어 주기적으로 실행되며(모든 단어마다 실행되지 않음), 기본 디바운스 길이는 100자이며 구성 가능합니다.

가드레일은 `RealtimeAgent`에 직접 연결하거나 세션의 `run_config`를 통해 제공할 수 있습니다. 두 소스의 가드레일은 함께 실행됩니다.

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

가드레일이 트리거되면 `guardrail_tripped` 이벤트를 생성하고 에이전트의 현재 응답을 인터럽트할 수 있습니다. 디바운스 동작은 안전성과 실시간 성능 요구 사이의 균형을 맞추는 데 도움이 됩니다. 텍스트 에이전트와 달리, 실시간 에이전트는 가드레일이 작동해도 Exception을 발생시키지 않습니다.

## 오디오 처리

[`session.send_audio(audio_bytes)`][agents.realtime.session.RealtimeSession.send_audio]를 사용해 오디오를, [`session.send_message()`][agents.realtime.session.RealtimeSession.send_message]를 사용해 텍스트를 세션으로 전송합니다.

오디오 출력의 경우 `audio` 이벤트를 수신하여 선호하는 오디오 라이브러리로 재생하세요. 사용자가 에이전트를 중단할 때 즉시 재생을 중지하고 대기 중인 오디오를 모두 비우기 위해 `audio_interrupted` 이벤트를 반드시 수신하세요.

## 모델 직접 액세스

기저 모델에 접근하여 커스텀 리스너를 추가하거나 고급 작업을 수행할 수 있습니다:

```python
# Add a custom listener to the model
session.model.add_listener(my_custom_listener)
```

이는 연결에 대한 하위 수준 제어가 필요한 고급 사용 사례를 위해 [`RealtimeModel`][agents.realtime.model.RealtimeModel] 인터페이스에 직접 액세스할 수 있게 해줍니다.

## 코드 예제

완전한 동작 코드 예제는 [examples/realtime 디렉터리](https://github.com/openai/openai-agents-python/tree/main/examples/realtime)를 참고하세요. UI 구성 요소가 있는 데모와 없는 데모를 모두 포함합니다.