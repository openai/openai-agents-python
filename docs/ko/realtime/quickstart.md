---
search:
  exclude: true
---
# 빠른 시작

실시간 에이전트는 OpenAI의 Realtime API를 사용해 AI 에이전트와 음성 대화를 가능하게 합니다. 이 가이드는 첫 실시간 음성 에이전트를 만드는 과정을 안내합니다.

!!! warning "베타 기능"
실시간 에이전트는 베타 버전입니다. 구현을 개선하는 동안 호환성 깨짐이 발생할 수 있습니다.

## 사전 준비

-   Python 3.9 이상
-   OpenAI API 키
-   OpenAI Agents SDK에 대한 기본적인 이해

## 설치

아직 설치하지 않았다면 OpenAI Agents SDK를 설치하세요:

```bash
pip install openai-agents
```

## 첫 실시간 에이전트 생성

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

### 3. 러너 설정

```python
runner = RealtimeRunner(
    starting_agent=agent,
    config={
        "model_settings": {
            "model_name": "gpt-4o-realtime-preview",
            "voice": "alloy",
            "modalities": ["text", "audio"],
        }
    }
)
```

### 4. 세션 시작

```python
async def main():
    # Start the realtime session
    session = await runner.run()

    async with session:
        # Send a text message to start the conversation
        await session.send_message("Hello! How are you today?")

        # The agent will stream back audio in real-time (not shown in this example)
        # Listen for events from the session
        async for event in session:
            if event.type == "response.audio_transcript.done":
                print(f"Assistant: {event.transcript}")
            elif event.type == "conversation.item.input_audio_transcription.completed":
                print(f"User: {event.transcript}")

# Run the session
asyncio.run(main())
```

## 전체 예시

다음은 완전한 작동 예시입니다:

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
                "model_name": "gpt-4o-realtime-preview",
                "voice": "alloy",
                "modalities": ["text", "audio"],
                "input_audio_transcription": {
                    "model": "whisper-1"
                },
                "turn_detection": {
                    "type": "server_vad",
                    "threshold": 0.5,
                    "prefix_padding_ms": 300,
                    "silence_duration_ms": 200
                }
            }
        }
    )

    # Start the session
    session = await runner.run()

    async with session:
        print("Session started! The agent will stream audio responses in real-time.")

        # Process events
        async for event in session:
            if event.type == "response.audio_transcript.done":
                print(f"Assistant: {event.transcript}")
            elif event.type == "conversation.item.input_audio_transcription.completed":
                print(f"User: {event.transcript}")
            elif event.type == "error":
                print(f"Error: {event.error}")
                break

if __name__ == "__main__":
    asyncio.run(main())
```

## 구성 옵션

### 모델 설정

-   `model_name`: 사용 가능한 실시간 모델 중에서 선택 (예: `gpt-4o-realtime-preview`)
-   `voice`: 음성 선택 (`alloy`, `echo`, `fable`, `onyx`, `nova`, `shimmer`)
-   `modalities`: 텍스트 및/또는 오디오 활성화 (`["text", "audio"]`)

### 오디오 설정

-   `input_audio_format`: 입력 오디오 형식 (`pcm16`, `g711_ulaw`, `g711_alaw`)
-   `output_audio_format`: 출력 오디오 형식
-   `input_audio_transcription`: 음성 인식(전사) 구성

### 턴 감지

-   `type`: 감지 방식 (`server_vad`, `semantic_vad`)
-   `threshold`: 음성 활동 임계값 (0.0-1.0)
-   `silence_duration_ms`: 턴 종료를 감지할 무음 지속 시간
-   `prefix_padding_ms`: 발화 전 오디오 패딩

## 다음 단계

-   [실시간 에이전트 자세히 알아보기](guide.md)
-   [examples/realtime](https://github.com/openai/openai-agents-python/tree/main/examples/realtime) 폴더에서 동작하는 code examples 확인
-   에이전트에 도구 추가
-   에이전트 간 핸드오프 구현
-   안전을 위한 가드레일 설정

## 인증

환경 변수에 OpenAI API 키가 설정되어 있는지 확인하세요:

```bash
export OPENAI_API_KEY="your-api-key-here"
```

또는 세션을 생성할 때 직접 전달하세요:

```python
session = await runner.run(model_config={"api_key": "your-api-key"})
```