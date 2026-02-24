---
search:
  exclude: true
---
# 빠른 시작

실시간 에이전트를 사용하면 OpenAI의 Realtime API로 AI 에이전트와 음성 대화를 할 수 있습니다. 이 가이드는 첫 번째 실시간 음성 에이전트를 만드는 과정을 안내합니다.

!!! warning "베타 기능"
실시간 에이전트는 베타입니다. 구현을 개선하는 과정에서 일부 변경 사항이 호환성을 깨뜨릴 수 있습니다.

## 사전 요구 사항

-   Python 3.10 이상
-   OpenAI API 키
-   OpenAI Agents SDK에 대한 기본적인 이해

## 설치

아직 설치하지 않았다면 OpenAI Agents SDK를 설치하세요:

```bash
pip install openai-agents
```

## 첫 실시간 에이전트 만들기

### 1. 필요한 구성 요소 가져오기

```python
import asyncio
from agents.realtime import RealtimeAgent, RealtimeRunner
```

### 2. 실시간 에이전트 만들기

```python
agent = RealtimeAgent(
    name="Assistant",
    instructions="You are a helpful voice assistant. Keep your responses conversational and friendly.",
)
```

### 3. 러너 설정하기

```python
runner = RealtimeRunner(
    starting_agent=agent,
    config={
        "model_settings": {
            "model_name": "gpt-realtime",
            "voice": "ash",
            "modalities": ["audio"],
            "input_audio_format": "pcm16",
            "output_audio_format": "pcm16",
            "input_audio_transcription": {"model": "gpt-4o-mini-transcribe"},
            "turn_detection": {"type": "semantic_vad", "interrupt_response": True},
        }
    }
)
```

### 4. 세션 시작하기

```python
# Start the session
session = await runner.run()

async with session:
    print("Session started! The agent will stream audio responses in real-time.")
    # Process events
    async for event in session:
        try:
            if event.type == "agent_start":
                print(f"Agent started: {event.agent.name}")
            elif event.type == "agent_end":
                print(f"Agent ended: {event.agent.name}")
            elif event.type == "handoff":
                print(f"Handoff from {event.from_agent.name} to {event.to_agent.name}")
            elif event.type == "tool_start":
                print(f"Tool started: {event.tool.name}")
            elif event.type == "tool_end":
                print(f"Tool ended: {event.tool.name}; output: {event.output}")
            elif event.type == "audio_end":
                print("Audio ended")
            elif event.type == "audio":
                # Enqueue audio for callback-based playback with metadata
                # Non-blocking put; queue is unbounded, so drops won’t occur.
                pass
            elif event.type == "audio_interrupted":
                print("Audio interrupted")
                # Begin graceful fade + flush in the audio callback and rebuild jitter buffer.
            elif event.type == "error":
                print(f"Error: {event.error}")
            elif event.type == "history_updated":
                pass  # Skip these frequent events
            elif event.type == "history_added":
                pass  # Skip these frequent events
            elif event.type == "raw_model_event":
                print(f"Raw model event: {_truncate_str(str(event.data), 200)}")
            else:
                print(f"Unknown event type: {event.type}")
        except Exception as e:
            print(f"Error processing event: {_truncate_str(str(e), 200)}")

def _truncate_str(s: str, max_length: int) -> str:
    if len(s) > max_length:
        return s[:max_length] + "..."
    return s
```

## 전체 예제(동일한 흐름을 한 파일로)

동일한 빠른 시작 흐름을 단일 스크립트로 다시 작성한 것입니다.

```python
import asyncio
from agents.realtime import RealtimeAgent, RealtimeRunner

async def main():
    # Create the agent
    agent = RealtimeAgent(
        name="Assistant",
        instructions="You are a helpful voice assistant. Keep responses brief and conversational.",
    )
    # Set up the runner with configuration
    runner = RealtimeRunner(
        starting_agent=agent,
        config={
            "model_settings": {
                "model_name": "gpt-realtime",
                "voice": "ash",
                "modalities": ["audio"],
                "input_audio_format": "pcm16",
                "output_audio_format": "pcm16",
                "input_audio_transcription": {"model": "gpt-4o-mini-transcribe"},
                "turn_detection": {"type": "semantic_vad", "interrupt_response": True},
            }
        },
    )
    # Start the session
    session = await runner.run()

    async with session:
        print("Session started! The agent will stream audio responses in real-time.")
        # Process events
        async for event in session:
            try:
                if event.type == "agent_start":
                    print(f"Agent started: {event.agent.name}")
                elif event.type == "agent_end":
                    print(f"Agent ended: {event.agent.name}")
                elif event.type == "handoff":
                    print(f"Handoff from {event.from_agent.name} to {event.to_agent.name}")
                elif event.type == "tool_start":
                    print(f"Tool started: {event.tool.name}")
                elif event.type == "tool_end":
                    print(f"Tool ended: {event.tool.name}; output: {event.output}")
                elif event.type == "audio_end":
                    print("Audio ended")
                elif event.type == "audio":
                    # Enqueue audio for callback-based playback with metadata
                    # Non-blocking put; queue is unbounded, so drops won’t occur.
                    pass
                elif event.type == "audio_interrupted":
                    print("Audio interrupted")
                    # Begin graceful fade + flush in the audio callback and rebuild jitter buffer.
                elif event.type == "error":
                    print(f"Error: {event.error}")
                elif event.type == "history_updated":
                    pass  # Skip these frequent events
                elif event.type == "history_added":
                    pass  # Skip these frequent events
                elif event.type == "raw_model_event":
                    print(f"Raw model event: {_truncate_str(str(event.data), 200)}")
                else:
                    print(f"Unknown event type: {event.type}")
            except Exception as e:
                print(f"Error processing event: {_truncate_str(str(e), 200)}")

def _truncate_str(s: str, max_length: int) -> str:
    if len(s) > max_length:
        return s[:max_length] + "..."
    return s

if __name__ == "__main__":
    # Run the session
    asyncio.run(main())
```

## 구성 및 배포 참고 사항

기본 세션이 실행된 뒤 아래 옵션을 사용하세요.

### 모델 설정

-   `model_name`: 사용 가능한 실시간 모델에서 선택(예: `gpt-realtime`)
-   `voice`: 음성 선택(`alloy`, `echo`, `fable`, `onyx`, `nova`, `shimmer`)
-   `modalities`: 텍스트 또는 오디오 활성화(`["text"]` 또는 `["audio"]`)
-   `output_modalities`: 출력이 텍스트 및/또는 오디오로만 나오도록 선택적으로 제한(`["text"]`, `["audio"]`, 또는 둘 다)

### 오디오 설정

-   `input_audio_format`: 입력 오디오 형식(`pcm16`, `g711_ulaw`, `g711_alaw`)
-   `output_audio_format`: 출력 오디오 형식
-   `input_audio_transcription`: 전사 구성
-   `input_audio_noise_reduction`: 입력 노이즈 감소 구성(`near_field` 또는 `far_field`)

### 턴 감지

-   `type`: 감지 방식(`server_vad`, `semantic_vad`)
-   `threshold`: 음성 활동 임계값(0.0-1.0)
-   `silence_duration_ms`: 턴 종료를 감지하기 위한 무음 지속 시간
-   `prefix_padding_ms`: 발화 전 오디오 패딩

### 실행 설정

-   `async_tool_calls`: 함수 도구를 비동기로 실행할지 여부(기본값 `True`)
-   `guardrails_settings.debounce_text_length`: 출력 가드레일을 실행하기 전에 누적되어야 하는 전사 최소 크기(기본값 `100`)
-   `tool_error_formatter`: 모델에 보이는 도구 오류 메시지를 커스터마이즈하는 콜백

전체 스키마는 [`RealtimeRunConfig`][agents.realtime.config.RealtimeRunConfig] 및 [`RealtimeSessionModelSettings`][agents.realtime.config.RealtimeSessionModelSettings]의 API 레퍼런스를 참고하세요.

### 인증

환경에 OpenAI API 키가 설정되어 있는지 확인하세요:

```bash
export OPENAI_API_KEY="your-api-key-here"
```

또는 세션을 만들 때 직접 전달하세요:

```python
session = await runner.run(model_config={"api_key": "your-api-key"})
```

### Azure OpenAI 엔드포인트 형식

OpenAI의 기본 엔드포인트 대신 Azure OpenAI에 연결하는 경우, GA Realtime URL을 `model_config["url"]`에 전달하고 인증 헤더를 명시적으로 설정하세요.

```python
session = await runner.run(
    model_config={
        "url": "wss://<your-resource>.openai.azure.com/openai/v1/realtime?model=<deployment-name>",
        "headers": {"api-key": "<your-azure-api-key>"},
    }
)
```

bearer 토큰도 사용할 수 있습니다:

```python
session = await runner.run(
    model_config={
        "url": "wss://<your-resource>.openai.azure.com/openai/v1/realtime?model=<deployment-name>",
        "headers": {"authorization": f"Bearer {token}"},
    }
)
```

실시간 에이전트에서는 레거시 베타 경로(`/openai/realtime?api-version=...`) 사용을 피하세요. SDK는 GA Realtime 인터페이스를 기대합니다.

## 다음 단계

-   [실시간 에이전트 더 알아보기](guide.md)
-   [examples/realtime](https://github.com/openai/openai-agents-python/tree/main/examples/realtime) 폴더의 동작하는 예제 확인하기
-   에이전트에 도구 추가하기
-   에이전트 간 핸드오프 구현하기
-   안전을 위한 가드레일 설정하기