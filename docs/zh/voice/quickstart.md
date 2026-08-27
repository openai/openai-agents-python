---
search:
  exclude: true
---
# 快速入门

## 前提条件 {#prerequisites}

请确保已按照 Agents SDK 的基础[快速入门说明](../quickstart.md)完成操作，并设置好虚拟环境。然后，从 SDK 安装可选的语音依赖项：

```bash
pip install 'openai-agents[voice]'
```

下面的演示代码还使用了 [`sounddevice`](https://pypi.org/project/sounddevice/) 处理麦克风和扬声器 I/O，但它不属于 `voice` extra：

```bash
pip install sounddevice
```

## 概念 {#concepts}

需要了解的主要概念是 [`VoicePipeline`][agents.voice.pipeline.VoicePipeline]，它包含三个步骤：

1. 运行语音转文本模型，将音频转换为文本。
2. 运行您的代码（通常是智能体工作流）以生成结果。
3. 运行文本转语音模型，将结果文本转换回音频。

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

## 智能体 {#agents}

首先，我们来设置一些智能体。如果您曾使用此 SDK 构建过智能体，这些内容应该会很熟悉。我们将设置两个智能体、一项已配置的任务转移和一个工具。

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

## 语音管线 {#voice-pipeline}

我们将设置一个简单的语音管线，并使用 [`SingleAgentVoiceWorkflow`][agents.voice.workflow.SingleAgentVoiceWorkflow] 作为工作流。

```python
from agents.voice import SingleAgentVoiceWorkflow, VoicePipeline
pipeline = VoicePipeline(workflow=SingleAgentVoiceWorkflow(agent))
```

## 管线运行 {#run-the-pipeline}

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

## 完整整合 {#put-it-all-together}

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

运行此代码示例后，智能体将生成可供您收听的语音音频！请查看 [examples/voice/static](https://github.com/openai/openai-agents-python/tree/main/examples/voice/static) 中的代码示例，了解如何亲自与智能体进行语音对话。