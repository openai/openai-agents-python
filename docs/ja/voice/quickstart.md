---
search:
  exclude: true
---
# クイックスタート

## 前提条件 {#prerequisites}

Agents SDKの基本的な[クイックスタート手順](../quickstart.md)に従い、仮想環境をセットアップしていることを確認してください。次に、SDK からオプションの音声依存関係をインストールします。

```bash
pip install 'openai-agents[voice]'
```

以下のデモコードでは、マイクとスピーカーの I/O に [`sounddevice`](https://pypi.org/project/sounddevice/) も使用します。これは `voice` extra には含まれていません。

```bash
pip install sounddevice
```

## 概念 {#concepts}

理解しておくべき主な概念は [`VoicePipeline`][agents.voice.pipeline.VoicePipeline] です。これは次の 3 ステップのプロセスです。

1. 音声テキスト変換モデルを実行して、音声をテキストに変換します。
2. 通常はエージェントワークフローであるコードを実行して、結果を生成します。
3. テキスト音声変換モデルを実行して、結果のテキストを音声に戻します。

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

## エージェント {#agents}

まず、複数のエージェントをセットアップします。この SDK でエージェントを構築したことがあれば、見慣れた内容です。2 つのエージェント、設定済みのハンドオフ、ツールを 1 つ用意します。

```python
import random

from agents import Agent
from agents.decorators import tool
from agents.extensions.handoff_prompt import prompt_with_handoff_instructions


@tool
def get_weather(city: str) -> str:
    """Get the weather for a given city."""
    print(f"[debug] get_weather called with city: {city}")
    choices = ["sunny", "cloudy", "rainy", "snowy"]
    return f"The weather in {city} is {random.choice(choices)}."


spanish_agent = Agent(
    name="Spanish",
    handoff_description="A Spanish-speaking agent.",
    instructions=prompt_with_handoff_instructions(
        "You're speaking to a human, so be polite and concise. Speak in Spanish.",
    ),
    model="gpt-5.6-sol",
)

agent = Agent(
    name="Assistant",
    instructions=prompt_with_handoff_instructions(
        "You're speaking to a human, so be polite and concise. If the user speaks in Spanish, hand off to the Spanish agent.",
    ),
    model="gpt-5.6-sol",
    handoffs=[spanish_agent],
    tools=[get_weather],
)
```

## 音声パイプライン {#voice-pipeline}

ワークフローに [`SingleAgentVoiceWorkflow`][agents.voice.workflow.SingleAgentVoiceWorkflow] を使用して、シンプルな音声パイプラインをセットアップします。

```python
from agents.voice import SingleAgentVoiceWorkflow, VoicePipeline
pipeline = VoicePipeline(workflow=SingleAgentVoiceWorkflow(agent))
```

## パイプラインの実行 {#run-the-pipeline}

```python
import numpy as np
import sounddevice as sd
from agents.voice import AudioInput

# For simplicity, we'll just create 3 seconds of silence
# In reality, you'd get microphone data
buffer = np.zeros(24000 * 3, dtype=np.int16)
audio_input = AudioInput(buffer=buffer)

result = await pipeline.run(audio_input)

# Create an audio player using `sounddevice`
player = sd.OutputStream(samplerate=24000, channels=1, dtype=np.int16)
player.start()

# Play the audio stream as it comes in
async for event in result.stream():
    if event.type == "voice_stream_event_audio":
        player.write(event.data)

```

## 全体の統合 {#put-it-all-together}

```python
import asyncio
import random

import numpy as np
import sounddevice as sd

from agents import Agent
from agents.decorators import tool
from agents.voice import (
    AudioInput,
    SingleAgentVoiceWorkflow,
    VoicePipeline,
)
from agents.extensions.handoff_prompt import prompt_with_handoff_instructions


@tool
def get_weather(city: str) -> str:
    """Get the weather for a given city."""
    print(f"[debug] get_weather called with city: {city}")
    choices = ["sunny", "cloudy", "rainy", "snowy"]
    return f"The weather in {city} is {random.choice(choices)}."


spanish_agent = Agent(
    name="Spanish",
    handoff_description="A Spanish-speaking agent.",
    instructions=prompt_with_handoff_instructions(
        "You're speaking to a human, so be polite and concise. Speak in Spanish.",
    ),
    model="gpt-5.6-sol",
)

agent = Agent(
    name="Assistant",
    instructions=prompt_with_handoff_instructions(
        "You're speaking to a human, so be polite and concise. If the user speaks in Spanish, hand off to the Spanish agent.",
    ),
    model="gpt-5.6-sol",
    handoffs=[spanish_agent],
    tools=[get_weather],
)


async def main():
    pipeline = VoicePipeline(workflow=SingleAgentVoiceWorkflow(agent))
    buffer = np.zeros(24000 * 3, dtype=np.int16)
    audio_input = AudioInput(buffer=buffer)

    result = await pipeline.run(audio_input)

    # Create an audio player using `sounddevice`
    player = sd.OutputStream(samplerate=24000, channels=1, dtype=np.int16)
    player.start()

    # Play the audio stream as it comes in
    async for event in result.stream():
        if event.type == "voice_stream_event_audio":
            player.write(event.data)


if __name__ == "__main__":
    asyncio.run(main())
```

このコード例を実行すると、エージェントが音声を生成し、実際に聞くことができます。自分でエージェントに話しかけられるデモについては、[examples/voice/static](https://github.com/openai/openai-agents-python/tree/main/examples/voice/static) のコード例をご覧ください。