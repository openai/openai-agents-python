---
search:
  exclude: true
---
# 실시간 전송

이 페이지를 사용해 실시간 에이전트가 Python 애플리케이션에 어떻게 맞는지 결정하세요

!!! note "Python SDK 경계"

    Python SDK에는 브라우저 WebRTC 전송이 **포함되지 않습니다**. 이 페이지는 Python SDK 전송 선택지만 다룹니다: 서버 측 WebSocket 및 SIP 연결 플로우. 브라우저 WebRTC는 별도의 플랫폼 주제이며, 공식 [WebRTC와 함께하는 Realtime API](https://developers.openai.com/api/docs/guides/realtime-webrtc/) 가이드에 문서화되어 있습니다.

## 결정 가이드

| 목표 | 시작점 | 이유 |
| --- | --- | --- |
| 서버에서 관리하는 실시간 앱 구축 | [빠른 시작](quickstart.md) | 기본 Python 경로는 `RealtimeRunner`가 관리하는 서버 측 WebSocket 세션입니다. |
| 어떤 전송 및 배포 형태를 선택할지 이해 | 이 페이지 | 전송 또는 배포 형태를 확정하기 전에 이 페이지를 사용하세요. |
| 전화 또는 SIP 통화에 에이전트 연결 | [실시간 가이드](guide.md) 및 [`examples/realtime/twilio_sip`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/twilio_sip) | 이 저장소는 `call_id`로 구동되는 SIP 연결 플로우를 제공합니다. |

## 서버 측 WebSocket 기본 Python 경로

`RealtimeRunner`는 사용자 정의 `RealtimeModel`을 전달하지 않는 한 `OpenAIRealtimeWebSocketModel`을 사용합니다.

즉, 표준 Python 토폴로지는 다음과 같습니다:

1. Python 서비스가 `RealtimeRunner`를 생성합니다.
2. `await runner.run()`이 `RealtimeSession`을 반환합니다.
3. 세션에 진입하고 텍스트, structured outputs 메시지 또는 오디오를 전송합니다.
4. `RealtimeSessionEvent` 항목을 소비하고 오디오 또는 전사본을 애플리케이션으로 전달합니다.

이 토폴로지는 핵심 데모 앱, CLI 예제, Twilio Media Streams 예제에서 사용됩니다:

-   [`examples/realtime/app`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/app)
-   [`examples/realtime/cli`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/cli)
-   [`examples/realtime/twilio`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/twilio)

서버가 오디오 파이프라인, 도구 실행, 승인 플로우, 히스토리 처리를 소유하는 경우 이 경로를 사용하세요.

## SIP 연결 전화 통신 경로

이 저장소에 문서화된 전화 통신 플로우에서는 Python SDK가 `call_id`를 통해 기존 실시간 통화에 연결됩니다.

이 토폴로지는 다음과 같습니다:

1. OpenAI가 `realtime.call.incoming` 같은 webhook을 서비스로 보냅니다.
2. 서비스가 Realtime Calls API를 통해 통화를 수락합니다.
3. Python 서비스가 `RealtimeRunner(..., model=OpenAIRealtimeSIPModel())`를 시작합니다.
4. 세션이 `model_config={"call_id": ...}`로 연결된 뒤, 다른 실시간 세션과 동일하게 이벤트를 처리합니다.

이 토폴로지는 [`examples/realtime/twilio_sip`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/twilio_sip)에 나와 있습니다.

더 넓은 Realtime API도 일부 서버 측 제어 패턴에 `call_id`를 사용하지만, 이 저장소에서 제공되는 연결 예제는 SIP입니다.

## 이 SDK 범위 외 브라우저 WebRTC

앱의 기본 클라이언트가 Realtime WebRTC를 사용하는 브라우저인 경우:

-   이 저장소의 Python SDK 문서 범위 밖으로 간주하세요
-   클라이언트 측 플로우와 이벤트 모델은 공식 [WebRTC와 함께하는 Realtime API](https://developers.openai.com/api/docs/guides/realtime-webrtc/) 및 [Realtime conversations](https://developers.openai.com/api/docs/guides/realtime-conversations/) 문서를 사용하세요
-   브라우저 WebRTC 클라이언트 위에 사이드밴드 서버 연결이 필요하면 공식 [Realtime server-side controls](https://developers.openai.com/api/docs/guides/realtime-server-controls/) 가이드를 사용하세요
-   이 저장소에서 브라우저 측 `RTCPeerConnection` 추상화나 즉시 사용 가능한 브라우저 WebRTC 샘플을 제공한다고 기대하지 마세요

또한 이 저장소는 현재 브라우저 WebRTC와 Python 사이드밴드를 함께 사용하는 예제를 제공하지 않습니다.

## 사용자 정의 엔드포인트 및 연결 지점

[`RealtimeModelConfig`][agents.realtime.model.RealtimeModelConfig]의 전송 구성 표면을 통해 기본 경로를 조정할 수 있습니다:

-   `url`: WebSocket 엔드포인트 재정의
-   `headers`: Azure 인증 헤더 같은 명시적 헤더 제공
-   `api_key`: API 키를 직접 또는 콜백을 통해 전달
-   `call_id`: 기존 실시간 통화에 연결. 이 저장소에서 문서화된 예제는 SIP입니다
-   `playback_tracker`: 인터럽션(중단 처리)을 위해 실제 재생 진행 상황 보고

토폴로지를 선택한 후 자세한 수명 주기 및 기능 표면은 [실시간 에이전트 가이드](guide.md)를 참조하세요.