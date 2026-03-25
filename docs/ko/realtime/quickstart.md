---
search:
  exclude: true
---
# 빠른 시작

Python SDK 의 실시간 에이전트는 WebSocket 전송을 통해 OpenAI Realtime API 위에서 구축된 서버 측 저지연 에이전트입니다

!!! warning "베타 기능"

    실시간 에이전트는 베타입니다. 구현을 개선하는 과정에서 일부 호환성이 깨지는 변경이 있을 수 있습니다.

!!! note "Python SDK 범위"

    Python SDK 는 브라우저 WebRTC 전송을 제공하지 **않습니다**. 이 페이지는 서버 측 WebSocket 을 통한 Python 관리 실시간 세션만 다룹니다. 이 SDK 는 서버 측 오케스트레이션, 도구, 승인, 전화 연동에 사용하세요. [실시간 전송](transport.md)도 참고하세요.

## 사전 요구 사항

-   Python 3.10 이상
-   OpenAI API 키
-   OpenAI Agents SDK 에 대한 기본적인 이해

## 설치

아직 설치하지 않았다면 OpenAI Agents SDK 를 설치하세요:

```bash
pip install openai-agents
```

## 서버 측 실시간 세션 생성

### 1. 실시간 구성 요소 가져오기

```python
import asyncio

from agents.realtime import RealtimeAgent, RealtimeRunner
```

### 2. 시작 에이전트 정의

```python
agent = RealtimeAgent(
    name="Assistant",
    instructions="You are a helpful voice assistant. Keep responses short and conversational.",
)
```

### 3. runner 구성

새 코드에서는 중첩된 `audio.input` / `audio.output` 세션 설정 형태를 권장합니다. 새 실시간 에이전트는 `gpt-realtime-1.5`로 시작하세요.

```python
runner = RealtimeRunner(
    starting_agent=agent,
    config={
        "model_settings": {
            "model_name": "gpt-realtime-1.5",
            "audio": {
                "input": {
                    "format": "pcm16",
                    "transcription": {"model": "gpt-4o-mini-transcribe"},
                    "turn_detection": {
                        "type": "semantic_vad",
                        "interrupt_response": True,
                    },
                },
                "output": {
                    "format": "pcm16",
                    "voice": "ash",
                },
            },
        }
    },
)
```

### 4. 세션 시작 및 입력 전송

`runner.run()`은 `RealtimeSession`을 반환합니다. 세션 컨텍스트에 들어가면 연결이 열립니다.

```python
async def main() -> None:
    session = await runner.run()

    async with session:
        await session.send_message("Say hello in one short sentence.")

        async for event in session:
            if event.type == "audio":
                # Forward or play event.audio.data.
                pass
            elif event.type == "history_added":
                print(event.item)
            elif event.type == "agent_end":
                # One assistant turn finished.
                break
            elif event.type == "error":
                print(f"Error: {event.error}")


if __name__ == "__main__":
    asyncio.run(main())
```

`session.send_message()`는 일반 문자열 또는 구조화된 실시간 메시지를 받습니다. 원문 오디오 청크에는 [`session.send_audio()`][agents.realtime.session.RealtimeSession.send_audio]를 사용하세요.

## 이 빠른 시작에 포함되지 않은 내용

-   마이크 캡처 및 스피커 재생 코드. [`examples/realtime`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime)의 실시간 코드 예제를 참고하세요.
-   SIP / 전화 연동 attach 흐름. [실시간 전송](transport.md) 및 [SIP 섹션](guide.md#sip-and-telephony)을 참고하세요.

## 주요 설정

기본 세션이 동작하면, 다음으로 가장 많이 사용하는 설정은 다음과 같습니다:

-   `model_name`
-   `audio.input.format`, `audio.output.format`
-   `audio.input.transcription`
-   `audio.input.noise_reduction`
-   자동 턴 감지를 위한 `audio.input.turn_detection`
-   `audio.output.voice`
-   `tool_choice`, `prompt`, `tracing`
-   `async_tool_calls`, `guardrails_settings.debounce_text_length`, `tool_error_formatter`

`input_audio_format`, `output_audio_format`, `input_audio_transcription`, `turn_detection` 같은 기존의 평면 별칭도 여전히 동작하지만, 새 코드에서는 중첩 `audio` 설정이 권장됩니다.

수동 턴 제어의 경우 [실시간 에이전트 가이드](guide.md#manual-response-control)에 설명된 대로 원문 `session.update` / `input_audio_buffer.commit` / `response.create` 흐름을 사용하세요.

전체 스키마는 [`RealtimeRunConfig`][agents.realtime.config.RealtimeRunConfig] 및 [`RealtimeSessionModelSettings`][agents.realtime.config.RealtimeSessionModelSettings]를 참고하세요.

## 연결 옵션

환경 변수에 API 키를 설정하세요:

```bash
export OPENAI_API_KEY="your-api-key-here"
```

또는 세션 시작 시 직접 전달하세요:

```python
session = await runner.run(model_config={"api_key": "your-api-key"})
```

`model_config`는 다음도 지원합니다:

-   `url`: 사용자 지정 WebSocket 엔드포인트
-   `headers`: 사용자 지정 요청 헤더
-   `call_id`: 기존 실시간 통화에 attach. 이 저장소에서 문서화된 attach 흐름은 SIP 입니다.
-   `playback_tracker`: 사용자가 실제로 들은 오디오 양 보고

`headers`를 명시적으로 전달하면 SDK 는 `Authorization` 헤더를 자동으로 주입하지 **않습니다**.

Azure OpenAI 에 연결할 때는 `model_config["url"]`에 GA Realtime 엔드포인트 URL 을 전달하고 명시적 헤더를 사용하세요. 실시간 에이전트에서는 레거시 베타 경로(`/openai/realtime?api-version=...`)를 피하세요. 자세한 내용은 [실시간 에이전트 가이드](guide.md#low-level-access-and-custom-endpoints)를 참고하세요.

## 다음 단계

-   서버 측 WebSocket 과 SIP 중에서 선택하려면 [실시간 전송](transport.md)을 읽어보세요.
-   수명 주기, 구조화된 입력, 승인, 핸드오프, 가드레일, 저수준 제어는 [실시간 에이전트 가이드](guide.md)를 읽어보세요.
-   [`examples/realtime`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime)의 예제를 살펴보세요.