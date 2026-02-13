---
search:
  exclude: true
---
# 파이프라인과 워크플로

[`VoicePipeline`][agents.voice.pipeline.VoicePipeline]은 에이전트 워크플로를 음성 앱으로 쉽게 전환할 수 있게 해주는 클래스입니다. 실행할 워크플로를 전달하면, 파이프라인이 입력 오디오 전사, 오디오 종료 시점 감지, 적절한 타이밍에 워크플로 호출, 그리고 워크플로 출력의 오디오 변환까지 처리합니다.

```mermaid
graph LR
    %% Input
    A["🎤 Audio Input"]

    %% Voice Pipeline
    subgraph Voice_Pipeline [Voice Pipeline]
        direction TB
        B["Transcribe (speech-to-text)"]
        C["Your Code"]:::highlight
        D["Text-to-speech"]
        B --> C --> D
    end

    %% Output
    E["🎧 Audio Output"]

    %% Flow
    A --> Voice_Pipeline
    Voice_Pipeline --> E

    %% Custom styling
    classDef highlight fill:#ffcc66,stroke:#333,stroke-width:1px,font-weight:700;

```

## 파이프라인 구성

파이프라인을 생성할 때 몇 가지를 설정할 수 있습니다:

1. [`workflow`][agents.voice.workflow.VoiceWorkflowBase]: 새 오디오가 전사될 때마다 실행되는 코드입니다
2. 사용되는 [`speech-to-text`][agents.voice.model.STTModel] 및 [`text-to-speech`][agents.voice.model.TTSModel] 모델
3. [`config`][agents.voice.pipeline_config.VoicePipelineConfig]: 다음과 같은 항목을 구성할 수 있습니다
    - 모델 제공자: 모델 이름을 모델에 매핑할 수 있습니다
    - 트레이싱: 트레이싱 비활성화 여부, 오디오 파일 업로드 여부, 워크플로 이름, trace ID 등
    - TTS 및 STT 모델 설정: 프롬프트, 언어, 사용되는 데이터 타입 등

## 파이프라인 실행

[`run()`][agents.voice.pipeline.VoicePipeline.run] 메서드로 파이프라인을 실행할 수 있으며, 오디오 입력을 두 가지 형태로 전달할 수 있습니다:

1. [`AudioInput`][agents.voice.input.AudioInput]: 전체 오디오 전사본이 있고, 이에 대한 결과만 생성하고 싶을 때 사용합니다. 화자가 말하기를 끝냈는지 감지할 필요가 없는 경우에 유용합니다. 예를 들어, 사전 녹음된 오디오가 있거나 사용자가 말하기를 끝낸 시점이 명확한 푸시-투-토크 앱에서 사용할 수 있습니다
2. [`StreamedAudioInput`][agents.voice.input.StreamedAudioInput]: 사용자가 말하기를 끝냈는지 감지해야 할 수 있을 때 사용합니다. 감지되는 대로 오디오 청크를 푸시할 수 있으며, 음성 파이프라인은 "activity detection"이라는 프로세스를 통해 적절한 시점에 에이전트 워크플로를 자동으로 실행합니다

## 결과

음성 파이프라인 실행 결과는 [`StreamedAudioResult`][agents.voice.result.StreamedAudioResult]입니다. 이는 이벤트가 발생하는 대로 스트리밍할 수 있게 해주는 객체입니다. 몇 가지 유형의 [`VoiceStreamEvent`][agents.voice.events.VoiceStreamEvent]가 있으며, 예시는 다음과 같습니다:

1. [`VoiceStreamEventAudio`][agents.voice.events.VoiceStreamEventAudio]: 오디오 청크를 포함합니다
2. [`VoiceStreamEventLifecycle`][agents.voice.events.VoiceStreamEventLifecycle]: 턴 시작/종료 같은 라이프사이클 이벤트를 알려줍니다
3. [`VoiceStreamEventError`][agents.voice.events.VoiceStreamEventError]: 오류 이벤트입니다

```python

result = await pipeline.run(input)

async for event in result.stream():
    if event.type == "voice_stream_event_audio":
        # play audio
    elif event.type == "voice_stream_event_lifecycle":
        # lifecycle
    elif event.type == "voice_stream_event_error":
        # error
    ...
```

## 모범 사례

### 인터럽션(중단 처리)

Agents SDK는 현재 [`StreamedAudioInput`][agents.voice.input.StreamedAudioInput]에 대해 기본 제공 인터럽션(중단 처리) 지원을 제공하지 않습니다. 대신 감지된 각 턴마다 워크플로가 별도로 실행되도록 트리거합니다. 애플리케이션 내부에서 인터럽션(중단 처리)을 처리하려면 [`VoiceStreamEventLifecycle`][agents.voice.events.VoiceStreamEventLifecycle] 이벤트를 수신하면 됩니다. `turn_started`는 새 턴이 전사되었고 처리가 시작됨을 나타냅니다. `turn_ended`는 해당 턴에 대해 모든 오디오가 디스패치된 후 트리거됩니다. 이 이벤트를 사용해 모델이 턴을 시작할 때 화자의 마이크를 음소거하고, 해당 턴과 관련된 모든 오디오를 플러시한 뒤 음소거를 해제할 수 있습니다